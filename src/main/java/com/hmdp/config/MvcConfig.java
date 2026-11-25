package com.hmdp.config;

import com.hmdp.utils.LoginInterceptor;
import com.hmdp.utils.RefreshTokenInterceptor;
import org.springframework.context.annotation.Configuration;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

import javax.annotation.Resource;

@Configuration
public class MvcConfig implements WebMvcConfigurer {

    @Resource
    private StringRedisTemplate stringRedisTemplate;
    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        // ① 先执行：拦截所有请求，从 token 解析用户放进 UserHolder，并刷新 token 有效期
        registry.addInterceptor(new RefreshTokenInterceptor(stringRedisTemplate))
               .addPathPatterns("/**");

        // ② 后执行：只校验"需要登录"的接口（白名单里的放行）
        registry.addInterceptor(new LoginInterceptor())
               .excludePathPatterns(
                       "/user/login",
                       "/user/code",
                       "/blog/hot",
                       "/shop/**",
                       "/shop-type/**",
                       "/upload/**",
                       "/voucher/**"
               );
    }
}
