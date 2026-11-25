-- ============================================================
-- 秒杀订单：一人一单的最终屏障
--
-- 为什么需要它：
--   应用层用「Redis Set 占位 + Redisson 锁」防重复下单，但这依赖锁不出问题。
--   Redisson 锁在主从切换时可能失效（异步复制丢锁），此时两个请求都能通过应用层校验。
--   数据库唯一索引是不依赖任何中间件的最后一道防线。
--
--   而且引入唯一索引后，落库顺序可以改成「先 insert 占位，再扣库存」：
--   重复请求会在 insert 阶段就被拦住，不会白扣库存（原实现先 count 再 insert，
--   两步之间的竞态会导致少卖）。
-- ============================================================

-- 第一步：先查有没有重复数据。有重复必须先清理，否则加索引会失败。
SELECT user_id, voucher_id, COUNT(*) AS cnt
FROM tb_voucher_order
GROUP BY user_id, voucher_id
HAVING COUNT(*) > 1;

-- 第二步：加唯一索引
ALTER TABLE tb_voucher_order
    ADD UNIQUE KEY uk_user_voucher (user_id, voucher_id);

-- 第三步：验证
SHOW INDEX FROM tb_voucher_order WHERE Key_name = 'uk_user_voucher';

-- ============================================================
-- 死信队列说明
-- ============================================================
-- 消费失败超过 3 次的消息会被搬到 Redis Stream `stream.orders.dlq`，
-- 字段包括原始的 userId / voucherId / id，外加：
--   _originId  原消息在 stream.orders 里的 id
--   _reason    进死信的原因（反序列化失败 / 字段缺失 / 投递 N 次仍失败）
--   _failedAt  失败时间戳（毫秒）
--
-- 排查命令：
--   XLEN stream.orders.dlq
--   XRANGE stream.orders.dlq - + COUNT 10
--
-- 处理完并确认无需重试后，可以清理：
--   XDEL stream.orders.dlq <recordId>
