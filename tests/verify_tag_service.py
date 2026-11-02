#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OneServe 标签服务端到端验证（task-003）——三 AC 一脚本，输出即证据。

前置：① COMPOSE_PROJECT_NAME=oneserve-003 bash scripts/up.sh（栈已起、路由已初始化）
      ② bash scripts/load_demo_data.sh（演示数据已灌入并切换上线）
用法：python3.13 tests/verify_tag_service.py 2>&1 | tee tests/evidence/task-003-verify.txt

  AC-1  网关 key-auth 下 SM3 键点查：found=true、字段集 ⊆ 授权白名单（红线：越权字段即失败）、
        data_as_of/live_version 正确；无 key 401；不存在键诚实 found=false；非法键 400；批量键 400
  AC-2  脱敏字段按 band_mask 规则输出（与源 CSV 原值按阈值独立复算一致），
        且原值在 fields 序列化文本中零出现
  AC-3  演示页可用：GET /demo 200 含「演示通道：真实接入传 SM3」标注；
        POST /demo/query（手机号→服务端代算 SM3）返回一致结果；curl 口径证据另存
        tests/evidence/task-003-demo-page.txt
"""

import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sync"))
from gen_mock_source import fake_phone, sm3_hex  # noqa: E402  与造数同口径（确定性假号/SM3）

GATEWAY = "http://localhost:9080"
DATA_DATE = "20260920"
DATA_AS_OF = "2026-09-20"
SEED = 42
SRC_CSV = REPO / "sync" / "out" / f"src_task003_{DATA_DATE}.csv"
DEMO_EVIDENCE = REPO / "tests" / "evidence" / "task-003-demo-page.txt"

DOCKER_ENV = {**os.environ,
              "PATH": os.environ.get("PATH", "") + ":" + os.environ["HOME"] + "/.docker/bin"}


def http(method, url, payload=None, headers=None):
    """HTTP 调用，返回 (状态码, 响应体文本)；4xx/5xx 不抛。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def sh(cmd):
    """跑命令（仓库根），返回 stdout。"""
    r = subprocess.run(cmd, shell=True, cwd=REPO, env=DOCKER_ENV,
                       capture_output=True, text=True)
    return r.stdout.strip()


def read_appkey():
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("APISIX_APPKEY="):
                return line.split("=", 1)[1].strip()
    return "oneserve-dev-appkey-001"


def pick_probe_user(whitelist, masked):
    """从源 CSV 选演示用户：两个脱敏字段＋≥2 个普通白名单字段非空（保证 AC-1/AC-2 都有料）。"""
    with open(SRC_CSV, encoding="utf-8") as f:
        for idx, row in enumerate(csv.DictReader(f)):
            tags = {k: v for k, v in row.items() if k != "usr_key_sm3" and v != ""}
            if all(tags.get(m) for m in masked) and \
               len([f for f in whitelist - masked if tags.get(f)]) >= 2:
                return idx, row["usr_key_sm3"], tags
    raise SystemExit("源 CSV 中找不到满足条件的演示用户（填充率过低？重跑 load_demo_data.sh）")


def expected_band(rule, raw):
    """按 services.yaml 阈值独立复算 band_mask 结果（测试不调用被测代码，防自证）。"""
    try:
        v = float(raw)
    except ValueError:
        return rule.get("unknown_out", "U")
    for band in rule["bands"]:
        if "max" not in band or v < float(band["max"]):
            return str(band["out"])
    return str(rule.get("unknown_out", "U"))


def check(cond, msg, errors):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        errors.append(msg)
    return cond


def main():
    errors = []
    appkey = read_appkey()
    services = yaml.safe_load((REPO / "service" / "services.yaml").read_text(encoding="utf-8"))
    matrix = yaml.safe_load((REPO / "service" / "auth_matrix.yaml").read_text(encoding="utf-8"))
    svc = services["services"][0]
    grant = next(t for t in matrix["matrix"] if t["tenant"] == "demo_merchant")
    whitelist = set(next(g["fields"] for g in grant["grants"] if g["service"] == "user_tag_query"))
    rules = svc["response"]["desensitize"]
    masked = set(rules)

    idx, key, tags = pick_probe_user(whitelist, masked)
    phone = fake_phone(SEED, idx)
    print("== OneServe 标签服务端到端验证（task-003）== " + time.strftime("%Y-%m-%d %H:%M:%S"))
    print(f"  演示用户：CSV 第 {idx} 行，phone=**{phone[3:7]}**（明文仅测试内存），"
          f"SM3={key[:16]}…，非空标签 {len(tags)} 个")

    # ---------------- AC-1：SM3 键点查，仅含授权字段 ----------------
    print("== AC-1 网关 key-auth → KV 执行器（投影仅授权字段）==")
    code, body = http("POST", GATEWAY + "/v1/tags/query", {"key": key})
    check(code == 401, f"无 key 调 /v1/tags/query 期望 401（网关 key-auth），实际 {code}", errors)

    code, body = http("POST", GATEWAY + "/v1/tags/query", {"key": key}, {"apikey": appkey})
    ok = check(code == 200, f"带 key 点查期望 200，实际 {code}（body={body[:120]}）", errors)
    resp = json.loads(body) if ok else {}
    if ok:
        fields = resp.get("fields") or {}
        check(resp.get("found") is True, "found=true", errors)
        check(resp.get("data_as_of") == DATA_AS_OF,
              f"data_as_of={resp.get('data_as_of')}（期望 {DATA_AS_OF}）", errors)
        check(resp.get("live_version") == DATA_DATE, f"live_version={resp.get('live_version')}", errors)
        check(resp.get("tenant") == "demo_merchant", "租户定位=demo_merchant", errors)
        # 红线：响应字段必须 ⊆ 白名单，且白名单外字段（用户确实有值）一个都不能出现
        leak = set(fields) - whitelist
        check(not leak, f"响应字段 ⊆ 授权白名单（越权字段零出现；越权={sorted(leak)}）", errors)
        some_unauth = sorted(set(tags) - whitelist)
        check(bool(some_unauth) and not (set(some_unauth) & set(fields)),
              f"越权字段在源数据有值但未泄露（抽验 {some_unauth[0]} 等 {len(some_unauth)} 个）", errors)
        check(len(fields) >= 3, f"返回字段 {len(fields)} 个（演示用户应 ≥3）", errors)
        print(f"  -- 响应字段：{sorted(fields)}")

    code, body = http("POST", GATEWAY + "/v1/tags/query", {"key": "cd" * 32}, {"apikey": appkey})
    r0 = json.loads(body) if code == 200 else {}
    check(code == 200 and r0.get("found") is False and r0.get("fields") is None,
          f"不存在键诚实 found=false/fields=null（实际 {code} {body[:80]}）", errors)

    code, _ = http("POST", GATEWAY + "/v1/tags/query", {"key": "not-hex"}, {"apikey": appkey})
    check(code == 400, f"非法键格式期望 400，实际 {code}", errors)

    code, body = http("POST", GATEWAY + "/v1/tags/query",
                      {"key": [key, "ab" * 32]}, {"apikey": appkey})
    check(code == 400 and "max_items_exceeded" in body,
          f"批量键（guard.max_items_per_call=1）期望 400 max_items_exceeded，实际 {code} {body[:60]}", errors)

    # ---------------- AC-2：band_mask 脱敏，原值零出现 ----------------
    print("== AC-2 band_mask 脱敏（A/B/C 档输出，原值零出现）==")
    if ok:
        for name, rule in rules.items():
            raw = tags.get(name)
            out_val = (resp.get("fields") or {}).get(name)
            check(out_val == expected_band(rule, raw),
                  f"{name}：源值（源 CSV 口径）→ 档位 {out_val}（独立复算一致）", errors)
        fields_text = json.dumps(resp.get("fields"), ensure_ascii=False)
        for name in rules:
            raw = tags.get(name)
            check(raw not in fields_text, f"{name} 原值未出现在 fields（原值零出现）", errors)
        outs = {str(b["out"]) for r in rules.values() for b in r["bands"]} | \
               {str(r.get("unknown_out", "U")) for r in rules.values()}
        check(all((resp.get("fields") or {}).get(m) in outs for m in rules if m in (resp.get("fields") or {})),
              f"脱敏字段值域 ⊆ {sorted(outs)}", errors)

    # ---------------- AC-3：演示页可用 ----------------
    print("== AC-3 演示页（/demo 输手机号 → 出标签表）==")
    code, page = http("GET", GATEWAY + "/demo")
    page_ok = check(code == 200, f"GET /demo 期望 200，实际 {code}", errors)
    if page_ok:
        check("演示通道" in page and "真实接入传 SM3" in page,
              "页面含「演示通道：真实接入传 SM3」标注", errors)
        check("user_tag_query" in page, "页面标注服务 user_tag_query", errors)

    code, body = http("POST", GATEWAY + "/demo/query", {"phone": phone})
    demo_ok = check(code == 200, f"POST /demo/query 期望 200，实际 {code}（body={body[:120]}）", errors)
    if demo_ok:
        d = json.loads(body)
        check(d.get("found") is True and d.get("key") == sm3_hex(phone),
              "演示通道键哈希＝SM3(phone)（服务端代算，同口径）", errors)
        check(set(d.get("fields") or {}) <= whitelist, "演示通道同样只出授权字段", errors)
        check(d.get("data_as_of") == DATA_AS_OF, f"演示通道 data_as_of={d.get('data_as_of')}", errors)

    code, body = http("POST", GATEWAY + "/demo/query", {"phone": "12345"})
    check(code == 400, f"非法手机号期望 400，实际 {code}", errors)

    # 演示页证据文件（AC-3「截图或 curl 输出留证」之 curl 口径）
    if page_ok and demo_ok:
        DEMO_EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# task-003 演示页证据（curl 口径） {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "$ curl -s http://localhost:9080/demo | head -c 600",
            page[:600],
            "",
            "$ curl -s -X POST http://localhost:9080/demo/query "
            "-H 'Content-Type: application/json' -d '{\"phone\": \"**********\"}'",
            "# （演示手机号明文不落证据文件——安全红线口径；键哈希可复算对答案）",
            json.dumps(d, ensure_ascii=False, indent=2),
        ]
        DEMO_EVIDENCE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  -- 演示页证据已存 {DEMO_EVIDENCE.relative_to(REPO)}")

    # ---------------- 附加：审计七元组抽检 ----------------
    print("== 附加：审计七元组抽检（tags/query 最后一条）==")
    last = sh("docker exec oneserve-service sh -c "
              "'grep \"service:/v1/tags/query\" /data/audit/audit.jsonl | tail -n 1'")
    try:
        rec = json.loads(last)
        check(rec.get("endpoint") == "service:/v1/tags/query" and rec.get("caller") == appkey,
              f"审计最后一条：endpoint={rec.get('endpoint')} caller={rec.get('caller')}", errors)
        check(len(rec.get("response_digest", "")) == 64 and len(rec.get("params_digest", "")) == 64,
              "params/response 均为 sha256 摘要（hash_only 口径）", errors)
    except json.JSONDecodeError:
        check(False, "审计最后一条不是合法 JSON：" + last[:100], errors)

    print("=" * 50)
    if errors:
        print(f"== 验证未过：{len(errors)} 项失败 ==")
        for e in errors:
            print("  ✗ " + e)
        return 1
    print("== 三 AC 全绿 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
