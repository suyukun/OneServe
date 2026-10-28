/* ============================================================
 * 服务发布 · 五步向导组件（publish.js · 卡3 流程②）
 * 依据：docs/plans/开工门槛-卡3-发布页.md §B3（契约冻结）＋装载与发布
 *      流程拆分设计_v1 §三（/v1/svc/<slug>；无服务修订号概念）。
 * 五步：① 选已装载表（assets）→ ② 选版本（tabver＋线上指针联动）→
 *      ③ 定服务（slug 机检）→ ④ 勾出参字段（schema＝投影白名单）→ ⑤ 发布。
 * 加载序：publish-hbase-adapter.js（契约层）→ 本文件 → alpine.min.js（硬规矩）。
 * 纯函数挂 window.WIZ（selftest 直调）；组件名 publishWizard（body x-data）。
 * 诚信线：assets/schema 端点失败＝如实错误条＋重试，绝不以示例数据冒充；
 *      未装载表不出现；schema 缺失＝禁用推进＋如实提示（不凭空造字段）。
 * 本页不做任何装载/发现/重灌（归数据装载页，流程①）；切版走 sync 三闸（按表隔离）。
 * ============================================================ */
(function (root) {
  'use strict';

  var HB = root.O1HBase;

  /* ── 纯函数（页面文案与状态推导唯一出处；selftest 直调） ── */
  function nameSuggest(t) {
    var n = (t && (t.tableCnName || t.table)) || '';
    return n ? (n + '查询') : '';
  }
  function routeSuggest(t) {
    /* W-6：表名下划线转连字符（usr_basic_info → /v1/svc/usr-basic-info）——
     * 自动建议必须满足自家校验口径；命名规范 v1 真源＝route-naming.js。 */
    var id = String((t && (t.table || t.name)) || '').replace(/_/g, '-');
    return id ? ('/v1/svc/' + id) : '';
  }
  var RN = root.ROUTE_NAMING || {};
  var ROUTE_RE = RN.routePattern ? new RegExp(RN.routePattern) : /$a^/;
  var PREFIX = RN.routePrefix || '/v1/svc/';
  var VER_RE = RN.versionTokenPattern ? new RegExp(RN.versionTokenPattern) : null;
  /* 命名机检（提案 v1 R1→R3→R4→R7，与后端 route_naming.route_violation 同常量同判定序；
   * 常量真源＝route-naming.js（生成物，gen_route_naming_js.py 从 route_naming.py 产出）。
   * 常量缺失＝fail-closed 全拒（ROUTE_RE 兜底永不匹配）。
   * P3（2026-10-08）：违规信息细化到「原因＋违例分词」（与 route_naming.route_violation
   * 的人话信息同口径）——③步红字可定位到具体哪一段不合规；规则源 route_naming.py。 */
  function routeViolationInfo(p) {
    var s = String(p || '').trim();
    if (s.indexOf(PREFIX) !== 0) return { clause: 'R1', reason: 'prefix', token: '' };
    var slug = s.slice(PREFIX.length);
    if (!ROUTE_RE.test(s)) {
      if (slug.indexOf('_') >= 0 || /[A-Z]/.test(slug)) return { clause: 'R1', reason: 'case', token: '' };
      if (slug.charAt(0) === '-' || slug.slice(-1) === '-' || slug.indexOf('--') >= 0)
        return { clause: 'R1', reason: 'hyphen', token: '' };
      if (slug.length < 3 || slug.length > 63)
        return { clause: 'R1', reason: 'length', token: '', len: slug.length };
      return { clause: 'R1', reason: 'format', token: '' };
    }
    var toks = slug.split('-');
    var i;
    for (i = 0; i < toks.length; i++) {
      if ((RN.reservedSlugs || []).indexOf(toks[i]) >= 0)
        return { clause: 'R3', reason: 'reserved', token: toks[i] };
    }
    for (i = 1; i < toks.length; i++) {
      if ((RN.verbTokens || []).indexOf(toks[i]) >= 0)
        return { clause: 'R4', reason: 'verb', token: toks[i] };
      if ((RN.domainCodes || []).indexOf(toks[i]) >= 0)
        return { clause: 'R7', reason: 'domain', token: toks[i] };
      if (VER_RE && VER_RE.test(toks[i]))
        return { clause: 'R7', reason: 'version', token: toks[i] };
    }
    return { clause: '', reason: '', token: '' };   /* 合法＝空条款（与旧签名兼容） */
  }
  function routeViolation(p) { return routeViolationInfo(p).clause; }
  function routeValid(p) { return routeViolation(p) === ''; }
  /* 红字原因文案：与 route_naming.route_violation 的 message 同口径（含改法）。
   * clause＋info（routeViolationInfo 产物；旧调用只传 clause 也能出通用文案）。 */
  function routeErrorText(clause, info) {
    var reason = (info && info.reason) || '';
    var tok = (info && info.token) || '';
    var len = (info && typeof info.len === 'number') ? info.len : 0;
    var msg;
    if (clause === 'R3') {
      msg = (tok ? ('地址中的「' + tok + '」是平台保留词（ping/admin/batch/query/proxy/demo 等平台占用段），'
        + '不能用作服务地址分词，请换成业务名词') : '地址含平台保留词（ping/admin/batch/query/proxy/demo 等）——请换成业务名词');
    } else if (clause === 'R4') {
      msg = (tok ? ('地址中的「' + tok + '」是动词（get/query/list/create 等黑名单）——本平台接口都是查询，'
        + '名字里带动词是冗余，请用业务名词') : '地址中的动词（get/query/list 等）是冗余——本平台接口都是查询，请用业务名词');
    } else if (clause === 'R7') {
      msg = reason === 'version'
        ? ('地址中的「' + tok + '」是版本号——服务版本不进路径，地址里不要带 v1/v2 段')
        : ('地址第二段起不得再出现域码' + (tok ? ('「' + tok + '」') : '')
          + '（一个地址只归一个业务域），请只保留首段域码');
    } else {
      /* R1 细分：前缀/大小写下划线/连字符位置/长度/通用（同 route_naming 判定序） */
      if (reason === 'prefix') {
        msg = '路由地址须以 /v1/svc/ 开头，形如 /v1/svc/ret-usr-basic（小写字母、数字、连字符）';
      } else if (reason === 'case') {
        msg = '地址须全小写：只允许小写字母、数字、连字符（不要用下划线 _ 或大写字母），多词用连字符分词';
      } else if (reason === 'hyphen') {
        msg = '连字符只能用在分词中间：地址不能以 - 开头或结尾，也不能连续 --';
      } else if (reason === 'length') {
        msg = '地址长度须 3~63 个字符（当前 ' + len + '）：建议「域码-业务名」两三段，如 /v1/svc/ret-usr-basic';
      } else {
        msg = '地址格式不对：须为 /v1/svc/ 开头＋小写字母起头、字母数字连字符、3~63 位，如 /v1/svc/ret-usr-basic';
      }
    }
    return msg + (RN.hint ? '；' + RN.hint : '');
  }
  function defaultVersionOf(t) {
    /* NC-3：优先 live:true；无线上版取最新已装载版（页面提示「尚未设为线上」）。 */
    var vs = (t && t.versions) || [];
    for (var i = 0; i < vs.length; i++) if (vs[i].live) return vs[i].version;
    return vs.length ? vs[vs.length - 1].version : '';
  }
  function liveVersionOf(t) {
    var vs = (t && t.versions) || [];
    for (var i = 0; i < vs.length; i++) if (vs[i].live) return vs[i].version;
    return '';
  }

  /* ── 失败文案（人话：出了什么事＋你该干什么） ── */
  function switchFailText(err) {
    var msg = String((err && err.message) || (err && err.title) || '原因未知');
    return '切换没有成功：' + msg + '。线上仍是原版本，可稍后再试。';
  }
  function publishFailText(err) {
    var code = String((err && err.code) || '');
    var msg = String((err && err.message) || '');
    if (code === 'FIELD_NOT_IN_DICT') {
      /* NC-9：字典语义换源——字段不在该表 schema（守卫 b 口径），不再引 297 字典。 */
      return '所选字段不在该表 schema（装载副产品）内，暂时不能发布。请返回第④步调整字段，或到数据装载页重跑装载。';
    }
    if (code === 'ROUTE_EXISTS' || code === 'DUPLICATE_ROUTE' || code === 'route_exists') {
      /* P3 二批（维护者 六条②）：占用＝冲突，人话给占用者＋出路（先下线/换地址）。 */
      var occ = msg.match(/「([^」]+)」/);
      return occ ? WIZ.routeOccupiedText(occ[1])
        : '该访问地址已被其他服务占用：请先在「已发布服务」页删除/下线它，或改用其他地址';
    }
    if (code === 'HBASE_UNAVAILABLE' || code === 'HBASE_AUTH_FAILED' || code === 'HBASE_UPSTREAM_ERROR'
        || code === 'HBASE_SCAN_TIMEOUT') {
      return msg ? ('发布没有完成：' + msg) : '发布所需的表样本暂不可得，发布没有完成。可稍后再试。';
    }
    return '发布没有完成（' + String((err && err.message) || (err && err.title) || '原因未知')
      + '）。请调整后重试，或联系平台管理员。';
  }
  function offlineFailText(err) {
    return '下线没有完成（' + String((err && err.message) || (err && err.title) || '原因未知')
      + '）。请重试，或联系平台管理员。';
  }
  function curlOf(route) {
    /* SQ-11：样例必须带 apikey 头（复制即用，防 401）。 */
    var o = (root.location && root.location.origin && String(root.location.origin).indexOf('http') === 0)
      ? root.location.origin : 'https://<平台访问地址>';
    return "curl -X POST '" + o + route + "' \
  -H 'Content-Type: application/json' \
  -H 'apikey: <调用凭证>' \
  -d '{\"key\": \"<用户标识>\"}'";
  }
  function checkedFieldsOf(fields, checks) {
    return (fields || []).filter(function (f) { return !!checks[f.name]; });
  }

  /* P3（2026-10-08 UX B1-6）：③步「可复制的示例键」——给调用方一个真实可查的 key 样例，
   * 免得对着摘要口径不知道该传什么（UX B3-6：试用台必然 found:false 的同源痛点）。
   * 键值＝共享栈 usr_basic_info 已装载版本（20261008/20261009）实测存在的行键；
   * 只放有实测凭据的表，没把握的表不编造（诚信线）。 */
  var EXAMPLE_KEYS = {
    usr_basic_info: 'a87ae39b27b7f83c2328e8bf4850a36bf1380032c252a1b2120d0d65f03d10d4'
  };
  function exampleKeyOf(table) {
    return EXAMPLE_KEYS[String(table || '')] || '';
  }

  /* P3 二批（2026-10-08 维护者 六条②）：③步占用检测——同 routePath 已被**其他服务**
   * （服务名不同）占用 → 返回占用者服务名，否则 ''。判定口径与后端
   * route_naming.find_route_occupant 一致（sid 由 route 派生，同 route 同 id；
   * 异名＝跨归属占用，同名重发＝同服务覆盖更新，不报占用）。 */
  function routeOccupantOf(route, name, services) {
    var r = String(route || '').trim();
    var n = String(name || '').trim();
    var list = services || [];
    for (var i = 0; i < list.length; i++) {
      var s = list[i] || {};
      if (String(s.routePath || '') !== r) continue;
      if (String(s.serviceName || '').trim() !== n) return String(s.serviceName || s.id || '已有服务');
    }
    return '';
  }
  /* 占用红字（维护者 原文口径）：给占用者＋两条出路（先下线/换地址）。 */
  function routeOccupiedText(occupant) {
    var who = String(occupant || '').trim();
    return '该访问地址已被服务「' + (who || '已有服务') + '」占用：'
      + '请先在「已发布服务」页删除/下线它，或改用其他地址';
  }

  /* ── 修三②（2026-10-08 维护者 实测：幽灵版本）——向导按数据面键数过滤 ──
   * 根因：②步版本清单源＝tabver 登记（/v1/admin/load/assets），版本空间删除后
   * 登记残留 → 出现「未上线 · 0 行 0 个字段」的幽灵版本；键数真源＝
   * /v1/admin/versions（SCAN tags:* 数据面聚合，只列 keys>0 的空间）。
   * 联查口径（选最简＝前端比对）：版本保留 ⇔ (表,版本) 命中 spaces 且键数>0；
   * 全部版本被滤掉的表（已删表）不再出现在①步选表列表。spaces 取数失败＝
   * 调用方不套过滤（对过滤降级，资产与错误呈现保持原口径）。 */
  function spaceKeyMap(spaces) {
    var m = {};
    (spaces || []).forEach(function (s) {
      var o = s || {};
      if (Number(o.keys || 0) > 0) {
        m[String(o.namespace || '') + '\u0000' + String(o.version || '')] = true;
      }
    });
    return m;
  }
  function filterGhostVersions(tables, spaces) {
    var live = spaceKeyMap(spaces);
    return (tables || []).map(function (t) {
      var ot = t || {};
      var vs = (ot.versions || []).filter(function (v) {
        return live[String(ot.table || '') + '\u0000' + String((v && v.version) || '')] === true;
      });
      return Object.assign({}, ot, { versions: vs });   /* 不改入参（缓存表对象原样） */
    }).filter(function (t) { return (t.versions || []).length > 0; });
  }

  /* ── 修四（2026-10-08）：覆盖更新提示——完成页「更新 vs 新增」二选一 ──
   * 判定源＝发布端点响应 action（updated＝同名同地址覆盖更新 200；created＝新增）。
   * 地址全网唯一、覆盖不产生第二条目 → 更新文案明示「地址原样保留」。 */
  function doneLineText(name, route, action) {
    var n = String(name || '');
    var r = String(route || '');
    if (action === 'updated') {
      return '本次发布更新了既有服务「' + n + '」（地址 ' + r + ' 原样保留），已上线，可被调用。';
    }
    return '本次发布新增了服务「' + n + '」（地址 ' + r + ' 为本次新建），已上线，可被调用。';
  }

  /* P3（UX B1-2）：④步折叠口径的数据源——列族去重清单＋元数据来源人话化 */
  function schemaCfsOf(fields) {
    var seen = {}, out = [];
    (fields || []).forEach(function (f) {
      var cf = String((f && f.cf) || '').trim();
      if (cf && !seen[cf]) { seen[cf] = true; out.push(cf); }
    });
    return out;
  }
  function schemaSourceTextOf(source) {
    var s = String(source || '');
    if (s === 'hbase+dict') return '来自表结构＋数据字典';
    return s ? ('来源 ' + s) : '来源未记录';
  }

  var WIZ = {
    nameSuggest: nameSuggest, routeSuggest: routeSuggest,
    routeValid: routeValid, routeErrorText: routeErrorText, routeViolation: routeViolation,
    routeViolationInfo: routeViolationInfo,
    defaultVersionOf: defaultVersionOf, liveVersionOf: liveVersionOf,
    switchFailText: switchFailText, publishFailText: publishFailText,
    offlineFailText: offlineFailText, curlOf: curlOf, checkedFieldsOf: checkedFieldsOf,
    exampleKeyOf: exampleKeyOf, schemaCfsOf: schemaCfsOf, schemaSourceTextOf: schemaSourceTextOf,
    routeOccupantOf: routeOccupantOf, routeOccupiedText: routeOccupiedText,
    spaceKeyMap: spaceKeyMap, filterGhostVersions: filterGhostVersions,
    doneLineText: doneLineText
  };
  root.WIZ = WIZ;
  root.O1 = WIZ;   /* 兼容旧命名（selftest/机检取纯函数入口） */

  root.publishWizard = function () {
    return {
      step: 1, done: false,
      stepNames: ['选表', '选版本', '定服务', '选字段', '发布'],
      /* ① assets */
      assetsTables: [], assetsBusy: false, assetsError: null,
      pickedTable: '',
      /* ② 版本＋切版 */
      pickedVersion: '', switched: false, switchError: '', switchBusy: false, rollbackBusy: false,
      /* ③ 定服务（keyType 只作发布载荷内部字段，不再上 UI 文案——P3 人话化）。
       * routeTaken＝占用者服务名（''＝未占用）：P3 二批占用检测（维护者 六条②）。 */
      svcName: '', route: '', routeError: '', routeTaken: '',
      occupancyTimer: null,
      keyType: 'phone_sm3', keyCopied: false,
      /* ④ 字段（schema 源）——schemaNeedReload＝元数据缺失类错误才亮「去装载页」指引；
       * schemaSource＝元数据来源（hbase+dict 等，折叠区展示口径与装载页一致） */
      schemaFields: [], fieldChecks: {}, schemaBusy: false, schemaError: '', schemaNeedReload: false,
      schemaSource: '',
      /* ⑤ 发布＋完成页 */
      publishError: '', publishBusy: false,
      publishedName: '', publishedRoute: '', publishedDataVersion: '',
      publishedAction: 'created', publishedServiceId: '',
      /* 已发布服务列表（P3 二批拆页后本页不再渲染列表/下线——仅作③步占用检测的
       * 数据源；列表与下线管理归 published.html）。 */
      services: [],

      /* ── 取数基础设施 ── */
      async apiFetch(url, opts) {
        try {
          var r = await root.fetch(url, Object.assign({
            headers: Object.assign({ 'Content-Type': 'application/json' },
                                   (root.AdminGate && root.AdminGate.adminHeaders()) || {})
          }, opts || {}));
          var j = {};
          try { j = await r.json(); } catch (e) { j = {}; }
          return { ok: r.ok, status: r.status, body: j };
        } catch (e) {
          return { ok: false, status: 0, body: { error: 'NETWORK', message: '网络连不上服务端' } };
        }
      },
      errOf(res) {
        var r = res || {};
        return HB.normalizeError(Object.assign({ status: r.status }, r.body || {}))
          || { code: 'UNKNOWN', status: 0, title: '', message: '原因未知', hint: '', fallback: false };
      },

      async boot() {
        await this.loadAssets();
        await this.loadServices();
      },

      /* ── ① 已装载资产（AC-2/AC-3：唯一表源；空态引导装载；不回落示例） ──
       * 修三②：取数后与 /v1/admin/versions（键数真源＝SCAN 数据面）联查——
       * 滤掉键数为 0 的幽灵版本与全部版本皆幽灵的表；键数端点失败＝不套过滤
       * （过滤是防幽灵兜底，不是向导门槛，不因此拦发布流程）。 */
      async loadAssets() {
        this.assetsBusy = true; this.assetsError = null;
        var res = await this.apiFetch(HB.CONTRACT.loadAssets);
        var vres = await this.apiFetch('/v1/admin/versions');
        this.assetsBusy = false;
        if (res.ok) {
          var norm = HB.normalizeAssets(res.body);
          if (norm.ok) {
            var vb = vres.body || {};
            this.assetsTables = (vres.ok && vb.ok === true)
              ? WIZ.filterGhostVersions(norm.tables, vb.spaces || [])
              : norm.tables;
            return true;
          }
        }
        var err = this.errOf(res);
        var why = (err && err.message && err.message !== '网络连不上服务端') ? (err.message + '。') : '';
        this.assetsError = {
          title: '暂时读不到已装载资产',
          message: why + '请稍后重试；若持续出现，请联系平台管理员。'
        };
        return false;
      },

      pickedTableObj() {
        var id = this.pickedTable;
        return this.assetsTables.filter(function (t) { return t.table === id; })[0] || null;
      },
      liveVersionOf(t) { return WIZ.liveVersionOf(t); },
      liveVersionOfPicked() { return this.liveVersionOf(this.pickedTableObj()); },
      versionsOfPicked() { return (this.pickedTableObj() || {}).versions || []; },
      isLatestOfPicked(v) {
        var vs = this.versionsOfPicked();
        return vs.length > 0 && vs[vs.length - 1].version === v.version;
      },
      isVersionLive() { return !!this.pickedVersion && this.pickedVersion === this.liveVersionOfPicked(); },

      pickTable(t) {
        this.pickedTable = t.table;
        this.pickedVersion = WIZ.defaultVersionOf(t);
        this.switched = false; this.switchError = '';
        this.svcName = WIZ.nameSuggest(t);
        this.route = WIZ.routeSuggest(t);
        this.validateRoute();          /* P3：建议地址进③步即校验（绿勾/红字即时呈现） */
        this.keyType = 'phone_sm3'; this.keyCopied = false;
        this.schemaFields = []; this.fieldChecks = {}; this.schemaError = ''; this.schemaNeedReload = false;
        this.schemaSource = '';
        this.publishError = ''; this.done = false;
      },

      pickVersion(v) {
        this.pickedVersion = v;
        this.switched = false; this.switchError = '';
        /* 换版本＝字段面结论作废（schema 随版本走） */
        this.schemaFields = []; this.fieldChecks = {}; this.schemaError = ''; this.schemaNeedReload = false;
        this.schemaSource = '';
      },

      gotoStep(n) {
        this.step = n;
        if (n === 3) this.validateRoute();   /* P3：进③步即对当前地址跑一次机检 */
        if (n === 4) this.ensureSchema();
      },

      /* ── ② 设为线上/切回（NC-1：复用 sync 三闸，按表隔离） ── */
      async switchNow() {
        if (!this.pickedVersion || this.switchBusy) return;
        this.switchBusy = true; this.switchError = '';
        var res = await this.apiFetch(HB.CONTRACT.syncSwitch, {
          method: 'POST',
          body: JSON.stringify(HB.buildSwitchPayload({
            table: { table: this.pickedTable }, version: this.pickedVersion
          }))
        });
        this.switchBusy = false;
        if (res.ok) {
          this.switched = true;
          await this.loadAssets();      /* live 标记随指针刷新（幂等重取） */
        } else {
          this.switchError = WIZ.switchFailText(this.errOf(res));
        }
      },
      async rollbackNow() {
        if (this.rollbackBusy) return;
        this.rollbackBusy = true; this.switchError = '';
        var res = await this.apiFetch(HB.CONTRACT.syncRollback, {
          method: 'POST',
          body: JSON.stringify(HB.buildRollbackPayload({ table: { table: this.pickedTable } }))
        });
        this.rollbackBusy = false;
        if (res.ok) {
          this.switched = false;
          await this.loadAssets();
        } else {
          this.switchError = WIZ.switchFailText(this.errOf(res));
        }
      },

      /* ── ③ 定服务（P3：实时校验三态——空＝灰提示、违规＝红字原因、合法＝绿勾；
       *    P3 二批：合法后叠加占用检测——格式绿≠可用，被占同样红字拦下） ── */
      validateRoute() {
        var info = WIZ.routeViolationInfo(this.route);
        if (info.clause) {
          this.routeTaken = '';
          this.routeError = WIZ.routeErrorText(info.clause, info);
          return;
        }
        /* 格式合法 → 比对已发布列表（同 route 异名＝占用；与后端 find_route_occupant 同口径） */
        this.routeTaken = WIZ.routeOccupantOf(this.route, this.svcName, this.services);
        this.routeError = this.routeTaken ? WIZ.routeOccupiedText(this.routeTaken) : '';
      },
      /* 输入防抖 400ms 后调后端查重（GET /v1/admin/publish 列表刷新缓存再判定）——
       * 即时判定用缓存（快），防抖后重取列表（准：他人刚发布的服务也能拦下）。 */
      onRouteInput() {
        this.validateRoute();
        if (this.occupancyTimer) clearTimeout(this.occupancyTimer);
        var self = this;
        this.occupancyTimer = setTimeout(async function () {
          await self.loadServices();
          self.validateRoute();
        }, 400);
      },
      nameRouteOk() {
        return String(this.svcName || '').trim() !== '' && WIZ.routeValid(this.route) && !this.routeTaken;
      },
      keyExample() { return WIZ.exampleKeyOf(this.pickedTable); },
      copyKeyExample() {
        var input = document.getElementById('key-example-input');
        var self = this;
        var done = function (ok) {
          self.keyCopied = ok;
          setTimeout(function () { self.keyCopied = false; }, 2000);
        };
        if (input && input.select) { input.select(); }
        var key = this.keyExample();
        if (root.navigator && root.navigator.clipboard && root.navigator.clipboard.writeText) {
          root.navigator.clipboard.writeText(key).then(function () { done(true); }, function () {
            done(self._copyFallback(input));
          });
          return;
        }
        done(this._copyFallback(input));
      },
      _copyFallback(input) {
        try { return !!(document.execCommand && document.execCommand('copy')); } catch (e) { return false; }
      },

      /* ── ④ 字段（AC-7/AC-8：schema 唯一源；unknown/待补 原样透出 NC-7） ──
       * P3（2026-10-08）：错误分类给文案＋行动指引，不裸报后端 message——
       * SCHEMA_NOT_FOUND＝历史旧版本（元数据功能上线前装载）或版本号不存在，
       * 明说「换版本日期重灌」（装载页同表同日严格幂等，原地重灌必被拒）。 */
      async ensureSchema() {
        if (this.schemaFields.length > 0 || this.schemaBusy || !this.pickedTable || !this.pickedVersion) return;
        this.schemaBusy = true; this.schemaError = ''; this.schemaNeedReload = false;
        var res = await this.apiFetch(HB.loadSchemaUrl(this.pickedTable, this.pickedVersion));
        this.schemaBusy = false;
        if (res.ok) {
          var norm = HB.normalizeSchema(res.body);
          if (norm.ok && norm.fields.length > 0) {
            this.schemaFields = norm.fields;
            this.schemaSource = norm.source;   /* P3：折叠区「字段元数据来源」与装载页同口径 */
            var checks = {};
            norm.fields.forEach(function (f) { checks[f.name] = true; });   /* NC-8：默认全选 */
            this.fieldChecks = checks;
            return;
          }
          this.schemaNeedReload = true;
          this.schemaError = '该版本的 schema 元数据为空（fieldCount=0）——该版本不支持发布，需补齐元数据';
          return;
        }
        var err = this.errOf(res);
        var code = String((err && err.code) || '');
        if (code === 'SCHEMA_NOT_FOUND') {
          this.schemaNeedReload = true;
          this.schemaError = '表 ' + this.pickedTable + ' 版本 ' + this.pickedVersion
            + ' 无字段元数据（旧版本装载早于元数据记录，或版本号不存在）——该版本不支持发布';
        } else if (code === 'SCHEMA_QUERY_INVALID') {
          /* 防御分支：loadSchemaUrl 修复后不应出现——出现即前端回归，不把参数错误甩给用户 */
          this.schemaError = '字段元数据请求参数异常（前端拼参回归）——请刷新页面重试；若持续出现请反馈平台运维';
        } else {
          this.schemaError = '字段元数据读取失败（' + ((err && err.message) || '原因未知') + '）——请稍后重试；持续出现请联系平台管理员';
        }
      },
      checkedCount() { return WIZ.checkedFieldsOf(this.schemaFields, this.fieldChecks).length; },
      /* P3（UX B1-2）：④步折叠口径——列族清单与元数据来源（纯函数经 WIZ 供 selftest） */
      schemaCfs() { return WIZ.schemaCfsOf(this.schemaFields); },
      schemaSourceText() { return WIZ.schemaSourceTextOf(this.schemaSource); },
      allFieldsChecked() {
        var fs = this.schemaFields;
        return fs.length > 0 && fs.every(function (f) { return !!this.fieldChecks[f.name]; }, this);
      },
      toggleAllFields(ev) {
        var on = !!(ev && ev.target && ev.target.checked);
        var checks = {};
        this.schemaFields.forEach(function (f) { checks[f.name] = on; }, this);
        this.fieldChecks = checks;
      },

      /* ── ⑤ 发布（B3.2 扁平契约；覆盖更新语义＝契约就位，机制待卡4） ── */
      canPublish() {
        return !this.publishBusy && this.nameRouteOk() && this.checkedCount() > 0 && !!this.pickedVersion;
      },
      async publishNow() {
        if (!this.canPublish()) return;
        this.publishError = ''; this.publishBusy = true;
        var fields = WIZ.checkedFieldsOf(this.schemaFields, this.fieldChecks);
        var payload = HB.buildPublishPayload({
          table: { table: this.pickedTable },
          serviceName: String(this.svcName).trim(),
          routePath: String(this.route).trim(),
          fields: fields,
          keyType: this.keyType,
          cf: (fields[0] && fields[0].cf) || 'cf',
          loadVersion: this.pickedVersion,
          status: 'published', operator: 'console'
        });
        var res = await this.apiFetch(HB.CONTRACT.publish, { method: 'POST', body: JSON.stringify(payload) });
        this.publishBusy = false;
        if (res.ok) {
          this.publishedName = String(this.svcName).trim();
          this.publishedRoute = String(this.route).trim();
          this.publishedDataVersion = String(res.body.dataVersion || this.pickedVersion);
          this.publishedAction = String(res.body.action || 'created');
          this.publishedServiceId = String(res.body.serviceId || '');
          this.done = true;
          await this.loadServices();
        } else {
          var err = this.errOf(res);
          var code = String(err.code || '');
          if (code === 'ROUTE_EXISTS' || code === 'DUPLICATE_ROUTE' || code === 'route_exists') {
            /* P3 二批（维护者 六条②）：后端 409 占用冲突——⑤步红字可见＋③步同步红字 */
            this.publishError = WIZ.publishFailText(err);
            this.routeError = this.publishError;
            await this.loadServices();
            this.validateRoute();
          } else {
            this.publishError = WIZ.publishFailText(err);
          }
        }
      },
      curlSample() { return WIZ.curlOf(this.publishedRoute); },
      /* 修四：完成页主文案＝「更新 vs 新增」二选一（判定源＝端点响应 action）。 */
      doneLineText() {
        return WIZ.doneLineText(this.publishedName, this.publishedRoute, this.publishedAction);
      },

      finishWizard() {
        this.done = false; this.step = 1;
        this.pickedTable = ''; this.pickedVersion = '';
        this.switched = false; this.switchError = '';
        this.svcName = ''; this.route = ''; this.routeError = ''; this.routeTaken = '';
        this.schemaFields = []; this.fieldChecks = {}; this.schemaError = ''; this.schemaNeedReload = false;
        this.schemaSource = '';
        this.publishError = '';
        this.loadAssets();
      },
      /* P3 二批（维护者 六条③）：已发布服务拆独立页——「查看已发布服务」直接跳转。 */
      viewServices() {
        root.location.href = 'published.html';
      },

      /* ── 已发布服务列表取数（占用检测数据源；列表渲染/下线归 published.html） ── */
      async loadServices() {
        var res = await this.apiFetch(HB.CONTRACT.consoleView);
        if (res.ok) this.services = HB.normalizeServices(res.body);
      }
    };
  };
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
