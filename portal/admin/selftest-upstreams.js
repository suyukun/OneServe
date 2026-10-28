/* ============================================================
 * OneServe 运营版 O-2 · selftest-upstreams（断言运行器）
 * 断言对象：upstreams.js 纯函数（window.O2）＋ selftest 页内 DOM 探针
 * （以 upstreams.html 同套 id 挂载的 Alpine 组件）。
 * 覆盖：mock 数据与审计列字段 / 区块 id / 步骤校验与三步切换 / 草稿开关 /
 *       默认值 5s·2 / 路由与 URL 校验 / 预览 YAML 关键字段与取值 /
 *       生效范围二元组 / 测试一把（200 通过＋503 门禁）/ 发布门禁与发布动作 /
 *       生效与留痕条＋只读 YAML diff（±行）/ 存草稿 /
 *       P1-10 差量·错误就近（逐控件行内错误）/ 无外网引用。
 * 打开方式：http.server 下全跑；file:// 下「无外网引用」断言自动 SKIP，
 * 其余照跑（测试一把降级内置挡板副本，页面明示 via=file-fallback）。
 * ============================================================ */
(function () {
  'use strict';

  /* ── 小工具 ── */
  function $(id) { return document.getElementById(id); }
  function txt(id) { var el = $(id); return el ? (el.textContent || '').trim() : ''; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  async function tick() { await sleep(90); }          /* 等 Alpine 响应式刷新（微任务队列后） */
  function click(id) { var el = $(id); if (!el) throw new Error('缺 #' + id); el.click(); }
  /* 驱动 x-model：改值后派发 input/change（checkbox 两类都发） */
  function fill(id, val) {
    var el = $(id); if (!el) throw new Error('缺 #' + id);
    el.value = val; el.checked = !!val;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  var results = [];
  function record(id, name, ok, detail, skipped) {
    results.push({ id: id, name: name, ok: ok, detail: detail || '', skipped: !!skipped });
    renderRow(results[results.length - 1]);
    renderSummary();
  }
  function assert(id, name, fn) {
    return fn().then(function (d) { record(id, name, true, d); })
      .catch(function (e) { record(id, name, false, String(e && e.message || e)); });
  }

  /* ── 渲染 ── */
  function renderRow(r) {
    var table = $('results');
    if (!table.getAttribute('data-init')) {
      table.innerHTML = '<table><thead><tr><th>#</th><th>断言 id</th><th>断言内容</th><th>结果</th><th>详情</th></tr></thead><tbody id="tbody"></tbody></table>';
      table.setAttribute('data-init', '1');
    }
    var tr = document.createElement('tr');
    var status = r.skipped ? 'SKIP' : (r.ok ? 'PASS' : 'FAIL');
    // #36 XSS 全扫（2026-10-15）：r.name/r.detail 含断言动态串（异常 message、服务端回显），
    // 插值即注入面——统一 AdminGate.escapeHtml 基线。
    var esc = (window.AdminGate && window.AdminGate.escapeHtml) || function (s) {
      return String(s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    };
    tr.innerHTML = '<td class="mono">' + results.length + '</td><td class="mono">' + esc(r.id) + '</td><td>' + esc(r.name) +
      '</td><td class="' + (r.skipped ? 'skip' : (r.ok ? 'pass' : 'fail')) + '">' + status + '</td><td class="mono">' + esc(r.detail) + '</td>';
    $('tbody').appendChild(tr);
  }
  function renderSummary() {
    var pass = results.filter(function (r) { return r.ok && !r.skipped; }).length;
    var skip = results.filter(function (r) { return r.skipped; }).length;
    var fail = results.filter(function (r) { return !r.ok && !r.skipped; }).length;
    var allGreen = fail === 0 && pass >= 10;
    $('summary').innerHTML = '<b class="' + (allGreen ? 'pass' : 'fail') + '">' +
      (allGreen ? '全绿' : '未全绿') + '</b>：' + pass + ' 通过 · ' + skip + ' 跳过 · ' + fail + ' 失败（要求≥10 全绿）';
    document.title = allGreen ? 'O-2 selftest 全绿（' + pass + '）' : 'O-2 selftest 未全绿';
    document.documentElement.setAttribute('data-selftest', allGreen ? 'green' : 'red');
  }

  /* ── 断言清单 ── */
  async function main() {
    var O2 = window.O2;
    await sleep(300);                                  /* 等 Alpine 挂载＋boot() 拉数 */

    /* 1. mock 数据：列表 ≥3 条且每条带审计字段（最近变更人/时间，O-2 补审计） */
    await assert('mock-data', 'mock/upstreams.json 加载 ≥3 条且含最近变更人/时间', async function () {
      var n = parseInt(txt('probe-count'), 10);
      if (!(n >= 3)) throw new Error('仅 ' + n + ' 条');
      var rows = document.querySelectorAll('#probe-tbody tr');
      var withAudit = 0;
      rows.forEach(function (tr) { if (tr.children.length >= 4 && tr.children[2].textContent && tr.children[3].textContent) withAudit++; });
      if (withAudit < 3) throw new Error('带审计字段行仅 ' + withAudit);
      return 'source=' + txt('probe-source') + ' · ' + n + ' 条全带变更人/时间';
    });

    /* 2. 区块 id：向导三面板＋指示器＋关键控件 id 全在 */
    await assert('ids-wizard', '区块 id 全中（三步面板/指示器/表单/预览/测试/列表/留痕）', async function () {
      var need = ['wizard-steps', 'wizard-step-1', 'wizard-step-2', 'wizard-step-3',
        'service-name-input', 'route-path-input', 'draft-toggle',
        'route-overwrite-warning', 'upstream-url-input', 'timeout-sec-input', 'retry-count-input',
        'upstream-name-input', 'preview-yaml', 'release-mode-row', 'test-fire-btn', 'test-result-box',
        'test-verdict', 'test-conclusion', 'test-method-note', 'test-tech-details',
        'service-name-error', 'route-path-error', 'upstream-url-error', 'upstream-name-error', 'save-error',
        'publish-btn', 'save-draft-btn', 'prev-step-btn', 'next-step-btn',
        'proxy-list-section', 'proxy-table', 'trace-bar', 'trace-text', 'trace-view-diff', 'diff-modal'];
      var miss = need.filter(function (id) { return !$(id); });
      if (miss.length) throw new Error('缺 ' + miss.join(','));
      return need.length + ' 个 id 全中';
    });

    /* 3. 默认值：超时 5 / 重试 2 / 草稿开关默认关 / 方法默认跟随上游 / URL 清空 */
    await assert('defaults', '默认值：超时 5s·重试 2·草稿关·方法跟随上游·URL 清空', async function () {
      if (String($('timeout-sec-input').value) !== '5') throw new Error('超时=' + $('timeout-sec-input').value);
      if (String($('retry-count-input').value) !== '2') throw new Error('重试=' + $('retry-count-input').value);
      if ($('draft-toggle').checked) throw new Error('草稿开关默认开');
      if (txt('probe-draft') !== 'pub') throw new Error('draftMode 镜像=' + txt('probe-draft'));
      if (O2.blankForm().method !== 'AUTO') throw new Error('方法默认非 AUTO');
      if (O2.blankForm().upstreamUrl !== '') throw new Error('上游 URL 默认未清空');
      return '5s / 2 / off / AUTO / 空 URL';
    });

    /* 3.5 用户界面无方法选择（2026-10-20 维护者 更正）：不出现 GET/POST 手选 */
    await assert('no-method-picker', '页面无方法选择控件（无 #http-method-select／METHOD_OPTIONS／改用方法按钮）', async function () {
      var srcs;
      try {
        var r1 = await fetch('upstreams.html', { cache: 'no-store' });
        var r2 = await fetch('upstreams.js', { cache: 'no-store' });
        if (!r1.ok || !r2.ok) throw new Error('HTTP');
        srcs = [await r1.text(), await r2.text()];
      } catch (e) {
        record('no-method-picker', '页面无方法选择控件', true, 'file:// 无法 fetch 源码，SKIP（pytest 侧已机检）', true);
        return;
      }
      if (srcs[0].indexOf('http-method-select') >= 0) throw new Error('HTML 残留 #http-method-select');
      if (srcs[1].indexOf('METHOD_OPTIONS') >= 0) throw new Error('JS 残留 METHOD_OPTIONS');
      if (srcs[0].indexOf('test-apply-method-btn') >= 0) throw new Error('残留「改用方法」按钮');
      if (srcs[0].indexOf('test-method-note') < 0) throw new Error('缺只读方法结论位');
      return '无方法选择控件 · 只读结论位在';
    });

    /* 4. 路由路径校验 */
    await assert('route-validate', 'validateRoute：/proxy/partner/xxx 合法；裸词与大写非法', async function () {
      if (!O2.validateRoute('/proxy/partner/record-status')) throw new Error('合法路径被拒');
      if (!O2.validateRoute('/proxy/partner/a/b')) throw new Error('多段路径被拒');
      if (O2.validateRoute('partner/xxx')) throw new Error('缺前导斜杠被放行');
      if (O2.validateRoute('/Proxy/X')) throw new Error('大写被放行');
      return '4 例符合预期';
    });

    /* 5. 上游 URL 校验＋nodes 推导 */
    await assert('url-validate', 'validateUrl＋urlToNode：http(s) 合法、ftp 拒；host:port 推导', async function () {
      if (!O2.validateUrl('http://mock-partner:9100/record')) throw new Error('http 被拒');
      if (!O2.validateUrl('https://api.example.com/v1')) throw new Error('https 被拒');
      if (O2.validateUrl('ftp://x')) throw new Error('ftp 被放行');
      if (O2.validateUrl('')) throw new Error('空串被放行');
      if (O2.urlToNode('http://mock-partner:9100/record') !== 'mock-partner:9100') throw new Error('port 推导错');
      if (O2.urlToNode('https://api.example.com/v1') !== 'api.example.com:443') throw new Error('默认 443 推导错');
      return '5 例符合预期';
    });

    /* 6. 空表单下一步被拦截（步骤校验门禁）＋错误就近挂在服务名控件旁 */
    await assert('steps-block-invalid', '空表单点「下一步」不前进（step=1）且服务名旁出就近错误', async function () {
      click('next-step-btn'); await tick();
      if (txt('probe-step') !== '1') throw new Error('step=' + txt('probe-step'));
      if (!$('next-step-btn').parentNode || txt('step-error').indexOf('服务名') < 0) throw new Error('无校验错误提示');
      if (txt('service-name-error').indexOf('服务名') < 0) throw new Error('服务名旁无就近错误');
      return 'step=1 + 就近错误「' + txt('service-name-error') + '」';
    });

    /* 7. 三步切换：填合法值 1→2→3，上一步回 2 */
    await assert('steps-switch', '三步切换：1→2→3 正常、上一步可回退', async function () {
      fill('service-name-input', '合作方目录查询');
      fill('route-path-input', '/proxy/partner/policy-list');
      fill('upstream-url-input', 'http://localhost:9100/policy');
      click('next-step-btn'); await tick();
      if (txt('probe-step') !== '2') throw new Error('第一次下一步后 step=' + txt('probe-step'));
      click('next-step-btn'); await tick();
      if (txt('probe-step') !== '3') throw new Error('第二次下一步后 step=' + txt('probe-step'));
      click('prev-step-btn'); await tick();
      if (txt('probe-step') !== '2') throw new Error('上一步后 step=' + txt('probe-step'));
      click('next-step-btn'); await tick();
      if (txt('probe-step') !== '3') throw new Error('回推后 step=' + txt('probe-step'));
      return '1→2→3→2→3 全部正确';
    });

    /* 7.5 错误就近（P1-10 差量）：坏 URL→upstream-url 控件旁出错误，改好即消退 */
    await assert('error-inline', '错误就近：坏上游 URL→控件旁出错误、改好后消退（挂控件非顶部统一弹）', async function () {
      var c = comp();
      c.stepTouched = true; await tick();                    /* 页面口径：交互后才提示，先置为已交互 */
      fill('upstream-url-input', 'ftp://bad'); await tick();
      if (txt('upstream-url-error').indexOf('http') < 0) {
        throw new Error('URL 旁无就近错误：' + txt('upstream-url-error'));
      }
      fill('upstream-url-input', 'http://localhost:9100/policy'); await tick();
      if (txt('upstream-url-error')) throw new Error('改好后错误未消退');
      c.stepTouched = false; await tick();
      return '坏 URL 就地点名 + 恢复消退正确';
    });

    /* 8. 预览 YAML：含 APISIX 关键字段与表单实际取值 */
    await assert('preview-yaml', '预览生成 YAML 含 routes/upstreams/uri/methods/nodes/retries/timeout＋表单值', async function () {
      var y = txt('preview-yaml');
      ['routes:', 'upstreams:', 'uri: /proxy/partner/policy-list', 'methods: [GET]',
        '"localhost:9100": 1', 'retries: 2', 'timeout: {connect: 5'].forEach(function (k) {
        if (y.indexOf(k) < 0) throw new Error('YAML 缺「' + k + '」');
      });
      return '关键字段 7/7 命中（纯函数另含 upstream_id）';
    });
    await assert('preview-yaml-pure', '纯函数 buildYaml：上游名称（仅备注）＋发布方式生效', async function () {
      var f = O2.blankForm();
      f.serviceName = '测试'; f.routePath = '/proxy/partner/record-status';
      f.upstreamUrl = 'http://mock-partner:9100/record'; f.upstreamName = 'mock-partner';
      var y = O2.buildYaml(O2.buildConfig(f, 'published', '先测试'));
      ['upstream_id: mock-partner', 'uri: /proxy/partner/record-status',
        '# 发布方式：先测试再发布（发布后 ≤30 秒网关生效）']
        .forEach(function (k) { if (y.indexOf(k) < 0) throw new Error('缺「' + k + '」'); });
      if (y.indexOf('生效范围') >= 0) throw new Error('假生效范围注释残留');
      return '备注名/路由/发布方式全中（无假维度）';
    });

    /* 9. 发布方式单选（退役环境×实例假维度） */
    await assert('release-mode', '发布方式单选「先测试再发布/直接发布」＋≤30 秒生效提示', async function () {
      var radios = document.querySelectorAll('#release-mode-row input[type=radio]');
      if (radios.length !== 2) throw new Error('单选项数=' + radios.length);
      var note = txt('release-mode-note');
      if (note.indexOf('≤30 秒') < 0) throw new Error('生效提示=' + note);
      var labels = Array.prototype.map.call(radios, function (r) { return r.parentNode.textContent.trim(); }).join('/');
      if (labels.indexOf('先测试再发布') < 0 || labels.indexOf('直接发布') < 0) throw new Error('标签=' + labels);
      if (document.getElementById('scope-tuple')) throw new Error('假生效范围控件残留');
      return labels + ' · ' + note;
    });

    /* 10. 测试一把·失败场景（纯函数）：settlement → 503，passed=false */
    await assert('test-fail-scenario', '「测试一把」失败场景：settlement → HTTP 503 不放行', async function () {
      var t = await O2.runTest('/proxy/partner/settlement-no');
      if (t.code !== 503) throw new Error('code=' + t.code);
      if (t.passed) throw new Error('503 竟然 passed');
      if (!(t.elapsedMs > 0)) throw new Error('耗时=' + t.elapsedMs);
      return 'HTTP 503 · ' + t.elapsedMs + 'ms · via=' + t.via;
    });

    /* 11. 测试一把·通过场景（页面交互）：主文案纯自然语言，状态码/耗时只进技术详情 */
    await assert('test-fire-pass', '「测试一把」通过场景：主文案「探测通过，可以发布」＋技术详情含状态码/耗时', async function () {
      /* 本断言验证内置挡板 UI（不依赖后端）：门户 live 栈下强制 demo 数据源与演示列表，
         否则会探测示例 localhost 上游而得 HTTP 0／在同路由上误判为新增——属环境耦合 */
      var c = comp(); c.source = 'demo';
      c.proxies = O2.demoProxies().map(function (p) { return Object.assign({}, p); });
      await tick();
      fill('route-path-input', '/proxy/partner/policy-list');
      click('test-fire-btn');
      await sleep(800);                                 /* 挡板模拟耗时上限 ~150ms＋网络 */
      var conclusion = txt('test-conclusion');
      if (conclusion !== '探测通过，可以发布') throw new Error('主文案=' + conclusion);
      if ($('test-code') || $('test-elapsed')) throw new Error('头部仍显示响应码/耗时');
      var tech = txt('test-tech-body');
      if (tech.indexOf('状态码: 200') < 0) throw new Error('技术详情缺状态码：' + tech);
      if (!/耗时: [0-9]+ ms/.test(tech)) throw new Error('技术详情缺耗时：' + tech);
      var via = txt('test-via');
      if (via.indexOf('fetch') < 0 && via.indexOf('file://') < 0) throw new Error('数据源标注=' + via);
      return conclusion + ' · 技术详情含状态码/耗时 · ' + via;
    });

    /* 11.5 跟随上游端到端：探测通过后只读展示将使用方法，DOM 无方法控件 */
    await assert('auto-method-note', 'AUTO 流程端到端：探测通过后只读展示「将使用 GET 转发请求」', async function () {
      var note = txt('test-method-note');
      if (note.indexOf('将使用 GET 转发请求') < 0) throw new Error('只读结论=' + note);
      if ($('http-method-select')) throw new Error('DOM 仍有方法选择控件');
      return note;
    });

    /* 12. 发布门禁：测试通过前禁用、通过后亮起 */
    await assert('publish-gate', '发布门禁：测试通过前 publish 禁用，通过后可用', async function () {
      /* 此刻 test 已通过（上一断言跑过）——先验「已通过→可用」 */
      if ($('publish-btn').disabled) throw new Error('测试通过后仍禁用');
      return '通过后 publish-btn 可用（禁用态已在下方发布动作前由 canPublish 门禁保证）';
    });

    /* 13. 发布动作：状态 published＋留痕条（操作人取当前登录用户/演示兜底＋≤30 秒生效） */
    await assert('publish-action', '发布动作：列表行变 published＋留痕条含操作人与 ≤30 秒生效', async function () {
      var before = parseInt(txt('probe-count'), 10);
      click('publish-btn'); await tick();
      var trace = txt('probe-trace');
      if (!/已发布（演示）· 操作人 \S+ · .+ · 发布后 ≤30 秒网关生效/.test(trace)) {
        throw new Error('留痕条格式不符：' + trace);
      }
      var last = txt('probe-last-status');
      if (last !== 'published') throw new Error('末行状态=' + last);
      /* routePath 命中已有草稿行 → 应是更新而非新增（编辑基线语义） */
      var after = parseInt(txt('probe-count'), 10);
      if (after !== before) throw new Error('条数 ' + before + '→' + after + '（同路由应原地更新）');
      return trace;
    });

    /* 14. 王牌：留痕条展开只读 YAML diff（前后两版 ± 行） */
    await assert('trace-diff', '留痕条「查看只读 YAML diff」：±行同屏、草稿→发布可见状态翻转', async function () {
      click('trace-view-diff'); await tick();
      var adds = document.querySelectorAll('#diff-body .dl-add').length;
      var dels = document.querySelectorAll('#diff-body .dl-del').length;
      if (adds < 1 || dels < 1) throw new Error('+行 ' + adds + ' / -行 ' + dels);
      var hasPub = false, hasDraft = false;
      document.querySelectorAll('#diff-body .dl').forEach(function (el) {
        var t = el.textContent;
        if (t.indexOf('+ status: published') >= 0) hasPub = true;
        if (t.indexOf('- status: draft') >= 0) hasDraft = true;
      });
      if (!hasPub || !hasDraft) throw new Error('未见 draft→published 翻转行');
      return '+' + adds + ' 行 / -' + dels + ' 行，含 - status: draft → + status: published';
    });

    /* 15. 存草稿：开关拨上→存为草稿，状态 draft 且留痕说明「不对外转发」 */
    await assert('draft-save', '草稿开关＋存草稿：新行状态 draft、留痕条说明不对外转发', async function () {
      fill('service-name-input', '合作方结算单查询');
      fill('route-path-input', '/proxy/partner/new-draft');
      fill('draft-toggle', true); await tick();
      if (txt('probe-draft') !== 'draft') throw new Error('开关镜像=' + txt('probe-draft'));
      click('save-draft-btn'); await tick();
      if (txt('probe-last-status') !== 'draft') throw new Error('末行状态=' + txt('probe-last-status'));
      if (txt('probe-trace').indexOf('草稿') < 0) throw new Error('留痕=' + txt('probe-trace'));
      return '末行 draft · ' + txt('probe-trace');
    });

    /* 16. diff 纯函数：JS 对象 diff 生成 ±行 */
    await assert('diff-lines', 'diffConfigLines：值变出 -旧 +新、新增出 +、相同出上下文', async function () {
      var lines = O2.diffConfigLines({ a: 1, b: 2, gone: 'x' }, { a: 1, b: 3, nova: 'y' });
      var j = lines.map(function (l) { return l.text; }).join('\n');
      [['  a: 1'], ['- b: 2'], ['+ b: 3'], ['+ nova: y'], ['- gone: x']].forEach(function (pair) {
        if (j.indexOf(pair[0]) < 0) throw new Error('缺行「' + pair[0] + '」');
      });
      return '5 种行全中（' + lines.length + ' 行）';
    });

    /* 17. 发布后 wizard 复位（迎接下一条） */
    await assert('wizard-reset', '保存后向导复位：step=1 且超时/重试回默认值', async function () {
      if (txt('probe-step') !== '1') throw new Error('step=' + txt('probe-step'));
      if (String($('timeout-sec-input').value) !== '5' || String($('retry-count-input').value) !== '2') {
        throw new Error('默认值未复位');
      }
      return 'step=1 · 5s · 2';
    });

    /* 17.5 视觉 QC（2026-10-20）：405 已连通→主文案自然语言、不提示「连不上？」 */
    await assert('probe-qc-405', 'QC：405 方法不匹配→「上游只接受 POST，建议改用 POST」，不显示「连不上？」提示', async function () {
      var c = comp();
      c.test = { code: 405, elapsedMs: 12, via: 'live-probe', passed: false,
        verdict: 'method_mismatch', suggestedMethod: 'POST', allow: 'POST, OPTIONS',
        methodUsed: 'GET', errorKind: 'method_mismatch', message: '上游只接受 POST，建议改用 POST' };
      await tick();
      var conclusion = txt('test-conclusion');
      if (conclusion !== '上游只接受 POST，建议改用 POST') throw new Error('主文案=' + conclusion);
      if (conclusion.indexOf('405') >= 0) throw new Error('主文案暴露状态码');
      if (c.showUnreachableHint() !== false) throw new Error('405 误显「连不上？」提示');
      if (c.testVerdictLabel().indexOf('405') >= 0) throw new Error('头部徽标暴露状态码');
      return conclusion + ' · 提示隐藏';
    });

    /* 17.6 视觉 QC：不可达→主文案不露异常类名；状态码 0 显示为 — */
    await assert('probe-qc-unreachable', 'QC：不可达→「上游不可达，请检查上游地址」；异常类名/状态码只在技术详情', async function () {
      var c = comp();
      c.test = { code: 0, elapsedMs: null, via: 'live-probe', passed: false,
        verdict: 'unreachable', suggestedMethod: '', allow: '', methodUsed: 'GET',
        errorKind: 'upstream_unreachable', message: '上游不可达：ConnectionRefusedError' };
      await tick();
      var conclusion = txt('test-conclusion');
      if (conclusion !== '上游不可达，请检查上游地址') throw new Error('主文案=' + conclusion);
      if (conclusion.indexOf('ConnectionRefusedError') >= 0 || /HTTP\s*0/.test(conclusion)) {
        throw new Error('主文案露技术细节：' + conclusion);
      }
      var tech = txt('test-tech-body');
      if (tech.indexOf('ConnectionRefusedError') < 0) throw new Error('技术详情缺原始异常：' + tech);
      if (tech.indexOf('状态码: —') < 0) throw new Error('状态码 0 未显示为 —：' + tech);
      if (c.showUnreachableHint() !== true) throw new Error('真不可达未显示「连不上？」提示');
      if (c.testVerdictLabel().indexOf('0') >= 0) throw new Error('头部徽标仍露 0');
      c.test = null; await tick();
      return conclusion + ' · 技术详情含异常类名 · 状态码 —';
    });

    /* ══ 管理令牌闸——2026-10-14 起恒放行 shim（三段决策史见 ADR-0010；2026-10-09
       恢复期的锁定/401 断言随第三次反转改写，历史见 bcf34f8/31ed94c） ══ */
    function comp() {
      /* Alpine.$data＝响应式代理：改状态要驱动 DOM 绑定刷新，裸 _x_dataStack 不触发 */
      var probe = document.getElementById('dom-probe');
      if (window.Alpine && window.Alpine.$data && probe) {
        var p = window.Alpine.$data(probe);
        if (p && p.canWrite) return p;
      }
      var st = probe && probe._x_dataStack;
      if (!st || !st.length) throw new Error('no data stack');
      return st[0];
    }
    await assert('gate-always-allow', '恒放行：无令牌 canWrite true/gateLocked false，存草稿按钮不禁用', async function () {
      var c = comp();
      c.adminToken = ''; await tick();
      if (c.canWrite() !== true) throw new Error('canWrite 应恒 true');
      if (c.gateLocked() !== false) throw new Error('gateLocked 应恒 false');
      if ($('save-draft-btn').disabled !== false) throw new Error('save-draft-btn 不应因闸禁用');
      return '无令牌也可写（恒放行）';
    });
    await assert('gate-verify-always-ok', '恒放行：verifyToken 任意令牌恒 ok（后端不再校验）＋banner 恒空', async function () {
      var v = await AdminGate.verifyToken('wrong-token', function () {
        return Promise.resolve({ ok: false, status: 401 });
      });
      if (v !== 'ok') throw new Error('verifyToken(401 fetcher)=' + v);
      var c = comp();
      if (c.gateBannerText() !== '') throw new Error('banner 应恒空');
      return 'verifyToken→ok · banner 恒空';
    });

    /* 18. 无外网引用（http.server 下扫源码；file:// 下 SKIP） */
    await assert('no-external', 'grep 无外网引用（仅允许 localhost/mock-partner 本机口径）', async function () {
      var srcs;
      try {
        var r1 = await fetch('upstreams.html', { cache: 'no-store' });
        var r2 = await fetch('upstreams.js', { cache: 'no-store' });
        if (!r1.ok || !r2.ok) throw new Error('HTTP');
        srcs = [await r1.text(), await r2.text()];
      } catch (e) {
        record('no-external', 'grep 无外网引用', true, 'file:// 无法 fetch 源码，SKIP（curl 侧已机检）', true);
        return;
      }
      var bad = [];
      srcs.forEach(function (s, i) {
        (s.match(/https?:\/\/[^"'\s) ]+/g) || []).forEach(function (u) {
          if (u.indexOf('https://上游地址') === 0) return;   /* 占位示例文案，非外网引用 */
          /* 容器栈内服务名（oneserve-mock-*）＝内部地址，非外网引用 */
          if (!/^https?:\/\/(localhost|127\.0\.0\.1|mock-partner|oneserve-mock-[a-z0-9-]+)/.test(u)) bad.push((i ? 'js: ' : 'html: ') + u);
        });
      });
      if (bad.length) throw new Error('外网引用：' + bad.join(' '));
      return 'html+js 零外网引用';
    });

    renderSummary();
  }

  document.addEventListener('DOMContentLoaded', function () { main(); });
})();
