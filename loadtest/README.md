# loadtest/ —— 通用压测梯子（P3 通用化 · 对抗复核 P0/P1 修正版）

**用户只提供「服务（service_id 或 route）＋ AppKey」，其余全自动**：
授权/配额前置门禁 → 服务登记解析（console_services 直取 /v1/svc/* route）→
Redis 键池取样（latest 版本空间）→ 起压前命中自检 → 预热＋梯度档位（docker stats
同步采样、逐档审计基线/终值）→ 静置探针 → 审计守恒对账 → 自动生成
`performance-report.md`＋`performance-report.html`（同一数据渲染双格式）。
**新增接口零开发直接测**——只要服务在登记里（有 KV namespace）就能压。

## 一行用法

```bash
ONESERVE_APPKEY=sb_xxx bash loadtest/run_ladder.sh --service v1-svc-usr-asset-tag \
    [--ladder 100,300,500,800] [--force]
（等价：--appkey sb_xxx 或 --secret /path/to/cred.json 传凭证，见下节）
```

跑完看：`loadtest/results/<时间戳>-<服务短名>/performance-report.md`（同目录
`.html`＋全部实测产物）。

## 凭证传递（P1-6）

- **首选环境变量 `ONESERVE_APPKEY`**——不进 shell history、不进 ps 参数表；
- `--appkey` 参数保留，但**会进 shell history 与 ps 输出**，只建议一次性临时用；
- `--secret`＝凭证 JSON 文件（取 `appkey` 字段，如控制台签发回执）：secret 本身不参与
  调用（网关 key-auth 只验 `apikey` 头），明文只经内存、不落盘；
- 都不提供则交互输入（不回显）。产物只存掩码（`sb_xxxx****xxxx`）。

## 前置门禁（P0-1/P0-2，起压前硬校验）

| 检查 | 逻辑 | 不过怎么办 |
|---|---|---|
| 授权校验 | `service/registry/appkeys.yaml` 中该 appkey 的 `grants` 须含目标 `--service` | EXIT 10「该凭证未授权服务 X，请在凭证页签发/授权」 |
| 配额档位 | `quota_qps` 低于梯子最低档位 → 429 熔断失真风险 | EXIT 11 警告「请管理员调高配额或换压测专用凭证」；确认要测限流行为则加 `--force` |
| latest 绑定 | 键池只从 `tags:<ns>:latest` 指向的现役版本空间 SCAN（禁止跨版本/跨表回落） | latest 缺失即 EXIT 2 拒跑（空投影假达标风险） |
| 命中自检 | 起压前抽 `PROBE_KEYS`（缺省 5）键真查：须全部 HTTP 200 且 `found=true` | EXIT 3 拒跑（键池与目标表错配） |

EXIT 码速查：`10`=未授权｜`11`=低配额未 --force｜`3`=命中自检失败｜`2`=参数/环境错误。

## 参数表

| 参数 | 缺省 | 说明 |
|---|---|---|
| `--service` | （必填） | service_id（`v1-svc-usr-asset-tag`）或 route（`/v1/svc/usr-asset-tag`）；优先 console_services 区命中（P1-4，route 直取登记值） |
| `--appkey` | — | 调用方凭证（会进 shell history；推荐 `ONESERVE_APPKEY` 环境变量） |
| `--secret` | — | 凭证 JSON 文件（取 `appkey` 字段，如控制台签发回执；secret 不参与调用、不落盘） |
| `--ladder` | `100,300,500,800` | 梯度档位（csv）；冒烟/低配可 `--ladder 20` |
| `--force` | — | quota_qps 低于最低档位时强制放行（meta.gate.forced=true，报告如实展示；429 单独计数） |
| `--keys` | `2000` | 键池取样上限（Redis SCAN 自动取样，池小于上限全取） |
| `--secs` / `--ramp` | `70` / `10` | 单档窗口＝起压 ramp＋全并发（secs−ramp）；70＝10s 起压＋60s 全并发 |
| `--warmup-users` / `--warmup-secs` | `50` / `30` | 预热（不计入正式统计）；低配额凭证可调小 |
| `--probe-wait` | `30` | 收尾静置秒数，之后 5 发单请求探针 |
| `--host` | `http://localhost:9080` | 被测网关地址 |
| 环境变量 | — | `ONESERVE_APPKEY`（凭证首选）；`LOADTEST_APIXISX_CONTAINER`/`LOADTEST_SERVICE_CONTAINER`/`LOADTEST_REDIS_CONTAINER`（缺省 `oneserve-*`）；`LOADTEST_PROBE_KEYS`（命中自检键数，缺省 5）；各 `--` 参数均有 `LOADTEST_*` 等名缺省 |

## 自动流程（run_ladder.sh 编排）

1. **发现＋门禁**（`discover.py`，只读）：服务登记解析 route/method/入参键字段/KV
   namespace（console_services 优先）；grants 授权校验＋quota_qps 档位比对（EXIT
   10/11）；`tags:<ns>:latest` 解析现役版本 → SCAN `tags:<ns>:<ver>:*` 取样实体键池
   （64hex 归一小写＝存储约定，非 hex 键原样保留；写 `keys.txt`）→ `meta.json`。
2. **起压前命中自检**：抽 `PROBE_KEYS` 键逐一真查，须全部 200＋`found=true`
   （防跨表键池在授权投影下全 miss＝空投影假达标；非 100% 即 EXIT 3）。
3. **预热**＋**梯子逐档**（`run_level.sh`）：每档＝审计基线 → docker stats 采样起 →
   locust（起压＋全并发）→ 采样停 → 审计终值 → 对账行。
4. **静置探针**：静置后 5 发单请求计时（瓶颈恢复佐证）。
5. **审计对账**（`audit_check.py`）：七元组完整性＋seq 单调＋窗口内 endpoint/caller
   归属（endpoint＝`service:/v1/query/<service_id>`——与审计落盘实测口径一致，
   main.py:385；caller＝本次 AppKey，均从 --service 与凭证派生）→ `audit_recon.json`。
6. **报告**（`report_gen.py`）：读本梯子产物生成 `performance-report.md`＋
   `performance-report.html`（同一数据渲染；HTML 门户 tokens 配色、自包含单文件，
   为版本管理式页面铺路）；`summarize.py` 同时落 `ladder_summary.csv`（CSV 能力保留）。

## 报告（模板固定：置顶声明＋六节）

**置顶声明（MD/HTML 同文）**：

- 共享栈压测声明（P0-3）：本机 M0 无隔离栈，结果含门户/管理流量干扰，尾部延迟供参考；
- 延迟双口径声明（P1-7）：P50/P95/P99 唯一判定口径＝locust 客户端实测；审计
  `latency_ms` 实测样本为 0（整数毫秒精度损失，不可信），不引用。

| 节 | 内容 |
|---|---|
| 1 环境与被测 | 服务/地址/凭证掩码（secret 不落盘）/配额/**授权门禁结论**/**键池来源（namespace/版本 latest/取样数/SCAN pattern）**/表数据量/时序口径 |
| 2 结果表 | 档位/请求数/错误数/错误率/RPS 均值与尾部/P50/P95/P99/审计 delta（09-21 报表列口径）＋错误构成（按状态码，429 单列） |
| 3 容器资源表 | apisix/service/redis CPU 均值峰值＋service 内存峰值（docker stats 2s 采样） |
| 4 审计守恒对账表 | 各档窗口内 2xx/非 2xx/endpoint/caller 异常＋客户端口径差值＋判定 |
| 5 静置探针 | 静置后单发计时（无并发基线） |
| 6 结论与建议 | AC「点查 P99<200ms」逐档 ✓/✗ 自动判定＋瓶颈观察（限流/CPU 饱和/内存）＋一句话建议 |

样例（单档低并发、被网关限流整形的口径）：

```markdown
| 档位 | 请求数 | 错误数 | 错误率 | RPS(均值) | RPS(尾部) | P50ms | P95ms | P99ms | 审计delta |
|---|---|---|---|---|---|---|---|---|---|
| 20 | 420 | 120 | 28.57% | 6.0 | 5.9 | 3.1 | 8.2 | 12.5 | 300 |

| 档位 | P99ms | P99<200ms | 错误率 | 判定 |
|---|---|---|---|---|
| 20 | 12.5 | ✓ | 28.57% | ✗ 未达标 |
```

## 前置与起栈

- 前置：目标栈已在跑（`oneserve-apisix`/`oneserve-service`/`oneserve-redis` 三容器，
  网关 9080）；PATH 含 docker 与 python3.13（脚本自会前置
  `/Library/Frameworks/Python.framework/Versions/3.13/bin` 与 Docker 路径）。
- **起栈**：当前共享演示栈＝仓库根 `bash scripts/up.sh`（项目名 `oneserve`）。
  历史上压测用独立栈：`COMPOSE_PROJECT_NAME=oneserve-load bash scripts/up.sh`——
  compose 文件固定 `container_name: oneserve-*`，同机只能起一套，独立栈需先停共享栈；
  工具本身不依赖项目名（docker exec 直连容器名）。
- 键池数据：服务命名空间需有数据（`bash scripts/ecs_seed.sh` 造数或发布台装载）。

## 文件

| 文件 | 作用 |
|---|---|
| `run_ladder.sh` | 参数化编排：门禁→发现→命中自检→预热→梯子→静置探针→对账→报告 MD＋HTML（本 README 用法入口） |
| `run_level.sh` | 单档执行器（审计基线→stats 采样→locust→审计终值→对账行）；目标/凭证/结果目录由环境注入 |
| `locustfile.py` | 负载画像：目标全走环境变量（`LOCUST_ROUTE/APIKEY/KEYS_FILE`＋键字段/方法），无旧硬编码 |
| `discover.py` | 授权/配额门禁＋服务登记解析（console_services 优先）＋latest 版本空间键池取样（只读；写 `keys.txt`/`meta.json`） |
| `audit_check.py` | 审计七元组完整性＋守恒对账（endpoint/caller 从 --service 与凭证派生）→ `audit_recon.json` |
| `summarize.py` | 汇总 `ladder_summary.csv`＋Markdown 表（CSV 能力保留；容器名后缀归位；错误数以 summary.log Error report 为权威） |
| `report_gen.py` | 报告生成器：读 run 目录产物 → `performance-report.md`＋`.html`（同一数据渲染；置顶声明＋六节固定模板） |
| `test_loadtest_tools.py` | 纯函数判据（15 例：门禁/解析/键池/渲染/安全面；放 loadtest/ 不入门禁，避免 docker 耦合进 CI） |
| `results/<时间戳>-<服务短名>/` | 一次梯子全部产物（键池/meta/locust CSV/summary/stats/对账/探针/报告） |

## 口径说明

- 每档窗口缺省 70s＝10s 起压＋60s 全并发（满足 AC「每档持续 ≥60 秒」）；RPS 为窗口
  均值，另给尾部 RPS（stats history 末 5 个时间片均值）看衰减。
- 总数/百分位＝locust 退出终表（`_stats.csv` 是提前 ~0.8s 的快照，不用）；错误数＝
  summary.log Error report 次数合计（缺段回落 failures.csv Occurrences）。
- **延迟百分位唯一口径＝locust 客户端实测**；审计 `latency_ms` 实测样本为 0（整数
  毫秒精度损失），不引用（P1-7）。
- **429＝网关限流**（凭证 quota_qps/quota_daily 与并发闸，APISIX limit-count/limit-conn）：
  被拒请求不经服务端、不进审计——对账差值的正当来源；quota 偏低时先调配额再测容量
  （门禁会拦，--force 仅用于有意测限流行为）。
- 客户端超时 30s：服务端 guard 3s，超过即客户端口径失败；服务端可能仍处理并审计，
  亦属对账差值来源，报告 §4 逐档显式列出。
- 凭证安全：appkey 只在进程内存/环境变量流转，产物只存掩码（`sb_xxxx****xxxx`）；
  secret 不参与调用、不落任何入库文件。
- docker stats 采样有少量开销，属本机环境局限（单机自压自测，网络含本机回环）。
- **共享栈污染面**（对抗复核 P0-3）：本机 M0 无隔离栈，portal/admin/mock-yibao 与
  压测共用 Redis/网关/service——尾部延迟含他人流量干扰，报告置顶声明，数字只作
  趋势与回归口径，不作容量结论。

## 复现（全流程）

```bash
bash scripts/up.sh && bash scripts/ecs_seed.sh        # 起栈＋造数（已有则跳过）
ONESERVE_APPKEY=sb_xxx bash loadtest/run_ladder.sh --service <service_id>   # 梯子＋报告
open loadtest/results/<ts>-<短名>/performance-report.html
```

旧式手工链（discover→summarize→audit_check→report_gen 也可分步直跑，
参数见各文件头注）。
