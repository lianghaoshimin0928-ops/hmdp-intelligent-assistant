"""把 Java 后端的能力包装成 LangChain 工具。

面试要点：
1. docstring 就是给模型看的「工具说明书」，写得越具体，模型选错工具的概率越低；
2. 工具**永不抛异常**，失败也返回一句话，让模型自己决定重试 / 降级 / 如实告知；
3. 返回值必须截断，否则几条几十 KB 的 JSON 就把上下文窗口撑爆了。
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from app.clients.hmdp import HmdpClient, HmdpResult
from app.clients import hmdp as hmdp_module


def _client() -> HmdpClient:
    """延迟取全局 client，便于测试时 monkeypatch。"""
    return hmdp_module.client


def _obs(result: HmdpResult) -> str:
    return result.to_observation()


@tool
def query_shop(keyword: str) -> str:
    """按关键词搜索本地商户，返回商户列表（含 id、名称、人均、地址、评分）。

    用户想找某类店时调用，例如「火锅」「烤肉」「KTV」「美发」。
    拿到返回的 id 后，可以再用 get_shop_detail 或 list_vouchers 查详情。

    Args:
        keyword: 搜索关键词，只传店名或品类词，不要带「帮我找」这类修饰语
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return "调用失败：keyword 不能为空"
    result = _client().list_shops_by_name(keyword, current=1)
    if result.ok and isinstance(result.data, list):
        # 只给模型前 3 条，避免上下文膨胀
        result.data = result.data[:3]
    return _obs(result)


@tool
def get_shop_detail(shop_id: int) -> str:
    """根据商户 id 查询商户详情（地址、人均、评分、营业时间、图片）。

    Args:
        shop_id: 商户 id，必须是从 query_shop 结果里拿到的真实 id
    """
    if not shop_id or int(shop_id) <= 0:
        return "调用失败：shop_id 非法"
    return _obs(_client().get_shop(int(shop_id)))


@tool
def list_vouchers(shop_id: int) -> str:
    """查询某个商户当前可售的优惠券列表（面额、门槛、库存、有效期）。

    Args:
        shop_id: 商户 id，必须是从 query_shop 结果里拿到的真实 id
    """
    if not shop_id or int(shop_id) <= 0:
        return "调用失败：shop_id 非法"
    result = _client().list_vouchers(int(shop_id))
    if result.ok and isinstance(result.data, list):
        result.data = [
            {
                "id": v.get("id"),
                "title": v.get("title"),
                "payValue": v.get("payValue"),
                "actualValue": v.get("actualValue"),
                "stock": v.get("stock"),
                "beginTime": v.get("beginTime"),
                "endTime": v.get("endTime"),
            }
            for v in result.data[:5]
        ]
    return _obs(result)


@tool
def seckill_voucher(voucher_id: int) -> str:
    """抢购指定优惠券。**不可逆的写操作**，执行前必须先向用户确认。

    只有在用户明确说出「确认」「抢吧」「可以」之后才允许调用。

    Args:
        voucher_id: 优惠券 id，必须是从 list_vouchers 结果里拿到的真实 id
    """
    if not voucher_id or int(voucher_id) <= 0:
        return "调用失败：voucher_id 非法"
    return _obs(_client().seckill(int(voucher_id)))


ALL_TOOLS = [query_shop, get_shop_detail, list_vouchers, seckill_voucher]


def describe_tools() -> str:
    """给 Prompt 用的工具清单（也用于 /api/tools 调试接口）。"""
    lines = []
    for i, t in enumerate(ALL_TOOLS, 1):
        lines.append(f"{i}. {t.name}\n   说明：{(t.description or '').strip()}")
    return "\n".join(lines)


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)
