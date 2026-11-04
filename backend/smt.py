"""ES / NQ divergence (SMT): one index makes a new high (low), the other doesn't; fade it.

  At a `tf`-minute candle, one of NQ / ES trades above its highest high of the previous `look` candles while the other
  stays under its own: a bearish divergence (the mirror at the lows: bullish). Within `wait` candles, the first candle of
  `tradeSym` that closes against the move (red after a bearish divergence) is the entry, at its close.
  Stop: past the extreme since the divergence (+ `bufTicks`). Target (`tgt`): vwap = the session VWAP (skipped when it's
  under `minRR` x the risk), r = `tgtR` x the risk, or the managed exit. `hours`: rth (09:35-`lastEntry`) or all
  (from 18:00). One position at a time, at most `maxDay` a session, flat at 15:55. Bias: optional (none by default)."""
import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tradeSym": "NQ", "tf": 5, "look": 12, "wait": 3, "hours": "all", "lastEntry": "15:00", "maxDay": 3, "tgt": "vwap", "tgtR": 2.0,
           "minRR": 1.0, "bufTicks": 2, "bias": "none", "side": "both", "exit": "target", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("who", "Which one made the new extreme"), ("ses", "Session")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    ts = p.get("tradeSym") if p.get("tradeSym") in ("NQ", "ES") else (N.K.tickers_of(p)[0] if N.K.tickers_of(p)[0] in ("NQ", "ES") else "NQ")
    out = {"setup": "smt", "tradeSym": ts, "tf": 1 if str(p.get("tf")) == "1" else 15 if str(p.get("tf")) == "15" else 5,
           "hours": p.get("hours") if p.get("hours") in ("rth", "all") else "all", "lastEntry": N.hhmm(p.get("lastEntry"), "15:00", "09:40", "15:30"),
           "tgt": p.get("tgt") if p.get("tgt") in ("vwap", "r") else "vwap"}
    N.K.num(p, out, (("look", 3, 200, 12), ("wait", 1, 30, 3), ("maxDay", 1, 20, 3), ("minRR", 0, 10, 1.0)), ("look", "wait", "maxDay"))
    N.clean_common(p, out, bias="none")
    out["tickers"] = [ts]
    return out


def describe(p):
    other = "ES" if p["tradeSym"] == "NQ" else "NQ"
    tg = ("the session VWAP" + (f" (at least {p['minRR']:g}R)" if p["minRR"] else "") if p["tgt"] == "vwap" else f"{p['tgtR']:g}R") if p["exit"] == "target" else ""
    b = {"all3": ", with the 15 / 30 / 60-minute EMA bias", "h1": ", with the hourly EMA bias", "none": ""}[p["bias"]]
    return (f"{p['tradeSym']} {p['tf']}-minute ({'overnight + NYSE' if p['hours'] == 'all' else 'NYSE session'}): {p['tradeSym']} and {other} diverge (one takes "
            f"its {p['look']}-candle high / low, the other doesn't){b}; the first {p['tradeSym']} candle closing back within {p['wait']}: fade it at that close, "
            f"stop past the extreme + {p['bufTicks']} tick(s)" + (f", target {tg}" if tg else N.ifvg._exit_txt(p)) + f", at most {p['maxDay']} a session, flat at 15:55")


def tickers(sub, p):
    return ["NQ", "ES"]                                # both are needed


def day_trades(d, A, B, VF, E, p):
    """A: the traded contract's full-session candles; B: the other's, by time."""
    out, k, buf, n = [], p["look"], p["bufTicks"] * N.K.TICK, p["look"]
    rth0, flat = f"{d}T09:30", f"{d}T15:55"
    while k < len(A) and len(out) < p["maxDay"]:
        t = A[k][0]
        if t[:10] == d and N.K.end_of(t, p["tf"])[11:16] > p["lastEntry"]:
            break
        if (p["hours"] == "rth" and (t < rth0 or N.K.end_of(t, p["tf"])[11:16] < "09:35")) or "16:00" <= t[11:16] < "18:00":
            k += 1
            continue
        wa = A[k - n:k]
        wb = [B[x[0]] for x in wa if x[0] in B]
        if t not in B or len(wb) < n // 2:
            k += 1
            continue
        hA, lA, hB, lB = max(b[2] for b in wa), min(b[3] for b in wa), max(b[2] for b in wb), min(b[3] for b in wb)
        upA, upB = A[k][2] > hA, B[t][2] > hB
        dnA, dnB = A[k][3] < lA, B[t][3] < lB
        dd, who = (-1, "NQ" if (upA if p["tradeSym"] == "NQ" else upB) else "ES") if upA != upB else \
            (1, "NQ" if (dnA if p["tradeSym"] == "NQ" else dnB) else "ES") if dnA != dnB else (0, None)
        if not dd:
            k += 1
            continue
        hit = None
        for j in range(k, min(len(A), k + p["wait"] + 1)):
            o, c = A[j][1], A[j][4]
            if (c - o) * dd > 0:
                hit = j
                break
        if hit is None:
            k += 1
            continue
        j = hit
        c = A[j][4]
        if not N.ok_side(dd, N.bias(E, A[j][0], c, p), p):
            k = j + 1
            continue
        seg = A[k:j + 1]
        stop = (max(b[2] for b in seg) + buf) if dd < 0 else (min(b[3] for b in seg) - buf)
        risk = (stop - c) * -dd
        if risk <= 0:
            k = j + 1
            continue
        tgt = None
        if p["exit"] == "target":
            if p["tgt"] == "vwap":
                tgt = VF[j] if j < len(VF) else None
                if tgt is None or (tgt - c) * dd <= 0 or (p["minRR"] and abs(tgt - c) < p["minRR"] * risk):
                    k = j + 1
                    continue
            else:
                tgt = c + dd * p["tgtR"] * risk
        x, jx = N.trade(A, j, dd, c, stop, tgt, risk, p, E, flat, who=who, ses=A[j][0] >= rth0, rt=A[k][0])
        out.append((x, (0 if p["hours"] == "all" else next((i for i, b in enumerate(A) if b[0] >= f"{d}T09:35"), len(A)), f"{d}T{p['lastEntry']}", flat)))
        k = jx + 1
    return out


def all_trades(sub, p, cells=None, prog=None):
    import random
    ts, other = p["tradeSym"], "ES" if p["tradeSym"] == "NQ" else "NQ"
    rows, Fall, fi, E = N.ifvg._series(ts, p)
    rows_b = {d: F for d, on, R, F, V in N.ifvg._series(other, p)[0]}
    trades, base = [], []
    rnd = random.Random(83)
    for d, on, R, F, V in rows:
        if len(F) < p["look"] + 3 or d not in rows_b:
            continue
        B = {b[0]: b for b in rows_b[d]}
        VF = N.K.full_vwap(ts, p["tf"], d)
        for x, (lo, hi_t, flat) in day_trades(d, F, B, VF, E, p):
            x.update(s=ts, d=d, cell=None)
            trades.append(x)
            base.append((d, x["side"], N.baseline(F, x, rnd, p, E, flat, lo, hi_t)))
    return trades, base


def decorate(x, p):
    N.decorate_common(x)
    x["who"] = f"{x.get('who')} took it" if x.get("who") else "—"
    x["ses"] = "NYSE" if x.get("ses") else "overnight"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, {**p, "tickers": [p["tradeSym"]]}, sym, start, end)
