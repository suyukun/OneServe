# -*- coding: utf-8 -*-
"""同步链路公共件：Redis 连接、键规则、meta 指针解析、读取方模拟、审计日志。

所有脚本（loader/reconcile/switch/rollback/delete_apply/read_user）共用此模块，
保证键规则与读取口径只有一份实现（单一事实来源）。
"""
import hashlib
import json
import random
from datetime import datetime, timezone

import redis as redis_lib

import sync_config as C


def now_iso():
    """当前时刻 ISO 字符串（含时区，审计用）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def redis_conn(url=None):
    """建 Redis 连接；失败即抛（各脚本 main 捕获后非零退出）。"""
    r = redis_lib.Redis.from_url(url or C.REDIS_URL, decode_responses=True)
    r.ping()  # 显式 ping，把连接问题前置成清晰错误
    return r


def data_key(version, sm3):
    """数据键：tags:<表>:<版本>:<sm3>（表域由 C.NS 当前值决定，缺省 user＝存量单域）。"""
    return f"{C.NS}:{version}:{sm3}"


def scan_keys(r, pattern):
    """SCAN 游标迭代收集键（禁 KEYS：不阻塞线上）。"""
    out = []
    cur = 0
    while True:
        cur, batch = r.scan(cursor=cur, match=pattern, count=1000)
        out.extend(batch)
        if cur == 0:
            return out


def count_keys(r, pattern):
    """按模式数键（SCAN 实现）。"""
    return len(scan_keys(r, pattern))


def list_versions(r):
    """现存版本空间清单：从数据键平面扫描提取（以数据面为准，不信 meta 残留）。"""
    vers = set()
    for k in scan_keys(r, f"{C.NS}:*"):
        parts = k.split(":")
        if len(parts) == 4:  # tags:<表>:<版本>:<sm3>（裸表名无冒号，段数恒 4）
            vers.add(parts[2])
    return sorted(vers)


def get_live(r):
    """读 meta 指针取当前线上版本；无指针（未上线）返回 None。"""
    return r.get(C.ALIAS_KEY)


def ver_meta_get(r, version):
    """读版本元信息 hash；不存在返回 None。"""
    return r.hgetall(C.VER_META.format(version=version)) or None


def ver_meta_set(r, version, **fields):
    """写版本元信息 hash（覆盖式）。"""
    r.delete(C.VER_META.format(version=version))
    if fields:
        r.hset(C.VER_META.format(version=version), mapping=fields)


def recon_meta_fields(ok, detail):
    """对账结果 hash 的字段集（序列化口径**唯一实现**，P0-2 收口）。

    命令行入口（sync/reconcile.py）与 HTTP 端点（service/app/hbase_load.py）共用
    本函数落 sync:recon:<版本>——两路写出的字段名/值形态逐字段一致，switch.py 第二闸
    （pass == "1"）只认这一种结构；禁止任何一处自拼第二份字段。
    """
    return {
        "pass": "1" if ok else "0",
        "checked_at": now_iso(),
        "detail": json.dumps(detail, ensure_ascii=False),
    }


def recon_meta_set(r, version, ok, detail):
    """写对账结果 hash sync:recon:<版本>（pass/checked_at/detail；字段口径见 recon_meta_fields）。"""
    r.hset(C.RECON_META.format(version=version), mapping=recon_meta_fields(ok, detail))


def read_user(r, sm3):
    """读取方模拟（线上点查口径）：先读 meta 指针 → 版本，再读数据键。

    返回 dict：{found, live_version, data_as_of, n_tags, preview}
    - found=False 表示无该用户或空标签 → 对外即「返回 null」（SPEC §4.3 时点诚实）。
    - data_as_of 来自版本元信息（数据日期），指针一切换读数即随新版本变化。
    """
    ver = get_live(r)
    if ver is None:
        return {"found": False, "live_version": None, "data_as_of": None,
                "n_tags": 0, "preview": {}}
    meta = ver_meta_get(r, ver) or {}
    raw = r.get(data_key(ver, sm3))
    if raw is None:
        return {"found": False, "live_version": ver,
                "data_as_of": meta.get("data_as_of"), "n_tags": 0, "preview": {}}
    tags = json.loads(raw)
    preview = dict(sorted(tags.items())[:3])  # 证据展示用前 3 个标签
    return {"found": True, "live_version": ver,
            "data_as_of": meta.get("data_as_of"), "n_tags": len(tags), "preview": preview}


def sample_indices(n_rows, size=None, seed=None):
    """对账抽样行号（确定性）：reconcile 与注入演练共用同一实现，保证口径一致。"""
    size = C.RECON_SAMPLE_SIZE if size is None else size
    seed = C.RECON_SAMPLE_SEED if seed is None else seed
    return sorted(random.Random(seed).sample(range(n_rows), min(size, n_rows)))


def sha256_file(path):
    """文件 SHA256（对账源指纹，防拿错文件对账）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def audit_append(r, key, entry):
    """追加审计条目（LPUSH JSON，最新在前）。"""
    r.lpush(key, json.dumps(entry, ensure_ascii=False))


def load_schema():
    """读表结构蓝本：[(字段名, 数据类型), …]，按 attnum 排序，共 297 列（含首列 usr_id）。"""
    import csv
    with open(C.SCHEMA_CSV, encoding="utf-8") as f:
        rows = [(r["字段名"], r["数据类型"]) for r in csv.DictReader(f)]
    if len(rows) != 297:
        raise RuntimeError(f"表结构蓝本应 297 列，实得 {len(rows)}")
    return rows


def tag_columns():
    """标签列清单＝297 列去掉键列 usr_id（296 列；task/ds 等分区列按报告上界口径一并计入标签域）。"""
    return [name for name, _ in load_schema() if name != "usr_id"]
