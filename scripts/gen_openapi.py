#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# OneServe 契约生成器（P0-2 · SPEC §4.1「OpenAPI 为权威源」构建期落地）。
#
# 从四份真源生成 portal/openapi/oneserve.openapi.yaml（OpenAPI 3.1）：
#   service/services.yaml               服务注册：service_id/version/params/response/guard/audit ← 权威
#   sync/schema_usr_label_wide_297.csv  宽表字段名/类型（详情页类型列用；卡5 缓删，卡2 换源）   ← 权威
#   service/auth_matrix.yaml            授权矩阵：对外端点与授权字段白名单               ← 权威
#   configs/apisix/init_routes.py       网关路由：key-auth 认证口径（限流未挂插件，如实标注）
#   sync/schema_*.csv（表级字典）        字段中文名摘录（展示补名；缺名标「待补」）——卡2 换源：
#                                       旧源 admin/mock/schema-domains.json 已随卡5 遗留清理删除
#
# 产出：
#   portal/openapi/oneserve.openapi.yaml      契约单一事实来源（PyYAML 落 yaml；缺失时降级 JSON 同内容）
#   portal/openapi/catalog.json               目录页索引 sidecar（构建期静态文件）
#   portal/openapi/details/<service_id>.json  每服务详情数据 sidecar（详情页 fetch 用；MPA 纪律：运行时零计算）
#   （卡5 2026-09-30：P0-9 字段字典 sidecar portal/openapi/fields.json 已随 297 字典体系下线移除；
#    表级 schema 元数据就位后由卡2/卡3 决定新形态，本脚本不再生成字典类产物）
#
# 设计约束（分层）：
#   - 纯逻辑（load_*/build_* 一族，可单测）与落盘（emit_*/write_*）分离；本脚本不渲染 HTML；
#   - 详情九要素按 SPEC §4.2 P0 口径落前 5 项＋版本：标识/入参表/出参表/错误码＋解决方案/
#     报文示例/版本＋变更；缺项如实进 elements.missing，不虚报完整；
#   - 错误码逐条对照执行器真源（kv_executor/proxy_executor/main 的 TagQueryError 与
#     main.py 的 {"error": code} 响应）；网关 401 来自 init_routes key-auth 插件——不发明码；
#   - 示例 curl 键值均为真实格式：用户域键＝示例摘要（造数工具实算）、资讯域键＝同步造数编号
#     段首条 n000001、代理域键＝mock 上游演示业务键；AppKey＝授权矩阵登记的开发占位凭证。
#
# 用法：python3 scripts/gen_openapi.py（仓库根或任意目录均可；幂等重跑逐字节一致）
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "service"))   # 错误码中心（service/app/errors.py）＝码表唯一真源
sys.path.insert(0, str(ROOT / "sync"))      # 表级字典读取（sync/schema_meta.py，卡2 换源同源实现）

from app import errors   # noqa: E402  P1-2：错误码表与信封字段在此同源，不在本脚本重复定义

SERVICES_YAML = ROOT / "service" / "services.yaml"
AUTH_MATRIX_YAML = ROOT / "service" / "auth_matrix.yaml"
APISIX_ROUTES = ROOT / "configs" / "apisix" / "init_routes.py"
SCHEMA_CSV = ROOT / "sync" / "schema_usr_label_wide_297.csv"
OUT_DIR = ROOT / "portal" / "openapi"

CONTRACT_VERSION = "2026-09-23"   # 契约版本＝SPEC v1 批准版次（显式版本防静默漂移，SPEC §4.3）
PLATFORM_VERSION = "v1"           # 服务路由版本（/v1 前缀；services.yaml 每服务 version）
GATEWAY_AUTH_HEADER = "apikey"    # 网关 key-auth 请求头（init_routes.py consumers/plugins）
DEMO_APPKEY_NOTE = "oneserve-dev-appkey-001（开发占位值；正式 AppKey 走 OA 审批后由授权登记发放）"

# 业务域展示名（与 service/app/portal_api.py 目录端点同序同名，前后台一套话术）
DOMAIN_OF = {
    "user_tag_query": ("user", "用户标签域"),
    "news_tag_query": ("news", "资讯标签域"),
    "partner_proxy": ("proxy", "合作方代理域"),
    # demo_hbase_query（M2-3 hbase 型演示服务）已随 oneserve_demo 演示遗留清理删除（2026-10-08）
}
ARCHETYPE_LABEL = {
    "kv_lookup": "A 型 · KV 点查",
    "external_proxy": "B 型 · 受控代理",
}

# （卡5 2026-09-30：P0-9 字段字典前缀→业务域展示映射 DOMAIN_PREFIXES 已随 297 字典体系下线移除，
#  卡2/卡3 表级 schema 元数据自带业务口径后不再需要前缀猜测归类。）

_DEMO_PHONE = "13800138000"        # 示例键入参（明文仅构建期内存，与试用台口径一致）
_NEWS_SAMPLE_ID = "n000001"        # sync/gen_news_data.py 造数编号段首条
_PARTNER_DEMO_KEY = "PT-DEMO-0001"   # mock 上游固定演示业务键（示例口径）
_PARTNER_DEMO_PATH = "policy/PT2026-0001"

# ── 出参表真源口径：kv_executor.execute() 实际返回体（含未命中分支，tests/test_kv_executor.py 守卫）──
KV_FOUND_FIELDS = [
    {"name": "service_id", "type": "string", "cn": "服务标识", "note": "命中的服务 ID，回显定位"},
    {"name": "key", "type": "string", "cn": "查询键", "note": "请求键原样回显（上游供数键，平台不留明文）"},
    {"name": "tenant", "type": "string", "cn": "调用方租户", "note": "按 AppKey 在授权矩阵定位的租户 ID"},
    {"name": "found", "type": "boolean", "cn": "是否命中", "note": "键不在库时如实返回 false（诚实空值，不伪造空标签集）"},
    {"name": "fields", "type": "object", "cn": "返回标签集", "note": "键值对：仅授权白名单字段；命中且非空才有值，否则为 null"},
    {"name": "data_as_of", "type": "string", "cn": "数据时点", "note": "数据批次业务日期（时点诚实字段，未命中也返回）"},
    {"name": "live_version", "type": "string", "cn": "线上数据版本", "note": "当前服务的同步版本号"},
]
KV_NOT_FOUND_FIELDS = [
    {"name": "found", "type": "boolean", "cn": "是否命中", "note": "固定 false"},
    {"name": "fields", "type": "object", "cn": "返回标签集", "note": "固定 null（绝不伪造空标签集）"},
    {"name": "data_as_of", "type": "string", "cn": "数据时点", "note": "数据批次业务日期照常返回"},
    {"name": "live_version", "type": "string", "cn": "线上数据版本", "note": "当前同步版本号"},
]
# hbase 型（M2-3）：响应与 KV 通道同构，差异两处——无 data_as_of/live_version
# （HBase 行内无版本元信息键，services.yaml response.meta=[]，如实留空不虚报时点）、
# 多 datasource 字段（数据源可观测，X-OneServe-DataSource 同口径）。出参=授权字段集。
HBASE_FOUND_FIELDS = [
    {"name": "service_id", "type": "string", "cn": "服务标识", "note": "命中的服务 ID，回显定位"},
    {"name": "key", "type": "string", "cn": "查询键", "note": "请求键原样回显（rowkey＝上游供数键，平台不留明文）"},
    {"name": "tenant", "type": "string", "cn": "调用方租户", "note": "按 AppKey 在授权矩阵定位的租户 ID"},
    {"name": "found", "type": "boolean", "cn": "是否命中", "note": "rowkey 不在表时如实返回 false（诚实空值，不伪造空字段集）"},
    {"name": "fields", "type": "object", "cn": "返回字段集", "note": "键值对：仅授权白名单列；命中且非空才有值，否则为 null"},
    {"name": "datasource", "type": "string", "cn": "数据源标识", "note": "固定 hbase（本服务数据源为 HBase 行式存储）"},
]
HBASE_NOT_FOUND_FIELDS = [
    {"name": "found", "type": "boolean", "cn": "是否命中", "note": "固定 false"},
    {"name": "fields", "type": "object", "cn": "返回字段集", "note": "固定 null（绝不伪造空字段集）"},
    {"name": "datasource", "type": "string", "cn": "数据源标识", "note": "固定 hbase"},
]
# B 型代理：响应体原样透传上游，平台不定义其结构（如实描述，不编造上游 schema）
PROXY_RESPONSE_FIELDS = [
    {"name": "（HTTP 状态码）", "type": "int", "cn": "上游状态码", "note": "上游应答原样透传，平台不改写状态语义"},
    {"name": "（响应体）", "type": "json", "cn": "上游响应体", "note": "原样透传；字段结构以上游接口文档为准，不在本契约内定义"},
    {"name": "retention", "type": "平台行为", "cn": "留存口径", "note": "成功响应体是否落盘由 services.yaml retention 开关决定；关闭则零落盘（错误响应任何情况都不落盘）"},
]

# ── 错误码表（P1-2 错误码中心同源）：唯一真源＝service/app/errors.py ──
# 本脚本不再自行维护码表（消除第二份事实来源）；码/含义/解决方案/相关参数均由
# errors.contract_rows 给出。分组口径（BASE/KV/HBASE/PROXY/GATEWAY）也移入 errors.py。
BASE_ERRORS = errors.contract_rows(errors.BASE_CODES)
KV_ERRORS = errors.contract_rows(errors.KV_CODES, variant="kv")
HBASE_ERRORS = errors.contract_rows(errors.HBASE_CODES, variant="hbase")
PROXY_ERRORS = errors.contract_rows(errors.PROXY_CODES)
GATEWAY_ERRORS = errors.contract_rows(errors.GATEWAY_CODES)

ERRORS_META = (
    "错误响应统一为 §4.4 信封：error（机读错误码，见本表）＋message（人话说明）"
    "＋param（出错参数，适用时）＋traceId＋request_log_url（审计反查深链）"
    "＋docs_url（本表锚点）。限流口径（P1-5）：网关按 route×consumer 挂 "
    "limit-count/limit-conn（阈值＝appkeys.yaml 登记值，与门户「我的配额」同源），"
    "超限返 429 并带 X-RateLimit-Reason（global 日配额 / endpoint 路由速率 / concurrent 并发）——"
    "429 按凭证登记配额触发（非无条件），故不逐服务罗列，统一在 rate_limit 段说明。"
)


# ══════════════════ 真源装载 ══════════════════

def _yaml_module():
    import yaml  # service 层同款依赖（PyYAML）；缺失时 emit 降级 JSON
    return yaml


def load_services(path: Path = SERVICES_YAML) -> dict:
    """service_id → 注册条目（卡4：注册表单键唯一条目，无 latest 归并）。

    契约收全部注册服务进 x-oneserve-details——同服务再发布＝覆盖更新（单键恒一条
    目；发布史留痕见条目 trace.history，ADR-0008 §Decision.2 服务修订号已删）。
    """
    services = _yaml_module().safe_load(path.read_text(encoding="utf-8"))["services"]
    return {s["service_id"]: s for s in services}


def _matrix(path: Path) -> list:
    return _yaml_module().safe_load(path.read_text(encoding="utf-8"))["matrix"]


def load_auth_endpoints(path: Path = AUTH_MATRIX_YAML) -> dict:
    """service_id → 对外端点（授权矩阵登记值，取首个登记；portal_api._endpoint_of 同口径）。"""
    endpoints: dict[str, str] = {}
    for row in _matrix(path):
        for g in row.get("grants", []):
            sid, ep = g.get("service"), g.get("endpoint")
            if sid and ep and sid not in endpoints:
                endpoints[sid] = str(ep)
    return endpoints


def load_auth_fields(path: Path = AUTH_MATRIX_YAML) -> dict:
    """service_id → 授权字段白名单（首个登记条目；投影红线真源）。"""
    fields: dict[str, list] = {}
    for row in _matrix(path):
        for g in row.get("grants", []):
            sid = g.get("service")
            if sid and sid not in fields and g.get("fields"):
                fields[sid] = [str(f) for f in g["fields"]]
    return fields


def load_schema_columns(path: Path = SCHEMA_CSV) -> dict:
    """字段名 → 数据类型（详情页出参表类型列；297 宽表 CSV 缓存真源）。

    TODO(卡2/卡3)：随「表级 schema 元数据（装载副产品）」换源——按服务所属表取
    schema_<表名>.csv；297 宽表 CSV 由卡2 删除时本函数同步改造（当前缓删保演示链）。
    """
    with path.open(encoding="utf-8") as fh:
        return {row["字段名"]: row["数据类型"] for row in csv.DictReader(fh)}


def load_cn_desc() -> dict:
    """字段名 → 中文名（展示补名，非契约权威；缺名如实标「待补」）。

    卡2 换源：旧源＝发布台 mock admin/mock/schema-domains.json（已删，卡5 遗留清理）；
    现源＝表级字典 sync/schema_*.csv 的 cn_name 列并集（与装载副产品 schema 元数据
    同源，读经 sync/schema_meta.dict_rows 零第二实现；297 宽表 CSV＝缓删旧形态不参与）。
    """
    import schema_meta as _sm
    out: dict[str, str] = {}
    for p in sorted((ROOT / "sync").glob("schema_*.csv")):
        if p.name == "schema_usr_label_wide_297.csv":
            continue
        for name, row in _sm.dict_rows(p.stem[len("schema_"):]).items():
            if row.get("cnName"):
                out.setdefault(name, row["cnName"])
    return out


def load_gateway_routes(path: Path = APISIX_ROUTES) -> list[dict]:
    """网关路由全集（uri/methods/key_auth）：单一渲染入口＋init_routes 静态资源。

    TASK-M2-7-a：点查路由（通配＋兼容期专用）经 registry_render.render_route_specs
    单一入口渲染；静态非渲染通道（/v1/ping、/demo*、/v1/proxy、/v1/batch）直接 import
    init_routes.static_resources() 取数——不再对源码文本正则解析（原文本解析即第二条
    渲染/解析路径，配置改格式即静默漏读）。path 形参保留仅为兼容旧签名，不再解析。
    """
    sys.path.insert(0, str(ROOT / "service"))
    sys.path.insert(0, str(APISIX_ROUTES.parent))
    from app import registry_render as rend
    import init_routes as initr
    routes: list[dict] = []
    for _p, body in rend.render_route_specs(load_services()):
        routes.append({"name": body["name"], "uri": body["uri"],
                       "methods": list(body.get("methods", [])),
                       "key_auth": "key-auth" in body.get("plugins", {})})
    for _p, body in initr.static_resources():
        if "route" not in _p:
            continue
        routes.append({"name": body["name"], "uri": body["uri"],
                       "methods": list(body.get("methods", [])),
                       "key_auth": "key-auth" in body.get("plugins", {})})
    return routes


def _route_for(routes: list[dict], endpoint: str) -> dict | None:
    """按授权矩阵端点配网关路由（/v1/proxy/<slug> 与 /v1/proxy/<slug>/* 同义匹配）。"""
    for r in routes:
        if r["uri"].rstrip("*/") == endpoint.rstrip("/"):
            return r
    return None


def _sm3_hex(text: str) -> str:
    """示例键摘要（gmssl，与 kv_executor.sm3_hex 同算法口径；仅供契约示例值，非平台口径）。"""
    from gmssl import func as sm3_func, sm3 as sm3_lib
    return sm3_lib.sm3_hash(sm3_func.bytes_to_list(text.encode("utf-8")))


# ══════════════════ 契约构建（纯逻辑） ══════════════════

def _desensitize_notes(svc: dict) -> list:
    rules = (svc.get("response") or {}).get("desensitize") or {}
    notes = []
    for name, rule in rules.items():
        if rule.get("type") == "band_mask":
            outs = "/".join(str(b.get("out")) for b in rule.get("bands", []))
            notes.append(f"{name}：分档脱敏，输出 {outs} 档（原值零出现；非数值输出 {rule.get('unknown_out', 'U')} 档）")
        else:
            notes.append(f"{name}：{rule.get('type')}（services.yaml 登记规则）")
    return notes or ["无（本服务未登记脱敏规则）"]


def _auth_fields_rows(svc_id: str, auth_fields: dict, cn_desc: dict, columns: dict) -> list:
    rows = []
    for name in auth_fields.get(svc_id, []):
        rows.append({
            "name": name,
            "cn": cn_desc.get(name) or "—（中文名待补，先给列名）",
            "type": columns.get(name, "—"),
            "desensitize": None,   # 由 build_spec 按脱敏规则填充
        })
    return rows


def _example_for(archetype: str) -> dict:
    """报文示例：真实格式键值＋分步注释（curl 单行可复制）。"""
    if archetype == "kv_lookup":
        return {
            "kind": "curl",
            "command": [
                "# 用户标签域：键＝上游供数键（示例为对手机号实算的摘要；平台全程不收明文）",
                'curl -sS -X POST "http://<网关域名>/v1/tags/query" \\',
                '  -H "Content-Type: application/json" \\',
                '  -H "apikey: oneserve-dev-appkey-001" \\',
                "  -d '{\"key\": \"" + _sm3_hex(_DEMO_PHONE) + "\"}'",
                "",
                "# 资讯标签域：键＝资讯 ID 明文（同步造数编号段 n000001 起）",
                'curl -sS -X POST "http://<网关域名>/v1/news/query" \\',
                '  -H "Content-Type: application/json" \\',
                '  -H "apikey: oneserve-dev-appkey-001" \\',
                "  -d '{\"key\": \"" + _NEWS_SAMPLE_ID + "\"}'",
            ],
            "notes": [
                f"用户域示例键＝对 {_DEMO_PHONE} 实算的摘要（示例值；实际键由上游 ETL 生成）；是否命中取决于该键是否在库",
                "AppKey 为授权矩阵登记的开发占位凭证；正式凭证走 OA 审批后由授权登记发放",
                "响应带 data_as_of（数据时点）：任何应答都可核对数据批次",
            ],
        }
    return {
        "kind": "curl",
        "command": [
            "# 合作方代理域：路径与业务键按上游接口定义，平台原样转发（业务键只进审计摘要）",
            'curl -sS "http://<网关域名>/v1/proxy/partner/' + _PARTNER_DEMO_PATH + '?key=' + _PARTNER_DEMO_KEY + '" \\',
            '  -H "apikey: oneserve-dev-appkey-001"',
        ],
        "notes": [
            "示例路径/键取 mock 上游的演示业务键（PT-DEMO-0001）；生产上游以合作方接口文档为准",
            "响应体为上游原样回传；留存行为由平台 services.yaml 的 retention 开关决定",
        ],
    }


def _elements(spec: dict) -> dict:
    """九要素 P0 口径自检：前 5 项＋版本；缺项如实进 missing（不虚报完整）。"""
    e = {
        "identification": bool(spec["endpoint"] and spec["methods"] and spec["auth"]),
        "params_table": bool(spec["params"]),
        "response_table": bool(spec["response"]["found_shape"]),
        "errors": bool(spec["errors"]),
        "example": bool(spec["example"]["command"]),
        "version": bool(spec["contract_version"] and spec["platform_version"]),
    }
    label = {"identification": "标识", "params_table": "入参表", "response_table": "出参表",
             "errors": "错误码＋解决方案", "example": "报文示例", "version": "版本＋变更"}
    return {"items": e, "complete": all(e.values()),
            "missing": [label[k] for k, v in e.items() if not v]}


# 字段空间 → 展示域（2026-09-29 业务表字典多表化）：发布管线可写入名册外的新服务
# （service_id 任意），展示域名由 field_space 派生，未登记空间回落用户标签域。
_FIELD_SPACE_DOMAIN = {
    "usr_basic_info": ("user", "用户基础信息域"),
    "usr_asset_tag": ("user", "用户资产标签域"),
    "card_acct_summary": ("user", "卡账户汇总域"),
}


def _domain_of(svc: dict) -> tuple[str, str]:
    """服务 → (域 id, 域名)：名册优先，其次字段空间派生，缺省用户标签域。"""
    return (DOMAIN_OF.get(svc["service_id"])
            or _FIELD_SPACE_DOMAIN.get(svc.get("field_space"))
            or ("user", "用户标签域"))


def _service_display_name(svc: dict) -> str:
    dom = _domain_of(svc)[1]
    short = ARCHETYPE_LABEL.get(svc["archetype"], "").split("·")[-1].strip()
    return dom + "·" + short if short else dom


def build_spec(svc: dict, endpoint: str, route: dict | None, auth_fields: dict,
               cn_desc: dict, columns: dict) -> dict:
    """单服务契约构建（纯逻辑；不落盘）。"""
    sid, arch = svc["service_id"], svc["archetype"]
    methods = (route or {}).get("methods") or (["POST"] if arch == "kv_lookup" else ["GET"])
    datasource = (svc.get("backend") or {}).get("source", "redis")   # 缺省 redis（M2-3 向后兼容）
    if arch == "kv_lookup" and datasource == "hbase":
        # hbase 型（M2-3）：入参/投影/脱敏机器与 KV 通道同构（hbase_service 复用
        # kv_executor 同款）；差异＝数据源 hbase、响应无 data_as_of/live_version、
        # 多 datasource 标识——如实标注 datasource=hbase，不虚报时点字段。
        p0 = svc["params"][0]
        params = [{
            "name": p0["name"], "type": p0["type"], "cn": "查询键",
            "required": True, "in": "body",
            "note": "格式 " + p0["pattern"] + ("；" + str(p0["key_type"]) + " 声明口径（键由上游 ETL 生成，即 HBase rowkey）"
                                               if p0.get("key_type") == "phone_sm3" else ""),
            "pattern": p0["pattern"], "key_type": p0.get("key_type", ""),
        }]
        rows = _auth_fields_rows(sid, auth_fields, cn_desc, columns)
        desens = set((svc.get("response") or {}).get("desensitize") or {})
        for r in rows:
            if r["name"] in desens:
                r["desensitize"] = "分档脱敏输出（原值零出现）"
        response = {
            "found_shape": HBASE_FOUND_FIELDS, "not_found_shape": HBASE_NOT_FOUND_FIELDS,
            "meta_fields": list((svc.get("response") or {}).get("meta") or []),
            "auth_fields": rows, "desensitize_notes": _desensitize_notes(svc),
        }
        errors = HBASE_ERRORS
    elif arch == "kv_lookup":
        p0 = svc["params"][0]
        params = [{
            "name": p0["name"], "type": p0["type"], "cn": "查询键",
            "required": True, "in": "body",
            "note": "格式 " + p0["pattern"] + ("；" + str(p0["key_type"]) + " 声明口径（键由上游 ETL 生成）"
                                               if p0.get("key_type") == "phone_sm3" else ""),
            "pattern": p0["pattern"], "key_type": p0.get("key_type", ""),
        }]
        rows = _auth_fields_rows(sid, auth_fields, cn_desc, columns)
        desens = set((svc.get("response") or {}).get("desensitize") or {})
        for r in rows:
            if r["name"] in desens:
                r["desensitize"] = "分档脱敏输出（原值零出现）"
        response = {
            "found_shape": KV_FOUND_FIELDS, "not_found_shape": KV_NOT_FOUND_FIELDS,
            "meta_fields": list((svc.get("response") or {}).get("meta") or []),
            "auth_fields": rows, "desensitize_notes": _desensitize_notes(svc),
        }
        errors = KV_ERRORS
    else:
        params = [
            {"name": "（URL 路径）", "type": "string", "cn": "上游接口路径",
             "required": True, "in": "path",
             "note": "网关前缀 /v1/proxy/ 之后的路径原样转发上游",
             "pattern": "", "key_type": "proxy_path"},
            {"name": "key", "type": "string(1..128)", "cn": "业务键",
             "required": True, "in": "query",
             "note": "随查询串透传上游；仅进审计摘要与留存文件名，平台不留存业务明文",
             "pattern": "", "key_type": "partner_query_key"},
        ]
        response = {"found_shape": PROXY_RESPONSE_FIELDS, "not_found_shape": None,
                    "meta_fields": [], "auth_fields": [], "desensitize_notes": _desensitize_notes(svc)}
        errors = PROXY_ERRORS

    spec = {
        "service_id": sid,
        # 卡4：服务修订号已删——版本面只余 contract_version/platform_version（下两行，B 类不动）
        "archetype": arch,
        "archetype_label": ARCHETYPE_LABEL.get(arch, arch),
        "name": _service_display_name(svc),
        "domain": {"id": _domain_of(svc)[0], "name": _domain_of(svc)[1]},
        "summary": ("输入查询键，返回该实体的授权标签字段（白名单投影＋脱敏后输出，data_as_of 时点照带）"
                    if arch == "kv_lookup" else
                    "合作方上游接口受控代理：网关认证后原样转发，超时熔断＋重试＋留存开关按平台配置"),
        "endpoint": endpoint,
        "methods": methods,
        "gateway": {"route": (route or {}).get("uri", ""), "key_auth": bool((route or {}).get("key_auth"))},
        "auth": {
            "scheme": "ApiKey（网关 key-auth）",
            "header": GATEWAY_AUTH_HEADER,
            "describe": ("请求头 apikey 携带 AppKey；APISIX 认证后透传服务层做授权定位"
                         "（未携带/无效统一 401，服务层再按授权矩阵判定 403）"),
            "keys_note": DEMO_APPKEY_NOTE,
        },
        "rate_limit": {
            "enabled": True,
            "note": ("网关按 route×consumer 挂 limit-count/limit-conn（P1-5）；阈值＝"
                     "appkeys.yaml 登记的 quota_qps（路由速率）与 quota_daily（日配额），"
                     "与门户「我的配额」同源。超限返 429＋X-RateLimit-Reason"
                     "（global/endpoint/concurrent）；并发上限为平台常量，未按凭证登记。"
                     "未登记配额的凭证不上限流（如实不限）"),
            "header": "X-RateLimit-Reason",
            "reasons": ["global", "endpoint", "concurrent"],
        },
        "params": params,
        "response": response,
        "errors": errors + GATEWAY_ERRORS,
        "errors_meta": ERRORS_META,
        "example": _example_for(arch),
        "trial_url": "trial.html",
        "trial_hint": "在线调试请前往试用台（SPEC P0-2 口径：调试器本体由试用台承接）",
        "contract_version": CONTRACT_VERSION,
        "platform_version": PLATFORM_VERSION,
        "changelog": [
            {"version": CONTRACT_VERSION,
             "note": "契约初版：由 gen_openapi.py 自真源生成（后续服务登记变更自动跟随，变更记录在此追加）"},
        ],
    }
    spec["elements"] = _elements(spec)
    return spec


def build_contract(services: dict, endpoints: dict, routes: list[dict],
                   auth_fields: dict, cn_desc: dict, columns: dict) -> dict:
    """组装完整契约 dict（OpenAPI 3.1 骨架＋x-oneserve 扩展；yaml/json 双可序列化）。"""
    specs: dict[str, dict] = {}
    paths: dict = {}
    missing_route: list = []
    for sid in sorted(services):                     # 服务 id 定序：注册表新增版本条目不挪既有键序（幂等）
        svc = services[sid]
        if sid not in endpoints:
            raise SystemExit(f"[FAIL] 服务 {sid} 在授权矩阵无登记端点（auth_matrix.yaml grants[].endpoint）")
        route = _route_for(routes, endpoints[sid])
        if route is None and svc.get("archetype") != "external_proxy":
            # B 型代理路由经 upstreams.yaml 由 push_proxy_routes 运行期幂等下发（ADR-0013），
            # init_routes 不含静态代理通道（2026-10-28 开源剥离后无静态代理路由）——
            # external_proxy 服务按运行期下发口径如实标注（gateway.route 为空）。
            missing_route.append(endpoints[sid])
        spec = build_spec(svc, endpoints[sid], route, auth_fields, cn_desc, columns)
        specs[sid] = spec
        body_props = {p["name"]: {"type": "string", "pattern": p["pattern"] or None}
                      for p in spec["params"] if p["in"] == "body"}
        for m in spec["methods"]:
            op = {
                "x-oneserve-service": sid,
                "tags": [spec["domain"]["name"]],
                "summary": spec["name"] + "：" + spec["summary"],
                "description": "认证：" + spec["auth"]["describe"] + "。错误码：" + ", ".join(e["code"] for e in spec["errors"]),
                "security": [{"ApiKeyAuth": []}],
                "responses": {
                    "200": {"description": "成功（业务结果见 x-oneserve-details 出参表；键未命中也是 200）"},
                    "4XX": {"description": "请求/授权错误（码表见 x-oneserve-errors 与详情页错误码表）"},
                    "5XX": {"description": "服务端/上游错误（码表同上）"},
                },
            }
            if m.lower() == "post":
                op["requestBody"] = {"required": True, "content": {"application/json": {
                    "schema": {"type": "object",
                               "required": [p["name"] for p in spec["params"] if p["in"] == "body"],
                               "properties": body_props}}}}
            paths.setdefault(spec["endpoint"], {})[m.lower()] = op
    if missing_route:
        raise SystemExit(f"[FAIL] 网关路由缺失（init_routes.py 无对应路由）：{missing_route}")

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "OneServe 数据API网关 OpenAPI",
            "version": CONTRACT_VERSION,
            "description": ("由 service/services.yaml 等真源在构建期生成（scripts/gen_openapi.py）；"
                            "改服务只改 services.yaml，文档自动跟随——防文档与实现漂移（SPEC §4.1）"),
        },
        "servers": [{"url": "/", "description": "同源部署（门户 nginx 同域反代网关）"}],
        "tags": [{"name": v[1], "description": f"{v[0]} 域"} for v in DOMAIN_OF.values()],
        "paths": paths,
        "components": {"securitySchemes": {"ApiKeyAuth": {
            "type": "apiKey", "in": "header", "name": GATEWAY_AUTH_HEADER,
            "description": "网关 key-auth：请求头 apikey 携带 AppKey（" + DEMO_APPKEY_NOTE + "）"}}},
        "security": [{"ApiKeyAuth": []}],
        "x-oneserve-contract-version": CONTRACT_VERSION,
        "x-oneserve-envelope": {"error": ERRORS_META},
        "x-oneserve-details": [specs[sid] for sid in sorted(specs)],
    }


# （卡5 2026-09-30：P0-9 字段字典段（build_field_dictionary/_domain_of_field/
#  write_fields_sidecar，产 portal/openapi/fields.json）已随 297 字典体系下线整体移除——
#  消费方（字段字典页/fields.js）同步下线；表级 schema 元数据（装载副产品）由卡2/卡3 重建。）


# ══════════════════ A6 错误码中心（P1-2） ══════════════════

ERRORS_SOURCE = "service/app/errors.py（P1-2 错误码中心唯一真源，构建期由 scripts/gen_openapi.py 生成）"


def build_errors_center(services: dict, endpoints: dict) -> dict:
    """A6 错误码中心 sidecar（纯逻辑）：全码表 ＋ 每码被哪些服务返回。

    A6 页（portal/errors.html）与 §4.4 docs_url 的锚点同源：每个码一个
    errors.json 行，页面据此渲染 <tr id="<code>">——docs_url 因此真可达。
    """
    # 服务 → 其契约错误码集（网关码与通用码按 archetype 覆盖；每组都含 BASE）
    service_codes: dict[str, list[str]] = {}
    for sid in sorted(services):
        svc = services[sid]
        arch = svc.get("archetype")
        source = (svc.get("backend") or {}).get("source", "redis")
        service_codes[sid] = list(errors.archetype_codes(arch, source)) + list(errors.GATEWAY_CODES)
    # 码 → 服务清单（稳定顺序）
    used: dict[str, list[str]] = {}
    for sid, codes in service_codes.items():
        for c in codes:
            used.setdefault(c, []).append(sid)
    rows = []
    for e in errors.CODES:
        rows.append({
            "code": e.code,
            "http_status": e.http_status,
            "meaning": e.meaning,
            "solution": e.solution,
            "param": e.param or None,
            "source": e.source,          # service | gateway
            "scope": e.scope,            # http | row | job（后两者非 HTTP 信封）
            "services": used.get(e.code, []),
        })
    return {
        "contractVersion": CONTRACT_VERSION,
        "source": ERRORS_SOURCE,
        "envelope": {
            "fields": [errors.FIELD_ERROR, errors.FIELD_MESSAGE, errors.FIELD_PARAM,
                       errors.FIELD_TRACE_ID, errors.FIELD_REQUEST_LOG_URL,
                       errors.FIELD_DOCS_URL],
            "note": ("所有 ≥400 响应统一 §4.4 信封：error/message 既有字段不改名；"
                     "param 仅在适用时出现；traceId/request_log_url 由 trace_middleware "
                     "统一注入；docs_url 指向本页对应码的锚点"),
            "request_log_page": errors.REQUEST_LOG_PAGE,
        },
        "meta": {"total": len(rows),
                 "service": sum(1 for r in rows if r["source"] == "service"),
                 "gateway": sum(1 for r in rows if r["source"] == "gateway")},
        "codes": rows,
    }


def write_errors_sidecar(errors_center: dict, out_dir: Path = OUT_DIR) -> Path:
    """A6 错误码中心 sidecar 落盘（portal/openapi/errors.json；幂等）。"""
    f = out_dir / "errors.json"
    f.write_text(json.dumps(errors_center, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8", newline="\n")
    return f


# ══════════════════ 落盘 ══════════════════

def emit_yaml(contract: dict, out_dir: Path = OUT_DIR) -> Path:
    """契约落盘（PyYAML 优先；缺失时降级 JSON 同内容并打印口径）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "oneserve.openapi.yaml"
    try:
        import yaml
        text = yaml.safe_dump(contract, allow_unicode=True, sort_keys=False, width=110)
    except ImportError:
        text = json.dumps(contract, ensure_ascii=False, indent=2)
        print("[warn] 未安装 PyYAML，契约以 JSON 语法写入 oneserve.openapi.yaml（内容等价）")
    out.write_text(text, encoding="utf-8", newline="\n")
    return out


def write_sidecars(contract: dict, out_dir: Path = OUT_DIR) -> list:
    """详情/目录 sidecar：catalog.json＋details/<sid>.json；陈旧文件清盘（幂等）。"""
    details = {s["service_id"]: s for s in contract["x-oneserve-details"]}
    ddir = out_dir / "details"
    ddir.mkdir(parents=True, exist_ok=True)
    for stale in ddir.glob("*.json"):
        if stale.stem not in details:
            stale.unlink()
    written = []
    for sid in sorted(details):
        f = ddir / f"{sid}.json"
        f.write_text(json.dumps(details[sid], ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8", newline="\n")
        written.append(f)
    domains: dict[str, dict] = {}
    for sid, spec in details.items():
        d = domains.setdefault(spec["domain"]["id"],
                               {"id": spec["domain"]["id"], "name": spec["domain"]["name"], "services": []})
        d["services"].append({"service_id": sid, "name": spec["name"],
                              "endpoint": spec["endpoint"], "methods": spec["methods"]})
    catalog = {
        "contractVersion": CONTRACT_VERSION,
        "source": "portal/openapi/oneserve.openapi.yaml（构建期由 scripts/gen_openapi.py 生成）",
        "domains": list(domains.values()),
        "services": [{"service_id": s["service_id"], "name": s["name"], "domainId": s["domain"]["id"],
                      "endpoint": s["endpoint"], "methods": s["methods"], "summary": s["summary"],
                      "elementsComplete": s["elements"]["complete"]}
                     for s in details.values()],
    }
    f = out_dir / "catalog.json"
    f.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    written.append(f)
    return written


def main() -> int:
    services = load_services()
    endpoints = load_auth_endpoints()
    routes = load_gateway_routes()
    auth_fields = load_auth_fields()
    contract = build_contract(services, endpoints, routes,
                              auth_fields, load_cn_desc(), load_schema_columns())
    out = emit_yaml(contract)
    sidecars = write_sidecars(contract)
    print(f"[ok] 契约：{out.relative_to(ROOT)}（OpenAPI {contract['openapi']}，契约版本 {CONTRACT_VERSION}）")
    for s in contract["x-oneserve-details"]:
        e = s["elements"]
        mark = "6/6 完整" if e["complete"] else "缺项：" + "、".join(e["missing"])
        print(f"[ok] 详情 {s['service_id']:<16} {s['endpoint']:<24} 九要素(P0) {mark}")
    # （卡5：P0-9 字段字典 sidecar 生成已随 297 字典体系下线移除，见文件头注释。）
    # P1-2 A6 错误码中心 sidecar（与 §4.4 信封/docs_url 同源；errors.html 据此渲染锚点）
    errors_center = build_errors_center(services, endpoints)
    errors_out = write_errors_sidecar(errors_center)
    print(f"[ok] 错误码中心 {errors_out.relative_to(ROOT)}（P1-2：{errors_center['meta']['total']} 码，"
          f"服务层 {errors_center['meta']['service']} ／ 网关 {errors_center['meta']['gateway']}）")
    print(f"[ok] sidecar：{len(sidecars) + 1} 件 → portal/openapi/（详情页/错误码页 fetch 静态文件，运行时零计算）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
