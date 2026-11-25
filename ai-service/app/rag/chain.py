"""RAG 生成链：把检索到的段落拼进 Prompt，要求模型带引用作答。

防幻觉三条硬约束（面试可以直接背）：
1. 只允许依据给定资料回答，资料里没有就说不知道；
2. 每句结论后面标 [来源编号]，方便人工核对；
3. 资料不足时明确拒答，而不是用常识硬补。
"""

from __future__ import annotations

import logging

from app.llm import get_chat_model
from app.rag.retriever import Hit

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是「本地生活智能助手」的知识问答模块。

【回答规则】
1. 只能依据下面提供的【参考资料】回答，禁止使用参考资料以外的信息。
2. 每个结论后面用方括号标注来源编号，例如 [1]、[2]。
3. 如果参考资料无法回答用户问题，直接回复「抱歉，现有资料里没有相关信息」，不要猜测。
4. 回答用中文，简洁，不超过 200 字。

【参考资料】
{context}
"""


def build_context(hits: list[Hit]) -> str:
    if not hits:
        return "（无参考资料）"
    blocks = []
    for i, hit in enumerate(hits, 1):
        blocks.append(f"[{i}] 来源：{hit.source}\n{hit.content}")
    return "\n\n".join(blocks)


def answer_with_rag(query: str, hits: list[Hit], llm=None) -> dict:
    """返回 {answer, sources, used_chunks}。"""
    if not hits:
        return {
            "answer": "抱歉，现有资料里没有相关信息。",
            "sources": [],
            "used_chunks": 0,
        }

    model = llm or get_chat_model()
    system = SYSTEM_PROMPT.format(context=build_context(hits))
    messages = [("system", system), ("human", query)]

    try:
        resp = model.invoke(messages)
        answer = resp.content if hasattr(resp, "content") else str(resp)
    except Exception as exc:  # noqa: BLE001
        logger.exception("RAG 生成失败")
        answer = f"生成回答时出错：{exc}"

    return {
        "answer": str(answer).strip(),
        "sources": [
            {"index": i, "source": h.source, "chunk_id": h.chunk_id,
             "rerank_score": round(h.rerank_score, 4)}
            for i, h in enumerate(hits, 1)
        ],
        "used_chunks": len(hits),
    }
