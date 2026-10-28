# -*- coding: utf-8 -*-
"""同步链路集中配置（task-006 · SPEC §4.3）。

纪律：对账阈值显式钉死在此（任务书口径），改动阈值＝只改本文件并留痕评审；
脚本内不得散落第二处阈值。运行期可用环境变量覆盖 Redis 地址。
"""
import os
import re
import threading
from contextlib import contextmanager

# —— Redis 连接（演练端口隔离：宿主 16379，避开 9080/9180/8000/6379）——
REDIS_URL = os.environ.get("SYNC_REDIS_URL", "redis://127.0.0.1:16379/0")

# —— 键空间设计（SPEC §4.1 双缓冲 / §4.3 原子切别名；2026-09-29 起按表参数化）——
# 键空间按表隔离（对抗审查裁决 B：每表独立指针＋独立版本空间，防 A 表切版顶掉 B 表）：
#   数据键 tags:<表>:<版本YYYYMMDD>:<上游供数键>；指针 tags:<表>:latest；
#   元信息 sync:ver:<表>:<版本>；对账 sync:recon:<表>:<版本>；切换日志 sync:switch:<表>:log。
# 表名口径＝裸表名（HBase qualifier，剥 namespace 前缀；news 域先例同构：tags:news:*）。
# 下面五个常量＝存量单域键空间（不切域时的静止值）：不带表参数的旧链路
# （loader/reconcile/read_user/delete_apply/ecs_seed…）**逐字节不变**；带表参数的
# 链路（hbase_to_redis/switch/rollback/status…）经 use_table()/table_scope() 切到
# tags:<表>:… 域。注意：缺省语义＝「不切域」而非「切到 user 域」——存量 sync:ver:<版本>
# 无表段，显式 use_table("user") 会得到 sync:ver:user:<版本>（新式口径），故缺省一律不切。
DEFAULT_TABLE = ""                 # 缺省＝不切域（存量单域口径；显式传表名才切域）
TABLE_DOMAIN_RE = re.compile(r"^[A-Za-z0-9_]+$")   # 裸表名（键段安全：无冒号，四段结构不破）
NS = "tags:user"                    # 数据键：tags:<表>:<版本YYYYMMDD>:<上游供数键>（缺省表=user）
ALIAS_KEY = "tags:user:latest"      # meta 指针键：原子切换点（单条 SET，读写经它解析版本）
VER_META = "sync:ver:{version}"     # 版本元信息 hash：rows/tag_cols/source_sha256/coverage_src/data_as_of…
RECON_META = "sync:recon:{version}" # 对账结果 hash：pass/checked_at/明细 JSON（switch 据此放行）
SWITCH_LOG = "sync:switch:log"     # 切换/回滚审计 list（LPUSH JSON，最新在前）
DELETE_LOG = "sync:delete:log"     # 删除权传导审计 list（LPUSH JSON，最新在前）


def key_domain(table: str) -> str:
    """表名 → 键空间域段（裸表名）：剥 namespace 前缀（default:usr_basic_info → usr_basic_info）。

    与 ops_config.resolve_schema_csv 的表级字典定位同口径（表名剥前缀取裸名）；
    非法域段（空/含冒号外字符）显式抛 ValueError——键段必须无冒号，否则
    tags:<表>:<版本>:<键> 四段结构被破坏。
    """
    domain = (table or "").strip()
    if ":" in domain:
        domain = domain.split(":", 1)[1]
    if not TABLE_DOMAIN_RE.fullmatch(domain):
        raise ValueError(f"表名 {table!r} 无法用作键空间域段（须为裸表名，字符集 [A-Za-z0-9_]）")
    return domain


def use_table(table: str):
    """把模块键空间切到指定表域（NS/ALIAS_KEY/VER_META/RECON_META/SWITCH_LOG/DELETE_LOG）。

    CLI（switch/rollback/hbase_to_redis 等）单进程单线程：argparse 后调一次即可，
    闸体/读写代码引用的 C.NS、C.ALIAS_KEY 等运行期求值，随本调用按表归位——
    三闸判定逻辑零改动（只参数化键名）。服务进程内并发请用 table_scope()（带锁防串台）。
    """
    global NS, ALIAS_KEY, VER_META, RECON_META, SWITCH_LOG, DELETE_LOG
    domain = key_domain(table)
    NS = f"tags:{domain}"
    ALIAS_KEY = f"{NS}:latest"
    VER_META = f"sync:ver:{domain}:{{version}}"
    RECON_META = f"sync:recon:{domain}:{{version}}"
    SWITCH_LOG = f"sync:switch:{domain}:log"
    DELETE_LOG = f"sync:delete:{domain}:log"
    return domain


# 存量单域键空间静止值（模块加载时快照）：不切域口径的权威值（守卫测试比对用）
LEGACY_KEYSPACE = (NS, ALIAS_KEY, VER_META, RECON_META, SWITCH_LOG, DELETE_LOG)

_TABLE_SCOPE_LOCK = threading.RLock()   # 可重入：允许同线程嵌套作用域（外层夹具/编排包内层加载）


@contextmanager
def table_scope(table: str):
    """服务进程内的按表键空间作用域：加锁切域 → 执行 → 恢复原域（防并发串台/污染）。

    sync_ops（status 只读）与 hbase_load（加载＋对账）在服务进程线程池里跑，
    多表并发时模块级键常量会被交错改写——本作用域串行化「切域→操作→还原」，
    保证任一时刻操作看到的键空间与其表参数一致。CLI 单线程进程用 use_table 即可。
    """
    global NS, ALIAS_KEY, VER_META, RECON_META, SWITCH_LOG, DELETE_LOG
    domain = key_domain(table)
    with _TABLE_SCOPE_LOCK:
        saved = (NS, ALIAS_KEY, VER_META, RECON_META, SWITCH_LOG, DELETE_LOG)
        use_table(domain)
        try:
            yield domain
        finally:
            NS, ALIAS_KEY, VER_META, RECON_META, SWITCH_LOG, DELETE_LOG = saved

# —— 表级 schema 元数据与 tabver 索引（卡2 装载副产品 · 拆分设计 §二.4/§六，B3.2 契约）——
# 唯一写入点＝sync/hbase_to_redis.write_space 尾部（CLI/HTTP 两入口同源）；键随版本走。
SCHEMA_META_KEY = "sync:schema:{table}:{version}"   # HASH：schema(JSON)/field_count/source/table/version/generated_at
TABVER_KEY = "sync:tabver:{table}"                  # SET：该表已装载版本（YYYYMMDD）
TABLES_INDEX_KEY = "sync:tables"                    # SET：已装载裸表名全集（跨表枚举索引，卡3 消费）

# —— 加载 ——
PIPE_BATCH = 1000                   # pipeline 批大小（行）

# —— 对账三查阈值（SPEC §4.3，任务书钉死口径）——
# 查一 行数：必须全等（无百分比容差，硬判等）
RECON_SAMPLE_SIZE = 1000            # 查二 抽样逐值：抽样条数
RECON_SAMPLE_SEED = 42              # 抽样种子（确定性，可复跑复算/注入演练同口径）
RECON_COVERAGE_DEV_MAX = 0.20       # 查三 覆盖率：相对偏差阈值，>20% 拒切

# —— 模拟源（表结构蓝本：主宽表 demo_src.tags_wide 297 列）——
# TODO(卡2): 297 宽表字典换源为表级 schema 元数据（装载副产品）；CSV 缓删至卡2 交接（卡5 2026-09-30）
SCHEMA_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "schema_usr_label_wide_297.csv")
KEY_COLUMN = "usr_key_sm3"          # 导出 CSV 首列＝键（列名沿造数工具；键由数仓导出侧生成，ADR-0001）
