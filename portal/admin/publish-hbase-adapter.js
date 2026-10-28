/* ============================================================
 * OneServe 运营版 O-1 · HBase 数据源适配层（publish-hbase-adapter.js）
 * 卡：TASK-M2-5（AC-4 发现 / AC-5 选表→配字段→真试查门禁）。
 *
 * 【为什么要这一层】M2-4（发布台后端 API）与本卡并行开发，端点契约未定稿。
 *   本文件是**唯一**解析后端响应的地方：端点常量、请求体构造、响应归一化
 *   全在这里；publish.js 只消费归一化后的形状。M2-4 定稿后只改本文件。
 *
 * 【假设的 M2-4 契约（AC-4/AC-5，未定稿·draft v0）】
 *   GET  /v1/admin/publish/hbase/discovery
 *        → 200 {checkedAt, dualSource:{status:'ok'|'mismatch',message,mismatches[]},
 *               domains:[{id,name,desc,tables:[{namespace,table,keyTypes[],
 *                 keySamples:{keyType:key}, families:[{name,columns:[{name,cnName,
 *                 type,sample}]}]}]}]}
 *        容错：也接受 namespaces[] 或扁平 tables[]（按 businessDomain/domain/namespace 分组）；
 *              family 接受 families/column_families/cfs 键；column 接受 name/qualifier/column。
 *        → 非 2xx {error,message}：503 HBase 不可达 / 409 双源不一致拦截 / 401 令牌错
 *   POST /v1/admin/publish/hbase/test
 *        ← {namespace,table,families[],fields[{name,sample,desc}],keyType,sampleKey}
 *        → 200 {passed,code,elapsedMs,body} | 非 2xx {error,message}
 *   GET  /v1/admin/publish/hbase/selftest      （AC-5 headless 机检端点）
 *        → 200 {passed,total,assertions[{id,name,ok,detail}]}
 *   POST /v1/admin/publish/hbase/load          （本卡：HBase 冷存储 → Redis 热存储加载）
 *        ← {namespace,table,families[],columns[],keys[],version,replace}
 *        → 200 {ok:true,version,table,cf,loaded,tagCols,elapsedMs,
 *               reconcile:{pass,diffCount,checks,diffs,error?}}
 *        → 非 2xx {ok:false,error,message}：409 目标空间非空拒灌 / 503 HBase|Redis 不可达
 *   POST /v1/admin/publish                     （沿用；发布产物已改为「加载后 Redis 型」）
 *        ← {serviceName,routePath,fields,releaseMode,status,operator,keyType,
 *           loadVersion,backend:{source:'hbase',hbase:{namespace,table,cf,key_type}}}
 *           ※ backend.hbase 只作「加载来源」入参（选表路径），产物条落 backend.kv；
 *             loadVersion＝加载动作落定的 Redis 版本空间（来源留痕）。
 *
 * 【诚信线】HBase 不可达/端点未就绪一律**如实报错**（normalizeError.fallback
 *   恒为 false）——绝不回落 mock 假表冒充真 HBase；是否降级由调用方决定，
 *   本层只给事实。纯函数无 DOM 依赖：浏览器与 node（headless 机检）同源。
 * ============================================================ */
(function (root) {
  'use strict';

  var CONTRACT_VERSION = 'hbase-discovery/assumed-v0';

  /* ── 端点与契约常量（唯一出处；M2-4 定稿后只改这里） ── */
  var CONTRACT = {
    /* 卡3（流程②）：表源/字段源换卡2 装载域——assets＝已装载资产（①步表源＋②步版本源），
     * schema＝表级 schema 元数据（④步字段源）。B3.1 冻结契约，字段名不得自定义第二套。 */
    loadAssets: '/v1/admin/load/assets',
    loadSchema: '/v1/admin/load/schema',
    /* 旧四步向导契约（discovery/test/load）：NC-5 裁定端点保留（运维/testkit/兼容），
     * 但发布页链路不再引用（卡3 迁移清单）；统一清理归卡6。 */
    discovery: '/v1/admin/publish/hbase/discovery',
    test: '/v1/admin/publish/hbase/test',
    selftest: '/v1/admin/publish/hbase/selftest',
    load: '/v1/admin/publish/hbase/load',
    publish: '/v1/admin/publish',
    /* 向导③④：服务列表（写区条目）/切版状态/切换/回滚（发布台重设计 B4） */
    consoleView: '/v1/admin/publish',
    syncStatus: '/v1/admin/publish/sync/status',
    syncSwitch: '/v1/admin/publish/sync/switch',
    syncRollback: '/v1/admin/publish/sync/rollback',
    offline: function (id) { return '/v1/admin/publish/' + encodeURIComponent(id) + '/offline'; },
    keyTypes: ['usr_id', 'phone_sm3'],
    keyTypeLabels: { 'usr_id': 'usr_id', 'phone_sm3': 'phone_sm3' },
    /* 错误标题＝页面直出人话（出了什么事）；处置建议由 hint/调用方补（你该干什么） */
    errorTitles: {
      UNAUTHORIZED: '没有操作权限',
      NOT_FOUND: '服务端暂不可用',
      HBASE_UNREACHABLE: '暂时连不上数据源',
      /* 卡1 诊断三分类对应的后端机读码（HBASE_AUTH_FAILED＝认证失败；不可达统一码） */
      HBASE_UNAVAILABLE: '暂时连不上数据源（含未启动）',
      HBASE_AUTH_FAILED: '数据源认证失败',
      HBASE_UPSTREAM_ERROR: '数据源读取失败',
      HBASE_SCHEMA_MISMATCH: '数据表结构发生了变化',
      LOAD_CONFLICT: '该日期的数据已经加载过',
      REDIS_UNAVAILABLE: '查询存储暂时不可用',
      LOAD_TIMEOUT: '加载超时',
      LOAD_UPSTREAM_ERROR: '加载失败',
      invalid_json: '请求内容有误'
    }
  };

  /* ── 小工具（无依赖） ── */
  function isStr(x) { return typeof x === 'string' && x.trim() !== ''; }
  function arr(x) { return Array.isArray(x) ? x : []; }
  function obj(x) { return (x && typeof x === 'object' && !Array.isArray(x)) ? x : {}; }
  function firstStr() {
    for (var i = 0; i < arguments.length; i++) {
      if (isStr(arguments[i])) return String(arguments[i]).trim();
    }
    return '';
  }
  function hasOwn(o, k) { return Object.prototype.hasOwnProperty.call(o || {}, k); }

  /* 兼容 {data:{...}} / {result:{...}} 包装（后端可能统一包一层） */
  function unwrap(payload) {
    var p = obj(payload);
    var inner = obj(p.data);
    if (!Object.keys(inner).length) inner = obj(p.result);
    if (!Object.keys(inner).length) return p;
    /* 只有内层真的带树/错误信息才下钻，避免把 data 里的业务行误当整包 */
    if (hasOwn(inner, 'domains') || hasOwn(inner, 'namespaces') || hasOwn(inner, 'tables') ||
        hasOwn(inner, 'dualSource') || hasOwn(inner, 'dual_source') || hasOwn(inner, 'error') ||
        hasOwn(inner, 'ok')) {
      var merged = {};
      Object.keys(p).forEach(function (k) { merged[k] = p[k]; });
      Object.keys(inner).forEach(function (k) { merged[k] = inner[k]; });
      return merged;
    }
    return p;
  }

  /* ── 列/列族归一 ── */
  function normalizeColumn(c) {
    if (typeof c === 'string') return { name: c.trim(), cnName: '', type: '', sample: '' };
    var o = obj(c);
    return {
      name: firstStr(o.name, o.qualifier, o.column, o.col, o.field, o['字段名']),
      cnName: firstStr(o.cnName, o.cn_name, o.chineseName, o.zhName, o['中文名']),
      type: firstStr(o.type, o.dataType, o.data_type, o['数据类型']),
      sample: firstStr(o.sample, o.example, o.sampleValue, o['示例值'])
    };
  }

  function normalizeFamily(f) {
    if (typeof f === 'string') return { name: f.trim(), columns: [] };
    var o = obj(f);
    return {
      name: firstStr(o.name, o.family, o.cf, o.columnFamily, o.column_family, o['列族']),
      columns: arr(o.columns || o.cols || o.qualifiers || o.fields).map(normalizeColumn)
    };
  }

  function normalizeKeyTypes(t) {
    var o = obj(t);
    var raw = o.keyTypes || o.key_types || o.keyType || o.key_type || [];
    var list = (Array.isArray(raw) ? raw : [raw])
      .map(function (k) { return String(k == null ? '' : k).trim(); })
      .filter(Boolean);
    return list.length ? list : CONTRACT.keyTypes.slice();
  }

  function normalizeKeySamples(t) {
    var o = obj(t);
    var out = {};
    var m = obj(o.keySamples || o.key_samples || o.sampleKeys || o.sample_keys);
    Object.keys(m).forEach(function (k) { if (isStr(m[k])) out[k] = String(m[k]).trim(); });
    var single = firstStr(o.sampleKey, o.sample_key, o.keySample, o.key_sample, o.demoKey);
    if (single) normalizeKeyTypes(o).forEach(function (k) { if (!out[k]) out[k] = single; });
    return out;
  }

  /* 列族内的重名限定：同一 qualifier 出现在多列族时以 cf:qualifier 作展示名，
   * 保证 DOM id/表单键不撞车（单列族起步场景下 name 即裸 qualifier）。 */
  function applyCollisionQualify(families) {
    var seen = {};
    families.forEach(function (f) {
      f.columns.forEach(function (c) {
        if (!c.name) return;
        seen[c.name] = (seen[c.name] || 0) + 1;
      });
    });
    families.forEach(function (f) {
      f.columns.forEach(function (c) {
        if (c.name && seen[c.name] > 1) c.name = f.name + ':' + c.name;
        c.cf = f.name;
        c.label = f.name ? (f.name + ':' + c.name) : c.name;
      });
    });
    return families;
  }

  function normalizeTable(t, fallbackDomain) {
    var o = obj(t);
    var namespace = firstStr(o.namespace, o.ns, o.nameSpace) || 'default';
    var table = firstStr(o.table, o.tableName, o.table_name, o.name, o.id);
    if (table.indexOf(':') >= 0) {                 /* ns:table 形式：拆开并回填 */
      var parts = table.split(':');
      namespace = parts[0] || namespace;
      table = parts.slice(1).join(':');
    }
    var families = applyCollisionQualify(
      arr(o.families || o.columnFamilies || o.column_families || o.cfs || o['列族']).map(normalizeFamily));
    var columnCount = families.reduce(function (n, f) { return n + f.columns.length; }, 0);
    var t2 = {
      id: (namespace + ':' + table),
      namespace: namespace,
      table: table,
      path: namespace + ':' + table,
      name: firstStr(o.displayName, o.label, o.title) || table,
      /* 表级中文名/一句话描述（发布台重设计 B5：卡片标题与服务名自动建议） */
      cnName: firstStr(o.tableCnName, o.table_cn_name, o.tableCn, o.cnName, o.displayName, o.label, o.title),
      desc: firstStr(o.tableDesc, o.table_desc, o.description, o.desc, o.note),
      businessDomain: firstStr(o.businessDomain, o.business_domain, o.domain, o.domainId) || fallbackDomain || '',
      families: families,
      cfCount: families.length,
      columnCount: columnCount,
      keyTypes: normalizeKeyTypes(o),
      keySamples: normalizeKeySamples(o),
      note: firstStr(o.note, o.description, o.desc)
    };
    return t2;
  }

  function normalizeDomain(id, name, desc, rawTables) {
    var tables = arr(rawTables).map(function (t) { return normalizeTable(t, id); });
    return { id: id, name: name, desc: desc || '', tables: tables };
  }

  /* ── 发现响应 → 归一化目录树（业务域 → namespace/表 → 列族 → 列） ── */
  function normalizeDiscovery(payload) {
    var p = unwrap(payload);

    /* 后端 200 里带错误体（部分网关口径）：仍按错误如实返回 */
    var e = p.error;
    if (isStr(e) && !arr(p.domains).length && !arr(p.tables).length && !arr(p.namespaces).length) {
      return { ok: false, source: 'hbase', checkedAt: '', dualSource: emptyDual(), domains: [], error: normalizeError({ code: e, message: p.message }) };
    }

    var dualRaw = obj(p.dualSource || p.dual_source);
    if (!Object.keys(dualRaw).length) dualRaw = obj(p.schemaCheck || p.schema_check);
    var dual = {
      status: firstStr(dualRaw.status, dualRaw.state) || 'unknown',
      message: firstStr(dualRaw.message, dualRaw.detail),
      mismatches: arr(dualRaw.mismatches || dualRaw.diffs || dualRaw.inconsistent).map(function (m) {
        if (isStr(m)) return m;
        var om = obj(m);
        var f = firstStr(om.field, om.column, om.name, om.qualifier) || '(未指明)';
        var why = firstStr(om.reason, om.message, om.detail);
        return why ? (f + '：' + why) : f;
      })
    };
    if (dual.status === 'unknown' && dual.mismatches.length) dual.status = 'mismatch';

    var domains = [];
    var dList = arr(p.domains);
    if (dList.length) {
      dList.forEach(function (d, i) {
        var od = obj(d);
        var id = firstStr(od.id, od.key, od.name) || ('domain-' + i);
        domains.push(normalizeDomain(id, firstStr(od.name, od.label) || id,
          firstStr(od.desc, od.description), od.tables));
      });
    } else {
      var nsList = arr(p.namespaces);
      if (nsList.length) {
        nsList.forEach(function (n) {
          var on = obj(n);
          var ns = firstStr(on.name, on.namespace, on.id) || 'default';
          var ts = arr(on.tables).map(function (t) {
            var o = Object.assign({}, obj(t));
            if (!firstStr(o.namespace, o.ns, o.nameSpace)) o.namespace = ns;
            return o;
          });
          domains.push(normalizeDomain(ns, ns + ' namespace', 'HBase namespace ' + ns, ts));
        });
      } else {
        /* 扁平 tables：按 businessDomain/domain/namespace 分组（业务域视角兜底） */
        var flats = arr(p.tables);
        if (!flats.length && p.hbase) flats = arr(obj(p.hbase).tables);
        var byKey = {}, order = [];
        flats.forEach(function (t) {
          var o = obj(t);
          var ns = firstStr(o.namespace, o.ns) || 'default';
          var key = firstStr(o.businessDomain, o.business_domain, o.domain, o.domainId) || ns;
          if (!byKey[key]) { byKey[key] = []; order.push(key); }
          byKey[key].push(o);
        });
        order.forEach(function (key) {
          domains.push(normalizeDomain(key, key, '业务域「' + key + '」', byKey[key]));
        });
      }
    }
    return {
      ok: true, source: 'hbase', checkedAt: firstStr(p.checkedAt, p.checked_at, p.updatedAt),
      dualSource: dual, domains: domains, error: null
    };
  }

  function emptyDual() { return { status: 'unknown', message: '', mismatches: [] }; }
  function emptyDiscovery() {
    return { ok: true, source: 'hbase', checkedAt: '', dualSource: emptyDual(), domains: [], error: null };
  }

  /* ── 错误归一（fallback 恒 false：不回落 mock 假表） ── */
  function normalizeError(err) {
    var o = obj(err);
    var status = Number(o.status) || 0;
    var code = firstStr(o.code, o.error, o.error_code);
    if (!code) code = status === 401 ? 'UNAUTHORIZED' : (status === 404 ? 'NOT_FOUND'
      : (status >= 500 ? 'HBASE_UNREACHABLE' : (status ? 'HTTP_' + status : 'NETWORK')));
    /* 机读码大小写归一：后端可能回 lowercase（unauthorized），标题表按大写键查 */
    var titleKey = String(code).toUpperCase();
    var title = CONTRACT.errorTitles[titleKey] || ('请求失败（' + code + '）');
    var message = firstStr(o.message, o.msg) || (status >= 500 ? '服务端处理出现问题'
      : (status ? '请求没有成功' : '网络连不上服务端'));
    var hint = '';
    if (code === 'HBASE_UNREACHABLE' || code === 'NETWORK' || code === 'NOT_FOUND') {
      hint = '请稍后重试；若持续出现，请联系平台管理员。';
    } else if (code === 'UNAUTHORIZED') {
      hint = '请确认你有本页面的操作权限，或联系平台管理员。';
    } else if (code === 'HBASE_SCHEMA_MISMATCH') {
      hint = '表的字段与登记信息不一致，请联系平台管理员处理后再试。';
    }
    return { code: code, status: status, title: title, message: message, hint: hint, fallback: false };
  }

  /* ── 请求体构造 ── */
  function primaryCf(table) {
    var f = arr(obj(table).families)[0];
    return f ? f.name : '';
  }

  function buildTestPayload(form) {
    var t = obj(form.table);
    return {
      namespace: firstStr(t.namespace) || 'default',
      table: firstStr(t.table, t.name),
      families: arr(t.families).map(function (f) { return f.name; }),
      fields: arr(form.fields).map(function (f) {
        return { name: f.name, cf: f.cf || '', sample: f.sample || '', desc: f.desc || '' };
      }),
      keyType: firstStr(form.keyType) || arr(t.keyTypes)[0] || CONTRACT.keyTypes[0],
      sampleKey: firstStr(form.sampleKey),
      timeoutMs: Number(form.timeoutMs) || 3000
    };
  }

  /* ── 加载请求体构造（本卡：选表 → 加载到 Redis） ──
   * 默认整列族加载（不带 columns）：与既有加载器 --cf 语义一致，也是既有上线闸
   * （sync/reconcile.py 源对账＋switch.py 三闸，本卡未改）可判的口径。
   * columns 仅在调用方显式给出时透传（最小加载面＝API 能力，供后续窄加载/增量用）；
   * version 缺省留空 → 后端按当日生成（YYYYMMDD，与 sync/loader.py 同口径）。 */
  function buildLoadPayload(form) {
    var t = obj(form.table);
    var columns = arr(form.columns).map(function (c) { return firstStr(c); }).filter(Boolean);
    var out = {
      namespace: firstStr(t.namespace) || 'default',
      table: firstStr(t.table, t.name),
      cf: firstStr(form.cf) || primaryCf(t),
      version: firstStr(form.version),
      replace: !!form.replace
    };
    if (columns.length) out.columns = columns;
    return out;
  }

  function buildPublishPayload(form) {
    /* 卡3（B3.2 冻结契约）：发布请求＝扁平式五件套——serviceName/routePath/table(裸表名)/
     * source:'hbase'/fields[{name,cf,type,cnName}]（投影白名单对象，⊆ 表 schema）/
     * keyType/loadVersion(tabver 留痕)。不再发 backend 嵌套与 sample/desc（装载语义
     * 已归流程①；示例值由后端采样自动填充）。 */
    var t = obj(form.table);
    var keyType = firstStr(form.keyType) || arr(t.keyTypes)[0] || CONTRACT.keyTypes[0];
    return {
      serviceName: firstStr(form.serviceName),
      routePath: firstStr(form.routePath),
      method: 'POST',
      key: 'usr_id',
      table: firstStr(t.table, t.name),
      source: 'hbase',
      fields: arr(form.fields).map(function (f) {
        return { name: f.name, cf: f.cf || '', type: f.type || 'unknown',
                 cnName: f.cnName || '待补' };
      }),
      status: firstStr(form.status) || 'published',
      operator: firstStr(form.operator) || 'console',
      keyType: keyType,
      cf: firstStr(form.cf) || primaryCf(t),
      loadVersion: firstStr(form.loadVersion)
    };
  }

  /* ── 卡3：已装载资产归一（GET /v1/admin/load/assets → ①步表源＋②步版本源） ──
   * B3.1 冻结契约：{ok, tables:[{table, domain, tableCnName, versions:[
   * {version, rows, tagCols, dataAsOf, loadedAt, reconPass, schemaFieldCount, live}]}]}。
   * 只列已装载表（服务端语义）；本层只做形状归一与容错，不发明任何字段。 */
  function normalizeAssets(payload) {
    var o = unwrap(payload);
    var tables = arr(o.tables).map(function (t) {
      var ot = obj(t);
      return {
        table: firstStr(ot.table, ot.domain),
        domain: firstStr(ot.domain, ot.table),
        tableCnName: firstStr(ot.tableCnName, ot.cn_name),
        tableDesc: firstStr(ot.tableDesc, ot.desc),
        versions: arr(ot.versions).map(function (v) {
          var ov = obj(v);
          return {
            version: firstStr(ov.version),
            rows: Number(ov.rows || 0),
            tagCols: Number(ov.tagCols || ov.tag_cols || 0),
            dataAsOf: firstStr(ov.dataAsOf, ov.data_as_of),
            loadedAt: firstStr(ov.loadedAt, ov.loaded_at),
            reconPass: ov.reconPass === true || ov.recon_pass === '1' || ov.reconPass === '1',
            schemaFieldCount: Number(ov.schemaFieldCount || ov.schema_field_count || 0),
            live: ov.live === true
          };
        })
      };
    });
    return { ok: o.ok === true && tables.length >= 0, tables: tables };
  }

  /* ── 卡3：表级 schema 归一（GET /v1/admin/load/schema → ④步字段源） ──
   * B3.1 冻结契约：fields[{name, cf, type, cnName, desc}]；type/cnName 缺＝
   * unknown/待补（卡2 NC-1 如实口径）——本层原样透出，不猜（卡3 NC-7）。 */
  function normalizeSchema(payload) {
    var o = unwrap(payload);
    return {
      ok: o.ok === true,
      table: firstStr(o.table),
      version: firstStr(o.version),
      fieldCount: Number(o.fieldCount || o.field_count || 0),
      source: firstStr(o.source),
      generatedAt: firstStr(o.generatedAt, o.generated_at),
      fields: arr(o.fields).map(function (f) {
        var of = obj(f);
        return {
          name: firstStr(of.name),
          cf: firstStr(of.cf),
          type: firstStr(of.type) || 'unknown',
          cnName: firstStr(of.cnName, of.cn_name) || '待补',
          desc: firstStr(of.desc)
        };
      }).filter(function (f) { return !!f.name; })
    };
  }

  /* P3 修复（2026-10-08）：入参是字符串表名（pickedTable），此前误经 syncTable 的
   * 表单包装——syncTable 期望 {table:{table:…}} 嵌套对象，字符串被 obj() 洗成 {}，
   * 表名悄悄变空 → ?table=&version=… → 后端按缺参回 400「缺少 table/version 查询
   * 参数」被裸报给用户（数据装载其实一切正常）。字符串直接经 bareName 剥 ns 前缀，
   * 与后端 key_domain 同口径。 */
  function loadSchemaUrl(table, version) {
    return CONTRACT.loadSchema + '?table=' + encodeURIComponent(bareName(table))
      + '&version=' + encodeURIComponent(String(version || ''));
  }

  /* ── 试查响应归一（后端可回 {passed} 或裸 {code}） ── */
  function normalizeTestResult(resp) {
    var o = unwrap(resp);
    var code = Number(o.code || o.status_code || o.status || 0);
    var passed;
    if (typeof o.passed === 'boolean') passed = o.passed;
    else if (typeof o.ok === 'boolean') passed = o.ok;
    else passed = code >= 200 && code < 300;
    return {
      passed: !!passed,
      code: code || (passed ? 200 : 0),
      elapsedMs: Number(o.elapsedMs || o.elapsed_ms || o.elapsed || 0),
      body: (o.body !== undefined ? o.body : (o.data !== undefined ? o.data : o)),
      message: firstStr(o.message, o.msg) || (passed ? 'ok' : '试查未通过'),
      keyType: firstStr(o.keyType, o.key_type)
    };
  }

  /* ── 加载响应归一（本卡：HBase → Redis 加载＋同源对账） ──
   * 真实报账：ok/loaded/elapsedMs/对账差异（pass/diffCount/checks）原样归一；
   * 失败（非 2xx，经 apiFetch 抛错）由调用方走 normalizeError 如实展示，不静默成功。
   * 对账差异＝0 才算绿；有差异时 differences 列出可解释项（不吞不美化）。 */
  function normalizeLoadResult(resp) {
    var o = unwrap(resp);
    var rec = obj(o.reconcile);
    var checks = obj(rec.checks);
    var diffs = arr(rec.diffs).map(function (d) {
      if (typeof d === 'string') return d;
      var od = obj(d);
      return firstStr(od.kind) + (firstStr(od.key) ? (' ' + od.key) : '') +
        (firstStr(od.detail) ? ('：' + JSON.stringify(od.detail)) : '');
    });
    /* 对账绿：后端明示 pass=true 且差异计数为 0；缺对账结果＝未确认绿（不谎报通过） */
    var reconOk = (rec.pass === true) && Number(rec.diffCount || 0) === 0;
    return {
      ok: o.ok !== false,
      mode: firstStr(o.mode),
      version: firstStr(o.version),
      table: firstStr(o.table),
      cf: firstStr(o.cf),
      loaded: Number(o.loaded || 0),
      tagCols: Number(o.tagCols || 0),
      nonemptyCells: Number(o.nonemptyCells || 0),
      clearedBeforeLoad: Number(o.clearedBeforeLoad || 0),
      dataAsOf: firstStr(o.dataAsOf),
      elapsedMs: Number(o.elapsedMs || 0),
      sourceSha256: firstStr(o.sourceSha256),
      reconcile: {
        pass: rec.pass === true, diffCount: Number(rec.diffCount || 0),
        checks: checks, diffs: diffs, error: firstStr(rec.error),
        ok: reconOk
      }
    };
  }

  /* ── 后端自检响应归一（AC-5：机检以后端 selftest API 断言为准） ── */
  function normalizeSelftest(resp) {
    var o = unwrap(resp);
    var list = arr(o.assertions || o.checks || o.tests).map(function (a, i) {
      var oa = obj(a);
      return {
        id: firstStr(oa.id, oa.key) || ('assertion-' + i),
        name: firstStr(oa.name, oa.title, oa.desc),
        ok: !!(oa.ok === true || oa.passed === true || oa.status === 'pass'),
        detail: firstStr(oa.detail, oa.message, oa.msg)
      };
    });
    var passed = list.filter(function (x) { return x.ok; }).length;
    var total = Number(o.total) || list.length;
    return {
      ok: (typeof o.passed === 'boolean') ? o.passed : (typeof o.ok === 'boolean' ? o.ok : (total > 0 && passed === total)),
      total: total, passed: passed, failed: total - passed, assertions: list,
      checkedAt: firstStr(o.checkedAt, o.checked_at)
    };
  }

  /* ── 目录树查询工具 ── */
  function allTables(domains) {
    var out = [];
    arr(domains).forEach(function (d) { arr(obj(d).tables).forEach(function (t) { out.push(t); }); });
    return out;
  }
  function findTable(domains, id) {
    var list = allTables(domains);
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }
  function keyTypesOf(domains) {
    var seen = {};
    allTables(domains).forEach(function (t) { arr(t.keyTypes).forEach(function (k) { seen[k] = 1; }); });
    var list = Object.keys(seen);
    return list.length ? list : CONTRACT.keyTypes.slice();
  }
  function sampleKeyFor(table, keyType) {
    var t = obj(table);
    var map = obj(t.keySamples);
    return firstStr(map[keyType], map[arr(t.keyTypes)[0]], t.sampleKey, t.sample_key);
  }
  function flattenFields(table) {
    var out = [];
    arr(obj(table).families).forEach(function (f) {
      arr(f.columns).forEach(function (c) { out.push(c); });
    });
    return out;
  }
  function shortKey(k) {
    k = String(k || '');
    return k.length > 20 ? k.slice(0, 10) + '\u2026' + k.slice(-6) : k;
  }
  function columnLabel(cf, name) { return cf ? (cf + ':' + name) : name; }

  /* ── 切版状态归一（向导③：当前线上版本回显） ──
   * 响应 {ok, liveVersion, versionMeta{rows,data_as_of,…}, recentSwitches[], note}；
   * liveVersion=null＝未上线（「当前线上版本：无」）。只取展示所需字段，note 不上界面。 */
  function normalizeSyncStatus(resp) {
    var o = unwrap(resp);
    var meta = obj(o.versionMeta);
    return {
      ok: o.ok !== false,
      table: firstStr(o.table),
      liveVersion: firstStr(o.liveVersion, o.live_version),
      rows: Number(meta.rows || meta.row_count || 0),
      dataAsOf: firstStr(meta.dataAsOf, meta.data_as_of)
    };
  }

  /* ── 裸表名归一（ns:table 剥前缀；空/非串如实回空串）——切版/schema 查询共用 ── */
  function bareName(name) {
    var s = firstStr(name);
    return s.indexOf(':') >= 0 ? s.split(':').slice(1).join(':') : s;
  }

  /* ── 切版键空间表参数（按表隔离）：向导③ status/switch/rollback 带所选表名 ──
   * 裸表名口径与 buildLoadPayload 一致（t.table；ns:table 形式剥前缀）——
   * 每表独立 latest 指针，A 表切版/回滚不动 B 表。 */
  function syncTable(form) {
    var t = obj(form && form.table);
    return bareName(firstStr(t.table, t.name));
  }

  function syncStatusUrl(form) {
    var tb = syncTable(form);
    return CONTRACT.syncStatus + (tb ? ('?table=' + encodeURIComponent(tb)) : '');
  }

  function buildSwitchPayload(form) {
    var f = form || {};
    return { version: firstStr(f.version), table: syncTable(f) };
  }

  function buildRollbackPayload(form) {
    return { table: syncTable(form) };
  }

  /* ── 服务列表归一（向导下方管理区：服务名/地址/版本/状态） ── */
  function normalizeServices(resp) {
    var o = unwrap(resp);
    return arr(o.services).map(function (s) {
      var os = obj(s);
      var loadSource = obj(os.loadSource);
      return {
        id: firstStr(os.id, os.serviceId, os.service_id),
        serviceName: firstStr(os.serviceName, os.name),
        routePath: firstStr(os.routePath, os.route),
        /* 卡3 AC-12：列表「版本」列改数据版本（tabver；来源＝条目 loadSource.version）。
         * 服务修订号（s.version）不再展示——列由卡4 彻底清，此处不渲染。 */
        version: firstStr(os.version),
        dataVersion: firstStr(loadSource.version),
        table: firstStr(loadSource.table),
        status: firstStr(os.status) || 'published',
        /* P3 二批（拆页）：已发布页详情展示用透传（缺省 ''，调用方按「—」处理） */
        publishedAt: firstStr(os.publishedAt, os.published_at),
        keyType: firstStr(os.keyType, os.key_type),
        fields: arr(os.fields).map(function (f) {
          return typeof f === 'string' ? f : firstStr(obj(f).name);
        }).filter(Boolean)
      };
    });
  }

  root.O1HBase = {
    CONTRACT_VERSION: CONTRACT_VERSION,
    CONTRACT: CONTRACT,
    normalizeDiscovery: normalizeDiscovery,
    emptyDiscovery: emptyDiscovery,
    emptyDual: emptyDual,
    normalizeError: normalizeError,
    normalizeTable: normalizeTable,
    normalizeColumn: normalizeColumn,
    normalizeFamily: normalizeFamily,
    normalizeTestResult: normalizeTestResult,
    normalizeSelftest: normalizeSelftest,
    normalizeLoadResult: normalizeLoadResult,
    normalizeSyncStatus: normalizeSyncStatus,
    normalizeServices: normalizeServices,
    buildTestPayload: buildTestPayload,
    buildLoadPayload: buildLoadPayload,
    buildPublishPayload: buildPublishPayload,
    syncTable: syncTable,
    syncStatusUrl: syncStatusUrl,
    buildSwitchPayload: buildSwitchPayload,
    buildRollbackPayload: buildRollbackPayload,
    normalizeAssets: normalizeAssets,
    normalizeSchema: normalizeSchema,
    loadSchemaUrl: loadSchemaUrl,
    allTables: allTables,
    findTable: findTable,
    keyTypesOf: keyTypesOf,
    sampleKeyFor: sampleKeyFor,
    flattenFields: flattenFields,
    primaryCf: primaryCf,
    shortKey: shortKey,
    columnLabel: columnLabel
  };
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
