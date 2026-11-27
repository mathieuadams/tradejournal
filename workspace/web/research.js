// Research: backtest a Pine strategy on N days of history (Alpaca SIP for stocks), split train / test,
// optimize its inputs on the train part, check them on the test part, and send the chosen inputs back to the chart.
import { W } from './ctx.js';
import { h, esc, api, toast, money, pct } from './util.js';
import { nyParts, fmtPrice } from './chart.js';
import { compile, securityRequests, metrics, dayRegimes, FUT_SPEC } from './pine.js';
import { freezeDialog } from './frozen.js';
import { editorState } from './editor.js';
import { focusChat } from './chat.js';

const R = { name: '', src: '', indicatorId: null, symbol: '', tf: '', days: 500, com: 0.005, slip: 1, split: 60, objective: 'tday', minTrades: 60, maxMonth: 35, regime: { vol: 'all', trend: 'any' }, tryRegime: false, forever: true,
  sel: {}, study: null, opt: null, running: false, cancel: false, progress: '', cache: new Map(), frozen: null, freezeAt: null, skip: [], budget: 3000, tryDays: false };
let root = null, pool = [];

const OBJ = { tday: ['t of the daily P&L (Research tab)', m => m.tDay], net: ['Net profit', m => m.net], pf: ['Profit factor', m => (Number.isFinite(m.pf) ? m.pf : 99)], sharpe: ['Sharpe', m => m.sharpe], netdd: ['Net ÷ max drawdown', m => m.maxDD ? m.net / m.maxDD : m.net], avg: ['Avg trade', m => m.avg] };

export function openResearch(o = {}) {
  const tab = W.tab();
  if (o.indicatorId) { const ind = tab.indicators.find(i => i.id === o.indicatorId); const sc = W.scriptFor(ind); Object.assign(R, { src: sc.src, name: sc.name, indicatorId: ind.id, skip: [...(ind.skip || [])], regime: { vol: 'all', trend: 'any', ...(ind.regime || {}) } }); CAL.off = new Set(R.skip); }
  else if (o.src) Object.assign(R, { src: o.src, name: o.name || 'Script', indicatorId: o.indicatorId || null });
  else if (!R.src) pickDefault();
  R.symbol = o.symbol || tab.symbol; R.tf = o.tf || tab.tf; if (o.days) R.days = o.days;
  R.study = null; R.opt = null; R.sel = {};
  if (W.currentSide() !== 'research') W.openSide('research', true); else mountResearch(root);
  if (o.run) return runStudy();
}
function pickDefault() {
  const es = editorState(); if (/^\s*strategy\s*\(/m.test(es.src)) { Object.assign(R, { src: es.src, name: es.name, indicatorId: es.indicatorId }); return; }
  const d = W.indOut.find(x => x.out && x.out.strategy); if (d) { const ind = W.tab().indicators.find(i => i.id === d.id); const sc = W.scriptFor(ind); Object.assign(R, { src: sc.src, name: sc.name, indicatorId: d.id }); return; }
  Object.assign(R, { src: es.src, name: es.name, indicatorId: es.indicatorId });
}

export function mountResearch(el) {
  root = el; if (!R.src) pickDefault(); if (!R.symbol) { R.symbol = W.tab().symbol; R.tf = W.tab().tf; }
  const isStrat = /^\s*strategy\s*\(/m.test(R.src);
  const sources = [{ k: 'editor', n: `Editor: ${editorState().name}` }, ...W.indOut.filter(d => d.out).map(d => ({ k: 'ind:' + d.id, n: `Chart: ${d.out.meta.title}${d.out.strategy ? '' : ' (indicator)'}` }))];
  el.innerHTML = '';
  el.appendChild(h(`<div class="ph"><h3>Research</h3><span class="dim" style="font-size:11.5px">backtest · optimize · send back to the chart</span><span class="grow"></span><button class="icon-btn x" title="Close">✕</button></div>`));
  const body = h(`<div class="pbody rs">
    <div class="row"><label class="field" style="flex:2"><span>Script</span><select id="rsSrc">${sources.map(s => `<option value="${s.k}">${esc(s.n)}</option>`).join('')}</select></label></div>
    <div class="row"><label class="field"><span>Symbol</span><input id="rsSym" value="${esc(R.symbol)}"></label><label class="field"><span>Timeframe</span><input id="rsTf" value="${esc(R.tf)}"></label><label class="field"><span>Days</span><input id="rsDays" type="number" value="${R.days}"></label></div>
    <div class="row"><label class="field"><span>Commission $ / share / side</span><input id="rsCom" type="number" step="0.001" value="${R.com}"></label><label class="field"><span>Slippage (ticks)</span><input id="rsSlip" type="number" step="1" value="${R.slip}"></label><label class="field"><span>Train %</span><input id="rsSplit" type="number" value="${R.split}"></label></div>
    <div class="row"><label class="field" style="flex-direction:row;align-items:center;gap:8px" title="Stocks / ETFs: only the 09:30-16:00 candles. Allows long 1-minute histories (up to about 8 years) for regular-session strategies."><input id="rsRth" type="checkbox" ${R.rth ? 'checked' : ''} style="width:auto"><span>Regular hours only (stocks, 09:30–16:00) — long 1-minute history</span></label></div>
    ${isStrat ? '' : `<div class="note warn-box">“${esc(R.name)}” is an indicator: it draws, it doesn't trade. A backtest needs <code>strategy()</code> with <code>strategy.entry</code> / <code>strategy.exit</code>. <button class="btn sm" data-a="convert">Ask Quant to convert it</button></div>`}
    <div class="row" style="margin:6px 0 4px"><button class="btn primary" data-a="study" ${isStrat && !R.running ? '' : 'disabled'}>▶ Run study</button><button class="btn" data-a="stop" ${R.running ? '' : 'hidden'}>Stop</button><span class="dim" id="rsProg" style="flex:2">${esc(R.progress)}</span></div>
    <div id="rsStudy"></div><div id="rsStg"></div><div id="rsOpt"></div>
  </div>`);
  el.appendChild(body);
  el.querySelector('.ph .x').onclick = () => W.openSide('research');
  const srcSel = body.querySelector('#rsSrc');
  srcSel.value = R.indicatorId && sources.some(s => s.k === 'ind:' + R.indicatorId) && !(R.src === editorState().src) ? 'ind:' + R.indicatorId : 'editor';
  srcSel.onchange = () => {
    const v = srcSel.value;
    if (v === 'editor') { const es = editorState(); Object.assign(R, { src: es.src, name: es.name, indicatorId: es.indicatorId }); }
    else { const id = v.slice(4), ind = W.tab().indicators.find(i => i.id === id), sc = W.scriptFor(ind); Object.assign(R, { src: sc.src, name: sc.name, indicatorId: id }); }
    R.study = null; R.opt = null; R.sel = {}; mountResearch(el);
  };
  const bind = (id, k, f = v => v) => { body.querySelector(id).onchange = e => { R[k] = f(e.target.value); }; };
  bind('#rsSym', 'symbol', v => v.trim().toUpperCase()); bind('#rsTf', 'tf', v => W.normTfStrict(v) || R.tf); bind('#rsDays', 'days', v => Math.max(5, Math.min(2000, +v || 500)));
  { const x = body.querySelector('#rsRth'); if (x) x.onchange = () => { R.rth = x.checked; }; }
  bind('#rsCom', 'com', v => Math.max(0, +v || 0)); bind('#rsSlip', 'slip', v => Math.max(0, +v || 0)); bind('#rsSplit', 'split', v => Math.max(10, Math.min(95, +v || 60)));
  body.onclick = e => {
    const fz0 = e.target.closest('[data-freeze]'); if (fz0) { freezeForm(+fz0.dataset.freeze); return; }
    const ap = e.target.closest('[data-apply]'); if (ap) { applyInputs(R.opt.rows[+ap.dataset.apply].values); return; }
    const a = e.target.closest('[data-a]'); if (!a) return;
    if (a.dataset.a === 'study') runStudy();
    if (a.dataset.a === 'stop') R.cancel = true;
    if (a.dataset.a === 'convert') { if (!W.state.chatOpen) W.toggleChat(true); focusChat(`Convert my "${R.name}" script into a Pine strategy (strategy() with strategy.entry / strategy.exit, same rules and inputs) so I can backtest and optimize it in Research. Add it to the chart.`, { script: true }); }
    if (a.dataset.a === 'optimize') runOptimize();
    if (a.dataset.a === 'staged') runStaged();
    if (a.dataset.a === 'apply') applyInputs(R.study.inputs.reduce((o, i) => (o[i.idx] = i.value, o), {}));
    if (a.dataset.a === 'freeze') freezeForm(null);
    const tr = e.target.closest('[data-trade]'); if (tr) gotoTrade(R.study.trades[+tr.dataset.trade]);
  };
  renderStudy(); renderStaged(); renderOpt();
}
const setProg = t => { R.progress = t; const p = root && root.querySelector('#rsProg'); if (p) p.textContent = t; };

// ------------------------------------------------------------------------------------------------ data & workers
async function loadBars() {
  const k = `${R.symbol}|${R.tf}|${R.days}|${R.src.length}|${R.rth ? 1 : 0}`;
  if (!R.cache.has(k)) {
    setProg(`Loading ${R.days} days of ${R.symbol} ${R.tf}…`);
    const get = async (sym, tf) => (await api(`bars?symbol=${encodeURIComponent(sym)}&tf=${encodeURIComponent(tf)}&days=${R.days}&feed=sip${R.rth ? '&rth=1' : ''}`)).bars.map(b => ({ t: b[0], o: b[1], h: b[2], l: b[3], c: b[4], v: b[5] }));
    const bars = await get(R.symbol, R.tf);
    if (bars.length < 50) throw new Error(`Only ${bars.length} bars of ${R.symbol} ${R.tf}`);
    let sec = [], securities = null;
    try { sec = securityRequests(compile(R.src), bars, { symbol: R.symbol, tf: R.tf }); } catch (e) { sec = []; }
    if (sec.length) {
      securities = {};
      for (const r of sec) { setProg(`Loading ${R.days} days of ${r.sym} ${r.tf} (request.security)…`); securities[r.key] = await get(r.sym, r.tf); }
    }
    R.cache.clear(); R.cache.set(k, { bars, securities, sec });
  }
  const d = R.cache.get(k);
  return { key: k, bars: d.bars, securities: d.securities, sec: d.sec };
}
function workers(n) {
  while (pool.length < n) { const w = new Worker(new URL('./research-worker.js', import.meta.url), { type: 'module' }); w.key = null; pool.push(w); }
  return pool.slice(0, n);
}
async function prime(ws, data) {
  await Promise.all(ws.map(w => w.key === data.key ? null : new Promise(res => { const f = e => { if (e.data.type === 'ready') { w.removeEventListener('message', f); w.key = data.key; res(); } }; w.addEventListener('message', f); w.postMessage({ type: 'bars', key: data.key, bars: data.bars, securities: data.securities }); })));
}
function job(w, msg) { return new Promise(res => { const f = e => { if (e.data.type === 'result' && e.data.id === msg.id) { w.removeEventListener('message', f); res(e.data); } }; w.addEventListener('message', f); w.postMessage({ type: 'run', ...msg }); }); }
function baseMsg(data) {
  const bars = data.bars, cut = bars[Math.floor(bars.length * R.split / 100)].t;
  return { src: R.src, symbol: R.symbol, tf: R.tf, tfSec: null, splitTime: cut, strategy: { comType: 'cash_per_contract', comVal: R.com, slip: R.slip, skip: R.skip.length ? R.skip : undefined, regime: regimeOn(R.regime) ? R.regime : undefined } };
}

// ------------------------------------------------------------------------------------------------ study
export async function runStudy(inputs) {
  if (R.running) return null;
  R.running = true; R.cancel = false; mountResearch(root);
  try {
    const data = await loadBars(), [w] = workers(1); await prime([w], data);
    setProg(`Backtesting ${data.bars.length.toLocaleString()} bars…`);
    const r = await job(w, { ...baseMsg(data), id: 'study', inputs: inputs || currentInputs(), full: true });
    if (r.error) throw new Error(r.error);
    const b = data.bars;
    R.study = { ...r, from: b[0].t, to: b[b.length - 1].t, cut: baseMsg(data).splitTime, n: b.length, symbol: R.symbol, tf: R.tf };
    R.sel = {}; setProg(`${b.length.toLocaleString()} bars · ${fmtD(b[0].t)} → ${fmtD(b[b.length - 1].t)} · ${r.ms} ms` + (r.warn && r.warn.length ? ' · ⚠ ' + r.warn.join(' ') : ''));
    if (r.warn && r.warn.length) toast(r.warn[0], 'err');
    return R.study;
  } catch (e) { setProg('⚠ ' + e.message); toast(e.message, 'err'); return { error: e.message }; }
  finally { R.running = false; mountResearch(root); }
}
function currentInputs() { const ind = R.indicatorId && W.tab().indicators.find(i => i.id === R.indicatorId); return ind && ind.inputs ? ind.inputs : null; }
const fmtD = t => { const p = nyParts(t); return `${p.mon} ${p.d} ${p.y}`; };
const cls = v => v > 0 ? 'up' : v < 0 ? 'dn' : '';

function renderStudy() {
  const el = root && root.querySelector('#rsStudy'); if (!el || !R.study) return;
  const S = R.study, cols = [['All', S.all], [`Train ${R.split}%`, S.train], [`Test ${100 - R.split}%`, S.test]];
  const row = (n, f) => `<tr><td class="t">${n}</td>${cols.map(([, m]) => `<td>${f(m)}</td>`).join('')}</tr>`;
  el.innerHTML = `<div class="tree-sec">Results · ${esc(S.symbol)} ${esc(S.tf)} · ${fmtD(S.from)} → ${fmtD(S.to)}</div>
    <table class="tbl rs-tbl"><tr><th></th>${cols.map(([n]) => `<th>${n}</th>`).join('')}</tr>
    ${row('Net profit', m => `<b class="${cls(m.net)}">${money(m.net, 0)}</b>`)}${row('Trades', m => m.n)}${row('Win rate', m => m.winRate.toFixed(1) + '%')}
    ${row('Profit factor', m => Number.isFinite(m.pf) ? m.pf.toFixed(2) : '∞')}${row('Avg trade', m => `<span class="${cls(m.avg)}">${money(m.avg)}</span>`)}
    ${row('Avg win / loss', m => `${money(m.avgWin, 0)} / ${money(m.avgLoss, 0)}`)}${row('Max drawdown', m => money(m.maxDD, 0))}${row('Sharpe (daily)', m => m.sharpe.toFixed(2))}
    ${row('Long', m => `${m.long.n} · <span class="${cls(m.long.net)}">${money(m.long.net, 0)}</span>`)}${row('Short', m => `${m.short.n} · <span class="${cls(m.short.net)}">${money(m.short.net, 0)}</span>`)}</table>
    <canvas id="rsEq" width="900" height="200" style="width:100%;height:150px;margin:10px 0;border-radius:8px;background:var(--bg2)"></canvas>
    ${breakdown(S.trades)}
    <div id="rsCal"></div>
    <div class="row" style="margin:8px 0;flex-wrap:wrap"><button class="btn sm" data-a="apply" title="Put these inputs on the chart">Send to the chart</button><button class="btn sm primary" data-a="freeze" title="Lock this script and these inputs and paper trade it from now on">❄ Freeze → paper</button></div>
    ${optForm(S.inputs)}
    <div class="tree-sec">Trades (${S.trades.length}) · click to see it on the chart</div>
    <div class="rs-trades"><table class="tbl"><tr><th>Entry</th><th>Side</th><th>Qty</th><th>In</th><th>Out</th><th>P&L</th><th>Exit</th></tr>
    ${S.trades.slice(-300).reverse().map(t => { const k = S.trades.indexOf(t); return `<tr data-trade="${k}" style="cursor:pointer"><td class="t">${fmtDT(t.entryTime)}</td><td class="${t.dir > 0 ? 'up' : 'dn'}">${t.dir > 0 ? 'Long' : 'Short'}</td><td>${t.qty}</td><td>${fmtPrice(t.entryPx)}</td><td>${fmtPrice(t.exitPx)}</td><td class="${cls(t.pnl)}">${money(t.pnl)}</td><td class="t dim">${esc(t.why || '')}</td></tr>`; }).join('')}</table></div>`;
  drawEquity(el.querySelector('#rsEq'), S);
  renderCal();
}
// ------------------------------------------------------------------------------------------------ day / hour filter calendar
// Each cell: the trades entered that weekday and hour (NY), train net over test net. Click a cell, a day or an hour to
// leave it out; the table under it shows train and test with and without them. A filter only holds up if what it removes
// also loses in the test part, which was not used to choose it.
const CAL = { off: new Set() };
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri'];
function renderCal() {
  const el = root && root.querySelector('#rsCal'); if (!el || !R.study) return;
  const S = R.study, T = S.trades, cut = S.cut;
  const key = t => { const p = nyParts(t.entryTime); return `${p.wd}|${p.h}`; };
  const hrs = [...new Set(T.map(t => nyParts(t.entryTime).h))].sort((a, b) => ((a + 6) % 24) - ((b + 6) % 24));      // the session order: 18:00 first
  const days = DAYS.filter(d => T.some(t => nyParts(t.entryTime).wd === d));
  const cell = {};
  for (const t of T) { const k = key(t), c = cell[k] || (cell[k] = { tr: 0, te: 0, ntr: 0, nte: 0 }); if (t.entryTime < cut) { c.tr += t.pnl; c.ntr++; } else { c.te += t.pnl; c.nte++; } }
  const maxAbs = Math.max(1, ...Object.values(cell).flatMap(c => [Math.abs(c.tr), Math.abs(c.te)]));
  const shade = v => { const a = Math.min(0.85, 0.12 + Math.abs(v) / maxAbs * 0.73); return v > 0 ? `rgba(8,153,129,${a})` : v < 0 ? `rgba(242,54,69,${a})` : 'transparent'; };
  const k0 = v => Math.abs(v) >= 1000 ? (v / 1000).toFixed(1) + 'k' : Math.round(v);
  const sum = (f) => { const x = { tr: 0, te: 0 }; for (const [k, c] of Object.entries(cell)) if (f(k)) { x.tr += c.tr; x.te += c.te; } return x; };
  const isOff = k => CAL.off.has(k) || CAL.off.has(k.split('|')[0] + '|*') || CAL.off.has('*|' + k.split('|')[1]);
  const keep = T.filter(t => !isOff(key(t))), drop = T.filter(t => isOff(key(t)));
  const m = (xs, a, b) => metrics(xs.filter(t => t.entryTime >= a && t.entryTime < b));
  const cols = [['Train', -Infinity, cut], ['Test', cut, Infinity]];
  const base = cols.map(([, a, b]) => m(T, a, b)), kept = cols.map(([, a, b]) => m(keep, a, b)), rem = cols.map(([, a, b]) => m(drop, a, b));
  const pfx = x => Number.isFinite(x.pf) ? x.pf.toFixed(2) : x.n ? '∞' : '—';
  const line = (name, ms, cls2 = '') => `<tr class="${cls2}"><td class="t">${name}</td>${ms.map(x => `<td>${x.n}</td><td class="${cls(x.net)}">${money(x.net, 0)}</td><td>${pfx(x)}</td><td>${x.n ? money(x.avg, 0) : '—'}</td>`).join('')}</tr>`;
  let verdict = '';
  if (drop.length) {
    const [rTr, rTe] = rem, [kTr, kTe] = kept, [bTr, bTe] = base;
    const held = rTe.net < 0, better = kTe.n && kTe.avg > bTe.avg;
    const t = (() => { const xs = drop.filter(x => x.entryTime >= cut).map(x => x.pnl); if (xs.length < 3) return NaN; const mu = xs.reduce((a, b) => a + b, 0) / xs.length, sd = Math.sqrt(xs.reduce((a, b) => a + (b - mu) ** 2, 0) / (xs.length - 1)); return sd ? mu / (sd / Math.sqrt(xs.length)) : NaN; })();
    const strong = held && better && Number.isFinite(t) && t <= -2;
    const st = strong ? ['ok', 'The filter holds up in the test part'] : held && better ? ['meh', 'Inconclusive: the removed trades also lost in test, but not by more than chance'] : ['bad', 'The filter does not hold up in the test part'];
    verdict = `<div class="cal-verdict ${st[0]}"><b>${st[1]}</b>:
      the removed trades made ${money(rTr.net, 0)} in train and ${money(rTe.net, 0)} in test (${rTe.n} trades${Number.isFinite(t) ? `, t = ${t.toFixed(2)}` : ''}).
      Avg trade in test ${money(bTe.avg, 0)} → ${money(kTe.avg, 0)}. ${Number.isFinite(t) && Math.abs(t) < 2 ? 'With |t| under 2 the removed trades are not clearly worse than zero: likely noise.' : ''}
      Choose the cells on the train numbers (top of each cell) and judge by the test ones.</div>`;
  }
  el.innerHTML = `<div class="tree-sec">Day / hour filter · click a cell, a day or an hour to leave it out · top: train, bottom: test</div>
    <div class="cal-wrap"><table class="cal"><tr><th class="cal-c" data-cal="reset" title="Clear the filter">↺</th>${hrs.map(hh => `<th data-cal="*|${hh}" class="${CAL.off.has('*|' + hh) ? 'off' : ''}">${hh}</th>`).join('')}<th>Day</th></tr>
    ${days.map(d => `<tr><th data-cal="${d}|*" class="${CAL.off.has(d + '|*') ? 'off' : ''}">${d}</th>${hrs.map(hh => { const k = `${d}|${hh}`, c = cell[k]; if (!c) return '<td class="nil"></td>';
        return `<td data-cal="${k}" class="${isOff(k) ? 'off' : ''}" title="${d} ${hh}:00 — train ${c.ntr} trades ${money(c.tr, 0)} · test ${c.nte} trades ${money(c.te, 0)}"><span style="background:${shade(c.tr)}">${k0(c.tr)}</span><span style="background:${shade(c.te)}">${k0(c.te)}</span></td>`; }).join('')}
      ${(() => { const x = sum(k => k.startsWith(d + '|')); return `<td class="tot"><span class="${cls(x.tr)}">${k0(x.tr)}</span><span class="${cls(x.te)}">${k0(x.te)}</span></td>`; })()}</tr>`).join('')}
    <tr><th>Hour</th>${hrs.map(hh => { const x = sum(k => k.endsWith('|' + hh)); return `<td class="tot"><span class="${cls(x.tr)}">${k0(x.tr)}</span><span class="${cls(x.te)}">${k0(x.te)}</span></td>`; }).join('')}<td></td></tr></table></div>
    <table class="tbl rs-res cal-res"><tr><th class="t"></th><th colspan="4" class="grp">Train ${R.split}%</th><th colspan="4" class="grp tst">Test ${100 - R.split}%</th></tr>
      <tr><th class="t"></th><th>N</th><th>Net</th><th>PF</th><th>Avg</th><th class="tst">N</th><th class="tst">Net</th><th class="tst">PF</th><th class="tst">Avg</th></tr>
      ${line('All trades', base)}${drop.length ? line('Kept (filtered)', kept, 'hl') + line('Removed', rem) : ''}</table>
    ${verdict}
    <div class="row" style="margin:6px 0 12px;flex-wrap:wrap;align-items:center;gap:8px">
      <button class="btn sm primary" data-cal-apply ${CAL.off.size || R.skip.length ? '' : 'disabled'} title="Re-run the backtest with no entries in the left-out cells (exact: freed-up time can take other trades), then Send to the chart / Optimize / Freeze use it too">Apply the filter in the backtest</button>
      ${R.skip.length ? `<span class="note" style="margin:0">Backtest filter on: no entries ${esc(skipText(R.skip))} · <a href="#" data-cal-clear>remove</a></span>` : ''}</div>`;
  const ap = el.querySelector('[data-cal-apply]'); if (ap) ap.onclick = () => { R.skip = [...CAL.off]; runStudy(); };
  const cl = el.querySelector('[data-cal-clear]'); if (cl) cl.onclick = e => { e.preventDefault(); R.skip = []; CAL.off.clear(); runStudy(); };
  el.querySelector('.cal').onclick = e => {
    const c = e.target.closest('[data-cal]'); if (!c) return; const k = c.dataset.cal;
    if (k === 'reset') CAL.off.clear(); else if (CAL.off.has(k)) CAL.off.delete(k); else CAL.off.add(k);
    renderCal();
  };
}

const regimeOn = g => g && ((g.vol && g.vol !== 'all') || (g.trend && g.trend !== 'any'));
export function regimeText(g) { return regimeOn(g) ? [g.vol !== 'all' ? `${g.vol} volatility` : '', g.trend !== 'any' ? `${g.trend}trend` : ''].filter(Boolean).join(', ') : 'all conditions'; }
export function skipText(skip) {
  const days = skip.filter(k => k.endsWith('|*')).map(k => k.split('|')[0]), hrs = skip.filter(k => k.startsWith('*|')).map(k => k.split('|')[1] + ':00'), cells = skip.filter(k => !k.includes('*')).map(k => k.replace('|', ' ') + ':00');
  return [days.length ? 'on ' + days.join(', ') : '', hrs.length ? 'at ' + hrs.join(', ') : '', cells.length ? 'on ' + cells.join(', ') : ''].filter(Boolean).join('; ');
}

const fmtDT = t => { const p = nyParts(t); return `${p.mon} ${p.d} '${String(p.y).slice(2)} ${String(p.h).padStart(2, '0')}:${String(p.mi).padStart(2, '0')}`; };
function breakdown(T) {
  const by = (f, keys) => keys.map(k => { const x = T.filter(t => f(t) === k), net = x.reduce((a, t) => a + t.pnl, 0); return x.length ? `<tr><td class="t">${k}</td><td>${x.length}</td><td>${(x.filter(t => t.pnl > 0).length / x.length * 100).toFixed(0)}%</td><td class="${cls(net)}">${money(net, 0)}</td></tr>` : ''; }).join('');
  const hrs = [...new Set(T.map(t => nyParts(t.entryTime).h))].sort((a, b) => a - b);
  return `<div class="row" style="align-items:flex-start;gap:12px"><div><div class="tree-sec">By weekday</div><table class="tbl"><tr><th></th><th>N</th><th>Win</th><th>Net</th></tr>${by(t => nyParts(t.entryTime).wd, ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])}</table></div>
    <div><div class="tree-sec">By entry hour (NY)</div><table class="tbl"><tr><th></th><th>N</th><th>Win</th><th>Net</th></tr>${by(t => nyParts(t.entryTime).h + ':00', hrs.map(x => x + ':00'))}</table></div></div>`;
}
function drawEquity(cv, S) {
  if (!cv) return; const g = cv.getContext('2d'), Wd = cv.width, H = cv.height; g.clearRect(0, 0, Wd, H);
  const T = S.trades; if (!T.length) { g.fillStyle = '#868c99'; g.font = '13px Inter'; g.fillText('No trades', 12, 24); return; }
  let eq = 0; const pts = [[S.from, 0], ...T.map(t => [t.exitTime, eq += t.pnl])];
  const lo = Math.min(0, ...pts.map(p => p[1])), hi = Math.max(0, ...pts.map(p => p[1])), X = t => 8 + (t - S.from) / ((S.to - S.from) || 1) * (Wd - 16), Y = v => 10 + (hi - v) / ((hi - lo) || 1) * (H - 20);
  g.strokeStyle = '#262b36'; g.beginPath(); g.moveTo(0, Y(0)); g.lineTo(Wd, Y(0)); g.stroke();
  g.setLineDash([4, 4]); g.strokeStyle = '#5d6370'; g.beginPath(); g.moveTo(X(S.cut), 0); g.lineTo(X(S.cut), H); g.stroke(); g.setLineDash([]);
  g.fillStyle = '#868c99'; g.font = '11px Inter'; g.fillText('train', X(S.cut) - 36, 14); g.fillText('test', X(S.cut) + 6, 14);
  g.lineWidth = 2; g.strokeStyle = eq >= 0 ? '#089981' : '#f23645'; g.beginPath(); pts.forEach(([t, v], k) => k ? g.lineTo(X(t), Y(v)) : g.moveTo(X(t), Y(v))); g.stroke();
}
function gotoTrade(t) {
  const go = () => { W.chart.showRange(t.entryTime - 40 * W.chart.tfSec); };
  if (W.tab().symbol !== R.study.symbol || W.tab().tf !== R.study.tf) { W.setSymbol(R.study.symbol).then(() => W.setTf(R.study.tf)).then(() => setTimeout(go, 300)); } else go();
  toast(`${t.dir > 0 ? 'Long' : 'Short'} ${fmtDT(t.entryTime)} → ${fmtDT(t.exitTime)}: ${money(t.pnl)} (only bars loaded on the chart can be shown; scroll left for older ones)`);
}

// ------------------------------------------------------------------------------------------------ optimizer
function optForm(inputs) {
  const num = inputs.filter(i => ['int', 'float', 'price'].includes(i.type) && !i.options), other = inputs.filter(i => i.type === 'bool' || i.options);
  if (!num.length && !other.length) return '';
  const rowN = i => { const on = !!R.sel[i.idx], s = R.sel[i.idx] || defRange(i), d = on ? '' : 'disabled';
    return `<tr class="${on ? 'on' : ''}" data-row="${i.idx}"><td class="nm"><label><input type="checkbox" data-sel="${i.idx}" ${on ? 'checked' : ''}><span>${esc(i.title)}</span></label></td><td class="now">${esc(String(i.value))}</td><td><input class="rs-in" type="number" data-f="${i.idx}" value="${s.from}" ${d}></td><td><input class="rs-in" type="number" data-t="${i.idx}" value="${s.to}" ${d}></td><td><input class="rs-in" type="number" data-s="${i.idx}" value="${s.step}" ${d}></td></tr>`; };
  const rowO = i => `<tr class="${R.sel[i.idx] ? 'on' : ''}" data-row="${i.idx}"><td class="nm"><label><input type="checkbox" data-sel="${i.idx}" ${R.sel[i.idx] ? 'checked' : ''}><span>${esc(i.title)}</span></label></td><td class="now">${esc(String(i.value))}</td><td colspan="3" class="dim">tries ${i.type === 'bool' ? 'on and off' : `all ${i.options.length} options`}</td></tr>`;
  setTimeout(wireOpt);
  return `<div class="tree-sec">Optimizer · like the journal's Research tab</div>
    <p class="note" style="margin-top:0">Each setting is a full backtest of this strategy, net of costs, starting from the current inputs. Settings are chosen on the <b>train</b> days only, ranked by the t of the daily P&L, with at least ${R.minTrades} trades and no month over ${R.maxMonth}% of the profit. The <b>test</b> days are never used to choose. With no box ticked it searches every input; tick inputs to search only those, in your ranges.</p>
    <table class="rs-opt"><colgroup><col class="c-nm"><col class="c-now"><col><col><col></colgroup><tr><th>Input</th><th>Now</th><th>From</th><th>To</th><th>Step</th></tr>${num.map(rowN).join('')}${other.map(rowO).join('')}</table>
    <div class="row" style="margin:8px 0"><label class="field"><span>Rank by (train)</span><select id="rsObj">${Object.entries(OBJ).map(([k, [n]]) => `<option value="${k}" ${R.objective === k ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
      <label class="field"><span>Min trades (train)</span><input id="rsMin" type="number" value="${R.minTrades}"></label>
      <label class="field"><span>Max runs</span><input id="rsBudget" type="number" min="10" max="50000" step="500" value="${R.budget}" title="Up to this many: every combination; more combinations: a random search, then around the best ones"></label>
      <label class="field"><span>No month over (% of profit)</span><input id="rsMonth" type="number" min="10" max="100" step="5" value="${R.maxMonth}"></label></div>
    <div class="row rs-checks"><label><input type="checkbox" id="rsTryDays" ${R.tryDays ? 'checked' : ''}> Also try leaving out each weekday</label>
      <label><input type="checkbox" id="rsForever" ${R.forever ? 'checked' : ''}> Keep searching until Stop</label>
      <button class="btn primary" data-a="staged" style="align-self:flex-end;margin-bottom:10px" ${R.running ? 'disabled' : ''} title="Measure each input alone first, then optimize the most influential ones together, then the next tier, then fine-tune the rest">▶ Staged: most influential first</button>
      <button class="btn" data-a="optimize" style="align-self:flex-end;margin-bottom:10px" ${R.running ? 'disabled' : ''}>Full search <span id="rsCombos" class="dim"></span></button></div>`;
}
function defRange(i) {
  const v = +i.value, st = i.step || (i.type === 'int' ? Math.max(1, Math.round(Math.abs(v) / 5)) : +(Math.abs(v) / 5 || 0.1).toPrecision(2));
  let from = i.type === 'int' ? Math.max(i.minval ?? -Infinity, Math.round(v / 2)) : +(v / 2).toPrecision(3), to = i.type === 'int' ? Math.round(v * 2) : +(v * 2).toPrecision(3);
  if (i.minval != null) from = Math.max(from, i.minval); if (i.maxval != null) to = Math.min(to, i.maxval); if (from === to) to = from + st * 4;
  return { from, to, step: st };
}
function wireOpt() {
  const el = root && root.querySelector('.rs-opt'); if (!el) return;
  const ins = R.study.inputs;
  el.onchange = e => {
    const t = e.target, k = t.dataset.sel ?? t.dataset.f ?? t.dataset.t ?? t.dataset.s, i = ins.find(x => String(x.idx) === String(k)); if (!i) return;
    if (t.dataset.sel !== undefined) {
      if (t.checked) R.sel[k] = { ...defRange(i), ...(readRow(k) || {}) }; else delete R.sel[k];
      const tr = el.querySelector(`tr[data-row="${k}"]`); if (tr) { tr.classList.toggle('on', t.checked); tr.querySelectorAll('.rs-in').forEach(x => { x.disabled = !t.checked; }); }
    }
    else if (R.sel[k]) Object.assign(R.sel[k], readRow(k));
    combos();
  };
  const o = root.querySelector('#rsObj'); if (o) o.onchange = () => { R.objective = o.value; if (R.opt) { sortOpt(); renderOpt(); } };
  const bu = root.querySelector('#rsBudget'); if (bu) bu.onchange = () => { R.budget = Math.max(10, Math.min(50000, Math.round(+bu.value || 3000))); combos(); };
  const dy = root.querySelector('#rsTryDays'); if (dy) dy.onchange = () => { R.tryDays = dy.checked; combos(); };
  const rg = root.querySelector('#rsRegime'); if (rg) rg.onchange = () => { R.tryRegime = rg.checked; combos(); };
  const fv = root.querySelector('#rsForever'); if (fv) fv.onchange = () => { R.forever = fv.checked; combos(); };
  const mo = root.querySelector('#rsMonth'); if (mo) mo.onchange = () => { R.maxMonth = Math.max(10, Math.min(100, +mo.value || 35)); if (R.opt) { sortOpt(); renderOpt(); } };
  const mn = root.querySelector('#rsMin'); if (mn) mn.onchange = () => { R.minTrades = Math.max(0, +mn.value || 0); if (R.opt) { sortOpt(); renderOpt(); } };
  combos();
}
function readRow(k) { const g = s => root.querySelector(`[data-${s}="${k}"]`); if (!g('f')) return null; return { from: +g('f').value, to: +g('t').value, step: Math.abs(+g('s').value) || 1 }; }
// the search space: one axis per ticked input (+ the weekday axis); combinations are numbered (mixed radix), never all built
function axesOf() {
  const ins = R.study.inputs, axes = [];
  const sel = Object.keys(R.sel).length ? R.sel : Object.fromEntries(ins.filter(i => !(i.type === 'bool' && /^(show|draw|plot|display)/i.test(i.title)) && i.type !== 'string' || i.options)
    .filter(i => ['int', 'float', 'price', 'bool'].includes(i.type) || i.options).map(i => [i.idx, defRange(i)]));
  for (const [k, s] of Object.entries(sel)) {
    const i = ins.find(x => String(x.idx) === String(k)); if (!i) continue;
    if (i.type === 'bool') axes.push([k, [true, false]]);
    else if (i.options) axes.push([k, i.options]);
    else if (/HHMM/i.test(i.title) && i.type === 'int' && !R.sel[k]) {         // a clock time: every 15 minutes, 2 hours either side
      const m0 = Math.floor(+i.value / 100) * 60 + (+i.value % 100), vals = [];
      for (let m = m0 - 120; m <= m0 + 120; m += 15) { const mm = ((m % 1440) + 1440) % 1440; vals.push(Math.floor(mm / 60) * 100 + mm % 60); }
      axes.push([k, vals]);
    }
    else { const vals = []; for (let v = s.from; v <= s.to + 1e-9 && vals.length < 500; v += s.step) vals.push(i.type === 'int' ? Math.round(v) : +v.toPrecision(10)); axes.push([k, [...new Set(vals)]]); }
    const ax = axes[axes.length - 1], cur = i.value;                          // the current value is always one of the values
    if (ax && ax[0] === k && !ax[1].includes(cur)) { ax[1].push(cur); if (ax[1].every(x => typeof x === 'number')) ax[1].sort((x, y) => x - y); }
  }
  if (R.tryDays) axes.push(['__skip', ['all days', 'no Sun', 'no Mon', 'no Tue', 'no Wed', 'no Thu', 'no Fri']]);
  if (R.tryRegime) { axes.push(['__vol', ['all', 'low', 'mid', 'high']]); axes.push(['__trend', ['any', 'up', 'down']]); }
  return axes;
}
// ------------------------------------------------------------------------------------------------ staged optimizer
// 1. influence: every input alone, the others at the current setting -> how much the train score moves with it
// 2. tier 1 (the most influential) optimized together  3. tier 2 together, from that best  4. the rest one at a time,
// kept only when they improve the train score. A few hundred backtests instead of the whole space.
async function runStaged() {
  const axes = axesOf(); if (!axes.length) { toast('This strategy has no inputs to optimize'); return; }
  const f = OBJ[R.objective][1], base0 = R.study.inputs.reduce((o, i) => (o[i.idx] = i.value, o), {});
  const raw = m => (m && m.n ? f(m) : -Infinity);
  // settings that meet the trade / month rules first, then the score
  const gt = (a, b) => { const pa = !!(a && passes(a)), pb = !!(b && passes(b)); return pa !== pb ? pa : raw(a) > raw(b) + 1e-9; };
  R.running = true; R.cancel = false; R.opt = null;
  R.staged = { stage: 'Influence of each input', infl: [], stages: [], runs: 0 }; mountResearch(root);
  const out = [];
  try {
    const data = await loadBars(), n = Math.max(1, Math.min(6, (navigator.hardwareConcurrency || 4) - 1)), ws = workers(n); await prime(ws, data);
    const msg = baseMsg(data), t0 = Date.now(), cache = new Map();
    const runAll = async settings => {                     // settings: [{ k: v }] over the base -> results (cached)
      const todo = settings.map(st => ({ st, key: JSON.stringify(st) })), res = new Array(todo.length); let next = 0;
      await Promise.all(ws.map(async (w, wi) => {
        while (!R.cancel && next < todo.length) {
          const j = next++, { st, key } = todo[j];
          if (cache.has(key)) { res[j] = cache.get(key); continue; }
          const values = { ...base0, ...st };
          const r = await job(w, { ...msg, id: `s${wi}_${R.staged.runs}`, inputs: values });
          R.staged.runs++; const row = r.error ? null : { values, st, train: r.train, test: r.test, all: r.all }; cache.set(key, row); res[j] = row;
          if (R.staged.runs % 5 === 0) setProg(`Staged optimizer · ${R.staged.stage} · ${R.staged.runs} backtests · ${((Date.now() - t0) / 1000).toFixed(0)}s`);
        }
      }));
      return res;
    };
    const cur0 = Object.fromEntries(axes.map(([k]) => [k, base0[k]]));
    const [b0] = await runAll([{}]); if (!b0) throw new Error('The current setting fails to run');
    const stageRow = (name, st, r) => { R.staged.stages.push({ name, st: { ...st }, r }); out.push(r); renderStaged(); };
    stageRow('Current setting', {}, b0);
    // 1. influence, one input at a time
    for (const [k, vals] of axes) {
      if (R.cancel) break;
      const rs = await runAll(vals.map(v => ({ [k]: v })));
      const pts = vals.map((v, j) => ({ v, r: rs[j] })).filter(x => x.r);
      const sc = pts.map(x => raw(x.r.train)).filter(Number.isFinite), te = pts.map(x => raw(x.r.test)).filter(Number.isFinite);
      const ord = pts.filter(x => Number.isFinite(raw(x.r.train))).sort((a, b) => gt(b.r.train, a.r.train) ? 1 : gt(a.r.train, b.r.train) ? -1 : 0), best = ord[0];
      R.staged.infl.push({ k, title: titleOf(k), cur: cur0[k], spread: sc.length ? Math.max(...sc) - Math.min(...sc) : 0, testSpread: te.length ? Math.max(...te) - Math.min(...te) : 0,
        best: best ? best.v : cur0[k], ranked: ord.map(x => x.v), n: vals.length });
      renderStaged();
    }
    if (R.cancel) throw new Error('stopped');
    // tiers
    const I = R.staged.infl.slice().sort((a, b) => b.spread - a.spread), top = I[0] ? I[0].spread : 0;
    I.forEach((x, j) => { x.tier = x.spread <= 1e-9 ? 0 : j < 3 ? 1 : j < 8 ? 2 : 3; });      // top 3, next 5, the rest; no effect: skipped
    renderStaged();
    let cur = {}, curR = b0;
    const better = r => r && gt(r.train, curR.train);
    // 2. / 3. a tier together: the best few values of each (from step 1), all combinations, around the current best
    const together = async (tier, label, keep) => {
      const T = I.filter(x => x.tier === tier); if (!T.length) return;
      R.staged.stage = label;
      let lists = T.map(x => [x.k, [...new Set([cur[x.k] ?? x.cur, ...x.ranked.slice(0, keep)])]]);
      while (lists.reduce((p, [, v]) => p * v.length, 1) > 2500 && lists.some(([, v]) => v.length > 2)) lists = lists.map(([k, v]) => [k, v.length > 2 ? v.slice(0, v.length - 1) : v]);
      const combos = lists.reduce((acc, [k, v]) => acc.flatMap(c => v.map(x => ({ ...c, [k]: x }))), [{}]);
      const rs = await runAll(combos.map(c => ({ ...cur, ...c })));
      let bi = -1; rs.forEach((r, j) => { if (better(r) && (bi < 0 || gt(r.train, rs[bi].train))) bi = j; });
      if (bi >= 0) { cur = { ...cur, ...combos[bi] }; curR = rs[bi]; }
      stageRow(`${label} (${T.map(x => short(x.title)).join(', ')}): ${combos.length} combinations`, cur, curR);
    };
    await together(1, 'Tier 1 together', 6); if (R.cancel) throw new Error('stopped');
    await together(2, 'Tier 2 together', 4); if (R.cancel) throw new Error('stopped');
    // 4. the rest, one at a time from the best so far (kept only if the train score improves)
    R.staged.stage = 'Tier 3, one at a time';
    for (const x of I.filter(y => y.tier === 3)) {
      if (R.cancel) break;
      const vals = (axes.find(a => a[0] === x.k) || [, []])[1];
      const rs = await runAll(vals.map(v => ({ ...cur, [x.k]: v })));
      let bi = -1; rs.forEach((r, j) => { if (better(r) && (bi < 0 || gt(r.train, rs[bi].train))) bi = j; });
      if (bi >= 0) { cur = { ...cur, [x.k]: vals[bi] }; curR = rs[bi]; }
    }
    stageRow('Tier 3, one at a time', cur, curR);
    // one more pass over tier 1 from the final setting (the tiers can interact)
    R.staged.stage = 'Tier 1 check from the final setting';
    for (const x of I.filter(y => y.tier === 1)) {
      const vals = (axes.find(a => a[0] === x.k) || [, []])[1];
      const rs = await runAll(vals.map(v => ({ ...cur, [x.k]: v })));
      let bi = -1; rs.forEach((r, j) => { if (better(r) && (bi < 0 || gt(r.train, rs[bi].train))) bi = j; });
      if (bi >= 0) { cur = { ...cur, [x.k]: vals[bi] }; curR = rs[bi]; }
    }
    stageRow('Final', cur, curR);
    setProg(`Staged optimizer done: ${R.staged.runs} backtests in ${((Date.now() - t0) / 1000).toFixed(0)}s`);
  } catch (e) { setProg(e.message === 'stopped' ? `Stopped after ${R.staged.runs} backtests` : '⚠ ' + e.message); }
  finally {
    // the stage results as optimizer rows (To chart / ❄), best first
    const keys = axes.map(([k]) => k);
    R.opt = { rows: R.staged.stages.slice().reverse().map(sg => ({ values: { ...base0, ...sg.st }, changed: Object.fromEntries(keys.map(k => [k, sg.st[k] ?? base0[k]])), train: sg.r.train, test: sg.r.test, all: sg.r.all, stage: sg.name })), done: R.staged.runs, space: R.staged.runs, mode: 'staged' };
    R.running = false; mountResearch(root);
  }
}
const titleOf = k => (R.study.inputs.find(i => String(i.idx) === String(k)) || {}).title || String(k);
const short = t => { const x = String(t).split(/[:(]/)[0].trim(); return x.length > 22 ? x.slice(0, 21) + '…' : x; };
function renderStaged() {
  const el = root && root.querySelector('#rsStg'), S = R.staged; if (!el || !S) return;
  const I = S.infl.slice().sort((a, b) => b.spread - a.spread), top = I[0] ? I[0].spread || 1 : 1, t2 = v => Number.isFinite(v) ? v.toFixed(2) : '—';
  const tierName = t => t === 1 ? 'Tier 1' : t === 2 ? 'Tier 2' : t === 3 ? 'Tier 3' : t === 0 ? 'no effect' : '…';
  el.innerHTML = `<div class="tree-sec">Staged optimizer · ${S.runs} backtests${R.running ? ' · ' + esc(S.stage) : ''}</div>
    <p class="note" style="margin-top:0">Influence = how far the train score (${esc(OBJ[R.objective][0])}) moves when only that input changes, the others at the current setting. The most influential are optimized together first, then the next tier, then the rest one at a time. Judge the result by the test columns: they are never used to choose.</p>
    ${I.length ? `<div class="rs-optres" style="max-height:300px"><table class="rs-or stg"><thead><tr><th class="t">Input</th><th class="t">Influence (train)</th><th>Test moves</th><th>Now</th><th>Best alone</th><th class="t">Tier</th></tr></thead><tbody>
      ${I.map(x => `<tr><td class="t" title="${esc(x.title)}">${esc(short(x.title))}</td><td class="t"><span class="bar" style="width:${Math.max(2, x.spread / top * 140)}px"></span> ${t2(x.spread)}</td><td>${t2(x.testSpread)}</td><td>${esc(String(x.cur))}</td><td>${esc(String(x.best))}</td><td class="t"><span class="tier t${x.tier ?? 'x'}">${tierName(x.tier)}</span></td></tr>`).join('')}</tbody></table></div>` : ''}
    ${S.stages.length ? `<table class="tbl rs-res" style="margin-top:8px"><tr><th class="t">Stage</th><th>Train trades</th><th>Train t</th><th class="t">Test trades</th><th>Test net</th><th>Test t</th></tr>
      ${S.stages.map(sg => `<tr><td class="t">${esc(sg.name)}</td><td>${sg.r.train.n}</td><td>${t2(sg.r.train.tDay)}</td><td class="t">${sg.r.test.n}</td><td class="${cls(sg.r.test.net)}">${money(sg.r.test.net, 0)}</td><td>${t2(sg.r.test.tDay)}</td></tr>`).join('')}</table>` : ''}`;
}

const total = axes => axes.length ? axes.reduce((n, [, v]) => n * v.length, 1) : 0;
function decode(axes, idx) { const o = {}; for (let a = axes.length - 1; a >= 0; a--) { const [k, v] = axes[a]; o[k] = v[idx % v.length]; idx = Math.floor(idx / v.length); } return o; }
function digits(axes, idx) { const d = new Array(axes.length); for (let a = axes.length - 1; a >= 0; a--) { const L = axes[a][1].length; d[a] = idx % L; idx = Math.floor(idx / L); } return d; }
function encode(axes, d) { let i = 0; for (let a = 0; a < axes.length; a++) i = i * axes[a][1].length + d[a]; return i; }
const regimeFor = c => ({ vol: c.__vol || R.regime.vol, trend: c.__trend || R.regime.trend });
const skipFor = c => c.__skip && c.__skip !== 'all days' ? [...new Set([...R.skip, c.__skip.slice(3) + '|*'])] : R.skip;
function combos() {
  const el = root && root.querySelector('#rsCombos'); if (!el) return; const n = total(axesOf());
  if (!Object.keys(R.sel).length && n) { el.textContent = `(all inputs: ${R.forever ? 'searches until stopped' : 'searches ' + R.budget.toLocaleString()})`; return; }
  el.textContent = !n ? '' : n <= R.budget && !R.forever ? `(${n.toLocaleString()} runs, all)` : `(${n.toLocaleString()} combinations: ${R.forever ? 'searches until stopped' : 'searches ' + R.budget.toLocaleString()})`;
}
async function runOptimize() {
  const axes = axesOf(), N = total(axes); if (!N) { toast('This strategy has no inputs to optimize'); return; }
  const full = N <= R.budget && !R.forever, budget = full ? N : R.forever ? Infinity : Math.max(10, R.budget);
  const base = R.study.inputs.reduce((o, i) => (o[i.idx] = i.value, o), {});
  // a setting = one index per axis ("digits"); the current inputs are the start point
  const startD = axes.map(([k, v]) => { const cur = k.startsWith('__') ? v[0] : base[k]; const j = v.findIndex(x => x === cur); return j >= 0 ? j : Math.floor(v.length / 2); });
  R.running = true; R.cancel = false; R.opt = { rows: [], total: budget, done: 0, space: N, mode: full ? 'grid' : 'search' }; mountResearch(root);
  try {
    const data = await loadBars(), n = Math.max(1, Math.min(6, (navigator.hardwareConcurrency || 4) - 1)), ws = workers(n); await prime(ws, data);
    const msg = baseMsg(data), t0 = Date.now(), seen = new Set(), queue = [], f = OBJ[R.objective][1];
    const push = d => { const k = d.join(','); if (seen.has(k)) return false; seen.add(k); queue.push(d); return true; };
    const rand = () => axes.map(([, v]) => Math.floor(Math.random() * v.length));
    // every combination when they fit; otherwise the current setting, its neighbours, random settings, then (repeatedly)
    // the neighbours of the best ones on train - the same search as the journal's Research tab
    if (full) { for (let i = 0; i < N; i++) push(digits(axes, i)); }
    else { push(startD); for (let a = 0; a < axes.length; a++) for (const s of [-1, 1]) { const e = startD.slice(); e[a] += s; if (e[a] >= 0 && e[a] < axes[a][1].length) push(e); } for (let g = 0; g < 400 && queue.length < 120; g++) push(rand()); }
    const refill = () => {
      if (full || R.opt.done + queue.length >= budget) return;
      const ok = R.opt.rows.filter(r => passes(r.train)).sort((a, b) => f(b.train) - f(a.train)).slice(0, 10);
      const top = ok.length ? ok : R.opt.rows.slice().sort((a, b) => f(b.train) - f(a.train)).slice(0, 5);
      for (const r of top) for (let a = 0; a < axes.length; a++) for (const s of [-1, 1, -2, 2]) {
        const e = r.d.slice(); e[a] += s; if (e[a] < 0 || e[a] >= axes[a][1].length) continue;
        push(e); if (R.opt.done + queue.length >= budget) return;
      }
      for (let g = 0; g < 200 && queue.length < 30; g++) push(rand());                      // keep exploring
      if (!queue.length && seen.size >= N) R.cancel = true;                                  // every combination tried
    };
    const decodeD = d => Object.fromEntries(axes.map(([k, v], a) => [k, v[d[a]]]));
    let started = 0;
    await Promise.all(ws.map(async (w, wi) => {
      while (!R.cancel && started < budget) {
        if (!queue.length) refill();
        if (!queue.length) { if (started > R.opt.done) { await new Promise(r => setTimeout(r, 30)); continue; } break; }
        const d = queue.shift(); started++;
        const changed = decodeD(d), values = { ...base, ...changed }; delete values.__skip; delete values.__vol; delete values.__trend;
        const skip = skipFor(changed), regime = regimeFor(changed);
        const r = await job(w, { ...msg, id: `o${wi}_${started}`, inputs: values, strategy: { ...msg.strategy, skip: skip.length ? skip : undefined, regime: regimeOn(regime) ? regime : undefined } });
        R.opt.done++;
        if (r.error) { R.opt.errors = (R.opt.errors || 0) + 1; R.opt.lastError = r.error; }
        if (!r.error) R.opt.rows.push({ d, values: { ...values, __skip: skip, __regime: regime }, changed, train: r.train, test: r.test, all: r.all });
        if (R.opt.done % 5 === 0) {
          const el = (Date.now() - t0) / R.opt.done;
          setProg(Number.isFinite(budget) ? `Optimizing ${R.opt.done.toLocaleString()} / ${budget.toLocaleString()}${full ? '' : ' (searching)'} · ~${Math.ceil(el * (budget - R.opt.done) / 1000)}s left`
            : `${R.opt.done.toLocaleString()} settings tried · ${(el * n / 1000).toFixed(2)}s each · running · searching`);
        }
        if (R.opt.done % 25 === 0) { sortOpt(); renderOpt(); }
      }
    }));
    sortOpt(); setProg(`${R.cancel ? 'Stopped' : 'Done'}: ${R.opt.done.toLocaleString()} settings tried in ${((Date.now() - t0) / 1000).toFixed(0)}s${R.opt.errors ? ` · ${R.opt.errors} failed (${R.opt.lastError})` : ''}`);
  } catch (e) { setProg('⚠ ' + e.message); }
  finally { R.running = false; mountResearch(root); }
  return R.opt;
}
const passes = m => m.n >= R.minTrades && m.monthShare <= R.maxMonth;
function sortOpt() { const f = OBJ[R.objective][1]; R.opt.rows.sort((a, b) => passes(b.train) - passes(a.train) || f(b.train) - f(a.train)); }
function renderOpt() {
  const el = root && root.querySelector('#rsOpt'); if (!el || !R.opt || !R.opt.rows.length) return;
  const ins = R.study.inputs, keys = Object.keys(R.opt.rows[0].changed), f = OBJ[R.objective];
  const title = k => ({ __skip: 'Weekday', __vol: 'Volatility', __trend: 'Trend' })[k] || (ins.find(i => String(i.idx) === String(k)) || {}).title || String(k);
  const short = k => { const t = title(k).split(/[:(]/)[0].trim(); return t.length > 18 ? t.slice(0, 17) + '…' : t; };
  const t2 = v => Number.isFinite(v) ? v.toFixed(2) : '—';
  // many inputs: one "Settings" column listing what differs from the current inputs (like the Research tab's "show")
  const wide = keys.length > 4, cur = R.study.inputs.reduce((o, i) => (o[i.idx] = i.value, o), {});
  const diffs = r => { const ch = keys.filter(k => !k.startsWith('__') ? r.changed[k] !== cur[k] : !['all days', 'all', 'any'].includes(r.changed[k]));
    return ch.length ? `<details><summary>${ch.length} changed</summary>${ch.map(k => `<div><span class="dim">${esc(title(k))}</span> <b>${esc(String(r.changed[k]))}</b>${k.startsWith('__') ? '' : ` <span class="dim">(was ${esc(String(cur[k]))})</span>`}</div>`).join('')}</details>` : '<span class="dim">current settings</span>'; };
  el.innerHTML = `<div class="tree-sec">Optimization · ${R.opt.rows.length.toLocaleString()} runs${R.opt.mode === 'search' ? ` (search of ${R.opt.space.toLocaleString()} combinations)` : ''}</div>
    <p class="note" style="margin-top:0">Each run is a full backtest. Ranked by <b>${esc(f[0])}</b> on the <b>train</b> part only, ${R.minTrades}+ trades, no month over ${R.maxMonth}% of the profit (rows that miss these are greyed, at the bottom). The <b>test</b> part is never used to choose: a row good on both is the one to try.</p>
    <div class="rs-optres"><table class="rs-or">
      <thead><tr><th rowspan="2" class="c">#</th><th rowspan="2">Score</th><th colspan="4" class="grp">Train ${R.split}%</th><th colspan="5" class="grp tst">Test ${100 - R.split}%</th><th rowspan="2">Max DD</th>
        ${wide ? '<th rowspan="2" class="t">Settings</th>' : keys.map(k => `<th rowspan="2" class="in" title="${esc(title(k))}">${esc(short(k))}</th>`).join('')}<th rowspan="2"></th></tr>
        <tr><th>Trades</th><th>Win</th><th>Avg</th><th>t</th><th class="tst">Trades</th><th class="tst">Win</th><th class="tst">Avg</th><th class="tst">Net</th><th class="tst">t</th></tr></thead>
      <tbody>${R.opt.rows.slice(0, 60).map((r, k) => { const ok = passes(r.train), good = ok && r.test.net > 0 && r.test.tDay > 1;
        return `<tr class="${ok ? '' : 'miss'} ${good ? 'both' : ''}">
        <td class="c dim">${k + 1}</td><td><b>${t2(f[1](r.train))}</b></td>
        <td class="${r.train.n < R.minTrades ? 'dn' : ''}">${r.train.n}</td><td>${r.train.winRate.toFixed(0)}%</td><td class="${cls(r.train.avg)}">${money(r.train.avg, 0)}</td><td>${t2(r.train.tDay)}</td>
        <td class="tst">${r.test.n}</td><td class="tst">${r.test.winRate.toFixed(0)}%</td><td class="tst ${cls(r.test.avg)}">${money(r.test.avg, 0)}</td><td class="tst ${cls(r.test.net)}"><b>${money(r.test.net, 0)}</b></td><td class="tst ${r.test.tDay >= 2 ? 'up' : r.test.tDay < 0 ? 'dn' : ''}">${t2(r.test.tDay)}</td>
        <td class="dn">${money(-r.all.maxDD, 0)}</td>
        ${wide ? `<td class="t set">${diffs(r)}</td>` : keys.map(x => `<td class="in">${esc(String(r.changed[x]))}</td>`).join('')}
        <td class="act"><button class="btn sm" data-apply="${k}" title="Put these settings on the chart">To chart</button><button class="btn sm" data-freeze="${k}" title="Freeze with these settings and paper trade it">❄</button></td></tr>`; }).join('')}</tbody></table></div>
    ${!wide && keys.some(k => short(k) !== title(k)) ? `<div class="rs-legend">${keys.filter(k => short(k) !== title(k)).map(k => `<span><b>${esc(short(k))}</b> ${esc(title(k))}</span>`).join('')}</div>` : ''}
    <p class="note">t = the t-statistic of the daily P&L (2+ is unlikely to be luck). Green edge: passes on train and the test t is above 1.</p>`;
}
const pf = v => Number.isFinite(v) ? v.toFixed(2) : '∞';

// ------------------------------------------------------------------------------------------------ back to the chart
async function applyInputs(values) {
  const tab = W.tab();
  if (tab.symbol !== R.symbol) await W.setSymbol(R.symbol);
  if (W.tab().tf !== R.tf) await W.setTf(R.tf);
  let ind = R.indicatorId && W.tab().indicators.find(i => i.id === R.indicatorId);
  if (!ind || W.scriptFor(ind).src !== R.src) { ind = W.addIndicator({ src: R.src, name: R.name }); R.indicatorId = ind.id; }
  const skip = values.__skip !== undefined ? values.__skip : R.skip, regime = values.__regime || R.regime;
  const ins = { ...values }; delete ins.__skip; delete ins.__regime;
  W.updateIndicator(ind.id, { inputs: ins, skip: skip && skip.length ? [...skip] : null, regime: regimeOn(regime) ? { ...regime } : null });
  toast(`Sent to the chart (${R.symbol} ${R.tf})${skip && skip.length ? ` · no entries ${skipText(skip)}` : ''}${regimeOn(regime) ? ` · only ${regimeText(regime)}` : ''}`, 'ok');
}

// for Quant: run a study / optimization and return a compact summary
export async function quantStudy(p) {
  if (p.source) Object.assign(R, { src: p.source, name: p.name || 'Quant strategy', indicatorId: null });
  else pickDefault();
  if (!/^\s*strategy\s*\(/m.test(R.src)) return { error: 'The script is not a strategy() (use strategy.entry / strategy.exit).' };
  Object.assign(R, { symbol: (p.symbol || W.tab().symbol).toUpperCase(), tf: W.normTfStrict(p.timeframe || W.tab().tf) || W.tab().tf, days: p.days || 500 });
  if (p.commission != null) R.com = +p.commission; if (p.slippage_ticks != null) R.slip = +p.slippage_ticks;
  if (W.currentSide() !== 'research') W.openSide('research', true);
  const st = await runStudy(); if (!st || st.error) return { error: (st && st.error) || 'study failed' };
  const out = { symbol: R.symbol, timeframe: R.tf, from: fmtD(st.from), to: fmtD(st.to), bars: st.n, all: slim(st.all), train: slim(st.train), test: slim(st.test), inputs: st.inputs.map(i => ({ title: i.title, value: i.value })) };
  if (p.optimize && p.optimize.length) {
    R.sel = {};
    for (const o of p.optimize) { const i = st.inputs.find(x => x.title.toLowerCase() === String(o.input).toLowerCase()); if (i) R.sel[i.idx] = { from: +o.from, to: +o.to, step: +o.step || 1 }; }
    if (p.objective && OBJ[p.objective]) R.objective = p.objective;
    mountResearch(root);
    const op = await runOptimize();
    out.optimization = (op.rows || []).slice(0, 10).map(r => ({ inputs: Object.fromEntries(Object.entries(r.changed).map(([k, v]) => [(st.inputs.find(i => String(i.idx) === k) || {}).title || k, v])), train: slim(r.train), test: slim(r.test) }));
  }
  return out;
}
const slim = m => ({ trades: m.n, net: +m.net.toFixed(2), win_rate: +m.winRate.toFixed(1), profit_factor: Number.isFinite(m.pf) ? +m.pf.toFixed(2) : null, avg_trade: +m.avg.toFixed(2), max_drawdown: +m.maxDD.toFixed(2), sharpe: +m.sharpe.toFixed(2), long: m.long, short: m.short });
export async function quantApply(values) {
  if (!R.study) return { error: 'Run a study first' };
  const v = { ...R.study.inputs.reduce((o, i) => (o[i.idx] = i.value, o), {}) };
  for (const [t, x] of Object.entries(values || {})) { const i = R.study.inputs.find(z => z.title.toLowerCase() === t.toLowerCase()); if (i) v[i.idx] = x; }
  await applyInputs(v); return { ok: true };
}

// ------------------------------------------------------------------------------------------------ freeze → paper
function freezeForm(row) {
  if (!R.study) return;
  const st = R.study, ins = st.inputs;
  const v0 = row == null ? ins.reduce((o, i) => (o[i.idx] = i.value, o), {}) : R.opt.rows[row].values;
  const values = { ...v0 }, skip = values.__skip !== undefined ? values.__skip : R.skip, regime = values.__regime || R.regime; delete values.__skip; delete values.__regime;
  const res = row == null ? st : R.opt.rows[row];
  const d = R.cache.values().next().value || {};
  freezeDialog({ src: R.src, name: R.name, inputs: values, symbol: R.symbol, tf: R.tf, com: R.com, slip: R.slip, sec: d.sec, skip, regime: regimeOn(regime) ? regime : null,
    labels: Object.fromEntries(ins.map(i => [i.title, values[i.idx]])),
    research: { days: R.days, from: st.from, to: st.to, split: R.split, train: slim(res.train), test: slim(res.test) } });
}
void pct;
