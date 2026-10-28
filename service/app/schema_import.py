# -*- coding: utf-8 -*-
"""表结构登记纯逻辑（B1 · 2026-10-09）——贴 DDL → sqlglot 解析 COMMENT → 写回既有真源。

定稿口径（handoff-2026-10-08 §三.6 / 维护者 2026-10-09「不要过度设计」）：
  · 真源＝既有 sync/schema_<表>.csv（6 列：attnum,字段名,数据类型,cn_name,业务口径,敏感级），
    不新建元数据体系；
  · DDL 解析只做三件事：字段名 / 类型 / COMMENT（→ cn_name）。解析失败如实报错，
    不做正则兜底（兜底解析＝静默错行，宁可拒收）；
  · 既有字段行的业务口径/敏感级一律保留（下游 drift/守卫依赖敏感级）；新字段两列留空
    （未判定 ≠ 内部，如实留白待补）；
  · 表名白名单 ^[a-z0-9_]+$（防路径穿越），且须在 HBase 平台表清单（端点层校验）。

分层纪律：本模块零 FastAPI/Redis 依赖（仅 sqlglot/csv/pathlib），可脱离服务单测；
admin_api 只做 HTTP 解析与调用。
"""
from __future__ import annotations

import csv
import os
import re
from pathlib import Path

import sqlglot
from sqlglot.expressions import ColumnConstraint, ColumnDef, CommentColumnConstraint

SYNC_DIR = Path(os.environ.get("ONESERVE_SYNC_DIR", "/sync"))
TABLE_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")

CSV_HEADER = ["attnum", "字段名", "数据类型", "cn_name", "业务口径", "敏感级"]


class SchemaImportError(Exception):
    """登记失败（机读码 + 人话消息；端点层映射 HTTP 状态）。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def parse_ddl(ddl_text: str) -> list[dict]:
    """DDL → [{name, type, comment}]（comment 缺省空串；顺序即 DDL 列序）。"""
    if not ddl_text or not ddl_text.strip():
        raise SchemaImportError("EMPTY_DDL", "DDL 内容为空")
    try:
        tree = sqlglot.parse_one(ddl_text, read=None)
    except Exception as exc:  # noqa: BLE001 —— 解析失败如实拒收，不正则兜底
        raise SchemaImportError(
            "DDL_PARSE_FAILED", f"DDL 解析失败：{str(exc)[:160]}") from exc
    cols = list(tree.find_all(ColumnDef))
    if not cols:
        raise SchemaImportError(
            "NO_COLUMNS", "DDL 中未找到列定义（须为 CREATE TABLE 语句）")
    out = []
    seen = set()
    for c in cols:
        name = c.name
        if not name:
            continue
        if name in seen:
            raise SchemaImportError(
                "DUPLICATE_COLUMN", f"字段重复：{name}")
        seen.add(name)
        kind = c.args.get("kind")
        comment = ""
        for con in (c.args.get("constraints") or []):
            if isinstance(con, ColumnConstraint):
                ck = con.args.get("kind")
                if isinstance(ck, CommentColumnConstraint) and ck.this:
                    comment = str(ck.this.this or "")
        out.append({"name": name,
                    "type": kind.sql() if kind else "",
                    "comment": comment})
    if not out:
        raise SchemaImportError("NO_COLUMNS", "DDL 列定义解析结果为空")
    return out


def load_rows(table: str) -> list[dict]:
    """读既有真源行（无文件→空列表；坏文件如实报错不吞）。"""
    path = _csv_path(table)
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = [{k: (v or "") for k, v in (r or {}).items()} for r in reader]
    return rows


def merge_rows(parsed: list[dict], existing: list[dict]) -> list[list[str]]:
    """解析结果 × 既有真源 → 6 列行。既有字段保留业务口径/敏感级；新字段留空。"""
    old_by_name = {r.get("字段名", ""): r for r in existing}
    rows, kept = [], []
    for i, p in enumerate(parsed, start=1):
        old = old_by_name.get(p["name"], {})
        biz = old.get("业务口径", "")
        sens = old.get("敏感级", "")
        if old:
            kept.append(p["name"])
        rows.append([str(i), p["name"], p["type"], p["comment"], biz, sens])
    removed = [n for n in old_by_name if n and n not in kept]
    if removed:  # 不静默删字段：既有字段在 DDL 中消失＝显式报出（维护者 决定是否接受）
        raise SchemaImportError(
            "FIELDS_DROPPED",
            f"既有字段未出现在 DDL（拒绝静默删除，请确认）：{', '.join(removed[:8])}"
            + ("…" if len(removed) > 8 else ""))
    return rows


def save_csv(table: str, rows: list[list[str]]) -> Path:
    """原子写回真源（tmp＋os.replace；目录内 rename 安全，非单文件 bind mount）。"""
    path = _csv_path(table)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        w.writerows(rows)
    os.replace(tmp, path)
    return path


def table_ok(table: str) -> bool:
    return bool(TABLE_NAME_RE.fullmatch(table or ""))


def _csv_path(table: str) -> Path:
    if not table_ok(table):
        raise SchemaImportError("INVALID_TABLE", f"表名非法：{table!r}")
    return SYNC_DIR / f"schema_{table}.csv"
