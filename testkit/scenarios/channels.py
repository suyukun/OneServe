# -*- coding: utf-8 -*-
"""S2 三通道对比（设计稿 §3）：同一批键、三种客户端形态各一列。

①HTTP API：裸 POST /v1/tags/query（键池 64 位十六进制）；
②Python SDK：随机手机号 → 客户端规范化＋SM3（sm3pure，官方参考实现口径）→ 调 ①同端点，
  端到端含客户端哈希成本；
③网页通道：POST /demo/query（手机号明文；当前为服务端代算 SM3 口径，前端自算上线后复测刷新）。

口径脚注（面板与 run.json 同步标注）：键池为单向哈希无手机号原文，②③使用随机合规手机号，
KV 未命中路径与命中路径同构（同执行器/投影/审计链路），通道对比看传输与计算开销差异。
"""
from __future__ import annotations

import random

from locust import HttpUser, constant, task

from sm3pure import phone_sm3
from scenarios.common import random_phone


class ApiHttpUser(HttpUser):
    """①HTTP API（裸 POST /v1/tags/query，key-auth）。"""
    wait_time = constant(0)
    abstract = True
    keys: list[str] = []
    appkey: str = ""

    def on_start(self) -> None:
        self.client.headers["apikey"] = self.appkey

    @task
    def query(self) -> None:
        self.client.post("/v1/tags/query", json={"key": random.choice(self.keys)},
                         timeout=30.0, name="①HTTP API /v1/tags/query")


class SdkSm3User(HttpUser):
    """②Python SDK：客户端 SM3（含规范化与哈希成本）后走正式端点。"""
    wait_time = constant(0)
    abstract = True
    appkey: str = ""

    def on_start(self) -> None:
        self.client.headers["apikey"] = self.appkey

    @task
    def query(self) -> None:
        key = phone_sm3(random_phone())   # 客户端口径：规范化＋SM3 纯函数（官方参考实现移植）
        self.client.post("/v1/tags/query", json={"key": key},
                         timeout=30.0, name="②Python SDK(客户端SM3)")


class WebChannelUser(HttpUser):
    """③网页通道：POST /demo/query（服务端代算 SM3；M0 演示页口径）。"""
    wait_time = constant(0)
    abstract = True

    @task
    def query(self) -> None:
        self.client.post("/demo/query", json={"phone": random_phone()},
                         timeout=30.0, name="③网页通道 /demo/query")
