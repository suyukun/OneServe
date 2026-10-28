# -*- coding: utf-8 -*-
"""错误码中心（P1-2 · SPEC §4.4 envelope ＋ A6 错误码中心同源）——纯逻辑，零依赖。

单一职责：**全平台机读错误码与错误信封字段的唯一定义处**。服务层
（kv/hbase/proxy/batch/appkeys/ops/admin/portal/main）与构建期文档生成
（scripts/gen_openapi.py）、A6 错误码中心页面（portal/openapi/errors.json）
全部从本模块取码——消除散落的字符串字面量（tests/test_error_envelope.py 用
AST 机检守卫）。

§4.4 信封（字段名向后兼容：既有 error/message 不改名，只增字段）：
    {
      "error": "<机读码>",                 # 既有字段，不改名（接入方已在用）
      "message": "<人话说明>",             # 既有字段，不改名
      "param": "<出错参数>",               # 适用时：指明改哪个参数（Stripe 错误对象设计）
      "traceId": "<本次追踪号>",           # trace_middleware 统一注入（P0-5）
      "request_log_url": "my.html?trace_id=<traceId>",  # 去哪查日志（既有深链，真可达）
      "docs_url": "errors.html#<error>"    # 码表锚点（A6 错误码中心）
    }

param 由本模块按码给出规范参数名，trace_middleware 统一补进错误体——端点层
无须逐处硬编码参数名，也不产生第二份事实来源。

审计反查深链（request_log_url）落点＝门户「我的接入」页 my.html 的既有 trace_id
深链机制（portal/assets/my.js init() 读 ?trace_id= 即触发反查）；docs_url 落点＝
A6 错误码中心页 errors.html（每码一个 <tr id="<code>"> 锚点），两者均真可达，
不是造出来的 404 URL。
"""
from __future__ import annotations

from urllib.parse import quote

# ---------- 信封字段名（既有字段不改名；新增字段集中在此，防各处拼写漂移） ----------
FIELD_ERROR = "error"
FIELD_MESSAGE = "message"
FIELD_PARAM = "param"
FIELD_TRACE_ID = "traceId"
FIELD_REQUEST_LOG_URL = "request_log_url"
FIELD_DOCS_URL = "docs_url"

# ---------- 深链落点（既有页面机制；改此处即改全平台错误体的指向） ----------
REQUEST_LOG_PAGE = "my.html"       # 我的接入·日志（?trace_id= 反查，portal/assets/my.js）
DOCS_PAGE = "errors.html"          # A6 错误码中心（每码 <tr id="<code>"> 锚点）

# ---------- 出错参数规范名（无单一参数＝空串） ----------
PARAM_BODY = "请求体"
PARAM_APIKEY = "apikey"


class ErrorCode:
    """一条机读码定义（不可变值对象；字段即 A6 码表列）。"""

    __slots__ = ("code", "http_status", "alt_statuses", "meaning", "solution",
                 "param", "related", "source", "scope", "docs_variants")

    def __init__(self, code: str, http_status: int | None, meaning: str,
                 solution: str, *, param: str = "", related: str = "",
                 source: str = "service", scope: str = "http",
                 alt_statuses: tuple = (), docs_variants: dict | None = None) -> None:
        self.code = code
        self.http_status = http_status          # None＝非 HTTP 面（批量行/任务级机读码）
        self.alt_statuses = tuple(alt_statuses)  # 同码多状态（如 credential_expired 403/409）
        self.meaning = meaning
        self.solution = solution
        self.param = param
        # 契约「相关参数」列文案（可含中文说明，如「apikey 请求头」）；缺省＝param
        self.related = related
        self.source = source                    # service | gateway
        self.scope = scope                      # http | row | job
        # 按服务原型细化的文档文案（键→{meaning, solution}）：同码不同档案的措辞差异，
        # 仍集中在本模块（单一真源），gen_openapi 按 archetype 取用——不丢原文档粒度。
        self.docs_variants = docs_variants or {}


# ══════════════════ 机读码表（唯一事实来源；新增码只在此加一行） ══════════════════

# 通用/基础（多域共用）
INVALID_JSON = "invalid_json"
UNKNOWN_TENANT = "unknown_tenant"
SERVICE_NOT_GRANTED = "service_not_granted"
INTERNAL_ERROR = "internal_error"
SERVICE_NOT_FOUND = "service_not_found"
INVALID_VERSION = "invalid_version"   # 卡4 NC-3=a：保留（查询体带 version 即 400；文案指 tabver）
MAX_ITEMS_EXCEEDED = "max_items_exceeded"
REDIS_UNAVAILABLE = "redis_unavailable"
INVALID_KEY = "invalid_key"
BAD_DESENSITIZE_RULE = "bad_desensitize_rule"
DATA_NOT_READY = "data_not_ready"
GUARD_TIMEOUT = "guard_timeout"
SCHEMA_DRIFT_QUARANTINED = "schema_drift_quarantined"
INVALID_PHONE = "invalid_phone"

# HBase 通道
HBASE_UPSTREAM_ERROR = "HBASE_UPSTREAM_ERROR"
HBASE_BAD_LIMIT = "HBASE_BAD_LIMIT"
HBASE_DRIVER_MISSING = "HBASE_DRIVER_MISSING"

# B 型代理通道
BAD_PROXY_CONFIG = "bad_proxy_config"
INVALID_PROXY_PATH = "invalid_proxy_path"
PROXY_TRAVERSAL = "proxy_traversal_blocked"   # #37（2026-10-15 复审 P2-4）：代理子路径白名单越界拒 403
UPSTREAM_TIMEOUT = "upstream_timeout"
UPSTREAM_UNREACHABLE = "upstream_unreachable"

# 批量通道（HTTP 面）
INVALID_REQUEST = "invalid_request"
INVALID_SCOPE = "invalid_scope"
EMPTY_KEYS = "empty_keys"
TOO_MANY_KEYS = "too_many_keys"
FIELDS_NOT_GRANTED = "fields_not_granted"
JOB_NOT_FOUND = "job_not_found"
JOB_NOT_FINISHED = "job_not_finished"
JOB_NOT_SUCCEEDED = "job_not_succeeded"
RESULTS_GONE = "results_gone"
INVALID_LIMIT = "invalid_limit"
INVALID_CURSOR = "invalid_cursor"
INVALID_PAGE = "invalid_page"
ILLEGAL_TRANSITION = "illegal_transition"
JOB_NOT_RETRYABLE = "job_not_retryable"
QUOTA_CONCURRENT_EXCEEDED = "quota_concurrent_exceeded"
QUOTA_DAILY_EXCEEDED = "quota_daily_exceeded"

# 凭证/授权（签发与调用期）
TRIAL_SOURCE_MISSING = "trial_source_missing"
INVALID_TENANT = "invalid_tenant"
INVALID_KEY_TYPE = "invalid_key_type"
INVALID_APP_NAME = "invalid_app_name"
OA_TICKET_REQUIRED = "oa_ticket_required"
INVALID_OA_TICKET = "invalid_oa_ticket"
KEY_LIMIT_REACHED = "key_limit_reached"
TENANT_NOT_REGISTERED = "tenant_not_registered"
TENANT_FROZEN = "tenant_frozen"
TENANT_NOT_FOUND = "tenant_not_found"
TENANT_YAML_MODE = "tenant_yaml_mode"
INVALID_EXPIRES_AT = "invalid_expires_at"
UNKNOWN_SERVICE = "unknown_service"
EMPTY_FIELDS = "empty_fields"
EMPTY_GRANTS = "empty_grants"
APPKEY_NOT_FOUND = "appkey_not_found"
ALREADY_ROTATED = "already_rotated"
NOT_ACTIVE = "not_active"
NOT_DISABLED = "not_disabled"
CREDENTIAL_EXPIRED = "credential_expired"
CREDENTIAL_ROTATED_EXPIRED = "credential_rotated_expired"
CREDENTIAL_DISABLED = "credential_disabled"
INVALID_ACTION = "invalid_action"
APPKEY_HAS_CALLS = "appkey_has_calls"               # 受限删（AC-2）：有调用记录的凭证不可删
EXPIRES_SHRINK_FORBIDDEN = "expires_shrink_forbidden"   # 续期缩短（AC-3）：只能延长，作废走禁用

# 运营管理（含目录校验/写区）
UNAUTHORIZED = "unauthorized"
AUTH_REQUIRED = "auth_required"   # 登录闸（休眠码：无登录模式 ADR-0029 下零触发，登记保留）
ROLE_FORBIDDEN = "role_forbidden"   # 角色闸（休眠码：无登录模式 ADR-0029 下零触发，登记保留）
RATE_LIMITED = "rate_limited"
GATEWAY_UNREACHABLE = "gateway_unreachable"
INVALID_QUOTA = "invalid_quota"
BAD_QUERY = "bad_query"
MISSING_CALLER = "missing_caller"
MISSING_QUERY = "missing_query"
BAD_CONSOLE_SERVICES = "bad_console_services"
RUNTIME_SECTION_LOST = "runtime_section_lost"
INVALID_NAME = "invalid_name"
INVALID_ROUTE = "invalid_route"
ROUTE_OCCUPIED = "route_exists"   # 前端 publishFailText 既有映射（route_exists＝地址被占）
SERVICE_NAME_CONFLICT = "service_name_conflict"   # 发布名查重（AppKey 二批 M 级）：同名异路由拒发
INVALID_STATUS = "invalid_status"
INVALID_FIELD_SPACE = "invalid_field_space"
FIELD_NOT_IN_DICT = "field_not_in_dict"
INVALID_FIELD = "invalid_field"
SAMPLE_REQUIRED = "sample_required"
ALREADY_OFFLINE = "already_offline"
DRAFT_NOT_OFFLINE = "draft_not_offline"
BAD_UPSTREAMS = "bad_upstreams"
INVALID_METHOD = "invalid_method"
INVALID_UPSTREAM_URL = "invalid_upstream_url"
INVALID_TIMEOUT = "invalid_timeout"
INVALID_RETRY = "invalid_retry"
INVALID_UPSTREAM_NAME = "invalid_upstream_name"
PROXY_NOT_FOUND = "proxy_not_found"

# 账单域（管理功能群一期 C 节·账单页＋CSV；真源＝pg usage schema，ADR-0024）
BILL_PERIOD_INVALID = "bill_period_invalid"
BILL_PERIOD_NOT_FOUND = "bill_period_not_found"
BILL_STATUS_REFUSED = "bill_status_refused"
BILL_DB_UNAVAILABLE = "bill_db_unavailable"

# 价目管理（管理功能群一期 B 节 · 域9 计费管理；pg-only 域）
PRICE_ITEM_NOT_FOUND = "price_item_not_found"
PRICING_VALIDATION = "pricing_validation"           # 区间不变量/月初约束/非法枚举等校验拒绝（422 人话报因）
PRICING_STATE_CONFLICT = "pricing_state_conflict"   # 生效窗重叠／已过期条目停用（409）
PRICING_DB_UNAVAILABLE = "pricing_db_unavailable"   # 计费库（oneserve-meta pg）未起/未响应（503，读面走降级态）

# 网关侧（APISIX key-auth；源码在 configs/apisix，码名在此同源）
AUTH_FAILED = "auth_failed"

# 批量行/任务级机读码（非 HTTP 信封；落结果行 error 列与任务记录，同属错误族）
KV_READ_FAILED = "kv_read_failed"
ROW_DECODE_FAILED = "row_decode_failed"
KV_UNAVAILABLE = "kv_unavailable"
JOB_STATE_INCONSISTENT = "job_state_inconsistent"
SNAPSHOT_EMPTY_VERSION = "snapshot_empty_version"
ROWCOUNT_MISSING = "row_count_missing"
ROWCOUNT_MISMATCH = "row_count_mismatch"


CODES: tuple[ErrorCode, ...] = (
    ErrorCode(INVALID_JSON, 400, "请求体不是合法 JSON 对象",
              "按 Content-Type: application/json 发送 JSON 对象体", param=PARAM_BODY),
    ErrorCode(UNKNOWN_TENANT, 403, "AppKey 未在授权矩阵登记（未绑定租户）",
              "联系平台管理员完成授权登记（审批走 OA）；或核对 AppKey 是否发放正确",
              param=PARAM_APIKEY, related="apikey 请求头"),
    ErrorCode(SERVICE_NOT_GRANTED, 403, "租户未获本服务授权",
              "按 OA 流程申请本服务授权；已批复则确认授权登记是否完成",
              param=PARAM_APIKEY, related="apikey 请求头"),
    ErrorCode(INTERNAL_ERROR, 503, "服务内部错误（存储不可用时降级拒绝，不读脏）",
              "稍后重试；持续失败联系平台值班并附时间点"),
    ErrorCode(SERVICE_NOT_FOUND, 404, "服务未注册（通配路由下的未知 service_id）",
              "核对服务标识；到服务目录确认该服务是否已上线"),
    ErrorCode(INVALID_VERSION, 400, "查询请求不再接受 version（服务修订号机制已移除）",
              "去掉 version 字段重试；表数据版本（tabver，YYYYMMDD）由平台加载切版管理，"
              "无需也不经查询传入（响应里的 live_version 即当前线上数据版本）", param="version"),
    ErrorCode(MAX_ITEMS_EXCEEDED, 400, "单次调用键数超过上限（点查服务为 1，批量请走数仓通道）",
              "改为单键调用；批量需求走批量任务通道", param="key", related="key"),
    ErrorCode(REDIS_UNAVAILABLE, 503, "KV 存储不可达（降级拒绝，不读脏）",
              "稍后重试；持续失败联系平台值班"),
    ErrorCode(INVALID_KEY, 400, "键格式不合法",
              "按服务入参 pattern 校验键格式（键模式由服务注册声明，平台不关注键的加密算法）",
              param="key", related="key",
              docs_variants={
                  "kv": {"meaning": "键格式不合法（按该服务入参 pattern 校验：UUID／自增 ID／摘要串等均可）",
                         "solution": "按入参表 pattern 校验键格式；键值由上游 ETL 生成，平台只按声明验收"},
                  "hbase": {"meaning": "键格式不合法（按该服务入参 pattern 校验；rowkey＝上游供数键）",
                            "solution": "按入参表 pattern 校验键格式；键值由上游 ETL 生成，平台只按声明验收"}}),
    ErrorCode(BAD_DESENSITIZE_RULE, 500, "服务端脱敏规则配置异常（fail-closed 拒服）",
              "平台侧配置问题，联系平台值班修复"),
    ErrorCode(DATA_NOT_READY, 503, "数据未上线（KV 无线上版本指针，拒绝读脏）",
              "等待数据同步上线后重试；可关注门户目录的同步状态"),
    ErrorCode(GUARD_TIMEOUT, 503, "服务端查询超时（超过 guard.timeout_ms 熔断）",
              "稍后重试；持续超时联系平台值班",
              docs_variants={"hbase": {"meaning": "服务端查询超时（HBase 读超过 guard.timeout_ms 熔断）"}}),
    ErrorCode(SCHEMA_DRIFT_QUARANTINED, 503, "字段字典 schema 漂移，服务已隔离（拒绝读脏）",
              "联系数据值班核对字段字典；修正后重跑对账脚本即恢复（无需重启）"),
    ErrorCode(INVALID_PHONE, 400, "手机号须为 11 位大陆号段（1[3-9] 开头）",
              "核对手机号；真实接入传上游生成的查询键调正式通道", param="phone"),
    ErrorCode(HBASE_UPSTREAM_ERROR, 502, "HBase 上游读取失败",
              "稍后重试；持续失败联系平台值班"),
    ErrorCode(HBASE_BAD_LIMIT, 400, "limit 须为正整数", "改为正整数 limit", param="limit"),
    ErrorCode(HBASE_DRIVER_MISSING, 503, "HBase 驱动未安装（环境缺依赖）",
              "平台侧环境问题，联系平台值班"),
    ErrorCode(BAD_PROXY_CONFIG, 500, "代理上游配置缺失（平台侧）",
              "平台侧配置问题，联系平台值班修复"),
    ErrorCode(INVALID_PROXY_PATH, 400, "代理路径不合法（含越界字符或路径段）",
              "核对上游接口路径拼写；路径按原样透传上游", param="URL 路径", related="URL 路径"),
    ErrorCode(PROXY_TRAVERSAL, 403, "代理子路径越界（白名单字符集外：上跳/../、编码变体、host/scheme/@/冒号/空白一律拒）",
              "子路径只许字母数字与 . _ - / 的相对路径段；确需特殊形态请走登记评审",
              param="URL 路径", related="URL 路径"),
    ErrorCode(UPSTREAM_TIMEOUT, 504, "上游响应超时（超过 backend.proxy.timeout_ms，熔断返回）",
              "稍后重试；如需超时口径探测联系平台（勿对生产上游慢速试探）"),
    ErrorCode(UPSTREAM_UNREACHABLE, 502, "上游服务不可达", "稍后重试；持续不可达联系平台值班"),
    ErrorCode(INVALID_REQUEST, 400, "请求体结构不合契约",
              "按本端点入参表发送；批量提交须为 {keys:[..]} 或 {scope:'snapshot'}"),
    ErrorCode(INVALID_SCOPE, 400, "scope 取值非法", "scope 须为 list 或 snapshot", param="scope"),
    ErrorCode(EMPTY_KEYS, 400, "keys 名单为空", "提供至少一个键", param="keys"),
    ErrorCode(TOO_MANY_KEYS, 413, "单任务键数超过上限",
              "拆分任务或走全量快照（scope=snapshot）", param="keys"),
    ErrorCode(FIELDS_NOT_GRANTED, 403, "请求字段超出授权白名单",
              "去掉未授权字段，或按 OA 流程申请字段授权", param="fields"),
    ErrorCode(JOB_NOT_FOUND, 404, "任务不存在", "核对 job_id", param="job_id"),
    ErrorCode(JOB_NOT_FINISHED, 409, "任务尚未到终态，结果不可取", "等待任务完成后再取结果"),
    ErrorCode(JOB_NOT_SUCCEEDED, 409, "任务未成功，结果不可取", "查看任务 error 字段并重试"),
    ErrorCode(RESULTS_GONE, 410, "结果文件已过期/被清理", "重新提交任务"),
    ErrorCode(INVALID_LIMIT, 400, "limit 参数非法", "改为正整数 limit", param="limit"),
    ErrorCode(INVALID_CURSOR, 400, "cursor 参数非法", "使用响应中的 next_cursor", param="cursor"),
    ErrorCode(INVALID_PAGE, 400, "page 参数非法", "改为正整数页码", param="page"),
    ErrorCode(ILLEGAL_TRANSITION, 409, "任务状态迁移非法", "按状态机允许的迁移操作"),
    ErrorCode(JOB_NOT_RETRYABLE, 409, "任务当前状态不可重试", "仅 failed 状态可重试"),
    ErrorCode(QUOTA_CONCURRENT_EXCEEDED, 429, "租户并发任务数超上限",
              "等待执行中任务结束后再提交（响应头 X-RateLimit-Reason=concurrency）"),
    ErrorCode(QUOTA_DAILY_EXCEEDED, 429, "当日键量配额超上限",
              "次日自动恢复，或按流程申请提额（响应头 X-RateLimit-Reason=daily）"),
    ErrorCode(TRIAL_SOURCE_MISSING, 409, "沙箱试用口径依赖的授权矩阵条目缺失",
              "平台侧配置问题，联系平台值班"),
    ErrorCode(INVALID_TENANT, 400, "租户 ID 非法（小写字母/数字/_-，2~64 位）",
              "按租户 ID 规则修正", param="tenant"),
    ErrorCode(INVALID_KEY_TYPE, 400, "key_type 须为 sandbox 或 formal",
              "改为 sandbox 或 formal", param="key_type"),
    ErrorCode(INVALID_APP_NAME, 400, "绑定应用名必填", "补充 app_name", param="app_name"),
    ErrorCode(OA_TICKET_REQUIRED, 400, "正式凭证必须绑定 OA 批件号",
              "补充 oa_ticket（正式凭证缺单号拒绝登记，ADR-0003）", param="oa_ticket"),
    ErrorCode(INVALID_OA_TICKET, 400, "OA 批件号非法（4~64 位字母数字-_）",
              "按 OA 批件号格式修正", param="oa_ticket"),
    ErrorCode(KEY_LIMIT_REACHED, 409, "该调用方凭证数达上限（正常＋轮转各一把）",
              "先禁用/轮换回收旧凭证再签发"),
    ErrorCode(TENANT_NOT_REGISTERED, 409, "租户未在授权矩阵登记，正式凭证不可签发",
              "先经 OA 审批并由管理员登记授权矩阵", param="tenant"),
    ErrorCode(TENANT_FROZEN, 409, "租户已冻结：写面动作禁止（签发/轮换/续期/授权登记）",
              "先解冻租户（租户管理页，解冻须带工单号）再执行", param="tenant"),
    ErrorCode(TENANT_NOT_FOUND, 404, "租户未登记（矩阵与注册表均无此实体）",
              "先在租户管理页新建登记或核对租户 ID", param="tenant"),
    ErrorCode(TENANT_YAML_MODE, 409, "yaml 配置模式无生命周期状态源，冻结/解冻不可用",
              "切换 META_WRITER=db 元数据库模式后操作（dev 缺省 yaml 态新建仍可用）",
              param="tenant"),
    ErrorCode(INVALID_EXPIRES_AT, 400, "有效期格式非法", "改为 YYYY-MM-DD", param="expires_at"),
    ErrorCode(UNKNOWN_SERVICE, 400, "授权引用的服务未注册", "核对 service", param="service"),
    ErrorCode(EMPTY_FIELDS, 400, "字段白名单不能为空", "至少勾选一个字段", param="fields"),
    ErrorCode(EMPTY_GRANTS, 400, "显式授权至少包含一个服务", "补充 grants", param="grants"),
    ErrorCode(APPKEY_NOT_FOUND, 404, "凭证未在注册表登记", "核对凭证或 kid", param="appkey"),
    ErrorCode(ALREADY_ROTATED, 409, "该凭证已是轮换后的旧凭证", "改用新凭证，无需再次轮换",
              param="appkey"),
    ErrorCode(NOT_ACTIVE, 409, "仅启用中的凭证可执行该操作", "先启用凭证"),
    ErrorCode(NOT_DISABLED, 409, "仅已禁用的凭证可启用", "核对凭证当前状态"),
    ErrorCode(CREDENTIAL_EXPIRED, 403, "凭证已过有效期", "重新签发凭证",
              param="appkey", alt_statuses=(409,)),
    ErrorCode(CREDENTIAL_ROTATED_EXPIRED, 403, "凭证已被轮换且重叠期已结束",
              "改用轮换后的新凭证", param="appkey", alt_statuses=(409,)),
    ErrorCode(CREDENTIAL_DISABLED, 403, "凭证已禁用", "联系平台管理员启用或重新签发",
              param="appkey"),
    ErrorCode(INVALID_ACTION, 400, "action 取值非法", "action 须为 disable 或 enable",
              param="action"),
    ErrorCode(APPKEY_HAS_CALLS, 409, "凭证已有调用记录，不能删除（审计账需不断链）",
              "改用「禁用」：调用立即被拒，登记与审计留痕保留", param="appkey"),
    ErrorCode(EXPIRES_SHRINK_FORBIDDEN, 409, "续期只能延长有效期（不能缩短）",
              "如需作废请用「禁用」；需要长期有效可选「永久」", param="expires_at"),
    ErrorCode(UNAUTHORIZED, 401, "管理令牌缺失或不正确（X-Admin-Token）",
              "携带正确的管理令牌 X-Admin-Token", param="X-Admin-Token"),
    ErrorCode(AUTH_REQUIRED, 401, "管理写操作需登录态（Bearer 令牌缺失或无效）",
              "本开源版为无登录模式（ADR-0029），此码为休眠登记不应出现；若出现请检查部署配置",
              param="Authorization"),
    ErrorCode(ROLE_FORBIDDEN, 403, "登录角色不足（admin/operator/viewer 三角色）",
              "本开源版为无登录模式（ADR-0029），此码为休眠登记不应出现；若出现请检查部署配置",
              param="Authorization"),
    ErrorCode(RATE_LIMITED, 429, "签发操作过于频繁（同租户/同 IP 固定窗限频）",
              "稍后重试（响应体 retry_after_s 为窗口秒数）"),
    ErrorCode(GATEWAY_UNREACHABLE, 502, "网关 Admin API 不可达，本次未生效",
              "稍后重试；持续失败联系平台值班"),
    ErrorCode(INVALID_QUOTA, 400, "配额须为正整数", "quota_qps/quota_daily 改为正整数",
              param="quota_qps/quota_daily"),
    ErrorCode(BAD_QUERY, 400, "审计查询条件非法（时间格式/状态条件）",
              "按提示修正查询条件", param="ts_from/ts_to/status"),
    ErrorCode(MISSING_CALLER, 400, "缺少查询凭证（caller）",
              "提供以 sb_ 或 ak_ 开头的凭证", param="caller"),
    ErrorCode(MISSING_QUERY, 400, "缺少查询条件（caller 或 trace_id 至少一项）",
              "提供凭证（caller）或追踪号（trace_id）", param="trace_id"),
    ErrorCode(BAD_CONSOLE_SERVICES, 500, "console_services 写区结构损坏",
              "平台侧修复 services.yaml 写区"),
    ErrorCode(RUNTIME_SECTION_LOST, 500, "校验失败：services.yaml 运行时注册区缺失",
              "平台侧修复文件后重试"),
    ErrorCode(INVALID_NAME, 400, "服务名必填", "补充 serviceName", param="serviceName"),
    ErrorCode(INVALID_ROUTE, 400, "路由路径格式非法", "按提示的路由格式修正", param="routePath"),
    ErrorCode(ROUTE_OCCUPIED, 400, "路由地址已被其他服务占用", "换一个地址重试；改名＝新地址发布", param="routePath"),
    ErrorCode(SERVICE_NAME_CONFLICT, 409, "服务名与既有服务同名但路由不同（同名服务无法区分）",
              "换一个服务名；同一服务更新＝保持路由不变重发（原地覆盖，版本史保留）", param="serviceName"),
    ErrorCode(INVALID_STATUS, 400, "status 取值非法", "status 须为 draft 或 published",
              param="status"),
    ErrorCode(INVALID_FIELD_SPACE, 400, "字段空间取值非法",
              "field_space 须为 wide297/news/proxy 之一（见 ADR-0004）", param="fieldSpace"),
    ErrorCode(FIELD_NOT_IN_DICT, 400, "宽表域引用了字段字典中不存在的列",
              "订正字段名，或在服务注册中声明正确的 field_space；"
              "宽表域引用不存在的列会在对账时被判删列而误隔离", param="fields"),
    ErrorCode(INVALID_FIELD, 400, "字段名非法", "字段名须小写字母开头、字母数字下划线",
              param="fields"),
    ErrorCode(SAMPLE_REQUIRED, 400, "发布态字段缺示例值（测试前置门禁）",
              "为每个字段补示例值", param="fields"),
    ErrorCode(ALREADY_OFFLINE, 409, "该对象已处于下线状态", "无需重复下线"),
    ErrorCode(DRAFT_NOT_OFFLINE, 409, "草稿无线上语义，不支持下线",
              "直接覆盖发布即可"),
    ErrorCode(BAD_UPSTREAMS, 500, "upstreams.yaml proxies 结构损坏",
              "平台侧修复 upstreams.yaml"),
    ErrorCode(INVALID_METHOD, 400, "method 须为 GET 或 POST", "改为 GET 或 POST", param="method"),
    ErrorCode(INVALID_UPSTREAM_URL, 400, "上游 URL 须为合法 http(s) 地址",
              "补充合法 upstreamUrl", param="upstreamUrl"),
    ErrorCode(INVALID_TIMEOUT, 400, "超时秒数须大于 0", "改为正数 timeoutSec", param="timeoutSec"),
    ErrorCode(INVALID_RETRY, 400, "重试次数不能为负", "改为非负 retryCount", param="retryCount"),
    ErrorCode(INVALID_UPSTREAM_NAME, 400, "可复用 Upstream 名称非法（小写 slug，2~32 位）",
              "按 slug 规则修正", param="upstreamName"),
    ErrorCode(PROXY_NOT_FOUND, 404, "代理未在登记表", "核对代理 id", param="proxy"),
    # 账单域（管理功能群一期 C 节：状态机守卫＝ADR-0024 决定第 4 条确认冻结；账单真源＝pg）
    ErrorCode(BILL_PERIOD_INVALID, 400, "账期参数非法（须为 YYYY-MM 自然月）",
              "按 YYYY-MM（自然月）修正账期参数", param="period"),
    ErrorCode(BILL_PERIOD_NOT_FOUND, 404, "账期不存在",
              "核对账期（YYYY-MM）；出账走命令行 billing_run（页面不触发出账）", param="period"),
    ErrorCode(BILL_STATUS_REFUSED, 422, "账期状态不符，动作拒绝（状态机不跳步）",
              "仅已对账（reconciled）账期可确认、仅已确认（confirmed）账期可补录单号；"
              "确认后账单底数冻结，修正走调整单（ADR-0024 决定第 4 条）", param="period"),
    ErrorCode(BILL_DB_UNAVAILABLE, 503, "账单库（pg）不可达（降级拒绝，不读脏、无 yaml 回落）",
              "稍后重试；持续失败联系平台值班（账单真源＝pg usage schema，不受 META_WRITER 管辖）"),
    ErrorCode(AUTH_FAILED, 401, "未携带 apikey 请求头，或 AppKey 无效/已吊销（网关 key-auth 拒绝）",
              "携带有效 AppKey 重试；新接入走 OA 审批后由授权登记发放",
              param=PARAM_APIKEY, related="apikey 请求头", source="gateway"),
    # 批量行/任务级机读码（无 HTTP 状态；落结果行 error 列与任务记录）
    ErrorCode(KV_READ_FAILED, None, "单键读取重试耗尽（该行失败，任务继续）",
              "重试该键或检查 KV 存储", scope="row"),
    ErrorCode(ROW_DECODE_FAILED, None, "结果行值损坏，重读后仍不可解析",
              "重试该键；持续失败联系平台值班", scope="row"),
    ErrorCode(KV_UNAVAILABLE, None, "全部键均不可读（KV 存储故障）",
              "稍后重试（任务可重提）", scope="job"),
    ErrorCode(JOB_STATE_INCONSISTENT, None, "任务状态与钉版信息不一致（异常残留）",
              "重试任务；持续失败联系平台值班", scope="job"),
    ErrorCode(SNAPSHOT_EMPTY_VERSION, None, "全量快照命中空版本（row_count=0）",
              "确认数据是否已上线后重提", scope="job"),
    ErrorCode(ROWCOUNT_MISSING, None, "版本元信息缺行数字段，无法对账",
              "检查同步链路的版本元信息", scope="job"),
    ErrorCode(ROWCOUNT_MISMATCH, None, "快照行数与版本元信息 row_count 不一致",
              "检查同步一致性后重提", scope="job"),
    # 价目管理（域9 计费管理；管理功能群一期 B 节）
    ErrorCode(PRICE_ITEM_NOT_FOUND, 404, "价目条目不存在（或未生效条目已撤回删除）",
              "核对 item_id；列表页刷新确认最新状态"),
    ErrorCode(PRICING_VALIDATION, 422, "价目校验拒绝（区间三不变量/月初约束/非法枚举/生效窗重叠/B 型暂不开放）",
              "按 message 人话报因修正表单后重提"),
    ErrorCode(PRICING_STATE_CONFLICT, 409, "价目条目已过期，自然到期无需停用",
              "已过期条目无需操作；停用仅对在窗/未生效条目开放"),
    ErrorCode(PRICING_DB_UNAVAILABLE, 503, "计费库未启用或未响应（compose.metadata.yml，项目名 oneserve-meta）",
              "起 meta 库后重试；价目为空期间出账走空价目试运行口径，不受影响"),
)

# ---------- 契约分组（gen_openapi 的 x-oneserve-errors 按服务原型的口径） ----------
# 原 scripts/gen_openapi.py 里 BASE/KV/HBASE/PROXY/GATEWAY 五张表在此同源重建，
# 码与相对顺序与既有契约逐条一致（tests/test_gen_openapi.py 守卫）。
BASE_CODES = (INVALID_JSON, UNKNOWN_TENANT, SERVICE_NOT_GRANTED, INTERNAL_ERROR)
KV_CODES = BASE_CODES + (INVALID_KEY, MAX_ITEMS_EXCEEDED, BAD_DESENSITIZE_RULE,
                         DATA_NOT_READY, GUARD_TIMEOUT)
HBASE_CODES = BASE_CODES + (INVALID_KEY, MAX_ITEMS_EXCEEDED, BAD_DESENSITIZE_RULE,
                            GUARD_TIMEOUT)   # hbase 路径无版本指针概念：data_not_ready 不收
PROXY_CODES = BASE_CODES + (INVALID_PROXY_PATH, PROXY_TRAVERSAL, BAD_PROXY_CONFIG,
                            UPSTREAM_UNREACHABLE, UPSTREAM_TIMEOUT)
GATEWAY_CODES = (AUTH_FAILED,)


def archetype_codes(archetype: str, source: str = "redis") -> tuple:
    """服务原型 → 契约错误码组（gen_openapi 单一取码入口；不发明码）。"""
    if archetype == "kv_lookup":
        return HBASE_CODES if source == "hbase" else KV_CODES
    return PROXY_CODES


# 快速索引（码 → 定义）＋ 唯一性断言（重复定义＝构建期即失败，防码表自相矛盾）
_BY_CODE: dict[str, ErrorCode] = {}
for _e in CODES:
    if _e.code in _BY_CODE:
        raise RuntimeError("错误码重复定义：" + _e.code)
    _BY_CODE[_e.code] = _e
del _e


# ══════════════════ 查询与信封构造 ══════════════════

def get(code: str) -> ErrorCode | None:
    """按码取定义；未知码返回 None（调用方须自行决定是否兜底，不静默发明码）。"""
    return _BY_CODE.get(code)


def is_known(code: str) -> bool:
    """码是否在本中心登记（机检「无散落字面量」的判据）。"""
    return code in _BY_CODE


def all_codes() -> list[str]:
    """全部登记码（稳定顺序＝定义顺序）。"""
    return [e.code for e in CODES]


def param_of(code: str) -> str:
    """码的规范出错参数名（未登记/无单一参数＝空串）。"""
    e = get(code)
    return e.param if e else ""


def http_status_of(code: str) -> int | None:
    """码的规范 HTTP 状态（非 HTTP 面／未登记＝None）。"""
    e = get(code)
    return e.http_status if e else None


def request_log_url(trace_id: str) -> str:
    """审计反查深链（既有门户深链：my.html?trace_id=… 会触发「我的接入」日志反查）。

    trace_id 为空＝无深链可给（返回空串，不造死链）；quote 防参数注入。
    """
    trace_id = (trace_id or "").strip()
    if not trace_id:
        return ""
    return f"{REQUEST_LOG_PAGE}?trace_id={quote(trace_id, safe='')}"


def docs_url(code: str) -> str:
    """码表锚点（A6 错误码中心页；每码一个锚点，真可达）。"""
    return f"{DOCS_PAGE}#{quote(code, safe='')}"


def envelope(code: str, message: str, *, trace_id: str = "", param: str | None = None,
             extra: dict | None = None) -> dict:
    """构造 §4.4 错误信封（字段名与 trace_middleware 实际注入保持同源）。

    - error/message 必带（既有字段，向后兼容）；
    - param：显式传入优先，否则取本码登记值（无＝不出现）；
    - traceId/request_log_url：有 trace_id 才带（无追踪号不造死链）；
    - docs_url：恒带（码表锚点，真可达）；
    - extra：端点特有字段（如批量 reason/retry_after），并入且不覆盖信封字段。
    """
    body: dict = {FIELD_ERROR: code, FIELD_MESSAGE: message}
    resolved_param = param if param is not None else param_of(code)
    if resolved_param:
        body[FIELD_PARAM] = resolved_param
    if trace_id:
        body[FIELD_TRACE_ID] = trace_id
        url = request_log_url(trace_id)
        if url:
            body[FIELD_REQUEST_LOG_URL] = url
    body[FIELD_DOCS_URL] = docs_url(code)
    if extra:
        for k, v in extra.items():
            body.setdefault(k, v)
    return body


def contract_rows(codes: list[str] | tuple[str, ...],
                  variant: str = "") -> list[dict]:
    """码 → 契约/文档行（gen_openapi 与 A6 页面共用；字段与既有契约表同构）。

    variant＝服务原型（kv/hbase/proxy）：命中 docs_variants 时用该原型的措辞
    （同码在不同档案含义有别，如 invalid_key 的 KV 域与 HBase rowkey 口径），
    未命中用基础文案——文档粒度不因集中而丢失，且仍出自本模块单一真源。
    """
    rows = []
    for c in codes:
        e = get(c)
        if e is None:
            raise KeyError("契约引用了未登记的错误码：" + c)
        meaning, solution = e.meaning, e.solution
        v = e.docs_variants.get(variant) if variant else None
        if v:
            meaning = v.get("meaning", meaning)
            solution = v.get("solution", solution)
        rows.append({"http_status": e.http_status, "code": e.code,
                     "meaning": meaning, "solution": solution,
                     "source": e.source, "related": e.related or e.param or "—"})
    return rows
