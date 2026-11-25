--- 秒杀资格判断（在 Redis 中原子执行，杜绝超卖）
--- KEYS[1] = voucherId
--- ARGV[1] = userId
--- 返回值：0=有资格  1=库存不足  2=重复下单  3=不在有效期内
---
--- 坑1：Redis 里不存在的 key，redis.call('get') 返回的是 false（不是 nil），
---      必须先 tonumber 再判空，否则比较会报 "attempt to compare nil with number"
--- 坑2：脚本末尾必须有 return，否则 Spring 收到 null，result != 0 拆箱时 NPE

--- Redis 3.2 的坑：脚本里一旦调用了 TIME 这类"非确定性命令"，
--- 后面就不允许再执行写命令，会报
---   Write commands not allowed after non deterministic commands
--- 必须在脚本开头调用 replicate_commands() 切到单命令复制模式（Redis 3.2+ 支持）
if redis.replicate_commands then
    redis.replicate_commands()
end

local voucherId = ARGV[1]
local userId    = ARGV[2]
local orderid   = ARGV[3]

local stockkey = 'seckill:stock:' .. voucherId
local orderkey = 'seckill:order:' .. voucherId
local beginkey = 'seckill:begin:' .. voucherId
local endkey   = 'seckill:end:'   .. voucherId

--- 0. 有效期校验（begin/end 由 VoucherServiceImpl.addSeckillVoucher 预热成 epoch 秒）
---    没有预热过就跳过，兼容老数据
local now = tonumber(redis.call('TIME')[1])
local beginTime = tonumber(redis.call('get', beginkey))
local endTime   = tonumber(redis.call('get', endkey))
if beginTime ~= nil and now < beginTime then
    return 3;
end
if endTime ~= nil and now > endTime then
    return 3;
end

--- 1. 判断库存是否大于0
local stock = tonumber(redis.call('get', stockkey))
if stock == nil or stock <= 0 then
    return 1;
end

--- 2. 判断用户是否已经购买过（Set，O(1)）
if redis.call('sismember', orderkey, userId) == 1 then
    return 2;
end

--- 3. 库存减一
redis.call('incrby', stockkey, -1);

--- 4. 保存订单信息（记录该用户已下单）
redis.call('sadd', orderkey, userId);

---发送信息到队列中，XADD
redis.call('xadd', 'stream.orders', '*', 'userId', userId, 'voucherId', voucherId, 'id', orderid);



--- 5. 成功必须有返回值！
return 0;
