/* ============================================================
 * OneServe 管理页 · 管理令牌闸——恒放行 shim（2026-10-14 第三次反转）
 * ------------------------------------------------------------
 * 沿革（拆一次建一次都有决策留痕，详见 ADR-0010）：
 *   · 2026-09-23 维护者 拍板整体拆除（本文件曾为恒放行 shim，35ab1aa）；
 *   · 2026-10-09 维护者 按生产目标拍板恢复——管理 API 不能裸奔（bcf34f8：
 *     X-Admin-Token 头，后端 admin_api._admin_gate 路由级统一过闸，
 *     /v1/admin/* 与 monitor 三端点全覆盖；31ed94c 修解锁条全站接线；
 *     306fb58 占位值改 admin321）；
 *   · 2026-10-14 维护者 亲测两轮被卡（①全站无令牌输入入口→恒 401；②admin321
 *     改值后跨标签页 sessionStorage 失忆再卡），拍板：现在不需要管理令牌，拆；
 *     未来统一做系统登录页（已记待办）。部署到公司环境前必须先有登录页或
 *     恢复闸＝硬前提（ADR-0010）。
 *
 * 当前语义（恒放行 shim，机制文件保留、闸逻辑放行）：
 *   · canWrite() 恒 true / gateLocked() 恒 false / gateBannerText() 恒空串
 *     ——写按钮不再因令牌被禁用，顶栏无「只读」提示；
 *   · verifyToken() 恒 'ok'（后端不再校验，任何令牌都算通过；不发请求）；
 *   · adminHeaders()：有残留令牌就照发（后端不校验）、无令牌即空对象——
 *     各页 apiFetch 合并点零改动；
 *   · 不注入解锁条：autoWireUnlockBar 短路（ensureUnlockBar 保留无调用方）；
 *   · bootGate/loadDevHint/saveToken/clearToken 空操作（不再碰 sessionStorage
 *     或 /v1/portal/admin-token-hint）；
 *   · 导出 API 面（gateMixin/adminHeaders/verifyToken/readToken/storeToken/
 *     TOKEN_KEY/VERIFY_URL）原样保留——node 直调用例与未来登录页时代低成本
 *     复用/恢复（恢复＝回 2026-10-09 形态，commit bcf34f8/31ed94c）。
 *
 * 【第四段 2026-10-14 晚：OIDC 登录闭环（Casdoor，ADR-0010 硬前提兑现起点）】
 *   · adminHeaders() 改发 Authorization: Bearer <oidc token>（token 存
 *     localStorage，跨标签页不失忆——2026-10-14 sessionStorage 失忆教训不重演）；
 *   · 401 auth_required 自动跳统一登录页（fetch 包装器全站拦截，免逐页改）；
 *   · 回跳页 auth-callback.html（同目录）换 token 后回到原址。
 *   · 2026-10-15 品牌登录页（维护者 拍板：用户不见 Casdoor 门面）——loginUrl 改指
 *     门户 /login.html（8000 内自建：账号密码 → 后端 /v1/admin/auth/login 密码流
 *     换 token），浏览器不再直连 19000（Casdoor 仅后端依赖）；登出不再打 Casdoor
 *     /api/logout（密码流无浏览器 SSO 会话可杀）。授权码流 auth-callback.html 与
 *     /v1/admin/auth/callback 端点保留（后备通道，不再主用）。
 * ============================================================ */
(function (root) {
  'use strict';

  var TOKEN_KEY = 'oneserve-admin-token';
  var VERIFY_URL = '/v1/admin/appkeys';

  /* ── OIDC 登录闭环（2026-10-14 Casdoor 集成）──
     2026-10-15 起 Casdoor 只作后端依赖（密码流在 auth_oidc 后端换 token），浏览器
     零直连：本文件不再持有 19000/authorize/client_id 常量，登录页＝门户自建
     /login.html（后端契约 POST /v1/admin/auth/login；compose 口径见 auth_oidc）。 */
  var OIDC_TOKEN_KEY = 'oneserve-oidc-token';
  var OIDC_REFRESH_KEY = 'oneserve-oidc-refresh';   /* v1.1 裁决#1：登出须连带清（site-gate 同 key） */
  var OIDC_RETURN_KEY = 'oneserve-auth-return';
  var LOGIN_PAGE = '/login.html';

  function lsGet(k) {
    try { return (typeof localStorage !== 'undefined' && localStorage) ? localStorage.getItem(k) : null; }
    catch (e) { return null; }
  }
  function lsSet(k, v) {
    try {
      if (typeof localStorage !== 'undefined' && localStorage) {
        if (v) { localStorage.setItem(k, v); } else { localStorage.removeItem(k); }
      }
    } catch (e) { /* 存储不可用：按无令牌处理 */ }
  }
  function readOidcToken() { return lsGet(OIDC_TOKEN_KEY) || ''; }
  function storeOidcToken(t) { lsSet(OIDC_TOKEN_KEY, t || ''); }

  function loginUrl() {
    /* 2026-10-15 品牌登录页：门户内自建页（8000 口径）——账号密码经后端
     * /v1/admin/auth/login 走 Casdoor 密码流换 token，用户全程不出本站。 */
    return LOGIN_PAGE;
  }
  /* 401 auth_required → 统一登录页（登录页存回址，成功后回原址；login.html 与
   * auth-callback.html 均不加载本文件，无循环跳转风险） */
  function redirectToLogin() {
    try { lsSet(OIDC_RETURN_KEY, (root.location && root.location.href) || ''); } catch (e) { /* noop */ }
    if (root.location) { root.location.href = loginUrl(); }
  }

  /* fetch 全站包装：管理写操作 401 auth_required 自动跳登录（免逐页接线）。
   * 加载期不发任何请求（shim 断言 fetches==0 口径不变）；非 auth_required 的
   * 401（如下游 appkey 无效）原样透传，页面既有错误规整不受影响。 */
  var _nativeFetch = (typeof root.fetch === 'function') ? root.fetch.bind(root) : null;
  if (_nativeFetch) {
    root.fetch = function (input, init) {
      return _nativeFetch(input, init).then(function (resp) {
        if (resp.status === 401 && typeof resp.clone === 'function') {
          resp.clone().json().then(function (j) {
            if (j && j.error === 'auth_required') { redirectToLogin(); }
          }).catch(function () { /* 非 JSON 体：透传不处理 */ });
        }
        return resp;
      });
    };
  }

  function readToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }
  function storeToken(t) {
    try {
      if (t) { sessionStorage.setItem(TOKEN_KEY, t); } else { sessionStorage.removeItem(TOKEN_KEY); }
    } catch (e) { /* 存储不可用：按无令牌处理 */ }
  }

  /* fetch 统一带头入口（各页 apiFetch 构造 headers 时合并本返回值）。
   * 2026-10-14 晚起：OIDC 登录闭环——有 Casdoor token 发 Authorization Bearer，
   * 无 token 即空对象（写操作会被后端 401 auth_required 拦下并自动跳登录）。 */
  function adminHeaders() {
    var t = readOidcToken();
    return t ? { 'Authorization': 'Bearer ' + t } : {};
  }

  /* 令牌校验：恒放行（2026-10-14 拆除后后端对任意令牌都 200，校验已无意义）。
   * 保留签名与导出（selftest/单测直调）；恢复闸时还原四态实现（bcf34f8）。 */
  function verifyToken(token, fetcher) {
    return Promise.resolve('ok');
  }

  /* 动态解锁条（2026-10-09 恢复期实现，31ed94c 接线）：恒放行期无调用方——
   * autoWireUnlockBar 已短路、bootGate 已空操作。整段保留供恢复闸期复用。 */
  function ensureUnlockBar() {
    if (document.getElementById('admin-gate-unlockbar')) { return; }
    var bar = document.createElement('div');
    bar.id = 'admin-gate-unlockbar';
    bar.setAttribute('style',
      'position:sticky;top:0;z-index:9999;background:#8a6d3b;color:#fff;' +
      'padding:8px 14px;font:13px/1.5 system-ui;cursor:pointer;text-align:center;');
    bar.textContent = '管理端已锁定（只读）——点此输入管理令牌解锁操作';
    bar.addEventListener('click', function () {
      var t = root.prompt('输入管理令牌（开发占位值见 .env.example；生产环境由部署方配置 ADMIN_API_TOKEN）', '');
      if (t === null) { return; }
      t = String(t).trim();
      verifyToken(t).then(function (state) {
        if (state === 'ok') { storeToken(t); root.location.reload(); }
        else if (state === 'bad') { root.alert('管理令牌不正确（401）'); }
        else { root.alert('校验通道不可用（' + state + '）：稍后重试或检查服务状态'); }
      });
    });
    (document.body || document.documentElement).appendChild(bar);
  }

  function gateMixin() {
    return {
      adminToken: '',
      tokenState: 'ok',      /* 恒放行：不再有锁定/校验态（恢复闸时还原四态） */
      tokenError: '',
      devHint: null,

      /* 写操作门禁：恒放行（2026-10-14 拆除，ADR-0010）。 */
      canWrite: function () { return true; },
      gateLocked: function () { return false; },
      /* 顶栏 banner 文案：恒空（无「只读」提示）。 */
      gateBannerText: function () { return ''; },
      /* boot 期令牌恢复＋校验：恒放行下无事可做（空操作）。 */
      bootGate: function () {},
      saveToken: function () {},
      clearToken: function () {},
      /* 开发占位令牌提示：闸已拆不再拉取（/v1/portal/admin-token-hint 端点保留）。 */
      loadDevHint: function () { return Promise.resolve(null); },
    };
  }

  root.AdminGate = {
    gateMixin: gateMixin,
    adminHeaders: adminHeaders,
    verifyToken: verifyToken,
    readToken: readToken,
    storeToken: storeToken,
    TOKEN_KEY: TOKEN_KEY,
    VERIFY_URL: VERIFY_URL,
    /* OIDC 登录闭环（2026-10-14）：token 读写/登录跳转（auth-callback.html 与
     * selftest 直调；页面 js 一律经 adminHeaders()，不直碰这些） */
    readOidcToken: readOidcToken,
    storeOidcToken: storeOidcToken,
    loginUrl: loginUrl,
    oidcLogout: oidcLogout,
    SIDEBAR_AUTH_ID: SIDEBAR_AUTH_ID,
    OIDC_TOKEN_KEY: OIDC_TOKEN_KEY,
    /* #36（2026-10-15 XSS 全扫）：escapeHtml 升为导出面——selftest 系渲染器统一
     * 复用本基线函数（新登录条即此口径），不再各自散写或裸插值。 */
    escapeHtml: escapeHtml,
  };

  /* ── 侧栏认证区（2026-10-15 T2 清账：顶部状态条拆除，登出并入侧栏底）──
   * 未登录：「去登录」链接；已登录：「用户名 · 登出」。
   * 注入目标：#os-sidebar-auth（sidebar.html 预留容器）。
   * 本条不发任何请求（加载期零 fetch 口径不变）；无 document 环境安全跳过。 */
  var SIDEBAR_AUTH_ID = 'os-sidebar-auth';

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function jwtClaims(token) {
    try {
      var part = String(token).split('.')[1] || '';
      var b64 = part.replace(/-/g, '+').replace(/_/g, '/');
      var json = decodeURIComponent(escape(atob(b64)));
      return JSON.parse(json) || {};
    } catch (e) { return {}; }   /* 坏 token 按无信息处理，不炸页面 */
  }

  function loginDisplayName() {
    var claims = jwtClaims(readOidcToken());
    return claims.name || claims.preferred_username || '已登录';
  }

  function ensureSidebarAuth() {
    if (typeof document === 'undefined' || !document) { return; }
    var container = document.getElementById(SIDEBAR_AUTH_ID);
    if (!container) { return; }  /* 侧栏未渲染则跳过（selftest 等无壳页面） */
    if (container.getAttribute('data-wired')) { return; }  /* 防重复注入 */
    container.setAttribute('data-wired', '1');
    if (readOidcToken()) {
      container.innerHTML = '<span style="color:var(--text-muted);font-size:var(--fs-caption);">' +
        escapeHtml(loginDisplayName()) + '</span> ' +
        '<a href="#" id="os-sidebar-logout" style="color:var(--status-info);font-size:var(--fs-caption);">登出</a>';
      var out = container.querySelector('#os-sidebar-logout');
      if (out && out.addEventListener) {
        out.addEventListener('click', function (ev) {
          if (ev && ev.preventDefault) { ev.preventDefault(); }
          oidcLogout();
        });
      }
    } else {
      container.innerHTML = '<a href="' + escapeHtml(loginUrl()) + '" ' +
        'style="color:var(--status-info);font-size:var(--fs-caption);">去登录</a>';
    }
  }
  function oidcLogout() {
    /* ADR-0018（2026-10-19）：登录改授权码流后浏览器持有 Casdoor 同源会话 cookie
     * （casdoor_session_id）——登出必须调 /api/logout 杀会话（实测无参 GET 即杀），
     * 否则「退出」后跳登录页会被静默自动登录。fire-and-forget：失败不阻断本地清理。 */
    try {
      fetch('/api/logout', { credentials: 'include', cache: 'no-store' })
        .catch(function () { /* SLO 尽力而为：本地态照清 */ });
    } catch (e) { /* 同上 */ }
    storeOidcToken('');
    lsSet(OIDC_REFRESH_KEY, '');   /* 登出连带清 refresh_token（防旧登录态续期复活） */
    if (root.location && root.location.reload) { root.location.reload(); }
  }

  function autoWireSidebarAuth() {
    /* DOM 就绪即注入侧栏认证区；loading 态挂 DOMContentLoaded 兜底（无 document 跳过）。 */
    if (typeof document === 'undefined' || !document) { return; }
    if (document.readyState === 'loading') {
      if (document.addEventListener) {
        document.addEventListener('DOMContentLoaded', ensureSidebarAuth);
      }
    } else {
      ensureSidebarAuth();
    }
  }
  /* ── 脚本尾部自动接线（2026-10-14 上午修复「解锁条全站永不渲染」，31ed94c）──
   * 当日下午闸拆除（第三次反转）：本函数短路——恒放行态不注入解锁条、不校验、
   * 不清残令牌。接线形状（DOM ready 分支＋DOMContentLoaded 兜底＋无条件自调用）
   * 保留在 git 史与恢复路径（ADR-0010）；恢复闸时删去下方 return 即回到上午形态。
   * Node 等无 document 环境安全跳过（selftest/单测直调本文件）。 */
  function autoWireUnlockBar() {
    return;   /* 2026-10-14 恒放行：不注入解锁条（短路） */
  }
  autoWireUnlockBar();
  autoWireSidebarAuth();   /* 2026-10-15 T2：侧栏认证区注入（顶部状态条已拆除） */
})(typeof window !== 'undefined' ? window : globalThis);
