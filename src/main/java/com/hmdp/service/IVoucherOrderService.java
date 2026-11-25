package com.hmdp.service;

import com.hmdp.dto.Result;
import com.hmdp.entity.VoucherOrder;
import com.baomidou.mybatisplus.extension.service.IService;
import com.hmdp.enums.ConsumeResult;

/**
 * <p>
 *  服务类
 * </p>
 *
 * @author 虎哥
 * @since 2021-12-22
 */
public interface IVoucherOrderService extends IService<VoucherOrder> {

    /**
     * 秒杀优惠券（生产端）：Redis + Lua 原子判断资格，成功后立即返回订单号
     * @param voucherId 优惠券 id
     * @return 订单号
     */
    Result seckillVoucher(Long voucherId);

    /**
     * 订单落库（消费端）。
     * <p>
     * 返回 {@link ConsumeResult} 而不是 {@code void}，是为了让调用方区分
     * 「业务已终结」和「临时故障需重试」——原实现返回 void，
     * 导致拿不到锁、DB 异常这类临时故障也被 ACK，消息就此永久丢失。
     *
     * @param voucherOrder 从消息队列解析出的订单
     * @return 消费结果
     */
    ConsumeResult createOrder(VoucherOrder voucherOrder);
}
