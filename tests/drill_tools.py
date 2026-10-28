# -*- coding: utf-8 -*-
"""演练专用工具（task-006）：失败注入与版本清理——只服务演练脚本，绝不进生产链路。

子命令：
  drop-keys      注入 a：从新空间确定性偷删 N 键（模拟加载丢行 → 查一行数红）
  tamper-sampled 注入 b：篡改 N 个抽样内用户的标签值（→ 查二抽样逐值红）
  strip-columns  注入 c：非抽样用户剥离前 N 个 ac_ 列（→ 查三覆盖率偏差红，且查二仍绿）
  purge-version  清理：整版本空间＋元信息出库（演练收尾用）

抽样口径与 reconcile 完全同源（common.sample_indices），保证注入位置在对账视野内。
用法（仓库根）：python3.13 tests/drill_tools.py <子命令> …
"""
import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync"))  # 定位同步模块

import sync_config as C
from common import data_key, redis_conn, sample_indices, scan_keys


def _load_src(csv_path):
    """读源 CSV → (keys, rows非空标签)。"""
    keys, rows = [], []
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            keys.append(row.pop(C.KEY_COLUMN))
            rows.append({k: v for k, v in row.items() if v != ""})
    return keys, rows


def cmd_drop_keys(a):
    r = redis_conn()
    pattern = f"{C.NS}:{a.version}:*"
    keys = sorted(scan_keys(r, pattern))
    victims = random.Random(a.seed).sample(keys, a.n)
    pipe = r.pipeline(transaction=False)
    for k in victims:
        pipe.delete(k)
    pipe.execute()
    print(json.dumps({"dropped": len(victims),
                      "remaining": len(scan_keys(r, pattern))}, ensure_ascii=False))


def cmd_tamper_sampled(a):
    keys, _ = _load_src(a.csv)
    idx = sample_indices(len(keys))
    r = redis_conn()
    tampered = []
    for i in idx:
        if len(tampered) >= a.n_keys:
            break
        k = data_key(a.version, keys[i])
        raw = r.get(k)
        if raw is None:
            continue
        tags = json.loads(raw)
        if not tags:  # 空标签用户无值可篡改，换下一个抽样行
            continue
        field = sorted(tags)[0]
        tags[field] = "TAMPERED_BY_DRILL"  # 逐值比对必炸的哨兵值
        r.set(k, json.dumps(tags, ensure_ascii=False, separators=(",", ":")))
        tampered.append({"row": i, "key": keys[i][:16] + "…", "field": field})
    print(json.dumps({"tampered": tampered}, ensure_ascii=False))


def cmd_strip_columns(a):
    keys, _ = _load_src(a.csv)
    sampled = {keys[i] for i in sample_indices(len(keys))}
    strip = [n for n, _ in __import__("common").load_schema()
             if n.startswith("ac_")][:a.n_cols]
    strip_set = set(strip)
    r = redis_conn()
    pattern = f"{C.NS}:{a.version}:*"
    stripped = 0
    pipe = r.pipeline(transaction=False)
    pending = 0
    for k in scan_keys(r, pattern):
        sm3 = k.split(":")[-1]
        if sm3 in sampled:
            continue  # 抽样键不动：保查二绿，让查三独立见红
        raw = r.get(k)
        if raw is None:
            continue
        tags = json.loads(raw)
        new = {f: v for f, v in tags.items() if f not in strip_set}
        pipe.set(k, json.dumps(new, ensure_ascii=False, separators=(",", ":")))
        stripped += 1
        pending += 1
        if pending % 1000 == 0:
            pipe.execute()
            pipe = r.pipeline(transaction=False)
            pending = 0
    pipe.execute()
    print(json.dumps({"stripped_keys": stripped, "stripped_fields": len(strip),
                      "first_field": strip[0], "last_field": strip[-1],
                      "note": "仅非抽样键（保证查二仍绿、查三独立见红）"}, ensure_ascii=False))


def cmd_purge_version(a):
    r = redis_conn()
    pattern = f"{C.NS}:{a.version}:*"
    keys = scan_keys(r, pattern)
    pipe = r.pipeline(transaction=False)
    for k in keys:
        pipe.delete(k)
    pipe.delete(C.VER_META.format(version=a.version))
    pipe.delete(C.RECON_META.format(version=a.version))
    pipe.execute()
    print(json.dumps({"purged_keys": len(keys), "version": a.version}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="演练注入/清理工具（非生产件）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("drop-keys", help="偷删 N 键（行数注入）")
    p.add_argument("--version", required=True)
    p.add_argument("--n", type=int, default=37)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(fn=cmd_drop_keys)

    p = sub.add_parser("tamper-sampled", help="篡改抽样内用户值（逐值注入）")
    p.add_argument("--csv", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--n-keys", type=int, default=3)
    p.set_defaults(fn=cmd_tamper_sampled)

    p = sub.add_parser("strip-columns", help="剥离非抽样用户的 ac_ 列（覆盖率注入）")
    p.add_argument("--csv", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--n-cols", type=int, default=65)
    p.set_defaults(fn=cmd_strip_columns)

    p = sub.add_parser("purge-version", help="清空整版本空间＋元信息")
    p.add_argument("--version", required=True)
    p.set_defaults(fn=cmd_purge_version)

    a = ap.parse_args()
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
