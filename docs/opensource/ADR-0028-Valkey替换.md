# ADR-0028：KV 镜像由 Redis 替换为 Valkey

- 状态：**已批准（维护者 2026-10-28「按你推荐」）·已实施（compose＋seed 自测镜像行）**
- 日期：2026-10-28
- 决策者：项目维护者
- 影响面：`docker-compose.yml` / `compose.prod.yml` 的 KV 服务、`NOTICE` 第 3 节与第 5 节、开源发布的许可面
- 前置阅读：`NOTICE`

---

## 1. 背景

本项目的 KV 热存储服务在 compose 中写作：

```yaml
redis:
  image: redis:7-alpine
```

该写法有两个问题，且都属于**开源分发**层面的硬问题，不是运行时问题：

1. **许可是浮动 tag 的许可，不是我们声明的许可。** `7-alpine` 是浮动 tag，上游 Redis 自
   **7.4 版（2024 年 7 月）**起将该版本及其后的发布改为 **RSALv2 与 SSPLv1 双许可**，
   两者均**不是 OSI 批准的开源许可**。也就是说，同一个 tag 在不同时间拉取，可能落到
   7.2（BSD-3-Clause）也可能落到 7.4+（非 OSI），**分发物的许可随拉取时间漂移**。
2. **与本项目的分发声明冲突。** 本项目以 Apache-2.0 分发。一份 Apache-2.0 的发布物
   内部打包了非 OSI 许可的组件，会让下游再分发者、企业内部合规审查和包管理器收录
   全部卡住；在 `NOTICE` 里"仅声明"并不消除这一冲突。

此外，本项目的使用形态只用到 KV 的一个很窄的子集：`GET`/`SET`/`DEL` 类点查、
`noeviction` 内存策略、AOF `everysec` 持久化、管道与少量统计命令。没有用到任何
Redis 7.4+ 的新特性，因此**替换的兼容成本极低**。

## 2. 决策

将 KV 服务镜像替换为 **`valkey/valkey:8-alpine`**（BSD-3-Clause，Linux 基金会托管，
由 Redis 7.2 分支 fork 而来，RESP 协议与命令集兼容）。

改动量为 compose 中的**一行**，配置参数不变：

```yaml
redis:
  image: valkey/valkey:8-alpine
  command:
    - valkey-server
    - --maxmemory-policy
    - noeviction
    - --appendonly
    - "yes"
    - --appendfsync
    - everysec
```

配套不变项：

- Python 客户端继续使用 `redis`（redis-py），版本不变——它按 RESP 协议通信，与 Valkey 兼容；
- 卷挂载点仍为 `/data`，排障命令由 `redis-cli` 改为 `valkey-cli`（镜像内自带，兼容）；
- 健康检查仍为 `PING` 应答判定。

## 3. 备选方案与未采纳理由

| 备选 | 未采纳理由 |
|---|---|
| **钉住 `redis:7.2-alpine`**（最后一个 BSD-3 版本） | 许可干净，但 7.2 分支只接收有限维护，新披露的安全问题不再有修复版本；把非 OSI 之后的版本全部放弃，等于长期背一个不再修缮的组件。作为**回退**方案保留（见第 5 节），不作为目标态。 |
| **保留 7.4+ 并在 NOTICE 声明** | 不解决冲突。下游再分发者拿到的是包含非 OSI 组件的聚合体，Apache-2.0 的对外承诺无法覆盖它。 |
| **换 Dragonfly / KeyDB 等其它 KV** | 许可与成熟度需单独评估，且本项目并未遇到 Redis/Valkey 的能力瓶颈——为替换而替换，收益不足。 |
| **换托管云 KV 服务** | 违反本项目"一条命令本地起栈"的 quickstart 目标，且引入外部依赖与费用。 |

## 4. 影响

1. **NOTICE 联动（必须同批完成）**：`NOTICE` 第 3 节镜像表把 `redis:7-alpine` 一行改为
   `valkey/valkey:8-alpine`（BSD-3-Clause），并删除第 5 节"拟替换"占位段落；否则 NOTICE
   会与实际分发的镜像不一致。
2. **镜像源同批收敛**：`bitnamilegacy` 命名空间下的 etcd 镜像已停止更新，与本次替换
   同批处理——改用 etcd 官方镜像。**具体选型（`quay.io/coreos/etcd` 或
   `gcr.io/etcd-development/etcd`）不在此预判**，由施工时按目标架构（arm64/amd64）的
   tag 可用性实测确定，避免把未验证的结论写进决策。
3. **数据兼容需实测**：Valkey 8 与 Redis 7.2 的 RDB 格式大体兼容，但"大体"不等于"验证过"。
   升级路径二选一，均须实测后写结论：
   - 复用既有数据卷直接启动（读取既有 RDB/AOF）；
   - 备份卷后空卷启动，用既有导出/灌数脚本重建数据。
4. **多架构覆盖需实测**：本项目开发机为 arm64、部署目标含 amd64，须确认目标 tag 为多架构
   manifest；不满足则改用可用的次新 tag，不得回退到 Redis。
5. **运维面**：排障命令名变化（`valkey-cli`），运维手册与脚本中的 `redis-cli` 用法需同步；
   服务名、容器名、卷名、环境变量名（`REDIS_URL` 等）**保持不变**，避免无关改动扩散。
6. **不自称"Redis"**：对外文档统一表述为"KV 存储（Valkey）"，不把 Valkey 表述为 Redis 的
   官方发行版，尊重上游商标。

## 5. 回退方案

回退**以镜像 tag 为单位**，但回退不等于"改回一行"就完事，存在数据方向性风险：

- **触发条件**（任一命中即回退）：`PING` 或健康检查不通过；KV 查询链路返回错误或结果
  不一致；AOF 重放失败；目标架构无可用 tag；压测 P95 明显劣化（阈值为估算口径，须在
  施工卡的压测报告中给出具体数字后再定）。
- **回退动作**：compose 中镜像行改回 `redis:7.2-alpine`（**显式钉 7.2，不用浮动
  `7-alpine`**，以免再次落到非 OSI 版本），配置参数不变；如已用 Valkey 写过数据，
  **先恢复升级前的卷备份**再启动——Valkey 8 写出的 RDB 不保证能被 Redis 7.2 读取。
- **回退后必须做**：重新执行第 6 节的验收命令，并在 NOTICE 中同步镜像许可（此时 Redis
  为 BSD-3-Clause，因其为 7.2）。

## 6. 验收（可执行）

替换施工完成后，逐条跑通并留下输出：

```bash
# 1) KV 服务起来且健康
docker compose ps redis                 # STATUS 应为 healthy

# 2) 网关鉴权行为不变：无 key 401、带 key 200
curl -i http://localhost:9080/v1/ping   # 期望 401
curl -i -H "X-API-Key: <dev-key>" http://localhost:9080/v1/ping   # 期望 200

# 3) 标签查询链路出数据（返回 JSON）
curl -s -H "X-API-Key: <dev-key>" "http://localhost:9080/v1/tags/<service_id>?<查询参数>"
```

附加检查：`AOF` 目录在重启后仍能重放；`noeviction` 在内存打满时表现为**拒写**而非驱逐
（可用小 `maxmemory` 复现）。

## 7. 未完成事项（施工卡口径）

- [ ] compose 两文件镜像行替换（dev 与 prod）
- [ ] NOTICE 第 3/5 节联动更新
- [ ] 数据卷兼容实测（二选一路径，留记录）
- [ ] 多架构 tag 确认
- [ ] 第 6 节验收命令输出留档
- [ ] etcd 官方镜像同批替换（选型实测后定）
