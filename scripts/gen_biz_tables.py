#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务化 HBase 测试表造数器（2026-09-29 · 维护者 走查定调后）。

背景：既有单表 oneserve_demo 无业务含义、296 字段不可用；核心链路（HBase→Redis→API）
要先用贴近业务的测试表走通。本脚本产 3 张业务表（字段名/cn_name/业务口径/敏感级钉死）：
  usr_basic_info  用户基本信息（12 列）
  usr_asset_tag   用户资产标签（18 列）
  card_acct_summary 卡账户汇总（14 列）

产物（可重跑，--seed 固定 → 逐字节可复现）：
  - sync/schema_<表名>.csv ×3：字段字典，6 列＝attnum,字段名,数据类型,cn_name,业务口径,
    敏感级（与 schema_usr_label_wide_297.csv 同格式同表头；ops_config.resolve_schema_csv
    按表名优先命中它，发布台每张表用自己字典的中文注释展示）
  （TODO(卡2): 297 宽表字典换源为表级 schema 元数据（装载副产品）；CSV 缓删至卡2 交接（卡5 2026-09-30）——表级 CSV 即 schema 元数据雏形，卡2 起为唯一字典形态）
  - sync/out/src_<表名>_10k.csv ×3：数据（首列 usr_key_sm3＝SM3(手机号)，gmssl 真算，
    与 sync/gen_mock_source.py 同款算法；loader --table 直传灌 HBase）

造数口径：三表共用同一批手机号（前缀 138/186/159 随机、构造期去重），每行字段填充率
约 85%（自然稀疏）；值风格＝分档(A-E)/枚举/日期(YYYY-MM-DD)/天数区间，贴近真实数仓标签。

用法：
  python3.13 scripts/gen_biz_tables.py --seed 42 --users 10000 --fill 0.85
"""
import argparse
import csv
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

from gmssl import func as sm3_func
from gmssl import sm3 as sm3_lib

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "sync"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sync_config as C                                # noqa: E402
from common import sha256_file                         # noqa: E402

PHONE_PREFIXES = ("138", "186", "159")     # 任务书口径：三号段随机
FILL_DEFAULT = 0.85

# 值域规格：("enum", [池]) / ("int", lo, hi) / ("date", lo, hi)
# 枚举池内重复值＝权重（如黑名单 0 居多），更像真实数仓标签。
TABLES: dict[str, dict] = {
    "usr_basic_info": {"cn": "用户基本信息", "fields": [
        ("gender_cd", "性别代码", "character varying(16)",
         "人口属性枚举：0未知 1男 2女", "内部", ("enum", ["0", "1", "2"])),
        ("age_band", "年龄段", "character varying(16)",
         "年龄分档：A<18 B18-25 C26-35 D36-50 E>50", "内部", ("enum", list("BCDCADEBACDCEABDC"))),
        ("city_code", "城市代码", "character varying(16)",
         "开户行所在城市行政代码（4 位枚举）", "内部",
         ("enum", ["1100", "3100", "4401", "5101", "3301", "4403", "3201", "5000", "6101", "4201"])),
        ("open_date", "开户日期", "date",
         "客户首次开户日期（YYYY-MM-DD）", "内部", ("date", "2016-01-01", "2026-09-28")),
        ("cust_level", "客户等级", "character varying(16)",
         "客户分级：L1普通 L2潜力 L3成长 L4高价值 L5高净值", "内部",
         ("enum", ["L1", "L1", "L2", "L2", "L3", "L3", "L4", "L5"])),
        ("risk_level", "风险等级", "character varying(16)",
         "风险承受能力评估：R1保守-R5激进（适当性口径）", "敏感",
         ("enum", ["R1", "R2", "R2", "R3", "R3", "R3", "R4", "R5"])),
        ("edu_level", "学历", "character varying(16)",
         "学历枚举：1初中及以下 2高中/中专 3大专 4本科 5硕士 6博士", "内部",
         ("enum", ["2", "3", "3", "4", "4", "4", "5", "1", "6"])),
        ("occupation_cd", "职业代码", "character varying(16)",
         "职业大类码（2 位枚举，适当性/授信口径）", "敏感",
         ("enum", ["11", "13", "21", "24", "31", "41", "51", "61", "71", "99"])),
        ("marital_sts", "婚姻状况", "character varying(16)",
         "婚姻状况枚举：10未婚 20已婚 30离异 40丧偶 90未知", "敏感",
         ("enum", ["10", "20", "20", "20", "30", "40", "90"])),
        ("channel_cd", "开户渠道", "character varying(16)",
         "首次开户渠道枚举：APP/WEB/MINI/BRANCH/API", "内部",
         ("enum", ["APP", "APP", "APP", "WEB", "MINI", "BRANCH", "API"])),
        ("vip_flag", "VIP标志", "tinyint", "VIP 客户标志：0否 1是", "内部",
         ("enum", ["0", "0", "0", "1"])),
        ("blacklist_flag", "黑名单标志", "tinyint",
         "风控黑名单标志：0否 1是（命中即拦截）", "敏感",
         ("enum", ["0"] * 49 + ["1"])),
    ]},
    "usr_asset_tag": {"cn": "用户资产标签", "fields": [
        ("aum_band", "AUM等级", "character varying(16)",
         "管理资产规模分档：A≥100万 B30-100万 C10-30万 D1-10万 E<1万", "内部",
         ("enum", ["A", "B", "B", "C", "C", "C", "D", "D", "D", "E"])),
        ("dep_bal_band", "存款余额档", "character varying(16)",
         "存款余额分档（口径同 AUM 五档）", "内部",
         ("enum", ["A", "B", "B", "C", "C", "C", "D", "D", "D", "E"])),
        ("wealth_hold_flag", "理财持有", "tinyint", "当前持有理财产品：0否 1是", "内部",
         ("enum", ["0", "0", "1", "1"])),
        ("fund_hold_flag", "基金持有", "tinyint", "当前持有基金产品：0否 1是", "内部",
         ("enum", ["0", "0", "1", "1"])),
        ("ins_hold_flag", "保险持有", "tinyint", "当前持有保单：0否 1是", "内部",
         ("enum", ["0", "0", "0", "1"])),
        ("loan_flag", "贷款标志", "tinyint", "存在未结清贷款：0否 1是", "内部",
         ("enum", ["0", "0", "0", "1"])),
        ("mortgage_flag", "房贷标志", "tinyint", "存在未结清个人住房贷款：0否 1是", "内部",
         ("enum", ["0", "0", "0", "0", "1"])),
        ("credit_limit_band", "信用额度档", "character varying(16)",
         "信用额度分档：A≥10万 B5-10万 C1-5万 D0.3-1万 E<0.3万", "敏感",
         ("enum", ["A", "B", "B", "C", "C", "C", "D", "D", "E"])),
        ("overdue_cnt_band", "逾期次数档", "character varying(16)",
         "近 24 月逾期次数分档：A0次 B1-2次 C3-5次 D6-9次 E≥10次", "敏感",
         ("enum", ["A"] * 6 + ["B", "B", "C", "D", "E"])),
        ("active_m3_flag", "近3月活跃", "tinyint", "近 3 个月有主动交易/登录：0否 1是", "内部",
         ("enum", ["0", "1", "1", "1"])),
        ("dormant_m12_flag", "12月沉睡", "tinyint", "近 12 个月无任何主动行为：0否 1是", "内部",
         ("enum", ["0"] * 3 + ["1"])),
        ("login_cnt_band", "月登录次数档", "character varying(16)",
         "近 1 月登录次数分档：A≥30 B10-29 C3-9 D1-2 E0", "内部",
         ("enum", list("ABCDE") * 2)),
        ("pay_cnt_band", "月支付笔数档", "character varying(16)",
         "近 1 月支付笔数分档：A≥50 B20-49 C5-19 D1-4 E0", "内部",
         ("enum", list("ABCDE") * 2)),
        ("salary_in_flag", "工资代发", "tinyint", "存在代发工资入账：0否 1是", "内部",
         ("enum", ["0", "0", "1"])),
        ("cross_sell_cnt_band", "交叉销售数档", "character varying(16)",
         "持有产品线数量分档：A≥5 B3-4 C2 D1 E0", "内部",
         ("enum", ["A", "B", "B", "C", "C", "D", "E"])),
        ("churn_risk_band", "流失风险档", "character varying(16)",
         "流失倾向模型分档：A极高 B高 C中 D低 E极低", "内部",
         ("enum", ["A", "B", "B", "C", "C", "C", "D", "D", "E"])),
        ("ltv_band", "客户价值档", "character varying(16)",
         "客户生命周期价值分档：A≥10万 B3-10万 C1-3万 D0.1-1万 E<0.1万", "内部",
         ("enum", ["A", "B", "B", "C", "C", "C", "D", "D", "E"])),
        ("mk_response_band", "营销响应档", "character varying(16)",
         "营销响应倾向分档：A高 B较高 C中 D低 E极低", "内部",
         ("enum", ["A", "B", "B", "C", "C", "C", "D", "D", "E"])),
    ]},
    "card_acct_summary": {"cn": "卡账户汇总", "fields": [
        ("card_sts", "卡状态", "character varying(16)",
         "卡状态枚举：1正常 2冻结 3止付 4销户", "内部",
         ("enum", ["1", "1", "1", "1", "1", "2", "3", "4"])),
        ("acct_sts", "账户状态", "character varying(16)",
         "账户状态枚举：1正常 2睡眠 3销户", "内部",
         ("enum", ["1", "1", "1", "1", "2", "3"])),
        ("open_days", "开户时长天数", "integer",
         "开户距今天数 [0,3650]（10 年内）", "内部", ("int", 0, 3650)),
        ("bal_band", "余额档", "character varying(16)",
         "卡账户余额分档：A≥10万 B1-10万 C0.1-1万 D<0.1万 E0", "内部",
         ("enum", ["A", "B", "B", "C", "C", "C", "D", "E"])),
        ("last_txn_days", "距上次交易天数", "integer",
         "最近一笔交易距今天数 [0,730]（2 年内）", "内部", ("int", 0, 730)),
        ("txn_cnt_m1_band", "月交易笔数档", "character varying(16)",
         "近 1 月交易笔数分档：A≥50 B20-49 C5-19 D1-4 E0", "内部",
         ("enum", list("ABCDE") * 2)),
        ("txn_amt_m1_band", "月交易金额档", "character varying(16)",
         "近 1 月交易金额分档：A≥5万 B1-5万 C0.3-1万 D<0.3万 E0", "敏感",
         ("enum", ["A", "B", "B", "C", "C", "C", "D", "E"])),
        ("overseas_txn_flag", "境外交易标志", "tinyint",
         "近 1 年有境外交易：0否 1是", "内部", ("enum", ["0", "0", "0", "1"])),
        ("qpay_flag", "快捷支付绑定", "tinyint",
         "绑定快捷支付（含三方支付）：0否 1是", "内部", ("enum", ["0", "1", "1", "1"])),
        ("annual_fee_waived_flag", "年费减免", "tinyint",
         "本年年费已减免/豁免：0否 1是", "内部", ("enum", ["0", "1", "1"])),
        ("points_band", "积分档", "character varying(16)",
         "当前可用积分分档：A≥10万 B3-10万 C1-3万 D0-1万 E0", "内部",
         ("enum", ["A", "B", "B", "C", "C", "C", "D", "E"])),
        ("install_cnt_flag", "分期标志", "tinyint",
         "存在未结清账单分期/消费分期：0否 1是", "敏感", ("enum", ["0", "0", "1"])),
        ("cash_adv_flag", "取现标志", "tinyint",
         "近 1 年有卡账户取现：0否 1是", "敏感", ("enum", ["0", "0", "0", "1"])),
        ("card_age_band", "卡龄档", "character varying(16)",
         "首卡持卡年限分档：A≥10年 B5-10年 C3-5年 D1-3年 E<1年", "内部",
         ("enum", ["A", "B", "B", "C", "C", "D", "E"])),
    ]},
}


def sm3_hex(text: str) -> str:
    """SM3 十六进制摘要（gmssl 真算；与 sync/gen_mock_source.py 同款实现）。"""
    return sm3_lib.sm3_hash(sm3_func.bytes_to_list(text.encode("utf-8")))


def gen_phones(seed: int, users: int) -> list[str]:
    """三表共用的同一批手机号：前缀 138/186/159 随机、后 8 位随机，构造期去重。"""
    rng = random.Random(f"{seed}:phones")
    seen: set[str] = set()
    while len(seen) < users:
        seen.add(rng.choice(PHONE_PREFIXES) + "".join(rng.choice("0123456789") for _ in range(8)))
    return sorted(seen)   # 排序定型：同 seed 逐字节可复现


def make_value(rng: random.Random, spec: tuple) -> str:
    """按值域规格产一个标签值（字符串口径，与宽表造数同风格）。"""
    kind = spec[0]
    if kind == "enum":
        return rng.choice(spec[1])
    if kind == "int":
        return str(rng.randint(spec[1], spec[2]))
    if kind == "date":
        lo = date.fromisoformat(spec[1])
        hi = date.fromisoformat(spec[2])
        return (lo + timedelta(days=rng.randint(0, (hi - lo).days))).isoformat()
    raise ValueError(f"未知值域规格：{kind}")


def write_schema(table: str, spec: dict, out_dir: Path) -> Path:
    """写字典 CSV（6 列表头与 wide297 字典同格式）。"""
    path = out_dir / f"schema_{table}.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["attnum", "字段名", "数据类型", "cn_name", "业务口径", "敏感级"])
        for i, (name, cn, dtype, cal, sens, _spec) in enumerate(spec["fields"], 1):
            w.writerow([i, name, dtype, cn, cal, sens])
    return path


def gen_table_csv(table: str, spec: dict, phones: list[str], rowkeys: list[str],
                  seed: int, fill: float, out_dir: Path) -> dict:
    """写数据 CSV：首列 rowkey（SM3），其余列按值域规格＋85% 填充。"""
    path = out_dir / "sync" / "out" / f"src_{table}_10k.csv"
    path.parent.mkdir(parents=True, exist_ok=True)   # sync/out/ 按仓库策略不入库（可再生）
    fields = spec["fields"]
    nonempty = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([C.KEY_COLUMN] + [fd[0] for fd in fields])
        for i, rk in enumerate(rowkeys):
            row = [rk]
            for name, _cn, _dt, _cal, _sens, vspec in fields:
                # 每（用户,列）独立确定性 RNG：同 seed 可复现（gen_mock_source 同款）
                rng = random.Random(f"{seed}:{table}:{i}:{name}")
                if rng.random() < fill:
                    row.append(make_value(rng, vspec))
                    nonempty += 1
                else:
                    row.append("")
            w.writerow(row)
    return {"out": str(path), "rows": len(rowkeys), "tag_cols": len(fields),
            "fill_target": fill,
            "fill_actual": round(nonempty / (len(rowkeys) * len(fields)), 6),
            "sha256": sha256_file(path), "bytes": path.stat().st_size}


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 3 张业务化 HBase 测试表（字典＋数据 CSV）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（同 seed 逐字节可复现）")
    ap.add_argument("--users", type=int, default=10_000, help="每表行数（三表共用同一批手机号）")
    ap.add_argument("--fill", type=float, default=FILL_DEFAULT, help="字段填充率（默认 0.85）")
    args = ap.parse_args()

    sync_dir = REPO / "sync"
    phones = gen_phones(args.seed, args.users)
    rowkeys = [sm3_hex(p) for p in phones]
    if len(set(rowkeys)) != len(rowkeys):
        raise RuntimeError("SM3 键碰撞（种子异常）")

    summary = {"seed": args.seed, "users": args.users, "fill_target": args.fill,
               "phone_prefixes": list(PHONE_PREFIXES),
               "key_algo": "SM3(gmssl 真算，与 sync/gen_mock_source.py 同款；明文不落盘)",
               "tables": {}}
    for table, spec in TABLES.items():
        schema_path = write_schema(table, spec, sync_dir)
        summary["tables"][table] = {
            "cn": spec["cn"], "columns": len(spec["fields"]),
            "schema_csv": str(schema_path), **gen_table_csv(
                table, spec, phones, rowkeys, args.seed, args.fill, REPO),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
