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
const DEC_CLS = { BUY: 'ok', WAIT: 'mid', SKIP: 'bad' };
const STRAT_LBL = { long_call: 'Long call', bull_call: 'Bull call spread', diagonal: 'Diagonal' };
const isSpread = p => !!(p && p.legs && (p.strategy === 'bull_call' || p.strategy === 'diagonal'));
const legTxt = p => !p || !p.exp ? '' : p.strategy === 'bull_call' && p.shortStrike ? `${fmtExp(p.exp)} ${p.strike}/${p.shortStrike}c`
  : p.strategy === 'diagonal' ? `${fmtExp(p.exp)} ${p.strike}c / ${p.shortStrike ? `${fmtExp(p.shortExp)} ${p.shortStrike}c` : 'no short'}` : `${fmtExp(p.exp)} ${p.strike}c`;
/* ---------- flow on the chart: when each options alert came in, and how big, against the price ---------- */
S.fc = S.fc || {}; S.fcBusy = S.fcBusy || {};
async function loadFlowChart(id) {
  S.fcBusy[id] = true;
  try { S.fc[id] = await api(`/bot/${id}/flowchart`); } catch (e) { S.fc[id] = { error: e.message }; }
  S.fcBusy[id] = false;
  if (isBotRoute()) render();
}
function fcSvg(d, id) {
  const bars = d.bars || [], n = bars.length;
  if (n < 2) return '<p class="muted" style="margin:0">No intraday prices for these days.</p>';
  const narrow = window.innerWidth < 700, fs = narrow ? 17 : 12;
  const W = narrow ? 560 : 960, H = narrow ? 470 : 340, L = 8, Rr = narrow ? 72 : 58, T = 16, B = H - (narrow ? 44 : 40);
  const lo0 = Math.min(...bars.map(b => b.l)), hi0 = Math.max(...bars.map(b => b.h)), pad = (hi0 - lo0) * 0.12 || 1;
  const lo = lo0 - pad, hi = hi0 + pad;
  const X = i => L + (i + 0.5) * (W - L - Rr) / n, Y = v => T + (hi - v) / (hi - lo) * (B - T), bw = Math.max(1, (W - L - Rr) / n * 0.6);
  const idx = {}; bars.forEach((b, i) => { idx[b.t] = i; });
  const bucket = t => { const d0 = t.slice(0, 10), hm = t.slice(11, 16); let m = (+hm.slice(0, 2)) * 60 + (+hm.slice(3, 5)); m = Math.max(570, Math.min(m, 945)); m = 570 + Math.floor((m - 570) / 15) * 15;
    return `${d0}T${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`; };
  const at = t => { const k = bucket(t); if (idx[k] != null) return idx[k]; let best = null; bars.forEach((b, i) => { if (b.t <= k) best = i; }); return best; };
  let g = '';
  for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4, y = Y(v); g += `<line x1="${L}" x2="${W - Rr}" y1="${y}" y2="${y}" stroke="var(--line)" stroke-width="1"/><text x="${W - Rr + 6}" y="${y + 4}" font-size="${fs}" fill="var(--muted)">${v.toFixed(v < 20 ? 2 : 1)}</text>`; }
  bars.forEach((b, i) => { if (i === 0 || b.t.slice(0, 10) !== bars[i - 1].t.slice(0, 10)) { const x = X(i) - (W - L - Rr) / n / 2;
    g += `<line x1="${x}" x2="${x}" y1="${T}" y2="${B}" stroke="var(--line)" stroke-dasharray="2 3"/><text x="${x + 3}" y="${B + 16}" font-size="${fs}" fill="var(--muted)">${b.t.slice(5, 10)}</text>`; } });
  bars.forEach((b, i) => { const up = b.c >= b.o, c = up ? 'var(--gain)' : 'var(--loss)', x = X(i);
    g += `<line x1="${x}" x2="${x}" y1="${Y(b.h)}" y2="${Y(b.l)}" stroke="${c}" stroke-width="1"/><rect x="${x - bw / 2}" y="${Y(Math.max(b.o, b.c))}" width="${bw}" height="${Math.max(1, Math.abs(Y(b.o) - Y(b.c)))}" fill="${c}"/>`; });
  if (d.evaluatedAt) { const ei = at(d.evaluatedAt); if (ei != null) { const x = X(ei);
    g += `<line x1="${x}" x2="${x}" y1="${T}" y2="${B}" stroke="var(--coach,#5b5bd6)" stroke-width="2" stroke-dasharray="6 4"/><text x="${Math.min(x + 4, W - Rr - 70)}" y="${T + 12}" font-size="${fs}" font-weight="600" fill="var(--coach,#5b5bd6)">evaluated</text>`; } }
  const stack = {};
  (d.alerts || []).forEach((a, k) => { const i = at(a.t); if (i == null) return; const b = bars[i], call = a.type === 'call';
    const key = `${i}${a.type}`; stack[key] = (stack[key] || 0) + 1;
    const r = Math.min(13, 2.5 + Math.sqrt(a.premium / 100000) * 3.2);
    const y = call ? Y(b.l) + 6 + r + (stack[key] - 1) * 5 : Y(b.h) - 6 - r - (stack[key] - 1) * 5;
    const sel = S.fcSel && S.fcSel.id === id && S.fcSel.k === k;
    g += `<circle data-fci="${id}|${k}" cx="${X(i)}" cy="${y}" r="${sel ? r + 3 : r}" fill="${call ? 'var(--gain)' : 'var(--loss)'}" fill-opacity="${a.big ? .75 : .38}" stroke="${a.big || sel ? 'var(--ink)' : 'none'}" stroke-width="${sel ? 2 : 1}" style="cursor:pointer"><title>${esc(a.t.replace('T', ' ').slice(5))} · ${a.type.toUpperCase()} ${money(a.premium, false)} · ${esc(a.contract || '')}</title></circle>`; });
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block" role="img" aria-label="${esc(d.symbol)} 15-minute price with options alerts">${g}</svg>`;
}
function flowChartPanel(r) {
  if (!r || !r.id || !r.createdAt) return '';
  const d = S.fc[r.id];
  if (!d && !S.fcBusy[r.id]) setTimeout(() => loadFlowChart(r.id), 0);
  const head = `<h3 style="font-size:.95rem;margin:14px 0 6px">Flow on the chart <span class="muted" style="font-weight:400;font-size:.82rem">15-minute price · every option bought at the ask, from ${money((d && d.minPrint) || 10000, false)}</span></h3>`;
  if (!d) return head + '<p class="loading" style="margin:0">Loading the chart and the alerts…</p>';
  if (d.error) return head + `<p class="muted" style="margin:0">${esc(d.error)}</p>`;
  const al = d.alerts || [], calls = al.filter(a => a.type === 'call'), puts = al.filter(a => a.type === 'put');
  const sum = xs => xs.reduce((s2, a) => s2 + a.premium, 0);
  const sel = S.fcSel && S.fcSel.id === r.id ? al[S.fcSel.k] : null;
  const top = [...al].sort((a, b) => b.premium - a.premium).slice(0, 6);
  return head + `<div style="border:1px solid var(--line);border-radius:10px;padding:6px;background:var(--surface)">${fcSvg(d, r.id)}</div>
    <p class="st-legend" style="margin:6px 0 4px"><span><i class="st-dot g"></i>Calls bought (${calls.length}, ${money(sum(calls), false)})</span><span><i class="st-dot" style="display:inline-block;background:var(--loss)"></i>Puts bought (${puts.length}, ${money(sum(puts), false)})</span><span>bigger dot = bigger premium · outlined = over ${money(d.bigPrint, false)}</span><span style="color:var(--coach,#5b5bd6)">┆ evaluated</span></p>
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
const aiCell = i => { const a = i.aiCheck && i.aiCheck.verdict ? i.aiCheck : null;
  const rv = i.aiReview, rvb = rv ? ` <span class="verdict ${rv.action === 'close' ? 'bad' : 'ok'}" title="${esc(`End-of-day review ${(rv.at || '').slice(5, 16).replace('T', ' ')}: ${rv.summary}`)}">${rv.action === 'close' ? 'EOD CLOSE' : 'EOD HOLD'}</span>` : '';
  if (a) return `<td title="${esc(a.summary || '')}"><span class="verdict ${AI_CLS[a.verdict] || 'mid'}">${esc(a.verdict.toUpperCase())}</span>${i.aiFrom ? ' <span class="muted" style="font-size:.78rem">carried</span>' : ''}${rvb}</td>`;
  if (rv) return `<td>${rvb}</td>`;
  if (i.aiReviewError) return `<td title="${esc(i.aiReviewError)}"><span class="loss">EOD error</span></td>`;
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
    <p class="muted" style="font-size:.84rem;margin:8px 0 0">${evToday} evaluation${evToday === 1 ? '' : 's'} today · <a href="#botevals">Evaluations</a> · <a href="#botskipped">Skipped & what-if</a></p>
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
    ${(d.events || []).length ? `<ul class="shl" style="margin-top:10px">${d.events.map(burstLine).join('')}</ul>` : `<p class="muted" style="margin:10px 0 0">No burst in the last ${d.days} days yet.</p>`}</section>`;
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
    ${(st.top || []).length ? `<h3 style="font-size:.95rem;margin:14px 0 6px">Bursts found (your thresholds), biggest first</h3><ul class="shl">${st.top.map(burstLine).join('')}</ul>` : ''}`}
    ${st && st.updatedAt ? `<p class="muted" style="font-size:.8rem;margin:10px 0 0">${R === 'done' ? 'Finished' : 'Updated'} ${esc(st.updatedAt.replace('T', ' ').slice(5, 16))} ET${st.errors ? ` · ${st.errors} ticker${st.errors > 1 ? 's' : ''} skipped (data errors)` : ''}</p>` : ''}
  </section>`;
  return head0 + live + study;
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
    if (v === 'botskipped') { await loadShadow(); }
    if (v === 'botbursts') { await loadBursts(); }
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
  bot: [['bot', 'Overview'], ['botevals', 'Evaluations'], ['botskipped', 'Skipped & what-if'], ['botbursts', 'Bursts']],
};
const isBotRoute = () => ['bot', 'botevals', 'botskipped', 'botbursts'].includes(route());
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
  botskipped: [vBotSkipped, () => { if (!S.shadowSum) loadShadow(); }],
  botbursts: [vBursts, () => { if (!S.bursts && !S.burstsLoading) loadBursts(); }], dashboard: [vDashboard, async () => { if (!S.bot) loadBot(); if (S.notes === undefined) { await loadNotes(); if (route() === 'dashboard') render(); } }], trades: [vTrades], analytics: [vAnalytics], coach: [vCoach, afterCoach], journal: [vJournal, afterJournal], import: [vImport, afterImport], settings: [vSettings, afterSettings] };
const route = () => { const r = location.hash.slice(1).split('?')[0] || 'dashboard'; return VIEWS[r] ? r : 'dashboard'; };
const hashParam = k => new URLSearchParams(location.hash.split('?')[1] || '').get(k);
const evTicker = () => route() === 'botevals' ? (hashParam('t') || '').toUpperCase() : '';
function render() {
  const v = route();
  const grp = groupOf(v);
  document.querySelectorAll('.nav a[data-r]').forEach(a => {
    const on = a.dataset.r === v || (grp === 'trades' && a.dataset.r === 'trades') || (grp === 'bot' && a.dataset.r === 'bot');
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  document.querySelectorAll('.nav .nav-sub a').forEach(a => { if (a.dataset.r === v) a.setAttribute('aria-current', 'page'); });
  document.querySelectorAll('.nav .nav-sub').forEach(x => { x.hidden = grp !== (x.dataset.group || 'trades'); });
  document.querySelectorAll('[data-acct]').forEach(b => b.classList.toggle('on', grp === 'settings'));
  try { $('#view').innerHTML = subnav(v) + VIEWS[v][0](); } catch (e) { console.error(e); $('#view').innerHTML = `<div class="errbox">This page failed to load: ${esc(e.message)}</div>`; }
  if (VIEWS[v][1]) VIEWS[v][1]();
}
async function reload() { const [me, tr] = await Promise.all([api('/me'), api('/trades')]); S.me = me; S.trades = tr.trades.sort(chron); if (!cur) render(); }
window.addEventListener('hashchange', () => { render(); window.scrollTo(0, 0); });
document.addEventListener('click', e => {
  const el = e.target.closest('[data-burstsave],[data-burstrun],[data-fci],[data-shwhy],[data-shclaude],[data-shopen],[data-botsave],[data-evpage],[data-uwtest],[data-reeval],[data-evticker],[data-evopen],[data-resclose],[data-bottoggle],#ana-run,#nt-save,#nt-test,[data-savesettings],[data-rescan],#fl-load,[data-undo],#rm-csv,#uw-save,#uw-del,[data-botchase],#bot-eval,#bs-save,[data-botrun],[data-botorder],[data-aicheck],[data-aireview],#refresh-btn,#sh-run,[data-botclose],[data-botdismiss],[data-botshow],#tc-run,#mkt-refresh,[data-gexlink],[data-gex],#gx-run,[data-coach],[data-sort],[data-cal],[data-bars],[data-day],[data-range],[data-open],[data-tf],[data-tag],[data-emo],[data-wi],[data-bd],[data-ask],[data-score],#dr-close,#scrim,#rp-play,#save-plan,#rv-run,#j-save,#rep-run,#s-save,#al-save,#al-sync,#al-del,#sch-connect,#sch-reconnect,#sch-sync,#sch-del,#al-show,#ctx-run,#ctx-all,#q-run,#q-all,#rebuild,#signout');
  if (!el) return;
  if (el.dataset.savesettings) { saveSettings(); return; }
  if (el.dataset.rescan) { reEvaluate(el.dataset.rescan); return; }
  if (el.dataset.botsave) { botSaveSettings(); return; }
  if (el.dataset.evpage) { loadEvals(+el.dataset.evpage); return; }
  if (el.dataset.reeval) { reEvaluate(el.dataset.reeval); return; }
  if (el.dataset.shwhy !== undefined) { openShadow('why', el.dataset.shwhy); return; }
  if (el.dataset.shclaude !== undefined) { openShadow('claude', el.dataset.shclaude); return; }
  if (el.dataset.shopen) { openRecord(el.dataset.shopen); return; }
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
document.addEventListener('change', e => {
  const id = e.target.id;
  if (['f-setup', 'f-tag', 'f-res', 'f-acct', 'f-asset', 'f-dir'].includes(id)) { tf[id.slice(2)] = e.target.value; $('#trade-table').innerHTML = tradeTable(filtered()); }
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
