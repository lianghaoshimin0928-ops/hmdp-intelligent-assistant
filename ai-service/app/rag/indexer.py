"""RAG 索引构建：文档 → 切分 → 向量化 → Chroma。

面试要点：
1. 不用 langchain-community 的 DirectoryLoader（该包已停止维护），自己用 pathlib 读，
   依赖更少、行为更可控；
2. 切分参数要能讲出为什么：chunk 太大噪声多、太小上下文断裂；
3. 每个 chunk 保留 source 元数据，回答时才能给出「出处」。
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import settings
from app.llm import get_embeddings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "hmdp_docs"
DOCS_DIR = Path(__file__).resolve().parents[2] / "data" / "docs"

# 中文要按标点切，否则会从词语中间断开
SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]


def load_documents(docs_dir: Path | None = None) -> list[Document]:
    """读取 docs 目录下的 .md / .txt / .pdf。"""
    root = docs_dir or DOCS_DIR
    if not root.exists():
        raise FileNotFoundError(f"文档目录不存在: {root}")

    docs: list[Document] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        try:
            if suffix in {".md", ".txt"}:
                text = path.read_text(encoding="utf-8")
            elif suffix == ".pdf":
                from pypdf import PdfReader

                reader = PdfReader(str(path))
                text = "\n".join((page.extract_text() or "") for page in reader.pages)
            else:
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 %s 失败: %s", path, exc)
            continue

        if not text.strip():
            continue
        docs.append(
            Document(
                page_content=text,
                metadata={"source": path.name, "path": str(path)},
            )
        )

    logger.info("载入文档 %d 个", len(docs))
    return docs


def split_documents(docs: list[Document]) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
        separators=SEPARATORS,
        length_function=len,
    )
    chunks = splitter.split_documents(docs)
    # 给每个 chunk 编个号，方便回答里引用
    for i, chunk in enumerate(chunks):
        chunk.metadata["chunk_id"] = i
    logger.info("切分为 %d 个 chunk（size=%d, overlap=%d）",
                len(chunks), settings.rag_chunk_size, settings.rag_chunk_overlap)
    return chunks


def get_vectorstore(embedding_function: Embeddings | None = None):
    """拿到（或创建）Chroma 实例。

    embedding_function 可注入，测试时用假的 embedding 就能完全离线跑通链路。
    """
    from langchain_chroma import Chroma

    emb = embedding_function or get_embeddings()
    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=emb,
        persist_directory=settings.chroma_dir,
    )


def build_index(
    reset: bool = True,
    docs_dir: Path | None = None,
    embedding_function: Embeddings | None = None,
) -> dict:
    """重建索引，返回统计信息。"""
    docs = load_documents(docs_dir)
    if not docs:
        raise RuntimeError("没有可索引的文档，请先在 data/docs 下放 .md/.txt/.pdf")

    chunks = split_documents(docs)

    store = get_vectorstore(embedding_function)
    if reset:
        try:
            store.reset_collection()
        except Exception as exc:  # noqa: BLE001
            logger.warning("重置 collection 失败（可忽略）: %s", exc)

    store.add_documents(chunks)

    return {
        "documents": len(docs),
        "chunks": len(chunks),
        "collection": COLLECTION_NAME,
        "source_files": [d.metadata.get("source") for d in docs],
    }
