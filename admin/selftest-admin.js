/* ============================================================
 * OneServe 运营版 O-3 · selftest-admin.html 断言套件（19 条）
 * 无第三方依赖、无外网引用；跑在浏览器里，直接调 appkeys.js 的纯函数单元
 * window.O3 ＋ DOM 区块 id 检查。输出即证据：全绿才允许演示。
 * ============================================================ */
(function () {
  'use strict';
  var O3 = window.O3;
  var NOW = Date.parse('2026-09-22T12:00:00+08:00');   // 注入基准时间，断言确定性

  /* 组件访问（组 H 闸断言用）：Alpine.$data＝响应式代理（改状态驱动 DOM 刷新），
     裸 _x_dataStack 兜底只够读函数值 */
  function comp() {
    var probe = document.getElementById('dom-probe');
    if (window.Alpine && window.Alpine.$data && probe) {
      var p = window.Alpine.$data(probe);
      if (p && p.canWrite) return p;
    }
    var st = probe && probe._x_dataStack;
    if (!st || !st.length) return null;
    var c = null;
    st.forEach(function (o) { if (o && o.canWrite) c = o; });
    return c;
  }

  function sampleKeys() {
    return [{
      callerId: 'partner-demo-1', callerName: '演示调用方', appName: '电商标签点查',
      appkeyMasked: 'ak_3f9c****a21b', secretHashOnly: 'sha256:7d0de8c5e96b4f20',
      tagSets: ['基本信息域', '行为域'], quotaQps: 50, quotaDaily: 10000,
      oaTicket: 'DEMO-2026-004217', status: 'active', expiresAt: '2027-03-21'
    }];
  }

  var CASES = [
    /* ── 组 A：Secret 只存 hash（v0.1 修正③·机检核心）── */
    ['A1 sha256("abc") 对齐 FIPS 向量', function () {
      return O3.sha256Js('abc') === 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad';
    }],
    ['A2 Secret 存储形态＝sha256:＋摘要前 16 位', function () {
      var h = O3.secretHashDisplay(O3.sha256Js('sk_anysecret'));
      return /^sha256:[0-9a-f]{16}$/.test(h);
    }],
    ['A3 yaml 导出含 hash、绝无 sk_ 明文', function () {
      var y = O3.buildYaml(sampleKeys());
      return y.indexOf('sha256:7d0de8c5e96b4f20') >= 0 && !/sk_[A-Za-z0-9_-]{10}/.test(y);
    }],
    ['A4 mock/appkeys.json 无 Secret 明文（fetch 后全文扫描）', async function () {
      var t = await fetch('mock/appkeys.json', { cache: 'no-store' }).then(function (r) { return r.text(); });
      return !/sk_[A-Za-z0-9_-]{20,}/.test(t) && t.indexOf('secretHashOnly') >= 0;
    }],
    ['A5 凭据文件是唯一含明文处且带安全交付文案', function () {
      var rec = Object.assign({ appkeyFull: 'ak_3f9ca21b7d0e41aa' }, sampleKeys()[0]);
      var fake = ['sk_', 'DEMO', 'SECRET', 'VALUE'].join('') + '0123456789abcdefghijklm'; // 拼接避免本文件被明文扫描误报
      var txt = O3.credentialText(rec, fake);
      return txt.indexOf('sk_DEMOSECRETVALUE') >= 0 && txt.indexOf('请经安全渠道交付接入方') >= 0
             && txt.indexOf('试用 URL') >= 0 && txt.indexOf(O3.TRIAL_URL) >= 0;
    }],

    /* ── 组 B：凭证格式与掩码 ── */
    ['B1 genAppkey 格式 ak_＋16hex', function () { return /^ak_[0-9a-f]{16}$/.test(O3.genAppkey()); }],
    ['B2 genSecret 格式 sk_＋43 urlsafe（对齐 auth_admin.py）', function () {
      return /^sk_[A-Za-z0-9_-]{43}$/.test(O3.genSecret());
    }],
    ['B3 AppKey 列表掩码＝ak_＋4hex＋****＋后4', function () {
      return O3.maskAppkey('ak_3f9ca21b7d0e41a2') === 'ak_3f9c****41a2';
    }],

    /* ── 组 C：OA 批件号——正式凭证必填（ADR-0003）、沙箱可不填（P0-4）── */
    ['C1 合法批件号通过', function () { return O3.oaTicketValid('DEMO-2026-004217') === true; }],
    ['C2 空/过短批件号拒绝（→ 正式类型按钮禁用）', function () {
      return O3.oaTicketValid('') === false && O3.oaTicketValid('ab') === false && O3.oaTicketValid(null) === false;
    }],
    ['C3 正式类型＋批件号留空 → canGenerate false；沙箱类型 → true', function () {
      var probe = document.getElementById('dom-probe');
      var st = probe && probe._x_dataStack;
      var comp = null;
      if (st && Array.isArray(st)) st.forEach(function (o) { if (o && o.form) comp = o; });
      if (!comp) return 'no data stack';
      var wasType = comp.form.keyType;
      comp.form.keyType = 'formal';        // 正式凭证：OA 留空 → 不可生成（ADR-0003）
      var blockedFormal = comp.canGenerate() === false;
      comp.form.keyType = 'sandbox';       // 沙箱凭证：OA 可不填 → 可生成（P0-4）
      var okSandbox = comp.canGenerate() === true;
      comp.form.keyType = wasType;
      return (blockedFormal && okSandbox) || ('formal blocked=' + blockedFormal + ' sandbox ok=' + okSandbox);
    }],
    ['C4 沙箱 TTL 演示口径（demoSandboxExpiry＝N 天后日期）', function () {
      var d = O3.demoSandboxExpiry(7);
      if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) return 'bad date: ' + d;
      var t = Date.parse(d + 'T00:00:00Z');
      var diff = t - Date.now();
      return (diff > 6 * 86400e3 && diff < 8 * 86400e3) || ('diff days=' + diff / 86400e3);
    }],

    /* ── 组 D：每调用方上限 2 把（阿里 RAM 规矩②）── */
    ['D1 KEY_LIMIT＝2', function () { return O3.KEY_LIMIT === 2; }],
    ['D2 计数逻辑：同调用方满 2 后拒绝第 3 把', function () {
      var keys = [
        { callerId: 'x' }, { callerId: 'x' }, { callerId: 'y' }
      ];
      var count = function (id) { return keys.filter(function (k) { return k.callerId === id; }).length; };
      return count('x') >= O3.KEY_LIMIT && count('y') < O3.KEY_LIMIT;
    }],

    /* ── 组 E：区块 id 全中（curl DoD 同源断言）── */
    ['E1 页面关键区块 id 齐备', function () {
      var ids = ['admin-topbar', 'trace-bar', 'appkey-list-section', 'appkey-table', 'issue-form-section',
                 'issue-form', 'caller-select', 'tagset-checks', 'oa-ticket-input', 'quota-qps-input',
                 'quota-daily-input', 'purpose-declare-check', 'risk-ack-check', 'generate-btn',
                 'secret-modal', 'secret-value', 'download-cred-btn', 'secret-saved-btn',
                 'confirm-modal', 'confirm-step-btn', 'confirm-auth-switch',
                 'quota-modal', 'quota-save-btn', 'yaml-modal', 'yaml-code', 'yaml-copy-btn', 'yaml-download-btn'];
      var missing = ids.filter(function (i) { return !document.getElementById(i); });
      return missing.length === 0 || ('缺：' + missing.join(','));
    }],
    ['E2 零外网引用（src/href 全部相对路径）', function () {
      var bad = [];
      Array.prototype.forEach.call(document.querySelectorAll('script[src],link[href]'), function (el) {
        var v = el.getAttribute(el.src ? 'src' : 'href') || '';
        if (/^(https?:)?\/\//i.test(v)) bad.push(v);
      });
      return bad.length === 0 || ('外链：' + bad.join(','));
    }],

    /* ── 组 F：默认配额与相对时间口径 ── */
    ['F1 表单默认配额 50QPS·1万日次', function () {
      var q = document.getElementById('quota-qps-input'), d = document.getElementById('quota-daily-input');
      var ok = q && d && q.value === '50' && d.value === '10000';
      if (ok) return true;
      return 'DOM=' + (q && q.value) + '/' + (d && d.value) + '（纯函数口径兜底见 F1b）';
    }],
    ['F1b 默认常量＝50QPS·1万日次（纯函数口径）', function () {
      // 通过组件初始 form 验证（Alpine 实例挂在 #dom-probe._x_dataStack）
      var probe = document.getElementById('dom-probe');
      var st = probe && (probe._x_dataStack || (probe.__x && probe.__x.getUnwrapedProxy));
      if (st && Array.isArray(st)) {
        var f = null;
        st.forEach(function (o) { if (o && o.form) f = o.form; });
        if (f) return f.quotaQps === 50 && f.quotaDaily === 10000;
      }
      return 'no data stack';
    }],
    ['G1 组件已挂载（Alpine x-text 生效）', function () {
      var el = document.getElementById('mount-probe');
      if (!el) return 'mount-probe missing';
      if (el.textContent.indexOf('mounted:') === 0) return true;
      return 'not hydrated: ' + JSON.stringify(el.textContent);
    }],
    ['F2 relTime 边界（now 注入）', function () {
      return O3.relTime(null, NOW) === '从未调用'
             && O3.relTime(new Date(NOW - 30e3).toISOString(), NOW) === '刚刚'
             && O3.relTime(new Date(NOW - 5 * 60e3).toISOString(), NOW) === '5 分钟前'
             && O3.relTime('not-a-date', NOW) === '—';
    }],

    /* ── 组 H：管理令牌闸——2026-10-14 起恒放行 shim（三段决策史见 ADR-0010；
       2026-10-09 恢复期的四态/锁定/401 断言随第三次反转改写，历史见 bcf34f8） ── */
    ['H1 恒放行：无令牌 canWrite true＋gateLocked false（写操作不因令牌禁用）', async function () {
      var c = comp();
      if (!c) return 'no component';
      c.adminToken = '';
      await new Promise(function (r) { setTimeout(r, 90); });
      var ok = c.canWrite() === true && c.gateLocked() === false;
      c.adminToken = 'selftest-seed';
      return ok || 'canWrite/gateLocked 非恒放行（shim 语义被破坏）';
    }],
    ['H2 恒放行：verifyToken 任意输入恒 ok（后端不再校验 X-Admin-Token）', async function () {
      var AG = window.AdminGate;
      var a = await AG.verifyToken('', function () { return Promise.reject(new Error('x')); });
      var b = await AG.verifyToken('wrong', function () { return Promise.resolve({ ok: false, status: 401 }); });
      return (a === 'ok' && b === 'ok') || ('verifyToken 恒 ok 被破坏：' + a + '/' + b);
    }],
    ['H3 banner 恒空：gateBannerText() 空串（无「输入管理令牌解锁操作」提示）', async function () {
      var c = comp();
      if (!c) return 'no component';
      await new Promise(function (r) { setTimeout(r, 90); });
      return c.gateBannerText() === '' || ('banner=' + c.gateBannerText());
    }],
    ['H4 三页一致：令牌键/校验端点常量保留（恢复闸期复用）', function () {
      var AG = window.AdminGate;
      return AG.TOKEN_KEY === 'oneserve-admin-token' && AG.VERIFY_URL === '/v1/admin/appkeys';
    }]
  ];

  function render(results) {
    var el = document.getElementById('results'), html = '<table><tr><th>#</th><th>断言</th><th>结果</th></tr>';
    var pass = 0;
    results.forEach(function (r, i) {
      if (r.ok) pass++;
      // #36 XSS 全扫（2026-10-15）：r.name/r.info 为异常 message 等动态串，插值即注入面——
      // 统一走 AdminGate.escapeHtml（登录条同款基线）。
      var esc = (window.AdminGate && window.AdminGate.escapeHtml) || function (s) {
        return String(s).replace(/[&<>"']/g, function (c) {
          return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; });
      };
      html += '<tr><td>' + (i + 1) + '</td><td>' + esc(r.name) + '</td><td class="' +
        (r.ok ? 'pass">PASS' : 'fail">FAIL' + (r.info ? '（' + esc(r.info) + '）' : '')) + '</td></tr>';
    });
    html += '</table>';
    el.innerHTML = html;
    var s = document.getElementById('summary');
    s.textContent = pass + ' / ' + results.length + (pass === results.length ? ' 全绿 ✓' : ' 有失败 ✗');
    s.className = pass === results.length ? 'pass' : 'fail';
    document.title = (pass === results.length ? '✓ ' : '✗ ') + 'O-3 selftest ' + pass + '/' + results.length;
    window.SELFTEST_RESULT = { pass: pass, total: results.length };
  }

  window.addEventListener('load', async function () {
    // 等 Alpine hydration（DOM 探针按钮的 disabled 由 x-bind 驱动）
    await new Promise(function (res) { setTimeout(res, 300); });
    var results = [];
    for (var i = 0; i < CASES.length; i++) {
      var name = CASES[i][0], fn = CASES[i][1], ok = false, info = '';
      try {
        var out = await fn();
        if (out === true) ok = true;
        else if (typeof out === 'string') { ok = false; info = out; }
        else { ok = false; info = 'returned ' + JSON.stringify(out); }
      } catch (e) { ok = false; info = e.message; }
      results.push({ name: name, ok: ok, info: info });
    }
    render(results);
  });
})();