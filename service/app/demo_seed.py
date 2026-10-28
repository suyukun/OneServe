# -*- coding: utf-8 -*-
"""demo 档自动灌数（一键开箱批 2026-10-28）：redis 空 → 起服务前自动 seed，零手工步骤。

北极星（维护者原话）：git clone → docker compose up → 浏览器打开 localhost:8000 全功能
可用。本模块是 compose service 入口的前置步（见 docker-compose.yml service.command），
四段灌数全部**哨兵键幂等**：已有数据不重灌；任何一段失败只记日志、不阻断 uvicorn 启动。

纪律：不重写既有灌数逻辑，全部复用既有单一事实来源——
  ① 用户基本信息（partner-demo 演示凭证的第一个查询）：直接执行仓内
     scripts/seed_demo.sh（compose 只读挂载到 /seed/seed_demo.sh；REDIS_MODE=py
     直连 REDIS_URL，与脚本自身文档的容器形态同款）——口径/造数/自检零复制；
  ② 用户资产标签（首页 KPI coveredUsers/新鲜度/试用台主数据）：复用 sync/ 真装载链
     gen_mock_source → loader → reconcile → switch（in-process 调用＋use_table 切到
     services.yaml 声明的 usr_asset_tag 域；参数与 scripts/ecs_seed.sh 完全同款：
     20260920 / 2000 户 / fill 0.30 / seed 42——同 seed 确定性复现）；
  ③ 资讯标签：复用 sync/gen_news_data.py（services.yaml 未注册 news_tag_query 时
     脚本自拒，本模块如实跳过——不造默认值）；
  ④ 门户公告：portal:announcements 两条，文案与 scripts/ecs_seed.sh 逐字同源。

哨兵键（与各写入方约定一致）：
  tags:usr_basic_info:latest / tags:usr_asset_tag:latest / tags:news:latest /
  portal:announcements——任一在场即跳过对应段（幂等，重灌请显式清键或 docker compose down -v）。

用法：python -m app.demo_seed（容器内；compose service.command 第一步）
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger("oneserve.demo_seed")

# —— 演示参数（与 scripts/ecs_seed.sh 同款，确定性：同参数复跑同数据；
#    注意全为字符串——本模块经 sys.argv 复用各脚本 argparse 入口，argv 必须全 str） ——
ASSET_VERSION = "20260920"     # 用户资产标签数据日期＝版本号
ASSET_USERS = "2000"
ASSET_FILL = "0.30"
ASSET_SEED = "42"

ANNOUNCE_ITEMS = [             # 与 scripts/ecs_seed.sh ⑦ 逐字同源
    '{"tag":"新增","title":"风险域 5 个字段通过 OA 白名单审批上线，目录已更新",'
    '"time":"2026-09-18T10:00:00+08:00"}',
    '{"tag":"维护","title":"9 月 22 日 02:00-04:00 同步窗口例行维护，期间点查不受影响",'
    '"time":"2026-09-15T09:00:00+08:00"}',
]

SEED_DEMO_SH = "/seed/seed_demo.sh"   # compose 只读挂载（./scripts/seed_demo.sh）
SYNC_DIR = "/sync"                     # compose 只读/读写挂载（./sync）


def _connect():
    """连 demo Redis（与 compose service 的 REDIS_URL 同源）；失败返回 None（不阻断启动）。"""
    import redis

    url = os.environ.get("REDIS_URL") or os.environ.get("SYNC_REDIS_URL") \
        or "redis://oneserve-redis:6379/0"
    try:
        r = redis.Redis.from_url(url, decode_responses=True,
                                 socket_connect_timeout=3, socket_timeout=30)
        r.ping()
        return r
    except Exception as exc:  # noqa: BLE001 —— redis 不可达：灌不了数，但 uvicorn 照常起
        logger.warning("demo_seed: redis 不可达（%s），跳过自动灌数", exc)
        return None


def _run(func, label: str) -> None:
    """段级兜底：任何异常只记日志不外抛——灌数失败不阻断 uvicorn（诚信线：页面如实降级）。"""
    try:
        func()
    except Exception:  # noqa: BLE001
        logger.exception("demo_seed: %s 段失败（跳过，不影响启动）", label)


def _seed_basic_info(r) -> None:
    """① 用户基本信息：复用仓内 seed_demo.sh（哨兵：tags:usr_basic_info:latest）。"""
    if r.get("tags:usr_basic_info:latest"):
        print("[demo_seed] basic-info 已有线上版本，跳过（幂等）", flush=True)
        return
    if not os.path.isfile(SEED_DEMO_SH):
        logger.warning("demo_seed: %s 不在容器内（compose 未挂载），跳过 basic-info 灌数", SEED_DEMO_SH)
        return
    env = dict(os.environ,
               REDIS_URL=os.environ.get("REDIS_URL") or "redis://oneserve-redis:6379/0",
               REDIS_MODE="py")
    proc = subprocess.run(["bash", SEED_DEMO_SH], env=env,   # noqa: S603 —— 固定脚本路径
                          capture_output=True, text=True, timeout=180)
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    print("[demo_seed] seed_demo.sh rc=%s\n  %s" % (proc.returncode, "\n  ".join(tail)),
          flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f"seed_demo.sh 退出码 {proc.returncode}：（stderr 尾部）"
                           f"{(proc.stderr or '').strip().splitlines()[-1:]}")


def _seed_usr_asset_tag(r) -> None:
    """② 用户资产标签：sync 真装载链（哨兵：tags:usr_asset_tag:latest）。

    loader.py 无 --table（存量单域口径），故经 sync_config.use_table 先切域——
    键空间随 services.yaml 的 usr_asset_tag 域（tags:usr_asset_tag:*），与
    hbase_to_redis 装载、kv_executor 读取、portal _domain_state 同一真源。
    三步退出码非 0 即抛（对账红＝不切指针，线上无脏数据，页面如实降级）。
    """
    if r.get("tags:usr_asset_tag:latest"):
        print("[demo_seed] usr_asset_tag 已有线上版本，跳过（幂等）", flush=True)
        return
    if not os.path.isdir(SYNC_DIR):
        logger.warning("demo_seed: %s 不在容器内，跳过用户标签灌数", SYNC_DIR)
        return
    sys.path.insert(0, SYNC_DIR)
    import sync_config as C
    C.use_table("usr_asset_tag")                      # 键空间切域（单进程单线程，CLI 同法）

    import gen_mock_source
    csv_path = "/tmp/oneserve-demo-seed-user.csv"
    sys.argv = ["gen_mock_source.py", "--date", ASSET_VERSION,
                "--users", str(ASSET_USERS), "--fill", str(ASSET_FILL),
                "--seed", str(ASSET_SEED), "--out", csv_path]
    gen_mock_source.main()                            # 造数：gmssl 真 SM3，零明文落库

    import loader
    sys.argv = ["loader.py", "--csv", csv_path, "--version", ASSET_VERSION]
    if loader.main() != 0:
        raise RuntimeError("sync loader 失败（见上方 JSON 输出）")

    import reconcile
    sys.argv = ["reconcile.py", "--csv", csv_path, "--version", ASSET_VERSION,
                "--table", "usr_asset_tag"]
    if reconcile.main() != 0:
        raise RuntimeError("对账三查未全绿，拒切（线上无数据，页面如实降级）")

    import switch
    sys.argv = ["switch.py", "--version", ASSET_VERSION, "--table", "usr_asset_tag"]
    if switch.main() != 0:
        raise RuntimeError("原子切换被三闸拒绝（对账未绿或空间为空）")
    print("[demo_seed] usr_asset_tag 装载链绿＋指针已切（v%s，%s 户）"
          % (ASSET_VERSION, ASSET_USERS), flush=True)


def _seed_news(r) -> None:
    """③ 资讯标签：复用 gen_news_data（哨兵：tags:news:latest；未注册如实跳过）。"""
    if r.get("tags:news:latest"):
        print("[demo_seed] news 已有线上版本，跳过（幂等）", flush=True)
        return
    if not os.path.isdir(SYNC_DIR):
        logger.warning("demo_seed: %s 不在容器内，跳过资讯灌数", SYNC_DIR)
        return
    sys.path.insert(0, SYNC_DIR)
    import gen_news_data
    sys.argv = ["gen_news_data.py", "--count", "1000",
                "--version", "20260921", "--seed", ASSET_SEED]
    if gen_news_data.main() != 0:
        # services.yaml 未注册 news_tag_query（开源版现役口径）＝脚本自拒：如实跳过，不造默认值
        print("[demo_seed] news 域未在 services.yaml 注册，跳过（非错误）", flush=True)
        return
    print("[demo_seed] news 资讯 1000 条已上线（v20260921）", flush=True)


def _seed_announcements(r) -> None:
    """④ 门户公告（哨兵：portal:announcements；文案与 ecs_seed 逐字同源）。"""
    if r.exists("portal:announcements"):
        print("[demo_seed] 公告已存在，跳过（幂等）", flush=True)
        return
    r.rpush("portal:announcements", *ANNOUNCE_ITEMS)
    print("[demo_seed] 公告 %d 条已写入" % len(ANNOUNCE_ITEMS), flush=True)


def main() -> int:
    """入口：永不抛出（返回值恒 0）——灌数是尽力而为的前置步，启动不可被它阻断。"""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    r = _connect()
    if r is None:
        return 0
    print("[demo_seed] 开始 demo 档自动灌数（哨兵幂等；复用 seed_demo.sh＋sync 装载链）",
          flush=True)
    for label, fn in (("basic-info", lambda: _seed_basic_info(r)),
                      ("usr_asset_tag", lambda: _seed_usr_asset_tag(r)),
                      ("news", lambda: _seed_news(r)),
                      ("announcements", lambda: _seed_announcements(r))):
        _run(fn, label)
    print("[demo_seed] 自动灌数结束：" + json.dumps({"sentinels": {
        "basic_info": bool(r.get("tags:usr_basic_info:latest")),
        "usr_asset_tag": bool(r.get("tags:usr_asset_tag:latest")),
        "news": bool(r.get("tags:news:latest")),
        "announcements": bool(r.exists("portal:announcements")),
    }}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
