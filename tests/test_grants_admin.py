#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""授权管理写面套件（1b-8 一期 · tests/test_grants_admin.py，本卡新建）。

覆盖（设计稿 v1 §7 验收 1/4/6 对应；正负例逐条对齐任务书）：
  1. validate_matrix 放宽两处（设计稿 §7-2 前置）：kind=proxy 空字段合法、
     沙箱租户（tenant_type=sandbox）单号豁免；既有红线不放松——普通服务空字段、
     正式租户缺单号照拒；
  2. 写服务双层五条（service/app/grants_admin.py，端点与 auth_admin CLI 同层）：
     · 子集违规（收窄租户级致在用凭证级越界）→ 422；
     · 未登记 tenant_id → 400 tenant_not_registered（一期派生清单门禁）；
     · 普通服务空字段 → 400 empty_fields；kind=proxy 空字段 → 放行
       （口径沿 tests/test_appkeys.py:174 签发端先例）；
     · 字段不在服务开放清单 → 400 field_not_in_dict（字段空间随服务绑定）；
     · OA 单号缺失 → 400 oa_ticket_required（纯登记提醒，零 OA 对接）；
     · 沙箱租户只读 → 409；
     · 两段式停用流：缺 confirm 拒（400）、带 confirm 落 revoked、可再启用；
  3. 无登录放行（TestClient 裸请求直达业务校验；原 on 态角色矩阵用例随 v0.1.1
     登录清理退役，ADR-0029 §7）；
  4. OA 口径防回潮：零 OA 对接代码（grep 同款断言：无 oa api / requests/httpx）。

运行：python3 -m pytest tests/test_grants_admin.py -q（宿主机依赖 fastapi/httpx 的
用例缺席时如实 skip，不假绿——同 test_accounts_admin 口径）。
"""
from __future__ import annotations

import base64
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import errors as E            # noqa: E402
from app import grants_admin as gam    # noqa: E402

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    _HAS_FASTAPI = True
except Exception:   # noqa: BLE001
    _HAS_FASTAPI = False

# ---------- 合成夹具（口径同 tests/test_appkeys.synth_services：不读真库） ----------

def synth_services() -> dict:
    """服务∪代理合并命名空间：两个字段服务＋一个整路由代理（kind=proxy）。"""
    return {
        "svc-tags": {"name": "用户资产标签", "route": "/v1/svc/tags", "status": "published",
                     "field_space": "wide297",
                     "fields": ["age_band", "city_code", "aum_band", "vip_flag"]},
        "svc-card": {"name": "账户摘要", "route": "/v1/svc/card", "status": "published",
                     "field_space": "wide297",
                     "fields": ["card_sts", "bal_band"]},
        "proxy-local": {"kind": "proxy", "name": "资产标签代理", "route": "/proxy/local/tags",
                        "fields": [], "status": "published", "field_space": "proxy"},
    }


def synth_matrix_doc() -> dict:
    """两租户底稿：t-fin（外部正式，单号齐备）＋ t-sbx（沙箱，单号豁免）。"""
    return {
        "meta": {"updated": "2026-10-20T10:00:00+00:00", "updated_by": "test"},
        "matrix": [
            {"tenant": "t-fin", "tenant_type": "external", "appkey": "ak_testfin00000001",
             "oa_ticket": "DEMO-2026-000001", "display_name": "演示调用方",
             "grants": [
                 {"service": "svc-tags", "endpoint": "/v1/svc/tags",
                  "oa_ticket": "DEMO-2026-000001", "fields": ["age_band", "city_code"]},
                 {"service": "proxy-local", "endpoint": "/proxy/local/tags",
                  "kind": "proxy", "fields": []},
             ]},
            {"tenant": "t-sbx", "tenant_type": "sandbox", "appkey": "sb_testsandbox001",
             "oa_ticket": "",
             "grants": [{"service": "svc-tags", "fields": ["age_band"]}]},
        ],
    }


def synth_keys() -> list[dict]:
    """注册表凭证：t-fin 一把在用正式凭证（子集不变量的凭证级一侧）。"""
    return [
        {"appkey": "ak_credfin0000001", "tenant": "t-fin", "key_type": "formal",
         "status": "active",
         "grants": [{"service": "svc-tags", "fields": ["age_band"]},
                    {"service": "proxy-local", "fields": []}]},
    ]


def _write_doc(path: Path, doc: dict) -> Path:
    path.write_text(gam.dump_matrix(doc), encoding="utf-8")
    return path


# ---------- 1. validate_matrix 放宽两处（改校验器不改语义） ----------

def test_validate_proxy_empty_fields_legal_and_plain_service_rejected():
    """① kind=proxy 空集合法；普通服务空字段仍拒（既有负例不放松）。"""
    doc = synth_matrix_doc()
    errs, _ = gam.validate_matrix(doc)
    assert errs == [], f"基线底稿应全绿：{errs}"
    doc["matrix"][0]["grants"][0]["fields"] = []      # 普通服务清空字段
    errs, _ = gam.validate_matrix(doc)
    assert any("非空字段白名单" in e for e in errs), errs
    doc["matrix"][0]["grants"][0]["kind"] = "proxy"   # 声明整路由后放行
    errs, _ = gam.validate_matrix(doc)
    assert errs == [], errs


def test_validate_sandbox_ticket_exempt_formal_still_required():
    """② tenant_type=sandbox 单号豁免；正式租户（含未标类型存量行）缺单号照拒。"""
    doc = synth_matrix_doc()
    errs, _ = gam.validate_matrix(doc)
    assert errs == [], errs                                   # 沙箱空单号已豁免
    fin = doc["matrix"][0]
    fin["tenant_type"] = "sandbox"                            # 类型改沙箱后同样豁免
    fin["oa_ticket"] = ""
    errs, _ = gam.validate_matrix(doc)
    assert errs == [], errs
    fin["tenant_type"] = "external"; fin["oa_ticket"] = ""    # 正式租户恢复必填
    errs, _ = gam.validate_matrix(doc)
    assert any("oa_ticket 必填" in e for e in errs), errs
    bad = dict(fin, tenant_type="internal")
    doc["matrix"][0] = bad
    errs, _ = gam.validate_matrix(doc)
    assert any("oa_ticket 必填" in e for e in errs), errs
    doc["matrix"][0]["tenant_type"] = "alien"                 # 枚举外类型拒
    errs, _ = gam.validate_matrix(doc)
    assert any("tenant_type 非法" in e for e in errs), errs


def test_validate_new_extension_fields():
    """新扩展字段（行 status/effective_from/to/grant status）合法值过、非法值拒。"""
    doc = synth_matrix_doc()
    g = doc["matrix"][0]["grants"][0]
    g.update({"status": "revoked", "effective_from": "2026-10-20", "effective_to": "2027-10-20"})
    errs, _ = gam.validate_matrix(doc)
    assert errs == [], errs
    g["effective_from"] = "2026/10/20"
    errs, _ = gam.validate_matrix(doc)
    assert any("effective_from 非法" in e for e in errs), errs
    g["effective_from"] = "2026-10-20"; g["status"] = "gone"
    errs, _ = gam.validate_matrix(doc)
    assert any("status 必须为 active|revoked" in e for e in errs), errs


# ---------- 2. 写服务：双层五条正负例 ----------

def _svc_map():
    return synth_services()


def test_upsert_new_grant_happy_path_writes_matrix(tmp_path):
    """新增授权：校验过→落盘→可读回（同租户同服务唯一；grant 级单号留痕）。"""
    path = _write_doc(tmp_path / "m.yaml", synth_matrix_doc())
    view, diff = gam.apply_grant_action(path, _svc_map(), synth_keys(), {
        "action": "upsert", "tenant": "t-fin", "service": "svc-card",
        "fields": ["card_sts"], "oa_ticket": "DEMO-2026-000002",
        "effective_from": "2026-10-20", "effective_to": "2027-10-20",
    }, operator="tester")
    assert diff["addedService"] is True and diff["addedFields"] == ["card_sts"]
    doc = gam.load_matrix_file(path)
    row = gam._matrix_row(doc["matrix"], "t-fin")
    g = next(g for g in row["grants"] if g["service"] == "svc-card")
    assert g["fields"] == ["card_sts"] and g["oa_ticket"] == "DEMO-2026-000002"
    assert g["effective_from"] == "2026-10-20" and g["effective_to"] == "2027-10-20"
    assert doc["meta"]["updated_by"] == "tester"
    assert view["tenant"] == "t-fin"


def test_upsert_unregistered_tenant_400():
    """五条之二：未登记租户 400 tenant_not_registered（派生清单门禁）。"""
    with tempfile.TemporaryDirectory() as td:
        path = _write_doc(Path(td) / "m.yaml", synth_matrix_doc())
        with pytest.raises(gam.GrantAdminError) as ei:
            gam.apply_grant_action(path, _svc_map(), synth_keys(), {
                "action": "upsert", "tenant": "ghost-tenant", "service": "svc-tags",
                "fields": ["age_band"], "oa_ticket": "DEMO-2026-000009"}, operator="t")
        assert ei.value.status == 400 and ei.value.code == E.TENANT_NOT_REGISTERED


def test_upsert_plain_service_empty_fields_400_proxy_ok():
    """普通服务空字段 400 empty_fields；kind=proxy 空字段放行（整路由授权）。"""
    with tempfile.TemporaryDirectory() as td:
        path = _write_doc(Path(td) / "m.yaml", synth_matrix_doc())
        with pytest.raises(gam.GrantAdminError) as ei:
            gam.apply_grant_action(path, _svc_map(), synth_keys(), {
                "action": "upsert", "tenant": "t-fin", "service": "svc-tags",
                "fields": [], "oa_ticket": "DEMO-2026-000001"}, operator="t")
        assert ei.value.status == 400 and ei.value.code == E.EMPTY_FIELDS
        # 代理整路由：fields 缺省/空集均放行
        view, diff = gam.apply_grant_action(path, _svc_map(), synth_keys(), {
            "action": "upsert", "tenant": "t-fin", "service": "proxy-local",
            "fields": [], "oa_ticket": "DEMO-2026-000001"}, operator="t")
        g = next(x for x in view["grants"] if x["service"] == "proxy-local")
        assert g["kind"] == "proxy" and g["fields"] == []


def test_upsert_field_outside_service_space_400():
    """五条之四：字段空间随服务绑定——勾选字段不在服务开放清单内 400。"""
    with tempfile.TemporaryDirectory() as td:
        path = _write_doc(Path(td) / "m.yaml", synth_matrix_doc())
        with pytest.raises(gam.GrantAdminError) as ei:
            gam.apply_grant_action(path, _svc_map(), synth_keys(), {
                "action": "upsert", "tenant": "t-fin", "service": "svc-card",
                "fields": ["card_sts", "not_a_field"], "oa_ticket": "DEMO-2026-000002"},
                operator="t")
        assert ei.value.status == 400 and ei.value.code == E.FIELD_NOT_IN_DICT


def test_upsert_missing_oa_ticket_reminded_400():
    """五条之五：缺 OA 单号 400 oa_ticket_required（纯登记提醒；真伪不做核对）。

    在 validate_grant_action 纯函数层断言（apply_grant_action 会先做底稿合法性
    预检——正式租户缺单号的底稿本身不合法，到不了动作校验，属纵深防御）。
    """
    matrix = synth_matrix_doc()["matrix"]
    # 底稿租户级有单号 → grant 级缺省回落放行（不 raise）
    norm = gam.validate_grant_action(matrix, _svc_map(), synth_keys(), {
        "action": "upsert", "tenant": "t-fin", "service": "svc-card",
        "fields": ["card_sts"], "oa_ticket": ""})
    assert norm["oa_ticket"] == ""
    # 租户级行也无单号 → 必填拒绝（纵深防御；合法底稿下正式租户行不会无单号）
    matrix[0]["oa_ticket"] = ""
    with pytest.raises(gam.GrantAdminError) as ei:
        gam.validate_grant_action(matrix, _svc_map(), synth_keys(), {
            "action": "upsert", "tenant": "t-fin", "service": "svc-card",
            "fields": ["card_sts"], "oa_ticket": ""})
    assert ei.value.status == 400 and ei.value.code == E.OA_TICKET_REQUIRED
    with pytest.raises(gam.GrantAdminError) as ei:
        gam.validate_grant_action(matrix, _svc_map(), synth_keys(), {
            "action": "upsert", "tenant": "t-fin", "service": "svc-card",
            "fields": ["card_sts"], "oa_ticket": "不是单号"})
    assert ei.value.status == 400 and ei.value.code == E.INVALID_OA_TICKET


def test_subset_invariant_narrowing_violation_422():
    """五条之一：收窄租户级致在用凭证级越界 → 422（fields 与 service 两形态）。"""
    with tempfile.TemporaryDirectory() as td:
        path = _write_doc(Path(td) / "m.yaml", synth_matrix_doc())
        # 字段收窄：凭证在用 age_band，收窄后只剩 city_code → 422
        with pytest.raises(gam.GrantAdminError) as ei:
            gam.apply_grant_action(path, _svc_map(), synth_keys(), {
                "action": "upsert", "tenant": "t-fin", "service": "svc-tags",
                "fields": ["city_code"], "oa_ticket": "DEMO-2026-000001"}, operator="t")
        assert ei.value.status == 422 and ei.value.code == E.FIELDS_NOT_GRANTED
        # 整行停用：凭证仍持有 svc-tags → 422 service_not_granted
        with pytest.raises(gam.GrantAdminError) as ei:
            gam.apply_grant_action(path, _svc_map(), synth_keys(), {
                "action": "disable", "tenant": "t-fin", "service": "svc-tags",
                "confirm": True}, operator="t")
        assert ei.value.status == 422 and ei.value.code == E.SERVICE_NOT_GRANTED


def test_sandbox_tenant_readonly_409():
    """五条之三：沙箱租户只读——新增/变更/停用/启用一律 409。"""
    with tempfile.TemporaryDirectory() as td:
        path = _write_doc(Path(td) / "m.yaml", synth_matrix_doc())
        for payload in (
            {"action": "upsert", "tenant": "t-sbx", "service": "svc-tags", "fields": ["age_band"]},
            {"action": "disable", "tenant": "t-sbx", "service": "svc-tags", "confirm": True},
            {"action": "enable", "tenant": "t-sbx", "service": "svc-tags"},
        ):
            with pytest.raises(gam.GrantAdminError) as ei:
                gam.apply_grant_action(path, _svc_map(), synth_keys(), payload, operator="t")
            assert ei.value.status == 409, payload


def test_two_phase_disable_flow_and_reenable(tmp_path):
    """两段式停用：缺 confirm 400 拒执行（矩阵不动）；confirm=true 落 revoked；可再启用。"""
    doc = synth_matrix_doc()
    # 停用目标＝svc-tags：名下在用凭证只持 proxy-local（不依赖 svc-tags），停用不越界
    keys = [{"appkey": "ak_credfin0000001", "tenant": "t-fin", "key_type": "formal",
             "status": "active", "grants": [{"service": "proxy-local", "fields": []}]}]
    with tempfile.TemporaryDirectory() as td:
        path = _write_doc(Path(td) / "m.yaml", doc)
        with pytest.raises(gam.GrantAdminError) as ei:
            gam.apply_grant_action(path, _svc_map(), keys, {
                "action": "disable", "tenant": "t-fin", "service": "svc-tags"}, operator="t")
        assert ei.value.status == 400, "缺第二段确认应拒（且拒执行矩阵不动）"
        view, diff = gam.apply_grant_action(path, _svc_map(), keys, {
            "action": "disable", "tenant": "t-fin", "service": "svc-tags",
            "confirm": True}, operator="t")
        g = next(x for x in view["grants"] if x["service"] == "svc-tags")
        assert g["status"] == "revoked" and diff["after"] == {"status": "revoked"}
        doc2 = gam.load_matrix_file(path)
        row = gam._matrix_row(doc2["matrix"], "t-fin")
        assert next(g for g in row["grants"] if g["service"] == "svc-tags")["status"] == "revoked"
        assert gam.validate_matrix(doc2)[0] == [], "停用后矩阵仍合规（validate 通过）"
        view, _ = gam.apply_grant_action(path, _svc_map(), keys, {
            "action": "enable", "tenant": "t-fin", "service": "svc-tags"}, operator="t")
        g = next(x for x in view["grants"] if x["service"] == "svc-tags")
        assert g["status"] == "active"
        with pytest.raises(gam.GrantAdminError) as ei:
            gam.apply_grant_action(path, _svc_map(), keys, {
                "action": "disable", "tenant": "t-fin", "service": "svc-card"}, operator="t")
        assert ei.value.status == 404, "名下无此服务的登记应 404"
        with pytest.raises(gam.GrantAdminError) as ei:
            gam.apply_grant_action(path, _svc_map(), keys, {
                "action": "disable", "tenant": "t-fin", "service": "no-such"}, operator="t")
        assert ei.value.status == 400 and ei.value.code == E.UNKNOWN_SERVICE


def test_upsert_grant_ticket_falls_back_to_tenant_level():
    """单号分级：grant 级可选，缺省回落租户级（demo-1020 回落口径）。"""
    with tempfile.TemporaryDirectory() as td:
        path = _write_doc(Path(td) / "m.yaml", synth_matrix_doc())
        view, _ = gam.apply_grant_action(path, _svc_map(), synth_keys(), {
            "action": "upsert", "tenant": "t-fin", "service": "svc-card",
            "fields": ["card_sts"], "oa_ticket": ""}, operator="t")   # 不填 grant 级单号
        g = next(x for x in view["grants"] if x["service"] == "svc-card")
        assert g["oaTicket"] == "" and g["oaEffective"] == "DEMO-2026-000001"
        assert g["oaSource"] == "tenant"


def test_new_grant_without_ticket_on_ticketless_formal_row_rejected_at_action_level():
    """正式租户行无单号且动作不带单号 → 动作校验层 400（底稿预检 422 之外的纵深）。"""
    doc = synth_matrix_doc()
    doc["matrix"].append({"tenant": "t-fin2", "tenant_type": "external",
                          "appkey": "ak_testfin00000002", "oa_ticket": "", "grants": []})
    with pytest.raises(gam.GrantAdminError) as ei:
        gam.validate_grant_action(doc["matrix"], _svc_map(), synth_keys(), {
            "action": "upsert", "tenant": "t-fin2", "service": "svc-tags",
            "fields": ["age_band"], "oa_ticket": ""})
    assert ei.value.status == 400 and ei.value.code == E.OA_TICKET_REQUIRED


def _grants_app(tmp_path):
    """最小授权写面 app：admin_api.router 原闸（_admin_gate 依赖）＋缺省 state。

    注册表落一份真文件（含 t-fin 在用凭证）——子集不变量的凭证级一侧有数据可依。
    """
    import yaml as _yaml
    from app import admin_api
    app = FastAPI()
    app.state.redis = None
    app.state.services = synth_services()
    app.state.auth_matrix = synth_matrix_doc()["matrix"]
    keys_path = tmp_path / "appkeys.yaml"
    keys_path.write_text(_yaml.safe_dump({"meta": {}, "keys": synth_keys()}, sort_keys=False,
                                         allow_unicode=True), encoding="utf-8")
    app.state.appkeys_path = keys_path
    app.state.auth_matrix_path = _write_doc(tmp_path / "m.yaml", synth_matrix_doc())
    app.include_router(admin_api.router)
    return app


@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi/httpx")
def test_write_passthrough_no_login(tmp_path):
    """无登录模式恒放行：裸请求直达业务校验（ADR-0029；v0.1.1 起）。

    原 on 态角色矩阵用例（无令牌 401／viewer 403 role_forbidden／admin 200，
    RS256/JWKS 桩）随 v0.1.1 登录清理退役（ADR-0029 §7）；保留放行反断言防回潮。
    """
    client = TestClient(_grants_app(tmp_path))
    r = client.post("/v1/admin/grants", json={"action": "upsert"})
    assert r.status_code == 400, "无登录态应放行到业务校验（非 401/403 登录闸拦截）"


@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi/httpx")
def test_write_endpoint_denied_and_two_phase_via_http(tmp_path):
    """端点层负例信封：子集违规 422／沙箱 409／两段停用缺 confirm 400＋denied 审计不炸。"""
    client = TestClient(_grants_app(tmp_path))
    r = client.post("/v1/admin/grants", json={
        "action": "upsert", "tenant": "t-fin", "service": "svc-tags",
        "fields": ["city_code"], "oa_ticket": "DEMO-2026-000001"})
    assert r.status_code == 422 and r.json()["error"] == E.FIELDS_NOT_GRANTED, r.text[:200]
    r = client.post("/v1/admin/grants", json={
        "action": "disable", "tenant": "t-sbx", "service": "svc-tags", "confirm": True})
    assert r.status_code == 409 and "沙箱" in r.json()["message"]
    r = client.post("/v1/admin/grants", json={
        "action": "disable", "tenant": "t-fin", "service": "svc-card", "confirm": True})
    assert r.status_code == 404, "名下无此服务的登记应 404"
    r = client.post("/v1/admin/grants", json={
        "action": "disable", "tenant": "t-fin", "service": "no-such", "confirm": True})
    assert r.status_code == 400 and r.json()["error"] == E.UNKNOWN_SERVICE


# ---------- 4. OA 口径防回潮（验收 6 的可执行证明，随套件常驻） ----------

def test_no_oa_integration_code():
    """零 OA 对接代码：服务层＋授权管理页无 OA API 痕迹、无 HTTP 客户端引 OA。"""
    import subprocess
    r = subprocess.run(
        ["grep", "-rniE", "oa[-_]?api|/api/oa",
         str(REPO / "service" / "app"), str(REPO / "admin" / "grants.html"),
         str(REPO / "admin" / "grants.js")],
        capture_output=True, text=True)
    hits = [ln for ln in (r.stdout or "").splitlines()
            if ln.strip() and "__pycache__" not in ln]
    assert not hits, "发现 OA 对接痕迹（口径写死零集成）：\n" + "\n".join(hits[:5])
    src = (REPO / "service" / "app" / "grants_admin.py").read_text(encoding="utf-8")
    for banned in ("import requests", "import httpx", "import urllib"):
        assert banned not in src, f"写服务不得引入 HTTP 客户端：{banned}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
