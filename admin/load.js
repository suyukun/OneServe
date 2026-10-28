/* ============================================================
 * OneServe · 数据装载页组件（load.js · 卡2 流程①，Alpine）
 * 依据：docs/plans/开工门槛-卡2-装载页.md §B3.1（端点契约 Lead 已冻结）
 *      ＋docs/plans/装载与发布流程拆分设计_v1 §二＋页面视觉标准_v1。
 * 加载序：admin-gate.js → 本文件 → alpine.min.js（硬规矩，同 publish/appkeys）。
 *
 * 四步：① 选源（连接状态真探活）→ ② 发现表（真连接清单，不读注册表；收权后只列
 *      源表，字段清单折叠已删——字段释义确认挪③）→ ③ 全量装载
 *      （**无勾字段控件**——AC-8：装载不裁列，字段投影归流程②）→ ④ 结果
 *      （表:版本/行数/对账；「去发布」直达发布台）。
 * 诚信线：HBase 不可达＝分类错误如实展示（unreachable/auth_failed/other＋行动指引），
 *      绝不显示假表清单；同表同日 409＝红色如实拒绝＋显式「覆盖重灌」
 *      （原生 confirm 二次确认；replace 走既有 /v1/admin/publish/hbase/load 端点，
 *      装载页自身 POST /v1/admin/load 保持严格幂等不覆盖，NC-2）。
 * 纯函数挂 window.O2LOAD（selftest 可直调）；组件名 loadWizard（body x-data）。
 * P3 追加（同日）：字段清单头部加「查询主键」行——keyHint 来自注册表（console 写区
 *      key 描述，如「usr_id 的 phone_sm3」），装载态无关（未装载表同样显示）；
 *      未登记表如实标注，不猜默认键型。
 * P3-装载超时修复卡（2026-10-08）：装载响应三分支人话化（非 JSON＝网关超时背景进行
 *      中 / 409＝可能此前已成功 / 其余错误信封）＋装载计时器（每秒跳，响应到达即停）
 *      ＋覆盖重灌期间 409 面板保持在场——根因：大表装载分钟级超 nginx 默认 60s →
 *      504 HTML → 裸 SyntaxError，且面板瞬失无任何反馈（维护者 usr_asset_tag 100 万行
 *      实测链路；nginx 侧已同步放宽 proxy_read/send_timeout 600s）。
 * P3-装载409分支修复卡（2026-10-08）：409 分流——目标版本==该表行 loaded.latestVersion
 *      （当前线上版）→ 前端**不发请求**（线上版后端连 replace 也拒：线上版保护，正确
 *      设计；一律引导覆盖重灌即死循环，维护者 实测 card_acct_summary 卡死），琥珀提示
 *      换版本日期；非线上版 409 → 现有覆盖面板照旧（replace 实测可行）。
 * P3 三批（2026-10-08，维护者 实测两修）：
 *   修一 schema 缺失态回退——/v1/admin/load/schema 读不到（404/缺失，实测
 *      usr_basic_info 20261009）时已装载表回退渲染 discovery 样本列；两态统一逻辑：
 *      schema 可读→完整字段表，不可读→样本列（旧占位态已随收权批退役）。
 *   修二 键行展示——字段名行「查询主键字段名：key（HBase rowkey）」固定；
 *      描述行有登记时「含义：…」（值＝发布登记人工填写的 key 含义）；未登记不显示。
 * 字段释义收权（2026-10-14，维护者 拍板·门槛稿 v1）：DDL 释义定义权收进装载环节、绑定
 *      Redis 侧结构——②发现表页只列源表（字段折叠与登记卡已删）；③选表后
 *      initFieldDefs 带出字段释义清单：已装载表默认沿用上次版本 sync:schema 释义
 *      （同字段集零操作可继续），读不到（404/网络失败）或未装载表回退 discovery
 *      样本列（释义全缺如实标出）；贴 DDL → 复用 B1 parse/save 端点 → ddlConfirm
 *      整表替换当次清单（含新增字段，按名替换不错位）→ 装载随版本固化 sync:schema，
 *      发布/凭证/文档同源读取；CSV 只作装载富化沿用库，不再驱动页面展示。
 * ============================================================ */
(function (root) {
  'use strict';

  var API = {
    sources: '/v1/admin/load/sources',
    tables: '/v1/admin/load/tables',
    load: '/v1/admin/load',
    /* B3.1-5 表级 schema 元数据（字段清单数据源；发布向导字段勾选同源） */
    schema: '/v1/admin/load/schema',
    /* 409 覆盖重灌唯一出口：既有发布装载端点支持 replace:true（清空重灌）；
     * POST /v1/admin/load 本身强制幂等（NC-2），前端不复用其做覆盖。 */
    replaceLoad: '/v1/admin/publish/hbase/load',
    /* B1 端点（parse 预览 / save 写回沿用库）——收权后入口＝③ 贴 DDL 覆盖释义 */
    schemaParse: '/v1/admin/schema/parse',
    schemaSave: '/v1/admin/schema/save'
  };

  /* ── 纯函数（页面文案与状态推导唯一出处） ── */
  function pad2(x) { return ('0' + x).slice(-2); }

  function todayStr() {
    /* P3-bug1：YYYY-MM-DD 须零填充——非填充（2026-10-8）会被 date input 判非法丢弃，
     * 状态残值直提交易得畸形版本号 2026108。 */
    var d = new Date();
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }

  function versionOf(dateStr) { return String(dateStr || '').replace(/-/g, ''); }

  /* N4（2026-10-14 门槛稿 AC-2）：连接状态探测时刻（本地时区 HH:MM:SS）——
   * 步骤①卡片内时间戳行展示，探测何时发生一目了然（顺带收紧卡片下方空旷感）。 */
  function probeTimeStr() {
    var d = new Date();
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  /* P3-装载409分支修复卡：409 两情形前端先分流——
   * ① 目标版本==该表行 loaded.latestVersion（发现表列表数据自带）＝当前线上版：
   *    后端连 replace 也拒（线上版保护，正确设计）→ 不发请求，琥珀提示换日期；
   * ② 非线上版 409：既有覆盖面板（replace:true 实测可行）。 */
  function isLiveVersion(t, dateStr) {
    var live = t && t.loaded && t.loaded.latestVersion;
    return !!live && versionOf(dateStr) === String(live);
  }

  /* 未释义判定（收权 2026-10-14，AC-5）：cnName 空/「待补」即缺——字段集变化时
   * 标出无释义字段引导补齐，不静默沿用错位释义。 */
  function defMissing(r) {
    var cn = String((r && r.cnName) || '').trim();
    return !cn || cn === '待补';
  }

  /* 次日建议日期（YYYY-MM-DD，date input 可直填；非法输入如实空串） */
  function nextDateStr(dateStr) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateStr || ''));
    if (!m) return '';
    var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    d.setDate(d.getDate() + 1);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }

  /* 线上版琥珀提示全文（步骤③ 内联展示文案唯一出处，O2LOAD 可直调） */
  function liveBlockText(dateStr) {
    return '版本 ' + versionOf(dateStr) + ' 是当前线上版，线上版不可覆盖重灌（保护在线调用）。'
      + '请换一个版本日期（如 ' + (nextDateStr(dateStr) || '次日日期') + '）装载新版本，'
      + '装载成功后可在发布台切换上线。';
  }

  function formatScanTime(iso) {
    /* P3 二批：discovery 的 checkedAt 为 ISO 8601（如 2026-10-08T05:52:05+00:00），
     * 运营不可读——转**本地时区**「YYYY-MM-DD HH:MM:SS」（去 T 与时差后缀）；
     * 空/非法输入原样透传（如实，不编造）。 */
    var d = new Date(iso || '');
    if (!iso || isNaN(d.getTime())) return iso || '';
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate())
         + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function errorText(body, fallback) {
    /* 后端错误信封 {error, category?, message} → 一句话人话（缺 message 用兜底） */
    if (!body) return fallback;
    return body.message || (body.error ? '错误码 ' + body.error : fallback);
  }

  function conflictText(body) {
    /* 409 人话（P3-装载超时修复卡定稿）：网关超时后服务端常已跑完——重试即 409，
     * 明示「可能此前已成功」，出路指向覆盖重灌（面板常驻红警示＋confirm 二次确认）。 */
    return '该表该版本已装载（可能此前已成功）。如需重灌请使用覆盖重灌。';
  }

  /* P3-装载超时修复卡：网关超时/代理错页（非 JSON 响应）的统一人话文案 */
  var TIMEOUT_TEXT = '装载仍在后台进行（大表需数分钟），网关已超时返回。'
    + '请勿重复提交；约 3-5 分钟后刷新本页查看结果，或检查 Redis 键数';

  /* 安全 JSON 读取：content-type 含 text/html 或解析失败 → null（调用方走人话分支）。
   * 装载族后端响应恒为 JSON 对象，null 即「不是 JSON」——杜绝裸 SyntaxError。 */
  async function readJson(r) {
    var ctype = (r.headers && r.headers.get('content-type')) || '';
    if (ctype.indexOf('text/html') >= 0) return null;
    try { return await r.json(); } catch (e) { return null; }
  }

  root.O2LOAD = { todayStr: todayStr, versionOf: versionOf, formatScanTime: formatScanTime,
                  errorText: errorText, conflictText: conflictText, readJson: readJson,
                  isLiveVersion: isLiveVersion, nextDateStr: nextDateStr,
                  liveBlockText: liveBlockText, defMissing: defMissing,
                  probeTimeStr: probeTimeStr };

  root.loadWizard = function () {
    return {
      step: 1,
      /* ① 源（N4 2026-10-14：probedAt＝连接状态探测时刻，卡片内时间戳行展示） */
      sources: [], sourceError: '', sourceBusy: false, probedAt: '',
      /* ② 表清单 */
      tables: [], tablesError: '', tablesErrorTitle: '', tablesBusy: false, checkedAt: '',
      /* 字段释义（收权 2026-10-14）：③ 选表后带出——carry＝沿用上次版本释义；
       * sample＝未装载过或上次版本 schema 读不到 → discovery 样本列（释义全缺）；
       * ddl＝用户贴 DDL 确认后整表替换。fieldRows 即当次装载确认释义清单。 */
      fieldState: 'idle', fieldVersion: '', fieldRows: [],
      /* 贴 DDL 覆盖（可选）：rows＝parse 预览行；added＝parse diff 标出的新增字段 */
      ddlImp: { ddl: '', rows: [], added: [], diff: '', note: '', busy: false },
      /* ③ 装载（P3-装载超时卡：loadElapsed/loadTimer＝计时反馈态；
       *    B2 2026-10-09：主装载改异步任务——loadProgress＝后台进度百分比） */
      loadProgress: 0,
      picked: null, cf: '', version: todayStr(),
      loading: false, conflict: '', loadError: '',
      loadElapsed: 0, loadTimer: null,
      /* ④ 结果 */
      result: null,

      get sourceOk() {
        return this.sources.length > 0 && this.sources[0].status === 'ok';
      },

      stepClass(n) {
        return this.step > n ? 'is-done' : (this.step === n ? 'is-current' : '');
      },

      async boot() {
        await this.refreshSources();
      },

      async refreshSources() {
        this.sourceBusy = true; this.sourceError = '';
        try {
          var body = await root.fetch(API.sources, { headers: Object.assign({ 'Accept': 'application/json' }, (root.AdminGate && root.AdminGate.adminHeaders()) || {}) })
            .then(function (r) { return r.json(); });
          this.sources = body.sources || [];
          this.probedAt = probeTimeStr();   /* N4：探测完成即记录时刻（本地时区） */
        } catch (e) {
          this.sources = [];
          this.sourceError = '连接状态读取失败：' + e;
        } finally {
          this.sourceBusy = false;
        }
      },

      async discover() {
        this.step = 2; this.tablesBusy = true;
        this.tablesError = ''; this.tablesErrorTitle = ''; this.tables = [];
        try {
          var r = await root.fetch(API.tables, { headers: Object.assign({ 'Accept': 'application/json' }, (root.AdminGate && root.AdminGate.adminHeaders()) || {}) });
          var body = await r.json();
          if (!r.ok) {
            /* 三分类如实展示（unreachable 带 up_hbase.sh 行动指引；不列假表） */
            this.tablesErrorTitle = '表清单不可用（' + (body.category || body.error || '未知') + '）';
            this.tablesError = errorText(body, 'HBase 不可达。');
            return;
          }
          this.tables = body.tables || [];
          this.checkedAt = body.checkedAt || '';
        } catch (e) {
          this.tablesErrorTitle = '请求失败';
          this.tablesError = String(e);
        } finally {
          this.tablesBusy = false;
        }
      },

      /* ── 字段释义清单（收权 2026-10-14）：已装载表默认沿用上次版本 sync:schema 释义；
       * 未装载过或上次版本 schema 读不到（404/网络失败）→ 回退 discovery 样本列，
       * 释义全缺如实标出（引导贴 DDL）。CSV 不再驱动页面展示（门槛稿 A5）。 ── */
      newestOf(t) {
        var vs = (t.loaded && t.loaded.versions) || [];
        return vs.length ? vs[vs.length - 1] : '';
      },

      /* discovery families[].sampleColumns 展平去重（样本列字段集数据源） */
      sampleCols(t) {
        var out = [];
        (t.families || []).forEach(function (f) {
          (f.sampleColumns || []).forEach(function (c) {
            if (out.indexOf(c) < 0) out.push(c);
          });
        });
        return out;
      },

      /* 样本列 → 无释义行（字段集以装载观测为准，释义如实缺） */
      sampleRows(t) {
        return this.sampleCols(t).map(function (n) {
          return { name: n, type: '', cnName: '', desc: '', added: false };
        });
      },

      /* 选表后带出释义清单（AC-1 零操作：同字段集默认沿用上次释义即可继续） */
      async initFieldDefs(t) {
        var v = this.newestOf(t);
        if (!v) {                                   /* 未装载过 → 样本列（释义全缺） */
          this.fieldState = 'sample';
          this.fieldVersion = '';
          this.fieldRows = this.sampleRows(t);
          return;
        }
        this.fieldState = 'loading';
        this.fieldVersion = v;
        this.fieldRows = [];
        try {
          var r = await root.fetch(
            API.schema + '?table=' + encodeURIComponent(t.full) + '&version=' + encodeURIComponent(v),
            { headers: Object.assign({ 'Accept': 'application/json' }, (root.AdminGate && root.AdminGate.adminHeaders()) || {}) });
          var body = await r.json();
          if (r.ok && body.fields && body.fields.length) {
            this.fieldState = 'carry';               /* 沿用上次释义（同字段集零操作继续） */
            this.fieldRows = body.fields.map(function (f) {
              return { name: f.name, type: f.type || '', cnName: f.cnName || '',
                       desc: f.desc || '', added: false };
            });
            return;
          }
        } catch (e) { /* 网络失败同 404：回退样本列（如实，不编造） */ }
        this.fieldState = 'sample';                  /* 上次版本无 schema（实测 card_acct_summary 20261009） */
        this.fieldVersion = '';
        this.fieldRows = this.sampleRows(t);
      },

      /* 释义清单状态行文案（唯一出处；源页/产物同断言） */
      fieldStateText() {
        if (!this.picked) return '';
        if (this.fieldState === 'loading') return '字段释义读取中…';
        if (this.fieldState === 'carry')
          return '默认沿用上次释义（版本 ' + this.fieldVersion + ' · ' + this.fieldRows.length
               + ' 个字段）——字段集未变可直接装载；贴 DDL 可覆盖。';
        if (this.fieldState === 'ddl')
          return '已按本次 DDL 覆盖释义（' + this.fieldRows.length + ' 个字段）——装载时随版本固化。';
        return '样本列 ' + this.fieldRows.length + ' 个（未装载过或上次版本无释义；实际字段以装载观测为准）——释义待补，建议贴 DDL。';
      },

      /* 未释义判定（AC-5）：cnName 空/待补即缺——标出引导补齐，不静默沿用错位 */
      defMissing(r) { return defMissing(r); },

      get missingCount() {
        return this.fieldRows.filter(defMissing).length;
      },

      /* 未释义引导文案（含新增字段数，琥珀面板） */
      missingText() {
        var added = this.fieldRows.filter(function (r) { return r.added; }).length;
        return '有 ' + this.missingCount + ' 个字段未释义'
             + (added ? '（其中新增 ' + added + ' 个）' : '')
             + '——建议贴 DDL（字段 COMMENT 即中文释义）补齐后再装载；'
             + '未释义字段装载后发布凭证/文档将缺中文说明。';
      },

      /* ② 表清单扫描时间（本地时区可读格式，P3 二批） */
      scanTime() { return formatScanTime(this.checkedAt); },

      /* P3-装载409分支修复卡：目标版本==线上版预判（随版本输入即时重算）——
       * 命中即步骤③ 内联琥珀提示（提交不发请求），否则空串不显示。 */
      get liveBlock() {
        return (this.picked && isLiveVersion(this.picked, this.version))
          ? liveBlockText(this.version) : '';
      },

      /* 版本号（8 位）：409 面板小字动态引用（x-text 作用域只见组件成员） */
      versionNum() { return versionOf(this.version); },

      /* 键行描述：keyHint 来自 /v1/admin/load/tables 表行（发布登记人工填写的
       * key 含义），装载态无关；未登记则不显示描述行。 */
      keyDescLine(t) {
        var h = String((t && t.keyHint) || '').trim();
        return h ? '含义：' + h
                 : '';
      },

      pick(t) {
        this.picked = t;
        /* P3 二批：列族选择器已删（维护者：实际只有 cf 一个值）——静默取 discovery
         * 列族（默认 cf），请求体照常带 cf，功能不变。 */
        this.cf = (t.families[0] || {}).name || 'cf';
        this.conflict = ''; this.loadError = '';
        /* 收权（2026-10-14）：选表即带出字段释义清单（AC-1 零操作沿用）＋清 DDL 态 */
        this.ddlImp = { ddl: '', rows: [], added: [], diff: '', note: '', busy: false };
        this.initFieldDefs(t);
        this.step = 3;
      },

      async submit() {
        /* P3-装载409分支修复卡：目标版本==线上版 → 不发请求直接返回（琥珀面板已给
         * 换日期出路；该请求必 409 且 replace 也被线上版保护拒——死循环根源）。 */
        if (isLiveVersion(this.picked, this.version)) return;
        await this.doLoad(false);
      },

      /* 覆盖重灌：红字警示已在 409 面板常驻；此处原生 confirm 二次确认（不另加步骤）。
       * loading 中重入直接忽略（防计时器/请求双开）。 */
      async overwrite() {
        if (!this.picked || this.loading) return;
        var ok = window.confirm('覆盖重灌：将先清空 ' + this.picked.full + ' 版本 '
          + versionOf(this.version) + ' 的已装载数据，再全量重新装载。确定继续？');
        if (!ok) return;
        await this.doLoad(true);
      },

      /* P3-装载超时卡：装载计时器——提交即起表，响应（任何形态）到达即停；
       * 「已用时 Xs」每秒跳，大表分钟级等待不再像「没反应」（防重复提交）。 */
      startLoadTimer() {
        var self = this;
        this.loadElapsed = 0;
        this.loadTimer = root.setInterval(
          function () { self.loadElapsed = self.loadElapsed + 1; }, 1000);
      },

      /* ── 贴 DDL 覆盖释义（收权 2026-10-14；端点复用 B1 parse/save 与错误信封） ──
       * fetch 直调（与 doLoad 同风格）：统一带管理令牌头＋JSON 解析与错误规整。 */
      async schemaImpCall(url, body) {
        var headers = Object.assign({ 'Content-Type': 'application/json' },
               (root.AdminGate && root.AdminGate.adminHeaders()) || {});
        var r = await root.fetch(url, { method: 'POST', headers: headers,
                                        body: JSON.stringify(body), cache: 'no-store' });
        var j = {};
        try { j = await r.json(); } catch (e) { j = {}; }
        if (!r.ok) throw new Error(j.message || ('HTTP ' + r.status));
        return j;
      },
      /* 解析预览（AC-2）：表＝当前选中表；错误信封人话透出（复用 B1 parse 端点） */
      async ddlParse() {
        var s = this.ddlImp;
        if (!this.picked || s.busy || !s.ddl.trim()) return;
        s.busy = true; s.note = ''; s.rows = []; s.added = []; s.diff = '';
        try {
          var j = await this.schemaImpCall(API.schemaParse,
                                           { table: this.picked.table, ddl: s.ddl });
          s.rows = j.rows || [];
          var d = j.diff || {};
          s.added = d.added || [];
          s.diff = '共 ' + (d.total || 0) + ' 字段：新增 ' + ((d.added || []).length) +
                   '、保留既有 ' + ((d.existingKept || []).length) +
                   (d.registered ? '（既有业务口径/敏感级保留）' : '（该表首次登记）');
        } catch (e) {
          s.note = '解析失败：' + (e.message || e);
        } finally { s.busy = false; }
      },
      /* 确认覆盖（AC-2/AC-5）：save 写回沿用库（装载富化真源）＋当次清单整表替换为
       * DDL 行——按名替换不静默沿用错位；新增/无 COMMENT 字段标未释义引导补齐。 */
      async ddlConfirm() {
        var s = this.ddlImp;
        if (!this.picked || !s.rows.length || s.busy) return;
        s.busy = true; s.note = '';
        try {
          var j = await this.schemaImpCall(API.schemaSave,
                                           { table: this.picked.table, rows: s.rows });
          var added = s.added;
          this.fieldState = 'ddl';
          this.fieldVersion = '';
          this.fieldRows = s.rows.map(function (r) {
            return { name: r['字段名'], type: r['数据类型'] || '',
                     cnName: r['cn_name'] || '', desc: r['业务口径'] || '',
                     added: added.indexOf(r['字段名']) >= 0 };
          });
          s.note = (j.message || '已写回') + '；当次清单已按 DDL 覆盖（'
                 + this.fieldRows.length + ' 字段）——装载时随版本固化。';
          s.rows = []; s.added = []; s.diff = '';
        } catch (e) {
          s.note = '写回失败：' + (e.message || e);
        } finally { s.busy = false; }
      },

      stopLoadTimer() {
        if (this.loadTimer) {
          root.clearInterval(this.loadTimer);
          this.loadTimer = null;
        }
      },

      async doLoad(replace) {
        if (!this.picked || this.loading) return;
        /* 覆盖重灌期间 409 面板保持在场（按钮转禁用＋计时器反馈，重灌入口不消失——
         * 修复「点了覆盖重灌面板瞬失、长时间零反馈」的卡死观感）；普通提交才清面板。 */
        if (!replace) this.conflict = '';
        this.loadError = '';
        this.loading = true;
        this.startLoadTimer();
        var payload = {
          source: 'hbase',
          table: this.picked.full,
          cf: this.cf || 'cf',
          version: versionOf(this.version)   /* 显式日期：补历史（NC-7）；不带列清单（AC-8） */
        };
        var url = API.load;
        if (replace) {
          payload.replace = true;            /* 清空重灌（后端幂等闸） */
          url = API.replaceLoad;
        }
        this.loadProgress = 0;
        try {
          /* B2 异步主路径：202+taskId → 轮询进度（覆盖重灌仍走同步端点，逻辑不变） */
          if (!replace) {
            var sub = await root.fetch(url, {
              method: 'POST',
              headers: Object.assign({ 'Content-Type': 'application/json' },
                                     (root.AdminGate && root.AdminGate.adminHeaders()) || {}),
              body: JSON.stringify(payload)
            });
            var subBody = await readJson(sub);
            if (!sub.ok || !subBody || !subBody.taskId) {
              this.loadError = (subBody && subBody.message) || ('提交失败 HTTP ' + sub.status);
              return;
            }
            var poll = await this.pollLoad(subBody.taskId);
            if (poll === 'conflict') return;           /* 冲突面板已由轮询层设置 */
            if (poll === 'error') return;              /* 错误信息已由轮询层设置 */
            this.result = poll;                         /* done：完整结果体 */
            this.step = 4;
            return;
          }
          var r = await root.fetch(url, {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' },
                                   (root.AdminGate && root.AdminGate.adminHeaders()) || {}),
            body: JSON.stringify(payload)
          });
          var body = await readJson(r);      /* 安全解析：非 JSON → null（不抛 SyntaxError） */
          if (body === null) {
            /* ① 非 JSON（网关 504 HTML / 解析失败）：服务端仍在后台继续装载——
             *    人话指引（勿重复提交＋稍后刷新查结果），不再裸报 SyntaxError。 */
            this.loadError = TIMEOUT_TEXT;
            return;
          }
          if (!r.ok) {
            if (r.status === 409 || body.error === 'LOAD_CONFLICT') {
              /* ② 409 人话：可能此前已成功（网关超时后服务端跑完，重试即此态） */
              this.conflict = conflictText(body);
            } else {
              /* ③ 其余非 2xx：沿用错误信封人话展示 */
              this.loadError = errorText(body, '装载失败。');
            }
            return;
          }
          if (!body.schema) {
            /* 覆盖重灌走发布装载端点：响应无 schema 段——从 schema 元数据端点补齐（同源） */
            body.schema = await this.schemaAfterLoad(body.table, body.version);
          }
          this.result = body;
          this.step = 4;
        } catch (e) {
          this.loadError = '装载请求失败：' + e;
        } finally {
          this.stopLoadTimer();              /* 响应到达（任何形态）即停表 */
          this.loading = false;
        }
      },

      /* B2 轮询：每 2s 查任务态。done→返回完整结果体；failed→设 conflict/loadError 返回标记。 */
      async pollLoad(taskId) {
        for (var i = 0; i < 1500; i++) {                /* 上限 50 分钟（防真死循环） */
          var j = null;
          try {
            var r = await root.fetch('/v1/admin/load/tasks/' + taskId, {
              headers: Object.assign({ 'Accept': 'application/json' },
                                     (root.AdminGate && root.AdminGate.adminHeaders()) || {}),
              cache: 'no-store'
            });
            j = await readJson(r);
          } catch (e) { /* 单次轮询网络抖动：稍后重试 */ }
          if (j && j.status === 'running') {
            this.loadProgress = j.progress || 0;
            await new Promise(function (res) { root.setTimeout(res, 2000); });
            continue;
          }
          if (j && j.status === 'done') return j;
          if (j && j.status === 'failed') {
            if (j.error === 'LOAD_CONFLICT') {
              this.conflict = conflictText(j);
              return 'conflict';
            }
            this.loadError = errorText(j, '装载失败。');
            return 'error';
          }
          /* 404（服务重启任务态丢失）或异常体：如实报 */
          this.loadError = (j && j.message) || '任务状态不可读（服务可能已重启）';
          return 'error';
        }
        this.loadError = '装载超过 50 分钟未完成，轮询停止（任务仍在后台，稍后可用版本管理页核实）';
        return 'error';
      },

      async schemaAfterLoad(table, version) {
        try {
          var r = await root.fetch(
            API.schema + '?table=' + encodeURIComponent(table) + '&version=' + encodeURIComponent(version),
            { headers: Object.assign({ 'Accept': 'application/json' }, (root.AdminGate && root.AdminGate.adminHeaders()) || {}) });
          var body = await r.json();
          if (r.ok) return { fieldCount: body.fieldCount, source: body.source };
        } catch (e) {
          /* 读不到＝如实降级展示「—」，不谎报字段数 */
        }
        return { fieldCount: null, source: '未生成' };
      },

      reset() {
        this.stopLoadTimer();
        this.step = 2; this.picked = null; this.result = null;
        this.conflict = ''; this.loadError = '';
        this.fieldState = 'idle'; this.fieldVersion = ''; this.fieldRows = [];
        this.ddlImp = { ddl: '', rows: [], added: [], diff: '', note: '', busy: false };
        this.version = todayStr();
        this.discover();
      }
    };
  };

  document.addEventListener('alpine:init', function () {
    if (root.Alpine) root.Alpine.data('loadWizard', root.loadWizard);
  });
})(window);
