# -*- coding: utf-8 -*-
"""限流口径落地（P1-5 · SPEC §4.6/§5-P1-5）——纯逻辑，不 import FastAPI/redis。

范围与边界：
  - 本模块只产出「按 route×consumer 维度的 APISIX 限流插件配置」与 429 原因头
    口径常量；真正的拦截由 APISIX limit-count / limit-conn 插件在网关执行
    （网关通道见 app/gw_consumer.py，落库登记见 app/appkeys.py）；
  - 阈值**单一事实来源**＝appkeys.yaml 登记值（quota_qps＝路由速率、quota_daily＝
    日配额）；门户四要素与网关 count 都从这里取，不存在「页面说 100、网关拦 50」
    的第二份口径（机检见 tests/test_ratelimit.py）；
  - 未登记配额的调用方（如 auth_matrix 直发的演示凭证）不施加限流——返回空配置，
    如实「不限」，不揣测默认值（SPEC D7 默认额度未定案前不虚设）。

429 原因头（X-RateLimit-Reason，取值与批量侧同族，SPEC §4.6）：
  - global     ：撞日配额（quota_daily，跨路由累计）；
  - endpoint   ：撞路由速率（quota_qps，route×consumer 维度）；
  - concurrent ：撞并发上限（platform 常量，尚未按 appkey 登记——如实标注）。

实现口径（真栈实测，APISIX 3.18.0-debian）：
  - 限流键按 $consumer_name|$route_id 组合＝「route×consumer」维度；
  - 原因判别读 ctx.var.rate_limiting_info（limit-count 拒绝时写入被撞规则的键），
    键含 "|global" / "|endpoint" 分派；limit-conn 不写该变量，故兜底 concurrent；
  - 原因头经 serverless-post-function（phase=header_filter）补挂——limit-count /
    limit-conn 自身只设配额头，不设原因头。
"""
from __future__ import annotations

# ---------- 429 原因头（与批量侧同族：Stripe 式机读原因） ----------

REASON_HEADER = "X-RateLimit-Reason"
RATE_LIMIT_REASON_GLOBAL = "global"          # 日配额（quota_daily）
RATE_LIMIT_REASON_ENDPOINT = "endpoint"      # 路由速率（quota_qps）
RATE_LIMIT_REASON_CONCURRENT = "concurrent"  # 并发（平台常量）

RATE_LIMIT_REASONS = (
    RATE_LIMIT_REASON_GLOBAL,
    RATE_LIMIT_REASON_ENDPOINT,
    RATE_LIMIT_REASON_CONCURRENT,
)

# 限流键（含 route_id ⇒ route×consumer 维度；hook 按后缀分派原因）
ENDPOINT_KEY = "$consumer_name|$route_id|endpoint"
GLOBAL_KEY = "$consumer_name|global"
CONCURRENT_KEY = "$consumer_name|$route_id|concurrent"

RATE_WINDOW_S = 1          # 路由速率窗＝1s（quota_qps 字面语义即每秒次数）
DAILY_WINDOW_S = 86400     # 日配额窗＝24h（与门户「东八区次日 00:00 重置」口径同量级）
# 并发上限：平台常量（registry 尚无 quota_concurrent 登记字段，最小实现不新增字段）
DEFAULT_MAX_CONCURRENT = 20

REJECTED_CODE = 429
RATE_LIMITED_MSG = "rate limited"
CONCURRENT_MSG = "too many concurrent requests"

# 原因头钩子（header_filter）：仅在**网关本地**拒绝（upstream_status 为空）时补头，
# 服务层自身 429（如批量配额）不覆盖——那类 429 的原因头由 batch.quota_headers 出。
REASON_HOOK_LUA = """
return function(conf, ctx)
    if tonumber(ngx.status) ~= 429 then return end
    if ngx.var.upstream_status then return end
    local reason = "concurrent"
    local info = ctx.var.rate_limiting_info
    if info then
        if string.find(info, "|global", 1, true) then
            reason = "global"
        elseif string.find(info, "|endpoint", 1, true) then
            reason = "endpoint"
        end
    end
    ngx.header["X-RateLimit-Reason"] = reason
end
"""


def reason_hook() -> dict:
    """原因头钩子的插件配置（每次返回新 dict，防调用方就地改写共享常量）。"""
    return {"phase": "header_filter", "functions": [REASON_HOOK_LUA]}


def _positive_int(value) -> int | None:
    """登记值 → 正整数；None/空/非数/≤0 一律视为「未登记」（不揣测默认值）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def consumer_plugins(quota_qps, quota_daily,
                     max_concurrent: int = DEFAULT_MAX_CONCURRENT) -> dict:
    """登记配额 → APISIX consumer 限流插件段（route×consumer 维度）。

    返回可直接并进 consumer.plugins 的 dict。两档配额都未登记 → 返回 {}（不限流，
    如实；调用方据此只挂 key-auth）。仅登记一档时，只挂对应规则＋并发闸。
    """
    qps = _positive_int(quota_qps)
    daily = _positive_int(quota_daily)
    if qps is None and daily is None:
        return {}
    rules: list[dict] = []
    if qps is not None:
        rules.append({"count": qps, "time_window": RATE_WINDOW_S,
                      "key": ENDPOINT_KEY, "header_prefix": "Endpoint"})
    if daily is not None:
        rules.append({"count": daily, "time_window": DAILY_WINDOW_S,
                      "key": GLOBAL_KEY, "header_prefix": "Global"})
    return {
        "limit-count": {
            "policy": "local",
            "rejected_code": REJECTED_CODE,
            "rejected_msg": RATE_LIMITED_MSG,
            "show_limit_quota_header": True,
            "rules": rules,
        },
        "limit-conn": {
            "policy": "local",
            "conn": int(max_concurrent), "burst": 0, "default_conn_delay": 0.001,
            "key": CONCURRENT_KEY,
            "rejected_code": REJECTED_CODE,
            "rejected_msg": CONCURRENT_MSG,
        },
        "serverless-post-function": reason_hook(),
    }


def consumer_body(appkey: str, quota_qps=None, quota_daily=None) -> dict:
    """consumer 注册体：key-auth（identity）＋（有登记则）限流插件——网关 PUT 的单一构造点。

    与 gw_consumer.put_consumer 共用；未登记配额时输出与 P0-4 既有形状逐字段一致
    （「只挂 key-auth」），保证存量调用方零行为变化。
    """
    plugins: dict = {"key-auth": {"key": appkey}}
    plugins.update(consumer_plugins(quota_qps, quota_daily))
    return {"username": appkey, "plugins": plugins}
