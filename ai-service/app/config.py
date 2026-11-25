"""全局配置。所有配置项都可通过 .env 覆盖，不要硬编码密钥到代码里。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 大模型 ----------
    ai_api_key: str = ""
    ai_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ai_model: str = "qwen-plus"
    ai_embedding_model: str = "text-embedding-v3"
    ai_temperature: float = 0.3

    # ---------- Java 业务后端 ----------
    hmdp_base_url: str = "http://127.0.0.1:8081"
    hmdp_phone: str = "13800000000"
    hmdp_token: str = ""

    # ---------- Redis ----------
    redis_url: str = "redis://:123456@127.0.0.1:6379/1"

    # ---------- 向量库 ----------
    chroma_dir: str = "./data/chroma"
    rag_chunk_size: int = 500
    rag_chunk_overlap: int = 80
    rag_top_k: int = 20          # 第一阶段：向量召回
    rag_rerank_top_n: int = 5    # 第二阶段：精排后送给模型

    # ---------- 服务 ----------
    ai_service_port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
