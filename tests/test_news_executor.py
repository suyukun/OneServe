#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资讯标签服务单元测试（task-007）——纯 python 直跑或 pytest 收集均可。

断言口径（任务书交付物①）：
  ① 资讯服务与用户标签同一机制注册（同一 KV 执行器；news 元指针约定同用户标签）；
  ② 键＝资讯 ID 明文；投影红线：授权白名单外字段（如标题）哪怕在库也零出现；
  ③ B 型代理服务注册配置齐备（upstream_url/timeout/retry/retention 开关与目录）。

被测对象与 test_kv_executor.py 同款：字典假 store，无 docker/redis 依赖。
"""

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
sys.path.insert(0, str(REPO / "sync"))

from app import kv_executor as kvx  # noqa: E402
from gen_news_data import TAG_POOL, gen_records  # noqa: E402  造数侧同口径

from synth_services import load_synth_services, synth_matrix, synth_services_dict  # noqa: E402  合成 fixture（全服务下线，见模块头注）
SERVICES = load_synth_services()   # 全下线终态：合成注册表（形状＝HEAD 真实条目）
MATRIX = synth_matrix()
NEWS = SERVICES["news_tag_query"]
PARTNER = SERVICES["partner_proxy"]
NEWS_GRANT = kvx.tenant_grant(MATRIX, "news_tag_query", tenant="demo_merchant")
NEWS_WHITELIST = NEWS_GRANT["fields"]


class FakeStore:
    """字典假 KV：预置 alias/数据键/版本元信息，键规则与 gen_news_data 同款。"""

    def __init__(self, data=None, hashes=None):
        self.data = data or {}
        self.hashes = hashes or {}

    async def get(self, key):
        return self.data.get(key)

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)


def _news_store():
    """假 KV：一条资讯（标题＋标签集在库；白名单只授权 tags）。"""
    data = {"tags:news:latest": "20260921",
            "tags:news:20260921:n000001":
                json.dumps({"title": "黄金ETF连获12日净流入", "tags": ["黄金", "避险"]},
                           ensure_ascii=False, separators=(",", ":"))}
    hashes = {"sync:ver:news:20260921": {"data_as_of": "2026-09-21"}}
    return FakeStore(data, hashes)


def test_services_yaml_news_shape():
    """资讯服务注册结构：同一 KV 执行器机制（archetype/元指针/插槽/guard/审计）。"""
    kv = NEWS["backend"]["kv"]
    assert NEWS["archetype"] == "kv_lookup"
    assert kv["entity"] == "news"
    assert kv["namespace"] == "tags:news"
    assert kv["alias_key"] == "tags:news:latest"           # news 元指针（约定同用户标签）
    assert "{version}" in kv["data_key"] and "{sm3}" in kv["data_key"]
    assert kv["version_meta_key"] == "sync:ver:news:{version}"  # 与用户域元信息不互撞
    assert kv["storage_key"] == "as_received"
    assert NEWS["params"][0]["key_type"] == "news_id"      # 键＝资讯 ID 明文
    assert NEWS["guard"]["timeout_ms"] > 0
    assert NEWS["audit"]["enabled"] is True
    assert "data_as_of" in NEWS["response"]["meta"]


def test_news_key_pattern():
    """资讯 ID 明文口径：小写字母数字过闸；大写归一化；怪键 400。"""
    assert kvx.normalize_key("N000001", NEWS["params"][0]["pattern"]) == "n000001"
    for bad in ["AB:CD", "涨价了", "n" * 65, ""]:
        try:
            kvx.normalize_key(bad, NEWS["params"][0]["pattern"])
        except kvx.TagQueryError as exc:
            assert exc.status == 400
        else:
            raise AssertionError(f"非法资讯键 {bad!r} 应 400")


def test_auth_matrix_news_grant():
    """授权矩阵：demo_merchant 获资讯字段白名单（仅 tags；标题在库未授权＝投影红线料）。"""
    assert NEWS_WHITELIST == {"tags"}
    try:
        kvx.tenant_grant(MATRIX, "news_tag_query", tenant="no_such_tenant")
    except kvx.TagQueryError as exc:
        assert exc.status == 403
    else:
        raise AssertionError("未登记租户应 403")


def test_news_execute_found_projection_red_line():
    """AC-1 主路径：资讯 ID→标签集；标题在库但未授权→响应零出现（投影红线）。"""
    out = asyncio.run(kvx.execute(_news_store(), NEWS, NEWS_GRANT, "n000001"))
    assert out["found"] is True
    assert out["fields"] == {"tags": ["黄金", "避险"]}
    assert "title" not in json.dumps(out["fields"]), "未授权字段 title 泄露（红线）"
    assert out["data_as_of"] == "2026-09-21" and out["live_version"] == "20260921"
    assert out["tenant"] == "demo_merchant"


def test_news_execute_not_found_and_not_ready():
    """未知资讯诚实 found=false；无线上版本指针 503 不读脏（与用户标签同语义）。"""
    out = asyncio.run(kvx.execute(_news_store(), NEWS, NEWS_GRANT, "n999999"))
    assert out["found"] is False and out["fields"] is None
    try:
        asyncio.run(kvx.execute(FakeStore(), NEWS, NEWS_GRANT, "n000001"))
    except kvx.TagQueryError as exc:
        assert exc.status == 503 and exc.code == "data_not_ready"
    else:
        raise AssertionError("无版本指针应 503（不读脏）")


def test_gen_records_deterministic():
    """造数确定性：同 seed 复跑同数据；~1000 条 ID 唯一；标签域不出池。"""
    a, b = gen_records(50, 42), gen_records(50, 42)
    assert a == b
    big = gen_records(1000, 42)
    assert len(big) == 1000 and len({r["id"] for r in big}) == 1000
    assert all(r["tags"] and set(r["tags"]) <= set(TAG_POOL) for r in big)
    assert {"黄金", "期货", "基金", "保险"} <= set(TAG_POOL)   # 任务书点名的四类在域


def test_services_yaml_partner_shape():
    """B 型代理注册结构：upstream/timeout/retry/留存开关与目录（任务书交付物②）。"""
    conf = PARTNER["backend"]["proxy"]
    assert PARTNER["archetype"] == "external_proxy"
    assert conf["upstream_url"].startswith("http://oneserve-mock-partner:")
    assert int(conf["timeout_ms"]) > 0
    assert int(conf["retry"]) >= 0
    assert isinstance(PARTNER["retention"]["enabled"], bool)
    assert PARTNER["retention"]["dir"]
    assert kvx.tenant_grant(MATRIX, "partner_proxy", tenant="demo_merchant")["fields"]


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== task-007 资讯服务＋代理注册单元测试（{len(tests)} 条）==")
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
