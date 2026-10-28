#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 套件：性能测试管理域 /v1/admin/perf/*（2026-10-08 · 压测管理页后端）。

判据（任务卡口径逐条）：
  - run 防重：梯子在跑（内存句柄或状态文件 pid 存活）再触发 → 409 perf_running
    「压测进行中」（subprocess 以 stub 替身注入，不起真压测）；
  - run 参数校验：缺 service → 400；梯子格式非法（非逗号分隔正整数串）→ 400；
    ONESERVE_APPKEY 未配置 → 400 perf_appkey_missing（凭证只认环境变量）；
  - run 成功路径：Popen 参数正确（bash run_ladder.sh --service X --ladder Y、
    env 继承、后台会话），立即返回 run_id＋「四档约 8-12 分钟」提示＋状态留痕；
  - 工具不可达如实 503：LOADTEST 目录无 run_ladder.sh（容器态）→ 拒跑不假启动；
  - status 聚合：running/appkey_configured/loadtest_available/latest（最新 results
    目录＋报告文件在场否）；服务重启口径：句柄丢失＋状态文件在 → running=false＋
    unknown「后台进程状态未知」如实（不追进程）；
  - reports 聚合：扫 results/ 目录（时间戳/服务/报告链接），meta.json 服务名
    尽力读、缺则目录短名；杂项（散文件/点文件/非命名目录）不进列表；新→旧排序；
  - 报告伺服：performance-report.html/.md 白名单直出（html 按 text/html）；
    白名单外文件名/路径穿越形状 → 404；不存在 → 404。

分层纪律：沿 test_versions_admin 口径——fastapi stub＋端点函数直调（FakeRequest）；
loadtest 根经 ONESERVE_LOADTEST_DIR 指到 tmp_path（不碰真 results/）；
subprocess.Popen 以替身注入（重操作绝不起真梯子）。

运行：python3 -m pytest tests/test_perf_admin.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "service"), str(REPO / "sync")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _install_fastapi_stub() -> list:
    """宿主门禁环境无 fastapi（容器内才有）——注入最小 stub 以便直测 admin_api 端点函数。

    与 test_versions_admin 同款：stub 只满足 admin_api 顶层 import 与被测端点用到的
    JSONResponse/PlainTextResponse 形状（.status_code/.body）。
    """
    if "fastapi" in sys.modules:
        return []
    import types as _types
    fastapi = _types.ModuleType("fastapi")

    class _APIRouter:
        def __init__(self, *a, **k):   # 2026-10-09 闸恢复：router 构造带 dependencies kwarg
            pass

        def get(self, *a, **k):
            return lambda fn: fn

        def post(self, *a, **k):
            return lambda fn: fn

    fastapi.APIRouter = _APIRouter
    # 2026-10-09 管理令牌闸恢复：admin_api 顶层新增 Depends/HTTPException import，
    # 桩环境同步补成员（直调端点函数不过路由层，_admin_gate 不在单测路径执行）。
    fastapi.Depends = lambda dep: dep

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            self.status_code = status_code
            self.detail = detail

    fastapi.HTTPException = _HTTPException
    fastapi.Request = object
    responses = _types.ModuleType("fastapi.responses")

    class _JSONResponse:
        def __init__(self, content, status_code=200, **k):
            self.status_code = status_code
            self.body = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.media_type = k.get("media_type", "application/json")

    class _PlainTextResponse:
        def __init__(self, content, media_type=None, **k):
            self.status_code = 200
            self.body = str(content).encode("utf-8")
            self.media_type = media_type

    responses.JSONResponse = _JSONResponse
    responses.PlainTextResponse = _PlainTextResponse
    fastapi.responses = responses
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    return ["fastapi", "fastapi.responses"]      # 隔离修复：登记注入键供调用方装完即拆


_STUB_KEYS = _install_fastapi_stub()

from app import admin_api as aa              # noqa: E402

# 隔离修复（2026-10-15）：stub 只服务上方被测模块 import——装完即拆，防假 fastapi
# 残留 sys.modules 污染单进程全量跑的后续套件（实证：test_proxy_hub 的
# importorskip("fastapi") 被残留假模块误判）。admin_api 已缓存
# sys.modules['app.admin_api']，其内 stub 类引用随模块存活，本套件不受影响。
for _k in _STUB_KEYS:
    sys.modules.pop(_k, None)
del _STUB_KEYS


class FakeRequest:
    """端点直调最小形状：await request.json()（perf 域不依赖 app.state）。"""

    def __init__(self, payload=None):
        self._p = payload or {}
        self.app = SimpleNamespace(state=SimpleNamespace())

    async def json(self):
        return self._p


def _run(coro):
    return asyncio.run(coro)


def _body(resp):
    return json.loads(resp.body.decode("utf-8"))


class FakeProc:
    """Popen 替身：poll() 可编排（None＝在跑 / 0＝已退出）。"""

    def __init__(self, pid=43210, exit_code=None):
        self.pid = pid
        self._exit = exit_code

    def poll(self):
        return self._exit


@pytest.fixture()
def lab(tmp_path, monkeypatch):
    """隔离环境：LOADTEST 根→tmp（含 run_ladder.sh 占位＋results/），env/状态清零。"""
    lt = tmp_path / "loadtest"
    (lt / "results").mkdir(parents=True)
    (lt / "run_ladder.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("ONESERVE_LOADTEST_DIR", str(lt))
    monkeypatch.setenv("ONESERVE_APPKEY", "sb_test_perf_key")
    monkeypatch.setattr(aa, "_perf_state", None)
    spawned = []

    def fake_popen(args, **kwargs):
        spawned.append({"args": args, "kwargs": kwargs})
        return FakeProc()

    monkeypatch.setattr(aa.subprocess, "Popen", fake_popen)
    return SimpleNamespace(lt=lt, results=lt / "results", spawned=spawned)


# ── run 端点：参数校验 ──

def test_run_requires_service(lab):
    resp = _run(aa.perf_run_ep(FakeRequest({"ladder": "20"})))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "invalid_service"


# 注：空串 ladder＝有意回落四档默认值（test_run_default_ladder_is_four_tiers 覆盖），不算非法
@pytest.mark.parametrize("bad", ["abc", "100,,", ",100", "100;300", "1e3"])
def test_run_rejects_bad_ladder(lab, bad):
    resp = _run(aa.perf_run_ep(FakeRequest({"service": "v1-svc-usr-asset-tag", "ladder": bad})))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "invalid_ladder"


def test_run_requires_appkey_env(lab, monkeypatch):
    monkeypatch.delenv("ONESERVE_APPKEY", raising=False)
    resp = _run(aa.perf_run_ep(FakeRequest({"service": "v1-svc-usr-asset-tag"})))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "perf_appkey_missing"
    assert not lab.spawned, "凭证缺失不得起进程"


# ── run 端点：成功路径（stub 子进程） ──

def test_run_spawns_run_ladder_and_returns_run_id(lab):
    resp = _run(aa.perf_run_ep(FakeRequest(
        {"service": "v1-svc-usr-basic-info", "ladder": "100,300,500,800"})))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["ok"] is True and body["run_id"].startswith("perf-")
    assert "8-12" in body["note"], "须带「四档约 8-12 分钟」时长提示"
    # Popen 参数：bash run_ladder.sh --service … --ladder …＋env 继承＋独立会话
    assert len(lab.spawned) == 1
    args = lab.spawned[0]["args"]
    assert args[0] == "bash" and args[1].endswith("run_ladder.sh")
    assert "--service" in args and args[args.index("--service") + 1] == "v1-svc-usr-basic-info"
    assert args[args.index("--ladder") + 1] == "100,300,500,800"
    kw = lab.spawned[0]["kwargs"]
    assert kw["env"].get("ONESERVE_APPKEY") == "sb_test_perf_key", "凭证经 env 继承给子进程"
    assert kw.get("start_new_session") is True
    # 状态留痕：内存句柄＋results/.perf-run.json（无 proc 键）
    assert aa._perf_state and aa._perf_state["pid"] == 43210
    st = json.loads((lab.results / ".perf-run.json").read_text(encoding="utf-8"))
    assert st["run_id"] == body["run_id"] and "proc" not in st


def test_run_default_ladder_is_four_tiers(lab):
    resp = _run(aa.perf_run_ep(FakeRequest({"service": "v1-svc-usr-asset-tag"})))
    assert resp.status_code == 200
    args = lab.spawned[0]["args"]
    assert args[args.index("--ladder") + 1] == "100,300,500,800", "缺省梯子＝四档默认值"


# ── run 端点：防重（同一时刻仅一个压测进程） ──

def test_run_rejects_while_in_memory_proc_alive(lab, monkeypatch):
    monkeypatch.setattr(aa, "_perf_state",
                        {"run_id": "perf-x", "service": "s", "ladder": "20",
                         "pid": 43210, "started_at": "", "proc": FakeProc(exit_code=None)})
    resp = _run(aa.perf_run_ep(FakeRequest({"service": "v1-svc-usr-asset-tag"})))
    assert resp.status_code == 409
    body = _body(resp)
    assert body["error"] == "perf_running" and "压测进行中" in body["message"]
    assert not lab.spawned, "运行中不得再起第二个梯子"


def test_run_rejects_via_state_file_pid_after_service_restart(lab, monkeypatch):
    """服务重启口径（防双跑）：句柄丢失但状态文件 pid 存活 → 仍 409（status 另报未知）。"""
    (lab.results / ".perf-run.json").write_text(json.dumps(
        {"run_id": "perf-old", "service": "s", "ladder": "100,300,500,800",
         "pid": 1, "started_at": "2026-10-08T20:00:00"}), encoding="utf-8")
    monkeypatch.setattr(aa, "_perf_pid_alive", lambda pid: True)   # pid 1 恒活，替身探测
    resp = _run(aa.perf_run_ep(FakeRequest({"service": "v1-svc-usr-asset-tag"})))
    assert resp.status_code == 409 and _body(resp)["error"] == "perf_running"
    assert not lab.spawned


def test_run_allows_again_after_proc_finished(lab, monkeypatch):
    monkeypatch.setattr(aa, "_perf_state",
                        {"run_id": "perf-x", "service": "s", "ladder": "20",
                         "pid": 43210, "started_at": "", "proc": FakeProc(exit_code=0)})
    resp = _run(aa.perf_run_ep(FakeRequest({"service": "v1-svc-usr-asset-tag"})))
    assert resp.status_code == 200, "上一梯子已退出 → 允许再次触发"
    assert len(lab.spawned) == 1


# ── run 端点：容器态工具不可达（如实 503） ──

def test_run_loadtest_unavailable_honest_503(lab, monkeypatch):
    monkeypatch.setenv("ONESERVE_LOADTEST_DIR", str(lab.lt / "nowhere"))
    resp = _run(aa.perf_run_ep(FakeRequest({"service": "v1-svc-usr-asset-tag"})))
    assert resp.status_code == 503
    body = _body(resp)
    assert body["error"] == "loadtest_unavailable"
    assert "CLI" in body["message"], "文案须给 CLI 触发指引"
    assert not lab.spawned, "不可达＝不假启动"


# ── status 端点 ──

def test_status_running_flags_and_latest(lab, monkeypatch):
    monkeypatch.setattr(aa, "_perf_state",
                        {"run_id": "perf-x", "service": "v1-svc-usr-basic-info", "ladder": "20",
                         "pid": 43210, "started_at": "2026-10-08T20:00:00",
                         "proc": FakeProc(exit_code=None)})
    d = lab.results / "20261008-203426-usr-basic-info"
    d.mkdir()
    (d / "performance-report.html").write_text("<html>r</html>", encoding="utf-8")
    resp = _run(aa.perf_status_ep(FakeRequest()))
    body = _body(resp)
    assert body["running"] is True
    assert body["appkey_configured"] is True
    assert body["loadtest_available"] is True
    assert body["run"]["run_id"] == "perf-x" and "proc" not in body["run"]
    assert body["latest"]["dir"] == "20261008-203426-usr-basic-info"
    assert body["latest"]["report_html"] is True
    assert body["latest"]["report_href"].endswith(
        "/v1/admin/perf/report/20261008-203426-usr-basic-info/performance-report.html")
    assert body["unknown"] is None


def test_status_appkey_false_when_env_missing(lab, monkeypatch):
    monkeypatch.delenv("ONESERVE_APPKEY", raising=False)
    body = _body(_run(aa.perf_status_ep(FakeRequest())))
    assert body["appkey_configured"] is False


def test_status_reports_unknown_after_service_restart(lab, monkeypatch):
    """服务重启不追进程：句柄丢失＋状态文件在 → running=false＋「状态未知」如实。"""
    (lab.results / ".perf-run.json").write_text(json.dumps(
        {"run_id": "perf-old", "service": "s", "ladder": "100,300,500,800",
         "pid": 43210, "started_at": "2026-10-08T20:00:00"}), encoding="utf-8")
    monkeypatch.setattr(aa, "_perf_pid_alive", lambda pid: True)
    body = _body(_run(aa.perf_status_ep(FakeRequest())))
    assert body["running"] is False
    assert body["unknown"] and "状态未知" in body["unknown"]["note"]
    assert body["unknown"]["run_id"] == "perf-old"


def test_status_clears_stale_state_when_pid_dead(lab, monkeypatch):
    (lab.results / ".perf-run.json").write_text(json.dumps(
        {"run_id": "perf-old", "pid": 999999, "started_at": ""}), encoding="utf-8")
    monkeypatch.setattr(aa, "_perf_pid_alive", lambda pid: False)
    body = _body(_run(aa.perf_status_ep(FakeRequest())))
    assert body["unknown"] is None
    assert not (lab.results / ".perf-run.json").exists(), "pid 已死＝清留痕，不留永久未知"


# ── reports 端点（聚合） ──

def _mk_run(results: Path, name: str, *, with_report=True, with_meta=True,
            svc_id="v1-svc-usr-basic-info", svc_name="用户基础信息"):
    d = results / name
    d.mkdir()
    if with_meta:
        (d / "meta.json").write_text(json.dumps(
            {"service": {"service_id": svc_id, "name": svc_name}}), encoding="utf-8")
    if with_report:
        (d / "performance-report.html").write_text("<html>r</html>", encoding="utf-8")
        (d / "performance-report.md").write_text("# r", encoding="utf-8")
    return d


def test_reports_aggregates_runs_newest_first(lab):
    _mk_run(lab.results, "20261008-202533-usr-basic-info")
    _mk_run(lab.results, "20261008-203426-usr-basic-info", with_meta=False)
    _mk_run(lab.results, "20261007-180000-usr-asset-tag",
            svc_id="v1-svc-usr-asset-tag", svc_name="用户资产标签")
    # 杂项不进列表：散文件/点目录/非命名目录
    (lab.results / "locust_300_stats.csv").write_text("x", encoding="utf-8")
    (lab.results / ".trash").mkdir()
    (lab.results / "not-a-run").mkdir()
    body = _body(_run(aa.perf_reports_ep(FakeRequest())))
    rows = body["reports"]
    assert [r["dir"] for r in rows] == [
        "20261008-203426-usr-basic-info", "20261008-202533-usr-basic-info",
        "20261007-180000-usr-asset-tag"], "新→旧；杂项排除"
    assert body["loadtest_available"] is True
    newest = rows[0]
    assert newest["service"] == "usr-basic-info", "meta 缺失回落目录短名"
    assert newest["report_html"] is True
    assert newest["href"].endswith("/v1/admin/perf/report/20261008-203426-usr-basic-info/performance-report.html")
    assert rows[1]["service"] == "v1-svc-usr-basic-info"
    assert rows[1]["service_name"] == "用户基础信息", "服务全名尽力读 meta.json"


def test_reports_run_without_report_flagged(lab):
    _mk_run(lab.results, "20261008-210000-usr-basic-info", with_report=False)
    row = _body(_run(aa.perf_reports_ep(FakeRequest())))["reports"][0]
    assert row["report_html"] is False and row["report_md"] is False and row["href"] is None


def test_reports_unavailable_returns_empty_with_note(lab, monkeypatch):
    monkeypatch.setenv("ONESERVE_LOADTEST_DIR", str(lab.lt / "nowhere"))
    body = _body(_run(aa.perf_reports_ep(FakeRequest())))
    assert body["reports"] == [] and body["loadtest_available"] is False
    assert body["note"], "容器态须如实带说明（页面据此回落静态镜像）"


# ── 报告伺服端点 ──

def test_report_serves_html_and_md(lab):
    d = _mk_run(lab.results, "20261008-203426-usr-basic-info")
    resp = _run(aa.perf_report_ep("20261008-203426-usr-basic-info",
                                  "performance-report.html", FakeRequest()))
    assert resp.status_code == 200
    assert "html" in (resp.media_type or "")
    assert resp.body == (d / "performance-report.html").read_bytes()
    resp_md = _run(aa.perf_report_ep("20261008-203426-usr-basic-info",
                                     "performance-report.md", FakeRequest()))
    assert resp_md.status_code == 200


def test_report_whitelist_and_traversal_guard(lab):
    _mk_run(lab.results, "20261008-203426-usr-basic-info")
    # 白名单外文件名（含穿越形状）→ 404
    for run, fname in (("20261008-203426-usr-basic-info", "meta.json"),
                       ("20261008-203426-usr-basic-info", "../../appkeys.yaml"),
                       ("..", "performance-report.html"),
                       ("20261008-203426-usr-basic-info", "keys.txt")):
        resp = _run(aa.perf_report_ep(run, fname, FakeRequest()))
        assert resp.status_code == 404, f"{run}/{fname} 须 404"
        assert not (lab.lt.parent / "appkeys.yaml").exists()


def test_report_missing_file_404(lab):
    _mk_run(lab.results, "20261008-203426-usr-basic-info", with_report=False)
    resp = _run(aa.perf_report_ep("20261008-203426-usr-basic-info",
                                  "performance-report.html", FakeRequest()))
    assert resp.status_code == 404 and _body(resp)["error"] == "not_found"


if __name__ == "__main__":
    import subprocess as _sp; sys.exit(_sp.call([sys.executable, '-m', 'pytest', __file__, '-q']))
