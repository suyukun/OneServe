#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc_ref_check.py —— 文档交叉引用存在性机检（今晚复盘改进第一条，挂 run_gates）。

只检「被引用的东西是否真实存在」，四类引用（只检不改，无 --fix）：
  1. ADR 引用   ADR-00XX（含 ADR-0021 v1.1 版本后缀、ADR-0015/0016 斜杠连写）
                -> docs/adr/00XX-*.md 文件存在；
  2. 章节引用   「SPEC §N / SPEC§N / docs/plans/SPEC-统一数据服务平台_v1.md §N」
                -> SPEC v1 实有该级章节号；「蓝图 §N / 架构演进蓝图_v1.md §N」同理。
                标题「### 6.4~6.9」这类区间标题按区间展开计（6.5..6.9 均算存在）；
  3. 文件路径   docs/ scripts/ tests/ service/ portal/ configs/ 开头的连续路径串
                -> 仓库根下真实存在（行号锚 #L24/:24 与尾部标点由「路径合法字符集
                截断 + rstrip('.')」天然剥除；URL 内的 docs/ 等以左邻字符排除；docs/adr/NNNN
                前缀式引用按 ADR 编号约定核对；以 -/_ 截断或含 YYYYMMDD 的占位写法不核对）；
  4. commit 引用 docs/ 内 .md，且同一行含 commit 字样邻域的 7 位小写 hex
                -> git cat-file 可查为 commit（邻域限定防普通 hex 串误报）。

范围 = docs/**/*.md；白名单（历史证据类，允许引用当时存在的东西）见下方常量。
基线模式：--baseline FILE（基线内存量不 fail，新增违规才 fail，exit 1）；
         --write-baseline FILE 把当前违规落成基线（生成模式恒 exit 0）。
基线行格式（tab 分隔，# 开头为注释）：文件 <TAB> 类型 <TAB> 引用（不含行号，行漂移不失效）。
退出码：0 全过（或仅基线内存量）；1 有未豁免违规；2 用法/环境错误。
用法：python3 scripts/doc_ref_check.py [--baseline FILE | --write-baseline FILE] [--root DIR]
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

# ---------------- 可配置常量（白名单/范围改这里） ----------------
SCAN_DIR = "docs"                       # 扫描范围：docs/**/*.md
WHITELIST_GLOBS = (                     # 整文件白名单（相对仓库根，fnmatch）
    "tests/evidence/**",                # 历史证据类：允许引用当时存在的东西
)
# docs/reviews 下既有历史报告：文件名含 2026 日期的复核/复盘报告
WHITELIST_REVIEWS_DIR = "docs/reviews"
WHITELIST_REVIEW_DATE_RE = re.compile(r"2026\d{4}")
WHITELIST_REVIEW_KIND_RE = re.compile(r"复核|复盘")

# 章节引用的两个目标文档
SPEC_V1_REL = "docs/plans/SPEC-统一数据服务平台_v1.md"
BLUEPRINT_REL = "docs/architecture/架构演进蓝图_v1.md"
RANGE_EXPAND_CAP = 60                   # 区间标题展开上限（防病态区间）

# ---------------- 引用提取 ----------------
# 1) ADR：ADR-00XX，可斜杠连写（ADR-0015/0016）；版本后缀（v1.1 等）天然不在捕获内
ADR_REF_RE = re.compile(r"(?<![0-9A-Za-z])ADR-(\d{4}(?:/\d{4})*)(?![0-9])")
# 2) 章节号：SPEC §N（含无空格 SPEC§N；_v0 等其它文件名因词界守卫不误中）
SPEC_SEC_RE = re.compile(
    r"(?:SPEC-统一数据服务平台_v1\.md|(?<![0-9A-Za-z_])SPEC)[ \t\u00a0]*§(\d+(?:\.\d+)*)"
)
BP_SEC_RE = re.compile(
    r"(?:架构演进蓝图_v1\.md|蓝图)[ \t\u00a0]*§(\d+(?:\.\d+)*)"
)
# 标题行编号：可选区间（### 6.4~6.9）
HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+(\d+(?:\.\d+)?)(?:\s*[~～]\s*(\d+(?:\.\d+)?))?"
)
# 3) 相对路径：前缀目录开头，路径合法字符集（行号锚 #、: 与标点天然截断）
PATH_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_./:@~#&%-])"
    r"((?:docs|scripts|tests|service|portal|configs)/"
    r"[A-Za-z0-9._/\u4e00-\u9fff\u00b7\-]+)"
)
PATH_PREFIXES = ("docs/", "scripts/", "tests/", "service/", "portal/", "configs/")
# 4) commit：同行含 commit 字样才核对 7 位小写 hex（防普通 hex 串误报）
COMMIT_WORD_RE = re.compile(r"commit", re.IGNORECASE)
HEX7_RE = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{7}(?![0-9a-zA-Z])")
ADR_PREFIX_RE = re.compile(r"docs/adr/(\d{4})")   # 前缀式 ADR 路径引用（docs/adr/0003，无文件名）


@dataclass
class Violation:
    file: str      # 相对仓库根 posix 路径
    line: int
    kind: str      # adr / section / section-target / path / commit
    ref: str       # 引用内容（基线身份三要素之一）
    reason: str

    @property
    def identity(self) -> str:
        return f"{self.file}\t{self.kind}\t{self.ref}"


class DocRefChecker:
    def __init__(self, root):
        self.root = Path(root)
        self._heading_cache = {}
        self._adr_cache = {}
        self._commit_cache = {}
        self._missing_targets = set()

    # ---- 资源解析 ----
    def _headings(self, rel):
        """目标文档的章节号集合；文件缺失返回 None。"""
        if rel in self._heading_cache:
            return self._heading_cache[rel]
        p = self.root / rel
        if not p.is_file():
            self._heading_cache[rel] = None
            return None
        nums = set()
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = HEADING_RE.match(ln)
            if not m:
                continue
            a, b = m.group(1), m.group(2)
            nums.add(a)
            if b and b != a:  # 区间标题：6.4~6.9 -> 展开 6.5..6.9
                pa, _, sa = a.rpartition(".")
                pb, _, sb = b.rpartition(".")
                if pa == pb:  # 同父级才展开
                    lo, hi = int(sa), int(sb)
                    if 0 < hi - lo <= RANGE_EXPAND_CAP:
                        for i in range(lo, hi + 1):
                            nums.add(f"{pa}.{i}" if pa else str(i))
        self._heading_cache[rel] = nums
        return nums

    def _adr_exists(self, num):
        if num not in self._adr_cache:
            adr_dir = self.root / "docs/adr"
            hits = list(adr_dir.glob(f"{num}-*.md")) if adr_dir.is_dir() else []
            self._adr_cache[num] = bool(hits)
        return self._adr_cache[num]

    def _commit_exists(self, sha):
        if sha not in self._commit_cache:
            try:
                r = subprocess.run(
                    ["git", "cat-file", "-e", sha + "^{commit}"],
                    cwd=str(self.root), capture_output=True,
                )
                self._commit_cache[sha] = (r.returncode == 0)
            except OSError:
                self._commit_cache[sha] = False
        return self._commit_cache[sha]

    # ---- 单行四类检查 ----
    def check_line(self, rel, lineno, line, out):
        # 1) ADR（每个编号一条违规，便于基线粒度）
        for m in ADR_REF_RE.finditer(line):
            for num in m.group(1).split("/"):
                if not self._adr_exists(num):
                    out.append(Violation(rel, lineno, "adr", "ADR-" + num,
                                         f"docs/adr/{num}-*.md 不存在"))
        # 2) 章节号
        for target, rx in ((SPEC_V1_REL, SPEC_SEC_RE), (BLUEPRINT_REL, BP_SEC_RE)):
            for m in rx.finditer(line):
                nums = self._headings(target)
                if nums is None:
                    if target not in self._missing_targets:
                        self._missing_targets.add(target)
                        out.append(Violation(rel, lineno, "section-target", target,
                                             "目标文件不存在，§ 引用无从核对：" + target))
                    continue
                num = m.group(1)
                if num not in nums:
                    out.append(Violation(rel, lineno, "section",
                                         posixpath.basename(target) + " §" + num,
                                         f"{target} 中不存在编号 {num} 的章节"))
        # 3) 文件路径
        for m in PATH_REF_RE.finditer(line):
            p = m.group(1).rstrip(".")
            while p.endswith("/"):
                p = p[:-1]
            if not p or "/" not in p:
                continue
            if p.endswith(("-", "_")) or "YYYYMMDD" in p:
                continue  # 截断/占位式写法（tests/test_*、开工门槛-<卡>、DR演练_YYYYMMDD 等），非具体路径引用
            m_adr = ADR_PREFIX_RE.fullmatch(p)
            if m_adr:  # 前缀式 ADR 引用（docs/adr/0003）按 ADR 编号约定核对（0003-*.md）
                if not self._adr_exists(m_adr.group(1)):
                    out.append(Violation(rel, lineno, "path", p,
                                         "docs/adr/" + m_adr.group(1) + "-*.md 不存在（前缀式 ADR 引用）"))
                continue
            norm = os.path.normpath(p)
            if not norm.startswith(PATH_PREFIXES):
                out.append(Violation(rel, lineno, "path", m.group(1), "路径越出仓库根：" + p))
                continue
            if not (self.root / norm).exists():
                out.append(Violation(rel, lineno, "path", p, "路径不存在"))
        # 4) commit（仅 docs/ 内 .md 已由扫描范围保证；邻域＝同行含 commit 字样）
        if COMMIT_WORD_RE.search(line):
            for m in HEX7_RE.finditer(line):
                sha = m.group(0)
                if not self._commit_exists(sha):
                    out.append(Violation(rel, lineno, "commit", sha,
                                         "git 中查不到 commit：" + sha))

    # ---- 全量扫描 ----
    def collect(self):
        out = []
        base = self.root / SCAN_DIR
        if not base.is_dir():
            return out
        for p in sorted(base.rglob("*.md")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root).as_posix()
            if is_whitelisted(rel):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                self.check_line(rel, lineno, line, out)
        # 去重（同行同引用只报一次），稳定排序
        seen = set()
        uniq = []
        for v in out:
            key = (v.file, v.line, v.kind, v.ref)
            if key not in seen:
                seen.add(key)
                uniq.append(v)
        uniq.sort(key=lambda v: (v.file, v.line, v.kind, v.ref))
        return uniq


def is_whitelisted(rel):
    for g in WHITELIST_GLOBS:
        if fnmatch.fnmatch(rel, g):
            return True
    parent, name = posixpath.split(rel)
    if (parent == WHITELIST_REVIEWS_DIR
            and WHITELIST_REVIEW_KIND_RE.search(name)
            and WHITELIST_REVIEW_DATE_RE.search(name)):
        return True
    return False


def load_baseline(path):
    if not Path(path).is_file():
        print("== doc_ref_check：基线文件不存在：" + str(path) +
              "（先跑 --write-baseline 生成）==", file=sys.stderr)
        raise SystemExit(2)
    ids = set()
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            ids.add(ln)
    return ids


def write_baseline(path, violations):
    lines = [
        "# doc_ref_check 基线（存量引用断裂冻结；行格式：文件<TAB>类型<TAB>引用）",
        "# 生成：scripts/doc_ref_check.py --write-baseline；基线内违规不 fail，新增违规才 fail。",
        "# 生成时存量：" + str(len(violations)) + " 条",
        "",
    ]
    lines += sorted(v.identity for v in violations)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description="文档交叉引用存在性机检（只检不改）")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="仓库根（默认脚本上级目录）")
    ap.add_argument("--baseline", metavar="FILE", help="基线模式：基线内存量不 fail，新增才 fail")
    ap.add_argument("--write-baseline", metavar="FILE",
                    help="生成模式：把当前违规写成基线文件（恒 exit 0）")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    checker = DocRefChecker(root)
    violations = checker.collect()

    if args.write_baseline:
        write_baseline(args.write_baseline, violations)
        print(f"== doc_ref_check：基线已写入 {args.write_baseline}（存量 {len(violations)} 条，exit 0）==")
        return 0

    known = set()
    if args.baseline:
        known = load_baseline(args.baseline)
    fresh = [v for v in violations if v.identity not in known]
    stale = len(known - {v.identity for v in violations}) if args.baseline else 0

    for v in fresh:
        print(f"{v.file}:{v.line}: {v.ref} : {v.reason}  [{v.kind}]")
    if args.baseline:
        print(f"== doc_ref_check：当前违规 {len(violations)} 条（基线内存量 "
              f"{len(violations) - len(fresh)} 条已豁免，新增 {len(fresh)} 条；"
              f"基线富余 {stale} 条已失效可清理）==")
    else:
        print(f"== doc_ref_check：违规 {len(violations)} 条 ==")

    if fresh:
        print("== doc_ref_check 未过（exit 1）==")
        return 1
    print("== doc_ref_check 全过（exit 0）==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
