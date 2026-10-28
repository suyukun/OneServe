# -*- coding: utf-8 -*-
"""外部数据源连接配置装配（卡1：配置/密钥双轨 · env > connections.yaml > 默认）。

口径（docs/plans/装载与发布流程拆分设计_v1.md §五，2026-09-30 维护者 质询后修正）：
  - 非敏感连接参数（host/port/auth_mode/user/principal）进 config/connections.yaml
    （结构化、可注释、可校验、进仓）；
  - 密钥类（密码/keytab/CA 内容）永不落仓——经环境变量注入（开发态 .env、
    生产态 K8s Secret/Vault），YAML 只存密钥文件**路径引用**（secret_file）；
  - 优先级链（每键三级，逐键取值）：环境变量 HBASE_* > connections.yaml > 代码默认值。
    环境变量为空串/空白＝未设置（compose 以 ${HBASE_HOST:-} 注入空串表示「不覆盖，走 YAML」）；
  - 如实报错（ConnectionsError，不静默回退默认值）：
      * 文件在场但 YAML 解析失败 / 顶层不是映射 → 报错；
      * 文件在场但缺 sources.hbase 段，或 hbase 段缺结构键（host/port/auth_mode）→ 报错；
      * auth_mode 不在 none|basic|kerberos|tls、port 非法、secret_file 非绝对路径 → 报错；
      * 认证模式的条件必填键缺失（basic→user；kerberos→principal＋secret_file；
        tls→secret_file）→ 报错；
      * hbase 段出现白名单外的键（含疑似密钥键 password/secret/token…）→ 报错
        ——这是「密钥零落仓」的第一道防呆闸。
  - 文件缺席＝合法（最简开发态：env＋默认值即可起）；文件在场即按上述口径全量校验。

本模块纯逻辑（不 import fastapi/redis/happybase），单测直测（tests/test_connections.py）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

# 代码默认值（优先级链第三级；host 与 hbase_executor.DEFAULT_HOST 同口径＝宿主自测地址，
# 容器/开发态的 oneserve-hbase 由 connections.yaml 提供）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9090
DEFAULT_AUTH_MODE = "none"
AUTH_MODES = ("none", "basic", "kerberos", "tls")

# 环境变量名（优先级链第一级；键名同步登记在 .env.example，密钥类只给路径/值注入位）
ENV_KEYS = {
    "host": "HBASE_HOST",
    "port": "HBASE_PORT",
    "auth_mode": "HBASE_AUTH_MODE",
    "user": "HBASE_USER",
    "principal": "HBASE_PRINCIPAL",
    "secret_file": "HBASE_SECRET_FILE",
}

# connections.yaml 的 hbase 段允许键（白名单；出现其他键＝报错，防密钥值误落仓）
HBASE_KEYS = frozenset(ENV_KEYS)
# hbase 段在场时必须显式给出的结构键（user/principal/secret_file 可缺省空串，
# 但按 auth_mode 有条件必填，见 load_hbase_connection）
STRUCTURAL_KEYS = ("host", "port", "auth_mode")
# 疑似密钥键（在白名单外命中这些名＝额外提示走环境变量/密钥文件，不落仓）
SECRET_LIKE_KEYS = ("password", "passwd", "secret", "secret_value", "token",
                    "keytab", "keytab_file", "ca", "ca_pem", "client_secret")


class ConnectionsError(Exception):
    """连接配置错误（缺键/坏 yaml/非法取值）——如实上抛，不静默回退默认值。"""


@dataclass(frozen=True)
class HBaseConnection:
    """装配完成的一份 HBase 连接参数（非敏感；密钥只有路径引用，值在外部）。"""

    host: str
    port: int
    auth_mode: str
    user: str = ""
    principal: str = ""
    secret_file: str = ""


def default_yaml_paths() -> list[Path]:
    """connections.yaml 候选路径（先命中先用；全缺席＝无 YAML，走 env＋默认，合法）。

    ① 环境变量 CONNECTIONS_YAML 显式指定（测试注入/生产 K8s ConfigMap 挂载位）；
    ② 仓库布局 <repo>/config/connections.yaml（本机开发、宿主跑测试）；
    ③ 容器布局 /app/config/connections.yaml（compose 把 ./config 只读挂到 /app/config）。
    """
    out: list[Path] = []
    explicit = str(os.environ.get("CONNECTIONS_YAML", "")).strip()
    if explicit:
        out.append(Path(explicit))
    here = Path(__file__).resolve()
    out.append(here.parents[2] / "config" / "connections.yaml")   # 宿主：仓库根/config
    out.append(here.parents[1] / "config" / "connections.yaml")   # 容器：/app/config
    return out


def _first_existing(paths: list[Path]) -> Path | None:
    for p in paths:
        if p.is_file():
            return p
    return None


def _load_hbase_section(path: str | Path | None) -> dict:
    """读 connections.yaml 的 sources.hbase 段；文件缺席＝{}（合法），坏配置抛 ConnectionsError。"""
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise ConnectionsError(
                f"connections.yaml 不存在（显式指定路径 {p}）——如实报错，不静默回退")
    else:
        p = _first_existing(default_yaml_paths())
        if p is None:
            return {}
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConnectionsError(f"connections.yaml 解析失败（{p}）：{exc}") from exc
    if not isinstance(doc, dict):
        raise ConnectionsError(
            f"connections.yaml 顶层须为映射（{p}，实为 {type(doc).__name__}）")
    sources = doc.get("sources")
    if not isinstance(sources, dict) or "hbase" not in sources:
        raise ConnectionsError(
            f"connections.yaml 缺 sources.hbase 段（{p}）——文件在场即须整段配置，"
            "不静默回退默认值")
    section = sources["hbase"]
    if not isinstance(section, dict):
        raise ConnectionsError(
            f"connections.yaml sources.hbase 须为映射（{p}，实为 {type(section).__name__}）")
    unknown = sorted(set(section) - HBASE_KEYS)
    if unknown:
        secret_like = [k for k in unknown if k in SECRET_LIKE_KEYS]
        hint = ("；疑似密钥键——密钥值永不落仓，请走环境变量注入或密钥文件路径引用"
                "（secret_file）" if secret_like else "")
        raise ConnectionsError(
            f"connections.yaml sources.hbase 出现未登记键 {unknown}（{p}）{hint}")
    missing = [k for k in STRUCTURAL_KEYS if k not in section]
    if missing:
        raise ConnectionsError(
            f"connections.yaml sources.hbase 缺键 {missing}（{p}）——"
            "在场段必须显式给出 host/port/auth_mode，不静默回退默认值")
    return section


def load_hbase_connection(path: str | Path | None = None,
                          env: dict | None = None) -> HBaseConnection:
    """装配一份 HBase 连接参数（优先级链 env > yaml > 默认；坏配置如实抛 ConnectionsError）。

    env 参数缺省读 os.environ（测试可注入假 env dict，免 monkeypatch）。
    每键取值：环境变量（非空）> YAML 段值 > 代码默认值；文件缺席时 YAML 级整体跳过。
    """
    env = os.environ if env is None else env

    def _env(key: str) -> str | None:
        raw = env.get(ENV_KEYS[key])
        raw = str(raw).strip() if raw is not None else ""
        return raw or None   # 空串/空白＝未设置（compose 注入空串表示不覆盖）

    section = _load_hbase_section(path)

    host = _env("host") or str(section.get("host") or DEFAULT_HOST).strip()

    port_raw = _env("port") if _env("port") is not None else section.get("port", DEFAULT_PORT)
    if isinstance(port_raw, bool) or not isinstance(port_raw, (int, str)):
        raise ConnectionsError(f"HBase port 非法：{port_raw!r}（须为 1..65535 整数）")
    try:
        port = int(str(port_raw).strip())
    except ValueError as exc:
        raise ConnectionsError(
            f"HBase port 非法：{port_raw!r}（须为 1..65535 整数）") from exc
    if not 1 <= port <= 65535:
        raise ConnectionsError(f"HBase port 超界：{port}（须为 1..65535）")

    auth_mode = str(_env("auth_mode") or section.get("auth_mode") or DEFAULT_AUTH_MODE).strip()
    if auth_mode not in AUTH_MODES:
        raise ConnectionsError(
            f"HBase auth_mode 非法：{auth_mode!r}（须为 {'|'.join(AUTH_MODES)} 之一）")

    user = _env("user") or str(section.get("user") or "").strip()
    principal = _env("principal") or str(section.get("principal") or "").strip()
    secret_file = _env("secret_file") or str(section.get("secret_file") or "").strip()
    if secret_file and not secret_file.startswith("/"):
        raise ConnectionsError(
            f"HBase secret_file 须为绝对路径引用（如 /run/secrets/hbase-pass），"
            f"实为 {secret_file!r}——本配置只存路径，密钥值永不落仓")

    # 认证模式条件必填（如实报错；none 开发态全可空）
    if auth_mode == "basic" and not user:
        raise ConnectionsError("auth_mode=basic 须给 user（connections.yaml 或环境变量 HBASE_USER）")
    if auth_mode == "kerberos" and not principal:
        raise ConnectionsError(
            "auth_mode=kerberos 须给 principal（connections.yaml 或环境变量 HBASE_PRINCIPAL）")
    if auth_mode in ("kerberos", "tls") and not secret_file:
        raise ConnectionsError(
            f"auth_mode={auth_mode} 须给 secret_file 密钥文件路径引用"
            "（connections.yaml 或环境变量 HBASE_SECRET_FILE；值在外部，不落仓）")

    return HBaseConnection(host=host, port=port, auth_mode=auth_mode, user=user,
                           principal=principal, secret_file=secret_file)
