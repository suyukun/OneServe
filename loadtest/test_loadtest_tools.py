# -*- coding: utf-8 -*-
"""压测工具纯函数判据（P3）：discover/summarize/report_gen 的可单测面。

范围：只测纯函数（无 docker/无网络/无 locust），数据用最小合成样本；
run_ladder→report 全链路真跑属冒烟（见 docs/plans/P3-压测通用化回执-20261008.md）。
运行：python3 loadtest/test_loadtest_tools.py（或 pytest loadtest/test_loadtest_tools.py）。
注意：本套件放 loadtest/ 而非 tests/——门禁动态枚举 tests/test_*.py，压测工具
自测不入门禁（避免给 CI 加 docker/网络耦合）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discover  # noqa: E402
import report_gen  # noqa: E402
import summarize  # noqa: E402

FAILURES = None


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# ---------- discover.resolve_service / extract_entity_keys / mask / credential ----------

def test_resolve_service_by_id_and_route():
    doc = {"services": [
        {"service_id": "v1-svc-usr-basic-info", "name": "用户基本信息查询",
         "route": "/v1/svc/usr-basic-info", "method": "POST",
         "params": [{"name": "key", "type": "string(64)"}],
         "backend": {"kv": {"namespace": "tags:usr_basic_info"}}, "status": "published"},
        {"service_id": "v1-svc-news", "name": "资讯", "route": "/v1/svc/news"},
    ]}
    by_id = discover.resolve_service(doc, "v1-svc-usr-basic-info")
    by_route = discover.resolve_service(doc, "/v1/svc/usr-basic-info")
    _check(by_id is not None and by_route == by_id, "id/route 命中应等价")
    _check(by_id["route"] == "/v1/svc/usr-basic-info", "route 解析")
    _check(by_id["key_field"] == "key", "入参键字段取 params[0].name")
    _check(by_id["namespace"] == "tags:usr_basic_info", "KV namespace 解析")
    _check(by_id["method"] == "POST", "method 解析")
    _check(discover.resolve_service(doc, "v1-svc-miss") is None, "未命中返回 None")


def test_resolve_service_prefers_console_services():
    # P1-4：console_services（发布台权威区，id 字段）优先于 services 运行时区；
    # route 直取登记值（/v1/svc/*），不做 /v1/query/* 拼接（实测 404）
    doc = {"console_services": [
               {"id": "v1-svc-x", "name": "发布台条目", "route": "/v1/svc/x", "method": "POST",
                "params": [{"name": "key"}], "backend": {"kv": {"namespace": "tags:x"}}}],
           "services": [
               {"service_id": "v1-svc-x", "name": "运行时旧条目", "route": "/v1/svc/x-old"}]}
    hit = discover.resolve_service(doc, "v1-svc-x")
    _check(hit is not None and hit["route"] == "/v1/svc/x", "console_services 优先命中")
    _check(hit["registry_section"] == "console_services", "命中区留痕")
    by_route = discover.resolve_service(doc, "/v1/svc/x")
    _check(by_route["name"] == "发布台条目", "route 命中同取 console_services")


def test_extract_entity_keys_filters_meta_and_dedupes():
    ns = "tags:usr_basic_info"
    k1 = "a" * 63 + "1"
    k2 = "b" * 63 + "2"
    lines = [
        f"{ns}:latest",
        f"{ns}:20261008:{k1}",
        f"{ns}:20261008:{k1.upper()}",        # 64hex 大小写归一去重（phone_sm3 存储约定）
        f"{ns}:20261008:{k2}",
        f"{ns}:20260901:{k2}",                # 历史版本同实体跨版本去重（键池按 latest 绑定后不出现）
        f"sync:ver:usr_basic_info:20261008",
        "",
    ]
    keys, stats = discover.extract_entity_keys(lines, ns)
    _check(keys == [k1, k2], f"64hex 归一去重保序：{keys}")
    _check(stats["data_keys"] == 4, "数据键按版本计数")
    _check(stats["versions"] == {"20261008": 3, "20260901": 1}, "版本分布")
    _check(stats["unique_entities"] == 2, "唯一实体数")
    _check(stats["meta_or_alias_keys"] == 1, "latest 别名计入 meta")


def test_extract_entity_keys_accepts_non_hex_key():
    ns = "tags:proxy_ns"
    keys, _ = discover.extract_entity_keys(
        [f"{ns}:20261008:partner-id-001", f"{ns}:20261008:Partner-Id-001"], ns)
    _check(keys == ["partner-id-001", "Partner-Id-001"],
           "非 64hex 键 as_received 原样保留（大小写不同＝不同键，改写会 miss；新键型兼容）")


def test_mask_secret():
    _check(discover.mask_secret("***REMOVED***") == "sb_d2a3****8a1b", "掩码口径头7尾4")
    _check(discover.mask_secret("short") == "sh****", "短值全掩")
    _check(discover.mask_secret("") == "", "空值")


def test_lookup_credential():
    doc = {"keys": [{"appkey": "sb_x", "quota_qps": 5, "quota_daily": 1000,
                     "status": "active", "expires_at": "2026-10-15", "tenant": "t",
                     "grants": [{"service": "v1-svc-usr-basic-info",
                                 "endpoint": "/v1/svc/usr-basic-info",
                                 "fields": ["age_band"]}]}]}
    hit = discover.lookup_credential(doc, "sb_x")
    _check(hit["quota_qps"] == 5 and hit["status"] == "active", "凭证配额命中")
    _check(hit["grants"][0]["service"] == "v1-svc-usr-basic-info", "grants 随凭证返回")
    _check(discover.lookup_credential(doc, "sb_y") == {}, "未命中返回空 dict")


def test_evaluate_gate():
    # P0-1：授权门禁——grants 不含目标服务 → deny_grant（EXIT 10）
    d1, m1 = discover.evaluate_gate(
        {"quota_qps": 500, "grants": [{"service": "v1-svc-other"}]},
        "v1-svc-usr-basic-info", 20)
    _check(d1 == "deny_grant" and "该凭证未授权服务 v1-svc-usr-basic-info" in m1
           and "凭证页签发/授权" in m1, f"未授权拒跑文案：{m1}")
    d0, m0 = discover.evaluate_gate({}, "v1-svc-usr-basic-info", 20)
    _check(d0 == "deny_grant" and "未登记" in m0, "凭证未登记同样拒跑")

    ok_grant = {"quota_qps": 500, "grants": [{"service": "v1-svc-usr-basic-info"}]}
    d2, _ = discover.evaluate_gate(ok_grant, "v1-svc-usr-basic-info", 100)
    _check(d2 == "allow", "配额不低于最低档位放行")

    # quota_qps 低于最低档位 → deny_quota（EXIT 11；--force 由调用方降级放行）
    low = {"quota_qps": 5, "grants": [{"service": "v1-svc-usr-basic-info"}]}
    d3, m3 = discover.evaluate_gate(low, "v1-svc-usr-basic-info", 20)
    _check(d3 == "deny_quota" and "每秒配额 5 低于档位（最低档 20）" in m3
           and "429 熔断失真" in m3 and "调高配额或换压测专用凭证" in m3,
           f"低配额警告文案：{m3}")
    d4, _ = discover.evaluate_gate({"quota_qps": 20, "grants": [{"service": "v1-svc-usr-basic-info"}]},
                                   "v1-svc-usr-basic-info", 20)
    _check(d4 == "allow", "quota_qps＝最低档位属不低于，放行")
    d5, _ = discover.evaluate_gate({"quota_qps": None, "grants": [{"service": "v1-svc-usr-basic-info"}]},
                                   "v1-svc-usr-basic-info", 500)
    _check(d5 == "allow", "quota_qps 空＝不限流登记，放行")


def test_evaluate_gate_limit_conn():
    """2026-10-14 并发闸防呆：配额登记即挂 limit-conn（平台常量），最高档超闸 → deny_conn。"""
    reg = {"quota_qps": 500, "quota_daily": 10000,
           "grants": [{"service": "v1-svc-usr-basic-info"}]}
    _check(discover.limit_conn_of(reg) == 20, "配额登记 → limit-conn=平台常量 20")
    _check(discover.limit_conn_of({"quota_qps": None, "quota_daily": None,
                                   "grants": []}) is None, "两档配额都未登记 → 无并发闸")
    d1, m1 = discover.evaluate_gate(reg, "v1-svc-usr-basic-info", 20, 100)
    _check(d1 == "deny_conn" and "limit-conn=20" in m1 and "量纲错配" in m1
           and "最高档 100" in m1, f"并发闸低于最高档拒跑文案：{m1}")
    d2, _ = discover.evaluate_gate(reg, "v1-svc-usr-basic-info", 20, 20)
    _check(d2 == "allow", "最高档＝并发闸属不超，放行")
    d3, _ = discover.evaluate_gate({"quota_qps": None, "quota_daily": None,
                                    "grants": [{"service": "v1-svc-usr-basic-info"}]},
                                   "v1-svc-usr-basic-info", 0, 800)
    _check(d3 == "allow", "无配额登记＝无 limit-conn，不限档放行")


# ---------- summarize：失败次数口径 / 容器角色 / meta 驱动 ----------

def test_load_fail_count_prefers_summary_log_error_report():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "locust_20_summary.log").write_text(
            "Aggregated x 1 2 3 4 5 6 7 8 9 10 11 12 308172\n"
            "\n"
            "Error report\n"
            "# occurrences      Error\n"
            "------------------|-------\n"
            "307822             POST n: HTTPError('429 Client Error: Too Many Requests')\n"
            "7                  POST n: HTTPError('502 Server Error: Bad Gateway')\n"
            "------------------|-------\n", encoding="utf-8")
        (p / "locust_20_failures.csv").write_text(
            "Method,Name,Error,Occurrences\n"
            "POST,n,HTTPError('429 ...'),303937\n"
            "POST,n,HTTPError('502 ...'),7\n", encoding="utf-8")
        _check(summarize.load_fail_count(p, 20) == 307829,
               "summary.log Error report 优先（冒烟实证 CSV 少计 3885）")
        (p / "locust_20_summary.log").unlink()
        _check(summarize.load_fail_count(p, 20) == 303944,
               "缺 Error report 段回落 failures.csv Occurrences 合计（旧按行数只数 2）")


def test_level_stats_rps_split_valid_vs_429():
    """2026-10-14 分列口径：有效 RPS＝(总数-错误)/窗口；429 RPS/命中率单列（429≠服务吞吐）。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "locust_100_summary.log").write_text(
            "Aggregated 10 20 30 40 50 60 70 80 90 100 110 120 308172\n"
            "\n"
            "Error report\n"
            "# occurrences      Error\n"
            "------------------|-------\n"
            "307822             POST n: HTTPError('429 Client Error: Too Many Requests')\n"
            "7                  POST n: HTTPError('502 Server Error: Bad Gateway')\n"
            "------------------|-------\n", encoding="utf-8")
        s = summarize.load_level_stats(p, 100, secs=70)
        _check(s["fails"] == 307829 and s["fails_429"] == 307822,
               "429 只计限流类错误（502 不计入 429）")
        _check(abs(s["rps"] - 308172 / 70) < 1e-6, "总 RPS＝总数/窗口")
        _check(abs(s["rps_valid"] - (308172 - 307829) / 70) < 1e-6,
               "有效 RPS＝(总数-全部错误)/窗口")
        _check(abs(s["rps_429"] - 307822 / 70) < 1e-6, "429 RPS＝429次/窗口")
        _check(abs(s["hit_429_pct"] - 307822 / 308172 * 100) < 1e-6, "429 命中率＝429次/总数")


def test_container_role_suffix_match():
    _check(summarize._container_role("oneserve-apisix") == "apisix", "apisix 后缀")
    _check(summarize._container_role("oneserve-load-service") == "service", "service 后缀")
    _check(summarize._container_role("any-project-redis") == "redis", "redis 后缀")
    _check(summarize._container_role("oneserve-batch-worker") is None, "无关容器不归位")


def test_level_secs_and_default_levels_from_meta():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "meta.json").write_text('{"ladder": {"levels": "20,60", "secs": 70}}', encoding="utf-8")
        _check(summarize.level_secs(p) == 70, "secs 取 meta")
        _check(summarize.default_levels(p) == [20, 60], "档位取 meta csv")
        (p / "meta.json").unlink()
        (p / "locust_100_stats.csv").write_text("x\n", encoding="utf-8")
        _check(summarize.default_levels(p) == [100], "无 meta 回落 glob（旧目录兼容）")


# ---------- report_gen：失败分类 / 六节渲染 / 安全面 ----------

def test_classify_failures_by_status():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "locust_20_summary.log").write_text(
            "Error report\n"
            "# occurrences      Error\n"
            "------------------|-------\n"
            "340                POST n: HTTPError('429 Too Many Requests')\n"
            "7                  POST n: HTTPError('500 Internal Server Error')\n"
            "3                  POST n: Connection reset by peer\n", encoding="utf-8")
        by = report_gen.classify_failures(p, 20)
        _check(by.get("429") == 340 and by.get("500") == 7 and by.get("other") == 3,
               f"按状态码归并（Error report 口径）：{by}")


def _synthetic_data():
    return {
        "generated_at": "2026-10-08 20:00:00 +0800",
        "run_dir": "loadtest/results/smoke",
        "meta": {"host": "http://localhost:9080", "keys_file": "keys.txt"},
        "svc": {"service_id": "v1-svc-usr-basic-info", "name": "用户基本信息查询",
                 "route": "/v1/svc/usr-basic-info", "method": "POST", "status": "published"},
        "cred": {"appkey_masked": "sb_d2a3****8a1b", "source": "--appkey",
                  "quota_qps": 5, "quota_daily": 1000, "status": "active",
                  "expires_at": "2026-10-15"},
        "redis": {"namespace": "tags:usr_basic_info", "live_version": "20261008",
                   "version_note": "现役版本 20261008（latest 别名解析）",
                   "redis_keys_total": 500, "data_keys": 500, "unique_entities": 500},
        "ladder": {"levels": "20", "secs": 70, "ramp": 10, "warmup_users": 5, "warmup_secs": 30},
        "keys_sampled": 500,
        "levels": [{
            "level": 20, "total": 420, "fails": 120, "err_pct": 28.57,
            "rps": 6.0, "rps_tail": 5.9, "p50": 3.1, "p95": 8.2, "p99": 12.5, "max": 40.0,
            "audit_delta": 300, "fail_by_status": {"429": 120},
            "containers": {"apisix": {"cpu_avg": 12.0, "cpu_max": 30.0, "mem_max": 528.0},
                            "service": {"cpu_avg": 40.0, "cpu_max": 80.0, "mem_max": 210.0},
                            "redis": {"cpu_avg": 20.0, "cpu_max": 35.0, "mem_max": 90.0}},
        }],
        "audit": {"endpoint": "service:/v1/query/v1-svc-usr-basic-info",
                   "caller_masked": "sb_d2a3****8a1b", "audit_total_lines": 612029,
                   "bad_lines": 0, "dup_or_desc_seq": 0,
                   "levels": [{"level": 20, "audit_delta": 300, "win_2xx": 300, "win_non2xx": 0,
                                "endpoint_bad": 0, "caller_bad": 0, "cli_total": 420,
                                "cli_2xx": 300, "diff_total_minus_audit": 120, "ok": True}],
                   "verdict": "✓ 通过：各档窗口内审计条数＝服务端处理请求数"},
        "probes": [{"ts": "2026-10-08T20:02:00+0800", "probe": "probe1",
                     "http_code": "200", "time_total_s": "0.004"}],
    }


def test_render_report_six_sections_and_ac():
    md = report_gen.render_report(_synthetic_data())
    for heading in ("## 1. 环境与被测", "## 2. 四档结果表", "## 3. 容器资源表",
                    "## 4. 审计守恒对账表", "## 5. 静置探针", "## 6. 结论与建议"):
        _check(heading in md, f"缺章节：{heading}")
    _check("| 20 | 420 | 120 | 28.57% |" in md, "四档表行（09-21 列口径）")
    _check("| 20 | 12.5 | ✓ |" in md, "AC 判定行 P99<200ms 达标")
    _check("sb_d2a3****8a1b" in md, "凭证掩码入环境节")
    _check("429×120" in md, "错误构成按状态码展示")
    _check("限流是当前绑定约束" in md, "429 主导时瓶颈观察点名限流")
    _check("quota_qps" in md, "配额入环境节")
    # P0-3 隔离声明置顶＋P1-7 延迟双口径声明
    _check(md.index("共享栈压测声明：本机 M0 无隔离栈") < md.index("## 1. 环境与被测"),
           "共享栈声明须置顶（先于任何章节）")
    _check("本机 M0 无隔离栈，结果含门户/管理流量干扰，尾部延迟供参考" in md, "隔离声明全文")
    _check("延迟双口径声明" in md and "审计 latency_ms 实测样本为 0" in md
           and "不引用审计延迟" in md, "延迟双口径声明（P1-7）")
    _check("***REMOVED***" not in md, "明文 appkey 不得出现")


def test_render_html_same_data_and_declarations():
    d = _synthetic_data()
    d["meta"]["gate"] = {"authorized": True, "quota_qps": 5, "min_level": 20,
                          "quota_below_level": True, "forced": True, "message": "…"}
    h = report_gen.render_html(d)
    _check(h.startswith("<!doctype html>"), "自包含 HTML 文档")
    _check("共享栈压测声明：本机 M0 无隔离栈" in h
           and h.index("共享栈压测声明") < h.index("1. 环境与被测"), "HTML 声明置顶（与 MD 同文）")
    _check("延迟双口径声明" in h and "不引用审计延迟" in h, "HTML 延迟双口径声明")
    for frag in ("<h2>2. 四档结果表</h2>", "<h2>4. 审计守恒对账表</h2>",
                 "<h2>6. 结论与建议</h2>", "sb_d2a3****8a1b", "429×120"):
        _check(frag in h, f"HTML 含：{frag}")
    _check("授权门禁" in h and "--force 强制放行" in h, "门禁结论入 HTML 环境节")
    _check("***REMOVED***" not in h, "明文 appkey 不得出现")
    _check("<script" not in h.lower(), "无脚本（纯静态自包含）")
    _check("C8102E" in h, "门户 tokens 配色内联（品牌红）")


def test_render_report_no_secret_leak_and_missing_audit():
    d = _synthetic_data()
    d["audit"] = None
    md = report_gen.render_report(d)
    _check("audit_recon.json 缺失" in md, "审计缺失如实标注")
    _check("***REMOVED***" not in md, "明文 appkey 不得出现在报告")
    _check("sk_" not in md, "secret 前缀不得出现在报告")


def test_render_report_ac_fail_path():
    d = _synthetic_data()
    d["levels"][0].update(p99=273.0, fail_by_status={}, err_pct=0.0, fails=0)
    md = report_gen.render_report(d)
    _check("| 20 | 273.0 | ✗ |" in md, "P99 超标判 ✗")
    _check("未达标，按 §3 资源热点" in md, "建议指向资源热点")


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
