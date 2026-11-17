// Quant: the chat panel. Claude sees the chart (a screenshot with each message plus the chart state as text)
// and acts on it through tools the browser executes: read bars, draw levels / zones / trend lines, change the chart,
// add indicators, write Pine Script (compile errors go back to Claude so it can fix them), and place paper orders.
import { W } from './ctx.js';
import { LIBRARY, findScript } from './library.js';
import { compile } from './pine.js';
import { newId, fmtPrice, nyParts } from './chart.js';
import { $, h, esc, api, toast, marketStatus, money, priceDigits, nyNow } from './util.js';
import { openInEditor, runOnChart, editorState } from './editor.js';

const C = { messages: [], busy: false, abort: null, ui: [] };
let body, input, sendBtn;

const SUGGEST = [
  'Mark up my chart with the key levels and the current trend',
  'Where would a good long entry, stop and target be here?',
  'Write a Pine script that marks inside bars that break out with volume',
  'Compare the trend on 15m, 1h and 1D for this symbol',
];

export function initChat() {
  const pane = $('#chatpane');
  pane.innerHTML = `<div class="ch-head"><div class="seg"><button class="on">✦ Chat</button><button id="chNew" title="Start a new conversation">＋ New chat</button></div><button class="icon-btn" id="chClose" title="Hide chat">✕</button></div>
    <div class="ch-body" id="chBody"></div>
    <div class="ch-foot"><div class="ch-input"><textarea id="chIn" rows="2" placeholder="Ask Quant about this chart…"></textarea>
      <div class="bar"><span class="sees ${W.state.sees ? 'on' : ''}" id="chSees" title="Send a screenshot of the chart with each message"><svg viewBox="0 0 24 24"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>Sees chart</span>
      <span class="dim" style="font-size:11px" id="chModel"></span><button class="send" id="chSend" title="Send (Enter)"><svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button></div></div>
      <div class="ch-disc">Quant can make mistakes. Paper trading only; not financial advice.</div></div>`;
  body = $('#chBody'); input = $('#chIn'); sendBtn = $('#chSend');
  $('#chModel').textContent = W.cfg.llm === 'anthropic' ? W.cfg.model : 'demo';
  $('#chClose').onclick = () => W.toggleChat(false);
  $('#chNew').onclick = () => { if (C.busy) stop(); C.messages = []; C.ui = []; saveChat(); renderEmpty(); };
  $('#chSees').onclick = () => { W.state.sees = !W.state.sees; $('#chSees').classList.toggle('on', W.state.sees); W.save(); };
  W.on('settings', () => $('#chSees').classList.toggle('on', W.state.sees));
  input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); } });
  input.addEventListener('input', autosize);
  sendBtn.onclick = () => C.busy ? stop() : submit();
  loadChat();
}
function autosize() { input.style.height = 'auto'; input.style.height = Math.min(180, input.scrollHeight) + 'px'; }
export function focusChat(text, opts = {}) { C.attachScript = !!opts.script; if (text) { input.value = text; autosize(); } input.focus(); if (text && opts.send) submit(); }

function renderEmpty() {
  body.innerHTML = '';
  const e = h(`<div class="ch-empty"><h2><svg viewBox="0 0 24 24"><path d="M4 6l5 12 3-7 3 7 5-12"/></svg>Ready when you are</h2><div class="mk">${esc(marketStatus().text)}</div><div class="chips">${SUGGEST.map(s => `<button>${esc(s)}</button>`).join('')}</div></div>`);
  e.querySelectorAll('.chips button').forEach(b => { b.onclick = () => { input.value = b.textContent; submit(); }; });
  body.appendChild(e);
}
function saveChat() { try { localStorage.setItem('ws-chat', JSON.stringify({ messages: C.messages.slice(-60).map(stripImages), ui: C.ui.slice(-80) })); } catch { /* quota */ } }
function loadChat() {
  try { const s = JSON.parse(localStorage.getItem('ws-chat') || 'null'); if (s && s.ui && s.ui.length) { C.messages = s.messages || []; C.ui = s.ui; body.innerHTML = ''; for (const u of C.ui) renderUi(u); scroll(); return; } } catch { /* ignore */ }
  renderEmpty();
}
function stripImages(m) { if (!Array.isArray(m.content)) return m; return { ...m, content: m.content.map(c => c.type === 'image' ? { type: 'text', text: '[chart screenshot]' } : c.type === 'tool_result' && Array.isArray(c.content) ? { ...c, content: c.content.map(x => x.type === 'image' ? { type: 'text', text: '[screenshot]' } : x) } : c) }; }
function scroll() { body.scrollTop = body.scrollHeight; }
function renderUi(u) {
  if (u.k === 'user') body.appendChild(h(`<div class="msg user">${esc(u.text)}${u.shot ? '<span class="shot">📷 chart attached</span>' : ''}</div>`));
  if (u.k === 'ai') { const d = h('<div class="msg ai"></div>'); d.innerHTML = md(u.text); wireCode(d); body.appendChild(d); }
  if (u.k === 'tool') body.appendChild(h(`<div class="tool ${u.err ? 'err' : 'done'}"><span class="spin"></span>${esc(u.text)}</div>`));
}

// ------------------------------------------------------------------------------------------------ agent loop
async function submit() {
  const text = input.value.trim(); if (!text || C.busy) return;
  input.value = ''; autosize();
  if (!C.messages.length) body.innerHTML = '';
  const content = [];
  let shot = false;
  if (W.state.sees && W.chart && W.activeBars().length) { try { const url = W.chart.snapshot(1400); content.push({ type: 'image', source: { type: 'base64', media_type: 'image/jpeg', data: url.split(',')[1] } }); shot = true; } catch { /* tainted */ } }
  let ctxText = `<chart_context>\n${contextText()}\n</chart_context>`;
  if (C.attachScript) { const es = editorState(); ctxText += `\n<editor_script name="${es.name}">\n${es.src}\n</editor_script>${es.error ? `\n<compile_error line="${es.error.line}">${es.error.message}</compile_error>` : ''}`; C.attachScript = false; }
  content.push({ type: 'text', text: ctxText + '\n\n' + text });
  const prevM = C.messages[C.messages.length - 1];
  if (prevM && prevM.role === 'user' && Array.isArray(prevM.content)) prevM.content.push(...content); else C.messages.push({ role: 'user', content });
  const u = { k: 'user', text, shot }; C.ui.push(u); renderUi(u); scroll();
  C.busy = true; sendBtn.innerHTML = '<svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="1"/></svg>'; sendBtn.title = 'Stop';
  try {
    for (let step = 0; step < 12; step++) {
      const res = await callClaude();
      if (!res) break;
      C.messages.push({ role: 'assistant', content: res.content });
      if (res.stop !== 'tool_use') break;
      const results = [];
      for (const b of res.content.filter(x => x.type === 'tool_use')) {
        const chip = h(`<div class="tool"><span class="spin"></span>${esc(toolLabel(b.name, b.input))}</div>`); body.appendChild(chip); scroll();
        let r;
        try { r = await runTool(b.name, b.input || {}); } catch (e) { r = { error: true, text: 'Tool failed: ' + e.message }; }
        chip.classList.add(r.error ? 'err' : 'done'); if (r.summary) chip.lastChild.nodeValue = r.summary;
        C.ui.push({ k: 'tool', text: r.summary || toolLabel(b.name, b.input), err: !!r.error });
        results.push({ type: 'tool_result', tool_use_id: b.id, content: r.content || [{ type: 'text', text: r.text || 'ok' }], ...(r.error ? { is_error: true } : {}) });
      }
      C.messages.push({ role: 'user', content: results });
    }
  } catch (e) {
    if (e.name !== 'AbortError') { const d = h(`<div class="msg ai dn">⚠ ${esc(e.message)}</div>`); body.appendChild(d); }
  } finally {
    C.busy = false; C.abort = null; sendBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>'; sendBtn.title = 'Send'; saveChat(); scroll();
  }
}
function stop() { if (C.abort) C.abort.abort(); }

function trimmed() {
  // keep the latest screenshot only; drop the oldest turns (at a user text turn, so tool pairs stay intact)
  let msgs = C.messages;
  if (msgs.length > 40) { let cut = msgs.length - 40; while (cut < msgs.length && !(msgs[cut].role === 'user' && Array.isArray(msgs[cut].content) && msgs[cut].content.some(c => c.type === 'text'))) cut++; msgs = msgs.slice(cut); }
  let lastImg = -1; msgs.forEach((m, i) => { if (Array.isArray(m.content) && m.content.some(c => c.type === 'image' || (c.type === 'tool_result' && Array.isArray(c.content) && c.content.some(x => x.type === 'image')))) lastImg = i; });
  return msgs.map((m, i) => i === lastImg ? m : stripImages(m));
}

async function callClaude() {
  C.abort = new AbortController();
  const r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: C.abort.signal, body: JSON.stringify({ system: systemPrompt(), messages: trimmed(), tools: TOOLS, max_tokens: 8000 }) });
  if (!r.ok || !r.body) throw new Error(`Chat failed (${r.status})`);
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = '', stopReason = null; const blocks = []; let cur = null, el = null, txt = '';
  const handle = (ev, data) => {
    if (ev === 'error' || data.type === 'error') throw new Error((data.error && data.error.message) || 'Claude error');
    if (data.type === 'content_block_start') {
      const b = data.content_block; cur = { ...b };
      if (b.type === 'text') { cur.text = ''; el = h('<div class="msg ai caret"></div>'); body.appendChild(el); txt = ''; }
      if (b.type === 'tool_use') cur._json = '';
      blocks[data.index] = cur;
    }
    if (data.type === 'content_block_delta') {
      const d = data.delta, b = blocks[data.index];
      if (d.type === 'text_delta') { b.text += d.text; txt = b.text; el.innerHTML = md(txt); scroll(); }
      if (d.type === 'input_json_delta') b._json += d.partial_json;
    }
    if (data.type === 'content_block_stop') {
      const b = blocks[data.index];
      if (b && b.type === 'tool_use') { try { b.input = b._json ? JSON.parse(b._json) : {}; } catch { b.input = {}; } delete b._json; }
      if (b && b.type === 'text' && el) { el.classList.remove('caret'); wireCode(el); if (b.text.trim()) C.ui.push({ k: 'ai', text: b.text }); else el.remove(); el = null; }
    }
    if (data.type === 'message_delta' && data.delta) stopReason = data.delta.stop_reason || stopReason;
  };
  for (;;) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let k;
    while ((k = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, k); buf = buf.slice(k + 2);
      let ev = 'message', data = '';
      for (const line of chunk.split('\n')) { if (line.startsWith('event:')) ev = line.slice(6).trim(); else if (line.startsWith('data:')) data += line.slice(5).trim(); }
      if (data) handle(ev, JSON.parse(data));
    }
  }
  if (el) el.classList.remove('caret');
  const content = blocks.filter(Boolean).map(b => b.type === 'text' ? { type: 'text', text: b.text } : b.type === 'tool_use' ? { type: 'tool_use', id: b.id, name: b.name, input: b.input || {} } : b).filter(b => !(b.type === 'text' && !b.text.trim()));
  if (!content.length) content.push({ type: 'text', text: '…' });
  return { content, stop: stopReason };
}

// ------------------------------------------------------------------------------------------------ context
function iso(t) { const p = nyParts(t); return `${p.y}-${String(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'].indexOf(p.mon) + 1).padStart(2, '0')}-${String(p.d).padStart(2, '0')} ${String(p.h).padStart(2, '0')}:${String(p.mi).padStart(2, '0')}`; }
function contextText() {
  const tab = W.tab(), bars = W.activeBars(), last = bars[bars.length - 1], d = priceDigits(last && last.c);
  const [i0, i1] = W.chart.visRange(); let hi = -Infinity, lo = Infinity; for (let i = i0; i <= i1; i++) { const b = bars[i]; if (b) { hi = Math.max(hi, b.h); lo = Math.min(lo, b.l); } }
  const pos = W.account ? W.account.positions.find(p => p.symbol === tab.symbol) : null, orders = W.account ? W.account.orders.filter(o => o.symbol === tab.symbol) : [];
  const n = nyNow();
  return [`Now: ${n.date} ${String(n.h).padStart(2, '0')}:${String(n.m).padStart(2, '0')} New York. ${marketStatus().text}`,
    `Chart: ${tab.symbol} · ${tab.tf} · ${tab.type || 'candles'} · data: ${W.cfg.data === 'alpaca' ? 'Alpaca live' : 'SIMULATED (not real prices)'}${W.replay != null ? ' · BAR REPLAY active (the last bar is the replay cursor)' : ''}`,
    last ? `Last bar ${iso(last.t)}: O ${fmtPrice(last.o, d)} H ${fmtPrice(last.h, d)} L ${fmtPrice(last.l, d)} C ${fmtPrice(last.c, d)} V ${Math.round(last.v)}` : 'No bars',
    bars[i0] ? `Visible: ${iso(bars[i0].t)} → ${iso(bars[i1].t)} (${i1 - i0 + 1} bars), range ${fmtPrice(lo, d)} – ${fmtPrice(hi, d)}` : '',
    `Indicators: ${W.indOut.map(x => `${x.out ? x.out.meta.title : x.title}${x.visible ? '' : ' (hidden)'}${x.out && x.out.error ? ' [error]' : ''}`).join(', ') || 'none'}`,
    `Drawings: ${W.drawings().filter(x => !x.hidden).length} (${W.drawings().filter(x => x.source === 'ai').length} by you)`,
    `Paper account: equity ${W.account ? money(W.account.equity) : '?'}${pos ? `; position ${pos.qty} @ ${pos.avg.toFixed(d)} (open P&L ${money(pos.pnl)})` : '; no position in this symbol'}${orders.length ? `; ${orders.length} working order(s)` : ''}`].filter(Boolean).join('\n');
}
function systemPrompt() {
  return `You are Quant, the trading assistant inside a charting workspace (TradingView-style). The user sees a chart in the middle of the screen; you see a screenshot of it with each message (when "Sees chart" is on) and a <chart_context> block.

What you can do with tools: read the bars and indicator values (get_chart_state, get_bars for any symbol / timeframe), mark up the chart (draw_levels, draw_zones, draw_trendlines, clear_ai_drawings), change symbol / timeframe / chart type (set_chart), add or remove library indicators (add_indicator, remove_indicator), write Pine Script that runs on the chart (write_pine_script), read the paper account (get_account) and place simulated paper orders (place_paper_order). take_screenshot shows you the chart after changes.

Rules:
- Base levels and numbers on the bars you read (get_chart_state), not on guesses from the image. Prices in the screenshot can be imprecise.
- When asked to mark up a chart: read the state first, then draw a handful of meaningful levels / zones (support, resistance, prior swing highs / lows, range edges, gaps), each with a short label; clear your previous drawings unless asked to keep them. Then explain briefly.
- Orders are paper only (simulated account). Only call place_paper_order when the user asks you to trade; give the side, quantity, type, and a stop loss / take profit when you suggested them. The user may have to confirm.
- Be concise and concrete: levels with prices, what would invalidate the idea. You are not a financial adviser; don't pad answers with disclaimers beyond one short line when you suggest a trade.
- Data may be SIMULATED (the context says so): then say the analysis is on simulated data.
- Use markdown sparingly: short paragraphs, bullets for levels, no big headers.

Pine Script: write v6 (//@version=6, indicator(...)). The interpreter supports: var/varip, typed declarations, tuples, :=, += etc., if / else if / else, for (to / by / in), while, switch, user functions (single line and block), history [], ta.* (sma ema rma wma vwma hma alma stdev dev variance highest lowest highestbars lowestbars change mom roc rsi tr atr crossover crossunder cross cum vwap macd bb bbw kc stoch cci mfi wpr supertrend sar pivothigh pivotlow valuewhen barssince rising falling dmi linreg correlation median percentrank obv accdist), math.*, str.*, color.new/rgb/from_gradient, input.int/float/bool/string/color/source, plot (styles line linebr stepline histogram columns circles cross area), plotshape, plotchar, hline, fill (plots or hlines), bgcolor, barcolor, line.new / label.new / box.new with their set_*/get_*/delete and method syntax, array.* with method syntax, time / hour / minute / dayofweek / timeframe.*, syminfo.*, barstate.*. Not supported yet: request.security on other timeframes (returns the chart's own series), tables, user-defined types, strategy orders (strategy() runs as an indicator). Overlay scripts use overlay = true. If write_pine_script returns an error, fix the script and call it again.

Library indicators you can add by name: ${LIBRARY.map(s => s.name).join('; ')}.`;
}

// ------------------------------------------------------------------------------------------------ tools
const lvl = { type: 'object', properties: { price: { type: 'number' }, label: { type: 'string' }, kind: { type: 'string', enum: ['support', 'resistance', 'pivot', 'target', 'stop', 'entry', 'other'] }, color: { type: 'string', description: 'optional hex color' }, style: { type: 'string', enum: ['solid', 'dashed', 'dotted'] } }, required: ['price'] };
const TOOLS = [
  { name: 'get_chart_state', description: 'Current chart: symbol, timeframe, the last N bars [time (New York), open, high, low, close, volume], indicator values and recent labels (e.g. BOS / CHoCH), drawings, and the paper position for this symbol.', input_schema: { type: 'object', properties: { bars: { type: 'integer', description: 'how many recent bars (default 150, max 600)' } } } },
  { name: 'get_bars', description: 'OHLCV bars for any symbol and timeframe (e.g. to check a higher timeframe) without changing the chart.', input_schema: { type: 'object', properties: { symbol: { type: 'string' }, timeframe: { type: 'string', description: '1m 5m 15m 30m 1h 4h 1D 1W 1M or any multiple like 22m' }, count: { type: 'integer', description: 'default 200, max 1000' } }, required: ['timeframe'] } },
  { name: 'draw_levels', description: 'Draw horizontal price levels on the current chart.', input_schema: { type: 'object', properties: { levels: { type: 'array', items: lvl }, clear_previous: { type: 'boolean', description: 'remove your earlier drawings first (default true)' } }, required: ['levels'] } },
  { name: 'draw_zones', description: 'Draw shaded price zones (supply / demand, ranges) extending to the right.', input_schema: { type: 'object', properties: { zones: { type: 'array', items: { type: 'object', properties: { top: { type: 'number' }, bottom: { type: 'number' }, label: { type: 'string' }, kind: { type: 'string', enum: ['supply', 'demand', 'range', 'gap', 'other'] }, from_time: { type: 'string', description: 'optional start, "YYYY-MM-DD HH:MM" New York' } }, required: ['top', 'bottom'] } }, clear_previous: { type: 'boolean' } }, required: ['zones'] } },
  { name: 'draw_trendlines', description: 'Draw trend lines between two (time, price) points. Times are "YYYY-MM-DD HH:MM" New York as in get_chart_state.', input_schema: { type: 'object', properties: { lines: { type: 'array', items: { type: 'object', properties: { time1: { type: 'string' }, price1: { type: 'number' }, time2: { type: 'string' }, price2: { type: 'number' }, label: { type: 'string' }, extend: { type: 'boolean' }, color: { type: 'string' } }, required: ['time1', 'price1', 'time2', 'price2'] } }, clear_previous: { type: 'boolean' } }, required: ['lines'] } },
  { name: 'clear_ai_drawings', description: 'Remove the drawings you made on the current symbol.', input_schema: { type: 'object', properties: {} } },
  { name: 'set_chart', description: 'Change the current tab: symbol, timeframe and / or chart type.', input_schema: { type: 'object', properties: { symbol: { type: 'string' }, timeframe: { type: 'string' }, chart_type: { type: 'string', enum: ['candles', 'hollow', 'bars', 'ha', 'line', 'area'] }, new_tab: { type: 'boolean' } } } },
  { name: 'add_indicator', description: 'Add a library indicator to the chart by name or id, optionally with input values by input title.', input_schema: { type: 'object', properties: { name: { type: 'string' }, inputs: { type: 'object', description: 'e.g. {"Length": 50}' } }, required: ['name'] } },
  { name: 'remove_indicator', description: 'Remove an indicator from the chart by its name.', input_schema: { type: 'object', properties: { name: { type: 'string' } }, required: ['name'] } },
  { name: 'write_pine_script', description: 'Compile Pine Script v6 and add it to the chart (and / or open it in the editor). Returns compile / runtime errors or a summary of the output.', input_schema: { type: 'object', properties: { name: { type: 'string' }, source: { type: 'string' }, add_to_chart: { type: 'boolean', description: 'default true' }, open_in_editor: { type: 'boolean', description: 'default true' } }, required: ['source'] } },
  { name: 'get_account', description: 'Paper account: equity, positions, working orders, recent closed trades.', input_schema: { type: 'object', properties: {} } },
  { name: 'place_paper_order', description: 'Place a simulated paper order. Only when the user asks to trade.', input_schema: { type: 'object', properties: { symbol: { type: 'string' }, side: { type: 'string', enum: ['buy', 'sell'] }, qty: { type: 'number' }, type: { type: 'string', enum: ['market', 'limit', 'stop'] }, price: { type: 'number' }, stop_loss: { type: 'number' }, take_profit: { type: 'number' }, reason: { type: 'string' } }, required: ['side', 'qty'] } },
  { name: 'take_screenshot', description: 'A fresh screenshot of the chart (after drawing or changing it).', input_schema: { type: 'object', properties: {} } },
];
function toolLabel(n, i = {}) {
  return { get_chart_state: 'Reading the chart', get_bars: `Reading ${i.symbol || W.tab().symbol} ${i.timeframe || ''} bars`, draw_levels: 'Drawing levels', draw_zones: 'Drawing zones', draw_trendlines: 'Drawing trend lines', clear_ai_drawings: 'Clearing drawings', set_chart: 'Changing the chart', add_indicator: `Adding ${i.name || 'indicator'}`, remove_indicator: `Removing ${i.name || 'indicator'}`, write_pine_script: `Writing Pine: ${i.name || 'script'}`, get_account: 'Reading the paper account', place_paper_order: 'Preparing a paper order', take_screenshot: 'Looking at the chart' }[n] || n;
}
const COLORS = { support: '#089981', demand: '#089981', resistance: '#f23645', supply: '#f23645', pivot: '#f5a623', target: '#2962ff', entry: '#2962ff', stop: '#e040fb', range: '#787b86', gap: '#00bcd4', other: '#f5a623' };
function parseT(s) {
  if (typeof s === 'number') return s > 1e12 ? s / 1000 : s;
  const m = String(s).match(/(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/); if (!m) return null;
  const guess = Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0)) / 1000, p = nyParts(guess);
  const asUtc = Date.UTC(p.y, ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'].indexOf(p.mon), p.d, p.h, p.mi) / 1000;
  return guess + (guess - asUtc);
}
const txt = t => ({ content: [{ type: 'text', text: typeof t === 'string' ? t : JSON.stringify(t) }] });
function clearAi() { W.setDrawings(W.drawings().filter(d => d.source !== 'ai')); }
function indSummary(d, i1) {
  const o = d.out; if (!o) return null;
  const plots = {}; for (const p of o.plots) { if (p.display === 'none') continue; const v = p.values[i1]; if (Number.isFinite(v)) plots[p.title] = +v.toFixed(4); }
  const labels = o.labels.slice(-12).map(l => ({ text: l.text, price: Number.isFinite(l.y) ? +l.y.toFixed(4) : null, bars_ago: Number.isFinite(l.x) ? i1 - Math.round(l.x) : null })).filter(l => l.text);
  const boxes = o.boxes.slice(-8).map(b => ({ top: +b.top.toFixed(4), bottom: +b.bottom.toFixed(4), text: b.text || undefined }));
  const shapes = o.shapes.map(s => { let k = -1; for (let i = i1; i >= Math.max(0, i1 - 300); i--) if (Number.isFinite(s.values[i])) { k = i; break; } return k >= 0 ? { title: s.title, last_bars_ago: i1 - k } : null; }).filter(Boolean);
  return { name: o.meta.title, overlay: o.meta.overlay, inputs: Object.fromEntries(o.inputs.filter(Boolean).map(x => [x.title, x.value])), last_values: plots, ...(labels.length ? { recent_labels: labels } : {}), ...(boxes.length ? { boxes } : {}), ...(shapes.length ? { signals: shapes } : {}), ...(o.error ? { error: o.error.message } : {}) };
}

async function runTool(name, i) {
  const tab = W.tab();
  if (name === 'get_chart_state') {
    const bars = W.activeBars(), n = Math.max(10, Math.min(600, i.bars || 150)), d = priceDigits(bars.at(-1)?.c), r = x => +x.toFixed(d);
    const sl = bars.slice(-n).map(b => [iso(b.t), r(b.o), r(b.h), r(b.l), r(b.c), Math.round(b.v)]);
    const i1 = bars.length - 1;
    const st = { symbol: tab.symbol, timeframe: tab.tf, data: W.cfg.data === 'alpaca' ? 'live' : 'simulated', bar_format: '[time New York, open, high, low, close, volume]', bars: sl,
      indicators: W.indOut.filter(x => x.visible).map(x => indSummary(x, i1)).filter(Boolean),
      drawings: W.drawings().filter(x => !x.hidden).map(x => ({ type: x.type, by: x.source === 'ai' ? 'you' : 'user', label: x.text || undefined, prices: x.points.map(p => +p.p.toFixed(d)) })),
      paper: W.account ? { equity: +W.account.equity.toFixed(2), position: W.account.positions.find(p => p.symbol === tab.symbol) || null, working_orders: W.account.orders.filter(o => o.symbol === tab.symbol).map(o => ({ side: o.side, type: o.type, qty: o.qty, price: o.price, role: o.note || 'entry' })) } : null };
    return { ...txt(st), summary: `Read ${sl.length} bars of ${tab.symbol} ${tab.tf}` };
  }
  if (name === 'get_bars') {
    const sym = (i.symbol || tab.symbol).toUpperCase(), tf = W.normTfStrict(i.timeframe) || i.timeframe, n = Math.max(10, Math.min(1000, i.count || 200));
    const j = await api(`bars?symbol=${encodeURIComponent(sym)}&tf=${encodeURIComponent(tf)}&limit=${n}`);
    const d = priceDigits(j.bars.at(-1)?.[4]), r = x => +x.toFixed(d);
    return { ...txt({ symbol: sym, timeframe: tf, bar_format: '[time New York, open, high, low, close, volume]', bars: j.bars.map(b => [iso(b[0]), r(b[1]), r(b[2]), r(b[3]), r(b[4]), Math.round(b[5])]) }), summary: `Read ${j.bars.length} bars of ${sym} ${tf}` };
  }
  if (name === 'draw_levels') {
    if (i.clear_previous !== false) clearAi();
    const t = W.activeBars().at(-1)?.t || Date.now() / 1000;
    const list = (i.levels || []).filter(l => Number.isFinite(+l.price)).map(l => ({ id: newId('ai'), type: 'hline', points: [{ t, p: +l.price }], source: 'ai', text: l.label || '', color: l.color || COLORS[l.kind] || COLORS.other, style: l.style || (l.kind === 'pivot' ? 'dashed' : 'solid') }));
    W.addDrawings(list); return { ...txt(`Drew ${list.length} levels on ${tab.symbol}.`), summary: `Drew ${list.length} level${list.length === 1 ? '' : 's'}` };
  }
  if (name === 'draw_zones') {
    if (i.clear_previous === true) clearAi();
    const bars = W.activeBars(), t0 = bars[Math.max(0, bars.length - 80)]?.t || 0;
    const list = (i.zones || []).filter(z => Number.isFinite(+z.top) && Number.isFinite(+z.bottom)).map(z => ({ id: newId('ai'), type: 'zone', points: [{ t: parseT(z.from_time) || t0, p: +z.top }, { t: bars.at(-1).t, p: +z.bottom }], source: 'ai', text: z.label || '', color: COLORS[z.kind] || COLORS.other }));
    W.addDrawings(list); return { ...txt(`Drew ${list.length} zones.`), summary: `Drew ${list.length} zone${list.length === 1 ? '' : 's'}` };
  }
  if (name === 'draw_trendlines') {
    if (i.clear_previous === true) clearAi();
    const list = (i.lines || []).map(l => ({ id: newId('ai'), type: 'trend', points: [{ t: parseT(l.time1), p: +l.price1 }, { t: parseT(l.time2), p: +l.price2 }], source: 'ai', text: l.label || '', extend: l.extend !== false, color: l.color || '#f5a623' })).filter(l => l.points.every(p => p.t && Number.isFinite(p.p)));
    W.addDrawings(list); return { ...txt(`Drew ${list.length} trend lines.`), summary: `Drew ${list.length} trend line${list.length === 1 ? '' : 's'}` };
  }
  if (name === 'clear_ai_drawings') { W.pushUndo(); clearAi(); return { ...txt('Cleared.'), summary: 'Cleared my drawings' }; }
  if (name === 'set_chart') {
    if (i.new_tab) W.newTab((i.symbol || tab.symbol).toUpperCase(), W.normTfStrict(i.timeframe || tab.tf) || tab.tf);
    else { if (i.symbol) await W.setSymbol(i.symbol); if (i.timeframe) { const tf = W.normTfStrict(i.timeframe); if (!tf) return { error: true, ...txt(`Unknown timeframe ${i.timeframe}`) }; await W.setTf(tf); } }
    if (i.chart_type) W.setType(i.chart_type);
    await new Promise(r => setTimeout(r, 150));
    const t = W.tab(); return { ...txt(`Chart is now ${t.symbol} ${t.tf} (${t.type}). ${W.activeBars().length} bars loaded.`), summary: `Switched to ${t.symbol} ${t.tf}` };
  }
  if (name === 'add_indicator') {
    const s = findScript(i.name) || W.state.scripts.find(x => x.name.toLowerCase() === String(i.name).toLowerCase());
    if (!s) return { error: true, ...txt(`No indicator named "${i.name}". Library: ${LIBRARY.map(x => x.name).join(', ')}`) };
    const ind = W.addIndicator({ scriptId: s.id });
    if (i.inputs && typeof i.inputs === 'object') {
      const d = W.indOut.find(x => x.id === ind.id), vals = {};
      for (const [k, v] of Object.entries(i.inputs)) { const spec = d.out.inputs.find(x => x && x.title.toLowerCase() === k.toLowerCase()); if (spec) vals[spec.idx] = v; }
      if (Object.keys(vals).length) W.updateIndicator(ind.id, { inputs: vals });
    }
    const d = W.indOut.find(x => x.id === ind.id); return { ...txt({ added: s.name, summary: indSummary(d, W.activeBars().length - 1) }), summary: `Added ${s.name}` };
  }
  if (name === 'remove_indicator') {
    const q = String(i.name).toLowerCase(); const d = W.indOut.find(x => (x.out ? x.out.meta.title : x.title).toLowerCase().includes(q) || (x.out && (x.out.meta.shorttitle || '').toLowerCase() === q));
    if (!d) return { error: true, ...txt(`Not on the chart: ${i.name}`) }; W.removeIndicator(d.id); return { ...txt('Removed.'), summary: `Removed ${d.out ? d.out.meta.title : d.title}` };
  }
  if (name === 'write_pine_script') {
    const src = String(i.source || ''); try { compile(src); } catch (e) { return { error: true, ...txt(`Compile error at line ${e.line || '?'}: ${e.message}`), summary: `Pine compile error (line ${e.line || '?'})` }; }
    openInEditor({ src, name: i.name || 'Quant script' });
    if (i.add_to_chart === false) return { ...txt('Compiled and opened in the editor.'), summary: `Opened ${i.name || 'script'} in the editor` };
    const r = runOnChart();
    if (!r.ok) return { error: true, ...txt(`Runtime error: ${r.out && r.out.error ? `line ${r.out.error.line}: ${r.out.error.message}` : 'unknown'}`), summary: 'Pine runtime error' };
    if (i.open_in_editor === false && W.currentSide() === 'editor') W.openSide('editor');
    const d = W.indOut.find(x => x.id === r.indicatorId), o = d.out;
    return { ...txt({ ok: true, title: o.meta.title, plots: o.plots.length, shapes_fired: o.shapes.map(s => ({ title: s.title, count: s.values.filter(Number.isFinite).length })), labels: o.labels.length, boxes: o.boxes.length, lines: o.lines.length, logs: o.logs.slice(0, 5), summary: indSummary(d, W.activeBars().length - 1) }), summary: `Added “${o.meta.title}” to the chart` };
  }
  if (name === 'get_account') { const a = W.account || await api('paper'); return { ...txt({ equity: a.equity, cash: a.cash, open_pnl: a.open_pnl, today_pnl: a.today_pnl, positions: a.positions, working_orders: a.orders, recent_trades: a.trades.slice(0, 10) }), summary: 'Read the paper account' }; }
  if (name === 'place_paper_order') {
    const req = { symbol: (i.symbol || tab.symbol).toUpperCase(), side: i.side, qty: +i.qty, type: i.type || 'market', source: 'quant', note: i.reason || '' };
    if (i.price) req.price = +i.price; if (i.stop_loss) req.sl = +i.stop_loss; if (i.take_profit) req.tp = +i.take_profit;
    const ok = await confirmCard(req);
    if (!ok) return { ...txt('The user declined the order.'), summary: 'Order declined' };
    try { const o = await api('paper/order', req); W.account = o.account; W.emit('account', o.account); W.orderLines(); return { ...txt({ status: o.order.status, fill_price: o.order.fill_price, order: o.order }), summary: `Paper order ${o.order.status}: ${req.side} ${req.qty} ${req.symbol}` }; }
    catch (e) { return { error: true, ...txt('Rejected: ' + e.message), summary: 'Order rejected: ' + e.message }; }
  }
  if (name === 'take_screenshot') { const url = W.chart.snapshot(1400); return { content: [{ type: 'image', source: { type: 'base64', media_type: 'image/jpeg', data: url.split(',')[1] } }], summary: 'Looked at the chart' }; }
  return { error: true, ...txt('Unknown tool ' + name) };
}
function confirmCard(req) {
  const ask = W.account ? W.account.settings.ask_before : true;
  if (!ask) return Promise.resolve(true);
  return new Promise(res => {
    const c = h(`<div class="confirm"><div class="dim" style="font-size:11px;margin-bottom:4px">Quant wants to place a paper order</div><b class="${req.side === 'buy' ? 'up' : 'dn'}">${req.side.toUpperCase()} ${req.qty} ${esc(req.symbol)}</b> ${req.type === 'market' ? 'at market' : `${req.type} @ ${req.price}`}${req.sl ? ` · SL ${req.sl}` : ''}${req.tp ? ` · TP ${req.tp}` : ''}${req.note ? `<div class="note" style="margin-top:4px">${esc(req.note)}</div>` : ''}
      <div class="row"><button class="btn sm ghost" data-a="no">Decline</button><button class="btn sm ${req.side === 'buy' ? 'buy' : 'sell'}" data-a="yes">Place paper order</button></div></div>`);
    c.onclick = e => { const a = e.target.closest('[data-a]'); if (!a) return; c.querySelectorAll('button').forEach(b => { b.disabled = true; }); c.querySelector('.row').innerHTML = `<span class="dim">${a.dataset.a === 'yes' ? 'Placed' : 'Declined'}</span>`; res(a.dataset.a === 'yes'); };
    body.appendChild(c); scroll();
  });
}

// ------------------------------------------------------------------------------------------------ markdown (small)
function md(src) {
  const blocks = []; let s = String(src || '').replace(/```(\w*)\n?([\s\S]*?)(```|$)/g, (_, lang, code) => { blocks.push({ lang, code }); return `\u0000${blocks.length - 1}\u0000`; });
  s = esc(s);
  const inline = t => t.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>');
  const lines = s.split('\n'); const out = []; let list = null, para = [], table = null;
  const flushP = () => { if (para.length) { out.push(`<p>${inline(para.join('<br>'))}</p>`); para = []; } };
  const flushL = () => { if (list) { out.push(`<${list.t}>${list.items.map(x => `<li>${inline(x)}</li>`).join('')}</${list.t}>`); list = null; } };
  const flushT = () => { if (table) { out.push(`<table>${table.map((r, k) => `<tr>${r.map(c => k === 0 ? `<th>${inline(c)}</th>` : `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</table>`); table = null; } };
  for (const line of lines) {
    let m;
    if ((m = line.match(/^\u0000(\d+)\u0000$/))) { flushP(); flushL(); flushT(); const b = blocks[+m[1]]; out.push(`<pre data-lang="${esc(b.lang)}"><code>${esc(b.code.replace(/\n$/, ''))}</code></pre>`); continue; }
    if (/^\s*\|.*\|\s*$/.test(line)) { flushP(); flushL(); if (/^\s*\|[\s:|-]+\|\s*$/.test(line)) continue; (table = table || []).push(line.trim().slice(1, -1).split('|').map(x => x.trim())); continue; } else flushT();
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { flushP(); if (!list || list.t !== 'ul') { flushL(); list = { t: 'ul', items: [] }; } list.items.push(m[1]); continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { flushP(); if (!list || list.t !== 'ol') { flushL(); list = { t: 'ol', items: [] }; } list.items.push(m[1]); continue; }
    if ((m = line.match(/^#{1,6}\s+(.*)/))) { flushP(); flushL(); out.push(`<h4>${inline(m[1])}</h4>`); continue; }
    if (!line.trim()) { flushP(); flushL(); continue; }
    flushL(); para.push(line);
  }
  flushP(); flushL(); flushT();
  return out.join('').replace(/\u0000(\d+)\u0000/g, (_, k) => `<pre><code>${esc(blocks[+k].code)}</code></pre>`);
}
function wireCode(el) {
  el.querySelectorAll('pre').forEach(pre => {
    const code = pre.textContent; if (!/indicator\(|strategy\(|@version/.test(code) || pre.nextElementSibling?.classList.contains('code-acts')) return;
    const acts = h(`<div class="code-acts"><button class="btn sm primary">Add to chart</button><button class="btn sm">Open in editor</button></div>`);
    acts.children[0].onclick = () => { openInEditor({ src: code, name: 'Quant script', run: true }); };
    acts.children[1].onclick = () => openInEditor({ src: code, name: 'Quant script' });
    pre.after(acts);
  });
}
void toast;
