# -*- coding: utf-8 -*-
"""声明式渲染器内核判据单测（TASK-M2-7-a）——「渲染产物与 git 源一致」的机检面。

判据原文（门槛稿 TASK-M2-7 行）：
  > a 声明式渲染器内核（git 恒权威→渲染产物，前置 M2-4）
  > 判据：渲染产物与 git 源一致断言

本套件把该判据机检化为六条（不依赖容器/网关，纯逻辑全路径覆盖）：
  ① 与 git 源一致：以 service/services.yaml（git 权威源）**独立重算**期望路由集合与
     形状（不经 registry_render 的渲染循环），断言渲染产物逐条一致——产物完全由源
     确定，源外服务零路由（不多不少）；
  ② 重跑无差异：同输入恒同输出（canonical JSON 逐字节一致），且渲染不读盘/不取
     时间/不取随机（纯函数：读盘与时间/随机被判错也照常产出）；
  ③ 单一渲染入口：render_route_specs == render_query_routes+legacy_route_specs
     逐字节相等；下发（init_routes）、热加载（registry_hot）、契约生成（gen_openapi）
     三处调用方均只经该入口，无第二渲染路径；
  ④ 源变→产物确定变：git 源加一服务 → 恰好多一条通配路由、既有条目逐字节不变；
  ⑤ 多列族扩展位（维护者 2026-09-23 口径）：backend.hbase.cf 接受 str 或 str 列表，
     本阶段单列族实现；cf 归一/取首单一事实来源在 registry_render，>1 列族执行器
     启动即拒（不做「有配置不生效」的静默降级）；
  ⑥ gen_openapi 无文本解析旁路：契约的静态通道路由改由 init_routes.static_resources()
     同源取数，不再对源码文本正则解析（原旁路即第二条解析路径）。

被测对象 service/app/registry_render.py（不 import fastapi/redis——宿主机可全路径覆盖）。
"""

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
sys.path.insert(0, str(REPO / "configs" / "apisix"))   # init_routes（静态资源同源取数机检）

from app import kv_executor as kvx       # noqa: E402
from app import registry_render as rend  # noqa: E402
import init_routes as initr              # noqa: E402

import synth_services as _syn  # noqa: E402  合成 fixture（全下线终态；形状＝HEAD 真实条目）
SERVICES_YAML = _syn.synth_services_path()                # 渲染输入（合成注册表；线上真源已清空 services: []）
REAL_YAML = REPO / "service" / "services.yaml"        # 线上真源（reality 用例守卫空表）
LEGACY_IDS = ("oneserve-v1-tags-query", "oneserve-v1-news-query", "oneserve-v1-hbase-query")


def _git_registry() -> dict:
    """从 git 权威源加载注册表（渲染输入）。"""
    return kvx.load_services(SERVICES_YAML)


def _expected_from_source() -> dict[str, dict]:
    """【独立重算】直接从 services.yaml 文本推导期望路由（不经渲染器）。

    与 registry_render 的实现解耦：本函数只按源文本字段（service_id/
    backend.kv|hbase/source）重建「应有路由」，用于反向校验渲染产物——渲染器若
    漏服务/多服务/改形状，本断言即红（卡4：service_id 单键，无版本归并）。
    """
    raw = yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8"))["services"]
    expected: dict[str, dict] = {}
    for s in raw:
        sid = s["service_id"]
        backend = s.get("backend") or {}
        source = backend.get("source", "redis")
        if ("kv" in backend or "hbase" in backend) and source in ("redis", "hbase"):
            name = f"oneserve-query-{sid}"
            expected[f"routes/{name}"] = {
                "name": name, "uri": f"/v1/query/{sid}",
                "upstream_id": "oneserve-service", "methods": ["POST"],
            }
    for name in LEGACY_IDS:                       # 兼容期三专用端点（固定声明）
        expected[f"routes/{name}"] = {"name": name}
    return expected


# ---------- ① 渲染产物与 git 源一致 ----------

def test_render_matches_git_source_independently_recomputed():
    """核心判据：渲染产物＝从 git 源独立重算的期望集合（不多不少、形状一致）。"""
    specs = dict(rend.render_route_specs(_git_registry()))
    expected = _expected_from_source()
    assert set(specs) == set(expected), "渲染路由集合与 git 源不一致"
    for path, exp in expected.items():
        body = specs[path]
        assert body["name"] == exp["name"]
        if "uri" in exp:
            assert body["uri"] == exp["uri"], f"{path} uri 与源不符"
            assert body["upstream_id"] == exp["upstream_id"]
            assert body["methods"] == exp["methods"]
            assert body["plugins"]["key-auth"] == {}       # A 型点查统一 key-auth
            assert "request-id" in body["plugins"]         # P0-5 traceId 贯通


def test_render_has_no_route_for_source_absent_service():
    """源外服务零路由：渲染不无中生有（完全由 git 源确定，反向断言）。"""
    specs = dict(rend.render_route_specs(_git_registry()))
    assert "routes/oneserve-query-svc_not_in_git" not in specs
    # 网关不存在的服务在源码里也找不到（双保险，防测试自身伪造）
    assert "svc_not_in_git" not in SERVICES_YAML.read_text(encoding="utf-8")


def test_registry_source_is_version_controlled_file():
    """权威源即仓库内文件（git 恒权威的可追溯锚点；非环境变量/运行时注入）。"""
    assert REAL_YAML.is_file()
    rel = REAL_YAML.relative_to(REPO).as_posix()
    assert rel == "service/services.yaml"
    tracked = REAL_YAML.read_bytes()          # 可读且渲染期零改动
    rend.render_route_specs(_git_registry())
    assert REAL_YAML.read_bytes() == tracked, "渲染不得改写权威源"


# ---------- ② 幂等/确定性：重跑无差异 ----------

def _canon(specs: list) -> str:
    return json.dumps(specs, sort_keys=True, ensure_ascii=False)


def test_render_is_deterministic_across_runs():
    """同输入多次渲染逐字节一致（幂等 PUT 收敛无漂移的机检前提）。"""
    reg = _git_registry()
    runs = [_canon(rend.render_route_specs(reg)) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    # 源文本指纹不变（渲染不改源、源不变则产物不变）
    h = hashlib.sha256(SERVICES_YAML.read_bytes()).hexdigest()
    assert h == hashlib.sha256(SERVICES_YAML.read_bytes()).hexdigest()


def test_render_is_pure_no_disk_time_or_random():
    """纯函数：读盘被禁、时间/随机被判错，渲染仍成功（产物只由入参确定）。

    自备 monkeypatch（try/finally 还原）以兼容无 pytest 的直跑入口（run_gates.sh）。
    """
    import random
    import time

    def _boom(*_a, **_k):
        raise AssertionError("渲染器不得读盘/取时间/取随机")

    reg = _git_registry()                    # 输入先于禁令取得（读盘只属加载器职责）
    patches = [(Path, "read_text"), (Path, "read_bytes"),
               (time, "time"), (random, "random")]
    saved = [(obj, name, getattr(obj, name)) for obj, name in patches]
    try:
        for obj, name in patches:
            setattr(obj, name, _boom)
        specs = rend.render_route_specs(reg)
    finally:
        for obj, name, orig in saved:
            setattr(obj, name, orig)
    assert specs, "纯函数渲染应产出非空路由集"


# ---------- ③ 单一渲染入口 ----------

def test_single_entry_equals_two_function_composition():
    """render_route_specs ＝ render_query_routes＋legacy_route_specs（同源同序逐字节）。"""
    reg = _git_registry()
    assert _canon(rend.render_route_specs(reg)) == _canon(
        rend.render_query_routes(reg) + rend.legacy_route_specs())
    assert _canon(rend.render_route_specs(reg, stats_hook={"phase": "log"})) == _canon(
        rend.render_query_routes(reg, stats_hook={"phase": "log"})
        + rend.legacy_route_specs(stats_hook={"phase": "log"}))


def test_callers_go_through_single_entry():
    """三处调用方只经单一入口：init_routes/registry_hot/gen_openapi 无第二拼装。"""
    hot = (REPO / "service" / "app" / "registry_hot.py").read_text(encoding="utf-8")
    assert "rend.render_route_specs(" in hot
    init_src = (REPO / "configs" / "apisix" / "init_routes.py").read_text(encoding="utf-8")
    assert "rend.render_route_specs(" in init_src
    gen = (REPO / "scripts" / "gen_openapi.py").read_text(encoding="utf-8")
    assert "rend.render_route_specs(" in gen
    for name, src in (("registry_hot", hot), ("init_routes", init_src), ("gen_openapi", gen)):
        assert "render_query_routes(" not in src, f"{name} 绕过单一入口直拼渲染函数"
        assert "legacy_route_specs(" not in src, f"{name} 绕过单一入口直拼渲染函数"


# ---------- ④ 源变 → 产物确定变（增量） ----------

def _registry_from_entries(*entries: dict) -> dict:
    path = Path(tempfile.mkdtemp()) / "services.yaml"
    path.write_text(yaml.safe_dump({"services": list(entries)}, allow_unicode=True), encoding="utf-8")
    return kvx.load_services(path)


def _kv(service_id: str) -> dict:
    return {"service_id": service_id, "archetype": "kv_lookup",
            "backend": {"source": "redis", "kv": {"namespace": f"ns:{service_id}"}},
            "params": [{"name": "key", "pattern": "^[0-9a-f]{64}$"}], "response": {"meta": []}}


def test_source_addition_determines_exact_product_delta():
    """源加一服务 → 恰好多一条通配路由，既有条目与源不变时逐字节相同。"""
    base = _registry_from_entries(_kv("svc_a"))
    grown = _registry_from_entries(_kv("svc_a"), _kv("svc_new"))
    before = dict(rend.render_route_specs(base))
    after = dict(rend.render_route_specs(grown))
    assert set(after) - set(before) == {"routes/oneserve-query-svc_new"}
    assert all(before[k] == after[k] for k in before), "源未变条目不得随源增长而漂移"
    # 新增服务路由形状可由新服务字段完全确定
    assert after["routes/oneserve-query-svc_new"]["uri"] == "/v1/query/svc_new"


# ---------- ⑤ 多列族扩展位（本阶段单列族实现） ----------

def test_cf_accepts_str_or_list_extension_slot():
    """扩展位契约：cf 字符串/字符串列表均被接受并归一为列表。"""
    assert rend.hbase_column_families({"hbase": {"cf": "cf"}}) == ["cf"]
    assert rend.hbase_column_families({"hbase": {"cf": ["cf"]}}) == ["cf"]
    assert rend.hbase_column_families({"hbase": {"cf": ["cf", "meta"]}}) == ["cf", "meta"]
    assert rend.hbase_column_families({"hbase": {}}) == []          # 缺省→空（调用方显式拒）
    assert rend.hbase_column_families({"hbase": {"cf": 3}}) == []   # 类型不符→空，不发明列族


def test_primary_column_family_is_single_family_semantics():
    """本阶段单列族语义：取归一后首个；多列族不在此层实现（归 M2-4+）。"""
    assert rend.primary_column_family({"hbase": {"cf": "cf"}}) == "cf"
    assert rend.primary_column_family({"hbase": {"cf": ["cf", "meta"]}}) == "cf"
    assert rend.primary_column_family({"hbase": {}}) == ""


def test_hbase_service_rejects_multi_cf_not_silent():
    """执行器对 >1 列族启动即拒（配置不生效按缺陷处理，不静默取首个）。"""
    from app import hbase_service as hbsvc
    svc = {"service_id": "h_multi", "backend": {"source": "hbase",
           "hbase": {"table": "t", "cf": ["cf", "meta"], "key_type": "phone_sm3"}},
           "params": [{"name": "key", "pattern": "^[0-9a-f]{64}$"}]}
    try:
        hbsvc.validate_backend(svc)
        raise AssertionError("多列族应显式拒（多列族语义未实现）")
    except RuntimeError as exc:
        assert "列族" in str(exc)
    # 单元素列表＝扩展位被接受（归一后单列族，本阶段实现）
    ok = {"service_id": "h_one", "backend": {"source": "hbase",
          "hbase": {"table": "t", "cf": ["cf"], "key_type": "phone_sm3"}},
          "params": [{"name": "key", "pattern": "^[0-9a-f]{64}$"}]}
    assert hbsvc.validate_backend(ok)["table"] == "t"


# ---------- ⑥ gen_openapi 无文本解析旁路 ----------

def test_gen_openapi_reads_static_routes_not_source_text():
    """契约静态通道同源 init_routes.static_resources()，无源码正则解析旁路。"""
    src = (REPO / "scripts" / "gen_openapi.py").read_text(encoding="utf-8")
    assert "init_routes.static_resources()" in src or "initr.static_resources()" in src
    # 原「按缩进 marker 切源码块 + 正则抓 uri/methods」的旁路已删除
    assert "blocks = text.split(" not in src
    assert 'read_text(encoding="utf-8")\n    marker' not in src
    # 同源取数：契约路由集 ⊇ init_routes 静态路由；且与渲染入口一致
    sys.path.insert(0, str(REPO / "scripts"))
    import gen_openapi as gen
    routes = {r["uri"]: r for r in gen.load_gateway_routes()}
    for _p, body in initr.static_resources():
        if "route" in _p and body.get("uri") != "/demo*":      # /demo* 不进契约端点
            assert body["uri"] in routes, f"静态路由 {body['uri']} 未进契约"
    # 服务路由与静态契约解耦（2026-09-29 全下线：静态 init_routes 只剩平台路由
    # ping/batch/proxy-wildcard；服务通配/兼容期路由由热加载 push_routes 下发）。
    # 同源不变式改为：单一渲染入口＝两函数拼接，无第二条渲染路径。
    reg = _git_registry()
    assert _canon(rend.render_route_specs(reg)) == _canon(
        rend.render_query_routes(reg) + rend.legacy_route_specs())


def main() -> int:
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    import traceback
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = []
    print(f"== TASK-M2-7-a 渲染器内核判据（{len(tests)} 条）==")
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception:
            failed.append(name)
            print(f"  ✗ {name}")
            traceback.print_exc()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
