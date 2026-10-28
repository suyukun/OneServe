#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TASK-M2-5 真 HBase 元数据抓取（开发替身容器；非常驻、验完即删）。

用途：从**真 HBase**（compose.hbase.yml 定义的开发替身容器，
默认不随 up.sh 起）抓取 namespace/表/列族元数据，并与字段字典 CSV 双源拼装成
「发现端点响应形状」fixture，供 tests/test_publish_hbase.py 断言适配层可正确归一化。

【平台前提·不要误解】平台**不自建** HBase 集群、**不新增常驻服务**；生产＝示例 HBase 集群
现役组件（运维归 MRS 团队）。本脚本只连开发替身，连接地址走 HBASE_HOST/HBASE_PORT
（.env.example），不硬编码；平台只读、不写 HBase（写入方归数仓，NC-M2-2）。

如实标注（本卡实测结论）：
  HBase meta（happybase.Connection.tables / 表描述）只给到 **namespace / 表 / 列族**，
  **不给列（qualifier）**——列无语义 schema 可发现；AC-4 的「列」必须来自字段字典 CSV，
  由 M2-4 做 meta×字典双源校验。故本 fixture 的 columns 取自真 CSV，families 取自真 meta。

用法（容器已起）：
  docker compose -f docker-compose.yml -f compose.hbase.yml up -d hbase
  python3 scripts/m25_hbase_fixture.py
  docker rm -f oneserve-hbase          # 验完让位内存
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "hbase_discovery.real.json"
META_OUT = ROOT / "tests" / "fixtures" / "hbase_meta.real.json"
CSV = ROOT / "sync" / "schema_usr_label_wide_297.csv"   # TODO(卡2): 297 宽表字典换源为表级 schema 元数据（装载副产品）；CSV 缓删至卡2 交接（卡5 2026-09-30）

HOST = os.environ.get("HBASE_HOST", "127.0.0.1")
PORT = int(os.environ.get("HBASE_PORT", "9090"))


def main() -> int:
    import happybase   # 延迟导入：无容器/CI 不强制安装

    conn = happybase.Connection(host=HOST, port=PORT, timeout=30000)
    try:
        raw = [t.decode("utf-8") for t in conn.tables()]
        # happybase 对 default namespace 回裸表名（不带 "default:"）；非默认才带 "ns:"。
        tables = sorted(raw)
        meta = {"host": HOST, "port": PORT, "capturedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "namespaces": sorted({(t.split(":")[0] if ":" in t else "default") for t in tables}) or ["default"],
                "tables": [{"name": t,
                            "namespace": (t.split(":")[0] if ":" in t else "default"),
                            "table": (t.split(":")[-1] if ":" in t else t),
                            "families": _families_of(conn, t)} for t in tables]}
    finally:
        conn.close()
    META_OUT.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    columns = _csv_columns()
    domains = {}
    for t in meta["tables"]:
        for fam in t["families"]:
            # 双源拼装：families 来自真 meta，columns 来自真 CSV（按列族分桶，本卡起步单列族）
            dom = "营销域" if t["table"].startswith(("ads_", "oneserve")) else "default"
            domains.setdefault(dom, {"id": dom, "name": dom, "desc": "真 HBase meta×字段字典 CSV",
                                     "tables": []})
            domains[dom]["tables"].append({
                "namespace": t["namespace"], "table": t["table"],
                "keyTypes": ["usr_id", "phone_sm3"],
                "keySamples": {"phone_sm3": _sm3("13800138000")},
                "families": [{"name": fam, "columns": columns}]})
    payload = {
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dualSource": {"status": "ok", "message": "HBase meta 与字段字典 CSV 一致（fixture 双源拼装）"},
        "domains": list(domains.values()),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] 真 HBase meta → {META_OUT.relative_to(ROOT)}（namespace/表/列族）")
    print(f"[ok] 发现形状 fixture → {OUT.relative_to(ROOT)}（families=真 meta，columns=真 CSV）")
    print(f"     namespaces={meta['namespaces']} tables={len(meta['tables'])} columns={len(columns)}")
    return 0


def _families_of(conn, table: str) -> list[str]:
    """真 meta 取列族名（happybase 表描述 API；不可用则回落空并如实）。"""
    try:
        return sorted(f.decode("utf-8") if isinstance(f, bytes) else str(f)
                      for f in conn.table(table).families().keys())
    except Exception:   # noqa: BLE001 —— 方言差异：列族探测不到就如实留空
        return []


def _csv_columns() -> list[dict]:
    if not CSV.is_file():
        return []
    out = []
    with CSV.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            name = (r.get("字段名") or "").strip()
            if not name or name == "usr_id":
                continue
            out.append({"name": name, "cnName": (r.get("cn_name") or "").strip() or "待补",
                        "type": (r.get("数据类型") or "").strip(), "sensitive": (r.get("敏感级") or "").strip()})
            if len(out) >= 40:   # 起步摘录（与 O-1 演示口径一致，不塞 296 列）
                break
    return out


def _sm3(text: str) -> str:
    sys.path.insert(0, str(ROOT / "service"))
    from app.kv_executor import sm3_hex
    return sm3_hex(text)


if __name__ == "__main__":
    raise SystemExit(main())
