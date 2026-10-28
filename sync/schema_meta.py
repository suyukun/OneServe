# -*- coding: utf-8 -*-
"""表级 schema 元数据与 tabver 索引（卡2 装载副产品 · 装载与发布流程拆分设计_v1 §二.4/§六）。

定位：装载成功的**副产品**——字段清单来自「本次装载观测列」（HBase 数据面事实），
类型/中文名/业务口径富化自表级字典 sync/schema_<裸表名>.csv（存在则填；缺则
type="unknown"/cnName="待补"/desc=""，如实不猜，source 如实区分 hbase / hbase+dict）。

键空间（B3.2 契约，随版本走；常量在 sync_config，本模块只做读写与拼装）：
  sync:schema:<表>:<版本>  HASH  schema=JSON 数组（字段对象，按字段名排序）
                                 field_count/source/table/version/generated_at
  sync:tables              SET   已装载裸表名全集（跨表枚举索引）
  sync:tabver:<表>         SET   该表已装载版本（YYYYMMDD）

纪律：
  - 唯一写入点＝sync/hbase_to_redis.write_space 尾部（CLI 与 HTTP 两入口同源——
    卡5 守卫「已装载表必有 schema」据此对全部装载路径成立）；
  - 绝不写 tags:<表>:latest（指针切换归 sync/switch.py 对账三闸）；
  - 键名按表参数化（key_domain 校验），不依赖模块级键域状态——服务进程并发安全。
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import sync_config as C


def _now_iso() -> str:
    """当前时刻 ISO（与 sync/common.now_iso 同口径；此处独立实现避免循环依赖）。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _sync_dir() -> Path:
    """sync/ 目录定位（与 hbase_load._sync_dir 同款双布局：env > 容器 /sync > 宿主）。"""
    env = os.environ.get("SYNC_DIR")
    if env and (Path(env) / "sync_config.py").is_file():
        return Path(env)
    for cand in (Path("/sync"), Path(__file__).resolve().parent):
        if (cand / "sync_config.py").is_file():
            return cand
    return Path(__file__).resolve().parent


def key_domain(table: str) -> str:
    """表名 → 裸表名域段（透传 sync_config.key_domain：剥 namespace、非法即抛）。"""
    return C.key_domain(table)


def dict_rows(table: str, sync_dir: Path | None = None) -> dict[str, dict]:
    """表级字典 CSV → {字段名: {type, cnName, desc}}；无表级 CSV＝{}（如实，不回落宽表）。

    CSV 口径＝sync/schema_<裸表名>.csv（6 列：attnum,字段名,数据类型,cn_name,业务口径,敏感级）。
    """
    import re as _re
    domain = key_domain(table)
    if not _re.fullmatch(r"[A-Za-z0-9_]+", domain):
        return {}
    p = (sync_dir or _sync_dir()) / f"schema_{domain}.csv"
    if not p.is_file():
        return {}
    out: dict[str, dict] = {}
    with p.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("字段名") or "").strip()
            if not name:
                continue
            out[name] = {
                "type": (row.get("数据类型") or "").strip(),
                "cnName": (row.get("cn_name") or "").strip(),
                "desc": (row.get("业务口径") or "").strip(),
            }
    return out


def build_fields(table: str, cf: str, observed_cols, sync_dir: Path | None = None
                 ) -> tuple[list[dict], str]:
    """装载观测列 → schema 字段对象数组（按字段名排序）＋ source 标注。

    observed_cols＝本次装载观测到的列名集合/清单（HBase 数据面事实，必有）；
    富化优先级＝表级字典（有则 type/cnName/desc，source="hbase+dict"）；
    缺字典或字段不在字典＝type="unknown"/cnName="待补"/desc=""（source 按是否命中字典整体标注）。
    """
    enrich = dict_rows(table, sync_dir)
    names = sorted({str(c).strip() for c in (observed_cols or []) if str(c).strip()})
    fields = [{
        "name": n,
        "cf": cf,
        "type": enrich.get(n, {}).get("type") or "unknown",
        "cnName": enrich.get(n, {}).get("cnName") or "待补",
        "desc": enrich.get(n, {}).get("desc") or "",
    } for n in names]
    source = "hbase+dict" if (enrich and any(n in enrich for n in names)) else "hbase"
    return fields, source


# ---------- 写入（唯一调用方＝hbase_to_redis.write_space 尾部） ----------

def schema_meta_set(r, table: str, version: str, fields: list[dict], *,
                    source: str, generated_at: str | None = None) -> dict:
    """写表级 schema 元数据 hash（sync:schema:<表>:<版本>）。

    返回 {"key", "field_count", "source", "generated_at"}——装载响应的 schema 段同源取值。
    """
    key = C.SCHEMA_META_KEY.format(table=key_domain(table), version=version)
    generated_at = generated_at or _now_iso()
    mapping = {
        "schema": json.dumps(fields, ensure_ascii=False),
        "field_count": str(len(fields)),
        "source": source,
        "table": key_domain(table),
        "version": version,
        "generated_at": generated_at,
    }
    r.delete(key)
    r.hset(key, mapping=mapping)
    return {"key": key, "field_count": len(fields), "source": source,
            "generated_at": generated_at}


def tabver_index_add(r, table: str, version: str) -> None:
    """登记 tabver 索引：SADD sync:tables ＋ SADD sync:tabver:<表>（装载成功时调用）。"""
    domain = key_domain(table)
    r.sadd(C.TABLES_INDEX_KEY, domain)
    r.sadd(C.TABVER_KEY.format(table=domain), version)


# ---------- 读取（服务端点 / 卡3 发布向导 / 卡5 通用守卫） ----------

def schema_meta_get(r, table: str, version: str) -> dict | None:
    """读表级 schema 元数据；不存在＝None（调用方按 404/守卫口径处理）。"""
    key = C.SCHEMA_META_KEY.format(table=key_domain(table), version=version)
    h = r.hgetall(key)
    if not h:
        return None
    return {
        "table": h.get("table", ""),
        "version": h.get("version", ""),
        "field_count": int(h.get("field_count") or 0),
        "source": h.get("source", ""),
        "generated_at": h.get("generated_at", ""),
        "fields": json.loads(h.get("schema") or "[]"),
    }


def tabver_tables(r) -> list[str]:
    """已装载表全集（sync:tables；排序稳定）。"""
    return sorted(r.smembers(C.TABLES_INDEX_KEY))


def tabver_versions(r, table: str) -> list[str]:
    """某表已装载版本清单（sync:tabver:<表>；排序稳定）。"""
    return sorted(r.smembers(C.TABVER_KEY.format(table=key_domain(table))))
