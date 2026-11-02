# OneServe 页面功能导览

> 本导览对应 v0.1.0 界面。页面名与组件标识均照仓内实际（`portal/src/`、`admin/`）；
> 入口路径为门户站点（nginx）实际路由，API 为页面粗粒度依赖，细节以 `docs/opensource/ARCHITECTURE.md` 与各页源码为准。

## 30 秒理解系统

调用方拿 AppKey 调数据 API，能看哪些字段由授权白名单决定，没批的字段后端零返回；运营方在管理台发布服务、发凭证、管授权；每一次调用都留审计、计计量，全程可查。门户给「用的人」，管理台给「管的人」。

> 关于 SM3：SM3 摘要在本平台仅作为国密合规场景的可选摘要算法存在（属标准摘要算法、可替换实现，如演示通道查询键生成），不是平台核心能力。

---

## 门户（调用方视角）

源码在 `portal/src/`，构建产物在 `portal/`。共 14 页（含报表专区 4 页）。

### index.html · 首页总览

- **入口路径**：`/`（组件 `homePage`）
- **一句话功能**：平台门面——全局搜索＋平台规模数字条＋运行状态条＋公告与数据新鲜度。
- **关键能力点**：
  1. 全局搜索框（hero 位）：按业务域/服务/API 路径过滤，回车携关键词进服务目录；
  2. 4 张数字卡：覆盖用户数（标口径）/ 在线服务 API / 已授权接入方 / 累计调用＋近 7 日迷你趋势，小基数如实徽标、演示数据带角标；
  3. 公告栏＋数据新鲜度（各业务域最后同步时间与同步条数）。
- **主要 API**：`/v1/portal/overview`、`/v1/portal/status`、`/v1/portal/freshness`、`/v1/portal/announcements`

### catalog.html · 服务目录

- **入口路径**：`/catalog`（组件 `catalogPage`）
- **一句话功能**：按业务域分组浏览全部在线 API。
- **关键能力点**：
  1. 业务域分组卡片，展示每个服务的名称/路径/字段规模；
  2. 支持携首页搜索关键词进入后前端过滤；
  3. 每个服务可进入详情页。
- **主要 API**：`/v1/portal/catalog`

### detail.html · 服务详情

- **入口路径**：`/detail?svc=…`（组件 `detailPage`）
- **一句话功能**：单个服务的「说明书」：入参、出参、报文示例、版本变更一页看全。
- **关键能力点**：
  1. 入参表＋出参表——出参即「返回标签字段清单（授权白名单口径）」，没批的字段不在列；
  2. 键未命中语义说明（HTTP 200 诚实空值）＋错误码与解决方案；
  3. 报文示例可复制＋版本与变更记录＋「如何获得调用凭证」指引。
- **主要 API**：`/v1/portal/catalog`（清单与详情数据）

### trial.html · 接口调试

- **入口路径**：`/trial`（组件 `trialPanel`）
- **一句话功能**：已发布服务在线调试——选服务、填 AppKey，浏览器里直接发起真实调用。
- **关键能力点**：
  1. 服务下拉选择＋访问地址展示；入参预填示例值可改；
  2. 出参按「该服务授权字段」渲染，授权投影红线现场体感；
  3. 演示通道可在页面本地生成查询键（SM3 摘要，见开头说明）。
- **主要 API**：`/v1/svc/{service}`（网关侧真实调用）

### docs.html · 帮助中心

- **入口路径**：`/docs`（静态页）
- **一句话功能**：从零用起来的帮助中心：接入、术语、SLA、找谁。
- **关键能力点**：
  1. 快速接入（四步）＋5 分钟上手（三步）＋术语表（一句话版：网关/KV 键/字段授权/降级/演示数据）；
  2. 限流与配额、SLA 响应时限、「查不到数怎么办（三步）」、值班说明；
  3. SDK 与代码样例＋变更日志；原 `/apply`、`/contact` 入口 301 到本页。
- **主要 API**：无（静态内容，示例引用 `/v1/svc/usr-basic-info`）

### my.html · 我的请求（凭证 · 配额 · 调用记录）

- **入口路径**：`/my`（组件 `myAccessPage`）
- **一句话功能**：调用方自助视角——我的凭证、用量、配额、调用日志一页看全。
- **关键能力点**：
  1. 我的凭证（AppKey 掩码与状态）；
  2. 用量统计（近 7 日）＋我的配额；
  3. 我的日志（调用记录）。
- **主要 API**：`/v1/portal/my/credentials`、`/v1/portal/my/quota`、`/v1/portal/my/logs`、`/v1/admin/metering/summary`

### grants.html · 授权与凭证全景（只读）

- **入口路径**：`/grants`（组件 `grantsViewPage`）
- **一句话功能**：授权登记全景与凭证联查的只读视图。
- **关键能力点**：
  1. 授权登记全景表（租户 × 服务 × 字段白名单），支持筛选；
  2. 凭证 kid ∪ 租户双口径联查；
  3. 后端不可达时降级读 mock（页面挂「演示数据」角标）。
- **主要 API**：`/v1/portal/grants/panorama`、`/v1/admin/appkeys/usage`

### audit.html · 审计查询

- **入口路径**：`/audit`（组件 `auditQueryPage`）
- **一句话功能**：按条件检索调用审计台账，每次调用都有据可查。
- **关键能力点**：
  1. 检索条件（时间/调用方/服务等）；
  2. 审计台账列表（状态、耗时、返回码）；
  3. 耗时 Top 端点。
- **主要 API**：`/v1/portal/audit/logs`

### errors.html · 统一错误码表

- **入口路径**：`/errors`（组件 `errorsCenterPage`）
- **一句话功能**：网关＋服务层统一错误码对照表。
- **关键能力点**：
  1. 错误响应长什么样（信封结构示例）；
  2. 码段规则：系统级 vs 服务级；
  3. 错误码列表（含义与解决方案）。
- **主要 API**：无（静态内容）

### ops.html · 运行视图

- **入口路径**：`/ops`（组件 `opsPage`）
- **一句话功能**：深色驾驶舱——平台今天跑得怎么样的单屏答案。
- **关键能力点**：
  1. 4 个 KPI 大数字（今日调用/成功率等；小基数「真实计数」徽标策略，不硬撑大数）；
  2. 近 7 日调用趋势面积图＋各业务域数据新鲜度条形；
  3. 网关/服务/Redis/审计组件真探活；60s 静默轮询原位更新。
- **主要 API**：`/v1/portal/status`

### 报表专区（`portal/reports/`，组件见 `assets/reports.js`）

| 页面 | 入口路径 | 一句话功能 | 主要 API |
|---|---|---|---|
| overview.html · 用量总览 | `/reports/overview` | 平台用量总貌：调用量/调用方/服务面 | `/v1/admin/usage/overview` |
| services.html · 服务用量 | `/reports/services` | 按服务维度拆解用量排行 | `/v1/admin/usage/services` |
| subjects.html · 主体用量 | `/reports/subjects` | 按主体（租户/调用方）维度拆解用量 | `/v1/admin/usage/subjects` |
| trend.html · 用量趋势 | `/reports/trend` | 用量随时间趋势曲线 | `/v1/admin/usage/trend` |

---

## 管理台（运营视角）

源码在 `admin/`，构建期由 `scripts/build_portal.py` 同步进 `portal/admin/`（门户壳版）。共 11 页。

### publish.html · 服务发布

- **入口路径**：`/admin/publish`（组件 `publishWizard`）
- **一句话功能**：五步向导，把已装载的数据发布成一个查询 API。
- **关键能力点**：
  1. 五步向导：选择已装载的表 → 选择数据版本 → 定义服务 → 选择出参字段 → 确认并发布；
  2. 发布后「让调用方用起来」授权配置三步引导；
  3. HBase 数据源可在线 discovery 探测表结构。
- **主要 API**：`/v1/admin/publish`、`/v1/admin/load/assets`、`/v1/admin/versions`、`/v1/admin/publish/hbase/discovery`

### appkeys.html · AppKey 发放（凭证）

- **入口路径**：`/admin/appkeys`（组件 `appkeyAdmin`）
- **一句话功能**：调用方凭证的签发与生命周期管理。
- **关键能力点**：
  1. 签发沙箱/正式 AppKey（正式件须 OA 批件号）；secret 只存 sha256 摘要、界面掩码；
  2. 每凭证配额（每秒·每日）设置；轮换/禁用；
  3. 计量报表已迁报表中心，本页专注凭证本身。
- **主要 API**：`/v1/admin/appkeys`、`/v1/admin/appkeys/{appkey}/quota`、`/v1/admin/appkeys/usage`

### grants.html · 授权管理（授权全景）

- **入口路径**：`/admin/grants`（组件 `grantsAdmin`）
- **一句话功能**：租户 × 服务 × 字段白名单的授权登记管理。
- **关键能力点**：
  1. 租户列表＋单租户授权详情全景（字段级白名单、OA 单号、生效区间）；
  2. 授权登记/调整（缺 OA 单号拒绝）；
  3. 后端不可达时降级读 `admin/mock/grants.json`（演示数据角标）。
- **主要 API**：`/v1/admin/grants`、`/v1/admin/grants/tenants`

### monitor.html · 监控

- **入口路径**：`/admin/monitor`（组件 `monitorAdmin`）
- **一句话功能**：异常、批量任务与常驻容器的运行监控。
- **关键能力点**：
  1. 异常摘要＋按调用方/按 API 下钻（调用方掩码展示）；
  2. 常驻容器健康（角色/探针/状态）；
  3. 批量任务检索与 trace 明细。
- **主要 API**：`/v1/admin/monitor/errors`、`/v1/admin/monitor/containers`、`/v1/admin/monitor/batch`、`/v1/admin/monitor/trace`

### load.html · 数据装载

- **入口路径**：`/admin/load`（组件 `loadWizard`）
- **一句话功能**：把外部数据源（如 HBase 表）装载进平台版本化 KV。
- **关键能力点**：
  1. 选择数据源 → 表清单 → 全量装载的任务式向导；
  2. 字段释义卡（字段名/类型/中文 COMMENT/业务口径/敏感级）；
  3. 装载任务状态跟踪；schema 解析与保存。
- **主要 API**：`/v1/admin/load`、`/v1/admin/load/sources`、`/v1/admin/load/tables`、`/v1/admin/load/tasks/*`、`/v1/admin/schema/parse`、`/v1/admin/schema/save`

### versions.html · 版本管理

- **入口路径**：`/admin/versions`（组件 `versionsConsole`）
- **一句话功能**：数据版本空间的管理台。
- **关键能力点**：
  1. 版本空间 × 表（namespace）× 版本列表（键数/状态）；
  2. 版本切换与删除（`versions/delete`）。
- **主要 API**：`/v1/admin/versions`、`/v1/admin/versions/delete`

### pricing.html · 价目管理

- **入口路径**：`/admin/pricing`（组件 `pricingAdmin`）
- **一句话功能**：计价对象 × 计价模型的价目登记与命中预览。
- **关键能力点**：
  1. 价目列表：计价对象/模型/档位摘要/生效区间/币种/展示状态；
  2. 新建价目向导（区间阶梯：下界含、上界不含、单价），支持 `#wizard` 深链；
  3. 计费命中预览（按服务引用试算命中档位）。
- **主要 API**：`/v1/admin/pricing/items`、`/v1/admin/pricing/selector`、`/v1/admin/pricing/hit-preview`

### billing.html · 月账单

- **入口路径**：`/admin/billing`（组件 `billingAdmin`）
- **一句话功能**：月度账期的出账对齐与人工确认。
- **关键能力点**：
  1. 账期四态流转：草稿 → 已对账 → 已确认 → 已核销（T+3 关闭 / 对账清零 / 覆盖齐零三硬前置留痕）；
  2. 出账＝机器任务走命令行（页面只读＋确认，不触发出账，出账引导卡给 CLI 指引）；
  3. 应收合计/欠费/OA 单号＋双快照明细＋CSV 导出；确认/补录仅管理员。
- **主要 API**：`/v1/admin/billing/periods/*`（出账脚本侧见 `scripts/metering_report.py`）

### upstreams.html · 上游配置

- **入口路径**：`/admin/upstreams`（组件 `upstreamAdmin`）
- **一句话功能**：代理类服务的上游地址与模拟代理配置。
- **关键能力点**：
  1. 三步向导新建代理（服务名/上游地址）；
  2. 已配置代理列表（状态/最近变更人/时间）＋探活测试；
  3. 下线代理/删除草稿。
- **主要 API**：`/v1/admin/upstreams`、`/v1/admin/upstreams/probe`

### tenants.html · 租户管理

- **入口路径**：`/admin/tenants`（组件 `tenantsAdmin`）
- **一句话功能**：租户全生命周期管理。
- **关键能力点**：
  1. 租户列表：类型（内部/外部/沙箱）/状态/active 凭证数/在册授权/开通 OA 单号；
  2. 新建租户；冻结/解冻（冻结后凭证签发与调用被门禁拦截）。
- **主要 API**：`/v1/admin/tenants`、`/v1/admin/tenants/resync`

### published.html · 已发布服务

- **入口路径**：`/admin/published`（组件 `publishedConsole`）
- **一句话功能**：已发布服务的清单与接口文档镜像。
- **关键能力点**：
  1. 服务列表：访问地址/数据版本/状态/字段数；
  2. 每服务接口文档（入参查询键＋出参字段示例值）；
  3. 授权一行文案＋凭证 kid ∪ 租户双口径联查。
- **主要 API**：`/v1/admin/publish`、`/v1/portal/grants/panorama`、`/v1/admin/appkeys`

---

## 页面清点

- 门户：14 页 = 主站 10 页（index / catalog / detail / trial / docs / my / grants / audit / errors / ops）＋报表专区 4 页（overview / services / subjects / trend）。
- 管理台：11 页（publish / appkeys / grants / monitor / load / versions / pricing / billing / upstreams / tenants / published）。
- 另有自检页（`portal/selftest*.html`、`admin/selftest-*.html`、`admin/perf.html`）面向开发/验收，不属日常使用导览。
