/* ============================================================
 * 版本管理页组件（versions.js · P3 2026-10-08）
 * Redis 版本空间管理（系统管理员定期清理）：列表＋按空间物理删除（两段式确认）。
 *
 * 数据面（本页后端，P3 新增）：
 *   GET  /v1/admin/versions            版本空间列表（SCAN tags:* 聚合；每行
 *                                      isLive/registered/verMeta/schemaMeta）
 *   POST /v1/admin/versions/delete     删除一个空间 {namespace, version}
 *                                      线上版 → 409 PROTECTED_LIVE_VERSION（后端兜底闸）
 * 加载序：admin-gate.js → 本文件 → alpine.min.js（硬规矩）。
 * 诚信线：读失败＝如实错误条＋重试（不回落假清单）；删除失败＝行内错误，
 * 不假装成功；线上版按钮禁用＋tooltip（前端闸），后端 409 兜底。
 * ============================================================ */
(function (root) {
  'use strict';

  /* ── 纯函数（状态分类；供机检/走查直调 VERSMGR） ── */
  /* 行状态：线上·保护（latest 指针指向）＞孤儿（无服务登记）＞可删。 */
  function statusOf(s) {
    var o = s || {};
    if (o.isLive) return '线上·保护';
    return o.registered ? '可删' : '孤儿';
  }

  /* 键数下方元信息行：ver/schema 元信息在场性（缺＝如实标注）。 */
  function metaLine(s) {
    var o = s || {};
    var parts = [];
    parts.push(o.verMeta ? 'ver ✓' : 'ver ✗');
    parts.push(o.schemaMeta ? 'schema ✓' : 'schema ✗');
    return parts.join(' · ');
  }

  function deleteFailText(err) {
    var e = err || {};
    if (e.code === 'PROTECTED_LIVE_VERSION') return e.message || '线上版不可删除；如需淘汰请先装载新版本并切换上线';
    return '删除没有完成（' + String(e.message || '原因未知') + '）。请重试，或联系平台管理员。';
  }

  root.VERSMGR = { statusOf: statusOf, metaLine: metaLine, deleteFailText: deleteFailText };

  root.versionsConsole = function () {
    return {
      /* 列表 */
      spaces: [], totalKeys: 0, busy: false, error: null,
      /* 删除（两段式确认：confirming＝待确认行号，-1＝无） */
      confirming: -1, deleting: false, deleteError: '', doneNote: '',

      /* ── 取数基础设施（与 published.js 同款：fail-closed 如实错误） ── */
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

      async boot() { await this.load(); },

      /* ── 列表（GET /v1/admin/versions） ── */
      async load() {
        this.busy = true; this.error = null;
        var res = await this.apiFetch('/v1/admin/versions');
        this.busy = false;
        if (res.ok) {
          var b = res.body || {};
          this.spaces = (b.spaces || []).map(function (s) {
            return {
              namespace: String(s.namespace || ''), version: String(s.version || ''),
              keys: Number(s.keys || 0), isLive: !!s.isLive,
              registered: s.registered !== false, verMeta: !!s.verMeta, schemaMeta: !!s.schemaMeta
            };
          });
          this.totalKeys = Number(b.totalKeys || 0);
          return true;
        }
        var msg = (res.body && res.body.message) || '';
        this.spaces = []; this.totalKeys = 0;
        this.error = {
          title: '暂时读不到版本空间清单',
          message: (msg && msg !== '网络连不上服务端' ? msg + '。' : '')
            + '请稍后重试；若持续出现，请联系平台管理员。'
        };
        return false;
      },

      statusOf: statusOf,
      metaLine: metaLine,

      /* ── 删除（两段式确认：① 点删除 → 确认条显示将删键数；② 二次点确认执行） ── */
      askDelete(i) {
        var s = this.spaces[i];
        if (!s || s.isLive) return;           /* 线上版：按钮已禁用，双保险不进入确认 */
        this.deleteError = '';
        this.confirming = i;
      },
      cancelDelete() {
        if (this.deleting) return;
        this.confirming = -1; this.deleteError = '';
      },
      async confirmDelete() {
        var s = this.spaces[this.confirming];
        if (!s) { this.confirming = -1; return; }
        this.deleting = true; this.deleteError = '';
        var res = await this.apiFetch('/v1/admin/versions/delete', {
          method: 'POST',
          body: JSON.stringify({ namespace: s.namespace, version: s.version, operator: 'console' })
        });
        this.deleting = false;
        if (res.ok) {
          var n = Number((res.body || {}).deletedKeys || 0);
          this.doneNote = '已删除 ' + s.namespace + ':' + s.version + '（' + n.toLocaleString() + ' 键；不可恢复）';
          this.confirming = -1;
          await this.load();                  /* 删后刷新（键数/总键数随之更新） */
          return;
        }
        var err = (res.body || {});
        this.deleteError = root.VERSMGR.deleteFailText(Object.assign({ status: res.status }, err));
        if (err.error === 'PROTECTED_LIVE_VERSION') {
          this.confirming = -1;               /* 指针已切走（列表过期）：退出确认并刷新 */
          await this.load();
        }
      }
    };
  };
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
