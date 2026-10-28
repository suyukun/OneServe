# -*- coding: utf-8 -*-
"""计量报表（P1 · D20 统计预聚合落地）——读统计快照，永不解析原始日志。

#76 Phase 2 改造：metering 数据源从「直读审计 JSONL」切换为「Redis 统计快照」
（audit_consumer.py 按 AppKey×日 预聚合写入），终结 D20 偏差（金融级架构盘点 §二②）。
旧 metering_scan（JSONL 倒序聚合）保留为降级路径（消费者未部署/统计键缺失时兜底）。

读取优先级（metering_api.py 编排）：
  ① Redis 统计快照（metering_from_stats）——O(callers×days) 次 HGETALL，毫秒级；
  ② JSONL 倒序聚合（metering_scan）——降级路径，GB 级文件分钟级（仅过渡期）。

分层纪律（同 appkeys.py/my_access.py）：本模块不 import FastAPI/redis——纯逻辑
＋视图两段；metering_api.py 是 HTTP 薄胶水（GET /v1/admin/metering/summary）。
Redis 读取经 async 函数注入 redis client（与 stats.py 同模式）。

掩码口径（同 my_access.admin_usage）：完整 appkey 不出端点——登记凭证出 kid
（sha256 前 12 位）＋掩码＋登记名；sb_/ak_ 前缀的未登记凭证只出掩码；非凭证
标识（demo-page/console 等非机密）截前 12 位＋…。纯聚合，无第二份计数器。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import appkeys as appk
# 倒序分块读/时间解析/东八区常量＝昨夜提速批同款（单一实现，不复制第二份；
# 以下为 my_access 模块内私有名，跨模块取用系有意为之——读取器语义被
# tests/test_audit_query_perf.py 机检钉死，复制漂移比共享私有名更危险）。
from app.my_access import _EPOCH_MIN, _TZ_CN, _parse_ts, _read_chunks_reverse

METERING_MAX_DAYS = 7          # 窗口上限（与任务卡「今日/近7天」一致；放大须重估倒读收益）

# Redis 统计键前缀（与 audit_consumer.py StatsAggregator 写入口径对齐）
STATS_KEY_PREFIX = "audit:stats:"       # audit:stats:{YYYYMMDD}:{caller}
STATS_DAY_INDEX_KEY = "audit:stats:days"  # SET of active day keys

# 口径说明（响应随行 caliber；页面文案唯一来源，改口径只改这一处）
METERING_CALIBER = {
    "usageSource": "审计统计快照（audit_consumer 预聚合）按东八区自然日的真实调用量；降级路径为 JSONL 倒序聚合",
    "dimensions": "调用主体（caller）× 服务（endpoint）× 日",
    "success": "2xx 计成功，其余计失败（非数字状态只计总量，不猜成败）",
    "avgMs": "窗口内 latency_ms 算术平均（缺耗时的行不计入均值）",
    "window": "东八区自然日、含今日（days=1 即今日，days=7 即近 7 天）",
    "notIncluded": "网关即拒（key-auth 401 未到服务层）不在审计台账，不计入本报表",
}


def _window_days(now: datetime | None, days: int) -> list[str]:
    """窗口日键（东八区 YYYY-MM-DD，升序最旧→今日）；days 非法抛 ValueError。"""
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= METERING_MAX_DAYS:
        raise ValueError(f"时间窗只支持 1（今日）到 {METERING_MAX_DAYS}（近 {METERING_MAX_DAYS} 天）天")
    t = (now or datetime.now(timezone.utc)).astimezone(_TZ_CN)
    return [(t - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]


def metering_scan(path: Path, *, days: int = 7, now: datetime | None = None) -> dict:
    """审计 JSONL 倒序聚合：窗口内按 caller×endpoint×日 计量（纯读，坏行容错）。

    返回 {"days": [日键…], "callers": {caller: {
        "calls","success","fail","latSum","latCnt",
        "services": {endpoint: [calls,success,fail,latSum,latCnt]},
        "days": {日键: [calls,success,fail]}}}}；
    caller 键为审计原值（完整 appkey）——**本函数返回值不得直接出端点**，
    掩码/登记名合并由 metering_view 做。文件不存在＝空聚合（未产生调用，非错误）。
    """
    day_keys = _window_days(now, days)
    day_set = set(day_keys)
    # 窗口下界＝最旧日（东八区）00:00：倒读一旦早于它即停（审计追加序＝时间序）
    floor = datetime.fromisoformat(day_keys[0] + "T00:00:00").replace(tzinfo=_TZ_CN)
    callers: dict[str, dict] = {}
    # #38（复审 P2-5 2026-10-15）：倒读提前终止的单调性守护——新→旧方向读到比
    # 「已见最小 ts」更大的时间戳＝乱序（时钟回拨/多写者交叠），floor 终止前提失效，
    # fail-safe＝不再 break、扫至文件头（聚合面本就保守口径，宁可多扫不漏账）。
    seq_ok = True
    min_seen_ts = None

    p = Path(path)
    if p.is_file():
        size = p.stat().st_size
        with p.open("rb") as fh:                      # 二进制句柄：字节偏移 seek 才精确
            for line in _read_chunks_reverse(fh, size):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue                          # 坏行跳过（只读容错，同 ③④ 读法）
                if not isinstance(row, dict):
                    continue                          # 合法 JSON 但非对象（数组/数字/裸串）：无八元组可取
                ts = _parse_ts(str(row.get("ts") or ""))
                if ts == _EPOCH_MIN:
                    continue                          # 脏 ts：不归入任何一日，也不触发终止
                if ts != _EPOCH_MIN and seq_ok and min_seen_ts is not None and ts > min_seen_ts:
                    seq_ok = False                    # 乱序：关终止闸（fail-safe 全扫）
                if seq_ok and (min_seen_ts is None or ts < min_seen_ts):
                    min_seen_ts = ts
                if ts < floor and seq_ok:
                    break                             # 时间序可信且早于窗口下界——提前终止
                key = ts.astimezone(_TZ_CN).strftime("%Y-%m-%d")
                if key not in day_set:
                    continue                          # 窗口外的未来日（时钟偏移）：如实不入窗
                caller = str(row.get("caller") or "")
                if not caller:
                    continue
                agg = callers.setdefault(
                    caller, {"calls": 0, "success": 0, "fail": 0, "latSum": 0, "latCnt": 0,
                             "services": {}, "days": {}})
                st = row.get("status")
                ok = 1 if isinstance(st, int) and 200 <= st < 300 else 0
                bad = 1 if isinstance(st, int) and not (200 <= st < 300) else 0
                lat = row.get("latency_ms")
                lat_i = lat if isinstance(lat, int) else None
                ep = str(row.get("endpoint") or "—")

                agg["calls"] += 1
                agg["success"] += ok
                agg["fail"] += bad
                if lat_i is not None:
                    agg["latSum"] += lat_i
                    agg["latCnt"] += 1
                svc = agg["services"].setdefault(ep, [0, 0, 0, 0, 0])
                svc[0] += 1
                svc[1] += ok
                svc[2] += bad
                if lat_i is not None:
                    svc[3] += lat_i
                    svc[4] += 1
                day = agg["days"].setdefault(key, [0, 0, 0])
                day[0] += 1
                day[1] += ok
                day[2] += bad
    return {"days": day_keys, "callers": callers}


# svc 维度字段解析（ADR-0023 第一实施项读侧）：svc:{endpoint}:{metric} 五指标 →
# services 行五元组 [calls,success,fail,latSum,latCnt] 下标（与 metering_scan 同形）。
# 端点用 rpartition 取尾段——路由路径本身不含冒号，即便未来含冒号也不破解析。
_SVC_METRIC_IDX = {"calls": 0, "success": 1, "fail": 2, "lat_sum": 3, "lat_cnt": 4}


def _absorb_svc_fields(h: dict, services: dict) -> None:
    """把单个快照 hash 里的 svc:{endpoint}:{metric} 字段累进 services 聚合体。

    兼容口径：hash 无任何 svc 字段（补丁前旧键）→ services 保持空——
    调用方按「服务维度未就绪」降级显示（「—」），总量列不受影响。
    """
    for raw_field, raw_val in h.items():
        field = raw_field if isinstance(raw_field, str) else raw_field.decode()
        if not field.startswith("svc:"):
            continue
        ep, sep, metric = field[len("svc:"):].rpartition(":")
        if not sep or not ep or metric not in _SVC_METRIC_IDX:
            continue                       # 非 svc 五指标形态：跳过不猜
        try:
            val = int(raw_val or 0)
        except (TypeError, ValueError):
            continue                       # 脏值跳过（同坏行容错口径）
        row = services.setdefault(ep, [0, 0, 0, 0, 0])
        row[_SVC_METRIC_IDX[metric]] += val


def masked_caller(caller: str) -> str:
    """调用主体出口掩码（唯一实现，metering_view 与 usage_report 共用防漂移）。

    登记凭证（sb_/ak_ 前缀）走 mask_appkey；非凭证标识（demo-page/console 等
    非机密）截前 12 位＋…。完整 appkey 不出端点（模块级掩码口径）。
    """
    return appk.mask_appkey(caller) if caller.startswith((appk.SB_PREFIX, appk.AK_PREFIX)) \
        else (caller[:12] + "…")


async def metering_from_stats(redis, *, days: int = 7,
                            now: datetime | None = None,
                            raise_on_error: bool = False) -> dict:
    """从 Redis 统计快照读取计量数据（D20 落地：永不解析原始日志）。

    读取 audit:stats:{YYYYMMDD}:{caller} hash 键，聚合窗口内数据。
    返回格式与 metering_scan 完全一致（{"days": [...], "callers": {...}}），
    使 metering_view 无需区分数据源。

    服务维度（ADR-0023 第一实施项）：hash 内 svc:{endpoint}:* 字段 →
    callers[*]["services"] 真实聚合（此前恒空的缺陷补丁）；补丁前旧键无
    svc 字段 → services 为空（服务列降级「—」，他列不受影响）。

    服务×日矩阵（1b-9 报表专区趋势页专用，metering_scan 无此键——本函数
    唯一有意增量）：callers[*]["svcDays"]＝{日键: {endpoint: 五元组}}，
    用量趋势 dim=service 的逐日曲线数据源；消费方仅限 usage_report。

    统计键缺失（消费者未部署/首日无数据）→ 返回空聚合（非错误）；
    raise_on_error=True（报表专区专用）→ Redis 异常原样上抛（端点据此出
    「降级态」响应，绝不回落 JSONL 现算——ADR-0023 §5 现算禁令），缺省
    False 保持旧行为（返回空聚合，旧端点降级路径不受影响）。
    """
    day_keys = _window_days(now, days)
    # YYYY-MM-DD → YYYYMMDD 转换（Redis 键用紧凑格式）
    day_compact = [d.replace("-", "") for d in day_keys]
    callers: dict[str, dict] = {}

    try:
        # 收集所有需要读取的键
        keys_to_read = []
        key_meta = []  # (day_display, caller) for each key
        for day_c, day_d in zip(day_compact, day_keys):
            # 枚举该日的所有 caller 键（SCAN 匹配）
            pattern = f"{STATS_KEY_PREFIX}{day_c}:*"
            cursor = 0
            while True:
                cursor, batch = await redis.scan(cursor, match=pattern, count=100)
                for k in batch:
                    # 提取 caller：audit:stats:20261015:sb_xxx → sb_xxx
                    prefix_len = len(f"{STATS_KEY_PREFIX}{day_c}:")
                    caller = k[prefix_len:] if isinstance(k, str) else k.decode()[prefix_len:]
                    keys_to_read.append(k)
                    key_meta.append((day_d, caller))
                if cursor == 0:
                    break

        if not keys_to_read:
            return {"days": day_keys, "callers": {}}

        # Pipeline batch read
        pipe = redis.pipeline(transaction=False)
        for k in keys_to_read:
            pipe.hgetall(k)
        results = await pipe.execute()

        for (day_d, caller), h in zip(key_meta, results):
            if not h:
                continue
            agg = callers.setdefault(
                caller, {"calls": 0, "success": 0, "fail": 0, "latSum": 0, "latCnt": 0,
                         "services": {}, "days": {}, "svcDays": {}})
            calls = int(h.get("calls", 0) or 0)
            success = int(h.get("success", 0) or 0)
            fail = int(h.get("fail", 0) or 0)
            lat_sum = int(h.get("lat_sum", 0) or 0)
            lat_cnt = int(h.get("lat_cnt", 0) or 0)

            agg["calls"] += calls
            agg["success"] += success
            agg["fail"] += fail
            agg["latSum"] += lat_sum
            agg["latCnt"] += lat_cnt
            day = agg["days"].setdefault(day_d, [0, 0, 0])
            day[0] += calls
            day[1] += success
            day[2] += fail
            # 服务维度（ADR-0023 第一实施项）：svc:{endpoint}:* 字段 → services
            _absorb_svc_fields(h, agg["services"])
            # 服务×日矩阵（趋势页 dim=service 数据源）：同字段按日再留一份
            _absorb_svc_fields(h, agg["svcDays"].setdefault(day_d, {}))
    except Exception:
        if raise_on_error:
            raise   # 报表专区：上抛由端点出降级态（现算禁令，不回落 JSONL）
        # Redis 不可用→返回空聚合（metering_api 可决定是否降级 JSONL）
        import logging
        logging.getLogger("oneserve.service").warning(
            "Redis 统计快照读取失败，返回空聚合", exc_info=True)
        return {"days": day_keys, "callers": {}}

    return {"days": day_keys, "callers": callers}


def _avg_ms(lat_sum: int, lat_cnt: int) -> float | None:
    """平均耗时（1 位小数）；无耗时样本＝None（不造 0 冒充均值）。"""
    return round(lat_sum / lat_cnt, 1) if lat_cnt else None


def metering_view(doc: dict, scan: dict) -> dict:
    """聚合结果 → 出端点视图：掩码＋登记名合并＋趋势补零对齐＋按调用量降序。

    doc＝appkeys 注册表（{"keys":[…]}，热加载态）；登记命中出 kid/appName/tenant，
    未登记（demo-page/console 等历史标识）如实标 registered=False——不硬造归属。
    """
    reg = {str(k.get("appkey")): k for k in doc.get("keys") or [] if k.get("appkey")}
    day_keys = scan["days"]
    items = []
    tot = {"calls": 0, "success": 0, "fail": 0, "latSum": 0, "latCnt": 0}
    for caller, agg in scan["callers"].items():
        rec = reg.get(caller)
        masked = masked_caller(caller)   # 掩码唯一实现（usage_report 同源共用）
        item = {
            "callerMasked": masked,
            "kid": appk.record_kid(caller) if rec else None,
            "appName": (rec or {}).get("app_name"),
            "tenant": (rec or {}).get("tenant"),
            "registered": rec is not None,
            "calls": agg["calls"], "success": agg["success"], "fail": agg["fail"],
            "avgMs": _avg_ms(agg["latSum"], agg["latCnt"]),
            # 趋势补零对齐窗口（连续轴不跳点；口径同 my_access.usage_scan）
            "trend": [{"date": d, "calls": (agg["days"].get(d) or [0, 0, 0])[0],
                       "success": (agg["days"].get(d) or [0, 0, 0])[1],
                       "fail": (agg["days"].get(d) or [0, 0, 0])[2]} for d in day_keys],
            "services": sorted(
                ({"endpoint": ep, "calls": c, "success": s, "fail": f,
                  "avgMs": _avg_ms(ls, lc)} for ep, (c, s, f, ls, lc) in agg["services"].items()),
                key=lambda x: (-x["calls"], x["endpoint"])),
        }
        items.append(item)
        tot["calls"] += agg["calls"]
        tot["success"] += agg["success"]
        tot["fail"] += agg["fail"]
        tot["latSum"] += agg["latSum"]
        tot["latCnt"] += agg["latCnt"]
    items.sort(key=lambda x: (-x["calls"], x["callerMasked"]))
    return {
        "days": len(day_keys),
        "dayKeys": day_keys,
        "total": {"calls": tot["calls"], "success": tot["success"], "fail": tot["fail"],
                  "avgMs": _avg_ms(tot["latSum"], tot["latCnt"])},
        "callers": items,
        "caliber": METERING_CALIBER,
    }


def metering_summary(doc: dict, path: Path, *, days: int = 7,
                     now: datetime | None = None) -> dict:
    """端点主入口：倒序聚合＋掩码视图（days 非法抛 ValueError → 端点 400）。"""
    return metering_view(doc, metering_scan(path, days=days, now=now))
