"""Volume climax reversal: a high-volume candle with a long wick at the session's extreme, faded.

  A `tf`-minute candle with volume at least `volMult` x the average of the last 20 candles, that makes the session's
  high (low) so far, with an upper (lower) wick at least `wickPct`% of its range: sell (buy) at its close. Stop past
  its high (low) + `bufTicks`; target the session VWAP (skipped under `minRR`) or `tgtR` x the risk (`tgt`), or the
  managed exit. `hours`: rth (09:35-`lastEntry`) or all. One position at a time, at most `maxDay` a session."""
import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 5, "volMult": 3.0, "wickPct": 40, "tgt": "vwap", "tgtR": 2.0, "minRR": 1.0, "hours": "rth", "lastEntry": "15:00",
           "maxDay": 2, "bias": "none", "side": "both", "exit": "target", "bufTicks": 2, "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("vm", "Volume (× the average)"), ("wk", "Wick (% of the candle)"), ("ses", "Session")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "volclimax", "tf": 1 if str(p.get("tf")) == "1" else 15 if str(p.get("tf")) == "15" else 5, "tgt": p.get("tgt") if p.get("tgt") in ("vwap", "r") else "vwap",
           "hours": p.get("hours") if p.get("hours") in ("rth", "all") else "rth", "lastEntry": N.hhmm(p.get("lastEntry"), "15:00", "09:40", "15:50")}
    N.K.num(p, out, (("volMult", 1, 20, 3.0), ("wickPct", 0, 95, 40), ("minRR", 0, 10, 1.0), ("maxDay", 1, 20, 2)), ("maxDay",))
    N.clean_common(p, out, bias="none")
    return out


def describe(p):
    tg = (("the session VWAP" + (f" (at least {p['minRR']:g}R)" if p["minRR"] else "")) if p["tgt"] == "vwap" else f"{p['tgtR']:g}R") if p["exit"] == "target" else ""
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute ({'overnight + NYSE' if p['hours'] == 'all' else 'NYSE session'}): a candle with {p['volMult']:g}×+ the average "
            f"volume making the session's high / low with a {p['wickPct']:g}%+ wick: fade it at its close, stop past it + {p['bufTicks']} tick(s)"
            + (f", target {tg}" if tg else N.ifvg._exit_txt(p)) + f", at most {p['maxDay']} a session, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    Vol = N.K.full_volume(sym, p["tf"], d)
    if len(Vol) != len(F):
        return []
    out, k, buf, rth0, flat, last = [], 20, p["bufTicks"] * N.K.TICK, f"{d}T09:30", f"{d}T15:55", f"{d}T{p['lastEntry']}"
    i0 = 0 if p["hours"] == "all" else (N.idx_at(F, rth0) or len(F))
    while k < len(F) and len(out) < p["maxDay"]:
        t, o, h, l, c = F[k]
        if t[:16] >= last:
            break
        if k <= i0 or (p["hours"] == "rth" and N.K.end_of(t, p["tf"])[11:16] < "09:35") or "16:00" <= t[11:16] < "18:00":
            k += 1
            continue
        av = sum(Vol[k - 20:k]) / 20
        rg = h - l
        if av <= 0 or rg <= 0 or Vol[k] < p["volMult"] * av:
            k += 1
            continue
        hi_s, lo_s = max(b[2] for b in F[i0:k]), min(b[3] for b in F[i0:k])
        up_w, dn_w = (h - max(o, c)) / rg * 100, (min(o, c) - l) / rg * 100
        dd = -1 if h > hi_s and up_w >= p["wickPct"] else 1 if l < lo_s and dn_w >= p["wickPct"] else 0
        if not dd or not N.ok_side(dd, N.bias(E, t, c, p), p):
            k += 1
            continue
        stop = (h + buf) if dd < 0 else (l - buf)
        risk = (c - stop) * dd
        tgt = None
        if p["exit"] == "target":
            tgt = VF[k] if p["tgt"] == "vwap" and k < len(VF) else c + dd * p["tgtR"] * risk
        if risk <= 0 or (tgt is not None and ((tgt - c) * dd <= 0 or (p["minRR"] and abs(tgt - c) < p["minRR"] * risk))):
            k += 1
            continue
        x, j = N.trade(F, k, dd, c, stop, tgt, risk, p, E, flat, vm=Vol[k] / av, wk=up_w if dd < 0 else dn_w, ses=t >= rth0)
        out.append((x, (max(i0, 20), last, flat)))
        k = j + 1
    return out


def all_trades(sub, p, cells=None, prog=None):
    return N.run(sub, p, engine, 139, prog, "candles + volume")


def decorate(x, p):
    N.decorate_common(x)
    v = x.get("vm") or 0
    x["vm"] = "under 5×" if v < 5 else "5-8×" if v < 8 else "8×+"
    v = x.get("wk") or 0
    x["wk"] = "under 50%" if v < 50 else "50-70%" if v < 70 else "70%+"
    x["ses"] = "NYSE" if x.get("ses") else "overnight"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
