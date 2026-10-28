# -*- coding: utf-8 -*-
r"""对外 API 路由命名规范（docs/plans/API路由命名规范_提案v1.md §3）——机器闸单一事实来源。

维护者 2026-09-29 口径：「不是我给我看，而是系统上怎么约束」——规范不进代码等于没定。
本模块是该规范 R 条款的唯一代码实现，发布端点（ops_config.publish_service）、渲染器
（registry_render，R8 兜底）、门户前端（经 scripts/gen_route_naming_js.py 生成的
portal/admin/route-naming.js）与全部测试共用同一份常量与判定函数，禁止在第二处重写正则。

条款落位（提案 §3 全可机检；执行顺序 R1→R3→R4→R7，先结构后语义，报错可定位条款号）：
  R1 结构正则：/v1/svc/<slug>，slug 全小写字母数字连字符、字母开头、末字符字母或数字、
     禁连续连字符、总长 3~63（长度 R5 内含于此，单列报错信息定位）；
  R2 域码段：**建议不强制**（Q2 起步集 {ret,news,pub,com}，宽进严出）——仅当首段
     命中域码表时经 domain_hint 给提示信息，不阻断发布；
  R3 保留词：slug 任一连字符分词不得 ∈ RESERVED_SLUGS（ping/admin/batch/query/proxy
     等平台占用段——与 /v1/ping、/v1/admin/*、/v1/batch/*、/v1/query/* 语义撞车）；
  R4 动词黑名单：首段（域码位）外任一分词不得 ∈ VERB_TOKENS（本平台 KV 服务天然全是
     查询，名字里出现 query/get/list 是冗余）；
  R6 唯一性：注册表查询（find_route_occupant）——同 routePath 已被其他 service_id/
     其他归属（服务名）占用即拒，防跨归属静默合并版本史（提案 §5 G3）；
  R7 域码唯一与版本禁入：首段外任一分词不得 ∈ DOMAIN_CODES（防 ret-news-x 双域码
     混淆归属）、不得匹配 ^v\d+$（版本不进路径，Q3：授权语法已无版本位）；
  R8 渲染器复检：registry_render.render_query_routes 对不合规 svc 路由 fail-closed
     拒渲染（注册表被手编绕过发布端点时的兜底）。

生命周期功能（别名 90 天/墓碑 180 天/对账巡检）**本次不做**——预留位：
  - 别名：注册表条目 aliases: [{slug, expires_at}]（渲染器按幂等键 oneserve-svc-<slug> 出双路由）；
  - 墓碑：条目 tombstoned_at（offline 后 180 天禁跨归属复用 slug）；
  见提案 §4.3/§4.4，独立卡实现，本模块届时在 find_route_occupant 内接墓碑判定。
"""
from __future__ import annotations

import re

# ── 单一权威常量（提案 §3 代码块原样；前端经 gen_route_naming_js.py 同源导出） ──
ROUTE_PREFIX = "/v1/svc/"
ROUTE_RE_STR = r"^/v1/svc/(?!-)(?!.*--)[a-z][a-z0-9-]{1,61}[a-z0-9]$"
ROUTE_RE = re.compile(ROUTE_RE_STR)

# 域码表（Q2 起步集：银行零售/资讯/公告/公共）——封闭表，增删须治理审批；
# 本卡仅作建议提示（domain_hint），不作发布硬拦（宽进严出）。
DOMAIN_CODES = ("ret", "news", "pub", "com")

RESERVED_SLUGS = frozenset({
    "ping", "health", "healthz", "status", "version", "metrics", "debug",
    "admin", "batch", "portal", "proxy", "svc", "query", "internal", "public",
    "api", "test", "demo", "null", "none", "all", "new", "index",
})
VERB_TOKENS = frozenset({
    "get", "query", "list", "find", "fetch", "search", "read", "create", "add",
    "new", "update", "set", "modify", "edit", "delete", "del", "remove", "drop",
    "do", "exec", "run", "call", "send", "test", "mock", "temp", "todo",
})
VERSION_TOKEN_RE = re.compile(r"^v\d+$")


def slug_of(route: str) -> str:
    """路由 → slug 段（/v1/svc/ 后缀部分）。"""
    return str(route or "")[len(ROUTE_PREFIX):]


def check_route(route: str) -> str | None:
    """机检（R1→R3→R4→R7）：合规返回 None，违规返回条款号（'R1'/'R3'/'R4'/'R7'）。

    与 route_violation 同一判定（本函数是其条款号视图）——发布端点/测试共用，
    不存在第二份判定逻辑。
    """
    clause, _tok, _msg = route_violation(route)
    return clause


def route_violation(route: str) -> tuple[str | None, str | None, str | None]:
    """完整机检：返回 (条款号|None, 违例分词|None, 人话错误信息|None)。

    人话信息含改法（违规原因 + 怎么改），供发布端点 400 message 直出。
    """
    route = str(route or "").strip()
    if not route.startswith(ROUTE_PREFIX):
        return ("R1", None,
                "路由地址须以 /v1/svc/ 开头，形如 /v1/svc/ret-usr-basic（小写字母、数字、连字符）")
    slug = slug_of(route)
    if not ROUTE_RE.fullmatch(route):
        if "_" in slug or any(c.isupper() for c in slug):
            return ("R1", None,
                    "地址须全小写：只允许小写字母、数字、连字符（不要用下划线 _ 或大写字母），"
                    "多词用连字符分词，如 /v1/svc/ret-usr-basic")
        if slug.startswith("-") or slug.endswith("-") or "--" in slug:
            return ("R1", None,
                    "连字符只能用在分词中间：地址不能以 - 开头/结尾，也不能连续 --；"
                    "请清理首尾与连续连字符后重试")
        if len(slug) < 3 or len(slug) > 63:
            return ("R1", None,
                    f"地址长度须 3~63 个字符（当前 {len(slug)}）：太短易撞名、太长不便书写，"
                    "建议「域码-业务名」两三段，如 /v1/svc/ret-usr-basic")
        return ("R1", None,
                "地址格式不对：须为 /v1/svc/<小写字母开头、字母数字连字符、3~63 位>，"
                "如 /v1/svc/ret-usr-basic")
    toks = slug.split("-")
    hit = next((t for t in toks if t in RESERVED_SLUGS), None)
    if hit:
        return ("R3", hit,
                f"地址中的「{hit}」是平台保留词（ping/admin/batch/query/proxy/demo 等平台占用段），"
                "不能用作服务地址分词，请换成业务名词")
    hit = next((t for t in toks[1:] if t in VERB_TOKENS), None)
    if hit:
        return ("R4", hit,
                f"地址中的「{hit}」是动词（get/query/list/create 等黑名单）——本平台接口都是查询，"
                "名字里带动词是冗余；请用业务名词，如 /v1/svc/ret-usr-basic")
    hit = next((t for t in toks[1:] if t in DOMAIN_CODES), None)
    if hit:
        return ("R7", hit,
                f"地址第二段起不得再出现域码「{hit}」（一个地址只归一个业务域，"
                "防 ret-news-x 双域码混淆归属）；请只保留首段域码")
    hit = next((t for t in toks[1:] if VERSION_TOKEN_RE.fullmatch(t)), None)
    if hit:
        return ("R7", hit,
                f"地址中的「{hit}」是版本号——服务版本不进路径（授权语法已无版本位），"
                "地址里不要带 v1/v2 段；平台级版本已在 /v1 前缀里")
    return (None, None, None)


def route_error_message(route: str) -> str | None:
    """机检人话信息视图（合规返回 None）——发布端点 400 message 单一出口。"""
    return route_violation(route)[2]


def domain_hint(route: str) -> str | None:
    """R2 域码建议（不阻断）：首段命中域码表时给提示信息；未命中/不合规返回 None。"""
    route = str(route or "").strip()
    if check_route(route) is not None:
        return None
    first = slug_of(route).split("-")[0]
    if first in DOMAIN_CODES:
        return (f"命名建议已满足：地址带域码前缀 {first}-（域码表 {'/'.join(DOMAIN_CODES)}，"
                "建议但不强制）")
    return None


def find_route_occupant(route: str, *, sid: str, name: str,
                        console_entries: list[dict] | None = None,
                        runtime_services: dict | None = None) -> str | None:
    """R6 唯一性：同 routePath 是否已被**其他服务**占用 → 返回占用者服务名，否则 None。

    「其他服务」＝占用条目的 service_id/id 不同，或服务名不同（跨归属：防 A 团队字段
    变更静默挂进 B 团队服务的版本历史，提案 §5 G3）。同 service_id 同名重发＝同服务
    升版本（既有语义保留）。查全量：console 写区（含草稿，草稿同样占用地址）＋运行时
    services: 区（手编条目可能带与派生不同的 service_id）。

    生命周期预留位（本卡不做）：别名不占用新地址（同 upstream 双路由）；墓碑态 slug
    在 180 天窗口内视为被下线服务占用——届时在本函数内追加判定。
    """
    route = str(route or "").strip()
    for e in console_entries or []:
        if str((e or {}).get("route") or "") != route:
            continue
        if str(e.get("id") or "") != str(sid) or str(e.get("name") or "") != str(name):
            return str(e.get("name") or e.get("id") or "已有服务")
    for key, s in (runtime_services or {}).items():
        if not isinstance(key, str):        # 只看 latest 指针（与渲染器同口径，防复合键重复计）
            continue
        if str((s or {}).get("route") or "") != route:
            continue
        if (str(s.get("service_id") or "") != str(sid)
                or str(s.get("name") or "") != str(name)):
            return str(s.get("name") or s.get("service_id") or "已有服务")
    return None
