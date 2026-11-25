package com.hmdp.service.impl;

import cn.hutool.core.bean.BeanUtil;
import cn.hutool.core.bean.copier.CopyOptions;
import cn.hutool.core.util.RandomUtil;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.hmdp.dto.LoginFormDTO;
import com.hmdp.dto.Result;
import com.hmdp.dto.UserDTO;
import com.hmdp.entity.User;
import com.hmdp.mapper.UserMapper;
import com.hmdp.service.IUserService;
import com.hmdp.utils.RegexPatterns;
import com.hmdp.utils.RegexUtils;
import org.springframework.beans.BeanUtils;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import javax.annotation.Resource;
import javax.servlet.http.HttpSession;
import java.util.HashMap;
import java.util.Map;
import java.util.Random;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

import static com.hmdp.utils.RedisConstants.LOGIN_CODE_KEY;
import static com.hmdp.utils.RedisConstants.LOGIN_CODE_TTL;
import static com.hmdp.utils.RedisConstants.LOGIN_USER_KEY;
import static com.hmdp.utils.RedisConstants.LOGIN_USER_TTL;
import static com.hmdp.utils.SystemConstants.USER_NICK_NAME_PREFIX;

/**
 * <p>
 * 服务实现类
 * </p>
 *
 * @author 虎哥
 * @since 2021-12-22
 */
@Service
public class UserServiceImpl extends ServiceImpl<UserMapper, User> implements IUserService {

    @Resource
    private StringRedisTemplate stringRedisTemplate;
    @Override
    public Result sendCode(String phone, HttpSession session) {
        //检验手机号格式
        if(RegexUtils.isPhoneInvalid(phone)){
            throw new IllegalArgumentException("手机号格式错误");
        }
        //生成验证码
        String code = RandomUtil.randomNumbers(6);

        //保存验证码到redis
        stringRedisTemplate.opsForValue().set(LOGIN_CODE_KEY + phone, code, LOGIN_CODE_TTL, TimeUnit.MINUTES);
        return Result.ok(code);
    }

    @Override
    public Result login(LoginFormDTO loginForm, HttpSession session) {

        String phone = loginForm.getPhone();
        //检验手机号格式

        if(RegexUtils.isPhoneInvalid(phone)){
            throw new IllegalArgumentException("手机号格式错误");
        }
        //检验验证码是否正确
        String code = stringRedisTemplate.opsForValue().get(LOGIN_CODE_KEY + phone);
        if (code == null || code.isEmpty()) {
            return Result.fail("验证码已过期，请重新获取");
        }
        if (!code.equals(loginForm.getCode())) {
            return Result.fail("验证码错误");
        }
        //验证码一次性使用，校验通过后立即删除
        stringRedisTemplate.delete(LOGIN_CODE_KEY + phone);

        //根据手机号查询用户
        User user = query().eq("phone", phone).one();
        //查询用户是否存在，不存在创建用户
        if(user == null){
            //创建用户，并把新用户赋回 user，否则下面 copyProperties 拿到的是 null
            user = createUser(phone);
        }

        //1.保存用户信息到redis

        //1.1 随机生成token，用uuid
        String token = UUID.randomUUID().toString();

        //1.2 将user信息保存到redis中
        UserDTO userDTO = BeanUtil.copyProperties(user, UserDTO.class);

        //用hashmap保存userDTO
        Map<String, Object> userDTOMap = BeanUtil.beanToMap(
                userDTO,
                new HashMap<>(),
                CopyOptions.create()
                        .setIgnoreNullValue(true) // 建议：忽略 null 值，避免 fieldValue.toString() 引发空指针
                        .setFieldValueEditor((fieldName, fieldValue) -> {
                            // 如果值为 null 则返回 null，否则返回其字符串形式
                            return fieldValue == null ? null : fieldValue.toString();
                        })
        );
        stringRedisTemplate.opsForHash().putAll("login:token:" + token, userDTOMap);
        //设置有效期
        stringRedisTemplate.expire(LOGIN_USER_KEY + token, LOGIN_USER_TTL, TimeUnit.SECONDS);
        return Result.ok(token);
    }

    //创建用户
    private User createUser(String phone){
        User user = new User();
        user.setPhone(phone);
        user.setNickName(USER_NICK_NAME_PREFIX + RandomUtil.randomString(6));
        save(user);
        return user;
    }
}
