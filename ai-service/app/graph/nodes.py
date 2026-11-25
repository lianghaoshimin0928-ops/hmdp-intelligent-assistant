"""图的各个节点。

三个值得在面试里展开的设计：
1. **权限分离**：给「ReAct 工具循环」的只有查询类工具（query_shop / get_shop_detail /
   list_vouchers）。seckill_voucher 这个写操作**不给模型**，只能走 confirm 节点。
   这样「模型自作主张抢券」在架构上就不可能发生，而不是靠 Prompt 祈祷。
2. **interrupt 挂起**：confirm 节点调 interrupt()，整张图暂停并把状态落盘到 checkpointer；
   用户回复后由 Command(resume=...) 从断点继续。
3. **interrupt 的坑**：恢复时该节点会**从头重新执行**，所以 interrupt 之前的代码必须幂等。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langgraph.types import interrupt

from app.graph.state import (
    INTENT_CHAT,
    INTENT_RAG,
    INTENT_SECKILL,
    INTENT_SHOP,
    VALID_INTENTS,
    AgentState,
)
from app.rag.chain import answer_with_rag
from app.rag.retriever import retrieve
from app.tools.hmdp_tools import ALL_TOOLS, describe_tools, seckill_voucher

logger = logging.getLogger(__name__)

# 给模型的只有查询类工具，写操作被排除在外
SAFE_TOOLS = [t for t in ALL_TOOLS if t.name != "seckill_voucher"]

ROUTER_PROMPT = """你是意图分类器。把用户的话分到下面四类之一，只输出英文标签，不要输出其他任何内容。

- chat    ：闲聊、打招呼、与业务无关的问题
- shop    ：想找店、查店详情、查优惠券
- rag     ：问经营知识、规则、攻略（需要查资料才能回答）
- seckill ：明确表达要抢某张券（如「帮我抢」「下单」「确认抢」）

用户输入：{query}
"""

AGENT_SYSTEM = """你是「本地生活智能助手」，可以调用工具查询真实的商户和优惠券数据。

{tools}

规则：
1. 严禁编造工具返回的数据，查不到就如实说。
2. 涉及价格、库存时说明这是查询结果。
3. 你**没有**抢券权限；用户要抢券时，告诉用户「确认后我会执行」，由系统走确认流程。
4. 回答用中文，简洁。
"""


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
def make_route_node(llm: Runnable | None) -> Callable[[AgentState], dict]:
    def route(state: AgentState) -> dict:
        preset = state.get("intent")
        if preset in VALID_INTENTS:
            return {"trace": [f"route:preset={preset}"]}

        query = _last_user_text(state)
        if llm is None:
            return {"intent": INTENT_CHAT, "trace": ["route:no-llm->chat"]}

        try:
            raw = llm.invoke(ROUTER_PROMPT.format(query=query))
            text = str(getattr(raw, "content", raw)).strip().lower()
            for candidate in (INTENT_SECKILL, INTENT_SHOP, INTENT_RAG, INTENT_CHAT):
                if candidate in text:
                    return {"intent": candidate, "trace": [f"route:llm->{candidate}"]}
        except Exception as exc:  # noqa: BLE001
            logger.warning("意图识别失败，降级为 chat: %s", exc)

        return {"intent": INTENT_CHAT, "trace": ["route:fallback->chat"]}

    return route


# --------------------------------------------------------------------------
# ReAct 工具循环
# --------------------------------------------------------------------------
def make_agent_node(llm: Runnable | None) -> Callable[[AgentState], dict]:
    if llm is None:
        def stub(state: AgentState) -> dict:
            return {
                "messages": [AIMessage(content="（未配置模型，无法执行查询）")],
                "trace": ["agent:no-llm"],
            }

        return stub

    bound = llm.bind_tools(SAFE_TOOLS)

    def agent(state: AgentState) -> dict:
        system = AGENT_SYSTEM.format(tools=describe_tools())
        msgs = [SystemMessage(content=system), *state.get("messages", [])]
        resp = bound.invoke(msgs)
        return {"messages": [resp], "trace": ["agent"]}

    return agent


def after_route(state: AgentState) -> str:
    """路由节点之后的跳转条件。"""
    intent = state.get("intent") or INTENT_CHAT
    return intent if intent in VALID_INTENTS else INTENT_CHAT


def after_agent(state: AgentState) -> str:
    """有 tool_calls 就继续循环，否则结束。"""
    msgs = state.get("messages") or []
    last = msgs[-1] if msgs else None
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "tools"
    return "end"


def finish_from_messages(state: AgentState) -> dict:
    """把最后一条 AI 消息作为最终回答。"""
    for msg in reversed(state.get("messages") or []):
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
            return {"answer": str(msg.content), "trace": ["finish"]}
    return {"answer": "抱歉，我没有查到相关信息。", "trace": ["finish:empty"]}


def make_chat_node(llm: Runnable | None) -> Callable[[AgentState], dict]:
    def chat(state: AgentState) -> dict:
        if llm is None:
            return {"answer": "（未配置模型）", "trace": ["chat:no-llm"]}
        resp = llm.invoke(state.get("messages") or [])
        return {"answer": str(getattr(resp, "content", resp)), "trace": ["chat"]}

    return chat


# --------------------------------------------------------------------------
# RAG
# --------------------------------------------------------------------------
def make_rag_node(llm: Runnable | None) -> Callable[[AgentState], dict]:
    def rag(state: AgentState) -> dict:
        query = _last_user_text(state)
        hits = retrieve(query)
        result = answer_with_rag(query, hits, llm=llm)
        return {
            "answer": result["answer"],
            "sources": result["sources"],
            "trace": [f"rag:hits={result['used_chunks']}"],
        }

    return rag


# --------------------------------------------------------------------------
# 抢券：interrupt 人工确认
# --------------------------------------------------------------------------
def confirm_node(state: AgentState) -> dict:
    """挂起整张图，等用户确认。

    注意：恢复执行时本节点会**从头重跑**，所以 interrupt 之前不要写有副作用的代码。
    """
    voucher_id = state.get("voucher_id")
    decision = interrupt(
        {
            "type": "confirm_required",
            "action": "seckill_voucher",
            "voucher_id": voucher_id,
            "message": f"抢购优惠券 {voucher_id} 是不可逆操作，确认要抢吗？",
            "expected": "回复「确认」继续，其他任意内容视为取消",
        }
    )
    return {"confirmed": _is_yes(decision), "trace": [f"confirm:{decision!r}"]}


def after_confirm(state: AgentState) -> str:
    return "do_seckill" if state.get("confirmed") else "cancelled"


def do_seckill_node(state: AgentState) -> dict:
    """这一步才真正调用写操作。"""
    voucher_id = int(state.get("voucher_id") or 0)
    observation = seckill_voucher.invoke({"voucher_id": voucher_id})
    return {"answer": str(observation), "trace": [f"do_seckill:{voucher_id}"]}


def cancelled_node(state: AgentState) -> dict:
    return {
        "answer": "好的，已取消抢券，没有做任何修改。",
        "trace": ["cancelled"],
    }


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _last_user_text(state: AgentState) -> str:
    for msg in reversed(state.get("messages") or []):
        if isinstance(msg, HumanMessage):
            return str(msg.content)
        if isinstance(msg, dict) and msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""


_YES_WORDS = ("确认", "确定", "抢", "yes", "y", "ok", "好", "可以", "同意", "是")


def _is_yes(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return False
    if any(w in text for w in ("取消", "不要", "别", "no", "n", "否")):
        return False
    return any(w in text for w in _YES_WORDS) or bool(re.search(r"^[1１]$", text))
