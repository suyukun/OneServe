# -*- coding: utf-8 -*-
"""租户管理端点套件（管理功能群一期 A 节 · tests/test_tenants_api.py，本卡新建）。

覆盖（设计稿 §1.10 验收映射）：
  1. 新建校验/双模分流/重复拒（§1.3-1；拍板① yaml 登记行）＋P0-2 全链回归——
     「新建登记行→validate 通过→授权页 upsert→签发→凭证落地」单进程闭环（1.10-6 前半）；
  2. 列表视图/指标卡/筛选（§1.2）＋分页信封（沿 metering_api 先例，1.10-5 读面）；
  3. 冻结/解冻端点（db 态 fakes；confirm 两段式；解冻必填工单号；P0-5 全量 quota
     重建断言；yaml 态 409 降级文案 §1.8）；resync-consumers 幂等（P1-③）；
  4. AST 契约守卫（P1-⑪）：五端点注册＋ROUTE_MIN_ROLE 行（GET viewer／POST admin）；
  5. db 模式 e2e（隔离一次性 pg，docker 不可达如实 skip；test_meta_writer_db 先例）：
     1.10-6 往返（新建→授权→签发→冻结→解冻→配额仍在）＋1.10-7 frozen 回归
     （frozen→他租户授权登记→仍 frozen＋remark 不丢）。

运行：python3 -m pytest tests/test_tenants_api.py -q（fastapi/httpx 缺席端点用例如实
skip；docker 不可达 e2e 如实 skip——不假绿）。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import appkeys as appk            # noqa: E402
from app import errors as E                # noqa: E402
from app import grants_admin as gam        # noqa: E402
from app import meta_writer as mw          # noqa: E402
from app import tenants_api as tw          # noqa: E402

try:
    from fastapi import FastAPI            # noqa: E402
    from fastapi.testclient import TestClient   # noqa: E402
    _HAS_FASTAPI = True
except Exception:   # noqa: BLE001
    TestClient = None   # type: ignore
    _HAS_FASTAPI = False


# ── 合成夹具（口径同 tests/test_grants_admin.synth_*：不读真库） ──

def synth_services() -> dict:
    return {
        "svc-tags": {"name": "用户资产标签", "route": "/v1/svc/tags", "status": "published",
                     "field_space": "wide297",
                     "fields": ["age_band", "city_code", "aum_band", "vip_flag"]},
        "svc-card": {"name": "账户摘要", "route": "/v1/svc/card", "status": "published",
                     "field_space": "wide297", "fields": ["card_sts", "bal_band"]},
    }


def synth_matrix() -> list[dict]:
    """已登记正式租户（active）＋frozen 戳租户＋未登记凭证租户串（t-ghost 走 keys）。"""
    return [
        {"tenant": "t-fin", "tenant_type": "external", "appkey": "ak_testfin00000001",
         "oa_ticket": "OA-2026-000001", "display_name": "演示调用方",
         "grants": [{"service": "svc-tags", "fields": ["age_band"]}]},
        {"tenant": "t-frozen", "tenant_type": "external", "oa_ticket": "OA-2026-000002",
         "display_name": "冻结样本", "tenant_status": "frozen", "grants": []},
        {"tenant": "t-sbx", "tenant_type": "sandbox", "appkey": "sb_testsandbox001",
         "grants": [{"service": "svc-tags", "fields": ["city_code"]}]},
    ]


def synth_keys() -> list[dict]:
    return [
        {"appkey": "ak_credfin0000001", "tenant": "t-fin", "key_type": "formal",
         "status": "active", "quota_qps": 7, "quota_daily": 77,
         "grants": [{"service": "svc-tags", "fields": ["age_band"]}]},
        {"appkey": "sb_ghost00000000001", "tenant": "t-ghost", "key_type": "sandbox",
         "status": "active", "quota_qps": 0, "quota_daily": 0},
    ]


def _write_matrix(path: Path, matrix: list[dict]) -> Path:
    doc = {"meta": {"updated": "2026-10-21T00:00:00+00:00", "updated_by": "test"},
           "matrix": matrix}
    path.write_text(gam.dump_matrix(doc), encoding="utf-8")
    return path


def _write_keys(path: Path, keys: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"meta": {}, "keys": keys}, sort_keys=False,
                                   allow_unicode=True), encoding="utf-8")
    return path


class _Audit:
    """审计 writer 假实现（收集 AuditEntry；断言 denied/成功事件）。"""

    def __init__(self):
        self.entries = []

    def append(self, entry):
        self.entries.append(entry)


def _client(tmp_path, matrix=None, keys=None):
    """自挂 tenants_api.router 的最小 app（test_metering._client 模式；off 态直测契约）。"""
    mpath = _write_matrix(tmp_path / "auth_matrix.yaml",
                          synth_matrix() if matrix is None else matrix)
    kpath = _write_keys(tmp_path / "registry" / "appkeys.yaml",
                        synth_keys() if keys is None else keys)
    app = FastAPI()
    state = app.state
    state.auth_matrix = yaml.safe_load(mpath.read_text(encoding="utf-8"))["matrix"]
    state.auth_matrix_path = mpath
    state.appkeys_path = kpath
    audit = _Audit()
    state.audit = audit
    app.include_router(tw.router)
    return TestClient(app), audit, mpath, kpath


@pytest.fixture(autouse=True)
def _yaml_mode(monkeypatch):
    """套件缺省钉 yaml 写模式（db 用例显式 monkeypatch 覆盖）。"""
    monkeypatch.delenv("META_WRITER", raising=False)


# ── 1. 新建：校验/双模/唯一性 ──

def test_validate_create_rejections():
    with pytest.raises(tw.TenantAdminError) as ei:
        tw.validate_tenant_create(["not", "a", "dict"])
    assert ei.value.status == 400 and ei.value.code == E.INVALID_JSON
    for payload, code in [
            ({"tenant_id": "Bad ID", "tenant_type": "external", "oa_ticket": "OA-1"}, E.INVALID_TENANT),
            ({"tenant_id": "t-x", "tenant_type": "vip", "oa_ticket": "OA-1"}, E.INVALID_REQUEST),
            ({"tenant_id": "t-x", "tenant_type": "sandbox", "oa_ticket": "OA-1"}, E.INVALID_REQUEST),
            ({"tenant_id": "t-x", "tenant_type": "external"}, E.OA_TICKET_REQUIRED),
            ({"tenant_id": "t-x", "tenant_type": "external", "oa_ticket": "o!"}, E.INVALID_OA_TICKET),
    ]:
        with pytest.raises(tw.TenantAdminError) as ei:
            tw.validate_tenant_create(payload)
        assert ei.value.code == code, (payload, ei.value.code)


def test_create_yaml_registered_row_and_remark_note(tmp_path):
    """yaml 新建＝矩阵登记行（四字段同快照投影形）；备注无列如实提示（§1.5 拍板①）。"""
    path = _write_matrix(tmp_path / "auth_matrix.yaml", synth_matrix())
    out = tw.create_tenant(path, synth_keys(),
                           {"tenant_id": "t-new", "display_name": "新租户",
                            "tenant_type": "external", "oa_ticket": "OA-2026-100001",
                            "remark": "备注样本"}, "demo")
    assert out["mode"] == "yaml" and "冻结/解冻需 db 模式" in out["hint"]
    assert out["tenant"]["status"] == "active"
    assert "备注仅 db 模式持久化" in out.get("remarkNote", "")
    doc = gam.load_matrix_file(path)
    errs, _ = gam.validate_matrix(doc)      # P0-2 前置：登记行读回校验通过（不 422）
    assert errs == [], errs
    row = gam._matrix_row(doc["matrix"], "t-new")
    assert row and row.get("oa_ticket") == "OA-2026-100001"
    assert row.get("grants") == [] and not row.get("appkey")
    assert "tenant_type" not in row         # external 未标＝规划器缺省（往返零漂移）
    out2 = tw.create_tenant(path, [], {"tenant_id": "t-sbx2", "display_name": "",
                                       "tenant_type": "sandbox", "oa_ticket": "",
                                       "remark": ""}, "demo")
    assert "演示模板" in out2["hint"] and "测试用" in out2["hint"]
    row2 = gam._matrix_row(gam.load_matrix_file(path)["matrix"], "t-sbx2")
    assert row2.get("tenant_type") == "sandbox" and not row2.get("oa_ticket")


def test_create_duplicate_rejected_both_modes(tmp_path, monkeypatch):
    path = _write_matrix(tmp_path / "auth_matrix.yaml", synth_matrix())
    keys = synth_keys()
    with pytest.raises(tw.TenantAdminError) as ei:   # 对已登记矩阵行
        tw.create_tenant(path, keys, {"tenant_id": "t-fin", "display_name": "x",
                                      "tenant_type": "external", "oa_ticket": "OA-2026-100009",
                                      "remark": ""}, "demo")
    assert ei.value.status == 422 and "双不复用" in ei.value.message
    with pytest.raises(tw.TenantAdminError) as ei:   # 对未登记凭证租户串同样拒
        tw.create_tenant(path, keys, {"tenant_id": "t-ghost", "display_name": "x",
                                      "tenant_type": "external", "oa_ticket": "OA-2026-100009",
                                      "remark": ""}, "demo")
    assert ei.value.status == 422
    # db 模式：tenant_exists 命中即拒（双不复用实体表核）
    monkeypatch.setattr(mw, "writer_mode", lambda: "db")
    monkeypatch.setattr(mw, "tenant_exists", lambda tid: tid == "t-db")
    upserts = []

    def fake_upsert(**kw):
        upserts.append(kw)
        return {"tenant_id": kw["tenant_id"], "display_name": kw["display_name"],
                "tenant_type": kw["tenant_type"], "status": "active",
                "oa_ticket_open": kw.get("oa_ticket_open"), "remark": kw.get("remark")}

    monkeypatch.setattr(mw, "tenant_upsert", fake_upsert)
    with pytest.raises(tw.TenantAdminError) as ei:
        tw.create_tenant(path, [], {"tenant_id": "t-db", "display_name": "x",
                                    "tenant_type": "external", "oa_ticket": "OA-2026-100010",
                                    "remark": ""}, "demo")
    assert ei.value.status == 422 and upserts == []
    out = tw.create_tenant(path, [], {"tenant_id": "t-db2", "display_name": "y",
                                      "tenant_type": "external", "oa_ticket": "OA-2026-100011",
                                      "remark": "r"}, "demo")
    assert out["mode"] == "db" and upserts[0]["tenant_id"] == "t-db2"
    assert upserts[0]["oa_ticket_open"] == "OA-2026-100011" and upserts[0]["remark"] == "r"


def test_p0_2_full_chain_register_grant_issue(tmp_path, monkeypatch):
    """P0-2 全链回归（1.10-6 前半，单进程闭环）：新建登记行→validate 通过→授权页
    upsert（不复现 422）→签发正式凭证→注册表落地→登记行原样（授权改写不吞登记行）。"""
    monkeypatch.delenv("META_WRITER", raising=False)
    path = _write_matrix(tmp_path / "auth_matrix.yaml", synth_matrix())
    keys_path = _write_keys(tmp_path / "registry" / "appkeys.yaml", [])
    services = synth_services()
    out = tw.create_tenant(path, [], {"tenant_id": "t-chain", "display_name": "全链样本",
                                      "tenant_type": "external", "oa_ticket": "OA-2026-100001",
                                      "remark": ""}, "demo")
    assert out["mode"] == "yaml"
    view, diff = gam.apply_grant_action(          # 授权页 upsert：登记行读回不再 422
        path, services, [],
        {"action": "upsert", "tenant": "t-chain", "service": "svc-tags",
         "fields": ["age_band"], "oa_ticket": "OA-2026-100002"}, "demo")
    assert diff["addedFields"] == ["age_band"] and view["tenant"] == "t-chain"
    doc_keys = {"meta": {}, "keys": []}
    rec, secret = appk.issue_appkey(doc_keys, gam.load_matrix_file(path)["matrix"],
                                    services, tenant="t-chain", app_name="对账应用",
                                    key_type="formal", oa_ticket="OA-2026-100003",
                                    quota_qps=7, quota_daily=77)
    assert rec["appkey"].startswith("ak_") and secret.startswith("sk_")
    doc_keys["keys"].append(rec)                   # 调用方追加（issue 不落 doc，admin_api 同款）
    appk.save_appkeys(keys_path, doc_keys)         # ④ 凭证落地（注册表落盘）
    reloaded = appk.load_appkeys(keys_path)
    landed = [k for k in reloaded["keys"] if k.get("tenant") == "t-chain"]
    assert len(landed) == 1 and landed[0]["quota_qps"] == 7
    row = gam._matrix_row(gam.load_matrix_file(path)["matrix"], "t-chain")
    assert row.get("oa_ticket") == "OA-2026-100001" and len(row["grants"]) == 1


# ── 2. 列表视图/指标卡 ──

def test_tenants_view_summary_filters_unregistered():
    view = tw.tenants_view(synth_matrix(), synth_keys())
    s = view["summary"]
    assert s["tenants"] == 4 and s["byType"] == {"internal": 0, "external": 2, "sandbox": 2}
    assert s["active"] == 3 and s["frozen"] == 1 and s["terminating"] == 0
    rows = {r["tenant"]: r for r in view["rows"]}
    assert rows["t-frozen"]["status"] == "frozen" and rows["t-frozen"]["registered"] is True
    assert rows["t-ghost"]["registered"] is False and rows["t-ghost"]["activeKeys"] == 1
    assert rows["t-fin"]["activeKeys"] == 1 and rows["t-fin"]["grantsCount"] == 1
    got = [r["tenant"] for r in tw.tenants_view(synth_matrix(), [], tenant_type="external")["rows"]]
    assert got == ["t-fin", "t-frozen"]
    got = [r["tenant"] for r in tw.tenants_view(synth_matrix(), [], status="frozen")["rows"]]
    assert got == ["t-frozen"]
    got = [r["tenant"] for r in tw.tenants_view(synth_matrix(), [], q="t-f")["rows"]]
    assert got == ["t-fin", "t-frozen"]


def test_freeze_plan_impact_preview():
    plan = tw.freeze_plan(synth_matrix(), synth_keys(), "t-fin")
    assert plan["activeKeys"] == 1 and "全量摘除" in plan["impacts"][0]
    assert plan["appkeys"] == [appk.mask_appkey("ak_credfin0000001")]
    with pytest.raises(tw.TenantAdminError) as ei:   # frozen 再冻结 → 409
        tw.freeze_plan(synth_matrix(), synth_keys(), "t-frozen")
    assert ei.value.status == 409 and ei.value.code == E.TENANT_FROZEN
    with pytest.raises(tw.TenantAdminError) as ei:   # 未登记 → 404
        tw.freeze_plan(synth_matrix(), synth_keys(), "t-miss")
    assert ei.value.status == 404 and ei.value.code == E.TENANT_NOT_FOUND


def test_sync_consumers_frozen_removal_active_quota_idempotent(monkeypatch):
    """P1-③ 幂等对账：frozen 摘／active 挂（带全量 quota＝P0-5 同口径）；复跑计数复现。"""
    calls = []

    async def fake_put(ak, *, quota_qps=None, quota_daily=None):
        calls.append(("put", ak, quota_qps, quota_daily))
        return True

    async def fake_delete(ak):
        calls.append(("delete", ak, None, None))
        return True

    monkeypatch.setattr(tw.gw_consumer, "put_consumer", fake_put)
    monkeypatch.setattr(tw.gw_consumer, "delete_consumer", fake_delete)
    out1 = asyncio.run(tw._sync_consumers(synth_matrix(), synth_keys()))
    assert ("put", "ak_credfin0000001", 7, 77) in calls
    assert ("delete", "sb_ghost00000000001", None, None) not in calls, "active 租户凭证不误摘"
    assert out1["ensured"] >= 1 and out1["removed"] == 0 and out1["failed"] == []
    calls.clear()
    keys2 = synth_keys() + [{"appkey": "ak_frozenkey0000001", "tenant": "t-frozen",
                             "key_type": "formal", "status": "active",
                             "quota_qps": 6, "quota_daily": 66}]
    out2 = asyncio.run(tw._sync_consumers(synth_matrix(), keys2))
    assert ("delete", "ak_frozenkey0000001", None, None) in calls
    assert out2["removed"] == 1
    calls.clear()
    out3 = asyncio.run(tw._sync_consumers(synth_matrix(), keys2))   # 幂等复跑
    assert out3["ensured"] == out2["ensured"] and out3["removed"] == out2["removed"]


# ── 3. 端点契约（自挂 TestClient；off 态） ──

@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi/httpx")
def test_list_endpoint_contract_and_pagination(tmp_path):
    c, audit, mpath, kpath = _client(tmp_path)
    r = c.get("/v1/admin/tenants")
    assert r.status_code == 200
    j = r.json()
    for k in ("total", "page", "pageSize", "items", "summary", "caliber"):
        assert k in j, "缺信封字段 " + k
    assert j["total"] == 4 and j["page"] == 1 and j["pageSize"] == 50
    assert j["summary"]["frozen"] == 1 and j["caliber"]["source"]
    assert all("registered" in i and "statusLabel" in i for i in j["items"])
    assert c.get("/v1/admin/tenants", params={"page": 0}).status_code == 400
    assert c.get("/v1/admin/tenants", params={"page_size": 30}).status_code == 400
    r = c.get("/v1/admin/tenants", params={"page_size": 20, "page": 1})
    assert r.status_code == 200 and r.json()["pageSize"] == 20
    r = c.get("/v1/admin/tenants", params={"status": "frozen"})
    assert r.json()["total"] == 1 and r.json()["items"][0]["tenant"] == "t-frozen"
    r = c.get("/v1/admin/tenants", params={"type": "sandbox", "q": "t-sbx"})
    assert r.json()["total"] == 1 and r.json()["items"][0]["tenant"] == "t-sbx"


@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi/httpx")
def test_create_endpoint_success_denied_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(tw, "_reload", lambda req: {})
    c, audit, mpath, kpath = _client(tmp_path)
    r = c.post("/v1/admin/tenants", json={"tenant_id": "t-new", "display_name": "新租户",
                                          "tenant_type": "external",
                                          "oa_ticket": "OA-2026-200001"})
    assert r.status_code == 200 and r.json()["mode"] == "yaml"
    assert r.json()["auditId"] is not None
    ok_events = [e for e in audit.entries if getattr(e, "endpoint", "") == "admin:tenant_create"]
    assert ok_events and getattr(ok_events[-1], "status", None) == 200
    r2 = c.post("/v1/admin/tenants", json={"tenant_id": "t-new", "display_name": "重复",
                                           "tenant_type": "external",
                                           "oa_ticket": "OA-2026-200002"})
    assert r2.status_code == 422 and "双不复用" in r2.json()["message"]
    denied = [e for e in audit.entries
              if getattr(e, "endpoint", "") == "admin:tenant_create"
              and getattr(e, "status", None) == 422]
    assert denied, "被拒提交须落 denied 审计事件"
    r3 = c.post("/v1/admin/tenants", json={"tenant_id": "t-s3", "tenant_type": "sandbox",
                                           "oa_ticket": "OA-2026-200003"})
    assert r3.status_code == 422
    assert gam._matrix_row(gam.load_matrix_file(mpath)["matrix"], "t-new")


@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi/httpx")
def test_yaml_lifecycle_denied_copy(tmp_path):
    """yaml 态冻结/解冻＝409 tenant_yaml_mode＋§1.8 降级文案（页面置灰的服务端兜底）。"""
    c, audit, mpath, kpath = _client(tmp_path)
    r = c.post("/v1/admin/tenants/t-fin/freeze", json={"confirm": True})
    assert r.status_code == 409 and r.json()["error"] == E.TENANT_YAML_MODE
    assert r.json()["message"] == tw.YAML_LIFECYCLE_COPY
    r = c.post("/v1/admin/tenants/t-fin/unfreeze", json={"oa_ticket": "OA-2026-300001"})
    assert r.status_code == 409 and r.json()["error"] == E.TENANT_YAML_MODE
    denied = [e for e in audit.entries if getattr(e, "status", None) == 409]
    assert len(denied) == 2, "yaml 态误触两笔均记 denied 管线事件"


@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi/httpx")
def test_freeze_unfreeze_db_endpoints_and_quota_rebuild(tmp_path, monkeypatch):
    """db 态冻结/解冻端点（fakes）：两段式→冻结（P0-6 锁内清单全摘）→解冻必填工单号
    →P0-5 全量 quota 重建断言（7/77 原样传给网关）。"""
    monkeypatch.setattr(tw, "_reload", lambda req: {})
    monkeypatch.setattr(mw, "writer_mode", lambda: "db")
    calls = []

    def fake_set_status(*, tenant_id, status, action, operator, oa_ticket=None):
        calls.append(("status", tenant_id, status, action, oa_ticket))
        return {"row": {"tenant_id": tenant_id, "status": status},
                "active_appkeys": ["ak_credfin0000001"]}

    monkeypatch.setattr(mw, "tenant_set_status", fake_set_status)

    async def fake_delete(ak):
        calls.append(("delete", ak, None, None))
        return True

    async def fake_put(ak, *, quota_qps=None, quota_daily=None):
        calls.append(("put", ak, quota_qps, quota_daily))
        return True

    monkeypatch.setattr(tw.gw_consumer, "delete_consumer", fake_delete)
    monkeypatch.setattr(tw.gw_consumer, "put_consumer", fake_put)
    c, audit, mpath, kpath = _client(tmp_path)
    r = c.post("/v1/admin/tenants/t-fin/freeze", json={})          # 第一段：影响预览
    assert r.status_code == 200 and r.json()["confirmRequired"] is True
    assert r.json()["plan"]["activeKeys"] == 1
    r = c.post("/v1/admin/tenants/t-fin/freeze", json={"confirm": True})
    assert r.status_code == 200 and r.json()["frozen"] is True
    assert r.json()["removed"] == 1 and r.json()["partial"] is False
    assert ("status", "t-fin", "frozen", "tenant_freeze", None) in calls
    assert ("delete", "ak_credfin0000001", None, None) in calls
    r = c.post("/v1/admin/tenants/t-fin/unfreeze", json={})         # 缺工单号拒
    assert r.status_code == 422 and r.json()["error"] == E.OA_TICKET_REQUIRED
    r = c.post("/v1/admin/tenants/t-fin/unfreeze", json={"oa_ticket": "bad!"})
    assert r.status_code == 422 and r.json()["error"] == E.INVALID_OA_TICKET
    calls.clear()
    r = c.post("/v1/admin/tenants/t-fin/unfreeze", json={"oa_ticket": "OA-2026-300001"})
    assert r.status_code == 200 and r.json()["unfrozen"] is True and r.json()["restored"] == 1
    assert ("status", "t-fin", "active", "tenant_unfreeze", "OA-2026-300001") in calls
    assert ("put", "ak_credfin0000001", 7, 77) in calls, "P0-5：解冻须带冻结前全量配额"


@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi/httpx")
def test_resync_endpoint_idempotent_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(tw, "_reload", lambda req: {})
    calls = []

    async def fake_put(ak, *, quota_qps=None, quota_daily=None):
        calls.append(("put", ak, quota_qps, quota_daily))
        return True

    async def fake_delete(ak):
        calls.append(("delete", ak, None, None))
        return True

    monkeypatch.setattr(tw.gw_consumer, "put_consumer", fake_put)
    monkeypatch.setattr(tw.gw_consumer, "delete_consumer", fake_delete)
    c, audit, mpath, kpath = _client(tmp_path)
    j = c.post("/v1/admin/tenants/resync-consumers", json={}).json()
    assert {"ensured", "removed", "failed", "partial"} <= set(j) and j["partial"] is False
    n1 = (j["ensured"], j["removed"])
    calls.clear()
    j2 = c.post("/v1/admin/tenants/resync-consumers", json={}).json()
    assert (j2["ensured"], j2["removed"]) == n1, "幂等复跑计数复现"


# ── 4. AST 契约守卫（P1-⑪） ──

def test_tenants_routes_ast_and_role_guard():
    """五端点注册齐全（§1.4）；新增端点漏挂＝红。

    （原 ROUTE_MIN_ROLE 五行断言随 tests/test_auth_oidc.py 于 v0.1.1 登录清理退役，
    无登录模式决策 §7。）"""
    import ast as _ast
    src = (REPO / "service" / "app" / "tenants_api.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    routes = []
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, _ast.Call) and isinstance(dec.func, _ast.Attribute)
                    and isinstance(dec.func.value, _ast.Name) and dec.func.value.id == "router"
                    and dec.args and isinstance(dec.args[0], _ast.Constant)):
                routes.append((dec.func.attr.upper(), str(dec.args[0].value)))
    assert sorted(routes) == sorted([
        ("GET", "/v1/admin/tenants"),
        ("POST", "/v1/admin/tenants"),
        ("POST", "/v1/admin/tenants/resync-consumers"),
        ("POST", "/v1/admin/tenants/{tenant_id}/freeze"),
        ("POST", "/v1/admin/tenants/{tenant_id}/unfreeze"),
    ]), routes
    assert "APIRouter(dependencies=[Depends(_admin_gate)])" in src, "router 须路由级挂闸"


# ── 5. db 模式 e2e（隔离一次性 pg；test_meta_writer_db 先例；docker 不可达 skip） ──

CONTAINER = "oneserve-tenants-test-" + str(os.getpid()) + "-" + uuid.uuid4().hex[:6]
PG_IMAGE = "postgres:17-alpine"
BOOT_SQL = (
    "DO $$ BEGIN"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='meta_app') THEN"
    " CREATE ROLE meta_app LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='meta_ro') THEN"
    " CREATE ROLE meta_ro LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='usage_app') THEN"
    " CREATE ROLE usage_app LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='usage_ro') THEN"
    " CREATE ROLE usage_ro LOGIN; END IF; END $$;"
    "CREATE SCHEMA IF NOT EXISTS meta AUTHORIZATION meta_migrate;"
    "CREATE SCHEMA IF NOT EXISTS usage AUTHORIZATION meta_migrate;")
SRC_FILES = ["services.yaml", "upstreams.yaml", "auth_matrix.yaml"]
STATE = {}


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _psql(db: str, sql: str):
    return subprocess.run(["docker", "exec", "-i", CONTAINER, "psql", "-U", "meta_migrate",
                           "-d", db, "-v", "ON_ERROR_STOP=1", "-A", "-t"],
                          input=sql, capture_output=True, text=True, timeout=120)


def _db_tenant(tid: str) -> dict:
    p = _psql("oneserve_meta",
              "SELECT coalesce(jsonb_agg(row_to_json(t)), '[]'::jsonb) FROM meta.tenant t "
              "WHERE tenant_id = '" + tid + "';")
    assert p.returncode == 0, p.stderr
    rows = __import__("json").loads(p.stdout.strip().splitlines()[-1])
    return rows[0] if rows else {}


def _cli(script: str, *args):
    return subprocess.run([sys.executable, str(REPO / "scripts" / script), *args],
                          capture_output=True, text=True, env=dict(os.environ),
                          cwd=str(REPO), timeout=180)


@pytest.fixture(scope="module")
def pgstack():
    """一次性 pg 容器＋角色/schema/DDL＋迁移装载＋快照基线（module 级全链就绪）。"""
    if not _docker_available():
        pytest.skip("docker 不可达（hermetic 降级跳过）")
    img = subprocess.run(["docker", "images", "-q", PG_IMAGE], capture_output=True, text=True)
    if not img.stdout.strip():
        pytest.skip("镜像未就位：" + PG_IMAGE)
    up = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", "POSTGRES_USER=meta_migrate", "-e", "POSTGRES_DB=oneserve_meta",
         "-e", "POSTGRES_PASSWORD=tenants-e2e-test", PG_IMAGE],
        capture_output=True, text=True)
    assert up.returncode == 0, up.stderr
    saved = {k: os.environ.get(k) for k in
             ("META_WRITER", "META_WRITER_PSQL_BASE", "META_MIGRATE_PSQL_BASE",
              "META_MIGRATE_SRC_DIR", "META_SNAPSHOT_DIR")}
    try:
        for _ in range(30):
            if subprocess.run(["docker", "exec", CONTAINER, "pg_isready",
                               "-U", "meta_migrate", "-d", "oneserve_meta"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("一次性 pg 30s 未就绪")
        _boot_stack()
        yield STATE
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


def _boot_stack():
    base = Path(tempfile.mkdtemp(prefix="tenants-e2e-"))
    src = base / "src"
    (src / "registry").mkdir(parents=True)
    for name in SRC_FILES:
        (src / name).write_text((REPO / "service" / name).read_text(encoding="utf-8"),
                                encoding="utf-8")
    (src / "registry" / "appkeys.yaml").write_text(
        (REPO / "service" / "registry" / "appkeys.yaml").read_text(encoding="utf-8"),
        encoding="utf-8")
    snap = base / "snap"
    os.environ["META_WRITER"] = "db"
    os.environ["META_WRITER_PSQL_BASE"] = (
        "docker exec -i " + CONTAINER +
        " psql -U meta_app -d oneserve_meta -v ON_ERROR_STOP=1 -A -t -P footer=off")
    os.environ["META_MIGRATE_PSQL_BASE"] = (
        "docker exec -i " + CONTAINER +
        " psql -U meta_migrate -d oneserve_meta -v ON_ERROR_STOP=1 -A -t -P footer=off")
    os.environ["META_MIGRATE_SRC_DIR"] = str(src)
    os.environ["META_SNAPSHOT_DIR"] = str(snap)
    mw._channel_inst = None
    mw._svc_ids_cache = None
    mw._proxy_ids_cache = None
    r = _psql("oneserve_meta", BOOT_SQL)
    assert r.returncode == 0, r.stderr
    for mig in sorted((REPO / "migrations").glob("*.sql")):
        r = _psql("oneserve_meta", mig.read_text(encoding="utf-8"))
        assert r.returncode == 0, mig.name + ": " + r.stderr[-600:]
    r = _cli("meta_migrate.py", "apply")
    assert r.returncode == 0, r.stdout + r.stderr
    r = _cli("meta_reconcile.py", "--baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    STATE.update({"src": src, "snap": snap, "base": base})


def _e2e_services() -> dict:
    """规划器服务目录 → grants_admin 形状（e2e 授权登记用；不读 dev 真文件）。"""
    rows, _stats = mw._mig().plan_service_catalog()
    out = {}
    for r in rows:
        names = [(f.get("name") if isinstance(f, dict) else str(f)) for f in (r.get("fields") or [])]
        out[r["service_id"]] = {"name": r.get("name") or r["service_id"],
                                "route": r.get("route") or "", "status": "published",
                                "field_space": r.get("field_space") or "wide297",
                                "fields": [str(n) for n in names if n]}
    return out


def test_db_e2e_freeze_unfreeze_roundtrip(pgstack, monkeypatch):
    """1.10-6（服务层往返，网关侧 fake _req 捕获）：新建正式租户→授权登记→签发凭证
    （带配额）→冻结（DB frozen＋快照戳＋台账）→解冻（工单号）→consumer 重建体携
    冻结前全量配额（P0-5「解冻后配额仍在」断言）。"""
    os.environ["META_WRITER"] = "db"   # autouse _yaml_mode 会删 env，e2e 显式钉回
    snap = pgstack["snap"]
    mpath = snap / "auth_matrix.yaml"
    keys_path = snap / "registry" / "appkeys.yaml"
    services = _e2e_services()
    assert services, "规划器服务目录为空（基线装载异常）"
    sid = next(s for s in sorted(services) if services[s]["fields"] or services[s].get("route"))
    fields_pick = list(services[sid]["fields"])[:1] or ["any"]
    row = mw.tenant_upsert(tenant_id="t-e2e", display_name="端到端样本",
                           tenant_type="external", oa_ticket_open="OA-2026-400001",
                           remark="e2e-remark-keep", operator="demo")
    assert row["status"] == "active"
    assert mw.tenant_exists("t-e2e")
    view, diff = gam.apply_grant_action(mpath, services, [],
        {"action": "upsert", "tenant": "t-e2e", "service": sid, "fields": fields_pick,
         "oa_ticket": "OA-2026-400002"}, "demo")
    assert diff["addedService"] is True
    doc = appk.load_appkeys(keys_path)
    matrix = gam.load_matrix_file(mpath)["matrix"]
    rec, _secret = appk.issue_appkey(doc, matrix, services, tenant="t-e2e",
                                     app_name="e2e应用", key_type="formal",
                                     oa_ticket="OA-2026-400003",
                                     quota_qps=7, quota_daily=77)
    doc["keys"].append(rec)                        # 调用方追加（admin_api 端点流同款）
    mw.credential_save(keys_path, doc)
    res = mw.tenant_set_status(tenant_id="t-e2e", status="frozen",
                               action=mw.TENANT_FREEZE_ACTION, operator="demo")
    assert rec["appkey"] in res["active_appkeys"], "锁内清单须含冻结前已签发凭证"
    assert _db_tenant("t-e2e")["status"] == "frozen"
    srow = gam._matrix_row(gam.load_matrix_file(mpath)["matrix"], "t-e2e")
    assert srow.get("tenant_status") == "frozen", "快照须携生命周期戳（P0-3 随迁）"
    p = _psql("oneserve_meta",
              "SELECT count(*) FROM meta.admin_event WHERE action='tenant_freeze'"
              " AND tenant_id='t-e2e';")
    assert p.stdout.strip().splitlines()[-1] == "1"
    sent = []

    async def fake_put(ak, *, quota_qps=None, quota_daily=None):
        from app import ratelimit as rl
        sent.append(rl.consumer_body(ak, quota_qps=quota_qps, quota_daily=quota_daily))
        return True

    monkeypatch.setattr(tw.gw_consumer, "put_consumer", fake_put)
    mw.tenant_set_status(tenant_id="t-e2e", status="active",
                         action=mw.TENANT_UNFREEZE_ACTION, operator="demo",
                         oa_ticket="OA-2026-400004")
    assert _db_tenant("t-e2e")["status"] == "active"
    keys = appk.load_appkeys(keys_path)["keys"]
    mine = [k for k in keys if k.get("tenant") == "t-e2e" and k.get("status") == "active"]
    assert mine and mine[0]["quota_qps"] == 7 and mine[0]["quota_daily"] == 77
    asyncio.run(tw._sync_consumers(gam.load_matrix_file(mpath)["matrix"], keys, "t-e2e"))
    puts = [b for b in sent if b and b.get("username") == rec["appkey"]]
    assert puts, "解冻须重建 consumer"
    rules = puts[-1]["plugins"]["limit-count"]["rules"]
    assert rules[0]["count"] == 7 and rules[1]["count"] == 77,         "P0-5：解冻后网关限流配额须与冻结前一致（不传 quota＝清成无限流的回归防线）"


def test_db_e2e_frozen_survives_grant_registration(pgstack):
    """1.10-7 回归：frozen 租户在其他租户授权登记（matrix_save 全量重规划）后
    仍 frozen＋remark 不丢——P0-3 护列（矩阵写径只 upsert 矩阵自有列）＋戳读回。"""
    os.environ["META_WRITER"] = "db"   # autouse _yaml_mode 会删 env，e2e 显式钉回
    snap = pgstack["snap"]
    mpath = snap / "auth_matrix.yaml"
    services = _e2e_services()
    sid = next(s for s in sorted(services) if services[s]["fields"] or services[s].get("route"))
    fields_pick = list(services[sid]["fields"])[:1] or ["any"]
    mw.tenant_upsert(tenant_id="t-keep", display_name="冻结保持样本",
                     tenant_type="external", oa_ticket_open="OA-2026-410001",
                     remark="keep-my-remark", operator="demo")
    mw.tenant_upsert(tenant_id="t-act", display_name="活跃登记方",
                     tenant_type="external", oa_ticket_open="OA-2026-410002",
                     remark="active-remark", operator="demo")
    mw.tenant_set_status(tenant_id="t-keep", status="frozen",
                         action=mw.TENANT_FREEZE_ACTION, operator="demo")
    view, diff = gam.apply_grant_action(mpath, services, [],
        {"action": "upsert", "tenant": "t-act", "service": sid, "fields": fields_pick,
         "oa_ticket": "OA-2026-410003"}, "demo")
    assert diff["addedService"] is True
    keep = _db_tenant("t-keep")
    assert keep["status"] == "frozen", "授权登记不得静默解冻（P0-3 回归）"
    assert keep["remark"] == "keep-my-remark", "授权登记不得抹备注（P0-3 回归）"
    act = _db_tenant("t-act")
    assert act["status"] == "active" and act["remark"] == "active-remark"
    srow = gam._matrix_row(gam.load_matrix_file(mpath)["matrix"], "t-keep")
    assert srow.get("tenant_status") == "frozen"


def test_db_e2e_frozen_issue_blocked_by_db_gate(pgstack):
    """P0-6 另一半：db 守卫哨兵——冻结租户的凭证写径整事务拒绝（tenant_frozen_gate），
    tenant_gate_code 识别映射 tenant_frozen；授权页 stamps 门先行 409。"""
    os.environ["META_WRITER"] = "db"   # autouse _yaml_mode 会删 env，e2e 显式钉回
    snap = pgstack["snap"]
    mpath = snap / "auth_matrix.yaml"
    keys_path = snap / "registry" / "appkeys.yaml"
    services = _e2e_services()
    sid = next(s for s in sorted(services) if services[s]["fields"] or services[s].get("route"))
    fields_pick = list(services[sid]["fields"])[:1] or ["any"]
    mw.tenant_upsert(tenant_id="t-gate", display_name="闸门样本",
                     tenant_type="external", oa_ticket_open="OA-2026-420001",
                     remark="", operator="demo")
    mw.tenant_set_status(tenant_id="t-gate", status="frozen",
                         action=mw.TENANT_FREEZE_ACTION, operator="demo")
    matrix = gam.load_matrix_file(mpath)["matrix"]
    with pytest.raises(gam.GrantAdminError) as ei:
        gam.apply_grant_action(mpath, services, [],
            {"action": "upsert", "tenant": "t-gate", "service": sid, "fields": fields_pick,
             "oa_ticket": "OA-2026-420002"}, "demo")
    assert ei.value.status == 409 and ei.value.code == "tenant_frozen"
    # 签发族 stamps 门（服务层）：快照矩阵带 frozen 戳 → 签发即拒
    doc2 = appk.load_appkeys(keys_path)
    with pytest.raises(appk.AppkeyIssueError) as ei:
        appk.issue_appkey(doc2, matrix, services, tenant="t-gate",
                          app_name="should-fail", key_type="formal",
                          oa_ticket="OA-2026-420003",
                          quota_qps=1, quota_daily=1)
    assert ei.value.code == "tenant_frozen"
    # db 守卫（P0-6 事务内核）：剥离 stamps 的底稿直发（模拟并发窗口）→ 凭证写径事务拒绝
    row_nostamp = {k: v for k, v in (gam._matrix_row(matrix, "t-gate") or {}).items()
                   if k != "tenant_status"}
    matrix_nostamp = [r for r in matrix if r.get("tenant") != "t-gate"] + [row_nostamp]
    rec, _secret = appk.issue_appkey(doc2, matrix_nostamp, services, tenant="t-gate",
                                     app_name="should-fail", key_type="formal",
                                     oa_ticket="OA-2026-420003",
                                     quota_qps=1, quota_daily=1,
                                     grants=[{"service": sid, "fields": fields_pick}])
    doc2["keys"].append(rec)
    with pytest.raises(mw.MetaDbError) as ei:
        mw.credential_save(keys_path, doc2)
    assert mw.tenant_gate_code(ei.value) == "tenant_frozen"
    p = _psql("oneserve_meta",
              "SELECT count(*) FROM meta.credential WHERE tenant_id='t-gate' AND deleted_at IS NULL;")
    assert p.stdout.strip().splitlines()[-1] == "0"


# ── 6. 防回潮杂项 ──

def test_main_py_mounted_by_closing_batch():
    """收口批兑现（本用例由「禁碰 main.py」防回潮断言反转）：main.py 已挂载 tenants_api。
    （A 节批 b039a55 挂载归收口批——收口批 2026-10-21 接线，本断言锚点防漏挂回潮。）"""
    main_src = (REPO / "service" / "app" / "main.py").read_text(encoding="utf-8")
    assert "from app import tenants_api" in main_src, "收口批须 import tenants_api"
    assert "app.include_router(tenants_api.router)" in main_src, \
        "收口批须挂载 /v1/admin/tenants/*（b039a55 遗留项兑现）"
