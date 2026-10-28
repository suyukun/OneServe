# -*- coding: utf-8 -*-
"""S5 批量场景（TASK-M15-7，开工门槛-M1.5批量查询_v0 §A3 AC-10 回归侧＋C1 判据）。

与 s0~s4（locust 点查画像）不同：批量是异步任务流（提交→轮询→交付），不是并发
打点——本场景走 requests 同步步进编排，不进 locust Environment（driver 对 s5
只提供 host/keys/模式复用，场景逻辑全在本文件，见 driver.run_scenario）。

五步机检（Checks 逐条落判据，任一红＝场景退出码 1）：
  ①提交名单任务（键池前 N 键，fields 请求 3 字段投影）→ 202：状态 queued＋
    total=N＋keys_sha256 与客户端对归一化名单复算一致（NC-5 名单侧）＋fields 回显；
  ②轮询状态到 succeeded（超时 per-job 120s）：终态 succeeded＋progress=100＋
    data_as_of 非空（钉版本）；
  ③下载完整结果 JSONL：X-Results-Sha256 头＝响应体自算 sha256（NC-5 结果侧）＋
    行数=total＋行 schema 五键＋命中行 fields ⊆ 请求投影（无越权字段；行字段＝
    白名单∩非空∩请求，字段为空即不出现）＋投影贯通有实出（存在非空行）＋
    if_acct_fct_24 ∈ A/B/C 档（band_mask 脱敏贯通到批量行）＋data_as_of 全行一致
    ＝状态接口所报＋found 对账（状态接口=文件行数）；
  ④取消 running 任务（AC-6）：提交名单任务（重复取样拉长执行窗口到秒级），等到
    running 即发 DELETE（200）→ 响应 status=cancelling（停止意向）→ 轮询到
    cancelled 终态；分片产物随取消清理（results 409 job_not_succeeded 间接核验）。
    竞态兜底：DELETE 到达前任务已 succeeded → 换名单重跑一次；两次机会用尽仍未
    观测到 cancelling→cancelled ＝ 红（如实报，不粉饰）；
  ⑤429 并发拒（AC-10）：提交长名单任务（键池×3≈6000 键）占住 worker，等到
    running（并发位已占——AC-3：queued 不占位，running 才占）后提交第二个名单
    任务：429＋机读码 quota_concurrent_exceeded＋X-RateLimit-Reason=concurrency
    ＋Retry-After 头。收尾取消占位任务并等 cancelled（收尾失败仅记 warn，
    不影响判定——本地卷残留的续跑语义归 A4，不属本场景判据）。

口径脚注：键池与 s0~s4 同源（CSV 首列 64 位十六进制，run.py 统一加载注入）；
fields=授权白名单子集（ac_active_sts_01/ac_login_fct_01/if_acct_fct_24，末者
脱敏 band_mask→A/B/C 档）；投影视角＝网关黑盒（与既有场景同交互面纪律，不
import 平台代码、不直连容器）；产物落 run_dir（checks.txt 机检清单＋下载头 5 行
样本 s5-download-head.jsonl，不落全量结果文件——GB 级本地卷纪律）。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import requests

TERMINAL = {"succeeded", "failed", "cancelled"}
PROBE_FIELDS = ["ac_active_sts_01", "ac_login_fct_01", "if_acct_fct_24"]
ROW_KEYS = {"key", "found", "fields", "data_as_of", "error"}
NL = chr(10)   # 名单摘要/JSONL 拼接用换行（文件内不写反斜杠转义，保持可读）


class Red(Exception):
    """判定红（场景级失败；消息进机检清单与退出码）。"""


class Checks:
    """机检清单：逐条 (判据名, 绿/红, 明细)；红即抛 Red 止步（先红先停）。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, bool, str]] = []
        self.lines: list[str] = []            # 全程留痕（checks.txt 落盘用，红也留）

    def add(self, name: str, ok: bool, detail: str = "") -> str:
        self.items.append((name, ok, detail))
        mark = "✅" if ok else "🔴"
        line = f"  {mark} {name}" + (f"｜{detail}" if detail else "")
        self.lines.append(line)
        print(line)
        if not ok:
            raise Red(name + (f"｜{detail}" if detail else ""))
        return line

    def table(self) -> str:
        n_ok = sum(1 for _, ok, _ in self.items if ok)
        return f"机检 {n_ok}/{len(self.items)} 绿"


def _auth(appkey: str) -> dict:
    return {"apikey": appkey}


def _post_job(s: requests.Session, host: str, appkey: str, payload: dict,
              timeout: float = 30.0) -> tuple[dict, int, dict]:
    r = s.post(f"{host}/v1/batch/jobs", json=payload, headers=_auth(appkey),
               timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {"_raw": r.text[:200]}
    return body, r.status_code, r.headers   # CaseInsensitiveDict：头名大小写不敏感


def _poll(s: requests.Session, host: str, appkey: str, job_id: str,
          want: set[str], deadline: float) -> dict:
    """轮询状态接口到 want 终态；落到非 want 终态＝Red（如实，不粉饰）。"""
    while True:
        r = s.get(f"{host}/v1/batch/jobs/{job_id}", headers=_auth(appkey),
                  timeout=30.0)
        if r.status_code != 200:
            raise Red(f"状态接口 {r.status_code}：{r.text[:200]}")
        last = r.json()
        status = last.get("status")
        if status in want:
            return last
        if status in TERMINAL:
            raise Red(f"终态 {status} ∉ {sorted(want)}：{last.get('error') or ''}")
        if time.monotonic() > deadline:
            raise Red(f"轮询超时：last={status} progress={last.get('progress')}")
        time.sleep(0.05)


def _wait_running(s: requests.Session, host: str, appkey: str, job_id: str,
                  deadline: float) -> dict:
    """轮询到 running（④取消/⑤占位的时序前置）：落任何终态＝Red（竞态如实报）。"""
    while True:
        r = s.get(f"{host}/v1/batch/jobs/{job_id}", headers=_auth(appkey),
                  timeout=30.0)
        if r.status_code != 200:
            raise Red(f"状态接口 {r.status_code}：{r.text[:200]}")
        last = r.json()
        status = last.get("status")
        if status == "running":
            return last
        if status in TERMINAL:
            raise Red(f"等 running 前已终态 {status}（竞态）：{last.get('error') or ''}")
        if time.monotonic() > deadline:
            raise Red(f"等 running 超时：last={status}")
        time.sleep(0.02)


# --------------------------------------------------------------------------- #
# 五步编排（driver.run_scenario 调用入口；独立可跑＝run.py s5 退化路径不用本函数）
# --------------------------------------------------------------------------- #
def run_scenario(host: str, keys: list[str], appkey: str, *, n_main: int,
                 n_cancel: int, per_job_timeout_s: float, cancel_wait_s: float,
                 artifacts_dir: Path | None) -> tuple[int, list[str]]:
    """跑五步机检；返回 (退出码, warns)。判据红以 Red 抛出（driver 捕获落报告）。"""
    ck = Checks()
    s = requests.Session()
    warns: list[str] = []
    try:
        # ---------- ① 提交名单任务 ----------
        print("[S5-①] 提交名单任务")
        keys_main = keys[:n_main]
        sha_expect = hashlib.sha256(NL.join(keys_main).encode()).hexdigest()
        body, code, _hdr = _post_job(s, host, appkey,
                                     {"keys": keys_main, "fields": PROBE_FIELDS})
        ck.add("① 提交 202", code == 202, f"code={code}")
        ck.add("① 状态 queued", body.get("status") == "queued", str(body.get("status")))
        ck.add("① total=名单数", body.get("total") == n_main,
               f"total={body.get('total')} expect={n_main}")
        ck.add("① keys_sha256 客户端复算一致（NC-5 名单侧）",
               body.get("keys_sha256") == sha_expect,
               f"resp={str(body.get('keys_sha256'))[:16]}… expect={sha_expect[:16]}…")
        ck.add("① fields 回显=请求投影", body.get("fields") == PROBE_FIELDS,
               str(body.get("fields")))
        job1 = body["job_id"]

        # ---------- ② 轮询到 succeeded ----------
        print("[S5-②] 轮询到 succeeded")
        t0 = time.monotonic()
        st = _poll(s, host, appkey, job1, {"succeeded"},
                   deadline=time.monotonic() + per_job_timeout_s)
        ck.add("② 终态 succeeded", st.get("status") == "succeeded",
               f"耗时 {time.monotonic() - t0:.1f}s")
        ck.add("② progress=100", st.get("progress") == 100, str(st.get("progress")))
        ck.add("② data_as_of 非空（钉版本）", bool(st.get("data_as_of")),
               str(st.get("data_as_of")))

        # ---------- ③ 下载结果校验 ----------
        print("[S5-③] 下载结果校验（行数/投影/脱敏/双 sha256 结果侧）")
        r = s.get(f"{host}/v1/batch/jobs/{job1}/results", headers=_auth(appkey),
                  timeout=60.0)
        ck.add("③ 下载 200", r.status_code == 200, f"code={r.status_code}")
        raw = r.content
        ck.add("③ X-Results-Sha256=响应体自算 sha256（NC-5 结果侧）",
               r.headers.get("X-Results-Sha256") == hashlib.sha256(raw).hexdigest(),
               f"hdr={str(r.headers.get('X-Results-Sha256'))[:16]}…")
        rows = [json.loads(ln) for ln in raw.decode("utf-8").splitlines() if ln]
        ck.add("③ 行数=total", len(rows) == n_main, f"rows={len(rows)} total={n_main}")
        bad_schema = [i for i, row in enumerate(rows[:50]) if set(row) != ROW_KEYS]
        ck.add("③ 行 schema 五键（抽样前 50 行）", not bad_schema,
               f"坏行序号={bad_schema[:3]}")
        hits = [row for row in rows if row["found"]]
        ck.add("③ 存在命中行（键池=库内同源数据）", bool(hits),
               f"命中 {len(hits)}/{len(rows)}")
        leak = [row["key"][:8] for row in hits[:50]
                if row["fields"] and not set(row["fields"]) <= set(PROBE_FIELDS)]
        ck.add("③ 行 fields ⊆ 请求投影（无越权/超投影字段，命中行抽样前 50）",
               not leak, f"越权字段键={leak[:3]}")
        nonempty = sum(1 for row in hits[:50] if row["fields"])
        ck.add("③ 投影贯通有实出（行 fields 非空且⊆请求，命中行抽样前 50）",
               nonempty > 0, f"非空投影行 {nonempty}/50")
        bands = {row["fields"].get("if_acct_fct_24") for row in hits[:50]
                 if row["fields"]}
        ck.add("③ 脱敏贯通（if_acct_fct_24 ∈ A/B/C 档）",
               bands <= {"A", "B", "C", None}, str(sorted(b for b in bands if b)))
        asofs = {row["data_as_of"] for row in rows}
        ck.add("③ data_as_of 全行一致=状态所报", asofs == {st.get("data_as_of")},
               str(asofs))
        found_n = sum(1 for row in rows if row["found"])
        ck.add("③ found 对账=状态接口", st.get("found") == found_n,
               f"状态={st.get('found')} 文件={found_n}")
        ck.add("③ X-Results-Rows 对账",
               r.headers.get("X-Results-Rows") == str(len(rows)),
               str(r.headers.get("X-Results-Rows")))
        if artifacts_dir is not None:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            head = NL.join(json.dumps(row, ensure_ascii=False) for row in rows[:5])
            (artifacts_dir / "s5-download-head.jsonl").write_text(
                head + NL, encoding="utf-8")

        # ---------- ④ 取消 running→cancelling→cancelled（AC-6） ----------
        print("[S5-④] 取消 running 任务（DELETE 在 running 态发出，AC-6 时序）")
        job2 = ""
        cancelled_via_running = False
        for attempt, n_keys in enumerate((n_cancel, max(50, n_cancel // 5)), start=1):
            # 名单重复取样（每键重复 n_keys//2000+1 次）：worker 对重复键照常
            # pipeline 读（无去重），把执行窗口拉长到秒级——running 态可观测，
            # DELETE 有窗口发出；不依赖「任务必须慢」的侥幸。
            keys_c = (keys[:max(2000, n_keys)] * (n_keys // 2000 + 1))[:n_keys]
            body, code, _ = _post_job(s, host, appkey,
                                      {"keys": keys_c, "fields": PROBE_FIELDS})
            if code != 202:
                ck.add(f"④ 取消任务#{attempt} 提交 202", False, f"code={code}")
            job2 = body["job_id"]
            try:
                _wait_running(s, host, appkey, job2,
                              deadline=time.monotonic() + cancel_wait_s / 2)
                # ---- running 态就位：立即发 DELETE（200＋cancelling 停止意向）----
                r = s.delete(f"{host}/v1/batch/jobs/{job2}",
                             headers=_auth(appkey), timeout=30.0)
                ck.add(f"④ DELETE 受理 200＋status=cancelling（#{attempt}）",
                       r.status_code == 200
                       and r.json().get("status") == "cancelling",
                       f"code={r.status_code} status={r.json().get('status')!r}")
                _poll(s, host, appkey, job2, {"cancelled"},
                      deadline=time.monotonic() + cancel_wait_s)
                cancelled_via_running = True
                break
            except Red as exc:
                if "等 running 前已终态" in str(exc):
                    warns.append(f"取消#{attempt}：{exc}")   # 竞态：任务抢先跑完
                    continue                                # 换下一名单重试
                raise                                       # 其余红＝真判据红
        if cancelled_via_running:
            ck.add("④ 取消 running→cancelling→cancelled（AC-6）", True)
            r = s.get(f"{host}/v1/batch/jobs/{job2}/results", headers=_auth(appkey),
                      timeout=30.0)
            ck.add("④ 取消任务无结果可交付（409 job_not_succeeded）",
                   r.status_code == 409
                   and r.json().get("error") == "job_not_succeeded",
                   f"code={r.status_code} err={r.json().get('error')!r}")
        else:
            ck.add("④ 取消 running→cancelling→cancelled（AC-6）", False,
                   f"两次机会均未观测到（任务均抢先终态，窗口不足）：{warns}")

        # ---------- ⑤ 429 并发拒（AC-10） ----------
        print("[S5-⑤] 429 并发拒（长名单任务占住 worker）")
        keys_hold = keys * 3                                # 6000 键≈3~6s 执行窗
        body, code, _ = _post_job(s, host, appkey,
                                  {"keys": keys_hold, "fields": PROBE_FIELDS})
        ck.add("⑤ 占位任务提交 202", code == 202, f"code={code}")
        job3 = body["job_id"]
        _wait_running(s, host, appkey, job3,
                      deadline=time.monotonic() + per_job_timeout_s)
        # 占位任务 running（并发位已占）：第二个任务此刻提交必被 429 拒
        # （AC-3：queued 不占位，但 running 占位在先——分界即本断言的时序）
        body, code, hdr = _post_job(s, host, appkey,
                                    {"keys": keys[:100], "fields": PROBE_FIELDS})
        ck.add("⑤ 并发第二个任务 429（AC-10）", code == 429, f"code={code}")
        ck.add("⑤ 机读码 quota_concurrent_exceeded",
               body.get("error") == "quota_concurrent_exceeded", str(body.get("error")))
        ck.add("⑤ X-RateLimit-Reason=concurrency",
               hdr.get("X-RateLimit-Reason") == "concurrency",
               str(hdr.get("X-RateLimit-Reason")))
        ck.add("⑤ Retry-After 头存在", bool(hdr.get("Retry-After")),
               str(hdr.get("Retry-After")))
        print("[S5-⑤] 收尾：取消占位任务")
        s.delete(f"{host}/v1/batch/jobs/{job3}", headers=_auth(appkey), timeout=30.0)
        try:
            _poll(s, host, appkey, job3, {"cancelled"},
                  deadline=time.monotonic() + 120.0)
        except Red as exc:
            warns.append(f"占位任务收尾未确认 cancelled（不影响判定）：{exc}")
    finally:
        s.close()
        # 机检清单落盘放 finally：红跑同样留痕（M15-7 实证——红处止步时
        # 异常冒泡会跳过尾部写入，缺陷现场反而没清单可查）。
        if artifacts_dir is not None:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / "checks.txt").write_text(NL.join(ck.lines) + NL,
                                                      encoding="utf-8")
    print(f"[S5] {ck.table()}")
    for w in warns:
        print(f"[S5][warn] {w}")
    return 0, warns
