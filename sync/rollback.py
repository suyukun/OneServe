# -*- coding: utf-8 -*-
"""回滚（task-006 · SPEC §4.3）：meta 指针指回旧版本，秒级生效（按表键空间隔离）。

- 默认取切换日志（sync:switch:<表>:log）中「切到当前版本」那一条的来源版本＝上一版；
  也可 --to 显式指定（仍受安全闸约束）；
- 安全闸：目标 ≠ 当前、目标空间必须存在且非空（拒绝指向空空间造成线上空数据）；
- 动作＝对 meta 指针单条 SET，读方下一跳即读旧版（无需停机/重灌）；
- 只动本表指针：别的表键空间/指针零影响（2026-09-29 按表隔离改造）。

用法：
  python3.13 sync/rollback.py --table usr_basic_info            # 自动回上一版
  python3.13 sync/rollback.py --table usr_basic_info --to 20260920
"""
import argparse
import json
import sys
import time

import sync_config as C
from common import (audit_append, count_keys, get_live, now_iso, redis_conn)


def _previous_version(r, live):
    """从切换/回滚日志倒序找「把指针变成 live」的最近一条，返回其来源版本。"""
    for raw in r.lrange(C.SWITCH_LOG, 0, 99):
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if e.get("to") == live and e.get("action") in ("switch", "rollback"):
            return e.get("from")
    return None


def main():
    ap = argparse.ArgumentParser(description="meta 指针回滚到旧版本（按表键空间）")
    ap.add_argument("--to", help="目标版本（缺省＝自动取上一版）")
    ap.add_argument("--table", default=C.DEFAULT_TABLE,
                    help="表名（键空间域段 tags:<表>:*；缺省不切域＝存量单域口径）")
    args = ap.parse_args()

    if args.table:
        C.use_table(args.table)   # 键空间按表归位（安全闸判定零改动：闸体键名随域段参数化）
    r = redis_conn()
    live = get_live(r)
    if live is None:
        print(json.dumps({"ok": False, "rolled_back": False, "reason": "指针不存在，无线上版本可回滚"},
                         ensure_ascii=False))
        return 1

    target = args.to or _previous_version(r, live)
    if target is None:
        print(json.dumps({"ok": False, "rolled_back": False,
                          "reason": "日志中找不到当前版本的来源（上一版），须 --to 显式指定"},
                         ensure_ascii=False))
        return 1
    if target == live:
        print(json.dumps({"ok": False, "rolled_back": False, "reason": f"目标 {target} 即当前版本"},
                         ensure_ascii=False))
        return 1
    n = count_keys(r, f"{C.NS}:{target}:*")
    if n == 0:
        print(json.dumps({"ok": False, "rolled_back": False,
                          "reason": f"目标空间 {target} 为空/不存在，拒回滚（防线上空数据）"},
                         ensure_ascii=False))
        return 1

    # —— 原子回滚：单条 SET ——
    t0 = time.perf_counter()
    r.set(C.ALIAS_KEY, target)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert r.get(C.ALIAS_KEY) == target

    audit_append(r, C.SWITCH_LOG, {
        "at": now_iso(), "action": "rollback", "from": live, "to": target,
        "target_rows": n,
    })
    print(json.dumps({"ok": True, "rolled_back": True, "from": live, "to": target,
                      "rollback_elapsed_ms": round(elapsed_ms, 3), "target_rows": n},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
