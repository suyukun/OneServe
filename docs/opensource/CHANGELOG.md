# Changelog

本文件记录本项目所有值得使用者知晓的变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

本项目尚未发布第一个公开版本，当前处于 `0.x` 阶段：接口与配置项仍可能在不预告的情况下
调整。首次公开发布时确定版本号基线与比较链接（仓库地址在发布时填写）。

## [Unreleased]

### Added

- **开源物料**：`LICENSE`（Apache-2.0）、`NOTICE`（依赖与镜像许可清单）、
  `CONTRIBUTING.md`、`SECURITY.md`、本 `CHANGELOG.md`。
- **决策记录草稿**：KV 镜像由 Redis 替换为 Valkey（`docs/opensource/ADR-0028-Valkey替换.md`）；
  开源版无登录模式（`docs/opensource/ADR-0029-开源版无登录模式.md`）。

### Changed

- **无登录模式（ADR-0029，维护者 2026-10-28 拍板）**：开源版彻底拆除人机登录——
  打开门户就是首页，管理台公开可访、无任何人机登录/账号概念（开箱即用演示优先）。
  参考栈由 9 容器收口为 6 容器（etcd/apisix/redis/service/batch-worker/portal，
  统一登录三服务与配套卷/探针/路由全数退役）；`.env.example` 与 compose 的登录链
  环境变量（AUTH_PROVIDER/ONESERVE_AUTH_SECRET/ONESERVE_SEED_ADMIN_*/OIDC 及
  统一登录凭证）全部移除。**AppKey 机器凭证鉴权不受影响**：数据面
  `/v1/query|/v1/tags/query|/v1/batch|/proxy` 与凭证面 `/v1/admin/*` 仍按既有
  体系鉴权。登录相关模块文件（auth_oidc/auth_native/auth_view*/users.yaml 等）
  保留不删、无任何路径触发，尸体清理归 v0.1.1。
- **依赖版本口径**：运行与分发版本以 `service/requirements.lock` 为准；
  `service/requirements.txt` 仅表达允许的版本区间。

### Removed

- **行业专有业务域剥离**：开源快照不包含任何行业专有业务域的业务实现、数据模型或
  演示数据。平台的通用机制（键空间隔离、KV 查询执行器、代理转发）保留。
  使用边界见 `NOTICE` 第 7 节。

### Security

- 开源前的敏感信息清理：仓库中不含凭据、密钥、真实手机号与真实业务数据；示例与自检
  脚本使用合成数据。
- 新增依赖或镜像时须同步在 `NOTICE` 声明许可（见 `CONTRIBUTING.md` 第 7 节）。

### 已知里程碑（进行中，不构成发布承诺）

| 里程碑 | 状态 | 说明 |
|---|---|---|
| 无登录模式（ADR-0029，去人机登录） | 已实施 | 登录链路整体拆除（页面/端点/编排/凭证键）；模块尸体清理归 v0.1.1 |
| 开源准备 | 进行中 | 物料已起草；依赖许可逐包机器复核、敏感词自检、镜像许可替换（ADR-0028）待完成 |
| 首次公开发布 | 未开始 | 判据：第三方可独立复现 quickstart |

## [0.1.0] - 待定 (TBD)

首个公开版本的条目在发布时从 `[Unreleased]` 迁移至此，并标注发布日期。

---

## 变更类型说明

- **Added** — 新增能力
- **Changed** — 既有行为的变化
- **Deprecated** — 即将移除、仍可使用
- **Removed** — 已移除
- **Fixed** — 缺陷修复
- **Security** — 安全相关
