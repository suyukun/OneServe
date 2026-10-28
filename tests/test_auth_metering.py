#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""授权登记＋计量报表测试（task-008，纯 stdlib 直跑或 pytest 收集均可）。

四组断言（对应任务书）：
  1. 登记成功：带 OA 单号登记 → 矩阵即时更新、新发 appkey/secret、
     secret 不落盘（矩阵只存 sha256 摘要）、重复登记拒绝；
  2. OA 缺失拒绝：不带 --oa-ticket → 非 0 退出、矩阵文件字节不变（ADR-0003）；
  3. appkey 唯一性：批量生成 500 把全唯一、格式合法、secret 长度合规；
  4. 计量 CSV 计数：对样例审计跑报表，逐格计数与测试内独立重算一致、
     汇总行 == 审计总条数。

安全纪律：测试只断言 secret 的长度与格式（正则），不把 secret 写入
任何文件或快照；断言用的正则匹配值不打印。
"""

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures"

APPKEY_RE = re.compile(r"^ak_[0-9a-f]{16}$")
SECRET_RE = re.compile(r"^sk_[A-Za-z0-9_-]{43}$")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_admin(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "auth_admin.py"), *args],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


def _run_metering(audit: Path, matrix: Path, out_dir: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "metering_report.py"), str(audit),
         "--matrix", str(matrix), "--out-dir", str(out_dir)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    rows = [line.split(",") for line in lines]
    return rows[0], rows[1:]


# ----------------------------------------------------------------------
# 1) 登记成功：矩阵即时更新 ＋ secret 一次性显示不落盘 ＋ 重复登记拒绝
# ----------------------------------------------------------------------

def test_register_success_updates_matrix_and_rejects_duplicate():
    with tempfile.TemporaryDirectory() as d:
        matrix = Path(d) / "m.yaml"
        r = _run_admin("register", "--matrix", str(matrix),
                       "--tenant", "tenant-zeta", "--display-name", "测试合作方",
                       "--service", "user_tag_query", "--endpoint", "/v1/tags/query",
                       "--fields", "phone_sm3,age_band,aum_band", "--oa-ticket", "DEMO-2026-0999")
        assert r.returncode == 0, f"登记应成功：{r.stderr}"

        # 矩阵即时更新：新租户/授权/OA 单号在文件里，且结构校验通过
        text = matrix.read_text(encoding="utf-8")
        assert "tenant-zeta" in text and "user_tag_query" in text and "DEMO-2026-0999" in text
        auth = _load_module("auth_admin_under_test", SCRIPTS / "auth_admin.py")
        data = auth.load_matrix_file(matrix)
        errors, _ = auth.validate_matrix(data)
        assert errors == [], f"登记后的矩阵应过校验：{errors}"

        # 凭证输出：appkey/secret 只在 stdout 出现一次；格式合规
        assert r.stdout.count("APPKEY=") == 1 and r.stdout.count("SECRET=") == 1
        appkey = re.search(r"APPKEY=(\S+)", r.stdout).group(1)
        secret = re.search(r"SECRET=(\S+)", r.stdout).group(1)
        assert APPKEY_RE.match(appkey), "appkey 应为 ak_+16位十六进制"
        assert SECRET_RE.match(secret), "secret 应为 sk_+43位urlsafe（长度46）"

        row = data["matrix"][0]
        assert row["tenant"] == "tenant-zeta" and row["appkey"] == appkey
        assert row["grants"][0]["fields"] == ["phone_sm3", "age_band", "aum_band"]
        assert row["grants"][0]["oa_ticket"] == "DEMO-2026-0999"
        # consumer 片段：key-auth 格式、只含 appkey、不含 secret 明文
        assert "key-auth" in r.stdout and appkey in r.stdout
        assert "consumers/tenant-zeta" in r.stdout

        # secret 不落盘：矩阵文件只有 sha256 摘要，无明文
        assert secret not in text, "安全红线：secret 明文不得写入矩阵文件"
        assert data["matrix"][0]["secret_hash"].startswith("sha256:")

        # 同租户新服务：复用 appkey、不再显示 secret
        r2 = _run_admin("register", "--matrix", str(matrix),
                        "--tenant", "tenant-zeta", "--service", "news_tag_query",
                        "--endpoint", "/v1/news/query", "--fields", "news_tags",
                        "--oa-ticket", "DEMO-2026-1000")
        assert r2.returncode == 0 and "SECRET=" not in r2.stdout and appkey in r2.stdout
        data2 = auth.load_matrix_file(matrix)
        assert data2["matrix"][0]["appkey"] == appkey, "复用凭证，不应重复发放 appkey"
        assert data2["matrix"][0]["secret_hash"] == row["secret_hash"], "复用凭证不应换发摘要"
        assert len(data2["matrix"][0]["grants"]) == 2

        # 重复登记（同租户×同服务）：拒绝且退出码 3
        r3 = _run_admin("register", "--matrix", str(matrix),
                        "--tenant", "tenant-zeta", "--service", "news_tag_query",
                        "--endpoint", "/v1/news/query", "--fields", "news_tags",
                        "--oa-ticket", "DEMO-2026-1001")
        assert r3.returncode == 3, "重复登记应拒绝（exit 3）"


# ----------------------------------------------------------------------
# 2) OA 缺失拒绝：矩阵文件字节不变（ADR-0003 对策）
# ----------------------------------------------------------------------

def test_register_rejected_without_oa_ticket():
    with tempfile.TemporaryDirectory() as d:
        matrix = Path(d) / "m.yaml"
        # 先做一次合法登记打底
        ok = _run_admin("register", "--matrix", str(matrix),
                        "--tenant", "tenant-iota", "--display-name", "预置租户",
                        "--service", "user_tag_query", "--endpoint", "/v1/tags/query",
                        "--fields", "phone_sm3", "--oa-ticket", "DEMO-2026-0009")
        assert ok.returncode == 0, ok.stderr
        before = hashlib.sha256(matrix.read_bytes()).hexdigest()

        for ticket in (None, ""):  # 完全缺参 ＋ 显式空串，都要拒
            args = ["register", "--matrix", str(matrix), "--tenant", "tenant-kappa",
                    "--service", "news_tag_query", "--endpoint", "/v1/news/query",
                    "--fields", "news_tags"]
            if ticket is not None:
                args += ["--oa-ticket", ticket]
            r = _run_admin(*args)
            assert r.returncode != 0, f"缺 OA 单号必须拒绝（ticket={ticket!r}）"
            assert "OA" in r.stderr, "报错信息应说明 OA 单号必填（ADR-0003）"

        after = hashlib.sha256(matrix.read_bytes()).hexdigest()
        assert before == after, "拒绝登记时矩阵文件必须保持原样（字节级不变）"


# ----------------------------------------------------------------------
# 3) appkey 唯一性与生成规则
# ----------------------------------------------------------------------

def test_appkey_generation_rules_and_uniqueness():
    auth = _load_module("auth_admin_unit", SCRIPTS / "auth_admin.py")
    seen_keys: set[str] = set()
    for _ in range(500):
        key = auth.gen_appkey(seen_keys)
        assert APPKEY_RE.match(key), f"appkey 格式非法：长度/字符集不符"
        assert key not in seen_keys, "appkey 必须全局唯一"
        seen_keys.add(key)
    # 已有 appkey 的排除集必须被避开
    reserved = {"ak_" + "0" * 16}
    for _ in range(50):
        assert auth.gen_appkey(reserved) not in reserved

    seen_secrets: set[str] = set()
    for _ in range(200):
        sec = auth.gen_secret()
        assert SECRET_RE.match(sec) and len(sec) == 46, "secret 应为 sk_+43位urlsafe（长度46）"
        assert sec not in seen_secrets
        seen_secrets.add(sec)
    # 摘要口径：矩阵存的 sha256 可复算
    assert auth.secret_digest("x").startswith("sha256:")
    assert len(auth.secret_digest("x")) == 7 + 64


# ----------------------------------------------------------------------
# 4) 计量 CSV 计数与样例审计条数逐格一致（AC-2）
# ----------------------------------------------------------------------

def test_metering_csv_counts_match_sample_audit():
    audit = FIXTURES / "sample_audit.jsonl"
    matrix = FIXTURES / "sample_auth_matrix.yaml"

    # 测试内独立重算（不经过被测脚本的映射逻辑）：fixture 口径写死在此
    tenant_of = {"ak_a1a1a1a1a1a1a1a1": "tenant-alpha", "ak_b2b2b2b2b2b2b2b2": "tenant-beta"}
    service_of = {"/v1/tags/query": "user_tag_query", "/v1/news/query": "news_tag_query"}
    expect_daily: Counter = Counter()
    expect_monthly: Counter = Counter()
    total = 0
    for line in audit.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        total += 1
        tenant = tenant_of[rec["caller"]]
        service = service_of[rec["endpoint"].rsplit(":", 1)[-1]]
        expect_daily[(tenant, service, rec["ts"][:10])] += 1
        expect_monthly[(tenant, service, rec["ts"][:7])] += 1
    assert total >= 200, "样例审计至少 200 条（任务书）"

    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        r = _run_metering(audit, matrix, out)
        assert r.returncode == 0, r.stderr
        assert "不在授权矩阵" not in r.stderr, "样例数据应与矩阵映射全对上"

        for name, dim, expect in (("metering_daily.csv", "date", expect_daily),
                                  ("metering_monthly.csv", "month", expect_monthly)):
            header, rows = _read_csv(out / name)
            assert header == ["tenant_id", "service_id", dim, "call_count"]
            assert rows[-1] == ["ALL", "ALL", "ALL", str(total)], "汇总行 == 审计总条数"
            got: Counter = Counter()
            for row in rows[:-1]:
                assert len(row) == 4
                got[(row[0], row[1], row[2])] = int(row[3])
            assert got == expect, f"{name} 逐格计数与独立重算不一致"
            assert sum(got.values()) == total, f"{name} 格子合计 == 审计总条数"
            # 逐格金额双写核对（防 Counter 互相抵消的假阳性）
            for key, want in expect.items():
                assert got.get(key) == want, f"{name} 格 {key} 期望 {want} 实 {got.get(key)}"


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("  ✓ " + name)
        except AssertionError as exc:
            print("  ✗ " + name + "：" + str(exc))
            failed.append(name)
    if failed:
        print("== " + str(len(failed)) + " 组失败 ==")
        return 1
    print("== 授权登记＋计量报表测试全绿（" + str(len(tests)) + " 组断言）==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
