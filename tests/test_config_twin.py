# -*- coding: utf-8 -*-
"""镜像生产化 1b 批·词条⑤ config twin 同步机检（2026-10-21）。

configs/apisix/config.prod.yaml 必须恒等于 scripts/config_twin.py 从 config.yaml
（dev 真源）的渲染输出；语义层除 deployment.admin.allow_admin 的 dev↔prod 网段对外
必须全等。守护方式：改 dev 网关配置后忘了重生成 prod ＝ 本套件红（消除人肉双改漂移）。
"""
import copy
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "config_twin.py"
DEV = REPO / "configs" / "apisix" / "config.yaml"
PROD = REPO / "configs" / "apisix" / "config.prod.yaml"
DEV_CIDR = "172.28.0.0/16"    # oneserve-net（dev 共享栈）
PROD_CIDR = "172.30.0.0/16"   # oneserve-prod-net（生产隔离栈）


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_check_passes():
    """scripts/config_twin.py 缺省 --check 须绿（prod 态＝渲染输出）。"""
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, f"--check 应绿：\n{r.stdout}\n{r.stderr}"


def test_semantic_diff_is_only_allow_admin_cidr_pair():
    """语义层唯一差异＝allow_admin 中 dev↔prod 网段对；其余（含顺序）全等。"""
    dev = _load(DEV)
    prod = _load(PROD)
    dev_acl = dev["deployment"]["admin"]["allow_admin"]
    prod_acl = prod["deployment"]["admin"]["allow_admin"]
    assert DEV_CIDR in dev_acl, f"dev 应含 {DEV_CIDR}：{dev_acl}"
    assert PROD_CIDR in prod_acl, f"prod 应含 {PROD_CIDR}：{prod_acl}"
    assert DEV_CIDR not in prod_acl and PROD_CIDR not in dev_acl
    assert dev_acl.index(DEV_CIDR) == prod_acl.index(PROD_CIDR), "网段位置应同构"

    dev_swapped = copy.deepcopy(dev)
    acl = dev_swapped["deployment"]["admin"]["allow_admin"]
    acl[acl.index(DEV_CIDR)] = PROD_CIDR
    assert dev_swapped == prod, "网段互换后 dev/prod 应语义全等（出现其他漂移）"

    prod_swapped = copy.deepcopy(prod)
    acl = prod_swapped["deployment"]["admin"]["allow_admin"]
    acl[acl.index(PROD_CIDR)] = DEV_CIDR
    assert prod_swapped == dev, "反向互换后 prod/dev 应语义全等（出现其他漂移）"


def test_render_is_pure_and_idempotent():
    """渲染为纯函数：连跑两次输出逐字节一致（--write 幂等的根基）。"""
    text = DEV.read_text(encoding="utf-8")
    mod_dir = str(REPO / "scripts")
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)
    import config_twin
    first = config_twin.render(text)
    second = config_twin.render(text)
    assert first == second
    assert PROD.read_text(encoding="utf-8") == first, "现存 prod 文件应等于渲染输出"
