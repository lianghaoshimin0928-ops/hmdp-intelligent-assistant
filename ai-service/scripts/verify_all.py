r"""一条命令验证「环境 + 图逻辑」，全部用例都不消耗大模型 token。

    cd ai-service
    .\.venv\Scripts\python.exe scripts\verify_all.py

覆盖：
1. 所有模块可导入
2. LangGraph 图能编译
3. seckill 意图会 **中断**（interrupt），且中断前**不会**调用写接口
4. 恢复（Command(resume=确认)）后才真正调用写接口
5. 恢复时回复「取消」走 cancelled 分支，不调用写接口
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402

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
        traceback.print_exc()


# ---------------------------------------------------------------- 假后端
class FakeHmdpClient:
    """假的 Java 后端：只记录被调用次数，不真的发 HTTP。"""

    def __init__(self) -> None:
        self.seckill_calls = 0
        self.lookup_calls = 0

    def list_shops_by_name(self, keyword, current=1):
        from app.clients.hmdp import HmdpResult

        self.lookup_calls += 1
        return HmdpResult(ok=True, data=[{"id": 1, "name": f"{keyword}店", "avgPrice": 45}])

    def get_shop(self, shop_id):
        from app.clients.hmdp import HmdpResult

        self.lookup_calls += 1
        return HmdpResult(ok=True, data={"id": shop_id, "name": "测试店"})

    def list_vouchers(self, shop_id):
        from app.clients.hmdp import HmdpResult

        self.lookup_calls += 1
        return HmdpResult(ok=True, data=[{"id": 7, "title": "满100减20", "stock": 8}])

    def seckill(self, voucher_id):
        from app.clients.hmdp import HmdpResult

        self.seckill_calls += 1
        return HmdpResult(ok=True, data={"orderId": 20260101, "voucherId": voucher_id})


def main() -> int:
    import app.clients.hmdp as hmdp_module
    from app.graph.build import build_assistant_graph

    fake = FakeHmdpClient()
    hmdp_module.client = fake  # 工具里取的是模块级 client

    check("1. 模块导入", lambda: "ok")

    graph_holder = {}

    def build():
        graph_holder["g"] = build_assistant_graph(llm=None)
        return "compiled"

    check("2. 图编译", build)
    graph = graph_holder.get("g")
    if graph is None:
        return 1

    cfg = {"configurable": {"thread_id": "t-confirm"}}

    # ---- 第一次调用：应该中断 ----
    first = graph.invoke(
        {
            "intent": "seckill",
            "voucher_id": 7,
            "messages": [HumanMessage(content="帮我抢7号券")],
        },
        cfg,
    )

    def assert_interrupted():
        assert "__interrupt__" in first, f"没有中断，实际返回 {list(first.keys())}"
        payload = first["__interrupt__"][0].value
        assert payload.get("voucher_id") == 7, f"中断载荷不对: {payload}"
        assert fake.seckill_calls == 0, "中断前就调用了写接口，权限控制失效！"
        return f"interrupt payload={payload}"

    check("3. 抢券意图触发 interrupt", assert_interrupted)

    def assert_next_is_confirm():
        snapshot = graph.get_state(cfg)
        assert snapshot.next, "checkpointer 里没有待执行节点"
        return f"next={snapshot.next}"

    check("4. checkpointer 保存了待执行节点", assert_next_is_confirm)

    # ---- 恢复：确认 ----
    second = graph.invoke(Command(resume="确认"), cfg)

    def assert_resumed():
        assert "__interrupt__" not in second, "恢复后仍处于中断状态"
        assert fake.seckill_calls == 1, f"应调用 1 次写接口，实际 {fake.seckill_calls}"
        assert "orderId" in str(second.get("answer", "")), f"回答异常: {second.get('answer')}"
        return f"answer={second.get('answer')}"

    check("5. 恢复后执行写操作", assert_resumed)

    # ---- 取消分支 ----
    cfg2 = {"configurable": {"thread_id": "t-cancel"}}
    graph.invoke(
        {"intent": "seckill", "voucher_id": 9, "messages": [HumanMessage(content="抢9号券")]},
        cfg2,
    )
    before = fake.seckill_calls
    third = graph.invoke(Command(resume="算了，取消"), cfg2)

    def assert_cancelled():
        assert fake.seckill_calls == before, "取消分支居然调用了写接口"
        assert "取消" in str(third.get("answer", "")), f"回答异常: {third.get('answer')}"
        return f"answer={third.get('answer')}"

    check("6. 取消分支不执行写操作", assert_cancelled)

    # ---- 工具本身可调用（不经过图）----
    def assert_tool():
        from app.tools.hmdp_tools import list_vouchers, query_shop

        out = query_shop.invoke({"keyword": "火锅"})
        assert "火锅店" in out, out
        v = list_vouchers.invoke({"shop_id": 1})
        assert "满100减20" in v or "id" in v, v
        return "查询类工具可正常调用"

    check("7. 查询工具可用", assert_tool)

    ok = sum(1 for _, p, _ in results if p)
    total = len(results)
    print(f"\n===== {ok}/{total} 通过 =====")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
