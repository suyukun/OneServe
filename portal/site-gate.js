/* ============================================================
 * OneServe 整站登录守卫 · site-gate.js（2026-10-15 设计稿 A2，W2 卡）
 * ------------------------------------------------------------
 * 背景：admin-gate.js 是管理页闸（admin 壳加载）；本文件是其「推广全站」形态——
 *   全部门户壳页（含 admin 壳页）由 build_portal 在 shell.head 以**第一个 defer
 *   脚本**注入（先于 data-format.js/api.js/pages.js——加载序是硬规矩，防 401
 *   落 api.js mock 降级出假「演示数据」页，设计稿 A2 点名）。login.html 与
 *   auth-callback.html 是 pre-token 页，不载本文件（防循环跳转）。
 *
 * 双机制（W2 任务口径：401 驱动＋本地 exp 预判；W1 后端闸本批默认 off，
 * off 态不改变现状浏览行为）：
 *   ① 401 驱动：全局 fetch 包装器——401 且信封 error=auth_required → 清 token
 *      ＋存回址 oneserve-auth-return → 跳 /login.html；非 auth_required 的 401
 *      （如下游 appkey 无效）原样透传。闸 off 时后端不回 401，包装器零干预。
 *   ② 本地 exp 预判（A3 增强）：本脚本执行即判（早于任何取数）——token 存在且
 *      exp 已过 → 存回址 → 跳 /login.html。无 token 静默放行（墙 off 时浏览
 *      行为不变；墙 on 时由 ① 在首个 401 处接管跳转）。
 *
 * 安全口径：纯本地判断，加载期零网络请求（零 fetch 口径与 admin-gate 一致）；
 * 回址只存本站 URL（redirectToLogin 存 location.href，login.html 侧另有
 * 「限本站 origin＋站内路径」校验，双向防开放跳转）。
 * 导出 root.SiteGate 供单测/selftest 直调（node 无 document 安全）。
 * ============================================================ */
(function (root) {
  'use strict';

  var TOKEN_KEY = 'oneserve-oidc-token';    /* 与 admin-gate.js/login.html 同 key 同口径 */
  var REFRESH_KEY = 'oneserve-oidc-refresh'; /* 权限方案 v1.1 裁决#1（档 2）：refresh_token（login.html 落入） */
  var RETURN_KEY = 'oneserve-auth-return';
  var LOGIN_PAGE = '/login.html';

  function lsGet(k) {
    try { return (root.localStorage && root.localStorage.getItem(k)) || ''; }
    catch (e) { return ''; }
  }
  function lsSet(k, v) {
    try {
      if (root.localStorage) {
        if (v) { root.localStorage.setItem(k, v); } else { root.localStorage.removeItem(k); }
      }
    } catch (e) { /* 存储不可用：按无令牌处理 */ }
  }

  function readToken() { return lsGet(TOKEN_KEY); }
  function readRefreshToken() { return lsGet(REFRESH_KEY); }
  function clearToken() { lsSet(TOKEN_KEY, ''); lsSet(REFRESH_KEY, ''); }

  /* JWT payload 解析（坏 token 按 {} 处理，不炸页面；与 admin-gate.jwtClaims 同法） */
  function jwtClaims(token) {
    try {
      var part = String(token || '').split('.')[1] || '';
      var b64 = part.replace(/-/g, '+').replace(/_/g, '/');
      var json = decodeURIComponent(escape(atob(b64)));
      return JSON.parse(json) || {};
    } catch (e) { return {}; }
  }

  /* 本地 exp 预判：exp 缺失＝不预判（交后端 401 裁决）；exp<=now＝已过期 */
  function isExpired(token) {
    var exp = Number(jwtClaims(token).exp);
    if (!exp || !isFinite(exp)) { return false; }
    return exp * 1000 <= Date.now();
  }

  /* ── 无感刷新（权限方案 v1.1 裁决#1·token 吊销档 2，2026-10-16）──
   * TTL 缩至 2h 后 access 临期（≤5 分钟）且本地有 refreshToken 时，经后端
   * /v1/admin/auth/refresh（Casdoor refresh_token grant，轮换式）换新 token，
   * 用户不被踢回登录页；刷新失败（refresh_token 被吊销/用户被禁用）才走
   * redirectToLogin（清 access＋refresh）。请求走 _nativeFetch（不经本包装器，
   * 防 401 递归）；无 refreshToken 时行为与旧版完全一致（临期/过期＝跳登录）。 */
  var REFRESH_AHEAD_MS = 5 * 60 * 1000;   /* 临期阈值：过期前 5 分钟 */
  var _refreshing = false;

  function needsRefresh(token) {
    var exp = Number(jwtClaims(token).exp);
    if (!exp || !isFinite(exp)) { return false; }
    return exp * 1000 - Date.now() <= REFRESH_AHEAD_MS;
  }

  function tryRefresh() {
    var token = readToken();
    var refresh = readRefreshToken();
    if (!token || !refresh || _refreshing || !needsRefresh(token)) {
      return Promise.resolve(false);
    }
    _refreshing = true;
    return _nativeFetch('/v1/admin/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refreshToken: refresh }),
    }).then(function (resp) {
      return resp.json().then(function (j) { return { ok: resp.ok, j: j }; });
    }).then(function (res) {
      _refreshing = false;
      if (res.ok && res.j && res.j.accessToken) {
        lsSet(TOKEN_KEY, res.j.accessToken);
        lsSet(REFRESH_KEY, res.j.refreshToken || refresh);
        return true;
      }
      redirectToLogin();   /* refresh 被拒＝登录态失效（禁用/吊销），清 token 跳登录 */
      return false;
    }).catch(function () {
      _refreshing = false;
      return false;        /* 网络错：不踢登录（下轮再试），后端 401 兜底 */
    });
  }

  function saveReturnUrl() {
    try { lsSet(RETURN_KEY, (root.location && root.location.href) || ''); } catch (e) { /* noop */ }
  }

  function redirectToLogin() {
    saveReturnUrl();
    clearToken();
    if (root.location) { root.location.replace(LOGIN_PAGE); }
  }

  /* ── 401 驱动：全局 fetch 包装器（必须先于 api.js/pages.js 装载——
   * build_portal 门禁断言此加载序）。响应原样透传给页面代码；auth 401 同时
   * 异步触发跳转（location.replace 不中断已开跑的页面逻辑，api.js 侧对
   * auth 401 不落 mock——见 api.js loadSection 配套改动）。
   *
   * 请求侧注入（2026-10-15 P0 热修）：整站墙 on 后 /v1/* 同站 API 不带
   * Authorization 必 401→循环踢回登录页。故发请求前注入本地 token：
   *   · 命中条件＝URL 含 '/v1/'（本站 API 路径段）且同站（相对路径，或经
   *     URL 解析 origin 等于本站）且调用方未显式带 Authorization 且本地有 token；
   *   · input 为 Request 对象时 effective headers＝init.headers 优先、
   *     否则 input.headers（与 fetch 合并语义一致）；注入落在 init 副本上，
   *     绝不改写调用方原对象；
   *   · 任一步异常静默照原样发（try/catch 兜底，不阻断页面取数）；
   *   · 外站 URL（跨 origin 绝对地址）绝不注入。 ── */
  var _nativeFetch = (typeof root.fetch === 'function') ? root.fetch.bind(root) : null;

  function requestUrlOf(input) {
    try {
      if (input && typeof input === 'object' && typeof input.url === 'string') { return input.url; }
      return String(input == null ? '' : input);
    } catch (e) { return ''; }
  }

  /* 同站 /v1/ API 判定：不含 '://' 视为相对路径（天然同站）；绝对 URL 须解析出
   * 与本站相同的 origin（location 缺 origin——如 node 单测桩——宁可不注入）。 */
  function isSameSiteV1(url) {
    try {
      url = String(url || '');
      if (url.indexOf('/v1/') < 0) { return false; }
      if (url.indexOf('://') < 0) { return true; }
      var loc = root.location;
      if (!loc || !loc.origin) { return false; }
      return new URL(url, loc.href).origin === loc.origin;
    } catch (e) { return false; }
  }

  function injectAuth(input, init) {
    try {
      if (!isSameSiteV1(requestUrlOf(input))) { return [input, init]; }
      var base = (init && init.headers) ? init.headers
        : (input && typeof input === 'object' && input.headers) ? input.headers : null;
      var headers = null;
      if (typeof Headers === 'function') {
        headers = new Headers(base || undefined);
        if (headers.has('Authorization')) { return [input, init]; }  /* 调用方自带：不覆盖 */
      } else if (base) {
        return [input, init];            /* 无 Headers 构造器且已有头：判不清不冒险 */
      }
      var token = readToken();
      if (!token) { return [input, init]; }
      var init2 = {};
      for (var k in (init || {})) { if (Object.prototype.hasOwnProperty.call(init, k)) { init2[k] = init[k]; } }
      if (headers) {
        headers.set('Authorization', 'Bearer ' + token);
        init2.headers = headers;
      } else {
        init2.headers = { Authorization: 'Bearer ' + token };
      }
      return [input, init2];
    } catch (e) { return [input, init]; }   /* 注入失败静默照原样发 */
  }

  if (_nativeFetch) {
    root.fetch = function (input, init) {
      var pair = injectAuth(input, init);
      input = pair[0];    /* 注入后的请求参数解构回原变量名（无注入时原样） */
      init = pair[1];
      return _nativeFetch(input, init).then(function (resp) {
        if (resp && resp.status === 401 && typeof resp.clone === 'function') {
          resp.clone().json().then(function (j) {
            if (j && j.error === 'auth_required') { redirectToLogin(); }
          }).catch(function () { /* 非 JSON 体：透传不处理 */ });
        }
        return resp;
      });
    };
  }

  /* ── 角色口（W5）：导航「仅 admin 可见」的实现位。
   * Casdoor 用户 tag 存 admin/operator/viewer（设计稿 B1）。W4 定稿（ADR-0016
   * Decision.2）：应用 tokenFormat=JWT 内嵌 User，**顶层 tag claim 即角色**——
   * 收口批 2a（2026-10-15）按定稿收窄：只认 tag（strip+lower 归一后等于 admin），
   * 与后端 auth_oidc.role_from_claims 完全同口径；roles/role/isAdmin 等其余
   * claim 一律不信（前端展示口与后端裁决口同一事实源）。纯展示解锁，鉴权
   * 仍在后端（require_role('admin')）。 ── */
  function isAdmin(claims) {
    claims = claims || {};
    return String(claims.tag || '').trim().toLowerCase() === 'admin';
  }

  /* 侧栏/壳内 [data-os-admin-only] 元素默认隐藏，admin token 在场才显示 */
  function revealAdminOnly() {
    if (typeof document === 'undefined' || !document) { return; }
    if (!isAdmin(jwtClaims(readToken()))) { return; }
    var nodes = document.querySelectorAll ? document.querySelectorAll('[data-os-admin-only]') : [];
    for (var i = 0; i < nodes.length; i++) { nodes[i].style.display = ''; }
  }

  /* ── ② 本地 exp 预判（脚本执行即判，早于页面任何取数）──
   * 无 token 静默放行（W1 off 态浏览行为不变；on 态由 401 驱动接管）。
   * 档 2 起：临期且带 refreshToken＝先试无感刷新（异步，不阻断页面），
   * 刷新失败才踢登录；无 refreshToken 的临期/过期行为与旧版一致。 */
  function boot() {
    var t = readToken();
    if (t && isExpired(t) && !readRefreshToken()) { redirectToLogin(); return; }
    if (t && readRefreshToken()) { tryRefresh(); }
    /* admin 导航解锁：defer 脚本执行时 body 未就绪，挂 DOMContentLoaded 兜底 */
    if (typeof document !== 'undefined' && document) {
      if (document.readyState === 'loading' && document.addEventListener) {
        document.addEventListener('DOMContentLoaded', revealAdminOnly);
      } else {
        revealAdminOnly();
      }
    }
    /* 常驻续期巡检（每 60s；仅临期＋有 refreshToken 才发请求，常态零网络）。
     * 仅浏览器态注册（node 单测无 document——定时器会让进程不退出，测试口径挂死）。 */
    if (typeof document !== 'undefined' && document
        && typeof root.setInterval === 'function') {
      root.setInterval(function () { tryRefresh(); }, 60 * 1000);
    }
  }
  boot();

  root.SiteGate = {
    TOKEN_KEY: TOKEN_KEY,
    REFRESH_KEY: REFRESH_KEY,
    RETURN_KEY: RETURN_KEY,
    LOGIN_PAGE: LOGIN_PAGE,
    readToken: readToken,
    readRefreshToken: readRefreshToken,
    clearToken: clearToken,
    jwtClaims: jwtClaims,
    isExpired: isExpired,
    isAdmin: isAdmin,
    revealAdminOnly: revealAdminOnly,
    saveReturnUrl: saveReturnUrl,
    redirectToLogin: redirectToLogin,
    tryRefresh: tryRefresh,
    needsRefresh: needsRefresh,
  };
})(typeof window !== 'undefined' ? window : globalThis);
