/* ============================================================
 * OneServe 运营控制台 · 账号管理逻辑（accounts.js，Alpine 组件）
 * W5 · 2026-10-15 登录墙与账号体系设计稿 B2/B3。
 *
 * 【走位】平台只做壳：本页全部数据经 /v1/admin/accounts/* 代理 Casdoor 管理 API，
 *   平台零用户表；Casdoor 不可达＝fail-closed 报错态（**无 mock 降级、无演示数据**
 *   ——账号是真实资产，出假账号＝弄虚作假，设计稿 B3 口径）。
 * 【能力】列表（GET users）／创建（账号＋显示名＋初始密码＋角色三选一）／
 *   禁用·启用／软删（hard 选项物理删）／重置密码（临时密码一次性显示）／角色分配。
 * 【角色】页面与导航仅 admin 可见（site-gate 按 JWT tag 解锁导航）；鉴权在后端
 *   require_role（W4 合流前为本地 admin tag 校验，off 态放行——联调项见
 *   service/app/admin_accounts.py 模块头）。
 * 【纪律】密码只在创建/重置弹窗出现一次，不落任何存储；请求头统一走
 *   AdminGate.adminHeaders()（Bearer 单点携带）；401 auth_required 由 site-gate
 *   fetch 包装器接管跳登录，本页不重复处理。
 * ============================================================ */
(function () {
  'use strict';

  var API = {
    users: '/v1/admin/accounts/users',
    user: '/v1/admin/accounts/users/',
  };
  var ROLES = ['admin', 'operator', 'viewer'];

  function roleLabel(tag) {
    return { admin: '管理员', operator: '操作员', viewer: '只读' }[String(tag || 'viewer')] || String(tag || 'viewer');
  }

  function humanError(payload, fallback) {
    /* 401 auth_required 由 site-gate 跳登录接管；此处只人话化其余信封 */
    var code = payload && payload.error;
    var msg = (payload && payload.message) || '';
    if (code === 'role_forbidden') { return '需要管理员角色（admin）——当前账号无权管理账号。'; }
    if (code === 'upstream_unreachable') { return '账号服务（Casdoor）暂不可达，请稍后重试。'; }
    if (code === 'unauthorized') { return '登录状态无效，请重新登录。'; }
    if (code === 'invalid_request') { return msg || '请求不合契约，请检查输入。'; }
    if (msg === 'Not Found') { return '账号管理端点未就绪（404）——待后端合流挂载后重试。'; }
    return msg || fallback || '操作失败，请稍后重试。';
  }

  function tempPassword() {
    /* 临时密码生成：3 段易读字符组拼接（数字/大写/小写各≥1，12 位） */
    var abc = 'abcdefghjkmnpqrstuvwxyz';
    var ABC = 'ABCDEFGHJKMNPQRSTUVWXYZ';
    var num = '23456789';
    var all = abc + ABC + num + '-_';
    var pick = function (set) {
      var arr = new Uint32Array(1);
      (window.crypto && window.crypto.getRandomValues) ? window.crypto.getRandomValues(arr)
                                                        : (arr[0] = Math.floor(Math.random() * 4294967296));
      return set.charAt(arr[0] % set.length);
    };
    var out = [pick(abc), pick(ABC), pick(num)];
    for (var i = 0; i < 9; i++) { out.push(pick(all)); }
    /* 打乱顺序（Fisher-Yates，量小直接实现） */
    for (var j = out.length - 1; j > 0; j--) {
      var arr2 = new Uint32Array(1);
      (window.crypto && window.crypto.getRandomValues) ? window.crypto.getRandomValues(arr2)
                                                        : (arr2[0] = Math.floor(Math.random() * 4294967296));
      var k = arr2[0] % (j + 1);
      var t = out[j]; out[j] = out[k]; out[k] = t;
    }
    return out.join('');
  }

  function accountsAdmin() {
    return {
      users: [], total: 0, source: 'live', err: '', loading: false,
      query: '',
      createModal: { open: false, name: '', displayName: '', password: '', role: 'viewer', err: '', busy: false },
      pwModal: { open: false, name: '', password: '', err: '', busy: false },
      confirm: { open: false, stage: 1, title: '', text: '', kind: '', user: null, hard: false, err: '', busy: false },

      get filtered() {
        var q = String(this.query || '').trim().toLowerCase();
        if (!q) { return this.users; }
        return this.users.filter(function (u) {
          return String(u.name || '').toLowerCase().indexOf(q) >= 0 ||
                 String(u.displayName || '').toLowerCase().indexOf(q) >= 0;
        });
      },

      boot: function () { this.load(); },

      _headers: function () {
        return Object.assign({ 'Content-Type': 'application/json' },
                             (window.AdminGate && AdminGate.adminHeaders()) || {});
      },

      _readErr: function (r) {
        var self = this;
        return r.json().catch(function () { return {}; }).then(function (j) {
          return { status: r.status, body: j, msg: humanError(j) };
        }).catch(function () { return { status: r.status, body: null, msg: humanError(null) }; });
      },

      load: function () {
        var self = this;
        this.loading = true; this.err = '';
        return fetch(API.users, { headers: this._headers(), cache: 'no-store' })
          .then(function (r) { return self._readErr(r); })
          .then(function (out) {
            self.loading = false;
            if (out.status !== 200) { self.err = out.msg; self.users = []; return; }
            self.users = out.body.users || [];
            self.total = out.body.total || self.users.length;
            self.source = 'live';
          })
          .catch(function (err) {
            self.loading = false;
            self.err = '账号服务不可达：' + (err && err.message ? err.message : err) + '——稍后重试。';
            self.users = [];
          });
      },

      openCreate: function () {
        this.createModal = { open: true, name: '', displayName: '', password: tempPassword(), role: 'viewer', err: '', busy: false };
      },
      submitCreate: function () {
        var self = this; var m = this.createModal;
        if (!m.name.trim()) { m.err = '请输入账号。'; return; }
        if (m.password.length < 6) { m.err = '初始密码至少 6 位。'; return; }
        m.busy = true; m.err = '';
        fetch(API.users, { method: 'POST', headers: this._headers(),
                           body: JSON.stringify({ name: m.name.trim(), displayName: m.displayName.trim(),
                                                  password: m.password, role: m.role }) })
          .then(function (r) { return self._readErr(r); })
          .then(function (out) {
            m.busy = false;
            if (out.status !== 200) { m.err = out.msg; return; }
            m.open = false;
            return self.load();
          })
          .catch(function (err) { m.busy = false; m.err = '提交失败：' + (err && err.message); });
      },

      askToggle: function (u) {
        this.confirm = { open: true, stage: 1, kind: 'toggle', user: u, hard: false, err: '', busy: false,
                         title: (u.isForbidden ? '启用' : '禁用') + '账号「' + u.name + '」',
                         text: u.isForbidden ? '启用后该账号可重新登录平台。'
                                             : '禁用后该账号立即无法登录（Casdoor 侧拒绝）。' };
      },
      askDelete: function (u) {
        this.confirm = { open: true, stage: 1, kind: 'delete', user: u, hard: false, err: '', busy: false,
                         title: '删除账号「' + u.name + '」',
                         text: '默认软删（保留记录、立即禁止登录）；勾选「物理删」将不可恢复。' };
      },
      runConfirm: function () {
        var self = this; var c = this.confirm; var u = c.user;
        if (c.stage === 1) { c.stage = 2; return; }
        c.busy = true; c.err = '';
        var url, payload;
        if (c.kind === 'toggle') {
          url = API.user + encodeURIComponent(u.name) + '/status';
          payload = { forbidden: !u.isForbidden };
        } else {
          url = API.user + encodeURIComponent(u.name) + '/delete';
          payload = { hard: !!c.hard };
        }
        fetch(url, { method: 'POST', headers: this._headers(), body: JSON.stringify(payload) })
          .then(function (r) { return self._readErr(r); })
          .then(function (out) {
            c.busy = false;
            if (out.status !== 200) { c.err = out.msg; return; }
            c.open = false;
            return self.load();
          })
          .catch(function (err) { c.busy = false; c.err = '操作失败：' + (err && err.message); });
      },

      askResetPw: function (u) {
        this.pwModal = { open: true, name: u.name, password: tempPassword(), err: '', busy: false };
      },
      submitResetPw: function () {
        var self = this; var m = this.pwModal;
        if (m.password.length < 6) { m.err = '临时密码至少 6 位。'; return; }
        m.busy = true; m.err = '';
        fetch(API.user + encodeURIComponent(m.name) + '/password',
              { method: 'POST', headers: this._headers(), body: JSON.stringify({ password: m.password }) })
          .then(function (r) { return self._readErr(r); })
          .then(function (out) {
            m.busy = false;
            if (out.status !== 200) { m.err = out.msg; return; }
          })
          .catch(function (err) { m.busy = false; m.err = '重置失败：' + (err && err.message); });
      },
      copyTempPw: function () {
        var self = this;
        var done = function () { self.pwModal.err = ''; self.__copied = true; setTimeout(function () { self.__copied = false; }, 1500); };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(this.pwModal.password).then(done).catch(function () { done(); });
        } else { done(); }
      },

      setRole: function (u, role) {
        if (!role || role === u.role) { return; }
        var self = this;
        u.__roleErr = '';
        fetch(API.user + encodeURIComponent(u.name) + '/role',
              { method: 'POST', headers: this._headers(), body: JSON.stringify({ role: role }) })
          .then(function (r) { return self._readErr(r); })
          .then(function (out) {
            if (out.status !== 200) { u.__roleErr = out.msg; return; }
            return self.load();
          })
          .catch(function (err) { u.__roleErr = '设置失败：' + (err && err.message); });
      },

      /* ── 可测纯函数挂载（window.O5，selftest/单测直调口） ── */
      roleLabel: roleLabel, humanError: humanError, tempPassword: tempPassword,
    };
  }

  window.O5 = { ROLES: ROLES, roleLabel: roleLabel, humanError: humanError, tempPassword: tempPassword };
  window.accountsAdmin = accountsAdmin;
})();
