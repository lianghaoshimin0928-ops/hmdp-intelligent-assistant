"""Agent 的状态定义。

LangGraph 的 State 就是一个 TypedDict：
- 每个节点返回**部分字段**的 dict，框架负责合并；
- 带 `Annotated[list, add_messages]` 的字段是「累加」语义（reducer），
  其他字段是「覆盖」语义。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

# 意图枚举
INTENT_CHAT = "chat"        # 闲聊 / 通用问答
INTENT_SHOP = "shop"        # 查店、查券（ReAct 工具循环）
INTENT_RAG = "rag"          # 知识库问答
INTENT_SECKILL = "seckill"  # 抢券（必须人工确认）

VALID_INTENTS = {INTENT_CHAT, INTENT_SHOP, INTENT_RAG, INTENT_SECKILL}


class AgentState(TypedDict, total=False):
    # --- 对话 ---
    messages: Annotated[list[AnyMessage], add_messages]

    # --- 路由 ---
    intent: str

    # --- 抢券（不可逆操作）---
    voucher_id: int | None
    confirmed: bool | None

    # --- 输出 ---
    answer: str
    sources: list[dict]

    # --- 可观测：记录走过哪些节点，方便调试和演示 ---
    trace: Annotated[list[str], lambda a, b: a + b]
