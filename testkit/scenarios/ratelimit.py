# -*- coding: utf-8 -*-
"""S3 限流触发（设计稿 §3）：双虚拟用户组——A 正常配额持续打＋B 低配额打超限。

讲点＝B 被限、A 无感（单家打不爆平台，限流是产品能力）。面板分列 A/B 组 200 与限流码计数。
前置（真实栈）：网关 limit-count 开启（M1 项，POC 已验证插件可用；M1 前可临时配在演示栈）。
配额数字＝演示假想值，--quota 可调，默认值注明「待定」（维护者 拍板后写入 config）。

实现注意：本类直接继承 HttpUser 而非 TagQueryUser——locust 任务收集沿 MRO 会同时收录
基类与覆写的同名 @task（实测各半混跑），基类版无 catch_response 会让 429 混入错误率。
角色分配：on_start 按共享计数器把前 users_a 个用户设为 A 组、其余 B 组
（locust 按 weight 派发无法钉死每组数量，计数器法保证 A/B 精确切分）。
"""
from __future__ import annotations

import itertools
import random

from locust import HttpUser, constant, task


class GroupCounters:
    """A/B 组 200 与限流码计数（gevent 单循环内递增，免锁）。"""

    def __init__(self) -> None:
        self.a_ok = 0
        self.a_limited = 0
        self.a_err = 0
        self.b_ok = 0
        self.b_limited = 0
        self.b_err = 0

    def line(self) -> str:
        return (f"A组 200×{self.a_ok} 限流×{self.a_limited} 错误×{self.a_err} │ "
                f"B组 200×{self.b_ok} 限流×{self.b_limited} 错误×{self.b_err}")


COUNTERS = GroupCounters()
_ROLE_SEQ = itertools.count(1)


class DualGroupUser(HttpUser):
    """A/B 双组用户：A＝appkey 正常配额；B＝appkey_b 低配额（网关按 consumer 限流）。"""

    wait_time = constant(0)
    abstract = True
    keys: list[str] = []
    appkey: str = ""
    appkey_b: str = ""
    ratelimit_status: int = 429
    users_a: int = 10 ** 9     # A 组名额（driver 注入；on_start 判角色用）

    def on_start(self) -> None:
        self.role = "A" if next(_ROLE_SEQ) <= self.users_a else "B"
        self.client.headers["apikey"] = self.appkey if self.role == "A" else self.appkey_b

    @task
    def query(self) -> None:
        payload = {"key": random.choice(self.keys)}
        name = f"{self.role}组 /v1/tags/query"
        try:
            with self.client.post("/v1/tags/query", json=payload, name=name,
                                  timeout=30.0, catch_response=True) as resp:
                status = getattr(resp, "status_code", 0) or 0
                if status == 200:
                    if self.role == "A":
                        COUNTERS.a_ok += 1
                    else:
                        COUNTERS.b_ok += 1
                    resp.success()
                elif status == self.ratelimit_status:
                    if self.role == "A":
                        COUNTERS.a_limited += 1
                    else:
                        COUNTERS.b_limited += 1
                    resp.success()   # 限流命中是预期行为：不计入错误率，面板分列计数
                else:
                    raise RuntimeError(f"HTTP {status}")
        except Exception:            # 连接异常/非预期码：locust 已计失败事件，这里只进组分列
            if self.role == "A":
                COUNTERS.a_err += 1
            else:
                COUNTERS.b_err += 1
