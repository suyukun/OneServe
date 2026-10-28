#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""联调哈希预检工具（OneServe task-011，SPEC v1 §4.2：双方各算对答案，防口径漂移）。

两种模式：
  出题  python3 access/precheck.py --generate 10 --out answers.csv [--seed 42]
        随机生成 N 个【伪造】手机号（仅内存生成；只有 --out 指定的答案文件落盘，
        交出题方保管，不给应答方），题目（照抄样式的手机号清单）打印到 stdout。
        题目故意混入 +86/空格/连字符样式，专钓规范化口径漂移。
  校验  python3 access/precheck.py --verify results.csv --answers answers.csv
        逐条比对 应答方回填的哈希 与 标准答案，输出 一致/不一致 清单＋汇总；
        任何 不一致/缺失/多余 → 退出码 1（全部一致 → 0）。

回填文件格式（与答案文件同构，CSV，UTF-8，首行表头可省）：
  phone,hash
  +86 138-0013-8000,<你算出的哈希>
  phone 列照抄题目原样即可，不要自行改写。

依赖：gmssl（同目录 sm3_ref.py 的说明）；本工具不占端口、不依赖 Docker。
"""

import argparse
import csv
import random
import sys
from pathlib import Path

# 复用官方参考实现的口径——预检答案必须出自同一份代码，避免"参考实现自己漂移"。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sm3_ref import phone_sm3  # noqa: E402

HEADER = ["phone", "hash"]


def gen_fake_phone(rng: random.Random) -> str:
    """随机伪造一个合规样式的大陆手机号（1[3-9]开头共 11 位）。纯随机，非真实数据。"""
    return "1" + rng.choice("3456789") + "".join(rng.choices("0123456789", k=9))


def dress_up(phone: str, style: int) -> str:
    """给规范化号套 4 种合法书写样式轮转，考应答方的规范化。口径规则见 README。"""
    if style == 0:
        return phone                                   # 裸 11 位
    if style == 1:
        return "+86" + phone                           # +86 紧贴
    if style == 2:
        return phone[:3] + "-" + phone[3:7] + "-" + phone[7:]  # 连字符分段
    return "+86 " + phone[:3] + "-" + phone[3:7] + "-" + phone[7:]  # +86＋空格＋连字符


def read_rows(path: Path) -> dict[str, str]:
    """读 CSV（容忍 utf-8 BOM／有无表头），返回 {phone: hash}；重复 phone 视为格式错误。"""
    rows: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.reader(fh), start=1):
            if not row or all(not cell.strip() for cell in row):
                continue  # 跳过空行
            if len(row) < 2:
                raise SystemExit(f"格式错误：{path} 第 {lineno} 行不足两列：{row}")
            phone, got = row[0], row[1]
            if lineno == 1 and phone.strip().lower() == HEADER[0]:
                continue  # 跳过表头
            if phone in rows:
                raise SystemExit(f"格式错误：{path} 第 {lineno} 行 phone 重复：{phone!r}")
            rows[phone] = got.strip().lower()  # 哈希统一按小写比对（参考实现即小写）
    if not rows:
        raise SystemExit(f"格式错误：{path} 没有任何数据行")
    return rows


def do_generate(n: int, out: Path, seed: int | None) -> int:
    """出题：题目打 stdout，标准答案写 --out（CSV：phone,hash）。"""
    rng = random.Random(seed)  # 固定 seed 可复现，便于排障
    phones = set()
    while len(phones) < n:
        phones.add(gen_fake_phone(rng))
    answers: list[tuple[str, str]] = []
    for i, phone in enumerate(sorted(phones)):
        quiz = dress_up(phone, i % 4)
        answers.append((quiz, phone_sm3(quiz)))
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(answers)
    print("# 预检题目（共 %d 题）：请应答方逐题按官方口径算 SM3，回填 CSV(phone,hash)" % len(answers))
    for quiz, _ in answers:
        print(quiz)
    print(f"# 标准答案已写：{out}（出题方保管，勿发应答方）")
    return 0


def do_verify(results: Path, answers: Path) -> int:
    """校验：逐条 一致/不一致 清单＋汇总；不一致/缺失/多余 任一存在即退出 1。"""
    expect = read_rows(answers)
    got = read_rows(results)
    ok_cnt = bad_cnt = 0
    lines: list[str] = []
    for phone, want in expect.items():
        if phone not in got:
            lines.append(f"✗ 缺失   {phone}（应答方未回填此题）")
            bad_cnt += 1
        elif got[phone] == want:
            lines.append(f"✓ 一致   {phone}")
            ok_cnt += 1
        else:
            lines.append(f"✗ 不一致 {phone}  期望={want}  实际={got[phone]}")
            bad_cnt += 1
    extra = [p for p in got if p not in expect]
    for phone in extra:
        lines.append(f"✗ 多余   {phone}（不在题目集内，疑题目版本不符）")
        bad_cnt += 1
    print("\n".join(lines))
    total = len(expect)
    print(f"汇总：题目 {total} 条，一致 {ok_cnt}，不一致/缺失/多余 {bad_cnt}")
    if bad_cnt:
        print("预检结论：失败——请对照 README.md 修正规范化/哈希口径后重测。")
        return 1
    print("预检结论：通过——口径与官方参考实现一致，可进入联调。")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="OneServe 联调哈希预检（出题/校验）")
    ap.add_argument("--generate", type=int, metavar="N", help="出题：生成 N 个样例手机号")
    ap.add_argument("--out", type=Path, metavar="FILE", help="出题：标准答案文件路径（CSV）")
    ap.add_argument("--verify", type=Path, metavar="FILE", help="校验：应答方回填的结果文件（CSV）")
    ap.add_argument("--answers", type=Path, metavar="FILE", help="校验：标准答案文件（--generate 的产物）")
    ap.add_argument("--seed", type=int, default=None, help="出题：随机种子（可选，便于复现）")
    args = ap.parse_args(argv)
    if args.generate is not None:
        if args.generate < 1:
            ap.error("--generate N 须为正整数")
        if args.out is None:
            ap.error("--generate 需要配 --out 指定答案文件路径")
        return do_generate(args.generate, args.out, args.seed)
    if args.verify is not None:
        if args.answers is None:
            ap.error("--verify 需要配 --answers 指定标准答案文件")
        return do_verify(args.verify, args.answers)
    ap.error("请指定模式：--generate N --out 答案.csv 或 --verify 回填.csv --answers 答案.csv")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
