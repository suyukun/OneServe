#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""configs/apisix 双态配置同源同步器（镜像生产化 1b 批·词条⑤ config twin 同步）。

背景：configs/apisix/config.yaml（dev 共享栈在用）与 config.prod.yaml（compose.prod.yml
生产隔离栈在用）是「唯一语义差异＝Admin ACL 的 dev 网段」的 twin 文件——此前靠人肉双改，
漂移无守护（1a 批 d8e08e0 登记遗留，本批收口）。

机制（渲染式同源，prod 态唯一生成源＝本脚本，禁止手改 config.prod.yaml）：
    python3 scripts/config_twin.py            # 缺省 --check：prod 态与渲染输出不一致＝exit 2
    python3 scripts/config_twin.py --write    # 改完 dev 网关配置后重跑，重生成 prod 态

渲染规则（仅三处，其余与 dev 逐字节同源）：
    ① 头部注释块（开头连续 # 行）替换为 prod 专用头（twin 约定＋「真部署换密＋secret
       注入」备忘，原 prod 手写头的全部信息并入模板）；
    ② 全文替换 dev 网段 172.28.0.0/16 → 172.30.0.0/16（allow_admin 条目行与注释行内同规则）；
    ③ 注释「oneserve-net 网段」→「oneserve-prod-net 网段」。

自校验（防文本替换规则失效时静默错渲染）：yaml 语义层比对两文件——除
deployment.admin.allow_admin 中 dev 网段↔prod 网段一对值外必须全等，否则无论
--check/--write 一律 exit 2 拒绝出结果。

机检：tests/test_config_twin.py（--check exit 0 ＋ 语义唯一差异断言，挂 run_gates）。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEV = REPO / "configs" / "apisix" / "config.yaml"
PROD = REPO / "configs" / "apisix" / "config.prod.yaml"

DEV_CIDR = "172.28.0.0/16"        # oneserve-net（dev 共享栈）
PROD_CIDR = "172.30.0.0/16"       # oneserve-prod-net（生产隔离栈，与 m26 172.29 错开）
ALLOW_ADMIN_PATH = ("deployment", "admin", "allow_admin")

PROD_HEADER = """\
# OneServe 网关配置·生产隔离栈（compose.prod.yml 专用；本文件＝渲染产物勿手改）
# 由 scripts/config_twin.py 从 configs/apisix/config.yaml（dev 真源）渲染生成——
# 改 dev 网关配置后重跑 `python3 scripts/config_twin.py --write` 再生本文件
# （镜像生产化 1b 批·词条⑤ config twin 同步；--check 挂 tests/test_config_twin.py）。
# 与 dev 的唯一语义差异＝Admin ACL 网段 172.28.0.0/16→172.30.0.0/16（prod 专属网
# oneserve-prod-net，与 dev 172.28、m26 验收 172.29 错开）；其余完全一致。
# 真部署换密＋APISIX_ADMIN_KEY 走 secret 注入（1b 批）。
"""


def render(dev_text: str) -> str:
    """从 dev 文本渲染 prod 文本（纯函数，规则见模块 docstring）。"""
    lines = dev_text.splitlines()
    i = 0
    while i < len(lines) and lines[i].lstrip().startswith("#"):
        i += 1
    body = "\n".join(lines[i:])
    body = body.replace(DEV_CIDR, PROD_CIDR)
    body = body.replace("# oneserve-net 网段", "# oneserve-prod-net 网段")
    return PROD_HEADER + body + "\n"


def _semantic_diff_ok() -> tuple[bool, str]:
    """yaml 语义层校验：唯一差异＝allow_admin 中 dev↔prod 网段一对值。"""
    try:
        import yaml
    except ImportError:
        return False, "PyYAML 不可用，无法做语义自校验（拒绝盲渲染）"
    dev = yaml.safe_load(DEV.read_text(encoding="utf-8"))
    prod = yaml.safe_load(PROD.read_text(encoding="utf-8"))

    diffs: list[str] = []

    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                if k not in a or k not in b:
                    diffs.append(f"{path}.{k}: 键单侧缺失")
                else:
                    walk(a[k], b[k], f"{path}.{k}")
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                diffs.append(f"{path}: 列表长度 {len(a)}≠{len(b)}")
            else:
                for idx, (x, y) in enumerate(zip(a, b)):
                    walk(x, y, f"{path}[{idx}]")
        elif a != b:
            diffs.append(f"{path}: {a!r}≠{b!r}")

    walk(dev, prod, "$")
    meaningful = [d for d in diffs if not d.startswith("$.deployment.admin.allow_admin[")]
    if meaningful:
        return False, "allow_admin 之外出现语义差异：" + "；".join(meaningful)
    acl_diffs = [d for d in diffs if d.startswith("$.deployment.admin.allow_admin[")]
    if len(acl_diffs) != 1:
        return False, f"allow_admin 差异应恰 1 处（dev↔prod 网段），实得 {len(acl_diffs)}：{acl_diffs}"
    dev_acl = _find_list(dev, ALLOW_ADMIN_PATH)
    prod_acl = _find_list(prod, ALLOW_ADMIN_PATH)
    if DEV_CIDR not in dev_acl or PROD_CIDR not in prod_acl:
        return False, f"网段值口径不符：dev 应含 {DEV_CIDR}、prod 应含 {PROD_CIDR}（实得 dev={dev_acl} prod={prod_acl}）"
    if dev_acl.index(DEV_CIDR) != prod_acl.index(PROD_CIDR):
        return False, "dev/prod 网段位置不一致（列表序应同构）"
    return True, "唯一语义差异＝allow_admin 网段对"


def _find_list(node, path):
    for key in path:
        node = node[key]
    return node


def main(argv: list[str]) -> int:
    write = "--write" in argv
    if set(argv) - {"--write"}:
        print(f"[config-twin] 未知参数：{argv}（支持 --write；缺省＝--check）", file=sys.stderr)
        return 2
    if not DEV.exists():
        print(f"[config-twin] dev 真源不存在：{DEV}", file=sys.stderr)
        return 2
    rendered = render(DEV.read_text(encoding="utf-8"))
    if write:
        PROD.write_text(rendered, encoding="utf-8")
        print(f"[config-twin] 已重生成 {PROD.relative_to(REPO)}")
    else:
        current = PROD.read_text(encoding="utf-8") if PROD.exists() else ""
        if current != rendered:
            print("[config-twin] ✗ config.prod.yaml 与渲染输出不一致（手改漂移或 dev 已改未同步）", file=sys.stderr)
            print("[config-twin]   下一步：python3 scripts/config_twin.py --write 重生成后 diff 审一眼", file=sys.stderr)
            return 2
        print("[config-twin] ✓ config.prod.yaml 与渲染输出一致")
    ok, msg = _semantic_diff_ok()
    if not ok:
        print(f"[config-twin] ✗ 语义自校验失败：{msg}", file=sys.stderr)
        return 2
    print(f"[config-twin] ✓ 语义自校验：{msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
