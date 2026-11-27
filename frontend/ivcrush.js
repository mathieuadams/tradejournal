// Swing → IV crush: sell out-of-the-money puts on S&P 500 stocks when their IV rank is high, buy back at 50% of the credit.
S.iv = S.iv || { cfg: null };

async function ivLoad() {
  const el = $('#sw-body'); if (!el) return;
  let r;
  try { r = await api('/ivcrush'); } catch (e) { el.innerHTML = `<div class="panel"><div class="bd loss">${esc(e.message)}</div></div>`; return; }
  S.iv.v = r;
  if (!S.iv.cfg) S.iv.cfg = { ...r.cfg };
  if (S.sw.kind !== 'ivc' || !$('#sw-body')) return;
  $('#sw-body').innerHTML = ivShell(r);
  const live = r.job && r.job.status === 'running' && !r.job.stale;
  clearTimeout(ivLoad.h);
  if (live) ivLoad.h = setTimeout(() => S.sw.kind === 'ivc' && ivLoad(), 5000);
}

function ivShell(r) {
  const c = S.iv.cfg, d = r.data, j = r.job || {}, run = j.status === 'running' && !j.stale, I = S.iv;
  const sel = (key, opts) => `<select data-ivv="${key}">${opts.map(([v, t]) => `<option value="${v}"${String(c[key]) === String(v) ? ' selected' : ''}>${t}</option>`).join('')}</select>`;
  const num = (key, step) => `<input type="number" step="${step}" data-ivv="${key}" value="${c[key]}" style="width:90px">`;
  const jmsg = j.status === 'error' ? `<div class="bd loss">Last job failed: ${esc(j.msg || '')}</div>` : j.stale ? `<div class="bd loss">The last job stopped answering (${esc(j.msg || '')}): start it again.</div>` : run || j.status === 'done' ? `<div class="bd dim">${esc(j.msg || '')}</div>` : '';
  return `<section class="panel"><div class="hd"><h2>IV crush — sell S&amp;P 500 puts when IV is high</h2><div class="grow"></div>${run ? `<span class="dim">${esc(j.msg || 'Running…')}</span>` : ''}</div>
    <div class="bd" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><b>Data</b>
      <span class="dim">${d.syms ? `S&P 500: ${d.syms} stocks (FMP, ${esc((d.uniAt || '').slice(0, 10))}) · IV rank history: ${d.have} stocks${d.first ? ', ' + esc(d.first) + ' → ' + esc(d.last) : ''}` : 'none yet'} · Unusual Whales (IV rank, option chains and daily option prices, from 2022)</span><div class="grow"></div>
      <button class="btn" data-iv="data" ${run ? 'disabled' : ''}>${d.have ? 'Update' : 'Download'} IV history</button></div>${jmsg}
    <div class="bd" style="display:flex;gap:12px;flex-wrap:wrap;align-items:end;border-top:1px solid var(--line)">
      <label class="f">IV rank ≥${num('rank', 5)}</label>
      <label class="f">Put delta${sel('delta', [[0.1, '0.10'], [0.15, '0.15'], [0.2, '0.20'], [0.25, '0.25'], [0.3, '0.30'], [0.4, '0.40']])}</label>
      <label class="f">Days to expiry (target)${num('dte', 1)}</label><label class="f">min${num('dteMin', 1)}</label><label class="f">max${num('dteMax', 1)}</label>
      <label class="f">Take profit (% of credit)${sel('take', [[0.25, '25%'], [0.5, '50%'], [0.75, '75%']])}</label>
      <label class="f">Close at DTE (0 = hold)${num('manage', 1)}</label>
      <label class="f">Stop (× credit, 0 = off)${num('stop', 0.5)}</label>
      <label class="f">Min premium ($)${num('minPx', 0.05)}</label>
      <label class="f">Max open puts${num('maxPos', 1)}</label>
      <label class="f">Collateral${sel('coll', [['cash', 'Cash-secured (strike × 100)'], ['margin', 'Margin (Reg T ≈ 20%)']])}</label>
      <label class="f">Allocation (× equity)${num('alloc', 0.1)}</label>
      <label class="f">Account ($)${num('acct', 10000)}</label>
      <label class="f">Slippage $ / share (mid or no NBBO)${num('slip', 0.01)}</label><label class="f">Commission $ / contract${num('comm', 0.05)}</label>
      <label class="f">Fill${sel('fill', [['nbbo', 'NBBO (sell at bid, buy at ask)'], ['mid', 'Mid ± slippage']])}</label>
      <label class="f">Backtest from<input type="date" data-ivv="from" value="${c.from}"></label>
      <button class="btn primary" data-iv="run" ${run ? 'disabled' : ''}>Run backtest</button></div>
    <p class="note" style="padding:0 16px 14px">IV rank: Unusual Whales' 1-year IV rank of each stock (0–100: where today's implied volatility sits between its low and high of the last year). Entry when IV rank ≥ the threshold: the expiry nearest the target days (within min–max); the strike for the target delta from the stock's IV, then of the 3 listed strikes nearest it, the one whose delta that day (its own IV) is nearest; sold at the day's NBBO bid. One put per stock at a time; at most "max open puts" at once, the highest IV ranks first. Exit on the contract's daily prices: its NBBO ask ≤ (1 − take profit) × the credit → bought back there; or at the DTE exit; or the stop; else expiry (strike − price). Size: equity × allocation ÷ max open puts ÷ the collateral of one contract. The first run downloads the option chains and prices it needs (cached: later runs are fast). Universe: today's S&P 500 (companies removed in the period are not in it). The first 60% of the days are the train period.</p></section>
    ${ivOptHtml(r.opt, run)}
    <div style="margin-top:12px">${r.res ? ivResHtml(r.res) : ''}</div>`;
}

function ivOptHtml(o, run) {
  const pc = (x, k = 0) => x == null ? '—' : (x * 100).toFixed(k) + '%';
  const st = x => !x || !x.n ? '<span class="dim">no trades</span>' : x.total == null ? `${x.n} trades` : `${pc(x.total)} · Sharpe ${x.sharpe ?? '—'} · DD ${pc(x.mdd)} · ${x.n} trades · win ${pc(x.win)}`;
  const lab = p => `IV rank ≥ ${p.rank} · take ${Math.round(p.take * 100)}% · ${p.manage ? 'close at ' + p.manage + ' DTE' : 'hold to expiry'} · ${p.stop ? 'stop ' + p.stop + '× credit' : 'no stop'}${p.delta != null ? ` · delta ${p.delta} · ${p.dte} DTE` : ''}`;
  const body = !o ? '<div class="bd dim">Not run yet.</div>' : o.running ? '<div class="bd dim">Running…</div>' : !o.top || !o.top.length ? '<div class="bd dim">No results.</div>' :
    `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Setting</th><th class="r">Train (before ${esc(o.split || '')})</th><th class="r">Test</th><th></th></tr></thead><tbody>
    ${o.top.map((x, i) => `<tr><td>${lab(x.p)}</td><td class="r">${st(x.train)}</td><td class="r ${cls(x.test.total)}">${st(x.test)}</td><td><button class="btn sm" data-iv-use="${i}">Use</button></td></tr>`).join('')}</tbody></table></div>
    <div class="note" style="padding:6px 16px 12px">${o.total} settings on ${o.syms} stocks, ranked on the train Sharpe only (the test column is out of sample). Equity on closed trades. ${o.wide ? 'Delta and DTE varied.' : `At delta ${o.cfg.delta} and ${o.cfg.dte} DTE (${o.cfg.dteMin}–${o.cfg.dteMax}).`} ${esc((o.at || '').slice(0, 16).replace('T', ' '))}</div>`;
  return `<section class="panel" style="margin-top:12px"><div class="hd"><h2>Optimizer</h2><span class="dim" style="font-size:13px">IV rank × take profit × DTE exit × stop</span><div class="grow"></div>
    <label class="dim" style="font-size:13px;margin-right:10px"><input type="checkbox" id="ivWide"${S.iv.wide ? ' checked' : ''}> also vary delta and DTE (more option data to download)</label>
    <button class="btn" data-iv="opt" ${run ? 'disabled' : ''}>Run optimizer</button></div>${body}</section>`;
}

function ivResHtml(r) {
  const pc = (x, k = 1) => x == null ? '—' : (x * 100).toFixed(k) + '%';
  const m = x => x == null ? '—' : (x < 0 ? '−$' : '$') + Math.abs(x).toLocaleString('en-US', { maximumFractionDigits: 0 });
  const head = '<thead><tr><th></th><th class="r">Trades</th><th class="r">Win</th><th class="r">Total</th><th class="r">CAGR</th><th class="r">Sharpe</th><th class="r">Max DD</th><th class="r">P&amp;L</th><th class="r">Avg / trade</th><th class="r">Avg return on collateral</th><th class="r">t</th><th class="r">Worst</th><th class="r">Held (days)</th><th class="r">Open (avg)</th><th>Exits</th></tr></thead>';
  const row = (lab, x) => !x || !x.n ? `<tr><td>${lab}</td><td class="r">0</td><td colspan="13" class="dim">no trades</td></tr>` :
    `<tr><td>${lab}</td><td class="r">${x.n.toLocaleString('en-US')}</td><td class="r">${pc(x.win, 0)}</td><td class="r ${cls(x.total)}">${pc(x.total)}</td><td class="r">${pc(x.cagr)}</td><td class="r">${x.sharpe ?? '—'}</td><td class="r loss">${pc(x.mdd)}</td><td class="r ${cls(x.pnl)}">${m(x.pnl)}</td><td class="r ${cls(x.avg)}">${m(x.avg)}</td><td class="r">${pc(x.avgRet, 2)}</td><td class="r">${x.t ?? '—'}</td><td class="r loss">${m(x.worst)}</td><td class="r">${x.days}</td><td class="r">${x.avgOpen ?? '—'}</td><td class="dim" style="font-size:12px">${Object.entries(x.why || {}).map(([k, v]) => `${k} ${v}`).join(' · ')}</td></tr>`;
  let svg = '';
  const cv = r.curve, bc = [];
  if (cv.length > 1) {
    const W = 900, H = 240, P = 70, all = cv.concat(bc).map(e => e[1]), lo = Math.min(...all), hi = Math.max(...all);
    const t0 = new Date(cv[0][0]).getTime(), t1 = new Date(cv[cv.length - 1][0]).getTime();
    const X = d => P + (new Date(d).getTime() - t0) / Math.max(1, t1 - t0) * (W - P - 10), Y = v => 10 + (hi - v) / Math.max(1e-9, hi - lo) * (H - 30);
    const sx = X(r.split), line = (a, col) => `<polyline fill="none" stroke="${col}" stroke-width="1.6" points="${a.map(e => X(e[0]).toFixed(1) + ',' + Y(e[1]).toFixed(1)).join(' ')}"/>`;
    svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto"><line x1="${sx}" x2="${sx}" y1="10" y2="${H - 20}" stroke="var(--line)" stroke-dasharray="4 4"/><text x="${sx + 4}" y="20" font-size="11" fill="currentColor" opacity=".6">test →</text>
      ${line(bc, '#9ca3af')}${line(cv, '#3b82f6')}<text x="4" y="${Y(hi) + 4}" font-size="11" fill="currentColor">${m(hi)}</text><text x="4" y="${Y(lo)}" font-size="11" fill="currentColor">${m(lo)}</text></svg>
      <div class="dim" style="font-size:12px;padding:0 16px 8px">equity, puts marked to market daily (mid)</div>`;
  }
  const bk = r.byRank.map(b => `<tr><td>IV rank ${b.range}</td><td class="r">${(b.n || 0).toLocaleString('en-US')}</td><td class="r">${b.n ? pc(b.win, 0) : '—'}</td><td class="r ${cls(b.avg)}">${b.n ? m(b.avg) : '—'}</td><td class="r ${cls(b.avgRet)}">${b.n ? pc(b.avgRet, 2) : '—'}</td><td class="r">${b.t ?? '—'}</td><td class="r loss">${b.n ? m(b.worst) : '—'}</td></tr>`).join('');
  const syms = (a, t) => `<div><b style="font-size:13px">${t}</b><table class="tbl"><tbody>${a.map(g => `<tr><td>${esc(g.sym)}</td><td class="r">${g.n}</td><td class="r">${Math.round(g.win / g.n * 100)}%</td><td class="r ${cls(g.pnl)}">${m(g.pnl)}</td></tr>`).join('')}</tbody></table></div>`;
  const today = (r.today || []).map(x => `<tr><td><b>${esc(x.sym)}</b></td><td class="mono">${esc(x.day)}</td><td class="r">$${x.S.toFixed(2)}</td><td class="r">${x.iv != null ? pc(x.iv, 0) : '—'}</td><td class="r">${x.rank}</td></tr>`).join('');
  const tr = r.trades.slice(-300).reverse().map(t => `<tr><td><b>${esc(t.sym)}</b></td><td class="mono">${esc(t.in)}</td><td class="mono">${esc(t.out)}</td><td>${esc(t.exp)} ${t.K}P</td><td class="r">$${t.S.toFixed(2)}</td><td class="r">${t.rank}</td><td class="r">${t.delta.toFixed(2)}</td><td class="r">$${t.credit.toFixed(2)}</td><td class="r">$${t.exitPx.toFixed(2)}</td><td>${esc(t.why)}</td><td class="r">${t.n}</td><td class="r ${cls(t.pnl)}">${m(t.pnl)}</td></tr>`).join('');
  return `<section class="panel"><div class="hd"><h2>Results</h2><span class="dim" style="font-size:13px">${r.syms} stocks · ${(r.cands || 0).toLocaleString('en-US')} candidate trades · ${esc(r.from)} → ${esc(r.to)} · test from ${esc(r.split)} · ${esc((r.at || '').slice(0, 16).replace('T', ' '))}</span><div class="grow"></div><button class="btn sm" data-iv="csv">Export trades (CSV)</button></div>
      <div class="tbl-wrap"><table class="tbl">${head}<tbody>${row(`<b>IV rank ≥ ${r.cfg.rank}</b> — all`, r.all)}${row('Train', r.train)}${row('Test', r.test)}</tbody></table></div>${svg}</section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>By IV rank at entry</h2><span class="dim" style="font-size:13px">every candidate trade (before the max-open limit), one contract each</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th></th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg / contract</th><th class="r">Avg return on collateral</th><th class="r">t</th><th class="r">Worst</th></tr></thead><tbody>${bk}</tbody></table></div></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Today: IV rank ≥ ${r.cfg.rank}</h2><span class="dim" style="font-size:13px">${r.todayN} stocks at their last day of data, highest first</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Stock</th><th>Day</th><th class="r">Price</th><th class="r">IV</th><th class="r">IV rank</th></tr></thead><tbody>${today || '<tr><td colspan="5" class="dim">none</td></tr>'}</tbody></table></div></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>By stock</h2></div><div class="bd" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">${syms(r.bestSyms, 'Best')}${syms(r.worstSyms, 'Worst')}</div></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Trades</h2><span class="dim" style="font-size:13px">the latest ${Math.min(300, r.trades.length)} of ${r.nTrades.toLocaleString('en-US')}</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Stock</th><th>Sold</th><th>Closed</th><th>Put</th><th class="r">Price</th><th class="r">IV rank</th><th class="r">Delta</th><th class="r">Credit</th><th class="r">Exit</th><th>Why</th><th class="r">Contracts</th><th class="r">P&amp;L</th></tr></thead><tbody>${tr}</tbody></table></div></section>`;
}

document.addEventListener('change', ev => {
  const t = ev.target; if (!t.dataset) return;
  if (t.id === 'ivWide') S.iv.wide = t.checked;
  if (t.dataset.ivv) S.iv.cfg[t.dataset.ivv] = ['coll', 'fill', 'from'].includes(t.dataset.ivv) ? t.value : +t.value;
});
document.addEventListener('click', async ev => {
  const el = ev.target.closest('[data-iv],[data-iv-use]'); if (!el || !$('#sw-body')) return;
  const a = el.dataset.iv, I = S.iv;
  try {
    if (el.dataset.ivUse != null) { Object.assign(I.cfg, I.v.opt.top[+el.dataset.ivUse].p); $('#sw-body').innerHTML = ivShell(I.v); toast('Setting applied: run the backtest.'); return; }
    if (a === 'opt') { el.disabled = true; await api('/ivcrush/opt', { method: 'POST', body: { cfg: I.cfg, wide: !!I.wide } }); toast('Optimizer started: it downloads the option prices it needs the first time.'); setTimeout(ivLoad, 1500); return; }
    if (a === 'data') { el.disabled = true; await api('/ivcrush/data', { method: 'POST', body: {} }); toast('Download started: it runs in the background.'); setTimeout(ivLoad, 1500); return; }
    if (a === 'run') { el.disabled = true; await api('/ivcrush', { method: 'POST', body: { cfg: I.cfg } }); toast('Backtest started: the first run fetches the option prices (can take hours).'); setTimeout(ivLoad, 1500); return; }
    if (a === 'csv') {
      const r = I.v && I.v.res; if (!r) return;
      const rows = [['symbol', 'sold', 'closed', 'expiry', 'strike', 'stock_price', 'iv_rank', 'put_iv', 'delta', 'credit', 'exit_price', 'why', 'contract', 'contracts', 'pnl', 'return_on_collateral', 'days']]
        .concat(r.trades.map(t => [t.sym, t.in, t.out, t.exp, t.K, t.S, t.rank, t.iv0, t.delta, t.credit, t.exitPx, t.why, t.occ, t.n, t.pnl, t.ret, t.days]));
      const b = new Blob([rows.map(x => x.join(',')).join('\n')], { type: 'text/csv' });
      const u = URL.createObjectURL(b), l = document.createElement('a'); l.href = u; l.download = 'ivcrush_trades.csv'; l.click(); URL.revokeObjectURL(u);
    }
  } catch (e) { toast(e.message); ivLoad(); }
});
