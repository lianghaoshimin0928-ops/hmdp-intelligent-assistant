package com.hmdp.utils;

import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.stereotype.Component;

import javax.annotation.Resource;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;

@Component
public class RedisIdWorker {

    @Resource
    private RedisTemplate<String, String> redisTemplate;



    public static final Long START_STAMP = 1672531200L;

    public Long nextId(String key) {


        //1.生成时间戳
        LocalDateTime now = LocalDateTime.now();
        long nowSecond = now.toEpochSecond(ZoneOffset.UTC);
        long timestamp = nowSecond - START_STAMP;
        if (timestamp < 0) {
            throw new RuntimeException("时间戳错误");
        }


        //2.生成序列号
        //2.1用Redis自增
        String data = now.format(DateTimeFormatter.ofPattern("yyyyMMdd"));
        long sequence = redisTemplate.opsForValue().increment("icr" + key+":"+data);
        if (sequence < 0) {
            throw new RuntimeException("序列号错误");
        }

        return timestamp << 32 | sequence;
    }
}
