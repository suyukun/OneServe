# Changelog

本文件记录本项目所有值得使用者知晓的变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

本项目尚未发布第一个公开版本，当前处于 `0.x` 阶段：接口与配置项仍可能在不预告的情况下
调整。首次公开发布时确定版本号基线与比较链接（仓库地址在发布时填写）。

## [Unreleased]

### Added

- **一键开箱（one-command）**：`git clone → docker compose up → 打开 localhost:8000`
  三步全功能可用。compose 服务分两档——默认 **demo 档（3 服务）**
  （redis/service/portal），`--profile full` 起 **full 档（6 服务）** 全栈
  （etcd/apisix/batch-worker 随档加入，`:9080` 机器数据面生效）。
- **演示数据自动灌**：service 容器启动前自动 seed（redis 空 → 灌，哨兵键幂等，
  已有数据不重灌）——复用 `scripts/seed_demo.sh`（150 条用户基本信息）与 `sync/`
  真装载链（2000 户用户资产标签：装载→对账→原子切）；门户公告与演示 AppKey
  随栈内置，门户/管理台/第一个查询零手工步骤。
- **demo 档门户降级通道**：门户 nginx 对网关反代内置 502/503/504 降级（直连服务层，
  `/v1/svc/<name>` 自动改写为服务层 `/v1/query/v1-svc-<name>`）；full 档该通道不触发，
  网关 key-auth/限流/网关侧审计语义零变化。
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
- **compose 凭证注入软化（一键开箱批）**：`APISIX_APPKEY`/`APISIX_ADMIN_KEY` 由
  「缺省即报错阻断起栈」改为「缺省注入 `.env.example` 同值演示占位」——零 `.env`
  也能起栈（demo/full 两档均可）；生产部署仍必须 `cp .env.example .env` 改真值。
- **README 快速开始重排（一键开箱批）**：首屏改为三步 Quick start（clone →
  `docker compose up` → 打开 :8000）；原两终端五步手工流程后置为
  「Manual setup (without Docker)」折叠节。
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
| 一键开箱（demo 档默认＋演示数据自动灌） | 已实施 | 三步起栈全功能可用；full 档（6 服务）经 `--profile full`/`up.sh` |
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
