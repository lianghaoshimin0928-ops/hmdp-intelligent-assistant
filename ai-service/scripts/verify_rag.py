r"""RAG 检索链路自检 + 双模式对比（不消耗任何 API 额度）。

    cd ai-service
    .\.venv\Scripts\python.exe scripts\verify_rag.py

跑两种召回方式，对比命中率：

1. **BM25**（默认，真实可用）：纯 Python 关键词检索，直接吃 data/docs 的语料。
2. **向量**（这里用「字符二元组哈希向量」冒充 embedding）：
   只为验证 vector 分支的代码路径通不通，不代表真实 embedding 的语义能力。

这个对比本身就是一个可写进简历/面试的结论：
在小规模、**专有名词密集**的知识库上，BM25 的命中率常能压过弱 embedding，
所以生产上更常见的是「BM25 + 向量」混合检索（如 RRF 融合），而不是单靠向量。
"""

from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from langchain_core.embeddings import Embeddings  # noqa: E402

DIM = 256
_CLEAN = re.compile(r"\s+")


class HashingEmbeddings(Embeddings):
    """字符二元组 → 哈希到固定维度 → L2 归一化。确定性、零依赖。"""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _CLEAN.sub("", text or "")
        if len(tokens) < 2:
            tokens = tokens + "  "
        for i in range(len(tokens) - 1):
            vec[zlib.crc32(tokens[i:i + 2].encode("utf-8")) % self.dim] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        return [v / norm for v in vec] if norm else vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


# (问题, 可接受的命中文档关键词列表)
CASES: list[tuple[str, list[str]]] = [
    ("秒杀券一个人能买几张", ["秒杀规则", "常见问题FAQ"]),
    ("103茶餐厅有什么优惠券", ["103茶餐厅"]),
    ("海底捞营业到几点", ["营业时间", "美食商户名录"]),
    ("退款怎么申请", ["退款与售后"]),
    ("评分最低的商户是哪家", ["KTV商户名录", "热门商户推荐", "商户评分"]),
    ("优惠券的折扣怎么算", ["优惠券体系", "103茶餐厅"]),
    ("签到连续天数怎么算", ["签到与积分"]),
    ("周末去103茶餐厅能用哪张券", ["103茶餐厅", "优惠券体系"]),
]


def evaluate(label: str, mode: str, emb: Embeddings | None) -> tuple[int, int]:
    from app.rag.retriever import retrieve

    print(f"\n{'=' * 70}")
    print(f"【{label}】 mode={mode}")
    print("=" * 70)

    hit1 = 0
    hit3 = 0
    for question, accepted in CASES:
        results = retrieve(question, top_k=20, top_n=3, embedding_function=emb, mode=mode)
        sources = [r.source for r in results]
        ok1 = bool(sources) and any(a in sources[0] for a in accepted)
        ok3 = any(any(a in s for a in accepted) for s in sources)
        hit1 += int(ok1)
        hit3 += int(ok3)
        mark = "HIT " if ok3 else "MISS"
        top = sources[0].split("-", 1)[-1].replace(".md", "") if sources else "（无结果）"
        print(f"  [{mark}] {question:<26} top1={'Y' if ok1 else 'N'}  -> {top}")

    n = len(CASES)
    print(f"\n  top1 命中 {hit1}/{n}    top3 命中 {hit3}/{n}")
    return hit1, hit3


def main() -> int:
    from app.config import settings
    from app.rag.indexer import build_index, load_documents, split_documents
    from app.rag.retriever import _resolve_mode, reset_bm25_cache

    docs_dir = Path(__file__).resolve().parents[1] / "data" / "docs"
    print("=" * 70)
    print("RAG 检索链路自检（BM25 + 向量，均不消耗 API 额度）")
    print("=" * 70)

    docs = load_documents(docs_dir)
    chunks = split_documents(docs)
    print(f"\n文档 {len(docs)} 篇 → chunk {len(chunks)} 个"
          f"（chunk_size={settings.rag_chunk_size}, overlap={settings.rag_chunk_overlap}）")
    print(f"auto 模式实际会走：{_resolve_mode(None)}")

    # ---- 模式 1：BM25（真实可用）----
    reset_bm25_cache()
    b1, b3 = evaluate("BM25 关键词检索", "bm25", None)

    # ---- 模式 2：向量（哈希 embedding，仅验证代码路径）----
    emb = HashingEmbeddings()
    offline_dir = Path(__file__).resolve().parents[1] / "data" / "chroma_offline"
    old_dir = settings.chroma_dir
    settings.chroma_dir = str(offline_dir)
    try:
        stats = build_index(reset=True, docs_dir=docs_dir, embedding_function=emb)
        print(f"\n  已为向量模式临时建索引：{stats['chunks']} chunk")
        v1, v3 = evaluate("向量检索（哈希 embedding 占位）", "vector", emb)
    finally:
        settings.chroma_dir = old_dir

    # ---- 汇总 ----
    n = len(CASES)
    print(f"\n{'=' * 70}")
    print("对比结论")
    print("=" * 70)
    print(f"  BM25 : top1 {b1}/{n}   top3 {b3}/{n}")
    print(f"  向量 : top1 {v1}/{n}   top3 {v3}/{n}   （哈希向量，非真实 embedding）")
    print()
    if b3 >= n - 1:
        print("  [PASS] BM25 通道可用，RAG 现在就能跑（不需要 embedding 服务）")
    else:
        print("  [WARN] BM25 命中率偏低，检查切分参数或文档内容")
    print("  注意：向量那组用的是哈希占位向量，不代表真实 embedding 的效果；")
    print("       真实 embedding（text-embedding-v3 / BGE）的语义泛化会明显更好。")
    print("=" * 70)
    return 0 if b3 >= n - 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
