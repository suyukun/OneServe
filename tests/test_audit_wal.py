#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计 WAL 兜底单元测试（task-002，纯 stdlib 直跑或 pytest 收集均可）。

覆盖容器级验证（verify_m0_skeleton.py AC-3）测不到的崩溃恢复分支：
模拟「已写 WAL、未写 JSONL」的中间态（任意时点被杀），断言 recover() 补齐零丢失。
被测对象 service/app/audit.py 无第三方依赖，直接导入。
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app.audit import AuditWriter, KafkaSink, make_entry  # noqa: E402

FIELDS = ["ts", "caller", "endpoint", "params_digest", "status", "latency_ms", "response_digest"]


def _entry(caller="tester"):
    return make_entry(caller=caller, endpoint="service:/v1/ping", params="[]",
                      status=200, latency_ms=3, response_body="{'ping': 'pong'}")


def _port_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """TCP 探测端口可达性（纯 stdlib，不依赖 kafka-python）。"""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def test_kafka_sink_readiness_tracks_reachability():
    """KafkaSink 为真实现（#76）：ready() 反映真实可达性，不再恒 False。

    - 负向：不可达端口（127.0.0.1:19999）→ ready() is False（环境无关，恒稳定）；
    - 正向：默认 bootstrap（127.0.0.1:9092）→ ready() 的值等于「端口是否可达」——
      先用 socket 探测可达性再断言，不硬编码期望值（Kafka 栈在跑则 True，没跑则 False）。
    """
    # ① 负向：不可达端口恒不就绪
    dead = KafkaSink(bootstrap_servers="127.0.0.1:19999")
    assert dead.ready() is False
    assert dead.send(_entry()) is False

    # ② 正向：默认 bootstrap 的就绪状态与端口可达性一致（环境自适应）
    from app.audit import KAFKA_BOOTSTRAP

    host, _, port_s = KAFKA_BOOTSTRAP.partition(":")
    reachable = _port_reachable(host, int(port_s))
    sink = KafkaSink()
    assert sink.ready() is reachable, (
        f"ready()={sink.ready()} 与端口可达性={reachable} 不一致"
        + (f"（错误：{sink._init_error}）" if not sink.ready() else ""))


def test_append_goes_through_wal_then_jsonl():
    """正常写路径：先 WAL 后 JSONL，成功后 WAL 清空；七元组字段齐备。"""
    with tempfile.TemporaryDirectory() as d:
        w = AuditWriter(d)
        e = w.append(_entry())
        lines = (Path(d) / "audit.jsonl").read_text().splitlines()
        assert len(lines) == 1
        wal = (Path(d) / "audit.wal").read_text()
        assert wal == ""  # 成功后清理
        import json as _json
        rec = _json.loads(lines[0])
        assert all(f in rec for f in FIELDS) and "seq" in rec
        assert rec["caller"] == "tester" and rec["status"] == 200
        assert e.seq == 1


def test_recover_replays_wal_only_entries():
    """崩溃恢复：WAL 有、JSONL 无的记录，recover() 补进 JSONL 并清空 WAL。"""
    with tempfile.TemporaryDirectory() as d:
        # 第一步：正常写两条（模拟崩溃前已完成的调用）
        w = AuditWriter(d)
        w.append(_entry("call-1"))
        w.append(_entry("call-2"))
        # 第二步：手工构造中间态——第 3 条只进 WAL、没进 JSONL（等价于写 WAL 后进程被杀）
        import json as _json
        orphan = _entry("call-3")
        orphan_line = _json.dumps({**vars(orphan), "seq": 3}, ensure_ascii=False)
        (Path(d) / "audit.wal").write_text(orphan_line + "\n", encoding="utf-8")
        # 第三步：新进程启动恢复
        w2 = AuditWriter(d)
        recovered = w2.recover()
        assert recovered == 1
        lines = (Path(d) / "audit.jsonl").read_text().splitlines()
        assert len(lines) == 3  # 2 条原有＋1 条 WAL 重放，零丢失
        assert _json.loads(lines[-1])["caller"] == "call-3"
        assert (Path(d) / "audit.wal").read_text() == ""  # 恢复后清理
        # 恢复后继续写，seq 不回退不冲突
        e4 = w2.append(_entry("call-4"))
        assert e4.seq == 4


def test_recover_idempotent():
    """恢复幂等：WAL 里已是 JSONL 子集时不重复补写。"""
    with tempfile.TemporaryDirectory() as d:
        w = AuditWriter(d)
        w.append(_entry("a"))
        # WAL 残留一条与 JSONL 重复的记录（seq 相同）
        import json as _json
        dup = _json.loads((Path(d) / "audit.jsonl").read_text().splitlines()[0])
        (Path(d) / "audit.wal").write_text(_json.dumps(dup) + "\n", encoding="utf-8")
        assert w.recover() == 0  # 不重复补
        assert len((Path(d) / "audit.jsonl").read_text().splitlines()) == 1


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
    print("== WAL 兜底单测全绿（" + str(len(tests)) + " 项）==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
