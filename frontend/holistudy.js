// Swing → Holidays & half days: what SPY / QQQ / IWM / DIA does around US market holidays and 13:00 early closes.
S.ho = S.ho || { sym: 'SPY', n: 5, from: 1993, exit: 'pre', cost: 1, r: null };

async function hoLoad() {
  const el = $('#sw-body'); if (!el) return;
  const H = S.ho;
  el.innerHTML = hoShell(H.r, true);
  try { H.r = await api(`/holidays?sym=${H.sym}&n=${H.n}&from=${H.from}&exit=${H.exit}&cost=${H.cost}`); }
  catch (e) { el.innerHTML = hoShell(null) + `<div class="panel"><div class="bd loss">${esc(e.message)}</div></div>`; return; }
  if (S.sw.kind === 'hol' && $('#sw-body')) $('#sw-body').innerHTML = hoShell(H.r);
}

function hoShell(r, loading) {
  const H = S.ho;
  const sel = (k, opts) => `<select data-hov="${k}">${opts.map(([v, t]) => `<option value="${v}"${String(H[k]) === String(v) ? ' selected' : ''}>${t}</option>`).join('')}</select>`;
  const form = `<section class="panel"><div class="hd"><h2>Holidays &amp; half days</h2><div class="grow"></div>${loading ? '<span class="dim">Loading…</span>' : ''}</div>
    <div class="bd" style="display:flex;gap:12px;flex-wrap:wrap;align-items:end">
      <label class="f">Symbol${sel('sym', [['SPY', 'SPY'], ['QQQ', 'QQQ'], ['IWM', 'IWM'], ['DIA', 'DIA']])}</label>
      <label class="f">Sessions before${sel('n', [[5, '5'], [10, '10']])}</label>
      <label class="f">From${sel('from', [[1993, '1993 (all)'], [2000, '2000'], [2010, '2010'], [2016, '2016'], [2020, '2020']])}</label>
      <label class="f">Backtest: exit at${sel('exit', [['pre', 'The close before the holiday'], ['post1', 'The close of the first session after']])}</label>
      <label class="f">Cost (bp a side)${sel('cost', [[0, '0'], [1, '1'], [2, '2'], [5, '5']])}</label>
      <button class="btn primary" data-ho="run">Run</button></div>
    <p class="note" style="padding:0 16px 12px">Holidays are found in the data (weekdays with no session, so special closures such as 9/11 count too) and named by the NYSE rules. Half days (13:00 close): the day after Thanksgiving, and Jul 3 / Dec 24 when they fall Monday–Thursday.
    Jewish holidays (the market is open): Purim, Passover, Shavuot, Rosh Hashanah, Yom Kippur, Sukkot and Hanukkah, from the Hebrew calendar; the event is the first session on or after the holiday's first day (it begins the evening before).
    "Before" = the ${H.n} sessions up to the last close before the holiday (for a half day: up to the session before it). "Day before" = the last session before the holiday (for a half day or a Jewish holiday: that session itself). "+1 … +10" = from that close to the close 1 … 10 sessions later. "Every day" is the same measure on all days of the data, the baseline: a holiday effect is the difference from it. t ≥ 2 is roughly significant; most groups here have 10–35 events, so read them as tendencies.</p></section>`;
  if (!r) return form;
  const pc = x => x == null ? '—' : ((x >= 0 ? '+' : '') + (x * 100).toFixed(2) + '%');
  const up = s => s && s.n ? Math.round(s.up * 100) + '%' : '—';
  const cell = (s, t) => !s || !s.n ? '<td class="r dim">—</td>' : `<td class="r ${cls(s.avg)}" title="median ${pc(s.med)} · up ${up(s)} of ${s.n}${s.t != null ? ' · t ' + s.t : ''}">${pc(s.avg)}<span class="dim" style="font-size:11px"> ${up(s)}${t && s.t != null && Math.abs(s.t) >= 2 ? ' ●' : ''}</span></td>`;
  const head = (first, extra = '') => `<thead><tr><th>${first}</th><th class="r">N</th>${extra}<th class="r">Before (${r.n})</th><th class="r">Day before</th><th class="r">Gap at reopen</th>${[1, 3, 5, 10].map(k => `<th class="r">+${k}</th>`).join('')}</tr></thead>`;
  const row = (lab, g) => `<tr><td>${lab}</td><td class="r">${g.n}</td>${cell(g.pre)}${cell(g.day0, 1)}${cell(g.gap, 1)}${[1, 3, 5, 10].map(k => cell(g['p' + k], 1)).join('')}</tr>`;
  const base = (lab, b, day) => `<tr class="dim"><td>${lab}</td><td class="r">${(b.p1 && b.p1.n || 0).toLocaleString('en-US')}</td><td class="r">—</td>${day ? cell(day) : '<td class="r">—</td>'}<td class="r">—</td>${[1, 3, 5, 10].map(k => cell(b['p' + k])).join('')}</tr>`;
  const sp = r.split;
  const tbl = (title, note, body, first = '') => `<section class="panel" style="margin-top:12px"><div class="hd"><h2>${title}</h2><span class="dim" style="font-size:13px">${note}</span></div><div class="tbl-wrap"><table class="tbl">${head(first)}<tbody>${body}</tbody></table></div></section>`;
  const ev = r.events.slice(0, 120).map(e => `<tr><td class="mono">${esc(e.date)}</td><td>${esc(e.name)}${e.kind === 'early' ? ' <span class="dim">(half day)</span>' : e.kind === 'jewish' ? ' <span class="dim">(Jewish, market open)</span>' : ''}</td><td class="r ${cls(e.pre)}">${pc(e.pre)}</td><td class="r ${cls(e.day0)}">${pc(e.day0)}</td><td class="r ${cls(e.gap)}">${pc(e.gap)}</td>${[1, 3, 5, 10].map(k => `<td class="r ${cls(e.post[k])}">${pc(e.post[k])}</td>`).join('')}</tr>`).join('');
  return form + `<div class="dim" style="margin:10px 2px;font-size:13px">${esc(r.sym)} daily, ${esc(r.from)} → ${esc(r.to)} (${r.days.toLocaleString('en-US')} sessions) · ${esc(r.src)} · each cell: average return, then % of events up · ● = t ≥ 2 · hover a cell for the median and t</div>` +
    hoBtHtml(r.bt, pc) +
    tbl('All holidays vs every day', `${r.holidays.n} holidays, ${r.early.n} half days`, row('<b>Holidays</b>', r.holidays) + row('<b>Half days</b>', r.early) + (r.jewish ? row('<b>Jewish holidays</b>', r.jewish) : '') + base('Every day (baseline)', r.base.all, r.base.day)) +
    tbl(`After a down / up ${r.n} sessions`, 'does a weak run into the holiday carry on or reverse?',
      row(`Holidays, the ${r.n} sessions before <b class="loss">down</b>`, sp.holidays.down) + row(`Holidays, before <b class="gain">up</b>`, sp.holidays.up) +
      row(`Half days, before <b class="loss">down</b>`, sp.early.down) + row(`Half days, before <b class="gain">up</b>`, sp.early.up) +
      (sp.jewish ? row(`Jewish holidays, before <b class="loss">down</b>`, sp.jewish.down) + row(`Jewish holidays, before <b class="gain">up</b>`, sp.jewish.up) : '') +
      base(`Every day after ${r.n} sessions down`, r.base.down) + base(`Every day after ${r.n} sessions up`, r.base.up)) +
    tbl('By holiday', 'for a half day, "Day before" is the half day itself', r.byName.map(g => row(esc(g.name), g)).join('') + r.byEarly.map(g => row(esc(g.name) + ' <span class="dim">(half day)</span>', g)).join('')) +
    (r.byJewish && r.byJewish.length ? tbl('Jewish holidays', 'the market is open: "Day before" is the holiday session itself', r.byJewish.map(g => row(esc(g.name), g)).join('')) +
      `<section class="panel" style="margin-top:12px"><div class="hd"><h2>“Sell Rosh Hashanah, buy Yom Kippur”</h2></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th></th><th class="r">Years</th><th class="r">Average</th><th class="r">Median</th><th class="r">Up</th><th class="r">t</th></tr></thead><tbody>
        ${[['Close before Rosh Hashanah → close of Yom Kippur', r.rhyk], ['Close of Yom Kippur → 10 sessions later', r.ykon], ['Every day: 10 sessions (baseline)', r.base10]].map(([l, x]) => x && x.n ? `<tr><td>${l}</td><td class="r">${x.n.toLocaleString('en-US')}</td><td class="r ${cls(x.avg)}">${pc(x.avg)}</td><td class="r">${pc(x.med)}</td><td class="r">${up(x)}</td><td class="r">${x.t ?? '—'}</td></tr>` : '').join('')}</tbody></table></div>
        <p class="note" style="padding:0 16px 12px">The 7 calendar days between them are about 5–7 sessions, so compare with a 5-session baseline in the first table (+5), not only with 10.</p></section>` : '') +
    `<section class="panel" style="margin-top:12px"><div class="hd"><h2>Events</h2><span class="dim" style="font-size:13px">the latest ${Math.min(120, r.events.length)} of ${r.events.length}</span><div class="grow"></div><button class="btn sm" data-ho="csv-events">Export all events (CSV)</button> <button class="btn sm" data-ho="csv-summary">Export the tables (CSV)</button></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Date</th><th>Holiday</th><th class="r">Before (${r.n})</th><th class="r">Day before</th><th class="r">Gap</th>${[1, 3, 5, 10].map(k => `<th class="r">+${k}</th>`).join('')}</tr></thead><tbody>${ev}</tbody></table></div></section>`;
}

document.addEventListener('change', ev => {
  const t = ev.target; if (!t.dataset || !t.dataset.hov) return;
  S.ho[t.dataset.hov] = ['sym', 'exit'].includes(t.dataset.hov) ? t.value : +t.value;
});
document.addEventListener('click', ev => {
  const el = ev.target.closest('[data-ho]'); if (!el || !$('#sw-body')) return;
  if (el.dataset.ho === 'run') hoLoad();
  if (el.dataset.ho === 'csv-events' || el.dataset.ho === 'csv-summary' || el.dataset.ho === 'csv-bt') hoCsv(el.dataset.ho);
});

// CSV: every event (one row each, returns in %), or every summary row of the tables
function hoCsv(which) {
  const r = S.ho.r; if (!r) return;
  const q = x => x == null ? '' : /[",\n]/.test(String(x)) ? '"' + String(x).replace(/"/g, '""') + '"' : String(x);
  const p = x => x == null ? '' : (x * 100).toFixed(3);
  let rows;
  if (which === 'csv-bt') {
    rows = [['set', 'holiday', 'holiday_date', 'buy_close', 'sell_close', 'sessions', 'return_pct_net']].concat(r.bt.trades.map(t => [t.set, t.holiday, t.date, t.in, t.out, t.days, p(t.ret)]));
  } else if (which === 'csv-events') {
    rows = [['date', 'kind', 'holiday', 'session (last before the holiday / the half day or Jewish holiday itself)', `before_${r.n}_pct`, 'day_before_or_of_pct', 'gap_pct', 'post1_pct', 'post3_pct', 'post5_pct', 'post10_pct']]
      .concat(r.events.map(e => [e.date, e.kind, e.name, e.last, p(e.pre), p(e.day0), p(e.gap), p(e.post[1]), p(e.post[3]), p(e.post[5]), p(e.post[10])]));
  } else {
    const cols = ['pre', 'day0', 'gap', 'p1', 'p3', 'p5', 'p10'];
    rows = [['table', 'group', 'n'].concat(...cols.map(c => [c + '_avg_pct', c + '_median_pct', c + '_up_pct', c + '_t']))];
    const add = (t, lab, g) => rows.push([t, lab, g.n].concat(...cols.map(c => { const x = g[c]; return x && x.n ? [p(x.avg), p(x.med), (x.up * 100).toFixed(1), x.t ?? ''] : ['', '', '', '']; })));
    add('all', 'Holidays', r.holidays); add('all', 'Half days', r.early); if (r.jewish) add('all', 'Jewish holidays', r.jewish);
    add('all', 'Every day (baseline)', { n: r.base.all.p1.n, day0: r.base.day, ...r.base.all });
    for (const k of ['holidays', 'early', 'jewish']) if (r.split[k]) { add('before down', k, r.split[k].down); add('before up', k, r.split[k].up); }
    add('before down', 'Every day', { n: r.base.down.p1.n, ...r.base.down }); add('before up', 'Every day', { n: r.base.up.p1.n, ...r.base.up });
    for (const g of r.byName) add('by holiday', g.name, g);
    for (const g of r.byEarly) add('by half day', g.name, g);
    for (const g of (r.byJewish || [])) add('by Jewish holiday', g.name, g);
  }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([rows.map(x => x.map(q).join(',')).join('\n')], { type: 'text/csv' }));
  a.download = `holidays_${r.sym}_${r.n}_${which === 'csv-events' ? 'events' : which === 'csv-bt' ? 'backtest' : 'tables'}.csv`; a.click(); URL.revokeObjectURL(a.href);
}

// the backtest: long into each US market holiday
function hoBtHtml(b, pc) {
  if (!b || !b.all || !b.all.n) return '';
  const row = (lab, x) => !x || !x.n ? '' : `<tr><td>${lab}</td><td class="r">${x.n}</td><td class="r ${cls(x.avg)}">${pc(x.avg)}</td><td class="r">${Math.round(x.win * 100)}%</td><td class="r">${x.t ?? '—'}</td>
    <td class="r ${cls(x.total)}">${pc(x.total)}</td><td class="r">${pc(x.cagr)}</td><td class="r loss">${pc(x.mdd)}</td><td class="r">${pc(x.best)} / ${pc(x.worst)}</td><td class="r">${x.days}</td><td class="mono dim">${esc(x.from)} → ${esc(x.to)}</td></tr>`;
  let svg = '';
  if (b.curve.length > 2) {
    const W = 900, H = 180, P = 50, ys = b.curve.map(c => c[1]), lo = Math.min(1, ...ys), hi = Math.max(...ys);
    const t0 = new Date(b.curve[0][0]).getTime(), t1 = new Date(b.curve[b.curve.length - 1][0]).getTime();
    const X = d => P + (new Date(d).getTime() - t0) / Math.max(1, t1 - t0) * (W - P - 10), Y = v => 10 + (hi - v) / Math.max(1e-9, hi - lo) * (H - 30);
    const sx = b.split ? X(b.split) : null;
    svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${sx ? `<line x1="${sx}" x2="${sx}" y1="10" y2="${H - 20}" stroke="var(--line)" stroke-dasharray="4 4"/><text x="${sx + 4}" y="20" font-size="11" fill="currentColor" opacity=".6">test →</text>` : ''}
      <line x1="${P}" x2="${W - 10}" y1="${Y(1)}" y2="${Y(1)}" stroke="var(--line)"/><polyline fill="none" stroke="#3b82f6" stroke-width="1.6" points="${b.curve.map(c => X(c[0]).toFixed(1) + ',' + Y(c[1]).toFixed(1)).join(' ')}"/>
      <text x="4" y="${Y(hi) + 4}" font-size="11" fill="currentColor">×${hi.toFixed(2)}</text><text x="4" y="${Y(lo)}" font-size="11" fill="currentColor">×${lo.toFixed(2)}</text></svg>`;
  }
  return `<section class="panel" style="margin-top:12px"><div class="hd"><h2>Backtest: long into US holidays</h2><span class="dim" style="font-size:13px">buy the close ${b.n} sessions before each market holiday, sell ${b.exit === 'post1' ? 'the close of the first session after it' : 'the close of the last session before it'} · ${b.cost} bp a side · special closures left out (not known in advance)</span><div class="grow"></div><button class="btn sm" data-ho="csv-bt">Export trades (CSV)</button></div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th></th><th class="r">Trades</th><th class="r">Avg / trade</th><th class="r">Win</th><th class="r">t</th><th class="r">Total</th><th class="r">CAGR</th><th class="r">Max DD</th><th class="r">Best / worst</th><th class="r">Days in</th><th>Period</th></tr></thead><tbody>
    ${row('Train (first 60% of trades)', b.train)}${row('<b>Test (last 40%)</b>', b.test)}${row('All', b.all)}
    <tr class="dim"><td>Any ${b.hold}-session window (baseline)</td><td class="r">${b.base.n.toLocaleString('en-US')}</td><td class="r ${cls(b.base.avg)}">${pc(b.base.avg)}</td><td class="r">${Math.round(b.base.up * 100)}%</td><td class="r">—</td><td colspan="6" class="dim">${esc(S.ho.sym)} buy &amp; hold over the same ${b.spy.years} years: ${pc(b.spy.total)}</td></tr></tbody></table></div>
    <div class="bd">${svg}<div class="dim" style="font-size:12px">Growth of $1 on the holiday trades only (out of the market the rest of the time). The edge is "Avg / trade" against the baseline row: the same holding time on any day. A strategy for a small part of the year: compare the CAGR with the days in the market, not with buy &amp; hold.</div></div></section>`;
}
