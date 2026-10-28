# -*- coding: utf-8 -*-
"""执行内核（双观众共用）：程序化 locust（进程内事件钩子喂数）＋档位编排＋run JSON＋对比报告。

档位窗口口径＝loadtest/run_level.sh（commit a7066fa）移植：每档 70s＝10s 起压＋60s 全并发
（S4），S1 演示窗每档 30s＝10s＋20s（面板脚注注明「正式口径以 S4 70s 窗为准」）；
预热 50×30s 不计入统计（run_ladder.sh 同口径）。
与平台解耦（设计稿 §6）：不 import service/sync/loadtest 任何代码，交互面＝仅网关 HTTP 黑盒
＋键池文件路径约定＋（首基线）一次性读 loadtest/results 数据。
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import gevent

from locust.env import Environment

from panel import live as panel_mod
from panel.metrics import RollingStats
from report import baseline_io, render

TESTKIT_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    import tomllib

    path = TESTKIT_DIR / "config.toml"
    if not path.exists():
        path = TESTKIT_DIR / "config.example.toml"
    with open(path, "rb") as f:
        return tomllib.load(f)


def thresholds_of(cfg: dict) -> dict:
    return cfg["thresholds"]


@dataclass
class Phase:
    kind: str                 # smoke / warmup / level / flat
    label: str
    users: int
    secs: float
    ramp_secs: float = 10.0
    level: int | None = None  # kind=level 时的档位


# --------------------------------------------------------------------------- #
# 目标解析：真实网关 or MOCK 降级（设计稿 §4.3）
# --------------------------------------------------------------------------- #
def probe_host(url: str, timeout: float = 1.5) -> bool:
    import socket

    try:
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((u.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def git_rev_of_repo() -> str:
    try:
        out = subprocess.run(["git", "-C", str(TESTKIT_DIR.parent), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


class Driver:
    def __init__(self, cfg: dict, scenario: str, mode: str, mock: bool, host: str | None,
                 keyfile: str | None, env_name: str | None, levels: list[int] | None,
                 secs: float | None, quota: int | None, run_dir: Path,
                 skip_smoke: bool = False, s6_opts: dict | None = None) -> None:
        self.cfg = cfg
        self.scenario = scenario
        self.mode = mode            # live / headless / only-summary
        self.run_dir = run_dir
        self.reports: list[str] = []
        self._levels = levels
        self._secs = secs
        self._quota = quota
        self._skip_smoke = skip_smoke
        self._s6_opts = s6_opts or {}

        general = cfg["general"]
        self.env_stage = env_name or general["env_stage"]
        self.appkey = _env_or("APISIX_APPKEY", general["appkey"])
        self.mock = mock
        self.mock_gw = None
        self.host = host
        if not self.host:
            self.host = _env_or("TESTKIT_HOST", general["host"])

        # ---- s6：M2 真链路场景（requests 步进机检，非 locust 打点）-------
        # 早退构造：不需要键池/locust Environment；环境由编排脚本起好并经 s6_opts 注入。
        if scenario == "s6":
            self.mock = False
            self.keys = []
            self.rolling = RollingStats()
            self.baseline = None
            self.baseline_name = None
            self.ctx = panel_mod.PanelContext(
                title=f"OneServe testkit · {scenario.upper()}",
                env_stage=self.env_stage, mock=False,
                baseline_name=None, thresholds=thresholds_of(cfg))
            self.panel = None
            return

        # ---- 目标可达性 → mock 降级（§4.3） --------------------------------
        if not self.mock and not probe_host(self.host):
            print(f"⚠ 目标 {self.host} 不可达 → 降级 MOCK 演示模式（面板/报告均标注；--mock 可直接强制）")
            self.mock = True
        if self.mock:
            from mocktarget import MockGateway, synthetic_keys

            self.mock_gw = MockGateway(
                quota_b=(quota if quota is not None else cfg["s3"]["quota_b"]),
                window_s=cfg["s3"]["quota_window_s"],
                appkey_a=self.appkey,
                appkey_b=general["appkey_b"],
                ratelimit_status=cfg["s3"]["ratelimit_status"],
            )
            self.host = self.mock_gw.start()
            self.keys = synthetic_keys(2000)
        else:
            kf = keyfile or str((TESTKIT_DIR / general["keyfile"]).resolve())
            from scenarios.common import load_keys

            self.keys = load_keys(kf)

        # ---- 面板上下文 ------------------------------------------------------
        self.rolling = RollingStats()
        bl_path = baseline_io.latest_baseline(TESTKIT_DIR / "baselines", self.env_stage)
        self.baseline = baseline_io.read_baseline(bl_path) if bl_path else None
        self.baseline_name = bl_path.stem if bl_path else None
        self.ctx = panel_mod.PanelContext(
            title=f"OneServe testkit · {scenario.upper()}",
            env_stage=self.env_stage, mock=self.mock,
            baseline_name=self.baseline_name, thresholds=thresholds_of(cfg))
        self.panel: panel_mod.BasePanel | None = None

        # ---- locust 环境 ------------------------------------------------------
        self.user_classes = self._build_user_classes(levels, quota)
        self.env = Environment(user_classes=self.user_classes, host=self.host)
        self.runner = self.env.create_local_runner()
        self.env.events.request.add_listener(self._on_request)
        self._locust_stats: list[tuple[int, int]] = []   # (reqs, fails) 每档 locust 终数交叉核对

    # ------------------------------------------------------------------ #
    def _build_user_classes(self, levels: list[int] | None, quota: int | None):
        from scenarios import channels, ratelimit
        from scenarios.common import TagQueryUser

        scen = self.scenario
        if scen in ("s0", "s1", "s4"):

            class U(TagQueryUser):
                abstract = False       # 显式取消继承的 abstract，确保可被 spawn
                keys = self.keys
                appkey = self.appkey

            return [U]
        if scen in ("s5", "s6"):
            return []                  # s5 批量／s6 M2 链路＝requests 步进编排，无 locust 用户类
        if scen == "s2":
            classes = []
            for cls in (channels.ApiHttpUser, channels.SdkSm3User, channels.WebChannelUser):
                sub = type(cls.__name__ + "Impl", (cls,),
                           {"abstract": False, "keys": self.keys, "appkey": self.appkey})
                classes.append(sub)
            return classes
        if scen == "s3":
            cfg3 = self.cfg["s3"]
            DualGroupUserImpl = type("DualGroupUserImpl", (ratelimit.DualGroupUser,), {
                "abstract": False,
                "keys": self.keys, "appkey": self.appkey,
                "appkey_b": self.cfg["general"]["appkey_b"],
                "ratelimit_status": cfg3["ratelimit_status"],
                "users_a": cfg3["users_a"],
            })
            return [DualGroupUserImpl]
        raise ValueError(f"未知场景：{scen}")

    def _on_request(self, request_type, name, response_time, response_length,
                    exception=None, context=None, **kwargs) -> None:
        self.rolling.add(response_time, exception is None, exception, name=name or "")

    # ------------------------------------------------------------------ #
    # 档位执行
    # ------------------------------------------------------------------ #
    def _run_phase(self, phase: Phase, tail_slices: int = 5) -> dict | None:
        self.env.stats.reset_all()
        self.ctx.phase = phase.label
        self.ctx.next_hint = ""
        spawn_rate = max(1.0, phase.users / max(phase.ramp_secs, 1e-9))
        t0 = time.time()
        t_end = t0 + phase.secs
        self.runner.start(phase.users, spawn_rate)
        while True:
            now = time.time()
            if now >= t_end:
                break
            self.ctx.users = self.runner.user_count
            self._update_live_ctx(phase, t0)
            if self.panel is not None:
                self.panel.tick()
            else:
                gevent.sleep(1.0)
        self.runner.stop()
        while self.runner.state != "stopped":   # 等用户全部退出（在途请求自然收尾）
            gevent.sleep(0.2)
        t1 = time.time()
        self.ctx.users = 0
        self._locust_stats.append((self.env.stats.total.num_requests,
                                   self.env.stats.total.num_failures))
        if phase.kind in ("level", "smoke", "flat"):
            res = self.rolling.level_result(t0, t1, secs=round(t1 - t0, 1), tail_slices=tail_slices)
            if phase.kind == "flat":
                self._flat_window = (t0, t1, round(t1 - t0, 1))
            return res
        return None

    def _update_live_ctx(self, phase: Phase, t0: float) -> None:
        if phase.kind == "level" and self.baseline:
            bm = baseline_io.baseline_metric(self.baseline, phase.level)
            if bm:
                so_far = self.rolling.level_result(t0, time.time(), secs=max(time.time() - t0, 1e-9))
                self.ctx.baseline_rows["tps"] = (so_far["rps_avg"], bm["rps_avg"],
                                                 (so_far["rps_avg"] - bm["rps_avg"]) / bm["rps_avg"] * 100
                                                 if bm["rps_avg"] else float("nan"))
                self.ctx.baseline_rows["p99"] = (so_far["p99_ms"], bm["p99_ms"],
                                                 (so_far["p99_ms"] - bm["p99_ms"]) / bm["p99_ms"] * 100
                                                 if bm["p99_ms"] else float("nan"))
        if self.scenario == "s3":
            from scenarios.ratelimit import COUNTERS

            self.ctx.group_line = COUNTERS.line()

    # ------------------------------------------------------------------ #
    # 场景编排
    # ------------------------------------------------------------------ #
    def run(self) -> int:
        cfg = self.cfg
        scen = self.scenario
        if scen == "s5":
            return self._run_s5(cfg)   # 批量场景：独立编排，不走 locust 阶段循环
        if scen == "s6":
            return self._run_s6(cfg)   # M2 链路场景：requests 步进机检，不走 locust 阶段循环
        levels: dict[int, dict] = {}
        smoke: dict | None = None

        if scen == "s0":
            c = cfg["s0"]
            phases = [Phase("smoke", f"S0 冒烟（{c['users']} 用户×{c['secs']}s）",
                            c["users"], c["secs"], ramp_secs=c["users"])]
        elif scen == "s1":
            c = cfg["s1"]
            lv = self._levels or c["levels"]
            step = self._secs or c["step_secs"]
            phases = [Phase("level", f"S1 档 {u}（起压 {c['ramp_secs']}s＋全并发 {step - c['ramp_secs']:.0f}s）",
                            u, step, c["ramp_secs"], level=u) for u in lv]
        elif scen == "s2":
            c = cfg["s2"]
            n = c["users_per_channel"] * 3
            secs = self._secs or c["secs"]
            phases = [Phase("flat", f"S2 三通道（每通道 {c['users_per_channel']} 用户×{secs:.0f}s）",
                            n, secs, 10)]
        elif scen == "s3":
            c = cfg["s3"]
            n = c["users_a"] + c["users_b"]
            quota = self._quota if self._quota is not None else c["quota_b"]
            secs = self._secs or c["secs"]
            phases = [Phase("flat", f"S3 限流（A {c['users_a']} 正常配额 / B {c['users_b']} 低配额 {quota}/窗口）",
                            n, secs, 10)]
        else:  # s4
            c = cfg["s4"]
            lv = self._levels or c["levels"]
            step = self._secs or c["step_secs"]
            phases: list[Phase] = []
            if not self._skip_smoke:
                s0 = cfg["s0"]
                phases.append(Phase("smoke", f"S0 冒烟门（{s0['users']} 用户×{s0['secs']}s，挂了即红）",
                                    s0["users"], s0["secs"], ramp_secs=s0["users"]))
            phases.append(Phase("warmup", f"预热（{c['warmup_users']} 用户×{c['warmup_secs']}s，不计入统计）",
                                c["warmup_users"], c["warmup_secs"], 10))
            for u in lv:
                phases.append(Phase("level", f"S4 档 {u}（起压 {c['ramp_secs']}s＋全并发 {step - c['ramp_secs']:.0f}s）",
                                    u, step, c["ramp_secs"], level=u))

        self.ctx.level_plan = [p.level for p in phases if p.kind == "level"]
        self._levels_cfg_note(scen)

        svg = str(self.run_dir / "panel.svg")
        self.panel = panel_mod.make_panel(self.ctx, self.rolling, self.mode,
                                          svg_path=svg if scen in ("s1", "s3") else None)
        if isinstance(self.panel, panel_mod.RichPanel):
            self.panel.live.start()

        exit_code = 0
        flat_result: dict | None = None
        for i, ph in enumerate(phases):
            nxt = phases[i + 1] if i + 1 < len(phases) else None
            if nxt is not None:
                self.ctx.next_hint = f"下一阶段：{nxt.label} 本阶段结束后开始"
            result = self._run_phase(ph, tail_slices=cfg["s4"].get("tail_slices", 5))
            if ph.kind == "smoke":
                smoke = result
                if (result and self.scenario == "s4"
                        and not (result["fails"] == 0
                                 and result["p99_ms"] == result["p99_ms"]
                                 and result["p99_ms"] < cfg["s0"]["p99_max_ms"])):
                    print("🔴 S0 未过——回归中止（挂了即红），梯子不跑、对比不产出")
                    break
            elif ph.kind == "level":
                levels[ph.level] = result
            elif ph.kind == "flat":
                flat_result = result
            if ph.kind == "level" and result:
                mark = ""
                print(f"== 档 {ph.level} 完成：{result['reqs']} 请求，错误 {result['fails']}"
                      f"（{result['err_pct']}%），RPS {result['rps_avg']}，"
                      f"P50 {result['p50_ms']:.0f}ms / P95 {result['p95_ms']:.0f}ms / "
                      f"P99 {result['p99_ms']:.0f}ms{mark} ==")
            gevent.sleep(2.0)   # 档间排空（连接/在途请求），对应独立进程切换间隙

        if isinstance(self.panel, panel_mod.RichPanel):
            self.panel.live.stop()
        if self.panel:
            self.panel.close()

        # ---- S0 判定 -------------------------------------------------------
        smoke_red = False
        if smoke is not None:
            p99_max = cfg["s0"]["p99_max_ms"]
            ok = smoke["fails"] == 0 and smoke["p99_ms"] == smoke["p99_ms"] and smoke["p99_ms"] < p99_max
            smoke_red = not ok
            print(f"[S0 冒烟] {'✅ 绿（栈活性探针通过）' if ok else '🔴 红（挂了即红——先看栈再回归）'}："
                  f"{smoke['reqs']} 请求 / 失败 {smoke['fails']} / P99 {smoke['p99_ms']:.0f}ms（<{p99_max}ms）")
            if scen == "s0":
                exit_code = 0 if ok else 1

        self._finish(levels, smoke, smoke_red, flat_result)
        return exit_code

    _levels: list[int] | None = None
    _secs: float | None = None
    _quota: int | None = None
    _flat_window: tuple[float, float, float] | None = None

    def _channel_stats(self) -> list[dict]:
        """S2 三通道分列（flat 窗口内按请求名分组统计）。"""
        if self.scenario != "s2" or not self._flat_window:
            return []
        t0, t1, secs = self._flat_window
        names = sorted({e.name for e in self.rolling.events if t0 <= e.t < t1 and e.name})
        out = []
        for n in names:
            m = self.rolling.level_result(t0, t1, secs=secs, names=[n])
            out.append({"channel": n, **m})
        return out

    def _run_s5(self, cfg: dict) -> int:
        """S5 批量场景编排（TASK-M15-7）：场景逻辑全在 scenarios/batch.py（五步
        步进机检），driver 只提供 host/键池/模式复用与产物落盘（run.json＋
        checks.txt＋下载头样本）。判据红以 batch.Red 异常冒泡（统一捕获落报告）。"""
        from scenarios import batch as batch_scen

        c5 = cfg["s5"]
        print(f"[S5] 批量场景：主任务 {c5['n_main']} 键＋取消任务 {c5['n_cancel']} 键"
              f"＋快照 429 段｜host={self.host}｜键池 {len(self.keys)} 键")
        run = {
            "schema": "oneserve.testkit.run/1",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "scenario": "s5",
            "env": {"stage": self.env_stage, "host": self.host,
                    "stack_git_rev": "mock" if self.mock else git_rev_of_repo(),
                    "locust": _locust_version(), "mock": self.mock,
                    "note": self.cfg["general"].get("note", "")},
            "params": dict(c5),
        }
        try:
            batch_scen.run_scenario(
                self.host, self.keys, self.appkey,
                n_main=c5["n_main"], n_cancel=c5["n_cancel"],
                per_job_timeout_s=c5["per_job_timeout_s"],
                cancel_wait_s=c5["cancel_wait_s"],
                artifacts_dir=self.run_dir)
        except batch_scen.Red as exc:
            run["s5"] = {"verdict": "红", "red": str(exc)}
            (self.run_dir / "run.json").write_text(
                json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            rp = self.run_dir / "report.md"
            rp.write_text(
                "# OneServe 批量场景报告 · " + self.env_stage + " · "
                + run["created_at"][:10] + "\n\n## 结论：红——" + str(exc) + "\n\n"
                "机检清单：checks.txt（红处止步，先红先停）。\n", encoding="utf-8")
            self.reports += [str(self.run_dir / "run.json"), str(rp),
                             str(self.run_dir / "checks.txt")]
            print(f"🔴 S5 红：{exc}")
            print(f"产物：{self.run_dir}")
            return 1
        run["s5"] = {"verdict": "绿"}
        (self.run_dir / "run.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rp = self.run_dir / "report.md"
        rp.write_text(
            "# OneServe 批量场景报告 · " + self.env_stage + " · "
            + run["created_at"][:10] + "\n\n## 结论：绿——五步机检全过"
            "（提交/轮询/交付/取消/429，判据明细见 checks.txt）\n\n"
            "| 参数 | 值 |\n|---|---|\n"
            + "".join(f"| {k} | {v} |\n" for k, v in c5.items()), encoding="utf-8")
        self.reports += [str(self.run_dir / "run.json"), str(rp),
                         str(self.run_dir / "checks.txt")]
        print(f"✅ S5 绿｜产物：{self.run_dir}")
        return 0

    def _run_s6(self, cfg: dict) -> int:
        """S6 M2 端到端＋冷热加载编排（TASK-M2-12）：场景逻辑全在
        scenarios/m2load.py（六步机检），driver 只提供隔离环境入参（config [s6]
        ＋命令行覆盖）与产物落盘（run.json＋checks.txt）。判据红以 m2load.Red
        异常冒泡（统一捕获落报告）。"""
        from scenarios import m2load as m2

        c6 = cfg["s6"]
        opt = lambda k: self._s6_opts.get(k) or c6.get(k)      # noqa: E731
        repo = TESTKIT_DIR.parent
        params = {
            "svc": opt("svc"), "gw": opt("gw"), "route": opt("route"),
            "version": opt("version") or datetime.now().strftime("%Y%m%d"),
            "redis_url": opt("redis_url"), "source_csv": opt("source_csv"),
            "table": c6["table"], "cf": c6["cf"],
            "docker_bin": opt("docker_bin"), "hbase_ct": opt("hbase_ct"),
            "apikey": opt("apikey"), "admin_token": opt("admin_token"),
        }
        print(f"[S6] M2 端到端＋冷热加载：svc={params['svc']} gw={params['gw']} "
              f"route={params['route']} 版本={params['version']}｜源 CSV={params['source_csv']}")
        run = {
            "schema": "oneserve.testkit.run/1",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "scenario": "s6",
            "env": {"stage": self.env_stage, "host": params["gw"],
                    "stack_git_rev": git_rev_of_repo(),
                    "locust": _locust_version(), "mock": False,
                    "note": self.cfg["general"].get("note", "")},
            "params": params,
        }
        try:
            m2.run_scenario(
                svc=params["svc"], gw=params["gw"], route=params["route"],
                apikey=params["apikey"], admin_token=params["admin_token"],
                version=params["version"], redis_url=params["redis_url"],
                source_csv=params["source_csv"], table=params["table"], cf=params["cf"],
                docker_bin=params["docker_bin"], hbase_ct=params["hbase_ct"],
                repo=repo, artifacts_dir=self.run_dir)
        except m2.Red as exc:
            run["s6"] = {"verdict": "红", "red": str(exc)}
            (self.run_dir / "run.json").write_text(
                json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            rp = self.run_dir / "report.md"
            rp.write_text(
                "# OneServe M2 端到端场景报告 · " + self.env_stage + " · "
                + run["created_at"][:10] + "\n\n## 结论：红——" + str(exc) + "\n\n"
                "机检清单：checks.txt（红处止步，先红先停）。\n", encoding="utf-8")
            self.reports += [str(self.run_dir / "run.json"), str(rp),
                             str(self.run_dir / "checks.txt")]
            print(f"🔴 S6 红：{exc}")
            print(f"产物：{self.run_dir}")
            return 1
        run["s6"] = {"verdict": "绿"}
        (self.run_dir / "run.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rp = self.run_dir / "report.md"
        rp.write_text(
            "# OneServe M2 端到端场景报告 · " + self.env_stage + " · "
            + run["created_at"][:10] + "\n\n## 结论：绿——六步机检全过"
            "（选表/加载/幂等/切指针/经网关真调/失败态，判据明细见 checks.txt）\n\n"
            "如实标注：真集群（NC-M2-9）未做——跑在开发替身容器（compose.hbase.yml）上。\n\n"
            "| 参数 | 值 |\n|---|---|\n"
            + "".join(f"| {k} | {v} |\n" for k, v in params.items()), encoding="utf-8")
        self.reports += [str(self.run_dir / "run.json"), str(rp),
                         str(self.run_dir / "checks.txt")]
        print(f"✅ S6 绿｜产物：{self.run_dir}")
        return 0

    def _levels_cfg_note(self, scen: str) -> None:
        notes = {
            "s1": "口径脚注：演示窗每档 30s＝10s 起压＋20s 全并发（config 可调）；正式口径以 S4 回归 70s 窗为准。"
                  "TPS＝5s 滚动窗、P50/P95/P99＝60s 滚动窗；跑后终数＝全窗口径（RPS=总数/档位时长）。",
            "s2": "口径脚注：三通道同一批键；②客户端 SM3（官方参考实现口径）；③服务端代算（前端自算上线后复测）。"
                  "键池为单向哈希无手机号原文，②③用随机合规号（KV 未命中路径与命中路径同构）。",
            "s3": "口径脚注：限流计数为预期行为不计入错误率；配额数字＝演示假想值（--quota 可调，默认值待定）。",
            "s4": "口径脚注：每档 70s＝10s 起压＋60s 全并发（run_level.sh 同口径）；预热不计入；"
                  "吞吐口径（SLA 承诺）与并发档容量参考严格区分（SPEC §6.1）。",
        }.get(scen)
        if notes:
            self.ctx.footnote = notes
            print(f"[{scen.upper()}] {notes}")
        if self.mock:
            print(f"[MOCK] 目标＝{self.host}（模拟网关：/v1/tags/query、/demo/query、按 apikey 限流）")

    # ------------------------------------------------------------------ #
    # 产物落盘
    # ------------------------------------------------------------------ #
    def _finish(self, levels: dict[int, dict], smoke: dict | None, smoke_red: bool = False,
                flat_result: dict | None = None) -> None:
        run = {
            "schema": "oneserve.testkit.run/1",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "scenario": self.scenario,
            "env": {"stage": self.env_stage, "host": self.host,
                    "stack_git_rev": "mock" if self.mock else git_rev_of_repo(),
                    "locust": _locust_version(), "mock": self.mock,
                    "note": self.cfg["general"].get("note", "")},
            "smoke": smoke,
            "flat": flat_result,
            "channels": self._channel_stats(),
            "levels": [dict(level=lv, **m) for lv, m in sorted(levels.items())],
            "cross_check_locust_totals": [{"reqs": r, "fails": f} for r, f in self._locust_stats],
        }
        run_path = self.run_dir / "run.json"
        run_path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.reports.append(str(run_path))
        print(f"已写 {run_path}")

        if self.scenario == "s4" and levels:
            self._regression_finish(run)
        elif self.scenario == "s4" and smoke_red:
            rp = self.run_dir / "report.md"
            rp.write_text(
                "# OneServe 性能回归报告 · " + self.env_stage + " · " + run["created_at"][:10] + "\n"
                "## 结论：红——S0 冒烟门未过（栈活性探针失败），梯子中止、未产出档位对比\n\n"
                "排查：栈是否起（scripts/up.sh）/ 网关 9080 是否通 / 键池是否就绪；恢复后重跑 s4。\n",
                encoding="utf-8")
            self.reports.append(str(rp))
            print(f"已写对比报告 {rp}")

        if self.mock_gw:
            self.mock_gw.stop()

    def _regression_finish(self, run: dict) -> None:
        th = thresholds_of(self.cfg)
        if self.baseline is None:
            cand = self._candidate_from_run(run)
            p = baseline_io.write_baseline(cand, TESTKIT_DIR / "baselines" / f"{self.env_stage}-candidate.json")
            print(f"⚠ 无 {self.env_stage} 基线（首跑建基线）：已写候选 {p}——人工确认后改名为 "
                  f"{self.env_stage}-<YYYYMMDD>.json 即生效")
            return
        report_path = render.render_report(self.baseline, run, th, self.run_dir)
        self.reports.append(report_path)
        print(f"已写对比报告 {report_path}")
        rows = render.compare_levels(self.baseline, run, th)
        n_red = sum(1 for r in rows if r["verdict"] == "红")
        n_yellow = sum(1 for r in rows if r["verdict"] == "黄")
        if n_red == 0 and n_yellow == 0:
            cand = self._candidate_from_run(run)
            p = baseline_io.write_baseline(
                cand, TESTKIT_DIR / "baselines" / f"{self.env_stage}-{datetime.now():%Y%m%d}-candidate.json")
            print(f"✅ 全绿——可提升为新基线：已写候选 {p}（人工确认后去掉 -candidate 后缀生效，不自动覆盖）")
        elif n_red:
            print(f"🔴 红 {n_red} 项（黄 {n_yellow} 项）——性能回退当场现形，详见报告")
        else:
            print(f"🟡 黄 {n_yellow} 项（无红项）——关注报告黄色行，暂不换基线")

    def _candidate_from_run(self, run: dict) -> dict:
        return {"schema": baseline_io.SCHEMA,
                "created_at": run["created_at"],
                "promoted_by": "待 维护者 确认（candidate）",
                "env": {k: v for k, v in run["env"].items() if k != "mock"},
                "scenario": "regression",
                "metrics": [dict(level=m["level"], reqs=m["reqs"], rps_avg=m["rps_avg"],
                                 rps_tail=m.get("rps_tail"), p50_ms=m["p50_ms"], p95_ms=m["p95_ms"],
                                 p99_ms=m["p99_ms"], err_pct=m["err_pct"]) for m in run["levels"]],
                "sla_check": (self.baseline or {}).get(
                    "sla_check", {"qps_target": self.cfg["sla"]["qps_target"],
                                  "p50_max_ms": self.cfg["sla"]["p50_max_ms"],
                                  "p99_max_ms": self.cfg["sla"]["p99_max_ms"],
                                  "basis": self.cfg["sla"]["latency_basis"]}),
                "source": {"type": "testkit-run", "run": str(self.run_dir / "run.json")}}

    # S1 面板标题需要档位计划
    def set_levels(self, levels: list[int] | None) -> None:
        self._levels = levels

    def set_secs(self, secs: float | None) -> None:
        self._secs = secs

    def set_quota(self, quota: int | None) -> None:
        self._quota = quota


def _env_or(var: str, default: str) -> str:
    import os

    return os.environ.get(var) or default


def _locust_version() -> str:
    try:
        from importlib.metadata import version

        return version("locust")
    except Exception:  # noqa: BLE001
        return "unknown"
