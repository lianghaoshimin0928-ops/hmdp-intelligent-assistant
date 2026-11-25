# 压测记录

被测接口：`GET /shop/{id}`（随机 id 1–14，覆盖全部真实商户）
工具：Apache JMeter 5.6.3，**non-GUI 模式**（避免 JMeter 自身渲染开销污染数据）

## 环境

| 项 | 配置 |
|---|---|
| 压测机 / 应用机 | 同一台：Intel i5-1240P（12 核 16 线程）、15.7 GB、Windows 11 |
| 应用 | Spring Boot 2.3.12 + JDK 8 + 内嵌 Tomcat（默认 maxThreads=200） |
| Redis | **本机** 127.0.0.1:6379，5.0.14.1，关闭 RDB/AOF |
| MySQL | **远程虚拟机** 192.168.188.128:3306，8.0.26 |
| 数据量 | 14 家商户、1009 用户、2 张券、4 篇笔记 |
| 参数 | 100 / 200 并发，爬坡 10s，持续 45s |

> 局限：Redis 与应用**同机部署**，省掉一次网络往返，因此「缓存命中」的数字偏乐观；
> JMeter 与被测应用同机，压测工具本身会抢 CPU。

## 结果

| 场景 | 并发 | 样本数 | QPS | avg | p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|
| 修复前（缓存从未命中） | 100 | 5,349 | **121** | 748.8 ms | 762 ms | 820 ms | 3,243 ms |
| 修复后（缓存命中） | 100 | 256,323 | **5,759** | 15.4 ms | 14 ms | 26 ms | 37 ms |
| 修复前 | 200 | 5,418 | **122** | 1,495.4 ms | 1,610 ms | 1,739 ms | 5,953 ms |
| 修复后 | 200 | 253,686 | **5,715** | 31.1 ms | 31 ms | 46 ms | 58 ms |

### 串行瓶颈的证据

修复前并发从 100 加到 200，**QPS 几乎不动**（121 → 122），但延迟线性翻倍（748 → 1,495 ms）。
用 Little's Law 验证 `并发 = 吞吐 × 延迟`：

- 100 ≈ 121 × 0.749 ✓
- 200 ≈ 122 × 1.495 ✓

QPS 恒定 + 延迟随并发线性增长 = 典型串行瓶颈（所有线程抢同一把锁，其余在 `sleep(50)` 空转）。

### 并发正确性

并发 20、共 100 次请求，校验**业务返回体**（不是只看 HTTP 状态码）：

| 版本 | 正常返回 | 业务失败 (`success:false`) |
|---|---|---|
| 修复前 | 83 / 100 | **17 / 100** |
| 修复后 | 100 / 100 | 0 |

HTTP 状态码全部是 200，JMeter 报错率 0% —— 说明压测必须校验业务返回体。

## 缺陷根因

`ShopServiceImpl.queryWithMutex()` 中，互斥锁的 key 与缓存 key 复用了同一个 `cache:shop:{id}`：

```java
String key = "cache:shop:" + id;
try {
    boolean isLocked = tryLock(key);       // SETNX cache:shop:{id} = "1"  ← 写进缓存 key
    if (!isLocked) { Thread.sleep(50); return queryWithMutex(id); }
    shop = getById(id);
    stringRedisTemplate.opsForValue().set(key, JSONUtil.toJsonStr(shop), 30, MINUTES);
} finally {
    unlock(key);                           // delete(key) ← 把刚写的缓存删了
}
```

三个连锁后果：

1. 缓存写完立刻被删 → 下次请求必然穿透到 DB；
2. 锁标记 `"1"` 被其他线程当成缓存数据，`JSONUtil.toBean("1", Shop.class)` 抛异常 → 业务失败；
3. 吞吐被持锁线程串行化，其余线程在 `Thread.sleep(50)` 空转。

## 修复

锁 key 与缓存 key 分离：

```diff
-  boolean isLocked = tryLock(key);
+  boolean isLocked = tryLock("lock:shop:" + id);
   ...
   } finally {
-      unlock(key);
+      unlock("lock:shop:" + id);
   }
```

验证：`cache:shop:1` 的 `EXISTS = 1`、`TTL = 1800`，并发 100 次业务成功率 100%。

## 复现

```bash
jmeter -n -t hmdp-bench.jmx -l results/run.jtl -e -o report \
       -Jhost=127.0.0.1 -Jport=8081 -Jthreads=100 -Jduration=45
```

参数可用 `-J` 覆盖：`host` / `port` / `threads` / `rampup` / `duration` / `token`。

`results/` 下保留了两份原始 JTL（修复前，100 / 200 并发）。修复后样本量 25 万、单份 28 MB，未入库，指标见上表。
