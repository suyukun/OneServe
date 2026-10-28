#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reconcile.py --table 参数两态回归（与 switch.py 对称补齐）：

  ① --help 声明 --table，且帮助文案与 switch.py 逐字一致（对齐要求）；
  ② 不传 --table：存量单域口径（向后兼容）——对账落 sync:recon:<版本>，
     ver/数据键均在无表段空间，模块键空间保持静止值；
  ③ 传 --table：按表键空间对账——前置查/三查全链落 sync:ver:<表>:<版本>、
     tags:<表>:<版本>:*，对账结果落 sync:recon:<表>:<版本>，不碰单域键。

分层纪律：内存假 Redis（沿 tests/test_p0_2_recon_write.py 手写替身惯例），
不依赖真 Redis；夹具灌数口径与该文件命令行基准一致（ver_meta_set＋非空标签数据键）。
"""
from __future__ import annotations

import fnmatch
import io
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "sync"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sync_config as C          # noqa: E402
import reconcile as rec          # noqa: E402
import switch as sw              # noqa: E402

VER = "20261015"
TABLE = "oneserve_demo"
KEYS = ["a" * 64, "b" * 64]
ROWS = {KEYS[0]: {"tag_a": "v1"}, KEYS[1]: {"tag_b": "v2"}}
CSV_TEXT = ("usr_key_sm3,tag_a,tag_b\n"
            + KEYS[0] + ",v1,\n"
            + KEYS[1] + ",,v2\n")


# ======================================================================
# 内存假 Redis（只需 reconcile.py 实际触达的接口面）
# ======================================================================

class _Pipe:
    def __init__(self, r): self._r, self._ops = r, []
    def get(self, k): self._ops.append(k)
    def execute(self): return [self._r.get(k) for k in self._ops]


class FakeRedis:
    def __init__(self): self.kv, self.hashes = {}, {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v): self.kv[k] = v; return True
    def delete(self, *keys):
        n = 0
        for k in keys:
            n += 1 if self.kv.pop(k, None) is not None else 0
            n += 1 if self.hashes.pop(k, None) is not None else 0
        return n
    def hset(self, k, mapping=None):
        self.hashes.setdefault(k, {}).update(mapping or {})
        return len(mapping or {})
    def hgetall(self, k): return dict(self.hashes.get(k, {}))
    def scan(self, cursor=0, match=None, count=1000):
        return 0, [k for k in self.kv if fnmatch.fnmatchcase(k, match or "*")]
    def pipeline(self, transaction=False): return _Pipe(self)
    def ping(self): return True


def _seed(r, table=None):
    """按指定域灌版本元信息＋非空标签数据键（加载侧同源口径），返回 CSV 路径。"""
    import hashlib
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    f.write(CSV_TEXT)
    f.close()
    from common import data_key, ver_meta_set
    saved = (C.NS, C.ALIAS_KEY, C.VER_META, C.RECON_META, C.SWITCH_LOG, C.DELETE_LOG)
    if table:
        C.use_table(table)
    try:
        ver_meta_set(r, VER, rows=len(ROWS), tag_cols=2, source=f.name,
                     source_sha256=hashlib.sha256(CSV_TEXT.encode()).hexdigest(),
                     coverage_src="0.500000", data_as_of="2026-10-15")
        for k, tags in ROWS.items():
            r.set(data_key(VER, k), json.dumps(tags, ensure_ascii=False))
    finally:
        C.NS, C.ALIAS_KEY, C.VER_META, C.RECON_META, C.SWITCH_LOG, C.DELETE_LOG = saved
    return f.name


@pytest.fixture(autouse=True)
def _restore_keyspace():
    """被测代码会切全局键空间：测后一律恢复存量静止值，防污染同进程其他套件。"""
    yield
    C.NS, C.ALIAS_KEY, C.VER_META, C.RECON_META, C.SWITCH_LOG, C.DELETE_LOG = C.LEGACY_KEYSPACE


def _run(monkeypatch, r, csv_path, extra=None):
    argv = ["reconcile.py", "--csv", csv_path, "--version", VER] + (extra or [])
    monkeypatch.setattr(rec, "redis_conn", lambda: r)
    monkeypatch.setattr(sys, "argv", argv)
    return rec.main()


# ======================================================================
# ① help 声明 --table，文案与 switch.py 逐字一致
# ======================================================================

def test_help_declares_table_aligned_with_switch(monkeypatch, capsys):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["reconcile.py", "--help"])
    with redirect_stdout(buf):
        with pytest.raises(SystemExit) as ei:
            rec.main()
    assert ei.value.code == 0
    help_text = buf.getvalue()
    assert "--table" in help_text, "--help 未声明 --table"
    assert "存量单域" in help_text, "--table 帮助未写明缺省口径"

    def _table_help(src):
        m = re.search(r'add_argument\(\s*"--table".*?help="([^"]+)"', src, re.S)
        assert m, "源码中未找到 --table 的 help 文案"
        return m.group(1)
    rec_src = (REPO / "sync" / "reconcile.py").read_text(encoding="utf-8")
    sw_src = (REPO / "sync" / "switch.py").read_text(encoding="utf-8")
    assert _table_help(rec_src) == _table_help(sw_src), "--table help 文案与 switch.py 不对齐"
    # 缺省值同源：两脚本 --table 缺省均引用 C.DEFAULT_TABLE（空＝不切域）
    assert C.DEFAULT_TABLE == ""
    assert "default=C.DEFAULT_TABLE" in rec_src and "default=C.DEFAULT_TABLE" in sw_src, \
        "--table 缺省值未同源引用 C.DEFAULT_TABLE"


# ======================================================================
# ② 不传 --table：存量单域（向后兼容），结果落 sync:recon:<版本>
# ======================================================================

def test_no_table_keeps_legacy_single_domain(monkeypatch):
    r = FakeRedis()
    csv_path = _seed(r, table=None)
    assert _run(monkeypatch, r, csv_path) == 0, "单域口径对账应绿（夹具与加载同源）"
    # 结果落无表段键；表域键不得存在
    legacy = r.hgetall(f"sync:recon:{VER}")
    assert legacy and legacy["pass"] == "1"
    assert f"sync:recon:{TABLE}:{VER}" not in r.hashes, "未传 --table 却写了表域键"
    assert set(legacy) == {"pass", "checked_at", "detail"}   # 序列化口径未旁路
    # 模块键空间保持存量静止值（向后兼容红线）
    assert (C.NS, C.ALIAS_KEY, C.VER_META, C.RECON_META) == C.LEGACY_KEYSPACE[:4]


# ======================================================================
# ③ 传 --table：按表键空间对账，结果落 sync:recon:<表>:<版本>
# ======================================================================

def test_with_table_reconciles_per_table_keyspace(monkeypatch):
    r = FakeRedis()
    csv_path = _seed(r, table=TABLE)
    assert _run(monkeypatch, r, csv_path, extra=["--table", TABLE]) == 0, "按表对账应绿（夹具与加载同源）"
    per_table = r.hgetall(f"sync:recon:{TABLE}:{VER}")
    assert per_table and per_table["pass"] == "1", "按表结果未落 sync:recon:<表>:<版本>"
    assert f"sync:recon:{VER}" not in r.hashes, "按表对账却误写单域键"
    detail = json.loads(per_table["detail"])
    assert detail["pass"] is True and detail["version"] == VER
    assert detail["checks"]["row_count"]["rows_redis"] == len(ROWS)   # 查一按表域扫描
    # 表名安全闸沿用 key_domain（非法表名显式拒绝）
    with pytest.raises(ValueError):
        C.key_domain("bad:table:name")


if __name__ == "__main__":
    # 用例依赖 pytest fixture（monkeypatch/tmp_path 语义），直跑不忠实——统一走 pytest：
    raise SystemExit("请用 pytest 跑：python3.13 -m pytest tests/test_reconcile_table_arg.py -q")
