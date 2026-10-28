# -*- coding: utf-8 -*-
"""压测前自动发现（P3 压测工具通用化）：服务登记解析 → Redis 键池取样 → meta.json。

一句用法（由 run_ladder.sh 编排调用，也可单跑）：
  python3 loadtest/discover.py --service v1-svc-usr-basic-info \
      --out-dir loadtest/results/<ts> [--keys 2000] [--appkey sb_xxx]

做五件事（全部只读，不写任何服务端状态）：
  1) 服务登记解析（P1-4）：docker exec <service 容器> cat /app/services.yaml →
     优先 console_services（发布台权威区）按 id/route 命中，回落 services 运行时区；
     route 直取登记值（/v1/svc/* 口径，实测 /v1/query/* 经网关 404，不做拼接）；
  2) 授权/配额前置门禁（P0-1）：docker exec cat /app/registry/appkeys.yaml →
     该 appkey 的 grants 必须含目标 service（否则 EXIT 10「未授权」）；
     quota_qps 低于梯子最低档位 → EXIT 11（--force 才放行，429 单独计数口径）；
  3) 键池取样（P0-2）：tags:<ns>:latest 解析现役版本（缺失即拒跑，禁止跨版本/
     跨表回落），SCAN tags:<ns>:<ver>:* 提取实体键（缺省上限 2000，dedupe，
     as_received 原样不归一）落 <out-dir>/keys.txt；
  4) 凭证配额记录：quota_qps/quota_daily/status 连同门禁结论写 meta（掩码口径）；
  5) 落 <out-dir>/meta.json（报告生成的环境事实来源；appkey 只存掩码，secret 永不落盘）。

EXIT 码：0 正常；2 环境致命；10 凭证未授权目标服务；11 quota_qps 低于最低档位；
         12 limit-conn 并发闸低于梯子最高档（量纲错配防呆，2026-10-14）
（--force 可越过 11，此时 meta.gate.forced=true 供报告如实展示）。
纯函数（resolve_service/extract_entity_keys/evaluate_gate/mask_secret）无 IO，
可单测（loadtest/test_loadtest_tools.py）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml

# limit-conn 并发闸阈值单一事实来源＝service/app/ratelimit.py（consumer_plugins 同款规则：
# 配额（qps/daily 任一）登记即挂 limit-conn，conn=DEFAULT_MAX_CONCURRENT 平台常量）。
# A窗 2026-10-19（压测页容器内触发使能）：宿主真源＝仓库根/service；容器内
# loadtest 挂在 /app/loadtest，app 包就在 /app（仓库根＝服务根同体）——按在场性择一。
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "service" if (_ROOT / "service" / "app").is_dir() else _ROOT))
from app import ratelimit as _rl  # noqa: E402

DEFAULT_KEYS = 2000
_KEY64_RE = re.compile(r"^[0-9a-f]{64}$")
# 数据键形状：<ns>:<version>:<entity_key>；ns 自身可含冒号（tags:usr_basic_info）
_VERSION_RE = re.compile(r"^\d{8,}$")


def mask_secret(v: str) -> str:
    """凭证掩码：保留头 7 尾 4（同门户 sb_d2a3****8a1b 口径）；短值全掩。"""
    if not v:
        return ""
    if len(v) <= 11:
        return v[:2] + "****"
    return f"{v[:7]}****{v[-4:]}"


def resolve_service(doc: dict, ident: str) -> dict | None:
    """服务登记命中（P1-4 修正）：优先 console_services（发布台权威区，id 字段），
    回落 services（运行时区，service_id 字段）；route 直取登记值（/v1/svc/* 口径，
    实测 /v1/query/* 经网关 404——任何 /v1/query 拼接都不做）。

    命中口径：service_id 精确＝route 精确＝route 去首斜杠；命中即归一化。
    """
    want = ident.strip()
    for section, id_key in (("console_services", "id"), ("services", "service_id")):
        for s in doc.get(section) or []:
            sid = str(s.get(id_key, "")).strip()
            route = str(s.get("route", "")).strip()
            if want in (sid, route, route.lstrip("/")):
                params = s.get("params") or []
                kv = (s.get("backend") or {}).get("kv") or {}
                return {
                    "service_id": sid or route.strip("/").replace("/", "-"),
                    "name": str(s.get("name", "")),
                    "route": route,
                    "method": str(s.get("method", "POST")).upper(),
                    "key_field": str(params[0]["name"]) if params and params[0].get("name") else "key",
                    "namespace": str(kv.get("namespace", "")),
                    "field_space": str(s.get("field_space", "")),
                    "status": str(s.get("status", "")),
                    "registry_section": section,
                }
    return None


def extract_entity_keys(scan_lines: list[str], ns: str) -> tuple[list[str], dict]:
    """SCAN 行 → 实体键池（dedupe 保序）＋计数。

    数据键＝<ns>:<version>:<entity>（version＝8+ 位数字，如 20261008）；
    别名/元数据键（<ns>:latest、sync:ver:* 等）不匹配数据键形状，自然被剔除。
    entity 段大小写口径（P0-2 修正）：64hex 键统一小写（phone_sm3 存储约定，大小写
    归一去重）；非 64hex 键型 as_received 原样保留（改写会造成跨键池 miss＝空投影
    假达标）。起压前命中自检（run_ladder.sh）为最终守门。
    """
    seen: dict[str, None] = {}          # 保序 dedupe
    versions: Counter = Counter()
    skipped_meta = 0
    prefix = ns + ":"
    for raw in scan_lines:
        key = raw.strip()
        if not key or not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        parts = rest.split(":")
        if len(parts) >= 2 and _VERSION_RE.match(parts[0]):
            versions[parts[0]] += 1
            ent = parts[-1]
            low = ent.lower()
            if _KEY64_RE.match(low):
                seen.setdefault(low, None)      # 64hex 键统一小写（存储约定）
            elif re.match(r"^[A-Za-z0-9_.-]{1,128}$", ent):
                seen.setdefault(ent, None)      # 其他键型原样保留（大小写敏感）
        else:
            skipped_meta += 1
    stats = {
        "redis_keys_total": len([r for r in scan_lines if r.strip()]),
        "data_keys": sum(versions.values()),
        "versions": dict(versions),
        "meta_or_alias_keys": skipped_meta,
        "unique_entities": len(seen),
    }
    return list(seen), stats


# ---------- docker 只读取数 ----------
# A窗 2026-10-19（压测页容器内触发使能）：宿主＝docker exec 读数；服务容器内＝本地
# 直读（services/appkeys 就在 /app；Redis 走 REDIS_URL 同栈内网，redis 客户端为
# 服务镜像内建依赖）。按在场性择一，宿主行为不变。

_IN_CONTAINER = Path("/app/services.yaml").is_file() and Path("/app/registry/appkeys.yaml").is_file()


def _redis_local():
    import redis  # noqa: PLC0415 —— 仅容器内分支触达（宿主直跑不依赖）
    return redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://oneserve-redis:6379/0"),
                                decode_responses=True)


def _docker_exec(container: str, argv: list[str]) -> str:
    proc = subprocess.run(["docker", "exec", container, *argv],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"docker exec {container} {argv} 失败：{proc.stderr.strip()[:300]}")
    return proc.stdout


def _load_services_yaml(container: str) -> dict:
    if _IN_CONTAINER:
        return yaml.safe_load(Path("/app/services.yaml").read_text(encoding="utf-8")) or {}
    return yaml.safe_load(_docker_exec(container, ["cat", "/app/services.yaml"])) or {}


def _scan_redis(container: str, pattern: str) -> list[str]:
    if _IN_CONTAINER:
        return list(_redis_local().scan_iter(match=pattern, count=500))
    out = _docker_exec(container, ["redis-cli", "--scan", "--pattern", pattern])
    return out.splitlines()


def _redis_get(container: str, key: str) -> str | None:
    if _IN_CONTAINER:
        val = _redis_local().get(key)
        return val if val else None
    proc = subprocess.run(["docker", "exec", container, "redis-cli", "GET", key],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    val = proc.stdout.strip()
    return val if val and val != "(nil)" else None


def _load_appkeys(container: str) -> dict:
    if _IN_CONTAINER:
        return yaml.safe_load(Path("/app/registry/appkeys.yaml").read_text(encoding="utf-8")) or {}
    return yaml.safe_load(_docker_exec(container, ["cat", "/app/registry/appkeys.yaml"])) or {}


def lookup_credential(doc: dict, appkey: str) -> dict:
    """凭证登记命中（纯函数）：返回配额/状态/grants；未命中返回空 dict（如实，不揣测）。"""
    for k in (doc.get("keys") or []):
        if str(k.get("appkey", "")) == appkey:
            return {
                "quota_qps": k.get("quota_qps"),
                "quota_daily": k.get("quota_daily"),
                "status": k.get("status"),
                "expires_at": k.get("expires_at"),
                "tenant": k.get("tenant"),
                "grants": [
                    {"service": str(g.get("service", "")),
                     "endpoint": str(g.get("endpoint", "")),
                     "fields": g.get("fields") or []}
                    for g in (k.get("grants") or [])
                ],
            }
    return {}


def limit_conn_of(cred: dict) -> int | None:
    """凭证的网关并发闸值（纯函数，与 ratelimit.consumer_plugins 同规则）。

    配额（quota_qps/quota_daily 任一）登记即挂 limit-conn，conn＝平台常量
    DEFAULT_MAX_CONCURRENT；两档都未登记＝不限流（consumer 只挂 key-auth），返回 None。
    """
    qps = cred.get("quota_qps")
    daily = cred.get("quota_daily")
    registered = (isinstance(qps, int) and qps > 0) or (isinstance(daily, int) and daily > 0)
    return _rl.DEFAULT_MAX_CONCURRENT if registered else None


def evaluate_gate(cred: dict, service_id: str, min_level: int,
                  max_level: int = 0) -> tuple[str, str]:
    """授权/配额/并发闸前置门禁（P0-1＋2026-10-14 并发闸防呆，纯函数）。

    返回 (decision, message)，decision ∈ allow | deny_grant | deny_quota | deny_conn：
      allow      grants 含目标服务，且 quota_qps 不低于最低档位，且并发闸不压档
                 （quota_qps 空＝不限流登记，consumer_plugins 返回 {}，如实放行）；
      deny_grant 凭证未登记或 grants 不含目标服务 → 拒跑（调用方 EXIT 10）；
      deny_quota quota_qps 低于最低档位 → 拒跑（调用方 EXIT 11；
                 --force 可降级为警告放行，429 单独计数、不入服务端错误口径）；
      deny_conn  网关 limit-conn 并发闸低于梯子最高档 → 拒跑（调用方 EXIT 12；
                 量纲错配防呆——档位并发超并发闸时，高档位实测的是闸不是服务容量；
                 --force 可降级放行，报告按有效 RPS/429 分列如实呈现）。
    """
    grants = cred.get("grants") or []
    if not any(str(g.get("service", "")) == service_id for g in grants):
        note = "" if grants else "（凭证未登记于 appkeys.yaml）"
        return "deny_grant", f"该凭证未授权服务 {service_id}，请在凭证页签发/授权{note}"
    qps = cred.get("quota_qps")
    if isinstance(qps, int) and qps > 0 and min_level and qps < min_level:
        return ("deny_quota",
                f"当前凭证每秒配额 {qps} 低于档位（最低档 {min_level}），"
                f"压测将被 429 熔断失真；请管理员调高配额或换压测专用凭证"
                f"（确认继续请加 --force）")
    conn = limit_conn_of(cred)
    if conn is not None and max_level and max_level > conn:
        return ("deny_conn",
                f"网关并发闸 limit-conn={conn}（配额登记即挂，平台常量）低于梯子最高档 "
                f"{max_level}——高档位实测的是并发闸而非服务容量（量纲错配）；"
                f"请调低档位至 ≤{conn}、提并发上限或确认加 --force（429 分列如实计数）")
    return "allow", ""


def main() -> int:
    ap = argparse.ArgumentParser(description="压测自动发现：服务登记解析＋Redis 键池取样")
    ap.add_argument("--service", required=True, help="service_id 或 route（如 v1-svc-usr-basic-info）")
    ap.add_argument("--out-dir", required=True, help="本次结果目录（loadtest/results/<ts>）")
    ap.add_argument("--keys", type=int, default=DEFAULT_KEYS, help=f"键池上限（默认 {DEFAULT_KEYS}）")
    ap.add_argument("--appkey", default="", help="门禁校验用（只在 meta 存掩码）")
    ap.add_argument("--cred-source", default="",
                    help="凭证来源描述（env:ONESERVE_APPKEY/--appkey/interactive）")
    ap.add_argument("--min-level", type=int, default=0,
                    help="梯子最低档位（quota_qps 门禁比较基准；0＝不比对，仅授权校验）")
    ap.add_argument("--max-level", type=int, default=0,
                    help="梯子最高档位（limit-conn 并发闸防呆基准；0＝不比对）")
    ap.add_argument("--force", action="store_true",
                    help="quota_qps 低于最低档位时强制放行（meta.gate.forced=true，429 单独计数口径）")
    ap.add_argument("--host", default="", help="被测网关地址（记入 meta）")
    ap.add_argument("--levels", default="", help="梯子档位 csv（记入 meta）")
    ap.add_argument("--secs", type=int, default=70, help="单档窗口秒数（记入 meta）")
    ap.add_argument("--ramp", type=int, default=10, help="单档起压秒数（记入 meta）")
    ap.add_argument("--warmup-users", type=int, default=50, help="预热并发（记入 meta）")
    ap.add_argument("--warmup-secs", type=int, default=30, help="预热秒数（记入 meta）")
    ap.add_argument("--service-container", default="oneserve-service")
    ap.add_argument("--redis-container", default="oneserve-redis")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = _load_services_yaml(args.service_container)
    svc = resolve_service(doc, args.service)
    if svc is None:
        ids = [str(s.get("service_id", "?")) for s in doc.get("services", [])]
        print(f"[discover] 服务登记未命中：{args.service}；在册：{', '.join(ids)}",
              file=sys.stderr)
        return 2
    if not svc["namespace"]:
        print(f"[discover] {svc['service_id']} 无 KV namespace（非点查型服务，暂不支持）",
              file=sys.stderr)
        return 2

    # ---- P0-1 前置门禁：grants 授权校验＋quota_qps 档位比对（先于任何 Redis 取样） ----
    cred_full: dict = {}
    decision, gate_msg = "skip", "未提供凭证，跳过门禁（run_ladder.sh 探针仍兜底）"
    if args.appkey:
        cred_full = lookup_credential(_load_appkeys(args.service_container), args.appkey)
        decision, gate_msg = evaluate_gate(cred_full, svc["service_id"], args.min_level,
                                            args.max_level)
        if decision == "deny_grant":
            print(f"[gate] ✗ {gate_msg}", file=sys.stderr)
            return 10
        if decision == "deny_quota" and not args.force:
            print(f"[gate] ✗ {gate_msg}", file=sys.stderr)
            return 11
        if decision == "deny_conn" and not args.force:
            print(f"[gate] ✗ {gate_msg}", file=sys.stderr)
            return 12
        if decision in ("deny_quota", "deny_conn"):    # --force 放行：警告不拦
            print(f"[gate] ⚠ {gate_msg.split('（确认')[0]}"
                  f"；--force 已确认继续（429 单独计数，不入服务端错误口径）")
        else:
            print(f"[gate] ✓ 凭证已授权 {svc['service_id']}；quota_qps="
                  f"{cred_full.get('quota_qps', '—')}｜最低档 {args.min_level}")

    # ---- P0-2 键池绑定目标表：namespace 的 latest 版本空间，禁止跨版本/跨表回落 ----
    ns = svc["namespace"]
    version = _redis_get(args.redis_container, f"{ns}:latest")
    if not version:
        print(f"[discover] {ns}:latest 别名缺失：无法定位现役版本空间，拒跑"
              f"（禁止跨版本/跨表 SCAN——跨表键在授权投影下全 miss＝空投影假达标，"
              f"对抗复核 P0-2）", file=sys.stderr)
        return 2
    lines = _scan_redis(args.redis_container, f"{ns}:{version}:*")
    ver_note = f"现役版本 {version}（{ns}:latest 解析；键池严格绑定该版本空间）"
    keys, stats = extract_entity_keys(lines, ns)
    if not keys:
        print(f"[discover] 命名空间 {ns} 无数据键（先造数/装载）", file=sys.stderr)
        return 2
    sampled = keys[: args.keys]

    keys_file = out_dir / "keys.txt"
    keys_file.write_text("\n".join(sampled) + "\n", encoding="utf-8")

    # 凭证只落掩码＋配额/状态（grants 字段白名单不入 meta——报告无需且从简）；
    # 门禁结论单独成节，报告据此声明「授权通过/低配额强制」。
    cred: dict = {"appkey_masked": mask_secret(args.appkey), "source": args.cred_source}
    if cred_full:
        cred.update({k: cred_full.get(k) for k in
                     ("quota_qps", "quota_daily", "status", "expires_at", "tenant")})
    gate = {
        "authorized": (decision != "deny_grant") if args.appkey else None,
        "quota_qps": cred_full.get("quota_qps") if cred_full else None,
        "min_level": args.min_level,
        "max_level": args.max_level,
        "limit_conn": limit_conn_of(cred_full) if cred_full else None,
        "quota_below_level": decision == "deny_quota",
        "conn_below_level": decision == "deny_conn",
        "forced": decision in ("deny_quota", "deny_conn") and args.force,
        "message": gate_msg if decision in ("deny_grant", "deny_quota", "deny_conn", "skip")
                   else "授权与配额通过",
    }

    meta = {
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": "loadtest P3 通用化（对抗复核 P0/P1 修正）",
        "service": svc,
        "redis": {"namespace": ns, "live_version": version, "version_note": ver_note,
                  "scan_pattern": f"{ns}:{version}:*", **stats},
        "keys_sampled": len(sampled),
        "keys_file": keys_file.name,
        "credential": cred,
        "gate": gate,
        "host": args.host,
        "ladder": {"levels": args.levels, "secs": args.secs, "ramp": args.ramp,
                   "warmup_users": args.warmup_users, "warmup_secs": args.warmup_secs},
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")

    print(f"[discover] 服务 {svc['service_id']}（{svc['name']}）→ {svc['method']} {svc['route']}"
          f"｜键字段 {svc['key_field']}")
    print(f"[discover] {ver_note}；Redis 键 {stats['redis_keys_total']}（数据键"
          f" {stats['data_keys']}·唯一实体 {stats['unique_entities']}）→ 取样 {len(sampled)}"
          f" → {keys_file}")
    qps = cred.get("quota_qps")
    if isinstance(qps, int) and qps > 0:
        note = (f"；⚠ quota_qps={qps} 偏低，压测流量将被网关限流（429），"
                f"如需测后端容量请临时调高配额" if qps < 100 else "")
        print(f"[discover] 凭证 {cred['appkey_masked']}：quota_qps={qps}"
              f" quota_daily={cred.get('quota_daily')} status={cred.get('status')}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
