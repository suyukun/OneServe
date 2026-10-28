# -*- coding: utf-8 -*-
"""HBase 服务执行器（TASK-M2-3）：hbase 数据源服务接业务读路径。

范围界定（门槛稿 TASK-M2-3 行＋AC-1/AC-2/AC-14）：
  - services.yaml 数据源类型字段：backend.source = redis | hbase（缺省 redis＝
    向后兼容，存量服务零改动）；hbase 型条目 backend.hbase 带 table/cf/key_type
    （B3 渲染器契约，M2-3.5 渲染器复用）；
  - hbase 数据源服务走 hbase_executor.get（rowkey＝上游供数键，ADR-0001/NC-M2-2 口径）
    →整行反序列化为字段集→复用 kv_executor 同款投影＋脱敏（AC-2：白名单∩请求，
    白名单外字段名零出现）；found 语义与 KV 通道同构（诚实空值）；
  - 每次调用记审计七元组（AC-14）：结构沿 audit.make_entry（caller/endpoint/
    params_digest/status/latency_ms/response_body/trace_id），audit_endpoint 带
    datasource=hbase 可区分数据源；写失败不阻断业务（main._audit_call 同口径）。

设计约束（沿 kv_executor / hbase_executor 同款纪律）：
  - 本模块不 import happybase/redis/FastAPI——HBase 客户端经 hbase_client 参数
    注入（鸭子类型：提供 run_get(query_timeout_s, table, rowkey, columns) 协程
    即可），单测用假客户端覆盖全路径，无容器也能跑；
  - 表名/列族/列集零硬编码，全按 services.yaml 配置驱动；
  - 列级投影前置到 Thrift 取数（columns=限定取列）：行内未授权列的值不进内存
    ——比取整行后丢弃更贴投影红线（AC-2 第一道闸；行键属键域非标签域）；
  - 超时双口径：guard.timeout_ms 包整链（kv_executor 同款），Thrift 调用另有
    hbase_executor 的 query_timeout_s（服务层超时预算内必有机读错误返回）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from app import errors
from app import kv_executor as kvx
from app import registry_render as rend   # 列族形态归一单一事实来源（TASK-M2-7-a 扩展位）
from app.hbase_executor import HBaseQueryError, run_get as _module_run_get

AUDIT_ENDPOINT = "service:/v1/hbase/query?datasource=hbase"   # AC-14：数据源可区分


def validate_backend(service: dict) -> dict:
    """hbase 型条目结构校验（配置错误启动期显式暴露，不带病运行）。

    TASK-M2-7-a 多列族扩展位（维护者 2026-09-23 口径）：backend.hbase.cf 契约允许
    str 或 str 列表（HBase 同步表未来可能 rowkey＝key、其余大量列族＝字段）。本阶段
    只实现单列族语义：列表 >1 列族显式拒（RuntimeError），绝不静默只取首个——
    「有配置不生效」按缺陷处理。多列族（按列族分组投影）归 M2-4 之后。
    """
    backend = service.get("backend", {})
    hb = backend.get("hbase")
    if not isinstance(hb, dict):
        raise RuntimeError(f"服务 {service.get('service_id')} 标记 source=hbase 但缺 backend.hbase")
    for field in ("table", "cf", "key_type"):
        if not hb.get(field):
            raise RuntimeError(f"服务 {service.get('service_id')} backend.hbase 缺必填字段 {field}")
    fams = rend.hbase_column_families(backend)
    if len(fams) > 1:
        raise RuntimeError(
            f"服务 {service.get('service_id')} backend.hbase.cf 登记 {len(fams)} 个列族 "
            f"{fams}；多列族语义未实现（TASK-M2-7-a 只留扩展位）——请登记单列族")
    if not service.get("params"):
        raise RuntimeError(f"服务 {service.get('service_id')} 缺 params（键模式校验不可少）")
    return hb


def row_to_fields(row: dict[str, str], cf: str) -> dict[str, str]:
    """Thrift 行数据反序列化为字段集：b"cf:col" → 字段名 col（非本列族列显式丢弃）。"""
    prefix = cf + ":"
    return {name.split(":", 1)[1]: value for name, value in row.items()
            if name.startswith(prefix) and name.split(":", 1)[1]}


async def execute(hbase_client: Any, service: dict, grant: dict, key: str,
                  *, hbase_run: Any = None, store: Any = None) -> dict:
    """一次 HBase 点查：rowkey 取行→列级投影→反序列化→白名单投影→脱敏→响应。

    hbase_client：hbase_executor.HBaseClient（或同形假实现，单测注入）；取数桥
    解析＝客户端自带 run_get 方法（鸭子类型，假客户端同形）优先，否则用模块级
    run_get 桥（to_thread＋wait_for 包阻塞调用）；service：services.yaml 单条
    注册（backend.source=hbase）；grant：授权条目（fields=字段白名单）；key：
    收到的键值。返回响应体 dict（found=false 时 fields=None，与 KV 通道同构）。

    漂移隔离闸（TASK-M2-3.8，AC-8-b）：store 非空时复用 kv_executor 同款
    check_drift_quarantine——每请求读 sync:drift:<id>@<ver>，命中 quarantined 抛
    TagQueryError(503/QUARANTINE_CODE)（与 Redis 通道同一错误族，端点层同款映射）。
    分层纪律：状态键读写=caller 传 store，判定/编解码在 schema_drift 纯逻辑。
    store=None（未注入）→ 跳过（既有直接调用方/单测零改动；fail-open 不误伤）。
    """
    run_bridge = hbase_run or getattr(hbase_client, "run_get", None) or _module_run_get
    hb = validate_backend(service)
    guard = service.get("guard", {})
    timeout_ms = guard.get("timeout_ms", 3000)

    key = kvx.normalize_key(key, service["params"][0]["pattern"])
    fields_granted = grant["fields"]
    # 本阶段单列族语义：cf 归一后取首个（cf 为单元素列表亦支持＝扩展位不堵死）
    cf = rend.primary_column_family(service.get("backend", {}))

    if store is not None:
        await kvx.check_drift_quarantine(store, service)   # 漂移隔离闸（同 kv_executor 口径）

    async def _read() -> dict:
        # 1) 列级投影前置（AC-2 第一道闸）：只取白名单列的值进内存
        columns = [f"{cf}:{name}" for name in fields_granted]
        # 2) rowkey＝收到的键（as_received 口径）；行不存在＝空 dict → 诚实空值
        row = await run_bridge(hbase_client, hb["table"], key, columns)
        # 3) 反序列化＋投影（第二道闸：白名单∩取回字段，kv_executor.project 同款）
        tags = row_to_fields(row, cf)
        fields = kvx.project(tags, fields_granted)
        fields = kvx.apply_desensitize(fields, service.get("response", {}).get("desensitize"))
        return {"found": bool(fields),
                "fields": fields or None,                     # 空标签集 → null（SPEC §4.3 同构）
                "datasource": "hbase"}

    try:
        result = await asyncio.wait_for(_read(), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError:
        raise HBaseQueryError(503, errors.GUARD_TIMEOUT,
                              f"HBase 读超时（>{timeout_ms}ms），guard 拒绝返回") from None

    resp = {"service_id": service["service_id"], "key": key, "tenant": grant["tenant"],
            # 卡4 AC-1d：响应不带服务修订号 version；live_version（表数据版本）口径不变
            **result}
    return resp