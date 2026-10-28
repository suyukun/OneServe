#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""meta_reconcile 对账门禁套件（卡-1b7 步7 · 全 hermetic tmp_path，零 docker 零 dev 容器组）。

任务单口径两例＋守卫方向补证：
  正例  两方一致 exit 0（基线冻结后源 vs 快照全 OK）；
  负例  篡改快照一条 exit 2（[DIFF] 明细打印到表与字段）；
  补证  源漂移即红（守卫方向＝冻结后源 yaml 手编被抓住）／--refresh-baseline 重冻转绿／
        严格模式快照缺失 exit 2／非法 META_WRITER 值回落 yaml 两方。
源目录用合成最小四源（规划器全链真实跑；不碰仓内 service/ 真文件）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

SECRET_HASH = "sha256:" + "3" * 64
SERVICES_YAML = """\
# 测试头注（运行时区）
services:
  - service_id: v1-svc-con-demo
    name: 对账测试运行时服务
    route: /v1/svc/con-demo
    method: POST
    key: usr_id
    field_space: news
    fields:
    - name: age_band
      sample: A
      desc: ''
      desensitize: ''
    status: offline
# ==== console_services（测试写区） ====
console_services:
  - id: v1-svc-con-demo
    name: 对账测试写区服务
    route: /v1/svc/con-demo
    method: POST
    key: usr_id
    field_space: news
    fields:
    - name: age_band
      sample: A
      desc: ''
      desensitize: ''
    status: published
"""
UPSTREAMS_YAML = """\
# 测试头注（upstreams）
meta:
  updated: '2026-10-20T00:00:00+00:00'
  updated_by: console
proxies:
- id: proxy-con-demo
  service_name: 对账测试代理
  route: /proxy/con/demo
  method: POST
  upstream_url: http://localhost:8000/v1/svc/con-demo
  timeout_sec: 5
  retry_count: 1
  reuse_upstream: false
  upstream_name: proxy-con-demo-up
  status: draft
  scope:
    env: 测试
    instance: 实例A
"""

APPKEYS_YAML = """\
# 测试头注（appkeys）
meta:
  updated: '2026-10-20T00:00:00+00:00'
  updated_by: console
keys:
- appkey: sb_reconkey000001
  secret_hash: %s
  tenant: tkeysandbox01
  caller_name: recon-test
  app_name: 对账测试应用
  key_type: sandbox
  oa_ticket: ''
  tag_sets: []
  quota_qps: 50
  quota_daily: 10000
  status: active
  expires_at: 2099-01-01
  issued_at: '2026-10-20T00:00:00+00:00'
  grants:
  - service: v1-svc-con-demo
    endpoint: /v1/svc/con-demo
    fields:
    - age_band
""" % SECRET_HASH

AUTH_MATRIX_YAML = """\
# 测试头注（matrix）
matrix:
- tenant: democon01
  tenant_type: external
  oa_ticket: OA-TEST-0001
  display_name: 对账测试租户
  grants:
  - service: v1-svc-con-demo
    fields:
    - age_band
    endpoint: /v1/svc/con-demo
"""
def _make_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "registry").mkdir(parents=True)
    (src / "services.yaml").write_text(SERVICES_YAML, encoding="utf-8")
    (src / "upstreams.yaml").write_text(UPSTREAMS_YAML, encoding="utf-8")
    (src / "registry" / "appkeys.yaml").write_text(APPKEYS_YAML, encoding="utf-8")
    (src / "auth_matrix.yaml").write_text(AUTH_MATRIX_YAML, encoding="utf-8")
    return src


def _cli(src: Path, snap: Path, *args, extra_env=None):
    env = dict(os.environ)
    env["META_MIGRATE_SRC_DIR"] = str(src)
    env["META_SNAPSHOT_DIR"] = str(snap)
    env.pop("META_WRITER", None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(REPO / "scripts" / "meta_reconcile.py"), *args],
                          capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120)


def _freeze_baseline(tmp_path: Path):
    src, snap = _make_src(tmp_path), tmp_path / "snap"
    r = _cli(src, snap, "--baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "基线快照已冻结" in r.stdout
    for rel in ("services.yaml", "upstreams.yaml", "registry/appkeys.yaml",
                "auth_matrix.yaml"):
        assert (snap / rel).is_file(), "基线缺文件：" + rel
    return src, snap
def test_two_way_consistent_exit0(tmp_path: Path):
    """正例：两方一致 exit 0（冻结后复跑全 OK；条数锚点——七域种子/两区合并/两粒度）。"""
    src, snap = _freeze_baseline(tmp_path)
    r = _cli(src, snap, "--baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    for anchor in ("[OK ] field_space: 源 7 行", "[OK ] service_catalog: 源 1 行",
    # 同 id 双区＝写区单键覆盖合并 1 行,
                   "[OK ] credential: 源 1 行", "[OK ] grant: 源 2 行"):
        assert anchor in r.stdout, "对账锚点缺失：" + anchor


def test_tampered_snapshot_exit2(tmp_path: Path):
    """负例：篡改快照一条（quota_qps 50→51）→ exit 2＋[DIFF] credential＋字段级明细。"""
    src, snap = _freeze_baseline(tmp_path)
    ak = snap / "registry" / "appkeys.yaml"
    text = ak.read_text(encoding="utf-8")
    assert "quota_qps: 50" in text
    ak.write_text(text.replace("quota_qps: 50", "quota_qps: 51", 1), encoding="utf-8")
    r = _cli(src, snap)
    assert r.returncode == 2, "篡改未被抓住：" + r.stdout
    assert "[DIFF] credential" in r.stdout
    assert "quota_qps" in r.stdout, "字段级差异明细未打印"
def test_source_drift_turns_red_then_refresh_repairs(tmp_path: Path):
    """守卫方向：冻结后手编源 yaml 写区生效条目即红；--refresh-baseline 重冻转绿。"""
    src, snap = _freeze_baseline(tmp_path)
    svc = src / "services.yaml"
    text = svc.read_text(encoding="utf-8")
    head, tail = text.split("# ==== console_services", 1)
    svc.write_text(head + "# ==== console_services" + tail.replace("对账测试写区服务",
                                                                  "对账测试写区服务（被手编）", 1),
                   encoding="utf-8")
    r = _cli(src, snap)
    assert r.returncode == 2, "源漂移未被抓住（守卫失效）"
    assert "[DIFF] service_catalog" in r.stdout
    r2 = _cli(src, snap, "--refresh-baseline")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert _cli(src, snap).returncode == 0
def test_strict_mode_missing_snapshot_exit2(tmp_path: Path):
    src, snap = _make_src(tmp_path), tmp_path / "snap-absent"
    r = _cli(src, snap)
    assert r.returncode == 2
    assert "快照缺失" in (r.stdout + r.stderr)


def test_invalid_writer_mode_falls_back_to_two_way(tmp_path: Path):
    src, snap = _freeze_baseline(tmp_path)
    r = _cli(src, snap, extra_env={"META_WRITER": "nonsense"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "模式=yaml" in r.stdout
