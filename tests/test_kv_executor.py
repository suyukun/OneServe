#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标签服务单元测试（task-003）——纯 python 直跑或 pytest 收集均可。

红线断言（任务书口径）：
  ① 投影漏字段＝失败：授权白名单外的字段出现在投影结果即红；
  ② 脱敏字段原值不得出现：band_mask 输出只允许配置档位（A/B/C/U），原值零出现。

被测对象 service/app/kv_executor.py 只依赖 pyyaml（KV 访问经 store 注入），
用字典假 store 即可覆盖全路径（含 meta 指针解析/未上线/超时分支）。
"""

import asyncio
import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
sys.path.insert(0, str(REPO / "sync"))

from app import kv_executor as kvx  # noqa: E402
from gen_mock_source import sm3_hex as gen_sm3  # noqa: E402  造数侧同口径 SM3

from synth_services import (load_synth_services, synth_matrix, synth_services_dict,  # noqa: E402  合成 fixture（全下线终态）
                             real_registry_state, real_matrix_state)
REAL_SERVICES, REAL_LOAD_ERR = real_registry_state()   # 现实态：全下线→空表 fail-loud（服务层红线）
MATRIX, _MATRIX_ERR = real_matrix_state()      # 现实态：空矩阵 fail-loud
SERVICE = synth_services_dict()["news_tag_query"]   # 合成 A 型样例（全下线终态）
GRANT = kvx.tenant_grant(synth_matrix(), "news_tag_query", tenant="demo_merchant")
WHITELIST = GRANT["fields"]
RULES = SERVICE["response"].get("desensitize") or {}   # news 域无脱敏（明文资讯标签）
BAND_RULE = {"type": "band_mask",                            # band_mask 机制红线用内联规则钉死（演示服务下线后真源不再配置脱敏）
             "bands": [{"max": 300000, "out": "A"},
                       {"max": 600000, "out": "B"},
                       {"out": "C"}],
             "unknown_out": "U"}



class FakeStore:
    """字典假 KV：预置 alias/数据键/版本元信息，覆盖 read_user 同款键规则。"""

    def __init__(self, data=None, hashes=None):
        self.data = data or {}
        self.hashes = hashes or {}

    async def get(self, key):
        return self.data.get(key)

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)


def _matrix_grant_services():
    """矩阵登记的全部授权服务（漂移守卫：不得残留已下线服务）。"""
    return {g["service"] for row in MATRIX for g in row.get("grants", [])}


def _upstream_proxy_ids():
    """upstreams.yaml 在册代理 id 集合（ADR-0013：B 型代理单一权威源）。

    proxy-local-* 命名空间条目由 proxy_hub 真实消费——授权在场即可调。
    """
    import yaml
    doc = yaml.safe_load((REPO / "service" / "upstreams.yaml").read_text(encoding="utf-8")) or {}
    return {str(p["id"]) for p in (doc.get("proxies") or [])
            if isinstance(p, dict) and p.get("id")}


def test_services_yaml_shape():
    """注册配置结构齐备（合成条目；archetype/键型/存储/guard/审计开关＝任务书交付物要点）。"""
    kv = SERVICE["backend"]["kv"]
    assert SERVICE["archetype"] == "kv_lookup"
    assert SERVICE["params"][0]["key_type"] == "news_id"    # 资讯 ID 明文（平台不关注键算法）
    assert kv["storage_key"] == "as_received"
    assert kv["alias_key"] == "tags:news:latest"            # 经 meta 指针解析版本空间（news 域）
    assert "{version}" in kv["data_key"] and "{sm3}" in kv["data_key"]
    assert SERVICE["guard"]["timeout_ms"] > 0
    assert SERVICE["guard"]["max_items_per_call"] == 1
    assert SERVICE["audit"]["enabled"] is True
    assert SERVICE["audit"]["log_params"] == "hash_only"
    assert "data_as_of" in SERVICE["response"]["meta"]


def _syn_matrix():
    """合成矩阵（synth_services 同源）：授权-服务一致性守卫的 fixture。"""
    return synth_matrix()


def test_auth_matrix_shape():
    """授权矩阵机制：租户×AppKey×OA 单号占位×字段白名单非空，授权服务全部在册（合成矩阵）。

    漂移守卫（机制层）：矩阵不得残留未注册服务的授权——残留会让凭证签发 400
    unknown_service（真实发生过：demo 下线时矩阵未同步）。
    """
    sm = _syn_matrix()
    assert GRANT["tenant"] == "demo_merchant"
    assert GRANT["oa_ticket"], "OA 单号占位缺失（task-008 回填）"
    assert kvx.tenant_grant(sm, "news_tag_query", tenant="demo_merchant")["fields"]
    granted = {g["service"] for row in sm for g in row.get("grants", [])}
    assert granted <= set(synth_services_dict()), f"矩阵残留未注册服务：{sorted(granted)}"
    # 2026-09-30 现实态口径：注册表/矩阵空或非空均合法，守卫 fail-loud 加载＋矩阵授权不残留未注册服务
    assert not _MATRIX_ERR and not REAL_LOAD_ERR
    # 2026-10-15 ADR-0013：B 型代理按 upstreams.yaml 声明式注册（proxy-local-*）且
    # proxy_hub 真实消费——在册判定＝services.yaml ∪ upstreams.yaml，授权在场即可调。
    known = set(REAL_SERVICES) | _upstream_proxy_ids()
    assert _matrix_grant_services() <= known, \
        f"矩阵残留未注册服务授权：{sorted(_matrix_grant_services() - known)}"


def test_unknown_tenant_403():
    """未登记 AppKey → 403（fail-closed）。"""
    try:
        kvx.tenant_grant(MATRIX, "news_tag_query", appkey="no-such-key")
    except kvx.TagQueryError as exc:
        assert exc.status == 403
    else:
        raise AssertionError("未登记 AppKey 应 403")


def test_desensitize_fields_granted():
    """脱敏规则只应配置在已授权字段上（守卫防配置漂移）；news 域如实无脱敏。"""
    not_granted = set(RULES) - WHITELIST
    assert not not_granted, f"脱敏字段未在白名单：{sorted(not_granted)}"
    assert RULES == {}, "news 域（明文资讯标签）不应配置脱敏"


def test_project_red_line():
    """红线①：白名单外字段绝不出现在投影结果（哪怕用户标签里有值）。"""
    tags = {"tags": "finance,sports",
            "title": "secret-title",                # 在库未授权字段（投影红线零出现）
            "usr_id": "should-never-leak"}          # 键列更不允许出现在标签域
    out = kvx.project(tags, WHITELIST)
    assert set(out) <= WHITELIST, "投影结果混入未授权字段（红线）"
    assert "title" not in out and "usr_id" not in out
    assert out == {"tags": "finance,sports"}


def test_band_mask_thresholds():
    """红线②：band_mask 输出仅档位；跨阈值分档正确；非数值→U（原值零出现）。"""
    rule = BAND_RULE
    cases = {"0": "A", "299999": "A", "300000": "B", "599999": "B",
             "600000": "C", "999999": "C", "not-a-number": "U"}
    for raw, expect in cases.items():
        got = kvx._band_mask(raw, rule)
        assert got == expect, f"band_mask({raw!r})={got!r}，期望 {expect!r}"
        assert got in {"A", "B", "C", "U"}, "band_mask 输出越出档位值域（红线）"
        if raw != "not-a-number":
            assert got != raw, "band_mask 输出等于原值（红线）"


def test_normalize_key():
    """键归一化：大写转小写放行；格式非法 400。

    2026-09-23 维护者 口径：默认模式放宽——原「SM3 形态」不再是默认口径，
    非十六进制的合理字符串键（UUID／自增 ID 等）也应放行；仅真正越出
    pattern 的键（空/含非法字符/超长）才 400。
    """
    pat = SERVICE["params"][0]["pattern"]   # news 口径 ^[a-z0-9]{4,64}$（服务自声明，平台不关注键算法）
    # 大写归一化放行（既有行为不变）
    assert kvx.normalize_key("NEWS" + "0" * 4, pat) == "news" + "0" * 4
    # 合理键放行：资讯 ID / 数字串
    for good in ["news0001", "n000001", "1234567890", "ab" * 32]:
        assert kvx.normalize_key(good, pat) == good.lower()
    # 真正非法：空键 / 含空白或特殊字符 / 键值 / 越出 news 长度口径
    for bad in ["", "ab" * 32 + "!", "has space", "a" * 65, "键值", "a/b", "ab-cd"]:
        try:
            kvx.normalize_key(bad, pat)
        except kvx.TagQueryError as exc:
            assert exc.status == 400
        else:
            raise AssertionError(f"非法键 {bad!r} 应 400")


def test_sm3_known_vector_and_consistency():
    """SM3 口径：国标向量前缀 abc→66c7f0f4…；与造数侧 gen_mock_source 完全一致。"""
    h = kvx.sm3_hex("abc")
    assert len(h) == 64 and h.startswith("66c7f0f4"), "SM3 国标向量（abc）不匹配"
    phone = "13800138000"
    assert kvx.sm3_hex(phone) == gen_sm3(phone), "服务端 SM3 与造数侧口径漂移"


def _demo_store():
    """假 KV：一条有标签的资讯（title 在库未授权——投影红线零出现）。"""
    tags = {"tags": "finance,sports", "title": "secret-title"}
    key = "news0001"
    data = {"tags:news:latest": "20260920",
            "tags:news:20260920:" + key: json.dumps(tags, separators=(",", ":"))}
    hashes = {"sync:ver:news:20260920": {"data_as_of": "2026-09-20"}}
    return FakeStore(data, hashes), key


def test_execute_found_path():
    """全路径：meta 指针→数据键→反序列化→投影→脱敏→data_as_of。"""
    store, key = _demo_store()
    out = asyncio.run(kvx.execute(store, SERVICE, GRANT, key))
    assert out["found"] is True
    assert out["data_as_of"] == "2026-09-20" and out["live_version"] == "20260920"
    assert set(out["fields"]) <= WHITELIST, "执行结果混入未授权字段（红线）"
    assert out["fields"] == {"tags": "finance,sports"}      # 授权标签集原样返回
    assert "secret-title" not in json.dumps(out["fields"]), "未授权字段值出现（红线）"
    assert "title" not in out["fields"]


def test_execute_not_found_and_not_ready():
    """无该用户→found=false＋fields=None（诚实空值）；无版本指针→503 不读脏。"""
    store, key = _demo_store()
    out = asyncio.run(kvx.execute(store, SERVICE, GRANT, "news9999"))
    assert out["found"] is False and out["fields"] is None
    assert out["data_as_of"] == "2026-09-20"               # 时点照报
    try:
        asyncio.run(kvx.execute(FakeStore(), SERVICE, GRANT, key))
    except kvx.TagQueryError as exc:
        assert exc.status == 503 and exc.code == "data_not_ready"
    else:
        raise AssertionError("无线上版本指针应 503（不读脏）")


def test_execute_guard_timeout():
    """guard 超时：整链 KV 读超过 timeout_ms → 503 guard_timeout。"""

    class SlowStore(FakeStore):
        async def get(self, key):
            await asyncio.sleep(0.3)
            return await super().get(key)

    _, key = _demo_store()
    svc = copy.deepcopy(SERVICE)
    svc["guard"]["timeout_ms"] = 50                          # 测试口径收紧到 50ms
    try:
        asyncio.run(kvx.execute(SlowStore(), svc, GRANT, key))
    except kvx.TagQueryError as exc:
        assert exc.status == 503 and exc.code == "guard_timeout"
    else:
        raise AssertionError("KV 读超时应 503 guard_timeout")


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== task-003 标签服务单元测试（{len(tests)} 条）==")
    failed = []
    for name, fn in tests:
        try:
            fn()
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
