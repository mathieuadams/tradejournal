/* Canvas chart for NQ / ES: candles, session VWAP, prior-day and overnight levels, volume profile, strategy trades,
   cumulative delta and WaveTrend panes, wheel zoom / drag pan, crosshair, and a replay cursor. No libraries. */
(function () {
  const COL = { bg: '#12161D', grid: '#1A2029', axis: '#93A0B2', text: '#E7EAF0', up: '#4A9EFF', dn: '#F08C2E', upT: '#7DB8FF', dnT: '#F5A54A',
    vwap: '#C9A3FF', lvl: '#93A0B2', on: '#6F7C8F', sep: '#2C3442', night: 'rgba(255,255,255,0.025)', prof: 'rgba(125,184,255,0.16)', poc: 'rgba(125,184,255,0.42)' };

  // WaveTrend [Krypt] (Pine v3 port): {sig, ma, buy, sell}
  function wtKrypt(close, n1 = 10, n2 = 21, smoothing = 4, maperiod = 4, ob = 0.75, os = -0.75) {
    const N = close.length, NA = NaN, fin = Number.isFinite, nz = (x, y) => fin(x) ? x : y;
    const xema = (src, len) => { const m = 2 / (len + 1), r = new Array(N).fill(NA); for (let i = 0; i < N; i++) r[i] = fin(src[i]) ? m * src[i] + (1 - m) * nz(i ? r[i - 1] : NA, src[i]) : NA; return r; };
    const ss = (src, lower) => { const a1 = Math.exp(-Math.PI * Math.SQRT2 / lower), c2 = 2 * a1 * Math.cos(Math.SQRT2 * Math.PI / lower), c3 = -a1 * a1, c1 = (1 - c2 - c3) / 2, f = new Array(N).fill(NA);
      for (let i = 0; i < N; i++) { const s1 = i ? src[i - 1] : NA, f1 = i ? f[i - 1] : NA, f2 = i > 1 ? f[i - 2] : NA; f[i] = nz(c1 * (src[i] + nz(s1, src[i])) + c2 * f1 + c3 * nz(f2, f1), src[i]); } return f; };
    const win = (s, n, fn) => s.map((_, i) => { if (i < n - 1) return NA; const w = s.slice(i - n + 1, i + 1); return w.every(fin) ? fn(w) : NA; });
    const sma = (s, n) => win(s, n, w => w.reduce((a, b) => a + b, 0) / n), sum = (s, n) => win(s, n, w => w.reduce((a, b) => a + b, 0));
    const step = (xs, vals) => { const r = new Array(N).fill(NA); for (let i = 0; i < N; i++) r[i] = nz(xs[i] ? vals[i] : (i ? r[i - 1] : NA), vals[i]); return r; };
    const cas = (xs, start, wh, up) => { const r = new Array(N).fill(NA); for (let i = 0; i < N; i++) { const x1 = i ? xs[i - 1] : NA, r1 = i ? r[i - 1] : NA, st = typeof start === 'boolean' ? start : start[i], w = typeof wh === 'boolean' ? wh : wh[i];
        const mv = up ? xs[i] > x1 : xs[i] < x1, hold = up ? xs[i] >= x1 : xs[i] <= x1, d = up ? xs[i] - x1 : x1 - xs[i];
        const v = (st && mv && !r1 && w) ? d : (hold && w ? r1 + d : 0); r[i] = Math.abs(nz(v, 0)); } return r; };
    const price = close.map((c, i) => i && fin(close[i - 1]) && c > 0 ? Math.log(c) : NA);
    const pe = xema(price, n1), delta = price.map((p, i) => p - pe[i]), d = xema(delta.map(Math.abs), n1);
    const ci = delta.map((x, i) => !fin(d[i]) ? NA : d[i] === 0 ? 0 : x / d[i]);
    const sig = ss(xema(ci, n2), smoothing), ma = sma(sig, maperiod);
    const mid = (ob + os) / 2, exoff = (ob - mid) / 7, hu = mid + 0.61803398875, hl = mid - 0.61803398875, req = 5;
    const ssig = sum(sig, req), sma_ = sum(ma, req);
    const xo = i => i > 0 && sig[i] > ma[i] && sig[i - 1] <= ma[i - 1], xu = i => i > 0 && sig[i] < ma[i] && sig[i - 1] >= ma[i - 1];
    const allb = sig.map((_, i) => ma[i] < ob && xo(i) && i > 0 && ssig[i - 1] < sma_[i - 1]);
    const allt = sig.map((_, i) => ma[i] > os && xu(i) && i > 0 && ssig[i - 1] > sma_[i - 1]);
    const bsteps = step(allb, ma), tsteps = step(allt, ma);
    const pb_ = step(allb, win(price, req, w => Math.min(...w))), pt_ = step(allt, win(price, req, w => Math.max(...w)));
    const cb = cas(pb_, true, true, true), cdp = cas(pt_, true, true, false), ct = cas(tsteps, true, true, true), cdb = cas(bsteps, true, true, false);
    const hb = cdb.map((x, i) => Math.sqrt(x * cb[i])), hr = ct.map((x, i) => Math.sqrt(x * cdp[i]));
    const hbull = hb.map((x, i) => x / (1 + hr[i])), hbear = hr.map((x, i) => x / (1 + hb[i]));
    const pbull = cas(bsteps, bsteps.map((x, i) => i > 0 && (bsteps[i - 1] < os || hbull[i - 1] > hbear[i - 1])), sig.map(x => x < ob), true);
    const pbear = cas(tsteps, tsteps.map((x, i) => i > 0 && (tsteps[i - 1] > ob || hbear[i - 1] > hbull[i - 1])), sig.map(x => x > os), false);
    const btr = pbull.map((x, i) => x / (1 + pbear[i])), str = pbear.map((x, i) => x / (1 + pbull[i]));
    const buy = sig.map((_, i) => allb[i] && ((ma[i] < mid && (sig[i - 1] < os - exoff || btr[i] > hbear[i])) || (ma[i] < hu && hbull[i] > str[i])));
    const sell = sig.map((_, i) => allt[i] && ((ma[i] > mid && (sig[i - 1] > ob + exoff || str[i] > hbull[i])) || (ma[i] > hl && hbear[i] > btr[i])));
    return { sig, ma, buy, sell };
  }

  const fmt = (p, d = 2) => p == null ? '—' : p.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });

  class Chart {
    constructor(canvas, opts = {}) {
      this.cv = canvas; this.opts = opts; this.d = null; this.view = null; this.hover = null;
      const on = (ev, fn, o) => canvas.addEventListener(ev, fn, o);
      on('wheel', e => { e.preventDefault(); this.zoom(e.deltaY > 0 ? 1.15 : 1 / 1.15, this.xToIdx(e.offsetX)); }, { passive: false });
      on('pointerdown', e => { this.drag = { x: e.clientX, a: this.view.a, b: this.view.b }; canvas.setPointerCapture(e.pointerId); });
      on('pointermove', e => {
        if (this.drag) { const w = (this.view.b - this.view.a) / this.plotW(); const dx = (e.clientX - this.drag.x) * w; this.setView(this.drag.a - dx, this.drag.b - dx); return; }
        this.hover = { x: e.offsetX, y: e.offsetY }; this.draw();
      });
      on('pointerup', () => { this.drag = null; });
      on('pointerleave', () => { this.hover = null; this.draw(); });
      on('dblclick', () => this.fit());
      this.ro = new ResizeObserver(() => this.draw()); this.ro.observe(canvas);
    }
    destroy() { this.ro.disconnect(); }
    plotW() { return this.cv.clientWidth - 70; }
    xToIdx(x) { const v = this.view; return v.a + (x - 6) / this.plotW() * (v.b - v.a); }
    count() { return this.d ? (this.d.replay != null ? this.d.replay + 1 : this.d.bars.length) : 0; }
    setView(a, b) {
      const n = this.count(), w = b - a;
      if (b > n + w * 0.3) { b = n + w * 0.3; a = b - w; }
      if (a < -w * 0.3) { a = -w * 0.3; b = a + w; }
      this.view = { a, b }; this.draw();
    }
    zoom(f, at) { const v = this.view, w = Math.max(15, Math.min(this.count() * 1.3 + 20, (v.b - v.a) * f)); const r = (at - v.a) / (v.b - v.a); this.setView(at - r * w, at - r * w + w); }
    fit(span) { const n = this.count(); const w = span || Math.min(n, this.opts.span || 180); this.setView(n - w + 2, n + 2); }
    follow() { const n = this.count(), v = this.view, w = v.b - v.a; if (n > v.b - 2 || n < v.a + 2) this.setView(n - w * 0.75, n + w * 0.25); else this.draw(); }

    set(d, keepView) {
      // d: {bars:[[t,o,h,l,c,v,day]], levels:{day:{...}}, marks:[...], fpDelta:{day:{'HH:MM':delta}}, tf, showWT, showCVD, showProfile, replay}
      this.d = d;
      const B = d.bars;
      this.vw = []; this.cvd = []; let day = null, pv = 0, vv = 0, cd = 0;
      this.cvdReal = false;
      for (let i = 0; i < B.length; i++) {
        const b = B[i];
        if (b[6] !== day) { day = b[6]; pv = 0; vv = 0; cd = 0; }
        pv += (b[2] + b[3] + b[4]) / 3 * b[5]; vv += b[5]; this.vw.push(vv ? pv / vv : b[4]);
        const fd = d.fpDelta && d.fpDelta[day];
        let dl;
        if (fd) { dl = 0; const m0 = toMin(b[0]); for (let m = m0; m < m0 + d.tf; m++) dl += fd[minStr(m)] || 0; this.cvdReal = true; }
        else { const r = b[2] - b[3]; dl = r > 0 ? b[5] * ((b[4] - b[3]) - (b[2] - b[4])) / r : 0; }
        cd += dl; this.cvd.push(cd);
      }
      this.wt = d.showWT && B.length > 40 ? wtKrypt(B.map(b => b[4])) : null;
      this.ema = null;
      if (d.showEMA) { const n = d.emaLen || 20, m = 2 / (n + 1); let e = null; this.ema = B.map(b => (e = e == null ? b[4] : m * b[4] + (1 - m) * e)); }
      // FVGs from the chart's candles; supply / demand from the server, redrawn every session from its own overnight + day
      this.zones = d.showFVG ? zones(B, true, false) : [];
      if (d.showSD) {
        const first = {}; B.forEach((b, i) => { if (first[b[6]] == null) first[b[6]] = i; });
        (d.sdZones || []).forEach(z => { if (first[z.day] == null) return;
          const i0 = Math.max(this.idxOf(z.t0), first[z.day]), end = this.idxOf(z.t1);
          if (end < i0) return;
          this.zones.push({ k: z.k, label: (z.k === 'demand' ? 'Demand' : 'Supply') + (z.on ? ' · ON' : ''), lo: z.lo, hi: z.hi, i0, end, broken: z.broken }); });
      }
      this.markIdx = (d.marks || []).map(m => ({ ...m, i0: this.idxOf(m.in), i1: this.idxOf(m.out) }));
      const ov = d.overlay || {};
      this.ovLines = (ov.lines || []).map(L => ({ ...L, ix: L.pts.map(([t, v]) => [this.idxOf(t), v, t]) }));
      this.ovRanges = (ov.ranges || []).map(r => ({ ...r, i0: this.idxOf(r.t0), i1: this.idxOf(r.t1), i2: this.idxOf(r.t2) }));
      if (!keepView || !this.view) this.fit(); else this.draw();
    }
    idxOf(t) { // last bar whose time <= t
      const B = this.d.bars; let lo = 0, hi = B.length - 1, r = 0; t = t.slice(0, 16);
      while (lo <= hi) { const m = (lo + hi) >> 1; if (B[m][0] <= t) { r = m; lo = m + 1; } else hi = m - 1; }
      return r;
    }

    draw() {
      const cv = this.cv, d = this.d; if (!d || !this.view) return;
      const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
      if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
      const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.fillStyle = COL.bg; g.fillRect(0, 0, W, H);
      g.font = '11px Inter, system-ui, sans-serif'; g.textBaseline = 'middle';
      const B = d.bars, n = this.count(), v = this.view, pl = 6, pr = 64, pw = W - pl - pr;
      const cvdH = d.showCVD ? 78 : 0, wtH = this.wt ? 96 : 0, xh = 22, gap = 8;
      const mainH = H - cvdH - wtH - xh - (cvdH ? gap : 0) - (wtH ? gap : 0), top = 8;
      const X = i => pl + (i - v.a + 0.5) / (v.b - v.a) * pw, bw = Math.max(1, pw / (v.b - v.a) * 0.68);
      const a = Math.max(0, Math.floor(v.a)), b = Math.min(n, Math.ceil(v.b));
      if (b <= a) return;
      let lo = Infinity, hi = -Infinity;
      for (let i = a; i < b; i++) { lo = Math.min(lo, B[i][3]); hi = Math.max(hi, B[i][2]); }
      const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
      const Y = p => top + (hi - p) / (hi - lo) * (mainH - top);
      // grid + price axis
      const stepP = niceStep((hi - lo) / 7);
      g.strokeStyle = COL.grid; g.lineWidth = 1; g.fillStyle = COL.axis; g.textAlign = 'left';
      for (let p = Math.ceil(lo / stepP) * stepP; p <= hi; p += stepP) { const y = Math.round(Y(p)) + 0.5; g.beginPath(); g.moveTo(pl, y); g.lineTo(W - pr, y); g.stroke(); g.fillText(fmt(p, stepP < 1 ? 2 : 0), W - pr + 6, y); }
      g.save(); g.beginPath(); g.rect(pl, 0, pw, mainH); g.clip();
      // overnight shading + session separators
      for (let i = a; i < b; i++) {
        const t = B[i][0].slice(11, 16), rth = B[i][0].slice(0, 10) === B[i][6] && t >= '09:30' && t < '16:00';
        if (!rth) { g.fillStyle = COL.night; g.fillRect(X(i) - pw / (v.b - v.a) / 2, 0, pw / (v.b - v.a) + 0.5, mainH); }
        if (i > 0 && B[i][6] !== B[i - 1][6]) { g.strokeStyle = COL.sep; g.setLineDash([2, 3]); g.beginPath(); g.moveTo(X(i) - 2, 0); g.lineTo(X(i) - 2, H - xh); g.stroke(); g.setLineDash([]); }
      }
      // volume profile of the visible bars
      if (d.showProfile) {
        const nb = 48, step = (hi - lo) / nb, prof = new Array(nb).fill(0);
        for (let i = a; i < b; i++) { const k0 = Math.max(0, Math.floor((B[i][3] - lo) / step)), k1 = Math.min(nb - 1, Math.floor((B[i][2] - lo) / step)); for (let k = k0; k <= k1; k++) prof[k] += B[i][5] / (k1 - k0 + 1); }
        const mx = Math.max(...prof), poc = prof.indexOf(mx);
        for (let k = 0; k < nb; k++) { const w = prof[k] / mx * pw * 0.18; g.fillStyle = k === poc ? COL.poc : COL.prof; g.fillRect(W - pr - w, Y(lo + (k + 1) * step), w, Math.max(1, (mainH - top) / nb - 1)); }
      }
      // prior day / overnight levels, per session
      const sessRange = {};
      for (let i = a; i < b; i++) { const s = B[i][6]; (sessRange[s] = sessRange[s] || [i, i])[1] = i; }
      Object.entries(sessRange).forEach(([s, [i0, i1]]) => {
        const lv = (d.levels || {})[s] || {};
        [['pdh', 'PDH', COL.lvl], ['pdl', 'PDL', COL.lvl], ['pdc', 'PDC', COL.lvl], ['onh', 'ONH', COL.on], ['onl', 'ONL', COL.on]].forEach(([k, lab, c]) => {
          if (lv[k] == null || lv[k] < lo || lv[k] > hi) return;
          const y = Math.round(Y(lv[k])) + 0.5; g.strokeStyle = c; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(X(i0), y); g.lineTo(X(i1), y); g.stroke(); g.setLineDash([]);
          g.fillStyle = c; g.textAlign = 'left'; g.fillText(lab, X(i0) + 3, y - 7);
        });
      });
      // the strategy's own lines: opening ranges (box, then dashed to the last entry time) and its EMA
      (this.ovRanges || []).forEach(r => {
        if (r.i2 < a || r.i0 >= b) return;
        g.fillStyle = 'rgba(231,195,90,0.07)'; g.fillRect(X(r.i0) - bw / 2, Y(r.hi), X(r.i1) - X(r.i0) + bw, Y(r.lo) - Y(r.hi));
        g.strokeStyle = 'rgba(231,195,90,0.6)'; g.setLineDash([5, 4]);
        [r.hi, r.lo].forEach(p => { g.beginPath(); g.moveTo(X(r.i0) - bw / 2, Y(p) + 0.5); g.lineTo(X(r.i2), Y(p) + 0.5); g.stroke(); });
        g.setLineDash([]); g.fillStyle = 'rgba(231,195,90,0.85)'; g.textAlign = 'left'; g.fillText('OR', X(r.i0), Y(r.hi) - 7);
      });
      (this.ovLines || []).forEach(L => {
        g.strokeStyle = L.color || '#E7C35A'; g.lineWidth = 1.5; g.beginPath(); let on = false, prevDay = null;
        L.ix.forEach(([i, v, t]) => { if (i < a - 1 || i > b || i >= n) { on = false; return; } if (t.slice(0, 10) !== prevDay) on = false; prevDay = t.slice(0, 10);
          on ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v)); on = true; });
        g.stroke(); g.lineWidth = 1;
      });
      // supply / demand zones and fair value gaps: from where they formed until broken / filled (or the right edge)
      this.zones.forEach(z => {
        const end = z.end == null || z.end >= n ? n - 1 + 0.5 : z.end;
        if (end < a || z.i0 >= b || z.i0 >= n) return;
        const x0 = X(z.i0) - bw / 2, x1 = X(Math.min(end, b)), y0 = Y(z.hi), y1 = Y(z.lo), open = z.broken === false || z.end == null || z.end >= n;
        const c = ZC[z.k];
        g.fillStyle = open ? c[0] : c[2]; g.fillRect(x0, y0, Math.max(2, x1 - x0), Math.max(1, y1 - y0));
        if (open) { g.strokeStyle = c[1]; g.strokeRect(x0 + 0.5, y0 + 0.5, Math.max(2, x1 - x0), Math.max(1, y1 - y0)); }
        if (x1 - x0 > 40 && y1 - y0 > 9) { g.fillStyle = c[1]; g.textAlign = 'left'; g.font = '10px Inter, system-ui, sans-serif'; g.fillText(z.label, x0 + 3, y0 + 7); g.font = '11px Inter, system-ui, sans-serif'; }
      });
      // EMA indicator (on the chart's candles)
      if (this.ema) {
        g.strokeStyle = '#5FD4C4'; g.lineWidth = 1.4; g.beginPath();
        for (let i = a; i < b; i++) { const x = X(i), y = Y(this.ema[i]); i === a ? g.moveTo(x, y) : g.lineTo(x, y); }
        g.stroke(); g.lineWidth = 1;
      }
      // VWAP
      g.strokeStyle = COL.vwap; g.lineWidth = 1.4; g.beginPath(); let st = false;
      for (let i = a; i < b; i++) { if (i > 0 && B[i][6] !== B[i - 1][6]) st = false; const x = X(i), y = Y(this.vw[i]); st ? g.lineTo(x, y) : g.moveTo(x, y); st = true; }
      g.stroke(); g.lineWidth = 1;
      // trades: bracket box, entry / exit
      this.markIdx.forEach(m => {
        if (m.i0 >= b || m.i1 < a || m.i0 >= n) return;
        const i1 = Math.min(m.i1, n - 1), win = (m.side === 'long' ? m.x - m.e : m.e - m.x) > 0, open = m.i1 >= n;
        const xx = open ? B[n - 1][4] : m.x;
        g.fillStyle = win ? 'rgba(74,158,255,0.13)' : 'rgba(240,140,46,0.15)';
        g.fillRect(X(m.i0), Math.min(Y(m.e), Y(xx)), Math.max(2, X(i1) - X(m.i0)), Math.max(2, Math.abs(Y(m.e) - Y(xx))));
        const ln = (p, c) => { if (p == null) return; g.strokeStyle = c; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X(m.i0), Y(p)); g.lineTo(X(i1), Y(p)); g.stroke(); g.setLineDash([]); };
        ln(m.stop, COL.dnT); ln(m.tgt, COL.upT);
        if (m.lv) { ln(m.lv.top, COL.lvl); ln(m.lv.bot, COL.lvl); }
        tri(g, X(m.i0), m.side === 'long' ? Y(B[m.i0][3]) + 10 : Y(B[m.i0][2]) - 10, m.side === 'long', COL.upT);
        [['bk', 'B'], ['rt', 'R'], ['cf', 'C']].forEach(([k, lab]) => {   // the setup's steps: breakout, retest, confirmation
          if (!m[k]) return; const i = this.idxOf(m[k]); if (i < a || i >= b || i >= n) return;
          const y = m.side === 'long' ? Y(B[i][2]) - 12 : Y(B[i][3]) + 12;
          g.fillStyle = '#E7C35A'; g.font = '600 10px Inter, system-ui, sans-serif'; g.textAlign = 'center'; g.fillText(lab, X(i), y); g.font = '11px Inter, system-ui, sans-serif';
        });
        if (!open) { g.fillStyle = COL.dnT; g.fillRect(X(m.i1) - 3.5, Y(m.x) - 3.5, 7, 7); }
      });
      // candles
      for (let i = a; i < b; i++) {
        const [, o, h, l, c] = B[i], col = c >= o ? COL.up : COL.dn, x = X(i);
        g.fillStyle = col; g.fillRect(Math.round(x) - 0.5, Y(h), 1, Math.max(1, Y(l) - Y(h)));
        g.fillRect(x - bw / 2, Y(Math.max(o, c)), bw, Math.max(1, Math.abs(Y(o) - Y(c))));
      }
      g.restore();
      // last price tag
      const last = B[n - 1][4]; g.fillStyle = COL.text; const ly = Y(last);
      if (ly > 0 && ly < mainH) { g.fillRect(W - pr + 1, ly - 8, pr - 2, 16); g.fillStyle = '#0B0E13'; g.font = '600 11px JetBrains Mono, monospace'; g.fillText(fmt(last), W - pr + 5, ly); g.font = '11px Inter, system-ui, sans-serif'; }
      // CVD pane
      let yb = mainH + gap;
      if (cvdH) {
        let cl = Infinity, ch = -Infinity; for (let i = a; i < b; i++) { cl = Math.min(cl, this.cvd[i]); ch = Math.max(ch, this.cvd[i]); }
        cl = Math.min(cl, 0); ch = Math.max(ch, 0); const r = ch - cl || 1, CY = val => yb + 4 + (ch - val) / r * (cvdH - 8);
        g.strokeStyle = COL.sep; g.beginPath(); g.moveTo(pl, CY(0) + 0.5); g.lineTo(W - pr, CY(0) + 0.5); g.stroke();
        for (let i = a; i < b; i++) { const val = this.cvd[i]; g.fillStyle = val >= 0 ? COL.up : COL.dn; g.fillRect(X(i) - bw / 2, Math.min(CY(val), CY(0)), bw, Math.max(1, Math.abs(CY(val) - CY(0)))); }
        { const lab = this.cvdReal ? 'Cumulative delta (trades)' : 'Cumulative delta (estimated from candles)'; g.fillStyle = 'rgba(18,22,29,.85)'; g.fillRect(pl + 1, yb + 1, g.measureText(lab).width + 8, 16); g.fillStyle = COL.axis; g.textAlign = 'left'; g.fillText(lab, pl + 4, yb + 9); }
        g.fillText(fmt(this.cvd[n - 1], 0), W - pr + 6, CY(this.cvd[n - 1]));
        yb += cvdH + gap;
      }
      if (wtH) {
        const WY = val => yb + 4 + (1.1 - Math.max(-1.1, Math.min(1.1, val))) / 2.2 * (wtH - 8);
        [[0.75, 'rgba(74,158,255,.35)'], [0, COL.sep], [-0.75, 'rgba(240,140,46,.35)']].forEach(([lv, c]) => { g.strokeStyle = c; g.setLineDash(lv ? [] : [2, 3]); g.beginPath(); g.moveTo(pl, WY(lv) + 0.5); g.lineTo(W - pr, WY(lv) + 0.5); g.stroke(); g.setLineDash([]); g.fillStyle = COL.axis; g.fillText(String(lv), W - pr + 6, WY(lv)); });
        [['sig', '#4A9EFF'], ['ma', '#F5A54A']].forEach(([k, c]) => { g.strokeStyle = c; g.lineWidth = 1.4; g.beginPath(); let s = false; for (let i = a; i < b; i++) { const val = this.wt[k][i]; if (!Number.isFinite(val)) { s = false; continue; } s ? g.lineTo(X(i), WY(val)) : g.moveTo(X(i), WY(val)); s = true; } g.stroke(); g.lineWidth = 1; });
        for (let i = a; i < b; i++) { if (this.wt.buy[i] || this.wt.sell[i]) { g.fillStyle = this.wt.buy[i] ? '#4A9EFF' : '#F0563C'; g.beginPath(); g.arc(X(i), WY(this.wt.sig[i - 1] ?? this.wt.sig[i]) + (this.wt.buy[i] ? 6 : -6), 3.5, 0, 7); g.fill(); } }
        g.fillStyle = COL.axis; g.textAlign = 'left'; g.fillText('WaveTrend [Krypt] · blue = signal, orange = MA, dots = buy / sell', pl + 4, yb + 9);
        yb += wtH + gap;
      }
      // time axis
      g.fillStyle = COL.axis; g.textAlign = 'center';
      const every = Math.max(1, Math.ceil((b - a) / Math.max(2, pw / 90)));
      for (let i = a; i < b; i++) {
        if ((i % every) !== 0) continue;
        const t = B[i][0]; const lab = d.tf >= 1440 ? t.slice(5, 10) : (i === a || B[i][6] !== B[Math.max(0, i - every)][6] ? t.slice(5, 10) + ' ' : '') + t.slice(11, 16);
        g.fillText(lab, X(i), H - xh / 2);
      }
      // crosshair
      if (this.hover && this.hover.x < W - pr) {
        const i = Math.round(this.xToIdx(this.hover.x) - 0.5);
        if (i >= a && i < b) {
          g.strokeStyle = 'rgba(231,234,240,.25)'; g.beginPath(); g.moveTo(X(i) + 0.5, 0); g.lineTo(X(i) + 0.5, H - xh); g.stroke();
          if (this.hover.y < mainH) { g.beginPath(); g.moveTo(pl, this.hover.y + 0.5); g.lineTo(W - pr, this.hover.y + 0.5); g.stroke();
            const p = hi - (this.hover.y - top) / (mainH - top) * (hi - lo); g.fillStyle = '#2C3442'; g.fillRect(W - pr + 1, this.hover.y - 8, pr - 2, 16); g.fillStyle = COL.text; g.textAlign = 'left'; g.fillText(fmt(p), W - pr + 5, this.hover.y); }
          const [t, o, h, l, c, vol] = B[i];
          const txt = `${t.replace('T', ' ')}   O ${fmt(o)}  H ${fmt(h)}  L ${fmt(l)}  C ${fmt(c)}  V ${vol.toLocaleString('en-US')}   VWAP ${fmt(this.vw[i])}${this.ema ? `   EMA ${this.d.emaLen || 20} ${fmt(this.ema[i])}` : ''}`;
          g.font = '12px JetBrains Mono, monospace'; const tw = g.measureText(txt).width;
          g.fillStyle = 'rgba(15,19,26,.92)'; g.fillRect(pl + 4, 4, tw + 12, 20); g.fillStyle = COL.text; g.textAlign = 'left'; g.fillText(txt, pl + 10, 14);
          g.font = '11px Inter, system-ui, sans-serif';
          if (this.opts.onHover) this.opts.onHover(i);
        }
      }
    }
  }
  // zone colors: [open fill, line / label, filled or broken fill]
  const ZC = { demand: ['rgba(74,158,255,.13)', 'rgba(125,184,255,.85)', 'rgba(74,158,255,.05)'], supply: ['rgba(240,140,46,.14)', 'rgba(245,165,74,.9)', 'rgba(240,140,46,.05)'],
    fvgUp: ['rgba(95,212,196,.16)', 'rgba(95,212,196,.9)', 'rgba(95,212,196,.05)'], fvgDn: ['rgba(240,86,60,.15)', 'rgba(240,120,100,.9)', 'rgba(240,86,60,.05)'] };
  // Fair value gaps: candle i-2's high under candle i's low (bullish) or the mirror, at least 0.1 x the 14-candle average range;
  // filled when price trades back through the whole gap. Supply / demand: a base (1-3 small candles, each under 0.7 x the
  // average range) right before an impulse candle (range >= 2 x average, body >= 60% of it); the zone is the base's range,
  // broken when a candle closes through its far side.
  function zones(B, fvg, sd) {
    const n = B.length, atr = []; let a = null;
    for (let i = 0; i < n; i++) { const pc = i ? B[i - 1][4] : B[i][1], tr = Math.max(B[i][2] - B[i][3], Math.abs(B[i][2] - pc), Math.abs(B[i][3] - pc)); a = a == null ? tr : (a * 13 + tr) / 14; atr.push(a); }
    const out = [];
    for (let i = 2; i < n; i++) {
      const at = atr[i - 1];
      if (fvg) {
        if (B[i - 2][2] < B[i][3] && B[i][3] - B[i - 2][2] >= 0.1 * at) out.push({ k: 'fvgUp', label: 'FVG', lo: B[i - 2][2], hi: B[i][3], i0: i - 1, test: (b) => b[3] <= B[i - 2][2], from: i + 1 });
        if (B[i - 2][3] > B[i][2] && B[i - 2][3] - B[i][2] >= 0.1 * at) out.push({ k: 'fvgDn', label: 'FVG', lo: B[i][2], hi: B[i - 2][3], i0: i - 1, test: (b) => b[2] >= B[i - 2][3], from: i + 1 });
      }
      if (sd) {
        const [, o, h, l, c] = B[i], r = h - l;
        if (r >= 2 * at && Math.abs(c - o) >= 0.6 * r) {
          let k = i - 1, lo = Infinity, hi = -Infinity, cnt = 0;
          while (k >= 0 && cnt < 3 && B[k][2] - B[k][3] <= 0.7 * at) { lo = Math.min(lo, B[k][3]); hi = Math.max(hi, B[k][2]); k--; cnt++; }
          if (cnt) {
            const up = c > o;
            out.push(up ? { k: 'demand', label: 'Demand', lo, hi, i0: k + 1, test: (b) => b[4] < lo, from: i + 1 }
                        : { k: 'supply', label: 'Supply', lo, hi, i0: k + 1, test: (b) => b[4] > hi, from: i + 1 });
          }
        }
      }
    }
    out.forEach(z => { z.end = null; for (let j = z.from; j < n; j++) if (z.test(B[j])) { z.end = j; break; } });
    return out;
  }

  // Order book heatmap (Bookmap style): x = time, y = price, color = resting size at that price. The book (10 levels per
  // side, MBP-10) is carried forward: a level keeps its last known size after it leaves the 10 shown levels, until price
  // trades through it or it comes back with a new size. Volume dots (green bought / red sold, sized by volume), the last
  // trade line, large lots, the current order book (COB) on the right and the cumulative volume delta underneath.
  const BM = [[10, 26, 48], [14, 45, 85], [22, 72, 128], [40, 112, 170], [90, 165, 205], [175, 215, 225], [250, 235, 110], [255, 185, 40], [250, 115, 20], [225, 35, 25]];
  function bmColor(v) { v = Math.max(0, Math.min(1, v)) * (BM.length - 1); const i = Math.floor(v), f = v - i, a = BM[i], b = BM[Math.min(BM.length - 1, i + 1)];
    return `rgb(${a[0] + (b[0] - a[0]) * f | 0},${a[1] + (b[1] - a[1]) * f | 0},${a[2] + (b[2] - a[2]) * f | 0})`; }
  class Heat {
    constructor(canvas) { this.cv = canvas; this.ro = new ResizeObserver(() => this.draw()); this.ro.observe(canvas); }
    destroy() { this.ro.disconnect(); }
    set(o) { this.o = o; this.draw(); }   // {hm, big, tEnd (seconds of the day), span (seconds), last, rows, bigMin}
    states(t0, t1) {
      // the carried-forward book at each column of [t0, t1): [[sec, Map(idx -> size) bids, Map asks, col]]
      const hm = this.o.hm, out = [], bids = new Map(), asks = new Map(), warm = t0 - 600;
      for (const c of hm.c) {
        if (c[0] < warm) continue;
        if (c[0] >= t1) break;
        const bi = c[1], ai = c[2];
        if (bi.length) {
          const best = bi[0], deep = bi[bi.length - 2], seen = new Set();
          for (let k = 0; k < bi.length; k += 2) { bids.set(bi[k], bi[k + 1]); seen.add(bi[k]); }
          for (const idx of [...bids.keys()]) if (idx > best || (idx >= deep && idx <= best && !seen.has(idx))) bids.delete(idx);
        }
        if (ai.length) {
          const best = ai[0], deep = ai[ai.length - 2], seen = new Set();
          for (let k = 0; k < ai.length; k += 2) { asks.set(ai[k], ai[k + 1]); seen.add(ai[k]); }
          for (const idx of [...asks.keys()]) if (idx < best || (idx <= deep && idx >= best && !seen.has(idx))) asks.delete(idx);
        }
        if (c[0] >= t0 - hm.step) out.push([c[0], new Map(bids), new Map(asks), c]);
      }
      return out;
    }
    draw() {
      const o = this.o, cv = this.cv; if (!o || !o.hm) return;
      const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
      if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
      const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0);
      const hm = o.hm, tick = hm.tick, base = hm.base, axisW = 66, cobW = 86, cvdH = 74, timeH = 18;
      const pw = W - axisW - cobW, ph = H - cvdH - timeH, t1 = o.tEnd, t0 = t1 - o.span;
      g.fillStyle = 'rgb(8,18,32)'; g.fillRect(0, 0, W, H);
      g.font = '11px JetBrains Mono, monospace'; g.textBaseline = 'middle';
      const rows = o.rows || 120, mid = Math.round(o.last / tick) - base, top = mid + rows / 2, rh = ph / rows;
      const Y = idx => (top - idx) * rh, X = sec => (sec - t0) / o.span * pw;
      const st = this.states(t0, t1);
      if (!st.length) { g.fillStyle = '#93A0B2'; g.textAlign = 'center'; g.fillText('No book data at this time', pw / 2, ph / 2); return; }
      // color scale: the 98th percentile of the sizes on screen
      const sizes = [];
      st.forEach(([, b, a]) => { b.forEach((v, k) => { if (k <= top && k >= top - rows) sizes.push(v); }); a.forEach((v, k) => { if (k <= top && k >= top - rows) sizes.push(v); }); });
      sizes.sort((x, y) => x - y);
      const mx = Math.max(1, sizes[Math.floor(sizes.length * (o.pct || 0.995))] || 1), C = v => Math.pow(Math.min(1, v / mx), 0.75);   // linear-ish: the walls stand out
      const cw = Math.max(1, hm.step / o.span * pw) + 0.6;
      g.save(); g.beginPath(); g.rect(0, 0, pw, ph); g.clip();
      st.forEach(([sec, b, a]) => { const x = X(sec);
        b.forEach((v, k) => { const y = Y(k); if (y < -rh || y > ph + rh) return; g.fillStyle = bmColor(C(v)); g.fillRect(x, y - rh / 2, cw, rh + 0.5); });
        a.forEach((v, k) => { const y = Y(k); if (y < -rh || y > ph + rh) return; g.fillStyle = bmColor(C(v)); g.fillRect(x, y - rh / 2, cw, rh + 0.5); }); });
      // best bid / ask edges
      [[1, 'rgba(80,220,120,.9)'], [2, 'rgba(240,80,70,.9)']].forEach(([s, col]) => { g.strokeStyle = col; g.lineWidth = 1; g.beginPath(); let on = false;
        st.forEach(([sec, , , c]) => { if (!c[s].length) { on = false; return; } const y = Y(c[s][0]) + (s === 1 ? rh / 2 : -rh / 2), x = X(sec); on ? g.lineTo(x, y) : g.moveTo(x, y); on = true; g.lineTo(x + cw, y); }); g.stroke(); });
      // last trade line
      g.strokeStyle = 'rgba(255,255,255,.85)'; g.lineWidth = 1.4; g.beginPath(); let on = false;
      st.forEach(([sec, , , c]) => { if (c[6] == null) return; const x = X(sec) + cw / 2, y = Y(c[6]); on ? g.lineTo(x, y) : g.moveTo(x, y); on = true; }); g.stroke(); g.lineWidth = 1;
      // volume dots: trades grouped in slices, at their VWAP, green share = bought
      const slice = Math.max(1, Math.round(o.span / 160 / hm.step)), dots = [];
      for (let i = 0; i < st.length; i += slice) {
        let bv = 0, sv = 0, pv = 0;
        for (let j = i; j < Math.min(st.length, i + slice); j++) { const c = st[j][3]; if (c[3] == null) continue; bv += c[3]; sv += c[4]; if (c[5] != null) pv += c[5] * (c[3] + c[4]); }
        if (bv + sv) dots.push([X(st[i][0]) + slice * cw / 2, Y(pv / (bv + sv)), bv, sv]);
      }
      const vmax = Math.max(1, ...dots.map(d => d[2] + d[3]));
      dots.forEach(([x, y, bv, sv]) => { const v = bv + sv, r = 2 + 13 * Math.sqrt(v / vmax);
        g.fillStyle = 'rgba(225,60,55,.85)'; g.beginPath(); g.arc(x, y, r, 0, 7); g.fill();
        if (bv) { g.fillStyle = 'rgba(60,200,110,.9)'; g.beginPath(); g.moveTo(x, y); g.arc(x, y, r, -Math.PI / 2, -Math.PI / 2 + 2 * Math.PI * bv / v); g.closePath(); g.fill(); }
        g.strokeStyle = 'rgba(10,20,30,.6)'; g.beginPath(); g.arc(x, y, r, 0, 7); g.stroke(); });
      // large lots: rings with the size
      (o.big || []).forEach(([t, p, sz, side]) => { const sec = +t.slice(0, 2) * 3600 + +t.slice(3, 5) * 60 + +t.slice(6, 8); if (sec < t0 || sec >= t1) return;
        const x = X(sec), y = Y(Math.round(p / tick) - base); g.strokeStyle = side === 'B' ? '#7CF0A8' : '#FF8A80'; g.lineWidth = 2; g.beginPath(); g.arc(x, y, 5 + Math.sqrt(sz), 0, 7); g.stroke(); g.lineWidth = 1;
        g.fillStyle = '#FFFFFF'; g.textAlign = 'left'; g.font = '600 10px JetBrains Mono, monospace'; g.fillText(sz, x + 7 + Math.sqrt(sz), y); g.font = '11px JetBrains Mono, monospace'; });
      g.restore();
      // COB: the current (carried) book, asks red above, bids green below
      const [, cb, ca, lc] = st[st.length - 1];
      g.fillStyle = 'rgb(12,22,36)'; g.fillRect(pw, 0, cobW, ph);
      let m2 = 1; [cb, ca].forEach(m => m.forEach((v, k) => { if (k <= top && k >= top - rows) m2 = Math.max(m2, v); }));
      g.textAlign = 'right'; g.font = '10px JetBrains Mono, monospace';
      [[ca, 'rgba(232,73,74,.85)'], [cb, 'rgba(60,200,110,.85)']].forEach(([m, col]) => m.forEach((v, k) => { const y = Y(k); if (y < 0 || y > ph) return;
        g.fillStyle = col; g.fillRect(pw + 2, y - rh / 2 + 0.5, v / m2 * (cobW - 30), Math.max(1, rh - 1));
        if (rh >= 9) { g.fillStyle = '#E7EAF0'; g.fillText(v, pw + cobW - 3, y); } }));
      g.font = '11px JetBrains Mono, monospace';
      // price axis
      g.fillStyle = 'rgb(10,16,26)'; g.fillRect(pw + cobW, 0, axisW, ph);
      g.fillStyle = '#93A0B2'; g.textAlign = 'left'; const every = Math.max(1, Math.round(18 / rh));
      for (let i = Math.floor(top); i > top - rows; i--) { if ((i + base) % every) continue; g.fillText(fmt((i + base) * tick), pw + cobW + 5, Y(i)); }
      const lastIdx = lc[6] != null ? lc[6] : mid;
      g.fillStyle = '#E7EAF0'; g.fillRect(pw + cobW + 1, Y(lastIdx) - 8, axisW - 2, 16); g.fillStyle = '#0B0E13'; g.fillText(fmt((lastIdx + base) * tick), pw + cobW + 4, Y(lastIdx));
      // cumulative volume delta (from the first column of the download)
      let cvd = 0; const cv_ = []; const first = st[0][0];
      for (const c of hm.c) { if (c[0] >= t1) break; if (c[3] != null) cvd += c[3] - c[4]; if (c[0] >= first) cv_.push([c[0], cvd]); }
      const y0 = ph + timeH, lo = Math.min(0, ...cv_.map(x => x[1])), hi = Math.max(0, ...cv_.map(x => x[1])), CY = v => y0 + 6 + (hi - v) / (hi - lo || 1) * (cvdH - 12);
      g.fillStyle = 'rgb(10,16,26)'; g.fillRect(0, y0, W, cvdH);
      g.strokeStyle = '#2C3442'; g.beginPath(); g.moveTo(0, CY(0) + 0.5); g.lineTo(pw, CY(0) + 0.5); g.stroke();
      g.strokeStyle = '#F5A54A'; g.lineWidth = 1.5; g.beginPath(); cv_.forEach(([sec, v], i) => { const x = X(sec), y = CY(v); i ? g.lineTo(x, y) : g.moveTo(x, y); }); g.stroke(); g.lineWidth = 1;
      g.fillStyle = '#93A0B2'; g.textAlign = 'left'; g.fillText('CVD', 6, y0 + 10);
      g.textAlign = 'right'; g.fillStyle = cvd >= 0 ? '#7CF0A8' : '#FF8A80'; g.fillText((cvd > 0 ? '+' : '') + cvd.toLocaleString('en-US'), pw + cobW - 4, y0 + cvdH / 2);
      // time axis
      g.fillStyle = '#93A0B2'; g.textAlign = 'center';
      for (let k = 0; k <= 5; k++) { const sec = t0 + o.span * k / 5; g.fillText(`${String(Math.floor(sec / 3600)).padStart(2, '0')}:${String(Math.floor(sec / 60) % 60).padStart(2, '0')}:${String(Math.floor(sec) % 60).padStart(2, '0')}`, Math.min(pw - 30, Math.max(30, X(sec))), ph + timeH / 2); }
    }
  }
  function tri(g, x, y, up, c) { g.fillStyle = c; g.beginPath(); if (up) { g.moveTo(x, y - 6); g.lineTo(x - 5, y + 3); g.lineTo(x + 5, y + 3); } else { g.moveTo(x, y + 6); g.lineTo(x - 5, y - 3); g.lineTo(x + 5, y - 3); } g.fill(); }
  function niceStep(r) { const p = Math.pow(10, Math.floor(Math.log10(r))); const m = r / p; return (m < 1.5 ? 1 : m < 3.5 ? 2.5 : m < 7.5 ? 5 : 10) * p; }
  function toMin(t) { return +t.slice(11, 13) * 60 + +t.slice(14, 16); }
  function minStr(m) { return String(Math.floor(m / 60) % 24).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0'); }

  window.FC = { Chart, Heat, wtKrypt, fmt, toMin, minStr, zones };
})();
