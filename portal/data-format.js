/* ============================================================
 * OneServe 门户 · 数字/时间格式化纯函数（零依赖 · 浏览器与 Node 双端可测）
 * 规则来源：门户修订包-议题二 v1.1「必须级细节表」：
 *   - ≥1亿 → x.xx亿；≥1万 → x.x万；其余千分位；禁 count-up
 *   - 时间相对化：刚刚 / N 分钟前 / N 小时前 / 昨天 HH:MM / MM-DD HH:MM
 *   - 数据缺失显示「—」，禁止假 0
 * 自测：selftest.html（断言页）或 `node -e "const f=require('./portal/data-format.js'); ..."`
 * ============================================================ */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) { module.exports = api; }   // Node 自测
  else { root.Fmt = api; }                                                       // 浏览器全局
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* 千分位（整数；缺失/非法 → '—'，绝不渲染假 0） */
  function formatThousands(v) {
    if (v === null || v === undefined || !isFinite(v)) return '\u2014';
    var n = Math.round(Number(v));
    var sign = n < 0 ? '-' : '';
    return sign + Math.abs(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  /* 紧凑格式：≥1亿 x.xx亿 ／ ≥1万 x.x万 ／ 其余千分位（单色大数字用） */
  function formatCompact(v) {
    if (v === null || v === undefined || !isFinite(v)) return '\u2014';
    var n = Number(v);
    if (n < 0) return '-' + formatCompact(-n);
    if (n >= 1e8) return trimFixed(n / 1e8, 2) + '\u4ebf';   // 亿
    if (n >= 1e4) return trimFixed(n / 1e4, 1) + '\u4e07';   // 万
    return formatThousands(n);
  }

  /* toFixed 但去掉科学计数法风险；保留指定位数（规则要求 x.x / x.xx 固定位） */
  function trimFixed(x, digits) {
    var s = x.toFixed(digits);
    return s === '-0' || /^-0(\.0+)?$/.test(s) ? s.slice(1) : s;
  }

  /* 百分比：0.996 → '99.6%'（digits 默认 1） */
  function formatPercent(ratio, digits) {
    if (ratio === null || ratio === undefined || !isFinite(ratio)) return '\u2014';
    var d = digits === undefined ? 1 : digits;
    return (Number(ratio) * 100).toFixed(d) + '%';
  }

  /* 时间相对化：now 可注入（断言页/测试用），缺省取当前时间 */
  function formatRelativeTime(input, now) {
    var t = parseTime(input);
    if (t === null) return '\u2014';
    var ref = (now === undefined || now === null) ? Date.now() : parseTime(now);
    if (ref === null) return '\u2014';
    var diff = ref - t;                      // 毫秒；只处理过去/未来±1天内按过去语义
    if (diff < 0) diff = 0;
    var sec = Math.floor(diff / 1000);
    if (sec < 60) return '\u521a\u521a';                                    // 刚刚
    if (sec < 3600) return Math.floor(sec / 60) + ' \u5206\u949f\u524d';   // N 分钟前
    if (sec < 86400) return Math.floor(sec / 3600) + ' \u5c0f\u65f6\u524d'; // N 小时前
    var d = new Date(t);
    var hhmm = pad2(d.getHours()) + ':' + pad2(d.getMinutes());
    if (sec < 172800) return '\u6628\u5929 ' + hhmm;                        // 昨天 HH:MM
    return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + hhmm;     // MM-DD HH:MM
  }

  /* HH:mm:ss（「上次更新」用） */
  function formatClock(input) {
    var d = input instanceof Date ? input : new Date(input);
    if (isNaN(d.getTime())) return '\u2014';
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function parseTime(input) {
    if (input instanceof Date) return isNaN(input.getTime()) ? null : input.getTime();
    if (typeof input === 'number' && isFinite(input)) return input;
    if (typeof input === 'string') {
      var t = Date.parse(input);
      return isNaN(t) ? null : t;
    }
    return null;
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  return {
    formatThousands: formatThousands,
    formatCompact: formatCompact,
    formatPercent: formatPercent,
    formatRelativeTime: formatRelativeTime,
    formatClock: formatClock
  };
});
