r"""离线验证 RAG 链路：不调用任何 embedding / 大模型 API，不花一分钱。

    cd ai-service
    .\.venv\Scripts\python.exe scripts\verify_rag.py

原理：用一个「字符二元组哈希词袋」冒充 embedding 模型。
它对中文是有效的词法相似度（同义改写会失效，但关键词命中足够），
足以验证：文档加载 → 切分 → 建索引 → 向量检索 → rerank → 命中正确来源。

真实语义效果必须接真 embedding（text-embedding-v3 / BGE），本脚本不替代它。
"""

from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.embeddings import Embeddings  # noqa: E402

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

DIM = 256
_CLEAN = re.compile(r"\s+")


class HashingEmbeddings(Embeddings):
    """字符二元组 → 哈希到固定维度 → L2 归一化。确定性、零依赖、可离线。"""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _CLEAN.sub("", text or "")
        if len(tokens) < 2:
            tokens = tokens + "  "
        for i in range(len(tokens) - 1):
            gram = tokens[i:i + 2]
            vec[zlib.crc32(gram.encode("utf-8")) % self.dim] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


# 用于验证的问答对：(问题, 可接受的命中文档关键词列表)
# 同一个问题可能合理地命中多篇文档，所以这里给的是"可接受集合"而不是唯一答案
CASES: list[tuple[str, list[str]]] = [
    ("秒杀券一个人能买几张", ["秒杀规则", "常见问题FAQ"]),
    ("103茶餐厅有什么优惠券", ["103茶餐厅"]),
    ("海底捞营业到几点", ["营业时间", "美食商户名录"]),
    ("退款怎么申请", ["退款与售后"]),
    ("评分最低的商户是哪家", ["KTV商户名录", "热门商户推荐", "商户评分"]),
    ("优惠券的折扣怎么算", ["优惠券体系", "103茶餐厅"]),
    ("签到连续天数怎么算", ["签到与积分"]),
    ("为什么笔记里人均和商户页不一样", ["内容口径", "探店笔记"]),
]


def main() -> int:
    from app.config import settings
    from app.rag.indexer import build_index, load_documents, split_documents
    from app.rag.retriever import retrieve

    docs_dir = Path(__file__).resolve().parents[1] / "data" / "docs"
    print("=" * 66)
    print("RAG 离线验证（词袋哈希向量，不消耗 API）")
    print("=" * 66)

    docs = load_documents(docs_dir)
    chunks = split_documents(docs)
    print(f"\n[1] 文档加载：{len(docs)} 篇")
    for d in docs:
        print(f"    - {d.metadata['source']}  ({len(d.page_content)} 字符)")
    print(f"\n[2] 切分：{len(chunks)} 个 chunk"
          f"（chunk_size={settings.rag_chunk_size}, overlap={settings.rag_chunk_overlap}）")

    # 用独立目录，不污染真实索引
    offline_dir = Path(__file__).resolve().parents[1] / "data" / "chroma_offline"
    old_dir = settings.chroma_dir
    settings.chroma_dir = str(offline_dir)
    emb = HashingEmbeddings()

    try:
        stats = build_index(reset=True, docs_dir=docs_dir, embedding_function=emb)
        print(f"\n[3] 建索引：collection={stats['collection']}  写入 {stats['chunks']} 个 chunk")

        print(f"\n[4] 检索验证（{len(CASES)} 个问题，看 top-3 是否命中预期文档）")
        hits = 0
        for question, expected_list in CASES:
            results = retrieve(question, top_k=10, top_n=3, embedding_function=emb)
            sources = [r.source for r in results]
            ok = any(any(e in s for e in expected_list) for s in sources)
            hits += 1 if ok else 0
            mark = "HIT " if ok else "MISS"
            print(f"\n    [{mark}] {question}   (可接受: {' / '.join(expected_list)})")
            for i, r in enumerate(results, 1):
                snippet = r.content.replace("\n", " ")[:56]
                print(f"          {i}. {r.source:<34} score={r.rerank_score:.4f}  {snippet}")

        print("\n" + "=" * 66)
        print(f"命中 {hits}/{len(CASES)}")
        if hits >= len(CASES) - 1:
            print("[PASS] 链路通畅；换成真实 embedding 后语义效果会明显更好")
        else:
            print("[WARN] 命中率偏低，检查切分参数或文档内容")
        print("=" * 66)
        return 0 if hits >= len(CASES) - 1 else 1
    finally:
        settings.chroma_dir = old_dir


if __name__ == "__main__":
    raise SystemExit(main())
