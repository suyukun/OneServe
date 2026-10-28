#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务重名治理＋授权可见性套件（AppKey 二批 M 级 · 2026-10-14）——纯 python 直跑或 pytest 收集均可。

背景（维护者 2026-10-14 晚亲测实证）：services.yaml 曾有 3 个同名「用户资产标签查询」
（usr-asset-tag / usr-asset-tag1 / usr-asset-tag1014，route 各异）→ 签发凭证「授权服务」
下拉同名无法区分；已发放凭证列表看不出每把凭证能查哪些服务。

覆盖（§A 治本＋治标 / §B 授权可见性 / §C 失败路径）：
  A1 发布名查重：admin_api.publish_service_ep 挂 _find_name_conflict——同名异路由 →
     409 service_name_conflict（人话指路既有路由）；查全量＝console 写区＋运行时区；
     同名同路由＝同服务重发（原地覆盖/升版本）不拦。**真行为直测**：宿主机无
     fastapi，按 test_appkeys_admin 语法机检口径之外，另以 AST 抽取 helper 源码
     exec 执行（被测＝admin_api.py 里的同一份实现，零第二逻辑）。
  A2 存量改名（数据修正）：真 services.yaml 双区（运行时＋console 写区）名字唯一；
     usr-asset-tag1 →「用户资产标签查询（1013 测试）」、usr-asset-tag1014 →
     「用户资产标签查询 1014」（route 不动）；usr-asset-tag 保留原名。
  A3 选服务 UI：签发下拉选项＝「名字（路由尾段）」（svcOptionText/routeTail）；
     调试台（portal/src/trial.html）下拉＝名字＋完整路由（既有行为守卫）。
  B  授权可见性：GET /v1/admin/appkeys 每行随行 grants（_grants_view：serviceName/
     endpoint/fieldCount，服务名自运行时注册表解析、解析不到回落 id）；前端
     grants-cell chips（名·路由尾段，超 3 枚「+N」收起、展开显 服务/路由/字段数，
     无授权如实「未授权」）。
  C  失败路径：签发/重置/禁用启用/配额 catch 走 humanError 人话（弹窗保持打开）。

同路由重发＝原地覆盖语义基线（ops_config 纯逻辑层，未被名查重波及）一并钉住。
真栈 409/覆盖/chips 的 curl 实证归 D 现场回执（不在本套件内造假）。
"""
import ast
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import errors                              # noqa: E402
from app import ops_config as oc                    # noqa: E402

REAL_SERVICES = REPO / "service" / "services.yaml"
ADMIN_API_SRC = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")


# ======================================================================
# 工具：AST 抽取 admin_api 纯 helper 源码并 exec（被测＝真实现，非复制品）
# ======================================================================

def _load_admin_helper(name: str):
    """从 admin_api.py 抽指定函数源码执行（宿主机无 fastapi，不能整模块 import）。

    抽取的是**当前源码文本**（ast.get_source_segment）——源码改了测试就跟真实现走，
    不存在第二份判定逻辑；测试若因签名改动而 exec 失败＝显式迁移信号。
    """
    tree = ast.parse(ADMIN_API_SRC)
    fn = next((n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)
    assert fn is not None, f"admin_api.py 缺纯 helper：{name}（应与 _grants_view/_find_name_conflict 同层）"
    seg = ast.get_source_segment(ADMIN_API_SRC, fn)
    proto = ("from __future__ import annotations\n"
             "from pathlib import Path\n"
             "from app import appkeys as appk\n"
             "from app import ops_config as opsc\n\n" + seg)
    ns: dict = {}
    exec(compile(proto, f"<admin_api.{name}>", "exec"), ns)   # noqa: S102  测试直执行被测源码
    return ns[name]


def _tmp_services(runtime: list[dict], console: list[dict] | None = None) -> Path:
    """合成 services.yaml（运行时区＋console 写区；形状对齐真实双区文件）。"""
    d = Path(tempfile.mkdtemp())
    p = d / "services.yaml"
    p.write_text(yaml.safe_dump(
        {"services": runtime, "console_services": console or []},
        sort_keys=False, allow_unicode=True), encoding="utf-8")
    return p


def _rt(name: str, route: str, sid: str) -> dict:
    return {"service_id": sid, "name": name, "route": route, "archetype": "kv_lookup",
            "backend": {"kv": {"namespace": "tags:t"}}}


# ======================================================================
# A1 发布名查重（真行为直测：AST 抽取 _find_name_conflict）
# ======================================================================

_find_name_conflict = _load_admin_helper("_find_name_conflict")


def test_name_conflict_same_name_different_route_found():
    """同名异路由 → 返回冲突路由（console 区与运行时区都占名）；带尾随空格算同名。"""
    p = _tmp_services(
        runtime=[_rt("用户资产标签查询", "/v1/svc/usr-asset-tag", "v1-svc-usr-asset-tag")],
        console=[{"id": "v1-svc-usr-asset-tag1", "name": "用户资产标签查询",
                  "route": "/v1/svc/usr-asset-tag1", "status": "draft"}])
    # 冲突源在 console 写区（含草稿：草稿同样占名）
    assert _find_name_conflict("用户资产标签查询", "/v1/svc/new-route", p) == "/v1/svc/usr-asset-tag1"
    # 冲突源在运行时区
    assert _find_name_conflict("用户资产标签查询 ", "/v1/svc/usr-asset-tag1", p) == "/v1/svc/usr-asset-tag"


def test_name_conflict_same_name_same_route_passes():
    """同名同路由＝同服务重发（原地覆盖/升版本语义）→ 不拦（返回 None）。"""
    p = _tmp_services(runtime=[_rt("用户资产标签查询", "/v1/svc/usr-asset-tag", "v1-svc-usr-asset-tag")])
    assert _find_name_conflict("用户资产标签查询", "/v1/svc/usr-asset-tag", p) is None


def test_name_conflict_distinct_name_or_empty_inputs_pass():
    """不同名不拦；名字/路由为空不判（交由后续必填/格式校验，职责单一）。"""
    p = _tmp_services(runtime=[_rt("用户资产标签查询", "/v1/svc/usr-asset-tag", "v1-svc-usr-asset-tag")])
    assert _find_name_conflict("另一个服务", "/v1/svc/new-route", p) is None
    assert _find_name_conflict("", "/v1/svc/new-route", p) is None
    assert _find_name_conflict("用户资产标签查询", "", p) is None


def test_name_conflict_wired_as_409_before_publish_write():
    """HTTP 编排接线：publish_service_ep 在调 opsc.publish_service **之前**查名，
    命中 → 409 + errors.SERVICE_NAME_CONFLICT + 人话（请换一个名字＋指路既有路由）。"""
    import py_compile
    py_compile.compile(str(REPO / "service" / "app" / "admin_api.py"), doraise=True)
    check_at = ADMIN_API_SRC.index("_find_name_conflict(\n")
    write_at = ADMIN_API_SRC.index("entry, before = mw.service_publish(")
    assert check_at < write_at, "名查重必须在写盘路径之前（先拦后写，拒绝时零副作用）"
    resp_at = ADMIN_API_SRC.index('{"error": errors.SERVICE_NAME_CONFLICT,')
    assert '409' in ADMIN_API_SRC[resp_at:resp_at + 400]
    assert "请换一个名字" in ADMIN_API_SRC[resp_at:resp_at + 400]


# ======================================================================
# A1 语义基线：同路由重发＝原地覆盖（纯逻辑层未被名查重波及）
# ======================================================================

def test_same_route_republish_still_overwrites():
    """同 route 重发＝同 id 升版本/覆盖（before 非空），不因名查重被拦——既有语义钉住。"""
    p = _tmp_services(runtime=[_rt("种子服务", "/v1/svc/seed-svc", "seed_kv_query")])
    fields = [{"name": "gender_cd", "sample": "1", "desc": ""}]
    oc.publish_service(p, name="服务甲", route="/v1/svc/dup-check-a", fields=fields,
                       status="published", operator="test", dict_cols={"gender_cd"})
    entry, before = oc.publish_service(
        p, name="服务甲", route="/v1/svc/dup-check-a", fields=fields,
        status="published", operator="test", dict_cols={"gender_cd"})
    assert before is not None, "同路由重发应识别为更新（原地覆盖），而非新建"
    # redis 型发布落 console 写区：同 id 单条（覆盖更新，不追加第二条目）
    entries = oc.load_console_services(p)
    same = [e for e in entries if e.get("id") == "v1-svc-dup-check-a"]
    assert len(same) == 1 and same[0]["name"] == "服务甲", \
        f"同路由重发不得追加条目（得 {len(same)} 条）"


# ======================================================================
# A2 存量改名（真 services.yaml 数据修正）
# ======================================================================

def test_services_yaml_names_unique_after_rename():
    """双区（运行时＋console 写区）服务名各自唯一——存量 3 个重名已治理。"""
    doc = yaml.safe_load(REAL_SERVICES.read_text(encoding="utf-8"))
    rt_names = [s["name"] for s in doc.get("services") or []]
    assert len(rt_names) == len(set(rt_names)), f"运行时区仍有重名：{rt_names}"
    cs_names = [c["name"] for c in doc.get("console_services") or []]
    assert len(cs_names) == len(set(cs_names)), f"console 写区仍有重名：{cs_names}"


def test_rename_keeps_routes_and_names_as_specified():
    """指定改名口径：tag1 →（1013 测试）、tag1014 → 1014；三条 route 一律不动；
     usr-asset-tag 保留原名。"""
    doc = yaml.safe_load(REAL_SERVICES.read_text(encoding="utf-8"))
    rt = {s["route"]: s["name"] for s in doc.get("services") or []}
    assert rt.get("/v1/svc/usr-asset-tag") == "用户资产标签查询"
    assert rt.get("/v1/svc/usr-asset-tag1") == "用户资产标签查询（1013 测试）"
    assert rt.get("/v1/svc/usr-asset-tag1014") == "用户资产标签查询 1014"
    cs = {c["route"]: c["name"] for c in doc.get("console_services") or []}
    assert cs.get("/v1/svc/usr-asset-tag1") == "用户资产标签查询（1013 测试）"
    assert cs.get("/v1/svc/usr-asset-tag1014") == "用户资产标签查询 1014"


# ======================================================================
# A1 错误码登记（errors.py 唯一真源＋构建产物同步）
# ======================================================================

def test_service_name_conflict_registered_in_errors_center():
    """码表：service_name_conflict / 409 / param=serviceName；errors.json 已再生同步。"""
    assert errors.SERVICE_NAME_CONFLICT == "service_name_conflict"
    row = next(e for e in errors.CODES if e.code == errors.SERVICE_NAME_CONFLICT)
    assert row.http_status == 409 and row.param == "serviceName"
    assert "换一个服务名" in row.solution
    art = (REPO / "portal" / "openapi" / "errors.json").read_text(encoding="utf-8")
    assert "service_name_conflict" in art, "portal/openapi/errors.json 未随 errors.py 再生"


# ======================================================================
# B 授权可见性（_grants_view 真行为直测＋HTTP/前端接线）
# ======================================================================

_grants_view = _load_admin_helper("_grants_view")


def test_grants_view_resolves_service_name_and_counts():
    """kid → [{service, serviceName, endpoint, fieldCount}]；服务名自运行时注册表解析；
    注册表没有的服务回落 service id（如实，不造名）。"""
    doc = {"keys": [{"appkey": "sb_abcdef1234567890",
                     "grants": [{"service": "v1-svc-usr-asset-tag",
                                 "endpoint": "/v1/svc/usr-asset-tag",
                                 "fields": ["gender_cd", "age_band"]},
                                {"service": "v1-svc-gone", "fields": ["x"]}]}]}
    services = {"v1-svc-usr-asset-tag": {"name": "用户资产标签查询",
                                         "route": "/v1/svc/usr-asset-tag"}}
    out = _grants_view(doc, services)
    kid = list(out)[0]
    gs = out[kid]
    assert gs[0] == {"service": "v1-svc-usr-asset-tag", "serviceName": "用户资产标签查询",
                     "endpoint": "/v1/svc/usr-asset-tag", "fieldCount": 2}
    assert gs[1]["serviceName"] == "v1-svc-gone" and gs[1]["fieldCount"] == 1
    # endpoint 缺失时自注册表路由补齐（手编登记可能只写 service）
    assert gs[1]["endpoint"] == ""


def test_list_appkeys_rows_carry_grants_wiring():
    """HTTP 接线：list_appkeys 对 list_view 逐行并入 grants（失败不影响掩码纪律——
    grants 只带计数不带字段清单）。"""
    assert 'r["grants"] = grants.get(r["kid"], [])' in ADMIN_API_SRC
    assert "_grants_view(doc, services)" in ADMIN_API_SRC
    assert '"fieldCount": len(g.get("fields") or [])' in ADMIN_API_SRC


# ======================================================================
# A3 / B / C 前端锚点（admin 源＋portal 镜像＋调试台守卫）
# ======================================================================

JS = (REPO / "admin" / "appkeys.js").read_text(encoding="utf-8")
HTML = (REPO / "admin" / "appkeys.html").read_text(encoding="utf-8")
MIRROR_JS = (REPO / "portal" / "admin" / "appkeys.js").read_text(encoding="utf-8")
MIRROR_HTML = (REPO / "portal" / "admin" / "appkeys.html").read_text(encoding="utf-8")
TRIAL_SRC = (REPO / "portal" / "src" / "trial.html").read_text(encoding="utf-8")


def test_issue_dropdown_shows_name_with_route_tail():
    """A3：签发下拉选项＝「名字（路由尾段）」（svcOptionText）；路由尾段单一实现 routeTail。"""
    assert 'x-text="svcOptionText(s)"' in HTML
    assert "function routeTail(route)" in JS and "routeTail(s && s.routePath)" in JS
    assert "svcOptionText" in JS


def test_trial_console_dropdown_keeps_name_plus_route():
    """A3：接口调试台下拉既有行为＝名字＋完整路由（守卫，防回退成纯名字）。"""
    assert "s.serviceName + '　' + s.routePath" in TRIAL_SRC


def test_grants_chips_anchors():
    """B：列表行授权 chips——未授权如实、+N 收起/展开、展开文案含字段数。"""
    for probe in ('data-testid="grants-cell"', "未授权", "grantChipText", "grantOverflow",
                  "toggleGrants", 'id="col-grants"'):
        assert probe in HTML, "admin 源缺锚点：" + probe
    assert "grantChipList" in JS and "GRANT_CHIP_LIMIT = 3" in JS
    assert "{ name: String(g.serviceName || g.service || '')" in JS


def test_failure_paths_use_human_error():
    """C：签发/重置/禁用启用/配额 catch 走 humanError；重置/配额/启停失败弹窗保持打开。"""
    assert "function humanError(err, fallback)" in JS
    assert "humanError(err, '签发失败')" in JS
    assert "humanError(err, '重置密钥失败')" in JS and "return false" in JS
    assert "humanError(err, '操作失败')" in JS
    assert "humanError(err, '配额更新失败')" in JS
    assert "if (okRot !== false) m.open = false" in JS


def test_mirror_portal_admin_matches_source_for_new_anchors():
    """双镜像契约：js 字节一致；镜像 html 带全部新增锚点（构建已再生）。"""
    assert MIRROR_JS == JS
    for probe in ('x-text="svcOptionText(s)"', 'data-testid="grants-cell"',
                  'id="col-grants"', "未授权", "grantChipText"):
        assert probe in MIRROR_HTML, "portal 镜像缺锚点：" + probe


# ----------------------------------------------------------------------

if __name__ == "__main__":
    fails = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"[ok] {_name}")
            except AssertionError as exc:
                fails += 1
                print(f"[FAIL] {_name}: {exc}")
    sys.exit(1 if fails else 0)
