"""Chart structure of the sessions BEFORE a day (no look-ahead): what a trader marks on the chart before the open.

build(prior_bars) -> {atr, hi, lo, trend, zones: [{lo, hi, n, touches}], poc, vaLo, vaHi, days}
  * daily candles from the prior sessions' regular-session candles (any interval: 1, 5 or 30 minutes);
  * atr: 14-day average true range; hi / lo: the range of those sessions;
  * trend: +1 close above its 20-day average and higher than 5 sessions ago, -1 the opposite, 0 otherwise;
  * zones: Gala's hourly levels (pivot bodies, impulsive candle opens; levels traded through often dropped)
    clustered within a quarter ATR, with how many hourly candles traded inside each (touches);
  * poc / vaLo / vaHi: the volume profile (typical price of each candle, volume weighted), value area = 70%.

Used by the 30-day context box of the chart (daytrade.context), by the formula series (dTrend, rangePos, supZone,
resZone, inZone, zoneTouches, poc30, vaHigh, vaLow, dAtr) and stored with each stocks-in-play pick (sipbench)."""


def daily(bars):
    out, cur = [], None
    for b in bars:
        if not ("09:30" <= b["t"][11:16] < "16:00"):
            continue
        d = b["t"][:10]
        if not cur or cur["d"] != d:
            cur = {"d": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": float(b.get("v") or 0)}
            out.append(cur)
        else:
            cur["h"], cur["l"], cur["c"] = max(cur["h"], b["h"]), min(cur["l"], b["l"]), b["c"]
            cur["v"] += float(b.get("v") or 0)
    return out


def build(prior, days=30):
    import gala
    reg = [b for b in prior if "09:30" <= b["t"][11:16] < "16:00"]
    D = daily(reg)[-days:]
    if len(D) < 5:
        return None
    keep = {x["d"] for x in D}
    reg = [b for b in reg if b["t"][:10] in keep]
    trs = [max(D[i]["h"] - D[i]["l"], abs(D[i]["h"] - D[i - 1]["c"]), abs(D[i]["l"] - D[i - 1]["c"])) for i in range(1, len(D))]
    atr = sum(trs[-14:]) / len(trs[-14:]) if trs else (D[-1]["h"] - D[-1]["l"])
    if atr <= 0:
        return None
    closes = [x["c"] for x in D]
    trend = 0
    if len(closes) >= 15:
        n = min(20, len(closes))
        sma = sum(closes[-n:]) / n
        trend = 1 if closes[-1] > sma and closes[-1] > closes[-6] else -1 if closes[-1] < sma and closes[-1] < closes[-6] else 0
    H = gala.agg(reg, 60)
    lv = sorted(x["p"] for x in gala.levels(H, {"gPivot": 2, "gImpulse": 1.5, "gMaxCross": 4}, 0.1 * atr))
    zones = []
    for p in lv:
        if zones and p - zones[-1]["hi"] <= 0.25 * atr:
            zones[-1]["hi"] = p
            zones[-1]["n"] += 1
        else:
            zones.append({"lo": p, "hi": p, "n": 1})
    pad = 0.08 * atr
    for z in zones:
        z["lo"], z["hi"] = round(z["lo"] - pad, 4), round(z["hi"] + pad, 4)
        z["touches"] = sum(1 for b in H if b["l"] <= z["hi"] and b["h"] >= z["lo"])
    price = closes[-1]
    step = max(atr / 8, price * 0.0005)
    vp = {}
    for b in reg:
        k = round(((b["h"] + b["l"] + b["c"]) / 3) / step)
        vp[k] = vp.get(k, 0) + float(b.get("v") or 0)
    poc = va_lo = va_hi = None
    if vp and sum(vp.values()) > 0:
        tot = sum(vp.values())
        pk = max(vp, key=vp.get)
        lo_k = hi_k = pk
        acc = vp[pk]
        while acc < 0.7 * tot and (lo_k - 1 in vp or hi_k + 1 in vp):
            up, dn = vp.get(hi_k + 1, -1), vp.get(lo_k - 1, -1)
            if up >= dn:
                hi_k += 1
                acc += max(0, up)
            else:
                lo_k -= 1
                acc += max(0, dn)
        poc, va_lo, va_hi = round(pk * step, 4), round(lo_k * step, 4), round(hi_k * step, 4)
    return {"atr": round(atr, 4), "hi": max(x["h"] for x in D), "lo": min(x["l"] for x in D), "trend": trend, "zones": zones,
            "poc": poc, "vaLo": va_lo, "vaHi": va_hi, "days": len(D), "last": D[-1]["d"]}


def compact(st):
    """A small copy to store with each pick (zones with few touches dropped first when there are many)."""
    if not st:
        return None
    zs = sorted(st["zones"], key=lambda z: -z["touches"])[:12]
    return {**{k: st[k] for k in ("atr", "hi", "lo", "trend", "poc", "vaLo", "vaHi", "days")},
            "zones": sorted(([round(z["lo"], 4), round(z["hi"], 4), z["touches"]] for z in zs), key=lambda z: z[0])}


def zones_of(st):
    """[(lo, hi, touches)] from a built or compact structure."""
    if not st:
        return []
    zs = st.get("zones") or []
    return [(z["lo"], z["hi"], z["touches"]) if isinstance(z, dict) else (z[0], z[1], z[2]) for z in zs]
