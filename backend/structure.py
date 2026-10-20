"""Chart structure of the sessions BEFORE a day (no look-ahead): what a trader marks on the chart before the open.

build(prior_bars) -> {atr, hi, lo, trend, zones: [{lo, hi, n, touches}], poc, vaLo, vaHi, days}
  * daily candles from the prior sessions' regular-session candles (any interval: 1, 5 or 30 minutes);
  * atr: 14-day average true range; hi / lo: the range of those sessions;
  * trend: +1 close above its 20-day average and higher than 5 sessions ago, -1 the opposite, 0 otherwise;
  * zones (sr_zones): where price actually turned: hourly swings (5 candles each side) and daily swings (2 days
    each side), each from its body edge to its wick tip (<= 0.35 ATR), overlapping ones merged (<= 0.5 ATR wide);
    touches = how many separate swings turned there (a daily swing counts 2); kept: 2+, the 3 nearest each side;
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
    zones = sr_zones(H, D, atr)
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


def _pivots(B, k):
    """(index, "high" | "low") of the candles higher / lower than k candles on each side (confirmed only)."""
    out = []
    for i in range(k, len(B) - k):
        hs = [B[j]["h"] for j in range(i - k, i + k + 1) if j != i]
        ls = [B[j]["l"] for j in range(i - k, i + k + 1) if j != i]
        if B[i]["h"] > max(hs):
            out.append((i, "high"))
        if B[i]["l"] < min(ls):
            out.append((i, "low"))
    return out


def sr_zones(H, D, atr, k_hour=5, max_w=0.35, keep=3):
    """Support / resistance where price actually turned: the swing highs / lows of the hourly candles (higher /
    lower than the 5 hourly candles on each side, about a day) and of the daily candles (2 days on each side). Each
    swing gives a zone from its candle body edge to its wick tip (at most 0.35 ATR wide); swings whose zones overlap
    (within 0.1 ATR) are one zone, as long as it stays under 0.5 ATR wide. A zone's strength = how many separate
    swings turned there (a daily swing counts 2). Kept: zones with 2+ (tested and held), the nearest 3 above and 3
    below the last close."""
    sw = []
    for B, k, wgt in ((H, k_hour, 1), (D, 2, 2)):
        for i, kind in _pivots(B, k):
            b = B[i]
            if kind == "high":
                top, bot = b["h"], max(b["o"], b["c"])
            else:
                top, bot = min(b["o"], b["c"]), b["l"]
            if top - bot > max_w * atr:                # a long wick: the zone is the part next to the extreme
                if kind == "high":
                    bot = top - max_w * atr
                else:
                    top = bot + max_w * atr
            sw.append({"lo": bot, "hi": top, "w": wgt, "kind": kind, "t": b.get("t") or b.get("d")})
    sw.sort(key=lambda z: z["lo"])
    zones = []
    for z in sw:
        cur = zones[-1] if zones else None
        if cur and z["lo"] <= cur["hi"] + 0.1 * atr and max(cur["hi"], z["hi"]) - cur["lo"] <= 0.5 * atr:
            cur["hi"] = max(cur["hi"], z["hi"])
            cur["n"] += 1
            cur["touches"] += z["w"]
            cur["last"] = max(cur["last"], z["t"] or "")
        else:
            zones.append({"lo": z["lo"], "hi": z["hi"], "n": 1, "touches": z["w"], "last": z["t"] or ""})
    zones = [z for z in zones if z["touches"] >= 2]
    last = D[-1]["c"] if D else (H[-1]["c"] if H else 0)
    above = sorted((z for z in zones if z["lo"] > last), key=lambda z: z["lo"])[:keep]
    below = sorted((z for z in zones if z["hi"] < last), key=lambda z: -z["hi"])[:keep]
    at = [z for z in zones if z["lo"] <= last <= z["hi"]]
    out = sorted(above + below + at, key=lambda z: z["lo"])
    for z in out:
        z["lo"], z["hi"] = round(z["lo"], 4), round(z["hi"], 4)
    return out


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
