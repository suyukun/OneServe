#!/usr/bin/env node
// gen_fig.mjs —— 说明书图形共用渲染库（F1/F7/F9/F10）
// 约束：Node 内置模块零第三方依赖；手写 SVG 字符串模板；数据（figures/*.yml）与样式（本文件 THEME）分离。
// 视觉规范（四图统一）：白底；大地色低饱和（区域底色米黄/浅橙/蓝灰）；白色条目盒；
//   颜色只编码层级不编码重要性；包含关系替代连线（组内禁箭头）；一盒一事；
//   「术语：说明」句式；严格网格（同行等宽等距、留白≈盒内边距）；字号三级同级严格一致；
//   形状语义：直角=条目、圆角=重点、虚线=边界。
// 用法：单图入口（figures/fX_*.mjs）import 本库 + 读同名 yml，产出 figures/fX_*.svg。

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

// ---------- 主题（样式唯一真源；改样式只改这里） ----------
export const THEME = {
  bg: '#FFFFFF',
  ink: '#3B3833',        // 主文字（暖深灰）
  sub: '#6E6759',        // 次文字
  faint: '#9B9384',      // 元数据/辅助
  zone: {
    beige: '#F4EEDF',    // 米黄（区域底色①）
    orange: '#F6E4CB',   // 浅橙（区域底色②）
    slate: '#E3E8EE',    // 蓝灰（区域底色③）
  },
  itemFill: '#FFFFFF',
  itemStroke: '#B7AF9F',
  focusFill: '#FDFBF4',  // 圆角重点盒底
  focusStroke: '#8C8577',
  dashStroke: '#8C8577', // 虚线=边界
  arrow: '#6E6759',
  hairline: '#D8D2C4',
  font: "'PingFang SC','Hiragino Sans GB','Microsoft YaHei','Noto Sans CJK SC',sans-serif",
  fs: { title: 30, section: 17, item: 13.5, meta: 11.5 },  // 字号三级＋元数据
  pad: 22,               // 盒内边距（网格留白≈盒内边距）
  gap: 22,               // 网格间距
  lineH: 19,             // 条目行高
};

// ---------- 极简 YAML 子集解析器（缩进块、映射、列表、标量、注释） ----------
// 支持形式：key: value / key: 块 / - 项 / - key: value 起头的列表内映射；值可单引号包裹。
// 列表内映射的键必须是 ASCII 标识符：值里的半角冒号（tags:表:版本:键）不构成键值分隔。
export function parseYaml(src) {
  const raw = src.split('\n').map((l, n) => ({ text: l.replace(/\t/g, '  '), n: n + 1 }))
    .filter(l => l.text.trim() !== '' && !/^\s*#/.test(l.text));
  let i = 0;
  const indentOf = (s) => { const m = s.match(/^ */); return m ? m[0].length : 0; };
  const scalar = (v) => {
    v = v.trim();
    if (v.startsWith("'") && v.endsWith("'") && v.length >= 2) return v.slice(1, -1).replace(/''/g, "'");
    if (v.startsWith('"') && v.endsWith('"') && v.length >= 2) return v.slice(1, -1);
    const h = v.indexOf(' #');
    return (h >= 0 ? v.slice(0, h) : v).trim();
  };
  function parseBlock(minIndent) {
    if (i >= raw.length) return null;
    const cur = indentOf(raw[i].text);
    if (cur < minIndent) return null;
    if (/^\s*- /.test(raw[i].text)) return parseList(cur);
    return parseMap(cur);
  }
  function parseMap(indent) {
    const out = {};
    while (i < raw.length) {
      const line = raw[i].text;
      const cur = indentOf(line);
      if (cur < indent) break;
      if (cur > indent) throw new Error('YAML 缩进异常 @line ' + raw[i].n + ': ' + line);
      if (/^- /.test(line.trim())) throw new Error('YAML 列表/映射混排 @line ' + raw[i].n);
      const m = line.match(/^([^:]+):\s*(.*)$/);
      if (!m) throw new Error('YAML 键解析失败 @line ' + raw[i].n + ': ' + line);
      const key = m[1].trim();
      const rest = m[2];
      i++;
      if (rest.trim() === '') {
        if (i < raw.length && indentOf(raw[i].text) > indent) out[key] = parseBlock(indent + 1);
        else if (i < raw.length && /^\s*- /.test(raw[i].text) && indentOf(raw[i].text) === indent) out[key] = parseList(indent);
        else out[key] = null;
      } else {
        out[key] = scalar(rest);
      }
    }
    return out;
  }
  function parseList(indent) {
    const arr = [];
    while (i < raw.length) {
      const line = raw[i].text;
      const cur = indentOf(line);
      if (cur !== indent || !/^\s*- /.test(line)) break;
      const body = line.replace(/^\s*- /, '');
      // 列表内映射只认 ASCII 标识符键（name:/steps: 等）；
      // 「术语：说明」整句条目（含半角冒号，如「键空间：tags:表:版本:键」）按标量处理，不再误拆成对象
      const bodyMap = body.match(/^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$/);
      if (bodyMap && !body.trim().startsWith("'") && !body.trim().startsWith('"')) {
        // 列表内映射：首键在当前行，后续兄弟键在缩进 cur+2 消费进同一项
        const itemIndent = cur + 2;
        const item = {};
        if (bodyMap[2].trim() === '') {
          i++;
          if (i < raw.length) {
            const ni = indentOf(raw[i].text);
            if (/^\s*- /.test(raw[i].text) && ni >= itemIndent - 2) {
              item[bodyMap[1].trim()] = parseList(ni);          // 键下嵌套列表（列表项内）
            } else if (ni >= itemIndent) {
              Object.assign(item, parseMap(ni));
            } else item[bodyMap[1].trim()] = null;
          } else item[bodyMap[1].trim()] = null;
        } else {
          item[bodyMap[1].trim()] = scalar(bodyMap[2]);
          i++;
        }
        if (i < raw.length) {
          const ni = indentOf(raw[i].text);
          if (ni >= itemIndent && !/^\s*- /.test(raw[i].text)) Object.assign(item, parseMap(ni));
        }
        arr.push(item);
      } else {
        arr.push(scalar(body));
        i++;
      }
    }
    return arr;
  }
  const doc = parseBlock(0);
  if (i < raw.length) throw new Error('YAML 尾部未消费 @line ' + raw[i].n + ': ' + raw[i].text);
  return doc;
}

// 数据源便捷读取：与本入口同目录的 <name>.yml
export function loadYaml(entryUrl, name) {
  const dir = dirname(fileURLToPath(entryUrl));
  return parseYaml(readFileSync(join(dir, name + '.yml'), 'utf8'));
}

// ---------- 文本度量（CJK 1em，ASCII 约 0.5em；估算口径） ----------
const CJK = /[\u2E80-\u9FFF\uF900-\uFAFF\uFF00-\uFFEF\u3000-\u303F\u2014\u2018-\u201D\u00B7\u2192\u2264]/;
export function textW(s, fs) {
  let w = 0;
  for (const ch of String(s)) {
    if (CJK.test(ch)) w += 1.0;
    else if (/[A-Z0-9]/.test(ch)) w += 0.62;
    else if (/[a-z]/.test(ch)) w += 0.52;
    else if (ch === ' ') w += 0.3;
    else w += 0.42;
  }
  return w * fs;
}
export function wrap(s, fs, maxW) {
  const out = [];
  for (const seg of String(s).split('\n')) {
    let line = '';
    for (const ch of seg) {
      if (textW(line + ch, fs) > maxW && line !== '') { out.push(line); line = ch; }
      else line += ch;
    }
    out.push(line);
  }
  return out;
}

// ---------- SVG 文档构建器 ----------
export function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
export class Doc {
  constructor(w, h) {
    this.w = w; this.h = h; this.buf = [];
  }
  add(s) { this.buf.push(s); return this; }
  rect(o) {
    const {
      x, y, w, h, fill = THEME.itemFill, stroke = THEME.itemStroke, sw = 1.2,
      rx = 0, dash = null, opacity = 1,
    } = o;
    return this.add(`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}"${rx ? ` rx="${rx}"` : ''}${dash ? ` stroke-dasharray="${dash}"` : ''} opacity="${opacity}"/>`);
  }
  zone(o) { return this.rect({ ...o, fill: o.fill ?? THEME.zone[o.color] ?? THEME.zone.beige, stroke: o.stroke ?? 'none', sw: 0 }); }
  text(o) {
    const {
      x, y, fs = THEME.fs.item, fill = THEME.ink, anchor = 'start', weight = 400,
      lines = null, lh = THEME.lineH, ls = 0,
    } = o;
    const arr = lines ?? [o.t ?? ''];
    const spans = arr.map((l, k) => `<tspan x="${x}" dy="${k === 0 ? 0 : lh}">${esc(l)}</tspan>`).join('');
    return this.add(`<text font-family="${THEME.font}" font-size="${fs}" fill="${fill}" text-anchor="${anchor}" font-weight="${weight}" letter-spacing="${ls}" x="${x}" y="${y}">${spans}</text>`);
  }
  line(x1, y1, x2, y2, stroke = THEME.hairline, sw = 1, dash = null) {
    return this.add(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${stroke}" stroke-width="${sw}"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`);
  }
  arrow(x1, y1, x2, y2, o = {}) {
    const { stroke = THEME.arrow, sw = 1.6, label = null, labelFs = 11.5 } = o;
    const ang = Math.atan2(y2 - y1, x2 - x1);
    const ah = 9;
    const p1 = `${x2 - ah * Math.cos(ang - 0.42)},${y2 - ah * Math.sin(ang - 0.42)}`;
    const p2 = `${x2 - ah * Math.cos(ang + 0.42)},${y2 - ah * Math.sin(ang + 0.42)}`;
    this.add(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${stroke}" stroke-width="${sw}"/>`);
    this.add(`<polygon points="${x2},${y2} ${p1} ${p2}" fill="${stroke}"/>`);
    if (label) {
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      this.text({ x: mx, y: my - 7, fs: labelFs, fill: THEME.sub, anchor: 'middle', t: label });
    }
    return this;
  }
  toString() {
    return `<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="${this.w}" height="${this.h}" viewBox="0 0 ${this.w} ${this.h}">\n<rect width="${this.w}" height="${this.h}" fill="${THEME.bg}"/>\n${this.buf.join('\n')}\n</svg>\n`;
  }
}

// ---------- 图题（特大）＋ 元数据块（右下角） ----------
export function figTitle(doc, t, sub = null) {
  doc.text({ x: 40, y: 58, fs: THEME.fs.title, weight: 700, t });
  if (sub) doc.text({ x: 40, y: 84, fs: THEME.fs.meta, fill: THEME.sub, t: sub });
}
export function metaBlock(doc, figId, status, sourceName, W, H) {
  const date = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' });
  const t = `图号 ${figId}｜状态 ${status}｜数据源 ${sourceName}｜生成日期 ${date}`;
  const w = textW(t, THEME.fs.meta);
  doc.line(W - 40 - w, H - 44, W - 40, H - 44, THEME.hairline, 1);
  doc.text({ x: W - 40, y: H - 26, fs: THEME.fs.meta, fill: THEME.faint, anchor: 'end', t });
  return date;
}

// 白色条目盒：直角=条目（默认）；rounded=true 圆角=重点；返回盒高
export function itemBox(doc, o) {
  const {
    x, y, w, text: txt, fs = THEME.fs.item, rounded = false, weight = 400, align = 'left', pad = 12,
  } = o;
  const inner = w - pad * 2;
  const lines = wrap(txt, fs, inner);
  const h = lines.length * THEME.lineH + (pad - 4) * 2;
  doc.rect({
    x, y, w, h,
    fill: rounded ? THEME.focusFill : THEME.itemFill,
    stroke: rounded ? THEME.focusStroke : THEME.itemStroke,
    rx: rounded ? 9 : 0,
    sw: rounded ? 1.5 : 1.2,
  });
  const anchor = align === 'center' ? 'middle' : 'start';
  const tx = align === 'center' ? x + w / 2 : x + pad;
  doc.text({
    x: tx, y: y + pad + fs - 5, fs, weight, anchor,
    lines,
  });
  return h;
}
