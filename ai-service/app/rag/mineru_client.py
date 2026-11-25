r"""MinerU 云端文档解析客户端。

官方文档：https://mineru.net/apiManage/docs

三步流程（对应官方 v4 接口）：
    1. POST /file-urls/batch                     申请上传链接 → 拿 batch_id + file_urls
    2. PUT  <file_url>                           把文件字节传上去（这一步**不带** Authorization）
    3. GET  /extract-results/batch/{batch_id}    轮询状态，done 后从 full_zip_url 下 zip，取 full.md

设计要点（面试可讲）：
- 解析结果按 **文件内容哈希** 缓存到 data/parsed/，重复建索引不会重复消耗解析额度；
- 轮询有超时与失败快速退出，不会把整个索引任务卡死；
- MinerU 不可用时由上层回退到 pypdf，保证「不联网也能跑」。

限制（官方）：单批 ≤200 个文件、单文件 ≤200MB、≤600 页。
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# MinerU 支持的输入格式
SUPPORTED_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp",
    ".html",
}

# 解析中/等待中的状态
_PENDING_STATES = {"waiting-file", "pending", "running", "converting"}


class MineruError(RuntimeError):
    """MinerU 调用失败。"""


@dataclass
class MineruFileResult:
    file_name: str
    state: str
    full_zip_url: str | None = None
    err_msg: str = ""

    @property
    def is_done(self) -> bool:
        return self.state == "done" and bool(self.full_zip_url)

    @property
    def is_failed(self) -> bool:
        return self.state == "failed"


@dataclass
class MineruClient:
    token: str = field(default_factory=lambda: settings.mineru_token)
    base_url: str = field(default_factory=lambda: settings.mineru_base_url.rstrip("/"))
    model_version: str = field(default_factory=lambda: settings.mineru_model_version)
    poll_interval: int = field(default_factory=lambda: settings.mineru_poll_interval)
    timeout: int = field(default_factory=lambda: settings.mineru_timeout)
    transport: httpx.BaseTransport | None = None  # 测试注入用

    _http: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = httpx.Client(timeout=60.0, transport=self.transport)

    # ------------------------------------------------------------------ 基础
    @property
    def enabled(self) -> bool:
        return bool(self.token and self.token.strip())

    def _json_headers(self) -> dict[str, str]:
        if not self.enabled:
            raise MineruError(
                "未配置 MINERU_TOKEN。请到 https://mineru.net/apiManage 申请后填进 .env"
            )
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "*/*"}

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ 1. 申请上传链接
    def request_upload(self, paths: list[Path]) -> tuple[str, list[str]]:
        files = []
        for p in paths:
            if p.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise MineruError(f"不支持的文件类型: {p.name}")
            files.append(
                {
                    "name": p.name,
                    "is_ocr": settings.mineru_enable_ocr,
                    "data_id": hashlib.sha1(p.name.encode("utf-8")).hexdigest()[:16],
                }
            )

        payload = {
            "files": files,
            "model_version": self.model_version,
            "enable_formula": settings.mineru_enable_formula,
            "enable_table": settings.mineru_enable_table,
            "language": settings.mineru_language,
        }

        url = f"{self.base_url}/file-urls/batch"
        logger.info("MinerU 申请上传链接: %s 个文件, model=%s", len(files), self.model_version)
        resp = self._http.post(url, headers=self._json_headers(), json=payload)
        data = self._parse_json(resp, "file-urls/batch")

        if data.get("code") != 0:
            raise MineruError(f"申请上传链接失败: code={data.get('code')} msg={data.get('msg')}")

        body = data.get("data") or {}
        batch_id = body.get("batch_id")
        urls = body.get("file_urls") or []
        if not batch_id or len(urls) != len(paths):
            raise MineruError(f"返回结构异常: batch_id={batch_id}, urls={len(urls)}")
        return batch_id, urls

    def _parse_json(self, resp: httpx.Response, api: str) -> dict:
        if resp.status_code != 200:
            raise MineruError(f"{api} HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise MineruError(f"{api} 返回非 JSON: {resp.text[:200]}") from exc

    # ------------------------------------------------------------------ 2. 上传
    def upload(self, upload_url: str, path: Path) -> None:
        # 上传到 OSS 预签名地址，不能带 Authorization，否则签名校验失败
        resp = self._http.put(upload_url, content=path.read_bytes())
        if resp.status_code not in (200, 201):
            raise MineruError(f"上传 {path.name} 失败: HTTP {resp.status_code} {resp.text[:200]}")
        logger.info("已上传 %s (%d 字节)", path.name, path.stat().st_size)

    # ------------------------------------------------------------------ 3. 轮询
    def poll(self, batch_id: str) -> list[MineruFileResult]:
        url = f"{self.base_url}/extract-results/batch/{batch_id}"
        deadline = time.monotonic() + self.timeout
        last_states: dict[str, str] = {}

        while True:
            resp = self._http.get(url, headers=self._auth_headers())
            data = self._parse_json(resp, "extract-results/batch")
            if data.get("code") != 0:
                raise MineruError(f"查询结果失败: code={data.get('code')} msg={data.get('msg')}")

            items = (data.get("data") or {}).get("extract_result") or []
            results = [
                MineruFileResult(
                    file_name=it.get("file_name", ""),
                    state=it.get("state", ""),
                    full_zip_url=it.get("full_zip_url"),
                    err_msg=it.get("err_msg", "") or "",
                )
                for it in items
            ]

            for r in results:
                if last_states.get(r.file_name) != r.state:
                    progress = next(
                        (it.get("extract_progress") for it in items if it.get("file_name") == r.file_name),
                        None,
                    )
                    extra = ""
                    if progress:
                        extra = f" ({progress.get('extracted_pages')}/{progress.get('total_pages')} 页)"
                    logger.info("MinerU [%s] 状态=%s%s", r.file_name, r.state, extra)
                    last_states[r.file_name] = r.state

            if any(r.is_failed for r in results):
                failed = next(r for r in results if r.is_failed)
                raise MineruError(f"MinerU 解析失败 [{failed.file_name}]: {failed.err_msg or '未知原因'}")

            if results and all(r.is_done for r in results):
                return results

            if time.monotonic() > deadline:
                states = ", ".join(f"{r.file_name}={r.state}" for r in results) or "无结果"
                raise MineruError(f"MinerU 解析超时（{self.timeout}s），当前状态: {states}")

            time.sleep(self.poll_interval)

    # ------------------------------------------------------------------ 4. 下载并提取 markdown
    def download_markdown(self, zip_url: str) -> str:
        resp = self._http.get(zip_url)
        if resp.status_code != 200:
            raise MineruError(f"下载解析结果失败: HTTP {resp.status_code}")

        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                names = zf.namelist()
                md_name = next(
                    (n for n in names if n.endswith("full.md")),
                    next((n for n in names if n.endswith(".md")), None),
                )
                if not md_name:
                    raise MineruError(f"压缩包里没有 markdown，包含: {names[:10]}")
                return zf.read(md_name).decode("utf-8", errors="replace")
        except zipfile.BadZipFile as exc:
            raise MineruError("解析结果不是合法 zip") from exc

    # ------------------------------------------------------------------ 一步到位
    def parse(self, path: Path) -> str:
        """单个文件：申请 → 上传 → 轮询 → 下载 → 返回 markdown。"""
        batch_id, urls = self.request_upload([path])
        self.upload(urls[0], path)
        results = self.poll(batch_id)
        target = next((r for r in results if r.file_name == path.name), results[0])
        if not target.full_zip_url:
            raise MineruError(f"{path.name} 没有返回 full_zip_url")
        md = self.download_markdown(target.full_zip_url)
        logger.info("%s 解析完成，markdown %d 字符", path.name, len(md))
        return md


def parse_to_markdown(path: Path, cache_dir: Path | None = None, force: bool = False) -> str:
    """带本地缓存的解析入口：按文件内容哈希缓存，避免重复消耗 MinerU 额度。"""
    cache_root = cache_dir or Path(settings.parsed_cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha1(path.read_bytes()).hexdigest()[:20]
    cache_file = cache_root / f"{digest}.md"

    if cache_file.exists() and not force:
        logger.info("%s 命中解析缓存: %s", path.name, cache_file.name)
        return cache_file.read_text(encoding="utf-8")

    client = MineruClient()
    try:
        md = client.parse(path)
    finally:
        client.close()

    cache_file.write_text(md, encoding="utf-8")
    logger.info("%s 解析结果已缓存: %s", path.name, cache_file.name)
    return md
