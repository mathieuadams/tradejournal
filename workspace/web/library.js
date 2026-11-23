// Built-in indicator library: every indicator is a Pine script run by pine.js, so users can open any of them
// in the editor, read it, and fork it.
const V = '//@version=6\n';
const S = (id, name, cat, desc, src, extra = {}) => ({ id, name, cat, desc, src: V + src.trim() + '\n', builtin: true, ...extra });

export const CATEGORIES = ['Strategies', 'Trend', 'Momentum', 'Volatility', 'Volume', 'Levels', 'Structure', 'SMC / ICT', 'Signals'];

export const LIBRARY = [
S('asiacrt', 'Asian Range CRT (sweep + MSS + FVG)', 'Strategies', 'Asian range 20:00-00:00 (high, low, midnight open, 50%), bias from the hourly 20 EMA, London 02:00-05:00 sweep of the side against the bias, proof = close back inside + break of the previous swing (MSS), entry at the fair value gap the displacement leaves (size optimizable), stop beyond the sweep wick, TP1 the other side of the box, TP2 +1x range. For the 15-minute chart.', `
// Asian Range CRT — 15-minute chart (NQ / ES / QQQ / SPY / forex), New York time.
//  1. Range: the Asian session 20:00-00:00 (candle 1). At midnight: Asian high, Asian low, the midnight open and the 50%.
//  2. Bias: bullish / bearish day from the hourly 20 EMA (price on its side, sloping that way), the 4-hour 20 EMA,
//     the previous day's candle, or none.
//  3. Sweep (candle 2): in London (02:00-05:00) price trades through one side of the box and takes its stops.
//     Bullish bias: the Asian low is swept -> look for longs. Bearish bias: the Asian high -> shorts.
//  4. Proof: price snaps back - a candle closes back inside the box AND breaks the previous swing high (MSS,
//     displacement); optionally its body must be at least x the average candle.
//  5. Entry: the fair value gap the displacement leaves behind (size optimizable): a limit order at the gap
//     (its near edge, middle or far edge). Stop below the sweep wick. TP1 the other side of the box (Asian high),
//     TP2 one more range beyond (+1x range). Break-even after TP1 (optional). Flat at the time exit.
strategy("Asian Range CRT", "Asia CRT", overlay = true, initial_capital = 100000, default_qty_type = strategy.fixed, default_qty_value = 2,
     commission_type = strategy.commission.cash_per_contract, commission_value = 2.25, slippage = 1, max_boxes_count = 300, max_lines_count = 500)

grpR = "Range"
rStart   = input.int(2000, "Asian range from (HHMM)", group = grpR)
rEnd     = input.int(0, "to (HHMM, 0 = midnight)", group = grpR)
grpB = "Bias"
biasMode = input.string("Hourly 20 EMA", "Bias", options = ["Hourly 20 EMA", "4-hour 20 EMA", "Previous day candle", "Midnight open vs 50%", "None"], group = grpB)
emaLen   = input.int(20, "EMA length", minval = 2, group = grpB)
needSlope = input.bool(true, "EMA must slope the bias way", group = grpB)
side     = input.string("Both", "Side", options = ["Both", "Long", "Short"], group = grpB)
grpS = "Sweep (London)"
sStart   = input.int(200, "Sweep window from (HHMM)", group = grpS)
sEnd     = input.int(500, "to (HHMM)", group = grpS)
minSweep = input.int(0, "Sweep at least (ticks beyond the box)", minval = 0, group = grpS)
maxSweep = input.float(0, "Sweep at most (x the range, 0 = off)", step = 0.1, group = grpS)
grpP = "Proof (displacement + MSS)"
needInside = input.bool(true, "Close back inside the box", group = grpP)
needMss  = input.bool(true, "Break the previous swing high / low (MSS)", group = grpP)
swingLen = input.int(2, "Swing length (candles each side)", minval = 1, group = grpP)
dispMult = input.float(0, "Displacement candle body at least (x avg candle, 0 = off)", step = 0.1, group = grpP)
avgLen   = input.int(20, "Average candle over", minval = 3, group = grpP)
proofBy  = input.int(700, "Proof by (HHMM)", group = grpP)
grpF = "Fair value gap entry"
minGap   = input.int(4, "Gap at least (ticks)", minval = 0, group = grpF)
maxGapR  = input.float(0, "Gap at most (x the range, 0 = off)", step = 0.05, group = grpF)
fvgWait  = input.int(4, "Gap must form within (candles after the proof)", minval = 0, group = grpF)
entryAt  = input.string("Near edge", "Limit at the gap's", options = ["Near edge", "Middle", "Far edge"], group = grpF)
cancelAt = input.int(800, "Cancel the unfilled order at (HHMM)", group = grpF)
grpX = "Stop / targets"
bufTicks = input.int(2, "Stop beyond the sweep wick (ticks)", minval = 0, group = grpX)
tp2Mult  = input.float(1.0, "TP2: the other side + x the range", step = 0.25, group = grpX)
tp1Pct   = input.int(50, "TP1 closes (% of the position, 0 = TP2 only)", minval = 0, maxval = 100, group = grpX)
beAfter  = input.bool(true, "Stop to entry after TP1", group = grpX)
minRR    = input.float(1.0, "Skip when TP1 is under (R, 0 = off)", step = 0.25, group = grpX)
exitAt   = input.int(1130, "Flat at (HHMM)", group = grpX)
maxDay   = input.int(1, "Trades per day", minval = 1, group = grpX)
showBox  = input.bool(true, "Draw the Asian range, its high / low, the midnight open and 50%")
rangeOnly = input.bool(false, "Show the range only (no trades)")

tick = syminfo.mintick
hm = hour * 100 + minute
inWin(a, b) => a <= b ? (hm >= a and hm < b) : (hm >= a or hm < b)

// ---- higher-timeframe EMA (its live value at this candle's close) and slope, from this chart's candles
emaTrack(n) =>
    var float done = na
    var float last = na
    var float key = na
    k = math.floor(time / (n * 60000))
    a = 2.0 / (emaLen + 1)
    if na(key) or k != key
        if not na(last)
            done := na(done) ? last : a * last + (1 - a) * done
        key := k
    last := close
    now = na(done) ? close : a * close + (1 - a) * done
    [now, not na(done) and now > done, not na(done) and now < done]
[e60, up60, dn60] = emaTrack(60)
[e240, up240, dn240] = emaTrack(240)

// ---- previous calendar day's candle (New York), for the "previous day" bias
var float dO = na
var float dC = na
var float pdO = na
var float pdC = na
newDay = dayofmonth != dayofmonth[1]
if newDay
    pdO := dO
    pdC := dC
    dO := open
dC := close

// ---- the Asian box
inRange = inWin(rStart, rEnd)
rangeStart = inRange and not inRange[1]
rangeDone = not inRange and inRange[1]
var float bH = na
var float bL = na
var float aH = na
var float aL = na
var float mOpen = na
var bool ready = false
var int trades = 0
// setup state, per side
var bool sweptL = false
var bool sweptS = false
var float extL = na              // the sweep wick (lowest low after the low was swept)
var float extS = na
var float mssL = na              // the swing high to break (the last one before the sweep wick)
var float mssS = na
var int proofL = na              // bar of the proof
var int proofS = na
var int extBarL = na
var int extBarS = na
var bool doneL = false
var bool doneS = false
var float lastSH = na
var float lastSL = na
var box bx = na                  // the Asian range itself (20:00-00:00 candles)
var line lnH = na                // after midnight: Asian high, low, midnight open, 50%
var line lnL = na
var line lnMo = na
var line ln50 = na
var label lbH = na
var label lbL = na
var label lbMo = na
var label lb50 = na

ph = ta.pivothigh(high, swingLen, swingLen)
pl = ta.pivotlow(low, swingLen, swingLen)
if not na(ph)
    lastSH := ph
if not na(pl)
    lastSL := pl

if rangeStart
    bH := high
    bL := low
    ready := false
    trades := 0
    sweptL := false
    sweptS := false
    doneL := false
    doneS := false
    extL := na
    extS := na
    proofL := na
    proofS := na
    strategy.cancel("L")
    strategy.cancel("S")
    if showBox
        bx := box.new(bar_index, bH, bar_index, bL, border_color = color.new(#5B8DEF, 10), bgcolor = color.new(#5B8DEF, 82), text = "ASIA", text_color = color.new(#5B8DEF, 30), text_size = size.small)
else if inRange
    bH := math.max(bH, high)
    bL := math.min(bL, low)
    if showBox and not na(bx)
        box.set_top(bx, bH)
        box.set_bottom(bx, bL)
        box.set_right(bx, bar_index)
if rangeDone and not na(bH)
    aH := bH
    aL := bL
    mOpen := open
    ready := true
    if showBox
        lnH := line.new(bar_index, aH, bar_index + 1, aH, color = color.new(#5B8DEF, 0), width = 1)
        lnL := line.new(bar_index, aL, bar_index + 1, aL, color = color.new(#5B8DEF, 0), width = 1)
        lnMo := line.new(bar_index, mOpen, bar_index + 1, mOpen, color = color.new(color.white, 30), style = line.style_dashed)
        ln50 := line.new(bar_index, (aH + aL) / 2, bar_index + 1, (aH + aL) / 2, color = color.new(#8a8f9c, 20), style = line.style_dotted)
        lbH := label.new(bar_index, aH, "Asian high " + str.tostring(aH, format.mintick), style = label.style_label_down, color = color.new(#5B8DEF, 70), textcolor = color.white, size = size.tiny)
        lbL := label.new(bar_index, aL, "Asian low " + str.tostring(aL, format.mintick), style = label.style_label_up, color = color.new(#5B8DEF, 70), textcolor = color.white, size = size.tiny)
        lbMo := label.new(bar_index + 1, mOpen, "Midnight open", style = label.style_label_left, color = color.new(color.white, 85), textcolor = color.white, size = size.tiny)
        lb50 := label.new(bar_index + 1, (aH + aL) / 2, "50%", style = label.style_label_left, color = color.new(#8a8f9c, 80), textcolor = color.white, size = size.tiny)
if ready and showBox and not inRange and hm < exitAt
    line.set_x2(lnH, bar_index)
    line.set_x2(lnL, bar_index)
    line.set_x2(lnMo, bar_index)
    line.set_x2(ln50, bar_index)
    label.set_x(lbMo, bar_index)
    label.set_x(lb50, bar_index)

rng = aH - aL
mid = (aH + aL) / 2
avgC = ta.sma(math.abs(close - open), avgLen)[1]

// ---- bias
bias = 0
if biasMode == "Hourly 20 EMA"
    bias := close > e60 and (not needSlope or up60) ? 1 : close < e60 and (not needSlope or dn60) ? -1 : 0
else if biasMode == "4-hour 20 EMA"
    bias := close > e240 and (not needSlope or up240) ? 1 : close < e240 and (not needSlope or dn240) ? -1 : 0
else if biasMode == "Previous day candle"
    bias := na(pdO) ? 0 : pdC > pdO ? 1 : pdC < pdO ? -1 : 0
else if biasMode == "Midnight open vs 50%"
    bias := na(mOpen) ? 0 : mOpen > mid ? 1 : mOpen < mid ? -1 : 0
else
    bias := 2                    // none: both ways

canTrade = ready and not rangeOnly and trades < maxDay and strategy.position_size == 0 and rng > 0
sweepWin = inWin(sStart, sEnd)
beforeProof = hm < proofBy and not inRange

// ---- long side: the Asian low swept, back inside, MSS up, then the bullish gap
if canTrade and not doneL and side != "Short" and (bias == 1 or bias == 2)
    if not sweptL and sweepWin and low < aL - minSweep * tick
        sweptL := true
        extL := low
        extBarL := bar_index
        mssL := lastSH
    else if sweptL and na(proofL) and low < extL
        extL := low
        extBarL := bar_index
        mssL := lastSH
    if sweptL and maxSweep > 0 and aL - extL > maxSweep * rng
        doneL := true
    if sweptL and na(proofL) and not doneL and beforeProof
        okIn = not needInside or close > aL
        okMss = not needMss or (not na(mssL) and close > mssL)
        okDisp = dispMult <= 0 or math.abs(close - open) >= dispMult * avgC
        if okIn and okMss and okDisp and close > open
            proofL := bar_index
    if sweptL and not na(proofL) and not doneL and strategy.opentrades == 0
        // the first bullish gap of the leg up from the sweep wick (the displacement's gap), within fvgWait candles after the proof
        if bar_index - proofL <= fvgWait
            float gTop = na
            float gBot = na
            back = math.min(bar_index - extBarL, 50)
            if back >= 2
                for i = back - 2 to 0
                    if na(gTop) and low[i] - high[i + 2] >= math.max(minGap, 1) * tick and (maxGapR <= 0 or low[i] - high[i + 2] <= maxGapR * rng)
                        gTop := low[i]
                        gBot := high[i + 2]
            if not na(gTop)
                px = entryAt == "Near edge" ? gTop : entryAt == "Middle" ? (gTop + gBot) / 2 : gBot
                stp = extL - bufTicks * tick
                tp1 = aH
                tp2 = aH + tp2Mult * rng
                risk = px - stp
                if risk > 0 and tp1 > px and (minRR <= 0 or (tp1 - px) >= minRR * risk)
                    strategy.entry("L", strategy.long, limit = px)
                    if tp1Pct > 0 and tp1Pct < 100
                        strategy.exit("L TP1", "L", qty_percent = tp1Pct, limit = tp1, stop = stp, comment_profit = "TP1 Asian high", comment_loss = "Stop")
                        strategy.exit("L TP2", "L", limit = tp2, stop = stp, comment_profit = "TP2", comment_loss = "Stop")
                    else
                        strategy.exit("L TP2", "L", limit = tp1Pct >= 100 ? tp1 : tp2, stop = stp, comment_profit = "Target", comment_loss = "Stop")
                    doneL := true
                else
                    doneL := true
        else
            doneL := true

// ---- short side: the Asian high swept, back inside, MSS down, then the bearish gap
if canTrade and not doneS and side != "Long" and (bias == -1 or bias == 2)
    if not sweptS and sweepWin and high > aH + minSweep * tick
        sweptS := true
        extS := high
        extBarS := bar_index
        mssS := lastSL
    else if sweptS and na(proofS) and high > extS
        extS := high
        extBarS := bar_index
        mssS := lastSL
    if sweptS and maxSweep > 0 and extS - aH > maxSweep * rng
        doneS := true
    if sweptS and na(proofS) and not doneS and beforeProof
        okIn = not needInside or close < aH
        okMss = not needMss or (not na(mssS) and close < mssS)
        okDisp = dispMult <= 0 or math.abs(close - open) >= dispMult * avgC
        if okIn and okMss and okDisp and close < open
            proofS := bar_index
    if sweptS and not na(proofS) and not doneS and strategy.opentrades == 0
        if bar_index - proofS <= fvgWait
            float gTop = na
            float gBot = na
            back = math.min(bar_index - extBarS, 50)
            if back >= 2
                for i = back - 2 to 0
                    if na(gTop) and low[i + 2] - high[i] >= math.max(minGap, 1) * tick and (maxGapR <= 0 or low[i + 2] - high[i] <= maxGapR * rng)
                        gTop := low[i + 2]
                        gBot := high[i]
            if not na(gTop)
                px = entryAt == "Near edge" ? gBot : entryAt == "Middle" ? (gTop + gBot) / 2 : gTop
                stp = extS + bufTicks * tick
                tp1 = aL
                tp2 = aL - tp2Mult * rng
                risk = stp - px
                if risk > 0 and tp1 < px and (minRR <= 0 or (px - tp1) >= minRR * risk)
                    strategy.entry("S", strategy.short, limit = px)
                    if tp1Pct > 0 and tp1Pct < 100
                        strategy.exit("S TP1", "S", qty_percent = tp1Pct, limit = tp1, stop = stp, comment_profit = "TP1 Asian low", comment_loss = "Stop")
                        strategy.exit("S TP2", "S", limit = tp2, stop = stp, comment_profit = "TP2", comment_loss = "Stop")
                    else
                        strategy.exit("S TP2", "S", limit = tp1Pct >= 100 ? tp1 : tp2, stop = stp, comment_profit = "Target", comment_loss = "Stop")
                    doneS := true
                else
                    doneS := true
        else
            doneS := true

// ---- the working order: cancel it at cancelAt, or when price reaches TP1 or the stop before it fills
var float wStop = na
if strategy.position_size == 0
    if hm >= cancelAt and hm < rStart
        strategy.cancel("L")
        strategy.cancel("S")
    if not na(aH) and (high >= aH)
        strategy.cancel("L")
    if not na(aL) and (low <= aL)
        strategy.cancel("S")
    if sweptL and low < extL - bufTicks * tick
        strategy.cancel("L")
    if sweptS and high > extS + bufTicks * tick
        strategy.cancel("S")

// ---- in the trade: count it, break-even after TP1, flat at exitAt
var float entryPx = na
var float fullQty = na
if strategy.position_size != 0 and nz(fullQty) == 0
    trades += 1
    entryPx := strategy.position_avg_price
    fullQty := math.abs(strategy.position_size)
if strategy.position_size == 0
    fullQty := na
if beAfter and strategy.position_size != 0 and math.abs(strategy.position_size) < nz(fullQty)
    if strategy.position_size > 0
        strategy.exit("L TP2", "L", limit = aH + tp2Mult * rng, stop = entryPx, comment_profit = "TP2", comment_loss = "Break-even")
    else
        strategy.exit("S TP2", "S", limit = aL - tp2Mult * rng, stop = entryPx, comment_profit = "TP2", comment_loss = "Break-even")
endHM = hour(time_close) * 100 + minute(time_close)
if strategy.position_size != 0 and ((endHM >= exitAt and hm < rStart) or (hm < exitAt and endHM > exitAt))
    strategy.close_all(comment = "Time", immediately = true)

plotshape(sweptL and sweptL != sweptL[1], "Sweep low", shape.triangleup, location.belowbar, #f23645, size = size.tiny, text = "sweep")
plotshape(sweptS and sweptS != sweptS[1], "Sweep high", shape.triangledown, location.abovebar, #f23645, size = size.tiny, text = "sweep")
plotshape(not na(proofL) and proofL == bar_index, "MSS up", shape.labelup, location.belowbar, #E7C35A, size = size.tiny, text = "MSS")
plotshape(not na(proofS) and proofS == bar_index, "MSS down", shape.labeldown, location.abovebar, #E7C35A, size = size.tiny, text = "MSS")
plot(biasMode == "Hourly 20 EMA" ? e60 : biasMode == "4-hour 20 EMA" ? e240 : na, "Bias EMA", #E07AE0, 1)
`, { aliases: 'crt candle range theory asia asian range london sweep liquidity mss displacement fvg midnight open strategy' }),
S('asiavwap', 'Asian rejection + VWAP (iFVG #2 bias)', 'Strategies', 'Asian range 20:00-00:00 high / low / 50% rejected as support or resistance (both sides), bias from the hourly 20 EMA as iFVG #2 (or VWAP / 15m EMA), entry on the bias side of the VWAP, stop at the range edge or other choices, trailing stop, hold to the close, bias flip reverses. 15-minute chart.', `
// Asian rejection + VWAP.
//   Levels: the Asian range high, low and 50% (20:00-00:00), used from both sides:
//     held as support (rejected from above) -> long setup; held as resistance (rejected from below) -> short setup.
//   Long: a support rejection first, then bias up and the entry filter (close above the VWAP / 15m EMA).
//   Short: mirror. Stop, trailing stop, target and the bias are inputs (all can be optimized).
//   A bias flip closes the trade and takes the other side right away if its conditions are met.
strategy("Asian rejection + VWAP (iFVG #2 bias)", "Asia rej VWAP", overlay = true, initial_capital = 100000, default_qty_type = strategy.fixed, default_qty_value = 1,
     process_orders_on_close = true, commission_type = strategy.commission.cash_per_contract, commission_value = 2.25, slippage = 1, max_boxes_count = 500, max_labels_count = 500)

grpT = "Times (New York)"
rStart    = input.int(2000, "Asian range from", group = grpT)
rEnd      = input.int(0, "to (0 = midnight)", group = grpT)
lastEntry = input.int(1200, "Last entry", group = grpT)
flatAt    = input.int(1600, "Flat at (the close of the candle ending then)", group = grpT)

grpB = "Bias"
biasSrc  = input.string("Hourly 20 EMA (as iFVG #2)", "Bias from", options = ["Hourly 20 EMA (as iFVG #2)", "15 / 30 / 60-minute 20 EMAs (as iFVG #2)", "VWAP", "15m 21 EMA"], group = grpB)
asiaMode = input.string("It closed in the bias direction", "Asian session (18:00-02:00) check", options = ["It closed in the bias direction", "Price on the bias side of its middle", "None"], group = grpB)
asiaLift = input.bool(true, "A bias change during the day lifts the Asian check (for the new side)", group = grpB)
flipExit = input.bool(true, "Bias flip: close the trade (and reverse if the other side is ready)", group = grpB)

grpR = "Rejection"
levels   = input.string("Asian high, low and 50%", "Levels", options = ["Asian high, low and 50%", "Asian high and low only", "50% only"], group = grpR)
bothWays = input.bool(true, "Use each level from both sides (a broken level retested from the other side)", group = grpR)
rejMode  = input.string("Wick: high/low reaches the level, closes back", "Rejection", options = ["Wick: high/low reaches the level, closes back", "Close: closes through, then closes back", "None (bias + entry filter only)"], group = grpR)
tolTk    = input.int(4, "Wick: within (ticks of the level)", minval = 0, group = grpR)

grpE = "Entry and exits"
entryRef = input.string("VWAP", "Entry: close on the bias side of", options = ["VWAP", "15m 21 EMA", "VWAP and 15m 21 EMA", "Off"], group = grpE)
side     = input.string("Both", "Side", options = ["Both", "Long", "Short"], group = grpE)
maxDay   = input.int(0, "At most per day (0 = no limit)", minval = 0, group = grpE)
stopMode = input.string("Asian range edge", "Stop", options = ["Asian range edge", "The rejected level", "Rejection wick", "Asian 50%", "Fixed (ticks)"], group = grpE)
bufTk    = input.int(4, "Stop buffer (ticks)", minval = 0, group = grpE)
stopTk   = input.int(80, "Fixed stop (ticks)", minval = 1, group = grpE)
trail    = input.string("Off", "Trailing stop", options = ["Off", "VWAP", "15m 21 EMA", "Previous candle", "Fixed (ticks)"], group = grpE)
trailTk  = input.int(80, "Trailing: fixed distance (ticks)", minval = 1, group = grpE)
trailAt  = input.float(0, "Trailing: start after (R in profit, 0 = at once)", step = 0.5, group = grpE)
tgtR     = input.float(0, "Target (R, 0 = hold to the close)", step = 0.5, group = grpE)
showLv   = input.bool(true, "Show the Asian box, levels and VWAP")
showWhy  = input.bool(true, "Show why a rejection did not trade")

tick = syminfo.mintick
hm = hour * 100 + minute

// ---- EMAs of the forming 15 / 30 / 60-minute candle (their live value at this candle's close) and slope
emaTrack(n, len) =>
    var float done = na
    var float last = na
    var float key = na
    a = 2.0 / (len + 1)
    k = math.floor(time / (n * 60000))
    if na(key) or k != key
        if not na(last)
            done := na(done) ? last : a * last + (1 - a) * done
        key := k
    last := close
    now = na(done) ? close : a * close + (1 - a) * done
    [now, not na(done) and now > done, not na(done) and now < done]
[e15, up15, dn15] = emaTrack(15, 20)
[e30, up30, dn30] = emaTrack(30, 20)
[e60, up60, dn60] = emaTrack(60, 20)
[m21, mUp, mDn] = emaTrack(15, 21)

// ---- Asian range (rStart-rEnd): box, then its high / low / 50% until the flat time
inWin(a, b) => a <= b ? (hm >= a and hm < b) : (hm >= a or hm < b)
inRange = inWin(rStart, rEnd)
rangeStart = inRange and not inRange[1]
rangeDone = not inRange and inRange[1]

// VWAP from the Asian range start
var float vPV = 0
var float vV = 0
if rangeStart
    vPV := 0
    vV := 0
vPV += hlc3 * nz(volume)
vV += nz(volume)
vwap = vV > 0 ? vPV / vV : close

// bias
biasL = switch biasSrc
    "VWAP" => close > vwap
    "15m 21 EMA" => close > m21 and mUp
    "Hourly 20 EMA (as iFVG #2)" => close > e60 and up60
    => close > e60 and up60 and close > e15 and up15 and close > e30 and up30
biasS = switch biasSrc
    "VWAP" => close < vwap
    "15m 21 EMA" => close < m21 and mDn
    "Hourly 20 EMA (as iFVG #2)" => close < e60 and dn60
    => close < e60 and dn60 and close < e15 and dn15 and close < e30 and dn30

// Asian check of iFVG #2 (18:00-02:00)
newSes = hm >= 1800 and (bar_index == 0 or hm[1] < 1800)
var float xO = na
var float xC = na
var float xH = na
var float xL = na
if newSes
    xO := na
    xC := na
    xH := na
    xL := na
if hm >= 1800 or hm < 200
    xO := na(xO) ? open : xO
    xC := close
    xH := na(xH) ? high : math.max(xH, high)
    xL := na(xL) ? low : math.min(xL, low)
var int dayBias = 0          // the last non-zero bias of the day
var bool liftL = false       // the bias turned up during the day
var bool liftS = false       // the bias turned down during the day
asiaOk(d) =>
    ad = na(xO) ? 0 : xC > xO ? 1 : xC < xO ? -1 : 0
    am = na(xH) ? 0 : close > (xH + xL) / 2 ? 1 : -1
    (asiaLift and (d > 0 ? liftL : liftS)) or asiaMode == "None" or (asiaMode == "It closed in the bias direction" and ad == d) or (asiaMode == "Price on the bias side of its middle" and am == d)

var float bH = na
var float bL = na
var float aH = na
var float aL = na
var int aStart = na
var box aBox = na
var bool live = false
var int nDay = 0
var float prevPos = 0
var float rejL = na          // long setup: wick low of the support rejection
var float lvlL = na          // ... and the level it held
var float rejS = na          // short setup: wick high of the resistance rejection
var float lvlS = na
var bool cbH = false         // closed below the high / 50% / low (for the close rejection back above)
var bool cbM = false
var bool cbL = false
var bool caH = false         // closed above the high / 50% / low (for the close rejection back below)
var bool caM = false
var bool caL = false
if rangeStart
    bH := high
    bL := low
    aStart := bar_index
    live := false
    if showLv
        aBox := box.new(bar_index, high, bar_index + 1, low, border_color = #5B8DEF, bgcolor = color.new(#5B8DEF, 85))
else if inRange
    bH := math.max(bH, high)
    bL := math.min(bL, low)
    if showLv and not na(aBox)
        box.set_top(aBox, bH)
        box.set_bottom(aBox, bL)
        box.set_right(aBox, bar_index + 1)
if rangeDone and not na(bH)
    aH := bH
    aL := bL
    live := true
    nDay := 0
    rejL := na
    rejS := na
    cbH := false
    cbM := false
    cbL := false
    caH := false
    caM := false
    caL := false
    if showLv and not na(aStart)
        box.delete(aBox)
        aBox := box.new(aStart, bH, bar_index, bL, border_color = #5B8DEF, bgcolor = color.new(#5B8DEF, 85))
dayOn = live and not inRange and hm < flatAt
bNow = biasL ? 1 : biasS ? -1 : 0
if rangeDone
    dayBias := bNow
    liftL := false
    liftS := false
else if dayOn and bNow != 0
    if dayBias == -1 and bNow == 1
        liftL := true
        liftS := false
    if dayBias == 1 and bNow == -1
        liftS := true
        liftL := false
    dayBias := bNow
mid = (aH + aL) / 2
tol = tolTk * tick
useH = levels != "50% only"
useL = levels != "50% only"
useM = levels != "Asian high and low only"

// ---- rejections. Support (long): wick to the level from above and close back above it, or a close under it then a
// close back above. Resistance (short): mirror. Without "both sides" the low and 50% are only support and the high and
// 50% only resistance.
supWick(lv) => low <= lv + tol and close > lv
resWick(lv) => high >= lv - tol and close < lv
float supLv = na
float resLv = na
if dayOn and rejMode != "None (bias + entry filter only)"
    wick = rejMode == "Wick: high/low reaches the level, closes back"
    // support candidates: low, 50%, and (both sides) the high
    if useL and (wick ? supWick(aL) : cbL and close > aL)
        supLv := aL
    if useM and (wick ? supWick(mid) : cbM and close > mid)
        supLv := na(supLv) ? mid : math.max(supLv, mid)
    if useH and bothWays and (wick ? supWick(aH) : cbH and close > aH)
        supLv := na(supLv) ? aH : math.max(supLv, aH)
    // resistance candidates: high, 50%, and (both sides) the low
    if useH and (wick ? resWick(aH) : caH and close < aH)
        resLv := aH
    if useM and (wick ? resWick(mid) : caM and close < mid)
        resLv := na(resLv) ? mid : math.min(resLv, mid)
    if useL and bothWays and (wick ? resWick(aL) : caL and close < aL)
        resLv := na(resLv) ? aL : math.min(resLv, aL)
if dayOn
    // what the close did against each level (for the close rejection)
    cbH := close < aH ? true : close > aH ? false : cbH
    cbM := close < mid ? true : close > mid ? false : cbM
    cbL := close < aL ? true : close > aL ? false : cbL
    caH := close > aH ? true : close < aH ? false : caH
    caM := close > mid ? true : close < mid ? false : caM
    caL := close > aL ? true : close < aL ? false : caL
lo3 = ta.lowest(low, 3)
hi3 = ta.highest(high, 3)
if not na(supLv)
    lvlL := supLv
    rejL := rejMode == "Close: closes through, then closes back" ? lo3 : low
if not na(resLv)
    lvlS := resLv
    rejS := rejMode == "Close: closes through, then closes back" ? hi3 : high
// a rejection is void once price closes beyond its wick
if not na(rejL) and close < rejL
    rejL := na
if not na(rejS) and close > rejS
    rejS := na
noRej = rejMode == "None (bias + entry filter only)"
plotshape(not na(supLv), "Support rejection", shape.triangleup, location.belowbar, #089981, size = size.tiny)
plotshape(not na(resLv), "Resistance rejection", shape.triangledown, location.abovebar, #f23645, size = size.tiny)

// ---- entry filter
refL = switch entryRef
    "VWAP" => close > vwap
    "15m 21 EMA" => close > m21
    "VWAP and 15m 21 EMA" => close > vwap and close > m21
    => true
refS = switch entryRef
    "VWAP" => close < vwap
    "15m 21 EMA" => close < m21
    "VWAP and 15m 21 EMA" => close < vwap and close < m21
    => true

// ---- stop for a new trade (d = 1 long, -1 short)
stopFor(d) =>
    buf = bufTk * tick
    switch stopMode
        "Asian range edge" => d > 0 ? aL - buf : aH + buf
        "The rejected level" => noRej ? (d > 0 ? aL - buf : aH + buf) : d > 0 ? lvlL - buf : lvlS + buf
        "Rejection wick" => noRej ? (d > 0 ? low - buf : high + buf) : d > 0 ? rejL - buf : rejS + buf
        "Asian 50%" => d > 0 ? mid - buf : mid + buf
        => close - d * stopTk * tick

// ---- entries: a bias flip closes the trade and takes the other side if its conditions are met
flipToS = flipExit and strategy.position_size > 0 and biasS
flipToL = flipExit and strategy.position_size < 0 and biasL
flat = strategy.position_size == 0 and prevPos == 0
canTrade = dayOn and (maxDay == 0 or nDay < maxDay) and hm < lastEntry
sL = stopFor(1)
sS = stopFor(-1)
condL = canTrade and side != "Short" and (noRej or not na(rejL)) and biasL and asiaOk(1) and refL and sL < close
condS = canTrade and side != "Long" and (noRej or not na(rejS)) and biasS and asiaOk(-1) and refS and sS > close
if flipToS
    strategy.close("Long", comment = "Bias flip")
if flipToL
    strategy.close("Short", comment = "Bias flip")
// why a pending rejection is not trading (one label each time the reason changes)
whyNot(d) =>
    string r = ""
    if not dayOn
        r := ""
    else if hm >= lastEntry
        r := ""
    else if maxDay > 0 and nDay >= maxDay
        r := "max trades today"
    else if (d > 0 and side == "Short") or (d < 0 and side == "Long")
        r := ""
    else if strategy.position_size * d > 0
        r := ""
    else if d > 0 ? not biasL : not biasS
        r := ""                                        // the bias bar shows it
    else if not asiaOk(d)
        r := "Asian session check"
    else if d > 0 ? not refL : not refS
        r := entryRef + " side"
    else if d > 0 ? sL >= close : sS <= close
        r := "stop on the wrong side"
    r
var string lastWhyL = ""
var string lastWhyS = ""
wL = not na(rejL) and not condL ? whyNot(1) : ""
wS = not na(rejS) and not condS ? whyNot(-1) : ""
if showWhy and wL != "" and wL != lastWhyL
    label.new(bar_index, low, "no long: " + wL, style = label.style_label_up, color = color.new(#089981, 75), textcolor = color.white, size = size.tiny)
if showWhy and wS != "" and wS != lastWhyS
    label.new(bar_index, high, "no short: " + wS, style = label.style_label_down, color = color.new(#f23645, 75), textcolor = color.white, size = size.tiny)
lastWhyL := na(rejL) ? "" : wL
lastWhyS := na(rejS) ? "" : wS
goL = condL and (flat or flipToL)
goS = condS and (flat or flipToS)
var float tStop = na
var float tEntry = na
var float tRisk = na
var float tTgt = na
if goL or goS
    d = goL ? 1 : -1
    nDay += 1
    tStop := goL ? sL : sS
    tEntry := close
    tRisk := math.abs(close - tStop)
    tTgt := tgtR > 0 ? close + d * tgtR * tRisk : na
    if goL
        rejL := na                                     // the next trade needs a new rejection
    else
        rejS := na
    strategy.entry(goL ? "Long" : "Short", goL ? strategy.long : strategy.short)
else if strategy.position_size != 0 and trail != "Off"
    // trailing stop: only tightens, from the next candle on
    d = strategy.position_size > 0 ? 1 : -1
    if trailAt <= 0 or (close - tEntry) * d >= trailAt * tRisk
        buf = bufTk * tick
        lvl = switch trail
            "VWAP" => vwap - d * buf
            "15m 21 EMA" => m21 - d * buf
            "Previous candle" => d > 0 ? low[1] - buf : high[1] + buf
            => close - d * trailTk * tick
        if (lvl - tStop) * d > 0 and (close - lvl) * d > 0
            tStop := lvl
if strategy.position_size > 0 or goL
    strategy.exit("xL", "Long", stop = tStop, limit = tTgt, comment_loss = "Stop", comment_profit = "Target")
if strategy.position_size < 0 or goS
    strategy.exit("xS", "Short", stop = tStop, limit = tTgt, comment_loss = "Stop", comment_profit = "Target")

endHM = hour(time_close) * 100 + minute(time_close)
if strategy.position_size != 0 and endHM >= flatAt and endHM < 1800
    strategy.close_all(comment = "End of day")
if hm >= 1600 and hm < 1800
    live := false

plot(showLv and dayOn ? aH : na, "Asian high", #5B8DEF, 1, plot.style_linebr)
plot(showLv and dayOn ? aL : na, "Asian low", #5B8DEF, 1, plot.style_linebr)
plot(showLv and dayOn ? mid : na, "Asian 50%", color.new(#8a8f9c, 20), 1, plot.style_linebr)
plot(showLv ? vwap : na, "VWAP (from 20:00)", color.new(#F5F5F5, 0), 2)
plot(strategy.position_size != 0 ? tStop : na, "Stop", #f23645, 1, plot.style_linebr)
plot(strategy.position_size != 0 ? tTgt : na, "Target", #089981, 1, plot.style_linebr)
plotshape(true, "Bias", shape.square, location.bottom, biasL ? #089981 : biasS ? #f23645 : color.new(#8a8f9c, 50), size = size.tiny)
prevPos := strategy.position_size
`, { aliases: 'asia asian rejection vwap range nq es futures strategy' }),
S('asiafail', 'Asian range fake breakout (daily 21 EMA bias)', 'Strategies', 'Fake breakout of the Asian range: rip beyond its high / low, first close back inside, daily 21 EMA bias, targets the 50%, far side and a range projection. 15-minute chart.', `
// Asian range failed breakout — NQ / ES 15-minute (5-minute works too), New York time.
//  1. Asian range 20:00-00:00 (high, low, 50%).
//  2. From London (02:00) one side is taken first (sweep), then price rips through the OTHER side (the breakout).
//  3. Trigger: the FIRST candle that closes back inside the range after the breakout (the breakout failed).
//     Bias: the daily 21 EMA. Above it (bullish): only fake breakdowns below the Asian low -> long.
//     Below it (bearish): only fake breakouts above the Asian high -> short.
//  4. Entry at that close (or a limit back at the broken level). Stop beyond the rip's extreme.
//     TP1 the 50% (part), TP2 the far side of the range (the first sweep's side), or hold the rest to the close.
//  Mirror: high taken first, dump through the low, close back above the low -> long.
strategy("Asian range fake breakout (daily 21 EMA bias)", "Asia fake BO", overlay = true, initial_capital = 100000, default_qty_type = strategy.fixed, default_qty_value = 3,
     process_orders_on_close = true, commission_type = strategy.commission.cash_per_contract, commission_value = 2.25, slippage = 1, max_boxes_count = 500, max_lines_count = 500)

grpT = "Sessions (HHMM, New York)"
rStart   = input.int(2000, "Asian range from", group = grpT)
rEnd     = input.int(0, "to (0 = midnight)", group = grpT)
seqFrom  = input.int(200, "Sweeps count from (London)", group = grpT)
trigBy   = input.int(1530, "Trigger by", group = grpT)
flatAt   = input.int(1600, "Flat at (the close of the candle ending then)", group = grpT)
grpS = "Setup"
needSweep = input.bool(false, "The other side must be taken first (sweep, then rip)", group = grpS)
sweepLon = input.bool(false, "The first sweep must be in London (02:00-05:00)", group = grpS)
minExtTk = input.int(4, "Rip: at least (ticks beyond the range)", minval = 0, group = grpS)
maxExt   = input.float(0, "Rip: at most (x the range beyond it, 0 = off)", step = 0.1, group = grpS)
backTk   = input.int(0, "Trigger: the first close back inside, by at least (ticks)", minval = 0, group = grpS)
trigColor = input.bool(false, "Trigger candle must close in the trade direction (red for shorts, green for longs)", group = grpS)
trigBody = input.int(0, "Trigger body at least (% of its range, 0 = off)", minval = 0, maxval = 100, group = grpS)
side     = input.string("Both", "Side", options = ["Both", "Short (failed upside breakout)", "Long (failed downside breakout)"], group = grpS)
grpE = "Entry / exits"
entryMode = input.string("At the trigger close", "Entry", options = ["At the trigger close", "Limit back at the broken level"], group = grpE)
cancelBars = input.int(8, "Limit entry: cancel after (candles)", minval = 1, group = grpE)
bufTk    = input.int(4, "Stop beyond the rip's extreme (ticks)", minval = 0, group = grpE)
contracts = input.int(3, "Contracts", minval = 1, group = grpE)
tp1Qty   = input.int(1, "TP1 at the 50%: contracts", minval = 0, group = grpE)
tp2Qty   = input.int(1, "TP2 at the far side of the range: contracts", minval = 0, group = grpE)
tp3Mode  = input.string("Range projection", "TP3 (the rest)", options = ["Range projection", "Hold to the close"], group = grpE)
tp3Mult  = input.float(1.0, "TP3: the far side + x the Asian range", step = 0.25, group = grpE)
beAfter  = input.bool(true, "Stop to entry after TP1", group = grpE)
minRR    = input.float(0, "Skip when the 50% is under (R, 0 = off)", step = 0.25, group = grpE)
grpD = "Daily 21 EMA"
dLen     = input.int(21, "Daily EMA length", minval = 2, group = grpD)
dFilter  = input.string("With it: shorts below, longs above", "Daily EMA bias (above: bullish, longs only; below: bearish, shorts only)", options = ["Off", "With it: shorts below, longs above", "Against it: shorts above, longs below"], group = grpD)
showD    = input.bool(true, "Show the daily EMA", group = grpD)
grpM = "15-minute 21 EMA"
mLen     = input.int(21, "15-minute EMA length", minval = 2, group = grpM)
mFilter  = input.bool(false, "Also require the trigger close on the trade side of it (below for shorts, above for longs)", group = grpM)
showM    = input.bool(true, "Show the 15-minute EMA", group = grpM)
grpV = "VWAP and volume profile (from the Asian session start, 20:00 to 20:00)"
showVwap = input.bool(true, "VWAP", group = grpV)
vwapBand = input.float(0, "VWAP bands (x standard deviation, 0 = off)", step = 0.5, group = grpV)
showVp   = input.bool(true, "Volume profile of every session", group = grpV)
vpRows   = input.int(24, "Profile rows", minval = 6, maxval = 100, group = grpV)
vaPct    = input.int(70, "Value area (% of the volume)", minval = 10, maxval = 99, group = grpV)
histW    = input.int(30, "Profile width (candles)", minval = 5, group = grpV)
showBox  = input.bool(true, "Draw the Asian range and levels")
showWhy  = input.bool(true, "Label the first close back inside (entry, or why it was skipped)")

tick = syminfo.mintick
hm = hour * 100 + minute
endHM = hour(time_close) * 100 + minute(time_close)
inWin(a, b) => a <= b ? (hm >= a and hm < b) : (hm >= a or hm < b)
inRange = inWin(rStart, rEnd)
rangeStart = inRange and not inRange[1]
rangeDone = not inRange and inRange[1]
dayOn = hm < flatAt and not inRange

// ---- daily EMA from this chart's candles (days roll at 18:00 for futures, at midnight otherwise); live value = today's close so far
var float dDone = na
var float dLast = na
newSess = syminfo.type == "futures" ? (hm >= 1800 and (bar_index == 0 or hm[1] < 1800)) : (bar_index == 0 or dayofmonth != dayofmonth[1])
dA = 2.0 / (dLen + 1)
if newSess and not na(dLast)
    dDone := na(dDone) ? dLast : dA * dLast + (1 - dA) * dDone
dLast := close
dEma = na(dDone) ? na : dA * close + (1 - dA) * dDone
emaShortOk = dFilter == "Off" or na(dEma) ? dFilter == "Off" : dFilter == "With it: shorts below, longs above" ? close < dEma : close > dEma
emaLongOk = dFilter == "Off" or na(dEma) ? dFilter == "Off" : dFilter == "With it: shorts below, longs above" ? close > dEma : close < dEma
plot(showD ? dEma : na, "Daily EMA", color.new(#FF9F1C, 0), 2)

// ---- 15-minute EMA from this chart's candles (the live value of the forming 15-minute candle; on a 15m chart = ta.ema)
var float mDone = na
var float mLast = na
var float mKey = na
mA = 2.0 / (mLen + 1)
mk = math.floor(time / 900000)
if na(mKey) or mk != mKey
    if not na(mLast)
        mDone := na(mDone) ? mLast : mA * mLast + (1 - mA) * mDone
    mKey := mk
mLast := close
mEma = na(mDone) ? close : mA * close + (1 - mA) * mDone
plot(showM ? mEma : na, "15m EMA", color.new(#E07AE0, 0), 1)

var float bH = na
var float bL = na
var float aH = na
var float aL = na
var bool live = false
var int first = 0             // 1 low taken first, -1 high taken first
var bool ripUp = false        // the high taken after the low
var bool ripDn = false
var float ripHi = na          // the rip's extreme
var float ripLo = na
var bool traded = false
var bool usedUp = false       // the first close back inside after each breakout is the only trigger
var bool usedDn = false
var int limBar = na
var int entryBar = na
var float fullQty = na
var float ePx = na
var float vT3 = na
var float vQ2 = 0
var float vQ1 = 0

if rangeStart
    bH := high
    bL := low
    live := false
    strategy.cancel("S")
    strategy.cancel("L")
else if inRange
    bH := math.max(bH, high)
    bL := math.min(bL, low)
if rangeDone and not na(bH)
    aH := bH
    aL := bL
    live := true
    first := 0
    ripUp := false
    ripDn := false
    ripHi := na
    ripLo := na
    traded := false
    usedUp := false
    usedDn := false
    limBar := na

// ---- VWAP and volume profile, both from the start of the Asian session (rStart) to the next one
var float vPV = 0
var float vV = 0
var float vPV2 = 0
var sesH = array.new_float()
var sesL = array.new_float()
var sesV = array.new_float()
var int sesBar = na
var curBoxes = array.new_box()

// the profile of one session drawn at its first candle: rows from its low to its high, each candle's volume shared
// by the rows its range covers; value area rows brighter, the POC row brightest
drawProfile(x0, keep) =>
    n = array.size(sesH)
    if n > 0
        lo = array.min(sesL)
        hi = array.max(sesH)
        stp = (hi - lo) / vpRows
        if stp > 0
            bins = array.new_float(vpRows, 0.0)
            for i = 0 to n - 1
                bh = array.get(sesH, i)
                bl = array.get(sesL, i)
                v = array.get(sesV, i)
                r0 = math.max(0, math.min(vpRows - 1, math.floor((bl - lo) / stp)))
                r1 = math.max(0, math.min(vpRows - 1, math.floor((bh - lo) / stp)))
                for r = r0 to r1
                    ov = bh > bl ? (math.min(bh, lo + (r + 1) * stp) - math.max(bl, lo + r * stp)) / (bh - bl) : 1.0 / (r1 - r0 + 1)
                    array.set(bins, r, array.get(bins, r) + v * math.max(0, ov))
            mx = array.max(bins)
            pi = array.indexof(bins, mx)
            tot = array.sum(bins)
            acc = mx
            up = pi
            dn = pi
            while acc < tot * vaPct / 100 and (up < vpRows - 1 or dn > 0)
                vu = up < vpRows - 1 ? array.get(bins, up + 1) : -1.0
                vd = dn > 0 ? array.get(bins, dn - 1) : -1.0
                if vu >= vd
                    up += 1
                    acc += vu
                else
                    dn -= 1
                    acc += vd
            for r = 0 to vpRows - 1
                v = array.get(bins, r)
                if v > 0 and mx > 0
                    w = math.max(1, math.round(v / mx * histW))
                    tr = r == pi ? 25 : r >= dn and r <= up ? 55 : 80
                    bxx = box.new(x0, lo + (r + 1) * stp, x0 + w, lo + r * stp, border_color = color.new(#FF5D8F, 100), bgcolor = color.new(#FF5D8F, tr))
                    if keep
                        array.push(curBoxes, bxx)
    0

if showVp and rangeStart
    for b in curBoxes
        box.delete(b)
    array.clear(curBoxes)
    if not na(sesBar)
        drawProfile(sesBar, false)                      // the session that just ended
    array.clear(sesH)
    array.clear(sesL)
    array.clear(sesV)
    sesBar := bar_index
if rangeStart
    vPV := 0
    vV := 0
    vPV2 := 0
if not na(sesBar)
    array.push(sesH, high)
    array.push(sesL, low)
    array.push(sesV, nz(volume))
if showVp and barstate.islast and not na(sesBar)
    for b in curBoxes
        box.delete(b)
    array.clear(curBoxes)
    drawProfile(sesBar, true)                           // the session in progress
vol = nz(volume)
vPV += hlc3 * vol
vV += vol
vPV2 += hlc3 * hlc3 * vol
vwap = vV > 0 ? vPV / vV : na
vsd = vV > 0 ? math.sqrt(math.max(0, vPV2 / vV - vwap * vwap)) : na
plot(showVwap ? vwap : na, "VWAP (from 20:00)", color.new(#F5F5F5, 0), 2)
plot(showVwap and vwapBand > 0 ? vwap + vwapBand * vsd : na, "VWAP upper", color.new(#F5F5F5, 60), 1)
plot(showVwap and vwapBand > 0 ? vwap - vwapBand * vsd : na, "VWAP lower", color.new(#F5F5F5, 60), 1)

// the range as plots (every day of the history, no drawing limit): forming 20:00-00:00, then its levels and projections
pRH = plot(showBox and inRange ? bH : na, "Asian range high (forming)", color.new(#5B8DEF, 20), 1, plot.style_linebr)
pRL = plot(showBox and inRange ? bL : na, "Asian range low (forming)", color.new(#5B8DEF, 20), 1, plot.style_linebr)
fill(pRH, pRL, color.new(#5B8DEF, 82), "Asian range")
plot(showBox and live and dayOn ? aH : na, "Asian high", #5B8DEF, 1, plot.style_linebr)
plot(showBox and live and dayOn ? aL : na, "Asian low", #5B8DEF, 1, plot.style_linebr)
plot(showBox and live and dayOn ? (aH + aL) / 2 : na, "Asian 50%", color.new(#8a8f9c, 20), 1, plot.style_linebr)
plot(showBox and live and dayOn ? aH + tp3Mult * (aH - aL) : na, "Projection up", color.new(#5B8DEF, 60), 1, plot.style_linebr)
plot(showBox and live and dayOn ? aL - tp3Mult * (aH - aL) : na, "Projection down", color.new(#5B8DEF, 60), 1, plot.style_linebr)

rng = aH - aL
mid = (aH + aL) / 2
canShort = side != "Long (failed downside breakout)"
canLong = side != "Short (failed upside breakout)"

if live and dayOn and rng > 0 and hm >= seqFrom and not traded and strategy.position_size == 0
    tl = low < aL
    th = high > aH
    // the first side taken
    if first == 0 and (tl or th) and not (tl and th)
        if not sweepLon or (hm >= 200 and hm < 500)
            first := tl ? 1 : -1
    // the rip through the other side (or any break when no sweep is needed)
    if not ripUp and th and (not needSweep ? true : first == 1) and high >= aH + minExtTk * tick
        ripUp := true
        ripHi := high
    else if ripUp
        ripHi := math.max(ripHi, high)
    if not ripDn and tl and (not needSweep ? true : first == -1) and low <= aL - minExtTk * tick
        ripDn := true
        ripLo := low
    else if ripDn
        ripLo := math.min(ripLo, low)
    if ripUp and maxExt > 0 and ripHi - aH > maxExt * rng
        ripUp := false
        first := needSweep ? 2 : first           // too far: no fade today on that side
    if ripDn and maxExt > 0 and aL - ripLo > maxExt * rng
        ripDn := false
        first := needSweep ? 2 : first
    bodyOk = trigBody <= 0 or (high > low and math.abs(close - open) / (high - low) * 100 >= trigBody)
    // the failed upside breakout: back below the Asian high -> short
    firstBackUp = ripUp and not usedUp and close < aH - backTk * tick
    firstBackDn = ripDn and not usedDn and close > aL + backTk * tick
    if firstBackUp
        usedUp := true
    if firstBackDn
        usedDn := true
    whyUp = not canShort ? "side" : not emaShortOk ? "above the daily EMA" : mFilter and close >= mEma ? "above the 15m EMA" : hm >= trigBy ? "after " + str.tostring(trigBy) : trigColor and close >= open ? "not red" : not bodyOk ? "small body" : ""
    whyDn = not canLong ? "side" : not emaLongOk ? "below the daily EMA" : mFilter and close <= mEma ? "below the 15m EMA" : hm >= trigBy ? "after " + str.tostring(trigBy) : trigColor and close <= open ? "not green" : not bodyOk ? "small body" : ""
    if showWhy and firstBackUp and whyUp != ""
        label.new(bar_index, high, "no short: " + whyUp, style = label.style_label_down, color = color.new(#8a8f9c, 60), textcolor = color.white, size = size.tiny)
    if showWhy and firstBackDn and whyDn != ""
        label.new(bar_index, low, "no long: " + whyDn, style = label.style_label_up, color = color.new(#8a8f9c, 60), textcolor = color.white, size = size.tiny)
    if firstBackUp and canShort and emaShortOk and (not mFilter or close < mEma) and hm < trigBy and (not trigColor or close < open) and bodyOk
        stp = ripHi + bufTk * tick
        e = entryMode == "At the trigger close" ? close : aH
        risk = stp - e
        t1ok = e > mid                                   // a close already past the 50%: the far side is the only target
        okS = risk > 0 and e > aL and (minRR <= 0 or ((t1ok ? e - mid : e - aL) >= minRR * risk))
        if showWhy and not okS
            label.new(bar_index, high, "no short: " + (e <= aL ? "closed past the range" : "target under " + str.tostring(minRR) + "R"), style = label.style_label_down, color = color.new(#8a8f9c, 60), textcolor = color.white, size = size.tiny)
        if okS
            strategy.entry("S", strategy.short, qty = contracts, limit = entryMode == "At the trigger close" ? na : aH, comment = "Fake BO short")
            if entryMode != "At the trigger close"
                limBar := bar_index
            q1 = t1ok ? math.min(tp1Qty, contracts) : 0
            q2 = math.min(tp2Qty + (t1ok ? 0 : tp1Qty), contracts - q1)
            if q1 > 0
                strategy.exit("S1", "S", qty = q1, limit = mid, stop = stp, comment_profit = "TP1 50%", comment_loss = "Stop")
            if q2 > 0
                strategy.exit("S2", "S", qty = q2, limit = aL, stop = stp, comment_profit = "TP2 Asian low", comment_loss = "Stop")
            if contracts - q1 - q2 > 0
                strategy.exit("S3", "S", limit = tp3Mode == "Range projection" ? aL - tp3Mult * rng : na, stop = stp, comment_profit = "TP3 projection", comment_loss = "Stop")
            vT3 := aL - tp3Mult * rng
            vQ2 := q2
            vQ1 := q1
            fullQty := contracts
            ePx := e
            entryBar := bar_index
        traded := true
    // the failed downside breakout: back above the Asian low -> long
    else if firstBackDn and canLong and emaLongOk and (not mFilter or close > mEma) and hm < trigBy and (not trigColor or close > open) and bodyOk
        stp = ripLo - bufTk * tick
        e = entryMode == "At the trigger close" ? close : aL
        risk = e - stp
        t1ok = e < mid
        okL = risk > 0 and e < aH and (minRR <= 0 or ((t1ok ? mid - e : aH - e) >= minRR * risk))
        if showWhy and not okL
            label.new(bar_index, low, "no long: " + (e >= aH ? "closed past the range" : "target under " + str.tostring(minRR) + "R"), style = label.style_label_up, color = color.new(#8a8f9c, 60), textcolor = color.white, size = size.tiny)
        if okL
            strategy.entry("L", strategy.long, qty = contracts, limit = entryMode == "At the trigger close" ? na : aL, comment = "Fake BO long")
            if entryMode != "At the trigger close"
                limBar := bar_index
            q1 = t1ok ? math.min(tp1Qty, contracts) : 0
            q2 = math.min(tp2Qty + (t1ok ? 0 : tp1Qty), contracts - q1)
            if q1 > 0
                strategy.exit("L1", "L", qty = q1, limit = mid, stop = stp, comment_profit = "TP1 50%", comment_loss = "Stop")
            if q2 > 0
                strategy.exit("L2", "L", qty = q2, limit = aH, stop = stp, comment_profit = "TP2 Asian high", comment_loss = "Stop")
            if contracts - q1 - q2 > 0
                strategy.exit("L3", "L", limit = tp3Mode == "Range projection" ? aH + tp3Mult * rng : na, stop = stp, comment_profit = "TP3 projection", comment_loss = "Stop")
            vT3 := aH + tp3Mult * rng
            vQ2 := q2
            vQ1 := q1
            fullQty := contracts
            ePx := e
            entryBar := bar_index
        traded := true

// the limit entry waits cancelBars candles
if not na(limBar) and strategy.position_size == 0 and bar_index - limBar >= cancelBars
    strategy.cancel("S")
    strategy.cancel("L")
    limBar := na

// break-even after TP1, flat at flatAt
if strategy.position_size == 0 and bar_index > nz(entryBar) + 1 and strategy.opentrades == 0
    fullQty := na
if beAfter and strategy.position_size != 0 and not na(fullQty) and math.abs(strategy.position_size) < fullQty
    hold = tp3Mode == "Range projection" ? vT3 : na
    if strategy.position_size < 0
        if vQ2 > 0 and math.abs(strategy.position_size) > contracts - vQ1 - vQ2         // TP2 not filled yet
            strategy.exit("S2", "S", qty = vQ2, limit = aL, stop = ePx, comment_profit = "TP2 Asian low", comment_loss = "Break-even")
        strategy.exit("S3", "S", limit = hold, stop = ePx, comment_profit = "TP3 projection", comment_loss = "Break-even")
    else
        if vQ2 > 0 and math.abs(strategy.position_size) > contracts - vQ1 - vQ2
            strategy.exit("L2", "L", qty = vQ2, limit = aH, stop = ePx, comment_profit = "TP2 Asian high", comment_loss = "Break-even")
        strategy.exit("L3", "L", limit = hold, stop = ePx, comment_profit = "TP3 projection", comment_loss = "Break-even")
if strategy.position_size != 0 and endHM >= flatAt and hm < flatAt
    strategy.close_all(comment = "Close", immediately = true)

plotshape(ripUp and not ripUp[1], "Rip up", shape.triangledown, location.abovebar, #f23645, size = size.tiny, text = "rip")
plotshape(ripDn and not ripDn[1], "Rip down", shape.triangleup, location.belowbar, #089981, size = size.tiny, text = "rip")
`, { aliases: 'asia asian fake breakout failed rip ema strategy' }),
S('asiaclean', 'Asian session: VWAP, 15m 21 EMA', 'Levels', 'Asian session box 20:00-00:00 with its high, low and 50% carried through the day, VWAP from 20:00 and the 15-minute 21 EMA.', `
indicator("Asian session: VWAP, 15m 21 EMA", "Asia clean", overlay = true, max_boxes_count = 500)

rStart  = input.int(2000, "Asian session from")
rEnd    = input.int(0, "to (0 = midnight)")
showBox = input.bool(true, "Asian session box and its levels")
flatAt  = input.int(1600, "Carry the Asian high/low/50% until")
mLen    = input.int(21, "15-minute EMA length", minval = 2)
showVwap = input.bool(true, "VWAP (from the Asian session start)")

hm = hour * 100 + minute
inWin(a, b) => a <= b ? (hm >= a and hm < b) : (hm >= a or hm < b)
inRange = inWin(rStart, rEnd)
rangeStart = inRange and not inRange[1]

// Asian session: a box from its high to its low, then its high, low and 50% carried through London and the US session
var float bH = na
var float bL = na
var float aH = na
var float aL = na
var box aBox = na
if rangeStart
    bH := high
    bL := low
    if showBox
        aBox := box.new(bar_index, high, bar_index + 1, low, border_color = color.new(#5B8DEF, 0), bgcolor = color.new(#5B8DEF, 85))
else if inRange
    bH := math.max(bH, high)
    bL := math.min(bL, low)
    if showBox and not na(aBox)
        box.set_top(aBox, bH)
        box.set_bottom(aBox, bL)
        box.set_right(aBox, bar_index + 1)
var int aStart = na
if rangeStart
    aStart := bar_index
if not inRange and inRange[1]
    aH := bH
    aL := bL
    if showBox and not na(aStart)
        box.delete(aBox)
        aBox := box.new(aStart, bH, bar_index, bL, border_color = color.new(#5B8DEF, 0), bgcolor = color.new(#5B8DEF, 85))
dayOn = not inRange and not na(aH) and hm < flatAt
plot(showBox and dayOn ? aH : na, "Asian high", #5B8DEF, 1, plot.style_linebr)
plot(showBox and dayOn ? aL : na, "Asian low", #5B8DEF, 1, plot.style_linebr)
plot(showBox and dayOn ? (aH + aL) / 2 : na, "Asian 50%", color.new(#8a8f9c, 20), 1, plot.style_linebr)

// 15-minute 21 EMA (live value of the forming 15m candle on lower timeframes)
var float mDone = na
var float mLast = na
var float mKey = na
mA = 2.0 / (mLen + 1)
mk = math.floor(time / 900000)
if na(mKey) or mk != mKey
    if not na(mLast)
        mDone := na(mDone) ? mLast : mA * mLast + (1 - mA) * mDone
    mKey := mk
mLast := close
plot(na(mDone) ? close : mA * close + (1 - mA) * mDone, "15m EMA", color.new(#E07AE0, 0), 1)

// VWAP from the Asian session start
var float vPV = 0
var float vV = 0
if rangeStart
    vPV := 0
    vV := 0
vol = nz(volume)
vPV += hlc3 * vol
vV += vol
plot(showVwap and vV > 0 ? vPV / vV : na, "VWAP (from the Asian start)", color.new(#F5F5F5, 0), 2)
`, { aliases: 'asia asian session box vwap ema levels' }),
S('asiastats', 'Asian range statistics', 'Levels', 'How often the Asian high / low hold or break in London and the US session, sweep then reverse sequences and fake-outs, with a stats table.', `
// Asian range statistics (New York time) — run it on NQ / ES 15-minute (or 5-minute) with as much history as possible.
//  1. Floor / ceiling: in London and in the US session, how often price comes back to the Asian low (high), and of those
//     touches how often it HELD as support (resistance: price then moved away by the bounce distance before a candle
//     closed beyond the level by the break distance) or BROKE (a candle closed beyond first).
//  2. Sweep -> rip -> reverse: which side of the Asian range is taken first (and in which session), how often the other
//     side is then taken the same day (new low, then rip to a new high), and how often that run then fails: a close
//     back inside the range and a trade back to the 50% (and to the far side).
indicator("Asian range statistics", "Asia stats", overlay = true, max_labels_count = 500, max_boxes_count = 500)

grpT = "Sessions (HHMM, New York)"
rStart = input.int(2000, "Asian range from", group = grpT)
rEnd   = input.int(0, "to (0 = midnight)", group = grpT)
lonS   = input.int(200, "London from", group = grpT)
lonE   = input.int(500, "London to", group = grpT)
usS    = input.int(930, "US from", group = grpT)
usE    = input.int(1600, "US to (end of the day)", group = grpT)
grpD = "Definitions"
tolTk  = input.int(4, "Touch: within (ticks of the level)", minval = 0, group = grpD)
brkTk  = input.int(8, "Break: a candle closes beyond the level by (ticks)", minval = 0, group = grpD)
bncMul = input.float(0.5, "Hold: then moves away by (x the Asian range)", step = 0.05, group = grpD)
bncMin = input.float(20, "Hold: at least (points)", step = 1, group = grpD)
showBox = input.bool(true, "Draw the Asian range")
showMk  = input.bool(true, "Mark the sweeps and the rips")

tick = syminfo.mintick
hm = hour * 100 + minute
inWin(a, b) => a <= b ? (hm >= a and hm < b) : (hm >= a or hm < b)
inRange = inWin(rStart, rEnd)
rangeStart = inRange and not inRange[1]
rangeDone = not inRange and inRange[1]
inLon = inWin(lonS, lonE)
inUs = inWin(usS, usE)
dayOn = hm < usE and not inRange            // midnight to the US close

// ---- totals (all days)
var int days = 0
// floor / ceiling: [touched, held, broke] per session
var int fLonT = 0
var int fLonH = 0
var int fLonB = 0
var int fUsT = 0
var int fUsH = 0
var int fUsB = 0
var int cLonT = 0
var int cLonH = 0
var int cLonB = 0
var int cUsT = 0
var int cUsH = 0
var int cUsB = 0
// sequences
var int lowFirst = 0
var int lowFirstLon = 0
var int lowFirstPre = 0
var int lowFirstUs = 0
var int lowThenHigh = 0
var int lowThenHighBack = 0
var int lowThenHighMid = 0
var int lowThenHighFar = 0
var int highFirst = 0
var int highFirstLon = 0
var int highFirstPre = 0
var int highFirstUs = 0
var int highThenLow = 0
var int highThenLowBack = 0
var int highThenLowMid = 0
var int highThenLowFar = 0
var int sameBar = 0
var int neither = 0
// breakouts judged at the US close: above / back inside / through the other side (fake-out)
var int boH = 0
var int boHUp = 0
var int boHIn = 0
var int boHDn = 0
var int boL = 0
var int boLDn = 0
var int boLIn = 0
var int boLUp = 0

// ---- the day being measured
var float bH = na
var float bL = na
var float aH = na
var float aL = na
var bool live = false
// floor (Asian low) and ceiling (Asian high) per session: 0 untouched, 1 touched (open), 2 held, 3 broke
var int fLon = 0
var int fUs = 0
var int cLon = 0
var int cUs = 0
// sequence: which side first (1 low, -1 high, 2 same candle), where, other side taken, back inside, 50%, far side
var int first = 0
var int firstWhere = 0
var bool other = false
var bool back = false
var bool toMid = false
var bool toFar = false
var bool brokeH = false
var bool brokeL = false
var float dayClose = na
var box bx = na

// one level in one session: touch, then held or broke
step(st, lvl, isFloor, rng) =>
    s = st
    away = math.max(bncMul * rng, bncMin)
    if s == 0 and (isFloor ? low <= lvl + tolTk * tick : high >= lvl - tolTk * tick)
        s := 1
    if s == 1
        if isFloor ? close < lvl - brkTk * tick : close > lvl + brkTk * tick
            s := 3
        else if isFloor ? high >= lvl + away : low <= lvl - away
            s := 2
    s

if rangeStart
    // the previous day goes into the totals
    if live
        days += 1
        fLonT += fLon > 0 ? 1 : 0
        fLonH += fLon == 2 ? 1 : 0
        fLonB += fLon == 3 ? 1 : 0
        fUsT += fUs > 0 ? 1 : 0
        fUsH += fUs == 2 ? 1 : 0
        fUsB += fUs == 3 ? 1 : 0
        cLonT += cLon > 0 ? 1 : 0
        cLonH += cLon == 2 ? 1 : 0
        cLonB += cLon == 3 ? 1 : 0
        cUsT += cUs > 0 ? 1 : 0
        cUsH += cUs == 2 ? 1 : 0
        cUsB += cUs == 3 ? 1 : 0
        if first == 1
            lowFirst += 1
            lowFirstLon += firstWhere == 1 ? 1 : 0
            lowFirstPre += firstWhere == 2 ? 1 : 0
            lowFirstUs += firstWhere == 3 ? 1 : 0
            lowThenHigh += other ? 1 : 0
            lowThenHighBack += other and back ? 1 : 0
            lowThenHighMid += other and toMid ? 1 : 0
            lowThenHighFar += other and toFar ? 1 : 0
        else if first == -1
            highFirst += 1
            highFirstLon += firstWhere == 1 ? 1 : 0
            highFirstPre += firstWhere == 2 ? 1 : 0
            highFirstUs += firstWhere == 3 ? 1 : 0
            highThenLow += other ? 1 : 0
            highThenLowBack += other and back ? 1 : 0
            highThenLowMid += other and toMid ? 1 : 0
            highThenLowFar += other and toFar ? 1 : 0
        else if first == 2
            sameBar += 1
        else
            neither += 1
        if brokeH and not na(dayClose)
            boH += 1
            boHUp += dayClose > aH ? 1 : 0
            boHIn += dayClose <= aH and dayClose >= aL ? 1 : 0
            boHDn += dayClose < aL ? 1 : 0
        if brokeL and not na(dayClose)
            boL += 1
            boLDn += dayClose < aL ? 1 : 0
            boLIn += dayClose >= aL and dayClose <= aH ? 1 : 0
            boLUp += dayClose > aH ? 1 : 0
    live := false
    bH := high
    bL := low
    if showBox
        bx := box.new(bar_index, high, bar_index, low, border_color = color.new(#5B8DEF, 20), bgcolor = color.new(#5B8DEF, 85))
else if inRange
    bH := math.max(bH, high)
    bL := math.min(bL, low)
    if showBox and not na(bx)
        box.set_top(bx, bH)
        box.set_bottom(bx, bL)
        box.set_right(bx, bar_index)

if rangeDone and not na(bH)
    aH := bH
    aL := bL
    live := true
    fLon := 0
    fUs := 0
    cLon := 0
    cUs := 0
    first := 0
    firstWhere := 0
    other := false
    back := false
    toMid := false
    toFar := false
    brokeH := false
    brokeL := false
    dayClose := na

rng = aH - aL
mid = (aH + aL) / 2
if live and dayOn and rng > 0
    // 1. floor / ceiling per session
    if inLon
        fLon := step(fLon, aL, true, rng)
        cLon := step(cLon, aH, false, rng)
    if inUs
        fUs := step(fUs, aL, true, rng)
        cUs := step(cUs, aH, false, rng)
    dayClose := close
    // 2. the sequence, from London on
    if hm >= lonS
        brokeH := brokeH or high > aH
        brokeL := brokeL or low < aL
        tl = low < aL
        th = high > aH
        where = inLon ? 1 : hm < usS ? 2 : 3
        if first == 0 and (tl or th)
            first := tl and th ? 2 : tl ? 1 : -1
            firstWhere := where
            if showMk and first == 1
                label.new(bar_index, low, "low taken", style = label.style_label_up, color = color.new(#f23645, 60), textcolor = color.white, size = size.tiny)
            if showMk and first == -1
                label.new(bar_index, high, "high taken", style = label.style_label_down, color = color.new(#f23645, 60), textcolor = color.white, size = size.tiny)
        else if first == 1
            if not other and th
                other := true
                if showMk
                    label.new(bar_index, high, "rip: high taken", style = label.style_label_down, color = color.new(#089981, 50), textcolor = color.white, size = size.tiny)
            else if other
                if not back and close < aH
                    back := true
                if back and not toMid and low <= mid
                    toMid := true
                    if showMk
                        label.new(bar_index, low, "back to 50%", style = label.style_label_up, color = color.new(#E7C35A, 50), textcolor = color.black, size = size.tiny)
                if back and not toFar and low <= aL
                    toFar := true
        else if first == -1
            if not other and tl
                other := true
                if showMk
                    label.new(bar_index, low, "dump: low taken", style = label.style_label_up, color = color.new(#089981, 50), textcolor = color.white, size = size.tiny)
            else if other
                if not back and close > aL
                    back := true
                if back and not toMid and high >= mid
                    toMid := true
                    if showMk
                        label.new(bar_index, high, "back to 50%", style = label.style_label_down, color = color.new(#E7C35A, 50), textcolor = color.black, size = size.tiny)
                if back and not toFar and high >= aH
                    toFar := true

plot(live and dayOn ? aH : na, "Asian high", #5B8DEF, 1, plot.style_linebr)
plot(live and dayOn ? aL : na, "Asian low", #5B8DEF, 1, plot.style_linebr)
plot(live and dayOn ? mid : na, "Asian 50%", color.new(#8a8f9c, 30), 1, plot.style_linebr)

// ---- the results (last candle)
pc(a, b) => b > 0 ? str.tostring(math.round(a / b * 100)) + "%" : "-"
held(t, h, b) => str.tostring(t) + " touched · held " + pc(h, t) + " · broke " + pc(b, t) + " · undecided " + pc(t - h - b, t)
var label res = na
if barstate.islast
    txt = "ASIAN RANGE · " + str.tostring(days) + " days (completed)\\n\\n" +
         "ASIAN LOW as support\\n  London: " + pc(fLonT, days) + " of days, " + held(fLonT, fLonH, fLonB) + "\\n  US: " + pc(fUsT, days) + " of days, " + held(fUsT, fUsH, fUsB) + "\\n\\n" +
         "ASIAN HIGH as resistance\\n  London: " + pc(cLonT, days) + " of days, " + held(cLonT, cLonH, cLonB) + "\\n  US: " + pc(cUsT, days) + " of days, " + held(cUsT, cUsH, cUsB) + "\\n\\n" +
         "LOW TAKEN FIRST: " + str.tostring(lowFirst) + " days (" + pc(lowFirst, days) + ") · London " + pc(lowFirstLon, lowFirst) + " · 05:00-09:30 " + pc(lowFirstPre, lowFirst) + " · US " + pc(lowFirstUs, lowFirst) +
         "\\n  then rip to a new high: " + str.tostring(lowThenHigh) + " (" + pc(lowThenHigh, lowFirst) + ")" +
         "\\n    then back below the high: " + pc(lowThenHighBack, lowThenHigh) + " · to the 50%: " + pc(lowThenHighMid, lowThenHigh) + " · to the Asian low: " + pc(lowThenHighFar, lowThenHigh) + "\\n\\n" +
         "HIGH TAKEN FIRST: " + str.tostring(highFirst) + " days (" + pc(highFirst, days) + ") · London " + pc(highFirstLon, highFirst) + " · 05:00-09:30 " + pc(highFirstPre, highFirst) + " · US " + pc(highFirstUs, highFirst) +
         "\\n  then dump to a new low: " + str.tostring(highThenLow) + " (" + pc(highThenLow, highFirst) + ")" +
         "\\n    then back above the low: " + pc(highThenLowBack, highThenLow) + " · to the 50%: " + pc(highThenLowMid, highThenLow) + " · to the Asian high: " + pc(highThenLowFar, highThenLow) + "\\n\\n" +
         "BREAKOUTS, where the day closed (US close)\\n  Above the Asian high: " + str.tostring(boH) + " days · closed above " + pc(boHUp, boH) + " · back inside " + pc(boHIn, boH) + " · below the Asian low (fake-out) " + pc(boHDn, boH) +
         "\\n  Below the Asian low: " + str.tostring(boL) + " days · closed below " + pc(boLDn, boL) + " · back inside " + pc(boLIn, boL) + " · above the Asian high (fake-out) " + pc(boLUp, boL) + "\\n\\n" +
         "Both sides in the same candle: " + str.tostring(sameBar) + " · neither side taken: " + str.tostring(neither) + " (" + pc(neither, days) + ")"
    label.delete(res)
    res := label.new(bar_index + 3, high, txt, style = label.style_label_left, color = color.new(#131722, 5), textcolor = color.white, size = size.small, textalign = text.align_left)
`, { aliases: 'asia asian range statistics stats support resistance fakeout' }),
S('ifvg2', 'FVG inversion EMA bias #2 (NQ)', 'Strategies', 'The journal Research strategy “FVG inversion EMA bias #2” as Pine: hourly 20 EMA bias + Asian session direction, a fair value gap against the bias inverted within 12 candles, stop at the pullback extreme, target the previous liquidity (at least 2R), 3 a session until 10:30, flat 15:55. Run it on NQ 5m.', `
// FVG inversion with the EMA bias (#2) — NQ 5-minute, overnight + NYSE session.
// Same rules as the journal's Research strategy "FVG inversion EMA bias #2":
//   bias = hourly 20 EMA (price on its side, EMA sloping that way), confirmed by the Asian session's direction
//   (a bias change after 02:00 lifts the Asian check for the new side);
//   a fair value gap against the bias (the pullback), then a close through it within 12 candles: enter at that close;
//   stop at the pullback extreme, target the previous liquidity (at least 2R), skip under 1R;
//   at most 3 a session, entries until 10:30 (overnight from 18:00 included), flat at 15:55;
//   trigger candle range at most 2.5x the average candle range (last 50), body at least 40% of its range.
strategy("FVG inversion EMA bias #2", "iFVG bias #2", overlay = true, initial_capital = 100000, default_qty_type = strategy.fixed, default_qty_value = 1,
     process_orders_on_close = true, commission_type = strategy.commission.cash_per_contract, commission_value = 2.25, slippage = 1)

biasMode  = input.string("Hourly 20 EMA only", "Bias", options = ["Hourly 20 EMA only", "15 / 30 / 60-minute 20 EMAs"])
asiaMode  = input.string("It closed in the bias direction", "Asian session (18:00-02:00) check", options = ["It closed in the bias direction", "Price on the bias side of its middle", "None"])
asiaLift  = input.bool(true, "A bias change after the Asian session lifts its check (for the new side)")
minGap    = input.int(2, "Gap at least (ticks)", minval = 0)
fvgMax    = input.int(12, "Inversion within (candles)", minval = 1)
stopMode  = input.string("Pullback extreme", "Stop", options = ["Pullback extreme", "Far end of the gap"])
bufTicks  = input.int(0, "Stop buffer (ticks)", minval = 0)
tgtMode   = input.string("Previous liquidity, at least 2R", "Target", options = ["Previous liquidity, at least 2R", "Previous liquidity", "Fixed R"])
tgtR      = input.float(1.5, "Fixed target (R)", step = 0.25)
minRR     = input.float(1.0, "Skip when the target is under (R, 0 = off)", step = 0.25)
pivLen    = input.int(2, "Liquidity: swing length (candles each side)", minval = 1)
liqLook   = input.int(150, "Liquidity: look back (candles)", minval = 10)
lastEntry = input.int(1030, "Last entry (HHMM, New York)")
maxDay    = input.int(3, "At most per session", minval = 1)
allHours  = input.bool(true, "Overnight entries too (from 18:00)")
gapFrom   = input.int(800, "Gaps from (HHMM) when overnight entries are off")
side      = input.string("Both", "Side", options = ["Both", "Long", "Short"])
trigBody  = input.bool(false, "Measure the trigger candle by its body (off = range, wicks included)")
trigMin   = input.float(0, "Trigger candle at least (x average range, 0 = off)", step = 0.1)
trigMax   = input.float(2.5, "Trigger candle at most (x average range, 0 = off)", step = 0.1)
avgN      = input.int(50, "Average range over (candles)", minval = 3)
trigN     = input.int(0, "Compare with the previous N candles (0 = off)", minval = 0)
trigPrevMult = input.float(1.0, "Trigger at least (x the largest of those N)", step = 0.1)
trigPrevMax  = input.float(0, "Trigger at most (x the largest of those N, 0 = off)", step = 0.1)
trigEngulf   = input.bool(true, "Trigger closes past those N candles' high / low")
bodyPct   = input.int(40, "Trigger body at least (% of its range, 0 = off)", minval = 0, maxval = 100)
showEma   = input.bool(true, "Show the 20 EMAs")

tick = syminfo.mintick
hm = hour * 100 + minute
tfMin = timeframe.multiplier

// ---- the 20 EMA of the forming 15 / 30 / 60-minute candle (its live value at this candle's close), and its slope
emaTrack(n) =>
    var float done = na
    var float last = na
    var float key = na
    k = math.floor(time / (n * 60000))
    if na(key) or k != key
        if not na(last)
            done := na(done) ? last : 2.0 / 21 * last + (1 - 2.0 / 21) * done
        key := k
    last := close
    now = na(done) ? close : 2.0 / 21 * close + (1 - 2.0 / 21) * done
    [now, not na(done) and now > done, not na(done) and now < done]

[e15, up15, dn15] = emaTrack(15)
[e30, up30, dn30] = emaTrack(30)
[e60, up60, dn60] = emaTrack(60)
h1only = biasMode == "Hourly 20 EMA only"
biasL = close > e60 and up60 and (h1only or (close > e15 and up15 and close > e30 and up30))
biasS = close < e60 and dn60 and (h1only or (close < e15 and dn15 and close < e30 and dn30))
bias = biasL ? 1 : biasS ? -1 : 0

// ---- the previous liquidity: the nearest untaken swing high (d > 0) above e / swing low under e
liquidity(d, e) =>
    float best = na
    float runHi = na
    float runLo = na
    for off = 1 to liqLook
        if off > pivLen and bar_index >= off + pivLen
            lv = d > 0 ? high[off] : low[off]
            isPiv = true
            for j = off - pivLen to off + pivLen
                if d > 0 and high[j] > lv
                    isPiv := false
                if d < 0 and low[j] < lv
                    isPiv := false
            untaken = d > 0 ? (na(runHi) or runHi <= lv) : (na(runLo) or runLo >= lv)
            if isPiv and untaken and (d > 0 ? lv > e : lv < e)
                best := na(best) ? lv : d > 0 ? math.min(best, lv) : math.max(best, lv)
        runHi := na(runHi) ? high[off] : math.max(runHi, high[off])
        runLo := na(runLo) ? low[off] : math.min(runLo, low[off])
    best

// ---- the session (18:00 the evening before to 17:00): its candles, trades and Asian session (18:00-02:00)
newSes = hm >= 1800 and (bar_index == 0 or hm[1] < 1800)
var int sesBars = 0
var int sesTrades = 0
var bool sesDone = false
var float aO = na
var float aC = na
var float aH = na
var float aL = na
var gD = array.new_int()
var gTop = array.new_float()
var gBot = array.new_float()
var gK = array.new_int()
if newSes
    sesBars := 0
    sesTrades := 0
    sesDone := false
    aO := na
    aC := na
    aH := na
    aL := na
    array.clear(gD)
    array.clear(gTop)
    array.clear(gBot)
    array.clear(gK)
sesBars += 1
if hm >= 1800 or hm < 200
    aO := na(aO) ? open : aO
    aC := close
    aH := na(aH) ? high : math.max(aH, high)
    aL := na(aL) ? low : math.min(aL, low)

today = hm < 1800

// a bias change after the Asian session (02:00 to 17:00) lifts the Asian check for the new side
var int dayBias = 0
var bool liftL = false
var bool liftS = false
afterAsia = today and hm >= 200
if newSes
    dayBias := 0
    liftL := false
    liftS := false
if afterAsia and bias != 0
    if dayBias == -1 and bias == 1
        liftL := true
        liftS := false
    if dayBias == 1 and bias == -1
        liftS := true
        liftL := false
    dayBias := bias
endMin = hour * 60 + minute + tfMin
endHM = math.floor(endMin / 60) * 100 + endMin % 60
if today and endHM > lastEntry
    sesDone := true
if sesTrades >= maxDay
    sesDone := true

var float prevPos = 0
inTrade = strategy.position_size != 0 or prevPos != 0      // one at a time; the exit candle is skipped too
var float tStop = na
var float tTgt = na

if not sesDone and not inTrade and sesBars >= 3
    // a gap forms (candle 1 and 3 don't overlap)
    if allHours or (today and hm >= gapFrom)
        if low[2] - high >= minGap * tick
            array.push(gD, 1)
            array.push(gTop, low[2])
            array.push(gBot, high)
            array.push(gK, bar_index)
        if low - high[2] >= minGap * tick
            array.push(gD, -1)
            array.push(gTop, low)
            array.push(gBot, high[2])
            array.push(gK, bar_index)
    // gaps older than fvgMax candles are gone
    if array.size(gK) > 0
        for i = array.size(gK) - 1 to 0
            if bar_index - array.get(gK, i) > fvgMax
                array.remove(gD, i)
                array.remove(gTop, i)
                array.remove(gBot, i)
                array.remove(gK, i)
    // the first gap closed through (inverted) on this candle
    hit = -1
    if array.size(gK) > 0
        for i = 0 to array.size(gK) - 1
            if hit < 0 and array.get(gK, i) != bar_index
                dd = array.get(gD, i)
                if (dd > 0 and close > array.get(gTop, i)) or (dd < 0 and close < array.get(gBot, i))
                    hit := i
    if hit >= 0
        d = array.get(gD, hit)
        top = array.get(gTop, hit)
        bot = array.get(gBot, hit)
        gk = array.get(gK, hit)
        array.remove(gD, hit)
        array.remove(gTop, hit)
        array.remove(gBot, hit)
        array.remove(gK, hit)
        ok = true
        if not allHours and not (today and hm >= 930)
            ok := false
        if allHours and hm >= 1600 and hm < 1800
            ok := false
        if (side == "Long" and d < 0) or (side == "Short" and d > 0)
            ok := false
        if bias != d
            ok := false
        ad = na(aO) ? 0 : aC > aO ? 1 : aC < aO ? -1 : 0
        am = na(aH) ? 0 : close > (aH + aL) / 2 ? 1 : -1
        lifted = asiaLift and (d > 0 ? liftL : liftS)
        if not lifted and asiaMode == "It closed in the bias direction" and ad != d
            ok := false
        if not lifted and asiaMode == "Price on the bias side of its middle" and am != d
            ok := false
        // the trigger candle against the session's average candle range (the candles before it)
        n = math.min(avgN, sesBars - 1)
        float sumR = 0
        if n > 0
            for j = 1 to n
                sumR += high[j] - low[j]
        avg = n > 0 ? sumR / n : 0
        body = math.abs(close - open)
        rng = high - low
        size = avg > 0 ? (trigBody ? body : rng) / avg : 0
        if trigMin > 0 and size < trigMin
            ok := false
        if trigMax > 0 and size > trigMax
            ok := false
        if bodyPct > 0 and rng > 0 and body / rng * 100 < bodyPct
            ok := false
        // against the previous N candles of the session
        pn = math.min(trigN, sesBars - 1)
        if trigN > 0 and pn > 0
            float bigN = 0
            float hiN = high[1]
            float loN = low[1]
            for j = 1 to pn
                bigN := math.max(bigN, trigBody ? math.abs(close[j] - open[j]) : high[j] - low[j])
                hiN := math.max(hiN, high[j])
                loN := math.min(loN, low[j])
            me = trigBody ? body : rng
            if me < trigPrevMult * bigN
                ok := false
            if trigPrevMax > 0 and me > trigPrevMax * bigN
                ok := false
            if trigEngulf and not (d > 0 ? close > hiN : close < loN)
                ok := false
        e = close
        buf = bufTicks * tick
        float stop = na
        if stopMode == "Far end of the gap"
            stop := d > 0 ? bot - buf : top + buf
        else
            back = bar_index - (gk - 2)
            float ext = d > 0 ? low : high
            for j = 0 to back
                ext := d > 0 ? math.min(ext, low[j]) : math.max(ext, high[j])
            stop := d > 0 ? ext - buf : ext + buf
        risk = (e - stop) * d
        if risk <= 0
            ok := false
        float tgt = na
        if ok
            two = e + d * 2 * risk
            if tgtMode == "Fixed R"
                tgt := e + d * tgtR * risk
            else
                lq = liquidity(d, e)
                if tgtMode == "Previous liquidity"
                    tgt := lq
                else
                    tgt := not na(lq) and (lq - two) * d > 0 ? lq : two
            if na(tgt) or (tgt - e) * d <= 0 or (minRR > 0 and math.abs(tgt - e) < minRR * risk)
                ok := false
        if ok
            sesTrades += 1
            tStop := stop
            tTgt := tgt
            strategy.entry(d > 0 ? "Long" : "Short", d > 0 ? strategy.long : strategy.short)
            strategy.exit("x", d > 0 ? "Long" : "Short", stop = stop, limit = tgt, comment_loss = "Stop", comment_profit = "Target")

prevPos := strategy.position_size

// flat at 15:55 (the close of the 15:55 candle)
if strategy.position_size != 0 and hm == 1555
    strategy.close_all(comment = "15:55")

plot(showEma ? e60 : na, "EMA 20 (60m)", #E07AE0, 2)
plot(showEma and not h1only ? e30 : na, "EMA 20 (30m)", #E7C35A, 1)
plot(showEma and not h1only ? e15 : na, "EMA 20 (15m)", #5FD4C4, 1)
plot(strategy.position_size != 0 ? tStop : na, "Stop", #f23645, 1, plot.style_linebr)
plot(strategy.position_size != 0 ? tTgt : na, "Target", #089981, 1, plot.style_linebr)
bgcolor(bias > 0 ? color.new(#089981, 94) : bias < 0 ? color.new(#f23645, 94) : na, title = "Bias")
`, { aliases: 'ifvg inverse fair value gap inversion ema bias nq futures research strategy' }),
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
