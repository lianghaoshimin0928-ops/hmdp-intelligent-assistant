"""FastAPI 接口层。

对外只有三件事：
1. /chat          多轮对话（内部走 LangGraph）
2. /chat/resume   恢复被 interrupt 挂起的图
3. /rag/index     重建知识库索引
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.config import settings
from app.tools.hmdp_tools import describe_tools

logger = logging.getLogger(__name__)
router = APIRouter()


# ------------------------------------------------------------------ 模型
class ChatIn(BaseModel):
    message: str = Field(..., description="用户这句话")
    thread_id: str = Field("default", description="会话 id，同一个 id 共享上下文")
    voucher_id: int | None = Field(None, description="抢券时指定券 id（会触发人工确认）")
    intent: str | None = Field(None, description="调试用：强制指定意图 chat/shop/rag/seckill")


class ResumeIn(BaseModel):
    thread_id: str
    answer: str = Field(..., description="对 interrupt 的回复，如「确认」")


# ------------------------------------------------------------------ 工具函数
def _graph():
    from app.graph.build import get_default_graph

    return get_default_graph()


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _shape(result: dict, thread_id: str) -> dict:
    """把图的返回值整理成前端友好的结构。"""
    interrupts = result.get("__interrupt__")
    if interrupts:
        first = interrupts[0]
        payload = getattr(first, "value", None)
        if payload is None:
            payload = str(first)
        return {
            "status": "interrupt",
            "thread_id": thread_id,
            "interrupt": payload,
            "hint": "调用 POST /api/chat/resume 传入 answer=确认 继续",
        }
    return {
        "status": "done",
        "thread_id": thread_id,
        "answer": result.get("answer", ""),
        "sources": result.get("sources", []),
        "trace": result.get("trace", []),
    }


def _build_inputs(body: ChatIn) -> dict:
    inputs: dict[str, Any] = {"messages": [HumanMessage(content=body.message)]}
    if body.voucher_id is not None:
        inputs["voucher_id"] = body.voucher_id
    if body.intent:
        inputs["intent"] = body.intent
    return inputs


# ------------------------------------------------------------------ 接口
@router.get("/health")
def health() -> dict:
    from app.llm import MissingApiKeyError

    ok = True
    reason = ""
    try:
        from app.graph.build import get_default_graph

        get_default_graph()
    except MissingApiKeyError as exc:
        ok, reason = False, str(exc)
    except Exception as exc:  # noqa: BLE001
        ok, reason = False, f"{type(exc).__name__}: {exc}"

    return {
        "ok": ok,
        "reason": reason,
        "model": settings.ai_model,
        "embedding": settings.ai_embedding_model,
        "hmdp_base_url": settings.hmdp_base_url,
    }


@router.get("/tools")
def tools() -> dict:
    return {"tools": describe_tools()}


@router.post("/chat")
def chat(body: ChatIn) -> dict:
    try:
        result = _graph().invoke(_build_inputs(body), _config(body.thread_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("对话失败")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return _shape(result, body.thread_id)


@router.post("/chat/resume")
def resume(body: ResumeIn) -> dict:
    try:
        result = _graph().invoke(Command(resume=body.answer), _config(body.thread_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("恢复失败")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return _shape(result, body.thread_id)


@router.post("/chat/stream")
def chat_stream(body: ChatIn):
    """按「节点粒度」流式推送，前端能实时看到 route → agent → tools 的过程。"""

    def gen():
        try:
            for chunk in _graph().stream(_build_inputs(body), _config(body.thread_id), stream_mode="updates"):
                for node, update in (chunk or {}).items():
                    payload = json.dumps(
                        {"node": node, "update": _safe(update)}, ensure_ascii=False
                    )
                    yield f"event: step\ndata: {payload}\n\n"
            yield "event: done\ndata: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("流式对话失败")
            yield f"event: failed\ndata: {type(exc).__name__}: {exc}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _safe(obj: Any) -> Any:
    """把 LangChain 的消息对象转成可 JSON 序列化的结构。"""
    if isinstance(obj, dict):
        return {k: _safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe(v) for v in obj]
    if hasattr(obj, "content"):
        return {"type": type(obj).__name__, "content": _safe(obj.content)}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


# ------------------------------------------------------------------ RAG
@router.post("/rag/index")
def rag_index(reset: bool = True) -> dict:
    from app.rag.indexer import build_index

    try:
        return {"ok": True, "stats": build_index(reset=reset)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("建索引失败")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.get("/rag/search")
def rag_search(q: str, top_k: int = 20, top_n: int = 5, strategy: str = "keyword") -> dict:
    from app.rag.retriever import retrieve

    try:
        hits = retrieve(q, top_k=top_k, top_n=top_n, strategy=strategy)
        return {"query": q, "count": len(hits), "hits": [h.to_dict() for h in hits]}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
