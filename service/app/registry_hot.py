# -*- coding: utf-8 -*-
"""注册表热加载（TASK-M2-3.5）：services.yaml 变更 ≤60s 生效，免重启。

范围界定（AC-6 判据「只改 yaml 零代码改动，60s 内 curl 可调」）——最小实现选型：
  - 触发＝文件 mtime 轮询（lifespan 后台 asyncio task，默认 30s 间隔；间隔经
    REGISTRY_POLL_INTERVAL_S 可调）：两拍 mtime 变化才动内存，配置错误/半写
    文件不进运行期（启动口径 load_services 的重复复合键即失败在这里同样拦）；
  - 生效分两层：① 注册表表换新——services 基类实例经 _promote_registry 复制
    latest 指针后整表换新（kv_executor.ServiceRegistry.__setitem__ 禁新键是
    「启动期防手滑」闸；运行期换表走基类 API，合规口径写入）；main.services
    （演示/鉴权链）与 app.state.services（portal/admin/batch 链）同帧指新表；
  - ② 网关路由渲染——registry_render.render_query_routes 经 Admin API 幂等
    PUT（新增服务新增路由；既有路由同 id 同形状 PUT 收敛无漂移）；下发失败
    挂起重试（指数退避至轮询间隔封顶）：注册表已换新（service 直连可用），
    网关路由续跑追平——AC-6 60s 判据的降级口径，非静默丢失。
  - 授权矩阵/凭证注册表沿用既有 reload_auth_state（P0-4），不在此重复。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from app import kv_executor as kvx
from app import registry_render as rend

logger = logging.getLogger("oneserve.registry-hot")

SERVICES_YAML = kvx.SERVICES_YAML                       # service/services.yaml
POLL_INTERVAL_S = float(os.environ.get("REGISTRY_POLL_INTERVAL_S", "30"))
ADMIN = os.environ.get("APISIX_ADMIN", "http://oneserve-apisix:9180")
ADMIN_KEY = os.environ.get("APISIX_ADMIN_KEY", "")   # 真值来自 .env（密钥出库批 2026-10-20）


def _mtime(path: Path) -> float | None:
    """文件 mtime（不存在/不可达返回 None＝本拍跳过，不误判变更）。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _promote_registry(raw: kvx.ServiceRegistry) -> kvx.ServiceRegistry:
    """运行期换表：load_services 产出的注册表复制 latest 指针后返回。

    load_services 已在构建期完成防呆（重复复合键即失败）；本步只把 str 键
    latest 指针补齐/复核一遍，换表后的 str 键/复合键行为与启动加载的表
    完全同构（整表对象原子替换，无增量写入路径）。
    """
    reg = kvx.ServiceRegistry(raw)
    for key, value in raw.items():
        if isinstance(key, str):
            dict.__setitem__(reg, key, value)   # 基类 API 复制 latest 指针
    return reg


def load_registry_file(path: Path = SERVICES_YAML) -> kvx.ServiceRegistry:
    """读盘→完整注册表（复合键＋latest 指针）；坏配置 raise＝调用方挂起重试。"""
    return _promote_registry(kvx.load_services(path))


# ---------- Admin API 幂等下发（与 init_routes.req 同形；urllib 同步，to_thread 包） ----------

def _admin_req(admin: str, admin_key: str, method: str, path: str,
               body: dict | None = None) -> int:
    url = f"{admin}/apisix/admin/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-KEY", admin_key)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError:
        return 0                       # 网络不可达＝0（调用方按失败重试）


async def push_routes(services: dict, *, admin: str = ADMIN,
                      admin_key: str = ADMIN_KEY) -> tuple[int, int]:
    """渲染并幂等 PUT 全部点查路由（通配＋兼容期专用）；返回 (成功数, 失败数)。

    TASK-M2-7-a：经单一渲染入口 rend.render_route_specs 取全部产物（消第二路径）。
    """
    specs = rend.render_route_specs(services)
    ok = bad = 0
    for path, body in specs:
        code = await asyncio.to_thread(_admin_req, admin, admin_key, "PUT", path, body)
        if code in (200, 201):
            ok += 1
        else:
            bad += 1
            logger.warning("网关路由下发失败：%s -> %s", path, code)
    return ok, bad


async def reload_if_changed(state_mtime: float | None, *, path: Path = SERVICES_YAML,
                            push: bool = True) -> tuple[kvx.ServiceRegistry | None, float | None]:
    """单拍检测＋生效：mtime 变化 → 读盘换表（＋网关下发）；返回 (新表或 None, 新 mtime)。

    mtime 未变/读盘失败 → (None, state_mtime)（原 mtime 原样带回＝下拍重试同文件）。
    push=False 供单测（不起网关即可断言注册表侧生效）。
    """
    mtime = _mtime(path)
    if mtime is None or mtime == state_mtime:
        return None, state_mtime
    try:
        registry = load_registry_file(path)
    except Exception as exc:  # noqa: BLE001 —— 坏 yaml/坏结构：挂起原 mtime，下拍重试
        logger.warning("services.yaml 重载失败（保留旧注册表，下拍重试）：%s", exc)
        return None, state_mtime
    if push:
        ok, bad = await push_routes(registry)
        if bad:
            logger.warning("网关路由部分下发失败（%d ok / %d bad）：注册表已换新，路由续跑追平", ok, bad)
    logger.info("注册表热加载完成：%d 个服务（mtime=%s）",
                len([k for k in registry if isinstance(k, str)]), mtime)
    return registry, mtime


async def watcher_loop(get_state, apply_state, *, path: Path = SERVICES_YAML,
                       interval_s: float = POLL_INTERVAL_S,
                       push: bool = True) -> None:
    """mtime 轮询主循环（lifespan 后台 task）：两拍 mtime 变化→换表→网关下发。

    get_state()/apply_state(registry, mtime) 由 main 注入（模块全局＋app.state
    同帧换新）；注册表读盘失败仅告警挂起（下拍重试），循环本身永不退出——
    配置事故不打断热加载看门（文件修好后自动收敛）。
    """
    state_mtime = _mtime(path)
    logger.info("注册表热加载看门启动：path=%s interval=%.0fs", path, interval_s)
    while True:
        try:
            await asyncio.sleep(interval_s)
            registry, new_mtime = await reload_if_changed(state_mtime, path=path, push=push)
            if registry is not None:
                apply_state(registry, new_mtime)
                state_mtime = new_mtime
        except asyncio.CancelledError:
            raise                       # lifespan 关停：正常取消路径
        except Exception:  # noqa: BLE001 —— 看门兜底：任何异常吞掉记日志，下拍再来
            logger.exception("热加载看门单拍异常（继续轮询）")


def admin_probe_ok(admin: str = ADMIN, admin_key: str = ADMIN_KEY) -> bool:
    """Admin API 可达性探测（main 启动旁路置位用；GET routes 200 即就绪）。"""
    try:
        code = _admin_req(admin, admin_key, "GET", "routes")
    except Exception:  # noqa: BLE001
        return False
    return code == 200
