package com.hmdp;

import org.springframework.boot.SpringApplication;

/**
 * 第二个启动类：端口 8082
 *
 * <p>用途：和 {@link HmDianPingApplication}（8081）同时启动，模拟"集群 / 多实例"。
 * 秒杀章节用它来验证：
 * <pre>
 *   synchronized (userId.toString().intern()) 只能锁住【同一个 JVM】里的线程，
 *   两个实例各持一把锁 → 一人一单依然会被击穿 → 所以需要"分布式锁"。
 * </pre>
 *
 * <p>注意事项：
 * <ul>
 *   <li>两个实例必须共用同一个 Redis 和同一个 MySQL（这是验证分布式问题的前提）</li>
 *   <li>不要把本类当成独立的 Spring Boot 应用另加 @SpringBootApplication，
 *       否则会出现两套组件扫描配置，容易踩坑</li>
 *   <li>真正启动时复用 {@link HmDianPingApplication} 的配置，只覆盖 server.port</li>
 * </ul>
 */
public class HmDianPingApplication8082 {

    public static void main(String[] args) {
        // 把 --server.port=8082 拼到参数最前面
        // 命令行参数优先级最高，能可靠覆盖 application.yaml 里的 8081
        String[] mergedArgs = new String[args.length + 1];
        mergedArgs[0] = "--server.port=8082";
        System.arraycopy(args, 0, mergedArgs, 1, args.length);

        SpringApplication.run(HmDianPingApplication.class, mergedArgs);
    }
}
