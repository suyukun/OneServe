#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AppKey 凭证管理补全套件（2026-10-14 门槛稿 §C TASK-1/2/3）——纯 python 直跑或 pytest 收集均可。

覆盖（AC-1/2/3/4/5 机检部分；真栈端到端归 D3 compose 场景）：
  永久语义：签发 expires_at=permanent 落库字面量；resolve_grant 远期不判过期（§B.1）；
  受限删（AC-2）：从未调用→登记移除；调用计数>0→409 appkey_has_calls（人话文案含「只能禁用」）；
  续期（AC-3）：只能延长（缩短→409 expires_shrink_forbidden 指引禁用）；permanent 上确界；
  接线证据（维护者 教训：能力存在≠页面接线）：admin_api.py 路由/审计事件源码断言＋
  admin 与 portal/admin 双镜像静态断言（历史标签/永久选项/重置文案/删除续期按钮）。

被测对象 service/app/appkeys.py 纯逻辑（不依赖 FastAPI/redis，分层纪律）；
admin_api.py 为 FastAPI 薄胶水（宿主机无 fastapi，语法机检口径同 test_batch_api）。
"""
import py_compile
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import appkeys as ak            # noqa: E402

from synth_services import synth_matrix, synth_services_dict   # noqa: E402  合成 fixture（口径同 test_appkeys）
SERVICES = synth_services_dict()
MATRIX = synth_matrix()
NOW = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)


def _issue(doc, **kw):
    args = dict(tenant="partner-demo-1", app_name="电商标签点查", key_type="sandbox", now=NOW)
    args.update(kw)
    return ak.issue_appkey(doc, MATRIX, SERVICES, **args)


def _doc_with_key(expires_at="2026-11-01"):
    doc = {"keys": []}
    rec, _ = _issue(doc, expires_at=expires_at)
    doc["keys"].append(rec)
    return doc, rec


# ---------- 永久语义（§B.1 / AC-3 存储口径） ----------

def test_issue_permanent_stores_literal_and_never_expires():
    """签发 expires_at=permanent（沙箱/正式都可选）：落库字面量 permanent；
    resolve_grant 远期（2030）不判过期；list_view 原样透出供前端映射「永久」。"""
    rec, _ = _issue({"keys": []}, expires_at="permanent")
    assert rec["expires_at"] == "permanent"
    grant = ak.resolve_grant([rec], rec["appkey"], rec["grants"][0]["service"],
                             now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert grant and grant["tenant"] == "partner-demo-1"   # 远期仍放行＝永久
    view = ak.list_view({"keys": [rec]})[0]
    assert view["expiresAt"] == "permanent"


def test_issue_invalid_expires_at_still_400():
    """非法格式（非 YYYY-MM-DD 且非 permanent）仍 400 invalid_expires_at。"""
    for bad in ("2026/11/07", "forever", "2026-11"):
        try:
            _issue({"keys": []}, expires_at=bad)
            raise AssertionError("非法有效期 " + bad + " 应 400")
        except ak.AppkeyIssueError as exc:
            assert exc.status == 400 and exc.code == "invalid_expires_at"


# ---------- 受限删（AC-2） ----------

def test_delete_never_called_removes_record():
    """从未被调用（used_count=0）→ 从注册表移除；kid 亦可寻址。"""
    doc, rec = _doc_with_key()
    removed = ak.delete_appkey(doc, ak.record_kid(rec["appkey"]), used_count=0)
    assert removed["appkey"] == rec["appkey"] and doc["keys"] == []


def test_delete_with_calls_409_and_human_message():
    """调用计数>0 一律 409 appkey_has_calls；人话文案点名「只能禁用」（审计账不断链）。"""
    doc, rec = _doc_with_key()
    try:
        ak.delete_appkey(doc, rec["appkey"], used_count=3)
        raise AssertionError("有调用记录的凭证应拒绝删除")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 409 and exc.code == "appkey_has_calls"
        assert "只能禁用" in exc.message and "调用记录" in exc.message
    assert len(doc["keys"]) == 1                  # 拒绝＝注册表不动


def test_delete_unknown_appkey_404():
    """未登记凭证 404 appkey_not_found。"""
    try:
        ak.delete_appkey({"keys": []}, "sb_nonexistent0000", used_count=0)
        raise AssertionError("未登记凭证应 404")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 404 and exc.code == "appkey_not_found"


# ---------- 续期（AC-3） ----------

def test_renew_extends_and_stamps_renewed_at():
    """延长（2026-11-01 → 2026-12-31）生效并留 renewed_at 痕。"""
    doc, rec = _doc_with_key("2026-11-01")
    out = ak.renew_appkey(doc, rec["appkey"], "2026-12-31", now=NOW)
    assert out["expires_at"] == "2026-12-31" and out.get("renewed_at")


def test_renew_shrink_rejected_with_disable_guidance():
    """缩短请求 409 expires_shrink_forbidden，文案指引「作废请用禁用」；注册表不动。"""
    doc, rec = _doc_with_key("2026-12-31")
    try:
        ak.renew_appkey(doc, rec["appkey"], "2026-10-01")
        raise AssertionError("缩短有效期应被拒绝")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 409 and exc.code == "expires_shrink_forbidden"
        assert "禁用" in exc.message
    assert rec["expires_at"] == "2026-12-31"


def test_renew_equal_date_idempotent_and_permanent_upper_bound():
    """相等日期＝幂等放行；永久是上确界（date→permanent 放行，permanent→date 拒绝）。"""
    doc, rec = _doc_with_key("2026-12-31")
    ak.renew_appkey(doc, rec["appkey"], "2026-12-31")          # 相等＝放行
    ak.renew_appkey(doc, rec["appkey"], "permanent")           # → 永久＝延长
    assert rec["expires_at"] == "permanent"
    try:
        ak.renew_appkey(doc, rec["appkey"], "2027-06-30")      # 永久改回日期＝缩短
        raise AssertionError("永久改固定日期应被拒绝")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 409 and exc.code == "expires_shrink_forbidden"


def test_renew_invalid_or_empty_expires_400():
    """空/非法 expires_at 400 invalid_expires_at（必填口径）。"""
    doc, rec = _doc_with_key()
    for bad in ("", "2027/01/01", "ever"):
        try:
            ak.renew_appkey(doc, rec["appkey"], bad)
            raise AssertionError(repr(bad) + " 应 400")
        except ak.AppkeyIssueError as exc:
            assert exc.status == 400 and exc.code == "invalid_expires_at"


def test_renew_expired_key_revives():
    """过期凭证续到未来＝复活（enable 拒绝过期凭证后的唯一恢复通道，D3 口径）。"""
    doc, rec = _doc_with_key("2026-09-01")   # NOW 之前＝已过期
    ak.renew_appkey(doc, rec["appkey"], "2026-12-31", now=NOW)
    grant = ak.resolve_grant(doc["keys"], rec["appkey"], rec["grants"][0]["service"], now=NOW)
    assert grant                                    # 续期后立即可调


# ---------- 接线证据（端点层源码断言＋语法机检；宿主机无 fastapi） ----------

ADMIN_API_SRC = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")


def test_admin_api_delete_renew_routes_wired():
    """admin_api.py 挂了受限删/续期两条路由；删除的调用计数来自审计聚合（scan_usage）。"""
    assert '@router.post("/v1/admin/appkeys/{appkey}/delete")' in ADMIN_API_SRC
    assert '@router.post("/v1/admin/appkeys/{appkey}/expires")' in ADMIN_API_SRC
    assert "mw.credential_delete(doc, full, used_count=" in ADMIN_API_SRC
    assert "appk.scan_usage(" in ADMIN_API_SRC
    py_compile.compile(str(REPO / "service" / "app" / "admin_api.py"), doraise=True)


def test_admin_api_delete_and_renew_leave_audit_events():
    """AC-2/AC-3 审计留痕：成功路径调用既有 _audit_admin_action（admin:appkey_delete/renew）。"""
    assert '"appkey_delete"' in ADMIN_API_SRC
    assert '"appkey_renew"' in ADMIN_API_SRC


# ---------- 前端双镜像静态断言（AC-1/4/5 消费侧；admin 源＋portal 镜像） ----------

JS = (REPO / "admin" / "appkeys.js").read_text(encoding="utf-8")
HTML = (REPO / "admin" / "appkeys.html").read_text(encoding="utf-8")
MIRROR_JS = (REPO / "portal" / "admin" / "appkeys.js").read_text(encoding="utf-8")
MIRROR_HTML = (REPO / "portal" / "admin" / "appkeys.html").read_text(encoding="utf-8")


def test_frontend_tabs_default_active_with_history():
    """AC-1：默认只显有效凭证（listTab 默认 active）；历史标签可切；行过滤接线。"""
    assert "listTab: 'active'" in JS
    assert 'id="tab-active-btn"' in HTML and 'id="tab-history-btn"' in HTML
    assert "listTab = 'history'" in HTML
    assert "inCurrentTab(k)" in HTML and "isHistoryKey" in JS


def test_frontend_issue_form_has_permanent_option():
    """AC-4：签发表单有效期下拉含「永久」选项；签发路径透传 permanent。"""
    assert '<option value="permanent">永久</option>' in HTML
    assert "validityToDate(f.validityDays)" in JS
    assert "return 'permanent'" in JS


def test_frontend_reset_copy_states_semantics():
    """AC-5：重置按钮/提示明示「密钥忘了就重置——新密钥立即显示一次，旧的 N 天缓冲后失效」。"""
    assert "密钥忘了就重置——新密钥立即显示一次，旧的" in JS and "天缓冲后失效" in JS
    assert "密钥忘了就重置——新密钥立即显示一次，旧的" in HTML and "天缓冲后失效" in HTML


def test_frontend_delete_renew_buttons_two_stage():
    """删除/续期按钮在行内（data-action）；两段式确认（armed 开关文案）。"""
    assert 'data-action="delete"' in HTML and 'data-action="renew"' in HTML
    assert "askDelete" in JS and "我确认删除该凭证" in JS
    assert "openRenew" in JS and "saveRenew" in JS
    assert 'x-model="renewModal.armed"' in HTML      # 两段式 armed 开关（.dlg 弹窗）


def test_mirror_portal_admin_matches_source():
    """双镜像契约：portal/admin/appkeys.js 与源字节一致；html 为构建壳版但功能锚点齐备。"""
    assert MIRROR_JS == JS
    probes = ('id="tab-history-btn"', '<option value="permanent">', "密钥忘了就重置",
              'data-action="delete"', 'data-action="renew"')
    for probe in probes:
        assert probe in MIRROR_HTML, "portal 镜像缺锚点：" + probe


def test_frontend_last_used_never_used_copy():
    """AC-6：最近调用取不到如实「—」；从未调用显示「从未使用」（JS lastUsedText）。"""
    assert "从未使用" in JS and "lastUsedText" in JS
