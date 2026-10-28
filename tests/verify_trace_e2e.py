#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-5 traceId 贯通端到端验证——一次调用全链路同 traceId 可验证（输出即证据）。

前置：bash scripts/up.sh（全套已起、init_routes 已跑——request-id 已挂五条路由）。
用法：python3 tests/verify_trace_e2e.py 2>&1 | tee tests/evidence/p0-5-verify.txt

  AC-1  网关全链路（200）：带 key 调 /v1/ping → 响应头 X-Trace-Id（网关 request-id
        生成）→ 审计 JSONL/WAL grep 到同 trace_id（八元组第八列）；
  AC-2  错误即入口（Stripe 模式）：非法 JSON 调 /v1/news/query → 400 错误体含
        traceId 字段（与响应头同值）→ 审计 grep 到同值；
  AC-3  门户同源链路：经门户 nginx（:8000 /v1/ → 网关）调 /v1/ping → 同上贯通；
  AC-4  客户端自带 traceId：X-Trace-Id 随请求上行 → 网关复用＋服务层透传，
        响应头/审计同值（APISIX 3.18 request-id 对已带同名头复用）；
  AC-5  网关即拒（401）旁路：无 key 调 /v1/ping → 401 且响应头 X-Trace-Id 已生成
        → Redis portal:trace:gw-rejected（Lua 统计钩子旁路）含该 trace_id；
        审计 JSONL 不含（未到 service，如实断点——不硬造记录）；
  AC-6  直连 service（不经网关）：容器内调 /v1/ping → 无上行头时服务层自生成
        traceId → 响应头回传＋审计第八列同值。
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY = "http://localhost:9080"
PORTAL = "http://localhost:8000"
DOCKER_ENV = {**os.environ, "PATH": os.environ.get("PATH", "") + ":" + os.environ["HOME"] + "/.docker/bin"}
GW_REJECT_KEY = "portal:trace:gw-rejected"


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, cwd=REPO, env=DOCKER_ENV,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit("命令失败（" + str(r.returncode) + "）：" + cmd + "\n" + r.stderr)
    return r.stdout.strip()


def read_appkey():
    env = os.path.join(REPO, ".env")
    if os.path.exists(env):
        for line in open(env, encoding="utf-8"):
            if line.startswith("APISIX_APPKEY="):
                return line.split("=", 1)[1].strip()
    return "oneserve-dev-appkey-001"


def http(method, url, headers=None, body=None):
    """HTTP 返回 (状态码, 小写响应头 dict, 响应体文本)；4xx/5xx 不抛异常。"""
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, hdrs, resp.read().decode()
    except urllib.error.HTTPError as exc:
        hdrs = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, hdrs, exc.read().decode()


def audit_grep(trace_id):
    """容器内 grep 审计 JSONL＋WAL，返回命中的行列表（留证含 WAL 现状）。"""
    out = sh("docker compose exec -T service sh -c "
             "'grep -h " + trace_id + " /data/audit/audit.jsonl /data/audit/audit.wal "
             "2>/dev/null || true'", check=False)
    return [l for l in out.splitlines() if l.strip()]


def gw_reject_records():
    """读网关即拒旁路列表（Lua 钩子写；Redis 容器内 redis-cli）。"""
    out = sh("docker compose exec -T redis redis-cli --no-raw LRANGE "
             + GW_REJECT_KEY + " 0 -1", check=False)
    return [l.strip().strip('"') for l in out.splitlines() if l.strip()]


def check(cond, msg, errors):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        errors.append(msg)


def main():
    errors = []
    appkey = read_appkey()
    print("== P0-5 traceId 贯通端到端验证 == " + time.strftime("%Y-%m-%d %H:%M:%S"))
    print("栈留证：")
    print(sh("docker ps --filter name=oneserve- --format '{{.Names}}\t{{.Status}}'"))

    # ---------- AC-1 网关全链路（200） ----------
    print("== AC-1 网关全链路：带 key /v1/ping → 响应头＋审计第八列同值 ==")
    st, hdrs, body = http("GET", GATEWAY + "/v1/ping", {"apikey": appkey})
    tid1 = hdrs.get("x-trace-id", "")
    check(st == 200, f"/v1/ping 状态 200（实际 {st}）", errors)
    check(len(tid1) > 0, f"响应头 X-Trace-Id 已生成（{tid1 or '缺失'}）", errors)
    time.sleep(0.3)  # Lua 旁路与审计 flush 均为异步/后台，稍候再查
    hits = audit_grep(tid1)
    check(len(hits) >= 1, f"审计 JSONL/WAL grep 命中 trace_id（{len(hits)} 行）", errors)
    if hits:
        rec = json.loads(hits[0])
        check(rec.get("trace_id") == tid1, "审计行 trace_id 与响应头同值", errors)
        check(rec.get("endpoint") == "service:/v1/ping", "审计行 endpoint=service:/v1/ping", errors)
        for f in ("ts", "caller", "endpoint", "params_digest", "status",
                  "latency_ms", "response_digest", "trace_id"):
            check(f in rec, f"审计八元组含 {f}", errors)

    # ---------- AC-2 错误即入口（400 错误体注入 traceId） ----------
    print("== AC-2 错误即入口：非法 JSON /v1/news/query → 错误体含 traceId ==")
    st, hdrs, body = http("POST", GATEWAY + "/v1/news/query",
                          {"apikey": appkey, "Content-Type": "application/json"},
                          "{not-json")
    tid2 = hdrs.get("x-trace-id", "")
    try:
        err_body = json.loads(body)
    except ValueError:
        err_body = {}
    check(st == 400, f"/v1/news/query 非法 JSON 状态 400（实际 {st}）", errors)
    check(err_body.get("traceId") == tid2 and tid2,
          f"400 错误体 traceId 与响应头同值（{err_body.get('traceId')} / {tid2}）", errors)
    hits = audit_grep(tid2)
    check(len(hits) >= 1, f"审计 grep 命中该错误调用（{len(hits)} 行）", errors)

    # ---------- AC-3 门户同源链路（nginx → 网关 → service） ----------
    print("== AC-3 门户同源：经 :8000/v1/ 反代网关同链路贯通 ==")
    st, hdrs, body = http("GET", PORTAL + "/v1/ping", {"apikey": appkey})
    tid3 = hdrs.get("x-trace-id", "")
    check(st == 200 and tid3, f"门户链路 200 且 X-Trace-Id 在（{tid3 or '缺失'}）", errors)
    hits = audit_grep(tid3)
    check(len(hits) >= 1, f"门户链路审计 grep 命中（{len(hits)} 行）", errors)

    # ---------- AC-4 客户端自带 traceId 透传 ----------
    print("== AC-4 客户端自带 traceId：网关复用＋服务层透传 ==")
    mine = "p0-5-e2e-passthrough-" + str(int(time.time()))
    st, hdrs, body = http("GET", GATEWAY + "/v1/ping",
                          {"apikey": appkey, "X-Trace-Id": mine})
    check(hdrs.get("x-trace-id", "") == mine,
          f"响应头回传同值（{hdrs.get('x-trace-id')}）", errors)
    hits = audit_grep(mine)
    check(len(hits) >= 1, f"审计 grep 命中自带 trace_id（{len(hits)} 行）", errors)

    # ---------- AC-5 网关即拒（401）旁路 ----------
    print("== AC-5 网关即拒 401：request-id 已生成；Lua 钩子旁路留痕；如实断点 ==")
    st, hdrs, body = http("GET", GATEWAY + "/v1/ping")   # 无 apikey
    tid5 = hdrs.get("x-trace-id", "")
    check(st == 401, f"无 key 状态 401（实际 {st}）", errors)
    check(len(tid5) > 0, f"401 响应头 X-Trace-Id 已生成（{tid5 or '缺失'}）", errors)
    found_bypass = False
    for _ in range(6):   # ngx.timer.at 异步落键，轮询至 ~3s
        if any(tid5 in r for r in gw_reject_records()):
            found_bypass = True
            break
        time.sleep(0.5)
    check(found_bypass, f"旁路 Redis {GW_REJECT_KEY} 含该 trace_id 记录", errors)
    hits = audit_grep(tid5)
    check(len(hits) == 0, "审计 JSONL 不含该 trace_id（未到 service，如实断点不硬造）", errors)

    # ---------- AC-6 直连 service（缺头自生成） ----------
    print("== AC-6 直连 service：无上行头 → 服务层自生成并回传＋落审计 ==")
    out = sh("docker compose exec -T service python -c \""
             "import urllib.request, json; "
             "r = urllib.request.urlopen('http://127.0.0.1:8000/v1/ping', timeout=10); "
             "print(json.dumps({'status': r.status, 'x-trace-id': r.headers.get('X-Trace-Id')}))\"")
    try:
        direct = json.loads(out)
    except ValueError:
        direct = {}
    tid6 = direct.get("x-trace-id", "")
    check(direct.get("status") == 200 and tid6,
          f"直连 200 且服务层自生成 X-Trace-Id（{tid6 or '缺失'}）", errors)
    hits = audit_grep(tid6)
    check(len(hits) >= 1, f"直连审计 grep 命中（{len(hits)} 行）", errors)

    print()
    if errors:
        print("== 验证失败 " + str(len(errors)) + " 项 ==")
        for e in errors:
            print("  ✗ " + e)
        return 1
    print("== P0-5 traceId 贯通验证全过（一次调用全链路同 traceId 可验证）==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
