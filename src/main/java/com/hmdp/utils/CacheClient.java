package com.hmdp.utils;

import cn.hutool.core.util.StrUtil;
import cn.hutool.json.JSONObject;
import cn.hutool.json.JSONUtil;
import com.hmdp.entity.Shop;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

import static com.hmdp.utils.RedisConstants.CACHE_SHOP_TTL;

@Slf4j
@Component
public class CacheClient {

    private final StringRedisTemplate stringRedisTemplate;
    public CacheClient(StringRedisTemplate stringRedisTemplate) {
        this.stringRedisTemplate = stringRedisTemplate;
    }

    public void set(String key, Object value, Long time, TimeUnit unit) {
        stringRedisTemplate.opsForValue().set(key, JSONUtil.toJsonStr(value), time, unit);
    }

    public void setWithLogicalExpire(String key, Object value, Long time, TimeUnit unit) {
        RedisData redisData = new RedisData();
        redisData.setData(value);
        redisData.setExpireTime(LocalDateTime.now().plusSeconds(unit.toSeconds(time)));
        stringRedisTemplate.opsForValue().set(key, JSONUtil.toJsonStr(redisData), time, unit);
    }

    private boolean tryLock(String key) {

        return stringRedisTemplate.opsForValue().setIfAbsent(key, "1", 10, TimeUnit.SECONDS);

    }

    /**
     * 释放锁
     * @param key
     */
    private void unlock(String key) {
        stringRedisTemplate.delete(key);
    }



    public <R, ID> R getCacheWithPassThrough(String key, ID id, Class<R> type, Function<ID, R> dbFallback) {
        String cacheKey = "cache:shop:" + id;
        String shopJson = stringRedisTemplate.opsForValue().get(key);
        //判断是否存在
        if (StrUtil.isNotBlank(shopJson)) {
            //存在，返回店铺
            return JSONUtil.toBean(shopJson, type);
        }
        //判断是否为空值
        if (shopJson != null) {
            return null;
        }
        //不存在，查询数据库
        R shop = dbFallback.apply(id);
        //不存在，返回错误
        if (shop == null) {
            stringRedisTemplate.opsForValue().set(key, "", CACHE_SHOP_TTL, TimeUnit.MINUTES);
            return null;
        }
        //存在，写入redis
        stringRedisTemplate.opsForValue().set(key, JSONUtil.toJsonStr(shop), CACHE_SHOP_TTL, TimeUnit.MINUTES);
        return shop;
    }

    private static final ExecutorService CACHE_REBUILD_EXECUTOR = Executors.newFixedThreadPool(10);
    public <R, ID> R getCacheWithLogicalExpire(String key, ID id, Class<R> type, Function<ID, R> dbFallback) {


            //1.在redis中查询店铺
            String cacheKey = "cache:shop:" + id;
            String shopJson = stringRedisTemplate.opsForValue().get(cacheKey);
            //2.判断是否存在
            if (StrUtil.isBlank(shopJson)) {
                return null;
            }

            //2.1如果存在，判断是否过期
            RedisData redisData = JSONUtil.toBean(shopJson, RedisData.class);
            LocalDateTime expireTime = redisData.getExpireTime();
            //将数据反序列化
            R shop = JSONUtil.toBean((JSONObject) (redisData.getData()), type);
            if (expireTime.isAfter(LocalDateTime.now())) {
                //2.1.1如果未过期，返回店铺
                return shop;
            }

            //4.如果过期，尝试获取锁
            boolean isLocked = tryLock(cacheKey);
            //5.1如果获取锁成功，开启独立线程实现缓存重建
            if (isLocked) {
                //5.1.1如果获取锁成功，开启独立线程实现缓存重建
                //用线程池执行缓存重建
                CACHE_REBUILD_EXECUTOR.submit(() -> {
                    try {
                        //重建缓存
                        //查询数据库
                        R newShop = dbFallback.apply(id);
                       this.setWithLogicalExpire(cacheKey, newShop, CACHE_SHOP_TTL, TimeUnit.MINUTES);
                    } catch (Exception e) {
                        throw new RuntimeException(e);
                    } finally {
                        //释放锁
                        unlock(key);
                    }
                });
                return null;
            }
            //5.2 如果获取锁失败，返回过期的店铺
            return shop;
    }


}
