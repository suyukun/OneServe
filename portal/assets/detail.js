/* ============================================================
 * OneServe 门户 · P0-2 详情页组件（assets/detail.js，新增文件）
 * 数据契约：portal/openapi/details/<service_id>.json（构建期由
 * scripts/gen_openapi.py 从 service/services.yaml 等真源生成；壳产出在
 * portal/catalog/<service_id>.html，fetch 用 ../openapi/ 前缀——单服务根＝门户根）；
 * 详情页只 fetch 静态文件——MPA 纪律：运行时零计算、零模板拼装。
 * 数据缺失 → error 态（契约数据暂不可用＋重试），不降级 mock、不造假。
 * 目录页跳转（svcHref）在 assets/pages.js 的 catalogPage 内实现。
 * ============================================================ */
(function () {
  'use strict';

  function detailPage() {
    return {
      svc: null,
      loadError: false,

      load: function () {
        var self = this;
        this.loadError = false;
        var m = location.pathname.match(/catalog\/([A-Za-z0-9_-]+)\.html/);
        var sid = m ? m[1] : new URLSearchParams(location.search).get('service');
        if (!sid) { self.loadError = true; return; }
        fetch('../openapi/details/' + sid + '.json', { cache: 'no-store' })
          .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
          .then(function (data) { self.svc = data; })
          .catch(function () { self.loadError = true; });
      },

      init: function () { this.load(); },

      retryLoad: function () { this.load(); },

      /* ── #46 错误码到动作（2026-10-15）：401/429/配额类错误行内跳转的静态分类。
       * 依据＝错误信封同源 sidecar 的 code/http_status（零新接口、零后端改动）。
       * ux-polish（2026-11-02）：原单一「联系运营组开通」（docs 锚）随 docs 页下线拆分——
       *   凭证类（401、credential_* 机读码）→ 领/重签凭证（admin/appkeys.html）；
       *   授权类（403、unknown_tenant、*_not_granted）→ 查授权全景（grants.html；
       *   开通仍按「解决方案」列找平台运营组——授权登记无站内自助页）；
       *   限流/配额类（429、quota_* 机读码）→ 查看配额（my.html）。
       * 其余码不挂按钮——按「解决方案」列处理，不造无处可去的假动作。 ── */
      errActionKey: function (e) {
        if (!e) return false;
        var code = String(e.code || '');
        return e.http_status === 401 ||
          code === 'credential_expired' ||
          code === 'credential_rotated_expired' || code === 'credential_disabled';
      },
      errActionGrant: function (e) {
        if (!e) return false;
        var code = String(e.code || '');
        return e.http_status === 403 ||
          code === 'unknown_tenant' || code === 'service_not_granted' ||
          code === 'fields_not_granted';
      },
      errActionQuota: function (e) {
        if (!e) return false;
        var code = String(e.code || '');
        return e.http_status === 429 || code === 'rate_limited' ||
          code.indexOf('quota_') === 0;
      },

      trialUrl: function () {
        return (this.svc && this.svc.trial_url ? '../' + this.svc.trial_url : '../trial.html') +
          '?service=' + encodeURIComponent(this.svc.service_id);
      },

      exampleText: function () {
        return this.svc ? this.svc.example.command.join('\n') : '';
      },

      copyExample: function (btn) {
        var text = this.exampleText();
        var done = function () { btn.textContent = '已复制'; setTimeout(function () { btn.textContent = '复制 curl'; }, 1600); };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, done);
        } else {
          var ta = document.createElement('textarea');
          ta.value = text; document.body.appendChild(ta); ta.select();
          try { document.execCommand('copy'); } catch (e) { /* 剪贴板不可用：提示即结束 */ }
          document.body.removeChild(ta); done();
        }
      }
    };
  }

  document.addEventListener('alpine:init', function () {
    window.Alpine.data('detailPage', detailPage);
  });
})();
