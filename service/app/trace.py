"""traceId 解析与生成（P0-5 纯逻辑；main.py 中间件是本模块的薄胶水）。

SPEC §4.5 traceId 贯通：APISIX request-id 生成 → header 透传 → 服务层接收（缺则
自生成，保证不经网关直达 service 的调用也有 trace）→ 审计第八列＋错误体注入。

header 名：任务卡口径 X-Trace-Id（APISIX request-id header_name 同值；插件对
客户端已携带的同名头复用透传）。读侧同时兼容 SPEC §4.5 行文的 X-OneServe-Trace-Id
——两个名字任一有值即认，写侧（响应回传）只出 X-Trace-Id，单一出口不歧义。
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping

# 网关透传/响应回传的正式 header 名（任务卡口径；APISIX request-id 插件同值）
TRACE_HEADER = "X-Trace-Id"
# 读侧兼容的备用 header 名（SPEC §4.5 行文名；仅读取，不回写）
ALT_TRACE_HEADER = "X-OneServe-Trace-Id"


def new_trace_id() -> str:
    """生成标准 UUID（36 位带连字符，与 APISIX request-id 默认算法同格式）。

    直连 service（不经网关）时服务层自生成；格式与网关侧一致，反查时形态统一。
    """
    return str(uuid.uuid4())


def resolve_trace_id(headers: Mapping[str, str], maker: Callable[[], str] = new_trace_id) -> str:
    """按 header 解析本次调用的 traceId：有则透传（X-Trace-Id 优先），缺则生成。

    headers 键大小写不敏感（网关/代理实现的 header 大小写无保证）。
    """
    lowered = {str(k).lower(): v for k, v in headers.items()}
    for name in (TRACE_HEADER, ALT_TRACE_HEADER):
        value = (lowered.get(name.lower()) or "").strip()
        if value:
            return value
    return maker()
