#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SM3 官方参考实现（OneServe task-011，SPEC v1 §4.2 K1 决策）。

用途：各合作方接入 OneServe 标签 API 时，统一按本文件口径计算手机号 SM3，
防止十余家各自实现口径漂移 → 标签查询静默返回空值。抄作业说明见同目录 README.md。

规范化规则（白纸黑字，顺序固定，共 4 条）：
  1. 去空格：删除所有半角空格与全角空格（\u3000）；
  2. 去连字符：删除所有半角连字符（-）与全角连字符（\uff0d）；
  3. 去 +86 前缀：清理后若以 "+86" 开头则去掉该前缀；
     注意：不带加号的 "86" 前缀【不】去——口径唯一，宁可在预检暴露也不静默猜；
  4. 字母小写：整体转小写（手机号正常不含字母，此条为手滑兜底）。

流程：normalize(手机号) → UTF-8 编码 → SM3 → 64 位小写十六进制字符串。

CLI（合作方一行命令自测）：
  python3 access/sm3_ref.py 13800138000            # 规范化号的 SM3
  python3 access/sm3_ref.py "+86 138-0013-8000"    # 与上一条同一哈希（自动规范化）

依赖：gmssl（pip3 install gmssl）。
"""

import sys

from gmssl import func, sm3

# 国标 GM/T 0004-2012 附录 A 标准向量："abc" 的 SM3，用于自检（AC-1）。
SM3_ABC_VECTOR = "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"


def normalize(raw: str) -> str:
    """按官方口径规范化手机号，规则详见模块 docstring（与 README 同源）。"""
    # 1) 去空格（半角/全角）
    s = raw.replace(" ", "").replace("\u3000", "")
    # 2) 去连字符（半角/全角）
    s = s.replace("-", "").replace("\uff0d", "")
    # 3) 去 +86 前缀（仅带加号的；不带加号的 86 不动，防误伤）
    if s.startswith("+86"):
        s = s[3:]
    # 4) 字母小写兜底
    return s.lower()


def sm3_hex(text: str) -> str:
    """字符串按 UTF-8 编码后取 SM3，返回 64 位小写十六进制。"""
    return sm3.sm3_hash(func.bytes_to_list(text.encode("utf-8")))


def phone_sm3(raw: str) -> str:
    """官方入口：规范化手机号 → SM3。合作方接入一律调这个口径。"""
    return sm3_hex(normalize(raw))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：python3 access/sm3_ref.py <手机号>", file=sys.stderr)
        print("示例：python3 access/sm3_ref.py \"+86 138-0013-8000\"", file=sys.stderr)
        return 2
    print(phone_sm3(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
