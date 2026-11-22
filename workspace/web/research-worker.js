// Runs one backtest of a Pine strategy per message, off the main thread (the research panel uses several of these).
import { compile, run, metrics } from './pine.js';

let bars = null, key = null, securities = null;
const compiled = new Map();

self.onmessage = e => {
  const m = e.data;
  if (m.type === 'bars') { bars = m.bars; key = m.key; securities = m.securities || null; self.postMessage({ type: 'ready', key }); return; }
  if (m.type !== 'run') return;
  try {
    let c = compiled.get(m.src); if (!c) { c = compile(m.src); compiled.set(m.src, c); }
    const r = run(c, bars, { inputs: m.inputs, symbol: m.symbol, tf: m.tf, tfSec: m.tfSec, strategy: m.strategy, securities, budgetMs: 120000 });
    if (r.error) { self.postMessage({ type: 'result', id: m.id, error: `line ${r.error.line}: ${r.error.message}` }); return; }
    if (!r.strategy) { self.postMessage({ type: 'result', id: m.id, error: 'not a strategy' }); return; }
    const T = r.strategy.trades, cut = m.splitTime;
    const res = { type: 'result', id: m.id, all: metrics(T), train: metrics(T.filter(t => t.entryTime < cut)), test: metrics(T.filter(t => t.entryTime >= cut)),
      open: r.strategy.open.length, inputs: r.inputs.filter(Boolean).map(i => ({ idx: i.idx, title: i.title, type: i.type, value: i.value, defval: i.defval, options: i.options, minval: i.minval, maxval: i.maxval, step: i.step })),
      settings: r.strategy.settings, ms: r.ms };
    if (m.full) res.trades = T.map(t => ({ dir: t.dir, qty: t.qty, entryTime: t.entryTime, entryPx: t.entryPx, exitTime: t.exitTime, exitPx: t.exitPx, pnl: t.pnl, pct: t.pct, why: t.why, id: t.id, bars: t.exitBar - t.entryBar, bar: t.entryBar }));
    self.postMessage(res);
  } catch (err) {
    self.postMessage({ type: 'result', id: m.id, error: (err.line ? `line ${err.line}: ` : '') + err.message });
  }
};
