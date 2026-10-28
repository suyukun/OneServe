# -*- coding: utf-8 -*-
"""异步装载任务管理（B2 · 维护者 2026-10-09 拍板 B 方案：后台任务＋单进度数字，不做三阶段）。

刻意不做的事（防过度设计，维护者「千万不要搞得太复杂」）：
  · 不持久化——服务重启任务即失（进度端点回 not_found，如实；装载幂等闸兜底可重提）；
  · 不做队列/并发限制——单实例管理面，同时点多个装载本来就会被表级幂等闸拒；
  · 不做三阶段进度——一个百分比数字（written/total），阶段差异（读 HBase vs 写 Redis）
    以 progress 停在 0% 区分（读扫描期间无回调），足够辨别「卡死 vs 在跑」。

线程安全：TASKS 单 dict，键值赋值在 GIL 下原子；progress_cb 从装载线程调用，
只做字段赋值，无读改写竞态。
"""
from __future__ import annotations

import threading
import uuid

TASKS: dict[str, dict] = {}
_LOCK = threading.Lock()


def new_task(table: str, version: str) -> str:
    task_id = uuid.uuid4().hex[:12]
    with _LOCK:
        TASKS[task_id] = {
            "id": task_id, "table": table, "version": version,
            "status": "running", "progress": 0,
            "result": None, "error": None,
        }
    return task_id


def run(task_id: str, fn, *, kwargs: dict) -> None:
    """后台线程执行 fn(**kwargs)；进度经 fn 的 progress_cb 回写；终态落 result/error。"""
    task = TASKS.get(task_id)
    if task is None:
        return

    def _cb(written: int, total: int) -> None:
        pct = int(written * 100 / total) if total else 0
        task["progress"] = min(pct, 99)   # 终局 100% 由完成态标定，防止末批跳满

    try:
        res = fn(**kwargs, progress_cb=_cb)
        task["result"] = res
        task["progress"] = 100
        task["status"] = "done"
    except Exception as exc:  # noqa: BLE001 —— 任意失败如实落终态（含机读码异常的 code/message）
        code = getattr(exc, "code", None) or type(exc).__name__
        task["error"] = {"error": code, "message": str(exc)[:300]}
        task["status"] = "failed"
    finally:
        task.setdefault("thread", None)


def view(task_id: str) -> dict | None:
    t = TASKS.get(task_id)
    if t is None:
        return None
    out = {"id": t["id"], "table": t["table"], "version": t["version"],
           "status": t["status"], "progress": t["progress"]}
    if t["status"] == "done":
        out["result"] = t["result"]
    if t["status"] == "failed":
        out["error"] = t["error"]
    return out


def start_thread(task_id: str, fn, *, kwargs: dict) -> threading.Thread:
    th = threading.Thread(target=run, args=(task_id, fn), kwargs={"kwargs": kwargs},
                          daemon=True, name=f"load-{task_id}")
    th.start()
    return th
