#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计异步刷盘性能对比测试（SLA试测_v1 §2-②：审计异步化 M1 项）。

同一 AuditWriter、同一写入口径（500 条 append），仅 fsync 策略不同：
  同步版 flush_interval=0   ：append 内逐条 fsync（旧语义，掉电级持久）
  异步版 flush_interval=0.2 ：后台线程批量 fsync（默认配置）
断言：
  1) 异步版 500 条墙钟时间较同步版显著下降（< 0.7×，实测通常低一个数量级）；
  2) 异步版 close() 后 500 条全在 JSONL、WAL 清空、seq 连续——性能不以丢记录为代价。
运行：python3 -m pytest tests/test_audit_wal_perf.py -s（-s 显示两版实测数字）
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app.audit import AuditWriter, make_entry  # noqa: E402

N = 500              # 对比写入条数（任务书口径）
ASYNC_INTERVAL = 0.2  # 异步版刷盘间隔（= 默认值 200ms）


def _entry(caller: str):
    return make_entry(caller=caller, endpoint="service:/v1/ping", params="[]",
                      status=200, latency_ms=3, response_body="{'ping': 'pong'}")


def _write_n(audit_dir: str, flush_interval: float) -> float:
    """写 N 条审计并返回纯 append 墙钟时间（close 的退出 flush 不计入，两版口径一致）。"""
    w = AuditWriter(audit_dir, flush_interval=flush_interval)
    t0 = time.perf_counter()
    for i in range(N):
        w.append(_entry(f"perf-{i}"))
    dt = time.perf_counter() - t0
    w.close()  # 退出兜底 flush（异步版保证数据落物理盘）
    return dt


def test_async_fsync_beats_sync_500_writes():
    """500 条写入：异步批量 fsync 较同步逐条 fsync 显著提速，且零丢失。"""
    with tempfile.TemporaryDirectory() as ds, tempfile.TemporaryDirectory() as da:
        t_sync = _write_n(ds, 0)                    # 同步版：逐条 fsync
        t_async = _write_n(da, ASYNC_INTERVAL)      # 异步版：200ms 批量 fsync

        # 异步版正确性：close 后 N 条全在 JSONL、顺序/seq 连续、WAL 清空
        lines = (Path(da) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == N, f"异步版应写满 {N} 条，实际 {len(lines)}"
        assert json.loads(lines[0])["caller"] == "perf-0"
        assert json.loads(lines[-1])["caller"] == f"perf-{N - 1}"
        assert [json.loads(x)["seq"] for x in lines[:3]] == [1, 2, 3]
        assert (Path(da) / "audit.wal").read_text(encoding="utf-8") == ""

        # 两版数字（回执证据）：打印总墙钟＋单条均摊
        print(f"[perf] N={N} 同步逐条fsync: {t_sync * 1000:.1f}ms "
              f"({t_sync / N * 1000:.3f}ms/条)")
        print(f"[perf] N={N} 异步批量({int(ASYNC_INTERVAL * 1000)}ms): {t_async * 1000:.1f}ms "
              f"({t_async / N * 1000:.3f}ms/条) 加速比 {t_sync / t_async:.1f}x")

        # 性能断言：显著下降（阈值 0.7×；留余量防计时抖动，实测远低于此）
        assert t_async < t_sync * 0.7, (
            f"异步版未显著快于同步版：sync={t_sync * 1000:.1f}ms "
            f"async={t_async * 1000:.1f}ms")


def test_sync_mode_keeps_old_durability_semantics():
    """flush_interval=0 兜底开关：恢复旧「逐条 fsync」同步口径，行为与异步版对外一致。"""
    with tempfile.TemporaryDirectory() as d:
        w = AuditWriter(d, flush_interval=0)
        e = w.append(_entry("sync-mode"))
        w.close()
        lines = (Path(d) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1 and e.seq == 1
        assert (Path(d) / "audit.wal").read_text(encoding="utf-8") == ""


def test_sigkill_zero_loss_without_any_flush():
    """异步模式下进程「硬死」（无 atexit/无 close/无 fsync）零丢失实证。

    子进程用 os._exit(137) 立刻蒸发（跳过一切退出钩子，等价于 SIGKILL 后的残局）：
    已 append 的记录因 write() 已入内核页缓存而全部可见，父进程 recover() 后零丢失。
    这是「write 内联＋fsync 异步」不改变对外语义的硬证据。
    """
    import os as _os
    import subprocess
    code = (
        "import os, sys; sys.path.insert(0, 'service')\n"
        "from app.audit import AuditWriter, make_entry\n"
        "w = AuditWriter(sys.argv[1])  # 默认异步模式（200ms 批量 fsync）\n"
        "for i in range(300):\n"
        "    w.append(make_entry(caller=f'kill-{i}', endpoint='service:/v1/ping',\n"
        "             params='[]', status=200, latency_ms=3,\n"
        "             response_body='pong'))\n"
        "os._exit(137)  # 硬退：不触发 atexit/close/final-fsync，模拟被 SIGKILL\n"
    )
    with tempfile.TemporaryDirectory() as d:
        p = subprocess.run([sys.executable, "-c", code, d], cwd=str(
            Path(__file__).resolve().parents[1]), capture_output=True)
        assert p.returncode == 137, f"子进程应硬退 137，实际 {p.returncode}"
        w2 = AuditWriter(d)      # 新进程启动恢复（同 service lifespan 的 recover 调用）
        recovered = w2.recover()
        w2.close()
        lines = (Path(d) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 300, f"硬死后应零丢失 300 条，实际 {len(lines)}（WAL 补回 {recovered}）"
        assert json.loads(lines[-1])["caller"] == "kill-299"
        assert (Path(d) / "audit.wal").read_text(encoding="utf-8") == ""
        print(f"[kill] 硬退后 JSONL={len(lines)} 条（WAL 补回 {recovered}），零丢失 ✓")


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("  ✓ " + name)
        except AssertionError as exc:
            print("  ✗ " + name + "：" + str(exc))
            failed.append(name)
    if failed:
        print("== " + str(len(failed)) + " 项失败 ==")
        return 1
    print("== 审计异步刷盘性能测试全绿（" + str(len(tests)) + " 项）==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
