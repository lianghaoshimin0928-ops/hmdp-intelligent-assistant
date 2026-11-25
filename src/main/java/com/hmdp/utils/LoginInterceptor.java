package com.hmdp.utils;

import com.hmdp.dto.UserDTO;
import org.springframework.web.servlet.HandlerInterceptor;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

public class LoginInterceptor implements HandlerInterceptor {

    //前置处理
    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) throws Exception {
       //判断用户是否存在
        UserDTO userDTO = UserHolder.getUser();
        if(userDTO == null){
            response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
            return false;
        }
        return true;
    }

    //后置处理

    public void afterHandle(HttpServletRequest request, HttpServletResponse response, Object handler) throws Exception {
        //移除用户
        UserHolder.removeUser();
    }
}
