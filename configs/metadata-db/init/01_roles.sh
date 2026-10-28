#!/bin/sh
# ============================================================
# 元数据库角色初始化（卡-1b7 步2 · ADR-0021 v1.1「DB 权限三角色」）
# 运行时机：docker-entrypoint-initdb.d——仅空卷首启执行一次（postgres 官方镜像语义）。
#
# 【psql 直改暗道已封：业务库不授权 postgres 超户直连】
#   实例引导超户（POSTGRES_USER）仅作 initdb 初始化，不作为业务/运维日常直连账号；
#   业务访问一律走下列角色，无万能键——绕过 version_history/admin_event 的 psql 直改
#   ＝ADR-0021 v1.1 明令封堵（权限＋禁令＋对账门禁三重封堵）：
#     meta_app      登录·业务表读写·无 DDL（NOSUPERUSER/NOCREATEDB/NOCREATEROLE）
#     meta_ro       登录·只读（对账、报表）
#     meta_migrate  登录·DDL 迁移（meta/usage 两 schema 属主＋库级 CREATE），仅迁移窗口启用
#   分域角色（对齐计费 v1.1 §3.3；NOLOGIN 权限角色，口令是否本卡发＝卡-1b7 待核#2，
#   归 ADR-0023 实施卡裁定时 GRANT 给登录角色并按需改 LOGIN）：
#     usage_app / usage_ro  usage schema 读写／只读
# 口令注入：META_APP_PASSWORD / META_RO_PASSWORD / META_MIGRATE_PASSWORD 三变量 compose
#   必填（:? 缺失即拒）；经 psql -v ＋ :'var' 安全引号落库，口令明文不进本文件不进仓。
# ============================================================
set -eu

: "${POSTGRES_USER:?POSTGRES_USER 未设置}"
: "${POSTGRES_DB:?POSTGRES_DB 未设置}"
: "${META_APP_PASSWORD:?META_APP_PASSWORD 未设置——cp .env.example .env 后填真值（凭证不进代码仓）}"
: "${META_RO_PASSWORD:?META_RO_PASSWORD 未设置——cp .env.example .env 后填真值（凭证不进代码仓）}"
: "${META_MIGRATE_PASSWORD:?META_MIGRATE_PASSWORD 未设置——cp .env.example .env 后填真值（凭证不进代码仓）}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_pw="$META_APP_PASSWORD" \
  -v ro_pw="$META_RO_PASSWORD" \
  -v mig_pw="$META_MIGRATE_PASSWORD" \
  -v db_name="$POSTGRES_DB" \
<<'EOSQL'
-- 三角色（无万能键：均 NOSUPERUSER/NOCREATEDB/NOCREATEROLE，谁也造不出新超户/新库）
CREATE ROLE meta_app     LOGIN PASSWORD :'app_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE meta_ro      LOGIN PASSWORD :'ro_pw'  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE meta_migrate LOGIN PASSWORD :'mig_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

-- usage 分域角色（NOLOGIN 权限角色；ADR-0023 实施卡接入时 GRANT 给登录角色，口令裁定＝卡-1b7 待核#2）
CREATE ROLE usage_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE usage_ro  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

-- 连接权显式授予（不依赖 public 缺省，后续收紧留余地）
GRANT CONNECT ON DATABASE :"db_name" TO meta_app, meta_ro, meta_migrate;

-- usage 分域角色 ADMIN（migrations/0003/0004 的 membership GRANT 授予通道：
--   meta_migrate 无 CREATEROLE，缺此授权则迁移内 GRANT usage_app TO meta_app 必炸
--   permission denied——e2e 以超户起库测不出，演练/生产三角色形态实爆；2026-10-21 微批根修）
GRANT usage_app, usage_ro TO meta_migrate WITH ADMIN OPTION;
EOSQL
