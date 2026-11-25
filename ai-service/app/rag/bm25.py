r"""纯 Python BM25 检索，零外部依赖、完全离线。

为什么需要它：
- 向量检索依赖 embedding 服务；没有可用的 embedding provider 时（例如只有 DeepSeek 的 key，
  而 DeepSeek 并不提供 /embeddings 接口），RAG 就整条断了；
- BM25 走字面匹配，对「专有名词、店名、编号」这类查询反而更准；
- 两者组合就是标准的「混合检索」：BM25 保召回下限，向量补语义泛化。

中文分词策略：字符二元组（bigram）。
不引入 jieba 这类分词器，是为了零依赖；对短文档检索，bigram 的效果已经足够，
而且对未登录词（新店名、菜品名）天然友好。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-zA-Z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """英文/数字按词切，中文按字符二元组切。"""
    lowered = (text or "").lower()
    tokens = _WORD.findall(lowered)
    for seg in _CJK.findall(lowered):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return tokens


@dataclass
class BM25Document:
    content: str
    source: str = ""
    chunk_id: int | None = None
    metadata: dict = field(default_factory=dict)


class BM25Index:
    """标准 BM25（k1 控制词频饱和，b 控制文档长度归一化）。"""

    def __init__(self, docs: list[BM25Document], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = docs
        self.k1 = k1
        self.b = b
        self._tfs: list[Counter] = []
        self._lens: list[int] = []
        self._df: Counter = Counter()

        for doc in docs:
            tokens = tokenize(doc.content)
            tf = Counter(tokens)
            self._tfs.append(tf)
            self._lens.append(len(tokens))
            for term in tf:
                self._df[term] += 1

        self.N = len(docs)
        self.avgdl = (sum(self._lens) / self.N) if self.N else 0.0

    def _idf(self, term: str) -> float:
        n = self._df.get(term, 0)
        return math.log(1.0 + (self.N - n + 0.5) / (n + 0.5))

    def search(self, query: str, top_k: int = 20) -> list[tuple[BM25Document, float]]:
        q_terms = tokenize(query)
        if not q_terms or not self.docs:
            return []

        weighted = [(t, self._idf(t)) for t in set(q_terms)]

        scored: list[tuple[int, float]] = []
        for i, tf in enumerate(self._tfs):
            dl = self._lens[i] or 1
            score = 0.0
            for term, idf in weighted:
                f = tf.get(term)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                score += idf * (f * (self.k1 + 1)) / denom
            if score > 0:
                scored.append((i, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [(self.docs[i], s) for i, s in scored[:top_k]]
