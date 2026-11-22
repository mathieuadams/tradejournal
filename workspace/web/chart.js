// Canvas chart engine. No libraries.
//   wheel = zoom time around the cursor (wheel over the price scale zooms price)
//   drag in a pane = pan time and price, drag the price scale = zoom price, drag the time scale = zoom time
//   double-click the price scale = auto scale, double-click the chart = reset
// Panes: the price pane plus one per non-overlay indicator. Indicators are pine.js results.
// Drawings are stored in time / price so they survive timeframe changes.

const C = {
  bg: '#131722', grid: 'rgba(42,46,57,0.55)', axis: '#787b86', text: '#d1d4dc', up: '#089981', dn: '#f23645', cross: '#9598a1',
  sep: '#2a2e39', wick: null, tagText: '#ffffff', water: 'rgba(120,123,134,0.08)', sel: '#2962ff',
};
const AXIS_MIN = 64, TIME_H = 28, FONT = '11px Inter, system-ui, sans-serif', MONO = '11px "JetBrains Mono", ui-monospace, monospace';
const SIZE_PX = { tiny: 7, small: 10, normal: 13, large: 17, huge: 24, auto: 9 };
const LABEL_FONT = { tiny: 9, small: 10, normal: 11, large: 14, huge: 18, auto: 10 };

let NYF = null; const tpCache = new Map();
export function nyParts(t) {
  let r = tpCache.get(t); if (r) return r;
  NYF = NYF || new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hourCycle: 'h23', year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', weekday: 'short' });
  const o = {}; for (const p of NYF.formatToParts(new Date(t * 1000))) o[p.type] = p.value;
  r = { y: +o.year, mon: o.month, d: +o.day, h: +o.hour, mi: +o.minute, wd: o.weekday };
  if (tpCache.size > 200000) tpCache.clear(); tpCache.set(t, r); return r;
}
export function fmtPrice(p, prec = 2) { if (p == null || !Number.isFinite(p)) return '—'; return p.toLocaleString('en-US', { minimumFractionDigits: prec, maximumFractionDigits: prec }); }
export function fmtVol(v) { if (!Number.isFinite(v)) return '—'; const a = Math.abs(v); return a >= 1e9 ? (v / 1e9).toFixed(2) + 'B' : a >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : a >= 1e3 ? (v / 1e3).toFixed(2) + 'K' : v.toFixed(0); }
function niceStep(range, n) { const raw = range / Math.max(1, n), mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag; return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag; }
const finite = Number.isFinite;
const dist2seg = (px, py, x1, y1, x2, y2) => { const dx = x2 - x1, dy = y2 - y1, L = dx * dx + dy * dy; let t = L ? ((px - x1) * dx + (py - y1) * dy) / L : 0; t = Math.max(0, Math.min(1, t)); return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)); };
let uid = 0; export const newId = p => (p || 'd') + Date.now().toString(36) + (uid++).toString(36);

export class Chart {
  constructor(host, opts = {}) {
    this.host = host; this.opts = opts;
    host.classList.add('chart-host');
    this.cv = document.createElement('canvas'); this.cv.className = 'chart-canvas'; this.cv.tabIndex = 0; host.appendChild(this.cv);
    this.legendEl = document.createElement('div'); this.legendEl.className = 'chart-legends'; host.appendChild(this.legendEl);
    this.ctx = this.cv.getContext('2d');
    this.bars = []; this.ind = []; this.drawings = []; this.orderLines = []; this.paneState = new Map([['main', { auto: true, w: 1 }]]);
    this.view = { a: 0, b: 100 }; this.chartType = 'candles'; this.tool = 'cursor'; this.replay = null; this.prec = 2;
    this.symbol = ''; this.tf = '1h'; this.tfSec = 3600; this.hover = null; this.sel = null; this.live = true; this.magnet = false;
    this.bind();
    this.ro = new ResizeObserver(() => this.req()); this.ro.observe(host);
  }
  destroy() { this.ro.disconnect(); this.host.innerHTML = ''; }
  req() { if (this._raf) return; this._raf = requestAnimationFrame(() => { this._raf = 0; this.draw(); }); }

  // ---------------------------------------------------------------- data
  setSymbol(symbol, tf, tfSec, prec) { this.symbol = symbol; this.tf = tf; this.tfSec = tfSec; this.prec = prec ?? this.prec; this.buildLegend(); }
  setBars(bars, { fit = false, keepView = false } = {}) {
    this.bars = bars; this._ha = null;
    if (fit || !keepView) this.fit(); else this.req();
  }
  prepend(n) { this.view.a += n; this.view.b += n; this._ha = null; this.req(); }
  liveUpdate(newBar) {
    this._ha = null; const n = this.count();
    if (newBar && this.view.b >= n - 2 && this.view.b <= n + 30) { this.view.a += 1; this.view.b += 1; }
    this.req(); this.updateLegendValues();
  }
  count() { return this.replay != null ? Math.min(this.replay + 1, this.bars.length) : this.bars.length; }
  fit(span) { const n = this.count(); const w = span || Math.min(Math.max(n, 20), this.opts.span || 150); this.view = { a: n - w, b: n + 5 }; for (const s of this.paneState.values()) s.auto = true; this.req(); }
  showRange(fromT) { const n = this.count(); const i = Math.max(0, Math.floor(this.tToIdx(fromT))); this.view = { a: i - 1, b: n + 3 }; for (const s of this.paneState.values()) s.auto = true; this.req(); this.checkMore(); }
  setIndicators(list) { // [{id, title, out, visible}]
    this.ind = list; const ids = new Set(['main']);
    for (const d of list) if (d.out && !this.isOverlay(d)) { ids.add(d.id); if (!this.paneState.has(d.id)) this.paneState.set(d.id, { auto: true, w: 0.28 }); }
    for (const k of [...this.paneState.keys()]) if (!ids.has(k)) this.paneState.delete(k);
    this.buildLegend(); this.req();
  }
  isOverlay(d) { return !d.out || d.out.meta.overlay || d.forceOverlay; }
  setDrawings(list) { this.drawings = list; if (this.sel && !list.includes(this.sel)) this.sel = null; this.req(); }
  setOrderLines(list) { this.orderLines = list; this.req(); }
  setTool(t) { this.tool = t; this.pending = null; this.cv.style.cursor = t === 'cursor' ? 'default' : 'crosshair'; this.req(); }
  setType(t) { this.chartType = t; this._ha = null; this.req(); }
  setReplay(i) { this.replay = i; this.req(); }

  data() {
    if (this.chartType !== 'ha') return this.bars;
    if (this._ha && this._ha.length === this.bars.length) return this._ha;
    const B = this.bars, H = new Array(B.length);
    for (let i = 0; i < B.length; i++) { const b = B[i], c = (b.o + b.h + b.l + b.c) / 4, o = i ? (H[i - 1].o + H[i - 1].c) / 2 : (b.o + b.c) / 2; H[i] = { t: b.t, o, h: Math.max(b.h, o, c), l: Math.min(b.l, o, c), c, v: b.v }; }
    return (this._ha = H);
  }

  // ---------------------------------------------------------------- geometry
  layout() {
    const last = this.bars.length ? this.bars[this.bars.length - 1].c : 100;
    this.ctx.font = MONO; this.axisW = Math.max(AXIS_MIN, Math.ceil(this.ctx.measureText(fmtPrice(last * 1.5, this.prec)).width) + 18);
    const W = this.host.clientWidth, H = this.host.clientHeight, plotW = Math.max(50, W - this.axisW), total = Math.max(60, H - TIME_H);
    const ids = [...this.paneState.keys()], subs = ids.filter(k => k !== 'main');
    const subW = subs.reduce((s, k) => s + this.paneState.get(k).w, 0), mainW = Math.max(0.35, 1 - Math.min(0.62, subW));
    const scale = (1 - mainW) / (subW || 1);
    let y = 0; const panes = [];
    for (const id of ['main', ...subs]) { const h = Math.round((id === 'main' ? mainW : this.paneState.get(id).w * scale) * total); panes.push({ id, y, h: id === subs[subs.length - 1] ? total - y : h }); y += h; }
    if (!subs.length) panes[0].h = total;
    return { W, H, plotW, total, panes };
  }
  idxToX(i) { return (i - this.view.a) / (this.view.b - this.view.a) * this.L.plotW; }
  xToIdx(x) { return this.view.a + x / this.L.plotW * (this.view.b - this.view.a); }
  barW() { return this.L.plotW / (this.view.b - this.view.a); }
  pY(pane, p) { return pane.y + (pane.max - p) / (pane.max - pane.min) * pane.h; }
  yP(pane, y) { return pane.max - (y - pane.y) / pane.h * (pane.max - pane.min); }
  paneAt(y) { return this.L.panes.find(p => y >= p.y && y < p.y + p.h) || null; }
  tToIdx(t) {
    const B = this.bars, n = B.length; if (!n) return 0;
    if (t <= B[0].t) return (t - B[0].t) / this.tfSec;
    if (t >= B[n - 1].t) return n - 1 + (t - B[n - 1].t) / this.tfSec;
    let lo = 0, hi = n - 1; while (hi - lo > 1) { const m = (lo + hi) >> 1; if (B[m].t <= t) lo = m; else hi = m; }
    return lo + (t - B[lo].t) / ((B[hi].t - B[lo].t) || 1);
  }
  idxToT(i) {
    const B = this.bars, n = B.length; if (!n) return 0;
    if (i <= 0) return B[0].t + i * this.tfSec;
    if (i >= n - 1) return B[n - 1].t + (i - n + 1) * this.tfSec;
    const k = Math.floor(i), f = i - k; return B[k].t + f * (B[k + 1].t - B[k].t);
  }
  visRange() { const n = this.count(); return [Math.max(0, Math.floor(this.view.a)), Math.min(n - 1, Math.ceil(this.view.b))]; }

  scales() {
    const L = this.L, D = this.data(), [i0, i1] = this.visRange();
    for (const pane of L.panes) {
      const st = this.paneState.get(pane.id);
      if (!st.auto && finite(st.min)) { pane.min = st.min; pane.max = st.max; continue; }
      let lo = Infinity, hi = -Infinity;
      if (pane.id === 'main') {
        for (let i = i0; i <= i1; i++) { const b = D[i]; if (!b) continue; if (b.l < lo) lo = b.l; if (b.h > hi) hi = b.h; }
        if (this.chartType === 'line' || this.chartType === 'area') { lo = Infinity; hi = -Infinity; for (let i = i0; i <= i1; i++) { const c = D[i]?.c; if (c < lo) lo = c; if (c > hi) hi = c; } }
      } else {
        const d = this.ind.find(x => x.id === pane.id); const o = d && d.out;
        if (o) {
          for (const p of o.plots) { if (p.display === 'none') continue; for (let i = i0; i <= i1; i++) { const v = p.values[i - (p.offset || 0)]; if (finite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; } } if (p.style === 'columns' || p.style === 'histogram') { lo = Math.min(lo, p.histbase || 0); hi = Math.max(hi, p.histbase || 0); } }
          for (const h of o.hlines) if (finite(h.price)) { lo = Math.min(lo, h.price); hi = Math.max(hi, h.price); }
        }
      }
      if (!finite(lo) || !finite(hi)) { lo = 0; hi = 1; }
      if (hi - lo < 1e-9) { hi += Math.abs(hi) * 0.01 || 1; lo -= Math.abs(lo) * 0.01 || 1; }
      const pad = (hi - lo) * (pane.id === 'main' ? 0.08 : 0.1); pane.min = lo - pad; pane.max = hi + pad;
      if (pane.id === 'main' && this.ind.some(d => d.visible !== false && d.out && d.out.meta.scale === 'none')) pane.min -= (pane.max - pane.min) * 0.12;
      st.min = pane.min; st.max = pane.max;
    }
  }

  // ---------------------------------------------------------------- drawing
  draw() {
    const cv = this.cv, dpr = window.devicePixelRatio || 1, W = this.host.clientWidth, H = this.host.clientHeight;
    if (!W || !H) return;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); cv.style.width = W + 'px'; cv.style.height = H + 'px'; }
    const g = this.ctx; g.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.L = this.layout(); this.scales(); const L = this.L;
    g.fillStyle = C.bg; g.fillRect(0, 0, W, H);
    const tticks = this.timeTicks();
    for (const pane of L.panes) {
      g.save(); g.beginPath(); g.rect(0, pane.y, L.plotW, pane.h); g.clip();
      this.drawGrid(g, pane, tticks);
      if (pane.id === 'main') {
        g.fillStyle = C.water; g.font = '600 56px Inter, system-ui, sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle';
        g.fillText(`${this.symbol} · ${this.tf}`, L.plotW / 2, pane.y + pane.h / 2);
        for (const d of this.ind) if (d.visible !== false && d.out && this.isOverlay(d)) this.drawIndicator(g, pane, d, 'under');
        this.drawSeries(g, pane);
        for (const d of this.ind) if (d.visible !== false && d.out && this.isOverlay(d)) this.drawIndicator(g, pane, d, 'over');
        this.drawUserDrawings(g, pane);
        this.drawOrderLines(g, pane);
        this.drawLastPrice(g, pane);
      } else {
        const d = this.ind.find(x => x.id === pane.id);
        if (d && d.visible !== false && d.out) { this.drawIndicator(g, pane, d, 'under'); this.drawIndicator(g, pane, d, 'over'); }
      }
      g.restore();
      if (pane.y > 0) { g.strokeStyle = C.sep; g.lineWidth = 1; g.beginPath(); g.moveTo(0, pane.y + 0.5); g.lineTo(L.W, pane.y + 0.5); g.stroke(); }
    }
    this.drawAxes(g, tticks);
    this.drawCrosshair(g);
    if (this.loadingMore) { g.fillStyle = C.axis; g.font = FONT; g.textAlign = 'left'; g.fillText('Loading history…', 8, L.total - 10); }
    this.layoutLegends();
  }

  drawGrid(g, pane, tticks) {
    g.strokeStyle = C.grid; g.lineWidth = 1; g.beginPath();
    for (const t of tticks) { const x = Math.round(t.x) + 0.5; g.moveTo(x, pane.y); g.lineTo(x, pane.y + pane.h); }
    for (const p of this.priceTicks(pane)) { const y = Math.round(this.pY(pane, p)) + 0.5; g.moveTo(0, y); g.lineTo(this.L.plotW, y); }
    g.stroke();
  }
  priceTicks(pane) { const step = niceStep(pane.max - pane.min, Math.max(2, pane.h / 48)); const out = []; for (let p = Math.ceil(pane.min / step) * step; p <= pane.max; p += step) out.push(+p.toFixed(10)); return out; }
  timeTicks() {
    const [i0, i1] = this.visRange(), B = this.bars, out = []; if (!B.length) return out;
    const cands = [];
    for (let i = Math.max(1, i0); i <= i1; i++) {
      const a = nyParts(B[i - 1].t), b = nyParts(B[i].t); let lvl = 0, txt;
      if (a.y !== b.y) { lvl = 6; txt = String(b.y); }
      else if (a.mon !== b.mon) { lvl = 5; txt = b.mon; }
      else if (a.d !== b.d) { lvl = this.tfSec >= 86400 ? 3 : 4; txt = String(b.d); }
      else if (a.h !== b.h) { lvl = b.h % 6 === 0 ? 3 : 2; txt = `${String(b.h).padStart(2, '0')}:${String(b.mi).padStart(2, '0')}`; }
      else if (Math.floor(a.mi / 30) !== Math.floor(b.mi / 30)) { lvl = 1; txt = `${String(b.h).padStart(2, '0')}:${String(b.mi).padStart(2, '0')}`; }
      else if (Math.floor(a.mi / 5) !== Math.floor(b.mi / 5)) { lvl = 0; txt = `${String(b.h).padStart(2, '0')}:${String(b.mi).padStart(2, '0')}`; }
      else continue;
      if (this.tfSec >= 86400 && lvl < 3) continue;
      cands.push({ i, x: this.idxToX(i), lvl, txt });
    }
    cands.sort((p, q) => q.lvl - p.lvl || p.i - q.i);
    const minGap = 72;
    for (const c of cands) { if (c.x < 0 || c.x > this.L.plotW) continue; if (out.every(o => Math.abs(o.x - c.x) >= minGap)) out.push(c); }
    return out.sort((p, q) => p.x - q.x);
  }

  drawSeries(g, pane) {
    const D = this.data(), [i0, i1] = this.visRange(), bw = this.barW(), type = this.chartType;
    const bc = new Map(); for (const d of this.ind) if (d.visible !== false && d.out) for (const b of d.out.barcolors) for (let i = i0; i <= i1; i++) if (b.colors[i]) bc.set(i, b.colors[i]);
    if (type === 'line' || type === 'area') {
      g.beginPath(); let first = true;
      for (let i = i0; i <= i1; i++) { const b = D[i]; if (!b) continue; const x = this.idxToX(i), y = this.pY(pane, b.c); if (first) { g.moveTo(x, y); first = false; } else g.lineTo(x, y); }
      g.strokeStyle = '#2962ff'; g.lineWidth = 2; g.stroke();
      if (type === 'area') { g.lineTo(this.idxToX(i1), pane.y + pane.h); g.lineTo(this.idxToX(i0), pane.y + pane.h); g.closePath(); const gr = g.createLinearGradient(0, pane.y, 0, pane.y + pane.h); gr.addColorStop(0, 'rgba(41,98,255,0.35)'); gr.addColorStop(1, 'rgba(41,98,255,0)'); g.fillStyle = gr; g.fill(); }
      return;
    }
    const body = Math.max(1, Math.min(bw * 0.72, bw - 1)), groups = new Map();
    const grp = (col) => { let p = groups.get(col); if (!p) { p = { wick: new Path2D(), body: new Path2D(), hollow: new Path2D() }; groups.set(col, p); } return p; };
    for (let i = i0; i <= i1; i++) {
      const b = D[i]; if (!b) continue; const up = b.c >= b.o, col = bc.get(i) || (up ? C.up : C.dn), p = grp(col);
      const x = Math.round(this.idxToX(i)) + 0.5, yh = this.pY(pane, b.h), yl = this.pY(pane, b.l), yo = this.pY(pane, b.o), yc = this.pY(pane, b.c);
      if (type === 'bars') { p.wick.moveTo(x, yh); p.wick.lineTo(x, yl); p.wick.moveTo(x - body / 2, yo); p.wick.lineTo(x, yo); p.wick.moveTo(x, yc); p.wick.lineTo(x + body / 2, yc); continue; }
      p.wick.moveTo(x, yh); p.wick.lineTo(x, Math.min(yo, yc)); p.wick.moveTo(x, Math.max(yo, yc)); p.wick.lineTo(x, yl);
      const top = Math.min(yo, yc), h = Math.max(1, Math.abs(yc - yo));
      if (type === 'hollow' && up) p.hollow.rect(Math.round(x - body / 2) + 0.5, Math.round(top) + 0.5, Math.max(1, Math.round(body) - 1), Math.max(1, Math.round(h) - 1));
      else p.body.rect(Math.round(x - body / 2), Math.round(top), Math.max(1, Math.round(body)), Math.max(1, Math.round(h)));
    }
    for (const [col, p] of groups) { g.strokeStyle = col; g.lineWidth = type === 'bars' ? Math.max(1, Math.min(2, bw / 6)) : 1; g.stroke(p.wick); g.fillStyle = col; g.fill(p.body); g.stroke(p.hollow); }
  }

  // indicator outputs ---------------------------------------------------------
  drawIndicator(g, pane, d, layer) {
    const o = d.out, [i0, i1] = this.visRange(), bw = this.barW();
    const ownScale = pane.id === 'main' && o.meta.scale === 'none';
    let sp = pane;
    if (ownScale) { // volume-style: bottom 18% of the price pane, own scale
      let hi = 0; for (const p of o.plots) for (let i = i0; i <= i1; i++) { const v = p.values[i]; if (finite(v) && v > hi) hi = v; }
      sp = { y: pane.y + pane.h * 0.82, h: pane.h * 0.18, min: 0, max: hi || 1, id: 'own' };
    }
    const Y = v => this.pY(sp, v);
    if (layer === 'under') {
      for (const bg of o.bgcolors) for (let i = i0; i <= i1; i++) { const c = bg.colors[i - (bg.offset || 0)]; if (!c) continue; const x = this.idxToX(i); g.fillStyle = c; g.fillRect(x - bw / 2, pane.y, bw + 0.5, pane.h); }
      for (const f of o.fills) this.drawFill(g, sp, o, f, i0, i1);
      for (const b of o.boxes) this.drawBox(g, sp, b);
      for (const p of o.plots) if (p.display !== 'none' && (p.style === 'columns' || p.style === 'histogram' || p.style === 'area')) this.drawPlot(g, sp, p, i0, i1, Y);
      return;
    }
    for (const h of o.hlines) { if (!finite(h.price)) continue; const y = Math.round(Y(h.price)) + 0.5; g.strokeStyle = h.color; g.lineWidth = h.linewidth || 1; g.setLineDash(h.linestyle === 'solid' ? [] : h.linestyle === 'dotted' ? [1, 3] : [4, 4]); g.beginPath(); g.moveTo(0, y); g.lineTo(this.L.plotW, y); g.stroke(); g.setLineDash([]); }
    for (const p of o.plots) if (p.display !== 'none' && !(p.style === 'columns' || p.style === 'histogram' || p.style === 'area')) this.drawPlot(g, sp, p, i0, i1, Y);
    for (const l of o.lines) this.drawPineLine(g, sp, l);
    for (const s of o.shapes) this.drawShapes(g, sp, s, i0, i1);
    for (const l of o.labels) this.drawLabel(g, sp, l);
    if (o.strategy && pane.id === 'main') this.drawTrades(g, pane, o.strategy);
  }
  drawTrades(g, pane, st) {
    const D = this.data(), n = this.count(), a = Math.floor(this.view.a) - 1, b = Math.ceil(this.view.b) + 1;
    const idx = t => this.tToIdx(t), mark = (i, px, dir, label, col, entry) => {
      const k = Math.round(i); if (k < a || k > b) return; const bar = D[k]; const x = this.idxToX(i), y = this.pY(pane, px);
      g.fillStyle = col; g.beginPath(); const s = 5;
      if (dir > 0) { g.moveTo(x, y + 2); g.lineTo(x - s, y + 2 + s * 1.6); g.lineTo(x + s, y + 2 + s * 1.6); } else { g.moveTo(x, y - 2); g.lineTo(x - s, y - 2 - s * 1.6); g.lineTo(x + s, y - 2 - s * 1.6); }
      g.fill(); g.font = '600 10px Inter, sans-serif'; g.textAlign = 'center'; g.textBaseline = dir > 0 ? 'top' : 'bottom';
      const ly = dir > 0 ? Math.max(y + 14, bar ? this.pY(pane, bar.l) + 6 : y + 14) : Math.min(y - 14, bar ? this.pY(pane, bar.h) - 6 : y - 14);
      g.fillText(label, x, ly);
    };
    for (const t of st.trades) {
      const i0 = idx(t.entryTime), i1 = idx(t.exitTime); if (i1 < a || i0 > b) continue;
      const col = t.pnl >= 0 ? '#089981' : '#f23645';
      g.strokeStyle = col; g.setLineDash([2, 3]); g.lineWidth = 1; g.beginPath(); g.moveTo(this.idxToX(i0), this.pY(pane, t.entryPx)); g.lineTo(this.idxToX(i1), this.pY(pane, t.exitPx)); g.stroke(); g.setLineDash([]);
      mark(i0, t.entryPx, t.dir, `${t.dir > 0 ? 'L' : 'S'} ${t.qty}`, t.dir > 0 ? '#2962ff' : '#e040fb');
      mark(i1, t.exitPx, -t.dir, `${t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(0)}`, col);
    }
    for (const l of st.open) { const i0 = idx(l.t); mark(i0, l.px, l.dir, `${l.dir > 0 ? 'L' : 'S'} ${l.qty}`, l.dir > 0 ? '#2962ff' : '#e040fb'); }
    void n;
  }
  drawPlot(g, pane, p, i0, i1, Y) {
    const off = p.offset || 0, bw = this.barW(), V = p.values, cols = p.colors;
    const lo = Math.max(0, Math.floor(this.view.a) - off - 1), hi = Math.min(V.length - 1, Math.ceil(this.view.b) - off + 1); void i0; void i1;
    const style = p.style;
    if (style === 'columns' || style === 'histogram') {
      const base = Y(p.histbase || 0), w = style === 'columns' ? Math.max(1, bw * 0.7) : Math.max(1, Math.min(2, bw * 0.3));
      for (let k = lo; k <= hi; k++) { const v = V[k]; if (!finite(v)) continue; const c = cols ? cols[k] : p.color; if (!c) continue; const x = this.idxToX(k + off), y = Y(v); g.fillStyle = c; g.fillRect(Math.round(x - w / 2), Math.min(y, base), Math.max(1, Math.round(w)), Math.max(1, Math.abs(base - y))); }
      return;
    }
    if (style === 'circles' || style === 'cross') {
      const r = Math.max(1.5, Math.min(3, bw * 0.25)) * (p.linewidth || 1) * 0.8;
      for (let k = lo; k <= hi; k++) { const v = V[k]; if (!finite(v)) continue; const c = cols ? cols[k] : p.color; if (!c) continue; const x = this.idxToX(k + off), y = Y(v); g.fillStyle = c; g.strokeStyle = c;
        if (style === 'circles') { g.beginPath(); g.arc(x, y, r, 0, 7); g.fill(); } else { g.lineWidth = 1; g.beginPath(); g.moveTo(x - r, y); g.lineTo(x + r, y); g.moveTo(x, y - r); g.lineTo(x, y + r); g.stroke(); } }
      return;
    }
    if (style === 'area') {
      g.beginPath(); let open = false, lastX = 0, firstX = 0; const base = pane.y + pane.h;
      const close = () => { if (open) { g.lineTo(lastX, base); g.lineTo(firstX, base); g.closePath(); open = false; } };
      for (let k = lo; k <= hi; k++) { const v = V[k]; if (!finite(v)) { close(); continue; } const x = this.idxToX(k + off), y = Y(v); if (!open) { g.moveTo(x, base); g.lineTo(x, y); firstX = x; open = true; } else g.lineTo(x, y); lastX = x; }
      close(); g.fillStyle = withAlpha(p.color, 0.25); g.fill();
    }
    g.lineWidth = p.linewidth || 1; g.lineJoin = 'round';
    const step = style === 'stepline', brk = style !== 'line' && style !== 'area';
    let prev = null; g.beginPath(); let curCol = null;
    const flush = () => { if (curCol) { g.strokeStyle = curCol; g.stroke(); } g.beginPath(); };
    for (let k = lo; k <= hi; k++) {
      const v = V[k], c = cols ? cols[k] : p.color;
      if (!finite(v) || !c) { if (brk || !c) prev = null; continue; }
      const x = this.idxToX(k + off), y = Y(v);
      if (c !== curCol) { if (prev) { flush(); g.moveTo(prev.x, prev.y); } else flush(); curCol = c; }
      if (!prev) { g.moveTo(x, y); if (step || brk) { g.lineTo(x + 0.01, y); } }
      else if (step) { g.lineTo(x, prev.y); g.lineTo(x, y); }
      else g.lineTo(x, y);
      prev = { x, y };
    }
    flush();
  }
  seriesOf(o, ref) {
    if (ref.ref === 'hline') { const h = o.hlines.find(x => x.id === ref.id); return h ? { at: () => h.price, off: 0 } : null; }
    const p = o.plots.find(x => x.id === ref.id); return p ? { at: k => p.values[k], off: p.offset || 0 } : null;
  }
  drawFill(g, pane, o, f, i0, i1) {
    const A = this.seriesOf(o, f.a), Bs = this.seriesOf(o, f.b); if (!A || !Bs) return;
    const off = A.off, Y = v => this.pY(pane, v);
    if (f.a.ref === 'hline' && f.b.ref === 'hline') { const y1 = Y(A.at()), y2 = Y(Bs.at()); g.fillStyle = f.color; g.fillRect(0, Math.min(y1, y2), this.L.plotW, Math.abs(y2 - y1)); return; }
    const iA = Math.max(i0, Math.floor(this.view.a)) - 1, iB = Math.max(i1, Math.ceil(this.view.b)) + 1;
    for (let i = iA; i < iB; i++) {
      const k = i - off, a0 = A.at(k), a1 = A.at(k + 1), b0 = Bs.at(i - Bs.off), b1 = Bs.at(i + 1 - Bs.off);
      if (![a0, a1, b0, b1].every(finite)) continue;
      const col = f.colors ? f.colors[k] : f.color; if (!col) continue;
      const x0 = this.idxToX(i), x1 = this.idxToX(i + 1);
      g.fillStyle = col; g.beginPath(); g.moveTo(x0, Y(a0)); g.lineTo(x1, Y(a1)); g.lineTo(x1, Y(b1)); g.lineTo(x0, Y(b0)); g.closePath(); g.fill();
    }
  }
  objX(xloc, x) { return xloc === 'bar_time' ? this.idxToX(this.tToIdx(x / 1000)) : this.idxToX(x); }
  drawPineLine(g, pane, l) {
    if (![l.x1, l.y1, l.x2, l.y2].every(finite)) return;
    let x1 = this.objX(l.xloc, l.x1), x2 = this.objX(l.xloc, l.x2), y1 = this.pY(pane, l.y1), y2 = this.pY(pane, l.y2);
    const W = this.L.plotW, dx = x2 - x1, dy = y2 - y1;
    if ((l.extend === 'right' || l.extend === 'both') && dx !== 0) { const t = (W + 10 - x1) / dx; x2 = x1 + dx * t; y2 = y1 + dy * t; } else if ((l.extend === 'right' || l.extend === 'both')) x2 = W + 10;
    if ((l.extend === 'left' || l.extend === 'both') && dx !== 0) { const t = (-10 - x1) / dx; const nx = x1 + dx * t, ny = y1 + dy * t; x1 = nx; y1 = ny; }
    g.strokeStyle = l.color; g.lineWidth = l.width || 1; g.setLineDash(l.style === 'dashed' ? [5, 4] : l.style === 'dotted' ? [1, 3] : []);
    g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke(); g.setLineDash([]);
  }
  drawBox(g, pane, b) {
    if (![b.left, b.top, b.right, b.bottom].every(finite)) return;
    let x1 = this.objX(b.xloc, b.left), x2 = this.objX(b.xloc, b.right); const y1 = this.pY(pane, b.top), y2 = this.pY(pane, b.bottom);
    if (b.extend === 'right' || b.extend === 'both') x2 = this.L.plotW; if (b.extend === 'left' || b.extend === 'both') x1 = 0;
    const x = Math.min(x1, x2), y = Math.min(y1, y2), w = Math.abs(x2 - x1), h = Math.abs(y2 - y1);
    if (b.bgcolor) { g.fillStyle = b.bgcolor; g.fillRect(x, y, w, h); }
    if (b.border_color && b.border_width > 0) { g.strokeStyle = b.border_color; g.lineWidth = b.border_width; g.setLineDash(b.border_style === 'dashed' ? [4, 3] : b.border_style === 'dotted' ? [1, 3] : []); g.strokeRect(x + 0.5, y + 0.5, w, h); g.setLineDash([]); }
    if (b.text) { g.fillStyle = b.text_color || C.text; g.font = `${LABEL_FONT[b.text_size] || 10}px Inter, sans-serif`; g.textAlign = b.text_halign === 'left' ? 'left' : b.text_halign === 'right' ? 'right' : 'center'; g.textBaseline = 'middle'; g.fillText(b.text, b.text_halign === 'left' ? x + 4 : b.text_halign === 'right' ? x + w - 4 : x + w / 2, y + h / 2); }
  }
  drawLabel(g, pane, l) {
    const D = this.data(); if (!finite(l.x)) return;
    const x = this.objX(l.xloc, l.x); if (x < -200 || x > this.L.plotW + 200) return;
    const bi = Math.round(l.xloc === 'bar_time' ? this.tToIdx(l.x / 1000) : l.x), b = D[bi];
    let y = l.yloc === 'abovebar' && b ? this.pY(pane, b.h) - 4 : l.yloc === 'belowbar' && b ? this.pY(pane, b.l) + 4 : this.pY(pane, l.y);
    if (!finite(y)) return;
    const fs = LABEL_FONT[l.size] || 11; g.font = `500 ${fs}px Inter, sans-serif`;
    const lines = String(l.text ?? '').split('\n'), tw = Math.max(...lines.map(s => g.measureText(s).width)), th = lines.length * (fs + 2);
    const st = l.style, transparent = !l.color || /00$/.test(l.color) && l.color.length === 9;
    if (['circle', 'triangleup', 'triangledown', 'xcross', 'cross', 'diamond', 'square', 'flag', 'arrowup', 'arrowdown'].includes(st)) { drawShape(g, st, x, y, SIZE_PX[l.size] || 9, l.color); if (l.text) { g.fillStyle = l.textcolor; g.textAlign = 'center'; g.textBaseline = 'bottom'; g.fillText(l.text, x, y - 8); } return; }
    let bx, by; const pw = tw + 10, ph = th + 4;
    if (st === 'label_down') { bx = x - pw / 2; by = y - ph - 5; }
    else if (st === 'label_up') { bx = x - pw / 2; by = y + 5; }
    else if (st === 'label_left') { bx = x + 5; by = y - ph / 2; }
    else if (st === 'label_right') { bx = x - pw - 5; by = y - ph / 2; }
    else { bx = x - pw / 2; by = y - ph / 2; }
    if (!transparent && st !== 'none') {
      g.fillStyle = l.color; roundRect(g, bx, by, pw, ph, 3); g.fill();
      g.beginPath();
      if (st === 'label_down') { g.moveTo(x - 4, by + ph); g.lineTo(x + 4, by + ph); g.lineTo(x, y); }
      if (st === 'label_up') { g.moveTo(x - 4, by); g.lineTo(x + 4, by); g.lineTo(x, y); }
      if (st === 'label_left') { g.moveTo(bx, y - 4); g.lineTo(bx, y + 4); g.lineTo(x, y); }
      if (st === 'label_right') { g.moveTo(bx + pw, y - 4); g.lineTo(bx + pw, y + 4); g.lineTo(x, y); }
      g.fill();
    }
    g.fillStyle = l.textcolor || C.text; g.textAlign = 'center'; g.textBaseline = 'top';
    lines.forEach((s, k) => g.fillText(s, bx + pw / 2, by + 2 + k * (fs + 2)));
  }
  drawShapes(g, pane, s, i0, i1) {
    const D = this.data(), off = s.offset || 0, sz = SIZE_PX[s.size] || 8;
    for (let k = Math.max(0, i0 - off); k <= Math.min(s.values.length - 1, i1 - off); k++) {
      const v = s.values[k]; if (!finite(v)) continue; const col = s.colors ? s.colors[k] : s.color; if (!col) continue;
      const i = k + off, b = D[k], x = this.idxToX(i); if (!b) continue;
      const above = s.location === 'abovebar' || s.location === 'top';
      let y = s.location === 'abovebar' ? this.pY(pane, b.h) - sz - 4 : s.location === 'belowbar' ? this.pY(pane, b.l) + sz + 4 : s.location === 'top' ? pane.y + sz + 4 : s.location === 'bottom' ? pane.y + pane.h - sz - 4 : this.pY(pane, v);
      if (s.kind === 'char') { g.fillStyle = col; g.font = `${sz + 4}px Inter, sans-serif`; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(s.char, x, y); }
      else if (s.style === 'labelup' || s.style === 'labeldown') {
        g.font = `600 ${LABEL_FONT[s.size] || 10}px Inter, sans-serif`; const txt = s.text || '', tw = Math.max(10, g.measureText(txt).width + 10), th = (LABEL_FONT[s.size] || 10) + 6;
        const up = s.style === 'labelup'; const by = up ? y - sz + 2 : y + sz - th - 2;
        g.fillStyle = col; roundRect(g, x - tw / 2, by, tw, th, 3); g.fill(); g.beginPath();
        if (up) { g.moveTo(x - 4, by); g.lineTo(x + 4, by); g.lineTo(x, by - 5); } else { g.moveTo(x - 4, by + th); g.lineTo(x + 4, by + th); g.lineTo(x, by + th + 5); } g.fill();
        if (txt) { g.fillStyle = s.textcolor || '#fff'; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(txt, x, by + th / 2 + 0.5); }
        continue;
      } else drawShape(g, s.style, x, y, sz, col);
      if (s.text) { g.fillStyle = s.textcolor || col; g.font = `${LABEL_FONT[s.size] || 10}px Inter, sans-serif`; g.textAlign = 'center'; g.textBaseline = above ? 'bottom' : 'top'; g.fillText(s.text, x, above ? y - sz - 2 : y + sz + 2); }
    }
  }

  // user drawings & orders ------------------------------------------------------
  dPts(d, pane) { return d.points.map(p => ({ x: this.idxToX(this.tToIdx(p.t)), y: this.pY(pane, p.p) })); }
  drawUserDrawings(g, pane) {
    const all = this.pending ? [...this.drawings, this.pending] : this.drawings;
    for (const d of all) {
      if (d.hidden) continue; const P = this.dPts(d, pane), col = d.color || (d.source === 'ai' ? '#f5a623' : '#2962ff'), sel = d === this.sel || d === this.hoverDrawing;
      g.strokeStyle = col; g.fillStyle = col; g.lineWidth = (d.width || (d.type === 'hline' ? 1 : 2)) + (sel ? 0.5 : 0);
      g.setLineDash(d.style === 'dashed' ? [6, 4] : d.style === 'dotted' ? [1, 3] : []);
      const W = this.L.plotW;
      if (d.type === 'hline' || d.type === 'hray') {
        const y = Math.round(P[0].y) + 0.5, x0 = d.type === 'hray' ? P[0].x : 0;
        g.beginPath(); g.moveTo(x0, y); g.lineTo(W, y); g.stroke(); g.setLineDash([]);
        const txt = d.text || ''; if (txt) { g.font = '600 11px Inter, sans-serif'; g.textAlign = 'right'; g.textBaseline = 'bottom'; g.fillText(txt, W - 8, y - 3); }
      } else if (d.type === 'vline') { const x = Math.round(P[0].x) + 0.5; g.beginPath(); g.moveTo(x, pane.y); g.lineTo(x, pane.y + pane.h); g.stroke(); }
      else if (d.type === 'trend' && P.length === 2) {
        let [a, b] = P; if (d.extend) { const dx = b.x - a.x, dy = b.y - a.y; if (dx) { const t = (W - a.x) / dx; b = { x: a.x + dx * t, y: a.y + dy * t }; } }
        g.beginPath(); g.moveTo(a.x, a.y); g.lineTo(b.x, b.y); g.stroke();
        if (d.text) { g.setLineDash([]); g.font = '600 11px Inter, sans-serif'; g.textAlign = 'left'; g.textBaseline = 'bottom'; g.fillText(d.text, P[1].x + 4, P[1].y - 2); }
      } else if ((d.type === 'rect' || d.type === 'zone') && P.length === 2) {
        const x1 = d.type === 'zone' && !d.points[0].t ? 0 : P[0].x, x2 = d.type === 'zone' && d.extend !== false ? W : P[1].x;
        const x = Math.min(x1, x2), y = Math.min(P[0].y, P[1].y), w = Math.abs(x2 - x1), h = Math.abs(P[1].y - P[0].y);
        g.fillStyle = withAlpha(col, d.type === 'zone' ? 0.16 : 0.12); g.fillRect(x, y, w, h); g.lineWidth = 1; g.strokeRect(x + 0.5, y + 0.5, w, h);
        if (d.text) { g.setLineDash([]); g.fillStyle = col; g.font = '600 11px Inter, sans-serif'; g.textAlign = 'left'; g.textBaseline = 'top'; g.fillText(d.text, x + 6, y + 4); }
      } else if (d.type === 'text') { g.setLineDash([]); g.font = `600 ${d.size || 13}px Inter, sans-serif`; g.textAlign = 'left'; g.textBaseline = 'middle'; g.fillText(d.text || 'Text', P[0].x, P[0].y); }
      else if (d.type === 'measure' && P.length === 2) {
        const [a, b] = P, up = d.points[1].p >= d.points[0].p, c2 = up ? 'rgba(41,98,255,' : 'rgba(242,54,69,';
        g.setLineDash([]); g.fillStyle = c2 + '0.15)'; g.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
        const dp = d.points[1].p - d.points[0].p, pct = dp / d.points[0].p * 100, nb = Math.round(this.tToIdx(d.points[1].t) - this.tToIdx(d.points[0].t));
        const txt = `${dp >= 0 ? '+' : ''}${fmtPrice(dp, this.prec)} (${pct.toFixed(2)}%)  ${nb} bars`;
        g.font = '600 11px Inter, sans-serif'; const tw = g.measureText(txt).width + 12, bx = (a.x + b.x) / 2 - tw / 2, by = up ? Math.min(a.y, b.y) - 24 : Math.max(a.y, b.y) + 4;
        g.fillStyle = c2 + '0.9)'; roundRect(g, bx, by, tw, 20, 4); g.fill(); g.fillStyle = '#fff'; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(txt, bx + tw / 2, by + 10);
      }
      g.setLineDash([]);
      if (d === this.sel) for (const p of P) { g.fillStyle = C.bg; g.strokeStyle = col; g.lineWidth = 1.5; g.beginPath(); g.arc(d.type === 'hline' ? this.L.plotW / 2 : p.x, p.y, 4.5, 0, 7); g.fill(); g.stroke(); }
    }
  }
  drawOrderLines(g, pane) {
    const W = this.L.plotW;
    for (const o of this.orderLines) {
      const price = this.dragOrder && this.dragOrder.id === o.id ? this.dragOrder.price : o.price;
      const y = Math.round(this.pY(pane, price)) + 0.5; if (y < pane.y - 20 || y > pane.y + pane.h + 20) continue;
      g.strokeStyle = o.color; g.lineWidth = 1; g.setLineDash(o.kind === 'position' ? [] : [5, 4]);
      g.beginPath(); g.moveTo(0, y); g.lineTo(W, y); g.stroke(); g.setLineDash([]);
      g.font = '600 11px Inter, sans-serif'; const tw = g.measureText(o.label).width + 14, bx = W - tw - 90;
      g.fillStyle = o.color; roundRect(g, bx, y - 10, tw, 20, 3); g.fill();
      g.fillStyle = '#fff'; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(o.label, bx + tw / 2, y + 0.5);
      if (o.cancelable) { g.fillStyle = C.bg; roundRect(g, bx + tw + 2, y - 10, 18, 20, 3); g.fill(); g.strokeStyle = o.color; g.strokeRect(bx + tw + 2.5, y - 9.5, 17, 19); g.fillStyle = o.color; g.fillText('×', bx + tw + 11, y); o._x = bx + tw + 2; }
      o._bx = bx; o._bw = tw;
    }
  }
  drawLastPrice(g, pane) {
    const D = this.data(), n = this.count(); if (!n) return; const b = D[n - 1], y = Math.round(this.pY(pane, b.c)) + 0.5;
    g.strokeStyle = b.c >= b.o ? C.up : C.dn; g.setLineDash([1, 2]); g.lineWidth = 1; g.beginPath(); g.moveTo(0, y); g.lineTo(this.L.plotW, y); g.stroke(); g.setLineDash([]);
  }

  drawAxes(g, tticks) {
    const L = this.L, D = this.data(), n = this.count();
    g.fillStyle = C.bg; g.fillRect(L.plotW, 0, this.axisW, L.H); g.fillRect(0, L.total, L.W, TIME_H);
    g.strokeStyle = C.sep; g.lineWidth = 1; g.beginPath(); g.moveTo(L.plotW + 0.5, 0); g.lineTo(L.plotW + 0.5, L.total); g.moveTo(0, L.total + 0.5); g.lineTo(L.W, L.total + 0.5); g.stroke();
    g.font = MONO; g.textBaseline = 'middle'; g.textAlign = 'left';
    for (const pane of L.panes) {
      const d = this.ind.find(x => x.id === pane.id), vol = d && d.out && d.out.meta.format === 'volume';
      const prec = pane.id === 'main' ? this.prec : (d && d.out && Number.isFinite(d.out.meta.precision) ? d.out.meta.precision : 2);
      g.fillStyle = C.axis;
      for (const p of this.priceTicks(pane)) { const y = this.pY(pane, p); if (y < pane.y + 8 || y > pane.y + pane.h - 8) continue; g.fillText(vol ? fmtVol(p) : fmtPrice(p, prec), L.plotW + 6, y); }
      const tags = [];
      if (pane.id === 'main') {
        if (n) { const b = D[n - 1]; tags.push({ y: this.pY(pane, b.c), txt: fmtPrice(b.c, this.prec), col: b.c >= b.o ? C.up : C.dn, sub: this.countdown() }); }
        for (const o of this.orderLines) { const pr = this.dragOrder && this.dragOrder.id === o.id ? this.dragOrder.price : o.price; tags.push({ y: this.pY(pane, pr), txt: fmtPrice(pr, this.prec), col: o.color }); }
        for (const dr of this.drawings) if ((dr.type === 'hline' || dr.type === 'hray') && !dr.hidden) tags.push({ y: this.pY(pane, dr.points[0].p), txt: fmtPrice(dr.points[0].p, this.prec), col: dr.color || (dr.source === 'ai' ? '#f5a623' : '#2962ff') });
      }
      const pi = d ? [d] : this.ind.filter(x => x.visible !== false && x.out && this.isOverlay(x) && x.out.meta.scale !== 'none');
      for (const x of pi) { if (x.visible === false || !x.out) continue; for (const p of x.out.plots.slice(0, 4)) { if (p.display === 'none') continue; const k = Math.min(n - 1, p.values.length - 1) - (p.offset > 0 ? 0 : 0); const v = p.values[k]; const col = p.colors ? p.colors[k] : p.color; if (finite(v) && col) tags.push({ y: this.pY(pane, v), txt: vol ? fmtVol(v) : fmtPrice(v, prec), col: solid(col), small: true }); } }
      g.save(); g.beginPath(); g.rect(L.plotW, pane.y, this.axisW, pane.h); g.clip();
      for (const t of tags.reverse()) {
        if (!finite(t.y)) continue; const h = t.sub ? 30 : 18, y = Math.max(pane.y + h / 2, Math.min(pane.y + pane.h - h / 2, t.y));
        g.fillStyle = t.col; g.fillRect(L.plotW + 1, y - (t.sub ? 9 : h / 2), this.axisW - 1, h);
        g.fillStyle = C.tagText; g.font = MONO; g.fillText(t.txt, L.plotW + 6, t.sub ? y : y + 0.5);
        if (t.sub) g.fillText(t.sub, L.plotW + 6, y + 13);
      }
      g.restore();
    }
    g.fillStyle = C.axis; g.font = FONT; g.textAlign = 'center';
    for (const t of tticks) g.fillText(t.txt, t.x, L.total + TIME_H / 2);
  }
  countdown() {
    if (!this.live || this.replay != null || this.tfSec > 86400 || !this.bars.length) return '';
    const end = this.bars[this.bars.length - 1].t + this.tfSec, left = Math.floor(end - Date.now() / 1000); if (left < 0 || left > this.tfSec) return '';
    const h = Math.floor(left / 3600), m = Math.floor(left % 3600 / 60), s = left % 60, p = x => String(x).padStart(2, '0');
    return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
  }
  drawCrosshair(g) {
    const h = this.hover, L = this.L; if (!h || this.drag) return;
    if (h.x > L.plotW || h.y > L.total) return;
    const idx = Math.round(this.xToIdx(h.x)), x = Math.round(this.idxToX(idx)) + 0.5, y = Math.round(h.y) + 0.5, pane = this.paneAt(h.y);
    g.strokeStyle = C.cross; g.setLineDash([4, 4]); g.lineWidth = 1; g.beginPath(); g.moveTo(x, 0); g.lineTo(x, L.total); if (pane) { g.moveTo(0, y); g.lineTo(L.plotW, y); } g.stroke(); g.setLineDash([]);
    if (this.tool === 'replay-pick') { g.fillStyle = 'rgba(41,98,255,0.12)'; g.fillRect(x, 0, L.plotW - x, L.total); }
    g.font = MONO; g.textBaseline = 'middle';
    if (pane) { const p = this.yP(pane, h.y); const d = this.ind.find(z => z.id === pane.id); const txt = d && d.out && d.out.meta.format === 'volume' ? fmtVol(p) : fmtPrice(p, pane.id === 'main' ? this.prec : 2); g.fillStyle = '#363a45'; g.fillRect(L.plotW + 1, y - 9, this.axisW - 1, 18); g.fillStyle = '#fff'; g.textAlign = 'left'; g.fillText(txt, L.plotW + 6, y); }
    const t = this.idxToT(idx), p = nyParts(Math.round(t)), txt = this.tfSec >= 86400 ? `${p.wd} ${p.d} ${p.mon} '${String(p.y).slice(2)}` : `${p.wd} ${p.d} ${p.mon} '${String(p.y).slice(2)}  ${String(p.h).padStart(2, '0')}:${String(p.mi).padStart(2, '0')}`;
    g.font = FONT; const tw = g.measureText(txt).width + 14; g.fillStyle = '#363a45'; g.fillRect(x - tw / 2, L.total + 1, tw, TIME_H - 2); g.fillStyle = '#fff'; g.textAlign = 'center'; g.fillText(txt, x, L.total + TIME_H / 2);
  }

  // ---------------------------------------------------------------- legends (DOM)
  buildLegend() {
    const el = this.legendEl; el.innerHTML = '';
    const main = document.createElement('div'); main.className = 'lg'; main.dataset.pane = 'main';
    main.innerHTML = `<div class="lg-row lg-sym"><b>${esc(this.symbol)}</b><span class="dim">· ${esc(this.tf)}</span><span class="lg-ohlc"></span></div>`;
    el.appendChild(main);
    if (this.opts.legendSlot) main.appendChild(this.opts.legendSlot);
    const row = (d, host) => {
      const r = document.createElement('div'); r.className = 'lg-row lg-ind' + (d.visible === false ? ' off' : '') + (d.out && d.out.error ? ' err' : ''); r.dataset.id = d.id;
      r.innerHTML = `<span class="lg-name">${esc(d.out ? (d.out.meta.shorttitle || d.out.meta.title) : d.title)}</span><span class="lg-args dim">${esc(argsText(d))}</span>${d.skip && d.skip.length ? `<span class="lg-skip" title="No entries: ${esc(d.skip.join(', '))} (day / hour filter from Research)">⊘ ${d.skip.length}</span>` : ''}<span class="lg-vals"></span>
        <span class="lg-btns"><button data-a="toggle" title="Show / hide">${d.visible === false ? '◌' : '◉'}</button><button data-a="settings" title="Settings">⚙</button><button data-a="source" title="Source code">{ }</button>${d.out && d.out.strategy ? '<button data-a="research" title="Backtest / optimize in Research">⚗</button><button data-a="freeze" title="Freeze → paper trade it on the server with these inputs">❄</button>' : ''}<button data-a="remove" title="Remove">✕</button></span>
        ${d.out && d.out.error ? `<span class="lg-err">⚠ ${esc(d.out.error.message)}</span>` : ''}`;
      r.addEventListener('click', e => { const b = e.target.closest('button'); if (b) { e.stopPropagation(); this.opts.onLegend && this.opts.onLegend(d.id, b.dataset.a); } });
      host.appendChild(r);
    };
    for (const d of this.ind) if (this.isOverlay(d)) row(d, main);
    for (const d of this.ind) if (!this.isOverlay(d)) { const p = document.createElement('div'); p.className = 'lg'; p.dataset.pane = d.id; el.appendChild(p); row(d, p); }
    this.updateLegendValues();
  }
  layoutLegends() {
    if (!this.L) return;
    for (const node of this.legendEl.children) { const p = this.L.panes.find(q => q.id === node.dataset.pane); if (p) { node.style.top = (p.y + 6) + 'px'; node.style.display = ''; } else node.style.display = 'none'; }
    this.updateLegendValues();
  }
  updateLegendValues() {
    const D = this.data(), n = this.count(); if (!n) return;
    const i = this.hover && this.L && this.hover.x < this.L.plotW ? Math.max(0, Math.min(n - 1, Math.round(this.xToIdx(this.hover.x)))) : n - 1;
    const b = D[i], prevC = i ? D[i - 1].c : b.o, ch = b.c - prevC, col = b.c >= b.o ? C.up : C.dn;
    const o = this.legendEl.querySelector('.lg-ohlc');
    if (o) o.innerHTML = `<span>O<i style="color:${col}">${fmtPrice(b.o, this.prec)}</i></span><span>H<i style="color:${col}">${fmtPrice(b.h, this.prec)}</i></span><span>L<i style="color:${col}">${fmtPrice(b.l, this.prec)}</i></span><span>C<i style="color:${col}">${fmtPrice(b.c, this.prec)}</i></span><span style="color:${ch >= 0 ? C.up : C.dn}">${ch >= 0 ? '+' : ''}${fmtPrice(ch, this.prec)} (${ch >= 0 ? '+' : ''}${(ch / prevC * 100).toFixed(2)}%)</span><span class="dim">Vol <i>${fmtVol(b.v)}</i></span>`;
    for (const d of this.ind) {
      const r = this.legendEl.querySelector(`.lg-ind[data-id="${d.id}"] .lg-vals`); if (!r || !d.out) continue;
      const fmt = d.out.meta.format === 'volume' ? fmtVol : v => fmtPrice(v, d.out.meta.overlay ? this.prec : (Number.isFinite(d.out.meta.precision) ? d.out.meta.precision : 2));
      const sm = d.out.strategy ? `<i class="${d.out.strategy.net >= 0 ? 'up' : 'dn'}">net ${d.out.strategy.net >= 0 ? '+' : ''}${d.out.strategy.net.toFixed(0)} · ${d.out.strategy.trades.length} trades</i>` : '';
      r.innerHTML = sm + d.out.plots.filter(p => p.display !== 'none').slice(0, 5).map(p => { const v = p.values[i - (p.offset || 0)], c = p.colors ? p.colors[i - (p.offset || 0)] : p.color; return `<i style="color:${solid(c || p.color)}">${finite(v) ? fmt(v) : '∅'}</i>`; }).join('');
    }
  }

  // ---------------------------------------------------------------- interaction
  bind() {
    const cv = this.cv, on = (t, f, o) => cv.addEventListener(t, f, o);
    on('wheel', e => {
      e.preventDefault(); if (!this.L) return;
      const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      if (x > this.L.plotW) { const pane = this.paneAt(y); if (pane) this.zoomY(pane, e.deltaY > 0 ? 1.1 : 1 / 1.1); return; }
      if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) { const d = (e.deltaX || e.deltaY) / this.L.plotW * (this.view.b - this.view.a); this.view.a += d; this.view.b += d; this.clampView(); return; }
      const f = Math.exp(e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015)); this.zoomX(f, this.xToIdx(x));
    }, { passive: false });
    on('pointerdown', e => this.down(e));
    on('pointermove', e => this.move(e));
    on('pointerup', e => this.up(e));
    on('pointercancel', e => this.up(e));
    on('pointerleave', () => { if (!this.drag) { this.hover = null; this.hoverDrawing = null; this.req(); this.updateLegendValues(); } });
    on('dblclick', e => {
      const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      if (x > this.L.plotW) { const p = this.paneAt(y); if (p) { this.paneState.get(p.id).auto = true; this.req(); } return; }
      const hit = this.hitDrawing(x, y); if (hit && this.opts.onDrawingEdit) { this.opts.onDrawingEdit(hit); return; }
      this.fit(this.view.b - this.view.a);
    });
    on('contextmenu', e => { e.preventDefault(); const r = cv.getBoundingClientRect(); const x = e.clientX - r.left, y = e.clientY - r.top; const pane = this.paneAt(y); if (this.opts.onContext && pane && x < this.L.plotW) this.opts.onContext({ x: e.clientX, y: e.clientY, price: this.yP(pane, y), t: this.idxToT(this.xToIdx(x)), pane: pane.id, drawing: this.hitDrawing(x, y) }); });
    cv.addEventListener('keydown', e => {
      if ((e.key === 'Delete' || e.key === 'Backspace') && this.sel) { e.preventDefault(); this.opts.onDrawingDelete && this.opts.onDrawingDelete(this.sel); this.sel = null; this.req(); }
      if (e.key === 'Escape') { this.pending = null; this.sel = null; if (this.tool !== 'cursor') this.opts.onToolDone && this.opts.onToolDone(); this.req(); }
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { const d = (e.key === 'ArrowLeft' ? -1 : 1) * Math.max(1, (this.view.b - this.view.a) * 0.05); this.view.a += d; this.view.b += d; this.clampView(); }
    });
  }
  pos(e) { const r = this.cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
  snapPrice(pane, x, y) {
    let p = this.yP(pane, y); if (!this.magnet || pane.id !== 'main') return p;
    const b = this.data()[Math.round(this.xToIdx(x))]; if (!b) return p;
    return [b.o, b.h, b.l, b.c].reduce((best, v) => Math.abs(v - p) < Math.abs(best - p) ? v : best, b.c);
  }
  down(e) {
    if (!this.L) return; this.cv.focus(); const { x, y } = this.pos(e), L = this.L;
    this.cv.setPointerCapture(e.pointerId);
    for (const p of L.panes) if (p.y > 0 && Math.abs(y - p.y) < 4 && x < L.plotW) { const prev = L.panes[L.panes.indexOf(p) - 1]; this.drag = { kind: 'sep', y, pane: p, prev, w0: this.paneState.get(p.id).w, h0: p.h }; return; }
    if (x > L.plotW && y < L.total) { const pane = this.paneAt(y); if (pane) { const st = this.paneState.get(pane.id); this.drag = { kind: 'yscale', y, pane, min: pane.min, max: pane.max }; st.auto = false; } return; }
    if (y > L.total) { this.drag = { kind: 'xscale', x, a: this.view.a, b: this.view.b }; return; }
    const pane = this.paneAt(y); if (!pane) return;
    if (this.tool === 'replay-pick') { this.opts.onReplayPick && this.opts.onReplayPick(Math.max(0, Math.round(this.xToIdx(x)))); return; }
    if (this.tool !== 'cursor' && pane.id === 'main') return this.startDraw(x, y, pane);
    if (pane.id === 'main') {
      for (const o of this.orderLines) {
        const oy = this.pY(pane, o.price);
        if (o.cancelable && o._x != null && x >= o._x && x <= o._x + 18 && Math.abs(y - oy) < 10) { this.opts.onOrderCancel && this.opts.onOrderCancel(o.id); return; }
        if (o.draggable && Math.abs(y - oy) < 5) { this.drag = { kind: 'order', o }; this.dragOrder = { id: o.id, price: o.price }; return; }
      }
      const hit = this.hitDrawing(x, y);
      if (hit) { this.sel = hit.d; this.opts.onSelect && this.opts.onSelect(hit.d); this.drag = { kind: 'drawing', d: hit.d, handle: hit.handle, x, y, orig: hit.d.points.map(p => ({ ...p })), moved: false }; this.req(); return; }
      if (this.sel) { this.sel = null; this.opts.onSelect && this.opts.onSelect(null); }
    }
    const st = this.paneState.get(pane.id);
    this.drag = { kind: 'pan', x, y, a: this.view.a, b: this.view.b, pane, min: pane.min, max: pane.max, wasAuto: st.auto };
  }
  move(e) {
    if (!this.L) return; const { x, y } = this.pos(e), d = this.drag, L = this.L;
    this.hover = { x, y };
    if (!d) {
      if (this.tool === 'cursor') {
        const pane = this.paneAt(y); let cur = x > L.plotW ? 'ns-resize' : y > L.total ? 'ew-resize' : 'default';
        if (L.panes.some(p => p.y > 0 && Math.abs(y - p.y) < 4) && x < L.plotW) cur = 'row-resize';
        this.hoverDrawing = null;
        if (pane && pane.id === 'main' && x < L.plotW) {
          if (this.orderLines.some(o => o.draggable && Math.abs(this.pY(pane, o.price) - y) < 5)) cur = 'ns-resize';
          const hit = this.hitDrawing(x, y); if (hit) { cur = hit.handle >= 0 ? 'move' : 'pointer'; this.hoverDrawing = hit.d; }
        }
        this.cv.style.cursor = cur;
      }
      if (this.pending) this.updatePending(x, y);
      this.req(); this.updateLegendValues(); return;
    }
    if (d.kind === 'pan') {
      const dx = x - d.x, dy = y - d.y, span = d.b - d.a, di = dx / L.plotW * span;
      this.view = { a: d.a - di, b: d.b - di };
      if (Math.abs(dy) > 2 || !d.wasAuto) { const st = this.paneState.get(d.pane.id), dp = dy / d.pane.h * (d.max - d.min); st.auto = false; st.min = d.min + dp; st.max = d.max + dp; }
      this.clampView(); return;
    }
    if (d.kind === 'yscale') { const st = this.paneState.get(d.pane.id), f = Math.exp((y - d.y) * 0.006), mid = (d.min + d.max) / 2, half = (d.max - d.min) / 2 * f; st.auto = false; st.min = mid - half; st.max = mid + half; this.req(); return; }
    if (d.kind === 'xscale') { const f = Math.exp(-(x - d.x) * 0.005), span = Math.max(8, Math.min(5000, (d.b - d.a) * f)); this.view = { a: d.b - span, b: d.b }; this.clampView(); return; }
    if (d.kind === 'sep') { const total = L.total, dyw = (y - d.y) / total; const st = this.paneState.get(d.pane.id); st.w = Math.max(0.08, Math.min(0.6, d.w0 - dyw)); this.req(); return; }
    if (d.kind === 'order') { this.dragOrder.price = this.yP(L.panes[0], y); this.req(); return; }
    if (d.kind === 'drawing') {
      const pane = L.panes[0], di = this.xToIdx(x) - this.xToIdx(d.x), dp = this.yP(pane, y) - this.yP(pane, d.y); d.moved = true;
      if (d.handle >= 0) { const p = d.d.points[d.handle]; p.t = this.idxToT(this.xToIdx(x)); p.p = this.snapPrice(pane, x, y); }
      else d.d.points.forEach((p, k) => { p.t = this.idxToT(this.tToIdx(d.orig[k].t) + di); p.p = d.orig[k].p + dp; });
      this.req();
    }
  }
  up(e) {
    const d = this.drag; this.drag = null; if (!d) return;
    if (d.kind === 'order') { const p = this.dragOrder.price; this.dragOrder = null; if (Math.abs(p - d.o.price) > 1e-9) this.opts.onOrderDrag && this.opts.onOrderDrag(d.o.id, p); this.req(); }
    if (d.kind === 'drawing' && d.moved) this.opts.onDrawingsChange && this.opts.onDrawingsChange();
    if (d.kind === 'pan' || d.kind === 'xscale') this.checkMore();
  }
  zoomX(f, at) { const v = this.view, span = Math.max(8, Math.min(6000, (v.b - v.a) * f)), r = (at - v.a) / (v.b - v.a); this.view = { a: at - r * span, b: at - r * span + span }; this.clampView(); this.checkMore(); }
  zoomY(pane, f) { const st = this.paneState.get(pane.id), mid = (pane.min + pane.max) / 2, half = (pane.max - pane.min) / 2 * f; st.auto = false; st.min = mid - half; st.max = mid + half; this.req(); }
  clampView() {
    const n = this.count(), w = this.view.b - this.view.a;
    if (this.view.a > n - 3) { this.view.a = n - 3; this.view.b = this.view.a + w; }
    if (this.view.b < 3) { this.view.b = 3; this.view.a = 3 - w; }
    this.req(); this.checkMore();
  }
  checkMore() { if (this.view.a < 40 && this.opts.onNeedMore && !this.loadingMore) this.opts.onNeedMore(); }

  // drawing tools ---------------------------------------------------------------
  startDraw(x, y, pane) {
    const pt = { t: this.idxToT(this.xToIdx(x)), p: this.snapPrice(pane, x, y) }, tool = this.tool;
    const one = { hline: 1, hray: 1, vline: 1, text: 1 }[tool];
    if (one) {
      const d = { id: newId('dr'), type: tool, points: [pt], source: 'user' };
      if (tool === 'text') { const t = prompt('Text', 'Note'); if (!t) return; d.text = t; }
      this.opts.onDrawingAdd && this.opts.onDrawingAdd(d); this.sel = d; this.opts.onToolDone && this.opts.onToolDone(); return;
    }
    if (!this.pending) { this.pending = { id: newId('dr'), type: tool, points: [pt, { ...pt }], source: 'user' }; this.req(); return; }
    this.pending.points[1] = pt; const d = this.pending; this.pending = null;
    if (d.type !== 'measure') { this.opts.onDrawingAdd && this.opts.onDrawingAdd(d); this.sel = d; }
    else { this.measure = d; setTimeout(() => { if (this.measure === d) { this.measure = null; this.req(); } }, 4000); }
    this.opts.onToolDone && this.opts.onToolDone(); this.req();
  }
  updatePending(x, y) { const pane = this.L.panes[0]; this.pending.points[1] = { t: this.idxToT(this.xToIdx(x)), p: this.snapPrice(pane, x, y) }; }
  hitDrawing(x, y) {
    const pane = this.L.panes[0]; const list = this.measure ? [...this.drawings] : this.drawings;
    for (let k = list.length - 1; k >= 0; k--) {
      const d = list[k]; if (d.hidden || d.locked) continue; const P = this.dPts(d, pane);
      for (let h = 0; h < P.length; h++) { const hx = d.type === 'hline' ? this.L.plotW / 2 : P[h].x; if (Math.hypot(hx - x, P[h].y - y) < 7) return { d, handle: h }; }
      if ((d.type === 'hline' && Math.abs(P[0].y - y) < 5) || (d.type === 'hray' && x >= P[0].x && Math.abs(P[0].y - y) < 5)) return { d, handle: -1 };
      if (d.type === 'vline' && Math.abs(P[0].x - x) < 5) return { d, handle: -1 };
      if (d.type === 'trend' && P.length === 2 && dist2seg(x, y, P[0].x, P[0].y, P[1].x, P[1].y) < 5) return { d, handle: -1 };
      if ((d.type === 'rect' || d.type === 'zone') && P.length === 2) { const x2 = d.type === 'zone' ? this.L.plotW : P[1].x; if (x >= Math.min(P[0].x, x2) && x <= Math.max(P[0].x, x2) && y >= Math.min(P[0].y, P[1].y) && y <= Math.max(P[0].y, P[1].y)) return { d, handle: -1 }; }
      if (d.type === 'text' && x >= P[0].x - 2 && x <= P[0].x + 120 && Math.abs(y - P[0].y) < 9) return { d, handle: -1 };
    }
    return null;
  }

  // snapshot for the AI: the canvas plus a title strip with symbol / timeframe / indicators
  snapshot(maxW = 1400) {
    this.draw(); const src = this.cv, k = Math.min(1, maxW / src.width), w = Math.round(src.width * k), h = Math.round(src.height * k);
    const out = document.createElement('canvas'); out.width = w; out.height = h + 26; const g = out.getContext('2d');
    g.fillStyle = C.bg; g.fillRect(0, 0, w, h + 26); g.drawImage(src, 0, 26, w, h);
    const D = this.data(), b = D[this.count() - 1];
    g.fillStyle = '#d1d4dc'; g.font = '600 13px Inter, sans-serif'; g.textBaseline = 'middle'; g.textAlign = 'left';
    const names = this.ind.filter(d => d.visible !== false && d.out).map(d => d.out.meta.shorttitle || d.out.meta.title).join(', ');
    g.fillText(`${this.symbol} · ${this.tf} · last ${b ? fmtPrice(b.c, this.prec) : '—'}${names ? '   |   ' + names : ''}`, 8, 13);
    return out.toDataURL('image/jpeg', 0.82);
  }
}

function drawShape(g, st, x, y, s, col) {
  g.fillStyle = col; g.strokeStyle = col; g.lineWidth = 1.5; g.beginPath();
  switch (st) {
    case 'triangleup': case 'arrowup': g.moveTo(x, y - s / 2); g.lineTo(x + s / 2, y + s / 2); g.lineTo(x - s / 2, y + s / 2); g.closePath(); g.fill(); break;
    case 'triangledown': case 'arrowdown': g.moveTo(x, y + s / 2); g.lineTo(x + s / 2, y - s / 2); g.lineTo(x - s / 2, y - s / 2); g.closePath(); g.fill(); break;
    case 'circle': g.arc(x, y, s / 2, 0, 7); g.fill(); break;
    case 'square': g.rect(x - s / 2, y - s / 2, s, s); g.fill(); break;
    case 'diamond': g.moveTo(x, y - s / 2); g.lineTo(x + s / 2, y); g.lineTo(x, y + s / 2); g.lineTo(x - s / 2, y); g.closePath(); g.fill(); break;
    case 'cross': g.moveTo(x - s / 2, y); g.lineTo(x + s / 2, y); g.moveTo(x, y - s / 2); g.lineTo(x, y + s / 2); g.stroke(); break;
    case 'flag': g.moveTo(x - s / 3, y + s / 2); g.lineTo(x - s / 3, y - s / 2); g.lineTo(x + s / 2, y - s / 4); g.lineTo(x - s / 3, y); g.fill(); g.stroke(); break;
    default: g.moveTo(x - s / 2, y - s / 2); g.lineTo(x + s / 2, y + s / 2); g.moveTo(x + s / 2, y - s / 2); g.lineTo(x - s / 2, y + s / 2); g.stroke();
  }
}
function roundRect(g, x, y, w, h, r) { g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r); g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath(); }
export function withAlpha(c, a) {
  if (typeof c !== 'string') return c; if (c[0] === '#' && (c.length === 7 || c.length === 9)) return c.slice(0, 7) + Math.round(a * 255).toString(16).padStart(2, '0');
  return c;
}
function solid(c) { return typeof c === 'string' && c.length === 9 ? c.slice(0, 7) : c; }
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch])); }
function argsText(d) { if (!d.out || !d.out.inputs) return ''; return d.out.inputs.filter(i => i && ['int', 'float', 'source'].includes(i.type)).map(i => i.value).join(' '); }
