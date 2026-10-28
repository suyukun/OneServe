-- ============================================================
-- 元数据库 schema 分域（卡-1b7 步2 · ADR-0021 v1.1「同实例分域：schema meta＋schema usage」）
-- 运行时机：docker-entrypoint-initdb.d（超户上下文，仅空卷首启执行一次）。
--   meta  ：业务元数据域——十表落此（migrations/0001_meta_init.sql，以 meta_migrate 执行）
--   usage ：计量落账域座位——本卡只建 schema 不建表；usage_daily 归 ADR-0023 实施卡
-- 属主：meta_migrate（DDL 迁移角色＝两 schema 属主，迁移窗口可直接 CREATE/ALTER）。
-- 默认权限：meta_migrate 未来建表自动授权给分域读写/只读角色（免每表手工 GRANT）；
--   已存在表的全量 GRANT 幂等兜底放 migrations/0001_meta_init.sql（meta_migrate 自身执行）。
-- 【psql 直改暗道已封：业务库不授权 postgres 超户直连】——口径同 01_roles.sh 头注。
-- ============================================================

CREATE SCHEMA IF NOT EXISTS meta  AUTHORIZATION meta_migrate;
CREATE SCHEMA IF NOT EXISTS usage AUTHORIZATION meta_migrate;

COMMENT ON SCHEMA meta  IS 'OneServe 业务元数据域（ADR-0021 v1.1 十表；卡-1b7）';
COMMENT ON SCHEMA usage IS 'OneServe 计量落账域座位：只建 schema 不建表，usage_daily 归 ADR-0023 实施卡（卡-1b7 边界红线）';

-- schema 使用权（业务角色见 meta；usage 分域角色见 usage）
GRANT USAGE ON SCHEMA meta  TO meta_app, meta_ro;
GRANT USAGE ON SCHEMA usage TO usage_app, usage_ro;

-- 默认权限（超户代设 FOR ROLE meta_migrate，管住 migrate 未来建的表/序列）
ALTER DEFAULT PRIVILEGES FOR ROLE meta_migrate IN SCHEMA meta
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO meta_app;
ALTER DEFAULT PRIVILEGES FOR ROLE meta_migrate IN SCHEMA meta
  GRANT SELECT ON TABLES TO meta_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE meta_migrate IN SCHEMA usage
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO usage_app;
ALTER DEFAULT PRIVILEGES FOR ROLE meta_migrate IN SCHEMA usage
  GRANT SELECT ON TABLES TO usage_ro;
