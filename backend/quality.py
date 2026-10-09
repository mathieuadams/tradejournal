"""Trade quality scorecard: how good was the entry, the exit and the position size.

All measures use the UNDERLYING's price path (the stock for options), so stock and option trades are comparable.
"Favorable" means up for long stock / long calls / short puts, down for the opposite.

Entry
  entryEff      (max - entry) / (max - min) over the holding period        1.0 = bought the exact low
  entryDayPct   where the entry sat in that day's range                    0 = day's low (for a bullish trade)
  heatAtr       worst move against you after entry, in daily ATRs          lower is better
  extAtr        distance above the 21 EMA at entry, in ATRs                >1.5 = chasing
Exit
  exitEff       (exit - min) / (max - min) over the holding period         1.0 = sold the exact high
  captured      (exit - entry) / (max - entry)                             share of the best move you kept
  afterUpAtr    best favorable move in the 5 days after exit, in ATRs      big = sold too early
  afterDownAtr  worst adverse move in the 5 days after exit, in ATRs       big = good exit
Total
  totalEff      (exit - entry) / (max - min)
Size
  costPctAccount, sizeVsTypical (vs your median position of the previous 50 trades), riskVsPlan (planned $ risk vs setting)
"""
import time
from datetime import datetime, timedelta

import db
from charts import _yahoo
from grouping import describe
from util import iso, now_ny, parse_iso

VERSION = 1


def _atr(bars, n=21):
    if len(bars) < n + 1:
        return None
    trs = [max(b["h"], bars[i - 1]["c"]) - min(b["l"], bars[i - 1]["c"]) for i, b in enumerate(bars) if i]
    k, e = 2 / (n + 1), sum(trs[:n]) / n
    for x in trs[n:]:
        e = x * k + e * (1 - k)
    return e


def _ema(xs, n=21):
    if len(xs) < n:
        return None
    k, e = 2 / (n + 1), sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _key(sym):
    d = describe(sym)
    return (f"{d['underlying']}=F" if d["assetType"] == "future" else d["underlying"].replace(".", "-")), d


def compute(trade, views_all, settings, fetch=_yahoo):
    """Returns the scorecard dict, or {"v":..., "missing": reason}."""
    if trade["status"] != "closed" or not trade.get("closeTs"):
        return {"v": VERSION, "missing": "open"}
    key, d = _key(trade["sym"])
    bullish = (trade["dir"] == "Long") == (d.get("optType") != "put")
    open_dt, close_dt = parse_iso(trade["openTs"]), parse_iso(trade["closeTs"])
    age = (datetime.utcnow() - open_dt).days
    tf = "5m" if age < 55 else "1h" if age < 700 else "1d"
    try:
        daily = fetch(key, "1d", open_dt - timedelta(days=60), close_dt + timedelta(days=12))
        intr = daily if tf == "1d" else fetch(key, tf, open_dt - timedelta(days=1), close_dt + timedelta(hours=8))
    except Exception as e:
        return {"v": VERSION, "missing": f"no price data ({str(e)[:80]})"}
    if not daily or not intr:
        return {"v": VERSION, "missing": "no price data"}

    cmp = 10 if tf == "1d" else 16
    o_key, c_key = trade["openTs"][:cmp], trade["closeTs"][:cmp]
    during = [b for b in intr if o_key <= b["t"][:cmp] <= c_key]
    if not during:
        return {"v": VERSION, "missing": "no bars during the trade"}
    stock = d["assetType"] == "stock"
    entry_u = trade["entry"] if stock else during[0]["c"] if tf != "1d" else during[0]["o"]
    exit_u = trade["exit"] if stock else during[-1]["c"]
    hi = max([b["h"] for b in during] + [entry_u, exit_u])
    lo = min([b["l"] for b in during] + [entry_u, exit_u])
    prior_daily = [b for b in daily if b["t"][:10] < trade["openTs"][:10]]
    atr = _atr(prior_daily) or (hi - lo) or 1e-9
    ema = _ema([b["c"] for b in prior_daily])

    fav_hi, fav_lo = (hi, lo) if bullish else (lo, hi)          # "max" = best price for the trade direction
    rng = abs(hi - lo) or 1e-9
    sg = 1 if bullish else -1
    entry_eff = _clip((fav_hi - entry_u) * sg / rng)
    exit_eff = _clip((exit_u - fav_lo) * sg / rng)
    total_eff = (exit_u - entry_u) * sg / rng
    best_move = (fav_hi - entry_u) * sg
    captured = (exit_u - entry_u) * sg / best_move if best_move > 0 else None
    heat = (entry_u - fav_lo) * sg / atr
    runup = best_move / atr

    day_bars = [b for b in intr if b["t"][:10] == trade["openTs"][:10]] if tf != "1d" else \
        [b for b in daily if b["t"][:10] == trade["openTs"][:10]]
    entry_day_pct = None
    if day_bars:
        dh, dl = max(b["h"] for b in day_bars), min(b["l"] for b in day_bars)
        if dh > dl:
            entry_day_pct = _clip((entry_u - dl) / (dh - dl)) if bullish else _clip((dh - entry_u) / (dh - dl))

    after = [b for b in daily if b["t"][:10] > trade["closeTs"][:10]][:5]
    after_up = after_down = None
    if after:
        a_hi, a_lo = max(b["h"] for b in after), min(b["l"] for b in after)
        after_up = max(0.0, ((a_hi - exit_u) if bullish else (exit_u - a_lo)) / atr)
        after_down = max(0.0, ((exit_u - a_lo) if bullish else (a_hi - exit_u)) / atr)
    ext = (entry_u - ema) * sg / atr if ema else None

    # ---- size ----
    cost = trade["entry"] * trade["qty"] * (trade.get("mult") or 1)
    prev_costs = sorted(v["cost"] for v in views_all if v.get("cost") and v["openTs"] < trade["openTs"])[-50:]
    typical = sorted(prev_costs)[len(prev_costs) // 2] if len(prev_costs) >= 5 else None
    acct = settings.get("accountSize") or 0
    size = {"cost": round(cost, 2),
            "costPctAccount": round(cost / acct * 100, 1) if acct else None,
            "typicalCost": round(typical, 2) if typical else None,
            "sizeVsTypical": round(cost / typical, 2) if typical else None,
            "maxLoss": round(cost, 2) if d["assetType"] == "option" and trade["dir"] == "Long" else None}

    # ---- scores (0-100, transparent weights) ----
    s_entry = 100 * (0.5 * entry_eff + 0.3 * (1 - _clip(heat / 1.5)) +
                     0.2 * (1 - _clip(((ext or 0) - 1.0) / 2.0)))
    post = 0.5 if after_up is None else _clip(0.5 + ((after_down or 0) - after_up) / 2)
    s_exit = 100 * (0.5 * exit_eff + 0.3 * _clip(captured if captured is not None else 0) + 0.2 * post)
    svt = size["sizeVsTypical"]
    maxpct = settings.get("maxPositionPct") or 0
    s_size = 100.0
    if svt:
        s_size -= min(60, abs(svt - 1) * 50) if svt > 1 else min(30, (1 - svt) * 30)
    if size["costPctAccount"] and maxpct and size["costPctAccount"] > maxpct:
        s_size -= min(40, (size["costPctAccount"] - maxpct) * 4)
    s_size = max(0, s_size)

    verdicts = []
    if entry_day_pct is not None:
        verdicts.append(f"Entry was at {entry_day_pct * 100:.0f}% of the day's range "
                        f"({'near the low' if entry_day_pct <= 0.3 else 'near the high' if entry_day_pct >= 0.7 else 'mid-range'} for your direction).")
    verdicts.append(f"The stock went {heat:.1f} ATR against you and {runup:.1f} ATR in your favor while you held.")
    if ext is not None and ext > 1.5:
        verdicts.append(f"You entered {ext:.1f} ATR above the 21 EMA (extended).")
    if captured is not None:
        verdicts.append(f"You kept {max(0, captured) * 100:.0f}% of the best move available during the trade.")
    if after_up is not None:
        if after_up >= 1 and after_up > (after_down or 0):
            verdicts.append(f"After you exited, it ran another {after_up:.1f} ATR in your direction within 5 days (early exit).")
        elif (after_down or 0) >= 1:
            verdicts.append(f"After you exited, it moved {after_down:.1f} ATR against the trade within 5 days (good exit).")
    if svt and (svt >= 1.5 or svt <= 0.5):
        verdicts.append(f"Position size was {svt:.1f}x your typical size.")

    r2 = lambda x: None if x is None else round(x, 2)
    return {"v": VERSION, "tf": tf, "bullish": bullish, "underlyingEntry": r2(entry_u), "underlyingExit": r2(exit_u),
            "best": r2(fav_hi), "worst": r2(fav_lo), "atr": r2(atr),
            "entryEff": r2(entry_eff), "exitEff": r2(exit_eff), "totalEff": r2(total_eff), "captured": r2(captured),
            "heatAtr": r2(heat), "runupAtr": r2(runup), "entryDayPct": r2(entry_day_pct), "extAtr": r2(ext),
            "afterUpAtr": r2(after_up), "afterDownAtr": r2(after_down), "size": size,
            "scores": {"entry": round(s_entry), "exit": round(s_exit), "size": round(s_size)},
            "verdicts": verdicts, "computedAt": iso(now_ny())}


def for_trade(sub, trade, views_all=None, settings=None, force=False):
    from views import load_settings, load_views
    if not force and (trade.get("q") or {}).get("v") == VERSION:
        return trade["q"]
    views_all = views_all if views_all is not None else load_views(sub)
    settings = settings or load_settings(sub)
    q = compute(trade, views_all, settings)
    db.update(db.upk(sub), trade["SK"], {"q": q})
    trade["q"] = q
    return q


def analyze(sub):
    """Score closed trades that don't have a scorecard yet, within ~20 seconds per call."""
    from views import load_settings, load_views
    t0 = time.time()
    pk = db.upk(sub)
    todo = [t for t in db.q_prefix(pk, "TRADE#") if t["status"] == "closed" and (t.get("q") or {}).get("v") != VERSION]
    views_all, settings = load_views(sub), load_settings(sub)
    daily_cache = {}

    def fetch(sym, tf, s, e):
        if tf != "1d":
            return _yahoo(sym, tf, s, e)
        if sym not in daily_cache:   # one daily request per ticker covering all its trades
            ts = [t for t in todo if _key(t["sym"])[0] == sym]
            lo = min(parse_iso(t["openTs"]) for t in ts) - timedelta(days=60)
            hi = min(max(parse_iso(t["closeTs"]) for t in ts) + timedelta(days=12), datetime.utcnow())
            daily_cache[sym] = _yahoo(sym, "1d", lo, hi)
        return [b for b in daily_cache[sym] if s.strftime("%Y-%m-%d") <= b["t"][:10] <= e.strftime("%Y-%m-%d")]

    done = 0
    for t in todo:
        if time.time() - t0 > 20:
            break
        q = compute(t, views_all, settings, fetch)
        db.update(pk, t["SK"], {"q": q})
        done += 1
    return {"analyzed": done, "remaining": len(todo) - done}
