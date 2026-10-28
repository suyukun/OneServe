# -*- coding: utf-8 -*-
"""Casdoor 登录审计消费（task-029）：拉原生记录 → 现有审计八元组管线。

两层审计口径（维护者 2026-10-19）：业务层＝平台自建八元组（不动）；登录层＝消费
Casdoor 原生记录。本模块是登录层的**只读消费者**：后台守护线程按固定间隔调
Casdoor GET /api/get-records（built-in admin 会话，凭证只取 env），把
login / logout 记录映射成审计八元组写进 app.audit.AuditWriter——不新建存储，
JSONL/Kafka 双模与 WAL 兜底全部复用既有管线，审计查询页（GET /v1/portal/audit/logs）
零改动即可见。

映射（action → endpoint，页面按 endpoint 部分匹配 "casdoor:" 查看）：
  login 且响应无错            → casdoor:login_success（status 200）
  login 且响应含冻结/禁用语义  → casdoor:login_frozen （status 403）
  login 其余错误              → casdoor:login_failed （status 401）
  logout                      → casdoor:logout        （status 200）
其余 action（管理操作、login/oauth/access_token 令牌交换）不属登录层，跳过。
caller＝记录 user；login 失败行 Casdoor 的 user 为空，回退解析 object 里的 username
（实测：失败行 user=""、object={"username":"tester",...}）。两者都空（授权码回调等
重复行）＝无「谁」可审计，跳过不落 unknown 噪声。timestamp＝createdTime。

detail（IP/组织/应用/请求路径/失败原因/记录 UUID；记录带 UA 类字段时一并带上）
进入 params/response 的 sha256 摘要输入——八元组 hash_only 纪律不破，不新增列，
不把明文 IP 落到审计主档；排障看本模块日志或 Casdoor 原记录。

游标（增量口径）：
  · 状态文件 AUDIT_DIR/casdoor-login-cursor.json 记 last_id（Casdoor 记录自增 id）
    ＋last_ts；重启从游标续拉，不重复落账；
  · **首次部署**（无状态文件）回补最近 7 天（AUDIT_LOGIN_BACKFILL_DAYS 可调），
    之后每次启动只拉游标之后的增量；
  · id 回退（Casdoor 库重置/恢复旧快照）自动降级为 7 天回补。

部署会话自噪声：拉取器每 30 分钟要重新登录 built-in admin，自己会产生一条
organization=built-in/user=admin/application=app-built-in 的 login 成功记录——
该签名显式排除，避免把拉取器自身心跳当成「管理员登录」写进审计。

开关：AUDIT_LOGIN_CONSUME（缺省 on；off/0/false/no 完全不启线程，测试友好）。
间隔：AUDIT_LOGIN_POLL_S（缺省 60s）；凭证：CASDOOR_ADMIN_PASSWORD（compose 注入，
与 configs/casdoor/app.conf 的 initAdminPassword 同源）；基址：OIDC_INTERNAL_BASE。
失败纪律：任何网络/协议错误只记节流日志（首错＋每 10 次一条），下一轮重试，
不抛、不阻断业务，也不推进游标（已落账的前缀游标在异常时先保存）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from app.audit import AuditWriter, make_entry

logger = logging.getLogger("oneserve.audit.login")

DEFAULT_BASE = "http://oneserve-casdoor:8000"
DEFAULT_INTERVAL_S = 60.0
DEFAULT_BACKFILL_DAYS = 7
PAGE_SIZE = 100                 # 实测该版本 get-records 单页上限 100（500 会重复分页）
MAX_PAGES = 40                  # 单轮最多 4000 条，防病态数据拖死一轮
SESSION_TTL_S = 1800            # built-in admin 会话缓存（同 admin_accounts）

ACTION_SUCCESS = "casdoor:login_success"
ACTION_FAILED = "casdoor:login_failed"
ACTION_FROZEN = "casdoor:login_frozen"
ACTION_LOGOUT = "casdoor:logout"

# 冻结/禁用语义（Casdoor 文案实测：The user is forbidden to sign in, please contact
# the administrator；另兼容 frozen/locked 与中文字样）
_FROZEN_WORDS = ("forbidden", "frozen", "locked", "冻结", "锁定", "禁用")

_STATE_NAME = "casdoor-login-cursor.json"
_STATUS_RE = re.compile(r'status:\s*"((?:[^"\\]|\\.)*)"')
_MSG_RE = re.compile(r'msg:\s*"((?:[^"\\]|\\.)*)"')


# ---------- 开关与配置 ----------

def login_consume_enabled(env: dict | None = None) -> bool:
    """AUDIT_LOGIN_CONSUME 开关：缺省 on；off/0/false/no 关闭（大小写不敏感）。"""
    src = os.environ if env is None else env
    raw = str(src.get("AUDIT_LOGIN_CONSUME", "on")).strip().lower()
    return raw not in ("off", "0", "false", "no")


def _env_float(env, key: str, default: float) -> float:
    try:
        val = float(str(env.get(key, default)).strip())
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _env_int(env, key: str, default: int) -> int:
    try:
        val = int(str(env.get(key, default)).strip())
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


# ---------- 纯逻辑：记录 → 审计字段 ----------

def parse_casdoor_time(raw) -> float | None:
    """Casdoor createdTime（如 2026-10-20T01:19:54Z）→ epoch 秒；坏值返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parse_object(raw) -> dict:
    """Casdoor object 字段是 JSON 字符串（密码已由 Casdoor 打码）；坏值返回空 dict。"""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def parse_response(raw) -> tuple[str, str]:
    """解析 Casdoor response 的 Go 字面量（{status:"ok", msg:""}）→ (status, msg)。"""
    text = str(raw or "")
    sm = _STATUS_RE.search(text)
    mm = _MSG_RE.search(text)
    status = sm.group(1) if sm else ""
    msg = mm.group(1) if mm else ""
    return status, msg


def classify_login(response: str) -> tuple[str, int]:
    """登录响应 → (endpoint, status)：无错误即成功；冻结/禁用优先于普通失败。"""
    status, msg = parse_response(response)
    if status == "error" or msg.strip():
        low = msg.lower()
        if any(word in low for word in _FROZEN_WORDS):
            return ACTION_FROZEN, 403
        return ACTION_FAILED, 401
    return ACTION_SUCCESS, 200


def actor_of(record: dict) -> str:
    """审计 actor：优先记录 user；失败行 user 为空时回退 object.username。"""
    user = str(record.get("user") or "").strip()
    if user:
        return user
    obj = _parse_object(record.get("object"))
    for key in ("username", "name", "user"):
        val = str(obj.get(key) or "").strip()
        if val:
            return val
    return "unknown"


def is_poller_self_login(record: dict) -> bool:
    """拉取器自心跳：built-in admin 拉取会话自身的 login 成功记录（不回灌审计）。"""
    if str(record.get("action") or "") != "login":
        return False
    if str(record.get("organization") or "") != "built-in":
        return False
    if str(record.get("user") or "") != "admin":
        return False
    obj = _parse_object(record.get("object"))
    return (str(obj.get("username") or "") == "admin"
            and str(obj.get("application") or "") == "app-built-in")


def _detail_of(record: dict, msg: str = "") -> str:
    """可读摘要（进摘要输入，不落明文主档）：IP/组织/应用/路径/原因/记录 UUID。"""
    obj = _parse_object(record.get("object"))
    parts: list[str] = []
    ip = str(record.get("clientIp") or "").strip()
    if ip:
        parts.append("ip=" + ip)
    org = str(record.get("organization") or "").strip()
    if org:
        parts.append("org=" + org)
    app = str(obj.get("application") or "").strip()
    if app:
        parts.append("app=" + app)
    uri = str(record.get("requestUri") or "").strip()
    if uri:
        parts.append("path=" + uri)
    # 「浏览器摘要（有则带）」：该版本记录模型无 UA 列，未来若带则自动收录
    for key in ("userAgent", "user_agent", "ua", "browser"):
        val = str(record.get(key) or "").strip()
        if val:
            parts.append("ua=" + val[:120])
            break
    if msg.strip():
        parts.append("msg=" + msg.strip()[:200])
    extra = str(record.get("detail") or "").strip()
    if extra:
        parts.append("detail=" + extra[:200])
    name = str(record.get("name") or "").strip()
    if name:
        parts.append("record=" + name)
    return "; ".join(parts)


def map_record(record: dict, now: float | None = None) -> dict | None:
    """单条 Casdoor 记录 → 审计字段；非登录层记录或拉取器自心跳返回 None。

    返回 {endpoint, caller, status, ts, detail, record_id, record_name}——
    endpoint/caller/status/ts 即八元组业务列，detail 进摘要输入。
    """
    action = str(record.get("action") or "").strip()
    if action == "login":
        if is_poller_self_login(record):
            return None
        endpoint, status = classify_login(record.get("response"))
        _, msg = parse_response(record.get("response"))
    elif action == "logout":
        endpoint, status = ACTION_LOGOUT, 200
        _, msg = parse_response(record.get("response"))
    else:
        return None
    rid = _as_int(record.get("id"))
    if rid <= 0:
        return None
    actor = actor_of(record)
    if actor == "unknown":
        # 授权码回调等无 user/username 的重复登录记录：无「谁」可审计，跳过
        # （同一事件通常另有一条带 user 的 login 行，避免 unknown 噪声）
        return None
    ts = parse_casdoor_time(record.get("createdTime"))
    if ts is None:
        ts = now if now is not None else time.time()
    return {
        "endpoint": endpoint,
        "caller": actor,
        "status": status,
        "ts": ts,
        "detail": _detail_of(record, msg),
        "record_id": rid,
        "record_name": str(record.get("name") or ""),
    }


def _as_int(raw) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def to_audit_entry(mapped: dict):
    """映射结果 → 八元组（detail 只进 params/response 的 sha256 摘要）。"""
    params = json.dumps({"record": mapped.get("record_name", ""),
                         "detail": mapped.get("detail", "")},
                        ensure_ascii=False, sort_keys=True)
    return make_entry(caller=mapped["caller"], endpoint=mapped["endpoint"],
                      params=params, status=int(mapped["status"]), latency_ms=0,
                      response_body=mapped.get("detail", ""), ts=mapped["ts"])


# ---------- Casdoor 客户端（built-in admin 会话；失败抛异常，由轮询节流） ----------

class CasdoorError(Exception):
    """Casdoor 不可达/拒绝（登录审计拉取降级口径：下轮重试）。"""


class CasdoorRecordClient:
    """GET /api/get-records 只读客户端（urllib；线程内同步调用）。"""

    def __init__(self, base: str = "", password: str = "", *, username: str = "admin",
                 application: str = "app-built-in", organization: str = "built-in",
                 timeout: float = 8.0, opener_factory=None):
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.password = password
        self.username = username
        self.application = application
        self.organization = organization
        self.timeout = timeout
        self._opener = None
        self._at = 0.0
        self._opener_factory = opener_factory or self._default_opener

    @staticmethod
    def _default_opener():
        import http.cookiejar
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def _session(self):
        if self._opener is not None and (time.time() - self._at) < SESSION_TTL_S:
            return self._opener
        if not self.password:
            raise CasdoorError("CASDOOR_ADMIN_PASSWORD 未配置")
        opener = self._opener_factory()
        body = json.dumps({"type": "login", "username": self.username,
                           "password": self.password,
                           "application": self.application,
                           "organization": self.organization}).encode()
        req = urllib.request.Request(self.base + "/api/login", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:   # noqa: BLE001 —— 统一降级口径
            raise CasdoorError("Casdoor 部署会话登录失败：" + type(exc).__name__) from exc
        if out.get("status") != "ok":
            raise CasdoorError("Casdoor 部署会话登录被拒")
        self._opener = opener
        self._at = time.time()
        return opener

    def _reset_session(self) -> None:
        self._opener = None
        self._at = 0.0

    def _get(self, path: str) -> dict:
        opener = self._session()
        req = urllib.request.Request(self.base + path)
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._reset_session()
            raise CasdoorError(f"Casdoor HTTP {exc.code}") from exc
        except Exception as exc:   # noqa: BLE001
            self._reset_session()
            raise CasdoorError("Casdoor 不可达：" + type(exc).__name__) from exc

    def fetch_page(self, page: int, page_size: int = PAGE_SIZE) -> list[dict]:
        """取一页记录（createdTime 倒序）；协议错/非 ok 抛 CasdoorError。"""
        path = (f"/api/get-records?pageSize={int(page_size)}&p={int(page)}"
                "&sortField=createdTime&sortOrder=descend")
        out = self._get(path)
        if out.get("status") != "ok":
            raise CasdoorError("get-records 拒绝：" + str(out.get("msg") or "")[:120])
        rows = out.get("data") or []
        return [r for r in rows if isinstance(r, dict)]


# ---------- 拉取器（游标＋回补＋节流重试） ----------

class LoginAuditPuller:
    """登录审计拉取器：持有 AuditWriter，按游标消费 Casdoor 记录。"""

    def __init__(self, writer: AuditWriter, client, *, state_path: str | Path,
                 backfill_days: int = DEFAULT_BACKFILL_DAYS,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 now_fn=time.time, page_size: int = PAGE_SIZE):
        self.writer = writer
        self.client = client
        self.state_path = Path(state_path)
        self.backfill_days = max(int(backfill_days), 1)
        self.interval_s = max(float(interval_s), 1.0)
        self.now_fn = now_fn
        self.page_size = page_size
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fail_count = 0

    # ----- 游标 -----

    def load_cursor(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"last_id": 0, "last_ts": ""}
        if not isinstance(data, dict):
            return {"last_id": 0, "last_ts": ""}
        return {"last_id": _as_int(data.get("last_id")),
                "last_ts": str(data.get("last_ts") or "")}

    def save_cursor(self, last_id: int, last_ts: str = "") -> None:
        """原子写游标（tmp+replace）；失败只告警，不影响已落账条目。"""
        payload = {"version": 1, "last_id": int(last_id), "last_ts": last_ts,
                   "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            logger.warning("登录审计游标写入失败（下轮重试；已落账条目不回滚）", exc_info=True)

    # ----- 取数 -----

    def _fetch_backfill(self) -> tuple[list[dict], int]:
        """回补最近 N 天：倒序翻页到 cutoff；返回 (窗口内记录, 已见最大 id)。"""
        cutoff = self.now_fn() - self.backfill_days * 86400
        out: list[dict] = []
        seen: set[int] = set()
        max_seen = 0
        for page in range(1, MAX_PAGES + 1):
            rows = self.client.fetch_page(page, self.page_size)
            if not rows:
                break
            stop = False
            for rec in rows:
                rid = _as_int(rec.get("id"))
                if rid > max_seen:
                    max_seen = rid
                ts = parse_casdoor_time(rec.get("createdTime"))
                if ts is not None and ts < cutoff:
                    stop = True          # 倒序：更旧的都在后面，收工
                    continue
                if rid > 0 and rid not in seen:
                    seen.add(rid)
                    out.append(rec)
            if stop or len(rows) < self.page_size:
                break
        if max_seen == 0 and out:
            max_seen = max(_as_int(r.get("id")) for r in out)
        return out, max_seen

    def _fetch_incremental(self, last_id: int) -> tuple[list[dict], bool]:
        """拉 id > last_id 的增量：返回 (记录, id 是否回退＝需回补)。"""
        out: list[dict] = []
        seen: set[int] = set()
        for page in range(1, MAX_PAGES + 1):
            rows = self.client.fetch_page(page, self.page_size)
            if not rows:
                break
            top = max((_as_int(r.get("id")) for r in rows), default=0)
            if page == 1 and top and top < last_id:
                return [], True       # Casdoor 库重置/回滚：游标失效，降级回补
            hit_known = False
            for rec in rows:
                rid = _as_int(rec.get("id"))
                if rid <= 0 or rid in seen:
                    continue
                if rid <= last_id:
                    hit_known = True  # 已穿越已知水位，停止翻页
                    continue
                seen.add(rid)
                out.append(rec)
            if hit_known or len(rows) < self.page_size:
                break
        return out, False

    # ----- 落账 -----

    def _consume(self, records: list[dict], cursor: dict, *, max_seen: int = 0) -> int:
        """按 id 升序落账；跳过非登录层记录；异常时先存已推进前缀游标再抛。"""
        last_done = _as_int(cursor.get("last_id"))
        last_ts = str(cursor.get("last_ts") or "")
        appended = 0
        by_id: dict[int, dict] = {}
        for rec in records:
            rid = _as_int(rec.get("id"))
            if rid > last_done and rid not in by_id:
                by_id[rid] = rec
        for rid in sorted(by_id):
            mapped = map_record(by_id[rid], now=self.now_fn())
            if mapped is not None:
                entry = to_audit_entry(mapped)
                try:
                    self.writer.append(entry)
                except Exception:
                    self.save_cursor(last_done, last_ts)
                    raise
                appended += 1
                ts_iso = entry.ts
                if ts_iso > last_ts:
                    last_ts = ts_iso
            last_done = rid
        if max_seen > last_done:
            last_done = max_seen
        if last_done != _as_int(cursor.get("last_id")) or appended:
            self.save_cursor(last_done, last_ts)
        return appended

    def run_once(self) -> int:
        """一轮拉取：首拉 7 天回补，之后增量；返回新写入审计条数。"""
        cursor = self.load_cursor()
        last_id = _as_int(cursor.get("last_id"))
        if last_id <= 0:
            records, max_seen = self._fetch_backfill()
            return self._consume(records, cursor, max_seen=max_seen)
        records, reset = self._fetch_incremental(last_id)
        if reset:
            logger.warning("Casdoor 记录 id 回退（库重置？）：游标失效，改 7 天回补")
            records, max_seen = self._fetch_backfill()
            # 回补必须从头重算（旧游标水位高于新库 id，不能再用它过滤）
            return self._consume(records, {"last_id": 0, "last_ts": ""},
                                 max_seen=max_seen)
        if not records:
            return 0
        return self._consume(records, cursor)

    def _safe_run_once(self) -> int:
        """异常吞掉＋节流日志（首错＋每 10 次一条），下一轮重试。"""
        try:
            n = self.run_once()
        except Exception as exc:   # noqa: BLE001 —— 拉取失败绝不阻断业务
            self._fail_count += 1
            if self._fail_count == 1 or self._fail_count % 10 == 0:
                logger.warning("Casdoor 登录审计拉取失败（第 %d 次，下轮重试）：%s",
                               self._fail_count, exc)
            return 0
        if self._fail_count:
            logger.info("Casdoor 登录审计拉取恢复（此前连续失败 %d 次）", self._fail_count)
            self._fail_count = 0
        return n

    # ----- 线程生命周期 -----

    def _loop(self) -> None:
        while True:
            self._safe_run_once()
            if self._stop.wait(self.interval_s):
                return

    def start(self) -> "LoginAuditPuller":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="casdoor-login-audit",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


def start_from_env(writer: AuditWriter, *, audit_dir: str | Path = "",
                   env: dict | None = None, client=None) -> LoginAuditPuller | None:
    """按 env 装配并启动拉取器；开关 off 或凭证缺失＝返回 None（不建线程）。"""
    src = os.environ if env is None else env
    if not login_consume_enabled(src):
        logger.info("AUDIT_LOGIN_CONSUME=off：登录审计消费未启动")
        return None
    password = str(src.get("CASDOOR_ADMIN_PASSWORD") or "")
    if not password:
        logger.warning("CASDOOR_ADMIN_PASSWORD 未配置：登录审计消费未启动（不影响业务）")
        return None
    base = str(src.get("OIDC_INTERNAL_BASE") or DEFAULT_BASE)
    interval = _env_float(src, "AUDIT_LOGIN_POLL_S", DEFAULT_INTERVAL_S)
    days = _env_int(src, "AUDIT_LOGIN_BACKFILL_DAYS", DEFAULT_BACKFILL_DAYS)
    state_path = str(src.get("AUDIT_LOGIN_STATE") or
                     (Path(audit_dir) if audit_dir else Path(".")) / _STATE_NAME)
    if client is None:
        client = CasdoorRecordClient(base=base, password=password)
    puller = LoginAuditPuller(writer, client, state_path=state_path,
                              backfill_days=days, interval_s=interval)
    puller.start()
    logger.info("登录审计消费已启动：interval=%ss backfill=%sd state=%s",
                interval, days, state_path)
    return puller
