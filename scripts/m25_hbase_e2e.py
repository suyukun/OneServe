#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TASK-M2-5 真链路 e2e（headless）：HBase 元数据 → 前端适配层 → 真试查 → 清理。

判据（AC-4/AC-5 前端侧）：
  1. 真 HBase（开发替身容器，非 MRS）meta → 抓 namespace/表/列族；
  2. node 加载 admin/publish-hbase-adapter.js，normalizeDiscovery 把真 meta＋真 CSV
     归一化为业务域→表→列族→列的目录树，键类型/示例键就位；
  3. 适配层 buildTestPayload 产出请求体（namespace/table/families/fields/keyType/sampleKey）；
     用该请求体对真 HBase 做**真实点查**（happybase row()），断言命中预置列值；
  4. 缺示例值/缺示例键时适配层 payload 语义（sampleKey 空）→ 如实为空（门禁前置）；
  5. 验完清理容器（内存让位纪律）。

【平台前提】平台不自建 HBase、不新增常驻服务；生产＝示例 HBase 集群。本脚本只连开发替身
（compose.hbase.yml，默认不随 up.sh 起），地址走 HBASE_HOST/HBASE_PORT，平台只读。

【M2-4 依赖·如实标注】试查端点的**服务端**（POST /v1/admin/publish/hbase/test）由 M2-4
实现，本卡未合入：本脚本用 happybase 按**适配层产出的同一请求体**直连真 HBase 执行该
点查，证明前端「以示例键真试查」的请求体正确且对真 HBase 可命中；端点联调待 M2-4 合入。

用法：bash scripts/m25_hbase_e2e.sh   （或 python3 scripts/m25_hbase_e2e.py）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
def _resolve_docker() -> str:
    """Docker CLI 定位：DOCKER_BIN > PATH > macOS 应用内全路径（本机 docker 不在 PATH 时的硬路径）。"""
    env = os.environ.get("DOCKER_BIN")
    if env:
        return env
    import shutil
    found = shutil.which("docker")
    if found:
        return found
    mac = "/Applications/Docker.app/Contents/Resources/bin/docker"
    return mac if os.path.exists(mac) else "docker"


DOCKER = _resolve_docker()
COMPOSE_FILES = ["-f", "docker-compose.yml", "-f", "compose.hbase.yml"]
CONTAINER = "oneserve-hbase"
TABLE = "default:oneserve_demo"
EVIDENCE = ROOT / "tests" / "evidence" / "m25-hbase-e2e.txt"
ADAPTER = ROOT / "admin" / "publish-hbase-adapter.js"
FIXTURE = ROOT / "tests" / "fixtures" / "hbase_discovery.real.json"

_lines: list[str] = []


def say(msg: str) -> None:
    print(msg, flush=True)
    _lines.append(msg)


def dump_evidence() -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text("\n".join(_lines) + "\n", encoding="utf-8")
    print(f"[evidence] {EVIDENCE.relative_to(ROOT)}", flush=True)


def docker(*args: str, check: bool = True, capture: bool = False):
    cmd = [DOCKER, *args]
    return subprocess.run(cmd, cwd=str(ROOT), check=check, capture_output=capture, text=True)


def cleanup() -> None:
    """内存让位纪律：删开发替身容器（不 compose down，避免误伤并行主栈）。"""
    docker("rm", "-f", CONTAINER, check=False, capture=True)
    say(f"  ✓ 已清理开发替身容器 {CONTAINER}（内存让位）")


def node(script: str) -> str:
    full = "require(" + json.dumps(str(ADAPTER)) + ");\n" + script
    r = subprocess.run(["node", "-e", full], cwd=str(ROOT), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("node 失败：" + r.stderr)
    return r.stdout.strip()


def main() -> int:
    say(f"===== TASK-M2-5 真链路 e2e {datetime.now().strftime('%F %T')} =====")
    # ── 共享 HBase 破坏闸（9-28 事故后立规）──────────────────────────────
    # 本脚本 compose 起共享容器名 oneserve-hbase（挂共享卷），且 [2] 会
    # disable+delete+recreate default:oneserve_demo——直打共享即清共享表（9-28 事故
    # 实证）。默认拒绝；须显式 E2E_ALLOW_SHARED=1 放行（oneserve_demo 为独立表名，
    # 不碰 usr_basic_info/usr_asset_tag/card_acct_summary）。
    if os.environ.get("E2E_ALLOW_SHARED") != "1":
        say("✗ 默认拒绝：本脚本起共享 oneserve-hbase 且会重建 default:oneserve_demo 表，")
        say("  有覆盖共享数据风险（9-28 事故实证）。放行：E2E_ALLOW_SHARED=1 python3 "
            "scripts/m25_hbase_e2e.py（仅动独立表 oneserve_demo，不碰业务表）")
        dump_evidence()
        return 2
    say("[共享闸放行] E2E_ALLOW_SHARED=1 → 允许在共享容器上重建 oneserve_demo 表（自担风险）")
    cleanup()   # 清可能的残留，保证冷起

    # 1) 起开发替身容器（独立 override；HBASE_HEAPSIZE 内存限制见 compose.hbase.yml）
    say("[1] 起 HBase 开发替身容器（compose.hbase.yml override）…")
    docker("compose", *COMPOSE_FILES, "up", "-d", "hbase")
    ready = False
    for _ in range(45):
        if docker("exec", CONTAINER, "bash", "-c", "echo > /dev/tcp/127.0.0.1/9090",
                  check=False, capture=True).returncode == 0:
            ready = True
            break
        import time
        time.sleep(2)
    if not ready:
        raise SystemExit("容器内 9090 未就绪")
    say("  ✓ 容器内 Thrift 9090 就绪")

    try:
        # 2) 造数（演示数据；写入方在生产归数仓，本处仅为 e2e 预置样例）
        import happybase
        sys.path.insert(0, str(ROOT / "service"))
        from app.kv_executor import sm3_hex
        host = os.environ.get("HBASE_HOST", "127.0.0.1")
        port = int(os.environ.get("HBASE_PORT", "9090"))
        conn = happybase.Connection(host=host, port=port, timeout=30000)
        if TABLE in [t.decode() for t in conn.tables()]:
            conn.disable_table(TABLE); conn.delete_table(TABLE)
        conn.create_table(TABLE, {"cf": {"max_versions": 1}})
        key = sm3_hex("13800138000")
        # 覆盖 fixture 前 3 列（字段字典按序取前 3），保证「请求字段全部命中真行」可断言
        row = {b"cf:ac_active_sts_01": b"band_C", b"cf:ac_active_sts_02": b"v2",
               b"cf:ac_active_sts_03": b"v3", b"cf:if_acct_fct_24": b"523877"}
        conn.table(TABLE).put(key.encode(), row)
        say(f"[2] 真 HBase 造数：{TABLE} rowkey=SM3(13800138000)={key[:12]}… 列数={len(row)}")

        # 3) 真 meta → fixture（namespace/表/列族）
        subprocess.run([sys.executable, str(ROOT / "scripts" / "m25_hbase_fixture.py")],
                       cwd=str(ROOT), check=True, capture_output=True)
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        say(f"[3] 真 meta 抓取：namespaces={sorted({t['namespace'] for d in fixture['domains'] for t in d['tables']})} "
            f"tables={sum(len(d['tables']) for d in fixture['domains'])}")

        # 4) 适配层归一化真 meta → 目录树
        out = json.loads(node("""
var v = O1HBase.normalizeDiscovery(JSON.parse(%s));
var tables = O1HBase.allTables(v.domains);
console.log(JSON.stringify({domains: v.domains.length, tables: tables.length,
  tree: tables.map(function (t) { return {path: t.path, cf: t.families.map(function (f) { return f.name; }),
    cols: t.columnCount, keyTypes: t.keyTypes, sample: O1HBase.sampleKeyFor(t, 'phone_sm3').slice(0, 12)}; }),
  dual: v.dualSource.status}));
""" % json.dumps(json.dumps(fixture))))
        assert out["domains"] >= 1 and out["tables"] >= 1, out
        t0 = out["tree"][0]
        say(f"[4] 适配层归一化：业务域={out['domains']} 表={out['tables']} · {t0['path']} "
            f"列族={t0['cf']} 列={t0['cols']} 键类型={t0['keyTypes']} 示例键={t0['sample']}… 双源={out['dual']}")
        assert t0["cf"] == ["cf"] and t0["cols"] >= 1, t0

        # 5) 适配层构造试查请求体 → 用该请求体对真 HBase 真实点查（M2-4 端点未合入，此处直连等价）
        payload = json.loads(node("""
var v = O1HBase.normalizeDiscovery(JSON.parse(%s));
var t = O1HBase.allTables(v.domains)[0];
var cols = O1HBase.flattenFields(t).slice(0, 3).map(function (c) {
  return {name: c.name, cf: c.cf, sample: 'x', desc: c.cnName}; });
console.log(JSON.stringify(O1HBase.buildTestPayload({table: t, fields: cols,
  keyType: 'phone_sm3', sampleKey: O1HBase.sampleKeyFor(t, 'phone_sm3')})));
""" % json.dumps(json.dumps(fixture))))
        say(f"[5] 适配层试查请求体：namespace={payload['namespace']} table={payload['table']} "
            f"families={payload['families']} keyType={payload['keyType']} fields={[f['name'] for f in payload['fields']]}")
        assert payload["namespace"] and payload["table"] and payload["keyType"] == "phone_sm3"
        got = conn.table(payload["namespace"] + ":" + payload["table"]).row(payload["sampleKey"].encode())
        got = {k.decode().split(":", 1)[-1]: v.decode() for k, v in got.items()}
        assert got, "真 HBase 点查未命中（示例键应命中预置样例）"
        say(f"  ✓ 真 HBase 点查命中：{got}")
        missing = [f["name"] for f in payload["fields"] if f["name"] not in got]
        say(f"  · 请求字段 {len(payload['fields']) - len(missing)}/{len(payload['fields'])} 命中真行")
        assert not missing, f"请求字段未全部命中真行：{missing}"

        # 6) 门禁前置：缺示例键 → payload.sampleKey 为空（如实，不放行）
        no_key = json.loads(node("""
var v = O1HBase.normalizeDiscovery(JSON.parse(%s));
var t = O1HBase.allTables(v.domains)[0];
t.keySamples = {};
console.log(JSON.stringify({sample: O1HBase.sampleKeyFor(t, 'phone_sm3')}));
""" % json.dumps(json.dumps(fixture))))
        assert no_key["sample"] == "", no_key
        say("[6] 门禁前置：清空示例键 → sampleKey 为空（如实不放行，不编造）")

        # 7) 不可达如实报错（不回落 mock）
        err = json.loads(node("""
console.log(JSON.stringify(O1HBase.normalizeError({status: 503, message: '连接被拒'})));
"""))
        assert err["fallback"] is False and err["code"] == "HBASE_UNREACHABLE"
        say(f"[7] 不可达如实报错：code={err['code']} title={err['title']} fallback={err['fallback']}")
        conn.close()
    finally:
        cleanup()

    say("===== e2e 全绿（真 HBase → 适配层 → 真试查） =====")
    dump_evidence()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
