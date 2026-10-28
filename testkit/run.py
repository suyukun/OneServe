#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OneServe testkit 统一入口（设计稿 §6）。

用法（README 有完整参数表）：
  python3 testkit/run.py s1 --mock                 # 演示模式：S1 爬坡面板（MOCK 降级可用）
  python3 testkit/run.py s4                        # 回归模式：冒烟门＋梯子→基线对比报告
  python3 testkit/run.py s0|s2|s3 ...              # 其余场景
  python3 testkit/run.py convert --from-loadtest ../loadtest/results [--sample-report]
  python3 testkit/run.py compare --run <run.json> --baseline <baseline.json>
  python3 testkit/run.py selftest

依赖：locust（已装）＋rich（若缺自动降级 ANSI；装法：pip3 install --target testkit/lib rich）。
运行边界：演示机/跳板机，不进生产；只用演示栈占位 appkey，不接触生产凭证。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_LIB = HERE / "lib"                       # rich 本地安装位（勿装全局）
if _LIB.exists():
    sys.path.insert(0, str(_LIB))

# gevent 打补丁须在任何网络/线程相关 import 之前（locust 程序化 API 前提）
from gevent import monkey; monkey.patch_all()  # noqa: E402

from driver import Driver, load_config, thresholds_of, TESTKIT_DIR  # noqa: E402
from report import baseline_io, render  # noqa: E402


def _levels_arg(s: str | None) -> list[int] | None:
    return [int(x) for x in s.split(",") if x.strip()] if s else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="testkit/run.py", description="OneServe 测试演示套件")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, live_default: str):
        sp.add_argument("--mock", action="store_true", help="强制 MOCK 目标（模拟网关；默认目标不可达时自动降级并标注）")
        sp.add_argument("--host", help="网关地址（默认 config general.host）")
        sp.add_argument("--keyfile", help="键池文件（CSV 首列 64 位十六进制）")
        sp.add_argument("--env-name", help="环境名＝基线隔离键（默认 config general.env_stage）")
        sp.add_argument("--live", dest="mode", action="store_const", const="live",
                        help="实时面板（rich Live；不可用自动降 ANSI 单行）")
        sp.add_argument("--headless", dest="mode", action="store_const", const="headless", help="无面板跑")
        sp.add_argument("--only-summary", dest="mode", action="store_const", const="only-summary",
                        help="只在结束时打表（降级链保底）")
        sp.add_argument("--debug-web", action="store_true", help="附加 locust web UI 调试（127.0.0.1:8089）")
        sp.set_defaults(mode=live_default)

    for name, help_ in (("s0", "冒烟：5 用户×30s，全 200 且 P99<500ms 即绿（栈活性探针/回归门）"),
                        ("s1", "并发爬坡演示：50→100→300，rich 面板（演示窗每档 30s）"),
                        ("s2", "三通道对比：HTTP API / Python SDK(客户端SM3) / 网页通道"),
                        ("s3", "限流演示：A 正常配额 / B 超限，双组分列计数"),
                        ("s4", "回归梯子：冒烟门＋预热＋100/300/500 档→基线对比报告"),
                        ("s5", "批量场景：提交→轮询→交付校验→取消→429 并发拒（M15-7）"),
                        ("s6", "M2 端到端＋冷热加载：选表→加载→对账差异=0→切指针→经网关真调（M2-12）")):
        sp = sub.add_parser(name, help=help_)
        common(sp, live_default="only-summary" if name in ("s5", "s6")
               else ("headless" if name == "s4" else "live"))
        sp.add_argument("--levels", help="档位列表，如 100,300,500（默认 config）")
        sp.add_argument("--secs", type=float, help="每档/阶段秒数（默认 config）")
        sp.add_argument("--quota", type=int, help="S3 B 组配额/窗口（演示假想值，默认 300 待定）")
        sp.add_argument("--skip-smoke", action="store_true", help="s4 跳过冒烟门（默认先跑 S0）")
        if name == "s6":   # s6 真链路专用入参（隔离环境由 scripts/m212_testkit_e2e.sh 起好）
            sp.add_argument("--svc", help="隔离 service 直连地址（默认 config [s6].svc）")
            sp.add_argument("--gw", help="隔离网关地址（默认 config [s6].gw）")
            sp.add_argument("--route", help="经网关真调的路由（默认 config [s6].route）")
            sp.add_argument("--admin-token", help="X-Admin-Token（默认 config [s6].admin_token）")
            sp.add_argument("--version", help="加载版本 YYYYMMDD（默认当日）")
            sp.add_argument("--redis-url", help="隔离 Redis 地址（只读计数用；默认 config [s6].redis_url）")
            sp.add_argument("--source-csv", help="造数 CSV（宿主相对路径；同时作源对账真件入参）")
            sp.add_argument("--docker-bin", help="docker CLI 全路径（默认 config [s6].docker_bin）")
            sp.add_argument("--hbase-ct", help="HBase 容器名（失败态 docker rm 用；默认 config [s6].hbase_ct）")

    cv = sub.add_parser("convert", help="首版基线转换：loadtest/results CSV → 基线 JSON（数据复用不重跑）")
    cv.add_argument("--from-loadtest", default=str((TESTKIT_DIR.parent / "loadtest" / "results").resolve()),
                    help="loadtest results 目录")
    cv.add_argument("--levels", help="档位子集，如 100,300,500（默认全量）")
    cv.add_argument("--env-name", default="本机M0")
    cv.add_argument("--host", default="http://localhost:9080")
    cv.add_argument("--sample-report", action="store_true",
                    help="另产样例对比报告（基线自比演示管线）＋阈值渲染演示（构造数据，显式标注）")

    cp = sub.add_parser("compare", help="run JSON × 基线 JSON → 对比报告（绿黄红）")
    cp.add_argument("--run", required=True)
    cp.add_argument("--baseline", required=True)
    cp.add_argument("--out-dir", default=None, help="报告输出目录（默认 reports/<ts>-compare）")

    sub.add_parser("selftest", help="内部自检：SM3 国标向量/百分位/判定阈值")
    return p


def cmd_convert(args, cfg) -> int:
    baseline = baseline_io.convert_from_loadtest(
        args.from_loadtest, levels=_levels_arg(args.levels),
        env_stage=args.env_name, host=args.host)
    date = baseline["source"]["measured_at"][:10].replace("-", "")
    path = baseline_io.write_baseline(baseline, TESTKIT_DIR / "baselines" / f"{args.env_name}-{date}.json")
    print(f"✅ 基线已写 {path}")
    for m in baseline["metrics"]:
        print(f"  档 {m['level']:>3}: reqs={m['reqs']:<7} rps_avg={m['rps_avg']:<7} "
              f"rps_tail={m['rps_tail']} p50={m['p50_ms']:.0f} p95={m['p95_ms']:.0f} "
              f"p99={m['p99_ms']:.0f} err={m['err_pct']}%")
    if args.sample_report:
        run = baseline_io.baseline_to_run(baseline, levels=[100, 300, 500])
        run_dir = baseline_io.run_path_for_write(TESTKIT_DIR / "reports", "sample")
        (run_dir / "run.json").write_text(__import__("json").dumps(run, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        note = "样例报告：本次＝基线同源（loadtest 2026-09-20 造数·09-21 实测自比），"                "Δ 全 0，仅演示对比管线与报告渲染。"
        rp = render.render_report(baseline, run, thresholds_of(cfg), run_dir, sample_note=note)
        print(f"✅ 样例对比报告已写 {rp}")
        demo = __import__("copy").deepcopy(run)
        for m in demo["levels"]:           # 构造偏差：演示绿黄红渲染（显式标注，非实测）
            if m["level"] == 300:
                m["rps_avg"] = round(m["rps_avg"] * 0.88, 1)      # -12% 黄
                m["rps_tail"] = round(m["rps_tail"] * 0.72, 1)    # -28% 红
                m["p99_ms"] = round(m["p99_ms"] * 1.30)           # +30% 黄
            if m["level"] == 500:
                m["p95_ms"] = round(m["p95_ms"] * 1.60)           # +60% 红
                m["err_pct"] = 0.5                                # >0.1% 红
        rp2 = render.render_report(
            baseline, demo, thresholds_of(cfg), run_dir, fname="report-variance-demo.md",
            sample_note="阈值渲染演示：数字为构造偏差（非实测），"
            "仅展示绿/黄/红三色判定与红项清单格式。")
        print(f"✅ 阈值渲染演示报告已写 {rp2}")
    return 0


def cmd_compare(args, cfg) -> int:
    run = baseline_io.read_baseline(args.run)          # run/baseline 同构读取
    baseline = baseline_io.read_baseline(args.baseline)
    out = Path(args.out_dir) if args.out_dir else baseline_io.run_path_for_write(TESTKIT_DIR / "reports", "compare")
    rp = render.render_report(baseline, run, thresholds_of(cfg), out)
    print(f"已写 {rp}")
    return 0


def cmd_selftest() -> int:
    import sm3pure

    from panel.metrics import pct_nearest_rank
    from report.render import judge

    bad = sm3pure.selftest(verbose=True)
    vals = list(range(1, 101))
    assert pct_nearest_rank(vals, 0.50) == 50 and pct_nearest_rank(vals, 0.99) == 99, "百分位最近秩法错"
    th = {"rps_yellow_drop_pct": 10, "rps_red_drop_pct": 25, "lat_yellow_rise_pct": 20,
          "lat_red_rise_pct": 50, "err_red_pct": 0.1}
    assert judge("rps_avg", "rps", 1000, 900, th)[0] == "黄"
    assert judge("rps_avg", "rps", 1000, 700, th)[0] == "红"
    assert judge("p99_ms", "lat", 100, 115, th)[0] == "绿"
    assert judge("p99_ms", "lat", 100, 125, th)[0] == "黄"
    assert judge("err_pct", "err", 0, 0.5, th)[0] == "红"
    print("[selftest] 百分位最近秩法＋绿黄红阈值判定：通过")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()

    if args.cmd == "selftest":
        return cmd_selftest()
    if args.cmd == "convert":
        return cmd_convert(args, cfg)
    if args.cmd == "compare":
        return cmd_compare(args, cfg)

    scen = args.cmd
    run_dir = baseline_io.run_path_for_write(TESTKIT_DIR / "reports", scen)
    s6_opts = None
    if scen == "s6":   # 真链路专用入参：命令行覆盖 config [s6]（隔离环境由编排脚本注入）
        s6_opts = {k: getattr(args, k, None)
                   for k in ("svc", "gw", "route", "admin_token", "version", "redis_url",
                             "source_csv", "docker_bin", "hbase_ct")}
    driver = Driver(cfg, scen, args.mode, args.mock, args.host, args.keyfile,
                    args.env_name, _levels_arg(args.levels), args.secs, args.quota, run_dir,
                    skip_smoke=getattr(args, "skip_smoke", False), s6_opts=s6_opts)
    web_ui = None
    if args.debug_web:
        web_ui = driver.env.create_web_ui("127.0.0.1", 8089)
        print("[debug-web] locust web UI：http://127.0.0.1:8089（方案 A 调试后备）")
    try:
        code = driver.run()
    finally:
        if web_ui is not None:
            web_ui.stop()
    if driver.reports:
        print("产物：")
        for r in driver.reports:
            print(f"  - {r}")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
