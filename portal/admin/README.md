# admin/ · OneServe 运营版操作面（夜卡 016 白天复产 · O-3 热身卡；P0-4 接真）
依据：docs/plans/运营版设计稿-三界面_v0.md（O-3 节＋v0.1 增补四修正）＋docs/plans/前台产品重造-SPEC_v0.md §5 P0-4。

> **2026-09-23 布局统一（本卡）**：三张管理页已接入门户壳——侧导航/环境条/顶栏/内容容器/页脚
> 由 `scripts/build_portal.py` 从 `portal/partials/` 拼接（不再是各自手写 `<html><head>`），
> 点进去与门户同一套布局。**本卡只做布局统一，未做令牌显示**（另卡）。
> 构建产物 `portal/admin/*.html`（入库）；`admin/*.html` 仍是唯一真源（构建期抽取正文/样式/脚本）。

## 文件
- `appkeys.html` —— O-3 AppKey 自助发放页（列表＋签发表单＋Secret 一次性弹窗＋两段式禁用/启用＋appkeys.yaml 导出预览）。**管理页保留自身深色操作面样式**（页内 `<style>` 经构建期注入，语义别名 `--admin-*` 覆盖门户令牌；`portal/assets/vendor/` Tabler 在本页不加载，沿 #171B21/#1F242C/#E85B6B）。
- `appkeys.js` —— Alpine 组件＋纯函数单元（window.O3，供自测直调）。注册口径＝alpine:init，业务 js 先于 alpine.min.js 加载（本仓 alpine 构建 microtask 即 start()，晚放会 not defined；O-2 转告实证 2026-09-22）。
- `mock/appkeys.json` —— 演示基线：只存 sha256(secret) 前 16 位，无 Secret 明文。
- `selftest-admin.html` / `selftest-admin.js` —— 浏览器断言页 19 条全绿（含「批件号空→按钮 disabled」「组件已挂载」）。

## 运行与验收
```bash
# ① 布局统一（本卡）：构建产物按门户单根（portal/）伺服，管理页即门户壳版
cd <仓库根> && python3 scripts/build_portal.py
python3 -m http.server 8765 --bind 127.0.0.1 --directory portal
curl -s http://127.0.0.1:8765/admin/appkeys.html | grep -o 'id="app-sidebar"\|id="app-topbar"\|id="appkey-table"'

# ② 断言：壳契约（Python）＋真浏览器（CDP，桌面 1440 ＋ 窄屏 390 无溢出）
python3 -m pytest tests/test_admin_shell.py tests/test_admin_gate.py -q
node tests/evidence/admin-shell/check_admin_shell.mjs      # 见 tests/evidence/admin-shell/result.txt

# ③ 源页自测（selftest 三套件；selftest-admin 的 A1 sha256 向量为既有失败，非本卡引入）
#    打开 http://127.0.0.1:8765/admin/selftest-publish.html → 17/17 全绿
#    打开 http://127.0.0.1:8765/admin/selftest-upstreams.html → 22/22 全绿
grep -rEn 'sk_[A-Za-z0-9+/_-]{20,}' admin/                  # 应无输出（存储无明文）
```

### 布局统一（本卡）相对路径口径
| 位置 | 源页（admin/） | 产物（portal/admin/） |
|---|---|---|
| 共享资产 | `../portal/assets/` | `../assets/` |
| 门户首页 | `../portal/index.html` | `../index.html` |
| 管理页互链 | `index.html`（同目录） | 同名 |

## P0-4 接真（凭证发放基建）
签发/禁用/启用/配额调整均调真后端（/v1/admin/appkeys*，门户 nginx 反代；写操作带管理令牌 X-Admin-Token，
本机开发默认值见 .env.example）：服务端建网关 consumer（APISIX Admin API）＋写权威源
service/registry/appkeys.yaml（随仓库 git 留痕）＋热加载（免重启即时生效）。「查看 appkeys.yaml」
展示服务端权威文件原文。后端不可达时降级 mock 列表并挂「演示数据」角标，此时操作仅内存演示
（留痕条如实标注演示模式，不伪造发放记录）。沙箱凭证 sb_ 前缀限时试用（默认 7 天，SB_TTL_DAYS 可配）；
正式凭证 ak_ 前缀须绑定 OA 批件号（ADR-0003）。Secret 明文只存在于一次性弹窗 DOM 与用户主动下载的 .txt。

## M2-5 O-1 前端升级 HBase 版（AC-4 发现 / AC-5 选表→配字段→真试查门禁）
发布台数据源双模式（`admin/publish.html` 顶部「数据源」切换）：
- **HBase 真表**（默认）：左侧真目录树＝**业务域 → namespace/表 → 列族/列**（数据来自 M2-4
  发现端点 `GET /v1/admin/publish/hbase/discovery`）；选表→勾列（示例值/描述行内编辑）→
  **键类型可配**（`usr_id` / `phone_sm3`，AC 明文；键模式默认宽松）→以示例键对 HBase **真实试查**
  （`POST /v1/admin/publish/hbase/test`）→通过才可发布（缺示例值/缺示例键/试查不过＝按钮 disabled）。
  页内「后端机检（AC-5 断言）」＝后端 selftest 端点（`…/hbase/selftest`）结果直显。
- **Redis 字段字典**（存量·向后兼容）：原「字段字典 CSV×注册表」流程一行未改。

**契约适配层**：`admin/publish-hbase-adapter.js` 是**唯一**解析后端响应的地方（端点常量、
请求体构造、响应归一化）；M2-4 定稿后只改这一个文件。load 序：适配层 → publish.js → alpine。

**诚信线**：HBase 不可达 / 发现端点未就绪 → **如实错误条**（机读码＋人话＋处置提示），
目录树零表、发布禁用；**绝不**回落 mock 假表冒充真表。

**测试（headless 口径，AC-5 原文「机检以后端 selftest API 断言为准，浏览器页仅人肉复核）」**：
- 机检：`tests/test_publish_hbase.py`（19 例：适配层归一/契约集中/诚信线/门禁函数/请求体/
  真 meta fixture/构建同步），已入 `scripts/run_gates.sh` 动态枚举；
- headless 可跑证据：`node scripts/m25_selftest_browser.mjs`（浏览器断言页 30/30 全绿）＋
  `node scripts/m25_publish_page_check.mjs`（真实发布页如实错误态 8/8）；
- 真链路：`python3 scripts/m25_hbase_e2e.py`（起 HBase 开发替身容器→真 meta→适配层归一→
  真点查命中→清理）；元数据抓取 `scripts/m25_hbase_fixture.py`；证据 `tests/evidence/m25-hbase-e2e.txt`。
- **平台前提**：平台不自建 HBase、不新增常驻服务；生产＝示例 HBase 集群。开发替身容器定义在
  `compose.hbase.yml`（独立 override，默认不随 up.sh 起，验完即删让位内存）；连接地址走
  `HBASE_HOST`/`HBASE_PORT`，平台只读（写入方归数仓，NC-M2-2）。
- **如实标注**：M2-4 后端端点未合入，本页以「契约＋适配层」实现；发现/试查/自检三端点的
  **服务端**由 M2-4 提供，当前现场＝404 如实错误态。端到端联调待 M2-4 合入。

## P0-6 导航可达＋最小鉴权 gate（SPEC §5 P0-6 行；D1=b 凭证兼身份）
- **导航可达**：门户侧栏「管理」组三链（发布台/AppKey 发放/上游代理）无死链；三页顶栏面包屑
  （admin-crumb）首级回链门户首页；「我的接入」侧栏入口在位。
- **最小鉴权 gate**（不自建登录系统）：三页共用 admin-gate.js（AdminGate）——令牌栏输入
  （sessionStorage 键 oneserve-admin-token，会话级，任一页保存三页生效）；**无令牌＝只读模式**
  （写按钮禁用＋顶部 banner「输入管理令牌解锁操作」）；令牌正确性由后端裁决（GET /v1/admin/appkeys
  带 X-Admin-Token 探测）：200=解锁／401=明确报「管理令牌不正确」且写操作锁死（不静默）／
  404·网络错=unverified（后端不在场＝演示模式，写仅内存演示）。
- **写操作令牌头**：appkeys 页写请求统一带 X-Admin-Token（后端 admin_api 全端点校验）；publish/
  upstreams 写侧接真归 P1-10/11（D2=a），闸与按钮先就位。
- **测试**：tests/test_admin_gate.py（导航/一致 DOM/闸语义/构建同步，10 例已入 run_gates）＋
  selftest 三套件各加闸断言组（锁定/解锁/错误令牌/三页一致）。
- **写操作令牌头**：appkeys 页写请求统一带 X-Admin-Token（后端 admin_api 全端点校验）；publish/
  upstreams 写侧接真归 P1-10/11（D2=a），闸与按钮先就位。
- **测试**：tests/test_admin_gate.py（导航/一致 DOM/闸语义/构建同步，10 例已入 run_gates）＋
  selftest 三套件各加闸断言组（锁定/解锁/错误令牌/三页一致）。

## 管理令牌可见（开发占位 · 维护者 2026-09-23）
- **背景**：管理令牌此前只写在 `.env.example` 与源码缺省值里，使用者「不知道它是什么」。
  维护者 决策：**不改令牌值**，而是让它在开发页面上可见并显式标注。
- **实现（单点）**：`admin-gate.js`（三页共用）经 `AdminGate.loadDevHint()` 调后端只读端点
  `GET /v1/portal/admin-token-hint`（`service/app/portal_api.py`，docstring 显式标注「仅开发用途」，
  经既有 `/v1/portal/` nginx 反代段，无需改 conf）。三页顶栏令牌栏同款 DOM：
  `dev-token-hint` / `dev-token-value` / `dev-token-note`，展示「开发占位」角标＋当前令牌＋生产改造说明。
- **安全底线（裁决唯一在 `service/app/dev_token.py` 纯逻辑）**：只有**开发占位态**
  （`ADMIN_API_TOKEN` 未显式配置，或显式等于内置默认值）才下发真值；一旦部署方显式设为
  非默认值，端点返回 `token: null`（**不泄漏，连掩码都不给**），页面只显示「不在页面显示」。
  `.env` 覆盖能力不变；`admin_api.py`/`monitor_api.py` 的令牌缺省值改取同一单源（防三处漂移）。
- **前端零硬编码**：页面/脚本不含任何令牌真值，真值只由后端在占位态下发。
- **生产改造**：严格认证落地时**应直接移除该端点**，前端提示随之消失（不在本阶段做 OAuth/SSO/审计留痕）。
- **测试**：`tests/test_admin_token_hint.py`（10 例：纯逻辑占位可见／非默认值不泄漏／Node 直跑渲染口径／
  端点标注开发用途／三页同款 DOM／零硬编码／既有闸三态回归）；既有三页 selftest 闸断言不变。

## P1-6 异常与批量监控（B7 · SPEC §5-P1-6／§2-B7）
- `monitor.html` / `monitor.js` —— B7「异常与批量监控」页（侧栏「管理」组第四入口）：
  异常列表＋按调用方/API 下钻＋错误样本＋追踪号详情，以及**批量运行视角**
  （队列深度/进度/五态计数/积压/最近任务）。
- **只做「可看」**：不做告警通道与值班送达（归后续阶段）；无异常如实「暂无异常」，
  不填假样本；未接入项（任务耗时趋势/worker 心跳/键级失败下钻）页面显式标注。
- **接真端点**（三只读，均过 X-Admin-Token 闸；决策全在 `service/app/monitor.py` 纯逻辑）：
  - `GET /v1/admin/monitor/errors` —— 审计 JSONL 筛 4xx/5xx＋下钻聚合（异常口径复用
    `my_access` 的掩码/时间/状态实现，与「合规审计」页同函数同口径）；
  - `GET /v1/admin/monitor/trace` —— 按 `trace_id` 跨「服务侧＋批量任务级」两份审计精确反查；
  - `GET /v1/admin/monitor/batch` —— 真源＝Redis 任务结构（JobStore）：LLEN 队列深度／
    running+cancelling 执行中／五态计数／积压最长等待（无 queued＝null，不是 0 秒）／
    队列滞留如实暴露；Redis 读失败＝degraded 零值降级并显著标注「非真实状态」。
- **状态带接真（维护者 追加）**：门户运行视图底部四组件卡（网关/服务/Redis/审计）由
  `/v1/portal/status` 的 `components[]` 真探活驱动——网关 TCP 探 9080／Redis PING／
  服务本进程 uptime／审计注册配置；**探不到＝error→页面「未知」，不回退「正常」**；
  容量梯子 CPU/内存实测降为副注并标「实测档案（非实时）」。
- **测试**：tests/test_monitor.py（后端纯函数 28 例）＋tests/test_monitor_portal.py（门户
  DOM/契约/状态带接真 21 例）＋tests/test_monitor_selftest.py（Node 纯函数真值 5 例）。

