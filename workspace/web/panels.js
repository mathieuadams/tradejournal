// Right-side panels (watchlist, order panel, object tree, settings), the bottom trading panel and indicator settings.
import { W } from './ctx.js';
import { $, $$, h, esc, api, toast, money, signed, pct, priceDigits, modal, closeModal, confirmDialog, fmtTime } from './util.js';
import { fmtPrice, fmtVol } from './chart.js';
import { openSymbolSearch } from './dialogs.js';
import { mountEditor } from './editor.js';
import { mountResearch } from './research.js';
import { mountFrozen } from './frozen.js';
import { mountOptions } from './options.js';

let current = null;
const RENDER = { watchlist: renderWatchlist, order: renderOrder, tree: renderTree, settings: renderSettings, editor: el => mountEditor(el), research: el => mountResearch(el), frozen: el => mountFrozen(el), options: el => mountOptions(el) };

export function initPanels() {
  $$('#rail [data-p]').forEach(b => { b.onclick = () => b.dataset.p === 'positions' ? toggleBottom() : openSide(b.dataset.p); });
  $('#bottomToggle').onclick = () => toggleBottom();
  W.on('account', () => { if (current === 'order') updateOrderAccount(); if (!$('#bottompanel').hidden) renderBottomBody(); });
  W.on('quote', q => { if (current === 'watchlist') updateWlRow(q.s, q.prev); if (current === 'order' && q.s === W.tab().symbol) updateOrderPrice(); });
  W.on('quotes', () => { if (current === 'watchlist') renderWatchlist($('#sidebody')); });
  W.on('watchlist', () => { if (current === 'watchlist') renderWatchlist($('#sidebody')); });
  W.on('tab', () => { if (current === 'order' || current === 'tree' || current === 'watchlist') RENDER[current]($('#sidebody')); });
  W.on('indicators', () => { if (current === 'tree') renderTree($('#sidebody')); });
  W.on('drawings', () => { if (current === 'tree') renderTree($('#sidebody')); });
  W.on('qty', () => { if (current === 'order') { const q = $('#opQty'); if (q) { q.value = W.state.qty; updateOrderButton(); } } });
  // resizable side panel
  const rz = $('#sideResize'); let start = null;
  rz.onpointerdown = e => { start = { x: e.clientX, w: $('#sidepanel').offsetWidth }; rz.setPointerCapture(e.pointerId); };
  rz.onpointermove = e => { if (!start) return; const w = Math.max(280, Math.min(innerWidth * 0.7, start.w - (e.clientX - start.x))); $('#sidepanel').style.width = w + 'px'; W.chart.req(); };
  rz.onpointerup = () => { start = null; };
}

export function openSide(name, force) {
  const panel = $('#sidepanel');
  if (current === name && !force) { panel.hidden = true; current = null; W.state.side = null; }
  else { current = name; W.state.side = name; panel.hidden = false; panel.classList.toggle('wide', name === 'editor' || name === 'research' || name === 'frozen' || name === 'options'); panel.style.width = ''; RENDER[name]($('#sidebody')); }
  $$('#rail [data-p]').forEach(b => b.classList.toggle('on', b.dataset.p === current || (b.dataset.p === 'positions' && !$('#bottompanel').hidden)));
  W.save(); requestAnimationFrame(() => W.chart.req());
}
W.openSide = openSide;
W.currentSide = () => current;

// ------------------------------------------------------------------------------------------------ watchlist
let wlSel = null;
function renderWatchlist(el) {
  el.innerHTML = '';
  el.appendChild(h(`<div class="ph"><h3>Watchlist</h3><span class="grow"></span><button class="icon-btn" data-a="add" title="Add symbol">＋</button><button class="icon-btn x" data-a="close" title="Close">✕</button></div>`));
  el.appendChild(h(`<div class="wl-head"><span>Symbol</span><span>Last</span><span>Chg%</span></div>`));
  const list = h('<div style="flex:1;overflow:auto"></div>'); el.appendChild(list);
  for (const s of W.state.watchlist) list.appendChild(wlRow(s));
  const det = h('<div class="detail" id="wlDetail"></div>'); el.appendChild(det); renderDetail();
  el.querySelector('[data-a=add]').onclick = () => openSymbolSearch('', { onPick: sym => { if (!W.state.watchlist.includes(sym)) { W.state.watchlist.push(sym); W.save(); W.resubscribe(); W.refreshQuotes(); renderWatchlist(el); } } });
  el.querySelector('[data-a=close]').onclick = () => openSide('watchlist');
}
function wlRow(s) {
  const q = W.quotes[s] || {}, ch = q.last != null && q.prev ? q.last - q.prev : null, chp = ch != null ? ch / q.prev * 100 : null;
  const r = h(`<div class="wl-row ${s === W.tab().symbol ? 'on' : ''}" data-s="${esc(s)}"><div><b>${esc(s)}</b><small>${esc(q.name || '')}</small></div>
    <span class="num last">${q.last != null ? fmtPrice(q.last, priceDigits(q.last)) : '—'}<small class="${ch >= 0 ? 'up' : 'dn'} chg">${ch != null ? signed(ch, priceDigits(q.last)) : ''}</small></span>
    <span class="num pc ${chp >= 0 ? 'up' : 'dn'}">${chp != null ? pct(chp) : ''}</span><button class="del" title="Remove">✕</button></div>`);
  r.onclick = e => {
    if (e.target.closest('.del')) { e.stopPropagation(); W.state.watchlist = W.state.watchlist.filter(x => x !== s); W.save(); r.remove(); W.resubscribe(); return; }
    wlSel = s; W.setSymbol(s); renderDetail();
  };
  return r;
}
function updateWlRow(s, prevLast) {
  const r = $(`.wl-row[data-s="${CSS.escape(s)}"]`); if (!r) return; const q = W.quotes[s]; if (!q || q.last == null) return;
  const d = priceDigits(q.last), last = r.querySelector('.last');
  last.firstChild.nodeValue = fmtPrice(q.last, d);
  if (q.prev) { const ch = q.last - q.prev; const c = r.querySelector('.chg'); c.textContent = signed(ch, d); c.className = (ch >= 0 ? 'up' : 'dn') + ' chg'; const p = r.querySelector('.pc'); p.textContent = pct(ch / q.prev * 100); p.className = 'num pc ' + (ch >= 0 ? 'up' : 'dn'); }
  if (prevLast != null && prevLast !== q.last && Date.now() - (last._fl || 0) > 1200) { last._fl = Date.now(); last.classList.remove('flash-up', 'flash-dn'); void last.offsetWidth; last.classList.add(q.last > prevLast ? 'flash-up' : 'flash-dn'); }
  if (s === (wlSel || W.tab().symbol)) renderDetail();
}
function renderDetail() {
  const el = $('#wlDetail'); if (!el) return; const s = wlSel || W.tab().symbol, q = W.quotes[s] || {};
  const ch = q.last != null && q.prev ? q.last - q.prev : 0, d = priceDigits(q.last);
  const lo = Math.min(q.low ?? q.last, q.last ?? 0), hi = Math.max(q.high ?? q.last, q.last ?? 0), f = hi > lo ? (q.last - lo) / (hi - lo) : 0.5;
  const a0 = Math.PI * 0.75, a1 = a0 + Math.PI * 1.5 * f, R = 42, cx = 50, cy = 50, pt = a => `${cx + R * Math.cos(a)},${cy + R * Math.sin(a)}`;
  el.innerHTML = `<h4>${esc(q.name || s)}</h4><div class="dim" style="font-size:11.5px">${esc(s)}</div>
    <div style="display:flex;align-items:baseline;gap:8px;margin-top:6px"><b style="font-size:26px;font-family:var(--mono)">${q.last != null ? fmtPrice(q.last, d) : '—'}</b><span class="${ch >= 0 ? 'up' : 'dn'}">${signed(ch, d)} today</span></div>
    <div class="gauge"><svg viewBox="0 0 100 100" style="width:110px;height:110px"><path d="M${pt(a0)} A${R} ${R} 0 1 1 ${pt(a0 + Math.PI * 1.5)}" stroke="#262b36" stroke-width="7" fill="none"/>
      <path d="M${pt(a0)} A${R} ${R} 0 ${f > 2 / 3 ? 1 : 0} 1 ${pt(a1)}" stroke="${ch >= 0 ? '#089981' : '#f23645'}" stroke-width="7" fill="none"/><circle cx="${cx + R * Math.cos(a1)}" cy="${cy + R * Math.sin(a1)}" r="5" fill="#fff"/>
      <text x="50" y="47" text-anchor="middle" fill="#868c99" font-size="8">TODAY</text><text x="50" y="60" text-anchor="middle" fill="${ch >= 0 ? '#089981' : '#f23645'}" font-size="11" font-weight="600">${q.prev ? pct(ch / q.prev * 100) : ''}</text></svg>
      <dl><dt>Open</dt><dd>${q.open != null ? fmtPrice(q.open, d) : '—'}</dd><dt>High</dt><dd>${q.high != null ? fmtPrice(Math.max(q.high, q.last), d) : '—'}</dd><dt>Low</dt><dd>${q.low != null ? fmtPrice(Math.min(q.low, q.last), d) : '—'}</dd><dt>Prev</dt><dd>${q.prev != null ? fmtPrice(q.prev, d) : '—'}</dd><dt>Volume</dt><dd>${q.volume != null ? fmtVol(q.volume) : '—'}</dd></dl></div>`;
}

// ------------------------------------------------------------------------------------------------ order panel
const op = { side: 'buy', type: 'market', qtyMode: 'shares', price: '', sl: '', tp: '', slMode: 'price' };
function renderOrder(el) {
  const a = W.account || {}, st = a.settings || {}, sym = W.tab().symbol;
  el.innerHTML = '';
  el.appendChild(h(`<div class="ph"><h3>Order panel</h3><span class="grow"></span><button class="icon-btn x" title="Close">✕</button></div>`));
  const body = h(`<div class="pbody">
    <div class="row" style="justify-content:space-between;margin-bottom:8px"><b>Paper Trading</b><span class="tag" style="flex:none">Paper</span></div>
    <div class="note" style="margin:-4px 0 10px">Simulated, not real money</div>
    <div class="kv" id="opAcct"></div>
    <div class="hr"></div>
    <div class="row" style="justify-content:space-between"><b style="font-size:14px">${esc(sym)}</b><b class="mono" id="opPx" style="text-align:right;font-size:14px">—</b></div>
    <div class="op-side"><button class="s ${op.side === 'sell' ? 'on' : ''}" data-side="sell">Sell</button><button class="b ${op.side === 'buy' ? 'on' : ''}" data-side="buy">Buy</button></div>
    <div class="seg" id="opType">${['market', 'limit', 'stop'].map(t => `<button data-t="${t}" class="${op.type === t ? 'on' : ''}">${t[0].toUpperCase() + t.slice(1)}</button>`).join('')}</div>
    <div class="field" style="margin-top:10px" id="opPriceF" ${op.type === 'market' ? 'hidden' : ''}><span>${op.type === 'limit' ? 'Limit' : 'Stop'} price</span><input id="opPrice" type="number" step="any" value="${esc(op.price)}"></div>
    <div class="field" style="margin-top:10px"><span>Quantity <i class="dim" id="opUnit">${op.qtyMode === 'shares' ? 'shares' : op.qtyMode === 'usd' ? 'USD' : op.qtyMode === 'pct' ? '% equity' : '$ risk'}</i></span>
      <div class="seg" id="opQm">${[['shares', 'Shares'], ['usd', 'USD'], ['pct', '% equity'], ['risk', 'Risk']].map(([k, n]) => `<button data-q="${k}" class="${op.qtyMode === k ? 'on' : ''}">${n}</button>`).join('')}</div>
      <input id="opQty" type="number" min="0" step="any" value="${op.qtyMode === 'shares' ? (W.state.qty || 1) : ''}" style="margin-top:6px"></div>
    <div class="note" id="opMargin"></div>
    <div class="op-quick" id="opQuick">${[1, 10, 100, 500].map(n => `<button data-n="${n}">${n}</button>`).join('')}</div>
    <div class="row"><label class="field"><span>Stop loss</span><div class="inline-inp"><input id="opSl" type="number" step="any" placeholder="Optional" value="${esc(op.sl)}"><button data-pct="sl">Use 1%</button></div></label>
      <label class="field"><span>Take profit</span><div class="inline-inp"><input id="opTp" type="number" step="any" placeholder="Optional" value="${esc(op.tp)}"><button data-pct="tp">Use 2%</button></div></label></div>
    <div class="note" id="opRisk"></div>
    <button class="btn op-place ${op.side === 'buy' ? 'buy' : 'sell'}" id="opPlace">…</button>
    <div class="note" style="text-align:center;margin-top:6px">Paper · fills are estimates at the last trade</div>
    <div class="hr"></div>
    <div class="toggle">Ask before placing <span class="sw ${st.ask_before ? 'on' : ''}" data-sw="ask_before"></span></div>
    <div class="toggle">Kill switch <span class="sw ${st.kill_switch ? 'on' : ''}" data-sw="kill_switch"></span></div>
    <div class="note">Paper limits: any symbol priced in dollars; total position value up to your equity. The kill switch cancels working entries and blocks new orders.</div>
    <div class="row" style="margin-top:10px"><button class="btn sm" data-a="flatten">Flatten all</button><button class="btn sm" data-a="reset">Reset account</button></div>
  </div>`);
  el.appendChild(body);
  el.querySelector('.ph .x').onclick = () => openSide('order');
  body.onclick = async e => {
    const t = e.target;
    const side = t.closest('[data-side]'); if (side) { op.side = side.dataset.side; renderOrder(el); return; }
    const ty = t.closest('[data-t]'); if (ty) { op.type = ty.dataset.t; if (op.type !== 'market' && !op.price) op.price = lastPx() ? String(lastPx()) : ''; renderOrder(el); return; }
    const qm = t.closest('[data-q]'); if (qm) { op.qtyMode = qm.dataset.q; renderOrder(el); return; }
    const n = t.closest('[data-n]'); if (n) { op.qtyMode = 'shares'; W.state.qty = +n.dataset.n; W.save(); W.emit('qty'); renderOrder(el); return; }
    const pc = t.closest('[data-pct]'); if (pc) { const ref = entryPx(); if (!ref) return; const k = pc.dataset.pct, d = priceDigits(ref), up = (op.side === 'buy') === (k === 'tp'); const v = ref * (1 + (up ? 1 : -1) * (k === 'sl' ? 0.01 : 0.02)); op[k] = v.toFixed(d); $('#op' + (k === 'sl' ? 'Sl' : 'Tp')).value = op[k]; updateOrderButton(); return; }
    const sw = t.closest('[data-sw]'); if (sw) { W.paper('settings', { [sw.dataset.sw]: !sw.classList.contains('on') }).then(() => renderOrder(el)).catch(() => {}); return; }
    const a = t.closest('[data-a]');
    if (a && a.dataset.a === 'flatten' && await confirmDialog('Flatten', 'Cancel every working order and close every position at market?', 'Flatten', 'sell')) W.paper('flatten');
    if (a && a.dataset.a === 'reset') resetDialog();
    if (t.id === 'opPlace') placeFromPanel();
  };
  body.oninput = e => {
    if (e.target.id === 'opQty') { if (op.qtyMode === 'shares') { W.state.qty = Math.max(0, +e.target.value || 0); W.save(); $('#qtQty').value = W.state.qty; } }
    if (e.target.id === 'opPrice') op.price = e.target.value;
    if (e.target.id === 'opSl') op.sl = e.target.value;
    if (e.target.id === 'opTp') op.tp = e.target.value;
    updateOrderButton();
  };
  updateOrderAccount(); updateOrderPrice();
}
const lastPx = () => { const s = W.tab().symbol; return (W.quotes[s] && W.quotes[s].last) || (W.activeBars().at(-1) || {}).c; };
const entryPx = () => op.type === 'market' ? lastPx() : (+op.price || lastPx());
function orderQty() {
  const v = +($('#opQty') || {}).value || 0, px = entryPx(), eq = (W.account || {}).equity || 0;
  if (op.qtyMode === 'shares') return v;
  if (op.qtyMode === 'usd') return px ? Math.floor(v / px) : 0;
  if (op.qtyMode === 'pct') return px ? Math.floor(eq * v / 100 / px) : 0;
  const sl = +op.sl; if (!sl || !px) return 0; return Math.floor(v / Math.abs(px - sl));
}
function updateOrderAccount() {
  const a = W.account, el = $('#opAcct'); if (!a || !el) return;
  el.innerHTML = `<div><small>Balance</small><b>${money(a.cash + a.positions.reduce((s, p) => s + p.qty * p.avg, 0))}</b></div><div><small>Equity</small><b>${money(a.equity)}</b></div>
    <div><small>Open P&L</small><b class="${a.open_pnl >= 0 ? 'up' : 'dn'}">${money(a.open_pnl)}</b></div><div><small>Today</small><b class="${a.today_pnl >= 0 ? 'up' : 'dn'}">${money(a.today_pnl)}</b></div>`;
  updateOrderButton();
}
function updateOrderPrice() { const el = $('#opPx'); if (el) { const p = lastPx(); el.textContent = p ? fmtPrice(p, priceDigits(p)) : '—'; } updateOrderButton(); }
function updateOrderButton() {
  const b = $('#opPlace'); if (!b) return; const q = orderQty(), px = entryPx(), sym = W.tab().symbol, a = W.account;
  b.textContent = `${op.side === 'buy' ? 'Buy' : 'Sell'} ${q || 0} ${sym} ${op.type === 'market' ? 'at market' : `${op.type} @ ${op.price || '—'}`}`;
  b.disabled = !(q > 0);
  const m = $('#opMargin'); if (m && a && px) m.textContent = `Margin covers up to ${Math.floor(Math.max(0, a.equity - a.margin_used) / px)} shares · order value ≈ ${money(q * px)} · ${(q * px / a.equity * 100).toFixed(1)}% of equity`;
  const r = $('#opRisk'); if (r) { const sl = +op.sl, tp = +op.tp; const risk = sl && px ? Math.abs(px - sl) * q : 0, rew = tp && px ? Math.abs(tp - px) * q : 0; r.textContent = risk ? `Risk ${money(risk)}${rew ? ` · reward ${money(rew)} · R:R ${(rew / risk).toFixed(2)}` : ''}` : ''; }
}
async function placeFromPanel() {
  const q = orderQty(); if (!(q > 0)) return;
  const req = { symbol: W.tab().symbol, side: op.side, type: op.type, qty: q, source: 'panel' };
  if (op.type !== 'market') req.price = +op.price; if (op.sl) req.sl = +op.sl; if (op.tp) req.tp = +op.tp;
  try { await W.placeOrder(req); op.sl = ''; op.tp = ''; if (current === 'order') renderOrder($('#sidebody')); } catch { /* toast shown */ }
}
function resetDialog() {
  const el = h(`<div class="dialog"><div class="ph"><h3>Reset paper account</h3></div><div class="pbody"><p class="note" style="margin-top:0">Closes everything and starts over. Your trade history is cleared.</p><label class="field"><span>Starting balance</span><input type="number" value="100000" step="1000"></label></div>
    <div class="foot"><button class="btn ghost" data-a="no">Cancel</button><button class="btn sell" data-a="ok">Reset</button></div></div>`);
  el.onclick = e => { const a = e.target.closest('[data-a]'); if (!a) return; if (a.dataset.a === 'ok') W.paper('reset', { balance: +el.querySelector('input').value || 100000 }).then(() => toast('Paper account reset', 'ok')); closeModal(); };
  modal(el);
}

// ------------------------------------------------------------------------------------------------ bottom trading panel
let btab = 'positions';
export function toggleBottom(force) {
  const p = $('#bottompanel'); const show = force ?? p.hidden; p.hidden = !show; W.state.bottom = show; W.save();
  if (show) renderBottom(); $$('#rail [data-p=positions]').forEach(b => b.classList.toggle('on', show)); $('#bottomToggle').classList.toggle('on', show);
  requestAnimationFrame(() => W.chart.req());
}
function renderBottom() {
  const p = $('#bottompanel'); p.innerHTML = '';
  const tabs = h(`<div class="btabs">${[['positions', 'Positions'], ['orders', 'Orders'], ['fills', 'Fills'], ['trades', 'Closed trades']].map(([k, n]) => `<button data-b="${k}" class="${btab === k ? 'on' : ''}">${n}<span class="dim" data-c="${k}"></span></button>`).join('')}
    <div class="acct" id="bAcct"></div><button class="icon-btn" data-b="close" title="Close">✕</button></div>`);
  tabs.onclick = e => { const b = e.target.closest('[data-b]'); if (!b) return; if (b.dataset.b === 'close') return toggleBottom(false); btab = b.dataset.b; renderBottom(); };
  p.appendChild(tabs); p.appendChild(h('<div class="tblwrap" id="bBody"></div>')); renderBottomBody();
}
function renderBottomBody() {
  const a = W.account, el = $('#bBody'); if (!a || !el) return;
  $('#bAcct').innerHTML = `<span>Equity <b>${money(a.equity)}</b></span><span>Open P&L <b class="${a.open_pnl >= 0 ? 'up' : 'dn'}">${money(a.open_pnl)}</b></span><span>Today <b class="${a.today_pnl >= 0 ? 'up' : 'dn'}">${money(a.today_pnl)}</b></span><span>Buying power <b>${money(a.buying_power, 0)}</b></span>`;
  const cnt = { positions: a.positions.length, orders: a.orders.length, fills: a.fills.length, trades: a.trades.length };
  for (const [k, v] of Object.entries(cnt)) { const c = document.querySelector(`[data-c=${k}]`); if (c) c.textContent = v ? ` ${v}` : ''; }
  const d = p => fmtPrice(p, priceDigits(p));
  if (btab === 'positions') el.innerHTML = a.positions.length ? `<table class="tbl"><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Avg</th><th>Last</th><th>Value</th><th>Open P&L</th><th></th></tr>${a.positions.map(p => `<tr><td class="t"><b>${esc(p.symbol)}</b></td><td class="${p.qty > 0 ? 'up' : 'dn'}">${p.qty > 0 ? 'Long' : 'Short'}</td><td>${Math.abs(p.qty)}</td><td>${d(p.avg)}</td><td>${d(p.last)}</td><td>${money(Math.abs(p.value))}</td><td class="${p.pnl >= 0 ? 'up' : 'dn'}">${money(p.pnl)} (${pct(p.pnl_pct)})</td><td><button class="btn sm" data-close="${esc(p.symbol)}">Close</button> <button class="btn sm ghost" data-go="${esc(p.symbol)}">Chart</button></td></tr>`).join('')}</table>` : '<div class="empty">No open positions. Use the order panel, the Buy / Sell box on the chart, or right-click a price.</div>';
  if (btab === 'orders') el.innerHTML = a.orders.length ? `<table class="tbl"><tr><th>Symbol</th><th>Side</th><th>Type</th><th>Qty</th><th>Price</th><th>SL</th><th>TP</th><th>Role</th><th>Placed</th><th></th></tr>${a.orders.map(o => `<tr><td class="t"><b>${esc(o.symbol)}</b></td><td class="${o.side === 'buy' ? 'up' : 'dn'}">${o.side}</td><td>${o.type}</td><td>${o.qty}</td><td>${o.price != null ? d(o.price) : 'mkt'}</td><td>${o.sl ? d(o.sl) : ''}</td><td>${o.tp ? d(o.tp) : ''}</td><td class="t dim">${o.note === 'sl' ? 'stop loss' : o.note === 'tp' ? 'take profit' : 'entry'}</td><td class="t dim">${fmtTime(o.created)}</td><td><button class="btn sm" data-cancel="${o.id}">Cancel</button></td></tr>`).join('')}</table>` : '<div class="empty">No working orders.</div>';
  if (btab === 'fills') el.innerHTML = a.fills.length ? `<table class="tbl"><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Realized</th><th>Source</th></tr>${a.fills.map(f => `<tr><td class="t dim">${fmtTime(f.t)}</td><td class="t"><b>${esc(f.symbol)}</b></td><td class="${f.side === 'buy' ? 'up' : 'dn'}">${f.side}</td><td>${f.qty}</td><td>${d(f.price)}</td><td class="${f.realized > 0 ? 'up' : f.realized < 0 ? 'dn' : ''}">${f.realized ? money(f.realized) : ''}</td><td class="t dim">${esc(f.source || '')}</td></tr>`).join('')}</table>` : '<div class="empty">No fills yet.</div>';
  if (btab === 'trades') {
    const tot = a.trades.reduce((s, t) => s + t.pnl, 0), wins = a.trades.filter(t => t.pnl > 0).length;
    el.innerHTML = a.trades.length ? `<div class="note" style="padding:8px 10px;display:flex;align-items:center;gap:10px">${a.trades.length} trades · win rate ${(wins / a.trades.length * 100).toFixed(0)}% · total ${money(tot)}<button class="btn sm" data-csv="1">Export CSV</button></div><table class="tbl"><tr><th>Closed</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Held</th></tr>${a.trades.map(t => `<tr><td class="t dim">${fmtTime(t.closed)}</td><td class="t"><b>${esc(t.symbol)}</b></td><td class="${t.side === 'long' ? 'up' : 'dn'}">${t.side}</td><td>${t.qty}</td><td>${d(t.entry)}</td><td>${d(t.exit)}</td><td class="${t.pnl >= 0 ? 'up' : 'dn'}">${money(t.pnl)} (${pct(t.pnl_pct)})</td><td class="t dim">${held(t.closed - t.opened)}</td></tr>`).join('')}</table>` : '<div class="empty">Closed round trips show up here, ready for the journal.</div>';
  }
  el.onclick = e => {
    const c = e.target.closest('[data-close]'); if (c) W.paper('close', { symbol: c.dataset.close }).catch(() => {});
    const x = e.target.closest('[data-cancel]'); if (x) W.paper('cancel', { id: x.dataset.cancel }).catch(() => {});
    const g = e.target.closest('[data-go]'); if (g) W.setSymbol(g.dataset.go);
    if (e.target.closest('[data-csv]')) {
      const rows = [['symbol', 'side', 'qty', 'entry', 'exit', 'pnl', 'pnl_pct', 'opened', 'closed', 'source'], ...a.trades.map(t => [t.symbol, t.side, t.qty, t.entry, t.exit, t.pnl.toFixed(2), t.pnl_pct.toFixed(3), new Date(t.opened * 1000).toISOString(), new Date(t.closed * 1000).toISOString(), t.source || ''])];
      const l = document.createElement('a'); l.href = URL.createObjectURL(new Blob([rows.map(r => r.join(',')).join('\n')], { type: 'text/csv' })); l.download = 'paper-trades.csv'; l.click();
    }
  };
}
const held = s => s < 3600 ? `${Math.round(s / 60)}m` : s < 86400 ? `${(s / 3600).toFixed(1)}h` : `${(s / 86400).toFixed(1)}d`;

// ------------------------------------------------------------------------------------------------ object tree
function renderTree(el) {
  const tab = W.tab(), dr = W.drawings();
  el.innerHTML = '';
  el.appendChild(h(`<div class="ph"><h3>Object tree</h3><span class="grow"></span><button class="icon-btn x" title="Close">✕</button></div>`));
  const body = h(`<div class="pbody">
    <div class="tree-sec">Main chart</div>
    <div class="tree-row"><span class="swatch" style="background:#089981"></span><b>${esc(tab.symbol)}</b><span class="dim">${esc(tab.tf)}</span></div>
    ${W.indOut.map(d => `<div class="tree-row" data-i="${d.id}"><span class="swatch" style="background:${(d.out && d.out.plots[0] && d.out.plots[0].color || '#787b86').slice(0, 7)}"></span><span style="${d.visible ? '' : 'opacity:.5'}">${esc(d.out ? d.out.meta.title : d.title)}</span>${d.out && d.out.error ? '<span class="dn" title="Error">⚠</span>' : ''}
      <span class="acts"><button data-a="source" title="Open the script in the editor">{ }</button><button data-a="toggle" title="Show / hide">${d.visible ? '◉' : '◌'}</button><button data-a="settings" title="Settings">⚙</button><button data-a="remove" title="Remove">✕</button></span></div>`).join('')}
    <div class="tree-sec">Drawings · ${esc(tab.symbol)} (${dr.length})</div>
    ${dr.length ? dr.map((d, k) => `<div class="tree-row" data-d="${k}"><span class="swatch" style="background:${d.color || (d.source === 'ai' ? '#f5a623' : '#2962ff')}"></span><span style="${d.hidden ? 'opacity:.5' : ''}">${esc(d.text || d.type)}${d.points[0] && d.type !== 'vline' ? ` <i class="dim mono">${fmtPrice(d.points[0].p, W.chart.prec)}</i>` : ''}</span>${d.source === 'ai' ? '<span class="tag" style="background:rgba(245,166,35,.15);color:#f5a623">AI</span>' : ''}
      <span class="acts"><button data-a="dtoggle">${d.hidden ? '◌' : '◉'}</button><button data-a="dremove">✕</button></span></div>`).join('') : '<div class="note">No drawings. Use the tools on the left of the chart, or ask Quant to mark up the chart.</div>'}
    ${dr.length ? '<div class="row" style="margin-top:10px"><button class="btn sm" data-a="clearai">Remove AI drawings</button><button class="btn sm" data-a="clearall">Remove all</button></div>' : ''}
  </div>`);
  el.appendChild(body);
  el.querySelector('.ph .x').onclick = () => openSide('tree');
  body.onclick = e => {
    const a = e.target.closest('[data-a]'); if (!a) return; const ir = e.target.closest('[data-i]'), dr2 = e.target.closest('[data-d]');
    if (ir) { const id = ir.dataset.i, ind = tab.indicators.find(i => i.id === id); if (a.dataset.a === 'toggle') W.updateIndicator(id, { visible: ind.visible === false }); if (a.dataset.a === 'settings') indicatorSettings(id); if (a.dataset.a === 'remove') W.removeIndicator(id); if (a.dataset.a === 'source') import('./editor.js').then(m => m.openInEditor({ indicatorId: id })); }
    if (dr2) { const d = W.drawings()[+dr2.dataset.d]; W.pushUndo(); if (a.dataset.a === 'dtoggle') { d.hidden = !d.hidden; W.setDrawings(W.drawings()); } if (a.dataset.a === 'dremove') W.setDrawings(W.drawings().filter(x => x !== d)); }
    if (a.dataset.a === 'clearai') { W.pushUndo(); W.setDrawings(W.drawings().filter(x => x.source !== 'ai')); }
    if (a.dataset.a === 'clearall') { W.pushUndo(); W.setDrawings([]); }
  };
}

// ------------------------------------------------------------------------------------------------ settings
function renderSettings(el) {
  el.innerHTML = '';
  el.appendChild(h(`<div class="ph"><h3>Settings</h3><span class="grow"></span><button class="icon-btn x" title="Close">✕</button></div>`));
  const c = W.cfg;
  const body = h(`<div class="pbody">
    <div class="tree-sec" style="margin-top:0">Server</div>
    <div class="kv"><div><small>Market data</small><b style="font-family:var(--sans)">${esc(c.data === 'alpaca' ? 'Alpaca (' + (c.feed || 'iex') + ')' : 'Simulated')}</b></div><div><small>AI</small><b style="font-family:var(--sans)">${esc(c.llm === 'anthropic' ? c.model : 'Demo')}</b></div></div>
    <p class="note">Keys never reach the browser. Alpaca: the key saved in the journal's Data page (live stream on the IEX feed). Claude: the <code>ANTHROPIC_API_KEY</code> GitHub secret. Both are picked up at each deploy. Without Alpaca keys the charts run on simulated data.</p>
    <div class="tree-sec">Workspace</div>
    <div class="toggle">Quick buy / sell on the chart <span class="sw ${W.state.quicktrade ? 'on' : ''}" data-sw="quicktrade"></span></div>
    <div class="toggle">Quant sees the chart (screenshot with each message) <span class="sw ${W.state.sees ? 'on' : ''}" data-sw="sees"></span></div>
    <div class="row" style="margin-top:10px"><button class="btn sm" data-a="export">Export workspace</button><button class="btn sm" data-a="reset">Reset layout</button></div>
    <div class="tree-sec">Keyboard</div>
    <div class="note">Type letters on the chart: symbol search · digits: timeframe · <b>/</b> indicators · <b>Ctrl+K</b> symbols · <b>Ctrl+Z</b> undo drawing · <b>Del</b> delete selected drawing · <b>Esc</b> cancel tool · replay: <b>Space</b> play, <b>→</b> step · wheel: zoom time · drag: pan · drag price scale: zoom price · double-click price scale: auto.</div>
  </div>`);
  el.appendChild(body);
  el.querySelector('.ph .x').onclick = () => openSide('settings');
  body.onclick = async e => {
    const sw = e.target.closest('[data-sw]'); if (sw) { const k = sw.dataset.sw; W.state[k] = !W.state[k]; sw.classList.toggle('on', W.state[k]); W.save(); if (k === 'quicktrade') $('#quicktrade').classList.toggle('hide', !W.state.quicktrade); W.emit('settings'); }
    const a = e.target.closest('[data-a]');
    if (a && a.dataset.a === 'export') { const b = new Blob([JSON.stringify(W.state, null, 2)], { type: 'application/json' }); const l = document.createElement('a'); l.href = URL.createObjectURL(b); l.download = 'workspace.json'; l.click(); }
    if (a && a.dataset.a === 'reset' && await confirmDialog('Reset layout', 'Reset tabs, watchlist and drawings to the defaults? Your saved scripts are kept.', 'Reset', 'sell')) { const keep = W.state.scripts; localStorage.removeItem('ws-state'); await api('state', { scripts: keep }, 'PUT'); location.reload(); }
  };
}

// ------------------------------------------------------------------------------------------------ indicator settings
export function indicatorSettings(id) {
  const ind = W.tab().indicators.find(i => i.id === id), d = W.indOut.find(x => x.id === id); if (!ind || !d || !d.out) return;
  const ins = d.out.inputs.filter(Boolean);
  const field = i => {
    const v = (ind.inputs && ind.inputs[i.idx] !== undefined) ? ind.inputs[i.idx] : i.defval;
    if (i.type === 'bool') return `<div class="toggle">${esc(i.title)} <span class="sw ${v ? 'on' : ''}" data-b="${i.idx}"></span></div>`;
    if (i.type === 'color') return `<label class="field"><span>${esc(i.title)}</span><input type="color" data-k="${i.idx}" value="${String(v).slice(0, 7)}"></label>`;
    if (i.type === 'source') return `<label class="field"><span>${esc(i.title)}</span><select data-k="${i.idx}">${['open', 'high', 'low', 'close', 'hl2', 'hlc3', 'ohlc4', 'hlcc4', 'volume'].map(s => `<option ${s === v ? 'selected' : ''}>${s}</option>`).join('')}</select></label>`;
    if (i.options) return `<label class="field"><span>${esc(i.title)}</span><select data-k="${i.idx}">${i.options.map(o => `<option ${o === v ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select></label>`;
    if (i.type === 'int' || i.type === 'float' || i.type === 'price') return `<label class="field"><span>${esc(i.title)}</span><input type="number" data-k="${i.idx}" data-num="1" step="${i.step || (i.type === 'int' ? 1 : 'any')}" ${i.minval != null ? `min="${i.minval}"` : ''} ${i.maxval != null ? `max="${i.maxval}"` : ''} value="${v}"></label>`;
    return `<label class="field"><span>${esc(i.title)}</span><input data-k="${i.idx}" value="${esc(v)}"></label>`;
  };
  const el = h(`<div class="dialog"><div class="ph"><h3>${esc(d.out.meta.title)}</h3></div><div class="pbody">${ins.length ? ins.map(field).join('') : '<p class="note">This script has no inputs.</p>'}</div>
    <div class="foot"><button class="btn ghost" data-a="def">Defaults</button><span class="grow"></span><button class="btn ghost" data-a="no">Cancel</button><button class="btn primary" data-a="ok">Apply</button></div></div>`);
  const vals = { ...(ind.inputs || {}) };
  el.onclick = e => {
    const b = e.target.closest('[data-b]'); if (b) { const k = b.dataset.b, cur = vals[k] !== undefined ? vals[k] : ins.find(i => String(i.idx) === k).defval; vals[k] = !cur; b.classList.toggle('on', vals[k]); return; }
    const a = e.target.closest('[data-a]'); if (!a) return;
    if (a.dataset.a === 'ok') { el.querySelectorAll('[data-k]').forEach(x => { const spec = ins.find(i => String(i.idx) === x.dataset.k); let v = x.dataset.num ? +x.value : x.value; if (spec && spec.type === 'color') { const orig = String(vals[x.dataset.k] ?? spec.defval); v = x.value === orig.slice(0, 7) ? orig : x.value + (orig.length === 9 ? orig.slice(7) : 'ff'); } vals[x.dataset.k] = v; }); W.updateIndicator(id, { inputs: vals }); }
    if (a.dataset.a === 'def') W.updateIndicator(id, { inputs: {} });
    closeModal();
  };
  modal(el);
}
