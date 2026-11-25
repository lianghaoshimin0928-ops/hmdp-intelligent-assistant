r"""MinerU 解析链路自检。

两种模式：

1) 默认：**完全离线**，用 httpx.MockTransport 模拟 MinerU 的四个接口，
   验证请求头、请求体、上传、轮询状态机、zip 解包、markdown 提取、本地缓存。
   不需要 token，不消耗额度。

       .\.venv\Scripts\python.exe scripts\verify_mineru.py

2) 传 --file 指定的真实 PDF：走真实 MinerU 云端解析（需要 MINERU_TOKEN）。

       .\.venv\Scripts\python.exe scripts\verify_mineru.py --file D:\some\菜单.pdf
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.rag.mineru_client import MineruClient, parse_to_markdown  # noqa: E402

PASS, FAIL = "[PASS]", "[FAIL]"
results: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    try:
        detail = fn() or ""
        results.append((name, True, str(detail)))
        print(f"{PASS} {name}  {detail}")
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"{FAIL} {name}  -> {type(exc).__name__}: {exc}")


def make_zip() -> bytes:
    """构造一个像 MinerU 输出那样的压缩包（full.md 在嵌套目录里）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("abc123/full.md", "# 菜单\n\n招牌菜：战斧牛排，288 元。\n")
        zf.writestr("abc123/layout.json", "{}")
    return buf.getvalue()


class MockBackend:
    """模拟 MinerU 服务端，记录所有请求以便断言。"""

    def __init__(self, file_name: str) -> None:
        self.file_name = file_name
        self.calls: list[tuple[str, str, dict, bytes]] = []
        self.poll_count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url, dict(request.headers), request.content))

        if request.method == "POST" and url.endswith("/file-urls/batch"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-test-1",
                        "file_urls": ["https://oss.example.com/upload/1"],
                    },
                },
            )

        if request.method == "PUT" and "oss.example.com" in url:
            return httpx.Response(200, text="ok")

        if request.method == "GET" and "/extract-results/batch/batch-test-1" in url:
            self.poll_count += 1
            if self.poll_count < 2:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "batch_id": "batch-test-1",
                            "extract_result": [
                                {
                                    "file_name": self.file_name,
                                    "state": "running",
                                    "extract_progress": {"extracted_pages": 1, "total_pages": 3},
                                }
                            ],
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": "batch-test-1",
                        "extract_result": [
                            {
                                "file_name": self.file_name,
                                "state": "done",
                                "full_zip_url": "https://cdn.example.com/result.zip",
                            }
                        ],
                    },
                },
            )

        if url.endswith("result.zip"):
            return httpx.Response(200, content=make_zip())

        return httpx.Response(404, text="unexpected request")

    def find(self, method: str, keyword: str):
        return next((c for c in self.calls if c[0] == method and keyword in c[1]), None)

    @staticmethod
    def header(call, name: str):
        """httpx 会把 header 名规范成小写，这里做大小写无关查找。"""
        return {k.lower(): v for k, v in call[2].items()}.get(name.lower())


def run_offline() -> int:
    print("=" * 68)
    print("MinerU 链路离线自检（MockTransport，不消耗额度）")
    print("=" * 68)

    tmp = Path(tempfile.mkdtemp(prefix="mineru-test-"))
    fake_pdf = tmp / "demo.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n% fake pdf for test\n")

    backend = MockBackend(file_name=fake_pdf.name)
    client = MineruClient(token="test-token", poll_interval=0, transport=httpx.MockTransport(backend.handler))

    holder: dict[str, object] = {}

    def t1():
        batch_id, urls = client.request_upload([fake_pdf])
        holder["batch_id"] = batch_id
        assert batch_id == "batch-test-1", batch_id
        assert urls == ["https://oss.example.com/upload/1"], urls
        call = backend.find("POST", "/file-urls/batch")
        assert call, "没有发出 POST /file-urls/batch"
        assert MockBackend.header(call, "Authorization") == "Bearer test-token", (
            MockBackend.header(call, "Authorization")
        )
        return f"batch_id={batch_id}"

    check("1. 申请上传链接（带 Bearer Token）", t1)

    def t2():
        import json

        call = backend.find("POST", "/file-urls/batch")
        assert call, "缺少 POST 记录"
        body = json.loads(call[3].decode("utf-8"))
        assert body["files"][0]["name"] == fake_pdf.name, body["files"]
        assert body["files"][0]["is_ocr"] is False, body["files"]
        assert body["model_version"] == settings.mineru_model_version, body["model_version"]
        for key in ("enable_formula", "enable_table", "language"):
            assert key in body, "请求体缺少字段 " + key
        return "files / model_version / enable_formula 等字段齐全"

    check("2. 请求体结构", t2)

    def t3():
        client.upload("https://oss.example.com/upload/1", fake_pdf)
        call = backend.find("PUT", "oss.example.com")
        assert call, "没有发出 PUT 上传"
        assert MockBackend.header(call, "Authorization") is None, "上传不应带 Authorization"
        return "PUT 成功且未携带 Authorization"

    check("3. 上传文件（不带鉴权头）", t3)

    def t4():
        out = client.poll("batch-test-1")
        assert len(out) == 1, out
        r = out[0]
        assert r.is_done, f"state={r.state}"
        assert r.full_zip_url == "https://cdn.example.com/result.zip", r.full_zip_url
        assert backend.poll_count >= 2, f"只轮询了 {backend.poll_count} 次，没验证到状态变化"
        return f"running → done（轮询 {backend.poll_count} 次）"

    check("4. 轮询状态机", t4)

    def t5():
        md = client.download_markdown("https://cdn.example.com/result.zip")
        assert "战斧牛排" in md, md[:80]
        return f"从嵌套 full.md 提取到 {len(md)} 字符"

    check("5. 解压 zip 并取 full.md", t5)

    def t6():
        md = client.parse(fake_pdf)
        assert "菜单" in md, md[:80]
        holder["md"] = md
        return f"端到端解析出 {len(md)} 字符"

    check("6. parse() 端到端", t6)

    def t7():
        # 写入缓存后再调 parse_to_markdown，应当直接读缓存、不发请求
        cache_dir = settings.parsed_cache_dir
        old = settings.parsed_cache_dir
        settings.parsed_cache_dir = str(tmp / "parsed")
        try:
            import hashlib

            digest = hashlib.sha1(fake_pdf.read_bytes()).hexdigest()[:20]
            cdir = Path(settings.parsed_cache_dir)
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / f"{digest}.md").write_text("# 缓存内容 ABC", encoding="utf-8")
            out = parse_to_markdown(fake_pdf)
            assert out == "# 缓存内容 ABC", out
            return "命中本地解析缓存，未发起网络请求"
        finally:
            settings.parsed_cache_dir = old

    check("7. 解析结果本地缓存", t7)

    def t8():
        from app.rag.indexer import _mineru_available

        old = settings.parser
        settings.parser = "pypdf"
        try:
            assert _mineru_available() is False, "parser=pypdf 时不应启用 MinerU"
        finally:
            settings.parser = old
        return "parser=pypdf 时自动关闭 MinerU（回退 pypdf）"

    check("8. 解析器开关与回退", t8)

    client.close()

    ok = sum(1 for _, p, _ in results if p)
    print("\n" + "=" * 68)
    print(f"{ok}/{len(results)} 通过")
    if ok == len(results):
        print("[PASS] MinerU 链路逻辑正确；填入真实 token 后即可解析 PDF")
    print("=" * 68)
    return 0 if ok == len(results) else 1


def run_real(pdf: Path) -> int:
    if not settings.mineru_token.strip():
        print("[WARN] 未配置 MINERU_TOKEN，跳过真实解析。")
        print("       去 https://mineru.net/apiManage 申请，填进 ai-service/.env 后重试。")
        return 0
    if not pdf.exists():
        print(f"[FAIL] 文件不存在: {pdf}")
        return 1

    print("=" * 68)
    print(f"真实解析: {pdf}  (model={settings.mineru_model_version})")
    print("=" * 68)
    print("提示：MinerU 云端解析通常 10~60 秒，请耐心等待...\n")
    try:
        md = parse_to_markdown(pdf, force=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        return 1

    print(f"[PASS] 解析成功，markdown 共 {len(md)} 字符\n")
    print("---- 前 800 字符 ----")
    print(md[:800])
    print("---- 结束 ----")

    parsed_dir = Path(settings.parsed_cache_dir)
    print(f"\n已缓存到: {parsed_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="MinerU 解析链路自检")
    ap.add_argument("--file", type=Path, help="真实 PDF 路径，走云端解析（需 token）")
    ap.add_argument("--offline", action="store_true", help="只跑离线自检")
    args = ap.parse_args()

    if args.file and not args.offline:
        return run_real(args.file)
    return run_offline()


if __name__ == "__main__":
    raise SystemExit(main())
