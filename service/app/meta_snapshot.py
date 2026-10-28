# -*- coding: utf-8 -*-
"""元数据快照导出（卡-1b7 步6 · ADR-0021 v1.1「提交后原子导出四 yaml」）。

职责（本模块只做导出与格式还原，不做写库——写侧事务链路归 meta_writer.py db 分支）：
  1. 全量导出：meta 库五查询（service_catalog／upstream_proxy／credential 未软删／
     "grant"／tenant）→ 四 yaml 快照，tmp＋os.replace 原子写（mtime 看门口径）；
  2. 格式还原（「导出快照保持 origin 双区旧格式」口径）：
     - services.yaml  双区结构按 origin 还原：origin=runtime 行 → services: 运行时区；
       origin=console 行 → CONSOLE_MARKER 写区（_dump_console_block 与 ops_config
       产物同款排版，load_console_services 诸读径零切换的前提）；
     - upstreams.yaml / registry/appkeys.yaml / auth_matrix.yaml  按「原 yaml 头注结构」
       还原（结构说明行保留＋头注标注 DB 导出·禁手编），字段名与既有读方
       （kv_executor.load_services／proxy_hub／appkeys.resolve_grant／load_auth_matrix）
       逐一对齐；
  3. 基线导出（无库）：reconcile 门禁在 META_WRITER=yaml 基线态的「源 vs 快照」两方
     需要先有快照——planned_rows（迁移规划器产物，与 DB 行同形状）可直接喂
     write_snapshots 生成基线，不连库。

稳定排序（各表按主键）保 diff 性（卡步6）；trace/history 不在库（version_history
替代，步3 已定），快照条目不带 trace——人写注释与 trace 丢失属预期（卡步6 风险栏）。

【compose 落位（收口批项5）】
  生产落点＝独立可写卷：SNAPSHOTS_DIR 即 env META_SNAPSHOT_DIR（缺省 ./metadata-snapshots
  仓内回落，仅本机开发）；编排落位＝compose.prod.yml「元数据库快照卷」注释块
  （默认注释的可配置挂载：service 容器可写挂同卷＋apisix-init 容器只读挂同卷，
  启用三步见该块头注）；本模块零改动（落点收口在 env）。

【已知还原边界（如实登记；收口批项1/2/4 已闭三项，余一项）】
  - credential.renewed_at（yaml 留痕字段）无 DDL 列——库内不含，快照自然不带
    （变更已存 version_history.delta_after，审计不丢）【遗留，delta 留台账口径不变】；
  - ~~矩阵 1b-8 扩展登记字段~~ 已平移（收口批项2）：tenant_type 或→meta.tenant（0001 已有列）、
    grant 级 oa_ticket→meta."grant"（0001 已有列）、effective_from/to→0002 新列；
    快照按迁移规划器同构还原（三方对账恒绿）；
  - ~~纯沙箱空壳租户~~ 已落地投影载体（收口批项4）：tenant_type=sandbox 且无任何凭证的
    空壳租户以单键沙箱行（tenant＋tenant_type=sandbox[＋display_name/grants]）投影进
    auth_matrix 快照（matrix_rows_from），apply 还原不丢租户行。
"""
from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger("oneserve.meta-snapshot")

# 快照落点（卡步6 已裁口径）：env META_SNAPSHOT_DIR 优先；缺省仓内 ./metadata-snapshots
# ＝本机开发回落（.gitignore 已忽略；生产经 env 指到独立可写卷挂载点）。
ENV_SNAPSHOT_DIR = "META_SNAPSHOT_DIR"
DEFAULT_DIRNAME = "metadata-snapshots"

# 与 ops_config.CONSOLE_MARKER 同串（双区切分标记；导入而非复制＝单一事实源）
from app import ops_config as _opsc  # noqa: E402  （service/ 包内模块，测试经 sys.path 注入）

SERVICES_NAME = "services.yaml"
UPSTREAMS_NAME = "upstreams.yaml"
APPKEYS_NAME = "registry/appkeys.yaml"
MATRIX_NAME = "auth_matrix.yaml"


def snapshots_dir() -> Path:
    """快照目录（env META_SNAPSHOT_DIR；缺省仓内 ./metadata-snapshots 本机开发回落）。"""
    raw = (os.environ.get(ENV_SNAPSHOT_DIR) or "").strip()
    return Path(raw) if raw else REPO_ROOT / DEFAULT_DIRNAME


# 读侧热载回落告警去重（同名文件进程内只警一次：upstreams 数据面每请求直读，防刷屏）
_hot_warned: set = set()


def hot_read_path(path) -> Path:
    """读侧热载/启动落点（ADR-0021 v1.1 收口小批）：META_SNAPSHOT_DIR 显式配置且
    同名快照在位 → 快照优先；快照缺文件 → 回落仓内路径并 WARN 一条（去重，不炸）。

    db 模式读方读快照＝库状态镜像（模块头注口径；meta_writer._throwaway_copy/
    _planning_dir 同先例）：写侧提交后原子导出四 yaml，仓内 yaml 退化为迁移基线。
    env 未配置（yaml 模式，dev 现状）＝逐字节零改动：原路径原样返回，不触盘不告警。
    仅重定向仓内权威源路径（与 opsc/appk/kvx 模块常量相等才切；APPKEYS_YAML
    等自定义注入路径＝测试/逃生口，原样透传）。appkeys 快照带 registry/ 目录段，
    按登记名对号（basename 四名互异，basename 对号不串位）。

    【services.yaml 注册表读侧不切（2026-10-21 拔库演练实证，遗留待设计裁）】
    快照双区按 origin 还原：console 覆盖行（写区覆盖语义，当前树 7 服务占 6）落
    console_services 写区、运行时区变薄——load_services 只读运行时区，切快照＝
    数据面注册表丢 console 已发布服务（演练实证 /v1/query 404 service_not_found）。
    快照对 load_console_services 是零切换的（本模块头注明示边界），对 load_services
    不是；且 registry_render 契约「git 运行时区＝路由唯一声明式权威源」。故注册表
    读侧保持仓内 services.yaml，直至快照运行时区完备性经设计裁（快照格式改或不改
    归 meta/设计窗，本函数单点收口——届时把 _kvx.SERVICES_YAML 加回 canonical 即全切）。
    """
    p = Path(path)
    raw = (os.environ.get(ENV_SNAPSHOT_DIR) or "").strip()
    if not raw:
        return p
    from app import appkeys as _appk      # 延迟导入：防模块装载环（读方模块亦延迟引本模块）
    from app import kv_executor as _kvx
    from app import ops_config as _opsc
    canonical = {q.name: q for q in (_opsc.UPSTREAMS_YAML,
                                     _appk.APPKEYS_YAML, _kvx.AUTH_MATRIX_YAML)}
    if p != canonical.get(p.name):
        return p                          # 非重定向面（含 services 注册表，见上注）：零干预
    name = next(n for n in (SERVICES_NAME, UPSTREAMS_NAME, APPKEYS_NAME, MATRIX_NAME)
                if Path(n).name == p.name)
    alt = Path(raw) / name
    if alt.is_file():
        return alt
    if p.name not in _hot_warned:
        _hot_warned.add(p.name)
        logger.warning("META_SNAPSHOT_DIR 已配置但快照缺 %s，读侧回落仓内路径 %s"
                       "（快照未初始化？先跑迁移 apply＋基线导出）", alt, p)
    return p


def load_migrate():
    """按路径载入 scripts/meta_migrate.py（迁移规划器单一事实源；不重复造轮子）。

    模块名用别名防与 tests 直插 scripts/path 的 import 撞 sys.modules；
    SERVICE_DIR 等模块级路径常量由 planner_dir 临时改写（用毕还原）。
    路径回退（2026-10-21 前置卡）：容器内 REPO_ROOT 解析为 /（无 scripts/），
    回退 service 目录同级 scripts/（镜像 COPY／compose 挂载约定路径
    /app/scripts/meta_migrate.py）；宿主仓内路径先命中，回退不触发＝行为不变。
    """
    path = REPO_ROOT / "scripts" / "meta_migrate.py"
    if not path.is_file():
        alt = Path(__file__).resolve().parents[1] / "scripts" / "meta_migrate.py"
        if alt.is_file():
            path = alt
    spec = importlib.util.spec_from_file_location("oneserve_meta_migrate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@contextlib.contextmanager
def planner_dir(mig, svc_dir):
    """临时把迁移规划器的四源路径指到 svc_dir（快照目录/临时目录复用同规划器）。"""
    d = Path(svc_dir)
    old = (mig.SERVICE_DIR, mig.SERVICES_YAML, mig.UPSTREAMS_YAML,
           mig.APPKEYS_YAML, mig.AUTH_MATRIX_YAML)
    mig.SERVICE_DIR = d
    mig.SERVICES_YAML = d / SERVICES_NAME
    mig.UPSTREAMS_YAML = d / UPSTREAMS_NAME
    mig.APPKEYS_YAML = d / APPKEYS_NAME
    mig.AUTH_MATRIX_YAML = d / MATRIX_NAME
    try:
        yield mig
    finally:
        (mig.SERVICE_DIR, mig.SERVICES_YAML, mig.UPSTREAMS_YAML,
         mig.APPKEYS_YAML, mig.AUTH_MATRIX_YAML) = old


def planned_rows_from_dir(mig, svc_dir):
    """目录（源或快照）→ {table: rows}（规划器同函数产出，行形状＝DB 列名）。"""
    with planner_dir(mig, svc_dir):
        svc_rows, _ = mig.plan_service_catalog()
        tenants, creds, grants, _ = mig.plan_tenants_creds_grants()
        ups = mig.plan_upstreams()
    return {
        "field_space": [{"space_name": n, "drift_mode": m, "dict_csv_ref": ref, "remark": None}
                        for n, m, ref in mig.FIELD_SPACE_SEEDS],
        "tenant": tenants, "service_catalog": svc_rows,
        "upstream_proxy": ups, "credential": creds, "grant": grants,
    }


# ----------------------------------------------------------------------
# 行 → yaml 条目转换（DB 行/规划行同形状；键序＝原 yaml 条目序，缺省值省略）
# ----------------------------------------------------------------------

def service_entry_from_row(r):
    """service_catalog 行 → (origin, yaml 条目)；console 区键名 id／运行时区键名 service_id。"""
    origin = r.get("origin") or "runtime"
    e = {}
    e["service_id" if origin != "console" else "id"] = r["service_id"]
    if r.get("name") is not None:
        e["name"] = r["name"]
    if r.get("description"):
        e["desc"] = r["description"]
    e["route"] = r["route"]
    e["method"] = r.get("method") or "POST"
    if r.get("query_key") is not None:
        e["key"] = r["query_key"]
    if r.get("archetype") is not None:
        e["archetype"] = r["archetype"]
    if r.get("backend"):
        e["backend"] = r["backend"]
    e["field_space"] = r.get("field_space") or "wide297"
    if r.get("params"):
        e["params"] = r["params"]
    if r.get("fields"):
        e["fields"] = r["fields"]
    if r.get("response") is not None:
        e["response"] = r["response"]
    if r.get("guard") is not None:
        e["guard"] = r["guard"]
    if r.get("audit") is not None:
        e["audit"] = r["audit"]
    if r.get("scope") is not None:
        e["scope"] = r["scope"]
    e["status"] = r.get("status") or "offline"
    if r.get("update_frequency"):
        e["update_frequency"] = r["update_frequency"]
    if r.get("owner"):
        e["owner"] = r["owner"]
    if r.get("published_at"):
        e["published_at"] = r["published_at"]
    if r.get("last_modified_by"):
        e["last_modified_by"] = r["last_modified_by"]
    if r.get("last_modified_at"):
        e["last_modified_at"] = r["last_modified_at"]
    return origin, e


def upstream_entry_from_row(r):
    """upstream_proxy 行 → proxies[] 条目（字段序同原 yaml；trace 不在库不带）。"""
    e = {"id": r["id"]}
    if r.get("service_name") is not None:
        e["service_name"] = r["service_name"]
    e["route"] = r["route"]
    e["method"] = r.get("method") or "POST"
    e["upstream_url"] = r["upstream_url"]
    e["timeout_sec"] = int(r.get("timeout_sec") or 5)
    e["retry_count"] = int(r.get("retry_count") or 0)
    e["reuse_upstream"] = bool(r.get("reuse_upstream") or False)
    if r.get("upstream_name") is not None:
        e["upstream_name"] = r["upstream_name"]
    e["status"] = r.get("status") or "draft"
    if r.get("scope") is not None:
        e["scope"] = r["scope"]
    if r.get("last_modified_by"):
        e["last_modified_by"] = r["last_modified_by"]
    if r.get("last_modified_at"):
        e["last_modified_at"] = r["last_modified_at"]
    if r.get("published_at"):
        e["published_at"] = r["published_at"]
    return e


def key_entry_from_cred(r, cred_grants):
    """credential 行（未软删）→ keys[] 条目；grants 从 grant 表（scope=credential）回嵌。"""
    e = {"appkey": r["appkey"], "secret_hash": r["secret_hash"], "tenant": r["tenant_id"]}
    if r.get("caller_name") is not None:
        e["caller_name"] = r["caller_name"]
    if r.get("app_name") is not None:
        e["app_name"] = r["app_name"]
    e["key_type"] = r.get("key_type") or "sandbox"
    if r.get("oa_ticket"):
        e["oa_ticket"] = r["oa_ticket"]
    e["tag_sets"] = r.get("tag_sets") or []
    e["quota_qps"] = int(r.get("quota_qps") or 0)
    e["quota_daily"] = int(r.get("quota_daily") or 0)
    e["status"] = r.get("status") or "active"
    if r.get("expires_at"):
        e["expires_at"] = r["expires_at"]
    if r.get("issued_at"):
        e["issued_at"] = r["issued_at"]
    if cred_grants:
        e["grants"] = [_grant_emit(g, r.get("oa_ticket")) for g in cred_grants]   # 扩展字段随迁（项2）
    # 轮换留痕置尾（与既有登记条目同位序：rotated_at 系后补键）
    if r.get("rotated_at"):
        e["rotated_at"] = r["rotated_at"]
    if r.get("overlap_expires_at"):
        e["overlap_expires_at"] = r["overlap_expires_at"]
    if r.get("superseded_by"):
        e["superseded_by"] = r["superseded_by"]
    if r.get("rotation_of"):
        e["rotation_of"] = r["rotation_of"]
    return e


def _grant_emit(g, fallback_oa=None):
    """grant 行 → grants[] 条目（1b-8 扩展字段随迁，收口批项2/项4）。

    oa_ticket 仅在与回落值（租户级/凭证级单号）不同时输出——规划器装载时以回落值
    填充 grant.oa_ticket，省略同值键＝快照往返零漂移；生效区间无回落语义，有值即输出。
    """
    e = {"service": g["service_id"] or g["proxy_id"],
         "endpoint": g["endpoint"],
         "fields": list(g.get("fields") or [])}
    oa = g.get("oa_ticket") or None
    if oa and oa != (fallback_oa or None):
        e["oa_ticket"] = oa
    for key in ("effective_from", "effective_to"):
        if g.get(key):
            e[key] = str(g[key])[:10]
    return e


def matrix_rows_from(tenants, creds, grants):
    """tenant/credential/grant 三表 → matrix[] 行（外部登记租户；沙箱租户由 keys[] 侧还原）。

    载体规则：oa_ticket_open 非空（矩阵登记租户）→ matrix 行（携带其租户级授权；
    矩阵内嵌凭证（source=matrix_fallback）回填 appkey/secret_hash，revoked 带行 status）；
    同租户多把矩阵回落凭证（退化态）→ 追加仅 appkey 的从行（授权只随首行，防重复）；
    行级 tenant_type 戳随迁（非 external 才输出——未标＝external 规划器缺省，往返零漂移）；
    行级 tenant_status 戳随迁（管理功能群一期 P0-3：非 active 才输出——缺省 active 规划器
    读回，往返零漂移；keys[] 侧非 active 租户投影壳行承载，防 re-apply 静默复活）。
    沙箱投影（收口批项4·ADR-0021 口径）：tenant_type=sandbox（开通单号恒空，DDL CHECK）——
    纯空壳租户（无任何凭证）→ 单键沙箱行（tenant＋tenant_type=sandbox＋display_name/
    grants 有则带）＝唯一投影载体；带矩阵回落凭证的沙箱租户 → 背书行（回填
    appkey/secret_hash 不带单号，规划器同串合并复原 source=matrix_fallback 与溯源单号）；
    仅 keys[] 侧凭证且无租户级授权的 active 沙箱租户不投影（keys[] 即载体，同迁移）。
    """
    fb = {}
    for c in creds:
        if c.get("source") == "matrix_fallback":
            fb.setdefault(c["tenant_id"], []).append(c)
    tg = {}
    for g in grants:
        if g.get("scope") == "tenant":
            tg.setdefault(g["tenant_id"], []).append(g)
    cred_tids = {c["tenant_id"] for c in creds}
    rows = []
    for t in sorted(tenants, key=lambda x: x["tenant_id"]):
        stamp = t.get("tenant_type") or "external"
        tstat = str(t.get("status") or "active")
        if t.get("oa_ticket_open"):
            row = {"tenant": t["tenant_id"]}
            if t.get("display_name") and t["display_name"] != t["tenant_id"]:
                row["display_name"] = t["display_name"]
            if stamp != "external":
                row["tenant_type"] = stamp          # 1b-8 类型戳随迁（项2）
            if tstat != "active":
                row["tenant_status"] = tstat        # P0-3 生命周期戳随迁（非 active 才输出）
            glist = [_grant_emit(g, t.get("oa_ticket_open")) for g in
                     sorted(tg.get(t["tenant_id"], []),
                            key=lambda x: (x.get("service_id") or "", x.get("proxy_id") or ""))]
            fbs = sorted(fb.get(t["tenant_id"], []), key=lambda x: x["appkey"])
            if not fbs:                  # 无内嵌凭证的登记租户：单号＋授权照登
                row["oa_ticket"] = t["oa_ticket_open"]
                row["grants"] = glist
                rows.append(row)
                continue
            for i, c in enumerate(fbs):
                r2 = dict(row)
                r2["appkey"] = c["appkey"]
                r2["secret_hash"] = c["secret_hash"]
                if c.get("status") == "revoked":
                    r2["status"] = "revoked"
                if i == 0:               # 授权只随首行（规划器按行重插会翻倍的防）
                    r2["oa_ticket"] = t["oa_ticket_open"]
                    r2["grants"] = glist
                rows.append(r2)
            continue
        # ---- 无单号＝沙箱（external 缺单号过不了装载校验；DDL CHECK 沙箱免单号，
        #      行级授权溯源单号已随凭证/grant 保留，此处按沙箱口径不回单号） ----
        base = {"tenant": t["tenant_id"], "tenant_type": "sandbox"}
        if t.get("display_name") and t["display_name"] != t["tenant_id"]:
            base["display_name"] = t["display_name"]
        if tstat != "active":
            base["tenant_status"] = tstat           # P0-3 生命周期戳随迁（沙箱壳行同口径）
        glist = [_grant_emit(g, None) for g in
                 sorted(tg.get(t["tenant_id"], []),
                        key=lambda x: (x.get("service_id") or "", x.get("proxy_id") or ""))]
        fbs = sorted(fb.get(t["tenant_id"], []), key=lambda x: x["appkey"])
        if (t["tenant_id"] in cred_tids and not fbs and not glist
                and tstat == "active"):
            continue                     # 仅 keys[] 侧凭证（appkeys 源）且无租户级授权的
                                         # active 租户：keys[] 即载体（同迁移），投影反改
                                         # remark 源区归属；非 active 租户不跳过——壳行承载
                                         # tenant_status 戳（P0-3），防 re-apply 静默复活
        if not fbs:                      # 沙箱空壳投影（项4）：keys[] 侧无任何凭证载体
            if glist:                    # 租户级授权（如有）随壳行，防还原丢授权
                base["grants"] = glist
            rows.append(base)
            continue
        # 沙箱回落凭证背书行（回填 appkey/secret_hash，规划器凭同串合并复原 source；
        # 授权只随首行，防重插翻倍——同登记行逻辑，仅无单号）
        for i, c in enumerate(fbs):
            r2 = dict(base)
            r2["appkey"] = c["appkey"]
            r2["secret_hash"] = c["secret_hash"]
            if c.get("status") == "revoked":
                r2["status"] = "revoked"
            if i == 0 and glist:
                r2["grants"] = glist
            rows.append(r2)
    return rows


# ----------------------------------------------------------------------
# 头注（结构行与原 yaml 同款；标注机器生成·禁手编）
# ----------------------------------------------------------------------

SERVICES_HEADER = """\
# ============================================================
# OneServe 服务注册快照（DB 导出·禁手编）——services.yaml 双区结构按 origin 还原
# origin=runtime 行 → services: 运行时区；origin=console 行 → console_services 写区
# （写区整体重生成口径与 ops_config._save_services_yaml 同款；写区单键覆盖语义归规划器）。
# 稳定排序（service_id）保 diff；trace.history 不在库（version_history 替代）。
# ============================================================
"""

UPSTREAMS_HEADER = """\
# ============================================================
# OneServe 上游代理快照（DB 导出·禁手编）——结构同 service/upstreams.yaml：
# proxies[] -> id / service_name / route(/proxy/*) / method(GET|POST) / upstream_url
#   / timeout_sec / retry_count / reuse_upstream(bool) / upstream_name
#   / status(draft|published|offline) / scope{env,instance}
#   / last_modified_by / last_modified_at / published_at（trace 不在库不带）
# ============================================================
"""

APPKEYS_HEADER = """\
# ============================================================
# OneServe AppKey 登记快照（DB 导出·禁手编）——结构同 registry/appkeys.yaml：
# keys[] -> appkey / secret_hash(仅 sha256 摘要) / tenant / caller_name / app_name
#   / key_type(sandbox|formal) / oa_ticket / tag_sets / quota_qps / quota_daily
#   / status / expires_at / issued_at / grants[]（本凭证字段白名单）
#   / rotated_at、overlap_expires_at、superseded_by、rotation_of（轮换留痕）
# 安全纪律：secret 明文永不落盘（meta.credential.secret_hash 为 sha256 摘要，同原表）。
# ============================================================
"""

MATRIX_HEADER = """\
# ============================================================
# OneServe 授权矩阵快照（DB 导出·禁手编）——结构同 service/auth_matrix.yaml：
# matrix[] -> tenant / display_name / tenant_type(非 external 才带) / appkey / secret_hash
#   / status(revoked 才带) / oa_ticket / grants[] -> service / fields / endpoint
#   / oa_ticket(与租户级不同才带) / effective_from、effective_to(有则带，1b-8 扩展字段)
# 还原口径：oa_ticket_open 非空的租户＝矩阵登记行；沙箱租户由 keys[] 侧还原（同迁移）；
#   纯沙箱空壳租户（无任何凭证）以单键沙箱行（tenant＋tenant_type=sandbox）投影（ADR-0021）。
# ============================================================
"""


def _dump(doc):
    import yaml
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def services_yaml_text(svc_rows):
    """service_catalog 行集 → services.yaml 全文（双区按 origin 还原）。"""
    runtime = []
    console = []
    for r in sorted(svc_rows, key=lambda x: x["service_id"]):
        origin, e = service_entry_from_row(r)
        (console if origin == "console" else runtime).append(e)
    parts = [SERVICES_HEADER]
    if runtime:
        parts.append("services:\n")
        parts.append(_opsc._dump_runtime_entries(runtime))
    else:
        parts.append("services: []\n")
    parts.append(_opsc._dump_console_block(console))
    return "".join(parts)


def upstreams_yaml_text(ups_rows):
    proxies = [upstream_entry_from_row(r) for r in sorted(ups_rows, key=lambda x: x["id"])]
    return UPSTREAMS_HEADER + _dump(
        {"meta": {"updated_by": "meta-db-snapshot"}, "proxies": proxies})


def appkeys_yaml_text(cred_rows, grant_rows):
    by_appkey = {}
    for g in grant_rows:
        if g.get("scope") == "credential":
            by_appkey.setdefault(g["credential_appkey"], []).append(g)
    keys = [key_entry_from_cred(r, sorted(by_appkey.get(r["appkey"], []),
                                          key=lambda x: (x.get("service_id") or "",
                                                         x.get("proxy_id") or "")))
            for r in sorted(cred_rows, key=lambda x: x["appkey"])]
    return APPKEYS_HEADER + _dump({"meta": {"updated_by": "meta-db-snapshot"}, "keys": keys})


def auth_matrix_yaml_text(tenant_rows, cred_rows, grant_rows):
    return MATRIX_HEADER + _dump(
        {"matrix": matrix_rows_from(tenant_rows, cred_rows, grant_rows)})


# ----------------------------------------------------------------------
# 原子写出（tmp＋os.replace；mtime 看门口径）
# ----------------------------------------------------------------------

def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_snapshots(rows, out_dir=None):
    """行集（DB 查询或规划器产物）→ 四 yaml 快照。返回写出文件路径列表。"""
    out = Path(out_dir) if out_dir else snapshots_dir()
    files = {
        out / SERVICES_NAME: services_yaml_text(rows["service_catalog"]),
        out / UPSTREAMS_NAME: upstreams_yaml_text(rows["upstream_proxy"]),
        out / APPKEYS_NAME: appkeys_yaml_text(rows["credential"], rows["grant"]),
        out / MATRIX_NAME: auth_matrix_yaml_text(rows["tenant"], rows["credential"],
                                                 rows["grant"]),
    }
    for path, text in files.items():
        _atomic_write(path, text)
    return list(files)


def export_all(reader, out_dir=None):
    """从 meta 库全量导出（reader＝带 .rows(query) 的通道，如 meta_writer.MetaDbChannel）。

    credential 只导未软删（deleted_at IS NULL）——与 appkeys.yaml「删除即移出登记表」
    既有语义一致；软删行只活在库内（审计/外键链），快照面不可见。
    """
    rows = {
        "service_catalog": reader.rows("SELECT * FROM meta.service_catalog"),
        "upstream_proxy": reader.rows("SELECT * FROM meta.upstream_proxy"),
        "credential": reader.rows(
            "SELECT * FROM meta.credential WHERE deleted_at IS NULL"),
        "grant": reader.rows('SELECT * FROM meta."grant"'),
        "tenant": reader.rows("SELECT * FROM meta.tenant"),
    }
    return write_snapshots(rows, out_dir)
