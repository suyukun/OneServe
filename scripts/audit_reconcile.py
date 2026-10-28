#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双写对账脚本（AUDIT_PIPELINE=dual 验证用）。

SPEC §5④ 双写切换纪律：dual 模式下 JSONL 与 Kafka 同时写入，本脚本消费
Kafka topic（全新消费组 --from-beginning）与本地 JSONL 条数对账，输出一致/差异报告。
对账一致才允许切 single（kafka）模式。

用法：
  python3 scripts/audit_reconcile.py [--jsonl PATH] [--topic TOPIC] [--bootstrap SERVERS]

踩坑实录对齐（docs/research/本地Kafka测试套验证_20261015.md §四）：
  - bootstrap 用 127.0.0.1（不写 localhost，避免 ::1 怪行为）；
  - 消费组用全新名（--from-beginning），不复用业务消费组 offset；
  - auto-create 关闭时 topic 须已存在（compose.kafka.yml 或手动建）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

DEFAULT_JSONL = os.environ.get("AUDIT_DIR", "/data/audit") + "/audit.jsonl"
DEFAULT_TOPIC = os.environ.get("KAFKA_AUDIT_TOPIC", "oneserve-audit")
DEFAULT_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092")
RECONCILE_GROUP = "audit-reconcile-" + str(int(time.time()))  # 每次全新组


def count_jsonl(path: str) -> tuple[int, list[dict]]:
    """读 JSONL 文件，返回 (条数, 记录列表)。文件不存在返回 (0, [])。"""
    p = Path(path)
    if not p.is_file():
        return 0, []
    records = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return len(records), records


def consume_kafka(topic: str, bootstrap: str, timeout_s: float = 10) -> tuple[int, list[dict]]:
    """消费 Kafka topic（全新组 from-beginning），返回 (条数, 记录列表)。

    kafka-python 不可用或 Kafka 不可达时抛异常（调用方决定如何处理）。
    """
    from kafka import KafkaConsumer

    consumer = KafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        group_id=RECONCILE_GROUP,
        auto_offset_reset="earliest",
        enable_auto_commit=False,       # 对账不改 offset
        max_poll_records=10000,
        request_timeout_ms=5000,
        session_timeout_ms=10000,
    )
    records = []
    deadline = time.monotonic() + timeout_s
    empty_polls = 0
    while time.monotonic() < deadline:
        batch = consumer.poll(timeout_ms=1000)
        if not batch:
            empty_polls += 1
            if empty_polls >= 3:  # 连续 3 次空轮询认为已消费完
                break
            continue
        empty_polls = 0
        for tp, msgs in batch.items():
            for msg in msgs:
                try:
                    records.append(json.loads(msg.value.decode("utf-8")))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    consumer.close()
    return len(records), records


def reconcile(jsonl_path: str, topic: str, bootstrap: str,
              timeout_s: float = 10) -> dict:
    """执行对账：JSONL 条数 vs Kafka 条数＋抽样字段比对。

    返回 {"consistent": bool, "jsonl_count": int, "kafka_count": int,
          "diff_sample": [...], "message": str}。
    """
    j_count, j_records = count_jsonl(jsonl_path)

    try:
        k_count, k_records = consume_kafka(topic, bootstrap, timeout_s)
    except Exception as exc:
        return {
            "consistent": False,
            "jsonl_count": j_count,
            "kafka_count": -1,
            "diff_sample": [],
            "message": f"Kafka 消费失败：{exc}",
        }

    # 条数对账
    if j_count != k_count:
        return {
            "consistent": False,
            "jsonl_count": j_count,
            "kafka_count": k_count,
            "diff_sample": [],
            "message": f"条数不一致：JSONL={j_count} Kafka={k_count}",
        }

    # 抽样字段比对（取 seq 集合交集的前 100 条）
    j_by_seq = {r.get("seq"): r for r in j_records if "seq" in r}
    k_by_seq = {r.get("seq"): r for r in k_records if "seq" in r}
    common_seqs = sorted(set(j_by_seq) & set(k_by_seq))[:100]
    diffs = []
    for seq in common_seqs:
        jr, kr = j_by_seq[seq], k_by_seq[seq]
        for key in ("ts", "caller", "endpoint", "status", "latency_ms", "trace_id"):
            if jr.get(key) != kr.get(key):
                diffs.append({"seq": seq, "field": key,
                              "jsonl": jr.get(key), "kafka": kr.get(key)})
                break

    if diffs:
        return {
            "consistent": False,
            "jsonl_count": j_count,
            "kafka_count": k_count,
            "diff_sample": diffs[:10],
            "message": f"条数一致但字段差异 {len(diffs)} 条（抽样 {len(common_seqs)}）",
        }

    return {
        "consistent": True,
        "jsonl_count": j_count,
        "kafka_count": k_count,
        "diff_sample": [],
        "message": f"对账一致：JSONL={j_count} Kafka={k_count}（抽样 {len(common_seqs)} 条全字段匹配）",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="审计双写对账（JSONL vs Kafka）")
    parser.add_argument("--jsonl", default=DEFAULT_JSONL, help="JSONL 文件路径")
    parser.add_argument("--topic", default=DEFAULT_TOPIC, help="Kafka topic")
    parser.add_argument("--bootstrap", default=DEFAULT_BOOTSTRAP, help="Kafka bootstrap")
    parser.add_argument("--timeout", type=float, default=10, help="消费超时秒数")
    args = parser.parse_args()

    print(f"对账开始：jsonl={args.jsonl} topic={args.topic} bootstrap={args.bootstrap}")
    result = reconcile(args.jsonl, args.topic, args.bootstrap, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["consistent"] else 1


if __name__ == "__main__":
    sys.exit(main())
