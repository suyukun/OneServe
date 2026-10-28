#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资讯标签造数＋上线（task-007）：模拟资讯 → Redis news 空间（meta 指针约定同用户标签）。

- 键空间以 services.yaml news_tag_query.backend.kv 为单一事实来源（配置驱动，零硬编码）；
- 数据键 tags:news:<版本>:<资讯ID>，value＝{"title":…,"tags":[…]} 紧凑 JSON
  （同 sync/loader.py 的「非空字段紧凑 JSON」存储约定）；
- 版本元信息 sync:ver:news:<版本>（rows/data_as_of/loaded_at），灌完单条 SET 别名
  原子切换上线（语义同 sync/switch.py 的 meta 指针切换）；
- 幂等：SET 天然覆盖，可重复执行（演示口径）；确定性 seed 保证同参复跑同数据。

用法（service 容器内跑：/app/services.yaml 与 redis 均可达）：
  python /sync/gen_news_data.py --count 1000 --version 20260921 --seed 42
"""
import argparse
import json
import random
import sys
import time

import yaml

SERVICES_YAML = "/app/services.yaml"   # 容器内路径（compose 已挂载；镜像内亦有同文件）
SERVICE_ID = "news_tag_query"

# 标签域：任务书点名四类（黄金/期货/基金/保险）＋常规财经类扩充，共 8 类
TAG_POOL = ["黄金", "期货", "基金", "保险", "股票", "债券", "宏观", "外汇"]

# 标题模板：主标签决定题材（演示真实感；槽位由确定性随机填）
TITLE_TEMPLATES = {
    "黄金": "黄金ETF连获{n}日净流入，避险需求支撑金价走强",
    "期货": "{topic}主力合约收{n}%，交易所发布风险提示",
    "基金": "{n}只{topic}产品年内收益转正，发行端回暖",
    "保险": "{topic}新单保费增{n}%，银保渠道占比回升",
    "股票": "两市成交额{n}0亿元，北向资金净流入收窄",
    "债券": "{n}年期国债收益率下行，{topic}配置盘加码",
    "宏观": "{n}月PMI回升至荣枯线上方，{topic}预期改善",
    "外汇": "在岸人民币兑美元收报{n}.{n1}，{topic}波动收窄",
}


def gen_records(count: int, seed: int) -> list[dict]:
    """确定性造数：资讯 ID（n+6位序号）/标题/标签集（1~4 个，主标签打头）。"""
    rng = random.Random(seed)
    records = []
    for i in range(count):
        nid = f"n{i + 1:06d}"
        primary = rng.choice(TAG_POOL)
        extra = rng.sample(TAG_POOL, rng.randint(0, 3))
        tags = [primary] + [t for t in extra if t != primary]
        title = TITLE_TEMPLATES[primary].format(
            n=rng.randint(2, 60), n1=rng.randint(0, 9), topic=rng.choice(TAG_POOL))
        records.append({"id": nid, "title": title, "tags": tags})
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟资讯 → Redis news 空间并切 meta 指针")
    ap.add_argument("--count", type=int, default=1000, help="造数条数（默认 1000）")
    ap.add_argument("--version", required=True, help="数据版本＝数据日期（YYYYMMDD）")
    ap.add_argument("--seed", type=int, default=42, help="确定性随机种子（默认 42）")
    args = ap.parse_args()

    # 键空间单一事实来源＝services.yaml（配置驱动；缺失即启动失败，不静默造默认值）
    services = {s["service_id"]: s
                for s in yaml.safe_load(open(SERVICES_YAML, encoding="utf-8"))["services"]}
    if SERVICE_ID not in services:
        print(json.dumps({"ok": False, "error": f"services.yaml 未注册 {SERVICE_ID}"},
                         ensure_ascii=False))
        return 1
    kv = services[SERVICE_ID]["backend"]["kv"]

    import redis  # 容器内可用（service 镜像已装）
    r = redis.Redis.from_url(
        __import__("os").environ.get("SYNC_REDIS_URL", "redis://oneserve-redis:6379/0"),
        decode_responses=True)
    r.ping()

    t0 = time.perf_counter()
    records = gen_records(args.count, args.seed)
    pipe = r.pipeline(transaction=False)
    for i, rec in enumerate(records, 1):
        # data_key 模板的 {sm3} 是执行器统一插槽名，news 域插槽值为资讯 ID 明文
        pipe.set(kv["data_key"].format(version=args.version, sm3=rec["id"]),
                 json.dumps({"title": rec["title"], "tags": rec["tags"]},
                            ensure_ascii=False, separators=(",", ":")))
        if i % 500 == 0:
            pipe.execute()
            pipe = r.pipeline(transaction=False)
    pipe.execute()

    data_as_of = f"{args.version[:4]}-{args.version[4:6]}-{args.version[6:8]}"
    r.hset(kv["version_meta_key"].format(version=args.version), mapping={
        "rows": len(records), "tag_cols": len(TAG_POOL), "data_as_of": data_as_of,
        "loaded_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "key_algo": "plaintext(资讯ID明文)", "seed": args.seed,
    })
    r.set(kv["alias_key"], args.version)             # 单条 SET＝原子切换上线

    print(json.dumps({
        "ok": True, "version": args.version, "count": len(records),
        "alias": kv["alias_key"], "data_as_of": data_as_of,
        "sample_ids": [rec["id"] for rec in records[:3]],
        "sample_tags": records[0]["tags"],
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
