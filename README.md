# OneServe

**OneServe — a self-hosted, gateway-fronted data service platform: publish a whitelisted data field as a query API in minutes, govern every caller with AppKeys, and audit every call.**
（中英一句定位：OneServe 是自托管的网关化数据服务平台——把白名单数据字段分钟级发布成查询 API，调用方按 AppKey 授权，全链路留痕审计。）

## What is this

OneServe sits between your data warehouse copies (loaded into Redis as versioned KV) and external/internal callers. A thin FastAPI service exposes **type-A KV point queries** (and tag-style batch lookups) behind an **APISIX gateway** that authenticates callers by AppKey; a per-tenant **field whitelist** (auth matrix + console-issued credential registry) decides which fields each caller may see. Around that core: a portal + admin console for service publishing, grant management and credential lifecycle (sandbox trial keys, formal keys with OA ticket, rotation with overlap), an audit pipeline (JSONL / Kafka) with usage metering and reports, an optional PostgreSQL "meta database" twin of the YAML registries with migration/reconcile tooling, and a batch worker for asynchronous bulk delivery. Demo data and credentials are built in, so you can run a first end-to-end query in minutes.

## Quick start (one command)

Prerequisites: Docker (with Compose v2) — nothing else.

```bash
git clone https://github.com/suyukun/OneServe.git && cd OneServe
docker compose up -d
open http://localhost:8000
```

> **国内网络提示**：Docker Hub 直连拉取可能超时——请先配置镜像加速（如 `docker.m.daocloud.io`），或手动拉取 compose 引用的镜像后再起栈。
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
| `demo_bank` | `uat-7f3a9c21e5b84d06` | asset-tag services (SM3 query keys) |
| `demo-1020` | `uat-58b0d2e7a9c14613` | `v1-svc-usr-asset-tag1020` |

### Two profiles: demo (default) vs full

| | `docker compose up` — demo, default | `docker compose --profile full up` (= `bash scripts/up.sh`) |
|---|---|---|
| Services | **3**: `redis` + `service` + `portal` | **6**: + `etcd` + `apisix` + `batch-worker` |
| Portal + admin console | ✅ full feature set on http://localhost:8000 | ✅ same |
| Demo data | ✅ auto-seeded | ✅ auto-seeded (same sentinels) |
| Machine data plane :9080 (gateway AppKey plane: `/v1/query/*`, `/v1/tags/query`, `/v1/batch/*`, `/proxy/*`) | ❌ not in this profile — portal-origin queries fall back to the service layer automatically, and `/healthz` reports `gateway`/`etcd` as `degraded` honestly | ✅ APISIX key-auth + rate limit + gateway-side audit |
| Async batch delivery worker | ❌ not in this profile | ✅ |

> **查询通道说明**：演示 AppKey 在默认 demo 档经门户同源反代直通服务层，开箱即查；`full` 档数据面查询（`:9080`）经 APISIX 网关 key-auth——需先经网关 Admin API 为该凭证注册 consumer（经管理台签发/启用凭证会自动同步；演示 consumer 的开箱自动注册在路线图中）。
> **Query-path note**: the demo AppKey works out of the box in the default demo profile (portal same-origin proxy); in the `full` profile, data-plane queries go through APISIX key-auth, so a consumer must be registered for the credential via the gateway Admin API first (issuing/enabling a credential in the admin console syncs it automatically; out-of-the-box auto-registration of the demo consumer is on the roadmap).

Tear down with `docker compose down` (demo) or `docker compose --profile full down` (everything); add `-v` to also drop data volumes.

## Full stack (Docker Compose `--profile full`, 6 services)

```bash
bash scripts/up.sh          # cp .env.example .env (first run) -> docker compose --profile full up -d --build -> wait healthy -> init APISIX routes
```

Brings up all 6 containers: `etcd`, `redis`, `apisix`, `service`, `batch-worker`, `portal` (the demo profile above runs the last three only — the gateway-side components are opt-in). Entrypoints after startup:

| Entry | URL | Notes |
|---|---|---|
| Portal + admin console | http://localhost:8000 | Public, no login (ADR-0029): open the portal and you are on the home page; the admin console is directly usable |
| Gateway (machine data plane) | http://localhost:9080 | `/v1/query/*`, `/v1/tags/query`, `/v1/batch/*`, `/proxy/*` — AppKey self-auth, no login |

<details>
<summary><b>Manual setup (without Docker)</b> — the old two-terminal quickstart, for developing against a local Redis</summary>

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

</details>

**Base image note (production build):** the dev compose builds `service` from [`service/Dockerfile`](service/Dockerfile) on the official `python:3.13-slim` — nothing to pre-build. The **prod** image [`Dockerfile.prod`](Dockerfile.prod) however builds on a locally pre-built hardened base:

```bash
docker build -f docker/python-base.Dockerfile -t oneserve-python-base:3.13-slim .
docker buildx build --platform linux/arm64 -f Dockerfile.prod -t oneserve/service:prod .
```

## Features

| Feature | Where | Notes |
|---|---|---|
| Type-A KV point query with per-tenant field whitelist | `service/app/kv_executor.py`, `service/services.yaml` | Versioned keyspace (`tags:<ns>:latest` alias + data keys); SM3-hashed query keys |
| Gateway AppKey auth + rate limiting | `docker-compose.yml` (APISIX), `service/app/ratelimit.py` | key-auth consumers synced from the credential registry; per-key QPS/daily quotas |
| Credential lifecycle (sandbox / formal / rotation) | `service/app/appkeys.py`, `service/registry/appkeys.yaml.example` | Time-boxed `sb_` trial keys, `ak_` formal keys (OA ticket), rotation with overlap window; secrets stored only as sha256 digests |
| Auth matrix (OA-granted tenants & fields) | `service/auth_matrix.yaml` | Registry-first auth with matrix fallback; demo tenants: `demo_bank`, `partner-demo`, `demo-1020` |
| Service publishing & admin API | `service/app/admin_api.py`, `admin/` | Publish/unpublish services, grants, upstreams; every write lands in an admin event ledger |
| Portal + catalog | `portal/` | Multi-page portal, service catalog/detail pages, admin console (public, no login) |
| No-login mode (ADR-0029) | `docs/opensource/ADR-0029-开源版无登录模式.md` | Human login removed: portal/admin console open out of the box; machine callers still authenticate via the AppKey credential system (`/v1/admin/*` included) |
| Audit pipeline + usage metering | `service/app/audit.py`, `scripts/audit_consumer.py`, `service/app/metering.py` | JSONL / Kafka / dual sinks; per-caller x per-service x per-day aggregation, masked appkeys in every view |
| Meta database twin (PostgreSQL) | `migrations/`, `scripts/meta_migrate.py`, `scripts/meta_reconcile.py` | YAML registries loaded into a meta DB; three-way reconcile + snapshot export |
| Batch delivery | `service/app/batch_api.py`, `service/app/batch_worker.py` | Asynchronous bulk queries with quota, snapshots and result files |
| Proxy hub (type-B passthrough) | `service/app/proxy_hub.py` | Whitelisted passthrough to registered upstreams, same auth + audit semantics |
| Sync/load tooling + demo seed | `sync/`, `scripts/seed_demo.sh`, `service/app/demo_seed.py`, `scripts/load_demo_data.sh` | CSV -> Redis pipelines, mock generators; **auto-seed on empty Redis at `docker compose up`** (sentinel-key idempotent, demo & full profiles) |

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

Current measured baseline (CI, full suite with environment-dependent deselects): **1272 passed, 4 skipped, 0 failed**. Docker-backed DB e2e modules (`test_admin_api_db_e2e`, `test_meta_writer_db`, `test_meta_migrate`, ...) skip gracefully when Docker or the image is unavailable. The Docker-backed DB e2e modules (`test_admin_api_db_e2e`, `test_meta_writer_db`, `test_tenants_api`, `test_meta_migrate`, ...) skip gracefully when Docker or the image is unavailable.

Known failures in a fresh clone, by cause (fixing them requires artifacts this open-source tree deliberately does not ship, or follow-up test repairs):

| Cause | Tests | Why not fixable by cloning |
|---|---|---|
| Archived e2e evidence files (`tests/evidence/*.txt`) are run outputs, not fixtures | 4 (`test_publish_hbase`, `test_publish_load`, `test_batch_first_run`, `test_m2_6_acceptance`) | They assert that real end-to-end runs left evidence archives; regenerate by running the corresponding e2e scripts (some need the full compose stack, HBase via `compose.hbase.yml`, or a CDP browser) |
| Internal governance docs (`AGENTS.md`, backlog tasks, internal manuals, internal changelog commit ids) are not shipped | 2 (`test_m2_6_acceptance`, `test_docs_help_center`) | Internal project-management artifacts were removed from the open-source tree; the remaining tests guard the e2e acceptance script's presence, isolation discipline and self-contained checks, and that the changelog invents no version numbers |
| Demo credential literal was redacted to a placeholder, stale masks remain in assertions | 5 (`test_metering` x2, `test_usage_report_api` x3) | Follow-up test repair (align `DEMO_KEY` fixture and expected masks) |
| e2e pair unmasked by the appkeys fix: issue/grants tests reference a console-covered service instead of the removed runtime-only one | 2 (`test_admin_api_db_e2e` test_02/test_03) | Follow-up test repair (grant the runtime-published probe service instead); previously hidden behind the missing-registry setup error |

## License

Released under the **Apache License 2.0** — see [`docs/opensource/LICENSE`](docs/opensource/LICENSE).
<!-- NOTE: the LICENSE file currently lives at docs/opensource/LICENSE; it is planned to move to the repository root (becoming ./LICENSE). When it moves, update this link to point to ./LICENSE. -->

## Architecture

One traffic path: caller -> APISIX gateway (key-auth, rate limit) -> thin FastAPI service -> Redis (versioned KV namespaces) with per-call whitelist projection, audit and metering on the way out; control plane state lives in YAML registries (`service/services.yaml`, `auth_matrix.yaml`, `registry/appkeys.yaml`) optionally mirrored into a PostgreSQL meta database with migrate/reconcile/snapshot tooling; the portal/admin console drives the control plane through the admin API. See [`docs/opensource/ARCHITECTURE.md`](docs/opensource/ARCHITECTURE.md) for the full picture with diagrams.
<!-- NOTE: docs/opensource/ARCHITECTURE.md is the open-source architecture doc (landing with the docs batch); if it is reorganized, keep this link in sync. -->
