-- ============================================================
-- OneServe 元数据库·1b-8 扩展字段 DDL（migrations/0002_meta_ext.sql）
-- 卡-1b7 收口批项2：矩阵 1b-8 扩展登记字段平移（ADR-0021 v1.1 遗留清单②）。
-- 执行角色/方式同 0001：meta_migrate（DDL 迁移角色，仅迁移窗口启用）——
--   docker compose -f compose.metadata.yml -p oneserve-meta exec -T metadata-db \
--     psql -U meta_migrate -d oneserve_meta -v ON_ERROR_STOP=1 -f - < migrations/0002_meta_ext.sql
-- 幂等设计：ADD COLUMN 全 IF NOT EXISTS；重复执行＝no-op（仅 NOTICE，exit 0）。
--
-- 【已核对 0001（不重复补列，任务单核对口径留痕）】
--   meta.tenant.tenant_type  ✔ 0001 已有（三值列 internal|external|sandbox，
--                              tenant_type_check 约束在列）——1b-8 行级 tenant_type 戳
--                              落点即此列，装载平移见 scripts/meta_migrate.py（项2）。
--   meta."grant".oa_ticket   ✔ 0001 已有（授权溯源列）——grant 级单次授权溯源单号
--                              落点即此列（grant 级优先，回落行级/凭证级，装载平移同上）。
-- 【本文件只补缺失列】grant 生效区间（1b-8 授权页「生效区间」登记面；一期只登记展示，
--   grants_admin.DATE_RE YYYY-MM-DD 同源校验＋装载前校验＋date 类型 DDL 三重兜底；
--   NULL＝长期有效＝存量缺省）。
-- ============================================================

ALTER TABLE meta."grant" ADD COLUMN IF NOT EXISTS effective_from date;
ALTER TABLE meta."grant" ADD COLUMN IF NOT EXISTS effective_to   date;

COMMENT ON COLUMN meta."grant".effective_from IS
  '生效区间起（1b-8 授权页登记面；YYYY-MM-DD，NULL＝长期有效）';
COMMENT ON COLUMN meta."grant".effective_to IS
  '生效区间止（1b-8 授权页登记面；YYYY-MM-DD，NULL＝长期有效；from ≤ to 归应用层）';

-- 列权限：表级 GRANT（0001 尾部 GRANT ALL TABLES）对新增列自动生效，无追加授权（同 0001 口径）。
