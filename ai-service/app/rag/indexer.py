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

# 只能靠 MinerU 解析的格式（md/txt 直接读，pdf 可回退 pypdf）
_MINERU_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp",
    ".html",
}


def load_documents(docs_dir: Path | None = None, force_parse: bool = False) -> list[Document]:
    """读取 docs 目录下的文档。

    - `.md` / `.txt`：直接读
    - `.pdf`：优先 MinerU（配置了 token 时），失败回退 pypdf
    - Office / 图片 / html：只有 MinerU 能处理，没 token 就跳过
    """
    root = docs_dir or DOCS_DIR
    if not root.exists():
        raise FileNotFoundError(f"文档目录不存在: {root}")

    docs: list[Document] = []
    parsers: dict[str, int] = {}

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text, parser = _extract_text(path, force_parse=force_parse)
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析 %s 失败: %s", path, exc)
            continue

        if text is None:
            if parser == "need-mineru":
                logger.info("跳过 %s（需要 MinerU 才能解析）", path.name)
            continue
        if not text.strip():
            logger.warning("跳过 %s（解析结果为空）", path.name)
            continue

        parsers[parser] = parsers.get(parser, 0) + 1
        docs.append(
            Document(
                page_content=text,
                metadata={"source": path.name, "path": str(path), "parser": parser},
            )
        )

    logger.info("载入文档 %d 个，解析方式分布: %s", len(docs), parsers)
    return docs


def _extract_text(path: Path, force_parse: bool = False) -> tuple[str | None, str]:
    """返回 (正文, 解析方式)。正文为 None 表示该文件无法/无需解析。"""
    suffix = path.suffix.lower()

    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8"), "plain"

    if suffix not in _MINERU_SUFFIXES:
        return None, "unsupported"

    if settings.parser == "mineru" and not settings.mineru_token.strip():
        raise RuntimeError("配置了 parser=mineru，但 MINERU_TOKEN 为空")

    if _mineru_available():
        from app.rag.mineru_client import parse_to_markdown

        try:
            return parse_to_markdown(path, force=force_parse), "mineru"
        except Exception as exc:  # noqa: BLE001
            logger.warning("MinerU 解析 %s 失败，尝试回退: %s", path.name, exc)
            if suffix != ".pdf":
                raise

    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages), "pypdf"

    return None, "need-mineru"


def _mineru_available() -> bool:
    return settings.parser in ("auto", "mineru") and bool(settings.mineru_token.strip())


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
    force_parse: bool = False,
) -> dict:
    """重建索引，返回统计信息。

    force_parse=True 时忽略 MinerU 的本地解析缓存，强制重新解析。
    """
    docs = load_documents(docs_dir, force_parse=force_parse)
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

    parser_stats: dict[str, int] = {}
    for d in docs:
        p = d.metadata.get("parser", "unknown")
        parser_stats[p] = parser_stats.get(p, 0) + 1

    return {
        "documents": len(docs),
        "chunks": len(chunks),
        "collection": COLLECTION_NAME,
        "source_files": [d.metadata.get("source") for d in docs],
        "parsers": parser_stats,
    }
