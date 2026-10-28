# -*- coding: utf-8 -*-
"""OneServe 通用点查压测负载画像（P3 压测工具通用化）。

流量画像：
  - 按 LOCUST_METHOD 打 LOCUST_ROUTE（经网关 9080，key-auth：apikey 头）；
  - 请求体＝{LOCUST_KEY_FIELD: <键池随机取样>}——键池由 run_ladder.sh 经
    discover.py 从 Redis tags:<ns>:* SCAN 取样产出（results/<ts>/keys.txt），
    防单键缓存失真；
  - 无思考时间（constant(0)）：并发档口径＝该并发数下的持续最大压力；
  - 计时与状态码逐请求由 locust 记录，百分位由实测响应时间分布算出。

环境变量（必填项无默认值——旧 /v1/tags/query 硬编码已删，目标只由环境决定）：
  LOCUST_ROUTE      被测路由（服务登记解析出的 route，如 /v1/svc/usr-basic-info）
  LOCUST_APIKEY     调用方凭证（APISIX key-auth；secret 不参与调用，不落盘）
  LOCUST_KEYS_FILE  键池文件（discover.py 产出一行一键的纯文本）
  LOCUST_KEY_FIELD  请求体键字段名（取服务登记 params[0].name，缺省 key）
  LOCUST_METHOD     HTTP 方法（取服务登记 method，缺省 POST）
"""
from __future__ import annotations

import os
import random
import re
import sys

from locust import HttpUser, constant, task

ROUTE = os.environ.get("LOCUST_ROUTE", "")
APIKEY = os.environ.get("LOCUST_APIKEY", "")
KEYFILE = os.environ.get("LOCUST_KEYS_FILE", "")
KEY_FIELD = os.environ.get("LOCUST_KEY_FIELD", "key")
METHOD = (os.environ.get("LOCUST_METHOD", "POST") or "POST").upper()

# 服务登记通用入参字符集（params[].pattern 的公共子集；64 位十六进制键天然满足）
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _load_keys(path: str) -> list[str]:
    """键池：纯文本一行一键；剔除空行，字符集非法即整体拒（防脏键池污染压测）。"""
    keys: list[str] = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            k = ln.strip()
            if not k:
                continue
            if not _KEY_RE.match(k):
                raise SystemExit(f"键池含非法键（不符服务登记字符集）：{k[:16]}…")
            keys.append(k)
    if not keys:
        raise SystemExit(f"键池为空：{path}（run_ladder.sh 会先自动取样，请勿手造）")
    return keys


for _name, _val in (("LOCUST_ROUTE", ROUTE), ("LOCUST_APIKEY", APIKEY),
                    ("LOCUST_KEYS_FILE", KEYFILE)):
    if not _val:
        print(f"[loadtest] 缺环境变量 {_name}（由 run_ladder.sh 注入；直跑请自查）",
              file=sys.stderr)
        raise SystemExit(2)

KEYS = _load_keys(KEYFILE)
REQ_NAME = f"{METHOD} {ROUTE}"
print(f"[loadtest] 目标 {REQ_NAME}；键池 {len(KEYS)} 键，来源 {KEYFILE}")


class PointQueryUser(HttpUser):
    """一个并发用户：无思考时间循环点查，键每次随机取样。"""

    wait_time = constant(0)

    def on_start(self):
        self.client.headers["apikey"] = APIKEY

    @task
    def query(self):
        # 超时 30s 兜底：服务端 guard 3s，超过即视为挂死；限制档位间隙的连接排空时间
        #（超时计入失败，且属客户端口径——对账时与审计 delta 的差值来源之一，报告中显式说明）
        self.client.request(
            METHOD,
            ROUTE,
            json={KEY_FIELD: random.choice(KEYS)},
            timeout=30.0,
            name=REQ_NAME,
        )
