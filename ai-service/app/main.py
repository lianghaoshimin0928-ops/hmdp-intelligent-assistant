r"""hmdp ai-service 入口。

启动：
    cd ai-service
    .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000

文档：http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(
    title="hmdp 智能助手 · AI Service",
    description="LangGraph Agent + RAG，业务逻辑通过 HTTP 调用 Java 后端 hmdp",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.get("/")
def root() -> dict:
    return {
        "service": "hmdp ai-service",
        "docs": "/docs",
        "health": "/api/health",
    }
