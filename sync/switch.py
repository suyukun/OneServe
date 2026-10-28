# -*- coding: utf-8 -*-
"""原子切换（task-006 · SPEC §4.3）：meta 指针键 tags:<表>:latest → 新版本号（按表隔离）。

放行三闸（任一不满足即拒切，线上仍旧版）：
  1) 新版本已加载（版本元信息存在）；
  2) 对账全绿（sync:recon:<表>:<版本>.pass == 1）；
  3) 新空间非空。
切换动作＝对 meta 指针单条 SET（Redis 单命令原子，读方要么旧版要么新版，无中间态）。
旧空间保留（双缓冲口径：回滚昨日秒级）。

用法：
  python3.13 sync/switch.py --table usr_basic_info --version 20260921
"""
import argparse
import json
import sys
import time

import sync_config as C
from common import (audit_append, count_keys, get_live, now_iso,
                    redis_conn, ver_meta_get)


def main():
    ap = argparse.ArgumentParser(description="原子切 meta 指针到新版本（按表键空间）")
    ap.add_argument("--version", required=True, help="目标版本（YYYYMMDD）")
    ap.add_argument("--table", default=C.DEFAULT_TABLE,
                    help="表名（键空间域段 tags:<表>:*；缺省不切域＝存量单域口径）")
    args = ap.parse_args()

    if args.table:
        C.use_table(args.table)   # 键空间按表归位（三闸判定零改动：闸体键名随域段参数化）
    r = redis_conn()

    # —— 放行三闸 ——
    meta = ver_meta_get(r, args.version)
    if meta is None:
        print(json.dumps({"ok": False, "switched": False,
                          "reason": f"版本 {args.version} 未加载（元信息缺失），拒切"}, ensure_ascii=False))
        return 1
    recon = r.hgetall(C.RECON_META.format(version=args.version))
    if recon.get("pass") != "1":
        print(json.dumps({"ok": False, "switched": False,
                          "reason": f"对账未通过或未对账（pass={recon.get('pass', '未对账')}），拒切，线上仍旧版"},
                         ensure_ascii=False))
        return 1
    n = count_keys(r, f"{C.NS}:{args.version}:*")
    if n == 0:
        print(json.dumps({"ok": False, "switched": False,
                          "reason": "新空间为空（0 键），拒切"}, ensure_ascii=False))
        return 1

    live = get_live(r)
    if live == args.version:
        print(json.dumps({"ok": True, "switched": False, "note": f"指针已指向 {args.version}，幂等无动作"},
                         ensure_ascii=False))
        return 0

    # —— 原子切：单条 SET ——
    t0 = time.perf_counter()
    r.set(C.ALIAS_KEY, args.version)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert r.get(C.ALIAS_KEY) == args.version  # 切后自校验

    audit_append(r, C.SWITCH_LOG, {
        "at": now_iso(), "action": "switch", "from": live, "to": args.version,
        "recon": "green", "rows": int(meta.get("rows", 0)),
    })
    print(json.dumps({"ok": True, "switched": True, "from": live, "to": args.version,
                      "switch_elapsed_ms": round(elapsed_ms, 3),
                      "old_space_kept": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
