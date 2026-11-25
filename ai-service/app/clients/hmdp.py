"""
调用 Java 业务后端（hmdp）的 HTTP 客户端。

设计要点（面试可讲）：
1. 只做 HTTP 转发，不碰数据库 —— 业务规则（秒杀扣减、一人一单、分布式锁）全部留在 Java 侧；
2. token 懒加载：首次调用时「发验证码 → 从 Redis 读验证码 → 登录 → 缓存 token」；
3. 所有异常归一化成一个 Result 对象，绝不把异常抛给 Agent —— 让模型自己决定重试/降级/如实告知用户；
4. 返回体截断，避免上下文窗口被撑爆。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
import redis as redis_lib

from app.config import settings

logger = logging.getLogger(__name__)

# 单次工具返回给模型的最大字符数
MAX_OBSERVATION_LEN = 1800


@dataclass
class HmdpResult:
    """统一的调用结果，永远不抛异常。"""

    ok: bool
    data: Any = None
    message: str = ""
    raw_status: int = 0

    def to_observation(self, limit: int = MAX_OBSERVATION_LEN) -> str:
        """转成给模型看的「观察结果」字符串。"""
        if not self.ok:
            return f"调用失败：{self.message}"
        import json

        text = json.dumps(self.data, ensure_ascii=False)
        if len(text) > limit:
            text = text[:limit] + " ...(已截断)"
        return text


@dataclass
class HmdpClient:
    base_url: str = field(default_factory=lambda: settings.hmdp_base_url.rstrip("/"))
    phone: str = field(default_factory=lambda: settings.hmdp_phone)
    timeout: float = 5.0

    _token: str | None = field(default=None, init=False)
    _http: httpx.Client = field(init=False)

    def __post_init__(self) -> None:
        self._http = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    # ---------------- 鉴权 ----------------

    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        if settings.hmdp_token:
            self._token = settings.hmdp_token
            return self._token

        phone = self.phone
        # 1) 触发 hmdp 发送验证码（会写入 Redis）
        try:
            self._http.post("/user/code", params={"phone": phone})
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送验证码失败: %s", exc)

        # 2) 和 Java 共用同一个 Redis，直接读出验证码
        code = None
        try:
            rds = redis_lib.from_url(settings.redis_url, decode_responses=True)
            code = rds.get(f"login:code:{phone}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取验证码失败: %s", exc)

        if not code:
            raise RuntimeError(
                f"无法获取验证码：Redis 中 login:code:{phone} 为空。"
                "请检查 Redis 连接、或直接配置 HMDP_TOKEN"
            )

        # 3) 登录换 token
        resp = self._http.post("/user/login", json={"phone": phone, "code": code})
        payload = resp.json()
        token = payload.get("data") or ""
        if not token:
            raise RuntimeError(f"登录失败，hmdp 返回: {payload}")
        self._token = token
        logger.info("hmdp 登录成功，token=%s...", token[:8])
        return token

    # ---------------- 业务接口 ----------------

    def _get(self, path: str, params: dict | None = None) -> HmdpResult:
        try:
            headers = {}
            try:
                headers["authorization"] = self._ensure_token()
            except Exception:  # 公开接口拿不到 token 也无所谓
                pass
            resp = self._http.get(path, params=params, headers=headers)
            return self._parse(resp)
        except httpx.TimeoutException:
            return HmdpResult(ok=False, message="调用超时，业务服务可能繁忙")
        except httpx.HTTPError as exc:
            return HmdpResult(ok=False, message=f"网络异常：{exc}")
        except Exception as exc:  # noqa: BLE001
            return HmdpResult(ok=False, message=f"未知异常：{exc}")

    def _parse(self, resp: httpx.Response) -> HmdpResult:
        if resp.status_code != 200:
            return HmdpResult(ok=False, message=f"HTTP {resp.status_code}", raw_status=resp.status_code)
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return HmdpResult(ok=False, message="响应不是合法 JSON")
        if not payload.get("success"):
            return HmdpResult(ok=False, message=payload.get("errorMsg") or "业务返回失败")
        return HmdpResult(ok=True, data=payload.get("data"), raw_status=200)

    def list_shops_by_name(self, keyword: str, current: int = 1) -> HmdpResult:
        return self._get("/shop/of/name", {"name": keyword, "current": current})

    def list_shops_by_type(self, type_id: int, current: int = 1) -> HmdpResult:
        return self._get("/shop/of/type", {"typeId": type_id, "current": current})

    def get_shop(self, shop_id: int) -> HmdpResult:
        return self._get(f"/shop/{shop_id}")

    def list_vouchers(self, shop_id: int) -> HmdpResult:
        return self._get(f"/voucher/list/{shop_id}")

    def seckill(self, voucher_id: int) -> HmdpResult:
        """抢券：不可逆写操作，必须带 token。"""
        try:
            resp = self._http.post(
                f"/voucher-order/seckill/{voucher_id}",
                headers={"authorization": self._ensure_token()},
            )
            result = self._parse(resp)
            if result.ok:
                result.data = {"orderId": result.data}
            return result
        except httpx.TimeoutException:
            return HmdpResult(ok=False, message="抢券请求超时，结果未知，请勿重复提交")
        except Exception as exc:  # noqa: BLE001
            return HmdpResult(ok=False, message=f"抢券异常：{exc}")

    def close(self) -> None:
        self._http.close()


client = HmdpClient()
