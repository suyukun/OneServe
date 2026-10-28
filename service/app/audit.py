"""审计八元组：结构定义＋AuditSink 抽象＋本地 JSONL/Kafka 双模管线。

SPEC §3.2 步骤 5 / §5④ / D20：审计七元组经 AuditSink 抽象投递——换实现不改业务码。
P0-5 traceId 贯通：在原七元组之外新增第八列 trace_id（SPEC §4.5，原七列不动；
存量行无 trace_id——全部读取按 key 取值，缺列天然容错，等价空串口径）。

管线模式（环境变量 AUDIT_PIPELINE，缺省 local 保现状）：
  local  —— 纯本地 JSONL＋WAL 兜底（M1 过渡存储，原有行为不变）；
  kafka  —— KafkaSink 直投 Kafka（topic=oneserve-audit），Kafka 不可达时自动降级
            回本地 JSONL＋WAL（SPEC 「不强依赖 Kafka 常驻」纪律）；
  dual   —— 双写验证模式：JSONL 与 Kafka 同时写入，用于切换前对账验证。
            对账一致后才允许切 single（kafka）——SPEC §5④ 双写切换纪律。

本地 JSONL 链路（local/dual 均走）严格写前日志（Write-Ahead Log）：
    写入路径：先追加 WAL → 再追加 JSONL → 成功后清理 WAL；
    启动恢复：重放 WAL 中 JSONL 尚缺的记录（按 seq 对齐）→ 清空 WAL。

异步刷盘（SLA试测_v1 §2-② M1 项：同步 fsync ~0.9ms/请求，吞吐封顶 ~1.09k QPS）：
    逐条 fsync 改为「后台刷盘线程＋可配置间隔（默认 200ms）＋退出/异常时兜底 flush」。
    write()/flush()（写入内核页缓存，微秒级）仍逐条内联完成，仅 os.fsync（毫秒级）异步批量执行：

      - 进程被杀（SIGKILL 也一样）零丢失语义不变——数据已 write 进内核页缓存，
        与是否 fsync 无关；已进 JSONL 的直接还在，只进 WAL 的由 recover() 补齐。
      - 掉电/内核崩溃的持久化窗口由 0 变为 ≤ flush_interval（默认 200ms）；
        close()/atexit 退出时与 append 异常时会做兜底 fsync。
      - flush_interval<=0 时退回旧「逐条 fsync」同步口径（掉电级持久的兜底开关）。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("oneserve.service")

# ---------- 管线模式（AUDIT_PIPELINE 环境变量，缺省 local 保现状）----------
# local  = 纯本地 JSONL+WAL（M1 过渡存储，原有行为不变）
# kafka  = KafkaSink 直投（Kafka 不可达时自动降级回 local）
# dual   = 双写验证（JSONL + Kafka 同时写，对账一致后才允许切 kafka）
PIPELINE_MODE = os.environ.get("AUDIT_PIPELINE", "local").strip().lower()
if PIPELINE_MODE not in ("local", "kafka", "dual"):
    logger.warning("AUDIT_PIPELINE=%r 非法，回退 local", PIPELINE_MODE)
    PIPELINE_MODE = "local"

# Kafka 配置（kafka/dual 模式使用；bootstrap 用 127.0.0.1 避免 IPv6 ::1 怪行为）
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092")
KAFKA_TOPIC = os.environ.get("KAFKA_AUDIT_TOPIC", "oneserve-audit")

# 审计文件名（落 AUDIT_DIR，容器内挂 named volume 持久化）
JSONL_NAME = "audit.jsonl"
WAL_NAME = "audit.wal"

DEFAULT_FLUSH_INTERVAL = 0.2  # 后台刷盘间隔（秒），即任务书要求的默认 200ms


@dataclass(frozen=True)
class AuditEntry:
    """审计八元组（SPEC §3.2/§3.3：hash_only 参数＋响应摘要口径；§4.5 trace_id）。

    业务字段固定如下（顺序即元组顺序；①-⑦为原七元组，原口径不动）：
      ① ts               请求时间戳（UTC，ISO8601）
      ② caller           调用方标识（网关认证后的 appkey）
      ③ endpoint         服务端点（如 service:/v1/ping）
      ④ params_digest    参数摘要 sha256（不落明文参数）
      ⑤ status           结果状态（HTTP 状态码）
      ⑥ latency_ms       服务处理时延（毫秒）
      ⑦ response_digest  响应体摘要 sha256（脱敏后口径）
      ⑧ trace_id         全链路追踪标识（P0-5：网关 request-id 透传；缺省空串）
    seq 为内部单调序号（WAL/JSONL 对齐用，非业务元组成员）。
    存量兼容：旧七列 JSONL 行无 trace_id 键——本模块读取一律按 key 取值，
    缺列不报错（等价空串），读写两侧均无需迁移。
    """

    ts: str
    caller: str
    endpoint: str
    params_digest: str
    status: int
    latency_ms: int
    response_digest: str
    trace_id: str = ""
    seq: int = field(default=0)

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def digest_sha256(text: str) -> str:
    """sha256 摘要（十六进制小写）。"""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class KafkaSink:
    """Kafka 审计 Sink（kafka-python 3.x producer，topic=oneserve-audit）。

    SPEC §5④/D20 AuditSink 抽象的 Kafka 实现：七元组 JSON＋traceId 序列化后
    以 bytes 投递（kafka-python 3.x 新 Serializer 接口不建议 lambda，直接收发 bytes）。
    bootstrap 用 127.0.0.1（踩坑实录：localhost 先解析 ::1，行为怪异）。

    Kafka 不可达时 ready()=False / send()=False，AuditWriter 自动降级回本地
    JSONL+WAL 兜底——SPEC「不强依赖 Kafka 常驻」纪律落地。

    延迟导入 kafka-python：local 模式完全不加载该依赖（零侵入既有部署）。
    """

    def __init__(self, bootstrap_servers: str = "", topic: str = ""):
        self._bootstrap = bootstrap_servers or KAFKA_BOOTSTRAP
        self._topic = topic or KAFKA_TOPIC
        self._producer = None
        self._ready = False
        self._init_error: str | None = None
        self._try_connect()

    def _try_connect(self) -> None:
        """尝试建 KafkaProducer；失败记错误但不抛（降级语义）。"""
        try:
            from kafka import KafkaProducer  # 延迟导入：local 模式不依赖 kafka-python
            self._producer = KafkaProducer(
                bootstrap_servers=self._bootstrap,
                # 不设 value_serializer（kafka-python 3.x DeprecationWarning）；
                # 直接发 bytes，序列化在 send() 内完成。
                acks="all",                    # 金融级不可丢：等全部 ISR 确认
                retries=3,                     # 瞬态故障重试
                max_block_ms=3000,             # 3s 超时（不阻塞业务线程过久）
                request_timeout_ms=3000,
                metadata_max_age_ms=30000,     # 30s 刷新元数据
            )
            # 验证连通性：获取 topic 元数据（auto-create 关闭时，topic 不存在会报错）
            partitions = self._producer.partitions_for(self._topic)
            if partitions is None:
                raise RuntimeError(f"topic {self._topic} 不存在（auto-create 已关闭，须显式创建）")
            self._ready = True
            self._init_error = None
            logger.info("KafkaSink 就绪：bootstrap=%s topic=%s partitions=%d",
                        self._bootstrap, self._topic, len(partitions))
        except Exception as exc:  # noqa: BLE001
            self._ready = False
            self._init_error = str(exc)
            self._producer = None
            logger.warning("KafkaSink 连接失败（将降级回本地 JSONL）：%s", exc)

    def ready(self) -> bool:
        return self._ready

    def send(self, entry: AuditEntry) -> bool:
        """投递一条审计到 Kafka；失败返回 False（调用方降级走本地兜底）。"""
        if not self._ready or self._producer is None:
            return False
        try:
            payload = entry.to_json_line().encode("utf-8")
            future = self._producer.send(self._topic, value=payload)
            future.get(timeout=5)  # 同步等待确认（acks=all 保证持久化）
            return True
        except Exception:  # noqa: BLE001
            logger.warning("KafkaSink send 失败（降级走本地兜底）", exc_info=True)
            return False

    def close(self) -> None:
        """关闭 producer（flush 未发消息＋释放连接）。"""
        if self._producer is not None:
            try:
                self._producer.flush(timeout=10)
                self._producer.close(timeout=10)
            except Exception:  # noqa: BLE001
                logger.warning("KafkaSink close 异常", exc_info=True)
            self._producer = None
            self._ready = False


class LocalSink:
    """本地 JSONL Sink 标识（pipeline_mode=local 时的占位，无外部依赖）。

    实际写入逻辑在 AuditWriter.append 内联（WAL→JSONL），此类仅作为
    sink 参数的类型兼容占位，使 AuditWriter.__init__ 签名统一。
    """

    def ready(self) -> bool:
        return False  # LocalSink 永远不「就绪」→ append 走本地 WAL+JSONL 路径

    def send(self, entry: AuditEntry) -> bool:
        return False  # 同上

    def close(self) -> None:
        pass


class AuditWriter:
    """本地审计写入器：WAL 先行、JSONL 为主、启动恢复、fsync 异步批量。

    flush_interval 语义：
      > 0（默认 0.2s）：异步模式——后台守护线程按间隔批量 fsync（性能路径）；
      <= 0          ：同步模式——append 内逐条 fsync（旧「掉电级持久」口径的兜底开关）。
    """

    def __init__(self, audit_dir: str, sink: KafkaSink | None = None,
                 flush_interval: float = DEFAULT_FLUSH_INTERVAL,
                 pipeline_mode: str = ""):
        self.dir = Path(audit_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.dir / JSONL_NAME
        self.wal = self.dir / WAL_NAME
        # 管线模式：显式传入 > 环境变量 > 缺省 local
        self.pipeline_mode = (pipeline_mode or PIPELINE_MODE).strip().lower()
        if self.pipeline_mode not in ("local", "kafka", "dual"):
            self.pipeline_mode = "local"
        # Sink 装配：kafka/dual 模式用 KafkaSink（可能降级）；local 用 LocalSink
        if sink is not None:
            self.sink = sink
        elif self.pipeline_mode in ("kafka", "dual"):
            self.sink = KafkaSink()
        else:
            self.sink = LocalSink()
        self._lock = threading.Lock()  # 串行化写路径，保证 WAL/JSONL 顺序一致
        self._seq: int | None = None   # 内存序号（惰性扫描一次后自增；task-005 压测修复）
        self._flush_interval = float(flush_interval)
        self._sync_mode = self._flush_interval <= 0
        self._closed = False
        self._dirty = False            # 有未 fsync 的脏数据（GIL 下赋值原子，无需另加锁）

        # 持久句柄：消除逐条 open/close 的 ~3 次文件开合（压测实测串行 I/O 的一部分）；
        # "a" 追加模式＋每次写后 flush()（write 系统调用，入内核页缓存，立即可见）
        self._wal_fh = open(self.wal, "a", encoding="utf-8")
        self._jsonl_fh = open(self.jsonl, "a", encoding="utf-8")

        # 异步模式：起后台刷盘线程＋注册退出兜底（close 幂等，atexit 重复调用无害）
        self._flusher: threading.Thread | None = None
        self._stop = threading.Event()
        if not self._sync_mode:
            self._flusher = threading.Thread(
                target=self._flush_loop, name="audit-wal-flusher", daemon=True)
            self._flusher.start()
        self._atexit_reg = atexit.register(self.close)

    # ---------- 后台刷盘（异步模式） ----------

    def _flush_loop(self) -> None:
        """后台刷盘线程：每 flush_interval 醒一次，把脏数据 fsync 到物理盘。

        纯间隔轮询（不在 append 时唤醒）：保证批量合并，避免退化为逐条 fsync。
        """
        while not self._stop.wait(self._flush_interval):
            self._fsync_dirty()

    def _fsync_dirty(self) -> None:
        """把仍在页缓存的 WAL/JSONL 内容 fsync 落物理盘（无脏数据则空转）。

        先清 _dirty 再 fsync：fsync 期间新到的 append 会重新置脏，由下一轮兜住，
        不会丢（最坏延迟一个间隔＋close() 的最终 fsync）。
        """
        if not self._dirty or self._closed:
            return
        self._dirty = False
        os.fsync(self._wal_fh.fileno())
        os.fsync(self._jsonl_fh.fileno())

    def close(self) -> None:
        """退出/异常兜底：停后台线程 → 最终 fsync → 关句柄 → 关 KafkaSink。幂等。

        持锁与 append 串行，保证最终 fsync 覆盖全部已写入记录。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()  # 唤醒刷盘线程使其退出
            if self._flusher is not None:
                self._flusher.join(timeout=5)
            try:
                # 最终兜底刷盘：无论同步/异步模式，退出前把页缓存内容落物理盘
                os.fsync(self._wal_fh.fileno())
                os.fsync(self._jsonl_fh.fileno())
            finally:
                self._wal_fh.close()
                self._jsonl_fh.close()
                atexit.unregister(self._atexit_reg)
            # 关闭 KafkaSink（flush 未发消息＋释放连接）
            if hasattr(self.sink, "close"):
                self.sink.close()

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("AuditWriter 已 close，不能继续写入/恢复")

    # ---------- 读辅助（测试/验证用） ----------

    def _read_seq_set(self, path: Path) -> set[int]:
        if not path.exists():
            return set()
        seqs: set[int] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                seqs.add(json.loads(line)["seq"])
            except (json.JSONDecodeError, KeyError):
                continue  # 半行（理论不会出现：逐条整行写）忽略
        return seqs

    def _next_seq(self) -> int:
        # task-005 压测修复：原先每次 append 全量重读 JSONL/WAL 逐行解析取 max+1，
        # O(N)/请求且锁内串行阻塞事件循环（100 并发实测 P50 1.1s、单核 90 QPS 封顶）。
        # 改为惰性扫描一次后内存自增；调用方（append/recover）均持锁，语义不变
        # （seq 仍严格递增、与文件对齐：扫描含 recover() 已补入的行）。
        if self._seq is None:
            done = self._read_seq_set(self.jsonl) | self._read_seq_set(self.wal)
            self._seq = max(done, default=0)
        self._seq += 1
        return self._seq

    # ---------- 写路径 ----------

    def append(self, entry: AuditEntry) -> AuditEntry:
        """写一条审计：按管线模式分发（local/kafka/dual）。

        local：WAL → JSONL → 清理 WAL（原有行为不变）。
        kafka：先投 Kafka，成功即返；失败降级走 WAL → JSONL。
        dual ：先投 Kafka（记录成败），再无条件走 WAL → JSONL（双写对账用）。

        WAL/JSONL 均只写内核页缓存（立即对读者可见、进程被杀不丢）；
        fsync 按模式异步批量（默认）或逐条同步（flush_interval<=0）执行。
        """
        with self._lock:
            self._assert_open()
            seq = self._next_seq()
            entry = AuditEntry(**{**asdict(entry), "seq": seq})
            line = entry.to_json_line()

            # 0) Kafka 投递（kafka/dual 模式）
            kafka_ok = False
            if self.pipeline_mode in ("kafka", "dual"):
                kafka_ok = self.sink.ready() and self.sink.send(entry)
                if self.pipeline_mode == "kafka" and kafka_ok:
                    return entry  # kafka 单写模式：投递成功即返，不落本地
                if self.pipeline_mode == "kafka" and not kafka_ok:
                    logger.warning("Kafka 投递失败，降级走本地 JSONL（pipeline=kafka）")
                # dual 模式：无论 kafka 成败，继续走本地（双写对账）

            try:
                # 1) 先写 WAL（追加＋flush 入内核页缓存；fsync 交后台线程/同步模式）
                self._wal_fh.write(line + "\n")
                self._wal_fh.flush()
                if self._sync_mode:
                    os.fsync(self._wal_fh.fileno())  # 同步模式：保持旧掉电级持久口径

                # 2) 再写 JSONL（主档；与旧实现一致只 flush 不 fsync）
                self._jsonl_fh.write(line + "\n")
                self._jsonl_fh.flush()
            except Exception:
                # 异常时兜底 flush：尽力把已写的 WAL 内容 fsync 落盘再抛出，
                # 保证崩溃后 recover() 仍能补齐（对应任务书「异常时 flush」）
                try:
                    os.fsync(self._wal_fh.fileno())
                except OSError:
                    pass
                raise

            # 3) 成功后清理 WAL（截断为空：记录已安全进入 JSONL 页缓存）
            self._wal_fh.truncate(0)
            self._dirty = True  # 置脏，交后台线程在下一间隔批量 fsync
            return entry

    # ---------- 启动恢复 ----------

    def recover(self) -> int:
        """启动时调用：把 WAL 中 JSONL 缺失的记录补进 JSONL，然后清空 WAL。

        返回恢复（补写）的条数。
        """
        with self._lock:
            self._assert_open()
            if not self.wal.exists():
                return 0
            done = self._read_seq_set(self.jsonl)
            recovered = 0
            lines: list[str] = []
            for line in self.wal.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    seq = json.loads(line)["seq"]
                except (json.JSONDecodeError, KeyError):
                    continue
                if seq not in done:  # JSONL 还没有 → 补写
                    lines.append(line)
                    recovered += 1
            if lines:
                self._jsonl_fh.write("".join(l + "\n" for l in lines))
                self._jsonl_fh.flush()
                self._dirty = True
            self._wal_fh.truncate(0)  # 恢复后清空 WAL（持久句柄截断）
            return recovered


def make_entry(
    *,
    caller: str,
    endpoint: str,
    params: str,
    status: int,
    latency_ms: int,
    response_body: str,
    ts: float | None = None,
    trace_id: str = "",
) -> AuditEntry:
    """从请求上下文构造八元组（参数与响应只存摘要，不落明文）。

    trace_id 缺省空串＝存量七元组口径（P0-5 之前的记录与此一致，读取兼容）。
    """
    from datetime import datetime, timezone

    t = ts if ts is not None else time.time()
    return AuditEntry(
        ts=datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="milliseconds"),
        caller=caller,
        endpoint=endpoint,
        params_digest=digest_sha256(params),
        status=status,
        latency_ms=latency_ms,
        response_digest=digest_sha256(response_body),
        trace_id=trace_id,
    )
