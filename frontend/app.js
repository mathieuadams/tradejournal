/* Trade Journal front end: plain JS, no build step. Talks to the HTTP API with the Cognito ID token. */
const C = window.TJ_CONFIG;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sum = a => a.reduce((s, x) => s + x, 0);
const avg = a => a.length ? sum(a) / a.length : 0;
const money = (v, signed = true) => { if (v == null) return '—'; const s = '$' + Math.abs(Math.round(v)).toLocaleString('en-US'); return (v < 0 ? '−' : (signed ? '+' : '')) + s; };
const fr = r => r == null ? '—' : (r >= 0 ? '+' : '−') + Math.abs(r).toFixed(2) + 'R';
const cls = v => v > 0 ? 'gain' : v < 0 ? 'loss' : '';
const fmtDate = d => new Date(d + 'T12:00:00Z').toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
const weekday = d => ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][new Date(d + 'T12:00:00Z').getUTCDay()];
const chron = (a, b) => a.openTs.localeCompare(b.openTs);
const cdate = t => t.closeTs ? t.closeTs.slice(0, 10) : t.date;  // realized P&L belongs to the day a trade closed
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const symFmt = s => { const m = /^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/.exec(s || ''); return m ? `${m[1]} ${MON[+m[3] - 1]} ${+m[4]} '${m[2]} ${+m[6] / 1000} ${m[5] === 'C' ? 'call' : 'put'}` : s; };
const fmtExp = d => d ? `${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+d.slice(5, 7) - 1]} ${+d.slice(8, 10)} '${d.slice(2, 4)}` : '';
const px = v => v == null ? '—' : (Math.abs(v) >= 1000 ? v.toFixed(2) : v.toFixed(Math.abs(v) < 10 ? 4 : 2));
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove('show'), 2200); }
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ---------- API ---------- */
async function api(path, { method = 'GET', body } = {}) {
  const token = await Auth.token();
  if (!token) { Auth.login(); return new Promise(() => {}); }
  let r;
  try {
    r = await fetch(C.apiUrl + path, { method, headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  } catch (e) { throw new Error('Can’t reach the server. Check your connection and try again.'); }
  if (r.status === 401) { Auth.login(); return new Promise(() => {}); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(data.error || `Request failed (${r.status}).`); e.status = r.status; throw e; }
  return data;
}

/* ---------- state ---------- */
const S = { me: null, trades: [], range: 'all', chat: [], tf: null, wi: { late: false, revenge: false, fixed2: false, worst: false }, bd: 'setup',
  report: undefined, imports: null, broker: undefined, schwab: undefined, jDay: null, daily: {}, bars: {}, reviews: {} };
const MISTAKE_LEAKS = [['Revenge', 'Revenge trades'], ['Moved stop', 'Moved stops'], ['Oversized', 'Oversized trades'], ['Chased', 'Chased entries']];
const RULES = {
  'Revenge': 'Two losing trades in a row ends your trading day. Close the platform and write the recap.',
  'Moved stop': 'Your stop goes in with the order and only ever moves toward profit.',
  'Oversized': 'Size comes from the plan: risk ÷ stop distance. No size changes after entry.',
  'Chased': 'If price is more than 0.25R past your planned entry, skip it and wait for the next setup.',
  'late': 'Last new entry at 11:00. Afternoons are for review, not trading.'
};
function scoped() {
  const all = S.acct ? S.trades.filter(t => t.acct === S.acct) : S.trades;
  if (S.range === 'all' || !all.length) return all;
  const last = all[all.length - 1].date; const days = S.range === '30' ? 30 : 7;
  const cut = new Date(new Date(last + 'T12:00:00Z').getTime() - days * 864e5).toISOString().slice(0, 10);
  return all.filter(t => t.date > cut);
}
const closed = ts => ts.filter(t => t.status === 'closed');
const byId = id => S.trades.find(t => t.id === id);
function replaceTrade(v) { const i = S.trades.findIndex(t => t.id === v.id); if (i >= 0) S.trades[i] = v; return v; }
function rangeCtl() {
  const o = [['all', 'All'], ['30', '30 days'], ['7', '7 days']];
  const accts = [...new Set(S.trades.map(t => t.acct))].sort();
  const acctSel = accts.length > 1 ? `<select id="g-acct" aria-label="Account" style="background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:6px 10px"><option value="">All accounts</option>${accts.map(a => `<option ${S.acct === a ? 'selected' : ''}>${esc(a)}</option>`).join('')}</select> ` : '';
  return `<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">${acctSel}<div class="seg" role="group" aria-label="Period">${o.map(([k, l]) => `<button data-range="${k}" aria-pressed="${S.range === k}">${l}</button>`).join('')}</div></div>`;
}
function head(title, sub, right = rangeCtl()) { return `<header class="page-head"><div><h1>${title}</h1>${sub ? `<p>${sub}</p>` : ''}</div>${right}</header>`; }
function periodText(ts) { if (!ts.length) return 'No trades in this period'; return `${ts.length} trades from ${fmtDate(ts[0].date)} to ${fmtDate(ts[ts.length - 1].date)}`; }
const spark = () => `<svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M10 2l2 5.5L17.5 10 12 12l-2 6-2-6-5.5-2L8 7.5z"/></svg>`;

/* ---------- analytics (same definitions as the backend) ---------- */
function stats(ts) {
  ts = closed(ts).slice().sort(chron);
  const wins = ts.filter(t => t.net > 0), losses = ts.filter(t => t.net <= 0);
  const gw = sum(wins.map(t => t.net)), gl = sum(losses.map(t => t.net));
  const rs = ts.filter(t => t.r != null).map(t => t.r);
  let eq = 0, peak = 0, dd = 0, sw = 0, sl = 0, cw = 0, cl = 0;
  for (const t of ts) { eq += t.net; peak = Math.max(peak, eq); dd = Math.min(dd, eq - peak); if (t.net > 0) { cw++; cl = 0 } else { cl++; cw = 0 } sw = Math.max(sw, cw); sl = Math.max(sl, cl); }
  const daily = {}; for (const t of ts) daily[cdate(t)] = (daily[cdate(t)] || 0) + t.net;
  const dv = Object.values(daily), m = avg(dv), sd = Math.sqrt(avg(dv.map(x => (x - m) ** 2)));
  return { n: ts.length, net: gw + gl, wr: ts.length ? wins.length / ts.length : 0, pf: gl ? gw / -gl : (gw > 0 ? Infinity : 0),
    exp: rs.length ? avg(rs) : null, avgW: avg(wins.map(t => t.net)), avgL: avg(losses.map(t => t.net)), dd, sharpe: sd ? m / sd * Math.sqrt(252) : 0, streakW: sw, streakL: sl };
}
function leaks(ts) {
  const b = MISTAKE_LEAKS.map(([k, label]) => ({ k, label, val: 0, n: 0 })); const late = { k: 'late', label: 'New trades after 11:00', val: 0, n: 0 };
  let clean = 0, cleanN = 0;
  for (const t of closed(ts)) { const i = MISTAKE_LEAKS.findIndex(([k]) => t.tags.includes(k)); if (i >= 0) { b[i].val += t.net; b[i].n++ } else if (t.min >= 660) { late.val += t.net; late.n++ } else { clean += t.net; cleanN++ } }
  const buckets = [...b, late].filter(x => x.n > 0);
  return { clean, cleanN, buckets, net: clean + sum(buckets.map(x => x.val)) };
}
function worstLeak(ts) { return leaks(ts).buckets.filter(b => b.val < 0).sort((a, b) => a.val - b.val)[0]; }
function patterns(ts) {
  ts = closed(ts).slice().sort(chron); const out = []; const days = {};
  for (const t of ts) (days[t.date] = days[t.date] || []).push(t);
  const after = [], other = [];
  for (const d in days) { const a = days[d]; a.forEach((t, i) => (i >= 2 && a[i - 1].net < 0 && a[i - 2].net < 0 ? after : other).push(t)); }
  if (after.length >= 4) out.push({ text: `After two losing trades in a row, you average ${money(avg(after.map(t => t.net)))} per trade. The rest of the time you average ${money(avg(other.map(t => t.net)))}.`, n: after.length });
  const bs = {}; for (const t of ts.filter(t => t.setup)) bs[t.setup] = (bs[t.setup] || 0) + t.net;
  const ranked = Object.entries(bs).sort((a, b) => b[1] - a[1]);
  if (ranked.length > 1) out.push({ text: `${esc(ranked[0][0])} made ${money(ranked[0][1])}. Your other setups combined made ${money(sum(ranked.slice(1).map(x => x[1])))}.`, n: ts.filter(t => t.setup === ranked[0][0]).length });
  const am = ts.filter(t => t.r != null && t.min < 660), pm = ts.filter(t => t.r != null && t.min >= 660);
  if (pm.length >= 4 && am.length) out.push({ text: `Before 11:00 your expectancy is ${fr(avg(am.map(t => t.r)))} per trade. After 11:00 it is ${fr(avg(pm.map(t => t.r)))}.`, n: pm.length });
  const left = ts.filter(t => t.r != null && t.mfe != null && t.r > 0 && t.mfe >= 2 && t.r < 1);
  if (left.length) out.push({ text: `${left.length} winners reached +2R and you closed them under +1R. Holding to plan would have added ${money(sum(left.map(t => (Math.min(t.mfe, t.targetR || 2) - t.r) * t.riskD)), false)}.`, n: left.length });
  if (!out.length && ts.length) out.push({ text: 'Tag mistakes and log plans with a stop on your trades. Patterns show up here once there is enough tagged data.', n: ts.length });
  return out;
}

/* ---------- dashboard ---------- */
function emptyState() {
  return `<div class="panel cta-empty"><h2>No trades yet</h2><p>Import a statement from your broker, or connect Schwab or Alpaca in Settings to sync fills automatically.</p>
    <a class="btn primary" href="#import" style="text-decoration:none">Import trades</a> <a class="btn" href="#settings" style="text-decoration:none">Connect a broker</a></div>`;
}
function vDashboard() {
  const ts = closed(scoped());
  if (!S.trades.length) return head('Overview', '', '') + emptyState();
  const St = stats(ts);
  const W = worstLeak(ts);
  return head('Overview', periodText(ts)) + `
  <section>${strip(St)}</section>
  <section class="grid2"><div class="panel"><h2>Equity curve</h2>${equity(ts)}</div><div class="panel">${prop()}</div></section>
  <section class="coach" aria-label="AI coach"><div class="coach-who">${spark()} Coach: rule for next week</div>
    ${W ? `<p class="rule">${RULES[W.k]}</p><p class="muted" style="margin:0">Your biggest leak is ${W.label.toLowerCase()}: ${money(W.val)} across ${W.n} trades.</p>` : `<p class="rule">No leak stands out in this period. Tag your mistakes so the coach can find them.</p>`}
    <ul class="patterns">${patterns(ts).slice(0, 3).map(p => `<li>${p.text}<small>Based on ${p.n} trades</small></li>`).join('')}</ul></section>`;
}
function strip(St) {
  const it = [['Net P&L', `<span class="${cls(St.net)}">${money(St.net)}</span>`], ['Win rate', (St.wr * 100).toFixed(0) + '%'], ['Profit factor', St.pf === Infinity ? '∞' : St.pf.toFixed(2)],
    ['Expectancy', `<span class="${cls(St.exp)}">${fr(St.exp)}</span>`], ['Max drawdown', `<span class="loss">${money(St.dd)}</span>`], ['Sharpe', St.sharpe.toFixed(2)]];
  return `<div class="strip">${it.map(([k, v]) => `<div class="stat"><span>${k}</span><b>${v}</b></div>`).join('')}</div>`;
}
function equity(ts) {
  if (!ts.length) return `<p class="empty">No closed trades in this period.</p>`;
  const daily = {}; for (const t of ts) daily[cdate(t)] = (daily[cdate(t)] || 0) + t.net;
  const ds = Object.keys(daily).sort(); let c = 0;
  const pts = ds.map(d => ({ d, day: daily[d], v: c += daily[d] }));
  const w = 600, h = 200, p = 10, vals = [0, ...pts.map(x => x.v)], lo = Math.min(...vals), hi = Math.max(...vals), sp = hi - lo || 1;
  const X = i => p + i / (pts.length - 1 || 1) * (w - 2 * p), Y = v => h - p - (v - lo) / sp * (h - 2 * p);
  S.eq = { pts, X, Y, w, h };
  const path = pts.map((q, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(q.v).toFixed(1)).join(' '); const end = pts[pts.length - 1].v;
  return `<div id="eq-wrap" style="position:relative">
    <svg id="eq-svg" viewBox="0 0 ${w} ${h}" role="img" aria-label="Cumulative net P&L by day, ending at ${money(end)}" style="width:100%;height:auto;touch-action:pan-y;cursor:crosshair">
    <line x1="${p}" x2="${w - p}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--line)" stroke-dasharray="3 4"/>
    <path d="${path} L${X(pts.length - 1)} ${Y(0)} L${X(0)} ${Y(0)} Z" fill="${end >= 0 ? 'var(--gain-soft)' : 'var(--loss-soft)'}" opacity=".7"/>
    <path d="${path}" fill="none" stroke="${end >= 0 ? 'var(--gain)' : 'var(--loss)'}" stroke-width="2.2" stroke-linejoin="round"/>
    <g id="eq-hover" style="display:none"><line id="eq-vl" y1="${p}" y2="${h - p}" stroke="var(--muted)" stroke-dasharray="3 3"/><circle id="eq-dot" r="4.5" fill="var(--ink)" stroke="var(--surface)" stroke-width="2"/></g></svg>
    <div id="eq-tip" style="display:none;position:absolute;top:0;pointer-events:none;background:var(--ink);color:var(--surface);padding:6px 10px;border-radius:8px;font-size:.84rem;white-space:nowrap;transform:translateX(-50%)"></div></div>
    <div class="kv"><span class="muted">${fmtDate(ds[0])}</span><b class="${cls(end)}">${money(end)} on ${fmtDate(ds[ds.length - 1])}</b></div>`;
}
function eqHover(e) {
  const svg = $('#eq-svg'); if (!svg || !S.eq) return;
  const r = svg.getBoundingClientRect(), { pts, X, Y, w } = S.eq;
  const vx = (e.clientX - r.left) / r.width * w;
  let i = 0, best = Infinity; pts.forEach((q, j) => { const dd = Math.abs(X(j) - vx); if (dd < best) { best = dd; i = j; } });
  const q = pts[i], x = X(i), y = Y(q.v);
  $('#eq-hover').style.display = ''; $('#eq-vl').setAttribute('x1', x); $('#eq-vl').setAttribute('x2', x);
  $('#eq-dot').setAttribute('cx', x); $('#eq-dot').setAttribute('cy', y);
  const tip = $('#eq-tip'); tip.style.display = '';
  tip.innerHTML = `<b>${fmtDate(q.d)}</b><br>Total ${money(q.v)} · day ${money(q.day)}`;
  const px_ = x / w * r.width; tip.style.left = Math.min(Math.max(px_, 70), r.width - 70) + 'px';
  tip.style.top = Math.max(0, y / S.eq.h * r.height - 58) + 'px';
}
function eqLeave() { const g = $('#eq-hover'); if (g) g.style.display = 'none'; const t = $('#eq-tip'); if (t) t.style.display = 'none'; }
function prop() {
  const P = S.me.settings.prop;
  if (!P.enabled || !P.account) return `<h2>Prop challenge</h2><p class="muted">Track drawdown buffer, profit target and daily loss limit for a prop firm challenge.</p><a class="btn" href="#settings" style="text-decoration:none">Set up a challenge</a>`;
  const fut = closed(S.trades).filter(t => t.acct === P.account && (!P.start || cdate(t) >= P.start)).sort((a, b) => cdate(a).localeCompare(cdate(b)));
  const days = {}; for (const t of fut) days[cdate(t)] = (days[cdate(t)] || 0) + t.net;
  let bal = P.balance, peak = P.balance, failed = null;
  for (const d of Object.keys(days).sort()) { bal += days[d]; const thr = Math.min(peak - P.trailing, P.balance + 100); if (bal <= thr && !failed) failed = d; peak = Math.max(peak, bal); }
  const thr = Math.min(peak - P.trailing, P.balance + 100), buffer = bal - thr, prog = P.target ? Math.max(0, Math.min(1, (bal - P.balance) / P.target)) : 0;
  const lastDay = fut.length ? cdate(fut[fut.length - 1]) : null, today = lastDay ? days[lastDay] : 0; const bpct = P.trailing ? Math.max(0, Math.min(100, buffer / P.trailing * 100)) : 0;
  return `<h2>${esc(P.account)}</h2><p class="muted" style="margin:-6px 0 12px;font-size:.9rem">${P.start ? 'Since ' + fmtDate(P.start) + '. ' : ''}Trailing drawdown ${money(P.trailing, false)} on end-of-day balance.</p>
  ${failed ? `<p class="loss" style="font-weight:600;margin:0 0 8px">Drawdown limit hit on ${fmtDate(failed)}.</p>` : ''}
  <div class="kv"><span>Drawdown buffer left</span><b class="${bpct < 30 ? 'loss' : ''}">${money(buffer, false)}</b></div>
  <div class="meter" aria-hidden="true"><i class="${bpct < 30 ? 'bad' : bpct < 55 ? 'warn' : ''}" style="width:${bpct}%"></i></div>
  <div class="kv"><span>Profit target progress</span><b>${money(bal - P.balance)} of ${money(P.target, false)}</b></div>
  <div class="meter" aria-hidden="true"><i style="width:${prog * 100}%"></i></div>
  <div class="kv"><span>Daily loss limit (${lastDay ? fmtDate(lastDay) : '—'})</span><b class="${today < 0 ? 'loss' : ''}">${today < 0 ? money(-today, false) + ' of ' + money(P.dailyLoss, false) + ' used' : 'Not touched'}</b></div>
  <div class="kv"><span>Balance</span><b>${money(bal, false)}</b></div>`;
}

/* ---------- trades ---------- */
const tf = { q: '', setup: '', tag: '', res: '', acct: '', asset: '' };
function vTrades() {
  const ts = scoped();
  if (!S.trades.length) return head('Trades', '', '') + emptyState();
  const setups = [...new Set(S.trades.map(t => t.setup).filter(Boolean))], accts = [...new Set(S.trades.map(t => t.acct))];
  return head('Trades', periodText(ts)) + `<div class="filters">
    <input id="f-q" type="search" placeholder="Symbol" value="${esc(tf.q)}" aria-label="Filter by symbol" size="10">
    ${accts.length > 1 ? `<select id="f-acct" aria-label="Account"><option value="">All accounts</option>${accts.map(s => `<option ${tf.acct === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>` : ''}
    <select id="f-asset" aria-label="Stock or option"><option value="">Stocks and options</option>${['stock', 'option', 'future'].filter(a => S.trades.some(t => t.assetType === a)).map(a => `<option value="${a}" ${tf.asset === a ? 'selected' : ''}>${a[0].toUpperCase() + a.slice(1)}s</option>`).join('')}</select>
    <select id="f-setup" aria-label="Setup"><option value="">All setups</option>${setups.map(s => `<option ${tf.setup === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>
    <select id="f-tag" aria-label="Mistake"><option value="">Any tags</option><option value="__none" ${tf.tag === '__none' ? 'selected' : ''}>No tags</option>${S.me.mistakes.map(s => `<option ${tf.tag === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
    <select id="f-res" aria-label="Result"><option value="">Winners and losers</option><option value="w" ${tf.res === 'w' ? 'selected' : ''}>Winners</option><option value="l" ${tf.res === 'l' ? 'selected' : ''}>Losers</option><option value="o" ${tf.res === 'o' ? 'selected' : ''}>Open</option></select>
  </div><div id="trade-table">${tradeTable(filtered())}</div>`;
}
function filtered() {
  return scoped().filter(t => (!tf.q || t.sym.toLowerCase().includes(tf.q.toLowerCase()) || (t.underlying || '').toLowerCase() === tf.q.toLowerCase()) && (!tf.asset || t.assetType === tf.asset) && (!tf.acct || t.acct === tf.acct) && (!tf.setup || t.setup === tf.setup)
    && (!tf.tag || (tf.tag === '__none' ? !t.tags.length : t.tags.includes(tf.tag)))
    && (!tf.res || (tf.res === 'o' ? t.status === 'open' : t.status === 'closed' && (tf.res === 'w' ? t.net > 0 : t.net <= 0)))).slice().reverse();
}
function tradeTable(ts, compact) {
  if (!ts.length) return `<div class="tablewrap"><p class="empty">No trades match these filters.</p></div>`;
  return `<div class="tablewrap"><table><thead><tr><th>Date</th><th>Time</th><th>Ticker / contract</th><th>Side</th>${compact ? '' : '<th>Setup</th><th>Tags</th>'}<th class="r">R</th><th class="r">Net P&L</th></tr></thead><tbody>
  ${ts.map(t => `<tr data-open="${t.id}" tabindex="0"><td>${fmtDate(t.date)}</td><td>${t.time}</td><td><b>${esc(t.underlying || t.sym)}</b>${t.assetType === 'option' ? ` <span class="muted">${esc(fmtExp(t.expiry))} ${t.strike} ${t.optType}</span>` : ''}</td><td>${t.dir}${t.status === 'open' ? ' <span class="chip">Open</span>' : ''}</td>${compact ? '' : `<td>${esc(t.setup) || '<span class="muted">—</span>'}</td><td>${t.tags.map(x => `<span class="chip ${x === 'Early exit' ? '' : 'bad'}">${esc(x)}</span>`).join('')}</td>`}<td class="r ${cls(t.r)}">${fr(t.r)}</td><td class="r ${cls(t.net)}"><b>${t.status === 'open' ? '<span class="muted">open</span>' : money(t.net)}</b></td></tr>`).join('')}
  </tbody></table></div>`;
}

/* ---------- trade drawer ---------- */
let cur = null; const replay = { k: Infinity, timer: null };
function openTrade(id) {
  cur = byId(id); if (!cur) return;
  S.tf = cur.hold == null || cur.hold > 2 * 1440 ? '1d' : cur.hold > 360 ? '1h' : '5m';
  replay.k = Infinity; stopReplay(); renderDrawer();
  $('#scrim').hidden = false; $('#drawer').classList.add('open'); document.body.style.overflow = 'hidden';
  setTimeout(() => $('#dr-close')?.focus(), 50);
}
function closeTrade() { stopReplay(); $('#drawer').classList.remove('open'); $('#scrim').hidden = true; document.body.style.overflow = ''; cur = null; render(); }
function renderDrawer() {
  const t = cur, p = t.plan || {}; const setups = S.me.settings.setups || [];
  $('#drawer').innerHTML = `<div class="dr-head"><div><h2>${esc(symFmt(t.sym))} ${t.dir.toLowerCase()} ${t.status === 'open' ? '<span class="chip">Open</span>' : `<span class="${cls(t.net)}">${money(t.net)}</span>`}</h2>
    <div class="muted">${weekday(t.date)} ${fmtDate(t.date)} at ${t.time}${t.hold != null ? `, held ${t.hold} min` : ''}, ${esc(t.acct)} ${t.r != null ? `<span class="${cls(t.r)}">(${fr(t.r)})</span>` : ''}</div></div>
    <button class="btn" id="dr-close" aria-label="Close trade detail">Close</button></div>
  <div class="dr-body">
    <div class="chartbox"><div id="chart-note" class="muted" style="font-size:.85rem;margin:0 4px 6px"></div><div id="chart"><p class="loading">Loading chart…</p></div>
      <div class="chart-ctl"><div class="seg" role="group" aria-label="Timeframe">${[['5m', '5m'], ['15m', '15m'], ['1h', '1h'], ['4h', '4h'], ['1d', 'Daily']].map(([k, l]) => `<button data-tf="${k}" aria-pressed="${S.tf === k}">${l}</button>`).join('')}</div>
      <button class="btn" id="rp-play">Replay</button><input type="range" id="rp" min="1" aria-label="Replay position"></div></div>
    <div class="coach review" id="review"><p class="loading" style="padding:0">Loading coach review…</p></div>
    <div class="panel"><h2>Fills</h2><div class="tablewrap" style="border:0"><table class="pvsa"><tbody>
      ${t.assetType === 'option' ? `<tr><td>Contract</td><td class="r">${esc(t.underlying)} ${t.optType} ${t.strike} exp ${esc(fmtExp(t.expiry))} (${t.dte} days at entry)</td></tr>` : ''}<tr><td>Quantity</td><td class="r">${t.qty}${t.assetType === 'option' ? ' contracts' : ''}</td></tr><tr><td>Average entry</td><td class="r">${px(t.entry)}</td></tr><tr><td>Average exit</td><td class="r">${px(t.exit)}</td></tr>
      <tr><td>Gross / fees</td><td class="r">${money(t.gross)} / ${money(-t.fees)}</td></tr>
      ${t.riskD != null ? `<tr><td>Risk at plan stop</td><td class="r ${t.riskD > S.me.settings.riskPerTrade * 1.25 ? 'dev' : ''}">${money(t.riskD, false)} (plan ${money(S.me.settings.riskPerTrade, false)})</td></tr>` : ''}
      ${t.mae != null ? `<tr><td>Went against / for you</td><td class="r">−${t.mae.toFixed(2)}R / +${t.mfe.toFixed(2)}R</td></tr>` : ''}
    </tbody></table></div></div>
    <div class="panel"><h2>Plan</h2>
      <div class="form-grid">
        <div class="field"><label for="p-setup">Setup</label><input id="p-setup" type="text" list="setup-list" value="${esc(t.setup)}"><datalist id="setup-list">${setups.map(s => `<option value="${esc(s)}">`).join('')}</datalist></div>
        <div class="field"><label for="p-entry">Planned entry</label><input id="p-entry" type="number" step="any" value="${p.entry ?? ''}"></div>
        <div class="field"><label for="p-stop">Stop</label><input id="p-stop" type="number" step="any" value="${p.stop ?? ''}"></div>
        <div class="field"><label for="p-target">Target</label><input id="p-target" type="number" step="any" value="${p.target ?? ''}"></div>
      </div>
      <div class="field"><label for="p-thesis">Thesis</label><textarea id="p-thesis" placeholder="Why this trade, where it's wrong">${esc(p.thesis || '')}</textarea></div>
      <div class="field"><label for="note">Notes</label><textarea id="note" placeholder="What did you see? What would you do differently?">${esc(t.notes)}</textarea></div>
      <button class="btn primary" id="save-plan">Save plan and notes</button>
    </div>
    <div class="panel"><h2>Tags</h2>
      <div class="filters" role="group" aria-label="Mistake tags">${S.me.mistakes.map(m => `<button class="toggle" data-tag="${m}" aria-pressed="${t.tags.includes(m)}">${m}</button>`).join('')}</div>
      <div class="filters" role="group" aria-label="Emotion">${S.me.emotions.map(m => `<button class="toggle emo" data-emo="${m}" aria-pressed="${t.emotion === m}">${m}</button>`).join('')}</div>
    </div>
  </div>`;
  loadBars(); loadReview();
}
async function patch(body, msg) {
  const id = cur.id;
  try { const v = replaceTrade(await api(`/trades/${id}`, { method: 'PATCH', body })); if (cur && cur.id === id) { cur = v; renderDrawer(); } toast(msg); }
  catch (e) { toast(e.message); }
}
function reviewHtml(r) {
  const v = { followed_plan: ['ok', 'Followed the plan'], partial: ['mid', 'Partly followed the plan'], broke_plan: ['bad', 'Broke the plan'], no_plan: ['mid', 'No plan logged'] }[r.verdict] || ['mid', 'Reviewed'];
  return `<div class="coach-who">${spark()} Coach review <span class="verdict ${v[0]}" style="margin-left:6px">${v[1]}</span></div>
    ${r.what_worked?.length ? `<h3>What worked</h3><ul>${r.what_worked.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    ${r.what_broke?.length ? `<h3>What broke</h3><ul>${r.what_broke.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    ${r.lesson ? `<h3>Takeaway</h3><p style="margin:0">${esc(r.lesson)}</p>` : ''}
    ${r.suggested_tags?.filter(x => !cur.tags.includes(x)).length ? `<div class="sug-tags"><span class="muted">Suggested tags:</span> ${r.suggested_tags.filter(x => !cur.tags.includes(x)).map(x => `<button class="toggle" data-tag="${esc(x)}" aria-pressed="false">Add ${esc(x)}</button>`).join(' ')}</div>` : ''}
    <p style="margin:12px 0 0"><button class="linkbtn" id="rv-run">Review again with my latest plan and tags</button></p>`;
}
async function loadReview() {
  const id = cur.id, box = () => cur && cur.id === id ? $('#review') : null;
  if (cur.status !== 'closed') { box().innerHTML = `<div class="coach-who">${spark()} Coach review</div><p style="margin:0">The review runs once the trade closes.</p>`; return; }
  try { const r = S.reviews[id] || await api(`/trades/${id}/review`); S.reviews[id] = r; box() && (box().innerHTML = reviewHtml(r)); }
  catch (e) {
    if (!box()) return;
    box().innerHTML = e.status === 404 ? `<div class="coach-who">${spark()} Coach review</div><p style="margin:0 0 10px">No review yet. Add your plan and tags first for a better review.</p><button class="btn coachbtn" id="rv-run">Get coach review</button>`
      : `<div class="errbox">${esc(e.message)}</div>`;
  }
}
async function runReview() {
  const id = cur.id; $('#review').innerHTML = `<p class="loading" style="padding:0">The coach is reviewing this trade…</p>`;
  try { const r = await api(`/trades/${id}/review`, { method: 'POST' }); S.reviews[id] = r; const t = byId(id); if (t) t.reviewed = true; if (cur && cur.id === id) $('#review').innerHTML = reviewHtml(r); }
  catch (e) { if (cur && cur.id === id) $('#review').innerHTML = `<div class="errbox">${esc(e.message)}</div>`; }
}
async function loadBars() {
  const id = cur.id, key = id + ':' + S.tf;
  try {
    if (!S.bars[key]) S.bars[key] = await api(`/trades/${id}/bars?tf=${S.tf}`);
    if (cur && cur.id === id) drawChart();
  } catch (e) { S.bars[key] = { error: e.message }; if (cur && cur.id === id) drawChart(); }
}
function drawChart() {
  const t = cur, data = S.bars[t.id + ':' + S.tf];
  if (!data) return;
  if (data.error || !data.bars?.length) { $('#chart').innerHTML = `<p class="empty">${esc(data.error || 'No bars returned for this trade window.')}</p>`; $('.chart-ctl .btn').hidden = true; $('#rp').hidden = true; $('#chart-note').textContent = ''; return; }
  $('.chart-ctl .btn').hidden = false; $('#rp').hidden = false;
  const onUnderlying = t.assetType === 'option';
  $('#chart-note').textContent = `${data.symbol} ${data.kind === 'future' ? 'continuous futures' : 'stock'} chart, ${S.tf === '1d' ? 'daily' : S.tf} bars from ${data.source}${onUnderlying ? `. Markers show when the ${t.optType} was bought and sold; prices on the chart are the stock's.` : ''}`;
  const bars = data.bars, n = bars.length, k = Math.min(replay.k, n), vis = bars.slice(0, k);
  const cmp = S.tf === '1d' ? 10 : 16;
  const idx = ts => { let i = 0; for (let j = 0; j < n; j++) if (bars[j].t.slice(0, cmp) <= ts.slice(0, cmp)) i = j; return i; };
  const ei = idx(t.openTs), xi = t.closeTs ? idx(t.closeTs) : null; const p = onUnderlying ? {} : (t.plan || {});
  const lv = onUnderlying ? [] : [p.stop, p.target, p.entry, t.entry, t.exit].filter(v => v != null);
  const W = 720, H = 340, pl = 6, pr = 82, pt = 12, pb = 34;
  const lo = Math.min(...bars.map(b => b.l), ...lv), hi = Math.max(...bars.map(b => b.h), ...lv), pad = (hi - lo) * .06 || 1;
  const Y = v => pt + (hi + pad - v) / ((hi - lo) + 2 * pad) * (H - pt - pb), bw = (W - pl - pr) / n, X = i => pl + i * bw + bw / 2;
  const line = (v, col, lab, dash) => v == null ? '' : `<line x1="${pl}" x2="${W - pr}" y1="${Y(v)}" y2="${Y(v)}" stroke="${col}" stroke-dasharray="${dash}" stroke-width="1.2"/><text x="${W - pr + 6}" y="${Y(v) + 4}" font-size="11" fill="${col}">${lab} ${px(v)}</text>`;
  const mk = (i, price, col, lab, below) => { const x = X(i), y = Y(price); const s = below ? `${x},${y + 5} ${x - 6},${y + 15} ${x + 6},${y + 15}` : `${x},${y - 5} ${x - 6},${y - 15} ${x + 6},${y - 15}`; return `<polygon points="${s}" fill="${col}"/><text x="${x}" y="${below ? y + 28 : y - 20}" font-size="11" text-anchor="middle" fill="${col}" font-weight="600">${lab}</text>`; };
  const candles = vis.map((b, i) => { const up = b.c >= b.o, col = up ? 'var(--candle-up)' : 'var(--candle-dn)'; const y1 = Y(Math.max(b.o, b.c)), y2 = Y(Math.min(b.o, b.c));
    return `<line x1="${X(i)}" x2="${X(i)}" y1="${Y(b.h)}" y2="${Y(b.l)}" stroke="${col}"/><rect x="${X(i) - bw * .34}" y="${y1}" width="${Math.max(1, bw * .68)}" height="${Math.max(1, y2 - y1)}" fill="${col}"/>`; }).join('');
  const zoneEnd = xi == null ? k - 1 : Math.min(k - 1, xi);
  const zone = k > ei ? `<rect x="${X(ei) - bw / 2}" y="${pt}" width="${(zoneEnd - ei + 1) * bw}" height="${H - pt - pb}" fill="var(--sunk)" fill-opacity=".75"/>` : '';
  const long = t.dir === 'Long';
  // For options, entry/exit prices are premiums, so markers sit on the stock bar instead.
  const ePrice = onUnderlying ? (long ? bars[ei].l : bars[ei].h) : t.entry;
  const xPrice = xi == null ? null : onUnderlying ? (long ? bars[xi].h : bars[xi].l) : t.exit;
  const eLab = onUnderlying ? `Buy ${px(t.entry)}` : 'Entry', xLab = onUnderlying ? `Sell ${px(t.exit)}` : 'Exit';
  // Axes: price gridlines on the right, time labels along the bottom.
  const MONS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const dayLab = t => `${MONS[+t.slice(5, 7) - 1]} ${+t.slice(8, 10)}`;
  const yTicks = [0, .25, .5, .75, 1].map(f => lo + (hi - lo) * f);
  let axes = yTicks.map(v => `<line x1="${pl}" x2="${W - pr}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)" stroke-width=".6"/><text x="${W - pr + 6}" y="${Y(v) + 4}" font-size="10.5" fill="var(--muted)">${px(v)}</text>`).join('');
  const step = Math.max(1, Math.ceil(n / 7));
  let prevDay = '';
  for (let i = 0; i < n; i += step) {
    const t0 = bars[i].t, day = t0.slice(0, 10);
    let lab;
    if (S.tf === '1d') lab = dayLab(t0) + (prevDay && prevDay.slice(0, 4) !== day.slice(0, 4) ? ` '${day.slice(2, 4)}` : '');
    else if (S.tf === '4h') lab = dayLab(t0);
    else lab = day !== prevDay ? `${dayLab(t0)} ${t0.slice(11, 16)}` : t0.slice(11, 16);
    prevDay = day;
    axes += `<line x1="${X(i)}" x2="${X(i)}" y1="${pt}" y2="${H - pb}" stroke="var(--line)" stroke-width=".4"/><line x1="${X(i)}" x2="${X(i)}" y1="${H - pb}" y2="${H - pb + 4}" stroke="var(--muted)"/><text x="${X(i)}" y="${H - pb + 16}" font-size="10.5" text-anchor="middle" fill="var(--muted)">${lab}</text>`;
  }
  axes += `<line x1="${pl}" x2="${W - pr}" y1="${H - pb}" y2="${H - pb}" stroke="var(--line)"/>`;
  $('#chart').innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(data.symbol)} ${S.tf} chart for this trade">${axes}${zone}
    ${line(p.stop, 'var(--loss)', 'Stop', '5 4')}${line(p.target, 'var(--gain)', 'Target', '5 4')}${line(p.entry, 'var(--muted)', 'Plan', '2 4')}
    ${candles}${k > ei ? mk(ei, ePrice, 'var(--coach)', eLab, long) : ''}${xi != null && k > xi ? mk(xi, xPrice, 'var(--ink)', xLab, !long) : ''}
</svg>`;
  const rp = $('#rp'); if (rp) { rp.max = n; rp.value = k; }
}
function stopReplay() { clearInterval(replay.timer); replay.timer = null; const b = $('#rp-play'); if (b) b.textContent = 'Replay'; }
function startReplay() {
  const d = S.bars[cur.id + ':' + S.tf]; if (!d?.bars) return; const n = d.bars.length;
  if (replay.k >= n) replay.k = Math.min(20, n); $('#rp-play').textContent = 'Pause';
  replay.timer = setInterval(() => { replay.k++; drawChart(); if (replay.k >= n) stopReplay(); }, matchMedia('(prefers-reduced-motion: reduce)').matches ? 40 : 140);
}

/* ---------- stats page ---------- */
function applyWhatIf(ts) {
  let out = ts; const w = S.wi;
  if (w.late) out = out.filter(t => t.min < 660);
  if (w.revenge) out = out.filter(t => !t.tags.includes('Revenge'));
  if (w.worst) { const W = worstLeak(ts); if (W) out = out.filter(t => W.k === 'late' ? t.min < 660 : !t.tags.includes(W.k)); }
  if (w.fixed2) out = out.map(t => { if (t.r == null || t.mfe == null) return t; const r = t.mfe >= 2 ? 2 : t.r; return { ...t, r, net: r * t.riskD - t.fees }; });
  return out;
}
const BD = {
  setup: ['Setup', t => t.setup || 'No setup'],
  time: ['Time of day', t => t.min < 600 ? 'Before 10:00' : t.min < 660 ? '10:00–11:00' : t.min < 720 ? '11:00–12:00' : t.min < 840 ? '12:00–14:00' : '14:00 and later'],
  weekday: ['Weekday', t => weekday(t.date)],
  hold: ['Hold time', t => t.hold == null ? 'Unknown' : t.hold < 10 ? 'Under 10 min' : t.hold < 30 ? '10–30 min' : t.hold < 60 ? '30–60 min' : t.hold < 1440 ? '1 hour to 1 day' : 'Over a day'],
  underlying: ['Ticker', t => t.underlying || t.sym],
  asset: ['Stock / option', t => ({ stock: 'Stock', option: 'Option', future: 'Future' })[t.assetType] || 'Stock'],
  optType: ['Call / put', t => t.optType ? (t.optType === 'call' ? 'Calls' : 'Puts') : 'Not an option'],
  dte: ['Days to expiry', t => t.dte == null ? 'Not an option' : t.dte <= 1 ? '0–1 days' : t.dte <= 7 ? '2–7 days' : t.dte <= 30 ? '8–30 days' : t.dte <= 60 ? '31–60 days' : 'Over 60 days'],
  symbol: ['Contract', t => symFmt(t.sym)], account: ['Account', t => t.acct], tag: ['Mistake tag', null], emotion: ['Emotion', t => t.emotion || 'Not set']
};
function vAnalytics() {
  const ts = closed(scoped());
  if (!S.trades.length) return head('Stats', '', '') + emptyState();
  const W = worstLeak(ts), A = stats(ts), B = stats(applyWhatIf(ts)), on = Object.values(S.wi).some(Boolean);
  const rows = [['Net P&L', x => `<span class="${cls(x.net)}">${money(x.net)}</span>`], ['Trades', x => x.n], ['Win rate', x => (x.wr * 100).toFixed(0) + '%'], ['Profit factor', x => x.pf === Infinity ? '∞' : x.pf.toFixed(2)], ['Expectancy', x => fr(x.exp)], ['Max drawdown', x => money(x.dd)]];
  return head('Stats', periodText(ts)) + `<section>${strip(A)}</section>
  <section class="panel"><h2>What if</h2><div class="filters">
    <button class="toggle whatif" data-wi="late" aria-pressed="${S.wi.late}">Skip trades after 11:00</button>
    <button class="toggle whatif" data-wi="revenge" aria-pressed="${S.wi.revenge}">Skip revenge trades</button>
    <button class="toggle whatif" data-wi="fixed2" aria-pressed="${S.wi.fixed2}">Fixed 2R target</button>
    ${W ? `<button class="toggle whatif" data-wi="worst" aria-pressed="${S.wi.worst}">Skip my worst leak (${W.label.toLowerCase()})</button>` : ''}</div>
    ${S.wi.fixed2 ? '<p class="muted" style="margin:0 0 8px;font-size:.88rem">Fixed 2R only changes trades that have a plan stop and bar data.</p>' : ''}
    <div class="tablewrap" style="border:0"><table><thead><tr><th></th><th class="r">Actual</th><th class="r">What if</th><th class="r">Difference</th></tr></thead><tbody>
    ${rows.map(([k, f]) => `<tr><td>${k}</td><td class="r">${f(A)}</td><td class="r">${on ? f(B) : '—'}</td><td class="r">${on && k === 'Net P&L' ? `<b class="${cls(B.net - A.net)}">${money(B.net - A.net)}</b>` : ''}</td></tr>`).join('')}
    </tbody></table></div></section>
  <section class="panel"><h2>Breakdown</h2><div class="filters" role="group" aria-label="Group by">${Object.entries(BD).map(([k, [l]]) => `<button class="toggle emo" data-bd="${k}" aria-pressed="${S.bd === k}">${l}</button>`).join('')}</div>${breakdown(ts)}</section>
  <section class="grid2">
    <div class="panel"><h2>How much of each move you kept</h2>${scatter(ts)}</div>
    <div class="panel"><h2>Streaks and averages</h2>
      <div class="kv"><span>Average winner</span><b class="gain">${money(A.avgW)}</b></div><div class="kv"><span>Average loser</span><b class="loss">${money(A.avgL)}</b></div>
      <div class="kv"><span>Longest winning streak</span><b>${A.streakW} trades</b></div><div class="kv"><span>Longest losing streak</span><b>${A.streakL} trades</b></div>
      <div class="kv"><span>Winners that went over 0.7R against you first</span><b>${ts.filter(t => t.r > 0 && t.mae > .7).length}</b></div>
      <div class="kv"><span>Losses bigger than 1.1R</span><b class="loss">${ts.filter(t => t.r != null && t.r < -1.1).length}</b></div></div>
  </section>`;
}
function breakdown(ts) {
  const g = {};
  if (S.bd === 'tag') { for (const t of ts) for (const x of (t.tags.length ? t.tags : ['No tags'])) (g[x] = g[x] || []).push(t); }
  else { const f = BD[S.bd][1]; for (const t of ts) (g[f(t)] = g[f(t)] || []).push(t); }
  const order = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const ent = Object.entries(g).map(([k, a]) => [k, stats(a)]);
  if (S.bd === 'weekday') ent.sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0])); else if (S.bd === 'dte') { const o = ['0–1 days', '2–7 days', '8–30 days', '31–60 days', 'Over 60 days', 'Not an option']; ent.sort((a, b) => o.indexOf(a[0]) - o.indexOf(b[0])); } else if (S.bd === 'time' || S.bd === 'hold') ent.sort((a, b) => a[0].localeCompare(b[0])); else ent.sort((a, b) => b[1].net - a[1].net);
  const mx = Math.max(1, ...ent.map(e => Math.abs(e[1].net)));
  return `<div class="tablewrap" style="border:0"><table><thead><tr><th>${BD[S.bd][0]}</th><th class="r">Trades</th><th class="r">Win rate</th><th class="r">Expectancy</th><th class="r">Net P&L</th><th style="width:28%"></th></tr></thead><tbody>
  ${ent.map(([k, s]) => `<tr><td><b>${esc(k)}</b></td><td class="r">${s.n}</td><td class="r">${(s.wr * 100).toFixed(0)}%</td><td class="r ${cls(s.exp)}">${fr(s.exp)}</td><td class="r ${cls(s.net)}"><b>${money(s.net)}</b></td><td><div class="bar ${s.net < 0 ? 'neg' : ''}" style="width:${Math.abs(s.net) / mx * 100}%"></div></td></tr>`).join('')}</tbody></table></div>`;
}
function scatter(ts) {
  const pts = ts.filter(t => t.r != null && t.mfe != null);
  if (pts.length < 3) return `<p class="muted">This chart needs trades with a plan stop and bar data (stocks with Alpaca connected). The coach review fills in the data when it runs.</p>`;
  const W = 420, H = 300, p = 34, xm = 4, y0 = -3, y1 = 4; const X = v => p + v / xm * (W - p - 10), Y = v => 10 + (y1 - v) / (y1 - y0) * (H - 10 - p);
  const grid = [-2, 0, 2, 4].map(v => `<line x1="${p}" x2="${W - 10}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${p - 6}" y="${Y(v) + 4}" font-size="10" text-anchor="end" fill="var(--muted)">${v}R</text>`).join('') + [0, 1, 2, 3, 4].map(v => `<text x="${X(v)}" y="${H - p + 16}" font-size="10" text-anchor="middle" fill="var(--muted)">${v}R</text>`).join('');
  return `<p class="muted" style="margin:-4px 0 10px;font-size:.9rem">How far each trade ran in your favor (across) against what you closed it at (up). Dots far below the diagonal are profit left on the table.</p>
  <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Best move in your favor versus realized R" style="width:100%;height:auto">${grid}<line x1="${X(0)}" y1="${Y(0)}" x2="${X(4)}" y2="${Y(4)}" stroke="var(--muted)" stroke-dasharray="4 4"/>
  ${pts.map(t => `<circle data-open="${t.id}" cx="${X(Math.min(xm, t.mfe))}" cy="${Y(Math.max(y0, Math.min(y1, t.r)))}" r="4" fill="${t.r > 0 ? 'var(--gain)' : 'var(--loss)'}" fill-opacity=".65" style="cursor:pointer"><title>${esc(symFmt(t.sym))} ${fmtDate(t.date)}: ran +${t.mfe.toFixed(1)}R, closed ${fr(t.r)}</title></circle>`).join('')}</svg>`;
}

/* ---------- coach ---------- */
function vCoach() {
  const r = S.report;
  const rep = r === undefined ? `<p class="loading" style="padding:0">Loading your latest report…</p>`
    : r === null ? `<p style="margin:0 0 10px">No weekly report yet. Reports are written every Sunday morning, or you can write one now.</p>`
    : `<div class="coach-who">${spark()} Weekly report, week ending ${fmtDate(r.weekEnding)}</div><p class="rule">${esc(r.headline)}</p><p style="margin:0 0 6px">${esc(r.summary)}</p>
      ${(r.leaks || []).length ? `<ul class="patterns">${r.leaks.map((b, i) => `<li><b>${i === 0 ? 'Top leak' : 'Leak'}: ${esc(b.label)}</b>, ${money(b.dollars)}<small>${esc(b.comment)}</small></li>`).join('')}</ul>` : ''}
      ${r.rule ? `<p style="margin:14px 0 0"><b>Rule for next week:</b> ${esc(r.rule)}</p><p class="muted" style="margin:4px 0 0">${esc(r.rule_reason || '')}</p>` : ''}`;
  return head('Coach', 'Weekly report and questions about your trades', `<button class="btn coachbtn" id="rep-run">Write report now</button>`) + `
  <section class="coach" id="report">${rep}</section>
  <section><h2>Ask about your trades</h2>
    <div class="chat" id="chat">${S.chat.length ? S.chat.map(m => `<div class="msg ${m.role === 'assistant' ? 'ai' : 'me'}">${m.role === 'assistant' ? esc(m.content).replace(/\n/g, '<br>') + (m.tradeIds?.length ? tradeTable(m.tradeIds.map(byId).filter(Boolean), true) : '') : esc(m.content)}</div>`).join('')
      : `<div class="msg ai">Ask about your trades in plain words. Answers come only from your journal data.</div>`}${S.chatBusy ? '<div class="msg ai loading">Looking through your trades…</div>' : ''}</div>
    <div class="sugg">${['Show my best trades on TSLA in the first hour', 'How much did revenge trades cost me?', 'What is my win rate after 11:00?', 'Which setup has the best expectancy?'].map(s => `<button data-ask="${s}">${s}</button>`).join('')}</div>
    <form class="composer" id="ask"><input id="askq" placeholder="Ask a question about your trades" aria-label="Question" autocomplete="off"><button class="btn coachbtn" type="submit" ${S.chatBusy ? 'disabled' : ''}>Ask</button></form>
  </section>`;
}
async function afterCoach() {
  if (S.report !== undefined) return;
  try { S.report = await api('/reports/latest'); } catch (e) { S.report = e.status === 404 ? null : null; }
  if (route() === 'coach') render();
}
async function ask(q) {
  q = q.trim(); if (!q || S.chatBusy) return;
  S.chat.push({ role: 'user', content: q }); S.chatBusy = true; render(); $('#chat').lastElementChild.scrollIntoView({ block: 'nearest' });
  try { const r = await api('/coach/chat', { method: 'POST', body: { messages: S.chat.map(m => ({ role: m.role, content: m.content })) } }); S.chat.push({ role: 'assistant', content: r.reply || '…', tradeIds: r.tradeIds }); }
  catch (e) { S.chat.push({ role: 'assistant', content: e.message }); }
  S.chatBusy = false; if (route() === 'coach') { render(); $('#chat').lastElementChild.scrollIntoView({ block: 'nearest' }); $('#askq')?.focus(); }
}
async function writeReport() {
  const prev = S.report?.createdAt;
  try { await api('/reports', { method: 'POST' }); } catch (e) { toast(e.message); return; }
  toast('Writing your report. It appears here in about a minute.');
  for (let i = 0; i < 18; i++) { await sleep(10000); try { const r = await api('/reports/latest'); if (r.createdAt !== prev) { S.report = r; if (route() === 'coach') render(); toast('Report ready'); return; } } catch (e) {} }
  toast('The report is taking longer than usual. Check back in a few minutes.');
}

/* ---------- daily journal ---------- */
function vJournal() {
  const todayNY = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
  const days = [...new Set([todayNY, ...S.trades.map(t => t.date), ...S.trades.filter(t => t.closeTs).map(cdate)])].sort().reverse(); if (!S.jDay) S.jDay = days[0];
  const d = S.daily[S.jDay]; const ts = S.trades.filter(t => t.date === S.jDay || (t.closeTs && cdate(t) === S.jDay));
  const St = stats(ts.filter(t => t.status === 'closed' && cdate(t) === S.jDay));
  const score = (k, l) => `<div class="field"><label id="lb-${k}">${l}</label><div class="scores" role="group" aria-labelledby="lb-${k}">${[1, 2, 3, 4, 5].map(n => `<button data-score="${k}" data-v="${n}" aria-pressed="${d && d[k] === n}">${n}</button>`).join('')}</div></div>`;
  return head('Daily journal', 'Plan before the open, recap after the close', '') + `
  <div class="filters"><select id="jday" aria-label="Day">${days.map(x => `<option value="${x}" ${x === S.jDay ? 'selected' : ''}>${weekday(x)} ${fmtDate(x)}</option>`).join('')}</select></div>
  <section class="grid2"><div class="panel">${!d ? '<p class="loading">Loading…</p>' : `
    <div class="field"><label for="j-pre">Pre-market plan</label><textarea id="j-pre" placeholder="Key levels, A+ setups you will take, max loss for the day">${esc(d.pre || '')}</textarea></div>
    <div class="field"><label for="j-rec">End-of-day recap</label><textarea id="j-rec" placeholder="What went to plan, what didn't, one thing to repeat tomorrow">${esc(d.rec || '')}</textarea></div>
    <div style="display:flex;flex-wrap:wrap;gap:6px 26px">${score('mood', 'Mood')}${score('sleep', 'Sleep')}${score('focus', 'Focus')}</div>
    <button class="btn primary" id="j-save">Save journal</button>`}</div>
    <div class="panel"><h2>${fmtDate(S.jDay)} result</h2>
      <div class="kv"><span>Net P&L</span><b class="${cls(St.net)}">${money(St.net)}</b></div><div class="kv"><span>Trades closed</span><b>${St.n}</b></div><div class="kv"><span>Trades opened</span><b>${ts.filter(t => t.date === S.jDay).length}</b></div>
      <div class="kv"><span>Win rate</span><b>${(St.wr * 100).toFixed(0)}%</b></div><div class="kv"><span>Mistake tags</span><b>${ts.reduce((s, t) => s + t.tags.filter(x => x !== 'Early exit').length, 0)}</b></div></div></section>
  <section><h2>Trades that day</h2><p class="muted" style="margin:-6px 0 10px;font-size:.9rem">Every trade opened or closed on this day. Net P&L counts the trades closed on this day.</p>${dayTable(ts)}</section>`;
}
function dayTable(ts) {
  if (!ts.length) return `<div class="tablewrap"><p class="empty">No trades opened or closed on this day.</p></div>`;
  const day = S.jDay, ev = t => { const o = t.date === day, c = t.closeTs && cdate(t) === day; return o && c ? 'Opened and closed' : o ? (t.status === 'open' ? 'Opened, still open' : `Opened, closed ${fmtDate(cdate(t))}`) : `Closed (opened ${fmtDate(t.date)})`; };
  const when = t => t.date === day ? t.time : t.closeTs.slice(11, 16);
  ts = ts.slice().sort((a, b) => when(b).localeCompare(when(a)));
  return `<div class="tablewrap"><table><thead><tr><th>Time</th><th>Ticker / contract</th><th>Side</th><th>What happened</th><th>Setup</th><th>Tags</th><th class="r">Net P&L</th></tr></thead><tbody>
  ${ts.map(t => `<tr data-open="${t.id}" tabindex="0"><td>${when(t)}</td><td><b>${esc(t.underlying || t.sym)}</b>${t.assetType === 'option' ? ` <span class="muted">${esc(fmtExp(t.expiry))} ${t.strike} ${t.optType}</span>` : ''}</td><td>${t.dir}</td><td>${ev(t)}</td><td>${esc(t.setup) || '<span class="muted">—</span>'}</td><td>${t.tags.map(x => `<span class="chip ${x === 'Early exit' ? '' : 'bad'}">${esc(x)}</span>`).join('')}</td><td class="r ${t.closeTs && cdate(t) === day ? cls(t.net) : ''}"><b>${t.status === 'open' ? '<span class="muted">open</span>' : t.closeTs && cdate(t) === day ? money(t.net) : `<span class="muted">${money(t.net)} later</span>`}</b></td></tr>`).join('')}
  </tbody></table></div>`;
}
async function afterJournal() {
  const day = S.jDay; if (S.daily[day]) return;
  try { S.daily[day] = await api(`/daily/${day}`); } catch (e) { S.daily[day] = {}; toast(e.message); }
  if (route() === 'journal' && S.jDay === day) render();
}
function readDaily() { const d = S.daily[S.jDay] = S.daily[S.jDay] || {}; if ($('#j-pre')) { d.pre = $('#j-pre').value; d.rec = $('#j-rec').value; } return d; }

/* ---------- import ---------- */
function vImport() {
  const accts = S.me.accounts;
  return head('Import trades', 'Upload fills from any broker as CSV. They are grouped into trades automatically, and re-uploading the same file never creates duplicates.', '') + `
  <section class="panel"><div class="form-grid"><div class="field"><label for="imp-acct">Account name</label><input id="imp-acct" type="text" list="acct-list" value="${esc(accts[0] || 'Main')}" placeholder="e.g. Topstep 50K, IBKR cash"><datalist id="acct-list">${accts.map(a => `<option value="${esc(a)}">`).join('')}</datalist></div>
    <div class="field"><label for="imp-tz">Times in the file are</label><select id="imp-tz"><option value="auto">Detect automatically</option><option value="ET">Eastern</option><option value="CT">Central</option><option value="MT">Mountain</option><option value="PT">Pacific</option></select></div></div>
    <div class="drop" id="drop"><p style="margin:0 0 10px;font-weight:600">Drop a CSV file here</p>
      <p class="muted" style="margin:0 0 14px">Schwab / thinkorswim account statements and IBKR Flex trade exports work as-is. Any other CSV needs columns for time, symbol, side, quantity and price.</p>
      <label class="btn primary" style="display:inline-block">Choose file<input type="file" id="file" accept=".csv,text/csv" hidden></label></div>
    <p id="imp-status" class="status" style="margin:12px 0 0"></p></section>
  <section><h2>Recent imports</h2><div id="imports">${importsTable()}</div></section>`;
}
function importsTable() {
  if (!S.imports) return '<p class="loading">Loading…</p>';
  if (!S.imports.length) return '<div class="tablewrap"><p class="empty">No imports yet.</p></div>';
  return `<div class="tablewrap"><table><thead><tr><th>File</th><th>Account</th><th>Status</th><th class="r">Fills</th><th class="r">New</th><th class="r">Duplicates</th><th class="r">Skipped rows</th><th class="r">Unmatched closes</th><th>When</th></tr></thead><tbody>
  ${S.imports.map(i => `<tr><td>${esc(i.fileName)}</td><td>${esc(i.account)}</td><td><span class="status ${esc(i.status)}">${esc(i.status)}</span>${i.error ? `<div class="loss" style="white-space:normal;max-width:360px">${esc(i.error)}</div>` : ''}</td><td class="r">${i.fills ?? ''}</td><td class="r">${i.newFills ?? ''}</td><td class="r">${i.duplicates ?? ''}</td><td class="r">${i.skippedRows ?? ''}</td><td class="r">${i.unmatchedCloses ?? ''}</td><td>${esc((i.createdAt || '').replace('T', ' ').slice(0, 16))}</td></tr>`).join('')}</tbody></table></div>`;
}
async function afterImport() { try { S.imports = (await api('/imports')).imports; } catch (e) { S.imports = []; toast(e.message); } if (route() === 'import') $('#imports').innerHTML = importsTable(); }
async function upload(file) {
  const st = $('#imp-status'); const account = ($('#imp-acct').value || 'Main').trim();
  if (!/\.csv$/i.test(file.name) && file.type !== 'text/csv') { st.className = 'status error'; st.textContent = 'Choose a .csv file.'; return; }
  if (file.size > 20 * 1024 * 1024) { st.className = 'status error'; st.textContent = 'The file is larger than 20 MB. Split it and upload the parts.'; return; }
  st.className = 'status processing'; st.textContent = `Uploading ${file.name}…`;
  try {
    const { importId, uploadUrl } = await api('/imports', { method: 'POST', body: { fileName: file.name, account, tz: $('#imp-tz').value } });
    const r = await fetch(uploadUrl, { method: 'PUT', headers: { 'Content-Type': 'text/csv' }, body: file });
    if (!r.ok) throw new Error(`Upload failed (${r.status}).`);
    st.textContent = 'Processing fills…';
    for (let i = 0; i < 90; i++) {
      await sleep(2000);
      const list = (await api('/imports')).imports; S.imports = list; if (route() === 'import') $('#imports').innerHTML = importsTable();
      const it = list.find(x => x.id === importId);
      if (it && it.status === 'done') { st.className = 'status done'; st.textContent = `Imported ${it.newFills} new fills (${it.duplicates} already in your journal). You now have ${it.trades} trades.` + (it.unmatchedCloses ? ` ${it.unmatchedCloses} closing fills were left out because their opening fill is older than your imported history. Import an earlier statement to include them.` : ''); await reload(); return; }
      if (it && it.status === 'error') { st.className = 'status error'; st.textContent = it.error; return; }
    }
    st.textContent = 'Still processing. Check the list below in a minute.';
  } catch (e) { st.className = 'status error'; st.textContent = e.message; }
}

/* ---------- settings ---------- */
function vSettings() {
  const s = S.me.settings, P = s.prop, b = S.broker;
  const brokerHtml = b === undefined ? '<p class="loading">Loading…</p>' : b.connected ? `
    <div class="kv"><span>Status</span><b><span class="status ${esc(b.status)}">${esc(b.status)}</span></b></div>
    <div class="kv"><span>Environment</span><b>${esc(b.env)} (key ending ${esc(b.keyHint)})</b></div>
    <div class="kv"><span>Last sync</span><b>${esc((b.lastSync || 'never').replace('T', ' ').slice(0, 16))}${b.lastResult ? ', ' + esc(b.lastResult) : ''}</b></div>
    ${b.error ? `<p class="loss">${esc(b.error)}</p>` : ''}
    <p><button class="btn primary" id="al-sync">Sync now</button> <button class="btn" id="al-del">Disconnect</button></p>` : `
    <p class="muted" style="margin:0 0 10px">Not connected.${S.showAlpaca ? '' : ' <button class="linkbtn" id="al-show">Connect Alpaca</button>'}</p>${!S.showAlpaca ? '' : `
    <p class="muted">Use read-only keys. Fills sync every 15 minutes during market hours, and stock charts load from Alpaca market data. Keys are encrypted and never shown again.</p>
    <div class="form-grid"><div class="field"><label for="al-key">API key ID</label><input id="al-key" type="text" autocomplete="off"></div>
      <div class="field"><label for="al-secret">Secret key</label><input id="al-secret" type="password" autocomplete="off"></div>
      <div class="field"><label for="al-env">Account</label><select id="al-env"><option value="paper">Paper</option><option value="live">Live</option></select></div>
      <div class="field"><label for="al-feed">Market data</label><select id="al-feed"><option value="iex">IEX (free)</option><option value="sip">SIP (paid plan)</option></select></div></div>
    <button class="btn primary" id="al-save">Connect Alpaca</button>`}`;
  return head('Settings', esc(Auth.email()), `<button class="btn" id="signout">Sign out</button>`) + `
  <section class="panel"><h2>Trading plan</h2><div class="form-grid">
    <div class="field"><label for="s-risk">Planned risk per trade ($)</label><input id="s-risk" type="number" min="0" step="any" value="${s.riskPerTrade}"></div></div>
    <div class="field"><label for="s-setups">Your setups, one per line</label><textarea id="s-setups">${esc((s.setups || []).join('\n'))}</textarea></div>
  </section>
  <section class="panel"><h2>Prop challenge</h2>
    <div class="field"><label><input type="checkbox" id="pp-on" ${P.enabled ? 'checked' : ''}> Track a prop challenge on the home page</label></div>
    <div class="form-grid">
      <div class="field"><label for="pp-acct">Account</label><select id="pp-acct"><option value="">Choose an account</option>${S.me.accounts.map(a => `<option ${P.account === a ? 'selected' : ''}>${esc(a)}</option>`).join('')}</select></div>
      <div class="field"><label for="pp-start">Start date</label><input id="pp-start" type="date" value="${esc(P.start)}"></div>
      <div class="field"><label for="pp-bal">Starting balance</label><input id="pp-bal" type="number" step="any" value="${P.balance}"></div>
      <div class="field"><label for="pp-trail">Trailing drawdown</label><input id="pp-trail" type="number" step="any" value="${P.trailing}"></div>
      <div class="field"><label for="pp-target">Profit target</label><input id="pp-target" type="number" step="any" value="${P.target}"></div>
      <div class="field"><label for="pp-dll">Daily loss limit</label><input id="pp-dll" type="number" step="any" value="${P.dailyLoss}"></div></div>
    <button class="btn primary" id="s-save">Save settings</button>
  </section>
  <section class="panel"><h2>Brokers</h2>
    <p class="muted" style="margin-top:-4px">Connected brokers sync your fills automatically. Each broker account shows up as its own account in the journal, and every page can show one account or all of them together.</p>
    <div class="broker-card"><h3>Charles Schwab</h3>${schwabHtml()}</div>
    <div class="broker-card"><h3>Alpaca</h3><div id="broker">${brokerHtml}</div></div>
    <div class="broker-card"><h3>Other brokers</h3><p class="muted" style="margin:0">Interactive Brokers, Tradovate, NinjaTrader, TradeStation, Webull, Robinhood and others: import a CSV of your fills on the <a href="#import">Import</a> page. Direct connections are added one broker at a time.</p></div>
  </section>`;
}
function schwabHtml() {
  const b = S.schwab;
  if (b === undefined) return '<p class="loading">Loading…</p>';
  if (!b.configured) return `<p class="muted">The Schwab connection isn't set up on the server yet. Add your Schwab developer app key and secret as the GitHub secrets <code>SCHWAB_APP_KEY</code> and <code>SCHWAB_APP_SECRET</code>, register <code>${esc(b.callback)}</code> as the app's callback URL, and redeploy.</p>`;
  const accts = S.me.accounts;
  const nameField = `<div class="form-grid"><div class="field"><label for="sch-name">Journal account name</label><input id="sch-name" type="text" list="sch-acct-list" value="${esc(b.journalAccount || accts[0] || 'Schwab')}"><datalist id="sch-acct-list">${accts.map(a => `<option value="${esc(a)}">`).join('')}</datalist></div></div>
    <p class="muted" style="margin-top:0;font-size:.9rem">Use the same name as your imported Schwab statements. The first sync then starts right after your last imported fill, so nothing is counted twice.</p>`;
  if (!b.connected) return `<p class="muted">Connect with your Schwab login. Access is read-only in this app: it only reads your trade history. New fills sync every 15 minutes during market hours.</p>${nameField}<button class="btn primary" id="sch-connect">Connect Schwab</button>`;
  const nyNow = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const daysLeft = b.refreshExpires ? (new Date(b.refreshExpires) - nyNow) / 864e5 : null;
  return `<div class="kv"><span>Status</span><b><span class="status ${esc(b.status === 'expired' ? 'error' : b.status)}">${esc(b.status === 'expired' ? 'login expired' : b.status)}</span></b></div>
    <div class="kv"><span>Accounts</span><b>${(b.accounts || []).map(a => `${esc(a.name)} (••${esc(a.last4)})`).join(', ')}</b></div>
    <div class="kv"><span>Last sync</span><b>${esc((b.lastSync || 'never').replace('T', ' ').slice(0, 16))}${b.lastResult ? ', ' + esc(b.lastResult) : ''}</b></div>
    <div class="kv"><span>Schwab login valid until</span><b class="${daysLeft != null && daysLeft < 2 ? 'loss' : ''}">${esc((b.refreshExpires || '').replace('T', ' ').slice(0, 16))} ET</b></div>
    ${b.error ? `<p class="loss">${esc(b.error)}</p>` : ''}
    <p class="muted" style="font-size:.9rem">Schwab requires logging in again every 7 days. Click Reconnect before the date above to keep syncing.</p>
    <p><button class="btn primary" id="sch-sync">Sync now</button> <button class="btn" id="sch-reconnect">Reconnect</button> <button class="btn" id="sch-del">Disconnect</button></p>`;
}
async function afterSettings() {
  if (S.schwab === undefined) { try { S.schwab = await api('/broker/schwab'); } catch (e) { S.schwab = { configured: false, callback: '' }; } if (route() === 'settings') render(); }
  if (S.broker !== undefined) return; try { S.broker = await api('/broker/alpaca'); } catch (e) { S.broker = { connected: false }; toast(e.message); } if (route() === 'settings') render(); }
async function saveSettings() {
  const body = { riskPerTrade: $('#s-risk').value, setups: $('#s-setups').value.split('\n'),
    prop: { enabled: $('#pp-on').checked, account: $('#pp-acct').value, start: $('#pp-start').value, balance: $('#pp-bal').value, trailing: $('#pp-trail').value, target: $('#pp-target').value, dailyLoss: $('#pp-dll').value } };
  try { S.me.settings = await api('/settings', { method: 'PUT', body }); toast('Settings saved'); } catch (e) { toast(e.message); }
}
async function pollSchwab() {
  for (let i = 0; i < 60; i++) { await sleep(3000); try { S.schwab = await api('/broker/schwab'); } catch (e) { break; } if (route() === 'settings') render(); if (S.schwab.status !== 'syncing') { await reload(); toast(S.schwab.status === 'connected' ? `Schwab sync finished: ${S.schwab.lastResult}` : 'Schwab sync stopped. See Settings.'); return; } }
}
async function schwabConnect() {
  try { const r = await api('/broker/schwab/authorize', { method: 'POST', body: { journalAccount: ($('#sch-name')?.value || S.schwab.journalAccount || 'Schwab').trim() } }); location.assign(r.url); }
  catch (e) { toast(e.message); }
}
async function pollBroker() {
  for (let i = 0; i < 30; i++) { await sleep(3000); try { S.broker = await api('/broker/alpaca'); } catch (e) { break; } if (route() === 'settings') render(); if (S.broker.status !== 'syncing') { await reload(); toast('Alpaca sync finished'); return; } }
}

/* ---------- router & events ---------- */
const VIEWS = { dashboard: [vDashboard], trades: [vTrades], analytics: [vAnalytics], coach: [vCoach, afterCoach], journal: [vJournal, afterJournal], import: [vImport, afterImport], settings: [vSettings, afterSettings] };
const route = () => { const r = location.hash.slice(1) || 'dashboard'; return VIEWS[r] ? r : 'dashboard'; };
function render() {
  const v = route();
  document.querySelectorAll('.nav a').forEach(a => { if (a.dataset.r === v) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  try { $('#view').innerHTML = VIEWS[v][0](); } catch (e) { console.error(e); $('#view').innerHTML = `<div class="errbox">This page failed to load: ${esc(e.message)}</div>`; }
  if (VIEWS[v][1]) VIEWS[v][1]();
}
async function reload() { const [me, tr] = await Promise.all([api('/me'), api('/trades')]); S.me = me; S.trades = tr.trades.sort(chron); if (!cur) render(); }
window.addEventListener('hashchange', () => { render(); window.scrollTo(0, 0); });
document.addEventListener('click', e => {
  const el = e.target.closest('[data-range],[data-open],[data-tf],[data-tag],[data-emo],[data-wi],[data-bd],[data-ask],[data-score],#dr-close,#scrim,#rp-play,#save-plan,#rv-run,#j-save,#rep-run,#s-save,#al-save,#al-sync,#al-del,#sch-connect,#sch-reconnect,#sch-sync,#sch-del,#al-show,#signout');
  if (!el) return;
  if (el.dataset.range) { S.range = el.dataset.range; render(); return; }
  if (el.dataset.open && !el.closest('#drawer')) { openTrade(el.dataset.open); return; }
  if (el.dataset.tf) { S.tf = el.dataset.tf; replay.k = Infinity; stopReplay(); document.querySelectorAll('[data-tf]').forEach(b => b.setAttribute('aria-pressed', b.dataset.tf === S.tf)); $('#chart').innerHTML = '<p class="loading">Loading chart…</p>'; loadBars(); return; }
  if (el.dataset.tag) { const m = el.dataset.tag; patch({ tags: cur.tags.includes(m) ? cur.tags.filter(x => x !== m) : [...cur.tags, m] }, 'Tags saved'); return; }
  if (el.dataset.emo) { patch({ emotion: cur.emotion === el.dataset.emo ? '' : el.dataset.emo }, 'Emotion saved'); return; }
  if (el.dataset.wi) { S.wi[el.dataset.wi] = !S.wi[el.dataset.wi]; render(); return; }
  if (el.dataset.bd) { S.bd = el.dataset.bd; render(); return; }
  if (el.dataset.ask) { ask(el.dataset.ask); return; }
  if (el.dataset.score) { const d = readDaily(); d[el.dataset.score] = +el.dataset.v; render(); return; }
  switch (el.id) {
    case 'dr-close': case 'scrim': closeTrade(); return;
    case 'rp-play': replay.timer ? stopReplay() : startReplay(); return;
    case 'rv-run': runReview(); return;
    case 'save-plan': patch({ setup: $('#p-setup').value, notes: $('#note').value, plan: { entry: $('#p-entry').value, stop: $('#p-stop').value, target: $('#p-target').value, thesis: $('#p-thesis').value } }, 'Plan and notes saved'); return;
    case 'j-save': { const d = readDaily(); api(`/daily/${S.jDay}`, { method: 'PUT', body: d }).then(() => toast('Journal saved')).catch(err => toast(err.message)); return; }
    case 'rep-run': writeReport(); return;
    case 's-save': saveSettings(); return;
    case 'al-save': el.disabled = true; api('/broker/alpaca', { method: 'PUT', body: { key: $('#al-key').value, secret: $('#al-secret').value, env: $('#al-env').value, feed: $('#al-feed').value } })
      .then(b => { S.broker = b; render(); toast('Alpaca connected. Syncing your fills…'); pollBroker(); }).catch(err => { el.disabled = false; toast(err.message); }); return;
    case 'al-sync': api('/broker/alpaca/sync', { method: 'POST' }).then(() => { S.broker.status = 'syncing'; render(); pollBroker(); }).catch(err => toast(err.message)); return;
    case 'al-del': if (confirm('Disconnect Alpaca? Your imported trades stay in the journal.')) api('/broker/alpaca', { method: 'DELETE' }).then(b => { S.broker = b; render(); }).catch(err => toast(err.message)); return;
    case 'sch-connect': case 'sch-reconnect': schwabConnect(); return;
    case 'sch-sync': api('/broker/schwab/sync', { method: 'POST' }).then(() => { S.schwab.status = 'syncing'; render(); pollSchwab(); }).catch(err => toast(err.message)); return;
    case 'sch-del': if (confirm('Disconnect Schwab? Trades already synced stay in the journal.')) api('/broker/schwab', { method: 'DELETE' }).then(b => { S.schwab = b; render(); }).catch(err => toast(err.message)); return;
    case 'al-show': S.showAlpaca = true; render(); return;
    case 'signout': Auth.logout(); return;
  }
});
document.addEventListener('input', e => {
  if (e.target.id === 'f-q') { tf.q = e.target.value; $('#trade-table').innerHTML = tradeTable(filtered()); }
  if (e.target.id === 'rp') { stopReplay(); replay.k = +e.target.value; drawChart(); }
});
document.addEventListener('change', e => {
  const id = e.target.id;
  if (['f-setup', 'f-tag', 'f-res', 'f-acct', 'f-asset'].includes(id)) { tf[id.slice(2)] = e.target.value; $('#trade-table').innerHTML = tradeTable(filtered()); }
  if (id === 'jday') { S.jDay = e.target.value; render(); }
  if (id === 'g-acct') { S.acct = e.target.value; render(); }
  if (id === 'file' && e.target.files[0]) upload(e.target.files[0]);
});
document.addEventListener('submit', e => { if (e.target.id === 'ask') { e.preventDefault(); ask($('#askq').value); } });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('#drawer').classList.contains('open')) closeTrade();
  if (e.key === 'Enter' && e.target.matches('tr[data-open]')) openTrade(e.target.dataset.open);
});
document.addEventListener('pointermove', e => { if (e.target.closest && e.target.closest('#eq-svg')) eqHover(e); else if (S.eq && $('#eq-tip') && $('#eq-tip').style.display !== 'none') eqLeave(); });
document.addEventListener('dragover', e => { const d = e.target.closest('#drop'); if (d) { e.preventDefault(); d.classList.add('over'); } });
document.addEventListener('dragleave', e => { const d = e.target.closest('#drop'); if (d) d.classList.remove('over'); });
document.addEventListener('drop', e => { const d = e.target.closest('#drop'); if (!d) return; e.preventDefault(); d.classList.remove('over'); const f = e.dataTransfer.files[0]; if (f) upload(f); });

/* ---------- boot ---------- */
function showLogin(msg) { $('#app').hidden = true; $('#login').hidden = false; if (msg) { $('#login-err').hidden = false; $('#login-err').textContent = msg; } }
$('#signin').addEventListener('click', () => Auth.login());
(async function boot() {
  if (!C || /REPLACE/.test(C.clientId)) { showLogin('This copy isn’t configured yet. Run deploy.sh (or deploy.ps1) to generate config.js.'); $('#signin').disabled = true; return; }
  try { await Auth.handleCallback(); } catch (e) { showLogin(e.message); return; }
  if (!(await Auth.token())) { showLogin(); return; }
  $('#login').hidden = true; $('#app').hidden = false; $('#who').textContent = Auth.email();
  if (location.pathname === '/schwab') {
    const p = new URLSearchParams(location.search);
    history.replaceState({}, '', '/#settings');
    try {
      if (p.get('error') || !p.get('code')) throw new Error('Schwab login was cancelled or failed' + (p.get('error_description') ? ': ' + p.get('error_description') : '.'));
      S.schwab = await api('/broker/schwab/callback', { method: 'POST', body: { code: p.get('code'), state: p.get('state') } });
      toast('Schwab connected. Syncing your trades…'); pollSchwab();
    } catch (e) { toast(e.message); }
  }
  try { await reload(); } catch (e) { $('#view').innerHTML = `<div class="errbox">${esc(e.message)}</div>`; }
})();
