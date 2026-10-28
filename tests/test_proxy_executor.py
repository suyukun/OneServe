#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 型代理执行器单元测试（task-007）——本地 http.server 全路径覆盖，无 docker 依赖。

三态口径（任务书交付物④②）：
  ① 上游正常 → 透传（状态/体/类型原样）；
  ② 上游停止/超时 → 按 timeout_ms/retry 配置熔断（502/504），且零落盘（不留脏数据）；
  ③ 留存开关两分支：开→2xx 落盘（文件名带时间戳与键）；关→零落盘（目录都不建）。
"""

import asyncio
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import proxy_executor as proxy  # noqa: E402
from app.kv_executor import TagQueryError  # noqa: E402

FIXED = {"code": "0", "source": "mock-partner", "data": {"record": {"id": "PT-1"}}}
FIXED_BYTES = json.dumps(FIXED, ensure_ascii=False).encode("utf-8")

PARTNER = {
    "service_id": "partner_proxy",
    "backend": {"proxy": {"upstream_url": "", "timeout_ms": 1000, "retry": 0}},
    "retention": {"enabled": True, "dir": ""},
}


# ---------- 本地上游桩（真 socket，覆盖 fetch_upstream 真路径） ----------

class _Handler(BaseHTTPRequestHandler):
    behavior = "ok"            # ok | hang | flaky
    attempts = 0

    def do_GET(self):
        type(self).attempts += 1
        if type(self).behavior == "hang":
            time.sleep(5)                               # 超过测试口径 timeout_ms
            return
        if type(self).behavior == "flaky" and type(self).attempts == 1:
            self.close_connection = True                # 第一次：不回包直接断（传输层失败）
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(FIXED_BYTES)))
        self.end_headers()
        self.wfile.write(FIXED_BYTES)

    def log_message(self, fmt, *args):
        pass


class UpstreamStub:
    """起/停本地 http.server；可测「正常/挂起/断连/彻底停止」四种上游形态。"""

    def __init__(self, behavior):
        _Handler.behavior = behavior
        _Handler.attempts = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def _svc(tmp_dir, *, retry=0, timeout_ms=1000, retention=True):
    svc = json.loads(json.dumps(PARTNER))                  # 深拷贝防串台
    svc["backend"]["proxy"].update(upstream_url=_svc.base, timeout_ms=timeout_ms, retry=retry)
    svc["retention"] = {"enabled": retention, "dir": str(tmp_dir)}
    return svc


def _tmp_path(tmp_name):
    import tempfile
    d = Path(tempfile.mkdtemp(prefix=tmp_name))
    return d


def _files(d: Path):
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_passthrough_and_retention_on():
    """态①＋态③开：透传原样（状态/体/类型）；留存文件名带时间戳与键，内容＝响应体。"""
    stub = UpstreamStub("ok")
    _svc.base = stub.base
    try:
        d = _tmp_path("pt-on")
        svc = _svc(d)
        out = asyncio.run(proxy.execute(proxy.fetch_upstream, svc, "info", "key=PT-1001", "PT-1001"))
        assert out["status"] == 200
        assert json.loads(out["body_text"]) == FIXED
        assert "application/json" in out["content_type"]
        assert out["retention"] is True and out["retained_path"]
        fs = _files(d)
        assert len(fs) == 1, f"应恰落 1 个文件：{fs}"
        name = fs[0]
        assert "PT-1001" in name and "T" in name and name.endswith(".json"), \
            f"文件名应含键与时间戳：{name}"
        assert (d / name).read_bytes() == FIXED_BYTES, "落盘内容应＝上游响应体"
    finally:
        stub.stop()


def test_retention_off_zero_disk():
    """态③关：响应照常透传，但零落盘——连留存目录都不建（严禁写任何响应内容）。"""
    stub = UpstreamStub("ok")
    _svc.base = stub.base
    try:
        d = _tmp_path("pt-off")
        target = d / "retention"                          # 配置指向的目录尚不存在
        svc = _svc(target, retention=False)
        out = asyncio.run(proxy.execute(proxy.fetch_upstream, svc, "info", "key=PT-2", "PT-2"))
        assert out["status"] == 200 and json.loads(out["body_text"]) == FIXED
        assert out["retention"] is False and out["retained_path"] is None
        assert not target.exists(), "留存关闭时目录不应被创建（零落盘强口径）"
        assert list(d.iterdir()) == [], "父目录下不应出现任何新文件/目录"
    finally:
        stub.stop()


def test_upstream_timeout_circuit():
    """态②超时：上游挂起→按 timeout_ms 熔断 504（retry=0 只试一次）；零落盘。"""
    stub = UpstreamStub("hang")
    _svc.base = stub.base
    try:
        d = _tmp_path("pt-hang")
        svc = _svc(d, timeout_ms=150, retry=0)
        t0 = time.monotonic()
        try:
            asyncio.run(proxy.execute(proxy.fetch_upstream, svc, "info", "", "K"))
        except TagQueryError as exc:
            assert exc.status == 504 and exc.code == "upstream_timeout"
        else:
            raise AssertionError("上游挂起应 504 upstream_timeout")
        assert time.monotonic() - t0 < 3, "超时熔断未按 timeout_ms 生效（拖过 3s）"
        assert _files(d) == [], "失败调用零落盘（不留脏数据）"
    finally:
        stub.stop()


def test_upstream_unreachable_and_no_dirty():
    """态②停机：上游进程停止→502 upstream_unreachable；留存开着也零落盘。"""
    stub = UpstreamStub("ok")
    dead_port = stub.port
    stub.stop()                                          # 先拿端口再停：必然连接拒绝
    _svc.base = f"http://127.0.0.1:{dead_port}"
    d = _tmp_path("pt-dead")
    svc = _svc(d, retry=0)
    try:
        asyncio.run(proxy.execute(proxy.fetch_upstream, svc, "info", "", "K"))
    except TagQueryError as exc:
        assert exc.status == 502 and exc.code == "upstream_unreachable"
    else:
        raise AssertionError("上游停止应 502 upstream_unreachable")
    assert _files(d) == [], "失败调用零落盘（不留脏数据）"


def test_retry_config():
    """retry 可配：第一次断连、第二次成功——retry=1 整体成功且上游被调 2 次。"""
    stub = UpstreamStub("flaky")
    _svc.base = stub.base
    try:
        d = _tmp_path("pt-retry")
        svc = _svc(d, retry=1, timeout_ms=500)
        out = asyncio.run(proxy.execute(proxy.fetch_upstream, svc, "info", "", "K"))
        assert out["status"] == 200 and _Handler.attempts == 2
        assert len(_files(d)) == 1                       # 重试成功＝正常业务响应，照常留存
        # retry=0 时同样故障应直接熔断
        _Handler.attempts = 0
        _Handler.behavior = "flaky"
        svc0 = _svc(d, retry=0, timeout_ms=500)
        try:
            asyncio.run(proxy.execute(proxy.fetch_upstream, svc0, "info", "", "K"))
        except TagQueryError as exc:
            assert exc.status == 502
        else:
            raise AssertionError("retry=0 遇断连应 502")
    finally:
        stub.stop()


def test_url_guard_and_annotate():
    """rest 黑名单（防改写转发目标）与审计标注开关后缀。"""
    stub = UpstreamStub("ok")
    _svc.base = stub.base
    try:
        svc = _svc(_tmp_path("pt-guard"))
        for bad in ["../etc", "a@b", "abs://x", "a//b"]:
            try:
                proxy.build_upstream_url(svc, bad, "")
            except TagQueryError as exc:
                assert exc.status == 400
            else:
                raise AssertionError(f"恶意路径 {bad!r} 应 400")
        url = proxy.build_upstream_url(svc, "info", "key=PT-3")
        assert url == f"{stub.base}/info?key=PT-3"
    finally:
        stub.stop()
    assert proxy.annotate_endpoint("service:/v1/proxy/partner", True) == \
        "service:/v1/proxy/partner?retention=on"
    assert proxy.annotate_endpoint("service:/v1/proxy/partner", False) == \
        "service:/v1/proxy/partner?retention=off"
    assert proxy.retention_enabled({"retention": {"enabled": False}}) is False
    assert proxy.retention_enabled({}) is False          # 缺省＝关（留口不默认留存）


def test_bad_config_and_stale_key_filename():
    """配置缺失 fail-closed；留存键做文件名清洗（防路径穿越）。"""
    try:
        proxy.proxy_conf({"backend": {}})
    except TagQueryError as exc:
        assert exc.status == 500 and exc.code == "bad_proxy_config"
    else:
        raise AssertionError("缺 proxy 配置应 500 fail-closed")
    d = _tmp_path("pt-safe")
    svc = {"retention": {"enabled": True, "dir": str(d)}}
    import datetime as dt
    now = dt.datetime(2026, 9, 21, 8, 30, 0, 123000, tzinfo=dt.timezone.utc)
    path = proxy.retain_body(svc, "../../etc@ passwd", b"{}", now=now)
    assert path and Path(path).parent == d
    assert "/" not in Path(path).name.replace(".json", "").split("_", 1)[1], \
        f"文件名键段应已清洗：{path}"
    assert Path(path).read_bytes() == b"{}"


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== task-007 B 型代理执行器单元测试（{len(tests)} 条）==")
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
