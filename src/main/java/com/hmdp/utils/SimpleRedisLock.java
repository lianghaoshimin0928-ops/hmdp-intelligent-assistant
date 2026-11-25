package com.hmdp.utils;


import org.springframework.core.io.ClassPathResource;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;

import java.util.Collections;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

public class SimpleRedisLock implements ILock {


    private StringRedisTemplate stringRedisTemplate;
    private String name;
    private static final String ID_PREFIX = UUID.randomUUID().toString()+"-";

    private  static final String KEY_PREFIX = "lock:";

    private static final DefaultRedisScript<Long> UNLOCK_SCRIPT;
    static {
        //调用classpath
        UNLOCK_SCRIPT = new DefaultRedisScript<>();
        UNLOCK_SCRIPT.setLocation(new ClassPathResource("unlock.lua"));
        UNLOCK_SCRIPT.setResultType(Long.class);

    }

    public SimpleRedisLock(String name,StringRedisTemplate stringRedisTemplate) {
        this.stringRedisTemplate = stringRedisTemplate;
        this.name = name;
    }
    /**
     * 尝试获取锁
     * @return 是否获取成功
     */
    @Override
    public boolean tryLock(long timeoutSec) {
        //获取当前线程ID
        String threadId = ID_PREFIX + Thread.currentThread().getId();
        boolean flag = stringRedisTemplate.opsForValue().setIfAbsent(KEY_PREFIX + name, threadId + "", timeoutSec, TimeUnit.SECONDS);
        return Boolean.TRUE.equals(flag);
    }

    /**
     * 释放锁
     */
    @Override
    public void unlock() {
       //调用lua脚本
        stringRedisTemplate.execute
                (UNLOCK_SCRIPT,
                        Collections.singletonList(KEY_PREFIX + name),
                        ID_PREFIX + Thread.currentThread().getId());

    }
}
