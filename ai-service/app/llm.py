"""大模型工厂：统一从这里取 ChatModel / Embeddings，方便测试时替换成假模型。"""

from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.config import settings


class MissingApiKeyError(RuntimeError):
    pass


def _require_key() -> None:
    if not settings.ai_api_key or settings.ai_api_key.startswith("sk-你的"):
        raise MissingApiKeyError(
            "未配置 AI_API_KEY。请在 ai-service/.env 里填入真实 key，"
            "例如阿里百炼：https://bailian.console.aliyun.com/"
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
    )


@lru_cache(maxsize=2)
def get_embeddings() -> OpenAIEmbeddings:
    _require_key()
    return OpenAIEmbeddings(
        model=settings.ai_embedding_model,
        api_key=settings.ai_api_key,          # type: ignore[arg-type]
        base_url=settings.ai_base_url,
        # 关键：DashScope 的 embedding 不接受 tiktoken 切 token，必须关掉
        check_embedding_ctx_length=False,
    )
