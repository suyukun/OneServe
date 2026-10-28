# -*- coding: utf-8 -*-
"""schema 漂移防护·纯逻辑（TASK-M2-3.8）。

真源＝sync/schema_usr_label_wide_297.csv（字段字典）；引用侧＝服务注册声明的字段集
（auth_matrix.yaml grants.fields ∩ services.yaml 已注册服务，见 referenced_fields）。
TODO(卡2): 297 宽表字典换源为表级 schema 元数据（装载副产品）；CSV 缓删至卡2 交接（卡5 2026-09-30）。

漂移判定口径（任务卡钉死）：只看前 3 列结构位——真源现 6 列
（attnum/字段名/数据类型/cn_name/业务口径/敏感级），比对只取「字段名」这一结构列；
cn_name/业务口径/敏感级是 M2-3.9 后加的业务列，新增业务列（列数或业务列内容变化）
不触发漂移——本模块只读字段名列，天然满足此口径。

三口径：
  - 删列：引用了但字典没有 → 隔离触发（数据可能已被上游删除，继续返回＝静默丢列）；
  - 改名：按删列处理——旧名消失进 missing（消费侧引用旧名即被隔离），新名多出不触发；
  - 增列：字典多了但无人引用 → 不触发（新增业务列是常态，不得误伤查询链路）。

「增列不泄露」双保险：added 清单只进对账报告绝不进任何响应体；执行器投影白名单
（kv_executor.project 交集语义）不变——新列即便进了 drift 报告也不在授权白名单内。

字段空间（TASK-M2-3.8 修复·假阳性根治）：授权矩阵 grants.fields 对**非宽表域**服务
是语义占位符而非宽表列名——资讯域 news_tag_query 的 tags 指「授权整个标签集」
（数据在 tags:news:* 独立键空间，值形如 title+tags 数组，见 sync/gen_news_data.py）；
B 型代理的 passthrough 指「整体透传，无字段投影语义」（auth_matrix.yaml
原文注释）。二者本就不应出现在宽表 297 列字典中，硬判「引用但字典没有」＝误隔离线上服务。

故引入**字段空间契约**（services.yaml 的 field_space，可机检、非黑名单）：
  - wide297（缺省）：引用集参与宽表 297 列删列对账；
  - news / proxy：独立字段空间，对账结果＝STATE_NOT_APPLICABLE（如实：不适用，
    既非 ok 也非 quarantined；绝不把「不适用」混进「健康」）；引用集置空、不比对。
缺省 wide297＝向后兼容：存量未声明 field_space 的服务仍按宽表参与（防漏判）。

自动化口径（如实标注，不得假称已生效）：**当前隔离为手工 CLI**
（python3 scripts/reconcile_drift.py）；AC-8-b 所称「自动转 quarantined」的
自动触发器（compose 定时/cron/流水线后置钩子）属 M2 生产化，本阶段未实现。

设计约束（分层纪律）：本模块零第三方依赖（标准库 json 而已），不 import
fastapi/redis/yaml——Redis 读写通道在调用方（kv_executor/hbase_service 薄胶水＋
scripts/reconcile_drift.py）。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from app import errors

# ---------- 键名与状态值编解码 ----------

DRIFT_KEY_PREFIX = "sync:drift"          # 键空间前缀（sync:drift:<service_id>；卡4：版本后缀段已删）
STATE_QUARANTINED = "quarantined"        # 隔离态（missing 非空）
STATE_OK = "ok"                          # 正常态（无删列；增列不影响）
STATE_NOT_APPLICABLE = "not_applicable"  # 不适用态（非宽表字段空间：不参与漂移判定）

# 字段空间（services.yaml field_space；缺省 wide297 向后兼容）
FIELD_SPACE_WIDE = "wide297"             # 宽表 297 列域——参与删列对账
FIELD_SPACE_NEWS = "news"                # 资讯域（tags:news 独立键空间）——不适用
FIELD_SPACE_PROXY = "proxy"              # B 型代理（整体透传，无字段投影语义）——不适用
# 2026-09-29 业务测试表三张（字典 sync/schema_<表名>.csv）：先入册为已知字段空间——
# 漂移判定暂按「不适用」如实处理（各表字典比对待多表字典接入后开通；不冒充 ok、不误隔离）。
FIELD_SPACE_USR_BASIC = "usr_basic_info"
FIELD_SPACE_USR_ASSET = "usr_asset_tag"
FIELD_SPACE_CARD_ACCT = "card_acct_summary"
FIELD_SPACES = (FIELD_SPACE_WIDE, FIELD_SPACE_NEWS, FIELD_SPACE_PROXY,
                FIELD_SPACE_USR_BASIC, FIELD_SPACE_USR_ASSET, FIELD_SPACE_CARD_ACCT)
FIELD_SPACE_DEFAULT = FIELD_SPACE_WIDE   # 缺省＝宽表域（向后兼容：存量服务零改动，防漏判）


def field_space_of(service: dict) -> str:
    """取服务声明字段空间；缺省 wide297（向后兼容）。未知值防御式回落 wide297（宁判不误放）。"""
    raw = str(service.get("field_space") or FIELD_SPACE_DEFAULT).strip()
    return raw if raw in FIELD_SPACES else FIELD_SPACE_DEFAULT


def drift_state_key(service_id: str) -> str:
    """漂移状态键名：sync:drift:<service_id>（卡4：注册表单键化，版本后缀段已删）。"""
    return f"{DRIFT_KEY_PREFIX}:{service_id}"


def encode_state(state: dict[str, Any]) -> str:
    """状态值序列化（紧凑 JSON；确定性输出便于 diff 与断言）。

    形态：{"state":"quarantined","missing":[...]} / {"state":"ok"}。
    """
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def decode_state(raw: str | bytes | None) -> dict[str, Any] | None:
    """状态值反序列化；None（键不存在）→ None；脏值防御式回落 None（fail-open 由调用方定夺）。"""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def make_quarantined(missing: Iterable[str]) -> dict[str, Any]:
    """隔离态构造：missing 排序去重（幂等——同输入同输出，重复跑查四结果稳定）。"""
    return {"state": STATE_QUARANTINED, "missing": sorted(set(missing))}


def make_ok() -> dict[str, Any]:
    """正常态构造。"""
    return {"state": STATE_OK}


def make_not_applicable(field_space: str, reason: str = "") -> dict[str, Any]:
    """不适用态构造（非宽表字段空间）：如实标注不参与宽表漂移判定。

    与 ok 严格区分：ok＝已比对且健康；not_applicable＝本就不该比对（资讯/代理域
    字段不属于宽表 297 列）。混同会把「未检查」粉饰成「已检查且健康」。
    """
    state: dict[str, Any] = {"state": STATE_NOT_APPLICABLE, "field_space": field_space}
    if reason:
        state["reason"] = reason
    return state


def is_not_applicable(state: dict[str, Any] | None) -> bool:
    """是否不适用态（缺 state 键/未知态一律按否处理）。"""
    return bool(state) and state.get("state") == STATE_NOT_APPLICABLE


def is_quarantined(state: dict[str, Any] | None) -> bool:
    """是否隔离态（缺 state 键/未知态一律按不隔离处理——fail-open 口径，不误伤）。"""
    return bool(state) and state.get("state") == STATE_QUARANTINED


# ---------- 解除流程（漂移修复后的恢复路径） ----------

def recovery_decision(current: Any, restored: bool, present_cols: Iterable[str] = ()) -> dict[str, Any]:
    """解除判定（纯函数）：漂移修复后该把状态键转回 ok 还是维持隔离。

    入参：
      current      当前状态键值（raw str/bytes）或已解码 dict（None＝无键）；
      restored     显式声明「字典真源已修复」（reconcile --recover --restored）；
      present_cols 当前字典真源字段集（用于「missing 是否已回归」的机器复核）。
    返回 {"action","state","reason","still_missing"}：
      action=write：写 state（ok 解除 / quarantined 维持）；action=none：原样不动。

    两条解除路径（对账脚本子命令）：
      - 显式 --restored（人确认误删已恢复）：清键回 ok（覆盖写 ok，幂等）；
      - 无条件 --recover（未声明 restored）：只当 missing 字段全部重新出现在真源时
        才转 ok（逐字段机器复核，不靠人嘴），否则维持 quarantined（fail-closed）。
    无键（None）→ 不写（补 ok 键制造「已对账」假象，反而掩盖真实巡检缺口）。
    """
    if isinstance(current, (str, bytes)):
        state = decode_state(current)
    elif isinstance(current, dict):
        state = current
    else:
        state = None
    if state is None:
        return {"action": "none", "state": None, "reason": "no_state_key", "still_missing": []}

    missing = [str(c) for c in (state.get("missing") or [])]
    if not is_quarantined(state):
        return {"action": "none", "state": state, "reason": "not_quarantined", "still_missing": []}
    if restored:
        return {"action": "write", "state": make_ok(), "reason": "restored_confirmed", "still_missing": []}

    present = {str(c) for c in present_cols}
    still = sorted(c for c in missing if c not in present)
    if still:
        return {"action": "write", "state": make_quarantined(still),
                "reason": "missing_columns_remain", "still_missing": still}
    return {"action": "write", "state": make_ok(), "reason": "all_missing_back", "still_missing": []}


# ---------- 漂移计算 ----------

def compute_drift(referenced_fields: Iterable[str],
                  dict_columns: Iterable[str]) -> dict[str, list[str]]:
    """比对引用字段集 vs 字典真源字段集 → {missing, added}。

    - missing＝引用了但字典没有（删列/改名后的旧名 → 隔离触发）；
    - added＝字典有但无人引用（增列 → 仅记录，不触发隔离）；
    两侧均排序去重（确定性输出）。
    """
    ref = set(referenced_fields)
    cols = set(dict_columns)
    return {"missing": sorted(ref - cols), "added": sorted(cols - ref)}


# ---------- 503 错误体 ----------

QUARANTINE_CODE = errors.SCHEMA_DRIFT_QUARANTINED  # 码名在 app/errors.py（单一事实来源）


def quarantine_error_body(missing: Iterable[str]) -> dict[str, Any]:
    """隔离期查询的 503 错误体：机读码＋人话（丢失列清单＋「联系数据值班」指引）。

    error/message 两键与 TagQueryError 端点层映射一致（main._run_query），
    traceId 由 trace_middleware 统一注入，此处不造。
    """
    cols = sorted(set(missing))
    listed = "、".join(cols) if cols else "（清单见 sync:drift:* 状态键）"
    message = (f"字段字典 schema 漂移：服务引用的字段已从字典真源删除（{listed}）。"
               "该服务已隔离，暂停查询以避免静默丢列返回脏数据"
               "（隔离状态键由对账脚本写入；当前为手工 CLI，自动触发器归 M2 生产化）。"
               "请联系数据值班核对字段字典（sync/schema_usr_label_wide_297.csv）："
               "确认删除是有意的则更新服务引用并重新发布，属误删则恢复字典；"
               "修正后重跑 python3 scripts/reconcile_drift.py 即恢复 ok（无需重启服务）。")
    return {"error": QUARANTINE_CODE, "missing_fields": cols, "message": message}


# ---------- 引用集提取（纯函数：文件读取归调用方） ----------

def field_space_by_service(services: list[dict]) -> dict[str, str]:
    """各 service_id 的字段空间（services.yaml field_space；缺省 wide297）。

    卡4：注册表 service_id 单键（复合键/latest 已删，ADR-0008 §Decision.2）。
    """
    return {s["service_id"]: field_space_of(s) for s in services}


def referenced_fields(services: list[dict], matrix_rows: list[dict]) -> dict[str, set]:
    """从注册配置＋授权矩阵提取「各服务注册引用的字段集」（仅宽表字段空间）。

    键＝service_id（卡4：与 ServiceRegistry 单键同构）；值＝该服务全部租户
    授权字段的并集（任一租户引用即算引用——隔离按服务粒度，不按租户）。

    字段空间过滤（TASK-M2-3.8 修复）：只有 field_space=wide297（缺省）的服务参与宽表
    297 列引用集；news/proxy 域字段是语义占位符（非宽表列名），引用集恒为空——其字段名
    不得进引用集，否则会对宽表字典产生假「删列」。非宽表键仍保留在返回 dict 中
    （值为空集），对账侧据 field_space 记 not_applicable（语义如实）。

    grants.service 定位沿 kv_executor 口径（卡4 AC-3a）：裸 service_id 一条路径；
    未注册服务的 grant 忽略（注册表是唯一入口）。
    """
    spaces = field_space_by_service(services)
    refs: dict[str, set] = {sid: set() for sid in spaces}
    for row in matrix_rows:
        for g in row.get("grants", []) or []:
            sid = str(g.get("service", ""))
            if sid not in refs:
                continue
            if spaces[sid] != FIELD_SPACE_WIDE:   # 非宽表域：语义占位符不入引用集
                continue
            refs[sid].update(str(f) for f in (g.get("fields") or []))
    return refs