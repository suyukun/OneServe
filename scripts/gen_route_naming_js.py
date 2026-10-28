#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门户前端路由命名常量生成器——「导出一份，禁止两处各写」的落点。

真源＝service/app/route_naming.py（发布端点同源机检）；本脚本把它导出为
admin/route-naming.js（window.ROUTE_NAMING；scripts/build_portal.py 的 admin 同步
会携带进 portal/admin/），供发布台④步地址输入实时校验（红框＋人话提示）复用
同一份正则/词表。生成物入库（门户零构建运行时），tests/test_route_naming.py
断言生成物与真源同步——手改生成物或改真源忘再生成即红。

用法：python3 scripts/gen_route_naming_js.py（仓库根；幂等，内容未变不重写）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "service"))

from app import route_naming as rn   # noqa: E402 —— 真源（单一事实来源）

OUT = ROOT / "admin" / "route-naming.js"   # 源目录；build_portal.py 同步至 portal/admin/

TEMPLATE = """/* ============================================================
 * GENERATED FILE — DO NOT EDIT（改这里会被同步门禁打红）
 * 真源：service/app/route_naming.py（发布端点机检同一份常量）
 * 生成：python3 scripts/gen_route_naming_js.py（幂等）
 * 用途：发布台④步地址实时校验（publish.js WIZ.routeValid）——前后端同一正则，
 *       消「门户允许 _ / 后端拒绝」类双口径（提案 v1 §5 G2）。
 * ============================================================ */
(function (root) {
  'use strict';
  root.ROUTE_NAMING = __PAYLOAD__;
})(typeof window !== 'undefined' ? window : this);
"""


def build_payload() -> str:
    data = {
        "source": "service/app/route_naming.py",
        "routePattern": rn.ROUTE_RE_STR,
        "routePrefix": rn.ROUTE_PREFIX,
        "domainCodes": list(rn.DOMAIN_CODES),
        "reservedSlugs": sorted(rn.RESERVED_SLUGS),
        "verbTokens": sorted(rn.VERB_TOKENS),
        "versionTokenPattern": r"^v\d+$",
        "hint": "域码建议（不强制）：" + "/".join(rn.DOMAIN_CODES) + "-业务名，如 /v1/svc/ret-usr-basic",
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def main() -> int:
    text = TEMPLATE.replace("__PAYLOAD__", build_payload())
    if OUT.is_file() and OUT.read_text(encoding="utf-8") == text:
        print(f"[skip] {OUT.relative_to(ROOT)} 已是最新（幂等，不重写）")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"[ok] 写出 {OUT.relative_to(ROOT)}（改真源后须重跑本脚本）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
