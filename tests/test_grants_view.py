#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-8 授权登记全景套件（后端 · tests/test_grants_view.py）——纯 python 直跑或 pytest 收集均可。

守卫口径（任务卡：全景/反查/OA 溯源/空态如实；SPEC §2-C2/§5-P1-8、ADR-0003）：
  1. 全景：租户×appkey×服务×字段逐行对照 service/auth_matrix.yaml 真源（无遗漏无发明）；
     facets/summary 自洽；完整 appkey 一律不进 JSON 出参（掩码＋kid 纪律）；
  2. 反查「这个字段给了谁」：字段精确命中/未命中/空字段三态；命中行带租户/服务/单号，
     未命中＝found=false 如实空态（字段未授权＝经任何接口零出现，不编造持有方）；
  3. OA 溯源：tenant 级与 grant 级两个登记位都参与匹配；grant 级优先且 oaSource 如实标注；
     未命中/空单号＝found=false；
  4. 如实口径：缺单号＝missing（不代填假值）、占位单号＝placeholder（演示占位≠合规单号）；
     缺单号条目不进 OA 单号索引；空矩阵＝全零空态；
  5. 服务标识拆分：'user_tag_query@v1' → (id, v1)；无 @ 版本位如实留空；
  6. 容错只读：matrix/entry/grants/fields 非预期类型跳过不抛（合规全景不被脏数据打断）；
  7. 胶水机检（宿主机无 fastapi，同 test_quota_usage 口径）：grants_api.py py_compile ＋
     AST 契约（三条只读路由/参数/委托 auth_view 纯函数/空参 400 人话）；main.py 挂载 router。

被测对象 service/app/auth_view.py 不依赖 FastAPI/redis（分层纪律同 my_access.py）。
运行：python3 -m pytest tests/test_grants_view.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import ast
import json
import py_compile
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import auth_view as av   # noqa: E402

AUTH_MATRIX = REPO / "service" / "auth_matrix.yaml"


def _real_matrix() -> list:
    return yaml.safe_load(AUTH_MATRIX.read_text(encoding="utf-8"))["matrix"]


def _entry(tenant="t1", appkey="oneserve-dev-appkey-xyz",
           oa_ticket="DEMO-2026-0001", grants=None, **extra) -> dict:
    e = {"tenant": tenant, "appkey": appkey, "oa_ticket": oa_ticket,
         "grants": grants if grants is not None else [
             {"service": "user_tag_query", "endpoint": "/v1/tags/query",
              "fields": ["ac_active_sts_01", "mk_touch_sts_02"]}]}
    e.update(extra)
    return e


# ---------- 1. 全景：真源对照＋计数自洽＋掩码纪律 ----------

def test_panorama_rows_match_auth_matrix_source():
    """每行对照真源：租户/服务/字段/端点/单号逐条一致（无遗漏无发明）。"""
    matrix = _real_matrix()
    p = av.panorama(matrix)
    assert len(p["rows"]) == sum(len(e["grants"]) for e in matrix)
    for e in matrix:
        for g in e["grants"]:
            hit = [r for r in p["rows"]
                   if r["tenant"] == e["tenant"] and r["service"] == g["service"]]
            assert len(hit) == 1, f"真源条目未 1:1 进全景：{e['tenant']}/{g['service']}"
            r = hit[0]
            assert r["fields"] == [str(f) for f in g["fields"]]
            assert r["fieldsCount"] == len(g["fields"])
            assert r["endpoint"] == g.get("endpoint", "")
            assert r["oaTicket"] == e["oa_ticket"]               # 真源当前只有 tenant 级单号
            assert r["oaSource"] == "tenant"


def test_panorama_summary_and_facets_selfconsistent():
    p = av.panorama(_real_matrix())
    rows, s = p["rows"], p["summary"]
    assert s["grants"] == len(rows)
    assert s["tenants"] == len({r["tenant"] for r in rows}) == len(p["facets"]["tenants"])
    assert s["services"] == len(p["facets"]["services"])
    assert s["fieldRefs"] == sum(r["fieldsCount"] for r in rows)
    assert s["distinctFields"] == len(p["facets"]["fields"])
    assert s["oaMissing"] + s["oaPlaceholder"] <= len(rows)
    assert set(p["facets"]["fields"]) == {f for r in rows for f in r["fields"]}


def test_panorama_masks_appkey_no_full_secret_leak():
    """掩码纪律：完整 appkey 不出 JSON 出参，出掩码＋kid（sha256 前 12 位）。"""
    matrix = _real_matrix()
    p = av.panorama(matrix)
    blob = json.dumps(p, ensure_ascii=False)
    for e in matrix:
        if not e.get("appkey"):   # 2026-09-30 现实态口径：OA 授权行 appkey 为空占位（凭证未发），无泄漏可查
            continue
        assert e["appkey"] not in blob, f"完整 appkey 泄漏到全景出参：{e['appkey']}"
    for r in p["rows"]:
        if not r["appkeyMasked"]:   # 2026-09-30 现实态口径：空 appkey 行掩码/kid 如实留空（_mask 契约，不伪造指纹）
            continue
        assert "****" in r["appkeyMasked"], "掩码形态异常：" + r["appkeyMasked"]
        assert len(r["kid"]) == 12, "kid 应为 sha256 前 12 位"


# ---------- 2. 反查「这个字段给了谁」 ----------

def test_field_lookup_hit_returns_holders():
    """反查机制（合成矩阵；线上矩阵全下线已清空，空态由 miss 用例守卫）。"""
    from synth_services import synth_matrix
    p = av.field_lookup(synth_matrix(), "ac_active_sts_01")
    assert p["field"] == "ac_active_sts_01" and p["found"] is True
    assert p["entries"], "命中字段却无持有条目"
    for e in p["entries"]:
        assert "ac_active_sts_01" in e["fields"]
        assert e["tenant"] and e["service"]
    s = p["summary"]
    assert s["grants"] == len(p["entries"])
    assert s["tenants"] == len({e["tenant"] for e in p["entries"]})
    assert s["services"] == len({e["service"] for e in p["entries"]})


def test_field_lookup_miss_and_empty_are_honest_empty_states():
    """未命中/空字段＝found=false 空态（不编造持有方，不抛错）。"""
    miss = av.field_lookup(_real_matrix(), "no_such_field_xyz")
    assert miss["found"] is False and miss["entries"] == []
    assert miss["summary"]["grants"] == 0 and miss["summary"]["tenants"] == 0
    empty = av.field_lookup(_real_matrix(), "   ")
    assert empty["field"] == "" and empty["found"] is False and empty["entries"] == []


def test_field_lookup_only_matches_exact_field_names():
    """精确匹配：前缀/子串不误命中（反查是合规事实检索，不做模糊联想）。"""
    matrix = [_entry(grants=[{"service": "s", "fields": ["ac_active_sts_01"]}])]
    assert av.field_lookup(matrix, "ac_active_sts_0")["found"] is False
    assert av.field_lookup(matrix, "ac_active_sts_012")["found"] is False
    assert av.field_lookup(matrix, "ac_active_sts_01")["found"] is True


# ---------- 3. OA 单号溯源 ----------

def test_oa_trace_matches_tenant_and_grant_level_ticket():
    matrix = [
        _entry(tenant="t_tenant", oa_ticket="OA-0001",
               grants=[{"service": "s1", "fields": ["f1"]}]),
        _entry(tenant="t_grant", oa_ticket="OA-9999",
               grants=[{"service": "s2", "fields": ["f2"], "oa_ticket": "OA-0001"}]),
    ]
    view = av.oa_trace(matrix, "OA-0001")
    assert view["found"] is True and view["state"] == "present"
    tenants = {e["tenant"] for e in view["entries"]}
    assert tenants == {"t_tenant", "t_grant"}, "tenant 级与 grant 级两个登记位都要命中"
    by_tenant = {e["tenant"]: e for e in view["entries"]}
    assert by_tenant["t_tenant"]["oaSource"] == "tenant"
    assert by_tenant["t_grant"]["oaSource"] == "grant"       # grant 级单号优先


def test_oa_trace_miss_and_empty_honest():
    matrix = _real_matrix()
    miss = av.oa_trace(matrix, "OA-NOT-EXIST")
    assert miss["found"] is False and miss["entries"] == []
    empty = av.oa_trace(matrix, "")
    assert empty["ticket"] == "" and empty["found"] is False


def test_oa_tickets_index_aggregates_and_excludes_missing():
    matrix = [
        _entry(tenant="t1", oa_ticket="OA-0001"),
        _entry(tenant="t2", oa_ticket="OA-0001",
               grants=[{"service": "s2", "fields": ["f2"]}]),
        _entry(tenant="t3", oa_ticket="", grants=[{"service": "s3", "fields": ["f3"]}]),
    ]
    idx = av.oa_tickets(matrix)
    assert [t["ticket"] for t in idx] == ["OA-0001"]         # 缺单号 t3 不进索引
    t = idx[0]
    assert t["count"] == 2 and t["tenants"] == ["t1", "t2"]
    assert t["services"] == ["user_tag_query", "s2"]


# ---------- 4. 如实口径：缺单号 / 占位 / 空矩阵 ----------

def test_oa_state_missing_placeholder_present():
    assert av.oa_state(None) == "missing"
    assert av.oa_state("") == "missing"
    assert av.oa_state("   ") == "missing"
    assert av.oa_state("OA-PENDING-TASK008") == "placeholder"
    assert av.oa_state("PENDING-1") == "placeholder"
    assert av.oa_state("TODO-1") == "placeholder"
    assert av.oa_state("TBD") == "placeholder"
    assert av.oa_state("DEMO-2026-0001") == "present"


def test_missing_oa_ticket_shown_as_missing_not_fake():
    """缺单号条目如实标 missing 并计入 oaMissing——不代填假单号（ADR-0003 存量不一致口径）。"""
    matrix = [_entry(oa_ticket="", grants=[{"service": "sx", "fields": ["fx"]}])]
    p = av.panorama(matrix)
    assert p["rows"][0]["oaState"] == "missing"
    assert p["rows"][0]["oaTicket"] == ""
    assert p["summary"]["oaMissing"] == 1
    assert p["summary"]["oaPlaceholder"] == 0
    assert p["oaTickets"] == []


def test_placeholder_ticket_is_not_present():
    """演示占位单号（OA-PENDING-*）≠ 合规单号：如实单列 placeholder 一类。"""
    matrix = [_entry(oa_ticket="OA-PENDING-TASK008",
                     grants=[{"service": "sp", "fields": ["fp"]}])]
    p = av.panorama(matrix)
    assert p["rows"][0]["oaState"] == "placeholder"
    assert p["summary"]["oaPlaceholder"] == 1
    assert p["summary"]["oaMissing"] == 0
    assert [t["state"] for t in p["oaTickets"]] == ["placeholder"]


def test_empty_matrix_is_zero_state():
    """空矩阵＝全零空态（不抛错、不造行）。"""
    p = av.panorama([])
    assert p["rows"] == [] and p["oaTickets"] == []
    assert set(p["summary"].values()) == {0}
    assert p["facets"] == {"tenants": [], "services": [], "fields": []}
    assert p["caliber"]  # 空态也带口径说明


# ---------- 5. 服务标识裸口径（卡4：版本位已删） ----------

def test_service_id_bare_no_version_dimension():
    """卡4 AC-3d：split_service 已删；行视图 serviceId＝裸 service，无 serviceVersion 键。"""
    assert not hasattr(av, "split_service")
    p = av.panorama([_entry(grants=[{"service": "user_tag_query", "fields": ["a"]}])])
    assert p["rows"][0]["serviceId"] == "user_tag_query"
    assert "serviceVersion" not in p["rows"][0]          # 不留恒空列


# ---------- 6. 容错只读 ----------

def test_dirty_matrix_skipped_without_raising():
    """脏数据（非 dict 条目/grants 非列表/fields 非列表/字段空串）跳过，不抛错。"""
    matrix = [
        "not-a-dict",
        {"tenant": "t1", "appkey": "k1", "oa_ticket": "OA-1", "grants": "nope"},
        {"tenant": "t2", "appkey": "k2", "oa_ticket": "OA-2",
         "grants": [{"service": "s1", "fields": ["f1", "", "", "f2"]}, "bad-grant"]},
    ]
    p = av.panorama(matrix)
    assert len(p["rows"]) == 1
    assert p["rows"][0]["fields"] == ["f1", "f2"], "空字段名应被剔除"


# ---------- 7. 薄胶水机检（宿主机无 fastapi，同 test_quota_usage 口径） ----------

def _routes(path: str) -> dict:
    src = (REPO / "service" / "app" / path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "get" and dec.args
                    and isinstance(dec.args[0], ast.Constant)):
                out[dec.args[0].value] = node
    return out


def test_grants_api_glue_routes_and_delegation():
    """grants_api.py：三条只读 GET 路由存在且委托 auth_view 纯函数，空参 400 人话。"""
    py_compile.compile(str(REPO / "service" / "app" / "grants_api.py"), doraise=True)
    routes = _routes("grants_api.py")
    assert "/v1/portal/grants/panorama" in routes
    field_fn = routes.get("/v1/portal/grants/field")
    oa_fn = routes.get("/v1/portal/grants/oa")
    assert field_fn is not None and oa_fn is not None, "缺字段反查/OA 溯源路由"
    assert [a.arg for a in field_fn.args.args] == ["request", "field"]
    assert [a.arg for a in oa_fn.args.args] == ["request", "ticket"]
    pan_src = ast.unparse(routes["/v1/portal/grants/panorama"])
    assert "av.panorama(" in pan_src, "全景端点须委托纯函数 auth_view.panorama"
    assert "av.field_lookup(" in ast.unparse(field_fn), "反查须委托 auth_view.field_lookup"
    assert "av.oa_trace(" in ast.unparse(oa_fn), "溯源须委托 auth_view.oa_trace"
    assert "missing_field" in ast.unparse(field_fn), "空 field 须 400 人话"
    assert "missing_ticket" in ast.unparse(oa_fn), "空 ticket 须 400 人话"


def test_caliber_fields_present_and_no_write_side():
    """口径说明字段齐备（页面文案唯一来源）；只读函数签名不带写参数。"""
    for k in ("source", "oaRule", "oaMissing", "writeSide", "appkeyMasked", "fieldLink"):
        assert k in av.AUTH_CALIBER, k
    assert "只读" in av.AUTH_CALIBER["writeSide"]
    # 只有读函数（无 register/save/update 之类写入口）
    public = [n for n in dir(av) if not n.startswith("_")]
    assert not any(w in n.lower() for n in public
                   for w in ("save", "write", "register", "update", "delete"))


def test_main_router_wired():
    src = (REPO / "service" / "app" / "main.py").read_text(encoding="utf-8")
    assert "from app.grants_api import router as grants_router" in src
    assert "app.include_router(grants_router)" in src


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("[ok] " + fn.__name__)
        except Exception:
            failed += 1
            print("[FAIL] " + fn.__name__)
            traceback.print_exc()
    print(str(len(fns) - failed) + "/" + str(len(fns)) + " passed")
    sys.exit(1 if failed else 0)
