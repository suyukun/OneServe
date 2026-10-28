#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务注册表单键测试（卡4 ADR-0008 §Decision.2）——门槛稿 AC-1 判据机检面。

红线断言（卡4 门槛稿 AC-1 口径，服务修订号机制已删）：
  ① 单键索引：service_id 单键建索引，返回值无 tuple 复合键（AC-1a）；
  ② 同 id 重复注册＝启动失败，报错含 service_id 与「重复注册」（AC-1b，NC-1=a
     ——删的是版本机制不是查重保护；更新服务走发布覆盖，不追加条目）；
  ③ 服务条目 schema 不含 version/versions 键；versions()/_service_version()/
     _version_sort_key() 三符号从 kv_executor 移除（AC-1c）；
  ④ execute() 响应不含 "version" 键（AC-1d；live_version 属表数据版本，保留）；
  ⑤ 授权定位只剩 grants.service == service_id 一条路径（AC-3a；未授权仍 403）。

被测对象 service/app/kv_executor.py（只依赖 pyyaml，注册表文件路径可注入）。
"""

import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import kv_executor as kvx  # noqa: E402

from synth_services import load_synth_services, synth_matrix, synth_services_dict, real_registry_state, real_matrix_state  # noqa: E402  合成 fixture（全服务下线，见模块头注）
REGISTRY, _REG_ERR = real_registry_state()     # 现实态（单键注册表可加载，reality 用例守卫）
MATRIX, _MATRIX_ERR = real_matrix_state()      # 现实态（矩阵可加载）
SYN = synth_services_dict()                     # 合成视图（单键；无版本语义）


def _synth_registry(*entries):
    """合成注册表（service_id 单键；现实态由 reality 用例守卫）。"""
    path = Path(tempfile.mkdtemp()) / "services.yaml"
    path.write_text(yaml.safe_dump({"services": list(entries)}, allow_unicode=True), encoding="utf-8")
    return kvx.load_services(path)


def _write_registry(tmp_path: Path, services: list[dict]) -> Path:
    path = tmp_path / "services.yaml"
    path.write_text(yaml.safe_dump({"services": services}, allow_unicode=True), encoding="utf-8")
    return path


def _svc(service_id: str) -> dict:
    entry = {"service_id": service_id, "archetype": "kv_lookup",
             "backend": {"kv": {}}, "params": [{"name": "key"}],
             "response": {"meta": []}, "guard": {}, "audit": {"enabled": False}}
    return entry


# ---------- AC-1a：单键索引 ----------

def test_single_key_index_no_tuple_keys():
    """注册表以 service_id 单键建索引，无 tuple 复合键（AC-1a 判据原文）。"""
    reg = _synth_registry(_svc("svc_a"), _svc("svc_b"))
    assert not any(isinstance(k, tuple) for k in reg)
    assert sorted(reg) == ["svc_a", "svc_b"]
    assert reg["svc_a"]["service_id"] == "svc_a"
    # 合成注册表同为单键形态；扁平视图与注册表逐键一致
    syn = load_synth_services()
    assert not any(isinstance(k, tuple) for k in syn)
    assert SYN == dict(syn)


def test_reality_registry_single_key_loadable():
    """现实态：线上 services.yaml 单键加载成功且条目无版本键（fold 后守卫）。"""
    assert not _REG_ERR and not _MATRIX_ERR
    assert not any(isinstance(k, tuple) for k in REGISTRY)
    for sid, svc in REGISTRY.items():
        assert "version" not in svc and "versions" not in svc, sid


# ---------- AC-1b：同 id 重复即拒（NC-1=a） ----------

def test_duplicate_service_id_fails_loud(tmp_path):
    """同 service_id 两条目＝启动失败，报错含 id 与「重复注册」（AC-1b 判据原文）。"""
    path = _write_registry(tmp_path, [_svc("svc_dup"), _svc("svc_dup")])
    try:
        kvx.load_services(path)
    except RuntimeError as exc:
        assert "svc_dup" in str(exc) and "重复注册" in str(exc)
    else:
        raise AssertionError("同 id 重复注册应启动失败")
    # 单条目同 id 正常加载（重复才拒，不是禁用该 id）
    reg = kvx.load_services(_write_registry(tmp_path, [_svc("svc_dup")]))
    assert reg["svc_dup"]["service_id"] == "svc_dup"


# ---------- AC-1c：版本机制符号移除 ----------

def test_version_machinery_symbols_removed():
    """versions()/_service_version()/_version_sort_key 三符号不再存在（AC-1c）。"""
    assert not hasattr(kvx.ServiceRegistry, "versions")
    assert not hasattr(kvx, "_service_version")
    assert not hasattr(kvx, "_version_sort_key")
    # 条目 schema 不再携带 version/versions 键（合成＋现实双守卫）
    for svc in list(SYN.values()) + list(REGISTRY.values()):
        assert "version" not in svc and "versions" not in svc


# ---------- AC-3a：授权定位单路径 ----------

def test_tenant_grant_single_path_by_bare_service_id():
    """grants.service == service_id 一条路径：裸 id 授权命中（合成矩阵钉死语义）。"""
    matrix = [{"tenant": "t_bare", "appkey": "k1", "oa_ticket": "OA-1", "grants": [
        {"service": "svc_x", "fields": ["a", "b"]},
    ]}]
    grant = kvx.tenant_grant(matrix, "svc_x", tenant="t_bare")
    assert grant["fields"] == {"a", "b"}
    # AppKey 通道同一路径
    grant2 = kvx.tenant_grant(matrix, "svc_x", appkey="k1")
    assert grant2["tenant"] == "t_bare"


def test_tenant_grant_rejects_version_kwarg():
    """tenant_grant 无 version 形参：版本维分支已删（传 version= 即 TypeError）。"""
    matrix = [{"tenant": "t_v", "appkey": "k1", "grants": [{"service": "svc_x", "fields": ["a"]}]}]
    try:
        kvx.tenant_grant(matrix, "svc_x", tenant="t_v", version="v1")
    except TypeError:
        pass
    else:
        raise AssertionError("tenant_grant 应不再接受 version 形参")


def test_tenant_grant_ungranted_403():
    """未授权服务 → 403 service_not_granted；未登记租户 → 403 UNKNOWN_TENANT（错误码不变）。"""
    matrix = [{"tenant": "t_g", "appkey": "k1", "grants": [
        {"service": "svc_x", "fields": ["a"]},
    ]}]
    try:
        kvx.tenant_grant(matrix, "svc_y", tenant="t_g")
    except kvx.TagQueryError as exc:
        assert exc.status == 403 and exc.code == "service_not_granted"
    else:
        raise AssertionError("未授权服务应 403")
    try:
        kvx.tenant_grant(matrix, "svc_x", tenant="t_nope")
    except kvx.TagQueryError as exc:
        assert exc.status == 403 and exc.code in ("unknown_tenant", "UNKNOWN_TENANT")
    else:
        raise AssertionError("未登记租户应 403")


# ---------- AC-1d：execute 响应无 version 键 ----------

def test_execute_response_has_no_version_key():
    """execute() 响应不含 "version" 键；live_version（表数据版本 meta）不受影响（AC-1d）。"""
    svc = _svc("svc_e2e")
    svc["backend"] = {"kv": {"alias_key": "tags:t:latest",
                             "data_key": "tags:t:{version}:{sm3}",
                             "version_meta_key": "sync:ver:t:{version}",
                             "storage_key": "as_received"}}
    svc["params"] = [{"name": "key", "pattern": r"^[a-z0-9]{4,64}$"}]
    svc["response"] = {"meta": ["data_as_of", "live_version"]}
    reg = _synth_registry(svc)
    grant = {"tenant": "t_e2e", "fields": {"tag_a"}}
    out = _run(kvx.execute, _FakeStore(), reg["svc_e2e"], grant, "key0001")
    assert "version" not in out
    assert out.get("live_version") == "20260920"      # tabver 元信息照常在（B 类不动）


# ---------- 脚手架 ----------

class _FakeStore:
    """字典假 KV（test_kv_executor 同款最小面）：覆盖指针→数据键→版本元信息。"""

    async def get(self, key):
        data = {"tags:t:latest": "20260920",
                "tags:t:20260920:key0001": '{"tag_a":"v1"}'}
        return data.get(key)

    async def hget(self, key, field):
        return {"sync:ver:t:20260920": {"data_as_of": "2026-09-20"}}.get(key, {}).get(field)


def _run(coro_fn, *args, **kwargs):
    import asyncio
    return asyncio.run(coro_fn(*args, **kwargs))


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== 卡4 服务注册表单键测试（{len(tests)} 条）==")
    failed = []
    for name, fn in tests:
        try:
            fn() if "tmp_path" not in fn.__code__.co_varnames else fn(Path(tempfile.mkdtemp()))
            print(f"  ✓ {name}")
        except AssertionError as exc:
            print(f"  ✗ {name}：{exc}")
            failed.append(name)
    if failed:
        print(f"== 未过：{len(tests) - len(failed)}/{len(tests)}；失败：{'、'.join(failed)} ==")
        return 1
    print(f"== 全绿：{len(tests)}/{len(tests)} ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
