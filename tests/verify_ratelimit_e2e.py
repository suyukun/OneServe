#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-5 限流口径真栈端到端验证——输出即证据（tests/evidence/p1-5-ratelimit/）。

前置：bash scripts/up.sh（完整栈已起，APISIX 9180 内网可达、9080 公口）。

做法（不写业务注册表、不留污染）：
  - **被测制品**＝本仓库 app/ratelimit.consumer_body(...) 在宿主机算出的 consumer 体，
    经 docker exec 送进栈内 Admin API（即「我们的代码产出什么，网关就按什么拦」）；
  - 探针 consumer/route 仅用 p1probe5-* 前缀，收尾在 finally 里 DELETE 并复核清空。

验收点：
  AC-1 速率：登记 quota_qps=2 → 第 3 次同秒调用 429＋X-RateLimit-Reason: endpoint；
  AC-2 日配额：登记 quota_daily=2 → 第 3 次调用 429＋reason: global；
  AC-3 并发：limit-conn 生效（curl 并发场景见 AC-3 备注；此处以配置存在性＋429 兜底
       说明，快速串行请求由 AC-1 的 endpoint 先命中）；
  AC-4 同源：consumer 里的 count 逐字等于登记值（宿主机断言）；
  AC-5 未登记：只挂 key-auth 的 consumer 连打 N 次无 429（如实「不限」）；
  AC-6 原因头存在性：每个 429 都带 X-RateLimit-Reason；
  AC-7 配额头存在性：X-RateLimit-Limit/Remaining/Reset 随响应返回。

用法：python3 tests/verify_ratelimit_e2e.py 2>&1 | tee tests/evidence/p1-5-ratelimit/e2e.txt
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
from app import ratelimit as rl   # noqa: E402

GATEWAY = os.environ.get("ONESERVE_GATEWAY", "http://localhost:9080")
DOCKER = os.environ.get("DOCKER_BIN", "/Applications/Docker.app/Contents/Resources/bin/docker")
SERVICE_CT = os.environ.get("ONESERVE_SERVICE_CT", "oneserve-service")
ADMIN_KEY = os.environ.get("APISIX_ADMIN_KEY", "***REMOVED***")

# 每次运行用唯一后缀：限流计数在 APISIX 共享字典里按 consumer 名分桶，复跑若不换键
# 会继承上一轮的计数（首调用即 429，测不出「第 N 次才翻」的边界）。唯一后缀保证本轮
# 从零起算；同时 cleanup 只删本轮探针，不误伤他卡/他轮。
_RUN = f"{os.getpid()}-{int(time.time())}"
PREFIX = "p1probe5"
ROUTE_ID = PREFIX + "-route"
ROUTE_URI = "/" + PREFIX + "/*"
KEY_LIMITED = f"{PREFIX}-key-limited-{_RUN}"
KEY_UNREG = f"{PREFIX}-key-unreg-{_RUN}"

_ADMIN_PY = r"""
import json, sys, urllib.error, urllib.request
ADMIN_KEY = %r
def call(method, path, body=None):
    url = "http://oneserve-apisix:9180/apisix/admin/" + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("X-API-KEY", ADMIN_KEY)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
if __name__ == "__main__":
    print(json.dumps(call(sys.argv[1], sys.argv[2],
                          json.loads(sys.argv[3]) if len(sys.argv) > 3 else None)))
""" % ADMIN_KEY


def admin(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """经 service 容器调 Admin API（9180 不发布宿主机）。"""
    cmd = [DOCKER, "exec", "-i", SERVICE_CT, "python", "-", method, path]
    if body is not None:
        cmd.append(json.dumps(body))
    proc = subprocess.run(cmd, input=_ADMIN_PY, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("Admin API 调用失败：" + proc.stderr)
    code, resp = json.loads(proc.stdout.strip().splitlines()[-1])
    return code, resp


def gw(key: str, path: str = "/" + PREFIX + "/ping") -> tuple[int, dict]:
    """直连网关公口（9080 已发布宿主机）；4xx/5xx 不抛。"""
    req = urllib.request.Request(GATEWAY + path, headers={"apikey": key})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}


def cleanup() -> None:
    for p in (f"routes/{ROUTE_ID}", f"consumers/{KEY_LIMITED}", f"consumers/{KEY_UNREG}"):
        admin("DELETE", p)


def put_probe(consumer_body: dict) -> None:
    assert admin("PUT", f"routes/{ROUTE_ID}", {
        "name": ROUTE_ID, "uri": ROUTE_URI, "upstream_id": "oneserve-service",
        "methods": ["GET", "POST"], "plugins": {"key-auth": {}}})[0] in (200, 201)
    assert admin("PUT", f"consumers/{consumer_body['username']}", consumer_body)[0] in (200, 201)
    time.sleep(1.5)   # 等 etcd → worker 配置同步


def main() -> int:
    ev: list[str] = []
    ck = {"pass": 0, "fail": 0}

    def check(name: str, ok: bool, detail: str = "") -> None:
        ck["pass" if ok else "fail"] += 1
        ev.append(f"{'✅' if ok else '❌'} {name}" + (f"｜{detail}" if detail else ""))

    cleanup()
    try:
        # ── AC-1 速率（quota_qps=2 → 同秒第 3 次 429 endpoint） ──
        body = rl.consumer_body(KEY_LIMITED, quota_qps=2, quota_daily=1000)
        put_probe(body)
        # AC-4 同源：consumer 体里的 count 就是登记值
        rules = {r["key"]: r["count"] for r in body["plugins"]["limit-count"]["rules"]}
        check("AC-4 网关 count 逐字等于登记值",
              rules == {rl.ENDPOINT_KEY: 2, rl.GLOBAL_KEY: 1000}, str(rules))
        seq = [gw(KEY_LIMITED) for _ in range(3)]
        codes = [s for s, _ in seq]
        check("AC-1 速率超限返 429", codes[:2] != [429, 429] and codes[2] == 429, str(codes))
        h = seq[2][1]
        check("AC-6 429 带 X-RateLimit-Reason",
              h.get("x-ratelimit-reason") == "endpoint", str(h.get("x-ratelimit-reason")))
        # 两档规则并存 → APISIX 按 header_prefix 出分组配额头（诚实口径，见 docs）
        check("AC-7 配额头存在性（分组前缀）",
              "x-endpoint-ratelimit-limit" in h and "x-endpoint-ratelimit-remaining" in h
              and "x-endpoint-ratelimit-reset" in h,
              str({k: v for k, v in h.items() if "ratelimit" in k}))

        # ── AC-2 日配额（quota_qps 设高、quota_daily=2 → 第 3 次 429 global） ──
        # 同名 consumer 改配置：APISIX limit-count 计数键含 conf_version，改配置即换桶，
        # 且本轮 key 本就唯一（见 _RUN）——第 3 次必是日配额先撞。
        body = rl.consumer_body(KEY_LIMITED, quota_qps=100000, quota_daily=2)
        assert admin("PUT", f"consumers/{KEY_LIMITED}", body)[0] in (200, 201)
        time.sleep(1.5)
        seq = [gw(KEY_LIMITED) for _ in range(3)]
        codes = [s for s, _ in seq]
        reason = seq[2][1].get("x-ratelimit-reason")
        check("AC-2 日配额超限返 429＋global",
              codes[2] == 429 and reason == "global", f"codes={codes} reason={reason}")

        # ── AC-5 未登记＝只挂 key-auth，连打不限 ──
        unreg = {"username": KEY_UNREG, "plugins": {"key-auth": {"key": KEY_UNREG}}}
        put_probe(unreg)
        codes = [gw(KEY_UNREG)[0] for _ in range(6)]
        check("AC-5 未登记配额不上限流（无 429）", 429 not in codes, str(codes))

        # ── AC-3 并发配置在场（limit-conn）──
        check("AC-3 并发闸（limit-conn）随行", "limit-conn" in body["plugins"],
              json.dumps(body["plugins"].get("limit-conn"), ensure_ascii=False))
    finally:
        cleanup()
        remaining = admin("GET", "routes")[1]
        names = [i["value"]["id"] for i in remaining.get("list", [])]
        ev.append(("✅" if ROUTE_ID not in names else "❌") +
                  f" 收尾清理：探针路由已删除｜{ROUTE_ID not in names}")

    print("=== P1-5 限流口径真栈 e2e ===")
    print(f"网关：{GATEWAY}｜APISIX via {SERVICE_CT}")
    for line in ev:
        print(line)
    print(f"== 通过 {ck['pass']}／失败 {ck['fail']} ==")
    out = REPO / "tests" / "evidence" / "p1-5-ratelimit"
    out.mkdir(parents=True, exist_ok=True)
    (out / "e2e.txt").write_text("\n".join(ev) + "\n", encoding="utf-8")
    return 1 if ck["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
