# -*- coding: utf-8 -*-
"""A 型 KV 执行器（task-003）：services.yaml 配置驱动的一次标签点查。

实现 SPEC §3.2「一次点查的完整路径」的服务层部分（步骤 3~5）：
  读 services.yaml → 经 meta 指针键解析当前线上版本 → 取数据键 →
  按 sync/loader.py 的存储格式（非空标签紧凑 JSON）反序列化 →
  按授权矩阵字段投影 → 按脱敏规则脱敏 → 带 data_as_of 返回。

设计约束：
  - 本模块不 import FastAPI/redis——KV 访问经 store 参数注入（鸭子类型：
    需要异步 get(key)/hget(key, field) 两个方法），单元测试用字典假实现即可全路径覆盖；
  - 配置与授权矩阵只认 services.yaml / auth_matrix.yaml 两份文件，代码零服务名硬编码；
  - guard 超时（timeout_ms）包住整链 KV 读；max_items_per_call 由端点层（main.py）强制。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import yaml

from app import errors
from app import schema_drift

SERVICE_DIR = Path(__file__).resolve().parent.parent          # service/
SERVICES_YAML = SERVICE_DIR / "services.yaml"
AUTH_MATRIX_YAML = SERVICE_DIR / "auth_matrix.yaml"


class TagQueryError(Exception):
    """执行器可控错误：携带 HTTP 状态码与机读错误码（端点层直接映射响应）。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------- 配置加载（启动时一次，路径可注入便于测试） ----------

class ServiceRegistry(dict):
    """服务注册索引（卡4 ADR-0008 §Decision.2：service_id 单键；服务修订号机制已删）。

    - services["user_tag_query"] → 该服务唯一条目；同 service_id 重复注册启动即失败
      （卡4 NC-1=a：删的是版本机制，不是查重保护——更新该服务走发布覆盖，不追加条目）；
    - 「版本」概念在注册表只剩两类（门槛稿 §〇三分法）：URL /v1/ API 大版本与表数据
      版本（tabver，backend.load.version/Redis {version} 插槽），均不经本索引。
    """


def load_services(path: Path = SERVICES_YAML) -> ServiceRegistry:
    """读服务注册配置；service_id 单键建索引（卡4：重复 id 即拒；复合键/latest 已删）。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    registry = ServiceRegistry()
    for s in data.get("services", []):
        sid = s["service_id"]
        if sid in registry:
            raise RuntimeError(
                f"{path.name}: 服务 {sid} 重复注册"
                "（单键注册表禁止同 id 双条目；更新该服务走发布覆盖，不追加）")
        registry[sid] = s
    if not registry:
        # 2026-09-29 空表合法态（维护者 拍板全下线）：文件在、YAML 合法、services 显式为 []
        # ＝无服务在线（所有查询 404），与 ops_config 发布管线同款语义；坏 YAML/文件缺失
        # 仍由上方 safe_load/read 抛错（fail-fast 不放松）。空注册表返回后：渲染器出零服务
        # 路由、查询路径查不到服务如实 404。
        return registry
    return registry


def load_auth_matrix(path: Path = AUTH_MATRIX_YAML) -> list[dict]:
    """读授权矩阵（静态版）：[{tenant, appkey, oa_ticket, grants:[{service, fields}]}]。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    matrix = data.get("matrix", [])
    # 2026-09-29 空矩阵合法态（全下线终态）：无授权＝所有调用如实 403/409，
    # 与 services:[] 同款语义；坏 YAML/文件缺失仍由上方 safe_load/read 抛错。
    return matrix


def tenant_grant(matrix: list[dict], service_id: str, *, appkey: str | None = None,
                 tenant: str | None = None) -> dict:
    """按 AppKey（正式通道）或租户名（演示通道）取授权条目。

    卡4（AC-3a）：授权定位只剩 grants.service == service_id 一条路径
    （服务修订号 @vN 维度已删；未授权仍 403，错误码不变）。
    返回 {tenant, oa_ticket, fields:set}；租户未登记或未授权该服务 → 403。
    """
    for row in matrix:
        if (appkey is not None and row.get("appkey") == appkey) or \
           (tenant is not None and row.get("tenant") == tenant):
            grants = {g["service"]: set(g["fields"]) for g in row.get("grants", [])}
            if service_id not in grants:
                raise TagQueryError(403, errors.SERVICE_NOT_GRANTED,
                                    f"租户 {row['tenant']} 未获服务 {service_id} 授权")
            return {"tenant": row["tenant"], "oa_ticket": row.get("oa_ticket"),
                    "fields": grants[service_id]}
    raise TagQueryError(403, errors.UNKNOWN_TENANT, "调用方未在授权矩阵登记（AppKey 未绑定租户）")


# ---------- 键校验（storage_key=as_received：直存收到的键，先归一化再校验） ----------

def normalize_key(raw: str, pattern: str) -> str:
    """键归一化＋格式校验：小写后必须整体匹配 pattern。

    平台不关注键的加密算法（SM3/MD5/UUID 均可）；模式由服务注册声明（params[].pattern），
    默认宽松（任意合理字符串键），执行器只按注册处声明校验收到的键。
    """
    key = (raw or "").strip().lower()
    if not re.fullmatch(pattern, key):
        raise TagQueryError(400, errors.INVALID_KEY, f"key 须匹配 {pattern}（小写）")
    return key


# ---------- 字段投影（红线：白名单外的字段绝不进结果） ----------

def project(tags: dict[str, str], fields: set[str]) -> dict[str, str]:
    """按授权白名单投影：只保留「白名单 ∩ 该用户非空标签」；顺序按白名单稳定排序。"""
    return {name: tags[name] for name in sorted(fields) if name in tags}


# ---------- 脱敏（band_mask 型：分档输出，原值零出现） ----------

def _band_mask(raw: str, rule: dict) -> str:
    """band_mask：数值原值映射到粗档位（A/B/C…）；非数值 → unknown_out，绝不回显原值。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return str(rule.get("unknown_out", "U"))
    for band in rule.get("bands", []):
        if "max" not in band or value < float(band["max"]):   # 自下而上首条命中
            return str(band["out"])
    return str(rule.get("unknown_out", "U"))


def apply_desensitize(fields: dict[str, str], rules: dict[str, dict]) -> dict[str, str]:
    """应用脱敏规则：规则命中字段值替换为脱敏值；未知规则类型＝配置错误，拒服（fail-closed）。"""
    out = dict(fields)
    for name, rule in (rules or {}).items():
        if name not in out:
            continue                                          # 该用户此标签为空或未授权：无值可脱
        rtype = rule.get("type")
        if rtype == "band_mask":
            out[name] = _band_mask(out[name], rule)
        else:
            raise TagQueryError(500, errors.BAD_DESENSITIZE_RULE, f"字段 {name} 配置了未知脱敏类型 {rtype}")
    return out


# ---------- schema 漂移隔离闸（TASK-M2-3.8） ----------

async def check_drift_quarantine(store: Any, service: dict) -> None:
    """每请求读 sync:drift:<id>@<ver>；命中 quarantined → 503 拒绝（不读脏）。

    状态键由查四写入（scripts/reconcile_drift.py）；解除＝人工修正后重跑查四恢复
    ok，本闸随之放行（幂等，无需重启服务）。

    fail-open 口径（任务卡钉死）：Redis 不可达 / 键不存在 / 值不可解析一律放行——
    漂移防护针对「字典真源变了」这一确定信号，不因基础设施抖动误伤正常查询。
    （store.get 抛错时异常上抛至调用方 except 分支按既有降级口径处理。）
    """
    raw = await store.get(schema_drift.drift_state_key(service["service_id"]))
    state = schema_drift.decode_state(raw)          # None（无键/脏值）→ 不隔离
    if schema_drift.is_quarantined(state):
        missing = state.get("missing") or []
        raise TagQueryError(503, schema_drift.QUARANTINE_CODE,
                            schema_drift.quarantine_error_body(missing)["message"])


# ---------- 演示通道哈希（仅供演示页把手机号转成查询键，平台不作为对外口径） ----------

def sm3_hex(text: str) -> str:
    """示例哈希：SM3 摘要（gmssl 真国密；十六进制小写；国标向量 abc→66c7f0f4… 与 task-006 同口径）。

    仅演示通道/造数工具内部使用——平台不关注键的加密算法，对外不强调 SM3。
    """
    from gmssl import func as sm3_func
    from gmssl import sm3 as sm3_lib

    return sm3_lib.sm3_hash(sm3_func.bytes_to_list(text.encode("utf-8")))


# ---------- 执行器主体 ----------

async def execute(store: Any, service: dict, grant: dict, key: str) -> dict:
    """一次点查（SPEC §3.2 步骤 3~5）。

    store：KV 访问对象（异步 get/hget）；service：services.yaml 单条注册；
    grant：tenant_grant() 的授权条目；key：收到的键值（storage_key=as_received）。
    返回响应体 dict（found=false 时 fields=None，SPEC §4.3 时点诚实）。
    """
    kv = service["backend"]["kv"]
    guard = service.get("guard", {})

    key = normalize_key(key, service["params"][0]["pattern"])

    await check_drift_quarantine(store, service)   # 漂移隔离闸（3.8；fail-open）

    async def _read() -> dict:
        # 1) meta 指针解析当前线上版本（读方口径＝sync/common.read_user：先别名后数据键）
        version = await store.get(kv["alias_key"])
        if version is None:
            raise TagQueryError(503, errors.DATA_NOT_READY, "KV 无线上版本指针（数据未上线），拒绝服务（不读脏）")
        # 2) 取数据键（tags:<表>:<版本>:<键>；{sm3} 为统一插槽名）——键模板来自该服务
        #    services.yaml 注册（发布时按源表生成，见 ops_config.build_redis_entry_from_hbase），
        #    本执行器零硬编码；value＝loader 写入的非空标签紧凑 JSON
        raw = await store.get(kv["data_key"].format(version=version, sm3=key))
        # 3) 版本元信息取 data_as_of（时点诚实：found 与否都带）
        data_as_of = await store.hget(kv["version_meta_key"].format(version=version),
                                      "data_as_of")
        if raw is None:
            # 无该用户 → 对外即 null（诚实空值，不伪造空标签集）
            return {"found": False, "fields": None, "data_as_of": data_as_of,
                    "live_version": version}
        tags = json.loads(raw)                                # loader 存储格式：{标签名: 值}（非空）
        fields = project(tags, grant["fields"])               # 投影（红线：白名单外零出现）
        fields = apply_desensitize(fields, service["response"].get("desensitize"))
        return {"found": True,
                "fields": fields or None,                     # 空标签集 → null（SPEC §4.3）
                "data_as_of": data_as_of, "live_version": version}

    try:
        result = await asyncio.wait_for(_read(), timeout=guard.get("timeout_ms", 3000) / 1000)
    except asyncio.TimeoutError:
        raise TagQueryError(503, errors.GUARD_TIMEOUT,
                            f"KV 读超时（>{guard.get('timeout_ms')}ms），guard 拒绝返回") from None

    return {"service_id": service["service_id"], "key": key, "tenant": grant["tenant"],
            # 卡4 AC-1d：响应不带服务修订号 version；live_version（表数据版本）保留
            **result}
