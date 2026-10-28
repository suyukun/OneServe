#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2a /healthz 组件探活套件（tests/test_healthz.py）——纯 python 直跑或 pytest 收集均可。

守卫口径（任务卡：/healthz 从只探 redis 扩为组件探活，供运维/容器编排；
与 /v1/portal/status 口径一致但独立，portal/status 不动；响应向后兼容）：
  1. 评级纯逻辑 hlz.overall：redis error ⇒ ("error",503)（既有语义：redis 挂＝致命，
     compose service healthcheck 依赖 503 判据）；hbase/网关/etcd 任一 degraded ⇒
     ("degraded",200)（不算 fatal，编排不打重启）；全 ok（hbase off 不参与评级）⇒
     ("ok",200)；
  2. HBase 轻探目标三态：env HBASE_HOST 优先；env 未设读 connections.yaml 的
     sources.hbase（仓库 yaml 即配 oneserve-hbase）；两路皆无/坏 yaml ⇒ None
     （组件如实 off，不探不评——代码默认 127.0.0.1 不算配置，防误报）；
  3. TCP 轻探真测：本机临时监听口可达＝ok；死口＝degraded（不 fatal），
     detail 带目标与异常类型；
  4. 胶水机检（宿主机无 fastapi，同 test_monitor 口径）：main.py py_compile＋
     AST 契约——/healthz 路由在、出参含 components 细分、既有 status/redis 字段
     保留、redis:down 仍 503；healthz.py py_compile（纯逻辑不 import fastapi）；
  5. compose 契约：batch-worker 有 healthcheck（CMD python 探针＝app.batch_worker
     进程检＋REDIS_URL 队列通道检）；service healthcheck 仍指 /healthz
     （既有编排判据不被本卡破坏）。

被测对象 service/app/healthz.py 不依赖 FastAPI/redis（redis 客户端注入，连接器 asyncio 原生）。
"""
from __future__ import annotations

import ast
import asyncio
import os
import py_compile
import socket
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import healthz as hlz   # noqa: E402


# ── 1. 评级纯逻辑（整体 status 与 HTTP 码；向后兼容红线） ──

def test_overall_ok_with_all_components_up():
    """全组件 ok ⇒ ("ok", 200)。"""
    comps = {"redis": {"status": "ok"}, "hbase": {"status": "ok"},
             "gateway": {"status": "ok"}, "etcd": {"status": "ok"}}
    assert hlz.overall(comps) == ("ok", 200)


def test_overall_ok_when_hbase_unconfigured_off():
    """hbase 未配置＝off 不参与评级：其余 ok ⇒ 整体仍 ok（off 不是降级）。"""
    comps = {"redis": {"status": "ok"}, "hbase": {"status": "off"},
             "gateway": {"status": "ok"}, "etcd": {"status": "ok"}}
    assert hlz.overall(comps) == ("ok", 200)


def test_overall_degraded_for_each_nonfatal_component():
    """hbase／gateway／etcd 任一不可达 ⇒ ("degraded", 200)——如实降级但不算 fatal。"""
    base = {"redis": {"status": "ok"}, "hbase": {"status": "ok"},
            "gateway": {"status": "ok"}, "etcd": {"status": "ok"}}
    for comp in ("hbase", "gateway", "etcd"):
        comps = {**base, comp: {"status": "degraded"}}
        assert hlz.overall(comps) == ("degraded", 200), comp


def test_overall_error_only_when_redis_down():
    """redis 挂 ⇒ ("error", 503)（既有语义保持）；其余组件再糟也不改这一判据。"""
    comps = {"redis": {"status": "error"}, "hbase": {"status": "degraded"},
             "gateway": {"status": "degraded"}, "etcd": {"status": "degraded"}}
    assert hlz.overall(comps) == ("error", 503)
    # redis 正常时，其余全 degraded 也只是 degraded＋200（编排不打重启）
    comps2 = {"redis": {"status": "ok"}, "hbase": {"status": "degraded"},
              "gateway": {"status": "degraded"}, "etcd": {"status": "degraded"}}
    assert hlz.overall(comps2) == ("degraded", 200)


# ── 2. HBase 轻探目标（env > yaml > 未配置 off） ──

def _with_env(mapping, fn):
    """临时改 os.environ 跑 fn（直跑/pytest 两模式都安全的 save-restore）。"""
    saved = {k: os.environ.get(k) for k in mapping}
    try:
        os.environ.update(mapping)
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_hbase_probe_target_env_wins():
    """env HBASE_HOST 非空 ⇒ 目标取 env（HBASE_PORT 缺省回代码默认口）。"""
    run = hlz.hbase_probe_target
    assert _with_env({"HBASE_HOST": "hbase-env-host", "HBASE_PORT": "19190"}, run) == \
        ("hbase-env-host", 19190)
    assert _with_env({"HBASE_HOST": "hbase-env-host", "HBASE_PORT": ""}, run) == \
        ("hbase-env-host", 9090)   # conn_cfg.DEFAULT_PORT


def test_hbase_probe_target_yaml_fallback():
    """env 未设 ⇒ 读 connections.yaml（仓库 yaml 即配 oneserve-hbase:9090）。"""
    assert _with_env({"HBASE_HOST": "", "CONNECTIONS_YAML": ""}, hlz.hbase_probe_target) == \
        ("oneserve-hbase", 9090)


def test_hbase_probe_target_none_when_unconfigured_or_broken():
    """env/yaml 皆无（yaml 缺 sources.hbase）或坏 yaml ⇒ None（off，不探不评）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write("sources: {}" + chr(10))    # 文件在场但缺 hbase 段
        empty_yaml = f.name
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write("sources: {hbase: {host: [broken")
        broken_yaml = f.name
    try:
        assert _with_env({"HBASE_HOST": "", "CONNECTIONS_YAML": empty_yaml},
                         hlz.hbase_probe_target) is None
        assert _with_env({"HBASE_HOST": "", "CONNECTIONS_YAML": broken_yaml},
                         hlz.hbase_probe_target) is None
    finally:
        os.unlink(empty_yaml)
        os.unlink(broken_yaml)


# ── 3. TCP 轻探（真监听口＝ok；死口＝degraded） ──

def test_probe_tcp_ok_against_live_listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))              # 临时口，不碰任何真实服务
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        res = asyncio.run(hlz.probe_tcp("127.0.0.1", port, what="轻探自测"))
        assert res["status"] == "ok"
        assert f"127.0.0.1:{port}" in res["detail"]
    finally:
        srv.close()


def test_probe_tcp_down_is_degraded_not_error():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()                              # 占位后即关＝死口（不搞破坏性模拟）
    res = asyncio.run(hlz.probe_tcp("127.0.0.1", port, what="轻探自测"))
    assert res["status"] == "degraded"       # 非 redis 组件不可达＝degraded，不 fatal
    assert "ConnectionRefusedError" in res["detail"] or "Error" in res["detail"]


# ── 4. 胶水机检（宿主机无 fastapi：py_compile＋AST 契约） ──

def _routes(filename: str) -> dict:
    """AST 抽 FastAPI 路由表：路径 → 函数节点（同 test_monitor 口径）。"""
    tree = ast.parse((REPO / "service" / "app" / filename).read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                        and deco.func.attr in ("get", "post")
                        and deco.args and isinstance(deco.args[0], ast.Constant)):
                    out[deco.args[0].value] = node
    return out


def test_modules_compile():
    py_compile.compile(str(REPO / "service" / "app" / "healthz.py"), doraise=True)
    py_compile.compile(str(REPO / "service" / "app" / "main.py"), doraise=True)


def test_healthz_route_and_components_contract():
    """/healthz 路由在；出参含 components 细分＋既有 status/redis 字段保留。"""
    fn = _routes("main.py")["/healthz"]
    src = ast.unparse(fn)
    assert "components" in src, "出参须含 components 细分（P2a 主字段）"
    assert '"redis"' in src or "'redis'" in src, "既有 redis 字段保留（向后兼容）"
    assert "hlz.overall" in src, "评级纯逻辑统一走 healthz.overall（可单测）"
    assert "503" in src, "redis 挂＝503 既有编排判据保留"


def test_healthz_module_is_fastapi_free():
    """healthz.py 纯逻辑不 import fastapi/redis（宿主可直测；分层同 monitor.py）。"""
    tree = ast.parse((REPO / "service" / "app" / "healthz.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] not in ("fastapi", "redis") for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in ("fastapi", "redis")


# ── 5. compose 契约（batch-worker healthcheck；service 判据不回归） ──

def _compose() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))


def test_batch_worker_has_healthcheck():
    hc = _compose()["services"]["batch-worker"].get("healthcheck")
    assert hc, "batch-worker 须有 healthcheck（P2a 缺口收口）"
    assert hc["test"][0] == "CMD" and hc["test"][1] == "python"
    probe = " ".join(hc["test"][2:])
    assert "app.batch_worker" in probe, "探针须检 worker 进程（/proc cmdline）"
    assert "REDIS_URL" in probe, "探针须检 Redis 队列通道（FIFO 消费前提）"
    assert hc.get("interval") and hc.get("timeout") and hc.get("retries"), "探针参数齐备"


def test_batch_worker_probe_excludes_self_pid():
    """探针排除自身 pid——防探针命令行自含 'app.batch_worker' 字样假阳性。"""
    probe = " ".join(_compose()["services"]["batch-worker"]["healthcheck"]["test"][2:])
    assert "getpid" in probe, "探针自身 pid 须排除"


def test_service_healthcheck_still_targets_healthz():
    """service 容器 healthcheck 仍打 /healthz（既有 503 判据不被本卡破坏）。"""
    hc = _compose()["services"]["service"]["healthcheck"]
    assert "/healthz" in " ".join(hc["test"])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[ok] {fn.__name__}")
        except Exception as exc:   # noqa: BLE001 —— 直跑模式聚合报错
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"[FAIL] {fn.__name__}: {exc}")
    if failed:
        print(f"== {failed} 组失败 ==")
    sys.exit(1 if failed else 0)
