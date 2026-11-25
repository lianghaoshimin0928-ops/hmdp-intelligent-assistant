package com.hmdp.service.impl;

import cn.hutool.core.bean.BeanUtil;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.hmdp.dto.Result;
import com.hmdp.entity.VoucherOrder;
import com.hmdp.mapper.VoucherOrderMapper;
import com.hmdp.service.ISeckillVoucherService;
import com.hmdp.service.IVoucherOrderService;
import com.hmdp.utils.RedisIdWorker;
import com.hmdp.utils.UserHolder;
import lombok.extern.slf4j.Slf4j;
import org.redisson.api.RLock;
import org.redisson.api.RedissonClient;
import org.springframework.aop.framework.AopContext;
import org.springframework.beans.BeanUtils;
import org.springframework.core.io.ClassPathResource;
import org.springframework.data.redis.connection.stream.*;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import javax.annotation.PostConstruct;
import javax.annotation.Resource;
import java.time.Duration;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * <p>
 * 秒杀异步下单：
 * <pre>
 *   生产端（HTTP 线程）：Lua 原子判断资格 → 入阻塞队列 → 立刻返回 orderId
 *   消费端（单个后台线程）：take() 取订单 → Redisson 锁 → 事务落库
 * </pre>
 * </p>
 *
 * @author 虎哥
 * @since 2021-12-22
 */
@Slf4j
@Service
public class VoucherOrderServiceImpl extends ServiceImpl<VoucherOrderMapper, VoucherOrder> implements IVoucherOrderService {

    @Resource
    private ISeckillVoucherService seckillVoucherService;

    @Resource
    private RedisIdWorker redisIdWorker;

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    @Resource
    private RedissonClient redissonClient;

    /** 秒杀资格判断脚本：KEYS[1]=voucherId，ARGV[1]=userId */
    private static final DefaultRedisScript<Long> SECKILL_SCRIPT;

    static {
        SECKILL_SCRIPT = new DefaultRedisScript<>();
        SECKILL_SCRIPT.setLocation(new ClassPathResource("seckill.lua"));
        SECKILL_SCRIPT.setResultType(Long.class);
    }

    /** 用消息队列stream */


    /** 单线程消费：保证顺序、不给数据库压力 */
    private static final ExecutorService SECKILL_EXECUTOR = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "seckill-order-consumer");
        t.setDaemon(false);
        return t;
    });

    /** 应用启动后立刻拉起消费线程（此时依赖都已注入完成，早于对外提供服务） */
    @PostConstruct
    private void init() {
        SECKILL_EXECUTOR.submit(new VoucherOrderHandler());

        log.info("秒杀订单消费者已启动");
    }

    // ==================== 消费端 ====================

    private class VoucherOrderHandler implements Runnable {
        @Override
        public void run() {
            String queueName = "stream.orders";
            while (true) {
                try {
                    //1.获取消息队列中的 订单信息，XREADGROUP GROUP g1 c1 COUNT 1
                    List<MapRecord<String, Object, Object>> list = stringRedisTemplate.opsForStream().read(
                            Consumer.from("g1", "c1"),
                            StreamReadOptions.empty().count(1).block(Duration.ofSeconds(2)),
                            StreamOffset.create(queueName, ReadOffset.lastConsumed())
                    );

                    //2.判断是否获取成功
                    if (list == null || list.isEmpty()) {
                        continue;
                    }

                    //3.解析订单信息
                    MapRecord<String, Object, Object> record = list.get(0);
                    Map<Object, Object> value = record.getValue();
                    VoucherOrder voucherOrder = BeanUtil.fillBeanWithMap(value, new VoucherOrder(),true);
                    log.info("处理订单：{}", voucherOrder);
                    //4.处理订单
                    handleVoucherOrder(voucherOrder);

                    //5.确认消息
                    stringRedisTemplate.opsForStream().acknowledge(queueName, "g1", record.getId());
                } catch (Exception e) {
                    log.error("处理订单异常", e);
                    handlePandingList();
                }
            }
        }


        //消费端处理未处理完的消息
        public void handlePandingList() {
            String queueName = "stream.orders";
            while (true) {
                try {
                    //1.获取消息队列中的 订单信息，XREADGROUP GROUP g1 c1 COUNT 1
                    List<MapRecord<String, Object, Object>> list = stringRedisTemplate.opsForStream().read(
                            Consumer.from("g1", "c1"),
                            StreamReadOptions.empty().count(1),
                            StreamOffset.create(queueName, ReadOffset.from("0"))
                    );

                    //2.判断是否获取成功
                    if (list == null || list.isEmpty()) {
                        break;
                    }

                    //3.解析订单信息
                    MapRecord<String, Object, Object> record = list.get(0);
                    Map<Object, Object> value = record.getValue();
                    VoucherOrder voucherOrder = BeanUtil.fillBeanWithMap(value, new VoucherOrder(),true);
                    log.info("处理订单：{}", voucherOrder);
                    //4.处理订单
                    handleVoucherOrder(voucherOrder);

                    //5.确认消息
                    stringRedisTemplate.opsForStream().acknowledge(queueName, "g1", record.getId());
                } catch (Exception e) {
                    log.info("处理PandingList异常", e);
                    try{
                        Thread.sleep(20);
                    } catch (InterruptedException e1) {
                        e1.printStackTrace();
                    }

                }
            }
        }
    }

    private void handleVoucherOrder(VoucherOrder voucherOrder) {
            Long userId = voucherOrder.getUserId();
            RLock lock = redissonClient.getLock("order:" + userId);
            boolean isLocked = false;
            try {
                isLocked = lock.tryLock();
                if (!isLocked) {
                    log.error("获取锁失败，userId={}", userId);
                    return;
                }
                // 自己取代理，不依赖成员变量（原代码把 proxy 声明成了局部变量，
                // 导致成员变量永远是 null，消费端一调用就 NPE）
                IVoucherOrderService proxy = (IVoucherOrderService) AopContext.currentProxy();
                proxy.createVocherOrder(voucherOrder);
            } finally {
                if (isLocked) {
                    lock.unlock();
                }
            }
    }

    // ==================== 生产端 ====================

    @Override
    public Result seckillVoucher(Long voucherId) {
        // 当前用户（这里是 HTTP 线程，UserHolder 有值）
        Long userId = UserHolder.getUser().getId();
        //获取订单ID
        Long orderId = redisIdWorker.nextId("order");

        // 1. 执行 lua：原子完成 有效期校验 + 库存判断 + 一人一单判断 + 扣减
        Long result;
        try {
            result = stringRedisTemplate.execute(
                    SECKILL_SCRIPT,
                    Collections.singletonList(voucherId.toString()),
                    userId.toString(), orderId.toString(), voucherId.toString());
        } catch (Exception e) {
            log.error("执行秒杀脚本失败，voucherId={}", voucherId, e);
            return Result.fail("秒杀失败，请稍后重试");
        }

        // 防御：脚本没返回值时不要直接 result != 0（Long 拆箱会 NPE）
        if (result == null) {
            log.error("秒杀脚本返回 null，voucherId={}", voucherId);
            return Result.fail("秒杀失败，请稍后重试");
        }

        // 2. 判断是否有资格
        if (result != 0) {
            if (result == 1) {
                return Result.fail("库存不足");
            } else if (result == 2) {
                return Result.fail("请勿重复下单");
            } else if (result == 3) {
                return Result.fail("优惠券不在有效期内");
            }
            return Result.fail("秒杀失败");
        }


        return Result.ok(orderId);
    }

    // ==================== 落库（由消费端调用） ====================

    @Transactional
    @Override
    public Result createVocherOrder(VoucherOrder voucherOrder) {
        // 关键：这里是异步消费线程，UserHolder（ThreadLocal）里【没有】当前用户！
        // 原代码用 UserHolder.getUser().getId() → 必然 NPE。必须从订单对象里取。
        Long userId = voucherOrder.getUserId();
        Long voucherId = voucherOrder.getVoucherId();

        // 1. 一人一单（DB 兜底）
        //    放在扣库存之前：因为返回 Result.fail 不会触发事务回滚，
        //    如果先扣库存再判重，重复下单会白扣一件库存（少卖）
        int count = query()
                .eq("user_id", userId)
                .eq("voucher_id", voucherId)
                .count();
        if (count > 0) {
            log.error("用户重复下单，userId={}, voucherId={}", userId, voucherId);
            return Result.fail("请勿重复下单");
        }

        // 2. 扣库存（乐观锁兜底：stock > 0 才扣）
        boolean success = seckillVoucherService.update()
                .setSql("stock = stock - 1")
                .eq("voucher_id", voucherId)
                .gt("stock", 0)
                .update();
        if (!success) {
            log.error("扣减库存失败（已售罄），voucherId={}", voucherId);
            return Result.fail("库存不足");
        }

        // 3. 保存订单（生产环境建议给 tb_voucher_order 加 (user_id, voucher_id) 唯一索引）
        save(voucherOrder);
        log.info("订单落库成功，orderId={}, userId={}, voucherId={}",
                voucherOrder.getId(), userId, voucherId);
        return Result.ok(voucherOrder.getId());
    }


    /* ==================== 改造前的同步版（留作对比） ====================
    @Override
    public Result seckillVoucher(Long voucherId) {
        SeckillVoucher voucher = seckillVoucherService.getById(voucherId);
        if (voucher.getBeginTime().isAfter(LocalDateTime.now())
                || voucher.getEndTime().isBefore(LocalDateTime.now()))
            return Result.fail("优惠卷不在有效期内");
        if (voucher.getStock() <= 0)
            return Result.fail("优惠卷已售罄");

        Long userId = UserHolder.getUser().getId();
        RLock lock = redissonClient.getLock("order:" + userId);
        if (!lock.tryLock()) return Result.fail("请勿重复下单");
        try {
            IVoucherOrderService proxy = (IVoucherOrderService) AopContext.currentProxy();
            return proxy.createVocherOrder(voucherId);
        } finally {
            lock.unlock();
        }
    }
    ==================================================================== */

}
