"""两阶段检索：向量粗排（Top-K）→ Rerank 精排（Top-N）。

为什么要两阶段（面试必问）：
- 向量检索是「语义近似」，召回率高但排序粗，还容易被高频词带偏；
- Rerank 模型对「查询-段落」做交叉编码打分，精度高但慢，只能用在少量候选上；
- 所以先用便宜的向量检索把候选缩到 20 条，再用精排挑 5 条喂给模型。

本文件的 rerank 提供了两种实现：
1. keyword（默认）：向量分 + 关键词重合度加权，零额外依赖、可离线；
2. llm：用大模型逐条打分（LLM-as-reranker），精度更好但费 token。
生产环境应换成专用 rerank 模型（bge-reranker / 通义 gte-rerank），接口位置见 TODO。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from langchain_core.embeddings import Embeddings

from app.config import settings
from app.rag.indexer import get_vectorstore

logger = logging.getLogger(__name__)

_CJK = re.compile(r"[\u4e00-\u9fa5]{2,}|[a-zA-Z0-9]+")


@dataclass
class Hit:
    content: str
    source: str
    chunk_id: int | None
    vector_score: float       # 0~1，越大越相关
    rerank_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "source": self.source,
            "chunk_id": self.chunk_id,
            "vector_score": round(self.vector_score, 4),
            "rerank_score": round(self.rerank_score, 4),
        }


def _tokens(text: str) -> set[str]:
    return set(_CJK.findall(text.lower()))


def _keyword_rerank(query: str, hits: list[Hit], top_n: int) -> list[Hit]:
    """向量分 + 关键词重合度加权。纯本地，不花钱。"""
    q = _tokens(query)
    for hit in hits:
        if not q:
            hit.rerank_score = hit.vector_score
            continue
        overlap = len(q & _tokens(hit.content)) / len(q)
        hit.rerank_score = 0.6 * hit.vector_score + 0.4 * overlap
    return sorted(hits, key=lambda h: h.rerank_score, reverse=True)[:top_n]


def _llm_rerank(query: str, hits: list[Hit], top_n: int) -> list[Hit]:
    """用大模型给每条候选打 0-10 分。费 token，只在需要高精度时开。"""
    from app.llm import get_chat_model

    llm = get_chat_model(temperature=0)
    for hit in hits:
        prompt = (
            "你在为一个检索系统做相关性打分。\n"
            f"用户问题：{query}\n"
            f"候选段落：{hit.content[:500]}\n\n"
            "这个段落能回答用户问题吗？只输出 0-10 的一个整数，10 表示完全能回答，0 表示无关。"
        )
        try:
            raw = llm.invoke(prompt).content
            score = int(re.search(r"\d+", str(raw)).group()) / 10.0
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM rerank 失败，降级为向量分: %s", exc)
            score = hit.vector_score
        hit.rerank_score = score
    return sorted(hits, key=lambda h: h.rerank_score, reverse=True)[:top_n]


def retrieve(
    query: str,
    top_k: int | None = None,
    top_n: int | None = None,
    embedding_function: Embeddings | None = None,
    strategy: str = "keyword",
) -> list[Hit]:
    """检索入口：粗排 + 精排。"""
    top_k = top_k or settings.rag_top_k
    top_n = top_n or settings.rag_rerank_top_n

    store = get_vectorstore(embedding_function)
    # Chroma 返回的是距离（越小越近），这里归一化成 0~1 的相似度
    pairs = store.similarity_search_with_score(query, k=top_k)

    hits: list[Hit] = []
    for doc, distance in pairs:
        similarity = 1.0 / (1.0 + float(distance))
        hits.append(
            Hit(
                content=doc.page_content,
                source=str(doc.metadata.get("source", "unknown")),
                chunk_id=doc.metadata.get("chunk_id"),
                vector_score=similarity,
            )
        )

    if not hits:
        return []

    if strategy == "llm":
        return _llm_rerank(query, hits, top_n)
    return _keyword_rerank(query, hits, top_n)
