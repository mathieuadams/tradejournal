// Workspace shell: tabs, data feed (history + live stream), chart wiring, toolbar, drawing tools, replay, paper trading glue.
import { Chart, newId, nyParts, fmtPrice } from './chart.js';
import { compile, run, tfSeconds } from './pine.js';
import { LIBRARY } from './library.js';
import { W } from './ctx.js';
import { $, $$, h, esc, api, toast, debounce, throttle, popover, closePopover, popAt, confirmDialog, money, priceDigits, nyNow, modal, closeModal } from './util.js';
import { initPanels, openSide, toggleBottom, indicatorSettings } from './panels.js';
import { openLibrary, openSymbolSearch, openTfPopover } from './dialogs.js';
import { initEditor, openInEditor } from './editor.js';
import { initChat, focusChat } from './chat.js';

const DEFAULT = () => ({
  v: 1,
  tabs: [
    { id: 't1', symbol: 'PLTR', tf: '1h', type: 'candles', indicators: [{ id: 'i1', scriptId: 'volume' }, { id: 'i2', scriptId: 'smc' }] },
    { id: 't2', symbol: 'AAPL', tf: '1h', type: 'candles', indicators: [{ id: 'i3', scriptId: 'volume' }, { id: 'i4', scriptId: 'ema' }] },
    { id: 't3', symbol: 'TSLA', tf: '1h', type: 'candles', indicators: [{ id: 'i5', scriptId: 'volume' }] },
  ],
  active: 't1', watchlist: ['NVDA', 'AAPL', 'TSLA', 'MSFT', 'AMZN', 'META', 'GOOG', 'AMD', 'PLTR', 'SPY', 'QQQ', 'BTC/USD'],
  scripts: [], favorites: ['smc', 'vwap', 'rsi'], drawings: {}, chatOpen: true, side: 'watchlist', bottom: false, sees: true, qty: 1, quicktrade: true, magnet: false,
});

// ------------------------------------------------------------------------------------------------ state
W.tab = () => W.state.tabs.find(t => t.id === W.state.active) || W.state.tabs[0];
const saveRemote = debounce(() => { api('state', W.state, 'PUT').catch(() => {}); }, 900);
W.save = () => { try { localStorage.setItem('ws-state', JSON.stringify(W.state)); } catch { /* full */ } saveRemote(); };
async function loadState() {
  let s = null;
  try { s = await api('state'); if (!s || !s.tabs) s = null; } catch { s = null; }
  if (!s) { try { s = JSON.parse(localStorage.getItem('ws-state') || 'null'); } catch { s = null; } }
  const d = DEFAULT(); s = s && s.tabs && s.tabs.length ? { ...d, ...s } : d;
  if (!s.tabs.find(t => t.id === s.active)) s.active = s.tabs[0].id;
  return s;
}

// ------------------------------------------------------------------------------------------------ series (history + live)
const key = (sym, tf) => sym + '|' + tf;
function unitOf(tf) { const m = String(tf).match(/^(\d*)([smhDWM])$/); return m ? { n: +(m[1] || 1), u: m[2] } : { n: 1, u: 'h' }; }
const WD = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
function bucketKey(S, t) {
  const { n, u } = S.unit;
  if (u === 'm' || u === 'h' || u === 's') return { k: t - (t % S.tfSec) };
  const p = nyParts(t), mid = t - (p.h * 3600 + p.mi * 60);
  if (u === 'D') return { k: mid, id: `${p.y}-${p.mon}-${p.d}`, n };
  if (u === 'W') return { k: mid - WD[p.wd] * 86400, id: 'w' + Math.round((mid - WD[p.wd] * 86400) / 86400) };
  return { k: mid - (p.d - 1) * 86400, id: `${p.y}-${p.mon}` };
}
W.getSeries = (sym, tf) => {
  const k = key(sym, tf); let S = W.series.get(k);
  if (!S) { S = { sym, tf, tfSec: tfSeconds(tf), unit: unitOf(tf), bars: [], more: true, loading: false, promise: null, err: null }; W.series.set(k, S); }
  return S;
};
W.loadSeries = (sym, tf) => {
  const S = W.getSeries(sym, tf);
  if (S.bars.length) return Promise.resolve(S);
  if (S.promise) return S.promise;
  S.promise = api(`bars?symbol=${encodeURIComponent(sym)}&tf=${encodeURIComponent(tf)}&limit=2000`).then(j => {
    S.bars = j.bars.map(b => ({ t: b[0], o: b[1], h: b[2], l: b[3], c: b[4], v: b[5] })); S.more = j.more; S.name = j.name; S.err = null; S.liveMin = null;
    return S;
  }).catch(e => { S.err = e.message; S.promise = null; throw e; });
  return S.promise;
};
async function loadMore() {
  const tab = W.tab(), S = W.getSeries(tab.symbol, tab.tf);
  if (!S.more || S.loading || !S.bars.length || W.replay != null) return;
  S.loading = true; W.chart.loadingMore = true; W.chart.req();
  try {
    const j = await api(`bars?symbol=${encodeURIComponent(S.sym)}&tf=${encodeURIComponent(S.tf)}&limit=1500&before=${S.bars[0].t}`);
    const older = j.bars.map(b => ({ t: b[0], o: b[1], h: b[2], l: b[3], c: b[4], v: b[5] })).filter(b => b.t < S.bars[0].t);
    S.more = j.more && older.length > 0;
    if (older.length && W.tab() === tab && key(tab.symbol, tab.tf) === key(S.sym, S.tf)) { S.bars = older.concat(S.bars); W.chart.bars = S.bars; W.chart.prepend(older.length); recompute(); }
    else if (older.length) S.bars = older.concat(S.bars);
  } catch (e) { toast('History: ' + e.message, 'err'); S.more = false; }
  finally { S.loading = false; W.chart.loadingMore = false; W.chart.req(); }
}
function mergeLive(S, b) {
  const [t, o, hh, ll, c, v] = b, bars = S.bars; if (!bars.length) return false;
  let add;
  if (!S.liveMin) { S.liveMin = { t, v }; add = 0; }
  else if (t === S.liveMin.t) { add = Math.max(0, v - S.liveMin.v); S.liveMin.v = v; }
  else if (t > S.liveMin.t) { add = v; S.liveMin = { t, v }; }
  else return false;
  const last = bars[bars.length - 1], bk = bucketKey(S, t), lk = bucketKey(S, last.t);
  const same = bk.id ? (bk.id === lk.id || (bk.n > 1 && t - last.t < bk.n * 86400)) : bk.k === last.t;
  if (same) { last.h = Math.max(last.h, hh); last.l = Math.min(last.l, ll); last.c = c; last.v += add; return 'update'; }
  if (bk.k > last.t) { bars.push({ t: bk.k, o, h: hh, l: ll, c, v: add || v }); return 'new'; }
  return false;
}

// ------------------------------------------------------------------------------------------------ indicators
const compiled = new Map();
W.scriptFor = ind => {
  if (ind.src) return { src: ind.src, name: ind.name || 'Script' };
  const lib = LIBRARY.find(s => s.id === ind.scriptId); if (lib) return { src: lib.src, name: lib.name, builtin: true };
  const mine = W.state.scripts.find(s => s.id === ind.scriptId); if (mine) return { src: mine.src, name: mine.name, mine: true };
  return { src: '//@version=6\nindicator("Missing script")\n', name: 'Missing script' };
};
function getCompiled(src) {
  let c = compiled.get(src);
  if (!c) { try { c = { ok: compile(src) }; } catch (e) { c = { err: { message: e.message, line: e.line || 0 } }; } compiled.set(src, c); if (compiled.size > 80) compiled.delete(compiled.keys().next().value); }
  return c;
}
W.runScript = (src, bars, inputs, tab = W.tab()) => {
  const c = getCompiled(src);
  if (c.err) return { meta: { title: 'Script', overlay: true }, inputs: [], plots: [], shapes: [], hlines: [], fills: [], bgcolors: [], barcolors: [], lines: [], labels: [], boxes: [], logs: [], error: c.err };
  return run(c.ok, bars, { inputs, symbol: tab.symbol, tf: tab.tf, tfSec: tfSeconds(tab.tf), live: W.replay == null });
};
function activeBars() { const t = W.tab(), S = W.getSeries(t.symbol, t.tf); return W.replay != null ? S.bars.slice(0, W.replay + 1) : S.bars; }
W.activeBars = activeBars;
function recompute() {
  const tab = W.tab(), bars = activeBars(); if (!bars.length) { W.indOut = []; W.chart.setIndicators([]); return; }
  W.indOut = tab.indicators.map(ind => {
    const sc = W.scriptFor(ind); let out;
    try { out = W.runScript(sc.src, bars, ind.inputs || null, tab); } catch (e) { out = { meta: { title: sc.name, overlay: true }, inputs: [], plots: [], shapes: [], hlines: [], fills: [], bgcolors: [], barcolors: [], lines: [], labels: [], boxes: [], logs: [], error: { message: e.message } }; }
    if (out.error && !out.meta.title) out.meta.title = sc.name;
    if (out.error && out.meta.title === 'Script') out.meta.title = sc.name;
    return { id: ind.id, title: sc.name, visible: ind.visible !== false, out };
  });
  W.chart.setIndicators(W.indOut); W.emit('indicators');
}
W.recompute = recompute;
const recomputeLive = throttle(recompute, 1000);
W.addIndicator = (spec) => {
  const tab = W.tab(); const ind = { id: newId('i'), ...spec }; tab.indicators.push(ind); W.save(); recompute(); return ind;
};
W.removeIndicator = id => { const tab = W.tab(); tab.indicators = tab.indicators.filter(i => i.id !== id); W.save(); recompute(); };
W.updateIndicator = (id, patch) => { const ind = W.tab().indicators.find(i => i.id === id); if (!ind) return; Object.assign(ind, patch); W.save(); recompute(); };

// ------------------------------------------------------------------------------------------------ drawings + undo
W.drawings = (sym = W.tab().symbol) => (W.state.drawings[sym] = W.state.drawings[sym] || []);
const undo = { past: [], future: [] };
W.pushUndo = () => { const s = W.tab().symbol; undo.past.push({ s, d: JSON.stringify(W.drawings(s)) }); if (undo.past.length > 80) undo.past.shift(); undo.future = []; };
function restoreDrawings(snap, to) { const s = W.tab().symbol; to.push({ s, d: JSON.stringify(W.drawings(s)) }); W.state.drawings[snap.s] = JSON.parse(snap.d); W.chart.setDrawings(W.drawings()); W.save(); W.emit('drawings'); }
function doUndo() { const s = undo.past.pop(); if (s) restoreDrawings(s, undo.future); }
function doRedo() { const s = undo.future.pop(); if (s) restoreDrawings(s, undo.past); }
W.setDrawings = (list, sym = W.tab().symbol) => { W.state.drawings[sym] = list; if (sym === W.tab().symbol) W.chart.setDrawings(list); W.save(); W.emit('drawings'); };
W.addDrawings = (list, sym = W.tab().symbol) => { W.pushUndo(); W.setDrawings([...W.drawings(sym), ...list], sym); };

// ------------------------------------------------------------------------------------------------ tabs
function renderTabs() {
  const el = $('#tabs'); el.innerHTML = '';
  for (const t of W.state.tabs) {
    const q = W.quotes[t.symbol];
    const n = h(`<div class="tab ${t.id === W.state.active ? 'on' : ''}" data-id="${t.id}"><b>${esc(t.symbol)}</b><small>${esc(t.tf)}</small>${W.state.tabs.length > 1 ? '<span class="x" title="Close tab">✕</span>' : ''}</div>`);
    n.addEventListener('click', e => { if (e.target.closest('.x')) { closeTab(t.id); return; } activate(t.id); });
    el.appendChild(n); void q;
  }
}
function closeTab(id) {
  const i = W.state.tabs.findIndex(t => t.id === id); if (i < 0 || W.state.tabs.length < 2) return;
  W.state.tabs.splice(i, 1); if (W.state.active === id) W.state.active = W.state.tabs[Math.max(0, i - 1)].id; W.save(); activate(W.state.active);
}
W.newTab = (symbol = W.tab().symbol, tf = W.tab().tf) => { const t = { id: newId('t'), symbol, tf, type: 'candles', indicators: [{ id: newId('i'), scriptId: 'volume' }] }; W.state.tabs.push(t); W.save(); activate(t.id); return t; };

let loadSeq = 0;
async function activate(id, { keepView = false } = {}) {
  if (W.replay != null) exitReplay();
  W.state.active = id; W.save(); renderTabs();
  const tab = W.tab(), seq = ++loadSeq, S = W.getSeries(tab.symbol, tab.tf);
  $('#symLabel').textContent = tab.symbol; $('#tfLabel').textContent = tab.tf; document.title = `${tab.symbol} · ${tab.tf} — Workspace`;
  const msg = $('#chartMsg');
  if (!S.bars.length) { msg.hidden = false; msg.textContent = `Loading ${tab.symbol} ${tab.tf}…`; }
  try { await W.loadSeries(tab.symbol, tab.tf); }
  catch (e) { if (seq === loadSeq) { msg.hidden = false; msg.textContent = `${tab.symbol}: ${e.message}`; } return; }
  if (seq !== loadSeq) return;
  msg.hidden = !!S.bars.length; if (!S.bars.length) msg.textContent = `No data for ${tab.symbol} ${tab.tf}.`;
  const last = S.bars[S.bars.length - 1];
  W.chart.setSymbol(tab.symbol, tab.tf, S.tfSec, priceDigits(last && last.c));
  W.chart.setType(tab.type || 'candles');
  W.chart.setBars(S.bars, { fit: !keepView, keepView });
  W.chart.setDrawings(W.drawings());
  recompute(); orderLines(); resubscribe(); W.emit('tab', tab);
}
W.activate = activate;
W.setSymbol = async (sym) => { const tab = W.tab(); sym = sym.toUpperCase(); if (tab.symbol === sym) return; tab.symbol = sym; W.save(); await activate(tab.id); };
W.setTf = async (tf) => {
  const tab = W.tab(); tf = normTf(tf); if (!tf) { toast('Unknown timeframe', 'err'); return false; }
  if (tab.tf === tf) return true; tab.tf = tf; W.save(); await activate(tab.id); return true;
};
W.setType = t => { W.tab().type = t; W.save(); W.chart.setType(t); };
function normTf(s) {
  s = String(s).trim(); let m = s.match(/^(\d+)\s*(s|sec|m|min|mins|minutes?)?$/i);
  if (m) return m[2] && /^s/i.test(m[2]) ? null : `${m[1]}m`;
  m = s.match(/^(\d*)\s*(h|hr|hour|hours|H)$/); if (m) return `${m[1] || 1}h`;
  m = s.match(/^(\d*)\s*(d|D|day|days)$/); if (m) return `${m[1] || 1}D`;
  m = s.match(/^(\d*)\s*(w|W|wk|week|weeks)$/); if (m) return `${m[1] || 1}W`;
  m = s.match(/^(\d*)\s*(M|mo|mon|month|months)$/); if (m) return `${m[1] || 1}M`;
  return null;
}
W.normTf = normTf;
function minuteToTf(tf) { const m = tf.match(/^(\d+)m$/); if (m && +m[1] >= 60 && +m[1] % 60 === 0) return `${+m[1] / 60}h`; return tf; }
W.normTfStrict = s => { const t = normTf(s); return t && minuteToTf(t); };

// ------------------------------------------------------------------------------------------------ live stream
let es = null, esSyms = '';
function wantedSymbols() {
  const s = new Set(W.state.tabs.map(t => t.symbol)); W.state.watchlist.forEach(x => s.add(x));
  if (W.account) { W.account.positions.forEach(p => s.add(p.symbol)); W.account.orders.forEach(o => s.add(o.symbol)); }
  return [...s].sort();
}
const resubscribe = debounce(() => {
  const syms = wantedSymbols().join(','); if (syms === esSyms && es && es.readyState !== 2) return;
  esSyms = syms; if (es) es.close();
  es = new EventSource('/api/stream?symbols=' + encodeURIComponent(syms));
  es.addEventListener('bar', e => onBar(JSON.parse(e.data)));
  es.addEventListener('paper', e => onPaper(JSON.parse(e.data)));
  es.onerror = () => { $('#dataPill').classList.add('warn'); };
  es.onopen = () => { $('#dataPill').classList.remove('warn'); };
}, 250);
W.resubscribe = resubscribe;
function onBar({ s, b }) {
  const tab = W.tab(); let activeChange = false;
  for (const S of W.series.values()) {
    if (S.sym !== s || !S.bars.length) continue;
    const r = mergeLive(S, b);
    if (r && S.sym === tab.symbol && S.tf === tab.tf && W.replay == null) activeChange = r;
  }
  const q = W.quotes[s] || (W.quotes[s] = { name: s }); const prevLast = q.last; q.last = b[4];
  if (activeChange) { W.chart.liveUpdate(activeChange === 'new'); if (activeChange === 'new') recompute(); else recomputeLive(); }
  const a = W.account, pos = a && a.positions.find(p => p.symbol === s);
  if (pos) { pos.last = b[4]; pos.pnl = (pos.last - pos.avg) * pos.qty; pos.value = pos.qty * pos.last; pos.pnl_pct = (pos.last / pos.avg - 1) * 100 * Math.sign(pos.qty); markAccount(); }
  W.emit('quote', { s, last: b[4], prev: prevLast });
}
const markAccount = throttle(() => {
  const a = W.account; if (!a) return;
  a.open_pnl = a.positions.reduce((x, p) => x + p.pnl, 0); a.margin_used = a.positions.reduce((x, p) => x + Math.abs(p.value), 0);
  const eq = a.cash + a.positions.reduce((x, p) => x + p.value, 0); a.today_pnl += eq - a.equity; a.equity = eq; a.buying_power = Math.max(0, eq - a.margin_used);
  orderLines(); W.emit('account', a);
}, 700);
function onPaper({ event, account }) {
  W.account = account; orderLines(); W.emit('account', account);
  if (event && event.type === 'fill') { const f = account.fills[0]; if (f) toast(`Filled ${f.side.toUpperCase()} ${f.qty} ${f.symbol} @ ${fmtPrice(f.price, priceDigits(f.price))}${f.realized ? ` · P&L ${money(f.realized)}` : ''}`, 'ok'); }
  resubscribe();
}
async function refreshQuotes() {
  const syms = wantedSymbols(); if (!syms.length) return;
  try { const j = await api('quotes?symbols=' + encodeURIComponent(syms.join(','))); for (const [s, q] of Object.entries(j.quotes)) W.quotes[s] = { ...(W.quotes[s] || {}), ...q }; W.emit('quotes'); } catch { /* offline */ }
}
W.refreshQuotes = refreshQuotes;

// ------------------------------------------------------------------------------------------------ paper trading
W.placeOrder = async (req, { confirm } = {}) => {
  const ask = confirm ?? (W.account ? W.account.settings.ask_before : true);
  const desc = `${req.side === 'buy' ? 'Buy' : 'Sell'} ${req.qty} ${req.symbol} ${req.type === 'market' || !req.type ? 'at market' : `${req.type} @ ${req.price}`}${req.sl ? ` · SL ${req.sl}` : ''}${req.tp ? ` · TP ${req.tp}` : ''}`;
  if (ask && !(await confirmDialog('Place paper order?', `<p style="margin:0 0 6px"><b>${esc(desc)}</b></p><p class="note">Simulated account, no real money. Turn off “Ask before placing” in the order panel to skip this.</p>`, 'Place order', req.side === 'buy' ? 'buy' : 'sell'))) return null;
  try { const r = await api('paper/order', req); W.account = r.account; W.emit('account', r.account); orderLines(); if (r.order.status === 'working') toast(`Order working: ${desc}`); return r.order; }
  catch (e) { toast(e.message, 'err'); throw e; }
};
W.paper = async (action, body = {}) => { try { const r = await api('paper/' + action, body); W.account = r.account; W.emit('account', r.account); orderLines(); return r; } catch (e) { toast(e.message, 'err'); throw e; } };
function orderLines() {
  const a = W.account, sym = W.tab().symbol, out = []; if (!a) { W.chart.setOrderLines([]); return; }
  const prec = W.chart.prec;
  for (const p of a.positions.filter(p => p.symbol === sym)) out.push({ id: 'pos:' + sym, kind: 'position', price: p.avg, color: p.pnl >= 0 ? '#089981' : '#f23645', label: `${p.qty > 0 ? 'LONG' : 'SHORT'} ${Math.abs(p.qty)} · ${p.pnl >= 0 ? '+' : ''}${money(p.pnl)}` });
  for (const o of a.orders.filter(o => o.symbol === sym)) {
    if (o.price != null) out.push({ id: o.id, kind: 'order', price: o.price, draggable: true, cancelable: true, color: o.note === 'sl' ? '#f23645' : o.note === 'tp' ? '#089981' : '#2962ff', label: o.note === 'sl' ? `SL ${o.qty}` : o.note === 'tp' ? `TP ${o.qty}` : `${o.side.toUpperCase()} ${o.type.toUpperCase()} ${o.qty}` });
    if (!o.reduce_only && o.sl) out.push({ id: o.id + ':sl', kind: 'order', price: o.sl, draggable: true, color: '#f23645', label: `SL ${o.qty}` });
    if (!o.reduce_only && o.tp) out.push({ id: o.id + ':tp', kind: 'order', price: o.tp, draggable: true, color: '#089981', label: `TP ${o.qty}` });
  }
  void prec; W.chart.setOrderLines(out);
}
W.orderLines = orderLines;

// ------------------------------------------------------------------------------------------------ toolbar & tools
const TOOLS = [
  ['cursor', 'Cursor', '<path d="M5 3l14 8-6 2-2 6z"/>'],
  ['trend', 'Trend line', '<path d="M4 19L20 5"/><circle cx="4" cy="19" r="1.5"/><circle cx="20" cy="5" r="1.5"/>'],
  ['hline', 'Horizontal line', '<path d="M3 12h18"/><circle cx="12" cy="12" r="1.5"/>'],
  ['hray', 'Horizontal ray', '<path d="M8 12h13"/><circle cx="7" cy="12" r="1.8"/>'],
  ['vline', 'Vertical line', '<path d="M12 3v18"/>'],
  ['rect', 'Rectangle', '<rect x="4" y="6" width="16" height="12" rx="1"/>'],
  ['text', 'Text', '<path d="M5 6h14M12 6v13"/>'],
  ['measure', 'Measure (price / % / bars)', '<path d="M4 20L20 4M7 17l2 2M10 14l2 2M13 11l2 2M16 8l2 2"/>'],
];
function renderDrawbar() {
  const el = $('#drawbar'); el.innerHTML = '';
  const q = h(`<button class="quant" title="Ask Quant (AI)"><svg viewBox="0 0 24 24"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/></svg>Quant</button>`);
  q.onclick = () => { if (!W.state.chatOpen) toggleChat(); focusChat(); }; el.appendChild(q); el.appendChild(h('<div class="dsep"></div>'));
  for (const [id, title, path] of TOOLS) {
    const b = h(`<button data-tool="${id}" title="${title}"><svg viewBox="0 0 24 24">${path}</svg></button>`);
    b.onclick = () => setTool(W.chart.tool === id && id !== 'cursor' ? 'cursor' : id); el.appendChild(b);
  }
  el.appendChild(h('<div class="dsep"></div>'));
  const mag = h(`<button title="Magnet: snap to OHLC" data-x="magnet"><svg viewBox="0 0 24 24"><path d="M6 4v8a6 6 0 0012 0V4h-4v8a2 2 0 01-4 0V4z"/></svg></button>`);
  mag.onclick = () => { W.state.magnet = !W.state.magnet; W.chart.magnet = W.state.magnet; mag.classList.toggle('on', W.state.magnet); W.save(); };
  mag.classList.toggle('on', !!W.state.magnet); W.chart.magnet = !!W.state.magnet; el.appendChild(mag);
  const hide = h(`<button title="Hide / show drawings"><svg viewBox="0 0 24 24"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg></button>`);
  hide.onclick = () => { const d = W.drawings(); const any = d.some(x => !x.hidden); d.forEach(x => { x.hidden = any; }); W.setDrawings(d); hide.classList.toggle('on', any); };
  el.appendChild(hide);
  const del = h(`<button title="Remove all drawings on this symbol"><svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button>`);
  del.onclick = async () => { if (!W.drawings().length) return; if (await confirmDialog('Remove drawings', `Remove all ${W.drawings().length} drawings on ${esc(W.tab().symbol)}?`, 'Remove', 'sell')) { W.pushUndo(); W.setDrawings([]); } };
  el.appendChild(del);
  syncTools();
}
function setTool(t) { W.chart.setTool(t); syncTools(); }
function syncTools() { $$('#drawbar [data-tool]').forEach(b => b.classList.toggle('on', b.dataset.tool === W.chart.tool)); }

const TYPES = [['candles', 'Candles'], ['hollow', 'Hollow candles'], ['bars', 'Bars'], ['ha', 'Heikin Ashi'], ['line', 'Line'], ['area', 'Area']];
function typeMenu(anchor) {
  const m = h('<div class="menu"></div>');
  for (const [k, n] of TYPES) { const b = h(`<button class="${W.tab().type === k ? 'on' : ''}">${n}</button>`); b.onclick = () => { W.setType(k); closePopover(); }; m.appendChild(b); }
  popover(anchor, m);
}
function tradeMenu(anchor) {
  const st = W.account ? W.account.settings : {};
  const m = h(`<div class="menu" style="width:260px">
    <div class="pop-sec">Trade on</div>
    <button class="on">Paper trading <span class="tag">Paper</span><span class="grow"></span><span class="dim">${W.account ? money(W.account.equity, 0) : ''}</span></button>
    <button disabled style="opacity:.5">Connect a broker (coming)</button>
    <div class="msep"></div>
    <button data-a="order">Order panel</button>
    <button data-a="bottom">Trading panel (positions, orders, history)</button>
    <button data-a="qt">${W.state.quicktrade ? 'Hide' : 'Show'} quick buy / sell on the chart</button>
    <div class="msep"></div>
    <button data-a="ask">${st.ask_before ? '✓ ' : ''}Ask before placing</button>
    <button data-a="kill">${st.kill_switch ? '✓ ' : ''}Kill switch (block new orders)</button>
    <button data-a="flatten">Flatten everything</button>
  </div>`);
  m.onclick = async e => {
    const a = e.target.closest('[data-a]'); if (!a) return; closePopover();
    if (a.dataset.a === 'order') openSide('order', true);
    if (a.dataset.a === 'bottom') toggleBottom(true);
    if (a.dataset.a === 'qt') { W.state.quicktrade = !W.state.quicktrade; $('#quicktrade').classList.toggle('hide', !W.state.quicktrade); W.save(); }
    if (a.dataset.a === 'ask') W.paper('settings', { ask_before: !st.ask_before });
    if (a.dataset.a === 'kill') W.paper('settings', { kill_switch: !st.kill_switch });
    if (a.dataset.a === 'flatten' && await confirmDialog('Flatten', 'Cancel every working order and close every position at market?', 'Flatten', 'sell')) W.paper('flatten');
  };
  popover(anchor, m, { align: 'right' });
}

const RANGES = [['1D', '1m', 1], ['5D', '5m', 5], ['1M', '30m', 30], ['3M', '1h', 91], ['6M', '4h', 182], ['YTD', '1D', 'ytd'], ['1Y', '1D', 365], ['5Y', '1W', 1826], ['All', '1M', 'all']];
function renderRanges() {
  const el = $('#ranges'); el.innerHTML = '';
  for (const [label, tf, days] of RANGES) {
    const b = h(`<button>${label}</button>`); el.appendChild(b);
    b.onclick = async () => {
      await W.setTf(tf); const S = W.getSeries(W.tab().symbol, tf); if (!S.bars.length) return;
      const last = S.bars[S.bars.length - 1].t; let from;
      if (days === 'all') from = 0; else if (days === 'ytd') { const y = new Date().getFullYear(); from = Date.UTC(y, 0, 1) / 1000; } else from = last - days * 86400;
      W.chart.showRange(from);
    };
  }
}

// ------------------------------------------------------------------------------------------------ replay
W.replay = null; let rpTimer = null;
function startReplayPick() { if (W.replay != null) { exitReplay(); return; } setTool('replay-pick'); $('#replayBtn').classList.add('on'); toast('Click on the chart where the replay should start'); }
function startReplay(i) {
  const S = W.getSeries(W.tab().symbol, W.tab().tf); i = Math.max(10, Math.min(S.bars.length - 2, i));
  W.replay = i; W.chart.live = false; W.chart.setReplay(i); setTool('cursor'); $('#replayBtn').classList.add('on'); $('#replaybar').hidden = false; recompute(); replayInfo();
}
function replayStep() {
  const S = W.getSeries(W.tab().symbol, W.tab().tf); if (W.replay == null) return;
  if (W.replay >= S.bars.length - 1) { pauseReplay(); toast('Replay reached the last bar'); return; }
  W.replay++; W.chart.setReplay(W.replay);
  const v = W.chart.view; if (W.replay > v.b - 3) { v.a += 1; v.b += 1; }
  recompute(); replayInfo();
}
function replayInfo() { const S = W.getSeries(W.tab().symbol, W.tab().tf), b = S.bars[W.replay]; if (!b) return; const p = nyParts(b.t); $('#rpInfo').textContent = `${p.mon} ${p.d} ${String(p.h).padStart(2, '0')}:${String(p.mi).padStart(2, '0')} · ${S.bars.length - 1 - W.replay} bars left`; }
function playReplay() { if (rpTimer) return pauseReplay(); $('#rpPlay').textContent = '❚❚'; const tick = () => { replayStep(); if (W.replay != null && rpTimer) rpTimer = setTimeout(tick, 1000 / +$('#rpSpeed').value); }; rpTimer = setTimeout(tick, 10); }
function pauseReplay() { clearTimeout(rpTimer); rpTimer = null; $('#rpPlay').textContent = '▶'; }
function exitReplay() { pauseReplay(); W.replay = null; W.chart.live = true; W.chart.setReplay(null); $('#replaybar').hidden = true; $('#replayBtn').classList.remove('on'); if (W.chart.tool === 'replay-pick') setTool('cursor'); recompute(); W.chart.fit(W.chart.view.b - W.chart.view.a); }

// ------------------------------------------------------------------------------------------------ chart context menu & drawing edit
function chartContext(info) {
  const last = W.activeBars().at(-1)?.c, p = info.price, prec = W.chart.prec, sym = W.tab().symbol, ps = fmtPrice(p, prec);
  const m = h(`<div class="menu" style="width:250px">
    <button data-a="buy">Buy ${p < last ? 'limit' : 'stop'} ${W.state.qty || 1} @ ${ps}</button>
    <button data-a="sell">Sell ${p > last ? 'limit' : 'stop'} ${W.state.qty || 1} @ ${ps}</button>
    <div class="msep"></div>
    <button data-a="hline">Horizontal line at ${ps}</button>
    <button data-a="ask">Ask Quant about ${ps}</button>
    ${info.drawing ? '<div class="msep"></div><button data-a="edit">Edit drawing…</button><button data-a="del">Delete drawing</button>' : ''}
    <div class="msep"></div>
    <button data-a="reset">Reset chart view</button>
  </div>`);
  m.onclick = e => {
    const a = e.target.closest('[data-a]'); if (!a) return; closePopover();
    const qty = W.state.qty || 1;
    if (a.dataset.a === 'buy') W.placeOrder({ symbol: sym, side: 'buy', qty, type: p < last ? 'limit' : 'stop', price: +p.toFixed(prec) }).catch(() => {});
    if (a.dataset.a === 'sell') W.placeOrder({ symbol: sym, side: 'sell', qty, type: p > last ? 'limit' : 'stop', price: +p.toFixed(prec) }).catch(() => {});
    if (a.dataset.a === 'hline') W.addDrawings([{ id: newId('dr'), type: 'hline', points: [{ t: info.t, p: +p.toFixed(prec) }], source: 'user' }]);
    if (a.dataset.a === 'ask') { if (!W.state.chatOpen) toggleChat(); focusChat(`What's the significance of ${ps} on ${sym} ${W.tab().tf}?`); }
    if (a.dataset.a === 'edit') editDrawing(info.drawing.d);
    if (a.dataset.a === 'del') { W.pushUndo(); W.setDrawings(W.drawings().filter(x => x !== info.drawing.d)); }
    if (a.dataset.a === 'reset') W.chart.fit();
  };
  popAt(info.x, info.y, m);
}
function editDrawing(d) {
  const colors = ['#2962ff', '#f5a623', '#089981', '#f23645', '#e040fb', '#00bcd4', '#d1d4dc'];
  const el = h(`<div class="dialog"><div class="ph"><h3>Edit ${esc(d.type)}</h3></div><div class="pbody">
    <label class="field"><span>Label</span><input name="text" value="${esc(d.text || '')}"></label>
    <div class="field"><span>Color</span><div class="row" style="justify-content:flex-start;gap:6px">${colors.map(c => `<button data-c="${c}" style="flex:none;width:24px;height:24px;border-radius:5px;background:${c};${(d.color || '') === c ? 'outline:2px solid #fff' : ''}"></button>`).join('')}</div></div>
    <label class="field"><span>Style</span><select name="style"><option value="solid">Solid</option><option value="dashed">Dashed</option><option value="dotted">Dotted</option></select></label>
    ${d.type === 'trend' ? '<label class="toggle">Extend right <span class="sw" data-sw="extend"></span></label>' : ''}
    ${d.type === 'hline' || d.type === 'hray' ? `<label class="field"><span>Price</span><input name="price" type="number" step="any" value="${d.points[0].p}"></label>` : ''}
  </div><div class="foot"><button class="btn ghost" data-a="del">Delete</button><span class="grow"></span><button class="btn ghost" data-a="no">Cancel</button><button class="btn primary" data-a="ok">Save</button></div></div>`);
  el.querySelector('[name=style]').value = d.style || 'solid';
  let color = d.color, ext = !!d.extend; const sw = el.querySelector('[data-sw]'); if (sw) sw.classList.toggle('on', ext);
  el.onclick = e => {
    const c = e.target.closest('[data-c]'); if (c) { color = c.dataset.c; el.querySelectorAll('[data-c]').forEach(x => { x.style.outline = x === c ? '2px solid #fff' : ''; }); return; }
    if (e.target.closest('[data-sw]')) { ext = !ext; sw.classList.toggle('on', ext); return; }
    const a = e.target.closest('[data-a]'); if (!a) return;
    if (a.dataset.a === 'ok') { W.pushUndo(); d.text = el.querySelector('[name=text]').value; d.color = color; d.style = el.querySelector('[name=style]').value; d.extend = ext; const pr = el.querySelector('[name=price]'); if (pr && pr.value) d.points[0].p = +pr.value; W.setDrawings(W.drawings()); }
    if (a.dataset.a === 'del') { W.pushUndo(); W.setDrawings(W.drawings().filter(x => x !== d)); }
    closeModal();
  };
  modal(el);
}

// ------------------------------------------------------------------------------------------------ misc ui
function toggleChat(force) { W.state.chatOpen = force ?? !W.state.chatOpen; $('#chatpane').classList.toggle('hide', !W.state.chatOpen); W.save(); W.chart.req(); }
W.toggleChat = toggleChat;
function tickClock() { const n = nyNow(); $('#clock').textContent = `${String(n.h).padStart(2, '0')}:${String(n.m).padStart(2, '0')}:${String(n.s).padStart(2, '0')} New York`; W.chart && W.chart.req(); }
function quickTrade() {
  const qt = $('#quicktrade'), inp = $('#qtQty'); inp.value = W.state.qty || 1; qt.classList.toggle('hide', !W.state.quicktrade);
  inp.onchange = () => { W.state.qty = Math.max(1, +inp.value || 1); W.save(); W.emit('qty'); };
  $$('#quicktrade [data-side]').forEach(b => { b.onclick = () => W.placeOrder({ symbol: W.tab().symbol, side: b.dataset.side, qty: Math.max(1, +inp.value || 1), type: 'market', source: 'chart' }).catch(() => {}); });
  W.on('quote', ({ s, last }) => { if (s === W.tab().symbol) { const p = fmtPrice(last, W.chart.prec); $('#qtBid').textContent = p; $('#qtAsk').textContent = p; } });
  W.on('qty', () => { inp.value = W.state.qty; });
}
function keys() {
  document.addEventListener('keydown', e => {
    const tag = (e.target.tagName || '').toLowerCase(); if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
    if (!$('#modal').hidden || !$('#popover').hidden) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? doRedo() : doUndo(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openSymbolSearch(''); return; }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (W.replay != null && e.key === ' ') { e.preventDefault(); playReplay(); return; }
    if (W.replay != null && e.key === 'ArrowRight') { e.preventDefault(); replayStep(); return; }
    if (e.key === '/') { e.preventDefault(); openLibrary(); return; }
    if (/^[0-9]$/.test(e.key)) { e.preventDefault(); openTfPopover($('#tfBtn'), e.key); return; }
    if (/^[a-zA-Z]$/.test(e.key) && document.activeElement === W.chart.cv) { e.preventDefault(); openSymbolSearch(e.key.toUpperCase()); }
  });
}

// ------------------------------------------------------------------------------------------------ boot
async function boot() {
  W.state = await loadState();
  try { W.cfg = await api('config'); } catch { W.cfg = { data: '?', llm: 'demo' }; }
  $('#dataPill').textContent = W.cfg.data === 'alpaca' ? `Alpaca ${W.cfg.feed || ''}`.trim() : W.cfg.data === 'fake' ? 'Simulated data' : 'Data offline';
  $('#dataPill').classList.toggle('ok', W.cfg.data === 'alpaca');
  $('#aiPill').textContent = W.cfg.llm === 'anthropic' ? `Claude · ${W.cfg.model}` : 'AI demo (no API key)';
  $('#aiPill').classList.toggle('ok', W.cfg.llm === 'anthropic');
  W.chart = new Chart($('#chart'), {
    onNeedMore: loadMore,
    onLegend: (id, a) => {
      const ind = W.tab().indicators.find(i => i.id === id); if (!ind) return;
      if (a === 'toggle') W.updateIndicator(id, { visible: ind.visible === false });
      if (a === 'remove') W.removeIndicator(id);
      if (a === 'settings') indicatorSettings(id);
      if (a === 'source') openInEditor({ indicatorId: id });
    },
    onDrawingAdd: d => { W.pushUndo(); W.setDrawings([...W.drawings(), d]); },
    onDrawingsChange: () => { W.save(); W.emit('drawings'); },
    onDrawingDelete: d => { W.pushUndo(); W.setDrawings(W.drawings().filter(x => x !== d)); },
    onDrawingEdit: hit => editDrawing(hit.d),
    onToolDone: () => setTool('cursor'),
    onSelect: () => {},
    onOrderDrag: (id, price) => {
      price = +price.toFixed(W.chart.prec);
      if (id.endsWith(':sl')) W.paper('modify', { id: id.slice(0, -3), sl: price }).catch(() => {});
      else if (id.endsWith(':tp')) W.paper('modify', { id: id.slice(0, -3), tp: price }).catch(() => {});
      else W.paper('modify', { id, price }).catch(() => {});
    },
    onOrderCancel: id => W.paper('cancel', { id }).catch(() => {}),
    onReplayPick: i => startReplay(i),
    onContext: chartContext,
    legendSlot: $('#quicktrade'),
  });
  $('#chatpane').classList.toggle('hide', !W.state.chatOpen);
  $('#toggleChat').onclick = () => toggleChat();
  $('#addTab').onclick = () => W.newTab();
  $('#symBtn').onclick = () => openSymbolSearch('');
  $('#tfBtn').onclick = () => openTfPopover($('#tfBtn'));
  $('#typeBtn').onclick = () => typeMenu($('#typeBtn'));
  $('#indBtn').onclick = () => openLibrary();
  $('#replayBtn').onclick = startReplayPick;
  $('#undoBtn').onclick = doUndo; $('#redoBtn').onclick = doRedo;
  $('#tradeBtn').onclick = () => tradeMenu($('#tradeBtn'));
  $('#snapBtn').onclick = () => { const a = document.createElement('a'); a.href = W.chart.snapshot(2400); a.download = `${W.tab().symbol}-${W.tab().tf}.jpg`; a.click(); };
  $('#rpPlay').onclick = playReplay; $('#rpStep').onclick = replayStep; $('#rpExit').onclick = exitReplay;
  renderDrawbar(); renderRanges(); quickTrade(); keys();
  initPanels(); initEditor(); initChat();
  setInterval(tickClock, 1000); tickClock();
  try { W.account = await api('paper'); W.emit('account', W.account); } catch { /* server down */ }
  await activate(W.state.active);
  refreshQuotes(); setInterval(refreshQuotes, 60000);
  if (W.state.side) openSide(W.state.side, true);
  if (W.state.bottom) toggleBottom(true);
}
boot();
