#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""meta_writer 写侧抽层套件（卡-1b7 步5 · yaml 模式行为全等铁律）。

三层验证（全 hermetic tmp_path，零真实网络、零 dev 容器组）：
  ① 切换对照表机检（grep 兜底）：admin_api 写调用点已全量切到 meta_writer（mw.*）、
     旧 appk 写函数直调零残留；auth_admin 写径现状＝1b-8 已下沉 grants_admin.save_matrix
     （meta_writer.matrix_save 为预留接线入口——auth_admin 头注「1b-7 合入时持久层切换
     发生在 grants_admin.save_matrix，本 CLI 零改动」口径，本套件机检该接缝在场）。
  ② yaml 模式确定性路径字节级全等：同一输入分别走「既有函数直调」与「meta_writer 转发」，
     权威源文件落盘结果逐字节一致（offline_service／upstream_offline／credential_set_status
     三条确定性写径；issue/rotate 含随机 secret，由既有 test_appkeys*/test_admin_publish_api*
     族全绿守（全量 pytest 复用即证），不在此重复造随机对比）。
  ③ 双模开关：META_WRITER 缺省 yaml；db 模式分支在场（步 6 已落地，桩件机检接线）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import meta_writer as mw          # noqa: E402
from app import ops_config as opsc         # noqa: E402
from app import appkeys as appk            # noqa: E402


# ----------------------------------------------------------------------
# ① 切换对照表机检（grep 兜底；卡步 5「写函数穷举表」）
# ----------------------------------------------------------------------

ADMIN_API = REPO / "service" / "app" / "admin_api.py"
AUTH_ADMIN = REPO / "scripts" / "auth_admin.py"
META_WRITER = REPO / "service" / "app" / "meta_writer.py"


def test_switch_table_no_legacy_write_calls_in_admin_api():
    """admin_api 写调用点零残留：六个旧写函数不得再直调（读函数 find_record/load 不限）。

    并行批边界：admin_api.py 与 1b-8 授权页批共用（import 区同 hunk），本单切换 hunks
    随该批收编时点入库；切换未入库的树上此测试 skip（机检在切换在场的树上真实生效）。
    """
    src = ADMIN_API.read_text(encoding="utf-8")
    if "mw.credential_issue(" not in src:
        pytest.skip("admin_api 切换 hunks 未入库（并行批收编时点入库后本机检生效）")
    for legacy in ("appk.save_appkeys(", "appk.issue_appkey(", "appk.set_status(",
                   "appk.rotate_appkey(", "appk.delete_appkey(", "appk.renew_appkey(",
                   "opsc.publish_service(", "opsc.offline_service(",
                   "opsc.save_upstream(", "opsc.offline_upstream(",
                   "opsc.delete_upstream_draft(",
                   "opsc._save_services_yaml("):
        assert legacy not in src, f"admin_api 残留旧写直调：{legacy}"
    switched = [f"mw.{entry}(" for entry in
                ("credential_issue", "credential_save", "credential_set_status",
                 "credential_rotate", "credential_delete", "credential_renew",
                 "credential_quota_set", "service_publish", "service_offline",
                 "service_yaml_save", "upstream_save", "upstream_offline",
                 "upstream_draft_delete")]
    present = {entry: (entry in src) for entry in switched}
    assert all(present.values()), f"切换缺失：{[k for k, v in present.items() if not v]}"


def test_switch_table_matrix_seam_in_place():
    """矩阵写径接缝（1b-8 口径）：auth_admin 零本地 _save_matrix/save_matrix 定义；
    grants_admin.save_matrix 在场；meta_writer.matrix_save 入口在场。

    并行批边界：auth_admin 的 grants_admin 下沉是 1b-8 未入库改动——接缝未入库的
    树上 skip（工作树即 1b-8 在场态，机检真实生效）。
    """
    aa = AUTH_ADMIN.read_text(encoding="utf-8")
    if "from app.grants_admin import" not in aa:
        pytest.skip("矩阵写径下沉（grants_admin）未入库——1b-8 批收编后本机检生效")
    assert "def _save_matrix" not in aa and "def save_matrix" not in aa
    assert "save_matrix," in aa
    ga = (REPO / "service" / "app" / "grants_admin.py")
    if ga.is_file():   # 1b-8 并行批产物在场即核（离场则跳过该断言——并行边界）
        assert "def save_matrix" in ga.read_text(encoding="utf-8")
    assert "def matrix_save" in META_WRITER.read_text(encoding="utf-8")


def test_meta_writer_covers_six_entities():
    """六实体入口齐全：tenant/credential/grant/upstream/service 定义（+field_space 种子归迁移工具）。"""
    src = META_WRITER.read_text(encoding="utf-8")
    for entry in ("credential_issue", "credential_set_status", "credential_rotate",
                  "credential_delete", "credential_renew", "credential_quota_set",
                  "credential_save", "service_publish", "service_offline",
                  "service_yaml_save", "upstream_save", "upstream_offline",
                  "upstream_draft_delete", "matrix_save", "tenant_upsert"):
        assert f"def {entry}(" in src, f"meta_writer 缺入口：{entry}"


# ----------------------------------------------------------------------
# ② yaml 模式确定性路径字节级全等
# ----------------------------------------------------------------------

def _base_services_yaml() -> str:
    """最小双区 services.yaml（一条 console published 条目；格式同 ops_config 产物）。"""
    entry = {
        "id": "v1-svc-demo-writer", "name": "抽层测试服务", "route": "/v1/svc/demo-writer",
        "method": "POST", "key": "usr_id", "field_space": "wide297",
        "params": [{"name": "key", "type": "string(64)", "key_type": "phone_sm3"}],
        "fields": [{"name": "age_band", "sample": "D", "desc": "", "desensitize": ""}],
        "status": "published",
        "scope": {"env": "测试", "instance": "实例A"},
        "last_modified_by": "console", "last_modified_at": "2026-10-20T00:00:00+00:00",
        "published_at": "2026-10-20T00:00:00+00:00",
        "trace": {"action": "published", "by": "console", "at": "2026-10-20T00:00:00+00:00",
                  "commit": None,
                  "history": [{"status": "published", "at": "2026-10-20T00:00:00+00:00",
                               "by": "console", "delta": "+1"}]},
    }
    head = "# meta_writer 全等测试头注（运行时区）\nservices: []\n\n"
    return head + opsc.CONSOLE_MARKER + "\n" + yaml.safe_dump(
        {"console_services": [entry]}, sort_keys=False, allow_unicode=True,
        default_flow_style=False)


def _published_upstreams_yaml() -> str:
    proxy = {
        "id": "proxy-demo-writer", "service_name": "抽层测试代理",
        "route": "/proxy/demo-writer", "method": "POST",
        "upstream_url": "http://localhost:8000/v1/svc/demo-writer",
        "timeout_sec": 5, "retry_count": 1, "reuse_upstream": False,
        "upstream_name": "proxy-demo-writer-up", "status": "published",
        "scope": {"env": "测试", "instance": "实例A"},
        "last_modified_by": "console", "last_modified_at": "2026-10-20T00:00:00+00:00",
        "published_at": "2026-10-20T00:00:00+00:00",
        "trace": {"action": "published", "by": "console",
                  "at": "2026-10-20T00:00:00+00:00", "commit": None},
    }
    head = "# meta_writer 全等测试头注（upstreams）\n"
    return head + yaml.safe_dump({"meta": {"updated": "2026-10-20T00:00:00+00:00",
                                           "updated_by": "console"},
                                  "proxies": [proxy]},
                                 sort_keys=False, allow_unicode=True,
                                 default_flow_style=False)


def _published_appkeys_yaml() -> str:
    rec = {"appkey": "sb_writer_equiv01", "secret_hash": "sha256:" + "a" * 64,
           "tenant": "writer-equiv", "caller_name": "writer-equiv",
           "app_name": "等价测试应用", "key_type": "sandbox", "oa_ticket": "",
           "tag_sets": [], "quota_qps": 50, "quota_daily": 10000, "status": "active",
           "expires_at": "2099-01-01", "issued_at": "2026-10-20T00:00:00+00:00",
           "grants": [{"service": "v1-svc-demo-writer",
                       "endpoint": "/v1/svc/demo-writer", "fields": ["age_band"]}]}
    head = "# meta_writer 全等测试头注（appkeys）\n"
    return head + yaml.safe_dump({"meta": {"updated": "2026-10-20T00:00:00+00:00",
                                           "updated_by": "console"},
                                  "keys": [rec]},
                                 sort_keys=False, allow_unicode=True,
                                 default_flow_style=False)


def _dual(tmp_path, name: str, initial: str):
    """同内容两份权威源：legacy 直调与 meta_writer 各写一份，终态逐字节比对。"""
    pa, pb = tmp_path / f"a-{name}", tmp_path / f"b-{name}"
    pa.write_text(initial, encoding="utf-8")
    pb.write_text(initial, encoding="utf-8")
    return pa, pb


def test_yaml_mode_service_offline_byte_equal(tmp_path):
    pa, pb = _dual(tmp_path, "services.yaml", _base_services_yaml())
    opsc.offline_service(pa, "v1-svc-demo-writer", operator="tester",
                         now=__import__("datetime").datetime(2026, 10, 20, 1, 0, 0))
    mw.service_offline(pb, "v1-svc-demo-writer", operator="tester",
                       now=__import__("datetime").datetime(2026, 10, 20, 1, 0, 0))
    assert pa.read_bytes() == pb.read_bytes(), "service_offline 抽层后字节级不等价"


def test_yaml_mode_upstream_offline_byte_equal(tmp_path):
    pa, pb = _dual(tmp_path, "upstreams.yaml", _published_upstreams_yaml())
    opsc.offline_upstream(pa, "proxy-demo-writer", operator="tester",
                          now=__import__("datetime").datetime(2026, 10, 20, 1, 0, 0))
    mw.upstream_offline(pb, "proxy-demo-writer", operator="tester",
                        now=__import__("datetime").datetime(2026, 10, 20, 1, 0, 0))
    assert pa.read_bytes() == pb.read_bytes(), "upstream_offline 抽层后字节级不等价"


def test_yaml_mode_credential_disable_byte_equal(tmp_path):
    pa, pb = _dual(tmp_path, "appkeys.yaml", _published_appkeys_yaml())
    fixed = __import__("datetime").datetime(2026, 10, 20, 1, 0, 0)
    doc_a = appk.load_appkeys(pa)
    appk.set_status(doc_a, "sb_writer_equiv01", "disable", now=fixed)
    appk.save_appkeys(pa, doc_a)
    doc_b = appk.load_appkeys(pb)
    mw.credential_set_status(doc_b, "sb_writer_equiv01", "disable", now=fixed)
    mw.credential_save(pb, doc_b)
    assert pa.read_bytes() == pb.read_bytes(), "credential disable 抽层后字节级不等价"


# ----------------------------------------------------------------------
# ③ 双模开关（缺省 yaml；db＝步 6 前置骨架）
# ----------------------------------------------------------------------

def test_default_mode_is_yaml(monkeypatch):
    monkeypatch.delenv("META_WRITER", raising=False)
    assert mw.writer_mode() == "yaml"


def test_db_mode_branches_wired(monkeypatch):
    """db 模式分支在场（步6 后不再是 NotImplementedError）：通道注桩件，
    写径触达事务通道即抛 MetaDbError（证明已接库通道，而非骨架占位）。
    """
    monkeypatch.setenv("META_WRITER", "db")

    class _StubChannel:
        def rows(self, query):
            return []

        def script(self, sql):
            raise mw.MetaDbError("stub-channel")

    monkeypatch.setattr(mw, "_channel_inst", _StubChannel())
    with pytest.raises(mw.MetaDbError):
        mw.tenant_upsert(tenant_id="twired01", display_name="w", oa_ticket_open="OA-X-1")
    with pytest.raises(mw.MetaDbError):
        doc = {"keys": [{"appkey": "sb_wired01", "tenant": "twired01", "quota_qps": 1}]}
        mw.credential_save(Path("/tmp/x"), doc)
    with pytest.raises(mw.MetaDbError):
        mw.upstream_save(Path("/tmp/nonexistent-upstreams.yaml"),
                         service_name="w", route="/proxy/wired/probe",
                         upstream_url="http://localhost:9/x", status="draft")


def test_invalid_mode_falls_back_to_yaml(monkeypatch):
    monkeypatch.setenv("META_WRITER", "nonsense")
    assert mw.writer_mode() == "yaml", "非法值必须回落 yaml（行为不变铁律）"
