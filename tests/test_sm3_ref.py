#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task-011 三条 AC 的断言脚本（SM3 参考实现＋联调预检）。

覆盖（对应 backlog/tasks/task-011 AC）：
  AC-1  国标向量：SM3("abc") == 66c7f0f4...ba8e0（GM/T 0004-2012）。
  AC-2  规范化等价："+86 138-0013-8000" 与 "13800138000" 产出同一哈希
        （含 CLI 端到端各跑一遍比对）。
  AC-3  预检两分支：一致答案集 → 通过且退出 0；被篡改答案集 → 失败且退出 1。

隐私纪律：预检手机号为工具随机伪造，测试全程在 tempfile 临时目录内完成，
结束即删，仓库不留任何含手机号的 fixture 文件。

运行方式（二选一）：
  python3 tests/test_sm3_ref.py              # 直跑
  python3 -m pytest tests/test_sm3_ref.py -q # pytest 协议
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "access"))

from sm3_ref import SM3_ABC_VECTOR, phone_sm3, sm3_hex  # noqa: E402

PRECHECK = REPO_ROOT / "access" / "precheck.py"
SM3_REF = REPO_ROOT / "access" / "sm3_ref.py"


def _run(*cli_args: str) -> subprocess.CompletedProcess:
    """跑本仓库工具的 CLI（用当前解释器，不依赖 PATH）。"""
    return subprocess.run(
        [sys.executable, *[str(a) for a in cli_args]],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


class TestSm3Ref(unittest.TestCase):
    """三条 AC，一一对应；断言消息里带足够上下文便于排障。"""

    def test_ac1_national_vector_abc(self):
        """AC-1：国标向量 abc。"""
        self.assertEqual(
            sm3_hex("abc"), SM3_ABC_VECTOR,
            "SM3(\"abc\") 与国标 GM/T 0004-2012 向量不符",
        )

    def test_ac2_plus86_format_equivalence(self):
        """AC-2：+86 138-0013-8000 ≡ 13800138000（同哈希），CLI 亦然。"""
        canonical, dressed = "13800138000", "+86 138-0013-8000"
        self.assertEqual(
            phone_sm3(dressed), phone_sm3(canonical),
            "规范化前后哈希不同——口径实现有漂移",
        )
        # CLI 端到端再各跑一遍，锁住交付给合作方的命令行入口。
        via_cli = [
            _run(SM3_REF, dressed).stdout.strip(),
            _run(SM3_REF, canonical).stdout.strip(),
        ]
        self.assertEqual(via_cli[0], via_cli[1], f"CLI 两式输出不同：{via_cli}")
        self.assertEqual(via_cli[0], phone_sm3(canonical), "CLI 输出与函数口径不符")

    def test_ac3_precheck_pass_and_fail_branches(self):
        """AC-3：预检一致→通过/退出 0；篡改一条→失败/退出 1。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            answers = tmpdir / "answers.csv"
            gen = _run(PRECHECK, "--generate", "8", "--out", answers, "--seed", "7")
            self.assertEqual(gen.returncode, 0, f"出题失败：{gen.stderr}")
            self.assertTrue(answers.is_file(), "答案文件未生成")
            self.assertEqual(
                len(gen.stdout.strip().splitlines()) - 2, 8,
                "题目条数（去 2 行注释）应为 8",
            )

            # 通过分支：应答方如实回填（复制答案集为结果集）。
            results = tmpdir / "results.csv"
            results.write_text(answers.read_text(encoding="utf-8"), encoding="utf-8")
            ok = _run(PRECHECK, "--verify", results, "--answers", answers)
            self.assertEqual(ok.returncode, 0, f"一致集应通过：{ok.stdout}")
            self.assertIn("预检结论：通过", ok.stdout)
            self.assertIn("一致 8", ok.stdout)

            # 失败分支：篡改第一条哈希的末位。
            text = results.read_text(encoding="utf-8").splitlines()
            head, first, rest = text[0], text[1], text[2:]
            col = first.split(",")
            col[1] = ("0" if col[1][-1] != "0" else "1") + col[1][:-1]  # 只改末位 1 个字符
            results.write_text("\n".join([head, ",".join(col), *rest]) + "\n", encoding="utf-8")
            bad = _run(PRECHECK, "--verify", results, "--answers", answers)
            self.assertEqual(bad.returncode, 1, f"不一致集应失败：{bad.stdout}")
            self.assertIn("不一致", bad.stdout)
            self.assertIn("预检结论：失败", bad.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
