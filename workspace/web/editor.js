// Pine Script editor: highlighted textarea, line numbers, compile errors, run on chart, save to "My scripts".
import { W } from './ctx.js';
import { LIBRARY, DEFAULT_SCRIPT } from './library.js';
import { compile, PineError } from './pine.js';
import { newId } from './chart.js';
import { $, h, esc, toast, popover, closePopover, confirmDialog } from './util.js';
import { focusChat } from './chat.js';

const E = { name: 'My script', src: DEFAULT_SCRIPT, scriptId: null, indicatorId: null, dirty: false, err: null, logs: [] };
let root = null;

const STRATEGY_TEMPLATE = `//@version=6
strategy("My strategy", overlay = true, initial_capital = 100000, default_qty_type = strategy.fixed, default_qty_value = 100)
fastLen = input.int(9, "Fast EMA")
slowLen = input.int(21, "Slow EMA")
stopPct = input.float(1.0, "Stop %", step = 0.1)
tgtPct  = input.float(2.0, "Target %", step = 0.1)

fast = ta.ema(close, fastLen)
slow = ta.ema(close, slowLen)
plot(fast, "Fast", color.new(color.teal, 0))
plot(slow, "Slow", color.new(color.orange, 0))

if ta.crossover(fast, slow) and strategy.position_size == 0
    strategy.entry("L", strategy.long)
    strategy.exit("x", "L", stop = close * (1 - stopPct / 100), limit = close * (1 + tgtPct / 100))
if ta.crossunder(fast, slow) and strategy.position_size == 0
    strategy.entry("S", strategy.short)
    strategy.exit("x", "S", stop = close * (1 + stopPct / 100), limit = close * (1 - tgtPct / 100))
`;

export function initEditor() {
  const s = W.state.editor; if (s && s.src) Object.assign(E, s, { dirty: !!s.dirty });
}
function persist() { W.state.editor = { name: E.name, src: E.src, scriptId: E.scriptId, indicatorId: E.indicatorId, dirty: E.dirty }; W.save(); }

export function openInEditor(o = {}) {
  if (o.indicatorId) {
    const ind = W.tab().indicators.find(i => i.id === o.indicatorId); if (!ind) return;
    const sc = W.scriptFor(ind);
    Object.assign(E, { name: sc.builtin ? sc.name + ' (copy)' : sc.name, src: sc.src, scriptId: sc.mine ? ind.scriptId : null, indicatorId: ind.id, dirty: false, err: null, logs: [] });
  } else if (o.copyOf) {
    const lib = LIBRARY.find(s => s.id === o.copyOf); if (!lib) return;
    Object.assign(E, { name: lib.name + ' (copy)', src: lib.src, scriptId: null, indicatorId: null, dirty: true, err: null, logs: [] });
  } else if (o.scriptId) {
    const s = W.state.scripts.find(x => x.id === o.scriptId); if (!s) return;
    const ind = W.tab().indicators.find(i => i.scriptId === s.id);
    Object.assign(E, { name: s.name, src: s.src, scriptId: s.id, indicatorId: ind ? ind.id : null, dirty: false, err: null, logs: [] });
  } else if (o.src) {
    Object.assign(E, { name: o.name || 'AI script', src: o.src, scriptId: null, indicatorId: o.indicatorId || null, dirty: true, err: null, logs: [] });
  } else if (o.fresh) {
    const n = (W.state.scripts || []).length + 1;
    Object.assign(E, o.fresh === 'strategy' ? { name: `My strategy ${n}`, src: STRATEGY_TEMPLATE } : { name: `My script ${n}`, src: DEFAULT_SCRIPT }, { scriptId: null, indicatorId: null, dirty: true, err: null, logs: [] });
  }
  persist();
  if (W.currentSide() !== 'editor') W.openSide('editor', true); else mountEditor($('#sidebody'));
  if (o.run) runOnChart();
}

export function mountEditor(el) {
  root = el; el.innerHTML = '';
  const bar = h(`<div class="ed-bar"><button class="ed-name" id="edName" title="Scripts">${esc(E.name)}${E.dirty ? ' •' : ''}<svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M7 10l5 5 5-5"/></svg></button>
    <button class="btn sm primary" id="edRun" title="Ctrl+Enter">▶ ${E.indicatorId && W.tab().indicators.some(i => i.id === E.indicatorId) ? 'Update on chart' : 'Add to chart'}</button>
    <button class="btn sm primary" id="edNew" title="Start a new script (indicator or strategy)">＋ New</button><button class="btn sm" id="edSave" title="Ctrl+S">Save</button><button class="btn sm" id="edStudy" title="Backtest and optimize this strategy">⚗ Research</button><button class="btn sm" id="edFreeze" title="Freeze this strategy (script + inputs) and paper trade it on the server">❄ Freeze</button><button class="btn sm ghost" id="edAsk" title="Ask Quant to explain or fix">✦ Ask Quant</button>
    <span class="grow"></span><span class="dim" style="font-size:11px">Pine v6</span><button class="icon-btn" id="edClose" title="Close">✕</button></div>`);
  const wrap = h(`<div class="ed-wrap"><div class="ed-gutter" id="edGut"></div><div class="ed-area"><div id="edErr"></div><pre id="edPre"></pre><textarea id="edTa" spellcheck="false" autocapitalize="off" autocomplete="off"></textarea></div></div>`);
  const log = h(`<div class="ed-log" id="edLog"></div>`);
  el.append(bar, wrap, log);
  const ta = $('#edTa', el), pre = $('#edPre', el), gut = $('#edGut', el);
  ta.value = E.src;
  const sync = () => { pre.innerHTML = highlight(ta.value) + '\n'; renderGutter(); pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; gut.scrollTop = ta.scrollTop; placeErr(); };
  ta.addEventListener('input', () => { E.src = ta.value; E.dirty = true; $('#edName', el).firstChild.nodeValue = E.name + ' •'; sync(); persistLater(); checkLater(); });
  ta.addEventListener('scroll', () => { pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; gut.scrollTop = ta.scrollTop; placeErr(); });
  ta.addEventListener('keydown', e => keys(e, ta, sync));
  $('#edNew', el).onclick = e => newMenu(e.currentTarget);
  $('#edRun', el).onclick = runOnChart; $('#edSave', el).onclick = () => save(); $('#edStudy', el).onclick = () => import('./research.js').then(m => m.openResearch({ src: E.src, name: E.name, indicatorId: E.indicatorId })); $('#edClose', el).onclick = () => W.openSide('editor');
  $('#edFreeze', el).onclick = () => import('./frozen.js').then(m => {
    const ind = E.indicatorId && W.tab().indicators.find(i => i.id === E.indicatorId);
    if (ind && W.scriptFor(ind).src === E.src) return m.freezeIndicator(ind.id);
    const tab = W.tab(); m.freezeDialog({ src: E.src, name: E.name, inputs: null, symbol: tab.symbol, tf: tab.tf });
  });
  $('#edAsk', el).onclick = () => { if (!W.state.chatOpen) W.toggleChat(true); focusChat(E.err ? `My Pine script fails with "${E.err.message}" on line ${E.err.line}. Fix it and add it to the chart.` : 'Explain what my Pine script in the editor does and suggest improvements.', { script: true }); };
  $('#edName', el).onclick = () => scriptsMenu($('#edName', el));
  sync(); check(); renderLog();
}
const persistLater = (() => { let t; return () => { clearTimeout(t); t = setTimeout(persist, 600); }; })();
const checkLater = (() => { let t; return () => { clearTimeout(t); t = setTimeout(check, 500); }; })();

function check() {
  try { compile(E.src); E.err = null; } catch (e) { E.err = { message: e.message, line: e instanceof PineError ? e.line : 0 }; }
  renderGutter(); placeErr(); renderLog();
}
function renderGutter() {
  const gut = root && $('#edGut', root); if (!gut) return; const n = E.src.split('\n').length; let s = '';
  for (let i = 1; i <= n; i++) s += (E.err && E.err.line === i ? `<span class="err">${i}</span>` : i) + '\n';
  gut.innerHTML = s;
}
function placeErr() {
  const box = root && $('#edErr', root), ta = root && $('#edTa', root); if (!box || !ta) return;
  box.innerHTML = E.err && E.err.line ? `<div class="ed-errline" style="top:${8 + (E.err.line - 1) * 20 - ta.scrollTop}px"></div>` : '';
}
function renderLog() {
  const el = root && $('#edLog', root); if (!el) return;
  const lines = [];
  if (E.err) lines.push(`<div class="e" data-line="${E.err.line}">✖ ${E.err.line ? `line ${E.err.line}: ` : ''}${esc(E.err.message)}</div>`);
  else lines.push('<div class="ok">✓ Compiles</div>');
  for (const l of E.logs) lines.push(`<div class="${l.level === 'error' ? 'e' : l.level === 'warning' ? 'w' : 'i'}">${l.level === 'info' ? 'ℹ' : '⚠'} ${l.bar != null ? `[bar ${l.bar}] ` : ''}${esc(l.msg)}</div>`);
  el.innerHTML = lines.join('');
  el.onclick = e => { const d = e.target.closest('[data-line]'); if (d && +d.dataset.line) gotoLine(+d.dataset.line); };
}
function gotoLine(n) {
  const ta = $('#edTa', root); const lines = ta.value.split('\n'); let pos = 0; for (let i = 0; i < n - 1; i++) pos += lines[i].length + 1;
  ta.focus(); ta.setSelectionRange(pos, pos + (lines[n - 1] || '').length); ta.scrollTop = Math.max(0, (n - 5) * 20);
}

export function runOnChart() {
  check();
  if (E.err) { toast(`Pine error line ${E.err.line}: ${E.err.message}`, 'err'); return { ok: false, error: E.err }; }
  const tab = W.tab(); let ind = E.indicatorId && tab.indicators.find(i => i.id === E.indicatorId);
  const spec = E.scriptId && !E.dirty ? { scriptId: E.scriptId, src: undefined, name: undefined } : { src: E.src, name: E.name, scriptId: undefined };
  if (E.scriptId && E.dirty) { save(true); spec.scriptId = E.scriptId; spec.src = undefined; spec.name = undefined; }
  if (ind) W.updateIndicator(ind.id, { ...spec, inputs: {} });
  else { ind = W.addIndicator(spec); E.indicatorId = ind.id; }
  persist();
  const out = W.indOut.find(d => d.id === ind.id);
  E.logs = out && out.out ? out.out.logs : [];
  if (out && out.out && out.out.error) { E.err = out.out.error; toast(`Runtime error: ${out.out.error.message}`, 'err'); }
  if (root && root.isConnected && W.currentSide() === 'editor') mountEditor(root);
  return { ok: !(out && out.out && out.out.error), indicatorId: ind.id, out: out && out.out };
}
function save(quiet) {
  const now = Date.now();
  if (E.scriptId) { const s = W.state.scripts.find(x => x.id === E.scriptId); if (s) { s.src = E.src; s.name = E.name; s.updated = now; } else E.scriptId = null; }
  if (!E.scriptId) {
    const s = { id: newId('s'), name: E.name, src: E.src, updated: now }; W.state.scripts.push(s); E.scriptId = s.id;
    const ind = E.indicatorId && W.tab().indicators.find(i => i.id === E.indicatorId);
    if (ind && ind.src) { delete ind.src; delete ind.name; ind.scriptId = s.id; }
  }
  E.dirty = false; persist(); W.recompute();
  if (!quiet) { toast(`Saved “${E.name}” to My scripts`, 'ok'); if (root && W.currentSide() === 'editor') mountEditor(root); }
}
function scriptsMenu(anchor) {
  const m = h(`<div class="menu" style="width:280px;max-height:60vh;overflow:auto">
    <button data-a="new">＋ New script</button><button data-a="rename">Rename…</button><button data-a="copy">Make a copy</button>${E.scriptId ? '<button data-a="del">Delete from My scripts</button>' : ''}
    <div class="msep"></div><div class="pop-sec">My scripts</div>${W.state.scripts.length ? W.state.scripts.map(s => `<button data-open="${s.id}">${esc(s.name)}</button>`).join('') : '<div class="note" style="padding:4px 10px">None saved yet</div>'}
    <div class="pop-sec">Built-in (opens a copy)</div>${LIBRARY.map(s => `<button data-lib="${s.id}">${esc(s.name)}</button>`).join('')}</div>`);
  m.onclick = async e => {
    const t = e.target.closest('button'); if (!t) return; closePopover();
    if (t.dataset.open) return openInEditor({ scriptId: t.dataset.open });
    if (t.dataset.lib) return openInEditor({ copyOf: t.dataset.lib });
    if (t.dataset.a === 'new') return openInEditor({ fresh: true });
    if (t.dataset.a === 'rename') { const n = prompt('Script name', E.name); if (n) { E.name = n.trim(); E.dirty = true; persist(); mountEditor(root); } }
    if (t.dataset.a === 'copy') { Object.assign(E, { name: E.name + ' (copy)', scriptId: null, indicatorId: null, dirty: true }); persist(); mountEditor(root); }
    if (t.dataset.a === 'del' && await confirmDialog('Delete script', `Delete “${esc(E.name)}” from My scripts? Charts using it keep a copy.`, 'Delete', 'sell')) {
      const id = E.scriptId; for (const tab of W.state.tabs) for (const i of tab.indicators) if (i.scriptId === id) { delete i.scriptId; i.src = E.src; i.name = E.name; }
      W.state.scripts = W.state.scripts.filter(s => s.id !== id); E.scriptId = null; E.dirty = true; persist(); mountEditor(root);
    }
  };
  popover(anchor, m);
}

function keys(e, ta, sync) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); runOnChart(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); return; }
  const v = ta.value, s = ta.selectionStart, en = ta.selectionEnd;
  const set = (val, a, b = a) => { ta.value = val; ta.setSelectionRange(a, b); E.src = val; E.dirty = true; sync(); persistLater(); checkLater(); };
  if (e.key === 'Tab') {
    e.preventDefault();
    const ls = v.lastIndexOf('\n', s - 1) + 1;
    if (s === en && !e.shiftKey) return set(v.slice(0, s) + '    ' + v.slice(en), s + 4);
    const le = v.indexOf('\n', en - (en > s && v[en - 1] === '\n' ? 1 : 0)); const end = le < 0 ? v.length : le;
    const block = v.slice(ls, end).split('\n').map(l => e.shiftKey ? l.replace(/^ {1,4}/, '') : '    ' + l).join('\n');
    return set(v.slice(0, ls) + block + v.slice(end), ls, ls + block.length);
  }
  if (e.key === 'Enter') {
    e.preventDefault();
    const ls = v.lastIndexOf('\n', s - 1) + 1, line = v.slice(ls, s), ind = line.match(/^\s*/)[0];
    const more = /(=>|^\s*(if|else|for|while|switch)\b.*)\s*$/.test(line.replace(/\/\/.*$/, '')) ? '    ' : '';
    const ins = '\n' + ind + more; return set(v.slice(0, s) + ins + v.slice(en), s + ins.length);
  }
  if (e.key === '/' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault(); const ls = v.lastIndexOf('\n', s - 1) + 1, le = v.indexOf('\n', en); const end = le < 0 ? v.length : le;
    const lines = v.slice(ls, end).split('\n'), all = lines.every(l => /^\s*\/\//.test(l) || !l.trim());
    const block = lines.map(l => all ? l.replace(/^(\s*)\/\/ ?/, '$1') : l.replace(/^(\s*)/, '$1// ')).join('\n'); return set(v.slice(0, ls) + block + v.slice(end), ls, ls + block.length);
  }
}

const KW = /^(if|else|for|to|by|while|var|varip|and|or|not|switch|break|continue|in|import|export|method|true|false|na)$/;
const TY = /^(int|float|bool|string|color|line|label|box|table|array|series|simple|const|map|matrix)$/;
function highlight(src) {
  return src.split('\n').map(line => {
    let out = '', i = 0; const n = line.length;
    while (i < n) {
      const rest = line.slice(i);
      let m;
      if (rest.startsWith('//')) { out += `<span class="tk-c">${esc(rest)}</span>`; break; }
      if ((m = rest.match(/^("([^"\\]|\\.)*"?|'([^'\\]|\\.)*'?)/))) { out += `<span class="tk-s">${esc(m[0])}</span>`; i += m[0].length; continue; }
      if ((m = rest.match(/^#[0-9a-fA-F]{6,8}\b/))) { out += `<span class="tk-col">${m[0]}</span>`; i += m[0].length; continue; }
      if ((m = rest.match(/^\d+(\.\d*)?([eE][+-]?\d+)?/))) { out += `<span class="tk-n">${m[0]}</span>`; i += m[0].length; continue; }
      if ((m = rest.match(/^[A-Za-z_][\w]*(\.[A-Za-z_]\w*)*/))) {
        const w = m[0], call = line[i + w.length] === '(';
        out += KW.test(w) ? `<span class="tk-k">${w}</span>` : TY.test(w) ? `<span class="tk-t">${w}</span>` : call ? `<span class="tk-f">${w}</span>` : /^(ta|math|str|color|input|plot|shape|location|size|line|label|box|array|timeframe|syminfo|barstate|xloc|yloc|extend|format|display|hline|position|text|request|strategy)\./.test(w) || /^(open|high|low|close|volume|time|bar_index|hl2|hlc3|ohlc4|last_bar_index)$/.test(w) ? `<span class="tk-b">${w}</span>` : w;
        i += w.length; continue;
      }
      out += esc(line[i]); i++;
    }
    return out;
  }).join('\n');
}
export function editorState() { return { name: E.name, src: E.src, error: E.err, scriptId: E.scriptId, indicatorId: E.indicatorId }; }

// ＋ New: a blank indicator or strategy (the current script stays saved in My scripts if it was saved)
export function newMenu(anchor, before) {
  document.querySelectorAll('.new-menu').forEach(x => x.remove());
  const r = anchor.getBoundingClientRect(), m = document.createElement('div');
  m.className = 'new-menu';
  m.style.cssText = `position:fixed;left:${Math.min(r.left, innerWidth - 230)}px;top:${r.bottom + 4}px;z-index:3000;min-width:240px`;
  m.innerHTML = '<button data-k="indicator">＋ New indicator<span class="dim"> · plots, levels</span></button><button data-k="strategy">＋ New strategy<span class="dim"> · entries, exits, backtest</span></button>';
  m.onclick = e => { const b = e.target.closest('[data-k]'); if (!b) return; m.remove();
    if (E.dirty && E.src && !confirm('Start a new script? Unsaved changes to the current one are lost (Save first to keep them in My scripts).')) return;
    if (before) before(); openInEditor({ fresh: b.dataset.k }); };
  document.body.appendChild(m);
  setTimeout(() => document.addEventListener('mousedown', function off(e) { if (!m.contains(e.target)) { m.remove(); document.removeEventListener('mousedown', off); } }), 0);
}
