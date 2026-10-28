/* ============================================================
 * OneServe 运营控制台 · AppKey 发放逻辑（appkeys.js，Alpine 组件）
 * 2026-10-08 重设计：签发表单极简化（授权服务单选／备注名／有效期）＋
 * 客户视角文案清洗＋四操作可见反馈修复。设计依据：
 * docs/plans/运营版设计稿-三界面_v0.md（AppKey 发放节）＋前台重造 SPEC 凭证发放节。
 *
 * 【签发走位】提交按钮 POST /v1/admin/appkeys：服务端登记（appkeys.yaml 权威源
 *   落盘＋热加载）并即时生效；后端不可达时降级 mock 列表并挂「演示数据」角标，
 *   此时签发仅内存演示（留痕条如实标注演示模式，不伪造真实发放）。
 *   写端点不需要任何管理令牌（管理令牌机制已于 2026-09-23 整体拆除；
 *   授权边界＝网络层可达）。
 *
 * 【按服务授权】GET /v1/admin/publish 取已发布服务（status=published），
 *   单选一个服务＋勾选字段（默认全选）→ grants=[{service, endpoint?, fields}]
 *   随表单提交（后端透传校验）；新发布服务无需矩阵登记即可获得授权。
 *   下拉选项＝「名字（路由尾段）」（AppKey 二批 §A.3：消灭纯同名歧义）。
 *
 * 【授权可见性】（AppKey 二批 §B）列表每行「授权服务」chips（名·路由尾段，
 *   超 3 枚收起「+N」，行内展开显完整 服务/路由/字段数）；无授权如实「未授权」。
 *   数据源＝GET /v1/admin/appkeys 每行随行 grants（admin_api._grants_view）。
 *
 * 【失败路径】（AppKey 二批 §C）签发/删除/续期/重置/禁用/配额的 catch 一律
 *   humanError() 人话进 inline err/确认框 hint（弹窗保持打开可重试），trace 同步留痕。
 *
 * 【表单极简化口径】（内部字段照传，后端契约不变）
 *   · 凭证类型固定 sandbox：正式凭证走线下 OA 审批，本页签发均为沙箱凭证；
 *   · 备注名（可选）→ caller_name/app_name；缺省自动生成「<租户>-<服务短名>」；
 *   · 有效期（30/90/365 天，默认 30）→ expires_at；
 *   · 配额不在表单暴露：后端缺省 quota_qps=50／quota_daily=10000
 *     （service/app/admin_api.py issue：int(payload.get('quota_qps') or 50) 同款缺省），
 *     管理员可随时经行内「调整配额」端点调整；
 *   · 业务域标注（tag_sets）不再采集，随表单恒传 []（存量凭证标注仅历史展示）。
 *
 * 【安全纪律】Secret 明文只存在于签发弹窗 DOM 与下载的 .txt（用户主动动作），
 *   绝不写入任何存储：内存列表、localStorage、导出 yaml、mock 文件全部只存
 *   sha256 摘要；完整 AppKey 列表不展示（掩码），仅签发弹窗出现一次。
 *
 * 【可测性】纯函数单元挂在 window.O3（供 selftest 页直接断言）；
 *   Alpine 组件 appkeyAdmin() 只做编排。
 * ============================================================ */
(function (root) {
  'use strict';

  var KEY_LIMIT = 2;                       // 同一使用方上限 2 把（正常＋重置过渡各一把）
  var TAGSETS = ['基本信息域', '行为域', '渠道域', '风险域', '营销域', '合作方域'];   // 存量凭证的业务域标注（新签发不再采集）
  var TRIAL_URL = '/trial.html'; // 调试台 URL（同源绝对路径，浏览器按当前站点 origin 解析，dev 与任意 IP 部署均正确）
  var VALIDITY_DAYS = [30, 90, 365, 'permanent'];   // 有效期下拉档位（默认 30；permanent=永久，2026-10-14 §B.1）
  var DEFAULT_QPS = 50, DEFAULT_DAILY = 10000;   // 与后端签发缺省同口径（演示模式造记录用）

  /* ── 后端端点（门户 nginx /v1/admin/ 反代到服务层） ── */
  var API = {
    list: '/v1/admin/appkeys',
    yaml: '/v1/admin/appkeys?format=yaml',
    issue: '/v1/admin/appkeys',
    status: function (kid) { return '/v1/admin/appkeys/' + encodeURIComponent(kid) + '/status'; },
    rotate: function (kid) { return '/v1/admin/appkeys/' + encodeURIComponent(kid) + '/rotate'; },
    quota: function (kid) { return '/v1/admin/appkeys/' + encodeURIComponent(kid) + '/quota'; },
    del: function (kid) { return '/v1/admin/appkeys/' + encodeURIComponent(kid) + '/delete'; },      // 受限删（AC-2）
    renew: function (kid) { return '/v1/admin/appkeys/' + encodeURIComponent(kid) + '/expires'; },   // 续期（AC-3）
    usage: '/v1/admin/appkeys/usage',                    // 当日「已用/上限」实时值（只读；2026-10-14 增补最近调用）
    publish: '/v1/admin/publish'                         // 已发布服务列表（签发按服务授权的数据源）
  };

  /* ── SHA-256：优先 Web Crypto；file:// 等非安全上下文回退纯 JS 实现 ── */
  function sha256Js(ascii) {
    // 极简纯 JS SHA-256（FIPS 180-4），输入限 ASCII（凭证均为 ASCII）。
    var K = [0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
      0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
      0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x98e5152c, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6353, 0x14292967,
      0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380a13, 0x650a7358, 0x766a0abb, 0x81c2c92e, 0x92722c85,
      0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
      0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2];
    var H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    var len = ascii.length, words = [];
    for (var i = 0; i < len; i++) words[i >> 2] = (words[i >> 2] || 0) | (ascii.charCodeAt(i) << (24 - (i % 4) * 8));
    words[len >> 2] = (words[len >> 2] || 0) | (0x80 << (24 - (len % 4) * 8));
    var nWords = (((len + 8) >> 6) + 1) * 16;
    words[nWords - 1] = len * 8;
    var w = new Array(64);
    function rr(x, n) { return (x >>> n) | (x << (32 - n)); }
    for (var p = 0; p < nWords; p += 16) {
      for (i = 0; i < 16; i++) w[i] = (words[p + i] || 0) >>> 0;
      for (i = 16; i < 64; i++) {
        var s0 = rr(w[i - 15], 7) ^ rr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        var s1 = rr(w[i - 2], 17) ^ rr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      var a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
      for (i = 0; i < 64; i++) {
        var S1 = rr(e, 6) ^ rr(e, 11) ^ rr(e, 25), ch = (e & f) ^ (~e & g);
        var t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
        var S0 = rr(a, 2) ^ rr(a, 13) ^ rr(a, 22), mj = (a & b) ^ (a & c) ^ (b & c);
        var t2 = (S0 + mj) >>> 0;
        h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0; H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
      H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0; H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
    }
    return H.map(function (x) { return ('00000000' + x.toString(16)).slice(-8); }).join('');
  }

  function hexToB64(hex) {
    var s = '';
    for (var i = 0; i < hex.length; i += 2) s += String.fromCharCode(parseInt(hex.substr(i, 2), 16));
    return root.btoa(s);
  }

  /* sha256Hex(secret) → Promise<64 位小写 hex> */
  function sha256Hex(secret) {
    if (root.crypto && root.crypto.subtle && root.crypto.subtle.digest) {
      try {
        return root.crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret)).then(function (buf) {
          return Array.prototype.map.call(new Uint8Array(buf), function (b) {
            return ('0' + b.toString(16)).slice(-2);
          }).join('');
        });
      } catch (err) { /* fall through to pure JS */ }
    }
    return Promise.resolve(sha256Js(secret));
  }

  /* ── 凭证格式（与服务端 appkeys.py/auth_admin.py 同口径：sb_/ak_；sk_）── */
  function randHex(nBytes) {
    var a = new Uint8Array(nBytes);
    if (root.crypto && root.crypto.getRandomValues) root.crypto.getRandomValues(a);
    else for (var i = 0; i < nBytes; i++) a[i] = Math.floor(Math.random() * 256);
    return Array.prototype.map.call(a, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
  }
  function genAppkey() { return 'ak_' + randHex(8); }   // 演示模式格式对齐用；live 模式由服务端签发
  function genSecret() { return 'sk_' + hexToB64(randHex(32)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '').slice(0, 43); }
  function maskAppkey(ak) { return ak.slice(0, 7) + '****' + ak.slice(-4); }
  /* Secret 存储形态：'sha256:' ＋ 摘要十六进制前 16 位——任何地方都不留明文 */
  function secretHashDisplay(hex64) { return 'sha256:' + hex64.slice(0, 16); }

  /* ── 内部租户标识：每次签发生成（小写字母数字，满足后端租户 ID 规则 2~64 位）。
   *   表单极简化后不再让管理员选「调用方」；备注名承担调用方/绑定应用名的展示职责。 ── */
  function genTenantId() { return 't' + randHex(5); }
  /* 备注名缺省值：<租户标识>-<服务短名>（服务短名＝服务 ID） */
  function defaultRemark(tenant, serviceId) { return (tenant || '') + '-' + (serviceId || ''); }
  /* 有效期 → expires_at（YYYY-MM-DD，本地时区）。
   * 后端口径（2026-10-08 起）：显式 expires_at 优先（本页 30/90/365 天真实生效），
   * 缺省回落部署常量 SB_TTL_DAYS（默认 7 天，门户试用链路不受影响）。 */
  function validityToDate(days) {
    if (days === 'permanent') return 'permanent';   // 永久：expires_at=permanent（后端同字面量）
    var d = new Date(Date.now() + (days || 30) * 86400e3);
    function pad(x) { return ('0' + x).slice(-2); }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  /* ── OA 批件号校验（存量校验函数；表单极简化后不再采集，保留供自助页复用） ── */
  function oaTicketValid(t) { return /^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$/.test((t || '').trim()); }

  /* ── 相对时间（列表「最近使用」） ── */
  function relTime(iso, nowMs) {
    if (!iso) return '从未调用';
    var t = Date.parse(iso);
    if (isNaN(t)) return '—';
    var now = nowMs || Date.now();
    var diff = now - t;
    if (diff < 0) diff = 0;
    if (diff < 60e3) return '刚刚';
    if (diff < 3600e3) return Math.floor(diff / 60e3) + ' 分钟前';
    if (diff < 86400e3) return Math.floor(diff / 3600e3) + ' 小时前';
    var d = new Date(t);
    function pad(x) { return ('0' + x).slice(-2); }
    var hm = pad(d.getHours()) + ':' + pad(d.getMinutes());
    var today = new Date(now);
    if (d.toDateString() === today.toDateString()) return '今天 ' + hm;
    var yest = new Date(now - 86400e3);
    if (d.toDateString() === yest.toDateString()) return '昨天 ' + hm;
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + hm;
  }
  function fmtThousands(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  /* ── 过期判定：对齐后端（service/app/appkeys.py）——UTC 当日日期 >= expires_at
   *    即过期（到期日当天调用即被拒）；未登记有效期（空/非法）不算过期。 ── */
  function keyExpired(k, todayIso) {
    var exp = String((k && k.expiresAt) || '').slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(exp)) return false;
    var today = todayIso || new Date().toISOString().slice(0, 10);
    return today >= exp;
  }

  /* ── 有效期展示（AC-4 贯通）：permanent/空（未登记）＝「永久」——不过期口径与
   *    后端 _expired 对齐（非 YYYY-MM-DD 一律不过期），展示层统一走本函数。 ── */
  function expiryText(exp) {
    var v = String(exp || '');
    return (!v || v === 'permanent') ? '永久' : v;
  }

  /* ── 历史/有效视图判定（AC-1）：过期 / 已禁用 / 轮换过渡期已结束 → 历史；
   *    轮换重叠期内的旧密钥仍可调用 → 留在有效视图。 ── */
  function isHistoryKey(k) {
    if (!k) return false;
    var r = k.rotation || {};
    return keyExpired(k) || k.status !== 'active' || !!(r.rotated && r.expired);
  }

  /* ── 凭证登记导出（演示模式预览；live 模式直接展示服务端登记文件原文） ── */
  function buildYaml(keys) {
    var head = [
      '# ============================================================',
      '# OneServe appkeys.yaml —— AppKey 发放登记（演示模式导出预览）',
      '# 真实权威源：service/registry/appkeys.yaml（控制台签发自动落盘、随仓库留痕）。',
      '# 安全纪律：secret_hash 只存 sha256(secret) 摘要，明文永不落盘。',
      '# ============================================================',
      'keys:'
    ].join('\n');
    var body = keys.map(function (k) {
      var lines = [
        '  - caller_id: ' + k.callerId,
        '    caller_name: ' + k.callerName,
        '    app_name: ' + k.appName,
        '    key_type: ' + (k.keyType || 'formal'),
        '    appkey_masked: ' + k.appkeyMasked,
        '    secret_hash: ' + k.secretHashOnly,
        '    tag_sets: [' + (k.tagSets || []).join(', ') + ']',
        '    quota_qps: ' + k.quotaQps,
        '    quota_daily: ' + k.quotaDaily,
        '    oa_ticket: ' + k.oaTicket,
        '    status: ' + k.status
      ];
      if (k.grants && k.grants.length) {
        lines.push('    grants: ' + k.grants.map(function (g) {
          return g.service + '（' + g.fields.length + ' 字段）';
        }).join('，'));
      }
      if (k.expiresAt) lines.push('    expires_at: ' + k.expiresAt);
      return lines.join('\n');
    }).join('\n');
    return head + (keys.length ? '\n' + body : '') + '\n';
  }

  /* ── 凭据文件内容（AppKey＋Secret＋调试台 URL；用户主动下载才含明文） ── */
  function credentialText(k, secret) {
    var lines = [
      'OneServe 接入凭据',
      '================================',
      '备注：' + k.appName,
      '使用方：' + k.callerName,
      '凭证类型：' + (k.keyType === 'sandbox' ? '沙箱凭证（测试用，正式凭证另行审批；限时有效）' : '正式凭证'),
      ''
    ];
    if (k.oaTicket && k.oaTicket !== '—') lines.push('OA 批件号：' + k.oaTicket, '');
    if (k.grants && k.grants.length) {
      lines.push('可调服务：' + k.grants.map(function (g) {
        return g.service + (g.endpoint ? ' ' + g.endpoint : '') + '（' + g.fields.length + ' 字段）';
      }).join('；'), '');
    }
    lines.push(
      'AppKey:  ' + k.appkeyFull,
      'Secret:  ' + secret,
      '有效期至：' + expiryText(k.expiresAt),
      '调试台 URL: ' + TRIAL_URL,
      '',
      '注意：请经安全渠道交付接入方（不经 IM 转发明文）。',
      'Secret 仅此一次显示，平台侧只存摘要：' + k.secretHashOnly
    );
    return lines.join('\n') + '\n';
  }

  /* ── 按服务授权：已发布服务视图归一（GET /v1/admin/publish → services 列表） ──
   * 形状对齐发布台 _svc_view（ops_config）：serviceId/id、serviceName、routePath、
   * fields（字段名数组）、fieldSpace、status（published|draft|offline）。 */
  function normalizePublishedServices(j) {
    var list = (j && j.services) || [];
    return list.map(function (s) {
      var sid = s.serviceId || s.id || '';
      return { id: sid, name: s.serviceName || s.name || sid,
               routePath: s.routePath || s.route || '',
               fieldSpace: s.fieldSpace || '', status: s.status || 'published',
               kind: s.kind || (s.fieldSpace === 'proxy' ? 'proxy' : 'service'),
               fields: (s.fields || []).filter(function (f) { return !!f; }) };
    }).filter(function (s) { return !!s.id; });
  }
  /* C3（2026-10-20）：代理服务＝整路由授权（无字段投影），前端据此隐藏字段勾选。 */
  function isProxyService(s) { return !!(s && s.kind === 'proxy'); }
  function publishedOnly(list) {
    return (list || []).filter(function (s) { return s.status === 'published'; });
  }
  /* ── 路由尾段（选服务/授权 chips 消歧用）：/v1/svc/usr-asset-tag → usr-asset-tag ── */
  function routeTail(route) {
    var r = String(route || '');
    var i = r.lastIndexOf('/');
    return i >= 0 ? r.slice(i + 1) : r;
  }

  /* ── 授权 chips（AppKey 二批 §B 授权可见性）：k.grants → [{name, tail, route, fieldCount}]。
   * live 行形状＝admin_api._grants_view：{service, serviceName, endpoint, kind, fieldCount}；
   * 演示/mock 行＝旧形状 {service, endpoint?, fields[]}（字段数取数组长度）。 ── */
  function grantChipList(k) {
    return ((k && k.grants) || []).map(function (g) {
      var route = String(g.endpoint || '');
      var isProxy = g.kind === 'proxy';
      return { name: String(g.serviceName || g.service || ''),
               route: route, tail: routeTail(route), kind: isProxy ? 'proxy' : 'service',
               fieldCount: isProxy ? 0 :
                 ((g.fieldCount != null) ? g.fieldCount : ((g.fields || []).length)) };
    });
  }
  var GRANT_CHIP_LIMIT = 3;   // 收起态最多展示 3 枚 chip，超出「+N」展开（AppKey 二批 §B.1）

  /* ── 人话错误（AppKey 二批 §C 失败路径）：后端 message 已是人话则直用；
   * 裸「HTTP xxx」/网络错 → 兜底文案（不把裸码丢给管理员）。 ── */
  function humanError(err, fallback) {
    var m = (err && err.message) || '';
    var mm = /^HTTP (\d+)$/.exec(m);
    if (m && !mm) return m;
    var code = (err && err.status) || (mm && mm[1]) || '';
    return (fallback || '操作失败') + (code ? '（服务端 ' + code + '）' : '') + '，请稍后重试';
  }

  /* 表单选择 → grants 请求体：[{service, endpoint?, fields}]；普通服务未选字段剔除
   * （后端对空字段拒绝，前端先行拦下）；代理服务例外——整路由授权，fields=[] 合法。 */
  function grantsFromForm(svcSel, fieldSel, services) {
    var byId = {};
    (services || []).forEach(function (s) { byId[s.id] = s; });
    return (svcSel || []).map(function (sid) {
      var fields = ((fieldSel || {})[sid] || []).slice();
      var g = { service: sid, fields: fields };
      var svc = byId[sid];
      var ep = svc && svc.routePath;
      if (ep) g.endpoint = ep;
      if (isProxyService(svc)) g.kind = 'proxy';   // 仅前端展示口径；后端 grants 校验忽略额外键
      return g;
    }).filter(function (g) {
      var svc = byId[g.service];
      return isProxyService(svc) || g.fields.length > 0;
    });
  }

  /* ── Alpine 组件：编排（取数→表单→签发→弹窗→登记查看）──
   * 注册口径同门户：alpine:init 事件里 Alpine.data(...)，业务 js 先于 alpine.min.js
   * 加载（本仓 alpine 构建在自身执行后 queueMicrotask 即 start()——晚放会报
   * not defined）。 */
  if (root.document) {
    root.document.addEventListener('alpine:init', function () {
      if (root.Alpine && root.Alpine.data) root.Alpine.data('appkeyAdmin', appkeyAdmin);
    });
  }
  function appkeyAdmin() {
    /* 管理令牌机制已拆除，仅保留 admin-gate.js 的无闸 shim */
    return Object.assign(AdminGate.gateMixin(), {
      /* 状态 */
      keys: [], callers: [], source: 'demo',
      publishedServices: [], svcState: 'idle',   // 已发布服务列表（签发授权数据源）；idle|ok|empty|error
      usage: {},                  // kid -> {used, limit, remaining, exceeded}（当日实时）
      usageState: 'idle',         // idle | ok | error（用量取数独立于列表，失败如实标注）
      usageCaliber: null,         // 端点随行口径说明（页面文案不写死第二份）
      busy: false,
      sandboxTtlDays: 7,
      rotateOverlapDays: 7,       // 旧密钥过渡期天数（后端常数默认 7；loadLive 以后端为准）
      issueOpen: false,
      form: blankForm(),
      issueError: '',
      secretModal: { open: false, appkey: '', secret: '', trialUrl: TRIAL_URL, keyRef: null, copied: false },
      confirmModal: { open: false, stage: 1, index: -1, action: '', title: '', detail: '', armLabel: '', armed: false, hint: '' },
      renewModal: { open: false, index: -1, mode: 'date', date: '', stage: 1, armed: false, hint: '', err: '' },   // 续期（AC-3，两段式）
      quotaModal: { open: false, index: -1, target: null, qps: 50, daily: 10000, err: '' },
      listTab: 'active',        // AC-1 双视图：'active' 有效凭证（默认）| 'history' 历史（过期/禁用/轮换失效）
      grantsOpen: {},           // AppKey 二批 §B：kid → true＝该行授权清单展开（+N / 收起）
      yamlModal: { open: false, text: '', copied: false },
      lastTrace: '',
      KEY_LIMIT: KEY_LIMIT, TAGSETS: TAGSETS, VALIDITY_DAYS: VALIDITY_DAYS,


      async boot() {
        var live = await this.loadLive();
        if (!live) {
          var stored = await this.loadJson('mock/appkeys.json');
          this.keys = ((stored.data && stored.data.keys) || []).map(function (k) { return Object.assign({}, k); });
          this.source = 'demo';
        }
        this.loadServices().then(function () {
          this.applyServicePrefill();   // C6：上游代理发布后带 ?service=<id> 跳来，预填该服务
        }.bind(this));
        /* 计量区块已迁报表专区（1b-9 迁移）：本页不再发计量取数请求 */
      },

      /* C6：?service=<id> → 直接打开签发区并预选服务（代理/普通服务同口径）。
         服务不在已发布列表时如实提示，不静默塞一个不存在的 id。 */
      applyServicePrefill() {
        var sid = '';
        try { sid = new URLSearchParams(root.location.search).get('service') || ''; } catch (e) { sid = ''; }
        if (!sid) return;
        var hit = this.publishedServices.filter(function (s) { return s.id === sid; })[0];
        this.openIssue();
        if (!hit) {
          this.issueError = '指定的服务「' + sid + '」不在已发布列表（可能已下线），请重新选择';
          return;
        }
        this.form.serviceId = sid;
        this.onServiceChange();
      },

      /* ── live 取数（列表；不可达降级 mock） ── */
      loadLive() {
        var self = this;
        return this.apiFetch(API.list)
          .then(function (j) {
            self.keys = (j.keys || []).map(function (k) { return Object.assign({}, k); });
            self.callers = j.callers || [];
            self.sandboxTtlDays = j.sandboxTtlDays || 7;
            self.rotateOverlapDays = j.rotateOverlapDays || 7;
            self.source = 'live';
            self.loadUsage();                  // 列表取到顺带拉当日用量（失败不阻断列表）
            return true;
          })
          .catch(function () {
            self.source = 'demo';
            return false;   // 网络不可达均降级 mock 列表（角标如实标注）
          });
      },

      /* ── 已发布服务列表（GET /v1/admin/publish；只取 status=published） ── */
      loadServices() {
        var self = this;
        return this.apiFetch(API.publish)
          .then(function (j) {
            self.publishedServices = publishedOnly(normalizePublishedServices(j));
            self.svcState = self.publishedServices.length ? 'ok' : 'empty';
          })
          .catch(function () { self.publishedServices = []; self.svcState = 'error'; });
      },

      loadJson(path) {
        return fetch(path, { cache: 'no-store' })
          .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
          .then(function (j) { return { data: j, source: 'live' }; })
          .catch(function () { return { data: null, source: 'demo' }; });
      },

      /* ── 后端调用（错误规整为 {status, message}） ── */
      apiFetch(url, opts) {
        var headers = Object.assign({}, (window.AdminGate && AdminGate.adminHeaders()) || {});
        if (opts && opts.body) headers['Content-Type'] = 'application/json';
        return fetch(url, Object.assign({}, opts || {}, { headers: headers, cache: 'no-store' }))
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

      saveToken() { this.loadLive(); },

      /* ── 列表辅助 ── */
      relTime(iso) { return relTime(iso); },
      fmt(n) { return fmtThousands(n); },
      /* 状态三态——过期优先（到期日当天起调用被拒，页面不得仍显「启用中」） */
      statusLabel(k) { return keyExpired(k) ? '已过期' : (k.status === 'active' ? '启用中' : '已禁用'); },
      statusBadgeClass(k) { return keyExpired(k) ? 'badge-expired' : (k.status === 'active' ? 'badge-active' : 'badge-disabled'); },
      keyStateHint(k) {
        if (keyExpired(k)) return '已于 ' + (k.expiresAt || '—') + ' 到期：调用会被拒绝；如需继续使用请重置密钥或重新签发';
        return '';
      },
      /* 旧密钥过渡期倒计时文案（后端口径：daysLeft<=0 即已失效） */
      rotationLabel(k) {
        var r = k.rotation || {};
        if (!r.rotated) return '—';
        if (r.expired) return '旧密钥已失效';
        return '旧密钥剩 ' + (r.daysLeft != null ? r.daysLeft : '?') + ' 天';
      },
      rotationBadgeClass(k) {
        var r = k.rotation || {};
        return r.expired ? 'badge-disabled' : 'badge-overlap';
      },

      /* ── AC-1 双视图：默认只显有效凭证；过期/禁用/轮换失效折叠进「历史」 ── */
      isHistoryKey(k) { return isHistoryKey(k); },
      inCurrentTab(k) {
        var h = isHistoryKey(k);
        return this.listTab === 'history' ? h : !h;
      },
      visibleCount() {
        var self = this;
        return this.keys.filter(function (k) { return self.inCurrentTab(k); }).length;
      },
      activeCount() { return this.keys.filter(function (k) { return !isHistoryKey(k); }).length; },
      historyCount() { return this.keys.filter(function (k) { return isHistoryKey(k); }).length; },
      expiryText(exp) { return expiryText(exp); },
      /* ── AC-6 最近调用：live 且用量端点可达 → lastUsedAt/everUsed；取不到如实「—」 ── */
      lastUsedText(k) {
        if (this.usageState !== 'ok') return '—';
        var u = this.usageOf(k);
        if (u.lastUsedAt) return relTime(u.lastUsedAt);
        return u.everUsed ? '—' : '从未使用';
      },

      /* ── 授权可见性（AppKey 二批 §B）：列表行 chips（名·路由尾段）＋行内展开完整 grants ── */
      grantChips(k) { return grantChipList(k); },
      hasGrants(k) { return grantChipList(k).length > 0; },
      grantsToShow(k) {
        return this.isGrantsOpen(k) ? grantChipList(k) : grantChipList(k).slice(0, GRANT_CHIP_LIMIT);
      },
      grantOverflow(k) { return Math.max(0, grantChipList(k).length - GRANT_CHIP_LIMIT); },
      isGrantsOpen(k) { return !!(k && this.grantsOpen[k.kid]); },
      toggleGrants(k) {
        if (!k) return;
        this.grantsOpen[k.kid] = !this.grantsOpen[k.kid];
      },
      /* chip 文案：收起＝「名·尾段」；展开（行内详情）＝「名　完整路由　N 字段」（§B.2 三要素） */
      grantChipText(g, open) {
        return open ? (g.name + '　' + g.route + '　' + g.fieldCount + ' 字段')
                    : (g.name + '·' + g.tail);
      },

      /* ── 按服务授权：单选服务＋字段勾选（默认全选） ── */
      selectedService() {
        var self = this;
        return this.publishedServices.find(function (s) { return s.id === self.form.serviceId; }) || null;
      },
      /* 签发下拉选项文案（AppKey 二批 §A.3）：名字（路由尾段）——彻底消灭纯同名歧义；
         C3：代理服务明确标注「代理服务 · 整路由授权」，与普通字段服务一眼区分。 */
      svcOptionText(s) {
        var suffix = isProxyService(s) ? ' · 代理服务' : '';
        return ((s && s.name) || '') + '（' + routeTail(s && s.routePath) + suffix + '）';
      },
      isProxySelected() { return isProxyService(this.selectedService()); },
      selectedServiceFields() {
        var svc = this.selectedService();
        return svc ? svc.fields : [];
      },
      fieldChecked(sid, f) { return (this.form.fieldSel[sid] || []).indexOf(f) >= 0; },
      toggleField(sid, f, checked) {
        var arr = this.form.fieldSel[sid] || (this.form.fieldSel[sid] = []);
        var i = arr.indexOf(f);
        if (checked && i < 0) arr.push(f);
        if (!checked && i >= 0) arr.splice(i, 1);
        this.issueError = '';
      },
      onServiceChange() {
        var sid = this.form.serviceId;
        var svc = this.selectedService();
        if (sid && svc && !this.form.fieldSel[sid]) {
          this.form.fieldSel[sid] = svc.fields.slice();   // 普通服务默认勾满；代理 fields=[] 即整路由
        }
        this.issueError = '';
      },
      selectedGrants() {
        var sel = this.form.serviceId ? [this.form.serviceId] : [];
        return grantsFromForm(sel, this.form.fieldSel, this.publishedServices);
      },
      svcGrantNote() {
        if (this.svcState === 'error') return '已发布服务列表暂时取不到，无法选择授权服务；请稍后刷新重试。';
        if (!this.publishedServices.length) return '当前没有已发布的服务：请先在「服务发布」页发布服务，再来签发。';
        if (this.isProxySelected()) return '';   /* 2026-10-20 视觉 QC：代理说明只由下方「整路由授权」块承担，避免同屏近重复 */
        return '所选字段即该凭证可查询的数据范围；默认勾满该服务全部字段，可按需减少。';
      },
      grantsText() {
        var g = (this.secretModal.keyRef && this.secretModal.keyRef.grants) || [];
        return g.map(function (x) {
          return x.service + (x.kind === 'proxy' ? '（整路由）' : '（' + x.fields.length + ' 字段）');
        }).join('；') || '—';
      },
      /* P3 弹窗卡（维护者 4）：签发成功弹窗/留痕条的配额口径行。live 签发回包 record 为
       * snake_case（quota_qps/quota_daily），演示 rec 为 camel；缺省同后端 50/10000。 */
      quotaText() {
        var r = this.secretModal.keyRef || {};
        var qps = Number(r.quota_qps != null ? r.quota_qps : (r.quotaQps != null ? r.quotaQps : DEFAULT_QPS));
        var daily = Number(r.quota_daily != null ? r.quota_daily : (r.quotaDaily != null ? r.quotaDaily : DEFAULT_DAILY));
        return '每日 ' + fmtThousands(daily) + ' 次／每秒 ' + fmtThousands(qps) + ' 次（如需调整联系管理员）';
      },

      /* ── 签发流程 ── */
      openIssue() {
        this.form = blankForm(); this.issueError = ''; this.issueOpen = true;
      },
      canGenerate() {
        return !!(this.form.serviceId && this.selectedGrants().length > 0);
      },
      generateHint() {
        if (this.svcState === 'error') return '服务列表暂时取不到：请稍后刷新重试';
        if (!this.publishedServices.length) return '当前无已发布服务：请先在「服务发布」页发布服务';
        if (!this.form.serviceId) return '请选择授权服务';
        if (!this.selectedGrants().length) return '请至少保留一个授权字段';
        if (this.isProxySelected()) return '信息完整，可签发（代理整路由授权 · 沙箱凭证 · 签发后立即生效）';
        return '信息完整，可签发（沙箱凭证 · 签发后立即生效 · 有效期 ' +
               (this.form.validityDays === 'permanent' ? '永久' : this.form.validityDays + ' 天') + '）';
      },

      async generate() {
        this.issueError = '';
        if (!this.canGenerate()) {
          this.issueError = this.generateHint() || '请补全表单后再生成';
          return;
        }
        if (this.source === 'live') return this.generateLive();
        return this.generateDemo();
      },

      /* 真签发：服务端登记（权威源落盘＋热加载）全在后端完成 */
      async generateLive() {
        var f = this.form;
        var svc = this.selectedService() || { id: f.serviceId };
        var tenant = genTenantId();
        var remark = f.remark.trim() || defaultRemark(tenant, svc.id);
        var body = {
          tenant: tenant,
          caller_name: remark,   // 备注名照传调用方名（表单极简化：调用方概念并入备注名）
          app_name: remark,      // 同上：绑定应用名＝备注名
          key_type: 'sandbox',   // 固定沙箱凭证（正式凭证走线下 OA 审批，本页不签发）
          tag_sets: [],          // 业务域标注已随表单极简化删除（存量标注仅历史展示）
          // 配额（每秒/每日）不在表单暴露：后端缺省 quota_qps=50／quota_daily=10000
          // （service/app/admin_api.py issue 的 or 50 / or 10000 缺省）；
          // 管理员可随时经行内「调整配额」（POST /v1/admin/appkeys/{appkey}/quota）改。
          expires_at: validityToDate(f.validityDays)
        };
        var grants = this.selectedGrants();
        if (grants.length) body.grants = grants;   // 显式按服务授权（后端透传校验）
        this.busy = true;
        try {
          var j = await this.apiFetch(API.issue, { method: 'POST', body: JSON.stringify(body) });
          var rec = Object.assign({}, j.record, { appkeyFull: j.appkey, grants: grants });
          this.secretModal = { open: true, appkey: j.appkey, secret: j.secret,
                               trialUrl: TRIAL_URL, keyRef: rec, copied: false };
        } catch (err) {
          this.issueError = (err.status === 429)
            ? '签发过于频繁（' + (err.message || '已限频') + '），请稍后再试'
            : humanError(err, '签发失败');   // §C：后端人话直用，裸 HTTP 码兜底成句
        } finally {
          this.busy = false;
        }
      },

      /* 演示模式（后端不可达降级）：仅内存演示，不伪造真实发放 */
      async generateDemo() {
        var f = this.form;
        var tenant = genTenantId();
        var remark = f.remark.trim() || defaultRemark(tenant, f.serviceId);
        var appkey = 'sb_' + randHex(8);
        var secret = genSecret();
        var hex = await sha256Hex(secret);
        var rec = {
          callerId: tenant, callerName: remark, appName: remark,
          keyType: 'sandbox',
          appkeyFull: appkey, appkeyMasked: maskAppkey(appkey),
          secretHashOnly: secretHashDisplay(hex),
          tagSets: [], quotaQps: DEFAULT_QPS, quotaDaily: DEFAULT_DAILY,
          oaTicket: '—', lastUsedAt: null, status: 'active',
          grants: this.selectedGrants(),
          expiresAt: validityToDate(f.validityDays),
          issuedAt: new Date().toISOString()
        };
        this.secretModal = { open: true, appkey: appkey, secret: secret, trialUrl: TRIAL_URL, keyRef: rec, copied: false };
      },

      copySecret() {
        var m = this.secretModal;
        this.copyText(m.secret);
        m.copied = true;
        var self = this;
        setTimeout(function () { self.secretModal.copied = false; }, 1500);
      },
      downloadCredential() {
        var m = this.secretModal;
        this.downloadText('oneserve-credential-' + m.appkey + '.txt', credentialText(m.keyRef, m.secret));
      },
      confirmSaved() {
        var m = this.secretModal;
        if (!m.keyRef) { m.open = false; return; }
        if (m.rotate) {   // 重置密钥：列表已由 loadLive 刷新，避免重复插入
          this.secretModal = { open: false, appkey: '', secret: '', trialUrl: TRIAL_URL, keyRef: null, copied: false };
          this.trace('新密钥已生效：旧密钥在过渡期内仍可继续调用，至 ' + (m.overlapExpiresAt || '—') + ' 自动失效 · ' + nowHM());
          return;
        }
        var rec = Object.assign({}, m.keyRef);
        delete rec.appkeyFull;                     // 完整 AppKey 也只进弹窗，列表/导出全用掩码
        this.keys.push(rec);                        // 列表刷新（Secret 列只显摘要）
        this.secretModal = { open: false, appkey: '', secret: '', trialUrl: TRIAL_URL, keyRef: null, copied: false };
        this.issueOpen = false;
        this.trace(this.source === 'live'
          ? '签发后立即生效：凭证已登记，可立即调用；配额：默认每日 10000 次／每秒 50 次（如需调整联系管理员） · ' + nowHM()
          : '演示模式：仅内存演示未写盘（后端不可达）；配额：默认每日 10000 次／每秒 50 次（如需调整联系管理员） · ' + nowHM());
        if (this.source === 'live') this.loadLive();   // 以服务端登记为准刷新列表
      },

      /* ── 两段式禁用/启用 ── */
      askToggle(i) {
        var k = this.keys[i];
        var disabling = k.status === 'active';
        this.confirmModal = {
          open: true, stage: 1, index: i, action: disabling ? 'disable' : 'enable',
          title: (disabling ? '禁用' : '启用') + ' AppKey ' + k.appkeyMasked,
          detail: '备注：' + (k.appName || k.callerName || '—') +
                  (disabling ? '。禁用后立即生效：使用该凭证的调用会立刻被拒绝（两段式确认）。'
                             : '。启用后立即生效：该凭证恢复可调用（两段式确认）。'),
          armLabel: disabling ? '我确认禁用该凭证，使用方调用将立刻失败' : '我确认启用该凭证',
          armed: false, hint: ''
        };
      },
      /* ── 删除（受限删，AC-2）：两段式确认；仅「从未被调用」的凭证可删（后端强制，
       *    有调用记录 409 人话拒绝且弹窗保持打开展示文案）。 ── */
      askDelete(i) {
        var k = this.keys[i];
        this.confirmModal = {
          open: true, stage: 1, index: i, action: 'delete',
          title: '删除 AppKey ' + k.appkeyMasked,
          detail: '备注：' + (k.appName || k.callerName || '—') +
                  '。删除＝从凭证登记中移除（不可恢复）；仅限「从未被调用」的凭证——' +
                  '有调用记录的凭证只能禁用（审计账不断链）。',
          armLabel: '我确认删除该凭证，且知悉此操作不可恢复',
          armed: false, hint: ''
        };
      },
      async doDelete(m) {
        var k = this.keys[m.index];
        if (this.source === 'live') {
          try {
            var j = await this.apiFetch(API.del(k.kid), { method: 'POST', body: JSON.stringify({}) });
            this.keys.splice(m.index, 1);
            this.trace('已删除 ' + k.appkeyMasked + '（从未被调用，登记移除' +
                       (j.auditId ? '，审计留痕 ' + j.auditId : '') + '）· ' + nowHM());
          } catch (err) {
            m.hint = err.message || '删除失败';   // 409 人话文案在确认框内直接可读
            this.trace('删除被拒绝：' + (err.message || '服务异常') + ' · ' + nowHM());
            return false;                        // 保持弹窗打开，文案不闪没
          }
        } else {
          this.keys.splice(m.index, 1);
          this.trace('演示模式：删除仅内存演示未写盘 · ' + nowHM());
        }
        return true;
      },
      /* ── 重置密钥：两段式确认后签发新密钥，旧密钥进入过渡期 ──
       * AC-5：按钮/提示明示语义「密钥忘了就重置——新密钥立即显示一次，旧的 7 天缓冲后失效」。 ── */
      askRotate(i) {
        var k = this.keys[i];
        this.confirmModal = {
          open: true, stage: 1, index: i, action: 'rotate',
          title: '重置密钥 ' + k.appkeyMasked,
          detail: '密钥忘了就重置——新密钥立即显示一次，旧的 ' + this.rotateOverlapDays + ' 天缓冲后失效。' +
                  '备注：' + (k.appName || k.callerName || '—') +
                  '。将生成一把新密钥并立即生效；旧密钥在 ' + this.rotateOverlapDays +
                  ' 天过渡期内仍可继续调用（不断联），到期自动失效。' +
                  '请及时把新密钥交付使用方并完成切换。',
          armLabel: '我确认重置密钥，并将新密钥交付使用方',
          armed: false, hint: ''
        };
      },
      async doRotate(m) {
        var k = this.keys[m.index];
        if (this.source === 'live') {
          try {
            var j = await this.apiFetch(API.rotate(k.kid), { method: 'POST', body: JSON.stringify({}) });
            this.secretModal = { open: true, appkey: j.appkey, secret: j.secret, trialUrl: TRIAL_URL,
                                 keyRef: Object.assign({}, j.record, { appkeyFull: j.appkey }),
                                 copied: false, rotate: true, overlapExpiresAt: j.overlapExpiresAt };
            this.trace('新密钥已生效：旧密钥可继续调用至 ' + (j.overlapExpiresAt || '—') +
                       '（' + (j.overlapDays || this.rotateOverlapDays) + ' 天过渡期） · ' + nowHM());
            this.loadLive();
          } catch (err) {
            m.hint = humanError(err, '重置密钥失败');   // §C：确认框内人话可见，弹窗保持打开
            this.trace('重置密钥失败：' + humanError(err, '服务异常') + ' · ' + nowHM());
            return false;
          }
        } else {
          var due = demoSandboxExpiry(this.rotateOverlapDays);
          k.rotation = { rotated: true, rotatedAt: new Date().toISOString().slice(0, 10),
                         overlapExpiresAt: due, daysLeft: this.rotateOverlapDays,
                         overlapDays: this.rotateOverlapDays, expired: false };
          this.trace('演示模式：重置密钥仅内存演示未写盘（过渡期 ' + this.rotateOverlapDays + ' 天） · ' + nowHM());
        }
        return true;
      },

      async confirmStep() {
        var m = this.confirmModal;
        if (m.stage === 1) { m.stage = 2; return; }
        if (!m.armed) {
          m.hint = '请先勾选确认开关，再点「确认执行」';   // 可见反馈（原为静默返回＝点击无反应）
          return;
        }
        m.hint = '';
        if (m.action === 'rotate') { var okRot = await this.doRotate(m); if (okRot !== false) m.open = false; return; }
        if (m.action === 'delete') { var okDel = await this.doDelete(m); if (okDel) m.open = false; return; }
        var k = this.keys[m.index];
        if (this.source === 'live') {
          try {
            var j = await this.apiFetch(API.status(k.kid), {
              method: 'POST', body: JSON.stringify({ action: m.action })
            });
            if (j.record) this.keys.splice(m.index, 1, Object.assign({}, j.record));
            this.trace((m.action === 'disable' ? '已禁用并生效：使用该凭证的调用即刻被拒绝 · ' : '已启用并生效：该凭证恢复可调用 · ') + nowHM());
          } catch (err) {
            m.hint = humanError(err, '操作失败');   // §C：人话进确认框，弹窗保持打开可重试
            this.trace('操作失败：' + humanError(err, '服务异常') + ' · ' + nowHM());
            return;
          }
        } else {
          k.status = (m.action === 'disable') ? 'disabled' : 'active';
          this.trace('演示模式：仅内存演示未写盘（' + (m.action === 'disable' ? '禁用' : '启用') + '） · ' + nowHM());
        }
        m.open = false;
      },
      closeConfirm() { this.confirmModal.open = false; },

      /* ── 调整配额（登记值落权威源并同步限流） ── */
      openQuota(i) {
        var k = this.keys[i];
        this.quotaModal = { open: true, index: i, target: k, qps: k.quotaQps, daily: k.quotaDaily, err: '' };
      },
      async saveQuota() {
        var m = this.quotaModal, k = this.keys[m.index];
        if (!k) { m.open = false; return; }
        if (!(m.qps > 0) || !(m.daily > 0)) {
          m.err = '每秒与每日上限须为正整数';   // 可见反馈（原为静默返回＝点击无反应）
          return;
        }
        m.err = '';
        if (this.source === 'live') {
          try {
            var j = await this.apiFetch(API.quota(k.kid), {
              method: 'POST', body: JSON.stringify({ quota_qps: m.qps, quota_daily: m.daily })
            });
            if (j.record) this.keys.splice(m.index, 1, Object.assign({}, j.record));
            this.trace('配额已更新并写入登记 · ' + nowHM());
          } catch (err) {
            m.err = humanError(err, '配额更新失败');   // §C：人话进弹窗，保持打开可修正重试
            this.trace('配额更新失败：' + humanError(err, '服务异常') + ' · ' + nowHM());
            return;
          }
        } else {
          k.quotaQps = m.qps; k.quotaDaily = m.daily;
          this.trace('演示模式：配额仅内存演示未写盘 · ' + nowHM());
        }
        m.open = false;
      },

      /* ── 续期（AC-3，两段式）：只能延长（后端强制，缩短 409 指引禁用）；可选永久 ── */
      openRenew(i) {
        var k = this.keys[i];
        var cur = k.expiresAt;
        this.renewModal = {
          open: true, index: i, stage: 1, armed: false, hint: '', err: '',
          mode: (!cur || cur === 'permanent') ? 'permanent' : 'date',
          date: (cur && /^\d{4}-\d{2}-\d{2}$/.test(cur)) ? cur : ''
        };
      },
      renewTargetText() {
        var m = this.renewModal;
        return m.mode === 'permanent' ? '永久（不再到期）' : (m.date || '未选择日期');
      },
      renewArmedLabel() {
        var m = this.renewModal;
        return m.mode === 'permanent'
          ? '我确认将该凭证的有效期改为永久'
          : '我确认把有效期延长至 ' + (m.date || '所选日期');
      },
      async saveRenew() {
        var m = this.renewModal, k = this.keys[m.index];
        if (!k) { m.open = false; return; }
        if (m.stage === 1) {
          if (m.mode === 'date' && !/^\d{4}-\d{2}-\d{2}$/.test(m.date)) {
            m.err = '请先选择延长到的日期';   // 可见反馈（不静默返回）
            return;
          }
          m.err = ''; m.stage = 2; return;
        }
        if (!m.armed) { m.hint = '请先勾选确认开关，再点「确认执行」'; return; }
        m.hint = '';
        var value = m.mode === 'permanent' ? 'permanent' : m.date;
        if (this.source === 'live') {
          try {
            var j = await this.apiFetch(API.renew(k.kid), {
              method: 'POST', body: JSON.stringify({ expires_at: value })
            });
            if (j.record) this.keys.splice(m.index, 1, Object.assign({}, j.record));
            this.trace('已续期 ' + k.appkeyMasked + ' → ' +
                       (value === 'permanent' ? '永久' : value) + ' · ' + nowHM());
          } catch (err) {
            m.err = err.message || '续期失败';   // 409 缩短拒绝文案（指引禁用）直接可见
            this.trace('续期被拒绝：' + (err.message || '服务异常') + ' · ' + nowHM());
            return;
          }
        } else {
          k.expiresAt = value;
          this.trace('演示模式：续期仅内存演示未写盘 · ' + nowHM());
        }
        m.open = false;
      },
      closeRenew() { this.renewModal.open = false; },

      /* ── 凭证登记查看（live＝服务端登记文件原文；demo＝当前列表导出预览） ── */
      async openYaml() {
        if (this.source === 'live') {
          try {
            var r = await fetch(API.yaml, { headers: {}, cache: 'no-store' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            var text = await r.text();
            this.yamlModal = { open: true, text: text, copied: false };
          } catch (err) {
            this.yamlModal = { open: true, text: '读取失败：' + (err.message || '服务异常'), copied: false };
          }
          return;
        }
        this.yamlModal = { open: true, text: buildYaml(this.keys), copied: false };
      },
      copyYaml() {
        var self = this;
        this.copyText(this.yamlModal.text);
        this.yamlModal.copied = true;
        setTimeout(function () { self.yamlModal.copied = false; }, 1500);
      },
      downloadYaml() { this.downloadText('appkeys.yaml', this.yamlModal.text); },

      /* ── 通用工具 ── */
      copyText(t) {
        if (root.navigator && navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(t).catch(function () {});
        }
      },
      downloadText(name, text) {
        var a = root.document.createElement('a');
        a.href = root.URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
        a.download = name;
        root.document.body.appendChild(a); a.click();
        setTimeout(function () { root.URL.revokeObjectURL(a.href); a.remove(); }, 0);
      },
      /* ── 当日用量（与门户配额视图同后端同口径） ──
       * 只按 kid 索引（列表不回完整 appkey）；取不到＝如实「—」，不填 0 冒充未用。 */
      loadUsage() {
        var self = this;
        return this.apiFetch(API.usage)
          .then(function (j) {
            var map = {};
            (j.items || []).forEach(function (u) { map[u.kid] = u; });
            self.usage = map;
            self.usageCaliber = j.caliber || null;
            self.usageState = 'ok';
            return true;
          })
          .catch(function () { self.usageState = 'error'; self.usage = {}; return false; });
      },
      usageOf(k) { return this.usage[k.kid] || {}; },
      usageText(k) {
        var u = this.usageOf(k);
        if (this.usageState !== 'ok' || u.used === undefined) return '—';
        return String(u.used) + ' / ' + (u.limit === null || u.limit === undefined ? '未登记' : this.fmt(u.limit));
      },
      /* 打满（used≥limit 且未超限）进警示态——「今日 10/10 与 5/10」视觉分级 */
      usageFull(k) {
        var u = this.usageOf(k);
        return this.usageState === 'ok' && typeof u.used === 'number' &&
               typeof u.limit === 'number' && u.limit > 0 && u.used >= u.limit;
      },
      /* 角标文案：超=已超当日上限（危险色）；满=恰好打满（警示色）；空=不显示 */
      usageFlag(k) {
        if (this.usageState !== 'ok') return '';
        if (this.usageOf(k).exceeded) return '超';
        return this.usageFull(k) ? '满' : '';
      },
      usageHint(k) {
        var u = this.usageOf(k);
        if (this.usageState !== 'ok') return '当日用量暂不可用（后端未返回）';
        if (u.used === undefined) return '当日用量暂不可用';
        var c = this.usageCaliber || {};
        return (c.usageSource || '当日真实调用条数') + '；' +
               (c.usageIncludes || '成功与失败都计入') +
               (u.exceeded ? '（已超当日上限）' : '');
      },
      usageNote() {
        if (this.usageState === 'error') return '当日用量暂不可用：用量端点未返回，可稍后重试（列表不受影响）。';
        if (this.usageState !== 'ok') return '';
        var c = this.usageCaliber || {};
        return '「今日 已用/上限」：' + (c.usageSource || '当日真实调用条数') + '，' +
               (c.usageIncludes || '成功与失败都计入') +
               '；上限为' + (c.limitSource || '登记的配额') + '，' +
               (c.limitEnforced === true ? '超过上限的调用会被拒绝' : '超限调用暂只记录、不拒绝') + '。';
      },
      /* ── 计量区块已迁报表专区（1b-9 迁移 · 设计稿 §5）：本页计量组件与取数整块撤除，
       * 用量聚合归侧栏「报表中心」四页（/v1/admin/usage/* 族）；本页零计量请求。 ── */
      trace(msg) { this.lastTrace = msg; }
    });
  };

  function blankForm() {
    return { serviceId: '', remark: '', validityDays: 30,
             fieldSel: {} };   // 服务 → 已勾选字段（默认全选）
  }
  function demoSandboxExpiry(days) {
    var d = new Date(Date.now() + (days || 7) * 86400e3);
    function pad(x) { return ('0' + x).slice(-2); }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  function nowHM() {
    var d = new Date();
    function pad(x) { return ('0' + x).slice(-2); }
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  /* 纯函数单元导出（selftest 直测） */
  root.appkeyAdmin = appkeyAdmin;   // 兜底直引用（Alpine 主路径走 alpine:init 注册）
  root.O3 = {
    KEY_LIMIT: KEY_LIMIT, TAGSETS: TAGSETS, TRIAL_URL: TRIAL_URL, VALIDITY_DAYS: VALIDITY_DAYS,
    sha256Js: sha256Js, sha256Hex: sha256Hex, secretHashDisplay: secretHashDisplay,
    maskAppkey: maskAppkey, genAppkey: genAppkey, genSecret: genSecret,
    oaTicketValid: oaTicketValid, relTime: relTime, fmtThousands: fmtThousands,
    buildYaml: buildYaml, credentialText: credentialText, demoSandboxExpiry: demoSandboxExpiry,
    keyExpired: keyExpired, expiryText: expiryText, isHistoryKey: isHistoryKey,
    normalizePublishedServices: normalizePublishedServices, publishedOnly: publishedOnly,
    isProxyService: isProxyService, grantsFromForm: grantsFromForm,
    routeTail: routeTail, grantChipList: grantChipList, humanError: humanError,
    genTenantId: genTenantId, defaultRemark: defaultRemark, validityToDate: validityToDate
  };
})(typeof window !== 'undefined' ? window : this);
