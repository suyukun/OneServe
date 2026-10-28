/* P0-3 试用台 DOM 断言驱动 v2（自验工具；生产页面零引用）
 * 静态断言针对首屏真实 DOM；template 惰性区块（x-if 未激活）改查构建产物源码；
 * 交互断言驱动真栈：未命中→空态引导→一键演示号→真实命中→历史；HTTP 空凭证→网关真 401→错误动作条。 */
(function () {
  'use strict';
  var ROWS = [];
  function assert(name, actual, expect, pass) {
    ROWS.push({ n: ROWS.length + 1, name: name, actual: String(actual).slice(0, 120),
                expect: String(expect).slice(0, 120), ok: !!pass });
    render();
  }
  function render() {
    var tb = document.querySelector('#rt tbody');
    // #36 XSS 全扫（2026-10-15）：r.name/r.actual 含 DOM 文本与服务端回显串（如网关错误
    // message），插值即注入面——escapeHtml 基线（与 admin-gate 登录条同款）。
    var esc = (window.AdminGate && window.AdminGate.escapeHtml) || function (s) {
      return String(s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    };
    tb.innerHTML = ROWS.map(function (r) {
      return '<tr><td>' + r.n + '</td><td>' + esc(r.name) + '</td><td>' + esc(r.actual) + '</td><td>' + esc(r.expect) +
             '</td><td class="' + (r.ok ? 'pass' : 'fail') + '">' + (r.ok ? 'PASS' : 'FAIL') + '</td></tr>';
    }).join('');
  }
  function q(doc, sel) { return doc.querySelector(sel); }
  function visible(el) { return el && el.offsetParent !== null; }

  var frame = document.getElementById('pt');
  frame.addEventListener('load', function () {
    var doc;
    try { doc = frame.contentDocument; } catch (e) { doc = null; }
    if (!doc) { assert('iframe 可访问', 'blocked', 'same-origin', false); done(); return; }
    var win = frame.contentWindow;

    /* ── 1. 默认页签＝在线查询（业务模式置首） ── */
    var tabs = doc.querySelectorAll('#trial-tabs .trial-tab');
    assert('1 页签顺序首项＝在线查询', tabs[0] && tabs[0].textContent.trim(), '在线查询', tabs[0] && tabs[0].textContent.trim() === '在线查询');
    var webSel = q(doc, '#trial-tab-web');
    assert('2 默认选中页签＝web(在线查询)', webSel && webSel.getAttribute('aria-selected'), 'true', webSel && webSel.getAttribute('aria-selected') === 'true');

    /* ── 2. 无凭证引导条 ── */
    var banner = q(doc, '#trial-guide-banner');
    var link = q(doc, '#trial-guide-appkey-link');
    assert('3 无凭证引导条存在', !!banner, '存在', !!banner);
    assert('4 引导条文案含「沙箱凭证（7 天免费）」', banner && /沙箱凭证（7 天免费）/.test(banner.textContent), '含', banner && /沙箱凭证（7 天免费）/.test(banner.textContent));
    assert('5 引导条链接→admin/appkeys.html', link && link.getAttribute('href'), 'admin/appkeys.html', link && link.getAttribute('href') === 'admin/appkeys.html');

    /* ── 3. SM3 一句话人话化 ── */
    var hints = Array.prototype.map.call(doc.querySelectorAll('.field-hint'), function (n) { return n.textContent; });
    var okPhrase = '手机号在本页转换成查询代码，平台不存明文';
    assert('6 在线查询页签 SM3 人话文案', hints.some(function (t) { return t.indexOf(okPhrase) >= 0; }), '含', hints.some(function (t) { return t.indexOf(okPhrase) >= 0; }));

    /* ── 4. 开发者模式页签仍在 ── */
    assert('7 HTTP API 页签存在（开发者模式收进）', !!q(doc, '#trial-tab-http'), '存在', !!q(doc, '#trial-tab-http'));
    assert('8 Python SDK 页签存在（开发者模式收进）', !!q(doc, '#trial-tab-sdk'), '存在', !!q(doc, '#trial-tab-sdk'));

    /* ── 5. template 惰性区块：查构建产物源码（与 build_portal required_ids 同口径） ── */
    fetch('trial.html', { cache: 'no-store' }).then(function (r) { return r.text(); }).then(function (src) {
      assert('9 构建产物含错误动作条/演示号/历史/空态四区块', ['trial-http-error-action', 'trial-web-demo-phone', 'trial-history', 'trial-web-empty'].every(function (id) { return src.indexOf('id="' + id + '"') >= 0; }), '4/4', ['trial-http-error-action', 'trial-web-demo-phone', 'trial-history', 'trial-web-empty'].every(function (id) { return src.indexOf('id="' + id + '"') >= 0; }));
    });

    /* ── 6. 错误动作文案（iframe window 的 __TrialDebug 出口） ── */
    var act = win.__TrialDebug && win.__TrialDebug.httpActionHtml;
    if (act) {
      assert('10 401 动作文案（去领凭证）', /去 AppKey 发放页/.test(act(401, '')), '含', /去 AppKey 发放页/.test(act(401, '')));
      assert('11 404 动作文案（检查手机号）', /检查手机号/.test(act(404, '')), '含', /检查手机号/.test(act(404, '')));
      assert('12 5xx 动作文案（稍后再试＋traceId）', /稍后再试/.test(act(502, 'TRACE123')) && /TRACE123/.test(act(502, 'TRACE123')), '含', true);
    } else {
      assert('10 错误动作函数可用（__TrialDebug 出口）', 'missing', '可用', false);
      done(); return;
    }

    /* ── 7. 调试历史卡片静态存在 ── */
    assert('13 调试历史卡片存在', !!q(doc, '#trial-history'), '存在', !!q(doc, '#trial-history'));

    /* ── 8. 交互①：在线查询 未命中→空态引导→一键演示号→真实命中→历史 ── */
    var input = q(doc, '#trial-web-phone');
    var send = q(doc, '#trial-web-send');
    if (!input || !send) { assert('14 在线查询输入位可用', false, '可用', false); done(); return; }
    var setter = Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, 'value').set;
    setter.call(input, '18400000000');
    input.dispatchEvent(new win.Event('input', { bubbles: true }));
    setTimeout(function () {
      send.click();
      var tries = 0;
      (function pollEmpty() {
        var empty = q(doc, '#trial-web-empty');
        if (visible(empty) || tries > 40) {
          assert('14 未命中→空结果引导（含演示手机号 16193131979）', empty && /16193131979/.test(empty.textContent), '含', !!(empty && /16193131979/.test(empty.textContent)));
          var demoBtn = q(doc, '#trial-web-demo-phone');
          assert('15 空态引导内一键填入按钮出现', !!demoBtn, '存在', !!demoBtn);
          if (demoBtn) demoBtn.click();
          setTimeout(function () {
            var val = q(doc, '#trial-web-phone').value;
            assert('16 一键填入演示号', val, '16193131979', val === '16193131979');
            q(doc, '#trial-web-send').click();
            var t2 = 0;
            (function pollHit() {
              var rows = doc.querySelectorAll('#trial-web-result .tag-table tbody tr');
              if (rows.length > 0 || t2 > 40) {
                assert('17 演示号真实命中（标签表格 >0 行）', rows.length > 0, '>0 行', rows.length > 0);
                setTimeout(function () {
                  var items = doc.querySelectorAll('#trial-history .history-item');
                  assert('18 调试历史渲染（近 10 条环形，含命中/未命中两条）', items.length >= 2 && items.length <= 10, '2~10 条', items.length >= 2 && items.length <= 10);
                  http401Flow();
                }, 400);
              } else if (t2++ < 40) { setTimeout(pollHit, 250); } else { assert('17 演示号真实命中', 'timeout', '>0 行', false); http401Flow(); }
            })();
          }, 80);
        } else if (tries++ < 40) { setTimeout(pollEmpty, 250); } else { assert('14 未命中→空结果引导', 'timeout', '出现', false); http401Flow(); }
      })();
    }, 300);

    /* ── 9. 交互②：HTTP 页签 空凭证发送→网关真 401→错误动作条「去领凭证」 ── */
    function http401Flow() {
      /* 页签切换走 Alpine 数据（与 switchTab 同一反应式通道；截图模式下合成点击偶发不生效） */
      win.Alpine.$data(doc.getElementById('trial-panel')).tab = 'http';
      var hInput = q(doc, '#trial-http-phone');
      var hSend = q(doc, '#trial-http-send');
      var setter2 = Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, 'value').set;
      setter2.call(hInput, '13800138000');
      hInput.dispatchEvent(new win.Event('input', { bubbles: true }));
      setTimeout(function () {
        var expectHash = win.TrialSM3.phoneSm3('13800138000');
        var curlText = q(doc, '#trial-http-curl pre').textContent;
        assert('19 curl 示例带当前输入值（SM3 键随输入更新）', curlText.indexOf(expectHash) >= 0, '含', curlText.indexOf(expectHash) >= 0);
        hSend.click();
        var t3 = 0;
        (function poll401() {
          var action = q(doc, '#trial-http-error-action');
          var vis = !!(action && action.offsetParent !== null);
          var dPanel = win.Alpine.$data(doc.getElementById('trial-panel'));
          var settled = dPanel && dPanel.httpState && dPanel.httpState !== 'loading';
          if (vis || t3 > 100 || (settled && t3 > 12)) {   // settled 后再留 12 轮等 Alpine DOM 效果冲刷
            var panel = q(doc, '#trial-panel-http');
            var disp = panel ? panel.style.display : 'no-panel';
            var chain = [];
            var anc = panel;
            while (anc) { var cs = win.getComputedStyle(anc); if (cs.display === 'none' || cs.visibility === 'hidden') chain.push(anc.id || anc.tagName); anc = anc.parentElement; }
            var diag = 'vis=' + vis + ' state=' + (dPanel && dPanel.httpState) + ' status=' + (dPanel && dPanel.httpStatus) +
                       ' el=' + !!action + ' actLen=' + (action ? action.textContent.length : -1) +
                       ' panelVis=' + visible(panel) + ' display=' + disp + ' tab=' + (dPanel && dPanel.tab) +
                       ' hiddenAncestors=' + (chain.length ? chain.join('>') : 'none');
            assert('20 无凭证发送→网关 401→错误动作条（去领凭证）[' + diag + ']', vis && /去 AppKey 发放页/.test(action.textContent), '动作条可见含「去 AppKey 发放页」', vis && /去 AppKey 发放页/.test(action.textContent));
            var meta = q(doc, '#trial-http-result .result-meta');
            assert('21 结果区如实显示 HTTP 401', meta && /HTTP 401/.test(meta.textContent), '含 HTTP 401', !!(meta && /HTTP 401/.test(meta.textContent)));
            done();
          } else if (t3++ < 100) { setTimeout(poll401, 250); } else { assert('20 无凭证发送→401 动作条', 'timeout state=' + (dPanel && dPanel.httpState), '出现', false); done(); }
        })();
      }, 300);
    }

    function done() {
      window.__P03_DONE = { total: ROWS.length, failed: ROWS.filter(function (r) { return !r.ok; }).length };
      render();
    }
  });
})();
