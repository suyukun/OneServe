-- ============================================================
-- OneServe 元数据库·十表 DDL（migrations/0001_meta_init.sql）
-- 卡-1b7 步3 · 权威依据：ADR-0021 v1.1（唯一权威）＋租户域模型 v1.1 §7（tenant DDL 口径）
-- 执行角色：meta_migrate（DDL 迁移角色，仅迁移窗口启用）；执行方式：
--   docker compose -f compose.metadata.yml -p oneserve-meta exec -T metadata-db \
--     psql -U meta_migrate -d oneserve_meta -v ON_ERROR_STOP=1 -f - < migrations/0001_meta_init.sql
-- 幂等设计：CREATE TABLE / CREATE INDEX 全 IF NOT EXISTS，约束全部内联建表语句
--   （ALTER ... ADD CONSTRAINT 无 IF NOT EXISTS 语义，故不入本文件）；重复执行＝no-op
--   （仅 NOTICE 提示已存在，exit 0——卡-1b7 验证 3）。
--
-- 【psql 直改暗道已封：业务库不授权 postgres 超户直连】
--   实例引导超户仅作 initdb 初始化；业务访问只走三角色（meta_app 读写无 DDL／meta_ro 只读／
--   meta_migrate DDL 迁移）。绕过本迁移文件与 version_history/admin_event 台账的 psql 直改
--   ＝ADR-0021 v1.1 明令封堵（权限＋禁令＋对账门禁三重封堵，详 configs/metadata-db/init/01_roles.sh 头注）。
--
-- 十表计数口径：物理建 9 张＋application 表位登记（只登记不建表——卡红线）＝十表声明全落位。
-- usage schema：座位已建（configs/metadata-db/init/02_schemas.sql）；本文件不建 usage_daily
--   （归 ADR-0023 实施卡——卡-1b7 边界红线），仅落 usage 域角色授权。
-- 跨表不变量（凭证级 grant ⊆ 租户级、每租户 2 把只计 active、写端点状态机迁移）走统一写服务
--   应用层＋单测（卡步4/5/7），不用触发器（ADR-0021 v1.1「不做的事」第 8 条）。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 字段空间 field_space（ADR-0021 v1.1 十表·字段空间行）
--    wide297/news/proxy/usr_basic_info/usr_asset_tag/card_acct_summary/yb 七域规则随迁
--    （schema_drift.FIELD_SPACES 同源）；字典 CSV 本体留文件，表内只登引用位。
--    种子行由迁移工具落（卡-1b7 步4）；登记即扩域，不动 DDL。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.field_space (
    space_name   text PRIMARY KEY,
    drift_mode   text NOT NULL DEFAULT 'not_applicable'
                 CONSTRAINT field_space_drift_mode_check
                 CHECK (drift_mode IN ('enforce', 'not_applicable')),  -- enforce＝参与宽表删列对账（仅 wide297）
    dict_csv_ref text,                                                 -- 字典 CSV 引用位（本体留文件，后续批次入库）
    remark       text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE meta.field_space IS
  '字段空间登记：漂移判定规则随迁＋字典 CSV 引用位（ADR-0021 v1.1；schema_drift.FIELD_SPACES 同源）';

-- ------------------------------------------------------------
-- 2. 租户 tenant（DDL 按租户域模型 v1.1 §7 定稿；ADR-0021 v1.1「租户表定稿」节）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.tenant (
    tenant_id      text PRIMARY KEY
                   CONSTRAINT tenant_id_format_check
                   CHECK (tenant_id ~ '^[a-z0-9]([a-z0-9_-]{0,62}[a-z0-9])?$'),  -- TENANT_ID_RE 同源（appkeys.py:69）
    display_name   text NOT NULL,
    tenant_type    text NOT NULL DEFAULT 'external'
                   CONSTRAINT tenant_type_check
                   CHECK (tenant_type IN ('internal', 'external', 'sandbox')),   -- 三值（租户 v1.1 §4 定稿）
    status         text NOT NULL DEFAULT 'active'
                   CONSTRAINT tenant_status_check
                   CHECK (status IN ('active', 'frozen', 'terminating', 'cancelled')),  -- 四态状态机值域（迁移合法性归应用层）
    org_ref        text,                                                         -- Casdoor 组织对照位；非空则唯一（下方偏索引）
    oa_ticket_open text,                                                         -- 开通 OA 单号；沙箱留空（租户 v1.1 §4）
    remark         text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tenant_oa_ticket_open_check CHECK (
        (tenant_type = 'sandbox' AND (oa_ticket_open IS NULL OR oa_ticket_open = ''))
        OR (tenant_type <> 'sandbox' AND oa_ticket_open IS NOT NULL AND oa_ticket_open <> '')
    )  -- 登记须带开通 OA 单号，沙箱例外（ADR-0003＋租户 v1.1 §4）
);
COMMENT ON TABLE meta.tenant IS
  '租户实体：三值 type×四态 status；双不复用（tenant_id 一经分配永不改写永不复用，软删保外键）——租户 v1.1 §4';
-- org_ref 非空则唯一：防 Casdoor 组织一对多回填（租户 v1.1 §3；ADR-0021 v1.1 租户表定稿）
CREATE UNIQUE INDEX IF NOT EXISTS tenant_org_ref_uniq
  ON meta.tenant (org_ref) WHERE org_ref IS NOT NULL;

-- ------------------------------------------------------------
-- 3. 服务目录 service_catalog（services.yaml 两区合并；ADR-0021 v1.1 十表·服务目录行）
--    origin=runtime|console：运行时区／console_services 写区各行留源区标记；
--    「写区单键覆盖运行时区」合并语义归迁移与快照导出（卡-1b7 步4/步6）。
--    trace.history 不迁本表（version_history 替代＝消灭内嵌 history 脏数据病根）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.service_catalog (
    service_id       text PRIMARY KEY,
    name             text,
    description      text,                 -- yaml desc 字段（DESC 保留字避让）
    route            text NOT NULL,
    method           text NOT NULL DEFAULT 'POST'
                     CONSTRAINT service_catalog_method_check CHECK (method IN ('GET', 'POST')),
    query_key        text,                 -- yaml key 字段（查询键说明；KEY 保留字避让）
    archetype        text,
    field_space      text NOT NULL DEFAULT 'wide297'
                     REFERENCES meta.field_space (space_name),   -- 空间准入＝field_space 表登记
    origin           text NOT NULL
                     CONSTRAINT service_catalog_origin_check CHECK (origin IN ('runtime', 'console')),
    status           text NOT NULL DEFAULT 'offline'
                     CONSTRAINT service_catalog_status_check CHECK (status IN ('draft', 'published', 'offline')),
    backend          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- backend.kv / backend.load 原样平移（嵌套结构）
    params           jsonb NOT NULL DEFAULT '[]'::jsonb,  -- 入参声明原样平移
    fields           jsonb NOT NULL DEFAULT '[]'::jsonb,  -- 字段声明（name/sample/desc/desensitize）原样平移
    response         jsonb,
    guard            jsonb,                               -- 原样平移（卡步3「guard 等字段原样迁入」）
    audit            jsonb,                               -- 原样平移
    scope            jsonb,
    update_frequency text,
    owner            text,
    published_at     timestamptz,
    last_modified_by text,
    last_modified_at timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE meta.service_catalog IS
  '服务目录：services.yaml 两区合并消灭双区双份；status/route/guard/audit 等原样迁入＋origin 源区标记（ADR-0021 v1.1）';

-- ------------------------------------------------------------
-- 4. 上游代理 upstream_proxy（upstreams.yaml 原样平移；ADR-0021 v1.1 十表·上游代理行）
--    网关下发仍走 proxy_hub 幂等收敛（读源＝快照）；trace 不迁（version_history 替代）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.upstream_proxy (
    id               text PRIMARY KEY,
    service_name     text,
    route            text NOT NULL,
    method           text NOT NULL DEFAULT 'POST'
                     CONSTRAINT upstream_proxy_method_check CHECK (method IN ('GET', 'POST')),
    upstream_url     text NOT NULL,
    timeout_sec      integer NOT NULL DEFAULT 5
                     CONSTRAINT upstream_proxy_timeout_check CHECK (timeout_sec > 0),
    retry_count      integer NOT NULL DEFAULT 0
                     CONSTRAINT upstream_proxy_retry_check CHECK (retry_count >= 0),
    reuse_upstream   boolean NOT NULL DEFAULT false,
    upstream_name    text,
    status           text NOT NULL DEFAULT 'draft'
                     CONSTRAINT upstream_proxy_status_check CHECK (status IN ('draft', 'published', 'offline')),
    scope            jsonb,                              -- {env, instance} 原样平移
    last_modified_by text,
    last_modified_at timestamptz,
    published_at     timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE meta.upstream_proxy IS
  '上游代理（B 型 /proxy/*）：upstreams.yaml 结构原样平移（id/route/upstream_url/status/scope 等）';

-- ------------------------------------------------------------
-- 5. 凭证 credential（appkeys.yaml＋auth_matrix 内嵌回落串收编；ADR-0021 v1.1 十表·凭证行）
--    source=appkeys|matrix_fallback（v1.1 增列）；secret 只存 sha256 摘要（CHECK 钉格式红线）；
--    软删禁物理删（deleted_at）；每租户 2 把只计 active＝写服务应用层校验（卡步5，非 DB 约束）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.credential (
    appkey             text PRIMARY KEY,          -- 凭证串不复用：主键唯一＋永不改写（租户 v1.1 §4）
    secret_hash        text NOT NULL
                       CONSTRAINT credential_secret_hash_check
                       CHECK (secret_hash ~ '^sha256:[0-9a-f]{64}$'),  -- 明文永不落盘，只存摘要
    tenant_id          text NOT NULL REFERENCES meta.tenant (tenant_id),
    caller_name        text,
    app_name           text,
    key_type           text NOT NULL DEFAULT 'sandbox'
                       CONSTRAINT credential_key_type_check CHECK (key_type IN ('sandbox', 'formal')),
    source             text NOT NULL DEFAULT 'appkeys'
                       CONSTRAINT credential_source_check CHECK (source IN ('appkeys', 'matrix_fallback')),
    status             text NOT NULL DEFAULT 'active'
                       CONSTRAINT credential_status_check
                       CHECK (status IN ('active', 'disabled', 'revoked')),  -- revoked＝矩阵收编条目语义（appkeys 侧 active|disabled）
    oa_ticket          text,
    expires_at         text
                       CONSTRAINT credential_expires_at_check
                       CHECK (expires_at IS NULL OR expires_at = 'permanent'
                              OR expires_at ~ '^\d{4}-\d{2}-\d{2}$'),      -- 『permanent』字面量口径（appkeys.py B.1）
    quota_qps          integer NOT NULL DEFAULT 0
                       CONSTRAINT credential_quota_qps_check CHECK (quota_qps >= 0),
    quota_daily        integer NOT NULL DEFAULT 0
                       CONSTRAINT credential_quota_daily_check CHECK (quota_daily >= 0),
    tag_sets           jsonb NOT NULL DEFAULT '[]'::jsonb,
    issued_at          timestamptz NOT NULL DEFAULT now(),
    rotated_at         timestamptz,
    overlap_expires_at timestamptz,
    superseded_by      text,
    rotation_of        text,                      -- 轮换链旧键串；悬空容忍归应用层（存量数据不 FGK 硬绑）
    deleted_at         timestamptz,               -- 软删标记（禁物理删：保外键与归属解析链，租户 v1.1 §6）
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT credential_formal_oa_check
      CHECK (key_type <> 'formal' OR (oa_ticket IS NOT NULL AND oa_ticket <> ''))  -- formal 必带 OA 单号（ADR-0003）
);
CREATE INDEX IF NOT EXISTS credential_tenant_id_idx ON meta.credential (tenant_id);
CREATE INDEX IF NOT EXISTS credential_status_idx    ON meta.credential (status);
COMMENT ON TABLE meta.credential IS
  '调用凭证：单表单状态收编 appkeys＋矩阵内嵌回落串；secret 只存摘要；软删禁物理删；双不复用之 appkey 侧';

-- ------------------------------------------------------------
-- 6. 授权关系 grant（auth_matrix.yaml＋appkeys.grants 两粒度统一；ADR-0021 v1.1 十表·授权关系行）
--    表名 grant＝SQL 保留字，恒加双引号引用。
--    scope=tenant（矩阵语义·OA 权威）／credential（签发语义，必须 ⊆ 租户级——子集校验走
--    应用层，1b 内三入口；租户 v1.1 §3）。代理授权＝整路由语义：fields 空集仅 kind=proxy 合法。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta."grant" (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id         text NOT NULL REFERENCES meta.tenant (tenant_id),
    scope             text NOT NULL DEFAULT 'tenant'
                      CONSTRAINT grant_scope_check CHECK (scope IN ('tenant', 'credential')),
    credential_appkey text REFERENCES meta.credential (appkey),   -- 租户级恒空／凭证级必填（下方配对 CHECK）
    target_kind       text NOT NULL
                      CONSTRAINT grant_target_kind_check CHECK (target_kind IN ('service', 'proxy')),
    service_id        text REFERENCES meta.service_catalog (service_id),
    proxy_id          text REFERENCES meta.upstream_proxy (id),
    fields            text[] NOT NULL DEFAULT '{}',
    endpoint          text,                                       -- 计量映射（可选，平移自两 yaml）
    oa_ticket         text,                                       -- 授权溯源（租户级登记单号）
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT grant_scope_pairing_check CHECK (
        (scope = 'tenant' AND credential_appkey IS NULL)
        OR (scope = 'credential' AND credential_appkey IS NOT NULL)
    ),
    CONSTRAINT grant_target_pairing_check CHECK (
        (target_kind = 'service' AND service_id IS NOT NULL AND proxy_id IS NULL)
        OR (target_kind = 'proxy' AND proxy_id IS NOT NULL AND service_id IS NULL)
    ),
    CONSTRAINT grant_fields_nonempty_check
      CHECK (target_kind = 'proxy' OR cardinality(fields) > 0)      -- fields 空集仅 kind=proxy 合法
);
CREATE INDEX IF NOT EXISTS grant_tenant_id_idx     ON meta."grant" (tenant_id);
CREATE INDEX IF NOT EXISTS grant_credential_idx    ON meta."grant" (credential_appkey);
-- 同租户×同服务唯一（矩阵规则）／凭证级同键×同服务唯一：partial unique＋COALESCE 归一 NULL
CREATE UNIQUE INDEX IF NOT EXISTS grant_tenant_level_uniq ON meta."grant"
  (tenant_id, target_kind, COALESCE(service_id, ''), COALESCE(proxy_id, ''), COALESCE(endpoint, ''))
  WHERE scope = 'tenant';
CREATE UNIQUE INDEX IF NOT EXISTS grant_credential_level_uniq ON meta."grant"
  (credential_appkey, target_kind, COALESCE(service_id, ''), COALESCE(proxy_id, ''), COALESCE(endpoint, ''))
  WHERE scope = 'credential';
COMMENT ON TABLE meta."grant" IS
  '授权关系两粒度统一：租户级（矩阵/OA 权威）＋凭证级（签发语义⊆租户级）；代理授权整路由语义（ADR-0021 v1.1）';

-- ------------------------------------------------------------
-- 7. 管理事件 admin_event（窄表·v1.1 定稿；替代 v1 audit_log——调用流水永不入库）
--    写侧动作台账（写端点事务内同发）；与 version_history 以 event_id 对偶。
--    ADR-0017 管线仍是全量审计真源；对账＝admin_event 行数↔管线管理动作条数日对（run_gates）。
--    ADR 字段名 at→occurred_at（at＝SQL 关键字避让，映射登记）；result 值域待与审计八元组
--    对齐，防发明枚举暂不加 CHECK。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.admin_event (
    event_id    text PRIMARY KEY,
    operator    text NOT NULL,
    action      text NOT NULL,
    target      text,
    result      text NOT NULL,
    oa_ticket   text,
    tenant_id   text REFERENCES meta.tenant (tenant_id),   -- 管理动作涉租户时非空（ADR-0021 v1.1 衔接确认节）
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS admin_event_occurred_at_idx ON meta.admin_event (occurred_at);  -- 日对账 T-1 窗
CREATE INDEX IF NOT EXISTS admin_event_tenant_id_idx   ON meta.admin_event (tenant_id);
COMMENT ON TABLE meta.admin_event IS
  '管理事件窄表：管理动作与配置变更台账，与 version_history 以 event_id 对偶（ADR-0021 v1.1 定稿说明节）';

-- ------------------------------------------------------------
-- 8. 版本历史 version_history（trace.history/trace 占位/git 留痕的等价替代；ADR-0021 v1.1）
--    每实体每次成功变更一行（before/after delta）；全量保留不设 TTL（≥3 年对齐审计拍板）；
--    operator 绑 OIDC subject（「点按钮的人」）。BEFORE 保留字避让：列名 delta_before/delta_after。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.version_history (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity      text NOT NULL
                CONSTRAINT version_history_entity_check
                CHECK (entity IN ('service_catalog', 'upstream_proxy', 'credential', 'grant',
                                  'tenant', 'field_space', 'tenant_contact')),
    entity_id   text NOT NULL,
    delta_before jsonb,                                    -- ADR 记法 before（BEFORE 保留字避让）
    delta_after  jsonb,                                    -- ADR 记法 after
    operator    text NOT NULL,
    source      text NOT NULL DEFAULT 'console',           -- 写入通道（console|auth_admin|migration|system）
    event_id    text NOT NULL REFERENCES meta.admin_event (event_id),  -- 动作轨对偶（FK 钉因果链）
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS version_history_entity_idx     ON meta.version_history (entity, entity_id, occurred_at);
CREATE INDEX IF NOT EXISTS version_history_occurred_at_idx ON meta.version_history (occurred_at);
COMMENT ON TABLE meta.version_history IS
  '版本历史：每实体每次成功变更一行（delta）；全量保留不设 TTL；git 留痕/回滚的库级等价替代（ADR-0021 v1.1）';

-- ------------------------------------------------------------
-- 9. 租户联系人 tenant_contact（租户 v1.1 §7：独立表已定；1b-10 变更通知预留挂点）
--    联系人制不进登录（已拍板）；Casdoor 映射在 tenant.org_ref，本表不设。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.tenant_contact (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id    text NOT NULL REFERENCES meta.tenant (tenant_id),
    contact_role text NOT NULL
                 CONSTRAINT tenant_contact_role_check
                 CHECK (contact_role IN ('business', 'technical', 'tenant_admin')),  -- 商务/技术/租户管理员
    contact_name text NOT NULL,
    contact_info text NOT NULL,
    remark       text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tenant_contact_uniq UNIQUE (tenant_id, contact_role, contact_name)
);
COMMENT ON TABLE meta.tenant_contact IS
  '租户联系人：角色（商务/技术/租户管理员）＋联系方式；环 9 变更通知预留挂点（租户 v1.1 §7）';

-- ------------------------------------------------------------
-- 10. application 表位（只登记不建表——卡-1b7 边界红线「application 只登记表位不建表」）
--     ADR-0021 v1.1 十表声明第 10 张：OA 对账与申请进度查询面（租户 v1.1 §3/§7）。
--     预留字段面：单号 / 类型 / 租户（FK meta.tenant）/ 登记结果快照 / 登记时间。
--     建表时点随 OA 接口形态【待核】；表位先行登记不夹带。
--     十表计数口径：本文件物理建 9 张＋本表位登记＝十表声明全落位。
-- ------------------------------------------------------------

-- ------------------------------------------------------------
-- usage 域角色授权（分域座位收口；usage_daily 归 ADR-0023 实施卡——本卡不建该表，故无 day 类索引）
-- ------------------------------------------------------------
GRANT USAGE ON SCHEMA usage TO usage_app, usage_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE meta_migrate IN SCHEMA usage
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO usage_app;
ALTER DEFAULT PRIVILEGES FOR ROLE meta_migrate IN SCHEMA usage
  GRANT SELECT ON TABLES TO usage_ro;

-- ------------------------------------------------------------
-- 业务表授权幂等兜底（默认权限 initdb 侧已设；对已存在表/序列补全量 GRANT，重复执行安全）
-- ------------------------------------------------------------
GRANT USAGE ON SCHEMA meta TO meta_app, meta_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA meta TO meta_app;
GRANT SELECT ON ALL TABLES IN SCHEMA meta TO meta_ro;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA meta TO meta_app;
ALTER DEFAULT PRIVILEGES FOR ROLE meta_migrate IN SCHEMA meta
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO meta_app;
ALTER DEFAULT PRIVILEGES FOR ROLE meta_migrate IN SCHEMA meta
  GRANT SELECT ON TABLES TO meta_ro;
