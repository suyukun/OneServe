#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凭证轮换重叠期套件（P1-4 · SPEC §5 / D8）——纯 python 直跑或 pytest 收集均可。

守卫口径（任务卡 AC）：轮换新 key 生效、旧 key 在重叠期内仍可用（默认 7 天，对齐
Stripe 官方上限，可经 ROTATION_OVERLAP_DAYS 覆盖）；重叠期倒计时（剩余天数）可算；
期满旧 key 解析自动失效且错误码为 credential_rotated_expired；出参掩码纪律不破。

边界钉死（沿用既有 _expired「到期日当天即失效」口径，days=7、轮换日 D0）：
  D0+1 … D0+6  旧 key 可用（daysLeft 6…1）
  D0+7（第 7 天）旧 key 仍可用（daysLeft 1，最后一个可用日）
  D0+8         旧 key 失效（第 8 天＝到期日次日；expires=overlap_expires_at=D0+7）
配套：常量默认 7＋环境变量覆盖；my_access 自助视图透出 rotation 且不含完整 secret 摘要。

被测对象 service/app/appkeys.py、my_access.py 不依赖 FastAPI/redis（分层纪律）；
admin_api.py/my_api.py 为 FastAPI 薄胶水（宿主机无 fastapi，语法机检口径同 test_appkeys）。
"""
import importlib
import json
import os
import py_compile
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import appkeys as ak            # noqa: E402
from app import kv_executor as kvx       # noqa: E402
from app import my_access as mya         # noqa: E402

from synth_services import load_synth_services, synth_matrix, synth_services_dict, real_registry_state  # noqa: E402  合成 fixture（全服务下线，见模块头注）
SERVICES = synth_services_dict()   # 全下线终态：合成 fixture（机制测试不依赖线上注册表）
MATRIX = synth_matrix()

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)   # 轮换日 D0
TRIAL_FIELDS = kvx.tenant_grant(MATRIX, "v1-svc-usr-asset-tag", tenant="demo_merchant")["fields"]   # 试用真源（TRIAL_SOURCE_SERVICE）——合成矩阵口径


def _issue(doc, **kw):
    """签发便捷封装（默认沙箱＋陌生租户，同 test_appkeys 口径）。"""
    args = dict(tenant="partner-demo-1", app_name="电商标签点查", key_type="sandbox", now=NOW)
    args.update(kw)
    return ak.issue_appkey(doc, MATRIX, SERVICES, **args)


def _rotated():
    """构造「已轮换」注册表：旧 rec 进重叠期＋新 new_rec。返回 (doc, old, new, secret)。

    基底用长有效期正式凭证（demo_merchant＋OA 批件，有效期 2027-03-01）：若用默认沙箱
    （TTL 恰为 7 天），其自然到期日与重叠期到期日同为 D0+7，期满会先命中
    credential_expired，掩盖 credential_rotated_expired 的边界。长有效期基底让
    「重叠期先于自然有效期结束」，边界可被确定性钉死（与生产轮换正式凭证同形）。
    """
    doc = {"keys": []}
    old, _ = _issue(doc, tenant="demo_merchant", key_type="formal",
                    oa_ticket="OA-2026-000123", expires_at="2027-03-01")
    doc["keys"].append(old)
    new, secret, old = ak.rotate_appkey(doc, MATRIX, SERVICES, old["appkey"], now=NOW)
    doc["keys"].append(new)
    return doc, old, new, secret


# ---------- 1. 轮换后新旧 key 并行可用 ----------

def test_rotate_issues_new_and_marks_old_overlap():
    """轮换：新 key 生效可用；旧 key 标记重叠期留痕且重叠期内仍可用。"""
    doc, old, new, secret = _rotated()
    assert new["appkey"] != old["appkey"]
    assert new["key_type"] == old["key_type"] == "formal"
    assert new["appkey"].startswith(ak.AK_PREFIX) and new["status"] == "active"
    assert new["expires_at"] == old["expires_at"] == "2027-03-01"   # 自然有效期继承
    # 身份/授权/配额继承旧 key
    assert new["tenant"] == old["tenant"] and new["app_name"] == old["app_name"]
    assert {g["service"] for g in new["grants"]} == {g["service"] for g in old["grants"]}
    assert secret not in json.dumps(doc)          # 明文不进注册表
    # 重叠期留痕（复用 expires_at 同口径的 YYYY-MM-DD）
    assert old["rotated_at"] == "2026-09-22"
    assert old["overlap_expires_at"] == "2026-09-29"    # D0+7
    assert old["superseded_by"] == new["appkey"]
    assert new["rotation_of"] == old["appkey"]
    # 并行可用：两者的试用真源（TRIAL_SOURCE_SERVICE）授权都能解析（合成矩阵口径）
    g_old = ak.resolve_grant(doc["keys"], old["appkey"], ak.TRIAL_SOURCE_SERVICE, now=NOW)
    g_new = ak.resolve_grant(doc["keys"], new["appkey"], ak.TRIAL_SOURCE_SERVICE, now=NOW)
    assert g_old["tenant"] == g_new["tenant"] == "demo_merchant"
    assert g_old["fields"] == g_new["fields"] == set(TRIAL_FIELDS)


def test_rotate_rejects_not_found_not_active_and_double_rotate():
    """未登记 404；禁用态 409 not_active；已是旧 key 再轮换 409 already_rotated（幂等）。"""
    doc, old, new, _ = _rotated()
    try:
        ak.rotate_appkey(doc, MATRIX, SERVICES, "sb_0000000000000000", now=NOW)
        raise AssertionError("未登记凭证轮换应 404")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 404 and exc.code == "appkey_not_found"
    doc2 = {"keys": []}
    r2, _ = _issue(doc2)
    doc2["keys"].append(r2)
    r2["status"] = "disabled"
    try:
        ak.rotate_appkey(doc2, MATRIX, SERVICES, r2["appkey"], now=NOW)
        raise AssertionError("禁用态凭证轮换应 409")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 409 and exc.code == "not_active"
    try:
        ak.rotate_appkey(doc, MATRIX, SERVICES, old["appkey"], now=NOW)  # old 已是旧 key
        raise AssertionError("旧凭证再次轮换应 409")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 409 and exc.code == "already_rotated"


# ---------- 2. 重叠期末尾边界（第 7 天 / 第 8 天）钉死 ----------

def test_overlap_boundary_day7_ok_day8_expired():
    """D0+6（第 7 天）仍可用；D0+8（第 8 天）失效为 credential_rotated_expired。"""
    doc, old, new, _ = _rotated()
    day7 = NOW + timedelta(days=6)     # 轮换日起第 7 个日历日（最后可用日）
    assert ak.resolve_grant(doc["keys"], old["appkey"], "news_tag_query", now=day7)
    day8 = NOW + timedelta(days=7)     # 到期日当天（第 8 天）＝失效
    try:
        ak.resolve_grant(doc["keys"], old["appkey"], "news_tag_query", now=day8)
        raise AssertionError("重叠期到期日旧 key 应失效")
    except kvx.TagQueryError as exc:
        assert exc.status == 403 and exc.code == "credential_rotated_expired"
    # 新 key 不受重叠期影响，同一时刻仍可用
    assert ak.resolve_grant(doc["keys"], new["appkey"], "news_tag_query", now=day8)
    later = NOW + timedelta(days=30)
    try:
        ak.resolve_grant(doc["keys"], old["appkey"], "news_tag_query", now=later)
        raise AssertionError("重叠期后旧 key 应持续失效")
    except kvx.TagQueryError as exc:
        assert exc.code == "credential_rotated_expired"


def test_rotation_state_countdown_days():
    """倒计时：轮换日 daysLeft=7，第 7 天=1，到期日=0(expired)；未轮换 rotated=False。"""
    doc, old, new, _ = _rotated()
    assert ak.rotation_state(old, today=NOW.date())["daysLeft"] == 7
    assert ak.rotation_state(old, today=(NOW + timedelta(days=6)).date())["daysLeft"] == 1
    st_due = ak.rotation_state(old, today=(NOW + timedelta(days=7)).date())
    assert st_due["daysLeft"] == 0 and st_due["expired"] is True
    assert ak.rotation_state(new, today=NOW.date())["rotated"] is False
    assert ak.rotation_state(new, today=NOW.date())["daysLeft"] is None
    # 非轮换旧态凭证（裸 active）不误报轮换
    plain, _ = _issue({"keys": []})
    assert ak.rotation_state(plain, today=NOW.date())["rotated"] is False


def test_overlap_capped_at_natural_expiry_no_overpromise():
    """短效凭证轮换：重叠期封顶于自身 expires_at，不虚报可续天数（倒计时与判定一致）。"""
    due_day = (NOW + timedelta(days=2)).date().isoformat()
    doc = {"keys": []}
    old, _ = _issue(doc, tenant="demo_merchant", key_type="formal",
                    oa_ticket="OA-2026-000123", expires_at=due_day)
    doc["keys"].append(old)
    new, _, old = ak.rotate_appkey(doc, MATRIX, SERVICES, old["appkey"], now=NOW)
    doc["keys"].append(new)
    assert old["overlap_expires_at"] == due_day          # 封顶：轮换日+7 被拉起回自身有效期
    assert ak.rotation_state(old, today=NOW.date())["daysLeft"] == 2
    # 第 2 天（D0+1）仍可用；到期日（D0+2）自然到期，先命中 credential_expired
    assert ak.resolve_grant(doc["keys"], old["appkey"], "user_tag_query",
                            now=NOW + timedelta(days=1))
    try:
        ak.resolve_grant(doc["keys"], old["appkey"], "user_tag_query",
                         now=NOW + timedelta(days=2))
        raise AssertionError("封顶后到期日应失效")
    except kvx.TagQueryError as exc:
        assert exc.code == "credential_expired"          # 自然到期优先于轮换到期码


# ---------- 3. 期满自动失效 + 错误码；启用口径 ----------

def test_rotated_expired_cannot_reenable_and_natural_expiry_keeps_code():
    """轮换期满旧 key 不能重新启用；自然到期仍走 credential_expired（错误码不混）。"""
    doc, old, new, _ = _rotated()
    ak.set_status(doc, old["appkey"], "disable", now=NOW)
    try:
        ak.set_status(doc, old["appkey"], "enable", now=NOW + timedelta(days=7))
        raise AssertionError("重叠期满旧 key 不应可启用")
    except ak.AppkeyIssueError as exc:
        assert exc.code == "credential_rotated_expired"
    # 重叠期内（旧 key 被禁用后）仍可重新启用
    ak.set_status(doc, old["appkey"], "enable", now=NOW + timedelta(days=1))
    assert old["status"] == "active"
    # 自然到期（未轮换）错误码仍为 credential_expired，不误报轮换
    doc2 = {"keys": []}
    r2, _ = _issue(doc2)
    doc2["keys"].append(r2)
    try:
        ak.resolve_grant(doc2["keys"], r2["appkey"], "news_tag_query", now=NOW + timedelta(days=8))
        raise AssertionError("自然过期应 403")
    except kvx.TagQueryError as exc:
        assert exc.code == "credential_expired"


# ---------- 4. 常量可配（环境变量覆盖后行为随动） ----------

def test_constant_configurable_via_env():
    """ROTATION_OVERLAP_DAYS 默认 7；改环境变量后重新导入＝重叠期与到期日随动。"""
    assert ak.ROTATION_OVERLAP_DAYS == 7                       # 默认口径（对齐 Stripe 上限）
    assert ak.overlap_expires_on("2026-09-22") == "2026-09-29"
    assert ak.overlap_expires_on("2026-09-22", days=3) == "2026-09-25"
    env = dict(os.environ, ROTATION_OVERLAP_DAYS="3")
    code = (
        "import sys; sys.path.insert(0, r'" + str(REPO / "service") + "');"
        "from app import appkeys as ak;"
        "print(ak.ROTATION_OVERLAP_DAYS, ak.overlap_expires_on('2026-09-22'))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "3 2026-09-25", out.stdout
    # 环境变量覆盖后，第 3 天可用、第 4 天失效
    os.environ["ROTATION_OVERLAP_DAYS"] = "3"
    try:
        mod = importlib.reload(ak)
        doc = {"keys": []}
        old, _ = _issue(doc, tenant="demo_merchant")   # 已登记租户；矩阵继承
        doc["keys"].append(old)
        new, _, old = mod.rotate_appkey(doc, MATRIX, SERVICES, old["appkey"], now=NOW)
        doc["keys"].append(new)
        assert old["overlap_expires_at"] == "2026-09-25"       # D0+3
        assert mod.resolve_grant(doc["keys"], old["appkey"], "user_tag_query",
                                 now=NOW + timedelta(days=2))   # 第 3 天可用
        try:
            mod.resolve_grant(doc["keys"], old["appkey"], "user_tag_query",
                              now=NOW + timedelta(days=3))       # 第 4 天失效
            raise AssertionError("重叠期 3 天口径下第 4 天应失效")
        except kvx.TagQueryError as exc:
            assert exc.code == "credential_rotated_expired"
    finally:
        del os.environ["ROTATION_OVERLAP_DAYS"]
        importlib.reload(ak)                                   # 复原默认常量，避免污染后续用例
    assert ak.ROTATION_OVERLAP_DAYS == 7


# ---------- 5. 掩码纪律：出参不含完整 secret/appkey（含轮换字段） ----------

def test_views_mask_full_appkey_and_secret():
    """list_view/credential_view 的 rotation 子对象不含完整 appkey/摘要（掩码纪律）。"""
    doc, old, new, secret = _rotated()
    views = ak.list_view(doc)
    blob = json.dumps(views, ensure_ascii=False)
    for rec in (old, new):
        assert rec["appkey"] not in blob          # 完整 appkey（含 superseded_by 关联）不出
        assert rec["appkey"] not in json.dumps(ak.list_view({"keys": [rec]}), ensure_ascii=False)
    assert secret not in blob                                     # 完整 secret 明文绝不出现
    assert all(len(v["secretHashOnly"]) <= 23 for v in views)      # 摘要仅截断展示口径
    old_view = next(v for v in views if v["appkeyMasked"] == ak.mask_appkey(old["appkey"]))
    assert old_view["rotation"]["rotated"] is True
    assert old_view["rotation"]["supersededByMasked"] == ak.mask_appkey(new["appkey"])
    assert ak.mask_appkey(new["appkey"]) in blob
    # my_access 自助视图同样只出掩码＋rotation 摘要
    cview = mya.my_credentials({"keys": [old]}, old["appkey"], today=NOW.date())[0]
    cblob = json.dumps(cview, ensure_ascii=False)
    assert old["appkey"] not in cblob and "sha256" not in cblob and secret not in cblob
    assert cview["rotation"]["rotated"] is True
    assert cview["rotation"]["daysLeft"] == 7 and cview["rotation"]["overlapExpiresAt"] == "2026-09-29"


# ---------- 持久化往返 + 薄胶水机检 ----------

def test_rotation_fields_roundtrip_through_yaml():
    """轮换留痕随 appkeys.yaml 落盘读回一致；明文断言不因新字段失守。"""
    import tempfile
    doc, old, new, secret = _rotated()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "appkeys.yaml"
        ak.save_appkeys(path, doc, secret_plain=secret)
        text = path.read_text(encoding="utf-8")
        assert secret not in text
        loaded = ak.load_appkeys(path)
        got = next(k for k in loaded["keys"] if k["appkey"] == old["appkey"])
        assert got["overlap_expires_at"] == "2026-09-29" and got["rotated_at"] == "2026-09-22"
        assert got["superseded_by"] == new["appkey"]
        # 读回后重叠期判定一致
        assert ak.resolve_grant(loaded["keys"], old["appkey"], "user_tag_query", now=NOW)


def test_rotation_api_layer_compiles_and_wires():
    """admin_api/my_api/main 语法机检＋轮换端点接线（宿主机无 fastapi）。"""
    for name in ("admin_api.py", "my_api.py", "main.py"):
        py_compile.compile(str(REPO / "service" / "app" / name), doraise=True)
    src = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")
    assert "appkeys/{appkey}/rotate" in src and "mw.credential_rotate(" in src
    assert "rotateOverlapDays" in src


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
