"""Ports of the trader's thinkorswim study (VwapVolumeADRFVGstrategy) to daily bars.

- High-Volume Close (HVC): when the previous day had the highest volume of the last 21 days, its close becomes the HVC,
  with bands at close * (1 + ADR%) and close * (1 - 0.618 * ADR%). ADR here is computed exactly like the study
  (21 high/low ratios summed and divided by 20).
- Anchored VWAP: restarts on the day the HVC changes and accumulates typical price * volume from there.
- Fair value gaps on daily bars with the study's threshold (gap larger than 33% of the 20-day average true range),
  kept "in memory" until price closes through them.
Everything is evaluated bar by bar with no look-ahead.
"""


def _adr_study(bars, j):
    if j < 20:
        return None
    return 100 * (sum(bars[j - k]["h"] / bars[j - k]["l"] for k in range(21) if bars[j - k]["l"]) / 20 - 1)


def series(bars):
    """Per-bar state list aligned with bars."""
    out = []
    hvc = up = dn = None
    anchor = None
    cum_pv = cum_v = 0.0
    trs = []
    bear_top = bear_bot = bull_top = bull_bot = None
    for i, b in enumerate(bars):
        # --- HVC (uses the previous bar, like isHighVol[1]) ---
        prev_hvc = hvc
        if i >= 1:
            window = [x.get("v") or 0 for x in bars[max(0, i - 21):i]]
            if window and (bars[i - 1].get("v") or 0) == max(window) and (bars[i - 1].get("v") or 0) > 0:
                c1, adr = bars[i - 1]["c"], _adr_study(bars, i - 1)
                hvc = c1
                up = c1 * (1 + adr / 100) if adr is not None else None
                dn = c1 * (1 - adr * 0.618 / 100) if adr is not None else None
        # --- anchored VWAP resets when the HVC changes ---
        if hvc is not None and hvc != prev_hvc:
            anchor, cum_pv, cum_v = i, 0.0, 0.0
        if anchor is not None:
            typ = (b["h"] + b["l"] + b["c"]) / 3
            cum_pv += typ * (b.get("v") or 0)
            cum_v += b.get("v") or 0
        avwap = cum_pv / max(1.0, cum_v) if anchor is not None and cum_v else None
        # --- FVG with memory, threshold = 33% of 20-day average true range ---
        pc = bars[i - 1]["c"] if i else b["c"]
        trs.append(max(b["h"], pc) - min(b["l"], pc))
        thr = 0.33 * sum(trs[-20:]) / len(trs[-20:])
        bear = i >= 2 and bars[i - 2]["l"] - b["h"] > thr
        bull = i >= 2 and b["l"] - bars[i - 2]["h"] > thr
        if bear:
            bear_top, bear_bot = bars[i - 2]["l"], b["h"]
        elif bear_top is not None and b["c"] > bear_top:
            bear_top = bear_bot = None
        if bull:
            bull_bot, bull_top = bars[i - 2]["h"], b["l"]
        elif bull_bot is not None and b["c"] < bull_bot:
            bull_top = bull_bot = None
        out.append({"t": b["t"], "hvc": hvc, "hvUp": up, "hvDn": dn, "avwap": avwap,
                    "anchor": bars[anchor]["t"][:10] if anchor is not None else None,
                    "bullTop": bull_top, "bullBot": bull_bot, "bearTop": bear_top, "bearBot": bear_bot,
                    "newBull": bull, "newBear": bear})
    return out


def state_at(bars, idx, price=None):
    """Indicator state after bar idx, and where `price` (default: that bar's close) sits relative to it."""
    s = series(bars[: idx + 1])[-1]
    p = price if price is not None else bars[idx]["c"]
    r = lambda x: None if x is None else round(x, 4)
    out = {"hvc": r(s["hvc"]), "hvUp": r(s["hvUp"]), "hvDn": r(s["hvDn"]), "avwap": r(s["avwap"]), "avwapAnchor": s["anchor"],
           "bullFvg": [r(s["bullBot"]), r(s["bullTop"])] if s["bullTop"] is not None else None,
           "bearFvg": [r(s["bearBot"]), r(s["bearTop"])] if s["bearTop"] is not None else None}
    if s["hvc"]:
        d = p / s["hvc"] - 1
        out["vsHvc"] = "near" if abs(d) <= 0.015 else "above" if d > 0 else "below"
        out["hvcDistPct"] = round(d * 100, 2)
    if s["avwap"]:
        out["vsAvwap"] = "above" if p > s["avwap"] else "below"
        out["avwapDistPct"] = round((p / s["avwap"] - 1) * 100, 2)
    bull, bear = s["bullTop"] is not None, s["bearTop"] is not None
    if bull:
        lo, hi = min(s["bullBot"], s["bullTop"]), max(s["bullBot"], s["bullTop"])
        pos = "above" if p > hi else "inside" if p >= lo else "below"
    out["fvgState"] = ("bull and bear" if bull and bear else f"bull FVG, price {pos}" if bull
                       else "bear FVG only" if bear else "no active FVG")
    return out
