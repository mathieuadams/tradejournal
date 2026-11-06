"""Intraday momentum: the first half hour predicts the last one (Gao, Han, Li & Zhou, 2018).

  The signal: the move from `from` (close = the previous session's 16:00 close, so the overnight counts; open = today's
  09:30 open) to 10:00. When it is at least `minMovePct`% (and the EMA bias agrees, if set), enter at `entryAt` (the
  close of the candle before it) in its direction (`mode` follow) or against it (fade); out at 16:00 (the 15:55 candle's
  close), or at the stop: `stopMult` x the average 5-minute range (0 = none; R then counts 10x that range)."""
import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 5, "from": "close", "minMovePct": 0.0, "entryAt": "15:30", "mode": "follow", "stopMult": 0.0, "bias": "none",
           "side": "both", "exit": "target", "tgtR": 0.0, "days": 500}
FACTORS = [("side", "Side"), ("wd", "Weekday"), ("mv", "First half hour's move"), ("agree", "Mid-day (10:00 to entry) moved the same way")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "imom", "tf": 5, "from": p.get("from") if p.get("from") in ("close", "open") else "close",
           "entryAt": p.get("entryAt") if p.get("entryAt") in ("14:00", "15:00", "15:30", "15:45") else "15:30", "mode": p.get("mode") if p.get("mode") in ("follow", "fade") else "follow"}
    N.K.num(p, out, (("minMovePct", 0, 5, 0.0), ("stopMult", 0, 50, 0.0)))
    N.clean_common(p, out, bias="none")
    if p.get("tgtR") in (None, ""):
        out["tgtR"] = 0.0
    return out


def describe(p):
    fr = "yesterday's 16:00 close" if p["from"] == "close" else "the 09:30 open"
    return (f"{', '.join(p['tickers'])}: the move from {fr} to 10:00" + (f" (at least {p['minMovePct']:g}%)" if p["minMovePct"] else "")
            + f": {'follow' if p['mode'] == 'follow' else 'fade'} it from {p['entryAt']} to 16:00"
            + (f", stop {p['stopMult']:g}× the average 5-minute range" if p["stopMult"] else ", no stop") + N.exit_txt(p))


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    i0, i10 = N.idx_at(F, f"{d}T09:30"), N.idx_at(F, f"{d}T09:55")
    hm = p["entryAt"]
    m = int(hm[:2]) * 60 + int(hm[3:]) - 5
    k = N.idx_at(F, f"{d}T{m // 60:02d}:{m % 60:02d}")
    if None in (i0, i10, k) or k <= i10:
        return []
    base = N.prev_close(prev) if p["from"] == "close" else F[i0][1]
    if not base:
        return []
    mv = (F[i10][4] - base) / base * 100
    if not mv or abs(mv) < p["minMovePct"]:
        return []
    dd = (1 if mv > 0 else -1) * (1 if p["mode"] == "follow" else -1)
    c = F[k][4]
    if not N.ok_side(dd, N.bias(E, F[k][0], c, p), p):
        return []
    a = N.avg_range(F, k, prev=prev)
    if a <= 0:
        return []
    risk = (p["stopMult"] or 10) * a
    stop = c - dd * risk if p["stopMult"] else c - dd * N.FAR
    x, j = N.trade(F, k, dd, c, stop, None, risk, p, E, f"{d}T15:55", mv=mv, agree=(c - F[i10][4]) * (1 if mv > 0 else -1) > 0)
    return [(x, (i10 + 1, f"{d}T15:50", f"{d}T15:55"))]


def all_trades(sub, p, cells=None, prog=None):
    return N.run(sub, p, engine, 107, prog, "the first and last half hour")


def decorate(x, p):
    N.decorate_common(x)
    v = abs(x.get("mv") or 0)
    x["mv"] = "under 0.25%" if v < 0.25 else "0.25-0.5%" if v < 0.5 else "0.5-1%" if v < 1 else "1%+"
    x["agree"] = "yes" if x.get("agree") else "no"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
