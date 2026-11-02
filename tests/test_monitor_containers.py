#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻容器健康聚合断言套件（蓝图 1a 批·监控最小集 2026-10-20）。

被测对象：service/app/monitor.py containers_view（纯编排，探活原语来自 app/healthz）
＋ compose 日志滚动契约。纯逻辑直测：monkeypatch healthz 探活原语（零真实网络）。
（原统一登录两容器探针与 probe_http_get 已随无登录模式决策 退役，见 git 史。）

覆盖：
  ① 出参形状：七行全量（六常驻＋可选 hbase）、字段齐全、顺序稳定；
  ② overall 评级：redis error→error；其余 degraded→degraded；全 ok→ok；off 不降级；
  ③ 端点/页面/脚本三处接线机检＋compose 日志滚动锚点契约（防漂移）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import healthz as hlz          # noqa: E402
from app import monitor as mon          # noqa: E402

EXPECTED_CONTAINERS = [
    "oneserve-etcd", "oneserve-apisix", "oneserve-redis",
    "oneserve-service", "oneserve-batch-worker",
    "oneserve-portal", "oneserve-hbase",
]


def _patch_probes(monkeypatch, *, tcp="ok", redis="ok", hbase="off"):
    """healthz 探活原语全量替换为可控假件（零真实网络）。"""
    async def fake_tcp(host, port, *, what):
        return {"status": tcp, "detail": f"[fake] {what} {tcp}"}

    async def fake_redis(r):
        return {"status": redis, "detail": f"[fake] redis {redis}"}

    async def fake_hbase():
        return {"status": hbase, "detail": f"[fake] hbase {hbase}"}

    monkeypatch.setattr(hlz, "probe_tcp", fake_tcp)
    monkeypatch.setattr(hlz, "probe_redis", fake_redis)
    monkeypatch.setattr(hlz, "probe_hbase", fake_hbase)


def test_containers_view_shape_and_order(monkeypatch):
    """七行全量（六常驻＋可选 hbase）＋字段齐全＋顺序稳定。"""
    _patch_probes(monkeypatch)
    out = asyncio.run(mon.containers_view(redis=None))
    assert out["overall"] == "ok"
    assert [r["container"] for r in out["containers"]] == EXPECTED_CONTAINERS
    for row in out["containers"]:
        assert set(row) == {"container", "role", "probe", "status", "detail", "latencyMs"}
        assert row["status"] in ("ok", "degraded", "error", "off")
        assert isinstance(row["detail"], str) and row["detail"]


def test_overall_grades(monkeypatch):
    """redis error→error；非 redis degraded→degraded；off（hbase/batch-worker）不降级。"""
    _patch_probes(monkeypatch, redis="error")
    assert asyncio.run(mon.containers_view(None))["overall"] == "error"
    _patch_probes(monkeypatch, tcp="degraded")
    assert asyncio.run(mon.containers_view(None))["overall"] == "degraded"
    _patch_probes(monkeypatch, hbase="off")
    assert asyncio.run(mon.containers_view(None))["overall"] == "ok"


def test_batch_worker_and_hbase_off_honesty(monkeypatch):
    """无探针的 batch-worker＝off（不造状态）；hbase 未配置＝off 不参与评级。"""
    _patch_probes(monkeypatch)
    rows = asyncio.run(mon.containers_view(None))["containers"]
    by = {r["container"]: r for r in rows}
    assert by["oneserve-batch-worker"]["status"] == "off"
    assert "无网络探针" in by["oneserve-batch-worker"]["probe"] + by["oneserve-batch-worker"]["detail"]
    assert by["oneserve-hbase"]["status"] == "off"


def test_endpoint_and_page_wiring():
    """端点在 monitor_api、monitor.js API 表、monitor.html 出处标注三处同在。"""
    api_src = (REPO / "service" / "app" / "monitor_api.py").read_text(encoding="utf-8")
    js_src = (REPO / "admin" / "monitor.js").read_text(encoding="utf-8")
    html_src = (REPO / "admin" / "monitor.html").read_text(encoding="utf-8")
    path = "/v1/admin/monitor/containers"
    assert path in api_src and "containers_view" in api_src
    assert path in js_src
    assert path in html_src


def test_compose_log_rotation_contract():
    """compose 日志滚动（监控最小集①）：锚点带 max-size/max-file，全部服务引用。"""
    src = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "x-log-rotation: &log-rotation" in src
    assert 'max-size: "10m"' in src and 'max-file: "3"' in src
    referenced = src.count("logging: *log-rotation")
    # 6 个服务全接（etcd/apisix/redis/service/batch-worker/portal；无登录模式
    # 无登录模式决策 起统一登录三服务已移除）；prod 形态经 compose merge 继承本配置
    assert referenced == 6, f"logging 引用数应为 6（6 服务）：{referenced}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
