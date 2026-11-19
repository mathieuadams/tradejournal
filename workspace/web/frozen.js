// Frozen strategies: freeze a Pine strategy (script + inputs locked) from the chart, the editor or Research, and the
// "Frozen" side panel that lists them. The workspace server paper trades each one after every bar close.
import { W } from './ctx.js';
import { h, esc, api, toast, money, modal, closeModal } from './util.js';
import { nyParts, fmtPrice } from './chart.js';
import { compile, securityRequests } from './pine.js';

const cls = v => v > 0 ? 'up' : v < 0 ? 'dn' : '';
const fmtD = t => { const p = nyParts(t); return `${p.mon} ${p.d} ${p.y}`; };
const fmtDT = t => { const p = nyParts(t); return `${p.mon} ${p.d} ${String(p.h).padStart(2, '0')}:${String(p.mi).padStart(2, '0')}`; };
const ago = t => { if (!t) return '—'; const s = Date.now() / 1000 - t; return s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : s < 129600 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`; };
const slim = m => m && { trades: m.n, net: +(+m.net).toFixed(2), win_rate: +(+m.winRate || 0).toFixed(1), profit_factor: Number.isFinite(m.pf) ? +m.pf.toFixed(2) : null, max_drawdown: +(+m.maxDD || 0).toFixed(2) };

// ------------------------------------------------------------------------------------------------ freeze
// o: { src, name, inputs, symbol, tf, com, slip, sec, labels, research }
export function freezeDialog(o) {
  if (!/^\s*strategy\s*\(/m.test(o.src || '')) { toast('Only strategies can be frozen: the script needs strategy(...)', 'err'); return; }
  const lab = o.labels ? Object.entries(o.labels).map(([k, v]) => `<span class="fz-chip">${esc(k)}: <b>${esc(String(v))}</b></span>`).join('') : '';
  const rs = o.research && o.research.test;
  const el = h(`<div class="dialog" style="width:min(560px,94vw)"><div class="ph"><h3>❄ Freeze → paper trading</h3></div><div class="pbody rs">
    <p class="note" style="margin-top:0">“${esc(o.name)}” on <b>${esc(o.symbol)} ${esc(o.tf)}</b> is locked with the inputs below. From now on the server runs it after every ${esc(o.tf)} bar close; the trades it takes from this moment are its paper record (also on the journal's Running page). Later edits to the script don't change it.</p>
    ${lab ? `<div class="fz-chips">${lab}</div>` : ''}
    ${rs ? `<div class="note">Backtest snapshot kept with it: ${money(rs.net, 0)} · ${rs.trades} trades${rs.profit_factor != null ? ' · PF ' + rs.profit_factor : ''}</div>` : ''}
    <div class="row"><label class="field" style="flex:2"><span>Name</span><input id="fzName" value="${esc(o.name)} · ${esc(o.symbol)} ${esc(o.tf)}"></label></div>
    <div class="row"><label class="field" style="flex:2"><span>Account</span><select id="fzAcct"><option value="paper">Paper account: mirror its positions</option><option value="ledger">Its own ledger only</option></select></label>
      <label class="field"><span>Commission $ / unit / side</span><input id="fzCom" type="number" step="0.001" value="${o.com ?? 0.005}"></label><label class="field"><span>Slippage (ticks)</span><input id="fzSlip" type="number" step="1" value="${o.slip ?? 1}"></label></div>
  </div><div class="foot"><button class="btn ghost" data-a="no">Cancel</button><button class="btn primary" data-a="yes">❄ Freeze and start</button></div></div>`);
  el.querySelector('[data-a=no]').onclick = () => closeModal();
  el.querySelector('[data-a=yes]').onclick = async () => {
    let sec = o.sec;
    if (!sec) { try { sec = securityRequests(compile(o.src), W.activeBars(), { symbol: o.symbol, tf: o.tf }); } catch (e) { sec = []; } }
    const body = { name: el.querySelector('#fzName').value || o.name, account: el.querySelector('#fzAcct').value, src: o.src, inputs: o.inputs || null, inputLabels: o.labels || null,
      symbol: o.symbol, tf: o.tf, strategy: { comType: 'cash_per_contract', comVal: Math.max(0, +el.querySelector('#fzCom').value || 0), slip: Math.max(0, +el.querySelector('#fzSlip').value || 0) },
      sec: (sec || []).map(r => ({ sym: r.sym, tf: r.tf })), research: o.research || null };
    try { await api('frozen', body); closeModal(); toast('Frozen: paper trading from the next bar close', 'ok'); if (W.currentSide() === 'frozen') refresh(); else W.openSide('frozen'); }
    catch (e) { toast(e.message, 'err'); }
  };
  modal(el);
}

// a strategy on the chart: its script, the inputs it runs with now, its backtest on the loaded bars
export function freezeIndicator(id) {
  const tab = W.tab(), ind = tab.indicators.find(i => i.id === id); if (!ind) return;
  const sc = W.scriptFor(ind), d = W.indOut.find(x => x.id === id), out = d && d.out;
  if (!out || !out.strategy) { toast('Only strategies can be frozen: the script needs strategy(...)', 'err'); return; }
  const ins = (out.inputs || []).filter(Boolean), inputs = Object.fromEntries(ins.map(i => [i.idx, i.value]));
  const b = W.activeBars(), m = out.strategy.metrics;
  freezeDialog({ src: sc.src, name: sc.name, inputs, symbol: tab.symbol, tf: tab.tf, labels: Object.fromEntries(ins.map(i => [i.title, i.value])),
    research: b.length ? { from: b[0].t, to: b[b.length - 1].t, chart: true, test: slim(m), train: null } : null });
}

// ------------------------------------------------------------------------------------------------ side panel
let root = null, list = null, timer = null;
export function mountFrozen(el) {
  root = el; el.innerHTML = '';
  el.appendChild(h(`<div class="ph"><h3>Frozen strategies</h3><span class="dim" style="font-size:11.5px">paper trading on the server</span><span class="grow"></span><button class="icon-btn x" title="Close">✕</button></div>`));
  el.appendChild(h(`<div class="pbody rs" id="fzBody"><div class="dim">Loading…</div></div>`));
  el.querySelector('.ph .x').onclick = () => W.openSide('frozen');
  el.querySelector('#fzBody').onclick = e => { const a = e.target.closest('[data-fz]'); if (a) act(a.dataset.fz, a.dataset.act); if (e.target.closest('[data-a=freeze-chart]')) freezeFromChart(); };
  refresh();
  clearInterval(timer); timer = setInterval(() => { if (root && root.isConnected) refresh(); else clearInterval(timer); }, 30000);
}
function freezeFromChart() {
  const strats = W.indOut.filter(d => d.out && d.out.strategy);
  if (!strats.length) { toast('No strategy on the chart: add one from the library (Strategies) or the Pine editor', 'err'); return; }
  freezeIndicator(strats[0].id);
}
async function refresh() {
  const el = root && root.querySelector('#fzBody'); if (!el) return;
  try { list = (await api('frozen')).frozen; } catch (e) { el.innerHTML = `<div class="note warn-box">${esc(e.message)}</div>`; return; }
  const strats = W.indOut.filter(d => d.out && d.out.strategy);
  el.innerHTML = `<div class="row" style="margin-bottom:10px;flex-wrap:wrap">${strats.map(d => `<button class="btn sm primary" data-fz="${d.id}" data-act="freeze-ind">❄ Freeze “${esc(d.out.meta.title || d.title)}” (${esc(W.tab().symbol)} ${esc(W.tab().tf)})</button>`).join('') || '<span class="note">Put a strategy on the chart (library → Strategies, or the Pine editor) to freeze it, or use ❄ in its legend.</span>'}</div>
    ${list.length ? list.map(card).join('') : '<p class="note">Nothing frozen yet.</p>'}`;
}
function card(f) {
  const m = f.metrics || {}, tst = f.research && f.research.test;
  return `<div class="fz-card ${f.status}"><div class="fz-hd"><b>${esc(f.name)}</b><span class="fz-st">${f.status === 'on' ? '● running' : '○ stopped'}</span><span class="grow"></span>
    <button class="btn sm" data-fz="${f.id}" data-act="${f.status === 'on' ? 'stop' : 'start'}">${f.status === 'on' ? 'Stop' : 'Start'}</button>
    <button class="btn sm" data-fz="${f.id}" data-act="${f.account === 'paper' ? 'ledger' : 'paper'}" title="${f.account === 'paper' ? 'Stop mirroring into the paper account' : 'Mirror its positions into the paper account'}">${f.account === 'paper' ? 'Paper acct ✓' : 'Ledger only'}</button>
    <button class="btn sm" data-fz="${f.id}" data-act="chart" title="Load the frozen script and inputs on the chart">Chart</button>
    <button class="icon-btn" data-fz="${f.id}" data-act="delete" title="Delete">✕</button></div>
    <div class="fz-sub dim">${esc(f.symbol)} ${esc(f.tf)} · frozen ${fmtD(f.frozenAt)} · last run ${ago(f.lastRun)}${(f.sec || []).length ? ' · data ' + f.sec.map(x => esc(x.sym)).join(', ') : ''}</div>
    <div class="fz-kv"><div><span>Paper trades</span><b>${m.n || 0}</b></div><div><span>Net</span><b class="${cls(m.net)}">${money(m.net || 0, 0)}</b></div><div><span>Win</span><b>${m.n ? m.winRate + '%' : '—'}</b></div>
      <div><span>Open</span><b class="${cls(f.openPnl)}">${(f.open || []).length ? `${f.open.map(o => (o.dir > 0 ? '+' : '−') + o.qty).join(' ')} · ${money(f.openPnl, 0)}` : 'flat'}</b></div>
      <div><span>${f.research && f.research.chart ? 'Backtest (chart)' : 'Research test'}</span><b class="${tst ? cls(tst.net) : ''}">${tst ? `${money(tst.net, 0)} · ${tst.trades} tr` : '—'}</b></div></div>
    ${f.error ? `<div class="note warn-box">${esc(f.error)}</div>` : ''}
    ${(f.recent || []).length ? `<table class="tbl rs-res"><tr><th class="t">Entry</th><th>Side</th><th>In</th><th>Out</th><th>P&L</th></tr>${f.recent.slice().reverse().map(t => `<tr><td class="t">${fmtDT(t.entryTime)}</td><td class="${t.dir > 0 ? 'up' : 'dn'}">${t.dir > 0 ? 'Long' : 'Short'}</td><td>${fmtPrice(t.entryPx)}</td><td>${fmtPrice(t.exitPx)}</td><td class="${cls(t.pnl)}">${money(t.pnl)}</td></tr>`).join('')}</table>` : ''}</div>`;
}
async function act(id, a) {
  if (a === 'freeze-ind') { freezeIndicator(id); return; }
  if (a === 'chart') {
    const f = (await api('frozen/' + id)).frozen;
    if (W.tab().symbol !== f.symbol) await W.setSymbol(f.symbol); if (W.tab().tf !== f.tf) await W.setTf(f.tf);
    W.addIndicator({ src: f.src, name: '❄ ' + f.name, inputs: { ...(f.inputs || {}) } });
    toast('Frozen script on the chart (a copy: editing it does not change the frozen one)', 'ok'); return;
  }
  if (a === 'delete' && !confirm('Delete this frozen strategy and its paper record?')) return;
  try { await api('frozen/' + id, { action: a }); refresh(); } catch (e) { toast(e.message, 'err'); }
}
