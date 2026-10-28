# -*- coding: utf-8 -*-
"""路由/执行器配置渲染器单测（TASK-M2-3.5）——AC-6 判据的纯逻辑机检面。

判据映射（门槛稿 TASK-M2-3.5 行「判据＝只改 yaml、零代码改动，60s 内新服务
curl 可调」＋AC-6「由 M2-3.5 渲染器＋热加载保证」）：
  ① yaml → 路由条目映射：A 型点查服务（backend.kv / backend.hbase）逐条渲染
     /v1/query/{service_id} 通配路由（POST＋key-auth＋request-id）；B 型代理等
     非点查型不渲染（既有专用通道承接）；
  ② 幂等：渲染是注册表纯函数——同输入两渲染逐字节一致（Admin API 幂等 PUT
     收敛无漂移的机检前提）；
  ③ 增量：注册表加一个服务条目 → 渲染恰好多一条路由，既有条目形状不变
     （「新增服务零代码改动」的渲染器侧语义）；
  ④ 单键计数：注册表条目数＝渲染路由数（service_id 单键，无 latest 指针）；
  ⑤ init_routes 接线机检：渲染函数被 init_routes 调用、静态点查路由块已删
     （消双份事实来源）；main.py 接通配端点。

被测对象 service/app/registry_render.py（不 import fastapi/redis——宿主机
无容器依赖可全路径覆盖）。
"""

import json
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import kv_executor as kvx  # noqa: E402
from app import registry_render as rend  # noqa: E402
from app import route_naming as rn  # noqa: E402  对外/内部路由 id 前缀单一来源

from synth_services import load_synth_services, synth_matrix, synth_services_dict, real_registry_state  # noqa: E402  合成 fixture（全服务下线，见模块头注）
REGISTRY = load_synth_services()              # 合成注册表（全下线终态；reality 用例另守空表）
REAL_REGISTRY, _ERR = real_registry_state()      # 现实态（空表＋fail-loud）


def _kv_entry(service_id: str) -> dict:
    """最小 A 型 KV 点查条目（backend.kv 在场＝渲染判定命中）。"""
    return {"service_id": service_id, "archetype": "kv_lookup",
            "backend": {"source": "redis", "kv": {"namespace": f"ns:{service_id}"}},
            "params": [{"name": "key", "pattern": "^[0-9a-f]{64}$"}],
            "response": {"meta": []}, "guard": {}, "audit": {"enabled": False}}


def _hbase_entry(service_id: str) -> dict:
    """最小 hbase 型点查条目（backend.hbase 在场）。"""
    return {"service_id": service_id, "archetype": "kv_lookup",
            "backend": {"source": "hbase", "hbase": {"table": "default:t1", "cf": "cf",
                                                     "key_type": "phone_sm3"}},
            "params": [{"name": "key", "pattern": "^[0-9a-f]{64}$"}],
            "response": {"meta": []}, "guard": {}, "audit": {"enabled": False}}


def _proxy_entry(service_id: str) -> dict:
    """最小 B 型代理条目（backend.proxy；无 source——回归 M2-3.5 实测误判样本）。"""
    return {"service_id": service_id, "archetype": "external_proxy",
            "backend": {"proxy": {"upstream_url": "http://mock:9100"}},
            "params": [{"name": "key"}], "guard": {}, "audit": {"enabled": False}}


def _registry(*entries: dict) -> dict:
    """构造单键注册表（复用 kvx.load_services 的 service_id 单键语义）。"""
    data = {"services": list(entries)}
    path = Path(tempfile.mkdtemp()) / "services.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return kvx.load_services(path)


# ---------- ① yaml → 路由条目映射 ----------

def test_real_registry_renders_query_routes_for_lookup_services():
    """注册表 → 路由：A 型点查服务各渲染一条 /v1/query/<id>（POST＋key-auth）。"""
    specs = dict(rend.render_query_routes(REGISTRY))
    assert sorted(specs) == [
        "routes/oneserve-query-news_tag_query",
        "routes/oneserve-query-user_tag_query",
        "routes/oneserve-query-usr_basic_info_query",
        "routes/oneserve-query-v1-svc-usr-asset-tag",
    ]
    for path, body in specs.items():
        assert body["uri"] == "/v1/query/" + path.removeprefix("routes/oneserve-query-")
        assert body["methods"] == ["POST"]
        assert body["plugins"]["key-auth"] == {}
        assert body["upstream_id"] == "oneserve-service"


def test_real_registry_renders_route_per_online_lookup_service():
    """现实态守卫：真实注册表 → 每个在线 A 型点查服务恰渲染一条路由（空表自然零渲染）。

    2026-09-30 现实态口径：首个真实服务上线后注册表非空——期望集按在线 kv_lookup
    服务动态推导（合规 routePath → oneserve-svc-* 对外出口，无 routePath →
    oneserve-query-* 内部通道）；注册表回空时期望集自然为空集，原「空表零渲染」
    语义保留。
    """
    assert not _ERR   # fail-loud 语义照守（load 失败须报错，不许静默空表）
    specs = dict(rend.render_query_routes(REAL_REGISTRY))
    online = {sid: s for sid, s in REAL_REGISTRY.items()
              if isinstance(sid, str) and s.get("archetype") == "kv_lookup"}
    expected = {"routes/" + (rend.svc_route_id(sid)
                             if str(s.get("route") or "").startswith(rn.ROUTE_PREFIX)
                             else rend.query_route_id(sid))
                for sid, s in online.items()}
    assert set(specs) == expected, f"渲染路由与在线点查服务漂移：{sorted(specs)} vs {sorted(expected)}"


def test_proxy_service_not_rendered():
    """B 型代理不进通配路由（backend.proxy 无 source 不误判 redis——实测回归样本）。"""
    specs = dict(rend.render_query_routes(_registry(_proxy_entry("partner_proxy"))))
    assert specs == {}


def test_hbase_service_rendered_like_kv():
    """hbase 数据源服务与 KV 同构渲染（backend.hbase 在场即点查型）。"""
    specs = dict(rend.render_query_routes(_registry(_hbase_entry("h1"))))
    assert list(specs) == ["routes/oneserve-query-h1"]


# ---------- ② 幂等 ----------

def test_render_is_idempotent():
    """同输入两次渲染逐字节一致（幂等 PUT 收敛无漂移的机检前提）。"""
    a = rend.render_query_routes(REGISTRY) + rend.legacy_route_specs()
    b = rend.render_query_routes(REGISTRY) + rend.legacy_route_specs()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_legacy_routes_keep_shape_and_ids():
    """兼容期专用端点路由：uri/id 与既有 init_routes 值一致（幂等覆盖不漂移）。"""
    specs = dict(rend.legacy_route_specs())
    assert specs["routes/oneserve-v1-tags-query"]["uri"] == "/v1/tags/query"
    assert specs["routes/oneserve-v1-news-query"]["uri"] == "/v1/news/query"
    assert specs["routes/oneserve-v1-hbase-query"]["uri"] == "/v1/hbase/query"
    for body in specs.values():
        assert body["methods"] == ["POST"] and body["plugins"]["key-auth"] == {}


# ---------- ③ 增量：加服务＝恰好多一条路由 ----------

def test_new_service_adds_exactly_one_route():
    """注册表加一个条目 → 渲染恰好多一条路由，既有条目形状不变（零代码改动语义）。"""
    base = _registry(_kv_entry("svc_a"), _kv_entry("svc_b"))
    grown = _registry(_kv_entry("svc_a"), _kv_entry("svc_b"), _kv_entry("svc_new"))
    before = dict(rend.render_query_routes(base))
    after = dict(rend.render_query_routes(grown))
    assert set(after) - set(before) == {"routes/oneserve-query-svc_new"}
    assert before == {k: after[k] for k in before}   # 既有条目逐字段不变


# ---------- ④ 单键计数：条目数＝路由数 ----------

def test_render_count_equals_entry_count():
    """单键注册表：每个 A 型点查条目恰渲染一条路由（无 latest 指针去重口径）。"""
    specs = dict(rend.render_query_routes(_registry(_kv_entry("svc_a"),
                                                    _kv_entry("svc_b"),
                                                    _kv_entry("svc_c"))))
    assert sorted(specs) == ["routes/oneserve-query-svc_a",
                             "routes/oneserve-query-svc_b",
                             "routes/oneserve-query-svc_c"]
    assert specs["routes/oneserve-query-svc_a"]["uri"] == "/v1/query/svc_a"


# ---------- ⑤ 接线机检（胶水存在性；端到端归真栈判据验证） ----------

def test_init_routes_wires_renderer_and_drops_static_query_blocks():
    """init_routes：经单一渲染入口＋读注册表；静态 tags/news/hbase 路由块已删。

    TASK-M2-7-a：入口收敛为 rend.render_route_specs（不再自拼两函数＝消第二路径）。
    """
    import py_compile
    src_path = REPO / "configs" / "apisix" / "init_routes.py"
    py_compile.compile(str(src_path), doraise=True)
    src = src_path.read_text(encoding="utf-8")
    assert "render_route_specs(" in src
    assert "render_query_routes(" not in src     # 入口收敛：不直接拼两函数
    assert "legacy_route_specs(" not in src
    assert "_load_registry()" in src
    # 静态点查路由块删除：三处硬编码 uri 不再出现在静态 resources 里（渲染器单一事实来源）
    assert '"uri": "/v1/tags/query"' not in src
    assert '"uri": "/v1/news/query"' not in src
    assert '"uri": "/v1/hbase/query"' not in src


def _lifespan_cancelled_watchers(src: str) -> set:
    """AST 不变式：main.py lifespan 关停段对哪些看门任务发起了 cancel。

    认两形态（断行为不断实现快照）：① 直接 <name>.cancel()；
    ② for <v> in (<a>, <b>, ...): … <v>.cancel()（循环遍历取消，含嵌套 if）。
    返回被取消任务变量名集合。
    """
    import ast
    tree = ast.parse(src)
    lifespan = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    cancelled: set = set()
    for node in ast.walk(lifespan):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "cancel" and isinstance(node.func.value, ast.Name)):
            cancelled.add(node.func.value.id)          # 形态①：直呼取消
        if isinstance(node, ast.For):
            iter_names = {e.id for e in node.iter.elts
                          if isinstance(node.iter, (ast.Tuple, ast.List))
                          and isinstance(e, ast.Name)}
            target_id = node.target.id if isinstance(node.target, ast.Name) else None
            for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "cancel" and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == target_id):
                    cancelled |= iter_names         # 形态②：循环变量取消 → 迭代集全取消
    return cancelled


def test_main_wires_wildcard_endpoint_and_hot_reload():
    """main.py：通配端点 /v1/query/{service_id} 接公共查询层；watcher 接 lifespan。"""
    import py_compile
    src_path = REPO / "service" / "app" / "main.py"
    py_compile.compile(str(src_path), doraise=True)
    src = src_path.read_text(encoding="utf-8")
    assert '@app.post("/v1/query/{service_id}")' in src
    # 2026-10-15 双看门形态（ADR-0013 upstreams 看门）：断行为不变式——lifespan
    # 关停段对每只看门任务都发起 cancel（认直呼与循环遍历两形态），
    # 不断「registry_watcher.cancel()」快照字面量（main.py 已改双看门循环，功能等价）。
    assert {"registry_watcher", "upstreams_watcher"} <= _lifespan_cancelled_watchers(src), \
        "lifespan 关停未对全部看门任务（registry_watcher/upstreams_watcher）发起 cancel"
    assert "asyncio.create_task(" in src                 # 看门后台任务
    assert "_apply_registry_state" in src                # 换表回调（全局+app.state 同帧）
    assert "reload_auth_state(app)" in src               # 新服务授权随行刷新（P0-4 复用）
    assert "rhot.SERVICES_YAML" in src                   # 轮询路径＝注册表权威文件
