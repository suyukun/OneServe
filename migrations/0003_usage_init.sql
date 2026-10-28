-- ============================================================
-- OneServe 用量域 DDL（migrations/0003_usage_init.sql · ADR-0023 实施卡）
-- 权威依据：ADR-0023 第 2/3/4 条 ＋ 用量计费报表 P0 整体设计 v1.1 §3.3/§3.5
-- 实例＝ADR-0021 oneserve-metadata-db（pg 17）同实例双 schema 分域（meta/usage），
--   不建第二实例；usage schema 座位已由 configs/metadata-db/init/02_schemas.sql 建好
--   （0001 头注「本文件不建 usage_daily（归 ADR-0023 实施卡）」即本文件）。
-- 执行角色：meta_migrate（usage schema 属主，迁移窗口）；执行方式同 0001：
--   docker compose -f compose.metadata.yml -p oneserve-meta exec -T metadata-db \
--     psql -U meta_migrate -d oneserve_meta -v ON_ERROR_STOP=1 -f - < migrations/0003_usage_init.sql
-- 幂等设计：CREATE TABLE / CREATE INDEX 全 IF NOT EXISTS；分区建表走 pg_class 存在性
--   判断；GRANT 重复执行＝no-op。重复执行＝no-op（仅 NOTICE，exit 0）——对齐 0001 幂等先例。
--
-- 【usage_daily 四键唯一（ADR-0023 第 3 条）】(day, tenant_id, caller, endpoint)：
--   同日转租户前后用量物理分属两行，租户分列才可实现。tenant_id 落「用量发生日
--   归属快照」，未登记主体/九元组缺列为空串（非 NULL）——NULL 会使 pg 唯一约束
--   对未登记主体失效（两条 NULL 并存绕过四键唯一），空串同义「无归属」（计费面
--   判定 tenant_id <> ''）且唯一约束真实生效，故以 NOT NULL DEFAULT '' 实现
--   设计稿 §3.3「未登记主体为空」口径。
-- 【计费面/总量面（ADR-0023 第 4 条）】计费面＝tenant_id 非空且 meta.tenant.tenant_type
--   不为 sandbox（沙箱租户实体化后 tenant_id 亦非空，单看非空会把沙箱量误拦进出账门）；
--   总量面（含未登记主体、沙箱、归属空缺）条数单列登记、不拦门。判定 SQL 见
--   scripts/usage_reconcile.py（对账侧唯一实现）。
-- 【分区滚动预建机制】本文件预建 2023-01 起历史＋未来月至 2027-12（任务卡口径
--   「至少建到 2027-12」）；此后滚动预建由 scripts/usage_rollup.py 的 ensure_partition()
--   承担——落账前检查目标日所在月分区，缺则 CREATE TABLE IF NOT EXISTS ... PARTITION OF
--   自动补建（至少提前一个自然月），机制说明同步落该脚本头注。分区缺失的插入会
--   直接失败（设计稿 §3.3「否则历史回填插入直接失败」），故脚本侧兜底在建写前执行。
-- ============================================================

-- ------------------------------------------------------------
-- 1. usage_daily（JSONL 按日重放投影·权威账源；ADR-0023 决定第 1/3 条）
--    口径唯一＝METERING_CALIBER（主体×端点×日、2xx 计成功、东八区自然日、网关即拒不计）；
--    写入一律按日整行覆盖 upsert（不增量合并，ADR 第 2 条），落账任务＝scripts/usage_rollup.py。
--    分区：day 按月 RANGE；唯一约束含分区键（pg 分区表唯一约束必须含分区键，四键天然满足）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage.usage_daily (
    day                date NOT NULL,
    tenant_id          text NOT NULL DEFAULT ''
                       CONSTRAINT usage_daily_tenant_id_format_check
                       CHECK (tenant_id = ''
                              OR tenant_id ~ '^[a-z0-9]([a-z0-9_-]{0,62}[a-z0-9])?$'),  -- 空串＝未登记/归属空缺（总量面）；非空值域 TENANT_ID_RE 同源（appkeys.py:69／0001 tenant 同款）
    caller             text NOT NULL,
    endpoint           text NOT NULL,
    calls              bigint NOT NULL DEFAULT 0 CONSTRAINT usage_daily_calls_check CHECK (calls >= 0),
    success            bigint NOT NULL DEFAULT 0 CONSTRAINT usage_daily_success_check CHECK (success >= 0),
    fail               bigint NOT NULL DEFAULT 0 CONSTRAINT usage_daily_fail_check CHECK (fail >= 0),
    lat_sum            bigint NOT NULL DEFAULT 0 CONSTRAINT usage_daily_lat_sum_check CHECK (lat_sum >= 0),
    lat_cnt            bigint NOT NULL DEFAULT 0 CONSTRAINT usage_daily_lat_cnt_check CHECK (lat_cnt >= 0),
    source_event_count bigint NOT NULL DEFAULT 0
                       CONSTRAINT usage_daily_source_event_count_check CHECK (source_event_count >= 0),
                       -- 该日重放事件条数（对账锚点，ADR-0023 第 3/4 条；坏行/脏 ts 不计入）
    source             text NOT NULL DEFAULT 'rollup'
                       CONSTRAINT usage_daily_source_check CHECK (source IN ('rollup', 'backfill')),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT usage_daily_pk UNIQUE (day, tenant_id, caller, endpoint)  -- 四键唯一（ADR 第 3 条）
) PARTITION BY RANGE (day);

COMMENT ON TABLE usage.usage_daily IS
  '用量日聚合（权威账源）：JSONL 不可变分片按日重放投影，四键唯一 (day,tenant_id,caller,endpoint)，按月 RANGE 分区（ADR-0023）；快照只做热展示不做账源';
COMMENT ON COLUMN usage.usage_daily.tenant_id IS
  '归属快照（用量发生日）：九元组直取，未登记/缺列为空串（非 NULL，保四键唯一生效）；as-of 解析工具归 ADR-0023 实施卡后续（不按当前凭证表翻判历史）';
COMMENT ON COLUMN usage.usage_daily.source_event_count IS
  '该日重放事件条数（对账锚点）：usage_daily vs JSONL vs Redis 三方对账以本列对分片条数（ADR-0023 第 4 条）';
COMMENT ON COLUMN usage.usage_daily.source IS
  '写入来源：rollup=T+1~T+3 滚动重算／backfill=回填与对账修正（以 JSONL 重放为准修表）';

-- ------------------------------------------------------------
-- 2. 分区预建：2023-01 ～ 2027-12 共 60 个月（历史回填 2023 起＋未来滚动；
--    幂等＝pg_class 存在性判断，重复执行跳过已存在分区）。
--    2027-12 之后的滚动预建归 scripts/usage_rollup.py ensure_partition()（见头注）。
-- ------------------------------------------------------------
DO $$
DECLARE
    d     date := date '2023-01-01';
    upper date;
    part  text;
BEGIN
    WHILE d < date '2028-01-01' LOOP
        upper := (d + interval '1 month')::date;
        part  := 'usage_daily_y' || to_char(d, 'YYYY') || 'm' || to_char(d, 'MM');
        IF NOT EXISTS (SELECT 1 FROM pg_class c
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname = 'usage' AND c.relname = part) THEN
            EXECUTE format(
                'CREATE TABLE usage.%I PARTITION OF usage.usage_daily FOR VALUES FROM (%L) TO (%L)',
                part, d, upper);
        END IF;
        d := upper;
    END LOOP;
END $$;

-- ------------------------------------------------------------
-- 3. 索引（报表下钻取数面：按租户/按主体的跨日窗口查询；唯一约束已覆盖 day 前缀）
--    分区表上建索引自动级联全部分区；IF NOT EXISTS 幂等。
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS usage_daily_tenant_day_idx ON usage.usage_daily (tenant_id, day);
CREATE INDEX IF NOT EXISTS usage_daily_caller_day_idx ON usage.usage_daily (caller, day);

-- ------------------------------------------------------------
-- 4. usage 域分域授权（角色 usage_app 读写／usage_ro 只读，ADR-0023 第 3 条；
--    角色本体已由 configs/metadata-db/init/01_roles.sh 建——NOLOGIN 权限角色，
--    该脚本头注预留「ADR-0023 实施卡接入时 GRANT 给登录角色」即下方 membership：
--    落账任务经 meta_app 登录获 usage 读写，报表经 meta_ro 获 usage 只读，互不越域）。
--    幂等兜底：schema USAGE 与默认权限已在 0001/02_schemas 落位，此处对已存在表
--    （父表＋60 分区）补全量 GRANT，重复执行安全。
-- ------------------------------------------------------------
GRANT USAGE ON SCHEMA usage TO usage_app, usage_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA usage TO usage_app;
GRANT SELECT ON ALL TABLES IN SCHEMA usage TO usage_ro;
GRANT usage_app TO meta_app;   -- 落账任务（usage_rollup）通道：meta_app 登录即获 usage 读写
GRANT usage_ro  TO meta_ro;    -- 报表/对账只读通道：meta_ro 登录即获 usage 只读
