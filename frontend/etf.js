// ETF: correlation matrix of daily returns (sector / industry / commodity / country ETFs; no index, no bond fund).
S.etf = S.etf || { days: 60, sort: 'group', tab: 'corr', cfg: { look: 40, entry: 2, exit: 0, stop: 0, hold: 20, minCorr: 0.7, stake: 1000, costBps: 5, borrowPct: 1 } };

async function vEtf() {
  const E = S.etf;
  const tabs = [['corr', 'Correlation'], ['pairs', 'Pairs strategy']].map(([k, t]) => `<button class="btn${E.tab === k ? ' primary' : ''}" data-etf-tab="${k}">${t}</button>`).join(' ');
  view(`${head('ETF', 'How the ETFs move together, and a pairs strategy that trades it. No broad index, no bond fund.')}<div style="margin-bottom:12px">${tabs}</div><div id="etf-body"><div class="panel"><div class="empty">Loading prices…</div></div></div>`);
  if (E.tab === 'pairs') { $('#etf-body').innerHTML = etfPairsShell(); etfOptLoad(); return; }
  let r;
  try { r = await api('/etf?days=' + E.days); }
  catch (e) { $('#etf-body').innerHTML = `<div class="panel"><div class="bd loss">${esc(e.message)}</div></div>`; return; }
  E.r = r;
  $('#etf-body').innerHTML = etfHtml(r);
}

function etfColor(c) {                                 // blue = move together, red = opposite, grey = unrelated
  if (c == null) return 'transparent';
  const a = Math.min(1, Math.abs(c)) * 0.85;
  return c >= 0 ? `rgba(59,130,246,${a.toFixed(2)})` : `rgba(239,68,68,${a.toFixed(2)})`;
}

function etfHtml(r) {
  const E = S.etf, n = r.syms.length;
  let idx = [...Array(n).keys()];
  if (E.sort === 'group') idx = r.order;
  else if (E.sort === 'avg') idx.sort((a, b) => r.avg[r.syms[b]] - r.avg[r.syms[a]]);
  const pc = x => (x >= 0 ? '+' : '') + (x * 100).toFixed(1) + '%';
  const tabs = r.windows.map(d => `<button class="btn sm${E.days === d ? ' primary' : ''}" data-etf-days="${d}">${d} days</button>`).join('');
  const sorts = [['group', 'Group similar'], ['avg', 'By average correlation'], ['list', 'List order']]
    .map(([k, t]) => `<button class="btn sm${E.sort === k ? ' primary' : ''}" data-etf-sort="${k}">${t}</button>`).join('');
  const headRow = idx.map(i => `<th class="r mono" style="writing-mode:vertical-rl;transform:rotate(180deg);padding:6px 2px">${esc(r.syms[i])}</th>`).join('');
  const body = idx.map(i => `<tr><th class="mono" style="text-align:left;white-space:nowrap">${esc(r.syms[i])}</th>${idx.map(j => {
    const c = r.matrix[i][j];
    return `<td class="r mono" title="${esc(r.syms[i])} vs ${esc(r.syms[j])}: ${c == null ? '—' : c.toFixed(2)}" style="background:${i === j ? 'var(--line)' : etfColor(c)};font-size:12px;padding:6px 4px;min-width:40px">${i === j ? '' : c == null ? '—' : c.toFixed(2)}</td>`;
  }).join('')}<td class="r mono">${r.avg[r.syms[i]].toFixed(2)}</td><td class="r mono ${r.perf[r.syms[i]] >= 0 ? 'gain' : 'loss'}">${pc(r.perf[r.syms[i]])}</td><td class="r mono">${(r.vol[r.syms[i]] * 100).toFixed(0)}%</td></tr>`).join('');
  const pairs = [];
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) if (r.matrix[i][j] != null) pairs.push([r.syms[i], r.syms[j], r.matrix[i][j]]);
  pairs.sort((a, b) => b[2] - a[2]);
  const plist = xs => xs.map(([a, b, c]) => `<span class="mono">${esc(a)}–${esc(b)}</span> <b>${c.toFixed(2)}</b>`).join(' · ');
  return `<section class="panel"><div class="hd"><h2>Correlation matrix</h2><span class="dim" style="font-size:13px">${r.days} daily returns, ${esc(r.from)} → ${esc(r.to)}</span><div class="grow"></div>${tabs}</div>
    <div class="bd" style="padding-top:0;display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span class="dim" style="font-size:12px">Order:</span>${sorts}
      <span class="dim" style="font-size:12px;margin-left:12px">1 = move together · 0 = unrelated · −1 = opposite</span></div>
    <div class="tbl-wrap"><table class="tbl" style="width:auto"><thead><tr><th></th>${headRow}<th class="r">Avg</th><th class="r">Return</th><th class="r">Vol (yr)</th></tr></thead><tbody>${body}</tbody></table></div>
    <div class="note" style="padding:8px 16px 4px"><b>Most together:</b> ${plist(pairs.slice(0, 5))}</div>
    <div class="note" style="padding:0 16px 14px"><b>Least together:</b> ${plist(pairs.slice(-5).reverse())}${r.missing.length ? `<br><span class="loss">No prices: ${esc(r.missing.join('; '))}</span>` : ''}</div></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>ETFs</h2><span class="dim" style="font-size:13px">one ticker per line or comma-separated (2–40)</span></div>
      <div class="bd"><textarea id="etf-syms" rows="3" style="width:100%" class="mono">${esc(r.settings.syms.join(', '))}</textarea>
      <div style="margin-top:8px;display:flex;gap:8px"><button class="btn primary" data-etf="save">Save and recompute</button><button class="btn" data-etf="reset">Default list</button></div></div></section>`;
}

document.addEventListener('click', async ev => {
  const el = ev.target.closest('[data-etf-days],[data-etf-sort],[data-etf],[data-etf-tab],[data-etf-use]');
  if (!el || !$('#etf-body')) return;
  const ds = el.dataset;
  try {
    if (ds.etfTab) { S.etf.tab = ds.etfTab; return vEtf(); }
    if (ds.etfUse) { Object.assign(S.etf.cfg, S.etf.opt.top[+ds.etfUse].p); $('#etf-body').innerHTML = etfPairsShell(); etfOptLoad(); toast('Setting applied: run the backtest.'); return; }
    if (ds.etf === 'run') {
      el.disabled = true; $('#etf-res').innerHTML = '<div class="panel"><div class="empty">Running every pair…</div></div>';
      const r = await api('/etf/pairs', { method: 'POST', body: { cfg: S.etf.cfg } }); S.etf.res = r; $('#etf-res').innerHTML = etfPairsHtml(r); el.disabled = false; return;
    }
    if (ds.etf === 'opt') { el.disabled = true; await api('/etf/pairs/opt', { method: 'POST', body: { cfg: S.etf.cfg } }); toast('Optimizer started.'); setTimeout(etfOptLoad, 1500); return; }
    if (ds.etf === 'csv') {
      const r = S.etf.res; if (!r) return;
      const keys = ['pair', 'long', 'short', 'entry', 'exit', 'sessions', 'z0', 'corr0', 'why', 'ret', 'pnl'];
      const q = x => x == null ? '' : /[",\n]/.test(String(x)) ? '"' + String(x).replace(/"/g, '""') + '"' : String(x);
      const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([[keys.join(',')].concat(r.trades.map(t => keys.map(k => q(t[k])).join(','))).join('\n')], { type: 'text/csv' }));
      a.download = 'etf_pairs_trades.csv'; a.click(); URL.revokeObjectURL(a.href); return;
    }
    if (ds.etfDays) { S.etf.days = +ds.etfDays; return vEtf(); }
    if (ds.etfSort) { S.etf.sort = ds.etfSort; $('#etf-body').innerHTML = etfHtml(S.etf.r); return; }
    if (ds.etf === 'save' || ds.etf === 'reset') {
      const syms = ds.etf === 'reset' ? ['XLK', 'SMH', 'XLC', 'XLY', 'XLF', 'XLI', 'XLB', 'XLE', 'XLV', 'XBI', 'XLP', 'XLU', 'XLRE', 'GLD', 'SLV', 'EEM', 'FXI']
        : $('#etf-syms').value.split(/[\s,;]+/).filter(Boolean);
      el.disabled = true;
      await api('/etf/settings', { method: 'PUT', body: { syms } });
      return vEtf();
    }
  } catch (e) { toast(e.message); el.disabled = false; }
});

document.addEventListener('change', ev => {
  const t = ev.target; if (!t.dataset || !t.dataset.etfv) return;
  S.etf.cfg[t.dataset.etfv] = +t.value;
});

function etfPairsShell() {
  const c = S.etf.cfg;
  const sel = (k, opts) => `<select data-etfv="${k}">${opts.map(([v, t]) => `<option value="${v}"${+c[k] === +v ? ' selected' : ''}>${t}</option>`).join('')}</select>`;
  const num = (k, step) => `<input type="number" step="${step}" data-etfv="${k}" value="${c[k]}">`;
  return `<section class="panel"><div class="hd"><h2>ETF pairs</h2><span class="dim" style="font-size:13px">every pair of the ETF list: when two ETFs that move together drift apart, short the one ahead and buy the one behind, until they come back</span></div>
    <div class="bd" style="display:flex;gap:12px;flex-wrap:wrap;align-items:end">
      <label class="f">Lookback (z-score)${sel('look', [[20, '20 days'], [40, '40 days'], [60, '60 days'], [90, '90 days'], [120, '120 days']])}</label>
      <label class="f">Enter at |z| ≥${sel('entry', [[1.5, '1.5'], [2, '2.0'], [2.5, '2.5'], [3, '3.0']])}</label>
      <label class="f">Exit at z${sel('exit', [[0, '0 (the average)'], [0.5, '±0.5'], [1, '±1.0'], [-0.5, 'past the average (−0.5)']])}</label>
      <label class="f">Stop at |z|${sel('stop', [[0, 'Off'], [3, '3.0'], [3.5, '3.5'], [4, '4.0'], [5, '5.0']])}</label>
      <label class="f">Hold up to${sel('hold', [[5, '5 sessions'], [10, '10 sessions'], [20, '20 sessions'], [40, '40 sessions'], [60, '60 sessions']])}</label>
      <label class="f">Only if correlation (60d) ≥${sel('minCorr', [[0, '0 (any)'], [0.5, '0.5'], [0.6, '0.6'], [0.7, '0.7'], [0.8, '0.8'], [0.9, '0.9']])}</label>
      <label class="f">$ per leg${num('stake', 100)}</label><label class="f">Costs, round trip per leg (bps)${num('costBps', 1)}</label><label class="f">Short borrow (%/year)${num('borrowPct', 0.5)}</label>
      <button class="btn primary" data-etf="run">Run backtest</button></div>
    <p class="note" style="padding:0 16px 14px">z = how far apart the pair is (log price ratio) vs its average over the lookback, in standard deviations. Decided on a close, traded at the next open — no look-ahead. Dollar-neutral: $ per leg long one ETF, the same short the other. Since 2016; the last 5 years are the test period.</p></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Optimizer</h2><span class="dim" style="font-size:13px">every combination of lookback, entry, exit, stop, hold and correlation (216), ranked on the years before the test period</span><div class="grow"></div><button class="btn" data-etf="opt">Run optimizer</button></div><div id="etf-opt"><div class="bd dim">Loading…</div></div></section>
    <div id="etf-res" style="margin-top:12px">${S.etf.res ? etfPairsHtml(S.etf.res) : ''}</div>`;
}

async function etfOptLoad() {
  const el = $('#etf-opt'); if (!el) return;
  try {
    const r = await api('/etf/pairs/opt'); const o = r.opt; S.etf.opt = o;
    const run = r.job && r.job.status === 'running';
    if (run) setTimeout(etfOptLoad, 4000);
    if (!o || !o.top || !o.top.length) { el.innerHTML = `<div class="bd dim">${run ? esc(r.job.msg || 'Running…') : 'Not run yet.'}</div>`; return; }
    const m = x => x == null ? '—' : (x < 0 ? '−$' : '$') + Math.abs(x).toLocaleString('en-US', { maximumFractionDigits: 0 });
    const st = x => `${x.n || 0} · ${m(x.total)} · ${x.win == null ? '—' : (x.win * 100).toFixed(0) + '%'} · t ${x.t ?? '—'}`;
    el.innerHTML = `${run ? `<div class="bd dim">${esc(r.job.msg || '')}</div>` : ''}<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Setting</th><th class="r">Train (before ${esc(o.split || '')}): trades · P/L · win · t</th><th class="r">Test</th><th></th></tr></thead><tbody>
      ${o.top.map((x, i) => `<tr><td>look ${x.p.look} · enter ${x.p.entry} · exit ${x.p.exit} · stop ${x.p.stop || 'off'} · hold ${x.p.hold} · corr ≥ ${x.p.minCorr}</td><td class="r">${st(x.train)}</td><td class="r ${cls(x.test.total)}">${st(x.test)}</td><td><button class="btn sm" data-etf-use="${i}">Use</button></td></tr>`).join('')}</tbody></table></div>
      <div class="note" style="padding:6px 16px 12px">${o.done} of ${o.total} settings tested. Ranked on train only (at least ${o.minTrades} train trades); the test column is what it did afterwards.</div>`;
  } catch (e) { el.innerHTML = `<div class="bd loss">${esc(e.message)}</div>`; }
}

function etfPairsHtml(r) {
  const m = x => x == null ? '—' : (x < 0 ? '−$' : '$') + Math.abs(x).toLocaleString('en-US', { maximumFractionDigits: 0 });
  const m2 = x => x == null ? '—' : (x < 0 ? '−$' : '$') + Math.abs(x).toFixed(2);
  const pcw = x => x == null ? '—' : (x * 100).toFixed(1) + '%';
  const row = (lab, x) => `<tr><td>${lab}</td><td class="r">${(x.n || 0).toLocaleString('en-US')}</td><td class="r ${cls(x.total)}">${m(x.total)}</td><td class="r ${cls(x.avg)}">${m2(x.avg)}</td><td class="r">${pcw(x.win)}</td><td class="r">${x.profitFactor ?? '—'}</td><td class="r gain">${m(x.avgWin)}</td><td class="r loss">${m(x.avgLoss)}</td><td class="r">${x.t ?? '—'}</td></tr>`;
  const head = '<thead><tr><th></th><th class="r">Trades</th><th class="r">P/L</th><th class="r">Avg / trade</th><th class="r">Win</th><th class="r">Profit factor</th><th class="r">Avg win</th><th class="r">Avg loss</th><th class="r">t</th></tr></thead>';
  const eq = r.equity; let svg = '';
  if (eq.length > 1) {
    const W = 900, H = 220, P = 50, vs = eq.map(e => e[1]), lo = Math.min(0, ...vs), hi = Math.max(0, ...vs);
    const t0 = new Date(eq[0][0]).getTime(), t1 = new Date(eq[eq.length - 1][0]).getTime();
    const X = d => P + (new Date(d).getTime() - t0) / Math.max(1, t1 - t0) * (W - P - 10), Y = v => 10 + (hi - v) / Math.max(1e-9, hi - lo) * (H - 30);
    const sx = X(r.split.date);
    svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto"><line x1="${P}" x2="${W - 10}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--line)"/>
      <line x1="${sx}" x2="${sx}" y1="10" y2="${H - 20}" stroke="var(--line)" stroke-dasharray="4 4"/><text x="${sx + 4}" y="20" font-size="11" fill="currentColor" opacity=".6">test →</text>
      <polyline fill="none" stroke="#3b82f6" stroke-width="1.6" points="${eq.map(e => X(e[0]).toFixed(1) + ',' + Y(e[1]).toFixed(1)).join(' ')}"/>
      <text x="4" y="${Y(hi) + 4}" font-size="11" fill="currentColor">${m(hi)}</text><text x="4" y="${Y(lo)}" font-size="11" fill="currentColor">${m(lo)}</text></svg>`;
  }
  const pairRow = x => `<tr><td class="mono">${esc(x.pair)}</td><td class="r">${x.n}</td><td class="r ${cls(x.total)}">${m(x.total)}</td><td class="r">${pcw(x.win)}</td><td class="r">${x.t ?? '—'}</td></tr>`;
  const ph = '<thead><tr><th>Pair</th><th class="r">Trades</th><th class="r">P/L</th><th class="r">Win</th><th class="r">t</th></tr></thead>';
  return `<section class="panel"><div class="hd"><h2>Results</h2><span class="dim" style="font-size:13px">${r.nPairs} pairs, ${esc(r.from)} → ${esc(r.to)} · at most ${r.peakPositions} positions at once (${m(r.peakPositions * 2 * r.cfg.stake)} gross)</span></div>
    <div class="tbl-wrap"><table class="tbl">${head}<tbody>${row(`Train (before ${esc(r.split.date)})`, r.split.train)}${row(`<b>Test (from ${esc(r.split.date)})</b>`, r.split.test)}${row('All', r.all)}</tbody></table></div>
    <div class="bd">${svg}<div class="dim" style="font-size:12px">Cumulative P/L by exit date, after costs.</div></div></section>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px">
      <section class="panel"><div class="hd"><h2>By year (exit)</h2></div><div class="tbl-wrap"><table class="tbl">${head}<tbody>${r.years.map(y => row(y.year, y)).join('')}</tbody></table></div></section>
      <section class="panel"><div class="hd"><h2>Best and worst pairs</h2></div><div class="tbl-wrap"><table class="tbl">${ph}<tbody>${r.pairs.map(pairRow).join('')}<tr><td colspan="5" class="dim">…</td></tr>${r.worstPairs.map(pairRow).join('')}</tbody></table></div></section></div>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Now</h2><span class="dim" style="font-size:13px">every pair's z and correlation at the last close: open positions and new entry signals for the next open</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Pair</th><th class="r">z</th><th class="r">Correlation 60d</th><th>Status</th></tr></thead><tbody>
      ${r.now.map(x => `<tr><td class="mono">${esc(x.pair)}</td><td class="r mono ${Math.abs(x.z) >= r.cfg.entry ? 'loss' : ''}">${x.z.toFixed(2)}</td><td class="r mono">${x.corr == null ? '—' : x.corr.toFixed(2)}</td>
        <td>${x.open ? `open since ${esc(x.open.entry)}: long ${esc(x.open.long)} / short ${esc(x.open.short)} · <span class="${cls(x.open.pnl)}">${m(x.open.pnl)}</span>` : x.signal ? `<b>entry signal:</b> ${esc(x.signal)}` : '<span class="dim">—</span>'}</td></tr>`).join('')}</tbody></table></div></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Trades</h2><span class="dim" style="font-size:13px">the latest ${Math.min(300, r.nTrades)} of ${r.nTrades.toLocaleString('en-US')}</span><div class="grow"></div><button class="btn sm" data-etf="csv">Export trades (CSV)</button></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Pair</th><th>Long</th><th>Short</th><th>Entry</th><th>Exit</th><th class="r">Sessions</th><th class="r">z at entry</th><th class="r">Corr</th><th>Exit</th><th class="r">Return</th><th class="r">P/L</th></tr></thead><tbody>
      ${r.trades.slice().reverse().map(t => `<tr><td class="mono">${esc(t.pair)}</td><td>${esc(t.long)}</td><td>${esc(t.short)}</td><td class="mono">${esc(t.entry)}</td><td class="mono">${esc(t.exit)}</td><td class="r">${t.sessions}</td><td class="r">${t.z0}</td><td class="r">${t.corr0}</td><td class="dim">${esc(t.why)}</td><td class="r ${cls(t.ret)}">${pcw(t.ret)}</td><td class="r ${cls(t.pnl)}">${m2(t.pnl)}</td></tr>`).join('')}</tbody></table></div></section>
    ${r.missing && r.missing.length ? `<div class="note loss">No prices: ${esc(r.missing.join('; '))}</div>` : ''}`;
}
