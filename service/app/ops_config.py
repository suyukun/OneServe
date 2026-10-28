# -*- coding: utf-8 -*-
"""运营控制台写操作接真（P1-10/11 · 前台重造 SPEC §5 P1-10/11 行，D2 写侧）——纯逻辑。

职责：发布台（O-1）与上游代理（O-2）两页的真源读写，全部决策在本模块，
admin_api 只做 HTTP 编排（分层纪律同 app/appkeys.py）：

  services.yaml —— 控制台发布的服务定义写文件尾「console_services 写区」：
    - 运行时执行器注册区（services: 键）零接触：kvx.load_services 只读 services:，
      写区外文本（含全部头注/行注）逐字节保留——写区整体重生成，其余不动；
    - 原子写：同目录 tmp → 整文件校验（yaml 回读＋load_services 真加载器跑通，
      证明运行时注册表零漂移）→ os.replace；
  upstreams.yaml（新权威源）—— O-2 代理配置登记（草稿/发布/下线同口径），
    结构同 appkeys.yaml：头注释＋meta＋proxies 列表，tmp+rename 原子写。

git 留痕口径占位（M2-4 门槛稿口径）：容器内无 git 二进制且无 .git 挂载，本模块
只落 trace{action,by,at,commit:null} 登记字段，不伪造 commit hash；真实 commit
归宿主侧 git 执行器（后续集成卡）。热加载联动（M2-3.5 渲染器）未合入：写区为
登记态，运行时不读它，故无热加载必要；GET 一律读盘即时值，写后控制台立即可见。

真源锚链（目录树）：services.yaml 头注指认字段字典 CSV（297 列蓝本）为 schema
真源 → 树按 CSV 字段族分组；示例键＝发现端点对真库 scan(limit=1) 实取 rowkey
（P0-1：历史演示键 SM3(DEMO-USR-0001) 是假用户、真库无此行，已下线——空库/扫描
失败时示例键留空并在响应标注原因，绝不造假键）。CSV 路径：宿主布局 service/../sync
与容器布局 /sync 双探测，SCHEMA_CSV 环境变量可覆盖。

字段空间回环防线（TASK-M2-3.8）：publish_service 记 field_space（缺省 wide297，与
schema_drift.field_space_of 同口径），并在写前校验宽表域字段属于字典真源 297 列——
防运营台把 tags/passthrough 这类语义占位符当宽表列发布，进而在对账时被判删列误隔离。
dict_columns() 提供真源列集（CSV 不可读返 None＝跳过校验，不误挡发布）。
可测性：本模块不 import FastAPI/redis；路径全部可注入，tests 直跑。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from app import errors
from app import route_naming as rn    # 路由命名机器闸单一事实来源（提案 v1 §3；前后端同源）
from app import schema_drift as sd   # 字段空间契约（ADR-0004；FIELD_SPACE_WIDE 单一事实来源）
from app.kv_executor import TagQueryError, apply_desensitize, load_services   # 复用既有加载器/错误口径＋脱敏唯一实现（B2/B3）
from app import schema_drift as sd   # 字段空间口径单一事实来源（TASK-M2-3.8 回环防线）

SERVICE_DIR = Path(__file__).resolve().parent.parent          # service/
SERVICES_YAML = SERVICE_DIR / "services.yaml"
UPSTREAMS_YAML = SERVICE_DIR / "upstreams.yaml"
SCHEMA_CSV_NAME = "schema_usr_label_wide_297.csv"   # TODO(卡2): 297 宽表字典换源为表级 schema 元数据（装载副产品）；CSV 缓删至卡2 交接（卡5 2026-09-30）
TABLES_META_NAME = "tables_meta.yaml"   # B5：表级中文名/一句话描述（discovery tableCnName 真源）

# 写区 marker：首行精确匹配；其上文本（运行时注册区＋全部注释）写操作永不触碰
CONSOLE_MARKER = "# ==== console_services（运营控制台写区 · /v1/admin/publish 维护；手编请改上方运行时区，本区整体重写） ===="

SVC_STATUS = ("draft", "published", "offline")
PX_STATUS = ("draft", "published", "offline")
# 服务路由名校验（提案 v1 机器闸）已收敛至 app/route_naming.py 单一实现（R1 正则/
# 保留词/动词/版本段/唯一性）；本模块不再各自持有第二份正则，发布路径直接引用 rn.*。
ROUTE_PX_RE = re.compile(r"^/proxy/[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)*$")
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
UPSTREAM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")

# hbase 型发布校验（TASK-M2-4 / AC-6/7）
HBASE_TABLE_RE = re.compile(r"^[a-zA-Z0-9_]+(:[a-zA-Z0-9_]+)?$")   # namespace:table（HBase meta 同款）
HBASE_CF_RE = re.compile(r"^[a-zA-Z0-9_]+$")
# 键类型 → 键模式（默认宽松：任意合理字符串键；平台不关注键的加密算法，
# 上游可用 SM3/MD5/UUID/自增 ID 等任意形态）。key_type 仅作声明性标识。
HBASE_KEY_TYPES = {"phone_sm3": r"^[A-Za-z0-9_.-]{1,128}$",   # 上游供数键（ADR-0001/NC-M2-2 声明口径）
                   "usr_id": r"^[A-Za-z0-9_.-]{1,128}$"}     # 明文 usr_id（B3 备选口径）

# 生效范围收敛（P1-10 差量·SPEC B3/B4「生效范围选项收敛为两选」）：运营者只选
# 「先测试 / 直接上生产」；本机网关（M0 原型）→测试实例（T1 准生产）为平台部署
# 事实，由发布选择派生（先测试=测试T1×网关实例B，直接上生产=本机M0×网关实例A），
# 不再让运营者手选环境×实例二元组。存量条目 env/instance 原样兼容读出（含手编旧值）。
RELEASE_TEST = ("先测试", "测试T1", "网关实例B")
RELEASE_PROD = ("直接上生产", "本机M0", "网关实例A")
RELEASE_MODES = (RELEASE_TEST[0], RELEASE_PROD[0])
DEFAULT_ENV = RELEASE_PROD[1]
DEFAULT_INSTANCE = RELEASE_PROD[2]


def derive_env_instance(mode: str) -> tuple[str, str]:
    """发布选择「先测试/直接上生产」→（env, instance）二元组（写侧唯一派生口径）。

    空值/未知值回落「直接上生产」（默认直上）；显式选「先测试」才走测试实例——
    未知值不猜成测试环境（测试实例是更敏感的收敛选择，缺省从严）。
    """
    if (mode or "").strip() == RELEASE_TEST[0]:
        return RELEASE_TEST[1], RELEASE_TEST[2]
    return DEFAULT_ENV, DEFAULT_INSTANCE


def describe_scope(scope: dict | None) -> str:
    """存储 scope → 人话生效范围（读侧展示口径，P1-10 差量）。

    命中两选映射＝「先测试/直接上生产」；否则如实回「env × instance」（存量手编旧值
    原样显示，不强行归类成两选——读侧不篡改历史）。
    """
    env = str((scope or {}).get("env") or "")
    instance = str((scope or {}).get("instance") or "")
    for mode, d_env, d_inst in (RELEASE_TEST, RELEASE_PROD):
        if env == d_env and instance == d_inst:
            return mode
    return f"{env or DEFAULT_ENV} × {instance or DEFAULT_INSTANCE}"

# 字段字典族分组（按前缀；未命中入「其他」＝同步链路元数据列，如实展示不裁剪）
_FIELD_FAMILIES = (
    ("base", "基本信息", "if_"),
    ("behavior", "行为", "ac_"),
    ("marketing", "营销", "mk_"),
    ("channel", "渠道", "pf_"),
    ("risk", "风险", "rs_mal_"),
)


class OpsConfigError(TagQueryError):
    """写操作校验错误（结构同 TagQueryError：status/code/message，端点层统一映射）。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug_from_route(route: str) -> str:
    """路由 → 配置 slug（与前端 slugFromRoute 同算法：去首斜杠、其余斜杠转连字符）。"""
    return route.strip().lstrip("/").replace("/", "-")


def resolve_schema_csv(table: str | None = None) -> Path:
    """字段字典 CSV 定位：env 覆盖 > 宿主布局（service/../sync）> 容器布局（/sync 挂载）。

    多表字典（2026-09-29 业务化测试表口径）：table 显式给定时优先找
    sync/schema_<表名>.csv（表名剥 namespace 前缀；含非法字符整体回落），
    找不到回落既有 wide297 单字典——demo 表（无同名 CSV）与 table 缺省时行为零变化。
    """
    env = os.environ.get("SCHEMA_CSV")
    if env:
        return Path(env)
    names: list[str] = []
    if table:
        plain = table.split(":", 1)[1] if ":" in table else table
        if re.fullmatch(r"[A-Za-z0-9_]+", plain):
            names.append(f"schema_{plain}.csv")
    names.append(SCHEMA_CSV_NAME)
    for name in names:
        host = SERVICE_DIR.parent / "sync" / name
        if host.is_file():
            return host
        ctr = Path("/sync") / name
        if ctr.is_file():
            return ctr
    return SERVICE_DIR.parent / "sync" / names[-1]   # 双布局均缺失：返回宿主路径，调用方按「字典缺失」降级


def _resolve_tables_meta_path() -> Path:
    """tables_meta.yaml 定位：env TABLES_META > 宿主 service/../sync > 容器 /sync。"""
    env = os.environ.get("TABLES_META")
    if env:
        return Path(env)
    for base in (SERVICE_DIR.parent / "sync", Path("/sync")):
        p = base / TABLES_META_NAME
        if p.is_file():
            return p
    return SERVICE_DIR.parent / "sync" / TABLES_META_NAME   # 均缺失＝宿主路径（调用方按空表降级）


def load_tables_meta(path: Path | None = None) -> dict[str, dict]:
    """读 sync/tables_meta.yaml → {表全名: {cn_name, desc}}（B5：表中文名真源）。

    定位与 resolve_schema_csv 同款双布局。缺文件/非映射结构＝空 dict（调用方按
    「未登记」渲染空串，如实不猜）；登记项缺字段补空串、非映射条目跳过——
    坏登记不拖垮整个发现视图。
    """
    p = path if path is not None else _resolve_tables_meta_path()
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for full, meta in data.items():
        if not isinstance(meta, dict):
            continue
        out[str(full)] = {"cn_name": str(meta.get("cn_name") or "").strip(),
                          "desc": str(meta.get("desc") or "").strip()}
    return out


# ---------- services.yaml console_services 写区 ----------

def _split_marker(text: str) -> tuple[str, str]:
    """按 marker 行切分：返回 (写区以上文本, 写区文本)。无 marker 时写区为空。"""
    idx = text.find(CONSOLE_MARKER)
    if idx < 0:
        return text, ""
    return text[:idx], text[idx:]


def load_console_services(path: Path = SERVICES_YAML) -> list[dict]:
    """读 console_services 写区（缺文件/无写区＝空列表，非错误——首发前属正常态）。"""
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("console_services") or []
    if not isinstance(entries, list):
        raise OpsConfigError(500, errors.BAD_CONSOLE_SERVICES, "console_services 写区须为列表（文件被手编坏）")
    return entries


def _dump_console_block(entries: list[dict]) -> str:
    """写区文本＝marker 行＋console_services 块（safe_dump 生成，整体重写）。

    列表条目统一缩进 2 空格，与文件既有 services: 区排版一致（纯排版，yaml 同构）。
    """
    body = yaml.safe_dump({"console_services": entries}, sort_keys=False,
                          allow_unicode=True, default_flow_style=False)
    out = []
    for i, ln in enumerate(body.splitlines()):
        out.append(ln if i == 0 else ("  " + ln if ln else ln))   # 首行＝键名，其余整体 +2
    return CONSOLE_MARKER + "\n" + "\n".join(out) + "\n"


_RUNTIME_SERVICES_RE = re.compile(r"^services:[ \t]*(?:\[\][ \t]*)?(?:#.*)?$", re.M)   # 2026-09-29：显式空表 services: [] 也是合法运行时区（全下线终态；发布＝从空表追加首个服务）


def _runtime_list_end(text: str) -> int:
    """运行时 services 列表末尾偏移（插入新服务条目的位置）：缩进行/空行属列表，
    顶格行（注释或新键）即列表结束——顶格注释原样保留在插入点之后。"""
    m = _RUNTIME_SERVICES_RE.search(text)
    if not m:
        raise OpsConfigError(500, errors.RUNTIME_SECTION_LOST, "services: 运行时注册区缺失")
    pos, consumed = m.end(), 0
    for ln in text[pos:].split("\n"):
        if consumed and ln.strip() and not ln.startswith((" ", "\t")):
            break                                  # 顶格非空行＝列表已结束
        consumed += 1
    offset = pos + sum(len(l) + 1 for l in text[pos:].split("\n")[:consumed])
    return min(offset, len(text))


def _strip_runtime_entry(text: str, sid: str) -> str:
    """运行时区删同 service_id 旧条目块（卡4 AC-2a 覆盖更新的文本面，保注释不重排）。

    只扫 services: 列表区（块起点＝行首两空格减号；块体＝缩进 ≥4 空格的续行）；
    console_services 区不在此函数管辖（写区整体重生成，另有覆盖语义）。
    """
    m = _RUNTIME_SERVICES_RE.search(text)
    if not m:
        return text
    start = m.end()
    end = _runtime_list_end(text)
    lines = text[start:end].splitlines(keepends=True)
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].startswith("  - "):
            j = i + 1
            while j < n and not lines[j].startswith("  - ") and lines[j].startswith("    "):
                j += 1
            block = lines[i:j]
            if any(re.match(r"^    service_id: %s\s*$" % re.escape(sid), b) for b in block):
                i = j
                continue
        out.append(lines[i])
        i += 1
    return text[:start] + "".join(out) + text[end:]


def _dump_runtime_entries(entries: list[dict]) -> str:
    """运行时服务条目 → yaml 文本（缩进 2，挂在 services: 下）。"""
    body = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True,
                          default_flow_style=False).rstrip("\n")
    return "\n".join(("  " + ln) if ln else ln for ln in body.splitlines()) + "\n"


def _save_services_yaml(path: Path, head: str, entries: list[dict],
                        runtime_entries: list[dict] | None = None) -> None:
    """原子写整文件：tmp → 双重校验（yaml 回读＋load_services 真加载器）→ replace。

    runtime_entries 非空（发布态）：卡4 AC-2a 单键覆盖——先删运行时区同 service_id
    旧条目块，再把新条目追加到列表末尾（其余条目与全部注释逐字节保留——不重排不丢
    注释、不产生第二条目）；console_services 写区照旧整体重生成。写入前后均以真加载器
    load_services 校验＝运行时注册表零漂移。
    """
    text = head
    if runtime_entries:
        m = _RUNTIME_SERVICES_RE.search(text)      # 内联空表 services: [] 归一为块式（去 []，保留行尾注释）
        if m and "[]" in m.group(0):
            text = text[:m.start()] + m.group(0).replace("[]", "") + text[m.end():]
        # 卡4 AC-2a：同 service_id 覆盖（先移除旧条目块，再追加新条目＝单键注册表恒一条目）
        for rt in runtime_entries:
            text = _strip_runtime_entry(text, str(rt["service_id"]))
        at = _runtime_list_end(text)              # 先定位（此时 head 仍是插入前文本）
        text = text[:at] + _dump_runtime_entries(runtime_entries) + text[at:]
    if not text.endswith("\n"):
        text += "\n"
    text += _dump_console_block(entries)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        parsed = yaml.safe_load(tmp.read_text(encoding="utf-8")) or {}
        if "services" not in parsed or parsed["services"] is None:
            raise OpsConfigError(500, errors.RUNTIME_SECTION_LOST, "校验失败：services: 运行时注册区缺失")
        if parsed["services"]:                     # 空表终态下真加载器 fail-loud（非发布运行时条目场景），只做 yaml 校验
            load_services(tmp)   # 真加载器跑通＝运行时注册表零漂移才允许落盘
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _svc_view(e: dict) -> dict:
    """写区条目 → 前端列表口径（camelCase，与 admin/publish.js 既有列表形状对齐）。"""
    scope = e.get("scope") or {}
    backend = e.get("backend") or {}
    hb = backend.get("hbase") or {}
    load = backend.get("load") or {}   # 加载来源留痕（后端不读；前端展示溯源）
    return {
        "id": e.get("id"), "serviceName": e.get("name"), "routePath": e.get("route"),
        "method": e.get("method", "POST"), "key": e.get("key"),
        "fields": [f["name"] for f in e.get("fields") or []],
        # 字段空间随视图回显；缺省＝宽表域（与 schema_drift.field_space_of 同口径）
        "fieldSpace": sd.field_space_of(e),
        "status": e.get("status"),   # 卡4：服务修订号已删（version/versions 不再回显）
        # 数据源（AC-4/AC-6 前端目录树用）：redis 型无 backend（存量形状零改动）→ datasource 缺省。
        # 注意：HBase 选表发布的产物现已改为 Redis 型（ADR-0005），其 HBase 溯源在 loadSource。
        "datasource": backend.get("source", "redis"),
        "serviceId": e.get("service_id"),
        "hbaseTable": hb.get("table") or None, "hbaseCf": hb.get("cf") or None,
        "keyType": (hb.get("key_type") or load.get("key_type")
                    or next((p.get("key_type") for p in e.get("params") or [] if p.get("key_type")), None)),
        # 加载来源（溯源）：从哪张 HBase 表/列族加载、落在哪个 Redis 版本空间
        "loadSource": ({"table": load.get("table"), "cf": load.get("cf"),
                        "keyType": load.get("key_type"), "version": load.get("version")}
                       if load else None),
        "fieldSpace": e.get("field_space") or sd.FIELD_SPACE_WIDE,
        "env": scope.get("env", DEFAULT_ENV), "instance": scope.get("instance", DEFAULT_INSTANCE),
        "releaseMode": describe_scope(scope),   # 读侧人话口径（两选命中→先测试/直接上生产）
        "lastModifiedBy": e.get("last_modified_by"), "lastModifiedAt": e.get("last_modified_at"),
        "publishedAt": e.get("published_at"), "gitShortHash": None,   # git 留痕占位：不伪造 hash
    }


def _history_append(prev: dict | None, status: str, iso: str, operator: str, delta: str) -> list[dict]:
    """发布史追加一条记录到 trace.history（卡4 NC-2b/NC-4b：旧值留痕保底通道）。"""
    hist = list(((prev or {}).get("trace") or {}).get("history") or [])
    return hist + [{"status": status, "at": iso, "by": operator, "delta": delta}]


def _delta_str(prev: list[str], now: list[str]) -> str:
    add = len([n for n in now if n not in prev])
    rem = len([n for n in prev if n not in now])
    if add and rem:
        return f"+{add} -{rem}"
    if add:
        return f"+{add}"
    if rem:
        return f"-{rem}"
    return "±0"


def _resolve_scope(env: str, instance: str, release_mode: str) -> dict:
    """生效范围落盘口径（P1-10 差量）：显式 env/instance（存量调用方/手编兼容）
    优先；否则由「先测试/直接上生产」发布选择派生（derive_env_instance）。"""
    if env.strip() and instance.strip():
        return {"env": env.strip(), "instance": instance.strip()}
    d_env, d_instance = derive_env_instance(release_mode)
    return {"env": env.strip() or d_env, "instance": instance.strip() or d_instance}


# ---------- 字段字典真源（双源校验的字典侧，AC-4） ----------

def load_field_dict(csv_path: Path | None = None, table: str | None = None) -> dict[str, dict]:
    """读字段字典 CSV → {字段名: {cnName, type, sensitivity, definition}}。

    真源缺省＝sync/schema_usr_label_wide_297.csv（297 列）；table 给定时按
    resolve_schema_csv(table) 优先取 sync/schema_<表名>.csv（多表字典：业务化测试表
    各用各的 cn_name/业务口径），缺表级 CSV 同样回落宽表字典。csv_path 显式传入
    优先（既有签名兼容，调用方注入测试字典不变）。文件缺失→返回空 dict，
    调用方按「字典缺失」显式拒绝（不静默放行）。
    """
    csv_path = csv_path if csv_path is not None else resolve_schema_csv(table)
    if not csv_path.is_file():
        return {}
    import csv as _csv
    out: dict[str, dict] = {}
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for r in _csv.DictReader(fh):
            name = (r.get("字段名") or "").strip()
            if not name:
                continue
            out[name] = {
                "cnName": (r.get("cn_name") or "").strip() or "待补",
                "type": (r.get("数据类型") or "").strip() or "unknown",
                "sensitivity": (r.get("敏感级") or "").strip(),
                "definition": (r.get("业务口径") or "").strip(),
            }
    return out


# ---------- AC-4：HBase meta × 字段字典 CSV 双源校验 ----------

def verify_datasource(meta: dict, dict_cols: dict[str, dict]) -> dict:
    """双源校验（AC-4）：HBase meta 与字段字典 CSV 不一致即拦截。

    口径（据实判定，不虚报一致）：
      - HBase meta 列 ∈ 字典 297 列 → 一致；
      - meta 列 ∉ 字典 → unknown_columns＝HBase 有而字典无（发布侧在此拦截）；
      - 字典 297 列 ∉ meta 列 → dict_not_in_meta（HBase 样本未覆盖；HBase meta
        无列级 API，列清单由样本行反推，语义＝「样本未见」而非「不存在」——如实标注）；
      - 字典缺失（dict_cols 空）→ ok=False（真源不可读＝不可发布，宁拦不放）。
    返回 {ok, unknown_columns, dict_not_in_meta, hbase_columns, dict_columns}。
    """
    hbase_cols: set[str] = set()
    for ns in (meta.get("namespaces") or {}).values():
        for tbl in ns.values():
            for cols in (tbl.get("families") or {}).values():
                hbase_cols.update(cols)
    dict_names = set(dict_cols)
    unknown = sorted(hbase_cols - dict_names)
    not_in_meta = sorted(dict_names - hbase_cols)
    return {
        "ok": bool(dict_names) and not unknown,
        "unknown_columns": unknown,              # HBase 有而字典无 → 拦截依据
        "dict_not_in_meta": not_in_meta,          # 字典有而 HBase 样本未见 → 如实提示（不单独拦截）
        "hbase_columns": sorted(hbase_cols),
        "dict_columns": sorted(dict_names),
        "dict_present": bool(dict_names),
    }


def validate_against_dict(fields: list[dict], dict_cols: dict[str, dict]) -> None:
    """加载期校验（AC-6）：出参字段必须 ∈ 字段字典真源，否则拒绝发布。

    B1（发布台重设计 SPEC v1.1 §七）：dict_cols 由调用方按表取（表级 CSV 优先、
    缺表级回落宽表 297）——列数如实按传入字典计，不再写死 297。
    校验在写状态键/写文件之前执行——把假阳性挡在写状态之前（ADR-0004 风险对策）。
    """
    if not dict_cols:
        raise OpsConfigError(503, "dict_unavailable",
                             "字段字典真源不可读（表级/宽表 CSV 均缺失或悬挂），"
                             "拒绝发布：字段校验无真源可比（宁拦不放）")
    n = len(dict_cols)
    for f in fields:
        fname = f["name"]
        if fname not in dict_cols:
            raise OpsConfigError(400, errors.FIELD_NOT_IN_DICT,
                                 f"字段 {fname} 不在字段字典真源 {n} 列内（加载期校验拒绝；"
                                 f"当前字典＝所选表命中的表级字典或宽表 {n} 列回落，"
                                 f"HBase 列与字典不一致须先核对字典，见 AC-4 双源校验）")


# ---------- B2/B3（发布台重设计 SPEC v1.1 §七）：示例值自动生成＋显式脱敏标记映射 ----------

SAMPLE_SCAN_LIMIT = 20   # 发布示例值采样行数上限（admin_api 按此 scan；每字段取首个非空值）

# band_mask 档位规则（显式配置专用；机制＝kv_executor._band_mask 分档输出，
# 原值零出现，单一实现不加第二份）：数值按量级 A(<1万)/B(<100万)/C(≥100万) 三档，
# 非数值 unknown 档 U。仅当发布请求显式给字段配 desensitize="band_mask" 时生效，
# 不再依据字典敏感级自动配置（2026-09-29 裁决：敏感概念整体移除，授权为唯一访问控制层）。
BAND_MASK_MARKER = "band_mask"
BAND_MASK_RULE: dict = {
    "type": "band_mask",
    "bands": [{"max": 10_000, "out": "A"}, {"max": 1_000_000, "out": "B"}, {"out": "C"}],
    "unknown_out": "U",
}


def effective_desensitize_rules(fields: list[dict]) -> dict[str, dict]:
    """字段落盘脱敏标记 → 可执行规则映射（band_mask→内建档位规则；其余标记无内建规则不映射）。

    同一映射两用（规则单一来源）：示例值脱敏（B2）与运行时 response.desensitize
    （kv_executor 查询期按此分档输出）——手动显式配置不止是标记，真实生效。
    """
    return {f["name"]: BAND_MASK_RULE
            for f in fields if f.get("desensitize") == BAND_MASK_MARKER}


def pick_field_samples(fields: list[dict], sample_rows: list[dict]) -> dict[str, str]:
    """B2 示例值自动生成：从表样本行实取每字段首个非空原始值 → {字段名: 值}。

    sample_rows＝discovery 同款 scan 采样形状 [{row, columns:{"cf:列名": 值}}]
    （与 collect_rowkey_samples 同一条 HBase 连接层）；列名按 qualifier 末段匹配，
    跨列族有效。只取值不脱敏——脱敏由调用方按生效规则统一执行（原值零出现）。
    """
    wanted = {f["name"] for f in fields}
    out: dict[str, str] = {}
    for row in sample_rows or []:
        for qual, val in ((row or {}).get("columns") or {}).items():
            name = str(qual).rsplit(":", 1)[-1]
            if name in wanted and name not in out and str(val).strip():
                out[name] = str(val).strip()
        if len(out) == len(wanted):
            break
    return out


def build_hbase_entry(*, sid: str, name: str, route: str, table: str, cf: str, key_type: str,
                      fields: list[dict], status: str, operator: str,
                      scope: dict, prev: dict | None, iso: str) -> dict:
    """【保留代码 · M3 候选】构造 hbase 直连型服务条目：backend.source=hbase＋backend.hbase{...}。

    ⚠️ 架构口径（ADR-0005 / M2范围裁定 v2，维护者 2026-09-23）：**发布链路不再产出本类型条目**
    ——M2 主链路＝「HBase 选表 → 加载到 Redis → 从 Redis 发 API」。本函数与
    backend.source=hbase 的执行能力（hbase_service/hbase_executor）一并**保留代码**，
    范围上归 M3 候选（HBase 独立数据源／缓存兜底），**不在发布产物里**；
    发布入口走 build_redis_entry_from_hbase（Redis 型）。

    格式照既有 demo_hbase_query 条目（services.yaml 运行时注册区）：
      backend: {source: hbase, hbase: {table: "ns:table", cf, key_type}}；
      field_space: wide297（ADR-0004 硬要求——漂移防护只对宽表域生效，本卡补齐）；
      params[0] 键模式按 key_type 派生（默认宽松，见 HBASE_KEY_TYPES）。
    版本复合键：同 route 重复发布＝新版本条目（versions 追加）；旧版本条目整体保留，
    不覆盖旧版（AC-8-a）；条目自带 trace（含 oa_ticket=OA 单号引用）。
    """
    prev_names = [f["name"] for f in (prev or {}).get("fields") or []]
    now_names = [f["name"] for f in fields]
    return {
        "id": sid, "name": name, "route": route, "method": "POST",
        "key": f"usr_id（{key_type}）",
        "backend": {
            "source": "hbase",
            "hbase": {"table": table, "cf": cf, "key_type": key_type},
        },
        "field_space": sd.FIELD_SPACE_WIDE,   # ADR-0004：宽表 297 列域（漂移防护生效面）
        # 键模式按 key_type 派生（hbase_service.validate_backend 要求 params 在场；
        # 模式与 hbase_executor/service 层同口径——默认宽松，平台不关注键的加密算法）
        "params": [{"name": "key", "type": "string(1..128)", "key_type": key_type,
                    "pattern": HBASE_KEY_TYPES[key_type]}],
        "fields": fields, "status": status,
        "scope": scope,
        "last_modified_by": operator, "last_modified_at": iso,
        "published_at": iso if status == "published" else (prev or {}).get("published_at"),
        "trace": {"action": status, "by": operator, "at": iso,
                  "commit": None,        # git 留痕占位：容器内无 git，不伪造 hash（宿主侧执行器）
                  "oa_ticket": str((prev or {}).get("trace", {}).get("oa_ticket") or ""),
                  # 卡4 NC-4b：发布史折算/追加在 trace.history（覆盖更新不丢既有记录）
                  "history": _history_append(prev, status, iso, operator,
                                             _delta_str(prev_names, now_names) if prev
                                             else f"+{len(now_names)}")},
    }


# 键空间口径（＝sync/sync_config.py NS/ALIAS_KEY/VER_META，单一事实来源；此处只读不改）。
# 下面四个常量＝缺省域 user（存量单域口径，守卫测试钉死与 sync_config 逐字一致）；
# 发布条目按**源表**生成各自键空间（2026-09-29 按表隔离：防 A 表切版顶掉 B 表），
# 见 kv_keyspace(table)——表名口径＝裸表名（剥 namespace 前缀，与 sync_config.key_domain 同规）。
KV_NS = "tags:user"
KV_ALIAS_KEY = "tags:user:latest"
KV_DATA_KEY = "tags:user:{version}:{sm3}"
KV_VERSION_META_KEY = "sync:ver:{version}"


def kv_keyspace(table: str) -> dict:
    """源表 → 该表的 KV 键空间四件套（namespace/alias_key/data_key/version_meta_key）。

    与 sync/sync_config.use_table 同一规则（裸表名剥前缀）：发布自 usr_basic_info 的
    服务读 tags:usr_basic_info:*，发布自 usr_asset_tag 的读 tags:usr_asset_tag:*——
    每表独立指针/版本空间，互不顶掉。表名非法（空/字符集外）抛 ValueError（HTTP 层如实 400）。
    """
    domain = (table or "").strip()
    if ":" in domain:
        domain = domain.split(":", 1)[1]
    if not re.fullmatch(r"[A-Za-z0-9_]+", domain):
        raise ValueError(f"表名 {table!r} 无法用作键空间域段（须为裸表名，字符集 [A-Za-z0-9_]）")
    return {
        "namespace": f"tags:{domain}",
        "alias_key": f"tags:{domain}:latest",
        "data_key": f"tags:{domain}:{{version}}:{{sm3}}",
        "version_meta_key": f"sync:ver:{domain}:{{version}}",
    }


def build_redis_entry_from_hbase(*, sid: str, name: str, route: str, table: str, cf: str,
                                 key_type: str, fields: list[dict], status: str, operator: str,
                                 scope: dict, prev: dict | None, iso: str,
                                 load_version: str = "", loaded: bool = False) -> dict:
    """构造**加载后 Redis 型**服务条目（本卡核心；ADR-0005 口径）。

    发布产物＝backend.kv（与存量 user_tag_query 同形同口径：alias_key 指针＋
    data_key 模板＋version_meta_key），**不再产出 backend.source=hbase**——
    查询路径＝Redis 热存储（kv_executor），加载链路另经 /v1/admin/publish/hbase/load。
    键空间按源表隔离（kv_keyspace(table)）：每表独立 tags:<表>:latest 指针与版本空间。
    保留 field_space=wide297（ADR-0004）与 params 键模式（与 hbase 型同派生口径）。

    来源留痕：backend.load 记「数据从哪张 HBase 表/列族加载、落在哪个版本空间」——
    只作运维溯源，**不被执行器读取**（kv_executor 只认 backend.kv），故不构成第二真相源。
    """
    prev_names = [f["name"] for f in (prev or {}).get("fields") or []]
    now_names = [f["name"] for f in fields]
    ks = kv_keyspace(table)   # 按源表隔离的键空间四件套（tags:<表>:…）
    entry = {
        "id": sid, "name": name, "route": route, "method": "POST",
        "key": f"usr_id 的 {key_type}" if key_type == "phone_sm3" else f"usr_id（{key_type}）",
        "backend": {
            "kv": {
                **ks,
                "storage_key": "as_received",
                "entity": "user",
            },
        },
        "field_space": sd.FIELD_SPACE_WIDE,   # ADR-0004：宽表 297 列域（漂移防护生效面）
        "params": [{"name": "key", "type": "string(64)", "key_type": key_type,
                    "pattern": HBASE_KEY_TYPES[key_type]}],
        "fields": fields, "status": status,
        "scope": scope,
        "last_modified_by": operator, "last_modified_at": iso,
        "published_at": iso if status == "published" else (prev or {}).get("published_at"),
        "trace": {"action": status, "by": operator, "at": iso,
                  "commit": None,        # git 留痕占位：容器内无 git，不伪造 hash（宿主侧执行器）
                  "oa_ticket": str((prev or {}).get("trace", {}).get("oa_ticket") or ""),
                  # 卡4 NC-4b：发布史折算/追加在 trace.history（覆盖更新不丢既有记录）
                  "history": _history_append(prev, status, iso, operator,
                                             _delta_str(prev_names, now_names) if prev
                                             else f"+{len(now_names)}")},
    }
    if load_version or loaded:
        # 来源留痕（溯源用；执行器不读）：从哪张 HBase 表加载、落在哪个 Redis 版本空间
        entry["backend"]["load"] = {
            "source": "hbase", "table": table, "cf": cf, "key_type": key_type,
            "version": load_version or "", "loaded": bool(loaded),
        }
    return entry


def _carry_card_meta(entry: dict, prev: dict | None) -> dict:
    """卡面元数据保持（维护者 2026-10-15 批，N5「重发重建条目丢 desc」边界同款收口）。

    发布向导不收集 desc/update_frequency/owner 三项（登记面＝手编 services.yaml 写区
    或后续运营台），重发重建条目时从前条目继承——否则三项静默丢失（N5 已知边界，
    2026-10-14 desc 实发）。新条目自带值（未来向导接入收集）优先，不覆盖。
    就地改 entry 并返回（hbase 通道须在拷贝运行时条目之前调用，两区同步继承）。
    """
    for meta_key in ("desc", "update_frequency", "owner"):
        if str(entry.get(meta_key) or "").strip():
            continue
        inherited = str((prev or {}).get(meta_key) or "").strip()
        if inherited:
            entry[meta_key] = inherited
    return entry


def normalize_publish_payload(payload: dict) -> dict:
    """前端发布请求 → publish_service 入参（**M2-5 契约**；纯函数，HTTP 层只做编排）。

    接受两种数据源口径（M2-5 适配层 admin/publish-hbase-adapter.js 发第一式）：
      ① 嵌套式：{datasource 或 backend.source, keyType, backend:{source,
                 hbase:{namespace,table,cf,key_type}}, oaTicket}
      ② 扁平式（本卡早期/手调）：{datasource, hbaseTable/hbaseCf/keyType, hbase{...}}
    表名统一为 services.yaml 口径 `namespace:table`（demo_hbase_query 同款）；namespace
    缺省 default。缺省数据源＝redis（存量行为零改动）。
    返回 {datasource, hbase:{table,cf,key_type}, load_version, oa_ticket}——hbase 结构校验
    归 publish_service（此处只归一形状，不做业务判断）。
    """
    p = payload if isinstance(payload, dict) else {}
    backend = p.get("backend") if isinstance(p.get("backend"), dict) else {}
    backend_hb = backend.get("hbase") if isinstance(backend.get("hbase"), dict) else {}
    flat_hb = p.get("hbase") if isinstance(p.get("hbase"), dict) else {}
    # 卡3（B3.2）：扁平式 source:'hbase' 与既有 datasource/backend.source 三式兼容——
    # 五步发布页发扁平式；旧适配层/手调试发嵌套式，行为零变化。
    datasource = str(p.get("datasource") or p.get("source")
                     or backend.get("source") or "redis").strip() or "redis"
    namespace = str(backend_hb.get("namespace") or p.get("namespace") or "").strip()
    table = str(backend_hb.get("table") or flat_hb.get("table")
                or p.get("hbaseTable") or p.get("table") or "").strip()
    if table and ":" not in table and namespace and namespace != "default":
        table = f"{namespace}:{table}"      # services.yaml 口径＝namespace:table
    return {
        "datasource": datasource,
        "hbase": {
            "table": table,
            "cf": str(backend_hb.get("cf") or flat_hb.get("cf")
                      or p.get("hbaseCf") or p.get("cf") or "").strip(),
            "key_type": str(backend_hb.get("key_type") or backend_hb.get("keyType")
                            or flat_hb.get("key_type") or p.get("keyType")
                            or p.get("key_type") or "").strip(),
        },
        "load_version": str(p.get("loadVersion") or p.get("load_version") or "").strip(),
        "oa_ticket": str(p.get("oaTicket") or p.get("oa_ticket") or "").strip(),
    }


def publish_service(path: Path, *, name: str, route: str, fields: list[dict],
                    field_space: str = sd.FIELD_SPACE_WIDE, env: str = "",
                    instance: str = "", status: str = "published",
                    operator: str = "console", now: datetime | None = None,
                    release_mode: str = "", dict_cols: set[str] | None = None,
                    datasource: str = "redis",
                    hbase: dict | None = None, oa_ticket: str = "",
                    load_version: str = "",
                    csv_path: Path | None = None,
                    require_dict: bool = False,
                    sample_rows: list[dict] | None = None) -> tuple[dict, dict | None]:
    """发布/存草稿一条数据服务定义 → 写 services.yaml console_services 写区。

    datasource="redis"（缺省，向后兼容）：既有 Redis(KV) 型行为逐字节不变——
    本卡不改存量 Redis 型发布行为。
    datasource="hbase"（本卡改造后＝「HBase 选表 → 加载到 Redis → Redis 型服务」）：
    须给 hbase={table, cf, key_type}；出参字段必须 ∈ 所选表字典（B1：表级 CSV 优先、
    缺表级回落宽表 297；加载期校验，写状态键之前拦下）；sample_rows 给定时发布态
    缺示例值自动从样本行实取（B2，显式配 band_mask 的字段经档位规则后填充）；
    **产物＝ backend.kv（Redis 型）＋field_space=wide297（ADR-0004）
    ＋backend.load 来源留痕**——不再落 backend.source=hbase（ADR-0005：HBase 直连
    保留代码但不在发布产物里）；同 route 重复发布＝版本复合键新版本（不覆盖旧版）。
    load_version＝加载落定的 Redis 版本空间（供来源留痕与前端展示，不参与执行）。

    同 route＝同 id＝新版本条目（版本栏 ±N 摘要）；新 route＝V1。
    返回 (新条目视图, 前版本视图)；校验失败抛 OpsConfigError，文件零变更。

    字段空间（TASK-M2-3.8 回环防线）：`field_space` 记入条目（缺省＝宽表域 wide297，
    与 schema_drift.field_space_of 同口径，向后兼容）。**写前校验**：宽表域的字段必须
    存在于字段字典真源 297 列；否则拒绝发布并给人话（换空间或订正字段名）——把
    假阳性（把 tags/passthrough 这类语义占位符当宽表列）挡在写状态键之前，防止
    运营台从后门把已修好的误隔离放回来。dict_cols=None 时自读真源；CSV 不可读
    则跳过该校验（不因解析不出字典而误挡发布）。

    AC-7：校验失败抛 OpsConfigError，文件零变更。
    """
    name = (name or "").strip()
    route = (route or "").strip()
    if not name:
        raise OpsConfigError(400, errors.INVALID_NAME, "服务名必填")
    # 路由命名机器闸（提案 v1 R1/R3/R4/R7，全部人话报错；R2 域码建议不阻断）。
    # 校验函数单一实现于 app/route_naming.py——前端（route-naming.js 同源生成）与
    # 测试复用同一份常量，禁止两处各写。
    if rn.check_route(route) is not None:
        raise OpsConfigError(400, errors.INVALID_ROUTE, rn.route_error_message(route))
    if status not in ("draft", "published"):
        raise OpsConfigError(400, errors.INVALID_STATUS, "status 须为 draft 或 published")
    if field_space not in sd.FIELD_SPACES:
        raise OpsConfigError(400, errors.INVALID_FIELD_SPACE,
                             f"字段空间非法：{field_space}（可选：{'/'.join(sd.FIELD_SPACES)}）")
        raise OpsConfigError(400, errors.INVALID_STATUS, "status 须为 draft 或 published")
    if datasource not in ("redis", "hbase"):
        raise OpsConfigError(400, "invalid_datasource", "datasource 须为 redis 或 hbase")

    hb: dict | None = None
    if datasource == "hbase":
        hb = hbase or {}
        table = str(hb.get("table") or "").strip()
        cf = str(hb.get("cf") or "").strip()
        key_type = str(hb.get("key_type") or "").strip()
        if not HBASE_TABLE_RE.fullmatch(table):
            raise OpsConfigError(400, "invalid_hbase_table",
                                 "HBase 表名须形如 table 或 namespace:table（字母数字下划线）")
        if not HBASE_CF_RE.fullmatch(cf):
            raise OpsConfigError(400, "invalid_hbase_cf", "列族名非法（字母数字下划线）")
        if key_type not in HBASE_KEY_TYPES:
            raise OpsConfigError(400, errors.INVALID_KEY_TYPE,
                                 f"键类型未定/非法：{key_type or '(空)'}（须为 phone_sm3 或 usr_id）")
        hb = {"table": table, "cf": cf, "key_type": key_type}

    # B1（发布台重设计 SPEC v1.1 §七）：发布校验按表取字典——hbase 型优先表级字典
    # sync/schema_<表名>.csv、缺表级 CSV 回落宽表 297（与 discovery 同款
    # load_field_dict/resolve_schema_csv 机制，零第二实现）；redis 型零变化。
    dict_meta: dict[str, dict] = {}
    if datasource == "hbase" or require_dict:
        dict_meta = load_field_dict(csv_path, hb["table"] if hb else None)

    clean = []
    for f in fields or []:
        fname = str((f or {}).get("name") or "").strip()
        if not FIELD_NAME_RE.fullmatch(fname):
            raise OpsConfigError(400, errors.INVALID_FIELD, f"字段名非法：{fname or '(空)'}（小写字母开头，字母数字下划线）")
        clean.append({"name": fname, "sample": str((f or {}).get("sample") or "").strip(),
                      "desc": str((f or {}).get("desc") or "").strip(),
                      "desensitize": str((f or {}).get("desensitize") or "").strip()})
    if not clean:
        raise OpsConfigError(400, errors.EMPTY_FIELDS, "至少勾选 1 个出参字段")

    if datasource == "hbase":
        # B2：发布态缺示例值 → 从表样本行实取（discovery 同款 scan 采样形状，由
        # admin_api 注入），经生效脱敏规则后填充——向导无输入框；采不到该列非空值
        # → 下方 SAMPLE_REQUIRED 门禁如实拒绝（fail-closed，不造假值顶位）。
        if status == "published" and sample_rows and any(not f["sample"] for f in clean):
            picked = pick_field_samples(clean, sample_rows)
            picked = apply_desensitize(picked, effective_desensitize_rules(clean))
            for f in clean:
                if not f["sample"] and f["name"] in picked:
                    f["sample"] = picked[f["name"]]

    for f in clean:
        if status == "published" and not f["sample"]:
            hint = ("；已尝试从表样本行自动生成，未取到该列非空值（表空或列缺失）"
                    if datasource == "hbase" and sample_rows else "")
            raise OpsConfigError(400, errors.SAMPLE_REQUIRED,
                                 f"字段 {f['name']} 缺示例值（测试前置门禁：发布态示例值必填{hint}）")

    # 回环防线（TASK-M2-3.8）：宽表域引用字段必须属于字典真源 297 列，否则拒发。
    # hbase 型例外（B1）：按所选表的字典校验（validate_against_dict，表级优先/宽表
    # 回落）——业务表字段不在宽表 297 是常态，正是按表取字典修的缺口。
    if field_space == sd.FIELD_SPACE_WIDE and datasource != "hbase":
        cols = dict_cols if dict_cols is not None else dict_columns()
        if cols is not None:
            unknown = [f["name"] for f in clean if f["name"] not in cols]
            if unknown:
                raise OpsConfigError(
                    400, errors.FIELD_NOT_IN_DICT,
                    f"字段 {('、'.join(unknown))} 不属于宽表域（字段字典 297 列中无此列）。"
                    "若为资讯域/代理域语义占位符，该服务须在服务注册中声明 field_space=news/proxy；"
                    "若确属宽表，请订正字段名——宽表域引用不存在的列会在对账时被判删列而误隔离。")

    if datasource == "hbase":
        # 加载期校验（AC-6）：字段∈所选表字典（B1：表级优先/宽表回落）
        # ——写状态键之前拦下
        validate_against_dict(clean, dict_meta)

    now = now or datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    entries = load_console_services(path)
    sid = _slug_from_route(route)
    # R6 唯一性（提案 §4.2/§5 G3）：同 routePath 已被其他服务（不同 service_id，或
    # 不同服务名＝跨归属）占用 → 400 拒发，防跨归属静默合并版本史；同服务同名重发＝
    # 同 id 升版本（既有语义保留）。查全量：console 写区＋运行时 services: 区（真加载器）。
    runtime_reg = load_services(path) if path.is_file() else {}
    occupant = rn.find_route_occupant(route, sid=sid, name=name,
                                      console_entries=entries,
                                      runtime_services=runtime_reg)
    if occupant is not None:
        raise OpsConfigError(400, errors.ROUTE_OCCUPIED,
                             f"该地址已被服务「{occupant}」占用（/v1/svc 地址全网唯一）。"
                             "请换一个地址后重试；如是服务改名，请用新地址重新发布，"
                             "旧地址走下线流程，不要复用他人地址")
    prev = next((e for e in entries if e.get("id") == sid), None)
    prev_names = [f["name"] for f in (prev or {}).get("fields") or []]
    now_names = [f["name"] for f in clean]
    runtime_entries: list[dict] = []      # AC-6：待写入运行时 services: 列表的新服务条目

    if datasource == "hbase":
        # 本卡核心（ADR-0005）：发布产物＝**加载后 Redis 型服务**（backend.kv），
        # 不再产出 backend.source=hbase（hbase 直连能力保留代码，归 M3 候选）。
        entry = build_redis_entry_from_hbase(
            sid=sid, name=name, route=route, table=hb["table"],
            cf=hb["cf"], key_type=hb["key_type"], fields=clean,
            status=status, operator=operator,
            scope=_resolve_scope(env, instance, release_mode),
            prev=prev, iso=iso, load_version=load_version)
        if oa_ticket.strip():
            entry["trace"]["oa_ticket"] = oa_ticket.strip()   # 操作者＋时间＋OA 单号引用
        # 卡面元数据保持：须在拷贝运行时条目之前——console 与运行时两区同步继承
        _carry_card_meta(entry, prev)
        # 已发布 → 落运行时服务条目（可调＝注册表必须认它；Redis 型经 kv_executor 点查）。
        # 卡4 AC-2a：注册表 service_id 单键——重复发布＝覆盖更新（_save_services_yaml
        # 先删同 id 旧条目块再追加新条目；旧值已进 trace.history，不产生第二条目）。
        if status == "published":
            runtime_entry = {k: v for k, v in entry.items() if k != "id"}
            runtime_entry.update({
                "service_id": sid, "archetype": "kv_lookup",
                "response": {"projection": "auth_matrix", "meta": ["data_as_of", "live_version"]},
                "guard": {"timeout_ms": 3000, "max_items_per_call": 1},
                "audit": {"enabled": True, "log_params": "hash_only",
                          "log_response_digest": "sha256_of_desensitized_body"},
            })
            # 显式 band_mask 标记落可执行规则——kv_executor 查询期按 response.
            # desensitize 分档输出（原值零出现），手动显式配置真实生效不止是标记。
            _rules = effective_desensitize_rules(clean)
            if _rules:
                runtime_entry["response"]["desensitize"] = _rules
            runtime_entries.append(runtime_entry)
    else:
        # —— 既有 Redis(KV) 型（向后兼容，行为逐字节保持不变）——
        prev_names = [f["name"] for f in (prev or {}).get("fields") or []]
        now_names = [f["name"] for f in clean]
        entry = {
            "id": sid, "name": name, "route": route, "method": "POST", "key": "usr_id",
            "fields": clean, "field_space": field_space, "status": status,
            "scope": _resolve_scope(env, instance, release_mode),
            "last_modified_by": operator, "last_modified_at": iso,
            "published_at": iso if status == "published" else (prev or {}).get("published_at"),
            "trace": {"action": status, "by": operator, "at": iso, "commit": None,
                      # 卡4 NC-4b：发布史追加在 trace.history（覆盖更新不丢既有记录）
                      "history": _history_append(prev, status, iso, operator,
                                                 _delta_str(prev_names, now_names) if prev
                                                 else f"+{len(now_names)}")},
        }
        _carry_card_meta(entry, prev)   # 卡面元数据保持（redis 通道同口径）

    if prev is not None:
        # 卡4 AC-2a：同 id 覆盖更新（hbase/redis 两通道同口径；旧值已进 trace.history）
        entries[entries.index(prev)] = entry
    else:
        entries.append(entry)

    text = path.read_text(encoding="utf-8") if path.is_file() else "services: []\n"
    head, _ = _split_marker(text)
    _save_services_yaml(path, head, entries, runtime_entries)
    return _svc_view(entry), (_svc_view(prev) if prev else None)


def offline_service(path: Path, sid: str, *, operator: str = "console",
                    now: datetime | None = None) -> tuple[dict, dict]:
    """下线一条已发布服务（published → offline；trace.history 追加下线记录）。"""
    now = now or datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    entries = load_console_services(path)
    prev = next((e for e in entries if e.get("id") == sid), None)
    if prev is None:
        raise OpsConfigError(404, errors.SERVICE_NOT_FOUND, f"服务 {sid} 未在写区登记")
    if prev.get("status") == "offline":
        raise OpsConfigError(409, errors.ALREADY_OFFLINE, "该服务已是下线状态")
    if prev.get("status") == "draft":
        raise OpsConfigError(409, errors.DRAFT_NOT_OFFLINE, "草稿无线上语义，直接覆盖发布即可（不支持下线草稿）")
    entry = dict(prev)
    prev_trace = dict(prev.get("trace") or {})
    # 卡4 AC-2c：下线不再向 versions[] 追加记录；留痕走 trace.history（保底通道）。
    # oa_ticket 等既有留痕字段原样继承（不被 offline 动作冲掉）。
    prev_trace.update({
        "action": "offline", "by": operator, "at": iso, "commit": None,
        "history": list(prev_trace.get("history") or []) + [
            {"status": "offline", "at": iso, "by": operator, "delta": "±0"}],
    })
    entry.update({
        "status": "offline", "last_modified_by": operator, "last_modified_at": iso,
        "trace": prev_trace,
    })
    entries[entries.index(prev)] = entry
    text = path.read_text(encoding="utf-8")
    head, _ = _split_marker(text)
    _save_services_yaml(path, head, entries)
    return _svc_view(entry), _svc_view(prev)


# ---------- upstreams.yaml（O-2 代理配置权威源） ----------

_UPSTREAMS_HEADER = """\
# ============================================================
# OneServe 上游代理配置（service/upstreams.yaml）——O-2 三步向导唯一权威源
# 写入方：运营控制台「存草稿/发布/下线」端点（P1-10）；本文件随仓库 git 留痕。
# 留痕口径占位：条目 trace 只登记 action/by/at（commit:null）——容器内无 git
# 二进制，真实 commit 归宿主侧 git 执行器（M2-4 口径，本端点不伪造 hash）。
# 生效边界（2026-10-15 起消费已接通）：本表由 proxy_hub 消费——数据面 /proxy/*
# 透传直读本表；网关路由经 upstreams 看门 ≤轮询间隔幂等下发（发布→PUT，下线→DELETE）。
# 结构：proxies[] -> id / service_name / route(/proxy/*) / method(GET|POST)
#       / upstream_url / timeout_sec / retry_count / reuse_upstream(bool)
#       / upstream_name / status(draft|published|offline) / scope{env,instance}
#       / last_modified_by / last_modified_at / published_at / trace
# ============================================================"""


def upstreams_view(path: Path = UPSTREAMS_YAML) -> list[dict]:
    """登记表 → 前端列表口径（camelCase）。"""
    return [_px_view(p) for p in load_upstreams(path)]


def load_upstreams(path: Path = UPSTREAMS_YAML) -> list[dict]:
    """读代理登记表；文件不存在＝空表（首发前正常态），坏结构即报错（权威源不静默）。"""
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    proxies = data.get("proxies") or []
    if not isinstance(proxies, list):
        raise OpsConfigError(500, errors.BAD_UPSTREAMS, "upstreams.yaml proxies 须为列表（文件被手编坏）")
    return proxies


def _save_upstreams(path: Path, proxies: list[dict]) -> None:
    """原子写（tmp＋rename，同 appkeys 口径）；写后 yaml 回读校验。"""
    doc = {"meta": {"updated": _now_iso(), "updated_by": "console"}, "proxies": proxies}
    text = _UPSTREAMS_HEADER + "\n" + yaml.safe_dump(doc, sort_keys=False,
                                                     allow_unicode=True, default_flow_style=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        parsed = yaml.safe_load(tmp.read_text(encoding="utf-8")) or {}
        if not isinstance(parsed.get("proxies"), list):
            raise OpsConfigError(500, errors.BAD_UPSTREAMS, "校验失败：proxies 回读非列表")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _px_view(p: dict) -> dict:
    """登记条目 → 前端列表口径（camelCase，与 admin/upstreams.js 既有形状对齐）。"""
    scope = p.get("scope") or {}
    return {
        "id": p.get("id"), "serviceName": p.get("service_name"), "routePath": p.get("route"),
        "method": p.get("method", "GET"), "upstreamUrl": p.get("upstream_url"),
        "timeoutSec": p.get("timeout_sec", 5), "retryCount": p.get("retry_count", 2),
        "reuseUpstream": bool(p.get("reuse_upstream")), "upstreamName": p.get("upstream_name"),
        "status": p.get("status"),
        "env": scope.get("env", DEFAULT_ENV), "instance": scope.get("instance", DEFAULT_INSTANCE),
        "releaseMode": describe_scope(scope),   # 读侧人话口径（两选命中→先测试/直接上生产）
        "lastModifiedBy": p.get("last_modified_by"), "lastModifiedAt": p.get("last_modified_at"),
        "publishedAt": p.get("published_at"), "gitShortHash": None,   # git 留痕占位：不伪造 hash
        "callCount7d": None,   # 未接入计量（接入后由审计聚合回填，不造数）
    }


def proxy_service_map(path: Path = UPSTREAMS_YAML) -> dict[str, dict]:
    """已发布代理 → 服务命名空间条目（供签发授权/凭证视图与发布台服务视图复用）。

    代理是「整路由授权」的服务形态：kind=proxy、fields=[]（无字段投影语义）。
    只取 published——草稿/下线不对外，不能签发授权（避免发了调不通的凭证）。
    服务名/路由按 upstreams.yaml 真源解析，视图层不再显示裸 id。
    """
    out: dict[str, dict] = {}
    for p in load_upstreams(path):
        if not isinstance(p, dict) or p.get("status") != "published":
            continue
        pid = str(p.get("id") or "")
        if not pid:
            continue
        out[pid] = {
            "service_id": pid,
            "id": pid,
            "name": str(p.get("service_name") or pid),
            "route": str(p.get("route") or ""),
            "method": str(p.get("method") or "POST"),
            "kind": "proxy",
            "fields": [],
            "status": "published",
        }
    return out


def _proxy_svc_view(p: dict) -> dict:
    """代理条目 → 发布台服务列表口径（与 _svc_view 同键；kind=proxy、fields=[]）。

    C1（2026-10-20 可用性改版）：GET /v1/admin/publish 合并 upstreams.yaml 已发布
    条目，签发页据此把代理作为「整路由授权」的可选服务；不复用 _svc_view 是因为
    代理没有 backend/kv/field_space 语义，避免伪字段。
    """
    scope = p.get("scope") or {}
    return {
        "id": p.get("id"), "serviceId": p.get("id"),
        "serviceName": p.get("service_name"), "routePath": p.get("route"),
        "method": p.get("method", "POST"), "key": None,
        "fields": [], "fieldSpace": "proxy",
        "status": p.get("status"), "datasource": "proxy",
        "kind": "proxy",
        "hbaseTable": None, "hbaseCf": None, "keyType": None, "loadSource": None,
        "env": scope.get("env", DEFAULT_ENV),
        "instance": scope.get("instance", DEFAULT_INSTANCE),
        "releaseMode": describe_scope(scope),
        "lastModifiedBy": p.get("last_modified_by"),
        "lastModifiedAt": p.get("last_modified_at"),
        "publishedAt": p.get("published_at"), "gitShortHash": None,
    }


def delete_upstream_draft(path: Path, pid: str, *, operator: str = "console",
                          now: datetime | None = None) -> tuple[dict, dict]:
    """删除一条草稿（仅 draft；已发布/下线须先下线再删，防误删线上路由）。"""
    now = now or datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    proxies = load_upstreams(path)
    prev = next((p for p in proxies if p.get("id") == pid), None)
    if prev is None:
        raise OpsConfigError(404, errors.PROXY_NOT_FOUND, f"代理 {pid} 未在登记表")
    if prev.get("status") != "draft":
        raise OpsConfigError(409, errors.INVALID_STATUS,
                             "仅草稿可删除；已发布/已下线条目请先下线再删除（防误删线上路由）")
    proxies.remove(prev)
    _save_upstreams(path, proxies)
    removed = dict(prev)
    removed["last_modified_by"] = operator
    removed["last_modified_at"] = iso
    return _px_view(removed), _px_view(prev)


def save_upstream(path: Path, *, service_name: str, route: str, method: str = "GET",
                  upstream_url: str = "", timeout_sec: int = 5, retry_count: int = 2,
                  reuse_upstream: bool = False, upstream_name: str = "",
                  status: str = "published", env: str = "", instance: str = "",
                  operator: str = "console", now: datetime | None = None,
                  release_mode: str = "") -> tuple[dict, dict | None]:
    """存草稿/发布一条代理配置（同 route＝同 id 更新；返回 (新条目, 前版本) 视图）。"""
    service_name = (service_name or "").strip()
    route = (route or "").strip()
    if not service_name:
        raise OpsConfigError(400, errors.INVALID_NAME, "服务名必填")
    if not ROUTE_PX_RE.fullmatch(route):
        raise OpsConfigError(400, errors.INVALID_ROUTE, "路由须形如 /proxy/partner/xxx（小写字母数字与连字符）")
    if method not in ("GET", "POST"):
        raise OpsConfigError(400, errors.INVALID_METHOD, "method 须为 GET 或 POST")
    if status not in PX_STATUS[:2]:
        raise OpsConfigError(400, errors.INVALID_STATUS, "status 须为 draft 或 published")
    url = (upstream_url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise OpsConfigError(400, errors.INVALID_UPSTREAM_URL, "上游 URL 须为合法 http(s) 地址")
    if timeout_sec is None or int(timeout_sec) <= 0:
        raise OpsConfigError(400, errors.INVALID_TIMEOUT, "超时秒数须大于 0")
    if retry_count is None or int(retry_count) < 0:
        raise OpsConfigError(400, errors.INVALID_RETRY, "重试次数不能为负")
    if reuse_upstream and not UPSTREAM_NAME_RE.fullmatch((upstream_name or "").strip()):
        raise OpsConfigError(400, errors.INVALID_UPSTREAM_NAME, "可复用 Upstream 名称非法（小写 slug，2~32 位）")

    now = now or datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    pid = _slug_from_route(route)
    proxies = load_upstreams(path)
    prev = next((p for p in proxies if p.get("id") == pid), None)
    entry = {
        "id": pid, "service_name": service_name, "route": route, "method": method,
        "upstream_url": url, "timeout_sec": int(timeout_sec), "retry_count": int(retry_count),
        "reuse_upstream": bool(reuse_upstream),
        "upstream_name": (upstream_name or "").strip() or (pid + "-up"),
        "status": status,
        "scope": _resolve_scope(env, instance, release_mode),
        "last_modified_by": operator, "last_modified_at": iso,
        "published_at": iso if status == "published" else (prev or {}).get("published_at"),
        "trace": {"action": status, "by": operator, "at": iso, "commit": None},
    }
    if prev is not None:
        proxies[proxies.index(prev)] = entry
    else:
        proxies.append(entry)
    _save_upstreams(path, proxies)
    return _px_view(entry), (_px_view(prev) if prev else None)


def offline_upstream(path: Path, pid: str, *, operator: str = "console",
                     now: datetime | None = None) -> tuple[dict, dict]:
    """下线一条已发布代理（published → offline；草稿无线上语义，拒绝下线）。"""
    now = now or datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    proxies = load_upstreams(path)
    prev = next((p for p in proxies if p.get("id") == pid), None)
    if prev is None:
        raise OpsConfigError(404, errors.PROXY_NOT_FOUND, f"代理 {pid} 未在登记表")
    if prev.get("status") == "offline":
        raise OpsConfigError(409, errors.ALREADY_OFFLINE, "该代理已是下线状态")
    if prev.get("status") == "draft":
        raise OpsConfigError(409, errors.DRAFT_NOT_OFFLINE, "草稿无线上语义，直接覆盖发布即可（不支持下线草稿）")
    entry = dict(prev)
    entry.update({"status": "offline", "last_modified_by": operator, "last_modified_at": iso,
                  "trace": {"action": "offline", "by": operator, "at": iso, "commit": None}})
    proxies[proxies.index(prev)] = entry
    _save_upstreams(path, proxies)
    return _px_view(entry), _px_view(prev)


# ---------- 发布台目录树（真源＝字段字典 CSV×services.yaml） ----------

def field_domains(csv_path: Path | None = None) -> list[dict]:
    """字段字典 CSV → 业务域分组树（真源；usr_id 为键列不入出参字段表）。

    每域 {id,name,desc,fields:[{name,cnName,type,sample,desc}]}；sample 置空＝示例值
    由运营者按测试前置门禁填写（真实口径，不预填造数）；cnName＝字段字典 cn_name
    （P1-10 差量·SPEC B3「字段双列：中文名＋列名」，缺名如实标「待补」不编造）；
    desc＝cn_name（敏感级）。
    """
    csv_path = csv_path if csv_path is not None else resolve_schema_csv()
    if not csv_path.is_file():
        return []
    import csv as _csv
    with csv_path.open(encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
    buckets: dict[str, dict] = {}
    for r in rows:
        fname = (r.get("字段名") or "").strip()
        if not fname or fname == "usr_id":   # 键列＝入参，不是出参候选
            continue
        fam = next((f for f in _FIELD_FAMILIES if fname.startswith(f[2])), None)
        key = fam[0] if fam else "misc"
        if key not in buckets:
            cn = fam[1] if fam else "其他"
            prefix = fam[2] + "*" if fam else "（同步链路元数据列）"
            buckets[key] = {"id": key, "name": cn + "域", "desc": f"字段字典前缀 {prefix}",
                            "fields": []}
        cn_name = (r.get("cn_name") or "").strip() or "待补"
        buckets[key]["fields"].append({
            "name": fname,
            "cnName": cn_name,   # 中文名（字段字典 cn_name；缺失如实「待补」）
            "type": (r.get("数据类型") or "").strip() or "unknown",
            "sample": "",
            "desc": f"{cn_name}"
                    f"（{(r.get('敏感级') or '').strip() or '未分级'}）",
        })
    order = [f[0] for f in _FIELD_FAMILIES] + ["misc"]
    for b in buckets.values():   # 域行数说明（真实计数，树节点直显）
        b["desc"] += f"，{len(b['fields'])} 列"
    return [buckets[k] for k in order if k in buckets]


def dict_columns() -> set[str] | None:
    """字段字典真源列集（宽表域 297 列）；CSV 不可读 → None（调用方据此跳过校验，不误挡发布）。"""
    path = resolve_schema_csv()
    if not path.is_file():
        return None
    import csv as _csv
    with path.open(encoding="utf-8", newline="") as fh:
        return {row["字段名"] for row in _csv.DictReader(fh) if row.get("字段名")}
def build_datasource_view(meta: dict, dict_cols: dict[str, dict] | None = None,
                          *, rowkey_samples: dict[str, str] | None = None,
                          sample_key_note: str = "", now: datetime | None = None,
                          tables_meta: dict[str, dict] | None = None) -> dict:
    """AC-4 数据源发现响应体（**契约对齐 M2-5 适配层** admin/publish-hbase-adapter.js）。

    rowkey_samples（P0-1）：{表全名: 真实 rowkey}——由调用方（admin_api）经既有
    HBase 连接层 scan(limit=1) 实取（见 collect_rowkey_samples）；缺省/空＝示例键
    留空＋sample_key_note 如实标注原因，绝不回退造演示键。

    HBase meta 只到 namespace/表/列族（M2-5 真容器实测：HBase meta **不提供列级元数据**）；
    **列（qualifier）来自字段字典 CSV**——这正是 AC-4「HBase meta 与字段字典 CSV
    双源校验」的另一源。故目录树每张表下按列族展开，列清单＝该表命中的表级字典
    sync/schema_<表名>.csv（2026-09-29 业务化测试表，各表各的中文注释），缺表级
    CSV 回落宽表 297 列（demo 表零变化；键列 usr_id 不入出参字段表），
    逐列带 cf/cnName/type/sensitive/sample。

    响应形状（M2-5 契约）：
      {checkedAt, dualSource:{status,message,mismatches[]},
       domains:[{id,name,desc,tables:[{namespace,table,keyTypes[],keySamples{},
         families:[{name,columns:[{name,cnName,type,sensitive,sample,cf}]}]}]}]}
    dualSource.status：ok＝两源一致；mismatch＝HBase 有字典外列（拦截）。
    HBase 不可达由调用方（admin_api）如实回 503，不在本纯函数处理。
    B5：表对象带 tableCnName/tableDesc（真源 sync/tables_meta.yaml；缺登记＝空串，
    如实不猜）——向导①表卡片标题与服务名自动建议依赖它。
    """
    dict_cols = dict_cols if dict_cols is not None else load_field_dict()
    tables_meta = tables_meta if tables_meta is not None else load_tables_meta()
    # 多表字典（2026-09-29 业务化测试表）：每表优先 sync/schema_<表名>.csv——业务表
    # 用自己的 cn_name/业务口径展示；缺表级 CSV（如 demo oneserve_demo）回落宽表
    # 297 字典，行为零变化。双源校验字典侧＝回落字典 ∪ 各表命中字典（否则新业务表
    # 的 HBase 样本列会被误判为字典外列 → dualSource 假 mismatch）。
    per_table: dict[tuple[str, str], dict[str, dict]] = {}
    merged = dict(dict_cols)
    for ns in (meta.get("namespaces") or {}):
        for tbl in meta["namespaces"][ns]:
            path = resolve_schema_csv(tbl)
            if path.name != SCHEMA_CSV_NAME:          # ≠宽表文件名 ＝ 命中表级字典
                d = load_field_dict(path)
                per_table[(ns, tbl)] = d
                merged.update(d)
    verify = verify_datasource(meta, merged)
    checked_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    def _columns_of(d: dict) -> list[dict]:
        """字段字典 → 列项（键列 usr_id 不入出参字段表）。"""
        return [{
            "name": name, "cnName": row.get("cnName") or "待补",
            "type": row.get("type") or "unknown",
            "sensitive": row.get("sensitivity") or "未分级",
            "sample": "",           # 示例值由运营者按测试前置门禁填写（不预填造数）
            "desc": row.get("definition") or "",
        } for name, row in d.items() if name != "usr_id"]

    key_types = sorted(HBASE_KEY_TYPES)
    rowkey_samples = rowkey_samples or {}
    # 示例键＝发现端点 scan(limit=1) 实取的真实 rowkey（P0-1）；取不到＝留空＋标注，
    # 绝不回退造演示键；usr_id 明文示例同样不编造（平台零明文口径）。
    def _key_samples(full: str) -> dict[str, str]:
        ks = {kt: "" for kt in key_types}
        real = str(rowkey_samples.get(full) or "").strip()
        if real:
            ks["phone_sm3"] = real
        return ks

    def _families(fams: dict, cols: list[dict]) -> list[dict]:
        """列族（HBase meta 真源）× 列清单（字段字典真源）→ M2-5 契约 families[]。

        每列带 sampleSeen＝该列是否出现在 HBase 样本行里（真库实见）；两源逐列可见，
        但**候选列清单以字段字典为准**（HBase meta 无列级 API，M2-5 真容器实测）；
        cols＝该表命中的表级字典列项（缺表级 CSV 回落宽表 297 列）。
        """
        out = []
        for cf in sorted(fams or {}):
            seen = set(fams.get(cf) or [])
            out.append({"name": cf,
                        "columns": [dict(c, cf=cf, sampleSeen=c["name"] in seen)
                                    for c in cols]})
        return out

    domains = []
    first_real = ""
    for ns in sorted(meta.get("namespaces") or {}):
        tables = []
        for tbl in sorted(meta["namespaces"][ns]):
            full = f"{ns}:{tbl}" if ns != "default" else tbl
            if not first_real and str(rowkey_samples.get(full) or "").strip():
                first_real = str(rowkey_samples[full]).strip()
            tmeta = tables_meta.get(full) or {}   # B5：缺登记＝空 dict → 渲染空串（如实，不猜）
            tables.append({
                "id": full, "namespace": ns, "table": tbl, "path": full,
                "tableCnName": tmeta.get("cn_name", ""),
                "tableDesc": tmeta.get("desc", ""),
                "keyTypes": key_types, "keySamples": _key_samples(full),
                "families": _families((meta["namespaces"][ns][tbl] or {}).get("families"),
                                      _columns_of(per_table.get((ns, tbl), dict_cols))),
            })
        domains.append({"id": ns, "name": f"{ns} namespace",
                        "desc": f"HBase namespace {ns}（表 {len(tables)} 张）",
                        "tables": tables})
    return {
        "checkedAt": checked_at,
        "datasource": "hbase",
        "hbase": {"reachable": True, "tableCount": int(meta.get("table_count") or 0)},
        "dict": {"source": f"sync/{SCHEMA_CSV_NAME}", "columnCount": len(dict_cols)},
        "dualSource": {
            "status": "mismatch" if (verify["unknown_columns"] or not verify["dict_present"]) else "ok",
            "message": ("HBase meta 与字段字典一致（字典外列 0）" if verify["ok"] else
                        f"HBase meta 与字段字典不一致：字典外列 {len(verify['unknown_columns'])} 个"
                        "（AC-4 拦截依据）"),
            "mismatches": [{"field": c,
                            "reason": "HBase 有而字段字典 297 列无此列"}
                           for c in verify["unknown_columns"]],
            "unknownColumns": verify["unknown_columns"],
            "dictNotInMeta": verify["dict_not_in_meta"],
            "dictPresent": verify["dict_present"],
            "note": ("双源校验：HBase meta 列须全部 ∈ 字段字典 297 列，否则 status=mismatch 并拦截发布；"
                     "HBase meta 无列级 API（列来自字段字典 CSV），字典列未见于 HBase 样本"
                     "＝「样本未见」而非「不存在」，如实标注不谎称。"),
        },
        "domains": domains,
        "key": {"name": "usr_id（phone_sm3）", "sampleKey": first_real,
                "sampleUsrId": "", "readonly": False,
                "sampleKeySource": "hbase-scan" if first_real else "none",
                "note": (sample_key_note or
                         ("示例键＝真库 scan(limit=1) 实取 rowkey" if first_real
                          else "示例键留空：未提供真实 scan 样本，不造演示键（可粘贴真实 rowkey）"))
                        + "；rowkey 键模式由所选表登记口径决定（phone_sm3 / usr_id，均默认宽松）"},
    }


# ---------- P0-1：示例键真源（发现端点逐表 scan 采样；client 注入可测） ----------

def collect_rowkey_samples(meta: dict, client) -> tuple[dict[str, str], str]:
    """每表 scan(limit=1) 实取一个真实 rowkey（P0-1：示例键不得造假）。

    client 鸭子类型注入既有 HBase 连接层（hbase_executor.HBaseClient.scan），本模块
    保持零 HBase import（分层纪律同文件头注）。返回 (rowkey_samples, note)：
      - rowkey_samples：{表全名: rowkey}，只含真扫到的键；
      - note：如实标注来源/原因——取到＝scan 口径＋计数；空库/无表/单表失败＝留空原因。
    单表扫描失败不拖垮整个发现（原因记入 note），绝不编造键顶位。
    """
    samples: dict[str, str] = {}
    scan_errors: list[str] = []   # 命名避开 errors 模块（test_error_envelope AST 扫描口径）
    scanned = 0
    for ns in sorted(meta.get("namespaces") or {}):
        for tbl in sorted(meta["namespaces"][ns]):
            full = f"{ns}:{tbl}" if ns != "default" else tbl
            scanned += 1
            try:
                rows = client.scan(full, limit=1)
            except Exception as exc:  # noqa: BLE001 —— 单表失败＝记录原因，不造假键
                scan_errors.append(f"{full}: {str(exc)[:80]}")
                continue
            key = str((rows[0] or {}).get("row") or "").strip() if rows else ""
            if key:
                samples[full] = key
    if samples:
        note = f"示例键来自真库 scan(limit=1)：{len(samples)}/{scanned} 表取到"
        if scan_errors:   # 部分成功也如实带上未取到的原因（不静默吞错）
            note += "；未取到——" + "；".join(scan_errors[:3])
    elif scan_errors:
        note = "示例键留空：scan 未取到行——" + "；".join(scan_errors[:3])
    elif scanned == 0:
        note = "示例键留空：HBase 无表（meta 0 表），无键可取"
    else:
        note = "示例键留空：空库（scan(limit=1) 未取到任何行），可粘贴真实 rowkey 试查"
    return samples, note


# ---------- AC-5 headless 自检断言（后端 selftest API；M2-5 契约） ----------

def hbase_selftest_assertions(meta: dict | None, dict_cols: dict[str, dict] | None = None,
                              *, reachable: bool, reach_error: str = "",
                              services: dict | None = None) -> list[dict]:
    """后端自检断言集（AC-5 headless 口径）：每项 {id,name,ok,detail}。

    诚实口径：HBase 不可达 → 对应断言 ok=false（detail 如实报错），绝不因不可达而跳过
    或谎报通过；字典真源不可读同理。纯逻辑（meta/连接池状态由 admin_api 注入）。
    """
    dict_cols = dict_cols if dict_cols is not None else load_field_dict()
    out: list[dict] = []

    out.append({
        "id": "dict-present", "name": "字段字典真源可读（同步 297 列宽表）",
        "ok": len(dict_cols) == 297,
        "detail": f"CSV={SCHEMA_CSV_NAME} 列数={len(dict_cols)}（应为 297）",
    })

    out.append({
        "id": "hbase-reachable", "name": "HBase 可达（Thrift meta 可读）",
        "ok": bool(reachable),
        "detail": ("HBase 连接池就绪，meta 可读" if reachable
                   else f"HBase 不可达：{reach_error or '无连接池'}（如实标红，不回落假数据）"),
    })

    if meta is None:
        out.append({"id": "dual-source", "name": "HBase meta × 字段字典双源校验",
                    "ok": False, "detail": "HBase 不可达，双源校验未执行（不谎称一致）"})
        out.append({"id": "domain-tree", "name": "业务域目录树（表×列族）分组",
                    "ok": False, "detail": "HBase 不可达，目录树未生成"})
    else:
        verify = verify_datasource(meta, dict_cols)
        out.append({
            "id": "dual-source", "name": "HBase meta × 字段字典双源校验",
            "ok": verify["ok"],
            "detail": (f"一致（HBase 列 {len(verify['hbase_columns'])}，"
                       f"字典 {len(verify['dict_columns'])}）；字典有而 meta 未见 "
                       f"{len(verify['dict_not_in_meta'])}"
                       if verify["ok"] else
                       f"不一致：字典外列 {verify['unknown_columns'][:8]}（AC-4 拦截依据）"),
        })
        tables = sum(len(t) for t in (meta.get("namespaces") or {}).values())
        out.append({
            "id": "domain-tree", "name": "业务域目录树（namespace→表→列族）分组",
            "ok": tables >= 1,
            "detail": f"namespace {len(meta.get('namespaces') or {})} 个，表 {tables} 张",
        })

    # 发布前置校验：字典外字段必拒（写状态键之前拦下）
    try:
        validate_against_dict([{"name": "__no_such_col__", "desensitize": ""}], dict_cols)
        rejected = False
        detail = "字典外字段未被拒绝（校验失效）"
    except OpsConfigError as exc:
        rejected = exc.code == errors.FIELD_NOT_IN_DICT
        detail = f"字典外字段 → {exc.code}（AC-6 加载期校验）"
    out.append({"id": "reject-not-in-dict", "name": "发布前置校验：字典外字段必拒",
                "ok": rejected, "detail": detail})

    # 渲染器：hbase 型服务须生成通配路由（零代码可调前提）
    try:
        from app import registry_render as _rend
        probe = {"__probe__": {
            "service_id": "__probe__", "archetype": "kv_lookup",
            "field_space": sd.FIELD_SPACE_WIDE,
            "backend": {"source": "hbase",
                        "hbase": {"table": "t", "cf": "cf", "key_type": "phone_sm3"}}}}
        rendered = dict(_rend.render_query_routes(probe))
        ok_route = "routes/oneserve-query-__probe__" in rendered
        detail = ("hbase 型服务渲染出可调路由（无 routePath 条目回落 /v1/query 内部通道；"
                  "带合规 routePath 条目渲染 /v1/svc 对外出口；新增服务零代码）"
                  if ok_route else "未渲染出通配路由（AC-6 可调前提不成立）")
    except Exception as exc:  # noqa: BLE001
        ok_route, detail = False, f"渲染器自检异常：{exc}"
    out.append({"id": "route-render", "name": "渲染器为 hbase 型服务生成通配路由",
                "ok": ok_route, "detail": detail})

    # 已发布 hbase 型服务：backend/field_space 契约在场（有则查，无则如实标注无样本）
    hbase_svcs = [s for s in (services or {}).values()
                  if isinstance(s, dict)
                  and (s.get("backend") or {}).get("source") == "hbase"]
    if hbase_svcs:
        ok_fs = all(s.get("field_space") == sd.FIELD_SPACE_WIDE for s in hbase_svcs)
        out.append({"id": "field-space-wide297", "name": "hbase 型服务 field_space=wide297（ADR-0004）",
                    "ok": ok_fs,
                    "detail": f"hbase 型服务 {len(hbase_svcs)} 个，field_space 全为 wide297={ok_fs}"})
    return out


def build_console_view(services_path: Path = SERVICES_YAML,
                       csv_path: Path | None = None,
                       upstreams_path: Path = UPSTREAMS_YAML) -> dict:
    """发布台整页数据（GET /v1/admin/publish 响应体；读盘即时值）。

    datasource/key 由 services.yaml 运行时注册区真实值生成；domains 由字段字典
    真源生成；services＝console_services 写区条目＋upstreams.yaml 已发布代理
    （C1：kind=proxy、fields=[]，签发页按整路由授权选它）。
    """
    domains = field_domains(csv_path)
    registry: dict = {}
    if services_path.is_file():
        registry = yaml.safe_load(services_path.read_text(encoding="utf-8")) or {}
    svc_list = registry.get("services") or []
    key_param = {}
    for s in svc_list:
        if s.get("service_id") == "user_tag_query":
            params = s.get("params") or [{}]
            key_param = params[0] if isinstance(params, list) else {}
            break
    key_name = str(key_param.get("key_type") or "phone_sm3")
    return {
        "datasource": {
            "name": "示例宽表 · demo_src.tags_wide",
            "columnCount": sum(len(d["fields"]) for d in domains) + 1,   # 出参列＋键列
            "schemaRef": f"sync/{SCHEMA_CSV_NAME}",
            "kind": "字段字典（元数据）＋服务注册表",
            "registeredServices": len({s.get("service_id") for s in svc_list}),
        },
        "key": {
            "name": f"usr_id（{key_name}）",
            "paramName": str(key_param.get("name") or "key"),
            "sampleKey": "",
            "sampleUsrId": "",
            "readonly": False,
            "note": "示例键不预造（P0-1：历史演示键是假用户、真库无此行，已下线）；请粘贴真实键试查",
        },
        "domains": domains,
        "services": [_svc_view(e) for e in load_console_services(services_path)]
                    + [_proxy_svc_view(p) for p in load_upstreams(upstreams_path)
                       if isinstance(p, dict) and p.get("status") == "published"],
    }

