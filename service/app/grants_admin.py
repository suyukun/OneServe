# -*- coding: utf-8 -*-
"""授权登记写服务（1b-8 一期 · 授权管理页）——矩阵 codec 下沉＋三动作写＋租户派生视图。

分层纪律（同 appkeys.py）：纯逻辑，不 import FastAPI/redis；HTTP 编排归 admin_api
（薄胶水）；scripts/auth_admin.py CLI 与页面端点**同调本层**（设计稿 v1 §5-1
「写径抽层」无悔步：CLI 与页面同一服务层、同一校验函数）。

职责三块：
  ① 编解码：auth_matrix.yaml 读（safe_load）写（safe_dump＋头注释模板＋tmp+rename
     原子写；secret 明文写前断言红线沿 auth_admin 既有口径）。原 scripts/auth_admin.py
     的 codec/validate 全量下沉至此，CLI 薄壳同调（行为不变）。
  ② 校验：validate_matrix 结构校验（放宽四处——①②＝1b-8 设计稿 §7-2 前置；
     ③＝卡-1b7 收口批项4；④＝管理功能群一期 P0-2 前置）——
     · kind=proxy 的 grants.fields 空集合法（整路由授权，无字段投影语义；普通服务
       空字段仍拒——口径同 tests/test_appkeys.py:174 签发端先例）；
     · tenant_type=sandbox 的租户 oa_ticket 豁免（沙箱＝平台演示模板，单号留空；
       正式租户单号必填不变，ADR-0003 不放松）；
     · 无凭证载体行豁免 appkey 必填扩「有 oa_ticket 的正式登记行」（grants 可空）
       ——与沙箱空壳同属元数据库快照投影载体（meta_snapshot.matrix_rows_from 同构
       输出），租户新建登记面（tenants_api yaml 态写矩阵登记行）此前置。
     改校验器不改语义：既有负例（普通服务空字段／正式租户缺单号缺凭证行）照拒。
  ③ 写服务：POST /v1/admin/grants 三动作（upsert 新增/变更、disable 停用、enable
     再启用）的校验与矩阵改写——双层五条（设计稿 §3）全落服务层：
       1. grants ⊆ 租户级不变量（收窄后对照名下在用凭证级授权，越界 422 拒）；
       2. 租户存在性（一期派生清单＝auth_matrix ∪ appkeys 去重；1b-7 后切实体表）；
       3. 沙箱租户只读（trial template 演示模板，编辑/停用一律拒）；
       4. 字段空间随服务绑定（勾选字段须在服务注册声明的白名单内；代理无字段列）；
       5. OA 单号必填（正式租户缺单号拒；格式按 OA_TICKET_RE 提醒；真伪校验与
          OA 对接一概不做——口径写死，零集成）。
  ④ 读侧视图：derived_tenants 派生租户清单（列表页）＋ tenant_detail 租户详情
     （服务∪代理混排矩阵＋kind 徽标＋字段白名单＋凭证级只读摘要）。

生效区间：grant 行可选 effective_from/effective_to（ISO 日期，缺省长期有效）。
一期只登记与展示；数据面到期拦截不在本页（停用可即时收权），执行侧落地随 1b-7 后小卡。

【写径合入点（1b-7 步 5 标注 → 收口批项3 已接线）】本模块的 load/save 即
meta_writer 合入面：save_matrix 已双模分流（META_WRITER=db → meta_writer.matrix_save
差量写库＋快照导出；缺省 yaml → _save_matrix_yaml 原文件写不变），三动作校验与
本文件其余函数零改动；admin_api 端点与 CLI 均只经本层访问矩阵，无第二写径。

安全纪律（沿 auth_admin/appkeys）：secret 明文永不落盘不落日志；save_matrix 写前
断言明文不出现在序列化结果。appkey 为网关 key-auth 凭证可全文落盘（git 留痕口径），
视图层一律掩码（appkeys.mask_appkey）。

可测性：全部函数无副作用（除 _save_matrix_yaml 写盘与 db 模式 save_matrix 落库），matrix/services/注册表路径均可注入。
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app import errors
from app.kv_executor import TagQueryError   # 复用既有错误类型：端点层 except 口径零改动

# 容器内 __file__＝/app/app/grants_admin.py → SERVICE_DIR＝/app（仓库 service/ 挂载点，
# auth_matrix.yaml 与仓库同一文件；宿主机即仓库 service/ 目录）。
SERVICE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MATRIX = SERVICE_DIR / "auth_matrix.yaml"

# ---------- 格式正则（原 scripts/auth_admin.py 全量下沉，口径不变） ----------
# 租户名容下划线（兼容 003 既有租户 demo_merchant）
TENANT_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,62}[a-z0-9])?$")
SERVICE_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,62}[a-z0-9])?$")
# appkey 宽格式：兼容 003 开发占位（oneserve-dev-appkey-001）与新生成（ak_/sb_+16hex）
APPKEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{4,62}$")
SECRET_RE = re.compile(r"^sk_[A-Za-z0-9_-]{43}$")
SECRET_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OA_TICKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
ENDPOINT_RE = re.compile(r"^/[A-Za-z0-9/_.-]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")   # 生效区间（ISO 日期）

APPKEY_PREFIX = "ak_"
SECRET_PREFIX = "sk_"

# ---------- 授权管理域枚举（1b-8 一期） ----------
TENANT_TYPES = ("internal", "external", "sandbox")
TENANT_TYPE_LABELS = {"internal": "内部", "external": "外部", "sandbox": "沙箱"}
TENANT_STATUSES = ("active", "frozen", "terminating", "cancelled")   # 四态（一期派生清单恒 active，1b-7 后有源）
GRANT_KINDS = ("service", "proxy")          # kind 徽标：字段服务 / 整路由代理
ROW_STATUS = ("active", "revoked")          # 行状态：在册生效 / 已停用（可再启用）

# 服务 ∪ 代理合并口径：服务注册表条目无 kind 键＝字段服务；代理条目由 ops_config
# proxy_service_map 注入 kind=proxy（签发页同款合并口径，admin_api._namespace_with_proxies）。


class MatrixFormatError(Exception):
    """矩阵文件无法解析或结构不合法。"""


class GrantAdminError(TagQueryError):
    """授权写服务校验错误（结构同 TagQueryError：status/code/message，端点层统一映射）。"""


# ----------------------------------------------------------------------
# 编解码（pyyaml 读 / safe_dump 写 ＋ 头注释模板）——原 auth_admin 下沉，头注释扩新字段
# ----------------------------------------------------------------------

_HEADER_COMMENT = """\
# ============================================================
# OneServe 授权矩阵（唯一权威文件：service/auth_matrix.yaml）
# SPEC §5-② / ADR-0003：审批走工单系统；平台只做授权登记，缺 OA 单号拒绝登记。
# 结构（task-003 基底 ＋ task-008 管理面扩展 ＋ 1b-8 授权管理页扩展；服务读取方只认基底字段）：
#   matrix[] -> tenant 租户ID / appkey 网关consumer凭证 / oa_ticket OA单号(必填；沙箱租户豁免)
#              / display_name、secret_hash(仅sha256摘要,无明文)、status(active|revoked) 可选
#              / tenant_type(internal|external|sandbox) 可选（1b-8；沙箱＝演示模板只读）
#              / grants[] -> service 服务ID / fields 字段白名单(kind=proxy 可空＝整路由授权)
#                           / kind(proxy) 可选（整路由代理登记位）
#                           / endpoint、oa_ticket 可选（计量映射 / 单次授权溯源）
#                           / status(active|revoked) 可选（行状态：停用可再启用）
#                           / effective_from、effective_to 可选（ISO 日期生效区间；一期只登记展示）
# 规则：同租户×同服务唯一；appkey 全局唯一；secret 明文永不落盘；
#       白名单字段须真实存在于服务注册声明的字段空间（tests/test_kv_executor.py 守卫口径）。
# 管理：授权管理页（admin/grants.html）＋ python3 scripts/auth_admin.py register/validate
#       （两入口同调 service/app/grants_admin.py 同一服务层；本文件可手编，改后请跑 validate；
#         脚本/页面改写会重新生成本头注释，行内注释不保留——字段级说明写进头注释或提交说明）
# ============================================================"""


def dump_matrix(data: dict) -> str:
    """矩阵对象 → YAML 文本（头注释 ＋ safe_dump，键序保持写入顺序）。"""
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return _HEADER_COMMENT + "\n" + body


def load_matrix_text(text: str) -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MatrixFormatError(f"YAML 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise MatrixFormatError("矩阵顶层必须是键值映射（matrix: [...]）")
    return data


def load_matrix_file(path: Path) -> dict:
    if not path.is_file():
        raise MatrixFormatError(f"矩阵文件不存在：{path}")
    return load_matrix_text(path.read_text(encoding="utf-8"))


def _new_matrix_doc(operator: str) -> dict:
    return {"meta": {"updated": _utc_now_iso(), "updated_by": operator}, "matrix": []}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _save_matrix_yaml(path: Path, data: dict, secret_plain: str | None = None) -> None:
    """矩阵 yaml 原子写盘本体（tmp＋rename）；写前断言 secret 明文不出现（安全红线）。

    【1b-7 meta_writer 合入点】切持久层只换本函数（文件写 → 库事务＋快照导出）；
    收口批项3 接线后本函数＝yaml 回退模式写径（META_WRITER 缺省/显式 yaml，行为
    逐字节不变），meta_writer db 分支演练副本亦直调本函数（绕模式开关防递归）。
    """
    text = dump_matrix(data)
    if secret_plain:
        assert secret_plain not in text, "安全红线：secret 明文不得写入矩阵文件"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_matrix(path: Path, data: dict, secret_plain: str | None = None,
                operator: str = "auth_admin") -> None:
    """矩阵落盘统一入口（卡-1b7 收口批项3 接线）：META_WRITER 双模分流。

    db 模式 → meta_writer.matrix_save（双层校验原样发生＋差量写库＋快照导出——
    拔库演练实证「矩阵写径尚未切库」的收口遗留，本接线闭合；operator 贯通台账
    ＝ADR-0021「operator 绑 OIDC subject」，授权页端点传登录态 sub，CLI 落缺省）；
    yaml 模式（缺省）→ _save_matrix_yaml 原行为不变（auth_admin CLI 与授权页端点
    调用方零改动）。局部导入防环（meta_writer 亦延迟导入本模块）。
    """
    from app import meta_writer as _mw
    if _mw.writer_mode() == "db":
        _mw.matrix_save(Path(path), data, secret_plain=secret_plain, operator=operator)
        return
    _save_matrix_yaml(Path(path), data, secret_plain=secret_plain)


# ----------------------------------------------------------------------
# 结构校验（规范的机器化）——放宽四处（①②设计稿 §7-2／③收口批项4／④管理功能群 P0-2），改校验器不改语义
# ----------------------------------------------------------------------

def validate_matrix(data) -> tuple[list, list]:
    """校验矩阵结构。返回 (errors, warnings)；errors 非空即不合法。

    放宽四处（①②＝1b-8 · 设计稿 §7-2 前置；③＝卡-1b7 收口批项4；④＝管理功能群
    一期 P0-2，其余口径不变）：
      ① grants[].kind=proxy 时 fields 允许空集（整路由授权，无字段投影语义）；
         普通服务（无 kind 或 kind=service）fields 仍须非空白名单；
      ② tenant_type=sandbox 的租户行 oa_ticket 豁免（沙箱＝平台演示模板）；
         正式租户（internal/external/未标类型）单号必填不变（ADR-0003）；
      ③ 无凭证载体行豁免 appkey 必填——沙箱空壳行（tenant_type=sandbox 且无
         appkey/secret_hash，单号豁免）：元数据库快照的沙箱空壳投影载体（仅登记
         未发凭证；ADR-0021 口径）；
      ④ 载体行豁免扩「有 oa_ticket 的正式登记行」（tenant_type 非 sandbox、无
         appkey/secret_hash、带合法 oa_ticket，grants 可空）——快照投影对正式登记
         租户本就输出此形（meta_snapshot.matrix_rows_from：单号＋授权照登），拒绝它
         ＝「新建正式租户→快照登记行→授权页读回校验拒」422 死锁；缺单号的无凭证
         正式行不在豁免内（ADR-0003 不放松）。载体行随矩阵改写往返，拒绝载体＝
         登记面全断。
    """
    errors_out: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict):
        return [f"顶层必须是映射，实为 {type(data).__name__}"], warnings
    matrix = data.get("matrix", [])
    if not isinstance(matrix, list):
        return ["matrix 必须是列表"], warnings
    if not matrix:
        warnings.append("授权矩阵为空（服务层启动会拒绝：kv_executor 要求数据非空）")
    meta = data.get("meta")
    if meta is not None and (not isinstance(meta, dict)
                             or not str(meta.get("updated", "")).strip()
                             or not str(meta.get("updated_by", "")).strip()):
        errors_out.append("meta.updated / meta.updated_by 必填（登记留痕）")

    seen_tenants: set[str] = set()
    seen_appkeys: set[str] = set()
    seen_grants: set[tuple[str, str]] = set()

    for t_idx, row in enumerate(matrix, 1):
        if not isinstance(row, dict):
            errors_out.append(f"matrix[{t_idx}] 必须是映射")
            continue
        tid = str(row.get("tenant", ""))
        where = f"matrix[{tid or t_idx}]"
        if not TENANT_ID_RE.match(tid):
            errors_out.append(f"{where}.tenant 非法（小写字母/数字/_-，2~64 位）：{tid!r}")
        if tid in seen_tenants:
            errors_out.append(f"{where}.tenant 重复：{tid}")
        seen_tenants.add(tid)

        # ② 放宽：沙箱租户（tenant_type=sandbox）单号豁免；其余租户必填不变（ADR-0003）
        tenant_type = str(row.get("tenant_type", "")).strip()
        ticket = str(row.get("oa_ticket", ""))
        # ③④ 放宽（收口批项4＋管理功能群 P0-2）：无凭证载体行豁免 appkey 必填——
        #   沙箱空壳行（免单号）＋有 oa_ticket 的正式登记行（grants 可空）＝快照投影
        #   两类载体行（见 docstring）；缺单号的无凭证正式行不豁免（ADR-0003 不放松）
        appkey = str(row.get("appkey", ""))
        is_carrier = (not appkey and not row.get("secret_hash")
                      and (tenant_type == "sandbox" or bool(ticket)))
        if not is_carrier and not APPKEY_RE.match(appkey):
            errors_out.append(f"{where}.appkey 必填且非法（字母数字-_，5~63 位）：{appkey!r}"
                              + ("（无凭证载体行豁免口径：沙箱空壳须 tenant_type=sandbox 且免单号；"
                                 "正式登记行须带 oa_ticket 且不带 appkey/secret_hash）"
                                 if not appkey and tenant_type != "sandbox" and not ticket else ""))
        if appkey in seen_appkeys:
            errors_out.append(f"{where}.appkey 全局重复：{appkey}")
        if appkey:
            seen_appkeys.add(appkey)
        if tenant_type and tenant_type not in TENANT_TYPES:
            errors_out.append(f"{where}.tenant_type 非法（须为 internal|external|sandbox）：{tenant_type!r}")
        if tenant_type == "sandbox":
            if ticket and not OA_TICKET_RE.match(ticket):
                errors_out.append(f"{where}.oa_ticket 非法（4~64 位字母数字-_）：{ticket!r}")
        elif not OA_TICKET_RE.match(ticket):
            errors_out.append(f"{where}.oa_ticket 必填且非法（4~64 位字母数字-_）：{ticket!r}——"
                              f"缺 OA 单号拒绝登记（ADR-0003；沙箱租户可豁免：补 tenant_type=sandbox）")

        if row.get("display_name") is not None and not str(row["display_name"]).strip():
            errors_out.append(f"{where}.display_name 若填写须非空")
        if row.get("status") is not None and str(row["status"]) not in ("active", "revoked"):
            errors_out.append(f"{where}.status 必须为 active|revoked，实为 {row.get('status')!r}")
        if row.get("secret_hash") is not None \
                and not SECRET_HASH_RE.match(str(row["secret_hash"])):
            errors_out.append(f"{where}.secret_hash 非法（须为 sha256:<64位十六进制>，禁止明文）")

        grants = row.get("grants", [])
        if not isinstance(grants, list):
            errors_out.append(f"{where}.grants 必须是列表")
            grants = []
        if not grants:
            warnings.append(f"{where} 未授权任何服务（仅注册未授权，当前不可调用）")
        for g_idx, grant in enumerate(grants, 1):
            gw = f"{where}.grants[{g_idx}]"
            if not isinstance(grant, dict):
                errors_out.append(f"{gw} 必须是映射")
                continue
            sid = str(grant.get("service", ""))
            # 卡4 AC-3e：service@vN 形式登记即拒（fail-closed，不静默截断为裸 id）
            if "@" in sid:
                errors_out.append(f"{gw}.service 非法：{sid!r}——服务修订号已删，登记须用裸 service_id")
            elif not SERVICE_ID_RE.match(sid):
                errors_out.append(f"{gw}.service 非法：{sid!r}")
            pair = (tid, sid)
            if pair in seen_grants:
                errors_out.append(f"{gw} 重复登记（租户×服务须唯一）：{tid} × {sid}")
            seen_grants.add(pair)
            # ① 放宽：kind=proxy 整路由授权 fields 允许空集；普通服务仍须非空白名单
            kind = str(grant.get("kind", "")).strip()
            if kind and kind not in GRANT_KINDS:
                errors_out.append(f"{gw}.kind 非法（service|proxy）：{kind!r}")
            is_proxy = kind == "proxy"
            fields = grant.get("fields")
            if not isinstance(fields, list):
                errors_out.append(f"{gw}.fields 必须是字段白名单列表")
            elif not fields and not is_proxy:
                errors_out.append(f"{gw}.fields 必须是非空字段白名单（整路由代理可空：补 kind=proxy）")
            else:
                seen_fields: set = set()
                for f in fields:
                    if not str(f).strip():
                        errors_out.append(f"{gw}.fields 含空白字段名")
                    if f in seen_fields:
                        errors_out.append(f"{gw}.fields 字段重复：{f}")
                    seen_fields.add(f)
            row_status = grant.get("status")
            if row_status is not None and str(row_status) not in ROW_STATUS:
                errors_out.append(f"{gw}.status 必须为 active|revoked，实为 {row_status!r}")
            for eff_key in ("effective_from", "effective_to"):
                eff = grant.get(eff_key)
                if eff is not None and not DATE_RE.match(str(eff)):
                    errors_out.append(f"{gw}.{eff_key} 非法（须为 YYYY-MM-DD，可留空＝长期有效）：{eff!r}")
            endpoint = grant.get("endpoint")
            if endpoint is not None and not ENDPOINT_RE.match(str(endpoint)):
                errors_out.append(f"{gw}.endpoint 非法（须以 / 开头）：{endpoint!r}")
            g_ticket = grant.get("oa_ticket")
            if g_ticket is not None and not OA_TICKET_RE.match(str(g_ticket)):
                errors_out.append(f"{gw}.oa_ticket 非法（4~64 位字母数字-_）：{g_ticket!r}")

    return errors_out, warnings


# ----------------------------------------------------------------------
# 凭证生成（appkey/secret）——原 auth_admin 下沉（CLI 与页面共用）
# ----------------------------------------------------------------------

def gen_appkey(existing: set[str]) -> str:
    """appkey = ak_ + 16 位随机小写十六进制；对 existing 集合保证全局唯一。"""
    while True:
        key = APPKEY_PREFIX + secrets.token_hex(8)
        if key not in existing:
            return key


def gen_secret(existing: set[str] | None = None) -> str:
    """secret = sk_ + 43 字符 urlsafe 随机串（32 字节熵）；仅显示一次。"""
    while True:
        sec = SECRET_PREFIX + secrets.token_urlsafe(32)
        if len(sec) == 46 and SECRET_RE.match(sec) and not (existing and sec in existing):
            return sec


def secret_digest(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# 租户派生视图（一期临时件：派生清单＝auth_matrix ∪ appkeys 去重；1b-7 后切实体表）
# ----------------------------------------------------------------------

def tenant_type_of(row: dict | None) -> str:
    """租户类型：矩阵行 tenant_type 显式值优先；缺省 external（正式租户口径）。"""
    t = str((row or {}).get("tenant_type", "")).strip()
    return t if t in TENANT_TYPES else "external"


def is_sandbox(row: dict | None) -> bool:
    return tenant_type_of(row) == "sandbox"


def tenant_status_of(row: dict | None) -> str:
    """租户生命周期状态：矩阵行 tenant_status 戳（db 态快照随迁位，非 active 才输出）；
    无戳＝active（yaml 态无生命周期源恒 active——派生清单 caliber 句现状口径，
    管理功能群一期 §1.5）。四态白名单外按 active 处理（戳由写入侧保证合法）。"""
    s = str((row or {}).get("tenant_status", "")).strip()
    return s if s in TENANT_STATUSES else "active"


def ensure_not_frozen(matrix, tenant: str, action: str) -> None:
    """冻结联动门（管理功能群一期 §1.5③/§1.6）：非 active 租户拒绝写面动作——
    授权登记/变更（validate_grant_action）与凭证签发/轮换（appkeys 同调本函数）。
    yaml 态矩阵无 tenant_status 戳＝恒放行（无生命周期源，门不生效，如实标注）。"""
    st = tenant_status_of(_matrix_row(matrix or [], tenant))
    if st == "active":
        return
    raise GrantAdminError(409, errors.TENANT_FROZEN,
                          f"租户 {tenant} 已冻结（状态 {st}）：{action}禁止——"
                          "解冻须管理员在租户管理页带工单号操作")


def _credential_grant_map(keys: list[dict], tenant: str) -> dict[str, set]:
    """名下「在用」凭证级授权合并面：{service: fields 集合}（代理行 fields 空集）。"""
    out: dict[str, set] = {}
    for k in keys or []:
        if str(k.get("tenant", "")) != tenant or k.get("status") != "active":
            continue
        for g in k.get("grants") or []:
            sid = str(g.get("service", ""))
            if not sid:
                continue
            fields = {str(f) for f in (g.get("fields") or []) if str(f).strip()}
            out.setdefault(sid, set()).update(fields)
    return out


def _tenant_level_grant_map(matrix: list[dict], tenant: str) -> dict[str, set]:
    """租户级授权面：{service: fields 集合}——只计在册生效行（status=revoked 停用行
    已是数据面收权语义（热加载即时生效），不计入覆盖，否则子集不变量对停用空转）。"""
    out: dict[str, set] = {}
    for row in matrix or []:
        if str(row.get("tenant", "")) != tenant:
            continue
        for g in row.get("grants") or []:
            if str(g.get("status") or "active") != "active":
                continue
            sid = str(g.get("service", ""))
            fields = {str(f) for f in (g.get("fields") or []) if str(f).strip()}
            out.setdefault(sid, set()).update(fields)
    return out


def assert_subset_invariant(matrix: list[dict], keys: list[dict], tenant: str) -> None:
    """五条之一「grants ⊆ 租户级不变量」：在用凭证级授权越出租户级 → 422 拒（design §3-1）。

    【三入口同门禁】本函数即统一校验函数：授权管理页写服务（本模块）先接；
    签发端点与 auth_admin CLI 接入随 1b-7 meta_writer 合入（遗留登记）。
    """
    cred = _credential_grant_map(keys, tenant)
    if not cred:
        return
    level = _tenant_level_grant_map(matrix, tenant)
    for sid, fields in sorted(cred.items()):
        if sid not in level:
            raise GrantAdminError(
                422, errors.SERVICE_NOT_GRANTED,
                f"收窄后租户级授权不含服务 {sid}，而名下在用凭证仍持有该服务授权——"
                "请先在签发页回收该凭证授权，或保留本服务的租户级登记")
        missing = fields - level[sid]
        if missing:
            raise GrantAdminError(
                422, errors.FIELDS_NOT_GRANTED,
                f"收窄后服务 {sid} 的租户级字段白名单不含凭证级在用字段："
                f"{', '.join(sorted(missing))}——请先回收凭证字段或保留勾选")


def derived_tenants(matrix: list[dict], keys: list[dict]) -> list[dict]:
    """派生租户清单（列表页数据源；一期临时件，1b-7 后撤换为 tenant 实体表读）。

    行＝auth_matrix 租户（权威序）∪ 注册表凭证租户（去重追加）；列＝ID/显示名/
    状态/类型/在用凭证数（只计 status=active）/在册授权数（含已停用行——可溯源）。
    一期无生命周期源：状态恒 active（四态枚举已在 TENANT_STATUSES，1b-7 后接真值）。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for row in matrix or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("tenant", "")).strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        grants = [g for g in (row.get("grants") or []) if isinstance(g, dict)]
        active_keys = sum(1 for k in (keys or [])
                          if str(k.get("tenant", "")) == tid and k.get("status") == "active")
        sandbox = is_sandbox(row)
        out.append({
            "tenant": tid,
            "displayName": str(row.get("display_name") or tid),
            "status": "active",
            "tenantType": tenant_type_of(row),
            "tenantTypeLabel": TENANT_TYPE_LABELS[tenant_type_of(row)],
            "oaTicket": str(row.get("oa_ticket") or ""),
            "activeKeys": active_keys,
            "grantsCount": len(grants),
            "readonly": sandbox,          # 沙箱＝演示模板整卡只读（设计稿 §3-3）
        })
    for k in (keys or []):
        tid = str(k.get("tenant", "")).strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        has_formal = any(str(x.get("tenant", "")) == tid and x.get("key_type") == "formal"
                         for x in (keys or []))
        active_keys = sum(1 for x in (keys or [])
                          if str(x.get("tenant", "")) == tid and x.get("status") == "active")
        # 注册表独有租户：无矩阵行＝未做 OA 登记面（授权数 0）；类型按凭证形态归类
        out.append({
            "tenant": tid,
            "displayName": str(k.get("caller_name") or tid),
            "status": "active",
            "tenantType": "external" if has_formal else "sandbox",
            "tenantTypeLabel": "外部" if has_formal else "沙箱",
            "oaTicket": "",
            "activeKeys": active_keys,
            "grantsCount": 0,
            "readonly": not has_formal,
        })
    return out


def _matrix_row(matrix: list[dict], tenant: str) -> dict | None:
    return next((r for r in (matrix or []) if str(r.get("tenant", "")) == tenant), None)


def tenant_detail(matrix: list[dict], services: dict, keys: list[dict], tenant: str) -> dict:
    """租户详情视图：租户卡＋服务∪代理混排授权矩阵＋可选服务清单＋凭证级只读摘要。

    services＝已合并代理的服务命名空间（admin_api._namespace_with_proxies 同款；
    代理条目 kind=proxy、fields=[]）。appkey 一律掩码（完整值不出端点）。
    """
    tenant = str(tenant or "").strip()
    row = _matrix_row(matrix, tenant)
    cred = _credential_grant_map(keys, tenant)
    if row is None:
        if not cred:
            raise GrantAdminError(404, errors.UNKNOWN_TENANT, f"租户 {tenant} 未在登记清单（授权矩阵＋凭证注册表）")
        row = {}     # 注册表独有租户：详情照出（无矩阵行，登记动作会创建）
    sandbox = is_sandbox(row)

    grant_rows: list[dict] = []
    for g in row.get("grants") or []:
        if not isinstance(g, dict):
            continue
        sid = str(g.get("service", ""))
        svc = services.get(sid) or {}
        is_proxy = str(g.get("kind", "")).strip() == "proxy" or svc.get("kind") == "proxy"
        fields = [str(f) for f in (g.get("fields") or []) if str(f).strip()]
        available = [] if is_proxy else [str(f) for f in (svc.get("fields") or [])]
        g_ticket = str(g.get("oa_ticket") or "")
        t_ticket = str(row.get("oa_ticket") or "")
        effective = g_ticket or t_ticket
        grant_rows.append({
            "service": sid,
            "serviceName": str(svc.get("name") or sid),
            "kind": "proxy" if is_proxy else "service",
            "kindLabel": "整路由" if is_proxy else "字段服务",
            "route": str(svc.get("route") or g.get("endpoint") or ""),
            "fields": fields,
            "availableFields": available,
            "fieldSpace": str(svc.get("field_space") or ("proxy" if is_proxy else "")),
            "oaTicket": g_ticket,
            "oaEffective": effective,
            "oaSource": "grant" if g_ticket else ("tenant" if t_ticket else "none"),
            "effectiveFrom": str(g.get("effective_from") or ""),
            "effectiveTo": str(g.get("effective_to") or ""),
            "status": str(g.get("status") or "active"),
            "readonly": sandbox,      # 沙箱整卡只读（演示模板，与活租户矩阵脱钩）
        })

    available_services: list[dict] = []
    for sid, svc in sorted((services or {}).items()):
        if not isinstance(svc, dict) or svc.get("status") not in (None, "published"):
            continue
        is_proxy = svc.get("kind") == "proxy"
        available_services.append({
            "id": sid,
            "name": str(svc.get("name") or sid),
            "kind": "proxy" if is_proxy else "service",
            "kindLabel": "整路由" if is_proxy else "字段服务",
            "route": str(svc.get("route") or ""),
            "fields": [] if is_proxy else [str(f) for f in (svc.get("fields") or [])],
            "fieldSpace": str(svc.get("field_space") or ("proxy" if is_proxy else "")),
        })

    credentials = []
    for k in (keys or []):
        if str(k.get("tenant", "")) != tenant:
            continue
        appkey = str(k.get("appkey", ""))
        credentials.append({
            "appkeyMasked": (appkey[:7] + "****" + appkey[-4:]) if appkey else "",
            "keyType": str(k.get("key_type") or ""),
            "status": str(k.get("status") or ""),
            "services": sorted(_credential_grant_map([k], tenant).keys()),
        })

    return {
        "tenant": tenant,
        "displayName": str(row.get("display_name") or tenant),
        "status": "active",
        "tenantType": tenant_type_of(row),
        "tenantTypeLabel": TENANT_TYPE_LABELS[tenant_type_of(row)],
        "oaTicket": str(row.get("oa_ticket") or ""),
        "readonly": sandbox,
        "grants": grant_rows,
        "availableServices": available_services,
        "credentials": credentials,
        "caliber": {
            "oaRule": "审批已在 OA 完成，此处仅登记结果（ADR-0003）；平台不做任何 OA 系统对接",
            "sandbox": "沙箱租户为平台演示模板，授权只读、不可编辑与停用",
            "effective": "生效区间一期只登记与展示；到期自动拦截随后续版本落地（停用可即时收权）",
            "writeSide": "写操作仅管理员；只读视图全角色可见",
        },
    }


# ----------------------------------------------------------------------
# 写服务：三动作（upsert 新增/变更、disable 停用、enable 再启用）
# ----------------------------------------------------------------------

GRANT_ACTIONS = ("upsert", "disable", "enable")


def _norm_fields(raw) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GrantAdminError(400, errors.INVALID_REQUEST, "fields 须为字段名列表")
    out, seen = [], set()
    for f in raw:
        name = str(f).strip()
        if not name:
            continue
        if name in seen:
            raise GrantAdminError(400, errors.INVALID_FIELD, f"字段重复：{name}")
        seen.add(name)
        out.append(name)
    return out


def _check_ticket_format(ticket: str) -> None:
    if not OA_TICKET_RE.match(ticket):
        raise GrantAdminError(400, errors.INVALID_OA_TICKET,
                              f"OA 单号格式提醒：{ticket!r}（4~64 位字母数字-_）；平台只做登记，不做 OA 系统核对")


def validate_grant_action(matrix: list[dict], services: dict, keys: list[dict],
                          payload: dict) -> dict:
    """三动作提交校验（双层五条的服务层落地；只读校验不改矩阵）。

    返回归一化 payload（含 tenant_row/grant_row/svc 解析结果）；拒绝即 raise
    GrantAdminError（status/code/message）。失败尝试由端点层记 denied 审计事件。
    """
    if not isinstance(payload, dict):
        raise GrantAdminError(400, errors.INVALID_REQUEST, "请求体须为 JSON 对象")
    action = str(payload.get("action") or "").strip()
    if action not in GRANT_ACTIONS:
        raise GrantAdminError(400, errors.INVALID_ACTION,
                              f"action 须为 {'/'.join(GRANT_ACTIONS)}（新增/变更、停用、启用）")
    tenant = str(payload.get("tenant") or "").strip()
    if not TENANT_ID_RE.match(tenant):
        raise GrantAdminError(400, errors.INVALID_TENANT, "租户 ID 非法（小写字母/数字/_-，2~64 位）")

    known = {str(r.get("tenant", "")) for r in (matrix or []) if isinstance(r, dict)}
    known |= {str(k.get("tenant", "")) for k in (keys or []) if k.get("tenant")}
    if tenant not in known:
        # 五条之二：租户存在性（一期派生清单；新租户首登仍走线下登记，本页不建租户）
        raise GrantAdminError(400, errors.TENANT_NOT_REGISTERED,
                              f"租户 {tenant} 未在登记清单（授权矩阵＋凭证注册表）；新租户请先经 OA 审批并线下登记")

    row = _matrix_row(matrix, tenant)
    if row is None:
        # 注册表独有租户（无矩阵行）：按凭证形态归类类型——仅沙箱凭证＝沙箱（只读）
        has_formal = any(str(k.get("tenant", "")) == tenant and k.get("key_type") == "formal"
                         for k in (keys or []))
        row = {} if has_formal else {"tenant_type": "sandbox"}
    if is_sandbox(row):
        # 五条之三：沙箱租户只读（trial template 演示模板，与活租户矩阵脱钩）
        raise GrantAdminError(409, errors.INVALID_REQUEST,
                              "沙箱租户为平台演示模板，授权只读——不可新增、变更、停用或启用")
    # 冻结联动门（管理功能群一期 §1.5③）：frozen/terminating/cancelled 租户拒绝
    # 授权登记/变更（db 态生效；yaml 态无戳恒 active＝门不生效，如实标注）
    ensure_not_frozen(matrix, tenant, "授权登记/变更")

    sid = str(payload.get("service") or "").strip()
    svc = services.get(sid)
    if not isinstance(svc, dict):
        # 五条之四前半：服务必须在册（服务∪代理合并命名空间）
        raise GrantAdminError(400, errors.UNKNOWN_SERVICE, f"服务未注册或已下线：{sid}")

    grant_row = next((g for g in (row.get("grants") or [])
                      if isinstance(g, dict) and str(g.get("service", "")) == sid), None)

    norm = {
        "action": action, "tenant": tenant, "service": sid,
        "svc": svc, "row": row, "grant_row": grant_row,
        "oa_ticket": str(payload.get("oa_ticket") or "").strip(),
        "effective_from": str(payload.get("effective_from") or "").strip(),
        "effective_to": str(payload.get("effective_to") or "").strip(),
        "confirm": payload.get("confirm") is True,
        "fields": [],
    }

    is_proxy = svc.get("kind") == "proxy"
    if action == "upsert":
        # 五条之五：OA 单号（租户级必填；grant 级可选回落租户级；沙箱豁免已在上拦）
        ticket = norm["oa_ticket"] or str(row.get("oa_ticket") or "")
        if not ticket:
            raise GrantAdminError(400, errors.OA_TICKET_REQUIRED,
                                  "OA 单号必填：审批已在 OA 完成，此处仅登记结果——请填审批单号后提交")
        if norm["oa_ticket"]:
            _check_ticket_format(norm["oa_ticket"])   # 填了就按格式提醒（真伪不做核对）
        # 生效区间（可选；ISO 日期；from ≤ to）
        for key in ("effective_from", "effective_to"):
            if norm[key] and not DATE_RE.match(norm[key]):
                raise GrantAdminError(400, errors.INVALID_EXPIRES_AT,
                                      f"{key} 须为 YYYY-MM-DD（留空＝长期有效）")
        if norm["effective_from"] and norm["effective_to"] \
                and norm["effective_from"] > norm["effective_to"]:
            raise GrantAdminError(400, errors.INVALID_EXPIRES_AT, "生效区间颠倒（起始晚于截止）")
        fields = _norm_fields(payload.get("fields"))
        if is_proxy:
            # 整路由授权：无字段列，空集合法（口径同 tests/test_appkeys.py:174）
            norm["fields"] = []
        else:
            if not fields:
                raise GrantAdminError(400, errors.EMPTY_FIELDS, "字段白名单不能为空：至少勾选一个字段")
            # 五条之四后半：字段空间随服务绑定（白名单字段须真实在册）
            allowed = {str(f) for f in (svc.get("fields") or [])}
            outside = [f for f in fields if allowed and f not in allowed]
            if outside:
                raise GrantAdminError(400, errors.FIELD_NOT_IN_DICT,
                                      f"字段不在服务「{svc.get('name') or sid}」的开放字段清单内：{', '.join(outside)}")
            norm["fields"] = fields
    elif action == "disable":
        if grant_row is None:
            raise GrantAdminError(404, errors.SERVICE_NOT_GRANTED,
                                  f"租户 {tenant} 名下无服务 {sid} 的授权登记")
        if str(grant_row.get("status") or "active") != "active":
            raise GrantAdminError(409, errors.NOT_ACTIVE, "该授权已停用（可再启用，无需重复停用）")
        if not norm["confirm"]:
            # 两段式停用：第一段拒执行、回 diff 预览（防表单默认勾空被静默剔除的 C1 病根）
            raise GrantAdminError(400, errors.INVALID_REQUEST,
                                  "缺第二段确认：停用即时收权，请先核对变更预览并勾选确认")
    else:   # enable
        if grant_row is None:
            raise GrantAdminError(404, errors.SERVICE_NOT_GRANTED,
                                  f"租户 {tenant} 名下无服务 {sid} 的授权登记")
        if str(grant_row.get("status") or "active") == "active":
            raise GrantAdminError(409, errors.NOT_DISABLED, "该授权在册生效中，无需启用")

    # 五条之一：grants ⊆ 租户级不变量（改写后投影上对照在用凭证级授权）
    if action in ("upsert", "disable"):
        draft = _draft_matrix(matrix, norm)
        assert_subset_invariant(draft, keys, tenant)
    return norm


def _draft_matrix(matrix: list[dict], norm: dict) -> list[dict]:
    """把待写变更投影到矩阵副本（不落盘），供子集不变量校验。

    upsert＝按提交内容改写该服务行；disable＝该行置 revoked（收权语义）。
    """
    import copy
    draft = copy.deepcopy(matrix or [])
    row = _matrix_row(draft, norm["tenant"])
    if row is None:
        row = {"tenant": norm["tenant"], "appkey": "draft-placeholder", "oa_ticket": "", "grants": []}
        draft.append(row)
    grants = [g for g in row.get("grants", []) if isinstance(g, dict)]
    if norm["action"] == "disable":
        target = next((g for g in grants if str(g.get("service", "")) == norm["service"]), None)
        if target is not None:
            target["status"] = "revoked"
        row["grants"] = grants
        return draft
    grants = [g for g in grants if str(g.get("service", "")) != norm["service"]]
    draft_grant: dict = {"service": norm["service"], "fields": list(norm["fields"])}
    if norm["svc"].get("kind") == "proxy":
        draft_grant["kind"] = "proxy"
    grants.append(draft_grant)
    row["grants"] = grants
    return draft


def apply_grant_action(path: Path, services: dict, keys: list[dict],
                       payload: dict, operator: str) -> tuple[dict, dict]:
    """三动作落盘：校验 → 矩阵改写 → 原子写。返回 (result_view, diff)。

    抛 GrantAdminError＝拒绝（矩阵不动）；MatrixFormatError＝底稿不合法（拒改写）。
    """
    data = load_matrix_file(path)
    errs, _ = validate_matrix(data)
    if errs:
        raise GrantAdminError(422, errors.INVALID_REQUEST,
                              "授权登记底稿不合法，拒绝改写：" + "；".join(errs[:3]))
    matrix = data.setdefault("matrix", [])
    norm = validate_grant_action(matrix, services, keys, payload)

    row = _matrix_row(matrix, norm["tenant"])
    if row is None:
        # 注册表独有租户首登：凭证回落登记（双路由单钥匙口径，同 demo-1020）
        link = next((k for k in (keys or [])
                     if str(k.get("tenant", "")) == norm["tenant"] and k.get("appkey")), None)
        row = {"tenant": norm["tenant"]}
        if link:
            row["appkey"] = str(link["appkey"])
            if link.get("secret_hash"):
                row["secret_hash"] = str(link["secret_hash"])
        else:
            row["appkey"] = gen_appkey({str(r.get("appkey")) for r in matrix if r.get("appkey")})
        row["display_name"] = next((str(k.get("caller_name") or "") for k in (keys or [])
                                    if str(k.get("tenant", "")) == norm["tenant"] and k.get("caller_name")),
                                   "") or norm["tenant"]
        row["tenant_type"] = "external"
        row["status"] = "active"
        row["grants"] = []
        matrix.append(row)

    diff: dict = {"addedFields": [], "removedFields": [], "before": None, "after": None}
    if norm["action"] == "upsert":
        is_proxy = norm["svc"].get("kind") == "proxy"
        old = norm["grant_row"]
        old_fields = [str(f) for f in ((old or {}).get("fields") or []) if str(f).strip()]
        if old is not None:
            diff["before"] = {"fields": old_fields, "oaTicket": str(old.get("oa_ticket") or ""),
                              "effectiveFrom": str(old.get("effective_from") or ""),
                              "effectiveTo": str(old.get("effective_to") or ""),
                              "status": str(old.get("status") or "active")}
            row["grants"] = [g for g in row["grants"] if g is not old]
        grant: dict = {"service": norm["service"]}
        if norm["svc"].get("route") or (old or {}).get("endpoint"):
            grant["endpoint"] = str(norm["svc"].get("route") or old.get("endpoint"))
        if is_proxy:
            grant["kind"] = "proxy"
        if norm["oa_ticket"]:
            grant["oa_ticket"] = norm["oa_ticket"]       # grant 级单号可选；缺省回落租户级
        grant["fields"] = list(norm["fields"])
        if norm["effective_from"]:
            grant["effective_from"] = norm["effective_from"]
        if norm["effective_to"]:
            grant["effective_to"] = norm["effective_to"]
        grant["status"] = "active"
        row.setdefault("grants", []).append(grant)
        diff["addedFields"] = [f for f in norm["fields"] if f not in old_fields]
        diff["removedFields"] = [f for f in old_fields if f not in norm["fields"]]
        if old is None:
            diff["addedService"] = True
        diff["after"] = {"fields": list(norm["fields"]),
                         "oaTicket": str(grant.get("oa_ticket") or row.get("oa_ticket") or ""),
                         "effectiveFrom": norm["effective_from"],
                         "effectiveTo": norm["effective_to"], "status": "active"}
        # 若本行动作把租户级单号一并登记（原行无单号）→ 回填租户级（五条之五登记位）
        if not str(row.get("oa_ticket") or "") and norm["oa_ticket"]:
            row["oa_ticket"] = norm["oa_ticket"]
    elif norm["action"] == "disable":
        g = norm["grant_row"]
        diff["before"] = {"status": str(g.get("status") or "active"),
                          "fields": [str(f) for f in (g.get("fields") or [])]}
        g["status"] = "revoked"
        diff["after"] = {"status": "revoked"}
    else:   # enable
        g = norm["grant_row"]
        g["status"] = "active"
        diff["after"] = {"status": "active"}

    data["meta"] = {"updated": _utc_now_iso(), "updated_by": operator or "grants_admin"}
    save_matrix(path, data, operator=operator)   # operator 贯通 db 台账（收口批项3）
    # 视图取自写入态 data 本体（db 模式落库不落文件，回读 path 会拿到陈旧底稿；
    # yaml 模式 safe_dump 同对象往返＝与回读等价——收口批项3）
    view = tenant_detail(data.get("matrix", []), services, keys, norm["tenant"])
    return view, diff
