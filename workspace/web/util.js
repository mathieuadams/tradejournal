// Small DOM / network helpers shared by the workspace modules.
export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export function h(html) { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; }
export const sleep = ms => new Promise(r => setTimeout(r, ms));
export function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
export function throttle(fn, ms) { let last = 0, t = null; return (...a) => { const now = Date.now(); if (now - last >= ms) { last = now; fn(...a); } else if (!t) t = setTimeout(() => { t = null; last = Date.now(); fn(...a); }, ms - (now - last)); }; }

export async function api(path, body, method) {
  const opt = body === undefined ? { method: method || 'GET' } : { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch('/api/' + path, opt);
  let j = null; try { j = await r.json(); } catch { /* not json */ }
  if (!r.ok) throw new Error((j && j.error) || `HTTP ${r.status}`);
  return j;
}

export function toast(msg, kind = '', ms = 3500) {
  const t = h(`<div class="toast ${kind}">${esc(msg)}</div>`); document.getElementById('toasts').appendChild(t);
  setTimeout(() => t.remove(), ms);
}

export const money = (v, d = 2) => (v < 0 ? '-$' : '$') + Math.abs(v ?? 0).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
export const signed = (v, d = 2) => (v >= 0 ? '+' : '') + (v ?? 0).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
export const pct = v => (v >= 0 ? '+' : '') + (v ?? 0).toFixed(2) + '%';
export function priceDigits(p) { return !p ? 2 : p < 1 ? 4 : p < 10 ? 3 : 2; }

// popover anchored to an element; closes on outside click / Escape
let popClose = null;
export function popover(anchor, content, { align = 'left', onClose } = {}) {
  closePopover();
  const p = document.getElementById('popover'); p.innerHTML = ''; p.appendChild(content); p.hidden = false;
  const r = anchor.getBoundingClientRect(), pw = p.offsetWidth, ph = p.offsetHeight;
  let x = align === 'right' ? r.right - pw : r.left, y = r.bottom + 4;
  x = Math.max(6, Math.min(innerWidth - pw - 6, x)); if (y + ph > innerHeight - 6) y = Math.max(6, r.top - ph - 4);
  p.style.left = x + 'px'; p.style.top = y + 'px';
  const out = e => { if (!p.contains(e.target) && !anchor.contains(e.target)) closePopover(); };
  const key = e => { if (e.key === 'Escape') closePopover(); };
  setTimeout(() => { document.addEventListener('pointerdown', out, true); document.addEventListener('keydown', key, true); });
  popClose = () => { document.removeEventListener('pointerdown', out, true); document.removeEventListener('keydown', key, true); p.hidden = true; p.innerHTML = ''; onClose && onClose(); };
  return p;
}
export function popAt(x, y, content) {
  const fake = { getBoundingClientRect: () => ({ left: x, right: x, top: y, bottom: y }), contains: () => false };
  return popover(fake, content);
}
export function closePopover() { if (popClose) { const f = popClose; popClose = null; f(); } }

let modalClose = null;
export function modal(content, { onClose, cls = '' } = {}) {
  closeModal();
  const back = document.getElementById('modal'), box = document.getElementById('modalBox');
  box.className = 'modal ' + cls; box.innerHTML = ''; box.appendChild(content); back.hidden = false;
  const key = e => { if (e.key === 'Escape') { e.stopPropagation(); closeModal(); } };
  const click = e => { if (e.target === back) closeModal(); };
  document.addEventListener('keydown', key, true); back.addEventListener('pointerdown', click);
  modalClose = () => { document.removeEventListener('keydown', key, true); back.removeEventListener('pointerdown', click); back.hidden = true; box.innerHTML = ''; onClose && onClose(); };
  return box;
}
export function closeModal() { if (modalClose) { const f = modalClose; modalClose = null; f(); } }

export function confirmDialog(title, bodyHtml, okText = 'Confirm', okCls = 'primary') {
  return new Promise(res => {
    let done = false;
    const el = h(`<div class="dialog"><div class="ph"><h3>${esc(title)}</h3></div><div class="pbody">${bodyHtml}</div>
      <div class="foot"><button class="btn ghost" data-a="no">Cancel</button><button class="btn ${okCls}" data-a="yes">${esc(okText)}</button></div></div>`);
    el.addEventListener('click', e => { const a = e.target.closest('[data-a]'); if (!a) return; done = true; closeModal(); res(a.dataset.a === 'yes'); });
    modal(el, { onClose: () => { if (!done) res(false); } });
    setTimeout(() => el.querySelector('[data-a=yes]').focus());
  });
}

export function nyNow() {
  const o = {}; for (const p of new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hourCycle: 'h23', weekday: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date())) o[p.type] = p.value;
  return { wd: o.weekday, h: +o.hour, m: +o.minute, s: +o.second, date: `${o.year}-${o.month}-${o.day}` };
}
export function marketStatus() {
  const n = nyNow(), t = n.h * 60 + n.m, wk = !['Sat', 'Sun'].includes(n.wd);
  if (!wk) return { open: false, text: 'US stocks are closed (weekend).' };
  if (t >= 570 && t < 960) return { open: true, text: 'US stocks are open.' };
  if (t >= 240 && t < 570) return { open: false, text: 'US stocks are in pre-market.' };
  if (t >= 960 && t < 1200) return { open: false, text: 'US stocks are in after-hours.' };
  return { open: false, text: 'US stocks are closed.' };
}
export function fmtTime(sec) { const d = new Date(sec * 1000); return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }); }
