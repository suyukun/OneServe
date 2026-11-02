#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OneServe 元数据迁移工具（卡-1b7 步4 · ADR-0021 v1.1）：四 yaml → meta 库十表装载。

用法：
  python3 scripts/meta_migrate.py apply [--dry-run] [--repeat-safe]
  python3 scripts/meta_migrate.py verify
  python3 scripts/meta_migrate.py status

子命令：
  apply   四 yaml（services/upstreams/appkeys/auth_matrix）→ 十表装载。
          恒幂等：主键表全 upsert（ON CONFLICT DO UPDATE）、grant 全删重插
          （矩阵/注册表为唯一权威，事务内收敛；grant 无被引用方，安全）。
          --dry-run    只打印将执行的装载统计，零写入（连库只读展示现状行数）。
          --repeat-safe 显式声明幂等重跑（与默认行为同一路径；声明性开关）。
  verify  只对账不装载：四源条数 vs 表内条数逐表打印＋字段级哈希对比；
          任一表不一致 exit 2（卡-1b7 步7 对账门禁的前身）。
  status  只读显示十表行数与最近 version_history（连接走 meta_migrate 角色）。

数据规则（卡步 4 ＋任务单原文）：
  services.yaml   双区合并装载 service_catalog：service_id 唯一；console 写区条目与
                  runtime 区同服务条目按**字段级合并**（收口批项1 处置：原「整条覆盖」
                  丢 7 对孪生条目执行字段 guard/audit/response/archetype）——console 键
                  覆盖（描述/状态面），runtime 独有执行字段保留；origin 语义不变
                  （console 条目在场＝生效定义经控制台覆盖 → origin=console）；
                  desc 缺失按现状空串；trace/history 不迁（version_history 替代）。
  upstreams.yaml  proxies[] 原样平移 upstream_proxy（trace 不迁）。
  appkeys.yaml    keys[] → credential(source=appkeys, status 原样 active|disabled)。
  auth_matrix.yaml matrix[] 行级 oa_ticket＝租户开通单号 → tenant（type 取行级
                  tenant_type 戳，未标＝external——1b-8 扩展字段平移，收口批项2）；
                  矩阵 grants[] → grant(scope=tenant)；appkeys grants[] →
                  grant(scope=credential, credential_appkey 必填)；grant 级扩展字段
                  （oa_ticket 单次授权溯源／effective_from/to 生效区间，1b-8 授权页
                  登记面）随迁（grant 级单号优先，回落行级/凭证级）。
                  矩阵内嵌 appkey 串收编 credential(source=matrix_fallback)：
                  同串在 appkeys 注册表＝同一把凭证两源合并（appkeys 侧字段为基，
                  矩阵侧补授权背书单号）；partner-demo 过期键收编即 status=revoked；
                  demo-1020 双路由单钥匙按既有登记照迁（无特判）。
                  仅凭证侧自造串（t504142bf8f/walkthrough-e2e/perf-ops 等）＝
                  tenant(type=sandbox, oa_ticket_open 空)。
  field_space     七域种子（schema_drift.FIELD_SPACES 同源）；wide297 drift_mode=enforce
                  ＋dict_csv_ref 引用位，其余 not_applicable。
  trial template  沙箱演示集（demo_merchant×TRIAL_SOURCE 服务）平移种子 sync/trial_template.yaml
                  （卡待核#3 已裁默认处置；本工具只生成种子不装载——模板消费归 1b-8/步 6）。
  台账            装载有实际变化才发一个 admin_event（source=migration）＋按实体
                  version_history 行（event_id FK 对偶，DDL 钉因果链）；无变化重跑
                  ＝台账零增长（幂等语义含台账）。
  负例            装载前校验（tenant_id 正则/expires_at 值域/grant 目标可判定/formal 必带
                  单号等对齐 0001 DDL CHECK）——任一脏数据在零写入前提下的 exit 2 拒载。

连接（META_* 六变量，compose.metadata.yml 同源；口令永不进仓）：
  META_DB_USER / META_DB_PASSWORD / META_DB_NAME —— 实例引导三件套（compose 渲染用）
  META_APP_PASSWORD / META_RO_PASSWORD / META_MIGRATE_PASSWORD —— 三角色口令
  执行器：本工具经 docker compose -f compose.metadata.yml -p oneserve-meta exec -T
  metadata-db psql（容器网内直连，宿主机无 psql 客户端/无宿主端口＝项目现状）；
  测试注入 PsqlRunner 同款协议（stdin SQL → stdout）。
  禁碰 dev 容器组：本工具只对 -p oneserve-meta 隔离栈操作。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# 四 yaml 源目录可经 env 指向临时副本（tests 级隔离；缺省＝仓库 service/，行为不变）
SERVICE_DIR = Path(os.environ.get("META_MIGRATE_SRC_DIR") or (REPO_ROOT / "service"))
SERVICES_YAML = SERVICE_DIR / "services.yaml"
UPSTREAMS_YAML = SERVICE_DIR / "upstreams.yaml"
APPKEYS_YAML = SERVICE_DIR / "registry" / "appkeys.yaml"
AUTH_MATRIX_YAML = SERVICE_DIR / "auth_matrix.yaml"
TRIAL_TEMPLATE_YAML = REPO_ROOT / "sync" / "trial_template.yaml"

CONSOLE_MARKER = "# ==== console_services"

# 租户 ID 正则（与 appkeys.py TENANT_ID_RE / 0001 DDL tenant_id_format_check 三方同源）
TENANT_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,62}[a-z0-9])?$")
# 租户生命周期四态（0001 DDL tenant_status_check 同源白名单；管理功能群一期 P0-3）
TENANT_STATUSES = ("active", "frozen", "terminating", "cancelled")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SECRET_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_EMPTY = "sha256:" + hashlib.sha256(b"").hexdigest()


def tenant_status_stamp(value) -> str:
    """矩阵行 tenant_status 戳归一（管理功能群一期 P0-3）：缺省 active（存量行为
    不变）；非 active 戳＝快照投影随迁位（meta_snapshot 沿 tenant_type 戳先例，
    非 active 才输出）；四态白名单外由 validate_rows 拒载（DDL CHECK 同源）。"""
    s = str(value or "").strip()
    return s if s in TENANT_STATUSES else "active"

# 七域字段空间种子（service/app/schema_drift.py FIELD_SPACES 同源；字典 CSV 只登引用位）
FIELD_SPACE_SEEDS = [
    ("wide297", "enforce", "sync/schema_usr_label_wide_297.csv"),
    ("news", "not_applicable", None),
    ("proxy", "not_applicable", None),
    ("usr_basic_info", "not_applicable", "sync/schema_usr_basic_info.csv"),
    ("usr_asset_tag", "not_applicable", "sync/schema_usr_asset_tag.csv"),
    ("card_acct_summary", "not_applicable", "sync/schema_card_acct_summary.csv"),
    ("yb", "not_applicable", None),
]

# trial template 种子来源（appkeys.py TRIAL_SOURCE_TENANT/TRIAL_SOURCE_SERVICE 同源常量）
TRIAL_SOURCE_TENANT = "demo_merchant"
TRIAL_SOURCE_SERVICE = "v1-svc-usr-asset-tag"

EXIT_OK, EXIT_MISMATCH, EXIT_DIRTY = 0, 2, 2


class MigrateError(Exception):
    """迁移期错误（人话 message；exit 2）。"""


# ----------------------------------------------------------------------
# yaml 源装载与清洗（纯函数：四 yaml → 规范化行集；可独立单测）
# ----------------------------------------------------------------------

def _yaml(path: Path):
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _ts(value):
    """yaml 时间串 → 规范化 timestamptz 字面量（UTC ISO；None 原样）。"""
    if value in (None, ""):
        return None
    s = str(value).strip()
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    dt = _dt.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")


def load_runtime_console_services(path: Path | None = None):
    path = path or SERVICES_YAML
    """services.yaml 双区解析：返回 (runtime_entries, console_entries) 原始列表。"""
    if not path.is_file():
        return [], []
    text = path.read_text(encoding="utf-8")
    top = _yaml(path)
    runtime = top.get("services") or []
    console = []
    idx = text.find(CONSOLE_MARKER)
    if idx >= 0:
        console = (_yaml_from_text(text[idx:]) or {}).get("console_services") or []
    return runtime, console


def _yaml_from_text(text: str):
    import yaml
    return yaml.safe_load(text) or {}


# 运行时执行字段（孪生条目合并的关注面：console 写区条目不带，原整条覆盖曾致库内丢失
# ——收口批项1 黑体项；快照还原与读侧执行都消费这些键）
EXEC_FIELDS = ("guard", "audit", "response", "archetype")


def plan_service_catalog(services_path: Path | None = None):
    services_path = services_path or SERVICES_YAML
    """双区合并装载 service_catalog：console 写区条目与 runtime 区同服务条目按字段级合并。

    合并策略（收口批项1 处置）：以 runtime 条目为底，console 条目逐键覆盖（值非 None 才
    覆盖——显式 null 视为「未表达」，绝不抹掉执行字段）；runtime 独有键（guard/audit/
    response/archetype 等执行字段）自然保留。origin 语义不变：console 条目在场＝生效
    定义经控制台覆盖 → origin=console（快照按 origin 还原双区旧格式，ADR-0021 v1.1 已裁）。

    返回 (rows, stats)：rows 按 service_id 唯一；stats 记录覆盖/执行字段补全/缺失 desc 等。
    """
    runtime, console = load_runtime_console_services(services_path)
    merged: dict[str, dict] = {}
    origin: dict[str, str] = {}
    for e in runtime:
        sid = str(e.get("service_id") or e.get("id") or "").strip()
        if sid:
            merged[sid] = e
            origin[sid] = "runtime"
    covered = exec_restored = 0
    for e in console:
        sid = str(e.get("id") or "").strip()
        if not sid:
            continue
        if sid in merged:
            covered += 1
            m = dict(merged[sid])              # runtime 为底（执行字段在此）
            for k, v in e.items():
                if v is not None:              # console 非 None 键覆盖
                    m[k] = v
            if any(k in m and k not in e for k in EXEC_FIELDS):
                exec_restored += 1             # 本次合并自运行时区保住执行字段的孪生条目数
            merged[sid] = m
        else:
            merged[sid] = e
        origin[sid] = "console"
    rows, desc_filled = [], 0
    for sid, e in merged.items():
        desc = e.get("desc")
        if desc is None:
            desc = ""
            desc_filled += 1
        rows.append({
            "service_id": sid,
            "name": e.get("name"),
            "description": str(desc),
            "route": str(e.get("route") or ""),
            "method": str(e.get("method") or "POST"),
            "query_key": e.get("key"),
            "archetype": e.get("archetype"),
            "field_space": str(e.get("field_space") or "wide297"),
            "origin": origin[sid],
            "status": str(e.get("status") or "offline"),
            "backend": e.get("backend") or {},
            "params": e.get("params") or [],
            "fields": e.get("fields") or [],
            "response": e.get("response"),
            "guard": e.get("guard"),
            "audit": e.get("audit"),
            "scope": e.get("scope"),
            "update_frequency": e.get("update_frequency"),
            "owner": e.get("owner"),
            "published_at": _ts(e.get("published_at")),
            "last_modified_by": e.get("last_modified_by"),
            "last_modified_at": _ts(e.get("last_modified_at")),
        })
    stats = {"runtime": len(runtime), "console": len(console), "console_overrides": covered,
             "exec_fields_restored": exec_restored,
             "desc_missing_filled_empty": desc_filled}
    return rows, stats


def plan_upstreams(path: Path | None = None):
    path = path or UPSTREAMS_YAML
    """upstreams.yaml → upstream_proxy 行（trace 不迁）。"""
    data = _yaml(path)
    proxies = data.get("proxies") or []
    rows = [{
        "id": p.get("id"),
        "service_name": p.get("service_name"),
        "route": str(p.get("route") or ""),
        "method": str(p.get("method") or "POST"),
        "upstream_url": str(p.get("upstream_url") or ""),
        "timeout_sec": int(p.get("timeout_sec") or 5),
        "retry_count": int(p.get("retry_count") or 0),
        "reuse_upstream": bool(p.get("reuse_upstream") or False),
        "upstream_name": p.get("upstream_name"),
        "status": str(p.get("status") or "draft"),
        "scope": p.get("scope"),
        "last_modified_by": p.get("last_modified_by"),
        "last_modified_at": _ts(p.get("last_modified_at")),
        "published_at": _ts(p.get("published_at")),
    } for p in proxies]
    return rows


def _grant_row(tenant: str, scope: str, g: dict, service_ids: set, proxy_ids: set,
               oa_ticket: str | None):
    sid = str(g.get("service") or "")
    if sid in service_ids and sid in proxy_ids:
        # demo-1020 双路由特例等罕见重名：服务表优先（矩阵登记语义＝服务授权，代理是旁路）
        kind, svc_id, proxy_id = "service", sid, None
    elif sid in service_ids:
        kind, svc_id, proxy_id = "service", sid, None
    elif sid in proxy_ids:
        kind, svc_id, proxy_id = "proxy", None, sid
    else:
        raise MigrateError(f"grant 目标不可判定：{tenant} × {sid}（服务表与代理表均无此 id）")
    fields = [str(f) for f in (g.get("fields") or [])]
    # 1b-8 grant 级扩展字段（收口批项2）：单次授权溯源单号（优先，回落行级/凭证级）
    # ＋生效区间（YYYY-MM-DD；NULL＝长期有效，同 grants_admin.DATE_RE 口径）
    eff = {}
    for key in ("effective_from", "effective_to"):
        v = g.get(key)
        eff[key] = str(v).strip() if v not in (None, "") else None
    return {
        "tenant_id": tenant, "scope": scope,
        "credential_appkey": None if scope == "tenant" else None,  # 凭证级由装载器回填
        "target_kind": kind, "service_id": svc_id, "proxy_id": proxy_id,
        "fields": fields, "endpoint": g.get("endpoint"),
        "oa_ticket": (str(g["oa_ticket"]).strip() if g.get("oa_ticket") not in (None, "") else oa_ticket),
        **eff,
    }


def plan_tenants_creds_grants(appkeys_path: Path | None = None,
                              matrix_path: Path | None = None):
    appkeys_path = appkeys_path or APPKEYS_YAML
    matrix_path = matrix_path or AUTH_MATRIX_YAML
    """appkeys.yaml ＋ auth_matrix.yaml → tenant/credential/grant 三表行集（合并去重）。

    返回 (tenants, credentials, grants, stats)。
    """
    ak = _yaml(appkeys_path)
    keys = ak.get("keys") or []
    mx = (_yaml(matrix_path).get("matrix")) or []

    # 服务/代理 id 全集（grant target_kind 判定）
    svc_rows, _ = plan_service_catalog()
    service_ids = {r["service_id"] for r in svc_rows}
    proxy_ids = {p["id"] for p in plan_upstreams()}
    today = _dt.date.today().isoformat()

    # ---- tenant：矩阵 3 源字段优先；仅凭证侧自造串＝sandbox ----
    tenants: dict[str, dict] = {}
    for row in mx:
        tid = str(row.get("tenant") or "")
        # 1b-8 行级 tenant_type 戳随迁（收口批项2）：internal|external|sandbox；
        # 未标＝external（存量行为不变）。sandbox 行开通单号恒不入 tenant（DDL CHECK
        # 「沙箱免单号」例外配对；行带单号如 OA-DEMO-1020 属授权溯源，保留在凭证合并
        # 与 grant.oa_ticket——下方 credential/grant 装载不裁单号）
        ttype = (str(row.get("tenant_type")).strip()
                 if row.get("tenant_type") not in (None, "") else "external")
        # 行级 tenant_status 戳读回（管理功能群一期 P0-3）：快照投影对非 active 租户
        # 随迁 tenant_status 位（meta_snapshot 沿 tenant_type 戳先例）——规划器不再
        # 硬编码 active，防「frozen 租户被任意授权登记/CLI register 静默解冻」；
        # 白名单外戳 validate_rows 拒载（tenant_status_stamp 归一缺省 active）
        tenants[tid] = {
            "tenant_id": tid,
            "display_name": str(row.get("display_name") or tid),
            "tenant_type": ttype,
            "status": tenant_status_stamp(row.get("tenant_status")),
            "org_ref": None,
            # 沙箱行单号不落开通位（DDL CHECK）；非沙箱缺单号由 validate_rows 拒载
            "oa_ticket_open": None if ttype == "sandbox" else (row.get("oa_ticket") or None),
            "remark": "auth_matrix 登记",
        }
    for k in keys:
        tid = str(k.get("tenant") or "")
        if tid not in tenants:
            # 仅凭证侧自造串（appkeys.yaml 无租户实体、无戳载体）：非 active 租户
            # 由快照投影壳行承载 tenant_status（matrix_rows_from），故能走到本分支
            # 的恒为 active 新租户——缺省即正解（P0-3 口径注记）
            tenants[tid] = {
                "tenant_id": tid,
                "display_name": tid,
                "tenant_type": "sandbox",        # 仅凭证侧自造串
                "status": tenant_status_stamp(None),
                "org_ref": None,
                "oa_ticket_open": None,          # 沙箱 OA 空（DDL CHECK 例外配对）
                "remark": "appkeys 注册表（控制台签发）",
            }

    # ---- credential：appkeys 为基；矩阵内嵌串收编（source=matrix_fallback）两源合并 ----
    credentials: dict[str, dict] = {}
    for k in keys:
        credentials[str(k.get("appkey"))] = {
            "appkey": str(k.get("appkey")),
            "secret_hash": k.get("secret_hash"),
            "tenant_id": str(k.get("tenant") or ""),
            "caller_name": k.get("caller_name"),
            "app_name": k.get("app_name"),
            "key_type": str(k.get("key_type") or "sandbox"),
            "source": "appkeys",
            "status": str(k.get("status") or "active"),
            "oa_ticket": (k.get("oa_ticket") or None) or None,
            "expires_at": (str(k.get("expires_at")) if k.get("expires_at") not in (None, "") else None),
            "quota_qps": int(k.get("quota_qps") or 0),
            "quota_daily": int(k.get("quota_daily") or 0),
            "tag_sets": k.get("tag_sets") or [],
            "issued_at": _ts(k.get("issued_at")),
            "rotated_at": None if not k.get("rotated_at") else _ts(k.get("rotated_at")),
            "overlap_expires_at": None if not k.get("overlap_expires_at")
                                  else _ts(k.get("overlap_expires_at")),
            "superseded_by": k.get("superseded_by"),
            "rotation_of": k.get("rotation_of"),
        }
    fallback_n = revoked_n = 0
    for row in mx:
        ak_str = row.get("appkey")
        if not ak_str:
            continue                              # 未发凭证的占位行：只贡献 tenant/grant
        ak_str = str(ak_str)
        expired = (row.get("status") != "revoked"
                   and str((credentials.get(ak_str) or {}).get("expires_at") or "")[:10] < today
                   and DATE_RE.match(str((credentials.get(ak_str) or {}).get("expires_at") or "")))
        if ak_str in credentials:
            rec = credentials[ak_str]             # 同串两源＝同一把凭证：矩阵侧补背书
            rec["source"] = "matrix_fallback"
            rec["oa_ticket"] = row.get("oa_ticket") or rec.get("oa_ticket")
            if row.get("status") == "revoked":
                rec["status"] = "revoked"
            if expired or row.get("status") != "revoked" and _is_expired(rec, today):
                pass                              # 过期判定在下方统一收口
            fallback_n += 1
        else:
            credentials[ak_str] = {
                "appkey": ak_str,
                "secret_hash": row.get("secret_hash"),
                "tenant_id": str(row.get("tenant") or ""),
                "caller_name": row.get("display_name") or row.get("tenant"),
                "app_name": None,
                "key_type": "formal" if str(ak_str).startswith("ak_") else "sandbox",
                "source": "matrix_fallback",
                "status": "active",
                "oa_ticket": row.get("oa_ticket") or None,
                "expires_at": None,
                "quota_qps": 0, "quota_daily": 0,
                "tag_sets": [], "issued_at": None,
                "rotated_at": None, "overlap_expires_at": None,
                "superseded_by": None, "rotation_of": None,
            }
            fallback_n += 1
    # 过期键收编即 revoked（卡点名 partner-demo；对全部 matrix_fallback 收编行统一口径）
    for rec in credentials.values():
        if rec["source"] == "matrix_fallback" and rec["status"] == "active" and _is_expired(rec, today):
            rec["status"] = "revoked"
            revoked_n += 1

    # ---- grant：矩阵=租户级（OA 权威）；appkeys=凭证级（签发语义） ----
    grants: list[dict] = []
    for row in mx:
        tid = str(row.get("tenant") or "")
        for g in row.get("grants") or []:
            r = _grant_row(tid, "tenant", g, service_ids, proxy_ids, row.get("oa_ticket"))
            grants.append(r)
    cred_grants = 0
    for k in keys:
        ak_str = str(k.get("appkey"))
        # 背书单号取矩阵合并后凭证行（快照往返同构：credentials 行 oa 已并矩阵侧，
        # grant 若仍取原始 key 单号，导出快照再规划会漂移——步6 对账门禁实证）
        merged_oa = (credentials.get(ak_str) or {}).get("oa_ticket")
        for g in k.get("grants") or []:
            r = _grant_row(str(k.get("tenant") or ""), "credential", g,
                           service_ids, proxy_ids, merged_oa or (k.get("oa_ticket") or None))
            r["credential_appkey"] = ak_str
            grants.append(r)
            cred_grants += 1

    stats = {
        "matrix_rows": len(mx), "appkey_rows": len(keys),
        "matrix_fallback_adopted": fallback_n, "expired_revoked": revoked_n,
        "matrix_grants": sum(len(r.get("grants") or []) for r in mx),
        "credential_grants": cred_grants,
        "tenants": len(tenants), "credentials": len(credentials), "grants": len(grants),
    }
    return list(tenants.values()), list(credentials.values()), grants, stats


def _is_expired(rec: dict, today: str) -> bool:
    exp = str(rec.get("expires_at") or "")[:10]
    return bool(DATE_RE.match(exp)) and exp < today


def validate_rows(tenants, credentials, grants):
    """装载前校验（对齐 0001 DDL CHECK；零写入前提下的快速失败）。"""
    for t in tenants:
        if not TENANT_ID_RE.match(t["tenant_id"]):
            raise MigrateError(f"租户 ID 非法（TENANT_ID_RE 不匹配）：{t['tenant_id']!r}")
        if t["tenant_type"] == "sandbox" and t["oa_ticket_open"]:
            raise MigrateError(f"沙箱租户 {t['tenant_id']} 不得带开通单号（DDL CHECK）")
        if t["tenant_type"] != "sandbox" and not t["oa_ticket_open"]:
            raise MigrateError(f"租户 {t['tenant_id']} 缺开通 OA 单号（登记须带单号，沙箱例外）")
        if t.get("status") not in TENANT_STATUSES:    # P0-3：status 戳白名单（DDL CHECK 同源）
            raise MigrateError(f"租户 {t['tenant_id']} status 非法"
                               f"（须为 {'/'.join(TENANT_STATUSES)}）：{t.get('status')!r}")
    for c in credentials:
        if not SECRET_HASH_RE.match(str(c.get("secret_hash") or "")):
            raise MigrateError(f"凭证 {c['appkey']} secret_hash 缺失或非 sha256 摘要格式")
        if not TENANT_ID_RE.match(c["tenant_id"]):
            raise MigrateError(f"凭证 {c['appkey']} 租户 ID 非法：{c['tenant_id']!r}")
        exp = c.get("expires_at")
        if exp not in (None, "permanent") and not DATE_RE.match(str(exp)):
            raise MigrateError(f"凭证 {c['appkey']} expires_at 非法：{exp!r}")
        if c["key_type"] == "formal" and not c.get("oa_ticket"):
            raise MigrateError(f"正式凭证 {c['appkey']} 缺 OA 单号（DDL formal 必带）")
    seen = set()
    for g in grants:
        key = (g["tenant_id"], g["scope"], g["credential_appkey"], g["target_kind"],
               g["service_id"], g["proxy_id"], g["endpoint"])
        if key in seen:
            raise MigrateError(f"重复 grant（同键已登记）：{key}")
        seen.add(key)
        if g["scope"] == "credential" and not g["credential_appkey"]:
            raise MigrateError(f"凭证级 grant 缺 credential_appkey：{g}")
        if g["target_kind"] == "service" and not g["fields"]:
            raise MigrateError(f"服务类 grant 字段白名单为空（仅代理可空）：{g['tenant_id']}×{g['service_id']}")
        for key in ("effective_from", "effective_to"):    # 1b-8 生效区间（收口批项2；date 列由 DDL 兜底）
            if g.get(key) and not DATE_RE.match(str(g[key])):
                raise MigrateError(f"grant 生效区间 {key} 非法（须 YYYY-MM-DD）：{g.get(key)!r}")
        if g.get("effective_from") and g.get("effective_to") \
                and str(g["effective_from"]) > str(g["effective_to"]):
            raise MigrateError(f"grant 生效区间颠倒（起始晚于截止）：{g['tenant_id']}×{g.get('service_id') or g.get('proxy_id')}")


# ----------------------------------------------------------------------
# SQL 生成（值字面量转义；受控本地 yaml 数据＋单引号翻倍防注入）
# ----------------------------------------------------------------------

def pg_lit(v, jsonb=False, timestamptz=False, textarr=False) -> str:
    if v is None:
        return "NULL"
    if textarr:
        return "'{" + ",".join(str(x).replace("'", "''") for x in v) + "}'"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int,)):
        return str(v)
    if isinstance(v, (dict, list)):
        return "'" + json.dumps(v, ensure_ascii=False, default=str).replace("'", "''") + "'::jsonb"
    s = str(v)
    return "'" + s.replace("'", "''") + "'"


def upsert_sql(table: str, pk: str, rows: list[dict], cols: list[str]) -> str:
    out = []
    col_updates = [f"{c} = EXCLUDED.{c}" for c in cols if c != pk]
    for r in rows:
        vals = ", ".join(pg_lit(r.get(c)) for c in cols)
        out.append(
            f"INSERT INTO meta.{table} ({', '.join(cols)}) VALUES ({vals}) "
            f"ON CONFLICT ({pk}) DO UPDATE SET {', '.join(col_updates)}, updated_at = now();")
    return "\n".join(out)


def apply_sql(rows_by_table: dict) -> str:
    """一次事务全量装载脚本（BEGIN…COMMIT；grant 全删重插；其余 upsert）。"""
    parts = ["BEGIN;",
             "SET LOCAL TimeZone TO 'UTC';",
             "SET LOCAL ROLE meta_migrate;"]
    # field_space 先行（service_catalog.field_space FK 引用）
    fs_cols = ["space_name", "drift_mode", "dict_csv_ref", "remark"]
    fs_rows = [{"space_name": n, "drift_mode": m, "dict_csv_ref": ref, "remark": None}
               for n, m, ref in FIELD_SPACE_SEEDS]
    parts.append(upsert_sql("field_space", "space_name", fs_rows, fs_cols))
    parts.append(upsert_sql("tenant", "tenant_id", rows_by_table["tenant"], [
        "tenant_id", "display_name", "tenant_type", "status", "org_ref",
        "oa_ticket_open", "remark"]))
    parts.append(upsert_sql("service_catalog", "service_id", rows_by_table["service_catalog"], [
        "service_id", "name", "description", "route", "method", "query_key", "archetype",
        "field_space", "origin", "status", "backend", "params", "fields", "response",
        "guard", "audit", "scope", "update_frequency", "owner", "published_at",
        "last_modified_by", "last_modified_at"]))
    parts.append(upsert_sql("upstream_proxy", "id", rows_by_table["upstream_proxy"], [
        "id", "service_name", "route", "method", "upstream_url", "timeout_sec",
        "retry_count", "reuse_upstream", "upstream_name", "status", "scope",
        "last_modified_by", "last_modified_at", "published_at"]))
    parts.append(upsert_sql("credential", "appkey", rows_by_table["credential"], [
        "appkey", "secret_hash", "tenant_id", "caller_name", "app_name", "key_type",
        "source", "status", "oa_ticket", "expires_at", "quota_qps", "quota_daily",
        "tag_sets", "issued_at", "rotated_at", "overlap_expires_at",
        "superseded_by", "rotation_of"]))
    # grant：全删重插（矩阵/注册表唯一权威；无被引用方；identity 主键随之重排＝迁移语义）
    g_rows = rows_by_table["grant"]
    parts.append('DELETE FROM meta."grant" WHERE true;')
    for r in g_rows:
        parts.append(
            'INSERT INTO meta."grant" (tenant_id, scope, credential_appkey, target_kind, '
            "service_id, proxy_id, fields, endpoint, oa_ticket, effective_from, effective_to) VALUES ("
            f"{pg_lit(r['tenant_id'])}, {pg_lit(r['scope'])}, {pg_lit(r['credential_appkey'])}, "
            f"{pg_lit(r['target_kind'])}, {pg_lit(r['service_id'])}, {pg_lit(r['proxy_id'])}, "
            f"{pg_lit(r['fields'], textarr=True)}, {pg_lit(r['endpoint'])}, {pg_lit(r['oa_ticket'])}, "
            f"{pg_lit(r['effective_from'])}, {pg_lit(r['effective_to'])});")
    parts.append("COMMIT;")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# 对账（条数＋字段级哈希；DB 侧 md5 聚合 vs python 侧同规范序列化）
# ----------------------------------------------------------------------

def _canon_jsonb(v) -> str:
    """复刻 jsonb::text 规范：键按 (长度, 字节序) 排序、', '/': ' 分隔。"""
    def enc(x):
        if x is None:
            return "null"
        if x is True:
            return "true"
        if x is False:
            return "false"
        if isinstance(x, (int, float)):
            return json.dumps(x)
        if isinstance(x, str):
            return json.dumps(x, ensure_ascii=False)
        if isinstance(x, list):
            return "[" + ", ".join(enc(i) for i in x) + "]"
        if isinstance(x, dict):
            keys = sorted(x.keys(), key=lambda k: (len(k), k.encode("utf-8")))
            return "{" + ", ".join(json.dumps(k, ensure_ascii=False) + ": " + enc(x[k])
                                   for k in keys) + "}"
        return json.dumps(str(x), ensure_ascii=False)
    return enc(v)


def _canon_ts(v) -> str:
    if v in (None, ""):
        return "\\N"
    s = str(v).strip()
    s = s[:-1] + "+00:00" if s.endswith("Z") else s
    dt = _dt.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canon(v, kind: str) -> str:
    if kind == "jsonb":
        return _canon_jsonb(v) if v is not None else "\\N"
    if kind == "ts":
        return _canon_ts(v)
    if kind == "bool":
        return ("true" if v else "false") if v is not None else "\\N"
    if kind == "int":
        return str(int(v)) if v is not None else "\\N"
    if kind == "textarr":
        return ",".join(str(x) for x in v) if v is not None else "\\N"
    return "\\N" if v is None else str(v)


# 每表对账列：(列名, 类型)；类型∈text/int/bool/ts/jsonb/textarr
RECON_COLUMNS = {
    "field_space": [("space_name", "text"), ("drift_mode", "text"),
                    ("dict_csv_ref", "text"), ("remark", "text")],
    "tenant": [("tenant_id", "text"), ("display_name", "text"), ("tenant_type", "text"),
               ("status", "text"), ("org_ref", "text"), ("oa_ticket_open", "text"),
               ("remark", "text")],
    "service_catalog": [("service_id", "text"), ("name", "text"), ("description", "text"),
                        ("route", "text"), ("method", "text"), ("query_key", "text"),
                        ("archetype", "text"), ("field_space", "text"), ("origin", "text"),
                        ("status", "text"), ("backend", "jsonb"), ("params", "jsonb"),
                        ("fields", "jsonb"), ("response", "jsonb"), ("guard", "jsonb"),
                        ("audit", "jsonb"), ("scope", "jsonb"), ("update_frequency", "text"),
                        ("owner", "text"), ("published_at", "ts"),
                        ("last_modified_by", "text"), ("last_modified_at", "ts")],
    "upstream_proxy": [("id", "text"), ("service_name", "text"), ("route", "text"),
                       ("method", "text"), ("upstream_url", "text"), ("timeout_sec", "int"),
                       ("retry_count", "int"), ("reuse_upstream", "bool"),
                       ("upstream_name", "text"), ("status", "text"), ("scope", "jsonb"),
                       ("last_modified_by", "text"), ("last_modified_at", "ts"),
                       ("published_at", "ts")],
    "credential": [("appkey", "text"), ("secret_hash", "text"), ("tenant_id", "text"),
                   ("caller_name", "text"), ("app_name", "text"), ("key_type", "text"),
                   ("source", "text"), ("status", "text"), ("oa_ticket", "text"),
                   ("expires_at", "text"), ("quota_qps", "int"), ("quota_daily", "int"),
                   ("tag_sets", "jsonb"), ("issued_at", "ts"), ("rotated_at", "ts"),
                   ("superseded_by", "text"), ("rotation_of", "text")],
}
RECON_PK = {"field_space": "space_name", "tenant": "tenant_id", "service_catalog": "service_id",
            "upstream_proxy": "id", "credential": "appkey"}
GRANT_RECON_COLUMNS = [("tenant_id", "text"), ("scope", "text"),
                       ("credential_appkey", "text"), ("target_kind", "text"),
                       ("service_id", "text"), ("proxy_id", "text"), ("fields", "textarr"),
                       ("endpoint", "text"), ("oa_ticket", "text"),
                       ("effective_from", "text"), ("effective_to", "text")]  # 1b-8 扩展列（项2）


def yaml_rows_for_recon():
    """四源（含 tenant/field_space）→ {table: rows}（对账口径与装载完全同函数）。"""
    svc_rows, _ = plan_service_catalog()
    tenants, creds, grants, _ = plan_tenants_creds_grants()
    return {
        "field_space": [{"space_name": n, "drift_mode": m, "dict_csv_ref": ref, "remark": None}
                        for n, m, ref in FIELD_SPACE_SEEDS],
        "tenant": tenants, "service_catalog": svc_rows,
        "upstream_proxy": plan_upstreams(), "credential": creds, "grant": grants,
    }


def _db_hash_expr(table: str, cols) -> str:
    parts = []
    for name, kind in cols:
        if kind == "ts":
            expr = (f"coalesce(to_char({name} AT TIME ZONE 'UTC', "
                    f"'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), '\\N')")
        elif kind == "textarr":
            expr = f"coalesce(array_to_string({name}, ','), '\\N')"
        elif kind == "jsonb":
            expr = f"coalesce({name}::text, '\\N')"
        elif kind == "bool":
            expr = f"coalesce({name}::text, '\\N')"
        else:
            expr = f"coalesce({name}::text, '\\N')"
        parts.append(expr)
    return f"md5(concat_ws('|', {', '.join(parts)}))"


def verify_counts_and_hashes(runner, verbose=True):
    """四源条数 vs 表内条数逐表打印＋字段级哈希对比。返回 exit code（0/2）。"""
    rows_by_table = yaml_rows_for_recon()
    rc = EXIT_OK
    for table, rows in rows_by_table.items():
        cols = RECON_COLUMNS.get(table) or GRANT_RECON_COLUMNS
        pk = RECON_PK.get(table) or "id"
        py_count = len(rows)
        db_count, db_hash = runner.one(
            f"SELECT count(*), coalesce(md5(string_agg(h, '' ORDER BY h)), 'empty') FROM "
            f"(SELECT {_db_hash_expr(table, cols)} AS h, {pk}::text AS pk FROM meta.{table}) t;")
        py_rows = []
        for r in rows:
            joined = "|".join(_canon(r.get(name), kind) for name, kind in cols)
            py_rows.append(hashlib.md5(joined.encode("utf-8")).hexdigest())
        py_hash = hashlib.md5("".join(sorted(py_rows)).encode("utf-8")).hexdigest() \
            if py_rows else "empty"
        match = (int(db_count) == py_count) and (db_hash == py_hash)
        if not match:
            rc = EXIT_MISMATCH
        if verbose:
            mark = "OK " if match else "DIFF"
            print(f"  [{mark}] {table}: yaml {py_count} 行 vs DB {db_count} 行；"
                  f"hash yaml={py_hash[:12]} db={db_hash[:12]}")
    return rc


# ----------------------------------------------------------------------
# psql 执行器（docker compose exec；测试注入同协议假件）
# ----------------------------------------------------------------------

class ComposePsqlRunner:
    """隔离栈 psql 通道：docker compose -p oneserve-meta exec -T metadata-db psql。

    禁碰 dev 容器组：命令钉死 -f compose.metadata.yml -p oneserve-meta。
    """

    def __init__(self, project: str = "oneserve-meta", timeout_s: int = 120):
        custom = os.environ.get("META_MIGRATE_PSQL_BASE", "").strip()
        if custom:
            # 测试/特殊环境注入：空格分隔的 psql 宿主命令（协议同 compose exec psql）
            self.base = custom.split()
        else:
            self.base = ["docker", "compose", "-f", "compose.metadata.yml",
                         "-p", project, "exec", "-T", "metadata-db",
                         "psql", "-U", "meta_migrate", "-d", "oneserve_meta",
                         "-v", "ON_ERROR_STOP=1", "-A", "-t", "-P", "footer=off"]
        self.timeout_s = timeout_s

    def sql(self, script: str) -> str:
        p = subprocess.run(self.base, input=script.encode("utf-8"),
                           capture_output=True, cwd=str(REPO_ROOT),
                           timeout=self.timeout_s)
        if p.returncode != 0:
            raise MigrateError(
                "psql 执行失败 rc=%d：\n%s" % (p.returncode, p.stderr.decode("utf-8", "replace")[-2000:]))
        return p.stdout.decode("utf-8", "replace")

    def one(self, query: str) -> tuple:
        out = self.sql("SET TimeZone TO 'UTC';\n" + query).strip()
        if not out:
            return ("", "")
        cells = out.splitlines()[-1].split("|")
        return (cells[0], cells[1] if len(cells) > 1 else "")


# ----------------------------------------------------------------------
# 台账（admin_event ＋ version_history；装载有变化才记）
# ----------------------------------------------------------------------

LEDGER_ENTITIES = ["field_space", "tenant", "service_catalog", "upstream_proxy", "credential"]


def ledger_sql(event_id: str, counts: dict) -> str:
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    parts = [
        "INSERT INTO meta.admin_event (event_id, operator, action, target, result, occurred_at) "
        f"VALUES ('{event_id}', 'meta_migrate', 'meta_import', 'meta', 'ok', '{ts}');"]
    for entity in LEDGER_ENTITIES:
        delta = json.dumps({"loaded": counts.get(entity, 0), "source_files": 4},
                           ensure_ascii=False, sort_keys=True)
        parts.append(
            "INSERT INTO meta.version_history (entity, entity_id, delta_after, operator, "
            f"source, event_id, occurred_at) VALUES ('{entity}', '*', "
            f"'{delta}'::jsonb, 'meta_migrate', 'migration', '{event_id}', '{ts}');")
    return "\n".join(parts)


def table_hash_snapshot(runner) -> str:
    queries = []
    for table in ["field_space", "tenant", "service_catalog", "upstream_proxy",
                  "credential", "grant"]:
        queries.append(f"SELECT coalesce(md5(string_agg(h, '' ORDER BY h)), 'empty') FROM "
                       f"(SELECT {_db_hash_expr(table, RECON_COLUMNS.get(table) or GRANT_RECON_COLUMNS)} AS h, "
                       f"{RECON_PK.get(table, 'id')}::text AS pk FROM meta.{table}) t;")
    out = runner.sql("SET TimeZone TO 'UTC';\n" + "\n".join(queries)).strip()
    return "|".join(line.strip() for line in out.splitlines() if line.strip())


# ----------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------

def cmd_apply(args) -> int:
    import yaml
    svc_rows, svc_stats = plan_service_catalog()
    tenants, creds, grants, tstats = plan_tenants_creds_grants()
    ups = plan_upstreams()
    rows_by_table = {
        "field_space": [{"space_name": n, "drift_mode": m, "dict_csv_ref": ref}
                        for n, m, ref in FIELD_SPACE_SEEDS],
        "tenant": tenants, "service_catalog": svc_rows,
        "upstream_proxy": ups, "credential": creds, "grant": grants,
    }
    print("== 装载统计（将执行） ==")
    print(f"  services.yaml     双区 runtime {svc_stats['runtime']} + console {svc_stats['console']}"
          f" → service_catalog {len(svc_rows)} 行（写区覆盖 {svc_stats['console_overrides']}，"
          f"执行字段自运行时区保全 {svc_stats['exec_fields_restored']} 条，"
          f"desc 缺失补空串 {svc_stats['desc_missing_filled_empty']}）")
    print(f"  upstreams.yaml    proxies {len(ups)} → upstream_proxy {len(ups)} 行")
    print(f"  appkeys.yaml      keys {tstats['appkey_rows']} → credential 合并 {tstats['credentials']} 行"
          f"（矩阵收编 source=matrix_fallback {tstats['matrix_fallback_adopted']}，"
          f"过期收编即 revoked {tstats['expired_revoked']}）")
    print(f"  auth_matrix.yaml  行 {tstats['matrix_rows']} → tenant 合并 {tstats['tenants']} 行；"
          f"grants 矩阵 {tstats['matrix_grants']}（租户级）＋appkeys {tstats['credential_grants']}"
          f"（凭证级）→ grant {tstats['grants']} 行")
    print(f"  field_space       七域种子 {len(FIELD_SPACE_SEEDS)} 行")
    try:
        validate_rows(tenants, creds, grants)
    except MigrateError as exc:
        print(f"装载前校验拒绝（零写入）：{exc}", file=sys.stderr)
        return EXIT_DIRTY
    if not args.dry_run:
        write_trial_template()
    runner = ComposePsqlRunner()
    if args.dry_run:
        print("== dry-run：零写入；当前表内行数 ==")
        for table in ["field_space", "tenant", "service_catalog", "upstream_proxy",
                      "credential", "grant"]:
            n, _ = runner.one(f"SELECT count(*), '' FROM meta.{table};")
            print(f"  {table}: {n}")
        print("dry-run 完成（未执行任何写语句）")
        return EXIT_OK
    script = apply_sql(rows_by_table)
    before = table_hash_snapshot(runner)
    runner.sql(script)
    after = table_hash_snapshot(runner)
    changed = before != after
    if changed:
        event_id = "mig-" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S") \
            + "-" + os.urandom(3).hex()
        runner.sql(ledger_sql(event_id, {k: len(v) for k, v in rows_by_table.items()}))
        print(f"台账：admin_event {event_id} ＋ version_history {len(LEDGER_ENTITIES)} 行（source=migration）")
    else:
        print("台账：装载前后零变化（幂等重跑），不记 admin_event/version_history")
    print("== 装载后对账 ==")
    rc = verify_counts_and_hashes(runner)
    print("apply 完成（幂等可重跑；grant 全删重插＝矩阵/注册表唯一权威收敛）"
          if rc == EXIT_OK else "对账不一致（exit 2）")
    return rc


def cmd_verify(_args) -> int:
    runner = ComposePsqlRunner()
    print("== 对账（四源 vs 表内；条数＋字段级哈希） ==")
    return verify_counts_and_hashes(runner)


def cmd_status(_args) -> int:
    runner = ComposePsqlRunner()
    print("== meta 库十表行数（只读；meta_migrate 角色） ==")
    for table in ["field_space", "tenant", "service_catalog", "upstream_proxy",
                  "credential", "grant", "admin_event", "version_history",
                  "tenant_contact"]:
        n, _ = runner.one(f"SELECT count(*), '' FROM meta.{table};")
        print(f"  {table}: {n}")
    print("  application: 表位登记未建表（卡红线）")
    out = runner.sql(
        "SET TimeZone TO 'UTC';"
        "SELECT id || ' | ' || entity || ' | ' || entity_id || ' | ' || operator"
        " || ' | ' || source || ' | ' || to_char(occurred_at AT TIME ZONE 'UTC',"
        " 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM meta.version_history"
        " ORDER BY id DESC LIMIT 5;")
    out = "\n".join(
        line for line in out.splitlines()
        if line.strip() and line.strip() != "SET").strip()
    print("== 最近 version_history（≤5 行） ==")
    print(out if out else "  （空）")
    return EXIT_OK


def write_trial_template() -> None:
    """沙箱演示集 → sync/trial_template.yaml 种子（卡待核#3 已裁默认处置）。

    幂等：已存在即跳过（不重写＝mtime 不漂移）；内容维护随管理面演进。
    """
    import yaml
    matrix = (_yaml(AUTH_MATRIX_YAML).get("matrix")) or []
    src = None
    for row in matrix:
        if row.get("tenant") == TRIAL_SOURCE_TENANT:
            for g in row.get("grants") or []:
                if g.get("service") == TRIAL_SOURCE_SERVICE:
                    src = {"service": g["service"], "endpoint": g.get("endpoint"),
                           "fields": list(g.get("fields") or [])}
    if src is None:
        raise MigrateError(f"trial template 源缺失：矩阵无 {TRIAL_SOURCE_TENANT}×{TRIAL_SOURCE_SERVICE}")
    if TRIAL_TEMPLATE_YAML.is_file():
        print(f"trial template 种子已存在，跳过重写：{TRIAL_TEMPLATE_YAML.relative_to(REPO_ROOT)}")
        return
    header = (
        "# ============================================================\n"
        "# OneServe 平台试用模板种子（卡-1b7 步4 · 卡待核#3 已裁默认处置）\n"
        f"# 来源：现状沙箱演示继承字段集（{TRIAL_SOURCE_TENANT} × {TRIAL_SOURCE_SERVICE} 已批字段）平移；\n"
        "# 内容维护随管理面演进（1b-8 授权页 / 步 6 写侧切换后归 meta 库）。\n"
        "# 消费方：沙箱签发默认试用口径（appkeys._trial_grants 的库后继来源）；本文件为平移种子，\n"
        "# 运行态读取切换前 appkeys.py 仍读矩阵，行为不变。\n"
        "# ============================================================\n")
    body = yaml.safe_dump({"trial_template": [src]}, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)
    TRIAL_TEMPLATE_YAML.write_text(header + body, encoding="utf-8")
    print(f"trial template 种子已平移：{TRIAL_TEMPLATE_YAML.relative_to(REPO_ROOT)}"
          f"（{TRIAL_SOURCE_TENANT}×{TRIAL_SOURCE_SERVICE}，{len(src['fields'])} 字段）")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="OneServe 元数据迁移工具（卡-1b7 步4）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_apply = sub.add_parser("apply", help="四 yaml → 十表装载（幂等；装载后自动对账）")
    p_apply.add_argument("--dry-run", action="store_true", help="只打印装载统计，零写入")
    p_apply.add_argument("--repeat-safe", action="store_true",
                         help="显式声明幂等重跑（与默认同一路径：upsert＋grant 全删重插）")
    p_apply.set_defaults(func=cmd_apply)
    p_verify = sub.add_parser("verify", help="只对账不装载（不一致 exit 2）")
    p_verify.set_defaults(func=cmd_verify)
    p_status = sub.add_parser("status", help="只读显示十表行数与最近 version_history")
    p_status.set_defaults(func=cmd_status)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MigrateError as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return EXIT_DIRTY


if __name__ == "__main__":
    sys.exit(main())
