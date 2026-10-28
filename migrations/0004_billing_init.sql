-- ============================================================
-- OneServe 计费域 DDL（migrations/0004_billing_init.sql · ADR-0024 实施卡·空价目试运行）
-- 权威依据：ADR-0024 决定第 1/2/3/4/6/8/9 条 ＋ 用量计费报表 P0 整体设计 v1.1 §5
-- 实例＝ADR-0021 oneserve-metadata-db 同实例；表落 usage schema（计费表组随「用量计费」
--   本域——设计 v1.1 §3「表域：schema meta＋schema usage（本域）」；不建新 schema／新角色：
--   真实应用通道 meta_migrate 无库级 CREATE／无 CREATEROLE（configs/metadata-db/init/01_roles.sh），
--   新建 schema/角色会在真实 apply 爆权限红——分域授权沿 0003 先例复用 usage_app/usage_ro）。
-- 执行角色：meta_migrate（usage schema 属主，迁移窗口）；执行方式同 0001/0003：
--   docker compose -f compose.metadata.yml -p oneserve-meta exec -T metadata-db \
--     psql -U meta_migrate -d oneserve_meta -v ON_ERROR_STOP=1 -f - < migrations/0004_billing_init.sql
-- 幂等设计：CREATE TABLE / CREATE INDEX 全 IF NOT EXISTS，约束全部内联建表语句；
--   GRANT 重复执行＝no-op；重复执行＝no-op（仅 NOTICE，exit 0）——对齐 0001/0003 幂等先例。
--
-- 【表族四件（ADR-0024 决定第 2/3/4 条）】
--   usage.price_item       价目主档（计价维度＝服务，可细化至接口【待定口径先存原值】；三模型枚举）
--   usage.price_tier       阶梯区间表（三模型统一落区间表：按次＝单区间单价；套餐＝额度内 0 价档
--                          ＋超额单价档；阶梯＝多档分级单价；区间下界含／上界不含，末档 NULL＝不封顶）
--   usage.billing_period   账期（自然月，period_start＝当月首日，东八区月切；状态机
--                          draft→reconciled→confirmed→written_off，核销可选默认不做；
--                          三硬前置核查留痕三列＋欠费逾期标记＋OA 单号纯登记字段）
--   usage.bill_line        账单行（四键 (period_start,tenant_id,caller,endpoint)＝PK 防重；
--                          双快照＝逐行用量值＋source_event_count 与成交单价；空价目试运行
--                          priced=false、金额 0 元且不计应收——ADR 决定第 9 条）
-- 【计费面口径（与 usage_reconcile.db_days 同源同句，改须同批】tenant_id 非空 且 meta.tenant
--   实存该租户 且 tenant_type≠sandbox；沙箱/未登记/归属空缺归总量面单列，不入 bill_line
--   （ADR 决定第 9 条；出账侧实现＝scripts/billing_run.py，报告行单列登记）。
-- 【冻结闸使用侧接管＝本卡遗留（如实）】usage_rollup.frozen_periods() 查询点在
--   meta.billing_period（0003 留位）；本卡不建该对象——tests/test_usage_rollup.py ⑨夹具以
--   「CREATE TABLE meta.billing_period」模拟冻结（他卡域文件），先建同名对象即令其爆红，
--   两态不可兼得；billing 侧冻结（confirmed 后账单不可改）由 scripts/billing_run.py 状态机
--   完整落地，usage 侧（rollup/回填跳过已确认月）随 0023 域批收口（改闸查询点或迁夹具，二选一）。
-- 【生效区间不重叠／首档下界＝0／区间连续】完备约束归登记应用层（0001 先例：不变量走应用层
--   ＋单测，不用触发器；btree_gist 排除约束需扩展安装权限，真实应用角色不具备）——DDL 只锁
--   UNIQUE(service_ref, effective_from) 与区间自身合法性。
-- ============================================================

-- ------------------------------------------------------------
-- 1. usage.price_item 价目主档（ADR-0024 决定第 2 条：价目三模型统一落 price_tier 区间表）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage.price_item (
    item_id        text PRIMARY KEY,
    service_ref    text NOT NULL,
                   -- 计价维度＝服务（可细化至接口，ADR 决定第 2 条【待定】口径；存原值，
                   -- 出账侧按 endpoint 精确匹配——billing_run 头注同源说明）
    model          text NOT NULL
                   CONSTRAINT price_item_model_check
                   CHECK (model IN ('per_call', 'monthly_package', 'tiered')),  -- 按次／套餐／阶梯
    currency       text NOT NULL DEFAULT 'CNY',
    effective_from date NOT NULL,
    effective_to   date,                    -- NULL＝长期有效
    remark         text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT price_item_effective_check
                   CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CONSTRAINT price_item_service_eff_uniq UNIQUE (service_ref, effective_from)
                   -- 「生效日期区间内同服务唯一生效价」的 DDL 侧可落部分；区间互斥归应用层（见头注）
);
COMMENT ON TABLE usage.price_item IS
  '价目主档：计价维度＝服务/接口，三模型（per_call/monthly_package/tiered）统一由 price_tier 区间行表达（ADR-0024 决定第 2 条）；金额数值 维护者 后定（空价目试运行期本表为空）';
COMMENT ON COLUMN usage.price_item.service_ref IS
  '计价维度原值：服务（可细化至接口【待定】）；出账侧与 usage_daily.endpoint 精确匹配（billing_run 同源口径）';

-- ------------------------------------------------------------
-- 2. usage.price_tier 阶梯区间表（区间下界含／上界不含；末档 NULL＝不封顶）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage.price_tier (
    item_id     text NOT NULL REFERENCES usage.price_item (item_id),
    lower_bound bigint NOT NULL DEFAULT 0,   -- 区间下界（含），单位＝计费量（成功次数）
    upper_bound bigint,                      -- 区间上界（不含）；NULL＝末档不封顶
    unit_price  numeric(14,6) NOT NULL DEFAULT 0
                CONSTRAINT price_tier_unit_price_check CHECK (unit_price >= 0),
    CONSTRAINT price_tier_pk PRIMARY KEY (item_id, lower_bound),
    CONSTRAINT price_tier_bounds_check CHECK (upper_bound IS NULL OR upper_bound > lower_bound)
);
COMMENT ON TABLE usage.price_tier IS
  '阶梯区间表：三模型统一落区间行（ADR-0024 决定第 2 条——按次＝单区间；套餐＝额度内 0 价档＋超额档；阶梯＝多档）；首档下界 0 与区间连续性归登记应用层';

-- ------------------------------------------------------------
-- 3. usage.billing_period 账期（自然月＋状态机＋三硬前置留痕＋欠费标记＋OA 登记字段）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage.billing_period (
    period_start   date PRIMARY KEY,         -- 自然月首日（东八区月切，与日切同源；人读形 to_char(period_start,'YYYY-MM')）
    status         text NOT NULL DEFAULT 'draft'
                   CONSTRAINT billing_period_status_check
                   CHECK (status IN ('draft', 'reconciled', 'confirmed', 'written_off')),
                   -- 草稿→已对账→已确认→已核销（ADR 决定第 3 条状态机；核销可选默认不做——
                   -- 本卡链路自检对账内联，落库即 reconciled，draft 保留给后续人工流程）
    gate_t3_closed       boolean NOT NULL DEFAULT false,  -- 三硬前置①：T+3 账期关闭（出账时点核查留痕）
    gate_reconcile_clean boolean NOT NULL DEFAULT false,  -- 三硬前置②：计费面三方对账清零（usage_reconcile 同源判定）
    gate_lag_zero        boolean NOT NULL DEFAULT false,  -- 三硬前置③：rollup 覆盖 lag 归零
    gate_checked_at      timestamptz,
    amount_due     numeric(14,2) NOT NULL DEFAULT 0
                   CONSTRAINT billing_period_amount_due_check CHECK (amount_due >= 0),
                   -- 应收合计＝Σ bill_line.amount WHERE priced（空价目试运行恒 0；真实价目归阶段二）
    overdue        boolean NOT NULL DEFAULT false,
                   -- 欠费逾期标记：只算不限（ADR 决定第 8 条——不做停服/降配/限流；联动点阶段二后另批）
    overdue_marked_at timestamptz,
    oa_ticket      text,
                   -- 确认 OA 单号：纯登记不校真伪（ADR 决定第 6 条零系统集成，ADR-0003 口径）
    confirmed_at   timestamptz,
    reconciled_at  timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_period_confirm_oa_check
                   CHECK (status <> 'confirmed' OR (oa_ticket IS NOT NULL AND oa_ticket <> ''))
                   -- 确认必带 OA 单号（登记留痕，ADR 决定第 6 条）
);
COMMENT ON TABLE usage.billing_period IS
  '账期：自然月＋状态机（draft/reconciled/confirmed/written_off）＋三硬前置核查留痕（ADR-0024 决定第 3 条「写进状态机，不靠人记」）＋欠费只算不限标记（决定第 8 条）＋OA 单号纯登记（决定第 6 条）';
COMMENT ON COLUMN usage.billing_period.overdue IS
  '欠费逾期标记（只算不限）：仅登记展示，平台不做任何限流/停服/降配动作；配额联动启用时点阶段二后另批（ADR-0024 决定第 8 条）';
COMMENT ON COLUMN usage.billing_period.oa_ticket IS
  '确认 OA 单号：线下 OA 审批结果的人工登记位，零系统集成不校真伪（ADR-0024 决定第 6 条；真校验随 OA 接口形态【待核】）';

-- ------------------------------------------------------------
-- 4. usage.bill_line 账单行（四键 PK 防重＋用量/单价双快照；ADR-0024 决定第 4 条 schema 约束）
--    实施不得按月×服务偷懒降粒（ADR 原文）：月聚合以四键为最低粒度，可下钻回 usage_daily
--    （日×主体×服务）再回溯审计 JSONL 分片（ADR-0023 分片保全前提）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage.bill_line (
    period_start date NOT NULL REFERENCES usage.billing_period (period_start),
    tenant_id    text NOT NULL REFERENCES meta.tenant (tenant_id),
                 -- 出账对象＝租户（ADR 决定第 1 条外键落位；账单引 tenant_id 不引显示名）
    caller       text NOT NULL,              -- 凭证级用量进账单行可下钻（决定第 1 条）
    endpoint     text NOT NULL,
    -- 用量快照（usage_daily 日行按四键月聚合的整值快照；决定第 4 条）
    calls              bigint NOT NULL DEFAULT 0 CONSTRAINT bill_line_calls_check CHECK (calls >= 0),
    success            bigint NOT NULL DEFAULT 0 CONSTRAINT bill_line_success_check CHECK (success >= 0),
                       -- 计费量＝成功量 2xx（ADR 决定第 1/3 条：价目与对账以 success 为口径）
    fail               bigint NOT NULL DEFAULT 0 CONSTRAINT bill_line_fail_check CHECK (fail >= 0),
                       -- 失败量入报表不入价（成败分列展示供核议）
    lat_sum            bigint NOT NULL DEFAULT 0 CONSTRAINT bill_line_lat_sum_check CHECK (lat_sum >= 0),
    lat_cnt            bigint NOT NULL DEFAULT 0 CONSTRAINT bill_line_lat_cnt_check CHECK (lat_cnt >= 0),
    source_event_count bigint NOT NULL DEFAULT 0
                       CONSTRAINT bill_line_source_event_count_check CHECK (source_event_count >= 0),
    -- 成交单价快照（出账时点冗余存储，价目表后续改动不改历史账；决定第 2/4 条）
    priced       boolean NOT NULL DEFAULT false,
                 -- false＝空价目试运行：未命中价目，金额 0 元且不计应收（决定第 9 条金额栏空口径，
                 -- 以 priced 标记与「真 0 元价」区分，报表侧可辨）
    price_item_id text REFERENCES usage.price_item (item_id),
    unit_price   numeric(14,6),
                 -- 成交单价快照；多档阶梯＝加权均价 amount/success（success=0 为 NULL）；未定价 NULL
    amount       numeric(14,2) NOT NULL DEFAULT 0 CONSTRAINT bill_line_amount_check CHECK (amount >= 0),
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT bill_line_pk UNIQUE (period_start, tenant_id, caller, endpoint)
                 -- 四键（period,tenant_id,caller,endpoint）＝schema 级防重（ADR 决定第 4 条）
    ,
    CONSTRAINT bill_line_priced_ref_check CHECK (priced = false OR price_item_id IS NOT NULL)
                 -- 有价必引价目（快照可下钻至价目主档）
);
CREATE INDEX IF NOT EXISTS bill_line_tenant_idx ON usage.bill_line (tenant_id, period_start);
CREATE INDEX IF NOT EXISTS bill_line_endpoint_idx ON usage.bill_line (endpoint, period_start);
COMMENT ON TABLE usage.bill_line IS
  '账单行：四键 (period_start,tenant_id,caller,endpoint) PK 防重＋用量快照＋成交单价快照（ADR-0024 决定第 4 条双快照；沙箱/未归属不入本表——决定第 9 条出账前置过滤）';
COMMENT ON COLUMN usage.bill_line.priced IS
  'false＝空价目试运行（未命中价目，amount=0 不计应收）；true＝已按价目计价（历史账不随价目变更追溯）';

-- ------------------------------------------------------------
-- 5. 分域授权（0003 先例：usage_app 读写／usage_ro 只读＋membership 给登录角色；
--    语句幂等——对 0003 已授的既有表重复 GRANT＝no-op，本卡新表由此全覆盖）
-- ------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA usage TO usage_app;
GRANT SELECT ON ALL TABLES IN SCHEMA usage TO usage_ro;
GRANT usage_app TO meta_app;   -- 出账/登记任务（billing_run）通道：meta_app 登录即获计费域读写
GRANT usage_ro  TO meta_ro;    -- 报表/对账只读通道：meta_ro 登录即获计费域只读
