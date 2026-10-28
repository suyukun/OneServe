#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OneServe M0 骨架验证（task-002）——三 AC 一脚本，输出即证据。

前置：bash scripts/up.sh（全套已起、路由已初始化）。
用法：python3 tests/verify_m0_skeleton.py 2>&1 | tee tests/evidence/task-002-verify.txt

  AC-1  docker compose up -d 后各组件健康检查全绿（docker ps 输出留证）
  AC-2  端到端：无 key 调网关 401；带 key 200 且响应含 redis 读取的 ping 值
  AC-3  审计 50 条写入 → 重启 service 容器 → 50 条全在（零丢失，WAL 已清）
        （每次带 key 的 /v1/ping 调用即一条审计七元组，见 service/app/audit.py）
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATEWAY = "http://localhost:9080/v1/ping"
AUDIT_N = 50

DOCKER_ENV = {**os.environ, "PATH": os.environ.get("PATH", "") + ":" + os.environ["HOME"] + "/.docker/bin"}


def sh(cmd, check=True):
    """跑命令（在仓库根），返回 stdout。"""
    r = subprocess.run(cmd, shell=True, cwd=REPO, env=DOCKER_ENV,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit("命令失败（" + str(r.returncode) + "）：" + cmd + "\n" + r.stderr)
    return r.stdout.strip()


def read_appkey():
    """从 .env 读调用方 AppKey（缺省回退开发占位值）。"""
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("APISIX_APPKEY="):
                return line.split("=", 1)[1].strip()
    return "oneserve-dev-appkey-001"


def http_get(url, headers=None):
    """GET 返回 (状态码, 响应体)；4xx/5xx 不抛异常。"""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def audit_count():
    """容器内数审计 JSONL 行数与 WAL 字节数。"""
    out = sh("docker compose exec -T service sh -c "
             "'wc -l < /data/audit/audit.jsonl 2>/dev/null || echo 0; "
             "wc -c < /data/audit/audit.wal 2>/dev/null || echo 0'")
    lines_raw, wal_raw = out.splitlines()
    return int(lines_raw), int(wal_raw)


def wait_service_healthy(timeout_s=90):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = sh("docker inspect -f '{{.State.Health.Status}}' oneserve-service")
        if st == "healthy":
            return True
        time.sleep(2)
    return False


def check(cond, msg, errors):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        errors.append(msg)


def main():
    errors = []
    appkey = read_appkey()

    print("== OneServe M0 骨架验证（task-002）== " + time.strftime("%Y-%m-%d %H:%M:%S"))

    # ---------- AC-1：健康检查全绿 ----------
    print("== AC-1 各组件健康检查 ==")
    for svc in ("etcd", "redis", "apisix", "service"):
        st = sh("docker inspect -f '{{.State.Health.Status}}' oneserve-" + svc, check=False)
        check(st == "healthy", "oneserve-" + svc + ": " + (st or "不存在"), errors)
    print("-- docker ps 留证 --")
    print(sh("docker ps --filter name=oneserve- --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"))

    # ---------- AC-2：端到端 401 / 200 ----------
    print("== AC-2 端到端（网关 key-auth → service → redis）==")
    code, body = http_get(GATEWAY)
    print("  无 key  调网关 -> " + str(code))
    check(code == 401, "无 key 期望 401，实际 " + str(code), errors)
    code, body = http_get(GATEWAY, {"apikey": appkey})
    print("  带 key  调网关 -> " + str(code) + "  body=" + body)
    check(code == 200, "带 key 期望 200，实际 " + str(code), errors)
    try:
        ping_value = json.loads(body).get("ping", "")
    except json.JSONDecodeError:
        ping_value = ""
    check(bool(ping_value) and ping_value.startswith("pong"),
          "响应须含 redis 读取的 ping 值（pong-*），实际 " + repr(ping_value), errors)

    # ---------- AC-3：审计 50 条 → 重启 → 零丢失 ----------
    print("== AC-3 审计写入 " + str(AUDIT_N) + " 条 → 重启 service → 零丢失 ==")
    before, wal_before = audit_count()
    print("  写入前基线：JSONL=" + str(before) + " 行，WAL=" + str(wal_before) + " 字节")
    ok = 0
    for _ in range(AUDIT_N):
        code, _ = http_get(GATEWAY, {"apikey": appkey})
        ok += code == 200
    mid, wal_mid = audit_count()
    print("  写入 " + str(ok) + "/" + str(AUDIT_N) + " 次 200 后：JSONL=" + str(mid)
          + " 行（+" + str(mid - before) + "），WAL=" + str(wal_mid) + " 字节")
    check(ok == AUDIT_N and mid == before + AUDIT_N,
          "写入后应为基线+" + str(AUDIT_N) + "，实际 " + str(mid), errors)

    print("  -- 重启 oneserve-service 容器 --")
    print(sh("docker compose restart service"))
    healthy = wait_service_healthy()
    check(healthy, "重启后 service 恢复 healthy", errors)
    after, wal_after = audit_count()
    print("  重启后：JSONL=" + str(after) + " 行（期望 " + str(before + AUDIT_N)
          + "），WAL=" + str(wal_after) + " 字节")
    check(after == before + AUDIT_N,
          "重启后审计应零丢失（期望 " + str(before + AUDIT_N) + "，实际 " + str(after) + "）", errors)
    check(wal_after == 0, "WAL 应已清理（0 字节），实际 " + str(wal_after), errors)

    # 七元组结构抽检（最后一条须含 7 个业务字段＋seq）
    print("== 附加：审计七元组结构抽检（最后一条）==")
    last = sh("docker compose exec -T service sh -c 'tail -n 1 /data/audit/audit.jsonl'")
    try:
        rec = json.loads(last)
        fields = ["ts", "caller", "endpoint", "params_digest", "status", "latency_ms", "response_digest"]
        dump = {f: rec.get(f) for f in fields + ["seq"]}
        check(all(f in rec for f in fields) and "seq" in rec,
              "七元组字段齐备：" + json.dumps(dump, ensure_ascii=False), errors)
    except json.JSONDecodeError:
        check(False, "最后一条审计记录不是合法 JSON：" + last[:120], errors)

    print("=" * 46)
    if errors:
        print("== 验证未过：" + str(len(errors)) + " 项失败 ==")
        for e in errors:
            print("  ✗ " + e)
        return 1
    print("== 三 AC 全绿 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
