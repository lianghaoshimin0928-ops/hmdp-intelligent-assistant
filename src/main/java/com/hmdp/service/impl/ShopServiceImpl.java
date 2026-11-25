package com.hmdp.service.impl;

import cn.hutool.core.util.StrUtil;
import cn.hutool.json.JSONObject;
import cn.hutool.json.JSONUtil;
import com.hmdp.dto.Result;
import com.hmdp.entity.Shop;
import com.hmdp.mapper.ShopMapper;
import com.hmdp.service.IShopService;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.hmdp.utils.RedisData;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import javax.annotation.Resource;
import java.time.LocalDateTime;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

import static com.hmdp.utils.RedisConstants.CACHE_SHOP_TTL;

/**
 * <p>
 *  服务实现类
 * </p>
 *
 * @author 虎哥
 * @since 2021-12-22
 */
@Service
public class ShopServiceImpl extends ServiceImpl<ShopMapper, Shop> implements IShopService {

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    public Result queryById(Long id) {
       //互斥锁解决缓存击穿

        Shop shop = queryWithMutex(id);
        return Result.ok(shop);
    }

    @Transactional
    public Result update(Shop shop) {

        Long id = shop.getId();
        if (id == null) {
            return Result.fail("店铺id不能为空");
        }
        //更新数据库
        updateById(shop);
        //删除redis缓存
        stringRedisTemplate.delete("cache:shop:" + id);
        return Result.ok();
    }

    /**
     * 尝试获取锁
     * @param key
     * @return
     */
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

    private void saveShop2Redis(Long id, Long expireSeconds){
        Shop shop = getById(id);
        RedisData redisData = new RedisData();
        redisData.setData(shop);
        redisData.setExpireTime(LocalDateTime.now().plusSeconds(expireSeconds));
        stringRedisTemplate.opsForValue().set("cache:shop:" + id, JSONUtil.toJsonStr(redisData));
    }


    public Shop queryWithPassThrough(Long id) {
        String key = "cache:shop:" + id;
        String shopJson = stringRedisTemplate.opsForValue().get(key);
        //判断是否存在
        if (StrUtil.isNotBlank(shopJson)) {
            //存在，返回店铺
            return JSONUtil.toBean(shopJson, Shop.class);
        }
        //判断是否为空值
        if (shopJson != null) {
            return null;
        }
        //不存在，查询数据库
        Shop shop = getById(id);
        //不存在，返回错误
        if (shop == null) {
            stringRedisTemplate.opsForValue().set(key, "", CACHE_SHOP_TTL, TimeUnit.MINUTES);
            return null;
        }
        //存在，写入redis
        stringRedisTemplate.opsForValue().set(key, JSONUtil.toJsonStr(shop), CACHE_SHOP_TTL, TimeUnit.MINUTES);
        return shop;
    }

    //缓存穿透
    public Shop queryWithMutex(Long id) {
        //1.在redis中查询店铺
        String key = "cache:shop:" + id;
        String shopJson = stringRedisTemplate.opsForValue().get(key);
        //将json反序列化为对象
        Shop shop = JSONUtil.toBean(shopJson, Shop.class);
        //3.如果存在，返回店铺
        if(StrUtil.isNotBlank(shopJson)) {
            return shop;
        }

        try {
            //4.1如果不存在，尝试获取锁
            boolean isLocked = tryLock(key);
            //4.2如果获取锁失败，休眠并重试
            if (!isLocked) {
                Thread.sleep(50);
                return queryWithMutex(id);
            }


            //4.3如果获取锁成功,查询数据库
            shop = getById(id);
            //5.如果数据库不存在，返回错误
            if(shop == null) {
                return null;
            }
            //6.将店铺写入redis，释放锁
            stringRedisTemplate.opsForValue().set(key, JSONUtil.toJsonStr(shop), CACHE_SHOP_TTL, TimeUnit.MINUTES);
        } catch (InterruptedException e) {
            throw new RuntimeException(e);
        } finally {
            unlock(key);
        }
        return shop;

    }

    //线程池
    private static final ExecutorService CACHE_REBUILD_EXECUTOR = Executors.newFixedThreadPool(10);
    /**
     * 逻辑过期缓存
     * @param id
     * @return
     */
    private Shop queryWithLogicalExpire(Long id) {

        //1.在redis中查询店铺
        String key = "cache:shop:" + id;
        String shopJson = stringRedisTemplate.opsForValue().get(key);
        //2.判断是否存在
        if (StrUtil.isBlank(shopJson)) {
            return null;
        }

        //2.1如果存在，判断是否过期
        RedisData redisData = JSONUtil.toBean(shopJson, RedisData.class);
        LocalDateTime expireTime = redisData.getExpireTime();
        //将数据反序列化
        Shop shop = JSONUtil.toBean((JSONObject) (redisData.getData()), Shop.class);
        if (expireTime.isAfter(LocalDateTime.now())) {
            //2.1.1如果未过期，返回店铺
            return shop;
        }

        //4.如果过期，尝试获取锁
        boolean isLocked = tryLock(key);
        //5.1如果获取锁成功，开启独立线程实现缓存重建
        if (isLocked) {
            //5.1.1如果获取锁成功，开启独立线程实现缓存重建
            //用线程池执行缓存重建
            CACHE_REBUILD_EXECUTOR.submit(() -> {
                try {
                    //重建缓存
                    this.saveShop2Redis(id, 20L);
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
