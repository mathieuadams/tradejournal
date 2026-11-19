// node workspace/tests/pine.test.mjs — runs every library script and a few language checks.
import { compile, run, securityRequests } from '../web/pine.js';
import { LIBRARY } from '../web/library.js';
import assert from 'node:assert/strict';

function bars(n, tf = 300) {
  let p = 100, seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const out = []; const t0 = Date.UTC(2026, 9, 1, 13, 30) / 1000;
  for (let i = 0; i < n; i++) { const o = p; const c = o * (1 + (rnd() - 0.5) * 0.01); const h = Math.max(o, c) * (1 + rnd() * 0.003), l = Math.min(o, c) * (1 - rnd() * 0.003); out.push({ t: t0 + i * tf, o, h, l, c, v: 1000 + rnd() * 5000 }); p = c; }
  return out;
}
const B = bars(3000);
let fails = 0;
for (const s of LIBRARY) {
  try {
    const r = run(compile(s.src), B, { tf: '5m', tfSec: 300, symbol: 'TEST' });
    if (r.error) throw new Error(`line ${r.error.line}: ${r.error.message}`);
    const nPlots = r.plots.filter(p => p.values.some(v => !Number.isNaN(v))).length;
    console.log(`ok  ${s.id.padEnd(11)} ${String(r.ms).padStart(4)}ms plots=${r.plots.length}(${nPlots} with data) shapes=${r.shapes.length} lines=${r.lines.length} labels=${r.labels.length} boxes=${r.boxes.length} inputs=${r.inputs.length}${r.logs.length ? ' logs=' + r.logs.map(l => l.msg).join('|') : ''}`);
  } catch (e) { fails++; console.log(`FAIL ${s.id}: ${e.message}`); }
}
// language checks
const R = (src, n = 50) => { const r = run(compile('//@version=6\nindicator("t")\n' + src), bars(n), { tf: '5m' }); if (r.error) throw new Error(r.error.message + ' @' + r.error.line); return r; };
const last = (r, k = 0) => r.plots[k].values[r.plots[k].values.length - 1];
assert.equal(last(R('var x = 0\nx += 1\nplot(x)')), 50);
assert.equal(last(R('x = 0\nx := x + 1\nplot(x)')), 1);
assert.equal(last(R('f(a) =>\n    s = 0.0\n    for i = 1 to a\n        s += i\n    s\nplot(f(4))')), 10);
assert.equal(last(R('v = if bar_index > 10\n    1\nelse\n    2\nplot(v)')), 1);
assert.equal(last(R('k = switch\n    bar_index > 100 => 1\n    bar_index > 10 => 2\n    => 3\nplot(k)')), 2);
assert.equal(last(R('c = ta.sma(close, 3)\nplot(c - (close + close[1] + close[2]) / 3)')).toFixed(8), '0.00000000');
assert.equal(last(R('a = array.new_float()\nfor i = 0 to 4\n    a.push(i)\nplot(a.sum())')), 10);
assert.equal(last(R('counter() =>\n    var n = 0\n    n += 1\n    n\nplot(counter() + counter())')), 100);
assert.equal(last(R('plot(bar_index[1])')), 48);
assert.equal(last(R('m = input.int(5, "M")\nplot(m)')), 5);
{ const r = run(compile('//@version=6\nindicator("t")\nm = input.int(5, "M")\nplot(m)'), bars(5), { inputs: { 0: 9 } }); assert.equal(last(r), 9); assert.equal(r.inputs[0].title, 'M'); }
{ let err; try { compile('//@version=6\nindicator("t")\nplot(foo)'); } catch (e) { err = e; } assert.ok(err && err.line === 3, 'undeclared identifier error with line'); }
{ const r = run(compile('//@version=6\nindicator("t")\nx = array.new_float()\nplot(x.get(3))'), bars(5)); assert.ok(r.error && r.error.line === 4); }
assert.equal(last(R('s = "a" + str.tostring(1.5)\nplot(str.length(s))')), 4);
assert.equal(last(R('plot(nz(na, 7))')), 7);
assert.equal(last(R('[a, b] = ta.macd(close, 12, 26, 9)\nplot(na(a) ? 0 : 1)', 100)), 1);
// request.security on another symbol / timeframe: a second pass on its bars, aligned on the bar closes
{
  const mk = (n, f, step = 300) => Array.from({ length: n }, (_, i) => { const c = f(i); return { t: 1700000100 + i * step, o: c, h: c + 1, l: c - 1, c, v: 1 }; });
  const c = compile('//@version=6\nindicator("s")\nf() =>\n    var float s = 0\n    s += close\n    [s, close * 2]\n[a, b] = request.security("CME_MINI:NQ1!", timeframe.period, f())\nplot(a)\nplot(b)\nd = request.security("NQ", "15", close)\nplot(d)');
  const main = mk(12, i => 100 + i);
  assert.deepEqual(securityRequests(c, main, { symbol: 'QQQ', tf: '5m' }).map(r => r.key), ['NQ|5m', 'NQ|15m']);
  const r = run(c, main, { symbol: 'QQQ', tf: '5m', securities: { 'NQ|5m': mk(12, i => 1000 + i), 'NQ|15m': mk(4, i => 5000 + i, 900) } });
  assert.ok(!r.error, r.error && r.error.message);
  assert.deepEqual(r.plots[0].values.slice(0, 4), [1000, 2001, 3003, 4006]);
  assert.deepEqual(r.plots[1].values.slice(0, 2), [2000, 2002]);
  assert.deepEqual(r.plots[2].values.slice(0, 7).map(v => Number.isNaN(v) ? null : v), [null, null, 5000, 5000, 5000, 5001, 5001]);
  const r2 = run(c, main, { symbol: 'QQQ', tf: '5m' });             // no data: the chart's own series + a warning
  assert.equal(r2.plots[2].values[0], 100); assert.ok(r2.logs.some(l => /request.security/.test(l.msg)));
}
console.log(fails ? `${fails} library script(s) failed` : 'all pine tests passed');
process.exit(fails ? 1 : 0);
