# OneServe

[English](README.en.md) ｜ 中文（本文件）

[![CI](https://github.com/suyukun/OneServe/actions/workflows/ci.yml/badge.svg)](https://github.com/suyukun/OneServe/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](docs/opensource/LICENSE)
[![Python](https://img.shields.io/badge/Python-3.13-blue.svg)](service/Dockerfile)

**OneServe 数据API网关（OneServe Data API Gateway）**：自托管、网关前置的数据服务平台——把白名单里的数据字段，几分钟发布成一个查询 API；调用方按 AppKey 授权，每一次调用都留痕可审计。

<details>
<summary>English</summary>

**OneServe — a self-hosted, gateway-fronted data service platform: publish a whitelisted data field as a query API in minutes, govern every caller with AppKeys, and audit every call.**

</details>

## 这是什么

OneServe 位于你的数据仓库副本（已加载进 Redis 的版本化 KV）和内外部调用方之间。中间是一层轻量 FastAPI 服务，在 **APISIX 网关**后面提供 **Type-A KV 点查**（以及标签式批量查询），网关用 AppKey 认证调用方；每个租户有一份**字段白名单**（授权矩阵 + 控制台下发的凭证注册表），决定这个调用方能看哪些字段。围绕这个核心还有一圈配套：Portal 门户 + Admin 管理台，管服务发布、授权管理和凭证生命周期（沙箱试用 key、带 OA 工单的正式 key、带重叠期的轮换）；审计管道（JSONL / Kafka）加用量计量与报表；可选的 PostgreSQL「元数据库」把 YAML 注册表镜像一份，配迁移/对账工具；再加一个跑异步批量交付的 batch worker。演示数据和凭证都内置，几分钟就能跑通第一次端到端查询。

<details>
<summary>English</summary>

OneServe sits between your data warehouse copies (loaded into Redis as versioned KV) and external/internal callers. A thin FastAPI service exposes **type-A KV point queries** (and tag-style batch lookups) behind an **APISIX gateway** that authenticates callers by AppKey; a per-tenant **field whitelist** (auth matrix + console-issued credential registry) decides which fields each caller may see. Around that core: a portal + admin console for service publishing, grant management and credential lifecycle (sandbox trial keys, formal keys with OA ticket, rotation with overlap), an audit pipeline (JSONL / Kafka) with usage metering and reports, an optional PostgreSQL "meta database" twin of the YAML registries with migration/reconcile tooling, and a batch worker for asynchronous bulk delivery. Demo data and credentials are built in, so you can run a first end-to-end query in minutes.

</details>

## 一条命令跑起来

前置条件：Docker（带 Compose v2），别的什么都不用装。

```bash
git clone https://github.com/suyukun/OneServe.git && cd OneServe
docker compose up -d
open http://localhost:8000
```

> **国内网络提示**：Docker Hub 直连拉取可能超时——请先配置镜像加速（如 `docker.m.daocloud.io`），或手动拉取 compose 引用的镜像后再起栈。

就这么多。首次运行会构建服务镜像（约 1-2 分钟；之后再启动只要几秒）。页面打开后，下面这些全部零手工步骤、无需登录即可用（无登录模式，见 [ADR-0029](docs/opensource/ADR-0029-开源版无登录模式.md)）：

- **Portal 门户**：http://localhost:8000 —— 首页 KPI、服务目录、API 详情、接口调试 (Trial)、文档、公告。
- **Admin 管理台**：http://localhost:8000/admin/ —— 发布、appkeys、授权、租户、监控、压测、版本、计费、上游，公开、免登录。
- **演示数据自动播种** —— 服务容器启动前，只要发现演示哨兵键缺失，就先播种 Redis（幂等：已有数据绝不重播）：150 条 `usr_basic_info` 记录，供第一次查询用；一份 2,000 用户的 `usr_asset_tag` 版本，走真实同步链路（load → reconcile → switch）推上来；再加几条门户公告。
- **演示 AppKey 拿来就能 curl** —— 第一条查询走门户在 :8000 的同源反向代理（不用多开端口）：

```bash
curl -s -X POST http://localhost:8000/v1/query/v1-svc-usr-basic-info \
  -H 'apikey: uat-c49d18ab72e03f55' \
  -H 'Content-Type: application/json' \
  -d '{"key":"demo-partner-0001"}' | jq .
```

预期结果：HTTP 200，`found: true`，12 个白名单字段（`gender_cd`、`age_band`、`city_code`、…）。同一条查询也可以在门户的**接口调试 (Trial)**页发：选中 `用户基本信息查询`，粘上 AppKey 和 key。

> 上面这个 `apikey`，是 **[`service/auth_matrix.yaml`](service/auth_matrix.yaml) 里 `partner-demo` 租户声明的演示凭证**——仓库内置的演示值，不是生产密钥。机器调用方一律用 AppKey 认证（服务端对照授权矩阵/凭证注册表校验；full 档下在网关校验）。

内置演示凭证（全部是仓库内置的演示值）：

| 租户 | AppKey | 试试 |
|---|---|---|
| `partner-demo` | `uat-c49d18ab72e03f55` | `v1-svc-usr-basic-info`，key 用 `demo-partner-0001`（即上面的 curl） |
| `demo_merchant` | `uat-7f3a9c21e5b84d06` | 资产标签类服务（SM3 查询键） |
| `demo-1020` | `uat-58b0d2e7a9c14613` | `v1-svc-usr-asset-tag1020` |

<details>
<summary>English</summary>

## Quick start (one command)

Prerequisites: Docker (with Compose v2) — nothing else.

```bash
git clone https://github.com/suyukun/OneServe.git && cd OneServe
docker compose up -d
open http://localhost:8000
```

> **Mainland China network note**: Docker Hub pulls may time out — configure a registry mirror (e.g. `docker.m.daocloud.io`) first, or pull the images referenced by compose manually before `up`.

That is the whole setup. The first run builds the service image (~1-2 minutes; later starts take seconds). When the page opens, **everything below works with zero manual steps and no login** (no-login mode, [ADR-0029](docs/opensource/ADR-0029-开源版无登录模式.md)):

- **Portal** at http://localhost:8000 — home KPIs, service catalog, API details, trial console (接口调试), docs, announcements.
- **Admin console** at http://localhost:8000/admin/ — publish, appkeys, grants, tenants, monitor, load, versions, billing, upstreams: public, no login.
- **Demo data auto-seeded** — the service container seeds Redis before boot whenever the demo sentinels are missing (idempotent: existing data is never re-seeded): 150 `usr_basic_info` records for the first query, a 2,000-user `usr_asset_tag` version pushed through the real sync chain (load → reconcile → switch), and portal announcements.
- **Demo AppKey ready to curl** — your first query, through the portal's same-origin reverse proxy on :8000 (no extra ports needed):

```bash
curl -s -X POST http://localhost:8000/v1/query/v1-svc-usr-basic-info \
  -H 'apikey: uat-c49d18ab72e03f55' \
  -H 'Content-Type: application/json' \
  -d '{"key":"demo-partner-0001"}' | jq .
```

Expected: HTTP 200 with `found: true` and 12 whitelisted fields (`gender_cd`, `age_band`, `city_code`, ...). The same query works from the portal's **接口调试 (Trial)** page: pick `用户基本信息查询`, paste the AppKey and the key.

> The `apikey` above is the **demo credential of the `partner-demo` tenant declared in [`service/auth_matrix.yaml`](service/auth_matrix.yaml)** — a committed demo value, not a production key. Machine callers always authenticate with an AppKey (checked by the service against the auth matrix / credential registry; at the gateway in the full profile).

Built-in demo credentials (all committed demo values):

| Tenant | AppKey | Try it with |
|---|---|---|
| `partner-demo` | `uat-c49d18ab72e03f55` | `v1-svc-usr-basic-info`, key `demo-partner-0001` (the curl above) |
| `demo_merchant` | `uat-7f3a9c21e5b84d06` | asset-tag services (SM3 query keys) |
| `demo-1020` | `uat-58b0d2e7a9c14613` | `v1-svc-usr-asset-tag1020` |

</details>

### 两种档位：demo（默认）与 full

| | `docker compose up` —— demo 档（默认） | `docker compose --profile full up`（= `bash scripts/up.sh`） |
|---|---|---|
| 服务数 | **3**：`redis` + `service` + `portal` | **6**：另加 `etcd` + `apisix` + `batch-worker` |
| Portal + Admin 管理台 | ✅ 全功能，http://localhost:8000 | ✅ 相同 |
| 演示数据 | ✅ 自动播种 | ✅ 自动播种（同一组哨兵键） |
| 机器数据面 :9080（网关 AppKey 通道：`/v1/query/*`、`/v1/tags/query`、`/v1/batch/*`、`/proxy/*`） | ❌ 本档没有——门户发起的查询自动回落到服务层，`/healthz` 也如实把 `gateway`/`etcd` 报成 `degraded` | ✅ APISIX key-auth + 限流 + 网关侧审计 |
| 异步批量交付 worker | ❌ 本档没有 | ✅ |

> **查询通道说明**：演示 AppKey 在默认 demo 档经门户同源反代直通服务层，开箱即查；`full` 档数据面查询（`:9080`）经 APISIX 网关 key-auth——需先经网关 Admin API 为该凭证注册 consumer（经管理台签发/启用凭证会自动同步；演示 consumer 的开箱自动注册在路线图中）。

停掉：demo 档用 `docker compose down`，全量用 `docker compose --profile full down`；加 `-v` 连数据卷一起删。

<details>
<summary>English</summary>

### Two profiles: demo (default) vs full

| | `docker compose up` — demo, default | `docker compose --profile full up` (= `bash scripts/up.sh`) |
|---|---|---|
| Services | **3**: `redis` + `service` + `portal` | **6**: + `etcd` + `apisix` + `batch-worker` |
| Portal + admin console | ✅ full feature set on http://localhost:8000 | ✅ same |
| Demo data | ✅ auto-seeded | ✅ auto-seeded (same sentinels) |
| Machine data plane :9080 (gateway AppKey plane: `/v1/query/*`, `/v1/tags/query`, `/v1/batch/*`, `/proxy/*`) | ❌ not in this profile — portal-origin queries fall back to the service layer automatically, and `/healthz` reports `gateway`/`etcd` as `degraded` honestly | ✅ APISIX key-auth + rate limit + gateway-side audit |
| Async batch delivery worker | ❌ not in this profile | ✅ |

> **Query-path note**: the demo AppKey works out of the box in the default demo profile (portal same-origin proxy); in the `full` profile, data-plane queries go through APISIX key-auth, so a consumer must be registered for the credential via the gateway Admin API first (issuing/enabling a credential in the admin console syncs it automatically; out-of-the-box auto-registration of the demo consumer is on the roadmap).

Tear down with `docker compose down` (demo) or `docker compose --profile full down` (everything); add `-v` to also drop data volumes.

</details>

## 完整栈（Docker Compose `--profile full`，6 个服务）

```bash
bash scripts/up.sh          # cp .env.example .env (first run) -> docker compose --profile full up -d --build -> wait healthy -> init APISIX routes
```

6 个容器全部拉起：`etcd`、`redis`、`apisix`、`service`、`batch-worker`、`portal`（上面的 demo 档只跑后三个——网关侧组件按需开）。启动后的入口：

| 入口 | URL | 说明 |
|---|---|---|
| Portal + Admin 管理台 | http://localhost:8000 | 公开、免登录（ADR-0029）：打开门户就是首页，管理台直接可用 |
| 网关（机器数据面） | http://localhost:9080 | `/v1/query/*`、`/v1/tags/query`、`/v1/batch/*`、`/proxy/*` —— AppKey 认证，无登录 |

<details>
<summary><b>手动搭建（不用 Docker）</b>——旧版双终端快速上手，适合对着本地 Redis 开发</summary>

前置条件：Python 3.13+、`curl`、`jq`，外加一个 Redis。

**终端 1 —— 起最小栈**（Redis 发布在 `:6379`，服务在 `:8000`；保持运行）：

```bash
# 1) Redis in port mode: no --name, so no container-name collisions.
#    Already have a Redis listening on 6379? Skip this step.
docker run -d -p 6379:6379 valkey/valkey:8-alpine

# 2) venv + the service (the venv also provides the redis-py driver used by the seed script)
python3 -m venv .venv && . .venv/bin/activate && pip install -r service/requirements.txt
cd service && REDIS_URL=redis://localhost:6379/0 AUDIT_DIR=/tmp/oneserve-audit uvicorn app.main:app --port 8000
```

**终端 2 —— 播种演示数据 + 第一次查询**（另开一个终端，在仓库根目录）：

```bash
# 3) seed demo data (150 records x 12 whitelisted fields, loads in seconds;
#    auto-detects Redis: redis-py direct connection first, container/cli as fallback)
bash scripts/seed_demo.sh

# 4) your first query
curl -s -X POST http://localhost:8000/v1/query/v1-svc-usr-basic-info \
  -H 'apikey: uat-c49d18ab72e03f55' \
  -H 'Content-Type: application/json' \
  -d '{"key":"demo-partner-0001"}' | jq .
```

预期结果：HTTP 200，`found: true`，12 个白名单字段。

顺手的还有：`bash scripts/seed_demo_selftest.sh`，用构建好的服务镜像把同一套流程（隔离端口、一次性容器）跑成端到端自测。

</details>

**基础镜像说明（生产构建）：** dev compose 用 [`service/Dockerfile`](service/Dockerfile) 基于官方 `python:3.13-slim` 构建 `service`——不用预构建。而 **prod** 镜像 [`Dockerfile.prod`](Dockerfile.prod) 则建在本地预构建的加固基础镜像之上：

```bash
docker build -f docker/python-base.Dockerfile -t oneserve-python-base:3.13-slim .
docker buildx build --platform linux/arm64 -f Dockerfile.prod -t oneserve/service:prod .
```

<details>
<summary>English</summary>

## Full stack (Docker Compose `--profile full`, 6 services)

```bash
bash scripts/up.sh          # cp .env.example .env (first run) -> docker compose --profile full up -d --build -> wait healthy -> init APISIX routes
```

Brings up all 6 containers: `etcd`, `redis`, `apisix`, `service`, `batch-worker`, `portal` (the demo profile above runs the last three only — the gateway-side components are opt-in). Entrypoints after startup:

| Entry | URL | Notes |
|---|---|---|
| Portal + admin console | http://localhost:8000 | Public, no login (ADR-0029): open the portal and you are on the home page; the admin console is directly usable |
| Gateway (machine data plane) | http://localhost:9080 | `/v1/query/*`, `/v1/tags/query`, `/v1/batch/*`, `/proxy/*` — AppKey self-auth, no login |

**Manual setup (without Docker)** — the old two-terminal quickstart, for developing against a local Redis

Prerequisites: Python 3.13+, `curl`, `jq`, and a Redis.

**Terminal 1 — start the minimal stack** (Redis published on `:6379`, the service on `:8000`; keep it running):

```bash
# 1) Redis in port mode: no --name, so no container-name collisions.
#    Already have a Redis listening on 6379? Skip this step.
docker run -d -p 6379:6379 valkey/valkey:8-alpine

# 2) venv + the service (the venv also provides the redis-py driver used by the seed script)
python3 -m venv .venv && . .venv/bin/activate && pip install -r service/requirements.txt
cd service && REDIS_URL=redis://localhost:6379/0 AUDIT_DIR=/tmp/oneserve-audit uvicorn app.main:app --port 8000
```

**Terminal 2 — seed demo data + your first query** (a new terminal, from the repo root):

```bash
# 3) seed demo data (150 records x 12 whitelisted fields, loads in seconds;
#    auto-detects Redis: redis-py direct connection first, container/cli as fallback)
bash scripts/seed_demo.sh

# 4) your first query
curl -s -X POST http://localhost:8000/v1/query/v1-svc-usr-basic-info \
  -H 'apikey: uat-c49d18ab72e03f55' \
  -H 'Content-Type: application/json' \
  -d '{"key":"demo-partner-0001"}' | jq .
```

Expected: HTTP 200 with `found: true` and 12 whitelisted fields.

Also handy: `bash scripts/seed_demo_selftest.sh` runs the same flow (isolated ports, throwaway containers) as an end-to-end self-test using the built service image.

**Base image note (production build):** the dev compose builds `service` from [`service/Dockerfile`](service/Dockerfile) on the official `python:3.13-slim` — nothing to pre-build. The **prod** image [`Dockerfile.prod`](Dockerfile.prod) however builds on a locally pre-built hardened base:

```bash
docker build -f docker/python-base.Dockerfile -t oneserve-python-base:3.13-slim .
docker buildx build --platform linux/arm64 -f Dockerfile.prod -t oneserve/service:prod .
```

</details>

## 功能一览

| 功能 | 位置 | 说明 |
|---|---|---|
| Type-A KV 点查 + 每租户字段白名单 | `service/app/kv_executor.py`、`service/services.yaml` | 版本化键空间（`tags:<ns>:latest` 别名 + 数据键）；查询键 SM3 哈希 |
| 网关 AppKey 认证 + 限流 | `docker-compose.yml`（APISIX）、`service/app/ratelimit.py` | key-auth consumer 从凭证注册表同步；按 key 限 QPS/每日配额 |
| 凭证生命周期（沙箱 / 正式 / 轮换） | `service/app/appkeys.py`、`service/registry/appkeys.yaml.example` | 限时 `sb_` 试用 key、`ak_` 正式 key（OA 工单）、带重叠窗口的轮换；密钥只存 sha256 摘要 |
| 授权矩阵（OA 授予的租户与字段） | `service/auth_matrix.yaml` | 注册表优先、矩阵兜底；演示租户：`demo_merchant`、`partner-demo`、`demo-1020` |
| 服务发布 + Admin API | `service/app/admin_api.py`、`admin/` | 发布/下线服务、授权、上游；每次写操作都落 admin 事件台账 |
| Portal 门户 + 服务目录 | `portal/` | 多页门户、服务目录/详情页、管理台（公开、免登录） |
| 无登录模式（ADR-0029） | `docs/opensource/ADR-0029-开源版无登录模式.md` | 去掉人的登录：门户/管理台开箱即用；机器调用方仍走 AppKey 凭证体系（`/v1/admin/*` 也不例外） |
| 审计管道 + 用量计量 | `service/app/audit.py`、`scripts/audit_consumer.py`、`service/app/metering.py` | JSONL / Kafka / 双写；按调用方 × 服务 × 天聚合，所有视图里 AppKey 一律打码 |
| 元数据库镜像（PostgreSQL） | `migrations/`、`scripts/meta_migrate.py`、`scripts/meta_reconcile.py` | YAML 注册表灌入元数据库；三方对账 + 快照导出 |
| 批量交付 | `service/app/batch_api.py`、`service/app/batch_worker.py` | 异步批量查询，带配额、快照和结果文件 |
| 代理枢纽（Type-B 透传） | `service/app/proxy_hub.py` | 白名单透传到已注册上游，同一套认证 + 审计语义 |
| 同步/装载工具 + 演示播种 | `sync/`、`scripts/seed_demo.sh`、`service/app/demo_seed.py`、`scripts/load_demo_data.sh` | CSV → Redis 管道、mock 生成器；**`docker compose up` 时 Redis 为空即自动播种**（哨兵键幂等，demo 与 full 档通用） |

<details>
<summary>English</summary>

## Features

| Feature | Where | Notes |
|---|---|---|
| Type-A KV point query with per-tenant field whitelist | `service/app/kv_executor.py`, `service/services.yaml` | Versioned keyspace (`tags:<ns>:latest` alias + data keys); SM3-hashed query keys |
| Gateway AppKey auth + rate limiting | `docker-compose.yml` (APISIX), `service/app/ratelimit.py` | key-auth consumers synced from the credential registry; per-key QPS/daily quotas |
| Credential lifecycle (sandbox / formal / rotation) | `service/app/appkeys.py`, `service/registry/appkeys.yaml.example` | Time-boxed `sb_` trial keys, `ak_` formal keys (OA ticket), rotation with overlap window; secrets stored only as sha256 digests |
| Auth matrix (OA-granted tenants & fields) | `service/auth_matrix.yaml` | Registry-first auth with matrix fallback; demo tenants: `demo_merchant`, `partner-demo`, `demo-1020` |
| Service publishing & admin API | `service/app/admin_api.py`, `admin/` | Publish/unpublish services, grants, upstreams; every write lands in an admin event ledger |
| Portal + catalog | `portal/` | Multi-page portal, service catalog/detail pages, admin console (public, no login) |
| No-login mode (ADR-0029) | `docs/opensource/ADR-0029-开源版无登录模式.md` | Human login removed: portal/admin console open out of the box; machine callers still authenticate via the AppKey credential system (`/v1/admin/*` included) |
| Audit pipeline + usage metering | `service/app/audit.py`, `scripts/audit_consumer.py`, `service/app/metering.py` | JSONL / Kafka / dual sinks; per-caller x per-service x per-day aggregation, masked appkeys in every view |
| Meta database twin (PostgreSQL) | `migrations/`, `scripts/meta_migrate.py`, `scripts/meta_reconcile.py` | YAML registries loaded into a meta DB; three-way reconcile + snapshot export |
| Batch delivery | `service/app/batch_api.py`, `service/app/batch_worker.py` | Asynchronous bulk queries with quota, snapshots and result files |
| Proxy hub (type-B passthrough) | `service/app/proxy_hub.py` | Whitelisted passthrough to registered upstreams, same auth + audit semantics |
| Sync/load tooling + demo seed | `sync/`, `scripts/seed_demo.sh`, `service/app/demo_seed.py`, `scripts/load_demo_data.sh` | CSV -> Redis pipelines, mock generators; **auto-seed on empty Redis at `docker compose up`** (sentinel-key idempotent, demo & full profiles) |

</details>

## 测试

前置条件：

```bash
pip install -r requirements-dev.txt          # pinned pytest dependency set (Python 3.13)
cp service/registry/appkeys.yaml.example service/registry/appkeys.yaml   # console credential registry is gitignored; fixtures read it
```

运行：

```bash
python3 -m pytest tests/ -q -rf
```

当前实测基线（CI 全量套件，含按环境跳过的项）：**1272 passed、4 skipped、0 failed**。依赖 Docker 的 DB e2e 模块（`test_admin_api_db_e2e`、`test_meta_writer_db`、`test_tenants_api`、`test_meta_migrate` 等）在 Docker 或镜像不可用时自动跳过。

全新克隆下的已知失败，按原因归类（要修掉它们，需要这份开源树刻意不带的东西，或者后续的测试修复）：

| 原因 | 涉及测试 | 为什么克隆下来修不了 |
|---|---|---|
| 归档的 e2e 证据文件（`tests/evidence/*.txt`）是运行输出，不是测试夹具 | 4 个（`test_publish_hbase`、`test_publish_load`、`test_batch_first_run`、`test_m2_6_acceptance`） | 它们断言真实端到端运行留下过证据归档；要过就得跑对应 e2e 脚本重新生成（有的需要完整 compose 栈、`compose.hbase.yml` 起的 HBase，或 CDP 浏览器） |
| 内部治理文档（`AGENTS.md`、backlog 任务、内部手册、内部 changelog 的 commit id）不随开源树发布 | 2 个（`test_m2_6_acceptance`、`test_docs_help_center`） | 内部项目管理产物已从开源树移除；留下的测试守护的是 e2e 验收脚本的存在性、隔离纪律、自包含检查，以及 changelog 不编造版本号 |
| 演示凭证字面量已脱敏成占位符，断言里残留旧掩码 | 5 个（`test_metering` ×2、`test_usage_report_api` ×3） | 待后续测试修复（对齐 `DEMO_KEY` 夹具与预期掩码） |
| appkeys 修复后暴露的一对 e2e：issue/grants 测试引用的是控制台已覆盖的服务，而不是那个已移除的仅运行时服务 | 2 个（`test_admin_api_db_e2e` 的 test_02/test_03） | 待后续测试修复（改为授予运行时发布的探针服务）；此前被「注册表缺失」的 setup 报错掩盖，没显出来 |

<details>
<summary>English</summary>

## Testing

Prerequisites:

```bash
pip install -r requirements-dev.txt          # pinned pytest dependency set (Python 3.13)
cp service/registry/appkeys.yaml.example service/registry/appkeys.yaml   # console credential registry is gitignored; fixtures read it
```

Run:

```bash
python3 -m pytest tests/ -q -rf
```

Current measured baseline (CI, full suite with environment-dependent deselects): **1272 passed, 4 skipped, 0 failed**. Docker-backed DB e2e modules (`test_admin_api_db_e2e`, `test_meta_writer_db`, `test_tenants_api`, `test_meta_migrate`, ...) skip gracefully when Docker or the image is unavailable.

Known failures in a fresh clone, by cause (fixing them requires artifacts this open-source tree deliberately does not ship, or follow-up test repairs):

| Cause | Tests | Why not fixable by cloning |
|---|---|---|
| Archived e2e evidence files (`tests/evidence/*.txt`) are run outputs, not fixtures | 4 (`test_publish_hbase`, `test_publish_load`, `test_batch_first_run`, `test_m2_6_acceptance`) | They assert that real end-to-end runs left evidence archives; regenerate by running the corresponding e2e scripts (some need the full compose stack, HBase via `compose.hbase.yml`, or a CDP browser) |
| Internal governance docs (`AGENTS.md`, backlog tasks, internal manuals, internal changelog commit ids) are not shipped | 2 (`test_m2_6_acceptance`, `test_docs_help_center`) | Internal project-management artifacts were removed from the open-source tree; the remaining tests guard the e2e acceptance script's presence, isolation discipline and self-contained checks, and that the changelog invents no version numbers |
| Demo credential literal was redacted to a placeholder, stale masks remain in assertions | 5 (`test_metering` x2, `test_usage_report_api` x3) | Follow-up test repair (align `DEMO_KEY` fixture and expected masks) |
| e2e pair unmasked by the appkeys fix: issue/grants tests reference a console-covered service instead of the removed runtime-only one | 2 (`test_admin_api_db_e2e` test_02/test_03) | Follow-up test repair (grant the runtime-published probe service instead); previously hidden behind the missing-registry setup error |

</details>

## 许可证

以 **Apache License 2.0** 发布，全文见 [`docs/opensource/LICENSE`](docs/opensource/LICENSE)。
<!-- NOTE: the LICENSE file currently lives at docs/opensource/LICENSE; it is planned to move to the repository root (becoming ./LICENSE). When it moves, update this link to point to ./LICENSE. -->

<details>
<summary>English</summary>

Released under the **Apache License 2.0** — see [`docs/opensource/LICENSE`](docs/opensource/LICENSE).

</details>

## 架构

一条流量路径：调用方 → APISIX 网关（key-auth、限流）→ 轻量 FastAPI 服务 → Redis（版本化 KV 命名空间），出口上做按调用的白名单字段投影、审计和计量；控制面状态放在 YAML 注册表里（`service/services.yaml`、`auth_matrix.yaml`、`registry/appkeys.yaml`），可选镜像一份进 PostgreSQL 元数据库，配套迁移/对账/快照工具；门户与管理台通过 admin API 驱动控制面。完整架构与图示见 [`docs/opensource/ARCHITECTURE.md`](docs/opensource/ARCHITECTURE.md)。
<!-- NOTE: docs/opensource/ARCHITECTURE.md is the open-source architecture doc (landing with the docs batch); if it is reorganized, keep this link in sync. -->

<details>
<summary>English</summary>

One traffic path: caller -> APISIX gateway (key-auth, rate limit) -> thin FastAPI service -> Redis (versioned KV namespaces) with per-call whitelist projection, audit and metering on the way out; control plane state lives in YAML registries (`service/services.yaml`, `auth_matrix.yaml`, `registry/appkeys.yaml`) optionally mirrored into a PostgreSQL meta database with migrate/reconcile/snapshot tooling; the portal/admin console drives the control plane through the admin API. See [`docs/opensource/ARCHITECTURE.md`](docs/opensource/ARCHITECTURE.md) for the full picture with diagrams.

</details>
