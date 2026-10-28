# -*- coding: utf-8 -*-
"""公共画像与键池（loadtest/locustfile.py 口径移植，commit 10e5c81——非 import，复制改写）。"""
from __future__ import annotations

import csv
import random

from locust import HttpUser, constant, task

HEX64 = set("0123456789abcdef")


def load_keys(path: str) -> list[str]:
    """键池：CSV 取首列（跳表头），或纯文本一行一键；只收 64 位十六进制。"""
    keys: list[str] = []
    with open(path, encoding="utf-8") as f:
        if path.endswith(".csv"):
            for row in csv.reader(f):
                if row:
                    k = row[0].strip().lower()
                    if len(k) == 64 and all(c in HEX64 for c in k):
                        keys.append(k)
        else:
            for ln in f:
                k = ln.strip().lower()
                if len(k) == 64 and all(c in HEX64 for c in k):
                    keys.append(k)
    if not keys:
        raise SystemExit(f"键池为空：{path}（真实栈需先跑 scripts/load_demo_data.sh 造数；演示可用 --mock）")
    return keys


def random_phone() -> str:
    """11 位大陆号段随机号（1[3-9] 开头：首位固定 1、次位 3-9、再 9 位数字）——S2 SDK/网页通道输入口径。"""
    return "1" + random.choice("345789") + "".join(random.choices("0123456789", k=9))


class TagQueryUser(HttpUser):
    """一个并发用户：无思考时间循环打标签点查，键每次随机取样（loadtest 同口径）。

    类属性 keys/appkey 由 driver 在 Environment 创建前注入。
    """
    wait_time = constant(0)
    abstract = True
    keys: list[str] = []
    appkey: str = ""

    def on_start(self) -> None:
        self.client.headers["apikey"] = self.appkey

    @task
    def query(self) -> None:
        self.client.post(
            "/v1/tags/query",
            json={"key": random.choice(self.keys)},
            timeout=30.0,
            name="POST /v1/tags/query",
        )
