// Indicator library, symbol search and the timeframe picker.
import { W } from './ctx.js';
import { LIBRARY, CATEGORIES } from './library.js';
import { $, h, esc, api, modal, closeModal, popover, closePopover, toast, debounce } from './util.js';
import { openInEditor } from './editor.js';
import { focusChat } from './chat.js';

// ------------------------------------------------------------------------------------------------ library
export function openLibrary(initial = '') {
  let nav = W.state.favorites.length ? 'fav' : 'all', q = initial, sel = 0, items = [];
  const el = h(`<div class="lib" style="display:flex;flex-direction:column">
    <div class="lib-top"><h3>Indicators</h3><input placeholder="Search indicators, concepts or aliases" value="${esc(q)}"><button class="icon-btn" data-x>✕</button></div>
    <div class="lib-main"><div class="lib-nav"></div><div class="lib-grid"></div><div class="lib-det"></div></div>
    <div class="lib-foot"><span><kbd>↑↓</kbd>Browse</span><span><kbd>↵</kbd>Add</span><span><kbd>⇧↵</kbd>Add, keep open</span><span><kbd>F</kbd>Favorite</span><span><kbd>Esc</kbd>Close</span><span class="grow"></span><span>${LIBRARY.length} built-in · ${W.state.scripts.length} yours</span></div></div>`);
  const input = el.querySelector('input'), navEl = el.querySelector('.lib-nav'), grid = el.querySelector('.lib-grid'), det = el.querySelector('.lib-det');
  el.querySelector('[data-x]').onclick = closeModal;
  const mine = () => W.state.scripts.map(s => ({ id: s.id, name: s.name, cat: 'My scripts', desc: 'Your script · edited ' + new Date(s.updated || Date.now()).toLocaleDateString(), src: s.src, mine: true }));
  function list() {
    let L = nav === 'fav' ? [...LIBRARY, ...mine()].filter(s => W.state.favorites.includes(s.id)) : nav === 'mine' ? mine() : nav === 'all' ? [...LIBRARY, ...mine()] : LIBRARY.filter(s => s.cat === nav);
    if (q) {
      const t = q.toLowerCase(), words = s => (s.name + ' ' + (s.aliases || '') + ' ' + s.id).toLowerCase().split(/[^a-z0-9/]+/);
      const score = s => s.id === t || words(s).includes(t) ? 0 : s.name.toLowerCase().startsWith(t) ? 1 : words(s).some(w => w.startsWith(t)) ? 2 : (s.name + ' ' + (s.aliases || '')).toLowerCase().includes(t) ? 3 : (s.cat + ' ' + (s.desc || '')).toLowerCase().includes(t) ? 4 : 9;
      L = [...LIBRARY, ...mine()].map(s => [score(s), s]).filter(x => x[0] < 9).sort((a, b) => a[0] - b[0]).map(x => x[1]);
    }
    return L;
  }
  function renderNav() {
    const c = k => k === 'fav' ? [...LIBRARY, ...mine()].filter(s => W.state.favorites.includes(s.id)).length : k === 'mine' ? W.state.scripts.length : k === 'all' ? LIBRARY.length + W.state.scripts.length : LIBRARY.filter(s => s.cat === k).length;
    navEl.innerHTML = `<div class="sec">Yours</div>${[['fav', '☆ Favorites'], ['mine', '◇ My scripts']].map(([k, n]) => `<button data-n="${k}" class="${nav === k ? 'on' : ''}">${n}<span>${c(k)}</span></button>`).join('')}
      <button data-n="new">＋ New script</button>
      <div class="sec">Library</div><button data-n="all" class="${nav === 'all' ? 'on' : ''}">All scripts<span>${c('all')}</span></button>
      <div class="sec">Families</div>${CATEGORIES.map(k => `<button data-n="${esc(k)}" class="${nav === k ? 'on' : ''}">${esc(k)}<span>${c(k)}</span></button>`).join('')}`;
  }
  function render() {
    items = list(); sel = Math.min(sel, Math.max(0, items.length - 1)); renderNav();
    grid.innerHTML = items.length ? '' : '<div class="empty" style="grid-column:1/-1">Nothing here yet.</div>';
    items.forEach((s, i) => {
      const c = h(`<div class="card ${i === sel ? 'on' : ''}"><canvas width="300" height="128"></canvas><b>${esc(s.name)}</b><small>${esc(s.cat)}</small><button class="star ${W.state.favorites.includes(s.id) ? 'on' : ''}" title="Favorite">★</button></div>`);
      c.onclick = e => { if (e.target.closest('.star')) { fav(s); return; } sel = i; mark(); detail(); };
      c.ondblclick = () => add(s, false);
      grid.appendChild(c); lazyPreview(c.querySelector('canvas'), s);
    });
    detail();
  }
  function mark() { [...grid.children].forEach((c, i) => c.classList.toggle('on', i === sel)); const c = grid.children[sel]; if (c && c.scrollIntoView) c.scrollIntoView({ block: 'nearest' }); }
  function detail() {
    const s = items[sel]; if (!s) { det.innerHTML = ''; return; }
    det.innerHTML = `<canvas width="560" height="300"></canvas><div class="dim" style="font-size:11.5px">${esc(s.cat)}${s.builtin ? ' · built-in' : ''}</div><h2>${esc(s.name)}</h2><p>${esc(s.desc || '')}</p>
      <div class="grow"></div><button class="btn primary" data-a="add">Add to chart</button><div class="row"><button class="btn" data-a="fav">${W.state.favorites.includes(s.id) ? '★ Favorited' : '☆ Favorite'}</button><button class="btn" data-a="src">Open source</button></div><button class="btn" data-a="ask">Ask Quant about it</button>`;
    preview(det.querySelector('canvas'), s, true);
    det.onclick = e => {
      const a = e.target.closest('[data-a]'); if (!a) return;
      if (a.dataset.a === 'add') add(s, false);
      if (a.dataset.a === 'fav') fav(s);
      if (a.dataset.a === 'src') { closeModal(); openInEditor(s.mine ? { scriptId: s.id } : { copyOf: s.id }); }
      if (a.dataset.a === 'ask') { closeModal(); if (!W.state.chatOpen) W.toggleChat(true); focusChat(`Explain the "${s.name}" indicator and how I could use it on ${W.tab().symbol} ${W.tab().tf}.`); }
    };
  }
  function fav(s) { const f = W.state.favorites; const i = f.indexOf(s.id); if (i >= 0) f.splice(i, 1); else f.push(s.id); W.save(); render(); }
  function add(s, keep) { W.addIndicator({ scriptId: s.id }); toast(`Added ${s.name}`); if (!keep) closeModal(); }
  navEl.onclick = e => { const b = e.target.closest('[data-n]'); if (!b) return; if (b.dataset.n === 'new') { closeModal(); openInEditor({ fresh: true }); return; } nav = b.dataset.n; q = ''; input.value = ''; sel = 0; render(); };
  input.oninput = debounce(() => { q = input.value.trim(); sel = 0; render(); }, 120);
  el.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') { e.preventDefault(); sel = Math.min(items.length - 1, sel + (e.key === 'ArrowDown' ? 3 : 1)); mark(); detail(); }
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') { e.preventDefault(); sel = Math.max(0, sel - (e.key === 'ArrowUp' ? 3 : 1)); mark(); detail(); }
    else if (e.key === 'Enter' && items[sel]) { e.preventDefault(); add(items[sel], e.shiftKey); }
    else if ((e.key === 'f' || e.key === 'F') && e.target !== input && items[sel]) fav(items[sel]);
  });
  modal(el, { cls: 'libmodal' }); render(); input.focus();
}

let io = null;
function lazyPreview(cv, s) {
  io = io || new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { io.unobserve(e.target); preview(e.target, e.target._s); } }), { root: null });
  cv._s = s; io.observe(cv);
}
function preview(cv, s, big) {
  const g = cv.getContext('2d'), W2 = cv.width, H = cv.height; g.fillStyle = '#0e1015'; g.fillRect(0, 0, W2, H);
  const all = W.activeBars(); if (!all.length) return;
  const bars = all.slice(-(big ? 320 : 220)), show = big ? 160 : 110, off = bars.length - show;
  let out; try { out = W.runScript(s.src, bars, null); } catch { return; }
  if (out.error) { g.fillStyle = '#f23645'; g.font = '11px Inter'; g.fillText('⚠ ' + out.error.message.slice(0, 50), 8, H / 2); return; }
  const overlay = out.meta.overlay, X = i => (i - off) / (show - 1) * (W2 - 8) + 4;
  const priceH = overlay ? H : H * 0.45, subY = priceH, subH = H - priceH;
  let lo = Infinity, hi = -Infinity; for (let i = off; i < bars.length; i++) { lo = Math.min(lo, bars[i].l); hi = Math.max(hi, bars[i].h); }
  const PY = p => 4 + (hi - p) / (hi - lo || 1) * (priceH - 8);
  const bw = Math.max(1, (W2 - 8) / show * 0.6);
  for (let i = off; i < bars.length; i++) { const b = bars[i]; g.fillStyle = g.strokeStyle = b.c >= b.o ? 'rgba(8,153,129,.75)' : 'rgba(242,54,69,.75)'; const x = X(i); g.beginPath(); g.moveTo(x, PY(b.h)); g.lineTo(x, PY(b.l)); g.stroke(); g.fillRect(x - bw / 2, Math.min(PY(b.o), PY(b.c)), bw, Math.max(1, Math.abs(PY(b.o) - PY(b.c)))); }
  let slo = Infinity, shi = -Infinity;
  if (!overlay || out.meta.scale === 'none') { for (const p of out.plots) for (let i = off; i < bars.length; i++) { const v = p.values[i]; if (Number.isFinite(v)) { slo = Math.min(slo, v); shi = Math.max(shi, v); } } for (const hl of out.hlines) { slo = Math.min(slo, hl.price); shi = Math.max(shi, hl.price); } }
  const own = out.meta.scale === 'none';
  const SY = v => own ? H - 2 - (v - Math.min(0, slo)) / ((shi - Math.min(0, slo)) || 1) * H * 0.25 : subY + 4 + (shi - v) / (shi - slo || 1) * (subH - 8);
  const Y = overlay && !own ? PY : SY;
  if (!overlay) { g.strokeStyle = '#262b36'; g.beginPath(); g.moveTo(0, subY); g.lineTo(W2, subY); g.stroke(); }
  for (const b of out.boxes) { if (b.right < off && b.extend === 'none') continue; g.fillStyle = b.bgcolor || 'rgba(41,98,255,.2)'; const x1 = X(Math.max(off, b.left)), x2 = b.extend === 'right' ? W2 : X(b.right); g.fillRect(x1, Math.min(PY(b.top), PY(b.bottom)), x2 - x1, Math.abs(PY(b.top) - PY(b.bottom))); }
  for (const p of out.plots) {
    if (p.display === 'none') continue; g.strokeStyle = (p.color || '#2962ff').slice(0, 7); g.fillStyle = g.strokeStyle; g.lineWidth = big ? 1.5 : 1;
    if (p.style === 'columns' || p.style === 'histogram') { for (let i = off; i < bars.length; i++) { const v = p.values[i]; if (!Number.isFinite(v)) continue; const c = p.colors ? p.colors[i] : p.color; if (!c) continue; g.fillStyle = c; const y0 = Y(own ? 0 : (p.histbase || 0)), y = Y(v); g.fillRect(X(i) - bw / 2, Math.min(y, y0), bw, Math.abs(y - y0)); } continue; }
    g.beginPath(); let pen = false;
    for (let i = off; i < bars.length; i++) { const v = p.values[i - (p.offset || 0)]; if (!Number.isFinite(v)) { pen = false; continue; } const x = X(i), y = Y(v); if (!pen) { g.moveTo(x, y); pen = true; } else g.lineTo(x, y); }
    g.stroke();
  }
  for (const hl of out.hlines) { g.strokeStyle = '#5d6370'; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(0, Y(hl.price)); g.lineTo(W2, Y(hl.price)); g.stroke(); g.setLineDash([]); }
  for (const l of out.lines) { if (l.x2 < off) continue; g.strokeStyle = (l.color || '#2962ff').slice(0, 7); g.setLineDash(l.style === 'solid' ? [] : [3, 3]); g.beginPath(); g.moveTo(X(Math.max(off, l.x1)), PY(l.y1)); g.lineTo(X(l.x2), PY(l.y2)); g.stroke(); g.setLineDash([]); }
  for (const s2 of out.shapes) for (let i = off; i < bars.length; i++) if (Number.isFinite(s2.values[i])) { const c = s2.colors ? s2.colors[i] : s2.color; if (!c) continue; g.fillStyle = c; const up = s2.location === 'belowbar'; g.beginPath(); g.arc(X(i), up ? PY(bars[i].l) + 6 : PY(bars[i].h) - 6, big ? 3 : 2, 0, 7); g.fill(); }
  for (const l of out.labels) { if (l.x < off) continue; g.fillStyle = (l.textcolor && !/ffffff/.test(l.textcolor) ? l.textcolor : l.color || '#d1d4dc').slice(0, 7); g.font = `${big ? 9 : 7}px Inter`; g.textAlign = 'center'; g.fillText(l.text, X(l.x), PY(l.y) + (l.style === 'label_up' ? 9 : -3)); }
}

// ------------------------------------------------------------------------------------------------ symbol search
export function openSymbolSearch(prefill = '', { newTab = false, onPick } = {}) {
  let sel = 0, results = [];
  const el = h(`<div class="search" style="display:flex;flex-direction:column"><div class="search-top"><input placeholder="Symbol, e.g. AAPL, BTC/USD" value="${esc(prefill)}"></div><div class="search-list"></div>
    <div class="lib-foot"><span><kbd>↵</kbd>Open</span><span><kbd>⇧↵</kbd>Open in a new tab</span><span><kbd>Esc</kbd>Close</span></div></div>`);
  const input = el.querySelector('input'), listEl = el.querySelector('.search-list');
  const pick = (r, nt) => { closeModal(); if (onPick) return onPick(r.symbol); if (nt || newTab) W.newTab(r.symbol); else W.setSymbol(r.symbol); };
  function render() {
    listEl.innerHTML = '';
    results.forEach((r, i) => {
      const inWl = W.state.watchlist.includes(r.symbol);
      const b = h(`<button class="${i === sel ? 'on' : ''}"><b>${esc(r.symbol)}</b><span>${esc(r.name)}</span><small>${esc(r.type)} <span class="wl" title="${inWl ? 'In watchlist' : 'Add to watchlist'}" style="margin-left:8px;padding:0 6px">${inWl ? '✓' : '＋'}</span></small></button>`);
      b.onclick = e => { if (e.target.closest('.wl')) { if (!inWl) { W.state.watchlist.push(r.symbol); W.save(); W.emit('watchlist'); W.resubscribe(); W.refreshQuotes(); } render(); return; } pick(r, e.shiftKey); };
      listEl.appendChild(b);
    });
    if (!results.length) listEl.innerHTML = '<div class="empty">No match</div>';
  }
  const search = debounce(async () => { try { results = (await api('symbols?q=' + encodeURIComponent(input.value.trim()))).results; } catch { results = []; } const t = input.value.trim().toUpperCase(); if (t && /^[A-Z.]{1,6}(\/[A-Z]{3,4})?$/.test(t) && !results.some(r => r.symbol === t)) results.push({ symbol: t, name: 'Open as typed', type: '' }); sel = 0; render(); }, 120);
  input.oninput = search;
  input.onkeydown = e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(results.length - 1, sel + 1); render(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(0, sel - 1); render(); }
    if (e.key === 'Enter' && results[sel]) { e.preventDefault(); pick(results[sel], e.shiftKey); }
  };
  modal(el); search(); input.focus(); input.setSelectionRange(input.value.length, input.value.length);
}

// ------------------------------------------------------------------------------------------------ timeframe
export function openTfPopover(anchor, prefill = '') {
  const cur = W.tab().tf, btn = t => `<button data-tf="${t}" class="${t === cur ? 'on' : ''}">${t}</button>`;
  const el = h(`<div style="width:300px"><input placeholder="Type any timeframe: 22m, 3h, 2D" value="${esc(prefill)}" style="width:100%">
    <div class="pop-sec">Minutes</div><div class="pop-grid">${['1m', '3m', '5m', '15m', '30m', '45m'].map(btn).join('')}</div>
    <div class="pop-sec">Hours</div><div class="pop-grid">${['1h', '2h', '4h', '6h'].map(btn).join('')}</div>
    <div class="pop-sec">Days and up</div><div class="pop-grid">${['1D', '2D', '1W', '1M', '3M'].map(btn).join('')}</div>
    <div class="note" style="margin-top:8px">↵ chart it · type a number on the chart to open this</div></div>`);
  const input = el.querySelector('input');
  el.onclick = e => { const b = e.target.closest('[data-tf]'); if (b) { closePopover(); W.setTf(b.dataset.tf); } };
  input.onkeydown = async e => { if (e.key === 'Enter') { const tf = W.normTfStrict(input.value); if (!tf) { input.style.borderColor = 'var(--dn)'; return; } closePopover(); W.setTf(tf); } };
  popover(anchor, el); input.focus(); input.setSelectionRange(input.value.length, input.value.length);
}
void $; void toast;
