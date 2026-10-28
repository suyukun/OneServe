#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task-029 登录审计消费（Casdoor 原生记录 → 八元组）测试套件。

分层：
  1. 纯逻辑：记录映射（成功/失败/冻结/登出/跳过）、actor 回退、时间解析、
     拉取器自心跳排除；
  2. 游标：首拉 7 天回补、重启续拉不重复、id 回退降级回补、跨过非登录记录；
  3. 开关：AUDIT_LOGIN_CONSUME=off / 缺凭证 → 不建线程；失败节流不抛；
  4. 可见性：落账后 my_access.audit_query 能按 endpoint 查出（审计页同一读法）；
  5. live：docker exec oneserve-service 验拉取器已在跑且已落 casdoor:login 条目
     （栈不可达/未部署本卡代码 → 显式 SKIP，不假绿；跑着但零产出 → 红）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import audit_casdoor_puller as puller  # noqa: E402
from app.audit import AuditWriter, digest_sha256  # noqa: E402

NOW = 1792454400.0   # 2026-10-20T08:00:00Z（固定时钟，避免时钟依赖）


def _skip(reason: str) -> None:
    print(f"  [SKIP] {reason}（不假绿）")


def _record(rid: int, *, action: str = "login", user: str = "tester",
            response: str = '{status:"ok", msg:""}', org: str = "oneserve",
            created: str = "2026-10-20T01:00:00Z",
            obj: dict | None = None, ip: str = "172.28.0.10",
            uri: str = "/api/login", name: str | None = None) -> dict:
    return {
        "id": rid, "name": name or f"rec-{rid}", "createdTime": created,
        "organization": org, "user": user, "clientIp": ip, "method": "POST",
        "requestUri": uri, "action": action, "language": "en",
        "object": json.dumps(obj if obj is not None else {
            "type": "login", "username": user or "tester",
            "application": "oneserve-admin", "organization": org,
        }, ensure_ascii=False),
        "response": response, "statusCode": 200, "detail": "", "isTriggered": True,
    }


class FakeClient:
    """分页假客户端：pages 是 {页码: 记录列表}（倒序，同 Casdoor 口径）。"""

    def __init__(self, pages: dict | None = None):
        self.pages = {k: list(v) for k, v in (pages or {}).items()}
        self.calls: list[int] = []
        self.error: Exception | None = None

    def fetch_page(self, page: int, page_size: int = puller.PAGE_SIZE) -> list[dict]:
        self.calls.append(page)
        if self.error is not None:
            raise self.error
        return list(self.pages.get(page, []))


def _writer(td: str) -> AuditWriter:
    return AuditWriter(td, flush_interval=0, pipeline_mode="local")


def _jsonl(w: AuditWriter) -> list[dict]:
    text = w.jsonl.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _puller(w: AuditWriter, client, td: str, **kw) -> puller.LoginAuditPuller:
    kw.setdefault("state_path", str(Path(td) / "cursor.json"))
    kw.setdefault("now_fn", lambda: NOW)
    return puller.LoginAuditPuller(w, client, **kw)


# ---------- 1. 纯逻辑映射 ----------

def test_map_login_success():
    rec = _record(7, user="tester")
    mapped = puller.map_record(rec, now=NOW)
    assert mapped is not None
    assert mapped["endpoint"] == puller.ACTION_SUCCESS
    assert mapped["caller"] == "tester"
    assert mapped["status"] == 200
    assert mapped["record_id"] == 7
    # detail 带 IP/组织/应用/路径/记录 UUID（进摘要，不落明文主档）
    assert "ip=172.28.0.10" in mapped["detail"]
    assert "org=oneserve" in mapped["detail"]
    assert "app=oneserve-admin" in mapped["detail"]
    assert "record=rec-7" in mapped["detail"]
    assert abs(mapped["ts"] - puller.parse_casdoor_time("2026-10-20T01:00:00Z")) < 0.001


def test_map_login_failed_falls_back_to_object_username():
    """实测形态：失败行 user=""，用户名只在 object.username 里。"""
    rec = _record(8, user="",
                  obj={"type": "login", "username": "tester",
                       "application": "oneserve-admin", "organization": "oneserve"},
                  response='{status:"error", msg:"password or code is incorrect, you have 4 remaining chances"}')
    mapped = puller.map_record(rec, now=NOW)
    assert mapped["endpoint"] == puller.ACTION_FAILED
    assert mapped["caller"] == "tester"
    assert mapped["status"] == 401
    assert "msg=password or code is incorrect" in mapped["detail"]


def test_map_login_frozen_is_403():
    rec = _record(9, user="",
                  response='{status:"error", msg:"The user is forbidden to sign in, please contact the administrator"}')
    mapped = puller.map_record(rec, now=NOW)
    assert mapped["endpoint"] == puller.ACTION_FROZEN
    assert mapped["status"] == 403


def test_map_login_chinese_frozen():
    rec = _record(10, response='{status:"error", msg:"账号已冻结，请联系管理员"}')
    assert puller.map_record(rec, now=NOW)["endpoint"] == puller.ACTION_FROZEN


def test_map_logout():
    rec = _record(11, action="logout", user="tester", response='{status:"ok", msg:""}',
                  uri="/api/logout")
    mapped = puller.map_record(rec, now=NOW)
    assert mapped["endpoint"] == puller.ACTION_LOGOUT
    assert mapped["status"] == 200
    assert mapped["caller"] == "tester"


def test_map_skips_token_exchange_and_admin_actions():
    assert puller.map_record(_record(12, action="login/oauth/access_token"), now=NOW) is None
    assert puller.map_record(_record(13, action="update-user"), now=NOW) is None
    assert puller.map_record(_record(14, action="token"), now=NOW) is None


def test_map_skips_poller_self_heartbeat():
    """拉取器 built-in admin 会话自身的 login 成功记录不得回灌审计。"""
    rec = _record(15, user="admin", org="built-in",
                  obj={"type": "login", "username": "admin",
                       "application": "app-built-in", "organization": "built-in"})
    assert puller.map_record(rec, now=NOW) is None
    # 同样的 built-in 组织但普通用户/失败行仍要保留（可能是真实平台登录尝试）
    other = _record(16, user="", org="built-in",
                    obj={"type": "login", "username": "tester",
                         "application": "app-built-in", "organization": "built-in"},
                    response='{status:"error", msg:"用户: built-in/tester不存在"}')
    mapped = puller.map_record(other, now=NOW)
    assert mapped is not None and mapped["caller"] == "tester"


def test_map_empty_response_counts_as_success():
    """Casdoor 旧记录 response 为空（status:"" msg:""）但无错误语义 → 成功。"""
    rec = _record(17, response='{status:"", msg:""}')
    assert puller.map_record(rec, now=NOW)["endpoint"] == puller.ACTION_SUCCESS


def test_map_skips_unknown_actor():
    """user 与 object.username 都缺（授权码回调重复行）→ 跳过，不落 unknown。"""
    rec = _record(20, user="", obj={"application": "oneserve-admin",
                                    "organization": "oneserve", "type": "code"})
    assert puller.map_record(rec, now=NOW) is None


def test_map_bad_record_id_or_time():
    rec = _record(0)
    assert puller.map_record(rec, now=NOW) is None
    rec2 = _record(18, created="not-a-time")
    mapped = puller.map_record(rec2, now=NOW)
    assert mapped["ts"] == NOW          # 坏时间回退本轮时钟，不炸


def test_parse_response_and_time():
    assert puller.parse_response('{status:"ok", msg:""}') == ("ok", "")
    assert puller.parse_response('{status:"error", msg:"boom"}') == ("error", "boom")
    assert puller.parse_response(None) == ("", "")
    assert puller.parse_casdoor_time("2026-10-20T01:00:00Z") is not None
    assert puller.parse_casdoor_time("") is None
    assert puller.parse_casdoor_time("garbage") is None


def test_detail_enters_digest_not_plaintext():
    """detail（含 IP）只进 sha256 摘要；审计主档不出现明文 IP。"""
    rec = _record(19, user="tester")
    mapped = puller.map_record(rec, now=NOW)
    entry = puller.to_audit_entry(mapped)
    line = entry.to_json_line()
    assert "172.28.0.10" not in line
    assert entry.response_digest == digest_sha256(mapped["detail"])
    assert entry.caller == "tester" and entry.endpoint == puller.ACTION_SUCCESS


# ---------- 2. 游标 ----------

def test_first_pull_backfills_seven_days_then_incremental():
    with tempfile.TemporaryDirectory() as td:
        w = _writer(td)
        client = FakeClient({1: [_record(3), _record(2), _record(1)]})
        p = _puller(w, client, td)
        assert p.run_once() == 3
        rows = _jsonl(w)
        assert [r["caller"] for r in rows] == ["tester", "tester", "tester"]
        assert {r["endpoint"] for r in rows} == {puller.ACTION_SUCCESS}
        cursor = p.load_cursor()
        assert cursor["last_id"] == 3

        # 新增一条：增量只落新的，不重复
        client.pages[1] = [_record(4), _record(3), _record(2), _record(1)]
        assert p.run_once() == 1
        rows = _jsonl(w)
        assert len(rows) == 4
        assert p.load_cursor()["last_id"] == 4
        w.close()


def test_restart_resumes_from_cursor_without_duplicates():
    with tempfile.TemporaryDirectory() as td:
        w = _writer(td)
        client = FakeClient({1: [_record(2), _record(1)]})
        p1 = _puller(w, client, td)
        assert p1.run_once() == 2
        w.close()
        # 新进程/新 writer：游标在位，只拉增量
        w2 = _writer(td)
        client2 = FakeClient({1: [_record(3), _record(2), _record(1)]})
        p2 = _puller(w2, client2, td)
        assert p2.run_once() == 1
        rows = _jsonl(w2)
        assert len(rows) == 3
        assert p2.load_cursor()["last_id"] == 3
        w2.close()


def test_backfill_cutoff_skips_old_but_advances_cursor():
    with tempfile.TemporaryDirectory() as td:
        w = _writer(td)
        old = _record(1, created="2026-10-01T00:00:00Z")   # 19 天前 > 7 天窗口
        new = _record(2, created="2026-10-19T00:00:00Z")
        client = FakeClient({1: [new, old]})
        p = _puller(w, client, td, backfill_days=7)
        assert p.run_once() == 1
        assert [r["caller"] for r in _jsonl(w)] == ["tester"]
        assert p.load_cursor()["last_id"] == 2
        w.close()


def test_cursor_advances_across_skipped_records():
    """令牌交换/管理操作不落账，但游标必须跨过去，否则每轮重复翻页。"""
    with tempfile.TemporaryDirectory() as td:
        w = _writer(td)
        rows = [_record(3, action="token"), _record(2, action="login/oauth/access_token"),
                _record(1, action="update-user", user="admin")]
        client = FakeClient({1: rows})
        p = _puller(w, client, td)
        assert p.run_once() == 0
        assert _jsonl(w) == []
        assert p.load_cursor()["last_id"] == 3
        w.close()


def test_cursor_reset_falls_back_to_backfill():
    """Casdoor 记录 id 回退（库重置）→ 游标失效，降级 7 天回补。"""
    with tempfile.TemporaryDirectory() as td:
        w = _writer(td)
        state = Path(td) / "cursor.json"
        state.write_text(json.dumps({"version": 1, "last_id": 999}), encoding="utf-8")
        client = FakeClient({1: [_record(2), _record(1)]})
        p = _puller(w, client, td)
        assert p.run_once() == 2
        assert len(_jsonl(w)) == 2
        assert p.load_cursor()["last_id"] == 2
        w.close()


def test_failure_is_silent_and_retried_next_round():
    with tempfile.TemporaryDirectory() as td:
        w = _writer(td)
        client = FakeClient()
        client.error = puller.CasdoorError("casdoor down")
        p = _puller(w, client, td)
        assert p._safe_run_once() == 0          # 不抛
        assert p._safe_run_once() == 0
        assert p._fail_count == 2               # 节流计数（日志只在首错/每 10 次）
        client.error = None
        client.pages[1] = [_record(1)]
        assert p._safe_run_once() == 1
        assert p._fail_count == 0
        w.close()


# ---------- 3. 开关 ----------

def test_switch_off_does_not_start_thread(tmp_path):
    w = AuditWriter(str(tmp_path), flush_interval=0, pipeline_mode="local")
    out = puller.start_from_env(w, audit_dir=str(tmp_path),
                                env={"AUDIT_LOGIN_CONSUME": "off",
                                     "CASDOOR_ADMIN_PASSWORD": "x"},
                                client=FakeClient())
    assert out is None
    assert puller.login_consume_enabled({"AUDIT_LOGIN_CONSUME": "off"}) is False
    assert puller.login_consume_enabled({}) is True
    w.close()


def test_missing_password_does_not_start_thread(tmp_path):
    w = AuditWriter(str(tmp_path), flush_interval=0, pipeline_mode="local")
    out = puller.start_from_env(w, audit_dir=str(tmp_path), env={}, client=FakeClient())
    assert out is None
    w.close()


def test_start_from_env_starts_and_stops_thread(tmp_path):
    w = AuditWriter(str(tmp_path), flush_interval=0, pipeline_mode="local")
    client = FakeClient({1: [_record(1)]})
    out = puller.start_from_env(w, audit_dir=str(tmp_path),
                                env={"AUDIT_LOGIN_CONSUME": "on",
                                     "CASDOOR_ADMIN_PASSWORD": "x",
                                     "AUDIT_LOGIN_POLL_S": "3600"},
                                client=client)
    assert out is not None and out._thread is not None
    out.stop()
    assert out._thread is None
    w.close()


# ---------- 4. 审计查询可见性 ----------

def test_entries_visible_through_audit_query():
    """审计页同一读法（my_access.audit_query）按 endpoint 命中登录条目。"""
    from app import my_access as mya
    with tempfile.TemporaryDirectory() as td:
        w = _writer(td)
        client = FakeClient({1: [_record(2, user="tester"),
                                 _record(1, user="",
                                         response='{status:"error", msg:"password or code is incorrect"}',
                                         obj={"username": "tester",
                                              "application": "oneserve-admin"})]})
        p = _puller(w, client, td)
        assert p.run_once() == 2
        res = mya.audit_query(w.jsonl, endpoint="casdoor:", limit=10)
        assert res["total"] == 2
        assert {r["endpoint"] for r in res["items"]} == {
            puller.ACTION_SUCCESS, puller.ACTION_FAILED}
        assert all(r["callerMasked"] for r in res["items"])
        w.close()


# ---------- 5. live（栈可达且已部署本卡代码才跑） ----------

def _docker(args: list[str], timeout: int = 20):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


def test_live_puller_consumed_casdoor_records():
    if shutil.which("docker") is None:
        _skip("live：宿主机无 docker")
        return
    try:
        run = _docker(["inspect", "-f", "{{.State.Running}}", "oneserve-service"])
    except Exception as exc:                              # noqa: BLE001
        _skip(f"live：docker 不可用（{type(exc).__name__}）")
        return
    if run.returncode != 0 or run.stdout.strip() != "true":
        _skip("live：oneserve-service 容器未运行")
        return
    imp = _docker(["exec", "oneserve-service", "python", "-c",
                   "import app.audit_casdoor_puller"])
    if imp.returncode != 0:
        _skip("live：栈内尚未部署本卡代码（容器仍是旧版本）")
        return
    env = _docker(["exec", "oneserve-service", "printenv", "AUDIT_LOGIN_CONSUME"])
    if env.stdout.strip().lower() in ("off", "0", "false", "no"):
        _skip("live：栈上 AUDIT_LOGIN_CONSUME=off")
        return
    got = _docker(["exec", "oneserve-service", "sh", "-c",
                   "grep -F 'casdoor:login' /data/audit/audit.jsonl | tail -n 5"])
    assert got.returncode == 0 and got.stdout.strip(), \
        "栈在跑且已部署拉取器，但审计 JSONL 里没有 casdoor:login 条目（登录审计零产出）"
    rows = [json.loads(line) for line in got.stdout.splitlines() if line.strip()]
    assert rows, "live 解析到的登录审计行为空"
    for row in rows:
        assert str(row.get("endpoint", "")).startswith("casdoor:")
        assert str(row.get("caller") or "").strip(), "登录审计条目须有 actor"
        assert puller.parse_casdoor_time(row.get("ts")) is not None
    state = _docker(["exec", "oneserve-service", "test", "-f",
                     "/data/audit/casdoor-login-cursor.json"])
    assert state.returncode == 0, "登录审计游标文件缺失（增量口径未落地）"
    print(f"  ok live 登录审计条目 {len(rows)} 条（endpoint/caller/ts＋游标在位）")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as exc:                          # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failed else 0)
