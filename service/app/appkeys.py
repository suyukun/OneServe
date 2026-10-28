# -*- coding: utf-8 -*-
"""AppKey 发放登记（P0-4 · 前台重造 SPEC §5 P0-4 行）——纯逻辑，不 import FastAPI/redis。

职责：控制台动态签发凭证的唯一权威源 service/appkeys.yaml 的全部决策逻辑：
  - 沙箱凭证 sb_ 前缀（限时 TTL，默认 7 天，SB_TTL_DAYS 可配）——供「陌生接入方
    发现→试用→拿钥→首调」自助旅程（SPEC §0 出口判据 3）；
  - 正式凭证 ak_ 前缀（OA 批件号必填，ADR-0003：审批走工单系统，平台只做登记）；
  - 发放限频阈值常量（同租户/同 IP，按小时窗，均可经环境变量覆盖）；
  - 禁用/启用/过期判定（与 auth_matrix.yaml 的关系见下）。

与 auth_matrix.yaml 的分工（避免同租户重复行破坏 auth_admin validate 唯一性）：
  auth_matrix.yaml 仍是 OA 授权登记权威（auth_admin CLI 通道，本模块不改写它）；
  控制台签发的凭证及其授权绑定记在本注册表——业务调用鉴权先查本表（命中即用
  本表 grants，含 TTL/禁用判定），未命中回落授权矩阵（main._caller_grant 薄胶水）。
  沙箱默认试用字段集＝矩阵演示租户 demo_bank 对 v1-svc-usr-asset-tag 的已批字段（零复制）；
  正式凭证只发给矩阵已登记租户（授权链起点必须是 OA 单，SPEC N7）。

安全纪律（沿用 auth_admin.py / 既有口径）：
  secret 明文永不落盘、不落日志——注册表只存 sha256 摘要（sha256:<64hex>），
  明文仅在签发响应/弹窗出现一次；写盘前断言明文不出现在序列化结果中。
  appkey 本身是网关 key-auth 凭证（与 auth_matrix 存明文 appkey 同口径），可全文落
  权威源供 git 留痕；UI 一律掩码展示。

可测性：本模块全部函数无副作用（除 save_* 写盘），路径/时间均可注入。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from app import errors
from app import grants_admin as gam         # 冻结联动门（管理功能群一期 §1.5③）：同门单源
from app.kv_executor import TagQueryError   # 复用既有错误类型：端点层 except 口径零改动

SERVICE_DIR = Path(__file__).resolve().parent.parent          # service/
# 注册表权威源＝service/registry/appkeys.yaml（目录级 bind mount 进容器：原子写 tmp+rename
# 在挂载目录内完成——单文件挂载点 rename 会 EBUSY；git 留痕口径＝仓库内该文件）
REGISTRY_DIR = SERVICE_DIR / "registry"
APPKEYS_YAML = REGISTRY_DIR / "appkeys.yaml"

# ---------- 常量（阈值可经环境变量覆盖；默认值即产品口径） ----------
SB_PREFIX = "sb_"                       # 沙箱凭证前缀（限时试用）
AK_PREFIX = "ak_"                       # 正式凭证前缀（OA 批件必填）
SECRET_PREFIX = "sk_"

KEY_LIMIT_PER_CALLER = 2                # 每调用方上限 2 把（正常＋轮转；B2 口径）

SANDBOX_TTL_DAYS = int(os.environ.get("SB_TTL_DAYS", "7"))     # D8 同源：7 天起步可配

# 凭证轮换重叠期（P1-4 · SPEC §5 / D8）：轮换时新旧 key 并行天数，默认 7（对齐 Stripe
# 官方密钥轮换重叠上限），可经环境变量 ROTATION_OVERLAP_DAYS 覆盖。旧 key 在轮换日
# 起 ROTATION_OVERLAP_DAYS 个日历日内仍可用，之后解析时自动失效（credential_rotated_expired）。
ROTATION_OVERLAP_DAYS = int(os.environ.get("ROTATION_OVERLAP_DAYS", "7"))

RATE_LIMIT_WINDOW_S = int(os.environ.get("APPKEY_RL_WINDOW_S", "3600"))
TENANT_ISSUE_LIMIT = int(os.environ.get("APPKEY_RL_TENANT_PER_HOUR", "5"))
IP_ISSUE_LIMIT = int(os.environ.get("APPKEY_RL_IP_PER_HOUR", "10"))

# 沙箱默认试用口径：矩阵演示租户对首个 A 型服务的已批字段（不复制字段名单进代码）
TRIAL_SOURCE_TENANT = "demo_bank"
TRIAL_SOURCE_SERVICE = "v1-svc-usr-asset-tag"   # 2026-10-14 由 user_tag_query 更名对齐（旧 id 随注册表 id 格式变更失效）

TENANT_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,62}[a-z0-9])?$")
OA_TICKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 永久凭证存储语义（2026-10-14 凭证管理补全 §B.1）：expires_at 存字面 "permanent"＝
# 无到期日。兼容性：DATE_RE 不匹配 → _expired()/前端 keyExpired() 均判「不过期」，
# 与存量空串（formal 未登记有效期）同语义，所有到期判断处零改动直通。
PERMANENT = "permanent"

_HEADER_COMMENT = """\
# ============================================================
# OneServe AppKey 发放登记（service/appkeys.yaml）——控制台签发凭证唯一权威源
# 写入方：运营控制台「签发/禁用」端点（P0-4）；本文件随仓库 git 留痕。
# 安全纪律：secret 明文永不落盘，只存 sha256:<64位十六进制> 摘要；
#          appkey 为网关 key-auth 凭证（同 auth_matrix 口径）可全文留痕，UI 掩码展示。
# 结构：keys[] -> appkey / secret_hash / tenant / caller_name / app_name
#       / key_type(sandbox|formal) / oa_ticket(formal 必填，ADR-0003)
#       / status(active|disabled) / expires_at(YYYY-MM-DD) / issued_at
#       / rotated_at、overlap_expires_at、superseded_by、rotation_of（P1-4 轮换重叠期留痕；
#         旧 key 在 overlap_expires_at 前仍可用，之后自动失效）
#       / quota_qps、quota_daily（登记口径；P1-5 起网关按此值强制限流）
#       / tag_sets（业务域标注，展示用） / grants[]（本凭证可调服务×字段白名单）
# 授权分工：OA 授权登记权威仍是 service/auth_matrix.yaml（auth_admin CLI）；
#          本表只记控制台签发凭证。业务鉴权先查本表、未命中回落矩阵。
# 沙箱默认试用字段集＝矩阵 demo_bank 对 v1-svc-usr-asset-tag 的已批字段（SB_TTL_DAYS 可配 TTL）。
# ============================================================"""


# ---------- 凭证生成（格式对齐 scripts/auth_admin.py：前缀+16hex / sk_+43 urlsafe） ----------

def gen_appkey(key_type: str, existing: set[str] | None = None) -> str:
    """appkey = sb_|ak_ + 16 位随机小写十六进制；对 existing 保证全局唯一。"""
    prefix = SB_PREFIX if key_type == "sandbox" else AK_PREFIX
    taken = existing or set()
    while True:
        key = prefix + secrets.token_hex(8)
        if key not in taken:
            return key


def gen_secret() -> str:
    """secret = sk_ + 43 字符 urlsafe 随机串（32 字节熵）；仅签发响应显示一次。"""
    return SECRET_PREFIX + secrets.token_urlsafe(32)[:43]


def secret_digest(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mask_appkey(appkey: str) -> str:
    """掩码：前 7 位＋****＋后 4 位（sb_xxxx****xxxx；与既有 UI 口径一致）。"""
    return appkey[:7] + "****" + appkey[-4:]


# ---------- 注册表读写（appkeys.yaml 权威源；原子写＋明文断言） ----------

def load_appkeys(path: Path = APPKEYS_YAML) -> dict:
    """读注册表；文件不存在＝尚未签发（空表，非错误——沙箱首发前属正常态）。"""
    if not path.is_file():
        return {"meta": {"updated": None, "updated_by": None}, "keys": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    keys = data.get("keys") or []
    if not isinstance(keys, list):
        raise RuntimeError(f"{path.name} keys 须为列表")
    return {"meta": data.get("meta") or {}, "keys": keys}


def dump_appkeys(doc: dict) -> str:
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return _HEADER_COMMENT + "\n" + body


def save_appkeys(path: Path, doc: dict, *, secret_plain: str | None = None) -> None:
    """原子写盘（tmp＋rename）；写前断言 secret 明文不出现（安全红线）。"""
    doc.setdefault("meta", {})["updated"] = _now_iso()
    doc.setdefault("meta", {})["updated_by"] = "console"
    text = dump_appkeys(doc)
    if secret_plain:
        assert secret_plain not in text, "安全红线：secret 明文不得写入注册表"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- 签发决策（纯函数） ----------

def _inherit_grants(matrix: list[dict], tenant: str) -> list[dict] | None:
    """取矩阵中该租户全部授权（继承签发）：[{service, endpoint, fields}]；未登记返回 None。"""
    rows = [r for r in matrix if r.get("tenant") == tenant]
    if not rows:
        return None
    grants = []
    for row in rows:
        for g in row.get("grants", []):
            grants.append({"service": g["service"], "endpoint": g.get("endpoint"),
                           "fields": list(g["fields"])})
    return grants or None


def _trial_grants(matrix: list[dict]) -> list[dict]:
    """沙箱默认试用口径：demo_bank 对 TRIAL_SOURCE_SERVICE 的已批字段。"""
    for row in matrix:
        if row.get("tenant") == TRIAL_SOURCE_TENANT:
            for g in row.get("grants", []):
                if g.get("service") == TRIAL_SOURCE_SERVICE:
                    return [{"service": g["service"], "endpoint": g.get("endpoint"),
                             "fields": list(g["fields"])}]
    raise AppkeyIssueError(409, errors.TRIAL_SOURCE_MISSING,
                           f"沙箱试用口径依赖矩阵 {TRIAL_SOURCE_TENANT} 的已批字段，当前矩阵缺失")


class AppkeyIssueError(TagQueryError):
    """签发期校验错误（结构同 TagQueryError：status/code/message，端点层统一映射）。"""


def issue_appkey(doc: dict, matrix: list[dict], services: dict, *, tenant: str,
                 app_name: str, key_type: str, caller_name: str = "",
                 oa_ticket: str = "", tag_sets: list[str] | None = None,
                 quota_qps: int = 50, quota_daily: int = 10000,
                 expires_at: str = "", grants: list[dict] | None = None,
                 now: datetime | None = None) -> tuple[dict, str]:
    """签发一把凭证：校验→定授权→算 TTL→生成 appkey/secret。

    返回 (record, secret_plain)；调用方负责网关 consumer 下发与注册表落盘。
    抛 AppkeyIssueError（status/code/message）＝拒绝签发，注册表不动。
    """
    tenant = (tenant or "").strip()
    if not TENANT_ID_RE.match(tenant):
        raise AppkeyIssueError(400, errors.INVALID_TENANT, "租户 ID 非法（小写字母/数字/_-，2~64 位）")
    # 冻结联动门（管理功能群一期 §1.5③）：frozen 租户禁止签发（db 态矩阵带
    # tenant_status 戳生效；yaml 态无戳恒 active＝门不生效，如实标注）。
    # 门错型就地转 AppkeyIssueError（端点层 except 口径零改动，杜绝 500）
    try:
        gam.ensure_not_frozen(matrix, tenant, "凭证签发")
    except gam.GrantAdminError as exc:
        raise AppkeyIssueError(exc.status, exc.code, exc.message) from exc
    if key_type not in ("sandbox", "formal"):
        raise AppkeyIssueError(400, errors.INVALID_KEY_TYPE, "key_type 须为 sandbox 或 formal")
    if not (app_name or "").strip():
        raise AppkeyIssueError(400, errors.INVALID_APP_NAME, "绑定应用名必填")
    oa_ticket = (oa_ticket or "").strip()
    if key_type == "formal":
        if not oa_ticket:
            raise AppkeyIssueError(400, errors.OA_TICKET_REQUIRED,
                                   "正式凭证必须绑定 OA 批件号（ADR-0003：缺单号拒绝登记）")
        if not OA_TICKET_RE.match(oa_ticket):
            raise AppkeyIssueError(400, errors.INVALID_OA_TICKET, "OA 批件号非法（4~64 位字母数字-_）")
    elif oa_ticket and not OA_TICKET_RE.match(oa_ticket):
        raise AppkeyIssueError(400, errors.INVALID_OA_TICKET, "OA 批件号非法（4~64 位字母数字-_）")

    held = [k for k in doc["keys"] if k.get("tenant") == tenant]
    if len(held) >= KEY_LIMIT_PER_CALLER:
        raise AppkeyIssueError(409, errors.KEY_LIMIT_REACHED,
                               f"租户 {tenant} 已持有 {len(held)} 把凭证（上限 "
                               f"{KEY_LIMIT_PER_CALLER}：正常＋轮转各一把）")

    # 授权来源：显式指定 > 继承矩阵已批 > （沙箱）平台默认试用集；正式凭证不接受陌生租户
    resolved = _validate_explicit_grants(grants, services) if grants else None
    if resolved is None:
        resolved = _inherit_grants(matrix, tenant)
        if resolved is None:
            if key_type == "formal":
                raise AppkeyIssueError(
                    409, errors.TENANT_NOT_REGISTERED,
                    f"租户 {tenant} 未在授权矩阵登记；正式凭证须先经 OA 审批并由管理员登记"
                    f"（scripts/auth_admin.py register），平台不做在线自助改授权（SPEC N6/N7）")
            resolved = _trial_grants(matrix)   # 沙箱：陌生租户走平台默认试用口径

    now = now or datetime.now(timezone.utc)
    if key_type == "sandbox":
        # 显式 expires_at 优先（签发表单可选 30/90/365 天/永久）；缺省回落部署常量 SB_TTL_DAYS。
        # 2026-10-08：原口径沙箱固定 SB_TTL_DAYS 不读表单，导致前端有效期下拉成假选项。
        # 2026-10-14 补全 §B.1：新增「永久」（expires_at=permanent；签发与续期都可选）。
        explicit = (expires_at or "").strip()
        if explicit.lower() == PERMANENT:
            expire = PERMANENT
        elif explicit:
            if not DATE_RE.match(explicit):
                raise AppkeyIssueError(400, errors.INVALID_EXPIRES_AT,
                                       "有效期须为 YYYY-MM-DD 或 permanent（永久）")
            expire = explicit
        else:
            expire = (now + timedelta(days=SANDBOX_TTL_DAYS)).date().isoformat()
    else:
        expire = (expires_at or "").strip()
        if expire.lower() == PERMANENT:
            expire = PERMANENT
        elif expire and not DATE_RE.match(expire):
            raise AppkeyIssueError(400, errors.INVALID_EXPIRES_AT,
                                   "有效期须为 YYYY-MM-DD 或 permanent（永久）")

    appkey = gen_appkey(key_type, {k.get("appkey") for k in doc["keys"]})
    secret = gen_secret()
    record = {
        "appkey": appkey,
        "secret_hash": secret_digest(secret),
        "tenant": tenant,
        "caller_name": (caller_name or "").strip() or tenant,
        "app_name": app_name.strip(),
        "key_type": key_type,
        "oa_ticket": oa_ticket,
        "tag_sets": [str(t) for t in (tag_sets or [])],
        "quota_qps": int(quota_qps),
        "quota_daily": int(quota_daily),
        "status": "active",
        "expires_at": expire,
        "issued_at": now.isoformat(timespec="seconds"),
        "grants": resolved,
    }
    return record, secret


def _validate_explicit_grants(grants: list[dict], services: dict) -> list[dict]:
    """显式授权校验：service 须已注册、fields 非空且为字符串（字段白名单红线由投影层守卫）。

    C2（2026-10-20 代理进签发页）：kind=proxy 的服务是「整路由授权」——没有字段
    投影语义，允许 fields=[] 并保留空清单（不发明字段）。普通服务仍拒绝空字段，
    字段白名单红线不放松。
    """
    out = []
    for g in grants or []:
        sid = str(g.get("service", ""))
        if sid not in services:
            raise AppkeyIssueError(400, errors.UNKNOWN_SERVICE, f"服务未注册：{sid}")
        svc = services.get(sid) or {}
        is_proxy = isinstance(svc, dict) and svc.get("kind") == "proxy"
        fields = [str(f) for f in (g.get("fields") or []) if str(f).strip()]
        if not fields and not is_proxy:
            raise AppkeyIssueError(400, errors.EMPTY_FIELDS, f"服务 {sid} 的字段白名单不能为空")
        out.append({"service": sid, "endpoint": g.get("endpoint"), "fields": fields})
    if not out:
        raise AppkeyIssueError(400, errors.EMPTY_GRANTS, "显式授权至少包含一个服务")
    return out


def rotate_appkey(doc: dict, matrix: list[dict], services: dict, appkey: str, *,
                  now: datetime | None = None, quota_qps: int | None = None,
                  quota_daily: int | None = None) -> tuple[dict, str, dict]:
    """轮换凭证（P1-4）：为同一调用方签发新 key，旧 key 进入重叠期。

    返回 (new_record, secret_plain, old_record)；new_record 由调用方负责下发网关与落盘。
    重叠期语义（SPEC D8）：旧 key 自轮换日起 ROTATION_OVERLAP_DAYS 天（默认 7，对齐
    Stripe 官方上限）内仍可用，期满由 resolve_grant 自动失效（credential_rotated_expired）。
    重叠期到期日取「轮换日+n」与旧 key 自身 expires_at 的较早者——自然到期是独立硬界限，
    封顶保证倒计时（rotation_state.daysLeft）与自动失效判定永远一致。
    拒绝口径：未登记 404；旧 key 已是被轮换的旧 key → 409 already_rotated（幂等）；
    非启用态 → 409 not_active；每调用方上限（正常＋轮转各一把）复用 issue_appkey 校验。
    """
    now = now or datetime.now(timezone.utc)
    rec = find_record(doc["keys"], appkey)
    if rec is None:
        raise AppkeyIssueError(404, errors.APPKEY_NOT_FOUND,
                               f"凭证 {mask_appkey(appkey)} 未在注册表登记")
    if rec.get("superseded_by"):
        raise AppkeyIssueError(409, errors.ALREADY_ROTATED,
                               f"凭证 {mask_appkey(rec['appkey'])} 已是轮换后的旧凭证，无需再次轮换")
    if rec.get("status") != "active":
        raise AppkeyIssueError(409, errors.NOT_ACTIVE, "仅启用中的凭证可轮换")
    # 冻结联动门（管理功能群一期 §1.5③）：frozen 租户禁止轮换（同签发门口径＋错型转换）
    try:
        gam.ensure_not_frozen(matrix, str(rec.get("tenant") or ""), "凭证轮换")
    except gam.GrantAdminError as exc:
        raise AppkeyIssueError(exc.status, exc.code, exc.message) from exc

    # 新 key 继承旧 key 的身份与授权（显式 grants 透传，避免矩阵变动导致轮换改授权）
    new_rec, secret = issue_appkey(
        doc, matrix, services,
        tenant=rec["tenant"], app_name=rec.get("app_name") or "未命名应用",
        key_type=rec.get("key_type") or "sandbox",
        caller_name=rec.get("caller_name") or rec["tenant"],
        oa_ticket=rec.get("oa_ticket") or "", tag_sets=rec.get("tag_sets") or [],
        quota_qps=quota_qps if quota_qps is not None else (rec.get("quota_qps") or 50),
        quota_daily=quota_daily if quota_daily is not None else (rec.get("quota_daily") or 10000),
        expires_at=rec.get("expires_at") or "", grants=rec.get("grants"), now=now)

    today = now.astimezone(timezone.utc).date()
    due = overlap_expires_on(today)
    exp = str(rec.get("expires_at") or "")
    if DATE_RE.match(exp) and exp < due:
        # 重叠期不得超出凭证自身有效期（不虚报可续天数）：旧 key 的自然到期是独立硬界限，
        # 倒计时与 resolve_grant 判定都以这个封顶后的日期为准，两者不会互相打架。
        due = exp
    rec["rotated_at"] = today.isoformat()
    rec["overlap_expires_at"] = due
    rec["superseded_by"] = new_rec["appkey"]        # 权威源全文留痕；视图层只出掩码
    new_rec["rotation_of"] = rec["appkey"]
    return new_rec, secret, rec


def set_status(doc: dict, appkey: str, action: str, *, now: datetime | None = None) -> dict:
    """禁用/启用（幂等拒绝非法迁移）；返回更新后的记录（doc 原地更新）。"""
    rec = next((k for k in doc["keys"] if k.get("appkey") == appkey), None)
    if rec is None:
        raise AppkeyIssueError(404, errors.APPKEY_NOT_FOUND, f"凭证 {mask_appkey(appkey)} 未在注册表登记")
    if action == "disable":
        if rec.get("status") != "active":
            raise AppkeyIssueError(409, errors.NOT_ACTIVE, "仅启用中的凭证可禁用")
        rec["status"] = "disabled"
    elif action == "enable":
        if rec.get("status") != "disabled":
            raise AppkeyIssueError(409, errors.NOT_DISABLED, "仅已禁用的凭证可启用")
        now = now or datetime.now(timezone.utc)
        if _expired(rec, now):
            raise AppkeyIssueError(409, errors.CREDENTIAL_EXPIRED,
                                   "凭证已过有效期，不能重新启用（请重新签发）")
        if rec.get("superseded_by") and not _in_overlap(rec, now):
            raise AppkeyIssueError(409, errors.CREDENTIAL_ROTATED_EXPIRED,
                                   "凭证已被轮换且重叠期已结束，不能重新启用（请改用新凭证）")
        rec["status"] = "active"
    else:
        raise AppkeyIssueError(400, errors.INVALID_ACTION, "action 须为 disable 或 enable")
    return rec


# ---------- 删除/续期（2026-10-14 凭证管理补全 §B.2/B.3） ----------

def delete_appkey(doc: dict, appkey: str, *, used_count: int = 0) -> dict:
    """受限删（AC-2）：仅「从未被调用」的凭证可从注册表删除；有调用记录一律 409。

    口径（§B.2）：删除会物理移除登记，历史调用将无法按凭证反查（审计账断链），
    故只放过从未被调用的凭证；有调用的走禁用（可逆、留痕完整）。
    used_count＝该凭证累计调用条数（端点层扫审计 JSONL 聚合注入；纯逻辑不读文件）。
    返回被删除的记录；doc["keys"] 原地移除。轮换关联（superseded_by/rotation_of）
    是登记留痕文本，不做级联清理——对端凭证的历史保持原样。
    """
    rec = find_record(doc["keys"], appkey)
    if rec is None:
        raise AppkeyIssueError(404, errors.APPKEY_NOT_FOUND,
                               f"凭证 {mask_appkey(appkey)} 未在注册表登记")
    if int(used_count) > 0:
        raise AppkeyIssueError(
            409, errors.APPKEY_HAS_CALLS,
            f"凭证 {mask_appkey(rec['appkey'])} 有调用记录（累计 {int(used_count)} 次），只能禁用不能删除："
            "禁用后调用立即被拒，登记与审计留痕保留（审计账不断链）")
    doc["keys"].remove(rec)
    return rec


def renew_appkey(doc: dict, appkey: str, expires_at: str, *,
                 now: datetime | None = None) -> dict:
    """续期（AC-3）：只能延长有效期；支持设为永久（expires_at=permanent）。

    缩短＝新日期早于现日期（或现永久改固定日期）→ 409 expires_shrink_forbidden，
    文案指引「作废请用禁用」——禁用是可逆的停用语义，不用缩短日期冒充作废。
    相等日期＝幂等放行。过期凭证续到未来日期＝复活（set_status 的 enable 对过期
    凭证拒绝，续期是其唯一恢复通道，D3 演示路径口径）。留痕 renewed_at。
    """
    rec = find_record(doc["keys"], appkey)
    if rec is None:
        raise AppkeyIssueError(404, errors.APPKEY_NOT_FOUND,
                               f"凭证 {mask_appkey(appkey)} 未在注册表登记")
    new = (expires_at or "").strip()
    if not new:
        raise AppkeyIssueError(400, errors.INVALID_EXPIRES_AT,
                               "expires_at 必填：YYYY-MM-DD（延长至）或 permanent（永久）")
    if new.lower() == PERMANENT:
        new = PERMANENT                      # 永久是上确界：任何现状 → 永久 都是延长
    elif not DATE_RE.match(new):
        raise AppkeyIssueError(400, errors.INVALID_EXPIRES_AT,
                               "有效期须为 YYYY-MM-DD 或 permanent（永久）")
    cur = str(rec.get("expires_at") or "")
    if new != PERMANENT:
        if DATE_RE.match(cur):
            if new < cur:
                raise AppkeyIssueError(
                    409, errors.EXPIRES_SHRINK_FORBIDDEN,
                    f"续期只能延长有效期（当前 {cur}，请求 {new}）；如需作废请用「禁用」")
        else:
            # 现状无到期日（permanent/空）→ 改回固定日期属于缩短
            raise AppkeyIssueError(
                409, errors.EXPIRES_SHRINK_FORBIDDEN,
                f"该凭证当前永久有效（{cur or PERMANENT}），改为固定日期 {new} 属于缩短；"
                "如需作废请用「禁用」")
    rec["expires_at"] = new
    rec["renewed_at"] = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return rec


# ---------- 调用期判定（业务鉴权先查本表；返回 None＝未命中回落矩阵） ----------

def _expired(rec: dict, now: datetime) -> bool:
    exp = str(rec.get("expires_at") or "")
    if not DATE_RE.match(exp):
        return False
    return now.astimezone(timezone.utc).date() >= date.fromisoformat(exp)


def overlap_expires_on(rotated_on: str | date, days: int | None = None) -> str:
    """轮换重叠期到期日（YYYY-MM-DD）：轮换日 + days 个日历日。

    语义钉死（对齐既有 _expired 的「到期日当天即失效」口径）：重叠期内可用天数为
    days 天，第 days+1 天（即到期日当天）起旧 key 自动失效。举例 days=7：
    轮换日 09-22 签发 → 到期日 09-29；09-28（第 7 天）仍可用，09-29/09-30 失效。
    """
    d = date.fromisoformat(rotated_on) if isinstance(rotated_on, str) else rotated_on
    return (d + timedelta(days=ROTATION_OVERLAP_DAYS if days is None else int(days))).isoformat()


def _in_overlap(rec: dict, now: datetime) -> bool:
    """旧 key 是否仍在重叠期内可用（仅看轮换留痕；无轮换留痕＝非轮换态）。

    口径与 _expired 同源：重叠期到期日当天及之后失效。rotated_at 缺失/非法时
    保守判为不在重叠期（不凭残缺字段放行）。
    """
    rot = str(rec.get("rotated_at") or "")[:10]
    if not DATE_RE.match(rot):
        return False
    due = str(rec.get("overlap_expires_at") or "")[:10]
    if not DATE_RE.match(due):
        due = overlap_expires_on(rot)
    return now.astimezone(timezone.utc).date() < date.fromisoformat(due)


def rotation_state(rec: dict, *, today: date | None = None) -> dict:
    """轮换状态视图：是否轮换态、重叠期剩余天数、旧 key 是否已失效。

    daysLeft＝重叠期到期日 − 今天（与 my_access.expiry_days 同源日期差口径）：
    轮换日当天为 7（days=7），第 7 天为 1，到期日及之后为 0/负＝已失效。
    """
    rot = str(rec.get("rotated_at") or "")[:10]
    superseded = str(rec.get("superseded_by") or "")
    if not (rot or superseded):
        return {"rotated": False, "rotatedAt": None, "overlapExpiresAt": None,
                "daysLeft": None, "overlapDays": ROTATION_OVERLAP_DAYS, "expired": False}
    due = str(rec.get("overlap_expires_at") or "")[:10]
    if not DATE_RE.match(due) and DATE_RE.match(rot):
        due = overlap_expires_on(rot)
    today = today or datetime.now(timezone.utc).date()
    days = (date.fromisoformat(due) - today).days if DATE_RE.match(due) else None
    return {"rotated": True, "rotatedAt": rot or None,
            "overlapExpiresAt": due or None, "daysLeft": days,
            "overlapDays": ROTATION_OVERLAP_DAYS,
            "expired": days is not None and days <= 0}


def resolve_grant(keys: list[dict], appkey: str, service_id: str,
                  *, now: datetime | None = None) -> dict | None:
    """按 appkey 取签发凭证的授权：{tenant, oa_ticket, fields:set}。

    未登记返回 None（调用方回落授权矩阵）；已登记但禁用/过期/未授权该服务 →
    TagQueryError(403)（与矩阵同语义，热加载后即时生效）。
    P1-4：被轮换的旧 key 在重叠期内仍按原授权可用；重叠期一过自动失效，
    错误码 credential_rotated_expired（区别于自然到期的 credential_expired）。
    """
    rec = next((k for k in keys if k.get("appkey") == appkey), None)
    if rec is None:
        return None
    if rec.get("status") != "active":
        raise TagQueryError(403, errors.CREDENTIAL_DISABLED,
                            f"凭证 {mask_appkey(appkey)} 已禁用，如需恢复请联系平台运营")
    now = now or datetime.now(timezone.utc)
    if _expired(rec, now):
        raise TagQueryError(403, errors.CREDENTIAL_EXPIRED,
                            f"凭证 {mask_appkey(appkey)} 已过有效期（{rec.get('expires_at')}），请重新签发")
    if rec.get("superseded_by") and not _in_overlap(rec, now):
        raise TagQueryError(403, errors.CREDENTIAL_ROTATED_EXPIRED,
                            f"凭证 {mask_appkey(appkey)} 已被轮换，重叠期已结束"
                            f"（{rec.get('overlap_expires_at') or rec.get('rotated_at')}），请改用新凭证")
    for g in rec.get("grants", []):
        if g.get("service") == service_id:
            return {"tenant": rec["tenant"], "oa_ticket": rec.get("oa_ticket"),
                    "fields": set(g["fields"])}
    raise TagQueryError(403, errors.SERVICE_NOT_GRANTED,
                        f"租户 {rec['tenant']} 未获服务 {service_id} 授权")


# ---------- 发放限频（阈值常量＋键名＋判定；Redis 计数经薄胶水注入） ----------

def rate_limit_keys(tenant: str, ip: str) -> dict[str, str]:
    """固定窗口计数键（Redis INCR＋EXPIRE；窗口秒数＝RATE_LIMIT_WINDOW_S）。"""
    return {"tenant": f"appkeys:rl:tenant:{tenant}", "ip": f"appkeys:rl:ip:{ip}"}


def rate_verdict(tenant_count: int, ip_count: int) -> tuple[bool, str | None]:
    """判定是否放行：超限返回 (False, "tenant"|"ip")；放行返回 (True, None)。"""
    if tenant_count > TENANT_ISSUE_LIMIT:
        return False, "tenant"
    if ip_count > IP_ISSUE_LIMIT:
        return False, "ip"
    return True, None


def record_kid(appkey: str) -> str:
    """记录短标识：sha256(appkey) 前 12 位——前端操作定位用（列表不回完整 appkey 的纪律不破）。"""
    return hashlib.sha256(appkey.encode("utf-8")).hexdigest()[:12]


def find_record(keys: list[dict], ident: str) -> dict | None:
    """按完整 appkey 或 kid 定位记录（控制台操作端点的统一寻址）。"""
    for k in keys:
        if k.get("appkey") == ident:
            return k
    for k in keys:
        if record_kid(k.get("appkey", "")) == ident:
            return k
    return None


def list_view(doc: dict) -> list[dict]:
    """列表视图：掩码 appkey＋摘要展示口径（sha256: 前 16 位十六进制）；不暴露完整 appkey。

    P1-4 增列 rotation：轮换态＋重叠期剩余天数（门户倒计时数据源）＋新旧关联掩码。
    严格沿用掩码纪律——superseded_by/rotation_of 存的是完整 appkey，视图只出掩码。
    """
    out = []
    for k in doc["keys"]:
        digest = str(k.get("secret_hash") or "")
        st = rotation_state(k)
        out.append({
            "kid": record_kid(k["appkey"]),
            "appkeyMasked": mask_appkey(k["appkey"]),
            "callerId": k.get("tenant"), "callerName": k.get("caller_name"),
            "appName": k.get("app_name"), "keyType": k.get("key_type"),
            "secretHashOnly": digest[:16 + len("sha256:")] if digest else "",
            "tagSets": k.get("tag_sets") or [],
            "quotaQps": k.get("quota_qps"), "quotaDaily": k.get("quota_daily"),
            "oaTicket": k.get("oa_ticket") or "—",
            "status": k.get("status"), "expiresAt": k.get("expires_at") or None,
            "issuedAt": k.get("issued_at"), "lastUsedAt": k.get("last_used_at"),
            "rotation": {
                "rotated": st["rotated"], "rotatedAt": st["rotatedAt"],
                "overlapExpiresAt": st["overlapExpiresAt"], "daysLeft": st["daysLeft"],
                "overlapDays": st["overlapDays"], "expired": st["expired"],
                "supersededByMasked": (mask_appkey(k["superseded_by"])
                                       if k.get("superseded_by") else None),
                "rotationOfMasked": (mask_appkey(k["rotation_of"])
                                     if k.get("rotation_of") else None),
            },
        })
    return out


# ---------- 用量聚合（2026-10-14 凭证管理补全 §B.4/AC-6；NC-1 最省路径） ----------
# 数据源决策：审计 JSONL 是既有真源（每次业务调用一条，caller=appkey 全文），一次扫描
# 同时产出「今日次数/最近调用/累计条数」——今日次数与 my_access.usage_scan 同 CN 日界
# 口径；累计条数兼作受限删的调用计数（>0 一律 409）。不另建 Redis 计数器：冷启动清零
# 会把「从未调用」误报给受限删/从未使用标记（假阴），聚合既有记录无此问题。

_TZ_CN = timezone(timedelta(hours=8))       # 与 my_access._TZ_CN 同口径（日界按北京时间）


def scan_usage(audit_path, callers, *, now: datetime | None = None) -> dict:
    """审计 JSONL 一遍扫描：{caller: {"today": 今日次数, "lastUsedAt": str|None, "total": 累计}}。

    caller 精确匹配（同 my_access 掩码纪律：掩码片段不得命中真凭证）；文件不存在＝
    全零（未产生调用，非错误）；坏行/脏 ts 跳过不造数（同 usage_scan 口径）。
    """
    wanted = {str(c) for c in callers or () if str(c)}
    if not wanted:
        return {}
    t = (now or datetime.now(timezone.utc)).astimezone(_TZ_CN)
    today = t.date().isoformat()
    out = {c: {"today": 0, "lastUsedAt": None, "total": 0} for c in wanted}
    p = Path(audit_path)
    if not p.is_file():
        return out
    last_dt = {c: None for c in wanted}
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            caller = str(row.get("caller") or "")
            if caller not in out:
                continue
            ts = str(row.get("ts") or "")
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                continue                       # 脏 ts：不归入任何口径（不造数）
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out[caller]["total"] += 1
            if dt.astimezone(_TZ_CN).date().isoformat() == today:
                out[caller]["today"] += 1
            if last_dt[caller] is None or dt > last_dt[caller]:
                last_dt[caller] = dt
                out[caller]["lastUsedAt"] = ts
    return out


def usage_view(doc: dict, audit_path, *, now: datetime | None = None) -> dict:
    """运营台用量视图（AC-6）：既有 admin_usage（今日已用/上限/超限，门户同口径）
    增补 lastUsedAt（最近调用时间）/everUsed（从未使用标记），按 kid 索引（完整
    appkey 不出端点，与 list_view 同纪律）。两遍扫描各司其职（配额四要素/最近调用），
    演示规模审计文件小，成本可忽略；量级上去再并单扫。
    """
    from app import my_access as mya   # 延迟导入：my_access 顶层 import 本模块，顶层互引成环
    base = mya.admin_usage(doc, audit_path, now=now)
    keys = [k for k in doc.get("keys") or [] if k.get("appkey")]
    scan = scan_usage(audit_path, [str(k["appkey"]) for k in keys], now=now)
    by_kid = {record_kid(c): v for c, v in scan.items()}
    for it in base.get("items", []):
        v = by_kid.get(it.get("kid")) or {}
        it["lastUsedAt"] = v.get("lastUsedAt")
        it["everUsed"] = bool(v.get("total"))
    return base
