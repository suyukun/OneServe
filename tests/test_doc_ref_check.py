#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc_ref_check 负例＋正例单测（tests/test_doc_ref_check.py）。

全 hermetic：tmp_path 造假仓造「不存在的引用」（负例 fixture），不依赖真实 docs、
不碰 dev 容器组；commit 正例用临时 git init 造真 commit（无 git 则 skip）。
跑法：python3 -m pytest tests/test_doc_ref_check.py -q（run_gates 动态枚举自动入列）。
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.doc_ref_check import (  # noqa: E402
    DocRefChecker,
    is_whitelisted,
    main,
)


def make_repo(tmp_path, files):
    """在 tmp_path 下按 {相对路径: 内容} 造假仓。"""
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


def found(violations, kind, ref):
    return any(v.kind == kind and v.ref == ref for v in violations)


def git_commit_short(root):
    """临时 git init 造一个真 commit，返回 7 位短 hash；无 git 则 skip。"""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git 不可用")

    def run(cmd):
        subprocess.run(cmd, cwd=str(root), check=True, capture_output=True)

    run(["git", "init", "-q"])
    run(["git", "config", "user.email", "t@example.com"])
    run(["git", "config", "user.name", "t"])
    (root / "f.txt").write_text("x", encoding="utf-8")
    run(["git", "add", "f.txt"])
    run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "init"])
    full = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                          capture_output=True, text=True, check=True).stdout.strip()
    return full[:7]


# ---------------- 负例 1：ADR 引用 ----------------
def test_negative_adr_ref(tmp_path):
    root = make_repo(tmp_path, {
        "docs/adr/0001-示例决策.md": "# ADR-0001",
        "docs/note.md": "依据 ADR-0001 v1.1（带版本后缀）与 ADR-0099（不存在）。",
    })
    vs = DocRefChecker(root).collect()
    assert found(vs, "adr", "ADR-0099")
    assert not found(vs, "adr", "ADR-0001")   # 在场的 ADR 不误报
    assert len(vs) == 1


def test_negative_adr_slash_chain(tmp_path):
    root = make_repo(tmp_path, {
        "docs/adr/0001-a.md": "# ADR-0001",
        "docs/note.md": "见 ADR-0001/0098。",
    })
    vs = DocRefChecker(root).collect()
    assert found(vs, "adr", "ADR-0098")       # 斜杠连写拆开逐个核对
    assert not found(vs, "adr", "ADR-0001")


# ---------------- 负例 2：章节引用 ----------------
SPEC_FIX = {"docs/plans/SPEC-统一数据服务平台_v1.md": "# SPEC\n## 1 章\n### 1.1 节\n"}
BP_FIX = {"docs/architecture/架构演进蓝图_v1.md": "# 蓝图\n## 1 目标\n"}


def test_negative_section_ref(tmp_path):
    root = make_repo(tmp_path, {**SPEC_FIX, **BP_FIX,
        "docs/note.md": "SPEC §2 与 SPEC§9 不在；SPEC §1.1 在；蓝图 §99 不在。"})
    vs = DocRefChecker(root).collect()
    assert found(vs, "section", "SPEC-统一数据服务平台_v1.md §2")
    assert found(vs, "section", "SPEC-统一数据服务平台_v1.md §9")   # 无空格写法也查
    assert found(vs, "section", "架构演进蓝图_v1.md §99")
    assert not found(vs, "section", "SPEC-统一数据服务平台_v1.md §1.1")
    assert len(vs) == 3


def test_section_range_heading_expansion(tmp_path):
    root = make_repo(tmp_path, {
        "docs/plans/SPEC-统一数据服务平台_v1.md": "# SPEC\n### 6.4~6.9（合并节）\n",
        "docs/note.md": "SPEC §6.7 在（区间展开）；SPEC §6.10 不在。",
    })
    vs = DocRefChecker(root).collect()
    assert found(vs, "section", "SPEC-统一数据服务平台_v1.md §6.10")
    assert len(vs) == 1


def test_section_other_spec_files_not_scoped(tmp_path):
    root = make_repo(tmp_path, {
        "docs/plans/SPEC-统一数据服务平台_v0.md": "# v0\n",
        "docs/note.md": "SPEC_v0 §2 与 SPEC差距登记 §3 均不在核对范围。",
    })
    assert DocRefChecker(root).collect() == []   # 只有 v1 与蓝图是 § 核对目标


def test_section_target_missing(tmp_path):
    root = make_repo(tmp_path, {"docs/note.md": "SPEC §1。"})
    vs = DocRefChecker(root).collect()
    assert len(vs) == 1 and vs[0].kind == "section-target"


# ---------------- 负例 3：文件路径 ----------------
def test_negative_path_ref(tmp_path):
    root = make_repo(tmp_path, {
        "scripts/ok.py": "x=1",
        "docs/real.md": "x",
        "tests/real.py": "x=1",
        "docs/note.md": "见 docs/nope/缺失.md；scripts/ok.py 与 tests/real.py 在；"
                        "尾部标点剥除 docs/real.md。行号锚剥除 docs/real.md#L12 与 docs/real.md:34。",
    })
    vs = DocRefChecker(root).collect()
    assert found(vs, "path", "docs/nope/缺失.md")
    assert len(vs) == 1                       # 存在的/剥锚后存在的都不报


def test_path_url_and_placeholder_guard(tmp_path):
    root = make_repo(tmp_path, {
        "docs/note.md": "URL https://example.com/docs/fake.md 不查；占位 tests/test_*.py、"
                        "docs/plans/开工门槛-<卡>.md、模板 docs/x/DR_YYYYMMDD.md 均不查。",
    })
    assert DocRefChecker(root).collect() == []


def test_prefix_adr_path_ref(tmp_path):
    root = make_repo(tmp_path, {
        "docs/adr/0003-审批走OA.md": "# ADR-0003",
        "docs/note.md": "依据 docs/adr/0003（审批）与 docs/adr/0009（不存在）。",
    })
    vs = DocRefChecker(root).collect()
    assert found(vs, "path", "docs/adr/0009")  # 前缀式引用按编号约定核对
    assert not found(vs, "path", "docs/adr/0003")
    assert len(vs) == 1


# ---------------- 负例 4：commit 引用 ----------------
def test_commit_neighborhood_and_positive(tmp_path):
    sha = git_commit_short(tmp_path)
    root = make_repo(tmp_path, {
        "docs/note.md": "落地 commit dead123（不存在）。\n"
                        "裸哈希 dead123 无邻域词触发，不查。\n"
                        "真提交 commit " + sha + " 在。",
    })
    vs = DocRefChecker(root).collect()
    assert found(vs, "commit", "dead123")
    assert not found(vs, "commit", sha)
    assert len(vs) == 1


# ---------------- 白名单 ----------------
def test_whitelist_reviews(tmp_path):
    root = make_repo(tmp_path, {
        "docs/reviews/复盘A_20261020.md": "ADR-0099 与 docs/nope.md（白名单内）",
        "docs/reviews/输入包.md": "ADR-0099（无日期，不在白名单）",
        "docs/reviews/盘点B_20261020.md": "ADR-0099（无复核/复盘字样，不在白名单）",
    })
    vs = DocRefChecker(root).collect()
    files = {v.file for v in vs}
    assert "docs/reviews/复盘A_20261020.md" not in files
    assert "docs/reviews/输入包.md" in files
    assert "docs/reviews/盘点B_20261020.md" in files
    assert is_whitelisted("tests/evidence/deep/dir.md")


# ---------------- 基线模式 ----------------
def test_baseline_mode(tmp_path):
    root = make_repo(tmp_path, {"docs/bad.md": "ADR-0099。"})
    argv = ["--root", str(root)]
    bl = str(tmp_path / "bl.txt")

    assert main(argv) == 1                                  # 无基线：存量即违规
    assert main(argv + ["--write-baseline", bl]) == 0       # 生成基线恒 0
    assert "adr\tADR-0099" in Path(bl).read_text(encoding="utf-8")
    assert main(argv + ["--baseline", bl]) == 0             # 基线内不 fail
    (root / "docs/bad2.md").write_text("ADR-0098。", encoding="utf-8")
    assert main(argv + ["--baseline", bl]) == 1             # 新增违规 fail


# ---------------- 正例：一致小仓全绿 ----------------
def test_positive_repo_clean(tmp_path):
    root = make_repo(tmp_path, {
        "docs/adr/0001-示例决策.md": "# ADR-0001",
        "docs/plans/SPEC-统一数据服务平台_v1.md": "# SPEC\n## 1 章\n### 1.1 节\n",
        "docs/architecture/架构演进蓝图_v1.md": "# 蓝图\n## 1 目标\n",
        "scripts/ok.py": "x=1",
    })
    assert DocRefChecker(root).collect() == []
    assert main(["--root", str(root)]) == 0
