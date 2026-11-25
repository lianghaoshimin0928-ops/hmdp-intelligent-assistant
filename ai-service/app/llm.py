"""大模型工厂：统一从这里取 ChatModel / Embeddings，方便测试时替换成假模型。

注意 chat 和 embedding 是**两套独立配置**：
- chat 走 AI_BASE_URL / AI_API_KEY（本项目是 DeepSeek）
- embedding 走 AI_EMBEDDING_BASE_URL / AI_EMBEDDING_API_KEY（本项目是阿里百炼）

原因是 DeepSeek 并不提供 /embeddings 接口（实测 404），必须换一家做向量化。
这种「chat 与 embedding 解耦」的配置方式在真实项目里很常见。
"""

from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.config import settings


class MissingApiKeyError(RuntimeError):
    pass


_PLACEHOLDER_MARKERS = ("你的", "xxx", "please-set", "placeholder")


def _looks_like_real_key(value: str | None) -> bool:
    if not value or len(value.strip()) < 8:
        return False
    lowered = value.lower()
    return not any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _require_key() -> None:
    if not _looks_like_real_key(settings.ai_api_key):
        raise MissingApiKeyError(
            "未配置 AI_API_KEY。请在 ai-service/.env 里填入真实 key（不是 .env.example）"
        )


@lru_cache(maxsize=8)
def get_chat_model(temperature: float | None = None, model: str | None = None) -> ChatOpenAI:
    """拿到一个 OpenAI 兼容的 ChatModel。

    DashScope / DeepSeek / 本地 vLLM 都走同一套兼容协议，
    换个 base_url + model 就能切，这也是面试可以讲的「模型可插拔」。
    """
    _require_key()
    return ChatOpenAI(
        model=model or settings.ai_model,
        api_key=settings.ai_api_key,          # type: ignore[arg-type]
        base_url=settings.ai_base_url,
        temperature=settings.ai_temperature if temperature is None else temperature,
        timeout=60,
        max_retries=2,
        # DeepSeek 的推理模型会先输出 reasoning_content，
        # 给足输出上限，避免 content 还没开始就被截断
        max_tokens=2048,
    )


def embedding_available() -> bool:
    """是否具备可用的 embedding 服务（只看配置，不发请求）。"""
    return bool(
        settings.ai_embedding_base_url.strip()
        and _looks_like_real_key(settings.ai_embedding_api_key or settings.ai_api_key)
    )


@lru_cache(maxsize=2)
def get_embeddings() -> OpenAIEmbeddings:
    if not embedding_available():
        raise MissingApiKeyError(
            "未配置 embedding 服务。请在 .env 里填 AI_EMBEDDING_BASE_URL + AI_EMBEDDING_API_KEY；"
            "若没有，可设 RAG_RETRIEVAL_MODE=bm25 走本地关键词检索。"
        )

    return OpenAIEmbeddings(
        # 注意这里必须用 embedding 专属的 base_url / key，
        # 不能复用 chat 的（DeepSeek 没有 /embeddings，会 404）
        model=settings.ai_embedding_model,
        api_key=(settings.ai_embedding_api_key or settings.ai_api_key),  # type: ignore[arg-type]
        base_url=settings.ai_embedding_base_url,
        # DashScope 的 embedding 不接受 tiktoken 切 token，必须关掉
        check_embedding_ctx_length=False,
        # 分批大小：通义 text-embedding-v3 实测单次上限 10 条
        chunk_size=settings.ai_embedding_batch_size,
        max_retries=2,
        timeout=60,
    )
