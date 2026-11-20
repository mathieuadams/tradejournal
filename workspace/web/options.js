// Options panel: the option chain of the chart's symbol (Alpaca, or Black-Scholes on simulated data), an order ticket
// for the Alpaca paper account, and its option positions.
import { W } from './ctx.js';
import { h, esc, api, toast, money } from './util.js';

const O = { sym: null, exps: [], exp: null, chain: null, n: 12, cols: 'basic', sel: null, err: null, source: '', busy: false };
let root = null, timer = null;

const f2 = v => v == null || !Number.isFinite(+v) ? '—' : (+v).toFixed(2);
const pct = v => v == null || !Number.isFinite(+v) ? '—' : (v * 100).toFixed(1) + '%';
const dl = v => v == null || !Number.isFinite(+v) ? '—' : (+v).toFixed(2);
const int = v => v == null ? '—' : (+v >= 1000 ? (v / 1000).toFixed(1) + 'k' : String(v));
const dte = e => { const d = Math.round((Date.parse(e + 'T16:00:00-05:00') - Date.now()) / 86400000); return d <= 0 ? '0d' : d + 'd'; };
const expLabel = e => { const d = new Date(e + 'T12:00:00Z'); return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' }); };

export function mountOptions(el) {
  root = el; el.innerHTML = '';
  el.appendChild(h(`<div class="ph"><h3>Option chain</h3><span class="dim" id="opSrc" style="font-size:11.5px"></span><span class="grow"></span><button class="icon-btn" id="opRef" title="Refresh">⟳</button><button class="icon-btn x" title="Close">✕</button></div>`));
  el.appendChild(h(`<div class="pbody op" id="opBody"></div>`));
  el.querySelector('.ph .x').onclick = () => W.openSide('options');
  el.querySelector('#opRef').onclick = () => load(true);
  const body = el.querySelector('#opBody');
  body.onclick = e => {
    const x = e.target.closest('[data-exp]'); if (x) { O.exp = x.dataset.exp; O.sel = null; loadChain(); return; }
    const c = e.target.closest('[data-pick]'); if (c) { pick(c.dataset.pick, c.dataset.side); return; }
    const a = e.target.closest('[data-a]'); if (!a) return;
    if (a.dataset.a === 'place') place();
    if (a.dataset.a === 'unsel') { O.sel = null; render(); }
    if (a.dataset.a === 'close') closePos(a.dataset.sym, +a.dataset.qty);
    if (a.dataset.a === 'side') { O.sel.side = a.dataset.v; O.sel.limit = defLimit(); render(); }
  };
  body.onchange = e => {
    const t = e.target;
    if (t.id === 'opN') { O.n = +t.value; render(); }
    if (t.id === 'opCols') { O.cols = t.value; render(); }
  };
  body.oninput = e => { const t = e.target; if (O.sel && t.id === 'opQty') { O.sel.qty = Math.max(1, Math.floor(+t.value || 1)); ticketMath(); } if (O.sel && t.id === 'opLim') { O.sel.limit = +t.value; ticketMath(); } };
  load();
  clearInterval(timer);
  timer = setInterval(() => { if (!root || !root.isConnected) return clearInterval(timer); if (W.tab().symbol !== O.sym) load(); else if (!O.busy) loadChain(true); }, 10000);
}

async function load(force) {
  const sym = W.tab().symbol;
  if (sym !== O.sym || force || !O.exps.length) {
    O.sym = sym; O.exps = []; O.exp = null; O.chain = null; O.sel = null; O.err = null; render();
    try {
      const r = await api(`options/expirations?symbol=${encodeURIComponent(sym)}`);
      O.exps = r.expirations || []; O.source = r.source;
      if (!O.exps.length) { O.err = `No listed options for ${sym}.`; render(); return; }
      O.exp = O.exps[0];
    } catch (e) { O.err = e.message; render(); return; }
  }
  loadChain();
}

async function loadChain(quiet) {
  if (!O.exp) return;
  O.busy = true; if (!quiet) { O.chain = O.chain && O.chain.exp === O.exp ? O.chain : null; render(); }
  try { O.chain = await api(`options/chain?symbol=${encodeURIComponent(O.sym)}&exp=${O.exp}`); O.err = null; }
  catch (e) { O.err = e.message; }
  O.busy = false;
  if (O.sel) { const leg = findLeg(O.sel.symbol); if (leg) O.sel.leg = leg; }
  render(quiet);
  loadPositions();
}

function findLeg(sym) {
  for (const r of (O.chain && O.chain.rows) || []) for (const k of ['call', 'put']) if (r[k] && r[k].symbol === sym) return { ...r[k], strike: r.strike, call: k === 'call' };
  return null;
}
const defLimit = () => { const l = O.sel.leg, mid = l.bid > 0 && l.ask > 0 ? (l.bid + l.ask) / 2 : l.last || 0; return Math.max(0.01, +mid.toFixed(2)); };
function pick(sym, side) {
  const leg = findLeg(sym); if (!leg) return;
  O.sel = { symbol: sym, leg, side: side === 'bid' ? 'sell' : 'buy', qty: (O.sel && O.sel.qty) || 1 };
  O.sel.limit = defLimit(); render();
  const t = root.querySelector('.op-ticket'); if (t) t.scrollIntoView({ block: 'nearest' });
}

function render(quiet) {
  const el = root && root.querySelector('#opBody'); if (!el) return;
  const src = root.querySelector('#opSrc'); if (src) src.textContent = O.source === 'alpaca' ? 'Alpaca · indicative quotes' : O.source ? 'simulated · Black-Scholes' : '';
  const C = O.chain, keepScroll = quiet && el.querySelector('.op-wrap') ? el.querySelector('.op-wrap').scrollTop : null;
  const exps = O.exps.length ? `<div class="op-exps">${O.exps.slice(0, 40).map(e => `<button class="${e === O.exp ? 'on' : ''}" data-exp="${e}"><b>${expLabel(e)}</b><span>${dte(e)}</span></button>`).join('')}</div>` : '';
  const head = `<div class="op-head"><div><b class="op-sym">${esc(O.sym || '')}</b> ${C ? `<span class="mono">${f2(C.spot)}</span>` : ''}</div><span class="grow"></span>
    <label class="op-mini">Strikes <select id="opN">${[8, 12, 20, 40, 999].map(n => `<option value="${n}" ${O.n === n ? 'selected' : ''}>${n === 999 ? 'All' : '±' + n}</option>`).join('')}</select></label>
    <label class="op-mini">Columns <select id="opCols"><option value="basic" ${O.cols === 'basic' ? 'selected' : ''}>Price · IV · Δ</option><option value="greeks" ${O.cols === 'greeks' ? 'selected' : ''}>Greeks</option></select></label></div>`;
  if (O.err && !C) { el.innerHTML = head + exps + `<div class="note warn-box">${esc(O.err)}</div>`; return; }
  if (!C) { el.innerHTML = head + exps + '<div class="dim" style="padding:20px">Loading the chain…</div>'; return; }
  const rows = C.rows, spot = C.spot;
  let atm = 0; rows.forEach((r, i) => { if (Math.abs(r.strike - spot) < Math.abs(rows[atm].strike - spot)) atm = i; });
  const vis = O.n >= 999 ? rows : rows.slice(Math.max(0, atm - O.n), atm + O.n + 1);
  const G = O.cols === 'greeks';
  const cCols = G ? ['Γ', 'Θ', 'V', 'Δ', 'Bid', 'Ask'] : ['OI', 'Vol', 'IV', 'Δ', 'Bid', 'Ask'];
  const pCols = G ? ['Bid', 'Ask', 'Δ', 'V', 'Θ', 'Γ'] : ['Bid', 'Ask', 'Δ', 'IV', 'Vol', 'OI'];
  const cell = (leg, c, itm) => {
    const k = itm ? 'itm' : '';
    if (!leg) return `<td class="dim ${k}">—</td>`;
    const sel = O.sel && O.sel.symbol === leg.symbol, it = leg.model ? ' it' : '';
    switch (c) {
      case 'Bid': return `<td class="pb ${k} ${sel && O.sel.side === 'sell' ? 'sel' : ''}" data-pick="${leg.symbol}" data-side="bid" title="Sell at the bid">${f2(leg.bid)}</td>`;
      case 'Ask': return `<td class="pa ${k} ${sel && O.sel.side === 'buy' ? 'sel' : ''}" data-pick="${leg.symbol}" data-side="ask" title="Buy at the ask">${f2(leg.ask)}</td>`;
      case 'IV': return `<td class="${k}${it}">${pct(leg.iv)}</td>`;
      case 'Δ': return `<td class="${k}${it}">${dl(leg.delta)}</td>`;
      case 'Γ': return `<td class="${k}">${leg.gamma == null ? '—' : (+leg.gamma).toFixed(3)}</td>`;
      case 'Θ': return `<td class="${k}">${f2(leg.theta)}</td>`;
      case 'V': return `<td class="${k}">${f2(leg.vega)}</td>`;
      case 'OI': return `<td class="${k}">${int(leg.oi)}</td>`;
      case 'Vol': return `<td class="${k}">${int(leg.vol)}</td>`;
    }
    return '<td></td>';
  };
  let tb = '';
  vis.forEach((r, i) => {
    const prev = vis[i - 1];
    if (prev && prev.strike < spot && r.strike >= spot) tb += `<tr class="op-spot"><td colspan="13"><span>${esc(O.sym)} ${f2(spot)}</span></td></tr>`;
    tb += `<tr>${cCols.map(c => cell(r.call, c, r.strike < spot)).join('')}<td class="op-k">${r.strike % 1 ? r.strike.toFixed(2) : r.strike}</td>${pCols.map(c => cell(r.put, c, r.strike > spot)).join('')}</tr>`;
  });
  el.innerHTML = head + exps + `${O.err ? `<div class="note warn-box">${esc(O.err)}</div>` : ''}
    <div class="op-sides"><span>Calls</span><span>Puts</span></div><div class="op-wrap"><table class="op-tbl"><thead>
      <tr>${cCols.map(c => `<th>${c}</th>`).join('')}<th class="op-k">Strike</th>${pCols.map(c => `<th>${c}</th>`).join('')}</tr></thead><tbody>${tb}</tbody></table></div>
    <div class="op-foot dim">Click an ask to buy, a bid to sell. <i>Italic</i> IV / Δ: computed with Black-Scholes from the mid quote. ${dte(O.exp)} to ${expLabel(O.exp)} 16:00 ET.</div>
    ${ticket()}
    <div id="opPos"></div>`;
  if (keepScroll != null) el.querySelector('.op-wrap').scrollTop = keepScroll;
  else { const s = el.querySelector('.op-spot'); const w = el.querySelector('.op-wrap'); if (s && w) w.scrollTop = Math.max(0, s.offsetTop - w.clientHeight / 2); }
  ticketMath(); renderPositions();
}

function ticket() {
  if (!O.sel) return '';
  const l = O.sel.leg, buy = O.sel.side === 'buy';
  return `<div class="op-ticket"><div class="op-thd"><b>${esc(O.sym)} ${expLabel(O.exp)} ${l.strike % 1 ? l.strike.toFixed(2) : l.strike} ${l.call ? 'Call' : 'Put'}</b><span class="dim mono">${esc(l.symbol)}</span><span class="grow"></span><button class="icon-btn" data-a="unsel">✕</button></div>
    <div class="op-q dim">Bid <b>${f2(l.bid)}</b> · Ask <b>${f2(l.ask)}</b> · Last ${f2(l.last)} · IV ${pct(l.iv)} · Δ ${dl(l.delta)} · Θ ${f2(l.theta)}/day</div>
    <div class="seg" style="margin:8px 0"><button class="${buy ? 'on buy' : ''}" data-a="side" data-v="buy">Buy to open / close</button><button class="${buy ? '' : 'on sell'}" data-a="side" data-v="sell">Sell to open / close</button></div>
    <div class="row"><label class="field"><span>Contracts</span><input id="opQty" type="number" min="1" step="1" value="${O.sel.qty}"></label><label class="field"><span>Limit (per share)</span><input id="opLim" type="number" min="0.01" step="0.01" value="${O.sel.limit}"></label></div>
    <div class="op-math" id="opMath"></div>
    <button class="btn ${buy ? 'buy' : 'sell'} wide" data-a="place">${buy ? 'Buy' : 'Sell'} ${O.sel.qty} × ${l.call ? 'call' : 'put'} @ ${f2(O.sel.limit)} limit</button>
    <div class="note" style="text-align:center">Sent to your Alpaca paper account (day order).</div></div>`;
}

function ticketMath() {
  const el = root && root.querySelector('#opMath'); if (!el || !O.sel) return;
  const l = O.sel.leg, q = O.sel.qty, lp = +O.sel.limit || 0, cost = lp * 100 * q, buy = O.sel.side === 'buy';
  const be = l.call ? l.strike + lp : l.strike - lp;
  el.innerHTML = `<div><span>${buy ? 'Cost' : 'Credit'}</span><b>${money(cost)}</b></div><div><span>Break-even at expiry</span><b>${f2(be)}</b></div>
    <div><span>${buy ? 'Max loss' : 'Max gain'}</span><b>${money(cost)}</b></div><div><span>Δ exposure</span><b>${l.delta == null ? '—' : ((buy ? 1 : -1) * l.delta * 100 * q).toFixed(0) + ' sh'}</b></div>`;
  const b = root.querySelector('[data-a=place]'); if (b) b.textContent = `${buy ? 'Buy' : 'Sell'} ${q} × ${l.call ? 'call' : 'put'} @ ${f2(lp)} limit`;
}

async function place() {
  const s = O.sel; if (!s) return;
  if (!(s.limit > 0)) return toast('Limit price?', 'err');
  if (!confirm(`${s.side === 'buy' ? 'Buy' : 'Sell'} ${s.qty} ${s.symbol} at ${s.limit.toFixed(2)} limit on your Alpaca paper account?`)) return;
  try { const r = await api('options/order', { symbol: s.symbol, side: s.side, qty: s.qty, type: 'limit', limit: s.limit }); toast(`Order ${r.order.status}: ${s.side} ${s.qty} ${s.symbol} @ ${s.limit.toFixed(2)}`, 'ok'); setTimeout(loadPositions, 1500); }
  catch (e) { toast(e.message, 'err'); }
}

let POS = null;
async function loadPositions() { try { POS = await api('options/positions'); } catch (e) { POS = { error: e.message, positions: [] }; } renderPositions(); }
function renderPositions() {
  const el = root && root.querySelector('#opPos'); if (!el || !POS) return;
  if (POS.error) { el.innerHTML = `<div class="note warn-box">Alpaca: ${esc(POS.error)}</div>`; return; }
  if (!POS.configured) { el.innerHTML = '<div class="note">Option orders go to your Alpaca paper account: save the Alpaca key on the journal\'s Data page.</div>'; return; }
  el.innerHTML = `<div class="tree-sec">Option positions · Alpaca paper</div>${POS.positions.length ? `<table class="tbl rs-res"><tr><th class="t">Contract</th><th>Qty</th><th>Avg</th><th>Mark</th><th>Value</th><th>P&L</th><th></th></tr>${POS.positions.map(p => `<tr><td class="t mono">${esc(p.symbol)}</td><td class="${p.qty > 0 ? 'up' : 'dn'}">${p.qty}</td><td>${f2(p.avg)}</td><td>${f2(p.price)}</td><td>${money(p.value, 0)}</td><td class="${p.upl > 0 ? 'up' : p.upl < 0 ? 'dn' : ''}">${money(p.upl)}</td><td><button class="btn sm" data-a="close" data-sym="${esc(p.symbol)}" data-qty="${p.qty}">Close</button></td></tr>`).join('')}</table>` : '<div class="dim" style="font-size:12px">No option positions.</div>'}`;
}
async function closePos(sym, qty) {
  const leg = findLeg(sym), side = qty > 0 ? 'sell' : 'buy';
  const px = leg ? (side === 'sell' ? leg.bid : leg.ask) : 0;
  const lim = prompt(`Close ${Math.abs(qty)} ${sym}: limit price`, px ? px.toFixed(2) : ''); if (lim == null) return;
  try { await api('options/order', { symbol: sym, side, qty: Math.abs(qty), type: +lim > 0 ? 'limit' : 'market', limit: +lim }); toast('Close order sent', 'ok'); setTimeout(loadPositions, 1500); } catch (e) { toast(e.message, 'err'); }
}
