# 测试指南（TESTING）

怎么跑 OneServe 的测试、基线在哪、哪些用例依赖仓外环境。贡献流程（issue/PR/提交规范）
见 [CONTRIBUTING.md](CONTRIBUTING.md)，本文只管「跑测试」这一件事。

## 怎么跑

```bash
# 迭代快速圈：只跑测试套件
python3 -m pytest -q

# 本地全门禁：仓库断言＋测试套件（PR 前必绿，同 CONTRIBUTING §5）
bash scripts/run_gates.sh

# CI 同款三步（ci/gitlab-ci.yml.template · test:pytest job）
pip install -r requirements-dev.txt   # 版本全钉死的固化依赖清单
python3 scripts/gen_openapi.py        # 契约产物幂等前置再生（防「本地绿 CI 红」）
python3 -m pytest tests/ -q           # 全量；CI 另挂 --junitxml 留档
```

依赖一律走 `requirements-dev.txt`，不要按需猜装——缺包不会响亮失败，而是「静默
SKIP 仍报绿」（历史事故：缺 PyJWT → 角色闸断言整批跳过）。

## 环境依赖类用例与 deselect 口径

一部分 DB e2e 套件（`test_billing_api`、`test_admin_api_db_e2e`、`test_meta_writer_db`、
`test_tenants_api`、`test_meta_migrate` 等）依赖 Docker 起一次性 `postgres:17-alpine`
容器灌迁移链。这些用例是 **hermetic 自降级**：`docker` 不可达或镜像未就位时
`pytest.skip`（跳过原因写进输出），**无需手工 deselect**，离线环境照样全套绿。
因此基线数字注明环境口径；要复现满额基线，请备 Docker＋本地 `postgres:17-alpine` 镜像。

## 测试基线

> 1272 passed / 4 skipped / 0 failed（Docker 可用＋本地 `postgres:17-alpine` 镜像就绪
> 口径；测试数随代码滚动，以当日实测为准——合并前由维护者统一跑全量回执）。

## 历史已知失败族（已随历次收尾处置，当前基线全绿）四族（历史归因，自 README 迁移）

开源树**刻意不内置**下面四类仓外产物，依赖它们的断言在缺件环境按族 skip/需先行再生。
当前基线全绿；本表保留用于理解「哪些用例依赖什么」，避免把环境性 skip 误读为故障。

| 族 | 涉及套件 | 依赖什么 |
|---|---|---|
| ① e2e 证据档案 | `test_publish_hbase`、`test_publish_load`、`test_batch_first_run`、`test_m2_6_acceptance` | `tests/evidence/*.txt` 是真实 e2e 跑的输出留档而非夹具——按对应 e2e 脚本再生（部分需全栈 compose / HBase（`compose.hbase.yml`）/ CDP 浏览器） |
| ② 内部治理文档 | `test_m2_6_acceptance` 等 | 内部项目管理工件（AGENTS.md、backlog 任务、内部手册、内部 changelog 提交号）不随开源树分发 |
| ③ 演示凭证打码 | `test_metering`、`test_usage_report_api` | 凭证字面量收编为占位/夹具后的断言对齐（DEMO_KEY 夹具与期望掩码） |
| ④ 授权 e2e 对偶 | `test_admin_api_db_e2e` | 授权用例须指向运行态已发布的服务（而非已下线的 runtime-only 服务） |

## 与贡献流程的关系

PR 门禁与跳过纪律见 [CONTRIBUTING.md](CONTRIBUTING.md) §5——skip 必须在 PR 描述里
给出理由，不允许用 skip 藏失败；架构总览见 [ARCHITECTURE.md](ARCHITECTURE.md)。
