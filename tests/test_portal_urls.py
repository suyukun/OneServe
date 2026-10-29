#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-10 补丁：门户干净 URL 回归套件（tests/test_portal_urls.py）。

缺陷背景：演示栈根 location 原为 try_files $uri $uri/ =404; 无 .html 回退，
导致 /trial /docs /my /fields /apply /contact /audit /ops /catalog 九个无后缀
干净 URL 直接 404（/trial.html 才 200）。修复：根 location 补 $uri.html，
与 /admin/ 段同款，职责注释同步为「全站干净 URL」。
（注：本段为历史缺陷背景；卡5 字段字典页下线后 fields 不再是现役页，清单见 CLEAN_URLS。）

守卫口径（两层）：
  1. 配置级（无 docker 也必跑）：
     - 根 location 含 .html 回退，且回退顺序仍 $uri → $uri.html → $uri/
       （静态资源/真实目录请求不被误伤）；
     - /admin/ 干净 URL 回退保持；
     - 作用域覆盖：/v1/portal/、/v1/admin/、/v1/、/demo 四个反代段与
       resolver 行原样保留（防回归）；
     - 顶部职责注释已同步「全站干净 URL」。
  2. 真链路级（docker CLI＋nginx 镜像可用才跑，否则 skip）：
     以【隔离容器】挂 portal/ 与改后的 conf，临时端口起一个独立 nginx
     （专属容器名 oneserve-urlf-nginx-*、不建 named volume、绝不 down -v 共享卷），
     断言 9 个裸路径全部 200 且与同名 .html 内容一致；静态资源 .css/.js 200；
     未知路径仍 404；finally 无条件 rm -f 自身容器。

运行：python3 -m pytest tests/test_portal_urls.py（或 python3 直跑，断言一致）。
真链路用例可用 ONESERVE_PORTAL_URLF=0 显式跳过（默认自动探测）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NGINX_CONF = REPO / "configs" / "nginx" / "portal-nginx.conf"
PORTAL = REPO / "portal"

# 演示栈实测 404 的裸路径（修前 404 / 修后 200）；P1-7 合并后 contact 下线（301 → /docs），
# 故不再列入 200 清单，改由独立用例断言其 301 行为；卡5 字段字典页下线，fields 同步移出；
# ux-fix-batch 2026-10-29：apply（申请接入）页下线，随 fields 移出（无 301——内部入口，
# nginx try_files 自然 404 兜底）。
CLEAN_URLS = ["trial", "docs", "my", "audit", "ops", "catalog"]

# P1-7 合并：下线的 contact 独立页 → 301 到帮助与支持页 /docs
RETIRED_REDIRECTS = {"/contact": "/docs", "/contact.html": "/docs"}

# Docker CLI 不在 PATH（本机事实）：先探全路径再退 PATH
DOCKER_CANDIDATES = [
    "/Applications/Docker.app/Contents/Resources/bin/docker",
    "docker",
]
NGINX_IMAGE = "nginxinc/nginx-unprivileged:1.27-alpine"   # 1b 批·词条②：与 dev/prod portal 同款非 root 基镜像（uid 101，监听 8080）


def _read_conf() -> str:
    return NGINX_CONF.read_text(encoding="utf-8")


def _location_body(conf: str, name: str) -> str:
    """纯文本截取 location 段正文（精确段名，不误吞同前缀段，避开正则转义坑）。"""
    lines = conf.splitlines()
    head = "location " + name + " {"
    start = None
    for i, line in enumerate(lines):
        if line.strip() == head:
            start = i + 1
            break
    assert start is not None, "未找到 location 段：" + name
    end = start
    while end < len(lines) and lines[end].strip() != "}":
        end += 1
    return chr(10).join(lines[start:end])


# ── 1. 配置级断言（无 docker 也必跑） ──

def test_root_location_html_fallback_order():
    body = _location_body(_read_conf(), "/")
    assert "try_files" in body, "根 location 缺 try_files"
    assert "$uri.html" in body, "根 location 缺 .html 回退（/trial 会 404）"
    # 回退顺序保持 $uri → $uri.html → $uri/：静态资源直出、目录请求不被 .html 抢先
    assert "try_files $uri $uri.html $uri/ =404;" in body, "回退顺序异常：" + body


def test_admin_clean_url_kept():
    body = _location_body(_read_conf(), "/admin/")
    assert "$uri.html" in body, "/admin/ 干净 URL 回退被破坏"


def test_proxy_locations_and_resolver_intact():
    conf = _read_conf()
    for loc in ["/v1/portal/", "/v1/admin/", "/v1/", "/demo"]:
        assert ("location " + loc + " {") in conf, "反代段缺失：" + loc
    assert "resolver 127.0.0.11" in conf, "resolver 行缺失"


def test_comment_mentions_sitewide_clean_url():
    conf = _read_conf()
    head = conf[: conf.index("server {")]
    assert "全站" in head and "干净 URL" in head, "顶部职责注释未同步全站干净 URL"


# ── 2. 真链路 E2E（隔离容器；docker 不可用则 skip） ──

def _which(cmd: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, cmd)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _docker() -> str | None:
    for cand in DOCKER_CANDIDATES:
        exe = cand if os.path.sep in cand else _which(cand)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            return exe
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """P1-7：301 断言需要看原状态码与 Location，禁止 urllib 自动跟随（否则会跟到丢端口的 Location）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _http_status_code(url: str) -> tuple[int, bytes]:
    try:
        with _NO_REDIRECT_OPENER.open(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        # 301 无 body：把 Location 头一并带出供断言（其余状态码不影响既有断言）
        loc = "\n".join(v for k, v in exc.headers.items() if k.lower() == "location")
        return exc.code, exc.read() + loc.encode()


def test_live_clean_urls_all_200():
    if os.environ.get("ONESERVE_PORTAL_URLF") == "0":
        print("  skip：ONESERVE_PORTAL_URLF=0 显式跳过真链路")
        return
    docker = _docker()
    if not docker:
        print("  skip：docker CLI/daemon 不可用，仅跑配置级断言")
        return
    # 镜像不在本地则跳过（门禁内不触发网络拉取，避免抖动）
    probe = subprocess.run([docker, "image", "inspect", NGINX_IMAGE],
                           capture_output=True, text=True, timeout=20)
    if probe.returncode != 0:
        print("  skip：本地无镜像 " + NGINX_IMAGE + "，仅跑配置级断言")
        return

    name = "oneserve-urlf-nginx-" + uuid.uuid4().hex[:8]
    port = _free_port()
    # TLS 预留（蓝图 1a 2026-10-20）：portal-nginx.conf 的 443 块引用 /etc/nginx/certs，
    # 无证书＝nginx 起不来。缺则先跑 gen-dev-cert.sh（幂等自签，certs/ 不入 git），
    # 与 up.sh 起栈前置同一口径。
    certs_dir = REPO / "certs" / "dev"
    if not (certs_dir / "fullchain.pem").is_file():
        gen = subprocess.run(["bash", str(REPO / "scripts" / "gen-dev-cert.sh")],
                             capture_output=True, text=True, timeout=60)
        assert gen.returncode == 0, "自签开发证书生成失败：" + gen.stderr.strip()
    run = [
        docker, "run", "-d", "--rm", "--name", name,
        "-p", "127.0.0.1:%d:8080" % port,   # 容器内口随词条②非 root 化＝8080（conf listen 8080）
        "-v", str(PORTAL) + ":/usr/share/nginx/html:ro",
        "-v", str(NGINX_CONF) + ":/etc/nginx/conf.d/default.conf:ro",
        "-v", str(certs_dir) + ":/etc/nginx/certs:ro",
        NGINX_IMAGE,
    ]
    try:
        r = subprocess.run(run, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, "隔离 nginx 启动失败：" + r.stderr.strip()
        base = "http://127.0.0.1:%d" % port
        # 就绪轮询：首页 200 即视为 conf 语法通过、站点起来
        ready = False
        for _ in range(40):
            try:
                if _http_status_code(base + "/")[0] == 200:
                    ready = True
                    break
            except OSError:
                pass
            time.sleep(0.25)
        assert ready, "隔离 nginx 60s 内未就绪（conf 语法或端口问题）"

        # 9 个裸路径全部 200，且与同名 .html 内容一致
        for slug in CLEAN_URLS:
            code, body = _http_status_code(base + "/" + slug)
            assert code == 200, "/%s 期望 200 实得 %s" % (slug, code)
            ref = (PORTAL / (slug + ".html")).read_bytes()
            assert body == ref, "/%s 未回退到 %s.html（内容不一致）" % (slug, slug)

        # P1-7 合并：下线的 contact 两个入口 301 到 /docs（旧书签不 404）
        for retired, target in RETIRED_REDIRECTS.items():
            code, body = _http_status_code(base + retired)
            assert code == 301, "%s 期望 301 实得 %s" % (retired, code)
            assert target.encode() in body, "%s 的 301 Location 未指向 %s" % (retired, target)

        # 既有行为不回归：静态资源直出 200、未知路径仍 404、admin 干净 URL 200
        for asset in ["/assets/app.js", "/index.html", "/admin/publish"]:
            code, _ = _http_status_code(base + asset)
            assert code == 200, "%s 期望 200 实得 %s" % (asset, code)
        code, _ = _http_status_code(base + "/no-such-page-xyz")
        assert code == 404, "未知路径期望 404 实得 %s（.html 回退误伤）" % code
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, text=True, timeout=30)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("  ok " + fn.__name__)
        except Exception as exc:   # noqa: BLE001
            failed += 1
            print("  FAIL " + fn.__name__ + ": " + str(exc))
    print(str(len(fns) - failed) + "/" + str(len(fns)) + " passed")
    sys.exit(1 if failed else 0)
