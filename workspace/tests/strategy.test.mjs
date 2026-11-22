import { compile, run } from "../web/pine.js";
import assert from "node:assert/strict";
const mk = (rows) => rows.map(([o, h, l, c], i) => ({ t: 1700000000 + i * 300, o, h, l, c, v: 100 }));
const S = (body, bars, o) => { const r = run(compile('//@version=6\nstrategy("t", overlay = true' + (o || '') + ')\n' + body), bars, { tf: '5m' }); if (r.error) throw new Error(r.error.message + ' @' + r.error.line); return r.strategy; };
// market entry at next open, exit by limit
let st = S('if bar_index == 0\n    strategy.entry("L", strategy.long, 10)\n    strategy.exit("x", "L", limit = 103, stop = 98)', mk([[100,101,99,100],[100.5,102,100,101],[101,104,100.5,103.5],[103,103,102,102]]));
assert.equal(st.trades.length, 1); assert.equal(st.trades[0].entryPx, 100.5); assert.equal(st.trades[0].exitPx, 103); assert.equal(st.trades[0].pnl, 25);
// stop hit first on a down bar; gap through stop fills at open
st = S('if bar_index == 0\n    strategy.entry("L", strategy.long, 1)\n    strategy.exit("x", "L", limit = 110, stop = 98)', mk([[100,101,99,100],[100,100,99,99.5],[97,97.5,96,97]]));
assert.equal(st.trades[0].exitPx, 97);
// short with profit / loss in ticks, commission per contract
st = S('if bar_index == 0\n    strategy.entry("S", strategy.short, 2)\n    strategy.exit("x", "S", profit = 100, loss = 100)', mk([[100,100,100,100],[100,100.2,99,99.2],[99,99.5,98.5,99]]), ', commission_type = strategy.commission.cash_per_contract, commission_value = 1');
assert.equal(st.trades[0].exitPx, 99); assert.equal(+st.trades[0].pnl.toFixed(6), 2 * 1 - 4);
// reversal, process_orders_on_close, close_all, position vars
st = S('if bar_index == 0\n    strategy.entry("L", strategy.long, 1)\nif bar_index == 1\n    strategy.entry("S", strategy.short, 1)\nif bar_index == 2\n    strategy.close_all()\nplot(strategy.position_size)', mk([[10,10,10,10],[11,11,11,11],[12,12,12,12],[13,13,13,13]]), ', process_orders_on_close = true');
assert.deepEqual(st.trades.map(t => [t.dir, t.entryPx, t.exitPx]), [[1, 10, 11], [-1, 11, 12]]);
// percent of equity sizing
st = S('if bar_index == 0\n    strategy.entry("L", strategy.long)', mk([[100,100,100,100],[100,100,100,100]]), ', default_qty_type = strategy.percent_of_equity, default_qty_value = 50, initial_capital = 10000');
assert.equal(st.open[0].qty, 50);
// day / hour filter (Research): no entries on the left-out weekdays / hours (New York)
{
  const days = Array.from({ length: 10 }, (_, i) => ({ t: Date.UTC(2026, 10, 2 + i, 15) / 1000, o: 100, h: 101, l: 99, c: 100, v: 1 }));  // 10:00 NY, Mon Nov 2 ..
  const c = compile('//@version=6\nstrategy("d", process_orders_on_close = true)\nstrategy.entry("L", strategy.long)\nstrategy.close("L", immediately = false)');
  const all = run(c, days, { symbol: 'X', tf: '1D' }).strategy, f = run(c, days, { symbol: 'X', tf: '1D', strategy: { skip: ['Wed|*', '*|11'] } }).strategy;
  const wd = t => new Date(t * 1000).getUTCDay();
  assert.ok(all.trades.some(t => wd(t.entryTime) === 3) && !f.trades.some(t => wd(t.entryTime) === 3) && f.trades.length < all.trades.length, 'weekday filter');
}
// market condition filter: high-volatility days only -> fewer trades, all on days after a wide day
{
  const bars = []; for (let d = 0; d < 80; d++) { const w = d % 3 === 0 ? 10 : 1; bars.push({ t: Date.UTC(2026, 0, 1 + d, 15) / 1000, o: 100, h: 100 + w, l: 100 - w, c: 100, v: 1 }); }
  const c = compile('//@version=6\nstrategy("r")\nstrategy.entry("L", strategy.long)\nstrategy.close("L")');
  const a = run(c, bars, { symbol: 'X', tf: '1D' }).strategy, h = run(c, bars, { symbol: 'X', tf: '1D', strategy: { regime: { vol: 'high', trend: 'any' } } }).strategy;
  assert.ok(h.trades.length > 0 && h.trades.length < a.trades.length / 2, `regime filter ${h.trades.length} / ${a.trades.length}`);
}
console.log('strategy tests passed; metrics sample', JSON.stringify(st.metrics).slice(0, 80));
