package com.hmdp.enums;

/**
 * 秒杀订单消费结果。
 * <p>
 * <b>为什么引入它</b>：原实现里 {@code handleVoucherOrder} 返回 void，外层消费者
 * <b>不管业务成功与否都会 acknowledge</b>。于是「拿不到分布式锁」「DB 超时」这类临时故障
 * 也被当成处理完成 —— 消息 ACK 后永久消失，而 Redis 里库存已扣、一人一单名额已占，
 * 数据库里却没有订单。用户既拿不到券，也无法再重试。
 * <p>
 * 现在把「业务结果」和「是否 ACK」解耦：
 * <ul>
 *   <li>{@link #SUCCESS} / {@link #DUPLICATE} / {@link #STOCK_OUT} / {@link #INVALID}
 *       —— 业务上已终结，可以 ACK</li>
 *   <li>{@link #RETRY} —— 临时故障，<b>不 ACK</b>，留在 pending 列表等重投</li>
 *   <li>{@link #DEAD} —— 重试次数耗尽，转死信队列后 ACK，避免毒消息卡死消费线程</li>
 * </ul>
 */
public enum ConsumeResult {

    /** 订单落库成功 */
    SUCCESS,

    /** 重复下单（一人一单已占位），业务拒绝，重试无意义 */
    DUPLICATE,

    /** DB 库存不足 —— 说明 Redis 与 DB 不一致，需告警人工核对 */
    STOCK_OUT,

    /** 消息内容非法（字段缺失 / 反序列化失败），重试无意义 */
    INVALID,

    /** 临时故障（拿不到锁、DB 超时、网络抖动），应当重试 */
    RETRY,

    /** 重试次数耗尽，转入死信队列 */
    DEAD;

    /** 业务是否已终结（决定能否 ACK） */
    public boolean ackable() {
        return this == SUCCESS || this == DUPLICATE || this == STOCK_OUT || this == INVALID;
    }
}
