package com.hmdp.service.impl;

import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.hmdp.dto.Result;
import com.hmdp.entity.Voucher;
import com.hmdp.mapper.VoucherMapper;
import com.hmdp.entity.SeckillVoucher;
import com.hmdp.service.ISeckillVoucherService;
import com.hmdp.service.IVoucherService;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import javax.annotation.Resource;
import java.time.ZoneId;
import java.util.List;

/**
 * <p>
 *  服务实现类
 * </p>
 *
 * @author 虎哥
 * @since 2021-12-22
 */
@Service
public class VoucherServiceImpl extends ServiceImpl<VoucherMapper, Voucher> implements IVoucherService {

    @Resource
    private ISeckillVoucherService seckillVoucherService;

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    @Override
    public Result queryVoucherOfShop(Long shopId) {
        // 查询优惠券信息
        List<Voucher> vouchers = getBaseMapper().queryVoucherOfShop(shopId);
        // 返回结果
        return Result.ok(vouchers);
    }

    @Override
    @Transactional
    public void addSeckillVoucher(Voucher voucher) {
        // 保存优惠券
        save(voucher);
        // 保存秒杀信息
        SeckillVoucher seckillVoucher = new SeckillVoucher();
        seckillVoucher.setVoucherId(voucher.getId());
        seckillVoucher.setStock(voucher.getStock());
        seckillVoucher.setBeginTime(voucher.getBeginTime());
        seckillVoucher.setEndTime(voucher.getEndTime());
        seckillVoucherService.save(seckillVoucher);

        // ===== 预热到 Redis =====
        // 坑：key 必须和 seckill.lua 里拼的完全一致！
        // 原来写成 "seckill:stock" + id = seckill:stock12（少了冒号），
        // 而 lua 里读的是 seckill:stock:12 → 读不到 → nil 比较 → 脚本报错 → 接口 500
        stringRedisTemplate.opsForValue()
                .set("seckill:stock:" + voucher.getId(), voucher.getStock().toString());

        // 预热有效期（epoch 秒），供 lua 做时间校验，避免未开始/已结束的券还能抢
        if (voucher.getBeginTime() != null) {
            stringRedisTemplate.opsForValue().set("seckill:begin:" + voucher.getId(),
                    String.valueOf(voucher.getBeginTime().atZone(ZoneId.systemDefault()).toEpochSecond()));
        }
        if (voucher.getEndTime() != null) {
            stringRedisTemplate.opsForValue().set("seckill:end:" + voucher.getId(),
                    String.valueOf(voucher.getEndTime().atZone(ZoneId.systemDefault()).toEpochSecond()));
        }
    }
}
