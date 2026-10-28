# -*- coding: utf-8 -*-
"""S6 M2 端到端＋冷热加载场景（TASK-M2-12 收尾卡）。

架构口径（ADR-0005 / docs/plans/M2范围裁定_v2-架构纠正_20260923.md）：
  HBase＝冷存储，Redis＝热存储；链路＝HBase 选表 → 加载到 Redis → 从 Redis 发 API。
  本场景把 M2 新能力（加载端点／同源对账／双缓冲切指针）纳入 testkit 回归体系，
  与 s0~s5 同风格（机检清单 checks.txt＋run.json＋report.md，任一红即场景退出 1）。

与 s0~s5 的差异（照 s5 的步进机检风格扩展）：
  - 交互面＝隔离 M2 环境的 HTTP 黑盒（发布台发现／加载端点＋经网关真调）；
    环境由 scripts/m212_testkit_e2e.sh 以 COMPOSE_PROJECT_NAME=oneserve-m212 起好，
    容器名/Redis 端口均隔离，绝不写共享栈 oneserve-redis。
  - **两个非 HTTP 步**：既有 sync/reconcile.py（源对账，写 sync:recon:<版本>.pass）
    与 sync/switch.py（真切换器，含对账三闸放行）——**直接执行既有 CLI 真件**，
    不 import、不复制其逻辑（与 driver 调 git 同类：仅编排真件，绝不作第二实现）。

六步机检（Checks 逐条落判据，任一红＝场景退出码 1）：
  ① 选表：GET /v1/admin/publish/hbase/discovery（真 meta×字典）→ 表/列族/列数；
  ② 加载＋对账：POST /v1/admin/publish/hbase/load（HBase 冷 → Redis 热）→
     ok／loaded>0／dataAsOf 非空／reconcile.pass 且 diffCount=0（行数双向全等、
     抽样逐值全等、覆盖率偏差达标）＝**对账差异=0 判定**；
  ③ 加载幂等：重跑不带 replace → 409 LOAD_CONFLICT（零副作用）；带 replace →
     200 且 clearedBeforeLoad==loaded（清空后重写，行数稳定）；
  ④ 上线真件：sync/reconcile.py（源指纹一致＋三查绿）→ sync/switch.py 原子切
     tags:user:latest（对账三闸放行）；
  ⑤ 经网关真调：POST <route>（→ /v1/tags/query）→ 200／found／live_version=版本／
     data_as_of 非空；**投影白名单**（fields ⊆ 授权白名单，且命中行的越权列零出现）
     ＋**脱敏**（if_acct_fct_24 ∈ A/B/C/U，CSV 原值零出现）在同链路上仍成立；
  ⑥ 失败态：docker rm -f HBase → 加载端点如实 503（HBASE_UNAVAILABLE）且目标空间
     零写入（不写脏、不回退假数据）。

如实标注：真集群（NC-M2-9）未做——本场景跑在开发替身容器（compose.hbase.yml）上；
真集群首灌/生产加载未验。
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

from scenarios.batch import Checks, NL, Red   # 复用 s5 机检清单（唯一一份实现）

# demo_bank × user_tag_query 授权白名单（service/auth_matrix.yaml 登记值；
# 越权字段零出现的判据口径）。用字面量＝与 s5 PROBE_FIELDS 同款：预期集是测试预言。
GRANT = {
    "ac_active_sts_01", "ac_active_sts_02", "ac_bussi_sts_01", "ac_login_fct_01",
    "ac_login_sts_02", "ac_point_sts_05", "ac_vist_sts_01", "mk_touch_sts_02",
    "if_acct_fct_24", "if_bse_fct_04",
}
BANDS = {"A", "B", "C", "U"}            # band_mask 脱敏档位（unknown_out=U）
DESENS_FIELD = "if_acct_fct_24"


def _hdr(*pairs: tuple[str, str]) -> dict:
    return {k: v for k, v in pairs}


def _post_json(s: requests.Session, url: str, headers: dict, body: dict,
               timeout: float = 120.0) -> tuple[int, dict]:
    r = s.post(url, json=body, headers=headers, timeout=timeout)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"_raw": r.text[:200]}


def _sample_key(csv_path: str) -> tuple[str, str, str]:
    """造数 CSV 里取「脱敏列非空」的首行：返回 (键, 脱敏列原值, 越权列名)。

    越权列名＝该行非空但不在授权白名单内的真实列，用作「越权字段零出现」的强预言。
    """
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            raw = (row.get(DESENS_FIELD) or "").strip()
            if not raw:
                continue
            leak_col = next((c for c, v in row.items()
                             if c != "usr_key_sm3" and v and c not in GRANT), None)
            if leak_col:
                return row["usr_key_sm3"], raw, leak_col
    raise Red(f"造数 CSV {csv_path} 找不到「{DESENS_FIELD} 非空且含越权列」的样本行")


def _run_sync_cli(repo: Path, script: str, args: list[str], redis_url: str,
                  timeout: float = 180.0) -> subprocess.CompletedProcess:
    """执行既有 sync CLI 真件（reconcile/switch）；不 import、不复制逻辑。"""
    env = dict(os.environ, SYNC_REDIS_URL=redis_url)
    return subprocess.run([sys.executable, str(repo / "sync" / script), *args],
                          capture_output=True, text=True, env=env,
                          cwd=str(repo), timeout=timeout)


def run_scenario(*, svc: str, gw: str, route: str, apikey: str, admin_token: str,
                 version: str, redis_url: str, source_csv: str, table: str, cf: str,
                 docker_bin: str, hbase_ct: str, repo: Path,
                 artifacts_dir: Path | None) -> tuple[int, list[str]]:
    """跑六步机检；返回 (退出码, warns)。判据红以 Red 抛出（driver 捕获落报告）。"""
    ck = Checks()
    s = requests.Session()
    warns: list[str] = []
    admin = _hdr(("X-Admin-Token", admin_token), ("Content-Type", "application/json"))
    auth = _hdr(("apikey", apikey), ("Content-Type", "application/json"))
    key0, raw0, leak_col = _sample_key(source_csv)
    ctr_csv = _ctr_csv(source_csv)     # 容器内路径（service 挂载 /sync；load 端点 sourceCsv 用）
    print(f"[S6] 环境 svc={svc} gw={gw} route={route} 版本={version} 键={len(load_keys_count(source_csv))} "
          f"样本={key0[:16]}…（越权预言列={leak_col}）")
    try:
        # ---------- ① 选表（真 meta×字典） ----------
        print("[S6-①] 选表：GET /v1/admin/publish/hbase/discovery")
        r = s.get(f"{svc}/v1/admin/publish/hbase/discovery", headers=admin, timeout=60.0)
        ck.add("① 发现端点 200", r.status_code == 200, f"code={r.status_code}")
        disc = r.json()
        tbls = {t["id"]: t for dom in disc.get("domains", []) for t in dom.get("tables", [])}
        tid = table if table in tbls else ("default:" + table if ("default:" + table) in tbls else table)
        ck.add(f"① 目录树含表 {table}", tid in tbls, f"tables={sorted(tbls)[:5]}")
        fams = {f["name"]: f for f in tbls[tid].get("families", [])}
        ck.add(f"① 列族 {cf} 存在", cf in fams, f"families={sorted(fams)}")
        n_cols = len(fams.get(cf, {}).get("columns", []))
        ck.add("① 列数≥296（宽表 297 列域）", n_cols >= 296, f"columns={n_cols}")

        # ---------- ② 加载 HBase→Redis ＋ 对账差异=0 ----------
        print("[S6-②] 加载：POST /v1/admin/publish/hbase/load（HBase 冷 → Redis 热）")
        load_body = {"namespace": "default", "table": table, "cf": cf,
                     "version": version, "sourceCsv": ctr_csv}
        code, body = _post_json(s, f"{svc}/v1/admin/publish/hbase/load", admin, load_body)
        ck.add("② 加载 200", code == 200 and body.get("ok") is True,
               f"code={code} error={body.get('error')}")
        ck.add("② 模式＝hbase->redis-load", body.get("mode") == "hbase->redis-load",
               str(body.get("mode")))
        loaded = body.get("loaded") or 0
        ck.add("② 加载键数>0", loaded > 0, f"loaded={loaded}")
        ck.add("② dataAsOf 非空（时点诚实）", bool(body.get("dataAsOf")), str(body.get("dataAsOf")))
        rc = body.get("reconcile") or {}
        ck.add("② 同源对账 pass＋差异=0", rc.get("pass") is True and rc.get("diffCount") == 0,
               f"pass={rc.get('pass')} diffCount={rc.get('diffCount')} err={rc.get('error', '')}")
        checks = rc.get("checks") or {}
        crow = checks.get("row_count") or {}
        ck.add("② 对账行数双向全等（Redis==HBase==loaded）",
               crow.get("rows_redis") == crow.get("rows_hbase") == loaded,
               f"redis={crow.get('rows_redis')} hbase={crow.get('rows_hbase')} loaded={loaded}")
        csam = checks.get("value_sample") or {}
        ck.add("② 对账抽样逐值全等（1000 条，错 1 即红）",
               csam.get("pass") is True and csam.get("mismatch") == 0,
               f"sampled={csam.get('sampled')} mismatch={csam.get('mismatch')}")
        ccov = checks.get("coverage") or {}
        ck.add("② 对账覆盖率偏差达标（≤20%）", ccov.get("pass") is True,
               f"deviation={ccov.get('deviation')} threshold={ccov.get('threshold')}")

        # ---------- ③ 加载幂等 ----------
        print("[S6-③] 加载幂等：重跑不带 replace → 409；带 replace → 清空重写")
        code2, body2 = _post_json(s, f"{svc}/v1/admin/publish/hbase/load", admin, load_body)
        ck.add("③ 非空空间未 replace → 409 LOAD_CONFLICT（零副作用）",
               code2 == 409 and body2.get("error") == "LOAD_CONFLICT",
               f"code={code2} error={body2.get('error')} msg={str(body2.get('message'))[:80]}")
        repl = dict(load_body, replace=True)
        code3, body3 = _post_json(s, f"{svc}/v1/admin/publish/hbase/load", admin, repl)
        ck.add("③ 带 replace 重灌 200", code3 == 200 and body3.get("ok") is True,
               f"code={code3} error={body3.get('error')}")
        ck.add("③ replace 清空后重写＝行数稳定（幂等可重跑）",
               body3.get("clearedBeforeLoad") == body3.get("loaded") == loaded,
               f"cleared={body3.get('clearedBeforeLoad')} loaded={body3.get('loaded')} 首灌={loaded}")
        rc3 = body3.get("reconcile") or {}
        ck.add("③ 重灌后对账仍差异=0",
               rc3.get("pass") is True and rc3.get("diffCount") == 0,
               f"pass={rc3.get('pass')} diffCount={rc3.get('diffCount')}")

        # ---------- ④ 上线真件（源对账＋原子切指针） ----------
        print("[S6-④] 上线：既有 sync/reconcile.py（源对账）→ sync/switch.py（对账三闸）")
        pr = _run_sync_cli(repo, "reconcile.py", ["--csv", str(repo / source_csv), "--version", version], redis_url)
        ck.add("④ 源对账绿（既有 reconcile.py，指纹一致＋三查）", pr.returncode == 0,
               f"rc={pr.returncode} out={pr.stdout.strip()[:120] or pr.stderr.strip()[:120]}")
        if pr.returncode == 0:
            rep = json.loads(pr.stdout.strip().splitlines()[-1])
            ck.add("④ 源对账 pass=True", rep.get("pass") is True, str(rep.get("checks", {}))[:120])
        ps = _run_sync_cli(repo, "switch.py", ["--version", version], redis_url)
        ck.add("④ 原子切指针 ok（既有 switch.py，三闸放行）", ps.returncode == 0,
               f"rc={ps.returncode} out={ps.stdout.strip()[:120] or ps.stderr.strip()[:120]}")
        if ps.returncode == 0:
            sw = json.loads(ps.stdout.strip().splitlines()[-1])
            ck.add("④ 指针指向新版本（tags:user:latest）",
                   sw.get("ok") is True and sw.get("to") == version,
                   f"from={sw.get('from')} to={sw.get('to')}")

        # ---------- ⑤ 经网关真调：投影白名单＋脱敏 ----------
        print("[S6-⑤] 经网关真调：POST " + route + "（→ /v1/tags/query）")
        r5 = s.post(f"{gw}{route}", json={"key": key0}, headers=auth, timeout=30.0)
        ck.add("⑤ 网关 200", r5.status_code == 200, f"code={r5.status_code} body={r5.text[:160]}")
        d = r5.json()
        ck.add("⑤ found=True（Redis 热存储有真数据）", d.get("found") is True, f"found={d.get('found')}")
        ck.add("⑤ live_version=新版本（切指针生效）", d.get("live_version") == version,
               f"live_version={d.get('live_version')} expect={version}")
        ck.add("⑤ data_as_of 非空（时点诚实）", bool(d.get("data_as_of")), str(d.get("data_as_of")))
        fields = d.get("fields") or {}
        ck.add("⑤ 投影贯通有实出（fields 非空）", bool(fields), f"fields={json.dumps(fields, ensure_ascii=False)}")
        over = sorted(set(fields) - GRANT)
        ck.add("⑤ 投影白名单：越权字段零出现（fields ⊆ 授权白名单）",
               not over and leak_col not in fields,
               f"越权={over} 预言列={leak_col}/{leak_col in fields}")
        if DESENS_FIELD in fields:
            val = fields.get(DESENS_FIELD)
            ck.add(f"⑤ 脱敏：{DESENS_FIELD} ∈ A/B/C/U（原值零出现）",
                   val in BANDS and val != raw0, f"值={val!r} CSV原值={raw0!r}")
        else:
            ck.add(f"⑤ 脱敏：{DESENS_FIELD} 命中行必带（样本键该列非空）", False,
                   "响应投影缺该列——样本键选取或投影口径异常")

        # ---------- ⑥ 失败态：HBase 不可达如实报错＋零写入 ----------
        print("[S6-⑥] 失败态：docker rm -f " + hbase_ct + " → 加载端点如实 503＋零写入")
        fail_ver = _next_version(version)
        subprocess.run([docker_bin, "rm", "-f", hbase_ct], capture_output=True, text=True, timeout=60)
        time.sleep(2)
        code6, body6 = _post_json(s, f"{svc}/v1/admin/publish/hbase/load", admin,
                                  {"namespace": "default", "table": table, "cf": cf,
                                   "version": fail_ver, "replace": True})
        ck.add("⑥ HBase 不可达 → 503 如实报错（HBASE_UNAVAILABLE）",
               code6 == 503 and body6.get("error") == "HBASE_UNAVAILABLE",
               f"code={code6} error={body6.get('error')} msg={str(body6.get('message'))[:100]}")
        ck.add("⑥ 声明不回退假数据（fallback=无）",
               "无" in str(body6.get("fallback", "")), str(body6.get("fallback")))
        n_written = _count_space(redis_url, fail_ver)
        ck.add("⑥ 不可达时目标空间零写入（不写脏）", n_written == 0,
               f"tags:user:{fail_ver}:* 键数={n_written}")
    finally:
        s.close()
        # 机检清单落盘放 finally：红跑同样留痕（s5 同款纪律）
        if artifacts_dir is not None:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / "checks.txt").write_text(NL.join(ck.lines) + NL, encoding="utf-8")
    print(f"[S6] {ck.table()}")
    for w in warns:
        print(f"[S6][warn] {w}")
    return 0, warns


def _ctr_csv(source_csv: str) -> str:
    """宿主相对路径 → 容器内路径（sync 只读挂载在 /sync）。"""
    s = source_csv.replace("\\", "/")
    i = s.find("sync/")
    return "/sync/" + s[i + len("sync/"):] if i >= 0 else s


def _count_space(redis_url: str, version: str) -> int:
    """数隔离 Redis 某版本空间键数（零写入断言；只读，不写共享栈）。"""
    import redis as redis_lib
    r = redis_lib.Redis.from_url(redis_url, decode_responses=True)
    try:
        n, cur = 0, 0
        while True:
            cur, batch = r.scan(cursor=cur, match=f"tags:user:{version}:*", count=1000)
            n += len(batch)
            if cur == 0:
                return n
    finally:
        r.close()


def load_keys_count(csv_path: str) -> list:
    """仅用于打印行数（不复用键池实现，避免依赖 locust）。"""
    with open(csv_path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _next_version(version: str) -> str:
    """失败态用独立版本空间（YYYYMMDD +1 天），避免与已加载空间同名。"""
    from datetime import date, timedelta
    d = date(int(version[:4]), int(version[4:6]), int(version[6:8])) + timedelta(days=1)
    return d.strftime("%Y%m%d")
