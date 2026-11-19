// Built-in indicator library: every indicator is a Pine script run by pine.js, so users can open any of them
// in the editor, read it, and fork it.
const V = '//@version=6\n';
const S = (id, name, cat, desc, src, extra = {}) => ({ id, name, cat, desc, src: V + src.trim() + '\n', builtin: true, ...extra });

export const CATEGORIES = ['Strategies', 'Trend', 'Momentum', 'Volatility', 'Volume', 'Levels', 'Structure', 'SMC / ICT', 'Signals'];

export const LIBRARY = [
S('onmidnq', 'Overnight Mid → ONH/ONL (range from NQ)', 'Strategies', 'The overnight-midpoint strategy with the overnight range taken from NQ futures (they trade all night, QQQ / SPY do not): the open in the top half of the NQ overnight range buys toward the overnight high, the bottom half sells toward the low. On a QQQ / SPY chart the NQ levels are scaled to the ETF by the price ratio at 09:30. Modes: take profit at the target, or hold to the end of the day. Freeze it in Research to paper trade it.', `
strategy("Overnight Mid → ONH/ONL (NQ range)", "ON mid NQ", overlay = true, initial_capital = 100000, default_qty_type = strategy.fixed, default_qty_value = 100)
rangeFrom = input.string("NQ", "Overnight range from", options = ["NQ", "ES", "Chart"])
onStart   = input.int(1600, "Overnight starts at (HHMM)")
mode      = input.string("Take profit at target", "Mode", options = ["Take profit at target", "Hold to end of day"])
stopMult  = input.float(0.5, "Stop: × the overnight range from the open", step = 0.05)
tightenIn = input.float(0, "Stop to entry once this % of the way to the target (0 = off)", step = 5)
pctMid    = input.float(0, "No trade within ±% of the range around the mid", step = 1)
minRR     = input.float(0, "Target at least × the risk (0 = any)", step = 0.25)
tradeOut  = input.bool(false, "Also trade opens outside the range (fade to the mid)")
exitAt    = input.int(1130, "Out at (HHMM, target mode)")
eodAt     = input.int(1555, "Out at (HHMM, hold mode)")
side      = input.string("Both", "Side", options = ["Both", "Long", "Short"])
qty       = input.float(100, "Quantity (shares / contracts)")

// the overnight high / low on the range symbol (runs on its own bars through request.security)
onRange(st) =>
    tt = hour * 100 + minute
    inON = tt >= st or tt < 930
    var float h = na
    var float l = na
    if inON and not inON[1]
        h := high
        l := low
    else if inON
        h := math.max(nz(h, high), high)
        l := math.min(nz(l, low), low)
    [h, l, close]

[rh, rl, rc] = request.security(rangeFrom == "Chart" ? syminfo.ticker : rangeFrom, timeframe.period, onRange(onStart))

t = hour * 100 + minute
tc = hour(time_close) * 100 + minute(time_close)
var float tgt = na
var float stp = na
var float ent = na
var float lvH = na
var float lvL = na
var bool tight = false

// decided on the candle that closes at 09:30 (its close = the open); the market order fills at the 09:30 open
if t < 930 and tc >= 930 and strategy.position_size == 0 and rh > rl and rc > 0
    k = close / rc
    onh = rh * k
    onl = rl * k
    rng = onh - onl
    mid = (onh + onl) / 2
    o = close
    pos = (o - onl) / rng
    inside = pos > 0 and pos < 1
    d = inside ? (o > mid ? 1 : -1) : tradeOut ? (pos >= 1 ? -1 : 1) : 0
    tg = inside ? (d > 0 ? onh : onl) : mid
    st = o - d * stopMult * rng
    risk = (o - st) * d
    sideOk = side == "Both" or (side == "Long" and d > 0) or (side == "Short" and d < 0)
    lvH := onh
    lvL := onl
    if d != 0 and sideOk and (not inside or math.abs(pos - 0.5) * 100 >= pctMid) and risk > 0 and (tg - o) * d > 0 and (minRR == 0 or (tg - o) * d >= minRR * risk)
        tgt := tg
        stp := st
        ent := o
        tight := false
        strategy.entry("ON", d > 0 ? strategy.long : strategy.short, qty)
        if mode == "Take profit at target"
            strategy.exit("x", "ON", stop = st, limit = tg, comment_loss = "Stop", comment_profit = "Target")
        else
            strategy.exit("x", "ON", stop = st, comment = "Stop")

if strategy.position_size != 0
    d = strategy.position_size > 0 ? 1 : -1
    if tightenIn > 0 and not tight and (d > 0 ? high - ent : ent - low) >= tightenIn / 100 * math.abs(tgt - ent)
        tight := true
        stp := ent
        if mode == "Take profit at target"
            strategy.exit("x", "ON", stop = stp, limit = tgt, comment_loss = "Break-even", comment_profit = "Target")
        else
            strategy.exit("x", "ON", stop = stp, comment = "Break-even")
    if t >= (mode == "Take profit at target" ? exitAt : eodAt)
        strategy.close("ON", comment = "Time", immediately = true)

rth = t >= 930 and t < 1600
plot(rth ? lvH : na, "Overnight high", #8a8f9c, 1, plot.style_linebr)
plot(rth ? lvL : na, "Overnight low", #8a8f9c, 1, plot.style_linebr)
plot(rth ? (lvH + lvL) / 2 : na, "Overnight mid", #FFB86B, 1, plot.style_linebr)
plot(strategy.position_size != 0 ? tgt : na, "Target", #089981, 1, plot.style_linebr)
plot(strategy.position_size != 0 ? stp : na, "Stop", #f23645, 1, plot.style_linebr)
`, { aliases: 'overnight midpoint onmid onh onl nq futures range strategy backtest freeze paper' }),
S('onmid', 'Overnight range midpoint → overnight high / low', 'Strategies', 'Research #5 as a backtestable strategy: the 09:30 open in the top half of the overnight range buys toward the overnight high, in the bottom half sells toward the low; stop 0.5× the range, out at 11:30. Open it in Research to study and optimize it.', `
strategy("Overnight range midpoint → ON high / low", "ON mid", overlay = true, initial_capital = 100000, default_qty_type = strategy.fixed, default_qty_value = 100)
onStart  = input.int(1600, "Overnight starts at (HHMM, 1800 for futures)")
stopPart = input.float(0.5, "Stop: part of the overnight range", step = 0.05)
deadPct  = input.float(0, "No trade within ±% of the range around the mid", step = 1)
minRR    = input.float(0, "Target at least × the risk (0 = any)", step = 0.25)
exitAt   = input.int(1130, "Out at (HHMM)")
atTarget = input.string("Take profit on touch", "At the target", options = ["Take profit on touch", "Hold if a candle closes beyond"])
holdStop = input.string("Overnight extreme", "Stop once held", options = ["Overnight extreme", "Entry", "Leave it"])
side     = input.string("Both", "Side", options = ["Both", "Long", "Short"])

t = hour * 100 + minute
tc = hour(time_close) * 100 + minute(time_close)
inON = t >= onStart or t < 930
startON = inON and not inON[1]
var float onh = na
var float onl = na
if startON
    onh := high
    onl := low
else if inON
    onh := math.max(nz(onh, high), high)
    onl := math.min(nz(onl, low), low)

var float tgt = na
var float stp = na
var bool held = false

// decided on the candle that closes at 09:30 (its close = the open); the market order fills at the 09:30 open
if inON and tc >= 930 and t < 930 and strategy.position_size == 0 and onh > onl
    rng = onh - onl
    mid = (onh + onl) / 2
    o = close
    pos = (o - onl) / rng
    if pos > 0 and pos < 1 and math.abs(pos - 0.5) * 100 >= deadPct
        d = o > mid ? 1 : -1
        tg = d > 0 ? onh : onl
        st = o - d * stopPart * rng
        risk = (o - st) * d
        ok = (side == "Both" or (side == "Long" and d > 0) or (side == "Short" and d < 0)) and risk > 0 and (minRR == 0 or (tg - o) * d >= minRR * risk)
        if ok
            tgt := tg
            stp := st
            held := false
            strategy.entry("ON", d > 0 ? strategy.long : strategy.short)
            if atTarget == "Take profit on touch"
                strategy.exit("x", "ON", stop = st, limit = tg, comment_loss = "Stop", comment_profit = "Target")
            else
                strategy.exit("x", "ON", stop = st, comment = "Stop")

if strategy.position_size != 0
    d = strategy.position_size > 0 ? 1 : -1
    if atTarget == "Hold if a candle closes beyond" and not held and (d > 0 ? high >= tgt : low <= tgt)
        if (close - tgt) * d > 0
            held := true
            ns = holdStop == "Overnight extreme" ? tgt : holdStop == "Entry" ? strategy.position_avg_price : stp
            stp := d > 0 ? math.max(ns, stp) : math.min(ns, stp)
            strategy.exit("x", "ON", stop = stp, comment = "Stop (held)")
        else
            strategy.close("ON", comment = "Target (close)", immediately = true)
    if strategy.position_size != 0 and t >= (held ? 1555 : exitAt)
        strategy.close("ON", comment = held ? "15:55" : "Time", immediately = true)

plot(strategy.position_size != 0 ? tgt : na, "Target", #089981, 1, plot.style_linebr)
plot(strategy.position_size != 0 ? stp : na, "Stop", #f23645, 1, plot.style_linebr)
`, { aliases: 'overnight midpoint onmid onh onl strategy backtest' }),

S('volume', 'Volume', 'Volume', 'Volume columns at the bottom of the price pane, colored by candle direction.', `
indicator("Volume", "Vol", overlay = true, format = format.volume, scale = scale.none)
upColor = input.color(color.new(#089981, 50), "Up color")
dnColor = input.color(color.new(#f23645, 50), "Down color")
plot(volume, "Volume", close >= open ? upColor : dnColor, style = plot.style_columns)
`, { aliases: 'vol' }),

S('sma', 'Moving Average Simple', 'Trend', 'Simple moving average of the source over N bars.', `
indicator("Moving Average Simple", "SMA", overlay = true)
len = input.int(20, "Length", minval = 1)
src = input.source(close, "Source")
col = input.color(#2962ff, "Color")
plot(ta.sma(src, len), "SMA", col, 2)
`, { aliases: 'ma simple moving average' }),

S('ema', 'Moving Average Exponential', 'Trend', 'Exponential moving average: reacts faster than the SMA.', `
indicator("Moving Average Exponential", "EMA", overlay = true)
len = input.int(21, "Length", minval = 1)
src = input.source(close, "Source")
col = input.color(#ff9800, "Color")
plot(ta.ema(src, len), "EMA", col, 2)
`, { aliases: 'ema exponential' }),

S('ribbon', 'EMA Ribbon 8 / 21 / 50 / 200', 'Trend', 'The four moving averages most traders watch, one look for trend alignment.', `
indicator("EMA Ribbon", "Ribbon", overlay = true)
plot(ta.ema(close, input.int(8, "Fast")), "EMA 8", #00bcd4, 1)
plot(ta.ema(close, input.int(21, "Medium")), "EMA 21", #2962ff, 1)
plot(ta.ema(close, input.int(50, "Slow")), "EMA 50", #ff9800, 2)
plot(ta.ema(close, input.int(200, "Long")), "EMA 200", #f23645, 2)
`, { aliases: 'moving averages ma' }),

S('hma', 'Hull Moving Average', 'Trend', 'Hull MA: smooth and low-lag, colored by slope.', `
indicator("Hull Moving Average", "HMA", overlay = true)
len = input.int(55, "Length", minval = 2)
h = ta.hma(close, len)
plot(h, "HMA", h > h[1] ? #089981 : #f23645, 2)
`),

S('vwma', 'Volume Weighted Moving Average', 'Volume', 'Moving average weighted by volume.', `
indicator("Volume Weighted Moving Average", "VWMA", overlay = true)
len = input.int(20, "Length", minval = 1)
plot(ta.vwma(close, len), "VWMA", #9c27b0, 2)
`),

S('vwap', 'VWAP with bands', 'Volume', 'Session VWAP (resets each New York day) with standard deviation bands.', `
indicator("VWAP", "VWAP", overlay = true)
mult = input.float(1.0, "Band multiplier", step = 0.5)
showBands = input.bool(true, "Show bands")
[vw, up, dn] = ta.vwap(hlc3, na, mult)
plot(vw, "VWAP", #e040fb, 2)
u = plot(showBands ? up : na, "Upper", color.new(#e040fb, 50))
l = plot(showBands ? dn : na, "Lower", color.new(#e040fb, 50))
fill(u, l, color.new(#e040fb, 92))
`, { aliases: 'volume weighted average price anchored' }),

S('bb', 'Bollinger Bands', 'Volatility', '20-period SMA with bands two standard deviations away.', `
indicator("Bollinger Bands", "BB", overlay = true)
len = input.int(20, "Length", minval = 1)
src = input.source(close, "Source")
mult = input.float(2.0, "StdDev", minval = 0.001, step = 0.1)
[basis, upper, lower] = ta.bb(src, len, mult)
plot(basis, "Basis", #ff6d00)
p1 = plot(upper, "Upper", #2962ff)
p2 = plot(lower, "Lower", #2962ff)
fill(p1, p2, color.rgb(33, 150, 243, 95), "Background")
`, { aliases: 'bollinger bands bb volatility' }),

S('kc', 'Keltner Channels', 'Volatility', 'EMA with bands at a multiple of the average true range.', `
indicator("Keltner Channels", "KC", overlay = true)
len = input.int(20, "Length")
mult = input.float(2.0, "Multiplier", step = 0.1)
[m, u, l] = ta.kc(close, len, mult)
plot(m, "Basis", #2962ff)
a = plot(u, "Upper", #2962ff)
b = plot(l, "Lower", #2962ff)
fill(a, b, color.rgb(33, 150, 243, 95))
`),

S('donchian', 'Donchian Channels', 'Volatility', 'Highest high and lowest low of the last N bars (Turtle breakout levels).', `
indicator("Donchian Channels", "DC", overlay = true)
len = input.int(20, "Length", minval = 1)
upper = ta.highest(len)
lower = ta.lowest(len)
plot(math.avg(upper, lower), "Basis", #ff6d00)
u = plot(upper, "Upper", #2962ff)
l = plot(lower, "Lower", #2962ff)
fill(u, l, color.rgb(33, 150, 243, 95))
`, { aliases: 'turtle breakout channel' }),

S('supertrend', 'Supertrend', 'Trend', 'ATR trailing stop that flips with the trend.', `
indicator("Supertrend", overlay = true)
atrPeriod = input.int(10, "ATR length", minval = 1)
factor = input.float(3.0, "Factor", minval = 0.01, step = 0.01)
[st, dir] = ta.supertrend(factor, atrPeriod)
st := barstate.isfirst ? na : st
body = plot((open + close) / 2, "Body middle", display = display.none)
up = plot(dir < 0 ? st : na, "Up trend", #089981, 2, plot.style_linebr)
dn = plot(dir < 0 ? na : st, "Down trend", #f23645, 2, plot.style_linebr)
fill(body, up, color.new(#089981, 90))
fill(body, dn, color.new(#f23645, 90))
`, { aliases: 'atr trailing stop trend' }),

S('psar', 'Parabolic SAR', 'Trend', 'Stop-and-reverse dots that trail price.', `
indicator("Parabolic SAR", "SAR", overlay = true)
start = input.float(0.02, "Start", step = 0.01)
inc = input.float(0.02, "Increment", step = 0.01)
mx = input.float(0.2, "Maximum", step = 0.01)
s = ta.sar(start, inc, mx)
plot(s, "SAR", s < low ? #089981 : #f23645, 1, plot.style_circles)
`),

S('ichimoku', 'Ichimoku Cloud', 'Trend', 'Conversion, base, lagging span and the cloud projected 26 bars ahead.', `
indicator("Ichimoku Cloud", "Ichimoku", overlay = true)
c1 = input.int(9, "Conversion")
b1 = input.int(26, "Base")
l2 = input.int(52, "Leading span B")
disp = input.int(26, "Displacement")
donchian(len) => math.avg(ta.lowest(len), ta.highest(len))
conv = donchian(c1)
base = donchian(b1)
leadA = math.avg(conv, base)
leadB = donchian(l2)
plot(conv, "Conversion", #2962ff)
plot(base, "Base", #b71c1c)
plot(close, "Lagging", #43a047, offset = 1 - disp)
p1 = plot(leadA, "Leading A", #a5d6a7, offset = disp - 1)
p2 = plot(leadB, "Leading B", #ef9a9a, offset = disp - 1)
fill(p1, p2, leadA > leadB ? color.rgb(67, 160, 71, 90) : color.rgb(244, 67, 54, 90))
`, { aliases: 'cloud kumo' }),

S('rsi', 'Relative Strength Index', 'Momentum', 'RSI with 70 / 30 bands.', `
indicator("Relative Strength Index", "RSI", format = format.price, precision = 2)
len = input.int(14, "Length", minval = 1)
src = input.source(close, "Source")
r = ta.rsi(src, len)
plot(r, "RSI", #7e57c2, 2)
up = hline(70, "Upper", #787b86)
hline(50, "Middle", color.new(#787b86, 50))
dn = hline(30, "Lower", #787b86)
fill(up, dn, color.rgb(126, 87, 194, 92), "Background")
`, { aliases: 'rsi oscillator overbought oversold' }),

S('macd', 'MACD', 'Momentum', 'Fast EMA minus slow EMA, its signal line and the histogram.', `
indicator("MACD", "MACD")
fast = input.int(12, "Fast length")
slow = input.int(26, "Slow length")
sig = input.int(9, "Signal length")
[m, s, h] = ta.macd(close, fast, slow, sig)
hline(0, "Zero", color.new(#787b86, 50))
plot(h, "Histogram", h >= 0 ? (h[1] < h ? #26a69a : #b2dfdb) : (h[1] < h ? #ffcdd2 : #ff5252), style = plot.style_columns)
plot(m, "MACD", #2962ff, 2)
plot(s, "Signal", #ff6d00, 2)
`, { aliases: 'moving average convergence divergence' }),

S('stoch', 'Stochastic', 'Momentum', 'Where the close sits in the recent high-low range, smoothed.', `
indicator("Stochastic", "Stoch", format = format.price, precision = 2)
len = input.int(14, "%K length", minval = 1)
sk = input.int(1, "%K smoothing", minval = 1)
sd = input.int(3, "%D smoothing", minval = 1)
k = ta.sma(ta.stoch(close, high, low, len), sk)
d = ta.sma(k, sd)
plot(k, "%K", #2962ff)
plot(d, "%D", #ff6d00)
a = hline(80, "Upper", #787b86)
b = hline(20, "Lower", #787b86)
fill(a, b, color.rgb(33, 150, 243, 92))
`),

S('cci', 'Commodity Channel Index', 'Momentum', 'Distance from the average in units of mean deviation.', `
indicator("Commodity Channel Index", "CCI")
len = input.int(20, "Length")
plot(ta.cci(hlc3, len), "CCI", #2962ff)
hline(100, "Upper", #787b86)
hline(0, "Middle", color.new(#787b86, 50))
hline(-100, "Lower", #787b86)
`),

S('adx', 'ADX / DMI', 'Momentum', 'Trend strength (ADX) with +DI and -DI.', `
indicator("Directional Movement Index", "DMI")
len = input.int(14, "DI length")
sm = input.int(14, "ADX smoothing")
[p, m, adx] = ta.dmi(len, sm)
plot(adx, "ADX", #f50057, 2)
plot(p, "+DI", #2962ff)
plot(m, "-DI", #ff6d00)
hline(25, "Trend threshold", color.new(#787b86, 40))
`, { aliases: 'adx dmi directional trend strength' }),

S('mom', 'Momentum', 'Momentum', 'Close minus the close N bars ago.', `
indicator("Momentum", "Mom")
len = input.int(10, "Length")
plot(ta.mom(close, len), "Momentum", #2962ff)
hline(0, "Zero", color.new(#787b86, 50))
`),

S('wpr', 'Williams %R', 'Momentum', 'Close relative to the high-low range, 0 to -100.', `
indicator("Williams %R", "%R")
len = input.int(14, "Length")
plot(ta.wpr(len), "%R", #7e57c2)
hline(-20, "Upper", #787b86)
hline(-80, "Lower", #787b86)
`),

S('atr', 'Average True Range', 'Volatility', 'Average bar range including gaps.', `
indicator("Average True Range", "ATR")
len = input.int(14, "Length", minval = 1)
plot(ta.atr(len), "ATR", #b71c1c)
`),

S('obv', 'On Balance Volume', 'Volume', 'Running total of volume signed by the close-to-close change.', `
indicator("On Balance Volume", "OBV", format = format.volume)
plot(ta.obv, "OBV", #2962ff)
`),

S('mfi', 'Money Flow Index', 'Volume', 'Volume-weighted RSI.', `
indicator("Money Flow Index", "MFI")
len = input.int(14, "Length")
plot(ta.mfi(hlc3, len), "MFI", #7e57c2)
hline(80, "Overbought", #787b86)
hline(20, "Oversold", #787b86)
`),

S('pdhl', 'Previous Day High / Low / Close', 'Levels', "Yesterday's high, low and close carried across today (intraday charts).", `
indicator("Previous Day High / Low", "PDH / PDL", overlay = true)
showClose = input.bool(true, "Show previous close")
newDay = dayofmonth != dayofmonth[1]
var float dh = na
var float dl = na
var float pdh = na
var float pdl = na
var float pdc = na
if newDay
    pdh := dh
    pdl := dl
    pdc := close[1]
    dh := high
    dl := low
else
    dh := math.max(nz(dh, high), high)
    dl := math.min(nz(dl, low), low)
ok = timeframe.isintraday
plot(ok ? pdh : na, "PDH", #ff9800, 1, plot.style_stepline)
plot(ok ? pdl : na, "PDL", #ff9800, 1, plot.style_stepline)
plot(ok and showClose ? pdc : na, "PDC", color.new(#ff9800, 50), 1, plot.style_stepline)
`, { aliases: 'pdh pdl previous day key levels' }),

S('orb', 'Opening Range', 'Levels', 'High and low of the first minutes after the 9:30 New York open.', `
indicator("Opening Range", "ORB", overlay = true)
mins = input.int(30, "Opening range minutes", minval = 1)
t = hour * 60 + minute
inOR = t >= 570 and t < 570 + mins
newSess = inOR and not inOR[1]
var float orh = na
var float orl = na
if newSess
    orh := high
    orl := low
else if inOR
    orh := math.max(orh, high)
    orl := math.min(orl, low)
show = timeframe.isintraday and t >= 570 and t < 960
a = plot(show ? orh : na, "OR high", #00bcd4, 2, plot.style_stepline)
b = plot(show ? orl : na, "OR low", #00bcd4, 2, plot.style_stepline)
fill(a, b, color.new(#00bcd4, 92))
`, { aliases: 'orb opening range breakout' }),

S('pivots', 'Pivot Points Classic (daily)', 'Levels', 'Floor-trader pivots from the previous day: P, R1-R2, S1-S2.', `
indicator("Pivot Points Classic", "Pivots", overlay = true)
newDay = dayofmonth != dayofmonth[1]
var float dh = na
var float dl = na
var float p = na
var float r1 = na
var float s1 = na
var float r2 = na
var float s2 = na
if newDay
    if not na(dh)
        pc = close[1]
        p := (dh + dl + pc) / 3
        r1 := 2 * p - dl
        s1 := 2 * p - dh
        r2 := p + (dh - dl)
        s2 := p - (dh - dl)
    dh := high
    dl := low
else
    dh := math.max(dh, high)
    dl := math.min(dl, low)
plot(p, "P", #ffeb3b, 1, plot.style_stepline)
plot(r1, "R1", #f23645, 1, plot.style_stepline)
plot(r2, "R2", color.new(#f23645, 40), 1, plot.style_stepline)
plot(s1, "S1", #089981, 1, plot.style_stepline)
plot(s2, "S2", color.new(#089981, 40), 1, plot.style_stepline)
`, { aliases: 'floor pivots support resistance' }),

S('pivotshl', 'Pivot Points High Low', 'Structure', 'Labels every confirmed swing high and low.', `
indicator("Pivot Points High Low", "Pivots HL", overlay = true, max_labels_count = 500)
lb = input.int(10, "Left bars", minval = 1)
rb = input.int(10, "Right bars", minval = 1)
ph = ta.pivothigh(lb, rb)
pl = ta.pivotlow(lb, rb)
if not na(ph)
    label.new(bar_index - rb, ph, str.tostring(ph, format.mintick), style = label.style_label_down, color = color.new(#f23645, 30), textcolor = color.white, size = size.small)
if not na(pl)
    label.new(bar_index - rb, pl, str.tostring(pl, format.mintick), style = label.style_label_up, color = color.new(#089981, 30), textcolor = color.white, size = size.small)
`, { aliases: 'swing high low pivots' }),

S('smc', 'Smart Money Concepts', 'SMC / ICT', 'Market structure (BOS / CHoCH), equal highs / lows and order blocks from swing pivots.', `
indicator("Smart Money Concepts", "SMC", overlay = true, max_lines_count = 500, max_labels_count = 500, max_boxes_count = 500)
len = input.int(10, "Swing length", minval = 2)
showEq = input.bool(true, "Equal highs / lows")
eqThr = input.float(0.1, "Equal threshold (x ATR)", step = 0.05)
showOB = input.bool(true, "Order blocks")
obCount = input.int(5, "Order blocks to keep", minval = 1)
bull = input.color(#089981, "Bullish")
bear = input.color(#f23645, "Bearish")

atr = ta.atr(200)
ph = ta.pivothigh(high, len, len)
pl = ta.pivotlow(low, len, len)

var float topY = na
var int topX = na
var bool topCrossed = true
var float btmY = na
var int btmX = na
var bool btmCrossed = true
var int trend = 0
var bullOBs = array.new_box()
var bearOBs = array.new_box()

if not na(ph)
    if showEq and not na(topY) and math.abs(ph - topY) < eqThr * atr
        line.new(topX, topY, bar_index - len, ph, color = bear, style = line.style_dotted)
        label.new(bar_index - len, ph, "EQH", color = color.new(bear, 100), textcolor = bear, style = label.style_label_down, size = size.tiny)
    topY := ph
    topX := bar_index - len
    topCrossed := false

if not na(pl)
    if showEq and not na(btmY) and math.abs(pl - btmY) < eqThr * atr
        line.new(btmX, btmY, bar_index - len, pl, color = bull, style = line.style_dotted)
        label.new(bar_index - len, pl, "EQL", color = color.new(bull, 100), textcolor = bull, style = label.style_label_up, size = size.tiny)
    btmY := pl
    btmX := bar_index - len
    btmCrossed := false

if not topCrossed and close > topY
    tag = trend < 0 ? "CHoCH" : "BOS"
    line.new(topX, topY, bar_index, topY, color = bull, style = line.style_dashed)
    label.new(math.round((topX + bar_index) / 2), topY, tag, color = color.new(bull, 100), textcolor = bull, style = label.style_label_down, size = size.tiny)
    if showOB
        float obTop = na
        float obBtm = na
        int obX = na
        for i = 1 to math.min(bar_index - topX, 60)
            if close[i] < open[i] and (na(obBtm) or low[i] < obBtm)
                obTop := high[i]
                obBtm := low[i]
                obX := bar_index - i
        if not na(obTop)
            bullOBs.push(box.new(obX, obTop, bar_index, obBtm, border_color = color.new(bull, 60), bgcolor = color.new(bull, 85), extend = extend.right))
            if bullOBs.size() > obCount
                bullOBs.shift().delete()
    topCrossed := true
    trend := 1

if not btmCrossed and close < btmY
    tag = trend > 0 ? "CHoCH" : "BOS"
    line.new(btmX, btmY, bar_index, btmY, color = bear, style = line.style_dashed)
    label.new(math.round((btmX + bar_index) / 2), btmY, tag, color = color.new(bear, 100), textcolor = bear, style = label.style_label_up, size = size.tiny)
    if showOB
        float obTop = na
        float obBtm = na
        int obX = na
        for i = 1 to math.min(bar_index - btmX, 60)
            if close[i] > open[i] and (na(obTop) or high[i] > obTop)
                obTop := high[i]
                obBtm := low[i]
                obX := bar_index - i
        if not na(obTop)
            bearOBs.push(box.new(obX, obTop, bar_index, obBtm, border_color = color.new(bear, 60), bgcolor = color.new(bear, 85), extend = extend.right))
            if bearOBs.size() > obCount
                bearOBs.shift().delete()
    btmCrossed := true
    trend := -1

// mitigated order blocks are removed
if bullOBs.size() > 0
    for i = bullOBs.size() - 1 to 0
        b = bullOBs.get(i)
        if close < b.get_bottom()
            b.delete()
            bullOBs.remove(i)
if bearOBs.size() > 0
    for i = bearOBs.size() - 1 to 0
        b = bearOBs.get(i)
        if close > b.get_top()
            b.delete()
            bearOBs.remove(i)
`, { aliases: 'smc ict bos choch order blocks market structure eqh eql' }),

S('fvg', 'Fair Value Gaps', 'SMC / ICT', 'Three-candle imbalances, removed once price fills them.', `
indicator("Fair Value Gaps", "FVG", overlay = true, max_boxes_count = 200)
ext = input.int(15, "Extend (bars)", minval = 1)
minGap = input.float(0.1, "Minimum gap (x ATR)", step = 0.05)
keep = input.int(10, "Gaps to keep", minval = 1)
atr = ta.atr(14)
var bulls = array.new_box()
var bears = array.new_box()
if low > high[2] and (low - high[2]) > minGap * atr
    bulls.push(box.new(bar_index - 2, low, bar_index + ext, high[2], border_color = color.new(#089981, 100), bgcolor = color.new(#089981, 78), text = "FVG", text_color = color.new(#089981, 30), text_size = size.tiny))
    if bulls.size() > keep
        bulls.shift().delete()
if high < low[2] and (low[2] - high) > minGap * atr
    bears.push(box.new(bar_index - 2, low[2], bar_index + ext, high, border_color = color.new(#f23645, 100), bgcolor = color.new(#f23645, 78), text = "FVG", text_color = color.new(#f23645, 30), text_size = size.tiny))
    if bears.size() > keep
        bears.shift().delete()
if bulls.size() > 0
    for i = bulls.size() - 1 to 0
        b = bulls.get(i)
        if low <= b.get_bottom()
            b.delete()
            bulls.remove(i)
if bears.size() > 0
    for i = bears.size() - 1 to 0
        b = bears.get(i)
        if high >= b.get_top()
            b.delete()
            bears.remove(i)
`, { aliases: 'fvg imbalance ict gap' }),

S('emacross', 'EMA Cross Signals', 'Signals', 'Buy / sell labels when the fast EMA crosses the slow EMA.', `
indicator("EMA Cross Signals", "EMA X", overlay = true)
f = input.int(9, "Fast")
s = input.int(21, "Slow")
fast = ta.ema(close, f)
slow = ta.ema(close, s)
plot(fast, "Fast", #00bcd4)
plot(slow, "Slow", #ff9800)
plotshape(ta.crossover(fast, slow), "Buy", shape.labelup, location.belowbar, #089981, text = "Buy", textcolor = color.white, size = size.small)
plotshape(ta.crossunder(fast, slow), "Sell", shape.labeldown, location.abovebar, #f23645, text = "Sell", textcolor = color.white, size = size.small)
`, { aliases: 'crossover signals buy sell' }),

S('rsidiv', 'RSI Extremes', 'Signals', 'Marks bars where RSI leaves overbought / oversold.', `
indicator("RSI Extremes", "RSI X", overlay = true)
len = input.int(14, "RSI length")
ob = input.int(70, "Overbought")
os = input.int(30, "Oversold")
r = ta.rsi(close, len)
plotshape(ta.crossunder(r, ob), "Leaves overbought", shape.triangledown, location.abovebar, #f23645, size = size.tiny)
plotshape(ta.crossover(r, os), "Leaves oversold", shape.triangleup, location.belowbar, #089981, size = size.tiny)
`),

S('insidebar', 'Inside Bars', 'Signals', 'Colors bars whose range sits inside the previous bar.', `
indicator("Inside Bars", overlay = true)
inside = high < high[1] and low > low[1]
barcolor(inside ? #fdd835 : na)
`),

S('sessions', 'Session Background', 'Levels', 'Shades pre-market and after-hours on intraday charts.', `
indicator("Session Background", "Sessions", overlay = true)
t = hour * 60 + minute
pre = t >= 240 and t < 570
post = t >= 960 and t < 1200
bgcolor(timeframe.isintraday and pre ? color.new(#2962ff, 93) : na)
bgcolor(timeframe.isintraday and post ? color.new(#ff9800, 93) : na)
`, { aliases: 'premarket after hours extended' }),
];

export const DEFAULT_SCRIPT = `//@version=6
indicator("My script", overlay = true)
len = input.int(20, "Length")
plot(ta.ema(close, len), "EMA", color.orange, 2)
`;

export function findScript(q) {
  q = String(q || '').toLowerCase().trim();
  return LIBRARY.find(s => s.id === q || s.name.toLowerCase() === q) || LIBRARY.find(s => (s.name + ' ' + (s.aliases || '') + ' ' + s.id).toLowerCase().includes(q));
}
