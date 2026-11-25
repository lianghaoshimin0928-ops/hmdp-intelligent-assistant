package com.hmdp.service.impl;

import cn.hutool.core.bean.BeanUtil;
import cn.hutool.core.util.BooleanUtil;
import cn.hutool.core.util.StrUtil;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.hmdp.dto.Result;
import com.hmdp.dto.UserDTO;
import com.hmdp.entity.Blog;
import com.hmdp.entity.User;
import com.hmdp.mapper.BlogMapper;
import com.hmdp.service.IBlogService;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.hmdp.service.IUserService;
import com.hmdp.utils.SystemConstants;
import com.hmdp.utils.UserHolder;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import javax.annotation.Resource;
import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * <p>
 *  服务实现类
 * </p>
 *
 * @author 虎哥
 * @since 2021-12-22
 */
@Service
public class BlogServiceImpl extends ServiceImpl<BlogMapper, Blog> implements IBlogService {

    @Resource
    private IUserService userService;
    @Resource
    private IBlogService blogService;

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    @Override
    public Result queryBlogById(Long id) {
        // 根据id查询blog
        Blog blog = getById(id);

        //通过blog查询用户id

        queryBlogUser(blog);
        isLikedBlog(blog);
        return Result.ok(blog);

    }

    private void queryBlogUser(Blog blog) {
        Long userId = blog.getUserId();
        User user = userService.getById(userId);
        //保存用户信息和blog信息
        blog.setName(user.getNickName());
        blog.setIcon(user.getIcon());
    }

    @Override
    public Result queryHotBlog(Integer current) {
        // 根据用户查询
        Page<Blog> page = blogService.query()
                .orderByDesc("liked")
                .page(new Page<>(current, SystemConstants.MAX_PAGE_SIZE));
        // 获取当前页数据
        List<Blog> records = page.getRecords();
        // 查询用户
        records.forEach(blog ->{
            this.queryBlogUser(blog);
            this.isLikedBlog(blog);

        });
        return Result.ok(records);
    }

    @Override
    public Result likeBlog(Long id) {
        // 获取登录用户
        Long userId = UserHolder.getUser().getId();
        String key = "blog:like:" + id;
        Boolean isLiked = stringRedisTemplate.opsForZSet().score(key, userId.toString()) != null;

        //在redis中查询用户是否点赞

        if (BooleanUtil.isFalse(isLiked)) {
            //先更新数据库，点赞数+1
            boolean success = update().setSql("liked = liked + 1").eq("id", id).update();
            if(success)
                stringRedisTemplate.opsForZSet().add(key, userId.toString(), System.currentTimeMillis());

        }
        //如果没点赞，数据库+1，redis中记录
        else{
            boolean success = update().setSql("liked = liked - 1").eq("id", id).update();
            if(success)
                stringRedisTemplate.opsForZSet().remove(key, userId.toString());
        }
        //返回结果
        return Result.ok();
    }

    //判断用户是否点赞
    private void isLikedBlog(Blog blog) {
        // 获取当前登录用户
        UserDTO user = UserHolder.getUser();
        if (user == null) {
            return;
        }
        // 获取登录用户
        Long userId = UserHolder.getUser().getId();
        String key = "blog:like:" + blog.getId();
        Boolean isLiked = stringRedisTemplate.opsForZSet().score(key, userId.toString()) != null;
        blog.setIsLike(BooleanUtil.isTrue(isLiked));
    }

    @Override
    public Result queryBlogLikes(Long id) {
        String key = "blog:like:" + id;
        //获取点赞前五的用户
        Set<String> top5 = stringRedisTemplate.opsForZSet().range(key, 0, 4);
        //解析用户
        List<Long> ids = top5.stream().map(Long::valueOf).collect(Collectors.toList());
        String idstr = StrUtil.join(",", ids);
        List<UserDTO> userDTOs = userService.query()
                .in("id", ids)
                .last("order by field(id," + idstr + ")").list()
                .stream()
                .map(user -> BeanUtil.copyProperties(user, UserDTO.class))
                .collect(Collectors.toList());
        return Result.ok(userDTOs);
        //返回userDTO


    }

}
