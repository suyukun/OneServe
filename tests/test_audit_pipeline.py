#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计管线 KafkaSink＋双写验证测试套件（#76 Phase 1）。

分层：
  - 行为级：KafkaSink mock 投递、dual 模式双写、local 降级、pipeline_mode 配置；
  - AST 防回潮：断言 AuditWriter.append 内 kafka/dual 分支关键字在位；
  - 对账脚本：reconcile 函数纯逻辑测试（mock KafkaConsumer）。

Kafka 未起时：KafkaSink 连接失败→ready()=False→自动降级，测试仍绿；
              仅 test_kafka_sink_live_* 标记 skip_if_no_kafka。
"""

import ast
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
sys.path.insert(0, str(REPO))  # 使 scripts.audit_reconcile 可导入

from app.audit import (  # noqa: E402
    AuditEntry, AuditWriter, KafkaSink, LocalSink, make_entry,
    PIPELINE_MODE, KAFKA_TOPIC,
)


# ---------- 辅助 ----------

def _make_entry(**kw) -> AuditEntry:
    """快捷构造审计条目（默认值覆盖）。"""
    defaults = dict(caller="test-app", endpoint="/v1/ping", params="{}",
                    status=200, latency_ms=5, response_body="ok")
    defaults.update(kw)
    return make_entry(**defaults)


def _read_jsonl(path: Path) -> list[dict]:
    """读 JSONL 文件返回记录列表。"""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _kafka_available() -> bool:
    """检测 Kafka 是否可达（127.0.0.1:9092）。"""
    try:
        from kafka import KafkaProducer
        p = KafkaProducer(bootstrap_servers="127.0.0.1:9092",
                          request_timeout_ms=2000, max_block_ms=2000)
        p.partitions_for(KAFKA_TOPIC)
        p.close()
        return True
    except Exception:
        return False


skip_if_no_kafka = pytest.mark.skipif(
    not _kafka_available(),
    reason="Kafka 未起（127.0.0.1:9092 不可达），跳过 live 测试")


# ========== 行为级测试 ==========

class TestLocalSink:
    """LocalSink 恒不就绪，append 走本地 WAL+JSONL。"""

    def test_local_sink_never_ready(self):
        ls = LocalSink()
        assert ls.ready() is False
        assert ls.send(_make_entry()) is False

    def test_local_sink_close_noop(self):
        ls = LocalSink()
        ls.close()  # 不应抛异常


class TestKafkaSinkMock:
    """KafkaSink mock 测试（不依赖真实 Kafka）。"""

    def test_kafka_sink_connect_failure_degrades(self):
        """Kafka 不可达时 ready()=False，不抛异常。"""
        ks = KafkaSink(bootstrap_servers="127.0.0.1:19999")  # 不存在的端口
        assert ks.ready() is False
        assert ks._init_error is not None

    def test_kafka_sink_send_when_not_ready(self):
        """未就绪时 send() 返回 False。"""
        ks = KafkaSink(bootstrap_servers="127.0.0.1:19999")
        assert ks.send(_make_entry()) is False

    def test_kafka_sink_mock_producer_success(self):
        """mock producer 成功投递。"""
        ks = KafkaSink.__new__(KafkaSink)
        ks._bootstrap = "mock"
        ks._topic = "test-topic"
        ks._ready = True
        ks._init_error = None
        mock_future = MagicMock()
        mock_future.get.return_value = None
        mock_producer = MagicMock()
        mock_producer.send.return_value = mock_future
        ks._producer = mock_producer

        assert ks.ready() is True
        assert ks.send(_make_entry()) is True
        mock_producer.send.assert_called_once()

    def test_kafka_sink_mock_producer_failure(self):
        """mock producer 投递失败返回 False。"""
        ks = KafkaSink.__new__(KafkaSink)
        ks._bootstrap = "mock"
        ks._topic = "test-topic"
        ks._ready = True
        ks._init_error = None
        mock_producer = MagicMock()
        mock_producer.send.side_effect = RuntimeError("send failed")
        ks._producer = mock_producer

        assert ks.send(_make_entry()) is False

    def test_kafka_sink_close_flushes(self):
        """close() 调用 flush + close。"""
        ks = KafkaSink.__new__(KafkaSink)
        mock_prod = MagicMock()
        ks._producer = mock_prod
        ks._ready = True
        ks.close()
        mock_prod.flush.assert_called_once()
        mock_prod.close.assert_called_once()
        assert ks._ready is False


class TestAuditWriterPipelineModes:
    """AuditWriter 三种管线模式行为验证。"""

    def test_local_mode_writes_jsonl(self):
        """local 模式：写入 JSONL，不走 Kafka。"""
        with tempfile.TemporaryDirectory() as td:
            w = AuditWriter(td, flush_interval=0, pipeline_mode="local")
            assert w.pipeline_mode == "local"
            e = w.append(_make_entry())
            assert e.seq == 1
            w.close()
            records = _read_jsonl(w.jsonl)
            assert len(records) == 1
            assert records[0]["caller"] == "test-app"

    def test_kafka_mode_degrades_to_local_when_unavailable(self):
        """kafka 模式 + Kafka 不可达 → 降级写本地 JSONL。"""
        with tempfile.TemporaryDirectory() as td:
            sink = KafkaSink(bootstrap_servers="127.0.0.1:19999")
            w = AuditWriter(td, sink=sink, flush_interval=0, pipeline_mode="kafka")
            assert w.pipeline_mode == "kafka"
            e = w.append(_make_entry())
            assert e.seq == 1
            w.close()
            records = _read_jsonl(w.jsonl)
            assert len(records) == 1, "Kafka 不可达时应降级写本地 JSONL"

    def test_dual_mode_writes_both_jsonl_and_kafka(self):
        """dual 模式：无论 Kafka 成败，JSONL 必写。"""
        with tempfile.TemporaryDirectory() as td:
            # mock sink 成功
            mock_sink = MagicMock()
            mock_sink.ready.return_value = True
            mock_sink.send.return_value = True
            mock_sink.close = MagicMock()

            w = AuditWriter(td, sink=mock_sink, flush_interval=0, pipeline_mode="dual")
            assert w.pipeline_mode == "dual"
            e = w.append(_make_entry())
            w.close()

            # JSONL 必写（dual 语义）
            records = _read_jsonl(w.jsonl)
            assert len(records) == 1
            # Kafka 也被调用
            mock_sink.send.assert_called_once()

    def test_dual_mode_writes_jsonl_even_when_kafka_fails(self):
        """dual 模式 + Kafka 失败 → JSONL 仍写。"""
        with tempfile.TemporaryDirectory() as td:
            mock_sink = MagicMock()
            mock_sink.ready.return_value = True
            mock_sink.send.return_value = False  # Kafka 投递失败
            mock_sink.close = MagicMock()

            w = AuditWriter(td, sink=mock_sink, flush_interval=0, pipeline_mode="dual")
            e = w.append(_make_entry())
            w.close()

            records = _read_jsonl(w.jsonl)
            assert len(records) == 1, "dual 模式 Kafka 失败时 JSONL 仍须写入"

    def test_kafka_mode_single_write_on_success(self):
        """kafka 模式 + Kafka 成功 → 不落本地 JSONL。"""
        with tempfile.TemporaryDirectory() as td:
            mock_sink = MagicMock()
            mock_sink.ready.return_value = True
            mock_sink.send.return_value = True
            mock_sink.close = MagicMock()

            w = AuditWriter(td, sink=mock_sink, flush_interval=0, pipeline_mode="kafka")
            e = w.append(_make_entry())
            w.close()

            records = _read_jsonl(w.jsonl)
            assert len(records) == 0, "kafka 单写模式成功时不应落本地 JSONL"

    def test_invalid_pipeline_mode_defaults_to_local(self):
        """非法 pipeline_mode 回退 local。"""
        with tempfile.TemporaryDirectory() as td:
            w = AuditWriter(td, flush_interval=0, pipeline_mode="invalid")
            assert w.pipeline_mode == "local"
            w.close()

    def test_close_closes_kafka_sink(self):
        """AuditWriter.close() 也关闭 KafkaSink。"""
        with tempfile.TemporaryDirectory() as td:
            mock_sink = MagicMock()
            mock_sink.ready.return_value = False
            mock_sink.close = MagicMock()
            w = AuditWriter(td, sink=mock_sink, flush_interval=0, pipeline_mode="dual")
            w.close()
            mock_sink.close.assert_called_once()


class TestAuditEntrySerialization:
    """审计条目序列化完整性。"""

    def test_entry_to_json_line_contains_all_fields(self):
        """to_json_line 包含全部八元组字段＋seq。"""
        e = _make_entry(trace_id="trace-abc")
        e = AuditEntry(**{**e.__dict__, "seq": 42})
        line = e.to_json_line()
        obj = json.loads(line)
        for key in ("ts", "caller", "endpoint", "params_digest", "status",
                     "latency_ms", "response_digest", "trace_id", "seq"):
            assert key in obj, f"缺少字段 {key}"
        assert obj["seq"] == 42
        assert obj["trace_id"] == "trace-abc"


# ========== AST 防回潮测试 ==========

class TestASTAntiRegression:
    """AST 断言管线模式分支关键字在位（接线层回归即红）。"""

    @staticmethod
    def _parse_audit_py() -> ast.Module:
        return ast.parse((REPO / "service" / "app" / "audit.py").read_text(encoding="utf-8"))

    def test_append_has_pipeline_mode_check(self):
        """append() 内须有 pipeline_mode 分支判断（防删改回纯 local）。"""
        tree = self._parse_audit_py()
        append_fn = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "append":
                    append_fn = node
                    break
        assert append_fn is not None, "AuditWriter.append 方法不存在"

        # 检查函数体内是否有 'pipeline_mode' 属性访问
        source = ast.get_source_segment(
            (REPO / "service" / "app" / "audit.py").read_text(encoding="utf-8"),
            append_fn)
        assert "pipeline_mode" in source,             "append() 内须有 pipeline_mode 分支（防回潮：删除管线模式判断＝静默退回纯 local）"

    def test_append_has_dual_branch(self):
        """append() 内须有 'dual' 字面量（双写分支存在性）。"""
        tree = self._parse_audit_py()
        append_fn = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "append":
                    append_fn = node
                    break
        source = ast.get_source_segment(
            (REPO / "service" / "app" / "audit.py").read_text(encoding="utf-8"),
            append_fn)
        assert '"dual"' in source or "'dual'" in source,             "append() 内须有 dual 分支字面量（防回潮：删除双写模式）"

    def test_kafka_sink_class_exists(self):
        """KafkaSink 类须存在且非 stub（有 _try_connect 方法）。"""
        tree = self._parse_audit_py()
        classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        assert "KafkaSink" in classes, "KafkaSink 类被删除"
        methods = [n.name for n in ast.walk(classes["KafkaSink"])
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        assert "_try_connect" in methods, "KafkaSink._try_connect 被删除（stub 回潮）"
        assert "send" in methods, "KafkaSink.send 被删除"
        assert "close" in methods, "KafkaSink.close 被删除"

    def test_local_sink_class_exists(self):
        """LocalSink 类须存在（pipeline_mode=local 的类型兼容占位）。"""
        tree = self._parse_audit_py()
        classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        assert "LocalSink" in classes, "LocalSink 类被删除"

    def test_pipeline_mode_env_var_read(self):
        """模块顶层须读 AUDIT_PIPELINE 环境变量（配置开关存在性）。"""
        source = (REPO / "service" / "app" / "audit.py").read_text(encoding="utf-8")
        assert "AUDIT_PIPELINE" in source,             "AUDIT_PIPELINE 环境变量读取被删除（配置开关丢失）"


# ========== 对账脚本测试 ==========

class TestReconcileScript:
    """audit_reconcile.py 纯逻辑测试（mock KafkaConsumer）。"""

    def test_count_jsonl_existing(self):
        """count_jsonl 正确计数。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"seq":1,"caller":"a"}\n{"seq":2,"caller":"b"}\n')
            f.flush()
            import importlib; mod = importlib.import_module("scripts.audit_reconcile"); count, records = mod.count_jsonl(f.name)
        assert count == 2
        assert len(records) == 2
        os.unlink(f.name)

    def test_count_jsonl_missing(self):
        """count_jsonl 文件不存在返回 0。"""
        import importlib; mod = importlib.import_module("scripts.audit_reconcile")
        count, records = mod.count_jsonl("/nonexistent/path.jsonl")
        assert count == 0
        assert records == []

    def test_reconcile_consistent(self):
        """条数一致＋字段匹配 → consistent=True。"""
        import importlib; mod = importlib.import_module("scripts.audit_reconcile")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"seq":1,"caller":"a","endpoint":"/v1/ping","status":200,"latency_ms":5,"trace_id":"","ts":"2026-01-01T00:00:00"}\n')
            f.flush()
            jsonl_path = f.name

        with patch.object(mod, "consume_kafka", return_value=(1, [
            {"seq": 1, "caller": "a", "endpoint": "/v1/ping", "status": 200,
             "latency_ms": 5, "trace_id": "", "ts": "2026-01-01T00:00:00"}
        ])):
            result = mod.reconcile(jsonl_path, "test-topic", "127.0.0.1:9092")
        assert result["consistent"] is True
        assert result["jsonl_count"] == 1
        assert result["kafka_count"] == 1
        os.unlink(jsonl_path)

    def test_reconcile_count_mismatch(self):
        """条数不一致 → consistent=False。"""
        import importlib; mod = importlib.import_module("scripts.audit_reconcile")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"seq":1}\n{"seq":2}\n')
            f.flush()
            jsonl_path = f.name

        with patch.object(mod, "consume_kafka", return_value=(1, [{"seq": 1}])):
            result = mod.reconcile(jsonl_path, "test-topic", "127.0.0.1:9092")
        assert result["consistent"] is False
        assert "条数不一致" in result["message"]
        os.unlink(jsonl_path)

    def test_reconcile_kafka_failure(self):
        """Kafka 消费失败 → consistent=False + 错误信息。"""
        import importlib; mod = importlib.import_module("scripts.audit_reconcile")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"seq":1}\n')
            f.flush()
            jsonl_path = f.name

        with patch.object(mod, "consume_kafka", side_effect=RuntimeError("connection refused")):
            result = mod.reconcile(jsonl_path, "test-topic", "127.0.0.1:9092")
        assert result["consistent"] is False
        assert "Kafka 消费失败" in result["message"]
        os.unlink(jsonl_path)


# ========== Live 测试（Kafka 可达时才跑）==========

@skip_if_no_kafka
class TestKafkaSinkLive:
    """真实 Kafka 集成测试（compose.kafka.yml 起栈后运行）。"""

    def test_kafka_sink_connects_and_sends(self):
        """KafkaSink 连接真实 Kafka 并成功投递。"""
        ks = KafkaSink(bootstrap_servers="127.0.0.1:9092", topic=KAFKA_TOPIC)
        assert ks.ready(), f"KafkaSink 连接失败：{ks._init_error}"
        e = _make_entry()
        assert ks.send(e) is True
        ks.close()

    def test_dual_write_end_to_end(self):
        """dual 模式端到端：JSONL + Kafka 双写，本次运行消息对账一致。

        口径修正（2026-10-16）：oneserve-audit 为共享 topic，历史累积消息使
        「全量条数相等」断言必然失败（JSONL=5 vs Kafka=26+，且每次重跑继续涨）。
        改为 run-scope 对账：本次条目用唯一 caller 标记（test-app-<uuid8>），
        消费侧只统计本标记消息——Kafka 语义下「本次双写的每条都到达且字段一致」，
        与 topic 历史无关，幂等可重复。bootstrap 走宿主 EXTERNAL 127.0.0.1:9092。
        """
        run_marker = f"test-app-{uuid.uuid4().hex[:8]}"
        with tempfile.TemporaryDirectory() as td:
            w = AuditWriter(td, flush_interval=0, pipeline_mode="dual")
            for i in range(5):
                w.append(_make_entry(caller=run_marker, endpoint=f"/v1/test/{i}"))
            w.close()

            records = _read_jsonl(w.jsonl)
            assert len(records) == 5

            # 对账（run-scope：消费结果按本次 caller 标记过滤后再交给 reconcile）
            import importlib; mod = importlib.import_module("scripts.audit_reconcile")
            orig_consume = mod.consume_kafka

            def scoped_consume(topic, bootstrap, timeout_s=10):
                _, recs = orig_consume(topic, bootstrap, timeout_s)
                mine = [r for r in recs if r.get("caller") == run_marker]
                return len(mine), mine

            with patch.object(mod, "consume_kafka", side_effect=scoped_consume):
                result = mod.reconcile(str(w.jsonl), KAFKA_TOPIC, "127.0.0.1:9092", timeout_s=15)
            assert result["consistent"], f"对账失败：{result['message']}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
