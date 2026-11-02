/* ============================================================
 * OneServe 管理台 · admin-gate.js —— 无登录模式空壳（v0.1.0）
 * ------------------------------------------------------------
 * 无登录模式决策（维护者 2026-10-28 拍板）：开源版拆除人机登录，
 * 管理台公开可访；AppKey 凭证体系仍在 /v1/admin/* 生效（机器鉴权不回退）。
 *
 * 本文件保留文件名（各管理页 HTML 有 script 引用，避免逐页删标签），内容为
 * 最小空壳：不做任何拦截/跳转/令牌校验，加载期零请求、零 DOM 注入。
 * 历史形态（管理令牌闸三段史＋OIDC 登录闭环）见 git 史与 ADR-0010/0015；
 * 如未来恢复登录，回 git 史还原实现，导出面已按页面消费习惯保留。
 *
 * 保留的最小导出面（页面 js 消费契约，见 admin/*.js）：
 *   · gateMixin()——canWrite 恒 true/gateLocked 恒 false/banner 恒空；
 *   · adminHeaders()——恒空对象（无凭证概念，调用方合并点零改动）；
 *   · verifyToken()——恒 'ok'（不发请求）；readToken/storeToken——空操作；
 *   · escapeHtml()——selftest 系渲染器的转义基线（单一来源）；
 *   · TOKEN_KEY/VERIFY_URL 常量（恢复闸期复用）。
 * ============================================================ */
(function (root) {
  'use strict';

  var TOKEN_KEY = 'oneserve-admin-token';
  var VERIFY_URL = '/v1/admin/appkeys';

  function readToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }
  function storeToken(t) {
    try {
      if (t) { sessionStorage.setItem(TOKEN_KEY, t); } else { sessionStorage.removeItem(TOKEN_KEY); }
    } catch (e) { /* 存储不可用：按无令牌处理 */ }
  }

  /* 请求头统一入口（各页 apiFetch 合并点）：无登录模式＝恒空对象。 */
  function adminHeaders() { return {}; }

  /* 令牌校验：恒放行（无登录模式后端不校验人机令牌；保留签名供 selftest 直调）。 */
  function verifyToken(token, fetcher) {
    return Promise.resolve('ok');
  }

  function gateMixin() {
    return {
      adminToken: '',
      tokenState: 'ok',      /* 无登录模式：不存在锁定/校验态 */
      tokenError: '',
      devHint: null,
      canWrite: function () { return true; },
      gateLocked: function () { return false; },
      gateBannerText: function () { return ''; },
      bootGate: function () {},
      saveToken: function () {},
      clearToken: function () {},
      loadDevHint: function () { return Promise.resolve(null); },
    };
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  root.AdminGate = {
    gateMixin: gateMixin,
    adminHeaders: adminHeaders,
    verifyToken: verifyToken,
    readToken: readToken,
    storeToken: storeToken,
    TOKEN_KEY: TOKEN_KEY,
    VERIFY_URL: VERIFY_URL,
    escapeHtml: escapeHtml,
  };
})(typeof window !== 'undefined' ? window : globalThis);
