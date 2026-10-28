#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量任务级审计接线防回潮（batch-million-20261015 演练发现的真实缺陷）。

缺陷（2026-10-15 百万键演练实证）：run_forever→drain→process_job 调用链两处
漏传 audit 参数 → FIFO 正常消费路径的任务级终态审计（M15-5/AC-8 后半）从未
生效——真实栈 /data/audit/batch/audit.jsonl 恒 0 字节（昨夜 50/50 与今日百万键
任务均零条），而既有 12 条审计测试全部直调 process_job(audit=...)，接线层无覆盖。

修复：batch_worker.py 三处接线补 audit（drain→process_job、run_forever→drain、
recover_running→process_job/finalize_cancel）。本套件两层防回潮：
  行为级：drain(audit=writer) 跑真 FIFO 消费 → 终态审计恰好一条；
  机检级：AST 断言四条调用链 audit=audit 关键字在位（接线层回归即红）。
"""

import ast
import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
sys.path.insert(0, str(REPO / "tests"))

from app import batch                                    # noqa: E402
from app import batch_worker as w                        # noqa: E402
from app.audit import AuditWriter                        # noqa: E402
from test_batch_audit import (FakeAuditRedis, SERVICES, MATRIX,   # noqa: E402
                              APPKEY, KEYS, seed_kv, put_user, read_entries)


def test_drain_fifo_wire_lands_task_finish_audit():
    """行为级：drain 走 FIFO 出队消费，audit 注入须落任务级终态审计一条。

    这是修复前红/修复后绿的核心断言（漏传时 drain 静默吞掉 audit 参数，
    终态审计零条且无任何报错——缺陷恰因此长期潜伏）。
    """
    st = FakeAuditRedis()
    seed_kv(st)
    put_user(st, KEYS[0], {"ac_active_sts_01": "1"})
    body, status = asyncio.run(batch.submit_job(
        st, SERVICES, MATRIX, APPKEY, {"keys": KEYS}))
    assert status == 202, body
    jid = body["job_id"]
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as rd:
        writer = AuditWriter(td, flush_interval=0)
        done = asyncio.run(w.drain(st, SERVICES, MATRIX,
                                   results_dir=rd, audit=writer))
        assert done == [jid]
        job = asyncio.run(batch.JobStore(st).get(jid))
        assert job["status"] == "succeeded"
        entries = read_entries(td)
        assert len(entries) == 1, "FIFO 消费路径终态审计恰好一条（漏传 audit=零条）"
        e = entries[0]
        assert e["endpoint"] == batch.AUDIT_JOB_FINISH_ENDPOINT
        assert e["status"] == 200 and e["caller"] == APPKEY and e["trace_id"]
        writer.close()


def _calls_in(fn_name, wanted):
    """AST：在 batch_worker.<fn_name> 函数体内找对 wanted 函数的调用，返回 kwargs 名集列表。"""
    tree = ast.parse((REPO / "service" / "app" / "batch_worker.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == fn_name)   # worker 侧均为 async def
    return [{k.arg for k in c.keywords}
            for c in ast.walk(fn)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name) and c.func.id == wanted]


def test_worker_wiring_passes_audit_keyword_on_all_paths():
    """机检级：四条调用链 audit=audit 关键字在位（接线层回归即红，防再漏传）。"""
    for fn_name, callee in [
            ("run_forever", "drain"),            # 修复点 2：主循环→FIFO 消费
            ("drain", "process_job"),            # 修复点 1：FIFO 消费→单任务流水线
            ("recover_running", "process_job"),  # 同族：启动恢复→续跑
            ("recover_running", "finalize_cancel")]:  # 同族：启动恢复→取消收尾
        calls = _calls_in(fn_name, callee)
        assert calls, f"{fn_name} 内未找到对 {callee} 的调用（接线被删？）"
        assert all("audit" in kw for kw in calls), (
            f"{fn_name}→{callee} 调用须带 audit= 关键字（"
            f"漏传＝任务级终态审计静默丢失，2026-10-15 百万键演练实证）")


def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== 批量审计接线防回潮（{len(tests)} 条）==")
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as exc:
            print(f"  ✗ {name}：{exc}")
            failed.append(name)
    if failed:
        print(f"== 未过：{len(tests) - len(failed)}/{len(tests)} ==")
        return 1
    print(f"== 全绿：{len(tests)}/{len(tests)} ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
