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
  if (!r.ok) { const e = new Error(data.error || (r.status >= 500 ? `The server took too long or failed (${r.status}) — try a shorter period (45d / 90d) and reload.` : `Request failed (${r.status}).`)); e.status = r.status; throw e; }
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
const isPaper = a => /paper/i.test(a || '');
// Account scope for every page: real accounts by default, paper bot accounts only when chosen.
const inScope = t => { const a = S.acct || '__real'; return a === '__all' ? true : a === '__real' ? !isPaper(t.acct) : a === '__paper' ? isPaper(t.acct) : t.acct === a; };
function scoped() {
  const all = S.trades.filter(inScope);
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
  const accts = [...new Set(S.trades.map(t => t.acct))].sort(), cur = S.acct || '__real', hasPaper = accts.some(isPaper);
  const opt = (v, l) => `<option value="${esc(v)}" ${cur === v ? 'selected' : ''}>${esc(l)}</option>`;
  const acctSel = accts.length > 1 || hasPaper ? `<select id="g-acct" aria-label="Account" style="background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:6px 10px">
    ${opt('__real', hasPaper ? 'Real accounts' : 'All accounts')}${hasPaper ? opt('__paper', 'Paper bot') + opt('__all', 'Everything (real + paper)') : ''}
    <optgroup label="One account">${accts.map(a => opt(a, a + (isPaper(a) ? ' (paper)' : ''))).join('')}</optgroup></select> ` : '';
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
const nyToday = () => new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
const addDays = (d, n) => new Date(Date.parse(d + 'T12:00:00Z') + n * 864e5).toISOString().slice(0, 10);
const monday = d => { const w = new Date(d + 'T12:00:00Z').getUTCDay(); return addDays(d, -((w + 6) % 7)); };
const MONTHS_LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
function acctClosed() { return closed(S.trades.filter(inScope)); }
function dayMap(ts) { const m = {}; for (const t of ts) { const d = cdate(t); (m[d] = m[d] || []).push(t); } return m; }
function periodCard(title, ts, sub) {
  if (!ts.length) return `<div class="period"><h3>${title}</h3><div class="big muted">$0</div><p>${sub}<br>No closed trades</p></div>`;
  const St = stats(ts), best = Math.max(...ts.map(t => t.net)), worst = Math.min(...ts.map(t => t.net));
  return `<div class="period"><h3>${title}</h3><div class="big ${cls(St.net)}">${money(St.net)}</div>
    <p>${sub}<br>${St.n} trade${St.n === 1 ? '' : 's'}, ${(St.wr * 100).toFixed(0)}% winners<br>Best ${money(best)}, worst ${money(worst)}</p></div>`;
}
function periods() {
  const all = acctClosed(), today = nyToday(), wk = monday(today), mo = today.slice(0, 7), yr = today.slice(0, 4);
  const lastDay = all.length ? all.map(cdate).sort().pop() : null;
  const dayLabel = lastDay && lastDay !== today ? `Last trading day` : 'Today';
  const dayDate = lastDay && lastDay !== today ? lastDay : today;
  return `<div class="periods">
    ${periodCard(dayLabel, all.filter(t => cdate(t) === dayDate), fmtDate(dayDate))}
    ${periodCard('This week', all.filter(t => cdate(t) >= wk), `Since Mon ${fmtDate(wk).replace(/, \d{4}$/, '')}`)}
    ${periodCard('This month', all.filter(t => cdate(t).slice(0, 7) === mo), MONTHS_LONG[+mo.slice(5) - 1] + ' ' + mo.slice(0, 4))}
    ${periodCard('This year', all.filter(t => cdate(t).slice(0, 4) === yr), yr)}</div>`;
}
function calendar() {
  const all = acctClosed(), dm = dayMap(all), today = nyToday();
  if (!S.calMonth) S.calMonth = (all.length ? all.map(cdate).sort().pop() : today).slice(0, 7);
  const [y, m] = S.calMonth.split('-').map(Number), first = `${S.calMonth}-01`;
  const last = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
  const monthTs = all.filter(t => cdate(t).slice(0, 7) === S.calMonth), mSt = stats(monthTs);
  const maxAbs = Math.max(1, ...Object.entries(dm).filter(([d]) => d.slice(0, 7) === S.calMonth).map(([, a]) => Math.abs(sum(a.map(t => t.net)))));
  let html = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Week'].map(d => `<div class="dow">${d}</div>`).join('');
  for (let wkStart = monday(first); wkStart <= last; wkStart = addDays(wkStart, 7)) {
    let wkNet = 0, wkN = 0;
    for (let i = 0; i < 7; i++) { const d = addDays(wkStart, i); if (d.slice(0, 7) === S.calMonth && dm[d]) { wkNet += sum(dm[d].map(t => t.net)); wkN += dm[d].length; } }
    for (let i = 0; i < 5; i++) {
      const d = addDays(wkStart, i), inMonth = d.slice(0, 7) === S.calMonth, ts = inMonth ? (dm[d] || []) : [];
      // weekend trades are folded into Friday's cell so nothing is hidden
      if (inMonth && i === 4) for (const x of [addDays(d, 1), addDays(d, 2)]) if (x.slice(0, 7) === S.calMonth && dm[x]) ts.push(...dm[x]);
      const net = sum(ts.map(t => t.net)), a = Math.min(1, Math.abs(net) / maxAbs);
      const bg = !ts.length ? '' : `background:color-mix(in srgb, ${net >= 0 ? 'var(--gain)' : 'var(--loss)'} ${Math.round(8 + a * 30)}%, var(--surface))`;
      html += inMonth ? `<button class="day${d === today ? ' today' : ''}" data-day="${d}" style="${bg}" aria-label="${fmtDate(d)}${ts.length ? ': ' + money(net) + ', ' + ts.length + ' trades' : ''}">
          <span class="d">${+d.slice(8)}</span>${ts.length ? `<span class="v ${cls(net)}">${money(net)}</span><span class="n">${ts.length} trade${ts.length === 1 ? '' : 's'}</span>` : ''}</button>`
        : `<div class="day out"><span class="d">${+d.slice(8)}</span></div>`;
    }
    html += `<div class="wk"><span class="d">Week</span>${wkN ? `<span class="v ${cls(wkNet)}">${money(wkNet)}</span><span class="n">${wkN} trade${wkN === 1 ? '' : 's'}</span>` : '<span class="n">—</span>'}</div>`;
  }
  return `<div class="cal-head"><div><h2>${MONTHS_LONG[m - 1]} ${y}</h2><span class="muted" style="font-size:.9rem">${monthTs.length ? `${money(mSt.net)} from ${mSt.n} trades, ${(mSt.wr * 100).toFixed(0)}% winners` : 'No closed trades this month'}</span></div>
    <div class="cal-nav"><button data-cal="-1" aria-label="Previous month">‹</button><button data-cal="0" style="width:auto;padding:0 10px;font-size:.85rem">Today</button><button data-cal="1" aria-label="Next month">›</button></div></div>
    <div class="cal">${html}</div><p class="muted" style="font-size:.82rem;margin:10px 0 0">P&L is counted on the day each trade closed. Click a day to open its journal.</p>`;
}
function pnlBars() {
  const all = acctClosed(), mode = S.barMode || 'day', today = nyToday();
  let keys = [], label;
  if (mode === 'day') { const ds = [...new Set(all.map(cdate))].sort(); keys = ds.slice(-30); label = k => fmtDate(k).replace(/, \d{4}$/, ''); }
  if (mode === 'week') { let w = monday(today); for (let i = 0; i < 12; i++) { keys.unshift(w); w = addDays(w, -7); } label = k => 'Wk of ' + fmtDate(k).replace(/, \d{4}$/, ''); }
  if (mode === 'month') { let [y, m] = today.split('-').map(Number); for (let i = 0; i < 12; i++) { keys.unshift(`${y}-${String(m).padStart(2, '0')}`); if (--m === 0) { m = 12; y--; } } label = k => MONTHS_LONG[+k.slice(5) - 1].slice(0, 3) + " '" + k.slice(2, 4); }
  const keyOf = t => mode === 'day' ? cdate(t) : mode === 'week' ? monday(cdate(t)) : cdate(t).slice(0, 7);
  const agg = {}; for (const t of all) { const k = keyOf(t); if (!agg[k]) agg[k] = { net: 0, n: 0, w: 0 }; agg[k].net += t.net; agg[k].n++; if (t.net > 0) agg[k].w++; }
  const vals = keys.map(k => (agg[k] || { net: 0, n: 0, w: 0 }));
  const seg = `<div class="seg" role="group" aria-label="Group P&L by">${[['day', 'Daily'], ['week', 'Weekly'], ['month', 'Monthly']].map(([k, l]) => `<button data-bars="${k}" aria-pressed="${mode === k}">${l}</button>`).join('')}</div>`;
  if (!keys.length) return `<div class="cal-head"><h2>P&L by period</h2>${seg}</div><p class="empty">No closed trades yet.</p>`;
  const W = 720, H = 220, pl = 8, pr = 8, pt = 14, pb = 30, mx = Math.max(1, ...vals.map(v => Math.abs(v.net)));
  const hasNeg = vals.some(v => v.net < 0), hasPos = vals.some(v => v.net > 0);
  const zeroY = hasNeg && hasPos ? pt + (H - pt - pb) / 2 : hasNeg ? pt : H - pb;
  const scale = (hasNeg && hasPos ? (H - pt - pb) / 2 : (H - pt - pb)) / mx;
  const bw = (W - pl - pr) / keys.length, step = Math.max(1, Math.ceil(keys.length / 8));
  const bars = vals.map((v, i) => { const h = Math.abs(v.net) * scale, x = pl + i * bw + bw * .15, y = v.net >= 0 ? zeroY - h : zeroY;
    return `<rect x="${x}" y="${y}" width="${bw * .7}" height="${Math.max(v.n ? 1.5 : 0, h)}" rx="2" fill="${v.net >= 0 ? 'var(--gain)' : 'var(--loss)'}"><title>${label(keys[i])}: ${money(v.net)}, ${v.n} trades${v.n ? `, ${Math.round(v.w / v.n * 100)}% winners` : ''}</title></rect>`
      + (i % step === 0 ? `<text x="${pl + i * bw + bw / 2}" y="${H - 10}" font-size="10.5" text-anchor="middle" fill="var(--muted)">${label(keys[i])}</text>` : ''); }).join('');
  const tot = sum(vals.map(v => v.net)), pos = vals.filter(v => v.n && v.net > 0).length, act = vals.filter(v => v.n).length;
  return `<div class="cal-head"><div><h2>P&L by period</h2><span class="muted" style="font-size:.9rem">${money(tot)} total, ${pos} of ${act} ${mode === 'day' ? 'trading days' : mode === 'week' ? 'weeks' : 'months'} green</span></div>${seg}</div>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Net P&L per ${mode}" style="width:100%;height:auto"><line x1="${pl}" x2="${W - pr}" y1="${zeroY}" y2="${zeroY}" stroke="var(--line)"/>${bars}</svg>`;
}
function vDashboard() {
  const ts = closed(scoped());
  if (!S.trades.length) return head('Overview', '', '') + botHome() + emptyState();
  const St = stats(ts);
  const n = S.notes && S.notes[0];
  return head('Overview', periodText(ts)) + `
  ${n ? `<section class="coach" style="padding:14px 18px">${noteHtml(n, true)}<a href="#coach" class="linkbtn">Open the full check</a></section>` : ''}
  ${botHome()}
  <section>${periods()}</section>
  <section class="grid2"><div class="panel">${calendar()}</div><div class="panel"><h2>Equity curve</h2>${equity(ts)}</div></section>
  <section class="panel">${pnlBars()}</section>
  <section>${strip(St)}</section>`;
}
function strip(St) {
  const it = [['Net P&L', `<span class="${cls(St.net)}">${money(St.net)}</span>`], ['Win rate', (St.wr * 100).toFixed(0) + '%'], ['Profit factor', St.pf === Infinity ? '∞' : St.pf.toFixed(2)],
    ['Expectancy', St.exp != null ? `<span class="${cls(St.exp)}">${fr(St.exp)}</span>` : `<span class="${cls(St.net)}">${money(St.n ? St.net / St.n : 0)}</span><small class="muted" style="font-size:.72rem;font-weight:400"> per trade</small>`], ['Max drawdown', `<span class="loss">${money(St.dd)}</span>`], ['Sharpe', St.sharpe.toFixed(2)]];
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
const tf = { q: '', setup: '', tag: '', res: '', acct: '', asset: '', dir: '' };
function vTrades() {
  const ts = scoped();
  if (!S.trades.length) return head('Trades', '', '') + emptyState();
  const setups = [...new Set(S.trades.map(t => t.setup).filter(Boolean))], accts = [...new Set(S.trades.map(t => t.acct))];
  return head('Trades', periodText(ts)) + `<div class="filters">
    <input id="f-q" type="search" placeholder="Symbol" value="${esc(tf.q)}" aria-label="Filter by symbol" size="10">
    ${accts.length > 1 ? `<select id="f-acct" aria-label="Account"><option value="">All accounts</option>${accts.map(s => `<option ${tf.acct === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>` : ''}
    <select id="f-asset" aria-label="Stock or option"><option value="">Stocks and options</option>${['stock', 'option', 'future'].filter(a => S.trades.some(t => t.assetType === a)).map(a => `<option value="${a}" ${tf.asset === a ? 'selected' : ''}>${a[0].toUpperCase() + a.slice(1)}s</option>`).join('')}</select>
    <select id="f-dir" aria-label="Long or short"><option value="">Long and short</option><option value="Long" ${tf.dir === 'Long' ? 'selected' : ''}>Long</option><option value="Short" ${tf.dir === 'Short' ? 'selected' : ''}>Short</option></select>
    <select id="f-setup" aria-label="Setup"><option value="">All setups</option>${setups.map(s => `<option ${tf.setup === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>
    <select id="f-tag" aria-label="Mistake"><option value="">Any tags</option><option value="__none" ${tf.tag === '__none' ? 'selected' : ''}>No tags</option>${S.me.mistakes.map(s => `<option ${tf.tag === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
    <select id="f-res" aria-label="Result"><option value="">Winners and losers</option><option value="w" ${tf.res === 'w' ? 'selected' : ''}>Winners</option><option value="l" ${tf.res === 'l' ? 'selected' : ''}>Losers</option><option value="o" ${tf.res === 'o' ? 'selected' : ''}>Open</option></select>
  </div><div id="trade-table">${tradeTable(filtered())}</div>`;
}
function filtered() {
  return scoped().filter(t => (!tf.q || t.sym.toLowerCase().includes(tf.q.toLowerCase()) || (t.underlying || '').toLowerCase() === tf.q.toLowerCase()) && (!tf.asset || t.assetType === tf.asset) && (!tf.dir || t.dir === tf.dir) && (!tf.acct || t.acct === tf.acct) && (!tf.setup || t.setup === tf.setup)
    && (!tf.tag || (tf.tag === '__none' ? !t.tags.length : t.tags.includes(tf.tag)))
    && (!tf.res || (tf.res === 'o' ? t.status === 'open' : t.status === 'closed' && (tf.res === 'w' ? t.net > 0 : t.net <= 0)))).slice();
}
const SORTS = {
  date: t => t.openTs, time: t => t.time, ticker: t => (t.underlying || t.sym) + ' ' + (t.expiry || '') + ' ' + String(t.strike ?? '').padStart(10, '0'),
  side: t => t.dir, setup: t => t.setup || null, tags: t => (t.tags.length + (t.emotion ? 1 : 0)) || null, hold: t => t.hold, r: t => t.r, net: t => t.status === 'open' ? null : t.net
};
const SORT_FIRST_DESC = new Set(['date', 'time', 'net', 'r', 'hold', 'tags']);
function sortTrades(ts) {
  const { key, dir } = S.sort || { key: 'date', dir: 'desc' }, f = SORTS[key] || SORTS.date, m = dir === 'asc' ? 1 : -1;
  return ts.slice().sort((a, b) => {
    const x = f(a), y = f(b);
    if (x == null && y == null) return b.openTs.localeCompare(a.openTs);
    if (x == null) return 1;          // empty values always go last
    if (y == null) return -1;
    const c = typeof x === 'number' ? x - y : String(x).localeCompare(String(y));
    return c ? c * m : b.openTs.localeCompare(a.openTs);
  });
}
const fmtHold = m => m == null ? '—' : m < 60 ? `${m} min` : m < 1440 ? `${Math.floor(m / 60)} h ${m % 60 ? (m % 60) + ' min' : ''}`.trim() : `${Math.round(m / 1440)} d`;
function tradeTable(ts, compact) {
  if (!ts.length) return `<div class="tablewrap"><p class="empty">No trades match these filters.</p></div>`;
  const cur_ = S.sort || { key: 'date', dir: 'desc' };
  const th = (key, label, right) => compact ? `<th${right ? ' class="r"' : ''}>${label}</th>`
    : `<th class="sortable${right ? ' r' : ''}" aria-sort="${cur_.key === key ? (cur_.dir === 'asc' ? 'ascending' : 'descending') : 'none'}"><button data-sort="${key}">${label}<span class="arrow">${cur_.key === key ? (cur_.dir === 'asc' ? '▲' : '▼') : '↕'}</span></button></th>`;
  if (!compact) ts = sortTrades(ts);
  return `<div class="tablewrap"><table><thead><tr>${th('date', 'Date')}${th('time', 'Time')}${th('ticker', 'Ticker / contract')}${th('side', 'Side')}${compact ? '' : th('setup', 'Setup') + th('tags', 'Tags') + th('hold', 'Held', true)}${th('r', 'R', true)}${th('net', 'Net P&L', true)}</tr></thead><tbody>
  ${ts.map(t => `<tr data-open="${t.id}" tabindex="0"><td>${fmtDate(t.date)}</td><td>${t.time}</td><td><b>${esc(t.underlying || t.sym)}</b>${t.assetType === 'option' ? ` <span class="muted">${esc(fmtExp(t.expiry))} ${t.strike} ${t.optType}</span>` : ''}</td><td>${t.dir}${t.status === 'open' ? ' <span class="chip">Open</span>' : ''}</td>${compact ? '' : `<td>${esc(t.setup) || '<span class="muted">—</span>'}</td><td>${t.tags.map(x => `<span class="chip ${x === 'Early exit' ? '' : 'bad'}">${esc(x)}</span>`).join('')}${t.emotion ? `<span class="chip emo">${esc(t.emotion)}</span>` : ''}</td><td class="r">${fmtHold(t.hold)}</td>`}<td class="r ${cls(t.r)}">${fr(t.r)}</td><td class="r ${cls(t.net)}"><b>${t.status === 'open' ? '<span class="muted">open</span>' : money(t.net)}</b></td></tr>`).join('')}
  </tbody></table></div>`;
}

/* ---------- trade drawer ---------- */
let cur = null; const replay = { k: Infinity, timer: null };
function openTrade(id) {
  cur = byId(id); if (!cur) return;
  S.tf = cur.hold == null || cur.hold > 2 * 1440 ? '1d' : cur.hold > 360 ? '1h' : '5m';
  if (!cur.ctx || cur.ctx.v !== 2) setTimeout(() => cur && cur.id === id && tradeContext(id, true), 0);
  if (cur.status === 'closed' && (!cur.q || cur.q.v !== 1)) setTimeout(() => cur && cur.id === id && tradeQuality(id, true), 0);
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
    ${t.status === 'open' ? `<div class="coach" id="entry-note"><p class="loading" style="padding:0">Loading entry check…</p></div>` : ''}
    <div class="coach review" id="review"><p class="loading" style="padding:0">Loading coach review…</p></div>
    ${t.status === 'closed' ? `<div class="panel" id="q-panel">${qPanel(t)}</div>` : ''}
    <div class="panel" id="ctx-panel">${ctxPanel(t)}</div>
    <div class="panel"><h2>Fills</h2><div class="tablewrap" style="border:0"><table class="pvsa"><tbody>
      ${t.assetType === 'option' ? `<tr><td>Contract</td><td class="r">${esc(t.underlying)} ${t.optType} ${t.strike} exp ${esc(fmtExp(t.expiry))} (${t.dte} days at entry)</td></tr>` : ''}<tr><td>Quantity</td><td class="r">${t.qty}${t.assetType === 'option' ? ' contracts' : ''}</td></tr><tr><td>Average entry</td><td class="r">${px(t.entry)}</td></tr><tr><td>Average exit</td><td class="r">${px(t.exit)}</td></tr>
      ${t.cost ? `<tr><td>Position size</td><td class="r">${money(t.cost, false)}${t.retPct != null ? ` <span class="${cls(t.retPct)}">(${t.retPct > 0 ? '+' : ''}${t.retPct.toFixed(1)}%)</span>` : ''}</td></tr>` : ''}
      <tr><td>Gross / fees</td><td class="r">${money(t.gross)} / ${money(-t.fees)}</td></tr>
      ${t.riskD != null ? `<tr><td>Risk at plan stop</td><td class="r ${t.riskD > S.me.settings.riskPerTrade * 1.25 ? 'dev' : ''}">${money(t.riskD, false)} (plan ${money(S.me.settings.riskPerTrade, false)})</td></tr>` : ''}
      ${t.mae != null ? `<tr><td>Went against / for you</td><td class="r">−${t.mae.toFixed(2)}R / +${t.mfe.toFixed(2)}R</td></tr>` : ''}
    </tbody></table></div></div>
    <div class="panel"><h2>Plan</h2>
      <div class="form-grid">
        <div class="field"><label for="p-setup">Setup</label><input id="p-setup" type="text" list="setup-list" value="${esc(t.setup)}"><datalist id="setup-list">${setups.map(s => `<option value="${esc(s)}">`).join('')}</datalist></div>
        <div class="field"><label for="p-entry">Planned entry${t.assetType === 'option' ? ' (option price)' : ''}</label><input id="p-entry" type="number" step="any" value="${p.entry ?? ''}"></div>
        <div class="field"><label for="p-stop">Stop${t.assetType === 'option' ? ' (option price)' : ''}</label><input id="p-stop" type="number" step="any" value="${p.stop ?? ''}"></div>
        <div class="field"><label for="p-target">Target${t.assetType === 'option' ? ' (option price)' : ''}</label><input id="p-target" type="number" step="any" value="${p.target ?? ''}"></div>
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
  loadBars(); loadReview(); if (t.status === 'open') loadEntryNote();
}
async function patch(body, msg) {
  const id = cur.id;
  try { const v = replaceTrade(await api(`/trades/${id}`, { method: 'PATCH', body })); if (cur && cur.id === id) { cur = v; renderDrawer(); } toast(msg); }
  catch (e) { toast(e.message); }
}
const pct = (v, d = 1) => v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`;
const scoreCol = v => v >= 70 ? 'var(--gain)' : v >= 45 ? 'var(--warn)' : 'var(--loss)';
const pc = (v, d = 0) => v == null ? '—' : `${(v * 100).toFixed(d)}%`;
const atrf = v => v == null ? '—' : `${v.toFixed(1)} ATR`;
function qPanel(t) {
  const q = t.q;
  if (!q || q.v !== 1) return `<h2>Execution scorecard</h2><p class="muted" style="margin:0 0 10px">${S.qBusy?.[t.id] ? 'Scoring the entry, exit and size…' : 'Grades the entry, the exit and the position size from the stock’s price path.'}</p><button class="btn" id="q-run"${S.qBusy?.[t.id] ? ' disabled' : ''}>${S.qBusy?.[t.id] ? 'Scoring…' : 'Score this trade'}</button>`;
  if (q.missing) return `<h2>Execution scorecard</h2><p class="muted" style="margin:0">Not available: ${esc(q.missing)}.</p>`;
  const g = (label, v) => `<div style="flex:1;min-width:110px"><div class="muted" style="font-size:.84rem">${label}</div><div style="font-size:1.6rem;font-weight:600;color:${scoreCol(v)}">${v}</div><div class="meter" style="margin-top:4px"><i style="width:${v}%;background:${scoreCol(v)}"></i></div></div>`;
  const row = (k, v, note) => `<tr><td>${k}</td><td class="r">${v}${note ? ` <span class="muted">${note}</span>` : ''}</td></tr>`;
  const z = q.size || {};
  return `<h2>Execution scorecard</h2>
  <div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:12px">${g('Entry', q.scores.entry)}${g('Exit', q.scores.exit)}${g('Size', q.scores.size)}</div>
  <ul style="margin:0 0 12px;padding-left:18px">${(q.verdicts || []).map(v => `<li>${esc(v)}</li>`).join('')}</ul>
  <div class="tablewrap" style="border:0"><table class="pvsa"><tbody>
    <tr><td colspan="2"><b>Entry</b></td></tr>
    ${row('Entry efficiency', pc(q.entryEff), '(100% = bought the best price of the trade)')}
    ${row('Where in the day’s range', pc(q.entryDayPct), '(0% = best side for your direction)')}
    ${row('Went against you (heat)', atrf(q.heatAtr))}
    ${row('Distance from 21 EMA at entry', atrf(q.extAtr))}
    <tr><td colspan="2" style="padding-top:12px"><b>Exit</b></td></tr>
    ${row('Exit efficiency', pc(q.exitEff), '(100% = sold the best price of the trade)')}
    ${row('Share of the best move kept', q.captured == null ? '—' : pc(Math.max(0, q.captured)))}
    ${row('Best move while held', atrf(q.runupAtr))}
    ${row('5 days after exit: further in your favor / against', `${atrf(q.afterUpAtr)} / ${atrf(q.afterDownAtr)}`)}
    ${row('Total efficiency', pc(q.totalEff))}
    <tr><td colspan="2" style="padding-top:12px"><b>Size</b></td></tr>
    ${row('Capital used', money(z.cost, false), z.costPctAccount != null ? `(${z.costPctAccount}% of account)` : '')}
    ${row('Vs your typical position', z.sizeVsTypical ? `${z.sizeVsTypical.toFixed(2)}x` : '—', z.typicalCost ? `(typical ${money(z.typicalCost, false)})` : '')}
    ${z.maxLoss ? row('Max loss (premium paid)', money(z.maxLoss, false)) : ''}
  </tbody></table></div>
  <p class="muted" style="font-size:.82rem;margin:10px 0 0">Measured on ${t.assetType === 'option' ? `${esc(t.underlying)} (the stock)` : 'the stock'} using ${q.tf === '1d' ? 'daily' : q.tf} bars. ATR = 21-day average true range (${px(q.atr)}).</p>`;
}
async function tradeQuality(id, quiet) {
  if (S.qBusy?.[id]) return; (S.qBusy = S.qBusy || {})[id] = true;
  const p0 = $('#q-panel'); if (p0 && cur && cur.id === id) p0.innerHTML = qPanel(cur);
  try { const v = replaceTrade(await api(`/trades/${id}/quality`, { method: 'POST', body: {} })); S.qBusy[id] = false; if (cur && cur.id === id) { cur = v; const p = $('#q-panel'); if (p) p.innerHTML = qPanel(v); } }
  catch (e) { S.qBusy[id] = false; if (!quiet) toast(e.message); const p = $('#q-panel'); if (p && cur && cur.id === id) p.innerHTML = qPanel(cur); }
}
async function runQualityAll() {
  const btn = $('#q-all'); if (btn) { btn.disabled = true; btn.textContent = 'Scoring…'; }
  let fails = 0, last = null;
  for (let i = 0; i < 300; i++) {
    try { const r = await api('/analysis/quality', { method: 'POST' }); fails = 0; const b = $('#q-all'); if (b) b.textContent = `Scoring… ${r.remaining} left`;
      if (!r.remaining || (last === r.remaining && !r.analyzed)) break; last = r.remaining; }
    catch (e) { if (++fails >= 3) { toast(e.message); break; } await sleep(2000); }
  }
  await reload(); toast('Scoring done');
}
function ctxPanel(t) {
  const c = t.ctx, who = t.assetType === 'option' ? `${esc(t.underlying)} (the stock)` : esc(t.underlying || t.sym);
  if (!c || c.v !== 2) return `<h2>Chart context at entry</h2><p class="muted" style="margin:0 0 10px">${S.ctxBusy?.[t.id] ? 'Analyzing the chart for this trade…' : `Trend, moving averages, your study levels, volume and gap for ${who} when you entered.`}</p><button class="btn" id="ctx-run"${S.ctxBusy?.[t.id] ? ' disabled' : ''}>${S.ctxBusy?.[t.id] ? 'Analyzing…' : 'Analyze chart'}</button>`;
  if (c.missing) return `<h2>Chart context at entry</h2><p class="muted" style="margin:0">No daily price history was found for ${who}.</p>`;
  const row = (k, v, note) => `<tr><td>${k}</td><td class="r">${v}${note ? ` <span class="muted">${note}</span>` : ''}</td></tr>`;
  const ma = (m) => c[m] == null ? '—' : `${px(c[m])} <span class="${c.prevClose >= c[m] ? 'gain' : 'loss'}">${c.prevClose >= c[m] ? 'above' : 'below'}</span>`;
  return `<h2>Chart context at entry</h2><p class="muted" style="margin:-4px 0 8px;font-size:.88rem">${who} as of the close before your entry${c.gapPct != null ? ', plus the entry day’s gap and volume' : ''}.</p>
  <div class="tablewrap" style="border:0"><table class="pvsa"><tbody>
    ${row('Trend (price vs 50 and 200-day MA)', `<b>${esc(c.trend || '—')}</b>`)}
    ${row('10 / 20 / 50-day MA', `${ma('ma10')} · ${ma('ma20')} · ${ma('ma50')}`)}
    ${row('Extension from 20-day MA', c.ext20Adr == null ? '—' : `${c.ext20Adr.toFixed(1)} ADR`, c.ext20Adr >= 3 ? '(very extended)' : c.ext20Adr < 0 ? '(below the MA)' : '')}
    ${row('Average daily range (20 days)', `${c.adrPct.toFixed(1)}%`)}
    ${row('From 20-day high / 52-week high', `${pct(c.fromHigh20Pct)} / ${pct(c.fromHigh52Pct)}`)}
    ${row('Move over last 5 / 20 days', `${pct(c.chg5Pct)} / ${pct(c.chg20Pct)}`)}
    ${row('RSI (14)', c.rsi14 == null ? '—' : c.rsi14.toFixed(0), c.rsi14 >= 70 ? '(overbought)' : c.rsi14 <= 30 ? '(oversold)' : '')}
    ${c.gapPct != null ? row('Gap at the open', pct(c.gapPct)) : ''}
    ${c.rvol != null ? row('Volume that day vs 20-day average', `${c.rvol.toFixed(1)}x`) : ''}
    ${c.brokeHigh20 != null ? row('Took out the 20-day high that day', c.brokeHigh20 ? 'Yes' : 'No') : ''}
    ${c.dayChgPct != null ? row('Stock’s move that day', pct(c.dayChgPct)) : ''}
    ${c.study ? `<tr><td colspan="2" style="padding-top:14px"><b>Your study (HVC, anchored VWAP, FVG)</b></td></tr>
      ${row('High-volume close (HVC)', c.study.hvc == null ? '—' : `${px(c.study.hvc)} <span class="muted">bands ${px(c.study.hvDn)}–${px(c.study.hvUp)}</span>`, c.study.vsHvc ? `(${c.study.vsHvc}, ${pct(c.study.hvcDistPct)})` : '')}
      ${row('Anchored VWAP', c.study.avwap == null ? '—' : `${px(c.study.avwap)} <span class="muted">since ${fmtDate(c.study.avwapAnchor)}</span>`, c.study.vsAvwap ? `(${c.study.vsAvwap}, ${pct(c.study.avwapDistPct)})` : '')}
      ${row('Fair value gap', esc(c.study.fvgState), c.study.bullFvg ? `bull ${px(c.study.bullFvg[0])}–${px(c.study.bullFvg[1])}` : c.study.bearFvg ? `bear ${px(c.study.bearFvg[0])}–${px(c.study.bearFvg[1])}` : '')}` : ''}
    ${c.intraday ? `<tr><td colspan="2" style="padding-top:14px"><b>At the moment you entered</b></td></tr>
      ${row('Stock price at entry', px(c.intraday.underlyingAtEntry))}
      ${row('Volume of the 5-min entry bar', c.intraday.entryBarRvol == null ? '—' : `${c.intraday.entryBarRvol.toFixed(1)}x the previous 20 bars`)}
      ${c.intraday.sessionVwap ? row('Day VWAP', `${px(c.intraday.sessionVwap)} <span class="${c.intraday.aboveSessionVwap ? 'gain' : 'loss'}">${c.intraday.aboveSessionVwap ? 'above' : 'below'}</span>`) : ''}
      ${c.intraday.studyAtEntryPrice ? row('Entry price vs HVC / anchored VWAP', `${esc(c.intraday.studyAtEntryPrice.vsHvc || '—')} / ${esc(c.intraday.studyAtEntryPrice.vsAvwap || '—')}`, esc(c.intraday.studyAtEntryPrice.fvgState || '')) : ''}` : ''}
    ${c.option ? `<tr><td colspan="2" style="padding-top:14px"><b>The option contract that day</b></td></tr>
      ${row('Contract volume', `${c.option.optVolume.toLocaleString('en-US')}`, c.option.optVolRatio != null ? `(${c.option.optVolRatio.toFixed(1)}x its 5-day average)` : '')}` : t.assetType === 'option' ? `<tr><td colspan="2" class="muted" style="padding-top:10px;white-space:normal">Option contract volume needs Alpaca connected (Settings → Brokers). History starts Feb 2024.</td></tr>` : ''}
  </tbody></table></div>`;
}
async function tradeContext(id, quiet) {
  if (S.ctxBusy?.[id]) return; (S.ctxBusy = S.ctxBusy || {})[id] = true;
  const btn = $('#ctx-run'); if (btn && !quiet) { btn.disabled = true; btn.textContent = 'Analyzing…'; }
  try {
    const v = replaceTrade(await api(`/trades/${id}/context`, { method: 'POST', body: {} }));
    if (cur && cur.id === id) { cur = v; const p = $('#ctx-panel'); if (p) p.innerHTML = ctxPanel(v); }
  } catch (e) { if (!quiet) toast(e.message); if (btn) { btn.disabled = false; btn.textContent = 'Try again'; } }
  finally { S.ctxBusy[id] = false; }
}
async function runContext(all) {
  if (!all) { if (cur) tradeContext(cur.id); return; }
  const btn = $('#ctx-all'); if (btn) { btn.disabled = true; btn.textContent = 'Analyzing…'; }
  let fails = 0, last = null;
  for (let i = 0; i < 200; i++) {
    try {
      const r = await api('/analysis/context', { method: 'POST' });
      fails = 0;
      const b = $('#ctx-all'); if (b) b.textContent = `Analyzing… ${r.remaining} trade${r.remaining === 1 ? '' : 's'} left`;
      if (!r.remaining) break;
      if (last === r.remaining && !r.analyzed) break;   // nothing moved: stop instead of looping
      last = r.remaining;
    } catch (e) { if (++fails >= 3) { toast(e.message); break; } await sleep(2000); }
  }
  await reload(); toast('Chart analysis done');
}
function reviewHtml(r) {
  const v = { followed_plan: ['ok', 'Followed the plan'], partial: ['mid', 'Partly followed the plan'], broke_plan: ['bad', 'Broke the plan'], no_plan: ['mid', 'No plan logged'] }[r.verdict] || ['mid', 'Reviewed'];
  return `<div class="coach-who">${spark()} Coach review <span class="verdict ${v[0]}" style="margin-left:6px">${v[1]}</span></div>
    ${r.summary ? `<p class="rule" style="font-size:1.05rem;margin:6px 0 4px">${esc(r.summary)}</p>` : ''}
    ${r.pattern ? `<p style="margin:8px 0 0;padding:10px 12px;background:var(--surface);border-radius:8px"><b>Your pattern:</b> ${esc(r.pattern)}</p>` : ''}
    ${r.what_worked?.length ? `<h3>What worked</h3><ul>${r.what_worked.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    ${r.what_broke?.length ? `<h3>What broke</h3><ul>${r.what_broke.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    ${r.lesson ? `<h3>Next time</h3><p style="margin:0">${esc(r.lesson)}</p>` : ''}
    ${r.suggested_tags?.filter(x => !cur.tags.includes(x)).length ? `<div class="sug-tags"><span class="muted">Suggested tags:</span> ${r.suggested_tags.filter(x => !cur.tags.includes(x)).map(x => `<button class="toggle" data-tag="${esc(x)}" aria-pressed="false">Add ${esc(x)}</button>`).join(' ')}</div>` : ''}
    <p style="margin:12px 0 0"><button class="linkbtn" id="rv-run">Review again with my latest plan and tags</button></p>`;
}
async function loadEntryNote() {
  const id = cur.id; let notes = [];
  try { notes = (await api(`/coach/notes?tradeId=${id}&limit=50`)).notes; } catch (e) {}
  const box = $('#entry-note'); if (!box || !cur || cur.id !== id) return;
  box.innerHTML = notes.length ? noteHtml(notes[0]) + `<p style="margin:10px 0 0"><button class="linkbtn" data-coach="entry" data-trade="${id}">Check this position again</button></p>`
    : `<div class="coach-who">${spark()} Entry check</div><p style="margin:0 0 10px">The coach can review this open position against your rules, the chart and your history.</p><button class="btn coachbtn" data-coach="entry" data-trade="${id}">Check this entry</button>`;
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
function studySeries(bars) {
  const out = []; let hvc = null, up = null, dn = null, anchor = null, pv = 0, vv = 0; const trs = [];
  let bearTop = null, bearBot = null, bullTop = null, bullBot = null;
  const adr = j => { if (j < 20) return null; let t = 0; for (let k = 0; k < 21; k++) t += bars[j - k].h / bars[j - k].l; return 100 * (t / 20 - 1); };
  bars.forEach((b, i) => {
    const prev = hvc;
    if (i >= 1) { const w = bars.slice(Math.max(0, i - 21), i).map(x => x.v || 0), v1 = bars[i - 1].v || 0;
      if (w.length && v1 > 0 && v1 === Math.max(...w)) { const c1 = bars[i - 1].c, a = adr(i - 1); hvc = c1; up = a == null ? null : c1 * (1 + a / 100); dn = a == null ? null : c1 * (1 - a * .618 / 100); } }
    if (hvc != null && hvc !== prev) { anchor = i; pv = 0; vv = 0; }
    if (anchor != null) { pv += (b.h + b.l + b.c) / 3 * (b.v || 0); vv += b.v || 0; }
    const pc = i ? bars[i - 1].c : b.c; trs.push(Math.max(b.h, pc) - Math.min(b.l, pc));
    const last = trs.slice(-20), thr = .33 * last.reduce((a, x) => a + x, 0) / last.length;
    const bear = i >= 2 && bars[i - 2].l - b.h > thr, bull = i >= 2 && b.l - bars[i - 2].h > thr;
    if (bear) { bearTop = bars[i - 2].l; bearBot = b.h; } else if (bearTop != null && b.c > bearTop) bearTop = bearBot = null;
    if (bull) { bullBot = bars[i - 2].h; bullTop = b.l; } else if (bullBot != null && b.c < bullBot) bullTop = bullBot = null;
    out.push({ hvc, up, dn, avwap: anchor != null && vv ? pv / vv : null, bullTop, bullBot, bearTop, bearBot });
  });
  return out;
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
  let maLines = '';
  if (S.tf === '1d') {
    const cl = bars.map(b => b.c);
    for (const [n, col] of [[10, '#5b8def'], [20, '#e0a544'], [50, '#a15cf0']]) {
      const pts = []; for (let i = n - 1; i < k; i++) { const v = cl.slice(i - n + 1, i + 1).reduce((a, b) => a + b, 0) / n; if (v >= lo - pad && v <= hi + pad) pts.push(`${X(i).toFixed(1)},${Y(v).toFixed(1)}`); }
      if (pts.length > 1) maLines += `<polyline points="${pts.join(' ')}" fill="none" stroke="${col}" stroke-width="1.3" opacity=".9"/>`;
    }
    if (bars.some(b => b.v)) {
      const st = studySeries(bars), seg = (key, col, w, dash) => { let d = '', on = false; for (let i = 0; i < k; i++) { const v = st[i][key]; if (v == null || v < lo - pad || v > hi + pad) { on = false; continue; } d += `${on ? 'L' : 'M'}${X(i).toFixed(1)} ${Y(v).toFixed(1)} `; on = true; } return d ? `<path d="${d}" fill="none" stroke="${col}" stroke-width="${w}"${dash ? ` stroke-dasharray="${dash}"` : ''}/>` : ''; };
      let zones = '';
      for (let i = 0; i < k; i++) { const z = st[i];
        if (z.bullTop != null) zones += `<rect x="${X(i) - bw / 2}" y="${Y(Math.max(z.bullTop, z.bullBot))}" width="${bw}" height="${Math.abs(Y(z.bullTop) - Y(z.bullBot))}" fill="#e0c341" opacity=".22"/>`;
        if (z.bearTop != null) zones += `<rect x="${X(i) - bw / 2}" y="${Y(Math.max(z.bearTop, z.bearBot))}" width="${bw}" height="${Math.abs(Y(z.bearTop) - Y(z.bearBot))}" fill="#d14fd1" opacity=".2"/>`; }
      maLines = zones + maLines + seg('hvc', '#18b5c9', 2) + seg('up', '#18b5c9', 1, '4 3') + seg('dn', '#18b5c9', 1, '4 3') + seg('avwap', '#e6b800', 2);
      maLines += `<text x="${pl + 112}" y="${pt + 10}" font-size="10" fill="#18b5c9">HVC ± bands</text><text x="${pl + 186}" y="${pt + 10}" font-size="10" fill="#c9a000">Anchored VWAP</text><text x="${pl + 272}" y="${pt + 10}" font-size="10" fill="#b39b1f">FVG zones</text>`;
    }
    maLines += `<text x="${pl + 4}" y="${pt + 10}" font-size="10" fill="#5b8def">MA10</text><text x="${pl + 40}" y="${pt + 10}" font-size="10" fill="#e0a544">MA20</text><text x="${pl + 76}" y="${pt + 10}" font-size="10" fill="#a15cf0">MA50</text>`;
  }
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
    ${candles}${maLines}${k > ei ? mk(ei, ePrice, 'var(--coach)', eLab, long) : ''}${xi != null && k > xi ? mk(xi, xPrice, 'var(--ink)', xLab, !long) : ''}
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
  symbol: ['Contract', t => symFmt(t.sym)], account: ['Account', t => t.acct], tag: ['Mistake tag', null], emotion: ['Emotion', t => t.emotion || 'Not set'],
  size: ['Position size', t => t.cost == null ? 'Unknown' : t.cost < 250 ? 'Under $250' : t.cost < 500 ? '$250–500' : t.cost < 1000 ? '$500–1,000' : t.cost < 2500 ? '$1,000–2,500' : t.cost < 5000 ? '$2,500–5,000' : '$5,000 and up'],
  perDay: ['Trades that day', t => { const n = S.trades.filter(x => x.date === t.date).length; return n === 1 ? '1 trade' : n <= 3 ? '2–3 trades' : n <= 5 ? '4–5 trades' : '6 or more'; }],
  after: ['After previous trade', t => { const prev = closed(S.trades).filter(x => (x.closeTs || '') < t.openTs).sort((a, b) => a.closeTs.localeCompare(b.closeTs)).slice(-2); if (!prev.length) return 'First trade'; const l = prev.filter(x => x.net < 0).length; return prev.length === 2 && l === 2 ? 'After 2 losses in a row' : prev[prev.length - 1].net < 0 ? 'After a loss' : 'After a win'; }],
  trend: ['Trend at entry', t => t.ctx && !t.ctx.missing ? (t.ctx.trend || 'Not enough history') : 'Not analyzed'],
  ma50: ['Vs 50-day MA', t => !t.ctx || t.ctx.missing || t.ctx.above50 == null ? 'Not analyzed' : t.ctx.above50 ? 'Above 50-day MA' : 'Below 50-day MA'],
  ext: ['Extension from 20-day MA', t => { const x = t.ctx && t.ctx.ext20Adr; return x == null ? 'Not analyzed' : x < 0 ? 'Below 20-day MA' : x < 1 ? '0–1 ADR above' : x < 2 ? '1–2 ADR above' : x < 3 ? '2–3 ADR above' : '3+ ADR above'; }],
  rvol: ['Volume vs average', t => { const x = t.ctx && t.ctx.rvol; return x == null ? 'Not analyzed' : x < 1 ? 'Under 1x' : x < 2 ? '1–2x' : x < 3 ? '2–3x' : '3x or more'; }],
  gap: ['Gap at open', t => { const x = t.ctx && t.ctx.gapPct; return x == null ? 'Not analyzed' : x <= -2 ? 'Gap down 2%+' : x < -0.5 ? 'Small gap down' : x <= 0.5 ? 'Flat open' : x < 2 ? 'Small gap up' : 'Gap up 2%+'; }],
  high20: ['Near 20-day high', t => { const c = t.ctx; if (!c || c.missing) return 'Not analyzed'; if (c.brokeHigh20) return 'Broke the 20-day high'; const x = c.fromHigh20Pct; return x >= -3 ? 'Within 3% of high' : x >= -10 ? '3–10% below high' : 'More than 10% below high'; }],
  hvcRel: ['Vs HVC (study)', t => t.ctx && t.ctx.study && t.ctx.study.vsHvc ? ({ above: 'Above HVC', near: 'Near HVC (±1.5%)', below: 'Below HVC' })[t.ctx.study.vsHvc] : 'Not analyzed'],
  avwapRel: ['Vs anchored VWAP', t => t.ctx && t.ctx.study && t.ctx.study.vsAvwap ? (t.ctx.study.vsAvwap === 'above' ? 'Above anchored VWAP' : 'Below anchored VWAP') : 'Not analyzed'],
  fvg: ['Fair value gap', t => t.ctx && t.ctx.study ? t.ctx.study.fvgState : 'Not analyzed'],
  entryVol: ['Entry-bar volume', t => { const x = t.ctx && t.ctx.intraday && t.ctx.intraday.entryBarRvol; return x == null ? 'Not available' : x < 1 ? 'Under 1x' : x < 2 ? '1–2x' : x < 4 ? '2–4x' : '4x or more'; }],
  dayVwap: ['Vs day VWAP at entry', t => t.ctx && t.ctx.intraday && t.ctx.intraday.aboveSessionVwap != null ? (t.ctx.intraday.aboveSessionVwap ? 'Above day VWAP' : 'Below day VWAP') : 'Not available'],
  optVol: ['Option volume vs its average', t => { const x = t.ctx && t.ctx.option && t.ctx.option.optVolRatio; return x == null ? 'Not available' : x < 1 ? 'Under 1x' : x < 2 ? '1–2x' : x < 5 ? '2–5x' : '5x or more'; }],
  entryQ: ['Entry score', t => !t.q || t.q.v !== 1 || t.q.missing ? 'Not scored' : t.q.scores.entry >= 70 ? 'Good entry (70+)' : t.q.scores.entry >= 45 ? 'OK entry (45–69)' : 'Poor entry (under 45)'],
  exitQ: ['Exit score', t => !t.q || t.q.v !== 1 || t.q.missing ? 'Not scored' : t.q.scores.exit >= 70 ? 'Good exit (70+)' : t.q.scores.exit >= 45 ? 'OK exit (45–69)' : 'Poor exit (under 45)'],
  sizeQ: ['Size vs typical', t => { const x = t.q && t.q.size && t.q.size.sizeVsTypical; return x == null ? 'Not scored' : x < 0.75 ? 'Smaller (<0.75x)' : x <= 1.25 ? 'Typical (0.75–1.25x)' : x < 2 ? 'Larger (1.25–2x)' : '2x or more'; }],
  rsi: ['RSI at entry', t => { const x = t.ctx && t.ctx.rsi14; return x == null ? 'Not analyzed' : x < 30 ? 'Under 30' : x < 50 ? '30–50' : x < 70 ? '50–70' : '70 and up'; }]
};
const BD_ORDER = {
  size: ['Under $250', '$250–500', '$500–1,000', '$1,000–2,500', '$2,500–5,000', '$5,000 and up', 'Unknown'],
  perDay: ['1 trade', '2–3 trades', '4–5 trades', '6 or more'],
  ext: ['Below 20-day MA', '0–1 ADR above', '1–2 ADR above', '2–3 ADR above', '3+ ADR above', 'Not analyzed'],
  rvol: ['Under 1x', '1–2x', '2–3x', '3x or more', 'Not analyzed'],
  gap: ['Gap down 2%+', 'Small gap down', 'Flat open', 'Small gap up', 'Gap up 2%+', 'Not analyzed'],
  high20: ['Broke the 20-day high', 'Within 3% of high', '3–10% below high', 'More than 10% below high', 'Not analyzed'],
  rsi: ['Under 30', '30–50', '50–70', '70 and up', 'Not analyzed'],
  entryQ: ['Good entry (70+)', 'OK entry (45–69)', 'Poor entry (under 45)', 'Not scored'],
  exitQ: ['Good exit (70+)', 'OK exit (45–69)', 'Poor exit (under 45)', 'Not scored'],
  sizeQ: ['Smaller (<0.75x)', 'Typical (0.75–1.25x)', 'Larger (1.25–2x)', '2x or more', 'Not scored'],
  hvcRel: ['Above HVC', 'Near HVC (±1.5%)', 'Below HVC', 'Not analyzed'],
  entryVol: ['Under 1x', '1–2x', '2–4x', '4x or more', 'Not available'],
  optVol: ['Under 1x', '1–2x', '2–5x', '5x or more', 'Not available']
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
  ${executionPanel(ts)}
  ${chartAnalysisPanel(ts)}
  <section class="panel"><h2>Breakdown</h2><div class="filters" role="group" aria-label="Group by">${Object.entries(BD).map(([k, [l]]) => `<button class="toggle emo" data-bd="${k}" aria-pressed="${S.bd === k}">${l}</button>`).join('')}</div>${breakdown(ts)}</section>
  <section class="grid2">
    <div class="panel"><h2>How much of each move you kept</h2>${scatter(ts)}</div>
    <div class="panel"><h2>Streaks and averages</h2>
      <div class="kv"><span>Average winner</span><b class="gain">${money(A.avgW)}</b></div><div class="kv"><span>Average loser</span><b class="loss">${money(A.avgL)}</b></div>
      ${(() => { const w = ts.filter(t => t.net > 0 && t.retPct != null), l = ts.filter(t => t.net <= 0 && t.retPct != null); return w.length || l.length ? `<div class="kv"><span>Average % gain on winners</span><b class="gain">${pct(avg(w.map(t => t.retPct)))}</b></div><div class="kv"><span>Average % loss on losers</span><b class="loss">${pct(avg(l.map(t => t.retPct)))}</b></div><div class="kv"><span>Biggest % loss</span><b class="loss">${pct(Math.min(...l.map(t => t.retPct), 0))}</b></div>` : ''; })()}
      <div class="kv"><span>Longest winning streak</span><b>${A.streakW} trades</b></div><div class="kv"><span>Longest losing streak</span><b>${A.streakL} trades</b></div>
      <div class="kv"><span>Winners that went over 0.7R against you first</span><b>${ts.filter(t => t.r > 0 && t.mae > .7).length}</b></div>
      <div class="kv"><span>Losses bigger than 1.1R</span><b class="loss">${ts.filter(t => t.r != null && t.r < -1.1).length}</b></div></div>
  </section>`;
}
function executionPanel(ts) {
  const todo = closed(S.trades).filter(t => !t.q || t.q.v !== 1).length;
  const sc = ts.filter(t => t.q && t.q.v === 1 && !t.q.missing);
  const a = f => { const xs = sc.map(f).filter(x => x != null); return xs.length ? avg(xs) : null; };
  const early = sc.filter(t => t.q.afterUpAtr >= 1 && t.q.afterUpAtr > (t.q.afterDownAtr || 0)).length;
  const chased = sc.filter(t => t.q.extAtr != null && t.q.extAtr > 1.5).length;
  const big = sc.filter(t => t.q.size && t.q.size.sizeVsTypical >= 1.5);
  const box = (label, v, sub) => `<div class="stat" style="border:0;padding:6px 14px 6px 0"><span>${label}</span><b style="color:${v != null && typeof v === 'number' ? scoreCol(v) : 'inherit'}">${v == null ? '—' : typeof v === 'number' ? Math.round(v) : v}</b>${sub ? `<small class="muted">${sub}</small>` : ''}</div>`;
  const winners = sc.filter(t => t.net > 0), losers = sc.filter(t => t.net <= 0);
  return `<section class="panel"><div class="cal-head"><h2>Execution</h2>${todo ? `<button class="btn coachbtn" id="q-all">Score ${todo} trade${todo === 1 ? '' : 's'}</button>` : ''}</div>
    ${!sc.length ? '<p class="muted" style="margin:0">Score your trades to see how good your entries, exits and sizing are. Each trade is measured on the stock’s price path: where you bought and sold within the move, how much heat you took, and what happened after you exited.</p>' : `
    <div style="display:flex;flex-wrap:wrap;gap:4px 28px">${box('Avg entry score', a(t => t.q.scores.entry))}${box('Avg exit score', a(t => t.q.scores.exit))}${box('Avg size score', a(t => t.q.scores.size))}</div>
    <div class="tablewrap" style="border:0;margin-top:8px"><table><thead><tr><th></th><th class="r">All scored</th><th class="r">Winners</th><th class="r">Losers</th></tr></thead><tbody>
      ${[['Entry efficiency', t => t.q.entryEff, pc], ['Where in the day’s range', t => t.q.entryDayPct, pc], ['Heat taken', t => t.q.heatAtr, atrf], ['Distance from 21 EMA at entry', t => t.q.extAtr, atrf],
         ['Exit efficiency', t => t.q.exitEff, pc], ['Share of best move kept', t => t.q.captured == null ? null : Math.max(0, t.q.captured), pc], ['Move after exit (in your favor)', t => t.q.afterUpAtr, atrf],
         ['Size vs typical', t => t.q.size && t.q.size.sizeVsTypical, v => v == null ? '—' : v.toFixed(2) + 'x']]
        .map(([k, f, fmt]) => { const m = xs => { const v = xs.map(f).filter(x => x != null); return v.length ? fmt(avg(v)) : '—'; }; return `<tr><td>${k}</td><td class="r">${m(sc)}</td><td class="r">${m(winners)}</td><td class="r">${m(losers)}</td></tr>`; }).join('')}
    </tbody></table></div>
    <ul class="patterns">
      <li><b>${early}</b> of ${sc.length} trades kept going at least 1 ATR in your direction within 5 days after you sold.<small><button class="linkbtn" data-bd="exitQ">Exit score breakdown</button></small></li>
      <li><b>${chased}</b> entries were more than 1.5 ATR above the 21 EMA.<small><button class="linkbtn" data-bd="entryQ">Entry score breakdown</button></small></li>
      <li><b>${big.length}</b> positions were 1.5x your typical size or more. Their net: <span class="${cls(sum(big.map(t => t.net)))}">${money(sum(big.map(t => t.net)))}</span>.<small><button class="linkbtn" data-bd="sizeQ">Size breakdown</button></small></li>
    </ul>`}</section>`;
}
function chartAnalysisPanel(ts) {
  const todo = S.trades.filter(t => !t.ctx || t.ctx.v !== 2).length, have = ts.filter(t => t.ctx && !t.ctx.missing);
  const feats = [['trend', 'Trend at entry'], ['ma50', 'Vs 50-day MA'], ['ext', 'Extension from 20-day MA'], ['rvol', 'Volume vs average'], ['gap', 'Gap at open'], ['high20', 'Near 20-day high'], ['rsi', 'RSI at entry'], ['hvcRel', 'Vs HVC'], ['avwapRel', 'Vs anchored VWAP'], ['fvg', 'Fair value gap'], ['entryVol', 'Entry-bar volume'], ['dayVwap', 'Vs day VWAP'], ['optVol', 'Option volume']];
  const findings = [];
  if (have.length >= 8) for (const [k, label] of feats) {
    const g = {}; for (const t of have) { const b = BD[k][1](t); if (b !== 'Not analyzed' && b !== 'Not enough history' && b !== 'Not available') (g[b] = g[b] || []).push(t); }
    const ent = Object.entries(g).filter(([, a]) => a.length >= 3).map(([b, a]) => [b, stats(a)]);
    if (ent.length < 2) continue;
    ent.sort((a, b) => b[1].net / b[1].n - a[1].net / a[1].n);
    const [bb, bs] = ent[0], [wb, ws] = ent[ent.length - 1];
    findings.push(`<li><b>${label}:</b> ${esc(bb)} is your best group, ${money(bs.net)} over ${bs.n} trades (${(bs.wr * 100).toFixed(0)}% winners). ${esc(wb)} is your weakest, ${money(ws.net)} over ${ws.n} trades (${(ws.wr * 100).toFixed(0)}% winners).<small><button class="linkbtn" data-bd="${k}">See the full breakdown</button></small></li>`);
  }
  return `<section class="coach"><div class="coach-who">${spark()} Chart analysis</div>
    <p style="margin:0 0 6px">For every trade, the app looks at the stock's daily chart before you entered (trend, moving averages, extension, volume, gap, RSI) and your study: high-volume close with bands, anchored VWAP and fair value gaps. For recent trades it also checks the 5-minute entry bar, the day VWAP and, with Alpaca connected, the option contract's volume. Then it compares your results in each situation.</p>
    ${todo ? `<p style="margin:10px 0"><button class="btn coachbtn" id="ctx-all">Analyze charts for ${todo} trade${todo === 1 ? '' : 's'}</button></p>` : ''}
    ${findings.length ? `<ul class="patterns">${findings.join('')}</ul>` : have.length ? '<p class="muted" style="margin:0">Not enough analyzed trades in this period for comparisons yet.</p>' : ''}</section>`;
}
function breakdown(ts) {
  const g = {};
  if (S.bd === 'tag') { for (const t of ts) for (const x of (t.tags.length ? t.tags : ['No tags'])) (g[x] = g[x] || []).push(t); }
  else { const f = BD[S.bd][1]; for (const t of ts) (g[f(t)] = g[f(t)] || []).push(t); }
  const order = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const ent = Object.entries(g).map(([k, a]) => [k, stats(a)]);
  if (BD_ORDER[S.bd]) ent.sort((a, b) => BD_ORDER[S.bd].indexOf(a[0]) - BD_ORDER[S.bd].indexOf(b[0])); else if (S.bd === 'weekday') ent.sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0])); else if (S.bd === 'dte') { const o = ['0–1 days', '2–7 days', '8–30 days', '31–60 days', 'Over 60 days', 'Not an option']; ent.sort((a, b) => o.indexOf(a[0]) - o.indexOf(b[0])); } else if (S.bd === 'time' || S.bd === 'hold') ent.sort((a, b) => a[0].localeCompare(b[0])); else ent.sort((a, b) => b[1].net - a[1].net);
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
const KIND_LABEL = { premarket: 'Pre-market check', preclose: 'Pre-close check', entry: 'New entry check', manual: 'Portfolio check' };
const STATUS_CLS = { 'on plan': 'ok', 'watch': 'mid', 'rule broken': 'bad', 'no plan': 'mid' };
function noteHtml(n, compact) {
  if (!n) return '';
  const when = (n.createdAt || '').replace('T', ' ').slice(0, 16) + ' ET';
  return `<div class="coach-who">${spark()} ${esc(KIND_LABEL[n.kind] || 'Coach')} <span class="muted" style="font-weight:400">· ${esc(when)}</span></div>
    <p class="rule" style="margin:4px 0 10px">${esc(n.headline || '')}</p>
    ${compact ? '' : `
    ${(n.portfolio || []).length ? `<h3 style="font-size:.95rem;margin:10px 0 4px">Portfolio</h3><ul style="margin:0;padding-left:18px">${n.portfolio.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    ${(n.positions || []).length ? `<h3 style="font-size:.95rem;margin:14px 0 6px">Positions</h3><ul class="patterns" style="margin-top:0">${n.positions.map(p => `<li><b>${esc(p.ticker)}</b> <span class="verdict ${STATUS_CLS[p.status] || 'mid'}" style="margin-left:6px">${esc(p.status)}</span>
        <div style="margin-top:6px">${esc(p.note)}</div>
        ${p.levels ? `<small><b>Levels:</b> ${esc(p.levels)}</small>` : ''}
        ${p.action ? `<div style="margin-top:6px"><b>Per your rules:</b> ${esc(p.action)}</div>` : ''}</li>`).join('')}</ul>` : ''}
    ${(n.focus || []).length ? `<h3 style="font-size:.95rem;margin:14px 0 4px">Focus</h3><ul style="margin:0;padding-left:18px">${n.focus.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    ${(n.rule_checks || []).length ? `<h3 style="font-size:.95rem;margin:14px 0 4px">Your rules</h3><ul style="margin:0;padding-left:0;list-style:none">${n.rule_checks.map(r => `<li style="margin:3px 0"><b class="${r.ok ? 'gain' : 'loss'}">${r.ok ? '✓' : '✗'}</b> ${esc(r.rule)} <span class="muted">${esc(r.detail)}</span></li>`).join('')}</ul>` : ''}`}`;
}
async function loadNotes(force) {
  if (S.notes !== undefined && !force) return;
  try { S.notes = (await api('/coach/notes?limit=15')).notes; } catch (e) { S.notes = []; }
}
async function runCoach(kind, tradeId) {
  await loadNotes();
  const before = (S.notes && S.notes[0] && S.notes[0].createdAt) || '';
  try { await api('/coach/notes', { method: 'POST', body: { kind, tradeId } }); } catch (e) { toast(e.message); return; }
  S.coachBusy = kind; if (['coach', 'dashboard'].includes(route())) render();
  toast('The coach is reviewing your positions. This takes about a minute.');
  for (let i = 0; i < 30; i++) {
    await sleep(5000);
    await loadNotes(true);
    if (S.notes[0] && S.notes[0].createdAt !== before) { S.coachBusy = null; if (route() === 'coach' || route() === 'dashboard') render(); if (cur && tradeId && cur.id === tradeId) loadEntryNote(); toast('Coach check ready'); return; }
  }
  S.coachBusy = null; toast('The coach is taking longer than usual. Check back in a minute.');
}
function liveCoachPanel() {
  const n = S.notes && S.notes[0], lc = S.me.settings.liveCoach || {};
  return `<section class="coach" id="live-coach">
    <div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between;align-items:center;margin-bottom:8px">
      <div class="coach-who" style="margin:0">${spark()} Live coach</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn" data-coach="premarket" ${S.coachBusy ? 'disabled' : ''}>Pre-market check</button>
        <button class="btn" data-coach="preclose" ${S.coachBusy ? 'disabled' : ''}>Pre-close check</button>
        <button class="btn coachbtn" data-coach="manual" ${S.coachBusy ? 'disabled' : ''}>${S.coachBusy ? 'Reviewing…' : 'Check my portfolio now'}</button></div></div>
    ${S.notes === undefined ? '<p class="loading" style="padding:0">Loading…</p>' : n ? noteHtml(n) : '<p style="margin:0">No coach checks yet. Run one now, or turn on the automatic checks in Settings.</p>'}
    <p class="muted" style="font-size:.82rem;margin:12px 0 0">${lc.enabled ? `Automatic: ${[lc.premarket && 'pre-market 9:00 ET', lc.preclose && 'pre-close 15:30 ET', lc.entry && 'every new entry'].filter(Boolean).join(', ')}.` : 'Automatic checks are off (Settings → Live coach).'} Coaching on your own positions and rules, not financial advice.</p>
    ${(S.notes || []).length > 1 ? `<details style="margin-top:10px"><summary class="muted" style="cursor:pointer">Earlier checks</summary>${S.notes.slice(1).map(x => `<div style="border-top:1px solid var(--line);padding-top:10px;margin-top:10px">${noteHtml(x)}</div>`).join('')}</details>` : ''}
  </section>`;
}
function vCoach() {
  const r = S.report;
  const rep = r === undefined ? `<p class="loading" style="padding:0">Loading your latest report…</p>`
    : r === null ? `<p style="margin:0 0 10px">No weekly report yet. Reports are written every Sunday morning, or you can write one now.</p>`
    : `<div class="coach-who">${spark()} Weekly report, week ending ${fmtDate(r.weekEnding)}</div><p class="rule">${esc(r.headline)}</p><p style="margin:0 0 6px">${esc(String(r.summary || '').split(/<\/\w+>|<parameter/)[0])}</p>
      ${(r.leaks || []).length ? `<ul class="patterns">${r.leaks.map((b, i) => `<li><b>${i === 0 ? 'Top leak' : 'Leak'}: ${esc(b.label)}</b>, ${money(b.dollars)}<small>${esc(b.comment)}</small></li>`).join('')}</ul>` : ''}
      ${r.rule ? `<p style="margin:14px 0 0"><b>Rule for next week:</b> ${esc(r.rule)}</p><p class="muted" style="margin:4px 0 0">${esc(r.rule_reason || '')}</p>` : ''}`;
  return head('Coach', 'Live checks on your positions, weekly report, and questions about your trades', `<button class="btn" id="rep-run">Write weekly report</button>`) + liveCoachPanel() + `
  <section class="coach" id="report">${rep}</section>
  <section><h2>Ask about your trades</h2>
    <div class="chat" id="chat">${S.chat.length ? S.chat.map(m => `<div class="msg ${m.role === 'assistant' ? 'ai' : 'me'}">${m.role === 'assistant' ? esc(m.content).replace(/\n/g, '<br>') + (m.tradeIds?.length ? tradeTable(m.tradeIds.map(byId).filter(Boolean), true) : '') : esc(m.content)}</div>`).join('')
      : `<div class="msg ai">Ask about your trades in plain words. Answers come only from your journal data.</div>`}${S.chatBusy ? '<div class="msg ai loading">Looking through your trades…</div>' : ''}</div>
    <div class="sugg">${['Show my best trades on TSLA in the first hour', 'How much did revenge trades cost me?', 'What is my win rate after 11:00?', 'Which setup has the best expectancy?', 'How do I do when the stock is extended from the 20-day MA?'].map(s => `<button data-ask="${s}">${s}</button>`).join('')}</div>
    <form class="composer" id="ask"><input id="askq" placeholder="Ask a question about your trades" aria-label="Question" autocomplete="off"><button class="btn coachbtn" type="submit" ${S.chatBusy ? 'disabled' : ''}>Ask</button></form>
  </section>`;
}
async function afterCoach() {
  if (S.notes === undefined) { await loadNotes(); if (route() === 'coach') render(); }
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
  const inAcct = S.trades.filter(inScope); const days = [...new Set([todayNY, ...inAcct.map(t => t.date), ...inAcct.filter(t => t.closeTs).map(cdate)])].sort().reverse(); if (!S.jDay) S.jDay = days[0];
  const d = S.daily[S.jDay]; const ts = inAcct.filter(t => t.date === S.jDay || (t.closeTs && cdate(t) === S.jDay));
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
  <section class="panel"><div class="form-grid"><div class="field"><label for="imp-acct">Account name</label><input id="imp-acct" type="text" list="acct-list" value="${esc(defaultImportAccount(accts))}" placeholder="e.g. Topstep 50K, IBKR cash"><datalist id="acct-list">${accts.map(a => `<option value="${esc(a)}">`).join('')}</datalist></div>
    <div class="field"><label for="imp-tz">Times in the file are</label><select id="imp-tz"><option value="auto">Detect automatically</option><option value="ET">Eastern</option><option value="CT">Central</option><option value="MT">Mountain</option><option value="PT">Pacific</option></select></div></div>
    <div class="drop" id="drop"><p style="margin:0 0 10px;font-weight:600">Drop a CSV file here</p>
      <p class="muted" style="margin:0 0 14px">Schwab / thinkorswim account statements and IBKR Flex trade exports work as-is. Any other CSV needs columns for time, symbol, side, quantity and price.</p>
      <label class="btn primary" style="display:inline-block">Choose file<input type="file" id="file" accept=".csv,text/csv" hidden></label></div>
    <p id="imp-status" class="status" style="margin:12px 0 0"></p></section>
  <section><h2>Recent imports</h2><div id="imports">${importsTable()}</div></section>`;
}
function defaultImportAccount(accts) {
  const last = (S.imports || []).find(i => i.account && !isPaper(i.account) && i.status !== 'undone');
  return (last && last.account) || accts.find(a => !isPaper(a)) || 'Main';
}
function importsTable() {
  if (!S.imports) return '<p class="loading">Loading…</p>';
  if (!S.imports.length) return '<div class="tablewrap"><p class="empty">No imports yet.</p></div>';
  return `<div class="tablewrap"><table><thead><tr><th>File</th><th>Account</th><th>Status</th><th class="r">Fills</th><th class="r">New</th><th class="r">Duplicates</th><th class="r">Skipped rows</th><th class="r">Unmatched closes</th><th>When</th><th></th></tr></thead><tbody>
  ${S.imports.map(i => `<tr><td>${esc(i.fileName)}</td><td>${esc(i.account)}</td><td title="${esc(i.error || '')}"><span class="status ${esc(i.status)}">${esc(i.status)}</span>${i.error ? ` <span class="loss" style="display:inline-block;max-width:260px;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom">${esc(i.error)}</span>` : ''}</td><td class="r">${i.fills ?? ''}</td><td class="r">${i.newFills ?? ''}</td><td class="r">${i.duplicates ?? ''}</td><td class="r">${i.skippedRows ?? ''}</td><td class="r">${i.unmatchedCloses ?? ''}</td><td>${esc((i.createdAt || '').replace('T', ' ').slice(0, 16))}</td><td>${i.status === 'done' ? `<button class="btn" data-undo="${esc(i.id)}">Undo</button>` : ''}</td></tr>`).join('')}</tbody></table></div>`;
}
async function afterImport() { try { S.imports = (await api('/imports')).imports; } catch (e) { S.imports = []; toast(e.message); } if (route() === 'import') $('#imports').innerHTML = importsTable(); }
async function upload(file) {
  const st = $('#imp-status'); const account = ($('#imp-acct').value || 'Main').trim();
  if (isPaper(account) && !confirm(`"${account}" is your paper-bot account. Import this file into it anyway?`)) return;
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
  return head('Settings', esc(Auth.email()), '') + `
  <section class="panel"><h2>Trading plan</h2><div class="form-grid">
    <div class="field"><label for="s-risk">Planned risk per trade ($)</label><input id="s-risk" type="number" min="0" step="any" value="${s.riskPerTrade}"></div></div>
    <div class="field"><label for="s-setups">Your setups, one per line</label><textarea id="s-setups">${esc((s.setups || []).join('\n'))}</textarea></div>
    <button class="btn primary" data-savesettings="1">Save</button> <span class="muted" style="font-size:.84rem">Changes also save automatically when you leave a field.</span>
  </section>
  <section class="panel"><h2>Live coach</h2>
    <p class="muted" style="margin-top:-4px">The coach reviews your open positions against your rules: before the open, 30 minutes before the close, and right after each new entry. It uses Claude, so each check has a small API cost.</p>
    <div class="field"><label><input type="checkbox" id="lc-on" ${s.liveCoach?.enabled ? 'checked' : ''}> Run automatic checks</label></div>
    <div style="display:flex;flex-wrap:wrap;gap:6px 22px;margin-bottom:12px">
      <label><input type="checkbox" id="lc-pre" ${s.liveCoach?.premarket !== false ? 'checked' : ''}> Pre-market (9:00 ET)</label>
      <label><input type="checkbox" id="lc-close" ${s.liveCoach?.preclose !== false ? 'checked' : ''}> Pre-close (15:30 ET)</label>
      <label><input type="checkbox" id="lc-entry" ${s.liveCoach?.entry !== false ? 'checked' : ''}> Every new entry</label></div>
    <div class="form-grid">
      <div class="field"><label for="s-acct">Account size ($)</label><input id="s-acct" type="number" min="0" step="any" value="${s.accountSize || ''}"></div>
      <div class="field"><label for="s-maxpos">Max size per position (% of account)</label><input id="s-maxpos" type="number" min="0" step="any" value="${s.maxPositionPct ?? 10}"></div></div>
    <div class="field"><label for="s-rules">My trading rules, one per line</label><textarea id="s-rules" style="min-height:140px" placeholder="Max 3 new entries per day&#10;Close any option below -40% of premium&#10;No new trades after 2 losses in a row&#10;Close options with less than 10 days to expiry&#10;Only enter within 1 ATR of the 21 EMA">${esc(s.rules || '')}</textarea></div>
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
  ${botSettingsPanels()}
  <section class="panel"><h2>Data</h2>
    <p class="muted" style="margin-top:-4px">Rebuilds every trade from your stored fills and removes duplicate copies of the same fill. Journal notes, tags and plans are kept.</p>
    <button class="btn" id="rebuild">Rebuild trades and remove duplicate fills</button>
    <div class="form-grid" style="align-items:end;margin-top:14px">
      <div class="field"><label for="rm-acct">Remove imported (file) fills from account</label><select id="rm-acct">${S.me.accounts.map(a => `<option>${esc(a)}</option>`).join('')}</select></div>
      <div class="field"><button class="btn" id="rm-csv">Remove</button></div></div>
    <p class="muted" style="font-size:.84rem;margin:0">Use this if a statement was imported into the wrong account. Fills synced from Schwab or Alpaca are kept; re-import the file into the right account afterwards. Newer imports can also be undone from the Import page.</p></section>
  <section class="panel"><h2>Data sources</h2>
    <p class="muted" style="margin-top:-4px">Unusual options flow comes from the Unusual Whales API with your own key (a paid Unusual Whales API plan). The key is encrypted and never shown again.</p>
    <div id="flow-key-box"><p class="loading" style="padding:0">Loading…</p></div></section>
  <section class="panel"><h2>Brokers</h2>
    <p class="muted" style="margin-top:-4px">Connected brokers sync your fills automatically. Each broker account shows up as its own account in the journal, and every page can show one account or all of them together.</p>
    <div class="broker-card"><h3>Charles Schwab</h3>${schwabHtml()}</div>
    <div class="broker-card"><h3>Alpaca</h3><div id="broker">${brokerHtml}</div></div>
    <div class="broker-card"><h3>Alpaca — day trading account</h3>${dtSettingsBox()}</div>
    <div class="broker-card"><h3>Alpaca — stock day trading account</h3>${sdSettingsBox()}</div>
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
async function loadFlowKey(force) {
  if (!S.flowKeySt || force) { let st = { connected: false }; try { st = await api('/flow/status'); } catch (e) {} S.flowKeySt = st; }
  const st = S.flowKeySt;
  const box = $('#flow-key-box'); if (!box) return;
  box.innerHTML = st.connected ? `<p style="margin:0 0 8px">Unusual Whales connected (key ending ${esc(st.hint)}).</p><button class="btn" id="uw-del">Remove key</button>`
    : `<div class="form-grid" style="align-items:end"><div class="field"><label for="uw-key">Unusual Whales API key</label><input id="uw-key" type="password" autocomplete="off"></div><div class="field"><button class="btn primary" id="uw-save">Save key</button></div></div>`;
}
async function afterSettings() {
  loadFlowKey();
  if (!S.bot) { await loadBot(); if (route() === 'settings') render(); }
  loadNtStatus();
  if (S.schwab === undefined) { try { S.schwab = await api('/broker/schwab'); } catch (e) { S.schwab = { configured: false, callback: '' }; } if (route() === 'settings') render(); }
  if (S.broker !== undefined) return; try { S.broker = await api('/broker/alpaca'); } catch (e) { S.broker = { connected: false }; toast(e.message); } if (route() === 'settings') render(); }
async function saveSettings() {
  const body = { riskPerTrade: $('#s-risk').value, setups: $('#s-setups').value.split('\n'),
    liveCoach: { enabled: $('#lc-on').checked, premarket: $('#lc-pre').checked, preclose: $('#lc-close').checked, entry: $('#lc-entry').checked },
    rules: $('#s-rules').value, accountSize: $('#s-acct').value, maxPositionPct: $('#s-maxpos').value,
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

/* ---------- options positioning (gamma / delta exposure) ---------- */
const bigMoney = v => { if (v == null) return '—'; const a = Math.abs(v), sg = v < 0 ? '−' : '+'; return a >= 1e9 ? `${sg}$${(a / 1e9).toFixed(2)}B` : a >= 1e6 ? `${sg}$${(a / 1e6).toFixed(1)}M` : a >= 1e3 ? `${sg}$${(a / 1e3).toFixed(0)}K` : `${sg}$${a.toFixed(0)}`; };
const MARKET_SYMS = ['SPY', 'QQQ', 'IWM'];
async function loadMarketGamma(force) {
  if (S.mkt && !force) return; S.mkt = S.mkt || {};
  await Promise.all(MARKET_SYMS.map(async sym => {
    try { S.mkt[sym] = await api(`/gex/market?symbol=${sym}`); } catch (e) { S.mkt[sym] = { symbol: sym, error: e.message }; }
    if (route() === 'gex') { const el = $('#mkt-strip'); if (el) el.innerHTML = marketCards(); }
  }));
}
function marketCards() {
  return MARKET_SYMS.map(sym => {
    const m = (S.mkt || {})[sym];
    if (!m) return `<div class="period"><h3>${sym}</h3><p class="loading" style="padding:0">Loading…</p></div>`;
    if (m.error) return `<div class="period"><h3>${sym}</h3><p class="muted">${esc(m.error)}</p></div>`;
    const pos = m.regime === 'positive', dist = m.gammaFlip ? (m.spot / m.gammaFlip - 1) * 100 : null;
    return `<button class="period" data-gex="${sym}" style="text-align:left;cursor:pointer;font:inherit;color:inherit">
      <h3>${sym} · ${px(m.spot)}</h3><div class="big" style="font-size:1.2rem;color:${pos ? 'var(--gain)' : 'var(--loss)'}">${pos ? 'Positive gamma' : 'Negative gamma'}</div>
      <p>Flip ${m.gammaFlip ? px(m.gammaFlip) : '—'}${dist != null ? ` (${dist >= 0 ? 'price ' + dist.toFixed(1) + '% above' : 'price ' + Math.abs(dist).toFixed(1) + '% below'})` : ''}<br>
      Walls ${px(m.putWall)} / ${px(m.callWall)}${m.dailyMove ? `<br>Next day ±${m.dailyMove.pct.toFixed(1)}%` : ''}</p></button>`;
  }).join('');
}
function tradeCheck(g) {
  const list = (g.expectedMove && g.expectedMove.byExpiration) || [];
  const tc = S.tc || {};
  const exps = list.map(m => m.exp);
  if (!tc.exp || !exps.includes(tc.exp)) tc.exp = (list.find(m => m.dte >= 40) || list[list.length - 1] || {}).exp;
  const form = `<div class="form-grid" style="align-items:end">
    <div class="field"><label for="tc-type">Type</label><select id="tc-type"><option value="call" ${tc.type !== 'put' ? 'selected' : ''}>Call</option><option value="put" ${tc.type === 'put' ? 'selected' : ''}>Put</option></select></div>
    <div class="field"><label for="tc-strike">Strike</label><input id="tc-strike" type="number" step="any" value="${tc.strike ?? ''}"></div>
    <div class="field"><label for="tc-exp">Expiration</label><select id="tc-exp">${list.map(m => `<option value="${m.exp}" ${tc.exp === m.exp ? 'selected' : ''}>${fmtExp(m.exp)} (${m.dte}d)${/^\d{4}-\d{2}-(1[5-9]|2[01])$/.test(m.exp) && new Date(m.exp + 'T12:00:00Z').getUTCDay() === 5 ? ' · monthly' : ''}</option>`).join('')}</select></div>
    <div class="field"><label for="tc-prem">Premium</label><input id="tc-prem" type="number" step="any" value="${tc.prem ?? ''}"></div>
    <div class="field"><button class="btn primary" id="tc-run">Check</button></div></div>`;
  return tcSection(form, tcResult(g));
}
function tcResult(g) {
  const list = (g.expectedMove && g.expectedMove.byExpiration) || [];
  const tc = S.tc || {};
  let out = '';
  if (tc.strike && tc.prem && tc.exp) {
    const call = tc.type !== 'put', sg = call ? 1 : -1, spot = g.spot;
    const be = call ? tc.strike + tc.prem : tc.strike - tc.prem, need = (be / spot - 1) * 100;
    const em = list.find(x => x.exp === tc.exp) || list.filter(x => x.exp <= tc.exp).pop();
    const target = call ? g.callWall : g.putWall;
    // Invalidation: the nearest meaningful gamma level on the other side of price (flip, put/call wall, or a
    // strike with heavy opposite-side gamma), but never farther than the expected move for that expiration.
    const maxOpp = Math.max(1, ...g.strikes.map(b => Math.abs(call ? b.putGex : b.callGex)));
    const heavy = g.strikes.filter(b => Math.abs(call ? b.putGex : b.callGex) >= 0.25 * maxOpp).map(b => b.strike);
    const levels = [g.gammaFlip, call ? g.putWall : g.callWall, ...heavy].filter(v => v != null && (call ? v < spot : v > spot));
    let stopLvl = levels.length ? (call ? Math.max(...levels) : Math.min(...levels)) : null;
    const emEdge = em ? (call ? em.lower : em.upper) : null;
    if (emEdge != null && (stopLvl == null || (call ? stopLvl < emEdge : stopLvl > emEdge))) stopLvl = emEdge;
    if (stopLvl == null) stopLvl = call ? spot * 0.95 : spot * 1.05;
    const above = g.gammaFlip ? spot > g.gammaFlip : null;
    const roomPct = (target / spot - 1) * 100 * sg, riskPct = (1 - stopLvl / spot) * 100 * sg;
    const mk = (ok, text) => `<li><b class="${ok === true ? 'gain' : ok === false ? 'loss' : 'muted'}">${ok === true ? '✓' : ok === false ? '✗' : '•'}</b> ${text}</li>`;
    const mkt = (S.mkt || {}).SPY;
    out = `<ul style="list-style:none;padding:0;margin:12px 0 0;line-height:1.6">
      ${mk(above == null ? null : (call ? above : !above), `Gamma regime: price is ${above ? 'above' : 'below'} the flip (${px(g.gammaFlip)}). ${above ? 'Positive gamma: moves tend to be absorbed, so expect a grind, and buy pullbacks rather than chase.' : 'Negative gamma: moves tend to extend. Good for momentum, but stops must be respected.'}`)}
      ${mk(em ? (call ? be <= em.upper : be >= em.lower) : null, `Breakeven ${px(be)} needs ${need >= 0 ? '+' : ''}${need.toFixed(1)}%. Expected move by ${em ? fmtExp(em.exp) : '—'}: ±${em ? em.pct.toFixed(1) : '—'}% (${em ? px(em.lower) + '–' + px(em.upper) : '—'}).`)}
      ${mk(call ? be < target : be > target, `${call ? 'Call' : 'Put'} wall at ${px(target)} (${roomPct >= 0 ? '+' : ''}${roomPct.toFixed(1)}% away). ${call ? (be < target ? 'Breakeven is below the wall, so the move to the wall pays.' : 'Breakeven is past the wall, so the option only pays if price breaks through resistance.') : (be > target ? 'Breakeven is above the put wall, so the move to the wall pays.' : 'Breakeven is past the put wall.')}`)}
      ${mk(roomPct > riskPct, `Room vs risk on the stock: ${roomPct.toFixed(1)}% to the ${call ? 'call' : 'put'} wall vs ${riskPct.toFixed(1)}% to the invalidation level (${px(stopLvl)}, the nearest gamma ${call ? 'support below' : 'resistance above'} price, capped at the expected move). Ratio ${(roomPct / Math.max(0.01, riskPct)).toFixed(1)} : 1.`)}
      ${mkt && !mkt.error ? mk(mkt.regime === 'positive' ? (call ? true : null) : (call ? null : true), `Market: SPY is in ${mkt.regime} gamma (flip ${px(mkt.gammaFlip)}).`) : ''}
    </ul>
    <p class="muted" style="font-size:.84rem;margin:8px 0 0">Suggested plan from the levels: stock target near ${px(target)}, invalidation on a close ${call ? 'below' : 'above'} ${px(stopLvl)}. Size the position so the premium you could lose at that stop fits your risk per trade.</p>`;
  }
  return out;
}
function tcSection(form, out) {
  return `<section class="panel"><div class="cal-head"><h2>Check an option before buying</h2><span class="muted" style="font-size:.85rem">Paste the contract you saw in the flow</span></div>${form}<div id="tc-out">${out}</div></section>`;
}
function vGex() {
  const g = S.gex, tickers = [...new Set(S.trades.filter(t => t.status === 'open').map(t => t.underlying || t.sym))];
  let recent = []; try { recent = JSON.parse(localStorage.getItem('tj.gexRecent') || '[]'); } catch (e) {}
  const days = S.gexDays || 45;
  const form = `<section class="panel"><div class="form-grid" style="align-items:end">
    <div class="field"><label for="gx-sym">Ticker</label><input id="gx-sym" type="text" list="gx-list" value="${esc(S.gexSym || tickers[0] || '')}" placeholder="Any ticker, e.g. NVDA, SPY, TSLA" style="text-transform:uppercase" autocomplete="off"><datalist id="gx-list">${tickers.map(x => `<option value="${esc(x)}">`).join('')}</datalist></div>
    <div class="field"><label for="gx-days">Expirations within</label><select id="gx-days">${[[7, '7 days'], [14, '14 days'], [30, '30 days'], [45, '45 days'], [60, '60 days'], [90, '90 days'], [180, '6 months'], [400, 'All (up to ~1 year)']].map(([d, l]) => `<option value="${d}" ${d == days ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
    <div class="field"><label for="gx-strikes">Strikes</label><select id="gx-strikes">${[['', 'All near price'], ['10', '10 each side'], ['20', '20 each side'], ['30', '30 each side']].map(([v, l]) => `<option value="${v}" ${String(S.gexStrikes || '') === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
    <div class="field"><label for="gx-exp">Expiration</label><select id="gx-exp"><option value="">All in that window</option>${(g && g.expirations || []).map(e => `<option value="${e}" ${S.gexExp === e ? 'selected' : ''}>${fmtExp(e)}</option>`).join('')}</select></div>
    <div class="field"><button class="btn primary" id="gx-run" ${S.gexBusy ? 'disabled' : ''}>${S.gexBusy ? 'Loading…' : 'Load'}</button></div></div>
    ${recent.length ? `<div class="filters" style="margin:0 0 6px"><span class="muted" style="align-self:center;font-size:.85rem">Recent:</span>${recent.map(x => `<button class="toggle emo" data-gex="${esc(x)}" aria-pressed="${S.gexSym === x}">${esc(x)}</button>`).join('')}</div>` : ''}
    ${tickers.length ? `<div class="filters" style="margin:0"><span class="muted" style="align-self:center;font-size:.85rem">Your open positions:</span>${tickers.map(x => `<button class="toggle emo" data-gex="${esc(x)}" aria-pressed="${S.gexSym === x}">${esc(x)}</button>`).join('')}</div>` : ''}</section>`;
  let body = '';
  if (S.gexErr) body = `<div class="errbox">${esc(S.gexErr)}</div>`;
  else if (g) {
    const card = (k, v, sub, col) => `<div class="period"><h3>${k}</h3><div class="big" style="${col ? `color:${col}` : ''}">${v}</div><p>${sub || ''}</p></div>`;
    const pos = g.regime === 'positive';
    body = `<section class="periods" style="grid-template-columns:repeat(auto-fit,minmax(170px,1fr))">
      ${card('Spot', px(g.spot), `${esc(g.symbol)} · ${esc(g.source)} data`)}
      ${card('Net gamma exposure', bigMoney(g.netGex), pos ? 'Positive: dealers tend to dampen moves' : 'Negative: dealers tend to amplify moves', pos ? 'var(--gain)' : 'var(--loss)')}
      ${card('Gamma flip', g.gammaFlip ? px(g.gammaFlip) : '—', g.gammaFlip ? `${pct((g.gammaFlip / g.spot - 1) * 100)} from spot` : 'No sign change within ±15%')}
      ${card('Call wall', px(g.callWall), `${pct((g.callWall / g.spot - 1) * 100)} from spot`)}
      ${card('Put wall', px(g.putWall), `${pct((g.putWall / g.spot - 1) * 100)} from spot`)}
      ${card('Max pain', g.maxPain != null ? px(g.maxPain) : '—', `for ${fmtExp(g.maxPainExpiry)}`)}
      ${card('Net delta exposure', bigMoney(g.netDex), 'Option holders’ delta, in $')}
      ${card('Put/call open interest', g.putCallOi ?? '—', `Volume ${g.putCallVolume ?? '—'} · ${g.callOi.toLocaleString('en-US')} calls, ${g.putOi.toLocaleString('en-US')} puts`)}
    </section>
    ${emPanel(g)}
    <section class="panel"><div class="cal-head"><h2>Gamma exposure by strike</h2><span class="muted" style="font-size:.85rem">$ of dealer hedging per 1% move. Green: calls, red: puts.</span></div>${gexBars(g)}</section>
    ${g.profile.length ? `<section class="panel"><div class="cal-head"><h2>Total gamma if the price moved</h2><span class="muted" style="font-size:.85rem">Where the line crosses zero is the gamma flip.</span></div>${gexProfile(g)}</section>` : ''}
    <section class="panel"><h2>Largest strikes</h2><div class="tablewrap" style="border:0"><table><thead><tr><th>Strike</th><th class="r">Net GEX</th><th class="r">Call GEX</th><th class="r">Put GEX</th><th class="r">Call OI</th><th class="r">Put OI</th><th class="r">Net DEX</th></tr></thead><tbody>
      ${g.strikes.slice().sort((a, b) => Math.abs(b.netGex) - Math.abs(a.netGex)).slice(0, 12).map(b => `<tr><td><b>${px(b.strike)}</b></td><td class="r ${cls(b.netGex)}">${bigMoney(b.netGex)}</td><td class="r">${bigMoney(b.callGex)}</td><td class="r">${bigMoney(b.putGex)}</td><td class="r">${b.callOi.toLocaleString('en-US')}</td><td class="r">${b.putOi.toLocaleString('en-US')}</td><td class="r">${bigMoney(b.dex)}</td></tr>`).join('')}
    </tbody></table></div></section>
    <p class="muted" style="font-size:.84rem">${g.contracts} contracts, ${g.expiry ? `expiring ${fmtExp(g.expiry)}` : `expiring within ${g.maxDays} days`}${g.strikesEachSide ? `, ${g.strikesEachSide} strikes each side of the price` : ''}. The flip depends on which expirations and strikes are included, so it differs between sites using different settings. Open interest is from the previous session${g.oiDate ? ` (${esc(g.oiDate)})` : ''}, so this shows positioning as of this morning. Uses the common assumption that dealers are long calls and short puts; real dealer positioning isn’t public. Loaded ${esc(g.asOf)} ET.</p>`;
  } else body = `<section class="panel"><p class="muted" style="margin:0">Pick a ticker to see where options positioning sits: gamma exposure by strike, the gamma flip level, call and put walls, max pain and delta exposure. Needs Alpaca (free) or Schwab with market data connected in Settings.</p></section>`;
  const market = `<section class="panel"><div class="cal-head"><h2>Market gamma</h2><button class="btn" id="mkt-refresh">Refresh</button></div>
    <div class="periods" id="mkt-strip" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">${marketCards()}</div>
    <p class="muted" style="font-size:.82rem;margin:8px 0 0">Options expiring within 30 days, 40 strikes each side. Click a card for the full view.</p></section>`;
  const analysis = `${S.anaErr ? `<section class="panel"><div class="errbox">${esc(S.anaErr)}</div></section>` : ''}
    ${S.anaBusy && !S.anaRes ? '<section class="panel"><p class="loading" style="padding:0">Analyzing…</p></section>' : botResult(S.anaRes && S.anaRes.symbol === S.gexSym ? S.anaRes : null)}`;
  return head('Analysis', 'Market gamma and the full analysis of any ticker', '') + market + form + (S.gexSym ? analysis : '') + body;
}
function emFor(g) {
  const list = (g.expectedMove && g.expectedMove.byExpiration) || [];
  return list.find(m => m.exp === g.expiry) || list.find(m => m.dte >= 1) || list[0] || null;
}
function emPanel(g) {
  const f2 = v => v == null ? '—' : Number(v).toFixed(2);
  const em = g.expectedMove || {}, list = em.byExpiration || [];
  if (!list.length) return '';
  const cur = emFor(g), d = em.daily;
  const mine = S.trades.filter(t => t.status === 'open' && t.assetType === 'option' && t.underlying === g.symbol);
  const posRows = mine.map(t => {
    const be = t.optType === 'call' ? t.strike + t.entry : t.strike - t.entry;
    const need = (be / g.spot - 1) * 100;
    const m = list.find(x => x.exp === t.expiry) || list.filter(x => x.exp <= t.expiry).pop();
    const inside = m ? (t.optType === 'call' ? be <= m.upper : be >= m.lower) : null;
    return `<tr><td><b>${esc(fmtExp(t.expiry))} ${t.strike} ${t.optType}</b></td><td class="r">${px(be)}</td><td class="r ${cls(t.optType === 'call' ? -need : need)}">${pct(need)}</td>
      <td class="r">${m ? `±${m.pct.toFixed(1)}% (${px(m.lower)}–${px(m.upper)})${m.exp !== t.expiry ? ` <span class="muted">by ${fmtExp(m.exp)}</span>` : ''}` : '—'}</td>
      <td>${inside == null ? '—' : inside ? '<span class="gain">Within the expected range</span>' : '<span class="loss">Needs a bigger move than expected</span>'}</td></tr>`;
  }).join('');
  return `<section class="panel"><div class="cal-head"><h2>Expected move</h2><span class="muted" style="font-size:.85rem">What the options market is pricing, from the at-the-money straddle</span></div>
    <div class="periods" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));margin-bottom:14px">
      ${d ? `<div class="period"><h3>Next day</h3><div class="big">±${f2(d.move)}</div><p>±${d.pct.toFixed(1)}% · range ${px(d.lower)}–${px(d.upper)}</p></div>` : ''}
      ${cur ? `<div class="period"><h3>By ${fmtExp(cur.exp)} (${cur.dte} days)</h3><div class="big">±${f2(cur.move)}</div><p>±${cur.pct.toFixed(1)}% · range ${px(cur.lower)}–${px(cur.upper)}</p></div>` : ''}
    </div>
    <div class="tablewrap" style="border:0"><table><thead><tr><th>Expiration</th><th class="r">Days</th><th class="r">ATM straddle</th><th class="r">Expected move</th><th class="r">Range</th><th class="r">IV</th></tr></thead><tbody>
      ${list.map(m => `<tr><td>${fmtExp(m.exp)}</td><td class="r">${m.dte}</td><td class="r">${m.straddle != null ? f2(m.straddle) : '—'}</td><td class="r"><b>±${f2(m.move)}</b> <span class="muted">(${m.pct.toFixed(1)}%)</span></td><td class="r">${px(m.lower)} – ${px(m.upper)}</td><td class="r">${m.ivAtm ? (m.ivAtm * 100).toFixed(0) + '%' : '—'}</td></tr>`).join('')}
    </tbody></table></div>
    ${posRows ? `<h3 style="font-size:.98rem;margin:16px 0 6px">Your open ${esc(g.symbol)} options</h3><div class="tablewrap" style="border:0"><table><thead><tr><th>Contract</th><th class="r">Breakeven at expiry</th><th class="r">Move needed</th><th class="r">Expected move</th><th></th></tr></thead><tbody>${posRows}</tbody></table></div>` : ''}
    <details style="margin-top:12px"><summary style="cursor:pointer;font-weight:600">How to use the expected move for entries and exits</summary>
      <ul style="margin:8px 0 0;padding-left:18px;line-height:1.55">
        <li><b>It’s the market’s range, not a forecast.</b> Roughly two out of three times, price finishes an expiration inside this range.</li>
        <li><b>Targets:</b> a profit target inside the range is realistic; one far outside needs an unusual move. Walls inside the range are natural targets.</li>
        <li><b>Stops:</b> a stop inside the daily expected move is likely to get hit by normal noise. Place it beyond the daily range or beyond a gamma level.</li>
        <li><b>Entries:</b> buying near the edge of the range in positive gamma (mean reversion) gives better location than buying in the middle or chasing the top edge.</li>
        <li><b>Buying options:</b> if your breakeven sits outside the expected range for that expiration, the option needs a bigger-than-expected move to pay. Choose a strike or expiration whose breakeven is inside it.</li>
        <li><b>With gamma:</b> in positive gamma, price tends to stay inside the range; in negative gamma (below the flip), breaks outside it are more common.</li>
      </ul></details></section>`;
}
function gexBars(g) {
  const lo = g.spot * 0.85, hi = g.spot * 1.15, xs = g.strikes.filter(b => b.strike >= lo && b.strike <= hi);
  if (!xs.length) return '<p class="muted">No strikes near the current price.</p>';
  const W = 720, H = 300, pl = 10, pr = 10, pt = 16, pb = 34, n = xs.length, bw = (W - pl - pr) / n;
  const mx = Math.max(1, ...xs.map(b => Math.max(Math.abs(b.callGex), Math.abs(b.putGex))));
  const zy = pt + (H - pt - pb) / 2, sc = (H - pt - pb) / 2 / mx, X = i => pl + i * bw + bw / 2;
  const xOf = p => { let i = xs.findIndex(b => b.strike >= p); if (i < 0) i = n - 1; const b0 = xs[Math.max(0, i - 1)], b1 = xs[i]; if (i === 0 || b1.strike === b0.strike) return X(i); return X(i - 1) + (p - b0.strike) / (b1.strike - b0.strike) * bw; };
  const step = Math.max(1, Math.ceil(n / 12));
  let svg = xs.map((b, i) => `<rect x="${X(i) - bw * .38}" y="${zy - b.callGex * sc}" width="${bw * .76}" height="${Math.max(0, b.callGex * sc)}" fill="var(--gain)"><title>${px(b.strike)} calls: ${bigMoney(b.callGex)} (OI ${b.callOi})</title></rect>
    <rect x="${X(i) - bw * .38}" y="${zy}" width="${bw * .76}" height="${Math.max(0, -b.putGex * sc)}" fill="var(--loss)"><title>${px(b.strike)} puts: ${bigMoney(b.putGex)} (OI ${b.putOi})</title></rect>
    ${i % step === 0 ? `<text x="${X(i)}" y="${H - 12}" font-size="10.5" text-anchor="middle" fill="var(--muted)">${b.strike}</text>` : ''}`).join('');
  const vline = (p, col, lab, y) => `<line x1="${xOf(p)}" x2="${xOf(p)}" y1="${pt}" y2="${H - pb}" stroke="${col}" stroke-dasharray="4 3" stroke-width="1.5"/><text x="${xOf(p) + 4}" y="${y}" font-size="11" fill="${col}" font-weight="600">${lab} ${px(p)}</text>`;
  const em = emFor(g);
  if (em) { const a = Math.max(lo, em.lower), b = Math.min(hi, em.upper); svg = `<rect x="${xOf(a)}" y="${pt}" width="${Math.max(0, xOf(b) - xOf(a))}" height="${H - pt - pb}" fill="var(--coach-soft)" opacity=".7"/><text x="${xOf(a) + 4}" y="${H - pb - 6}" font-size="10.5" fill="var(--coach)">Expected move by ${fmtExp(em.exp)}</text>` + svg; }
  svg += `<line x1="${pl}" x2="${W - pr}" y1="${zy}" y2="${zy}" stroke="var(--line)"/>` + vline(g.spot, 'var(--ink)', 'Spot', pt + 10) + (g.gammaFlip && g.gammaFlip >= lo && g.gammaFlip <= hi ? vline(g.gammaFlip, 'var(--coach)', 'Flip', pt + 24) : '');
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Gamma exposure by strike" style="width:100%;height:auto">${svg}</svg>`;
}
function gexProfile(g) {
  const p = g.profile, W = 720, H = 220, pl = 10, pr = 10, pt = 12, pb = 28;
  const lo = Math.min(0, ...p.map(x => x.gex)), hi = Math.max(0, ...p.map(x => x.gex)), sp = hi - lo || 1;
  const X = i => pl + i / (p.length - 1) * (W - pl - pr), Y = v => pt + (hi - v) / sp * (H - pt - pb);
  const path = p.map((q, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(q.gex).toFixed(1)).join(' ');
  const si = p.reduce((b, q, i) => Math.abs(q.price - g.spot) < Math.abs(p[b].price - g.spot) ? i : b, 0);
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Total gamma exposure across prices" style="width:100%;height:auto">
    <line x1="${pl}" x2="${W - pr}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--line)"/>
    <path d="${path}" fill="none" stroke="var(--coach)" stroke-width="2.2"/>
    <line x1="${X(si)}" x2="${X(si)}" y1="${pt}" y2="${H - pb}" stroke="var(--ink)" stroke-dasharray="4 3"/><text x="${X(si) + 4}" y="${pt + 10}" font-size="11" fill="var(--ink)">Spot</text>
    ${[0, 15, 30, 45, 60].map(i => `<text x="${X(i)}" y="${H - 10}" font-size="10.5" text-anchor="${i === 0 ? 'start' : i === 60 ? 'end' : 'middle'}" fill="var(--muted)">${px(p[i].price)}</text>`).join('')}</svg>`;
}
async function loadGex() {
  const sym = ($('#gx-sym')?.value || S.gexSym || '').trim().toUpperCase(); if (!sym) { toast('Enter a ticker'); return; }
  const expSel = $('#gx-exp')?.value || '';
  if (sym !== S.gexSym || !S.anaRes || S.anaRes.symbol !== sym) setTimeout(() => anaEvaluate(sym), 0);
  S.gexSym = sym; S.gexDays = +($('#gx-days')?.value || S.gexDays || 45); S.gexStrikes = $('#gx-strikes') ? $('#gx-strikes').value : (S.gexStrikes || ''); S.gexExp = sym === (S.gex && S.gex.symbol) ? expSel : ''; S.gexBusy = true; S.gexErr = null; render();
  try { const rc = JSON.parse(localStorage.getItem('tj.gexRecent') || '[]').filter(x => x !== sym); rc.unshift(sym); localStorage.setItem('tj.gexRecent', JSON.stringify(rc.slice(0, 10))); } catch (e) {}
  try { S.gex = await api(`/gex?symbol=${encodeURIComponent(sym)}&days=${S.gexDays}${S.gexExp ? `&expiry=${S.gexExp}` : ''}${S.gexStrikes ? `&strikes=${S.gexStrikes}` : ''}`); }
  catch (e) { S.gexErr = e.message; S.gex = null; }
  S.gexBusy = false; if (route() === 'gex') render();
}
async function anaEvaluate(sym) {
  sym = (sym || S.gexSym || '').toUpperCase(); if (!sym) return;
  const d = $('#gx-earn') && $('#gx-earn').value, t = $('#gx-earn-t') && $('#gx-earn-t').value;
  const body = { symbol: sym }; if ($('#gx-earn')) body.earnings = d ? `${d} ${t}` : '';
  S.anaBusy = true; S.anaErr = null; if (route() === 'gex') render();
  try { S.anaRes = await api('/bot/evaluate', { method: 'POST', body }); if (S.bot) loadBot(true); }
  catch (e) { S.anaErr = e.message; }
  S.anaBusy = false; if (route() === 'gex') render();
}

/* ---------- paper bot ---------- */
async function loadNtStatus(force) {
  // load once per visit (the page's after-render hook calls this; re-rendering must not trigger another request)
  if (S.ntStatus !== undefined && !force) return;
  S.ntStatus = null;
  try { S.ntStatus = await api('/notify/status'); } catch (e) { S.ntStatus = { email: null, emailStatus: 'unknown' }; }
  if (isBotRoute() || route() === 'settings') render();
}
async function loadEvals(page) {
  const t = evTicker(), mode = t ? `t:${t}` : 'group';
  const pg = page || (S.botEvals && S.botEvals.mode === mode && S.botEvals.page) || 1;
  try {
    const r = await api(`/bot/evaluations?page=${pg}&size=20${t ? `&symbol=${encodeURIComponent(t)}` : `&group=ticker${S.evClaude ? '&claude=1' : ''}${S.evQ ? `&q=${encodeURIComponent(S.evQ)}` : ''}`}`);
    S.botEvals = { ...r, mode };
    // on a ticker page, show its latest evaluation unless one of its evaluations is already open
    if (t && r.items.length && !(S.botRes && S.botRes.symbol === t)) S.botRes = r.items[0];
  } catch (e) {}
  if (isBotRoute()) render();
}
async function reEvaluate(sym) {
  sym = (sym || '').toUpperCase();
  if (!sym) return;
  if (evTicker() !== sym) location.hash = `#botevals?t=${encodeURIComponent(sym)}`;
  S.evBusy = sym; render();
  try {
    const rec = await api('/bot/evaluate', { method: 'POST', body: { symbol: sym } });
    S.botRes = rec; toast(`${sym}: ${rec.decision}`);
  } catch (e) { toast(e.message); }
  S.evBusy = null;
  await loadEvals(1);
  window.scrollTo(0, 0);
}
async function loadBot(force) {
  if (S.bot && !force) return;
  try { S.bot = await api('/bot'); S.botAt = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); } catch (e) { S.bot = { error: e.message, items: [], settings: {} }; }
  if (S.broker === undefined) { try { S.broker = await api('/broker/alpaca'); } catch (e) { S.broker = { connected: false }; } }
  if (isBotRoute() || route() === 'dashboard') render();
}
const DEC_CLS = { BUY: 'ok', WAIT: 'mid', SKIP: 'bad', WATCH: 'mid' };
const STRAT_LBL = { long_call: 'Long call', bull_call: 'Bull call spread', diagonal: 'Diagonal' };
const isSpread = p => !!(p && p.legs && (p.strategy === 'bull_call' || p.strategy === 'diagonal'));
const legTxt = p => !p || !p.exp ? '' : p.strategy === 'bull_call' && p.shortStrike ? `${fmtExp(p.exp)} ${p.strike}/${p.shortStrike}c`
  : p.strategy === 'diagonal' ? `${fmtExp(p.exp)} ${p.strike}c / ${p.shortStrike ? `${fmtExp(p.shortExp)} ${p.shortStrike}c` : 'no short'}` : `${fmtExp(p.exp)} ${p.strike}c`;
/* ---------- flow on the chart: when each options alert came in, and how big, against the price ---------- */
S.fc = S.fc || {}; S.fcBusy = S.fcBusy || {};
const fcKey = id => `${id}|${S.fcDays || 5}`;
async function loadFlowChart(id) {
  const k = fcKey(id), days = S.fcDays || 5;
  S.fcBusy[k] = true;
  try { S.fc[k] = await api(`/bot/${id}/flowchart?days=${days}`); } catch (e) { S.fc[k] = { error: e.message }; }
  S.fcBusy[k] = false;
  if (isBotRoute()) render();
}
S.fcZoom = S.fcZoom || {}; S.fcAutoZ = S.fcAutoZ || {};
S.fcDp = S.fcDp || {}; S.fcDpBusy = S.fcDpBusy || {};
S.fcLayers = (() => { try { const v = { sig: true, zones: true, pwv: true, fvg: false, inertia: true, inertiah: true, sqz: true, bands: true, lis: true, pmz: true, fibs: true, turn: true, kch: true, vwap: true, ema: true, kc: false, orb: false, spy: false, mp: true, evals: true, flow: true, walls: true, dark: false, acc: true, pres: true, cvd: false, ...JSON.parse(localStorage.getItem('fcLayers') || '{}') }; delete v.bt; delete v.ws; return v; } catch (e) { return { evals: true, flow: true, walls: true, dark: false, acc: true }; } })();
const FC_LAYERS = [['sig', 'Wall watch signals'], ['pwv', 'VWAP reset on walls'], ['fvg', 'FVG'], ['inertia', 'Inertia'], ['inertiah', 'Inertia 1h'], ['sqz', 'Squeeze'], ['turn', 'Squeeze turns'], ['vwap', 'VWAP'], ['ema', 'EMA'], ['kc', 'Keltner daily'], ['kch', 'Keltner hourly'], ['orb', 'Opening range'], ['spy', 'SPY trend'], ['mp', 'Expirations & max pain'], ['evals', 'Evaluations'], ['flow', 'Options flow'], ['walls', 'Gamma walls'], ['dark', 'Dark pool'], ['acc', 'Premium total'], ['pres', 'CVD'], ['cvd', 'Order flow (real CVD)']];
function fcLayerBar(id) {
  const dp = S.fcDp[fcKey(id)], ww = String(id).startsWith('ww-');
  const wwLay = S.fcAllLayers ? ['sig', 'pwv', 'fvg', 'inertia', 'inertiah', 'sqz', 'turn', 'vwap', 'ema', 'kc', 'kch', 'orb', 'spy', 'mp', 'walls', 'flow', 'dark', 'acc', 'pres'] : ['sig', 'pwv', 'fvg', 'inertia', 'inertiah', 'sqz', 'turn', 'vwap', 'kc', 'kch', 'walls', 'flow', 'dark', 'pres'];
  return `${ww ? `<button class="btn" data-fcalllayers title="${S.fcAllLayers ? 'Only the layers of the strategy' : 'All layers (EMA, opening range, SPY trend, expirations, dark pool, premium)'}" style="font-size:.8rem;padding:2px 9px">${S.fcAllLayers ? '−' : '…'}</button>` : ''}<span class="seg" role="group" aria-label="Chart layers">${FC_LAYERS.filter(([k]) => ww ? wwLay.includes(k) : !['cvd', 'sig', 'pwv', 'fvg', 'inertia', 'inertiah', 'sqz', 'turn', 'mp', 'vwap', 'ema', 'kc', 'kch', 'orb', 'spy'].includes(k)).map(([k, l]) => `<button class="btn ${S.fcLayers[k] ? 'coachbtn' : ''}" data-fclayer="${k}" aria-pressed="${!!S.fcLayers[k]}" style="font-size:.8rem;padding:2px 9px">${l}${k === 'dark' && S.fcLayers.dark && S.fcDpBusy[fcKey(id)] ? '…' : ''}</button>`).join('')}</span>${S.fcLayers.dark && dp ? `<span class="muted" style="font-size:.8rem">${dp.error ? esc(dp.error) : `dark pool: ${dp.prints.length} prints ≥ ${money(dp.minPremium, false)} · lines from the last ${dp.levelDays || 180} days${dp.missingDays ? ` (loading ${dp.missingDays} more days…)` : ''}`}</span>` : ''}`;
}
async function loadDarkPool(id) {
  const k = fcKey(id);
  S.fcDpBusy[k] = true;
  const ww = String(id).startsWith('ww-') || String(id).startsWith('dt-');
  try { S.fcDp[k] = await api(ww ? `/bot/wallwatch/darkpool?t=${encodeURIComponent(id.replace(/^ww-(lab-)?/, "").replace(/^dt-/, "").replace(/-\d+$/, "").replace(/:D$/, ""))}&pct=${S.wwDpPct ?? 0.25}&days=${S.wwDays || 45}` : `/bot/${id}/darkpool?days=${S.fcDays || 5}`); } catch (e) { S.fcDp[k] = { error: e.message, prints: [], levels: [] }; }
  S.fcDpBusy[k] = false;
  const r = S.fcDp[k] || {};                       // 180 days of history still loading: fetch again (each day is cached)
  S.fcDpTry = S.fcDpTry || {};
  if (r.missingDays && (S.fcDpTry[k] = (S.fcDpTry[k] || 0) + 1) <= 8) setTimeout(() => loadDarkPool(id), 2000);
  if (isBotRoute()) render();
}
function fcNeedDark(id) { const k = fcKey(id); if (S.fcLayers.dark && !S.fcDp[k] && !S.fcDpBusy[k]) setTimeout(() => loadDarkPool(id), 0); }
function fcRange(id, N) {             // visible bars [a, b] for this chart (zoom)
  const z = S.fcZoom[fcKey(id)];
  if (!z || !isFinite(z[0]) || !isFinite(z[1]) || N < 3 || z[0] > N - 3 || z[1] - z[0] < 2) { if (z) S.fcZoom[fcKey(id)] = null; return [0, Math.max(0, N - 1)]; }
  const a = Math.max(0, Math.min(Math.round(z[0]), N - 2)), b = Math.min(N - 1, Math.max(Math.round(z[1]), a + 1));
  return [a, b];
}
function fcSetZoom(id, a, b, N) {
  if (!isFinite(a) || !isFinite(b) || !(N > 2)) return;   // a drag on a chart that was redrawn meanwhile: ignore
  const span = Math.round(Math.min(N - 1, Math.max(8, b - a)));     // never fewer than 8 bars; whole bars only
  a = Math.round(a); b = a + span;
  if (b > N - 1) { b = N - 1; a = Math.max(0, b - span); }
  if (a < 0) { a = 0; b = Math.min(N - 1, span); }
  S.fcZoom[fcKey(id)] = a <= 0 && b >= N - 1 ? null : [a, b];
  render();
}
function fcDaily(d) {                        // hourly chart data -> daily candles; lines take the last value of each day
  const by = new Map(), day = t => t.slice(0, 10) + 'T09:30';
  (d.bars || []).forEach(b => { const k = b.t.slice(0, 10), x = by.get(k);
    if (!x) by.set(k, { t: day(b.t), o: b.o, h: b.h, l: b.l, c: b.c, v: b.v || 0 });
    else { x.h = Math.max(x.h, b.h); x.l = Math.min(x.l, b.l); x.c = b.c; x.v += b.v || 0; } });
  const last = rows => { const m = new Map(); (rows || []).forEach(r => { if (r && r.t) m.set(r.t.slice(0, 10), { ...r, t: day(r.t) }); }); return [...m.values()]; };
  return { ...d, interval: 390, daily: true, bars: [...by.values()], vwap: last(d.vwap), ema: last(d.ema), kc: last(d.kc), kch: last(d.kch), inertia: last(d.inertia),
    vwapResets: [...new Set((d.vwapResets || []).map(day))] };
}
function fcClean(d) {                       // drop malformed rows so one bad record can't break the chart
  const ok = x => x && typeof x.t === 'string' && x.t.length >= 16;
  const bars = (d.bars || []).filter(b => ok(b) && isFinite(b.o) && isFinite(b.h) && isFinite(b.l) && isFinite(b.c)).sort((a, b) => a.t < b.t ? -1 : a.t > b.t ? 1 : 0);
  return { ...d, bars, alerts: (d.alerts || []).filter(ok), walls: (d.walls || []).filter(ok), evals: (d.evals || []).filter(ok) };
}
function fcSvg(d, id, opts) {
  try { return fcSvg0(fcClean(d), id, opts); } catch (e) {
    console.error('flow chart', e); S.fcZoom[fcKey(id)] = null;
    const where = ((e.stack || '').split('\n')[1] || '').trim().slice(0, 120);
    return `<p class="muted" style="margin:0">The chart couldn't be drawn (${esc(e.message)}${where ? ` — ${esc(where)}` : ''}). <button class="btn" data-fczoom="${esc(id)}|reset">Reset the view</button></p>`;
  }
}
function fcSvg0(d, id, opts) {
  const all = d.bars || [];
  if (all.length < 2) return '<p class="muted" style="margin:0">No intraday prices for these days.</p>';
  const [za, zb] = fcRange(id, all.length), bars = all.slice(za, zb + 1), n = bars.length;
  const nS = n + (opts && opts.padRight && zb >= all.length - 1 ? opts.padRight : 0);   // empty room on the right of the last candle
  const LY = id.startsWith('ww-') && !S.fcAllLayers ? { ...S.fcLayers, mp: false, acc: false } : S.fcLayers;
  const dp = LY.dark ? S.fcDp[fcKey(id)] : null;
  const full = !!(opts && opts.W), narrow = !full && window.innerWidth < 700, fs = narrow ? 17 : full ? 13 : 12;
  const AH = !LY.acc || (opts && opts.noAcc) ? 0 : full ? Math.round(opts.H * 0.2) : narrow ? 150 : 110;
  const SH = LY.sqz && ((d.sqzH || []).length || (d.sqzD || []).length) && !(opts && opts.noSqz) ? (full ? Math.max(80, Math.round(opts.H * 0.18)) : narrow ? 110 : 80) : 0;
  const VH = (LY.pres || (LY.cvd && d.cvd)) && bars.some(b => +b.v > 0) ? (full ? Math.max(70, Math.round(opts.H * 0.14)) : narrow ? 100 : 70) : 0;   // buy / sell pressure pane
  const W = full ? opts.W : narrow ? 560 : 960, PH = (full ? opts.H - (AH ? AH + 10 : 0) : narrow ? 470 : 340) - (SH ? SH + 10 : 0) - (VH ? VH + 10 : 0);
  const L = 8, Rr = narrow ? 72 : full ? 64 : 58, T = 16, B = PH - (narrow ? 44 : 40);
  const H = PH + (AH ? AH + 10 : 0) + (SH ? SH + 10 : 0) + (VH ? VH + 10 : 0), AT = PH + 6, AB = AH ? PH + AH - 6 : B, iv = d.interval || 15;
  const ST = PH + (AH ? AH + 10 : 0) + 6, SB = ST + SH - 6;   // squeeze momentum pane
  const VT = PH + (AH ? AH + 10 : 0) + (SH ? SH + 10 : 0) + 6, VB = VT + VH - 6, XB = VH ? VB : SH ? SB : AB;   // pressure pane (bottom)
  const pl0 = Math.min(...bars.map(b => b.l)), ph0 = Math.max(...bars.map(b => b.h)), span0 = (ph0 - pl0) || ph0 * 0.02;
  const walls = LY.walls ? (d.walls || []).filter(w => w.callWall != null || w.src === 'gap') : [];
  const near = v => v != null && v >= pl0 - span0 * 0.6 && v <= ph0 + span0 * 0.6;      // walls far from price don't squash the chart
  const wv = walls.flatMap(w => [w.callWall, w.putWall, w.flip]).concat(dp && !dp.error ? (dp.levels || []).slice(0, S.dpN ?? 5).map(l => l.price) : [])
    .concat(LY.mp !== false ? (d.maxPain || []).map(m => m.maxPain) : [])
.filter(near);
  const fbv = LY.fibs && d.fibs ? (() => { const dd = new Set(bars.map(b => b.t.slice(0, 10))); return Object.entries(d.fibs).filter(([k]) => dd.has(k)).flatMap(([, lv]) => lv.map(x => x[1])); })() : [];
  const lo0 = Math.min(pl0, ...wv, ...fbv), hi0 = Math.max(ph0, ...wv, ...fbv), pad = (hi0 - lo0) * 0.08 || 1;   // fibs: always in view
  const lo = lo0 - pad, hi = hi0 + pad;
  const X = i => L + (i + 0.5) * (W - L - Rr) / nS, Y = v => T + (hi - v) / (hi - lo) * (B - T), bw = Math.max(1, (W - L - Rr) / nS * 0.6);
  const idx = {}; bars.forEach((b, i) => { idx[b.t] = i; });
  const bucket = t => { t = String(t || ''); const d0 = t.slice(0, 10), hm = t.slice(11, 16); let m = (+hm.slice(0, 2)) * 60 + (+hm.slice(3, 5)); m = Math.max(570, Math.min(m, 960 - iv)); m = 570 + Math.floor((m - 570) / iv) * iv;
    return `${d0}T${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`; };
  const lastT = bars[n - 1].t;
  const at = t => { const k = bucket(t); if (idx[k] != null) return idx[k]; if (k > lastT) return null; let best = null; bars.forEach((b, i) => { if (b.t <= k) best = i; }); return best; };
  let g = '';
  for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4, y = Y(v); g += `<line x1="${L}" x2="${W - Rr}" y1="${y}" y2="${y}" stroke="var(--line)" stroke-width="1"/><text x="${W - Rr + 6}" y="${y + 4}" font-size="${fs}" fill="var(--muted)">${v.toFixed(v < 20 ? 2 : 1)}</text>`; }
  if (LY.zones !== false && (d.zones || []).length) d.zones.forEach(z => {    // 30-day support / resistance zones
    if (z.hi < lo || z.lo > hi) return;
    const y0 = Y(Math.min(hi, z.hi)), y1 = Y(Math.max(lo, z.lo)), col = z.kind === 'support' ? '#16a34a' : z.kind === 'resistance' ? '#dc2626' : '#5b5bd6';
    g += `<rect x="${L}" y="${y0}" width="${W - Rr - L}" height="${Math.max(2, y1 - y0)}" fill="${col}" opacity=".09"><title>${z.kind} ${z.lo.toFixed(2)}–${z.hi.toFixed(2)} · strength ${z.touches}: swings that turned here in 30 days</title></rect><text x="${W - Rr - 4}" y="${y0 + fs}" text-anchor="end" font-size="${fs - 3}" fill="${col}" opacity=".8">${z.kind === 'at price' ? 'zone' : z.kind} · ${z.touches}</text>`; });
  const nDays = new Set(bars.map(b => b.t.slice(0, 10))).size, every = Math.max(1, Math.ceil(fs * 4.5 / ((W - L - Rr) / Math.max(1, nDays))));
  let dk = -1;
  bars.forEach((b, i) => { if (i === 0 || b.t.slice(0, 10) !== bars[i - 1].t.slice(0, 10)) { dk++; if (dk % every) return; const x = X(i) - (W - L - Rr) / nS / 2;
    g += `<line x1="${x}" x2="${x}" y1="${T}" y2="${B}" stroke="var(--line)" stroke-dasharray="2 3"/><text x="${x + 3}" y="${B + 16}" font-size="${fs}" fill="var(--muted)">${b.t.slice(5, 10)}</text>`; } });
  const pf = v => v.toFixed(v < 20 ? 3 : 2);
  if (LY.fvg && (d.fvg || []).length) {               // fair value gaps: from the middle candle to the candle that fills it
    d.fvg.forEach(z => { if (z.hi < lo || z.lo > hi) return; const i0 = z.t < bars[0].t ? 0 : at(z.t); if (i0 == null) return;   // formed before the view: from the left edge
      const i1 = z.fillAt ? at(z.fillAt) : n - 1, bull = z.type === 'bull', c = bull ? '#16a34a' : '#dc2626';
      const x0 = X(i0) - bw / 2, x1 = X(i1 == null ? n - 1 : i1) + bw / 2, y0 = Y(Math.min(hi, z.hi)), y1 = Y(Math.max(lo, z.lo));
      g += `<rect x="${x0}" y="${y0}" width="${Math.max(2, x1 - x0)}" height="${Math.max(1, y1 - y0)}" fill="${c}" fill-opacity="${z.fillAt ? .05 : .1}" stroke="${c}" stroke-opacity=".35" stroke-width="1" ${z.fillAt ? 'stroke-dasharray="3 2"' : ''}><title>${esc(`${bull ? 'Bullish' : 'Bearish'} FVG ${z.lo}–${z.hi} · ${z.t.replace('T', ' ').slice(5)}${z.fillAt ? ` · ${z.end === 'expired' ? 'expired' : 'filled'} ${z.fillAt.replace('T', ' ').slice(5)}` : ' · still open'}`)}</title></rect>`; });
  }
  bars.forEach((b, i) => { const up = b.c >= b.o, c = up ? 'var(--gain)' : 'var(--loss)', x = X(i);
    const tip = `<title>${esc(b.t.replace('T', ' ').slice(5))} ET · O ${pf(b.o)} H ${pf(b.h)} L ${pf(b.l)} C ${pf(b.c)} (${b.c >= b.o ? '+' : ''}${((b.c / b.o - 1) * 100).toFixed(2)}%)</title>`;
    g += `<g><line x1="${x}" x2="${x}" y1="${Y(b.h)}" y2="${Y(b.l)}" stroke="${c}" stroke-width="1"/><rect x="${x - bw / 2}" y="${Y(Math.max(b.o, b.c))}" width="${bw}" height="${Math.max(1, Math.abs(Y(b.o) - Y(b.c)))}" fill="${c}"/>`
      + `<rect x="${x - Math.max(bw, 4) / 2}" y="${Y(b.h)}" width="${Math.max(bw, 4)}" height="${Math.max(2, Y(b.l) - Y(b.h))}" fill="transparent">${tip}</rect></g>`; });
  if (d.evaluatedAt && !(LY.evals && (d.evals || []).length)) { const ei = at(d.evaluatedAt); if (ei != null) { const x = X(ei);
    g += `<line x1="${x}" x2="${x}" y1="${T}" y2="${B}" stroke="var(--coach,#5b5bd6)" stroke-width="2" stroke-dasharray="6 4"/><text x="${Math.min(x + 4, W - Rr - 70)}" y="${T + 12}" font-size="${fs}" font-weight="600" fill="var(--coach,#5b5bd6)">evaluated</text>`; } }
  if (LY.evals && (d.evals || []).length) {   // every evaluation: a flag on top, colored by its decision
    const ecol = e => e.taken ? 'var(--coach,#5b5bd6)' : e.decision === 'BUY' ? 'var(--gain)' : e.decision === 'SKIP' ? 'var(--loss)' : 'var(--warn)';
    const seen = {}, lastLblX = [];
    d.evals.forEach((e, k) => { const i = at(e.t); if (i == null) return; const x = X(i);
      seen[i] = (seen[i] || 0) + 1; const yy = T + 2 + (seen[i] - 1) * 9;
      const sel = S.fcSelE && S.fcSelE.id === id && S.fcSelE.k === k, isCur = e.id === id;
      g += `<line x1="${x}" x2="${x}" y1="${T}" y2="${B}" stroke="${ecol(e)}" stroke-width="${isCur || sel ? 2 : 1.3}" stroke-dasharray="5 4" opacity="${isCur || sel ? .9 : .6}"/>
        <path data-fce="${id}|${k}" d="M${x - 6},${yy} L${x + 6},${yy} L${x},${yy + 10} Z" fill="${ecol(e)}" stroke="${sel ? 'var(--ink)' : 'none'}" stroke-width="1.5" style="cursor:pointer"><title>${esc(e.t.replace('T', ' ').slice(5))} ET · ${esc(e.decision || '')}${e.taken ? ' · traded' : ''}${e.why ? ' · ' + esc(e.why) : ''}</title></path>`;
      // time label under the price area, staggered so close evaluations don't overlap
      let row = 0; while (lastLblX[row] != null && x - lastLblX[row] < fs * 5.2) row++;
      lastLblX[row] = x;
      const ly = B - 6 - row * (fs + 3);
      g += `<text data-fce="${id}|${k}" x="${x + 3}" y="${ly}" font-size="${fs - 2}" font-weight="${isCur ? 700 : 600}" fill="${ecol(e)}" paint-order="stroke" stroke="var(--surface)" stroke-width="3" style="cursor:pointer">${esc(e.t.slice(5, 10).replace('-', '/'))} ${esc(e.t.slice(11, 16))}${e.decision ? ' ' + esc(e.decision[0]) : ''}</text>`; });
  }
  if (walls.length) {                       // gamma walls as step lines: each value holds until the next snapshot
    const t0 = bars[0].t, segs = walls.map((w, k) => { const i0 = w.t <= t0 ? 0 : at(w.t), nx = walls[k + 1];
      return { w, x0: i0 == null ? null : (w.t <= t0 ? L : X(i0)), x1: !nx || bucket(nx.t) > lastT ? W - Rr : (at(nx.t) == null ? null : X(at(nx.t))) }; }).filter(sg => sg.x0 != null && sg.x1 != null && sg.x1 > sg.x0 - 1);
    const lines = [['callWall', 'var(--gain)', '7 4', 'Call wall'], ['putWall', 'var(--loss)', '7 4', 'Put wall'], ['flip', 'var(--coach,#5b5bd6)', '2 3', 'Flip']];
    lines.forEach(([key, col, dash, name]) => {
      let prev = null;
      segs.forEach(sg => { const v = sg.w[key]; if (v == null) { prev = null; return; }
        if (!near(v)) { prev = null; return; }
        const y = Y(v);
        if (prev && Math.abs(prev.y - y) > 0.5) g += `<line x1="${sg.x0}" x2="${sg.x0}" y1="${prev.y}" y2="${y}" stroke="${col}" stroke-width="1.5" stroke-dasharray="${dash}" opacity=".7"/>`;
        g += `<line x1="${sg.x0}" x2="${sg.x1}" y1="${y}" y2="${y}" stroke="${col}" stroke-width="2" stroke-dasharray="${dash}" opacity=".85"><title>${name} ${v} · from ${esc(sg.w.t.replace('T', ' ').slice(5))}</title></line>`;
        prev = { y }; });
      const vis = segs.filter(sg => sg.w[key] != null), lw = vis.length ? vis[vis.length - 1].w[key] : null;
      if (lw == null) return;
      const halo = 'paint-order="stroke" stroke="var(--surface)" stroke-width="4" stroke-linejoin="round"';
      if (near(lw)) g += `<text x="${W - Rr - 4}" y="${Y(lw) - 4}" text-anchor="end" font-size="${fs}" font-weight="600" fill="${col}" ${halo}>${name} ${lw}</text>`;
      else g += `<text x="${W - Rr - 4}" y="${lw > hi ? T + 12 + (key === 'flip' ? fs + 2 : 0) : B - 4 - (key === 'flip' ? fs + 2 : 0)}" text-anchor="end" font-size="${fs}" font-weight="600" fill="${col}" ${halo}>${name} ${lw} ${lw > hi ? '↑' : '↓'}</text>`;
    });
  }
  if (dp && !dp.error) {                    // dark pool: the price levels with the most premium, then each big print
    const prem = v => v >= 1e9 ? `$${(v / 1e9).toFixed(1)}B` : v >= 1e6 ? `$${(v / 1e6).toFixed(0)}M` : `$${Math.round(v / 1e3)}k`;
    const bi = t => { let q = -1; for (let i = 0; i < n; i++) { if (bars[i].t <= t) q = i; else break; } return q; };   // candle of a print
    const md = t => (t || '').slice(5, 10);
    (dp.levels || []).slice(0, S.dpN ?? 5).forEach((lv, j) => { if (!near(lv.price)) return; const y = Y(lv.price), c = 'var(--warn)';
      const i0 = lv.first ? bi(lv.first) : -1, before = lv.first && i0 < 0, x0 = i0 >= 0 ? X(i0) : L;
      const since = lv.first ? ` · since ${md(lv.first)}${before ? ' (before the chart)' : ''}` : '';
      const tip = `Dark pool level ${lv.price}: ${prem(lv.premium)} in ${lv.prints} prints, first ${(lv.first || '').replace('T', ' ')}, last ${(lv.last || '').replace('T', ' ')}`;
      if (i0 > 0) g += `<line x1="${L}" x2="${x0}" y1="${y}" y2="${y}" stroke="${c}" stroke-width="1" stroke-dasharray="2 4" opacity=".3"><title>${esc(tip)} (no print here yet)</title></line>`;
      g += `<line x1="${x0}" x2="${W - Rr}" y1="${y}" y2="${y}" stroke="${c}" stroke-width="${j === 0 ? 3 : 2}" opacity="${j === 0 ? .55 : .35}"><title>${esc(tip)}</title></line>`;
      if (i0 >= 0) g += `<circle cx="${x0}" cy="${y}" r="4.5" fill="${c}" stroke="var(--surface)" stroke-width="1.5"><title>${esc(`First dark pool print at this level: ${lv.first.replace('T', ' ')}`)}</title></circle>`;
      const mx = Math.max(1, ...(lv.times || []).map(x => x[1]));
      (lv.times || []).forEach(([t, pr]) => { const i = bi(t); if (i < 0) return; const h = 3 + 7 * Math.sqrt(pr / mx);
        g += `<line x1="${X(i)}" x2="${X(i)}" y1="${y - h}" y2="${y + h}" stroke="${c}" stroke-width="1.5" opacity=".8"><title>${esc(`Dark pool print ${t.replace('T', ' ')} · ${prem(pr)}`)}</title></line>`; });
      g += `<text x="${Math.max(L + 4, Math.min(x0 + 6, W - Rr - 260))}" y="${y - 4}" font-size="${fs - 1}" font-weight="600" fill="${c}" paint-order="stroke" stroke="var(--surface)" stroke-width="4" stroke-linejoin="round">DP ${lv.price} · ${prem(lv.premium)} · ${lv.prints} prints${since}</text>`; });
    // only the N levels with the most dark pool money are drawn (lines setting, 0-10; no single prints)
  }
  const kcDraw = (rows, kcol, name, wid) => { const km = {}; rows.forEach(x => { km[x.t] = x; });
    ['u', 'm', 'l'].forEach(f => { let path = '', on_ = false;
      bars.forEach((b, i) => { const x = km[b.t], v = x && x[f]; if (v == null || v < lo || v > hi) { on_ = false; return; }
        path += `${on_ ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`; on_ = true; });
      if (path) g += `<path d="${path}" fill="none" stroke="${kcol}" stroke-width="${f === 'm' ? wid * .75 : wid}" ${f === 'm' ? 'stroke-dasharray="4 3"' : ''} opacity=".8"><title>${name} ${f === 'u' ? 'upper band' : f === 'l' ? 'lower band' : 'middle (EMA)'}</title></path>`; });
    const last = km[bars[n - 1].t];
    if (last && last.u >= lo && last.u <= hi) g += `<text x="${X(n - 1) - 4}" y="${Y(last.u) - 4}" text-anchor="end" font-size="${fs - 2}" font-weight="700" fill="${kcol}" paint-order="stroke" stroke="var(--surface)" stroke-width="3">${name}</text>`; };
  if (LY.kc && (d.kc || []).length) kcDraw(d.kc, '#475569', d.kcDaily === false ? 'Keltner (hourly)' : 'Keltner daily', 1.6);   // over-extension
  if (LY.kch && (d.kch || []).length && d.kcDaily !== false) kcDraw(d.kch, '#d97706', d.dt ? 'Keltner' : 'Keltner hourly', 1);                  // entry fine tuning
  if (LY.orb && (d.orb || []).length) {              // opening range (09:30–10:30 candle) for the rest of each day
    const om = {}; d.orb.forEach(x => { om[x.d] = x; });
    let curD = null, i0 = null;
    const flush = (dd, a, z) => { const o = om[dd]; if (!o || a == null || z < a) return;
      [['h', '#16a34a', 'Opening range high'], ['l', '#dc2626', 'Opening range low']].forEach(([f, col, nm]) => { const v = o[f]; if (v < lo || v > hi) return;
        g += `<line x1="${X(a)}" x2="${X(z)}" y1="${Y(v)}" y2="${Y(v)}" stroke="${col}" stroke-width="1.3" stroke-dasharray="5 3" opacity=".8"><title>${nm} ${dd.slice(5)}: ${v}</title></line>`; }); };
    bars.forEach((b, i) => { const dd = b.t.slice(0, 10); if (dd !== curD) { if (curD) flush(curD, i0, i - 1); curD = dd; i0 = null; } if (b.t.slice(11, 16) !== '09:30' && i0 == null) i0 = i; });
    if (curD) flush(curD, i0, n - 1);
  }
  if (LY.inertia && (d.inertia || []).length) {      // daily inertia regime: a strip under the candles
    const im = {}; d.inertia.forEach(x => { im[x.t] = x; });
    const col = { G: '#16a34a', Y: '#eab308', R: '#dc2626' }, nm = { G: 'green: all 3 rising', Y: 'yellow: mixed', R: 'red: all 3 falling' };
    bars.forEach((b, i) => { const x = im[b.t]; if (!x) return;
      g += `<rect x="${X(i) - (W - L - Rr) / nS / 2}" y="${B - 4}" width="${Math.max(1, (W - L - Rr) / nS)}" height="4" fill="${col[x.s]}" opacity=".85"><title>${esc(b.t.replace('T', ' ').slice(5))} · daily inertia ${nm[x.s]} · regime points ${x.pts}</title></rect>`; });
  }
  if (LY.inertiah && !d.daily && (d.inertiaH || []).length) {     // hourly inertia regime: a thin strip above the daily one
    const im = {}; d.inertiaH.forEach(x => { im[x.t] = x; });
    const col = { G: '#16a34a', Y: '#eab308', R: '#dc2626' }, nm = { G: 'green: all 3 rising', Y: 'yellow: mixed', R: 'red: all 3 falling' };
    bars.forEach((b, i) => { const x = im[b.t]; if (!x) return;
      g += `<rect x="${X(i) - (W - L - Rr) / nS / 2}" y="${B - 8}" width="${Math.max(1, (W - L - Rr) / nS)}" height="3" fill="${col[x.s]}" opacity=".6"><title>${esc(b.t.replace('T', ' ').slice(5))} · hourly inertia ${nm[x.s]}</title></rect>`; });
  }
  if (LY.spy && (d.spyDown || []).length) {          // red dots: SPY down over 6 sessions
    const sd = new Set(d.spyDown);
    bars.forEach((b, i) => { if (sd.has(b.t)) g += `<circle cx="${X(i)}" cy="${B - 5}" r="2.2" fill="#dc2626"><title>SPY trending down (vs 6 sessions ago) ${esc(b.t.replace('T', ' ').slice(5))}</title></circle>`; });
  }
  if (LY.ema !== false && (d.ema || []).length) {     // hourly EMA
    const ec = '#c026d3', em = {}; d.ema.forEach(x => { em[x.t] = x.v; });
    let path = '', on_ = false;
    bars.forEach((b, i) => { const v = em[b.t]; if (v == null || v < lo || v > hi) { on_ = false; return; }
      path += `${on_ ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`; on_ = true; });
    if (path) g += `<path d="${path}" fill="none" stroke="${ec}" stroke-width="1.5" opacity=".85"><title>EMA ${d.emaLen || 20} (hourly closes)</title></path>`;
    const lastE = em[bars[n - 1].t];
    if (lastE != null && lastE >= lo && lastE <= hi) g += `<text x="${W - Rr - 4}" y="${Y(lastE) - 6}" text-anchor="end" font-size="${fs - 1}" font-weight="700" fill="${ec}" paint-order="stroke" stroke="var(--surface)" stroke-width="3">EMA${d.emaLen || 20} ${lastE.toFixed(2)}</text>`;
  }
  if (LY.vwap !== false && (d.vwap || []).length) {   // VWAP anchored at the last high-volume candle
    const vc = '#0891b2', vm = {}; (d.vwap || []).forEach(x => { vm[x.t] = x.v; });
    let path = '', prev = null;
    bars.forEach((b, i) => { const v = vm[b.t]; if (v == null || v < lo || v > hi) { prev = null; return; }
      const reset = (d.vwapResets || []).includes(b.t);
      path += `${prev == null || reset ? 'M' : 'L'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`; prev = v; });
    if (path) g += `<path d="${path}" fill="none" stroke="${vc}" stroke-width="1.8" opacity=".9"><title>VWAP (restarts on the candle with the highest volume of the last days set in the settings)</title></path>`;
    (d.vwapResets || []).forEach(t => { const i = idx[t]; if (i == null) return; const v = vm[t]; if (v == null || v < lo || v > hi) return;
      g += `<circle cx="${X(i)}" cy="${Y(v)}" r="3.5" fill="${vc}" stroke="var(--surface)" stroke-width="1"><title>VWAP reset ${esc(t.replace('T', ' ').slice(5))}: highest volume of the period</title></circle>`; });
    const lastV = vm[bars[n - 1].t];
    if (lastV != null && lastV >= lo && lastV <= hi) g += `<text x="${W - Rr - 4}" y="${Y(lastV) + 14}" text-anchor="end" font-size="${fs - 1}" font-weight="700" fill="${vc}" paint-order="stroke" stroke="var(--surface)" stroke-width="3">VWAP ${lastV.toFixed(2)}</text>`;
  }
  if (LY.mp !== false && ((d.expirations || []).length || (d.maxPain || []).length)) {   // monthly expirations + max pain
    const mpc = 'var(--warn,#d97706)', bwH = (W - L - Rr) / nS / 2;
    (d.expirations || []).forEach(e => { let i = null; bars.forEach((b, q) => { if (b.t.slice(0, 10) === e) i = q; });
      if (i == null) { if (e > lastT.slice(0, 10) && e === (d.expirations || []).find(x => x > lastT.slice(0, 10)))
          g += `<text x="${W - Rr - 4}" y="${B - 6}" text-anchor="end" font-size="${fs - 2}" font-weight="700" fill="${mpc}" paint-order="stroke" stroke="var(--surface)" stroke-width="3">next monthly exp ${e.slice(5)}</text>`;
        return; }
      const x = X(i) + bwH;
      g += `<line x1="${x}" x2="${x}" y1="${T}" y2="${B}" stroke="${mpc}" stroke-width="1.5" stroke-dasharray="6 3" opacity=".8"><title>Monthly options expiration ${e}</title></line>
        <text x="${x - 3}" y="${T + 12}" text-anchor="end" font-size="${fs - 2}" font-weight="700" fill="${mpc}" paint-order="stroke" stroke="var(--surface)" stroke-width="3">OPEX ${e.slice(5)}</text>`; });
    const mps = d.maxPain || [];
    mps.forEach((m, q) => { if (m.maxPain < lo || m.maxPain > hi) return;
      const i0 = idx[m.t] ?? at(m.t); if (i0 == null) return;
      const nx = mps[q + 1], i1 = nx ? (idx[nx.t] ?? at(nx.t)) : n;
      const x0 = X(i0) - bwH, x1 = i1 == null ? W - Rr : i1 >= n ? X(n - 1) + bwH : X(i1) - bwH, y = Y(m.maxPain);
      g += `<line x1="${x0}" x2="${x1}" y1="${y}" y2="${y}" stroke="${mpc}" stroke-width="2" stroke-dasharray="2 2"><title>Max pain ${m.maxPain} for the ${m.exp} expiration (as of ${m.t.slice(0, 10)})</title></line>`;
      if (nx && nx.maxPain >= lo && nx.maxPain <= hi && i1 != null && i1 < n) g += `<line x1="${x1}" x2="${x1}" y1="${y}" y2="${Y(nx.maxPain)}" stroke="${mpc}" stroke-width="1" opacity=".5"/>`;
      if (!nx) g += `<text x="${W - Rr - 4}" y="${y - 4}" text-anchor="end" font-size="${fs - 1}" font-weight="700" fill="${mpc}" paint-order="stroke" stroke="var(--surface)" stroke-width="3">Max pain ${m.maxPain} (${m.exp.slice(5)})</text>`; });
  }
  if ((d.trades || []).length) {                  // the real (paper) option trades placed from the signals
    (d.trades || []).forEach(tr => {
      const i0 = tr.at ? at(tr.at) : null, y0v = tr.under;
      if (i0 != null && y0v != null && y0v >= lo && y0v <= hi) { const x = X(i0), y = Y(y0v);
        g += `<path d="M${x},${y - 14} L${x + 8},${y - 6} L${x},${y + 2} L${x - 8},${y - 6} Z" fill="var(--coach,#5b5bd6)" stroke="var(--ink)" stroke-width="1.5"><title>BOUGHT ${esc(tr.contract || '')} ×${tr.qty ?? ''} at ${tr.fill ?? tr.limit ?? '—'} · ${esc((tr.at || '').replace('T', ' ').slice(5))} · ${esc(tr.status || '')}</title></path>
          <text x="${x}" y="${y - 18}" text-anchor="middle" font-size="${fs - 2}" font-weight="800" fill="var(--coach,#5b5bd6)" paint-order="stroke" stroke="var(--surface)" stroke-width="3">BUY ${tr.qty ?? ''}× ${tr.strike ?? ''}C @${tr.fill ?? tr.limit ?? '—'}</text>`; }
      const i1 = tr.exitAt ? at(tr.exitAt) : null, y1v = tr.underExit;
      if (i1 != null && tr.status === 'closed' && y1v != null && y1v >= lo && y1v <= hi) { const x = X(i1), y = Y(y1v), c = (tr.pl || 0) >= 0 ? 'var(--gain)' : 'var(--loss)';
        g += `<path d="M${x},${y + 14} L${x + 8},${y + 6} L${x},${y - 2} L${x - 8},${y + 6} Z" fill="${c}" stroke="var(--ink)" stroke-width="1.5"><title>SOLD at ${tr.exit ?? '—'} · ${esc(tr.exitReason || '')}</title></path>
          <text x="${x}" y="${y + 28}" text-anchor="middle" font-size="${fs - 2}" font-weight="800" fill="${c}" paint-order="stroke" stroke="var(--surface)" stroke-width="3">SELL @${tr.exit ?? '—'}${tr.plPct != null ? ` ${tr.plPct >= 0 ? '+' : ''}${tr.plPct}%` : ''}</text>`; }
    });
  }
  {                                                    // day-trading studies: EMA bands, line in the sand, pre-market zone, fibs, squeeze turns
    const span = {}; bars.forEach((b, i) => { const dd = b.t.slice(0, 10); if (!span[dd]) span[dd] = [i, i]; else span[dd][1] = i; });
    const hline = (dd, v, col, wid, dash, lab, op) => { const sp = span[dd]; if (!sp || v == null || v < lo || v > hi) return; const x0 = X(sp[0]) - bw, x1 = X(sp[1]) + bw;
      g += `<line x1="${x0}" x2="${x1}" y1="${Y(v)}" y2="${Y(v)}" stroke="${col}" stroke-width="${wid}" ${dash ? `stroke-dasharray="${dash}"` : ''} opacity="${op || .8}"><title>${esc(lab)} ${v}</title></line>`
        + (lab ? `<text x="${x1 - 4}" y="${Y(v) - 3}" text-anchor="end" font-size="${fs - 2}" fill="${col}" paint-order="stroke" stroke="var(--surface)" stroke-width="3">${esc(lab)}</text>` : ''); };
    const arrow = (i, up, y, col, tip) => { const x = X(i), z = Math.max(4, Math.min(7, bw));
      g += `<path d="${up ? `M${x},${y} L${x - z},${y + z * 1.6} L${x + z},${y + z * 1.6} Z` : `M${x},${y} L${x - z},${y - z * 1.6} L${x + z},${y - z * 1.6} Z`}" fill="${col}" stroke="var(--surface)" stroke-width="1"><title>${esc(tip)}</title></path>`; };
    if (LY.pmz && d.pmz) Object.entries(d.pmz).forEach(([dd, z]) => { const sp = span[dd]; if (!sp) return; const y0 = Y(Math.min(hi, Math.max(z.up, z.lo))), y1 = Y(Math.max(lo, Math.min(z.up, z.lo)));
      if (y1 <= y0) return; g += `<rect x="${X(sp[0]) - bw}" y="${y0}" width="${X(sp[1]) - X(sp[0]) + 2 * bw}" height="${y1 - y0}" fill="${z.gap ? '#06b6d4' : '#dc2626'}" opacity=".14"><title>${esc(`Pre-market zone ${dd.slice(5)}: ${z.lo} – ${z.up} (pre-market ${z.l} – ${z.h}, gap ${z.gap ? 'up' : 'down'}) · risk ${(Math.abs(z.up - z.lo)).toFixed(2)}`)}</title></rect>`; });
    if (LY.fibs && d.fibs) Object.entries(d.fibs).forEach(([dd, lv]) => lv.forEach(([nm, v]) => hline(dd, v, 'var(--muted)', 1, nm === 'high' || nm === 'low' ? '' : '4 3', nm, .7)));
    if (LY.bands && (d.bands || []).length) {
      const bi = d.bands.map(x => [idx[x.t], x]).filter(([i]) => i != null);
      for (let q = 1; q < bi.length; q++) { const [i0, a] = bi[q - 1], [i1, b] = bi[q];
        g += `<path d="M${X(i0)},${Y(a.sl)} L${X(i1)},${Y(b.sl)} L${X(i1)},${Y(b.tr)} L${X(i0)},${Y(a.tr)} Z" fill="${b.sl >= b.tr ? '#16a34a' : '#dc2626'}" opacity=".1"/>`; }
      [['sf', '#16a34a', 'EMA 9'], ['sl', '#ca8a04', 'EMA 22'], ['tr', '#a855f7', 'EMA 44']].forEach(([f, col, nm]) => {
        g += `<path d="${bi.map(([i, x], q) => `${q ? 'L' : 'M'}${X(i).toFixed(1)},${Y(x[f]).toFixed(1)}`).join('')}" fill="none" stroke="${col}" stroke-width="1.3" opacity=".85"><title>${nm}</title></path>`; });
      (d.bandsF || []).forEach(x => { const i = idx[x.t]; if (i == null) return; const b = bars[i];
        arrow(i, x.dir > 0, x.dir > 0 ? Y(b.l) + 4 : Y(b.h) - 4, x.dir > 0 ? '#16a34a' : '#dc2626', `${x.dir > 0 ? 'Bullish' : 'Bearish'} candle formation (EMA bands aligned) ${x.t.replace('T', ' ').slice(5)}`); });
    }
    if (LY.lis && d.lis) {
      Object.entries(d.lis).forEach(([dd, v]) => hline(dd, v, '#0891b2', 2, '', 'LIS ' + (+v).toFixed(2), .9));
      (d.lisF || []).forEach(x => { const i = at(x.t); if (i == null) return; const b = bars[i];
        arrow(i, x.dir > 0, x.dir > 0 ? Y(b.l) + 14 : Y(b.h) - 14, x.strong ? (x.dir > 0 ? '#16a34a' : '#dc2626') : '#94a3b8', `${x.dir > 0 ? 'Bullish' : 'Bearish'} formation off the LIS (5-min) ${x.t.replace('T', ' ').slice(5)}${x.strong ? ' · EMAs aligned' : ''}`); });
    }
    if (LY.turn) {
      let tu = d.turns;
      if (!tu && (d.sqzH || d.sqzD)) { const src = d.daily ? (d.sqzD || []) : (d.sqzH || []); tu = [];
        for (let q = 2; q < src.length; q++) { const a = src[q - 2].m, b = src[q - 1].m, c = src[q].m;
          if (c > 0 && c < b && a < b) tu.push({ t: src[q].t, dir: -1 }); else if (c < 0 && c > b && a > b) tu.push({ t: src[q].t, dir: 1 }); } }
      (tu || []).forEach(x => { const i = d.daily ? bars.findIndex(b => b.t.slice(0, 10) === x.t.slice(0, 10)) : idx[x.t] ?? at(x.t); if (i == null || i < 0) return; const b = bars[i];
        arrow(i, x.dir > 0, x.dir > 0 ? Y(b.l) + 26 : Y(b.h) - 26, x.dir > 0 ? '#eab308' : '#06b6d4', `Squeeze turn ${x.dir > 0 ? 'up' : 'down'} ${x.t.replace('T', ' ').slice(5)}`); });
    }
  }
  if (LY.sig !== false && (d.misses || []).length) {    // candles at the support that didn't enter: why
    const byBar = {};
    d.misses.forEach(m => { const i = idx[m.t]; if (i == null) return; (byBar[i] = byBar[i] || []).push(m); });
    Object.entries(byBar).forEach(([i, ms]) => { const b = bars[+i], y = Math.min(B - 4, Y(b.l) + 11), x = X(+i);
      const why = ms.map(m => m.r).join(' · '), alert = ms.every(m => m.r.startsWith('no call alert'));
      g += `<g opacity="${alert ? .45 : .85}"><path d="M${x - 3.5},${y - 3.5}L${x + 3.5},${y + 3.5}M${x - 3.5},${y + 3.5}L${x + 3.5},${y - 3.5}" stroke="${alert ? 'var(--muted)' : '#d97706'}" stroke-width="1.8"/><circle cx="${x}" cy="${y}" r="7" fill="transparent"><title>${esc(`No entry ${b.t.replace('T', ' ').slice(5)}: ${why}`)}</title></circle></g>`; });
  }
  if (LY.sig !== false && (d.signals || []).length) {   // Wall watch: entry, target (call wall), stop (put wall), exit
    d.signals.forEach((sg, k) => {
      const i0 = at(sg.at); let i1 = sg.exitAt ? at(sg.exitAt) : n - 1;
      if (sg.exitAt && i1 == null && sg.exitAt > lastT) i1 = n - 1;
      if (i0 == null && !(sg.exitAt && sg.exitAt >= bars[0].t && sg.at < bars[0].t)) return;
      const a0 = i0 == null ? 0 : i0, b0 = i1 == null ? a0 : i1, xa = i0 == null ? L : X(a0), xb = X(b0);
      const col = sg.status === 'target' || (['trail', 'flip', 'vwap', 'ema', 'keltner', 'lost', 'eodpw', 'fvg', 'breakeven', 'inertia', 'inertiaH', 'squeeze', 'pwlost', 'cvd'].includes(sg.status) && sg.pct >= 0) ? 'var(--gain)' : ['stop', 'trail', 'flip', 'vwap', 'ema', 'keltner', 'lost', 'eodpw', 'fvg', 'breakeven', 'inertia', 'inertiaH', 'squeeze', 'pwlost', 'cvd'].includes(sg.status) ? 'var(--loss)' : 'var(--coach,#5b5bd6)';
      const sel = S.wwSel && S.wwSel.sym === d.symbol && S.wwSel.at === sg.at;
      const dl = (sg.dlog || []).map(([dd, c, ku, fl, rid]) => `${dd.slice(5)} close ${c}` + (ku != null ? ` · upper Keltner ${ku}${c > ku ? ' (above)' : rid ? ' (under: ride ends)' : ''}` : '') + (fl != null ? ` · flip ${fl}${c < fl ? (sg.aboveFlipAtEntry ? ' (under)' : ' (under, but the trade was entered under the flip: no flip exit)') : ''}` : ''));
      const why = sg.status === 'open' ? `Open since ${sg.at.replace('T', ' ').slice(5)} · stop ${sg.stop}${sg.v && d.curV && sg.v !== d.curV ? ' · computed with older settings (press Get walls to recompute)' : ''}\nDaily closes:\n${dl.join('\n')}` : `${sg.status} ${(sg.exitAt || '').replace('T', ' ').slice(5)} · ${sg.pct >= 0 ? '+' : ''}${sg.pct}%\nDaily closes:\n${dl.join('\n')}`;
      g += `<rect x="${xa}" y="${T}" width="${Math.max(2, xb - xa)}" height="${B - T}" fill="${col}" opacity="${sel ? .12 : .06}"><title>${esc(why)}</title></rect>`;
      if (sg.target != null && sg.target >= lo && sg.target <= hi) g += `<line x1="${xa}" x2="${xb}" y1="${Y(sg.target)}" y2="${Y(sg.target)}" stroke="var(--gain)" stroke-width="2" opacity=".85"><title>target ${sg.target} (call wall ${sg.cw})</title></line>`;
      const steps = sg.stops || [[sg.at, sg.stop0 ?? sg.stop ?? sg.pw]];       // trailing stop: one segment per level
      steps.forEach(([t1, v], q) => { if (v == null || v < lo || v > hi) return;
        const s0 = Math.max(xa, at(t1) == null ? xa : X(at(t1)) - (q ? (W - L - Rr) / nS / 2 : 0)), nx = steps[q + 1], e0 = nx && at(nx[0]) != null ? X(at(nx[0])) - (W - L - Rr) / nS / 2 : xb;
        g += `<line x1="${s0}" x2="${Math.max(s0, e0)}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--loss)" stroke-width="2" opacity=".85"><title>${q ? 'trailing stop' : 'stop'} ${v}</title></line>`;
        if (nx && nx[1] >= lo && nx[1] <= hi) g += `<line x1="${e0}" x2="${e0}" y1="${Y(v)}" y2="${Y(nx[1])}" stroke="var(--loss)" stroke-width="1" opacity=".6"/>`; });
      if (i0 != null && sg.entry >= lo && sg.entry <= hi) { const y = Y(sg.entry);
        g += `<path data-wwsel="${esc(d.symbol)}|${esc(sg.at)}" d="M${xa},${y + 5} L${xa - 8},${y + 18} L${xa + 8},${y + 18} Z" fill="var(--coach,#5b5bd6)" stroke="var(--surface)" stroke-width="1.5" style="cursor:pointer"><title>${sg.kind === 'day' ? `Day trade ${sg.dir} · rules: ${(sg.rules || []).join(', ')}` : sg.kind === 'flip' ? 'Flip setup' : sg.kind === 'merged' ? 'Merged walls setup' : sg.kind === 'retest' ? `Flow retest of the ${sg.levelName || 'level'} ${sg.level}` : 'PW setup'} ${esc(sg.at.replace('T', ' ').slice(5))} · entry ${(+sg.entry).toFixed(2)} · put wall ${sg.pw} · flip ${sg.flip} · call wall ${sg.cw}</title></path>
          <text x="${xa}" y="${y + 31}" text-anchor="middle" font-size="${fs - 2}" font-weight="700" fill="var(--coach,#5b5bd6)" paint-order="stroke" stroke="var(--surface)" stroke-width="3">${sg.kind === 'day' ? (sg.side < 0 ? 'SHORT' : 'LONG') : sg.kind === 'flip' ? 'FLIP' : sg.kind === 'merged' ? 'MRG' : sg.kind === 'retest' ? 'RT' : 'IN'}</text>`; }
      if (sg.kind === 'retest' && sg.armedAt) { const ia = at(sg.armedAt); if (ia != null && sg.level >= lo && sg.level <= hi) { const xa2 = X(ia), yl = Y(sg.level);
        g += `<line x1="${xa2}" x2="${xa}" y1="${yl}" y2="${yl}" stroke="var(--coach,#5b5bd6)" stroke-width="1.5" stroke-dasharray="3 3"><title>Waiting for the retest of the ${esc(sg.levelName || 'level')} ${sg.level}</title></line>
          <text x="${xa2}" y="${yl - 6}" font-size="${fs - 2}" font-weight="700" fill="var(--coach,#5b5bd6)" paint-order="stroke" stroke="var(--surface)" stroke-width="3">⚑ ${money(sg.armPremium || 0, false)} calls${sg.armX ? ` · ${sg.armX}× usual` : ''}</text>`; } }
      if (sg.fvgZone && sg.fvgZone.hi >= lo && sg.fvgZone.lo <= hi && i0 != null) {    // the gap the entry used: a bracket at the entry candle
        const y0 = Y(Math.min(hi, sg.fvgZone.hi)), y1 = Y(Math.max(lo, sg.fvgZone.lo)), xz = xa - Math.max(bw, 4) / 2 - 3;
        g += `<path d="M${xz + 4},${y0} L${xz},${y0} L${xz},${y1} L${xz + 4},${y1}" fill="none" stroke="#15803d" stroke-width="2"><title>${esc(`Bullish FVG used by the entry ${sg.at.replace('T', ' ').slice(5)}: ${sg.fvgZone.lo}–${sg.fvgZone.hi}, formed ${sg.fvgZone.t.replace('T', ' ').slice(5)}`)}</title></path>`; }
      if (sg.scaled && typeof sg.scaled === 'object' && sg.scaled.at) { const ip = at(sg.scaled.at); if (ip != null && sg.scaled.px >= lo && sg.scaled.px <= hi) { const xp = X(ip), yp = Y(sg.scaled.px);
        g += `<circle cx="${xp}" cy="${yp}" r="5.5" fill="var(--gain)" stroke="var(--surface)" stroke-width="1.5"><title>${esc(`Sold ${Math.round(sg.scaled.frac * 100)}% at ${sg.scaled.px} (${sg.scaled.at.replace('T', ' ').slice(5)}) · +${((sg.scaled.px / sg.entry - 1) * 100).toFixed(2)}%${sg.scaleBE ? ' · stop of the rest moved to the entry' : ''}`)}</title></circle>
          <text x="${xp + 7}" y="${yp - 6}" font-size="${fs - 2}" font-weight="700" fill="var(--gain)" paint-order="stroke" stroke="var(--surface)" stroke-width="3">${Math.round(sg.scaled.frac * 100)}%</text>`; } }
      if (sg.status !== 'open' && i1 != null && sg.exit >= lo && sg.exit <= hi) { const y = Y(sg.exit);
        g += `<rect x="${xb - 6}" y="${y - 6}" width="12" height="12" fill="${col}" stroke="var(--surface)" stroke-width="1.5"><title>${sg.exitWhy ? ({ inRiseX: 'Daily inertia rising', inhRiseX: 'Hourly inertia rising', inhRedX: 'Hourly inertia falling', sqhRiseX: 'Hourly squeeze momentum rising', sqdRiseX: 'Daily squeeze momentum rising', cvdRiseX: 'CVD rising', cvdFallX: 'CVD falling' }[sg.exitWhy] || sg.exitWhy) : sg.status === 'pwlost' ? `Day closed under the ${sg.lostLevel || 'put wall'}` : sg.status === 'squeeze' ? 'Squeeze momentum turned down' : sg.status === 'inertiaH' ? 'Hourly inertia no longer green' : sg.status === 'inertia' ? 'Daily inertia exit (daily close)' : sg.status === 'breakeven' ? 'Rest stopped at the entry (after the partial sale)' : sg.status === 'fvg' ? 'New bearish FVG' : sg.status === 'eodpw' ? 'Entry day closed under the put wall' : sg.status === 'lost' ? `Passed the ${sg.lostLevel || 'level'}, the day closed back under it` : sg.status === 'keltner' ? 'Keltner ride ended: daily close back under the upper band' : sg.status === 'vwap' ? 'Hourly close under the VWAP' : sg.status === 'ema' ? 'Hourly close under the EMA' : sg.status === 'flip' ? 'Day closed under the gamma flip' : sg.status === 'target' ? 'Target: call wall' : sg.status === 'trail' ? `Trailing stop ${sg.stop} (${sg.kind === 'merged' ? 'call wall of the day − N × ATR' : 'highest high − N × 30-day ATR'})` : `Stop (${sg.kind === 'flip' ? 'flip' : sg.kind === 'merged' ? 'merged wall' : 'put wall'} − N × 30-day ATR) ${sg.stop ?? sg.pw}`} ${esc((sg.exitAt || '').replace('T', ' ').slice(5))} at ${(+sg.exit).toFixed(2)} · ${sg.pct >= 0 ? '+' : ''}${sg.pct}%</title></rect>
          <text x="${xb + 8}" y="${y + 4}" font-size="${fs - 1}" font-weight="700" fill="${col}" paint-order="stroke" stroke="var(--surface)" stroke-width="3">${sg.pct >= 0 ? '+' : ''}${sg.pct}%</text>`; }
    });
  }
  const stack = {};
  (LY.flow ? d.alerts || [] : []).forEach((a, k) => { const i = at(a.t); if (i == null) return; const b = bars[i], call = a.type === 'call';
    const key = `${i}${a.type}`; stack[key] = (stack[key] || 0) + 1;
    const r = Math.min(13, 2.5 + Math.sqrt(a.premium / 100000) * 3.2);
    const y = call ? Y(b.l) + 6 + r + (stack[key] - 1) * 5 : Y(b.h) - 6 - r - (stack[key] - 1) * 5;
    const sel = S.fcSel && S.fcSel.id === id && S.fcSel.k === k;
    g += `<circle data-fci="${id}|${k}" cx="${X(i)}" cy="${y}" r="${sel ? r + 3 : r}" fill="${call ? 'var(--gain)' : 'var(--loss)'}" fill-opacity="${a.big ? .75 : .38}" stroke="${a.big || sel ? 'var(--ink)' : 'none'}" stroke-width="${sel ? 2 : 1}" style="cursor:pointer"><title>${esc(a.t.replace('T', ' ').slice(5))} · ${a.type.toUpperCase()} ${money(a.premium, false)} · ${esc(a.contract || '')}</title></circle>`; });
  // accumulation: running total of call and put premium bought at the ask, bar by bar
  if (AH) {
  const cAcc = new Array(n).fill(0), pAcc = new Array(n).fill(0);
  (d.alerts || []).forEach(a => { const i = at(a.t); if (i == null) return; (a.type === 'call' ? cAcc : pAcc)[i] += a.premium; });
  for (let i = 1; i < n; i++) { cAcc[i] += cAcc[i - 1]; pAcc[i] += pAcc[i - 1]; }
  const amax = Math.max(1, cAcc[n - 1], pAcc[n - 1]), AY = v => AB - v / amax * (AB - AT - 14);
  g += `<line x1="${L}" x2="${W - Rr}" y1="${AB}" y2="${AB}" stroke="var(--line)"/><text x="${L + 2}" y="${AT + 10}" font-size="${fs}" fill="var(--muted)">Premium bought at the ask, running total</text>`;
  dk = -1;
  bars.forEach((b, i) => { if (i === 0 || b.t.slice(0, 10) !== bars[i - 1].t.slice(0, 10)) { dk++; if (!i || dk % every) return; const x = X(i) - (W - L - Rr) / nS / 2; g += `<line x1="${x}" x2="${x}" y1="${AT}" y2="${AB}" stroke="var(--line)" stroke-dasharray="2 3"/>`; } });
  const path = arr => arr.map((v, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${AY(v).toFixed(1)}`).join('');
  g += `<path d="${path(cAcc)}" fill="none" stroke="var(--gain)" stroke-width="2"/><path d="${path(pAcc)}" fill="none" stroke="var(--loss)" stroke-width="2"/>`;
  const km = v => v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v / 1e3)}k`;
  const yc = AY(cAcc[n - 1]), yp = AY(pAcc[n - 1]), clash = Math.abs(yc - yp) < fs;
  g += `<text x="${W - Rr + 6}" y="${(clash && yc >= yp ? yc + fs : yc) + 4}" font-size="${fs}" fill="var(--gain)">${km(cAcc[n - 1])}</text><text x="${W - Rr + 6}" y="${(clash && yp > yc ? yp + fs : yp) + 4}" font-size="${fs}" fill="var(--loss)">${km(pAcc[n - 1])}</text>`;
  }
  if (SH) {                                            // TTM squeeze: momentum histogram + squeeze dots on the zero line
    const daily = !!d.daily, src = daily ? (d.sqzD || []) : (d.sqzH || []), mp = {};
    src.forEach(x => { mp[daily ? x.t.slice(0, 10) : x.t] = x; });
    const get = b => mp[daily ? b.t.slice(0, 10) : b.t];
    const dm = {}; (d.sqzD || []).forEach(x => { dm[x.t.slice(0, 10)] = x; });
    const vals = bars.map(get).filter(Boolean).map(x => Math.abs(x.m)), mx = Math.max(1e-9, ...vals);
    const Z = (ST + 14 + SB) / 2, MY = v => Z - v / mx * ((SB - ST - 14) / 2), cw = Math.max(1, (W - L - Rr) / nS), hw = Math.max(1, cw * 0.7);
    g += `<line x1="${L}" x2="${W - Rr}" y1="${SB}" y2="${SB}" stroke="var(--line)"/><line x1="${L}" x2="${W - Rr}" y1="${Z}" y2="${Z}" stroke="var(--line)"/>`
      + `<text x="${L + 2}" y="${ST + 10}" font-size="${fs}" fill="var(--muted)">Squeeze ${daily ? 'daily' : d.dt ? (d.interval || 1) + 'm' : '1h'} momentum · dots: red = squeeze on, green = off${daily || d.dt ? '' : ' · top strip: daily squeeze'}</text>`;
    bars.forEach((b, i) => { const x = get(b); if (!x) return;
      const col = x.m >= 0 ? (x.u ? '#06b6d4' : '#1d4ed8') : (x.u ? '#eab308' : '#dc2626'), y = MY(x.m);
      g += `<rect x="${X(i) - hw / 2}" y="${Math.min(y, Z)}" width="${hw}" height="${Math.max(1, Math.abs(y - Z))}" fill="${col}"><title>${esc(b.t.replace('T', ' ').slice(5))} · squeeze ${x.on ? 'ON' : 'off'}${x.f ? ' · fired' : ''} · momentum ${x.m} ${x.u ? 'rising' : 'falling'}</title></rect>`
        + `<circle cx="${X(i)}" cy="${Z}" r="${Math.max(1.2, Math.min(2.4, cw / 2.6))}" fill="${x.on ? '#dc2626' : '#16a34a'}"/>`; });
    if (!daily) bars.forEach((b, i) => { const x = dm[b.t.slice(0, 10)]; if (x) g += `<rect x="${X(i) - cw / 2}" y="${ST + 13}" width="${cw}" height="3" fill="${x.on ? '#dc2626' : '#16a34a'}" opacity=".6"/>`; });
    g += `<text x="${W - Rr + 6}" y="${MY(mx) + 8}" font-size="${fs - 1}" fill="var(--muted)">+${+mx.toPrecision(3)}</text><text x="${W - Rr + 6}" y="${Z + 4}" font-size="${fs - 1}" fill="var(--muted)">0</text>`;
  }
  if (LY.cvd && (d.big || []).length) {               // the largest trades of each day (real order flow)
    const mxn = Math.max(...d.big.map(x => x[1] * x[2]));
    d.big.forEach(([t, p, sz, side]) => { const i = at(t); if (i == null || p < lo || p > hi) return;
      const r = 3 + 9 * Math.sqrt(p * sz / mxn), c = side > 0 ? '#16a34a' : '#dc2626';
      g += `<circle cx="${X(i)}" cy="${Y(p)}" r="${r.toFixed(1)}" fill="${c}" fill-opacity=".28" stroke="${c}" stroke-width="1.2"><title>${esc(`${t.replace('T', ' ').slice(5)} · ${side > 0 ? 'BUY' : 'SELL'} ${sz.toLocaleString()} @ ${p} · $${(p * sz / 1e6).toFixed(2)}M`)}</title></circle>`; });
  }
  if (VH) {                                            // buy / sell pressure, estimated from each candle: the close's place in its range
    const real = LY.cvd && d.cvd ? d.cvd : null;           // real order flow: trades classified one by one (per minute)
    const addMin = (t, k) => { const dt0 = new Date(t + ':00Z'); dt0.setUTCMinutes(dt0.getUTCMinutes() + k); return dt0.toISOString().slice(0, 16); };
    let nReal = 0;
    const prAll = all.map((b, i) => { const v = +b.v || 0, r = b.h - b.l, pc = i ? all[i - 1].c : b.o;
      if (real && real[b.t.slice(0, 10)]) { let buy = 0, sell = 0; const mm = real[b.t.slice(0, 10)];
        for (let k = 0; k < (d.interval || 1); k++) { const x = mm[addMin(b.t, k)]; if (x) { buy += x[0]; sell += x[1]; } }
        nReal++; return { buy, sell, d: buy - sell, real: true }; }
      const buy = r > 0 ? v * (b.c - b.l) / r : (b.c > pc ? v : b.c < pc ? 0 : v / 2); return { buy, sell: v - buy, d: 2 * buy - v }; });
    const day = b => d.daily ? '' : b.t.slice(0, 10);
    const rollN = opts && opts.cvdN ? Math.max(1, Math.round(opts.cvdN)) : 0;   // swing charts: sum of the last N candles (= the CVD rules)
    let cum = 0, lastDay = null; const cdAll = all.map((b, i) => { if (rollN) { cum += prAll[i].d - (i >= rollN ? prAll[i - rollN].d : 0); return cum; }
      if (day(b) !== lastDay) { cum = 0; lastDay = day(b); } cum += prAll[i].d; return cum; });
    const pr = prAll.slice(za, zb + 1), cd = cdAll.slice(za, zb + 1);   // from each day's open even when zoomed in
    nReal = pr.filter(x => x.real).length;
    // drawn like the squeeze: a histogram of the CVD around 0, colour = side of 0 and direction vs the candle before
    const prevCd = i => { const q = za + i; return q > 0 && (rollN || day(all[q]) === day(all[q - 1])) ? cdAll[q - 1] : 0; };
    const mx = Math.max(1e-9, ...cd.map(v => Math.abs(v)));
    const Z = (VT + 14 + VB) / 2, CY = v => Z - v / mx * ((VB - VT - 14) / 2);
    const cw = Math.max(1, (W - L - Rr) / nS), hw = Math.max(1, cw * 0.7), last = pr.slice(-20);
    const tb = last.reduce((a, x) => a + x.buy, 0), ts = last.reduce((a, x) => a + x.sell, 0), share = tb + ts > 0 ? Math.round(tb / (tb + ts) * 100) : 50;
    const who = share >= 55 ? `<tspan fill="var(--gain)" font-weight="700">buyers in charge ${share}%</tspan>` : share <= 45 ? `<tspan fill="var(--loss)" font-weight="700">sellers in charge ${100 - share}%</tspan>` : `<tspan font-weight="700">balanced ${share}% buy</tspan>`;
    g += `<line x1="${L}" x2="${W - Rr}" y1="${VB}" y2="${VB}" stroke="var(--line)"/><line x1="${L}" x2="${W - Rr}" y1="${Z}" y2="${Z}" stroke="var(--line)"/>`
      + `<text x="${L + 2}" y="${VT + 10}" font-size="${fs}" fill="var(--muted)">CVD ${nReal ? (nReal === n ? '(REAL: every trade classified buy / sell)' : '(real where loaded, estimated elsewhere)') : '(estimated: volume split by where each candle closes in its range)'}${d.flowLoading ? ' · loading the trades…' : ''} · ${rollN ? `sum of the last ${rollN} candles` : `running total${d.daily ? '' : ' from each day\'s open'}`} · cyan / blue = above 0 rising / falling, yellow / red = below 0 rising / falling · dots = the entries · last 20 candles: ${who}</text>`;
    bars.forEach((b, i) => { const v = cd[i], up = v > prevCd(i), y = CY(v);
      const col = v >= 0 ? (up ? '#06b6d4' : '#1d4ed8') : (up ? '#eab308' : '#dc2626');
      g += `<rect x="${X(i) - hw / 2}" y="${Math.min(y, Z)}" width="${hw}" height="${Math.max(1, Math.abs(y - Z))}" fill="${col}"><title>${esc(b.t.replace('T', ' ').slice(5))} · CVD ${Math.round(v).toLocaleString()} ${up ? 'rising' : 'falling'} · this candle: buy ≈ ${Math.round(pr[i].buy).toLocaleString()} · sell ≈ ${Math.round(pr[i].sell).toLocaleString()}</title></rect>`; });
    g += `<text x="${W - Rr + 6}" y="${Z + 4}" font-size="${fs - 1}" fill="var(--muted)">0</text>`;
    if (d.signals && LY.sig !== false) d.signals.forEach(sg => { const i = idx[sg.at] ?? at(sg.at); if (i == null) return;   // where the CVD was at each entry
      g += `<circle cx="${X(i)}" cy="${CY(cd[i])}" r="3.5" fill="${sg.side < 0 ? '#dc2626' : '#16a34a'}" stroke="var(--surface)" stroke-width="1"><title>${esc(`${sg.side < 0 ? 'SHORT entry' : 'LONG entry'}${sg.kind && sg.kind !== 'day' ? ' (' + ({ retest: 'retest', flip: 'flip', merged: 'merged' }[sg.kind] || sg.kind) + ')' : ''} ${sg.at.replace('T', ' ').slice(5)} · CVD ${Math.round(cd[i]).toLocaleString()} (${cd[i] > 0 ? 'above' : 'below'} 0, ${cd[i] > prevCd(i) ? 'rising' : 'falling'})`)}</title></circle>`; });
    const fmt = v => Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : Math.abs(v) >= 1e3 ? (v / 1e3).toFixed(0) + 'k' : Math.round(v);
    g += `<text x="${W - Rr + 6}" y="${CY(cd[cd.length - 1]) + 4}" font-size="${fs - 1}" fill="var(--coach,#5b5bd6)">${cd[cd.length - 1] >= 0 ? '+' : ''}${fmt(cd[cd.length - 1])}</text>`;
  }
  if (LY.pwv !== false && (d.pwVwap || []).length) {  // VWAP reset candle touching the put wall: highlighted on top of everything
    d.pwVwap.forEach(e => { const i = idx[e.t]; if (i == null) return;
      const sh = e.side === 'short', lv = sh ? e.cw : e.pw, x = X(i), c = sh ? '#dc2626' : '#16a34a', cw = Math.max(6, bw + 4);
      const yl = sh ? Math.max(T + 10, Y(e.h != null ? e.h : lv) - 10) : Math.min(B - 2, Y(e.l != null ? e.l : lv) + 10), yp = Y(lv);
      const tip = esc(`${sh ? 'SHORT · VWAP reset on the call wall' : 'LONG · VWAP reset on the put wall'} ${e.t.replace('T', ' ').slice(5)}\nPut wall ${e.pw} · Call wall ${e.cw ?? '—'}${e.l != null ? `\nO ${e.o}  H ${e.h}  L ${e.l}  C ${e.c}` : ''}\n${e.green ? 'Green' : 'Red'} candle, closed ${e.above ? 'above' : 'under'} the put wall${e.kcHalf ? `\nKeltner: ${e.kcHalf} half (${e.kcPos}% of the channel · lower ${e.kcL} · mid ${e.kcM} · upper ${e.kcU})` : ''}`);
      g += `<g style="cursor:help"><title>${tip}</title><rect x="${x - cw / 2}" y="${T}" width="${cw}" height="${B - T}" fill="${c}" fill-opacity=".13"/>`;
      if (lv >= lo && lv <= hi) g += `<line x1="${x - cw}" x2="${x + cw}" y1="${yp}" y2="${yp}" stroke="${c}" stroke-width="2.5"/>`;
      g += `<path d="M${x},${yl - 8} L${x + 7},${yl} L${x},${yl + 8} L${x - 7},${yl} Z" fill="${c}" stroke="var(--surface)" stroke-width="1.5"/>`;
      g += `<text x="${x}" y="${sh ? Math.max(T + 2, yl - 12) : Math.min(B + 12, yl + 20)}" text-anchor="middle" font-size="${fs - 1}" font-weight="700" fill="${c}" paint-order="stroke" stroke="var(--surface)" stroke-width="3">${sh ? 'SHORT' : 'LONG'}</text></g>`; });
  }
  g += `<line class="fc-xh" x1="0" x2="0" y1="${T}" y2="${XB}" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" style="display:none;pointer-events:none"/>
    <text class="fc-ohlc" x="${L + 4}" y="${T + 13}" font-size="${fs}" fill="var(--ink)" paint-order="stroke" stroke="var(--surface)" stroke-width="4" style="pointer-events:none"></text>`;
  g += `<rect class="fcz-sel" x="0" y="${T}" width="0" height="${AB - T}" fill="var(--coach,#5b5bd6)" fill-opacity=".12" stroke="var(--coach,#5b5bd6)" stroke-width="1" style="display:none;pointer-events:none"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" data-fcz="${esc(id)}" data-l="${L}" data-pw="${W - L - Rr}" data-a="${za}" data-n="${n}" data-slots="${nS}" data-all="${all.length}" data-vw="${W}" style="${full ? `width:${W}px;height:${H}px` : 'width:100%;height:auto'};display:block;touch-action:pan-y;cursor:crosshair;user-select:none" role="img" aria-label="${esc(d.symbol)} price with options alerts, gamma walls and premium accumulation">${g}</svg>`;
}
/* ---------- full-screen chart: one ticker at a time, every evaluated ticker on the right ---------- */
async function loadTickers() {
  S.tkBusy = true;
  try { S.tickers = await api('/bot/tickers'); } catch (e) { S.tickers = { error: e.message }; }
  S.tkBusy = false;
  if (route() === 'botchart') render();
}
try { const m0 = localStorage.getItem('fcfMode'); S.fcfMode = ['dt', 'sd'].includes(m0) ? 'dt' : 'ww'; } catch (e) { S.fcfMode = 'ww'; }   // Wall watch, or Day trading
function fcfSetMode(m) { S.fcfMode = m; try { localStorage.setItem('fcfMode', m); } catch (e) {} }
function wwReviewBox() {
  const r = S.wwRev;
  if (!r) return '';
  if (r.error) return `<div class="loss" style="font-size:.75rem;padding:2px 6px">${esc(r.error)}</div>`;
  const bad = r.rows.filter(x => x.verdict === 'exit' || x.verdict === 'no entry');
  const col = { exit: 'var(--loss)', 'no entry': 'var(--warn,#d97706)', keep: 'var(--gain)', unknown: 'var(--muted)' };
  const lbl = { exit: 'current exits: out', 'no entry': 'would not enter now', keep: 'fits', unknown: '?' };
  return `<div style="font-size:.74rem;padding:2px 6px 6px;border-bottom:1px solid var(--line)">
    <div class="muted" style="margin-bottom:3px">Checked ${esc((r.at || '').slice(11, 16))} · ${r.rows.length} open · ${bad.length} don't fit${bad.filter(x => x.closed).length ? ` · ${bad.filter(x => x.closed).length} closed` : ''} <button class="btn" data-wwrevhide style="font-size:.7rem;padding:0 5px">✕</button></div>
    ${r.rows.map(x => `<div style="display:flex;gap:4px;align-items:flex-start;padding:2px 0;border-top:1px dashed var(--line)"><div style="flex:1"><b>${esc(x.symbol)}</b> <b style="color:${col[x.verdict]}">${lbl[x.verdict]}</b>
      ${x.plPct != null ? ` <span class="${x.plPct >= 0 ? 'gain' : 'loss'}">${x.plPct >= 0 ? '+' : ''}${x.plPct}%</span>` : ''}<br><span class="muted">${x.exitNow ? `${esc(x.exitNow.status)} ${esc((x.exitNow.at || '').replace('T', ' ').slice(5))} at ${x.exitNow.px}` : ''}${x.earnings ? esc(x.earnings) : ''}${(x.why || []).length ? esc(x.why.join(' · ')) : ''}${x.verdict === 'keep' && x.stopNow ? `stop now ${x.stopNow}${x.targetNow ? ` · target ${x.targetNow}` : ''}` : ''}</span></div>
      ${x.closed ? '<b class="loss" style="font-size:.7rem">closed</b>' : x.closeError ? `<span class="loss" title="${esc(x.closeError)}">close failed</span> <button class="btn" data-botclose="${esc(x.id)}" style="font-size:.7rem;padding:0 6px">Retry</button>` : ''}</div>`).join('')}</div>`;
}
async function wwReview() {
  S.wwRevBusy = true; render();
  try { S.wwRev = await api('/bot/wallwatch/review', { method: 'POST', body: { close: true } }); } catch (e) { S.wwRev = { error: e.message }; }
  S.wwRevBusy = false;
  const n = ((S.wwRev || {}).rows || []).filter(x => x.closed).length;
  if (n) { toast(`${n} position${n > 1 ? 's' : ''} closed: not fitting the current settings`); try { await loadWatch(); } catch (e) {} }
  render();
}
async function loadQuotes() {                    // watchlist Mark / change / % change, every 30 s while the chart page is open
  clearTimeout(S.wwQT);
  if (route() !== 'botchart') { S.wwQOn = false; return; }
  S.wwQOn = true;
  try { const q = await api('/bot/wallwatch/quotes'); if (q && typeof q === 'object') { S.wwQ = q; if (!S.fczDrag) wlQuotePaint(); } } catch (e) {}
  S.wwQT = setTimeout(loadQuotes, 30000);
}
function qHtml(x, setup) {
  const qx = (S.wwQ || {})[x], sc = setup ? '<span class="verdict ok" style="font-size:.66rem;padding:0 5px">setup</span>' : '';
  if (!qx) return sc;
  const cls = qx.ch > 0 ? 'gain' : qx.ch < 0 ? 'loss' : 'muted';
  return `<b style="font-size:.85rem" title="Mark">${qx.m < 10 ? qx.m.toFixed(3) : qx.m.toFixed(2)}</b><br><span class="${cls}" style="font-size:.74rem" title="Mark change · % mark change vs the previous close">${qx.ch != null ? (qx.ch > 0 ? '+' : '') + qx.ch.toFixed(2) : '—'} · ${qx.pct != null ? (qx.pct > 0 ? '+' : '') + qx.pct.toFixed(2) + '%' : '—'}</span>${sc ? '<br>' + sc : ''}`;
}
function wlQuotePaint() { document.querySelectorAll('[data-q]').forEach(el => { el.innerHTML = qHtml(el.dataset.q, !!el.dataset.setup); }); }
try { const v = localStorage.getItem('dpN'); if (v !== null) S.dpN = Math.max(0, Math.min(10, +v)); } catch (e) {}
function wlPut(body, msg) {
  api('/bot/wallwatch', { method: 'PUT', body }).then(r => { S.ww = { ...(S.ww || {}), ...r }; S.wwSw = null; toast(msg); render(); }).catch(err => toast(err.message));
}
try { S.wlClosed = JSON.parse(localStorage.getItem('wlClosed') || '{}') || {}; S.wlH = JSON.parse(localStorage.getItem('wlH') || '{}') || {}; } catch (e) { S.wlClosed = {}; S.wlH = {}; }
document.addEventListener('mouseup', () => {          // a watchlist box resized by its corner: keep its height
  let ch = false;
  document.querySelectorAll('[data-wlbox]').forEach(b => { const h = Math.round(b.getBoundingClientRect().height); if (h && Math.abs(h - ((S.wlH || {})[b.dataset.wlbox] || 0)) > 4) { S.wlH = { ...(S.wlH || {}), [b.dataset.wlbox]: h }; ch = true; } });
  if (ch) try { localStorage.setItem('wlH', JSON.stringify(S.wlH)); } catch (e) {}
});
function wwTickers() { return ((S.ww && S.ww.config && S.ww.config.tickers) || []); }
async function loadWwChart(sym) {
  const k = fcKey('ww-' + sym); S.fcBusy[k] = true;
  try { S.fc[k] = await api(`/bot/wallwatch/chart?t=${encodeURIComponent(sym)}&days=${S.wwDays || 45}`); } catch (e) { S.fc[k] = { error: e.message }; }
  S.fcBusy[k] = false; if (route() === 'botchart') render();
}
const fcModeSeg = () => `<span class="seg" role="group" aria-label="Tickers">${[['ev', 'Evaluated'], ['ww', 'Wall watch'], ['dt', 'Day trading']].map(([m, l]) => `<button class="btn ${S.fcfMode === m ? 'coachbtn' : ''}" data-fcfmode="${m}" style="padding:3px 10px">${l}</button>`).join('')}</span>`;
function vChartWW() {
  const list = wwTickers(), t0 = (hashParam('t') || '').toUpperCase(), held = ((S.ww && S.ww.positions) || []).map(x => x.symbol);
  const sym = t0 && (list.includes(t0) || held.includes(t0) || S.chartLab) ? t0 : (list[0] || t0), labOn = !!S.chartLab && !!S.ww && !!labCfg(), id = (labOn ? 'ww-lab-' : 'ww-') + sym;
  if (labOn && S.labTest === undefined) { S.labTest = null; setTimeout(loadLabTest, 0); }
  if (labOn && S.optTest === undefined) { S.optTest = null; setTimeout(loadOptTest, 0); }
  if (!S.ww && !S.wwBusy) { S.wwBusy = true; setTimeout(() => { S.wwBusy = false; loadWatch(); }, 0); }
  const narrow = window.innerWidth < 800, sideW = narrow ? 0 : 260, headH = S.fcfHeadH || (narrow ? 104 : 58), infoH = Math.min(110, S.fcfInfoH || (narrow ? 96 : 64));
  const W = Math.max(320, window.innerWidth - sideW - 20), H = Math.max(380, window.innerHeight - headH - infoH - 18);   // the page scrolls for the rest
  const k = fcKey(id), d = sym ? S.fc[k] : null;
  const did = id + ':D', vid = S.chartDaily ? did : id;          // daily view: the same data as daily candles
  if (S.chartDaily && d && !d.error) {
    const dk = fcKey(did);
    if (!S.fc[dk] || S.fc[dk]._src !== d) S.fc[dk] = { ...fcDaily(d), _src: d };
    S.fcDp[dk] = S.fcDp[k];
  }
  if (sym && !d && !S.fcBusy[k]) { S.fcBusy[k] = true; setTimeout(() => labOn ? loadLabChart(sym) : loadWwChart(sym), 0); }
  if (sym) fcNeedDark(id);
  const sigs = (d && d.signals) || [];
  const selK = S.wwSel && S.wwSel.sym === sym ? sigs.findIndex(x => x.at === S.wwSel.at) : -1;
  if (d && !d.error && S.wwSel && S.wwSel.sym === sym && S.wwSel.zoom) {        // zoom on the chosen signal
    S.wwSel.zoom = false;
    const sg = selK >= 0 ? sigs[selK] : null, bs = fcClean(d).bars, ix = t => { let q = -1; bs.forEach((b0, i) => { if (b0.t <= t) q = i; }); return q; };
    if (sg) { const a0 = ix(sg.at), b0 = sg.exitAt ? ix(sg.exitAt) : bs.length - 1; if (a0 >= 0) { const pad = Math.max(12, Math.round((b0 - a0) * 0.4)); S.fcZoom[k] = [Math.max(0, a0 - pad), Math.min(bs.length - 1, Math.max(b0, a0 + 6) + pad)]; } }
  }
  const allSig = (S.ww && S.ww.signals) || [], bySym = {};
  allSig.forEach(x => { const b = bySym[x.symbol] = bySym[x.symbol] || { n: 0, t: 0, open: 0, tot: 0 }; b.n++; if (x.status === 'target') b.t++; if (x.status === 'open') b.open++; else b.tot += x.pct; });
  const stMap = Object.fromEntries(((S.ww && S.ww.status) || []).map(x => [x.symbol, x]));
  const zb_ = (q, l, t) => sym ? `<button class="btn" data-fczoom="${vid}|${q}" title="${t}" style="padding:3px 10px">${l}</button>` : '';
  const sel = narrow ? `<select id="fcf-sel" aria-label="Ticker">${list.map(x => `<option value="${esc(x)}" ${x === sym ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select>` : '';
  const done = sigs.filter(x => x.status !== 'open'), hit = done.filter(x => x.status === 'target').length;
  const headBar = `<div class="fcf-head"><b style="font-size:1.25rem">${esc(sym || 'Chart')}</b>
      ${d && !d.error ? `<span class="verdict ${d.setupNow ? 'ok' : 'mid'}">${d.setupNow === 'flip' ? 'CW > PW > flip' : d.setupNow === 'merged' ? 'PW = CW < flip' : d.setupNow ? 'CW > flip > PW' : 'no setup'}</span>
      <span class="muted" style="font-size:.85rem">${sigs.length} signal${sigs.length === 1 ? '' : 's'} · ${hit}/${done.length} reached the call wall</span>${d && d.staleSignals ? ` <span class="muted" style="font-size:.8rem">· ${d.staleSignals} from older settings hidden (recomputing)</span>` : ''}${(stMap[sym] || {}).watch ? ` <b style="color:var(--coach);font-size:.85rem">⚑ ${stMap[sym].watch.level ? `waiting for a retest of the ${esc(stMap[sym].watch.levelName)} ${stMap[sym].watch.level}` : 'waiting for a call alert to arm a retest'}</b>` : ''}` : ''}
      ${sel}${zb_('in', '＋', 'Zoom in')}${zb_('out', '−', 'Zoom out')}${zb_('left', '◀', 'Earlier')}${zb_('right', '▶', 'Later')}${zb_('reset', 'Reset', 'Show everything')}
      <span class="seg" role="group" aria-label="Candles"><button class="btn ${!S.chartDaily ? 'coachbtn' : ''}" data-chartdaily="0" style="padding:3px 10px" title="Hourly candles">1h</button><button class="btn ${S.chartDaily ? 'coachbtn' : ''}" data-chartdaily="1" style="padding:3px 10px" title="Daily candles (same signals and levels)">1D</button></span>
      <span class="seg" role="group" aria-label="Days on the chart">${[45, 90, 180].map(n => `<button class="btn ${(S.wwDays || 45) === n ? 'coachbtn' : ''}" data-wwdays="${n}" title="${n} trading days${n > 45 ? ' (signals older than 45 days are replayed with your current settings)' : ''}" style="padding:3px 9px">${n}d</button>`).join('')}</span>
      ${sym ? fcLayerBar(id) : ''}${sym ? wwTkBtn(sym, d && !d.error ? d : null) : ''}<button class="btn ${S.hideSettings ? 'coachbtn' : ''}" data-hidesettings title="Hide / show the indicators and the entry / exit rules" style="padding:3px 10px">${S.hideSettings ? 'Show settings' : 'Hide settings'}</button><button class="btn ${S.hideStats ? 'coachbtn' : ''}" data-hidestats title="Hide / show the stats under the chart" style="padding:3px 10px">${S.hideStats ? 'Show stats' : 'Hide stats'}</button><button class="btn ${labOn ? 'coachbtn' : ''}" data-chartlab title="${labOn ? 'Back to the live strategy' : 'Try other settings on this chart without touching the live bot'}" style="padding:3px 10px">${labOn ? '✓ Strategy lab' : 'Strategy lab'}</button><button class="btn" data-fcfmode="dt" title="1-minute day trading on QQQ / SPY (second Alpaca paper account)">Day trading</button>
      ${S.ww && !S.hideSettings ? (labOn ? labPanel() : wwParams(true)) : ''}<span style="flex:1"></span><a class="btn" href="#botwatch" aria-label="Back to Wall watch" title="Back to Wall watch">✕</a></div>`;
  let main;
  if (!S.ww) main = '<p class="loading" style="padding:16px">Loading the watchlist…</p>';
  else if (!sym) main = '<p class="muted" style="padding:16px">The watchlist is empty.</p>';
  else if (!d) main = '<p class="loading" style="padding:16px">Loading the chart and the walls…</p>';
  else if (d.error) main = `<div class="errbox" style="margin:16px">${esc(d.error)}</div>`;
  else {
    const w = d.wallsNow, pc = v => `<b class="${v >= 0 ? 'gain' : 'loss'}">${v >= 0 ? '+' : ''}${v}%</b>`;
    main = fcSvg(S.chartDaily ? S.fc[fcKey(did)] : d, vid, { W, H, cvdN: +((labCfg() || {}).cvdN) || 20 }) + `<div class="fcf-info" ${S.hideStats ? 'hidden style="display:none"' : ''}>
      ${w ? `<span>Walls: <b class="gain">call ${w.callWall}</b> · <b style="color:var(--coach)">flip ${w.flip ?? '—'}</b> · <b class="loss">put ${w.putWall}</b></span>` : ''}
      ${(d.trades || []).length ? `<span><b style="color:var(--coach)">Option trades</b> ${d.trades.map(tr => `${esc((tr.at || '').slice(5, 16).replace('T', ' '))} ${esc(tr.contract || '')} ×${tr.qty ?? ''} @${tr.fill ?? tr.limit ?? '—'} · ${esc(tr.status || '')}${tr.plPct != null ? ` <b class="${tr.plPct >= 0 ? 'gain' : 'loss'}">${tr.plPct >= 0 ? '+' : ''}${tr.plPct}%</b>` : ''}`).join(' | ')}</span>` : ''}
      ${(() => { const m = (d.maxPain || []).slice(-1)[0], nx = (d.expirations || []).find(e => e >= new Date().toISOString().slice(0, 10)); return m || nx ? `<span style="color:var(--warn,#d97706)"><b>${nx ? `Next monthly exp ${esc(nx)}` : ''}${m ? ` · max pain ${m.maxPain}${m.exp !== nx ? ` (${esc(m.exp)})` : ''}` : ''}</b></span>` : ''; })()}
      ${(() => { const dpd = S.fcDp[k]; if (!S.fcLayers.dark || !dpd) return S.fcLayers.dark && S.fcDpBusy[k] ? '<span class="muted">loading dark pool…</span>' : '';
        if (dpd.error && !(dpd.prints || []).length) return `<span class="muted">Dark pool: ${esc(dpd.error)}</span>`;
        const ds = S.fcSelD && S.fcSelD.id === id ? (dpd.prints || [])[S.fcSelD.k] : null;
        const vol = dpd.adv ? ` (${dpd.minPct}% of ${(dpd.adv / 1e6).toFixed(1)}M sh/day = ${Math.round(dpd.minShares).toLocaleString('en-US')} sh)` : '';
        const bs = '';
        return `<span>Dark pool ${(dpd.prints || []).length} prints ≥ ${money(dpd.minPremium, false)}${vol}${bs}${(dpd.levels || []).length ? ` · levels ${dpd.levels.slice(0, 3).map(l => `<b style="color:var(--warn)">${l.price}</b> ${money(l.premium, false)}`).join(' · ')}` : ''}</span>`
          + (ds ? `<span><b style="color:var(--warn)">DARK POOL</b> ${esc(ds.t.replace('T', ' ').slice(5))} ET · ${ds.size.toLocaleString('en-US')} sh at ${ds.price} · <b>${money(ds.premium, false)}</b></span>` : ''); })()}
      ${(() => { const al = d.alerts || [], sm = xs => xs.reduce((a, x) => a + x.premium, 0), c = al.filter(a => a.type === 'call'), p_ = al.filter(a => a.type === 'put');
        const sa = S.fcSel && S.fcSel.id === id ? al[S.fcSel.k] : null;
        return `<span><b class="gain">Calls</b> ${c.length} · ${money(sm(c), false)} &nbsp; <b class="loss">Puts</b> ${p_.length} · ${money(sm(p_), false)}</span>`
          + (sa ? `<span><b>${esc(sa.t.replace('T', ' ').slice(5))} ET · ${sa.type === 'call' ? '<span class="gain">CALL</span>' : '<span class="loss">PUT</span>'} ${money(sa.premium, false)}</b> · ${esc(sa.contract || '')}${sa.sweep ? ' · sweep' : ''}</span>` : '')
          + (d.note ? `<span class="muted">${esc(d.note)}</span>` : ''); })()}
      <span>${sigs.length ? sigs.map((x, q) => `<button class="btn ${q === selK ? 'coachbtn' : ''}" data-wwsel="${esc(sym)}|${esc(x.at)}" style="font-size:.75rem;padding:1px 7px">${esc(x.at.slice(5, 16).replace('T', ' '))} ${x.status === 'open' ? 'open' : q === selK ? `${x.pct >= 0 ? '+' : ''}${x.pct}%` : pc(x.pct)}${labOn ? optTag(sym, x) : ''}</button>`).join(' ') : '<span class="muted">No signal in this period</span>'}</span>
      ${labOn ? rsTabs('swing') + (S.rsTab === 'ai' ? researchBox('swing') : labStats(sigs) + optSymBox(sym, sigs, selK >= 0 ? sigs[selK] : null) + labTestBox() + optTestBox()) : ''}
      <span class="muted">IN = PW setup, FLIP = flip setup, MRG = merged walls, RT = flow retest · green line = target (call wall) · red line = stop, trailing under the highest high · square = exit · × = reached the support but no entry (hover for the reason; grey = no call alert) · ◆ = VWAP reset candle touching the put wall (purple: closed above it, pink: under it) · ↑ ↓ ticker</span>
      ${d.wallNote ? `<span class="muted">${esc(d.wallNote)}</span>` : ''}</div>`;
  }
  const added = ((S.ww && S.ww.config && S.ww.config.added) || {});
  if (!S.wwQOn) setTimeout(loadQuotes, 0);
  const side = narrow ? '' : `<aside class="fcf-side"><form id="ww-addf" style="display:flex;gap:4px;margin-bottom:4px;flex-wrap:wrap"><input id="ww-add" placeholder="Add ticker" style="flex:1;min-width:0;margin:6px 0 6px 6px" aria-label="Add a ticker">
        <select id="ww-addlist" style="max-width:7.5em;margin:6px 0" aria-label="To list">${(((S.ww && S.ww.config && S.ww.config.lists) || [{ name: 'Main' }])).map(L => `<option ${L.name === (S.wlAddTo || 'Main') ? 'selected' : ''}>${esc(L.name)}</option>`).join('')}</select>
        <button class="btn" type="submit" style="padding:2px 8px;margin:6px 0">Add</button><button class="btn" type="button" data-wlnew style="padding:2px 8px;margin:6px 6px 6px 0" title="Create a new watchlist">+ List</button></form>
      ${(() => { const ps = (S.ww && S.ww.positions) || []; if (!ps.length) return '';
        return `<div style="display:flex;align-items:center;gap:6px;margin:6px 2px 4px"><span class="muted" style="font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;flex:1">Current positions (${ps.length})</span>
          <button class="btn coachbtn" data-wwreview ${S.wwRevBusy ? 'disabled' : ''} style="font-size:.7rem;padding:1px 7px" title="Replay every open wall watch trade with the CURRENT settings (entry rules + exits) and sell at market the ones that don't fit">${S.wwRevBusy ? 'Checking…' : 'Check & close misfits'}</button></div>
        ${wwReviewBox()}
        <ul class="fcf-list" style="margin-bottom:8px;border-bottom:2px solid var(--line)">${ps.map(p0 => `<li data-sym="${esc(p0.symbol)}"><a href="#botchart?t=${encodeURIComponent(p0.symbol)}" ${p0.symbol === sym ? 'aria-current="true"' : ''}>
          <span><b>${esc(p0.symbol)}</b> <span class="muted" style="font-size:.75rem">${p0.strike ? esc(String(p0.strike)) + 'C' : ''} ${esc((p0.exp || '').slice(5))}</span><br><span class="muted" style="font-size:.75rem">${p0.qty ?? ''}× @${p0.fill ?? p0.limit ?? '—'}${p0.status !== 'open' ? ' · ' + esc(p0.status) : ''}</span></span>
          <span style="text-align:right"><b class="${(p0.plPct || 0) >= 0 ? 'gain' : 'loss'}" style="font-size:.85rem">${p0.plPct != null ? (p0.plPct >= 0 ? '+' : '') + p0.plPct + '%' : '—'}</b><br><span class="muted" style="font-size:.72rem">${p0.pl != null ? money(p0.pl) : ''}</span></span></a></li>`).join('')}</ul>
        <div class="muted" style="font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin:2px 2px 4px">Watchlist (${list.length})</div>`; })()}
      <input id="fcf-q" type="search" placeholder="Filter tickers" value="${esc(S.fcfQ || '')}" aria-label="Filter tickers">
      ${(((S.ww && S.ww.config && S.ww.config.lists) || [{ name: 'Main', tickers: list }])).map(L => { const open_ = !(S.wlClosed || {})[L.name], h = (S.wlH || {})[L.name];
        return `<div class="wl-sec" style="border-bottom:2px solid var(--line)"><div style="display:flex;align-items:center;gap:4px;padding:4px 6px;background:var(--sunk)">
          <button class="btn" data-wltoggle="${esc(L.name)}" style="font-size:.7rem;padding:0 6px">${open_ ? '▾' : '▸'}</button><b style="font-size:.78rem;flex:1;text-transform:uppercase;letter-spacing:.04em">${esc(L.name)} <span class="muted">(${L.tickers.length})</span></b>
          ${!['Main', 'Unusual flow'].includes(L.name) ? `<button class="btn" data-wlren="${esc(L.name)}" title="Rename" style="font-size:.7rem;padding:0 6px">✎</button><button class="btn" data-wldel="${esc(L.name)}" title="Delete this list" style="font-size:.7rem;padding:0 6px">✕</button>` : ''}</div>
          ${open_ ? `<div class="wl-box" data-wlbox="${esc(L.name)}" style="height:${h || (L.name === 'Main' ? 420 : 200)}px;resize:vertical;overflow:auto;min-height:60px"><ul class="fcf-list" style="overflow:visible">${L.tickers.map(x => { const b = bySym[x] || { n: 0, t: 0, open: 0, tot: 0 }, st = stMap[x] || {}, ad = added[x];
        return `<li data-sym="${esc(x)}" ${S.fcfQ && !x.includes(S.fcfQ.toUpperCase()) ? 'hidden' : ''}><a href="#botchart?t=${encodeURIComponent(x)}" ${x === sym ? 'aria-current="true"' : ''}>
        <span><b>${esc(x)}</b>${b.open ? ' <span class="exchip" style="font-size:.7rem">open</span>' : ''}${ad && ad.by === 'flow' ? ` <span class="exchip" style="font-size:.7rem" title="Added ${esc((ad.at || '').replace('T', ' ').slice(5, 16))} from unusual call flow ${money(ad.premium || 0, false)}">flow ${esc((ad.at || '').slice(5, 10))}</span>` : ''}<br><span class="muted" style="font-size:.75rem">${b.n} signal${b.n === 1 ? '' : 's'}${b.n ? ` · ${b.t} target` : ''}</span></span>
        <span data-q="${esc(x)}" data-setup="${st.setup ? 1 : ''}" style="text-align:right;font-variant-numeric:tabular-nums;line-height:1.25">${qHtml(x, !!st.setup)}</span></a><span class="wl-act"><button class="btn" data-wlmv="${esc(L.name)}|${esc(x)}" title="Move to another list" style="font-size:.68rem;padding:0 5px">⇄</button><button class="btn" data-wlrm="${esc(L.name)}|${esc(x)}" title="Remove from ${esc(L.name)}" style="font-size:.68rem;padding:0 5px">✕</button></span></li>`; }).join('')}</ul></div>` : ''}</div>`; }).join('')}</aside>`;
  return `<div class="fcf">${headBar}<div class="fcf-body"><div class="fcf-main">${main}</div>${side}</div></div>`;
}
function fcfSymbol() {
  if (S.fcfMode === 'ww') { const l = wwTickers(), t = (hashParam('t') || '').toUpperCase(), hd = ((S.ww && S.ww.positions) || []).map(x => x.symbol); return l.includes(t) || hd.includes(t) ? t : (l[0] || t); }
  const items = (S.tickers && S.tickers.items) || [];
  const t = (hashParam('t') || '').toUpperCase();
  return items.find(x => x.symbol === t) ? t : (items[0] && items[0].symbol) || t;
}
/* ---------- day trading: 1-minute strategy on a second Alpaca paper account ---------- */
const DT_IND = [
  ['vwap', 'Session VWAP', 'vwap', [], 'Volume-weighted average price from 09:30', ['aboveVwap', 'vwapCross', 'vwapExit']],
  ['ema', 'EMA fast / slow', 'ema', [['emaLen', 'fast', 1, 3.2, '', 9], ['ema2Len', 'slow', 1, 3.2, '', 21]], 'Exponential averages of the 1-minute closes', ['aboveEma', 'emaStack', 'emaExit']],
  ['kc', 'Keltner', 'kch', [['kcLen', 'EMA', 1, 3.2, '', 20], ['kcMult', '±', 0.1, 3, '× ATR', 2]], 'Keltner channel on 1-minute candles', ['kcNotExt', 'kcLower']],
  ['sq', 'Squeeze (TTM)', 'sqz', [['sqLen', 'length', 1, 3.2, '', 20], ['sqBB', 'BB', 0.1, 3, '× SD', 2], ['sqKC', 'KC', 0.1, 3, '× ATR', 1.5], ['sqFireN', 'fired within', 1, 3, 'candles', 3]], 'Momentum pane under the chart', ['sqOn', 'sqFire', 'sqMom', 'sqExit']],
  ['in', 'Inertia', 'inertia', [['inN1', 'lengths', 1, 3.2, '', 70], ['inN2', '', 1, 3.2, '', 40], ['inN3', '', 1, 3.2, 'candles', 20]], 'Linear regression of the closes, 3 lengths (strip under the candles)', ['inGreen', 'inNotRed', 'inExit']],
  ['fvg', 'Fair value gaps', null, [['fvgMin', 'min size', 0.05, 3.2, '× ATR', 0.3], ['fvgNear', 'margin', 0.05, 3.2, '× ATR', 0.25], ['fvgDays', 'max age', 1, 3, 'days', 1]], '3-candle gaps on 1-minute candles', ['fvgTouch', 'fvgExit']],
  ['orb', 'Opening range', null, [['orbMin', 'first', 1, 3, 'minutes', 15]], 'High / low of the first minutes (values under the chart)', ['orbBreak']],
  ['pm', 'Pre-market high / low', null, [], '04:00-09:30 (values under the chart)', ['pmBreak']],
  ['walls', 'Gamma walls', 'walls', [['zoneAtr', 'test zone', 0.1, 3.2, '× ATR', 1]], 'Put wall, flip, call wall of the day (or of the previous day: switch “use the previous day’s walls”)', ['pwTouch', 'aboveFlip', 'flipOpen', 'flipFade', 'pwSide', 'lvlTarget']],
  ['bands', 'EMA bands 9 / 22 / 44', 'bands', [['formWin', 'formation signal lasts', 1, 3, 'min', 5]], 'EMA 9 / 22 / 44 with the 22-44 cloud; arrows = candle formations when 9 > 13 > 22 (or the reverse). The window applies to the band formation, LIS formation and squeeze turn rules', ['bandForm']],
  ['lis', 'LIS (line in the sand)', 'lis', [], 'Previous regular-session close; arrows = 5-minute formations off it (colored when the EMAs agree)', ['lisForm', 'lisAbove']],
  ['pmz', 'Pre-market zone', 'pmz', [], '07:25-09:30 range: 20-40% under the high on a gap up, above the low on a gap down', ['pmzTouch']],
  ['fibs', 'Auto fibs', 'fibs', [], 'The PREVIOUS day high / low, known at the open: inside 38.2% / 50% / 61.8%, above 127.2% / 161.8% / 200%, below -27.2% / -61.8% / -100%', ['fibTouch', 'lvlTarget']],
  ['turn', 'Squeeze turns', 'turn', [['sqTurnTf', 'turns of the', 1, 3, '-minute squeeze (1 = engine default, 5 = the turns of the 5m chart)', 1]], 'Momentum peaks above 0 (turn down) / bottoms below 0 (turn up). Settings on the Squeeze row; the rule can use 1-minute or 5-minute turns', ['sqTurn']],
  ['dark', 'Dark pool', 'dark', [], 'Top dark pool price levels of the last 180 days', ['dpTouch', 'lvlTarget']],
];
const DT_RULES = [
  ['aboveVwap', 'Entry', 'Above VWAP', 'Close above the session VWAP (short: below)'],
  ['vwapCross', 'Entry', 'VWAP cross', 'This candle crosses the VWAP (up for a long, down for a short)'],
  ['aboveEma', 'Entry', 'Above fast EMA', 'Close above the fast EMA (short: below)'],
  ['emaStack', 'Entry', 'Fast EMA > slow', 'Fast EMA above the slow EMA (short: under)'],
  ['greenBar', 'Entry', 'Green candle', 'The entry candle closes green (short: red)'],
  ['kcNotExt', 'Entry', 'Inside the KC', 'Not past the outer Keltner band in the trade direction'],
  ['kcLower', 'Entry', 'KC pullback half', 'Close at or under the middle of the Keltner (short: at or above)'],
  ['sqOn', 'Entry', 'Squeeze on', 'Bollinger bands inside the Keltner channel'],
  ['sqFire', 'Entry', 'Squeeze fired', 'The squeeze turned off within the last N candles, momentum in the trade direction'],
  ['sqMom', 'Entry', 'Squeeze momentum', 'Momentum above 0 and rising (short: below 0 and falling)'],
  ['inGreen', 'Entry', 'Inertia with the trade', 'All 3 inertias rising (short: all falling)'],
  ['inNotRed', 'Entry', 'Inertia not against', 'Not all 3 inertias against the trade'],
  ['fvgTouch', 'Entry', 'In an FVG', 'Trades into an open bullish gap and closes above its bottom (short: bearish gap)'],
  ['orbBreak', 'Entry', 'Opening range break', 'Crosses the opening range high (short: low)'],
  ['pmBreak', 'Entry', 'Pre-market break', 'Crosses the pre-market high (short: low)'],
  ['pwTouch', 'Entry', 'Wall test', 'Low tests the put wall and closes above (short: high tests the call wall, closes under)'],
  ['aboveFlip', 'Entry', 'Above the flip', 'Close above the gamma flip (short: below)'],
  ['stopOn', 'Exit', 'Stop', 'Entry − this × the unit (short: +). Units: 1-minute ATR (tight: one minute of noise), the average move (true range) of the last N 1-minute candles (the current volatility; N = 200-500), 5-minute ATR, daily ATR (the average day range), or % of the price. Swing low / high: the stop goes under the last swing low already formed (a low under the N candles before and after it; short: above the swing high) minus the swing %, the number is not used and the trailing stop is × that risk', [['stopAtr', 0.05, 3.4, '', 1.5], ['stopBase', 0, 0, '', 'atr1', 'sel', '×', [['atr1', '1-min ATR'], ['avgN', 'avg move of the last N candles'], ['atr5', '5-min ATR'], ['atrD', 'daily ATR'], ['pct', '% of price'], ['swing', 'swing low / high']]], ['volN', 1, 3.8, 'candles', 300, '', 'N ='], ['swingBuf', 0.01, 3.6, '%', 0.05, '', 'swing −'], ['swingN', 1, 3, 'candles each side', 5, '', 'pivot']]],
  ['targetOn', 'Exit', 'Target', 'Entry + this × the risk', ['targetR', 0.25, 3.4, 'R', 2]],
  ['trailOn', 'Exit', 'Trailing stop', 'Best price − this × the stop unit, never back', ['trailAtr', 0.05, 3.4, '× stop unit', 2]],
  ['vwapExit', 'Exit', 'Back through VWAP', 'A close back under the VWAP (short: above)'],
  ['emaExit', 'Exit', 'Back through fast EMA', 'A close back under the fast EMA (short: above)'],
  ['sqExit', 'Exit', 'Squeeze momentum turns', 'Momentum stops rising (short: stops falling)'],
  ['inExit', 'Exit', 'Inertia turns', 'Inertia no longer all with the trade'],
  ['fvgExit', 'Exit', 'FVG against', 'A new gap against the trade'],
  ['beOn', 'Exit', 'Breakeven stop', 'Once the trade is this many R in profit, the stop moves to the entry', ['beR', 0.25, 3.2, 'R', 1]],
  ['scaleOn', 'Exit', 'Scale out half', 'Half the position off at this many R; the rest runs to the other exits (R counts both halves)', ['scaleR', 0.25, 3.2, 'R', 1]],
  ['timeOn', 'Exit', 'Time stop', 'Out after this many minutes in the trade', ['timeMin', 1, 3.4, 'min', 30]],
  ['lvlTarget', 'Exit', 'Target = next level', 'Target at the next level in the trade direction (at least 0.25 ATR away): previous-day fibs, gamma walls, previous high / low / close, dark pool, pre-market and opening range; falls back to the R target'],
  ['dayStop', 'Exit', 'Daily loss limit', 'No new entry once the day\'s closed trades reach −this R', ['dayStopR', 0.5, 3.2, 'R', 2]],
  ['bandForm', 'Entry', 'EMA band formation', 'A Tr3ndy EMA-band formation in the trade direction within the last N minutes (window under Indicators → EMA bands)'],
  ['lisForm', 'Entry', 'LIS formation', 'A 5-minute LIS formation in the trade direction, confirmed, within the last N minutes'],
  ['lisAbove', 'Entry', 'LIS side: long above / short below', 'Close above the LIS (line in the sand = previous close) → longs only; close below the LIS → shorts only'],
  ['pmzTouch', 'Entry', 'Pre-market zone reclaim', 'Low into the pre-market zone, close above its top (short: high into it, close under its bottom)'],
  ['fibTouch', 'Entry', 'Fib level test', 'Low tests a previous-day fib level (within the wall test zone) and closes above it (short: the mirror)'],
  ['dpTouch', 'Entry', 'Dark pool level test', 'Low tests a dark pool level (top levels of the ~2 months before) and closes above it (short: the mirror)'],
  ['sqTurn', 'Entry', 'Squeeze turn', 'A squeeze-turn arrow in the trade direction within the last N minutes (window under Indicators → EMA bands): long = momentum below 0 bottoms (turn up), short = momentum above 0 peaks (turn down)'],
  ['prevTouch', 'Entry', 'Previous-day level test', 'Low tests the previous day high / low / close and closes above it (short: the mirror)'],
  ['gapOn', 'Filter', 'Gap size', 'Only days whose open is this far from the previous close (absolute %)', [['gapMin', 0.05, 4, '%', 0.25, '', 'from'], ['gapMax', 0.25, 4, '%', 99, '', 'to']]],
  ['gapWith', 'Filter', 'With the gap', 'Longs only on a gap up, shorts only on a gap down'],
  ['gapFade', 'Filter', 'Fade the gap', 'Longs only on a gap down, shorts only on a gap up'],
  ['flipOpen', 'Filter', 'Open vs flip', 'Longs only on days that opened above the gamma flip, shorts only below'],
  ['pwSide', 'Filter', 'Put wall side', 'Close under the put wall (put wall > close) → shorts only; close above the put wall → longs only. Days without a put wall: no trade'],
  ['cvdSide', 'Filter', 'CVD side', 'Longs only while the cumulative volume delta from the open is above 0 (buyers ahead), shorts only below. Estimated from the candles unless “CVD from real trades” is on'],
  ['cvdReal', 'Filter', 'CVD from real trades', 'For the CVD side: the real order flow (every trade) on the days already loaded with the Order flow layer; the other days use the estimate'],
  ['flipPrev', 'Filter', 'Use the previous day’s walls', 'The flip and walls of the PREVIOUS trading day (surely known before the open) instead of the day itself: checks that a result does not come from a flip computed with that day\'s prices'],
  ['flipFade', 'Filter', 'Open vs flip (reverse)', 'Longs only on days that opened BELOW the gamma flip, shorts only on days that opened above (the scenario study: below the flip at the open closed green 60-78% of the days)'],
  ['peerAgree', 'Filter', 'Other ticker agrees', 'QQQ for SPY (and the reverse): the other ticker is above its VWAP at that minute for a long (below for a short)'],
  ['rvolOn', 'Filter', 'Relative volume', 'Volume so far today vs the average of the last 10 days at the same minute', ['rvolMin', 0.1, 3, '× or more', 1.5]],
  ['vixOn', 'Filter', 'VIX range', 'The previous VIX close inside this range', [['vixMin', 1, 3, '', 0, '', 'from'], ['vixMax', 1, 3, '', 25, '', 'to']]],
  ['noLunch', 'Filter', 'Skip lunch', 'No new entry in this window', [['lunchFrom', 0, 4.2, '', '11:30', 'time', 'from'], ['lunchTo', 0, 4.2, '', '13:30', 'time', 'to']]],
];
try { S.dtTf = +(localStorage.getItem('dtTf') || 1) || 1; S.dtDays = +(localStorage.getItem('dtDays') || 3) || 3; } catch (e) { S.dtTf = 1; S.dtDays = 3; }
async function loadDT() { try { S.dt = await api('/daytrade'); } catch (e) { S.dt = { error: e.message }; } if (['botchart', 'settings'].includes(route())) render(); }
function dtSettingsBox() {                       // Settings → Brokers: the second Alpaca paper account for the 1-minute day trading
  if (S.dt === undefined) { S.dt = null; setTimeout(loadDT, 0); }
  if (!S.dt) return '<p class="loading" style="padding:0">Loading…</p>';
  if (S.dt.error) return `<div class="errbox">${esc(S.dt.error)}</div>`;
  return `<p class="muted" style="margin:0 0 6px;font-size:.85rem">A SECOND Alpaca <b>paper</b> account (its own API key / secret), kept apart from the options bot. The day-trading strategy (Chart → Day trading) trades QQQ / SPY on it every minute.</p>${dtAccountBox(true)}`;
}
function dtUnpack(d) {                           // the server sends long series as columns + rows (candle index for the time)
  if (!d || typeof d !== 'object') return d;
  const un = v => v.rows.map(r => Object.fromEntries(v.cols.map((c, j) => [c, r[j]])));
  if (d.bars && d.bars._p) d.bars = un(d.bars);
  const ts = (d.bars || []).map(b => b.t);
  Object.keys(d).forEach(k => { const v = d[k]; if (v && v._p && v.cols && v.cols[0] === 'i') d[k] = v.rows.map(r => { const o = { t: ts[r[0]] }; v.cols.slice(1).forEach((c, j) => { o[c] = r[j + 1]; }); return o; }); });
  return d;
}
const dtTfEff = () => (S.dtTf === 1 && S.dtDays >= 20 ? 5 : S.dtTf);   // 20-30 days of 1-minute candles: drawn as 5-minute (12,000 candles freeze the page)
function svgCached(slot, ref, key, draw) {        // the chart SVG is rebuilt only when its data / view changes, not on every page refresh
  const c = S._svg && S._svg[slot];
  if (c && c.ref === ref && c.key === key) return c.html;
  const html = draw();
  S._svg = { ...(S._svg || {}), [slot]: { ref, key, html } };
  return html;
}
async function loadDTChart(sym) {
  const k = `dt|${sym}|${dtTfEff()}|${S.dtDays}|${(S.dtView || {}).id || ''}`; S.dtBusy = k; if (route() === 'botchart') render();
  try { S.dtFc = { k, d: dtUnpack(await api('/daytrade/chart', { method: 'POST', body: { t: sym, days: S.dtDays, tf: dtTfEff(), ...(S.dtView ? { cfg: S.dtView.cfg } : {}) } })) }; } catch (e) { S.dtFc = { k, d: { error: e.message } }; }
  S.dtBusy = null; if (route() === 'botchart') render();
}
function dtCfg() { return { ...((S.dt && S.dt.config) || {}), ...(S.dtDraft || {}) }; }
function fxRow(keys, c, attr) {                    // formula rules: any condition the data allows (formula.py)
  const tip = 'Formula rule: an expression over the candles, VWAP, CVD, walls, flip, dark pool… e.g. close > vwap and crossUp(ema(close, 9), ema(close, 21)) and cvd > 0 — entry: an extra condition (AND); exit: sell at the close of the first candle where it is true. Empty = off.';
  return `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center"><b style="font-size:.8rem;min-width:3.6em" title="${esc(tip)}">Formula</b>
    ${keys.map(([k, l]) => `<label style="display:inline-flex;gap:4px;align-items:center;font-size:.78rem;flex:1 1 260px"><span class="muted" style="white-space:nowrap">${l}</span><input data-${attr}="${k}" value="${esc(c[k] || '')}" placeholder="off" spellcheck="false" style="flex:1;min-width:10em;padding:1px 5px;font-family:ui-monospace,monospace;font-size:.78rem"></label>`).join('')}
    <span class="muted" style="font-size:.72rem" title="${esc(tip)}">names: open high low close volume vwap delta cvd tod dow dayOpen dayHigh dayLow prevClose prevHigh prevLow gap pmHigh pmLow putWall callWall flip dpAbove dpBelow flowPrem vix peer · day context: callPrem putPrem netPrem netPremDay sweepCall sweepPut gex breadth sectorBreadth megaBreadth vixy vixyChg vixTerm fomc cpi nfp opex qend eventDay onMove pmRange putWallNow callWallNow flipNow · ema sma highest lowest ref change pct stdev rsi atr crossUp crossDown rising falling abs min max between sqMom sqOn rvol cvdSum orHigh orLow</span></div>`;
}
// ---------------- research: the AI agent (proposes) and the test pipeline (tests + grades) are two separate boxes
try { S.rsTab = localStorage.getItem('rsTab') || 'tools'; S.rsSort = localStorage.getItem('rsSort') || 'best'; } catch (e) { S.rsTab = 'tools'; }
function rsTabs(strategy) {                       // the AI research agent gets its own tab, apart from the other tools
  S.rs = S.rs || {};
  if (S.rs[strategy] === undefined) { S.rs[strategy] = null; setTimeout(() => loadRs(strategy), 0); }
  const t = S.rs[strategy] || {}, run = t.run || {}, au = ((t.auto || {})[strategy]) || {}, ai = S.rsTab === 'ai';
  const ab = (t.ideas || []).filter(x => ['A', 'B'].includes(x.grade)).length;
  const badge = run.running ? '<span style="background:#d97706;color:#fff;border-radius:8px;padding:0 6px;font-size:.7rem;margin-left:4px">running</span>'
    : au.on ? '<span style="background:var(--gain);color:#fff;border-radius:8px;padding:0 6px;font-size:.7rem;margin-left:4px">autopilot</span>' : '';
  const tab = (k, l) => `<button class="btn ${(S.rsTab === 'ai') === (k === 'ai') ? 'coachbtn' : ''}" data-rx="tab|${k}" style="font-size:.85rem;padding:3px 14px">${l}</button>`;
  return `<div class="seg" role="tablist" style="display:flex;gap:0;margin:14px 0 4px">${tab('tools', strategy === 'day' ? 'Backtest & tools' : 'Lab tests')}${tab('ai', `AI research${badge}${(t.ideas || []).length ? ` <span class="muted" style="font-size:.72rem">${(t.ideas || []).length} ideas${ab ? ` · ${ab} A/B` : ''}</span>` : ''}`)}</div>`;
}
function rsCatalog(strategy) {
  const rules = strategy === 'day' ? DT_RULES : WW_RULES, inds = strategy === 'day' ? DT_IND : WW_IND, out = [];
  rules.forEach(r => { out.push({ key: r[0], kind: 'rule', group: r[1], label: r[2], what: r[3] });
    [r[4], r[5]].forEach(f => (!f ? [] : Array.isArray(f[0]) ? f : [f]).forEach(x => { const k = strategy === 'day' ? x[0] : x[1];
      if (typeof k === 'string' && x[5] !== 'time') out.push({ key: k, kind: 'number', of: r[0], unit: strategy === 'day' ? x[3] : x[4] }); })); });
  inds.forEach(x => x[3].forEach(p0 => out.push({ key: p0[0], kind: 'number', indicator: x[1], label: p0[1], unit: p0[4], default: p0[5] })));
  if (strategy === 'day') out.push({ key: 'barMin', kind: 'number', label: 'candle size in minutes: 1 or 5 (the strategy runs on these candles)', default: 5 },
    { key: 'startT', kind: 'time', label: 'entries from (HH:MM)' }, { key: 'endT', kind: 'time', label: 'entries until (HH:MM)' }, { key: 'flatT', kind: 'time', label: 'flat at (HH:MM)' },
    { key: 'maxTrades', kind: 'number', label: 'max trades a day' }, { key: 'longs', kind: 'rule', label: 'take longs' }, { key: 'shorts', kind: 'rule', label: 'take shorts' });
  return out;
}
function rsLabel(strategy) { const m = {}; (strategy === 'day' ? DT_RULES : WW_RULES).forEach(r => { m[r[0]] = r[2]; }); return m; }
async function loadRs(strategy, lite) {
  clearTimeout(S.rsPoll);
  try {
    if (lite && S.rs && S.rs[strategy] && !S.rs[strategy].error) {   // status only; the full list when something changed
      const x = await api(`/research?strategy=${strategy}&lite=1`), cur = S.rs[strategy];
      const changed = x.nIdeas !== cur.nIdeas || x.robAt !== cur.robAt || x.evoAt !== cur.evoAt || !!(x.run || {}).running !== !!(cur.run || {}).running || (x.run || {}).testedNow !== (cur.run || {}).testedNow || (x.run || {}).step !== (cur.run || {}).step;
      S.rs[strategy] = { ...cur, run: { ...(cur.run || {}), ...x.run }, autoToday: x.autoToday, auto: x.auto };
      if (x.nIdeas !== cur.nIdeas || x.robAt !== cur.robAt || x.evoAt !== cur.evoAt || (!x.run.running && (cur.run || {}).running)) { S.rs[strategy] = { ...(await api('/research?strategy=' + strategy)), nIdeas: x.nIdeas, robAt: x.robAt, evoAt: x.evoAt }; }
      if (changed && route() === 'botchart' && S.rsTab === 'ai') render();
    } else {
      const x = await api('/research?strategy=' + strategy);
      S.rs = { ...(S.rs || {}), [strategy]: { ...x, nIdeas: (x.ideas || []).length } };
      if (route() === 'botchart') render();
    }
  } catch (e) { S.rs = { ...(S.rs || {}), [strategy]: { error: e.message } }; if (route() === 'botchart') render(); }
  clearTimeout(S.rsPoll);
  const r = (S.rs || {})[strategy];
  const au = ((r && r.auto) || {})[strategy] || {}, left = au.on && (!au.perDay || (((r.autoToday || {})[strategy]) || 0) < au.perDay);
  const evo = strategy === 'day' && r && ((r.evolve && (r.evolve.on || r.evolve.running)) || (r.sip && r.sip.running));
  if (r && ((r.run && r.run.running) || left || evo) && route() === 'botchart' && S.rsTab === 'ai') S.rsPoll = setTimeout(() => loadRs(strategy, true), (r.run && r.run.running) || (r.evolve || {}).running ? 15000 : 60000);
}
function rsCur(strategy) { return strategy === 'day' ? dtBody() : { ...(labCfg() || {}), ...(S.labDraft || {}), ...(S.labSw || {}) }; }
function researchBox(strategy) {
  S.rs = S.rs || {};
  if (S.rs[strategy] === undefined) { S.rs[strategy] = null; setTimeout(() => loadRs(strategy), 0); }
  const t = S.rs[strategy] || {}, c = t.criteria || {}, run = t.run || {}, busy = !!run.running;
  const lab = rsLabel(strategy), R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const ch = x => Object.entries({ ...(x.changes || {}), ...(x.readable || {}) }).map(([k, v]) => typeof v === 'boolean' ? `${v ? '+' : '−'} ${esc(lab[k] || k)}` : String(k).startsWith('fx') ? `<code style="font-size:.74rem">${esc(k)}: ${esc(v)}</code>` : `${esc(lab[k] || k)} = ${esc(v)}`).join(' · ');
  const GC = { A: '#16a34a', B: '#0891b2', C: '#d97706', F: '#dc2626' };
  const badge = x => x.status === 'tested' ? `<b style="display:inline-block;min-width:1.6em;text-align:center;border-radius:5px;color:#fff;background:${GC[x.grade] || '#888'}">${x.grade}</b>` : `<span class="muted" style="font-size:.75rem">${esc({ 'needs feature': 'waiting: indicator', 'needs data': 'needs data', queued: 'queued' }[x.status] || x.status || '')}</span>`;
  const filt = S.rsFilter || 'all';
  const all = t.ideas || [], xs = all.filter(x => filt === 'all' || (filt === 'graded' ? x.status === 'tested' : filt === 'ab' ? ['A', 'B'].includes(x.grade) : filt === 'needs' ? ['needs feature', 'needs data'].includes(x.status) : ['long', 'short', 'both'].includes(filt) ? ideaSide(x) === filt : true));
  const GR = { A: 0, B: 1, C: 2, F: 3 }, srt = S.rsSort || 'best';
  if (srt === 'best') xs.sort((a, b) => {                // best first: grade, then t, then average R; untested at the bottom
    const ta = a.status === 'tested', tb = b.status === 'tested';
    if (ta !== tb) return ta ? -1 : 1;
    if (!ta) return (b.at || '').localeCompare(a.at || '');
    const ma = a.metrics || {}, mb = b.metrics || {};
    const few = m => (m.n || 0) < (c.minTrades || 100) || Math.min((m.h1 || {}).n || 0, (m.h2 || {}).n || 0) < (c.minHalf || 40) ? 1 : 0;   // too few trades: below the ones that trade enough
    return (GR[a.grade] ?? 9) - (GR[b.grade] ?? 9) || few(ma) - few(mb) || (mb.t ?? -99) - (ma.t ?? -99) || (mb.avgR ?? -99) - (ma.avgR ?? -99); });
  else xs.sort((a, b) => (b.at || '').localeCompare(a.at || ''));
  const nA = all.filter(x => x.grade === 'A').length, nB = all.filter(x => x.grade === 'B').length;
  const num = (k, step, w, l, u) => `<label style="display:inline-flex;gap:3px;align-items:center"><span class="muted">${l}</span><input data-rscrit="${k}" type="number" step="${step}" value="${c[k] ?? ''}" style="width:${w}em;padding:1px 4px">${u ? `<span class="muted">${u}</span>` : ''}</label>`;
  const agent = `<div style="border:1px solid var(--coach,#5b5bd6);border-radius:8px;padding:8px 10px;margin:14px 0 6px">
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap"><b style="font-size:.95rem">AI research agent</b>
      <span class="muted" style="font-size:.75rem">designs whole strategies from scratch (every rule off, then the entries, filters, exits and formulas it chooses, over the Alpaca / Unusual Whales data); when an idea needs an indicator the app doesn't have, it creates it as a formula and the idea is re-run with it. It works after reading the record of every past idea (1st-half results only, never the 2nd half or the grades), and hands them to the test pipeline below. It can't run tests, change the criteria or touch your settings.</span></div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:.8rem">${(() => { const au = (t.auto || {})[strategy] || {};
      return `<button class="btn ${au.on ? 'coachbtn' : ''}" data-rx="auto|${strategy}" style="font-size:.78rem;padding:1px 12px" title="Every hour the agent proposes new strategies and the pipeline tests them, without you clicking">Autopilot ${au.on ? 'ON' : 'OFF'}</button>
        <label><select id="rs-aper" style="padding:1px 4px">${[[0, 'continuous'], [4, 'up to 4 runs a day'], [8, 'up to 8 runs a day'], [12, 'up to 12 runs a day'], [24, 'up to 24 runs a day']].map(([n, l]) => `<option value="${n}" ${n === (au.perDay ?? 4) ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
        <button class="btn" data-rx="autoset|${strategy}" style="font-size:.74rem;padding:0 8px" title="Save these autopilot settings and the focus">Save</button>
        <label><select id="rs-an" style="padding:1px 4px">${[2, 3, 4, 6, 8].map(n => `<option ${n === (au.n || 4) ? 'selected' : ''}>${n}</option>`).join('')}</select> <span class="muted">ideas a run</span></label>
        <label><span class="muted">on</span> <select id="rs-adays" style="padding:1px 4px">${[120, 250, 500].map(n => `<option ${n === (au.days || 250) ? 'selected' : ''} value="${n}">${n} days</option>`).join('')}</select></label>
        <span class="muted" style="font-size:.74rem">${au.on ? `runs back to back on its own: ${((t.autoToday || {})[strategy]) || 0}${au.perDay ? ` of ${au.perDay}` : ''} runs today${au.perDay && (((t.autoToday || {})[strategy]) || 0) >= au.perDay ? ' — done for today, starts again after midnight ET' : au.perDay ? '' : ' · continuous: until you turn it off'} (each run calls the AI once: API cost)` : 'off: runs only when you click'}</span>`; })()}</div>
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:.8rem">
      <input id="rs-focus" placeholder="Focus (optional): e.g. use CVD and the put wall for longs, avoid lunch hours" value="${esc(S.rsFocus ?? ((((t.auto || {})[strategy]) || {}).focus || ''))}" style="flex:1 1 320px;padding:2px 6px">
      <label><span class="muted">ideas</span> <select id="rs-n" style="padding:1px 4px">${[2, 3, 4, 6, 8].map(n => `<option ${n === (S.rsN || 4) ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
      <label><span class="muted">rounds</span> <select id="rs-rounds" style="padding:1px 4px">${[1, 2, 3].map(n => `<option ${n === (S.rsRounds || 1) ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
      <label><span class="muted">history</span> <select id="rs-days" style="padding:1px 4px">${[120, 250, 500].map(n => `<option ${n === (S.rsDays || 250) ? 'selected' : ''} value="${n}">${n} days</option>`).join('')}</select></label>
      <button class="btn coachbtn" data-rx="run|${strategy}" ${busy ? 'disabled' : ''} style="font-size:.8rem;padding:1px 12px">${busy ? 'Working…' : 'Propose & test'}</button></div>
    ${busy ? `<p style="font-size:.8rem;margin:6px 0 0"><b>${esc(run.step || '')}</b> <span class="muted">· ${run.made || 0} proposed · ${run.testedNow || 0} tested so far</span></p>` : ''}
    ${strategy === 'day' ? (() => { const m = t.mctx || {};
      return `<p style="font-size:.78rem;margin:6px 0 0"><b>Market context</b> <span class="muted">(options flow, dealer gamma, breadth, VIXY, events for the formulas)</span>: ${m.running ? '<b>loading…</b> ' : ''}${m.days ? `options flow / gamma on <b>${(m.cov || {}).uw ?? '—'}</b> of ${m.days} days · breadth / VIXY on <b>${(m.cov || {}).alp ?? '—'}</b>${m.left ? ` · ${m.left} left to load` : ''}${m.uwKey === false ? ' · <span class="loss">no Unusual Whales key</span>' : ''}${m.days && (m.cov || {}).uw === 0 && !m.left ? ' · <span class="loss">Unusual Whales returned nothing for these days (plan / history limit?)</span>' : ''}` : 'not loaded yet'}
        <button class="btn" data-rx="mctx|day|250" ${m.running ? 'disabled' : ''} style="font-size:.74rem;padding:0 8px">${m.days ? 'Load the missing days' : 'Load 250 days'}</button>
        <button class="btn" data-rx="mctx|day|500" ${m.running ? 'disabled' : ''} style="font-size:.74rem;padding:0 8px" title="Two years (for the 500-day backtest; Unusual Whales may not go back that far)">Load 500 days</button></p>`; })() : ''}
    ${!busy && run.at && run.step === 'done' ? `<p class="muted" style="font-size:.78rem;margin:6px 0 0">Last run ${esc((run.at || '').slice(5, 16).replace('T', ' '))}${run.auto ? ' (autopilot)' : ''}: ${run.made || 0} proposed · ${run.testedNow || 0} tested${run.secs ? ` · ${run.secs} s` : ''}</p>` : ''}
    ${(run.notes || []).length && !busy ? `<p style="font-size:.8rem;margin:6px 0 0"><b>Agent notes:</b> ${esc(run.notes.join(' '))}</p>` : ''}
    ${(run.errors || []).length ? `<p class="loss" style="font-size:.78rem;margin:4px 0 0">${esc(run.errors.join(' · '))}</p>` : ''}
    ${(t.runlog || []).length ? `<details style="margin-top:6px"><summary style="font-size:.78rem;cursor:pointer">Last runs (${t.runlog.length})</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.76rem"><thead><tr><th>When (ET)</th><th></th><th class="r">Proposed</th><th class="r">Tested</th><th class="r">Secs</th><th>Focus</th><th>Errors</th></tr></thead><tbody>
      ${t.runlog.slice().reverse().map(r0 => `<tr><td>${esc((r0.at || '').slice(5, 16).replace('T', ' '))}</td><td>${r0.strategy === 'swing' ? 'swing' : 'day'}${r0.auto ? ' · auto' : ''}</td><td class="r">${r0.made ?? '—'}</td><td class="r">${r0.testedNow ?? '—'}</td><td class="r">${r0.secs ?? '—'}</td><td>${esc(r0.focus || '')}${(r0.explore || []).length ? ` <span class="muted">exploration: no ${esc(r0.explore.join(', '))}</span>` : ''}</td><td class="loss">${esc((r0.errors || []).join(' · '))}</td></tr>`).join('')}</tbody></table></div></details>` : ''}</div>`;
  const robB = x => { const rb = x.robust; if (!rb) return ''; if (rb.status !== 'done') return ` <span class="muted" style="font-size:.7rem">· robustness ${esc(rb.status === 'error' ? 'failed' : rb.status === 'running' ? 'running…' : 'queued')}</span>`;
    const col = rb.verdict === 'robust' ? 'gain' : rb.verdict === 'fragile' ? 'loss' : ''; return ` <span class="${col}" style="font-size:.72rem;font-weight:600" title="${rb.days} days: ${rb.positive}/${rb.variants} variants positive, median ${rb.median}R">· ${esc(rb.verdict)} ${rb.positive}/${rb.variants}</span>`; };
  const robD = x => { const rb = x.robust; if (!rb) return ''; if (rb.status !== 'done') return `<div class="muted"><b>Robustness check:</b> ${esc(rb.status)}${(rb.errors || []).length ? ' · ' + esc(rb.errors.join(' · ')) : ''}</div>`;
    const b = rb.base || {}; return `<div><b>Robustness check</b> (automatic, ${rb.daysFound || rb.days} days, ${esc((rb.at || '').slice(5, 16).replace('T', ' '))}): <b class="${rb.verdict === 'robust' ? 'gain' : rb.verdict === 'fragile' ? 'loss' : ''}">${esc(rb.verdict)}</b> · ${rb.positive}/${rb.variants} variants positive (${rb.share ?? '—'}%) · median ${rb.median ?? '—'}R · as is ${b.n || 0} trades ${R(b.avgR)} t ${b.t ?? '—'} · halves ${R((rb.h1 || {}).avgR)} / ${R((rb.h2 || {}).avgR)}${(rb.extra || []).map(e => ` · ${esc(e.label)} ${R(e.avgR)}`).join('')}${(rb.worst || []).length ? `<br><span class="muted">weakest: ${rb.worst.map(w => `${esc(w.label)} ${R(w.avgR)}`).join(' · ')}</span>` : ''}${(rb.errors || []).length ? `<br><span class="loss">${esc(rb.errors.join(' · '))}</span>` : ''}</div>`; };
  const holdD = x => { const h0 = x.holdout; if (!h0) return '';
    const v = h0.verdict || (h0.pass ? 'passes' : 'fails'), dg = h0.diag || {};
    const why = v === 'no trades' ? ` — its conditions never came true on these ${dg.holdDays ?? '?'} days${dg.ctxDays != null && dg.holdDays && dg.ctxDays < dg.holdDays ? ` (market context loaded on only ${dg.ctxDays} of them: load the missing days)` : ' (a rare market condition, e.g. fear days, may simply not have happened)'}` : v === 'no data' ? ' — no stored days after the research period (the history is not up to date)' : v === 'too few trades' ? ' — not enough trades to judge' : '';
    return `<div><b>Final holdout check</b> (${h0.days} days from ${esc(h0.from || '')}${dg.to ? ` to ${esc(dg.to)}` : ''}, look #${h0.use}): <b class="${v === 'passes' ? 'gain' : v === 'fails' ? 'loss' : ''}">${esc(v)}</b>${esc(why)} · ${h0.n || 0} trades${h0.tradeDays ? ` on ${h0.tradeDays} days` : ''} · ${R(h0.avgR)} · t ${h0.t ?? '—'} · win ${h0.win ?? '—'}% · PF ${h0.pf ?? '—'}${dg.series != null ? ` <span class="muted">(${dg.series} stock-days checked)</span>` : ''}</div>`; };
  const rows = xs.map(x => { const m = x.metrics || {}, open = S.rsOpen === x.id;
    return `<tr data-rx="open|${x.id}" style="cursor:pointer"><td>${badge(x)}</td><td><b style="font-size:.8rem">${esc(x.name)}</b> <span class="muted" style="font-size:.72rem">${x.by === 'you' ? 'yours' : x.by === 'evolve' ? 'evolution' : 'AI'} · ${esc((x.at || '').slice(5, 16).replace('T', ' '))}${x.universe === 'inplay' ? ' · <b>stocks in play</b>' : ''}</span>${robB(x)}</td>
      <td class="r">${m.n ?? '—'}</td><td class="r">${m.win != null ? m.win + '%' : '—'}</td><td class="r">${R(m.avgR)}</td><td class="r">${m.t ?? '—'}${x.tReq ? ` <span class="muted">/ ${x.tReq}</span>` : ''}</td><td class="r">${m.pf ?? '—'}</td>
      <td class="r">${R((m.h1 || {}).avgR)} / ${R((m.h2 || {}).avgR)}</td><td class="r">${m.maxDD != null ? m.maxDD + 'R' : '—'}</td>
      <td style="font-size:.75rem">${x.status === 'tested' ? ((x.fails || []).length ? `<span class="loss">${esc(x.fails.join(' · '))}</span>` : '<span class="gain">all criteria pass</span>') : ['banned piece', 'family exhausted', 'failed combo'].includes(x.status) ? `<span class="muted">${esc(x.status)}: ${esc((x.fails || []).join(' · '))}</span>` : esc((x.missing || []).join(' · ') || x.status || '')}</td></tr>`
      + (open ? `<tr><td></td><td colspan="9" style="font-size:.8rem;background:var(--sunk);padding:6px 8px">
        ${x.hypothesis ? `<div><b>Why:</b> ${esc(x.hypothesis)}</div>` : ''}${x.learnedFrom ? `<div><b>Builds on:</b> ${esc(x.learnedFrom)}</div>` : ''}
        <div><b>Changes:</b> ${ch(x) || '—'}</div>${(m.folds || []).length ? `<div><b>Walk-forward windows:</b> ${m.folds.map(f0 => `<span title="from ${esc(f0.from)}">${esc(f0.from.slice(2, 7))} ${f0.n} · ${R(f0.avgR)}</span>`).join(' · ')} → <b class="${m.foldsPos >= (c.minFolds || 0) ? 'gain' : 'loss'}">${m.foldsPos}/${m.folds.length} positive</b></div>` : ''}${robD(x)}${holdD(x)}
        ${(x.indicators || []).length ? `<div><b>Indicators created:</b> ${x.indicators.map(nm => { const d = (t.indicators || []).find(i => i.name === nm); return `<code style="font-size:.74rem">${esc(nm)} = ${esc(d ? d.formula : '?')}</code>`; }).join(' · ')}</div>` : ''}
        ${x.rebuilt ? `<div class="gain">Rebuilt by the agent with new indicators (it was waiting for: ${esc((x.wasMissing || []).join(' · '))})</div>` : ''}
        ${(x.missing || []).length ? `<div><b>${x.status === 'needs data' ? 'Needs data the app doesn\'t have' : 'Needs'}:</b> ${esc(x.missing.join(' · '))}${x.status === 'needs feature' && !x.buildTried ? ' <span class="muted">(the agent will create the indicators on its next run)</span>' : ''}</div>` : ''}
        ${x.status === 'tested' ? `<div class="muted">${esc((x.period || {}).from || '')} → ${esc((x.period || {}).to || '')}, halves split at ${esc((x.period || {}).mid || '')} · ${m.days != null ? `${m.days} trading days with trades · t per trade ${m.tTrade ?? '—'}, per day ${m.tDay ?? '—'} (the lower one counts) · ` : ''}cost ${m.cost}R a trade taken off · 1st half ${(m.h1 || {}).n} trades t ${(m.h1 || {}).t ?? '—'} · 2nd half ${(m.h2 || {}).n} trades t ${(m.h2 || {}).t ?? '—'}${(x.bySym || []).length ? ' · ' + x.bySym.map(r => `${esc(r.sym)} ${r.totR > 0 ? '+' : ''}${r.totR}R`).join(' ') : ''}</div>` : ''}
        <div style="display:flex;gap:6px;margin-top:4px">${Object.keys(x.changes || {}).length ? `<button class="btn coachbtn" data-rx="use|${x.id}|${strategy}" style="font-size:.75rem;padding:1px 10px">Load into ${strategy === 'day' ? 'the settings' : 'the lab'}</button>` : ''}${x.status === 'tested' ? `<button class="btn coachbtn" data-rx="ssavei|${x.id}|${strategy}" style="font-size:.75rem;padding:1px 10px" title="Keep it: re-tested every day, and the agent looks for other strategies">Save as strategy</button>` : ''}${strategy === 'day' && x.status === 'tested' && c.holdoutDays ? `<button class="btn" data-rx="hold|${x.id}|${strategy}" style="font-size:.75rem;padding:1px 10px" title="The final test on the last ${c.holdoutDays} days that no research test has used. Use it once, on a finalist: every look makes it less clean.">${x.holdout ? 'Holdout check again' : 'Final holdout check'}</button>` : ''}${strategy === 'day' && x.status === 'tested' ? `<button class="btn" data-rx="rob|${x.id}|${strategy}" style="font-size:.75rem;padding:1px 10px" title="Load it into the settings and run the robustness check (Backtest & tools tab)">Robustness check</button>` : ''}<button class="btn" data-rx="del|${x.id}|${strategy}" style="font-size:.75rem;padding:1px 10px">Delete</button></div></td></tr>` : ''); }).join('');
  const pipe = `<div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0">
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap"><b style="font-size:.95rem">Test pipeline</b>
      <span class="muted" style="font-size:.75rem">plain code, no AI: every idea (the agent's or yours) runs on the same history with the same criteria · ${t.tested || 0} ideas tested · t needed now ${t.tReq ?? '—'}${c.adjustT ? ` (raised for ${t.nTrials ?? '—'} ${c.adjustBy === 'idea' ? 'ideas' : 'entry families'})` : ''} · ${nA} A · ${nB} B</span></div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:.78rem;margin:6px 0">
      ${num('minTrades', 10, 4.2, 'trades ≥')}${num('minHalf', 5, 3.6, 'each half ≥')}${num('minT', 0.1, 3.4, 't ≥')}${num('minTestT', 0.1, 3.4, '2nd-half t ≥')}${num('minPF', 0.05, 3.4, 'PF ≥')}${num('minWin', 1, 3.2, 'win ≥', '%')}${num('minAvgR', 0.01, 3.6, 'avg ≥', 'R')}${num('costR', 0.01, 3.6, 'cost', 'R / trade')}
      <label><input type="checkbox" data-rscritb="bothHalves" ${c.bothHalves ? 'checked' : ''}> positive on both halves</label>
      <label title="Bonferroni: the t needed rises with the number of ideas tested, so a long search doesn't pass luck"><input type="checkbox" data-rscritb="adjustT" ${c.adjustT ? 'checked' : ''}> raise t with the number of ideas</label>
      ${strategy === 'day' ? `<label title="Every idea graded C or better with t above this gets the robustness check on its own (one at a time, in the background)"><input type="checkbox" data-rscritb="autoRob" ${c.autoRob ? 'checked' : ''}> auto robustness check when t ></label>${num('robT', 0.1, 3.2, '')}${num('robDays', 50, 4, 'on', 'days')}` : ''}
      ${strategy === 'day' ? (() => { const sp = t.sip || {}; return `<div style="flex-basis:100%;display:flex;gap:8px;align-items:center;flex-wrap:wrap;border:1px dashed var(--line);border-radius:6px;padding:4px 8px">
        <b>Research universe</b> <select data-rscritsel="universe" style="padding:1px 4px"><option value="tickers" ${(c.universe || 'tickers') === 'tickers' ? 'selected' : ''}>fixed tickers</option><option value="inplay" ${c.universe === 'inplay' ? 'selected' : ''}>stocks in play (top by relative volume, each day)</option></select>
        ${num('sipTop', 1, 3, 'top', 'stocks a day')}
        <span class="muted" style="font-size:.74rem">stocks-in-play history: <b>${sp.stored || 0}</b> days stored${sp.stored ? ` · chart structure on <b>${sp.structured || 0}</b>` : ''}${sp.running ? ` · <b>building…</b> ${esc(sp.progress || '')}` : sp.progress ? ` · ${esc(sp.progress)}` : ''}${sp.error ? ` · <span class="loss">${esc(sp.error)}</span>` : ''}</span>
        <button class="btn" data-rx="sipprep|500|day" ${sp.running ? 'disabled' : ''} style="font-size:.74rem;padding:0 8px" title="Each day's top stocks by opening-range RVOL: their 5-minute candles, 30-minute candles of the 5 sessions before and of the pre-market, and QQQ (once, then extended every evening)">${!sp.stored ? 'Build the stocks-in-play history (500 days)' : (sp.structured || 0) < sp.stored ? 'Load the missing days + chart structure' : 'Load the missing days'}</button>
        <span class="muted" style="font-size:.72rem">ideas, the map of what works, retired pieces and the never-again lists are kept apart per universe · on stocks in play the options flow / gamma are the market's (QQQ), no walls / dark pool, no entry before 09:35</span></div>`; })() : ''}
      ${strategy === 'day' ? `<label title="The research tests every day idea on these tickers (more tickers = more trades, so a real edge can reach the t needed); empty = your day-trading tickers"><span class="muted">research tickers</span> <input data-rscrittxt="dayTickers" value="${esc((c.dayTickers || []).join(' '))}" placeholder="QQQ SPY IWM AAPL MSFT NVDA AMZN META TSLA AMD" style="width:22em;padding:1px 4px"></label>${num('familyCap', 1, 3.4, 'max ideas per entry family', '(0 = no cap)')}${num('exploreEvery', 1, 3.4, 'every', 'runs: exploration round (0 = off), banning the pieces of the top')}${num('banTop', 1, 3, '', 'ideas')}
      <label title="A piece used in enough ideas whose lift is not positive on the 2nd half is retired: the agent can't use it any more"><input type="checkbox" data-rscritb="retire" ${c.retire ? 'checked' : ''}> retire pieces that fail the 2nd half</label>${num('retireMin', 1, 3, 'after', 'ideas')}
      ${num('holdoutDays', 5, 3.6, 'holdout: the last', 'days kept out of every research test')}
      ${num('minFolds', 1, 3, 'walk-forward: positive in ≥', '')}${num('folds', 1, 3, 'of', 'windows')}
      <label title="Use the lower of the per-trade t and the t of the R summed per day: many trades of the same day (a market-wide condition) count as one bet"><input type="checkbox" data-rscritb="dayT" ${c.dayT !== false ? 'checked' : ''}> t per day too</label>
      <label title="What the t needed is raised for: each distinct entry family (variants of one strategy are not independent bets), or every idea"><span class="muted">raise t per</span> <select data-rscritsel="adjustBy" style="padding:1px 4px">${[['family', 'entry family'], ['idea', 'idea']].map(([v, l]) => `<option value="${v}" ${(c.adjustBy || 'family') === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>` : ''}
      <button class="btn" data-rx="crit|${strategy}" style="font-size:.75rem;padding:1px 10px">Save criteria</button>
      <button class="btn" data-rx="mine|${strategy}" ${busy ? 'disabled' : ''} style="font-size:.75rem;padding:1px 10px" title="Your current ${strategy === 'day' ? 'settings (with the changes not applied yet)' : 'lab settings'} vs your saved / live settings, as an idea of your own">Test my ${strategy === 'day' ? 'settings' : 'lab settings'} as an idea</button></div>
    <p class="muted" style="font-size:.74rem;margin:0 0 4px">Grade: <b style="color:${GC.A}">A</b> every criterion passes and the 2nd half alone reaches the t needed · <b style="color:${GC.B}">B</b> every criterion passes · <b style="color:${GC.C}">C</b> positive on both halves, some criteria fail · <b style="color:${GC.F}">F</b> the rest. Ideas needing data the app doesn't have are kept as feature requests.</p>
    <div style="display:flex;gap:4px;font-size:.75rem;margin-bottom:4px;align-items:center">${[['all', 'All'], ['graded', 'Tested'], ['ab', 'A / B'], ['needs', 'Waiting / needs data'], ['long', 'Long only'], ['short', 'Short only'], ['both', 'Both sides']].map(([k, l]) => `<button class="btn ${filt === k ? 'coachbtn' : ''}" data-rx="filter|${k}" style="font-size:.74rem;padding:0 8px">${l}</button>`).join('')}
      <span class="muted" style="margin-left:10px">sort</span>${[['best', 'Best first'], ['new', 'Newest first']].map(([k, l]) => `<button class="btn ${srt === k ? 'coachbtn' : ''}" data-rx="sort|${k}" style="font-size:.74rem;padding:0 8px">${l}</button>`).join('')}</div>
    ${(t.blocks || []).length ? `<details style="margin:2px 0 6px" ${S.rsBlocksOpen ? 'open' : ''}><summary style="font-size:.8rem;cursor:pointer" data-rx="blocks|${strategy}"><b>What works: building blocks</b> <span class="muted">(across ${t.tested || 0} tested ideas: average R of the ideas WITH each piece minus the ideas WITHOUT it; the agent sees the 1st half only, the 2nd half shows if it holds)</span></summary>
      <div style="overflow-x:auto"><table class="pos" style="font-size:.76rem"><thead><tr><th>Piece</th><th class="r">Ideas with</th><th class="r">1st-half lift</th><th class="r">t</th><th class="r">2nd-half lift</th><th>Holds?</th></tr></thead><tbody>
      ${t.blocks.map(b0 => { const l1 = (b0.lift || {}).h1, l2 = (b0.lift || {}).h2, ret = (t.retired || []).includes(b0.block), holds = (l1 > 0 && l2 > 0 ? '<span class="gain">yes</span>' : l1 < 0 && l2 < 0 ? '<span class="loss">hurts on both</span>' : '<span class="muted">no</span>') + (ret ? ' <b class="loss">· retired</b>' : '');
        return `<tr><td>${esc(b0.block)}</td><td class="r">${b0.n} <span class="muted">/ ${b0.n + b0.nOut}</span></td><td class="r">${R(l1)}</td><td class="r">${b0.t ?? '—'}</td><td class="r">${R(l2)}</td><td>${holds}</td></tr>`; }).join('')}</tbody></table></div>
      ${(t.families || []).length ? `<p style="font-size:.76rem;margin:4px 0"><b>Entry families tried:</b> ${t.families.map(([k, v]) => `${esc(k)} <b>${v}</b>`).join(' · ')}</p>` : ''}</details>` : ''}
    ${(t.indicators || []).length ? `<details style="margin:2px 0 6px"><summary style="font-size:.8rem;cursor:pointer"><b>Indicators the agent created</b> <span class="muted">(${t.indicators.length}: formulas over the Alpaca / Unusual Whales data, reused by later ideas)</span></summary>
      <div style="overflow-x:auto"><table class="pos" style="font-size:.78rem"><thead><tr><th>Name</th><th>Formula</th><th>What</th><th>For</th></tr></thead><tbody>${t.indicators.slice().reverse().map(i => `<tr><td><code>${esc(i.name)}</code></td><td><code style="font-size:.74rem">${esc(i.formula)}</code></td><td>${esc(i.what || '')}</td><td class="muted">${esc(i.idea || '')}</td></tr>`).join('')}</tbody></table></div></details>` : ''}
    ${t.error ? `<div class="errbox">${esc(t.error)}</div>` : xs.length ? `<div style="overflow-x:auto;max-height:520px;overflow-y:auto"><table class="pos" style="font-size:.8rem"><thead><tr><th></th><th>Idea</th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg R</th><th class="r">t / needed</th><th class="r">PF</th><th class="r">1st / 2nd half</th><th class="r">Max DD</th><th>Result</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="muted" style="font-size:.8rem">No ideas yet.</p>'}</div>`;
  return rsStratBox(strategy, t) + agent + (strategy === 'day' ? evoBox(t) : '') + pipe;
}
function evoBox(t) {
  const e = t.evolve || {}, R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  if (e.error) return `<div class="errbox">${esc(e.error)}</div>`;
  const last = e.last || {};
  return `<div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:14px 0 6px">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b style="font-size:.95rem">Evolution</b>
      <span class="muted" style="font-size:.75rem">genetic search over formula strategies (no AI): 1st half only, 3 walk-forward windows, extra conditions cost, rarely used pieces earn a bonus, retired pieces are out, and an atom set whose idea didn't pay on the 2nd half is never tried again. The best one of each run goes to the ideas list (by “evolve”), graded by the same pipeline with every evaluation counted in its t needed.</span></div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:.8rem">
      <label style="display:inline-flex;gap:4px;align-items:center"><input type="checkbox" data-evoon ${e.on ? 'checked' : ''}> run continuously</label>
      ${e.universe === 'inplay' ? '<span style="font-size:.78rem"><b>universe: stocks in play</b></span>' : ''}<label ${e.universe === 'inplay' ? 'style="display:none"' : ''} title="Few tickers = many more strategies tested per run (each strategy runs on every ticker). Empty = the first 4 research tickers."><span class="muted">tickers</span> <input id="evo-tk" value="${esc((e.tickers || []).join(' '))}" placeholder="first 4 research tickers" style="width:13em;padding:1px 4px"></label>
      <span class="muted">history</span> <select id="evo-days" style="padding:1px 4px">${[120, 250, 500].map(n => `<option ${n === (e.days || 250) ? 'selected' : ''} value="${n}">${n} days</option>`).join('')}</select>
      <button class="btn coachbtn" data-rx="evorun|1|day" ${e.running ? 'disabled' : ''} style="font-size:.76rem;padding:0 10px">${e.running ? 'Running…' : 'Run once'}</button>
      <button class="btn" data-rx="evoreset|1|day" style="font-size:.74rem;padding:0 8px" title="Start a new population (the evaluated set and the taboo list are kept)">New population</button>
      <span class="muted" style="font-size:.76rem">generation ${e.gen || 0} · ${e.evals || 0} strategies evaluated · ${e.taboo || 0} taboo${e.runsMax ? ` · runs today ${e.runsToday || 0}/${e.runsMax}${e.on && (e.runsToday || 0) >= e.runsMax && !e.running ? ' <b class="loss">daily limit reached: continues after midnight ET</b>' : ''}` : ''}${last.at ? ` · last run ${esc(last.at.slice(5, 16).replace('T', ' '))} ET: ${last.gens ?? 0} generations, ${last.evals ?? 0} evaluated${last.loadSecs != null ? `, candles loaded in ${last.loadSecs} s` : ''}${last.side ? `, focus: <b>${esc(last.side)}</b>` : ''}${(last.jolt || []).length ? `, <b>jolt</b> (without ${esc(last.jolt.join(' · '))})` : ''}` : ''}${last.error && !e.running ? ` · <span class="loss">${esc(last.error)}</span>` : ''}</span></div>
    ${e.running ? `<p style="font-size:.8rem;margin:4px 0 0"><b>Now:</b> ${esc(e.step || 'starting')} <span class="muted">(started ${esc((e.running || '').slice(11, 16))} ET; this box refreshes by itself)</span></p>` : ''}
    ${e.timedOut ? `<p class="loss" style="font-size:.78rem;margin:4px 0 0">The last run was cut off by the 10-minute limit${e.step ? ` while: ${esc(e.step)}` : ''}. Its progress so far is kept; if it keeps happening, use fewer tickers or 120 days.</p>` : ''}
    ${(e.top || []).length ? `<div style="overflow-x:auto"><table class="pos" style="font-size:.76rem;margin-top:4px"><thead><tr><th>Best of the population (1st half only)</th><th class="r">Trades</th><th class="r">Days</th><th class="r">Avg R</th><th class="r" title="the lower of the per-trade t and the per-day t">t</th><th class="r">Windows +</th><th class="r">Rare pieces</th><th class="r">Fitness</th></tr></thead><tbody>
      ${e.top.map(p0 => `<tr><td><b>${esc(p0.name)}</b><div class="muted" style="font-size:.7rem">${p0.formulas.long ? `long: <code>${esc(p0.formulas.long)}</code> ` : ''}${p0.formulas.short ? `short: <code>${esc(p0.formulas.short)}</code> ` : ''}· stop ${p0.stop} ATR · ${String(p0.target || '').startsWith('trail') ? `trailing ${String(p0.target).slice(5)} ATR` : p0.target ? `target ${p0.target}R` : 'no target'}</div></td><td class="r">${p0.n ?? '—'}</td><td class="r">${p0.days ?? '—'}</td><td class="r">${R(p0.avgR)}</td><td class="r" title="per trade ${p0.tTrade ?? '—'} · per day ${p0.tDay ?? '—'}">${p0.t ?? '—'}</td><td class="r">${p0.posWin ?? '—'}/3</td><td class="r">${p0.novel ?? 0}</td><td class="r"><b>${p0.fit ?? '—'}</b></td></tr>`).join('')}</tbody></table></div>` : ''}
    ${(e.promoted || []).length ? `<p style="font-size:.76rem;margin:4px 0 0"><b>Sent to the ideas list:</b> ${e.promoted.slice().reverse().map(p0 => `${esc(p0.name)} <b>${esc(p0.grade || '')}</b>${p0.h2 != null ? ` (2nd half ${R(p0.h2)}${p0.h2 <= 0 ? ' → taboo' : ''})` : ''}`).join(' · ')}</p>` : ''}</div>`;
}
function ideaSide(x) { const f = { ...(x.full || {}), ...(x.changes || {}) }; return f.longs === false && f.shorts !== false ? 'short' : f.shorts === false && f.longs !== false ? 'long' : 'both'; }
function basketBox(bk, ss) {
  const bc = bk.cfg || {}, R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const cb = bk.combined, one = bk.bestAlone, rows = bk.rows || [];
  const st = x => x ? `${x.days} days · ${R(x.totR)} · ${R(x.avgDay)} a day · t ${x.t ?? '—'} · max drawdown ${R(x.maxDD)} · green days ${x.greenPct ?? '—'}%` : '—';
  const need = (bk.missingSides || []).map(s0 => ({ long: 'long only', short: 'short only', both: 'both sides' }[s0]));
  const corr = rows.length > 1 && rows.length <= 8 ? `<details style="margin-top:4px"><summary style="font-size:.78rem;cursor:pointer">How much they move together (correlation of daily R, last 250 days)</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.74rem"><thead><tr><th></th>${rows.map((r0, i) => `<th class="r">${i + 1}</th>`).join('')}</tr></thead><tbody>
    ${rows.map((r0, i) => `<tr><td>${i + 1}. ${esc(r0.name)}</td>${(bk.corr[i] || []).map((v, j) => `<td class="r ${i !== j && v != null && v > 0.6 ? 'loss' : i !== j && v != null && v < 0.2 ? 'gain' : ''}">${v == null ? '—' : (+v).toFixed(2)}</td>`).join('')}</tr>`).join('')}</tbody></table></div><p class="muted" style="font-size:.72rem;margin:2px 0">green: under 0.2 (they win and lose on different days: good for the basket) · red: over 0.6 (nearly the same strategy)</p></details>` : '';
  return `<div style="border-top:1px dashed var(--line);margin-top:8px;padding-top:6px;font-size:.8rem">
    <b>Basket</b> <span class="muted" style="font-size:.74rem">every saved strategy above, ranked by its score on the last 60 days; one is used live at a time</span>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0">
      <label style="display:inline-flex;gap:4px;align-items:center"><input type="checkbox" data-bkauto ${bc.auto ? 'checked' : ''}> switch automatically to the best one</label>
      <span class="muted" style="font-size:.74rem">only promoted ones (paper ${bc.paperDays || 20} days, ${bc.paperTrades || 15} trades) · kill switch: a promoted one drifting below its backtest (z < ${bc.killZ ?? -2} over 20 days) or losing 1.5× its backtest drawdown is killed; if it was in use, the basket switches or the orders go OFF</span>
      <span class="muted">side</span> ${[['any', 'any'], ['long', 'long only'], ['short', 'short only'], ['both', 'both sides']].map(([v, l]) => `<button class="btn ${bc.side === v ? 'coachbtn' : ''}" data-rx="bside|${v}|day" style="font-size:.72rem;padding:0 8px">${l}</button>`).join('')}
      <span class="muted" style="font-size:.74rem">after the daily check (17:00 ET); it switches only when the new best scores ${Math.round((bc.margin ?? 0.25) * 100)}% above the one in use, or the one in use is no longer healthy</span></div>
    <div>Basket (all ${cb ? cb.members : 0} healthy, equal weight): ${st(cb)}</div><div>Best one alone: ${st(one)}</div>
    ${need.length ? `<div class="muted" style="font-size:.76rem">Missing in the basket: <b>${esc(need.join(', '))}</b> — the AI agent now aims most of its ideas there.</div>` : ''}
    ${(bc.log || []).length ? `<div class="muted" style="font-size:.74rem">Switches: ${bc.log.slice(-5).reverse().map(l0 => `${esc((l0.at || '').slice(5, 16).replace('T', ' '))} → ${esc(l0.to)}${l0.by === 'you' ? ' (you)' : ''}`).join(' · ')}</div>` : ''}${corr}</div>`;
}
function stratRecent(x, c) {                     // a saved strategy's trades of the last 30 days, grouped by day
  if (!c || !c.recent) return '<p class="muted" style="font-size:.78rem;margin:4px 0">No trade list yet: it comes with the next check (click <b>Re-check now</b>, a few minutes).</p>';
  const by = {};
  c.recent.forEach(r => { const d = (r.at || '').slice(0, 10); (by[d] = by[d] || []).push(r); });
  const days = (c.days30 || []).slice().reverse(), Rf = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const tot = c.recent.reduce((a, r) => a + (+r.R || 0), 0), cost = +c.cost || 0;
  let eq = 0; const curve = days.slice().reverse().map(d => (eq += (by[d] || []).reduce((a, r) => a + (+r.R || 0) - cost, 0)));
  const mx = Math.max(0.5, ...curve.map(Math.abs));
  const spark = curve.length > 1 ? `<svg width="240" height="40" viewBox="0 0 240 40" style="vertical-align:middle"><line x1="0" x2="240" y1="20" y2="20" stroke="var(--line)"/><path d="${curve.map((v, i) => `${i ? 'L' : 'M'}${(i / (curve.length - 1) * 238 + 1).toFixed(1)},${(20 - v / mx * 18).toFixed(1)}`).join('')}" fill="none" stroke="var(--coach,#5b5bd6)" stroke-width="1.6"/></svg>` : '';
  const rows = days.map((d, i) => { const ts = by[d] || [], s0 = ts.reduce((a, r) => a + (+r.R || 0), 0);
    return `<tr><td style="white-space:nowrap">${esc(d)}</td><td class="r">${ts.length || '<span class="muted">0</span>'}</td><td class="r">${ts.length ? Rf(s0) : ''}</td><td style="font-size:.76rem">${ts.map(r => `<button class="btn" data-stview="chart|${x.id}|${esc(r.sym || '')}|${i + 1}" style="font-size:.72rem;padding:0 6px;margin:1px" title="Open ${esc(r.sym || '')} on the chart with this strategy's settings">${esc(r.sym || '')} ${r.dir === 'short' ? 'S' : 'L'} ${esc((r.at || '').slice(11, 16))} ${r.entry != null ? (+r.entry).toFixed(2) : ''}→${r.exit != null ? (+r.exit).toFixed(2) : '…'} ${Rf(r.R)}</button>`).join('')}</td></tr>`; }).join('');
  return `<div style="border:1px solid var(--line);border-radius:8px;padding:6px 8px;margin:2px 0 8px">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:.8rem"><b>Last 30 trading days</b> ${c.recent.length} trades · ${Rf(tot)} before the cost (${Rf(tot - cost * c.recent.length)} after) ${spark}
    <span class="muted" style="font-size:.74rem">checked ${esc((c.at || '').slice(5, 16).replace('T', ' '))} · each button opens that ticker on the chart with this strategy's settings (its signals drawn)</span></div>
    <div style="overflow-x:auto;max-height:420px;overflow-y:auto"><table class="pos" style="font-size:.8rem;margin:4px 0"><thead><tr><th>Day</th><th class="r">Trades</th><th class="r">Day R</th><th>Trades (ticker side entry time entry→exit R)</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}
function rsStratBox(strategy, t) {
  const ss = t.strategies || [], R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const ST = { healthy: ['gain', 'healthy'], weakening: ['', 'weakening (last 60 days ≤ 0)'], failing: ['loss', 'failing'] };
  const bk = t.basket || {}, bc = bk.cfg || {}, brow = id => (bk.rows || []).find(r0 => r0.id === id) || {};
  const SIDE = { long: ['gain', 'long only'], short: ['loss', 'short only'], both: ['', 'both sides'] };
  const sideOf = x => x.side || ((x.full || {}).longs === false ? 'short' : (x.full || {}).shorts === false ? 'long' : 'both');
  const rows = ss.slice().sort((p, q) => (brow(q.id).score ?? -99) - (brow(p.id).score ?? -99)).map(x => { const c = (x.checks || []).slice(-1)[0], st = c ? ST[c.status] || ['', c.status] : ['muted', 'not checked yet'];
    const sg = x.stage || { status: 'paper' }, fw = sg.fwd || {}, SG = { paper: ['', 'paper'], promoted: ['gain', 'promoted'], dropped: ['loss', 'dropped'], killed: ['loss', 'killed'] }[sg.status] || ['', sg.status];
    const stageCell = `<b class="${SG[0]}">${SG[1]}</b>${sg.status === 'paper' ? ` <span class="muted">day ${fw.days ?? 0}/${bc.paperDays || 20} · ${fw.n ?? 0}/${bc.paperTrades || 15} trades</span>` : ''}<div class="muted" style="font-size:.7rem">${fw.n ? `since ${esc(sg.start || '')}: ${fw.n} trades ${R(fw.avgR)} · z ${fw.z ?? '—'}` : `since ${esc(sg.start || '')}`}${sg.why ? `<br>${esc(sg.why)}` : ''}</div>`;
    const sd = sideOf(x), br = brow(x.id), tags = [bc.active === x.id ? '<b class="gain">● in use</b>' : '', bk.best === x.id ? '<b>best now</b>' : '', (bk.bySide || {})[sd] === x.id && bk.best !== x.id ? `best ${esc(SIDE[sd][1])}` : ''].filter(Boolean).join(' · ');
    const hist = (x.checks || []).slice(-30).map(k => (k.last60 || {}).avgR ?? 0), mx = Math.max(0.05, ...hist.map(Math.abs));
    const spark = hist.length > 1 ? `<svg width="90" height="22" viewBox="0 0 90 22" style="vertical-align:middle"><line x1="0" x2="90" y1="11" y2="11" stroke="var(--line)"/><path d="${hist.map((v, i) => `${i ? 'L' : 'M'}${(i / (hist.length - 1) * 88 + 1).toFixed(1)},${(11 - v / mx * 10).toFixed(1)}`).join('')}" fill="none" stroke="var(--coach,#5b5bd6)" stroke-width="1.5"/></svg>` : '';
    return `<tr><td><b>${esc(x.name)}</b> <span class="${SIDE[sd][0]}" style="font-size:.72rem;border:1px solid var(--line);border-radius:4px;padding:0 4px">${SIDE[sd][1]}</span>${tags ? ` <span style="font-size:.72rem">${tags}</span>` : ''}<div class="muted" style="font-size:.72rem">saved ${esc((x.at || '').slice(0, 10))}${x.baseline ? ` · when found: ${x.baseline.n} trades ${x.baseline.avgR > 0 ? '+' : ''}${x.baseline.avgR}R t ${x.baseline.t}` : ''}</div></td>
      <td style="font-size:.76rem;min-width:11em">${stageCell}</td><td class="${st[0]}" style="font-size:.78rem">${st[1]}</td><td class="r">${c ? `${(c.all || {}).n} · ${R((c.all || {}).avgR)} · t ${(c.all || {}).t ?? '—'}` : '—'}</td>
      <td class="r">${c ? `${(c.last60 || {}).n} · ${R((c.last60 || {}).avgR)}` : '—'}</td><td class="r">${c ? `${(c.last20 || {}).n} · ${R((c.last20 || {}).avgR)}` : '—'}</td>
      <td class="r">${br.score ?? '—'}</td><td class="r" style="font-size:.74rem">${c && c.long ? `L ${c.long.n} · ${R(c.long.avgR)}` : ''}${c && c.short ? `<br>S ${c.short.n} · ${R(c.short.avgR)}` : ''}</td><td>${spark}</td>
      <td style="white-space:nowrap">${strategy === 'day' ? `<button class="btn ${bc.active === x.id ? '' : 'coachbtn'}" data-rx="buse|${x.id}|${strategy}" style="font-size:.72rem;padding:0 8px" title="Put it in the live day-trading settings now">${bc.active === x.id ? 'In use' : 'Use live'}</button> ` : ''}<button class="btn" data-rx="suse|${x.id}|${strategy}" style="font-size:.72rem;padding:0 8px">Load</button>${strategy === 'day' ? ` <button class="btn ${S.stratOpen === x.id ? 'coachbtn' : ''}" data-stview="open|${x.id}" style="font-size:.72rem;padding:0 8px" title="Its trades of the last 30 days, each one on the chart">Last 30 days</button> <button class="btn" data-rx="sstage|${x.id}|restart" style="font-size:.72rem;padding:0 8px" title="Start its paper period again from today">Restart paper</button>${sg.status !== 'dropped' ? ` <button class="btn" data-rx="sstage|${x.id}|drop" style="font-size:.72rem;padding:0 8px">Drop</button>` : ''}` : ''} <button class="btn" data-rx="sdel|${x.id}|${strategy}" style="font-size:.72rem;padding:0 8px">Delete</button></td></tr>${S.stratOpen === x.id ? `<tr><td colspan="10">${stratRecent(x, c)}</td></tr>` : ''}`; }).join('');
  return `<div style="border:1px solid var(--gain);border-radius:8px;padding:8px 10px;margin:14px 0 6px">
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap"><b style="font-size:.95rem">Saved strategies</b>
      <span class="muted" style="font-size:.75rem">kept apart from the ideas: re-tested every day after the close on the last 250 days (all, last 60 and last 20 days, after the cost), so you see when one weakens. The AI agent knows them and looks for OTHER strategies, not copies.</span>
      ${strategy === 'day' ? `<button class="btn" data-rx="scheck|day" style="font-size:.74rem;padding:0 8px">Re-check now</button>` : ''}
      <button class="btn" data-rx="ssave|${strategy}" style="font-size:.74rem;padding:0 8px" title="Save the settings on screen as a strategy">Save the current ${strategy === 'day' ? 'settings' : 'lab settings'}</button></div>
    ${ss.length ? `<div style="overflow-x:auto"><table class="pos" style="font-size:.8rem;margin-top:4px"><thead><tr><th>Strategy</th><th title="paper: trading forward on new days since it was saved; promoted after enough days and trades in line with its backtest; dropped if not; killed if a promoted one drifts below its backtest">Stage</th><th>Status</th><th class="r">250 days: trades · avg · t</th><th class="r">Last 60 days</th><th class="r">Last 20 days</th><th class="r" title="t of the daily R over the last 60 days: how the basket ranks them">Score (60 d)</th><th class="r">Long / short</th><th>Last 60 days, check by check</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>${strategy === 'day' ? basketBox(bk, ss) : ''}`
      : '<p class="muted" style="font-size:.8rem;margin:4px 0 0">None yet: open a good idea below and click “Save as strategy”.</p>'}${strategy === 'swing' && ss.length ? '<p class="muted" style="font-size:.74rem;margin:4px 0 0">The daily re-check runs for day-trading strategies; swing ones are kept and shown to the agent.</p>' : ''}</div>`;
}
function rsAction([act, a, b]) {
  const val = id => (document.getElementById(id) || {}).value;
  const busy = st => { S.rs[st] = { ...(S.rs[st] || {}), run: { running: 'now', step: 'starting', made: 0, testedNow: 0 } }; render(); setTimeout(() => loadRs(st), 2500); };
  if (act === 'tab') { S.rsTab = a; try { localStorage.setItem('rsTab', a); } catch (e) {} if (a === 'ai') Object.keys(S.rs || {}).forEach(k => loadRs(k)); else clearTimeout(S.rsPoll); render(); return; }
  if (act === 'filter') { S.rsFilter = a; render(); return; }
  if (act === 'sort') { S.rsSort = a; try { localStorage.setItem('rsSort', a); } catch (e) {} render(); return; }
  if (act === 'open') { S.rsOpen = S.rsOpen === a ? null : a; render(); return; }
  if (act === 'run') {
    S.rsFocus = val('rs-focus') || ''; S.rsN = +val('rs-n') || 4; S.rsRounds = +val('rs-rounds') || 1; S.rsDays = +val('rs-days') || 250;
    api('/research/run', { method: 'POST', body: { strategy: a, n: S.rsN, rounds: S.rsRounds, days: S.rsDays, focus: S.rsFocus, catalog: rsCatalog(a), lab: rsCur(a) } })
      .then(() => busy(a)).catch(err => toast(err.message)); return; }
  if (act === 'auto') {
    const au = (((S.rs[a] || {}).auto) || {})[a] || {};
    S.rsFocus = val('rs-focus') ?? S.rsFocus;
    const body = { strategy: a, on: !au.on, perDay: +(val('rs-aper') ?? 4), n: +val('rs-an') || 4, days: +val('rs-adays') || 250, focus: S.rsFocus || '', catalog: rsCatalog(a) };
    api('/research/auto', { method: 'PUT', body }).then(r => { S.rs[a] = { ...(S.rs[a] || {}), auto: r.auto, autoToday: r.autoToday }; toast(body.on ? 'Autopilot on: starting now' : 'Autopilot off'); render(); setTimeout(() => loadRs(a), 2500); }).catch(err => toast(err.message)); return; }
  if (act === 'autoset') {
    S.rsFocus = val('rs-focus') ?? S.rsFocus;
    api('/research/auto', { method: 'PUT', body: { strategy: a, perDay: +(val('rs-aper') ?? 4), n: +val('rs-an') || 4, days: +val('rs-adays') || 250, focus: S.rsFocus || '' } })
      .then(r => { S.rs[a] = { ...(S.rs[a] || {}), auto: r.auto, autoToday: r.autoToday }; toast('Autopilot saved'); render(); setTimeout(() => loadRs(a), 2500); }).catch(err => toast(err.message)); return; }
  if (act === 'scheck') { api('/research/strategy/check', { method: 'POST', body: {} }).then(() => { toast('Re-checking the saved strategies (a few minutes)'); setTimeout(() => loadRs(a), 60000); }).catch(err => toast(err.message)); return; }
  if (act === 'ssave') {
    const name = prompt('Name of the strategy', ''); if (!name) return;
    api('/research/strategy', { method: 'POST', body: { strategy: a, name, cfg: rsCur(a) } }).then(() => { toast(`Saved “${name}”: first check running`); loadRs(a); setTimeout(() => loadRs(a), 90000); }).catch(err => toast(err.message)); return; }
  if (act === 'ssavei') {
    const id0 = a, x0 = (((S.rs[b] || {}).ideas) || []).find(i => i.id === id0); const name = prompt('Name of the strategy', x0 ? x0.name : ''); if (!name) return;
    api('/research/strategy', { method: 'POST', body: { strategy: b, name, ideaId: id0 } }).then(() => { toast(`Saved “${name}”: first check running`); loadRs(b); setTimeout(() => loadRs(b), 90000); }).catch(err => toast(err.message)); return; }
  if (act === 'sstage') {
    if (b === 'drop' && !confirm('Drop this strategy? (if it is in use, the kill switch takes it out)')) return;
    api('/research/strategy/stage', { method: 'PUT', body: { id: a, action: b } }).then(r => { const sx = (((S.rs.day || {}).strategies) || []).find(i => i.id === a); if (sx) sx.stage = r.stage; S.rs.day = { ...(S.rs.day || {}), basket: r.basket }; toast(b === 'restart' ? 'Paper period restarted from today' : 'Dropped'); render(); }).catch(err => toast(err.message)); return; }
  if (act === 'buse' || act === 'bside') {
    const body = act === 'buse' ? { use: a } : { side: a };
    api('/research/basket', { method: 'PUT', body }).then(r => { S.rs.day = { ...(S.rs.day || {}), basket: r.basket }; if (act === 'buse') { S.dt = undefined; toast('In the live day-trading settings now'); } render(); }).catch(err => toast(err.message)); return; }
  if (act === 'suse' || act === 'sdel') {
    const sx = (((S.rs[b] || {}).strategies) || []).find(i => i.id === a); if (!sx) return;
    if (act === 'sdel') { if (!confirm(`Delete “${sx.name}”?`)) return; api('/research/strategy?id=' + encodeURIComponent(a), { method: 'DELETE' }).then(() => loadRs(b)).catch(err => toast(err.message)); return; }
    if (b === 'day') { S.dtDraft = { ...(S.dtDraft || {}), ...sx.full }; S.dtDirty = true; toast('Loaded into the settings: click Apply to use them'); render(); }
    else { S.lab = { ...(labCfg() || {}), ...sx.full }; S.labDraft = {}; S.labSw = {}; toast('Loaded into the lab'); labRedraw(); }
    return; }
  if (act === 'mctx') {
    api('/research/marketctx', { method: 'POST', body: { days: +(b || S.rsDays || 250) } }).then(r => { S.rs[a] = { ...(S.rs[a] || {}), mctx: r.state }; toast(r.started ? 'Loading the market context in the background (a few runs of ~7 min)' : 'Already loading'); render(); }).catch(err => toast(err.message)); return; }
  if (act === 'sipprep') { api('/research/sipprep', { method: 'POST', body: { days: +a || 500 } }).then(r => { S.rs.day = { ...(S.rs.day || {}), sip: r.sip }; toast('Building the stocks-in-play history in the background (a few runs of ~8 minutes)'); render(); setTimeout(() => loadRs('day'), 30000); }).catch(err => toast(err.message)); return; }
  if (act === 'evorun' || act === 'evoreset') {
    const body = act === 'evorun' ? { run: true, days: +(val('evo-days') || 250), tickers: val('evo-tk') || '' } : { reset: true };
    api('/research/evolve', { method: 'PUT', body }).then(r => { S.rs.day = { ...(S.rs.day || {}), evolve: { ...r.evolve, running: act === 'evorun' ? 'now' : r.evolve.running } }; toast(act === 'evorun' ? 'Evolution running (about 8 minutes)' : 'New population on the next run'); render(); setTimeout(() => loadRs('day'), 30000); }).catch(err => toast(err.message)); return; }
  if (act === 'hold') { api('/research/holdout', { method: 'POST', body: { id: a } }).then(() => { toast('Holdout check running (about a minute); the result shows on the idea'); setTimeout(() => loadRs(b), 60000); }).catch(err => toast(err.message)); return; }
  if (act === 'blocks') { S.rsBlocksOpen = !S.rsBlocksOpen; render(); return; }
  if (act === 'crit') {
    const body = {}; document.querySelectorAll('[data-rscrit]').forEach(i => { const v = parseFloat(i.value); if (!isNaN(v)) body[i.dataset.rscrit] = v; });
    document.querySelectorAll('[data-rscritb]').forEach(i => { body[i.dataset.rscritb] = i.checked; });
    document.querySelectorAll('[data-rscrittxt]').forEach(i => { body[i.dataset.rscrittxt] = i.value; });
    document.querySelectorAll('[data-rscritsel]').forEach(i => { body[i.dataset.rscritsel] = i.value; });
    api('/research/criteria', { method: 'PUT', body }).then(r => { toast('Criteria saved: they apply to the next tests' + (r.robQueued ? ` · ${r.robQueued} ideas queued for the robustness check` : '')); loadRs(a); }).catch(err => toast(err.message)); return; }
  if (act === 'mine') {
    const cur = rsCur(a), saved = a === 'day' ? ((S.dt && S.dt.config) || {}) : ((S.ww && S.ww.config) || {}), skip = ['tickers', 'enabled', 'trade', 'main', 'lists', 'autoAdd', 'pvSetup'];
    const changes = Object.fromEntries(Object.entries(cur).filter(([k, v]) => !skip.includes(k) && v !== undefined && JSON.stringify(v) !== JSON.stringify(saved[k])));
    if (!Object.keys(changes).length) { toast(a === 'day' ? 'No change vs your saved settings' : 'The lab is the same as the live settings'); return; }
    const name = prompt('Name of this idea', 'my idea'); if (name === null) return;
    api('/research/idea', { method: 'POST', body: { strategy: a, name, changes, days: S.rsDays || 250 } })
      .then(r => { const x = r.idea || {}; if (x.status === 'queued') busy(a); else { toast(`Not tested: ${x.status}${(x.missing || []).length ? ' — ' + x.missing.join(' · ') : ''}`); loadRs(a); } }).catch(err => toast(err.message)); return; }
  const x = ((S.rs[b] || {}).ideas || []).find(i => i.id === a);
  if (act === 'use' && x) {
    const full = x.full || x.changes;
    if (b === 'day') { S.dtDraft = { ...(S.dtDraft || {}), ...full }; S.dtDirty = true; toast('Loaded into the settings: click Apply to use them'); render(); }
    else { S.lab = { ...(labCfg() || {}), ...full }; S.labDraft = {}; S.labSw = {}; S.labDirty = false; toast('Loaded into the lab (the live bot is not changed)'); labRedraw(); }
    return; }
  if (act === 'rob' && x) {
    S.dtDraft = { ...(S.dtDraft || {}), ...(x.full || x.changes) }; S.dtDirty = true; S.rsTab = 'tools';
    dtRobRun({ ...dtCfg(), ...(x.full || x.changes) }, x.name); toast('Robustness check started (Backtest & tools tab); the idea is loaded in the settings (not applied)'); render(); return; }
  if (act === 'del' && x) { if (!confirm(`Delete “${x.name}”?`)) return; api('/research/idea?id=' + encodeURIComponent(a), { method: 'DELETE' }).then(() => loadRs(b)).catch(err => toast(err.message)); }
}
function dtPanel() {
  const c = dtCfg(), num = (k, step, w, def) => `<input data-dtnum="${k}" type="number" step="${step}" value="${c[k] ?? def}" style="width:${w}em;padding:1px 4px">`;
  const lbl = Object.fromEntries(DT_RULES.map(r => [r[0], r[2]]));
  const rows = DT_IND.map(([k, name, layer, ps, tip, uses]) => { const used = uses.filter(u => c[u]);
    const eye = layer ? `<button class="btn ${S.fcLayers[layer] ? 'coachbtn' : ''}" data-fclayer="${layer}" style="font-size:.72rem;padding:0 7px">${S.fcLayers[layer] ? 'shown' : 'hidden'}</button>` : '<span class="muted" style="font-size:.72rem">—</span>';
    return `<tr style="${used.length ? '' : 'opacity:.6'}"><td style="padding:2px 6px">${eye}</td><td style="padding:2px 6px" title="${esc(tip)}"><b style="font-size:.82rem">${esc(name)}</b></td>
      <td style="white-space:nowrap;padding:2px 6px">${k === 'dark' ? `<span class="muted" style="font-size:.78rem">prints ≥</span> <input id="ww-dpmin" type="number" step="0.05" min="0.01" value="${S.wwDpPct ?? 0.25}" style="width:4.2em;padding:1px 4px"><span class="muted" style="font-size:.75rem">% of avg daily volume</span> &nbsp;<span class="muted" style="font-size:.78rem">lines</span> <input id="ww-dpn" type="number" step="1" min="0" max="10" value="${S.dpN ?? 5}" style="width:3.2em;padding:1px 4px"><span class="muted" style="font-size:.75rem">(0–10, last 180 days)</span>` : (ps.map(([pk, pl, st, w, suf, def]) => `<span class="muted" style="font-size:.78rem">${esc(pl)}</span> ${num(pk, st, w, def)}<span class="muted" style="font-size:.75rem">${esc(suf)}</span>`).join(' &nbsp;') || '<span class="muted" style="font-size:.75rem">no setting</span>') + (k === 'walls' ? ` &nbsp;<span class="muted" style="font-size:.78rem">expirations</span> <button class="btn ${c.wallNear ? '' : 'coachbtn'}" data-dtwexp="all" style="font-size:.72rem;padding:0 7px" title="Open interest of every expiration">all</button><button class="btn ${c.wallNear ? 'coachbtn' : ''}" data-dtwexp="near" style="font-size:.72rem;padding:0 7px" title="Only the nearest expiration (0DTE on SPY / QQQ); flip approximated from the strikes">nearest only</button>` : '')}</td>
      <td style="font-size:.78rem;padding:2px 6px">${used.length ? `<span class="gain">used by</span> ${used.map(u => esc(lbl[u] || u)).join(', ')}` : '<span class="muted">not used</span>'}</td></tr>`; }).join('');
  const chip = ([k, , l, tip, f]) => { const on = !!c[k];
    return `<span style="display:inline-flex;gap:3px;align-items:center;border:1px solid ${on ? 'var(--gain)' : 'var(--line)'};border-radius:14px;padding:1px 4px 1px 2px;${on ? '' : 'opacity:.55'}">
      <button class="btn" data-dtsw="${k}" title="${esc(tip)}" style="font-size:.75rem;padding:0 8px;border-radius:12px;${on ? 'background:var(--gain);color:#fff;border-color:transparent' : ''}">${on ? 'ON' : 'OFF'}</button><span style="font-size:.8rem" title="${esc(tip)}">${l}</span>${(!f ? [] : Array.isArray(f[0]) ? f : [f]).map(x => ` ${x[6] ? `<span class="muted" style="font-size:.72rem">${x[6]}</span> ` : ''}${x[5] === 'time' ? `<input data-dttime="${x[0]}" value="${esc(c[x[0]] || x[4])}" style="width:${x[2]}em;padding:1px 4px">` : x[5] === 'sel' ? `<select data-dtsel="${x[0]}" style="padding:0 2px;font-size:.78rem">${x[7].map(([v, l]) => `<option value="${v}" ${(c[x[0]] || x[4]) === v ? 'selected' : ''}>${l}</option>`).join('')}</select>` : num(x[0], x[1], x[2], x[4])}<span class="muted" style="font-size:.75rem">${x[3]}</span>`).join('')}</span>`; };
  const grp = g => `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center"><b style="font-size:.8rem;min-width:3.6em">${g}</b>${DT_RULES.filter(r => r[1] === g).map(chip).join('')}</div>`;
  const tog = (k, l) => `<button class="btn ${c[k] ? 'coachbtn' : ''}" data-dtsw="${k}" style="font-size:.78rem;padding:1px 10px">${l}</button>`;
  const tm = (k, l) => `<span class="muted" style="font-size:.78rem">${l}</span> <input data-dttime="${k}" value="${esc(c[k] || '')}" style="width:4.2em;padding:1px 4px">`;
  return `<div style="display:flex;flex-direction:column;gap:6px;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0">
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="font-size:.8rem;min-width:3.6em">Trades</b>${tog('longs', 'Longs')}${tog('shorts', 'Shorts')}
      ${tm('startT', 'entries from')} ${tm('endT', 'to')} ${tm('flatT', 'flat at')}
      <span class="muted" style="font-size:.78rem">max/day</span> ${num('maxTrades', 1, 3, 3)} <span class="muted" style="font-size:.78rem">shares</span> ${num('qty', 1, 4.5, 10)}
      <span class="muted" style="font-size:.78rem" title="If set, shares = this $ ÷ (entry − stop), capped by max shares">or risk $</span> ${num('riskUsd', 10, 4.5, 0)} <span class="muted" style="font-size:.78rem">max shares</span> ${num('maxQty', 1, 4.5, 500)}
      <span class="muted" style="font-size:.78rem">candles</span> <select data-dtsel="barMin" style="padding:1px 4px">${[1, 5].map(m => `<option value="${m}" ${+(c.barMin || 1) === m ? 'selected' : ''}>${m} min</option>`).join('')}</select>
      <span title="ON: the account trades today's 09:15 top-15 movers (the Today list on the side), updated every morning; the tickers box is then only used for the backtest">${tog('inplay', `${c.inplay ? 'ON' : 'OFF'} · trade today's top 15 movers`)}</span>
      <span class="muted" style="font-size:.78rem">${c.inplay ? 'backtest tickers' : 'tickers'}</span> <input data-dttk value="${esc((c.tickers || ['QQQ', 'SPY']).join(' '))}" style="width:8em;padding:1px 4px"></div>
    <details open style="margin:0"><summary style="font-size:.8rem;font-weight:700;cursor:pointer">Indicators</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.8rem;margin:4px 0"><tbody>${rows}</tbody></table></div></details>
    <div style="display:flex;flex-direction:column;gap:6px">${grp('Entry')}${grp('Filter')}${grp('Exit')}</div>
    ${fxRow([['fxLong', 'Long entry'], ['fxShort', 'Short entry'], ['fxExitLong', 'Long exit'], ['fxExitShort', 'Short exit']], c, 'dtfx')}
    <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center"><button class="btn ${S.dtDirty ? 'coachbtn' : ''}" data-dtapply style="font-size:.8rem;padding:1px 12px">${S.dtDirty ? 'Apply changes' : 'Apply'}</button>
      <span class="muted" style="font-size:.75rem">Every rule that is ON must pass on the 1-minute entry candle (shorts: the mirror). Nothing changes until you click Apply. Settings apply to the backtest and to the live day-trading account.</span></div></div>`;
}
function dtAccountBox(full) {
  const a = (S.dt && S.dt.account) || {}, c = dtCfg();
  if (!a.connected && !full) return '<p class="muted" style="font-size:.85rem;margin:6px 0">Day-trading account not connected: connect a second Alpaca paper account in <a href="#settings">Settings → Brokers</a> to trade this live.</p>';
  if (!a.connected) return `<div style="border:1px dashed var(--line);border-radius:8px;padding:8px 10px;margin:6px 0;font-size:.85rem"><b>Day-trading account</b> — connect a SECOND Alpaca paper account (its own API key / secret), kept apart from the options bot.
    <form id="dt-acct" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px"><input id="dt-key" placeholder="API key" style="width:14em" autocomplete="off"><input id="dt-secret" placeholder="Secret" type="password" style="width:18em" autocomplete="off"><button class="btn coachbtn" type="submit">Connect</button></form></div>`;
  const pos = a.positions || [];
  return `<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;font-size:.85rem;margin:4px 0">
    <b>Day account</b> <span class="muted">paper ··${esc(a.keyHint || '')}${a.equity ? ` · equity ${money(+a.equity)} · buying power ${money(+a.buyingPower)}` : ''}${a.error ? ` · <span class="loss">${esc(a.error)}</span>` : ''}</span>
    <label style="display:inline-flex;gap:4px;align-items:center"><input type="checkbox" data-dtlive="enabled" ${c.enabled ? 'checked' : ''}>run every minute</label>
    <label style="display:inline-flex;gap:4px;align-items:center" title="Place the orders on the day account (off: only follow the signals)"><input type="checkbox" data-dtlive="trade" ${c.trade ? 'checked' : ''}>place orders</label>
    <button class="btn" data-dtdisconnect style="font-size:.75rem;padding:0 8px">Disconnect</button>
    ${pos.length ? `<span>Positions: ${pos.map(p0 => `<b>${esc(p0.symbol)}</b> ${esc(p0.side)} ${esc(p0.qty)} @ ${(+p0.entry).toFixed(2)} <span class="${+p0.pl >= 0 ? 'gain' : 'loss'}">${+p0.pl >= 0 ? '+' : ''}${money(+p0.pl)}</span>`).join(' · ')}</span>` : '<span class="muted">no position</span>'}</div>`;
}
async function loadDTBt() {
  try { S.dtBt = await api('/daytrade/backtest'); } catch (e) { S.dtBt = { error: e.message }; }
  if (route() === 'botchart') render();
  clearTimeout(S.dtBtPoll);
  if (S.dtBt && S.dtBt.running && route() === 'botchart') S.dtBtPoll = setTimeout(loadDTBt, 10000);
}
function dtBtBox() {
  if (S.dtBt === undefined) { S.dtBt = null; setTimeout(loadDTBt, 0); }
  const t = S.dtBt || {};
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const head = `<div style="display:flex;gap:6px;align-items:center;margin-top:10px;flex-wrap:wrap"><b style="font-size:.95rem">Backtest</b> <select id="dt-btdays" style="padding:1px 4px">${[20, 60, 120, 250, 500].map(n => `<option ${n === (S.dtBtDays || 120) ? 'selected' : ''} value="${n}">${n} trading days</option>`).join('')}</select>
    <button class="btn coachbtn" data-dtbt ${t.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 12px">${t.running ? 'Running… (a few minutes)' : 'Run on all day tickers'}</button><span class="muted" style="font-size:.78rem">1-minute candles from Alpaca (full-market SIP history when your account allows it), the saved settings${S.dtDirty ? ' + pending changes' : ''}</span></div>
    ${dtOptCtl('bt')}`;
  if (t.error) return head + `<div class="errbox">${esc(t.error)}</div>`;
  if (!t.all) return head + ((t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : '');
  const a = t.all || {};
  const row = (l, x) => `<tr><td>${l}</td><td class="r">${x.n || 0}</td><td class="r">${x.win ?? '—'}%</td><td class="r">${R(x.avgR)}</td><td class="r">${R(x.totR)}</td><td class="r">${x.pf ?? '—'}</td><td class="r">${R(x.avgWin)} / ${R(x.avgLoss)}</td></tr>`;
  const tbl = (title, obj) => obj && Object.keys(obj).length ? `<details style="margin:4px 0"><summary style="font-size:.84rem;cursor:pointer">${title}</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.8rem"><thead><tr><th></th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg R</th><th class="r">Total R</th><th class="r">PF</th><th class="r">Avg win / loss</th></tr></thead><tbody>${Object.entries(obj).map(([k, x]) => row(esc(k), x)).join('')}</tbody></table></div></details>` : '';
  const cv = t.curve || [];
  let svg = '';
  if (cv.length > 1) { const W0 = 640, H0 = 120, mn = Math.min(0, ...cv), mx = Math.max(0, ...cv), sx = i => 4 + i / (cv.length - 1) * (W0 - 8), sy = v => H0 - 6 - (v - mn) / ((mx - mn) || 1) * (H0 - 12);
    svg = `<svg viewBox="0 0 ${W0} ${H0}" style="width:100%;max-width:${W0}px;height:auto;display:block"><line x1="4" x2="${W0 - 4}" y1="${sy(0)}" y2="${sy(0)}" stroke="var(--line)"/><path d="${cv.map((v, i) => `${i ? 'L' : 'M'}${sx(i).toFixed(1)},${sy(v).toFixed(1)}`).join('')}" fill="none" stroke="${cv[cv.length - 1] >= 0 ? 'var(--gain)' : 'var(--loss)'}" stroke-width="2"/><text x="6" y="12" font-size="11" fill="var(--muted)">equity curve (R, trade by trade)</text></svg>`; }
  const nm = Object.fromEntries(DT_RULES.map(r => [r[0], r[2]])), u = t.used;
  const usedLine = u ? `<p style="font-size:.8rem;margin:4px 0;padding:4px 8px;background:var(--sunk);border-radius:6px"><b>Settings used:</b> <b>${u.barMin || 1}-minute candles</b> · ${u.formulas ? `formulas: ${Object.entries(u.formulas).map(([k, v]) => `<code style="font-size:.74rem">${esc(k)}: ${esc(v)}</code>`).join(' · ')} · ` : ''}${esc(u.sides)} · entry: ${esc((u.entry || []).map(k => nm[k] || k).join(' + ') || '—')}${(u.filter || []).length ? ` · filter: ${esc(u.filter.map(k => nm[k] || k).join(' + '))}` : ''} · stop ${esc(u.stop)}${u.target ? ` · target ${esc(u.target)}` : ''}${(u.exit || []).length ? ` · exits: ${esc(u.exit.map(k => nm[k] || k).join(', '))}` : ''} · ${esc(u.time)} · walls: <b>${esc(u.walls)}</b></p>` : '';
  return head + usedLine + `<p class="muted" style="font-size:.8rem;margin:4px 0">Run ${esc((t.at || '').slice(5, 16).replace('T', ' '))} · ${t.daysFound ?? '—'} trading days from ${esc(t.first || '')} · data ${esc(Object.entries(t.feed || {}).map(([k, v]) => `${k} ${v.toUpperCase()}`).join(', '))} · ${t.secs ?? '—'} s</p>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.84rem"><thead><tr><th></th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg R</th><th class="r">Total R</th><th class="r">PF</th><th class="r">Avg win / loss</th></tr></thead><tbody>
    ${row('<b>All</b>', a)}${t.h1 ? row(`1st half <span class="muted">(to ${esc(t.mid || '')})</span>`, t.h1) : ''}${t.h2 ? row('2nd half', t.h2) : ''}${Object.entries(t.byTicker || {}).map(([k, x]) => row(esc(k), x)).join('')}${Object.entries(t.byDir || {}).map(([k, x]) => row(esc(k), x)).join('')}</tbody></table></div>
    <p style="font-size:.84rem;margin:4px 0">Max drawdown <b class="loss">${t.maxDD ?? '—'}R</b> · longest losing streak <b>${t.maxLosing ?? '—'}</b> · green days <b>${t.greenDays ?? 0}</b> / ${t.daysTraded ?? 0} traded (of ${t.daysTotal ?? 0}) · avg day ${R(t.avgDayR)} · daily Sharpe ${t.dailySharpe ?? '—'}</p>
    ${svg}${tbl('By month', t.byMonth)}${tbl('By weekday', t.byWeekday)}${tbl('By entry hour', t.byHour)}
    <p class="muted" style="font-size:.8rem;margin:4px 0">Exits: ${Object.entries(a.exits || {}).map(([k, v]) => `${esc(k)} ${v}`).join(' · ')}</p>
    ${t.opt ? dtOptLine(t.opt) : ''}
    ${(t.trades || []).length ? `<details style="margin:4px 0"><summary style="font-size:.84rem;cursor:pointer">Trades (last ${Math.min(300, t.trades.length)})</summary><div style="overflow-x:auto;max-height:340px;overflow-y:auto"><table class="pos" style="font-size:.78rem"><thead><tr><th>Ticker</th><th>Entry</th><th>Side</th><th class="r">Price</th><th>Exit</th><th class="r">Price</th><th>Why</th><th class="r">R</th>${t.opt ? '<th>Option</th><th class="r">In → out</th><th class="r">Option %</th>' : ''}</tr></thead><tbody>
      ${t.trades.slice().reverse().map(x => `<tr><td>${esc(x.sym || '')}</td><td>${esc((x.at || '').slice(5).replace('T', ' '))}</td><td>${esc(x.dir || '')}</td><td class="r">${x.entry != null ? (+x.entry).toFixed(2) : ''}</td><td>${esc((x.exitAt || '').slice(11))}</td><td class="r">${x.exit != null ? (+x.exit).toFixed(2) : ''}</td><td>${esc(x.status || '')}</td><td class="r">${R(x.R)}</td>${t.opt ? `<td>${esc(x.optSym || '—')}</td><td class="r">${x.optIn != null ? `${(+x.optIn).toFixed(2)} → ${(+x.optOut).toFixed(2)}` : ''}</td><td class="r">${x.optPct != null ? `<b class="${x.optPct > 0 ? 'gain' : 'loss'}">${x.optPct > 0 ? '+' : ''}${(+x.optPct).toFixed(1)}%</b>` : '—'}</td>` : ''}</tr>`).join('')}</tbody></table></div></details>` : ''}
    ${(t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : ''}`;
}
function dtOptCtl(k) {
  const o = S.dtOpt || {};
  return `<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:.8rem;margin:4px 0"><label style="display:inline-flex;gap:4px;align-items:center" title="Each trade bought as the nearest-expiration option (0DTE on SPY / QQQ): a call for a long, a put for a short. Alpaca option history starts Feb 2024."><input type="checkbox" data-dtopt="on" ${o.on ? 'checked' : ''}>also price on real 0DTE options</label>
    <span class="muted">strike</span> <input data-dtopt="money" type="number" step="0.1" value="${o.money ?? 0}" style="width:3.6em;padding:1px 4px"><span class="muted">% out of the money</span>
    <span class="muted">slippage</span> <input data-dtopt="slip" type="number" step="0.5" min="0" value="${o.slip ?? 1}" style="width:3.4em;padding:1px 4px"><span class="muted">% each side</span></div>`;
}
function dtOptGet() { const o = { ...(S.dtOpt || {}) }; document.querySelectorAll('[data-dtopt]').forEach(i => { o[i.dataset.dtopt] = i.type === 'checkbox' ? i.checked : parseFloat(i.value) || 0; }); S.dtOpt = o; return o.on ? o : null; }
function dtOptLine(o) {
  const P = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(1)}%</b>`;
  return o.n ? `<p style="font-size:.84rem;margin:4px 0"><b>On 0DTE options</b> ${o.n} trades priced${o.of ? ` of ${o.of}` : ''}${o.before2024 ? ` (${o.before2024} before Feb 2024: no option data)` : ''} · win <b>${o.win}%</b> · avg ${P(o.avg)} · median ${P(o.med)} · total ${P(o.tot)} · PF ${o.pf ?? '—'} · best ${P(o.best)} · worst ${P(o.worst)} <span class="muted">(strike ${o.money ?? 0}% OTM, slippage ${o.slip ?? 1}% each side)</span></p>`
    : `<p class="muted" style="font-size:.8rem">Options: ${esc(o.error || 'no trade could be priced')}${o.before2024 ? ` (${o.before2024} before Feb 2024)` : ''}</p>`;
}
function dtBody() {                              // the settings on screen: saved + pending changes + the inputs
  const body = { ...(S.dtDraft || {}) };
  document.querySelectorAll('[data-dtnum]').forEach(i => { const v = parseFloat(i.value); if (!isNaN(v)) body[i.dataset.dtnum] = v; });
  document.querySelectorAll('[data-dttime]').forEach(i => { body[i.dataset.dttime] = i.value; });
  document.querySelectorAll('[data-dtsel]').forEach(i => { body[i.dataset.dtsel] = i.value; });
  document.querySelectorAll('[data-dtfx]').forEach(i => { body[i.dataset.dtfx] = i.value.trim(); });
  const tk = document.querySelector('[data-dttk]'); if (tk) body.tickers = tk.value;
  return body;
}
const DT_NUMS = () => { const out = []; DT_IND.forEach(r => r[3].forEach(p => out.push([p[0], `${r[1]}: ${p[1] || p[0]}`]))); DT_RULES.forEach(r => { const f = r[4]; (!f ? [] : Array.isArray(f[0]) ? f : [f]).forEach(x => { if (x[5] !== 'time') out.push([x[0], `${r[2]}${x[6] ? ' ' + x[6] : ''}`]); }); }); out.push(['maxTrades', 'Max trades a day']); return out; };
async function loadDTRob() {
  try { S.dtRob = await api('/daytrade/robust'); } catch (e) { S.dtRob = { error: e.message }; }
  if (route() === 'botchart') render();
  clearTimeout(S.dtRobPoll);
  if (S.dtRob && S.dtRob.running && route() === 'botchart') S.dtRobPoll = setTimeout(loadDTRob, 10000);
}
function dtRobRun(cfg, label) {
  const days = +((document.getElementById('dt-robdays') || {}).value || S.dtRobDays || 250); S.dtRobDays = days;
  const cost = +(((S.rs || {}).day || {}).criteria || {}).costR || 0.05;
  api('/daytrade/robust', { method: 'POST', body: { days, cost, cfg } }).then(() => { S.dtRob = { ...(S.dtRob || {}), running: 'now', label }; render(); clearTimeout(S.dtRobPoll); S.dtRobPoll = setTimeout(loadDTRob, 10000); }).catch(err => toast(err.message));
}
function dtRobBox() {
  if (S.dtRob === undefined) { S.dtRob = null; setTimeout(loadDTRob, 0); }
  const t = S.dtRob || {};
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const head = `<div style="display:flex;gap:6px;align-items:center;margin-top:14px;flex-wrap:wrap"><b style="font-size:.95rem">Robustness check</b>
    <select id="dt-robdays" style="padding:1px 4px">${[120, 250, 500].map(n => `<option ${n === (S.dtRobDays || 250) ? 'selected' : ''} value="${n}">${n} trading days</option>`).join('')}</select>
    <button class="btn coachbtn" data-dtrob ${t.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 12px">${t.running ? 'Checking… (a few minutes)' : 'Check the current settings'}</button>
    <span class="muted" style="font-size:.78rem">every number nudged ±20% (formulas too), each rule taken out, longs / shorts only, shorter entry window, twice the cost, without the best 5% of trades: a real edge survives most of them</span></div>`;
  if (t.error) return head + `<div class="errbox">${esc(t.error)}</div>`;
  if (!t.rows) return head + ((t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : '');
  const V = { robust: ['gain', 'ROBUST: most nearby settings stay positive'], mixed: ['', 'MIXED: part of the result depends on the exact numbers'], fragile: ['loss', 'FRAGILE: the result depends on the exact numbers (likely curve-fit)'] }[t.verdict] || ['', ''];
  const row = (x, b) => `<tr${b ? ' style="font-weight:700"' : ''}><td>${esc(x.label)}</td><td class="r">${x.all.n || 0}</td><td class="r">${x.all.win ?? '—'}%</td><td class="r">${R(x.all.avgR)}</td><td class="r">${x.all.t ?? '—'}</td><td class="r">${x.all.pf ?? '—'}</td><td class="r">${x.h1 ? R(x.h1.avgR) + ' / ' + R(x.h2.avgR) : ''}</td></tr>`;
  return head + `<p style="font-size:.85rem;margin:6px 0"><b class="${V[0]}">${V[1]}</b> · ${t.positive} of ${t.variants} variants positive (${t.share ?? '—'}%) · median variant ${R(t.median)} vs ${R((t.base || {}).avgR)} as they are · cost ${t.cost}R a trade · ${t.daysFound} days · ${t.secs ?? '—'} s</p>
    <div style="overflow-x:auto;max-height:420px;overflow-y:auto"><table class="pos" style="font-size:.8rem"><thead><tr><th>Variant</th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg R</th><th class="r">t</th><th class="r">PF</th><th class="r">1st / 2nd half</th></tr></thead><tbody>
    ${row(t.rows[0], true)}${(t.extra || []).map(x => row(x)).join('')}${t.rows.slice(1).sort((a, b) => ((b.all || {}).avgR ?? -99) - ((a.all || {}).avgR ?? -99)).map(x => row(x)).join('')}</tbody></table></div>
    ${(t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : ''}`;
}
async function loadDTSrch() {
  try { S.dtSrch = await api('/daytrade/search'); } catch (e) { S.dtSrch = { error: e.message }; }
  if (route() === 'botchart') render();
  clearTimeout(S.dtSrchPoll);
  if (S.dtSrch && S.dtSrch.running && route() === 'botchart') S.dtSrchPoll = setTimeout(loadDTSrch, 10000);
}
function dtSearchBox() {
  if (S.dtSrch === undefined) { S.dtSrch = null; setTimeout(loadDTSrch, 0); }
  const t = S.dtSrch || {};
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const T = v => v == null ? '—' : `<b class="${v >= 2 ? 'gain' : v <= 0 ? 'loss' : ''}">${(+v).toFixed(1)}</b>`;
  const names = Object.fromEntries(DT_RULES.map(r => [r[0], r[2]]));
  const lab = l => esc(String(l || '').replace(/^(add|remove) (\w+)$/, (m, a, k) => `${a} “${names[k] || k}”`));
  const head = `<div style="display:flex;gap:6px;align-items:center;margin-top:14px;flex-wrap:wrap"><b style="font-size:.95rem">Find the best settings</b>
    <select id="dt-srdays" style="padding:1px 4px">${[120, 250, 500].map(n => `<option ${n === (S.dtSrDays || 250) ? 'selected' : ''} value="${n}">${n} trading days</option>`).join('')}</select>
    <button class="btn coachbtn" data-dtsearch ${t.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 12px">${t.running ? 'Searching… (up to 10 min)' : 'Search automatically'}</button></div>
    <p class="muted" style="font-size:.78rem;margin:2px 0 4px">Starts from the settings on screen and tests every single change (each rule on / off, each exit, stop and target sizes). It chooses using the <b>1st half</b> of the days only: the most total R, kept only when the gain is bigger than the noise and t doesn't drop (up to 3 changes; stop sizes are tried in 1-min, 5-min, daily ATR and % of price); the <b>2nd half</b> (never used to choose) shows if it holds. <b>t</b> = average R ÷ (spread ÷ √trades): under 1 = noise, 2 or more = probably real.</p>`;
  if (t.error) return head + `<div class="errbox">${esc(t.error)}</div>`;
  if (!t.final) return head + ((t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : '');
  const vd = { edge: ['gain', 'Edge found: it holds on the days the search did not see (t ≥ 2).'], weak: ['', 'Weak: positive on the unseen days but it could still be luck (t between 1 and 2). Paper trade it, run again with more days.'],
    none: ['loss', 'No edge found: on the days the search did not see, the best settings don\'t beat chance. Don\'t trade it live; try other rules or another entry idea.'] }[t.verdict] || ['', ''];
  const row = (l, x) => `<tr><td>${l}</td><td class="r">${x.train.n}</td><td class="r">${R(x.train.avgR)}</td><td class="r">${T(x.train.t)}</td><td class="r">${x.test.n}</td><td class="r">${R(x.test.avgR)}</td><td class="r">${T(x.test.t)}</td><td class="r">${R(x.all.totR)}</td><td class="r">${x.all.win ?? '—'}%</td><td class="r">${x.all.maxDD != null ? `<span class="loss">${x.all.maxDD}R</span>` : '—'}</td></tr>`;
  const th = `<thead><tr><th></th><th class="r">Trades (1st half)</th><th class="r">Avg R</th><th class="r">t</th><th class="r">Trades (2nd half, unseen)</th><th class="r">Avg R</th><th class="r">t</th><th class="r">Total R</th><th class="r">Win</th><th class="r">Max DD</th></tr></thead>`;
  const ch = Object.entries(t.changes || {});
  return head + `<div class="${vd[0] === 'loss' ? 'errbox' : ''}" style="font-size:.86rem;margin:4px 0;${vd[0] === 'gain' ? 'border:1px solid var(--gain);border-radius:6px;padding:6px 10px' : vd[0] === '' ? 'border:1px solid var(--line);border-radius:6px;padding:6px 10px' : ''}"><b>${vd[1]}</b></div>
    <p class="muted" style="font-size:.8rem;margin:4px 0">Run ${esc((t.at || '').slice(5, 16).replace('T', ' '))} · ${t.daysFound} trading days from ${esc(t.first || '')} · 1st half to ${esc(t.mid || '')} · ${t.tested} settings tested · ${t.secs} s</p>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.8rem">${th}<tbody>${row('Your settings', t.start)}${(t.path || []).map(p0 => row(`+ ${lab(p0.label)}`, p0)).join('')}</tbody></table></div>
    ${ch.length ? `<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:.84rem;margin:4px 0"><span>Changes found: <b>${(t.path || []).map(p0 => lab(p0.label)).join(' · ')}</b></span><button class="btn coachbtn" data-dtsrchuse style="font-size:.78rem;padding:1px 10px">Load these settings</button><span class="muted">(then Apply)</span></div>` : '<p style="font-size:.84rem;margin:4px 0">No single change clearly improved your settings on the 1st half.</p>'}
    <details style="margin:4px 0"><summary style="font-size:.84rem;cursor:pointer">Every change tested in the last round (most total R on the 1st half first)</summary><div style="overflow-x:auto;max-height:380px;overflow-y:auto"><table class="pos" style="font-size:.78rem">${th}<tbody>${(t.table || []).map(x => row(lab(x.label), x)).join('')}</tbody></table></div></details>
    ${(t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : ''}`;
}
async function loadFlipCheck() { try { S.flipChk = await api('/daytrade/flipcheck'); } catch (e) { S.flipChk = { error: e.message }; } if (route() === 'botchart') render(); }
function flipCheckBox() {
  if (S.flipChk === undefined) { S.flipChk = null; setTimeout(loadFlipCheck, 0); }
  const t = S.flipChk || {}, rows = t.rows || [];
  const f2 = v => v == null ? '—' : (+v).toFixed(2);
  const head = `<div style="display:flex;gap:6px;align-items:center;margin-top:14px;flex-wrap:wrap"><b style="font-size:.95rem">Morning flip check</b>
    <span class="muted" style="font-size:.78rem">the flip recorded every trading day at 09:20 / 09:32 / 09:40 ET (as Unusual Whales showed it then) vs the stored one the backtest uses. If “opened above / below the flip” agrees almost every day, the backtest's same-day flip is safe to trust.</span></div>`;
  if (t.error) return head + `<div class="errbox">${esc(t.error)}</div>`;
  if (!rows.length) return head + '<p class="muted" style="font-size:.82rem">No recording yet: the first one is taken on the next trading morning.</p>';
  return head + `<p style="font-size:.86rem;margin:4px 0">Same side of the flip (morning vs stored): <b class="${t.pct >= 90 ? 'gain' : t.pct != null && t.pct < 75 ? 'loss' : ''}">${t.pct ?? '—'}%</b> of ${t.n} day${t.n === 1 ? '' : 's'}</p>
    <div style="overflow-x:auto;max-height:320px;overflow-y:auto"><table class="pos" style="font-size:.78rem"><thead><tr><th>Day</th><th>Ticker</th><th class="r">Open</th><th class="r">Flip 09:20</th><th class="r">09:32</th><th class="r">09:40</th><th class="r">Stored (backtest)</th><th>Morning side</th><th>Stored side</th><th></th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${esc(r.date)}</td><td>${esc(r.sym)}</td><td class="r">${f2(r.open)}</td><td class="r">${f2(r.snaps['09:20'])}</td><td class="r">${f2(r.snaps['09:32'])}</td><td class="r">${f2(r.snaps['09:40'])}</td><td class="r">${f2(r.stored)}</td><td>${esc(r.sideMorning || '—')}</td><td>${esc(r.sideStored || '—')}</td><td>${r.agree == null ? '' : r.agree ? '<span class="gain">✓</span>' : '<b class="loss">✗</b>'}</td></tr>`).join('')}</tbody></table></div>`;
}
async function loadDTCmp() {
  try { S.dtCmp = await api('/daytrade/compare'); } catch (e) { S.dtCmp = { error: e.message }; }
  if (route() === 'botchart') render();
  clearTimeout(S.dtCmpPoll);
  if (S.dtCmp && S.dtCmp.running && route() === 'botchart') S.dtCmpPoll = setTimeout(loadDTCmp, 10000);
}
async function loadDTPre() { try { S.dtPre = (await api('/daytrade/presets')).presets || []; } catch (e) { S.dtPre = []; } if (route() === 'botchart') render(); }
const CMP_COL = ['#2f80ed', '#eb5757', '#27ae60', '#f2994a', '#9b51e0', '#00a3a3', '#c2185b', '#7f8c8d', '#b8860b', '#3d5afe'];
function dtCmpBox() {
  if (S.dtCmp === undefined) { S.dtCmp = null; setTimeout(loadDTCmp, 0); }
  if (S.dtPre === undefined) { S.dtPre = null; setTimeout(loadDTPre, 0); }
  const t = S.dtCmp || {}, pre = S.dtPre || [], sel = S.dtCmpSel || {}, cur = S.dtCmpCur !== false;
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const P = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(1)}%</b>`;
  const nums = DT_NUMS();
  const head = `<div style="display:flex;gap:6px;align-items:center;margin-top:14px;flex-wrap:wrap"><b style="font-size:.95rem">Compare settings</b>
    <span class="muted" style="font-size:.78rem">several settings on the same days and candles, side by side</span></div>
    <div style="display:flex;flex-direction:column;gap:6px;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0;font-size:.82rem">
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap"><b style="min-width:5.5em">Saved</b>
        <label style="display:inline-flex;gap:4px;align-items:center"><input type="checkbox" data-dtcmpcur ${cur ? 'checked' : ''}>Current settings${S.dtDirty ? ' (with pending changes)' : ''}</label>
        ${pre === null ? '<span class="muted">loading…</span>' : pre.map(p0 => `<span style="display:inline-flex;gap:3px;align-items:center;border:1px solid var(--line);border-radius:14px;padding:1px 6px"><input type="checkbox" data-dtpresel="${esc(p0.name)}" ${sel[p0.name] ? 'checked' : ''}><b>${esc(p0.name)}</b>
          <button class="btn" data-dtpreload="${esc(p0.name)}" title="Load these settings into the panel (then Apply)" style="font-size:.7rem;padding:0 6px">load</button><button class="btn" data-dtpredel="${esc(p0.name)}" title="Delete" style="font-size:.7rem;padding:0 6px">✕</button></span>`).join('')}
        <input id="dt-prename" placeholder="name" style="width:9em;padding:1px 4px"><button class="btn" data-dtpresave style="font-size:.78rem;padding:1px 10px">Save current as</button></div>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap"><b style="min-width:5.5em">Sweep</b><span class="muted">current settings with</span>
        <select id="dt-swkey" style="padding:1px 4px;max-width:18em"><option value="">— no sweep —</option>${nums.map(([k, l]) => `<option value="${k}" ${S.dtSwKey === k ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>
        <span class="muted">=</span><input id="dt-swvals" value="${esc(S.dtSwVals || '0.5 1 1.5 2')}" placeholder="0.5 1 1.5 2" style="width:10em;padding:1px 4px"><span class="muted">(each value = one setting)</span></div>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap"><b style="min-width:5.5em">Run</b><select id="dt-cmpdays" style="padding:1px 4px">${[20, 60, 120, 250, 500].map(n => `<option ${n === (S.dtCmpDays || 120) ? 'selected' : ''} value="${n}">${n} trading days</option>`).join('')}</select>
        <button class="btn coachbtn" data-dtcmp ${t.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 12px">${t.running ? 'Running… (a few minutes)' : 'Compare'}</button><span class="muted">up to 10 settings</span></div>
      ${dtOptCtl('cmp')}</div>`;
  if (t.error) return head + `<div class="errbox">${esc(t.error)}</div>`;
  const vs = t.variants || [];
  if (!vs.length) return head + ((t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : '');
  const best = Math.max(...vs.map(v => (v.all || {}).totR ?? -1e9));
  const anyOpt = vs.some(v => v.opt);
  const rows = vs.map((v, i) => { const a = v.all || {}, ch = Object.entries(v.changes || {});
    return `<tr><td><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${CMP_COL[i % CMP_COL.length]}"></span> <b>${esc(v.name)}</b>${!ch.length ? '<div class="muted" style="font-size:.7rem">same as the saved settings</div>' : ''}${ch.length ? `<div class="muted" style="font-size:.7rem;max-width:22em">${esc(ch.slice(0, 8).map(([k, x]) => `${k} ${typeof x === 'boolean' ? (x ? 'on' : 'off') : x}`).join(' · '))}${ch.length > 8 ? ' …' : ''}</div>` : ''}</td>
      <td class="r">${a.n || 0}</td><td class="r">${a.win ?? '—'}%</td><td class="r">${R(a.avgR)}</td><td class="r" title="average R ÷ (spread ÷ √trades): under 1 = noise, 2+ = probably real"><b class="${v.t >= 2 ? 'gain' : v.t <= 0 ? 'loss' : ''}">${v.t ?? '—'}</b></td><td class="r" style="${a.totR === best ? 'outline:2px solid var(--gain);outline-offset:-2px' : ''}">${R(a.totR)}</td><td class="r">${a.pf ?? '—'}</td>
      <td class="r"><b class="loss">${v.maxDD ?? '—'}R</b></td><td class="r">${v.maxLosing ?? '—'}</td><td class="r">${v.greenDays ?? 0} / ${v.daysTraded ?? 0}</td><td class="r">${v.dailySharpe ?? '—'}</td>
      <td class="r">${R((v.h1 || {}).avgR)} / ${R((v.h2 || {}).avgR)}</td>${anyOpt ? `<td class="r">${v.opt && v.opt.n ? `${P(v.opt.avg)} · ${v.opt.win}% · ${v.opt.n}` : '—'}</td>` : ''}</tr>`; }).join('');
  const cvs = vs.map(v => v.curve || []), all = [].concat(...cvs);
  let svg = '';
  if (all.length > 1) { const W0 = 640, H0 = 150, mn = Math.min(0, ...all), mx = Math.max(0, ...all), sy = v => H0 - 6 - (v - mn) / ((mx - mn) || 1) * (H0 - 12);
    svg = `<svg viewBox="0 0 ${W0} ${H0}" style="width:100%;max-width:${W0}px;height:auto;display:block"><line x1="4" x2="${W0 - 4}" y1="${sy(0)}" y2="${sy(0)}" stroke="var(--line)"/>${cvs.map((cv, i) => cv.length > 1 ? `<path d="${cv.map((v, j) => `${j ? 'L' : 'M'}${(4 + j / (cv.length - 1) * (W0 - 8)).toFixed(1)},${sy(v).toFixed(1)}`).join('')}" fill="none" stroke="${CMP_COL[i % CMP_COL.length]}" stroke-width="1.8"/>` : '').join('')}<text x="6" y="12" font-size="11" fill="var(--muted)">equity curves (R, trade by trade)</text></svg>`; }
  return head + `<p class="muted" style="font-size:.8rem;margin:4px 0">Run ${esc((t.at || '').slice(5, 16).replace('T', ' '))} · ${t.daysFound ?? '—'} trading days from ${esc(t.first || '')} · halves split at ${esc(t.mid || '')} · ${t.secs ?? '—'} s</p>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.8rem"><thead><tr><th>Setting</th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg R</th><th class="r">t</th><th class="r">Total R</th><th class="r">PF</th><th class="r">Max DD</th><th class="r">Losing streak</th><th class="r">Green days</th><th class="r">Daily Sharpe</th><th class="r">Avg R 1st / 2nd half</th>${anyOpt ? '<th class="r">0DTE avg · win · n</th>' : ''}</tr></thead><tbody>${rows}</tbody></table></div>
    ${svg}${(t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : ''}`;
}
async function loadDTStudy() {
  try { S.dtSt = await api('/daytrade/study'); } catch (e) { S.dtSt = { error: e.message }; }
  if (route() === 'botchart') render();
  clearTimeout(S.dtStPoll);
  if (S.dtSt && S.dtSt.running && route() === 'botchart') S.dtStPoll = setTimeout(loadDTStudy, 10000);
}
function dtStudyBox() {
  if (S.dtSt === undefined) { S.dtSt = null; setTimeout(loadDTStudy, 0); }
  const t = S.dtSt || {};
  const P = v => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}%</span>`;
  const head = `<div style="display:flex;gap:6px;align-items:center;margin-top:14px;flex-wrap:wrap"><b style="font-size:.95rem">Scenario study</b>
    <select id="dt-stdays" style="padding:1px 4px">${[60, 120, 250, 500].map(n => `<option ${n === (S.dtStDays || 250) ? 'selected' : ''} value="${n}">${n} trading days</option>`).join('')}</select>
    <button class="btn coachbtn" data-dtstudy ${t.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 12px">${t.running ? 'Running… (a few minutes)' : 'Run the study'}</button>
    <span class="muted" style="font-size:.78rem">gaps, retests of the previous high / low / close, pre-market, opening range, gamma walls and dark pool levels: what price did next (1-minute candles)</span></div>`;
  if (t.error) return head + `<div class="errbox">${esc(t.error)}</div>`;
  if (!t.gaps) return head + ((t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : '');
  const th = cols => `<thead><tr>${cols.map((c, i) => `<th${i ? ' class="r"' : ''}>${c}</th>`).join('')}</tr></thead>`;
  const gaps = `<div style="overflow-x:auto"><table class="pos" style="font-size:.8rem">${th(['Open vs previous close', 'Days', 'Gap filled', 'Median minutes to fill', 'Green close', 'Open → close', 'Max up from open', 'Max down from open', 'Close > prev high', 'Close < prev low'])}<tbody>
    ${Object.entries(t.gaps).map(([k, x]) => `<tr><td>${esc(k)}</td><td class="r">${x.n}</td><td class="r">${x.filled}%</td><td class="r">${x.fillMedMin ?? '—'}</td><td class="r">${x.greenClose}%</td><td class="r">${P(x.oc)}</td><td class="r">${P(x.upOpen)}</td><td class="r">${P(x.dnOpen)}</td><td class="r">${x.abovePH}%</td><td class="r">${x.belowPL}%</td></tr>`).join('')}</tbody></table></div>`;
  const g = S.dtStGap || 'all', src = g === 'all' ? t.levels : (t.byGap || {})[g] || {};
  const lvRow = (k, x) => `<tr><td>${esc(k)}</td><td class="r">${x.n}</td><td class="r">${x.n ? x.held + '%' : '—'}</td><td class="r">${P(x.f5)}</td><td class="r">${P(x.f15)}</td><td class="r">${P(x.f30)}</td><td class="r">${P(x.f60)}</td><td class="r">${P(x.fc)}</td><td class="r">${x.p15 ?? '—'}% / ${x.p60 ?? '—'}%</td><td class="r">${P(x.mfe30)} / ${P(x.mae30)}</td><td class="r">${x.minute ?? '—'}</td></tr>`;
  const levels = `<div style="display:flex;gap:6px;align-items:center;margin:10px 0 4px"><b style="font-size:.88rem">First touch of a level</b> <select id="dt-stgap" style="padding:1px 4px"><option value="all">all days</option>${Object.keys(t.byGap || {}).map(k => `<option ${k === g ? 'selected' : ''}>${esc(k)}</option>`).join('')}</select>
    <span class="muted" style="font-size:.75rem">moves in % from the level, + = through it (a test from below: higher) · held = broke it and stayed past it 15-30 min later</span></div>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.8rem">${th(['Level · approach', 'Touches', 'Broke & held', '+5 min', '+15 min', '+30 min', '+60 min', 'Close', 'Through at 15 / 60 min', 'Best / worst 30 min', 'Avg minute'])}<tbody>
    ${Object.entries(src).map(([k, x]) => lvRow(k, x)).join('')}</tbody></table></div>`;
  const cf = Object.entries(t.confluence || {});
  const conf = cf.length ? `<b style="font-size:.88rem;display:block;margin:10px 0 4px">Dark pool × gamma levels <span class="muted" style="font-weight:400;font-size:.75rem">a dark pool level within 0.15% of a gamma level (and the reverse)</span></b>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.8rem">${th(['Level · approach', 'With: touches', 'held', '+30 min', 'close', 'Without: touches', 'held', '+30 min', 'close'])}<tbody>
    ${cf.map(([k, x]) => `<tr><td>${esc(k)}</td><td class="r">${x.with.n}</td><td class="r">${x.with.n ? x.with.held + '%' : '—'}</td><td class="r">${P(x.with.f30)}</td><td class="r">${P(x.with.fc)}</td><td class="r">${x.without.n}</td><td class="r">${x.without.n ? x.without.held + '%' : '—'}</td><td class="r">${P(x.without.f30)}</td><td class="r">${P(x.without.fc)}</td></tr>`).join('')}</tbody></table></div>` : '';
  const fg = Object.entries(t.flipGap || {});
  const flip = fg.length ? `<b style="font-size:.88rem;display:block;margin:10px 0 4px">Gamma flip × gap</b><div style="overflow-x:auto"><table class="pos" style="font-size:.8rem">${th(['Open', 'Days', 'Green close', 'Open → close'])}<tbody>
    ${fg.map(([k, x]) => `<tr><td>${esc(k)}</td><td class="r">${x.n}</td><td class="r">${x.greenClose}%</td><td class="r">${P(x.oc)}</td></tr>`).join('')}</tbody></table></div>` : '';
  return head + `<p class="muted" style="font-size:.8rem;margin:4px 0">Run ${esc((t.at || '').slice(5, 16).replace('T', ' '))} · ${esc((t.tickers || []).join(', '))} · ${t.nDays} days · ${t.nEvents} level touches · gamma walls (${t.wallNear ? 'nearest expiration' : 'all expirations'}) on ${t.wallDays} days, dark pool levels on ${t.dpDays} days · data ${esc(Object.entries(t.feed || {}).map(([k, v]) => `${k} ${v.toUpperCase()}`).join(', '))}</p>
    <b style="font-size:.88rem;display:block;margin:6px 0 4px">Gaps</b>${gaps}${levels}${conf}${flip}${dtStudyCvd(t, P)}
    ${(t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : ''}`;
}
function dtStudyCvd(t, P) {
  const th = cols => `<thead><tr>${cols.map((c, i) => `<th${i ? ' class="r"' : ''}>${c}</th>`).join('')}</tr></thead>`;
  const ct = Object.entries(t.cvdTime || {}), cl = Object.entries(t.cvdLevels || {});
  if (!ct.length && !cl.length) return '<p class="muted" style="font-size:.8rem;margin:10px 0 0">CVD: run the study again to get the CVD tables.</p>';
  const src = `<span class="muted" style="font-weight:400;font-size:.75rem">real order flow on ${t.cvdRealDays ?? 0} days (loaded on the chart), estimated from the candles on the others</span>`;
  const time = ct.length ? `<b style="font-size:.88rem;display:block;margin:10px 0 4px">CVD at a time → rest of the day ${src}</b>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.8rem">${th(['Time · CVD · day', 'Days', 'Close moved the CVD way', 'Avg move to the close'])}<tbody>
    ${ct.map(([k, x]) => `<tr><td>${esc(k)}</td><td class="r">${x.n}</td><td class="r">${x.same}%</td><td class="r">${P(x.move)}</td></tr>`).join('')}</tbody></table></div>`: '';
  const lv = cl.length ? `<b style="font-size:.88rem;display:block;margin:10px 0 4px">Level touches × CVD <span class="muted" style="font-weight:400;font-size:.75rem">with = CVD on the side of the approach (> 0 on a test from below, < 0 from above) just before the touch</span></b>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.8rem">${th(['Level · approach', 'With: touches', 'held', '+30 min', 'close', 'Against: touches', 'held', '+30 min', 'close'])}<tbody>
    ${cl.map(([k, x]) => `<tr><td>${esc(k)}</td><td class="r">${x.with.n}</td><td class="r">${x.with.n ? x.with.held + '%' : '—'}</td><td class="r">${P(x.with.f30)}</td><td class="r">${P(x.with.fc)}</td><td class="r">${x.against.n}</td><td class="r">${x.against.n ? x.against.held + '%' : '—'}</td><td class="r">${P(x.against.f30)}</td><td class="r">${P(x.against.fc)}</td></tr>`).join('')}</tbody></table></div>` : '';
  return time + lv;
}
function vChartDT() {
  if (S.dt === undefined) { S.dt = null; setTimeout(loadDT, 0); }
  const cfg = dtCfg(), tickers = cfg.tickers || ['QQQ', 'SPY'], t0 = (hashParam('t') || '').toUpperCase();
  const sym = t0 || tickers[0];                        // any ticker: the day's movers too (side list)
  if (S.dtMovers === undefined) { S.dtMovers = null; setTimeout(loadMovers, 0); }
  const k = `dt|${sym}|${dtTfEff()}|${S.dtDays}|${(S.dtView || {}).id || ''}`, d = S.dtFc && S.dtFc.k === k ? S.dtFc.d : null;
  if (!d && S.dtBusy !== k) setTimeout(() => loadDTChart(sym), 0);
  const narrow = window.innerWidth < 800, headH = S.fcfHeadH || 58;
  const side = !narrow, W = Math.max(320, window.innerWidth - 20 - (side ? 232 : 0)), H = Math.max(420, window.innerHeight - headH - 90);   // settings / backtest below: scroll
  const id = 'dt-' + sym + '-' + S.dtTf;
  fcNeedDark(id);
  const sigs = (d && d.signals) || [], st = (d && d.stats) || {};
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const zb = (q, l) => `<button class="btn" data-fczoom="${id}|${q}" style="padding:3px 10px">${l}</button>`;
  const head = `<div class="fcf-head"><b style="font-size:1.25rem">${esc(sym)}</b> ${S.dtView ? `<span style="font-size:.8rem;border:1px solid var(--coach);border-radius:6px;padding:2px 8px">signals of the saved strategy <b>${esc(S.dtView.name)}</b> <button class="btn" data-stview="off" style="font-size:.72rem;padding:0 6px">back to my settings</button></span>` : ''} ${fcModeSeg()}
    <span class="seg">${(tickers.includes(sym) ? tickers : tickers.concat([sym])).map(x => `<a class="btn ${x === sym ? 'coachbtn' : ''}" href="#botchart?t=${encodeURIComponent(x)}" style="padding:3px 10px">${esc(x)}</a>`).join('')}</span>
    <span class="seg">${[1, 5].map(m => `<button class="btn ${S.dtTf === m ? 'coachbtn' : ''}" data-dttf="${m}" style="padding:3px 10px">${m}m</button>`).join('')}</span>
    <span class="seg">${[1, 3, 5, 10, 20, 30].map(n => `<button class="btn ${S.dtDays === n ? 'coachbtn' : ''}" data-dtdays="${n}" style="padding:3px 10px">${n}d</button>`).join('')}</span>
    ${zb('in', '＋')}${zb('out', '−')}${zb('left', '◀')}${zb('right', '▶')}${zb('reset', 'Reset')}
    <span class="seg">${[['sig', 'Signals'], ['zones', 'S/R zones'], ['bands', 'EMA bands'], ['lis', 'LIS'], ['pmz', 'PM zone'], ['fibs', 'Fibs'], ['turn', 'Squeeze turns'], ['vwap', 'VWAP'], ['ema', 'EMA'], ['kch', 'Keltner'], ['inertia', 'Inertia'], ['sqz', 'Squeeze'], ['walls', 'Gamma walls'], ['dark', 'Dark pool'], ['pres', 'CVD'], ['cvd', 'Order flow']].map(([l, n]) => `<button class="btn ${S.fcLayers[l] !== false && S.fcLayers[l] ? 'coachbtn' : ''}" data-fclayer="${l}" style="font-size:.8rem;padding:2px 9px">${n}</button>`).join('')}</span>
    <button class="btn" data-dtreload style="padding:3px 10px" title="Reload the candles">↻</button>
    <button class="btn" data-hidesettings>${S.hideSettings ? 'Show settings' : 'Hide settings'}</button><button class="btn ${S.hideAnalysis ? 'coachbtn' : ''}" data-hideanalysis title="Hide / show the backtest, search, compare and study sections under the chart">${S.hideAnalysis ? 'Show analysis' : 'Hide analysis'}</button>
    <span style="flex:1"></span><a class="btn" href="#botevals" aria-label="Close full screen" title="Close">✕</a></div>`;
  let main;
  if (!d) main = '<p class="loading">Loading 1-minute candles…</p>';
  else if (d.error) main = `<div class="errbox">${esc(d.error)}</div>`;
  else {
    const fl = S.flow[sym] || {}, ck = [JSON.stringify(S.fcLayers), JSON.stringify(S.fcZoom[fcKey(id)] || null), W, H, fl.k, (fl.missing || []).length, Object.keys(fl.days || {}).length, S.dpN, (() => { const x = S.fcDp[fcKey(id)]; return x ? `${(x.levels || []).length}/${(x.prints || []).length}/${x.error || ''}` : '-'; })()].join('|');
    const cx = ctxOf(sym);
    main = svgCached('dt', d, ck + '|' + (cx && cx.to ? cx.to + (cx.zones || []).length : '-'), () => fcSvg(withFlow(sym, { ...d, ema: (S.fcLayers.ema ? d.ema : []), zones: cx && !cx.error ? cx.zones : null }), id, { W, H: Math.max(380, H - 40), noAcc: true, padRight: 100 }));
    main += ctxBox(sym);
    if (S.dtTf === 1 && S.dtDays >= 20) main += '<p class="muted" style="font-size:.78rem;margin:2px 4px">20-30 days: shown with 5-minute candles (the signals still come from the 1-minute engine). Pick 10 days or less for 1-minute candles.</p>';
    main += dtWhyBox();
    const f2 = v => v == null ? '—' : (+v).toFixed(2), lv = Object.entries(d.pm || {}).map(([dy, x]) => `${dy.slice(5)} pre-market ${f2(x.h)} / ${f2(x.l)}`).concat(Object.entries(d.orbLv || {}).map(([dy, x]) => `${dy.slice(5)} opening range ${f2(x.h)} / ${f2(x.l)}`));
    main += `<div class="fcf-info" style="display:flex;flex-wrap:wrap;gap:6px 14px;font-size:.84rem">
      <span><b>This view</b> ${sigs.length} trade${sigs.length === 1 ? '' : 's'} · win ${st.win ?? 0}% · avg ${R(st.avgR)} · total ${R(st.totR)} · PF ${st.pf ?? '—'} · ${Object.entries(st.exits || {}).map(([a, b]) => `${esc(a)} ${b}`).join(' · ')}</span>
      <span class="muted">${esc(lv.slice(-4).join(' · '))}</span>
      <span>${sigs.slice(-40).map(x => `<span class="exchip" style="font-size:.72rem">${esc(x.at.slice(5, 16).replace('T', ' '))} ${x.side < 0 ? 'S' : 'L'} ${x.status === 'open' ? 'open' : R(x.R)}</span>`).join(' ')}</span></div>`;
  }
  const live = (S.dt && S.dt.trades) || [];
  const info = `${dtAccountBox()}
    ${live.length ? `<details style="margin:4px 0"><summary style="font-size:.85rem;cursor:pointer">Live day trades (${live.length})</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.8rem"><thead><tr><th>Ticker</th><th>Entry</th><th>Side</th><th class="r">Qty</th><th class="r">Price</th><th>Status</th><th>Exit</th></tr></thead><tbody>
      ${live.slice().reverse().map(r0 => `<tr><td>${esc(r0.symbol)}</td><td>${esc((r0.at || '').replace('T', ' ').slice(5))}</td><td>${esc(r0.dir || '')}</td><td class="r">${r0.qty ?? ''}</td><td class="r">${r0.entry ?? ''}</td><td>${esc(r0.status || '')}</td><td>${esc(r0.exitReason || '')}</td></tr>`).join('')}</tbody></table></div></details>` : ''}
    ${S.hideSettings ? '' : dtPanel()}${S.hideAnalysis ? '' : rsTabs('day') + (S.rsTab === 'ai' ? researchBox('day') : dtBtBox() + dtRobBox() + flipCheckBox() + dtSearchBox() + dtCmpBox() + dtStudyBox())}`;
  const panel = moversBox(sym);
  return `<div class="fcf">${head}<div class="fcf-body"><div class="fcf-main">${side ? `<div style="display:flex;gap:8px;align-items:flex-start"><div style="flex:1;min-width:0">${main}</div>${panel}</div>` : panel + main}${info}</div></div></div>`;
}
function ctxOf(sym) {
  S.ctx = S.ctx || {};
  const x = S.ctx[sym];
  if (!x || (x.at && Date.now() - x.at > 600000 && !x.busy)) {
    S.ctx[sym] = { ...(x || {}), busy: true, at: Date.now() };
    api('/daytrade/context', { method: 'POST', body: { t: sym } }).then(r => { S.ctx[sym] = { ...r, at: Date.now() }; })
      .catch(e => { S.ctx[sym] = { error: e.message, at: Date.now() }; }).finally(() => { if (route() === 'botchart' && S.fcfMode === 'dt') render(); });
  }
  return S.ctx[sym] && !S.ctx[sym].busy ? S.ctx[sym] : (x && !x.busy ? x : null);
}
function ctxBox(sym) {
  const x = ctxOf(sym);
  if (!x) return '<p class="muted" style="font-size:.8rem;margin:4px">Loading the 30-day context…</p>';
  if (x.error) return `<p class="muted" style="font-size:.8rem;margin:4px">30-day context: ${esc(x.error)}</p>`;
  const f2 = v => v == null ? '—' : (+v).toFixed(2), tr = v => v > 0 ? '<span class="gain">up</span>' : v < 0 ? '<span class="loss">down</span>' : '<span class="muted">unclear</span>';
  const w = x.walls || {}, z = x.zones || [];
  return `<div class="fcf-info" style="font-size:.82rem;border:1px solid var(--line);border-radius:8px;padding:6px 8px;margin:4px 0">
    <div><b>30-day context</b> <span class="muted">${esc(x.from)} → ${esc(x.to)}</span> · price <b>${f2(x.price)}</b> · range ${f2(x.lo)}–${f2(x.hi)} (at <b>${x.pos}%</b> of it) · ATR ${f2(x.atr)} · trend 1h ${tr(x.trendH)}, daily ${tr(x.trendD)}
      ${x.poc != null ? ` · volume POC ${f2(x.poc)}, value area ${f2(x.vaLo)}–${f2(x.vaHi)}` : ''}${w.putWall || w.callWall ? ` · walls ${w.date ? esc(w.date.slice(5)) : ''}: put ${f2(w.putWall)} · call ${f2(w.callWall)} · flip ${f2(w.flip)}` : ''}</div>
    ${(x.notes || []).length ? `<div style="margin-top:2px"><b>Now:</b> ${x.notes.map(esc).join(' · ')}</div>` : ''}
    <div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">${z.slice().sort((a, b) => b.lo - a.lo).map(q => `<span class="exchip" style="font-size:.74rem;border-color:${q.kind === 'support' ? 'var(--gain)' : q.kind === 'resistance' ? 'var(--loss)' : 'var(--coach,#5b5bd6)'}" title="${q.n} separate swing(s) turned here (a daily swing counts 2)${q.last ? ` · last ${esc(String(q.last).slice(0, 10))}` : ''}">${q.kind === 'at price' ? 'at price' : esc(q.kind)} ${f2(q.lo)}–${f2(q.hi)} · strength ${q.touches} · ${q.dist > 0 ? '+' : ''}${q.dist} ATR</span>`).join('')}</div></div>`;
}
async function loadMovers() {
  try { S.dtMovers = (await api('/daytrade/movers')).days || []; } catch (e) { S.dtMovers = [{ label: 'Today', error: e.message, list: [] }]; }
  if (route() === 'botchart' && S.fcfMode === 'dt') render();
  clearTimeout(S.mvPoll);                              // the 09:15 list appears by itself (checked every 2 minutes 09:10-09:40 ET, else every 10)
  const hm = new Date().toLocaleTimeString('en-GB', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit' });
  const pending = (S.dtMovers || []).some(x => x.note);
  S.mvPoll = setTimeout(() => { if (route() === 'botchart' && S.fcfMode === 'dt') loadMovers(); else S.dtMovers = undefined; }, pending || (hm >= '09:10' && hm < '09:40') ? 60000 : 600000);
}
function moversBox(sym) {
  const ds = S.dtMovers;
  const f1 = v => v == null ? '' : `${(+v).toFixed(1)}×`;
  if (!ds) return '<div style="width:224px;flex:none;font-size:.8rem" class="muted">Loading the movers…</div>';
  const k = Math.min(S.mvDay || 0, ds.length - 1), d = ds[k] || {};
  const tabs = ds.map((x, i) => `<button class="btn ${i === k ? 'coachbtn' : ''}" data-mvday="${i}" style="font-size:.72rem;padding:0 6px">${esc(x.label || x.date)}</button>`).join('');
  const rows = (d.list || []).map((x, i) => `<a href="#botchart?t=${encodeURIComponent(x.sym)}" style="display:flex;justify-content:space-between;gap:6px;padding:2px 6px;border-radius:4px;text-decoration:none;${x.sym === sym ? 'background:var(--sunk);font-weight:700' : ''}">
      <span>${i + 1}. ${esc(x.sym)}${(d.watch || []).includes(x.sym) ? ' <span class="gain" title="on the 09:15 watchlist (clear hourly trend)">★</span>' : ''}</span>
      <span class="muted" style="font-size:.72rem">${x.pmRvol != null && d.source === 'pre-market 09:15' ? f1(x.pmRvol) : f1(x.rvol)}${x.gapPct != null ? ` · ${x.gapPct > 0 ? '+' : ''}${(+x.gapPct).toFixed(1)}%` : ''}</span></a>`).join('');
  return `<div style="width:224px;flex:none;border:1px solid var(--line);border-radius:8px;padding:6px;font-size:.8rem;max-height:${Math.max(380, window.innerHeight - 160)}px;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px"><b>Top movers</b><button class="btn" data-mvreload title="Reload" style="font-size:.72rem;padding:0 6px">↻</button></div>
    <div style="display:flex;gap:3px;flex-wrap:wrap;margin-bottom:4px">${tabs}</div>
    <div class="muted" style="font-size:.7rem;margin-bottom:3px">${esc(d.date || '')}${d.source ? ` · ${esc(d.source)}` : ''}${d.at ? ` · built ${esc(d.at)} ET` : ''}</div>
    ${d.note ? `<p style="font-size:.74rem;margin:2px 4px"><b>${esc(d.note)}</b></p>` : ''}
    ${rows || `<p class="muted" style="font-size:.76rem;margin:4px">${k === 0 ? (d.error ? esc(d.error) : 'Today’s list builds at 09:15 ET (pre-market relative volume) and shows here by itself.') : 'No stocks in play stored for that day.'}</p>`}</div>`;
}
/* ---------- day trading stocks in play: ORB on the top relative-volume stocks (third Alpaca paper account) ---------- */
async function loadSD() { try { S.sd = await api('/stockday'); } catch (e) { S.sd = { error: e.message }; } if (['botchart', 'settings'].includes(route())) render();
  clearTimeout(S.sdPoll); const pr = (S.sd && S.sd.prep) || {}; if (pr.running && route() === 'botchart') S.sdPoll = setTimeout(loadSD, 15000); }
async function loadSDBt() { try { S.sdBt = await api('/stockday/backtest'); } catch (e) { S.sdBt = { error: e.message }; } if (route() === 'botchart') render();
  clearTimeout(S.sdBtPoll); if (S.sdBt && S.sdBt.running && route() === 'botchart') S.sdBtPoll = setTimeout(loadSDBt, 10000); }
async function loadSDChart(sym, date) { const k = `${sym}|${date || ''}`; S.sdBusy = k; if (route() === 'botchart') render();
  try { const d = await api('/stockday/chart', { method: 'POST', body: { t: sym, date } }); if (d.mode === 'gala' && (d.levels || []).length) d.fibs = { [d.date]: d.levels.map(x => [`${x.src} ${(+x.p).toFixed(2)}`, x.p]) }; else if (d.orb) d.fibs = { [d.date]: [['ORB high', d.orb.h], ['ORB low', d.orb.l]] }; S.sdFc = { k, d }; } catch (e) { S.sdFc = { k, d: { error: e.message } }; }
  S.sdBusy = null; if (route() === 'botchart') render(); }
function sdCfg() { return { ...((S.sd && S.sd.config) || {}), ...(S.sdDraft || {}) }; }
function sdPut(body, msg) { return api('/stockday', { method: 'PUT', body }).then(r => { S.sd = r; if (msg) toast(msg); render(); }).catch(err => toast(err.message)); }
function sdBody() { const b = { ...(S.sdDraft || {}) };
  document.querySelectorAll('[data-sdnum]').forEach(i => { const v = parseFloat(i.value); if (!isNaN(v)) b[i.dataset.sdnum] = v; });
  document.querySelectorAll('[data-sdtxt]').forEach(i => { b[i.dataset.sdtxt] = i.value; }); return b; }
function sdAccountBox(full) {
  const a = (S.sd && S.sd.account) || {}, c = sdCfg();
  if (!a.connected && !full) return '<p class="muted" style="font-size:.85rem;margin:6px 0">Stock account not connected: connect a THIRD Alpaca paper account in <a href="#settings">Settings → Brokers</a> to trade this live (the backtest works without it).</p>';
  if (!a.connected) return `<div style="border:1px dashed var(--line);border-radius:8px;padding:8px 10px;margin:6px 0;font-size:.85rem"><b>Stock day-trading account</b> — a THIRD Alpaca paper account (its own API key / secret), apart from the options bot and the QQQ / SPY day account.
    <form id="sd-acct" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px"><input id="sd-key" placeholder="API key" style="width:14em" autocomplete="off"><input id="sd-secret" placeholder="Secret" type="password" style="width:18em" autocomplete="off"><button class="btn coachbtn" type="submit">Connect</button></form></div>`;
  const pos = a.positions || [];
  return `<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;font-size:.85rem;margin:4px 0">
    <b>Stock account</b> <span class="muted">paper ··${esc(a.keyHint || '')}${a.equity ? ` · equity ${money(+a.equity)} · buying power ${money(+a.buyingPower)}` : ''}${a.error ? ` · <span class="loss">${esc(a.error)}</span>` : ''}</span>
    <label style="display:inline-flex;gap:4px;align-items:center"><input type="checkbox" data-sdlive="enabled" ${c.enabled ? 'checked' : ''}>run every minute</label>
    <label style="display:inline-flex;gap:4px;align-items:center"><input type="checkbox" data-sdlive="trade" ${c.trade ? 'checked' : ''}>place orders</label>
    <button class="btn" data-sddisconnect style="font-size:.75rem;padding:0 8px">Disconnect</button>
    ${pos.length ? `<span>Positions: ${pos.map(p0 => `<b>${esc(p0.symbol)}</b> ${esc(p0.side)} ${esc(p0.qty)} <span class="${+p0.pl >= 0 ? 'gain' : 'loss'}">${+p0.pl >= 0 ? '+' : ''}${money(+p0.pl)}</span>`).join(' · ')}</span>` : '<span class="muted">no position</span>'}</div>`;
}
function sdSettingsBox() {
  if (S.sd === undefined) { S.sd = null; setTimeout(loadSD, 0); }
  if (!S.sd) return '<p class="loading" style="padding:0">Loading…</p>';
  if (S.sd.error) return `<div class="errbox">${esc(S.sd.error)}</div>`;
  return `<p class="muted" style="margin:0 0 6px;font-size:.85rem">A THIRD Alpaca <b>paper</b> account for the stocks-in-play day trading (Chart → Day trading stocks).</p>${sdAccountBox(true)}`;
}
function sdPanel() {
  const c = sdCfg(), num = (k, step, w, l, suf) => `<span class="muted" style="font-size:.78rem">${l}</span> <input data-sdnum="${k}" type="number" step="${step}" value="${c[k] ?? ''}" style="width:${w}em;padding:1px 4px"><span class="muted" style="font-size:.75rem">${suf || ''}</span>`;
  const tog = (k, l, tip) => `<button class="btn ${c[k] ? 'coachbtn' : ''}" data-sdsw="${k}" title="${esc(tip || '')}" style="font-size:.78rem;padding:1px 10px">${c[k] ? 'ON' : 'OFF'} · ${l}</button>`;
  const txt = (k, l, w) => `<span class="muted" style="font-size:.78rem">${l}</span> <input data-sdtxt="${k}" value="${esc(Array.isArray(c[k]) ? c[k].join(' ') : (c[k] || ''))}" style="width:${w}em;padding:1px 4px">`;
  return `<div style="display:flex;flex-direction:column;gap:7px;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0;font-size:.82rem">
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">Universe</b>${num('minPrice', 0.5, 4, 'price >', '$')} ${num('minAvgVol', 100000, 7, '14-day avg volume >')} ${num('minAtr', 0.05, 4, 'ATR >', '$')} ${txt('exclude', 'never', 12)}</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">In play</b>
      <span class="muted" style="font-size:.78rem">rank by</span> ${[['orb', 'opening-range RVOL'], ['pm', 'pre-market RVOL']].map(([v, l]) => `<button class="btn ${c.rvolMode === v ? 'coachbtn' : ''}" data-sdset="rvolMode=${v}" style="font-size:.75rem;padding:0 8px">${l}</button>`).join('')}
      ${num('minRvol', 0.1, 3.6, 'RVOL ≥', '×')} ${num('topN', 1, 3.4, 'top', 'stocks')}
      <span class="muted" style="font-size:.78rem">opening range</span> ${[5, 15, 30].map(m => `<button class="btn ${+c.orbMin === m ? 'coachbtn' : ''}" data-sdset="orbMin=${m}" style="font-size:.75rem;padding:0 8px">${m} min</button>`).join('')}</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">Pre-market</b><span class="muted" style="font-size:.78rem">09:15 watchlist:</span> ${num('gWatchN', 1, 3, 'top', 'stocks')} ${num('gWatchMinPmVol', 10000, 6, 'pre-market volume ≥', 'shares')} ${tog('gWatch', 'Gala trades the 09:15 list only', 'Live and in the backtest (Gala strategy): the top pre-market RVOL stocks with a clear hourly trend, instead of the opening-range ranking')} <button class="btn coachbtn" data-sdwatch style="font-size:.75rem;padding:0 10px">${S.sdWatchBusy ? 'Building…' : 'Build the list now'}</button></div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">Strategy</b>${[['orb', 'Opening-range breakout'], ['gala', 'Gala hourly levels']].map(([v, l]) => `<button class="btn ${(c.mode || 'orb') === v ? 'coachbtn' : ''}" data-sdset="mode=${v}" style="font-size:.75rem;padding:0 8px">${l}</button>`).join('')}
      <span class="muted" style="font-size:.75rem">${c.mode === 'gala' ? '1-hour trend + hourly levels, 5-minute breaking retest / bounce / rejection on the stocks in play' : 'stop entry at the opening-range high / low'}</span></div>
    ${c.mode === 'gala' ? `<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">Setups</b>${tog('gRetest', 'Breaking retest', 'A 5-min close beyond an hourly level, then a wick back to it and a close on the right side; stop beyond the most extreme retest wick')} ${tog('gBounce', 'Bounce', 'Uptrend: candles test hourly support with the bodies holding above; stop under the lowest wick')} ${tog('gReject', 'Rejection', 'Downtrend: candles test hourly resistance with the bodies staying below; stop above the highest wick')} ${tog('longs', 'Longs')} ${tog('shorts', 'Shorts')}</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">Hourly</b>${tog('gTrend', 'Trend filter', 'Higher highs + higher lows → longs only; lower highs + lower lows → shorts only; unclear → no trade')} ${num('gPivot', 1, 3, 'pivot', 'candles each side')} ${num('gLvlDays', 1, 3, 'levels of the last', 'days')} ${num('gMaxCross', 1, 3, 'drop after', 'crosses (0 = never)')} ${num('gImpulse', 0.1, 3.4, 'impulse open: body ≥', '× avg range (0 = off)')} ${num('gTolAtr', 0.5, 3.4, 'touch within', '% of ATR')}</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">5-min entry</b>${num('gTestBars', 1, 3, 'bounce / rejection', 'test candles')} ${num('gRetestMax', 1, 3, 'retest within', 'candles of the break')} ${num('gStopBuf', 0.5, 3.4, 'stop buffer', '% of ATR')} ${txt('gEnd', 'entries until', 4)} ${tog('gMktAlign', 'Index alignment', 'Long only when SPY and QQQ are both above their VWAP, short only when both are below')} ${tog('gRelStr', 'Relative strength', 'Long only when the stock is up more than SPY since the open, short only when it is weaker')} ${tog('gSkipEvents', 'Skip FOMC / CPI days')}</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">Exit</b>${num('gTargetR', 0.1, 3.4, 'target', 'R')} ${tog('gLvlTarget', 'Next level first', 'Target at the next hourly level when it comes before the R target (at least 1R away)')} ${num('gPartial', 5, 3.6, 'take', '% at the target')} ${num('gTrailBars', 1, 3, 'runner trailed behind', '5-min candles (after break-even)')} ${txt('flatT', 'flat at', 4)}</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">Limits</b>${num('gMaxTrades', 1, 3, 'max', 'trades a day')} ${num('gMaxTries', 1, 3, 'max', 'attempts per level')} ${tog('gOneDone', 'One-and-done', 'No more trades after a winner')} ${num('gOneDoneR', 0.1, 3.4, 'winner ≥', 'R')}</div>` : `<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">Entry</b>${tog('longs', 'Longs')} ${tog('shorts', 'Shorts')} ${tog('dirLock', 'Direction lock', 'Green opening-range candle → longs only, red → shorts only, doji → skip')} ${num('dojiPct', 1, 3.4, 'doji: body <', '% of the range')}
      ${tog('vwapSide', 'VWAP side', 'Long only when the breakout is above the session VWAP, short only below')} ${tog('cvdSide', 'CVD side', 'Long only when the cumulative volume delta from the open is above 0 at the breakout, short only below (estimated from the candles)')} ${txt('entryEnd', 'entries until', 4)} ${tog('skipMid', 'Skip 12:00-15:00', 'No entries between noon and 3 pm')}</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">Exit</b>${num('stopAtrPct', 1, 3.6, 'stop', '% of the 14-day ATR')} ${tog('vwapExit', 'VWAP exit', 'Out on a 1-minute CLOSE on the other side of the VWAP (not a wick)')} ${num('vwapCap', 1, 3, 'done after', 'VWAP stop-outs (0 = off)')} ${txt('flatT', 'flat at', 4)}</div>`}
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="min-width:5.5em">Sizing</b>${num('riskPct', 0.1, 3.4, 'risk', '% of capital per trade')} ${num('maxLev', 0.5, 3.4, 'max leverage', '× (split over the top N)')}</div>
    <div style="display:flex;gap:6px;align-items:center"><button class="btn ${S.sdDirty ? 'coachbtn' : ''}" data-sdapply style="font-size:.8rem;padding:1px 12px">${S.sdDirty ? 'Apply changes' : 'Apply'}</button><span class="muted" style="font-size:.75rem">${c.mode === 'gala' ? 'Half at the target, the runner trailed, flat at the close.' : 'Exit at the close, no target (the papers).'} Settings apply to the backtest and to the live stock account.</span></div></div>`;
}
function sdPrepBox() {
  const v = S.sd || {}, pr = v.prep || {}, cov = v.coverage || {};
  return `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;font-size:.84rem"><b style="font-size:.95rem">History</b>
    <span>${cov.rDays || 0} days with opening ranges${cov.first ? ` (${esc(cov.first)} → ${esc(cov.last)})` : ''} · universe ${cov.uDays || 0} days</span>
    <select id="sd-prepdays" style="padding:1px 4px">${[250, 500, 1000].map(n => `<option value="${n}" ${n === (S.sdPrepDays || 500) ? 'selected' : ''}>${n} trading days</option>`).join('')}</select>
    <button class="btn coachbtn" data-sdprep ${pr.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 12px">${pr.running ? 'Building… (runs in the background)' : 'Build / extend history'}</button>
    <span class="muted" style="font-size:.78rem">${esc(pr.progress || '')}${pr.error ? ` · <span class="loss">${esc(pr.error)}</span>` : ''} · daily candles of every US stock, then the pre-market + opening-range volume of the candidates of each day (Alpaca SIP, once; ~30-60 min for 500 days on the free data plan). Updated every morning at 09:05.</span></div>`;
}
function sdWatchBox() {
  const w = (S.sd && S.sd.watch) || {}, pk = w.picks || [], f2 = v => v == null ? '—' : (+v).toFixed(2);
  const today = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
  const busy = S.sdWatchBusy || (w.building && (!w.built || w.built < w.building) && w.date === today);
  const btn = `<button class="btn" data-sdwatch ${busy ? 'disabled' : ''} style="font-size:.74rem;padding:0 8px">${busy ? 'Building… (~30 s)' : 'Build now'}</button>`;
  const head = `<b style="font-size:.9rem">Pre-market watchlist ${esc(w.date || '')} ${esc(w.at || '')}</b> <span class="muted" style="font-size:.75rem">every morning at 09:15 ET: top pre-market RVOL with a clear hourly trend${w.candidates ? ` · ${w.candidates} stocks traded pre-market` : ''}${(w.events || []).length ? ` · <b class="loss">event day: ${esc(w.events.join(', ').toUpperCase())}</b>` : ''}</span> ${btn}
    ${w.market ? `<div style="font-size:.78rem;margin:3px 0"><b>Market</b> ${['SPY', 'QQQ'].map(i => { const m = w.market[i] || {}; return `${i} <span class="${m.trend === 'up' ? 'gain' : m.trend === 'down' ? 'loss' : 'muted'}">1h ${esc(m.trend || '—')}</span>${m.gapPct != null ? ` <span class="muted">(${m.gapPct > 0 ? '+' : ''}${(+m.gapPct).toFixed(2)}% pre-market)</span>` : ''}`; }).join(' · ')} · bias <b>${w.market.bias > 0 ? 'calls / longs' : w.market.bias < 0 ? 'puts / shorts' : 'mixed'}</b> · news ${(w.events || []).length ? `<b class="loss">${esc(w.events.join(', ').toUpperCase())}</b>` : '<span class="gain">no FOMC / CPI / jobs report</span>'}${w.reviewed ? ` <span class="muted">· ${w.reviewed} reviewed, the cleanest ${pk.length} kept</span>` : ''}</div>` : ''}`;
  if (!pk.length) return `<div style="margin:8px 0 4px">${head}<p class="muted" style="font-size:.8rem;margin:4px 0">${busy ? 'Building the list…' : w.error ? `<span class="loss">${esc(w.error)}</span>` : 'No list yet today.'}</p></div>`;
  return `<div style="margin:8px 0 4px">${head}<div style="overflow-x:auto"><table class="pos" style="font-size:.8rem"><thead><tr><th>Stock</th><th class="r">Pre-mkt RVOL</th><th class="r">Pre-mkt vol</th><th class="r">Price</th><th class="r">Gap</th><th>1h trend</th><th>Plan</th><th>Pre-checks</th><th>Levels nearby</th><th></th></tr></thead><tbody>
    ${pk.map(p0 => `<tr><td><b>${esc(p0.sym)}</b></td><td class="r">${f2(p0.pmRvol)}×</td><td class="r">${(+p0.pmVol || 0).toLocaleString()}</td><td class="r">${f2(p0.price)}</td><td class="r ${+p0.gapPct >= 0 ? 'gain' : 'loss'}">${p0.gapPct != null ? (p0.gapPct > 0 ? '+' : '') + (+p0.gapPct).toFixed(1) + '%' : '—'}</td>
      <td class="${p0.trend === 'up' ? 'gain' : p0.trend === 'down' ? 'loss' : ''}">${esc(p0.trend)}</td><td><b class="${p0.direction === 'long' ? 'gain' : p0.direction === 'short' ? 'loss' : ''}">${esc(p0.direction)}</b> · ${esc(p0.setup)}</td>
      <td style="font-size:.72rem;min-width:16em">${p0.grade ? `<b class="${p0.grade === 'A' ? 'gain' : p0.grade === 'D' ? 'loss' : ''}" style="font-size:.8rem">${esc(p0.grade)}</b> <span class="muted">${p0.score}/6</span><br>` : ''}${(p0.checks || []).map(c0 => `<span class="${c0.ok ? 'gain' : 'loss'}">${c0.ok ? '✓' : '✗'}</span> ${esc(c0.txt)}`).join('<br>')}</td>
      <td style="font-size:.74rem">${(p0.levels || []).map(l => `${f2(l.p)} <span class="muted">${esc(l.src)}</span>`).join(' · ')}</td><td><button class="btn" data-sdchart="${esc(p0.sym)}|${esc(w.date || '')}" style="font-size:.72rem;padding:0 8px">chart</button></td></tr>`).join('')}</tbody></table></div></div>`;
}
function sdScanBox() {
  const sc = (S.sd && S.sd.scan) || {}, pk = sc.picks || [];
  const f2 = v => v == null ? '—' : (+v).toFixed(2);
  if (!pk.length) return `<p class="muted" style="font-size:.82rem;margin:6px 0">Today's stocks in play appear here once the opening range is complete (live, with “run every minute” on).${sc.error ? ` <span class="loss">${esc(sc.error)}</span>` : ''}</p>`;
  return `<b style="font-size:.9rem;display:block;margin:8px 0 4px">Stocks in play ${esc(sc.date || '')} ${esc(sc.at || '')} <span class="muted" style="font-weight:400;font-size:.75rem">${sc.candidates} in the universe${sc.feed === 'iex' ? ' · IEX volume (no real-time SIP on this data plan)' : ''}</span></b>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.8rem"><thead><tr><th>Stock</th><th class="r">RVOL</th><th class="r">Pre-market RVOL</th><th class="r">ATR</th><th class="r">Range high / low</th><th>Direction</th><th></th></tr></thead><tbody>
    ${pk.map(p0 => `<tr><td><b>${esc(p0.sym)}</b></td><td class="r">${f2(p0.rvol)}×</td><td class="r">${f2(p0.pmRvol)}×</td><td class="r">${f2(p0.atr)}</td><td class="r">${f2(p0.h)} / ${f2(p0.l)}</td><td>${p0.dir > 0 ? '<span class="gain">long</span>' : p0.dir < 0 ? '<span class="loss">short</span>' : 'doji: skip'}</td><td><button class="btn" data-sdchart="${esc(p0.sym)}|${esc(sc.date || '')}" style="font-size:.72rem;padding:0 8px">chart</button></td></tr>`).join('')}</tbody></table></div>`;
}
function sdBtBox() {
  if (S.sdBt === undefined) { S.sdBt = null; setTimeout(loadSDBt, 0); }
  const t = S.sdBt || {};
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const head = `<div style="display:flex;gap:6px;align-items:center;margin-top:12px;flex-wrap:wrap"><b style="font-size:.95rem">Backtest</b> <select id="sd-btdays" style="padding:1px 4px">${[60, 120, 250, 500, 1000].map(n => `<option ${n === (S.sdBtDays || 250) ? 'selected' : ''} value="${n}">${n} trading days</option>`).join('')}</select>
    <button class="btn coachbtn" data-sdbt ${t.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 12px">${t.running ? 'Running… (a few minutes)' : 'Run'}</button><span class="muted" style="font-size:.78rem">on the stored history, the settings on screen; 1-minute candles of the top stocks of each day</span></div>`;
  if (t.error) return head + `<div class="errbox">${esc(t.error)}</div>`;
  if (!t.all) return head + ((t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : '');
  const row = (l, x) => `<tr><td>${l}</td><td class="r">${x.n || 0}</td><td class="r">${x.win ?? '—'}%</td><td class="r">${R(x.avgR)}</td><td class="r">${R(x.totR)}</td><td class="r">${x.pf ?? '—'}</td><td class="r">${R(x.avgWin)} / ${R(x.avgLoss)}</td></tr>`;
  const th = `<thead><tr><th></th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg R</th><th class="r">Total R</th><th class="r">PF</th><th class="r">Avg win / loss</th></tr></thead>`;
  const cv = t.curvePct || [];
  let svg = '';
  if (cv.length > 1) { const W0 = 640, H0 = 120, mn = Math.min(0, ...cv), mx = Math.max(0, ...cv), sx = i => 4 + i / (cv.length - 1) * (W0 - 8), sy = v => H0 - 6 - (v - mn) / ((mx - mn) || 1) * (H0 - 12);
    svg = `<svg viewBox="0 0 ${W0} ${H0}" style="width:100%;max-width:${W0}px;height:auto;display:block"><line x1="4" x2="${W0 - 4}" y1="${sy(0)}" y2="${sy(0)}" stroke="var(--line)"/><path d="${cv.map((v, i) => `${i ? 'L' : 'M'}${sx(i).toFixed(1)},${sy(v).toFixed(1)}`).join('')}" fill="none" stroke="${cv[cv.length - 1] >= 0 ? 'var(--gain)' : 'var(--loss)'}" stroke-width="2"/><text x="6" y="12" font-size="11" fill="var(--muted)">capital, % (sized: ${esc(String(sdCfg().riskPct))}% risk, ≤ ${esc(String(sdCfg().maxLev))}× leverage)</text></svg>`; }
  const tk = (title, arr) => arr && arr.length ? `<details style="margin:4px 0"><summary style="font-size:.84rem;cursor:pointer">${title}</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.8rem">${th}<tbody>${arr.map(x => row(`<a href="javascript:void 0" data-sdchart="${esc(x.sym)}|">${esc(x.sym)}</a>`, x)).join('')}</tbody></table></div></details>` : '';
  const tb = (title, obj) => obj && Object.keys(obj).length ? `<details style="margin:4px 0"><summary style="font-size:.84rem;cursor:pointer">${title}</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.8rem">${th}<tbody>${Object.entries(obj).map(([k, x]) => row(esc(k), x)).join('')}</tbody></table></div></details>` : '';
  return head + `<p class="muted" style="font-size:.8rem;margin:4px 0">${t.mode === 'gala' ? '<b>Gala hourly levels</b> · ' : ''}Run ${esc((t.at || '').slice(5, 16).replace('T', ' '))} · ${t.daysFound} days from ${esc(t.first || '')} · ${t.picksPerDay} stocks in play a day · ${t.secs} s</p>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.84rem">${th}<tbody>${row('<b>All</b>', t.all)}${t.h1 ? row(`1st half <span class="muted">(to ${esc(t.mid || '')})</span>`, t.h1) : ''}${t.h2 ? row('2nd half', t.h2) : ''}${Object.entries(t.byDir || {}).map(([k, x]) => row(esc(k), x)).join('')}</tbody></table></div>
    <p style="font-size:.84rem;margin:4px 0">t <b class="${t.t >= 2 ? 'gain' : t.t <= 0 ? 'loss' : ''}">${t.t ?? '—'}</b> · capital <b class="${t.retPct >= 0 ? 'gain' : 'loss'}">${t.retPct >= 0 ? '+' : ''}${t.retPct}%</b>${t.annPct != null ? ` (${t.annPct >= 0 ? '+' : ''}${t.annPct}% a year)` : ''} · max drawdown <b class="loss">${t.ddPct}%</b> · ${t.maxDD}R · longest losing streak <b>${t.maxLosing ?? '—'}</b> · green days ${t.greenDays ?? 0} / ${t.daysTraded ?? 0}</p>
    ${svg}<b style="font-size:.86rem;display:block;margin:6px 0 2px">By relative volume</b><div style="overflow-x:auto"><table class="pos" style="font-size:.8rem">${th}<tbody>${Object.entries(t.byRvol || {}).map(([k, x]) => row(esc(k), x)).join('')}</tbody></table></div>
    ${tb('By setup', t.bySetup)}${tb('By exit', t.byExit)}${tk('Best stocks (3+ trades)', t.best)}${tk('Worst stocks', t.worst)}${tb('By entry hour', t.byHour)}${tb('By month', t.byMonth)}
    ${(t.trades || []).length ? `<details style="margin:4px 0"><summary style="font-size:.84rem;cursor:pointer">Trades (last ${t.trades.length})</summary><div style="overflow-x:auto;max-height:340px;overflow-y:auto"><table class="pos" style="font-size:.78rem"><thead><tr><th>Stock</th><th>Entry</th><th>Side</th><th class="r">RVOL</th><th class="r">Price</th><th>Exit</th><th class="r">Price</th><th>Why</th><th class="r">R</th><th class="r">% capital</th></tr></thead><tbody>
      ${t.trades.slice().reverse().map(x => `<tr><td><a href="javascript:void 0" data-sdchart="${esc(x.sym)}|${esc((x.at || '').slice(0, 10))}">${esc(x.sym)}</a></td><td>${esc((x.at || '').slice(5).replace('T', ' '))}</td><td>${esc(x.dir)}${x.setup ? ` <span class="muted">${esc(x.setup)}</span>` : ''}</td><td class="r">${x.rvol}×</td><td class="r">${(+x.entry).toFixed(2)}</td><td>${esc((x.exitAt || '').slice(11))}</td><td class="r">${(+x.exit).toFixed(2)}</td><td>${esc(x.status)}</td><td class="r">${R(x.R)}</td><td class="r">${(+x.ret).toFixed(2)}%</td></tr>`).join('')}</tbody></table></div></details>` : ''}
    ${(t.errors || []).length ? `<p class="loss" style="font-size:.8rem">${esc(t.errors.join(' · '))}</p>` : ''}`;
}
function vChartSD() {
  if (S.sd === undefined) { S.sd = null; setTimeout(loadSD, 0); }
  const sel = S.sdSel || null;
  const k = sel ? `${sel.sym}|${sel.date || ''}` : null, d = S.sdFc && S.sdFc.k === k ? S.sdFc.d : null;
  if (sel && !d && S.sdBusy !== k) setTimeout(() => loadSDChart(sel.sym, sel.date), 0);
  const W = Math.max(320, window.innerWidth - 20), H = Math.max(380, window.innerHeight - 200);
  const id = 'sd-' + (sel ? sel.sym : 'none');
  const head = `<div class="fcf-head"><b style="font-size:1.25rem">${esc(sel ? sel.sym : 'Stocks in play')}</b> ${fcModeSeg()}
    <form id="sd-sym" style="display:inline-flex;gap:4px"><input id="sd-symin" placeholder="ticker" value="${esc(sel ? sel.sym : '')}" style="width:6em;padding:3px 6px"><input id="sd-datein" type="date" value="${esc(sel && sel.date ? sel.date : '')}" style="padding:2px 4px"><button class="btn" type="submit" style="padding:3px 10px">Chart</button></form>
    <span class="seg">${[['sig', 'Signals'], ['vwap', 'VWAP'], ['fibs', 'Opening range'], ['pres', 'CVD'], ['cvd', 'Order flow']].map(([l, n]) => `<button class="btn ${S.fcLayers[l] ? 'coachbtn' : ''}" data-fclayer="${l}" style="font-size:.8rem;padding:2px 9px">${n}</button>`).join('')}</span>
    <button class="btn" data-hidesettings>${S.hideSettings ? 'Show settings' : 'Hide settings'}</button><button class="btn ${S.hideAnalysis ? 'coachbtn' : ''}" data-hideanalysis title="Hide / show the backtest, search, compare and study sections under the chart">${S.hideAnalysis ? 'Show analysis' : 'Hide analysis'}</button>
    <span style="flex:1"></span><a class="btn" href="#botevals" aria-label="Close full screen" title="Close">✕</a></div>`;
  let main = '';
  if (sel) {
    if (!d) main = '<p class="loading">Loading 1-minute candles…</p>';
    else if (d.error) main = `<div class="errbox">${esc(d.error)}</div>`;
    else if (!(d.bars || []).length) main = '<p class="muted">No candles for this day.</p>';
    else { const fl = S.flow[sel.sym] || {}, ck = [JSON.stringify(S.fcLayers), JSON.stringify(S.fcZoom[fcKey(id)] || null), W, H, fl.k, (fl.missing || []).length, Object.keys(fl.days || {}).length].join('|');
      main = svgCached('sd', d, ck, () => fcSvg(withFlow(sel.sym, d), id, { W, H: Math.max(360, H - 40), noAcc: true }));
      const sg = (d.signals || [])[0];
      main += `<div class="fcf-info" style="font-size:.84rem"><span><b>${esc(d.date)}</b> pre-market volume ${(d.pmVol || 0).toLocaleString()}${d.orb ? ` · opening range ${(+d.orb.h).toFixed(2)} / ${(+d.orb.l).toFixed(2)} (${d.orb.c > d.orb.o ? 'green' : d.orb.c < d.orb.o ? 'red' : 'doji'})` : ''}${sg ? ` · ${esc(sg.dir)} ${esc(sg.at.slice(11))} @ ${(+sg.entry).toFixed(2)} → ${esc(sg.status)} ${esc((sg.exitAt || '').slice(11))} @ ${(+sg.exit).toFixed(2)} <b class="${sg.R > 0 ? 'gain' : 'loss'}">${sg.R > 0 ? '+' : ''}${sg.R}R</b>` : ' · no trade with these settings'}</span></div>`; }
  } else main = '<p class="muted" style="padding:10px">Pick a stock from today\'s stocks in play or the backtest trades, or type a ticker and a date.</p>';
  const live = (S.sd && S.sd.trades) || [];
  const info = `${sdAccountBox()}${sdWatchBox()}${sdScanBox()}
    ${live.length ? `<details style="margin:4px 0"><summary style="font-size:.85rem;cursor:pointer">Live stock trades (${live.length})</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.8rem"><thead><tr><th>Stock</th><th>At</th><th>Side</th><th class="r">Qty</th><th class="r">Entry stop</th><th class="r">Stop</th><th class="r">RVOL</th><th>Status</th><th>Exit</th></tr></thead><tbody>
      ${live.slice().reverse().map(r0 => `<tr><td>${esc(r0.symbol)}</td><td>${esc((r0.at || '').replace('T', ' ').slice(5))}</td><td>${esc(r0.dir || '')}</td><td class="r">${r0.qty ?? ''}</td><td class="r">${r0.level ?? ''}</td><td class="r">${r0.stop ?? ''}</td><td class="r">${r0.rvol ?? ''}</td><td>${esc(r0.status || '')}</td><td>${esc(r0.exitReason || '')}</td></tr>`).join('')}</tbody></table></div></details>` : ''}
    ${S.hideSettings ? '' : sdPanel()}${S.hideAnalysis ? '' : sdPrepBox() + sdBtBox()}`;
  return `<div class="fcf">${head}<div class="fcf-body"><div class="fcf-main">${main}${info}</div></div></div>`;
}
S.flow = {};
try { S.hideAnalysis = !!localStorage.getItem('hideAnalysis'); } catch (e) { S.hideAnalysis = false; }
async function loadFlow2(sym, dates) {
  const k = sym + '|' + dates.join(','); if (S.flowBusy2 === k) return; S.flowBusy2 = k;
  try { S.flow[sym] = { k, ...(await api('/flow', { method: 'POST', body: { t: sym, dates } })) }; } catch (e) { S.flow[sym] = { k, error: e.message, days: {} }; }
  S.flowBusy2 = null; if (route() === 'botchart') render();
  clearTimeout(S.flowPoll2); if (S.flow[sym] && S.flow[sym].running && route() === 'botchart') S.flowPoll2 = setTimeout(() => { S.flow[sym] = { ...S.flow[sym], k: '' }; loadFlow2(sym, dates); }, 15000);
}
function withFlow(sym, d) {                       // the real order flow of the days on the chart (layer 'Order flow')
  if (!S.fcLayers.cvd || !d || !(d.bars || []).length) return d;
  const dates = [...new Set(d.bars.map(b => b.t.slice(0, 10)))].slice(-10), k = sym + '|' + dates.join(','), f = S.flow[sym];
  if ((!f || f.k !== k) && S.flowBusy2 !== k) setTimeout(() => loadFlow2(sym, dates), 0);
  if (!f || !f.days) return { ...d, flowLoading: true };
  const cvd = {}, big = [];
  Object.entries(f.days).forEach(([dy, x]) => { if (dates.includes(dy)) { cvd[dy] = x.min || {}; (x.big || []).forEach(r => big.push(r)); } });
  return { ...d, cvd: Object.keys(cvd).length ? cvd : null, big, flowLoading: !!(f.missing || []).length };
}
async function dtWhy(sym, t) {
  S.dtWhy = { sym, at: t, busy: true }; if (route() === 'botchart') render();
  try { S.dtWhy = { sym, at: t, ...(await api('/daytrade/why', { method: 'POST', body: { t: sym, at: t, cfg: dtBody() } })) }; } catch (e) { S.dtWhy = { sym, at: t, error: e.message }; }
  if (route() === 'botchart') render();
}
function dtWhyBox() {
  const w = S.dtWhy;
  const form = `<form id="dt-why" style="display:inline-flex;gap:4px;align-items:center;font-size:.8rem"><span class="muted">Why (no) trade at</span><input id="dt-whyat" placeholder="YYYY-MM-DD HH:MM" value="${esc(w && w.at ? w.at.replace('T', ' ') : '')}" style="width:10.5em;padding:1px 4px"><button class="btn" type="submit" style="font-size:.75rem;padding:0 8px">Check</button><span class="muted">or click a candle (1-minute view)</span></form>`;
  if (!w) return `<div style="margin:4px 0">${form}</div>`;
  if (w.busy) return `<div style="margin:4px 0">${form} <span class="loading" style="padding:0">checking ${esc(w.at.replace('T', ' '))}…</span></div>`;
  if (w.error) return `<div style="margin:4px 0">${form} <span class="loss" style="font-size:.8rem">${esc(w.error)}</span></div>`;
  const names = Object.fromEntries(DT_RULES.map(r => [r[0], r[2]]));
  const rules = [...new Set((w.rules || []).map(r => r.rule))];
  const ok = side => (w.rules || []).filter(r => r.side === side).every(r => r.ok);
  const cell = (k, side) => { const r = (w.rules || []).find(x => x.rule === k && x.side === side); return r ? (r.ok ? '<span class="gain">✓</span>' : '<b class="loss">✗</b>') : ''; };
  const wl = w.walls || {};
  return `<div style="margin:4px 0;border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:.8rem">${form}
    <div style="margin:4px 0"><b>${esc(w.symbol)} ${esc(w.at.replace('T', ' '))}</b> close ${(+w.close).toFixed(2)} · CVD ${(+w.cvd).toLocaleString()} · put wall ${wl.putWall ?? '—'} · flip ${wl.flip ?? '—'} · call wall ${wl.callWall ?? '—'}
      · long <b class="${ok('long') ? 'gain' : 'loss'}">${ok('long') ? 'all rules pass' : 'blocked'}</b> · short <b class="${ok('short') ? 'gain' : 'loss'}">${ok('short') ? 'all rules pass' : 'blocked'}</b>
      ${(w.notes || []).length ? ` · <span class="loss">${esc(w.notes.join(' · '))}</span>` : ''}</div>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.78rem;width:auto"><thead><tr><th>Rule</th><th class="r">Long</th><th class="r">Short</th></tr></thead><tbody>${rules.map(k => `<tr><td>${esc(names[k] || k)}</td><td class="r">${cell(k, 'long')}</td><td class="r">${cell(k, 'short')}</td></tr>`).join('')}</tbody></table></div></div>`;
}
function dtPut(body, msg) { return api('/daytrade', { method: 'PUT', body }).then(r => { S.dt = r; if (msg) toast(msg); S.dtFc = null; render(); }).catch(err => toast(err.message)); }
function vChartFull() {
  if (S.fcfMode === 'dt') return vChartDT();
  if (S.fcfMode === 'sd') S.fcfMode = 'dt';
  if (S.fcfMode === 'ww') return vChartWW();
  const L = S.tickers, items = (L && L.items) || [], sym = fcfSymbol(), cur0 = items.find(x => x.symbol === sym), evId = hashParam('id');
  const cur = cur0 && evId ? { ...cur0, id: evId } : cur0;      // a specific evaluation (backtest replay) or the latest one
  const narrow = window.innerWidth < 800, sideW = narrow ? 0 : 260, headH = S.fcfHeadH || (narrow ? 104 : 58), infoH = S.fcfInfoH || (narrow ? 96 : 64);
  const W = Math.max(320, window.innerWidth - sideW - 20), H = Math.max(300, window.innerHeight - headH - infoH - 18);
  const days = S.fcDays || 5, d = cur ? S.fc[fcKey(cur.id)] : null;
  if (cur && !d && !S.fcBusy[fcKey(cur.id)]) setTimeout(() => loadFlowChart(cur.id), 0);
  if (cur) fcNeedDark(cur.id);
  if (cur && d && !d.error && evId && !S.fcAutoZ[fcKey(cur.id)]) {        // replay: zoom around the trade the first time
    S.fcAutoZ[fcKey(cur.id)] = true;
    const bs = fcClean(d).bars, tr = d.trade || {}, ix = t => { let k = -1; bs.forEach((b0, i) => { if (b0.t <= t) k = i; }); return k; };
    const a0 = ix((tr.at || '').slice(0, 16)), b0 = tr.exitAt ? ix(tr.exitAt.length === 10 ? tr.exitAt + 'T15:59' : tr.exitAt) : bs.length - 1;
    if (a0 >= 0) { const pad = Math.max(10, Math.round((b0 - a0) * 0.3)); S.fcZoom[fcKey(cur.id)] = [Math.max(0, a0 - pad), Math.min(bs.length - 1, Math.max(b0, a0 + 8) + pad)]; }
  }
  const rl = S.replay || [], ri = cur ? rl.findIndex(x => x.id === cur.id) : -1;
  const nav = ri >= 0 ? `<span class="seg"><a class="btn" href="${ri > 0 ? `#botchart?t=${encodeURIComponent(rl[ri - 1].symbol)}&id=${rl[ri - 1].id}` : '#'}" ${ri > 0 ? '' : 'aria-disabled="true" style="opacity:.4;pointer-events:none"'}>◀ prev idea</a><span class="btn" style="cursor:default">Idea ${ri + 1} of ${rl.length}</span><a class="btn" href="${ri < rl.length - 1 ? `#botchart?t=${encodeURIComponent(rl[ri + 1].symbol)}&id=${rl[ri + 1].id}` : '#'}" ${ri < rl.length - 1 ? '' : 'aria-disabled="true" style="opacity:.4;pointer-events:none"'}>next idea ▶</a></span>` : '';
  const back = `#botevals${sym ? '?t=' + encodeURIComponent(sym) : ''}`;
  const zb_ = (k, l, t) => cur ? `<button class="btn" data-fczoom="${cur.id}|${k}" title="${t}" style="padding:3px 10px">${l}</button>` : '';
  const seg = `<span class="seg">${[[5, '5D · 15m'], [45, '45D · 1h']].map(([v, l]) => `<button class="btn ${days === v ? 'coachbtn' : ''}" data-fcdays="${v}" style="padding:3px 10px">${l}</button>`).join('')}</span>`;
  const sel = narrow ? `<select id="fcf-sel" aria-label="Ticker">${items.map(x => `<option value="${esc(x.symbol)}" ${x.symbol === sym ? 'selected' : ''}>${esc(x.symbol)} · ${esc(x.decision || '')}</option>`).join('')}</select>` : '';
  const headBar = `<div class="fcf-head">
      <b style="font-size:1.25rem">${esc(sym || 'Chart')}</b>${cur ? `<span class="verdict ${DEC_CLS[cur.decision] || 'mid'}">${esc(cur.decision || '')}</span>
      <span class="muted" style="font-size:.85rem">evaluated ${esc((cur.at || '').replace('T', ' ').slice(5, 16))} ET${cur.price ? ` at ${px(cur.price)}` : ''}</span>` : ''}
      ${sel}${fcModeSeg()}${seg}${zb_('in', '＋', 'Zoom in')}${zb_('out', '−', 'Zoom out')}${zb_('left', '◀', 'Earlier')}${zb_('right', '▶', 'Later')}${zb_('reset', 'Reset', 'Show everything')}
      ${nav}${cur ? fcLayerBar(cur.id) : ''}
      <span style="flex:1"></span>${cur ? `<a class="btn" href="#botevals?t=${encodeURIComponent(sym)}">Evaluation</a>` : ''}<a class="btn" href="${back}" aria-label="Close full screen">✕</a></div>`;
  let main;
  if (!L) main = '<p class="loading" style="padding:16px">Loading tickers…</p>';
  else if (L.error) main = `<div class="errbox" style="margin:16px">${esc(L.error)}</div>`;
  else if (!cur) main = '<p class="muted" style="padding:16px">No evaluated ticker yet.</p>';
  else if (!d) main = '<p class="loading" style="padding:16px">Loading the chart, the alerts and the gamma walls…</p>';
  else if (d.error) main = `<div class="errbox" style="margin:16px">${esc(d.error)}</div>`;
  else {
    const al = d.alerts || [], sum = xs => xs.reduce((s2, a) => s2 + a.premium, 0), calls = al.filter(a => a.type === 'call'), puts = al.filter(a => a.type === 'put');
    const sa = S.fcSel && S.fcSel.id === cur.id ? al[S.fcSel.k] : null, w = d.wallsNow, dpd = S.fcDp[fcKey(cur.id)];
    const dsel = S.fcSelD && S.fcSelD.id === cur.id && dpd ? (dpd.prints || [])[S.fcSelD.k] : null;
    main = fcSvg(d, cur.id, { W, H }) + `<div class="fcf-info">
      <span><b class="gain">Calls</b> ${calls.length} · ${money(sum(calls), false)} &nbsp; <b class="loss">Puts</b> ${puts.length} · ${money(sum(puts), false)}</span>
      ${w ? `<span>Walls: <b class="gain">call ${w.callWall}</b> · <b class="loss">put ${w.putWall}</b> · <b style="color:var(--coach)">flip ${w.flip ?? '—'}</b></span>` : ''}
      ${(() => { const tr = d.trade; if (!tr || !tr.at) return ''; const r = tr.R;
        return `<span><b>${tr.taken ? 'Trade' : 'Backtest'}</b> ${esc(tr.at.replace('T', ' ').slice(5))} · ${esc(tr.contract)} at ${tr.entryOpt ?? '—'} · stop ${tr.stop ?? '—'} / target ${tr.target ?? '—'} · ${tr.status === 'done' ? `exit ${esc(tr.exitAt || '')} (${esc(tr.reason || '')}) <b class="${r >= 0 ? 'gain' : 'loss'}">${r >= 0 ? '+' : ''}${r}R</b>` : tr.status ? `<b>still open</b>${r != null ? ` ${r >= 0 ? '+' : ''}${r}R so far` : ''}` : 'no result yet'}</span>`; })()}
      <span>${sa ? `<b>${esc(sa.t.replace('T', ' ').slice(5))} ET · ${sa.type === 'call' ? '<span class="gain">CALL</span>' : '<span class="loss">PUT</span>'} ${money(sa.premium, false)}</b> · ${esc(sa.contract || '')}${sa.sweep ? ' · sweep' : ''}` : '<span class="muted">Tap a dot to see the alert · drag or scroll on the chart to zoom · ↑ ↓ to change ticker</span>'}</span>
      ${(() => { const e = S.fcSelE && S.fcSelE.id === cur.id ? (d.evals || [])[S.fcSelE.k] : null; return e ? `<span><b>Evaluated ${esc(e.t.replace('T', ' ').slice(5))} ET</b> · <span class="verdict ${DEC_CLS[e.decision] || 'mid'}" style="font-size:.75rem;padding:1px 7px">${esc(e.decision || '')}</span>${e.taken ? ' · <b>traded</b>' : ''}${e.why ? ` · <span class="muted">${esc(e.why)}</span>` : ''} · <a href="#botevals?t=${encodeURIComponent(sym)}" data-evload="${e.id}">open it</a></span>` : `<span class="muted">${(d.evals || []).length} evaluation${(d.evals || []).length === 1 ? '' : 's'} in this period</span>`; })()}
      ${dsel ? `<span><b style="color:var(--warn)">DARK POOL</b> ${esc(dsel.t.replace('T', ' ').slice(5))} ET · ${dsel.size.toLocaleString('en-US')} sh at ${dsel.price} · <b>${money(dsel.premium, false)}</b></span>` : ''}
      ${S.fcLayers.dark && dpd && !dpd.error && (dpd.levels || []).length ? `<span>Dark pool levels: ${dpd.levels.slice(0, 3).map(l => `<b style="color:var(--warn)">${l.price}</b> ${money(l.premium, false)}`).join(' · ')}</span>` : ''}
      ${d.wallNote && S.fcLayers.walls ? `<span class="muted">${esc(d.wallNote)}</span>` : ''}</div>`;
  }
  const side = narrow ? '' : `<aside class="fcf-side"><input id="fcf-q" type="search" placeholder="Filter tickers" value="${esc(S.fcfQ || '')}" aria-label="Filter tickers">
      <ul class="fcf-list">${items.map(x => `<li data-sym="${esc(x.symbol)}" ${S.fcfQ && !x.symbol.includes(S.fcfQ.toUpperCase()) ? 'hidden' : ''}><a href="#botchart?t=${encodeURIComponent(x.symbol)}" ${x.symbol === sym ? 'aria-current="true"' : ''}>
        <span><b>${esc(x.symbol)}</b>${x.held ? ' <span class="exchip" style="font-size:.7rem">held</span>' : ''}<br><span class="muted" style="font-size:.75rem">${esc((x.at || '').slice(5, 10))} · ${x.count} eval${x.count === 1 ? '' : 's'}</span></span>
        <span style="text-align:right"><span class="verdict ${DEC_CLS[x.decision] || 'mid'}" style="font-size:.72rem;padding:1px 7px">${esc(x.decision || '')}</span><br><span class="muted" style="font-size:.75rem">${x.price ? px(x.price) : ''}</span></span></a></li>`).join('')}</ul></aside>`;
  return `<div class="fcf">${headBar}<div class="fcf-body"><div class="fcf-main">${main}</div>${side}</div></div>`;
}
(() => {
  let rt = null;
  window.addEventListener('resize', () => { if (route() !== 'botchart') return; clearTimeout(rt); rt = setTimeout(render, 150); });
  document.addEventListener('change', e => { if (e.target.id === 'fcf-sel') location.hash = '#botchart?t=' + encodeURIComponent(e.target.value); });
  document.addEventListener('change', e => { if (e.target.id !== 'opt-sel') return;   // option test contract mode: show its fields
    const P = optParams(); P.sel = e.target.value; try { localStorage.setItem('optParams', JSON.stringify(P)); } catch (x) {} render(); });
  document.addEventListener('change', e => {      // bot checkboxes: pending until Apply
  if (e.target.id === 'ww-auto' || e.target.id === 'ww-trade') {
    S.wwDraft = { ...(S.wwDraft || {}), [e.target.id === 'ww-auto' ? 'autoAdd' : 'trade']: e.target.checked }; S.wwDirty = true; wwMarkDirty(); }
});
document.addEventListener('input', e => {       // settings typed but not applied yet: kept across redraws
  const t = e.target;
  if (t.dataset && (t.dataset.pk || t.dataset.labnum)) {
    const v = parseFloat(t.value), k = t.dataset.pk || t.dataset.labnum;
    if (isNaN(v)) return;
    const val = t.dataset.scale ? v * +t.dataset.scale : v;
    if (t.dataset.pk) { S.wwDraft = { ...(S.wwDraft || {}), [k]: val }; S.wwDirty = true; wwMarkDirty(); }
    else { S.labDraft = { ...(S.labDraft || {}), [k]: val }; S.labDirty = true; wwMarkDirty(); }
  }
});
document.addEventListener('input', e => {
    if (e.target.id !== 'fcf-q') return;
    S.fcfQ = e.target.value.trim(); const q = S.fcfQ.toUpperCase();
    document.querySelectorAll('.fcf-list li').forEach(li => { li.hidden = !!q && !li.dataset.sym.includes(q); });
  });
  document.addEventListener('keydown', e => {
    if (route() !== 'botchart' || /INPUT|SELECT|TEXTAREA/.test((e.target.tagName || ''))) return;
    if (e.key === 'Escape') { if (S.fcfMode === 'ww') { location.hash = '#botwatch'; return; } const s2 = fcfSymbol(); location.hash = `#botevals${s2 ? '?t=' + encodeURIComponent(s2) : ''}`; return; }
    if ((e.key === 'ArrowRight' || e.key === 'ArrowLeft') && (S.replay || []).length) {      // replay: next / previous idea
      const id = hashParam('id'), i = S.replay.findIndex(x => x.id === id), nx = S.replay[i + (e.key === 'ArrowRight' ? 1 : -1)];
      if (i >= 0 && nx) { e.preventDefault(); location.hash = `#botchart?t=${encodeURIComponent(nx.symbol)}&id=${nx.id}`; }
      return;
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    const lis = [...document.querySelectorAll('.fcf-list li:not([hidden])')].map(li => li.dataset.sym);
    const list = lis.length ? lis : S.fcfMode === 'ww' ? wwTickers() : ((S.tickers && S.tickers.items) || []).map(x => x.symbol);
    const i = list.indexOf(fcfSymbol()); if (!list.length) return;
    e.preventDefault();
    const nx = list[Math.max(0, Math.min(list.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))];
    if (nx) location.hash = '#botchart?t=' + encodeURIComponent(nx);
  });
})();
function flowChartPanel(r) {
  if (!r || !r.id || !r.createdAt) return '';
  const days = S.fcDays || 5, d = S.fc[fcKey(r.id)];
  if (!d && !S.fcBusy[fcKey(r.id)]) setTimeout(() => loadFlowChart(r.id), 0);
  const seg = `<span class="seg" style="margin-left:8px">${[[5, '5 days · 15 min'], [45, '45 days · 1 hour']].map(([v, l]) => `<button class="btn ${days === v ? 'coachbtn' : ''}" data-fcdays="${v}" style="font-size:.8rem;padding:2px 8px">${l}</button>`).join('')}</span>`;
  const head = `<h3 style="font-size:.95rem;margin:14px 0 6px;display:flex;flex-wrap:wrap;align-items:center;gap:4px">Flow on the chart <span class="muted" style="font-weight:400;font-size:.82rem">${days > 10 ? 'hourly' : '15-minute'} price · every option bought at the ask, from ${money((d && d.minPrint) || 10000, false)}</span>${seg}<a class="btn" href="#botchart?t=${encodeURIComponent(r.symbol)}" style="font-size:.8rem;padding:2px 10px;margin-left:4px">⛶ Full screen</a></h3>`;
  if (!d) return head + '<p class="loading" style="margin:0">Loading the chart and the alerts…</p>';
  if (d.error) return head + `<p class="muted" style="margin:0">${esc(d.error)}</p>`;
  const al = d.alerts || [], calls = al.filter(a => a.type === 'call'), puts = al.filter(a => a.type === 'put');
  const sum = xs => xs.reduce((s2, a) => s2 + a.premium, 0);
  const sel = S.fcSel && S.fcSel.id === r.id ? al[S.fcSel.k] : null;
  const top = [...al].sort((a, b) => b.premium - a.premium).slice(0, 6);
  const N = (d.bars || []).length, [za, zb] = N > 1 ? fcRange(r.id, N) : [0, 0], zoomed = N > 1 && (za > 0 || zb < N - 1);
  const zb_ = (k, l, t) => `<button class="btn" data-fczoom="${r.id}|${k}" title="${t}" style="font-size:.85rem;padding:2px 10px">${l}</button>`;
  const zbar = N > 1 ? `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 6px">${zb_('in', '＋', 'Zoom in')}${zb_('out', '−', 'Zoom out')}${zb_('left', '◀', 'Earlier')}${zb_('right', '▶', 'Later')}${zoomed ? zb_('reset', 'Reset', 'Show everything') : ''}
    <span class="muted" style="font-size:.8rem">${zoomed && d.bars[za] && d.bars[zb] ? `${esc(d.bars[za].t.replace('T', ' ').slice(5))} → ${esc(d.bars[zb].t.replace('T', ' ').slice(5))} · ` : ''}drag across the chart to zoom on a period${window.innerWidth >= 700 ? ', or scroll on it' : ''}</span></div>` : '';
  const wi = d.wallInfo, nW = (d.walls || []).length;
  const wline = `<p class="muted" style="font-size:.82rem;margin:0 0 6px">${d.wallSource === 'uw' && wi ? `Gamma walls: ${wi.days} of ${wi.of} days from Unusual Whales${wi.days < wi.of ? ` — missing ${(wi.missing || []).map(x => x.slice(5)).slice(0, 8).join(', ')}${(wi.missing || []).length > 8 ? '…' : ''} (shown as gaps) <button class="btn" data-fcreload="${r.id}" style="font-size:.78rem;padding:1px 8px">Retry</button>` : ''}.`
    : nW ? `Gamma walls: ${nW} reading${nW === 1 ? '' : 's'} from the bot.` : '<b>No gamma walls for this period.</b>'} ${d.wallNote ? esc(d.wallNote) : ''}</p>`;
  fcNeedDark(r.id);
  const lbar = `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 6px">${fcLayerBar(r.id)}</div>`;
  const dsel = S.fcSelD && S.fcSelD.id === r.id && S.fcDp[fcKey(r.id)] ? (S.fcDp[fcKey(r.id)].prints || [])[S.fcSelD.k] : null;
  return head + zbar + lbar + wline + `<div style="border:1px solid var(--line);border-radius:10px;padding:6px;background:var(--surface)">${fcSvg(d, r.id)}</div>
    ${(() => { const e = S.fcSelE && S.fcSelE.id === r.id ? (d.evals || [])[S.fcSelE.k] : null; return e ? `<p style="margin:6px 0 0;font-size:.88rem"><b>Evaluated ${esc(e.t.replace('T', ' ').slice(5))} ET</b> · <span class="verdict ${DEC_CLS[e.decision] || 'mid'}" style="font-size:.75rem;padding:1px 7px">${esc(e.decision || '')}</span>${e.taken ? ' · <b>traded</b>' : ''}${e.price ? ` · at ${px(e.price)}` : ''}${e.why ? ` · <span class="muted">${esc(e.why)}</span>` : ''}${e.id !== r.id ? ` · <a href="#" data-evload="${e.id}">open it</a>` : ' · <span class="muted">this one</span>'}</p>` : ''; })()}
    ${dsel ? `<p style="margin:6px 0 0;font-size:.88rem"><b style="color:var(--warn)">DARK POOL</b> <b>${esc(dsel.t.replace('T', ' ').slice(5))} ET · ${dsel.size.toLocaleString('en-US')} shares at ${dsel.price} · ${money(dsel.premium, false)}</b></p>` : ''}
    <p class="st-legend" style="margin:6px 0 4px"><span><i class="st-dot g"></i>Calls bought (${calls.length}, ${money(sum(calls), false)})</span><span><i class="st-dot" style="display:inline-block;background:var(--loss)"></i>Puts bought (${puts.length}, ${money(sum(puts), false)})</span><span>bigger dot = bigger premium · outlined = over ${money(d.bigPrint, false)}</span><span style="color:var(--coach,#5b5bd6)">┆ evaluated</span>${(d.walls || []).length ? `<span><b class="gain">- -</b> call wall · <b class="loss">- -</b> put wall · <b style="color:var(--coach,#5b5bd6)">···</b> gamma flip (steps = how they moved)</span>` : ''}</p>
    ${d.wallsNow ? `<p class="muted" style="font-size:.84rem;margin:0 0 4px">Latest walls (${esc(d.wallsNow.source || '')}): call wall <b>${d.wallsNow.callWall}</b> · put wall <b>${d.wallsNow.putWall}</b> · flip <b>${d.wallsNow.flip ?? '—'}</b>. ${d.wallSource === 'uw' ? 'Each step is one day\'s open interest, so you can see the walls move as positions are opened and closed.' : 'Open interest updates once a day, so the walls mostly move overnight.' + ((d.walls || []).length < 2 ? ' History builds from each evaluation and each time this chart is opened.' : '')} The lower panel adds up the premium bought at the ask: calls (green) vs puts (red).</p>` : ''}

    <p style="margin:2px 0 6px;font-size:.88rem;min-height:1.3em">${sel ? `<b>${esc(sel.t.replace('T', ' ').slice(5))} ET · ${sel.type === 'call' ? '<span class="gain">CALL</span>' : '<span class="loss">PUT</span>'} ${money(sel.premium, false)}</b> · ${esc(sel.contract || '')}${sel.sweep ? ' · sweep' : ''}${sel.rule ? ` · ${esc(sel.rule)}` : ''}` : '<span class="muted">Tap a dot to see the alert.</span>'}</p>
    ${top.length ? `<details><summary class="muted" style="cursor:pointer">Biggest prints</summary><ul style="list-style:none;padding:0;margin:6px 0 0;line-height:1.7;font-size:.88rem">${top.map(a => `<li><span class="${a.type === 'call' ? 'gain' : 'loss'}">${a.type.toUpperCase()}</span> <b>${money(a.premium, false)}</b> <span class="muted">${esc(a.t.replace('T', ' ').slice(5))} · ${esc(a.contract || '')}${a.sweep ? ' · sweep' : ''}</span></li>`).join('')}</ul></details>` : ''}
    ${d.note ? `<p class="muted" style="font-size:.82rem">${esc(d.note)}</p>` : ''}`;
}
function botResult(r) {
  if (!r) return '';
  const p = r.proposal, groups = ['flow', 'chart', 'gamma', 'events', 'contract'], gl = { flow: 'Flow', chart: 'Chart', gamma: 'Gamma', events: 'Events', contract: 'Contract' };
  return `<section class="panel"><div class="cal-head"><h2>${esc(r.symbol)} <span class="verdict ${DEC_CLS[r.decision]}" style="margin-left:8px">${r.decision}</span></h2><span style="display:flex;gap:10px;align-items:center"><span class="muted" style="font-size:.85rem">${esc(r.createdAt.replace('T', ' ').slice(0, 16))} ET · ${esc(r.source)} data · price ${px(r.signals.price)}</span><button class="btn" data-resclose="1" aria-label="Close this detail">Close</button></span></div>
    ${r.origin && r.origin.type === 'flow' ? `<p style="margin:6px 0 0"><b>From unusual flow:</b> ${money(r.origin.premium, false)} in ${r.origin.alerts} alert${r.origin.alerts === 1 ? '' : 's'}${r.origin.sweep ? ', sweep' : ''}${r.origin.askPct != null ? `, ${r.origin.askPct}% at the ask` : ''}${r.origin.volOi != null ? `, vol/OI ${r.origin.volOi}` : ''}${r.origin.contract ? ` · flow contract ${esc(r.origin.contract)}` : ''}</p>` : ''}
    ${flowChartPanel(r)}
    ${(() => { const today = nyTime(Date.now()).slice(0, 10), day = (r.createdAt || '').slice(0, 10);
      if (!day || day >= today || r.orderId) return '';
      return `<div class="stale-box"><b>These checks are from ${esc(day.slice(5))} ${esc((r.createdAt || '').slice(11, 16))} ET</b>, when ${esc(r.symbol)} was at ${px(r.signals && r.signals.price)}. The chart above runs to today, the checks below don't.
        <button class="btn coachbtn" data-reeval="${esc(r.symbol)}">Re-evaluate ${esc(r.symbol)} with today's data</button></div>`; })()}
    ${groups.map(gname => { const items = r.checks.filter(c => c.group === gname); return items.length ? `<h3 style="font-size:.95rem;margin:12px 0 4px">${gl[gname]}</h3><ul style="list-style:none;padding:0;margin:0;line-height:1.6">${items.map(c => `<li><b class="${c.ok ? 'gain' : c.required ? 'loss' : 'muted'}">${c.ok ? '✓' : c.required ? '✗' : '!'}</b> ${esc(c.text)}${!c.required ? ' <span class="muted">(warning only)</span>' : ''}</li>`).join('')}</ul>` : ''; }).join('')}
    ${r.blocking.length && r.decision !== 'BUY' ? `<p style="margin:12px 0 0"><b>Why not a buy:</b> ${r.blocking.map(esc).join('; ')}.</p>` : ''}
    ${p ? `<h3 style="font-size:.95rem;margin:16px 0 6px">Proposed order</h3>
      <div class="tablewrap" style="border:0"><table class="pvsa"><tbody>
        ${isSpread(p) ? `<tr><td>Structure</td><td class="r"><b>${STRAT_LBL[p.strategy]}</b></td></tr>
        <tr><td>Buy</td><td class="r"><b>${esc(r.symbol)} ${esc(fmtExp(p.exp))} ${p.strike} call</b> <span class="muted">${esc(p.contract)} · mid ${p.legs[0].mid} · delta ${p.legs[0].delta}</span></td></tr>
        <tr><td>Sell</td><td class="r"><b>${p.shortContract ? `${esc(r.symbol)} ${esc(fmtExp(p.shortExp))} ${p.shortStrike} call` : 'no short call held — selling the next cycle'}</b>${p.shortContract ? ` <span class="muted">${esc(p.shortContract)} · mid ${p.legs[1].mid} · delta ${p.legs[1].delta}</span>` : ''}</td></tr>
        <tr><td>Spreads × net debit</td><td class="r">${p.qty} × ${p.limit.toFixed(2)} = ${money(p.cost, false)}</td></tr>
        <tr><td>Width${p.maxProfit != null ? ' / max profit' : ''} / max loss</td><td class="r">${p.width}${p.maxProfit != null ? ` / ${money(p.maxProfit * 100 * p.qty, false)}` : ''} / ${money(p.maxLoss * 100 * p.qty, false)}${p.breakeven ? ` <span class="muted">(breakeven ${px(p.breakeven)} at expiry)</span>` : ''}</td></tr>
        <tr><td>Exit if the spread falls to</td><td class="r">${p.optionStop.toFixed(2)} <span class="muted">(risk ${money(p.riskAtStop, false)})</span></td></tr>
        <tr><td>Take profit at spread</td><td class="r">${p.optionTarget != null ? p.optionTarget.toFixed(2) : 'none — keeps rolling the short call'}</td></tr>
        ${(r.rolls || []).length ? `<tr><td>Short calls rolled</td><td class="r">${r.rolls.length}× · net ${money((r.cashAdj || 0) * 100 * (r.filledQty || p.qty), false)} ${(r.cashAdj || 0) >= 0 ? 'collected' : 'paid'}</td></tr>` : ''}` : `<tr><td>Contract</td><td class="r"><b>${esc(r.symbol)} ${esc(fmtExp(p.exp))} ${p.strike} call</b> <span class="muted">${esc(p.contract)}</span></td></tr>
        <tr><td>Quantity × limit</td><td class="r">${p.qty} × ${p.limit.toFixed(2)} = ${money(p.cost, false)}</td></tr>
        <tr><td>Exit if the option falls to</td><td class="r">${p.optionStop.toFixed(2)} <span class="muted">(risk ${money(p.riskAtStop, false)})</span></td></tr>
        <tr><td>Take profit at option</td><td class="r">${p.optionTarget.toFixed(2)}</td></tr>`}
        <tr><td>Exit if ${esc(r.symbol)} closes below</td><td class="r">${px(p.underlyingStop)}</td></tr>
        <tr><td>Take profit if ${esc(r.symbol)} reaches</td><td class="r">${px(p.underlyingTarget)}</td></tr>
      </tbody></table></div>` : ''}
    ${(r.candidates || []).length ? `<details style="margin-top:10px" ${r.proposal ? '' : 'open'}><summary style="cursor:pointer" class="muted">Contracts considered (best first)</summary><div class="tablewrap" style="border:0"><table><thead><tr><th>Expiry</th><th class="r">Strike</th><th class="r">Delta</th><th class="r">Mid</th><th class="r">Spread</th><th class="r">OI</th><th class="r">Breakeven</th><th class="r">EM upper</th><th>Issues</th></tr></thead><tbody>
      ${r.candidates.map(c => `<tr><td>${fmtExp(c.exp)} (${c.dte}d)</td><td class="r">${c.strike}</td><td class="r">${c.delta}</td><td class="r">${c.mid}</td><td class="r">${c.spreadPct ?? '—'}%</td><td class="r">${c.oi}</td><td class="r">${c.breakeven}</td><td class="r">${c.emUpper ?? '—'}</td><td>${c.problems.map(esc).join(', ') || '<span class="gain">ok</span>'}</td></tr>`).join('')}
    </tbody></table></div></details>` : ''}
    ${(r.spreadCandidates || []).length ? `<details style="margin-top:6px"><summary style="cursor:pointer" class="muted">Short calls considered (best first)</summary><div class="tablewrap" style="border:0"><table><thead><tr><th>Expiry</th><th class="r">Strike</th><th class="r">Delta</th><th class="r">Mid</th><th class="r">Net debit</th><th class="r">Width</th><th class="r">Max profit</th><th>Issues</th></tr></thead><tbody>
      ${r.spreadCandidates.map(c => `<tr><td>${fmtExp(c.exp)} (${c.dte}d)</td><td class="r">${c.strike}</td><td class="r">${c.delta}</td><td class="r">${c.mid}</td><td class="r">${c.debit.toFixed(2)}</td><td class="r">${c.width}</td><td class="r">${c.maxProfit != null ? c.maxProfit.toFixed(2) : '—'}</td><td>${c.problems.map(esc).join(', ') || '<span class="gain">ok</span>'}</td></tr>`).join('')}
    </tbody></table></div></details>` : ''}
    ${(r.rolls || []).length ? `<details style="margin-top:6px"><summary style="cursor:pointer" class="muted">Rolls</summary><div class="tablewrap" style="border:0"><table><thead><tr><th>When</th><th>Bought back</th><th>Sold</th><th class="r">Net per spread</th></tr></thead><tbody>
      ${r.rolls.map(x => `<tr><td>${esc(x.at.replace('T', ' ').slice(5, 16))}</td><td>${esc(x.from || '—')}</td><td>${esc(x.to)}</td><td class="r ${cls(x.credit)}">${x.credit >= 0 ? '+' : ''}${x.credit.toFixed(2)}</td></tr>`).join('')}</tbody></table></div></details>` : ''}
    ${(r.marks || []).length > 1 ? `<h3 style="font-size:.95rem;margin:16px 0 6px">${p && (p.strategy === 'bull_call' || p.strategy === 'diagonal') ? 'Spread' : 'Option'} value since entry (every ~15 min)</h3>${markLine(r)}` : ''}
    ${tradePathPanel(r)}
    ${putWatchPanel(r)}
    ${afterExitPanel(r)}
    ${shadowPanel(r)}
    ${aiPanel(r)}
    ${reviewPanel(r)}
    ${evTicker() === r.symbol ? '' : `<p style="margin:12px 0 0"><button class="btn" data-rescan="${esc(r.symbol)}">Re-evaluate ${esc(r.symbol)} now</button></p>`}
    ${p && r.status === 'proposed' ? `<div class="form-grid" style="align-items:end;margin-top:12px">
      <div class="field"><label for="bo-qty">${isSpread(p) ? 'Spreads' : 'Contracts'}</label><input id="bo-qty" type="number" min="1" value="${Math.max(1, p.qty)}"></div>
      <div class="field"><label for="bo-lim">${isSpread(p) ? 'Net debit limit' : 'Limit price'}</label><input id="bo-lim" type="number" step="0.05" value="${p.limit.toFixed(2)}"></div>
      <div class="field"><button class="btn coachbtn" data-botorder="${r.id}" ${S.aiBusy === r.id ? 'disabled' : ''}>${orderLabel(r)}</button></div>
      ${aiOn() && r.aiCheck && !aiPasses(r.aiCheck) ? `<div class="field"><button class="btn" data-botorder="${r.id}" data-override="1">Place anyway (override Claude)</button></div>` : ''}
      <div class="field"><button class="btn" data-botdismiss="${r.id}">Dismiss</button></div></div>` : ''}
  </section>`;
}
/* ---------- tracking for optimization: best/worst during taken trades, and what skipped trades would have done ---------- */
const sgn = (v, d = 1) => v == null ? '—' : `${v > 0 ? '+' : ''}${(+v).toFixed(d)}`;
async function loadShadow() {
  const days = S.shadowDays || 5;
  const [a, b] = await Promise.all([api(`/bot/shadow/summary?days=${days}`).catch(e => ({ error: e.message })),
                                    api(`/bot/flow/summary?days=${days}`).catch(e => ({ error: e.message }))]);
  S.shadowSum = a; S.flowSum = b;
  if (isBotRoute()) render();
}
async function runShadow() {
  const before = S.shadowSum && S.shadowSum.run && S.shadowSum.run.at;
  S.shadowBusy = true; render();
  try { await api('/bot/shadow/run', { method: 'POST' }); } catch (e) { S.shadowBusy = false; render(); toast(e.message); return; }
  for (let i = 0; i < 40; i++) {
    await sleep(4000);
    try { const r = await api(`/bot/shadow/summary?days=${S.shadowDays || 5}`); if (r.run && r.run.status !== 'running' && r.run.at !== before) { S.shadowSum = r; break; } } catch (e) {}
  }
  S.shadowBusy = false; await loadEvals(S.botEvals ? S.botEvals.page : 1); render();
  const res = (S.shadowSum && S.shadowSum.run && S.shadowSum.run.result) || {};
  toast(res.error ? `Shadow tracking failed: ${res.error}` : `Shadow tracking updated (${res.tracked ?? 0} evaluations followed).`);
}
function shList() {
  const L = S.shList;
  if (!L || L.loading) return '<p class="loading" style="margin:8px 12px">Loading the skipped trades…</p>';
  if (L.error) return `<div class="errbox" style="margin:8px 12px">${esc(L.error)}</div>`;
  const R = v => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${sgn(v, 2)}R</span>`;
  return `<div><p class="muted" style="font-size:.82rem;margin:0 2px 6px">${L.n} skipped trade${L.n === 1 ? '' : 's'}, best result first · tap one to open it</p>
    <ul class="shl">${L.items.map(x => `<li data-shopen="${x.id}"><span class="shl-a"><b>${esc(x.symbol)}</b> <span class="muted">${x.exp ? esc(fmtExp(x.exp)) + ' ' + x.strike + 'c' : ''}</span></span>
      <span class="shl-r">${R(x.R)} <span class="${x.plPct > 0 ? 'gain' : x.plPct < 0 ? 'loss' : ''}">${sgn(x.plPct)}%</span></span>
      <span class="shl-b muted">${esc((x.at || '').replace('T', ' ').slice(5, 16))} · ${esc(x.decision || '')}${x.claude ? ` · Claude ${esc(x.claude)}` : ''} · ${x.status === 'tracking' ? `still open (day ${x.days || 0})` : esc(x.exitReason || 'done')}</span></li>`).join('')}</ul></div>`;
}
async function openShadow(kind, key) {
  if (S.shSel && S.shSel[kind] === key) { S.shSel = null; render(); return; }
  S.shSel = { [kind]: key }; S.shList = { loading: true }; render();
  const show = () => { const b = $('.shbox'); if (b) b.scrollIntoView({ behavior: 'smooth', block: 'start' }); };
  show();                                          // the list opens under the table: bring it into view
  try { S.shList = await api(`/bot/shadow/list?days=${S.shadowDays || 5}&${kind}=${encodeURIComponent(key)}`); }
  catch (e) { S.shList = { error: e.message }; }
  render(); show();
}
async function openRecord(id) {
  try {
    const rec = await api(`/bot/${id}`);
    S.botRes = rec; location.hash = `#botevals?t=${encodeURIComponent(rec.symbol)}`; window.scrollTo(0, 0);
  } catch (e) { toast(e.message); }
}
function flowSummary() {
  const d = S.flowSum;
  if (!d || d.error || !d.n) return d && d.error ? `<section><h2>Which flow is worth acting on</h2><div class="errbox">${esc(d.error)}</div></section>` : '';
  const R = v => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${sgn(v, 2)}R</span>`;
  const rows = d.dimensions.filter(x => x.groups.length).map(dim => {
    const best = Math.max(...dim.groups.filter(g => g.n >= 5 && g.avgR != null).map(g => g.avgR));
    return `<tr><th colspan="5" style="background:var(--sunk);text-align:left">${esc(dim.title)}</th></tr>` + dim.groups.map(g =>
      `<tr><td style="padding-left:18px">${esc(g.label)}${g.n >= 5 && g.avgR === best && best > 0 ? ' <span class="verdict ok" style="font-size:.72rem">best</span>' : ''}</td><td class="r">${g.n}${g.tracking ? ` <span class="muted">(${g.tracking} open)</span>` : ''}</td><td class="r">${g.winPct == null ? '—' : g.winPct + '%'}</td><td class="r">${R(g.avgR)}</td><td class="r">${R(g.totalR)}</td></tr>`).join('');
  }).join('');
  return `<section><div class="cal-head"><h2>Which flow is worth acting on</h2><span class="muted" style="font-size:.85rem">${d.n} flow ideas · ${R(d.all.avgR)} avg · same period</span></div>
    <div class="tablewrap"><table><thead><tr><th>Flow characteristic</th><th class="r">Ideas</th><th class="r">Winners</th><th class="r">Avg R</th><th class="r">Total R</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="muted" style="font-size:.82rem;margin:10px 0 0">Every idea that came from unusual flow, whatever the bot did with it: the real result when it was taken, the shadow result when it was skipped. "best" marks the strongest group with at least 5 ideas. Look for groups that stay clearly better as the sample grows; small groups are noise.</p></section>`;
}
function shadowSummary() {
  const d = S.shadowSum;
  const days = S.shadowDays || 5;
  const sel = `<select id="sh-days" aria-label="Period">${[5, 10, 20, 35].map(n => `<option value="${n}" ${n === days ? 'selected' : ''}>Last ${n} days</option>`).join('')}</select>`;
  const btn = `<button class="btn" id="sh-run" ${S.shadowBusy ? 'disabled' : ''}>${S.shadowBusy ? 'Following the skipped trades…' : 'Update now'}</button>`;
  const head = `<div class="cal-head"><h2>Skipped trades: what they would have done</h2><div style="display:flex;gap:8px;align-items:center">${sel}${btn}</div></div>`;
  if (!d) return `<section>${head}<p class="loading">Loading…</p></section>`;
  if (d.error) return `<section>${head}<div class="errbox">${esc(d.error)}</div></section>`;
  const R = v => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${sgn(v, 2)}R</span>`;
  const row = (label, g, attr) => `<tr ${attr || ''} ${attr ? 'style="cursor:pointer"' : ''} ${S.shSel && attr && ((S.shSel.why !== undefined && attr.includes(`data-shwhy="${esc(S.shSel.why)}"`)) || (S.shSel.claude !== undefined && attr.includes(`data-shclaude="${esc(S.shSel.claude)}"`))) ? 'class="sel" aria-current="true"' : ''}><td class="shg-l">${label}${attr ? ' <span class="muted">›</span>' : ''}</td><td class="r" data-l="trades">${g.n}${g.tracking ? ` <span class="muted">(${g.tracking} still open)</span>` : ''}</td><td class="r" data-l="winners">${g.winPct == null ? '—' : g.winPct + '%'}</td><td class="r" data-l="avg">${R(g.avgR)}</td><td class="r" data-l="total">${R(g.totalR)}</td></tr>`;
  const th = first => `<thead><tr><th>${first}</th><th class="r">Trades</th><th class="r">Winners</th><th class="r">Avg R</th><th class="r">Total R</th></tr></thead>`;
  const sk = d.skipped, tk = d.taken;
  const rr = (d.run && d.run.result) || {};
  const runTxt = d.run && d.run.at ? `Last update ${esc(d.run.at.replace('T', ' ').slice(5, 16))} ET${d.run.status === 'error' || rr.error ? ` (failed: ${esc(rr.error || 'unknown error')})` : rr.errors ? ` (${rr.errors} contract${rr.errors > 1 ? 's' : ''} without data: ${esc(rr.firstError || '')})` : ''}` : 'Not run yet: press <b>Update now</b>';
  const exits = (d.exits || []).length ? `<h3 style="font-size:.95rem;margin:16px 0 6px">After the bot's exits <span class="muted" style="font-weight:400;font-size:.82rem">change from the exit price, in R · positive = it kept going up (exit was early)</span></h3>
    <div class="tablewrap"><table><thead><tr><th>Exit reason</th><th class="r">Trades</th><th class="r">+1 day</th><th class="r">+5 days</th><th class="r">+10 days</th><th class="r">Best after</th><th class="r">Higher now</th></tr></thead><tbody>
    ${d.exits.map(g => `<tr><td>${esc(g.why)}</td><td class="r">${g.n}</td><td class="r">${R(g.day1R)}</td><td class="r">${R(g.day5R)}</td><td class="r">${R(g.day10R)}</td><td class="r">${R(g.bestR)}</td><td class="r">${g.earlyPct == null ? '—' : g.earlyPct + '%'}</td></tr>`).join('')}
    </tbody></table></div>` : '';
  if (!sk.n) return `<section>${head}<p class="muted">No skipped trade has a result yet in this period.${d.pending ? ` ${d.pending} evaluation${d.pending > 1 ? 's are' : ' is'} waiting to be followed: press <b>Update now</b>.` : ''} ${runTxt}.</p>${exits}</section>`;
  const list = (title, xs) => xs.length ? `<h3 style="font-size:.95rem;margin:14px 0 6px">${title}</h3><ul style="list-style:none;padding:0;margin:0;line-height:1.7">${xs.map(x => `<li data-shopen="${x.id}" style="cursor:pointer">${R(x.R)} <b>${esc(x.symbol)}</b> <span class="muted">${esc((x.at || '').replace('T', ' ').slice(5, 16))} · ${esc(x.decision)}${x.claude ? ` · Claude ${esc(x.claude)}` : ''} · ${esc(x.why)} · ${esc(x.exitReason || 'still open')}</span></li>`).join('')}</ul>` : '';
  return `<section>${head}
    <div class="strip" style="margin-bottom:12px">
      <div class="stat"><span>Skipped with a result</span><b>${sk.n}</b><span>${sk.done} finished · ${sk.tracking} still open${d.pending ? ` · ${d.pending} waiting` : ''}</span></div>
      <div class="stat"><span>If taken: avg R</span><b>${R(sk.avgR)}</b><span>${sk.winPct ?? '—'}% winners · total ${sk.totalR ?? '—'}R</span></div>
      <div class="stat"><span>Taken by the bot: avg R</span><b>${R(tk.avgR)}</b><span>${tk.n} trades · ${tk.winPct ?? '—'}% winners</span></div>
    </div>
    <div class="tablewrap"><table class="shg">${th('Why it was skipped')}<tbody>${d.byReason.map(g => row(esc(g.why), g, `data-shwhy="${esc(g.why)}"`)).join('')}</tbody></table></div>
    ${S.shSel && S.shSel.why !== undefined ? `<div class="shbox"><div class="cal-head" style="margin:0 0 6px"><b>Skipped because: ${esc(S.shSel.why)}</b><button class="btn" data-shwhy="${esc(S.shSel.why)}">Close</button></div>${shList()}</div>` : ''}
    <div class="tablewrap" style="margin-top:10px"><table class="shg">${th("Claude's entry check")}<tbody>${d.byClaude.map(g => row(esc(g.verdict), g, `data-shclaude="${esc(g.verdict)}"`)).join('')}</tbody></table></div>
    ${S.shSel && S.shSel.claude !== undefined ? `<div class="shbox"><div class="cal-head" style="margin:0 0 6px"><b>Claude's entry check: ${esc(S.shSel.claude)}</b><button class="btn" data-shclaude="${esc(S.shSel.claude)}">Close</button></div>${shList()}</div>` : ''}
    ${list('Best skipped trades', d.best)}${list('Worst skipped trades', d.worst)}
    ${exits}
    <p class="muted" style="font-size:.82rem;margin:10px 0 0">${runTxt}. Skipped trades are followed for up to 20 trading days with the bot's exit rules, closed trades for 10 trading days after the exit, on daily bars (approximate). A positive avg R on a skip reason means that filter is removing winners; a positive R after an exit reason means those exits come too early.</p>
  </section>`;
}
function shadowCell(i) {
  const s = i.shadow;
  if (!s) return '<td><span class="muted">—</span></td>';
  if (s.status === 'dup') return '<td><span class="muted" title="Same contract evaluated earlier that day: followed once">same as earlier</span></td>';
  if (s.status === 'nodata') return `<td><span class="muted" title="${esc(s.note || '')}">no data</span></td>`;
  if (s.status === 'tracking' && !s.days) return '<td><span class="muted" title="No trading day since the evaluation yet">waiting</span></td>';
  const cls = s.plPct > 0 ? 'gain' : s.plPct < 0 ? 'loss' : '';
  return `<td title="${esc(s.exitReason || 'still being followed')}"><span class="${cls}">${sgn(s.plPct)}%</span>${s.R != null ? ` <span class="muted">${sgn(s.R, 2)}R</span>` : ''}${s.status === 'tracking' ? ' <span class="muted" style="font-size:.78rem">tracking</span>' : ''}</td>`;
}
function shadowPanel(r) {
  const s = r.shadow;
  if (r.orderId || ['submitting', 'submitted', 'open', 'closing', 'closed'].includes(r.status) || !r.proposal) return '';
  const head = '<h3 style="font-size:.95rem;margin:16px 0 6px">If the bot had taken it <span class="muted" style="font-weight:400;font-size:.82rem">shadow tracking · daily bars · the bot\'s exit rules</span></h3>';
  const sat = (r.createdAt || '').slice(0, 10), nextRun = 'the next update (every trading day at 16:20 ET, or <b>Update now</b> on the Paper bot page)';
  if (!s) return head + `<p class="muted" style="margin:0">Not followed yet: it starts with ${nextRun}. Evaluated ${esc(sat)}; the first result uses that day's close if it was evaluated during the session, otherwise the next trading day.</p>`;
  if (s.status === 'tracking' && !s.days) return head + `<p class="muted" style="margin:0">Waiting for the first trading day after the evaluation (${esc(sat)}). The result appears with ${nextRun} after that day's close.</p>`;
  if (s.status === 'dup') return head + '<p class="muted" style="margin:0">The same contract was evaluated earlier that day; that evaluation is the one followed.</p>';
  if (s.status === 'nodata') return head + `<p class="muted" style="margin:0">No trades in this contract to follow (${esc(s.note || 'no data')}).</p>`;
  const cls = s.plPct > 0 ? 'gain' : s.plPct < 0 ? 'loss' : '';
  return head + `<div class="tablewrap" style="border:0"><table class="pvsa"><tbody>
    <tr><td>Result</td><td class="r"><b class="${cls}">${sgn(s.plPct)}%</b>${s.R != null ? ` · ${sgn(s.R, 2)}R` : ''} ${s.status === 'tracking' ? '<span class="muted">(still being followed)</span>' : ''}</td></tr>
    <tr><td>${s.status === 'done' ? 'Exit' : 'So far'}</td><td class="r">${esc(s.exitReason || `day ${s.days} of 20`)}${s.exitDate ? ` <span class="muted">${esc(s.exitDate)}</span>` : ''}</td></tr>
    <tr><td>Entry → ${s.status === 'done' ? 'exit' : 'last'}</td><td class="r">${(+s.entry).toFixed(2)} → ${(+(s.exitPrice ?? s.last)).toFixed(2)}</td></tr>
    <tr><td>Best / worst option move</td><td class="r"><span class="gain">${sgn(s.mfePct)}%</span> / <span class="loss">${sgn(s.maePct)}%</span>${s.closesOnly ? ' <span class="muted">(daily closes)</span>' : ''}</td></tr>
    ${s.undMax != null ? `<tr><td>${esc(r.symbol)} range while followed</td><td class="r">${s.undMin} – ${s.undMax}</td></tr>` : ''}
  </tbody></table></div><p class="muted" style="font-size:.8rem;margin:4px 0 0">Approximate: daily bars, so when a stop and a target both fall on the same day the stop is counted.</p>`;
}
function afterExitPanel(r) {
  const a = r.afterExit;
  if (r.status !== 'closed') return '';
  const head = '<h3 style="font-size:.95rem;margin:16px 0 6px">After the exit <span class="muted" style="font-weight:400;font-size:.82rem">the option for 10 trading days after the bot closed it</span></h3>';
  if (!a) return head + '<p class="muted" style="margin:0">Followed every trading day after the close (16:20 ET).</p>';
  if (!a.days) return head + `<p class="muted" style="margin:0">${esc(a.note || 'Nothing to show yet.')}</p>`;
  const v = (p, rr) => p == null ? '—' : `<span class="${p > 0 ? 'gain' : p < 0 ? 'loss' : ''}">${sgn(p)}%</span>${rr != null ? ` <span class="muted">${sgn(rr, 2)}R</span>` : ''}`;
  return head + `<div class="tablewrap" style="border:0"><table class="pvsa"><tbody>
    <tr><td>Exit price</td><td class="r">${(+a.exitPrice).toFixed(2)} <span class="muted">${esc(a.exitDate || '')}</span></td></tr>
    <tr><td>1 / 5 / 10 trading days later</td><td class="r">${v(a.day1Pct, a.day1R)} · ${v(a.day5Pct, a.day5R)} · ${v(a.day10Pct, a.day10R)}</td></tr>
    <tr><td>Best / worst after the exit</td><td class="r">${v(a.bestPct, a.bestR)} / ${v(a.worstPct, a.worstR)}${a.closesOnly ? ' <span class="muted">(daily closes)</span>' : ''}</td></tr>
    <tr><td>Verdict so far (day ${a.days})</td><td class="r">${a.lastPct > 10 ? '<b class="loss">Closed too early</b>' : a.lastPct < -10 ? '<b class="gain">Good exit</b>' : 'About even'}${a.status === 'tracking' ? ' <span class="muted">(still being followed)</span>' : ''}</td></tr>
  </tbody></table></div>`;
}
function putWatchPanel(r) {
  const pw = r.putWatch || [];
  if (!pw.length) return '';
  const lim = ((S.bot || {}).settings || {}).putMaxRatio ?? 0.5;
  return `<h3 style="font-size:.95rem;margin:16px 0 6px">Put flow since entry <span class="muted" style="font-weight:400;font-size:.82rem">puts vs calls bought at the ask on ${esc(r.symbol)} · checked daily at 15:40 ET</span></h3>
    <div class="tablewrap" style="border:0"><table><thead><tr><th>Day</th><th class="r">Puts today</th><th class="r">Calls today</th><th class="r">Put/call (look-back)</th></tr></thead><tbody>
    ${pw.slice().reverse().map(x => { const hot = (x.ratio != null && x.ratio > lim) || (x.todayPuts > x.todayCalls && x.todayPuts > 0);
      return `<tr><td>${esc(x.day)}${x.entry ? ' <span class="muted">(entry)</span>' : ''}</td><td class="r ${x.todayPuts > x.todayCalls ? 'loss' : ''}">${money(x.todayPuts || 0, false)}</td><td class="r">${money(x.todayCalls || 0, false)}</td><td class="r ${hot ? 'loss' : ''}">${x.ratio == null ? '—' : x.ratio.toFixed(2)}${hot ? ' ⚠' : ''}</td></tr>`; }).join('')}
    </tbody></table></div><p class="muted" style="font-size:.8rem;margin:4px 0 0">⚠ = put/call above ${lim} or more puts than calls that day; you get an alert and Claude weighs it in the end-of-day review.</p>`;
}
function tradePathPanel(r) {
  if (!r.fillPrice || r.optMax == null) return '';
  const R = r.realizedR != null ? r.realizedR : (r.riskAtFill && r.lastPl != null ? Math.round(r.lastPl / r.riskAtFill * 100) / 100 : null);
  return `<h3 style="font-size:.95rem;margin:16px 0 6px">During the trade</h3><div class="tablewrap" style="border:0"><table class="pvsa"><tbody>
    <tr><td>Result in R</td><td class="r"><b class="${R > 0 ? 'gain' : R < 0 ? 'loss' : ''}">${sgn(R, 2)}R</b>${r.riskAtFill ? ` <span class="muted">(1R = ${money(r.riskAtFill, false)}, the ${r.stopPctAtFill}% stop on the filled size)</span>` : ''}</td></tr>
    <tr><td>Best / worst P&L</td><td class="r"><span class="gain">${sgn(r.plMax)}%</span> / <span class="loss">${sgn(r.plMin)}%</span></td></tr>
    <tr><td>Option high / low</td><td class="r">${(+r.optMax).toFixed(2)} / ${(+r.optMin).toFixed(2)}</td></tr>
    ${r.undMax != null ? `<tr><td>${esc(r.symbol)} high / low</td><td class="r">${r.undMax} / ${r.undMin}</td></tr>` : ''}
  </tbody></table></div>`;
}

/* ---------- Claude's chart check (final visual verification before an order) ---------- */
const AI_CLS = { approve: 'ok', caution: 'mid', reject: 'bad' };
const isWW = r => !!(r && r.proposal && (r.proposal.wallwatch || r.proposal.strategyOnly === 'wallwatch'));
const aiCell = i => { const a = i.aiCheck && i.aiCheck.verdict ? i.aiCheck : null;
  const rv = isWW(i) ? null : i.aiReview, rvb = rv ? ` <span class="verdict ${rv.action === 'close' ? 'bad' : 'ok'}" title="${esc(`End-of-day review ${(rv.at || '').slice(5, 16).replace('T', ' ')}: ${rv.summary}`)}">${rv.action === 'close' ? 'EOD CLOSE' : 'EOD HOLD'}</span>` : '';
  if (a) return `<td title="${esc(a.summary || '')}"><span class="verdict ${AI_CLS[a.verdict] || 'mid'}">${esc(a.verdict.toUpperCase())}</span>${i.aiFrom ? ' <span class="muted" style="font-size:.78rem">carried</span>' : ''}${rvb}</td>`;
  if (rv) return `<td>${rvb}</td>`;
  if (i.aiReviewError && !isWW(i)) return `<td title="${esc(i.aiReviewError)}"><span class="loss">EOD error</span></td>`;
  if (i.aiRunning) return '<td><span class="muted">checking…</span></td>';
  if (i.aiError) return `<td title="${esc(i.aiError)}"><span class="loss">error</span></td>`;
  return '<td><span class="muted">—</span></td>'; };
S.aiImgs = S.aiImgs || {}; S.aiImgBusy = S.aiImgBusy || {};
const aiOn = () => ((S.bot && S.bot.settings) || {}).aiCheck !== false;
const aiPasses = a => !!a && (a.verdict === 'approve' || (a.verdict === 'caution' && ((S.bot && S.bot.settings) || {}).aiAllowCaution !== false));
const nyTime = ms => new Date(ms).toLocaleString('sv-SE', { timeZone: 'America/New_York' }).replace(' ', 'T');
const aiFresh = a => { const m = ((S.bot && S.bot.settings) || {}).aiMaxAgeMin || 30; return !!(a && a.at && a.at >= nyTime(Date.now() - m * 60000)); };
function orderLabel(r) {
  const base = r.decision === 'BUY' ? 'Place paper order' : 'Place paper order anyway';
  if (!aiOn()) return base;
  if (S.aiBusy === r.id) return 'Claude is checking…';
  if (r.aiCheck && aiFresh(r.aiCheck) && aiPasses(r.aiCheck)) return base;
  return r.decision === 'BUY' ? 'Check chart with Claude, then place' : 'Check chart with Claude, then place anyway';
}
function aiPanel(r) {
  if (!r.signals) return '';
  const busy = S.aiBusy === r.id || (r.aiRunning && !r.aiCheck);
  const prev = !r.aiCheck && r.prevAiCheck && r.prevAiCheck.verdict ? r.prevAiCheck : null, a = r.aiCheck || prev;
  const imgs = S.aiImgs[r.id];
  if (a && !imgs && !S.aiImgBusy[r.id]) setTimeout(() => loadAiImgs(r.id), 0);
  return `<h3 style="font-size:.95rem;margin:16px 0 6px">Claude's chart check</h3>
    ${busy ? '<p class="loading" style="padding:0;margin:0">Claude is looking at the daily and hourly charts… (about 20 seconds)</p>' : ''}
    ${!busy && prev ? `<p class="muted" style="margin:0 0 6px;font-size:.85rem">Previous check for ${esc(r.symbol)} from an earlier evaluation${prev.contract && r.proposal && prev.contract !== r.proposal.contract ? ` on ${esc(prev.contract)} (different contract)` : ''}. Claude looks again before an order.</p>` : ''}
    ${!busy && !prev && r.aiFrom ? '<p class="muted" style="margin:0 0 6px;font-size:.85rem">Carried over from the earlier evaluation of the same contract.</p>' : ''}
    ${!busy && a ? `<p style="margin:0 0 6px"><span class="verdict ${AI_CLS[a.verdict] || 'mid'}">${esc(a.verdict.toUpperCase())}</span> <span class="muted" style="font-size:.85rem">${a.confidence}% confidence · ${esc((a.at || '').replace('T', ' ').slice(5, 16))} ET${prev || aiFresh(a) ? '' : ' · older than the allowed age, will re-check before placing'}</span></p>
      <p style="margin:0 0 6px">${esc(a.summary)}</p>
      <ul style="list-style:none;padding:0;margin:0;line-height:1.6">${(a.supports || []).map(x => `<li><b class="gain">✓</b> ${esc(x)}</li>`).join('')}${(a.concerns || []).map(x => `<li><b class="loss">✗</b> ${esc(x)}</li>`).join('')}</ul>` : ''}
    ${!busy && !a ? `<p class="muted" style="margin:0">${aiOn() ? 'Not checked yet. Claude looks at the daily and hourly charts with the stop, target, gamma levels and fair value gaps before the order goes in.' : 'Off in the bot settings.'}</p>` : ''}
    ${r.aiError ? `<p class="loss" style="margin:6px 0 0">${esc(r.aiError)}</p>` : ''}
    ${imgs && imgs.length ? `<div style="display:grid;gap:10px;margin-top:10px">${imgs.map(x => `<figure style="margin:0"><img src="data:image/png;base64,${x.b64}" alt="${esc(r.symbol)} ${esc(x.label)} chart sent to Claude" style="width:100%;height:auto;border:1px solid var(--line);border-radius:8px"><figcaption class="muted" style="font-size:.8rem">${esc(x.label)} — what Claude saw</figcaption></figure>`).join('')}</div>` : ''}
    ${r.status === 'proposed' && !busy ? `<p style="margin:8px 0 0"><button class="btn" data-aicheck="${r.id}">${a ? 'Check again' : 'Ask Claude to check the chart'}</button></p>` : ''}`;
}
S.rvImgs = S.rvImgs || {}; S.rvImgBusy = S.rvImgBusy || {};
function reviewPanel(r) {
  if (isWW(r)) return '';
  if (!['open', 'closing', 'closed'].includes(r.status) || (!r.aiReview && r.status !== 'open')) return '';
  const rv = r.aiReview, busy = S.rvBusy === r.id || r.aiReviewRunning, hist = (r.aiReviews || []).slice(0, -1).reverse();
  const imgs = S.rvImgs[r.id];
  if (rv && !imgs && !S.rvImgBusy[r.id]) setTimeout(() => loadRvImgs(r.id), 0);
  const cfg = (S.bot && S.bot.settings) || {};
  return `<h3 style="font-size:.95rem;margin:16px 0 6px">Claude's end-of-day review <span class="muted" style="font-weight:400;font-size:.82rem">hold overnight or close · 15:40 ET${cfg.aiExitReview === false ? ' · off in settings' : ''}</span></h3>
    ${busy ? '<p class="loading" style="padding:0;margin:0">Claude is reviewing the position… (about 20 seconds)</p>' : ''}
    ${!busy && rv ? `<p style="margin:0 0 6px"><span class="verdict ${rv.action === 'close' ? 'bad' : 'ok'}">${rv.action.toUpperCase()}</span> <span class="muted" style="font-size:.85rem">${rv.confidence}% confidence · ${esc((rv.at || '').replace('T', ' ').slice(5, 16))} ET${rv.plPct != null ? ` · P&L then ${rv.plPct > 0 ? '+' : ''}${rv.plPct}%` : ''}${rv.closed ? ` · <b class="loss">closed by the review${rv.weakHold ? ' (low-confidence hold on a losing position)' : ''}</b>` : ''}</span></p>
      <p style="margin:0 0 6px">${esc(rv.summary)}</p>
      <ul style="list-style:none;padding:0;margin:0;line-height:1.6">${(rv.supports || []).map(x => `<li><b class="gain">✓</b> ${esc(x)}</li>`).join('')}${(rv.concerns || []).map(x => `<li><b class="loss">✗</b> ${esc(x)}</li>`).join('')}</ul>` : ''}
    ${!busy && !rv ? '<p class="muted" style="margin:0">No review yet. It runs every trading day at 15:40 ET for each open position.</p>' : ''}
    ${r.aiReviewError ? `<p class="loss" style="margin:6px 0 0">${esc(r.aiReviewError)}</p>` : ''}
    ${hist.length ? `<details style="margin-top:6px"><summary class="muted" style="cursor:pointer">Earlier reviews (${hist.length})</summary><ul style="list-style:none;padding:0;margin:6px 0 0;line-height:1.6">${hist.map(h => `<li><span class="verdict ${h.action === 'close' ? 'bad' : 'ok'}">${h.action.toUpperCase()}</span> <span class="muted">${esc((h.at || '').replace('T', ' ').slice(5, 16))} · ${h.confidence}%</span> ${esc(h.summary)}</li>`).join('')}</ul></details>` : ''}
    ${imgs && imgs.length ? `<details style="margin-top:6px"><summary class="muted" style="cursor:pointer">Charts Claude reviewed</summary><div style="display:grid;gap:10px;margin-top:8px">${imgs.map(x => `<img src="data:image/png;base64,${x.b64}" alt="${esc(r.symbol)} ${esc(x.label)} chart" style="width:100%;height:auto;border:1px solid var(--line);border-radius:8px">`).join('')}</div></details>` : ''}
    ${r.status === 'open' && !busy ? `<p style="margin:8px 0 0"><button class="btn" data-aireview="${r.id}">Ask Claude now: hold or close?</button></p>` : ''}`;
}
async function loadRvImgs(id) {
  S.rvImgBusy[id] = true;
  try { const r = await api(`/bot/${id}/reviewcharts`); S.rvImgs[id] = r.images || []; if (isBotRoute() || route() === 'gex') render(); } catch (e) { S.rvImgs[id] = []; }
}
async function rvRun(id) {
  S.rvBusy = id; render();
  try { await api(`/bot/${id}/aireview`, { method: 'POST' }); } catch (e) { S.rvBusy = null; render(); toast(e.message); return; }
  for (let i = 0; i < 30; i++) {
    await sleep(4000);
    let rec; try { rec = await api(`/bot/${id}`); } catch (e) { continue; }
    if (rec.aiReviewRunning) continue;
    S.rvBusy = null; delete S.rvImgs[id]; S.rvImgBusy[id] = false; mergeRes(rec); await loadBot(true); render();
    toast(rec.aiReviewError || `Claude: ${rec.aiReview.action} (${rec.aiReview.confidence}%)${rec.aiReview.action === 'close' ? ' — use Close if you agree' : ''}`);
    return;
  }
  S.rvBusy = null; render(); toast('The review is taking longer than usual. Refresh in a minute.');
}
async function loadAiImgs(id) {
  S.aiImgBusy[id] = true;
  try { const r = await api(`/bot/${id}/charts`); S.aiImgs[id] = r.images || []; if (isBotRoute() || route() === 'gex') render(); } catch (e) { S.aiImgs[id] = []; }
}
function mergeRes(rec) {
  if (S.botRes && S.botRes.id === rec.id) S.botRes = { ...S.botRes, ...rec };
  if (S.anaRes && S.anaRes.id === rec.id) S.anaRes = { ...S.anaRes, ...rec };
}
async function aiRun(id, placeAfter) {
  S.aiBusy = id; render();
  try { await api(`/bot/${id}/aicheck`, { method: 'POST' }); } catch (e) { S.aiBusy = null; render(); toast(e.message); return; }
  for (let i = 0; i < 30; i++) {
    await sleep(4000);
    let rec; try { rec = await api(`/bot/${id}`); } catch (e) { continue; }
    if (rec.aiRunning) continue;
    S.aiBusy = null; delete S.aiImgs[id]; S.aiImgBusy[id] = false; mergeRes(rec); render();
    if (rec.aiError) { toast(rec.aiError); return; }
    const v = rec.aiCheck && rec.aiCheck.verdict;
    if (placeAfter && aiPasses(rec.aiCheck)) { placeOrder(id, false, placeAfter); return; }
    toast(placeAfter ? `Claude: ${v}. Not placed — review the chart, or "Place anyway" if you disagree.` : `Claude: ${v}`);
    return;
  }
  S.aiBusy = null; render(); toast('The chart check is taking longer than usual. Refresh in a minute.');
}
function placeOrder(id, override, form) {
  api(`/bot/${id}/order`, { method: 'POST', body: { qty: form.qty, limit: form.limit, override } })
    .then(async r => { mergeRes(r); await loadBot(true); const c = S.bot.settings || {}; toast(`Paper order sent. If it doesn't fill, the limit moves ${c.chaseStep ?? 0.1} every ${c.chaseSeconds ?? 12}s (up to ${c.chaseMaxSteps ?? 5} times).`); setTimeout(() => loadBot(true), 60000); })
    .catch(err => { toast(err.message); render(); });
}
function markLine(r) {
  const m = r.marks, W = 720, H = 160, p = 10, v = m.map(x => x.mark), lo = Math.min(...v, r.fillPrice || Infinity), hi = Math.max(...v, r.fillPrice || -Infinity), sp = hi - lo || 1;
  const X = i => p + i / (m.length - 1) * (W - 2 * p), Y = x => H - p - (x - lo) / sp * (H - 2 * p);
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto" role="img" aria-label="Option value over time">
    ${r.fillPrice ? `<line x1="${p}" x2="${W - p}" y1="${Y(r.fillPrice)}" y2="${Y(r.fillPrice)}" stroke="var(--muted)" stroke-dasharray="4 3"/><text x="${W - p}" y="${Y(r.fillPrice) - 4}" font-size="10.5" text-anchor="end" fill="var(--muted)">entry ${r.fillPrice.toFixed(2)}</text>` : ''}
    <path d="${m.map((x, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(x.mark).toFixed(1)).join(' ')}" fill="none" stroke="var(--coach)" stroke-width="2"/></svg>
    <div class="kv"><span class="muted">${esc(m[0].t.replace('T', ' ').slice(5, 16))}</span><b>${m[m.length - 1].mark.toFixed(2)} (${m[m.length - 1].plPct > 0 ? '+' : ''}${m[m.length - 1].plPct}%) at ${esc(m[m.length - 1].t.slice(11, 16))}</b></div>`;
}
function feedStatus() {
  const st = ((S.bot || {}).state) || {}, cfg = ((S.bot || {}).settings) || {};
  const now = nyTime(Date.now()), hm = now.slice(11, 16), day = now.slice(0, 10);
  const wd = new Date(day + 'T12:00:00').getDay();
  const open = wd >= 1 && wd <= 5 && hm >= '09:35' && hm <= '15:50';
  const t = x => x ? esc(x.replace('T', ' ').slice(x.slice(0, 10) === day ? 11 : 5, 16)) : '—';
  const ago = x => x ? Math.round((Date.parse(now + 'Z') - Date.parse(x + 'Z')) / 60000) : null;
  const err = st.uwErrorAt && (!st.uwOkAt || st.uwErrorAt > st.uwOkAt);
  const stale = open && cfg.flowAuto && (ago(st.lastFlowRun) == null || ago(st.lastFlowRun) > 5);
  const sk = st.lastFlowSkips || {};
  const counts = st.flowDay === day
    ? `Today: ${st.alertsToday || 0} alert${st.alertsToday === 1 ? '' : 's'} over your minimum · ${st.evalsToday || 0} analyzed`
      + (st.lastFlowWindow != null ? `<br>Last check: ${st.lastFlowWindow} alerts in the last ${cfg.flowWindowMin || 60} min on ${st.lastFlowTickers || 0} ticker${st.lastFlowTickers === 1 ? '' : 's'}`
        + ` (${[sk.held ? `${sk.held} already held` : '', sk.recent ? `${sk.recent} analyzed in the last ${cfg.flowCooldownMin || 120} min` : '', sk.waiting ? `${sk.waiting} waiting for the next minute` : ''].filter(Boolean).join(', ') || 'all analyzed'})` : '')
    : 'No flow check yet today';
  const [color, msg] = err ? ['var(--loss)', `<b>Unusual Whales: error</b> at ${t(st.uwErrorAt)} ET — ${esc(st.uwError || '')}`]
    : stale ? ['#b7860b', `<b>No flow check since ${t(st.lastFlowRun)} ET</b> during market hours — the bot may be stopped or the feed slow`]
    : !open ? ['var(--muted)', `<b>Market closed for flow:</b> checks run 9:35–15:50 ET on weekdays · last good pull ${t(st.uwOkAt)} ET`]
    : ['var(--gain)', `<b>Unusual Whales connected</b> · last pull ${t(st.uwOkAt || st.lastFlowRun)} ET (${ago(st.uwOkAt || st.lastFlowRun)} min ago)`];
  return `<div style="display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;margin:8px 0 0;padding:8px 10px;border-radius:8px;background:var(--surface);border-left:4px solid ${color}">
    <span style="flex:1;min-width:220px;font-size:.88rem">${msg}<br><span class="muted">${counts}</span></span>
    <button class="btn" data-uwtest="1" ${S.uwTesting ? 'disabled' : ''}>${S.uwTesting ? 'Testing…' : 'Test the feed now'}</button></div>`;
}
function botHome() {
  const b = S.bot;
  if (!b || b.error || !(b.summary || {}).account) return '';
  const sm = b.summary, ac = sm.account, items = b.items || [];
  const closedT = items.filter(i => i.status === 'closed' && i.fillPrice);
  const rs = closedT.map(i => i.realizedR).filter(v => v != null);
  const avgR = rs.length ? Math.round(rs.reduce((a, c) => a + c, 0) / rs.length * 100) / 100 : null;
  const hist = (sm.equityHistory || []).filter(h => h.equity > 0);
  let peak = 0, dd = 0;
  hist.forEach(h => { peak = Math.max(peak, h.equity); if (peak) dd = Math.min(dd, (h.equity / peak - 1) * 100); });
  const today = nyTime(Date.now()).slice(0, 10);
  const evToday = items.filter(i => (i.createdAt || '').startsWith(today) && !i.orderId).length;
  const card = (k, v, sub, c) => `<div class="period"><h3>${k}</h3><div class="big" ${c != null ? `style="color:${c >= 0 ? 'var(--gain)' : 'var(--loss)'}"` : ''}>${v}</div><p>${sub}</p></div>`;
  return `<section><div class="cal-head"><h2>Paper bot</h2><a href="#bot" class="linkbtn">Open the paper bot</a></div>
    <div class="periods" style="grid-template-columns:repeat(auto-fit,minmax(170px,1fr))">
      ${card('Paper account', money(ac.equity, false), `Today ${money(ac.dayChange)}`, null)}
      ${card('Bot P&L', money(sm.total || 0), `${money(sm.realized || 0)} closed · ${money(sm.unrealized || 0)} open`, sm.total || 0)}
      ${card('Open positions', String(sm.openPositions || 0), `${money(sm.capitalInOpen || 0, false)} invested`, null)}
      ${card('Win rate', sm.winRate != null ? sm.winRate + '%' : '—', `${sm.closedTrades || 0} closed trade${sm.closedTrades === 1 ? '' : 's'}`, null)}
      ${card('Avg result', avgR != null ? `${avgR > 0 ? '+' : ''}${avgR}R` : '—', rs.length ? `per closed trade (${rs.length})` : 'no closed trade with R yet', avgR)}
      ${card('Max drawdown', hist.length > 1 ? `${dd.toFixed(1)}%` : '—', 'from the equity peak', hist.length > 1 && dd < 0 ? dd : null)}
    </div>
    ${hist.length > 1 ? `<section class="panel" style="margin-top:14px"><h2>Paper account equity</h2>${eqLine(hist)}</section>` : ''}
    <p class="muted" style="font-size:.84rem;margin:8px 0 0">${evToday} evaluation${evToday === 1 ? '' : 's'} today · <a href="#botevals">Evaluations</a> · trading <a href="#botstrats"><b>${esc((b.active && b.active.name) || 'Bot rules')}</b>${b.active && b.active.stop ? ` · stop -${b.active.stop}%` : ''}</a></p>
  </section>`;
}
function eqLine(hist) {
  const W = 720, H = 180, p = 10, v = hist.map(h => h.equity), lo = Math.min(...v), hi = Math.max(...v), sp = hi - lo || 1;
  const X = i => p + i / (hist.length - 1) * (W - 2 * p), Y = x => H - p - (x - lo) / sp * (H - 2 * p);
  const up = v[v.length - 1] >= v[0];
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto" role="img" aria-label="Paper account equity"><path d="${hist.map((h, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(h.equity).toFixed(1)).join(' ')}" fill="none" stroke="${up ? 'var(--gain)' : 'var(--loss)'}" stroke-width="2"/></svg>
    <div class="kv"><span class="muted">${esc(fmtDate(hist[0].t))}: ${money(v[0], false)}</span><b class="${cls(v[v.length - 1] - v[0])}">${money(v[v.length - 1] - v[0])} to ${money(v[v.length - 1], false)}</b></div>`;
}
function flowPanel() {
  const f = S.flowF || { minPremium: 100000, type: 'call', minDte: 20, maxDte: 120, askSide: true, sweeps: false, minVolOi: 0, period: 'today', from: '', to: '', ticker: '' };
  const rows = (S.flow && S.flow.alerts) || [];
  return `<section class="panel"><div class="cal-head"><h2>Unusual options flow</h2><span class="muted" style="font-size:.85rem">Unusual Whales flow alerts, newest first</span></div>
    <div class="form-grid" style="align-items:end">
      <div class="field"><label for="fl-prem">Min premium ($)</label><input id="fl-prem" type="number" step="10000" value="${f.minPremium}"></div>
      <div class="field"><label for="fl-type">Type</label><select id="fl-type"><option value="call" ${f.type === 'call' ? 'selected' : ''}>Calls</option><option value="put" ${f.type === 'put' ? 'selected' : ''}>Puts</option><option value="all" ${f.type === 'all' ? 'selected' : ''}>Both</option></select></div>
      <div class="field"><label for="fl-min">Days to expiry, min</label><input id="fl-min" type="number" value="${f.minDte}"></div>
      <div class="field"><label for="fl-max">Days to expiry, max</label><input id="fl-max" type="number" value="${f.maxDte}"></div>
      <div class="field"><label for="fl-voi">Min volume / OI</label><input id="fl-voi" type="number" step="0.1" value="${f.minVolOi || ''}" placeholder="e.g. 1 = volume > OI"></div>
      <div class="field"><label for="fl-tk">Ticker (optional)</label><input id="fl-tk" type="text" style="text-transform:uppercase" value="${esc(f.ticker || '')}"></div>
      <div class="field"><label for="fl-period">Period (Pacific time, midnight to 11:59 PM)</label><select id="fl-period">${[['today', 'Today'], ['yesterday', 'Yesterday'], ['7d', 'Last 7 days'], ['custom', 'Custom']].map(([v, l]) => `<option value="${v}" ${f.period === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
      ${f.period === 'custom' ? `<div class="field"><label for="fl-from">From</label><input id="fl-from" type="date" value="${esc(f.from || '')}"></div><div class="field"><label for="fl-to">To</label><input id="fl-to" type="date" value="${esc(f.to || '')}"></div>` : ''}
      <div class="field"><label><input type="checkbox" id="fl-etf" ${f.noEtf ? 'checked' : ''}> Hide ETFs</label><label><input type="checkbox" id="fl-ask" ${f.askSide ? 'checked' : ''}> Mostly bought at the ask</label><label><input type="checkbox" id="fl-sweep" ${f.sweeps ? 'checked' : ''}> Sweeps only</label></div>
      <div class="field"><button class="btn primary" id="fl-load" ${S.flowBusy ? 'disabled' : ''}>${S.flowBusy ? 'Loading…' : 'Load flow'}</button>
        <label style="margin-top:6px"><input type="checkbox" id="fl-auto" ${S.flowAuto !== false ? 'checked' : ''}> Refresh every minute</label></div></div>
    ${S.flow && S.flowAt ? `<p class="muted" style="font-size:.82rem;margin:0 0 4px">Prices as of ${esc(S.flowAt)} (your time). "Now" prices are the live option mid and last stock trade.</p>` : ''}
    ${S.flowErr ? `<div class="errbox">${esc(S.flowErr)}</div>` : ''}
    ${S.flow && S.flow.fetched ? `<p class="muted" style="font-size:.84rem;margin:4px 0">Unusual Whales returned ${S.flow.fetched} alerts matching your filters, from ${esc(S.flow.returnedFrom || '?')} to ${esc(S.flow.returnedTo || '?')} ET${S.flow.stale ? `; ${S.flow.stale} fall outside ${esc(S.flow.window || '')} and are not shown` : ''}.${S.flow.stale && !(S.flow.alerts || []).length ? ' If the newest is from a previous day, no alert matching these filters has printed yet today: loosen the filters or choose a longer period.' : ''}</p>` : ''}
    ${S.flow && rows.length ? `<p style="margin:4px 0 10px"><b>${rows.length}</b> alerts · ${money(S.flow.totalPremium, false)} premium, ${esc(S.flow.window || '')}${(S.flow.topTickers || []).length ? ` · Top: ${S.flow.topTickers.map(t => `<button class="toggle emo" data-rescan="${esc(t.ticker)}" title="Evaluate ${esc(t.ticker)}">${esc(t.ticker)} ${t.alerts}× ${money(t.premium, false)}</button>`).join(' ')}` : ''}</p>` : ''}
    ${S.flow ? (rows.length ? `<div class="tablewrap" style="border:0"><table><thead><tr><th>Time (ET)</th><th>Ticker</th><th>Contract</th><th class="r">Premium</th><th class="r">At ask</th><th class="r">Vol / OI</th><th class="r">Option at alert</th><th class="r">Option now</th><th class="r">Stock at alert</th><th class="r">Stock now</th><th></th></tr></thead><tbody>
      ${rows.map(a => `<tr><td>${esc((a.atEt || '').slice(0, 10) === new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' }) ? (a.atEt || '').slice(11) : (a.atEt || '').slice(5))}</td><td><b>${esc(a.ticker)}</b></td><td>${esc(fmtExp(a.expiry))} ${a.strike} ${a.type}${a.sweep ? ' <span class="chip">sweep</span>' : ''} <span class="muted">(${a.dte}d)</span></td>
        <td class="r">${money(a.premium, false)}</td><td class="r">${a.askPct}%</td><td class="r">${a.volOi}</td><td class="r">${a.price.toFixed(2)}</td>
        <td class="r">${a.nowPrice != null ? `${a.nowPrice.toFixed(2)} <span class="${cls(a.nowChangePct)}" style="font-size:.84rem">${a.nowChangePct != null ? (a.nowChangePct > 0 ? '+' : '') + a.nowChangePct + '%' : ''}</span>` : '—'}</td>
        <td class="r">${a.underlying ? a.underlying.toFixed(2) : '—'}</td>
        <td class="r">${a.nowStock != null ? `${a.nowStock.toFixed(2)} <span class="${cls(a.stockChangePct)}" style="font-size:.84rem">${a.stockChangePct != null ? (a.stockChangePct > 0 ? '+' : '') + a.stockChangePct + '%' : ''}</span>` : '—'}</td>
        <td class="r" style="white-space:nowrap"><button class="btn" data-rescan="${esc(a.ticker)}">Evaluate</button></td></tr>`).join('')}</tbody></table></div>`
      : `<p class="muted">No alerts match these filters for ${esc(S.flow.window || '')}.</p>`) : ''}</section>`;
}
async function loadFlow() {
  S.flowF = { minPremium: +$('#fl-prem').value || 0, type: $('#fl-type').value, minDte: +$('#fl-min').value || 0, maxDte: +$('#fl-max').value || 120, askSide: $('#fl-ask').checked, sweeps: $('#fl-sweep').checked,
    minVolOi: +$('#fl-voi').value || 0, period: $('#fl-period').value, from: $('#fl-from') ? $('#fl-from').value : '', to: $('#fl-to') ? $('#fl-to').value : '', ticker: ($('#fl-tk').value || '').trim().toUpperCase(), noEtf: $('#fl-etf').checked };
  S.flowBusy = true; S.flowErr = null; render();
  const f = S.flowF;
  S.flowAt = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  try { S.flow = await api(`/flow?minPremium=${f.minPremium}&type=${f.type}&minDte=${f.minDte}&maxDte=${f.maxDte}&askSide=${f.askSide ? 1 : 0}&sweeps=${f.sweeps ? 1 : 0}&minVolOi=${f.minVolOi}&period=${f.period}${f.period === 'custom' ? `&from=${f.from}&to=${f.to || f.from}` : ''}&tz=America/Los_Angeles${f.noEtf ? '&noEtf=1' : ''}${f.ticker ? '&ticker=' + encodeURIComponent(f.ticker) : ''}`); }
  catch (e) { S.flowErr = e.message; }
  S.flowBusy = false; if (route() === 'gex') render();
}
function vBotEvals() { return vBot('evals'); }
/* ---------- call bursts (tracking only) ---------- */
async function loadBursts() {
  S.burstsLoading = true;
  try { S.bursts = await api(`/bot/bursts?days=${S.burstDays || 10}`); } catch (e) { S.bursts = { error: e.message }; }
  S.burstsLoading = false;
  const st = S.bursts && S.bursts.study;
  if (st && ['starting', 'running', 'partial'].includes(st.status)) {
    clearTimeout(S.burstPoll);
    S.burstPoll = setTimeout(async () => {
      if (route() !== 'botbursts') return;
      if (S.bursts.study.status === 'partial' && !S.burstCont) {       // keep the replay going, part after part
        S.burstCont = true;
        try { await api('/bot/bursts/replay', { method: 'POST', body: {} }); } catch (e) {}
        S.burstCont = false;
      }
      loadBursts();
    }, 6000);
  }
  if (route() === 'botbursts') render();
}
const bpct = v => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}%</span>`;
function burstLine(e) {
  const tags = [e.move != null && e.move <= 0 ? 'into weakness' : e.move != null && e.move >= 1 ? 'chasing' : '', e.tod !== 'midday' ? `at the ${e.tod}` : '', e.contracts >= 5 ? `${e.contracts} contracts` : ''].filter(Boolean);
  return `<li data-evticker="${esc(e.ticker)}">
    <span class="shl-a"><b>${esc(e.ticker)}</b> <span class="muted">${esc((e.date || '').slice(5))} ${esc(e.start)}–${esc(e.end)}</span></span>
    <span class="shl-r"><b>${money(e.cp, false)}</b>${e.ratio != null ? ` <span class="muted">${e.ratio}×</span>` : ''}</span>
    <span class="shl-b muted">${e.n} call prints (avg ${money(Math.round(e.cp / Math.max(1, e.n)), false)}, largest ${money(e.maxPrint || 0, false)}) · ${Math.round(e.callShare * 100)}% calls · price ${e.move == null ? '—' : (e.move > 0 ? '+' : '') + e.move + '%'} during${tags.length ? ' · ' + esc(tags.join(' · ')) : ''}</span>
    <span class="shl-b">${['fwd1', 'fwd3', 'fwd5'].map((k, i) => `<span class="muted">+${[1, 3, 5][i]}d</span> ${bpct(e[k])}`).join(' &nbsp; ')}</span></li>`;
}
function burstGroups(evs, key) {
  // one block per ticker, the ticker with the largest burst first; inside, largest burst first
  const g = {};
  (evs || []).forEach(e => { (g[e.ticker] = g[e.ticker] || []).push(e); });
  const list = Object.entries(g).map(([t, xs]) => ({ t, xs: xs.sort((a, b) => b.cp - a.cp) }))
    .sort((a, b) => b.xs[0].cp - a.xs[0].cp);
  if (!list.length) return '';
  return `<div class="bgrp">${list.map(({ t, xs }) => {
    const tot = xs.reduce((s2, e) => s2 + e.cp, 0), top = xs[0];
    const open = S.burstOpen && S.burstOpen[key + t];
    return `<div class="bgrp-i"><div class="bgrp-h" data-burstgrp="${esc(key + t)}">
        <span><b>${esc(t)}</b> <span class="muted">${xs.length} burst${xs.length > 1 ? 's' : ''} · ${money(tot, false)} total</span></span>
        <span style="text-align:right"><b>${money(top.cp, false)}</b>${top.ratio != null ? ` <span class="muted">${top.ratio}×</span>` : ''} <span class="muted">${open ? '▾' : '▸'}</span></span>
        <span class="muted" style="grid-column:1/3;font-size:.8rem">largest ${esc((top.date || '').slice(5))} ${esc(top.start)}–${esc(top.end)} · ${top.n} prints${top.move != null ? ` · price ${top.move > 0 ? '+' : ''}${top.move}% during` : ''}${top.fwd5 != null ? ` · +5d ${top.fwd5 > 0 ? '+' : ''}${top.fwd5}%` : top.fwd1 != null ? ` · +1d ${top.fwd1 > 0 ? '+' : ''}${top.fwd1}%` : ''}</span></div>
      ${open ? `<ul class="shl" style="margin:6px 0 4px">${xs.map(burstLine).join('')}</ul><p style="margin:0 0 6px"><a href="#botevals?t=${encodeURIComponent(t)}" class="linkbtn">Open ${esc(t)} (evaluations and flow chart) ›</a></p>` : ''}</div>`;
  }).join('')}</div>`;
}
function vBursts() {
  const d = S.bursts;
  const head0 = head('Call bursts', 'Tracking only: clusters of calls bought at the ask, much bigger than the ticker’s normal flow, within 30 minutes. Recorded and followed, never traded.', '');
  if (!d) return head0 + '<p class="loading">Loading…</p>';
  if (d.error) return head0 + `<div class="errbox">${esc(d.error)}</div>`;
  const c = d.cfg || {}, lv = d.live || {}, st = d.study;
  const today = nyTime(Date.now()).slice(0, 10);
  const thr = `${money(c.burstMinPremium, false)}+ of calls in 30 min, ${c.burstMinRatio}× the ticker's normal, ${c.burstMinPrints}+ prints, ${Math.round(c.burstMinCallShare * 100)}%+ calls`;
  const live = `<section class="panel"><h2>Live</h2>
    <p style="margin:0 0 6px">${lv.lastRun ? `Last scan ${esc(lv.lastRun.replace('T', ' ').slice(5, 16))} ET · ${lv.today === today ? lv.burstsToday || 0 : 0} burst${(lv.burstsToday || 0) === 1 && lv.today === today ? '' : 's'} today` : 'Not running yet: live tracking runs every minute with flow trading on (9:35–15:50 ET).'}</p>
    <p class="muted" style="font-size:.84rem;margin:0">A burst = ${esc(thr)}. One per ticker every ${c.burstCooldownMin} min. Results 1, 3 and 5 trading days later are filled in after each close.</p>
    <details style="margin-top:8px" ${S.burstEdit ? 'open' : ''}><summary class="muted" style="cursor:pointer">Change what counts as a burst</summary>
      <div class="form-grid" style="margin-top:8px;align-items:end">
        <div class="field"><label for="bu-prem">Call premium in 30 min ($)</label><input id="bu-prem" type="number" step="50000" value="${c.burstMinPremium}"></div>
        <div class="field"><label for="bu-n">Call prints in 30 min</label><input id="bu-n" type="number" step="1" value="${c.burstMinPrints}"></div>
        <div class="field"><label for="bu-r">× the ticker's normal</label><input id="bu-r" type="number" step="0.5" value="${c.burstMinRatio}"></div>
        <div class="field"><label for="bu-s">Min % calls (vs puts)</label><input id="bu-s" type="number" step="5" value="${Math.round(c.burstMinCallShare * 100)}"></div>
        <div class="field"><button class="btn coachbtn" data-burstsave="1">Save</button></div></div>
      <p class="muted" style="font-size:.8rem;margin:4px 0 0">Applies to live tracking from the next minute, and re-sorts the 30-day replay right away (clusters from $100k and 3 prints are kept, so lowering the thresholds works without running it again).</p></details>
    ${(d.events || []).length ? `<div style="margin-top:10px">${burstGroups(d.events, 'live:')}</div>` : `<p class="muted" style="margin:10px 0 0">No burst in the last ${d.days} days yet.</p>`}</section>`;
  const R = st && st.status;
  const btn = `<button class="btn" data-burstrun="1" ${['starting', 'running', 'partial'].includes(R) ? 'disabled' : ''}>${!st ? 'Run the 30-day replay' : ['starting', 'running', 'partial'].includes(R) ? `Replaying… ${st.done || 0}/${st.total || '?'} tickers` : 'Run it again'}</button>`;
  const grow = g => `<tr><td class="shg-l">${esc(g.label)}</td><td class="r" data-l="trades">${g.n}</td><td class="r" data-l="d1">${bpct(g.fwd1)}</td><td class="r" data-l="d3">${bpct(g.fwd3)}</td><td class="r" data-l="d5">${bpct(g.fwd5)}</td><td class="r" data-l="w5">${g.win5 == null ? '—' : g.win5 + '%'}</td></tr>`;
  const gth = t => `<thead><tr><th>${esc(t)}</th><th class="r">Bursts</th><th class="r">+1 day</th><th class="r">+3 days</th><th class="r">+5 days</th><th class="r">Up after 5 days</th></tr></thead>`;
  const card = (k, g, sub) => `<div class="stat"><span>${k}</span><b>${g ? bpct(g.fwd5) : '—'}</b><span>${sub}</span></div>`;
  const study = `<section class="panel" style="margin-top:14px"><div class="cal-head"><h2>30-day replay</h2>${btn}</div>
    <p class="muted" style="font-size:.84rem;margin:0 0 10px">Replays the last 30 days of flow on every ticker the bot evaluated, finds the same clusters, and measures the stock 1, 3 and 5 trading days later against a normal day on the same tickers. A wider net (clusters from $100k and 3 prints, 1.5× normal) is included so the thresholds themselves can be judged.</p>
    ${!st ? '' : !st.all ? `<p class="muted">${R === 'done' ? 'No cluster found.' : 'Working…'}${st.lastError ? ` Last error: ${esc(st.lastError)}` : ''}</p>` : `
    <div class="strip" style="margin-bottom:12px">
      ${card('Bursts (your thresholds): +5 days', st.strict, st.strict ? `${st.strict.n} bursts · +1d ${(st.strict.fwd1 ?? '—')}% · ${st.strict.win5 ?? '—'}% up` : 'none found')}
      ${card('All clusters (wide net): +5 days', st.all, `${st.all.n} clusters · ${st.all.win5 ?? '—'}% up`)}
      <div class="stat"><span>Normal day, same tickers: +5 days</span><b>${bpct(st.baseline.fwd5)}</b><span>+1d ${st.baseline.fwd1 ?? '—'}% · +3d ${st.baseline.fwd3 ?? '—'}%</span></div>
    </div>
    <p class="muted" style="font-size:.84rem;margin:0 0 10px">The edge is the difference with the normal day. Small groups (under ~30) are noise.</p>
    ${st.groups.map(g => `<div class="tablewrap" style="margin-top:10px"><table class="shg bst">${gth(g.title)}<tbody>${g.rows.map(grow).join('')}</tbody></table></div>`).join('')}
    ${(st.top || []).length ? `<h3 style="font-size:.95rem;margin:14px 0 6px">Bursts found (your thresholds), by ticker, largest first</h3>${burstGroups(st.top, 'replay:')}` : ''}`}
    ${st && st.updatedAt ? `<p class="muted" style="font-size:.8rem;margin:10px 0 0">${R === 'done' ? 'Finished' : 'Updated'} ${esc(st.updatedAt.replace('T', ' ').slice(5, 16))} ET${st.errors ? ` · ${st.errors} ticker${st.errors > 1 ? 's' : ''} skipped (data errors)` : ''}</p>` : ''}
  </section>`;
  const errs = (d.errors || []).length ? `<div class="errbox" style="margin-bottom:12px">Part of this page couldn't load: ${d.errors.map(esc).join(' · ')}</div>` : '';
  return head0 + errs + live + study;
}
/* ---------- explorer: mix and match conditions over every taken and skipped idea ---------- */
S.exConds = S.exConds || [];
async function runExplore(search) {
  S.exBusy = search ? 'search' : 'filter'; render();
  try {
    const r = await api('/bot/explore', { method: 'POST', body: { days: S.exDays || 35, conds: S.exConds, search, minN: S.exMinN || 15, group: S.exGroup || '', depth: S.exDepth || 4 } });
    if (!search && S.ex && S.ex.search) r.search = S.ex.search;
    S.ex = r;
  } catch (e) { S.ex = { error: e.message }; }
  S.exBusy = null;
  if (route() === 'botexplore') render();
}
async function runMC() {
  S.mcBusy = true; render();
  const num = id => { const v = parseFloat(($(id) || {}).value); return isNaN(v) ? null : v; };
  S.mcIn = { trades: num('#mc-n') || 100, risk: num('#mc-risk'), account: num('#mc-acct') };
  try { S.mc = await api('/bot/montecarlo', { method: 'POST', body: { days: S.exDays || 35, conds: S.exConds, ...S.mcIn } }); }
  catch (e) { S.mc = { error: e.message }; }
  S.mcBusy = false; if (route() === 'botexplore') render();
}
function mcFan(m) {
  const f = m.fan, W = 600, H = 260, L = 46, Rr = 10, T = 10, B = 230;
  const lo = Math.min(0, ...f.map(x => x.p5)), hi = Math.max(0, ...f.map(x => x.p95));
  const X = t => L + t / m.trades * (W - L - Rr), Y = v => T + (hi - v) / ((hi - lo) || 1) * (B - T);
  const band = (a, b2) => `M${f.map(x => `${X(x.trade)},${Y(x[a])}`).join('L')}L${[...f].reverse().map(x => `${X(x.trade)},${Y(x[b2])}`).join('L')}Z`;
  let g = '';
  for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4; g += `<line x1="${L}" x2="${W - Rr}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${L - 6}" y="${Y(v) + 4}" font-size="12" text-anchor="end" fill="var(--muted)">${v.toFixed(0)}R</text>`; }
  g += `<line x1="${L}" x2="${W - Rr}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--muted)" stroke-dasharray="4 3"/>`;
  g += `<path d="${band('p5', 'p95')}" fill="var(--coach,#5b5bd6)" fill-opacity=".12"/><path d="${band('p25', 'p75')}" fill="var(--coach,#5b5bd6)" fill-opacity=".22"/>`;
  g += `<path d="M${f.map(x => `${X(x.trade)},${Y(x.p50)}`).join('L')}" fill="none" stroke="var(--coach,#5b5bd6)" stroke-width="2.5"/>`;
  g += `<text x="${W - Rr}" y="${B + 22}" font-size="12" text-anchor="end" fill="var(--muted)">${m.trades} trades</text><text x="${L}" y="${B + 22}" font-size="12" fill="var(--muted)">0</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block" role="img" aria-label="Simulated equity in R over the next trades">${g}</svg>`;
}
function mcHist(m) {
  const h = m.hist, W = 600, H = 140, mx = Math.max(...h.counts), bw = (W - 20) / h.counts.length;
  let g = '';
  h.counts.forEach((c, i) => { const v0 = h.from + i * h.width, x = 10 + i * bw, hh = c / mx * 110;
    g += `<rect x="${x + 1}" y="${120 - hh}" width="${bw - 2}" height="${hh}" fill="${v0 + h.width / 2 >= 0 ? 'var(--gain)' : 'var(--loss)'}" fill-opacity=".7"/>`; });
  g += `<text x="10" y="136" font-size="12" fill="var(--muted)">${h.from.toFixed(0)}R</text><text x="${W - 10}" y="136" font-size="12" text-anchor="end" fill="var(--muted)">${(h.from + h.width * h.counts.length).toFixed(0)}R</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block" role="img" aria-label="Distribution of the total result">${g}</svg>`;
}
function mcPanel() {
  const m = S.mc, i = S.mcIn || {};
  const R = v => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${sgn(v, 1)}R</span>`;
  const form = `<div class="form-grid" style="align-items:end">
      <div class="field"><label for="mc-n">Future trades to simulate</label><input id="mc-n" type="number" value="${i.trades || 100}" min="10" max="500"></div>
      <div class="field"><label for="mc-risk">Risk per trade ($, 1R)</label><input id="mc-risk" type="number" value="${i.risk ?? ''}" placeholder="from your settings"></div>
      <div class="field"><label for="mc-acct">Account size ($)</label><input id="mc-acct" type="number" value="${i.account ?? ''}" placeholder="from your settings"></div>
      <div class="field"><button class="btn coachbtn" data-mcrun="1" ${S.mcBusy ? 'disabled' : ''}>${S.mcBusy ? 'Simulating…' : 'Run Monte Carlo'}</button></div></div>`;
  let out = '';
  if (m && m.error) out = `<p class="muted">${esc(m.error)}</p>`;
  else if (m) {
    const d = m.dollars;
    out = `${m.note ? `<p class="muted" style="font-size:.84rem">${esc(m.note)}</p>` : ''}
      <div class="strip" style="margin:10px 0">
        <div class="stat"><span>After ${m.trades} trades (median)</span><b>${R(m.total.p50)}</b><span>5–95%: ${sgn(m.total.p5, 1)} to ${sgn(m.total.p95, 1)}R${d ? ` · ${money(d.p50)}` : ''}</span></div>
        <div class="stat"><span>Chance of finishing up</span><b>${m.probPositive}%</b><span>from ${m.n} results · avg ${sgn(m.avgR, 2)}R · ${m.winPct}% winners</span></div>
        <div class="stat"><span>Worst drop from a peak</span><b class="loss">${m.drawdown.p50}R</b><span>1 in 20 runs: ${m.drawdown.p95}R${d ? ` (${money(d.dd95)})` : ''}</span></div>
        <div class="stat"><span>Longest losing streak</span><b>${m.losingStreak.p50}</b><span>1 in 20 runs: ${m.losingStreak.p95} in a row</span></div>
      </div>
      <h3 style="font-size:.95rem;margin:12px 0 4px">Equity in R over the next ${m.trades} trades <span class="muted" style="font-weight:400;font-size:.82rem">line = median · dark band = middle 50% · light band = 90% of runs</span></h3>
      <div style="border:1px solid var(--line);border-radius:10px;padding:6px;background:var(--surface)">${mcFan(m)}</div>
      <h3 style="font-size:.95rem;margin:12px 0 4px">Where ${m.runs.toLocaleString()} runs ended</h3>
      <div style="border:1px solid var(--line);border-radius:10px;padding:6px;background:var(--surface)">${mcHist(m)}</div>
      <p style="margin:10px 0 0;font-size:.9rem">${m.randomAsGood == null ? '' : `<b>Better than chance?</b> ${m.randomAsGood}% of random groups of ${m.n} ideas did at least as well (${m.randomAsGood <= 5 ? '<span class="gain">unlikely to be luck</span>' : m.randomAsGood <= 20 ? 'promising, not proven' : '<span class="loss">could easily be luck</span>'}).`}
      ${d && d.halfAccountPct != null ? ` Chance of a drop of half the ${money(d.account, false)} account: <b>${d.halfAccountPct}%</b> at ${money(d.risk, false)} per trade.` : ''}</p>
      <p class="muted" style="font-size:.8rem;margin:6px 0 0">Resamples these ideas' actual results ${m.runs.toLocaleString()} times. It assumes the future looks like this sample: market conditions change, so read it as a range, not a forecast.</p>`;
  }
  return `<section class="panel" style="margin-top:14px"><h2>Monte Carlo</h2>
    <p class="muted" style="font-size:.84rem;margin:0 0 8px">Simulates the next trades by drawing from the results of the ideas matching your conditions above (${(S.exConds || []).length ? 'your filter' : 'all ideas'}).</p>${form}${out}</section>`;
}
/* ---------- best exit + strategies ---------- */
async function prepPaths() {
  S.eoPrep = true; render();
  let before = null;
  try { before = ((await api('/bot/shadow/summary?days=5')).run || {}).at; await api('/bot/shadow/run', { method: 'POST' }); }
  catch (e) { S.eoPrep = false; render(); toast(e.message); return; }
  for (let i = 0; i < 60; i++) {
    await sleep(5000);
    try { const r = await api('/bot/shadow/summary?days=5'); if (r.run && r.run.status !== 'running' && r.run.at !== before) break; } catch (e) {}
  }
  S.eoPrep = false; S.shadowSum = null; await runExitOpt();
}
async function runExitOpt() {
  S.eoBusy = true; render();
  try { S.eo = await api('/bot/exitopt', { method: 'POST', body: { days: S.exDays || 35, conds: S.exConds, minN: S.exMinN || 15, spreadMult: S.eoMult ?? 1 } }); }
  catch (e) { S.eo = { error: e.message }; }
  S.eoBusy = false; if (route() === 'botexplore') render();
}
function eoPanel() {
  const d = S.eo;
  const R = v => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${sgn(v, 2)}R</span>`;
  let body = '';
  if (d && d.error) body = `<p class="muted">${esc(d.error)}</p>${/price path/.test(d.error) ? `<p><button class="btn coachbtn" data-eoprep="1" ${S.eoPrep ? 'disabled' : ''}>${S.eoPrep ? 'Saving the price paths… (1–3 min)' : 'Save the price paths now'}</button></p>` : ''}`;
  else if (d) {
    const row = (x, i) => `<li ${i != null ? `data-eouse="${i}"` : ''} class="${S.exExit && i != null && JSON.stringify(S.exExit) === JSON.stringify(x.params) ? 'sel' : ''}"><span class="shl-a">${esc(x.text)}</span><span class="shl-r">${R(x.avgR)}</span>
      <span class="shl-b muted">${x.n} ideas · ${x.winPct}% winners · total ${x.totalR}R · held ${x.avgDays} days on average · older ${x.older ?? '—'}R / newer ${x.newer ?? '—'}R${x.holds ? ' · <b class="gain">better than now in both halves</b>' : ''}${x.tight ? ' · <b class="loss">stop under 2× the spread: fragile</b>' : ''}</span></li>`;
    body = `<p class="muted" style="font-size:.84rem;margin:0 0 8px">Includes the bid/ask spread${d.spreadMult !== 1 ? ` ×${d.spreadMult}` : ''}: you pay up on entry and get the bid on exit (average spread ${d.avgSpread}% of the option price; known for ${d.spreadKnown} of ${d.n} ideas, 10% assumed for the rest).</p>
      <p style="margin:0 0 6px"><b>Your current exit rules</b></p><ul class="shl">${row(d.current, null)}</ul>
      <p style="margin:12px 0 6px"><b>Best of ${d.tested} exit rule sets</b> <span class="muted">tap one to use it in a strategy</span></p><ul class="shl">${d.results.map(row).join('')}</ul>`;
  }
  return `<section class="panel" style="margin-top:14px"><div class="cal-head"><h2>Best exit</h2>
    <span style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><label class="muted" style="font-size:.85rem">Spread cost <select id="eo-mult">${[[1, 'as quoted'], [1.5, '×1.5'], [2, '×2 (worst fills)'], [0, 'none']].map(([v, l]) => `<option value="${v}" ${(S.eoMult ?? 1) === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
    <button class="btn coachbtn" data-eorun="1" ${S.eoBusy ? 'disabled' : ''}>${S.eoBusy ? 'Replaying…' : 'Find the best exit for these ideas'}</button></span></div>
    <p class="muted" style="font-size:.84rem;margin:0 0 8px">Replays every combination of stop (20–60%), target (none, +40% to +150%), sell vs. trail after the target (15/25/35%), maximum hold (5, 10 or 20 days) and the invalidation exit on each matching idea's stored daily option prices, and ranks them by average R. Risk is the stop, so a wider stop must earn more to rank higher.</p>${body}</section>`;
}
async function saveStrategy() {
  const name = prompt('Name this strategy', (S.ex.conds || []).map(c => c.text).join(' + ').slice(0, 60));
  if (name === null) return;
  const m = S.ex.match;
  try {
    await api('/bot/strategies', { method: 'POST', body: { name, conds: S.exConds, mode: 'watch', exit: S.exExit || null,
      backtest: { n: m.n, avgR: m.avgR, winPct: m.winPct, days: S.exDays || 35, at: nyTime(Date.now()) } } });
    toast('Saved in Strategies (watching). Turn on Trade there when you trust it.'); S.strats = null;
    location.hash = '#botstrats';
  } catch (e) { toast(e.message); }
}
async function loadStrats() {
  S.stratsBusy = true;
  try { S.strats = await api('/bot/strategies'); } catch (e) { S.strats = { error: e.message }; }
  S.stratsBusy = false; if (route() === 'botstrats') render();
}
function exitText(e) {
  if (!e) return 'Bot settings';
  return [`stop -${e.stop}%`, e.target == null ? 'no target' : e.trail ? `at +${e.target}% trail ${e.trail}%` : `sell at +${e.target}%`,
    e.maxDays ? `max ${e.maxDays} days` : 'up to the time stop', e.invalidation === false ? 'no invalidation exit' : 'invalidation on'].join(' · ');
}
function vStrats() {
  const h = head('Strategies', 'Combinations saved from the Explorer. Watch: new evaluations that match are tagged and followed. Trade: a match buys even if some standard rules failed (safety rules and Claude still apply).', '');
  const d = S.strats;
  if (!d) return h + '<p class="loading">Loading…</p>';
  if (d.error) return h + `<div class="errbox">${esc(d.error)}</div>`;
  const act = d.active || { id: '', stop: null }, setStop = (S.bot && S.bot.settings && S.bot.settings.stopPct) || 40;
  const actStrat = d.items.find(x => x.id === act.id);
  const defStop = actStrat && actStrat.exit ? `${actStrat.exit.stop}% (the strategy's exit)` : `${setStop}% (Settings)`;
  const picker = `<section class="panel" style="margin-bottom:12px"><h2>What the bot trades</h2>
    <div class="form-grid">
      <label>Strategy<select id="act-strat"><option value="">Bot rules (default)</option>${d.items.map(x => `<option value="${x.id}" ${x.id === act.id ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select></label>
      <label>Stop<select id="act-stop"><option value="">${esc(defStop)}</option>${(d.stops || []).map(v => `<option value="${v}" ${act.stop === v ? 'selected' : ''}>-${v}% on the option</option>`).join('')}</select></label>
    </div>
    <p style="margin:10px 0 0"><button class="btn primary" data-actsave="1">Apply</button></p>
    <p class="muted" style="font-size:.84rem;margin:8px 0 0">Now: <b>${esc(act.name || 'Bot rules')}</b> · stop <b>${act.stop ? '-' + act.stop + '%' : esc(defStop)}</b>.
      ${act.id ? 'The bot trades this strategy and nothing else: every match is bought, no match is never bought. The bot\'s other rules (chart checks, earnings, puts, liquidity, size, Claude\'s check) are ignored, and only the strategy\'s exit closes the trade (its stop, target / trail, max days and invalidation if set). The bot still needs a call in your expiration / delta window and a free slot under Max positions.' : 'The bot buys on its own rules; strategies set to Trade can also buy their matches.'}
      The stop is locked into each trade when proposed; changing it later doesn't move the stop on open trades.</p></section>`;
  if (!d.items.length) return h + picker + '<section class="panel"><p class="muted" style="margin:0">No strategy yet. In the Explorer, combine conditions, optionally pick an exit in <b>Best exit</b>, then press <b>Save as strategy</b>.</p></section>';
  const R = v => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${sgn(v, 2)}R</span>`;
  return h + picker + d.items.map(x => { const f = x.forward, b = x.backtest || {};
    return `<section class="panel" style="margin-bottom:12px${x.id === act.id ? ';border:2px solid var(--accent, #3b82f6)' : ''}"><div class="cal-head"><h2 style="font-size:1.05rem">${esc(x.name)}${x.id === act.id ? ' <span class="exchip">ACTIVE</span>' : ''}</h2>
      <span class="seg">${['watch', 'trade', 'off'].map(m => `<button class="btn ${x.mode === m ? 'coachbtn' : ''}" data-stmode="${x.id}|${m}">${m === 'watch' ? 'Watch' : m === 'trade' ? 'Trade' : 'Off'}</button>`).join('')}</span></div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px">${x.conds.map(c => `<span class="exchip">${esc(c.text || (c.key + (c.op ? ' ' + c.op + ' ' + c.value : c.value === false ? ': no' : '')))}</span>`).join('')}</div>
      <p class="muted" style="font-size:.86rem;margin:0 0 8px">Exit: ${esc(exitText(x.exit))} · saved ${esc((x.createdAt || '').slice(0, 10))}${b.n ? ` · backtest when saved: ${b.n} ideas, ${b.avgR > 0 ? '+' : ''}${b.avgR}R avg, ${b.winPct}% winners (last ${b.days} days)` : ''}</p>
      <div class="strip"><div class="stat"><span>Since saved (forward only)</span><b>${R(f.avgR)}</b><span>${f.n} ideas · ${f.done} finished · ${f.winPct ?? '—'}% winners</span></div>
        <div class="stat"><span>Total</span><b>${f.totalR == null ? '—' : sgn(f.totalR, 2) + 'R'}</b><span>${x.tagged} evaluation${x.tagged === 1 ? '' : 's'} tagged</span></div></div>
      ${x.items.length ? `<details style="margin-top:8px"><summary class="muted" style="cursor:pointer">Latest matching ideas</summary><ul class="shl" style="margin-top:6px">${x.items.map(i => `<li data-shopen="${i.id}"><span class="shl-a"><b>${esc(i.symbol)}</b> <span class="muted">${esc(i.at.replace('T', ' ').slice(5, 16))}${i.taken ? ' · taken' : ''}</span></span><span class="shl-r">${R(i.R)}${i.status !== 'done' ? ' <span class="muted">open</span>' : ''}</span></li>`).join('')}</ul></details>` : '<p class="muted" style="font-size:.84rem;margin:8px 0 0">No new evaluation has matched yet.</p>'}
      <p style="margin:10px 0 0"><button class="btn" data-stload="${x.id}">Open in Explorer</button> <button class="btn" data-stdel="${x.id}">Delete</button></p></section>`; }).join('')
    + '<p class="muted" style="font-size:.82rem">Trade mode (with Bot rules selected above): a match can buy even if chart rules failed, but liquidity, earnings, puts, size and Claude still apply. To trade a strategy alone with nothing on top, select it above.</p>';
}
/* ---------- Wall watch: CW > flip > PW + green hourly candle crossing the put wall ---------- */
async function loadWatch() {
  if (S.wwBusy) return; S.wwBusy = true;
  try { S.ww = await api('/bot/wallwatch'); } catch (e) { if (!S.ww) S.ww = { error: e.message }; }
  S.wwBusy = false;
  if (['botwatch', 'botchart', 'botlab'].includes(route())) render();
}
function wwFillTxt() {
  const f = (S.ww && S.ww.fill) || {};
  if (f.running || S.wwFilling) return `<span class="muted" style="font-size:.82rem">getting walls… ${f.total ? `${f.total - (f.left ?? f.total)} / ${f.total} days` : ''}</span>`;
  if (f.total && f.left) return `<span class="muted" style="font-size:.82rem">walls ${f.total - f.left} / ${f.total} days</span>`;
  if (f.total && !f.left) return `<span class="muted" style="font-size:.82rem">all walls stored ${esc((f.at || '').slice(5, 16).replace('T', ' '))}</span>`;
  return '';
}
const wwFillBtn = () => `<button class="btn coachbtn" data-wwfill="1" ${S.wwFilling ? 'disabled' : ''} style="font-size:.8rem;padding:2px 10px" title="Fetch the call wall, put wall and flip of the last 45 days for every ticker, then find the signals">${S.wwFilling ? 'Getting walls…' : 'Get all walls (45 days)'}</button> ${wwFillTxt()}`;
async function wwFill() {
  S.wwFilling = true; render();
  try { await api('/bot/wallwatch/walls', { method: 'POST', body: {} }); toast('Getting the walls of every ticker — a few minutes'); }
  catch (e) { toast(e.message); S.wwFilling = false; render(); return; }
  let rounds = 0, idle = 0;
  const poll = async () => {
    await loadWatch();
    const f = (S.ww && S.ww.fill) || {};
    if (f.running) { idle = 0; return setTimeout(poll, 20000); }
    if (f.left && rounds < 15 && !(f.errors || []).length) {           // the job stopped on time: start the next round
      rounds++; try { await api('/bot/wallwatch/walls', { method: 'POST', body: {} }); } catch (e) {} return setTimeout(poll, 20000); }
    if (!f.left && idle++ < 4 && !(S.ww.run || {}).bfAt) return setTimeout(poll, 20000);   // signals being found
    S.wwFilling = false;
    Object.keys(S.fc).filter(k => k.startsWith('ww-')).forEach(k => delete S.fc[k]);
    render(); toast(f.left ? `${f.left} days still missing: press the button again` : 'All walls stored');
  };
  setTimeout(poll, 20000);
}
S.wwTk = S.wwTk || {};
const WW_RULES = [
  // [switch key, group, label, tooltip, optional field: [id, key, step, width, suffix, default, scale]]
  ['alertReq', 'Entry', 'Call alert first', 'A call alert must arm the ticker before an entry (size, wait and unusual in the Options flow row); off: the options flow is ignored and any candle at the support can enter'],
  ['vwapReset', 'Entry', 'VWAP reset candle', 'The entry candle must be the VWAP reset candle: the hourly candle with the highest volume of the last N days (VWAP reset in the Flow row)'],
  ['pwNeCw', 'Entry', 'PW ≠ CW', 'No entry when the put wall and the call wall are the same strike'],
  ['relFlow', 'Entry', 'Unusual flow', 'The call alert counts when its premium is at least N × the median call alert premium of THAT ticker over the last 30 days (Options flow row, min $50K). Fewer than 5 alerts in 30 days: the $ amount is used. Off: the fixed $ amount only.'],
  ['usePW', 'Entry', 'Support: put wall', 'The put wall can be the support that is retested'],
  ['useFlip', 'Entry', 'Support: flip', 'The gamma flip can be the support that is retested'],
  ['pwTest', 'Entry', 'Put wall test (daily)', 'Daily timeframe: the day\'s low comes within the retest zone of the put wall and the day closes above it (stop under the put wall)'],
  ['greenOnly', 'Entry', 'Green candle', 'The retest candle must close green (close > open)'],
  ['aboveFlip', 'Entry', 'Above the flip', 'The entry candle must close above the gamma flip'],
  ['abovePW', 'Entry', 'Above the PW', 'The entry candle must close above the put wall of the day'],
  ['belowPW', 'Entry', 'Below the PW', 'The entry candle must close below the put wall of the day'],
  ['cvdUp', 'Entry', 'CVD > 0', 'Buyers in charge: the volume delta (buy − sell, estimated from each candle) summed over the last N candles is above 0 (N in the CVD row)'],
  ['cvdDn', 'Entry', 'CVD < 0', 'Sellers in charge: the volume delta summed over the last N candles is below 0'],
  ['cvdRise', 'Entry', 'CVD rising', 'The CVD (sum of the last N candles) is higher than on the candle before'],
  ['cvdFall', 'Entry', 'CVD falling', 'The CVD (sum of the last N candles) is lower than on the candle before'],
  ['vwapOn', 'Entry', 'Above VWAP', 'The entry candle must close above the anchored VWAP'],
  ['emaOn', 'Entry', 'Above EMA', 'The entry candle must close above the hourly EMA (length in the EMA row)'],
  ['emaOverVwap', 'Entry', 'EMA > VWAP', 'The EMA must be above the anchored VWAP on the entry candle (uptrend)'],
  ['skewOn', 'Entry', 'Skew normal', 'Only when the 25-delta skew (put IV − call IV, ~30 days) is in the middle third of its last 6 months: neither calls already bid nor puts bid (fear)'],
  ['spyUp', 'Entry', 'SPY trend up', 'SPY above its close of 6 sessions ago (the TOS ttmTrend of SPY)'],
  ['kcNotExt', 'Entry', 'Under upper daily KC', 'Not over-extended: the entry candle closes under the upper band of the DAILY Keltner channel'],
  ['inGreen', 'Entry', 'Daily inertia green', 'Direction: the inertia (linear regression of the daily closes) over the 3 lengths is rising on all of them, today included'],
  ['inFall', 'Entry', 'Daily inertia falling', 'All 3 daily inertias falling (red)'],
  ['inNotRed', 'Entry', 'Daily inertia not red', 'Direction: at least one of the 3 daily inertias is rising (green or yellow)'],
  ['inhGreen', 'Entry', 'Hourly inertia green', 'The inertia of the hourly closes over the 3 lengths is rising on all of them at the entry candle (1D timeframe: at the day\'s last hourly candle)'],
  ['inhFall', 'Entry', 'Hourly inertia falling', 'All 3 hourly inertias falling (red)'],
  ['inhNotRed', 'Entry', 'Hourly inertia not red', 'At least one of the 3 hourly inertias is rising at the entry candle'],
  ['sqhOn', 'Entry', 'Squeeze on (1h)', 'The hourly TTM squeeze is on at the entry candle: Bollinger bands inside the Keltner channel (coiled)'],
  ['sqhFire', 'Entry', 'Squeeze fired up (1h)', 'The hourly squeeze turned off within the last N candles (Squeeze rows) and its momentum is above 0'],
  ['sqhMom', 'Entry', 'Squeeze momentum up (1h)', 'Hourly squeeze momentum above 0 and rising'],
  ['sqhRise', 'Entry', 'Squeeze momentum rising (1h)', 'Hourly squeeze momentum higher than on the candle before (above or below 0)'],
  ['sqhFall', 'Entry', 'Squeeze momentum falling (1h)', 'Hourly squeeze momentum lower than on the candle before'],
  ['sqhTurn', 'Entry', 'Squeeze turn up (1h)', 'The hourly squeeze momentum turned up under 0 (falling, then rising: the yellow arrow of Squeeze turns) on this candle or within the last N candles (N in the Squeeze hourly row)'],
  ['sqdOn', 'Entry', 'Squeeze on (daily)', 'The daily TTM squeeze is on (hourly timeframe: yesterday\'s daily candle; 1D: the entry day)'],
  ['sqdFire', 'Entry', 'Squeeze fired up (daily)', 'The daily squeeze turned off within the last N days and its momentum is above 0'],
  ['sqdMom', 'Entry', 'Squeeze momentum up (daily)', 'Daily squeeze momentum above 0 and rising'],
  ['sqdRise', 'Entry', 'Squeeze momentum rising (daily)', 'Daily squeeze momentum higher than the day before (1h timeframe: as of yesterday)'],
  ['sqdFall', 'Entry', 'Squeeze momentum falling (daily)', 'Daily squeeze momentum lower than the day before (1h timeframe: as of yesterday)'],
  ['sqdTurn', 'Entry', 'Squeeze turn up (daily)', 'The daily squeeze momentum turned up under 0 (the yellow arrow on the 1D chart) within the last N days (1h timeframe: as of yesterday\'s daily candle)'],
  ['kcdLower', 'Entry', 'Lower half daily KC', 'The entry candle closes in the lower half of the DAILY Keltner channel (at or under its middle line, the 21 EMA)'],
  ['kchLower', 'Entry', 'Lower half hourly KC', 'Entry fine tuning: the entry candle closes in the lower half of the HOURLY Keltner channel (at or under its middle line)'],
  ['orbAbove', 'Entry', 'Above ORB high', 'The entry candle closes above the opening range high (09:30–10:30 candle); no entry in the first hour'],
  ['fvgOn', 'Entry', 'In bullish FVG', 'The entry candle must trade into (or come within the margin of) an open bullish fair value gap and close above its bottom (sizes in the FVG row)'],
  ['noAboveCW', 'Entry', 'Not above CW', 'No entry when the candle opens or closes above the call wall'],
  ['cwRoomOn', 'Entry', 'Room to CW', 'No entry when the close is less than this × ATR under the call wall', ['ww-cwr', 'cwRoom', 0.25, 3.6, 'ATR', 1]],
  ['earnOn', 'Entry', 'Earnings', 'No entry within this many days of earnings', ['ww-earn', 'earnDays', 1, 3.2, 'd', 10]],
  ['earnExit', 'Exit', 'Sell before earnings', 'Live bot: sell at 15:30 the trading day before earnings (or on the day for after-close reports). The backtest / lab can\'t replay it: no past earnings dates.'],
  ['pvEod', 'Exit', 'Entry day close > support', 'The entry day must close above the support (put wall / flip) it bounced from, else the trade is sold at that close'],
  ['stopOn', 'Exit', 'Stop', 'Stop = the support − this × the 30-day ATR (off: no stop, the value still sets 1R)', ['ww-stop', 'stopAtr', 0.1, 3.4, 'ATR', 0.5]],
  ['trailOn', 'Exit', 'Trailing stop', 'Highest high since entry − this × the 30-day ATR, never down', ['ww-trail', 'trailAtr', 0.25, 3.4, 'ATR', 1]],
  ['targetOn', 'Exit', 'Call wall target', 'Sell at the call wall − this %', ['ww-tbuf', 'tgtBuf', 0.1, 3.4, '%', 0.5]],
  ['pwExit', 'Exit', 'Close under PW', 'Sell when the day closes under the day\'s put wall (live bot: at 15:50 if the price is under it)'],
  ['flipExit', 'Exit', 'Close under flip', 'Sell when the day closes under the gamma flip (trades entered above it)'],
  ['vwapExit', 'Exit', 'Close under VWAP', 'Sell when an hourly candle closes under the anchored VWAP'],
  ['kcRide', 'Exit', 'Ride the daily KC', 'Follow the daily Keltner: an hourly close above its upper band starts the ride; the call wall target and the other exits wait, and the trade is sold at the first daily close back under the upper band, the same day included (the stop still applies).'],
  ['cwLost', 'Exit', 'Back under CW', 'After an hourly close above the call wall, sell at the first daily close back under it'],
  ['dpLost', 'Exit', 'Back under dark pool', 'After an hourly close above one of the top 5 dark pool levels (above your entry), sell at the first daily close back under the highest one passed'],
  ['fvgExit', 'Exit', 'New bearish FVG', 'Sell at the close of the hourly candle that completes a new bearish fair value gap after the entry (its high under the low of two candles before, gap at least the FVG size × ATR)'],
  ['scaleOn', 'Exit', 'Sell part at', 'Sell part of the position (contracts, at market; at least 1, never all) when price reaches the entry + this many R (R = entry − stop), the rest keeps the other exits', ['ww-scr', 'scaleR', 0.25, 3.2, 'R', 1], ['ww-scp', 'scalePct', 5, 3.2, '%', 50]],
  ['scaleBE', 'Exit', 'Rest to breakeven', 'After the partial sale, the stop of the rest moves up to the entry price'],
  ['inYExit', 'Exit', 'Inertia not green', 'Sell at the daily close when the daily inertia is no longer green (yellow or red)'],
  ['sqhExit', 'Exit', 'Squeeze momentum down (1h)', 'Sell at the close of the first hourly candle where the hourly squeeze momentum falls'],
  ['sqhRiseX', 'Exit', 'Squeeze momentum up (1h)', 'Sell at the close of the first hourly candle whose squeeze momentum rises'],
  ['sqdExit', 'Exit', 'Squeeze momentum down (daily)', 'Sell at the daily close when the daily squeeze momentum falls'],
  ['sqdRiseX', 'Exit', 'Squeeze momentum up (daily)', 'Sell at the daily close when the daily squeeze momentum rises'],
  ['inhYExit', 'Exit', 'Hourly inertia not green', 'Sell when the hourly inertia is no longer green (yellow or red): at that hourly close (1D timeframe: at the daily close)'],
  ['inhRiseX', 'Exit', 'Hourly inertia rising', 'Sell at the close of the first hourly candle with all 3 hourly inertias rising (green)'],
  ['inhRedX', 'Exit', 'Hourly inertia falling', 'Sell at the close of the first hourly candle with all 3 hourly inertias falling (red)'],
  ['inRedExit', 'Exit', 'Inertia turns red', 'Sell at the daily close when all 3 daily inertias are falling (red)'],
  ['inRiseX', 'Exit', 'Daily inertia rising', 'Sell at the daily close when all 3 daily inertias are rising (green)'],
  ['cvdRiseX', 'Exit', 'CVD rising', 'Sell at the close of the first candle whose CVD (sum of the last N candles) is higher than the candle before'],
  ['cvdFallX', 'Exit', 'CVD falling', 'Sell at the close of the first candle whose CVD (sum of the last N candles) is lower than the candle before'],
  ['emaExit', 'Exit', 'Close under EMA', 'Sell when an hourly candle closes under the EMA (same length as the entry EMA)'],
];
// indicators: [key, name, chart layer, parameters [key, label, step, width, suffix, default, scale], what it is, rules that use it]
const WW_IND = [
  ['walls', 'Gamma walls', 'walls', [['retestZone', 'retest zone', 0.05, 4, '× ATR', 0.25]], 'Put wall, call wall and gamma flip of each day; a candle retests a wall when its low comes within the zone', ['usePW', 'useFlip', 'aboveFlip', 'abovePW', 'belowPW', 'pwExit', 'noAboveCW', 'cwRoomOn', 'pwNeCw', 'targetOn', 'flipExit', 'cwLost', 'pvEod']],
  ['flow', 'Options flow', 'flow', [['retestMin', 'call alert ≥ $', 50, 4.2, 'K', 500000, 1000], ['retestDays', 'wait', 1, 3, 'days', 5], ['relMult', 'unusual', 0.5, 3, '× median', 3]], 'Call alerts bought at the ask (Unusual Whales); the retest must come within the wait', ['alertReq', 'relFlow']],
  ['vwap', 'Anchored VWAP', 'vwap', [['vwapDays', 'reset on highest volume of', 1, 3.2, 'days', 10]], 'VWAP restarted on the hourly candle with the highest volume of the period', ['vwapOn', 'vwapReset', 'emaOverVwap', 'vwapExit']],
  ['ema', 'EMA (hourly)', 'ema', [['emaLen', 'length', 1, 3.4, '', 20]], 'Exponential average of the hourly closes', ['emaOn', 'emaOverVwap', 'emaExit']],
  ['kcd', 'Keltner daily', 'kc', [['kcLen', 'EMA', 1, 3.2, '', 21], ['kcMult', '±', 0.5, 3, '× ATR', 3]], 'Daily candles (today building up hour by hour): over-extension', ['kcNotExt', 'kcdLower', 'kcRide']],
  ['kch', 'Keltner hourly', 'kch', [['kchLen', 'EMA', 1, 3.2, '', 21], ['kchMult', '±', 0.5, 3, '× ATR', 3]], 'Hourly candles: entry fine tuning', ['kchLower']],
  ['fvg', 'Fair value gaps', 'fvg', [['fvgMin', 'min size', 0.05, 3.2, '× ATR', 0.2], ['fvgNear', 'margin', 0.05, 3.2, '× ATR', 0.25], ['fvgDays', 'max age', 1, 3, 'days', 10]], '3-candle gaps on hourly candles, until filled or expired', ['fvgOn', 'fvgExit']],
  ['inertia', 'Daily inertia', 'inertia', [['inN1', 'lengths', 1, 3.2, '', 70], ['inN2', '', 1, 3.2, '', 40], ['inN3', '', 1, 3.2, 'days', 20]], 'Direction from the daily closes: linear regression over 3 lengths; green = all rising, yellow = mixed, red = all falling (colored strip under the candles)', ['inGreen', 'inNotRed', 'inYExit', 'inRedExit', 'inFall', 'inRiseX']],
  ['inertiah', 'Hourly inertia', 'inertiah', [['inhN1', 'lengths', 1, 3.2, '', 70], ['inhN2', '', 1, 3.2, '', 40], ['inhN3', '', 1, 3.2, 'candles', 20]], 'The same regression on the hourly closes (lengths in hourly candles); thin strip above the daily one (1h chart view)', ['inhGreen', 'inhNotRed', 'inhYExit', 'inhFall', 'inhRiseX', 'inhRedX']],
  ['sqzh', 'Squeeze hourly', 'sqz', [['sqTurnN', 'turn within', 1, 3, 'candles', 1], ['sqhLen', 'length', 1, 3.2, '', 20], ['sqhBB', 'BB', 0.1, 3, '× SD', 2], ['sqhKC', 'KC', 0.1, 3, '× ATR', 1.5], ['sqFireN', 'fired within', 1, 3, 'candles', 3]], 'TTM squeeze on hourly candles: red dot = squeeze on (BB inside KC), green = off; strip at the bottom of the chart', ['sqhOn', 'sqhFire', 'sqhMom', 'sqhExit', 'sqhTurn', 'sqhRise', 'sqhFall', 'sqhRiseX']],
  ['sqzd', 'Squeeze daily', 'sqz', [['sqdLen', 'length', 1, 3.2, '', 20], ['sqdBB', 'BB', 0.1, 3, '× SD', 2], ['sqdKC', 'KC', 0.1, 3, '× ATR', 1.5]], 'TTM squeeze on daily candles (same dots, lower strip)', ['sqdOn', 'sqdFire', 'sqdMom', 'sqdExit', 'sqdTurn', 'sqdRise', 'sqdFall', 'sqdRiseX']],
  ['cvd', 'CVD (volume delta)', 'pres', [['cvdN', 'sum of the last', 1, 3.2, 'candles', 20]], 'Buy − sell volume of each candle, estimated from where it closes in its range (close at the high = all buying), summed over the last N candles of the timeframe (1h: hourly candles, 1D: days). The Buy / sell pressure pane draws the same line; drawn like the squeeze (cyan / blue above 0, yellow / red below)', ['cvdUp', 'cvdDn', 'cvdRise', 'cvdFall', 'cvdRiseX', 'cvdFallX']],
  ['dark', 'Dark pool', 'dark', [], 'Top dark pool price levels of the last 2 months', ['dpLost']],
  ['orb', 'Opening range', 'orb', [], 'High / low of the 09:30–10:30 candle', ['orbAbove']],
  ['spy', 'SPY trend', 'spy', [], 'SPY vs its close 6 sessions ago', ['spyUp']],
  ['skew', 'Skew (25Δ)', null, [], '25-delta put IV − call IV vs its last 6 months', ['skewOn']],
];
// the indicators table, then the Entry / Exit ON/OFF rules. vals: settings; sw: switches; mode 'live' | 'lab'
function wwRulePanel(vals, sw, mode, shown) {
  const numA = mode === 'lab' ? 'data-labnum' : 'data-pk', swA = mode === 'lab' ? 'data-labsw' : 'data-wwsw';
  const inp = (k, step, w, def, scale) => `<input ${numA}="${k}" ${scale ? `data-scale="${scale}"` : ''} type="number" step="${step}" min="0" value="${vals[k] != null ? +(vals[k] / (scale || 1)).toFixed(4) : def / (scale || 1)}" style="width:${w}em;padding:1px 4px">`;
  const label = Object.fromEntries(WW_RULES.map(r => [r[0], r[2]]));
  const rows = WW_IND.map(([k, name, layer, ps, tip, uses]) => {
    const used = uses.filter(u => sw[u]);
    const eye = layer ? `<button class="btn ${S.fcLayers[layer] ? 'coachbtn' : ''}" data-fclayer="${layer}" title="Show / hide on the chart" style="font-size:.72rem;padding:0 7px">${S.fcLayers[layer] ? 'shown' : 'hidden'}</button>` : '<span class="muted" style="font-size:.72rem">—</span>';
    return `<tr style="${used.length ? '' : 'opacity:.6'}"><td style="padding:2px 6px">${eye}</td><td style="padding:2px 6px" title="${esc(tip)}"><b style="font-size:.82rem">${esc(name)}</b></td>
      <td style="white-space:nowrap;padding:2px 6px">${k === 'dark' ? `<span class="muted" style="font-size:.78rem" title="Dark pool prints of at least this % of the 30-day average daily volume">prints ≥</span> <input id="ww-dpmin" type="number" step="0.05" min="0.01" value="${S.wwDpPct ?? 0.25}" style="width:4.2em;padding:1px 4px"><span class="muted" style="font-size:.75rem">% of avg daily volume</span> &nbsp;<span class="muted" style="font-size:.78rem" title="How many dark pool price levels to draw (most premium first), from the last 180 days">lines</span> <input id="ww-dpn" type="number" step="1" min="0" max="10" value="${S.dpN ?? 5}" style="width:3.2em;padding:1px 4px"><span class="muted" style="font-size:.75rem">(0–10, last 180 days)</span>` : ''}${ps.map(([pk, pl, st, w, suf, def, sc]) => `<span class="muted" style="font-size:.78rem">${esc(pl)}</span> ${inp(pk, st, w, def, sc)}<span class="muted" style="font-size:.75rem">${esc(suf)}</span>`).join(' &nbsp;') || (k === 'dark' ? '' : '<span class="muted" style="font-size:.75rem">no setting</span>')}</td>
      <td style="font-size:.78rem;padding:2px 6px">${used.length ? `<span class="gain">used by</span> ${used.map(u => esc(label[u] || u)).join(', ')}` : '<span class="muted">not used</span>'}</td></tr>`;
  }).join('');
  const chip = ([k, , lbl, tip, f, f2]) => { const onv = !!sw[k];
    return `<span style="display:inline-flex;gap:3px;align-items:center;border:1px solid ${onv ? 'var(--gain)' : 'var(--line)'};border-radius:14px;padding:1px 4px 1px 2px;${onv ? '' : 'opacity:.55'}">
      <button class="btn" ${swA}="${k}" title="${esc(tip)} — click to turn ${onv ? 'off' : 'on'}" style="font-size:.75rem;padding:0 8px;border-radius:12px;${onv ? 'background:var(--gain);color:#fff;border-color:transparent' : ''}">${onv ? 'ON' : 'OFF'}</button>
      <span style="font-size:.8rem">${lbl}</span>${f ? ` ${inp(f[1], f[2], f[3], f[5])}<span class="muted" style="font-size:.75rem">${f[4]}</span>` : ''}${f2 ? ` <span style="font-size:.8rem">sell</span> ${inp(f2[1], f2[2], f2[3], f2[5])}<span class="muted" style="font-size:.75rem">${f2[4]}</span>` : ''}</span>`; };
  const grp = g => `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center"><b style="font-size:.8rem;min-width:3.6em">${g}</b>${WW_RULES.filter(r => r[1] === g && shown(r)).map(chip).join('')}</div>`;
  return `<details data-indpanel ${S.indOpen !== false ? 'open' : ''} style="margin:0"><summary style="font-size:.8rem;font-weight:700;cursor:pointer">Indicators</summary>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.8rem;margin:4px 0"><tbody>${rows}</tbody></table></div></details>
    <details data-rulespanel ${S.rulesOpen !== false ? 'open' : ''} style="margin:0"><summary style="font-size:.8rem;font-weight:700;cursor:pointer">Entry &amp; exit rules</summary>
    <div style="display:flex;flex-direction:column;gap:6px;margin-top:4px">${grp('Entry')}${grp('Exit')}</div></details>`;
}
const LAB_ONLY = ['pwTest'];   // daily-timeframe only: the live bot runs on hourly candles   // rules the live bot can't do yet: shown in the Strategy lab only
const WW_CORE = ['earnExit', 'alertReq', 'relFlow', 'usePW', 'vwapOn', 'kcNotExt', 'cwRoomOn', 'earnOn', 'stopOn', 'targetOn', 'flipExit', 'kcRide', 'cwLost', 'dpLost'];   // the validated strategy
const WW_DEF_ON = { inFall: false, inhFall: false, sqhRise: false, sqhFall: false, sqdRise: false, sqdFall: false, cvdRise: false, cvdFall: false, inRiseX: false, inhRiseX: false, inhRedX: false, sqhRiseX: false, sqdRiseX: false, cvdRiseX: false, cvdFallX: false, pwExit: false, sqhTurn: false, sqdTurn: false, sqhOn: false, sqhFire: false, sqhMom: false, sqhExit: false, sqdOn: false, sqdFire: false, sqdMom: false, sqdExit: false, inhGreen: false, inhNotRed: false, inhYExit: false, pwTest: false, inYExit: false, inGreen: false, inNotRed: false, inRedExit: false, kcdLower: false, kchLower: false, scaleOn: false, scaleBE: true, fvgExit: false, fvgOn: false, relFlow: true, alertReq: true, vwapReset: false, pwNeCw: false, pvEod: false, aboveFlip: false, abovePW: false, belowPW: false, cvdUp: false, cvdDn: false, vwapOn: false, emaOn: false, vwapExit: false, emaExit: false, emaOverVwap: false, spyUp: false, kcNotExt: false, orbAbove: false, skewOn: false, kcRide: false, cwLost: false, dpLost: false };
S.wwSw = S.wwSw || null;
try { S.rtUseLab = localStorage.getItem('rtUseLab') === '1'; } catch (e) { S.rtUseLab = false; }
try { S.chartDaily = localStorage.getItem('chartDaily') === '1'; } catch (e) { S.chartDaily = false; }
try { S.indOpen = localStorage.getItem('indOpen') !== '0'; S.rulesOpen = localStorage.getItem('rulesOpen') !== '0'; S.hideStats = localStorage.getItem('hideStats') === '1'; S.hideSettings = localStorage.getItem('hideSettings') === '1'; } catch (e) { S.indOpen = true; S.rulesOpen = true; S.hideStats = false; S.hideSettings = false; }
document.addEventListener('toggle', e => {        // the Indicators / rules panels stay as you left them (toggle doesn't bubble: capture)
  const t = e.target;
  if (!t.matches || !t.matches('[data-indpanel],[data-rulespanel]')) return;
  const key = t.matches('[data-indpanel]') ? 'indOpen' : 'rulesOpen';
  if (S[key] === t.open) return;                  // fired by a redraw (a panel created open): nothing changed, no redraw loop
  S[key] = t.open;
  try { localStorage.setItem(key, t.open ? '1' : '0'); } catch (x) {}
  if (route() === 'botchart') { clearTimeout(S.panelT); S.panelT = setTimeout(render, 50); }   // the chart takes the freed space
}, true);
function wwSwState() {
  const c = (S.ww && S.ww.config) || {};
  if (S.wwSwFor !== c && !S.wwDirty) S.wwSw = null;      // settings (re)loaded from the server: start from them, unless edits are pending
  S.wwSwFor = c;
  if (!S.wwSw) S.wwSw = Object.fromEntries(WW_RULES.map(([k]) => [k, c[k] ?? (WW_DEF_ON[k] ?? true)]));
  return S.wwSw;
}
function wwParams(compact) {
  const c = { ...((S.ww && S.ww.config) || {}), ...(S.wwDraft || {}) }, sw = wwSwState(), lab = 'class="muted" style="font-size:.8rem;display:inline-flex;gap:4px;align-items:center"';
  const shown = r => LAB_ONLY.includes(r[0]) ? false : r[0] === 'scaleBE' ? !!sw.scaleOn : true;   // the same rules as the Strategy lab
  return `<div data-wwwhere="${compact ? 'chart' : 'page'}" style="display:flex;flex-direction:column;gap:6px${compact ? '' : ';border:1px solid var(--line);border-radius:8px;padding:8px 10px'}">
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><b style="font-size:.8rem;min-width:3.6em">Bot</b>
    <label ${lab} title="Every minute, tickers with unusual call flow (your bot's flow filters) join the list"><input type="checkbox" id="ww-auto" ${c.autoAdd !== false ? 'checked' : ''}>add flow tickers</label>
    <label ${lab} title="Buy a call on Alpaca paper when a signal fires (contract picked by the bot, limit chased up until filled)"><input type="checkbox" id="ww-trade" ${c.trade !== false ? 'checked' : ''}>trade signals</label></div>
    ${wwRulePanel({ ...c, ...(S.wwDraft || {}) }, sw, 'live', shown)}
    <div><button class="btn ${S.wwDirty ? 'coachbtn' : ''}" data-wwparams="${compact ? 'chart' : 'page'}" data-applybtn="live" style="font-size:.78rem;padding:1px 12px">${S.wwDirty ? 'Apply changes' : 'Apply'}</button>
    <span class="muted" style="font-size:.75rem">Nothing changes until you click Apply (switches and numbers). Signals are then recomputed.</span></div></div>`;
}
function wwMarkDirty() {                        // pending changes: light up the Apply buttons without redrawing
  document.querySelectorAll('[data-applybtn]').forEach(b => { const d = b.dataset.applybtn === 'lab' ? S.labDirty : S.wwDirty;
    if (d) { b.classList.add('coachbtn'); b.textContent = 'Apply changes'; } });
}
async function wwSaveParams(where) {
  const nums = {};
  document.querySelectorAll(`[data-wwwhere="${where}"] [data-pk]`).forEach(i => { const v = parseFloat(i.value); if (!isNaN(v)) nums[i.dataset.pk] = i.dataset.scale ? v * +i.dataset.scale : v; });
  const body = { autoAdd: !!($('#ww-auto') || {}).checked, trade: !!($('#ww-trade') || {}).checked, retestSetup: true, kcDaily: true, ...wwSwState(), ...(S.wwDraft || {}), ...nums };
  try { S.ww = { ...(S.ww || {}), ...(await api('/bot/wallwatch', { method: 'PUT', body })) }; } catch (e) { toast(e.message); return; }
  S.wwDraft = {}; S.wwDirty = false; S.wwSw = null;
  try { await api('/bot/wallwatch/run', { method: 'POST', body: { days: 45 } }); } catch (e) {}
  if (where === 'chart') { wwTkFill(fcfSymbol()); toast('Recomputing this chart now, every ticker in the background'); }
  else { toast('Recomputing every ticker with the new settings — about a minute'); runWatchPoll(); }
  render();
}
function wwTkBtn(sym, d) {
  const f = (d && d.fill) || {}, busy = S.wwTk[sym] || f.running, miss = d ? Math.max((d.missingDays || 0) + (d.noflipDays || 0), d.maxPainMissing || 0) : null;
  return `<button class="btn ${miss ? 'coachbtn' : ''}" data-wwtk="${esc(sym)}" ${busy ? 'disabled' : ''} style="font-size:.8rem;padding:2px 10px" title="Fetch the put wall, call wall, flip and max pain of the last 45 days for ${esc(sym)}, then find its signals">${busy ? 'Getting walls…' : 'Get walls (45 days)'}</button>`
    + (d && !busy ? ` <span class="muted" style="font-size:.8rem">${miss ? `${miss} day${miss === 1 ? '' : 's'} missing` : 'all days stored'}</span>` : '');
}
S.wwTkPoll = S.wwTkPoll || {};
try { S.wwDays = +(localStorage.getItem('wwDays') || 45) || 45; } catch (e) { S.wwDays = 45; }
async function wwTkFill(sym) {
  clearTimeout(S.wwTkPoll[sym]);                 // one poller per ticker
  S.wwTk[sym] = true; render();
  try { await api('/bot/wallwatch/walls', { method: 'POST', body: { t: sym } }); }
  catch (e) { toast(e.message); S.wwTk[sym] = false; render(); return; }
  let n = 0;
  const poll = async () => {
    n++;
    const k = fcKey('ww-' + sym);
    try { S.fc[k] = await api(`/bot/wallwatch/chart?t=${encodeURIComponent(sym)}&days=${S.wwDays || 45}`); } catch (e) {}
    const f = (S.fc[k] && S.fc[k].fill) || {};
    if (f.running && n < 40) { if (route() === 'botchart') render(); S.wwTkPoll[sym] = setTimeout(poll, 8000); return; }
    S.wwTk[sym] = false; loadWatch();
    toast(f.left ? `${sym}: ${f.left} days still missing — press again` : `${sym}: walls stored`);
    if (route() === 'botchart') render();
  };
  S.wwTkPoll[sym] = setTimeout(poll, 8000);
}
function runWatchPoll() {
  let n = 0; const tick = async () => { n++; await loadWatch(); if (S.ww && S.ww.run && S.ww.run.running && n < 12 && route() === 'botwatch') setTimeout(tick, 30000); };
  setTimeout(tick, 30000);
}
async function runWatch(days) {
  try { await api('/bot/wallwatch/run', { method: 'POST', body: { days } }); toast(days > 3 ? `Checking the last ${days} days in the background — a few minutes` : 'Checking every ticker now — about a minute'); }
  catch (e) { toast(e.message); return; }
  if (S.ww && S.ww.run) S.ww.run.running = 'now';
  render();
  let n = 0; const tick = async () => { n++; await loadWatch(); if (S.ww && S.ww.run && S.ww.run.running && n < 12 && route() === 'botwatch') setTimeout(tick, 30000); };
  setTimeout(tick, 30000);
}
/* ---------- rule tester: one fixed exit, every rule measured on the history ---------- */
async function loadRuleTest(poll) {
  S.rtBusy = true;
  try { S.rt = await api('/bot/wallwatch/ruletest'); } catch (e) { S.rt = { error: e.message }; }
  S.rtBusy = false;
  if (route() === 'botwatch') render();
  const t = (S.rt && S.rt.test) || {}, hs = (S.rt && S.rt.history) || {}, rb = (S.rt && S.rt.robust) || {}, pv = (S.rt && S.rt.pv) || {}, rgm = (S.rt && S.rt.regime) || {}, srch = (S.rt && S.rt.search) || {};
  clearTimeout(S.rtPoll);
  if ((t.running || hs.running || rb.running || pv.running || rgm.running || srch.running) && route() === 'botwatch') S.rtPoll = setTimeout(() => loadRuleTest(true), 15000);
}
const SEARCH_RULES = ['inGreen', 'inNotRed', 'alertReq', 'relFlow', 'greenOnly', 'aboveFlip', 'abovePW', 'belowPW', 'vwapOn', 'emaOn', 'emaOverVwap', 'kcNotExt', 'kcdLower', 'kchLower', 'orbAbove', 'fvgOn', 'noAboveCW', 'cwRoomOn', 'vwapReset', 'pwNeCw', 'spyUp', 'skewOn'];
function wwSearchBox(x) {
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const lbl = Object.fromEntries(WW_RULES.map(r => [r[0], r[2]]));
  const names = rs => rs && rs.length ? rs.map(k => esc(lbl[k] || k)).join(' + ') : '<span class="muted">no entry rule (support retest only)</span>';
  const row = (title, st, rs) => `<tr><td style="min-width:240px;max-width:460px;white-space:normal;word-break:normal">${title}</td><td class="r">${st.n ?? 0}</td><td class="r">${st.win ?? '—'}${st.win != null ? '%' : ''}</td><td class="r">${R(st.avgR)}</td><td class="r">${st.totR ?? '—'}</td><td class="r">${st.pf ?? '—'}</td>
    <td class="r">${R(st.h1)} / ${R(st.h2)}<br><span class="muted" style="font-size:.75rem">${st.h1n ?? 0} / ${st.h2n ?? 0}</span></td>
    <td>${rs ? `<button class="btn" data-trycombo="${esc(rs.join(','))}" style="font-size:.75rem;padding:0 8px" title="Set these entry rules in the Strategy lab and open the chart">Try in lab</button>` : ''}</td></tr>`;
  const head = '<thead><tr><th>Entry rules</th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg R</th><th class="r">Total R</th><th class="r">PF</th><th class="r">1st / 2nd half</th><th></th></tr></thead>';
  const body = x.running ? '<p class="loading">Trading every candle at the support with your exits, then replaying the mixes of rules… (a few minutes)</p>' : !x.top ? '' : `
    <p class="muted" style="font-size:.82rem;margin:6px 0">${x.tickers} tickers · ${x.candidates} candles at the support · ${x.tested} mixes of rules tested · at least ${x.minTrades} trades · ${x.lab ? 'Strategy lab' : 'live'} exits · run ${esc((x.at || '').slice(5, 16).replace('T', ' '))}</p>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.84rem">${head}<tbody>
      <tr><td colspan="8" style="background:var(--line);font-weight:700;font-size:.82rem">Best mixes (positive in both halves)</td></tr>
      ${(x.top || []).map(t => row(names(t.rules), t, t.rules)).join('') || '<tr><td colspan="8" class="muted">None with enough trades.</td></tr>'}
      <tr><td colspan="8" style="background:var(--line);font-weight:700;font-size:.82rem">Step by step: add the rule that helps most</td></tr>
      ${(x.path || []).map((t, i) => row(`${i + 1}. + ${esc(lbl[t.rules[t.rules.length - 1]] || '')}<br><span class="muted" style="font-size:.75rem">${names(t.rules)}</span>`, t, t.rules)).join('')}
      <tr><td colspan="8" style="background:var(--line);font-weight:700;font-size:.82rem">Your entry rules, and each one changed</td></tr>
      ${row('<b>Your entry rules</b><br><span class="muted" style="font-size:.75rem">' + names((x.mine || {}).rules) + '</span>', x.mine || {}, (x.mine || {}).rules)}
      ${(x.singles || []).map(t => row(`${t.change === 'on' ? 'add' : 'remove'} <b>${esc(lbl[t.rule] || t.rule)}</b>`, t, t.rules)).join('')}
      ${row('Base: support retest only', x.base || {}, [])}
    </tbody></table></div>
    <p class="muted" style="font-size:.8rem;margin:4px 0 0">Screening: every candle at the support is traded alone, then each mix replays them one trade at a time per ticker. Check a mix you like with “Try in lab” → Test on all tickers. More mixes tested = more chance one looks good by luck: prefer mixes that hold in both halves with many trades.</p>`;
  return `<h3 style="font-size:.95rem;margin:16px 0 4px">Rule search <span class="muted" style="font-weight:400;font-size:.8rem">min trades</span> <input id="rs-min" type="number" min="10" step="10" value="${x.minTrades || 60}" style="width:4.5em;padding:1px 4px"> <button class="btn coachbtn" data-rsrun ${x.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 10px">Search the mixes</button></h3>
    <p class="muted" style="margin:0;font-size:.85rem">Every entry rule on / off and their mixes, on the ${S.rtUseLab ? 'Strategy lab' : 'live'} exits.</p>${body}
    ${(x.errors || []).length ? `<p class="loss" style="font-size:.82rem">${esc(x.errors.join(' · '))}</p>` : ''}`;
}
function wwRuleTester() {
  const r = S.rt || {}, t = r.test || {}, hs = r.history || {}, P = t.params || {};
  const num = (id, v, step, w) => `<input id="${id}" type="number" step="${step}" min="0" value="${v}" style="width:${w}em;padding:1px 4px">`;
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const base = (t.rules || [])[0] || {};
  const verdict = (x, ref) => {
    ref = ref || base;
    if (!x.n) return '<span class="muted">no trade</span>';
    if (x.n < 30) return '<span class="muted">too few trades</span>';
    if (x === ref) return '<span class="muted">reference</span>';
    const up1 = x.h1 != null && ref.h1 != null && x.h1 > ref.h1, up2 = x.h2 != null && ref.h2 != null && x.h2 > ref.h2;
    return up1 && up2 && x.avgR > 0 ? '<b class="gain">helps in both halves</b>' : up1 || up2 ? '<span style="color:var(--warn)">one half only: noise?</span>' : '<span class="loss">doesn’t help</span>';
  };
  const row = (x, label, ref) => `<tr><td>${esc(label || x.label)}</td><td class="r">${x.n || 0}${x.open ? `<br><span class="muted" style="font-size:.72rem" title="Trades no exit closed yet: counted at their last price">${x.open} still open</span>` : ''}</td><td class="r">${x.win ?? '—'}${x.win != null ? '%' : ''}</td><td class="r">${R(x.avgR)}</td><td class="r">${x.totR ?? '—'}</td>
    <td class="r">${R(x.h1)} <span class="muted" style="font-size:.75rem">${x.h1n ?? ''}</span></td><td class="r">${R(x.h2)} <span class="muted" style="font-size:.75rem">${x.h2n ?? ''}</span></td><td>${verdict(x, ref)}</td></tr>`;
  const thead = first => `<thead><tr><th>${first}</th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg R</th><th class="r">Total R</th><th class="r">1st half</th><th class="r">2nd half</th><th></th></tr></thead>`;
  const oldest = Object.values(hs.oldest || {}).filter(Boolean).sort()[0];
  const hsTxt = hs.running ? `<span class="loading" style="padding:0">loading history… alerts ${(hs.alertsDone || []).length} tickers${hs.wallsTotal ? ` · walls ${hs.wallsTotal - (hs.wallsLeft || 0)} / ${hs.wallsTotal} days` : ''}${(hs.skewDone || []).length ? ` · skew ${(hs.skewDone || []).length} tickers` : ''}${(hs.dpDone || []).length ? ` · dark pool ${(hs.dpDone || []).length} tickers` : ''}</span>`
    : hs.finishedAt ? `<span class="muted">history stored ${esc((hs.finishedAt || '').slice(5, 16).replace('T', ' '))} (${hs.days} days asked${oldest ? `, oldest call alert ${esc(oldest)}` : ''}${(hs.skewDone || []).length ? `, skew for ${Object.values(hs.skewDays || {}).filter(n => n > 0).length} tickers` : ', no skew yet'}${(hs.dpDone || []).length ? `, dark pool for ${(hs.dpDone || []).length} tickers` : ', no dark pool yet'}) · loading again only adds the new days</span>` : '<span class="muted">no history loaded yet: 45 days only</span>';
  const groups = {};
  (t.buckets || []).forEach(b => { (groups[b.group] = groups[b.group] || []).push(b); });
  const body = t.running ? '<p class="loading">Testing every rule on the history… (a few minutes)</p>' : !t.rules ? '<p class="muted">Load the history, then run the test.</p>' : `
    <p class="muted" style="margin:6px 0">${t.tickers} tickers · ${t.alerts} call alerts · ${esc(t.period.from)} → ${esc(t.period.to)} (halves split at ${esc(t.period.mid)}) · exit: stop ${t.params.stopAtr} ATR under the support, target ${t.params.fixR}R${t.params.maxDays ? `, or sold after ${t.params.maxDays} days` : ''}. Earnings aren’t tested (no history).</p>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.85rem">${thead('Entry rules (each added alone to the base)')}<tbody>${(t.rules || []).map(x => row(x)).join('')}</tbody></table></div>
    ${!(t.buckets || []).length ? '' : `<div style="overflow-x:auto;margin-top:10px"><table class="pos" style="font-size:.85rem">${thead('Base split by')}<tbody>${Object.entries(groups).map(([g, bs]) => `<tr><td colspan="8" style="padding-top:8px"><b>${esc(g)}</b></td></tr>` + bs.map(b => row(b, b.label)).join('')).join('')}</tbody></table></div>`}
    <div style="overflow-x:auto;margin-top:10px"><table class="pos" style="font-size:.85rem">${thead('Exits (on your entry settings)')}<tbody>${(t.exits || []).map(x => row(x, null, t.exits[0])).join('')}</tbody></table></div>
    ${(t.bySymbol || []).length ? `<details style="margin-top:8px"><summary class="muted" style="cursor:pointer">By ticker (your entry settings)</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.85rem">${thead('Ticker')}<tbody>${t.bySymbol.map(x => row(x, x.sym, { h1: 0, h2: 0 })).join('')}</tbody></table></div></details>` : ''}
    ${(t.errors || []).length ? `<p class="loss" style="font-size:.85rem">${esc(t.errors.join(' · '))}</p>` : ''}`;
  const rb = r.robust || {}, mc = rb.monteCarlo || {}, cc = rb.concentration || {}, b0 = (rb.rows || [])[0] || {};
  const rbBody = rb.running ? '<p class="loading">Re-running your settings with each number nudged… (a few minutes)</p>' : !rb.rows ? '' : `
    <div style="overflow-x:auto;margin-top:6px"><table class="pos" style="font-size:.85rem"><thead><tr><th>Your settings (entry + exits) with one number changed</th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg R</th><th class="r">Total R</th><th class="r">1st half</th><th class="r">2nd half</th><th></th></tr></thead><tbody>
    ${rb.rows.map((x, i) => `<tr><td>${esc(x.label)}</td><td class="r">${x.n || 0}</td><td class="r">${x.win ?? '—'}${x.win != null ? '%' : ''}</td><td class="r">${R(x.avgR)}</td><td class="r">${x.totR ?? '—'}</td><td class="r">${R(x.h1)}</td><td class="r">${R(x.h2)}</td>
      <td>${i === 0 ? '<span class="muted">reference</span>' : !x.n ? '' : x.avgR > 0 && b0.avgR > 0 && x.avgR >= b0.avgR * 0.5 ? '<b class="gain">holds</b>' : x.avgR > 0 ? '<span style="color:var(--warn)">weaker</span>' : '<span class="loss">breaks</span>'}</td></tr>`).join('')}</tbody></table></div>
    <div class="strip" style="margin:10px 0 0">
      <div class="stat"><span>Best 3 tickers</span><b>${cc.top3Share ?? '—'}%</b><span>of the total R · ${(cc.top3 || []).map(x => `${esc(x.sym)} ${x.R}R`).join(' · ')}</span></div>
      <div class="stat"><span>Without them</span><b>${R((cc.withoutTop3 || {}).avgR)}</b><span>${(cc.withoutTop3 || {}).n || 0} trades · ${cc.tickers || 0} tickers traded</span></div>
      <div class="stat"><span>Drawdown to expect</span><b>${mc.ddMedian ?? '—'}R</b><span>bad case (95%) ${mc.dd95 ?? '—'}R · longest losing streak ${mc.lossStreak ?? '—'}</span></div>
      <div class="stat"><span>Chance of losing</span><b>${mc.pLoss ?? '—'}%</b><span>over ${mc.n || 0} trades (resampled)</span></div></div>
    <p class="muted" style="font-size:.8rem;margin:6px 0 0">“Holds” = still positive with at least half the average R. If most lines break, the result is fitted to the past. If the best 3 tickers make most of the profit, it depends on them. Size so the bad-case drawdown is a loss you can live with.</p>`;
  const useLab = !!S.rtUseLab, hasLab = (() => { try { return !!localStorage.getItem('labCfg'); } catch (e) { return false; } })() || !!S.lab;
  const ranOn = (t.params && t.params.cfg) ? 'Strategy lab' : 'live';
  return `<section class="panel" style="margin:0 0 14px"><h2>Rule tester</h2>
    <div style="display:flex;gap:6px;align-items:center;margin:0 0 6px;font-size:.85rem"><b>Settings to test</b>
      <span class="seg"><button class="btn ${!useLab ? 'coachbtn' : ''}" data-rtsrc="live" style="padding:2px 10px">Live bot</button><button class="btn ${useLab ? 'coachbtn' : ''}" data-rtsrc="lab" ${hasLab ? '' : 'disabled title="Open the Strategy lab on the chart first"'} style="padding:2px 10px">Strategy lab</button></span>
      <span class="muted">${useLab ? 'the settings of the Strategy lab (nothing is saved, the bot is not changed)' : 'the settings the bot trades with'}${t.at ? ` · last test ran on the ${ranOn} settings` : ''}</span></div>
    <p class="muted" style="margin:0 0 8px">Every rule measured on the same trades with one fixed exit. A rule is worth keeping only if it raises the average R in <b>both halves</b> of the period with at least 30 trades.</p>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:.85rem">
      <b>History</b> ${num('rt-hdays', hs.days || 250, 10, 4.2)} trading days <button class="btn" data-rthist ${hs.running ? 'disabled' : ''}>Load history</button> ${hsTxt}</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:.85rem;margin-top:6px">
      <b>Test</b> last ${num('rt-days', P.days || 250, 10, 4.2)} days · stop ${num('rt-stop', P.stopAtr ?? ((S.ww && S.ww.config && S.ww.config.stopAtr) || 0.5), 0.1, 3.6)} ATR · target ${num('rt-fixr', P.fixR ?? 2, 0.5, 3.4)} R · sell after ${num('rt-maxd', P.maxDays ?? 5, 1, 3)} days
      <label style="display:inline-flex;gap:4px;align-items:center" title="Every rule added alone, every split (alert size, room, skew, dark pool, Keltner, opening range, SPY…) and every exit. Slower."><input type="checkbox" id="rt-full" ${P.full ? 'checked' : ''}>full analysis</label>
      <button class="btn coachbtn" data-rtrun ${t.running ? 'disabled' : ''}>Run the test</button>${t.at && !t.running ? ` <span class="muted">last run ${esc(t.at.slice(5, 16).replace('T', ' '))} · ${t.secs}s</span>` : ''}</div>
    ${body}
    ${wwSearchBox(r.search || {})}
    <h3 style="font-size:.95rem;margin:16px 0 4px">Robustness check <button class="btn" data-rtrob ${rb.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 10px;margin-left:6px">Run it</button>${rb.at && !rb.running ? ` <span class="muted" style="font-weight:400;font-size:.8rem">last run ${esc(rb.at.slice(5, 16).replace('T', ' '))}</span>` : ''}</h3>
    <p class="muted" style="margin:0;font-size:.85rem">Your full settings re-run with each number moved a little, where the profit comes from, and the drawdown to expect.</p>
    ${rbBody}
    ${wwPvStudy(r.pv || {})}
    ${wwRegime(r.regime || {})}</section>`;
}
/* ---------- put wall crossings with a VWAP reset: what happens next ---------- */
function wwPvStudy(pv) {
  const P = v => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}%</span>`;
  const H = ['1 day', '3 days', '5 days', '10 days'];
  const cell = x => x ? `<b>${x.up}%</b> up<br>${P(x.avg)} <span class="muted" style="font-size:.75rem">med ${x.med > 0 ? '+' : ''}${x.med}%</span>` : '—';
  const body = pv.running ? '<p class="loading">Finding every put wall crossing and VWAP reset in the history… (a few minutes)</p>' : !pv.groups ? '' : `
    <p class="muted" style="font-size:.82rem;margin:6px 0">${pv.tickers} tickers since ${esc((pv.period || {}).from || '')} · VWAP reset = the hourly candle with the highest volume of the last ${(S.ww && S.ww.config && S.ww.config.vwapDays) || 10} days · touching = its low–high range reaches the put wall of the day (±0.3%) · returns from that candle’s close.</p>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.84rem"><thead><tr><th>Situation</th><th class="r">Times</th>${H.map(h => `<th class="r">After ${h}</th>`).join('')}<th class="r">Best move<br>5 days (MFE)</th><th class="r">Worst move<br>5 days (MAE)</th><th class="r">5 days: 1st / 2nd half</th></tr></thead><tbody>
    ${pv.groups.map(g => `<tr><td>${esc(g.label)}</td><td class="r">${g.n}</td>${H.map(h => `<td class="r">${cell(g[h])}</td>`).join('')}<td class="r">${P(g.mfe)}</td><td class="r">${P(g.mae)}</td><td class="r">${P(g.h1)} / ${P(g.h2)}<br><span class="muted" style="font-size:.75rem">${g.h1n} / ${g.h2n}</span></td></tr>`).join('')}</tbody></table></div>
    ${pvTrades(pv.trades)}
    ${(pv.errors || []).length ? `<p class="loss" style="font-size:.82rem">${esc(pv.errors.join(' · '))}</p>` : ''}`;
  return `<h3 style="font-size:.95rem;margin:16px 0 4px">VWAP reset on the walls (long / short) <button class="btn" data-pvrun ${pv.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 10px;margin-left:6px">Run it</button>${pv.at && !pv.running ? ` <span class="muted" style="font-weight:400;font-size:.8rem">last run ${esc(pv.at.slice(5, 16).replace('T', ' '))}</span>` : ''}</h3>
    <p class="muted" style="margin:0;font-size:.85rem">LONG = the VWAP reset candle (highest volume of the period) touches the put wall and closes in the lower half of the Keltner channel. SHORT = it touches the call wall and closes in the upper half. Skipped when the put wall equals the call wall.</p>${body}`;
}
function wwRegime(rg) {
  const P = v => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}%</span>`;
  const H = ['1 day', '3 days', '5 days', '10 days'];
  const cell = x => x ? `<b>${x.up}%</b> up<br>${P(x.avg)} <span class="muted" style="font-size:.75rem">med ${x.med > 0 ? '+' : ''}${x.med}%</span>` : '—';
  const sk = rg.streaks || {};
  const body = rg.running ? '<p class="loading">Classifying every day by its gamma walls… (a few minutes)</p>' : !rg.sections ? '' : `
    <p class="muted" style="font-size:.82rem;margin:6px 0">${rg.tickers} tickers · ${rg.days} ticker-days since ${esc((rg.period || {}).from || '')} · from each day's close · a run of the same regime lasts on average: CW > PW ${sk.normal ?? '—'} d, CW = PW ${sk.same ?? '—'} d, CW < PW ${sk.inverted ?? '—'} d</p>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.84rem"><thead><tr><th>Situation</th><th class="r">Days</th><th class="r">% of days</th>${H.map(h => `<th class="r">After ${h}</th>`).join('')}<th class="r">Best move<br>5 days</th><th class="r">Worst move<br>5 days</th><th class="r">5 days: 1st / 2nd half</th></tr></thead><tbody>
    ${rg.sections.map(sc => `<tr><td colspan="10" style="background:var(--line);font-weight:700;font-size:.82rem">${esc(sc.title)}</td></tr>` + sc.groups.map(g => `<tr><td>${esc(g.label)}</td><td class="r">${g.n}</td><td class="r">${g.share}%</td>${H.map(h => `<td class="r">${cell(g[h])}</td>`).join('')}<td class="r">${P(g.mfe)}</td><td class="r">${P(g.mae)}</td><td class="r">${P(g.h1)} / ${P(g.h2)}<br><span class="muted" style="font-size:.75rem">${g.h1n} / ${g.h2n}</span></td></tr>`).join('')).join('')}
    </tbody></table></div>
    ${(rg.errors || []).length ? `<p class="loss" style="font-size:.82rem">${esc(rg.errors.join(' · '))}</p>` : ''}`;
  return `<h3 style="font-size:.95rem;margin:16px 0 4px">Gamma wall regimes <button class="btn" data-regrun ${rg.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 10px;margin-left:6px">Run it</button>${rg.at && !rg.running ? ` <span class="muted" style="font-weight:400;font-size:.8rem">last run ${esc(rg.at.slice(5, 16).replace('T', ' '))}</span>` : ''}</h3>
    <p class="muted" style="margin:0;font-size:.85rem">Every day of every ticker, sorted by how the call wall, put wall and flip sit and where the stock closed — and the days the walls move (yesterday's call wall becoming the put wall, the put wall stepping down…). Compare each row with "Every day".</p>${body}`;
}
/* ---------- strategy lab: try settings on the chart without touching the live bot ---------- */
S.lab = S.lab || null;
function labSave() { try { localStorage.setItem('labCfg', JSON.stringify(S.lab || {})); } catch (e) {} }
function labCfg() {
  if (!S.lab) { try { const v = JSON.parse(localStorage.getItem('labCfg') || 'null'); if (v && Object.keys(v).length) S.lab = v; } catch (e) {} }
  if (!S.lab) { const c = (S.ww && S.ww.config) || {}; if (!S.ww) return null;
    S.lab = Object.fromEntries([...WW_RULES.map(([k]) => [k, c[k] ?? (WW_DEF_ON[k] ?? true)]),
      ...WW_RULES.filter(r => r[4]).map(r => [r[4][1], c[r[4][1]] ?? r[4][5]]),
      ...WW_RULES.filter(r => r[5]).map(r => [r[5][1], c[r[5][1]] ?? r[5][5]]),
      ...WW_IND.flatMap(x => x[3]).map(p0 => [p0[0], c[p0[0]] ?? p0[5]]),
      ...['retestMin', 'retestDays', 'retestZone', 'vwapDays', 'kcLen', 'kcMult', 'fvgNear'].map(k => [k, c[k] ?? (k === 'fvgNear' ? 0.25 : undefined)])].filter(([, v]) => v !== undefined)); }
  if (S.lab && S.lab.fvgNear === undefined) S.lab.fvgNear = 0.25;
  if (S.lab) WW_IND.flatMap(x => x[3]).forEach(p0 => { if (S.lab[p0[0]] === undefined) S.lab[p0[0]] = ((S.ww && S.ww.config) || {})[p0[0]] ?? p0[5]; });
  if (S.lab && 'pvSetup' in S.lab) delete S.lab.pvSetup;
  if (S.lab) {                                   // rules added since the lab was saved: same value as what the lab really runs with
    const c0 = (S.ww && S.ww.config) || {};
    WW_RULES.forEach(r => { if (S.lab[r[0]] === undefined) S.lab[r[0]] = c0[r[0]] ?? (WW_DEF_ON[r[0]] ?? true);
      [r[4], r[5]].forEach(f => { if (f && S.lab[f[1]] === undefined) S.lab[f[1]] = c0[f[1]] ?? f[5]; }); });
  }
  if (S.lab && S.lab.kcDaily === undefined) S.lab.kcDaily = ((S.ww && S.ww.config) || {}).kcDaily !== false;
  if (S.lab && S.lab.fvgDays === undefined) S.lab.fvgDays = 10;
  return S.lab;
}
const labLive = () => { const c = (S.ww && S.ww.config) || {}, L = labCfg() || {};
  const def = k => { const r = WW_RULES.find(x => x[0] === k); if (r) return WW_DEF_ON[k] ?? true; const ip = WW_IND.flatMap(x => x[3]).find(x => x[0] === k); if (ip) return ip[5]; const f = WW_RULES.find(x => x[4] && x[4][1] === k); if (f) return f[4][5]; const f2 = WW_RULES.find(x => x[5] && x[5][1] === k); return f2 ? f2[5][5] : k === 'fvgNear' ? 0.25 : k === 'fvgDays' ? 10 : k === 'kcDaily' ? true : undefined; };
  return Object.keys(L).filter(k => L[k] !== undefined && String(L[k]) !== String(c[k] ?? def(k))).length; };
async function loadLabChart(sym) {
  const k = fcKey('ww-lab-' + sym); S.fcBusy[k] = true; if (route() === 'botchart') render();
  try { S.fc[k] = await api('/bot/lab/chart', { method: 'POST', body: { t: sym, days: S.wwDays || 45, cfg: labCfg(), flow: !!S.fcLayers.flow } }); } catch (e) { S.fc[k] = { error: e.message }; }
  S.fcBusy[k] = false; if (route() === 'botchart') render();
}
async function loadOptTest() {
  try { S.optTest = await api('/bot/lab/options'); } catch (e) { S.optTest = { error: e.message }; }
  if (route() === 'botchart') render();
  clearTimeout(S.optPoll);
  if (S.optTest && S.optTest.running && route() === 'botchart') S.optPoll = setTimeout(loadOptTest, 15000);
}
function optParams() { let v = {}; try { v = JSON.parse(localStorage.getItem('optParams') || '{}') || {}; } catch (e) {}
  return { sel: 'live', dteMin: 7, dteMax: 21, money: 0, tp: 50, sl: 40, slip: 2, follow: true, ...v }; }
function optTestBox() {
  const t = S.optTest || {}, P = optParams();
  const pc = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${v}%</b>`;
  const inp = (id, v, w, step) => `<input id="${id}" type="number" step="${step || 1}" value="${v}" style="width:${w}em;padding:1px 4px">`;
  const lab = 'class="muted" style="font-size:.8rem;display:inline-flex;gap:3px;align-items:center"';
  const head = `<h3 id="opt-test" style="font-size:.95rem;margin:14px 0 4px">Option test <span class="muted" style="font-weight:400;font-size:.8rem">the last lab test's trades, each bought as a real call (Alpaca option prices, from Feb 2024)</span></h3>
    <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:4px 0">
    <span ${lab} title="Same as the live bot: its expiration window and delta window (Bot settings), delta computed from each option's price that day">contract <select id="opt-sel" style="padding:1px 4px"><option value="live" ${P.sel !== 'manual' ? 'selected' : ''}>same as the live bot</option><option value="manual" ${P.sel === 'manual' ? 'selected' : ''}>manual: days + strike %</option></select></span>
    ${P.sel === 'manual' ? `<span ${lab}>expiration ${inp('opt-dmin', P.dteMin, 3.4)}–${inp('opt-dmax', P.dteMax, 3.4)} days</span>
    <span ${lab} title="Strike vs the stock entry price: 0 = at the money, -5 = 5% in the money, +5 = 5% out of the money">strike ${inp('opt-mon', P.money, 3.6, 0.5)}% from price</span>` : `<span class="muted" style="font-size:.78rem">${(t.params && t.params.sel === 'live') ? `live window: ${t.params.dteMin}–${t.params.dteMax} days, delta ${t.params.deltaMin}–${t.params.deltaMax}` : 'live window from Bot settings'}</span>`}
    <span ${lab} title="Sell when the option is up this much (0 = no target)">take profit ${inp('opt-tp', P.tp, 3.6, 5)}%</span>
    <span ${lab} title="Sell when the option is down this much (0 = no stop)">stop ${inp('opt-sl', P.sl, 3.4, 5)}%</span>
    <span ${lab} title="Paid on the way in and out (no historical bid/ask)">slippage ${inp('opt-slip', P.slip, 3, 0.5)}%</span>
    <label ${lab} title="Also sell when the stock trade exits"><input type="checkbox" id="opt-follow" ${P.follow ? 'checked' : ''}>sell on the stock exit</label>
    <button class="btn coachbtn" data-optrun ${t.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 12px">${t.running ? 'Pricing options…' : 'Run option test'}</button></div>`;
  if (t.error) return head + `<div class="errbox">${esc(t.error)}</div>`;
  if (t.running) return head + '<p class="loading">Picking a contract and pulling its prices for each trade… (a few minutes)</p>';
  if (!t.mine) return head + `${(t.errors || []).length ? `<p class="loss" style="font-size:.82rem">${esc(t.errors.join(' · '))}</p>` : '<p class="muted" style="font-size:.82rem">Run Test on all tickers first, then Run option test.</p>'}`;
  const m = t.mine, st = t.stock || {}, p = t.params || {};
  const lbl = g => `${g.tp ? '+' + g.tp + '%' : 'no target'} / ${g.sl ? '−' + g.sl + '%' : 'no stop'}${g.follow ? ' + stock exit' : ''}`;
  return head + `<p class="muted" style="font-size:.8rem;margin:2px 0">Run ${esc((t.at || '').slice(5, 16).replace('T', ' '))} · ${t.priced} of ${t.tested} trades priced${t.skipped ? ` (${t.skipped} without option prints)` : ''} · calls ${p.dteMin}–${p.dteMax} days, ${p.sel === 'live' ? `delta ${p.deltaMin}–${p.deltaMax} (live bot)` : `strike ${p.money > 0 ? '+' : ''}${p.money}%`} · slippage ${p.slip}% each way</p>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.84rem"><thead><tr><th></th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg</th><th class="r">Median</th><th class="r">Avg win / loss</th><th class="r">PF</th></tr></thead><tbody>
    <tr><td><b>Option</b> ${esc(lbl({ tp: p.tp, sl: p.sl, follow: p.follow }))}</td><td class="r">${m.n}</td><td class="r">${m.win ?? '—'}%</td><td class="r">${pc(m.avg)}</td><td class="r">${pc(m.med)}</td><td class="r">${pc(m.avgWin)} / ${pc(m.avgLoss)}</td><td class="r">${m.pf ?? '—'}</td></tr>
    <tr><td class="muted">Same trades, stock (R)</td><td class="r">${st.n || 0}</td><td class="r">${st.win ?? '—'}%</td><td class="r">${st.avgR != null ? (st.avgR > 0 ? '+' : '') + st.avgR + 'R' : '—'}</td><td></td><td></td><td class="r">${st.pf ?? '—'}</td></tr></tbody></table></div>
    <p class="muted" style="font-size:.82rem;margin:4px 0">Option exits: ${Object.entries(m.exits || {}).map(([k, v]) => `${esc(k)} ${v}`).join(' · ')}</p>
    <details open style="margin:4px 0"><summary style="font-size:.82rem;cursor:pointer">Take profit × stop on the same option prices (best average first)</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.8rem"><thead><tr><th>Target / stop</th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg</th><th class="r">Median</th><th class="r">Avg win / loss</th><th class="r">PF</th><th class="r">Total</th></tr></thead><tbody>
    ${(t.grid || []).slice(0, 20).map(g => `<tr><td>${esc(lbl(g))}</td><td class="r">${g.n}</td><td class="r">${g.win ?? '—'}%</td><td class="r">${pc(g.avg)}</td><td class="r">${pc(g.med)}</td><td class="r">${pc(g.avgWin)} / ${pc(g.avgLoss)}</td><td class="r">${g.pf ?? '—'}</td><td class="r">${pc(g.tot)}</td></tr>`).join('')}</tbody></table></div></details>
    <details style="margin:4px 0"><summary style="font-size:.82rem;cursor:pointer">Option trades (last ${(t.trades || []).length})</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.8rem"><thead><tr><th>Ticker</th><th>Entry</th><th>Contract</th><th class="r">Paid</th><th class="r">Best</th><th>Exit</th><th class="r">Option</th><th class="r">Stock</th></tr></thead><tbody>
    ${(t.trades || []).slice().reverse().map(x => `<tr><td><a href="#botchart?t=${encodeURIComponent(x.sym)}">${esc(x.sym)}</a></td><td>${esc(x.at.replace('T', ' ').slice(2))}</td><td>${esc(x.opt.exp.slice(2))} ${x.opt.strike}C</td><td class="r">${x.opt.entry}</td><td class="r">${pc(x.opt.mfe)}</td><td>${esc(x.opt.why || '')} ${esc((x.opt.exitAt || '').replace('T', ' ').slice(5))}</td><td class="r">${pc(x.opt.pct)}</td><td class="r">${x.R > 0 ? '+' : ''}${x.R}R</td></tr>`).join('')}</tbody></table></div></details>
    ${(t.errors || []).length ? `<p class="loss" style="font-size:.82rem">${esc(t.errors.join(' · '))}</p>` : ''}`;
}
async function loadLabTest() {
  try { S.labTest = await api('/bot/lab/test'); } catch (e) { S.labTest = { error: e.message }; }
  if (route() === 'botchart') render();
  clearTimeout(S.labPoll);
  if (S.labTest && S.labTest.running && route() === 'botchart') S.labPoll = setTimeout(loadLabTest, 15000);
}
function labRedraw() { labSave(); const sym = labSymbol(); Object.keys(S.fc).filter(x => x.includes('ww-lab-')).forEach(x => { delete S.fc[x]; }); clearTimeout(S.labT); S.labT = setTimeout(() => loadLabChart(sym), 600); render(); }
function labSymbol() { const l = wwTickers(), t = (hashParam('t') || '').toUpperCase(); return t || l[0] || ''; }
try { S.chartLab = localStorage.getItem('chartLab') === '1'; } catch (e) { S.chartLab = false; }
const HOURLY_ONLY = ['alertReq', 'relFlow', 'vwapReset', 'usePW', 'useFlip', 'vwapOn', 'emaOverVwap', 'skewOn', 'spyUp', 'kchLower', 'orbAbove', 'pvEod', 'vwapExit', 'dpLost'];
const DAILY_PRESET = { tf: '1D', pwTest: true, inGreen: true, fvgOn: true, inYExit: true, greenOnly: false, inNotRed: false, emaOn: false, kcdLower: false, kcNotExt: false, aboveFlip: false, abovePW: false, belowPW: false, cvdUp: false, cvdDn: false, noAboveCW: false, cwRoomOn: false, earnOn: false, pwNeCw: false, stopOn: true, trailOn: false, targetOn: false, flipExit: false, pwExit: false, kcRide: false, cwLost: false, fvgExit: false, scaleOn: false, inRedExit: false, emaExit: false };
function labPanel() {
  const L = labCfg() || {};
  const tf = (S.labDraft && S.labDraft.tf) || L.tf || '1h';
  const shown = r => r[0] === 'scaleBE' ? !!{ ...L, ...(S.labSw || {}) }.scaleOn : tf === '1D' ? !HOURLY_ONLY.includes(r[0]) : r[0] !== 'pwTest';
  const diff = labLive();
  return `<div style="display:flex;flex-direction:column;gap:6px;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0">
    <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center"><b style="font-size:.8rem;min-width:3.6em">Timeframe</b>
      <span class="seg" role="group"><button class="btn ${tf === '1h' ? 'coachbtn' : ''}" data-labtf="1h" style="font-size:.78rem;padding:1px 10px" title="The strategy on hourly candles">1h</button><button class="btn ${tf === '1D' ? 'coachbtn' : ''}" data-labtf="1D" style="font-size:.78rem;padding:1px 10px" title="The strategy on daily candles: entry at the daily close, exits checked on each daily candle">1D</button></span>
      <button class="btn" data-labpreset="daily" style="font-size:.75rem;padding:1px 9px" title="Daily candles · entry: put wall test + daily inertia green + an open bullish daily FVG under the close · exits: stop, inertia not green">Daily preset: Green + FVG + PW test → exit on yellow</button>
      ${tf === '1D' ? '<span class="muted" style="font-size:.75rem">Daily: FVG = an open bullish daily gap at or under the close (min size / max age in the FVG row, in days); the hourly-only rules are hidden.</span>' : ''}</div>
    ${wwRulePanel({ ...L, ...(S.labDraft || {}) }, { ...L, ...(S.labSw || {}) }, 'lab', shown)}
    ${fxRow([['fxEntry', 'Entry'], ['fxExit', 'Exit']], { ...L, ...(S.labDraft || {}) }, 'labfx')}
    <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center"><button class="btn coachbtn" data-labapply data-applybtn="lab" style="font-size:.8rem;padding:1px 12px">${S.labDirty ? 'Apply changes' : 'Apply'}</button>
    <button class="btn" data-labtest ${S.labTest && S.labTest.running ? 'disabled' : ''} style="font-size:.8rem;padding:1px 12px" title="These settings vs your live settings, on every ticker of the watchlist">${S.labTest && S.labTest.running ? 'Testing…' : 'Test on all tickers'}</button>
    <button class="btn" data-optshow style="font-size:.8rem;padding:1px 12px" title="Price the lab trades with real call options (results under the chart)">Option test ↓</button>
    <a class="btn" href="#botwatch" data-rtlab style="font-size:.8rem;padding:1px 10px" title="Rule tester and robustness check on these lab settings (Wall watch tab)">Full stats ↗</a>
    <button class="btn" data-labreset style="font-size:.8rem;padding:1px 10px" title="Start over from a copy of the live settings (the live bot is not changed)">Copy live settings</button>
    <button class="btn" data-labtolive style="font-size:.8rem;padding:1px 10px" title="Make the live bot use these lab settings (every rule and number; the 1D timeframe and Put wall test stay lab only)">Use these settings live</button>
    <span class="muted" style="font-size:.75rem">Lab only: these settings are kept apart and never change the live bot or its signals${diff ? ` · ${diff} differ from live` : ''}. Nothing changes until you click Apply.</span></div></div>`;
}
S.optSym = S.optSym || {};
async function loadOptSym(sym) {
  try { S.optSym[sym] = await api(`/bot/lab/chartoptions?t=${encodeURIComponent(sym)}`); } catch (e) { S.optSym[sym] = { error: e.message }; }
  if (route() === 'botchart') render();
  clearTimeout(S.optSymPoll);
  if (S.optSym[sym] && S.optSym[sym].running && route() === 'botchart') S.optSymPoll = setTimeout(() => loadOptSym(sym), 8000);
}
function optSymRun(sym) {
  const d = S.fc[fcKey('ww-lab-' + sym)] || {};
  const trades = (d.signals || []).map(x => ({ at: x.at, entry: x.entry, exitAt: x.exitAt, exit: x.status, R: x.R, pct: x.pct }));
  if (!trades.length) return;
  S.optSym[sym] = { ...(S.optSym[sym] || {}), running: 'now' }; if (route() === 'botchart') render();
  api('/bot/lab/chartoptions', { method: 'POST', body: { t: sym, trades, ...optParams() } })
    .then(() => { clearTimeout(S.optSymPoll); S.optSymPoll = setTimeout(() => loadOptSym(sym), 8000); })
    .catch(err => { S.optSym[sym] = { error: err.message }; if (route() === 'botchart') render(); });
}
function optTag(sym, x) {                       // the call bought for this lab trade, on its button
  const o = ((S.optSym[sym] || {}).byAt || {})[x.at];
  if (!o) return '';
  if (o.none) return ` <span class="muted" title="${esc(o.none)}">│ no call</span>`;
  return ` <span title="${esc(o.sym)} bought ${o.entry} · ${esc(o.why || '')} ${esc((o.exitAt || '').replace('T', ' '))} · best +${o.mfe}%" style="border-left:1px solid var(--line);padding-left:4px;margin-left:2px">call ${o.strike} ${esc(o.exp.slice(5))} <b class="${o.pct > 0 ? 'gain' : o.pct < 0 ? 'loss' : ''}">${o.pct > 0 ? '+' : ''}${o.pct}%</b></span>`;
}
function optSymBox(sym, sigs, sel) {
  const r = S.optSym[sym];
  if (r === undefined) { S.optSym[sym] = null; setTimeout(() => loadOptSym(sym), 0); }
  const x = r || {}, P = optParams(), st = x.stats || {};
  // automatic: price this ticker's trades as calls once they're loaded (again when the lab trades change)
  const key = sym + '|' + sigs.map(s0 => s0.at).join(',') + '|' + JSON.stringify(P);
  S.optAuto = S.optAuto || {};
  if (r && !r.running && !r.error && sigs.length && !S.optAuto[key]) {
    const have = x.byAt || {}, missing = sigs.filter(s0 => s0.at >= '2024-02-05' && !(s0.at in have)).length;
    const pp = x.params || {}, same = (pp.sel || 'manual') === P.sel && (P.sel === 'live' ? ['tp', 'sl', 'slip'] : ['dteMin', 'dteMax', 'money', 'tp', 'sl', 'slip']).every(k => +pp[k] === +P[k]) && !!pp.follow === !!P.follow;
    S.optAuto[key] = true;
    if (missing || !same || x.v !== 4) setTimeout(() => optSymRun(sym), 0);   // v2: stray prints filtered
  }
  const pc = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${v}%</b>`;
  const o = sel && x.byAt ? x.byAt[sel.at] : null;
  const btn = `<button class="btn coachbtn" data-optsym="${esc(sym)}" ${x.running || !sigs.length ? 'disabled' : ''} style="font-size:.78rem;padding:1px 10px" title="Each of these trades bought as a real call: ${P.dteMin}–${P.dteMax} days, strike ${P.money}% from price, take profit ${P.tp}%, stop ${P.sl}%${P.follow ? ', sold on the stock exit' : ''}, slippage ${P.slip}% (set in the Option test section)">${x.running ? 'Pricing calls…' : x.byAt ? 'Re-price calls' : 'Buy calls for these trades'}</button>`;
  return `<span style="display:inline-flex;flex-wrap:wrap;gap:8px;align-items:center">${btn}
    ${x.stats && st.n ? `<span><b>Calls</b> ${st.n} trades · win ${st.win}% · avg ${pc(st.avg)} · median ${pc(st.med)} · avg win / loss ${pc(st.avgWin)} / ${pc(st.avgLoss)} · total ${pc(st.tot)}${x.params ? ` <span class="muted">(${x.params.sel === 'live' ? `live bot: ${x.params.dteMin}–${x.params.dteMax}d, delta ${x.params.deltaMin}–${x.params.deltaMax}` : `${x.params.dteMin}–${x.params.dteMax}d`}, TP ${x.params.tp || 'none'}${x.params.tp ? '%' : ''}, stop ${x.params.sl || 'none'}${x.params.sl ? '%' : ''})</span>` : ''}</span>` : ''}
    ${x.older ? `<span class="muted">${x.older} trades before Feb 2024: no option prices</span>` : ''}
    ${o && !o.none ? (() => { const bs0 = (S.fc[fcKey('ww-lab-' + sym)] || {}).bars || [], px = t => { let v = null; bs0.forEach(b0 => { if (b0.t < t) v = b0.c; }); return v; };
      const se = px(o.entryAt || sel.at), sx = o.exitAt ? px(o.exitAt) : null, sm = o.maeAt ? px(o.maeAt) : null;
      return `<span class="muted">${esc(sym)} ${se != null ? se.toFixed(2) : '—'} at entry · option worst ${pc(o.mae)} ${esc((o.maeAt || '').replace('T', ' ').slice(5))}${sm != null ? ` (${esc(sym)} ${sm.toFixed(2)}, ${se ? ((sm / se - 1) * 100).toFixed(1) + '%' : ''})` : ''} · at the option exit ${esc(sym)} ${sx != null ? sx.toFixed(2) : '—'}${sx != null && se ? ` (${((sx / se - 1) * 100).toFixed(1)}%)` : ''}</span>`; })() : ''}
    ${o ? (o.none ? `<span class="muted">Selected trade: ${esc(o.none)}</span>` : `<span><b>Selected trade</b> bought <b>${esc(o.sym)}</b> (${esc(o.exp)} ${o.strike} call${o.delta != null ? `, delta ${o.delta}` : ''}) at ${o.entry}${o.entryAt ? ` (${esc(o.entryAt.replace('T', ' ').slice(5))})` : ''} → sold ${o.exitPx ?? '—'} · ${esc(o.why || '')} ${esc((o.exitAt || '').replace('T', ' ').slice(5))} → ${pc(o.pct)} · best ${pc(o.mfe)}</span>`) : ''}
    ${(x.errors || []).length ? `<span class="loss">${esc(x.errors.join(' · '))}</span>` : ''}${x.error ? `<span class="loss">${esc(x.error)}</span>` : ''}</span>`;
}
function labStats(sigs) {
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const rs = sigs.filter(x => x.R != null), w = rs.filter(x => x.R > 0), tot = rs.reduce((a, x) => a + x.R, 0);
  const ex = {}; sigs.forEach(x => { ex[x.status] = (ex[x.status] || 0) + 1; });
  return `<span><b>This ticker</b> ${sigs.length} trade${sigs.length === 1 ? '' : 's'} · win ${rs.length ? Math.round(w.length / rs.length * 100) : 0}% · avg ${R(rs.length ? tot / rs.length : null)} · total ${R(tot)} · ${Object.entries(ex).map(([k, v]) => `${esc(k)} ${v}`).join(' · ')}</span>`;
}
function labTestBox() {
  const t = S.labTest || {};
  if (t.error) return `<div class="errbox">${esc(t.error)}</div>`;
  if (t.running) return '<p class="loading">Testing these settings and your live settings on every ticker… (a few minutes)</p>';
  if (!t.lab) return '';
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const row = (lab, x) => `<tr><td>${lab}</td><td class="r">${x.n}</td><td class="r">${x.win ?? '—'}%</td><td class="r">${R(x.avgR)}</td><td class="r">${R(x.totR)}</td><td class="r">${x.pf ?? '—'}</td><td class="r">${R(x.h1)} / ${R(x.h2)}<br><span class="muted" style="font-size:.75rem">${x.h1n} / ${x.h2n}</span></td><td class="r">${x.top3Share != null ? x.top3Share + '%' : '—'}</td><td class="r">${x.open ?? 0}</td></tr>`;
  const changed = Object.keys(t.cfg || {}).length;
  return `<h3 style="font-size:.95rem;margin:12px 0 4px">All tickers <span class="muted" style="font-weight:400;font-size:.8rem">${t.tickers} tickers since ${esc((t.period || {}).from || '')} · run ${esc((t.at || '').slice(5, 16).replace('T', ' '))}</span></h3>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.84rem"><thead><tr><th>Settings</th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg R</th><th class="r">Total R</th><th class="r">PF</th><th class="r">1st / 2nd half</th><th class="r">Top 3 tickers</th><th class="r">Open</th></tr></thead>
    <tbody>${row(`<b>Lab</b>${changed ? '' : ' <span class="muted">(same as live when run)</span>'}`, t.lab)}${row('Live', t.live)}</tbody></table></div>
    <p class="muted" style="font-size:.82rem;margin:4px 0">Lab exits: ${Object.entries(t.lab.exits || {}).map(([k, v]) => `${esc(k)} ${v}`).join(' · ')} · best / worst tickers: ${(t.lab.top || []).map(x => `${esc(x.sym)} ${x.totR > 0 ? '+' : ''}${x.totR}R`).join(' · ')}</p>
    ${(t.trades || []).length ? `<details style="margin:4px 0"><summary style="font-size:.82rem;cursor:pointer">Lab trades (last ${t.trades.length}) — click one to open its chart</summary><div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">
      ${t.trades.slice().reverse().map(x => `<a class="btn" href="#botchart?t=${encodeURIComponent(x.sym)}" style="font-size:.75rem;padding:1px 7px">${esc(x.sym)} ${esc(x.at.slice(5, 10))} <b class="${x.R > 0 ? 'gain' : 'loss'}">${x.R > 0 ? '+' : ''}${x.R}R</b></a>`).join('')}</div></details>` : ''}
    ${(t.errors || []).length ? `<p class="loss" style="font-size:.82rem">${esc(t.errors.join(' · '))}</p>` : ''}`;
}
function pvTrades(t) {
  if (!t) return '';
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const EX = { target: 'call wall target', stop: 'stop', trail: 'trailing stop', flip: 'close under flip', vwap: 'close under VWAP', ema: 'close under EMA', keltner: 'Keltner ride ended', lost: 'back under level', eodpw: 'entry day closed under PW', fvg: 'new bearish FVG', breakeven: 'rest stopped at entry', inertia: 'inertia exit', inertiaH: 'hourly inertia exit', squeeze: 'squeeze momentum down', pwlost: 'close under PW', cvd: 'CVD exit', open: 'still open' };
  if (!t.n) return '<h4 style="font-size:.9rem;margin:14px 0 4px">LONG signal traded with your exits</h4><p class="muted" style="font-size:.85rem">No trades.</p>';
  return `<h4 style="font-size:.9rem;margin:14px 0 4px">LONG signal traded with your exits</h4>
    <p class="muted" style="font-size:.82rem;margin:0 0 6px">Entry at the close of the reset candle · stop = put wall − stop ATR · target = call wall · same switches as the strategy (Keltner ride, close under flip, back under CW / dark pool…) · the entry day must close above the put wall, else sold at that close. One trade at a time per ticker.</p>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.84rem"><thead><tr><th class="r">Trades</th><th class="r">Win rate</th><th class="r">Avg R</th><th class="r">Total R</th><th class="r">Profit factor</th><th class="r">Avg win / loss</th><th class="r">1st half / 2nd half</th><th class="r">Avg days held</th><th class="r">Still open</th></tr></thead>
    <tbody><tr><td class="r">${t.n}</td><td class="r">${t.win}%</td><td class="r">${R(t.avgR)}</td><td class="r">${R(t.totR)}</td><td class="r">${t.pf ?? '—'}</td><td class="r">${R(t.avgWin)} / ${R(t.avgLoss)}</td><td class="r">${R(t.h1)} / ${R(t.h2)}<br><span class="muted" style="font-size:.75rem">${t.h1n} / ${t.h2n}</span></td><td class="r">${t.avgDays ?? '—'}</td><td class="r">${t.open}</td></tr></tbody></table></div>
    <p class="muted" style="font-size:.82rem;margin:4px 0">Exits: ${Object.entries(t.exits || {}).map(([k, v]) => `${esc(EX[k] || k)} ${v}`).join(' · ')}</p>
    <details><summary style="font-size:.85rem;cursor:pointer">Last ${(t.list || []).length} trades</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.82rem"><thead><tr><th>Ticker</th><th>Entry</th><th class="r">Price</th><th class="r">PW</th><th class="r">CW</th><th>Exit</th><th class="r">Days</th><th class="r">%</th><th class="r">R</th></tr></thead><tbody>
    ${(t.list || []).slice().reverse().map(x => `<tr><td>${esc(x.sym)}</td><td>${esc(x.at.replace('T', ' ').slice(2))}</td><td class="r">${(+x.entry).toFixed(2)}</td><td class="r">${x.pw ?? '—'}</td><td class="r">${x.cw ?? '—'}</td><td>${esc(EX[x.exit] || x.exit)}</td><td class="r">${x.days}</td><td class="r ${x.pct > 0 ? 'gain' : 'loss'}">${x.pct > 0 ? '+' : ''}${x.pct}%</td><td class="r">${R(x.R)}</td></tr>`).join('')}</tbody></table></div></details>`;
}
/* ---------- intraday gamma levels (the NQ framework on 5-minute candles) ---------- */
async function loadIntraday() {
  S.itdBusy = true;
  try { S.itd = await api('/bot/wallwatch/intraday'); } catch (e) { S.itd = { error: e.message }; }
  S.itdBusy = false;
  if (route() === 'botwatch') render();
  clearTimeout(S.itdPoll);
  if (S.itd && S.itd.running && route() === 'botwatch') S.itdPoll = setTimeout(loadIntraday, 15000);
}
function wwIntraday() {
  const r = S.itd || {}, P = { tickers: 'QQQ SPY', sessionEnd: '11:30', fastK: 4, minRR: 1.5, maxTrades: 2, skipEvents: true, longs: true, shorts: true, watchlist: false, useFlip: true, ...(r.params || {}) };
  const R = v => v == null ? '—' : `<b class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(2)}R</b>`;
  const lab = 'style="display:inline-flex;gap:4px;align-items:center"';
  const num = (id, v, step, w) => `<input id="${id}" type="number" step="${step}" min="0" value="${v}" style="width:${w}em;padding:1px 4px">`;
  const chk = (id, v, t) => `<label ${lab}><input type="checkbox" id="${id}" ${v ? 'checked' : ''}>${t}</label>`;
  const row = (x, label) => `<tr><td>${esc(label || x.label)}</td><td class="r">${x.n || 0}</td><td class="r">${x.win ?? '—'}${x.win != null ? '%' : ''}</td><td class="r">${R(x.avgR)}</td><td class="r">${x.totR ?? '—'}</td>
    <td class="r">${R(x.h1)} <span class="muted" style="font-size:.75rem">${x.h1n ?? ''}</span></td><td class="r">${R(x.h2)} <span class="muted" style="font-size:.75rem">${x.h2n ?? ''}</span></td>
    <td>${!x.n ? '' : x.n < 30 ? '<span class="muted">too few trades</span>' : x.h1 > 0 && x.h2 > 0 ? '<b class="gain">positive in both halves</b>' : x.h1 > 0 || x.h2 > 0 ? '<span style="color:var(--warn)">one half only</span>' : '<span class="loss">negative</span>'}</td></tr>`;
  const thead = f => `<thead><tr><th>${f}</th><th class="r">Trades</th><th class="r">Win</th><th class="r">Avg R</th><th class="r">Total R</th><th class="r">1st half</th><th class="r">2nd half</th><th></th></tr></thead>`;
  const groups = {};
  (r.groups || []).forEach(g => { (groups[g.group] = groups[g.group] || []).push(g); });
  const t5 = t => (t || '').replace('T', ' ').slice(5, 16);
  const body = r.running ? '<p class="loading">Testing on 5-minute candles… (a few minutes; walls of missing days are fetched once)</p>' : !r.all ? '<p class="muted">Run the test to see the results.</p>' : `
    <p class="muted" style="margin:6px 0">${r.tickers} tickers · ${r.days} ticker-days with walls · ${esc(r.period.from)} → ${esc(r.period.to)} (5-minute candles: Yahoo keeps ~60 days) · halves split at ${esc(r.period.mid)}</p>
    <div style="overflow-x:auto"><table class="pos" style="font-size:.85rem">${thead('All trades')}<tbody>${row(r.all, 'Every setup')}</tbody></table></div>
    <div style="overflow-x:auto;margin-top:10px"><table class="pos" style="font-size:.85rem">${thead('Exits')}<tbody>${(r.exits || []).map(x => row(x)).join('')}</tbody></table></div>
    <div style="overflow-x:auto;margin-top:10px"><table class="pos" style="font-size:.85rem">${thead('Split by')}<tbody>${Object.entries(groups).map(([g, bs]) => `<tr><td colspan="8" style="padding-top:8px"><b>${esc(g)}</b></td></tr>` + bs.map(b => row(b, b.label)).join('')).join('')}</tbody></table></div>
    ${(r.bySymbol || []).length ? `<details style="margin-top:8px"><summary class="muted" style="cursor:pointer">By ticker</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.85rem">${thead('Ticker')}<tbody>${r.bySymbol.map(x => row(x, x.sym)).join('')}</tbody></table></div></details>` : ''}
    ${(r.recent || []).length ? `<details style="margin-top:8px"><summary class="muted" style="cursor:pointer">Last ${r.recent.length} trades</summary><div style="overflow-x:auto"><table class="pos" style="font-size:.82rem"><thead><tr><th>Ticker</th><th>Entry</th><th>Side</th><th>Level</th><th>Setup</th><th class="r">Entry</th><th class="r">Stop</th><th class="r">Target</th><th>Exit</th><th class="r">R</th></tr></thead><tbody>${r.recent.slice().reverse().map(t => `<tr><td><a href="#botchart?t=${encodeURIComponent(t.sym)}">${esc(t.sym)}</a></td><td>${esc(t5(t.at))}</td><td>${t.side}</td><td>${esc(t.levelName)} ${t.levelPx}</td><td>${t.kind === 'reclaim' ? 'overshoot + reclaim' : 'rejection'}</td><td class="r">${(+t.entry).toFixed(2)}</td><td class="r">${(+t.stop).toFixed(2)}</td><td class="r">${(+t.target).toFixed(2)}</td><td>${esc(t.status)} ${esc(t5(t.exitAt))}</td><td class="r">${R(t.R)}</td></tr>`).join('')}</tbody></table></div></details>` : ''}
    ${(r.errors || []).length ? `<p class="loss" style="font-size:.85rem">${esc(r.errors.join(' · '))}</p>` : ''}`;
  return `<section class="panel" style="margin:0 0 14px"><h2>Intraday gamma levels <span class="muted" style="font-weight:400;font-size:.85rem">the NQ day-trading framework on 5-minute candles</span></h2>
    <p class="muted" style="margin:0 0 8px">Levels of the day: put wall, flip, call wall. Long at the put wall / flip from above, short at the call wall / flip from below. Slow approach → rejection candle at the level; fast approach → overshoot, then reclaim. Stop beyond the reaction, target the next level, flat at the close, a level that stopped you isn’t traded again that day. Not testable: futures order flow, 0DTE-only gamma, skew, convexity.</p>
    <div style="display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;font-size:.85rem">
      <label ${lab}>Tickers <input id="itd-tk" value="${esc(P.tickers)}" style="width:9em;padding:1px 4px"></label> ${chk('itd-wl', P.watchlist, '+ watchlist')}
      <label ${lab}>entries until <input id="itd-end" value="${esc(P.sessionEnd)}" style="width:4em;padding:1px 4px"></label>
      <label ${lab} title="Fast approach = the last 30 minutes moved more than this × the 5-minute ATR (then only the overshoot + reclaim is traded)">fast = ${num('itd-fk', P.fastK, 0.5, 3.4)} × 5-min ATR</label>
      <label ${lab} title="Skip when the next level gives less than this reward : risk">min reward:risk ${num('itd-rr', P.minRR, 0.5, 3.4)}</label>
      <label ${lab}>max ${num('itd-mt', P.maxTrades, 1, 2.8)} trades / day</label>
      ${chk('itd-ev', P.skipEvents, 'skip OPEX / triple witching / VIX expiration')} ${chk('itd-l', P.longs, 'longs')} ${chk('itd-s', P.shorts, 'shorts')} ${chk('itd-f', P.useFlip, 'flip as a level')}
      <button class="btn coachbtn" data-itdrun ${r.running ? 'disabled' : ''}>Run the test</button>${r.at && !r.running ? ` <span class="muted">last run ${esc(r.at.slice(5, 16).replace('T', ' '))} · ${r.secs}s</span>` : ''}</div>
    ${body}</section>`;
}
function vWatch() {
  const h = head('Wall watch', 'Flow retest: a call alert of at least the chosen size arms the ticker; within the chosen days, a retest of the support under the price (the ON rules below decide which support and which candles qualify) is the entry. Exits: the ON exit rules below. Unused rules are hidden under “Show all rules”; other tools are at the bottom of the page.');
  const d = S.ww;
  if (!d) return h + '<p class="loading">Loading…</p>';
  if (d.error) return h + `<div class="errbox">${esc(d.error)}</div>`;
  const sm = d.summary || {}, run = d.run || {}, cfg = d.config || {};
  const pc = (v, dg = 2) => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${v > 0 ? '+' : ''}${(+v).toFixed(dg)}%</span>`;
  const t5 = t => (t || '').replace('T', ' ').slice(5, 16);
  const stats = `<div class="strip" style="margin:0 0 12px">
    <div class="stat"><span>Signals</span><b>${sm.signals || 0}</b><span>${sm.open || 0} open · ${sm.closed || 0} finished</span></div>
    <div class="stat"><span>Reached the call wall</span><b>${sm.hitPct ?? '—'}%</b><span>${sm.targets || 0} of ${sm.closed || 0} finished</span></div>
    <div class="stat"><span>Average result</span><b>${pc(sm.avgPct)}</b><span>${sm.avgR != null ? (sm.avgR > 0 ? '+' : '') + sm.avgR + 'R · ' : ''}total ${pc(sm.totalPct)} (stock move)</span></div>
    <div class="stat"><span>Target / trailing stop / stop</span><b style="font-size:1.05rem">${pc(sm.avgWin)} / ${pc(sm.avgTrail)} / ${pc(sm.avgLoss)}</b><span>${sm.targets || 0} / ${sm.trails || 0} / ${(sm.closed || 0) - (sm.targets || 0) - (sm.trails || 0)} trades · target ${pc(sm.avgTgt)} vs stop ${pc(sm.avgRisk == null ? null : -sm.avgRisk)} from entry</span></div></div>`;
  const runLine = `<p class="muted" style="font-size:.84rem;margin:0 0 12px">${run.at ? `Last check ${esc(t5(run.at))} · ${run.checked ?? 0} tickers` : 'Not checked yet'}${run.running ? ' · <b>checking…</b>' : ''}${run.bfAt ? ` · history: last ${run.bfDays} days${run.bfLeft ? ` (${run.bfLeft} tickers left: run again)` : ''}` : ''}${(run.errors || []).length ? ` · <span class="loss" title="${esc((run.errors || []).join('\n'))}">${run.errors.length} errors</span>` : ''}
    <button class="btn coachbtn" data-wwrun="3" style="font-size:.8rem;padding:2px 10px">Check now</button>
    ${wwFillBtn()}
    <button class="btn" data-wwchart="${esc((cfg.tickers || [])[0] || '')}|" style="font-size:.8rem;padding:2px 10px">Open the charts</button>
    <label style="margin-left:6px"><input type="checkbox" id="ww-on" ${cfg.enabled ? 'checked' : ''}> every hour</label></p>`;
  const sigs = d.signals || [];
  const st = s => s.status === 'cvd' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">CVD ${s.exitWhy === 'cvdRiseX' ? 'rising' : 'falling'}</b>` : s.status === 'pwlost' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">closed under the PW</b>` : s.status === 'inertiaH' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">hourly inertia exit</b>` : s.status === 'inertia' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">inertia exit</b>` : s.status === 'breakeven' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">rest stopped at entry</b>` : s.status === 'fvg' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">new bearish FVG</b>` : s.status === 'eodpw' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">entry day closed under PW</b>` : s.status === 'target' ? '<b class="gain">target</b>' : s.status === 'stop' ? '<b class="loss">stop</b>' : s.status === 'trail' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">trailing stop</b>` : s.status === 'flip' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">closed under the flip</b>` : s.status === 'vwap' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">closed under the VWAP</b>` : s.status === 'keltner' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">Keltner ride ended</b>` : s.status === 'lost' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">back under the ${esc(s.lostLevel || 'level')}</b>` : s.status === 'ema' ? `<b class="${s.pct >= 0 ? 'gain' : 'loss'}">closed under the EMA</b>` : '<span style="color:var(--coach)">open</span>';
  const sigTable = `<section class="panel"><h2>Signals</h2>${sigs.length ? `<div class="tablewrap"><table class="pos" style="font-size:.85rem"><thead><tr><th>Ticker</th><th>Setup</th><th>Candle</th><th class="r">Entry</th><th class="r">Put wall</th><th class="r">Flip</th><th class="r">Call wall</th><th class="r">Target</th><th class="r">Stop</th><th>Result</th><th class="r">Move</th><th class="r">R</th><th></th></tr></thead><tbody>
    ${sigs.map(s => `<tr><td><b>${esc(s.symbol)}</b></td><td>${s.kind === 'flip' ? '<span class="exchip">Flip</span>' : s.kind === 'merged' ? '<span class="exchip">Merged</span>' : s.setup === 'pvlong' ? '<span class="exchip" title="VWAP reset candle touching the put wall, lower Keltner half">PW · VWAP reset</span>' : s.kind === 'retest' ? `<span class="exchip" title="Armed ${esc((s.armedAt || '').replace('T', ' ').slice(5))} by a ${money(s.armPremium || 0, false)} call alert">Retest ${esc(s.levelName || '')}</span>` : '<span class="exchip">PW</span>'}</td><td>${esc(t5(s.at))}</td><td class="r">${(+s.entry).toFixed(2)}</td><td class="r">${s.pw}</td><td class="r">${s.flip}</td><td class="r">${s.cw}</td><td class="r">${s.tgtPct == null ? '<span class="muted">trail</span>' : pc(s.tgtPct, 1)}</td><td class="r" title="${s.stop0 != null ? `first stop ${s.stop0} (${s.kind === 'flip' ? 'flip' : 'put wall'} − N × 30-day ATR ${s.atr})${s.stop > s.stop0 ? ` · trailed to ${s.stop}` : ''}` : ''}">${pc(-s.riskPct, 1)}</td>
      <td>${s.botId ? '<span class="exchip" title="A call was bought on Alpaca paper">traded</span> ' : s.tradeError ? `<span class="exchip" title="${esc(s.tradeError)}">not traded</span> ` : ''}${st(s)}${s.exitAt ? ` <span class="muted">${esc(t5(s.exitAt))}</span>` : s.lastAt ? ` <span class="muted">now ${(+s.last).toFixed(2)}</span>` : ''}</td><td class="r">${pc(s.pct)}</td><td class="r">${s.R == null ? '—' : (s.R > 0 ? '+' : '') + s.R}</td><td><button class="btn" data-wwchart="${esc(s.symbol)}|${esc(s.at)}" style="font-size:.75rem;padding:1px 7px">chart</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="muted" style="margin:0">No signal yet. Press <b>Get all walls (45 days)</b> to see how often it happened.</p>'}</section>`;
  const by = (d.byTicker || []).length ? `<section class="panel" style="margin-top:14px"><details><summary class="muted" style="cursor:pointer">By ticker (finished signals)</summary><div class="tablewrap"><table class="pos" style="font-size:.85rem;margin-top:8px"><thead><tr><th>Ticker</th><th class="r">Signals</th><th class="r">Targets</th><th class="r">Total move</th></tr></thead><tbody>
    ${d.byTicker.map(b => `<tr><td><b>${esc(b.symbol)}</b></td><td class="r">${b.n}</td><td class="r">${b.targets}</td><td class="r">${pc(b.total)}</td></tr>`).join('')}</tbody></table></div></details></section>` : '';
  const stMap = Object.fromEntries((d.status || []).map(x => [x.symbol, x]));
  const now = `<section class="panel" style="margin-top:14px"><h2>Now</h2><div class="tablewrap"><table class="pos" style="font-size:.85rem"><thead><tr><th>Ticker</th><th class="r">Last close</th><th class="r">Put wall</th><th class="r">Flip</th><th class="r">Call wall</th><th>Setup now</th><th class="r">Candle</th><th></th></tr></thead><tbody>
    ${(cfg.tickers || []).map(t => { const x = stMap[t] || {}, w = x.walls || {}; return `<tr><td><b>${esc(t)}</b></td><td class="r">${x.price != null ? (+x.price).toFixed(2) : '—'}</td><td class="r">${w.putWall ?? '—'}</td><td class="r">${w.flip ?? '—'}</td><td class="r">${w.callWall ?? '—'}</td><td>${x.watch ? `<b style="color:var(--coach)" title="Armed ${esc((x.watch.armedAt || '').replace('T', ' ').slice(5))} by a ${money(x.watch.armPremium || 0, false)} call alert">${x.watch.level ? `waiting for a retest of the ${esc(x.watch.levelName)} ${x.watch.level}` : 'waiting for a call alert to arm a retest'}</b><br>` : ''}${x.walls ? (x.setup === 'merged' ? '<b class="gain">PW = CW < flip</b>' : x.setup === 'flip' ? '<b class="gain">CW > PW > flip</b>' : x.setup ? '<b class="gain">CW > flip > PW</b>' : '<span class="muted">no</span>') : '<span class="muted">no walls</span>'}</td><td class="r muted">${esc(t5(x.barAt))}</td><td><button class="btn" data-wwchart="${esc(t)}|" style="font-size:.75rem;padding:1px 7px">chart</button></td></tr>`; }).join('')}</tbody></table></div>
    <details style="margin-top:10px"><summary class="muted" style="cursor:pointer">Edit the list (${(cfg.tickers || []).length} tickers)</summary>
      <textarea id="ww-list" rows="5" style="width:100%;margin-top:6px">${esc((cfg.tickers || []).join(' '))}</textarea>
      <button class="btn" data-wwsave="1">Save the list</button></details></section>`;
  const bk = d.byKind || {}, kRow = (k, l) => { const x = bk[k] || {}; return `<tr><td><b>${l}</b></td><td class="r">${x.signals || 0}</td><td class="r">${x.closed || 0}</td><td class="r">${x.hitPct ?? '—'}%</td><td class="r">${pc(x.avgPct)}</td><td class="r">${x.avgR == null ? '—' : (x.avgR > 0 ? '+' : '') + x.avgR + 'R'}</td><td class="r">${pc(x.totalPct)}</td></tr>`; };
  const kinds = `<section class="panel" style="margin:0 0 14px"><div class="cal-head"><h2>Settings</h2>${wwParams(false)}</div>
    <div class="tablewrap"><table class="pos" style="font-size:.85rem"><thead><tr><th>Setup</th><th class="r">Signals</th><th class="r">Finished</th><th class="r">Call wall reached</th><th class="r">Avg</th><th class="r">Avg R</th><th class="r">Total</th></tr></thead><tbody>${kRow('retest', 'Flow retest: call alert, then a retest of the support under the price')}</tbody></table></div></section>`;
  const fsd = d.flowStudy || {}, base = fsd.base || {}, rule = cfg.flowRule;
  const fr = (x, i) => { const better = x.n >= 5 && base.avgPct != null && x.avgPct != null && x.avgPct > base.avgPct;
    return `<tr${better ? ' style="background:color-mix(in srgb, var(--gain) 9%, transparent)"' : ''}><td>${esc(x.name)}${i === rule ? ' <span class="exchip">trading filter</span>' : ''}</td><td class="r">${x.n}</td><td class="r">${x.n ? (x.hitPct ?? '—') + '%' : '—'}</td><td class="r">${x.n ? x.winPct + '%' : '—'}</td><td class="r">${pc(x.avgPct)}</td><td class="r">${x.avgR == null ? '—' : (x.avgR > 0 ? '+' : '') + x.avgR + 'R'}</td><td class="r">${pc(x.totalPct)}</td>
      <td>${i == null ? '' : i === rule ? `<button class="btn" data-wwrule="-1" style="font-size:.75rem;padding:1px 7px">remove filter</button>` : x.n ? `<button class="btn" data-wwrule="${i}" style="font-size:.75rem;padding:1px 7px">trade only these</button>` : ''}</td></tr>`; };
  const flowBox = `<section class="panel" style="margin:0 0 14px"><div class="cal-head"><h2>Options flow vs signals</h2>
      <span><button class="btn coachbtn" data-wwflow="1" ${fsd.running || S.wwFlowBusy ? 'disabled' : ''} style="font-size:.8rem;padding:2px 10px">${fsd.running || S.wwFlowBusy ? 'Matching…' : fsd.at ? 'Match again' : 'Match the signals with the flow'}</button></span></div>
    <p class="muted" style="font-size:.84rem;margin:0 0 8px">For every finished signal: the calls and puts bought at the ask on the ticker before the signal candle closed (3 days, and the last 7 hours). Rows show how the signals did when the condition was true. Green rows beat all signals (with at least 5 signals). ${fsd.at ? `Matched ${esc((fsd.at || '').replace('T', ' ').slice(5, 16))} · ${fsd.withFlow}/${fsd.signals} signals with flow data` : ''}${(fsd.errors || []).length ? ` · <span class="loss">${fsd.errors.length} errors</span>` : ''}</p>
    ${base.n ? `<div class="tablewrap"><table class="pos" style="font-size:.85rem"><thead><tr><th>Condition before the signal</th><th class="r">Signals</th><th class="r">Call wall reached</th><th class="r">Winners</th><th class="r">Avg</th><th class="r">Avg R</th><th class="r">Total</th><th></th></tr></thead><tbody>
      ${fr({ ...base, name: 'All signals' }, null)}${(fsd.rows || []).map((x, i) => fr(x, i)).join('')}</tbody></table></div>
      <p class="muted" style="font-size:.8rem;margin:6px 0 0">${rule != null ? `Trading filter on: only signals with “${esc(((fsd.rows || [])[rule] || {}).name || '')}” are bought.` : 'No trading filter: every signal is bought.'} Few signals = a hint, not a proof.</p>` : '<p class="muted" style="margin:0">No finished signal matched with the flow yet.</p>'}</section>`;
  const rsd = d.retestStudy || {}, rb = rsd.base || {};
  const rrow = x => { const better = x.n >= 5 && rb.avgPct != null && x.avgPct != null && x.avgPct > rb.avgPct;
    return `<tr${better ? ' style="background:color-mix(in srgb, var(--gain) 9%, transparent)"' : ''}><td>${esc(x.name)}</td><td class="r">${x.n}</td><td class="r">${x.n ? (x.hitPct ?? '—') + '%' : '—'}</td><td class="r">${x.n ? x.winPct + '%' : '—'}</td><td class="r">${pc(x.avgPct)}</td><td class="r">${x.avgR == null ? '—' : (x.avgR > 0 ? '+' : '') + x.avgR + 'R'}</td><td class="r">${pc(x.totalPct)}</td></tr>`; };
  const rtBox = `<section class="panel" style="margin:0 0 14px"><h2>Flow retest analysis</h2>
    <p class="muted" style="font-size:.84rem;margin:0 0 8px">Finished flow retest signals split by the support that was retested, the size of the call alert that armed them and how long the retest took. Green rows beat all signals (with at least 5 signals).</p>
    ${rb.n ? `<div class="tablewrap"><table class="pos" style="font-size:.85rem"><thead><tr><th>Group</th><th class="r">Signals</th><th class="r">Call wall reached</th><th class="r">Winners</th><th class="r">Avg</th><th class="r">Avg R</th><th class="r">Total</th></tr></thead><tbody>
      ${rrow({ ...rb, name: 'All flow retest signals' })}${(rsd.rows || []).map(rrow).join('')}</tbody></table></div>
      ${rsd.exitCompare ? (() => { const x = rsd.exitCompare; return `<p style="font-size:.86rem;margin:8px 0 0"><b>Daily close under the flip exit:</b> ${x.flipExit ? 'without it (now on)' : 'with it (now off)'} the same ${x.n} entries would have made avg ${pc(x.avgPct)}${x.avgR != null ? ` / ${x.avgR > 0 ? '+' : ''}${x.avgR}R` : ''}, total ${pc(x.totalPct)}, ${x.winPct}% winners — vs avg ${pc(rb.avgPct)}, total ${pc(rb.totalPct)} now.${x.flipExit ? '' : ` ${x.flipExits} trades closed by it.`}</p>`; })() : ''}` : '<p class="muted" style="margin:0">No finished flow retest signal yet. Press <b>Get all walls (45 days)</b> to build the history.</p>'}</section>`;
  const ps = d.positions || [];
  const posBox = `<section class="panel" style="margin:0 0 14px"><h2>Current positions</h2>${ps.length ? `<div class="tablewrap"><table class="pos" style="font-size:.85rem"><thead><tr><th>Ticker</th><th>Contract</th><th class="r">Qty</th><th class="r">Entry</th><th class="r">Now</th><th class="r">P&L</th><th class="r">Stock</th><th class="r">Stop</th><th class="r">Target</th><th>Status</th><th></th></tr></thead><tbody>
    ${ps.map(p0 => `<tr><td><b>${esc(p0.symbol)}</b>${p0.wallwatch ? ` <span class="exchip">${p0.kind === 'retest' ? 'retest' : 'wall watch'}</span>` : ''}</td><td>${p0.strike ? esc(String(p0.strike)) + 'C ' : ''}${esc(p0.exp || '')}</td><td class="r">${p0.qty ?? ''}</td><td class="r">${p0.fill ?? (p0.limit ? 'limit ' + p0.limit : '—')}</td><td class="r">${p0.mark ?? '—'}</td>
      <td class="r ${(p0.pl || 0) >= 0 ? 'gain' : 'loss'}">${p0.pl != null ? money(p0.pl) : '—'}${p0.plPct != null ? ` (${p0.plPct >= 0 ? '+' : ''}${p0.plPct}%)` : ''}</td><td class="r">${p0.under != null ? (+p0.under).toFixed(2) : '—'}</td><td class="r">${p0.stop ?? '—'}</td><td class="r">${p0.target ?? '<span class="muted">trail</span>'}</td><td>${esc(p0.status)}</td>
      <td><button class="btn" data-wwchart="${esc(p0.symbol)}|" style="font-size:.75rem;padding:1px 7px">chart</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="muted" style="margin:0">No open position.</p>'}</section>`;
  const more = `<details class="panel" style="margin:14px 0 0;padding:10px 14px" ${S.wwMoreOpen ? 'open' : ''} data-wwmore><summary class="muted" style="cursor:pointer">Other tools and studies (intraday gamma levels, flow studies, by ticker)</summary><div style="margin-top:10px">${wwIntraday()}${rtBox}${flowBox}${by}</div></details>`;
  return h + runLine + posBox + kinds + stats + wwRuleTester() + sigTable + now + more;
}
function vExplore() {
  const d = S.ex;
  const h = head('Explorer', 'Combine any conditions and see how every matching idea did: the real result when the bot took it, the shadow result when it skipped it.', '');
  if (!d) return h + '<p class="loading">Loading…</p>';
  if (d.error) return h + `<div class="errbox">${esc(d.error)}</div>`;
  const R = v => v == null ? '—' : `<span class="${v > 0 ? 'gain' : v < 0 ? 'loss' : ''}">${sgn(v, 2)}R</span>`;
  const feats = d.features || [];
  const fopt = f => `<option value="${f.key}" ${S.exPick === f.key ? 'selected' : ''}>${esc(f.label)}${f.known < (d.all.n || 0) ? ` (known for ${f.known})` : ''}</option>`;
  const cats = d.cats || [...new Set(feats.map(f => f.cat || 'Other'))];
  const fsel = `<select id="ex-key" style="max-width:100%"><option value="">Add a condition… (${feats.length} available)</option>${cats.map(c => { const fs = feats.filter(f => (f.cat || 'Other') === c); return fs.length ? `<optgroup label="${esc(c)}">${fs.map(fopt).join('')}</optgroup>` : ''; }).join('')}${feats.filter(f => !cats.includes(f.cat || 'Other')).map(fopt).join('')}</select>`;
  const pk = S.exPick && feats.find(f => f.key === S.exPick);
  const picker = pk ? (pk.kind === 'bool'
    ? `<select id="ex-val"><option value="1">Yes</option><option value="0">No</option></select>`
    : `<select id="ex-op"><option value=">=">≥</option><option value="<=">≤</option></select><input id="ex-num" type="number" step="any" placeholder="value" style="width:8em">`) + ` <button class="btn" data-exadd="1">Add</button>` : '';
  const chips = (d.conds || []).map((c, i) => `<span class="exchip">${esc(c.text)} <button aria-label="Remove" data-exdel="${i}">×</button></span>`).join('');
  const m = d.match, a = d.all;
  const cmp = (a.avgR != null && m.avgR != null) ? (m.avgR - a.avgR) : null;
  const res = `<div class="strip" style="margin:12px 0">
      <div class="stat"><span>Matching ideas</span><b>${m.n}</b><span>${m.done} finished · ${m.open} still open</span></div>
      <div class="stat"><span>Avg result</span><b>${R(m.avgR)}</b><span>${m.winPct ?? '—'}% winners · total ${m.totalR ?? '—'}R</span></div>
      <div class="stat"><span>All ideas (same period)</span><b>${R(a.avgR)}</b><span>${a.n} ideas · ${cmp != null ? `difference ${sgn(cmp, 2)}R` : ''}</span></div>
    </div>
    ${d.conds.length && m.n >= 4 ? `<p class="muted" style="font-size:.84rem;margin:0 0 8px">Older half ${R(d.older)} · newer half ${R(d.newer)} — ${d.older != null && d.newer != null && d.older > (a.avgR || 0) && d.newer > (a.avgR || 0) ? '<b class="gain">holds in both halves</b>' : 'not consistent across the period yet'}${m.n < 30 ? ' · fewer than 30 ideas: treat as a hint, not a result' : ''}</p>` : ''}`;
  const list = (d.items || []).length ? `<details ${d.conds.length ? 'open' : ''}><summary class="muted" style="cursor:pointer">The ${d.items.length} matching ideas, best first</summary>
    <p style="margin:8px 0"><button class="btn coachbtn" data-replay="0">▶ Open them on the chart</button> <span class="muted" style="font-size:.82rem">one after the other (← → keys)</span></p>
    <ul class="shl" style="margin-top:8px">${d.items.slice(0, 80).map((x, i) => `<li data-shopen="${x.id}"><span class="shl-a"><b>${esc(x.symbol)}</b> <span class="muted">${esc(x.at.replace('T', ' ').slice(5, 16))} · ${esc(x.decision || '')}${x.taken ? ' · taken' : ''}</span></span><span class="shl-r">${R(x.R)}${x.status !== 'done' ? ' <span class="muted">open</span>' : ''} <button class="btn" data-replay="${i}" style="font-size:.75rem;padding:1px 7px" title="See it on the chart">chart</button></span></li>`).join('')}</ul></details>` : '';
  const sr = d.search;
  const search = `<section class="panel" style="margin-top:14px"><div class="cal-head"><h2>Best combinations</h2>
      <span style="display:flex;gap:8px;align-items:center"><label class="muted" style="font-size:.85rem">at least <input id="ex-minn" type="number" value="${S.exMinN || 15}" min="5" style="width:4.5em"> ideas</label>
      <select id="ex-group" aria-label="Conditions to combine">${[['', 'All conditions'], ['wallsflow', 'Gamma walls + options volume'], ['walls', 'Gamma walls only'], ['gamma', 'All gamma (walls, flip, net GEX)'], ['flow', 'Options volume / flow only'], ['trend', 'Price & trend only'], ['trendflow', 'Price & trend + options volume']].map(([v, l]) => `<option value="${v}" ${(S.exGroup || '') === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <select id="ex-depth" aria-label="Conditions per combination">${[2, 3, 4, 5, 6].map(n => `<option value="${n}" ${(S.exDepth || 4) === n ? 'selected' : ''}>up to ${n} conditions</option>`).join('')}</select>
      <button class="btn coachbtn" data-exsearch="1" ${S.exBusy ? 'disabled' : ''}>${S.exBusy === 'search' ? 'Searching…' : 'Find the best combinations'}</button></span></div>
    <p class="muted" style="font-size:.84rem;margin:0 0 8px">Tries every single condition, every pair of the 60 most promising ones, then keeps extending the best combinations one condition at a time up to the chosen size, and ranks them by average R. More conditions = fewer ideas each: keep the minimum high enough to trust the result. Only combinations with enough ideas are kept, and each one is checked on the older and the newer half of the period: one that holds in both is far less likely to be luck.</p>
    ${!sr ? '' : !sr.results.length ? `<p class="muted">No combination has enough ideas yet.${sr.group && sr.group !== 'flow' && d.walls && d.walls.known < (sr.minN || 15) ? ` Gamma walls are known for only ${d.walls.known} ideas: press <b>Store walls for every ticker</b> above, then search again in a few minutes.` : ' Lower the minimum or wait for more results.'}</p>` :
      `<ul class="shl">${sr.results.map((x, i) => `<li data-exuse="${i}"><span class="shl-a">${esc(x.text)}</span><span class="shl-r">${R(x.avgR)}</span>
        <span class="shl-b muted">${x.n} ideas (${x.open} open) · ${x.winPct}% winners · total ${x.totalR}R · older ${x.older ?? '—'}R / newer ${x.newer ?? '—'}R ${x.holds ? '· <b class="gain">holds in both halves</b>' : ''}</span></li>`).join('')}</ul>
      <p class="muted" style="font-size:.8rem;margin:6px 0 0">${sr.tested} combinations tested against ${R(sr.baseline.avgR)} for all ideas. Tap one to load it above.</p>`}</section>`;
  return h + `<section class="panel"><div class="cal-head"><h2>Conditions</h2><select id="ex-days">${[10, 20, 35, 60].map(n => `<option value="${n}" ${n === (S.exDays || 35) ? 'selected' : ''}>Last ${n} days</option>`).join('')}</select></div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${chips || '<span class="muted">No condition: all ideas.</span>'}${chips ? ` <button class="btn" data-exclear="1">Clear</button> <button class="btn coachbtn" data-exsave="1">Save as strategy${S.exExit ? ' (with the chosen exit)' : ''}</button>` : ''}</div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">${fsel} ${picker}</div>
    ${d.walls ? `<p class="muted" style="font-size:.82rem;margin:8px 0 0">Gamma walls (put wall / call wall by day) known for <b>${d.walls.known}</b> of ${d.walls.of} ideas${d.walls.at ? ` · last stored ${esc(d.walls.at.replace('T', ' ').slice(5))}${d.walls.left ? ` (${d.walls.left} days still to fetch)` : ''}` : ''}.
      <button class="btn" data-wallsfill="1" style="font-size:.78rem;padding:1px 8px">Store walls for every ticker</button></p>` : ''}
    ${S.exBusy === 'filter' ? '<p class="loading" style="margin:8px 0 0">Filtering…</p>' : res + list}</section>` + eoPanel() + search;
}
function vBotSkipped() {
  return head('Skipped & what-if', 'What the trades the bot skipped would have done, what happened after its exits, and which flow is worth acting on', '')
    + shadowSummary() + flowSummary();
}
function vBot(mode) {
  const b = S.bot, cfg = (b && b.settings) || {}, br = S.broker;
  const paperOk = br && br.connected && br.env === 'paper';
  const warn = !br ? '' : !br.connected ? `<div class="errbox">Connect Alpaca with <b>paper</b> keys in Settings → Brokers. The bot only trades the paper account.</div>`
    : br.env !== 'paper' ? `<div class="errbox">Alpaca is connected with <b>live</b> keys. The bot only trades paper; reconnect with paper keys to place orders.</div>` : '';
  const items = (b && b.items) || [];
  const posRow = i => { const p = i.proposal || {};
    const placed = (i.placedBy || '').startsWith('auto') ? (i.placedBy === 'auto-flow' ? 'Bot (flow)' : 'Bot') : `You <span class="muted">(bot said ${esc(i.decision)})</span>`;
    const pl = i.status === 'closed' ? i.realizedPl : i.lastPl, plp = i.status === 'closed' ? i.realizedPct : i.lastPlPct;
    const now = i.status === 'closed' ? i.exitPrice : i.lastMark;
    const st = i.status === 'open' ? 'st-open' : ['submitting', 'submitted', 'closing'].includes(i.status) ? 'st-work' : 'st-closed';
    const stTxt = i.status === 'open' ? 'Open' : i.status === 'closing' ? 'Closing' : ['submitting', 'submitted'].includes(i.status) ? 'Order working' : 'Closed';
    return `<tr data-evopen="${i.id}" class="pos-row ${st}" style="cursor:pointer" title="${stTxt}"><td data-l="Placed">${esc((i.submittedAt || i.createdAt || '').replace('T', ' ').slice(5, 16))}</td><td class="pos-c"><span class="st-dot" aria-label="${stTxt}"></span><b>${esc(i.symbol)}</b>${p.exp ? ` <span class="muted">${esc(legTxt(p))}</span>` : ''}</td>${aiCell(i).replace('<td', '<td data-l="Claude"')}
      <td class="r" data-l="Qty">${i.filledQty || i.qty || ''}</td><td class="r" data-l="Entry">${i.fillPrice ? i.fillPrice.toFixed(2) : i.limit ? `<span class="muted">limit ${i.limit.toFixed(2)}</span>` : ''}</td>
      <td class="r" data-l="Now">${now != null ? Number(now).toFixed(2) : '—'}</td>
      <td class="r pos-pl ${cls(pl)}" data-l="P&L">${pl != null ? money(pl) + (plp != null ? ` <span style="font-weight:400">(${plp > 0 ? '+' : ''}${plp}%)</span>` : '') : '—'}</td>
      <td class="pos-sum">${i.filledQty || i.qty || ''} × ${i.fillPrice ? i.fillPrice.toFixed(2) : i.limit ? 'limit ' + i.limit.toFixed(2) : '—'} → ${now != null ? Number(now).toFixed(2) : '—'}</td>
      <td class="pos-note" title="${esc(i.exitReason || i.chaseNote || i.rollNote || '')}"><span class="ellipsis">${esc(i.exitReason || i.chaseNote || i.rollNote || (i.status === 'submitted' && i.chaseSteps ? `limit raised ${i.chaseSteps}× to ${i.limit.toFixed(2)}` : '')) || (i.trailing ? `<span class="gain">trailing from ${Number(i.peakMark || 0).toFixed(2)}</span>` : '') || (i.lastCheck ? `<span class="muted">updated ${esc(i.lastCheck.slice(11, 16))}</span>` : '')}</span></td>
      <td class="r pos-act" style="white-space:nowrap">${i.status === 'submitted' ? `<button class="btn" data-botchase="${i.id}">Chase</button> ` : ''}${i.status === 'submitted' ? `<button class="btn" data-botclose="${i.id}">Cancel</button>` : ''}</td></tr>`; };
  const posTable = rows => `<p class="st-legend"><span><i class="st-dot g"></i>Open</span><span><i class="st-dot y"></i>Order working / closing</span><span><i class="st-dot n"></i>Closed</span></p><div class="tablewrap"><table class="pos"><thead><tr><th>Placed</th><th>Contract</th><th>Claude</th><th class="r">Qty</th><th class="r">Entry</th><th class="r">Now / exit</th><th class="r">P&L</th><th>Note</th><th></th></tr></thead><tbody>${rows.map(posRow).join('')}</tbody></table></div>`;
  const evalRow = i => { const p = i.proposal || {};
    return `<tr data-botshow="${i.id}" style="cursor:pointer"><td>${esc((i.createdAt || '').replace('T', ' ').slice(5, 16))}</td><td><b>${esc(i.symbol)}</b>${p.exp ? ` <span class="muted">${esc(legTxt(p))}</span>` : ''}</td><td><span class="verdict ${DEC_CLS[i.decision] || 'mid'}">${i.decision}</span></td>${aiCell(i)}${shadowCell(i)}<td>${esc(i.status)}</td><td title="${esc((i.blocking || []).join('; '))}"><span class="ellipsis">${esc((i.blocking || [])[0] || '')}</span></td><td><button class="btn" data-rescan="${esc(i.symbol)}">Rescan</button></td></tr>`; };
  const trades = items.filter(i => ['submitting', 'submitted', 'open', 'closing', 'closed'].includes(i.status) && (i.orderId || i.fillPrice || i.status === 'submitting'));
  const active = trades.filter(i => i.status !== 'closed');
  const done = trades.filter(i => i.status === 'closed');
  const ev = S.botEvals, evals = ev ? ev.items : items.filter(i => !trades.includes(i)).slice(0, 20);
  const pager = ev && ev.pages > 1 ? (() => {
    const cur = ev.page, last = ev.pages, nums = [...new Set([1, cur - 2, cur - 1, cur, cur + 1, cur + 2, last])].filter(n => n >= 1 && n <= last).sort((a, b) => a - b);
    let prev = 0; const parts = [];
    for (const n of nums) { if (n - prev > 1) parts.push('<span class="muted">…</span>'); parts.push(`<button class="toggle" data-evpage="${n}" aria-pressed="${n === cur}">${n}</button>`); prev = n; }
    return `<div class="filters" style="justify-content:center;margin:10px 0 0;align-items:center"><button class="toggle" data-evpage="${Math.max(1, cur - 1)}" ${cur === 1 ? 'disabled' : ''}>‹ Newer</button>${parts.join('')}<button class="toggle" data-evpage="${Math.min(last, cur + 1)}" ${cur === last ? 'disabled' : ''}>Older ›</button></div>
      <p class="muted" style="text-align:center;font-size:.82rem;margin:4px 0 0">${ev.total} ${ev.group === 'ticker' ? 'tickers' : 'evaluations'} · page ${cur} of ${last}</p>`;
  })() : '';
  const sm = (b && b.summary) || {}, ac = sm.account;
  const card = (k, v, sub, col) => `<div class="period"><h3>${k}</h3><div class="big" style="${col ? 'color:' + col : ''}">${v}</div><p>${sub || ''}</p></div>`;
  const summaryHtml = `<section class="periods" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr))">
    ${ac ? card('Paper account', money(ac.equity, false), `Today ${money(ac.dayChange)}`, null) : ''}
    ${card('Bot total P&L', money(sm.total || 0), 'Closed + open trades', (sm.total || 0) >= 0 ? 'var(--gain)' : 'var(--loss)')}
    ${card('Realized', money(sm.realized || 0), `${sm.closedTrades || 0} closed · ${sm.winRate != null ? sm.winRate + '% winners' : 'no closed trades yet'}${sm.avgReturnPct != null ? ` · avg ${sm.avgReturnPct > 0 ? '+' : ''}${sm.avgReturnPct}%` : ''}`, (sm.realized || 0) >= 0 ? 'var(--gain)' : 'var(--loss)')}
    ${card('Open', money(sm.unrealized || 0), `${sm.openPositions || 0} positions · ${money(sm.capitalInOpen || 0, false)} value`, (sm.unrealized || 0) >= 0 ? 'var(--gain)' : 'var(--loss)')}
  </section>
  ${(sm.equityHistory || []).length > 1 ? `<section class="panel"><h2>Paper account equity</h2>${eqLine(sm.equityHistory)}</section>` : ''}`;
  const earnTxt = Object.entries(cfg.earnings || {}).map(([k, v]) => `${k} ${v}`).join('\n');
  const f = (id, label, v, step = 'any') => `<div class="field"><label for="${id}">${label}</label><input id="${id}" type="number" step="${step}" value="${v ?? ''}"></div>`;
  const st = (b && b.state) || {}, nt = (S.me.settings || {}).notify || {};
  const lastRun = st.lastFlowRun ? `Last flow check ${esc(st.lastFlowRun.replace('T', ' ').slice(5, 16))} ET · ${st.lastFlowAlerts ?? 0} new alerts${(st.lastFlowResult || []).length ? ' · ' + st.lastFlowResult.map(r => `${esc(r.symbol)}: ${esc(r.decision || r.error || '')}${r.ordered ? ' (ordered)' : ''}`).join(', ') : ''}` : 'No automatic flow check yet.';
  if (mode === 'evals') {
    const t = evTicker(), ev2 = S.botEvals && S.botEvals.mode === (t ? `t:${t}` : 'group') ? S.botEvals : null;
    if (t) {
      const rows = ev2 ? ev2.items : [];
      return head(t, `Every evaluation of ${esc(t)}, newest first`, `<a href="#botevals" class="linkbtn">‹ All tickers</a>`)
        + `<p style="margin:0 0 12px"><button class="btn coachbtn" data-reeval="${esc(t)}" ${S.evBusy ? 'disabled' : ''}>${S.evBusy === t ? `Evaluating ${esc(t)}…` : `Re-evaluate ${esc(t)} now`}</button></p>`
        + botResult(S.botRes && S.botRes.symbol === t ? S.botRes : null)
        + `<section><h2>History</h2>${!ev2 ? '<p class="loading">Loading…</p>' : rows.length ? `<div class="tablewrap"><table><thead><tr><th>When</th><th>Contract</th><th>Decision</th><th>Claude</th><th>If taken</th><th>Status</th><th>Main reason</th></tr></thead><tbody>
          ${rows.map(i => { const p = i.proposal || {}; return `<tr data-botshow="${i.id}" style="cursor:pointer" ${S.botRes && S.botRes.id === i.id ? 'aria-current="true" class="sel"' : ''}><td>${esc((i.createdAt || '').replace('T', ' ').slice(5, 16))}</td><td>${p.exp ? esc(legTxt(p)) : '<span class="muted">no contract</span>'}</td><td><span class="verdict ${DEC_CLS[i.decision] || 'mid'}">${i.decision}</span></td>${aiCell(i)}${i.taken ? '<td><span class="muted">traded</span></td>' : shadowCell(i)}<td>${esc(i.taken ? (i.status === 'closed' ? 'trade closed' : 'trade ' + i.status) : i.status)}</td><td title="${esc((i.blocking || []).join('; '))}"><span class="ellipsis">${esc((i.blocking || [])[0] || '')}</span></td></tr>`; }).join('')}
          </tbody></table></div>${pager}` : '<p class="muted">No evaluation of this ticker yet.</p>'}</section>`;
    }
    const groups = ev2 ? ev2.items : [];
    return head('Evaluations', 'One row per ticker, latest evaluation first. Open a ticker for its full history and to re-evaluate it.', '')
      + feedStatus() + `<section style="margin-top:14px"><div class="cal-head"><h2>Tickers</h2><label style="font-size:.88rem"><input type="checkbox" id="ev-claude" ${S.evClaude ? 'checked' : ''}> Only ones Claude checked</label></div>
      <div class="field" style="margin:0 0 10px;max-width:320px"><input id="ev-q" type="search" inputmode="search" autocapitalize="characters" placeholder="Search a ticker (e.g. PLTR)" value="${esc(S.evQ || '')}" aria-label="Search a ticker"></div>
      ${!ev2 ? '<p class="loading">Loading…</p>' : groups.length ? `<div class="tablewrap"><table class="evg"><thead><tr><th>Ticker</th><th class="r">Evaluations</th><th>Latest</th><th>Decision</th><th>Claude</th><th>Main reason</th></tr></thead><tbody>
        ${groups.map(g => { const l = g.latest || {}, p = l.proposal || {}, d = g.decisions || {};
          return `<tr data-evticker="${esc(g.symbol)}" style="cursor:pointer"><td class="evg-t"><b>${esc(g.symbol)}</b>${g.held ? ' <span class="verdict ok" style="font-size:.72rem">held</span>' : ''}${p.exp ? ` <span class="muted">${esc(legTxt(p))}</span>` : ''}</td>
            <td class="r evg-n">${g.count}${g.count > 1 ? ` <span class="muted" style="font-size:.8rem">${Object.entries(d).map(([k, n]) => `${n} ${k}`).join(' · ')}</span>` : ''}</td>
            <td class="evg-w">${esc((l.createdAt || '').replace('T', ' ').slice(5, 16))}</td><td class="evg-d"><span class="verdict ${DEC_CLS[l.decision] || 'mid'}">${l.decision}</span></td>${aiCell(l).replace('<td', '<td class="evg-c"')}
            <td class="evg-r" title="${esc((l.blocking || []).join('; '))}"><span class="ellipsis">${esc((l.blocking || [])[0] || '')}</span></td></tr>`; }).join('')}
      </tbody></table></div>${pager}` : `<div class="tablewrap"><p class="empty">${S.evQ ? `No evaluation matches “${esc(S.evQ)}”. <a href="#botevals?t=${encodeURIComponent(S.evQ)}">Evaluate ${esc(S.evQ)} now</a>` : S.evClaude ? "Claude hasn't checked any evaluation yet." : 'No evaluations yet.'}</p></div>`}</section>`;
  }
  return head('Paper bot', 'Trades the Alpaca paper account automatically from unusual options flow and manages every exit', '') + warn + `
  <section class="coach" style="padding:14px 18px"><div class="coach-who">${spark()} Status</div>
    <div class="switches">
      ${[['flowAuto', 'Flow trading', 'Check unusual flow every minute'], ['autoSubmit', 'Automatic orders', 'Place paper orders when every rule passes']].map(([k, l, d]) =>
        `<button class="switch" data-bottoggle="${k}" aria-pressed="${!!cfg[k]}"><span class="knob" aria-hidden="true"></span><span><b>${l}</b> <span class="sw-state">${cfg[k] ? 'On' : 'Off'}</span><small>${d}</small></span></button>`).join('')}
    </div>
    <p style="margin:0">${cfg.flowAuto ? `<b class="gain">Flow trading is on.</b> Every minute from 9:35 to 15:50 ET the bot pulls new unusual-flow alerts, analyzes up to ${cfg.flowMaxEvals} tickers, and ${cfg.autoSubmit ? 'places a paper order when every rule passes' : '<b>only logs the analysis</b> (automatic orders are off)'}.` : '<b>Flow trading is off.</b> Turn on <b>Flow trading</b> above (and <b>Automatic orders</b> to let it place trades).'} Exits are checked every minute for every bot position.</p>
    ${feedStatus()}
    <p class="muted" style="margin:6px 0 0;font-size:.86rem">${lastRun}</p>
    <p class="muted" style="margin:4px 0 0;font-size:.8rem">This page refreshes every minute${S.botAt ? ` · last update ${esc(S.botAt)}` : ''}.</p></section>
  ${summaryHtml.replace(/<section class="panel"><h2>Paper account equity[\s\S]*$/, '')}
  ${active.length ? `<section><h2>Open positions and working orders</h2>${posTable(active)}<p class="muted" style="font-size:.82rem;margin:6px 0 0">Values refresh every minute. Exits are automatic.</p></section>` : ''}
  ${done.length ? `<section><h2>Closed bot trades</h2>${posTable(done.slice(0, 30))}</section>` : ''}
  <p class="muted" style="margin:18px 0 0">Bot rules, automatic flow trading and alerts are in <a href="#settings">Settings → Paper bot</a>.</p>`;
}
function botSettingsPanels() {
  const b = S.bot, cfg = (b && b.settings) || {}, nt = (S.me.settings || {}).notify || {};
  if (!b) return `<section class="panel"><h2>Paper bot</h2><p class="loading" style="padding:0">Loading…</p></section>`;
  const earnTxt = Object.entries(cfg.earnings || {}).map(([k, v]) => `${k} ${v}`).join('\n');
  const f = (id, label, v, step = 'any') => `<div class="field"><label for="${id}">${label}</label><input id="${id}" type="number" step="${step}" value="${v ?? ''}"></div>`;
  return `<h2 style="margin:26px 0 10px">Paper bot</h2>
  <section class="panel"><h2>Automatic flow trading</h2>
    <div style="display:flex;flex-wrap:wrap;gap:6px 22px;margin-bottom:12px">
      <label><input type="checkbox" id="bf-on" ${cfg.flowAuto ? 'checked' : ''}> Check unusual flow every minute and analyze new tickers</label>
      <label><input type="checkbox" id="bf-etf" ${cfg.excludeEtfs ? 'checked' : ''}> Skip ETFs</label>
      <label><input type="checkbox" id="bf-ask" ${cfg.flowAskSide ? 'checked' : ''}> Mostly bought at the ask</label>
      <label><input type="checkbox" id="bf-sweep" ${cfg.flowSweeps ? 'checked' : ''}> Sweeps only</label></div>
    <div class="form-grid">
      ${f('bf-prem', 'Min premium ($)', cfg.flowMinPremium, 10000)}${f('bf-dmin', 'Flow days to expiry, min', cfg.flowMinDte, 1)}${f('bf-dmax', 'Flow days to expiry, max', cfg.flowMaxDte, 1)}
      ${f('bf-voi', 'Min volume / OI', cfg.flowMinVolOi, 0.1)}${f('bf-cool', 'Re-check a ticker after (min)', cfg.flowCooldownMin, 1)}${f('bf-evals', 'Max tickers analyzed per minute', cfg.flowMaxEvals, 1)}${f('bf-win', 'Look at alerts from the last (min)', cfg.flowWindowMin ?? 60, 5)}</div>
    <h3 style="font-size:.95rem;margin:10px 0 6px">Repeat buyers</h3>
    <label style="display:block;margin-bottom:6px"><input type="checkbox" id="bf-rep" ${cfg.repeatEnabled !== false ? 'checked' : ''}> Look for smaller call buys (under the min premium) on the ticker, any contract</label>
    <div class="form-grid">
      ${f('bf-rdays', 'Look back (trading days, today included)', cfg.repeatDays ?? 5, 1)}${f('bf-rprem', 'Smallest print counted ($)', cfg.repeatMinPremium ?? 10000, 5000)}
      ${f('bf-rhits', 'Smaller buys needed', cfg.repeatMinHits ?? 4, 1)}${f('bf-rmin', 'In at least … different minutes', cfg.repeatMinMinutes ?? 3, 1)}
      ${f('bf-rtot', 'Total of the smaller buys ($)', cfg.repeatMinTotal ?? 200000, 50000)}</div>
    <h3 style="font-size:.95rem;margin:10px 0 6px">The other side: puts</h3>
    <label style="display:block;margin-bottom:4px"><input type="checkbox" id="bf-put" ${cfg.putCheck !== false ? 'checked' : ''}> Check the puts bought on the ticker over the same look-back (Flow check)</label>
    <div class="form-grid" style="align-items:end">
      ${f('bf-putr', 'Max puts vs calls bought (0.5 = half)', cfg.putMaxRatio ?? 0.5, 0.05)}
      <div class="field"><label><input type="checkbox" id="bf-putb" ${cfg.putBlock ? 'checked' : ''}> Block the buy when puts are above that (otherwise a caution)</label></div></div>
    <p class="muted" style="font-size:.84rem;margin:0 0 8px">Only alerts above the min premium start an analysis. Each analysis then looks up everything bought at the ask on that ticker (any contract) today and over the look-back: smaller call buys, big call buys and puts. Repeat buying is a ✓ when the smaller buys reach all of these (or Unusual Whales flags repeated hits and the total is reached); it's information, passed to Claude and tracked in "Which flow is worth acting on". Puts above the ratio block the buy.</p>
    <p style="margin:0 0 8px"><button class="btn primary" data-botsave="1">Save</button> <span class="muted" style="font-size:.84rem">Bot settings also save automatically when you change a field.</span></p>
    <p class="muted" style="font-size:.84rem;margin:0">Orders are placed only when <b>Place paper orders automatically</b> (below) is also on. Calls only; the contract the bot buys is chosen by its own rules, not copied from the flow.</p></section>
  <section class="panel"><h2>Alerts</h2>
    <p class="muted" style="margin-top:-4px">Every bot order is sent as an Amazon SNS notification to your email right away. Text messages are optional and need a registered toll-free number in AWS (SETUP.md step 8).</p>
    <div class="form-grid" style="align-items:end">
      <div class="field"><label for="nt-email">Email for alerts (SNS)</label><input id="nt-email" type="email" placeholder="you@example.com" value="${esc(nt.email || '')}"></div>
      <div class="field"><span class="muted" style="font-size:.88rem">${S.ntStatus ? (S.ntStatus.emailStatus === 'confirmed' ? '<b class="gain">Subscribed and confirmed</b>' : S.ntStatus.emailStatus === 'pending' ? '<b class="warn" style="color:var(--warn)">Waiting for you to confirm:</b> open the email from AWS Notifications and click <b>Confirm subscription</b>' : S.ntStatus.email ? esc(S.ntStatus.emailStatus || '') : 'Not set') : ''}</span></div></div>
    <div class="form-grid" style="align-items:end">
      <div class="field"><label for="nt-phone">Mobile number (with country code)</label><input id="nt-phone" type="tel" placeholder="+19165551234" value="${esc(nt.phone || '')}"></div>
      <div class="field"><label><input type="checkbox" id="nt-on" ${nt.sms ? 'checked' : ''}> I agree to receive text alerts about my paper-bot orders at this number. Frequency varies. Msg &amp; data rates may apply. Reply STOP to opt out, HELP for help. <a href="/terms.html" target="_blank">Terms</a> · <a href="/privacy.html" target="_blank">Privacy</a></label></div>
      <div class="field"><button class="btn primary" id="nt-save">Save</button> <button class="btn" id="nt-test">Send test</button></div></div>
    <div style="display:flex;flex-wrap:wrap;gap:6px 18px;margin:0 0 10px">${[['entry', 'Buy orders placed'], ['fill', 'Buys filled'], ['exit', 'Exit orders (with reason)'], ['closed', 'Positions closed (with P&L)'], ['cancel', 'Unfilled orders cancelled'], ['roll', 'Diagonal short calls rolled'], ['ai', 'Orders blocked by Claude']]
      .map(([k, l]) => `<label><input type="checkbox" class="nt-ev" value="${k}" ${(nt.events || ['entry', 'fill', 'exit', 'closed', 'cancel', 'roll', 'ai']).includes(k) ? 'checked' : ''}> ${l}</label>`).join('')}</div>
    
    ${(b && b.notifications || []).length ? `<details><summary class="muted" style="cursor:pointer">Recent alerts</summary><div class="tablewrap" style="border:0"><table><thead><tr><th>When</th><th>Message</th><th>Status</th></tr></thead><tbody>
      ${b.notifications.map(n => `<tr><td>${esc((n.at || '').replace('T', ' ').slice(5, 16))}</td><td title="${esc(n.text)}"><span class="ellipsis" style="max-width:520px">${esc(n.text)}</span></td><td class="${n.status === 'sent' ? 'gain' : n.status === 'failed' ? 'loss' : 'muted'}" title="${esc(n.error || '')}">${esc(n.status)}</td></tr>`).join('')}</tbody></table></div></details>` : ''}
  </section>
  <section class="panel"><h2>Bot settings</h2>
    <div style="display:flex;flex-wrap:wrap;gap:6px 22px;margin-bottom:12px">
      <label><input type="checkbox" id="bs-auto" ${cfg.autoSubmit ? 'checked' : ''}> Place paper orders automatically when the decision is BUY</label>
      <label><input type="checkbox" id="bs-flip" ${cfg.requireAboveFlip ? 'checked' : ''}> Require price above the gamma flip</label>
      <label><input type="checkbox" id="bs-ai" ${cfg.aiCheck !== false ? 'checked' : ''}> Claude checks the chart before every order</label>
      <label><input type="checkbox" id="bs-aicau" ${cfg.aiAllowCaution ? 'checked' : ''}> Still place when Claude says “caution”</label>
      <label><input type="checkbox" id="bs-aierr" ${cfg.aiBlockOnError !== false ? 'checked' : ''}> Don't place if the chart check can't run</label>
      <label><input type="checkbox" id="bs-aiexit" ${cfg.aiExitReview !== false ? 'checked' : ''}> Claude reviews every open position at 15:40 ET</label>
      <label><input type="checkbox" id="bs-aiexitauto" ${cfg.aiExitAutoClose !== false ? 'checked' : ''}> Close when Claude says close with at least <input id="bs-aiexitconf" type="number" min="0" max="100" step="5" value="${cfg.aiExitMinConf ?? 60}" style="width:4.2em;padding:2px 4px"> % confidence</label>
      <label>…or says hold with under <input id="bs-aiholdconf" type="number" min="0" max="100" step="5" value="${cfg.aiExitHoldMinConf ?? 50}" style="width:4.2em;padding:2px 4px"> % confidence while the option is down <input id="bs-aiholdloss" type="number" min="0" max="100" step="5" value="${cfg.aiExitHoldLossPct ?? 25}" style="width:4.2em;padding:2px 4px"> % or more</label>
      <label><input type="checkbox" id="bs-onclose" ${cfg.invalidationOnClose !== false ? 'checked' : ''}> Exit on the invalidation level only on a close below it (checked from 15:50 ET)</label>
      <label><input type="checkbox" id="bs-trail" ${cfg.trailAfterTarget !== false ? 'checked' : ''}> After the target is reached, trail instead of selling</label></div>
    <h3 style="font-size:.95rem;margin:4px 0 6px">Structure</h3>
    <div class="form-grid">
      <div class="field"><label for="bs-strat">Trade as</label><select id="bs-strat">${Object.entries(STRAT_LBL).map(([k, l]) => `<option value="${k}" ${(cfg.strategy || 'long_call') === k ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
      ${f('bs-sstop', 'Spread stop (% of debit lost)', cfg.spreadStopPct ?? 50, 1)}</div>
    <div class="form-grid" ${(cfg.strategy || 'long_call') === 'bull_call' ? '' : 'hidden'} id="bs-bull">
      ${f('bs-sdmin', 'Short call delta, min', cfg.spreadShortDeltaMin ?? 0.2, 0.05)}${f('bs-sdmax', 'Short call delta, max', cfg.spreadShortDeltaMax ?? 0.4, 0.05)}
      ${f('bs-sdebit', 'Max debit (% of width)', cfg.spreadMaxDebitPct ?? 70, 1)}${f('bs-starget', 'Take profit at (% of max profit)', cfg.spreadTargetPct ?? 70, 1)}</div>
    <div class="form-grid" ${cfg.strategy === 'diagonal' ? '' : 'hidden'} id="bs-diag">
      ${f('bs-dldmin', 'Long call days, min', cfg.diagLongDteMin ?? 60, 1)}${f('bs-dldmax', 'Long call days, max', cfg.diagLongDteMax ?? 150, 1)}
      ${f('bs-dlxmin', 'Long call delta, min', cfg.diagLongDeltaMin ?? 0.65, 0.05)}${f('bs-dlxmax', 'Long call delta, max', cfg.diagLongDeltaMax ?? 0.85, 0.05)}
      ${f('bs-dsdmin', 'Short call days, min', cfg.diagShortDteMin ?? 5, 1)}${f('bs-dsdmax', 'Short call days, max', cfg.diagShortDteMax ?? 21, 1)}
      ${f('bs-dsxmin', 'Short call delta, min', cfg.diagShortDeltaMin ?? 0.2, 0.05)}${f('bs-dsxmax', 'Short call delta, max', cfg.diagShortDeltaMax ?? 0.4, 0.05)}
      ${f('bs-dcredit', 'Short call must pay at least ($ per contract)', cfg.diagMinShortCredit ?? 100, 1)}${f('bs-droll', 'Roll the short call when it has ≤ days left (from 15:30 ET)', cfg.diagRollDte ?? 0, 1)}
      ${f('bs-drollwait', 'Roll at mid for (minutes), then at market', cfg.diagRollWaitMin ?? 3, 1)}${f('bs-dtarget', 'Take profit (% gain on debit, 0 = keep rolling)', cfg.diagTargetPct ?? 0, 1)}</div>
    <div class="form-grid">
      ${f('bs-dtemin', 'Days to expiry, min', cfg.dteMin, 1)}${f('bs-dtemax', 'Days to expiry, max', cfg.dteMax, 1)}
      ${f('bs-dmin', 'Delta, min', cfg.deltaMin, 0.05)}${f('bs-dmax', 'Delta, max', cfg.deltaMax, 0.05)}
      ${f('bs-oi', 'Min open interest', cfg.minOi, 1)}${f('bs-spread', 'Max bid/ask spread %', cfg.maxSpreadPct)}
      ${f('bs-stop', 'Option stop (% loss)', cfg.stopPct)}${f('bs-target', 'Option target (% gain)', cfg.targetPct)}
      ${f('bs-time', 'Exit when days to expiry ≤', cfg.timeStopDte, 1)}${f('bs-emerg', 'Exit at once if stock is this many ATR below invalidation', cfg.emergencyAtr ?? 1, 0.25)}${f('bs-trailpct', 'Trail: exit when option falls this % from its best', cfg.trailPct ?? 25, 1)}${f('bs-max', 'Max open positions', cfg.maxPositions, 1)}
      ${f('bs-g30', 'Min gain from the 30-day low (%) to enter', cfg.minGrowth30 ?? 10, 1)}${f('bs-cross', 'EMA cross within (days)', cfg.crossWindow, 1)}${f('bs-ext', 'Max extension (ATR)', cfg.maxExtAtr)}
      ${f('bs-cstep', 'Raise unfilled limit by ($)', cfg.chaseStep, 0.05)}${f('bs-csec', '… every (seconds)', cfg.chaseSeconds, 1)}
      ${f('bs-cmax', 'Max raises', cfg.chaseMaxSteps, 1)}${f('bs-cpct', 'Never pay more than first limit + (%)', cfg.chaseMaxPct)}
      ${f('bs-risk', 'Risk per trade ($)', (S.me.settings || {}).riskPerTrade ?? 200, 1)}
      ${f('bs-room', 'Min room/risk ratio', cfg.minRoomRatio)}${f('bs-noentry', 'No new entry within (days of earnings)', cfg.noEntryDays, 1)}</div>
    <p class="muted" style="font-size:.84rem;margin:0 0 10px">Risk per trade: the bot buys as many contracts as fit so that hitting the option stop loses about this amount. It is the same value as Settings → Trading plan → Planned risk per trade.</p>
    <button class="btn primary" id="bs-save">Save settings</button> <button class="btn" data-botrun="scan">Scan watchlist now</button> <button class="btn" data-botrun="monitor">Check exits now</button>
  </section>`;
}
async function botEvaluate() {
  const sym = ($('#bot-sym')?.value || '').trim().toUpperCase(); if (!sym) { toast('Enter a ticker'); return; }
  const earn = $('#bot-earn') && $('#bot-earn').value ? `${$('#bot-earn').value} ${$('#bot-earn-t').value}` : '';
  S.botSym = sym; S.botBusy = true; S.botErr = null; render();
  try { S.botRes = await api('/bot/evaluate', { method: 'POST', body: { symbol: sym, earnings: earn } }); await loadBot(true); }
  catch (e) { S.botErr = e.message; }
  S.botBusy = false; if (isBotRoute()) render();
}
async function botSaveSettings(quiet) {
  const v = id => $(id).value;
  const body = { autoSubmit: $('#bs-auto').checked, requireAboveFlip: $('#bs-flip').checked, aiCheck: $('#bs-ai').checked, aiAllowCaution: $('#bs-aicau').checked, aiBlockOnError: $('#bs-aierr').checked, aiExitReview: $('#bs-aiexit').checked, aiExitAutoClose: $('#bs-aiexitauto').checked, aiExitMinConf: v('#bs-aiexitconf'), aiExitHoldMinConf: v('#bs-aiholdconf'), aiExitHoldLossPct: v('#bs-aiholdloss'),
    dteMin: v('#bs-dtemin'), dteMax: v('#bs-dtemax'), deltaMin: v('#bs-dmin'), deltaMax: v('#bs-dmax'),
    minOi: v('#bs-oi'), maxSpreadPct: v('#bs-spread'), stopPct: v('#bs-stop'), targetPct: v('#bs-target'), timeStopDte: v('#bs-time'), maxPositions: v('#bs-max'),
    minGrowth30: v('#bs-g30'), invalidationOnClose: $('#bs-onclose').checked, trailAfterTarget: $('#bs-trail').checked, emergencyAtr: v('#bs-emerg'), trailPct: v('#bs-trailpct'),
    crossWindow: v('#bs-cross'), maxExtAtr: v('#bs-ext'), minRoomRatio: v('#bs-room'), noEntryDays: v('#bs-noentry'),
    flowAuto: $('#bf-on').checked, excludeEtfs: $('#bf-etf').checked, flowAskSide: $('#bf-ask').checked, flowSweeps: $('#bf-sweep').checked,
    flowMinPremium: v('#bf-prem'), flowMinDte: v('#bf-dmin'), flowMaxDte: v('#bf-dmax'), flowMinVolOi: v('#bf-voi'), flowCooldownMin: v('#bf-cool'), flowMaxEvals: v('#bf-evals'), flowWindowMin: v('#bf-win'), repeatEnabled: $('#bf-rep').checked, repeatDays: v('#bf-rdays'), repeatMinPremium: v('#bf-rprem'), repeatMinHits: v('#bf-rhits'), repeatMinMinutes: v('#bf-rmin'), repeatMinTotal: v('#bf-rtot'), putCheck: $('#bf-put').checked, putBlock: $('#bf-putb').checked, putMaxRatio: v('#bf-putr'), chaseStep: v('#bs-cstep'), chaseSeconds: v('#bs-csec'), chaseMaxSteps: v('#bs-cmax'), chaseMaxPct: v('#bs-cpct'),
    strategy: v('#bs-strat'), spreadStopPct: v('#bs-sstop'), spreadShortDeltaMin: v('#bs-sdmin'), spreadShortDeltaMax: v('#bs-sdmax'),
    spreadMaxDebitPct: v('#bs-sdebit'), spreadTargetPct: v('#bs-starget'), diagLongDteMin: v('#bs-dldmin'), diagLongDteMax: v('#bs-dldmax'),
    diagLongDeltaMin: v('#bs-dlxmin'), diagLongDeltaMax: v('#bs-dlxmax'), diagShortDteMin: v('#bs-dsdmin'), diagShortDteMax: v('#bs-dsdmax'),
    diagShortDeltaMin: v('#bs-dsxmin'), diagShortDeltaMax: v('#bs-dsxmax'), diagTargetPct: v('#bs-dtarget'), diagMinShortCredit: v('#bs-dcredit'), diagRollDte: v('#bs-droll'), diagRollWaitMin: v('#bs-drollwait') };
  if (body.autoSubmit && !(S.bot.settings || {}).autoSubmit && !confirm('Automatic paper orders: the bot will place paper trades by itself when every rule passes. Continue?')) return;
  try {
    const risk = +v('#bs-risk');
    if (risk > 0 && risk !== (S.me.settings || {}).riskPerTrade) S.me.settings = await api('/settings', { method: 'PUT', body: { riskPerTrade: risk } });
    S.bot.settings = await api('/bot/settings', { method: 'PUT', body }); toast('Bot settings saved'); if (!quiet) render();
  } catch (e) { toast(e.message); }
}

/* ---------- manual refresh (button + pull down), and refresh when the app comes back to the foreground ---------- */
let refreshing = false, hiddenAt = 0;
async function refreshNow(quiet) {
  if (refreshing) return;
  refreshing = true;
  const b = $('#refresh-btn'); if (b) b.classList.add('spin');
  try {
    const v = route();
    await reload();
    if (v === 'bot' || v === 'dashboard') { await loadBot(true); }
    if (v === 'botevals') { await loadBot(true); await loadEvals(S.botEvals ? S.botEvals.page : 1); }
    if (v === 'botchart' && S.fcfMode === 'ww') { delete S.fc[fcKey('ww-' + fcfSymbol())]; await loadWatch(); }
    else if (v === 'botchart') { const c = ((S.tickers && S.tickers.items) || []).find(x => x.symbol === fcfSymbol()); if (c) { delete S.fc[fcKey(c.id)]; delete S.fcDp[fcKey(c.id)]; } await loadTickers(); }
    if (v === 'botskipped') { await loadShadow(); }
    if (v === 'botbursts') { await loadBursts(); }
    if (v === 'botexplore') { await runExplore(false); }
    if (v === 'botwatch') { await loadWatch(); }
    if (v === 'botstrats') { await loadStrats(); }
    else if (v === 'gex') { if (S.flow && !S.flowBusy && $('#fl-prem')) loadFlow(); loadMarketGamma(true); }
    else if (v === 'coach' || v === 'dashboard') { await loadNotes(true); }
    if (S.botRes && S.botRes.id && S.bot && S.bot.items) { const n = S.bot.items.find(i => i.id === S.botRes.id); if (n) S.botRes = { ...S.botRes, ...n }; }
    render();
    if (!quiet) toast('Up to date');
  } catch (e) { toast(e.message); }
  finally { refreshing = false; const b2 = $('#refresh-btn'); if (b2) b2.classList.remove('spin'); }
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') { hiddenAt = Date.now(); return; }
  if (hiddenAt && Date.now() - hiddenAt > 15000 && !$('#app').hidden) refreshNow(true);   // back from another app / lock screen
});
window.addEventListener('pageshow', e => { if (e.persisted && !$('#app').hidden) refreshNow(true); });
(() => {   // pull down from the top of the page to refresh (phones)
  let y0 = null, pulled = 0;
  const ind = () => $('#pull-ind');
  window.addEventListener('touchstart', e => { y0 = window.scrollY <= 0 && !document.body.style.overflow ? e.touches[0].clientY : null; pulled = 0; }, { passive: true });
  window.addEventListener('touchmove', e => {
    if (y0 === null) return;
    pulled = Math.max(0, e.touches[0].clientY - y0);
    const i = ind(); if (i) { i.style.opacity = Math.min(1, pulled / 80); i.textContent = pulled > 80 ? 'Release to refresh' : 'Pull to refresh'; }
  }, { passive: true });
  window.addEventListener('touchend', () => {
    const i = ind(); if (i) i.style.opacity = 0;
    if (y0 !== null && pulled > 80) refreshNow();
    y0 = null; pulled = 0;
  });
})();

/* ---------- auto refresh (every minute, only while the page is visible and you're not typing) ---------- */
setInterval(() => {
  if (document.visibilityState !== 'visible') return;
  const a = document.activeElement;
  if (a && ['INPUT', 'TEXTAREA', 'SELECT'].includes(a.tagName)) return;
  if (isBotRoute()) { loadBot(true).then(() => { if (!S.botEvals || S.botEvals.page === 1) loadEvals(1); else if (isBotRoute()) render(); }); }
  else if (route() === 'gex' && S.flow && S.flowAuto !== false && !S.flowBusy && $('#fl-prem')) loadFlow();
}, 60000);

/* ---------- router & events ---------- */
/* ---------- navigation groups: Trades (list, stats, coach, journal) and Settings (general, import) ---------- */
const GROUPS = {
  trades: [['trades', 'Trades'], ['analytics', 'Stats'], ['coach', 'Coach'], ['journal', 'Journal']],
  settings: [['settings', 'General'], ['import', 'Import']],
  bot: [['bot', 'Overview'], ['botevals', 'Evaluations'], ['botchart', 'Chart'], ['botwatch', 'Wall watch'], ['botexplore', 'Explorer'], ['botstrats', 'Strategies']],
};
const isBotRoute = () => ['bot', 'botevals', 'botchart', 'botwatch', 'botlab', 'botexplore', 'botstrats'].includes(route());
const groupOf = v => Object.keys(GROUPS).find(g => GROUPS[g].some(([r]) => r === v));
function subnav(v) {
  const g = groupOf(v);
  if (!g) return '';
  return `<nav class="subnav" aria-label="${{ trades: 'Trades', settings: 'Settings', bot: 'Paper bot' }[g]} sections">${GROUPS[g].map(([r, l]) =>
    `<a href="#${r}" ${r === v ? 'aria-current="page"' : ''}>${l}</a>`).join('')}</nav>`;
}
function toggleAccount(force) {
  const m = $('#acct-menu'); if (!m) return;
  const open = force !== undefined ? force : m.hidden;
  m.hidden = !open;
  document.querySelectorAll('[data-acct]').forEach(b => b.setAttribute('aria-expanded', open ? 'true' : 'false'));
}
document.addEventListener('click', e => {
  if (e.target.closest('[data-acct]')) { e.preventDefault(); toggleAccount(); return; }
  if (!e.target.closest('#acct-menu')) toggleAccount(false);
  if (e.target.closest('#acct-menu a')) toggleAccount(false);
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') toggleAccount(false); });
const VIEWS = { bot: [() => vBot(), () => { loadBot(); loadNtStatus(); }],
  botevals: [vBotEvals, () => {
    if (!S.bot) loadBot();
    const t = evTicker(), mode = t ? `t:${t}` : 'group';
    if ((!S.botEvals || S.botEvals.mode !== mode) && S.evLoading !== mode) { S.evLoading = mode; loadEvals(1).finally(() => { S.evLoading = null; }); }
  }],
  botexplore: [vExplore, () => { if (!S.ex && !S.exBusy) runExplore(false); }],
  botwatch: [vWatch, () => { if (!S.ww) loadWatch(); if (!S.rt && !S.rtBusy) loadRuleTest(); }],
  botlab: [() => '', () => { S.chartLab = true; try { localStorage.setItem('chartLab', '1'); } catch (e) {} fcfSetMode('ww'); location.replace('#botchart' + (hashParam('t') ? '?t=' + encodeURIComponent(hashParam('t')) : '')); }],
  botchart: [vChartFull, () => { if (!S.tickers && !S.tkBusy) loadTickers();
    const h = document.querySelector('.fcf-head'), inf = document.querySelector('.fcf-info');      // fit the chart to what's left
    const hh = h ? h.offsetHeight : 0, ih = inf ? inf.offsetHeight + 8 : 0;
    if (hh && (Math.abs(hh - (S.fcfHeadH || 0)) > 3 || (inf && Math.abs(ih - (S.fcfInfoH || 0)) > 3))) { S.fcfHeadH = hh; if (inf) S.fcfInfoH = ih; render(); } }],
  botstrats: [vStrats, () => { if (!S.strats && !S.stratsBusy) loadStrats(); if (!S.bot) loadBot(); }], dashboard: [vDashboard, async () => { if (!S.bot) loadBot(); if (S.notes === undefined) { await loadNotes(); if (route() === 'dashboard') render(); } }], trades: [vTrades], analytics: [vAnalytics], coach: [vCoach, afterCoach], journal: [vJournal, afterJournal], import: [vImport, afterImport], settings: [vSettings, afterSettings] };
const route = () => { const r = location.hash.slice(1).split('?')[0] || 'dashboard'; return VIEWS[r] ? r : 'dashboard'; };
const hashParam = k => new URLSearchParams(location.hash.split('?')[1] || '').get(k);
const evTicker = () => route() === 'botevals' ? (hashParam('t') || '').toUpperCase() : '';
function render() {
  if (S.fczDrag) { S.renderPending = true; return; }   // a drag-zoom in progress: redraw once it ends (else the drag is lost)
  const v = route();
  const grp = groupOf(v);
  document.querySelectorAll('.nav a[data-r]').forEach(a => {
    const on = a.dataset.r === v || (grp === 'trades' && a.dataset.r === 'trades') || (grp === 'bot' && a.dataset.r === 'bot');
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  document.querySelectorAll('.nav .nav-sub a').forEach(a => { if (a.dataset.r === v) a.setAttribute('aria-current', 'page'); });
  document.querySelectorAll('.nav .nav-sub').forEach(x => { x.hidden = grp !== (x.dataset.group || 'trades'); });
  document.querySelectorAll('[data-acct]').forEach(b => b.classList.toggle('on', grp === 'settings'));
  const keep = {};                                      // scroll positions survive the redraw (watchlists, side panel, chart page)
  document.querySelectorAll('[data-wlbox],.fcf-side').forEach(el => { keep[el.dataset.wlbox ? 'wl:' + el.dataset.wlbox : el.className.split(' ')[0]] = el.scrollTop; });
  const f0 = document.querySelector('.fcf'), fh = f0 ? f0.scrollHeight : 0;
  if (f0 && S.fcfY == null) S.fcfY = f0.scrollTop;
  const winY = window.scrollY;
  S.renderAt = Date.now();
  try { $('#view').innerHTML = subnav(v) + VIEWS[v][0](); } catch (e) { console.error(e); $('#view').innerHTML = `<div class="errbox">This page failed to load: ${esc(e.message)}</div>`; }
  document.querySelectorAll('[data-wlbox],.fcf-side').forEach(el => { const k = el.dataset.wlbox ? 'wl:' + el.dataset.wlbox : el.className.split(' ')[0]; if (keep[k]) el.scrollTop = keep[k]; });
  const f1 = document.querySelector('.fcf');
  if (f1 && S.fcfY) {                                   // the full-screen chart page: keep the place even while parts reload
    const body = f1.querySelector('.fcf-body');
    if (body && fh) { body.style.minHeight = (fh - (f1.querySelector('.fcf-head') || {}).offsetHeight || 0) + 'px'; clearTimeout(S.fcfMinT); S.fcfMinT = setTimeout(() => { const b = document.querySelector('.fcf-body'); if (b) b.style.minHeight = ''; }, 4000); }
    f1.scrollTop = S.fcfY;
    requestAnimationFrame(() => { const f = document.querySelector('.fcf'); if (f && S.fcfY) f.scrollTop = S.fcfY; });
  } else if (!f1 && winY) window.scrollTo(0, winY);
  if (VIEWS[v][1]) VIEWS[v][1]();
}
document.addEventListener('scroll', e => {             // remember where the chart page is scrolled (not the jumps caused by a redraw)
  const t = e.target;
  if (t && t.classList && t.classList.contains('fcf') && Date.now() - (S.renderAt || 0) > 400) S.fcfY = t.scrollTop;
}, true);
async function reload() { const [me, tr] = await Promise.all([api('/me'), api('/trades')]); S.me = me; S.trades = tr.trades.sort(chron); if (!cur) render(); }
window.addEventListener('hashchange', () => { S.fcfY = 0; render(); window.scrollTo(0, 0); });
document.addEventListener('click', e => {
  const el = e.target.closest('[data-rx],[data-stview],[data-mvday],[data-mvreload],[data-chartdaily],[data-hideanalysis],[data-sdsw],[data-sdset],[data-sdapply],[data-sdwatch],[data-sdprep],[data-sdbt],[data-sdchart],[data-sddisconnect],[data-dtrob],[data-dtsearch],[data-dtsrchuse],[data-dtwexp],[data-dtcmpcur],[data-dtpresel],[data-dtpresave],[data-dtpredel],[data-dtpreload],[data-dtcmp],[data-dtstudy],[data-dtsw],[data-dtapply],[data-dttf],[data-dtdays],[data-dtreload],[data-dtdisconnect],[data-dtbt],[data-wwreview],[data-wwrevhide],[data-wwrevcloseall],[data-wltoggle],[data-wlnew],[data-wldel],[data-wlren],[data-wlrm],[data-wlmv],[data-labtolive],[data-optrun],[data-optsym],[data-optshow],[data-labtf],[data-labpreset],[data-rsrun],[data-trycombo],[data-rtlab],[data-rtsrc],[data-hidesettings],[data-hidestats],[data-chartlab],[data-labsw],[data-labapply],[data-labreset],[data-labtest],[data-regrun],[data-pvrun],[data-wwdays],[data-wwallrules],[data-fcalllayers],[data-rtrob],[data-itdrun],[data-rthist],[data-rtrun],[data-wwsw],[data-wwflow],[data-wwrule],[data-wwparams],[data-wwtk],[data-wwfill],[data-fcfmode],[data-wwsel],[data-wwchart],[data-wwrun],[data-wwsave],[data-eoprep],[data-eorun],[data-eouse],[data-exsave],[data-actsave],[data-fcdays],[data-fczoom],[data-replay],[data-fclayer],[data-fcd],[data-fce],[data-evload],[data-fcreload],[data-wallsfill],[data-stmode],[data-stdel],[data-stload],[data-mcrun],[data-exadd],[data-exdel],[data-exclear],[data-exsearch],[data-exuse],[data-burstgrp],[data-burstsave],[data-burstrun],[data-fci],[data-shwhy],[data-shclaude],[data-shopen],[data-botsave],[data-evpage],[data-uwtest],[data-reeval],[data-evticker],[data-evopen],[data-resclose],[data-bottoggle],#ana-run,#nt-save,#nt-test,[data-savesettings],[data-rescan],#fl-load,[data-undo],#rm-csv,#uw-save,#uw-del,[data-botchase],#bot-eval,#bs-save,[data-botrun],[data-botorder],[data-aicheck],[data-aireview],#refresh-btn,#sh-run,[data-botclose],[data-botdismiss],[data-botshow],#tc-run,#mkt-refresh,[data-gexlink],[data-gex],#gx-run,[data-coach],[data-sort],[data-cal],[data-bars],[data-day],[data-range],[data-open],[data-tf],[data-tag],[data-emo],[data-wi],[data-bd],[data-ask],[data-score],#dr-close,#scrim,#rp-play,#save-plan,#rv-run,#j-save,#rep-run,#s-save,#al-save,#al-sync,#al-del,#sch-connect,#sch-reconnect,#sch-sync,#sch-del,#al-show,#ctx-run,#ctx-all,#q-run,#q-all,#rebuild,#signout');
  if (!el) return;
  if (el.dataset.rx) { rsAction(el.dataset.rx.split('|')); return; }
  if (el.dataset.mvday) { S.mvDay = +el.dataset.mvday; render(); return; }
  if (el.dataset.stview) {
    const [act, id, sym, n] = el.dataset.stview.split('|');
    if (act === 'off') { S.dtView = null; render(); return; }
    if (act === 'open') { S.stratOpen = S.stratOpen === id ? null : id; render(); return; }
    const x = ((((S.rs || {}).day || {}).strategies) || []).find(s0 => s0.id === id);
    if (!x) { toast('strategy not found'); return; }
    S.dtView = { id, name: x.name, cfg: { ...(x.full || {}), ...(x.universe === 'inplay' ? { barMin: 5 } : {}) } };
    S.dtDays = [1, 3, 5, 10, 20, 30].find(v => v >= +n) || 30; S.dtTf = 5;
    location.hash = `#botchart?t=${encodeURIComponent(sym)}`; render(); return;
  }
  if (el.hasAttribute('data-mvreload')) { loadMovers(); return; }
  if (el.dataset.savesettings) { saveSettings(); return; }
  if (el.dataset.rescan) { reEvaluate(el.dataset.rescan); return; }
  if (el.dataset.botsave) { botSaveSettings(); return; }
  if (el.dataset.evpage) { loadEvals(+el.dataset.evpage); return; }
  if (el.dataset.reeval) { reEvaluate(el.dataset.reeval); return; }
  if (el.dataset.shwhy !== undefined) { openShadow('why', el.dataset.shwhy); return; }
  if (el.dataset.shclaude !== undefined) { openShadow('claude', el.dataset.shclaude); return; }
  if (el.dataset.shopen) { openRecord(el.dataset.shopen); return; }
  if (el.dataset.mcrun) { runMC(); return; }
  if (el.dataset.eorun) { runExitOpt(); return; }
  if (el.dataset.eoprep) { prepPaths(); return; }
  if (el.dataset.eouse !== undefined) { const x = S.eo.results[+el.dataset.eouse]; S.exExit = JSON.stringify(S.exExit) === JSON.stringify(x.params) ? null : x.params; render(); return; }
  if (el.dataset.exsave) { saveStrategy(); return; }
  if (el.dataset.fclayer) { const k = el.dataset.fclayer; S.fcLayers = { ...S.fcLayers, [k]: !S.fcLayers[k] };
    try { localStorage.setItem('fcLayers', JSON.stringify(S.fcLayers)); } catch (err) {}
    render(); return; }
  if (el.dataset.evload) { e.preventDefault(); api(`/bot/${el.dataset.evload}`).then(it => { S.botRes = it; S.fcSelE = null; if (route() !== 'botevals') location.hash = `#botevals?t=${encodeURIComponent(it.symbol)}`; else { render(); window.scrollTo(0, 0); } }).catch(err => toast(err.message)); return; }
  if (el.dataset.replay !== undefined) { e.stopPropagation(); const its = (S.ex && S.ex.items) || []; const x = its[+el.dataset.replay]; if (!x) return;
    S.replay = its.slice(0, 80).map(i => ({ id: i.id, symbol: i.symbol })); S.fcDays = 45;
    location.hash = `#botchart?t=${encodeURIComponent(x.symbol)}&id=${x.id}`; return; }
  if (el.dataset.wallsfill) { el.disabled = true; api('/bot/walls/backfill', { method: 'POST' }).then(() => toast('Storing the walls in the background (a few minutes for many tickers). Refresh the Explorer later.')).catch(err => { el.disabled = false; toast(err.message); }); return; }
  if (el.dataset.fcreload) { const id = el.dataset.fcreload; delete S.fc[fcKey(id)]; loadFlowChart(id); render(); return; }
  if (el.dataset.fce) { const [id, k] = el.dataset.fce.split('|'); S.fcSelE = { id, k: +k }; render(); return; }
  if (el.dataset.fcd) { const [id, k] = el.dataset.fcd.split('|'); S.fcSelD = { id, k: +k }; render(); return; }
  if (el.dataset.fczoom) { const [id, k] = el.dataset.fczoom.split('|'), d = S.fc[fcKey(id)], N = ((d && d.bars) || []).length; if (N < 2) return;
    const [a, b] = fcRange(id, N), span = b - a, mid = (a + b) / 2;
    if (k === 'reset') { S.fcZoom[fcKey(id)] = null; render(); }
    else if (k === 'in') fcSetZoom(id, mid - span / 4, mid + span / 4, N);
    else if (k === 'out') fcSetZoom(id, mid - span, mid + span, N);
    else if (k === 'left') fcSetZoom(id, a - span / 3, b - span / 3, N);
    else if (k === 'right') fcSetZoom(id, a + span / 3, b + span / 3, N);
    return; }
  if (el.dataset.fcdays) { S.fcDays = +el.dataset.fcdays; S.fcSel = null; render(); return; }
  if (el.dataset.actsave) { const strategy = document.getElementById('act-strat').value, stop = document.getElementById('act-stop').value;
    api('/bot/strategies/active', { method: 'PUT', body: { strategy, stop: stop ? +stop : null } })
      .then(r => { toast(`Bot now trades: ${r.name}${r.stop ? ` · stop -${r.stop}%` : ''}`); if (S.bot) S.bot.active = r; loadStrats(); }).catch(err => toast(err.message)); return; }
  if (el.dataset.stmode) { const [id, mode] = el.dataset.stmode.split('|');
    if (mode === 'trade' && !confirm('Let this strategy place paper orders on its own matches?')) return;
    api(`/bot/strategies/${id}`, { method: 'PUT', body: { mode } }).then(() => { toast(`Strategy: ${mode}`); loadStrats(); }).catch(err => toast(err.message)); return; }
  if (el.dataset.stdel) { if (!confirm('Delete this strategy?')) return; api(`/bot/strategies/${el.dataset.stdel}`, { method: 'DELETE' }).then(loadStrats).catch(err => toast(err.message)); return; }
  if (el.dataset.stload) { const x = S.strats.items.find(i => i.id === el.dataset.stload); S.exConds = x.conds.map(c => ({ key: c.key, op: c.op, value: c.value })); S.exExit = x.exit || null; S.ex = null; location.hash = '#botexplore'; return; }
  if (el.dataset.exadd) {
    const f = (S.ex.features || []).find(x => x.key === S.exPick); if (!f) return;
    let c;
    if (f.kind === 'bool') c = { key: f.key, value: $('#ex-val').value === '1' };
    else { const v = parseFloat($('#ex-num').value); if (isNaN(v)) { toast('Enter a value'); return; } c = { key: f.key, op: $('#ex-op').value, value: v }; }
    S.exConds = [...S.exConds.filter(x => x.key !== c.key), c]; S.exPick = null; runExplore(false); return;
  }
  if (el.dataset.exdel !== undefined) { S.exConds.splice(+el.dataset.exdel, 1); runExplore(false); return; }
  if (el.dataset.exclear) { S.exConds = []; runExplore(false); return; }
  if (el.dataset.fcfmode) { fcfSetMode(el.dataset.fcfmode); if (el.dataset.fcfmode === 'ww' && !S.ww) loadWatch(); else if (el.dataset.fcfmode === 'ev' && !S.tickers) loadTickers(); location.hash = '#botchart'; render(); return; }
  if (el.dataset.wwsel) { const [sym, at] = el.dataset.wwsel.split('|'); S.wwSel = { sym, at, zoom: true }; render(); return; }
  if (el.dataset.wwchart) { const [sym, at] = el.dataset.wwchart.split('|'); fcfSetMode('ww'); S.wwSel = at ? { sym, at, zoom: true } : null; location.hash = '#botchart?t=' + encodeURIComponent(sym); return; }
  if (el.dataset.wwflow) { S.wwFlowBusy = true; render(); api('/bot/wallwatch/flowstudy', { method: 'POST', body: {} }).then(() => { toast('Matching every signal with the flow — a few minutes'); let n = 0; const tk = async () => { n++; await loadWatch(); const f = (S.ww && S.ww.flowStudy) || {}; if (f.running && n < 20) setTimeout(tk, 20000); else { S.wwFlowBusy = false; render(); } }; setTimeout(tk, 20000); }).catch(err => { S.wwFlowBusy = false; toast(err.message); render(); }); return; }
  if (el.dataset.wwrule !== undefined) { const v = +el.dataset.wwrule; api('/bot/wallwatch', { method: 'PUT', body: { flowRule: v < 0 ? null : v } }).then(r => { S.ww = r; toast(v < 0 ? 'Flow filter removed' : 'Only signals passing this flow condition will be bought'); render(); }).catch(err => toast(err.message)); return; }
  if (el.dataset.wwsw) {                 // flip a rule ON/OFF in place (typed values stay); saved on Apply
    const k = el.dataset.wwsw, sw = wwSwState(); sw[k] = !sw[k];
    document.querySelectorAll(`[data-wwsw="${k}"]`).forEach(b => { const onv = sw[k], wrap = b.parentElement;
      b.textContent = onv ? 'ON' : 'OFF'; b.style.background = onv ? 'var(--gain)' : ''; b.style.color = onv ? '#fff' : ''; b.style.borderColor = onv ? 'transparent' : '';
      wrap.style.opacity = onv ? '' : '.55'; wrap.style.borderColor = onv ? 'var(--gain)' : 'var(--line)'; });
    S.wwDirty = true; wwMarkDirty();                                               // saved only on Apply
    return;
  }
  if (el.dataset.wwparams) { wwSaveParams(el.dataset.wwparams); return; }
  if (el.dataset.chartdaily !== undefined) { S.chartDaily = el.dataset.chartdaily === '1'; try { localStorage.setItem('chartDaily', S.chartDaily ? '1' : '0'); } catch (e) {} render(); return; }
  if (el.hasAttribute('data-hidesettings')) { S.hideSettings = !S.hideSettings; S.fcfHeadH = null; try { localStorage.setItem('hideSettings', S.hideSettings ? '1' : '0'); } catch (e) {} render(); return; }
  if (el.hasAttribute('data-hidestats')) { S.hideStats = !S.hideStats; S.fcfInfoH = S.hideStats ? 0 : null; try { localStorage.setItem('hideStats', S.hideStats ? '1' : '0'); } catch (e) {} render(); return; }
  if (el.hasAttribute('data-chartlab')) { S.chartLab = !S.chartLab; try { localStorage.setItem('chartLab', S.chartLab ? '1' : '0'); } catch (e) {} render(); return; }
  if (el.dataset.labtf) { S.labDraft = { ...(S.labDraft || {}), tf: el.dataset.labtf }; S.labDirty = true; render(); return; }
  if (el.dataset.labpreset) { const { tf, ...sw } = DAILY_PRESET; S.labDraft = { ...(S.labDraft || {}), tf }; S.labSw = { ...(S.labSw || {}), ...sw }; S.labDirty = true; render(); return; }
  if (el.dataset.labsw) { const k = el.dataset.labsw; S.labSw = { ...(S.labSw || {}) }; const L = labCfg(); S.labSw[k] = !((k in S.labSw) ? S.labSw[k] : L[k]); S.labDirty = true; render(); return; }
  if (el.hasAttribute('data-labapply')) { const L = labCfg();
    document.querySelectorAll('[data-labnum]').forEach(i => { const v = parseFloat(i.value); if (!isNaN(v)) L[i.dataset.labnum] = i.dataset.scale ? v * +i.dataset.scale : v; });
    document.querySelectorAll('[data-labfx]').forEach(i => { L[i.dataset.labfx] = i.value.trim(); });
    Object.assign(L, S.labDraft || {}, S.labSw || {}); S.labDraft = {}; S.labSw = {}; S.labDirty = false;
    if (L.tf === '1D' && !S.chartDaily) { S.chartDaily = true; try { localStorage.setItem('chartDaily', '1'); } catch (e) {} }
    labRedraw(); return; }
  if (el.dataset.wltoggle) { S.wlClosed = { ...(S.wlClosed || {}), [el.dataset.wltoggle]: !(S.wlClosed || {})[el.dataset.wltoggle] }; try { localStorage.setItem('wlClosed', JSON.stringify(S.wlClosed)); } catch (x) {} render(); return; }
  if (el.dataset.dtwexp) { S.dtDraft = { ...(S.dtDraft || {}), wallNear: el.dataset.dtwexp === 'near' }; S.dtDirty = true; render(); return; }
  if (el.dataset.dtsw) { const k = el.dataset.dtsw; S.dtDraft = { ...(S.dtDraft || {}), [k]: !dtCfg()[k] }; S.dtDirty = true; render(); return; }
  if (el.hasAttribute('data-dtapply')) {
    const body = dtBody();
    S.dtDraft = {}; S.dtDirty = false; dtPut(body, 'Day-trading settings saved'); return; }
  if (el.dataset.dttf) { S.dtTf = +el.dataset.dttf; try { localStorage.setItem('dtTf', S.dtTf); } catch (x) {} render(); return; }
  if (el.dataset.dtdays) { S.dtDays = +el.dataset.dtdays; try { localStorage.setItem('dtDays', S.dtDays); } catch (x) {} render(); return; }
  if (el.hasAttribute('data-dtreload')) { S.dtFc = null; render(); return; }
  if (el.hasAttribute('data-dtdisconnect')) { if (!confirm('Disconnect the day-trading account?')) return; api('/daytrade/account', { method: 'DELETE' }).then(r => { S.dt = r; render(); }).catch(err => toast(err.message)); return; }
  if (el.hasAttribute('data-hideanalysis')) { S.hideAnalysis = !S.hideAnalysis; try { localStorage.setItem('hideAnalysis', S.hideAnalysis ? '1' : ''); } catch (x) {} render(); return; }
  if (el.dataset.sdsw) { const k = el.dataset.sdsw; S.sdDraft = { ...(S.sdDraft || {}), [k]: !sdCfg()[k] }; S.sdDirty = true; render(); return; }
  if (el.dataset.sdset) { const [k, v] = el.dataset.sdset.split('='); S.sdDraft = { ...(S.sdDraft || {}), [k]: isNaN(+v) ? v : +v }; S.sdDirty = true; render(); return; }
  if (el.hasAttribute('data-sdwatch')) { S.sdWatchBusy = true; render(); api('/stockday/watch', { method: 'POST' }).then(r => { S.sd = r; toast('Building the pre-market watchlist (about 30 s)'); let k = 0; const poll = () => { loadSD().then(() => { const w = (S.sd && S.sd.watch) || {}; if ((w.built && w.built >= (w.building || '')) || ++k > 12) { S.sdWatchBusy = false; render(); } else setTimeout(poll, 10000); }); }; setTimeout(poll, 15000); }).catch(err => { S.sdWatchBusy = false; toast(err.message); render(); }); return; }
  if (el.hasAttribute('data-sdapply')) { const b = sdBody(); S.sdDraft = {}; S.sdDirty = false; sdPut(b, 'Stock day-trading settings saved'); return; }
  if (el.hasAttribute('data-sdprep')) { const days = +((document.getElementById('sd-prepdays') || {}).value || 500); S.sdPrepDays = days;
    api('/stockday/prep', { method: 'POST', body: { days } }).then(() => { toast('Building the history in the background'); loadSD(); }).catch(err => toast(err.message)); return; }
  if (el.hasAttribute('data-sdbt')) { const days = +((document.getElementById('sd-btdays') || {}).value || 250); S.sdBtDays = days;
    api('/stockday/backtest', { method: 'POST', body: { days, cfg: sdBody() } }).then(() => { S.sdBt = { ...(S.sdBt || {}), running: 'now' }; render(); clearTimeout(S.sdBtPoll); S.sdBtPoll = setTimeout(loadSDBt, 10000); }).catch(err => toast(err.message)); return; }
  if (el.dataset.sdchart) { e.preventDefault(); const [sym, date] = el.dataset.sdchart.split('|'); S.sdSel = { sym, date: date || '' }; S.fcfY = 0; render(); const f = document.querySelector('.fcf'); if (f) f.scrollTop = 0; return; }
  if (el.hasAttribute('data-sddisconnect')) { if (!confirm('Disconnect the stock day-trading account?')) return; api('/stockday/account', { method: 'DELETE' }).then(r => { S.sd = r; render(); }).catch(err => toast(err.message)); return; }
  if (el.hasAttribute('data-dtrob')) { dtRobRun(dtBody(), 'current settings'); return; }
  if (el.hasAttribute('data-dtsearch')) {
    const days = +((document.getElementById('dt-srdays') || {}).value || 250); S.dtSrDays = days;
    api('/daytrade/search', { method: 'POST', body: { days, cfg: dtBody() } }).then(() => { S.dtSrch = { ...(S.dtSrch || {}), running: 'now' }; render(); clearTimeout(S.dtSrchPoll); S.dtSrchPoll = setTimeout(loadDTSrch, 10000); }).catch(err => toast(err.message));
    return; }
  if (el.hasAttribute('data-dtsrchuse')) { S.dtDraft = { ...(S.dtDraft || {}), ...((S.dtSrch || {}).changes || {}) }; S.dtDirty = true; toast('Loaded: click Apply to use these settings'); render(); return; }
  if (el.hasAttribute('data-dtcmpcur')) { S.dtCmpCur = el.checked; return; }
  if (el.dataset.dtpresel) { S.dtCmpSel = { ...(S.dtCmpSel || {}), [el.dataset.dtpresel]: el.checked }; return; }
  if (el.hasAttribute('data-dtpresave')) {
    const name = ((document.getElementById('dt-prename') || {}).value || '').trim(); if (!name) { toast('Give the settings a name'); return; }
    api('/daytrade/presets', { method: 'PUT', body: { name, cfg: dtBody() } }).then(r => { S.dtPre = r.presets; S.dtCmpSel = { ...(S.dtCmpSel || {}), [name]: true }; toast(`Saved “${name}”`); render(); }).catch(err => toast(err.message)); return; }
  if (el.dataset.dtpredel) { if (!confirm(`Delete “${el.dataset.dtpredel}”?`)) return; api('/daytrade/presets?name=' + encodeURIComponent(el.dataset.dtpredel), { method: 'DELETE' }).then(r => { S.dtPre = r.presets; render(); }).catch(err => toast(err.message)); return; }
  if (el.dataset.dtpreload) { const p0 = (S.dtPre || []).find(x => x.name === el.dataset.dtpreload); if (!p0) return;
    const c0 = (S.dt && S.dt.config) || {}, d = {}; Object.entries(p0.cfg || {}).forEach(([k, v]) => { if (!['enabled', 'trade'].includes(k) && JSON.stringify(v) !== JSON.stringify(c0[k])) d[k] = v; });
    S.dtDraft = d; S.dtDirty = true; toast(`“${p0.name}” loaded: click Apply to use it`); render(); return; }
  if (el.hasAttribute('data-dtcmp')) {
    const days = +((document.getElementById('dt-cmpdays') || {}).value || 120); S.dtCmpDays = days;
    const key = (document.getElementById('dt-swkey') || {}).value || '', vals = ((document.getElementById('dt-swvals') || {}).value || '').replace(/,/g, ' ').split(/\s+/).filter(Boolean).map(Number).filter(x => !isNaN(x));
    S.dtSwKey = key; S.dtSwVals = (document.getElementById('dt-swvals') || {}).value;
    const cur = dtBody(), vs = [];
    if (S.dtCmpCur !== false && !key) vs.push({ name: 'Current', cfg: cur });
    (S.dtPre || []).filter(p0 => (S.dtCmpSel || {})[p0.name]).forEach(p0 => vs.push({ name: p0.name, cfg: p0.cfg }));
    if (key) vals.forEach(v => vs.push({ name: `${key} = ${v}`, cfg: { ...cur, [key]: v } }));
    if (!vs.length) { toast('Pick the settings to compare'); return; }
    if (vs.length > 10) { toast('10 settings at most'); return; }
    api('/daytrade/compare', { method: 'POST', body: { days, variants: vs, opt: dtOptGet() } }).then(() => { S.dtCmp = { ...(S.dtCmp || {}), running: 'now' }; render(); clearTimeout(S.dtCmpPoll); S.dtCmpPoll = setTimeout(loadDTCmp, 10000); }).catch(err => toast(err.message));
    return; }
  if (el.hasAttribute('data-dtstudy')) {
    const days = +((document.getElementById('dt-stdays') || {}).value || 250); S.dtStDays = days;
    api('/daytrade/study', { method: 'POST', body: { days, wallNear: !!dtCfg().wallNear } }).then(() => { S.dtSt = { ...(S.dtSt || {}), running: 'now' }; render(); clearTimeout(S.dtStPoll); S.dtStPoll = setTimeout(loadDTStudy, 10000); }).catch(err => toast(err.message));
    return; }
  if (el.hasAttribute('data-dtbt')) {
    const days = +((document.getElementById('dt-btdays') || {}).value || 20); S.dtBtDays = days; S.dtBtBusy = true; render();
    const cfg = dtBody(), opt = dtOptGet();
    api('/daytrade/backtest', { method: 'POST', body: { days, cfg, opt } }).then(() => { S.dtBt = { ...(S.dtBt || {}), running: 'now' }; S.dtBtBusy = false; render(); clearTimeout(S.dtBtPoll); S.dtBtPoll = setTimeout(loadDTBt, 10000); })
      .catch(err => { S.dtBt = { error: err.message }; S.dtBtBusy = false; render(); });
    return; }
  if (el.hasAttribute('data-wwreview')) { wwReview(); return; }
  if (el.hasAttribute('data-wwrevhide')) { S.wwRev = null; render(); return; }
  if (el.hasAttribute('data-wwrevcloseall')) {
    const bad = ((S.wwRev || {}).rows || []).filter(x => x.verdict === 'exit' || x.verdict === 'no entry');
    if (!bad.length || !confirm(`Close ${bad.length} paper position${bad.length > 1 ? 's' : ''} at market (${bad.map(x => x.symbol).join(', ')})?`)) return;
    Promise.all(bad.map(x => api(`/bot/${x.id}/close`, { method: 'POST' }).catch(err => toast(`${x.symbol}: ${err.message}`))))
      .then(async () => { toast('Close orders sent'); S.wwRev = null; await loadWatch(); render(); });
    return; }
  if (el.hasAttribute('data-wlnew')) { const nm = (prompt('Name of the new watchlist') || '').trim(); if (!nm) return; wlPut({ newList: nm }, `List ${nm} created`); return; }
  if (el.dataset.wldel) { if (!confirm(`Delete the list ${el.dataset.wldel}? Its tickers stop being scanned unless they are in another list.`)) return; wlPut({ deleteList: el.dataset.wldel }, 'List deleted'); return; }
  if (el.dataset.wlren) { const nm = (prompt('New name', el.dataset.wlren) || '').trim(); if (!nm) return; wlPut({ renameList: el.dataset.wlren, to: nm }, 'List renamed'); return; }
  if (el.dataset.wlrm) { const [ln, t] = el.dataset.wlrm.split('|'); e.preventDefault(); wlPut({ remove: t, list: ln }, `${t} removed from ${ln}`); return; }
  if (el.dataset.wlmv) { const [ln, t] = el.dataset.wlmv.split('|'); e.preventDefault();
    const names = ((S.ww && S.ww.config && S.ww.config.lists) || []).map(L => L.name).filter(n => n !== ln);
    const to = (prompt(`Move ${t} from ${ln} to which list?\n${names.join(' · ')}`, names[0] || '') || '').trim();
    if (!to) return; if (!names.includes(to)) { toast(`No list named ${to}`); return; }
    wlPut({ move: t, to }, `${t} moved to ${to}`); return; }
  if (el.hasAttribute('data-labtolive')) {
    const L = { ...(labCfg() || {}), ...(S.labDraft || {}), ...(S.labSw || {}) };
    if (L.tf === '1D' && !confirm('The lab is on the 1D timeframe; the live bot trades hourly candles. Copy the rules anyway?')) return;
    if (!confirm('Replace the live bot settings with these lab settings? Signals are recomputed.')) return;
    const body = Object.fromEntries(Object.entries(L).filter(([k, v]) => !['tf', 'pwTest', 'pvSetup'].includes(k) && v !== undefined));
    api('/bot/wallwatch', { method: 'PUT', body: { ...body, retestSetup: true } })
      .then(r => { S.ww = { ...(S.ww || {}), ...r }; S.wwSw = null; S.wwDraft = {}; S.wwDirty = false;
        return api('/bot/wallwatch/run', { method: 'POST', body: { days: 45 } }).catch(() => {}); })
      .then(() => { toast('Live bot now uses the lab settings — recomputing signals'); render(); }).catch(err => toast(err.message));
    return; }
  if (el.hasAttribute('data-labreset')) { S.lab = null; S.labDraft = {}; S.labSw = {}; S.labDirty = false; try { localStorage.removeItem('labCfg'); } catch (e) {} labRedraw(); return; }
  if (el.dataset.optsym) { optSymRun(el.dataset.optsym); return; }
  if (el.hasAttribute('data-optshow')) { S.hideStats = false; S.fcfInfoH = null; try { localStorage.setItem('hideStats', '0'); } catch (e) {} render();
    setTimeout(() => { const h = document.getElementById('opt-test'); if (h) h.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 80); return; }
  if (el.hasAttribute('data-optrun')) {
    const g = id => parseFloat(($('#' + id) || {}).value);
    const P = { sel: (($('#opt-sel') || {}).value) || 'live', dteMin: g('opt-dmin'), dteMax: g('opt-dmax'), money: g('opt-mon'), tp: g('opt-tp'), sl: g('opt-sl'), slip: g('opt-slip'), follow: !!($('#opt-follow') || {}).checked };
    Object.keys(P).forEach(k => { if (typeof P[k] === 'number' && isNaN(P[k])) delete P[k]; });
    try { localStorage.setItem('optParams', JSON.stringify(P)); } catch (e) {}
    api('/bot/lab/options', { method: 'POST', body: P }).then(() => { S.optTest = { ...(S.optTest || {}), running: 'now' }; render(); setTimeout(loadOptTest, 15000); }).catch(err => toast(err.message));
    return; }
  if (el.hasAttribute('data-labtest')) {
    S.hideStats = false; S.fcfInfoH = null; try { localStorage.setItem('hideStats', '0'); } catch (x) {}
    api('/bot/lab/test', { method: 'POST', body: { cfg: labCfg(), days: 250 } }).then(() => { S.labTest = { ...(S.labTest || {}), running: 'now' }; toast('Testing every ticker — a few minutes; the results show under the chart (scroll down)'); render(); setTimeout(loadLabTest, 15000); }).catch(err => toast('Test on all tickers failed: ' + err.message));
    return; }
  if (el.hasAttribute('data-regrun')) {
    api('/bot/wallwatch/regime', { method: 'POST', body: { days: parseFloat(($('#rt-days') || {}).value) || 250 } })
      .then(() => { S.rt = { ...(S.rt || {}), regime: { ...((S.rt || {}).regime || {}), running: 'now' } }; render(); setTimeout(loadRuleTest, 15000); }).catch(err => toast(err.message));
    return;
  }
  if (el.hasAttribute('data-pvrun')) {
    api('/bot/wallwatch/pvstudy', { method: 'POST', body: { days: parseFloat(($('#rt-days') || {}).value) || 250 } })
      .then(() => { S.rt = { ...(S.rt || {}), pv: { ...((S.rt || {}).pv || {}), running: 'now' } }; render(); setTimeout(loadRuleTest, 15000); }).catch(err => toast(err.message));
    return;
  }
  if (el.dataset.wwdays) {
    S.wwDays = +el.dataset.wwdays;
    try { localStorage.setItem('wwDays', String(S.wwDays)); } catch (e) {}
    Object.keys(S.fc).filter(x => x.includes('ww-')).forEach(x => { delete S.fc[x]; delete S.fcZoom[x]; });
    Object.keys(S.fcDp || {}).filter(x => x.includes('ww-')).forEach(x => delete S.fcDp[x]);
    render(); return;
  }
  if (el.hasAttribute('data-wwallrules')) { S.wwAllRules = !S.wwAllRules; render(); return; }
  if (el.hasAttribute('data-fcalllayers')) {
    S.fcAllLayers = !S.fcAllLayers;
    if (!S.fcAllLayers) ['mp', 'acc'].forEach(k => { S.fcLayers[k] = false; });   // hidden layers aren't drawn
    try { localStorage.setItem('fcLayers', JSON.stringify(S.fcLayers)); } catch (e) {}
    render(); return;
  }
  if (el.hasAttribute('data-rtlab')) { S.rtUseLab = true; try { localStorage.setItem('rtUseLab', '1'); } catch (e) {} }   // the link then opens Wall watch
  if (el.hasAttribute('data-rsrun')) {
    api('/bot/wallwatch/search', { method: 'POST', body: { days: parseFloat(($('#rt-days') || {}).value) || 250, minTrades: parseFloat(($('#rs-min') || {}).value) || 60, ...(S.rtUseLab && labCfg() ? { cfg: labCfg() } : {}) } })
      .then(() => { S.rt = { ...(S.rt || {}), search: { ...((S.rt || {}).search || {}), running: 'now' } }; render(); setTimeout(loadRuleTest, 15000); }).catch(err => toast(err.message));
    return;
  }
  if (el.dataset.trycombo !== undefined) {
    const want = el.dataset.trycombo ? el.dataset.trycombo.split(',') : [], L = labCfg();
    if (!L) { toast('Open the chart once first'); return; }
    SEARCH_RULES.forEach(k => { L[k] = want.includes(k); }); S.labSw = {}; S.labDirty = false; labSave();
    Object.keys(S.fc).filter(x0 => x0.includes('ww-lab-')).forEach(x0 => { delete S.fc[x0]; });
    S.chartLab = true; try { localStorage.setItem('chartLab', '1'); } catch (e) {}
    fcfSetMode('ww'); location.hash = '#botchart'; toast('Lab entry rules set: check the chart, then Test on all tickers'); return;
  }
  if (el.dataset.rtsrc) { S.rtUseLab = el.dataset.rtsrc === 'lab'; try { localStorage.setItem('rtUseLab', S.rtUseLab ? '1' : '0'); } catch (e) {} render(); return; }
  if (el.hasAttribute('data-rtrob')) {
    api('/bot/wallwatch/robust', { method: 'POST', body: { days: parseFloat(($('#rt-days') || {}).value) || 250, ...(S.rtUseLab && labCfg() ? { cfg: labCfg() } : {}) } })
      .then(() => { S.rt = { ...(S.rt || {}), robust: { ...((S.rt || {}).robust || {}), running: 'now' } }; render(); setTimeout(loadRuleTest, 15000); }).catch(err => toast(err.message));
    return;
  }
  if (el.hasAttribute('data-itdrun')) {
    const v = id => { const x = parseFloat(($(id) || {}).value); return isNaN(x) ? undefined : x; }, c = id => !!($(id) || {}).checked;
    const body = { tickers: ($('#itd-tk') || {}).value || 'QQQ SPY', watchlist: c('#itd-wl'), sessionEnd: (($('#itd-end') || {}).value || '11:30').trim(),
      fastK: v('#itd-fk'), minRR: v('#itd-rr'), maxTrades: v('#itd-mt'), skipEvents: c('#itd-ev'), longs: c('#itd-l'), shorts: c('#itd-s'), useFlip: c('#itd-f') };
    api('/bot/wallwatch/intraday', { method: 'POST', body }).then(() => { S.itd = { ...(S.itd || {}), running: 'now', params: body }; render(); setTimeout(loadIntraday, 15000); }).catch(err => toast(err.message));
    return;
  }
  if (el.hasAttribute('data-rthist')) {
    const days = parseFloat(($('#rt-hdays') || {}).value) || 250;
    api('/bot/wallwatch/history', { method: 'POST', body: { days } }).then(h => { S.rt = { ...(S.rt || {}), history: h }; toast('Loading the history in the background: this can take an hour or more (Unusual Whales limits)'); loadRuleTest(); }).catch(err => toast(err.message));
    return;
  }
  if (el.hasAttribute('data-rtrun')) {
    const v = id => { const x = parseFloat(($(id) || {}).value); return isNaN(x) ? undefined : x; };
    const body = { days: v('#rt-days'), stopAtr: v('#rt-stop'), fixR: v('#rt-fixr'), maxDays: v('#rt-maxd'), full: !!($('#rt-full') || {}).checked, ...(S.rtUseLab && labCfg() ? { cfg: labCfg() } : {}) };
    api('/bot/wallwatch/ruletest', { method: 'POST', body }).then(() => { S.rt = { ...(S.rt || {}), test: { ...((S.rt || {}).test || {}), running: 'now' } }; render(); loadRuleTest(); }).catch(err => toast(err.message));
    return;
  }
  if (el.dataset.wwtk) { wwTkFill(el.dataset.wwtk); return; }
  if (el.dataset.wwfill) { wwFill(); return; }
  if (el.dataset.wwrun) { runWatch(+el.dataset.wwrun); return; }
  if (el.dataset.wwsave) { api('/bot/wallwatch', { method: 'PUT', body: { tickers: $('#ww-list').value } }).then(r => { S.ww = r; toast('Saved'); render(); }).catch(err => toast(err.message)); return; }
  if (el.dataset.exsearch) { S.exMinN = +($('#ex-minn').value || 15); S.exGroup = ($('#ex-group') || {}).value || ''; S.exDepth = +(($('#ex-depth') || {}).value || 4); runExplore(true); return; }
  if (el.dataset.exuse !== undefined) { const x = S.ex.search.results[+el.dataset.exuse]; S.exConds = x.conds.map(c => ({ ...c })); runExplore(false); window.scrollTo(0, 0); return; }
  if (el.dataset.burstgrp) { S.burstOpen = S.burstOpen || {}; S.burstOpen[el.dataset.burstgrp] = !S.burstOpen[el.dataset.burstgrp]; render(); return; }
  if (el.dataset.burstsave) {
    const body = { burstMinPremium: +$('#bu-prem').value, burstMinPrints: +$('#bu-n').value, burstMinRatio: +$('#bu-r').value, burstMinCallShare: (+$('#bu-s').value) / 100 };
    S.burstEdit = true;
    api('/bot/settings', { method: 'PUT', body }).then(st => { if (S.bot) S.bot.settings = st; toast('Saved'); loadBursts(); }).catch(err => toast(err.message));
    return;
  }
  if (el.dataset.burstrun) {
    api('/bot/bursts/replay', { method: 'POST', body: { restart: true } }).then(() => { toast('Replay started: it takes a few minutes. You can leave this page.'); loadBursts(); }).catch(err => toast(err.message));
    return;
  }
  if (el.dataset.fci) { const [id, k] = el.dataset.fci.split('|'); S.fcSel = { id, k: +k }; render(); return; }
  if (el.dataset.uwtest) {
    S.uwTesting = true; render();
    api('/bot/flow/test', { method: 'POST' }).then(r => toast(r.ok ? `Unusual Whales is live (${r.ms} ms): newest alert ${r.newestTicker || ''} ${r.newest ? r.newest.slice(11) + ' ET' : '—'}. With your filters: ${r.filtered20 ?? '?'} alerts in the last 20 min, ${r.filtered60 ?? '?'} in the last 60 min on ${r.filteredTickers ?? '?'} tickers${r.filteredNewest ? `, newest ${r.filteredNewest.slice(11)} ET` : ''}${r.filterError ? ` (filter check failed: ${r.filterError})` : ''}` : `Unusual Whales error: ${r.error}`))
      .catch(err => toast(err.message)).finally(async () => { S.uwTesting = false; await loadBot(true); });
    return;
  }
  if (el.dataset.resclose) { if (isBotRoute()) S.botRes = null; else S.anaRes = null; render(); return; }
  if (el.dataset.evopen) {
    const it = ((S.bot || {}).items || []).find(x => x.id === el.dataset.evopen);
    if (it) { S.botRes = it; location.hash = `#botevals?t=${encodeURIComponent(it.symbol)}`; }
    return;
  }
  if (el.dataset.evticker) { S.botRes = null; location.hash = `#botevals?t=${encodeURIComponent(el.dataset.evticker)}`; return; }
  if (el.dataset.bottoggle) {
    const k = el.dataset.bottoggle, cur = !!((S.bot && S.bot.settings) || {})[k];
    if (!cur && k === 'autoSubmit' && !confirm('Automatic orders: the bot will place paper trades by itself when every rule passes. Turn on?')) return;
    el.disabled = true;
    api('/bot/settings', { method: 'PUT', body: { [k]: !cur } }).then(st => { S.bot.settings = st; render(); toast(`${k === 'flowAuto' ? 'Flow trading' : k === 'autoSubmit' ? 'Automatic orders' : 'Watchlist scans'} ${!cur ? 'on' : 'off'}`); })
      .catch(err => { el.disabled = false; toast(err.message); }); return;
  }
  if (el.id === 'ana-run') { anaEvaluate(S.gexSym); return; }
  if (el.id === 'nt-save' || el.id === 'nt-test') { const body = { notify: { email: $('#nt-email').value.trim(), phone: $('#nt-phone').value, sms: $('#nt-on').checked, events: [...document.querySelectorAll('.nt-ev')].filter(x => x.checked).map(x => x.value) } };
    api('/settings', { method: 'PUT', body }).then(async s2 => { S.me.settings = s2; if (el.id === 'nt-test') { const r = await api('/notify/test', { method: 'POST' }); toast(r.status === 'sent' ? 'Test alert sent' : r.status === 'failed' ? 'Alert failed: ' + (r.error || '') : 'No email or text is set up, so the test was only logged'); } else toast(s2.notify && s2.notify.email ? 'Saved. If this is a new email, confirm the subscription from your inbox.' : 'Alert settings saved'); loadBot(true); loadNtStatus(true); }).catch(err => toast(err.message)); return; }
  if (el.id === 'fl-load') { loadFlow(); return; }
  if (el.dataset.undo) { if (!confirm('Undo this import? The fills it added are removed and trades are rebuilt. Notes and tags are kept.')) return; api(`/imports/${el.dataset.undo}/undo`, { method: 'POST' }).then(async r => { await reload(); await afterImport(); toast(`Removed ${r.removedFills} fills`); }).catch(err => toast(err.message)); return; }
  if (el.id === 'rm-csv') { const a = $('#rm-acct').value; if (!confirm(`Remove all file-imported fills from "${a}"? Broker-synced fills are kept.`)) return; api('/maintenance/remove-csv-fills', { method: 'POST', body: { account: a } }).then(async r => { await reload(); toast(`Removed ${r.removedFills} fills from ${a}`); }).catch(err => toast(err.message)); return; }
  if (el.id === 'uw-save') { api('/flow/key', { method: 'PUT', body: { key: $('#uw-key').value } }).then(() => { toast('Unusual Whales key saved'); loadFlowKey(true); }).catch(err => toast(err.message)); return; }
  if (el.id === 'uw-del') { api('/flow/key', { method: 'PUT', body: { key: '' } }).then(() => loadFlowKey(true)).catch(err => toast(err.message)); return; }
  if (el.dataset.botchase) { api(`/bot/${el.dataset.botchase}/chase`, { method: 'POST' }).then(() => { toast('Chasing the fill…'); setTimeout(() => loadBot(true), 30000); }).catch(err => toast(err.message)); return; }
  if (el.id === 'bot-eval') { botEvaluate(); return; }
  if (el.id === 'bs-save') { botSaveSettings(); return; }
  if (el.dataset.botrun) { api('/bot/run', { method: 'POST', body: { job: el.dataset.botrun } }).then(() => { toast(el.dataset.botrun === 'scan' ? 'Scanning the watchlist… refresh in a minute' : 'Checking exits…'); setTimeout(() => loadBot(true), 20000); }).catch(err => toast(err.message)); return; }
  if (el.dataset.botorder) {
    const id = el.dataset.botorder, form = { qty: +$('#bo-qty').value, limit: +$('#bo-lim').value }, override = !!el.dataset.override;
    const r = [S.botRes, S.anaRes].find(x => x && x.id === id) || {};
    if (override && !confirm("Place this order even though Claude's chart check didn't approve it?")) return;
    el.disabled = true;
    if (aiOn() && !override && !(r.aiCheck && aiFresh(r.aiCheck) && aiPasses(r.aiCheck))) { aiRun(id, form); return; }
    placeOrder(id, override, form); return;
  }
  if (el.dataset.aicheck) { aiRun(el.dataset.aicheck, null); return; }
  if (el.dataset.aireview) { rvRun(el.dataset.aireview); return; }
  if (el.id === 'refresh-btn') { refreshNow(); return; }
  if (el.id === 'sh-run') { runShadow(); return; }
  if (el.dataset.botclose) { if (!confirm('Close this paper position at market?')) return; api(`/bot/${el.dataset.botclose}/close`, { method: 'POST' }).then(async () => { await loadBot(true); toast('Close order sent'); }).catch(err => toast(err.message)); return; }
  if (el.dataset.botdismiss) { api(`/bot/${el.dataset.botdismiss}/dismiss`, { method: 'POST' }).then(async () => { S.botRes = null; await loadBot(true); }).catch(err => toast(err.message)); return; }
  if (el.dataset.botshow) { const id = el.dataset.botshow; S.botRes = [...((S.bot || {}).items || []), ...((S.botEvals || {}).items || [])].find(i => i.id === id) || null; render(); window.scrollTo(0, 0); return; }
  if (el.id === 'tc-run') { tcRead(); return; }
  if (el.id === 'mkt-refresh') { S.mkt = null; render(); loadMarketGamma(true); return; }
  if (el.dataset.gexlink) { S.gexSym = el.dataset.gexlink; S.gexExp = ''; closeTrade(); location.hash = '#gex'; setTimeout(loadGex, 50); return; }
  if (el.dataset.gex) { $('#gx-sym').value = el.dataset.gex; S.gexExp = ''; loadGex(); return; }
  if (el.id === 'gx-run') { loadGex(); return; }
  if (el.dataset.coach) { runCoach(el.dataset.coach, el.dataset.trade); if (el.dataset.trade) { el.disabled = true; el.textContent = 'Reviewing…'; } return; }
  if (el.dataset.sort) { const k = el.dataset.sort, c = S.sort || { key: 'date', dir: 'desc' };
    S.sort = c.key === k ? { key: k, dir: c.dir === 'asc' ? 'desc' : 'asc' } : { key: k, dir: SORT_FIRST_DESC.has(k) ? 'desc' : 'asc' };
    if ($('#trade-table')) $('#trade-table').innerHTML = tradeTable(filtered()); else render(); return; }
  if (el.dataset.cal !== undefined) { const d = +el.dataset.cal; if (!d) S.calMonth = nyToday().slice(0, 7); else { let [y, m] = S.calMonth.split('-').map(Number); m += d; if (m === 0) { m = 12; y--; } if (m === 13) { m = 1; y++; } S.calMonth = `${y}-${String(m).padStart(2, '0')}`; } render(); return; }
  if (el.dataset.bars) { S.barMode = el.dataset.bars; render(); return; }
  if (el.dataset.day) { S.jDay = el.dataset.day; location.hash = '#journal'; return; }
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
    case 'ctx-run': runContext(false); return;
    case 'q-run': if (cur) tradeQuality(cur.id); return;
    case 'rebuild': el.disabled = true; el.textContent = 'Rebuilding…'; api('/maintenance/rebuild', { method: 'POST' })
      .then(async r => { await reload(); toast(`Rebuilt ${r.trades} trades. Removed ${r.duplicateFillsRemoved} duplicate fill${r.duplicateFillsRemoved === 1 ? '' : 's'}.`); })
      .catch(err => { el.disabled = false; el.textContent = 'Rebuild trades and remove duplicate fills'; toast(err.message); }); return;
    case 'q-all': runQualityAll(); return;
    case 'ctx-all': runContext(true); return;
    case 'signout': Auth.logout(); return;
  }
});
function tcRead() { S.tc = { type: $('#tc-type').value, strike: +$('#tc-strike').value || null, exp: $('#tc-exp').value, prem: +$('#tc-prem').value || null }; const o = $('#tc-out'); if (o && S.gex) o.innerHTML = tcResult(S.gex); }
document.addEventListener('input', e => {
  if (['tc-strike', 'tc-prem'].includes(e.target.id)) { tcRead(); return; }
  if (e.target.id === 'bot-sym' && $('#bot-earn') && S.bot) { const d = (((S.bot.settings || {}).earnings || {})[e.target.value.trim().toUpperCase()] || '').split(' '); $('#bot-earn').value = d[0] || ''; $('#bot-earn-t').value = d[1] || 'AMC'; }
  if (e.target.id === 'f-q') { tf.q = e.target.value; $('#trade-table').innerHTML = tradeTable(filtered()); }
  if (e.target.id === 'rp') { stopReplay(); replay.k = +e.target.value; drawChart(); }
});
let evQTimer = null;
document.addEventListener('input', e => {
  if (e.target.id !== 'ev-q') return;
  clearTimeout(evQTimer);
  const v = e.target.value.trim().toUpperCase();
  evQTimer = setTimeout(async () => {
    S.evQ = v; await loadEvals(1);
    const i = $('#ev-q'); if (i) { i.focus(); i.setSelectionRange(i.value.length, i.value.length); }
  }, 350);
});
document.addEventListener('submit', e => {
  if (e.target.id !== 'dt-acct') return;
  e.preventDefault();
  const key = (($('#dt-key') || {}).value || '').trim(), secret = (($('#dt-secret') || {}).value || '').trim();
  api('/daytrade/account', { method: 'PUT', body: { key, secret } }).then(r => { S.dt = r; toast('Day-trading account connected'); render(); }).catch(err => toast(err.message));
});
document.addEventListener('submit', e => {
  if (e.target.id === 'sd-acct') { e.preventDefault();
    const key = (($('#sd-key') || {}).value || '').trim(), secret = (($('#sd-secret') || {}).value || '').trim();
    api('/stockday/account', { method: 'PUT', body: { key, secret } }).then(r => { S.sd = r; toast('Stock day-trading account connected'); render(); }).catch(err => toast(err.message)); return; }
  if (e.target.id === 'dt-why') { e.preventDefault();
    const v = (($('#dt-whyat') || {}).value || '').trim().replace(' ', 'T'), sym = (S.dtFc && S.dtFc.d && S.dtFc.d.symbol) || 'QQQ';
    if (v) dtWhy(sym, v); return; }
  if (e.target.id === 'sd-sym') { e.preventDefault();
    const sym = (($('#sd-symin') || {}).value || '').trim().toUpperCase(), date = (($('#sd-datein') || {}).value || '').trim();
    if (sym) { S.sdSel = { sym, date }; render(); } }
});
document.addEventListener('change', e => {
  if (e.target.hasAttribute && e.target.hasAttribute('data-evoon')) {
    api('/research/evolve', { method: 'PUT', body: { on: e.target.checked, days: +(($('#evo-days') || {}).value || 250), tickers: (($('#evo-tk') || {}).value || '') } }).then(r => { S.rs.day = { ...(S.rs.day || {}), evolve: r.evolve }; toast(e.target.checked ? 'Evolution ON: runs back to back (at most 80 runs a day)' : 'Evolution OFF'); render(); }).catch(err => toast(err.message)); return; }
  if (e.target.hasAttribute && e.target.hasAttribute('data-bkauto')) {
    api('/research/basket', { method: 'PUT', body: { auto: e.target.checked } }).then(r => { S.rs.day = { ...(S.rs.day || {}), basket: r.basket }; S.dt = undefined; toast(e.target.checked ? 'Auto-switch ON: the best healthy strategy goes live after each daily check' : 'Auto-switch OFF'); render(); }).catch(err => toast(err.message)); return; }
  const sk = e.target.dataset && e.target.dataset.sdlive;
  if (sk) { sdPut({ [sk]: e.target.checked }, sk === 'enabled' ? (e.target.checked ? 'Stock day trading runs every minute' : 'Stock day trading stopped') : (e.target.checked ? 'Orders ON (paper stock account)' : 'Orders OFF')); return; }
  const k = e.target.dataset && e.target.dataset.dtlive; if (!k) return;
  dtPut({ [k]: e.target.checked }, k === 'enabled' ? (e.target.checked ? 'Day trading runs every minute' : 'Day trading stopped') : (e.target.checked ? 'Orders ON (paper day account)' : 'Orders OFF'));
});
document.addEventListener('input', e => {
  if (e.target.matches && e.target.matches('[data-sdnum],[data-sdtxt]')) { S.sdDirty = true; const b = document.querySelector('[data-sdapply]'); if (b) { b.classList.add('coachbtn'); b.textContent = 'Apply changes'; } }
  if (e.target.matches && e.target.matches('[data-dtnum],[data-dttime],[data-dttk],[data-dtsel]')) { S.dtDirty = true; const b = document.querySelector('[data-dtapply]'); if (b) { b.classList.add('coachbtn'); b.textContent = 'Apply changes'; } }
});
document.addEventListener('submit', e => {
  if (e.target.id !== 'ww-addf') return;
  e.preventDefault();
  const t = (($('#ww-add') || {}).value || '').trim().toUpperCase(); if (!t) return;
  const ln = (($('#ww-addlist') || {}).value) || 'Main'; S.wlAddTo = ln;
  api('/bot/wallwatch', { method: 'PUT', body: { add: t, list: ln } }).then(r => { S.ww = r; toast(`${t} added`); location.hash = '#botchart?t=' + encodeURIComponent(t); render(); }).catch(err => toast(err.message));
});
document.addEventListener('change', e => {
  const id = e.target.id;
  if (['f-setup', 'f-tag', 'f-res', 'f-acct', 'f-asset', 'f-dir'].includes(id)) { tf[id.slice(2)] = e.target.value; $('#trade-table').innerHTML = tradeTable(filtered()); }
  if (id === 'eo-mult') { S.eoMult = +e.target.value; if (S.eo) runExitOpt(); return; }
  if (id === 'ex-key') { S.exPick = e.target.value || null; render(); return; }
  if (id === 'dt-stgap') { S.dtStGap = e.target.value; render(); return; }
  if (id === 'ww-dpn') { const v = Math.max(0, Math.min(10, Math.round(+e.target.value || 0))); S.dpN = v; try { localStorage.setItem('dpN', String(v)); } catch (x) {} render(); return; }
  if (id === 'ww-dpmin') { const v = parseFloat(e.target.value); if (v > 0) { S.wwDpPct = v; Object.keys(S.fcDp).filter(x => x.startsWith('ww-') || x.startsWith('dt-')).forEach(x => delete S.fcDp[x]); render(); } return; }
  if (id === 'ww-on') { api('/bot/wallwatch', { method: 'PUT', body: { enabled: e.target.checked } }).then(r => { S.ww = r; render(); }).catch(err => toast(err.message)); return; }
  if (id === 'ex-days') { S.exDays = +e.target.value; S.ex = null; runExplore(false); return; }
  if (id === 'sh-days') { S.shadowDays = +e.target.value; S.shSel = null; loadShadow(); return; }
  if (id === 'ev-claude') { S.evClaude = e.target.checked; loadEvals(1); return; }
  if (id === 'bs-strat') { $('#bs-bull').hidden = e.target.value !== 'bull_call'; $('#bs-diag').hidden = e.target.value !== 'diagonal'; return; }
  if (id === 'fl-auto') { S.flowAuto = e.target.checked; if (S.flowAuto && S.flow) loadFlow(); return; }
  if (id === 'fl-period') { S.flowF = { ...(S.flowF || {}), period: e.target.value }; if (e.target.value !== 'custom') { loadFlow(); } else render(); return; }
  if (id === 'tc-type' || id === 'tc-exp') { tcRead(); return; }
  if (route() === 'settings' && /^(s-|pp-|lc-)/.test(id)) { clearTimeout(S.autoSave); S.autoSave = setTimeout(saveSettings, 400); }
  if (route() === 'settings' && /^(bs-|bf-)/.test(id) && id !== 'bs-auto') { clearTimeout(S.botAutoSave); S.botAutoSave = setTimeout(() => botSaveSettings(true), 500); }
  if (id === 'jday') { S.jDay = e.target.value; render(); }
  if (id === 'gx-exp' || id === 'gx-days' || id === 'gx-strikes') { if (id === 'gx-days') S.gexExp = ''; loadGex(); }
  if (id === 'g-acct') { S.acct = e.target.value; render(); }
  if (id === 'file' && e.target.files[0]) upload(e.target.files[0]);
});
document.addEventListener('submit', e => { if (e.target.id === 'ask') { e.preventDefault(); ask($('#askq').value); } });
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target.id === 'bot-sym') { e.preventDefault(); botEvaluate(); return; }
  if (e.key === 'Enter' && e.target.id === 'gx-sym') { e.preventDefault(); S.gexExp = ''; loadGex(); return; }
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
  $('#login').hidden = true; $('#app').hidden = false; $('#who-email').textContent = Auth.email();
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

/* ---------- flow chart zoom: drag across a period, or scroll ---------- */
(() => {
  let drag = null, suppress = false;
  document.addEventListener('click', ev => { if (suppress && ev.target.closest && ev.target.closest('svg[data-fcz]')) { ev.stopPropagation(); ev.preventDefault(); } }, true);
  const info = svg => { const r = svg.getBoundingClientRect(), k = r.width ? (+svg.dataset.vw) / r.width : NaN;
    return { r, k, L: +svg.dataset.l, pw: +svg.dataset.pw, a: +svg.dataset.a, n: +svg.dataset.n, s: +svg.dataset.slots || +svg.dataset.n, all: +svg.dataset.all, id: svg.dataset.fcz }; };
  const vx = (svg, ev) => { const f = info(svg); return (ev.clientX - f.r.left) * f.k; };
  const barAt = (f, x) => f.a + Math.max(0, Math.min(f.n - 1, Math.floor((x - f.L) / (f.pw / f.s))));
  document.addEventListener('pointerdown', ev => {
    const svg = ev.target.closest && ev.target.closest('svg[data-fcz]');
    if (!svg || ev.button > 0) return;
    drag = { svg, id: svg.dataset.fcz, x0: vx(svg, ev), moved: false };
    S.fczDrag = true;
  });
  document.addEventListener('pointermove', ev => {
    const hsvg = ev.target.closest && ev.target.closest('svg[data-fcz]');
    document.querySelectorAll('svg[data-fcz] .fc-xh').forEach(l => { if (!hsvg || !hsvg.contains(l)) l.style.display = 'none'; });
    if (hsvg) {                                  // OHLC of the candle under the pointer
      const f = info(hsvg), d = S.fc[fcKey(f.id)], bars = d ? fcClean(d).bars : [];
      const x = vx(hsvg, ev), i = barAt(f, x), b = bars[i], txt = hsvg.querySelector('.fc-ohlc'), xh = hsvg.querySelector('.fc-xh');
      if (b && txt && isFinite(x)) {
        const pf = v => v.toFixed(v < 20 ? 3 : 2), ch = (b.c / b.o - 1) * 100;
        txt.textContent = `${b.t.replace('T', ' ').slice(5)}  O ${pf(b.o)}  H ${pf(b.h)}  L ${pf(b.l)}  C ${pf(b.c)}  ${ch >= 0 ? '+' : ''}${ch.toFixed(2)}%`;
        txt.setAttribute('fill', b.c >= b.o ? 'var(--gain)' : 'var(--loss)');
        const cx = f.L + (i - f.a + 0.5) * f.pw / f.s;
        if (xh) { xh.setAttribute('x1', cx); xh.setAttribute('x2', cx); xh.style.display = ''; }
      }
    }
    if (!drag) return;
    const x = vx(drag.svg, ev), sel = drag.svg.querySelector('.fcz-sel');
    if (Math.abs(x - drag.x0) > 6) drag.moved = true;
    if (sel && drag.moved) { sel.style.display = ''; sel.setAttribute('x', Math.min(x, drag.x0)); sel.setAttribute('width', Math.abs(x - drag.x0)); }
  });
  const endDrag = () => { S.fczDrag = false; if (S.renderPending) { S.renderPending = false; render(); } };
  document.addEventListener('pointercancel', () => { if (drag) { drag = null; endDrag(); } });
  document.addEventListener('pointerup', ev => {
    if (!drag) return;
    const d0 = drag; drag = null;
    if (!document.contains(d0.svg)) {              // redrawn meanwhile: use the chart on screen now
      const now = document.querySelector(`svg[data-fcz="${CSS.escape(d0.id)}"]`);
      if (now) d0.svg = now;
    }
    if (!d0.moved) {                               // a click on a day-trading chart: why a trade was / wasn't taken at that minute
      if (String(d0.id).startsWith('dt-') && S.dtFc && S.dtFc.d && S.dtFc.d.bars && !(ev.target.closest && ev.target.closest('title,circle,rect[data-fce]'))) {
        const f = info(d0.svg), i = barAt(f, vx(d0.svg, ev)), b = fcClean(S.dtFc.d).bars[i];
        if (b) { const sym = String(d0.id).replace(/^dt-/, '').replace(/-\d+$/, ''); setTimeout(() => dtWhy(sym, b.t), 0); }
      }
      endDrag(); return; }
    const pend = S.renderPending; S.fczDrag = false; S.renderPending = false;     // the zoom below redraws
    suppress = true; setTimeout(() => { suppress = false; }, 50);      // a drag that ends on a dot doesn't select it
    const f = info(d0.svg), x = vx(d0.svg, ev);
    const a = barAt(f, Math.min(x, d0.x0)), b = barAt(f, Math.max(x, d0.x0));
    if (b - a >= 2) fcSetZoom(f.id, a, b, f.all);
    else { const sel = d0.svg.querySelector('.fcz-sel'); if (sel) sel.style.display = 'none'; if (pend) render(); }
  });
  document.addEventListener('wheel', ev => {
    const svg = ev.target.closest && ev.target.closest('svg[data-fcz]');
    if (!svg || Math.abs(ev.deltaY) < Math.abs(ev.deltaX)) return;
    ev.preventDefault();
    const f = info(svg), c = barAt(f, vx(svg, ev)), a = f.a, b = f.a + f.n - 1, z = ev.deltaY < 0 ? 0.8 : 1.25;
    fcSetZoom(f.id, c - (c - a) * z, c - (c - a) * z + (b - a) * z, f.all);
  }, { passive: false });
})();

document.addEventListener('toggle', e => { if (e.target && e.target.matches && e.target.matches('[data-wwmore]')) { S.wwMoreOpen = e.target.open; if (e.target.open && !S.itd && !S.itdBusy) loadIntraday(); } }, true);
