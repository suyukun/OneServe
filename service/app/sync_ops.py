# -*- coding: utf-8 -*-
"""切版/回滚服务层（发布台重设计 SPEC v1.1 §七 B4）——复用既有 CLI，零第二实现。

定位：sync/switch.py（放行三闸＋原子 SET）与 sync/rollback.py（安全闸＋原子 SET）
此前只有 CLI 入口，发布向导③的 [切换到新版]/版本回显无 HTTP 契约。本模块把两脚本
**按子进程原样调用**（闸逻辑一行不动＝「只包不改」），解析其 JSON 输出与退出码
如实映射 HTTP；失败如实报错（人话 reason），不吞不美化。

为什么子进程而非函数复用：两脚本的闸逻辑内联在 argparse 版 main() 里（打印 JSON
退出），抽函数＝改闸文件（违反「零改动」约束）；子进程与手工运维走**同一条代码
路径**，行为逐字节一致（同一 Python、同一 env、同一 Redis 连接口径 SYNC_REDIS_URL）。

status（只读）不走子进程：直接经 sync/common.py（键规则单一事实来源）读指针/
版本元信息/切换日志——读侧无需闸，也不复制任何判定逻辑。

分层纪律：本模块纯逻辑（无 FastAPI），HTTP 编排归 app/admin_api.py；阻塞调用
（Redis/子进程）由端点层 asyncio.to_thread 入线程池。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app import errors
from app.hbase_load import _sync_dir   # sync/ 目录定位唯一实现（env SYNC_DIR>/sync>宿主；复用不复制）

SWITCH_SCRIPT = "switch.py"
ROLLBACK_SCRIPT = "rollback.py"
SWITCH_LOG_TAIL = 10          # status 返回最近切换日志条数
DEFAULT_CLI_TIMEOUT_S = 60.0  # 三闸含 SCAN 计数——宽裕上限，超时如实报 504


class SyncOpsError(RuntimeError):
    """切版服务层可控错误（脚本未就位/Redis 不可达/超时）；端点层映射如实错误响应。"""

    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ---------- 只读 status（经 sync/common.py 键规则单一事实来源） ----------

def _sync_common():
    """载入既有 sync/common.py（键规则/连接唯一实现；延迟导入＋sys.path 就位）。"""
    d = _sync_dir()
    if d is None:
        raise SyncOpsError("LOADER_UNAVAILABLE",
                           "sync/ 脚本目录不可见（switch.py/rollback.py 未就位）："
                           "请确认 <repo>/sync 存在或容器内已挂载 /sync")
    p = str(d)
    if p not in sys.path:
        sys.path.insert(0, p)
    import common as sync_common      # noqa: E402 —— 既有公共件（键规则/连接唯一实现）
    return sync_common


def get_sync_redis():
    """建 sync 侧 Redis 连接（sync/common.redis_conn 同 CLI 口径：env SYNC_REDIS_URL）。

    失败抛 SyncOpsError（如实 503，不吞）——连接口径与 CLI 完全一致，容器内
    SYNC_REDIS_URL 已由 compose 注入（与 service 同一 Redis）。
    """
    sync_common = _sync_common()
    try:
        return sync_common.redis_conn()
    except Exception as exc:  # noqa: BLE001 —— 连接失败如实报，不回退假数据
        raise SyncOpsError(
            "SYNC_REDIS_UNAVAILABLE",
            f"同步 Redis 不可达（SYNC_REDIS_URL={os.environ.get('SYNC_REDIS_URL', '缺省 redis://127.0.0.1:16379/0')}）："
            f"{str(exc)[:200]}") from exc


def _table_domain(table: str) -> str:
    """表参数 → 键空间域段（空＝不切域＝存量单域口径；非法表名如实 400，不进 CLI）。"""
    sync_common = _sync_common()
    t = (table or "").strip()
    if not t:
        return ""
    try:
        return sync_common.C.key_domain(t)
    except ValueError as exc:
        raise SyncOpsError("INVALID_TABLE", str(exc), 400) from exc


def sync_status(r, table: str = "") -> dict:
    """指定表的指针/版本元信息/最近切换日志（只读；r＝sync common 口径连接，可注入测试）。

    - table 缺省＝不切域（存量单域口径，向导③之外的既有调用零改动）；传表名则键空间
      按表隔离：指针 tags:<表>:latest、日志 sync:switch:<表>:log，各表互不可见；
    - live 无指针（未上线）→ liveVersion=null＋如实 note（向导③「当前线上版本：无」）；
    - 切换日志条目坏 JSON 跳过不计（日志面不因单条脏数据拖垮），计数如实。
    """
    sync_common = _sync_common()     # 键规则（指针键/日志键）单一事实来源
    domain = _table_domain(table)
    if domain:
        scope = sync_common.C.table_scope(domain)   # 只读也走作用域：防多表并发下键常量串台
    else:
        from contextlib import nullcontext
        scope = nullcontext("")
    with scope:
        live = sync_common.get_live(r)
        meta = sync_common.ver_meta_get(r, live) if live else None
        alias_key = sync_common.C.ALIAS_KEY
        switches, bad = [], 0
        for raw in r.lrange(sync_common.C.SWITCH_LOG, 0, SWITCH_LOG_TAIL - 1):
            try:
                switches.append(json.loads(raw))
            except ValueError:
                bad += 1
    note = "未上线（指针不存在）" if live is None else f"指针 {alias_key} → {live}"
    if bad:
        note += f"；切换日志 {bad} 条坏 JSON 已跳过"
    return {"ok": True, "table": domain or None, "liveVersion": live,
            "versionMeta": meta, "recentSwitches": switches, "note": note}


# ---------- 切换/回滚（子进程包既有 CLI；闸逻辑零改动、零复制） ----------

def _run_cli(script_name: str, args: list[str], *, timeout_s: float | None = None) -> dict:
    """跑 sync/ 下既有 CLI 脚本 → {rc, out(dict|None), stdout, stderr}。

    env 原样继承（CLI 自读 SYNC_REDIS_URL——与容器/手工运维同一口径，不另建连接）。
    脚本缺失/超时抛 SyncOpsError（如实 503/504，不吞不美化）。
    """
    d = _sync_dir()
    if d is None:
        raise SyncOpsError("LOADER_UNAVAILABLE",
                           "sync/ 脚本目录不可见（switch.py/rollback.py 未就位）")
    script = d / script_name
    if not script.is_file():
        raise SyncOpsError("LOADER_UNAVAILABLE", f"sync/{script_name} 不存在（未就位）")
    cmd = [sys.executable, str(script), *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_s if timeout_s is not None else float(
                os.environ.get("SWITCH_CLI_TIMEOUT_S", DEFAULT_CLI_TIMEOUT_S)),
            cwd=str(d))
    except subprocess.TimeoutExpired as exc:
        raise SyncOpsError(
            "SWITCH_CLI_TIMEOUT",
            f"sync/{script_name} 执行超时（SWITCH_CLI_TIMEOUT_S）——切换未确认完成，"
            f"请查 Redis 指针后重试", 504) from exc
    except OSError as exc:  # noqa: BLE001 —— 解释器/脚本层故障如实报
        raise SyncOpsError("SWITCH_CLI_CRASH",
                           f"sync/{script_name} 无法执行：{str(exc)[:200]}", 502) from exc
    out = None
    for ln in reversed((proc.stdout or "").strip().splitlines()):
        try:
            parsed = json.loads(ln)
            if isinstance(parsed, dict):
                out = parsed
                break
        except ValueError:
            continue
    return {"rc": proc.returncode, "out": out,
            "stdout": proc.stdout or "", "stderr": proc.stderr or ""}


def _http_result(action: str, cli: dict, **echo) -> tuple[int, dict]:
    """CLI 结果 → (HTTP 状态码, 响应体)：人话如实，不吞不美化。

    rc=0 → 200（幂等无动作也 200，note 如实带出）；rc=1 → 409（闸拒绝，reason 原样）；
    其余/解析不出 JSON → 502（进程级故障，stderr 尾部如实带出）。
    """
    out = cli.get("out")
    if cli.get("rc") == 0 and isinstance(out, dict) and out.get("ok"):
        return 200, {"ok": True, **echo, **out}   # CLI 真实字段优先（echo 只补缺，不覆盖）
    if cli.get("rc") == 1 and isinstance(out, dict) and out.get("reason"):
        return 409, {"ok": False, "error": f"{action}_rejected",
                     "reason": out["reason"], **echo}
    tail = (cli.get("stderr") or cli.get("stdout") or "").strip()[-300:]
    return 502, {"ok": False, "error": "SWITCH_CLI_CRASH",
                 "message": f"sync/{action} 输出异常（rc={cli.get('rc')}，未确认完成）：{tail}", **echo}


def run_switch(version: str, table: str = "") -> tuple[int, dict]:
    """POST /v1/admin/publish/sync/switch 服务层：过三道闸切**指定表**指针（复用 switch.py）。

    table 缺省＝不切域（存量单域口径）；三闸（已加载/对账绿/空间非空）全部在
    所指键空间判定——别的表同版本号互不影响。
    """
    version = (version or "").strip()
    if not version:
        return 400, {"ok": False, "error": errors.INVALID_VERSION,
                     "message": "version 必填（目标版本 YYYYMMDD，如 20260929）"}
    domain = _table_domain(table)
    args = (["--table", domain] if domain else []) + ["--version", version]
    return _http_result("switch", _run_cli(SWITCH_SCRIPT, args),
                        version=version, table=domain or None)


def run_rollback(to: str = "", table: str = "") -> tuple[int, dict]:
    """POST /v1/admin/publish/sync/rollback 服务层：回滚**指定表**到旧版（复用 rollback.py）。

    to 缺省＝CLI 自动取切换日志里的上一版（既有语义，不复制判定）；
    table 缺省＝不切域（存量单域口径）；传表名则只动本表指针，别的表零影响。
    """
    domain = _table_domain(table)
    to = (to or "").strip()
    args = (["--table", domain] if domain else []) + (["--to", to] if to else [])
    return _http_result("rollback", _run_cli(ROLLBACK_SCRIPT, args), to=to or None, table=domain or None)
