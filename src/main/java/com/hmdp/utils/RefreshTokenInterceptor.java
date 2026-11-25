package com.hmdp.utils;

import cn.hutool.core.bean.BeanUtil;
import com.hmdp.dto.UserDTO;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.web.servlet.HandlerInterceptor;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import java.util.Map;
import java.util.concurrent.TimeUnit;

public class RefreshTokenInterceptor implements HandlerInterceptor {


    //调用redistemplate
    private StringRedisTemplate redisTemplate;

    public RefreshTokenInterceptor(StringRedisTemplate redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    //前置处理
    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) throws Exception {
        //1.获取请求头中的token
        String token = request.getHeader("Authorization");
        //1.1 不存在，返回401状态码
        if (token == null) {
            return true;
        }
        Map<Object, Object> userMap = redisTemplate.opsForHash().entries(RedisConstants.LOGIN_USER_KEY + token);

        //3.判断用户是否存在登录
        if (userMap == null || userMap.isEmpty()) {

            return true;
        }
        //4.将查询到的用户信息转为userDTO
        UserDTO userDTO = BeanUtil.fillBeanWithMap(userMap, new UserDTO(),false);

        //5.存在，保存到threadlocal
        UserHolder.saveUser(userDTO);

        //6.刷新token有效期
        redisTemplate.expire(RedisConstants.LOGIN_USER_KEY + token, RedisConstants.LOGIN_USER_TTL, TimeUnit.SECONDS);

        return true;
    }

    //后置处理

    public void afterHandle(HttpServletRequest request, HttpServletResponse response, Object handler) throws Exception {
        //移除用户
        UserHolder.removeUser();
    }
}
