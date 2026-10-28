# testkit/ —— OneServe 测试演示套件

一套执行内核、两种观众：**演示模式**（终端实时面板）＋**回归模式**（headless 梯子→基线 JSON→对比报告）。
画像与口径全部移植 loadtest/（不 import 平台代码，交互面＝网关 HTTP 黑盒＋键池文件）；SLA 口径锚定 SPEC §6.1（500 QPS 吞吐口径；50 并发档 P50=45ms/P99≈60ms 为达标背书；梯子档位为容量参考）。

## 两条命令起步

```bash
# 演示模式（S1 并发爬坡面板；无栈也能起——目标不可达自动降级 MOCK 并显式标注）
python3 testkit/run.py s1 --mock

# 回归模式（S0 冒烟门＋预热＋100/300/500 档 70s→run.json→对基线出绿黄红报告）
python3 testkit/run.py s4
```

依赖：locust（已装）＋rich（勿装全局：`pip3 install --target testkit/lib rich`；缺 rich 自动降级 ANSI 单行，再不行 `--only-summary`）。

## 场景与参数

| 命令 | 场景 | 一句话 | 常用参数 |
|---|---|---|---|
| `s0` | 冒烟 | 5 用户×30s，全 200 且 P99<500ms 即绿（栈探针/回归门） | `--secs --host --mock` |
| `s1` | 并发爬坡演示 | 50→100→300 档，面板数字滚动（演示窗每档 30s，正式口径以 s4 70s 窗为准） | `--levels 50,100 --secs 30` |
| `s2` | 三通道对比 | HTTP API / Python SDK(客户端 SM3) / 网页通道 各一列（run.json `channels`） | `--secs` |
| `s3` | 限流演示 | A 正常配额＋B 超限双组，面板分列 200/限流计数（B 被限、A 无感） | `--quota 150`（默认待定） `--secs` |
| `s4` | 回归梯子 | 冒烟门→预热 50×30s→100/300/500 档（10s 起压＋60s 全并发）→对比报告 | `--levels 100,300,500,800 --skip-smoke` |
| `s5` | 批量场景 | 提交名单→轮询 succeeded→下载校验（双 sha256/投影/脱敏）→取消 running（cancelling→cancelled）→429 并发拒（AC-10） | 真栈专用（需批量链路；机检清单落 checks.txt） |
| `s6` | M2 端到端＋冷热加载 | HBase 冷→加载→Redis 热（对账差异=0）→幂等→切指针→经网关真调（投影白名单/脱敏）→HBase 不可达如实 503＋零写入 | 隔离 M2 栈专用（`scripts/m212_testkit_e2e.sh` 起环境；六步机检落 checks.txt） |
| `convert` | 首版基线转换 | loadtest/results CSV→基线 JSON（数据复用不重跑） | `--from-loadtest loadtest/results --sample-report` |
| `compare` | 手动对比 | run JSON × 基线 JSON→报告 | `--run X --baseline Y` |
| `selftest` | 内部自检 | SM3 国标向量／百分位／阈值判定 | — |

通用参数：`--mock`（强制模拟网关）`--host` `--keyfile` `--env-name`（基线隔离键）`--live|--headless|--only-summary`（降级链）`--debug-web`（locust web UI 调试后备，127.0.0.1:8089）。配置单一来源＝`config.example.toml`（复制为 `config.toml` 本地覆盖）。

## s6：M2 端到端＋冷热加载（隔离环境）

架构口径＝**HBase 冷存储 / Redis 热存储**（ADR-0005）：链路＝HBase 选表 → 加载到 Redis → 从 Redis 发 API。
s6 把 M2 新能力（加载端点／同源对账／双缓冲切指针）纳入回归体系，六步机检：

1. **选表**：GET `/v1/admin/publish/hbase/discovery`（真 HBase meta × 字段字典双源）；
2. **加载＋对账**：POST `/v1/admin/publish/hbase/load`（HBase 冷 → Redis 热）→ `reconcile.pass` 且
   `diffCount=0`（行数双向全等＋抽样逐值全等＋覆盖率达标）＝**对账差异=0 判定**；
3. **幂等**：重跑不带 replace → 409 `LOAD_CONFLICT`（零副作用）；带 replace → 清空重写、行数稳定；
4. **上线真件**：执行既有 `sync/reconcile.py`（源指纹＋三查）→ `sync/switch.py`（对账三闸）原子切 `tags:user:latest`；
5. **经网关真调**：`route`（→ `/v1/tags/query`）→ 200／found／`live_version`／`data_as_of`；
   **投影白名单**（越权字段零出现）＋**脱敏**（`if_acct_fct_24` ∈ A/B/C/U，CSV 原值零出现）在同链路上仍成立；
6. **失败态**：`docker rm -f` HBase → 加载端点如实 503 `HBASE_UNAVAILABLE` 且目标空间**零写入**（不写脏、不回退假数据）。

起隔离环境（容器名一律 `oneserve-m212-*`，Redis 一次性容器零 named volume；共享 APISIX 只加临时路由，验后即删）：

```bash
bash scripts/m212_testkit_e2e.sh            # 起隔离栈 → 跑 s6 → 落证据 tests/evidence/m212-*
python3 testkit/run.py s6 --svc http://127.0.0.1:18014 --gw http://127.0.0.1:9080 \
    --route /v1/m212/query --version <YYYYMMDD> --source-csv sync/out/src_m212_<YYYYMMDD>.csv \
    --redis-url redis://127.0.0.1:16390/0 --hbase-ct oneserve-m212-hbase   # 环境已起时单跑
```

**如实标注**：真集群（NC-M2-9）未做——s6 跑在开发替身容器（compose.hbase.yml）上；真集群首灌/生产加载未验。

## 回归基线机制

- 基线＝`baselines/<env>-<YYYYMMDD>.json`（schema `oneserve.testkit.baseline/1`），按 env.stage 隔离，**只比同环境迁移前后**；首版由 `convert` 从 loadtest/results（2026-09-20 造数·09-21 实测）转换。
- 判定（首版估算，跑 2~3 轮实测后校准入 config）：RPS 降 >10% 黄 / >25% 红；P50/P95/P99 升 >20% 黄 / >50% 红；错误率 >0.1% 红。
- 全绿只写 `<env>-<date>-candidate.json` 并提示，**人工确认后才转正**，不自动覆盖；环境指纹（栈 rev/压测机/网段）变更在报告高亮，防「换机器当回退」。

## 运行边界

演示机/跳板机，不进生产、不压真实合作方链路；只用演示栈占位 appkey（同 loadtest 现状）。产物落 `reports/<ts>-<场景>/`（run.json＋report.md，S1/S3 另存 panel.svg 面板留证）。
