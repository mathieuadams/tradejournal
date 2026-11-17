// Swing: the Turtle breakout and Qullamaggie's breakout, backtested on daily bars over the earnings universes / ETFs.
S.sw = S.sw || { kind: 'qulla', cfg: {} };
const SW_LABEL = { turtle: 'Turtle', qulla: 'Qullamaggie breakout' };
const SW_UNI = [['both', 'S&P 500 + Nasdaq-100'], ['spy', 'S&P 500'], ['qqq', 'Nasdaq-100'], ['etf', 'ETF list']];

async function vSwing() {
  const W = S.sw;
  const tabs = ['qulla', 'turtle'].map(k => `<button class="btn${W.kind === k ? ' primary' : ''}" data-sw-kind="${k}">${SW_LABEL[k]}</button>`).join(' ');
  view(`${head('Swing', 'Daily breakout strategies on the S&P 500 / Nasdaq-100 (removed companies included) or the ETF list. Since 2016; the last 5 years are the test period.')}<div style="margin-bottom:12px">${tabs}</div><div id="sw-body"><div class="panel"><div class="empty">Loading…</div></div></div>`);
  await swLoad();
}

async function swLoad() {
  const W = S.sw, el = $('#sw-body'); if (!el) return;
  let r;
  try { r = await api('/swing/' + W.kind); } catch (e) { el.innerHTML = `<div class="panel"><div class="bd loss">${esc(e.message)}</div></div>`; return; }
  W.r = r;
  if (!W.cfg[W.kind]) W.cfg[W.kind] = { ...r.cfg };
  el.innerHTML = swShell(r);
  if (r.job && r.job.status === 'running' && /^swing/.test(r.job.job || '')) setTimeout(swLoad, 4000);
}

function swShell(r) {
  const W = S.sw, k = W.kind, c = W.cfg[k];
  const sel = (key, opts) => `<select data-swv="${key}">${opts.map(([v, t]) => `<option value="${v}"${String(c[key]) === String(v) ? ' selected' : ''}>${t}</option>`).join('')}</select>`;
  const num = (key, step) => `<input type="number" step="${step}" data-swv="${key}" value="${c[key]}">`;
  const yn = key => sel(key, [[false, 'Off'], [true, 'On']]);
  const form = k === 'turtle'
    ? `<label class="f">System${sel('entryN', [[20, '1: 20-day breakout, 10-day exit'], [55, '2: 55-day breakout, 20-day exit']])}</label>
       <label class="f">Stop${sel('stopN', [[1, '1 N'], [2, '2 N'], [3, '3 N'], [4, '4 N']])}</label>
       <label class="f">Pyramid (+1 unit every ½ N, max 4)${yn('pyramid')}</label><label class="f">Shorts${yn('shorts')}</label>
       <label class="f">Skip after a winner (System 1)${yn('skipWin')}</label><label class="f">Market filter (benchmark vs 200-day)${yn('mkt')}</label>`
    : `<label class="f">ADR at least${sel('minAdr', [[0.02, '2%'], [0.03, '3%'], [0.04, '4%'], [0.05, '5%'], [0.06, '6%']])}</label>
       <label class="f">Up over 60 days at least${sel('prior', [[0.2, '20%'], [0.3, '30%'], [0.5, '50%'], [1, '100%']])}</label>
       <label class="f">Base (days)${sel('base', [[5, '5'], [10, '10'], [15, '15'], [20, '20']])}</label>
       <label class="f">Base range at most${sel('tight', [[0.08, '8%'], [0.1, '10%'], [0.15, '15%'], [0.2, '20%'], [0.3, '30%']])}</label>
       <label class="f">Partial sale${sel('partialDay', [[0, 'Off'], [3, 'day 3'], [4, 'day 4'], [5, 'day 5']])}</label>
       <label class="f">Partial size${sel('partial', [[0.25, '¼'], [0.3333333333333333, '⅓'], [0.5, '½']])}</label>
       <label class="f">Trail: close below${sel('trail', [[10, '10-day average'], [20, '20-day average'], [50, '50-day average']])}</label>
       <label class="f">Market filter (benchmark above its 20-day)${yn('mkt')}</label>`;
  const rules = k === 'turtle'
    ? 'N = 20-day average true range. Long on a break of the highest high of the last 20 (55) days, filled at that level (or the open on a gap). Stop 2 N under the entry; exit on a break of the 10 (20)-day low. Pyramid: one more unit every ½ N, up to 4, the stop raised to 2 N under the last add. R = P/L ÷ the first unit\'s risk.'
    : 'Setup at a close: ADR ≥ the minimum, up ≥ the minimum over 60 days, above the 50-day with the 10 above the 20, the last N days\' range tight, price ≥ $5, $20M+ a day traded. Entry the next day on a break of the base high (or the open on a gap). Stop: that day\'s low, skipped if wider than one ADR — with daily bars the order of the day\'s moves is unknown, so this flatters the entry day. Partial sale at that day\'s close, then the stop to breakeven; the rest on the first close below the trailing average. R = return ÷ the entry-to-stop distance.';
  const o = r.opt, run = r.job && r.job.status === 'running' && /^swing/.test(r.job.job || '');
  const m = x => x == null ? '—' : (x < 0 ? '−$' : '$') + Math.abs(x).toLocaleString('en-US', { maximumFractionDigits: 0 });
  const st = x => `${(x.n || 0).toLocaleString('en-US')} · ${m(x.total)} · ${x.win == null ? '—' : (x.win * 100).toFixed(0) + '%'} · ${x.avgR == null ? '—' : x.avgR + ' R'} · t ${x.t ?? '—'}`;
  const optHtml = !o || !o.top || !o.top.length ? `<div class="bd dim">${o && o.i ? `Running: ${o.i} symbols done…` : 'Not run yet.'}</div>`
    : `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Setting</th><th class="r">Train (before ${esc(o.split || '')}): trades · P/L · win · avg R · t</th><th class="r">Test</th><th></th></tr></thead><tbody>
      ${o.top.map((x, i) => `<tr><td>${esc(Object.entries(x.p).map(([a, b]) => `${a} ${b === true ? 'on' : b === false ? 'off' : b}`).join(' · '))}</td><td class="r">${st(x.train)}</td><td class="r ${cls(x.test.total)}">${st(x.test)}</td><td><button class="btn sm" data-sw-use="${i}">Use</button></td></tr>`).join('')}</tbody></table></div>
      <div class="note" style="padding:6px 16px 12px">${o.done} of ${o.total} settings. Ranked on train only (at least ${o.minTrades} train trades).</div>`;
  return `<section class="panel"><div class="hd"><h2>${SW_LABEL[k]}</h2><div class="grow"></div>${run ? `<span class="dim">${esc(r.job.msg || 'Running…')}</span>` : ''}</div>
    <div class="bd" style="display:flex;gap:12px;flex-wrap:wrap;align-items:end">
      <label class="f">Universe${sel('uni', SW_UNI)}</label>${form}
      <label class="f">$ per unit${num('stake', 100)}</label><label class="f">Costs, round trip (bps)${num('costBps', 1)}</label><label class="f">Short borrow (%/year)${num('borrowPct', 0.5)}</label>
      <button class="btn primary" data-sw="run" ${run ? 'disabled' : ''}>Run backtest</button></div>
    <p class="note" style="padding:0 16px 14px">${rules} Costs on every unit; shorts pay borrow. Runs in the background (a minute or two for ~600 stocks).</p></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Optimizer</h2><span class="dim" style="font-size:13px">every combination of the main settings on every symbol, ranked on the years before the test period</span><div class="grow"></div><button class="btn" data-sw="opt" ${run ? 'disabled' : ''}>Run optimizer</button></div>${optHtml}</section>
    <div style="margin-top:12px">${r.res ? swResHtml(r.res) : ''}</div>`;
}

function swResHtml(r) {
  const m = x => x == null ? '—' : (x < 0 ? '−$' : '$') + Math.abs(x).toLocaleString('en-US', { maximumFractionDigits: 0 });
  const m2 = x => x == null ? '—' : (x < 0 ? '−$' : '$') + Math.abs(x).toFixed(2);
  const pcw = x => x == null ? '—' : (x * 100).toFixed(1) + '%';
  const row = (lab, x) => `<tr><td>${lab}</td><td class="r">${(x.n || 0).toLocaleString('en-US')}</td><td class="r ${cls(x.total)}">${m(x.total)}</td><td class="r ${cls(x.avg)}">${m2(x.avg)}</td><td class="r">${pcw(x.win)}</td><td class="r">${x.profitFactor ?? '—'}</td><td class="r ${cls(x.avgR)}">${x.avgR ?? '—'}</td><td class="r">${pcw(x.big)}</td><td class="r gain">${m(x.avgWin)}</td><td class="r loss">${m(x.avgLoss)}</td><td class="r">${x.t ?? '—'}</td></tr>`;
  const head = '<thead><tr><th></th><th class="r">Trades</th><th class="r">P/L</th><th class="r">Avg / trade</th><th class="r">Win</th><th class="r">Profit factor</th><th class="r">Avg R</th><th class="r">≥ 3 R</th><th class="r">Avg win</th><th class="r">Avg loss</th><th class="r">t</th></tr></thead>';
  const eq = r.equity; let svg = '';
  if (eq.length > 1) {
    const W = 900, H = 220, P = 60, vs = eq.map(e => e[1]), lo = Math.min(0, ...vs), hi = Math.max(0, ...vs);
    const t0 = new Date(eq[0][0]).getTime(), t1 = new Date(eq[eq.length - 1][0]).getTime();
    const X = d => P + (new Date(d).getTime() - t0) / Math.max(1, t1 - t0) * (W - P - 10), Y = v => 10 + (hi - v) / Math.max(1e-9, hi - lo) * (H - 30);
    const sx = X(r.split.date);
    svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto"><line x1="${P}" x2="${W - 10}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--line)"/>
      <line x1="${sx}" x2="${sx}" y1="10" y2="${H - 20}" stroke="var(--line)" stroke-dasharray="4 4"/><text x="${sx + 4}" y="20" font-size="11" fill="currentColor" opacity=".6">test →</text>
      <polyline fill="none" stroke="#3b82f6" stroke-width="1.6" points="${eq.map(e => X(e[0]).toFixed(1) + ',' + Y(e[1]).toFixed(1)).join(' ')}"/>
      <text x="4" y="${Y(hi) + 4}" font-size="11" fill="currentColor">${m(hi)}</text><text x="4" y="${Y(lo)}" font-size="11" fill="currentColor">${m(lo)}</text></svg>`;
  }
  const symRow = x => `<tr><td class="mono">${esc(x.sym)}</td><td class="r">${x.n}</td><td class="r ${cls(x.total)}">${m(x.total)}</td><td class="r">${pcw(x.win)}</td><td class="r">${x.avgR ?? '—'}</td></tr>`;
  const sh = '<thead><tr><th>Symbol</th><th class="r">Trades</th><th class="r">P/L</th><th class="r">Win</th><th class="r">Avg R</th></tr></thead>';
  return `<section class="panel"><div class="hd"><h2>Results</h2><span class="dim" style="font-size:13px">${r.nSyms} symbols vs ${esc(r.bench)} · ${esc(r.at.replace('T', ' ').slice(0, 16))} · at most ${r.peak} positions at once</span></div>
    <div class="tbl-wrap"><table class="tbl">${head}<tbody>${row(`Train (before ${esc(r.split.date)})`, r.split.train)}${row(`<b>Test (from ${esc(r.split.date)})</b>`, r.split.test)}${row('All', r.all)}</tbody></table></div>
    <div class="bd">${svg}<div class="dim" style="font-size:12px">Cumulative P/L by exit date, after costs.</div></div></section>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px">
      <section class="panel"><div class="hd"><h2>By year (exit)</h2></div><div class="tbl-wrap"><table class="tbl">${head}<tbody>${r.years.map(y => row(y.year, y)).join('')}</tbody></table></div></section>
      <section class="panel"><div class="hd"><h2>By exit reason</h2></div><div class="tbl-wrap"><table class="tbl">${head}<tbody>${r.why.map(y => row(esc(y.why), y)).join('')}</tbody></table></div></section></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px">
      <section class="panel"><div class="hd"><h2>Best symbols</h2></div><div class="tbl-wrap"><table class="tbl">${sh}<tbody>${r.best.map(symRow).join('')}</tbody></table></div></section>
      <section class="panel"><div class="hd"><h2>Worst symbols</h2></div><div class="tbl-wrap"><table class="tbl">${sh}<tbody>${r.worst.map(symRow).join('')}</tbody></table></div></section></div>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Watchlist for the next session</h2><span class="dim" style="font-size:13px">setups at the last close (${esc(r.last)}): the level that triggers an entry</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Symbol</th><th class="r">Close</th><th class="r">Trigger</th><th class="r">Stop</th><th>Why</th></tr></thead><tbody>
      ${r.watch.length ? r.watch.map(w => `<tr><td class="mono">${esc(w.sym)}</td><td class="r">${w.close.toFixed(2)}</td><td class="r"><b>${w.level}</b></td><td class="r">${w.stop ?? 'the day\'s low'}</td><td class="dim">${esc(w.note)}</td></tr>`).join('') : '<tr><td colspan="5" class="dim">No setup at the last close.</td></tr>'}</tbody></table></div></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Open positions</h2><span class="dim" style="font-size:13px">not counted in the results; marked at the last close</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Symbol</th><th>Side</th><th>Entry</th><th class="r">In</th><th class="r">Last</th><th class="r">Units</th><th class="r">P/L</th><th class="r">R</th></tr></thead><tbody>
      ${r.open.length ? r.open.slice().reverse().map(t => `<tr><td class="mono">${esc(t.sym)}</td><td>${t.side}</td><td class="mono">${esc(t.entry)}</td><td class="r">${t.in}</td><td class="r">${t.out}</td><td class="r">${t.units}</td><td class="r ${cls(t.pnl)}">${m2(t.pnl)}</td><td class="r">${t.R ?? '—'}</td></tr>`).join('') : '<tr><td colspan="8" class="dim">None.</td></tr>'}</tbody></table></div></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Trades</h2><span class="dim" style="font-size:13px">the latest ${r.trades.length} of ${r.n.toLocaleString('en-US')}</span><div class="grow"></div><button class="btn sm" data-sw="csv">Export trades (CSV)</button></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th class="r">In</th><th class="r">Out</th><th class="r">Units</th><th>Exit</th><th class="r">Return</th><th class="r">P/L</th><th class="r">R</th></tr></thead><tbody>
      ${r.trades.slice().reverse().map(t => `<tr><td class="mono">${esc(t.sym)}</td><td>${t.side}</td><td class="mono">${esc(t.entry)}</td><td class="mono">${esc(t.exit)}</td><td class="r">${t.in}</td><td class="r">${t.out}</td><td class="r">${t.units}</td><td class="dim">${esc(t.why)}</td><td class="r ${cls(t.ret)}">${pcw(t.ret)}</td><td class="r ${cls(t.pnl)}">${m2(t.pnl)}</td><td class="r">${t.R ?? '—'}</td></tr>`).join('')}</tbody></table></div></section>`;
}

document.addEventListener('change', ev => {
  const t = ev.target; if (!t.dataset || !t.dataset.swv) return;
  const c = S.sw.cfg[S.sw.kind], k = t.dataset.swv;
  c[k] = t.value === 'true' ? true : t.value === 'false' ? false : k === 'uni' ? t.value : +t.value;
});

document.addEventListener('click', async ev => {
  const el = ev.target.closest('[data-sw-kind],[data-sw],[data-sw-use]');
  if (!el || !$('#sw-body')) return;
  const ds = el.dataset, W = S.sw;
  try {
    if (ds.swKind) { W.kind = ds.swKind; return vSwing(); }
    if (ds.swUse) { Object.assign(W.cfg[W.kind], W.r.opt.top[+ds.swUse].p); $('#sw-body').innerHTML = swShell(W.r); toast('Setting applied: run the backtest.'); return; }
    if (ds.sw === 'run') { el.disabled = true; await api('/swing/' + W.kind, { method: 'POST', body: { cfg: W.cfg[W.kind] } }); toast('Backtest started.'); setTimeout(swLoad, 1500); return; }
    if (ds.sw === 'opt') { el.disabled = true; await api('/swing/' + W.kind + '/opt', { method: 'POST', body: { cfg: W.cfg[W.kind] } }); toast('Optimizer started.'); setTimeout(swLoad, 1500); return; }
    if (ds.sw === 'csv') {
      const r = W.r && W.r.res; if (!r) return;
      const keys = ['sym', 'side', 'entry', 'exit', 'in', 'out', 'units', 'days', 'why', 'ret', 'pnl', 'R'];
      const q = x => x == null ? '' : /[",\n]/.test(String(x)) ? '"' + String(x).replace(/"/g, '""') + '"' : String(x);
      const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([[keys.join(',')].concat(r.trades.map(t => keys.map(k => q(t[k])).join(','))).join('\n')], { type: 'text/csv' }));
      a.download = `swing_${W.kind}_trades.csv`; a.click(); URL.revokeObjectURL(a.href); return;
    }
  } catch (e) { toast(e.message); el.disabled = false; }
});
