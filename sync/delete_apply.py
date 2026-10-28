# -*- coding: utf-8 -*-
"""删除权传导·最小版（task-006 · SPEC §4.3）：删除清单文件 → KV 删键。

- 清单格式：纯文本/CSV，每行一个供数键（造数口径为 64 位十六进制）；空行与 # 注释忽略；
  首行若是表头 usr_key_sm3 也忽略；
- 作用域（--scope）：all＝所有现存版本空间（默认，防旧缓冲翻回复活已删用户）；
  live＝仅当前线上空间；
- 审计：sync:delete:log 追加 JSON（时间/文件指纹/清单条数/各空间命中数）；
- 「次日凌晨生效」的生产时序在生产调度里落位，本最小版＝跑即生效（如实口径）。

用法：
  python3.13 sync/delete_apply.py --list sync/out/delete_list.csv
"""
import argparse
import json
import re
import sys
import time

import sync_config as C
from common import audit_append, data_key, get_live, list_versions, now_iso, redis_conn, sha256_file

_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def load_list(path):
    """解析删除清单：返回 (keys, skipped)；非法行计 skipped。"""
    keys, skipped = [], 0
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#") or (ln == 1 and s == C.KEY_COLUMN):
                continue
            if _KEY_RE.match(s):
                keys.append(s.lower())
            else:
                skipped += 1
    return keys, skipped


def main():
    ap = argparse.ArgumentParser(description="删除清单 → KV 删键（含审计）")
    ap.add_argument("--list", required=True, help="删除清单文件（每行一个键）")
    ap.add_argument("--scope", choices=["all", "live"], default="all",
                    help="删哪些空间（默认 all＝所有现存版本空间）")
    args = ap.parse_args()

    t0 = time.perf_counter()
    keys, skipped = load_list(args.list)
    if not keys:
        print(json.dumps({"ok": False, "error": "清单为空（无合法键），拒绝执行"}, ensure_ascii=False))
        return 1

    r = redis_conn()
    if args.scope == "live":
        live = get_live(r)
        versions = [live] if live else []
    else:
        versions = list_versions(r)

    per_version = {}
    for v in versions:
        pipe = r.pipeline(transaction=False)
        for k in keys:
            pipe.delete(data_key(v, k))
        res = pipe.execute()
        per_version[v] = sum(1 for x in res if x == 1)  # DEL 返回 1＝真删到

    entry = {
        "at": now_iso(), "file": args.list, "file_sha256": sha256_file(args.list)[:16] + "…",
        "listed": len(keys), "skipped_illegal": skipped, "scope": args.scope,
        "per_version_hit": per_version, "elapsed_s": round(time.perf_counter() - t0, 2),
    }
    audit_append(r, C.DELETE_LOG, entry)
    print(json.dumps({"ok": True, **entry}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
