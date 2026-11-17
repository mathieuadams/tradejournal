// Beat the Market (Zarattini, Aziz, Barbon): SPY intraday momentum on 1-minute bars, VIX regimes. A tab of the Swing page.
S.im = S.im || { cfg: null };

async function imLoad() {
  const el = $('#sw-body'); if (!el) return;
  let r;
  try { r = await api('/intramom'); } catch (e) { el.innerHTML = `<div class="panel"><div class="bd loss">${esc(e.message)}</div></div>`; return; }
  S.im.v = r;
  if (!S.im.cfg) S.im.cfg = { ...r.cfg };
  el.innerHTML = imShell(r);
  const live = r.job && r.job.status === 'running' && !r.job.stale;
  if (live) { clearTimeout(imLoad.h); imLoad.h = setTimeout(() => S.sw.kind === 'btm' && imLoad(), 4000); }
}

function imShell(r) {
  const c = S.im.cfg, d = r.data, j = r.job || {}, run = j.status === 'running' && !j.stale;
  const sel = (key, opts) => `<select data-imv="${key}">${opts.map(([v, t]) => `<option value="${v}"${String(c[key]) === String(v) ? ' selected' : ''}>${t}</option>`).join('')}</select>`;
  const num = (key, step) => `<input type="number" step="${step}" data-imv="${key}" value="${c[key]}">`;
  const jmsg = j.status === 'error' ? `<div class="bd loss">Last job failed: ${esc(j.msg || '')}</div>` : j.stale ? `<div class="bd loss">The last job stopped answering (${esc(j.msg || '')}): start it again.</div>` : run || j.status === 'done' ? `<div class="bd dim">${esc(j.msg || '')}</div>` : '';
  const o = r.opt;
  const st = x => !x || !x.days ? '—' : `${(x.total * 100).toFixed(0)}% · Sharpe ${x.sharpe ?? '—'} · DD ${(x.mdd * 100).toFixed(0)}%`;
  const optHtml = !o ? '<div class="bd dim">Not run yet.</div>' : !o.top || !o.top.length ? `<div class="bd dim">Running…</div>` :
    `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Setting</th><th class="r">Train (before ${esc(o.split || '')}): return · Sharpe · max DD</th><th class="r">Test</th><th></th></tr></thead><tbody>
    ${o.top.map((x, i) => `<tr><td>VM ${x.p.vm} · stop ${x.p.stop} · ${x.p.sizing === 'dyn' ? 'vol target' : '100%'} · every ${x.p.step} min${x.p.vixMin ? ' · VIX ≥ ' + x.p.vixMin : ''}</td><td class="r">${st(x.train)}</td><td class="r ${cls(x.test.total)}">${st(x.test)}</td><td><button class="btn sm" data-im-use="${i}">Use</button></td></tr>`).join('')}</tbody></table></div>
    <div class="note" style="padding:6px 16px 12px">${o.total || r.grid} settings, ranked on train Sharpe only.</div>`;
  return `<section class="panel"><div class="hd"><h2>Beat the Market — SPY intraday momentum</h2><div class="grow"></div>${run ? `<span class="dim">${esc(j.msg || 'Running…')}</span>` : ''}</div>
    <div class="bd" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><b>Data</b><span class="dim">${d.days ? `SPY 1-minute: ${d.days.toLocaleString('en-US')} sessions, ${esc(d.first)} → ${esc(d.last)} · VIX (FMP): ${d.vix ? esc(d.vixFirst) + ' → ' + esc(d.vixLast) : 'none (connect FMP for the VIX)'}` : 'none yet'}</span><div class="grow"></div>
      <button class="btn" data-im="data" ${run ? 'disabled' : ''}>${d.days ? 'Update' : 'Download'} SPY 1-minute + VIX</button></div>${jmsg}
    <div class="bd" style="display:flex;gap:12px;flex-wrap:wrap;align-items:end;border-top:1px solid var(--line)">
      <label class="f">Noise area × (VM)${sel('vm', [[0.75, '0.75'], [1, '1 (paper)'], [1.25, '1.25'], [1.5, '1.5'], [2, '2']])}</label>
      <label class="f">Stop${sel('stop', [['vwap', 'Current band or VWAP (paper final)'], ['band', 'Current band'], ['opp', 'Opposite band (reverse)']])}</label>
      <label class="f">Checks every${sel('step', [[15, '15 min'], [30, '30 min (paper)'], [60, '60 min']])}</label>
      <label class="f">Size${sel('sizing', [['dyn', 'Volatility target (paper)'], ['full', '100% of the account']])}</label>
      <label class="f">Daily vol target${sel('target', [[0.01, '1%'], [0.015, '1.5%'], [0.02, '2% (paper)'], [0.03, '3%']])}</label>
      <label class="f">Max leverage${sel('maxLev', [[1, '1×'], [2, '2×'], [4, '4× (paper)']])}</label>
      <label class="f">Sides${sel('sides', [['both', 'Long and short'], ['long', 'Long only'], ['short', 'Short only']])}</label>
      <label class="f">Trade only if VIX opens ≥${num('vixMin', 1)}</label><label class="f">… and ≤ (0 = no limit)${num('vixMax', 1)}</label>
      <label class="f">Account ($)${num('acct', 1000)}</label><label class="f">Commission $ / share${num('comm', 0.0005)}</label><label class="f">Slippage $ / share${num('slip', 0.0005)}</label>
      <button class="btn primary" data-im="run">Run backtest</button></div>
    <p class="note" style="padding:0 16px 14px">Noise area for each time of day: the average |move from the open| at that time over the previous 14 sessions; upper bound = max(open, yesterday's close) × (1 + VM × σ), lower = min(…) × (1 − VM × σ). At each check (10:00, 10:30 … 15:30) long above the upper bound, short below the lower; stop at the same checks; flat at 16:00. Volatility target: account × min(max leverage, target ÷ σ of SPY's last 14 daily returns). Paper: 2007–2024 on IQFeed; here Alpaca 1-minute since 2016. The last 5 years are the test period.</p></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Optimizer</h2><span class="dim" style="font-size:13px">VM × stop × size × check interval × VIX floor</span><div class="grow"></div><button class="btn" data-im="opt" ${run ? 'disabled' : ''}>Run optimizer</button></div>${optHtml}</section>
    <div id="im-res" style="margin-top:12px">${r.res ? imResHtml(r.res) : ''}</div>`;
}

function imResHtml(r) {
  const pc = x => x == null ? '—' : (x * 100).toFixed(1) + '%';
  const m = x => x == null ? '—' : (x < 0 ? '−$' : '$') + Math.abs(x).toLocaleString('en-US', { maximumFractionDigits: 0 });
  const head = '<thead><tr><th></th><th class="r">Days</th><th class="r">Total</th><th class="r">CAGR</th><th class="r">Vol</th><th class="r">Sharpe</th><th class="r">Max DD</th><th class="r">Hit</th><th class="r">Trades</th><th class="r">Best day</th><th class="r">Worst day</th></tr></thead>';
  const row = (lab, x) => !x || !x.days || x.total == null ? `<tr><td>${lab}</td><td class="r">${x && x.days || 0}</td><td colspan="9" class="dim">too few days</td></tr>` :
    `<tr><td>${lab}</td><td class="r">${x.days}</td><td class="r ${cls(x.total)}">${pc(x.total)}</td><td class="r">${pc(x.cagr)}</td><td class="r">${pc(x.vol)}</td><td class="r">${x.sharpe ?? '—'}</td><td class="r loss">${pc(x.mdd)}</td><td class="r">${pc(x.hit)}</td><td class="r">${(x.trades || 0).toLocaleString('en-US')}</td><td class="r gain">${pc(x.best)}</td><td class="r loss">${pc(x.worst)}</td></tr>`;
  let svg = '';
  const cv = r.curve, sp = r.spyCurve;
  if (cv.length > 1) {
    const W = 900, H = 240, P = 70, all = cv.concat(sp).map(e => e[1]), lo = Math.min(...all), hi = Math.max(...all);
    const L = v => Math.log(Math.max(1, v)), t0 = new Date(cv[0][0]).getTime(), t1 = new Date(cv[cv.length - 1][0]).getTime();
    const X = d => P + (new Date(d).getTime() - t0) / Math.max(1, t1 - t0) * (W - P - 10), Y = v => 10 + (L(hi) - L(v)) / Math.max(1e-9, L(hi) - L(lo)) * (H - 30);
    const sx = X(r.split), line = (a, col) => `<polyline fill="none" stroke="${col}" stroke-width="1.6" points="${a.map(e => X(e[0]).toFixed(1) + ',' + Y(e[1]).toFixed(1)).join(' ')}"/>`;
    svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto"><line x1="${sx}" x2="${sx}" y1="10" y2="${H - 20}" stroke="var(--line)" stroke-dasharray="4 4"/><text x="${sx + 4}" y="20" font-size="11" fill="currentColor" opacity=".6">test →</text>
      ${line(sp, '#9ca3af')}${line(cv, '#3b82f6')}<text x="4" y="${Y(hi) + 4}" font-size="11" fill="currentColor">${m(hi)}</text><text x="4" y="${Y(lo)}" font-size="11" fill="currentColor">${m(lo)}</text></svg>`;
  }
  const ys = r.years.map(y => `<tr><td>${y.year}</td><td class="r ${cls(y.total)}">${pc(y.total)}</td><td class="r">${y.sharpe ?? '—'}</td><td class="r loss">${pc(y.mdd)}</td><td class="r ${cls(y.spy)}">${pc(y.spy)}</td></tr>`).join('');
  const simple = (lab, xs, key) => `<section class="panel"><div class="hd"><h2>${lab}</h2></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th></th><th class="r">Days</th><th class="r">Total</th><th class="r">Sharpe</th><th class="r">Hit</th></tr></thead><tbody>
    ${xs.map(x => `<tr><td>${esc(String(x[key]))}${x.range ? ' <span class="dim">(' + esc(x.range) + ')</span>' : ''}</td><td class="r">${x.days}</td><td class="r ${cls(x.total)}">${pc(x.total)}</td><td class="r">${x.sharpe ?? '—'}</td><td class="r">${pc(x.hit)}</td></tr>`).join('')}</tbody></table></div></section>`;
  return `<section class="panel"><div class="hd"><h2>Results</h2><span class="dim" style="font-size:13px">${m(r.cfg.acct)} → <b>${m(r.final)}</b> · ${esc(r.at.replace('T', ' ').slice(0, 16))}</span></div>
    <div class="tbl-wrap"><table class="tbl">${head}<tbody>${row(`Train (before ${esc(r.split)})`, r.train)}${row(`<b>Test (from ${esc(r.split)})</b>`, r.test)}${row('All', r.all)}
      ${row('<span class="dim">SPY buy &amp; hold, test</span>', r.spy.test)}${row('<span class="dim">SPY buy &amp; hold, all</span>', r.spy.all)}</tbody></table></div>
    <div class="bd">${svg}<div class="dim" style="font-size:12px">Account (blue) vs SPY buy &amp; hold (grey), log scale.</div></div></section>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px">
      <section class="panel"><div class="hd"><h2>By year</h2></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Year</th><th class="r">Return</th><th class="r">Sharpe</th><th class="r">Max DD</th><th class="r">SPY</th></tr></thead><tbody>${ys}</tbody></table></div></section>
      ${r.vix.length ? simple('Days when the VIX opens ≥', r.vix, 'vix') : '<section class="panel"><div class="bd dim">No VIX data (connect FMP, then update the data).</div></section>'}</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-top:12px">${simple('Day of the week', r.dow, 'day')}${r.rsi.length ? simple('SPY 5-day RSI the day before', r.rsi, 'rsi') : ''}
      <section class="panel"><div class="hd"><h2>Long vs short</h2></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th></th><th class="r">Trades</th><th class="r">P/L</th><th class="r">Win</th></tr></thead><tbody>
      ${r.sides.map(s => `<tr><td>${s.side}</td><td class="r">${s.n.toLocaleString('en-US')}</td><td class="r ${cls(s.pnl)}">${m(s.pnl)}</td><td class="r">${pc(s.win)}</td></tr>`).join('')}</tbody></table></div></section></div>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Trades</h2><span class="dim" style="font-size:13px">the latest ${r.trades.length} of ${r.nTrades.toLocaleString('en-US')}</span><div class="grow"></div><button class="btn sm" data-im="csv">Export trades (CSV)</button></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Day</th><th>In</th><th>Out</th><th>Side</th><th class="r">Entry</th><th class="r">Exit</th><th class="r">Shares</th><th class="r">P/L</th></tr></thead><tbody>
      ${r.trades.slice().reverse().map(t => `<tr><td class="mono">${esc(t.day)}</td><td class="mono">${t.in}</td><td class="mono">${t.out}</td><td>${t.side > 0 ? 'long' : 'short'}</td><td class="r">${t.entry}</td><td class="r">${t.exit}</td><td class="r">${t.shares.toLocaleString('en-US')}</td><td class="r ${cls(t.pnl)}">${m(t.pnl)}</td></tr>`).join('')}</tbody></table></div></section>`;
}

document.addEventListener('change', ev => {
  const t = ev.target; if (!t.dataset || !t.dataset.imv || !S.im.cfg) return;
  const k = t.dataset.imv;
  S.im.cfg[k] = ['stop', 'sizing', 'sides'].includes(k) ? t.value : +t.value;
});

document.addEventListener('click', async ev => {
  const el = ev.target.closest('[data-im],[data-im-use]');
  if (!el || !$('#sw-body')) return;
  const ds = el.dataset;
  try {
    if (ds.imUse) { Object.assign(S.im.cfg, S.im.v.opt.top[+ds.imUse].p); $('#sw-body').innerHTML = imShell(S.im.v); toast('Setting applied: run the backtest.'); return; }
    if (ds.im === 'data') { el.disabled = true; await api('/intramom/data', { method: 'POST', body: {} }); toast('Download started.'); setTimeout(imLoad, 1500); return; }
    if (ds.im === 'opt') { el.disabled = true; await api('/intramom/opt', { method: 'POST', body: { cfg: S.im.cfg } }); toast('Optimizer started.'); setTimeout(imLoad, 1500); return; }
    if (ds.im === 'run') {
      el.disabled = true; el.textContent = 'Running…';
      const r = await api('/intramom', { method: 'POST', body: { cfg: S.im.cfg } });
      S.im.v.res = r; $('#im-res').innerHTML = imResHtml(r); el.disabled = false; el.textContent = 'Run backtest'; return;
    }
    if (ds.im === 'csv') {
      const r = S.im.v && S.im.v.res; if (!r) return;
      const keys = ['day', 'in', 'out', 'side', 'entry', 'exit', 'shares', 'pnl'];
      const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([[keys.join(',')].concat(r.trades.map(t => keys.map(k => t[k]).join(','))).join('\n')], { type: 'text/csv' }));
      a.download = 'beat_the_market_trades.csv'; a.click(); URL.revokeObjectURL(a.href); return;
    }
  } catch (e) { toast(e.message); el.disabled = false; if (ds.im === 'run') el.textContent = 'Run backtest'; }
});
