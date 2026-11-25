"""组装 LangGraph 状态机。

        ┌─────────┐
        │  START  │
        └────┬────┘
             ▼
        ┌─────────┐   chat    ┌────────┐
        │  route  ├──────────►│  chat  ├──► END
        │(意图识别)│           └────────┘
        └────┬────┘   rag     ┌────────┐
             ├───────────────►│  rag   ├──► END
             │                └────────┘
             │   shop         ┌────────┐   tool_calls   ┌───────┐
             ├───────────────►│ agent  ├───────────────►│ tools │
             │                └───┬────┘◄───────────────┴───────┘
             │                    │ 无 tool_call
             │                    ▼
             │                ┌────────┐
             │                │ finish ├──► END
             │                └────────┘
             │  seckill       ┌──────────────────┐  confirmed   ┌────────────┐
             └───────────────►│ confirm(interrupt)├──────────────►│ do_seckill ├─► END
                              └────────┬─────────┘              └────────────┘
                                       │ 未确认
                                       ▼
                                  ┌───────────┐
                                  │ cancelled ├─► END
                                  └───────────┘
"""

from __future__ import annotations

from functools import lru_cache

from langchain_core.runnables import Runnable
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.graph.nodes import (
    SAFE_TOOLS,
    after_agent,
    after_confirm,
    cancelled_node,
    confirm_node,
    do_seckill_node,
    finish_from_messages,
    make_agent_node,
    make_chat_node,
    make_rag_node,
    make_route_node,
    after_route,
)
from app.graph.state import INTENT_CHAT, INTENT_RAG, INTENT_SECKILL, INTENT_SHOP, AgentState


def build_assistant_graph(llm: Runnable | None = None, checkpointer=None):
    """构建并编译图。

    llm 可注入（测试时传 None 或假模型）；checkpointer 默认内存版，
    生产应换成 RedisSaver / PostgresSaver —— 换掉这一行，图的逻辑完全不用改。
    """
    graph = StateGraph(AgentState)

    graph.add_node("route", make_route_node(llm))
    graph.add_node("chat", make_chat_node(llm))
    graph.add_node("agent", make_agent_node(llm))
    graph.add_node("tools", ToolNode(SAFE_TOOLS))
    graph.add_node("finish", finish_from_messages)
    graph.add_node("rag", make_rag_node(llm))
    graph.add_node("confirm", confirm_node)
    graph.add_node("do_seckill", do_seckill_node)
    graph.add_node("cancelled", cancelled_node)

    graph.add_edge(START, "route")

    graph.add_conditional_edges(
        "route",
        after_route,
        {
            INTENT_CHAT: "chat",
            INTENT_SHOP: "agent",
            INTENT_RAG: "rag",
            INTENT_SECKILL: "confirm",
        },
    )

    # ReAct 循环：agent → tools → agent ...
    graph.add_conditional_edges("agent", after_agent, {"tools": "tools", "end": "finish"})
    graph.add_edge("tools", "agent")
    graph.add_edge("finish", END)

    graph.add_edge("chat", END)
    graph.add_edge("rag", END)

    # 抢券：interrupt 人工确认
    graph.add_conditional_edges(
        "confirm", after_confirm, {"do_seckill": "do_seckill", "cancelled": "cancelled"}
    )
    graph.add_edge("do_seckill", END)
    graph.add_edge("cancelled", END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())


@lru_cache(maxsize=1)
def get_default_graph():
    """生产用：真实模型 + 内存 checkpointer（换成 RedisSaver 即可持久化）。"""
    from app.llm import get_chat_model

    return build_assistant_graph(llm=get_chat_model())
