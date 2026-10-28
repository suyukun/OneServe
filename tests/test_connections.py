#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡1 套件：connections.yaml 双轨读取（env > yaml > 默认）＋如实报错口径。

判据映射（docs/plans/开工门槛-卡1 目标 1/2/6）：
  a) yaml 读取与优先级：env 覆盖 yaml、yaml 覆盖默认、无文件＝env＋默认、
     空串 env＝不覆盖；仓库随件 config/connections.yaml 可被实际读取装配；
  b) 如实报错（ConnectionsError，不静默回退）：坏 yaml／缺 sources.hbase 段／
     缺结构键／非法 auth_mode／非法 port／疑似密钥键落段／认证模式条件必填缺失；
  c) 密钥零落仓防呆：hbase 段白名单外的疑似密钥键（password/secret/token…）即拒。

诊断三分类单测在 tests/test_hbase_executor.py（classify_hbase_failure）。
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import connections as cc  # noqa: E402

REPO_YAML = REPO / "config" / "connections.yaml"


def _tmp_yaml(text: str) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "connections.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _ok_yaml(host="yaml-host", **over):
    body = {
        "host": host, "port": 9090, "auth_mode": "none",
        "user": "", "principal": "", "secret_file": "",
    }
    body.update(over)
    lines = ["sources:", "  hbase:"]
    for k, v in body.items():
        lines.append(f"    {k}: {v!r}" if not isinstance(v, str) or v == ""
                     else f"    {k}: {v}")
    return _tmp_yaml("\n".join(lines) + "\n")


# ---------- a) 优先级链 env > yaml > 默认 ----------

def test_repo_shipped_yaml_is_loadable_and_non_sensitive():
    """仓库随件 connections.yaml 可被装配读取；密钥只允许路径引用（无密钥值键）。"""
    conn = cc.load_hbase_connection(path=REPO_YAML, env={})
    assert conn.host == "oneserve-hbase" and conn.port == 9090
    assert conn.auth_mode == "none" and conn.secret_file == ""
    text = REPO_YAML.read_text(encoding="utf-8")
    for bad in ("password:", "passwd:", "secret:", "token:", "keytab:"):
        assert bad not in text, f"connections.yaml 不得含密钥值键 {bad!r}"


def test_priority_env_over_yaml_over_default():
    """每键三级：env（非空）> yaml > 代码默认；空串 env＝不覆盖。"""
    p = _ok_yaml(host="yaml-host")
    assert cc.load_hbase_connection(path=p, env={}).host == "yaml-host"       # yaml 生效
    out = cc.load_hbase_connection(path=p, env={"HBASE_HOST": "env-host"})
    assert out.host == "env-host"                                            # env 覆盖 yaml
    blank = cc.load_hbase_connection(path=p, env={"HBASE_HOST": "  "})
    assert blank.host == "yaml-host"                                         # 空白 env＝未设置
    # 候选路径全缺席 → 代码默认（与 hbase_executor.DEFAULT_HOST 同口径＝宿主自测地址）
    orig_paths = cc.default_yaml_paths
    missing = Path(tempfile.mkdtemp()) / "none.yaml"
    cc.default_yaml_paths = lambda: [missing]
    try:
        dft = cc.load_hbase_connection(env={})
        assert dft.host == cc.DEFAULT_HOST and dft.port == cc.DEFAULT_PORT
        assert dft.auth_mode == "none"
        only_env = cc.load_hbase_connection(env={"HBASE_PORT": "9999"})      # env 仍生效
        assert only_env.port == 9999
    finally:
        cc.default_yaml_paths = orig_paths
    # 显式指定的路径不存在 → 如实报错（不静默当无文件）
    _expect_error(missing, needle="不存在")


def test_env_path_explicit_via_connections_yaml_var():
    """CONNECTIONS_YAML 显式指路（生产 ConfigMap/测试注入位）优先于候选布局。"""
    p = _ok_yaml(host="explicit-path-host")
    import os
    old = os.environ.get("CONNECTIONS_YAML")
    try:
        os.environ["CONNECTIONS_YAML"] = str(p)
        out = cc.load_hbase_connection(env={})
        assert out.host == "explicit-path-host"
    finally:
        if old is None:
            os.environ.pop("CONNECTIONS_YAML", None)
        else:
            os.environ["CONNECTIONS_YAML"] = old


# ---------- b) 如实报错（不静默回退） ----------

def _expect_error(path, env=None, needle=""):
    try:
        cc.load_hbase_connection(path=path, env=env or {})
    except cc.ConnectionsError as exc:
        if needle:
            assert needle in str(exc), f"报错文案缺 {needle!r}：{exc}"
        return str(exc)
    raise AssertionError(f"应抛 ConnectionsError：{path}")


def test_bad_yaml_reports_honestly():
    """坏 yaml（解析失败）→ ConnectionsError 带文件路径，不静默回退默认。"""
    p = _tmp_yaml("sources: [broken {{{")
    _expect_error(p, needle="解析失败")


def test_missing_hbase_section_or_structural_keys():
    """文件在场但缺 sources.hbase 段／缺结构键（host/port/auth_mode）→ 报错。"""
    _expect_error(_tmp_yaml("sources: {}\n"), needle="sources.hbase")
    _expect_error(_tmp_yaml("other: 1\n"), needle="sources.hbase")
    p = _tmp_yaml("sources:\n  hbase:\n    port: 9090\n    auth_mode: none\n")
    _expect_error(p, needle="host")   # 缺结构键 host（连同 port/auth_mode 校验）


def test_invalid_values_report_honestly():
    """非法 auth_mode／非法 port／相对路径 secret_file → 报错。"""
    _expect_error(_ok_yaml(auth_mode="weird"), needle="auth_mode")
    _expect_error(_ok_yaml(port="abc"), needle="port")
    _expect_error(_ok_yaml(port=70000), needle="超界")
    _expect_error(_ok_yaml(secret_file="secrets/pass"), needle="绝对路径")


def test_secret_like_keys_rejected_zero_secret_in_repo():
    """密钥零落仓防呆：hbase 段出现疑似密钥键（password/secret/token…）即拒。"""
    p = _tmp_yaml(
        "sources:\n  hbase:\n    host: h\n    port: 9090\n"
        "    auth_mode: none\n    password: hunter2\n")
    msg = _expect_error(p, needle="未登记键")
    assert "密钥" in msg                     # 文案指向环境变量/密钥文件出口


def test_auth_mode_conditional_required_keys():
    """认证模式条件必填：basic→user；kerberos→principal＋secret_file；tls→secret_file。"""
    _expect_error(_ok_yaml(auth_mode="basic"), needle="user")
    _expect_error(_ok_yaml(auth_mode="kerberos", principal="hbase/_HOST@CORP"),
                  needle="secret_file")
    _expect_error(_ok_yaml(auth_mode="tls"), needle="secret_file")
    ok = cc.load_hbase_connection(
        path=_ok_yaml(auth_mode="kerberos", principal="hbase/_HOST@CORP",
                      secret_file="/run/secrets/hbase.keytab"), env={})
    assert ok.auth_mode == "kerberos" and ok.secret_file == "/run/secrets/hbase.keytab"
    ok2 = cc.load_hbase_connection(path=_ok_yaml(auth_mode="basic", user="u"), env={})
    assert ok2.user == "u"


# ---------- 卡2 T3：装载链路接入双轨（不再只读 HBASE_HOST env） ----------

LOAD_SRC = REPO / "service" / "app" / "hbase_load.py"


def test_hbase_load_default_endpoint_uses_dual_track(monkeypatch=None):
    """host/port 未显式给 ⇒ 走 connections.load_hbase_connection() 装配（卡1 尾巴修复）。

    行为验证：注入 _default_endpoint＝双轨产出（yaml host），装载链路建池即用它；
    源码机检：load_into_redis 路径不再直读 os.environ["HBASE_HOST"]。
    """
    import importlib
    if str(REPO / "sync") not in sys.path:
        sys.path.insert(0, str(REPO / "sync"))
    from app import hbase_load as hbl
    hbase_loader = importlib.import_module("hbase_loader")

    captured = {}

    def fake_connect(host, port):
        captured["connect"] = (host, port)
        return object()

    def fake_list_tables(pool, host, port):
        captured["probe"] = (host, port)
        raise hbase_loader.HBaseLoadError("探活即停（测试注入）")

    connect_orig, lt_orig = hbase_loader.connect, hbase_loader.list_tables
    ep_orig = hbl._default_endpoint
    hbase_loader.connect, hbase_loader.list_tables = fake_connect, fake_list_tables
    hbl._default_endpoint = lambda: ("dual-yaml-host", 9110)
    try:
        import pytest as _pytest
        from app import hbase_load as _h
        try:
            _h.load_into_redis(table="usr_basic_info", cf="cf", version="20990101",
                               redis_client=object())
        except _h.HBaseLoadServiceError as exc:
            assert exc.code == "HBASE_UNAVAILABLE"          # 探活失败如实报错后停
        else:
            raise AssertionError("探活失败应上抛 HBaseLoadServiceError")
    finally:
        hbase_loader.connect, hbase_loader.list_tables = connect_orig, lt_orig
        hbl._default_endpoint = ep_orig
    assert captured["connect"] == ("dual-yaml-host", 9110)   # 双轨端点生效
    assert captured["probe"] == ("dual-yaml-host", 9110)

    src = LOAD_SRC.read_text(encoding="utf-8")
    assert "def _default_endpoint" in src, "hbase_load 须有双轨端点装配函数"
    assert 'os.environ.get("HBASE_HOST"' not in src, \
        "装载链路不得直读 HBASE_HOST env（卡1 尾巴：env>yaml>默认 只经 connections 装配）"


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("[ok] " + name)
            except Exception:
                failed += 1
                print("[FAIL] " + name)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
