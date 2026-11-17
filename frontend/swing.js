// Swing: the Turtle breakout and Qullamaggie's breakout, backtested on daily bars over the earnings universes / ETFs.
S.sw = S.sw || { kind: 'qulla', cfg: {} };
const SW_LABEL = { turtle: 'Turtle', qulla: 'Qullamaggie breakout', btm: 'Beat the Market (SPY intraday)' };
const SW_UNI = [['all', 'Whole US market'], ['both', 'S&P 500 + Nasdaq-100'], ['spy', 'S&P 500'], ['qqq', 'Nasdaq-100'], ['etf', 'ETF list']];
const SW_UNI_T = [['fut', 'Futures (30 CME markets)'], ...SW_UNI];

async function vSwing() {
  const W = S.sw;
  const tabs = ['qulla', 'turtle', 'btm'].map(k => `<button class="btn${W.kind === k ? ' primary' : ''}" data-sw-kind="${k}">${SW_LABEL[k]}</button>`).join(' ');
  view(`${head('Swing', 'Daily breakout strategies: Turtle on 30 CME futures (or stocks / ETFs), Qullamaggie on the whole US market (delisted stocks included), the S&P 500 / Nasdaq-100 or the ETF list. The last 5 years are the test period.')}<div style="margin-bottom:12px">${tabs}</div><div id="sw-body"><div class="panel"><div class="empty">Loading…</div></div></div>`);
  if (W.kind === 'btm') return imLoad();
  await swLoad();
}

async function swLoad() {
  const W = S.sw, el = $('#sw-body'); if (!el || W.kind === 'btm') return;
  let r;
  try { r = await api('/swing/' + W.kind); } catch (e) { el.innerHTML = `<div class="panel"><div class="bd loss">${esc(e.message)}</div></div>`; return; }
  W.r = r;
  if (!W.cfg[W.kind]) W.cfg[W.kind] = { ...r.cfg };
  el.innerHTML = swShell(r);
  const live = j => j && j.status === 'running' && !j.stale;
  if (live(r.job) || live(r.futJob) || live(r.allJob)) { clearTimeout(swLoad.h); swLoad.h = setTimeout(swLoad, 4000); }
}

function swShell(r) {
  const W = S.sw, k = W.kind, c = W.cfg[k];
  const sel = (key, opts) => `<select data-swv="${key}">${opts.map(([v, t]) => `<option value="${v}"${String(c[key]) === String(v) ? ' selected' : ''}>${t}</option>`).join('')}</select>`;
  const num = (key, step) => `<input type="number" step="${step}" data-swv="${key}" value="${c[key]}">`;
  const yn = key => sel(key, [[false, 'Off'], [true, 'On']]);
  const fut = k === 'turtle' && c.uni === 'fut';
  const form = k === 'turtle'
    ? `<label class="f">System${sel('entryN', [[20, '1: 20-day breakout, 10-day exit'], [55, '2: 55-day breakout, 20-day exit']])}</label>
       <label class="f">Stop${sel('stopN', [[1, '1 N'], [2, '2 N'], [3, '3 N'], [4, '4 N']])}</label>
       <label class="f">Pyramid (+1 unit every ½ N, max 4)${yn('pyramid')}</label><label class="f">Shorts${yn('shorts')}</label>
       <label class="f">Skip after a winner (System 1)${yn('skipWin')}</label>${fut ? '' : `<label class="f">Market filter (benchmark vs 200-day)${yn('mkt')}</label>`}`
    : `<label class="f">ADR at least${sel('minAdr', [[0.02, '2%'], [0.03, '3%'], [0.04, '4%'], [0.05, '5%'], [0.06, '6%']])}</label>
       <label class="f">Up at least${sel('prior', [[0.2, '20%'], [0.3, '30%'], [0.5, '50%'], [1, '100%']])}</label>
       <label class="f">… over${sel('priorDays', [[30, '30 days'], [60, '60 days']])}</label>
       <label class="f">$ volume (20-day avg) at least${sel('minDv', [[5e6, '$5M'], [10e6, '$10M'], [20e6, '$20M'], [50e6, '$50M']])}</label>
       <label class="f">Base (days)${sel('base', [[5, '5'], [10, '10'], [15, '15'], [20, '20']])}</label>
       <label class="f">Base range at most${sel('tight', [[0.08, '8%'], [0.1, '10%'], [0.15, '15%'], [0.2, '20%'], [0.3, '30%']])}</label>
       <label class="f">Partial sale${sel('partialDay', [[0, 'Off'], [3, 'day 3'], [4, 'day 4'], [5, 'day 5']])}</label>
       <label class="f">Partial size${sel('partial', [[0.25, '¼'], [0.3333333333333333, '⅓'], [0.5, '½']])}</label>
       <label class="f">Trail: close below${sel('trail', [[10, '10-day average'], [20, '20-day average'], [50, '50-day average']])}</label>
       <label class="f">Market filter (benchmark above its 20-day)${yn('mkt')}</label>`;
  const rules = k === 'turtle'
    ? 'N = 20-day average true range. Long on a break of the highest high of the last 20 (55) days, filled at that level (or the open on a gap). Stop 2 N under the entry; exit on a break of the 10 (20)-day low. Pyramid: one more unit every ½ N, up to 4, the stop raised to 2 N under the last add. R = P/L ÷ the first unit\'s risk.'
    : 'Setup at a close: ADR ≥ the minimum, up ≥ the minimum over 30 or 60 days, above the 50-day with the 10 above the 20, the last N days\' range tight, price ≥ $5, average $ volume (20 days) ≥ the minimum (default $10M). Entry the next day on a break of the base high (or the open on a gap). Stop: that day\'s low, skipped if wider than one ADR — with daily bars the order of the day\'s moves is unknown, so this flatters the entry day. Partial sale at that day\'s close, then the stop to breakeven; the rest on the first close below the trailing average. R = return ÷ the entry-to-stop distance.';
  const live = j => j && j.status === 'running' && !j.stale;   // a job silent for 20 minutes died
  const o = r.opt, run = live(r.job), fj = r.futJob || {}, frun = live(fj);
  const jerr = r.job && r.job.status === 'error' ? `<div class="bd loss" style="border-top:1px solid var(--line)">Last run failed: ${esc(r.job.msg || '')}</div>` : '';
  const jdone = fj.status === 'error' ? `<div class="bd loss">Futures download failed: ${esc(fj.msg || '')}</div>` : fj.status === 'running' && !frun ? `<div class="bd loss">The download stopped answering (${esc(fj.msg || '')}): press Update to resume.</div>` : frun || fj.status === 'done' ? `<div class="bd dim">${esc(fj.msg || '')}</div>` : '';
  const m = x => x == null ? '—' : (x < 0 ? '−$' : '$') + Math.abs(x).toLocaleString('en-US', { maximumFractionDigits: 0 });
  const st = x => `${(x.n || 0).toLocaleString('en-US')} · ${m(x.total)} · ${x.win == null ? '—' : (x.win * 100).toFixed(0) + '%'} · ${x.avgR == null ? '—' : x.avgR + ' R'} · t ${x.t ?? '—'}`;
  const optHtml = !o || !o.top || !o.top.length ? `<div class="bd dim">${o && o.i ? `Running: ${o.i} symbols done…` : 'Not run yet.'}</div>`
    : `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Setting</th><th class="r">Train (before ${esc(o.split || '')}): trades · P/L · win · avg R · t</th><th class="r">Test</th><th></th></tr></thead><tbody>
      ${o.top.map((x, i) => `<tr><td>${esc(Object.entries(x.p).map(([a, b]) => `${a} ${b === true ? 'on' : b === false ? 'off' : b}`).join(' · '))}</td><td class="r">${st(x.train)}</td><td class="r ${cls(x.test.total)}">${st(x.test)}</td><td><button class="btn sm" data-sw-use="${i}">Use</button></td></tr>`).join('')}</tbody></table></div>
      <div class="note" style="padding:6px 16px 12px">${o.done} of ${o.total} settings. Ranked on train only (at least ${o.minTrades} train trades).</div>`;
  return `<section class="panel"><div class="hd"><h2>${SW_LABEL[k]}</h2><div class="grow"></div>${run ? `<span class="dim">${esc(r.job.msg || 'Running…')}</span>` : ''}</div>
    <div class="bd" style="display:flex;gap:12px;flex-wrap:wrap;align-items:end">
      <label class="f">Universe${sel('uni', k === 'turtle' ? SW_UNI_T : SW_UNI)}</label>${form}
      ${fut ? `<label class="f">$ risk per unit (1 N)${num('risk', 100)}</label><label class="f">Whole contracts${yn('whole')}</label>
        <label class="f">Commission $ / contract, round trip${num('comm', 0.5)}</label><label class="f">Slippage (ticks per side)${num('slip', 0.5)}</label>
        <div class="f" style="display:flex;gap:8px;flex-wrap:wrap">${(r.groups || []).map(g => `<label style="font-size:13px"><input type="checkbox" data-sw-grp="${esc(g)}"${!c.groups || c.groups.includes(g) ? ' checked' : ''}> ${esc(g)}</label>`).join('')}</div>`
      : k === 'qulla' ? `<label class="f">Position size${sel('sizing', [['account', 'From the account'], ['fixed', 'Fixed $']])}</label>
        ${c.sizing === 'fixed' ? `<label class="f">$ per trade${num('stake', 100)}</label>` : `<label class="f">Account start ($)${num('acct', 1000)}</label><label class="f">Risk per trade (% of account)${num('riskPct', 0.25)}</label><label class="f">Max position (% of account)${num('maxPos', 5)}</label>`}
        <label class="f">Costs, round trip (bps)${num('costBps', 1)}</label>`
      : `<label class="f">$ per unit${num('stake', 100)}</label><label class="f">Costs, round trip (bps)${num('costBps', 1)}</label><label class="f">Short borrow (%/year)${num('borrowPct', 0.5)}</label>`}
      <button class="btn primary" data-sw="run" ${run ? 'disabled' : ''}>Run backtest</button></div>
    <p class="note" style="padding:0 16px 14px">${rules} ${fut ? 'Futures: unit = $ risk ÷ (N × $ per point) contracts, as the Turtles sized (1% of equity per N); P/L in points × $ per point, minus commission and slippage on every contract. Prices back-adjusted at each roll, so P/L is exact but old prices can be negative (no % returns). No portfolio unit limits (the Turtles capped 12 units per direction).' : 'Costs on every unit; shorts pay borrow. Runs in the background (a minute or two for ~600 stocks).'}</p>
    ${fut ? swFutHtml(r.fut, frun) : ''}${c.uni === 'all' ? swAllHtml(r) : ''}${jerr}${jdone}</section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Optimizer</h2><span class="dim" style="font-size:13px">every combination of the main settings on every symbol, ranked on the years before the test period</span><div class="grow"></div><button class="btn" data-sw="opt" ${run ? 'disabled' : ''}>Run optimizer</button></div>${optHtml}</section>
    <div style="margin-top:12px">${r.res ? swResHtml(r.res) : ''}</div>`;
}

function swAllHtml(r) {
  const a = r.all || {}, j = r.allJob || {}, run = j.status === 'running' && !j.stale;
  const msg = j.status === 'error' ? `<div class="loss">Failed: ${esc(j.msg || '')}</div>` : j.stale ? `<div class="loss">The download stopped answering (${esc(j.msg || '')}): press the button to resume.</div>` : run ? `<div class="dim">${esc(j.msg || '')}</div>` : '';
  return `<div class="bd" style="border-top:1px solid var(--line)"><div style="display:flex;align-items:center;gap:12px"><b>Whole market</b><span class="dim">${a.n ? `${a.n.toLocaleString('en-US')} stocks ever liquid ($5M+ a day at $3+) out of ${(a.listed || 0).toLocaleString('en-US')} listed / delisted since 2016 · ${esc((a.at || '').slice(0, 10))}` : 'not downloaded'}${a.partial ? ` · in progress: ${a.partial.i} / ${a.partial.of}` : ''} · symbols from FMP (NYSE, NASDAQ, AMEX, no ETF / fund, delisted included), daily bars from Alpaca · takes ~30–60 minutes the first time</span><div class="grow"></div>
    <button class="btn" data-sw="alldl" ${run ? 'disabled' : ''}>${a.n ? 'Update' : 'Download'} the whole market</button></div>${msg}</div>`;
}

function swFutHtml(f, run) {
  const have = (f || []).filter(x => x.n).length;
  const rows = (f || []).map(x => `<tr><td class="mono">${esc(x.root)}</td><td>${esc(x.name)}</td><td class="dim">${esc(x.group)}</td><td class="r">$${x.pv.toLocaleString('en-US')}</td><td class="mono">${esc(x.first || '—')}</td><td class="mono">${esc(x.last || '—')}</td><td class="r">${x.rolls ?? '—'}</td><td class="r">${x.missing ?? '—'}</td>${x.error ? `<td class="loss">${esc(x.error)}</td>` : '<td></td>'}</tr>`).join('');
  return `<div class="bd" style="border-top:1px solid var(--line)"><div style="display:flex;align-items:center;gap:12px"><b>Futures daily bars</b><span class="dim">${have} of ${(f || []).length} markets downloaded · Databento GLBX ohlcv-1d since June 2010, front by volume, back-adjusted at each roll · the cost is checked against your Data-page limit first</span><div class="grow"></div><button class="btn" data-sw="futdl" ${run ? 'disabled' : ''}>${have ? 'Update' : 'Download'} futures daily bars</button></div>
    <details style="margin-top:8px"><summary class="dim">Markets</summary><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Root</th><th>Market</th><th>Group</th><th class="r">$ / point</th><th>First</th><th>Last</th><th class="r">Rolls</th><th class="r">Rolls not adjusted</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></details></div>`;
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
  const A = r.acct, acctPanel = !A ? '' : (() => {
    const pc = x => x == null ? '—' : (x * 100).toFixed(1) + '%';
    const card = (lab, a) => `<div><div class="dim" style="font-size:12px">${lab}</div><div style="font-size:20px" class="${cls(a.final - a.start)}">${m(a.final)}</div>
      <div class="dim" style="font-size:12px">${m(a.start)} from ${esc(a.from || '—')} · ${pc(a.ret)} · CAGR ${pc(a.cagr)} · max drawdown ${pc(a.maxDD)} · ${a.taken} trades taken, ${a.skipped} skipped (no cash left)</div></div>`;
    const cv = A.all.curve; let sv = '';
    if (cv.length > 1) {
      const W = 900, H = 200, P = 70, lo = Math.min(...cv.map(e => e[1])), hi = Math.max(...cv.map(e => e[1]));
      const L = v => Math.log(Math.max(1, v)), t0 = new Date(cv[0][0]).getTime(), t1 = new Date(cv[cv.length - 1][0]).getTime();
      const X = d => P + (new Date(d).getTime() - t0) / Math.max(1, t1 - t0) * (W - P - 10), Y = v => 10 + (L(hi) - L(v)) / Math.max(1e-9, L(hi) - L(lo)) * (H - 30);
      sv = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto"><polyline fill="none" stroke="#22c55e" stroke-width="1.6" points="${cv.map(e => X(e[0]).toFixed(1) + ',' + Y(e[1]).toFixed(1)).join(' ')}"/>
        <text x="4" y="${Y(hi) + 4}" font-size="11" fill="currentColor">${m(hi)}</text><text x="4" y="${Y(lo)}" font-size="11" fill="currentColor">${m(lo)}</text></svg>`;
    }
    return `<section class="panel" style="margin-bottom:12px"><div class="hd"><h2>Account</h2><span class="dim" style="font-size:13px">risk ${r.cfg.riskPct}% of the account per trade ÷ the stop distance, at most ${r.cfg.maxPos}% of the account in one position, no margin; compounding</span></div>
      <div class="bd" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">${card('Whole period', A.all)}${card('Test period only (fresh account)', A.test)}</div>
      <div class="bd">${sv}<div class="dim" style="font-size:12px">Account value at each exit (log scale).</div></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Year</th><th class="r">Account at year end</th><th class="r">Return</th></tr></thead><tbody>${A.all.years.map(y => `<tr><td>${y.year}</td><td class="r">${m(y.end)}</td><td class="r ${cls(y.ret)}">${pc(y.ret)}</td></tr>`).join('')}</tbody></table></div></section>`;
  })();
  return `${acctPanel}<section class="panel"><div class="hd"><h2>${A ? 'Per trade (at a fixed $' + r.cfg.stake + ' each)' : 'Results'}</h2><span class="dim" style="font-size:13px">${r.nSyms} symbols vs ${esc(r.bench)} · ${esc(r.at.replace('T', ' ').slice(0, 16))} · at most ${r.peak} positions at once</span></div>
    <div class="tbl-wrap"><table class="tbl">${head}<tbody>${row(`Train (before ${esc(r.split.date)})`, r.split.train)}${row(`<b>Test (from ${esc(r.split.date)})</b>`, r.split.test)}${row('All', r.all)}</tbody></table></div>
    <div class="bd">${svg}<div class="dim" style="font-size:12px">Cumulative P/L by exit date, after costs.</div></div></section>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px">
      <section class="panel"><div class="hd"><h2>By year (exit)</h2></div><div class="tbl-wrap"><table class="tbl">${head}<tbody>${r.years.map(y => row(y.year, y)).join('')}</tbody></table></div></section>
      <section class="panel"><div class="hd"><h2>By exit reason</h2></div><div class="tbl-wrap"><table class="tbl">${head}<tbody>${r.why.map(y => row(esc(y.why), y)).join('')}</tbody></table></div></section></div>
    ${r.groups && r.groups.length ? `<section class="panel" style="margin-top:12px"><div class="hd"><h2>By market group</h2></div><div class="tbl-wrap"><table class="tbl">${head}<tbody>${r.groups.map(g => row(esc(g.group), g)).join('')}</tbody></table></div></section>` : ''}
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
      ${r.trades.slice().reverse().map(t => `<tr><td class="mono">${esc(t.sym)}</td><td>${t.side}</td><td class="mono">${esc(t.entry)}</td><td class="mono">${esc(t.exit)}</td><td class="r">${t.in}</td><td class="r">${t.out}</td><td class="r">${t.size != null ? m(t.size) : t.units}${t.contracts != null ? ' · ' + t.contracts + ' ct' : ''}</td><td class="dim">${esc(t.why)}</td><td class="r ${cls(t.ret ?? t.pts)}">${t.ret == null && t.pts != null ? t.pts + ' pts' : pcw(t.ret)}</td><td class="r ${cls(t.pnl)}">${m2(t.pnl)}</td><td class="r">${t.R ?? '—'}</td></tr>`).join('')}</tbody></table></div></section>`;
}

document.addEventListener('change', ev => {
  const t = ev.target; if (!t.dataset) return;
  const c = S.sw.cfg[S.sw.kind];
  if (t.dataset.swGrp) {
    const all = S.sw.r.groups, cur = c.groups || all.slice();
    c.groups = t.checked ? [...new Set([...cur, t.dataset.swGrp])] : cur.filter(g => g !== t.dataset.swGrp);
    if (c.groups.length === all.length) c.groups = null;
    return;
  }
  if (!t.dataset.swv) return;
  const k = t.dataset.swv;
  c[k] = t.value === 'true' ? true : t.value === 'false' ? false : k === 'uni' ? t.value : +t.value;
  if (k === 'uni' || k === 'sizing') { c[k] = t.value; $('#sw-body').innerHTML = swShell(S.sw.r); }
});

document.addEventListener('click', async ev => {
  const el = ev.target.closest('[data-sw-kind],[data-sw],[data-sw-use]');
  if (!el || !$('#sw-body')) return;
  const ds = el.dataset, W = S.sw;
  try {
    if (ds.swKind) { W.kind = ds.swKind; return vSwing(); }
    if (ds.swUse) { Object.assign(W.cfg[W.kind], W.r.opt.top[+ds.swUse].p); $('#sw-body').innerHTML = swShell(W.r); toast('Setting applied: run the backtest.'); return; }
    if (ds.sw === 'run') { el.disabled = true; await api('/swing/' + W.kind, { method: 'POST', body: { cfg: W.cfg[W.kind] } }); toast('Backtest started.'); setTimeout(swLoad, 1500); return; }
    if (ds.sw === 'alldl') { el.disabled = true; await api('/swing/all', { method: 'POST', body: {} }); toast('Download started: it runs in the background.'); setTimeout(swLoad, 1500); return; }
    if (ds.sw === 'futdl') { el.disabled = true; await api('/swing/futdaily', { method: 'POST', body: {} }); toast('Download started.'); setTimeout(swLoad, 1500); return; }
    if (ds.sw === 'opt') { el.disabled = true; await api('/swing/' + W.kind + '/opt', { method: 'POST', body: { cfg: W.cfg[W.kind] } }); toast('Optimizer started.'); setTimeout(swLoad, 1500); return; }
    if (ds.sw === 'csv') {
      const r = W.r && W.r.res; if (!r) return;
      const keys = ['sym', 'side', 'entry', 'exit', 'in', 'out', 'units', 'size', 'contracts', 'pts', 'days', 'why', 'ret', 'pnl', 'R'];
      const q = x => x == null ? '' : /[",\n]/.test(String(x)) ? '"' + String(x).replace(/"/g, '""') + '"' : String(x);
      const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([[keys.join(',')].concat(r.trades.map(t => keys.map(k => q(t[k])).join(','))).join('\n')], { type: 'text/csv' }));
      a.download = `swing_${W.kind}_trades.csv`; a.click(); URL.revokeObjectURL(a.href); return;
    }
  } catch (e) { toast(e.message); el.disabled = false; }
});
