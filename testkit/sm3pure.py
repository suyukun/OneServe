# -*- coding: utf-8 -*-
"""SM3 纯函数实现＋手机号规范化（testkit 自含副本，无第三方依赖）。

来源口径：access/sm3_ref.py 官方参考实现（task-011，SPEC v1 §4.2 K1 决策）——
设计稿 §6「移植而非引用」：规范化规则照抄官方口径，SM3 哈希改为本文件纯函数
（官方版依赖 gmssl；testkit 依赖仅 locust＋rich，故自带纯 Python 实现），
算法＝国标 GM/T 0004-2012 / GB/T 32905-2012，与 gmssl 输出逐位一致（自检见下）。

规范化规则（顺序固定，共 4 条）：
  1. 去空格（半角＋全角 \u3000）；
  2. 去连字符（半角 - ＋全角 \uff0d）；
  3. 去 +86 前缀（仅带加号的；不带加号的 86 不去——口径唯一，宁可在预检暴露）；
  4. 字母小写兜底。

流程：normalize(手机号) → UTF-8 编码 → SM3 → 64 位小写十六进制。
"""
from __future__ import annotations

import struct

# GM/T 0004-2012 附录 A 标准向量："abc" 的 SM3（access/sm3_ref.py SM3_ABC_VECTOR 同源）
SM3_ABC_VECTOR = "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"

_IV = [
    0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
    0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E,
]
_MASK = 0xFFFFFFFF


def _rotl(x: int, n: int) -> int:
    n &= 31
    return ((x << n) | (x >> (32 - n))) & _MASK if n else x


def _p0(x: int) -> int:
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x: int) -> int:
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def _pad(data: bytes) -> bytes:
    # 与 SHA-256 同构的位填充：1 bit 置位 → 0 填充 → 64 位大端比特长度
    bit_len = len(data) * 8
    data = data + b"\x80" + b"\x00" * ((56 - (len(data) + 1) % 64) % 64)
    return data + struct.pack(">Q", bit_len)


def sm3_bytes(data: bytes) -> bytes:
    """SM3 摘要，输入字节串，输出 32 字节（输入为手机号量级，性能足够）。"""
    w = [0] * 68
    padded = _pad(data)
    v = list(_IV)
    for off in range(0, len(padded), 64):
        blk = padded[off:off + 64]
        for i in range(16):
            w[i] = struct.unpack_from(">I", blk, i * 4)[0]
        for j in range(16, 68):
            w[j] = (_p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15))
                    ^ _rotl(w[j - 13], 7) ^ w[j - 6]) & _MASK
        a, b, c, d, e, f, g, h = v
        for j in range(64):
            t = 0x79CC4519 if j < 16 else 0x7A879D8A
            ss1 = _rotl((_rotl(a, 12) + e + _rotl(t, j)) & _MASK, 7)
            ss2 = ss1 ^ _rotl(a, 12)
            if j < 16:
                ff = a ^ b ^ c
                gg = e ^ f ^ g
            else:
                ff = (a & b) | (a & c) | (b & c)
                gg = (e & f) | (~e & _MASK & g)
            tt1 = (ff + d + ss2 + (w[j] ^ w[j + 4])) & _MASK  # W'[j]＝W[j]⊕W[j+4]（与官方参考实现逐位对齐，国标向量验证）
            tt2 = (gg + h + ss1 + w[j]) & _MASK
            d, c, b, a, h, g, f, e = (
                c, _rotl(b, 9), a, tt1, g, _rotl(f, 19), e, _p0(tt2))
        v = [x ^ y for x, y in zip(v, (a, b, c, d, e, f, g, h))]
    return struct.pack(">8I", *v)


def sm3_hex(text: str) -> str:
    """字符串按 UTF-8 编码后取 SM3，64 位小写十六进制。"""
    return sm3_bytes(text.encode("utf-8")).hex()


def normalize(raw: str) -> str:
    """官方口径规范化手机号（规则见模块 docstring，与 access/sm3_ref.py 逐条同源）。"""
    s = raw.replace(" ", "").replace("\u3000", "")      # 1) 去空格
    s = s.replace("-", "").replace("\uff0d", "")        # 2) 去连字符
    if s.startswith("+86"):                             # 3) 去 +86 前缀（仅带加号的）
        s = s[3:]
    return s.lower()                                    # 4) 字母小写兜底


def phone_sm3(raw: str) -> str:
    """官方入口：规范化手机号 → SM3（合作方接入口径，S2 SDK 通道用它测端到端含客户端哈希成本）。"""
    return sm3_hex(normalize(raw))


def selftest(verbose: bool = False) -> list[str]:
    """自检：国标向量＋规范化等价性＋（gmssl 可导入时）逐位交叉验证。返回失败项清单（空＝全过）。"""
    failures: list[str] = []
    if sm3_hex("abc") != SM3_ABC_VECTOR:
        failures.append(f"国标向量不符：{sm3_hex('abc')}")
    if phone_sm3("+86 138-0013-8000") != phone_sm3("13800138000"):
        failures.append("规范化等价性失败：+86/空格/连字符样式应哈希一致")
    if phone_sm3("8613800138000") == phone_sm3("13800138000"):
        failures.append("裸 86 前缀不得去除（口径红线）")
    try:  # 可选交叉验证：gmssl 仅在本机压测环境存在，testkit 不依赖它
        from gmssl import func as _f, sm3 as _s  # type: ignore

        for probe in ("abc", "13800138000", "138 0013-8000", "x" * 200):
            want = _s.sm3_hash(_f.bytes_to_list(probe.encode("utf-8")))
            if sm3_hex(probe) != want:
                failures.append(f"与 gmssl 不一致：{probe!r}")
                break
    except ImportError:
        if verbose:
            print("[sm3pure] gmssl 未安装，跳过交叉验证（纯函数已过国标向量）")
    if verbose and not failures:
        print(f"[sm3pure] 自检通过：国标向量 {SM3_ABC_VECTOR[:16]}… ＋规范化等价性")
    return failures


if __name__ == "__main__":
    import sys

    bad = selftest(verbose=True)
    if bad:
        print("\n".join(bad), file=sys.stderr)
        sys.exit(1)
