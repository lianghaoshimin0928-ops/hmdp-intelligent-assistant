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
    rag_top_k: int = 20          # 第一阶段：召回
    rag_rerank_top_n: int = 5    # 第二阶段：精排后送给模型

    # auto  : 配了 AI_EMBEDDING_BASE_URL 就走向量，否则用 BM25
    # vector: 强制向量检索（需要能用的 embedding 服务）
    # bm25  : 强制 BM25 关键词检索（纯本地、零依赖）
    rag_retrieval_mode: str = "auto"

    # ---------- Embedding（注意：DeepSeek 不提供 embedding 接口）----------
    # 留空则不支持向量检索，自动回退 BM25。
    # 阿里百炼：https://dashscope.aliyuncs.com/compatible-mode/v1  + text-embedding-v3
    ai_embedding_api_key: str = ""
    ai_embedding_base_url: str = ""

    # ---------- 文档解析 ----------
    # auto   : 有 MinerU token 就用 MinerU 解析 PDF/Office/图片，否则回退 pypdf
    # mineru : 强制用 MinerU（没 token 直接报错）
    # pypdf  : 强制只用 pypdf 抽文本，不联网
    parser: str = "auto"
    parsed_cache_dir: str = "./data/parsed"

    # ---------- MinerU 云端解析 ----------
    mineru_token: str = ""
    mineru_base_url: str = "https://mineru.net/api/v4"
    # pipeline（快、便宜）/ vlm（效果更好，适合复杂版式与扫描件）
    mineru_model_version: str = "vlm"
    mineru_enable_ocr: bool = False
    mineru_enable_formula: bool = True
    mineru_enable_table: bool = True
    mineru_language: str = "ch"
    mineru_poll_interval: int = 5     # 轮询间隔（秒）
    mineru_timeout: int = 600         # 单个文件解析超时（秒）

    # ---------- 服务 ----------
    ai_service_port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
