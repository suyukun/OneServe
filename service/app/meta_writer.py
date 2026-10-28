# -*- coding: utf-8 -*-
"""统一写服务 meta_writer（卡-1b7 步5 抽层＋步6 db 模式；行为不变铁律下的双模入口）。

双模开关（.env：META_WRITER=yaml|db；缺省 yaml＝与抽层前逐字节同行为）：
  yaml  委托既有权威源写函数（appkeys/ops_config/grants_admin），行为零变化；
  db    事务内写 meta 库（步6 已落地）：SELECT…FOR UPDATE 行锁（写目标行或租户聚合行）
        → 写表（幂等 upsert）→ version_history 台账（delta_before/after JSON＋event_id）
        → admin_event 同发（同事务，ADR-0021 v1.1「写库事务内同发不经管线回写」）
        → COMMIT → 快照导出（service/app/meta_snapshot.py，tmp＋rename 原子写）；
        任何一步失败全回滚，另起事务记 version_history abort 行（DR runbook 口径）。

【写函数切换对照表（卡步 5 要求穷举；grep 兜底命令见 tests/test_meta_writer.py）】

  既有写函数（权威源）                          meta_writer 入口             yaml 委托                  调用点
  ---------------------------------------------------------------------------------------------  --------------------------  -------------------------  --------------------------------
  appkeys.issue_appkey＋save_appkeys            credential_issue(+save)     appkeys.issue_appkey       admin_api.issue（签发端点）
  appkeys.set_status＋save_appkeys              credential_set_status(+save) appkeys.set_status        admin_api.set_appkey_status
  appkeys.rotate_appkey＋save_appkeys           credential_rotate(+save)    appkeys.rotate_appkey      admin_api.rotate_appkey_ep
  appkeys.delete_appkey＋save_appkeys           credential_delete(+save)    appkeys.delete_appkey      admin_api.delete_appkey_ep
  appkeys.renew_appkey＋save_appkeys            credential_renew(+save)     appkeys.renew_appkey       admin_api.renew_appkey_ep
  admin_api 配额直改＋save_appkeys              credential_quota_set(+save) 内存改＋save               admin_api.set_appkey_quota
  ops_config.publish_service                    service_publish             ops_config.publish_service admin_api.publish_service_ep
  ops_config.offline_service                    service_offline             ops_config.offline_service admin_api.publish_offline
  ops_config._save_services_yaml（版本删除同步） service_yaml_save           ops_config._save_services… admin_api._sync_registry_after_delete
  ops_config.save_upstream                      upstream_save               ops_config.save_upstream   admin_api.save_upstream_ep
  ops_config.delete_upstream_draft              upstream_draft_delete       ops_config.delete_upstream… admin_api.upstream_delete_draft
  ops_config.offline_upstream                   upstream_offline            ops_config.offline_upstream admin_api.upstream_offline
  grants_admin.save_matrix（1b-8 已下沉的矩阵写径：auth_admin CLI＋授权页端点同调）  matrix_save  grants_admin.save_matrix   grants_admin.save_matrix 内部（收口批项3 已接线：db 模式切本入口，yaml 回退保留）
  tenant 实体写（1b-8 授权页/步 6）              tenant_upsert               （无独立 yaml 写径——矩阵即租户真源）  1b-8/步 6

  读侧与编排不属本层：网关 consumer 下发/回滚、审计八元组、热加载留在端点原位
  （行为逐字节一致铁律——本层只收编「纯逻辑＋权威源落盘」两拍）。

【db 模式实现口径（步6；详各 _*_db 内聚函数）】
  - 双模同内存语义：db 分支先在「隔离演练副本」上原样跑既有纯函数/校验
    （OpsConfigError/AppkeyIssueError/MatrixFormatError 原样上抛＝拒绝即零写入），
    再把结果状态差量写入 meta 库——纯逻辑不第二实现，权威语义不漂移；
  - credential 落盘收敛在 credential_save：端点流（issue/rotate/set_status/renew/
    quota_set/delete 都是「内存改 doc→网关下发→save 落盘」）在 db 模式逐字保留，
    save 以 doc↔库差量定写集（新增/变更/软删各记台账；无变化＝幂等零写）；
  - 通道：宿主机无 psql/psycopg（项目现状，meta_migrate 步4 同款先例）＝
    docker compose -p oneserve-meta exec 容器内 psql（-U meta_app，本地 trust），
    META_WRITER_PSQL_BASE env 钩子供测试注入一次性容器；「连接池」在本环境落为
    模块级缓存通道（每事务一次会话脚本，事务边界＝BEGIN…COMMIT 脚本整体成败）；
    容器内直连形态（2026-10-21 前置卡）：META_DB_HOST 非空＝组装 psql -h 直连
    （五键清单见 _psql_base_from_env；compose 注入口径详 compose.prod.yml service 块
    与 DR runbook §6），env 全空＝宿主 docker compose 通道现状不变；
  - 已知边界（收口批裁量）：credential.renewed_at 无 DDL 列（delta 留台账不丢审计）
    【遗留】；矩阵 1b-8 扩展字段（tenant_type 戳/grant 级 oa_ticket/effective_from/to）
    已平移（收口批项2：0002_meta_ext.sql＋规划器/快照同构）；矩阵登记租户 remark 为
    规划器常量（"auth_matrix 登记"），自定义 remark 会被下一次 matrix_save 归位；
    快照还原边界详 meta_snapshot.py 头注（沙箱空壳投影载体已落地＝收口批项4）。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app import appkeys as _appk
from app import ops_config as _opsc

REPO_ROOT = Path(__file__).resolve().parents[2]


def writer_mode() -> str:
    """当前写模式（META_WRITER 环境变量；缺省 yaml＝行为不变铁律）。"""
    mode = (os.environ.get("META_WRITER") or "yaml").strip().lower()
    return mode if mode in ("yaml", "db") else "yaml"


# ======================================================================
# db 模式底层：通道／事务／台账／差量（卡-1b7 步6）
# ======================================================================

PSQL_BASE_DEFAULT = ("docker compose -f compose.metadata.yml -p oneserve-meta exec -T "
                     "metadata-db psql -U meta_app -d oneserve_meta "
                     "-v ON_ERROR_STOP=1 -A -t -P footer=off")
ENV_PSQL_BASE = "META_WRITER_PSQL_BASE"     # 测试注入钩子（协议同 META_MIGRATE_PSQL_BASE）
# 容器内直连形态（2026-10-21 前置卡·对抗复核 P0-1 根治）：META_DB_HOST 非空＝服务容器
# 内直连 psql（compose service 块注入五键 META_DB_HOST/PORT/USER/PASSWORD/DATABASE；
# 宿主 docker compose 通道容器内无 docker CLI＝ENOENT）。口令不进 base 数组：
# META_DB_PASSWORD 进程级透传 PGPASSWORD（psql 原生 env），不落 SQL/日志/错误消息。
ENV_DB_HOST = "META_DB_HOST"


def _psql_base_from_env() -> list:
    """META_DB_* 五键 → 容器内直连 psql 数组（HOST 空＝未启用，返回空表回落缺省通道）。"""
    host = (os.environ.get(ENV_DB_HOST) or "").strip()
    if not host:
        return []
    port = (os.environ.get("META_DB_PORT") or "").strip() or "5432"
    user = (os.environ.get("META_DB_USER") or "").strip() or "meta_app"
    db = (os.environ.get("META_DB_DATABASE") or "").strip() or "oneserve_meta"
    return ["psql", "-h", host, "-p", port, "-U", user, "-d", db,
            "-v", "ON_ERROR_STOP=1", "-A", "-t", "-P", "footer=off"]

# 列清单与 scripts/meta_migrate.py apply_sql 同源（逆向对账测试 functional 钉住）
TENANT_COLS = ["tenant_id", "display_name", "tenant_type", "status", "org_ref",
               "oa_ticket_open", "remark"]
# 矩阵自有列（管理功能群一期 P0-3）：矩阵行只承载这四列——status/org_ref/remark
# 无矩阵载体（规划器对三者恒产出缺省值），矩阵写径 upsert 全列会「任意授权登记/
# CLI register 静默解冻 frozen 租户并抹 remark/org_ref」；矩阵写径与差量判定均按
# 本子集（_matrix_tenant_differs），生命周期状态只走 tenant_set_status 专用写径
TENANT_MATRIX_COLS = ["tenant_id", "display_name", "tenant_type", "oa_ticket_open"]


def _matrix_tenant_differs(old: dict | None, new: dict) -> bool:
    """矩阵自有列差量（P0-3）：判定口径＝TENANT_MATRIX_COLS 子集（canon 与
    _row_differs 同源）；全列差量会把 frozen/remark 租户每次授权登记都误判
    changed（upsert 护列后虽不覆写、误判仍刷 matrix_save 台账噪音）。"""
    mig = _mig()
    cols = [(n, k) for n, k in mig.RECON_COLUMNS["tenant"] if n in TENANT_MATRIX_COLS]
    if old is None:
        return True
    return any(mig._canon(old.get(n), k) != mig._canon(new.get(n), k) for n, k in cols)
SERVICE_COLS = ["service_id", "name", "description", "route", "method", "query_key",
                "archetype", "field_space", "origin", "status", "backend", "params",
                "fields", "response", "guard", "audit", "scope", "update_frequency",
                "owner", "published_at", "last_modified_by", "last_modified_at"]
UPSTREAM_COLS = ["id", "service_name", "route", "method", "upstream_url", "timeout_sec",
                 "retry_count", "reuse_upstream", "upstream_name", "status", "scope",
                 "last_modified_by", "last_modified_at", "published_at"]
CRED_COLS = ["appkey", "secret_hash", "tenant_id", "caller_name", "app_name", "key_type",
             "source", "status", "oa_ticket", "expires_at", "quota_qps", "quota_daily",
             "tag_sets", "issued_at", "rotated_at", "overlap_expires_at",
             "superseded_by", "rotation_of"]
GRANT_COLS = ["tenant_id", "scope", "credential_appkey", "target_kind", "service_id",
              "proxy_id", "fields", "endpoint", "oa_ticket",
              "effective_from", "effective_to"]   # 1b-8 扩展列（0002；收口批项2）


class MetaDbError(RuntimeError):
    """db 写径错误（人话 message；含 psql stderr 尾部）。"""


class MetaDbChannel:
    """meta 库通道（meta_app 角色）——宿主机无 psql/psycopg 的池等价物（见模块头注）。

    script()＝一次会话一个事务脚本（BEGIN…COMMIT 整体成败：任一语句失败 psql
    ON_ERROR_STOP 即非零退出，未到 COMMIT＝整体回滚）；rows()＝SELECT jsonb_agg 读行（jsonb 文本渲染恒单行）
    （TimeZone 钉 UTC，timestamptz/jsonb/text[] 原生反序列化为 Python 形状）。
    """

    def __init__(self, base=None, timeout_s=None):
        custom = (os.environ.get(ENV_PSQL_BASE) or "").strip()
        self.base = list(base) if base else (custom.split() if custom
                                             else (_psql_base_from_env()
                                                   or PSQL_BASE_DEFAULT.split()))
        self.timeout_s = int(timeout_s or os.environ.get("META_WRITER_TIMEOUT_S") or "120")
        pw = (os.environ.get("META_DB_PASSWORD") or "").strip()
        self.env = {**os.environ, "PGPASSWORD": pw} if pw else None

    def script(self, sql: str) -> str:
        p = subprocess.run(self.base, input=sql.encode("utf-8"),
                           capture_output=True, cwd=str(REPO_ROOT),
                           timeout=self.timeout_s, env=self.env)
        if p.returncode != 0:
            raise MetaDbError(
                "meta 库事务失败 rc=%d：\n%s" % (p.returncode,
                                                 p.stderr.decode("utf-8", "replace")[-1500:]))
        return p.stdout.decode("utf-8", "replace")

    def rows(self, query: str) -> list:
        """查询 → 行字典列表（query 不带分号；本方法自动包 jsonb_agg 单行输出）。"""
        out = self.script("SET TimeZone TO 'UTC';\n"
                          f"SELECT coalesce(jsonb_agg(t), '[]'::jsonb) FROM ({query}) t;").strip()
        if not out:
            return []
        import json
        last = out.splitlines()[-1]
        return json.loads(last) if last and last != "[]" else []


_channel_inst = None


def _channel() -> MetaDbChannel:
    global _channel_inst
    if _channel_inst is None:
        _channel_inst = MetaDbChannel()
    return _channel_inst


def db_reachable(timeout_s: int = 5) -> bool:
    """meta 库可达性探针（卡-1b7 步8 写冻结判定：SELECT 1 轻探，缺省 5s 超时）。

    消费方＝main.py 的 MetaDbError 全局 handler：写径抛 MetaDbError 后探一次，
    可达＝非连接类 DB 错误（约束/SQL/数据形状，500 META_DB_ERROR 人话）；
    不可达＝写冻结态（503 WRITE_FROZEN 拒绝码，DR runbook §2 影响面口径）。
    独立短超时通道实例（不复用 _channel 缓存实例），探针自身失败只返回 False，
    绝不上抛——拒绝响应不能被探针故障二次打断。
    """
    try:
        MetaDbChannel(timeout_s=timeout_s).script("SELECT 1;")
        return True
    except (MetaDbError, subprocess.TimeoutExpired, OSError):
        return False


_mig_mod = None


def _mig():
    """迁移规划器（scripts/meta_migrate.py：pg_lit/_canon/RECON_* 单一事实源）。"""
    global _mig_mod
    if _mig_mod is None:
        from app import meta_snapshot as _ms
        _mig_mod = _ms.load_migrate()
    return _mig_mod


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _new_event_id(tag: str) -> str:
    return ("mw-" + tag + "-" +
            _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S") +
            "-" + os.urandom(3).hex())


def _tx(stmts) -> None:
    """一个事务＝一条通道脚本（行锁→写表→双台账→COMMIT；失败整体回滚）。"""
    body = "BEGIN;\nSET LOCAL TimeZone TO 'UTC';\n" + \
        "\n".join(s.strip().rstrip(";") + ";" for s in stmts) + "\nCOMMIT;"
    _channel().script(body)


def _abort_ledger(entity: str, entity_id: str, action: str, operator: str,
                  detail: str) -> None:
    """失败全回滚后的补偿台账（DR runbook 口径：version_history 记 abort 行）。

    独立小事务（原事务已回滚）；二级失败只告警不抛——不掩盖原始错误。
    """
    eid = _new_event_id("abort")
    try:
        _tx([
            _ae(eid, operator or "system", action, entity_id, result="abort"),
            _vh(entity, entity_id, None, {"abort": str(detail)[:400]},
                operator or "system", "system", eid),
        ])
    except MetaDbError as exc:  # noqa: BLE001 —— 台账尽力而为
        print(f"[meta_writer] abort 台账写失败（原错误照常上抛）: {exc}", file=sys.stderr)


def _commit_and_export(stmts, *, entity: str, entity_id: str, action: str,
                       operator: str, secret_plain: str | None = None) -> None:
    """提交事务→快照导出；两段各自失败走 abort 台账（卡步5「导出失败补偿」口径）。"""
    if secret_plain:
        joined = "\n".join(stmts)
        assert secret_plain not in joined, "安全红线：secret 明文不得进入任何 SQL"
    try:
        _tx(stmts)
    except MetaDbError as exc:
        _abort_ledger(entity, entity_id, action, operator, f"事务回滚：{exc}")
        raise
    try:
        from app import meta_snapshot as _ms
        _ms.export_all(_channel())
    except Exception as exc:  # noqa: BLE001 —— 导出失败原因不掩盖
        _abort_ledger(entity, entity_id, action, operator,
                      f"提交后快照导出失败：{exc}")
        raise MetaDbError(f"快照导出失败（事务已提交，对账门禁兜底）：{exc}") from exc


def _ae(event_id: str, operator: str, action: str, target, *, oa_ticket=None,
        tenant_id=None, result: str = "ok") -> str:
    lit = _mig().pg_lit
    return ("INSERT INTO meta.admin_event (event_id, operator, action, target, result, "
            "oa_ticket, tenant_id, occurred_at) VALUES ("
            f"{lit(event_id)}, {lit(operator)}, {lit(action)}, {lit(target)}, {lit(result)}, "
            f"{lit(oa_ticket)}, {lit(tenant_id)}, {lit(_now_iso())});")


def _vh(entity: str, entity_id: str, before, after, operator: str, source: str,
        event_id: str) -> str:
    lit = _mig().pg_lit
    return ("INSERT INTO meta.version_history (entity, entity_id, delta_before, delta_after, "
            "operator, source, event_id, occurred_at) VALUES ("
            f"{lit(entity)}, {lit(entity_id)}, {lit(before, jsonb=True)}, "
            f"{lit(after, jsonb=True)}, {lit(operator)}, {lit(source)}, {lit(event_id)}, "
            f"{lit(_now_iso())});")


def _upsert_sql(table: str, pk: str, row: dict, cols) -> str:
    lit = _mig().pg_lit
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != pk)
    vals = ", ".join(lit(row.get(c)) for c in cols)
    return (f"INSERT INTO meta.{table} ({', '.join(cols)}) VALUES ({vals}) "
            f"ON CONFLICT ({pk}) DO UPDATE SET {updates}, updated_at = now();")





def _lock_in(table: str, col: str, values) -> str:
    lit = _mig().pg_lit
    vals = ", ".join(lit(v) for v in values)
    return f"SELECT {col} FROM meta.{table} WHERE {col} IN ({vals}) ORDER BY 1 FOR UPDATE;"


def _row_differs(table: str, old: dict, new: dict) -> bool:
    """行差量（对账列口径）：_canon 归一两侧（ts/jsonb/textarr 形状差全吸收）。"""
    mig = _mig()
    cols = mig.RECON_COLUMNS.get(table) or mig.GRANT_RECON_COLUMNS
    return any(mig._canon(old.get(n), k) != mig._canon(new.get(n), k) for n, k in cols)


def _grant_key(g: dict):
    """grant 身份键（对账列 canon 元组；同迁移装载语义）。"""
    mig = _mig()
    return tuple(mig._canon(g.get(n), k) for n, k in mig.GRANT_RECON_COLUMNS)


def _grant_insert_sql(g: dict) -> str:
    lit = _mig().pg_lit
    vals = ", ".join([lit(g.get("tenant_id")), lit(g.get("scope")),
                      lit(g.get("credential_appkey")), lit(g.get("target_kind")),
                      lit(g.get("service_id")), lit(g.get("proxy_id")),
                      lit(g.get("fields") or [], textarr=True), lit(g.get("endpoint")),
                      lit(g.get("oa_ticket")),
                      lit(g.get("effective_from")), lit(g.get("effective_to"))])
    return (f'INSERT INTO meta."grant" ({", ".join(GRANT_COLS)}) VALUES ({vals});')


def _grant_target_row(rec: dict, g: dict) -> dict:
    """签发侧 grants[] → credential 级 grant 行（目标判定同迁移：服务表优先）。"""
    mig = _mig()
    sid = str(g.get("service") or "")
    if sid in _svc_ids():
        kind, svc, px = "service", sid, None
    elif sid in _proxy_ids():
        kind, svc, px = "proxy", None, sid
    else:
        raise MetaDbError(f"grant 目标不可判定：{rec.get('tenant')} × {sid}"
                          "（服务表与代理表均无此 id）")
    fields = [str(f) for f in (g.get("fields") or [])]
    if kind == "service" and not fields:
        raise MetaDbError(f"服务类 grant 字段白名单为空（仅代理可空）：{rec.get('tenant')}×{sid}")
    return {"tenant_id": rec["tenant"], "scope": "credential",
            "credential_appkey": rec["appkey"], "target_kind": kind,
            "service_id": svc, "proxy_id": px, "fields": fields,
            "endpoint": g.get("endpoint"),
            # grant 级单号优先回落凭证级；生效区间随迁（1b-8 扩展字段，同迁移装载语义）
            "oa_ticket": (str(g["oa_ticket"]).strip() if g.get("oa_ticket") not in (None, "")
                          else (rec.get("oa_ticket") or None)),
            "effective_from": (str(g["effective_from"]).strip() if g.get("effective_from") else None),
            "effective_to": (str(g["effective_to"]).strip() if g.get("effective_to") else None)}


_svc_ids_cache = None
_proxy_ids_cache = None


def _svc_ids():
    global _svc_ids_cache
    if _svc_ids_cache is None:
        _svc_ids_cache = {r["service_id"] for r in
                          _channel().rows("SELECT service_id FROM meta.service_catalog")}
    return _svc_ids_cache


def _proxy_ids():
    global _proxy_ids_cache
    if _proxy_ids_cache is None:
        _proxy_ids_cache = {r["id"] for r in
                            _channel().rows("SELECT id FROM meta.upstream_proxy")}
    return _proxy_ids_cache


def _throwaway_copy(src, name: str) -> Path:
    """写纯函数的隔离演练副本：优先复制调用方传入文件；缺文件给最小合法底稿。

    快照目录优先回退（db 时代读方读快照）：传入 path 缺失时取 snapshots_dir 同名件。
    """
    from app import meta_snapshot as _ms
    src = Path(src) if src is not None else None
    if src is None or not src.is_file():
        alt = _ms.snapshots_dir() / name
        src = alt if alt.is_file() else None
    tmpdir = Path(tempfile.mkdtemp(prefix="meta-writer-"))
    dst = tmpdir / name
    if src is not None:
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    elif name == "services.yaml":
        dst.write_text("services: []\n", encoding="utf-8")
    elif name == "upstreams.yaml":
        dst.write_text("proxies: []\n", encoding="utf-8")   # 空表合法态（读方语义兼容）
    return dst                        # upstreams/auth_matrix 缺文件＝空表（读方语义兼容）


def _planning_dir(path) -> Path:
    """矩阵规划四源目录：快照目录优先（读方读快照＝库状态镜像，改授权外的部分零差量），
    回落传入 auth_matrix.yaml 所在目录（快照未初始化的开发/演练态）。

    收口批项3 纠偏：原实现传入目录优先——db 模式下端点传仓内路径时规划面会落到
    陈旧仓内源（≠库态），差量把源漂移误当写集；快照才是库镜像（模块头注同口径）。
    """
    from app import meta_snapshot as _ms
    d = _ms.snapshots_dir()
    if (d / "services.yaml").is_file() and (d / _ms.APPKEYS_NAME).is_file():
        return d
    d = Path(path).parent
    if (d / "services.yaml").is_file() and (d / _ms.APPKEYS_NAME).is_file():
        return d
    raise MetaDbError("db 模式矩阵写径需要四源目录（快照未初始化？先跑迁移 apply＋基线导出）")


# ---- credential：注册表 doc ↔ 库差量（issue/rotate/set_status/renew/quota/delete 收敛） ----

def _cred_row_from_rec(rec: dict) -> dict:
    """appkeys 登记条目 → credential 行（source=appkeys；renewed_at 无列随 delta 留台账）。"""
    return {"appkey": str(rec.get("appkey")), "secret_hash": rec.get("secret_hash"),
            "tenant_id": str(rec.get("tenant") or ""),
            "caller_name": rec.get("caller_name"), "app_name": rec.get("app_name"),
            "key_type": rec.get("key_type") or "sandbox", "source": "appkeys",
            "status": rec.get("status") or "active",
            "oa_ticket": rec.get("oa_ticket") or None,
            "expires_at": rec.get("expires_at") or None,
            "quota_qps": int(rec.get("quota_qps") or 0),
            "quota_daily": int(rec.get("quota_daily") or 0),
            "tag_sets": rec.get("tag_sets") or [],
            "issued_at": rec.get("issued_at") or None,
            "rotated_at": rec.get("rotated_at") or None,
            "overlap_expires_at": rec.get("overlap_expires_at") or None,
            "superseded_by": rec.get("superseded_by"),
            "rotation_of": rec.get("rotation_of")}


def _cred_action(old: dict | None, new: dict) -> str:
    if old is None:
        return "credential_rotate" if new.get("rotation_of") else "credential_issue"
    if new.get("superseded_by") and not old.get("superseded_by"):
        return "credential_rotate"
    mig = _mig()
    if mig._canon(new.get("status"), "text") != mig._canon(old.get("status"), "text"):
        return "credential_set_status"
    if mig._canon(new.get("expires_at"), "text") != mig._canon(old.get("expires_at"), "text"):
        return "credential_renew"
    if (mig._canon(new.get("quota_qps"), "int") != mig._canon(old.get("quota_qps"), "int")
            or mig._canon(new.get("quota_daily"), "int")
            != mig._canon(old.get("quota_daily"), "int")):
        return "credential_quota_set"
    return "credential_update"


def _credential_save_db(path, doc: dict, *, operator: str = "console",
                        secret_plain: str | None = None) -> bool:
    """注册表 doc ↔ credential 表差量落盘（端点流终点；返回是否有写入）。"""
    lit = _mig().pg_lit
    ch = _channel()
    keys = doc.get("keys") or []
    planned = {}
    for k in keys:
        ak = str(k.get("appkey") or "")
        if not ak:
            raise MetaDbError("注册表出现无 appkey 条目（doc 形状坏）")
        if ak in planned:
            raise MetaDbError(f"注册表内重复 appkey：{ak[:8]}…（拒绝静默合并）")
        planned[ak] = k
    cur = {r["appkey"]: r for r in
           ch.rows("SELECT * FROM meta.credential WHERE deleted_at IS NULL")}
    cur_grants: dict = {}
    for g in ch.rows("SELECT * FROM meta.\"grant\" WHERE scope = 'credential'"):
        cur_grants.setdefault(g["credential_appkey"], []).append(g)
    changes = []                        # (appkey, action, old_row, new_row|None, rec|None)
    for ak in sorted(set(planned) | set(cur)):
        rec, old = planned.get(ak), cur.get(ak)
        new = _cred_row_from_rec(rec) if rec is not None else None
        if new is not None and old is not None \
                and old.get("source") == "matrix_fallback":
            # 注册表 doc 是 appkeys 侧视图，翻不动矩阵回落语义：source=matrix_fallback
            # 的背书载体在矩阵登记行（收口批项3 e2e 实证——翻源后快照矩阵行丢 appkey
            # 回填，1b-8 校验器拒绝该底稿）。source 只随 matrix_save 收敛（登记行移除
            # 时由其规划面回 appkeys），credential_save 恒保留
            new["source"] = "matrix_fallback"
        if new is None:                 # doc 缺失＝受限删（软删；禁物理删）
            if old is not None:
                changes.append((ak, "credential_delete", old, None, None))
            continue
        if old is not None and not _row_differs("credential", old, new):
            # 行同但授权差（grants 不在 credential 列内）也要写
            old_gk = {_grant_key(g) for g in cur_grants.get(ak, [])}
            new_gk = {_grant_key(_grant_target_row(rec, g)) for g in rec.get("grants") or []}
            if old_gk == new_gk:
                continue
            changes.append((ak, "credential_grant_sync", old, new, rec))
            continue
        changes.append((ak, _cred_action(old, new), old, new, rec))
    if not changes:
        return False
    tenants = sorted({(c[3] or c[2])["tenant_id"] for c in changes})
    aks = [c[0] for c in changes]
    stmts = [_lock_in("tenant", "tenant_id", tenants),        # 租户聚合行锁（每租户 2 把不变量）
             _lock_in("credential", "appkey", aks)]           # 写目标行锁
    for tid in tenants:   # P0-6 冻结闸（事务内核，与 tenant_set_status 同把行锁）：
        stmts.append(_tenant_gate_stmt(tid, "active", "tenant_frozen_gate"))
        # 非 active 租户凭证写径整事务拒绝——签发族即使并行抢到先手，冻结事务也会
        # 等其提交后再读清单摘除，两头不漏（与 appkeys 层快照戳门双保险）
    now = _now_iso()
    for ak, action, old, new, rec in changes:
        eid = _new_event_id("cred")
        oa = (new or old or {}).get("oa_ticket")
        tid = (new or old)["tenant_id"]
        stmts.append(_ae(eid, operator, action, ak, oa_ticket=oa, tenant_id=tid))
        if new is None:               # 软删：deleted_at 落列＋授权随登记表移除
            gone = dict(old)
            gone["deleted_at"] = now
            stmts.append(_vh("credential", ak, old, gone, operator, "console", eid))
            stmts.append(f"UPDATE meta.credential SET deleted_at = {lit(now)}, "
                         f"updated_at = {lit(now)} WHERE appkey = {lit(ak)};")
            old_g = cur_grants.get(ak, [])
            if old_g:
                stmts.append(f"DELETE FROM meta.\"grant\" WHERE scope = 'credential' "
                             f"AND credential_appkey = {lit(ak)};")
                stmts.append(_vh("grant", ak, {"grants": old_g}, {"grants": []},
                                 operator, "console", eid))
            continue
        stmts.append(_vh("credential", ak, old, new, operator, "console", eid))
        stmts.append(_upsert_sql("credential", "appkey", new, CRED_COLS))
        new_g = [_grant_target_row(rec, g) for g in (rec.get("grants") or [])]
        old_g = cur_grants.get(ak, [])
        if {_grant_key(g) for g in old_g} != {_grant_key(g) for g in new_g}:
            stmts.append(f"DELETE FROM meta.\"grant\" WHERE scope = 'credential' "
                         f"AND credential_appkey = {lit(ak)};")
            for g in new_g:
                stmts.append(_grant_insert_sql(g))
            stmts.append(_vh("grant", ak, {"grants": old_g}, {"grants": new_g},
                             operator, "console", eid))
    _commit_and_export(stmts, entity="credential", entity_id=aks[0],
                       action="credential_save", operator=operator,
                       secret_plain=secret_plain)
    return True


# ---- service/upstream：副本演练＋状态差量（publish/offline/yaml_save/upstream 三动作） ----

def _service_write_db(path, run, *, action: str, operator: str,
                      sync_runtime: bool = True):
    """services.yaml 写纯函数在副本上原样发生，再以规划行差量写库（卡步6 双区语义）。"""
    mig = _mig()
    ch = _channel()
    base = _throwaway_copy(path, "services.yaml")
    before = {r["service_id"]: r for r in mig.plan_service_catalog(base)[0]}
    run(base)                         # OpsConfigError 原样上抛＝拒绝即零写入
    after = mig.plan_service_catalog(base)[0]
    planned = {r["service_id"]: r for r in after}
    # runtime_entries=None（版本删除同步流）＝运行时区不在本动作管辖（镜像 yaml 分支语义）
    zones = {"console", "runtime"} if sync_runtime else {"console"}
    diffs = []
    for sid in sorted(set(before) | set(planned)):
        old, new = before.get(sid), planned.get(sid)
        zone = (new or old).get("origin") or "runtime"
        if zone not in zones:
            continue
        if old is not None and new is not None and not _row_differs("service_catalog", old, new):
            continue
        diffs.append((sid, old, new))
    # 删除集＝库内该区 origin 行中不在本次规划面的（版本删除同步）
    cur = {r["service_id"]: r for r in ch.rows("SELECT * FROM meta.service_catalog")}
    planned_ids = {sid for sid, r in planned.items()
                   if ((r.get("origin") or "runtime")) in zones}
    gone = sorted(sid for sid, r in cur.items()
                  if (r.get("origin") or "runtime") in zones and sid not in planned_ids)
    if not diffs and not gone:
        return False
    touched = [d[0] for d in diffs] + gone
    stmts = [_lock_in("service_catalog", "service_id", touched)]
    for sid, old, new in diffs:
        eid = _new_event_id("svc")
        stmts.append(_ae(eid, operator, action, sid, tenant_id=None))
        stmts.append(_vh("service_catalog", sid, old, new, operator, "console", eid))
        if new is not None:
            stmts.append(_upsert_sql("service_catalog", "service_id", new, SERVICE_COLS))
        else:
            stmts.append(f"DELETE FROM meta.service_catalog WHERE service_id = "
                         f"{mig.pg_lit(sid)};")
    for sid in gone:
        eid = _new_event_id("svc")
        stmts.append(_ae(eid, operator, action, sid, tenant_id=None))
        stmts.append(_vh("service_catalog", sid, cur[sid], None, operator, "console", eid))
        stmts.append(f"DELETE FROM meta.service_catalog WHERE service_id = "
                     f"{mig.pg_lit(sid)};")
    _commit_and_export(stmts, entity="service_catalog", entity_id=touched[0],
                       action=action, operator=operator)
    return True


def _upstream_write_db(path, run, *, action: str, operator: str):
    """upstreams.yaml 写纯函数在副本上原样发生，再以规划行差量写库。"""
    mig = _mig()
    ch = _channel()
    base = _throwaway_copy(path, "upstreams.yaml")
    before = {r["id"]: r for r in mig.plan_upstreams(base)}
    run(base)
    after = {r["id"]: r for r in mig.plan_upstreams(base)}
    diffs = []
    for pid in sorted(set(before) | set(after)):
        old, new = before.get(pid), after.get(pid)
        if old is not None and new is not None and not _row_differs("upstream_proxy", old, new):
            continue
        diffs.append((pid, old, new))
    if not diffs:
        return False
    stmts = [_lock_in("upstream_proxy", "id", [d[0] for d in diffs])]
    for pid, old, new in diffs:
        eid = _new_event_id("ups")
        stmts.append(_ae(eid, operator, action, pid, tenant_id=None))
        stmts.append(_vh("upstream_proxy", pid, old, new, operator, "console", eid))
        if new is not None:
            stmts.append(_upsert_sql("upstream_proxy", "id", new, UPSTREAM_COLS))
        else:
            stmts.append(f"DELETE FROM meta.upstream_proxy WHERE id = {mig.pg_lit(pid)};")
    _commit_and_export(stmts, entity="upstream_proxy", entity_id=diffs[0][0],
                       action=action, operator=operator)
    return True


# ---- 矩阵/租户 ----

def _matrix_save_db(path, data: dict, *, operator: str = "auth_admin",
                    secret_plain: str | None = None) -> bool:
    """矩阵写径（grants_admin.save_matrix 校验原样发生）→ 租户/凭证 upsert＋grant 全删重插。

    规划面＝快照四源×新矩阵（读方读快照＝库状态镜像，改授权外的部分零差量）；
    grant 沿迁移语义全删重插（矩阵/注册表唯一权威收敛）。
    """
    from app import meta_snapshot as _ms
    from app import grants_admin as _gam
    mig = _mig()
    ch = _channel()
    plan_dir = _planning_dir(path)
    tmp = _throwaway_copy(path, "auth_matrix.yaml")
    # 演练副本走 yaml 写函数本体（不经 save_matrix 模式开关——防 db 模式递归回本入口，
    # 收口批项3 接线后 save_matrix 会切 mw.matrix_save）；双层校验原样发生口径不变
    _gam._save_matrix_yaml(tmp, data, secret_plain=secret_plain)
    with _ms.planner_dir(mig, plan_dir):
        tenants, creds, grants, _ = mig.plan_tenants_creds_grants(
            appkeys_path=plan_dir / _ms.APPKEYS_NAME, matrix_path=tmp)
    cur_t = {r["tenant_id"]: r for r in ch.rows("SELECT * FROM meta.tenant")}
    cur_c = {r["appkey"]: r for r in
             ch.rows("SELECT * FROM meta.credential WHERE deleted_at IS NULL")}
    t_changed = [t for t in tenants if t["tenant_id"] not in cur_t
                 or _matrix_tenant_differs(cur_t[t["tenant_id"]], t)]   # P0-3 矩阵自有列差量
    c_changed = [c for c in creds if c["appkey"] not in cur_c
                 or _row_differs("credential", cur_c[c["appkey"]], c)]
    cur_g = ch.rows('SELECT * FROM meta.\"grant\"')
    cur_keys = {_grant_key(g) for g in cur_g}
    new_keys = {_grant_key(g) for g in grants}
    if not t_changed and not c_changed and cur_keys == new_keys:
        return False
    tids = sorted({t["tenant_id"] for t in tenants})
    stmts = [_lock_in("tenant", "tenant_id", tids)] if tids else []
    for t in t_changed:
        eid = _new_event_id("mtx")
        stmts.append(_ae(eid, operator, "matrix_save", t["tenant_id"],
                         oa_ticket=t.get("oa_ticket_open"), tenant_id=t["tenant_id"]))
        stmts.append(_vh("tenant", t["tenant_id"], cur_t.get(t["tenant_id"]),
                         {**t, "status": (cur_t.get(t["tenant_id"]) or {}).get("status"),
                          "org_ref": (cur_t.get(t["tenant_id"]) or {}).get("org_ref"),
                          "remark": (cur_t.get(t["tenant_id"]) or {}).get("remark")},
                         operator, "auth_admin", eid))
        stmts.append(_upsert_sql("tenant", "tenant_id", t, TENANT_MATRIX_COLS))   # P0-3 护列
    for c in c_changed:
        eid = _new_event_id("mtx")
        stmts.append(_ae(eid, operator, "matrix_save", c["appkey"],
                         oa_ticket=c.get("oa_ticket"), tenant_id=c["tenant_id"]))
        stmts.append(_vh("credential", c["appkey"], cur_c.get(c["appkey"]), c,
                         operator, "auth_admin", eid))
        stmts.append(_upsert_sql("credential", "appkey", c, CRED_COLS))
    if cur_keys != new_keys:
        eid = _new_event_id("mtx")
        stmts.append(_ae(eid, operator, "matrix_save", "grant"))
        stmts.append(_vh("grant", "*", {"count": len(cur_g)}, {"count": len(grants)},
                         operator, "auth_admin", eid))
        stmts.append('DELETE FROM meta.\"grant\" WHERE true;')
        for g in grants:
            stmts.append(_grant_insert_sql(g))
    _commit_and_export(stmts, entity="grant", entity_id="*",
                       action="matrix_save", operator=operator,
                       secret_plain=secret_plain)
    return True


def _tenant_upsert_db(*, tenant_id: str, display_name: str, tenant_type: str = "external",
                      status: str = "active", org_ref=None, oa_ticket_open=None,
                      remark=None, operator: str = "console", oa_ticket=None) -> dict:
    """租户实体写（行锁→upsert→双台账→提交→导出；校验对齐 0001 DDL CHECK）。"""
    mig = _mig()
    ch = _channel()
    if not mig.TENANT_ID_RE.match(tenant_id or ""):
        raise MetaDbError(f"租户 ID 非法（TENANT_ID_RE 不匹配）：{tenant_id!r}")
    if tenant_type == "sandbox" and oa_ticket_open:
        raise MetaDbError(f"沙箱租户 {tenant_id} 不得带开通单号（DDL CHECK）")
    if tenant_type != "sandbox" and not oa_ticket_open:
        raise MetaDbError(f"租户 {tenant_id} 缺开通 OA 单号（登记须带单号，沙箱例外）")
    prev = ch.rows(f"SELECT * FROM meta.tenant WHERE tenant_id = {mig.pg_lit(tenant_id)}")
    new = {"tenant_id": tenant_id, "display_name": display_name,
           "tenant_type": tenant_type, "status": status, "org_ref": org_ref,
           "oa_ticket_open": oa_ticket_open, "remark": remark}
    old = prev[0] if prev else None
    if old is not None and not _row_differs("tenant", old, new):
        return new                    # 幂等：零变化零写零台账
    eid = _new_event_id("tenant")
    stmts = [
        f"SELECT tenant_id FROM meta.tenant WHERE tenant_id = {mig.pg_lit(tenant_id)} "
        "FOR UPDATE;",                # 写目标行锁（新建行锁空集＝PK 唯一兜底）
        _upsert_sql("tenant", "tenant_id", new, TENANT_COLS),
        _ae(eid, operator, "tenant_upsert", tenant_id,
            oa_ticket=(oa_ticket or oa_ticket_open), tenant_id=tenant_id),
        _vh("tenant", tenant_id, old, new, operator, "console", eid),
    ]
    _commit_and_export(stmts, entity="tenant", entity_id=tenant_id,
                       action="tenant_upsert", operator=operator)
    return new


# ---- 租户生命周期状态写（管理功能群一期 P1-①②／P0-6；冻结解冻共用入口） ----

TENANT_FREEZE_ACTION = "tenant_freeze"
TENANT_UNFREEZE_ACTION = "tenant_unfreeze"
_TENANT_STATUS_ACTIONS = {TENANT_FREEZE_ACTION: ("active", "frozen"),
                          TENANT_UNFREEZE_ACTION: ("frozen", "active")}


def _tenant_gate_stmt(tid: str, expect_status: str, gate: str) -> str:
    """事务内租户状态守卫（P0-6 完整修法）：DO 块行锁下复核状态，不符即 RAISE——
    ON_ERROR_STOP 整事务回滚，杜绝「校验靠事务外预读、锁后状态已变」的赛跑窗。
    gate＝机读哨兵前缀（tenant_status_guard/tenant_frozen_gate/tenant_missing），
    端点层经 tenant_gate_code() 识别映射 4xx。"""
    lit = _mig().pg_lit
    return ("DO $tenant_gate$ DECLARE cur text; BEGIN "
            "SELECT status INTO cur FROM meta.tenant WHERE tenant_id = " + lit(tid) + " FOR UPDATE; "
            "IF cur IS NULL THEN RAISE EXCEPTION 'tenant_missing:" + tid + "'; END IF; "
            "IF cur <> " + lit(expect_status) + " THEN RAISE EXCEPTION '" + gate + ":%', cur; "
            "END IF; END $tenant_gate$;")


def tenant_gate_code(exc: BaseException) -> str | None:
    """MetaDbError stderr 里的租户守卫哨兵 → 机读错误码（无哨兵＝None 原样上抛）。"""
    text = str(exc or "")
    if "tenant_frozen_gate" in text or "tenant_status_guard" in text:
        return "tenant_frozen"
    if "tenant_missing:" in text:
        return "tenant_not_found"
    return None


def tenant_set_status(*, tenant_id: str, status: str, action: str, operator: str,
                      oa_ticket=None) -> dict:
    """租户生命周期状态写（管理功能群一期 P1-①②：动作名参数化——不复用
    tenant_upsert 硬编码动作名；version_history 状态轨同事务）。

    P0-6 完整修法：单事务内 tenant 行 FOR UPDATE（与签发族 _credential_save_db
    的租户聚合行锁同锁竞争）＋DO 块状态守卫（_tenant_gate_stmt，不符即 RAISE 回滚）
    ＋**同锁下读名下 active 凭证清单**（返回值 active_appkeys——冻结读清单与并发
    签发按锁序串行：先签发者其 key 必入清单、后签发者撞冻结守卫被拒，两头不漏）。
    状态机在写层钉死：tenant_freeze 须 active→frozen、tenant_unfreeze 须
    frozen→active；同态幂等＝零写零台账原样返回；目标行不存在 MetaDbError（端点层
    经 tenant_gate_code 映射 404）。oa_ticket＝解冻工单号（纯登记留痕，域模型 §4）。
    """
    mig = _mig()
    ch = _channel()
    if action not in _TENANT_STATUS_ACTIONS:
        raise MetaDbError(f"非法状态动作（须为 {'/'.join(_TENANT_STATUS_ACTIONS)}）：{action!r}")
    if status not in ("active", "frozen", "terminating", "cancelled"):
        raise MetaDbError(f"非法租户状态（0001 DDL 四态）：{status!r}")
    expect_from, _to = _TENANT_STATUS_ACTIONS[action]
    prev = ch.rows(f"SELECT * FROM meta.tenant WHERE tenant_id = {mig.pg_lit(tenant_id)}")
    if not prev:
        raise MetaDbError(f"tenant_missing:{tenant_id}（冻结/解冻只作用于已登记实体）")
    old = prev[0]
    cur = str(old.get("status") or "active")
    if cur == status:
        return old                    # 幂等：同态零写零台账（事务外快路径，零锁）
    new = {**old, "status": status}
    eid = _new_event_id("tenant")
    lit = mig.pg_lit
    # 单事务脚本：守卫（锁＋状态复核）→ 状态写 → 同锁下凭证清单 → 双台账 → COMMIT
    script = "\n".join([
        "BEGIN;",
        _tenant_gate_stmt(tenant_id, expect_from, "tenant_status_guard"),
        f"UPDATE meta.tenant SET status = {lit(status)}, updated_at = now() "
        f"WHERE tenant_id = {lit(tenant_id)};",
        "SELECT coalesce(jsonb_agg(appkey), '[]'::jsonb) FROM meta.credential "
        f"WHERE tenant_id = {lit(tenant_id)} AND deleted_at IS NULL AND status = 'active';",
        _ae(eid, operator, action, tenant_id, oa_ticket=oa_ticket, tenant_id=tenant_id),
        _vh("tenant", tenant_id, old, new, operator, "admin", eid),
        "COMMIT;",
    ])
    try:
        out = ch.script(script)
    except MetaDbError as exc:
        _abort_ledger("tenant", tenant_id, action, operator, f"事务回滚：{exc}")
        raise
    active_keys: list = []
    for line in out.splitlines():     # 凭证清单＝脚本输出中的 jsonb 数组行（-A -t 单行）
        line = line.strip()
        if line.startswith("["):
            active_keys = json.loads(line)
            break
    try:
        from app import meta_snapshot as _ms
        _ms.export_all(_channel())
    except Exception as exc:  # noqa: BLE001 —— 导出失败原因不掩盖
        _abort_ledger("tenant", tenant_id, action, operator,
                      f"提交后快照导出失败：{exc}")
        raise MetaDbError(f"快照导出失败（事务已提交，对账门禁兜底）：{exc}") from exc
    return {"row": new, "active_appkeys": active_keys}


def tenant_exists(tenant_id: str) -> bool:
    """租户实体存在性（db 态新建查重——双不复用：新建拒绝改写既有行，tenant_upsert
    的 upsert 语义只归显式登记面）。写前校验读，非读侧消费面（ADR-0021 口径不变）。"""
    mig = _mig()
    rows = _channel().rows(
        f"SELECT tenant_id FROM meta.tenant WHERE tenant_id = {mig.pg_lit(tenant_id)}")
    return bool(rows)


def admin_event_log(*, action: str, operator: str, target, result: str = "denied",
                    tenant_id=None, oa_ticket=None) -> bool:
    """admin_event 窄表补记（§0 审计总口径分模承载）：db 态被拒提交 result=denied、
    网关摘挂不完整 result=partial（状态写本体的事件在事务内已落 ok 行）——yaml 态
    不落库＝管线八元组事件＋文件 git 留痕。单语句自动提交；尽力而为（失败只返回
    False，不掩盖调用方正在返回的原拒绝错误——沿 _audit_admin_action「审计
    失败不阻断业务」同款纪律）。"""
    try:
        mig = _mig()
        eid = _new_event_id("admin")
        _channel().script(
            "INSERT INTO meta.admin_event (event_id, operator, action, target, result, "
            "oa_ticket, tenant_id, occurred_at) VALUES ("
            f"{mig.pg_lit(eid)}, {mig.pg_lit(operator)}, {mig.pg_lit(action)}, "
            f"{mig.pg_lit(target)}, {mig.pg_lit(result)}, {mig.pg_lit(oa_ticket)}, "
            f"{mig.pg_lit(tenant_id)}, {mig.pg_lit(_now_iso())});")
        return True
    except Exception:  # noqa: BLE001 —— 审计尽力而为（原因不掩盖原错误）
        return False


# ======================================================================
# credential 实体（appkeys.yaml 权威源；yaml 模式＝appkeys 纯函数转发）
# ======================================================================

def credential_issue(doc: dict, matrix: list, services: dict, *, tenant: str,
                     app_name: str, key_type: str, caller_name: str = "",
                     oa_ticket: str = "", tag_sets=None,
                     quota_qps: int = 50, quota_daily: int = 10000,
                     expires_at: str = "", grants=None,
                     now=None) -> tuple:
    """签发纯逻辑（校验→定授权→算 TTL→生成 key/secret）；落盘走 credential_save。

    双模同内存语义（doc 原地追加新条目）：db 模式落盘同样收敛在 credential_save
    （doc↔库差量），端点流「签发→网关下发→save」失败即零写入，与 yaml 模式逐拍对齐。
    """
    return _appk.issue_appkey(doc, matrix, services, tenant=tenant, app_name=app_name,
                              key_type=key_type, caller_name=caller_name,
                              oa_ticket=oa_ticket, tag_sets=tag_sets,
                              quota_qps=quota_qps, quota_daily=quota_daily,
                              expires_at=expires_at, grants=grants, now=now)


def credential_set_status(doc: dict, appkey: str, action: str, *, now=None) -> dict:
    return _appk.set_status(doc, appkey, action, now=now)


def credential_rotate(doc: dict, matrix: list, services: dict, appkey: str, *,
                      now=None, quota_qps=None, quota_daily=None) -> tuple:
    return _appk.rotate_appkey(doc, matrix, services, appkey, now=now,
                               quota_qps=quota_qps, quota_daily=quota_daily)


def credential_delete(doc: dict, appkey: str, *, used_count: int = 0) -> dict:
    return _appk.delete_appkey(doc, appkey, used_count=used_count)


def credential_renew(doc: dict, appkey: str, expires_at: str, *, now=None) -> dict:
    return _appk.renew_appkey(doc, appkey, expires_at, now=now)


def credential_quota_set(doc: dict, appkey: str, quota_qps: int, quota_daily: int) -> dict:
    """配额登记（P1-5：先落权威源再下发网关的顺序由端点保持）。"""
    rec = _appk.find_record(doc["keys"], appkey)
    rec["quota_qps"], rec["quota_daily"] = int(quota_qps), int(quota_daily)
    return rec


def credential_save(path, doc: dict, *, operator: str = "console",
                    secret_plain=None) -> None:
    """注册表落盘：yaml＝原子写文件（写前 secret 断言）；db＝doc↔库差量事务＋快照导出。"""
    if writer_mode() == "yaml":
        _appk.save_appkeys(Path(path), doc, secret_plain=secret_plain)
        return
    _credential_save_db(path, doc, operator=operator, secret_plain=secret_plain)


# ======================================================================
# service 定义实体（services.yaml 双区权威源；yaml 模式＝ops_config 转发）
# ======================================================================

def service_publish(path, **kw) -> tuple:
    if writer_mode() == "yaml":
        return _opsc.publish_service(Path(path), **kw)
    operator = str(kw.get("operator") or "console")
    box = {}

    def _run(p):
        box["ret"] = _opsc.publish_service(p, **kw)

    _service_write_db(Path(path), _run, action="service_publish", operator=operator,
                      sync_runtime=True)
    return box.get("ret") or (None, None)   # 视图＝副本演练产物（端点 response 形状不变）


def service_offline(path, sid: str, *, operator: str = "console", now=None) -> tuple:
    if writer_mode() == "yaml":
        return _opsc.offline_service(Path(path), sid, operator=operator, now=now)
    box = {}

    def _run(p):
        box["ret"] = _opsc.offline_service(p, sid, operator=operator, now=now)

    _service_write_db(Path(path), _run, action="service_offline", operator=operator,
                      sync_runtime=False)
    return box.get("ret") or (None, None)


def service_yaml_save(path, head: str, entries: list,
                      runtime_entries: list | None = None) -> None:
    """services.yaml 原子落盘（yaml：运行时区逐字节保留；db：写区差量＋按参同步运行时区）。"""
    if writer_mode() == "yaml":
        _opsc._save_services_yaml(Path(path), head, entries, runtime_entries)
        return
    _service_write_db(Path(path),
                      lambda p: _opsc._save_services_yaml(p, head, entries, runtime_entries),
                      action="service_yaml_save", operator="console",
                      sync_runtime=runtime_entries is not None)


# ======================================================================
# upstream 实体（upstreams.yaml 权威源）
# ======================================================================

def upstream_save(path, **kw) -> tuple:
    if writer_mode() == "yaml":
        return _opsc.save_upstream(Path(path), **kw)
    operator = str(kw.get("operator") or "console")
    box = {}

    def _run(p):
        box["ret"] = _opsc.save_upstream(p, **kw)

    _upstream_write_db(Path(path), _run, action="upstream_save", operator=operator)
    return box.get("ret") or (None, None)


def upstream_offline(path, pid: str, *, operator: str = "console", now=None) -> tuple:
    if writer_mode() == "yaml":
        return _opsc.offline_upstream(Path(path), pid, operator=operator, now=now)
    box = {}

    def _run(p):
        box["ret"] = _opsc.offline_upstream(p, pid, operator=operator, now=now)

    _upstream_write_db(Path(path), _run, action="upstream_offline", operator=operator)
    return box.get("ret") or (None, None)


def upstream_draft_delete(path, pid: str, *, operator: str = "console", now=None) -> tuple:
    if writer_mode() == "yaml":
        return _opsc.delete_upstream_draft(Path(path), pid, operator=operator, now=now)
    box = {}

    def _run(p):
        box["ret"] = _opsc.delete_upstream_draft(p, pid, operator=operator, now=now)

    _upstream_write_db(Path(path), _run, action="upstream_draft_delete", operator=operator)
    return box.get("ret") or (None, None)


# ======================================================================
# grant / tenant 实体（auth_matrix.yaml 权威源；1b-8 写径接线点）
# ======================================================================

def matrix_save(path, data: dict, secret_plain=None, *, operator: str = "auth_admin") -> None:
    """矩阵落盘（auth_admin CLI 与授权页端点同调 grants_admin.save_matrix——
    本入口为 1b-8/步6 的 meta_writer 接线点：矩阵写径切换时 grants_admin.save_matrix
    内部改调本函数，CLI 零改动。yaml 模式＝原样转发；db＝校验原样发生＋差量写库。"""
    if writer_mode() == "yaml":
        from app import grants_admin as _gam   # 局部导入：1b-8 并行批模块，避免硬依赖序
        _gam.save_matrix(Path(path), data, secret_plain=secret_plain)
        return
    _matrix_save_db(Path(path), data, operator=operator, secret_plain=secret_plain)


def tenant_upsert(*, tenant_id: str, display_name: str, tenant_type: str = "external",
                  status: str = "active", org_ref=None, oa_ticket_open=None,
                  remark=None, operator: str = "console",
                  oa_ticket=None) -> dict:
    """租户实体写（1b-8 授权页/登记入口用；yaml 模式无独立租户写径——矩阵即真源）。

    yaml 模式＝矩阵登记（auth_admin register 的授权页等价物不属本层，本入口在 yaml
    模式按矩阵即真源口径转发 grants_admin 三动作不合形——现按 db 专属入口处理：
    yaml 模式调用即提示走 auth_admin CLI，不做第二种写径）。
    """
    if writer_mode() == "yaml":
        raise MetaDbError(
            "tenant_upsert 为 db 专属入口（yaml 模式租户真源＝auth_matrix.yaml，"
            "登记走 scripts/auth_admin.py register）")
    return _tenant_upsert_db(tenant_id=tenant_id, display_name=display_name,
                             tenant_type=tenant_type, status=status, org_ref=org_ref,
                             oa_ticket_open=oa_ticket_open, remark=remark,
                             operator=operator, oa_ticket=oa_ticket)
