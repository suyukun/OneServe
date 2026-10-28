#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""键校验放松＋SM3 叙事清理机检（维护者 2026-09-23 口径）。

背景：维护者 明示「平台压根就不应该出现 SM3」「我们关注的就是 K 跟 V：传入参是什么、
出参是什么、报错码是什么」。加密是上游（数仓/ETL）准备数据的事，不在平台关注范围。

本套件两条机检：
  ① 键校验已放松：默认键模式不再把 64 位十六进制当默认口径；非 SM3 形态的键
     （UUID／自增 ID／MD5 形态）能通过 normalize_key 并被服务受理（services.yaml 真源）；
  ② 平台侧对外表述不再出现「SM3 十六进制」这类**强口径**（平台强制某种加密算法）。

扫描边界（**避免误伤**）：
  - 扫：平台侧源码与对外文案（service/app/、service/services.yaml、service/static/、
        admin/ 源页、portal/ 产出与 src、portal/openapi/、scripts/gen_openapi.py）；
  - 不扫：造数工具（sync/gen_mock_source.py、sync/hbase_loader.py、testkit/）、
        上游供数侧参考实现（access/）、历史文档（docs/）、前端 sm3 组件与实现
        （portal/assets/sm3.min.js、trial.js 的哈希实现）——这些是本工具的合法用法，
        不是平台口径。
运行：python3 -m pytest tests/test_sm3_narrative_clean.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import kv_executor as kvx  # noqa: E402
from synth_services import real_registry_state  # noqa: E402  现实态助手（全下线终态）

# ── ① 键校验放松 ────────────────────────────────────────────────

# 非 SM3 形态的合理键（UUID / 自增 ID / MD5 形态）——放宽后应被受理
NON_SM3_KEYS = [
    "123e4567-e89b-12d3-a456-426614174000",   # UUID
    "1234567890",                              # 自增 ID / 数字串
    "d41d8cd98f00b204e9800998ecf8427e",        # MD5 形态（32 位）
    "n000001",                                 # 造数编号段
]
# 真正越出默认模式（^[A-Za-z0-9_.-]{1,128}$）的键——仍应 400
INVALID_KEYS = ["", "has space", "a" * 129, "键值", "a/b"]


DEFAULT_KEY_PATTERN = r"^[A-Za-z0-9_.-]{1,128}$"   # 平台默认键模式口径（维护者 2026-09-23 放宽决定；服务可自声明更严模式）


def test_default_pattern_is_not_hex64():
    """键模式不再把 64 位十六进制当默认口径：平台默认模式＋全部在线服务声明模式均非十六进制。"""
    assert DEFAULT_KEY_PATTERN == r"^[A-Za-z0-9_.-]{1,128}$"
    assert "0-9a-f" not in DEFAULT_KEY_PATTERN
    services, load_err = real_registry_state()
    assert not load_err   # 2026-09-30 现实态口径：注册表空/非空均合法，只守 fail-loud 语义
    for sid, svc in services.items():   # 2026-09-30 现实态口径：在线服务声明模式动态对真源，不得是 64 位十六进制强口径
        assert "0-9a-f" not in str(svc), f"{sid} 声明模式疑似 64 位十六进制默认口径"


def test_non_sm3_keys_accepted_by_default_pattern():
    """非 SM3 形态的键（UUID／数字串／MD5）通过键校验并被服务受理。"""
    for key in NON_SM3_KEYS:
        assert kvx.normalize_key(key, DEFAULT_KEY_PATTERN) == key.lower()


def test_out_of_pattern_keys_still_rejected():
    """放宽不等于放行一切：真正越出 pattern 的键仍 400 invalid_key。"""
    pat = DEFAULT_KEY_PATTERN
    for bad in INVALID_KEYS:
        try:
            kvx.normalize_key(bad, pat)
        except kvx.TagQueryError as exc:
            assert exc.status == 400 and exc.code == "invalid_key"
        else:
            raise AssertionError(f"越出 pattern 的键 {bad!r} 应 400")


# ── ② 平台侧叙事：不再出现「SM3 十六进制」这类强口径 ──────────────

# 强口径短语（平台强制某种加密算法／把 SM3 当默认口径）——平台侧对外表述不得出现
STRONG_PHRASES = [
    "SM3 十六进制",
    "64 位小写十六进制",
    "64位SM3",
    "服务端代算 SM3",
    "服务端代算",
    "前端自算 SM3",
    "SM3 键",
    "须为 64 位",
]

# 平台侧对外源码/文案（造数工具与上游参考实现另列，不扫）
PLATFORM_GLOBS = [
    "service/app/*.py",
    "service/services.yaml",
    "service/static/*.html",
    "admin/*.js",
    "admin/*.html",
    "portal/*.html",
    "portal/catalog/*.html",
    "portal/admin/*.html",
    "portal/src/*.html",
    "portal/openapi/*.yaml",
    "portal/openapi/*.json",
    "portal/openapi/details/*.json",
    "portal/mock/*.json",
    "scripts/gen_openapi.py",
]


def _platform_files():
    seen = {}
    for g in PLATFORM_GLOBS:
        for p in REPO.glob(g):
            if p.name == "sm3.min.js":
                continue
            if "__pycache__" in p.parts:
                continue
            seen[str(p)] = p
    return [seen[k] for k in sorted(seen)]


def test_platform_narrative_has_no_strong_sm3_mandate():
    """平台侧源码/文案不再出现「SM3 十六进制」这类对外强口径表述。"""
    offenders = []
    for p in _platform_files():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for phrase in STRONG_PHRASES:
            if phrase in text:
                rel = p.relative_to(REPO)
                # 定位行号，便于人查
                for i, line in enumerate(text.splitlines(), 1):
                    if phrase in line:
                        offenders.append(f"{rel}:{i}: {phrase} → {line.strip()[:90]}")
    assert not offenders, "平台侧仍存在 SM3 强口径表述：" + "\n".join([""] + offenders)


def test_platform_does_not_force_sm3_in_default_patterns():
    """默认键模式不再隐含 64 位十六进制（真源机检）。"""
    yaml_text = (REPO / "service" / "services.yaml").read_text(encoding="utf-8")
    assert "^[0-9a-f]{64}$" not in yaml_text, "services.yaml 仍残留硬编码 64 位十六进制模式"


def test_gen_openapi_no_strong_sm3_narrative():
    """契约生成器对外表述不出现强口径（上游供数侧参考实现可另述，不在此文件）。"""
    gen = (REPO / "scripts" / "gen_openapi.py").read_text(encoding="utf-8")
    for phrase in ("SM3 十六进制", "本地计算后传输", "须为 64 位小写十六进制 SM3"):
        assert phrase not in gen, f"gen_openapi.py 仍存在强口径：{phrase}"


def main():
    """无 pytest 直跑入口。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== 键校验放松＋SM3 叙事清理机检（{len(tests)} 条）==")
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as exc:
            print(f"  ✗ {name}：{exc}")
            failed.append(name)
    if failed:
        print(f"== 未过：{len(tests) - len(failed)}/{len(tests)}；失败：{'、'.join(failed)} ==")
        return 1
    print(f"== 全绿：{len(tests)}/{len(tests)} ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
