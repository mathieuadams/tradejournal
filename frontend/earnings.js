/* Earnings page: fundamentals screen (SEC EDGAR), company quarters + release reactions, and the earnings event study.
   Uses the helpers of app.js ($, api, esc, view, head, toast, S, jobHtml). */
S.en = { tab: 'screen', sym: '', out: 'r1ab', sort: 'rev3y', dir: -1, f: { cagr: '', ttm: '', qoq: '', accel: '', infl: false, noWeak: false }, rsort: 'date', rdir: -1, rshow: 100 };

const pct = (v, d = 1) => v == null || !Number.isFinite(+v) ? '—' : (v < 0 ? '−' : v > 0 ? '+' : '') + Math.abs(v * 100).toFixed(d) + '%';
const pp = (v, d = 1) => v == null || !Number.isFinite(+v) ? '—' : (v < 0 ? '−' : v > 0 ? '+' : '') + Math.abs(v * 100).toFixed(d) + ' pp';
const big = v => { if (v == null) return '—'; const a = Math.abs(v), s = v < 0 ? '−' : ''; return a >= 1e9 ? s + '$' + (a / 1e9).toFixed(2) + 'B' : a >= 1e6 ? s + '$' + (a / 1e6).toFixed(1) + 'M' : s + '$' + Math.round(a).toLocaleString('en-US'); };
const QCOL = { strong: 'gain', weak: 'loss', ok: '' };
const tstat = t => t == null ? '—' : `<span class="${Math.abs(t) >= 2 ? (t > 0 ? 'gain' : 'loss') : 'dim'}">${t.toFixed(1)}</span>`;

async function vEarnings() {
  const E = S.en;
  const d = await api('/fund');
  S.enD = d;
  view(`${head('Earnings', 'Revenue growth, EPS inflection and earnings quality from SEC filings; how releases reprice the stock.')}
    <div class="seg" style="margin-bottom:12px">${[['screen', 'Screen'], ['company', 'Company'], ['today', 'Today'], ['study', 'Event study'], ['bt', 'Strategy backtest']].map(([k, l]) => `<button data-entab="${k}" aria-pressed="${E.tab === k}">${l}</button>`).join('')}</div>
    <div id="en-body"></div><div id="job" style="margin-top:12px">${jobHtml(d.job)}</div>`);
  if (d.job && d.job.status === 'running') pollJob('/fund', r => r.job, r => { S.enD = r; if (E.tab === 'study' && r.job.job === 'fundstudy') { S.enStudy = null; } enBody(); });
  return enBody();
}
function enBody() {
  const E = S.en, b = $('#en-body');
  if (!b) return;
  if (E.tab === 'screen') b.innerHTML = enScreen();
  else if (E.tab === 'company') { b.innerHTML = enCompanyShell(); if (E.sym) enCompanyLoad(); }
  else if (E.tab === 'today') { b.innerHTML = enTodayShell(); enTodayLoad(); }
  else if (E.tab === 'bt') { b.innerHTML = enBtShell(); enOptLoad(); if (S.enBt) $('#en-bt-res').innerHTML = enBtHtml(S.enBt); }
  else { b.innerHTML = enStudyShell(); enStudyLoad(); }
}

/* ---------- Screen ---------- */
const SCOLS = [['sym', 'Ticker', 0], ['end', 'Last quarter', 0], ['revTTM', 'Revenue TTM', 1], ['rev3y', '3Y CAGR', 1], ['revTTMYoY', 'TTM YoY', 1], ['revYoY', 'Quarter YoY', 1],
  ['revQoQ', 'QoQ', 1], ['revAccel', 'YoY change', 1], ['trail', 'YoY, 8 quarters', 0], ['epsTTM', 'EPS TTM', 1], ['epsYoY', 'EPS YoY', 1], ['flags', 'Inflection', 0],
  ['cashConv', 'CFO / NI', 1], ['accruals', 'Accruals', 1], ['quality', 'Quality', 0], ['lastRelease', 'Last release', 0]];
function enFiltered() {
  const E = S.en, f = E.f, rows = (S.enD.rows || []).slice();
  const min = (v, x) => x === '' || x == null || (v != null && v * 100 >= +x);
  const ok = rows.filter(r => r.missing || r.empty || (min(r.rev3y, f.cagr) && min(r.revTTMYoY, f.ttm) && min(r.revQoQ, f.qoq) && min(r.revAccel, f.accel)
    && (!f.infl || (r.flags || []).some(x => x.includes('EPS'))) && (!f.noWeak || r.quality !== 'weak')));
  const k = E.sort;
  ok.sort((a, b) => { const x = a[k], y = b[k]; if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1; return (typeof x === 'string' ? x.localeCompare(y) : x - y) * E.dir; });
  return ok;
}
function trailSpark(xs) {
  const v = (xs || []).filter(x => x != null); if (v.length < 2) return '';
  const lo = Math.min(...v, 0), hi = Math.max(...v), W = 80, H = 22, n = xs.length;
  const pts = xs.map((x, i) => x == null ? null : `${(i / (n - 1) * W).toFixed(1)},${(H - (x - lo) / (hi - lo || 1) * H).toFixed(1)}`).filter(Boolean).join(' ');
  const z = H - (0 - lo) / (hi - lo || 1) * H;
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="display:block"><line x1="0" x2="${W}" y1="${z}" y2="${z}" stroke="var(--line2)"/><polyline points="${pts}" fill="none" stroke="#4A9EFF" stroke-width="1.5"/></svg>`;
}
function enScreen() {
  const d = S.enD, E = S.en, f = E.f, rows = enFiltered();
  const cell = (r, k) => {
    if (k === 'sym') return `<td><a class="name" href="#earnings" data-enco="${esc(r.sym)}">${esc(r.sym)}</a><div class="sub">${esc(r.name || '')}</div></td>`;
    if (r.missing) return k === 'end' ? `<td class="muted" colspan="${SCOLS.length - 1}">Not loaded yet: <b>Update fundamentals</b>.</td>` : '';
    if (r.empty) return k === 'end' ? `<td class="muted" colspan="${SCOLS.length - 1}">No quarterly XBRL revenue / EPS found in SEC filings.</td>` : '';
    const v = r[k];
    switch (k) {
      case 'end': return `<td class="mono">${esc(v || '—')}${(r.d || []).length ? ' <span class="dim" title="Derived: ' + esc(r.d.join(', ')) + ' (from year-to-date or full-year figures)">ᵈ</span>' : ''}</td>`;
      case 'revTTM': return `<td class="r">${big(v)}</td>`;
      case 'epsTTM': return `<td class="r ${cls(v)}">${v == null ? '—' : (v < 0 ? '−' : '') + '$' + Math.abs(v).toFixed(2)}</td>`;
      case 'revAccel': return `<td class="r ${cls(v)}">${pp(v)}</td>`;
      case 'trail': return `<td>${trailSpark(r.yoyTrail)}</td>`;
      case 'flags': return `<td>${(v || []).map(x => `<span class="pill ${x.includes('EPS') ? 'ok' : ''}" style="margin:0 4px 4px 0;display:inline-block">${esc(x)}</span>`).join('') || '<span class="dim">—</span>'}</td>`;
      case 'cashConv': return `<td class="r">${v == null ? '—' : v.toFixed(2) + '×'}</td>`;
      case 'accruals': return `<td class="r ${v > 0.1 ? 'loss' : ''}">${pct(v)}</td>`;
      case 'quality': return `<td class="${QCOL[v] || 'dim'}">${esc(v || '—')}</td>`;
      case 'lastRelease': return `<td class="mono dim">${esc(v || '—')}</td>`;
      default: return `<td class="r ${cls(v)}">${pct(v)}</td>`;
    }
  };
  const inp = (k, lab) => `<label class="f">${lab}<input type="number" step="1" data-enf="${k}" value="${esc(f[k])}" placeholder="any" style="width:110px"></label>`;
  return `<section class="panel"><div class="hd"><h2>Tickers</h2><span class="dim" style="font-size:13px">US SEC filers; up to 150</span></div>
      <div class="bd"><textarea id="en-tickers" rows="2" style="width:100%;font-family:var(--mono)" placeholder="NVDA CRWD AXON …">${esc(d.settings.tickers.join(' '))}</textarea>
      <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap"><button class="btn" data-en="save-tickers">Save</button><button class="btn primary" data-en="refresh">Update fundamentals</button>
      <span class="note" style="align-self:center">Quarters come from each company's 10-Q / 10-K XBRL. Q4 and quarterly cash flow are derived (full year − 9 months); marked ᵈ.</span></div></div></section>
    <section class="panel" style="margin-top:12px"><div class="bd" style="display:flex;gap:12px;flex-wrap:wrap;align-items:end">
      ${inp('cagr', 'Revenue 3Y CAGR ≥ %')}${inp('ttm', 'TTM YoY ≥ %')}${inp('qoq', 'QoQ ≥ %')}${inp('accel', 'YoY change ≥ pp')}
      <label class="check"><input type="checkbox" data-enf="infl" ${f.infl ? 'checked' : ''}> EPS inflection only</label>
      <label class="check"><input type="checkbox" data-enf="noWeak" ${f.noWeak ? 'checked' : ''}> Hide weak earnings quality</label>
      <div class="grow"></div><span class="dim">${rows.length} of ${(d.rows || []).length}</span></div></section>
    <section class="panel" style="margin-top:12px">${(d.rows || []).length ? `<div class="tbl-wrap"><table class="tbl" style="min-width:1400px"><thead><tr>${SCOLS.map(([k, l, num]) =>
      `<th class="${num ? 'r' : ''}" style="cursor:pointer" data-ensort="${k}">${l}${E.sort === k ? (E.dir < 0 ? ' ↓' : ' ↑') : ''}</th>`).join('')}</tr></thead>
      <tbody>${rows.map(r => `<tr>${SCOLS.map(([k]) => cell(r, k)).join('')}</tr>`).join('')}</tbody></table></div>
      <div class="note" style="padding:10px 16px">Quality: accrual ratio = (net income − operating cash flow) / average assets, TTM. Strong: under 5% and cash conversion ≥ 0.9×. Weak: over 10%, or cash conversion under 0.6× with positive earnings.</div>`
      : '<div class="empty">Add tickers above, save, then <b>Update fundamentals</b>.</div>'}</section>`;
}

/* ---------- Company ---------- */
function enCompanyShell() {
  const E = S.en;
  return `<section class="panel"><div class="bd" style="display:flex;gap:10px;flex-wrap:wrap;align-items:end">
      <label class="f">Ticker<input id="en-sym" value="${esc(E.sym)}" placeholder="NVDA" style="width:120px;text-transform:uppercase"></label>
      <button class="btn primary" data-en="co-load">Load</button><button class="btn" data-en="co-update" ${E.sym ? '' : 'disabled'}>Update from SEC</button>
      <span class="note">Releases are the company's 8-K filings with item 2.02 (results of operations), timed by their EDGAR acceptance time.</span></div></section>
    <div id="en-co" style="margin-top:12px">${E.sym ? '<div class="panel"><div class="empty">Loading…</div></div>' : ''}</div>`;
}
async function enCompanyLoad() {
  const E = S.en, out = $('#en-co');
  try {
    const c = await api('/fund/company?sym=' + encodeURIComponent(E.sym));
    S.enCo = c;
    out.innerHTML = `<div id="en-tx-view"></div><div id="en-proj"><section class="panel"><div class="empty">Projecting the next release…</div></section></div>` + enCompanyHtml(c);
    enProjLoad();
  } catch (e) { out.innerHTML = `<div class="panel"><div class="empty loss">${esc(e.message)}</div></div>`; }
}
function enCompanyHtml(c) {
  const qs = (c.quarters || []).slice(-16).reverse(), rs = c.reactions || [];
  const dm = (q, k) => (q.d || []).includes(k) ? '<span class="dim">ᵈ</span>' : '';
  const ok = rs.filter(r => r.r1ab != null);
  const avgAbs = ok.length ? ok.reduce((a, r) => a + Math.abs(r.r1ab), 0) / ok.length : null;
  const up = ok.length ? ok.filter(r => r.r1ab > 0).length / ok.length : null;
  return `<section class="panel"><div class="hd"><h2>${esc(c.name || c.sym)}</h2><span class="dim" style="font-size:13px">${esc(c.sym)} · CIK ${c.cik} · fiscal year ends ${esc((c.fiscalYearEnd || '').replace(/(\d\d)(\d\d)/, '$1/$2'))} · SEC data ${esc((c.at || '').replace('T', ' ').slice(0, 16))}</span></div>
    <div class="tbl-wrap"><table class="tbl" style="min-width:1300px"><thead><tr><th>Quarter end</th><th class="r">Revenue</th><th class="r">YoY</th><th class="r">QoQ</th><th class="r">YoY change</th><th class="r">TTM</th><th class="r">3Y CAGR</th>
      <th class="r">EPS</th><th class="r">EPS YoY</th><th class="r">EPS TTM</th><th class="r">NI TTM</th><th class="r">CFO TTM</th><th class="r">CFO / NI</th><th class="r">Accruals</th><th>Quality</th><th>Flags</th></tr></thead><tbody>
    ${qs.map(q => `<tr><td class="mono">${esc(q.end)}</td><td class="r">${big(q.rev)}${dm(q, 'rev')}</td><td class="r ${cls(q.revYoY)}">${pct(q.revYoY)}</td><td class="r ${cls(q.revQoQ)}">${pct(q.revQoQ)}</td>
      <td class="r ${cls(q.revAccel)}">${pp(q.revAccel)}</td><td class="r">${big(q.revTTM)}</td><td class="r">${pct(q.rev3y)}</td><td class="r ${cls(q.eps)}">${q.eps == null ? '—' : q.eps.toFixed(2)}${dm(q, 'eps')}</td>
      <td class="r ${cls(q.epsYoY)}">${pct(q.epsYoY)}</td><td class="r">${q.epsTTM == null ? '—' : q.epsTTM.toFixed(2)}</td><td class="r">${big(q.niTTM)}</td><td class="r">${big(q.cfoTTM)}</td>
      <td class="r">${q.cashConv == null ? '—' : q.cashConv.toFixed(2) + '×'}</td><td class="r ${q.accruals > 0.1 ? 'loss' : ''}">${pct(q.accruals)}</td><td class="${QCOL[q.quality] || 'dim'}">${esc(q.quality || '—')}</td>
      <td>${(q.flags || []).map(esc).join('<br>') || '<span class="dim">—</span>'}</td></tr>`).join('')}</tbody></table></div></section>
  <section class="panel" style="margin-top:12px"><div class="hd"><h2>Earnings releases</h2><span class="dim" style="font-size:13px">${rs.length} since ${esc(rs.length ? rs[rs.length - 1].date : '—')}${c.bench ? ' · vs ' + esc(c.bench) : ''}${avgAbs != null ? ` · average day-0 move ${(avgAbs * 100).toFixed(1)}% · up ${(up * 100).toFixed(0)}% of the time` : ''}</span></div>
    ${c.reactionsError ? `<div class="note" style="padding:10px 16px">Price reactions need Alpaca (Data page): ${esc(c.reactionsError)}</div>` : ''}
    <div class="tbl-wrap"><table class="tbl" style="min-width:1200px"><thead><tr><th>Release</th><th>Time ET</th><th>Reaction day</th><th>Quarter</th><th class="r">Rev YoY</th><th class="r">YoY change</th><th class="r">EPS YoY</th>
      <th class="r">Run-up 20d</th><th class="r">Gap</th><th class="r">Day 0</th><th class="r">Day 0 vs bench</th><th class="r">σ</th><th class="r">5d vs bench</th><th class="r">20d vs bench</th><th class="r">2m vs bench</th><th class="r">Drift d1→d20</th><th class="r">Call tone</th><th>Guidance (call)</th></tr></thead><tbody>
    ${rs.map(r => `<tr><td class="mono">${esc(r.date)}</td><td class="mono dim">${esc(r.time || '')} ${esc(r.timing)}</td><td class="mono">${esc(r.day0 || '—')}</td><td class="mono dim">${esc(r.qEnd || '—')}</td>
      <td class="r ${cls(r.revYoY)}">${pct(r.revYoY)}</td><td class="r ${cls(r.revAccel)}">${pp(r.revAccel)}</td><td class="r ${cls(r.epsYoY)}">${pct(r.epsYoY)}</td><td class="r ${cls(r.runup20)}">${pct(r.runup20)}</td>
      <td class="r ${cls(r.gap)}">${pct(r.gap)}</td><td class="r ${cls(r.r1)}">${pct(r.r1)}</td><td class="r ${cls(r.r1ab)}"><b>${pct(r.r1ab)}</b></td><td class="r">${r.z1 == null ? '—' : r.z1.toFixed(1)}</td>
      <td class="r ${cls(r.r5ab)}">${pct(r.r5ab)}</td><td class="r ${cls(r.r20ab)}">${pct(r.r20ab)}</td><td class="r ${cls(r.r40ab)}">${pct(r.r40ab)}</td><td class="r ${cls(r.drift20)}">${pct(r.drift20)}</td>
      <td class="r ${cls(r.txTone)}">${r.txTone == null ? '—' : `<a href="#earnings" data-entx="${esc(r.txDate)}">${r.txTone.toFixed(2)}</a>`}</td><td>${r.txGuideDir ? `<span class="pill ${r.txGuideDir === 'raise' ? 'ok' : r.txGuideDir === 'lower' ? 'warn' : ''}">${esc(r.txGuideDir)}</span>` : '<span class="dim">—</span>'}</td></tr>`).join('') || `<tr><td colspan="18" class="dim">No 8-K item 2.02 filings found.</td></tr>`}</tbody></table></div></section>`;
}

/* ---------- Projection: the next release ---------- */
S.en.pmode = 'ab';
async function enProjLoad() {
  const el = $('#en-proj'); if (!el) return;
  try { S.enProj = await api('/fund/project?sym=' + encodeURIComponent(S.en.sym)); el.innerHTML = enProjHtml(S.enProj); }
  catch (e) { el.innerHTML = `<section class="panel"><div class="hd"><h2>Next release: projection</h2></div><div class="bd note loss">${esc(e.message)}</div></section>`; }
}
function enProjHtml(p) {
  const m = S.en.pmode, n = p.now || {}, nx = p.next;
  const x = v => v == null ? '—' : v.toFixed(2) + '×';
  const SIG = { prevRevYoY: ['Revenue YoY (last quarter)', pct], prevRevAccel: ['Revenue YoY change', pp], prevEpsYoY: ['EPS YoY (last quarter)', pct],
    estEpsG: ['Consensus EPS growth', pct], estRevG: ['Consensus revenue growth', pct],
    pre1ab: ['Last day vs ' + p.bench, pct], pre5ab: ['5 days vs ' + p.bench, pct], pre21ab: ['1 month vs ' + p.bench, pct], pre63ab: ['3 months vs ' + p.bench, pct],
    offHigh: ['From 52w high', pct], sma50: ['vs 50-day average', pct], sma200: ['vs 200-day average', pct], vol20: ['Daily volatility (20d)', v => pct(v, 2)],
    relVol1: ['Volume last day (× 50d avg)', x], relVol5: ['Volume 5 days (× 50d avg)', x], relVol20: ['Volume 20 days (× 50d avg)', x],
    cmf20: ['Accumulation (CMF 20d)', v => v == null ? '—' : v.toFixed(2)], obv20: ['On-balance volume 20d', v => v == null ? '—' : v.toFixed(2)],
    rangePos: ['Position in the 52-week range', v => (v * 100).toFixed(0) + '% (0 low … 100 high)'], offLow: ['Above the 52w low', pct], beta: ['Beta (1 year)', v => v.toFixed(2)],
    spy63: ['Market 3 months', pct], spyVs200: ['Market vs 200-day avg', pct],
    volTrend42: ['Volume 2 months (× 1-year avg)', x], volTrend126: ['Volume 6 months (× 1-year avg)', x], udVol126: ['Up-day volume share, 6 months', v => (v * 100).toFixed(0) + '%'],
    cmf126: ['Accumulation (CMF 6 months)', v => v.toFixed(2)], pre126ab: ['6 months vs ' + p.bench, pct],
    ownAbs: ['Own average |day-0 move|', pct], gradeNet90: ['Analyst net upgrades, 90 days', v => (v > 0 ? '+' : '') + v],
    prevTxTone: ['Last call tone', v => v.toFixed(2)], prevTxGuide: ['Last call guidance score', v => v.toFixed(2)] };
  const ptv = p.priceTarget;
  const range = d => d.n ? `<span class="mono">${pct(d.p25)} … ${pct(d.p75)}</span><div class="sub">10–90%: ${pct(d.p10)} … ${pct(d.p90)}</div>` : '—';
  const col = d => `<td class="r ${cls(d.median)}">${d.n ? pct(d.median) : '—'}</td><td class="r">${range(d)}</td><td class="r">${d.hit == null ? '—' : (d.hit * 100).toFixed(0) + '%'}</td><td class="r">${d.n || 0}</td>`;
  const VER = { edge: ['ok', 'Tested edge'], weak: ['', 'Weak'], none: ['loss', 'No tested edge'] };
  const md = p.model;
  const modelHtml = !md || !md.length ? `<div class="note" style="padding:0 16px 12px">Model: not trained yet. Run the Event study on a broad universe (300+ releases; the S&P 500 over 10 years gives ~20,000): the model is trained and tested there.</div>`
    : `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Model (walk-forward tested)</th><th class="r">Expected</th><th>Verdict</th><th class="r">Hit rate</th><th class="r">Always-up baseline</th><th class="r">Rank corr. / quarter</th><th class="r">t</th><th class="r">Out-of-sample R²</th><th class="r">Tested on</th></tr></thead><tbody>
      ${md.map(t => `<tr><td>${esc(t.label)}</td><td class="r ${t.key === 'abs1' ? '' : cls(t.pred)}"><b>${t.key === 'abs1' ? '±' + (Math.abs(t.pred) * 100).toFixed(1) + '%' : pct(t.pred, 2)}</b></td>
        <td><span class="pill ${(VER[t.verdict] || ['', '—'])[0]}">${(VER[t.verdict] || ['', '—'])[1]}</span></td><td class="r">${t.hit == null ? '—' : (t.hit * 100).toFixed(1) + '%'}</td><td class="r dim">${t.hitBase == null ? '—' : (t.hitBase * 100).toFixed(1) + '%'}</td>
        <td class="r">${t.ic == null ? '—' : t.ic.toFixed(3)}</td><td class="r">${tstat(t.icT)}</td><td class="r">${t.r2 == null ? '—' : (t.r2 * 100).toFixed(2) + '%'}</td><td class="r dim">${t.n || 0}</td></tr>`).join('')}</tbody></table></div>
      <div class="note" style="padding:6px 16px 12px">Only trust a horizon marked “Tested edge” (rank correlation t ≥ 2, positive out-of-sample R², hit rate above the baseline). “Expected” is the model's average outcome for setups like this one, not a price target.</div>`;
  const when = nx ? (nx.estimated ? `≈ ${esc(nx.date)} (${esc(nx.timing)}, estimated: last release ${esc(nx.last)} + ${nx.spacing} days)`
    : `${esc(nx.date)} (${esc(nx.timing || '?')}, FMP calendar) · consensus EPS ${nx.epsEst == null ? '—' : nx.epsEst.toFixed(2)} · revenue ${big(nx.revEst)}`) : 'no past releases';
  const sz = (md || []).find(t => t.key === 'abs1'), im = p.implied, h1 = (p.horizons || [])[0] || {};
  const own = (h1.own || {}).ab || {};
  const sizeHtml = `<div class="kv" style="grid-template-columns:repeat(4,minmax(0,1fr));margin-bottom:12px">
      <div><div class="k">Expected size of the day-0 move (model)</div><div class="v" style="font-size:20px">${sz ? '±' + (Math.abs(sz.pred) * 100).toFixed(1) + '%' : '—'}</div><div class="sub">${sz ? `tested rank corr. ${sz.ic == null ? '—' : sz.ic.toFixed(2)} (t ${sz.icT == null ? '—' : sz.icT.toFixed(0)})` : 'run the event study'}</div></div>
      <div><div class="k">Options imply</div><div class="v" style="font-size:20px">${im && im.move != null ? '±' + (im.move * 100).toFixed(1) + '%' : '—'}</div><div class="sub">${im && im.move != null ? `${esc(im.expiry)} straddle at $${im.strike}: $${im.call} + $${im.put}` : im && im.error ? esc(im.error) : 'no option quotes'}</div></div>
      <div><div class="k">Model ÷ options</div><div class="v" style="font-size:20px">${sz && im && im.move ? (Math.abs(sz.pred) / im.move).toFixed(2) + '×' : '—'}</div><div class="sub">below 1: options price a bigger move than usual</div></div>
      <div><div class="k">Own median |day-0 move|</div><div class="v" style="font-size:20px">${own.absMed == null ? '—' : '±' + (own.absMed * 100).toFixed(1) + '%'}</div><div class="sub">${own.n || 0} past releases, vs ${esc(p.bench)}</div></div></div>
    <div class="note" style="margin:-4px 0 12px">The size of the move is what the data predicts well; the direction before the release is close to a coin flip. The straddle includes the ordinary days up to its expiry.</div>`;
  return `<section class="panel"><div class="hd"><h2>Next release: projection</h2><span class="dim" style="font-size:13px">${when}</span><div class="grow"></div>
      <div class="seg"><button data-enpm="ab" aria-pressed="${m === 'ab'}">vs ${esc(p.bench)}</button><button data-enpm="raw" aria-pressed="${m === 'raw'}">Raw</button></div></div>
    <div class="bd">${sizeHtml}<div class="kv" style="grid-template-columns:repeat(5,minmax(0,1fr))">${Object.entries(SIG).map(([k, [l, f]]) => `<div><div class="k">${l}</div><div class="v ${['pre1ab', 'pre5ab', 'pre21ab', 'pre63ab'].includes(k) ? cls(n[k]) : ''}">${n[k] == null ? '—' : f(n[k])}</div></div>`).join('')}</div>
      ${n.rangeFlow ? `<div class="note" style="margin-top:8px">Volume pattern: <b>${esc(n.rangeVol || '')}</b> · <b>${esc(n.rangeFlow)}</b> — see the “52-week range × 6-month …” groups in the Event study for how each pattern did after releases.</div>` : ''}
      ${ptv && ptv.targetConsensus ? `<div class="note" style="margin-top:8px">Analysts' consensus price target $${ptv.targetConsensus.toFixed(2)} (low $${(ptv.targetLow || 0).toFixed(0)} – high $${(ptv.targetHigh || 0).toFixed(0)}): <b class="${cls(ptv.upside)}">${pct(ptv.upside)}</b> from the last close. Today's snapshot only: not in the backtest.</div>` : ''}
      ${n.lastCall ? `<div class="note">Last call ${esc(n.lastCall.date)}: tone ${n.lastCall.tone == null ? '—' : n.lastCall.tone.toFixed(2)}, Q&A ${n.lastCall.toneQA == null ? '—' : n.lastCall.toneQA.toFixed(2)}, guidance ${esc(n.lastCall.guideDir || '—')} · <a href="#earnings" data-entx="${esc(n.lastCall.date)}">read it</a></div>` : ''}
      <div class="note" style="margin-top:8px">Going into the release: the last reported quarter, the consensus (FMP), analysts' actions, the last call, and what the stock did up to the ${esc(n.asOf || '—')} close.</div></div>
    ${modelHtml}
    <div class="tbl-wrap"><table class="tbl" style="min-width:1100px"><thead><tr><th>Horizon</th><th class="r" colspan="4">Similar releases (nearest ${p.neighbours.length})</th><th class="r" colspan="4">${esc(p.sym)}'s own ${p.ownN} releases</th><th class="r" colspan="4">All ${p.refN} releases</th><th class="r">±1σ from volatility</th></tr>
      <tr>${'<th></th>' + ['', '', ''].map(() => '<th class="r">Median</th><th class="r">25–75%</th><th class="r">Up</th><th class="r">N</th>').join('') + '<th></th>'}</tr></thead><tbody>
      ${p.horizons.map(h => `<tr><td><b>${esc(h.label)}</b></td>${col(h.similar[m])}${col(h.own[m])}${col(h.all[m])}<td class="r">${h.volRange == null ? '—' : '±' + (h.volRange * 100).toFixed(1) + '%'}</td></tr>`).join('')}</tbody></table></div>
    <details class="bd" style="border-top:1px solid var(--row)"><summary class="dim" style="cursor:pointer">The ${p.neighbours.length} most similar past releases (matched on: ${p.signals.map(k => (SIG[k] || [k])[0]).join(', ') || 'none'})</summary>
      <div class="tbl-wrap" style="margin-top:8px"><table class="tbl"><thead><tr><th>Release</th><th>Ticker</th><th class="r">Distance</th><th class="r">Rev YoY (prior q)</th><th class="r">Consensus EPS g.</th><th class="r">1 month before</th><th class="r">Volume 5d</th><th class="r">CMF</th><th class="r">1d</th><th class="r">5d</th><th class="r">1m</th><th class="r">2m</th></tr></thead><tbody>
      ${p.neighbours.map(r => `<tr><td class="mono">${esc(r.date)}</td><td>${esc(r.sym)}</td><td class="r">${r.dist.toFixed(2)}</td><td class="r">${pct(r.prevRevYoY)}</td><td class="r">${pct(r.estEpsG)}</td><td class="r ${cls(r.pre21ab)}">${pct(r.pre21ab)}</td><td class="r">${x(r.relVol5)}</td><td class="r">${r.cmf20 == null ? '—' : r.cmf20.toFixed(2)}</td>
        <td class="r ${cls(r.r1ab)}">${pct(r.r1ab)}</td><td class="r ${cls(r.r5ab)}">${pct(r.r5ab)}</td><td class="r ${cls(r.r20ab)}">${pct(r.r20ab)}</td><td class="r ${cls(r.r40ab)}">${pct(r.r40ab)}</td></tr>`).join('')}</tbody></table></div>
      <p class="note">Nearest neighbours on standardised pre-release signals, drawn from the last event study${p.studyAt ? ' (' + esc(p.studyAt.slice(0, 10)) + ')' : ' (none run yet: own releases only)'} plus this company's own releases.</p></details></section>`;
}

/* ---------- Today: releases reacting at the open ---------- */
function enTodayShell() {
  const E = S.en, d = E.day || new Date(Date.now() - 8 * 3600e3).toISOString().slice(0, 10);
  E.day = d;
  return `<section class="panel"><div class="bd" style="display:flex;gap:10px;flex-wrap:wrap;align-items:end">
      <label class="f">Reaction day<input type="date" id="en-day" value="${esc(d)}"></label><button class="btn primary" data-en="today-build">Build the list</button>
      <span class="note">Releases of the study universe that react at this day's open: after-close releases of the business day before and before-open releases of the day. Before 09:30 the gap uses the latest pre-market trade; rebuild after the open for the real one.</span></div></section>
    <div id="en-today" style="margin-top:12px"><div class="panel"><div class="empty">Loading…</div></div></div>`;
}
async function enTodayLoad() {
  const el = $('#en-today'); if (!el) return;
  try { const r = await api('/fund/today?day=' + S.en.day); S.enToday = r; el.innerHTML = enTodayHtml(r); }
  catch (e) { el.innerHTML = `<div class="panel"><div class="empty loss">${esc(e.message)}</div></div>`; }
}
function enRange(x) {
  if (x.rangePos == null) return '—';
  const p = Math.max(0, Math.min(1, x.rangePos)) * 100;
  return `<span title="${pct(x.offLow)} above the 52w low · ${pct(x.offHigh)} from the 52w high" style="display:inline-flex;align-items:center;gap:6px"><span style="position:relative;display:inline-block;width:70px;height:6px;background:var(--line2);border-radius:3px"><i style="position:absolute;left:calc(${p.toFixed(0)}% - 4px);top:-2px;width:8px;height:10px;border-radius:2px;background:${p < 20 ? 'var(--sellT)' : p > 80 ? 'var(--buyT)' : 'var(--fg)'}"></i></span><span class="dim">${p.toFixed(0)}%</span></span>`;
}
function enTodayHtml(r) {
  if (!r.rows) return `<div class="panel"><div class="empty">No list for ${esc(r.day)} yet: <b>Build the list</b>.</div></div>`;
  const VER = { edge: ['ok', 'tested edge'], weak: ['', 'weak'], none: ['loss', 'no tested edge'] };
  const mh = Object.entries(r.models || {}).map(([k, t]) => `${k === 'o2c' ? 'Open → close' : 'Open → 5 days'}: <span class="pill ${(VER[t.verdict] || [''])[0]}">${(VER[t.verdict] || ['', '—'])[1]}</span> (rank corr. ${t.icQ && t.icQ.mean != null ? t.icQ.mean.toFixed(3) : '—'}, top − bottom fifth ${t.spread ? pct(t.spread.mean, 2) : '—'})`).join(' · ');
  const band = b => b === 'top' ? '<span class="pill ok">top fifth</span>' : b === 'bottom' ? '<span class="pill warn">bottom fifth</span>' : b ? '<span class="dim">middle</span>' : '—';
  const flag = f => f ? `<span class="pill ${f.startsWith('Bad') ? 'warn' : 'ok'}"><b>${esc(f)}</b></span>` : '';
  return `<section class="panel"><div class="hd"><h2>${esc(r.day)} · ${r.rows.length} release(s)</h2><span class="dim" style="font-size:13px">built ${esc((r.at || '').replace('T', ' ').slice(0, 16))} · vs ${esc(r.bench)}</span></div>
    ${r.market ? `<div class="bd" style="padding-bottom:0"><div class="kv" style="grid-template-columns:repeat(5,minmax(0,1fr))"><div><div class="k">Market (${esc(r.bench)}) at the ${esc(r.market.asOf)} close</div><div class="v">${esc(r.market.regime || '—')}</div></div>
      <div><div class="k">1 month</div><div class="v ${cls(r.market.spy21)}">${pct(r.market.spy21)}</div></div><div><div class="k">3 months</div><div class="v ${cls(r.market.spy63)}">${pct(r.market.spy63)}</div></div>
      <div><div class="k">vs 200-day average</div><div class="v ${cls(r.market.spyVs200)}">${pct(r.market.spyVs200)}</div></div><div><div class="k">Daily volatility (20d)</div><div class="v">${pct(r.market.spyVol20, 2)}</div></div></div>
      <div class="note">History: after a 3-month market drop, top-fifth releases drifted +2.2% over 2 months; after a rising market, bottom-fifth releases lagged most (−1.0 to −1.4%). In a sharp selloff during the 2 months, the top fifth fell harder than the market.</div></div>` : ''}
    <div class="bd" style="padding-bottom:0"><div class="note">History: “bad news + gap up” (bottom-fifth score, gap above +2%) averaged −0.72% from the open to the close, down 58% of the time; “good news + gap down” (top fifth, gap below −2%) +0.39%. After the call model — ${mh || 'run the event study to train it'}.</div></div>
    <div class="tbl-wrap"><table class="tbl" style="min-width:1450px"><thead><tr><th>Ticker</th><th>Release</th><th>Flag</th><th>Score</th><th class="r">EPS surp.</th><th class="r">Rev surp.</th><th class="r">Rev YoY</th><th class="r">YoY change</th>
      <th class="r">Call tone</th><th class="r">Tone change</th><th>Guidance</th><th class="r">Gap</th><th class="r">Open → now vs bench</th><th class="r">Model: open → close</th><th class="r">Model: 5 days</th><th class="r">1m before</th><th>52-week range</th><th class="r">Volume 6m</th><th class="r">Up-day vol. 6m</th><th class="r">Beta</th></tr></thead><tbody>
    ${r.rows.map(x => `<tr><td><a class="name" href="#earnings" data-enco="${esc(x.sym)}">${esc(x.sym)}</a></td><td class="mono" style="white-space:nowrap">${esc(x.date)} ${esc(x.timing)}</td><td>${flag(x.flag)}</td><td>${band(x.scoreBand)}</td>
      <td class="r ${cls(x.epsSurprise)}">${x.epsSurprise == null ? '—' : sgn(x.epsSurprise, 1, '%')}</td><td class="r ${cls(x.revSurprise)}">${x.revSurprise == null ? '—' : sgn(x.revSurprise, 1, '%')}</td>
      <td class="r ${cls(x.revYoY)}">${pct(x.revYoY)}</td><td class="r ${cls(x.revAccel)}">${pp(x.revAccel)}</td>
      <td class="r ${cls(x.txTone)}">${x.txTone == null ? '—' : x.txTone.toFixed(2)}${x.timing !== 'amc' && x.txTone != null ? ' <span class="dim" title="morning call: may end after the open">*</span>' : ''}</td><td class="r ${cls(x.txToneChg)}">${x.txToneChg == null ? '—' : (x.txToneChg > 0 ? '+' : '') + x.txToneChg.toFixed(2)}</td>
      <td>${x.txGuideDir ? esc(x.txGuideDir) : '—'}</td><td class="r ${cls(x.gap)}"><b>${pct(x.gap)}</b>${x.indicated ? ' <span class="dim" title="pre-market">pm</span>' : ''}</td><td class="r ${cls(x.o2cSoFar)}">${pct(x.o2cSoFar, 2)}</td>
      <td class="r ${cls(x.pred_o2c)}">${pct(x.pred_o2c, 2)}</td><td class="r ${cls(x.pred_after5)}">${pct(x.pred_after5, 2)}</td><td class="r ${cls(x.pre21ab)}">${pct(x.pre21ab)}</td><td>${enRange(x)}</td><td class="r ${x.volTrend126 > 1.15 ? 'gain' : x.volTrend126 < 0.87 ? 'dim' : ''}" title="2 months: ${x.volTrend42 == null ? '—' : x.volTrend42.toFixed(2) + '×'}">${x.volTrend126 == null ? '—' : x.volTrend126.toFixed(2) + '×'}</td>
      <td class="r ${x.udVol126 > 0.52 ? 'gain' : x.udVol126 < 0.48 ? 'loss' : ''}">${x.udVol126 == null ? '—' : (x.udVol126 * 100).toFixed(0) + '%'}</td><td class="r">${x.beta == null ? '—' : x.beta.toFixed(2)}</td></tr>`).join('') || '<tr><td colspan="20" class="dim">No release of the study universe reacts on this day.</td></tr>'}</tbody></table></div>
    ${(r.notes || []).length ? `<div class="note" style="padding:8px 16px">${r.notes.map(esc).join(' · ')}</div>` : ''}
    <div class="note" style="padding:8px 16px 14px">Score = the post-release score (EPS and revenue surprise, call tone, tone change, revenue acceleration) ranked against the last year's releases; growth from FMP's reported actuals. * a morning call may still be running at the open: the model ignores those call scores.</div></section>`;
}

/* ---------- Strategy backtest ---------- */
S.en.bt = { stake: 1000, costBps: 10, borrowPct: 1, from: '', to: '',
  legs: { A: { on: true, cut: 0.2 }, B: { on: true, band: 0.2, gap: 0.02 },
    C: { on: true, band: 0.2, hold: 40, hedge: 'spy', weak: true, heavy: false, far: false }, D: { on: true, band: 0.2, hold: 40, hedge: 'spy', strong: true } } };
S.en.optOn = 'alpha';
function enBtShell() {
  const c = S.en.bt, L = c.legs;
  const ck = (leg, k) => `<input type="checkbox" data-enleg="${leg}.${k}" ${L[leg][k] ? 'checked' : ''}>`;
  const sel = (leg, k, opts) => `<select data-enleg="${leg}.${k}">${opts.map(([v, l]) => `<option value="${v}" ${String(L[leg][k]) === String(v) ? 'selected' : ''}>${l}</option>`).join('')}</select>`;
  const band = (leg, lab) => `<label class="f">${lab}${sel(leg, leg === 'A' ? 'cut' : 'band', [[0.1, '10%'], [0.2, '20%'], [0.3, '30%']])}</label>`;
  const hold = leg => `<label class="f">Hold${sel(leg, 'hold', [[5, '5 sessions'], [20, '20 sessions (~1 month)'], [40, '40 sessions (~2 months)']])}</label>`;
  const hedge = leg => `<label class="f">Hedge${sel(leg, 'hedge', [['none', 'None'], ['spy', 'SPY 1:1'], ['beta', 'Beta × SPY']])}</label>`;
  const box = (leg, title, desc, body) => `<div style="border:1px solid var(--line);border-radius:8px;padding:10px 12px"><label class="check"><b>${ck(leg, 'on')} ${leg} · ${title}</b></label>
    <div class="note" style="margin:4px 0 8px 24px">${desc}</div><div style="margin-left:24px;display:flex;gap:10px;flex-wrap:wrap;align-items:end">${body}</div></div>`;
  return `<section class="panel"><div class="hd"><h2>Earnings strategy</h2><span class="dim" style="font-size:13px">the event study's rules, traded with a fixed stake per trade over the last study's releases</span></div>
    <div class="bd"><div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      ${box('A', 'After the call: buy at the open', 'The after-the-call model (out-of-sample) in the top part of the previous year’s predictions, and not “bad news + gap up”. Buy at the day-0 open, sell at the close.', band('A', 'Model top'))}
      ${box('B', 'Fade “bad news + gap up”', 'Post-release score in the bottom part and a gap up. Short at the open, cover at the close.', band('B', 'Score bottom') + `<label class="f">Gap above${sel('B', 'gap', [[0.01, '+1%'], [0.02, '+2%'], [0.04, '+4%']])}</label>`)}
      ${box('C', 'Post-release long', 'Score in the top part. Buy at the day-0 close.', band('C', 'Score top') + hold('C') + hedge('C') + `<div style="display:flex;flex-direction:column;gap:2px;flex-basis:100%">
        <label class="check">${ck('C', 'weak')} only when the market is weak going in (below its 200-day average or down over 3 months)</label>
        <label class="check">${ck('C', 'heavy')} only when day 0 was up >2% on heavy volume (≥2.5× normal)</label>
        <label class="check">${ck('C', 'far')} only when the stock is >25% below its 52-week high</label></div>`)}
      ${box('D', 'Post-release short', 'Score in the bottom part. Short at the day-0 close.', band('D', 'Score bottom') + hold('D') + hedge('D') + `<label class="check" style="flex-basis:100%">${ck('D', 'strong')} only when the market is strong going in (above its 200-day average and up over 3 months)</label>`)}
    </div>
    <div class="form" style="margin-top:12px;align-items:end">
      <label class="f">$ per trade<input type="number" min="10" step="100" data-enbtv="stake" value="${c.stake}"></label>
      <label class="f">Costs, round trip (bps)<input type="number" min="0" step="1" data-enbtv="costBps" value="${c.costBps}"></label>
      <label class="f">Short borrow (%/year)<input type="number" min="0" step="0.5" data-enbtv="borrowPct" value="${c.borrowPct}"></label>
      <label class="f">From<input type="date" data-enbtv="from" value="${esc(c.from)}"></label><label class="f">To<input type="date" data-enbtv="to" value="${esc(c.to)}"></label>
      <button class="btn primary" data-en="bt-run">Run backtest</button></div>
    <p class="note">No look-ahead: the score uses signals ranked against earlier releases, the after-the-call prediction is the walk-forward one, and the top / bottom parts are judged against the previous 365 days. A and B are day trades, unhedged; for C and D a hedge is a second trade, and its cost is counted too. “Beyond the market” = the same trades measured against SPY: what the earnings rules added, not the market's own move.</p></div></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Optimizer</h2><span class="dim" style="font-size:13px">like Research: every setting of each leg, trades split at a date (first 60% of the period = train, the rest = test), ranked on train only</span><div class="grow"></div>
      <div class="seg"><button data-enopton="alpha" aria-pressed="${S.en.optOn === 'alpha'}">Rank on profit beyond the market</button><button data-enopton="pnl" aria-pressed="${S.en.optOn === 'pnl'}">Rank on profit as traded</button></div>
      <button class="btn primary" data-en="opt-run" style="margin-left:8px">Run optimizer</button></div><div id="en-opt"><div class="bd dim">Loading…</div></div></section>
    <div id="en-bt-res" style="margin-top:12px"></div>`;
}
const BT_LEG = { A: 'A · after the call, buy at the open', B: 'B · fade bad news + gap up', C: 'C · post-release long', D: 'D · post-release short' };
function enOptSetting(leg, p) {
  const pc = v => (v * 100).toFixed(0) + '%';
  if (leg === 'A') return `model top ${pc(p.cut)}`;
  if (leg === 'B') return `score bottom ${pc(p.band)}, gap > +${pc(p.gap)}`;
  const f = leg === 'C' ? [p.weak && 'weak market', p.heavy && 'heavy-volume up day', p.far && '>25% below high'].filter(Boolean) : [p.strong && 'strong market'].filter(Boolean);
  return `score ${leg === 'C' ? 'top' : 'bottom'} ${pc(p.band)} · hold ${p.hold} · hedge ${p.hedge}${f.length ? ' · ' + f.join(', ') : ''}`;
}
async function enOptLoad() {
  const el = $('#en-opt'); if (!el) return;
  try { const r = await api('/fund/backtest/opt?on=' + S.en.optOn); S.enOpt = r; el.innerHTML = enOptHtml(r); if (r.job && r.job.status === 'running' && r.job.job === 'fundopt') setTimeout(enOptLoad, 3000); }
  catch (e) { el.innerHTML = `<div class="bd loss">${esc(e.message)}</div>`; }
}
function enOptHtml(r) {
  const o = r.opt;
  if (r.job && r.job.status === 'running' && r.job.job === 'fundopt') return `<div class="bd">${esc(r.job.msg || 'Optimizing…')}</div>`;
  if (!o || !o.legs) return '<div class="bd dim">Not run yet. It tests every setting of each leg (about 280) in under a minute.</div>';
  const money = v => v == null ? '—' : (v < 0 ? '−$' : '$') + Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 0 });
  const cell = (m, x) => `<td class="r">${m.n || 0}</td><td class="r ${cls(m.total)}">${money(m.total)}</td><td class="r ${cls(x && x.total)}">${money(x && x.total)}</td><td class="r">${tstat(x && x.t)}</td>`;
  return `<div class="bd" style="padding-bottom:0"><div class="note">Split: train before ${esc(o.split || '—')}, test after. Ranked on the train ${o.on === 'alpha' ? 'profit beyond the market' : 'profit as traded'} (t of the P/L per trade, at least ${o.minTrades} trades). A setting that also holds on the test period is the one to use. ${esc((o.at || '').replace('T', ' ').slice(0, 16))}</div>
      <button class="btn" data-en="opt-best" style="margin-top:8px">Use the best setting of each leg</button></div>
    ${'ABCD'.split('').map(leg => `<div class="tbl-wrap" style="margin-top:10px"><table class="tbl"><thead><tr><th>${BT_LEG[leg]}</th><th class="r">Train trades</th><th class="r">Train P/L</th><th class="r">Beyond mkt</th><th class="r">t</th><th class="r">Test trades</th><th class="r">Test P/L</th><th class="r">Beyond mkt</th><th class="r">t</th><th></th></tr></thead><tbody>
      ${(o.legs[leg] || []).slice(0, 6).map((x, i) => `<tr><td>${esc(enOptSetting(leg, x.p))}</td>${cell(x.train, x.trainX)}${cell(x.test, x.testX)}<td><button class="btn sm" data-enoptuse="${leg}.${i}">Use</button></td></tr>`).join('')}</tbody></table></div>`).join('')}
    <div class="note" style="padding:8px 16px 14px">Beyond mkt = the same trades measured against SPY. In a rising market an unhedged long wins on the market alone: compare the two columns, and trust the test period.</div>`;
}
function enBtEquity(eq) {
  if (!eq || eq.length < 2) return '';
  const W = 1000, H = 220, P = 78, ys = eq.map(e => e[1]), lo = Math.min(0, ...ys), hi = Math.max(0, ...ys), t0 = Date.parse(eq[0][0]), t1 = Date.parse(eq[eq.length - 1][0]);
  const X = d => P + (Date.parse(d) - t0) / (t1 - t0 || 1) * (W - P - 10), Y = v => 10 + (hi - v) / (hi - lo || 1) * (H - 30);
  const pts = eq.map(e => `${X(e[0]).toFixed(1)},${Y(e[1]).toFixed(1)}`).join(' ');
  const yrs = []; for (let y = +eq[0][0].slice(0, 4) + 1; y <= +eq[eq.length - 1][0].slice(0, 4); y++) yrs.push(y);
  const money = v => (v < 0 ? '−$' : '$') + Math.abs(Math.round(v)).toLocaleString('en-US');
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">
    <line x1="${P}" x2="${W - 10}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--line2)"/>
    ${yrs.map(y => `<line x1="${X(y + '-01-01')}" x2="${X(y + '-01-01')}" y1="10" y2="${H - 20}" stroke="var(--line)" /><text x="${X(y + '-01-01')}" y="${H - 5}" font-size="11" fill="var(--muted)" text-anchor="middle">${y}</text>`).join('')}
    <text x="${P - 4}" y="${Y(hi) + 4}" font-size="11" fill="var(--muted)" text-anchor="end">${money(hi)}</text><text x="${P - 4}" y="${Y(lo) + 4}" font-size="11" fill="var(--muted)" text-anchor="end">${money(lo)}</text>
    <polyline points="${pts}" fill="none" stroke="#4A9EFF" stroke-width="1.6"/></svg>`;
}
function enBtHtml(r) {
  if (!r.n) return '<div class="panel"><div class="empty">No trades with these rules.</div></div>';
  const money = v => v == null ? '—' : (v < 0 ? '−$' : '$') + Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 0 });
  const money2 = v => v == null ? '—' : (v < 0 ? '−$' : '$') + Math.abs(v).toFixed(2);
  const row = (lab, x) => `<tr><td>${lab}</td><td class="r">${x.n.toLocaleString('en-US')}</td><td class="r ${cls(x.total)}"><b>${money(x.total)}</b></td><td class="r ${cls(x.avg)}">${money2(x.avg)}</td><td class="r">${(x.win * 100).toFixed(1)}%</td>
    <td class="r">${x.profitFactor ?? '—'}</td><td class="r gain">${money2(x.avgWin)}</td><td class="r loss">${money2(x.avgLoss)}</td><td class="r">${tstat(x.t)}</td></tr>`;
  const head = `<thead><tr><th></th><th class="r">Trades</th><th class="r">P/L</th><th class="r">Avg / trade</th><th class="r">Win</th><th class="r">Profit factor</th><th class="r">Avg win</th><th class="r">Avg loss</th><th class="r">t</th></tr></thead>`;
  const k = (l, v, sub = '', c = '') => `<div><div class="k">${l}</div><div class="v ${c}" style="font-size:20px">${v}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
  return `<section class="panel"><div class="bd"><div class="kv" style="grid-template-columns:repeat(6,minmax(0,1fr))">
      ${k('Total P/L', money(r.total), `${esc(r.from)} → ${esc(r.to)}`, cls(r.total))}${k('Per year', money(r.perYear), '', cls(r.perYear))}${k('Trades', r.n.toLocaleString('en-US'), `${money(r.cfg.stake)} each · avg ${money2(r.avg)}`)}
      ${k('Win rate', (r.win * 100).toFixed(1) + '%', `profit factor ${r.profitFactor ?? '—'}`)}${k('Max drawdown', money(-r.maxDrawdown), esc(r.maxDrawdownAt || ''), 'loss')}
      ${k('Capital in use', money(r.avgCapital) + ' avg', `peak ${money(r.capitalNeeded)} (${r.maxOpen} positions) · ${r.returnOnAvgCapitalPerYear == null ? '—' : (r.returnOnAvgCapitalPerYear * 100).toFixed(1) + '%/yr on the average'}`)}</div></div>
    ${r.split ? `<div class="bd" style="padding-top:0"><table class="tbl"><thead><tr><th></th><th class="r">Trades</th><th class="r">P/L</th><th class="r">Avg / trade</th><th class="r">Win</th><th class="r">Beyond the market</th><th class="r">t</th></tr></thead><tbody>
      ${[['Train (before ' + r.split.date + ')', r.split.train, r.split.trainX], ['Test (from ' + r.split.date + ')', r.split.test, r.split.testX], ['All', r, r.alpha]].map(([l, m, x]) => `<tr><td>${esc(l)}</td><td class="r">${(m.n || 0).toLocaleString('en-US')}</td><td class="r ${cls(m.total)}"><b>${money(m.total)}</b></td><td class="r">${money2(m.avg)}</td><td class="r">${m.win == null ? '—' : (m.win * 100).toFixed(1) + '%'}</td><td class="r ${cls(x && x.total)}">${money(x && x.total)}</td><td class="r">${tstat(x && x.t)}</td></tr>`).join('')}</tbody></table></div>` : ''}
    <div style="padding:0 16px 8px">${enBtEquity(r.equity)}</div><div class="note" style="padding:0 16px 12px">Cumulative P/L by exit date, after costs.</div></section>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px">
      <section class="panel"><div class="hd"><h2>By leg</h2></div><div class="tbl-wrap"><table class="tbl">${head}<tbody>${r.legs.map(x => row(BT_LEG[x.leg] || x.leg, x)).join('')}</tbody></table></div></section>
      <section class="panel"><div class="hd"><h2>By year (exit)</h2></div><div class="tbl-wrap"><table class="tbl">${head}<tbody>${r.years.map(x => row(x.year, x)).join('')}</tbody></table></div></section></div>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Trades</h2><span class="dim" style="font-size:13px">the latest ${Math.min(300, r.trades.length)} of ${r.nTrades.toLocaleString('en-US')}</span><div class="grow"></div><button class="btn sm" data-en="bt-csv">Export trades (CSV)</button></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Leg</th><th>Ticker</th><th>Release</th><th>Entry</th><th>Exit</th><th>Side</th><th class="r">Return</th><th class="r">P/L</th><th>Why</th></tr></thead><tbody>
      ${r.trades.slice(-300).reverse().map(t => `<tr><td>${t.leg}</td><td><a class="name" href="#earnings" data-enco="${esc(t.sym)}">${esc(t.sym)}</a></td><td class="mono">${esc(t.release)}</td><td class="mono">${esc(t.entry)}</td><td class="mono">${esc(t.exit)}</td><td>${t.side}</td><td class="r ${cls(t.side === 'long' ? t.ret : -t.ret)}">${pct(t.ret, 2)}</td><td class="r ${cls(t.pnl)}"><b>${money2(t.pnl)}</b></td><td class="dim">${esc(t.why)}</td></tr>`).join('')}</tbody></table></div></section>`;
}
function enBtCsv() {
  const r = S.enBt; if (!r) return;
  const keys = ['leg', 'sym', 'release', 'entry', 'exit', 'side', 'ret', 'pnl', 'why'];
  const q = v => v == null ? '' : /[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v);
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([[keys.join(',')].concat(r.trades.map(t => keys.map(k => q(t[k])).join(','))).join('\n')], { type: 'text/csv' }));
  a.download = 'earnings_strategy_trades.csv'; a.click(); URL.revokeObjectURL(a.href);
}

/* ---------- Call transcript viewer ---------- */
async function enTxShow(date) {
  const el = $('#en-tx-view'); if (!el) return;
  el.innerHTML = '<section class="panel" style="margin-bottom:12px"><div class="empty">Loading the call…</div></section>';
  try {
    const t = await api(`/fund/transcript?sym=${encodeURIComponent(S.en.sym)}&date=${date}`), c = t.scores || {};
    const num = v => v == null ? '—' : v.toFixed(2);
    const hl = esc(t.content || '').replace(/\n/g, '<br>').replace(/\b(rais(?:e|ed|ing)|lower(?:ed|ing)?|reaffirm(?:ed|ing)?|guidance|outlook)\b/gi, '<mark>$1</mark>');
    el.innerHTML = `<section class="panel" style="margin-bottom:12px"><div class="hd"><h2>${esc(t.sym)} earnings call · ${esc(t.date)} (Q${t.quarter} ${t.year})</h2><div class="grow"></div><button class="btn sm" data-en="tx-close">Close</button></div>
      <div class="bd"><div class="kv" style="grid-template-columns:repeat(6,minmax(0,1fr))"><div><div class="k">Tone</div><div class="v ${cls(c.tone)}">${num(c.tone)}</div></div><div><div class="k">Prepared remarks</div><div class="v ${cls(c.tonePrep)}">${num(c.tonePrep)}</div></div>
        <div><div class="k">Q&A</div><div class="v ${cls(c.toneQA)}">${num(c.toneQA)}</div></div><div><div class="k">Uncertainty / 1,000 words</div><div class="v">${num(c.uncert)}</div></div>
        <div><div class="k">Guidance</div><div class="v">${esc(c.guideDir || '—')} (${c.guideUp || 0}↑ ${c.guideDown || 0}↓ ${c.guideKeep || 0}=)</div></div><div><div class="k">Words</div><div class="v">${(c.words || 0).toLocaleString('en-US')}</div></div></div>
        ${(t.guideSents || []).length ? `<h3 style="font-size:13px;margin:14px 0 6px">Guidance sentences</h3>${t.guideSents.map(g => `<div style="margin:4px 0"><span class="pill ${g.dir === 'raise' ? 'ok' : g.dir === 'lower' ? 'warn' : ''}">${esc(g.dir)}</span> ${esc(g.text)}</div>`).join('')}` : ''}
        <details style="margin-top:12px"><summary class="dim" style="cursor:pointer">Full transcript</summary><div style="max-height:520px;overflow:auto;margin-top:8px;font-size:13px;line-height:1.55">${hl}</div></details>
        <p class="note">Tone = (positive − negative words) / (positive + negative), from −1 to +1; word-list scoring, negations flipped. Guidance = sentences about the outlook, classified by their verbs.</p></div></section>`;
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) { el.innerHTML = `<section class="panel" style="margin-bottom:12px"><div class="empty loss">${esc(e.message)}</div></section>`; }
}

/* ---------- Event study ---------- */
function enStudyShell() {
  const d = S.enD, ev = d.events, st = d.settings, fm = d.fmp || {};
  const uni = st.universe || [];
  return `<div class="row"><div class="col-main">
    <section class="panel"><div class="bd" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center"><b>Financial Modeling Prep</b>
      ${fm.connected ? `<span class="pill ok">Connected</span>${fm.plan ? ['earnings', 'grades', 'transcripts'].map(k => `<span class="pill ${fm.plan[k] ? 'ok' : 'warn'}">${fm.plan[k] ? '✓' : '✗'} ${{ earnings: 'consensus', grades: 'analyst grades', transcripts: 'call transcripts' }[k]}</span>`).join('') : ''}` : '<span class="pill warn">Not connected</span>'}
      <div class="grow"></div><a class="btn sm" href="#data">${fm.connected ? 'Manage on the Data page' : 'Connect on the Data page'}</a></div></section>
    <section class="panel"><div class="hd"><h2>Study universe</h2><div class="grow"></div><span class="pill ${uni.length ? 'ok' : ''}">${uni.length ? esc(st.uniName || 'Custom') + ' · ' + uni.length + ' tickers' : 'The screen\'s ' + st.tickers.length + ' tickers'}</span></div>
      <div class="bd"><div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><button class="btn primary" data-en="uni-sp500" ${fm.connected ? '' : 'disabled'}>Load the S&P 500 (FMP)</button>
        <label class="check"><input type="checkbox" id="en-removed" checked> + companies removed from the index in the period (less survivorship bias)</label>
        ${uni.length ? '<button class="btn" data-en="uni-clear">Use the screen\'s tickers instead</button>' : ''}</div>
      <details style="margin-top:10px"><summary class="dim" style="cursor:pointer">Or paste a ticker list</summary><textarea id="en-uni" rows="3" style="width:100%;margin-top:8px;font-family:var(--mono)" placeholder="AAPL MSFT NVDA …">${esc(uni.join(' '))}</textarea><button class="btn" style="margin-top:8px" data-en="uni-save">Save list</button></details>
      <p class="note">A 500-ticker, 10-year study reads ~500 SEC filings sets, ~500 FMP histories and ~500 price histories the first time (about 30–60 minutes, in several background runs); later runs reuse them. Removed companies without SEC or Alpaca data are listed as skipped.</p></div></section>
    <details class="panel" ${ev.n ? 'open' : ''} style="padding:0"><summary class="dim" style="cursor:pointer;padding:12px 16px">Optional: import your own release list (not needed: FMP, SEC and Alpaca supply everything)</summary><div class="bd"><p class="muted" style="margin-top:0">Import your own release list (e.g. your 142-release study) as CSV. <b>ticker</b> and <b>date</b> are required. Recognised: <span class="mono">timing</span> (bmo / amc), <span class="mono">eps_est</span>, <span class="mono">eps_actual</span>, <span class="mono">rev_est</span>, <span class="mono">rev_actual</span>, <span class="mono">eps_surprise</span> / <span class="mono">rev_surprise</span> (%), <span class="mono">guidance</span> (raise / maintain / lower / none). Every other column becomes a signal of its own (numbers → terciles, text → groups).</p>
      ${ev.n ? `<div class="note" style="margin-bottom:10px">Columns: ${ev.cols.map(c => `<span class="mono">${esc(c)}</span>`).join(', ') || '—'} · imported ${esc((ev.at || '').replace('T', ' ').slice(0, 16))}</div>` : ''}
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><input type="file" id="en-file" accept=".csv,.tsv,.txt,text/csv">
        <button class="btn primary" data-en="imp-replace">Import (replace)</button><button class="btn" data-en="imp-merge">Import (add)</button>${ev.n ? '<button class="btn danger" data-en="imp-clear">Clear</button>' : ''}</div>
      <details style="margin-top:10px"><summary class="dim" style="cursor:pointer">Or paste the CSV</summary><textarea id="en-csv" rows="6" style="width:100%;margin-top:8px;font-family:var(--mono)" placeholder="ticker,date,timing,eps_est,eps_actual,rev_surprise,guidance&#10;NVDA,2025-08-27,amc,1.01,1.05,1.2,raise"></textarea></details>
      </div></details>

    <section class="panel"><div class="hd"><h2>Run the study</h2></div><div class="bd"><div class="form" style="align-items:end">
      <label class="check"><input type="checkbox" id="en-sec" ${st.useSec ? 'checked' : ''}> Add every release of the study universe (${(uni.length || st.tickers.length)} tickers: SEC 8-K${fm.connected ? ' + FMP' : ''})</label>
      <label class="f">Years back<input type="number" id="en-years" min="1" max="11" value="${st.years}"></label>
      <label class="check"><input type="checkbox" id="en-tx" ${fm.connected && (!fm.plan || fm.plan.transcripts) ? 'checked' : ''} ${fm.connected ? '' : 'disabled'}> Download and score the call transcripts (kept for good)</label>
      <label class="f">Benchmark<select id="en-bench">${d.benches.map(b => `<option ${b === st.bench ? 'selected' : ''}>${b}</option>`).join('')}</select></label>
      <button class="btn primary" data-en="run" ${d.alpaca ? '' : 'disabled'}>Run event study</button></div>
      ${d.alpaca ? '' : '<p class="note loss">Connect Alpaca on the Data page: the reactions are measured on its daily bars (split-adjusted).</p>'}
      <p class="note">Base = the last close before the market could react (the release day's close after an after-close release, the previous close otherwise). Day 0 = the first session that could react. Returns vs the benchmark are simple differences; σ = day-0 abnormal move ÷ the 20 days' daily volatility before. Drift = day-0 close to day 20 / 60, vs the benchmark. Fundamentals are matched to the quarter that ended 5–120 days before the release (restated figures where the company restated).</p>
    </div></section></div>
    <aside class="col-side"><section class="panel"><div class="hd"><h2>Last study</h2></div><div class="bd" id="en-sum">${d.study ? `<div class="kv kv3"><div><div class="k">Releases</div><div class="v">${d.study.n}</div></div><div><div class="k">Tickers</div><div class="v">${d.study.tickers}</div></div><div><div class="k">Benchmark</div><div class="v">${esc(d.study.bench)}</div></div>
      <div><div class="k">From</div><div class="v">${esc(d.study.from || '—')}</div></div><div><div class="k">To</div><div class="v">${esc(d.study.to || '—')}</div></div><div><div class="k">Run</div><div class="v">${esc((d.study.at || '').slice(5, 16).replace('T', ' '))}</div></div>
      <div><div class="k">With call transcript</div><div class="v">${d.study.withTx ?? '—'}</div></div><div><div class="k">With analyst actions</div><div class="v">${d.study.withGrades ?? '—'}</div></div><div></div>
      <div><div class="k">With consensus</div><div class="v">${d.study.withEst ?? '—'}</div></div><div><div class="k">From FMP</div><div class="v">${(d.study.sources || {}).fmp ?? '—'}</div></div><div><div class="k">CSV / SEC only</div><div class="v">${(d.study.sources || {}).csv ?? 0} / ${(d.study.sources || {}).sec ?? 0}</div></div></div>
      ${d.study.withTx ? `<p class="note">Call sentiment: the “Call tone”, “Call tone, Q&A”, “Call uncertainty” and “Call guidance” groups and the Signal → reaction table below; the drift model's weights; Company tab → Call tone column (click to read the call).</p>`
        : `<p class="note loss">No call transcripts in this study${d.study.txStatus ? (d.study.txStatus.on ? (d.study.txStatus.planOk ? ` (${d.study.txStatus.failed} ticker(s) failed${(d.study.txStatus.notes || []).length ? ': ' + esc(d.study.txStatus.notes.join(' · ')) : ''})` : ': ' + esc((d.study.txStatus.notes || [])[0] || 'not in the FMP plan')) : ': the transcript box was off') : ' (this study ran before transcripts were added, or the box was off)'}.</p>`}` : '<div class="dim">Not run yet.</div>'}</div></section></aside></div>
    <div id="en-res" style="margin-top:12px"></div>`;
}
async function enStudyLoad() {
  if (!S.enD.study) return;
  const out = $('#en-res');
  out.innerHTML = '<div class="panel"><div class="empty">Loading the results…</div></div>';
  try { if (!S.enStudy || S.enStudy.at !== S.enD.study.at) S.enStudy = await api('/fund/study'); out.innerHTML = enResults(S.enStudy); }
  catch (e) { out.innerHTML = `<div class="panel"><div class="empty loss">${esc(e.message)}</div></div>`; }
}
function enBar(v, scale) {
  if (v == null) return '';
  const w = Math.min(60, Math.abs(v) / (scale || 1) * 60);
  return `<span style="display:inline-block;width:60px;height:8px;position:relative;vertical-align:middle;margin-left:6px"><i style="position:absolute;top:0;height:8px;${v >= 0 ? 'left:30px' : 'right:30px'};width:${w / 2}px;background:${v >= 0 ? 'var(--buyT)' : 'var(--sellT)'};border-radius:2px"></i><i style="position:absolute;left:30px;top:-2px;height:12px;width:1px;background:var(--line2)"></i></span>`;
}
function enResults(r) {
  const E = S.en, o = E.out, outs = r.outcomes || [];
  const isPct = o !== 'z1', f = v => isPct ? pct(v, 2) : (v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2) + 'σ');
  const all = r.all[o] || {};
  const scale = Math.max(1e-9, ...r.groups.flatMap(g => g.groups.map(x => Math.abs((x[o] || {}).mean || 0))));
  const rng = (g, x) => g.kind === 'num' && x.range ? `<div class="sub">${g.key.match(/YoY|Accel|QoQ|3y|accruals|runup|offHigh|vol20/) ? pct(x.range[0]) + ' … ' + pct(x.range[1]) : x.range[0].toFixed(2) + ' … ' + x.range[1].toFixed(2)}</div>` : '';
  const grp = g => `<section class="panel"><div class="hd"><h2 style="font-size:14px">${esc(g.label)}</h2></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Group</th><th class="r">N</th><th class="r">Mean</th><th class="r">Median</th><th class="r">Up</th><th class="r">t</th></tr></thead><tbody>
    ${g.groups.map(x => { const s = x[o] || {}; return `<tr><td>${esc(x.name)}${rng(g, x)}</td><td class="r">${s.n || 0}</td><td class="r ${cls(s.mean)}">${f(s.mean)}${enBar(s.mean, scale)}</td><td class="r ${cls(s.median)}">${f(s.median)}</td><td class="r">${s.hit == null ? '—' : (s.hit * 100).toFixed(0) + '%'}</td><td class="r">${tstat(s.t)}</td></tr>`; }).join('')}</tbody></table></div></section>`;
  const rhoCell = c => { if (!c || c.rho == null) return '<td class="r dim">—</td>'; const a = Math.min(1, Math.abs(c.rho) / 0.4); return `<td class="r" style="background:${c.rho > 0 ? `rgba(76,195,138,${(a * 0.35).toFixed(2)})` : `rgba(240,100,100,${(a * 0.35).toFixed(2)})`}" title="n = ${c.n}">${c.rho.toFixed(2)}</td>`; };
  const rows = enRowsSorted(r.rows || []);
  const RC = [['date', 'Release'], ['sym', 'Ticker'], ['timing', 'Timing'], ['setup', 'Beat / raise'], ['revYoY', 'Rev YoY'], ['revAccel', 'YoY change'], ['epsYoY', 'EPS YoY'], ['epsSurprise', 'EPS surp.'], ['revSurprise', 'Rev surp.'], ['pre5ab', '5d before'], ['pre21ab', '1m before'], ['relVol5', 'Vol 5d ×'], ['cmf20', 'CMF'], ['gradeNet90', 'Net upgr. 90d'], ['txTone', 'Call tone'],
    ['gap', 'Gap'], ['r1ab', 'Day 0 vs bench'], ['z1', 'σ'], ['r5ab', '5d'], ['r20ab', '20d'], ['r40ab', '2m'], ['drift20', 'Drift 20'], ['drift60', 'Drift 60']];
  const rc = (x, k) => { const v = x[k]; if (['date', 'sym', 'timing'].includes(k)) return `<td class="mono">${esc(v || '—')}</td>`; if (k === 'setup') return `<td>${esc(v || '—')}</td>`;
    if (k === 'epsSurprise' || k === 'revSurprise') return `<td class="r ${cls(v)}">${v == null ? '—' : sgn(v, 1, '%')}</td>`; if (k === 'z1' || k === 'cmf20') return `<td class="r">${v == null ? '—' : v.toFixed(k === 'z1' ? 1 : 2)}</td>`; if (k === 'gradeNet90') return `<td class="r ${cls(v)}">${v == null ? '—' : (v > 0 ? '+' : '') + v}</td>`; if (k === 'txTone') return `<td class="r ${cls(v)}">${v == null ? '—' : v.toFixed(2)}${x.guideSrc === 'call' && x.guide ? ' <span class="dim">' + esc(x.guide) + '</span>' : ''}</td>`; if (k === 'relVol5') return `<td class="r">${v == null ? '—' : v.toFixed(2)}</td>`; if (k === 'revAccel') return `<td class="r ${cls(v)}">${pp(v)}</td>`; return `<td class="r ${cls(v)}">${pct(v)}</td>`; };
  return enModelHtml(r) + `<section class="panel" style="margin-top:12px"><div class="bd" style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
      <div class="seg">${outs.map(([k, l]) => `<button data-enout="${k}" aria-pressed="${k === o}">${l}</button>`).join('')}</div><div class="grow"></div>
      <span>All releases: <b class="${cls(all.mean)}">${f(all.mean)}</b> mean · ${f(all.median)} median · ${all.hit == null ? '—' : (all.hit * 100).toFixed(0) + '% up'} · t ${tstat(all.t)} · N ${all.n || 0}</span></div>
      ${(r.skipped || []).length ? `<div class="note" style="padding:0 16px 12px">Skipped: ${r.skipped.map(esc).join(' · ')}</div>` : ''}</section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Signal → reaction</h2><span class="dim" style="font-size:13px">Spearman rank correlation of each pre-release signal with each outcome (|ρ| ≥ 0.2 shaded strongest)</span></div>
      <div class="tbl-wrap"><table class="tbl" style="min-width:900px"><thead><tr><th>Signal</th>${outs.map(([, l]) => `<th class="r">${l}</th>`).join('')}</tr></thead><tbody>
      ${r.corr.map(c => `<tr><td>${esc(c.label)}</td>${outs.map(([k]) => rhoCell(c[k])).join('')}</tr>`).join('')}</tbody></table></div></section>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px;margin-top:12px">${r.groups.map(grp).join('')}</div>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Releases</h2><span class="dim" style="font-size:13px">the latest ${rows.length} of ${r.n} · Export CSV has all of them</span><div class="grow"></div><button class="btn sm" data-en="csv">Export CSV</button></div>
      <div class="tbl-wrap"><table class="tbl" style="min-width:1400px"><thead><tr>${RC.map(([k, l]) => `<th class="${['date', 'sym', 'timing', 'setup'].includes(k) ? '' : 'r'}" style="cursor:pointer" data-enrsort="${k}">${l}${E.rsort === k ? (E.rdir < 0 ? ' ↓' : ' ↑') : ''}</th>`).join('')}</tr></thead>
      <tbody>${rows.slice(0, E.rshow).map(x => `<tr>${RC.map(([k]) => rc(x, k)).join('')}</tr>`).join('')}</tbody></table></div>
      ${rows.length > E.rshow ? `<div class="bd"><button class="btn sm" data-en="more">Show all ${rows.length}</button></div>` : ''}</section>`;
}
function enModelHtml(r) {
  const md = r.model;
  const bar = `<section class="panel" style="margin-bottom:12px"><div class="bd" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><b>Study of ${r.n} releases · ${r.tickers} tickers · ${esc(r.from || '')} → ${esc(r.to || '')}</b><div class="grow"></div>
    <span class="note">One file with everything: scorecards, groups, correlations and every release with all its signals. Attach it in the chat for review.</span><button class="btn primary" data-en="review">Export for review</button></div></section>`;
  if (!md) return bar;
  const VER = { edge: ['ok', 'Tested edge'], weak: ['', 'Weak'], none: ['loss', 'No tested edge'] };
  const T = md.targets || [], sel = T.find(t => t.key === S.en.mt) || T[0];
  const row = t => { const size = t.key === 'abs1', q = t.icQ || {}, sp = t.spread || {};
    return `<tr style="cursor:pointer;${sel && t.key === sel.key ? 'background:var(--row)' : ''}" data-enmt="${t.key}"><td>${esc(t.label)}<div class="sub">${t.kind === 'pre' ? 'before the release' : t.kind === 'score' ? 'fixed score, nothing fitted' : 'after day 0'}</div></td><td class="r">${t.n || 0}</td>
      <td><span class="pill ${(VER[t.verdict] || ['', '—'])[0]}">${(VER[t.verdict] || ['', 'Too few releases'])[1]}</span></td>
      <td class="r">${size ? '—' : t.hit == null ? '—' : (t.hit * 100).toFixed(1) + '%'}</td><td class="r dim">${size ? '—' : t.hitBase == null ? '—' : (t.hitBase * 100).toFixed(1) + '%'}</td>
      <td class="r">${q.mean == null ? '—' : q.mean.toFixed(3)}</td><td class="r">${tstat(q.t)}</td><td class="r">${q.pos == null ? '—' : (q.pos * 100).toFixed(0) + '%'}</td>
      <td class="r ${cls(sp.mean)}">${size ? '—' : pct(sp.mean, 2)}</td><td class="r">${size ? '—' : tstat(sp.t)}</td><td class="r ${cls(t.r2)}">${t.r2 == null ? '—' : (t.r2 * 100).toFixed(2) + '%'}</td>
      <td class="r">${size ? (t.icOwn == null ? '—' : t.icOwn.toFixed(3)) : '—'}</td></tr>`; };
  const det = !sel || !sel.deciles ? '' : `<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:0 16px 16px">
    <div><h3 style="font-size:13px;margin:8px 0">Calibration — ${esc(sel.label)}: deciles of the prediction (out-of-sample)</h3><table class="tbl"><thead><tr><th>Decile</th><th class="r">Predicted</th><th class="r">Actual</th><th class="r">N</th></tr></thead><tbody>
      ${sel.deciles.map((d, i) => `<tr><td>${i + 1}</td><td class="r">${pct(d.pred, 2)}</td><td class="r ${cls(d.actual)}"><b>${pct(d.actual, 2)}</b>${enBar(d.actual, Math.max(...sel.deciles.map(x => Math.abs(x.actual))))}</td><td class="r dim">${d.n}</td></tr>`).join('')}</tbody></table></div>
    <div><h3 style="font-size:13px;margin:8px 0">What drives it (final model, standardised weights)</h3><table class="tbl"><thead><tr><th>Signal</th><th class="r">Weight</th></tr></thead><tbody>
      ${(sel.coef || []).map(c => `<tr><td>${esc(c.label)}</td><td class="r ${cls(c.beta)}">${(c.beta * 100).toFixed(2)}${enBar(c.beta, Math.max(...sel.coef.map(x => Math.abs(x.beta))))}</td></tr>`).join('')}</tbody></table>
      <p class="note">Weight = change in the expected outcome (in % points) for a one-standard-deviation move in the signal, other signals equal.</p></div></div>`;
  return bar + `<section class="panel"><div class="hd"><h2>Forward-looking model: walk-forward test</h2><span class="dim" style="font-size:13px">ridge regression on ${md.scale === 'trailing percentiles' ? 'signals ranked against the previous year\'s releases' : 'capped signals'}, refitted each quarter on releases whose outcome was already known; the first ${md.minTrain} releases only train</span></div>
    <div class="tbl-wrap"><table class="tbl" style="min-width:1200px"><thead><tr><th>Target</th><th class="r">Tested on</th><th>Verdict</th><th class="r">Hit rate</th><th class="r">Always-up baseline</th><th class="r">Rank corr. / quarter</th><th class="r">t</th><th class="r">Quarters &gt; 0</th><th class="r">Top − bottom fifth</th><th class="r">t</th><th class="r">Out-of-sample R²</th><th class="r">Own history (size)</th></tr></thead>
    <tbody>${T.map(row).join('')}</tbody></table></div>${det}
    <div class="note" style="padding:0 16px 14px">Tested edge = rank correlation t ≥ 2 across quarters, positive out-of-sample R² and a hit rate above always guessing the training average's sign. Earnings returns are noisy: R² of a fraction of a percent with a steady positive rank correlation is already meaningful; a hit rate near 50% is normal. Click a row for its calibration and weights.</div></section>`;
}
function enRowsSorted(rows) {
  const E = S.en, k = E.rsort;
  return rows.slice().sort((a, b) => { const x = a[k], y = b[k]; if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1; return (typeof x === 'string' ? x.localeCompare(y) : x - y) * E.rdir; });
}
async function enDownload(kind, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  try {
    for (let i = 0; i < 120; i++) {
      const r = await api('/fund/study/export?kind=' + kind);
      if (r.url || r.data) {
        const a = document.createElement('a');
        if (r.url) a.href = r.url;
        else { const b = Uint8Array.from(atob(r.data), c => c.charCodeAt(0)); a.href = URL.createObjectURL(new Blob([b], { type: 'application/gzip' })); }
        a.download = r.name; document.body.appendChild(a); a.click(); a.remove();
        toast(kind === 'review' ? `Downloading ${r.name}: attach it in the chat.` : `Downloading ${r.name}.`);
        return;
      }
      btn.textContent = 'Preparing the file…';
      await new Promise(res => setTimeout(res, 3000));
    }
    toast('The export is taking long: try again in a minute.');
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = label; }
}
async function enImport(mode) {
  const file = $('#en-file').files[0];
  const text = file ? await file.text() : ($('#en-csv') ? $('#en-csv').value : '');
  if (!text.trim()) return toast('Choose a CSV file or paste the rows.');
  const r = await api('/fund/events', { method: 'POST', body: { csv: text, mode } });
  toast(`${r.n} releases, ${r.tickers} tickers imported.${r.problems.length ? ' ' + r.problems.length + ' row(s) skipped.' : ''}`);
  if (r.problems.length) alert('Rows not imported:\n' + r.problems.join('\n'));
  return vEarnings();
}

document.addEventListener('click', async e => {
  const el = e.target.closest('[data-entab],[data-en],[data-enco],[data-ensort],[data-enout],[data-enrsort],[data-enpm],[data-enmt],[data-entx],[data-enopton],[data-enoptuse]');
  if (!el || el.disabled) return;
  const E = S.en, ds = el.dataset;
  try {
    if (ds.entab) { E.tab = ds.entab; document.querySelectorAll('[data-entab]').forEach(b => b.setAttribute('aria-pressed', b === el)); return enBody(); }
    if (ds.enco) { e.preventDefault(); E.sym = ds.enco; E.tab = 'company'; return vEarnings(); }
    if (ds.ensort) { E.dir = E.sort === ds.ensort ? -E.dir : -1; E.sort = ds.ensort; return enBody(); }
    if (ds.enopton) { S.en.optOn = ds.enopton; document.querySelectorAll('[data-enopton]').forEach(b => b.setAttribute('aria-pressed', b === el)); return enOptLoad(); }
    if (ds.enoptuse) { const [leg, i] = ds.enoptuse.split('.'); const x = S.enOpt.opt.legs[leg][+i]; Object.assign(S.en.bt.legs[leg], x.p, { on: true }); enBody(); toast(`${leg}: ${enOptSetting(leg, x.p)}. Run the backtest.`); return; }
    if (ds.entx) { e.preventDefault(); return enTxShow(ds.entx); }
    if (ds.enmt) { S.en.mt = ds.enmt; $('#en-res').innerHTML = enResults(S.enStudy); return; }
    if (ds.enpm) { S.en.pmode = ds.enpm; $('#en-proj').innerHTML = enProjHtml(S.enProj); return; }
    if (ds.enout) { E.out = ds.enout; $('#en-res').innerHTML = enResults(S.enStudy); return; }
    if (ds.enrsort) { E.rdir = E.rsort === ds.enrsort ? -E.rdir : -1; E.rsort = ds.enrsort; $('#en-res').innerHTML = enResults(S.enStudy); return; }
    switch (ds.en) {
      case 'save-tickers': await api('/fund/settings', { method: 'PUT', body: { tickers: $('#en-tickers').value } }); toast('Saved.'); return vEarnings();
      case 'refresh': await api('/fund/settings', { method: 'PUT', body: { tickers: $('#en-tickers').value } }); await api('/fund/refresh', { method: 'POST', body: {} }); toast('Reading the SEC filings…'); return vEarnings();
      case 'co-load': { const s = $('#en-sym').value.trim().toUpperCase(); if (!s) return toast('Enter a ticker.'); E.sym = s; $('#en-co').innerHTML = '<div class="panel"><div class="empty">Reading the SEC filings…</div></div>'; document.querySelector('[data-en="co-update"]').disabled = false; return enCompanyLoad(); }
      case 'co-update': el.disabled = true; await api('/fund/refresh', { method: 'POST', body: { sym: E.sym } }); toast('Updating from SEC…'); return vEarnings();
      case 'imp-replace': return enImport('replace');
      case 'imp-merge': return enImport('merge');
      case 'imp-clear': if (!confirm('Remove the imported releases?')) return; await api('/fund/events', { method: 'DELETE' }); return vEarnings();
      case 'run': el.disabled = true; await api('/fund/study', { method: 'POST', body: { useSec: $('#en-sec').checked, years: +$('#en-years').value || 5, bench: $('#en-bench').value, transcripts: $('#en-tx').checked } }); toast('Event study started.'); S.enStudy = null; return vEarnings();
      case 'more': E.rshow = 1e9; $('#en-res').innerHTML = enResults(S.enStudy); return;
      case 'csv': return enDownload('csv', el);
      case 'review': return enDownload('review', el);
      case 'today-build': { S.en.day = $('#en-day').value; el.disabled = true; await api('/fund/today', { method: 'POST', body: { day: S.en.day } }); toast('Building the list…');
        const t0 = Date.now(); while (Date.now() - t0 < 240000) { await new Promise(r => setTimeout(r, 3000)); const r = await api('/fund/today?day=' + S.en.day); if (!r.job || r.job.status !== 'running') { S.enToday = r; $('#en-today').innerHTML = enTodayHtml(r); if (r.job && r.job.status === 'error') toast(r.job.msg); break; } }
        el.disabled = false; return; }
      case 'bt-run': { el.disabled = true; el.textContent = 'Running…'; $('#en-bt-res').innerHTML = '<div class="panel"><div class="empty">Running the backtest…</div></div>';
        try { S.enBt = await api('/fund/backtest', { method: 'POST', body: { cfg: S.en.bt } }); $('#en-bt-res').innerHTML = enBtHtml(S.enBt); }
        catch (err) { $('#en-bt-res').innerHTML = `<div class="panel"><div class="empty loss">${esc(err.message)}</div></div>`; }
        el.disabled = false; el.textContent = 'Run backtest'; return; }
      case 'bt-csv': return enBtCsv();
      case 'opt-run': el.disabled = true; await api('/fund/backtest/opt', { method: 'POST', body: { cfg: { stake: S.en.bt.stake, costBps: S.en.bt.costBps, borrowPct: S.en.bt.borrowPct } } }); toast('Optimizing…'); setTimeout(enOptLoad, 1500); return;
      case 'opt-best': { const L = S.enOpt.opt.legs; 'ABCD'.split('').forEach(k => { if (L[k] && L[k][0]) Object.assign(S.en.bt.legs[k], L[k][0].p, { on: true }); }); enBody(); toast('Best train setting of each leg applied: run the backtest.'); return; }
      case 'tx-close': $('#en-tx-view').innerHTML = ''; return;
      case 'uni-sp500': { el.disabled = true; toast('Loading the S&P 500 from FMP…'); const r = await api('/fund/universe', { method: 'POST', body: { source: 'sp500', removed: $('#en-removed').checked, years: +$('#en-years').value || 10 } }); toast(r.uniName + ' loaded.' + (r.note ? ' ' + r.note : '')); return vEarnings(); }
      case 'uni-clear': await api('/fund/universe', { method: 'POST', body: { tickers: [] } }); return vEarnings();
      case 'uni-save': await api('/fund/universe', { method: 'POST', body: { tickers: $('#en-uni').value } }); toast('Universe saved.'); return vEarnings();
    }
  } catch (err) { toast(err.message); el.disabled = false; }
});
document.addEventListener('input', e => {
  const k = e.target.dataset && e.target.dataset.enf;
  if (!k || S.page !== 'earnings') return;
  S.en.f[k] = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
  const pos = e.target.selectionStart, id = k;
  $('#en-body').innerHTML = enScreen();
  const n = document.querySelector(`[data-enf="${id}"]`); if (n && n.type !== 'checkbox') { n.focus(); try { n.setSelectionRange(pos, pos); } catch (x) { /* number inputs */ } }
});
document.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.id === 'en-sym') { e.preventDefault(); document.querySelector('[data-en="co-load"]').click(); } });
document.addEventListener('change', e => { if (e.target.id === 'en-day') { S.en.day = e.target.value; enTodayLoad(); } });

document.addEventListener('change', e => {
  const t = e.target, c = S.en.bt;
  if (t.dataset.enleg) { const [leg, k] = t.dataset.enleg.split('.'); c.legs[leg][k] = t.type === 'checkbox' ? t.checked : (['cut', 'band', 'gap', 'hold'].includes(k) ? +t.value : t.value); }
  else if (t.dataset.enbtv) { const k = t.dataset.enbtv; c[k] = ['stake', 'costBps', 'borrowPct'].includes(k) ? +t.value : t.value; }
});
