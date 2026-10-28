# -*- coding: utf-8 -*-
"""模拟数仓导出生成器（task-006 · SPEC §4.3 链路头）。

按主宽表 demo_src.tags_wide 297 列结构（sync/schema_usr_label_wide_297.csv，
取自 OntoRun 表元数据普查 CSV，只读）生成 T+1 模拟导出：
（TODO(卡2): 297 宽表字典换源为表级 schema 元数据（装载副产品）；CSV 缓删至卡2 交接（卡5 2026-09-30）——本造数链为演示主路径，卡2 迁移至表级 schema 后此蓝本换源）
  - 首列 usr_key_sm3 ＝ SM3(假手机号)，gmssl 真算（无明文落盘，ADR-0001 口径）；
  - 其余 296 列为标签值，稀疏填充率默认 15%（容量实测中性档）；
  - 同 seed 下用户基线稳定；值随 --date 演化 → 可演练「新版数据变化」。

用法：
  python3.13 sync/gen_mock_source.py --date 20260920 --users 10000 \
      --fill 0.15 --seed 42 --out sync/out/src_20260920.csv
"""
import argparse
import csv
import json
import random
import sys
from pathlib import Path

from gmssl import func as sm3_func
from gmssl import sm3 as sm3_lib

import sync_config as C
from common import load_schema, sha256_file

# 值池（模拟标签取值；中性档单值 ~20B 口径）
_V128_POOL = ["band_A", "band_B", "band_C", "band_D", "band_E", "band_F",
              "lvl_1", "lvl_2", "lvl_3", "lvl_4", "lvl_5",
              "chn_app", "chn_web", "chn_mini", "chn_offline", "chn_api",
              "prov_11", "prov_31", "prov_44", "prov_51", "prov_zz", "y_n"]


def sm3_hex(text):
    """SM3 十六进制摘要（gmssl 真算；国标向量 abc→66c7f0f4… 已验证）。"""
    return sm3_lib.sm3_hash(sm3_func.bytes_to_list(text.encode("utf-8")))


# 真实号段（前 3 位）——用于生成"像真的"手机号，同时保证 idx 一一对应（唯一，不靠随机运气）
PHONE_PREFIXES = (
    "186", "185", "138", "139", "137", "136", "135", "159", "158", "188",
    "189", "131", "132", "133", "155", "156", "176", "177", "150", "152",
)

# 演示位（前 N 个 idx）固定为"好看好记"的号码，含 维护者 的手机号（便于现场用自己号演示）
# 说明：这些号仍走同一套 SM3 口径；平台侧不存明文，仅造数期在内存中构造。
DEMO_PHONES = (
    "18601188543",   # 维护者 本人号（现场演示用）
    "13800138000",   # 经典演示号
    "13900139000",
    "18612345678",
    "13812345678",
    "15912345678",
    "18812345678",
    "13112345678",
    "15512345678",
    "17612345678",
)


def fake_phone(seed, idx):
    """确定性假手机号：11 位大陆号段（仅内存中存在，不落盘）。

    **2026-09-23 修复（手工走查发现）**：原实现第二位取 "3456789"、后 9 位纯随机，
    号码空间仅 7×10^9，按生日悖论在 **i≈5.5 万即碰撞**（实测 i=55165 与 32609 相同），
    导致"造 20 万行"必然报 SM3 键碰撞中断。
    修法＝**唯一性由构造保证**（不靠随机运气），并**用真实号段**让号码更逼真：
      - 前 N 个 idx 取 DEMO_PHONES（含 维护者 的号，便于现场演示）；
      - 其余 idx：号段 = PHONE_PREFIXES[idx % len] ，后 8 位 = idx 十进制补零 →
        **idx 唯一 ⇒ 号码唯一**；空间 = 20 号段 × 10^8 = 2×10^9，20 万行余量充足。
    """
    n = int(idx)
    if n < len(DEMO_PHONES):
        return DEMO_PHONES[n]
    body = PHONE_PREFIXES[n % len(PHONE_PREFIXES)] + f"{n:08d}"[-8:]
    return body


def make_value(rng, col_type):
    """按蓝本数据类型造一个模拟标签值（全部以字符串落 CSV，KV 点查即字符串口径）。"""
    if col_type == "integer":
        return str(rng.randint(0, 999_999))
    if col_type == "tinyint":
        return str(rng.choice([0, 1, 1, 2]))  # 状态位，1 居多更像真实
    if col_type == "smallint":
        return str(rng.randint(0, 300))
    if col_type.startswith("character varying(256)"):
        return "-".join(rng.choice(_V128_POOL) for _ in range(3))  # ~30B 长值
    if col_type.startswith("character varying"):
        return rng.choice(_V128_POOL)
    if col_type.startswith("timestamp"):
        return f"2026-{rng.randint(1, 9):02d}-{rng.randint(1, 28):02d} " \
               f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
    return rng.choice(_V128_POOL)  # 兜底（text 列不会走到：usr_id 已剔除）


def main():
    ap = argparse.ArgumentParser(description="生成模拟数仓导出 CSV（键已 SM3）")
    ap.add_argument("--date", required=True, help="数据日期 YYYYMMDD（＝版本号与 data_as_of 口径）")
    ap.add_argument("--users", type=int, default=10_000, help="用户行数（默认 1 万）")
    ap.add_argument("--fill", type=float, default=0.15, help="标签稀疏填充率（默认 0.15＝中性档）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（同 seed 用户基线稳定）")
    ap.add_argument("--out", required=True, help="输出 CSV 路径")
    args = ap.parse_args()

    schema = load_schema()                       # 297 列（含 usr_id）
    tags = [(n, t) for n, t in schema if n != "usr_id"]  # 296 个标签列
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    nonempty_cells = 0
    seen_keys = set()
    with out.open("w", encoding="utf-8", newline="") as f:
        wcsv = csv.writer(f)
        wcsv.writerow([C.KEY_COLUMN] + [n for n, _ in tags])
        for i in range(args.users):
            key = sm3_hex(fake_phone(args.seed, i))
            if key in seen_keys:
                raise RuntimeError(f"SM3 键碰撞（第 {i} 行），种子异常")
            seen_keys.add(key)
            row = [key]
            for name, ctype in tags:
                # 每（用户,列）独立确定性 RNG：同 seed 同日期可复现，换日期值演化
                rng = random.Random(f"{args.seed}:{args.date}:{i}:{name}")
                if rng.random() < args.fill:
                    row.append(make_value(rng, ctype))
                    nonempty_cells += 1
                else:
                    row.append("")
            wcsv.writerow(row)

    summary = {
        "out": str(out),
        "date": args.date,
        "users": args.users,
        "tag_cols": len(tags),
        "fill_target": args.fill,
        "fill_actual": round(nonempty_cells / (args.users * len(tags)), 6),
        "nonempty_per_user_avg": round(nonempty_cells / args.users, 2),
        "sha256": sha256_file(out),
        "bytes": out.stat().st_size,
        "key_algo": "SM3(gmssl 3.2.2 真算，假手机号仅内存生成不落盘)",
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
