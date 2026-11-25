package com.hmdp.config;

import org.redisson.Redisson;
import org.redisson.api.RedissonClient;
import org.redisson.config.Config;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * Redisson 配置：创建 RedissonClient，用于分布式锁。
 *
 * <p>注意三点：
 * <ol>
 *   <li>地址必须是<b>你自己的 Redis</b>（127.0.0.1:6379），
 *       不是课程视频里的 192.168.150.101</li>
 *   <li>密码要和 application.yaml 的 spring.redis.password 一致（123456）</li>
 *   <li>database 要和 application.yaml 的 spring.redis.database 一致（1），
 *       否则锁会写到别的库，排查时在 GUI 里看不到</li>
 * </ol>
 */
@Configuration
public class RedissonConfig {

    @Bean
    public RedissonClient redissonClient() {
        // 1. 创建配置对象
        Config config = new Config();

        // 2. 单机模式：地址 + 密码 + 库号
        config.useSingleServer()
                .setAddress("redis://127.0.0.1:6379")
                .setPassword("123456")
                .setDatabase(1);

        // 3. 创建客户端（注意：Redisson.create 会立即连接 Redis，Redis 没启动会启动失败）
        return Redisson.create(config);
    }
}
