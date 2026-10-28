/* ============================================================
 * GENERATED FILE — DO NOT EDIT（改这里会被同步门禁打红）
 * 真源：service/app/route_naming.py（发布端点机检同一份常量）
 * 生成：python3 scripts/gen_route_naming_js.py（幂等）
 * 用途：发布台④步地址实时校验（publish.js WIZ.routeValid）——前后端同一正则，
 *       消「门户允许 _ / 后端拒绝」类双口径（提案 v1 §5 G2）。
 * ============================================================ */
(function (root) {
  'use strict';
  root.ROUTE_NAMING = {
  "source": "service/app/route_naming.py",
  "routePattern": "^/v1/svc/(?!-)(?!.*--)[a-z][a-z0-9-]{1,61}[a-z0-9]$",
  "routePrefix": "/v1/svc/",
  "domainCodes": [
    "ret",
    "news",
    "pub",
    "com"
  ],
  "reservedSlugs": [
    "admin",
    "all",
    "api",
    "batch",
    "debug",
    "demo",
    "health",
    "healthz",
    "index",
    "internal",
    "metrics",
    "new",
    "none",
    "null",
    "ping",
    "portal",
    "proxy",
    "public",
    "query",
    "status",
    "svc",
    "test",
    "version"
  ],
  "verbTokens": [
    "add",
    "call",
    "create",
    "del",
    "delete",
    "do",
    "drop",
    "edit",
    "exec",
    "fetch",
    "find",
    "get",
    "list",
    "mock",
    "modify",
    "new",
    "query",
    "read",
    "remove",
    "run",
    "search",
    "send",
    "set",
    "temp",
    "test",
    "todo",
    "update"
  ],
  "versionTokenPattern": "^v\\d+$",
  "hint": "域码建议（不强制）：ret/news/pub/com-业务名，如 /v1/svc/ret-usr-basic"
};
})(typeof window !== 'undefined' ? window : this);
