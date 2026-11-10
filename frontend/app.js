/* Tradejournal Futures: plain JS, no build step. Talks to the HTTP API with the Cognito ID token. */
const C = window.TJ_CONFIG;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (p, d = 2) => p == null || !Number.isFinite(+p) ? '—' : (+p).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const money = (v, signed = true) => v == null ? '—' : (v < 0 ? '−' : signed && v > 0 ? '+' : '') + '$' + Math.abs(Math.round(v)).toLocaleString('en-US');
const sgn = (v, d = 2, suf = '') => v == null ? '—' : (v < 0 ? '−' : v > 0 ? '+' : '') + Math.abs(v).toFixed(d) + suf;
const cls = v => v > 0 ? 'gain' : v < 0 ? 'loss' : '';
const today = () => new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
const addDays = (d, n) => { const x = new Date(d + 'T12:00:00Z'); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove('show'), 2600); }

async function api(path, { method = 'GET', body } = {}) {
  const token = await Auth.token();
  if (!token) { Auth.login(); return new Promise(() => {}); }
  let r;
  const ac = new AbortController(), tm = setTimeout(() => ac.abort(), 45000);
  try { r = await fetch(C.apiUrl + path, { method, headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined, signal: ac.signal }); }
  catch (e) { throw new Error(ac.signal.aborted ? 'The server didn’t answer in time.' : 'Can’t reach the server.'); }
  finally { clearTimeout(tm); }
  if (r.status === 401) { Auth.login(); return new Promise(() => {}); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(data.error || `Request failed (${r.status}).`); e.status = r.status; throw e; }
  return data;
}

const S = { me: null, page: '', timers: [], chart: { sym: 'NQ', tf: 5, days: 3, strat: '', wt: true, cvd: true, prof: true, rth: false, ema: true, emaLen: 20, fvg: false, sd: false }, rp: null, filter: 'All', newSetup: null, trSet: 'study' };
const SIDE = [['long', 'Long'], ['short', 'Short'], ['both', 'Both']];
const BIASP = ['bias', 'EMA bias', 'sel', [['all3', '15 / 30 / 60-min 20 EMAs all agree'], ['h1', 'Hourly 20 EMA only'], ['none', 'None']]];
const MANAGEP = [['exit', 'Exit', 'sel', [['target', 'Stop / target'], ['trail', 'Managed: break-even, partial, trailing']]], ['tgtR', 'Target (R, 0 = none)', 'num', 0.5],
  ['beR', 'Managed: break-even at (R, 0 = off)', 'num', 0.25], ['partR', 'Managed: partial at (R)', 'num', 0.25], ['partPct', 'Managed: partial size (%, 0 = none)', 'num', 5],
  ['trail', 'Managed: trail the rest', 'sel', [['swing', 'Under the last N candles\' low / high'], ['r', 'R behind the best price'], ['ema15', 'Close through the 15-min 20 EMA'], ['none', 'No trail (break-even only)']]],
  ['trailN', 'Managed: trail N candles', 'num', 1], ['trailR', 'Managed: trail R behind', 'num', 0.1]];
const TAILP = [['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']];
const SETUPS = {
  orb: { name: 'Opening range break + FVG retest', blurb: 'A close outside the opening range that leaves a fair value gap, a retest of the gap, an engulfing candle: enter, target in R.',
    params: [['rangeMin', 'Opening range (minutes)', 'sel', [[1, '1'], [5, '5'], [10, '10'], [15, '15'], [30, '30'], [60, '60']]], ['tgtR', 'Target (R)', 'num', 0.5], ['fvgWin', 'Gap within (candles)', 'num', 1],
      ['confWin', 'Confirmation within (candles)', 'num', 1], ['lastEntry', 'Last entry (ET)', 'text'], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  orbema: { name: 'Opening range break + 20 EMA retest', blurb: 'A close outside the 15 / 30-minute opening range, then price closes back through the 20 EMA and back out: enter, stop past that candle, target the low / high of the day.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min']]], ['rangeMin', 'Opening range (minutes)', 'sel', [[15, '15'], [30, '30']]], ['ema', 'EMA length', 'num', 1], ['emaOn', 'EMA on', 'sel', [['full', 'Full session (overnight too)'], ['rth', 'Regular hours only']]],
      ['entryAt', 'Entry', 'sel', [['close', 'At the confirmation close'], ['open', 'Next candle open']]], ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ['minRR', 'Skip if the target is under (R, 0 = never)', 'num', 0.25], ['lastEntry', 'Last entry (ET)', 'text'],
      ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  range50: { name: 'Range 50% retrace', blurb: 'A leg, a retrace to its 50%, then back to 25% of the range: enter there, target the 50%, stop past the range end.',
    params: [['tf', 'Candles', 'sel', [[1, '1 min'], [5, '5 min']]], ['legPct', 'Minimum leg (% of price)', 'num', 0.05], ['entryLvl', 'Entry (share of the range)', 'num', 0.05], ['targetLvl', 'Target (share of the range)', 'num', 0.05],
      ['stopBuf', 'Stop past the range end (share)', 'num', 0.05], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  silver: { name: 'ICT Silver Bullet', blurb: '10:00–11:00 (and the other windows): a liquidity sweep sets the bias, enter on the fair value gap, target in R.',
    params: [['tf', 'Candles', 'sel', [[1, '1 min'], [5, '5 min']]], ['pivLen', 'Swing length', 'num', 1], ['expiry', 'Bias expires after (candles)', 'num', 1], ['rr', 'Target (R)', 'num', 0.5],
      ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ['flatEod', 'Flat at 15:55', 'bool'], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  tvpoc: { name: 'Trend volume profile + rolling POC', blurb: 'BigBeluga: bands = MA ± ATR; a close over the upper band starts a bull trend, under the lower band a bear one. Enter on the trend change or on a retest of the rolling POC.',
    params: [['tf', 'Candles', 'sel', [['15', '15 min'], ['5', '5 min'], ['60', '1 hour'], ['1D', 'Daily']]], ['entry', 'Entry', 'sel', [['flip', 'At the trend change'], ['poc', 'Retest of the rolling POC']]],
      ['maType', 'MA type', 'sel', [['SMA', 'SMA'], ['EMA', 'EMA'], ['WMA', 'WMA'], ['HMA', 'HMA'], ['RMA', 'RMA'], ['VWMA', 'VWMA']]], ['maLen', 'MA length', 'num', 1], ['atrLen', 'ATR length', 'num', 1],
      ['atrMult', 'ATR multiplier', 'num', 0.1], ['binMult', 'Profile bin (× ATR)', 'num', 0.01], ['minBars', 'POC retest: trend at least (candles)', 'num', 1],
      ['stop', 'Stop', 'sel', [['band', 'Opposite band'], ['none', 'None (exit on trend change)']]], ['flatEod', 'Intraday: flat at 15:55', 'bool'], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  vwapret: { name: 'VWAP retest (overnight included)', blurb: 'Short: below the VWAP, a green candle tests it from underneath without closing through it; the first red candle after it (closing under) is the entry. Long: the mirror. VWAP from 18:00, overnight trades allowed.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min'], [15, '15 min']]], ['hours', 'Hours', 'sel', [['all', 'Overnight + day'], ['on', 'Overnight only'], ['rth', 'Regular hours only']]],
      ['trend', 'Closes on one side before the test', 'num', 1], ['wait', 'Entry within (candles after the test)', 'num', 1], ['tgtR', 'Target (R)', 'num', 0.5], ['bufTicks', 'Stop buffer (ticks)', 'num', 1],
      ['maxDay', 'Max trades per session', 'num', 1], ['flatAt', 'Flat at (ET)', 'text'], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  onsweep: { name: 'Overnight high / low sweep and reclaim', blurb: 'In the first hour, price runs past the overnight high (or low) and closes back inside: fade it, stop past the sweep, target the VWAP / overnight mid / other side.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min']]], ['lastEntry', 'Last entry (ET)', 'text'], ['maxAbove', 'Cancel after (closes outside)', 'num', 1],
      ['tgt', 'Target', 'sel', [['vwap', 'VWAP'], ['mid', 'Middle of the overnight range'], ['other', 'Other side of the overnight range'], ['r', 'Fixed R']]], ['tgtR', 'Fixed R target', 'num', 0.5],
      ['minRR', 'Skip if the target is under (R)', 'num', 0.25], ['minDepth', 'Sweep at least (points past the level)', 'num', 1], ['minOnPct', 'Overnight range at least (%)', 'num', 0.05],
      ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  ibbreak: { name: 'Initial balance breakout', blurb: 'The 09:30–10:30 range: trade the first close outside it (on the VWAP side, or with the overnight trend), target 1× the range past the edge.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [15, '15 min'], [1, '1 min']]], ['ibMin', 'Initial balance', 'sel', [[60, '60 min (09:30–10:30)'], [30, '30 min (09:30–10:00)']]],
      ['filter', 'Filter', 'sel', [['vwap', 'Breakout on the VWAP side'], ['overnight', 'With the overnight trend'], ['none', 'None']]], ['onMinPct', 'Overnight trend: move at least (%)', 'num', 0.05], ['stop', 'Stop', 'sel', [['mid', 'Middle of the IB'], ['opp', 'Other side of the IB'], ['bar', 'Breakout candle']]],
      ['tgtMult', 'Target (× IB range past the edge)', 'num', 0.25], ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ['lastEntry', 'Last entry (ET)', 'text'], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  odpull: { name: 'Opening drive, first pullback', blurb: 'A strong 09:30–09:45 move closing near its extreme, then the first pullback to the 9 EMA: enter on the break of the previous candle, stop past the pullback.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min']]], ['driveMin', 'Drive', 'sel', [[15, '09:30–09:45'], [30, '09:30–10:00']]], ['drivePct', 'Drive at least (%)', 'num', 0.05],
      ['strength', 'Close within (share of the drive range)', 'num', 0.05], ['pbMin', 'Pullback candles at least', 'num', 1], ['ema', 'EMA', 'num', 1], ['tgtR', 'Target (R)', 'num', 0.5],
      ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ['lastEntry', 'Last entry (ET)', 'text'], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  pdbreak: { name: 'Prior-day high / low break and retest', blurb: 'Daily level, intraday entry: a 15-minute close past yesterday\'s high (or low), then a 5-minute retest that holds: enter, stop back inside, target 1× the daily ATR from the open.',
    params: [['tf', 'Entry candles', 'sel', [[5, '5 min'], [15, '15 min']]], ['brkTf', 'Break confirmed on', 'sel', [[15, '15-min close'], [5, '5-min close'], [30, '30-min close'], [60, '1-hour close']]],
      ['bias', 'Daily bias', 'sel', [['close', 'Where yesterday closed (top / bottom of its range)'], ['trend', 'Yesterday vs its 20-day average'], ['none', 'None']]], ['closePct', 'Closed in the top / bottom (share)', 'num', 0.05],
      ['wait', 'Retest within (candles)', 'num', 1], ['tolTicks', 'Retest within (ticks of the level)', 'num', 1], ['tgt', 'Target', 'sel', [['atr', 'Open ± × daily ATR'], ['r', 'Fixed R']]],
      ['tgtAtr', 'Target (× daily ATR)', 'num', 0.25], ['tgtR', 'Fixed R target', 'num', 0.5], ['minRR', 'Skip if the target is under (R)', 'num', 0.25], ['bufTicks', 'Stop buffer (ticks)', 'num', 1],
      ['lastEntry', 'Last entry (ET)', 'text'], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  pdsweep: { name: 'Prior-day high / low sweep and reclaim', blurb: 'Daily level, intraday entry: price runs past yesterday\'s high (or low) and closes back inside: fade it, stop past the sweep, target the VWAP / middle / other side of yesterday.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min']]], ['levels', 'Levels', 'sel', [['pd', 'Yesterday\'s high / low'], ['outer', 'Outer of yesterday and the overnight']]], ['fresh', 'Skip a level the overnight already took', 'bool'],
      ['lastEntry', 'Last entry (ET)', 'text'], ['maxAbove', 'Cancel after (closes outside)', 'num', 1], ['minDepth', 'Sweep at least (points past the level)', 'num', 1],
      ['tgt', 'Target', 'sel', [['vwap', 'VWAP'], ['mid', 'Middle of yesterday\'s range'], ['other', 'Other side of yesterday\'s range'], ['r', 'Fixed R']]], ['tgtR', 'Fixed R target', 'num', 0.5],
      ['minRR', 'Skip if the target is under (R)', 'num', 0.25], ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  nrbreak: { name: 'Narrow-range day breakout', blurb: 'Daily setup, intraday entry: after an NR7 or inside day, trade the first 15-minute opening-range break in the 20-day trend\'s direction, target 1× the daily ATR.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [15, '15 min']]], ['cond', 'Yesterday was', 'sel', [['nr7', 'NR7 (narrowest of 7 days)'], ['inside', 'An inside day'], ['either', 'NR7 or inside'], ['any', 'Any day (comparison)']]],
      ['trend', 'Direction', 'sel', [['sma', 'With the 20-day trend'], ['none', 'Either way']]], ['orMin', 'Opening range', 'sel', [[15, '15 min'], [30, '30 min'], [60, '60 min']]],
      ['stop', 'Stop', 'sel', [['opp', 'Other side of the range'], ['mid', 'Middle of the range']]], ['tgtAtr', 'Target (× daily ATR from the open)', 'num', 0.25], ['minRR', 'Skip if the target is under (R)', 'num', 0.25],
      ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ['lastEntry', 'Last entry (ET)', 'text'], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  fpdisp: { name: 'Fair price displacement', blurb: 'Fair price = the 09:30 open (or the VWAP). Above it, a red candle with a bigger body than the previous one that closes below its wick: short, target the fair price (long: the mirror).',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min'], [15, '15 min']]], ['fair', 'Fair price', 'sel', [['open', 'The 09:30 open'], ['vwap', 'The VWAP']]],
      ['minAwayPct', 'Away from it at least (%)', 'num', 0.05], ['bodyMult', 'Body at least (× the previous)', 'num', 0.25],
      ['stop', 'Stop', 'sel', [['bar', 'Past the displacement candle'], ['swing', 'Past the last swing (N candles)']]], ['swingN', 'Swing: last N candles', 'num', 1],
      ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ['minRR', 'Skip if the target is under (R)', 'num', 0.25], ['firstEntry', 'First entry (ET)', 'text'], ['lastEntry', 'Last entry (ET)', 'text'],
      ['maxDay', 'Max trades per session', 'num', 1], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  ifvg: { name: 'FVG inversion with the EMA bias', blurb: '5-minute, NYSE session. Bias from the 20 EMAs on 15 / 30 / 60 minutes (price on their side and sloping that way), checked against the Asian session. A pullback leaves a fair value gap against the bias; a close through it = entry. Stop at the gap, target the previous liquidity (2R when it is closer).',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min']]], ['bias', 'Bias', 'sel', [['all3', '15 / 30 / 60-min 20 EMAs all agree'], ['h1', 'Hourly 20 EMA only']]],
      ['asia', 'Asian session (18:00–02:00) check', 'sel', [['dir', 'It closed in the bias direction'], ['hold', 'Price on the bias side of its middle'], ['none', 'None']]],
      ['minGap', 'Gap at least (ticks)', 'num', 1], ['fvgMax', 'Inversion within (candles)', 'num', 1], ['gapFrom', 'Gaps count from (ET, 18:00 = whole overnight)', 'text'], ['stop', 'Stop', 'sel', [['fvg', 'Far end of the gap'], ['swing', 'Pullback extreme']]],
      ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ['tgt', 'Target', 'sel', [['liq2r', 'Previous liquidity, at least 2R'], ['liq', 'Previous liquidity'], ['r', 'Fixed R']]], ['tgtR', 'Fixed R target', 'num', 0.5],
      ['pivLen', 'Liquidity: swing length', 'num', 1], ['liqLook', 'Liquidity: look back (candles)', 'num', 10], ['minRR', 'Skip if the target is under (R)', 'num', 0.25],
      ['lastEntry', 'Last entry (ET)', 'text'], ['maxDay', 'Max trades per session', 'num', 1],
      ['trigSize', 'Trigger candle: measure', 'sel', [['body', 'Body only'], ['range', 'Whole candle (wicks included)']]],
      ['trigMin', 'Trigger candle at least (× the average range, 0 = off)', 'num', 0.25], ['trigMax', 'Trigger candle at most (× avg range, 0 = off)', 'num', 0.25],
      ['bodyPct', 'Trigger body at least (% of its range, 0 = off)', 'num', 5],
      ['volMin', 'Only when the daily range percentile is at least (0-100)', 'num', 5], ['volMax', 'and at most (0-100)', 'num', 5],
      ['avgN', 'Average range over the last N candles', 'num', 1], ['trigN', 'Compare with the previous N candles (0 = off)', 'num', 1],
      ['trigPrevMult', 'Trigger at least (× the largest of those N)', 'num', 0.25], ['trigPrevMax', 'Trigger at most (× the largest of those N, 0 = off)', 'num', 0.25], ['trigEngulf', 'Trigger closes past those N candles\' high / low', 'bool'],
      ['hours', 'Hours', 'sel', [['rth', 'NYSE session only (09:35–last entry)'], ['all', 'Overnight + NYSE (from 18:00)']]],
      ['exit', 'Exit', 'sel', [['target', 'Stop / target'], ['trail', 'Managed: break-even, partial, trailing']]], ['beR', 'Managed: break-even at (R, 0 = off)', 'num', 0.25],
      ['partR', 'Managed: partial at (R)', 'num', 0.25], ['partPct', 'Managed: partial size (%, 0 = none)', 'num', 5],
      ['trail', 'Managed: trail the rest', 'sel', [['swing', 'Under the last N candles\' low / high'], ['r', 'R behind the best price'], ['ema15', 'Close through the 15-min 20 EMA'], ['none', 'No trail (break-even only)']]],
      ['trailN', 'Managed: trail N candles', 'num', 1], ['trailR', 'Managed: trail R behind', 'num', 0.1], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  biaspull: { name: 'EMA bias pullback (no FVG control)', blurb: 'The FVG inversion without the FVG: same EMA bias, hours and exit; enter on the first candle back with the bias after a 2+ candle pullback. If it does as well, the FVG adds nothing.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min']]], ['bias', 'Bias', 'sel', [['all3', '15 / 30 / 60-min 20 EMAs all agree'], ['h1', 'Hourly 20 EMA only']]],
      ['asia', 'Asian session (18:00–02:00) check', 'sel', [['none', 'None'], ['dir', 'It closed in the bias direction'], ['hold', 'Price on the bias side of its middle']]],
      ['pbMin', 'Pullback: candles against the bias at least', 'num', 1], ['minRisk', 'Skip if the stop is under (ticks)', 'num', 1], ['bufTicks', 'Stop buffer (ticks)', 'num', 1],
      ['hours', 'Hours', 'sel', [['all', 'Overnight + NYSE (from 18:00)'], ['rth', 'NYSE session only (09:35–last entry)']]], ['lastEntry', 'Last entry (ET)', 'text'], ['maxDay', 'Max trades per session', 'num', 1],
      ['exit', 'Exit', 'sel', [['trail', 'Managed: break-even, partial, trailing'], ['target', 'Stop / fixed R target']]], ['tgtR', 'Fixed R target', 'num', 0.5], ['beR', 'Managed: break-even at (R, 0 = off)', 'num', 0.25],
      ['partR', 'Managed: partial at (R)', 'num', 0.25], ['partPct', 'Managed: partial size (%, 0 = none)', 'num', 5],
      ['trail', 'Managed: trail the rest', 'sel', [['swing', 'Under the last N candles\' low / high'], ['r', 'R behind the best price'], ['ema15', 'Close through the 15-min 20 EMA'], ['none', 'No trail (break-even only)']]],
      ['trailN', 'Managed: trail N candles', 'num', 1], ['trailR', 'Managed: trail R behind', 'num', 0.1], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  ondrift: { name: 'Overnight drift (hold with the bias)', blurb: 'The overnight control: at 18:00 enter in the direction of the 15 / 30 / 60-min EMAs, hold until 09:30. If plain holding does as well as the overnight strategies, their entries add nothing.',
    params: [['entryAt', 'Enter at (ET)', 'sel', ['18:00', '19:00', '20:00', '22:00', '00:00', '02:00', '03:00'].map(x => [x, x])], ['exitAt', 'Out at (ET)', 'sel', ['00:00', '02:00', '04:00', '06:00', '08:30', '09:30', '11:00', '15:55'].map(x => [x, x])],
      BIASP, ['stopMult', 'Stop (× the average 5-min range, 0 = none)', 'num', 0.5], ...MANAGEP, ...TAILP] },
  ldnbrk: { name: 'London breakout of the Asian range', blurb: 'Asian range 18:00–02:00; the first 5-minute close outside it after 02:00 (London opens ~03:00), in the EMA bias direction. Stop in the range, target a multiple of it or the managed exit.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [15, '15 min']]], ['asiaEnd', 'Asian range until (ET)', 'sel', ['00:00', '01:00', '02:00', '03:00'].map(x => [x, x])], ['lastEntry', 'Last entry (ET)', 'text'],
      ['flatAt', 'Flat at (ET)', 'sel', ['06:00', '08:00', '09:30', '11:00', '15:55'].map(x => [x, x])], ['stop', 'Stop', 'sel', [['mid', 'Middle of the range'], ['opp', 'Other side of the range'], ['bar', 'Breakout candle']]],
      ['tgtMult', 'Target (× the range past the edge, 0 = none)', 'num', 0.25], ['minRangePct', 'Range at least (% of price, 0 = off)', 'num', 0.05], ['maxRangePct', 'Range at most (%, 0 = off)', 'num', 0.05],
      BIASP, ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ...MANAGEP.filter(x => x[0] !== 'tgtR'), ...TAILP] },
  smt: { name: 'ES / NQ divergence (SMT)', blurb: 'One index takes its recent high (or low) and the other doesn\'t: fade it on the first candle back, stop past the extreme, target the VWAP. Uses both NQ and ES data.',
    params: [['tradeSym', 'Trade', 'sel', [['NQ', 'NQ'], ['ES', 'ES']]], ['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min'], [15, '15 min']]], ['look', 'High / low of the last N candles', 'num', 1],
      ['wait', 'Entry within (candles)', 'num', 1], ['hours', 'Hours', 'sel', [['all', 'Overnight + NYSE'], ['rth', 'NYSE session only']]], ['lastEntry', 'Last entry (ET)', 'text'], ['maxDay', 'Max trades per session', 'num', 1],
      ['tgt', 'Target', 'sel', [['vwap', 'The session VWAP'], ['r', 'Fixed R']]], ['minRR', 'VWAP target: skip if under (R)', 'num', 0.25], BIASP, ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ...MANAGEP,
      ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10]] },
  flatrev: { name: 'Flat-bias mean reversion to VWAP', blurb: 'When the EMAs have no bias (the grey band), fade a stretched move: a close 2× the average range from the VWAP, then a candle back toward it. Target the VWAP.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min'], [15, '15 min']]], ['distMult', 'Away from the VWAP at least (× the average range)', 'num', 0.25],
      ['bias', 'Flat means', 'sel', [['all3', 'The 15 / 30 / 60-min EMAs disagree'], ['h1', 'The hourly EMA has no clear bias']]], ['hours', 'Hours', 'sel', [['all', 'Overnight + NYSE'], ['rth', 'NYSE session only']]],
      ['lastEntry', 'Last entry (ET)', 'text'], ['maxDay', 'Max trades per session', 'num', 1], ['minRR', 'Skip if the VWAP is under (R)', 'num', 0.25], ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ...MANAGEP.filter(x => x[0] !== 'tgtR'), ...TAILP] },
  postclose: { name: 'Post-close continuation (16:00-17:00)', blurb: 'The move from 15:00 into the 16:00 close carries on (or reverses) until the 17:00 halt: enter at the close, out at 16:55.',
    params: [['fromAt', 'Move measured from (ET)', 'sel', [['14:00', '14:00'], ['15:00', '15:00'], ['15:30', '15:30']]], ['minMovePct', 'Move at least (%)', 'num', 0.05],
      ['mode', 'Then', 'sel', [['follow', 'Follow it'], ['fade', 'Fade it']]], ['exitAt', 'Out at (ET)', 'sel', [['16:15', '16:15'], ['16:30', '16:30'], ['16:55', '16:55']]],
      ['stopMult', 'Stop (× the average 5-min range)', 'num', 0.5], BIASP, ...MANAGEP, ...TAILP] },
  sungap: { name: 'Sunday / reopen gap', blurb: 'The Sunday 18:00 open vs Friday\'s close: fade small gaps back to the close, follow big ones. Or every 18:00 reopen after the daily halt.',
    params: [['which', 'Gaps', 'sel', [['monday', 'Sunday open vs Friday\'s close'], ['all', 'Every 18:00 reopen']]], ['mode', 'Trade', 'sel', [['both', 'Fade small, follow big'], ['fade', 'Fade small only'], ['follow', 'Follow big only']]],
      ['smallPct', 'Small gap: under (%)', 'num', 0.05], ['bigPct', 'Big gap: at least (%)', 'num', 0.05], ['exitAt', 'Out at (ET)', 'sel', ['20:00', '22:00', '00:00', '02:00', '06:00', '09:30'].map(x => [x, x])],
      ['stopMult', 'Stop (× the average 5-min range)', 'num', 0.5], BIASP, ...MANAGEP, ...TAILP] },
  insidebar: { name: 'Inside bar breakout', blurb: 'A candle entirely inside the one before (wicks included); enter on the break of its high (long) or low (short), stop past its other side, target 2R. 1, 5 or 15-minute candles.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min'], [15, '15 min']]], ['entry', 'Entry', 'sel', [['break', 'Stop order 1 tick past the inside candle'], ['close', 'First close past it']]],
      ['wait', 'Break within (candles)', 'num', 1], ['minMother', 'Mother candle at least (× the average range, 0 = off)', 'num', 0.25], ['maxInside', 'Inside candle at most (% of the mother)', 'num', 5],
      ['hours', 'Hours', 'sel', [['rth', 'NYSE session only'], ['all', 'Overnight + NYSE']]], ['lastEntry', 'Last entry (ET)', 'text'], ['maxDay', 'Max trades per session', 'num', 1],
      BIASP, ['bufTicks', 'Extra stop buffer (ticks)', 'num', 1], ...MANAGEP, ...TAILP] },
  imom: { name: 'Intraday momentum (first → last half hour)', blurb: 'The move from yesterday\'s close (or the open) to 10:00 predicts the last half hour: go with it from 15:30 to 16:00. A published finding (Gao et al., 2018).',
    params: [['from', 'Move measured from', 'sel', [['close', 'Yesterday\'s 16:00 close (overnight included)'], ['open', 'Today\'s 09:30 open']]], ['minMovePct', 'Move at least (%)', 'num', 0.05],
      ['entryAt', 'Enter at (ET)', 'sel', [['14:00', '14:00'], ['15:00', '15:00'], ['15:30', '15:30'], ['15:45', '15:45']]], ['mode', 'Then', 'sel', [['follow', 'Follow it'], ['fade', 'Fade it']]],
      ['stopMult', 'Stop (× the average 5-min range, 0 = none)', 'num', 0.5], BIASP, ...MANAGEP, ...TAILP] },
  onrev: { name: 'Overnight → day reversal', blurb: 'A big gap between yesterday\'s close and today\'s open (0.6%+): fade it from the open, target yesterday\'s close, out by 11:30.',
    params: [['minPct', 'Gap at least (%)', 'num', 0.05], ['maxPct', 'Gap under (%, 0 = no limit)', 'num', 0.05], ['fill', 'Target (share of the gap back, 1 = the close)', 'num', 0.1],
      ['stopGap', 'Stop (× the gap past the open)', 'num', 0.1], ['exitAt', 'Out at (ET)', 'sel', [['10:00', '10:00'], ['10:30', '10:30'], ['11:30', '11:30'], ['13:00', '13:00'], ['15:55', '15:55']]], BIASP, ...MANAGEP.filter(x => x[0] !== 'tgtR'), ...TAILP] },
  gapfill: { name: 'Regular-hours gap fill', blurb: 'A small gap at the 09:30 open (0.1-0.5%): fade it back to yesterday\'s 16:00 close.',
    params: [['minPct', 'Gap at least (%)', 'num', 0.05], ['maxPct', 'Gap under (%)', 'num', 0.05], ['fill', 'Target (share of the gap back, 1 = the close)', 'num', 0.1],
      ['stopGap', 'Stop (× the gap past the open)', 'num', 0.1], ['exitAt', 'Out at (ET)', 'sel', [['10:00', '10:00'], ['10:30', '10:30'], ['11:30', '11:30'], ['13:00', '13:00'], ['15:55', '15:55']]], BIASP, ...MANAGEP.filter(x => x[0] !== 'tgtR'), ...TAILP] },
  tom: { name: 'Turn of the month', blurb: 'Buy the close of the month\'s last trading day, sell the close of the new month\'s 3rd: a long-documented calendar effect. Held across days.',
    params: [['entryDay', 'Enter at the close of (trading days from the month end)', 'sel', [[-1, 'The last day'], [-2, 'The 2nd to last'], [-3, 'The 3rd to last']]],
      ['exitDay', 'Out at the close of the new month\'s day', 'num', 1], ['trend', 'Only when above', 'sel', [['none', 'No filter'], ['sma50', 'The 50-day average'], ['sma200', 'The 200-day average']]],
      ['stopAtr', 'Stop (× the daily average range, 0 = none)', 'num', 0.5], ['side', 'Side', 'sel', [['long', 'Long'], ['short', 'Short']]], ['days', 'Days to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  vwapband: { name: 'VWAP 2σ band fade', blurb: 'Regular hours: a close outside VWAP ± 2 standard deviations, then one back inside: fade it to the VWAP. No trades once the day\'s range reaches its average (trend day).',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min'], [15, '15 min']]], ['k', 'Bands (standard deviations)', 'num', 0.25], ['trendMult', 'Stop trading once the day\'s range is (× the daily average, 0 = off)', 'num', 0.1],
      ['minRR', 'Skip if the VWAP is under (R)', 'num', 0.25], ['lastEntry', 'Last entry (ET)', 'text'], ['maxDay', 'Max trades per session', 'num', 1], BIASP, ['bufTicks', 'Stop buffer (ticks)', 'num', 1],
      ...MANAGEP.filter(x => x[0] !== 'tgtR'), ...TAILP] },
  rsi2: { name: 'RSI(2) pullback with the trend (hourly)', blurb: 'Hourly: above the 200-hour EMA and the 2-period RSI under 10: buy; out when it crosses 70 or after 10 hours. Larry Connors\' classic.',
    params: [['trendLen', 'Trend EMA (hours)', 'num', 10], ['lo', 'Enter when the RSI(2) is under', 'num', 1], ['hi', 'Out when it crosses', 'num', 1], ['maxHold', 'Out after (hours)', 'num', 1],
      ['stopMult', 'Stop (× the average hourly range, 0 = none)', 'num', 0.5], ['flatEod', 'Flat at 15:55 each day', 'bool'], ['side', 'Side', 'sel', SIDE], ['days', 'Sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
  fractal: { name: 'Fractal (TradeWellX): pivot cross, level fade, wave count', blurb: 'The Fractal page\'s tools as rules on NQ / ES: a close across the pivot (new wave) or the purple vector, a fade of the outlier cluster levels, or a fade of a wave longer than the Option Analyzer odds allow. Targets: cluster level, previous wave high / low, previous session open / high / low, gamma wall or the pivot. Filters: Macro Trend, StdDev squeeze, daily wave, intraday MM option volume, Option Analyzer odds. Levels use only waves finished before the candle (no look-ahead); MM data and walls only exist on the days recorded or rebuilt.',
    params: [['entry', 'Entry', 'sel', [['cross', 'Pivot cross: go with the new wave'], ['vector', 'Vector cross: close across the purple vector, go with it'], ['smfade', 'Spread Monster fade: the day\'s daily-level wall / outlier touched and rejected'], ['late', 'Late counter-wave: a wave against the higher timeframe runs N candles, then turns'], ['fade', 'Level fade: outlier cluster level touched and rejected'], ['count', 'Wave count: wave longer than the odds allow, then a turn']]],
      ['tf', 'Candles', 'sel', [[15, '15 min'], [5, '5 min'], [30, '30 min'], [60, '60 min']]], ['wave', 'Wave (bars)', 'num', 1], ['stats', 'Levels from the last (sessions)', 'num', 1],
      ['hours', 'Hours', 'sel', [['all', 'Overnight + NYSE'], ['rth', 'NYSE session only']]],
      ['stopP', 'Cross: stop at the other side\'s level', 'sel', [[0.5, '50%'], [0.8, '80%'], [0.2, '20%'], [0.1, '10%']]], ['tgtP', 'Cross: target this side\'s level', 'sel', [[0.5, '50%'], [0.8, '80%'], [0.2, '20%'], [0.1, '10%'], [0.05, '5%']]],
      ['fadeP', 'Fade: level to fade', 'sel', [[0.1, '10%'], [0.2, '20%'], [0.05, '5% (red 2%)']]], ['oddsMax', 'Count: fade when the odds of a wave this long are under', 'num', 0.05],
      ['tgt', 'Target', 'sel', [['level', 'Cluster level (cross, vector) / the pivot (fade, count)'], ['prev', 'The previous actual wave high (long) / low (short)'],
        ['day', 'The previous session\'s open / high / low (nearest beyond)'], ['wall', 'Gamma wall: call wall (long) / put wall (short), MM data'], ['pivot', 'The pivot'],
        ['r', 'R multiple (Target R below, 0 = hold to the stop or 15:55)']]],
      ['mm', 'Intraday MM option volume', 'sel', [['none', 'Any'], ['calls', 'Calls at/above vs below agree'], ['puts', 'Puts at/above vs below agree (bearish divergence for shorts)'], ['both', 'Calls and puts agree']]],
      ['oaMin', 'Option Analyzer: cross / vector needs this share of past waves (0 = off)', 'num', 0.05], ['oaBars', '... to have run at least (candles)', 'num', 1],
      ['macro', 'Macro Trend', 'sel', [['with', 'With it'], ['none', 'Any'], ['against', 'Against it']]], ['sqz', 'StdDev squeeze', 'sel', [['any', 'Any'], ['after', 'Only after a squeeze'], ['no', 'Not while squeezed']]],
      ['daily', 'Daily wave (Wave 1)', 'sel', [['none', 'Any'], ['with', 'With it (H = longs, L = shorts)']]],
      ['smLevel', 'Spread Monster: level to fade', 'sel', [['wall', 'Wall (MaxAvg, ~25% odds)'], ['max', '1% outlier (Max)'], ['median', '50% median']]],
      ['smTgt', 'Spread Monster: target', 'sel', [['median', '50% median on that side (or the previous close)'], ['close', 'The previous close'], ['pivot', 'The intraday pivot']]],
      ['smMatch', 'Spread Monster: matching', 'sel', [['hl', 'HL state'], ['weekday', 'Weekday']]], ['smDays', 'Spread Monster: daily bars (aggregations)', 'num', 10],
      ['lateN', 'Late counter-wave: wave length (candles)', 'num', 1], ['lateStopP', 'Late counter-wave: stop at the level', 'sel', [[0.1, '10%'], [0.2, '20%'], [0.05, '5% / 2%']]],
      ['oaExit', 'Option Analyzer exit (cross, vector, late): out when fewer than this share of past waves ran longer (0 = off)', 'num', 0.05],
      ['htf', 'Higher-timeframe bias', 'sel', [['none', 'None'], ['60', '60-minute fractal'], ['240', '4-hour fractal'], ['D', 'Daily fractal']]],
      ['htfWave', 'Higher-timeframe wave (bars)', 'num', 1],
      ['htfRule', 'Higher-timeframe rule', 'sel', [['side', 'Side: longs above its pivot (H), shorts below (L)'], ['dir', 'Direction of its vector (up / down)'], ['vector', 'Close vs its purple vector'], ['macro', 'Its Macro Trend']]],
      ['lastEntry', 'Last entry (ET)', 'text'], ['maxDay', 'Max trades per session', 'num', 1], ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ...MANAGEP, ...TAILP] },
  squeeze: { name: 'Squeeze breakout', blurb: 'Bollinger Bands inside the Keltner Channels for 6+ candles (low volatility), then the first close outside the bands: go with it.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min'], [15, '15 min']]], ['kc', 'Keltner width (× the average range)', 'num', 0.25], ['minBars', 'Squeezed at least (candles)', 'num', 1],
      ['wait', 'Breakout within (candles)', 'num', 1], ['stop', 'Stop', 'sel', [['mid', 'The 20-candle average'], ['opp', 'The other band']]], ['hours', 'Hours', 'sel', [['rth', 'NYSE session only'], ['all', 'Overnight + NYSE']]],
      ['lastEntry', 'Last entry (ET)', 'text'], ['maxDay', 'Max trades per session', 'num', 1], BIASP, ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ...MANAGEP, ...TAILP] },
  volclimax: { name: 'Volume climax reversal', blurb: 'A candle with 3× the average volume making the session\'s high (low) with a long wick: fade it, target the VWAP.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min'], [15, '15 min']]], ['volMult', 'Volume at least (× the last 20 candles)', 'num', 0.5], ['wickPct', 'Wick at least (% of the candle)', 'num', 5],
      ['tgt', 'Target', 'sel', [['vwap', 'The session VWAP'], ['r', 'Fixed R']]], ['minRR', 'VWAP target: skip if under (R)', 'num', 0.25], ['hours', 'Hours', 'sel', [['rth', 'NYSE session only'], ['all', 'Overnight + NYSE']]],
      ['lastEntry', 'Last entry (ET)', 'text'], ['maxDay', 'Max trades per session', 'num', 1], BIASP, ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ...MANAGEP, ...TAILP] },
  pwsweep: { name: 'Prior-week high / low sweep', blurb: 'The weekly version of the overnight sweep: price runs past last week\'s high (low) and closes back inside: fade it.',
    params: [['tf', 'Candles', 'sel', [[5, '5 min'], [1, '1 min']]], ['lastEntry', 'Last entry (ET)', 'text'], ['maxAbove', 'Cancel after (closes outside)', 'num', 1], ['minDepth', 'Sweep at least (points)', 'num', 1],
      ['fresh', 'Skip a level already taken this week', 'bool'], ['tgt', 'Target', 'sel', [['vwap', 'VWAP'], ['mid', 'Middle of last week\'s range'], ['other', 'Other side of last week\'s range'], ['r', 'Fixed R']]],
      ['tgtR', 'Fixed R target', 'num', 0.5], ['minRR', 'Skip if the target is under (R)', 'num', 0.25], ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ...TAILP] },
  trendday: { name: 'Trend-day hold', blurb: 'A first hour of 0.6× the daily average range closing at its top (bottom) 20%: go with it at 10:30 and hold to the close.',
    params: [['rangeMult', 'First hour at least (× the daily average range)', 'num', 0.1], ['edgePct', 'Closing in its top / bottom (%)', 'num', 5],
      ['stop', 'Stop', 'sel', [['mid', 'The first hour\'s middle'], ['opp', 'Its other side']]], BIASP, ['bufTicks', 'Stop buffer (ticks)', 'num', 1], ...MANAGEP, ...TAILP] },
  wt: { name: 'WaveTrend dots', blurb: 'WaveTrend [Krypt]: in at the open after a buy dot, out at the open after a sell dot (and the mirror for shorts).',
    params: [['tf', 'Candles', 'sel', [['1D', 'Daily'], ['60', '1 hour'], ['15', '15 min'], ['5', '5 min']]], ['side', 'Side', 'sel', SIDE], ['stopAtr', 'Stop (× average range, 0 = none)', 'num', 0.25],
      ['flatEod', 'Intraday: flat at 15:55', 'bool'], ['days', 'Candles / sessions to test', 'num', 10], ['tickers', 'Contracts', 'tick']] },
};
const STAGES = { idea: 'Idea', study: 'Study', validated: 'Validated', paper: 'Paper', live: 'Live', retired: 'Retired' };

/* ---------- boot & routing ---------- */
async function boot() {
  try { await Auth.handleCallback(); } catch (e) { const el = $('#login-err'); el.textContent = e.message; el.hidden = false; }
  if (!(await Auth.token())) { $('#login').hidden = false; $('#signin').onclick = () => Auth.login(); return; }
  $('#app').hidden = false; $('#who-email').textContent = Auth.email();
  $('#signout').onclick = () => Auth.logout();
  try { S.me = await api('/me'); } catch (e) { toast(e.message); }
  feedPill();
  window.addEventListener('hashchange', route);
  route();
}
function feedPill() {
  const p = $('#feed-pill'), d = S.me && S.me.databento;
  p.className = 'pill ' + (d && d.connected ? 'ok' : 'warn');
  p.textContent = d && d.connected ? `Databento ••${d.last4}` : 'Databento not connected';
}
function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; if (S.rpTimer) { clearInterval(S.rpTimer); S.rpTimer = null; } }
function route() {
  clearTimers();
  if (S.ch) { S.ch.destroy(); S.ch = null; }
  if (S.heat) { S.heat.destroy(); S.heat = null; }
  const h = (location.hash || '#running').slice(1), [page, arg] = h.split('/');
  S.page = page;
  document.querySelectorAll('.nav a').forEach(a => a.classList.toggle('on', a.dataset.r === (page === 'strategy' ? 'research' : page)));
  const v = { running: vRunning, chart: vChart, research: vResearch, strategy: () => vStrategy(arg), data: vData, levels: vLevels, fractal: vFractal }[page] || vRunning;
  v().catch(e => { $('#view').innerHTML = `<div class="panel"><div class="empty loss">${esc(e.message)}</div></div>`; });
}
const view = html => { $('#view').innerHTML = html; };
const head = (title, sub, right = '') => `<div class="page-head"><div><h1>${title}</h1>${sub ? `<div class="dim" style="margin-top:4px">${sub}</div>` : ''}</div><div class="grow"></div>${right}</div>`;
const cellName = c => c ? c.replace('|', ' · ') : '—';


/* ---------- Levels: walls (density) and cycles (AMW) ---------- */
S.lv = { tab: 'walls', match: 'none', minLen: 3, tol: '', sdLen: 10, macroLen: 50, sym: 'NQ', day: '', lookback: 250, ref: 'open', weekday: false, scale: 'none', peak: 50, inner: 75, true: 90, over: 97.5, tf: '60', length: 20, kind: 'ema', days: 50, show: 300 };
async function vLevels() {
  const L = S.lv;
  const f = (id, lab, inner) => `<label class="f">${lab}${inner}</label>`;
  const sel = (id, v, opts) => `<select id="${id}">${opts.map(([k, l]) => `<option value="${k}" ${String(v) === String(k) ? 'selected' : ''}>${l}</option>`).join('')}</select>`;
  const ctrl = L.tab === 'conf' ? '' : L.tab === 'walls' ? `
      ${f('', 'Day', `<input type="date" id="lv-day" value="${esc(L.day)}">`)}
      ${f('', 'Sessions to load', sel('lv-lookback', L.lookback, [[100, '100'], [200, '200'], [250, '250'], [500, '500']]))}
      ${f('', 'Moves measured from', sel('lv-ref', L.ref, [['open', 'The 09:30 open'], ['close', 'Yesterday\'s 16:00 close']]))}
      ${f('', 'Scaling', sel('lv-scale', L.scale, [['none', 'Raw %'], ['vol', 'Adjusted to today\'s volatility']]))}
      ${f('', 'Matching', sel('lv-match', L.match || 'none', [['none', 'All sessions'], ['weekday', 'Same weekday'], ['hl', 'HL matching (yesterday\'s pattern)']]))}
      ${f('', 'Peak / inner / true / overflow (percentiles)', `<span style="display:flex;gap:4px">${['peak', 'inner', 'true', 'over'].map(k => `<input type="number" id="lv-${k}" value="${L[k]}" step="0.5" min="1" max="99.9" style="width:64px">`).join('')}</span>`)}`
    : `
      ${f('', 'Candles', sel('lv-tf', L.tf, [['5', '5 min'], ['15', '15 min'], ['60', '1 hour'], ['1D', 'Daily']]))}
      ${f('', 'Sessions to load', `<input type="number" id="lv-days" value="${L.days}" min="5" max="2000" style="width:90px">`)}
      ${f('', 'Vector', `<span style="display:flex;gap:4px">${sel('lv-kind', L.kind, [['ema', 'EMA'], ['sma', 'SMA']])}<input type="number" id="lv-length" value="${L.length}" min="2" max="400" style="width:70px"></span>`)}
      ${f('', 'Candles shown', `<input type="number" id="lv-show" value="${L.show}" min="20" max="3000" style="width:90px">`)}
      ${f('', 'Ignore cycles under (candles)', `<input type="number" id="lv-minlen" value="${L.minLen}" min="1" max="50" style="width:70px">`)}
      ${f('', 'Volatility over (candles)', `<input type="number" id="lv-sdlen" value="${L.sdLen}" min="3" max="100" style="width:70px">`)}
      ${f('', 'Macro trend EMA', `<input type="number" id="lv-macro" value="${L.macroLen}" min="5" max="400" style="width:70px">`)}`;
  const ctrl2 = L.tab === 'conf' ? `${f('', 'Stacked within (points, empty = 15 NQ / 2 ES)', `<input type="number" id="lv-tol" value="${esc(String(L.tol))}" step="0.25" min="0.25" style="width:110px">`)}` : '';
  view(`${head('Levels', 'Where price usually ranges, reverts and extends — measured from the stored history.')}
    <div class="seg" style="margin-bottom:12px"><button data-lvtab="walls" aria-pressed="${L.tab === 'walls'}">Walls &amp; Peak Zone</button><button data-lvtab="cycles" aria-pressed="${L.tab === 'cycles'}">Cycles, AMW &amp; volatility</button><button data-lvtab="conf" aria-pressed="${L.tab === 'conf'}">Confluence</button></div>
    <section class="panel"><div class="bd" style="display:flex;flex-wrap:wrap;gap:12px;align-items:end">
      <div class="seg">${['NQ', 'ES'].map(s => `<button data-lvsym="${s}" aria-pressed="${L.sym === s}">${s}</button>`).join('')}</div>${ctrl}${ctrl2}
      <button class="btn primary" data-act="lv-go">Show</button></div></section>
    <div id="lv-out" style="margin-top:12px"><div class="empty">Loading…</div></div>`);
  return lvLoad();
}
function lvRead() {
  const L = S.lv, v = id => { const e = $('#' + id); return e ? e.value : null; };
  if (L.tab === 'walls') { L.day = v('lv-day') || ''; L.lookback = +v('lv-lookback'); L.ref = v('lv-ref'); L.scale = v('lv-scale'); L.match = v('lv-match'); ['peak', 'inner', 'true', 'over'].forEach(k => L[k] = +v('lv-' + k)); }
  else if (L.tab === 'cycles') { L.tf = v('lv-tf'); L.days = +v('lv-days'); L.kind = v('lv-kind'); L.length = +v('lv-length'); L.show = +v('lv-show'); L.sdLen = +v('lv-sdlen'); L.macroLen = +v('lv-macro'); L.minLen = +v('lv-minlen'); }
  else { L.tol = v('lv-tol') || ''; }
}
async function lvLoad() {
  const L = S.lv, out = $('#lv-out');
  try {
    if (L.tab === 'walls') {
      const q = new URLSearchParams({ sym: L.sym, lookback: L.lookback, ref: L.ref, scale: L.scale, match: L.match || 'none', peak: L.peak, inner: L.inner, true: L.true, over: L.over, ...(L.day ? { day: L.day } : {}) });
      const w = await api('/walls?' + q);
      if (!L.day) { L.day = w.day; const e = $('#lv-day'); if (e) e.value = w.day; }
      out.innerHTML = wallsHtml(w); drawWalls($('#lv-canvas'), w); drawFan($('#lv-fan'), w); drawOdds($('#lv-odds'), w);
    } else if (L.tab === 'conf') {
      const c = await api('/confluence?' + new URLSearchParams({ sym: L.sym, ...(L.tol ? { tol: L.tol } : {}) }));
      out.innerHTML = confHtml(c); drawConf($('#lv-canvas'), c);
    } else {
      const c = await api('/cycles?' + new URLSearchParams({ sym: L.sym, tf: L.tf, days: L.days, kind: L.kind, length: L.length, show: L.show, sdLen: L.sdLen, macroLen: L.macroLen, minLen: L.minLen }));
      out.innerHTML = cyclesHtml(c); drawCycles($('#lv-canvas'), c);
    }
  } catch (e) { out.innerHTML = `<div class="panel"><div class="empty loss">${esc(e.message)}</div></div>`; }
}
const WCOL = { peak: '#4CC38A', inner: 'rgba(220,226,235,0.55)', true: '#E8EDF3', over: '#F2994A' };
function wallsHtml(w) {
  const st = w.stats, by = k => w.levels.filter(x => x.k === k);
  return `<section class="panel"><div class="hd"><h2>${esc(w.sym)} · ${esc(w.day)}</h2><span class="dim" style="font-size:13px">Reference ${fmt(w.ref)} (${esc(w.refKind)}) · ${w.sample} sessions (${esc(w.from)} → ${esc(w.to)})${w.atrPct ? ` · daily average range ${w.atrPct.toFixed(2)}%` : ''}</span></div>
    <div class="bd"><canvas id="lv-canvas" style="width:100%;height:520px;display:block"></canvas>
    <div class="note" style="margin-top:6px">Grey = where the 5-minute closes of past sessions were, by time of day (darker = more often). Curves = how far the running high / low had reached by that time. Lines = the levels for the whole day. Blue = this day's own path.</div></div></section>
    <div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-top:12px">
      <section class="panel"><div class="hd"><h2>Volatility</h2><span class="dim" style="font-size:13px">from the reference: the moves up and down, and the share of sessions that got that far</span></div><div class="bd"><canvas id="lv-fan" style="width:100%;height:420px;display:block"></canvas></div></section>
      <section class="panel"><div class="hd"><h2>Odds</h2><span class="dim" style="font-size:13px">the share of sessions whose high (green) / low (red) reached each level</span></div><div class="bd"><canvas id="lv-odds" style="width:100%;height:420px;display:block"></canvas></div></section>
    </div>
    <div class="grid2" style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-top:12px">
    <section class="panel"><div class="hd"><h2>Levels</h2></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Level</th><th class="r">Up</th><th class="r">% </th><th class="r">Beyond</th><th class="r">Down</th><th class="r">%</th><th class="r">Beyond</th></tr></thead><tbody>
      ${['over', 'true', 'inner', 'peak'].map(k => { const [u, d] = [by(k).find(x => x.side === 'up'), by(k).find(x => x.side === 'down')]; return `<tr><td><span style="color:${WCOL[k]};font-weight:600">${esc(u.label)}</span></td><td class="r mono">${fmt(u.price)}</td><td class="r">+${u.pct.toFixed(2)}%</td><td class="r dim">${u.beyond}/${u.of}</td><td class="r mono">${fmt(d.price)}</td><td class="r">−${d.pct.toFixed(2)}%</td><td class="r dim">${d.beyond}/${d.of}</td></tr>`; }).join('')}
    </tbody></table></div><div class="bd note">"Beyond" = how many of the sessions went past that level (e.g. 3/250 = an outlier day).</div></section>
    <section class="panel"><div class="hd"><h2>How the sessions behaved</h2></div><div class="bd" style="display:flex;flex-direction:column;gap:6px">
      <div><b>${st.medianTagged}%</b> touched at least one Peak (the median move up or down)</div>
      <div><b>${st.bothPeaks}%</b> touched both Peaks</div>
      <div><b>${st.anyWall}%</b> reached a True wall · <b>${st.bothWalls}%</b> reached both</div>
      <div><b>${st.overflow}%</b> went past an Overflow wall (outlier days)</div>
      <table class="tbl" style="margin-top:8px"><tbody>${(st.lines || []).map(([t, k, n]) => `<tr><td>${esc(t)}</td><td class="r mono">${k} of ${n}</td><td class="r"><b>${(k / n * 100).toFixed(0)}%</b></td></tr>`).join('')}</tbody></table>
      ${w.actual ? `<div style="margin-top:6px" class="dim">This day: high ${fmt(w.actual.h)} (${sgn((w.actual.h - w.ref) / w.ref * 100, 2, '%')}), low ${fmt(w.actual.l)} (${sgn((w.actual.l - w.ref) / w.ref * 100, 2, '%')}), close ${fmt(w.actual.c)}</div>` : '<div class="dim" style="margin-top:6px">This day has no regular-hours candles yet: the levels use yesterday\'s close as the reference.</div>'}
    </div></section></div>`;
}
function canvasCtx(cv, h) {
  const r = window.devicePixelRatio || 1, W = cv.clientWidth, H = h || cv.clientHeight;
  cv.width = W * r; cv.height = H * r; const g = cv.getContext('2d'); g.scale(r, r); g.font = '11px Inter, system-ui, sans-serif';
  return [g, W, H];
}
function drawWalls(cv, w) {
  if (!cv) return;
  const [g, W, H] = canvasCtx(cv, 520), pl = 56, pr = 190, pt = 14, pb = 26, pw = W - pl - pr, ph = H - pt - pb;
  const n = w.times.length, span = w.span, X = i => pl + (i + 0.5) / n * pw, Y = v => pt + (span - v) / (2 * span) * ph;
  g.fillStyle = '#0d1117'; g.fillRect(0, 0, W, H);
  const mx = Math.max(1, ...w.grid.flat()), bh = ph / w.bins;
  w.grid.forEach((col, i) => col.forEach((c, b) => { if (!c) return; g.fillStyle = `rgba(200,208,220,${Math.min(0.85, 0.08 + c / mx * 0.9)})`; g.fillRect(pl + i / n * pw, pt + (w.bins - 1 - b) * bh, pw / n + 0.5, bh + 0.5); }));
  g.strokeStyle = 'rgba(255,255,255,0.25)'; g.beginPath(); g.moveTo(pl, Y(0)); g.lineTo(pl + pw, Y(0)); g.stroke();
  Object.entries(w.cone).forEach(([k, c]) => [['up', 1], ['down', -1]].forEach(([s, sg]) => {
    g.strokeStyle = WCOL[k]; g.lineWidth = 1.5; g.beginPath(); let on = false;
    c[s].forEach((v, i) => { if (v == null) return; const y = Y(sg * v); on ? g.lineTo(X(i), y) : g.moveTo(X(i), y); on = true; }); g.stroke(); g.lineWidth = 1;
  }));
  w.levels.forEach(x => { const v = x.side === 'up' ? x.pct : -x.pct, y = Math.round(Y(v)) + 0.5; g.strokeStyle = WCOL[x.k]; g.setLineDash(x.k === 'peak' ? [] : [5, 4]); g.beginPath(); g.moveTo(pl, y); g.lineTo(pl + pw, y); g.stroke(); g.setLineDash([]);
    g.fillStyle = WCOL[x.k]; g.textAlign = 'left'; g.fillText(`${x.label} ${fmt(x.price)} (${x.beyond}/${x.of})`, pl + pw + 6, y + 4); });
  if (w.path && w.path.length) { g.strokeStyle = '#4FA3FF'; g.lineWidth = 2; g.beginPath(); w.path.forEach(([i, v], j) => j ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.stroke(); g.lineWidth = 1; }
  g.fillStyle = 'rgba(170,178,190,0.9)'; g.textAlign = 'right';
  for (let v = -Math.floor(span * 2) / 2; v <= span; v += 0.5) { if (Math.abs(v) > span) continue; g.fillText(`${v > 0 ? '+' : ''}${v.toFixed(1)}%`, pl - 6, Y(v) + 4); }
  g.textAlign = 'center'; w.times.forEach((t, i) => { if (t.endsWith(':00') || t === '09:30') g.fillText(t, X(i), H - 8); });
}
function drawFan(cv, w) {
  if (!cv || !w.fan) return;
  const [g, W, H] = canvasCtx(cv, 420), pl = 70, pr = 170, pt = 16, pb = 16, top = Math.max(w.fan.up[0].pct, w.fan.down[0].pct) * 1.08;
  const Y = v => pt + (top - v) / (2 * top) * (H - pt - pb), x0 = pl, x1 = W - pr;
  g.fillStyle = '#0d1117'; g.fillRect(0, 0, W, H);
  g.strokeStyle = 'rgba(255,255,255,0.3)'; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(x0, Y(0)); g.lineTo(x1, Y(0)); g.stroke(); g.setLineDash([]);
  g.fillStyle = 'rgba(220,226,235,0.9)'; g.textAlign = 'right'; g.fillText('ref ' + fmt(w.ref), x0 - 6, Y(0) + 4);
  [['up', 1, '#3FB978'], ['down', -1, '#F25F5C']].forEach(([s, sg, col]) => w.fan[s].forEach(f => {
    const y = Y(sg * f.pct); g.strokeStyle = col; g.beginPath(); g.moveTo(x0, Y(0)); g.lineTo(x1 - 120, y); g.lineTo(x1, y); g.stroke();
    g.fillStyle = col; g.textAlign = 'left'; g.fillText(`${f.label} ${sg > 0 ? '+' : '−'}${f.pct.toFixed(2)}%  ${fmt(f.price)}  (${f.odds}% got there)`, x1 + 6 - 120 > 0 ? x1 - 118 : x1 + 6, y - 4);
  }));
  const wl = w.fan.up[1], ws = w.fan.down[1];
  g.fillStyle = 'rgba(255,255,255,0.05)'; g.fillRect(x0, Y(wl.pct), x1 - x0, Y(-ws.pct) - Y(wl.pct));
  g.fillStyle = 'rgba(220,226,235,0.7)'; g.textAlign = 'left'; g.fillText('between the walls: ~75% of each side', x1 + 6, Y(0) + 4);
}
function drawOdds(cv, w) {
  if (!cv || !w.odds) return;
  const [g, W, H] = canvasCtx(cv, 420), pl = 70, pr = 60, pt = 16, pb = 26, o = w.odds, top = o.pct[o.pct.length - 1] || 1;
  const Y = v => pt + (top - v) / (2 * top) * (H - pt - pb), X = q => pl + q / 100 * (W - pl - pr);
  g.fillStyle = '#0d1117'; g.fillRect(0, 0, W, H);
  [0, 25, 50, 75, 100].forEach(q => { g.strokeStyle = 'rgba(255,255,255,0.08)'; g.beginPath(); g.moveTo(X(q), pt); g.lineTo(X(q), H - pb); g.stroke(); g.fillStyle = 'rgba(170,178,190,0.9)'; g.textAlign = 'center'; g.fillText(q + '%', X(q), H - 8); });
  [['up', 1, '#3FB978'], ['down', -1, '#F25F5C']].forEach(([s, sg, col]) => { g.strokeStyle = col; g.lineWidth = 2; g.beginPath(); o[s].forEach((q, k) => { const x = X(q), y = Y(sg * o.pct[k]); k ? g.lineTo(x, y) : g.moveTo(x, y); }); g.stroke(); g.lineWidth = 1; });
  g.textAlign = 'right'; g.fillStyle = 'rgba(170,178,190,0.9)';
  for (let k = 0; k <= 4; k++) { const v = top * k / 4; g.fillText(`${fmt(w.ref * (1 + v / 100))}`, pl - 6, Y(v) + 4); if (k) g.fillText(`${fmt(w.ref * (1 - v / 100))}`, pl - 6, Y(-v) + 4); }
  const yM = Y(0); g.strokeStyle = 'rgba(255,255,255,0.3)'; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(pl, yM); g.lineTo(W - pr, yM); g.stroke(); g.setLineDash([]);
  [50, 25].forEach(q => { g.strokeStyle = 'rgba(231,195,90,0.5)'; g.beginPath(); g.moveTo(X(q), pt); g.lineTo(X(q), H - pb); g.stroke(); });
}
function cyclesHtml(c) {
  const cur = c.current, cy = c.cycles, row = (k, lab) => cy[k] ? `<tr><td>${lab}</td><td class="r">${cy[k].n}</td><td class="r">${cy[k].avgPct.toFixed(2)}%</td><td class="r">${cy[k].avgBars.toFixed(1)}</td><td class="r">${cy[k].longest}</td><td class="r">${cy[k].avgToPeak.toFixed(1)}</td></tr>` : '';
  return `<section class="panel"><div class="hd"><h2>${esc(c.sym)} · ${c.params.tf === '1D' ? 'daily' : c.params.tf + '-minute'} · vector ${c.params.kind.toUpperCase()} ${c.params.length}</h2>
      ${cur ? `<span class="dim" style="font-size:13px">Current ${cur.side > 0 ? 'up' : 'down'} cycle: ${cur.barsIn} candles, from the base ${fmt(cur.base)} (${esc(cur.baseT.replace('T', ' '))}), extended ${cur.extPct.toFixed(2)}% · AMW peak ${fmt(cur.amw.peak)} (${cur.amw.pct.toFixed(2)}%, ~${cur.amw.bars} candles)</span>` : ''}</div>
    <div class="bd"><canvas id="lv-canvas" style="width:100%;height:760px;display:block"></canvas>
    <div class="note" style="margin-top:6px">Bottom panel = volatility (the standard deviation of the last ${c.params.sdLen} closes vs its usual level; under 0.1 = squeeze, 0.1-0.5 = compressed, 0.5-0.8+ = expanding, 1.6+ = aggressive), green / red = price direction over those candles, k / m = volatility turning up with an up / down move, top bar = macro trend (above / below the ${c.params.macroLen}-candle EMA). It's measured from past candles: an approximation of the Fractal's tool, not its forward-looking version. Purple = the vector. H n / L n = a finished cycle's extreme and its length in candles (effort). Blue arc = the Average Mean Wave from the current base: the average extension of past cycles, after their average time to the peak. Bands = from the base, how many past cycles reached each level (80% = most did, 5% = outliers).</div></div></section>
    <section class="panel" style="margin-top:12px"><div class="hd"><h2>Cycle statistics</h2></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Cycles</th><th class="r">Count</th><th class="r">Average extension</th><th class="r">Average length (candles)</th><th class="r">Longest</th><th class="r">Candles to the peak</th></tr></thead><tbody>${row('up', 'Up (above the vector)')}${row('down', 'Down (below the vector)')}</tbody></table></div></section>`;
}
function drawCycles(cv, c) {
  if (!cv) return;
  const [g, W, H] = canvasCtx(cv, 760), pl = 10, pr = 150, pt = 14, pb = 26 + 200, pw = W - pl - pr, ph = H - pt - pb;
  const C = c.candles, cur = c.current, n = C.length;
  const fut = cur ? Math.max(5, Math.ceil(cur.amw.fromIdx + 2 * cur.amw.bars - (n - 1)) + 3) : 5, N = n + fut;
  let lo = Math.min(...C.map(b => b[3])), hi = Math.max(...C.map(b => b[2]));
  if (cur) { cur.bands.forEach(b => { lo = Math.min(lo, b.price); hi = Math.max(hi, b.price); }); lo = Math.min(lo, cur.amw.peak, cur.base); hi = Math.max(hi, cur.amw.peak, cur.base); }
  const pad = (hi - lo) * 0.04; lo -= pad; hi += pad;
  const X = i => pl + (i + 0.5) / N * pw, Y = p => pt + (hi - p) / (hi - lo) * ph, bw = Math.max(1, pw / N * 0.7);
  g.fillStyle = '#0d1117'; g.fillRect(0, 0, W, H);
  if (cur) cur.bands.forEach(b => { const y = Y(b.price), col = cur.side > 0 ? '63,185,120' : '242,95,92'; g.fillStyle = `rgba(${col},${0.10 + (100 - b.p) / 400})`; g.fillRect(pl, y - 4, pw, 8);
    g.fillStyle = `rgb(${col})`; g.textAlign = 'left'; g.fillText(`${b.p}% @ ${fmt(b.price)}`, pl + pw + 6, y + 4); });
  C.forEach((b, i) => { const [t, o, h, l, cl] = b, up = cl >= o; g.strokeStyle = g.fillStyle = up ? '#4FA3FF' : '#F2994A'; g.beginPath(); g.moveTo(X(i), Y(h)); g.lineTo(X(i), Y(l)); g.stroke();
    g.fillRect(X(i) - bw / 2, Y(Math.max(o, cl)), bw, Math.max(1, Math.abs(Y(o) - Y(cl)))); });
  g.strokeStyle = '#B07AE0'; g.lineWidth = 1.5; g.beginPath(); c.vector.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.stroke(); g.lineWidth = 1;
  const idx = new Map(C.map((b, i) => [b[0], i]));
  c.marks.forEach(m => { const i = idx.get(m.t); if (i == null) return; g.fillStyle = 'rgba(230,235,240,0.85)'; g.textAlign = 'center'; g.fillText(m.text, X(i), m.side > 0 ? Y(m.p) - 8 : Y(m.p) + 16); });
  if (cur) {
    const a = cur.amw, i0 = a.fromIdx, T = Math.max(1, a.bars);
    g.strokeStyle = 'rgba(255,255,255,0.4)'; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X(i0), Y(cur.base)); g.lineTo(pl + pw, Y(cur.base)); g.stroke(); g.setLineDash([]);
    g.fillStyle = 'rgba(255,255,255,0.7)'; g.textAlign = 'left'; g.fillText(`Base ${fmt(cur.base)}`, pl + pw + 6, Y(cur.base) + 4);
    g.strokeStyle = '#3D8BFF'; g.lineWidth = 2.5; g.beginPath();
    for (let s = 0; s <= 2 * T; s += Math.max(0.25, T / 40)) { const u = (s - T) / T, p = cur.base + (a.peak - cur.base) * (1 - u * u); s ? g.lineTo(X(i0 + s), Y(p)) : g.moveTo(X(i0 + s), Y(p)); }
    g.stroke(); g.lineWidth = 1; g.fillStyle = '#3D8BFF'; g.textAlign = 'left'; g.fillText(`AMW ${fmt(a.peak)}`, pl + pw + 6, Y(a.peak) + 4);
    g.strokeStyle = 'rgba(255,255,255,0.2)'; g.beginPath(); g.moveTo(X(n - 0.5), pt); g.lineTo(X(n - 0.5), pt + ph); g.stroke();
  }
  // volatility panel
  const sd = c.sd || [], y0 = pt + ph + 18, sh = 170, smax = Math.max(2.8, ...sd.map(x => x[0] || 0)), SY = v => y0 + 12 + (1 - v / smax) * (sh - 24);
  g.fillStyle = '#10151c'; g.fillRect(pl, y0, pw, sh);
  sd.forEach((x, i) => { g.fillStyle = x[2] > 0 ? '#2E9E5B' : '#C2413B'; g.fillRect(X(i) - pw / N / 2, y0, pw / N + 0.5, 6); });
  [[0.1, 'Squeeze 0.1', '#E7C35A'], [0.5, '0.5', 'rgba(255,255,255,0.35)'], [0.8, 'Expanding 0.8', 'rgba(255,255,255,0.35)'], [1.6, 'Aggressive 1.6', 'rgba(255,255,255,0.35)'], [2.6, '2.6', 'rgba(255,255,255,0.25)']].forEach(([v, lab, col]) => {
    const y = Math.round(SY(v)) + 0.5; g.strokeStyle = col; g.beginPath(); g.moveTo(pl, y); g.lineTo(pl + pw, y); g.stroke(); g.fillStyle = col; g.textAlign = 'left'; g.fillText(lab, pl + pw + 6, y + 4); });
  for (let i = 1; i < sd.length; i++) { if (sd[i][0] == null || sd[i - 1][0] == null) continue; g.strokeStyle = sd[i][1] > 0 ? '#3FB978' : sd[i][1] < 0 ? '#F25F5C' : '#9AA4B2'; g.lineWidth = 2; g.beginPath(); g.moveTo(X(i - 1), SY(sd[i - 1][0])); g.lineTo(X(i), SY(sd[i][0])); g.stroke(); g.lineWidth = 1; }
  g.font = '11px Inter, system-ui, sans-serif'; g.textAlign = 'center';
  sd.forEach((x, i) => { if (x[3]) { g.fillStyle = x[3] === 'k' ? '#3FB978' : '#F25F5C'; g.fillText(x[3], X(i), y0 + sh - 4); } });
  g.fillStyle = 'rgba(170,178,190,0.9)'; g.textAlign = 'center';
  const step = Math.max(1, Math.floor(n / 8)); for (let i = 0; i < n; i += step) g.fillText(C[i][0].slice(5, 16).replace('T', ' '), X(i), H - 8);
}
const SRC_COL = { Walls: '#E8EDF3', Session: '#E7C35A', Daily: '#E07AE0', Hourly: '#B07AE0', '15-min': '#5FD4C4', '1-min': '#4FA3FF', Imprints: '#9AA4B2' };
function confHtml(c) {
  const top = c.clusters.filter(x => x.score >= 2).sort((a, b) => b.score - a.score || Math.abs(a.dist) - Math.abs(b.dist));
  return `<section class="panel"><div class="hd"><h2>${esc(c.sym)} · now ${fmt(c.price)}</h2><span class="dim" style="font-size:13px">${esc((c.t || '').replace('T', ' '))} ET · session ${esc(c.day)} · stacked = within ${c.tol} points · ${c.levels.length} levels from ${Object.keys(SRC_COL).length} sources</span></div>
    <div class="bd" style="display:grid;grid-template-columns:minmax(0,360px) minmax(0,1fr);gap:16px">
      <canvas id="lv-canvas" style="width:100%;height:640px;display:block"></canvas>
      <div><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Zone</th><th class="r">Sources</th><th class="r">From price</th><th>Levels in it</th></tr></thead><tbody>
        ${top.length ? top.map(z => `<tr style="${z.score >= 3 ? 'background:rgba(63,185,120,0.10)' : ''}"><td class="mono">${fmt(z.lo)}${z.hi > z.lo ? '–' + fmt(z.hi) : ''}</td><td class="r"><b>${z.score}</b>${z.score >= 3 ? ' · A+' : ''}</td><td class="r ${z.dist > 0 ? 'gain' : 'loss'}">${sgn(z.dist)}</td>
          <td style="font-size:12px">${z.levels.map(x => `<span style="color:${SRC_COL[x.src] || '#ccc'}">${esc(x.label)}</span> <span class="dim">${fmt(x.price)}</span>`).join(' · ')}</td></tr>`).join('') : '<tr><td colspan="4" class="dim">No levels stacked within the tolerance.</td></tr>'}
      </tbody></table></div>
      <div class="note" style="margin-top:8px">Sources: the Walls tool (Peak / walls for today), the session levels (yesterday's high / low / close, overnight high / low), the vector + AMW peak + 80 / 50 / 20% bands on the daily (200 sessions), hourly (50), 15-minute (10) and 1-minute (3), and imprints (the last hourly cycle extremes). A zone with 3+ different sources = A+ confluence. Not included: Open States and the OMM / Moosebot levels (their method and options data aren't available).</div></div>
    </div></section>`;
}
function drawConf(cv, c) {
  if (!cv) return;
  const [g, W, H] = canvasCtx(cv, 640), pt = 12, pb = 12, ph = H - pt - pb;
  const ps = c.levels.map(x => x.price).concat([c.price]);
  let lo = Math.min(...ps), hi = Math.max(...ps); const pad = (hi - lo) * 0.03 || 1; lo -= pad; hi += pad;
  const Y = p => pt + (hi - p) / (hi - lo) * ph;
  g.fillStyle = '#0d1117'; g.fillRect(0, 0, W, H);
  c.clusters.filter(z => z.score >= 2).forEach(z => { g.fillStyle = z.score >= 3 ? 'rgba(63,185,120,0.22)' : 'rgba(231,195,90,0.12)'; const y1 = Y(z.hi) - 4, y2 = Y(z.lo) + 4; g.fillRect(0, y1, W, y2 - y1); });
  c.levels.forEach(x => { const y = Math.round(Y(x.price)) + 0.5; g.strokeStyle = SRC_COL[x.src] || '#ccc'; g.beginPath(); g.moveTo(70, y); g.lineTo(W - 4, y); g.stroke(); });
  c.clusters.forEach(z => { g.fillStyle = 'rgba(200,208,220,0.9)'; g.textAlign = 'left'; g.fillText(fmt(z.mid), 4, Y(z.mid) + 4); });
  if (c.price) { const y = Math.round(Y(c.price)) + 0.5; g.strokeStyle = '#4FA3FF'; g.lineWidth = 2; g.beginPath(); g.moveTo(60, y); g.lineTo(W, y); g.stroke(); g.lineWidth = 1; g.fillStyle = '#4FA3FF'; g.textAlign = 'right'; g.fillText('now ' + fmt(c.price), W - 4, y - 5); }
}

/* ---------- Running ---------- */
async function vRunning() {
  const r = await api('/running');
  S.run = r;
  const any = Object.values(r.coverage).some(c => c.days);
  const tile = m => m.empty ? `<div class="panel tile"><div><b>${m.sym}</b> <span class="dim">${esc(m.name)}</span></div><div class="dim">No candles yet. <a href="#data">Download them</a>.</div></div>` : `
    <div class="panel tile">
      <div style="display:flex;align-items:baseline;gap:10px"><b style="font-size:16px">${m.sym}</b><span class="dim" style="font-size:12px">${esc(m.name)} · continuous</span><div style="flex:1"></div><span class="dim" style="font-size:12px">${esc((m.t || '').replace('T', ' '))} ET</span></div>
      <div class="px"><b>${fmt(m.last)}</b><span class="${cls(m.chg)}">${sgn(m.chg)}  ${m.prevClose ? sgn(m.chg / m.prevClose * 100, 2, '%') : ''}</span></div>
      <div class="spark">${spark(m.spark)}</div>
      <div class="kv"><div><div class="k">VWAP</div><div class="v">${fmt(m.vwap)}</div></div><div><div class="k">Session range</div><div class="v">${fmt(m.low)} – ${fmt(m.high)}</div></div>
        <div><div class="k">Overnight H / L</div><div class="v">${fmt(m.onh, 0)} / ${fmt(m.onl, 0)}</div></div><div><div class="k">Condition</div><div>${esc(cellName(m.cell))}</div></div></div>
    </div>`;
  const rk = r.risk, used = rk.limit ? Math.min(100, Math.max(0, -rk.net / rk.limit * 100)) : 0;
  const rows = r.strategies;
  const fl = S.filterRun || 'All';
  const rf = r.refresh || {}, lb = r.lastBar || {};
  const rfLine = `<div class="${rf.status === 'error' ? 'job error' : 'note'}" style="margin-bottom:12px">Candles to ${Object.entries(lb).map(([s, t]) => `${s} ${t ? esc(t.replace('T', ' ')) : '—'}`).join(' · ')} ET${rf.at ? ` · last automatic update ${esc(rf.at.replace('T', ' ').slice(0, 16))} ET: ${rf.status === 'error' ? 'error: ' + esc(rf.msg || '') : esc(rf.msg || rf.status || '')}` : ' · no automatic update recorded yet'}${rf.avail ? ` · Databento has data to ${esc(rf.avail.replace('T', ' ').slice(0, 16))} UTC` : ''}</div>`;
  view(`${head('Running', 'Strategies in paper trading. Everything else stays in Research.', `<button class="btn" data-act="refresh">Update data now</button>`)}
    ${rfLine}
    <div class="grid-auto" style="margin-bottom:16px">${r.markets.map(tile).join('')}
      <div class="panel tile"><div style="display:flex;align-items:baseline"><b>Risk today</b><div style="flex:1"></div><span class="dim" style="font-size:12px">${esc(r.today)}</span></div>
        <div><div style="display:flex;font-size:13px;margin-bottom:6px"><span>Daily loss limits</span><div style="flex:1"></div><span class="mono">${money(Math.min(0, rk.net), false).replace('$0', '$0')} / ${money(rk.limit, false)}</span></div><div class="meter"><i class="${used > 75 ? 'warn' : ''}" style="width:${used}%"></i></div></div>
        <div class="kv kv3"><div><div class="k">Net today</div><div class="big ${cls(rk.net)}">${money(rk.net)}</div></div><div><div class="k">Open contracts</div><div class="big">${rk.open}</div></div><div><div class="k">Running</div><div class="big">${rows.filter(x => x.on).length}</div></div></div>
      </div>
    </div>
    <div class="row">
      <section class="panel col-main" style="display:block"><div class="hd"><h2>Strategies</h2><span class="dim" style="font-size:13px">Paper trading runs on the Databento candles, refreshed every 5 minutes, overnight included.</span></div>
        ${rows.length ? `<div class="tbl-wrap"><table class="tbl" style="min-width:820px"><thead><tr><th>Strategy</th><th>Mode</th><th>Position</th><th class="r">Today</th><th class="r">Trades</th><th class="r">Net since start</th><th>Paper vs test</th><th>Status</th></tr></thead><tbody>
          ${rows.map(s => `<tr><td><a class="name" href="#strategy/${s.id}">${esc(s.name)}</a><div class="sub">${esc(s.meta)}</div></td>
            <td><span class="stage st-paper">${s.mode}</span></td>
            <td class="mono">${s.pos.length ? s.pos.map(p => `${p.side === 'long' ? 'Long' : 'Short'} ${p.s} @ ${fmt(p.e)}`).join('<br>') : 'Flat'}</td>
            <td class="r ${cls(s.today)}">${money(s.today)}</td><td class="r">${s.tradesToday} / ${s.n}</td><td class="r ${cls(s.net)}">${money(s.net)}</td>
            <td>${zbar(s.z)}</td><td style="color:${s.kill ? 'var(--sellT)' : 'var(--muted)'}">${s.kill ? 'Stopped: ' + esc(s.kill) : s.on ? 'Running' : 'Stopped'}</td></tr>`).join('')}
        </tbody></table></div>` : `<div class="empty">Nothing is paper trading yet. Validate a strategy in <a href="#research">Research</a>, then start paper trading from its page.</div>`}
      </section>
      <aside class="panel col-side" style="display:block"><div class="hd"><h2>Today's fills</h2><div class="grow"></div><a href="#chart" style="font-size:13px">Open chart</a></div>
        ${r.fills.length ? r.fills.map(f => `<div class="fill"><span class="t">${esc(f.t)}</span><span class="s ${f.side === 'BUY' ? 'gain' : 'loss'}">${f.side}</span><span class="w">${esc(f.what)}</span><span class="mono">${fmt(f.px)}</span></div>`).join('') : '<div class="empty">No fills today.</div>'}
        <div class="bd note" style="border-top:1px solid var(--line)">${any ? `Candles stored: ${Object.entries(r.coverage).map(([s, c]) => `${s} ${c.from || '—'} → ${(c.to || '').replace('T', ' ')}`).join(' · ')}` : 'No market data yet: <a href="#data">connect Databento and download the candles</a>.'}</div>
      </aside>
    </div>`);
  S.timers.push(setInterval(() => { if (S.page === 'running' && !document.hidden) vRunning().catch(() => {}); }, 60000));
}
function spark(xs) { if (!xs || !xs.length) return ''; const lo = Math.min(...xs), hi = Math.max(...xs); return xs.map((v, i) => `<i style="height:${6 + (v - lo) / (hi - lo || 1) * 38}px;${i === xs.length - 1 ? 'background:#E7EAF0' : ''}"></i>`).join(''); }
function zbar(z) {
  if (z == null) return '<span class="dim">—</span>';
  const left = Math.max(0, Math.min(100, 50 + z * 15));
  return `<div style="display:flex;align-items:center;gap:8px"><div style="position:relative;width:90px;height:6px;background:#1F2632;border-radius:3px"><div style="position:absolute;left:50%;top:-3px;width:1px;height:12px;background:#4B5566"></div><div style="position:absolute;left:calc(${left}% - 5px);top:-2px;width:10px;height:10px;border-radius:5px;background:${z < -2 ? 'var(--sellT)' : 'var(--buyT)'}"></div></div><span class="mono dim" style="font-size:12px">z ${sgn(z, 1)}</span></div>`;
}

/* ---------- Fractal: the TradeWellX Wave [N] rebuild (backend/fractal.py) ---------- */
S.vp = { fwd: 1, match: 'hl', wave: 1, days: 200, asof: '', spag: true, bg: true, x: true, view: 'density' };
S.fx = { sym: 'QQQ', wave: 10, days: 10, stats: 10, agg: 15, rth: false, lv: 'steps', fixed: true, basis: 'pivot', cold: false, close16: true, macro: 'ema30' };
async function vFractal() {
  const f = S.fx, alp = await api('/alpaca');
  const ctl = `<div class="form" style="align-items:end">
      <label class="f">Symbol<input type="text" id="fx-sym" value="${esc(f.sym)}" style="width:90px;text-transform:uppercase"></label>
      <label class="f">Wave<input type="number" id="fx-wave" min="2" max="100" value="${f.wave}" style="width:70px"></label>
      <label class="f">Days to load<input type="number" id="fx-days" min="1" max="60" value="${f.days}" style="width:70px"></label>
      <label class="f" title="History the levels and wave odds are drawn from (TradeWellX uses waves from before the window it shows)">Stats days<input type="number" id="fx-stats" min="1" max="120" value="${f.stats}" style="width:70px"></label>
      <label class="f">Aggregation (min)<select id="fx-agg">${[1, 5, 15, 30, 60].map(n => `<option ${n === f.agg ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
      <label class="check"><input type="checkbox" id="fx-rth" ${f.rth ? 'checked' : ''}> NQ / ES: regular hours only</label>
      <label class="check" title="TradeWellX loads no bars before the window: its MA and first pivot start from the window's own bars, which changes the first waves and the cluster marks"><input type="checkbox" id="fx-cold" ${f.cold ? 'checked' : ''}> Start cold, like TradeWellX</label>
      <label class="check" title="TradeWellX ends the window with one 16:00 bar: the last session's closing print (10 days at 15 min = 261 bars)"><input type="checkbox" id="fx-c16" ${f.close16 !== false ? 'checked' : ''}> Stocks: add the 16:00 closing bar</label>
      <label class="f">Macro Trend<select id="fx-macro">${[['ema30', 'Close vs EMA 30'], ['ema40', 'Close vs EMA 40'], ['sma50', 'Close vs 50-bar average']].map(([v, t]) => `<option value="${v}" ${f.macro === v ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
      <button class="btn primary" data-act="fx-load">Calculate</button>
      <label class="f" style="flex-direction:row;align-items:center;gap:6px" title="Every 5 minutes while the market is open and this tab is visible: candles, levels, gamma / OI, Option Analyzer and MM data (the slider stays where it is)"><input type="checkbox" id="fx-auto" ${S.fx.auto !== false ? 'checked' : ''}>Auto-refresh 5 min <span class="dim mono" id="fx-upd" style="font-size:11px"></span></label>
      <label class="f" title="Draw a strategy's study trades on this chart (the trades on the chart's symbol)">Show trades of<select id="fx-strat"><option value="">— none —</option></select></label></div>
    <p class="note">QQQ and other stocks come from Alpaca (regular hours${alp.connected ? ', feed ' + esc(alp.feed.toUpperCase()) + (alp.feed === 'sip' ? ': SIP is 15 minutes late on the free plan, so the last 15 minutes come from IEX, in real time' : '') : ': <a href="#data">connect Alpaca</a>'}). NQ and ES come from the stored Databento candles, all hours like TradeWellX's NQ=F.</p>`;
  view(`${head('Fractal', 'Wave pivots, H/L counts, percent-probable clusters and wave odds, calculated the TradeWellX way.')}
    <section class="panel"><div class="bd">${ctl}</div></section><div id="fx-out"><div class="panel"><div class="empty muted">Loading…</div></div></div>`);
  S.fxLast = Date.now();
  S.timers.push(setInterval(fxTick, 30000));            // cleared when leaving the page (route -> clearTimers)
  S.fxStrats = null;                                    // the strategy list is fresh on each visit
  fxStratList().then(() => { if (S.fx.strat) fxStratLoad(); });
  return fxLoad();
}
/* Fold any Fractal page panel: an arrow before its title hides everything but the header (kept in this browser). */
function fxFold(p, fold) {
  p.dataset.folded = fold ? '1' : '0';
  [...p.children].forEach(c => { if (!c.classList.contains('hd')) c.style.display = fold ? 'none' : ''; });
  const b = p.querySelector(':scope > .hd [data-act=fold]'); if (b) b.textContent = fold ? '▸' : '▾';
}
function fxFolds() {
  document.querySelectorAll('#view .panel > .hd').forEach(hd => {
    const h2 = hd.querySelector('h2'); if (!h2 || hd.querySelector('[data-act=fold]')) return;
    const k = h2.textContent.trim().slice(0, 40);
    h2.insertAdjacentHTML('afterbegin', `<button class="btn sm" data-act="fold" data-k="${esc(k)}" title="Fold / unfold" style="padding:0 7px;margin-right:6px">▾</button>`);
    let f = false; try { f = localStorage.getItem('fold:' + k) === '1'; } catch (e) { /* private mode */ }
    if (f) fxFold(hd.parentElement, true);
  });
}
/* Auto-refresh: every 5 minutes while the chart's market is open (stocks 09:30-16:05 ET on weekdays; NQ / ES Sunday
   18:00 to Friday 17:00 ET, except 17:00-18:00) and the tab is visible. The slider stays on the same bar (or on the
   latest bar if it was there); the panels keep their settings. */
function fxMarketOpen(sym) {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date()).map(x => [x.type, x.value]));
  const wd = p.weekday, hm = `${p.hour}:${p.minute}`;
  if (['NQ', 'ES'].includes(sym)) return !(wd === 'Sat' || (wd === 'Sun' && hm < '18:00') || (wd === 'Fri' && hm >= '17:00') || (hm >= '17:00' && hm < '18:00'));
  return !['Sat', 'Sun'].includes(wd) && hm >= '09:30' && hm <= '16:05';
}
function fxTick() {
  if (S.page !== 'fractal' || S.fx.auto === false || document.hidden || !S.fxR || S.fxBusy) return;
  if (Date.now() - (S.fxLast || 0) < 5 * 60e3 - 5e3 || !fxMarketOpen(S.fx.sym)) return;
  fxRefresh();
}
async function fxRefresh() {
  const f = S.fx, sl = $('#fx-k'), old = S.fxR; if (!sl || !old) return;
  S.fxBusy = true; S.fxLast = Date.now();
  try {
    const atEnd = +sl.value >= old.bars.length - 1, t0 = old.bars[+sl.value][0];
    const r = await api(`/fractal?sym=${encodeURIComponent(f.sym)}&wave=${f.wave}&days=${f.days}&stats=${f.stats}&agg=${f.agg}&rth=${f.rth ? 1 : 0}&cold=${f.cold ? 1 : 0}&close16=${f.close16 !== false ? 1 : 0}&macro=${f.macro || 'ema30'}`);
    if (S.page !== 'fractal' || S.fxR !== old) return;  // the page changed meanwhile
    S.fxR = r;
    sl.max = r.bars.length - 1; sl.min = Math.min(r.bars.length - 1, 2 * r.params.wave);
    const i = r.bars.findIndex(b => b[0] === t0);
    sl.value = atEnd || i < 0 ? r.bars.length - 1 : i;
    fxAt(); if (typeof fxTable === 'function') fxTable();
    await Promise.allSettled([gxLoad(), oaLoad(($('#oa-exp') || {}).value || undefined), mmLoad()]);
    const u = $('#fx-upd'); if (u) u.textContent = '· updated ' + new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
  } catch (e) { const u = $('#fx-upd'); if (u) u.textContent = '· refresh failed: ' + e.message; }
  finally { S.fxBusy = false; }
}
/* A strategy's study trades on the Fractal chart: entry (triangle) and exit (dot), joined, with the result. */
async function fxStratList() {
  const sel = $('#fx-strat'); if (!sel) return;
  try {
    const d = S.fxStrats || (S.fxStrats = (await api('/strategies')).strategies || []);
    sel.innerHTML = '<option value="">— none —</option>' + d.map(x => `<option value="${esc(x.id)}" ${x.id === S.fx.strat ? 'selected' : ''}>${esc(x.name)} (${esc((x.params.tickers || []).join(', '))})</option>`).join('');
  } catch (e) { /* the list is optional */ }
}
async function fxStratLoad() {
  S.fxTr = null;
  if (S.fx.strat) { try { S.fxTr = (await api(`/strategies/${S.fx.strat}/trades?set=study`)).trades || []; } catch (e) { S.fxTr = []; toast(e.message); } }
  fxAt();
}
function fxTrades(r, k, X, YR, rv, shown) {
  const ts = S.fxTr; if (!ts || !ts.length) return '';
  const sym = r.params.sym, starts = r.bars.map(b => b[0].replace('T', ' ')), first = starts[0], last = starts[starts.length - 1];
  const at = t => { t = t.replace('T', ' ').slice(0, 16); if (t < first || t.slice(0, 10) > last.slice(0, 10)) return -1;
    let lo = 0, hi = starts.length - 1; while (lo < hi) { const m = (lo + hi + 1) >> 1; if (starts[m] <= t) lo = m; else hi = m - 1; } return starts[lo].slice(0, 10) === t.slice(0, 10) ? lo : -1; };
  return ts.filter(t => t.s === sym).map(t => {
    const i = at(t.in), j = at(t.out || t.in); if (i < 0 || i > shown) return '';
    const L = t.side === 'long', ye = YR(rv(i, t.e)), win = (t.net || 0) > 0, col = win ? '#58a6ff' : '#f0883e';
    const tip = `<title>${esc(t.in)} ${t.side} ${fmt(t.e)} → ${esc(t.out || 'open')} ${t.x != null ? fmt(t.x) : ''} · ${esc(t.why || '')} · ${t.r != null ? sgn(t.r, 2, 'R') : ''} · ${t.net != null ? (t.net >= 0 ? '+' : '−') + '$' + Math.abs(Math.round(t.net)).toLocaleString() : ''}${t.stop != null ? ` · stop ${fmt(t.stop)}` : ''}${t.tgt != null ? ` · target ${fmt(t.tgt)}` : ''}</title>`;
    const ent = `<g>${tip}<rect x="${X(i) - 7}" y="${ye - 7}" width="14" height="14" rx="3" fill="${L ? '#1f883d' : '#cf222e'}" stroke="#fff" stroke-width="1.2"/><text x="${X(i)}" y="${ye + 4}" font-size="10" font-weight="700" text-anchor="middle" fill="#fff">${L ? 'L' : 'S'}</text></g>`;
    if (j < 0 || j > shown || t.x == null) return ent;
    const yx = YR(rv(j, t.x));
    return `<g class="fx-tr">${tip}<line x1="${X(i)}" y1="${ye}" x2="${X(j)}" y2="${yx}" stroke="${col}" stroke-width="2.2"/>${ent}<circle cx="${X(j)}" cy="${yx}" r="5.5" fill="${col}" stroke="#fff" stroke-width="1.2"/><text x="${X(j)}" y="${yx - 9}" font-size="9.5" text-anchor="middle" fill="${col}">${t.r != null ? sgn(t.r, 1, 'R') : ''}</text></g>`;
  }).join('');
}
async function fxLoad() {
  const f = S.fx, out = $('#fx-out');
  let r;
  try { r = await api(`/fractal?sym=${encodeURIComponent(f.sym)}&wave=${f.wave}&days=${f.days}&stats=${f.stats}&agg=${f.agg}&rth=${f.rth ? 1 : 0}&cold=${f.cold ? 1 : 0}&close16=${f.close16 !== false ? 1 : 0}&macro=${f.macro || 'ema30'}`); }
  catch (e) { out.innerHTML = `<div class="panel"><div class="empty loss">${esc(e.message)}</div></div>`; return; }
  S.fxR = r; S.fxLast = Date.now();
  const last = r.series[r.series.length - 1], pct = (a, b) => b ? sgn((a / b - 1) * 100, 2, '%') : '—';
  const lvl = (x, cls) => `<tr><td><span class="pill ${cls}">${Math.round(x.p * 100)}%</span></td><td class="r mono"><b>${fmt(x.price)}</b></td><td class="r mono">${sgn(x.move * 100, 2, '%')}</td></tr>`;
  const odds = (rows, side) => `<table class="tbl"><thead><tr><th>${side}</th><th class="r">Waves</th><th class="r">% of waves</th><th class="r">Odds (at least)</th></tr></thead><tbody>${rows.map(o => `<tr><td class="mono">${side}${o.count}</td><td class="r mono">${o.n}</td><td class="r mono">${(o.pct * 100).toFixed(0)}%</td><td class="r"><b>${(o.atLeast * 100).toFixed(0)}%</b></td></tr>`).join('')}</tbody></table>`;
  const cur = r.current;
  out.innerHTML = `
    <section class="panel"><div class="hd"><h2>${esc(r.params.sym)} · Wave [${r.params.wave}] · ${r.params.agg} min · ${r.bars.length} bars · ${r.params.days} days</h2><div class="grow"></div><span class="dim" style="font-size:12px">${esc(r.bars[0][0])} → ${esc(r.bars[r.bars.length - 1][0])} ET · levels from ${r.waves.length} waves since ${esc(String(r.params.statsFrom).slice(0, 10))} · ${esc(r.params.source)}</span><button class="btn sm" data-act="fx-csv" style="margin-left:10px">Download CSV</button></div>
      <div class="bd"><div id="gx-top" style="margin-bottom:12px"></div><div class="kv">
        <div><div class="k">Close</div><div class="v">${fmt(r.close)}</div></div>
        <div><div class="k">Pivot</div><div class="v"><b>${fmt(r.pivot)}</b></div></div>
        <div><div class="k">InProgress pivot (MA)</div><div class="v">${fmt(last.ma)}</div></div>
        <div><div class="k">Pivot close %</div><div class="v">${pct(r.close, r.pivot)}</div></div>
        <div><div class="k">SlowMA</div><div class="v">${fmt(last.slowMa)}</div></div>
        <div><div class="k">Direction</div><div class="v ${last.direction > 0 ? 'win' : last.direction < 0 ? 'loss' : ''}">${last.direction > 0 ? '+1 long' : last.direction < 0 ? '−1 short' : '0'}</div></div>
        <div><div class="k">Current wave (counted, like TradeWellX)</div><div class="v">${cur ? `${cur.side}${cur.count} · ${cur.bars} bars` : '—'}</div></div>
        <div><div class="k">Waves</div><div class="v">${r.oddsH.reduce((a, o) => a + o.n, 0)} H · ${r.oddsL.reduce((a, o) => a + o.n, 0)} L</div></div>
      </div>
      <div style="margin-top:14px;display:flex;gap:14px;align-items:center;flex-wrap:wrap"><label class="check"><input type="checkbox" id="fx-next" ${S.fx.next !== false ? 'checked' : ''}> Show what came next (faded)</label><label class="check"><input type="checkbox" id="fx-imp" ${S.fx.imp !== false ? 'checked' : ''}> Open / closed wave lines</label><label class="check"><input type="checkbox" id="fx-lin" ${S.fx.linear ? 'checked' : ''}> Linear graph (real prices, one scale)</label>
        <label class="check" title="TradeWellX keeps the same %s through the replay; untick to recompute them from the waves finished by each bar"><input type="checkbox" id="fx-fixed" ${S.fx.fixed !== false ? 'checked' : ''}> Same %s through the replay</label>
        <label class="f" style="flex-direction:row;align-items:center;gap:6px">Wave move<select id="fx-basis"><option value="pivot" ${S.fx.basis !== 'peak' ? 'selected' : ''}>from its pivot</option><option value="peak" ${S.fx.basis === 'peak' ? 'selected' : ''}>peak to trough</option></select></label></div>
      <div style="display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:10px;position:sticky;top:0;z-index:5;background:var(--panel, #11161d);padding:6px 0">
        <span class="dim mono" id="fx-at" style="font-size:12px">${esc(r.bars[r.bars.length - 1][0])}</span>
        <button class="btn sm" data-act="fx-step" data-d="-1">◀</button>
        <input type="range" id="fx-k" min="${Math.min(r.bars.length - 1, 2 * r.params.wave)}" max="${r.bars.length - 1}" value="${r.bars.length - 1}" style="width:min(520px,60vw)">
        <button class="btn sm" data-act="fx-step" data-d="1">▶</button>
        <button class="btn sm" data-act="fx-step" data-d="9999">Latest</button></div>
      <div id="fx-tog" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${fxTogBar()}</div>
      <div id="fx-chart" style="margin-top:6px">${fxChart(r, r.bars.length - 1)}</div>
      <div id="fx-lower">${fxLower(r, r.bars.length - 1)}</div>
</div></section>
    <div id="gx-out" hidden></div>
    <section class="panel"><div class="hd"><h2>Option Analyzer</h2><span class="dim" style="font-size:12px">odds of an H / L wave reaching each length · market makers' position at expiry</span></div>
      <div class="bd"><div id="oa-odds">${oaOdds(r)}</div>
        <div style="display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin:16px 0 8px"><h3 style="margin:0 12px 0 0">Market Maker Landscape</h3>
          <label class="f">Expiry<select id="oa-exp"><option value="">nearest</option></select></label>
          <button class="btn sm" data-act="oa-load">Calculate MMM</button><span class="dim" id="oa-note" style="font-size:12px"></span></div>
        <div id="oa-out"><div class="empty muted">Loading…</div></div>
        <div style="display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin:18px 0 8px"><h3 style="margin:0 12px 0 0">Intraday MM data</h3>
          <label class="f">Days<select id="mm-days">${[1, 2, 3, 5, 10, 20, 60, 120].map(n => `<option ${n === 3 ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
          <button class="btn sm" data-act="mm-load">Refresh</button><button class="btn sm" data-act="mm-snap">Snapshot now</button>
          <button class="btn sm" data-act="mm-backfill" title="Rebuild the last N sessions (Days) for QQQ and SPY from Unusual Whales history: a few minutes per day">Rebuild past days</button>
          <span class="dim" id="mm-job" style="font-size:12px">recorded every 5 minutes, 09:30–16:00 ET, for QQQ and SPY</span></div>
        <div id="mm-out"><div class="empty muted">Loading…</div></div></div></section>
    <section class="panel"><div class="hd"><h2>Spread Monster</h2><span class="dim" style="font-size:12px">forward looking density and odds, from past days in the same daily wave state</span></div>
      <div class="bd"><div style="display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin-bottom:10px">
        <label class="f">Forward days<select id="vp-fwd">${[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(n => `<option ${n === (S.vp || {}).fwd ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
        <label class="f">Matching<select id="vp-match"><option value="hl">HL state</option><option value="weekday" ${(S.vp || {}).match === 'weekday' ? 'selected' : ''}>Weekday</option></select></label>
        <label class="f">Wave<input id="vp-wave" type="number" min="1" max="50" value="${(S.vp || {}).wave || 1}" style="width:64px"></label>
        <label class="f">Aggregations (days)<input id="vp-days" type="number" min="30" max="1000" value="${(S.vp || {}).days || 200}" style="width:80px"></label>
        <label class="f">Graph<select id="vp-view"><option value="density">Density</option><option value="odds" ${S.vp.view === 'odds' ? 'selected' : ''}>Odds</option></select></label>
        <label class="f" style="flex-direction:row;align-items:center;gap:4px"><input type="checkbox" id="vp-spag" ${S.vp.spag !== false ? 'checked' : ''}>Spaghetti</label>
        <label class="f" style="flex-direction:row;align-items:center;gap:4px"><input type="checkbox" id="vp-bg" ${S.vp.bg !== false ? 'checked' : ''}>Background</label>
        <label class="f" style="flex-direction:row;align-items:center;gap:4px"><input type="checkbox" id="vp-x" ${S.vp.x !== false ? 'checked' : ''}>Strikes X</label>
        <button class="btn sm" data-act="vp-load">Project</button>
        <button class="btn sm" data-act="vp-step" data-d="-1" title="Replay the previous day">◀ Back</button>
        <button class="btn sm" data-act="vp-step" data-d="1" title="Next day">Forward ▶</button>
        <button class="btn sm" data-act="vp-step" data-d="9999">Latest</button></div>
        <div id="vp-out"><div class="empty muted">Loading…</div></div></div></section>
    <div class="row"><div class="col-main">
      <section class="panel"><div class="hd"><h2>Clusters: percent probable</h2><span class="dim" style="font-size:12px">at the last bar · green from the lower of pivot / vector, red from the higher</span></div>
        <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Level</th><th class="r">Price</th><th class="r">Move</th></tr></thead><tbody id="fx-cl"></tbody></table></div></section>
      <section class="panel"><div class="hd"><h2>Waves</h2><span class="dim" style="font-size:12px">count = bars from the wave start to its extreme</span></div>
        <div class="tbl-wrap" style="max-height:360px;overflow:auto"><table class="tbl"><thead><tr><th>Label</th><th>Start</th><th>Extreme at</th><th class="r">Bars</th><th class="r">Pivot</th><th class="r">Extreme</th><th class="r">Move</th><th class="r">Peak to trough</th></tr></thead><tbody>
          ${[...r.waves].reverse().map(w => `<tr><td class="mono ${w.side === 'H' ? 'win' : 'loss'}">${w.side}${w.count}</td><td class="mono">${esc(w.start.slice(5))}</td><td class="mono">${esc(w.extreme.slice(5))}</td><td class="r mono">${w.bars}</td><td class="r mono">${fmt(w.pivot)}</td><td class="r mono">${fmt(w.price)}</td><td class="r mono">${sgn((w.side === 'H' ? 1 : -1) * w.move * 100, 2, '%')}</td><td class="r mono">${w.ptMove == null ? '—' : sgn((w.side === 'H' ? 1 : -1) * w.ptMove * 100, 2, '%')}</td></tr>`).join('')}</tbody></table></div></section>
    </div><div class="col-side">
      <section class="panel"><div class="hd"><h2>Option Analyzer: H waves</h2></div><div class="tbl-wrap">${odds(r.oddsH, 'H')}</div></section>
      <section class="panel"><div class="hd"><h2>Option Analyzer: L waves</h2></div><div class="tbl-wrap">${odds(r.oddsL, 'L')}</div></section>
    </div></div>`;
  fxTable();
  gxLoad();
  oaLoad();
  mmLoad();
  vpLoad();
  fxFolds();
}
/* MacroTrend strip + StdDev panel under the chart (same x axis as fxChart). */
/* Show / hide parts of the chart (kept in this browser): arrows on the buttons above the chart. */
const FX_LAYERS = [['macro', 'Macro Trend'], ['sd', 'StdDev'], ['gex', 'GEX / OI column'], ['glines', 'Gamma lines'], ['lbl', 'Wave labels (1H, 2L…)']];
S.fxShow = (() => { try { return JSON.parse(localStorage.getItem('fxShow')) || {}; } catch (e) { return {}; } })();
const fxOn = key => S.fxShow[key] !== false;
function fxTogBar() {
  return FX_LAYERS.map(([k, t]) => `<button class="btn sm" data-act="fx-tog" data-k="${k}" title="${fxOn(k) ? 'Hide' : 'Show'} ${t}" style="${fxOn(k) ? '' : 'opacity:.55'}">${fxOn(k) ? '▾' : '▸'} ${t}</button>`).join('');
}
const FX_SPACE = 20;                                     // empty candle slots after the last bar (room to read the right edge)
function fxLayout(r) {
  /* The price chart and the indicators below share this: same width, same right margin, same candle x. */
  const gxg = fxGammaFor(r), GX = gxg && fxOn('gex') ? 190 : 0, W = 1100 + GX, padR = 170 + GX, slots = r.bars.length + FX_SPACE, cw = (W - padR) / slots;
  return { gxg, GX, W, padR, cw, X: i => (i + 0.5) * cw, plotR: r.bars.length * cw };
}
function fxLower(r, k) {
  const L0 = fxLayout(r), W = L0.W, padR = L0.padR, n = r.bars.length, cw = L0.cw, X = L0.X, shown = S.fx.next !== false ? n - 1 : k;
  const mOn = fxOn('macro'), sOn = fxOn('sd');
  if (!mOn && !sOn) return '';
  const SH = mOn ? 14 : 0, PH = sOn ? 130 : 0, top = SH + (sOn ? 6 : 0), H = top + PH + (sOn ? 16 : 0);
  const strip = !mOn ? '' : r.series.map((x, i) => i > shown || x.macro == null ? '' : `<rect x="${i * cw}" y="0" width="${cw + 0.5}" height="${SH}" fill="${x.macro > 0 ? '#2E9E5B' : '#D0453B'}" opacity="${i > k ? .3 : 1}"><title>${esc(r.bars[i][0])} MacroTrend ${x.macroTrend}</title></rect>`).join('');
  const sd = r.series.map(x => x.stdev), vmax = Math.max(1, ...sd.slice(0, shown + 1).filter(v => v != null)) * 1.08;
  const lg = v => Math.log10(v + 0.05), y0 = lg(0), y1 = lg(vmax);             // log scale, like TradeWellX's 0.1 … 2.6 axis
  const Y = v => top + PH - (lg(v) - y0) / (y1 - y0) * PH;
  const refs = !sOn ? '' : [[0.1, 'StdDev squeeze'], [0.5, 'Default'], [0.8, 'Expanding'], [1.7, ''], [2.6, '']].filter(([v]) => v <= vmax)
    .map(([v, t]) => `<line x1="0" x2="${W - padR}" y1="${Y(v)}" y2="${Y(v)}" stroke="${v === 0.1 ? '#3b82f6' : '#8b949e'}" opacity="${v === 0.1 ? .9 : .35}"/><text x="${W - padR + 4}" y="${Y(v) + 3}" font-size="10" fill="#8b949e">${v}${t ? ' ' + t : ''}</text>`).join('');
  let segs = '';
  for (let i = 1; sOn && i <= shown; i++) {
    if (sd[i] == null || sd[i - 1] == null) continue;
    segs += `<line x1="${X(i - 1)}" x2="${X(i)}" y1="${Y(sd[i - 1])}" y2="${Y(sd[i])}" stroke="${sd[i] >= sd[i - 1] ? '#2E9E5B' : '#D0453B'}" stroke-width="2" opacity="${i > k ? .3 : 1}"/>`;
  }
  const dots = !sOn ? '' : sd.map((v, i) => i <= shown && v != null && v < 0.1 ? `<circle cx="${X(i)}" cy="${H - 6}" r="2.2" fill="#e6edf3" opacity="${i > k ? .3 : .9}"><title>${esc(r.bars[i][0])} squeeze: ${v.toFixed(3)}</title></circle>` : '').join('');
  const days = r.bars.map((b, i) => i && b[0].slice(0, 10) !== r.bars[i - 1][0].slice(0, 10) ? `<line x1="${i * cw}" x2="${i * cw}" y1="0" y2="${H}" stroke="#E5C100" opacity=".25"/>` : '').join('');
  const cur = r.series[k];
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;background:#151A22;border-radius:6px;margin-top:6px">${days}${strip}
    ${mOn ? `<text x="${W - padR + 4}" y="${SH - 3}" font-size="10.5" fill="#cfd6df">Macro Trend ${cur.macroTrend || '—'}</text>` : ''}${refs}${segs}${dots}
    <line x1="${X(k)}" x2="${X(k)}" y1="0" y2="${H}" stroke="#8ab4ff" stroke-dasharray="4 3" opacity=".7"/>
    ${!sOn ? '' : `<text x="6" y="${top + 12}" font-size="10.5" fill="#cfd6df">StdDev ${cur.stdev != null ? cur.stdev.toFixed(2) : '—'}% ${cur.stdev == null ? '' : cur.stdev < 0.1 ? '· squeeze' : cur.stdev >= 0.8 ? '· expanding' : ''}</text>`}</svg>
    <div class="note">Macro Trend: green when the close is above its ${({ ema30: 'EMA 30', ema40: 'EMA 40', sma50: '50-bar average' })[r.params.macro] || 'EMA 30'}, red below (TradeWellX's MacroTrend L001 / S001 count in the hover). StdDev: standard deviation of the last ${r.params.wave} closes in % of price, green while rising, red while falling; dots = squeeze (under 0.1).</div>`;
}
function fxAt() {
  const sl = $('#fx-k'), r = S.fxR; if (!sl || !r) return;
  const k = +sl.value; $('#fx-chart').innerHTML = fxChart(r, k); $('#fx-lower').innerHTML = fxLower(r, k); $('#fx-at').textContent = r.bars[k][0];
}
document.addEventListener('input', e => { if (e.target.id === 'fx-k') fxAt(); });
document.addEventListener('change', e => { if (e.target.id === 'fx-strat') { S.fx.strat = e.target.value; fxStratLoad(); } if (e.target.id === 'fx-auto') { S.fx.auto = e.target.checked; if (e.target.checked) fxTick(); } });
document.addEventListener('change', e => { if (['vp-spag', 'vp-bg', 'vp-x'].includes(e.target.id)) { S.vp[e.target.id.slice(3)] = e.target.checked; if (S.vpR) $('#vp-out').innerHTML = vpShow(S.vpR); } if (e.target.id === 'vp-view') { S.vp.view = e.target.value; if (S.vpR) $('#vp-out').innerHTML = vpShow(S.vpR); } if (e.target.id === 'vp-fwd' || e.target.id === 'vp-match') { S.vp.fwd = +$('#vp-fwd').value; S.vp.match = $('#vp-match').value; vpLoad(); } if (e.target.id === 'fx-next') { S.fx.next = e.target.checked; fxAt(); } if (e.target.id === 'fx-imp') { S.fx.imp = e.target.checked; fxAt(); } if (e.target.id === 'fx-lin') { S.fx.linear = e.target.checked; fxAt(); } if (e.target.id === 'fx-fixed') { S.fx.fixed = e.target.checked; fxAt(); fxTable(); } if (e.target.id === 'fx-basis') { S.fx.basis = e.target.value; fxAt(); fxTable(); } });
/* Levels in force at bar k (TradeWellX): every wave finished before k plus the wave still running at k (its move so
   far); the move that p of them reached, nearest wave, no two levels on one wave (backend/fractal.py levels()).
   Green from min(pivot, MA), red from max(pivot, MA). */
const FX_BULL = [0.80, 0.50, 0.20, 0.10, 0.05], FX_BEAR = [0.80, 0.50, 0.20, 0.10, 0.02];
function fxRank(vals, ps) {
  // TradeWellX ClusterNode: marks sorted from the largest move down, take mark round(p x n) (backend/fractal.py _rank)
  const v = [...vals].sort((a, b) => a - b), n = v.length;
  return ps.map(p => v[Math.min(n - 1, Math.max(0, n - 1 - Math.round(p * n)))]);
}
function fxFresh(r, k) {
  /* Levels as computed at bar k. Moves: "fixed" (TradeWellX: the %s are worked out once from every finished wave and
     stay the same through the replay) or "rolling" (only waves finished before k). Basis: from the wave's pivot, or
     peak-to-trough (from the previous wave's extreme). Green from min(pivot, MA), red from max(pivot, MA). */
  const I = r._idx, s = r.series[k], piv = s && s.pivot;
  if (!piv) return null;
  const last = r.bars.length - 1, cut = S.fx.fixed !== false ? last : k, pt = S.fx.basis === 'peak';
  const up = [], dn = [];
  const fixed = S.fx.fixed !== false;
  r.waves.forEach(w => { const b = I[w.end]; if ((b == null || b < cut || (fixed && w.open)) && (fixed || !w.open)) { const m = pt ? w.ptMove : w.move; if (m != null) (w.side === 'H' ? up : dn).push(m); } });
  const ma = s.ma ?? piv, lo = Math.min(piv, ma), hi = Math.max(piv, ma);
  return { pivot: piv, ma, nH: up.length, nL: dn.length, bullBase: lo, bearBase: hi,
    bull: up.length >= 2 ? fxRank(up, FX_BULL).map((m, i) => ({ p: FX_BULL[i], move: m, price: lo * (1 + m) })) : [],
    bear: dn.length >= 2 ? fxRank(dn, FX_BEAR).map((m, i) => ({ p: FX_BEAR[i], move: -m, price: hi * (1 - m) })) : [] };
}
function fxLevelsAt(r, k) {
  /* TradeWellX (from its replay): while price is BELOW the pivot, green updates every bar (from the vector) and red stays
     frozen at what it was on the bar before price crossed down; ABOVE the pivot, the reverse. The wave still running
     is not counted. */
  if (!r._idx) { r._idx = {}; r.bars.forEach((b, i) => { r._idx[b[0]] = i; }); }
  const now = fxFresh(r, k);
  if (!now) return null;
  const I = r._idx, cur = r.waves.find(w => (I[w.start] ?? -1) <= k && k <= (I[w.end] ?? -1));
  const a = cur ? (I[cur.start] ?? 0) : k, before = a > 0 ? fxFresh(r, a - 1) : null;
  const above = r.bars[k][4] > now.pivot;
  const out = { ...now, frozen: above ? 'bull' : 'bear', since: r.bars[a][0] };
  if (before) {
    if (above) { out.bull = before.bull; out.bullBase = before.bullBase; }
    else { out.bear = before.bear; out.bearBase = before.bearBase; }
  }
  if (!out.bull.length && !out.bear.length) return null;
  return out;
}
/* ---------- Gamma (Unusual Whales): levels table, NQ / ES conversion, GEX by strike ---------- */
S.gx = { source: 'oi', data: null };
const GX_BASE = { NQ: 'QQQ', ES: 'SPY' };
async function gxLoad() {
  const out = $('#gx-out'), top = $('#gx-top'); if (!out) return;
  const st = await api('/uw').catch(() => ({}));
  if (!st.connected) { out.innerHTML = '<div class="empty muted">Connect Unusual Whales on the <a href="#data">Data</a> page to show the gamma levels.</div>'; if (top) top.innerHTML = ''; S.gx.data = null; return; }
  const sym = (S.fx.sym || 'QQQ').toUpperCase(), base = GX_BASE[sym] || sym;
  out.innerHTML = '<div class="empty muted">Loading gamma…</div>';
  try { S.gx.data = await api(`/gamma?syms=${encodeURIComponent(base)}&source=${S.gx.source || 'oi'}`); }
  catch (e) { out.innerHTML = `<div class="empty loss">${esc(e.message)}</div>`; if (top) top.innerHTML = `<span class="dim">Gamma: ${esc(e.message)}</span>`; S.gx.data = null; return; }
  out.innerHTML = gxView(S.gx.data);
  if (S.fxR) fxAt();
  gxTop();
  if (S.vpR && $('#vp-out')) $('#vp-out').innerHTML = vpShow(S.vpR);
}
function gxTop() {
  const top = $('#gx-top'), gg = fxGammaFor(S.fxR); if (!top) return;
  if (!gg) { top.innerHTML = S.gx.data ? `<span class="dim">Gamma: no ${esc(S.fxR ? S.fxR.params.sym : '')} conversion yet (download its candles on the Data page).</span>` : ''; return; }
  const px = S.fxR.bars[S.fxR.bars.length - 1][4], d = v => v == null ? '' : ` <span class="dim">${sgn((v / px - 1) * 100, 2, '%')}</span>`;
  const chip = (t, v, c) => `<div style="padding:6px 10px;border-radius:6px;background:#0d1117;border-left:3px solid ${c}"><div class="dim" style="font-size:11px">${t}</div><div class="mono" style="font-size:14px">${v == null ? '—' : fmt(v)}${d(v)}</div></div>`;
  const pos = gg.net >= 0;
  top.innerHTML = `<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:stretch">
    ${chip('Call wall', gg.lv.call_wall, '#3fb950')}${chip('Put wall', gg.lv.put_wall, '#f85149')}${chip('Gamma magnet', gg.lv.gamma_magnet, '#B24BD6')}${chip('Gamma flip', gg.lv.gamma_flip, '#d29922')}
    <div style="padding:6px 10px;border-radius:6px;background:#0d1117;border-left:3px solid ${pos ? '#3fb950' : '#f85149'}"><div class="dim" style="font-size:11px">Net GEX ${esc(gg.base)} (±25%)</div><div class="mono" style="font-size:14px">${(gg.net / 1e9).toFixed(2)}B <span class="${pos ? 'win' : 'loss'}">${pos ? 'positive: moves damped' : 'negative: moves amplified'}</span></div></div>
    <div class="dim" style="font-size:11px;align-self:center">Unusual Whales · ${esc(String(gg.g.time || '').replace('T', ' ').slice(0, 16))} UTC${gg.f !== 1 ? ` · ${esc(gg.base)} × ${gg.f.toFixed(4)} → ${esc(gg.sym)}` : ''}</div></div>`;
}
function gxView(d) {
  const L = [['call_wall', 'Call wall', 'resistance'], ['gamma_magnet', 'Gamma magnet', 'pin'], ['gamma_flip', 'Gamma flip', 'zero gamma'], ['put_wall', 'Put wall', 'support']];
  const v = x => x == null ? '—' : fmt(x), diff = (a, b) => a == null || b == null ? '' : Math.abs(a - b) < 1e-9 ? ' <span class="dim">=</span>' : ` <span class="warn" title="Unusual Whales' level vs the one computed here from its per-strike gamma">≠</span>`;
  const tbl = d.items.map(g => `<div style="flex:1 1 520px;min-width:340px">
      <h3 style="margin:0 0 6px">${esc(g.sym)} <span class="dim mono" style="font-size:12px">${fmt(g.price)} · ${esc(String(g.time || '').replace('T', ' ').slice(0, 16))} UTC</span></h3>
      <table class="tbl"><thead><tr><th>Level</th><th class="r">Unusual Whales</th><th class="r">Computed</th>${g.fut ? `<th class="r">${esc(g.fut.sym)}</th>` : ''}</tr></thead><tbody>
      ${L.map(([k, t, h]) => `<tr><td>${t} <span class="dim" style="font-size:11px">${h}</span></td><td class="r mono">${v(g.uw[k])}</td><td class="r mono">${v(g.computed[k])}${diff(g.uw[k], g.computed[k])}</td>${g.fut ? `<td class="r mono">${v(g.fut.uw[k])}</td>` : ''}</tr>`).join('')}
      </tbody></table>
      ${g.fut ? `<div class="note">${esc(g.fut.sym)} = level × ${g.fut.ratio.toFixed(4)} (${esc(g.fut.sym)} ${fmt(g.fut.price)} at ${esc(g.fut.time)} ET ÷ ${esc(g.sym)} ${fmt(g.price)}), rounded to 0.25.</div>` : `<div class="note">${g.sym === 'SPY' ? 'ES' : 'NQ'} levels show once its candles are downloaded on the Data page.</div>`}
      ${gxBars(g)}</div>`).join('');
  return `<div style="display:flex;gap:18px;flex-wrap:wrap">${tbl}</div>
    <div class="note">Basis: ${d.source === 'vol' ? 'volume (directionalized)' : 'open interest'}. Computed = the same rules applied here to Unusual Whales' per-strike gamma (call wall: strike above the price with the largest positive net gamma; put wall: below; magnet: largest net gamma by size; flip: nearest zero crossing), so it can be compared with a Schwab option chain later.</div>`;
}
function gxBars(g) {
  const rows = g.strikes.filter(r => Math.abs(r.strike / g.price - 1) <= 0.05 && r.net !== 0);
  if (!rows.length) return '';
  const W = 520, H = Math.max(160, rows.length * 4.2), mx = Math.max(...rows.map(r => Math.abs(r.net))) || 1, k0 = rows[0].strike, k1 = rows[rows.length - 1].strike;
  const Y = k => 6 + (k1 - k) / ((k1 - k0) || 1) * (H - 12), X0 = W / 2, sc = (W / 2 - 50) / mx, bh = Math.max(1, (H - 12) / rows.length - 0.6);
  const bars = rows.map(r => `<rect x="${r.net >= 0 ? X0 : X0 + r.net * sc}" y="${Y(r.strike) - bh / 2}" width="${Math.max(0.5, Math.abs(r.net) * sc)}" height="${bh}" fill="${r.net >= 0 ? '#2E9E5B' : '#D0453B'}"><title>${r.strike}: net ${(r.net / 1e6).toFixed(1)}M (calls ${(r.call / 1e6).toFixed(1)}M, puts ${(r.put / 1e6).toFixed(1)}M) per 1% move</title></rect>`).join('');
  const ms = [[g.price, 'price', '#e6edf3'], [g.uw.call_wall, 'call wall', '#3fb950'], [g.uw.put_wall, 'put wall', '#f85149'], [g.uw.gamma_flip, 'flip', '#d29922'], [g.uw.gamma_magnet, 'magnet', '#B24BD6']]
    .filter(([k]) => k != null && k >= k0 && k <= k1).sort((a, b) => Y(a[0]) - Y(b[0]));
  let ly = -99;
  const marks = ms.map(([k, t, c]) => { const ty = Math.max(Y(k) - 3, ly + 11); ly = ty;
    return `<line x1="0" x2="${W}" y1="${Y(k)}" y2="${Y(k)}" stroke="${c}" stroke-dasharray="4 3"/><text x="${W - 4}" y="${ty}" font-size="10" text-anchor="end" fill="${c}">${t} ${fmt(k)}</text>`; }).join('');
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px;height:auto;background:#151A22;border-radius:6px;margin-top:8px">
    <line x1="${X0}" x2="${X0}" y1="0" y2="${H}" stroke="#8b949e" opacity=".4"/>${bars}
    ${marks}</svg>
    <div class="note">Net gamma by strike ($ per 1% move), ±5% of the price: green = positive (dealers dampen moves), red = negative (dealers amplify them).</div>`;
}
/* ---------- Option Analyzer: wave odds (top) + Market Maker Landscape (Unusual Whales option contracts) ---------- */
function oaOdds(r) {
  const bars = (rows, side, c) => {
    if (!rows.length) return '<div class="empty muted">No waves.</div>';
    const W = 360, H = 190, n = rows.length, bw = Math.min(26, (W - 30) / n - 8), mx = Math.max(...rows.map(o => o.pct));
    return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;background:#151A22;border-radius:6px">` + rows.map((o, i) => {
      const x = 22 + i * (W - 30) / n + ((W - 30) / n - bw) / 2, h = o.pct / mx * (H - 70), y = H - 40 - h;
      return `<rect x="${x}" y="${y}" width="${bw}" height="${h}" fill="${c}"><title>${side}${o.count}: ${o.n} waves (${(o.pct * 100).toFixed(0)}%), ${(o.atLeast * 100).toFixed(0)}% reach at least ${o.count} bars</title></rect>
        <text x="${x + bw / 2}" y="${y - 16}" font-size="10" text-anchor="middle" fill="#cfd6df">${o.n}</text><text x="${x + bw / 2}" y="${y - 5}" font-size="10" text-anchor="middle" fill="#cfd6df">${(o.pct * 100).toFixed(0)}%</text>
        <text x="${x + bw / 2}" y="${H - 26}" font-size="11" text-anchor="middle" fill="#e6edf3">${o.count}</text><text x="${x + bw / 2}" y="${H - 10}" font-size="10" text-anchor="middle" fill="#8b949e">${(o.atLeast * 100).toFixed(0)}%</text>`;
    }).join('') + `<text x="4" y="${H - 26}" font-size="11" fill="#e6edf3">${side}</text><text x="4" y="${H - 10}" font-size="9" fill="#8b949e">odds</text></svg>`;
  };
  const ws = r.waves.slice(-28), W = 420, H = 190, mU = Math.max(0.001, ...ws.filter(w => w.side === 'H').map(w => w.move)), mD = Math.max(0.001, ...ws.filter(w => w.side === 'L').map(w => w.move));
  const z = H * mU / (mU + mD) * 0.9 + 10, sc = (H - 30) / (mU + mD), bw = Math.max(3, (W - 40) / Math.max(1, ws.length) - 3);
  const avg = s2 => { const a = ws.filter(w => w.side === s2).map(w => w.move); return a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0; };
  const seq = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;background:#151A22;border-radius:6px"><line x1="34" x2="${W}" y1="${z}" y2="${z}" stroke="#e6edf3" opacity=".6"/>
    <line x1="34" x2="${W}" y1="${z - avg('H') * sc}" y2="${z - avg('H') * sc}" stroke="#3fb950" stroke-dasharray="3 3" opacity=".6"/><line x1="34" x2="${W}" y1="${z + avg('L') * sc}" y2="${z + avg('L') * sc}" stroke="#f85149" stroke-dasharray="3 3" opacity=".6"/>
    <text x="2" y="${z - avg('H') * sc + 3}" font-size="9" fill="#3fb950">+${(avg('H') * 100).toFixed(1)}%</text><text x="2" y="${z + avg('L') * sc + 3}" font-size="9" fill="#f85149">−${(avg('L') * 100).toFixed(1)}%</text>`
    + ws.map((w, i) => { const x = 38 + i * (W - 40) / ws.length, h = w.move * sc, up = w.side === 'H';
      return `<rect x="${x}" y="${up ? z - h : z}" width="${bw}" height="${Math.max(1, h)}" fill="${up ? '#2E9E5B' : '#D0453B'}"><title>${w.side}${w.count} ${esc(w.start)}: ${up ? '+' : '−'}${(w.move * 100).toFixed(2)}% from its pivot</title></rect><text x="${x + bw / 2}" y="${up ? z - h - 3 : z + h + 10}" font-size="9" text-anchor="middle" fill="#cfd6df">${w.count}</text>`; }).join('') + '</svg>';
  return `<div style="display:grid;grid-template-columns:1fr 1fr 1.2fr;gap:12px">
    <div><div class="dim" style="font-size:12px;margin-bottom:4px">H waves: bars to the high · % of waves · odds of reaching at least</div>${bars(r.oddsH, 'H', '#2E9E5B')}</div>
    <div><div class="dim" style="font-size:12px;margin-bottom:4px">L waves: bars to the low</div>${bars(r.oddsL, 'L', '#D0453B')}</div>
    <div><div class="dim" style="font-size:12px;margin-bottom:4px">Each wave's move from its pivot (last ${ws.length}) · dashed: averages</div>${seq}</div></div>`;
}
async function oaLoad(expiry) {
  const out = $('#oa-out'); if (!out) return;
  const st = await api('/uw').catch(() => ({}));
  if (!st.connected) { out.innerHTML = '<div class="empty muted">Connect Unusual Whales on the <a href="#data">Data</a> page for the market maker landscape.</div>'; return; }
  out.innerHTML = '<div class="empty muted">Loading the option chain…</div>';
  let d;
  try { d = await api(`/landscape?sym=${encodeURIComponent(S.fx.sym || 'QQQ')}${expiry ? '&expiry=' + expiry : ''}`); }
  catch (e) { out.innerHTML = `<div class="empty loss">${esc(e.message)}</div>`; return; }
  S.oa = d;
  $('#oa-exp').innerHTML = d.expiries.map(e => `<option value="${e.expiry}" ${e.expiry === d.expiry ? 'selected' : ''}>${e.expiry} · OI ${e.oi.toLocaleString()}</option>`).join('');
  out.innerHTML = oaView(d);
  const row = out.querySelector('tr.sel'), box = row && row.closest('.tbl-wrap');
  if (box) box.scrollTop = row.offsetTop - box.clientHeight / 2;
}
function oaView(d) {
  const f = d.fut, fx = v => v == null ? '—' : fmt(v), m = v => (v / 1e6).toFixed(1) + 'M';
  const fp = v => f && v != null ? ` <span class="dim">(${esc(f.sym)} ${fmt(Math.round(v * f.ratio * 4) / 4)})</span>` : '';
  const rows = d.targets.map(t => `<tr class="${t.move === 0 ? 'sel' : ''}"><td class="mono">${fmt(t.target)}${f ? `<span class="dim"> / ${fmt(Math.round(t.target * f.ratio * 4) / 4)}</span>` : ''}</td>
      <td class="r mono">${sgn(t.move * 100, 1, '%')}</td><td class="r mono ${t.holders < 0 ? 'loss' : 'win'}">${m(t.holders)}</td>
      <td class="mono">${t.best ? esc(t.best.contract.slice(-15)) : '—'}</td><td class="r mono">${t.best ? fmt(t.best.bto) : ''}</td><td class="r mono">${t.best ? fmt(t.best.stc) : ''}</td>
      <td class="r mono ${t.best && t.best.gain > 0 ? 'win' : 'loss'}">${t.best ? (t.best.gain * 100).toFixed(1) + '%' : ''}</td></tr>`).join('');
  // the landscape: holders' P&L at expiry by price, price on the vertical axis like TradeWellX
  const cv = d.curve, W = 430, H = 560, lo = cv[0].price, hi = cv[cv.length - 1].price, mn = Math.min(...cv.map(z => z.holders)), mxv = Math.max(0, ...cv.map(z => z.holders));
  const Y = p => 8 + (hi - p) / (hi - lo) * (H - 16), X = v => 30 + (v - mn) / ((mxv - mn) || 1) * (W - 140);
  const path = cv.map((z, i) => `${i ? 'L' : 'M'}${X(z.holders)},${Y(z.price)}`).join('');
  const line = (p, t, c) => p == null || p < lo || p > hi ? '' : `<line x1="0" x2="${W}" y1="${Y(p)}" y2="${Y(p)}" stroke="${c}" stroke-dasharray="5 3"/><text x="${W - 4}" y="${Y(p) - 4}" font-size="11" text-anchor="end" fill="${c}">${t} ${fmt(p)}${f ? ' · ' + f.sym + ' ' + fmt(Math.round(p * f.ratio * 4) / 4) : ''}</text>`;
  const chart = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;background:#151A22;border-radius:6px">
    <rect x="0" y="0" width="${W}" height="${Y(d.ceiling ?? hi)}" fill="#2E9E5B" opacity=".08"/><rect x="0" y="${Y(d.floor ?? lo)}" width="${W}" height="${H}" fill="#D0453B" opacity=".08"/>
    <line x1="${X(0)}" x2="${X(0)}" y1="0" y2="${H}" stroke="#8b949e" opacity=".5"/><path d="${path}" fill="none" stroke="#d29922" stroke-width="2"/>
    ${line(d.ceiling, 'Ceiling', '#3fb950')}${line(d.pml, 'PML', '#f0883e')}${line(d.price, 'Price', '#e6edf3')}${line(d.floor, 'Floor', '#f85149')}
    <text x="${X(0) + 4}" y="14" font-size="10" fill="#8b949e">holders' P&L at expiry → (0 = wash-out)</text></svg>`;
  const v = d.volumes;
  return `<div class="kv" style="margin-bottom:12px">
      <div><div class="k">${esc(d.sym)} price · expiry</div><div class="v">${fmt(d.price)} · ${esc(d.expiry)}</div></div>
      <div><div class="k">Peak Money Line (MM best)</div><div class="v">${fx(d.pml)}${fp(d.pml)} · ${m(d.pmlValue)}</div></div>
      <div><div class="k">Ceiling / Floor (wash-out)</div><div class="v">${fx(d.ceiling)} / ${fx(d.floor)}</div></div>
      <div><div class="k">Aggregate at the price</div><div class="v ${d.aggregate < 0 ? 'loss' : 'win'}">${m(d.aggregate)}</div></div>
      <div><div class="k">Contracts · OI calls / puts</div><div class="v">${d.contracts} · ${d.oi.calls.toLocaleString()} / ${d.oi.puts.toLocaleString()}</div></div>
      <div><div class="k">Volume ≥ PML calls / puts</div><div class="v">${v.pml.aboveCalls.toLocaleString()} / ${v.pml.abovePuts.toLocaleString()}</div></div>
      <div><div class="k">Volume &lt; PML calls / puts</div><div class="v">${v.pml.belowCalls.toLocaleString()} / ${v.pml.belowPuts.toLocaleString()}</div></div>
      <div><div class="k">Price vs PML</div><div class="v">${sgn((d.price / d.pml - 1) * 100, 2, '%')}</div></div></div>
    <div style="display:grid;grid-template-columns:1.4fr 1fr;gap:14px;align-items:start">
      <div class="tbl-wrap" style="max-height:560px;overflow:auto"><table class="tbl"><thead><tr><th>Target${f ? ' / ' + esc(f.sym) : ''}</th><th class="r">Move</th><th class="r">Holders P&L</th><th>Best contract</th><th class="r">BTO (ask)</th><th class="r">STC at target</th><th class="r">% gain</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div>${chart}</div></div>
    <div class="note">Holders P&L = what all ${esc(d.expiry)} option holders make or lose at expiry at that price (open interest × 100 × (value at expiry − premium at the NBBO mid)); market makers hold the other side. PML = their best price; Ceiling / Floor = where the holders break even. Best contract = the call (above) or put (below) with the highest % gain if bought at the ask now and the price is at the target at expiry. Unusual Whales option contracts, ${esc(String(d.price))} at load.</div>`;
}
async function mmPoll() {
  clearTimeout(S.mmTimer);
  const st = await api('/mmhistory/status').catch(() => ({})), el = $('#mm-job'); if (!el) return;
  el.innerHTML = st.status === 'running' ? `<span class="warn">${esc(st.msg || 'Rebuilding…')}</span>` : st.status === 'error' ? `<span class="loss">${esc(st.msg || 'Error')}</span>` : st.status === 'done' ? esc(st.msg || 'Done') : el.innerHTML;
  if (st.status === 'running') S.mmTimer = setTimeout(mmPoll, 5000); else if (st.status === 'done') mmLoad();
}
async function mmLoad() {
  const out = $('#mm-out'); if (!out) return;
  const st = await api('/uw').catch(() => ({}));
  if (!st.connected) { out.innerHTML = '<div class="empty muted">Connect Unusual Whales on the <a href="#data">Data</a> page to record the market maker data.</div>'; return; }
  let d;
  try { d = await api(`/mmhistory?sym=${encodeURIComponent(S.fx.sym || 'QQQ')}&days=${Math.min(20, +(($('#mm-days') || {}).value || 3))}`); }
  catch (e) { out.innerHTML = `<div class="empty loss">${esc(e.message)}</div>`; return; }
  out.innerHTML = mmView(d);
}
function mmView(d) {
  const pts = []; d.days.forEach(day => day.rows.forEach(r => pts.push({ ...r, day: day.date, key: day.date + ' ' + r.t })));
  if (pts.length < 2) return `<div class="empty muted">${pts.length ? 'One snapshot so far' : 'No snapshots yet'}: they are recorded every 5 minutes during the session; click Rebuild past days for earlier sessions.</div>`;
  const W = 1100, H1 = 300, H2 = 200, L = 56, R = 16, n = pts.length, X = i => L + i * (W - L - R) / (n - 1);
  const prices = pts.flatMap(p => [p.ceiling, p.floor, p.price, p.pml]).filter(v => v != null), lo = Math.min(...prices), hi = Math.max(...prices), pad = (hi - lo) * 0.06 || 1;
  const Y1 = v => 10 + (hi + pad - v) / (hi - lo + 2 * pad) * (H1 - 30);
  const vmax = Math.max(1, ...pts.flatMap(p => [p.pa, p.pb, p.ca, p.cb])), Y2 = v => 10 + (1 - v / vmax) * (H2 - 30);
  const ln = (key, Y, c, dash, w = 1.8) => { let dd = ''; pts.forEach((p, i) => { if (p[key] == null) return; dd += `${dd ? 'L' : 'M'}${X(i)},${Y(p[key])}`; }); return `<path d="${dd}" fill="none" stroke="${c}" stroke-width="${w}" ${dash ? 'stroke-dasharray="5 4"' : ''}/>`; };
  const days = pts.map((p, i) => i && p.day !== pts[i - 1].day ? `<line x1="${X(i)}" x2="${X(i)}" y1="0" y2="${H1 + H2}" stroke="#E5C100" opacity=".3"/>` : '').join('');
  let ds = 0;
  const ticks = pts.map((p, i) => { const first = i === 0 || p.day !== pts[i - 1].day; if (first) ds = i;
    const show = first || (p.t.endsWith(':00') && +p.t.slice(0, 2) % 2 === 0 && i - ds > 8);
    return show ? `<text x="${X(i)}" y="${H1 - 6}" font-size="10" text-anchor="${first ? 'start' : 'middle'}" fill="#8b949e">${first ? p.day.slice(5) + ' ' : ''}${p.t}</text>` : ''; }).join('');
  const yt = [0, 0.25, 0.5, 0.75, 1].map(f => { const v = lo - pad + f * (hi - lo + 2 * pad); return `<text x="${L - 6}" y="${Y1(v) + 3}" font-size="10" text-anchor="end" fill="#8b949e">${fmt(v)}</text><line x1="${L}" x2="${W - R}" y1="${Y1(v)}" y2="${Y1(v)}" stroke="#30363d" opacity=".5"/>`; }).join('');
  const vt = [0, 0.5, 1].map(f => `<text x="${L - 6}" y="${H1 + Y2(f * vmax) + 3}" font-size="10" text-anchor="end" fill="#8b949e">${Math.round(f * vmax / 1000)}k</text>`).join('');
  const last = pts[pts.length - 1], f = d.fut;
  const lg = (c, t, dash) => `<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px"><svg width="22" height="8"><line x1="0" x2="22" y1="4" y2="4" stroke="${c}" stroke-width="2" ${dash ? 'stroke-dasharray="4 3"' : ''}/></svg>${t}</span>`;
  const putDiv = last.pa > last.pb, callDiv = last.ca > last.cb;
  return `<div class="kv" style="margin-bottom:10px">
      <div><div class="k">Latest (${esc(last.day)} ${esc(last.t)}) · expiry ${esc(last.expiry || '')}</div><div class="v mono">C ${fmt(last.ceiling)} · F ${fmt(last.floor)} · GL ${fmt(last.price)} · PML ${fmt(last.pml)}${f ? ` <span class="dim">(${esc(f.sym)} ×${f.ratio.toFixed(2)})</span>` : ''}</div></div>
      <div><div class="k">Puts at/above vs below the price</div><div class="v ${putDiv ? 'loss' : ''}">${last.pa.toLocaleString()} vs ${last.pb.toLocaleString()} ${putDiv ? '· bearish divergence' : ''}</div></div>
      <div><div class="k">Calls at/above vs below the price</div><div class="v ${callDiv ? 'win' : ''}">${last.ca.toLocaleString()} vs ${last.cb.toLocaleString()} ${callDiv ? '· bullish' : ''}</div></div>
      <div><div class="k">Price vs PML</div><div class="v">${sgn((last.price / last.pml - 1) * 100, 2, '%')}</div></div></div>
    <div style="font-size:12px;margin-bottom:6px">${lg('#e6edf3', 'Ceiling')}${lg('#e6edf3', 'Floor', true)}${lg('#2E9E5B', 'GreenLine (price)')}${lg('#f85149', 'PML')}${lg('#a8f0a0', 'Puts at/above price')}${lg('#8b1d1d', 'Puts below')}${lg('#1f7a35', 'Calls at/above')}${lg('#f4a6a6', 'Calls below')}</div>
    <svg viewBox="0 0 ${W} ${H1 + H2}" style="width:100%;height:auto;background:#151A22;border-radius:6px">${days}${yt}${ticks}
      ${ln('ceiling', Y1, '#e6edf3')}${ln('floor', Y1, '#e6edf3', true)}${ln('price', Y1, '#2E9E5B')}${ln('pml', Y1, '#f85149')}
      <g transform="translate(0,${H1})"><line x1="${L}" x2="${W - R}" y1="${Y2(0)}" y2="${Y2(0)}" stroke="#30363d"/>
        ${ln('pa', Y2, '#a8f0a0')}${ln('pb', Y2, '#8b1d1d')}${ln('ca', Y2, '#1f7a35')}${ln('cb', Y2, '#f4a6a6')}</g>${vt}
      <text x="${L + 4}" y="${H1 + 14}" font-size="11" fill="#cfd6df">Option volume vs the price (day total so far)</text></svg>
    <div class="note">${d.days.filter(x => x.backfilled).length ? `Rebuilt from history: ${d.days.filter(x => x.backfilled).map(x => esc(x.date)).join(', ')}. ` : ''}Puts at/above the price (light green) above puts below (dark red) = bearish divergence; calls at/above (green) above calls below (pink) = buying pressure above. Volumes are the expiry's day totals at each snapshot, split at the price.</div>`;
}
/* Volatility projection (TradeWellX "Forward Looking Volatility"). */
async function vpLoad() {
  const out = $('#vp-out'); if (!out) return;
  const v = S.vp, sym = S.fx.sym || 'QQQ';
  let d;
  try { d = await api(`/volproj?sym=${encodeURIComponent(sym)}&wave=${v.wave}&days=${v.days}&fwd=${v.fwd}&match=${v.match}${v.asof ? '&asof=' + v.asof : ''}`); }
  catch (e) { out.innerHTML = `<div class="empty loss">${esc(e.message)}${v.asof ? ` (${esc(v.asof)}): step Back / Forward to another day.` : ''}</div>`;
    if (S.vpR && v.asof) S.vpR = { ...S.vpR, index: S.vpR.dates.indexOf(v.asof) }; return; }
  S.vpR = d; out.innerHTML = vpShow(d);
}
function vpShow(d) {
  // the statistics table sits under both graphs
  const html = S.vp.view === 'odds' ? vpOdds(d) : vpView(d), i = html.indexOf('<div class="tbl-wrap"');
  return S.vp.view === 'odds' ? html + vpView(d).slice(vpView(d).indexOf('<div class="tbl-wrap"')) : html;
}
function vpView(d) {
  /* TradeWellX Spread Monster "Forward Looking Density": density background, sinusoidal waves (spaghetti), 1% / 25% / 50% levels. */
  const f = d.fut ? d.fut.ratio : 1, u = d.fut ? d.fut.sym : d.sym, P = d.price * f, pc = m => P * (1 + m);
  const B = d.bull, R = d.bear, opt = S.vp, red = d.fwd > 1;
  const W = 1100, H = 560, T = 16, Bm = 40, xs = W * 0.34, xe = W * 0.72, xR = W - 128;
  const top = pc(B.max), bot = pc(R.max), pad = (top - bot) * 0.015, Y = v => T + (top + pad - v) / (top - bot + 2 * pad) * (H - T - Bm);
  const k = d.bars.findIndex(b => b[0] === d.date), after = d.bars.slice(k + 1);
  // forward trading days: the real ones when replaying, else the next weekdays
  const days = []; { let t = new Date(d.date + 'T12:00:00Z'); for (let i = 0; i < d.fwd; i++) { if (after[i]) { days.push(after[i][0]); continue; }
    do { t = new Date(t.getTime() + 864e5); } while ([0, 6].includes(t.getUTCDay())); days.push(t.toISOString().slice(0, 10)); if (after[i - 1]) t = new Date(after[i - 1][0] + 'T12:00:00Z'); } }
  const wd = s2 => ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][new Date(s2 + 'T12:00:00Z').getUTCDay()];
  // x of each day's low / high point (low first, then high, like TradeWellX); one vertical line per point for 1 day, per day otherwise
  const a0 = xs + (xe - xs) * 0.12, a1 = xs + (xe - xs) * (d.fwd === 1 ? 0.46 : 0.74), slot = (a1 - a0) / d.fwd;
  const xLo = i => d.fwd === 1 ? a0 + slot * 0.2 : a0 + slot * (i + 0.25), xHi = i => d.fwd === 1 ? a0 + slot * 0.8 : a0 + slot * (i + 0.75), xDay = i => d.fwd === 1 ? xHi(i) : a0 + slot * (i + 0.5);
  // density: how many matched days' high-low ranges cover each price
  const NB = 140, bins = new Array(NB).fill(0), bp = i => bot - pad + (i + 0.5) / NB * (top - bot + 2 * pad);
  d.paths.forEach(p => p.forEach(([h, l]) => { const hi = pc(h), lo = pc(l); for (let i = 0; i < NB; i++) { const v = bp(i); if (v >= lo && v <= hi) bins[i]++; } }));
  const bmax = Math.max(1, ...bins), bh = (H - T - Bm) / NB * 1.02;
  const dens = opt.bg === false ? '' : bins.map((c, i) => { const a = Math.pow(c / bmax, 0.8) * (red ? 0.75 : 0.5);
    return a > 0.01 ? `<rect x="0" y="${Y(bp(i)) - bh / 2}" width="${xR}" height="${bh}" fill="${red ? '#b3261e' : '#dfe6ee'}" opacity="${a.toFixed(3)}"/>` : ''; }).join('');
  // spaghetti: price -> each day's low, high -> back to price (Catmull-Rom smoothing)
  const smooth = pts => { let o = `M${pts[0][0]},${pts[0][1]}`; for (let i = 0; i < pts.length - 1; i++) { const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
    o += `C${p1[0] + (p2[0] - p0[0]) / 6},${p1[1] + (p2[1] - p0[1]) / 6} ${p2[0] - (p3[0] - p1[0]) / 6},${p2[1] - (p3[1] - p1[1]) / 6} ${p2[0]},${p2[1]}`; } return o; };
  const spag = opt.spag === false ? '' : d.paths.map((p, j) => `<path d="${smooth([[xs, Y(P)], ...p.flatMap(([h, l], i) => [[xLo(i), Y(pc(l))], [xHi(i), Y(pc(h))]]), [xe, Y(P)]])}" fill="none" stroke="${red ? '#ff8a80' : '#e6edf3'}" stroke-width="1" opacity=".55"><title>${esc(d.matchDays[d.matchDays.length - d.paths.length + j] || '')}: ${p.map(([h, l]) => `H ${sgn(h * 100, 2, '%')} L ${sgn(l * 100, 2, '%')}`).join(' · ')}</title></path>`).join('');
  const dayLines = days.map((dt, i) => { const xx = d.fwd === 1 ? [xLo(i), xHi(i)] : [xDay(i)];
    return xx.map(x => `<line x1="${x}" x2="${x}" y1="${T}" y2="${H - Bm}" stroke="#4c6ef5" stroke-width="1.6"/><text x="${x}" y="${H - Bm + 13}" font-size="11" font-weight="700" text-anchor="middle" fill="#cfd6df">${dt.slice(8)}</text><text x="${x}" y="${H - Bm + 26}" font-size="10" text-anchor="middle" fill="#8b949e">${wd(dt)}</text>`).join(''); }).join('');
  // the actual days (replay): purple candle per day from its low to its high, and the window's high / low
  const real = after.slice(0, d.fwd), candles = real.map((b, i) => `<rect x="${xDay(i) - 3}" y="${Y(b[2] * f)}" width="6" height="${Math.max(2, Y(b[3] * f) - Y(b[2] * f))}" fill="#B24BD6"><title>${b[0]} H ${fmt(b[2] * f)} L ${fmt(b[3] * f)} C ${fmt(b[4] * f)}</title></rect>`).join('');
  const uf = d.unfold, hl = uf ? [['high', uf.high * f], ['low', uf.low * f]].map(([t, v]) => `<line x1="40" x2="${xR}" y1="${Y(v)}" y2="${Y(v)}" stroke="#e6edf3" stroke-dasharray="3 3" opacity=".7"/><text x="${xR - 4}" y="${Y(v) - 3}" font-size="10" text-anchor="end" fill="#e6edf3">${t} ${fmt(v)}</text>`).join('') : '';
  // levels: 1% = the outlier (Max), 25% = MaxAvg (the walls), 50% = the median
  const lvl = [[B.levels.max, '1%', '#3fb950', '1'], [B.levels.maxAvg, '25%', '#3fb950', '5 4'], [B.levels.median, '50%', '#2E9E5B', ''], [R.levels.median, '50%', '#D0453B', ''], [R.levels.maxAvg, '25%', '#f0883e', '5 4'], [R.levels.max, '1%', '#f0883e', '1']]
    .map(([v, t, c, dash]) => { const y = Y(v * f), dd = v * f - P;
      return `<line x1="0" x2="${W}" y1="${y}" y2="${y}" stroke="${c}" stroke-width="${t === '50%' ? 1.4 : 1}" ${dash && dash !== '1' ? `stroke-dasharray="${dash}"` : ''} opacity="${t === '1%' ? 0.7 : 0.9}"/>
        <text x="${W - 4}" y="${dd >= 0 ? y + 12 : y - 14}" font-size="11" text-anchor="end" fill="#e6edf3"><tspan font-weight="700">${fmt(v * f)}</tspan> ${t}</text>
        <text x="${W - 4}" y="${dd >= 0 ? y + 25 : y - 2}" font-size="10" text-anchor="end" fill="#8b949e">${dd >= 0 ? '+' : ''}${dd.toFixed(2)}</text>`; }).join('');
  // option strikes (Unusual Whales, if loaded): calls at the start, puts at the end
  const g = S.gx && S.gx.data && S.gx.data.items.find(x => x.sym === d.sym);
  const xm = (x, y, c) => `<text x="${x}" y="${y + 4}" font-size="12" text-anchor="middle" fill="${c}">X</text>`;
  const strikes = !g || opt.x === false ? '' : ['call', 'put'].map(side => g.strikes.filter(st => Math.abs(st[side]) > 0 && st.strike * f <= top && st.strike * f >= bot)
    .sort((p1, p2) => Math.abs(p2[side]) - Math.abs(p1[side])).slice(0, 30).map(st => xm(side === 'call' ? xs : xe, Y(st.strike * f), '#B24BD6')).join('')).join('');
  const xa = xs + (xe - xs) * 0.82;
  const arrows = `<line x1="${xa}" x2="${xa}" y1="${Y(P)}" y2="${Y(top) + 12}" stroke="#2E9E5B" stroke-width="1.6"/><path d="M${xa - 6},${Y(top) + 14} l6,-14 l6,14 z" fill="#2E9E5B"/>
    <line x1="${xa}" x2="${xa}" y1="${Y(P)}" y2="${Y(bot) - 12}" stroke="#D0453B" stroke-width="1.6"/><path d="M${xa - 6},${Y(bot) - 14} l6,14 l6,-14 z" fill="#D0453B"/>
    <text x="${xa - 4}" y="${(Y(P) + Y(top)) / 2}" font-size="11" text-anchor="end" fill="#cfd6df">Outsized Move</text><text x="${xa - 4}" y="${(Y(P) + Y(bot)) / 2}" font-size="11" text-anchor="end" fill="#cfd6df">Downside Risk</text>`;
  const hit = uf && uf.complete ? ` · actual: high ${fmt(uf.high * f)}, low ${fmt(uf.low * f)}` : uf ? ' · unfolding…' : '';
  return `<div class="kv" style="margin-bottom:10px">
      <div><div class="k">${esc(u)} · Wave [${d.wave}] Daily · Aggregations ${S.vp.days}</div><div class="v mono">${fmt(P)} · ${esc(d.date)} ${wd(d.date)} · state <b>${esc(d.state)}</b>${d.date !== d.to ? ' (replay)' : ''}</div></div>
      <div><div class="k">Forward looking density: ${d.fwd} aggregation${d.fwd > 1 ? 's' : ''} · ${d.match === 'weekday' ? 'weekday' : 'HL'} matching</div><div class="v">${d.matches} matching days${d.matchedBy && d.matchedBy !== 'state' && d.matchedBy !== 'weekday' ? ` <span class="warn" title="No earlier day in state ${esc(d.state)}: widened">(widened: ${esc(d.matchedBy)})</span>` : ''}</div></div>
      <div><div class="k">50% (median) long / short</div><div class="v mono"><span class="win">${fmt(B.levels.median * f)}</span> / <span class="loss">${fmt(R.levels.median * f)}</span></div></div>
      <div><div class="k">25% (wall) · 1% (outlier) long / short</div><div class="v mono"><span class="win">${fmt(B.levels.maxAvg * f)}</span> / <span class="loss">${fmt(R.levels.maxAvg * f)}</span> · ${fmt(B.levels.max * f)} / ${fmt(R.levels.max * f)}</div></div></div>
    <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;background:#151A22;border-radius:6px">${dens}${lvl}${dayLines}${spag}${strikes}${arrows}${hl}${candles}
      <circle cx="${xs}" cy="${Y(P)}" r="3" fill="#e6edf3"/><text x="${xs - 8}" y="${Y(P) - 4}" font-size="11" text-anchor="end" fill="#e6edf3">${fmt(P)}</text><text x="${xs - 8}" y="${Y(P) + 10}" font-size="10" text-anchor="end" fill="#8b949e">${esc(d.date)} ${wd(d.date)}</text>
      <text x="${(xs + xe) / 2}" y="${T - 3}" font-size="11" text-anchor="middle" fill="#cfd6df">Sinusoidal Wave</text>
      ${g && opt.x !== false ? `<text x="${xs}" y="${H - 4}" font-size="10" text-anchor="middle" fill="#8b949e">calls</text><text x="${xe}" y="${H - 4}" font-size="10" text-anchor="middle" fill="#8b949e">puts</text>` : ''}</svg>
    <div class="note">The deeper the colour, the more the price action resides. Fractal data set ${esc(d.from)} to ${esc(d.to)}${d.fut ? ` · ${esc(u)} = ${esc(d.sym)} × ${f.toFixed(2)}` : ''}${hit}. Purple bars: the actual days (step Back to replay). X: the largest call / put strikes by gamma (Unusual Whales, today).</div>
    <div class="tbl-wrap" style="margin-top:10px"><table class="tbl"><thead><tr><th>Statistic</th><th class="r">Count</th><th class="r">Odds</th></tr></thead><tbody>
      ${d.counts.map(x => `<tr><td>${esc(x.label)}</td><td class="r mono">${x.n} out of ${x.of}</td><td class="r mono">${(x.pct * 100).toFixed(2)}%</td></tr>`).join('')}</tbody></table></div>`;
}
function vpOdds(d) {
  /* TradeWellX Spread Monster "Forward Looking Odds". Each match's window high (bullish moves) and low (bearish moves), from that day's close.
     Bull odds of a price = (matches whose high reached it) / (2 x matches); bear the same with lows. Both are 50% at the far end of their list.
     Green curve = bull odds, red = bear odds, both plotted against price. Normal price range = lowest high to highest low. */
  const f = d.fut ? d.fut.ratio : 1, P = d.price * f, n = d.paths.length, N2 = 2 * n;
  const hs = d.paths.map(p => P * (1 + Math.max(...p.map(x => x[0])))).sort((a, b) => b - a);   // highest first
  const ls = d.paths.map(p => P * (1 + Math.min(...p.map(x => x[1])))).sort((a, b) => a - b);   // lowest first
  const bull = hs.map((v, i) => ({ v, o: (i + 1) / N2 })), bear = ls.map((v, i) => ({ v, o: (i + 1) / N2 }));
  const top = hs[0], bot = ls[0], nTop = ls[ls.length - 1], nBot = hs[hs.length - 1];         // normal range: highest low .. lowest high
  const W = 1100, H = 640, T = 30, Bm = 40, gx0 = 150, gx1 = 560, Y = v => T + (top - v) / (top - bot || 1) * (H - T - Bm), X = o => gx0 + (o - 0.01) / 0.49 * (gx1 - gx0);
  const curve = (arr, c) => `<path d="${arr.map((p, i) => `${i ? 'L' : 'M'}${X(p.o).toFixed(1)},${Y(p.v).toFixed(1)}`).join('')}" fill="none" stroke="${c}" stroke-width="1.5"/>`;
  // where the curves cross (TradeWellX's intersection: long-off / short-off area)
  let inter = null; { const ob = v => { const k = hs.filter(h => h >= v).length; return k / N2; }, os = v => ls.filter(l => l <= v).length / N2;
    for (let i = 0; i <= 400; i++) { const v = bot + (top - bot) * i / 400; if (ob(v) <= os(v)) { inter = v; break; } } }
  const yt = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(i => bot + (top - bot) * i / 10).map(v => `<text x="${gx0 - 70}" y="${Y(v) + 3}" font-size="10" text-anchor="end" fill="#8b949e">${sgn((v / P - 1) * 100, 2, '%')}</text><text x="${gx0 + 50}" y="${Y(v) - 3}" font-size="10" fill="#cfd6df">${fmt(v)}</text><line x1="${gx0}" x2="${gx1 + 40}" y1="${Y(v)}" y2="${Y(v)}" stroke="#30363d" stroke-dasharray="2 3" opacity=".6"/>`).join('');
  // stacked price / odds lists (positioned at their price, nudged apart)
  const list = (arr, x, c) => { let last = -99; const out = [];
    const rows = [...arr].sort((a, b) => b.v - a.v);
    rows.forEach((p, i) => { if (Y(p.v) < last + 11 && i !== rows.length - 1 && i !== 0) return;   // too close to the row above: skip (the curve still has it)
      const y = Math.max(Y(p.v), last + 11, 40); last = y;
      out.push(`<line x1="${x - 8}" x2="${x - 2}" y1="${Y(p.v)}" y2="${y - 3}" stroke="${c}" opacity=".5"/><text x="${x}" y="${y}" font-size="10" fill="#e6edf3">${fmt(p.v)}</text><text x="${x + 110}" y="${y}" font-size="10" text-anchor="end" fill="${c}">${(p.o * 100).toFixed(2)}%</text>`); });
    return out.join(''); };
  const bx = 650, sx = 880, uf = d.unfold, real = uf ? [uf.high * f, uf.low * f] : null;
  const arrows = (x) => `<path d="M${x - 5},${Y(P) - 16} l5,14 l5,-14 z" fill="#D0453B"/><path d="M${x - 5},${Y(P) + 16} l5,-14 l5,14 z" fill="#2E9E5B"/>`;
  return `<div class="kv" style="margin-bottom:10px">
      <div><div class="k">Forward looking odds: ${d.fwd} aggregation${d.fwd > 1 ? 's' : ''} · ${d.match === 'weekday' ? 'weekday' : 'HL'} matching</div><div class="v mono">${fmt(P)} · ${esc(d.date)} · state <b>${esc(d.state)}</b>${d.date !== d.to ? ' (replay)' : ''}</div></div>
      <div><div class="k">Normal price range (50% of the time all or part of price is in it)</div><div class="v mono">${fmt(nBot)} – ${fmt(nTop)}</div></div>
      <div><div class="k">Intersection (long-off / short-off)</div><div class="v mono">${inter == null ? '—' : fmt(inter)}</div></div>
      <div><div class="k">Outliers (1%)</div><div class="v mono"><span class="win">${fmt(top)}</span> / <span class="loss">${fmt(bot)}</span></div></div></div>
    <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;background:#151A22;border-radius:6px">${yt}
      <rect x="${gx0}" y="${T}" width="${gx1 + 40 - gx0}" height="${H - T - Bm}" fill="none" stroke="#8b949e"/>
      <line x1="0" x2="${W}" y1="${Y(P)}" y2="${Y(P)}" stroke="#2E9E5B" stroke-dasharray="3 3"/>
      <text x="10" y="${Y(P) - 30}" font-size="11" fill="#cfd6df">last price</text><text x="10" y="${Y(P) - 16}" font-size="11" font-weight="700" fill="#e6edf3">${fmt(P)}</text><text x="10" y="${Y(P) + 14}" font-size="10" fill="#8b949e">start ${esc(d.date)}</text>
      <path d="M${X(0.01)},${Y(top)} L${gx1 - 8},${Y(nTop)} M${X(0.01)},${Y(bot)} L${gx1 - 8},${Y(nBot)}" stroke="#e6edf3" stroke-width="1.2" fill="none"/>
      <line x1="${gx1 - 8}" x2="${gx1 - 8}" y1="${Y(nTop)}" y2="${Y(nBot)}" stroke="#e6edf3" stroke-width="1.4" marker-start="url(#vpa)" marker-end="url(#vpa)"/>
      <defs><marker id="vpa" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#e6edf3"/></marker></defs>
      <text x="${gx1 - 30}" y="${(Y(top) + Y(nTop)) / 2}" font-size="11" text-anchor="end" fill="#cfd6df">Outlier</text><text x="${gx1 - 30}" y="${(Y(bot) + Y(nBot)) / 2}" font-size="11" text-anchor="end" fill="#cfd6df">Outlier</text>
      <text x="${gx1 - 14}" y="${(Y(nTop) + Y(nBot)) / 2}" font-size="11" text-anchor="end" fill="#cfd6df">Normal price range</text>
      ${curve(bull, '#2E9E5B')}${curve(bear, '#D0453B')}
      ${inter == null ? '' : `<line x1="${gx0}" x2="${gx1 + 40}" y1="${Y(inter)}" y2="${Y(inter)}" stroke="#E5C100" stroke-dasharray="6 3"/><text x="${gx0 + 4}" y="${Y(inter) - 3}" font-size="10" fill="#E5C100">intersection ${fmt(inter)}</text>`}
      ${real ? `<rect x="${gx1 + 14}" y="${Y(Math.min(real[0], top))}" width="7" height="${Math.max(2, Y(Math.max(real[1], bot)) - Y(Math.min(real[0], top)))}" fill="#B24BD6"><title>actual: high ${fmt(real[0])}, low ${fmt(real[1])}</title></rect>` : ''}
      <text x="${gx0}" y="${H - Bm + 14}" font-size="10" fill="#8b949e">1%</text><text x="${gx1}" y="${H - Bm + 14}" font-size="10" text-anchor="end" fill="#8b949e">50%</text><text x="${(gx0 + gx1) / 2}" y="${H - Bm + 28}" font-size="11" text-anchor="middle" fill="#cfd6df">frequency of price →</text>
      <text x="${bx}" y="14" font-size="11" font-weight="700" fill="#2E9E5B">Bullish moves</text><text x="${bx}" y="26" font-size="10" fill="#8b949e">price · odds</text>
      <text x="${sx}" y="14" font-size="11" font-weight="700" fill="#D0453B">Bearish moves</text><text x="${sx}" y="26" font-size="10" fill="#8b949e">price · odds</text>
      ${list(bull, bx, '#7ee787')}${list(bear, sx, '#ff9b95')}${arrows(bx + 130)}${arrows(sx + 130)}</svg>
    <div class="note">Odds = share of the ${n} matching days whose ${d.fwd}-day high reached the price (bullish) or whose low reached it (bearish), out of ${N2} moves, as TradeWellX counts them. The best setups are in the 1–2% outlier odds. Purple bar: what actually happened (replay).</div>`;
}
function fxTable() {
  const r = S.fxR, el = $('#fx-cl'); if (!r || !el) return;
  const L = fxLevelsAt(r, r.bars.length - 1) || { bull: [], bear: [], pivot: r.pivot };
  const row = (x, cls) => `<tr><td><span class="pill ${cls}">${Math.round(x.p * 100)}%</span></td><td class="r mono"><b>${fmt(x.price)}</b></td><td class="r mono">${sgn(x.move * 100, 2, '%')}</td></tr>`;
  el.innerHTML = [...L.bull].reverse().map(x => row(x, 'ok')).join('') + `<tr><td><b>Pivot</b></td><td class="r mono"><b>${fmt(r.pivot)}</b></td><td class="r mono">0.00%</td></tr>` + L.bear.map(x => row(x, 'warn')).join('');
}
function fxGammaFor(r) {
  /* Today's gamma in the chart symbol's units: QQQ / SPY / any stock as is, NQ / ES from QQQ / SPY x (futures / ETF). */
  const d = S.gx && S.gx.data; if (!d || !r) return null;
  const sym = r.params.sym, g = d.items.find(x => x.sym === sym || (x.fut && x.fut.sym === sym)); if (!g) return null;
  const f = g.sym === sym ? 1 : g.fut.ratio, conv = v => v == null ? null : (f === 1 ? v : Math.round(v * f * 4) / 4);
  return { g, f, sym, base: g.sym, price: g.price * f, lv: g.sym === sym ? g.uw : g.fut.uw,
    strikes: g.strikes.map(x => ({ ...x, px: x.strike * f })), net: g.netTotal, conv };
}
function fxGammaLadder(r, k, gg, Y, inView, W, padR, GX) {
  /* Left: net gamma by strike (green +, red -, around its axis). Right: open interest by strike (calls green over the
     strike line, puts red under it), all expiries. Same strike rows, so walls and OI clusters line up. */
  if (!gg) return '';
  const x0 = W - padR + 4, half = (GX - 14) / 2, vis = gg.strikes.filter(s => inView(s.px));
  if (!vis.length) return '';
  const hasOi = vis.some(s => s.callOi || s.putOi), wG = hasOi ? half : GX - 10;
  const mx = Math.max(...vis.map(s => Math.abs(s.net))) || 1, mid = x0 + wG / 2, live = k === r.bars.length - 1;
  const ys = vis.map(s => Y(s.px)).sort((a, b) => a - b), gap = ys.length > 1 ? Math.max(1, Math.min(...ys.slice(1).map((y, i) => y - ys[i]).filter(v => v > 0.1)) * 0.75) : 3;
  const bh = Math.max(1, Math.min(6, gap));
  const ox = x0 + half + 8, mo = Math.max(1, ...vis.map(s => Math.max(s.callOi || 0, s.putOi || 0)));
  const oi = !hasOi ? '' : `<line x1="${ox}" x2="${ox}" y1="0" y2="440" stroke="#8b949e" opacity=".35"/>` + vis.map(s => {
    const y = Y(s.px), wc = (s.callOi || 0) / mo * (half - 4), wp = (s.putOi || 0) / mo * (half - 4), hh = Math.max(0.8, bh / 2);
    return `<g><title>${gg.base} ${s.strike}: open interest calls ${(s.callOi || 0).toLocaleString('en-US')}, puts ${(s.putOi || 0).toLocaleString('en-US')}</title>`
      + `<rect x="${ox}" y="${y - hh}" width="${Math.max(0.5, wc)}" height="${hh}" fill="#56d364" opacity=".85"/><rect x="${ox}" y="${y}" width="${Math.max(0.5, wp)}" height="${hh}" fill="#ff7b72" opacity=".85"/></g>`; }).join('');
  return `<g opacity="${live ? 1 : 0.4}"><rect x="${x0 - 2}" y="0" width="${GX - 4}" height="440" fill="#0d1117" opacity=".55"/><line x1="${mid}" x2="${mid}" y1="0" y2="440" stroke="#8b949e" opacity=".35"/>`
    + vis.map(s => { const w = Math.abs(s.net) / mx * (wG / 2); return `<rect x="${s.net >= 0 ? mid : mid - w}" y="${Y(s.px) - bh / 2}" width="${Math.max(0.5, w)}" height="${bh}" fill="${s.net >= 0 ? '#2E9E5B' : '#D0453B'}"><title>${gg.base} ${s.strike}${gg.f !== 1 ? ` (≈ ${fmt(s.px)} ${gg.sym})` : ''}: net gamma ${(s.net / 1e6).toFixed(1)}M per 1% (calls ${(s.call / 1e6).toFixed(1)}M, puts ${(s.put / 1e6).toFixed(1)}M)</title></rect>`; }).join('')
    + oi + `<text x="${mid}" y="12" font-size="10" text-anchor="middle" fill="#8b949e">GEX ${esc(gg.base)}${live ? '' : ' (today)'}</text>`
    + (hasOi ? `<text x="${ox + half / 2}" y="12" font-size="10" text-anchor="middle" fill="#8b949e">OI calls / puts</text>` : '') + '</g>';
}
function fxGammaLines(r, k, Y, inView, W, padR) {
  // today's walls apply to the latest bar only
  const gg = fxGammaFor(r); if (!gg || k !== r.bars.length - 1) return '';
  const lv = gg.lv;
  return [['call_wall', 'Call wall', '#3fb950'], ['put_wall', 'Put wall', '#f85149'], ['gamma_flip', 'Gamma flip', '#d29922'], ['gamma_magnet', 'Magnet', '#e3b8f5']]
    .filter(([key]) => lv[key] != null && inView(lv[key]))
    .map(([key, t, c]) => `<line x1="0" x2="${W - padR}" y1="${Y(lv[key])}" y2="${Y(lv[key])}" stroke="${c}" stroke-width="1.6" stroke-dasharray="10 4" opacity=".9"/><text x="${W - padR - 6}" y="${Y(lv[key]) - 4}" font-size="11" font-weight="600" text-anchor="end" fill="${c}">${t} ${fmt(lv[key])}</text>`).join('');
}
function fxChart(r, k) {
  /* TradeWellX layout: the pivot in force at bar k sits at the vertical centre; the levels are the ones in force then
     (recalculated at each pivot change from the waves finished before it). Bars after k are faded (or hidden). */
  const L0 = fxLayout(r), gxg = L0.gxg, GX = L0.GX, W = L0.W, H = 440, padR = L0.padR, n = r.bars.length, cw = L0.cw, X = L0.X;
  const sx = r.series[k], piv = sx.pivot, nextOn = S.fx.next !== false, impOn = S.fx.imp !== false;
  const G = '#2E7D4F', R = '#B4443C';
  const lvK = fxLevelsAt(r, k), seg = lvK;
  const lv = lvK ? [...lvK.bull.map(x => [x, G]), ...lvK.bear.map(x => [x, R])] : [];
  const shown = nextOn ? n - 1 : k;
  // Fractal Graph (TradeWellX default): every bar is drawn as a % of the pivot in force AT THAT BAR (its PivotHigh /
  // PivotLow / ... columns), so the pivot is always the centre line and every wave is measured the same way; prices on
  // the right are those %s applied to the slider bar's pivot. The half above and the half below are scaled separately.
  // Linear Graph: real prices around the slider bar's pivot, one scale for both halves.
  const fr = !S.fx.linear;
  const rv = (i, p) => p / (fr ? r.series[i].pivot || piv : piv);          // bar i's price as a ratio to its pivot
  let up = 0, dn = 0;
  for (let i = 0; i <= shown; i++) { up = Math.max(up, rv(i, r.bars[i][2]) - 1); dn = Math.max(dn, 1 - rv(i, r.bars[i][3])); }
  lv.forEach(([x]) => { if (x.p >= 0.5) { if (x.move > 0) up = Math.max(up, x.move); else dn = Math.max(dn, -x.move); } });
  up = (up || 0.005) * 1.06; dn = (dn || 0.005) * 1.06;
  if (!fr) up = dn = Math.max(up, dn);
  const hh = H / 2 - 8;
  const YR = q => q >= 1 ? H / 2 - (q - 1) / up * hh : H / 2 + (1 - q) / dn * hh;   // y of a ratio
  const Y = p => YR(p / piv), inView = p => p / piv <= 1 + up && p / piv >= 1 - dn;
  const bw = Math.max(1, cw * 0.6);
  const candles = r.bars.map((b, i) => { if (i > shown) return ''; const up = b[4] >= b[1], c = up ? 'var(--buyT)' : 'var(--sellT)', o = i > k ? ' opacity=".22"' : '';
    const yy = p => YR(rv(i, p));
    return `<g${o}><line x1="${X(i)}" x2="${X(i)}" y1="${yy(b[2])}" y2="${yy(b[3])}" stroke="${c}"/><rect x="${X(i) - bw / 2}" y="${yy(Math.max(b[1], b[4]))}" width="${bw}" height="${Math.max(1, Math.abs(yy(b[1]) - yy(b[4])))}" fill="${c}"/></g>`; }).join('');
  const step = key => { let d = ''; for (let i = 0; i <= k; i++) { const v = r.series[i][key]; if (v == null) continue; const y = YR(rv(i, v)); d += `${d ? 'L' : 'M'}${i * cw},${y}L${(i + 1) * cw},${y}`; } return d; };
  const grp = {};                                       // levels on the same wave share one line: "10% / 5% +1.76% @651.52"
  // TradeWellX places each level by its MOVE: green at +move above the centre line, red at -move below it; the price on
  // the label is that move applied to its base (green: the lower of pivot / vector, red: the higher).
  const LY = x => YR(1 + x.move), inMove = x => 1 + x.move <= 1 + up && 1 + x.move >= 1 - dn;
  lv.filter(([x]) => inMove(x)).forEach(([x, c]) => { const key = c + x.price.toFixed(2); (grp[key] = grp[key] || { x, c, ps: [] }).ps.push(Math.round(x.p * 100) + '%'); });
  const now = r.bars[k][4], pc = (now / piv - 1) * 100;
  const vis = [...Object.values(grp), { x: { price: piv, move: 0 }, pivot: true }].sort((a, b) => LY(a.x) - LY(b.x) || (a.pivot ? -1 : 1));
  // stack the right-hand labels around their lines without overlap (13 px apart), keeping the pivot label on its line
  const pos = vis.map(v => LY(v.x) + 4), pi = vis.findIndex(v => v.pivot);
  for (let i = pi - 1; i >= 0; i--) pos[i] = Math.min(pos[i], pos[i + 1] - 13);
  for (let i = pi + 1; i < pos.length; i++) pos[i] = Math.max(pos[i], pos[i - 1] + 13);
  const bands = vis.map((v, i) => { const t = pos[i];
    if (v.pivot) return `<rect x="${W - padR + GX + 2}" y="${t - 11}" width="${padR - GX - 4}" height="14" fill="#E8E8E8" rx="2"/><text x="${W - padR + GX + 6}" y="${t}" font-size="10.5" fill="#111">Pivot ${fmt(piv)} · close ${sgn(pc, 2, '%')}</text>`;
    const { x, c, ps } = v, y = LY(x);
    const green = c === G, base = green ? lvK.bullBase : lvK.bearBase, baseName = Math.abs(base - piv) < 1e-9 ? 'the pivot' : `the vector ${fmt(base)}`;
    const tip = `<title>${ps.join(' / ')} of past ${green ? 'H' : 'L'} waves reached ${sgn(x.move * 100, 2, '%')} from their pivot; drawn at ${sgn(x.move * 100, 2, '%')} from the centre line; price = ${baseName} ${x.move >= 0 ? '+' : '−'} ${Math.abs(x.move * 100).toFixed(2)}% = ${fmt(x.price)} (${sgn((x.price / piv - 1) * 100, 2, '%')} from the pivot ${fmt(piv)})</title>`;
    return `<g>${tip}<line x1="0" x2="${W - padR}" y1="${y}" y2="${y}" stroke="${c}" stroke-width="7" opacity=".35"/><rect x="${W - padR + GX + 2}" y="${t - 10}" width="${padR - GX - 4}" height="13" fill="${c}" rx="2"/><text x="${W - padR + GX + 6}" y="${t}" font-size="10.5" fill="#fff">${ps.join(' / ')} @${fmt(x.price)} ${sgn(x.move * 100, 2, '%')}</text></g>`; }).join('');
  // open / closed wave lines: each wave extreme up to k, drawn until price came back to it (closed) or up to k (open)
  const idx = {}; r.bars.forEach((b, i) => { idx[b[0]] = i; });
  let imp = '';
  const openTags = [];
  const ws = r.waves.filter(w => idx[w.extreme] != null && idx[w.extreme] < k && idx[w.end] < k);
  if (impOn) ws.forEach(w => {
    const i0 = idx[w.extreme], hiSide = w.side === 'H';
    const q0 = rv(i0, w.price), px = piv * q0;          // fractal: the wave's move applied to the slider bar's pivot
    let j = i0 + 1; while (j <= k && !(hiSide ? rv(j, r.bars[j][2]) >= q0 : rv(j, r.bars[j][3]) <= q0)) j++;
    const open = j > k, x2 = X(Math.min(j, k)), y = YR(q0);
    if (!inView(px)) return;
    const col = open ? '#FF3B30' : (hiSide ? '#C8781E' : '#7a2a26');
    imp += `<line x1="${X(i0)}" x2="${x2}" y1="${y}" y2="${y}" stroke="${col}" stroke-width="${open ? 3 : 2}" stroke-dasharray="3 2" opacity="${open ? .95 : .6}"><title>${w.side}${w.count} extreme ${fmt(w.price)} (${sgn((q0 - 1) * 100, 2, '%')} from its pivot ${fmt(w.pivot)}) · ${open ? 'open' : `closed after ${j - i0} bars`}</title></line>`
      ;
    // the wave's real extreme first; in the fractal graph the line sits at its move from its own pivot, so the price at
    // today's pivot is shown too when it differs (TradeWellX labels that one)
    if (open) openTags.push([y, `${fmt(w.price)} ${hiSide ? 'high' : 'low'}${Math.abs(px - w.price) > 0.005 ? ` (≡ ${fmt(px)} at this pivot)` : ''} open [${k - i0}]`]);
  });
  openTags.sort((a, b) => a[0] - b[0]);
  let oy = -99;
  openTags.forEach(([y, t]) => { const ty = Math.max(y - 3, oy + 12); oy = ty; imp += `<text x="${X(k) - 4}" y="${ty}" font-size="10" text-anchor="end" fill="#FF6B60">${t}</text>`; });
  // Long / short: the close crosses the purple vector while on the far side of the pivot (TradeWellX "Long ... target")
  let sig = '', lastSig = null;
  for (let i = 1; i <= k; i++) {
    const a0 = r.series[i - 1], a1 = r.series[i], c0 = r.bars[i - 1][4], c1 = r.bars[i][4];
    if (a0.ma == null || a1.ma == null || !a1.pivot) continue;
    const longS = c0 <= a0.ma && c1 > a1.ma && c1 < a1.pivot, shortS = c0 >= a0.ma && c1 < a1.ma && c1 > a1.pivot;
    if (!longS && !shortS) continue;
    const y = YR(rv(i, c1));
    sig += longS ? `<path d="M${X(i)},${y + 14} l-5,8 h10 z" fill="#3fb950"><title>${esc(r.bars[i][0])} long: close ${fmt(c1)} crossed above the vector ${fmt(a1.ma)} below the pivot ${fmt(a1.pivot)} (target: the pivot)</title></path>`
                 : `<path d="M${X(i)},${y - 14} l-5,-8 h10 z" fill="#f85149"><title>${esc(r.bars[i][0])} short: close ${fmt(c1)} crossed below the vector ${fmt(a1.ma)} above the pivot ${fmt(a1.pivot)} (target: the pivot)</title></path>`;
    lastSig = { i, long: longS, c: c1, piv: a1.pivot };
  }
  if (lastSig && k - lastSig.i <= 2 * r.params.wave && (lastSig.long ? r.bars[k][4] < sx.pivot : r.bars[k][4] > sx.pivot)) {
    const y0 = YR(rv(lastSig.i, lastSig.c)), y1 = YR(1);
    sig += `<line x1="${X(lastSig.i)}" x2="${X(lastSig.i)}" y1="${y0}" y2="${y1}" stroke="${lastSig.long ? '#3fb950' : '#f85149'}" stroke-width="1.5" stroke-dasharray="3 3"/><text x="${X(lastSig.i) + 5}" y="${(y0 + y1) / 2}" font-size="10.5" fill="${lastSig.long ? '#3fb950' : '#f85149'}">${lastSig.long ? 'Long' : 'Short'} → target pivot ${fmt(sx.pivot)} (${sgn((sx.pivot / r.bars[k][4] - 1) * 100, 2, '%')})</text>`;
  }
  const labels = r.series.map((x, i) => i > k || !x.peakToTrough ? '' : x.peakToTrough > 0
    ? `<text x="${X(i)}" y="${YR(rv(i, r.bars[i][2])) - 6}" font-size="10" text-anchor="middle" fill="var(--text)">${x.peakToTrough}H</text>`
    : `<text x="${X(i)}" y="${YR(rv(i, r.bars[i][3])) + 13}" font-size="10" text-anchor="middle" fill="var(--text)">${-x.peakToTrough}L</text>`).join('');
  const days = r.bars.map((b, i) => i && b[0].slice(0, 10) !== r.bars[i - 1][0].slice(0, 10) ? `<line x1="${i * cw}" x2="${i * cw}" y1="0" y2="${H}" stroke="#E5C100" opacity=".35"/>` : '').join('');
  // above / below pivot statistics from the waves finished by k (TradeWellX boxes)
  const avg = a => a.length ? Math.round(a.reduce((x, y) => x + y, 0) / a.length) : 0;
  const hb = ws.filter(w => w.side === 'H').map(w => w.bars), lb = ws.filter(w => w.side === 'L').map(w => w.bars);
  const box = (x, y, lines) => `<rect x="${x}" y="${y}" width="250" height="${14 * lines.length + 6}" fill="#0d1117" opacity=".85" rx="3"/>` + lines.map((t, i) => `<text x="${x + 6}" y="${y + 15 + i * 14}" font-size="10.5" fill="#cfd6df">${t}</text>`).join('');
  const nice = m => [0.25, 0.5, 1, 2, 3, 5, 10, 15, 20].find(v => v >= m / 3) || 25;
  const ticks = [];
  [[1, up], [-1, dn]].forEach(([sgnv, ext]) => { const st = nice(ext * 100); for (let q = st; q <= ext * 100; q += st) ticks.push(sgnv * q); });
  const axis = ticks.map(q => { const y = Y(piv * (1 + q / 100)); return `<line x1="${W - padR - 34}" x2="${W - padR}" y1="${y}" y2="${y}" stroke="#8b949e" opacity=".5"/><text x="${W - padR - 36}" y="${y + 3}" font-size="10" text-anchor="end" fill="#8b949e">${q > 0 ? '+' : ''}${+q.toFixed(2)}% ${fmt(piv * (1 + q / 100))}</text>`; }).join('');
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;background:#151A22;border-radius:6px">${days}${axis}${bands}${imp}${candles}
    <line x1="0" x2="${W - padR}" y1="${H / 2}" y2="${H / 2}" stroke="#E8E8E8" stroke-width="1.6"/>
    <path d="${step('ma')}" fill="none" stroke="#B24BD6" stroke-width="2"/>${fxOn('lbl') ? labels : ''}${sig}${fxOn('glines') ? fxGammaLines(r, k, Y, inView, W, padR) : ''}${GX ? fxGammaLadder(r, k, gxg, Y, inView, W, padR, GX) : ''}
    ${lvK && Math.abs(lvK.ma - piv) > 1e-9 && inView(lvK.ma) ? `<line x1="${X(k)}" x2="${W - padR}" y1="${Y(lvK.ma)}" y2="${Y(lvK.ma)}" stroke="#B24BD6" stroke-width="1.5" stroke-dasharray="5 3"/><text x="${W - padR - 4}" y="${Y(lvK.ma) - 4}" font-size="10" text-anchor="end" fill="#D58BEF">vector ${fmt(lvK.ma)} (${lvK.ma < piv ? 'green' : 'red'} level prices start here)</text>` : ''}
    <line x1="${X(k)}" x2="${X(k)}" y1="0" y2="${H}" stroke="#8ab4ff" stroke-dasharray="4 3" opacity=".7"/>${fxTrades(r, k, X, YR, rv, shown)}
    ${box(6, 6, [`Above pivot: average ${avg(hb)} bars per wave (${hb.length} waves)`, `Longest above: ${hb.length ? Math.max(...hb) : 0} bars`])}
    ${box(6, H - 54, [`Below pivot: average ${avg(lb)} bars per wave (${lb.length} waves)`, `Longest below: ${lb.length ? Math.max(...lb) : 0} bars`, `Scale: +${(up * 100).toFixed(2)}% / −${(dn * 100).toFixed(2)}% around the pivot${fr ? ' (fractal: each bar vs its own pivot)' : ' (linear)'}`])}
    ${seg ? '' : `<text x="${(W - padR) / 2}" y="40" font-size="12" text-anchor="middle" fill="#cfd6df">Levels start once 2 H and 2 L waves have finished</text>`}</svg>`;
}

/* ---------- Data (Databento) ---------- */
async function vData() {
  const d = await api('/data');
  S.data = d; S.me.databento = d.databento; feedPill();
  const db = d.databento, cv = d.coverage, job = d.job || {};
  const t = today();
  view(`${head('Data', 'CME Globex through Databento: NQ and ES continuous front month (rolled on volume).')}
    <div class="row">
      <div class="col-main">
        <section class="panel"><div class="hd"><h2>Databento</h2><div class="grow"></div>${db.connected ? '<span class="pill ok">Connected</span>' : '<span class="pill warn">Not connected</span>'}</div>
          <div class="bd">${db.connected ? `
            <div class="kv" style="margin-bottom:14px"><div><div class="k">API key</div><div class="v">db-••••${esc(db.last4)}</div></div><div><div class="k">Dataset</div><div class="v">${esc(db.dataset)}</div></div>
              <div><div class="k">Data available to</div><div class="v">${db.range && db.range.end ? esc(db.range.end.replace('T', ' ')) + ' UTC' : '—'}</div></div><div><div class="k">Checked</div><div class="v">${esc((db.testedAt || db.savedAt || '').replace('T', ' ').slice(0, 16))}</div></div></div>
            <div class="form" style="align-items:end">
              <label class="check"><input type="checkbox" id="db-auto" ${db.auto ? 'checked' : ''}> Update candles every 5 min (whenever the market is open)</label>
              <label class="check"><input type="checkbox" id="db-autofp" ${db.autoFp ? 'checked' : ''}> Download each day's trades after the close</label>
              <label class="f">Ask me before a download over<input type="number" id="db-cap" min="0" step="0.5" value="${db.maxCost}"></label>
              <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" data-act="db-save">Save</button><button class="btn" data-act="db-test">Test</button><button class="btn danger" data-act="db-del">Disconnect</button></div>
            </div>
            ${d.refresh && d.refresh.at ? `<div class="${d.refresh.status === 'error' ? 'job error' : 'note'}" style="margin-top:12px">Last automatic update ${esc(d.refresh.at.replace('T', ' ').slice(0, 16))} ET: ${esc(d.refresh.status === 'error' ? 'error: ' + (d.refresh.msg || '') : (d.refresh.msg || d.refresh.status || ''))}${d.refresh.last ? ' · candles to ' + Object.entries(d.refresh.last).map(([s, t]) => `${s} ${t ? esc(t.replace('T', ' ')) : '—'}`).join(' · ') + ' ET' : ''}${d.refresh.schemas ? ' · Databento has ' + Object.entries(d.refresh.schemas).map(([k, t]) => `${esc(k)} to ${esc(String(t).replace('T', ' ').slice(0, 16))}`).join(', ') + ' UTC' : ''}</div>` : '<div class="note" style="margin-top:12px">No automatic update recorded yet.</div>'}
            <div class="sep"></div><div class="note">Replace the key: <input type="password" id="db-key" placeholder="db-…" autocomplete="off" style="width:280px"> <button class="btn sm" data-act="db-connect">Save key</button></div>` : `
            <p class="muted" style="margin-top:0">Paste your Databento API key. It is checked against Databento, then stored encrypted (AWS KMS); the browser never sees it again.</p>
            <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><input type="password" id="db-key" placeholder="db-…" autocomplete="off" style="flex:1 1 320px;max-width:460px">
              <button class="btn primary" data-act="db-connect">Connect</button></div>
            <p class="note">Keys: <a href="https://databento.com/portal/keys" target="_blank" rel="noopener">databento.com/portal/keys</a>. The key needs access to the CME Globex dataset (GLBX.MDP3). Downloads are billed by Databento; every download shows its cost first.</p>`}
          </div></section>

        <section class="panel"><div class="hd"><h2>Unusual Whales</h2><span class="dim" style="font-size:13px">Gamma (GEX), call / put walls for the Fractal page</span><div class="grow"></div>${d.uw && d.uw.connected ? '<span class="pill ok">Connected</span>' : '<span class="pill warn">Not connected</span>'}</div>
          <div class="bd">${d.uw && d.uw.connected ? `
            <div class="kv" style="margin-bottom:14px"><div><div class="k">Token</div><div class="v">••••${esc(d.uw.last4)}</div></div><div><div class="k">Checked</div><div class="v">${esc((d.uw.testedAt || '').replace('T', ' ').slice(0, 16))}</div></div><div></div><div></div></div>
            <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" data-act="uw-test">Test</button><button class="btn danger" data-act="uw-del">Disconnect</button></div>` : `
            <p class="muted" style="margin-top:0">Paste your Unusual Whales API token (unusualwhales.com/dashboard/api). It is checked against Unusual Whales, then stored encrypted (AWS KMS).</p>
            <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><input type="password" id="uw-token" placeholder="API token" autocomplete="off" style="flex:1 1 300px;max-width:420px"><button class="btn primary" data-act="uw-connect">Connect</button></div>
            <p class="note">API access is usually a separate plan from the website membership.</p>`}
          </div></section>

        <section class="panel"><div class="hd"><h2>Alpaca</h2><span class="dim" style="font-size:13px">Stock / ETF bars (QQQ) for the Fractal page</span><div class="grow"></div>${d.alpaca && d.alpaca.connected ? '<span class="pill ok">Connected</span>' : '<span class="pill warn">Not connected</span>'}</div>
          <div class="bd">${d.alpaca && d.alpaca.connected ? `
            <div class="kv" style="margin-bottom:14px"><div><div class="k">Key ID</div><div class="v">••••${esc(d.alpaca.last4)}</div></div><div><div class="k">Feed</div><div class="v">${esc(d.alpaca.feed.toUpperCase())}</div></div><div><div class="k">Checked</div><div class="v">${esc((d.alpaca.testedAt || '').replace('T', ' ').slice(0, 16))}</div></div><div></div></div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><label class="f">Feed<select id="alp-feed2"><option value="sip" ${d.alpaca.feed === 'sip' ? 'selected' : ''}>SIP (all exchanges, 15 min delay on the free plan)</option><option value="iex" ${d.alpaca.feed === 'iex' ? 'selected' : ''}>IEX (real time, one exchange)</option></select></label>
              <button class="btn" data-act="alp-save">Save</button><button class="btn" data-act="alp-test">Test</button><button class="btn danger" data-act="alp-del">Disconnect</button></div>` : `
            <p class="muted" style="margin-top:0">Paste your Alpaca API key ID and secret. They are checked against Alpaca, then stored encrypted (AWS KMS); the browser never sees them again.</p>
            <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><input type="password" id="alp-id" placeholder="Key ID (PK…)" autocomplete="off" style="flex:1 1 200px;max-width:260px"><input type="password" id="alp-secret" placeholder="Secret key" autocomplete="off" style="flex:1 1 260px;max-width:360px">
              <select id="alp-feed"><option value="sip">SIP</option><option value="iex">IEX</option></select><button class="btn primary" data-act="alp-connect">Connect</button></div>
            <p class="note">Keys: Alpaca dashboard → API keys (paper or live keys both work for market data). SIP matches TradeWellX's prices; on the free plan it stops 15 minutes before now.</p>`}
          </div></section>

        <section class="panel"><div class="hd"><h2>Download 1-minute candles</h2><span class="dim" style="font-size:13px">Charts, studies, validation and paper trading</span></div>
          <div class="bd"><div class="form" style="align-items:end">
            <div class="f" style="font-size:13px;color:var(--muted)">Contracts<div class="seg">${['NQ', 'ES'].map(s => `<button data-symc="${s}" aria-pressed="${(S.dlSyms || ['NQ', 'ES']).includes(s)}">${s}</button>`).join('')}</div></div>
            <label class="f">From<input type="date" id="dl-from" value="${S.dlFrom || addDays(t, -730)}"></label>
            <label class="f">To<input type="date" id="dl-to" value="${t}"></label>
            <div class="f" style="font-size:13px;color:var(--muted)">Quick<div class="seg">${[['6m', -182], ['1y', -365], ['2y', -730], ['5y', -1826]].map(([l, n]) => `<button data-dlq="${n}">${l}</button>`).join('')}</div></div>
          </div>
          <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center"><button class="btn" data-act="est-c" ${db.connected ? '' : 'disabled'}>Estimate cost</button><button class="btn primary" data-act="dl-c" ${db.connected ? '' : 'disabled'}>Download</button><span id="est-c" class="muted"></span></div>
          </div></section>

        <section class="panel"><div class="hd"><h2>Download trades (footprint)</h2><span class="dim" style="font-size:13px">Order flow: footprint, real delta, tape and replay. Regular hours, up to 3 months at a time.</span></div>
          <div class="bd"><div class="form" style="align-items:end">
            <div class="f" style="font-size:13px;color:var(--muted)">Contracts<div class="seg">${['NQ', 'ES'].map(s => `<button data-symf="${s}" aria-pressed="${(S.fpSyms || ['NQ']).includes(s)}">${s}</button>`).join('')}</div></div>
            <label class="f">From<input type="date" id="fp-from" value="${addDays(t, -7)}"></label>
            <label class="f">To<input type="date" id="fp-to" value="${t}"></label>
          </div>
          <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center"><button class="btn" data-act="est-f" ${db.connected ? '' : 'disabled'}>Estimate cost</button><button class="btn primary" data-act="dl-f" ${db.connected ? '' : 'disabled'}>Download</button><span id="est-f" class="muted"></span></div>
          <p class="note">Not needed when you download the order book below: it contains the trades too.</p>
          </div></section>

        <section class="panel"><div class="hd"><h2>Download the order book (MBP-10)</h2><span class="dim" style="font-size:12px">Bookmap-style heatmap, real DOM, and the footprint / tape from the same data. Up to 22 sessions at a time.</span></div>
          <div class="bd"><div class="form" style="align-items:end">
            <div class="f" style="font-size:13px;color:var(--muted)">Contracts<div class="seg">${['NQ', 'ES'].map(s => `<button data-symb="${s}" aria-pressed="${(S.bkSyms || ['NQ']).includes(s)}">${s}</button>`).join('')}</div></div>
            <label class="f">From<input type="date" id="bk-from" value="${addDays(t, -1)}"></label>
            <label class="f">To<input type="date" id="bk-to" value="${t}"></label>
            <label class="f">Hours (ET)<select id="bk-win"><option value="rth">09:30–16:00</option><option value="am">09:30–11:30</option><option value="open">09:30–10:30</option></select></label>
          </div>
          <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center"><button class="btn" data-act="est-b" ${db.connected ? '' : 'disabled'}>Estimate cost</button><button class="btn primary" data-act="dl-b" ${db.connected ? '' : 'disabled'}>Download</button><span id="est-b" class="muted"></span></div>
          <p class="note">MBP-10 is the 10 best price levels on each side after every change (about 2.5 points of depth on NQ / ES). It is a large download (GBs per day) but priced low per GB: check the estimate first.</p>
          </div></section>
      </div>
      <aside class="col-side">
        <section class="panel"><div class="hd"><h2>Stored</h2><div class="grow"></div><button class="btn sm" data-act="refresh" ${db.connected ? '' : 'disabled'}>Update now</button></div>
          ${Object.entries(cv).map(([s, c]) => `<div class="bd" style="border-top:1px solid var(--row)"><div style="display:flex;align-items:baseline;gap:8px;margin-bottom:8px"><b>${s}</b><span class="dim" style="font-size:12px">${esc(c.name)}</span></div>
            <div class="kv kv3"><div><div class="k">Candles</div><div class="v">${c.days ? c.days + ' days' : '—'}</div></div><div><div class="k">From</div><div class="v">${esc(c.from || '—')}</div></div><div><div class="k">To</div><div class="v">${esc((c.to || '—').replace('T', ' '))}</div></div>
            <div><div class="k">Footprint</div><div class="v">${c.fpDays ? c.fpDays + ' days' : '—'}</div></div><div><div class="k">From</div><div class="v">${esc(c.fpFrom || '—')}</div></div><div><div class="k">To</div><div class="v">${esc(c.fpTo || '—')}</div></div>
            <div style="grid-column:1/-1"><div class="k">Order book</div><div class="v">${c.hmDays ? c.hmDays + ' days · ' + esc((c.hmLast || []).slice(-4).join(', ')) : '—'}</div></div></div></div>`).join('')}
        </section>
        <div id="job">${jobHtml(job)}</div>
      </aside>
    </div>`);
  if (job.status === 'running') pollJob('/data', j => j.job, () => vData());
}
function jobHtml(j) {
  if (!j || !j.status) return '';
  return `<div class="job ${j.status === 'error' ? 'error' : ''}">${j.status === 'running' ? '<span class="spin"></span>' : ''}<span>${esc(j.msg || j.status)}</span><span class="dim" style="margin-left:auto;font-size:12px">${esc((j.at || '').slice(11, 16))}</span></div>`;
}
function pollJob(path, pick, done) {
  const h = setInterval(async () => {
    try { const r = await api(path); const j = pick(r); const el = $('#job'); if (el) el.innerHTML = jobHtml(j); if (j.status !== 'running') { clearInterval(h); done(r); } } catch (e) { clearInterval(h); }
  }, 2500);
  S.timers.push(h);
}
function dlBody(kind) {
  if (kind === 'book') return { kind, syms: S.bkSyms || ['NQ'], from: $('#bk-from').value, to: $('#bk-to').value, window: $('#bk-win').value };
  const isC = kind === 'candles';
  return { kind, syms: isC ? (S.dlSyms || ['NQ', 'ES']) : (S.fpSyms || ['NQ']), from: $(isC ? '#dl-from' : '#fp-from').value, to: $(isC ? '#dl-to' : '#fp-to').value };
}
async function download(kind, confirm) {
  const body = { ...dlBody(kind), confirm };
  const r = await api('/data/download', { method: 'POST', body });
  if (r.needConfirm) {
    if (window.confirm(`Databento estimates $${r.usd.toFixed(2)} for this download (your limit is $${r.cap}). Download anyway?`)) return download(kind, true);
    return;
  }
  toast(`Download started · estimated $${(r.usd || 0).toFixed(2)}`);
  vData();
}

/* ---------- Research ---------- */
async function vResearch() {
  const r = await api('/strategies');
  S.strats = r.strategies;
  const cov = r.coverage, f = S.filter;
  const rows = r.strategies.filter(s => f === 'All' || STAGES[s.stage] === f).sort((a, b) => (b.created || '').localeCompare(a.created || ''));
  const m = s => (s.valid && s.valid.test) || (s.study && s.study.m) || {};
  view(`${head('Research', 'Every strategy goes through the same protocol: data → study → validation → paper → live.', `<button class="btn primary" data-act="new">New strategy</button>`)}
    <section class="panel" style="margin-bottom:16px"><div class="bd" style="display:flex;flex-wrap:wrap;gap:20px;align-items:center">
      ${Object.entries(cov).map(([s, c]) => `<div><b>${s}</b> <span class="dim">${c.days ? `${c.days} sessions · ${c.from} → ${(c.to || '').slice(0, 10)}${c.fpDays ? ` · footprint ${c.fpDays} days` : ''}` : 'no candles yet'}</span></div>`).join('')}
      <div style="flex:1"></div><a class="btn sm" href="#data">Data</a></div></section>
    <div id="newbox">${S.newSetup !== null ? newForm() : ''}</div>
    <section class="panel"><div class="hd"><h2>Strategies</h2><div class="seg" role="group" aria-label="Stage">${['All', ...Object.values(STAGES)].map(l => `<button data-filter="${l}" aria-pressed="${l === f}">${l}</button>`).join('')}</div></div>
      ${rows.length ? `<div class="tbl-wrap"><table class="tbl" style="min-width:860px"><thead><tr><th>Strategy</th><th>Market</th><th>Stage</th><th class="r">Trades</th><th class="r">Avg R</th><th class="r">Edge vs random</th><th class="r">PF</th><th class="r">$ / trade</th><th>Next step</th></tr></thead><tbody>
        ${rows.map(s => { const x = m(s); return `<tr><td><a class="name" href="#strategy/${s.id}">${esc(s.name)}</a><div class="sub">${esc(s.describe)}</div></td><td>${s.params.tickers.join(', ')}</td>
          <td><span class="stage st-${s.stage}">${STAGES[s.stage] || s.stage}</span></td><td class="r">${x.n ?? '—'}</td><td class="r ${cls(x.avgR)}">${x.avgR != null ? sgn(x.avgR, 2, 'R') : '—'}</td>
          <td class="r ${cls(x.edge)}">${x.edge != null ? sgn(x.edge, 2, ' pts') : '—'}</td><td class="r">${x.pf ?? '—'}</td><td class="r ${cls(x.exp)}">${x.exp != null ? money(x.exp) : '—'}</td><td class="muted">${nextStep(s)}</td></tr>`; }).join('')}
      </tbody></table></div>` : `<div class="empty">${r.strategies.length ? 'None at this stage.' : 'No strategies yet. Start one with <b>New strategy</b>.'}</div>`}
    </section>`);
}
function nextStep(s) {
  if (s.stage === 'idea') return 'Run the study';
  if (s.stage === 'study') return s.valid ? 'Failed a gate: change the rules' : 'Run the validation';
  if (s.stage === 'validated') return (s.paper || {}).kill ? 'Stopped by the kill switch' : 'Start paper trading';
  if (s.stage === 'paper') return `${(s.paper || {}).n || 0} paper trades`;
  return '';
}
function paramForm(setup, p, locked) {
  return `<div class="form">${SETUPS[setup].params.map(([k, lab, type, opt]) => {
    const v = p[k], dis = locked ? 'disabled' : '';
    if (type === 'sel') return `<label class="f">${lab}<select data-p="${k}" ${dis}>${opt.map(([ov, ol]) => `<option value="${ov}" ${String(ov) === String(v) ? 'selected' : ''}>${ol}</option>`).join('')}</select></label>`;
    if (type === 'bool') return `<label class="check" style="align-self:end;min-height:36px"><input type="checkbox" data-p="${k}" ${v ? 'checked' : ''} ${dis}> ${lab}</label>`;
    if (type === 'tick') return `<div class="f" style="font-size:13px;color:var(--muted)">${FX_STOCK.includes(setup) ? 'Contracts / stocks (NQ, ES or QQQ, SPY: not mixed)' : lab}<div class="seg">${(FX_STOCK.includes(setup) ? ['NQ', 'ES', 'QQQ', 'SPY'] : ['NQ', 'ES']).map(s => `<button type="button" data-ptick="${s}" aria-pressed="${(v || ['NQ']).includes(s)}" ${dis}>${s}</button>`).join('')}</div></div>`;
    return `<label class="f">${lab}<input type="${type === 'num' ? 'number' : 'text'}" ${type === 'num' ? `step="${opt}"` : ''} data-p="${k}" value="${esc(v ?? '')}" ${dis}></label>`;
  }).join('')}</div>`;
}
function readParams(root) {
  const p = {};
  root.querySelectorAll('[data-p]').forEach(el => { p[el.dataset.p] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? +el.value : el.value; });
  const t = [...root.querySelectorAll('[data-ptick][aria-pressed="true"]')].map(b => b.dataset.ptick);
  if (root.querySelector('[data-ptick]')) p.tickers = t.length ? t : ['NQ'];
  return p;
}
const DEF = { orb: { rangeMin: 5, tgtR: 3, fvgWin: 5, confWin: 10, lastEntry: '11:30', side: 'both', days: 250, tickers: ['NQ'] },
  orbema: { tf: 5, rangeMin: 15, ema: 20, emaOn: 'full', entryAt: 'close', bufTicks: 1, minRR: 1, lastEntry: '12:00', side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  range50: { tf: 1, legPct: 0.35, entryLvl: 0.25, targetLvl: 0.5, stopBuf: 0, side: 'both', days: 250, tickers: ['NQ'] },
  silver: { tf: 1, pivLen: 3, expiry: 30, rr: 2, bufTicks: 2, flatEod: true, side: 'both', days: 250, tickers: ['NQ'] },
  tvpoc: { tf: '15', entry: 'flip', maType: 'SMA', maLen: 25, atrLen: 50, atrMult: 1, binMult: 0.2, minBars: 3, stop: 'band', flatEod: true, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  vwapret: { tf: 5, hours: 'all', trend: 3, wait: 3, tgtR: 2, bufTicks: 2, maxDay: 10, flatAt: '15:55', side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  onsweep: { tf: 5, lastEntry: '10:30', maxAbove: 3, minDepth: 0, minOnPct: 0, tgt: 'vwap', tgtR: 2, minRR: 1, bufTicks: 2, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  ibbreak: { tf: 5, ibMin: 60, filter: 'vwap', onMinPct: 0.3, stop: 'mid', tgtMult: 1, bufTicks: 2, lastEntry: '14:00', side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  odpull: { tf: 5, driveMin: 15, drivePct: 0.25, strength: 0.25, pbMin: 2, ema: 9, tgtR: 2, bufTicks: 2, lastEntry: '11:30', side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  pdbreak: { tf: 5, brkTf: 15, bias: 'close', closePct: 0.33, wait: 12, tolTicks: 4, tgt: 'atr', tgtAtr: 1, tgtR: 2, minRR: 1, bufTicks: 2, lastEntry: '12:00', side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  pdsweep: { tf: 5, levels: 'pd', fresh: true, lastEntry: '11:30', maxAbove: 3, minDepth: 0, tgt: 'vwap', tgtR: 2, minRR: 1, bufTicks: 2, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  nrbreak: { tf: 5, cond: 'nr7', trend: 'sma', orMin: 15, stop: 'opp', tgtAtr: 1, minRR: 0.5, bufTicks: 2, lastEntry: '12:00', side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  fpdisp: { tf: 5, fair: 'open', minAwayPct: 0.1, bodyMult: 1, stop: 'bar', swingN: 5, bufTicks: 2, minRR: 1, firstEntry: '09:35', lastEntry: '13:00', maxDay: 2, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  ifvg: { tf: 5, bias: 'all3', asia: 'none', minGap: 4, fvgMax: 24, gapFrom: '08:00', stop: 'fvg', bufTicks: 0, tgt: 'liq2r', tgtR: 2, pivLen: 3, liqLook: 150, minRR: 1, lastEntry: '14:00', maxDay: 2, hours: 'all', exit: 'trail', beR: 0.5, partR: 1, partPct: 50, trail: 'swing', trailN: 3, trailR: 0.5, trigSize: 'body', trigMin: 0, trigMax: 0, bodyPct: 0, avgN: 20, trigN: 0, trigPrevMult: 1, trigPrevMax: 0, trigEngulf: false, volMin: 0, volMax: 100, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  biaspull: { tf: 5, bias: 'all3', asia: 'none', pbMin: 2, minRisk: 8, bufTicks: 0, hours: 'all', lastEntry: '14:00', maxDay: 2, exit: 'trail', tgtR: 2, beR: 0.5, partR: 1, partPct: 50, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  ondrift: { entryAt: '18:00', exitAt: '09:30', bias: 'all3', stopMult: 8, exit: 'target', tgtR: 0, beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  ldnbrk: { tf: 5, asiaEnd: '02:00', lastEntry: '06:00', flatAt: '09:30', stop: 'mid', tgtMult: 1, minRangePct: 0, maxRangePct: 0, bias: 'all3', bufTicks: 2, exit: 'target', beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  smt: { tradeSym: 'NQ', tf: 5, look: 12, wait: 3, hours: 'all', lastEntry: '15:00', maxDay: 3, tgt: 'vwap', minRR: 1, bias: 'none', bufTicks: 2, exit: 'target', tgtR: 2, beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ'] },
  flatrev: { tf: 5, distMult: 2, bias: 'all3', hours: 'all', lastEntry: '15:00', maxDay: 3, minRR: 1, bufTicks: 2, exit: 'target', beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  postclose: { fromAt: '15:00', minMovePct: 0.3, mode: 'follow', exitAt: '16:55', stopMult: 3, bias: 'none', exit: 'target', tgtR: 0, beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  sungap: { which: 'monday', mode: 'both', smallPct: 0.25, bigPct: 0.5, exitAt: '02:00', stopMult: 4, bias: 'none', exit: 'target', tgtR: 2, beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  insidebar: { tf: 5, entry: 'break', wait: 1, minMother: 0, maxInside: 100, hours: 'rth', lastEntry: '15:00', maxDay: 5, bias: 'none', bufTicks: 0, exit: 'target', tgtR: 2, beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  imom: { from: 'close', minMovePct: 0, entryAt: '15:30', mode: 'follow', stopMult: 0, bias: 'none', exit: 'target', tgtR: 0, beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  onrev: { minPct: 0.6, maxPct: 0, fill: 1, stopGap: 1, exitAt: '11:30', bias: 'none', exit: 'target', beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  gapfill: { minPct: 0.1, maxPct: 0.5, fill: 1, stopGap: 1.5, exitAt: '13:00', bias: 'none', exit: 'target', beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  tom: { entryDay: -1, exitDay: 3, trend: 'none', stopAtr: 0, side: 'long', days: 500, tickers: ['NQ', 'ES'] },
  vwapband: { tf: 5, k: 2, trendMult: 1, minRR: 1, lastEntry: '15:00', maxDay: 3, bias: 'none', bufTicks: 2, exit: 'target', beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  rsi2: { trendLen: 200, lo: 10, hi: 70, maxHold: 10, stopMult: 0, flatEod: true, side: 'long', days: 515, tickers: ['NQ', 'ES'] },
  fractal: { entry: 'cross', tf: 15, wave: 10, stats: 10, hours: 'all', stopP: 0.5, tgtP: 0.5, fadeP: 0.1, oddsMax: 0.2, tgt: 'level', mm: 'none', oaMin: 0, oaBars: 3, htf: 'none', htfWave: 10, htfRule: 'side', smLevel: 'wall', smTgt: 'median', smMatch: 'hl', smDays: 200, lateN: 3, lateStopP: 0.1, oaExit: 0, macro: 'with', sqz: 'any', daily: 'none', lastEntry: '15:00', maxDay: 2, bias: 'none', bufTicks: 2, exit: 'target', tgtR: 0, beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  squeeze: { tf: 5, kc: 1.5, minBars: 6, wait: 6, stop: 'mid', hours: 'rth', lastEntry: '15:00', maxDay: 3, bias: 'none', bufTicks: 0, exit: 'target', tgtR: 2, beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  volclimax: { tf: 5, volMult: 3, wickPct: 40, tgt: 'vwap', minRR: 1, hours: 'rth', lastEntry: '15:00', maxDay: 2, bias: 'none', bufTicks: 2, exit: 'target', tgtR: 2, beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  pwsweep: { tf: 5, lastEntry: '15:00', maxAbove: 3, minDepth: 0, fresh: true, tgt: 'vwap', tgtR: 2, minRR: 1, bufTicks: 2, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  trendday: { rangeMult: 0.6, edgePct: 20, stop: 'mid', bias: 'none', bufTicks: 2, exit: 'target', tgtR: 0, beR: 0, partR: 1, partPct: 0, trail: 'swing', trailN: 3, trailR: 0.5, side: 'both', days: 515, tickers: ['NQ', 'ES'] },
  wt: { tf: '1D', side: 'long', stopAtr: 1.5, flatEod: true, days: 500, tickers: ['NQ'] } };
/* Three strategies on the Fractal engine (backend fxvariants.py): the Fractal form's fields that apply to each. */
(() => {
  const F = SETUPS.fractal.params, pick = ks => ks.map(k => F.find(x => x[0] === k)).filter(Boolean), tail = [...MANAGEP, ...TAILP];
  const common = ['tf', 'wave', 'stats', 'macro', 'sqz', 'daily', 'mm', 'htf', 'htfWave', 'htfRule', 'lastEntry', 'maxDay'];
  SETUPS.smfade = { name: 'Spread Monster fade (daily levels)', blurb: 'Every session: the Spread Monster 1-day projection from the daily bars up to the previous close. A candle reaching the wall (MaxAvg, ~25% odds), the 1% outlier or the 50% median and closing back inside is faded: stop over the session\'s high / low so far, target the 50% median, the previous close or the pivot. Daily bars only: the whole history can be tested.',
    params: [...pick(['smLevel', 'smTgt', 'smMatch', 'smDays', 'tf', 'hours', 'macro', 'sqz', 'mm', 'htf', 'htfWave', 'htfRule', 'lastEntry', 'maxDay', 'bufTicks']), ...tail] };
  SETUPS.oaride = { name: 'Wave ride + Option Analyzer exit', blurb: 'Go with a new wave (the first close across the pivot or the purple vector), stop at the other side\'s cluster level, and get out once the wave has run longer than most past waves on that side did (Option Analyzer odds), unless the target comes first.',
    params: [['entry', 'Entry', 'sel', [['cross', 'Pivot cross: go with the new wave'], ['vector', 'Vector cross: close across the purple vector']]], ...pick(['oaExit', 'stopP', 'tgt', 'tgtP', 'oaMin', 'oaBars', 'hours', ...common]), ...tail] };
  SETUPS.latewave = { name: 'Late counter-wave (higher-timeframe bias)', blurb: 'When a wave against the higher-timeframe fractal (60-minute, 4-hour or daily) has run N candles and a candle turns back, go with the higher timeframe: stop at the 10% cluster level, target the pivot (or a cluster level, the previous wave, the previous session, R).',
    params: [...pick(['htf', 'htfWave', 'htfRule', 'lateN', 'lateStopP', 'tgt', 'tgtP', 'oaExit', 'hours', 'tf', 'wave', 'stats', 'macro', 'sqz', 'mm', 'lastEntry', 'maxDay']), ...tail] };
})();
DEF.smfade = { ...DEF.fractal, entry: 'smfade', tf: 5, macro: 'none', smLevel: 'wall', smTgt: 'median', bufTicks: 4 };
DEF.oaride = { ...DEF.fractal, entry: 'cross', oaExit: 0.3, tgt: 'r', tgtR: 0 };
DEF.latewave = { ...DEF.fractal, entry: 'late', macro: 'none', htf: 'D', htfRule: 'side', lateN: 3, lateStopP: 0.1 };
const FX_STOCK = ['fractal', 'smfade', 'oaride', 'latewave'];   // the setups that also run on QQQ / SPY
// what's been tried for a setup: a badge on its card in New strategy
function tested(key) {
  const xs = (S.strats || []).filter(s => s.setup === key), done = xs.filter(s => s.study && s.study.m);
  if (!xs.length) return '<span class="tbadge new">Not tried yet</span>';
  if (!done.length) return `<span class="tbadge">Created · no study yet</span>`;
  const best = Math.max(...done.map(s => ((s.valid && s.valid.test) || s.study.m).avgR ?? -9));
  const val = xs.some(s => ['validated', 'paper', 'live'].includes(s.stage));
  return `<span class="tbadge ${val ? 'ok' : 'done'}">${val ? 'Validated' : `Tested ×${done.length}`} · best ${best > -9 ? sgn(best, 2, 'R') : '—'}</span>`;
}
function newForm() {
  const k = S.newSetup;
  return `<section class="panel" style="margin-bottom:16px"><div class="hd"><h2>New strategy</h2><div class="grow"></div><button class="btn sm" data-act="new-close">Close</button></div><div class="bd">
    <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-bottom:${k ? 16 : 0}px">${Object.entries(SETUPS).map(([key, s]) => `
      <button class="cell" data-setup="${key}" aria-pressed="${k === key}">${tested(key)}<span style="font-weight:600;font-size:14px">${s.name}</span><span class="note">${s.blurb}</span></button>`).join('')}</div>
    ${k ? `<div id="newp"><label class="f" style="max-width:420px;margin-bottom:12px">Name<input type="text" id="new-name" value="${esc(SETUPS[k].name)}"></label>${paramForm(k, DEF[k])}
      <div style="margin-top:14px"><button class="btn primary" data-act="create">Create</button></div></div>` : ''}
  </div></section>`;
}

/* ---------- Strategy ---------- */
async function vStrategy(id) {
  const s = await api('/strategies/' + id);
  S.cur = s;
  let ts = [];
  if (s.study) { try { ts = (await api(`/strategies/${id}/trades?set=${S.trSet}`)).trades; } catch (e) { ts = []; } }
  let srch = null; try { srch = await api(`/strategies/${id}/search`); } catch (e) { srch = null; }
  S.srch = srch;
  const v = s.valid, st = s.study, pp = s.paper || {}, job = s.job || {};
  const locked = !!pp.on;
  const STK = ['QQQ', 'SPY'], stocks = Object.keys(s.coverage).filter(k => STK.includes(k));
  const dataOk = Object.entries(s.coverage).every(([k, c]) => c.days >= 60 || STK.includes(k));   // QQQ / SPY download from Alpaca when the study runs
  const steps = [
    ['Data', dataOk ? Object.entries(s.coverage).map(([k, c]) => STK.includes(k) && c.days < 60 ? `${k}: downloads from Alpaca when the study runs` : `${k} ${c.days} sessions`).join(' · ') : 'Need 60+ sessions', dataOk ? 'done' : ''],
    ['Study', st ? `${st.n.toLocaleString('en-US')} trades` : 'Not run', st ? 'done' : ''],
    ['Validation', v ? `${v.gates.filter(g => g.ok).length} / ${v.gates.length} gates` : 'Not run', v ? (v.pass ? 'done' : 'fail') : ''],
    ['Paper', pp.on ? `${pp.n || 0} trades` : pp.kill ? 'Stopped' : 'Off', pp.on ? 'run' : ''],
    ['Live', 'Needs a futures broker', ''],
  ];
  const cur = !dataOk ? 0 : !st ? 1 : !(v && v.pass) ? 2 : 3;
  const mrow = (name, k, f) => `<tr><td>${name}</td>${['train', 'test', 'base', 'cost2'].map(w => `<td class="r">${v[w] && v[w][k] != null ? f(v[w][k]) : '—'}</td>`).join('')}</tr>`;
  const grid = v && v.grid;
  view(`<div class="crumb"><a href="#research">Research</a> › ${s.params.tickers.join(', ')} · ${esc(s.setupName)}</div>
    ${head(esc(s.name), esc(s.describe), `<a class="btn" href="#chart" data-act="open-chart">Open on chart</a><button class="btn" data-act="duplicate" title="A copy with the same rules: change it without touching this one">Duplicate</button>${s.stage === 'retired' ? '<button class="btn" data-act="unretire">Bring back</button>' : '<button class="btn" data-act="retire">Retire</button>'}<button class="btn danger" data-act="delete">Delete</button>`)}
    <ol class="steps" aria-label="Validation protocol">${steps.map(([n, sb, stt], i) => `<li class="${i === cur ? 'cur' : ''}"><span class="n"><span class="dot ${stt === 'done' ? 'done' : stt === 'run' ? 'run' : ''}">${stt === 'fail' ? '!' : i + 1}</span>${stt === 'done' ? 'Done' : stt === 'run' ? 'Running' : stt === 'fail' ? 'Failed' : i === cur ? 'Next' : 'Locked'}</span><span class="nm">${n}</span><span class="sb">${esc(sb)}</span></li>`).join('')}</ol>
    <div id="job" style="margin-bottom:12px">${jobHtml(job)}</div>
    <div class="row">
      <div class="col-main">
        <section class="panel"><div class="hd"><h2>${v ? 'Validation' : 'Study'}</h2><span class="dim" style="font-size:13px">${v ? `Train = first 60% of the trades, test = the last 40% (from ${esc(v.split || '')}). Costs: $${s.comm} commission + ${s.slip} tick slippage per side, ${s.contracts} contract(s).` : st ? `${esc(st.from)} → ${esc(st.to)}` : 'Run the study to see the trades this rule would have taken.'}</span>
          <div class="grow"></div><button class="btn sm" data-act="run-study" ${locked || !dataOk ? 'disabled' : ''}>${st ? 'Re-run study' : 'Run study'}</button>${st ? `<button class="btn sm ${v ? '' : 'primary'}" data-act="run-valid" ${locked ? 'disabled' : ''}>${v ? 'Re-validate' : 'Run validation'}</button>` : ''}</div>
          ${v ? `<div class="tbl-wrap"><table class="tbl" style="min-width:600px"><thead><tr><th>Metric</th><th class="r">Train</th><th class="r">Test</th><th class="r">Random entries (test)</th><th class="r">Test at 2× costs</th></tr></thead><tbody>
            ${mrow('Trades', 'n', x => x.toLocaleString('en-US'))}${mrow('Win rate', 'win', x => x + '%')}${mrow('Avg R', 'avgR', x => sgn(x, 2, 'R'))}${mrow('Profit factor', 'pf', x => x)}
            ${mrow('Avg points / trade', 'pts', x => sgn(x, 2))}${mrow('$ / trade (net)', 'exp', x => money(x))}${mrow('Net total', 'total', x => money(x))}${mrow('Max drawdown', 'dd', x => money(-x))}
            ${mrow('Edge vs random (pts)', 'edge', x => sgn(x, 2))}</tbody></table></div>` : st ? `<div class="bd"><div class="kv">${[['Trades', st.m.n], ['Win rate', st.m.win != null ? st.m.win + '%' : '—'], ['Avg R', sgn(st.m.avgR, 2, 'R')], ['Profit factor', st.m.pf ?? '—'],
              ['$ / trade', money(st.m.exp)], ['Net total', money(st.m.total)], ['Max drawdown', money(-st.m.dd)], ['Edge vs random', sgn(st.m.edge, 2, ' pts')]].map(([k, x]) => `<div><div class="k">${k}</div><div class="v big">${x}</div></div>`).join('')}</div></div>` : ''}
          ${ts.length && S.trSet === 'study' ? `<div class="bd" style="border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:24px">
            <div style="flex:2 1 340px;min-width:0"><div class="note" style="margin-bottom:8px">Equity ($, net) · <span class="gain">train</span> then <span class="loss">test</span></div><div class="bars">${equityBars(ts, v && v.split)}</div></div>
            <div style="flex:1 1 220px;min-width:0"><div class="note" style="margin-bottom:8px">R per trade</div><div class="bars" style="gap:3px">${rHist(ts)}</div><div class="note mono" style="display:flex;justify-content:space-between"><span>≤ −1R</span><span>0</span><span>≥ +3R</span></div></div></div>` : ''}
        </section>
        ${srch ? `<div id="srch">${srchHtml(s, srch, locked, dataOk)}</div>` : ''}
        ${grid ? `<section class="panel"><div class="hd"><h2>Where it may trade</h2><span class="dim" style="font-size:13px">Test results by market condition (known before the open). Tick the ones paper trading may use.</span></div>
          <div class="bd tbl-wrap"><div class="cells"><div></div><div class="dim" style="font-size:12px;text-align:center">calm</div><div class="dim" style="font-size:12px;text-align:center">volatile</div>
          ${['Bull', 'Sideways', 'Bear'].map(ph => `<div class="muted" style="display:flex;align-items:center">${ph}</div>${['calm', 'volatile'].map(vo => { const k = ph + '|' + vo, g = grid[k] || {}, on = !!(s.cells || {})[k];
            return `<button class="cell ${S.todayCell === k ? 'today' : ''}" data-cellk="${k}" aria-pressed="${on}" ${locked ? 'disabled' : ''}><span class="ok">${on ? 'Approved' : 'Not approved'}</span><span class="rv ${cls(g.avgR)}">${g.avgR != null ? sgn(g.avgR, 2, 'R') : '—'}</span><span class="note">${g.n || 0} trades · ${money(g.total)}</span></button>`; }).join('')}`).join('')}
          </div></div></section>` : ''}
        ${st && st.factors && Object.keys(st.factors).length ? `<section class="panel"><div class="hd"><h2>What drives it</h2><span class="dim" style="font-size:13px">All study trades, by factor</span></div><div class="bd" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px">
          ${Object.entries(st.factors).map(([lab, rows]) => `<div><div class="note" style="margin-bottom:6px">${esc(lab)}</div><table class="tbl"><tbody>${rows.slice(0, 8).map(r => `<tr><td style="padding:5px 8px">${esc(r.v)}</td><td class="r" style="padding:5px 8px">${r.n}</td><td class="r ${cls(r.avgR)}" style="padding:5px 8px">${r.avgR != null ? sgn(r.avgR, 2, 'R') : '—'}</td><td class="r ${cls(r.exp)}" style="padding:5px 8px">${money(r.exp)}</td></tr>`).join('')}</tbody></table></div>`).join('')}
        </div></section>` : ''}
        ${st ? `<section class="panel"><div class="hd"><h2>Trades</h2><div class="seg"><button data-trset="study" aria-pressed="${S.trSet === 'study'}">Study</button><button data-trset="paper" aria-pressed="${S.trSet === 'paper'}">Paper</button></div><div class="grow"></div><span class="dim" style="font-size:12px">last 200</span></div>
          ${ts.length ? `<div class="tbl-wrap" style="max-height:520px;overflow:auto"><table class="tbl" style="min-width:760px"><thead><tr><th>Entry</th><th>Exit</th><th>Side</th><th class="r">Entry px</th><th class="r">Exit px</th><th class="r">Points</th><th class="r">R</th><th class="r">Net</th><th>Why</th><th>Condition</th></tr></thead><tbody>
            ${ts.slice(-200).reverse().map(t => `<tr><td class="mono">${esc(t.s)} ${esc(t.in.replace('T', ' ').slice(0, 16))}</td><td class="mono">${esc((t.out || '').slice(5, 16).replace('T', ' '))}</td><td>${t.side}</td><td class="r">${fmt(t.e)}</td><td class="r">${fmt(t.x)}</td>
              <td class="r ${cls(t.pts)}">${sgn(t.pts)}</td><td class="r ${cls(t.r)}">${t.r != null ? sgn(t.r, 2) : '—'}</td><td class="r ${cls(t.net)}">${money(t.net)}</td><td class="muted">${esc(t.status === 'open' ? 'open' : t.why || '')}</td><td class="muted">${esc(cellName(t.cell))}</td></tr>`).join('')}
          </tbody></table></div>` : `<div class="empty">${S.trSet === 'paper' ? 'No paper trades yet.' : 'No trades.'}</div>`}</section>` : ''}
      </div>
      <aside class="col-side">
        <section class="panel"><div class="hd"><h2>Rules</h2>${locked ? '<span class="dim" style="font-size:12px">locked while paper trading</span>' : ''}</div><div class="bd" id="rules">
          <div style="display:flex;gap:8px;align-items:end;margin-bottom:12px"><label class="f" style="flex:1">Name<input type="text" id="s-name" value="${esc(s.name)}" maxlength="80"></label><button class="btn sm" data-act="rename">Rename</button></div>
          ${paramForm(s.setup, s.params, locked)}
          ${locked ? '' : `<div style="margin-top:12px;display:flex;gap:8px;align-items:center"><button class="btn sm" data-act="save-rules">Save rules</button><span class="note">Saving clears the study and validation.</span></div>`}${s.setup === 'ifvg' ? `<div style="margin-top:12px;border-top:1px solid var(--line);padding-top:12px;display:flex;flex-direction:column;gap:6px"><button class="btn sm" data-act="make-control">Make the no-FVG control with these settings</button><span class="note">Same bias, hours and exit, entry on a plain pullback. Run both studies: if the control does as well, the FVG adds nothing.</span></div>` : ''}</div></section>
        ${v ? `<section class="panel"><div class="hd"><h2>Validation gates</h2><div class="grow"></div><span class="${v.pass ? 'gain' : 'loss'}" style="font-weight:600;font-size:13px">${v.gates.filter(g => g.ok).length} / ${v.gates.length} passed</span></div><div class="bd" style="padding-top:4px">
          ${v.gates.map(g => `<div class="gate"><span style="color:${g.ok ? 'var(--buyT)' : 'var(--sellT)'};font-weight:700">${g.ok ? '✓' : '✕'}</span><span class="g">${esc(g.t)}</span><span class="v">${esc(g.v)}</span></div>`).join('')}
          ${(v.info || []).map(g => `<div class="gate" style="opacity:.75"><span class="dim" style="font-weight:700">i</span><span class="g">${esc(g.t)} <span class="dim">(not a gate)</span></span><span class="v ${g.ok ? 'gain' : 'loss'}">${esc(g.v)}</span></div>`).join('')}
          ${(v.info || []).length ? `<label class="f" style="margin-top:10px">Random-entry gate tests<select id="edge-test" ${locked ? 'disabled' : ''}><option value="timing" ${v.edgeTest !== 'rule' ? 'selected' : ''}>The entry timing (random entries on the same side)</option><option value="rule" ${v.edgeTest === 'rule' ? 'selected' : ''}>The whole rule (random entries on a random side)</option></select></label>
            <div class="note" style="margin-top:4px">Same side: does the entry beat entering at a random time in the bias direction? Random side: does the rule (bias + entry + exit) beat random trades with the same exit? Changing it re-runs the validation.</div>` : ''}</div></section>` : ''}
        <section class="panel"><div class="hd"><h2>Paper trading</h2></div><div class="bd" style="display:flex;flex-direction:column;gap:12px">
          ${pp.on || pp.n ? `<div class="kv kv3"><div><div class="k">Trades</div><div class="big">${pp.n || 0}</div></div><div><div class="k">Avg R</div><div class="big ${cls(pp.avgR)}">${pp.avgR != null ? sgn(pp.avgR, 2) : '—'}</div></div><div><div class="k">Net</div><div class="big ${cls(pp.net)}">${money(pp.net)}</div></div></div>${zbar(pp.z)}` : ''}
          ${pp.kill ? `<div class="job error">Kill switch: ${esc(pp.kill)}</div>` : ''}
          <div class="note">Simulated on the stored Databento candles (refreshed every 5 minutes, overnight included), only in the approved conditions. Kill switch: z below −2 after 10 trades, or a drawdown over 2× the test's.</div>
          <div class="form" style="grid-template-columns:repeat(2,minmax(0,1fr))">
            <label class="f">Contracts<input type="number" id="pp-c" min="1" value="${s.contracts}"></label>
            <label class="f">Daily loss limit ($)<input type="number" id="pp-dl" min="0" step="50" value="${s.dailyLoss}"></label>
            <label class="f">Commission / side ($)<input type="number" id="pp-comm" min="0" step="0.05" value="${s.comm}"></label>
            <label class="f">Slippage / side (ticks)<input type="number" id="pp-slip" min="0" step="0.5" value="${s.slip}"></label>
          </div>
          <button class="btn sm" data-act="save-costs">Save</button>
          <label class="f">Account<select disabled><option>Simulated paper account</option><option>[Futures broker — not connected]</option></select></label>
          ${pp.on ? '<button class="btn" data-act="paper-stop">Stop paper trading</button>' : v && v.pass ? '<button class="btn primary" data-act="paper">Start paper trading</button>'
            : v ? `<button class="btn" data-act="paper-force">Start paper trading anyway</button><div class="note">The validation didn't pass (${v.gates.filter(g => !g.ok).map(g => esc(g.t)).join(', ')}). Paper trading is simulated: it shows how the rule does from now on, with the same kill switch.</div>`
            : '<button class="btn primary" disabled>Paper trading needs a validation</button>'}
          ${pp.on && pp.forced ? '<div class="note">Started without a passed validation.</div>' : ''}
          <button class="btn" disabled>Promote to live · needs a futures broker</button>
        </div></section>
      </aside>
    </div>`);
  if (job.status === 'running') pollJob('/strategies/' + id, x => x.job, () => vStrategy(id));
  if (srch && srch.on) pollSearch(id);
}
function pollSearch(id) {
  clearTimeout(S.srchT);
  S.srchT = setTimeout(async () => {
    if (!S.cur || S.cur.id !== id || !$('#srch')) return;
    try { S.srch = await api(`/strategies/${id}/search`); $('#srch').innerHTML = srchHtml(S.cur, S.srch, !!(S.cur.paper || {}).on, true); } catch (e) { /* next time */ }
    if (S.srch && S.srch.on) pollSearch(id);
  }, 15000);
}
function blockLabel(setup, k, v) {
  const p = (SETUPS[setup].params || []).find(x => x[0] === k);
  if (!p) return [k, String(v)];
  if (p[2] === 'sel') { const o = p[3].find(x => String(x[0]) === String(v)); return [p[1], o ? o[1] : String(v)]; }
  if (p[2] === 'bool') return [p[1], v ? 'yes' : 'no'];
  return [p[1], String(v)];
}
function srchHtml(s, o, locked, dataOk) {
  const r2 = (m, k, f) => m && m[k] != null ? f(m[k]) : '—';
  const sp = o.split || {};
  const green = x => x.test && x.test.n >= 30 && (x.test.t || 0) >= 1.5;
  const isCur = x => Object.keys(x.g).every(k => String(x.g[k]) === String(s.params[k]));
  const status = o.running ? `running · ${esc(o.step || '')}` : o.on ? 'starting the next run…' : o.done ? 'every setting tried' : 'stopped';
  return `<section class="panel"><div class="hd"><h2>Optimizer</h2>
      <button class="btn sm ${o.on ? '' : 'primary'}" data-act="${o.on ? 'srch-stop' : 'srch-start'}" ${!o.on && !dataOk ? 'disabled' : ''}>${o.on ? '■ Stop' : '▶ Start'}</button>
      <button class="btn sm" data-act="srch-restart">Restart</button>
      <span class="dim" style="font-size:13px">${(o.tried || 0).toLocaleString('en-US')} settings tried${o.avgEval ? ` · ${o.avgEval}s each` : ''} · ${status}</span></div>
    <div class="bd note" style="border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:4px">
      ${o.log && o.log.length ? `<div>Runs (about 8 min each): ${o.log.slice().reverse().map(l => l.error ? `<span class="loss">${esc(l.at.slice(11, 16))} error: ${esc(l.error)}</span>` : `${esc(l.at.slice(11, 16))} ${l.tried} tried · best ${l.best != null ? l.best : '—'}`).join(' · ')}</div>` : ''}
      ${o.error ? `<div class="loss">Last error: ${esc(o.error)}</div>` : ''}
      <div>Each setting is a full study of this strategy (net of costs, in R of each trade's risk). Chosen on the <b>train</b> days only${sp.cut ? ` (${esc(sp.from)} → ${esc(sp.cut)}, ${sp.trainDays} days)` : ''} by the <b>t of the R per day</b>, ${o.minTrades}+ trades, no month over ${Math.round(o.monthShare * 100)}% of the profit.
        The <b>test</b> days${sp.cut ? ` (${esc(sp.cut)} → ${esc(sp.to)}, ${sp.testDays} days)` : ''} are never used to choose: a setting good on both is the one to try. Starts from the current rules.</div></div>
    ${o.top && o.top.length ? `<div class="tbl-wrap"><table class="tbl" style="min-width:980px"><thead><tr><th>#</th><th class="r">Score</th><th class="r">Train trades</th><th class="r">Win</th><th class="r">Avg</th><th class="r">t</th>
      <th class="r">Test trades</th><th class="r">Win</th><th class="r">Avg</th><th class="r">Total</th><th class="r">t</th><th class="r">DD</th><th>Settings</th><th></th></tr></thead><tbody>
      ${o.top.map((x, i) => `<tr style="${green(x) ? 'background:rgba(63,185,120,0.10)' : ''}"><td>${i + 1}</td><td class="r mono">${x.fit.toFixed(2)}</td>
        <td class="r">${r2(x.train, 'n', v => v.toLocaleString('en-US'))}</td><td class="r">${r2(x.train, 'win', v => v + '%')}</td><td class="r ${cls(x.train.avgR)}">${r2(x.train, 'avgR', v => sgn(v, 2, 'R'))}</td><td class="r mono">${r2(x.train, 't', v => v.toFixed(2))}</td>
        <td class="r">${r2(x.test, 'n', v => v.toLocaleString('en-US'))}</td><td class="r">${r2(x.test, 'win', v => v + '%')}</td><td class="r ${cls(x.test.avgR)}">${r2(x.test, 'avgR', v => sgn(v, 2, 'R'))}</td>
        <td class="r ${cls(x.test.totR)}">${r2(x.test, 'totR', v => sgn(v, 2, 'R'))}</td><td class="r mono">${r2(x.test, 't', v => v.toFixed(2))}</td><td class="r loss">${r2(x.test, 'dd', v => sgn(v, 2, 'R'))}</td>
        <td><details><summary class="dim" style="cursor:pointer;font-size:12px">show</summary><div style="font-size:12px;margin-top:4px;display:grid;grid-template-columns:auto auto;gap:2px 10px">${(x.keys || Object.keys(x.g)).map(k => [k, x.g[k]]).map(([k, v]) => { const [l, t] = blockLabel(s.setup, k, v); return `<span class="dim">${esc(l)}</span><span class="mono" style="${String(s.params[k]) !== String(v) ? 'color:var(--buyT)' : ''}">${esc(t)}</span>`; }).join('')}</div></details></td>
        <td>${isCur(x) ? '<span class="dim" style="font-size:12px">current</span>' : `<button class="btn sm" data-srchuse="${i}" ${locked ? 'disabled' : ''}>Use</button>`}</td></tr>`).join('')}
    </tbody></table></div><div class="bd note">Green rows: still positive on the test days (30+ trades, t ≥ 1.5). In the settings, values in blue differ from the current rules. Use one, then run the study and the validation.</div>`
    : `<div class="empty">${o.on ? 'Searching: the first results appear after the first setting.' : 'Press Start: it keeps trying settings (about 8 minutes per run, one run after another) until you stop it.'}</div>`}
  </section>`;
}
function optHtml(s, o, locked) {
  const keys = Object.keys(o.grid), cur = keys.every(k => true) ? s.params : {};
  const isCur = r => keys.every(k => String(r.p[k]) === String(cur[k]));
  const val = (m, k, f) => m && m[k] != null ? f(m[k]) : '—';
  const best = o.rows.find(r => r.train.n >= o.minTrades);
  const lab = { bufTicks: 'Stop buffer', tgt: 'Target', lastEntry: 'Last entry', minDepth: 'Min depth', stop: 'Stop', tgtMult: 'Target ×', filter: 'Filter',
    drivePct: 'Drive %', pbMin: 'Pullback', ema: 'EMA', tgtR: 'Target R', trend: 'Closes before', wait: 'Within', hours: 'Hours', rangeMin: 'Range', minRR: 'Min R',
    entryAt: 'Entry', legPct: 'Leg %', entryLvl: 'Entry', stopBuf: 'Stop past', pivLen: 'Swing', rr: 'Target R', expiry: 'Bias', entry: 'Entry', atrMult: 'ATR ×',
    maLen: 'MA', tf: 'Candles', stopAtr: 'Stop ATR', side: 'Side' };
  return `<section class="panel"><div class="hd"><h2>Optimization</h2><span class="dim" style="font-size:13px">${o.rows.length} of ${o.total} settings tested${o.complete ? '' : ' (running…)'} · ranked on the <b>train</b> period (before ${esc(o.split || '')}); the <b>test</b> columns are the honest check</span></div>
    ${best ? `<div class="bd note" style="border-bottom:1px solid var(--line)">Best on train: ${keys.map(k => `${lab[k] || k} <b class="mono" style="color:var(--text)">${esc(best.p[k])}</b>`).join(' · ')} →
      test ${val(best.test, 'n', x => x)} trades, ${val(best.test, 'exp', x => money(x))} / trade, edge ${val(best.test, 'edge', x => sgn(x, 2, ' pts'))}.
      ${best.test && best.test.exp > 0 && best.test.edge > 0 ? '<span class="gain">Holds on the test period.</span>' : '<span class="loss">Doesn\'t hold on the test period: likely fitted to the past.</span>'}</div>` : ''}
    <div class="tbl-wrap" style="max-height:460px;overflow:auto"><table class="tbl" style="min-width:${560 + keys.length * 80}px"><thead><tr>${keys.map(k => `<th>${lab[k] || k}</th>`).join('')}
      <th class="r">Train n</th><th class="r">Train $/tr</th><th class="r">Train PF</th><th class="r">Train edge</th><th class="r">Test n</th><th class="r">Test $/tr</th><th class="r">Test PF</th><th class="r">Test edge</th><th></th></tr></thead><tbody>
      ${o.rows.map((r, i) => `<tr style="${isCur(r) ? 'background:var(--selB)' : r.train.n < o.minTrades ? 'opacity:.55' : ''}">${keys.map(k => `<td class="mono">${esc(r.p[k])}</td>`).join('')}
        <td class="r">${val(r.train, 'n', x => x)}</td><td class="r ${cls(r.train.exp)}">${val(r.train, 'exp', x => money(x))}</td><td class="r">${val(r.train, 'pf', x => x)}</td><td class="r ${cls(r.train.edge)}">${val(r.train, 'edge', x => sgn(x, 1))}</td>
        <td class="r">${val(r.test, 'n', x => x)}</td><td class="r ${cls(r.test.exp)}">${val(r.test, 'exp', x => money(x))}</td><td class="r">${val(r.test, 'pf', x => x)}</td><td class="r ${cls(r.test.edge)}">${val(r.test, 'edge', x => sgn(x, 1))}</td>
        <td>${isCur(r) ? '<span class="dim" style="font-size:12px">current</span>' : `<button class="btn sm" data-optuse="${i}" ${locked ? 'disabled' : ''}>Use</button>`}</td></tr>`).join('')}
    </tbody></table></div></section>`;
}
function equityBars(ts, split) {
  const xs = ts.filter(t => t.status !== 'open' && t.why !== 'open'); if (!xs.length) return '';
  const step = Math.max(1, Math.ceil(xs.length / 160)); let e = 0; const pts = [];
  xs.forEach((t, i) => { e += t.net; if (i % step === 0 || i === xs.length - 1) pts.push([e, split && t.d >= split]); });
  const lo = Math.min(0, ...pts.map(p => p[0])), hi = Math.max(...pts.map(p => p[0]));
  return pts.map(([v, te]) => `<i style="height:${4 + (v - lo) / (hi - lo || 1) * 104}px;background:${te ? '#B5702A' : '#2F5D93'}"></i>`).join('');
}
function rHist(ts) {
  const bins = new Array(9).fill(0);
  ts.forEach(t => { if (t.r == null) return; const k = Math.max(0, Math.min(8, Math.floor((t.r + 1) * 2))); bins[k]++; });
  const mx = Math.max(...bins) || 1;
  return bins.map((v, i) => `<i title="${v} trades" style="height:${2 + v / mx * 106}px;background:${i < 2 ? 'var(--sell)' : 'var(--buy)'};border-radius:2px 2px 0 0"></i>`).join('');
}

/* ---------- Chart ---------- */
const DAYS = { 1: [1, 3, 5, 10], 5: [1, 3, 5, 10, 30], 15: [5, 10, 30, 60], 60: [30, 60, 120, 180] };
async function vChart() {
  const c = S.chart;
  if (!S.strats) { try { S.strats = (await api('/strategies')).strategies; } catch (e) { S.strats = []; } }
  if (!DAYS[c.tf].includes(c.days)) c.days = DAYS[c.tf][1] || DAYS[c.tf][0];
  view(`<div class="toolbar">
      <div class="seg" role="group" aria-label="Contract">${['NQ', 'ES'].map(s => `<button data-csym="${s}" aria-pressed="${c.sym === s}" style="font-weight:600">${s}</button>`).join('')}</div>
      <div class="quote" id="quote"></div><div class="stats" id="cstats"></div><div style="flex:1"></div>
      <div class="seg" role="group" aria-label="Timeframe">${[1, 5, 15, 60].map(t => `<button data-ctf="${t}" aria-pressed="${c.tf === t}">${t === 60 ? '1h' : t + 'm'}</button>`).join('')}</div>
      <div class="seg" role="group" aria-label="Days">${DAYS[c.tf].map(d => `<button data-cdays="${d}" aria-pressed="${c.days === d}">${d}d</button>`).join('')}</div>
    </div>
    <div class="row">
      <section class="panel col-main" style="display:block;padding-top:12px">
        <div class="legend"><span>Strategy on the chart</span><select id="c-strat" style="min-height:30px;padding:3px 8px"><option value="">None</option>${S.strats.filter(s => s.params.tickers.includes(c.sym) && s.stage !== 'retired').map(s => `<option value="${s.id}" ${c.strat === s.id ? 'selected' : ''}>${esc(s.name)} · ${STAGES[s.stage] || s.stage}</option>`).join('')}</select>
          <span style="display:inline-flex;align-items:center;gap:4px"><button class="chip" data-ctog="ema" aria-pressed="${!!c.ema}">EMA</button><input type="number" id="c-emalen" min="2" max="400" value="${c.emaLen}" aria-label="EMA length" style="width:62px;min-height:28px;padding:2px 6px"></span>${[['fvg', 'FVG'], ['sd', 'Supply / demand'], ['prof', 'Volume profile'], ['cvd', 'Delta'], ['wt', 'WaveTrend'], ['walls', 'Walls'], ['rth', 'Regular hours only']].map(([k, l]) => `<button class="chip" data-ctog="${k}" aria-pressed="${!!c[k]}">${l}</button>`).join('')}
          <div style="flex:1"></div>${c.strat ? '<span><span class="sw" style="border-top:2px solid #E7C35A"></span>Strategy lines · B breakout · R retest · C confirmation</span>' : ''}${c.ema ? `<span><span class="sw" style="border-top:2px solid #5FD4C4"></span>EMA ${c.emaLen}</span>` : ''}<span><span class="sw" style="border-top:2px solid var(--vwap)"></span>VWAP</span><span><span class="sw" style="border-top:1px dashed #93A0B2"></span>PDH / PDL / PDC · ONH / ONL</span></div>
        ${S.rp && S.rp.hm ? `<div class="dual ${S.rpLayout === 'stack' ? 'stack' : ''}"><div class="chartbox"><canvas id="cv" style="height:600px"></canvas></div>
          <div class="heatbox"><div class="legend" style="padding:0 0 8px"><b style="color:var(--text)">Order book</b><span>${S.rp.hm.step} s columns · book carried beyond the 10 levels until traded through</span><div style="flex:1"></div>
            <div class="seg">${[300, 900, 1800, 3600].map(v => `<button data-hmspan="${v}" aria-pressed="${(S.hmSpan || 900) === v}">${v / 60}m</button>`).join('')}</div>
            <div class="seg">${[[60, '+'], [120, '='], [240, '−']].map(([v, l]) => `<button data-hmrows="${v}" aria-pressed="${(S.hmRows || 120) === v}" aria-label="Zoom ${l}">${l}</button>`).join('')}</div>
            <div class="seg"><button data-rplayout="side" aria-pressed="${S.rpLayout !== 'stack'}">Side by side</button><button data-rplayout="stack" aria-pressed="${S.rpLayout === 'stack'}">Stacked</button></div></div>
            <canvas id="hm" style="height:${S.rpLayout === 'stack' ? 520 : 600}px;width:100%;display:block"></canvas></div></div>` : `<div class="chartbox"><canvas id="cv" style="height:${S.rp ? 600 : 640}px"></canvas></div>`}
        ${S.rp ? replayBar() : `<div class="transport"><span class="muted" style="font-size:13px">Replay a session bar by bar with its order flow:</span><input type="date" id="rp-day" value="${S.lastDay || ''}"><button class="btn" data-act="rp-start">Start replay</button><span class="note">The heatmap, DOM, footprint and tape need that day's order book (Data page, or the button in the replay).</span></div>`}
        ${S.rp && S.rpView === 'fp' ? `<div class="bd" style="border-top:1px solid var(--line)"><div id="fpview"></div></div>` : ''}
      </section>
      <aside class="col-side">${S.rp ? `
        <section class="panel ladder"><div class="hd"><h2 id="ladder-h">Volume at price</h2><div class="grow"></div><span class="dim" style="font-size:12px">${S.rp.hm ? 'resting size · traded so far' : 'traded so far · bid × ask'}</span></div><div id="ladder" style="padding:6px 0"></div></section>
        <section class="panel tape"><div class="hd"><h2>Time &amp; sales</h2><div class="grow"></div><span class="dim" style="font-size:12px">big prints</span></div><div id="tape" style="padding:6px 0;max-height:360px;overflow:auto"></div></section>` : `
        <section class="panel"><div class="hd"><h2>Session</h2></div><div class="bd" id="sessinfo"><span class="dim">Loading…</span></div></section>`}
      </aside>
    </div>`);
  S.ch = new FC.Chart($('#cv'), { span: S.rp ? 120 : (c.tf === 1 ? 240 : 180) });
  if (S.heat) { S.heat.destroy(); S.heat = null; }
  if ($('#hm')) S.heat = new FC.Heat($('#hm'));
  await loadChart();
}
function replayBar() {
  const r = S.rp;
  return `<div class="transport">
    <button class="iconbtn" data-act="rp-back" aria-label="One candle back"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg></button>
    <button class="iconbtn play" data-act="rp-play" aria-label="${r.playing ? 'Pause' : 'Play'}">${r.playing ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M8 5v14M16 5v14"/></svg>' : '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 5l12 7-12 7z"/></svg>'}</button>
    <button class="iconbtn" data-act="rp-fwd" aria-label="One candle forward"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></button>
    <div class="seg" role="group" aria-label="Speed">${[1, 2, 5, 10].map(v => `<button data-rpspeed="${v}" aria-pressed="${r.speed === v}">${v}×</button>`).join('')}</div>
    <label style="flex:1 1 220px;display:flex;align-items:center;gap:10px;font-size:12px" class="dim"><span class="mono" id="rp-time" style="color:var(--text)"></span><input type="range" id="rp-scrub" min="0" max="${(r.n || 1) - 1}" value="${r.k}" style="flex:1" aria-label="Replay position"></label>
    <div class="seg"><button data-rpview="chart" aria-pressed="${S.rpView !== 'fp'}">Candles</button><button data-rpview="fp" aria-pressed="${S.rpView === 'fp'}">+ Footprint</button></div>
    <span class="mono" style="font-size:13px">${esc(r.day)}</span>${r.hm ? '' : '<button class="btn sm" data-act="rp-getbook">Download this day\'s order book</button>'}${r.fp || r.hm ? '' : '<button class="btn sm" data-act="rp-getfp">Trades only</button>'}
    <button class="btn sm" data-act="rp-stop">Exit replay</button></div>`;
}
async function loadChart() {
  const c = S.chart, r = S.rp;
  const q = r ? `sym=${c.sym}&tf=${c.tf <= 5 ? c.tf : 1}&days=1&end=${r.day}${c.strat ? '&strat=' + c.strat : ''}` : `sym=${c.sym}&tf=${c.tf}&days=${c.days}${c.rth ? '&rth=1' : ''}${c.walls ? '&walls=1' : ''}${c.strat ? '&strat=' + c.strat : ''}`;
  let d;
  try { d = await api('/chart?' + q); } catch (e) { toast(e.message); return; }
  S.cd = d;
  if (!d.bars.length) { $('.chartbox').innerHTML = `<div class="empty">No ${c.sym} candles stored${r ? ' for ' + esc(r.day) : ''}. <a href="#data">Download them on the Data page</a>.</div>`; const si = $('#sessinfo'); if (si) si.innerHTML = '<span class="dim">—</span>'; return; }
  S.lastDay = d.days[d.days.length - 1];
  if (r) {
    r.n = d.bars.length; r.tf = d.tf;
    if (r.k == null) { const i = d.bars.findIndex(b => b[0].slice(11, 16) >= '09:30' && b[0].slice(0, 10) === r.day); r.k = Math.max(0, i) + Math.round(15 / d.tf); }
    r.k = Math.min(r.k, r.n - 1);
    const sc = $('#rp-scrub'); if (sc) sc.max = r.n - 1;
    if (r.fp === undefined) { try { r.fp = await api(`/footprint?sym=${c.sym}&day=${r.day}`); } catch (e) { r.fp = null; } if (!r.fp) { const tb = $('.transport'); if (tb) tb.outerHTML = replayBar(); } }
    if (r.hm === undefined) {
      r.hm = null;
      if ((d.hmDays || []).includes(r.day)) { try { r.hm = await api(`/heatmap?sym=${c.sym}&day=${r.day}`); } catch (e) { r.hm = null; } }
      if (r.hm) return vChart();                    // the side-by-side layout
      const tb = $('.transport'); if (tb) tb.outerHTML = replayBar();
    }
  }
  drawChart(false);
}
function fpDelta() {
  const r = S.rp; if (!r || !r.fp) return null;
  if (!r.delta) { r.delta = {}; r.fp.m.forEach(([m, lv]) => { r.delta[m] = lv.reduce((s, [, b, a]) => s + a - b, 0); }); }
  return { [r.day]: r.delta };
}
function drawChart(keep) {
  const c = S.chart, d = S.cd, r = S.rp;
  if (!S.ch || !d) return;
  S.ch.set({ bars: d.bars, vwap: d.vwap, levels: d.levels, walls: c.walls ? d.walls : null, marks: d.marks || [], overlay: d.overlay, showEMA: c.ema, emaLen: c.emaLen, showFVG: c.fvg, showSD: c.sd, sdZones: d.zones, tf: d.tf, showWT: c.wt, showCVD: c.cvd, showProfile: c.prof, replay: r ? r.k : null, fpDelta: fpDelta() }, keep);
  const n = r ? r.k + 1 : d.bars.length, B = d.bars, last = B[n - 1], day = last[6];
  const sess = B.slice(0, n).filter(b => b[6] === day), lv = d.levels[day] || {};
  const pc = lv.pdc, chg = pc ? last[4] - pc : null;
  $('#quote').innerHTML = `<b>${fmt(last[4])}</b><span class="${cls(chg)}">${chg != null ? sgn(chg) + '  ' + sgn(chg / pc * 100, 2, '%') : ''}</span>`;
  const vol = sess.reduce((s, b) => s + b[5], 0), vw = S.ch.vw[n - 1];
  $('#cstats').innerHTML = `<span>VWAP <b>${fmt(vw)}</b></span><span>High <b>${fmt(Math.max(...sess.map(b => b[2])))}</b></span><span>Low <b>${fmt(Math.min(...sess.map(b => b[3])))}</b></span><span>Volume <b>${vol.toLocaleString('en-US')}</b></span><span>Condition <b>${esc(cellName(d.cells[day]))}</b></span>`;
  const si = $('#sessinfo');
  if (si) {
    const mk = d.marks || [];
    si.innerHTML = `<div class="kv kv3" style="margin-bottom:12px">${[['Prior high', lv.pdh], ['Prior low', lv.pdl], ['Prior close', lv.pdc], ['ON high', lv.onh], ['ON low', lv.onl], ['RTH open', lv.open]].map(([k, x]) => `<div><div class="k">${k}</div><div class="v">${fmt(x)}</div></div>`).join('')}</div>
      <div class="note">Session ${esc(day)} · ${esc(cellName(d.cells[day]))}. Footprint stored for ${d.fpDays.length ? esc(d.fpDays.join(', ')) : 'none of these days'}.</div>
      ${c.strat ? `<div class="sep"></div><div class="note" style="margin-bottom:6px">${mk.length} trade(s) of the strategy on these days</div>${mk.slice(-8).reverse().map(t => `<div class="fill" style="padding:6px 0"><span class="t">${esc(t.in.slice(5, 16).replace('T', ' '))}</span><span class="s ${t.side === 'long' ? 'gain' : 'loss'}">${t.side === 'long' ? 'L' : 'S'}</span><span class="w mono">${fmt(t.e)} → ${fmt(t.x)}</span><span class="mono ${cls(t.net)}">${money(t.net)}</span></div>`).join('')}` : ''}
      <div class="note" style="margin-top:10px">Scroll to zoom, drag to pan, double-click to reset.</div>`;
  }
  if (r && S.heat && r.hm) { const b = d.bars[r.k], end = FC.toMin(b[0]) * 60 + d.tf * 60; S.heat.set({ hm: r.hm, big: r.fp ? r.fp.big : [], tEnd: end, span: S.hmSpan || 900, last: b[4], rows: S.hmRows || 120 }); }
  if (r) { replaySide(); const tm = $('#rp-time'); if (tm) tm.textContent = last[0].slice(11, 16); const sc = $('#rp-scrub'); if (sc) sc.value = r.k; }
}
function replayNowEnd() { const r = S.rp, b = S.cd.bars[r.k]; return FC.minStr(FC.toMin(b[0]) + S.cd.tf); }
function replaySide() {
  const r = S.rp, d = S.cd, lad = $('#ladder'), tape = $('#tape');
  if (!lad) return;
  if (!r.fp || S.cd.bars[r.k][6] !== r.day || S.cd.bars[r.k][0].slice(0, 10) !== r.day) {
    const msg = r.fp ? '<div class="empty">Before the regular session.</div>' : '<div class="empty">No trades stored for this day.</div>';
    lad.innerHTML = msg; tape.innerHTML = msg; const fv = $('#fpview'); if (fv) fv.innerHTML = msg; return;
  }
  const end = replayNowEnd(), last = d.bars[r.k][4], step = S.chart.sym === 'NQ' ? 1 : 0.25;
  const agg = {};
  r.fp.m.forEach(([m, lv]) => { if (m >= end) return; lv.forEach(([p, b, a]) => { const k = Math.floor(p / step) * step; const x = agg[k] || (agg[k] = [0, 0]); x[0] += b; x[1] += a; }); });
  const lk = Math.floor(last / step) * step, rows = [];
  for (let j = 15; j >= -15; j--) { const p = +(lk + j * step).toFixed(2); rows.push([p, ...(agg[p] || [0, 0])]); }
  const mx = Math.max(1, ...rows.map(x => Math.max(x[1], x[2])));
  lad.innerHTML = r.hm ? domHtml(end) : rows.map(([p, b, a]) => `<div class="lr ${p === lk ? 'last' : ''}"><div class="b"><i style="width:${b / mx * 100}%"></i><span>${b || ''}</span></div><div class="p">${fmt(p)}</div><div class="a"><i style="width:${a / mx * 100}%"></i><span>${a || ''}</span></div></div>`).join('');
  const lh = $('#ladder-h'); if (lh) lh.textContent = r.hm ? 'Order book (DOM)' : 'Volume at price';
  const big = r.fp.big.filter(x => x[0].slice(0, 5) < end).slice(-30).reverse();
  tape.innerHTML = big.length ? big.map(([t, p, s, side]) => `<div class="tr big" style="background:${side === 'B' ? 'rgba(74,158,255,.12)' : side === 'S' ? 'rgba(240,140,46,.12)' : 'transparent'}"><span class="dim">${esc(t)}</span><span>${fmt(p)}</span><span style="text-align:right">${s}</span><span class="${side === 'B' ? 'gain' : 'loss'}">${side}</span></div>`).join('') : '<div class="empty">No big prints yet.</div>';
  const fv = $('#fpview');
  if (fv) fv.innerHTML = footprintHtml(end);
}
function domHtml(end) {
  // the 10 resting levels on each side at the cursor (from the heatmap), with the volume traded at each price so far
  const r = S.rp, hm = r.hm, tick = hm.tick, es = +end.slice(0, 2) * 3600 + +end.slice(3, 5) * 60;
  let col = null; for (const c of hm.c) { if (c[0] < es) col = c; else break; }
  if (!col) return '<div class="empty">No book yet at this time.</div>';
  const traded = {};
  r.fp && r.fp.m.forEach(([m, lv]) => { if (m >= end) return; lv.forEach(([p, b, a]) => { const x = traded[p] || (traded[p] = [0, 0]); x[0] += b; x[1] += a; }); });
  const lv = [];
  for (let k = col[2].length - 2; k >= 0; k -= 2) lv.push([(hm.base + col[2][k]) * tick, 0, col[2][k + 1]]);
  for (let k = 0; k < col[1].length; k += 2) lv.push([(hm.base + col[1][k]) * tick, col[1][k + 1], 0]);
  const mx = Math.max(1, ...lv.map(x => Math.max(x[1], x[2])));
  return `<div class="lr" style="grid-template-columns:1fr 46px 82px 46px 1fr;color:var(--dim);font-size:11px"><span style="text-align:right">Bids</span><span style="text-align:right">sold</span><span style="text-align:center">Price</span><span>bought</span><span>Asks</span></div>` +
    lv.map(([p, b, a]) => { const t = traded[p] || [0, 0];
      return `<div class="lr" style="grid-template-columns:1fr 46px 82px 46px 1fr"><div class="b" style="justify-content:flex-end"><i style="width:${b / mx * 100}%;background:rgba(95,212,196,.3)"></i><span>${b || ''}</span></div>
        <span class="dim" style="text-align:right">${t[0] || ''}</span><div class="p" style="color:${a ? 'var(--sellT)' : 'var(--buyT)'}">${fmt(p)}</div><span class="dim">${t[1] || ''}</span>
        <div class="a"><i style="width:${a / mx * 100}%;background:rgba(240,86,60,.3)"></i><span>${a || ''}</span></div></div>`; }).join('');
}
function footprintHtml(end) {
  const r = S.rp, d = S.cd, tf = d.tf, k0 = Math.max(0, r.k - 7), bars = d.bars.slice(k0, r.k + 1).filter(b => b[0].slice(0, 10) === r.day && b[0].slice(11, 16) >= '09:30');
  if (!bars.length) return '<div class="empty">Footprint starts at 09:30.</div>';
  const byMin = {}; r.fp.m.forEach(([m, lv]) => { byMin[m] = lv; });
  const hi = Math.max(...bars.map(b => b[2])), lo = Math.min(...bars.map(b => b[3]));
  const step = Math.max(0.25, Math.ceil((hi - lo) / 22 / 0.25) * 0.25), top = Math.ceil(hi / step) * step, nRows = Math.round((top - Math.floor(lo / step) * step) / step) + 1;
  const cols = bars.map(b => {
    const m0 = FC.toMin(b[0]), cells = new Array(nRows).fill(null).map(() => [0, 0]);
    for (let m = m0; m < m0 + tf; m++) { const ms = FC.minStr(m); if (ms >= end) break; (byMin[ms] || []).forEach(([p, bb, aa]) => { const i = Math.round((top - Math.floor(p / step) * step) / step); if (i >= 0 && i < nRows) { cells[i][0] += bb; cells[i][1] += aa; } }); }
    const dl = cells.reduce((s, x) => s + x[1] - x[0], 0);
    return { t: b[0].slice(11, 16), cells, dl, h: b[2], l: b[3] };
  });
  return `<div class="fp">${cols.map(c => `<div class="c"><div class="h">${c.t}</div>${c.cells.map(([b, a], i) => { const p = top - i * step, inBar = p <= c.h + step && p >= c.l - step && (b || a);
      return inBar ? `<div class="x"><span style="${b >= 3 * Math.max(1, a) ? 'color:var(--sellT);font-weight:700' : ''}">${b}</span><span style="${a >= 3 * Math.max(1, b) ? 'color:var(--buyT);font-weight:700' : ''}">${a}</span></div>` : '<div class="x blank"><span>&nbsp;</span><span></span></div>'; }).join('')}
      <div class="h ${cls(c.dl)}" style="margin-top:4px">Δ ${c.dl > 0 ? '+' : ''}${c.dl}</div></div>`).join('')}
    <div class="c px"><div class="h">&nbsp;</div>${new Array(nRows).fill(0).map((_, i) => `<div>${fmt(top - i * step)}</div>`).join('')}</div></div>
    <p class="note">Bid volume × ask volume at each price (${fmt(step)}-point rows). Bold: 3× imbalance (orange sellers, blue buyers).</p>`;
}
function rpMove(k) { const r = S.rp; r.k = Math.max(0, Math.min(r.n - 1, k)); S.ch.d.replay = r.k; drawChart(true); S.ch.follow(); }
function rpPlay(on) {
  const r = S.rp; r.playing = on;
  if (S.rpTimer) { clearInterval(S.rpTimer); S.rpTimer = null; }
  if (on) S.rpTimer = setInterval(() => { if (r.k >= r.n - 1) { rpPlay(false); return; } rpMove(r.k + 1); }, 1000 / r.speed);
  const tb = $('.transport'); if (tb) tb.outerHTML = replayBar();
}

/* ---------- events ---------- */
document.addEventListener('change', async e => {
  if (e.target.id !== 'edge-test' || !S.cur) return;
  try { await api('/strategies/' + S.cur.id, { method: 'PUT', body: { edgeTest: e.target.value } }); await api(`/strategies/${S.cur.id}/run`, { method: 'POST', body: { step: 'validate' } }); vStrategy(S.cur.id); } catch (err) { toast(err.message || 'Failed'); }
});
document.addEventListener('click', async e => {
  const el = e.target.closest('button, a[data-act]');
  if (!el || el.disabled) return;
  const ds = el.dataset, act = ds.act;
  try {
    if (ds.symc) { const s = new Set(S.dlSyms || ['NQ', 'ES']); s.has(ds.symc) ? s.delete(ds.symc) : s.add(ds.symc); S.dlSyms = [...s]; el.setAttribute('aria-pressed', s.has(ds.symc)); return; }
    if (ds.symb) { const s = new Set(S.bkSyms || ['NQ']); s.has(ds.symb) ? s.delete(ds.symb) : s.add(ds.symb); S.bkSyms = [...s]; el.setAttribute('aria-pressed', s.has(ds.symb)); return; }
    if (ds.symf) { const s = new Set(S.fpSyms || ['NQ']); s.has(ds.symf) ? s.delete(ds.symf) : s.add(ds.symf); S.fpSyms = [...s]; el.setAttribute('aria-pressed', s.has(ds.symf)); return; }
    if (ds.dlq) { $('#dl-from').value = addDays(today(), +ds.dlq); return; }
    if (ds.ptick) { el.setAttribute('aria-pressed', el.getAttribute('aria-pressed') !== 'true'); return; }
    if (ds.filter) { S.filter = ds.filter; return vResearch(); }
    if (ds.setup) { S.newSetup = ds.setup; $('#newbox').innerHTML = newForm(); return; }
    if (ds.trset) { S.trSet = ds.trset; return vStrategy(S.cur.id); }
    if (ds.srchuse) { const x = S.srch.top[+ds.srchuse]; await api('/strategies/' + S.cur.id, { method: 'PUT', body: { params: { ...S.cur.params, ...x.g } } }); toast('Settings applied: run the study and the validation.'); return vStrategy(S.cur.id); }
    if (ds.optuse) { const r = S.opt.rows[+ds.optuse]; await api('/strategies/' + S.cur.id, { method: 'PUT', body: { params: { ...S.cur.params, ...r.p } } }); toast('Settings applied: run the study and the validation.'); return vStrategy(S.cur.id); }
    if (ds.cellk) { const on = el.getAttribute('aria-pressed') !== 'true'; await api('/strategies/' + S.cur.id, { method: 'PUT', body: { cells: { [ds.cellk]: on } } }); return vStrategy(S.cur.id); }
    if (ds.csym) { S.chart.sym = ds.csym; S.chart.strat = ''; S.rp = null; return vChart(); }
    if (ds.ctf) { S.chart.tf = +ds.ctf; if (S.rp) { S.rp.k = null; S.rp.delta = null; } return vChart(); }
    if (ds.cdays) { S.chart.days = +ds.cdays; return vChart(); }
    if (ds.lvtab) { S.lv.tab = ds.lvtab; return vLevels(); }
    if (ds.lvsym) { lvRead(); S.lv.sym = ds.lvsym; return vLevels(); }
    if (ds.ctog) { S.chart[ds.ctog] = !S.chart[ds.ctog]; el.setAttribute('aria-pressed', S.chart[ds.ctog]); if (ds.ctog === 'rth' || ds.ctog === 'walls') return loadChart(); return drawChart(true); }
    if (ds.rpspeed) { S.rp.speed = +ds.rpspeed; return rpPlay(S.rp.playing); }
    if (ds.rpview) { S.rpView = ds.rpview; return vChart(); }
    if (ds.hmspan) { S.hmSpan = +ds.hmspan; document.querySelectorAll('[data-hmspan]').forEach(b => b.setAttribute('aria-pressed', b === el)); return drawChart(true); }
    if (ds.hmrows) { S.hmRows = +ds.hmrows; document.querySelectorAll('[data-hmrows]').forEach(b => b.setAttribute('aria-pressed', b === el)); return drawChart(true); }
    if (ds.rplayout) { S.rpLayout = ds.rplayout; return vChart(); }
    switch (act) {
      case 'refresh': await api('/data/refresh', { method: 'POST', body: {} }); toast('Updating the candles…'); if (S.page === 'data') vData(); break;
      case 'fx-step': { const sl = $('#fx-k'); sl.value = Math.max(+sl.min, Math.min(+sl.max, +sl.value + +el.dataset.d)); fxAt(); break; }
      case 'fx-csv': { const r = S.fxR; if (!r) return; const rows = [['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume', 'MA', 'Pivot', 'SlowMA', 'Direction', 'PeakToTrough', 'BarsElapsed', 'PivotClose']];
        r.bars.forEach((b, i) => { const x = r.series[i]; rows.push([b[0], b[1], b[2], b[3], b[4], b[5], x.ma, x.pivot, x.slowMa, x.direction, x.peakToTrough ?? '', x.barsElapsed ?? '', x.pivot ? (b[4] / x.pivot).toFixed(6) : '']); });
        const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([rows.map(r => r.join(',')).join('\n')], { type: 'text/csv' }));
        a.download = `${r.params.sym}_wave${r.params.wave}_${r.params.agg}m_${r.params.days}d.csv`; a.click(); URL.revokeObjectURL(a.href); break; }
      case 'fx-load': S.fx = { ...S.fx, sym: ($('#fx-sym').value || 'QQQ').trim().toUpperCase(), wave: +$('#fx-wave').value || 10, days: +$('#fx-days').value || 10, stats: +$('#fx-stats').value || 10, agg: +$('#fx-agg').value || 15, rth: $('#fx-rth').checked, cold: $('#fx-cold').checked, close16: $('#fx-c16').checked, macro: $('#fx-macro').value }; $('#fx-out').innerHTML = '<div class="panel"><div class="empty muted">Calculating…</div></div>'; await fxLoad(); break;
      case 'uw-connect': { const t = $('#uw-token').value.trim(); if (!t) return toast('Paste the API token.'); el.disabled = true; el.textContent = 'Checking…'; await api('/uw', { method: 'PUT', body: { token: t } }); toast('Unusual Whales connected.'); vData(); break; }
      case 'uw-test': el.disabled = true; await api('/uw/test', { method: 'POST' }); toast('Unusual Whales answered.'); vData(); break;
      case 'uw-del': if (!confirm('Disconnect Unusual Whales?')) return; await api('/uw', { method: 'DELETE' }); vData(); break;
      case 'mm-load': await mmLoad(); break;
      case 'mm-backfill': { const n = +($('#mm-days').value || 3); el.disabled = true;
        try { await api('/mmhistory/backfill', { method: 'POST', body: { sym: S.fx.sym || 'QQQ', days: n } }); mmPoll(); } finally { el.disabled = false; } break; }
      case 'mm-snap': el.disabled = true; try { await api('/mmhistory/snapshot', { method: 'POST', body: { sym: S.fx.sym || 'QQQ' } }); toast('Snapshot recorded.'); } finally { el.disabled = false; } await mmLoad(); break;
      case 'fx-tog': { const kk = el.dataset.k; S.fxShow[kk] = !fxOn(kk); try { localStorage.setItem('fxShow', JSON.stringify(S.fxShow)); } catch (e) { /* private mode */ }
        $('#fx-tog').innerHTML = fxTogBar(); fxAt(); break; }
      case 'fold': { const p = el.closest('.panel'), key = 'fold:' + el.dataset.k, open = p.dataset.folded === '1';
        fxFold(p, !open); try { localStorage.setItem(key, open ? '0' : '1'); } catch (e) { /* private mode */ } break; }
      case 'vp-load': S.vp = { ...S.vp, fwd: +$('#vp-fwd').value || 1, match: $('#vp-match').value, wave: +$('#vp-wave').value || 1, days: +$('#vp-days').value || 200 }; $('#vp-out').innerHTML = '<div class="empty muted">Calculating…</div>'; await vpLoad(); break;
      case 'vp-step': { const r = S.vpR; if (!r) break; const dd = +el.dataset.d, i = dd === 9999 ? r.dates.length - 1 : Math.max(2, Math.min(r.dates.length - 1, r.index + dd)); S.vp.asof = i === r.dates.length - 1 ? '' : r.dates[i]; await vpLoad(); break; }
      case 'oa-load': await oaLoad($('#oa-exp').value); break;
      case 'gx-load': S.gx = { ...S.gx, source: $('#gx-src').value }; await gxLoad(); break;
      case 'alp-connect': { const id = $('#alp-id').value.trim(), sec = $('#alp-secret').value.trim(); if (!id || !sec) return toast('Paste the key ID and the secret.'); el.disabled = true; el.textContent = 'Checking…'; await api('/alpaca', { method: 'PUT', body: { keyId: id, secret: sec, feed: $('#alp-feed').value } }); toast('Alpaca connected.'); vData(); break; }
      case 'alp-save': await api('/alpaca', { method: 'PUT', body: { feed: $('#alp-feed2').value } }); toast('Saved.'); vData(); break;
      case 'alp-test': el.disabled = true; await api('/alpaca/test', { method: 'POST' }); toast('Alpaca answered.'); vData(); break;
      case 'alp-del': if (!confirm('Disconnect Alpaca?')) return; await api('/alpaca', { method: 'DELETE' }); vData(); break;
      case 'db-connect': { const k = $('#db-key').value.trim(); if (!k) return toast('Paste the key first.'); el.disabled = true; el.textContent = 'Checking…'; await api('/databento', { method: 'PUT', body: { key: k } }); toast('Databento connected.'); vData(); break; }
      case 'db-save': await api('/databento', { method: 'PUT', body: { auto: $('#db-auto').checked, autoFp: $('#db-autofp').checked, maxCost: +$('#db-cap').value } }); toast('Saved.'); vData(); break;
      case 'db-test': el.disabled = true; await api('/databento/test', { method: 'POST' }); toast('Databento answered.'); vData(); break;
      case 'db-del': if (!confirm('Disconnect Databento? The stored candles stay.')) return; await api('/databento', { method: 'DELETE' }); vData(); break;
      case 'est-c': case 'est-f': case 'est-b': { const kind = { 'est-c': 'candles', 'est-f': 'footprint', 'est-b': 'book' }[act], out = $('#' + act); out.className = 'muted'; out.textContent = 'Asking Databento…';
        const r = await api('/data/estimate', { method: 'POST', body: dlBody(kind) });
        out.textContent = `Estimated cost: $${r.usd.toFixed(2)}${kind === 'footprint' ? ' (at most)' : ''}`; break; }
      case 'dl-c': await download('candles'); break;
      case 'dl-f': await download('footprint'); break;
      case 'dl-b': await download('book'); break;
      case 'new': S.newSetup = S.newSetup === null ? '' : null; $('#newbox').innerHTML = S.newSetup !== null ? newForm() : ''; break;
      case 'new-close': S.newSetup = null; $('#newbox').innerHTML = ''; break;
      case 'create': { const p = readParams($('#newp')); const s = await api('/strategies', { method: 'POST', body: { setup: S.newSetup, name: $('#new-name').value, params: p } }); S.newSetup = null; S.strats = null; location.hash = '#strategy/' + s.id; break; }
      case 'srch-start': case 'srch-stop': case 'srch-restart': S.srch = await api(`/strategies/${S.cur.id}/search`, { method: 'POST', body: { action: act.slice(5) } }); $('#srch').innerHTML = srchHtml(S.cur, S.srch, !!(S.cur.paper || {}).on, true); toast(act === 'srch-stop' ? 'Stopping: the run in progress ends within a minute.' : act === 'srch-start' ? 'Optimizer started.' : 'Results cleared.'); if (S.srch.on) pollSearch(S.cur.id); break;
      case 'rename': { const nm = ($('#s-name').value || '').trim(); if (!nm) { toast('Give it a name.'); break; } await api('/strategies/' + S.cur.id, { method: 'PUT', body: { name: nm } }); S.strats = null; toast('Renamed.'); vStrategy(S.cur.id); break; }
      case 'duplicate': { const c = await api('/strategies', { method: 'POST', body: { setup: S.cur.setup, name: S.cur.name + ' (copy)', params: { ...S.cur.params } } }); toast('Copy created: change its rules, then run the study.'); location.hash = '#strategy/' + c.id; break; }
      case 'lv-go': lvRead(); await lvLoad(); break;
      case 'make-control': { const c = await api('/strategies', { method: 'POST', body: { setup: 'biaspull', name: 'No-FVG control of ' + S.cur.name, params: { ...S.cur.params } } }); toast('Control created: run its study.'); location.hash = '#strategy/' + c.id; break; }
      case 'run-study': case 'run-valid': case 'run-opt': await api(`/strategies/${S.cur.id}/run`, { method: 'POST', body: { step: { 'run-study': 'study', 'run-valid': 'validate', 'run-opt': 'optimize' }[act] } }); if (act === 'run-opt') toast('Optimizing: each setting is a full study, this can take several minutes.'); vStrategy(S.cur.id); break;
      case 'save-rules': await api('/strategies/' + S.cur.id, { method: 'PUT', body: { params: readParams($('#rules')) } }); toast('Rules saved: run the study again.'); vStrategy(S.cur.id); break;
      case 'save-costs': await api('/strategies/' + S.cur.id, { method: 'PUT', body: { contracts: +$('#pp-c').value, dailyLoss: +$('#pp-dl').value, comm: +$('#pp-comm').value, slip: +$('#pp-slip').value } }); toast('Saved.'); vStrategy(S.cur.id); break;
      case 'paper': case 'paper-force': case 'paper-stop': case 'retire': case 'unretire': await api(`/strategies/${S.cur.id}/action`, { method: 'POST', body: { action: act === 'paper-stop' ? 'stop' : act === 'paper-force' ? 'paper_force' : act } }); S.strats = null; vStrategy(S.cur.id); break;
      case 'delete': if (!confirm('Delete this strategy and its trades?')) return; await api('/strategies/' + S.cur.id, { method: 'DELETE' }); S.strats = null; location.hash = '#research'; break;
      case 'open-chart': { e.preventDefault(); S.chart.strat = S.cur.id; S.chart.sym = S.cur.params.tickers[0]; const tf = S.cur.params.tf; S.chart.tf = tf === '1D' ? 60 : [1, 5, 15, 60].includes(+tf) ? +tf : 5; S.rp = null; location.hash = '#chart'; break; }
      case 'rp-start': { const day = $('#rp-day').value; if (!day) return toast('Pick a day.'); if (S.chart.tf > 5) S.chart.tf = 1; S.rp = { day, k: null, playing: false, speed: 2 }; vChart(); break; }
      case 'rp-stop': rpPlay(false); S.rp = null; vChart(); break;
      case 'rp-play': rpPlay(!S.rp.playing); break;
      case 'rp-back': rpPlay(false); rpMove(S.rp.k - 1); break;
      case 'rp-fwd': rpPlay(false); rpMove(S.rp.k + 1); break;
      case 'rp-getbook': { const body = { kind: 'book', syms: [S.chart.sym], from: S.rp.day, to: S.rp.day, window: 'rth' };
        const r = await api('/data/download', { method: 'POST', body });
        if (r.needConfirm && !confirm(`Databento estimates $${r.usd.toFixed(2)} for this day's order book (09:30–16:00). Download?`)) return;
        if (r.needConfirm) await api('/data/download', { method: 'POST', body: { ...body, confirm: true } });
        toast(`Downloading the order book${r.usd != null ? ` (about $${(+r.usd).toFixed(2)})` : ''}… it takes a few minutes; then start the replay again.`); break; }
      case 'rp-getfp': { const r = await api('/data/download', { method: 'POST', body: { kind: 'footprint', syms: [S.chart.sym], from: S.rp.day, to: S.rp.day } });
        if (r.needConfirm && !confirm(`Databento estimates $${r.usd.toFixed(2)} for this day's trades. Download?`)) return;
        if (r.needConfirm) await api('/data/download', { method: 'POST', body: { kind: 'footprint', syms: [S.chart.sym], from: S.rp.day, to: S.rp.day, confirm: true } });
        toast('Downloading the trades… open the replay again in a minute.'); break; }
    }
  } catch (err) {
    toast(err.message);
    if (/^(est|dl)-[cfb]$/.test(act || '')) { const o = $('#est-' + act.slice(-1)); if (o) { o.textContent = err.message; o.className = 'loss'; } }
    if (el.textContent === 'Checking…') { el.disabled = false; el.textContent = 'Connect'; } }
});
document.addEventListener('change', e => {
  if (e.target.id === 'c-strat') {
    S.chart.strat = e.target.value;
    const st = (S.strats || []).find(x => x.id === S.chart.strat), tf = st && +st.params.tf;
    if (st && [1, 5, 15, 60].includes(tf) && tf !== S.chart.tf && !S.rp) S.chart.tf = tf;   // the rule's own candles
    vChart();
  }
  if (e.target.id === 'rp-scrub') { rpPlay(false); rpMove(+e.target.value); }
  if (e.target.id === 'c-emalen') { const v = Math.round(+e.target.value); if (v >= 2 && v <= 400) { S.chart.emaLen = v; S.chart.ema = true; vChart(); } }
});
document.addEventListener('input', e => { if (e.target.id === 'rp-scrub' && S.rp) { S.rp.k = +e.target.value; S.ch.d.replay = S.rp.k; drawChart(true); } });
document.addEventListener('keydown', e => {
  if (!S.rp || S.page !== 'chart' || /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
  if (e.key === 'ArrowRight') { rpPlay(false); rpMove(S.rp.k + 1); } else if (e.key === 'ArrowLeft') { rpPlay(false); rpMove(S.rp.k - 1); } else if (e.key === ' ') { e.preventDefault(); rpPlay(!S.rp.playing); }
});

boot();
