package com.hmdp.service.impl;

import cn.hutool.core.bean.BeanUtil;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.hmdp.dto.Result;
import com.hmdp.entity.VoucherOrder;
import com.hmdp.enums.ConsumeResult;
import com.hmdp.mapper.VoucherOrderMapper;
import com.hmdp.service.ISeckillVoucherService;
import com.hmdp.service.IVoucherOrderService;
import com.hmdp.utils.RedisIdWorker;
import com.hmdp.utils.UserHolder;
import lombok.extern.slf4j.Slf4j;
import org.redisson.api.RLock;
import org.redisson.api.RedissonClient;
import org.springframework.core.io.ClassPathResource;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.data.domain.Range;
import org.springframework.data.redis.connection.stream.Consumer;
import org.springframework.data.redis.connection.stream.MapRecord;
import org.springframework.data.redis.connection.stream.PendingMessage;
import org.springframework.data.redis.connection.stream.PendingMessages;
import org.springframework.data.redis.connection.stream.ReadOffset;
import org.springframework.data.redis.connection.stream.StreamOffset;
import org.springframework.data.redis.connection.stream.StreamReadOptions;
import org.springframework.data.redis.core.RedisCallback;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

import javax.annotation.PostConstruct;
import javax.annotation.PreDestroy;
import javax.annotation.Resource;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;

/**
 * <p>
 * 秒杀异步下单 —— 生产端 / 消费端分离。
 * <pre>
 *   生产端（HTTP 线程）：Lua 原子完成「有效期校验 + 库存判断 + 一人一单 + 扣减 + XADD」
 *                        成功后立刻返回 orderId，不等数据库
 *   消费端（常驻线程）：XREADGROUP 拉取 → Redisson 加锁 → 事务落库 → 按结果决定 ACK
 * </pre>
 * </p>
 *
 * <b>本类相对初版做的四处加固（都是真实缺陷）：</b>
 * <ol>
 *   <li><b>ACK 与业务结果解耦</b>：初版 {@code handleVoucherOrder} 返回 void，
 *       拿不到锁直接 return，外层却照样 ACK —— Redis 已扣库存、已占一人一单，
 *       但数据库没订单，用户拿不到券也无法重试。现在返回 {@link ConsumeResult}，
 *       只有业务终结才 ACK。</li>
 *   <li><b>pending 有界重试 + 死信队列</b>：初版 {@code handlePandingList} 是
 *       {@code while(true)} 且 catch 里 sleep 后继续，一条毒消息会永久卡死消费线程。
 *       现在按投递次数判断，超过上限转 {@code stream.orders.dlq} 再 ACK。</li>
 *   <li><b>consumer 名称带主机名</b>：初版硬编码 {@code c1}，多实例部署会互相抢消息、
 *       pending 归属混乱。现在用主机名做 consumer 名，天然支持水平扩容。</li>
 *   <li><b>一人一单以数据库唯一索引为权威</b>：初版靠 {@code count()} 查再 insert，
 *       两步之间存在竞态。现在先 insert，由 {@code (user_id, voucher_id)} 唯一索引拦截，
 *       捕获 {@link DuplicateKeyException} 视为重复。</li>
 * </ol>
 *
 * @author 虎哥
 * @since 2021-12-22
 */
@Slf4j
@Service
public class VoucherOrderServiceImpl extends ServiceImpl<VoucherOrderMapper, VoucherOrder> implements IVoucherOrderService {

    /** 秒杀订单消息流 */
    private static final String QUEUE_NAME = "stream.orders";
    /** 死信队列：重试耗尽的订单进这里，供人工/补偿任务处理 */
    private static final String DLQ_NAME = "stream.orders.dlq";
    /** 消费组 */
    private static final String GROUP_NAME = "g1";
    /** 最大重试次数（按投递次数算） */
    private static final int MAX_RETRY = 3;
    /** 触发一次 pending 扫描的间隔 */
    private static final long DRAIN_INTERVAL_MS = 5_000L;
    /** 单次扫描批量 */
    private static final int DRAIN_BATCH = 50;
    /** 抢锁等待时间：不再用无参 tryLock（立即失败→丢消息） */
    private static final long LOCK_WAIT_SECONDS = 1L;

    @Resource
    private ISeckillVoucherService seckillVoucherService;

    @Resource
    private RedisIdWorker redisIdWorker;

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    @Resource
    private RedissonClient redissonClient;

    @Resource
    private PlatformTransactionManager transactionManager;

    /**
     * 显式事务模板。
     * <p>
     * <b>为什么不用 @Transactional + AopContext.currentProxy()</b>：
     * {@code AopContext.currentProxy()} 依赖 ThreadLocal，只有当「当前线程正处在某次 AOP 调用中」
     * 才能拿到代理对象。消费端是独立的常驻线程，它根本不在 AOP 调用栈里，
     * 调用它必然抛 {@code IllegalStateException: Cannot find current proxy}
     * ——初版就是这样：消息每次都被当成失败，秒杀的订单永远落不了库。
     * <p>
     * 用 {@link TransactionTemplate} 显式划定事务边界，与调用线程、代理机制都无关，
     * 在异步消费场景下才是正确选择。
     */
    private TransactionTemplate txTemplate;

    /** 秒杀资格判断脚本：KEYS[1]=voucherId，ARGV[1]=userId ARGV[2]=orderId ARGV[3]=voucherId */
    private static final DefaultRedisScript<Long> SECKILL_SCRIPT;

    static {
        SECKILL_SCRIPT = new DefaultRedisScript<>();
        SECKILL_SCRIPT.setLocation(new ClassPathResource("seckill.lua"));
        SECKILL_SCRIPT.setResultType(Long.class);
    }

    /**
     * 消费端标识。
     * <p>用「主机名」而非固定 {@code c1}：多实例部署时每个实例拥有独立的 pending 归属，
     * 不会互相抢消息；扩容只要加实例，不用改代码。</p>
     */
    private final String consumerName = buildConsumerName();

    /** 常驻消费线程。单线程 = 顺序消费；要提吞吐应该加消费者实例，而不是给单实例加线程 */
    private volatile Thread consumerThread;

    private volatile boolean running = true;

    private static String buildConsumerName() {
        try {
            return "consumer-" + InetAddress.getLocalHost().getHostName();
        } catch (Exception e) {
            return "consumer-" + Long.toHexString(System.currentTimeMillis());
        }
    }

    @PostConstruct
    private void init() {
        this.txTemplate = new TransactionTemplate(transactionManager);

        ensureConsumerGroup();

        consumerThread = new Thread(this::consumeLoop, "seckill-order-consumer");
        consumerThread.setDaemon(false);
        consumerThread.start();
        log.info("秒杀订单消费者已启动，consumerName={}, group={}, queue={}", consumerName, GROUP_NAME, QUEUE_NAME);
    }

    /**
     * 启动时确保「消息流 + 消费组」存在。
     * <p>
     * 原实现假设 {@code stream.orders} 和消费组 {@code g1} 已存在（教程里是手动跑
     * {@code XGROUP CREATE stream.orders g1 0 MKSTREAM}）。部署到新环境时忘了这一步，
     * 消费端就会一直抛 {@code NOGROUP} 而完全失效 —— 但接口看起来还是「抢购成功」。
     * 这类问题排查成本很高，所以在启动时自动兜底。
     * <p>
     * 用低层 {@code xGroupCreate(..., mkStream=true)} 而不是 {@code opsForStream().createGroup()}，
     * 因为后者不带 MKSTREAM，流不存在时会直接失败。
     */
    private void ensureConsumerGroup() {
        try {
            String result = stringRedisTemplate.execute((RedisCallback<String>) connection ->
                    connection.xGroupCreate(
                            QUEUE_NAME.getBytes(StandardCharsets.UTF_8),
                            GROUP_NAME,
                            ReadOffset.from("0"),
                            true));
            log.info("已创建消费组 {}/{}（MKSTREAM），返回={}", QUEUE_NAME, GROUP_NAME, result);
        } catch (Exception e) {
            String msg = String.valueOf(e.getMessage());
            if (msg.contains("BUSYGROUP")) {
                log.info("消费组 {}/{} 已存在，跳过创建", QUEUE_NAME, GROUP_NAME);
            } else {
                log.warn("创建消费组失败（需确认 Redis 版本 >= 5 且有权限）: {}", msg);
            }
        }
    }

    @PreDestroy
    private void shutdown() {
        running = false;
        if (consumerThread != null) {
            consumerThread.interrupt();
            log.info("秒杀订单消费者已停止");
        }
    }

    // ==================================================================
    // 消费端
    // ==================================================================

    private void consumeLoop() {
        long lastDrain = 0L;
        while (running && !Thread.currentThread().isInterrupted()) {
            try {
                List<MapRecord<String, Object, Object>> list = read(ReadOffset.lastConsumed(), 1, true);
                if (list == null || list.isEmpty()) {
                    lastDrain = maybeDrain(lastDrain);
                    continue;
                }
                process(list.get(0));
            } catch (Exception e) {
                // 读取本身失败（Redis 抖动等）：记日志后短睡，不能退出循环
                log.error("读取消息流异常", e);
                sleepQuietly(100);
            }
            lastDrain = maybeDrain(lastDrain);
        }
    }

    /** 按间隔触发 pending 扫描 */
    private long maybeDrain(long lastDrain) {
        long now = System.currentTimeMillis();
        if (now - lastDrain < DRAIN_INTERVAL_MS) {
            return lastDrain;
        }
        try {
            drainPending();
        } catch (Exception e) {
            log.error("扫描 pending 列表异常", e);
        }
        return now;
    }

    /** 处理单条消息：按业务结果决定 ACK / 留给 pending / 转死信 */
    private void process(MapRecord<String, Object, Object> record) {
        String recordId = record.getId().getValue();

        VoucherOrder order;
        try {
            order = BeanUtil.fillBeanWithMap(record.getValue(), new VoucherOrder(), true);
        } catch (Exception e) {
            log.error("消息反序列化失败，转死信 id={}", recordId, e);
            toDeadLetter(recordId, "反序列化失败: " + e.getMessage());
            return;
        }
        if (order.getUserId() == null || order.getVoucherId() == null) {
            log.error("消息字段缺失，转死信 id={} body={}", recordId, record.getValue());
            toDeadLetter(recordId, "字段缺失");
            return;
        }

        ConsumeResult result = handleOrder(order);
        log.info("订单消费结果={} orderId={} userId={} voucherId={}",
                result, order.getId(), order.getUserId(), order.getVoucherId());

        if (result.ackable()) {
            ack(recordId);
        } else if (result == ConsumeResult.DEAD) {
            toDeadLetter(recordId, "重试耗尽");
        } else {
            // 关键：不 ACK，留在 pending 列表，等下一轮扫描重投
            log.warn("订单处理失败，保留在 pending 等待重投 orderId={}", order.getId());
        }
    }

    /** 加锁 + 调用事务方法 */
    private ConsumeResult handleOrder(VoucherOrder order) {
        Long userId = order.getUserId();
        RLock lock = redissonClient.getLock("order:" + userId);
        boolean locked = false;
        try {
            // 有等待时间的 tryLock：拿不到锁返回 RETRY（原实现直接 return，消息随后被 ACK → 丢单）
            locked = lock.tryLock(LOCK_WAIT_SECONDS, TimeUnit.SECONDS);
            if (!locked) {
                log.warn("获取订单锁失败，稍后重试 userId={}", userId);
                return ConsumeResult.RETRY;
            }
            // 直接调用本类方法：事务由 txTemplate 显式开启，不依赖 AOP 代理
            return createOrder(order);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return ConsumeResult.RETRY;
        } catch (Exception e) {
            log.error("处理订单异常 orderId={}", order.getId(), e);
            return ConsumeResult.RETRY;
        } finally {
            if (locked) {
                try {
                    lock.unlock();
                } catch (Exception ignore) {
                    // 锁可能已因超时释放，忽略
                }
            }
        }
    }

    /**
     * 扫描 pending 列表：超次数的转死信，其余重投一次。
     * <p>初版是无上限 {@code while(true)}，一条永远失败的消息会让消费线程彻底卡死。</p>
     */
    private void drainPending() {
        PendingMessages pendings;
        try {
            pendings = stringRedisTemplate.opsForStream()
                    .pending(QUEUE_NAME, GROUP_NAME, Range.unbounded(), DRAIN_BATCH);
        } catch (Exception e) {
            // 消费组不存在时会抛异常，说明还没初始化，跳过即可
            log.debug("查询 pending 失败: {}", e.getMessage());
            return;
        }
        if (pendings == null || pendings.isEmpty()) {
            return;
        }

        // 1) 超过最大重试次数的，转死信并 ACK
        for (PendingMessage pm : pendings) {
            if (pm.getTotalDeliveryCount() > MAX_RETRY) {
                log.error("消息投递 {} 次仍未成功，转入死信 id={}", pm.getTotalDeliveryCount(), pm.getIdAsString());
                toDeadLetter(pm.getIdAsString(), "投递 " + pm.getTotalDeliveryCount() + " 次仍失败");
            }
        }

        // 2) 其余重投：同一轮内每条只处理一次，避免同一条毒消息在本轮反复重试
        Set<String> handledInThisRound = new HashSet<>();
        int guard = 0;
        while (running && guard++ < DRAIN_BATCH) {
            List<MapRecord<String, Object, Object>> list;
            try {
                list = read(ReadOffset.from("0"), DRAIN_BATCH, false);
            } catch (Exception e) {
                log.error("重投 pending 消息失败", e);
                break;
            }
            if (list == null || list.isEmpty()) {
                break;
            }
            boolean progressed = false;
            for (MapRecord<String, Object, Object> rec : list) {
                if (handledInThisRound.add(rec.getId().getValue())) {
                    progressed = true;
                    process(rec);
                }
            }
            if (!progressed) {
                break;
            }
        }
    }

    /** 把消息原文搬到死信队列，然后 ACK 原消息 */
    private void toDeadLetter(String recordId, String reason) {
        try {
            List<MapRecord<String, Object, Object>> origin =
                    stringRedisTemplate.opsForStream().range(QUEUE_NAME, Range.closed(recordId, recordId));
            Map<String, String> body = new LinkedHashMap<>();
            if (origin != null && !origin.isEmpty()) {
                origin.get(0).getValue().forEach((k, v) -> body.put(String.valueOf(k), String.valueOf(v)));
            }
            body.put("_originId", recordId);
            body.put("_reason", reason);
            body.put("_failedAt", String.valueOf(System.currentTimeMillis()));
            stringRedisTemplate.opsForStream().add(DLQ_NAME, body);
            ack(recordId);
            log.error("消息已转入死信队列 {}，originId={} reason={}", DLQ_NAME, recordId, reason);
        } catch (Exception e) {
            log.error("转死信失败 originId={}", recordId, e);
        }
    }

    private void ack(String recordId) {
        try {
            stringRedisTemplate.opsForStream().acknowledge(QUEUE_NAME, GROUP_NAME, recordId);
        } catch (Exception e) {
            log.warn("ACK 失败 recordId={}", recordId, e);
        }
    }

    private List<MapRecord<String, Object, Object>> read(ReadOffset offset, int count, boolean block) {
        StreamReadOptions options = StreamReadOptions.empty().count(count);
        if (block) {
            options = options.block(Duration.ofSeconds(2));
        }
        return stringRedisTemplate.opsForStream().read(
                Consumer.from(GROUP_NAME, consumerName),
                options,
                StreamOffset.create(QUEUE_NAME, offset));
    }

    private static void sleepQuietly(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    // ==================================================================
    // 生产端
    // ==================================================================

    @Override
    public Result seckillVoucher(Long voucherId) {
        // 当前用户（这里是 HTTP 线程，UserHolder 有值）
        Long userId = UserHolder.getUser().getId();
        // 获取订单ID
        Long orderId = redisIdWorker.nextId("order");

        // 执行 lua：原子完成 有效期校验 + 库存判断 + 一人一单判断 + 扣减 + XADD 入队
        // 参数约定：KEYS[1]=voucherId，ARGV[1]=userId，ARGV[2]=orderId
        // ⚠️ 这里曾经写成 (userId, orderId, voucherId) 而 lua 里用 ARGV[1] 当 voucherId，
        //    顺序错位导致脚本去查 seckill:stock:{userId}，秒杀恒返回「库存不足」
        Long result;
        try {
            result = stringRedisTemplate.execute(
                    SECKILL_SCRIPT,
                    Collections.singletonList(voucherId.toString()),
                    userId.toString(), orderId.toString());
        } catch (Exception e) {
            log.error("执行秒杀脚本失败，voucherId={}", voucherId, e);
            return Result.fail("秒杀失败，请稍后重试");
        }

        // 防御：脚本没返回值时不要直接 result != 0（Long 拆箱会 NPE）
        if (result == null) {
            log.error("秒杀脚本返回 null，voucherId={}", voucherId);
            return Result.fail("秒杀失败，请稍后重试");
        }

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

    // ==================================================================
    // 落库（由消费端调用，@Transactional 通过接口代理生效）
    // ==================================================================

    @Override
    public ConsumeResult createOrder(VoucherOrder voucherOrder) {
        ConsumeResult result = txTemplate.execute(status -> doCreateOrder(voucherOrder, status));
        return result == null ? ConsumeResult.RETRY : result;
    }

    /** 真正的事务逻辑。用 TransactionStatus 显式控制回滚，替代原来的 @Transactional 注解。 */
    private ConsumeResult doCreateOrder(VoucherOrder voucherOrder, TransactionStatus status) {
        Long userId = voucherOrder.getUserId();
        Long voucherId = voucherOrder.getVoucherId();

        // 1. 先 insert：以 (user_id, voucher_id) 唯一索引作为一人一单的权威判据。
        //    原实现是「先 count 再 insert」，两步之间存在竞态；
        //    而且要保证「先占位再扣库存」——反过来会导致重复下单白扣一件库存（少卖）。
        try {
            save(voucherOrder);
        } catch (DuplicateKeyException e) {
            log.warn("唯一索引拦截重复下单 userId={} voucherId={}", userId, voucherId);
            return ConsumeResult.DUPLICATE;
        }

        // 2. 扣库存（乐观锁兜底：stock > 0 才扣）
        boolean success = seckillVoucherService.update()
                .setSql("stock = stock - 1")
                .eq("voucher_id", voucherId)
                .gt("stock", 0)
                .update();
        if (!success) {
            // Redis 判断有库存但 DB 扣不动 → 两边不一致，必须告警人工核对。
            // 标记回滚，把上面那条 insert 一起撤销，避免「有订单没扣库存」。
            log.error("DB 扣减库存失败，Redis 与 DB 可能不一致！voucherId={} orderId={}",
                    voucherId, voucherOrder.getId());
            status.setRollbackOnly();
            return ConsumeResult.STOCK_OUT;
        }

        log.info("订单落库成功 orderId={} userId={} voucherId={}", voucherOrder.getId(), userId, voucherId);
        return ConsumeResult.SUCCESS;
    }
}
