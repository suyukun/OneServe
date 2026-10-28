# -*- coding: utf-8 -*-
"""mock 网关目标（设计稿 §4.3：目标不可达时的降级模式——无服务也能起面板演示）。

行为模拟对象＝APISIX 9080 ＋薄服务层（service/app/main.py 黑盒契约）：
  POST /v1/tags/query   —— apikey 头鉴权；请求体 {"key": <键>}；
                           响应 200 {service_id,key,tenant,found,fields,data_as_of,live_version}；
                           非法键 400 {"error":"invalid_key"}；缺 apikey 401。
  POST /demo/query      —— 手机号 11 位（1[3-9] 开头）；服务端生成查询键；响应加 channel/note。
  限流（S3）           —— 按 apikey 记滚动窗口计数：超配额 → 429 {"error":"rate_limited"}。

时延画像：对数正态（P50≈85ms / P99≈100ms 量级，参照 2026-09-20 实测 100 档分布形状），
仅用于让面板数字形态可信；面板/报告均显式标注 MOCK。
"""
from __future__ import annotations

import json
import math
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY_RE = re.compile(r"^[0-9a-f]{64}$")
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

_FIELDS = {
    "ac_active_sts_01": "1", "ac_login_sts_03": "0", "if_acct_sts_15": "2",
    "mk_touch_sts_04": "1", "ac_click_sts_01": "1", "ac_vist_sts_21": "0",
}


class QuotaTracker:
    """按 apikey 的滚动窗口限流（模拟网关 limit-count 插件；窗口默认 1s）。"""

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, apikey: str, quota: int) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(apikey, []) if now - t < self.window_s]
            ok = len(hits) < quota
            if ok:
                hits.append(now)
            self._hits[apikey] = hits
            return ok


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive：与经网关打真实栈的连接形态一致
    server_version = "OneServeMock/1.0"

    def log_message(self, fmt, *args):  # 静默：压测下逐请求日志会拖垮自身
        pass

    # ---- 基础设施 -------------------------------------------------------
    def _reply(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sim_latency(self) -> None:
        # 对数正态：中值 85ms，σ≈0.10 → P95≈98ms / P99≈100ms（实测 100 档形状）
        time.sleep(min(random.lognormvariate(math.log(0.085), 0.10), 5.0))

    # ---- 端点 -----------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802（http.server 命名）
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, json.JSONDecodeError):
            return self._reply(400, {"error": "invalid_json", "message": "请求体须为 JSON 对象"})

        apikey = self.headers.get("apikey", "")
        server: "MockGateway" = self.server.mock  # type: ignore[attr-defined]

        if self.path == "/v1/tags/query":
            if not apikey:
                return self._reply(401, {"error": "unauthorized", "message": "缺少 apikey（网关 key-auth）"})
            if not server.quota_allows(apikey):   # S3 限流演示：超配额即 429
                return self._reply(server.ratelimit_status,
                                   {"error": "rate_limited", "message": f"配额 {server.quota_of(apikey)}/窗口已用尽（mock）"})
            key = str(payload.get("key", ""))
            if not KEY_RE.fullmatch(key):
                return self._reply(400, {"error": "invalid_key", "message": "key 须为 64 位十六进制"})
            self._sim_latency()
            return self._reply(200, {"service_id": "user_tag_query", "key": key, "tenant": "demo_bank",
                                     "found": (int(key[:8], 16) % 3 != 0), "fields": _FIELDS,
                                     "data_as_of": "2026-09-20", "live_version": "mock"})

        if self.path == "/demo/query":
            phone = str(payload.get("phone", "")).strip()
            if not PHONE_RE.fullmatch(phone):
                return self._reply(400, {"error": "invalid_phone", "message": "手机号须为 11 位大陆号段（1[3-9] 开头）"})
            self._sim_latency()
            return self._reply(200, {"channel": "demo",
                                     "note": "演示通道：服务端生成查询键；真实接入传上游供数键调 /v1/tags/query",
                                     "service_id": "user_tag_query", "key": "f" * 64, "tenant": "demo_bank",
                                     "found": (int(phone[-4:]) % 3 != 0), "fields": _FIELDS,
                                     "data_as_of": "2026-09-20", "live_version": "mock"})

        return self._reply(404, {"error": "not_found", "message": f"无路由 {self.path}"})


class _QuietHTTPServer(ThreadingHTTPServer):
    """静默处理压测客户端断连（用户停止时 Session 被 locust 直接关掉，属正常现象）。"""

    def handle_error(self, request, client_address):  # noqa: D102
        import sys

        exc = sys.exception()
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class MockGateway:
    """一次 start/stop 管理 mock 目标生命周期；返回 http://127.0.0.1:<port> 供 locust 打。"""

    def __init__(self, quota_a: int = 10 ** 9, quota_b: int = 300, window_s: float = 1.0,
                 appkey_a: str = "oneserve-dev-appkey-001", appkey_b: str = "oneserve-dev-appkey-002",
                 ratelimit_status: int = 429) -> None:
        self.quotas = {appkey_a: quota_a, appkey_b: quota_b}
        self.tracker = QuotaTracker(window_s)
        self.ratelimit_status = ratelimit_status
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def quota_of(self, apikey: str) -> int:
        return self.quotas.get(apikey, 1)   # 未登记凭证：模拟网关 401 前的保守计数

    def quota_allows(self, apikey: str) -> bool:
        return self.tracker.allow(apikey, self.quota_of(apikey))

    def start(self) -> str:
        self._httpd = _QuietHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.mock = self  # type: ignore[attr-defined]
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                        daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def synthetic_keys(n: int = 2000, seed: int = 20260920) -> list[str]:
    """mock 模式键池：与造数键池同规模、同形态的合成 64 位十六进制键。"""
    rng = random.Random(seed)
    return ["%064x" % rng.getrandbits(256) for _ in range(n)]
