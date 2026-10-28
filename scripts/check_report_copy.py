#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报表专区术语净化机检（1b-9 · 设计稿 docs/design/报表专区一期设计_v1.md §6/§7-4）。

扫描面＝报表专区四页：真源 portal/src/reports/*.html＋构建产物 portal/reports/*.html。
剥离 script/style／aside 侧栏（产物含全站导航，其文案不属专区本卡文案）／HTML 注释／
details 折叠区（口径原文与技术详情的豁免位）／code、pre 技术标识后，对 §6 禁用
工程术语正则必须零命中——用户可见主文案零工程术语由本机检固化。

禁用词表（§6 对照表「禁用工程术语」列，机检形态）：
  东八区／自然日／日切；调用主体；caller；kid；AppKey；endpoint；接口路由；
  2xx；latency_ms；avgMs；网关即拒；key-auth。
拉丁词按不区分大小写匹配（kid 加词边界防误伤 kind 等普通词）。

退出码：零命中＝0；任一命中＝1（并列出文件、行号与命中词）。
运行：python3 scripts/check_report_copy.py（已挂 run_gates，doc_ref_check 之后）。
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCAN_GLOBS = ("portal/src/reports/*.html", "portal/reports/*.html")

# 提示层属性（tooltip/占位符/无障碍名即用户可见文案，同 test_terminology 口径）
VISIBLE_ATTRS = ("title", "placeholder", "aria-label", "aria-description", "alt")

# §6 禁用工程术语（正则形态；拉丁词不区分大小写）
FORBIDDEN_RES = (
    re.compile(r"东八区|自然日|日切"),
    re.compile(r"调用主体"),
    re.compile(r"caller", re.IGNORECASE),
    re.compile(r"\bkid\b", re.IGNORECASE),
    re.compile(r"appkey", re.IGNORECASE),
    re.compile(r"endpoint", re.IGNORECASE),
    re.compile(r"接口路由"),
    re.compile(r"2xx", re.IGNORECASE),
    re.compile(r"latency_ms", re.IGNORECASE),
    re.compile(r"avgms", re.IGNORECASE),
    re.compile(r"网关即拒"),
    re.compile(r"key-auth", re.IGNORECASE),
)


class _VisibleText(HTMLParser):
    """抽取用户可见文案：文本节点＋提示层属性（跳过 aside/script/style/details/code/pre）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[tuple[int, str]] = []   # (近似行号, 文本)
        self._skip_tags: list[str] = []
        self._line = 1

    def _skipping(self) -> bool:
        return bool(self._skip_tags)

    def handle_starttag(self, tag, attrs):
        self._line = self.getpos()[0]
        if tag in ("aside", "script", "style", "details", "code", "pre"):
            self._skip_tags.append(tag)
            return
        if self._skipping():
            return
        for k, v in attrs:
            if k in VISIBLE_ATTRS and v:
                self.chunks.append((self._line, v))

    def handle_startendtag(self, tag, attrs):
        self._line = self.getpos()[0]
        if self._skipping():
            return
        for k, v in attrs:
            if k in VISIBLE_ATTRS and v:
                self.chunks.append((self._line, v))

    def handle_endtag(self, tag):
        self._line = self.getpos()[0]
        if self._skip_tags and self._skip_tags[-1] == tag:
            self._skip_tags.pop()

    def handle_data(self, data):
        self._line = self.getpos()[0]
        if self._skipping():
            return
        if data.strip():
            self.chunks.append((self._line, data))


def visible_hits(path: Path) -> list[str]:
    """单文件可见文案的禁用词命中清单（human-readable 行）。"""
    parser = _VisibleText()
    parser.feed(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for line_no, text in parser.chunks:
        for pat in FORBIDDEN_RES:
            m = pat.search(text)
            if m:
                hits.append(f"{path}:{line_no}: 命中「{m.group(0)}」in {text.strip()[:60]!r}")
    return hits


def main() -> int:
    paths = sorted(p for glob in SCAN_GLOBS for p in REPO.glob(glob))
    if not paths:
        print(f"[FAIL] 扫描面为空：{SCAN_GLOBS} 无文件——报表专区四页未构建？")
        return 1
    hits: list[str] = []
    for p in paths:
        hits.extend(visible_hits(p))
    if hits:
        print(f"[FAIL] 报表专区可见文案命中禁用工程术语 {len(hits)} 处（设计稿 §6 净化表）：")
        for h in hits:
            print("  " + h)
        return 1
    print(f"[ok] 报表专区术语净化：{len(paths)} 页可见文案禁用词零命中（§6 机检）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
