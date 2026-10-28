# -*- coding: utf-8 -*-
"""注册表热加载单测（TASK-M2-3.5）——「services.yaml 变更 ≤60s 生效」的逻辑机检面。

判据映射（门槛稿 TASK-M2-3.5「热加载（消掉 services.yaml 只读一次性加载）」
＋AC-6「60 秒内可调…由 M2-3.5 渲染器＋热加载保证」）：
  ① mtime 触发：文件未变 → 不动内存；mtime 变化且 yaml 可解析 → 新注册表返回；
  ② 重载后注册表更新：新条目按 service_id 单键可查（query_any 分发前提）；
  ③ 坏配置挂起：坏 yaml/重复 id 注册 → (None, 原 mtime)＝下拍重试同一文件，
     运行期旧表不被半写/坏文件污染（不读脏）；
  ④ 换表语义：_promote_registry 产出的表与启动加载同构（service_id 单键）；
  ⑤ 网关下发胶水：push_routes 按渲染结果逐条 PUT（失败计数；下发失败不回滚
     注册表——AC-6 降级口径）；watcher_loop 后台循环 mtime 触发→apply 回调。
  网关 HTTP 胶水以记录式假 _admin_req 验证（不依赖 APISIX）；端到端 60s 实录
  归真栈判据验证（tests/evidence/m235-hot-reload.txt）。
"""

import asyncio
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import kv_executor as kvx
from synth_services import load_synth_services  # noqa: E402  合成 fixture（全下线终态）  # noqa: E402
from app import registry_hot as rhot  # noqa: E402


def _kv_entry(service_id: str) -> dict:
    return {"service_id": service_id, "archetype": "kv_lookup",
            "backend": {"source": "redis", "kv": {"namespace": f"ns:{service_id}"}},
            "params": [{"name": "key", "pattern": "^[0-9a-f]{64}$"}],
            "response": {"meta": []}, "guard": {}, "audit": {"enabled": False}}


def _write_yaml(path: Path, entries: list) -> None:
    path.write_text(yaml.safe_dump({"services": entries}, allow_unicode=True),
                    encoding="utf-8")


def _fresh_file(tmp: Path, entries: list) -> Path:
    path = tmp / "services.yaml"
    _write_yaml(path, entries)
    return path


# ---------- ① mtime 触发 ----------

def test_unchanged_mtime_is_noop(tmp_path):
    """文件未变 → (None, 原 mtime)：不读盘不换表（轮询零扰动）。"""
    path = _fresh_file(tmp_path, [_kv_entry("svc_a")])
    mtime = rhot._mtime(path)
    reg, new_mtime = asyncio.run(rhot.reload_if_changed(mtime, path=path, push=False))
    assert reg is None and new_mtime == mtime


def test_changed_mtime_triggers_reload(tmp_path):
    """mtime 变化 + yaml 可解析 → 返回新注册表＋新 mtime（热加载触发面）。"""
    path = _fresh_file(tmp_path, [_kv_entry("svc_a")])
    old_mtime = rhot._mtime(path)
    _write_yaml(path, [_kv_entry("svc_a"), _kv_entry("svc_new")])   # 模拟改 yaml 加服务
    reg, new_mtime = asyncio.run(rhot.reload_if_changed(old_mtime, path=path, push=False))
    assert reg is not None and new_mtime == rhot._mtime(path) and new_mtime != old_mtime
    assert "svc_new" in reg and reg["svc_new"]["service_id"] == "svc_new"


# ---------- ② 重载后注册表更新（单键语义不丢） ----------

def test_reloaded_registry_single_key_lookups(tmp_path):
    """换表后：新服务 service_id 单键可查；注册表无 tuple 键（AC-1a 换表后不丢）。"""
    path = _fresh_file(tmp_path, [_kv_entry("svc_a")])
    old_mtime = rhot._mtime(path)
    _write_yaml(path, [_kv_entry("svc_a"), _kv_entry("svc_new")])
    reg, _ = asyncio.run(rhot.reload_if_changed(old_mtime, path=path, push=False))
    assert reg["svc_a"]["service_id"] == "svc_a"
    assert reg["svc_new"]["service_id"] == "svc_new"
    assert not any(isinstance(k, tuple) for k in reg)        # 单键形态换表后保持


# ---------- ③ 坏配置挂起（不读脏） ----------

def test_bad_yaml_pends_with_original_mtime(tmp_path):
    """坏 yaml → (None, 原 mtime)：旧表不动；mtime 原样带回＝下拍重试同一文件。"""
    path = _fresh_file(tmp_path, [_kv_entry("svc_a")])
    old_mtime = rhot._mtime(path)
    path.write_text("services: [ { broken", encoding="utf-8")
    reg, new_mtime = asyncio.run(rhot.reload_if_changed(old_mtime, path=path, push=False))
    assert reg is None and new_mtime == old_mtime
    # 修好后下一拍收敛（文件修好自动恢复，无需重启）
    _write_yaml(path, [_kv_entry("svc_a"), _kv_entry("svc_fixed")])
    reg2, m2 = asyncio.run(rhot.reload_if_changed(new_mtime, path=path, push=False))
    assert reg2 is not None and "svc_fixed" in reg2


def test_duplicate_service_id_pends_not_crashes(tmp_path):
    """同 id 重复注册（启动口径即失败）在热加载侧＝挂起重试，不砸看门循环。"""
    path = _fresh_file(tmp_path, [_kv_entry("svc_a")])
    old_mtime = rhot._mtime(path)
    _write_yaml(path, [_kv_entry("svc_a"), _kv_entry("svc_a")])
    reg, new_mtime = asyncio.run(rhot.reload_if_changed(old_mtime, path=path, push=False))
    assert reg is None and new_mtime == old_mtime


def test_missing_file_is_noop(tmp_path):
    """文件缺失（半写窗口/误删）→ None 跳过本拍，不误判变更。"""
    reg, mtime = asyncio.run(rhot.reload_if_changed(None, path=tmp_path / "nope.yaml",
                                                    push=False))
    assert reg is None and mtime is None


# ---------- ④ 换表语义：热加载表与启动表同构 ----------

def test_promoted_registry_isomorphic_to_startup_load():
    """_promote_registry：热加载表与启动加载表同构（service_id 单键，无 tuple 键）。"""
    start = load_synth_services()   # 合成注册表（单键；含 kv/news/hbase 型多样本）
    reg = rhot._promote_registry(start)
    assert not any(isinstance(k, tuple) for k in reg)      # 单键形态在换表路径保持
    assert "user_tag_query" in reg and "news_tag_query" in reg
    assert "demo_hbase_query" not in reg                    # 已下线服务不进注册表（合成表亦不含）
    assert dict(reg) == dict(start)                        # 内容逐键一致


# ---------- ⑤ 网关下发胶水（记录式假 Admin API） ----------

def _fake_admin(code: int):
    def _req(admin, admin_key, method, path, body=None):
        _req.calls.append((method, path, body))
        return code
    _req.calls = []
    return _req


def test_push_routes_puts_all_rendered_routes(monkeypatch):
    """push_routes：通配＋兼容期路由逐条幂等 PUT，形状＝渲染器输出（不复制第二份）。"""
    fake = _fake_admin(201)
    monkeypatch.setattr(rhot, "_admin_req", fake)
    ok, bad = asyncio.run(rhot.push_routes(load_synth_services()))
    paths = [p for _m, p, _b in fake.calls]
    assert all(m == "PUT" for m, _p, _b in fake.calls)
    assert ok == len(paths) and bad == 0
    assert "routes/oneserve-query-news_tag_query" in paths
    assert "routes/oneserve-query-user_tag_query" in paths
    assert "routes/oneserve-query-usr_basic_info_query" in paths   # 合成 hbase 型业务表服务
    assert "routes/oneserve-query-demo_hbase_query" not in paths   # 已下线 demo 服务不渲染
    assert "routes/oneserve-v1-tags-query" in paths        # 兼容期专用路由随行下发（产品口径：调用方迁移后整段删除）
    body = next(b for _m, p, b in fake.calls if p == "routes/oneserve-query-news_tag_query")
    assert body["uri"] == "/v1/query/news_tag_query" and body["methods"] == ["POST"]


def test_push_failure_keeps_new_registry(monkeypatch, tmp_path):
    """下发失败（admin 不可达）→ bad 计数；注册表仍换新（AC-6 降级口径，续跑追平）。"""
    fake = _fake_admin(0)                                  # 0＝网络不可达
    monkeypatch.setattr(rhot, "_admin_req", fake)
    path = _fresh_file(tmp_path, [_kv_entry("svc_a")])
    old_mtime = rhot._mtime(path)
    _write_yaml(path, [_kv_entry("svc_a"), _kv_entry("svc_new")])
    reg, _ = asyncio.run(rhot.reload_if_changed(old_mtime, path=path, push=True))
    assert fake.calls and reg is not None and "svc_new" in reg   # 网关失败不回滚注册表


# ---------- watcher 后台循环（asyncio 集成：mtime 触发→apply 回调） ----------

def test_watcher_loop_applies_on_change_and_cancels_cleanly(tmp_path):
    """watcher_loop：改 yaml → apply 回调携新注册表被调；cancel 优雅退出不抛。"""
    path = _fresh_file(tmp_path, [_kv_entry("svc_a")])
    applied: list = []

    async def scenario():
        task = asyncio.create_task(rhot.watcher_loop(
            lambda: None,
            lambda reg, mtime: applied.append((reg, mtime)),
            path=path, interval_s=0.02, push=False))
        await asyncio.sleep(0.06)                          # 首拍：未变
        assert not applied
        _write_yaml(path, [_kv_entry("svc_a"), _kv_entry("svc_new")])   # 改文件
        for _ in range(100):                               # 下一拍内生效（轮询间隔 20ms）
            await asyncio.sleep(0.02)
            if applied:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass   # 惯例：看门把取消向上传播（main.lifespan 同款 try/except 接法）

    asyncio.run(scenario())
    assert applied and "svc_new" in applied[0][0]          # 重载后注册表更新到位


def test_watcher_loop_survives_bad_config(tmp_path):
    """坏配置期间看门不退出；修好后自动收敛（配置事故不打断热加载）。"""
    path = _fresh_file(tmp_path, [_kv_entry("svc_a")])
    applied: list = []

    async def scenario():
        task = asyncio.create_task(rhot.watcher_loop(
            lambda: None,
            lambda reg, mtime: applied.append(reg),
            path=path, interval_s=0.02, push=False))
        await asyncio.sleep(0.05)
        path.write_text("services: [ { broken", encoding="utf-8")       # 坏配置若干拍
        await asyncio.sleep(0.08)
        assert task.done() is False                        # 看门仍活着
        _write_yaml(path, [_kv_entry("svc_a"), _kv_entry("svc_healed")])  # 修好
        for _ in range(100):
            await asyncio.sleep(0.02)
            if applied:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert applied and "svc_healed" in applied[0]
