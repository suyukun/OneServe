# -*- coding: utf-8 -*-
"""我的接入 · 自助查询纯逻辑（P0-7 · SPEC §5 P0-7 行；D1=b 凭证兼身份）。

不 import FastAPI/redis（分层纪律同 appkeys.py）：本模块只做四件事的决策逻辑，
my_api.py / audit_api.py 是 HTTP 薄胶水——
  ① 我的凭证：按凭证身份（完整 appkey 精确匹配）查 appkeys.yaml 权威源，
     产出接入方自助视图：掩码＋类型（sb_ 沙箱/ak_ 正式）＋状态＋有效期＋
     到期倒计时（≤ EXPIRING_SOON_DAYS 天标黄）；
  ② 我的配额（P1-3 接真；P1-5 起登记值＝网关拦截值）：四要素全真——日限额取
     注册表登记值（quota_daily，网关 limit-count 同取此值强制，见 app/ratelimit.py）；
     已用＝当日（东八区）审计 JSONL 按 caller 精确聚合的真实调用条数（成败与 429 都计入）；
     剩余＝上限−已用（下限 0）；重置＝东八区次日 00:00。附近 7 日逐日用量
     （同一遍扫描聚合）与口径说明字段（caliber），不造数不估算；
  ③ 我的日志：审计 JSONL（audit.py 八元组）按 caller 过滤，traceId 透出可反查；
     网关拒记（P0-5 旁路 Redis 列表 portal:trace:gw-rejected）合并展示；
  ④ 审计查询（P1-1 · C 面合规审计）：审计 JSONL 多条件过滤（时间/caller/endpoint/
     状态/trace_id）＋limit/分页＋统计摘要（今日窗总量/成功率/慢查询 top）——
     audit_api.py 薄胶水出 GET /v1/portal/audit/logs。
  ⑤ 配额用量（P1-3 · SPEC §5-P1-3）：审计 JSONL 一遍扫描按 caller 精确聚合
     当日用量＋近 7 日逐日用量；四要素（上限/已用/剩余/重置）在此算出——
     my_api.py 出 GET /v1/portal/my/quota（A7 我的配额卡），admin_api.py 出
     GET /v1/admin/appkeys/usage（B6 按调用方看用量，同函数同口径）。

安全与如实口径：
  - caller 参数＝凭证本身（sb_/ak_ 全文）而非租户 ID——按租户查会泄露他人凭证；
  - 输出一律掩码（mask_appkey），secret 摘要不透出；
  - 网关拒记无调用方身份（key-auth 401 未过认证），如实标 source=gateway 并在
    页面注明「不能按凭证过滤、按时间并入参考」——不硬造归属。
"""
from __future__ import annotations

import json
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app import appkeys as appk

# 到期倒计时标黄阈值（天）：≤7 天提醒轮转/续期（沙箱 TTL 默认同值，属正常口径）
EXPIRING_SOON_DAYS = 7

# 日志默认/上限条数（端点层 clamp；页面「近 N 次」口径）
DEFAULT_LOG_LIMIT = 20
MAX_LOG_LIMIT = 100

_EPOCH_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _parse_ts(ts: str) -> datetime:
    """ISO8601 容错解析：审计行带 +00:00、网关拒记带 Z，统一成可比较的 aware datetime。

    解析失败返回 epoch 下限（排序沉底，不抛错——日志视图对脏行保持只读容错）。
    """
    s = (ts or "").strip()
    if not s:
        return _EPOCH_MIN
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH_MIN


def expiry_days(expires_at: str | None, today: date | None = None) -> int | None:
    """距有效期还有几天（UTC 日期差；无有效期返回 None）。"""
    exp = str(expires_at or "").strip()
    if not appk.DATE_RE.match(exp):
        return None
    today = today or datetime.now(timezone.utc).date()
    return (date.fromisoformat(exp) - today).days


def credential_view(rec: dict, *, today: date | None = None) -> dict:
    """单条凭证 → 自助视图（掩码＋类型＋状态＋倒计时；grants 只给服务×字段数摘要）。

    P1-4：轮换态下追加 rotation 子对象（重叠期剩余天数/到期日/是否已失效），供门户
    展示「重叠期内·剩 N 天」；完整 appkey 与 secret 摘要一如既往不出端点。
    """
    days = expiry_days(rec.get("expires_at"), today)
    expired = days is not None and days <= 0
    status = "expired" if expired else str(rec.get("status") or "unknown")
    rot = appk.rotation_state(rec, today=today)
    return {
        "appkeyMasked": appk.mask_appkey(rec.get("appkey", "")),
        "keyType": rec.get("key_type"),
        "status": status,
        "expiresAt": rec.get("expires_at") or None,
        "daysLeft": days,
        "expiringSoon": days is not None and not expired and days <= EXPIRING_SOON_DAYS,
        "issuedAt": rec.get("issued_at"),
        "appName": rec.get("app_name"),
        "callerName": rec.get("caller_name"),
        "quotaQps": rec.get("quota_qps"),
        "quotaDaily": rec.get("quota_daily"),
        "grants": [{"service": g.get("service"), "fieldsCount": len(g.get("fields") or [])}
                   for g in rec.get("grants", [])],
        "rotation": {
            "rotated": rot["rotated"], "rotatedAt": rot["rotatedAt"],
            "overlapExpiresAt": rot["overlapExpiresAt"], "daysLeft": rot["daysLeft"],
            "overlapDays": rot["overlapDays"], "expired": rot["expired"],
        },
    }


def my_credentials(doc: dict, caller: str, *, today: date | None = None) -> list[dict]:
    """按凭证身份过滤注册表：完整 appkey 精确匹配（0/1 条），其余凭证不可见。"""
    caller = (caller or "").strip()
    if not caller:
        return []
    rec = next((k for k in doc.get("keys") or [] if k.get("appkey") == caller), None)
    return [credential_view(rec, today=today)] if rec else []


# ---------- ③ 日志：审计尾读＋网关拒记＋合并 ----------

def audit_tail(path: Path, *, caller: str = "", trace_id: str = "",
               limit: int = DEFAULT_LOG_LIMIT) -> list[dict]:
    """审计 JSONL 尾读：按 caller/trace_id（两者都给＝AND）过滤，取最近 limit 条。

    流式逐行读＋deque(maxlen)——文件多大都只留命中尾窗；坏行跳过（只读容错，
    不抛错：日志视图不该被一条脏行打断）；存量七列行缺 trace_id 等价空串。
    返回按文件序新在后，调用方（merge_logs）统一降序。
    """
    caller, trace_id = (caller or "").strip(), (trace_id or "").strip()
    hits: deque[dict] = deque(maxlen=max(1, limit))
    p = Path(path)
    if not p.is_file():
        return []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if caller and str(row.get("caller") or "") != caller:
                continue
            if trace_id and str(row.get("trace_id") or "") != trace_id:
                continue
            hits.append({
                "ts": row.get("ts"),
                "endpoint": row.get("endpoint"),
                "status": row.get("status"),
                "latencyMs": row.get("latency_ms"),
                "traceId": str(row.get("trace_id") or ""),
                "callerMasked": _mask_caller(str(row.get("caller") or "")),
                "source": "service",
            })
    return list(hits)


def _mask_caller(caller: str) -> str:
    """调用方掩码：sb_/ak_ 凭证走 mask_appkey；其余标识（如 demo-page）截前 12 位＋…。"""
    if not caller:
        return ""
    if caller.startswith((appk.SB_PREFIX, appk.AK_PREFIX)):
        return appk.mask_appkey(caller)
    return caller[:12] + "…"


def gw_rejected_view(raw_entries: list[str], *, trace_id: str = "",
                     limit: int = DEFAULT_LOG_LIMIT) -> list[dict]:
    """网关拒记（P0-5 旁路列表原始 JSON 串）→ 视图条目（source=gateway）。

    条目本身无调用方身份（请求未通过网关认证）——callerMasked 如实留空，
    页面注明「不能按凭证过滤」。trace_id 给定时按其精确过滤（反查场景）。
    """
    trace_id = (trace_id or "").strip()
    out: list[dict] = []
    for raw in raw_entries:
        try:
            row = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if trace_id and str(row.get("trace_id") or "") != trace_id:
            continue
        out.append({
            "ts": row.get("ts"),
            "endpoint": row.get("uri"),
            "status": row.get("status"),
            "latencyMs": None,
            "traceId": str(row.get("trace_id") or ""),
            "callerMasked": "",
            "source": "gateway",
        })
        if not trace_id and len(out) >= max(1, limit):
            break   # 反查模式须扫全列表；列表模式留最近 limit 条即够
    return out


def merge_logs(*groups: list[dict], limit: int = DEFAULT_LOG_LIMIT) -> list[dict]:
    """多源合并：按 ts 降序（归一化解析，Z/+00:00 混排安全）取前 limit 条。"""
    items = [it for g in groups for it in (g or [])]
    items.sort(key=lambda it: _parse_ts(str(it.get("ts") or "")), reverse=True)
    return items[: max(1, limit)]


# ---------- ④ 审计查询（P1-1 · SPEC §2-C1/§5-P1-1）：多条件过滤＋分页＋统计 ----------

AUDIT_QUERY_DEFAULT_LIMIT = 20   # 默认页大小（与 ③ 日志「近 N 次」同档）
AUDIT_QUERY_MAX_LIMIT = 100      # 页大小上限（端点层 clamp）
AUDIT_SLOW_MS = 200              # 慢查询阈值：latency_ms > 200 计慢（对齐 SLA P99≤200ms 口径）
AUDIT_SLOW_TOP_N = 5             # 统计摘要「慢查询 Top」端点条数
AUDIT_RATE_SAMPLE_MIN = 100      # 成功率小基数阈值（同门户 app.js rateSampleMin：不足不展示）


_TZ_CN = timezone(timedelta(hours=8))   # 纯日期「今日」按东八区滚动（与 stats.py 同口径）


def _norm_ts_bound(ts: str, *, day_end: bool = False) -> datetime:
    """时间边界解析：纯日期(YYYY-MM-DD)或 ISO 时刻 → aware datetime（含端闭区间）。

    纯日期按东八区全天口径（门户受众与 stats.py「今日」同源）：day_end=False →
    当日（东八区）00:00:00；True → 当日 23:59:59.999999；带时区偏移的完整 ISO
    时刻原样解析，不受此口径影响。
    解析失败抛 ValueError（端点转 400 人话提示——审计面输入错误要就近说清）。
    """
    s = (ts or "").strip()
    if not s:
        raise ValueError("时间条件为空")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"时间格式不认识：{s}（示例 2026-09-22 或 2026-09-22T00:00:00+08:00）") from exc
    if dt.tzinfo is None:                       # 纯日期：东八区全天；无时区时刻：按 UTC
        dt = dt.replace(tzinfo=_TZ_CN if len(s) <= 10 else timezone.utc)
    if day_end and len(s) <= 10:                # 纯日期作上界＝含当日（东八区）全天
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt


def _match_status(row_status, status_filter: str) -> bool:
    """状态过滤：空＝不过滤；"2xx"/"4xx"/"5xx"＝按类；数字串＝精确匹配。"""
    sf = (status_filter or "").strip().lower()
    if not sf:
        return True
    try:
        rs = int(row_status)
    except (TypeError, ValueError):
        return False                            # 脏行状态不匹配任何条件
    if len(sf) == 3 and sf[1:] == "xx":
        return 100 * int(sf[0]) <= rs < 100 * (int(sf[0]) + 1)
    try:
        return rs == int(sf)
    except ValueError:
        return False


AUDIT_QUERY_CHUNK = 4 * 1024 * 1024   # 倒序分块大小：4MB，文件多大都不整读进内存


def _read_chunks_reverse(fh, size: int, *, chunk: int = AUDIT_QUERY_CHUNK):
    """倒序分块读文件：从文件尾向前产出完整行（yield 行文本，新→旧）。

    行按物理序倒序产出——审计 JSONL 追加写（时间新在后），倒读即时间新→旧；
    每块至多 chunk 字节（尾块可更小），内存有界；跨块的行由前缀余量拼接。
    （2026-10-14 读写提速：文件 392MB/105 万行全量正扫≈1.5s/查，近窗查询
    倒读＋提前终止后毫秒级；块读替代整文件 load，内存同样有界。）

    fh 必须是二进制句柄（open("rb")）——文本流 seek 只认 tell() 的不透明
    cookie，任意字节偏移会错位（tests/test_audit_query_perf.py 小块机检钉死）。
    行按 b"\n" 切（UTF-8 多字节序列不含 0x0A，字节级切行安全），产出前解码。
    """
    pos = size
    rem = b""
    while pos > 0:
        n = min(chunk, pos)
        pos -= n
        fh.seek(pos)
        buf = fh.read(n)
        lines = (buf + rem).split(b"\n")
        rem = lines.pop(0)                # 块首可能是不完整行，留给下一块（更旧侧）拼
        for ln in reversed(lines):        # 本块内仍新在前
            if ln:                        # 尾块末尾换行后的空段/空行跳过（非数据行）
                yield ln.decode("utf-8", errors="replace")
    if rem:
        yield rem.decode("utf-8", errors="replace")   # 文件首行（最旧）


def audit_query(path: Path, *, ts_from: str = "", ts_to: str = "",
                caller: str = "", endpoint: str = "", status: str = "",
                trace_id: str = "", limit: int = AUDIT_QUERY_DEFAULT_LIMIT,
                offset: int = 0) -> dict:
    """C1 审计查询：审计 JSONL 倒序扫描＋提前终止，多条件 AND 过滤＋新在前分页＋统计摘要。

    读法（2026-10-14 提速改造）：**从文件尾向前分块读**（_read_chunks_reverse，
    4MB 块）——审计文件按行追加（时间新在后），倒读即时间新→旧，正合「新在前
    分页」口径：命中行进 deque(maxlen=offset+limit) 头窗，够页即无需换序。
    **提前终止**：带 ts_from 下界时，一旦行时间 < 下界即停（时间序保证更旧
    行不可能命中）——近窗查询不再全量扫；无 ts_from 的查询同样倒读（最近的
    先出），但 total/summary 要全量计数，条目窗满后仍继续扫至文件头（口径
    不变优先，性能次之）。

    其余语义与改造前完全一致：时间窗（ts_from/ts_to 含端闭，纯日期按东八区
    全天）/caller（部分匹配——C 侧合规检索，与 ③ 的 A 侧精确匹配不同：合规
    要能凭掩码片段搜人，且出口仍掩码，不因检索能力放宽出参口径）/endpoint
    （部分匹配）/status（类或精确）/trace_id（精确）。

    返回 {items, total, limit, offset, summary}：
      items   八元组行（caller 掩码出参，完整凭证不出端点——掩码纪律同 ③）；
      total   全部命中条数（分页导航）；
      summary 按**全部命中行**聚合（与翻页无关）：总量/成功(2xx)/失败/成功率
              （total<AUDIT_RATE_SAMPLE_MIN 时 None＝小基数不展示口径，页面标
              「样本量不足」）/慢查询数（>AUDIT_SLOW_MS）/慢查询 Top 端点
              （按平均耗时降序，AUDIT_SLOW_TOP_N 条）。
    坏行跳过（只读容错，同 ③）；存量七列行缺 trace_id 等价空串。
    """
    sf = (status or "").strip()
    if sf and not (sf in ("2xx", "3xx", "4xx", "5xx") or sf.isdigit()):
        raise ValueError(f"状态条件不认识：{sf}（可用 2xx/4xx/5xx 或具体状态码如 403）")
    lo = _norm_ts_bound(ts_from) if ts_from.strip() else None
    hi = _norm_ts_bound(ts_to, day_end=True) if ts_to.strip() else None
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("时间范围反了：起始时间晚于结束时间")
    caller_q, endpoint_q = caller.strip(), endpoint.strip()
    trace_q = trace_id.strip()
    limit = min(max(int(limit), 1), AUDIT_QUERY_MAX_LIMIT)
    offset = max(int(offset), 0)

    # 倒读头窗：时间新→旧产出，先到先占窗——窗口即「最新 offset+limit 条命中」
    window: deque[dict] = deque(maxlen=offset + limit)
    total = success = fail = slow = 0
    per_ep: dict[str, list[int]] = {}                    # endpoint -> [count, sum_ms, max_ms]
    # 只有带 ts_from 下界且行时间序可信时才允许提前终止（total/summary 全量口径
    # 才保得住）；无 ts_from 时必须扫到文件头才能给 total——见 docstring 口径说明。
    # #38（复审 P2-5 2026-10-15）：单调性守护——倒读方向新→旧，一旦读到比「已见最小
    # ts」更大的时间戳即乱序（时钟回拨/多写者交叠），提前终止前提被破坏＝fail-safe
    # 关闸不终止、扫至文件头（宁可退化全扫，不冒漏计风险）。脏 ts 行不参与判定。
    can_stop = lo is not None
    seq_ok = True
    min_seen_ts = None

    p = Path(path)
    if p.is_file():
        size = p.stat().st_size
        with p.open("rb") as fh:                 # 二进制：字节偏移 seek 才精确
            for line in _read_chunks_reverse(fh, size):
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
                if not _match_status(row.get("status"), sf):
                    continue
                ts = _parse_ts(str(row.get("ts") or ""))
                if ts != _EPOCH_MIN:                       # 脏 ts 不参与单调判定（既有跳过口径）
                    if seq_ok and min_seen_ts is not None and ts > min_seen_ts:
                        seq_ok = False                     # 乱序（回拨/多写者）：关终止闸，fail-safe
                    if seq_ok and (min_seen_ts is None or ts < min_seen_ts):
                        min_seen_ts = ts
                if hi is not None and ts > hi:
                    continue
                if lo is not None and ts < lo:
                    if can_stop and seq_ok:               # 时间序可信：更旧行不可能再命中——提前终止
                        break
                    continue
                total += 1
                st = row.get("status")
                st_i = st if isinstance(st, int) else None
                if st_i is not None:
                    if 200 <= st_i < 300:
                        success += 1
                    else:
                        fail += 1
                lat = row.get("latency_ms")
                lat_i = lat if isinstance(lat, int) else None
                if lat_i is not None:
                    if lat_i > AUDIT_SLOW_MS:
                        slow += 1
                    ep = str(row.get("endpoint") or "—")
                    agg = per_ep.setdefault(ep, [0, 0, 0])
                    agg[0] += 1
                    agg[1] += lat_i
                    agg[2] = max(agg[2], lat_i)
                if len(window) < offset + limit:      # 头窗：只收最新的 offset+limit 条
                    window.append({
                        "ts": row.get("ts"),
                        "callerMasked": _mask_caller(str(row.get("caller") or "")),
                        "endpoint": row.get("endpoint"),
                        "paramsDigest": str(row.get("params_digest") or ""),
                        "status": st,
                        "latencyMs": lat,
                        "responseDigest": str(row.get("response_digest") or ""),
                        "traceId": str(row.get("trace_id") or ""),
                    })

    items = list(window)[offset:offset + limit]          # 倒读即新在前，直接切当前页
    slow_top = sorted(
        ({"endpoint": ep, "count": c, "avgMs": round(s / c, 1), "maxMs": mx}
         for ep, (c, s, mx) in per_ep.items()),
        key=lambda d: (-d["avgMs"], -d["count"], d["endpoint"]))[:AUDIT_SLOW_TOP_N]
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": {
            "total": total, "success": success, "fail": fail,
            "successRate": (round(success * 100.0 / total, 1)
                            if total >= AUDIT_RATE_SAMPLE_MIN else None),
            "rateSampleMin": AUDIT_RATE_SAMPLE_MIN,
            "slowCount": slow, "slowMs": AUDIT_SLOW_MS,
            "slowTop": slow_top,
        },
    }

# ---------- ⑤ 配额用量（P1-3 · SPEC §5-P1-3）：四要素真值＋近 7 日趋势 ----------

QUOTA_TREND_DAYS = 7                 # 迷你趋势窗口（含今日，与 stats.TREND_DAYS 同值同口径）

# 口径说明（响应随行 caliber 字段＝页面就地文案的唯一来源；改口径只改这一处）
QUOTA_CALIBER = {
    "usageSource": "当日审计台账（八元组 JSONL）按凭证精确聚合的真实调用条数",
    "usageIncludes": "成功（2xx）与失败（4xx/5xx，含 429 限流拒绝）一并计入已用",
    "limitSource": "授权登记配置（appkeys.yaml quota_daily 登记值）——网关限流插件同取此值，单一事实来源",
    # P1-5：按 route×consumer 的日配额/速率/并发已由网关插件强制（登记值＝拦截值，
    # 见 app/ratelimit.py）。未登记配额的调用方不放行限流（如实标注见 gatewayEnforcedBy）。
    "limitEnforced": True,
    "gatewayEnforcedBy": "APISIX limit-count/limit-conn 按 consumer×route 强制；429 带 X-RateLimit-Reason（global/endpoint/concurrent）",
    "resetAt": "东八区次日 00:00（与平台日滚动口径同源）",
    "trendDays": QUOTA_TREND_DAYS,
}


def _day_floor(dt: datetime) -> datetime:
    """东八区日界零点（aware datetime；配额日滚动与 stats.day_key 同口径）。"""
    return dt.astimezone(_TZ_CN).replace(hour=0, minute=0, second=0, microsecond=0)


def reset_at(now: datetime | None = None) -> datetime:
    """重置时刻＝东八区次日 00:00（可注入 now 便于钉死边界）。"""
    t = (now or datetime.now(timezone.utc)).astimezone(_TZ_CN)
    return _day_floor(t) + timedelta(days=1)


def usage_scan(path: Path, callers, *, now: datetime | None = None) -> dict:
    """审计 JSONL 一遍扫描：按 caller 精确聚合当日用量＋近 7 日逐日用量。

    caller 精确匹配（掩码纪律）："sb_1234…" 这类掩码片段不得命中真凭证——
    配额是「我的」视图与运营台用量列共用的真值，按片段匹配会张冠李戴。

    返回 {caller: {"day": YYYY-MM-DD, "used": int, "trend": [{"date","used"}×7]}}；
    趋势按日期升序（最旧→今日），无调用的日补 0（迷你趋势要连续轴，不跳点）。
    坏行跳过、缺列容错（同 ③④ 读法）；文件不存在＝全零（未产生调用，非错误）。
    """
    wanted = {str(c) for c in callers or () if str(c)}
    if not wanted:
        return {}
    t = (now or datetime.now(timezone.utc)).astimezone(_TZ_CN)
    today = _day_floor(t)
    days = [(today - timedelta(days=i)) for i in range(QUOTA_TREND_DAYS - 1, -1, -1)]
    day_keys = [d.strftime("%Y-%m-%d") for d in days]
    # 每个 caller 自带一份按日计数（只给要查的 caller 建表——全表 caller 不落内存）
    per = {c: {k: 0 for k in day_keys} for c in wanted}

    p = Path(path)
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
                bucket = per.get(str(row.get("caller") or ""))
                if bucket is None:
                    continue
                ts = _parse_ts(str(row.get("ts") or ""))
                if ts == _EPOCH_MIN:                     # 脏 ts：不归入任何一日（不造数）
                    continue
                key = ts.astimezone(_TZ_CN).strftime("%Y-%m-%d")
                if key in bucket:
                    bucket[key] += 1
    out = {}
    for caller, bucket in per.items():
        out[caller] = {
            "day": day_keys[-1],
            "used": bucket[day_keys[-1]],
            "trend": [{"date": k, "used": bucket[k]} for k in day_keys],
        }
    return out


def quota_view(quota_daily, usage: dict | None) -> dict:
    """四要素计算（纯算术，无时间/IO）：上限/已用/剩余/重置＋趋势＋口径说明。

    quota_daily＝登记值（None/非法＝未登记，四要素如实降级为 null 而非 0——
    「没登记」与「登记了 0 次」不是一回事）；usage＝usage_scan 该 caller 的聚合
    （None 视同无调用，used=0：未产生调用是真实状态，不是缺失）。
    剩余下限 0（超额调用不显示负数）；ratio 供页面画进度条（上限≤0 时为 None）。
    """
    used = int((usage or {}).get("used") or 0)
    trend = (usage or {}).get("trend") or []
    try:
        limit = int(quota_daily)
    except (TypeError, ValueError):
        limit = None
    if limit is None or limit < 0:
        return {"limit": None, "used": used, "remaining": None, "ratio": None,
                "exceeded": False, "trend": trend, "registered": False}
    remaining = max(0, limit - used)
    return {"limit": limit, "used": used, "remaining": remaining,
            "ratio": (round(used * 100.0 / limit, 1) if limit > 0 else None),
            "exceeded": used > limit, "trend": trend, "registered": True}


def my_quota(doc: dict, caller: str, audit_path: Path, *,
             now: datetime | None = None) -> dict:
    """② A7「我的配额」视图：凭证登记值 × 当日审计真实用量＝四要素（含口径说明）。

    caller 精确匹配（同 ① 凭证视图）：未知 caller / 空 caller＝found=false 如实空态
    （不返回「0 已用」——那会把「查无此凭证」伪装成「今天没调用」）。
    """
    caller = (caller or "").strip()
    if not caller:
        return {"found": False, "callerMasked": "", "quota": None, "caliber": QUOTA_CALIBER}
    rec = next((k for k in doc.get("keys") or [] if k.get("appkey") == caller), None)
    if rec is None:
        return {"found": False, "callerMasked": appk.mask_appkey(caller),
                "quota": None, "caliber": QUOTA_CALIBER}
    usage = usage_scan(audit_path, [caller], now=now).get(caller)
    return {
        "found": True,
        "callerMasked": appk.mask_appkey(caller),
        "appName": rec.get("app_name"),
        "keyType": rec.get("key_type"),
        "status": rec.get("status"),
        "day": (usage or {}).get("day"),
        "quota": quota_view(rec.get("quota_daily"), usage),
        "resetAt": reset_at(now).isoformat(),
        "caliber": QUOTA_CALIBER,
    }


def admin_usage(doc: dict, audit_path: Path, *, now: datetime | None = None) -> dict:
    """⑤ B6 运营台用量：按注册凭证聚合「已用/上限」实时值（同 my_quota 口径同函数）。

    一次扫描全部登记凭证（运营台一次要看全表，不能逐行扫文件）；出参只带 kid
    （sha256 前 12 位）与掩码——完整 appkey 不出端点，与 appkeys.list_view 同纪律。
    """
    keys = [k for k in doc.get("keys") or [] if k.get("appkey")]
    usage = usage_scan(audit_path, [str(k.get("appkey")) for k in keys], now=now)
    items = []
    for k in keys:
        appkey = str(k.get("appkey"))
        q = quota_view(k.get("quota_daily"), usage.get(appkey))
        items.append({"kid": appk.record_kid(appkey),
                      "appkeyMasked": appk.mask_appkey(appkey),
                      "used": q["used"], "limit": q["limit"],
                      "remaining": q["remaining"], "exceeded": q["exceeded"],
                      "day": (usage.get(appkey) or {}).get("day")})
    return {"items": items, "resetAt": reset_at(now).isoformat(), "caliber": QUOTA_CALIBER}
