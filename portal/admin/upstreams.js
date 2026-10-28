/* ============================================================
 * OneServe 运营版 O-2 · 上游/挡板代理配置逻辑（upstreams.js，Alpine 组件）
 * 依据：docs/plans/运营版设计稿-三界面_v0.md（O-2 节＋文末 v0.1 增补：
 *   通用「生效与留痕条」＋ O-2 补审计：列表行加最近变更人/时间——
 *   代理配置＝流量劫持最高危面）。
 *
 * 三步向导（2026-10-20 可用性改版）：①基本信息（服务名/路由路径/「先存草稿」开关；
 *   方法不设手选，固定跟随上游，探测后只读展示判定结论）
 *   →②上游服务（上游 URL/超时 1~30s/重试/上游名称仅登记备注）
 *   →③预览确认（本次片段 YAML＋发布方式＋「测试一把」：三态结论，2xx 才放行）。
 *
 * 【测试一把的实现口径（任务书注明项）】
 *   http.server 下：对 mock/upstream-target.json 发真实 HTTP fetch（挡板数据文件），
 *     耗时含真实网络往返；file:// 下 fetch 不可用 → 降级用内置同构副本
 *     EMBEDDED_TARGET（数据源标注 file-fallback，页面上明示）。
 *   跨源真实上游（如容器 mock-partner:9100）会被浏览器 CORS 拦截，M1 不趟，
 *     演示语义＝「请求真的发出去了＋响应码/耗时是真的」。
 *
 * 【P1-10/11 写侧接真】列表读配置文件：GET /v1/admin/upstreams 读 upstreams.yaml
 *   （mock/upstreams.json 仅为后端不可达时的演示降级）；「存草稿/发布/下线」
 *   POST /v1/admin/upstreams[/offline|/delete] 原子写 upstreams.yaml
 *   （校验＋tmp+rename）。演示模式（后端不可达）行为与本卡之前完全一致。
 *
 * 【可测性】纯函数挂在 window.O2（供 selftest-upstreams.html 直测）；
 *   Alpine 组件 upstreamAdmin() 只做编排。
 *
 * 【P0-6 最小鉴权 gate】无令牌＝只读模式（发布/存草稿/下线按钮禁用＋顶部
 *   banner「输入管理令牌解锁操作」）；令牌栏/会话存储/后端校验口径三页共用
 *   admin-gate.js（AdminGate.gateMixin——D1=b 凭证兼身份，不自建登录系统）。
 *   写操作接真后端归 P1-10/11（D2=a：P0 只读接真），闸与令牌头先就位。
 * ============================================================ */
(function (root) {
  'use strict';

  var DEFAULT_TIMEOUT = 5;    /* 超时秒·设计稿默认 */
  var DEFAULT_RETRY = 2;      /* 重试次数·设计稿默认 */
  var TIMEOUT_MIN = 1, TIMEOUT_MAX = 30;   /* 与后端 probe 上限对齐（统一 1~30） */
  var METHOD_AUTO = 'AUTO';   /* 跟随上游：探测时自动判方法 */
  var OPERATOR = 'admin@oneserve';   /* 仅无登录态时的兜底展示；写操作取当前登录用户 */
  var RELEASE_MODES = [
    { value: '先测试', label: '先测试再发布' },
    { value: '直接上生产', label: '直接发布' }
  ];
  var METHODS = ['GET', 'POST'];   /* 登记口径（AUTO 探测后落成具体方法） */

  /* ── 后端端点（P1-10/11：门户 nginx /v1/admin/ 反代到服务层；写端点带管理令牌） ── */
  var API = {
    view: '/v1/admin/upstreams',
    save: '/v1/admin/upstreams',
    probe: '/v1/admin/upstreams/probe',
    offline: function (id) { return '/v1/admin/upstreams/' + encodeURIComponent(id) + '/offline'; },
    remove: function (id) { return '/v1/admin/upstreams/' + encodeURIComponent(id) + '/delete'; }
  };

  /* ── 校验器 ── */
  /* 路由路径：必须 /proxy/ 前缀＋小写 slug 段（O-2「接上游=填个地址」的路径约定） */
  function validateRoute(p) {
    return /^\/proxy\/[a-z0-9][a-z0-9-]*(\/[a-z0-9][a-z0-9-]*)*$/.test((p || '').trim());
  }
  /* 上游 URL：http(s) 且可解析 */
  function validateUrl(u) {
    try {
      var x = new URL((u || '').trim());
      return x.protocol === 'http:' || x.protocol === 'https:';
    } catch (e) { return false; }
  }

  /* ── URL → APISIX nodes 条目（host:port，缺省端口按协议补） ── */
  function urlToNode(u) {
    try {
      var x = new URL((u || '').trim());
      var port = x.port || (x.protocol === 'https:' ? '443' : '80');
      return x.hostname + ':' + port;
    } catch (e) { return ''; }   /* 空/坏 URL：预览占位为空，不抛错（步骤②校验会拦） */
  }
  /* 路由路径 → 配置 slug（/proxy/partner/record-status → proxy-partner-record-status） */
  function slugFromRoute(p) {
    return (p || '').trim().replace(/^\//, '').replace(/\//g, '-');
  }

  /* ── 表单默认值 ── */
  function blankForm() {
    return {
      serviceName: '', routePath: '', method: METHOD_AUTO, draftMode: false,
      upstreamUrl: '',          /* 默认值清空：避免把示例 localhost 当成真上游 */
      timeoutSec: DEFAULT_TIMEOUT, retryCount: DEFAULT_RETRY,
      upstreamName: ''          /* 仅登记备注，不再创建/共享 Upstream 对象 */
    };
  }

  /* 跟随上游（AUTO）未探测时按 GET 登记；探测到方法后前端回填具体方法 */
  function concreteMethod(m) { return (m && m !== METHOD_AUTO) ? m : 'GET'; }
  /* 探测方法序列（后端 methodUsed，如 "GET→POST"）→ 末段转发方法；单步原样取 */
  function lastForwardMethod(seq) {
    var parts = String(seq || '').split('→');
    var last = parts[parts.length - 1];
    return (last === 'GET' || last === 'POST') ? last : '';
  }

  /* ── 表单 → 配置对象（预览 YAML 与列表条目共用口径） ── */
  function buildConfig(f, status, releaseMode) {
    var slug = slugFromRoute(f.routePath);
    var name = (f.upstreamName || '').trim();
    return {
      slug: slug,
      serviceName: (f.serviceName || '').trim(),
      routePath: (f.routePath || '').trim(),
      method: concreteMethod(f.method),
      upstreamUrl: (f.upstreamUrl || '').trim(),
      node: urlToNode(f.upstreamUrl),
      upstreamId: name || (slug + '-up'),
      timeoutSec: f.timeoutSec, retryCount: f.retryCount,
      status: status, releaseMode: releaseMode || RELEASE_MODES[0].value
    };
  }

  /* ── 配置对象 → 本次片段 YAML（仅含本条，与线上完整配置文件不同） ── */
  function buildYaml(cfg) {
    if (!cfg) return '';
    var lines = [
      '# ============================================================',
      '# OneServe 代理配置片段（proxy-x.yaml）',
      '# 仅含本条配置，与线上完整配置文件（service/upstreams.yaml）不同。',
      '# ============================================================',
      'version: 1',
      'routes:',
      '  - id: ' + cfg.slug,
      '    uri: ' + cfg.routePath,
      '    methods: [' + cfg.method + ']',
      '    upstream_id: ' + cfg.upstreamId,
      '    status: ' + cfg.status + '   # draft=草稿 published=已发布 offline=已下线',
      'upstreams:',
      '  - id: ' + cfg.upstreamId,
      '    type: roundrobin',
      '    nodes:',
      '      ' + JSON.stringify(cfg.node) + ': 1',
      '    timeout: {connect: ' + cfg.timeoutSec + ', send: ' + cfg.timeoutSec + ', read: ' + cfg.timeoutSec + '}',
      '    retries: ' + cfg.retryCount,
      '# 发布方式：' + (cfg.releaseMode === '先测试' ? '先测试再发布' : '直接发布') + '（发布后 ≤30 秒网关生效）'
    ];
    return lines.join('\n') + '\n';
  }

  /* ── 列表条目 → 扁平配置对象（diff 与 YAML 导出用） ── */
  function normalizeEntry(e) {
    if (!e) return {};
    return {
      service_name: e.serviceName,
      route: e.routePath,
      method: e.method,
      upstream_url: e.upstreamUrl,
      upstream_id: e.upstreamName || (slugFromRoute(e.routePath) + '-up'),
      timeout_sec: e.timeoutSec,
      retries: e.retryCount,
      status: e.status,
      env: e.env || '本机M0',
      instance: e.instance || '网关实例A',
      last_modified_by: e.lastModifiedBy || '',
      last_modified_at: (e.lastModifiedAt || '').slice(0, 19).replace('T', ' ')
    };
  }

  /* ── JS 对象 diff → ±行（留痕条王牌：只读 YAML diff 前后两版对比） ──
   * 嵌套对象拍平成 path→scalar；值相同出上下文行，值变出 -旧行 +新行，
   * 单侧存在出删除/新增行。行序：after 键序优先，删除项殿后（阅读友好）。 */
  function flatten(obj, prefix, out) {
    Object.keys(obj || {}).forEach(function (k) {
      var v = obj[k], key = prefix ? prefix + '.' + k : k;
      if (v && typeof v === 'object' && !Array.isArray(v)) flatten(v, key, out);
      else out[key] = Array.isArray(v) ? '[' + v.join(', ') + ']' : String(v);
    });
  }
  function diffConfigLines(before, after) {
    var b = {}, a = {};
    flatten(before, '', b); flatten(after, '', a);
    var lines = [];
    Object.keys(a).forEach(function (k) {
      if (!(k in b)) lines.push({ op: '+', text: '+ ' + k + ': ' + a[k] });
      else if (b[k] === a[k]) lines.push({ op: ' ', text: '  ' + k + ': ' + a[k] });
      else { lines.push({ op: '-', text: '- ' + k + ': ' + b[k] }); lines.push({ op: '+', text: '+ ' + k + ': ' + a[k] }); }
    });
    Object.keys(b).forEach(function (k) {
      if (!(k in a)) lines.push({ op: '-', text: '- ' + k + ': ' + b[k] });
    });
    return lines;
  }

  /* ── 假合作方挡板（内置副本；与 admin/mock/upstream-target.json 同构，file:// 降级用） ── */
  var EMBEDDED_TARGET = {
    stub: {
      name: 'mock-合作方数据中台',
      scenarios: [
        { name: '信息查询', match: 'record', status: 200, delayMs: [40, 120],
          response: { code: '0', msg: 'ok', source: 'mock-partner',
            data: { agency: 'mock-合作方数据中台',
              record: { id: 'PT-DEMO-0001', name: '张*三', status: '正常', balance_yuan: 1234.56 } } } },
        { name: '政策目录查询', match: 'policy', status: 200, delayMs: [60, 150],
          response: { code: '0', msg: 'ok', source: 'mock-partner',
            data: { agency: 'mock-合作方数据中台', items: [{ no: 'PT2026-0001', type: '标准类型A' }] } } },
        { name: '结算单查询（挡板检修中·故意 503）', match: 'settlement', status: 503, delayMs: [200, 400],
          response: { code: '503', msg: 'mock-合作方结算接口检修中（门禁：测试不过→不让发布）', source: 'mock-partner', data: null } }
      ],
      fallback: { name: '通用兜底', match: '*', status: 200, delayMs: [30, 90],
        response: { code: '0', msg: 'ok', source: 'mock-partner',
          data: { agency: 'mock-合作方数据中台', echo: '任意路径兜底响应' } } }
    }
  };

  /* 按路由末段挑挡板场景：命中 scenarios[].match，否则 fallback */
  function pickScenario(target, routePath) {
    var stub = target && target.stub ? target.stub : EMBEDDED_TARGET.stub;
    var tail = (routePath || '').trim().replace(/\/+$/, '').split('/').pop() || '';
    var list = stub.scenarios || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].match && tail.indexOf(list[i].match) !== -1) return list[i];
    }
    return stub.fallback || list[0];
  }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function nowMs() { return (root.performance && root.performance.now) ? root.performance.now() : Date.now(); }

  /* ──「测试一把」：向 mock 挡板发一条请求，返回响应码/耗时/数据源标注 ──
   * http.server：fetch mock/upstream-target.json＝真实 HTTP 请求（耗时含网络往返）；
   * file://：fetch 抛错 → 内置副本降级（via=file-fallback，页面明示）。 */
  async function runTest(routePath, fetcher) {
    var t0 = nowMs();
    var data = null, via = 'fetch-mock-json';
    try {
      var r = await (fetcher || root.fetch)('mock/upstream-target.json', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      data = await r.json();
    } catch (e) { data = EMBEDDED_TARGET; via = 'file-fallback'; }
    var sc = pickScenario(data, routePath);
    var lo = (sc.delayMs && sc.delayMs[0]) || 30, hi = (sc.delayMs && sc.delayMs[1]) || 90;
    await sleep(lo + Math.floor(Math.random() * Math.max(1, hi - lo)));   /* 模拟上游网络耗时 */
    var elapsed = Math.round(nowMs() - t0);
    var code = sc.status || 200;
    return {
      code: code, elapsedMs: elapsed, via: via,
      scenario: sc.name || sc.match || 'fallback',
      passed: code >= 200 && code < 300,
      body: sc.response
    };
  }

  /* ── 演示短 hash（非密码学语义，仅作 git 短 commit 占位；同 O-3 口径） ── */
  function shortHash(seed) {
    var h = 0;
    for (var i = 0; i < String(seed).length; i++) h = ((h << 5) - h + String(seed).charCodeAt(i)) | 0;
    return ('0000000' + (h >>> 0).toString(16)).slice(-7);
  }
  function nowHM() {
    var d = new Date();
    function pad(x) { return ('0' + x).slice(-2); }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  /* 当前登录用户（列表「最近变更人」不再写死常量）：OIDC JWT 的 name/username/email；
     无登录态（本机 off/demo）回落 admin@oneserve——不代表真实身份，仅演示兜底。 */
  function currentOperator() {
    try {
      var tok = (root.AdminGate && root.AdminGate.readOidcToken)
        ? root.AdminGate.readOidcToken() : '';
      if (!tok) return OPERATOR;
      var claims = (root.SiteGate && root.SiteGate.jwtClaims)
        ? root.SiteGate.jwtClaims(tok) : {};
      if (!claims || !Object.keys(claims).length) {
        var part = String(tok).split('.')[1] || '';
        var b64 = part.replace(/-/g, '+').replace(/_/g, '/');
        claims = JSON.parse(decodeURIComponent(escape(root.atob(b64)))) || {};
      }
      return claims.name || claims.preferred_username || claims.email || OPERATOR;
    } catch (e) { return OPERATOR; }
  }
  /* 探测三态徽标文案/样式（live probe；demo 挡板同样按 passed 归类） */
  function verdictKey(t) {
    if (!t) return '';
    if (t.verdict) return t.verdict;
    return t.passed ? 'ok_2xx' : 'unreachable';
  }
  function verdictLabel(t) {
    var k = verdictKey(t);
    if (k === 'ok_2xx') return '通过';
    if (k === 'ok_post_confirmed') return '通过（POST 已确认）';
    if (k === 'method_mismatch') return '方法不匹配';
    if (t && t.code > 0) return '响应异常';   /* 状态码只进技术详情，头部行不露数字 */
    return '不可达';
  }
  /* 主文案（纯自然语言）：状态码/耗时/异常类名一律只进「技术详情」。
     2026-10-20 视觉 QC：主文案不出现 HTTP 状态码数字；不可达不暴露异常类名；
     405 属「已连通但方法不对」，不得写成「连不上」。 */
  function verdictText(t) {
    if (!t) return '';
    if (t.verdict === 'ok_2xx') return '探测通过，可以发布';
    if (t.verdict === 'ok_post_confirmed') return '上游在线，POST 方法已确认，可以发布';
    if (t.verdict === 'method_mismatch') {
      if (t.suggestedMethod) {
        return '上游只接受 ' + t.suggestedMethod + '，建议改用 ' + t.suggestedMethod;
      }
      return '上游不接受当前请求方法，请确认上游可用方法';
    }
    if (t.code > 0) return '上游可达但探测未通过，请检查上游返回内容';
    if (t.errorKind === 'upstream_timeout') return '上游响应超时，请检查上游状态或调大超时时间';
    return '上游不可达，请检查上游地址';
  }
  function verdictClass(t) {
    var k = verdictKey(t);
    if (k === 'ok_2xx' || k === 'ok_post_confirmed') return 'badge-active';
    if (k === 'method_mismatch') return 'badge-disabled';
    return 'badge-danger';
  }
  /* 列表「最近变更时间」直显 MM-DD HH:mm（审计列看绝对时间，不玩相对时间） */
  function fmtDateTime(iso) {
    if (!iso) return '—';
    var t = Date.parse(iso);
    if (isNaN(t)) return '—';
    var d = new Date(t);
    function pad(x) { return ('0' + x).slice(-2); }
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  /* ── file:// 降级列表（与 mock/upstreams.json 同构的精简副本） ── */
  function demoProxies() {
    return [
      { id: 'proxy-partner-record-status', serviceName: '合作方信息查询', routePath: '/proxy/partner/record-status',
        method: 'GET', upstreamUrl: 'http://mock-partner:9100/record', timeoutSec: 5, retryCount: 2,
        reuseUpstream: true, upstreamName: 'mock-partner', status: 'published', env: '本机M0', instance: '网关实例A',
        lastModifiedBy: 'admin@oneserve', lastModifiedAt: '2026-09-21T21:18:00+08:00',
        publishedAt: '2026-09-21T21:18:00+08:00', gitShortHash: 'e3f9a2c', callCount7d: 128 },
      { id: 'proxy-partner-policy-list', serviceName: '合作方目录查询', routePath: '/proxy/partner/policy-list',
        method: 'GET', upstreamUrl: 'http://mock-partner:9100/policy', timeoutSec: 5, retryCount: 2,
        reuseUpstream: true, upstreamName: 'mock-partner', status: 'draft', env: '本机M0', instance: '网关实例A',
        lastModifiedBy: 'demo-user-1@oneserve', lastModifiedAt: '2026-09-22T08:41:00+08:00',
        publishedAt: null, gitShortHash: null, callCount7d: 0 },
      { id: 'proxy-partner-settlement-no', serviceName: '合作方结算单查询', routePath: '/proxy/partner/settlement-no',
        method: 'POST', upstreamUrl: 'http://mock-partner:9100/settlement', timeoutSec: 8, retryCount: 3,
        reuseUpstream: false, upstreamName: 'proxy-partner-settlement-no-up', status: 'offline', env: '测试T1', instance: '网关实例B',
        lastModifiedBy: 'admin@oneserve', lastModifiedAt: '2026-09-20T18:05:00+08:00',
        publishedAt: '2026-09-18T10:00:00+08:00', gitShortHash: '9c77e01', callCount7d: 12 },
      { id: 'proxy-partner-remote-filing', serviceName: '合作方备案查询', routePath: '/proxy/partner/remote-filing',
        method: 'GET', upstreamUrl: 'http://mock-partner:9100/filing', timeoutSec: 5, retryCount: 2,
        reuseUpstream: true, upstreamName: 'mock-partner', status: 'published', env: '本机M0', instance: '网关实例A',
        lastModifiedBy: 'demo-user-2@oneserve', lastModifiedAt: '2026-09-22T08:12:00+08:00',
        publishedAt: '2026-09-22T08:12:00+08:00', gitShortHash: '41ab7de', callCount7d: 57 }
    ];
  }

  /* ══ Alpine 组件：三步向导＋列表＋发布留痕编排 ══ */
  root.upstreamAdmin = function () {
    /* 2026-09-23：管理令牌机制已彻底拆除，仅保留 admin-gate.js 的无闸 shim */
    return Object.assign(AdminGate.gateMixin(), {
      /* 状态 */
      step: 1,
      stepTouched: false,          /* 步骤校验只在意图交互后提示（点「下一步」），空态不先亮红字 */
      form: blankForm(),
      proxies: [], source: 'demo',
      releaseMode: RELEASE_MODES[0].value,   /* 先测试再发布（默认）/ 直接发布 */
      test: null, testing: false, saveError: '',
      lastTrace: '', traceDiff: null,
      diffModal: { open: false, title: '', meta: '', lines: [], added: 0, removed: 0 },
      offlineModal: { open: false, stage: 1, index: -1, armed: false },
      draftDeleteModal: { open: false, stage: 1, index: -1 },   /* 删除草稿：两段确认 */
      followup: { open: false, serviceId: '', serviceName: '' },
      note: '',
      METHODS: METHODS, RELEASE_MODES: RELEASE_MODES,
      OPERATOR: OPERATOR,

      async boot() {
        var self = this;
        /* 2026-09-23：管理令牌机制已彻底拆除，不再恢复/校验令牌 */
        var live = await this.loadLive();            /* P1-10：在线优先（upstreams.yaml 配置文件） */
        if (live) return;
        try {                                        /* 后端不可达：mock 文件降级（演示口径不变） */
          var r = await root.fetch('mock/upstreams.json', { cache: 'no-store' });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          var j = await r.json();
          self.proxies = (j.proxies || []).map(function (p) { return Object.assign({}, p); });
          self.source = 'demo';
        } catch (e) { self.proxies = demoProxies(); self.source = 'demo'; }
      },

      /* ── live 取数（P1-10：upstreams.yaml 配置文件；401＝令牌问题，其余按不可达降级） ── */
      loadLive() {
        var self = this;
        return this.apiFetch(API.view)
          .then(function (j) {
            self.proxies = (j.proxies || []).map(function (p) { return Object.assign({}, p); });
            self.source = 'live';
            return true;
          })
          .catch(function (err) {
            return false;
          });
      },

      /* ── 后端调用（统一带管理令牌；错误规整为 {status, message}——同 appkeys.js） ── */
      apiFetch(url, opts) {
        var headers = Object.assign({}, (root.AdminGate && root.AdminGate.adminHeaders()) || {});
        if (opts && opts.body) headers['Content-Type'] = 'application/json';
        return root.fetch(url, Object.assign({}, opts || {}, { headers: headers, cache: 'no-store' }))
          .then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (j) {
              if (!r.ok) {
                var e = new Error(j.message || ('HTTP ' + r.status));
                e.status = r.status; e.code = j.error;
                throw e;
              }
              return j;
            });
          });
      },

      /* ── 步骤校验与切换 ── */
      stepOk(n) {
        var f = this.form;
        if (n === 1) return !!(f.serviceName.trim() && validateRoute(f.routePath) && f.method);
        if (n === 2) return !!(validateUrl(f.upstreamUrl) && f.timeoutSec >= TIMEOUT_MIN &&
          f.timeoutSec <= TIMEOUT_MAX && f.retryCount >= 0);
        return true;
      },
      stepErr(n) {
        if (n === 1) return this.serviceNameError() || this.routeError();
        if (n === 2) return this.upstreamUrlError() || this.timeoutError() || this.retryError();
        return '';
      },
      /* ── 错误就近：逐控件错误（页面把提示挂在各自输入框旁） ── */
      serviceNameError() { return this.form.serviceName.trim() ? '' : '请填写服务名'; },
      routeError() {
        return validateRoute(this.form.routePath) ? ''
          : '路由路径须形如 /proxy/partner/xxx（小写字母数字与连字符）';
      },
      upstreamUrlError() {
        return validateUrl(this.form.upstreamUrl) ? '' : '上游 URL 须为合法 http(s) 地址';
      },
      timeoutError() {
        var v = Number(this.form.timeoutSec);
        return (v >= TIMEOUT_MIN && v <= TIMEOUT_MAX) ? ''
          : '超时秒数须在 ' + TIMEOUT_MIN + '~' + TIMEOUT_MAX + ' 之间（等待上游响应的最大秒数）';
      },
      retryError() { return this.form.retryCount >= 0 ? '' : '重试次数不能为负'; },
      upstreamNameError() { return ''; },   /* 仅登记备注，不做 slug 强校验 */
      /* 同一路由已存在条目 → 保存会覆盖；用于就近警告（不拦保存，后端同 route 更新） */
      routeOverwriteEntry() {
        var route = (this.form.routePath || '').trim();
        if (!route) return null;
        for (var i = 0; i < this.proxies.length; i++) {
          if ((this.proxies[i].routePath || '').trim() === route) return this.proxies[i];
        }
        return null;
      },
      releaseModeLabel() {
        var self = this;
        var hit = RELEASE_MODES.filter(function (m) { return m.value === self.releaseMode; })[0];
        return hit ? hit.label : RELEASE_MODES[0].label;
      },
      nextStep() {
        this.stepTouched = true;
        if (this.step < 3 && this.stepOk(this.step)) { this.step += 1; this.stepTouched = false; this.note = ''; }
      },
      prevStep() { if (this.step > 1) this.step -= 1; },
      goStep(n) { if (n < this.step) this.step = n; },   /* 指示器只允许回看已过的步 */

      /* ── 第③步预览（响应式方法调用，表单一变预览即变） ── */
      previewCfg() {
        var f = Object.assign({}, this.form);
        if (f.method === METHOD_AUTO) {
          f.method = (this.test && (this.test.forwardMethod || lastForwardMethod(this.test.methodUsed))) || 'GET';
        }
        return buildConfig(f, this.form.draftMode ? 'draft' : 'published', this.releaseMode);
      },
      previewYaml() { return buildYaml(this.previewCfg()); },

      /* ── 测试一把（bug7 三态门禁 → 2026-10-21 四态：ok_2xx/ok_post_confirmed 放行） ──
       * live 模式一律走服务端 probe（真实 HTTP；无浏览器 CORS 限制），不再用浏览器
       * mock 挡板绕过；demo 模式保留挡住板口径。方法跟随上游：AUTO 两步判定在服务端
       * 完成（GET 405→POST 复探）；仅显式方法 405＋Allow 建议时前端重测一次
       * （界面不提供 GET/POST 手选）。回填用 forwardMethod（methodUsed 是探测序列）。 */
      async runTestNow() {
        this.testing = true; this.test = null;
        try {
          if (this.source === 'live') {
            this.test = await this.probeLive(this.form);
            /* 跟随上游：405＋Allow 建议方法 → 自动改用建议方法并重测一次 */
            if (this.test.verdict === 'method_mismatch' && this.test.suggestedMethod &&
                String(this.form.method).toUpperCase() !== this.test.suggestedMethod) {
              this.form.method = this.test.suggestedMethod;
              this.test = await this.probeLive(this.form);
            }
            var fwd = this.test.forwardMethod || lastForwardMethod(this.test.methodUsed);
            if (this.test.passed && fwd) {
              this.form.method = fwd;
              this.note = '已探测：将使用 ' + fwd + ' 转发请求';
              var selfA = this; setTimeout(function () { selfA.note = ''; }, 4000);
            }
          } else {
            this.test = await runTest(this.form.routePath);
            this.test.verdict = this.test.passed ? 'ok_2xx' : 'unreachable';
          }
        } finally { this.testing = false; }
      },
      /* 试连（可传表单/列表行）：返回统一三态 test 对象；耗时缺失记 null → 页面显「—」 */
      async probeLive(f) {
        f = f || this.form;
        var j = await this.apiFetch(API.probe, {
          method: 'POST',
          body: JSON.stringify({
            upstreamUrl: String(f.upstreamUrl || '').trim(),
            method: f.method || METHOD_AUTO,
            timeoutSec: f.timeoutSec
          })
        }).catch(function (err) {
          return { ok: false, verdict: 'unreachable', status: 0, latencyMs: null,
                   error: err.message || '试连请求失败', errorKind: 'probe_request_failed',
                   message: '试连请求失败：' + (err.message || '后端不可达') };
        });
        var verdict = j.verdict || (j.ok ? 'ok_2xx' : 'unreachable');
        var code = Number(j.status || 0);
        /* 放行判定：2xx＝通过；ok_post_confirmed＝POST 复探被参数校验 400/422 拒
         * （上游在线、方法已确认，同为通过态——状态码如实保留，不伪装 2xx） */
        var passed = !!j.ok && (verdict === 'ok_2xx' ? (code >= 200 && code < 300)
                                : verdict === 'ok_post_confirmed' && (code === 400 || code === 422));
        return {
          code: code,
          elapsedMs: (j.latencyMs == null ? null : Number(j.latencyMs)),
          via: 'live-probe', scenario: '服务端真实探测',
          passed: passed,
          verdict: verdict,
          methodUsed: j.methodUsed || concreteMethod(f.method),
          forwardMethod: j.forwardMethod || lastForwardMethod(j.methodUsed) || '',
          probeSeq: j.probeSeq || j.methodUsed || concreteMethod(f.method),
          suggestedMethod: j.suggestedMethod || '',
          allow: j.allow || '',
          message: j.message || j.error || '',
          error: j.error || '',
          errorKind: j.errorKind || '',
          body: null
        };
      },
      /* 只读方法结论（界面不提供方法选择）：探测判定成功→明示将使用的方法；
       * 无法判定（不可达/405 且无 Allow）→提示检查上游地址。 */
      testMethodNote() {
        var t = this.test;
        if (!t) return '';
        if (t.verdict === 'ok_2xx' || t.verdict === 'ok_post_confirmed') {
          var fwd = t.forwardMethod || lastForwardMethod(t.methodUsed) || concreteMethod(this.form.method);
          return '将使用 ' + fwd + ' 转发请求';
        }
        if (t.verdict === 'method_mismatch' && t.suggestedMethod) {
          return '将使用 ' + t.suggestedMethod + ' 转发请求';
        }
        return '无法识别上游接口方法，请检查上游地址';
      },
      canPublish() {
        var t = this.test;
        if (!(t && t.passed)) return false;
        var k = verdictKey(t);
        if (k === 'ok_post_confirmed') return t.code === 400 || t.code === 422;
        return k === 'ok_2xx' && t.code >= 200 && t.code < 300;
      },
      testVerdictLabel() { return verdictLabel(this.test); },
      testVerdictClass() { return verdictClass(this.test); },
      testVerdictText() { return verdictText(this.test); },
      /* 2026-10-20 QC：仅「真不可达」（无 HTTP 响应）才提示服务名/localhost；
         405/5xx 已连通，红色「连不上？」会误导。 */
      showUnreachableHint() {
        var t = this.test;
        return !!(t && t.via === 'live-probe' && verdictKey(t) === 'unreachable' && !(t.code > 0));
      },
      testElapsedText() {
        var t = this.test;
        return (t && t.elapsedMs != null && t.elapsedMs >= 0) ? (t.elapsedMs + ' ms') : '—';
      },
      testTechnicalText() {
        var t = this.test;
        if (!t) return '';
        return [
          '结论: ' + verdictKey(t),
          '状态码: ' + (t.code > 0 ? t.code : '—'),   /* 0 是「无响应」哨兵，不展示成 HTTP 0 */
          '耗时: ' + this.testElapsedText(),
          '数据源: ' + this.testViaLabel(),
          '探测序列: ' + (t.probeSeq || t.methodUsed || '—'),   /* 2026-10-21 两步判定：如实显示如 GET→405→POST */
          'Allow: ' + (t.allow || '—'),
          '建议方法: ' + (t.suggestedMethod || '—'),
          '错误码: ' + (t.errorKind || '—'),
          '原始信息: ' + (t.message || t.error || '—')
        ].join('\n');
      },
      testViaLabel() {
        if (!this.test) return '';
        if (this.test.via === 'live-probe') return '服务端真实探测 · 后端 probe 端点';
        return this.test.via === 'fetch-mock-json'
          ? '真实 HTTP fetch · mock/upstream-target.json' : 'file:// 降级 · 内置挡板副本';
      },

      /* ── 落库：live（P1-10 接真）写 upstreams.yaml 真源；demo 落内存态（原口径不动）。
       *   P0-6 写操作统一过闸（按钮已禁用，此处兜底） ── */
      saveDraft() {
        this.saveError = '';
        this.finishEntry('draft');
      },
      publish() {
        if (!this.canPublish()) return;
        this.saveError = '';
        this.finishEntry('published');
      },
      finishEntry(status) {
        if (this.source === 'live') return this.finishLive(status);
        return this.finishDemo(status);
      },

      /* ── live 保存（P1-10）：POST /v1/admin/upstreams 原子写 upstreams.yaml ── */
      finishLive(status) {
        var self = this;
        var f = this.form;
        var operator = currentOperator();
        return this.apiFetch(API.save, {
          method: 'POST',
          body: JSON.stringify({
            serviceName: f.serviceName.trim(), routePath: f.routePath.trim(),
            method: concreteMethod(f.method),
            upstreamUrl: f.upstreamUrl.trim(), timeoutSec: f.timeoutSec, retryCount: f.retryCount,
            reuse: false, upstreamName: f.upstreamName.trim(),
            status: status, releaseMode: this.releaseMode, operator: operator
          })
        }).then(function (j) {
          self.applyEntry(j.entry);
          if (status === 'published') {
            self.traceDiff = {
              title: 'upstreams.yaml · ' + j.entry.id, meta: '配置文件',
              before: j.before || null, after: j.entry
            };
            self.lastTrace = '已写盘并发布 · ' + j.entry.id + ' · 操作人 ' + operator +
              ' · ' + nowHM() + ' · 发布后 ≤30 秒网关生效';
            self.followup = { open: true, serviceId: j.entry.id, serviceName: j.entry.serviceName };
          } else {
            self.traceDiff = null;
            self.lastTrace = '草稿已写盘（' + nowHM() + '·' + operator + '）——不对外转发，点发布才生效';
          }
          self.note = '已写入：' + j.entry.serviceName + '（' + (status === 'published' ? '已发布' : '草稿') + '）';
          setTimeout(function () { self.note = ''; }, 4000);
          self.step = 1; self.stepTouched = false; self.form = blankForm(); self.test = null;
        }).catch(function (err) {
          /* P1-10 差量·错误就近：保存失败提示挂在操作按钮旁（不再只落顶部 note） */
          self.saveError = '保存失败：' + (err && err.message ? err.message : '后端不可达');
          setTimeout(function () { self.saveError = ''; }, 6000);
        });
      },

      /* ── live 条目回填列表（按 id upsert；callCount7d 保留原值——后端未接入计量） ── */
      applyEntry(entry) {
        if (!entry) return;
        var idx = -1;
        for (var i = 0; i < this.proxies.length; i++) {
          if (this.proxies[i].id === entry.id) { idx = i; break; }
        }
        if (idx >= 0) {
          entry.callCount7d = this.proxies[idx].callCount7d != null ? this.proxies[idx].callCount7d : entry.callCount7d;
          this.proxies.splice(idx, 1, entry);
        } else this.proxies.push(entry);
      },

      /* ── demo 保存（内存态——本卡之前的原实现，演示模式行为不变） ── */
      finishDemo(status) {
        var cfg = this.previewCfg();
        if (!cfg.slug) return;
        var prev = null, idx = -1;
        for (var i = 0; i < this.proxies.length; i++) {
          if (this.proxies[i].routePath === cfg.routePath) { prev = Object.assign({}, this.proxies[i]); idx = i; break; }
        }
        var now = new Date().toISOString();
        var operator = currentOperator();
        var entry = {
          id: cfg.slug, serviceName: cfg.serviceName, routePath: cfg.routePath, method: cfg.method,
          upstreamUrl: cfg.upstreamUrl, timeoutSec: cfg.timeoutSec, retryCount: cfg.retryCount,
          reuseUpstream: false, upstreamName: cfg.upstreamId,
          status: status, env: cfg.releaseMode, instance: '',
          lastModifiedBy: operator, lastModifiedAt: now,
          publishedAt: status === 'published' ? now : (prev && prev.publishedAt) || null,
          gitShortHash: status === 'published' ? shortHash(cfg.slug + status + Date.now()) : null,
          callCount7d: prev ? prev.callCount7d || 0 : 0
        };
        if (idx >= 0) this.proxies.splice(idx, 1, entry); else this.proxies.push(entry);
        if (status === 'published') {
          this.traceDiff = { title: 'upstreams.yaml · ' + cfg.slug, meta: '演示模式', before: prev ? normalizeEntry(prev) : null, after: normalizeEntry(entry) };
          this.lastTrace = '已发布（演示）· 操作人 ' + operator + ' · ' + nowHM() + ' · 发布后 ≤30 秒网关生效';
          this.followup = { open: true, serviceId: entry.id, serviceName: entry.serviceName };
        } else {
          this.traceDiff = null;
          this.lastTrace = '草稿已写盘（' + nowHM() + '·' + operator + '）——不对外转发，点发布才生效';
        }
        this.note = '已保存：' + cfg.serviceName + '（' + (status === 'published' ? '已发布' : '草稿') + '）';
        var self = this;
        setTimeout(function () { self.note = ''; }, 4000);
        this.step = 1; this.stepTouched = false; this.form = blankForm(); this.test = null;   /* 复位，迎接下一条 */
      },

      /* ── 留痕条 → 只读 YAML diff 弹窗 ── */
      openDiff() {
        if (!this.traceDiff) return;
        var d = this.traceDiff;
        var lines = diffConfigLines(d.before || {}, d.after || {});
        this.diffModal = {
          open: true, title: d.title, meta: d.meta, lines: lines,
          added: lines.filter(function (l) { return l.op === '+'; }).length,
          removed: lines.filter(function (l) { return l.op === '-'; }).length
        };
      },

      /* ── 列表行操作 ── */
      statusBadge(s) { return s === 'published' ? '已发布' : (s === 'offline' ? '已下线' : '草稿'); },
      fmtDT(iso) { return fmtDateTime(iso); },
      async publishRow(i) {
        var e = this.proxies[i];
        var prev = Object.assign({}, e);
        this.testing = true;
        var t;
        if (this.source === 'live') {
          t = await this.probeLive(e);        /* 行内发布统一走真实 probe，禁用浏览器挡板绕过 */
        } else {
          t = await runTest(e.routePath);
          t.verdict = t.passed ? 'ok_2xx' : 'unreachable';
        }
        this.testing = false;
        if (!t.passed || verdictKey(t) !== 'ok_2xx') {
          this.note = '发布未通过：' + verdictText(t);
          var self0 = this; setTimeout(function () { self0.note = ''; }, 6000);
          return;
        }
        var operator = currentOperator();
        if (this.source === 'live') {         /* 行内发布写配置文件 */
          var selfL = this;
          return this.apiFetch(API.save, {
            method: 'POST',
            body: JSON.stringify({
              serviceName: e.serviceName, routePath: e.routePath, method: e.method,
              upstreamUrl: e.upstreamUrl, timeoutSec: e.timeoutSec, retryCount: e.retryCount,
              reuse: false, upstreamName: e.upstreamName || '',
              status: 'published', env: e.env, instance: e.instance, operator: operator
            })
          }).then(function (j) {
            selfL.applyEntry(j.entry);
            selfL.traceDiff = {
              title: 'upstreams.yaml · ' + j.entry.id, meta: '配置文件',
              before: j.before || null, after: j.entry
            };
            selfL.lastTrace = '已写盘并发布 · ' + j.entry.id + ' · 操作人 ' + operator +
              ' · ' + nowHM() + ' · 发布后 ≤30 秒网关生效';
            selfL.followup = { open: true, serviceId: j.entry.id, serviceName: j.entry.serviceName };
          }).catch(function (err) {
            selfL.note = '发布失败：' + (err && err.message ? err.message : '后端不可达');
            setTimeout(function () { selfL.note = ''; }, 6000);
          });
        }
        var now = new Date().toISOString();
        e.status = 'published'; e.lastModifiedBy = operator; e.lastModifiedAt = now;
        e.publishedAt = now; e.gitShortHash = shortHash(e.id + 'pub' + Date.now());
        this.traceDiff = { title: 'upstreams.yaml · ' + e.id, meta: '演示模式', before: normalizeEntry(prev), after: normalizeEntry(e) };
        this.lastTrace = '已发布（演示）· 操作人 ' + operator + ' · ' + nowHM() + ' · 发布后 ≤30 秒网关生效';
        this.followup = { open: true, serviceId: e.id, serviceName: e.serviceName };
      },
      askOffline(i) {   /* 下线＝危险操作两段式（v0.1 漏场景补位） */
        this.offlineModal = { open: true, stage: 1, index: i, armed: false };
      },
      confirmOffline() {
        var m = this.offlineModal;
        if (m.stage === 1) { m.stage = 2; return; }
        if (!m.armed) return;
        if (this.source === 'live') return this.confirmOfflineLive();
        var e = this.proxies[m.index];
        var prev = Object.assign({}, e);
        var now = new Date().toISOString();
        var operator = currentOperator();
        e.status = 'offline'; e.lastModifiedBy = operator; e.lastModifiedAt = now;
        e.gitShortHash = shortHash(e.id + 'off' + Date.now());
        this.traceDiff = { title: 'upstreams.yaml · ' + e.id, meta: '演示模式', before: normalizeEntry(prev), after: normalizeEntry(e) };
        this.lastTrace = '已下线（演示）· 操作人 ' + operator + ' · ' + nowHM() + ' · 发布后 ≤30 秒网关生效';
        m.open = false;
      },

      /* ── live 下线（P1-10）：POST offline → 配置文件状态翻转 ── */
      confirmOfflineLive() {
        var self = this;
        var m = this.offlineModal;
        var prev = this.proxies[m.index];
        var operator = currentOperator();
        return this.apiFetch(API.offline(prev.id), {
          method: 'POST',
          body: JSON.stringify({ operator: operator })
        }).then(function (j) {
          self.applyEntry(j.entry);
          self.traceDiff = {
            title: 'upstreams.yaml · ' + j.entry.id, meta: '配置文件',
            before: j.before || null, after: j.entry
          };
          self.lastTrace = '已下线并写盘 · ' + j.entry.id + ' · 操作人 ' +
            operator + ' · ' + nowHM() + ' · 发布后 ≤30 秒网关生效';
          m.open = false;
        }).catch(function (err) {
          self.note = '下线失败：' + (err && err.message ? err.message : '后端不可达');
          setTimeout(function () { self.note = ''; }, 6000);
        });
      },

      /* ── 删除草稿（两段确认：第一段点名条目，第二段确认删除） ── */
      askDeleteDraft(i) {
        this.draftDeleteModal = { open: true, stage: 1, index: i };
      },
      confirmDeleteDraft() {
        var m = this.draftDeleteModal;
        if (m.stage === 1) { m.stage = 2; return; }
        if (this.source === 'live') return this.confirmDeleteDraftLive();
        var e = this.proxies[m.index];
        if (e) this.proxies.splice(m.index, 1);
        this.lastTrace = '草稿已删除 · ' + (e && e.serviceName || '') + ' · ' + nowHM();
        m.open = false;
      },
      confirmDeleteDraftLive() {
        var self = this;
        var m = this.draftDeleteModal;
        var e = this.proxies[m.index];
        if (!e) { m.open = false; return; }
        return this.apiFetch(API.remove(e.id), {
          method: 'POST',
          body: JSON.stringify({ operator: currentOperator() })
        }).then(function () {
          var idx = self.proxies.findIndex(function (p) { return p.id === e.id; });
          if (idx >= 0) self.proxies.splice(idx, 1);
          self.lastTrace = '草稿已删除 · ' + e.serviceName + ' · ' + nowHM();
          m.open = false;
        }).catch(function (err) {
          self.note = '删除失败：' + (err && err.message ? err.message : '后端不可达');
          setTimeout(function () { self.note = ''; }, 6000);
        });
      },
      editRow() {   /* 编辑＝建设中（二期提供） */
        this.note = '编辑功能建设中，当前可通过新建向导完成配置调整';
        var self = this; setTimeout(function () { self.note = ''; }, 4000);
      }
    });
  };

  /* 纯函数单元导出（selftest 直测，不依赖 DOM） */
  root.O2 = {
    DEFAULT_TIMEOUT: DEFAULT_TIMEOUT, DEFAULT_RETRY: DEFAULT_RETRY, OPERATOR: OPERATOR,
    TIMEOUT_MIN: TIMEOUT_MIN, TIMEOUT_MAX: TIMEOUT_MAX, METHOD_AUTO: METHOD_AUTO,
    RELEASE_MODES: RELEASE_MODES,
    validateRoute: validateRoute, validateUrl: validateUrl,
    urlToNode: urlToNode, slugFromRoute: slugFromRoute, blankForm: blankForm,
    concreteMethod: concreteMethod, buildConfig: buildConfig, buildYaml: buildYaml, normalizeEntry: normalizeEntry,
    diffConfigLines: diffConfigLines, pickScenario: pickScenario, runTest: runTest,
    shortHash: shortHash, fmtDateTime: fmtDateTime, demoProxies: demoProxies, EMBEDDED_TARGET: EMBEDDED_TARGET,
    currentOperator: currentOperator, verdictKey: verdictKey, verdictLabel: verdictLabel,
    verdictClass: verdictClass, verdictText: verdictText
  };
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
