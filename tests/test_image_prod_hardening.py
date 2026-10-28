# -*- coding: utf-8 -*-
"""镜像生产化 1b 批静态机检（词条①③④②；2026-10-21）。

1a 批 d8e08e0「服务镜像生产化」登记遗留五项中可静态机检的四项在此固化：
  ① 第三方基镜像配置收编完整性——两个主 compose 的全部 bind 挂载源均为仓内相对
     路径且在仓内存在（不留仓外手工步骤，重建即可复现）＋第三方镜像自定义配置
     文件清单齐备（apisix/casdoor/nginx）；
  ② portal 非 root——portal/Dockerfile.prod 用 nginx-unprivileged 基镜像（uid 101）
     ＋高位监听口 8080/8443（免特权免 setcap），dev/prod/m26 三处端口映射同步；
  ③ OIDC 域名注入完整性——issuer/redirect 全链由 PROD_PUBLIC_ORIGIN 派生、
     compose 与代码/测试缺省同值（dev 缺省 http://localhost:8000 可用，19000 旧
     口径清零）；
  ④ sync 工具链载体——prod 栈 service 挂载宿主 ./sync（物料包 update.sh 现状
     ＝git 工作副本随包走，不进镜像）。
（词条⑤ config twin 同步单独机检：tests/test_config_twin.py）
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEV_COMPOSE = REPO / "docker-compose.yml"
PROD_COMPOSE = REPO / "compose.prod.yml"
M26_COMPOSE = REPO / "compose.m26-iso.yml"
PORTAL_DOCKERFILE = REPO / "portal" / "Dockerfile.prod"
NGINX_CONF = REPO / "configs" / "nginx" / "portal-nginx.conf"
AUTH_OIDC = REPO / "service" / "app" / "auth_oidc.py"
AUTH_OIDC_TEST = REPO / "tests" / "test_auth_oidc.py"

BIND_SOURCE_RE = re.compile(r"^\s*-\s+(\./[^:\s]+):", re.MULTILINE)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── ① 第三方基镜像配置收编 ──────────────────────────────────────────────


def test_bind_mount_sources_are_in_repo():
    """两主 compose 全部 bind 挂载源＝仓内相对路径且存在（不留仓外手工步骤）。"""
    missing = []
    for compose in (DEV_COMPOSE, PROD_COMPOSE):
        for src in BIND_SOURCE_RE.findall(_text(compose)):
            if not src.startswith("./"):
                missing.append(f"{compose.name}: 非仓内相对路径挂载源 {src}")
                continue
            target = REPO / src[2:]
            if not target.exists():
                missing.append(f"{compose.name}: 挂载源 {src} 不在仓内")
    assert not missing, "；".join(missing)


def test_cert_gen_script_present_for_certs_dev():
    assert (REPO / "scripts" / "gen-dev-cert.sh").exists(), "certs/dev 生成脚本须随仓（重建即可复现）"


def test_thirdparty_custom_configs_present():
    """第三方镜像（apisix/casdoor/portal nginx）自定义配置文件清单齐备。"""
    required = [
        "configs/apisix/config.yaml",
        "configs/apisix/config.prod.yaml",
        "configs/casdoor/app.conf.template",
        "configs/casdoor/render_app_conf.sh",
        "configs/casdoor/bootstrap_admin.py",
        "configs/nginx/portal-nginx.conf",
    ]
    missing = [p for p in required if not (REPO / p).exists()]
    assert not missing, f"第三方配置收编缺件：{missing}"


# ── ② portal 非 root ────────────────────────────────────────────────────


def test_portal_prod_image_is_unprivileged():
    dockerfile = _text(PORTAL_DOCKERFILE)
    assert "nginx-unprivileged" in dockerfile, "portal 生产镜像须用 nginx-unprivileged 基镜像（uid 101 非 root）"
    assert not re.search(r"^USER\s+root", dockerfile, re.MULTILINE), "不得回退 USER root"
    assert "EXPOSE 8080" in dockerfile, "镜像声明监听口须为 8080（高位免特权）"


def test_portal_nginx_conf_listens_high_ports():
    conf = _text(NGINX_CONF)
    assert re.search(r"listen\s+8080;", conf), "HTTP 监听须 8080"
    assert re.search(r"listen\s+8443\s+ssl;", conf), "HTTPS 监听须 8443"
    assert not re.search(r"listen\s+80;", conf), "残留 80 监听（非 root 跑不了，须清理）"
    assert not re.search(r"listen\s+443\s+ssl;", conf), "残留 443 监听（须清理）"


def test_portal_port_maps_synced_all_stacks():
    dev = _text(DEV_COMPOSE)
    prod = _text(PROD_COMPOSE)
    m26 = _text(M26_COMPOSE)
    assert '"8000:8080"' in dev, "dev portal 宿主映射须 8000:8080"
    assert "127.0.0.1:80/index.html" not in dev and "127.0.0.1:8080/index.html" in dev, "dev portal healthcheck 须随 8080"
    assert ':20000:8080"' in prod, "prod portal 宿主映射须 20000:8080"
    assert ':20443:8443"' in prod, "prod portal TLS 预留映射须 20443:8443"
    assert "18000:8080" in m26, "m26 隔离栈 portal 映射须随 8080"


# ── ③ OIDC 域名注入 ─────────────────────────────────────────────────────


def test_oidc_issuer_redirect_derived_from_public_origin():
    dev = _text(DEV_COMPOSE)
    prod = _text(PROD_COMPOSE)
    # dev 基座定义派生链（compose.prod.yml 为 override 形态，service environment 继承
    # dev 基座——prod 文本断言「未覆盖 OIDC 键」即继承同一条派生链）。
    assert "OIDC_ISSUER=${PROD_PUBLIC_ORIGIN" in dev, "dev 基座 OIDC_ISSUER 须由 PROD_PUBLIC_ORIGIN 派生"
    assert "OIDC_REDIRECT_URI=${PROD_PUBLIC_ORIGIN" in dev, "dev 基座 OIDC_REDIRECT_URI 须由 PROD_PUBLIC_ORIGIN 派生"
    assert "OIDC_ISSUER:" not in prod and "OIDC_REDIRECT_URI:" not in prod, "prod override 不得另设 OIDC 域名键（须继承基座派生链）"
    # casdoor（origin/staticBaseUrl 渲染源）与 casdoor-init（redirectUris 派生源）注入
    assert dev.count("- PROD_PUBLIC_ORIGIN=") >= 2, "casdoor 与 casdoor-init 须注入 PROD_PUBLIC_ORIGIN"


def test_oidc_code_default_aligned_no_19000():
    code = _text(AUTH_OIDC)
    assert 'os.environ.get("OIDC_ISSUER", "http://localhost:8000")' in code, "代码缺省须与 compose 缺省同值（dev 门户入口 8000）"
    assert "localhost:19000" not in code, "19000 旧口径残留（ADR-0018 同源部署后已封死）"
    test_code = _text(AUTH_OIDC_TEST)
    assert '"iss": "http://localhost:8000"' in test_code, "测试签发 iss 须与代码缺省同源"
    # 全 tests/ 目录（.py）扫 19000 旧口径的两类耦合形态（token 签发 iss 字面量＋
    # OIDC_ISSUER 缺省读取）——正向「不含 19000」断言不受影响，凡自造 token 的
    # 测试文件一律同源对齐。
    stale = []
    me = Path(__file__).name
    for p in sorted((REPO / "tests").glob("*.py")):
        if p.name == me:
            continue   # 本文件的断言字面量自身含该形态，排除自引用
        text = p.read_text(encoding="utf-8")
        if '"iss": "http://localhost:19000"' in text or 'os.environ.get("OIDC_ISSUER", "http://localhost:19000")' in text:
            stale.append(str(p.relative_to(REPO)))
    assert not stale, f"测试侧 19000 旧口径残留（须与 8000 缺省同源）：{stale}"


# ── ④ sync 工具链载体 ───────────────────────────────────────────────────


def test_prod_service_mounts_host_sync():
    prod = _text(PROD_COMPOSE)
    assert "- ./sync:/sync" in prod, "prod 栈 service 须宿主挂载 ./sync（载体＝工作副本随包走，不进镜像）"
    assert (REPO / "sync").is_dir(), "sync 工具链目录须在仓内"
    assert (REPO / "scripts" / "update.sh").exists(), "update.sh（测试环境一键更新）须在仓内 scripts/"
