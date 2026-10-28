#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凭证发放基建单元测试（P0-4 · 前台重造 SPEC §5 P0-4 行）——纯 python 直跑或 pytest 收集均可。

覆盖（本卡 AC 的机器化部分；端到端 sb_ 过网关证据归 compose 场景）：
  签发：沙箱 sb_（默认试用授权＋TTL 7 天）/正式 ak_（OA 批件必填，ADR-0003；
        陌生租户拒绝——授权链起点必须是 OA 单）/每调用方上限 2 把/显式授权校验；
  限频：同租户/同 IP 阈值判定（默认 5/10 每小时窗，常量可经环境变量覆盖）；
  热加载语义：注册表落盘→重读→resolve_grant 即时生效（禁用/过期 403、未命中回落矩阵）；
  持久化：appkeys.yaml 权威源原子写＋读回一致；secret 明文绝不落盘（写前断言）；
  网关通道：consumer 幂等 PUT（200/201 皆成功）/删除 404 视作成功（假传输注入）。

被测对象 service/app/appkeys.py、gw_consumer.py 不依赖 FastAPI/redis（分层纪律）；
admin_api.py/main.py 为 FastAPI 薄胶水（宿主机无 fastapi，语法机检口径同 test_batch_api）。
"""
import asyncio
import json
import py_compile
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import appkeys as ak            # noqa: E402
from app import gw_consumer as gw        # noqa: E402
from app import kv_executor as kvx       # noqa: E402

from synth_services import load_synth_services, synth_matrix, synth_services_dict, real_registry_state, real_matrix_state  # noqa: E402  合成 fixture（全服务下线，见模块头注）
SERVICES = synth_services_dict()   # 全下线终态：合成 fixture（机制测试不依赖线上注册表）
MATRIX = synth_matrix()
REAL_SERVICES, REAL_LOAD_ERR = real_registry_state()   # 现实态（全下线→空表＋fail-loud 留痕）
REAL_MATRIX, REAL_MATRIX_ERR = real_matrix_state()
NOW = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
TRIAL_FIELDS = kvx.tenant_grant(MATRIX, "v1-svc-usr-asset-tag", tenant="demo_bank")["fields"]   # 试用真源（TRIAL_SOURCE_SERVICE）——合成矩阵口径


def _issue(doc, **kw):
    """签发便捷封装（默认沙箱＋陌生租户）。"""
    args = dict(tenant="partner-demo-1", app_name="电商标签点查", key_type="sandbox", now=NOW)
    args.update(kw)
    return ak.issue_appkey(doc, MATRIX, SERVICES, **args)


# ---------- 凭证生成 ----------

def test_gen_appkey_prefix_and_shape():
    """sb_/ak_ 前缀＋16 位小写十六进制；对 existing 保证唯一。"""
    for kind, prefix in (("sandbox", ak.SB_PREFIX), ("formal", ak.AK_PREFIX)):
        key = ak.gen_appkey(kind)
        assert key.startswith(prefix) and len(key) == len(prefix) + 16
        assert ak.gen_appkey(kind, {key}) != key
    secret = ak.gen_secret()
    assert secret.startswith(ak.SECRET_PREFIX) and len(secret) == 46   # sk_+43（auth_admin 同口径）
    assert ak.secret_digest(secret).startswith("sha256:") and len(ak.secret_digest(secret)) == 71
    assert ak.mask_appkey("sb_1234567890abcdef") == "sb_1234****cdef"


# ---------- 签发：沙箱（sb_ 前缀＋默认试用授权＋TTL） ----------

def test_issue_sandbox_trial_source_live_in_real_matrix():
    """现实态守卫（2026-10-14 TRIAL_SOURCE_SERVICE 对齐现役 id）：真实矩阵 demo_bank 已批
    v1-svc-usr-asset-tag → 沙箱试用签发恢复（原「全服务下线→409 冻结」态自然解除）。"""
    assert isinstance(REAL_SERVICES, dict)  # 现实态只断类型不断空
    assert not REAL_LOAD_ERR  # 空表合法态（容器须可空启动）
    assert not REAL_MATRIX_ERR  # 空矩阵合法态（无授权=如实 403/409）
    trial = kvx.tenant_grant(REAL_MATRIX, ak.TRIAL_SOURCE_SERVICE,
                             tenant=ak.TRIAL_SOURCE_TENANT)["fields"]
    assert trial  # 试用真源白名单非空（现实态：demo_bank 已批现役服务）
    rec, _ = ak.issue_appkey({"keys": []}, REAL_MATRIX, REAL_SERVICES, tenant="partner-demo-1",
                             app_name="电商标签点查", key_type="sandbox", now=NOW)
    assert rec["appkey"].startswith(ak.SB_PREFIX)
    grants = {g["service"]: set(g["fields"]) for g in rec["grants"]}
    assert grants == {ak.TRIAL_SOURCE_SERVICE: set(trial)}


def test_issue_sandbox_409_when_trial_source_missing():
    """机制守卫：矩阵缺试用真源（demo_bank 未批 TRIAL_SOURCE_SERVICE）→ 签发如实
    409 TRIAL_SOURCE_MISSING（试用功能冻结，不为绿硬造服务）。"""
    stripped = [dict(r, grants=[g for g in r["grants"] if g["service"] != ak.TRIAL_SOURCE_SERVICE])
                if r.get("tenant") == ak.TRIAL_SOURCE_TENANT else r for r in MATRIX]
    try:
        ak.issue_appkey({"keys": []}, stripped, SERVICES, tenant="partner-demo-1",
                        app_name="电商标签点查", key_type="sandbox", now=NOW)
        raise AssertionError("试用真源缺失时应 409")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 409 and exc.code == "trial_source_missing"


def test_issue_sandbox_unknown_tenant_gets_trial_grants_and_ttl():
    """陌生租户沙箱（合成矩阵）：授权＝demo_bank 已批字段（平台试用口径）；有效期＝签发日+7 天（D8 可配）。"""
    rec, secret = _issue({"keys": []})
    assert rec["appkey"].startswith(ak.SB_PREFIX)
    assert rec["status"] == "active" and rec["key_type"] == "sandbox"
    assert rec["expires_at"] == (NOW + timedelta(days=ak.SANDBOX_TTL_DAYS)).date().isoformat()
    assert ak.SANDBOX_TTL_DAYS == 7                                   # 默认口径（SB_TTL_DAYS 可覆盖）
    grants = {g["service"]: set(g["fields"]) for g in rec["grants"]}
    assert grants == {ak.TRIAL_SOURCE_SERVICE: set(TRIAL_FIELDS)}
    assert secret not in json.dumps(rec)                              # 明文只在返回值，不进记录
    assert rec["secret_hash"] == ak.secret_digest(secret)


def test_issue_sandbox_explicit_expires_at_overrides_ttl():
    """2026-10-08：沙箱显式 expires_at 优先于 SB_TTL_DAYS（签发表单可选 30/90/365 天真实生效）；
    缺省仍回落 7 天口径；非法格式仍 400。"""
    rec, _ = _issue({"keys": []}, expires_at="2026-11-07")
    assert rec["expires_at"] == "2026-11-07"                       # 显式值生效，非 NOW+7
    assert rec["expires_at"] != (NOW + timedelta(days=ak.SANDBOX_TTL_DAYS)).date().isoformat()
    try:
        _issue({"keys": []}, expires_at="2026/11/07")
        raise AssertionError("非法日期格式应 400")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 400 and exc.code == "invalid_expires_at"


def test_issue_sandbox_known_tenant_inherits_matrix_grants():
    """已登记租户沙箱：继承矩阵全部授权（user/news/partner 三服务）。"""
    rec, _ = _issue({"keys": []}, tenant="demo_bank")
    granted = {g["service"] for g in rec["grants"]}
    assert granted == {"user_tag_query", "news_tag_query", "partner_proxy", "usr_basic_info_query",
                       "v1-svc-usr-asset-tag"}


# ---------- 签发：正式（ak_ 前缀＋OA 批件必填，ADR-0003） ----------

def test_issue_formal_requires_oa_ticket():
    """正式凭证缺 OA 批件号 → 400 oa_ticket_required（ADR-0003：缺单号拒绝登记）。"""
    try:
        _issue({"keys": []}, tenant="demo_bank", key_type="formal")
        raise AssertionError("应拒绝缺 OA 批件号的正式签发")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 400 and exc.code == "oa_ticket_required"


def test_issue_formal_unknown_tenant_rejected():
    """正式凭证不接受矩阵未登记租户（授权链起点必须是 OA 单，SPEC N6/N7）。"""
    try:
        _issue({"keys": []}, key_type="formal", oa_ticket="OA-2026-000123")
        raise AssertionError("应拒绝陌生租户正式签发")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 409 and exc.code == "tenant_not_registered"


def test_issue_formal_ok_inherits_and_optional_expiry():
    """正式凭证（已登记租户＋OA 批件）：继承授权；有效期选填（YYYY-MM-DD 校验）。"""
    rec, _ = _issue({"keys": []}, tenant="demo_bank", key_type="formal",
                    oa_ticket="OA-2026-000123", expires_at="2027-03-01")
    assert rec["appkey"].startswith(ak.AK_PREFIX) and rec["expires_at"] == "2027-03-01"
    try:
        _issue({"keys": []}, tenant="demo_bank", key_type="formal",
               oa_ticket="OA-2026-000123", expires_at="2027/03/01")
        raise AssertionError("非法日期格式应拒绝")
    except ak.AppkeyIssueError as exc:
        assert exc.code == "invalid_expires_at"


# ---------- 签发：上限与显式授权 ----------

def test_issue_key_limit_per_caller():
    """每调用方上限 2 把（正常＋轮转）；第 3 把 → 409。"""
    doc = {"keys": []}
    rec1, _ = _issue(doc); doc["keys"].append(rec1)
    rec2, _ = _issue(doc); doc["keys"].append(rec2)
    try:
        _issue(doc)
        raise AssertionError("应触发每调用方上限")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 409 and exc.code == "key_limit_reached"
    assert rec1["appkey"] != rec2["appkey"]


def test_explicit_grants_accepts_proxy_namespace_without_fields():
    """C2/C5（2026-10-20）：显式授权接纳 proxy 命名空间，代理允许 fields=[]（整路由授权）。

    合理放宽说明：原红线「授权 ⊆ services.yaml 且 fields 非空」是为字段投影服务守门；
    B 型上游代理登记在 upstreams.yaml（kind=proxy），没有字段投影语义，整条路由授权
    即完整授权。放宽只对 kind=proxy 生效——普通服务空字段仍 400 empty_fields（下方
    同文件用例照守），不是整体放松。
    """
    services = dict(SERVICES)
    services["proxy-local-usr-asset-tag"] = {
        "kind": "proxy", "name": "用户资产标签代理", "route": "/proxy/local/usr-asset-tag",
        "fields": [], "status": "published",
    }
    rec, _ = ak.issue_appkey(
        {"keys": []}, MATRIX, services, tenant="partner-demo-1", app_name="代理整路由",
        key_type="sandbox", now=NOW,
        grants=[{"service": "proxy-local-usr-asset-tag", "fields": []}])
    assert rec["grants"] == [{"service": "proxy-local-usr-asset-tag",
                              "endpoint": None, "fields": []}]
    grant = ak.resolve_grant([rec], rec["appkey"], "proxy-local-usr-asset-tag", now=NOW)
    assert grant["fields"] == set()
    # 普通服务仍拒绝空字段（放宽不越界）
    try:
        ak.issue_appkey({"keys": []}, MATRIX, services, tenant="partner-demo-1",
                        app_name="x", key_type="sandbox", now=NOW,
                        grants=[{"service": "news_tag_query", "fields": []}])
        raise AssertionError("普通服务空字段仍应 400")
    except ak.AppkeyIssueError as exc:
        assert exc.code == "empty_fields"


def test_issue_explicit_grants_validation():
    """显式授权：未注册服务 400；空字段白名单 400。"""
    try:
        _issue({"keys": []}, grants=[{"service": "no_such", "fields": ["x"]}])
        raise AssertionError("未注册服务应拒绝")
    except ak.AppkeyIssueError as exc:
        assert exc.code == "unknown_service"
    try:
        _issue({"keys": []}, grants=[{"service": "news_tag_query", "fields": []}])
        raise AssertionError("空字段应拒绝")
    except ak.AppkeyIssueError as exc:
        assert exc.code == "empty_fields"


# ---------- 禁用/启用/过期 ----------

def test_set_status_transitions():
    """禁用→启用幂等迁移；启用已过期凭证 → 409；未登记 → 404。"""
    rec, _ = _issue({"keys": []})
    doc = {"keys": [rec]}
    ak.set_status(doc, rec["appkey"], "disable")
    assert rec["status"] == "disabled"
    try:
        ak.set_status(doc, rec["appkey"], "disable")
        raise AssertionError("重复禁用应拒绝")
    except ak.AppkeyIssueError as exc:
        assert exc.code == "not_active"
    ak.set_status(doc, rec["appkey"], "enable", now=NOW)
    assert rec["status"] == "active"
    try:
        ak.set_status(doc, rec["appkey"], "enable", now=NOW)
        raise AssertionError("启用中凭证再启用应拒绝")
    except ak.AppkeyIssueError as exc:
        assert exc.code == "not_disabled"
    ak.set_status(doc, rec["appkey"], "disable")
    try:
        ak.set_status(doc, rec["appkey"], "enable", now=NOW + timedelta(days=8))
        raise AssertionError("过期凭证不应可启用")
    except ak.AppkeyIssueError as exc:
        assert exc.code == "credential_expired"
    try:
        ak.set_status(doc, "sb_0000000000000000", "disable")
        raise AssertionError("未登记凭证应 404")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 404 and exc.code == "appkey_not_found"


# ---------- 调用期判定（热加载后即时生效的语义基础） ----------

def test_resolve_grant_active_disabled_expired_fallback():
    """注册表命中：授权投影/禁用 403/过期 403；未命中返回 None（回落矩阵）。"""
    rec, _ = _issue({"keys": []})
    keys = [rec]
    g = ak.resolve_grant(keys, rec["appkey"], ak.TRIAL_SOURCE_SERVICE, now=NOW)
    assert g["tenant"] == "partner-demo-1" and g["fields"] == set(TRIAL_FIELDS)
    assert ak.resolve_grant(keys, "oneserve-dev-appkey-001", ak.TRIAL_SOURCE_SERVICE) is None  # 回落信号
    try:
        ak.resolve_grant(keys, rec["appkey"], "partner_proxy", now=NOW)   # 未授权服务（B 型样例）应 403
        raise AssertionError("未授权服务应 403")
    except kvx.TagQueryError as exc:
        assert exc.code == "service_not_granted"
    rec["status"] = "disabled"
    try:
        ak.resolve_grant(keys, rec["appkey"], "news_tag_query", now=NOW)
        raise AssertionError("禁用凭证应 403")
    except kvx.TagQueryError as exc:
        assert exc.status == 403 and exc.code == "credential_disabled"
    rec["status"] = "active"
    try:
        ak.resolve_grant(keys, rec["appkey"], ak.TRIAL_SOURCE_SERVICE, now=NOW + timedelta(days=8))
        raise AssertionError("过期凭证应 403")
    except kvx.TagQueryError as exc:
        assert exc.code == "credential_expired"


# ---------- 持久化：appkeys.yaml 权威源（git 留痕＋secret 明文红线） ----------

def test_appkeys_yaml_roundtrip_and_secret_redline():
    """落盘→读回一致；文件不含 secret 明文；硬塞明文触发写前断言（安全红线）。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "appkeys.yaml"
        doc = {"keys": []}
        rec, secret = _issue(doc)
        doc["keys"].append(rec)
        ak.save_appkeys(path, doc, secret_plain=secret)   # 原子写＋明文断言
        text = path.read_text(encoding="utf-8")
        assert secret not in text and "secret_hash" in text and rec["appkey"] in text
        assert text.startswith("# ============")          # 头注释＝权威源说明（git 留痕口径）
        loaded = ak.load_appkeys(path)
        assert loaded["keys"][0]["appkey"] == rec["appkey"]
        assert ak.resolve_grant(loaded["keys"], rec["appkey"], ak.TRIAL_SOURCE_SERVICE, now=NOW)

        bad = {"keys": [dict(rec, secret_hash=secret)]}   # 模拟违规：明文混入记录
        try:
            ak.save_appkeys(path, bad, secret_plain=secret)
            raise AssertionError("明文断言应触发")
        except AssertionError as exc:
            assert "安全红线" in str(exc)

        empty = ak.load_appkeys(Path(td) / "nonexistent.yaml")
        assert empty["keys"] == []                        # 未签发＝空表（正常态非错误）


def test_list_view_masks_appkey_and_truncates_digest():
    """列表视图：完整 appkey 不出现；摘要只给 sha256:＋16 位十六进制；kid 可反查记录。"""
    rec, secret = _issue({"keys": []})
    view = ak.list_view({"keys": [rec]})[0]
    assert rec["appkey"] not in json.dumps(view)
    assert view["appkeyMasked"] == ak.mask_appkey(rec["appkey"])
    assert view["secretHashOnly"] == ak.secret_digest(secret)[:7 + 16]
    assert view["keyType"] == "sandbox" and view["expiresAt"] == rec["expires_at"]
    assert view["kid"] == ak.record_kid(rec["appkey"])
    assert ak.find_record([rec], view["kid"]) is rec                 # kid 寻址（前端操作）
    assert ak.find_record([rec], rec["appkey"]) is rec               # 完整值寻址
    assert ak.find_record([rec], "sb_000000000000000") is None


# ---------- 发放限频（默认阈值常量；Redis 计数由薄胶水注入） ----------

def test_rate_limit_verdict_and_keys():
    """同租户 5/小时、同 IP 10/小时（默认口径）；超限判 scope；键名含维度标识。"""
    assert ak.TENANT_ISSUE_LIMIT == 5 and ak.IP_ISSUE_LIMIT == 10 and ak.RATE_LIMIT_WINDOW_S == 3600
    assert ak.rate_verdict(1, 1) == (True, None)
    assert ak.rate_verdict(5, 10) == (True, None)
    assert ak.rate_verdict(6, 3) == (False, "tenant")
    assert ak.rate_verdict(3, 11) == (False, "ip")
    keys = ak.rate_limit_keys("partner-demo-1", "10.0.0.9")
    assert keys["tenant"] == "appkeys:rl:tenant:partner-demo-1"
    assert keys["ip"] == "appkeys:rl:ip:10.0.0.9"


# ---------- 网关 consumer 通道（假传输注入；与 init_routes 同款幂等口径） ----------

def test_gw_consumer_put_delete_ready():
    """PUT 200/201 皆成功、500 失败；DELETE 200/404 皆成功；探针 200。"""
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path))
        table = {
            ("PUT", "consumers/sb_x"): 201,
            ("PUT", "consumers/sb_y"): 200,
            ("PUT", "consumers/sb_bad"): 500,
            ("DELETE", "consumers/sb_x"): 200,
            ("DELETE", "consumers/sb_gone"): 404,
            ("GET", "routes"): 200,
        }
        return table.get((method, path), 500), {}

    assert asyncio.run(gw.put_consumer("sb_x", _req=fake)) is True
    assert asyncio.run(gw.put_consumer("sb_y", _req=fake)) is True
    assert asyncio.run(gw.put_consumer("sb_bad", _req=fake)) is False
    assert asyncio.run(gw.delete_consumer("sb_x", _req=fake)) is True
    assert asyncio.run(gw.delete_consumer("sb_gone", _req=fake)) is True
    assert asyncio.run(gw.admin_ready(_req=fake)) is True
    put_body = [("PUT", "consumers/sb_x")]
    assert calls[:1] == put_body
    # consumer 结构：username=appkey、key-auth.key=appkey（三处同源：网关/审计 caller/UI）
    def fake_capture(method, path, body=None):
        if method == "PUT":
            captured.append(body)
        return 200, {}
    captured = []
    asyncio.run(gw.put_consumer("sb_abc", _req=fake_capture))
    assert captured == [{"username": "sb_abc", "plugins": {"key-auth": {"key": "sb_abc"}}}]


# ---------- 薄胶水语法机检（宿主机无 fastapi；口径同 test_batch_api） ----------

def test_api_layer_compiles():
    """admin_api.py / main.py 语法机检（端点级 E2E 归 compose 场景）。"""
    py_compile.compile(str(REPO / "service" / "app" / "admin_api.py"), doraise=True)
    py_compile.compile(str(REPO / "service" / "app" / "main.py"), doraise=True)
    py_compile.compile(str(REPO / "service" / "app" / "gw_consumer.py"), doraise=True)


# ---------- 权威源种子文件（仓库内 git 留痕口径） ----------

def test_seed_registry_file_valid():
    """service/registry/appkeys.yaml 可读且结构合法。

    原断言写死「keys == []」（首发前空表态）——那描述的是**某一次初始提交的状态**，
    不是文件的不变式：只要控制台真签发过凭证（运营日常动作），该断言必红。
    2026-09-23 实测反例：为验收主链路签发 2 把 sandbox 凭证后本断言失败，
    而失败与凭证本身无关（文件合法、每把 key 结构完整）。
    改为断言**结构不变式**：keys 为列表；逐把校验 appkey/secret_hash/tenant/status/
    expires_at 齐备，且 secret 明文绝不落盘（安全纪律，与 appkeys.py 头注一致）。
    """
    doc = ak.load_appkeys(REPO / "service" / "registry" / "appkeys.yaml")
    assert isinstance(doc["keys"], list)
    for k in doc["keys"]:
        for field in ("appkey", "secret_hash", "tenant", "status", "expires_at"):
            assert k.get(field), f"凭证缺 {field}：{k.get('appkey')}"
        assert str(k["secret_hash"]).startswith("sha256:"), "secret 只应存 sha256 摘要"
        assert "secret" not in k, "禁止落盘 secret 明文"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
