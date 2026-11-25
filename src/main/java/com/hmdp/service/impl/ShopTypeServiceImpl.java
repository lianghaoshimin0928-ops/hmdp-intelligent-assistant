package com.hmdp.service.impl;

import cn.hutool.core.util.StrUtil;
import cn.hutool.json.JSONUtil;
import com.hmdp.dto.Result;
import com.hmdp.entity.ShopType;
import com.hmdp.mapper.ShopTypeMapper;
import com.hmdp.service.IShopTypeService;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import javax.annotation.Resource;
import java.util.List;

/**
 * <p>
 *  服务实现类
 * </p>
 *
 * @author 虎哥
 * @since 2021-12-22
 */
@Service
public class ShopTypeServiceImpl extends ServiceImpl<ShopTypeMapper, ShopType> implements IShopTypeService {
   @Resource
   private StringRedisTemplate stringRedisTemplate;

   @Resource
   private IShopTypeService shopTypeService;

    @Override
    public Result sortlist() {

        String key ="shop:list";
        //在redis中查询店铺
        String shopTypes = stringRedisTemplate.opsForValue().get(key);

        //如果存在，返回店铺类型
        if (StrUtil.isNotBlank(shopTypes)) {
            List<ShopType> shopTypeList = JSONUtil.toList(shopTypes, ShopType.class);
            return Result.ok(shopTypeList);
        }
        //如果不存在，查询数据库
        List<ShopType> shopTypeList = shopTypeService.list();
        //如果数据库不存在，返回错误
        if (shopTypeList == null) {
            return Result.fail("店铺类型不存在");
        }
        stringRedisTemplate.opsForValue().set(key, JSONUtil.toJsonStr(shopTypeList));
        return Result.ok(shopTypeList);
    }

}
