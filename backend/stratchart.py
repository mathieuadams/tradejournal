"""Strategy charts: any pipeline strategy drawn on a ticker's chart with its own indicators and its entries / exits.

Day trading strategies (range50, orbfvg, silver): 1 / 5-minute candles, the last 1 / 3 / 5 / 10 sessions.
Swing strategies (daily rules, flow, breakout retest): 15-minute / 1-hour / daily candles, the last 10 / 30 / 90 / 180 days.
The trades are computed exactly like the backtest (on the strategy's own timeframe) and drawn on the chart's timeframe.
Everything a chart draws: bars [[t,o,h,l,c]], lines [{label,color,pts:[[t,v]],dash}], boxes [{label,color,t0,t1,lo,hi}],
marks [{t,price,label,color,shape}], shades [{t0,t1,color,label}], trades [{in,out,e,x,stop,tgt,side,why,r,pct}]."""
import json

DAY_SETUPS = ("range50", "orbfvg", "silver")
DAY_TF, DAY_DAYS = (1, 5), (1, 3, 5, 10)
SWING_TF, SWING_DAYS = ("15", "60", "D"), (10, 30, 90, 180)
RED, GREEN, BLUE, ORANGE, PURPLE, GREY, TEAL = "#e5534b", "#2da44e", "#5b8def", "#e0a544", "#a15cf0", "#8b949e", "#18b5c9"


def family(s):
    if s["kind"] == "intraday" and (s.get("ip") or {}).get("setup", "range50") in DAY_SETUPS:
        return "day"
    return "swing"


def _tickers(sub, s):
    import pipeline
    if s["kind"] == "intraday":
        return pipeline._imod(s["ip"]).tickers(sub, s["ip"])
    if s["kind"] == "flow":
        import wallwatch
        return list(wallwatch.config(sub)["tickers"])
    return pipeline._universe_tickers(sub, s)


def view(sub, pid, sym=None, tf=None, days=None):
    import pipeline
    s = pipeline.get(sub, pid)
    fam = family(s)
    tk = _tickers(sub, s)
    sym = (sym or "").strip().upper()[:10] or (tk[0] if tk else "SPY")
    if fam == "day":
        tf = int(tf) if str(tf) in ("1", "5") else 1
        days = int(days) if str(days) in ("1", "3", "5", "10") else 3
        out = _day(sub, s, sym, tf, days)
    else:
        tf = str(tf) if str(tf) in SWING_TF else "D"
        days = int(days) if str(days) in ("10", "30", "90", "180") else 90
        out = _swing(sub, s, sym, tf, days)
    pos = [p for p in pipeline.positions(sub, pid) if p.get("symbol") == sym]
    out.update(symbol=sym, tickers=tk, tf=tf, days=days, family=fam, id=pid, name=s["name"], rule=pipeline.describe(s),
               paper=[{k: p.get(k) for k in ("status", "fillDay", "fillPrice", "exitDay", "exitPrice", "pct", "side")} for p in pos])
    return out


def _in_window(t, t0):
    return t and t >= t0


def _bars_out(by, days):
    ds = sorted(by)[-days:]
    return ds, [list(c) for d in ds for c in by[d]]


# ---------------------------------------------------------------- day trading
def _day(sub, s, sym, tf, days):
    import range50
    p = s["ip"]
    setup = p.get("setup", "range50")
    show_by = range50.bars(sub, sym, max(days + 3, 6), tf)
    ds, bars = _bars_out(show_by, days)
    t0 = ds[0] if ds else "9999"
    lines, boxes, marks, shades, trades = [], [], [], [], []
    for d in ds:                                       # the sessions the strategy trades in
        if setup == "silver":
            shades.append({"t0": f"{d}T10:00", "t1": f"{d}T11:00", "color": PURPLE, "label": "NY AM"})
            shades.append({"t0": f"{d}T14:00", "t1": f"{d}T15:00", "color": ORANGE, "label": "NY PM"})
    if setup == "range50":
        by = range50.bars(sub, sym, max(days + 3, 6), p["tf"])
        for d in ds:
            for x in range50.setups(by.get(d) or [], p):
                lv = x["lv"]
                boxes.append({"label": "range", "color": GREY, "t0": x.get("legT") or x["touch"], "t1": x["out"], "lo": lv["bot"], "hi": lv["top"]})
                lines.append({"label": "50%", "color": BLUE, "pts": [[x["touch"], lv["mid"]], [x["out"], lv["mid"]]], "dash": True})
                lines.append({"label": "25% entry", "color": ORANGE, "pts": [[x["touch"], lv["ent"]], [x["out"], lv["ent"]]], "dash": True})
                marks.append({"t": x["touch"], "price": lv["mid"], "label": "50% touch", "color": BLUE, "shape": "dot"})
                trades.append(_tr(x, x["e"] - (1 if x["side"] == "long" else -1) * x["stopDist"], x["e"] + (1 if x["side"] == "long" else -1) * x["tgtDist"]))
    elif setup == "orbfvg":
        import datetime as dt
        import orb
        by = range50.bars(sub, sym, max(days + 3, 6), 1)
        end = (dt.datetime.strptime("09:30", "%H:%M") + dt.timedelta(minutes=p["rangeMin"])).strftime("%H:%M")
        for d in ds:
            B = by.get(d) or []
            rng = [b for b in B if b[0][11:16] < end]
            if rng:
                boxes.append({"label": f"opening range ({p['rangeMin']} min)", "color": BLUE, "t0": rng[0][0], "t1": f"{d}T16:00",
                              "lo": min(b[3] for b in rng), "hi": max(b[2] for b in rng)})
            for x in orb.setups(B, p):
                boxes.append({"label": "FVG", "color": PURPLE, "t0": x["fvgT"], "t1": x["out"], "lo": x["fvgBot"], "hi": x["fvgTop"]})
                marks.append({"t": x["bk"], "price": x["rlo"] if x["side"] == "short" else x["rhi"], "label": "breakout", "color": GREY, "shape": "dot"})
                marks.append({"t": x["eng"], "price": x["e"], "label": "engulfing", "color": ORANGE, "shape": "dot"})
                trades.append(_tr(x, x["stop"], x["tgt"]))
    elif setup == "silver":
        import silver
        by = range50.bars(sub, sym, max(days + 5, 8), p["tf"])
        C = [c for d in sorted(by) for c in by[d]]
        trace = []
        for x in silver.run(C, p, trace):
            if not _in_window(x["in"], t0):
                continue
            boxes.append({"label": "FVG", "color": PURPLE, "t0": x["fvgT"], "t1": x["out"], "lo": min(x["fvgBot"], x["fvgTop"]), "hi": max(x["fvgBot"], x["fvgTop"])})
            if x.get("sweepT"):
                marks.append({"t": x["sweepT"], "price": x["sweepLvl"], "label": "SSL sweep" if x["side"] == "long" else "BSL sweep",
                              "color": GREEN if x["side"] == "long" else RED, "shape": "up" if x["side"] == "long" else "down"})
            trades.append(_tr(x, x["stop"], x["tgt"]))
        for k, (lab, col) in ((1, ("buy-side liquidity (swing high)", RED)), (2, ("sell-side liquidity (swing low)", GREEN))):
            cur, start = None, None
            for row in trace:
                if row[0] < t0:
                    continue
                if row[k] != cur:
                    if cur is not None:
                        lines.append({"label": lab, "color": col, "pts": [[start, cur], [row[0], cur]]})
                    cur, start = row[k], row[0]
            if cur is not None and bars:
                lines.append({"label": lab, "color": col, "pts": [[start, cur], [bars[-1][0], cur]]})
    trades = [t for t in trades if _in_window(t["in"], t0)]
    return {"bars": bars, "lines": lines, "boxes": boxes, "marks": marks, "shades": shades, "trades": trades}


def _tr(x, stop, tgt):
    return {"in": x["in"], "out": x["out"], "e": x["e"], "x": x["x"], "stop": stop, "tgt": tgt, "side": x["side"], "why": x.get("why"),
            "r": round(x["r"], 2) if x.get("r") is not None else None, "pct": round(x["pct"], 3)}


# ---------------------------------------------------------------- swing
def _swing(sub, s, sym, tf, days):
    import macro
    import plab
    daily = plab._bars(sym, 2) or {}
    dds = sorted(daily)
    if tf == "D":
        show = dds[-days:]
        bars = [[d, *daily[d][:4]] for d in show]
    else:
        import retest
        by = retest.intraday(sub, sym, days + 3, int(tf))
        show, bars = _bars_out(by, days)
    t0 = show[0] if show else "9999"
    cl = [daily[d][3] for d in dds]
    lines, boxes, marks, shades, trades = [], [], [], [], []

    def dline(label, color, vals, dash=False):
        pts = [[d, v] for d, v in zip(dds, vals) if d >= t0 and v is not None]
        if len(pts) > 1:
            lines.append({"label": label, "color": color, "pts": pts, "dash": dash, "step": tf != "D"})
    if s["kind"] == "intraday":                        # breakout retest
        import retest
        p = s["ip"]
        e10, e20 = macro.ema(cl, 10), macro.ema(cl, 20)
        if p["ema"] in ("10", "either"):
            dline("EMA 10 (daily)", BLUE, e10)
        if p["ema"] in ("20", "either"):
            dline("EMA 20 (daily)", ORANGE, e20)
        intra = retest.intraday(sub, sym, days + p["window"] + 5, p["tf"])
        tr, _ = retest.setups(daily, intra, p, sym)
        for x in tr:
            if x["out"][:10] < t0:
                continue
            lines.append({"label": f"breakout level ({p['lookback']}-day high)", "color": PURPLE, "pts": [[x["bkDay"], x["lvl"]], [x["out"], x["lvl"]]], "dash": True})
            marks.append({"t": x["bkDay"], "price": x["lvl"], "label": "breakout", "color": PURPLE, "shape": "dot"})
            marks.append({"t": x["touch"], "price": x["e"], "label": x["pat"], "color": ORANGE, "shape": "up"})
            trades.append(_tr(x, x["stop"], x["tgt"]))
    else:
        import pipeline
        res = pipeline.results(sub, s["id"])
        e21, e50, e200 = macro.ema(cl, 21), macro.ema(cl, 50), macro.ema(cl, 200)
        dline("EMA 21", BLUE, e21)
        dline("EMA 50", ORANGE, e50)
        dline("EMA 200", PURPLE, e200)
        if s["kind"] == "daily":
            s1 = {**s, "universe": {"tickers": [sym]}}
            B = pipeline.build(sub, s1)
            A, X, bdays = B["X"]["A"].get(sym), B["X"], B["days"]
            cx = (B.get("ctxs") or {}).get(sym) or {}
            if A:
                ck = s.get("cond") or {}
                if "hi52" in ck:
                    dline("52-week high", GREY, [cx["hi52"][bdays.index(d)] if d in bdays and cx.get("hi52") else None for d in dds], True)
                if "ath" in ck:
                    dline("all-time high", GREY, [cx["ath"][bdays.index(d)] if d in bdays and cx.get("ath") else None for d in dds], True)
                pos_ = {d: i for i, d in enumerate(bdays)}
                if "sqz" in ck or "sqz5" in ck:
                    for d in show if tf == "D" else []:
                        i = pos_.get(d)
                        if i is not None and cx.get("sq") and cx["sq"][i]:
                            marks.append({"t": d, "price": A["r"][i][2] if A["r"][i] else None, "label": "squeeze", "color": TEAL, "shape": "dot", "small": True})
                d_ = -1 if s["side"] == "short" else 1
                for r in B["rows"]:
                    if r["s"] != sym:
                        continue
                    i = r["i"]
                    if "base" in ck and cx.get("bh") and cx["bh"][i] is not None and bdays[i] >= t0:
                        boxes.append({"label": "base (15 days)", "color": GREY, "t0": bdays[max(0, i - 15)], "t1": bdays[i], "lo": cx["bl"][i], "hi": cx["bh"][i]})
                    xr = pipeline._tr(s, A, r, d_, s["hold"], s["stop"], 0)
                    if not xr or bdays[min(len(bdays) - 1, i + 1)] < t0:
                        continue
                    e = A["r"][i + 1][0]
                    xp = e * (1 + d_ * xr[0] / 100)
                    stop = e - d_ * s["stop"] * A["atr"][i] if s.get("stop") else None
                    marks.append({"t": bdays[i], "price": A["r"][i][3], "label": "signal", "color": ORANGE, "shape": "up" if d_ > 0 else "down"})
                    trades.append({"in": bdays[i + 1], "out": bdays[xr[1]], "e": e, "x": xp, "stop": stop, "tgt": None, "side": s["side"],
                                   "why": "time", "r": None, "pct": round(xr[0], 3)})
        else:                                          # flow: the alerts + the validation's trades of this ticker
            import uoa
            al = uoa.stored(sub, sym).get(s.get("flowSide", "call")) or []
            seen = set()
            for a in al:
                d = a["t"][:10]
                if d >= t0 and d not in seen and d in daily:
                    seen.add(d)
                    marks.append({"t": d, "price": daily[d][2], "label": f"{s.get('flowSide', 'call')} alert", "color": GREEN if s.get("flowSide", "call") == "call" else RED,
                                  "shape": "down", "small": True})
            tr = json.loads((pipeline.db.get(pipeline._pk(sub), pipeline.TRD + s["id"]) or {}).get("data") or "{}").get(sym, [])
            for t_in, t_out, pct in tr:
                if t_out < t0 or t_in not in daily:
                    continue
                e = daily[t_in][0]
                trades.append({"in": t_in, "out": t_out, "e": e, "x": e * (1 + pct / 100), "stop": None, "tgt": None, "side": "long",
                               "why": "time", "r": None, "pct": pct})
    return {"bars": bars, "lines": lines, "boxes": boxes, "marks": marks, "shades": shades, "trades": trades}
