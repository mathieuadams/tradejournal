// Runs a Pine strategy for the server (frozen strategies): JSON on stdin -> JSON on stdout.
//   in:  { src, inputs, symbol, tf, bars: [[t,o,h,l,c,v]...], securities: { "NQ|5m": [[...]] }, strategy: { comType, comVal, slip } }
//   out: { trades, open, metrics, logs, error }
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const code = readFileSync(join(here, '..', 'web', 'pine.js'), 'utf8');          // (imported as a module whatever package.json says)
const P = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));

let raw = '';
for await (const chunk of process.stdin) raw += chunk;
const m = JSON.parse(raw);
const toBars = xs => (xs || []).map(b => ({ t: b[0], o: b[1], h: b[2], l: b[3], c: b[4], v: b[5] || 0 }));
let out;
try {
  const c = P.compile(m.src), secs = {};
  for (const [k, xs] of Object.entries(m.securities || {})) secs[k] = toBars(xs);
  const r = P.run(c, toBars(m.bars), { inputs: m.inputs || null, symbol: m.symbol, tf: m.tf, tfSec: P.tfSeconds(m.tf), strategy: m.strategy || null, securities: secs, budgetMs: 60000 });
  if (r.error) out = { error: `line ${r.error.line}: ${r.error.message}` };
  else if (!r.strategy) out = { error: 'not a strategy (use strategy(...) instead of indicator(...))' };
  else {
    const S = r.strategy;
    out = { trades: S.trades.map(t => ({ dir: t.dir, qty: t.qty, entryTime: t.entryTime, entryPx: t.entryPx, exitTime: t.exitTime, exitPx: t.exitPx, pnl: t.pnl, why: t.why, id: t.id })),
      open: S.open.map(l => ({ dir: l.dir, qty: l.qty, entryTime: l.t, entryPx: l.px, pnl: l.pnl, id: l.id })), logs: r.logs.map(l => l.msg) };
  }
} catch (e) { out = { error: (e.line ? `line ${e.line}: ` : '') + e.message }; }
process.stdout.write(JSON.stringify(out));
