#!/bin/sh
# app.conf 渲染器（密钥出库批 2026-10-20）：beego 不展开 ${VAR}（隔离实证：带引号/裸值
# 两形态均 pq 鉴权失败、pg 侧口令对照正确），故容器启动期用环境值把模板渲染进 tmpfs
# /conf——真值只经 .env→compose environment 注入，不落仓库、不落容器磁盘（tmpfs 即焚）。
# 渲染产物与出库前的 configs/casdoor/app.conf 逐字节一致（行为不变口径）。
# 生产复杂口令路径：.env 直填任意单行真值（含 | & / \ 空格等），渲染前经 esc_for_sed 转义
# （\ | & 三字符；/ 非本脚本分隔符无需转义），无需 base64 中转——原 1b 登记项就此收口（2026-10-21）。
set -eu
# sed 替换串转义：先转义反斜杠，再转义分隔符 | 与替换侧全匹配符 &（顺序不可换）。
esc_for_sed() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/[|&]/\\&/g'; }
: "${CASDOOR_DB_PASSWORD:?CASDOOR_DB_PASSWORD 未设置（应由 .env→compose 注入）}"
: "${CASDOOR_ADMIN_PASSWORD:?CASDOOR_ADMIN_PASSWORD 未设置（应由 .env→compose 注入）}"
# 部署地址（2026-10-20 部署地址批）：未设＝缺省 dev 本机口径，渲染产物与历史硬编码一致。
: "${PROD_PUBLIC_ORIGIN:=http://localhost:8000}"
DB_PW=$(esc_for_sed "$CASDOOR_DB_PASSWORD")
ADMIN_PW=$(esc_for_sed "$CASDOOR_ADMIN_PASSWORD")
ORIGIN=$(esc_for_sed "$PROD_PUBLIC_ORIGIN")
mkdir -p /conf
sed -e "s|\${CASDOOR_DB_PASSWORD}|$DB_PW|g" \
    -e "s|\${CASDOOR_ADMIN_PASSWORD}|$ADMIN_PW|g" \
    -e "s|\${PROD_PUBLIC_ORIGIN}|$ORIGIN|g" \
    /conf-tpl/app.conf.template > /conf/app.conf
chmod 600 /conf/app.conf
