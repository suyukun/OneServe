# -*- coding: utf-8 -*-
"""异常与批量监控纯逻辑（P1-6 · 前台重造 SPEC §5-P1-6／§2-B7）——不 import FastAPI/redis。

职责（决策全在本模块，monitor_api.py／portal_api.py 只做 HTTP 编排；分层同 my_access）：

  ① 异常列表（异常真源＝审计 JSONL，与 P1-1 audit_query 同文件同口径）：
     筛出失败记录（4xx/5xx；2xx/3xx 非异常），可按调用方／API（endpoint）下钻，
     带错误样本（参数摘要／响应摘要，明文一律不落）与 trace 详情入口（trace_id 直链）；
  ② 下钻聚合（F5「哪些 API 失败率超阈值」）：同一遍扫描同时算
     按调用方／按 API 的总量、异常数、异常率（小基数不出比率，同 P1-1 口径）；
  ③ trace 详情：审计八元组按 trace_id 精确反查（服务侧审计＋批量任务级审计两文件）；
  ④ 批量运行视角（数据真源＝JobStore 的 Redis 任务结构，见 batch.py）：
     队列深度（LLEN）／执行中（running＋cancelling）／五态计数／积压（最长等待秒数）
     ——全部由真实任务状态算出，不造数；未接入项如实标注（见 BATCH_CALIBER）。

如实口径（本卡红线）：
  - 无异常记录＝空列表＋total=0，页面如实「暂无异常」，绝不填假样本；
  - 队列项无任务记录（滞留）＝如实计数并标注，不静默吞掉；
  - 积压「等待时长」只在有 queued 任务时给出；无 queued＝None（不是 0 秒）；
  - 磁盘/Redis 读失败＝降级为空/零并附 degraded 说明，不伪造成功。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app import appkeys as appk
from app import batch as batch_mod
from app import my_access as mya

# ── 异常口径（与 my_access.AUDIT_* 同源常量） ──
ERROR_CLASSES = ("4xx", "5xx")           # 异常＝4xx/5xx（2xx 成功、3xx 重定向均不计异常）
ERROR_DEFAULT_LIMIT = 30                 # 异常列表默认页大小
ERROR_MAX_LIMIT = 100                    # 页大小上限（端点层 clamp）
ERROR_SAMPLE_LIMIT = 5                   # 「错误样本」（列表内可展开的样本条数）
ERROR_TOP_N = 5                          # 下钻榜（按调用方／按 API）条数
MAX_DIM_KEYS = 500                       # 下钻维度键上限（超限并入 other，如实标注 dimTruncated）
DIM_OTHER = "（其余）"

# 掩码／时间／状态匹配一律复用 my_access 同一实现——「异常列表口径要与审计页一致」的落点
_mask_caller = mya._mask_caller
_parse_ts = mya._parse_ts
_norm_ts_bound = mya._norm_ts_bound
_match_status = mya._match_status


# ---------- ① 异常列表＋② 下钻聚合（单遍流式扫审计 JSONL） ----------

def _error_status(status) -> bool:
    """异常判定：4xx 或 5xx（脏状态/非整型＝不计异常，也不计非异常）。"""
    try:
        s = int(status)
    except (TypeError, ValueError):
        return False
    return 400 <= s < 600


def _error_class(status) -> str:
    """状态码 → 异常类（4xx/5xx）；非异常返回空串。"""
    return "" if not _error_status(status) else ("5xx" if int(status) >= 500 else "4xx")


def drill_caller(caller: str) -> str:
    """掩码 → 可下钻前缀（审计 caller 过滤是**子串**匹配，掩码尾部的 ****/… 匹配不上）。

    取 _mask_caller 保留的可辨识头部（sb_/ak_ 凭证＝前 7 位，其余标识＝前 12 位）——
    该前缀必是原始标识的真前缀，故按它下钻能命中；代价是同时命中同前缀的其它调用方
    （与审计页「部分匹配」同口径，页面就地注明「按前缀下钻」）。
    """
    c = caller or ""
    if not c:
        return ""
    if c.startswith((appk.SB_PREFIX, appk.AK_PREFIX)):
        return c[:7]
    return c[:12]


def _dim_view(bucket: dict, total_calls: int, *, rate_sample_min: int) -> dict:
    """单个下钻维度 → 出参（异常率小基数不出比率，同 P1-1 口径）。"""
    errors = bucket["errors"]
    return {
        "total": total_calls,
        "errors": errors,
        "errorRate": (round(errors * 100.0 / total_calls, 1)
                      if total_calls >= rate_sample_min else None),
        "rateSampleMin": rate_sample_min,
        "byClass": dict(bucket["byClass"]),
        "topStatus": _top_status(bucket["byStatus"]),
    }


def _bump(bucket: dict, status, *, is_error: bool) -> None:
    """累加一个维度桶：总量／异常数／异常类／状态码分布。"""
    bucket["total"] += 1
    if not is_error:
        return
    bucket["errors"] += 1
    cls = _error_class(status)
    bucket["byClass"][cls] = bucket["byClass"].get(cls, 0) + 1
    key = str(status)
    bucket["byStatus"][key] = bucket["byStatus"].get(key, 0) + 1


def _new_bucket() -> dict:
    return {"total": 0, "errors": 0, "byClass": {}, "byStatus": {}}


def _top_status(by_status: dict) -> int | None:
    """该维度出现最多的错误状态码（次数降序、码值升序）；无错误＝None。"""
    if not by_status:
        return None
    key = min(by_status.items(), key=lambda kv: (-kv[1], int(kv[0])))[0]
    try:
        return int(key)
    except ValueError:
        return None


def error_query(audit_path: Path, *, ts_from: str = "", ts_to: str = "",
                caller: str = "", endpoint: str = "", error_class: str = "",
                status: str = "", trace_id: str = "",
                limit: int = ERROR_DEFAULT_LIMIT, offset: int = 0) -> dict:
    """异常列表＋下钻聚合：单遍流式扫审计 JSONL（八元组真源，与 P1-1 同文件同读法）。

    条件（全部 AND；时间/状态非法抛 ValueError，端点层就近 400）：
      ts_from/ts_to 时间窗（含端闭，纯日期按东八区全天——同 audit_query）；
      caller 部分匹配（掩码出参）／endpoint 部分匹配／status 类或精确码／trace_id 精确；
      error_class ∈ {"", "4xx", "5xx"}——空＝4xx/5xx 全收（本卡默认异常口径）。

    返回 {items,total,limit,offset,summary,byCaller,byApi,dimTruncated}：
      items    异常行（新在前，掩码 caller；携 paramsDigest/responseDigest＝错误样本来源，
               明文参数与响应体永不落盘）；
      total    异常行总数（与翻页无关）；
      summary  窗口内总量／异常数／异常率（小基数 None）／异常类构成／状态码 Top；
      byCaller/byApi 下钻榜（按异常数降序，ERROR_TOP_N 条；含总量与异常率）；
      dimTruncated 维度键超上限＝true（其余并入「（其余）」，不静默丢）。
    """
    ec = (error_class or "").strip()
    if ec and ec not in ERROR_CLASSES:
        raise ValueError(f"异常类不认识：{ec}（可用 4xx/5xx，留空＝两类全收）")
    sf = (status or "").strip()
    if sf and not (sf in ("2xx", "3xx", "4xx", "5xx") or sf.isdigit()):
        raise ValueError(f"状态条件不认识：{sf}（可用 2xx/4xx/5xx 或具体状态码如 403）")
    lo = _norm_ts_bound(ts_from) if ts_from.strip() else None
    hi = _norm_ts_bound(ts_to, day_end=True) if ts_to.strip() else None
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("时间范围反了：起始时间晚于结束时间")
    caller_q, endpoint_q, trace_q = caller.strip(), endpoint.strip(), trace_id.strip()
    limit = min(max(int(limit), 1), ERROR_MAX_LIMIT)
    offset = max(int(offset), 0)

    window: deque[dict] = deque(maxlen=offset + limit)   # 异常行尾窗（文件序旧在前）
    total = err_total = 0
    by_class = {"4xx": 0, "5xx": 0}
    codes: dict[str, int] = {}
    callers: dict[str, dict] = {}
    apis: dict[str, dict] = {}
    dim_truncated = False

    def _bucket(table: dict, key: str) -> dict:
        nonlocal dim_truncated
        b = table.get(key)
        if b is None:
            if len(table) >= MAX_DIM_KEYS:
                dim_truncated = True
                b = table.setdefault(DIM_OTHER, _new_bucket())
            else:
                b = table[key] = _new_bucket()
        return b

    p = Path(audit_path)
    if p.is_file():
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if caller_q and caller_q not in str(row.get("caller") or ""):
                    continue
                if endpoint_q and endpoint_q not in str(row.get("endpoint") or ""):
                    continue
                if trace_q and str(row.get("trace_id") or "") != trace_q:
                    continue
                ts = _parse_ts(str(row.get("ts") or ""))
                if lo is not None and ts < lo:
                    continue
                if hi is not None and ts > hi:
                    continue
                raw_status = row.get("status")
                is_err = _error_status(raw_status)
                if ec and (not is_err or _error_class(raw_status) != ec):
                    continue
                if sf and not _match_status(raw_status, sf):
                    continue
                # 通过状态条件后：总量只统计「异常口径」命中行（本端点即异常视图）
                total += 1
                _bump(_bucket(callers, str(row.get("caller") or "")), raw_status,
                      is_error=is_err)
                _bump(_bucket(apis, str(row.get("endpoint") or "—")), raw_status,
                      is_error=is_err)
                if not is_err:
                    continue
                err_total += 1
                cls = _error_class(raw_status)
                by_class[cls] += 1
                k = str(raw_status)
                codes[k] = codes.get(k, 0) + 1
                _raw_caller = str(row.get("caller") or "")
                window.append({
                    "ts": row.get("ts"),
                    "callerMasked": _mask_caller(_raw_caller),
                    "callerQuery": drill_caller(_raw_caller),
                    "endpoint": row.get("endpoint"),
                    "status": raw_status,
                    "errorClass": cls,
                    "latencyMs": row.get("latency_ms"),
                    "paramsDigest": str(row.get("params_digest") or ""),
                    "responseDigest": str(row.get("response_digest") or ""),
                    "traceId": str(row.get("trace_id") or ""),
                })

    items = list(window)[::-1][offset:offset + limit]   # 新在前，取当前页窗口
    rate_min = mya.AUDIT_RATE_SAMPLE_MIN

    def _rank(table: dict) -> list[dict]:
        ranked = sorted(table.items(), key=lambda kv: (-kv[1]["errors"], -kv[1]["total"], kv[0]))
        return [{"key": k, **_dim_view(b, b["total"], rate_sample_min=rate_min)}
                for k, b in ranked[:ERROR_TOP_N]]

    by_caller = [{"callerMasked": _mask_caller(r["key"]),
                  "callerQuery": drill_caller(r["key"]),
                  **{k: v for k, v in r.items() if k != "key"}}
                 for r in _rank(callers)]
    by_api = [{"endpoint": r["key"], **{k: v for k, v in r.items() if k != "key"}}
              for r in _rank(apis)]
    return {
        "items": items,
        "total": err_total,
        "limit": limit,
        "offset": offset,
        "summary": {
            "windowTotal": total,
            "errorCount": err_total,
            "errorRate": (round(err_total * 100.0 / total, 1)
                          if total >= rate_min else None),
            "rateSampleMin": rate_min,
            "byClass": by_class,
            "codes": [{"status": int(k), "count": v}
                      for k, v in sorted(codes.items(), key=lambda kv: (-kv[1], int(kv[0])))[:ERROR_TOP_N * 2]],
            "sampleLimit": ERROR_SAMPLE_LIMIT,
        },
        "byCaller": by_caller,
        "byApi": by_api,
        "dimTruncated": dim_truncated,
    }


# ---------- ③ trace 详情（服务侧审计＋批量任务级审计两文件按 trace_id 精确反查） ----------

def trace_lookup(sources: list[tuple[str, Path]], trace_id: str, *,
                 limit: int = 20) -> dict:
    """按 trace_id 精确反查审计八元组（跨文件）：返回命中条目（新在前，掩码 caller）。

    sources＝[(来源标签, JSONL 路径)]——服务侧审计（service/audit.jsonl）与批量任务级
    审计（batch-worker 落 /data/audit/batch/audit.jsonl）分属两个写者，故按文件分列，
    页面注明来源；条目含 endpoint/status/latencyMs 与两大摘要（明文永不透出）。
    无 trace_id 或查无命中＝found=false 如实空态（不返回编造条目）。
    """
    tid = (trace_id or "").strip()
    if not tid:
        return {"traceId": "", "found": False, "entries": [], "sources": []}
    hits: list[dict] = []
    seen_sources: list[str] = []
    for label, path in sources:
        p = Path(path)
        if not p.is_file():
            continue
        seen_sources.append(label)
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if str(row.get("trace_id") or "") != tid:
                    continue
                _c = str(row.get("caller") or "")
                hits.append({
                    "source": label,
                    "ts": row.get("ts"),
                    "callerMasked": _mask_caller(_c),
                    "callerQuery": drill_caller(_c),
                    "endpoint": row.get("endpoint"),
                    "status": row.get("status"),
                    "errorClass": _error_class(row.get("status")),
                    "latencyMs": row.get("latency_ms"),
                    "paramsDigest": str(row.get("params_digest") or ""),
                    "responseDigest": str(row.get("response_digest") or ""),
                    "traceId": tid,
                })
    hits.sort(key=lambda it: _parse_ts(str(it.get("ts") or "")), reverse=True)
    return {"traceId": tid, "found": bool(hits), "entries": hits[:max(1, limit)],
            "sources": seen_sources}


# ---------- ④ 批量运行视角（真源＝JobStore/Redis 任务结构，async store 注入） ----------

# 口径说明（响应随行 caliber 字段＝页面就地文案的唯一来源；改口径只改这一处）
BATCH_CALIBER = {
    "queueDepth": "Redis 列表 oneserve:batch:queue 的 LLEN（真实队列长度，含尚未出队的任务 id）",
    "inFlight": "任务记录 status ∈ {running, cancelling} 的条数（执行中；queued 只排队不占并发位）",
    "counts": "全量任务记录按五态（queued/running/succeeded/failed/cancelled；cancelling 为取消中）计数",
    "backlog": "积压＝队列深度与「最长等待」——最长等待取 queued 任务 created_at 距当前秒数；无 queued 时为 null（不是 0）",
    "staleQueueItems": "队列项数 − queued 任务记录数（>0 ＝队列滞留，如实暴露不静默）",
    "recent": "最近 ERROR_RECENT_JOBS 条任务（按 created_at 降序）",
    "notConnected": ["任务耗时趋势", "worker 心跳/存活", "按键级失败明细下钻"],
    "notConnectedNote": "未接入项如实标注，不在本卡造数（归后续阶段）",
}
ERROR_RECENT_JOBS = 50     # 最近任务列表条数（有界内存）
RECENT_JOBS = ERROR_RECENT_JOBS


def _job_created_ts(job: dict) -> float:
    """任务 created_at → epoch 秒（解析失败返回 0，排序沉底，不抛错）。"""
    try:
        return datetime.fromisoformat(str(job.get("created_at") or "")).timestamp()
    except ValueError:
        return 0.0


def _job_row(job: dict, *, now_ts: float) -> dict:
    """单任务 → 运行视角行（进度百分比；无 total（快照物化前）＝progress null 不伪造）。"""
    total = int(job.get("total") or 0)
    processed = int(job.get("processed") or 0)
    status = str(job.get("status") or "unknown")
    return {
        "jobId": job.get("job_id"),
        "tenant": job.get("tenant"),
        "scope": job.get("scope") or batch_mod.SCOPE_LIST,
        "status": status,
        "total": total,
        "processed": processed,
        "progress": (processed * 100 // total) if total else None,
        "found": int(job.get("found") or 0),
        "error": job.get("error") or None,
        "createdAt": job.get("created_at"),
        "startedAt": job.get("started_at") or None,
        "finishedAt": job.get("finished_at") or None,
        "waitSec": (max(0, int(now_ts - _job_created_ts(job)))
                    if status == "queued" else None),
    }


async def batch_overview(store: Any, *, now_ts: float | None = None,
                         recent_limit: int = RECENT_JOBS) -> dict:
    """批量运行视角聚合（真源＝Redis 任务结构，store 注入；不依赖 fastapi/redis）。

    一遍 SCAN 任务键（跳过键名单 list）→ 逐任务读 hash（JobStore.get，整型还原）
    → 五态计数＋最近任务列表；队列深度＝LLEN(oneserve:batch:queue)（不经任务记录，
    故滞留队列项可被如实暴露：队列项数 − queued 记录数）。

    返回 {queueDepth,inFlight,counts,total,backlog,recent,degraded,caliber,generatedAt}；
    Redis 读失败＝degraded 附错误摘要并全零降级（页面标「未接入/读取失败」，不伪造在线）。
    """
    now = time.time() if now_ts is None else float(now_ts)
    counts = {s: 0 for s in batch_mod.STATES}
    jobs: list[dict] = []
    degraded: str | None = None
    try:
        async for key in store.scan_iter(match=batch_mod.JOB_KEY_PREFIX + "*"):
            if key.endswith(batch_mod.JOB_KEYS_SUFFIX):
                continue                              # 键名单 list 不是任务 hash
            raw = await store.hgetall(key)
            if not raw:
                continue
            job = dict(raw)
            for f in batch_mod._INT_FIELDS:          # 复用 batch 的整型字段口径（单一事实来源）
                if f in job:
                    try:
                        job[f] = int(job[f])
                    except (TypeError, ValueError):
                        job[f] = 0
            status = str(job.get("status") or "")
            if status in counts:
                counts[status] += 1
            jobs.append(job)
        queue_depth = int(await store.llen(batch_mod.QUEUE_KEY) or 0)
    except Exception as exc:  # noqa: BLE001 —— 读失败降级（页面如实标读取失败，不造假）
        degraded = f"{type(exc).__name__}: {exc}"
        counts = {s: 0 for s in batch_mod.STATES}
        jobs, queue_depth = [], 0

    jobs.sort(key=lambda j: (_job_created_ts(j), str(j.get("job_id"))), reverse=True)
    queued = [j for j in jobs if str(j.get("status")) == "queued"]
    oldest_wait = max((max(0, int(now - _job_created_ts(j))) for j in queued), default=None)
    in_flight = counts["running"] + counts["cancelling"]
    stale = (queue_depth - len(queued)) if degraded is None else None
    return {
        "queueDepth": queue_depth,
        "inFlight": in_flight,
        "counts": counts,
        "total": len(jobs),
        "backlog": {
            "oldestWaitSec": oldest_wait,      # 无 queued＝None（不是 0）
            "queued": len(queued),
            "staleQueueItems": stale,          # >0＝队列滞留（如实暴露）
        },
        "recent": [_job_row(j, now_ts=now) for j in jobs[:max(1, int(recent_limit))]],
        "degraded": degraded,                  # None＝未降级
        "caliber": BATCH_CALIBER,
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


# ---------- ⑤ 组件探活（维护者 追加：运维大盘四个状态卡必须接真） ----------

def probe_service(*, started_monotonic: float, version: str,
                  now_monotonic: float | None = None) -> dict:
    """服务组件＝本进程存活（真值：能执行到本函数就是活的）。

    返回 uptime 秒（由 lifespan 记的起始点算出）与版本——不用 mock 常量撑门面。
    """
    now = now_monotonic if now_monotonic is not None else time.monotonic()
    up = max(0, int(now - started_monotonic))
    h, m = up // 3600, (up % 3600) // 60
    return {"status": "ok",
            "detail": f"本进程存活 · 已运行 {h}h{m:02d}m · 版本 {version or '—'}"}


async def probe_redis(redis: Any, *, timeout: float = 2.0) -> dict:
    """Redis 组件＝真做一次 PING（失败如实 error，不吞掉）。"""
    t0 = time.monotonic()
    try:
        pong = await asyncio.wait_for(redis.ping(), timeout=timeout)
        ms = int((time.monotonic() - t0) * 1000)
        if pong:
            return {"status": "ok", "detail": f"PING→PONG（{ms}ms，命令通道可用）"}
        return {"status": "error", "detail": f"PING 返回异常值 {pong!r}"}
    except Exception as exc:  # noqa: BLE001 —— 探活失败如实报，不降级成「正常」
        return {"status": "error",
                "detail": f"PING 失败（{type(exc).__name__}: {exc}）"}


async def probe_gateway(*, host: str = "oneserve-apisix", port: int = 9080,
                        timeout: float = 1.5,
                        connector: Callable | None = None) -> dict:
    """网关组件＝真探一次数据面 TCP 可达性（APISIX 9080，compose 网内）。

    口径：TCP 连接成功＝数据面端口在线（与 compose healthcheck 同判据——镜像内无
    curl，凭「端口应答」判活）；连接失败/超时＝error 并如实给出目标与异常类型。
    探活目标经 APISIX_PROBE_HOST/PORT 可配；连接器可注入（纯逻辑层可单测，不起真网关）。
    """
    t0 = time.monotonic()
    conn = connector or asyncio.open_connection
    try:
        _reader, writer = await asyncio.wait_for(conn(host, port), timeout=timeout)
        ms = int((time.monotonic() - t0) * 1000)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 —— 关闭异常不影响「端口已应答」的结论
            pass
        return {"status": "ok", "detail": f"数据面 TCP 可达 {host}:{port}（{ms}ms）"}
    except Exception as exc:  # noqa: BLE001 —— 探不到＝error/未探到，不写「正常」
        return {"status": "error",
                "detail": f"数据面不可达 {host}:{port}（{type(exc).__name__}: {exc}）"}


def _audit_probe(services: dict) -> dict:
    """审计组件＝服务注册配置真实值（开启态/脱敏口径；关闭＝off，不是 error）。"""
    # 2026-10-15 死引用修复：原键 user_tag_query 已随 09-29 全服务下线删除（读恒空致审计
    # 组件恒 off）——对齐 batch.py BATCH_SOURCE_SERVICE 同款先例，改读现役主标签服务。
    audit = ((services or {}).get("v1-svc-usr-asset-tag") or {}).get("audit") or {}
    enabled = bool(audit.get("enabled"))
    mode = str(audit.get("log_params", "hash_only"))
    return {"status": "ok" if enabled else "off",
            "detail": f"审计模式 {mode}（哈希脱敏口径）" if enabled else "审计关闭（注册配置 audit.enabled=false）"}


async def components_view(redis: Any, services: dict, *, version: str,
                          started_monotonic: float,
                          gateway_host: str = "oneserve-apisix",
                          gateway_port: int = 9080,
                          gateway_connector: Callable | None = None) -> list[dict]:
    """运维大盘四组件状态（真探活；每项 {id,name,tech,status,detail,checkedAt}）。

    status ∈ {ok,warn,error,off}——未探到的组件由前端如实显示「未知」（不回退「正常」）；
    service 同步算、redis 与 gateway 并发探（互不阻塞，单项失败不影响其余项）。
    """
    t0 = time.time()
    svc = probe_service(started_monotonic=started_monotonic, version=version)
    redis_res, gw_res = await asyncio.gather(
        probe_redis(redis),
        probe_gateway(host=gateway_host, port=gateway_port, connector=gateway_connector),
    )
    checked = datetime.fromtimestamp(t0, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    audit_res = _audit_probe(services)
    return [
        {"id": "ops-comp-gateway", "name": "网关", "tech": "APISIX", **gw_res,
         "checkedAt": checked, "probe": f"TCP {gateway_host}:{gateway_port}"},
        {"id": "ops-comp-service", "name": "服务", "tech": "OneServe API", **svc,
         "checkedAt": checked, "probe": "本进程存活"},
        {"id": "ops-comp-redis", "name": "Redis", "tech": "缓存", **redis_res,
         "checkedAt": checked, "probe": "PING"},
        {"id": "ops-comp-audit", "name": "审计", "tech": "哈希脱敏", **audit_res,
         "checkedAt": checked, "probe": "注册配置"},
    ]

# ══ 常驻容器健康聚合（蓝图 1a 批·监控最小集 2026-10-20） ══
# 全部常驻容器一屏只读视图。探活原语单一来源＝healthz（TCP/PING/hbase 装配），
# 本模块只做编排与出参形状（分层同 components_view）。无网络探针的容器如实 off 标注，
# 不造状态。无登录模式（无登录模式决策）：原统一登录两容器探针行随 compose 收口移除（形态见 git 史）。
_CONTAINER_ROWS = (   # (容器名, 角色, 探针描述, 探活协程工厂名)——顺序＝页面呈现顺序
    # 一键开箱批（2026-10-28）：etcd/apisix/batch-worker 三件为 compose full 档组件
    # （profiles: [full]）——角色名如实标注，demo 档（默认 compose up）用户不误读。
    ("oneserve-etcd", "配置存储（etcd）· full 档组件", "TCP", "etcd"),
    ("oneserve-apisix", "网关（APISIX）· full 档组件", "TCP", "gateway"),
    ("oneserve-redis", "KV 缓存", "PING", "redis"),
    ("oneserve-service", "服务层（本端点宿主）", "本进程存活", "self"),
    ("oneserve-batch-worker", "批量 worker · full 档组件", "无网络探针", "batch_worker"),
    ("oneserve-portal", "门户静态站", "TCP", "portal"),
    ("oneserve-hbase", "冷存储数据源（可选）", "TCP Thrift", "hbase"),
)

# demo 档降级提示（一键开箱批）：full 档组件探活 degraded 时拼进 detail——如实汇报
# 「探不到」这一事实，同时说明 demo 档（默认 compose up）本就不起该组件（非故障），
# full 档出现同状态则为异常需查容器。文案对两档读者都为真，不引入档位配置项。
_FULL_PROFILE_DEGRADED_HINT = "（demo 档默认不含本组件，此状态属预期；--profile full 起全栈，full 档出现则为异常）"


async def containers_view(redis: Any) -> dict:
    """常驻容器健康一屏（只读；GET /v1/admin/monitor/containers 出参口径）。

    探活判据（与既有判据同源，不新造）：redis=PING（error 致命，对齐 /healthz 编排
    口径）；etcd/apisix/portal/hbase=TCP 三次握手；service=本端点进程存活；
    batch-worker 无网络探针＝如实 off（进程级判据归 compose healthcheck，
    本视图不越权造状态）。
    overall：任一 error→error（现仅 redis）；其余任一 degraded→degraded；否则 ok
    （off 不参与评级——hbase 未配置/batch-worker 无探针＝常态非故障）。
    """
    from app import healthz as hlz   # 局部导入：探活原语单一来源（healthz 零 fastapi 依赖，同层安全）
    results = await asyncio.gather(
        hlz.probe_tcp(hlz.ETCD_PROBE_HOST, hlz.ETCD_PROBE_PORT, what="etcd"),
        hlz.probe_tcp(hlz.GATEWAY_PROBE_HOST, hlz.GATEWAY_PROBE_PORT, what="APISIX"),
        hlz.probe_redis(redis),
        hlz.probe_tcp(hlz.PORTAL_PROBE_HOST, hlz.PORTAL_PROBE_PORT, what="门户"),
        hlz.probe_hbase(),
    )
    by_key = {"etcd": results[0], "gateway": results[1], "redis": results[2],
              "portal": results[3], "hbase": results[4],
              "self": {"status": "ok", "detail": "本端点进程存活（聚合视图宿主）"},
              "batch_worker": {"status": "off",
                               "detail": "无网络探针（纯脚本容器）；进程级判据归 compose healthcheck——如实标注不造状态"}}
    rows = []
    probe_targets = {
        "etcd": f"TCP {hlz.ETCD_PROBE_HOST}:{hlz.ETCD_PROBE_PORT}",
        "gateway": f"TCP {hlz.GATEWAY_PROBE_HOST}:{hlz.GATEWAY_PROBE_PORT}",
        "redis": "PING",
        "self": "本进程存活",
        "batch_worker": "—",
        "portal": f"TCP {hlz.PORTAL_PROBE_HOST}:{hlz.PORTAL_PROBE_PORT}",
        "hbase": "TCP（配置了才探，未配置＝off）",
    }
    for container, role, probe_label, key in _CONTAINER_ROWS:
        res = by_key[key]
        detail = res["detail"]
        if res["status"] == "degraded" and container in (
                "oneserve-etcd", "oneserve-apisix"):
            detail += _FULL_PROFILE_DEGRADED_HINT   # demo 档降级如实提示（一键开箱批）
        rows.append({
            "container": container, "role": role,
            "probe": probe_targets[key] if key != "batch_worker" else "—",
            "status": res["status"], "detail": detail,
            "latencyMs": res.get("latencyMs"),
        })
    statuses = [r["status"] for r in rows]
    if "error" in statuses:
        overall = "error"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "ok"
    return {"overall": overall, "containers": rows}
