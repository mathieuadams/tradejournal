"""Deterministic analytics. The AI never computes numbers; it only explains these."""
import math

LEAK_TAGS = [("Revenge", "Revenge trades"), ("Moved stop", "Moved stops"),
             ("Oversized", "Oversized trades"), ("Chased", "Chased entries")]


def closed(ts):
    return [t for t in ts if t["status"] == "closed"]


def _avg(a):
    return sum(a) / len(a) if a else 0.0


def stats(ts):
    ts = sorted(closed(ts), key=lambda t: t["openTs"])
    wins = [t for t in ts if t["net"] > 0]
    losses = [t for t in ts if t["net"] <= 0]
    gw, gl = sum(t["net"] for t in wins), sum(t["net"] for t in losses)
    rs = [t["r"] for t in ts if t.get("r") is not None]
    eq = peak = dd = 0.0
    sw = sl = cw = cl = 0
    for t in ts:
        eq += t["net"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        if t["net"] > 0:
            cw, cl = cw + 1, 0
        else:
            cl, cw = cl + 1, 0
        sw, sl = max(sw, cw), max(sl, cl)
    daily = {}
    for t in ts:
        daily[t["date"]] = daily.get(t["date"], 0) + t["net"]
    dv = list(daily.values())
    m = _avg(dv)
    sd = math.sqrt(_avg([(x - m) ** 2 for x in dv])) if dv else 0
    return {
        "trades": len(ts), "net": round(gw + gl, 2),
        "win_rate": round(len(wins) / len(ts), 3) if ts else 0,
        "profit_factor": round(gw / -gl, 2) if gl else None,
        "expectancy_r": round(_avg(rs), 3) if rs else None,
        "avg_win": round(_avg([t["net"] for t in wins]), 2),
        "avg_loss": round(_avg([t["net"] for t in losses]), 2),
        "max_drawdown": round(dd, 2), "sharpe": round(m / sd * math.sqrt(252), 2) if sd else None,
        "longest_win_streak": sw, "longest_loss_streak": sl,
    }


def leaks(ts):
    """Each trade counts once: first matching mistake tag, else late session (after 11:00), else clean."""
    buckets = {k: {"key": k, "label": label, "dollars": 0.0, "trades": 0} for k, label in LEAK_TAGS}
    late = {"key": "late", "label": "New trades after 11:00", "dollars": 0.0, "trades": 0}
    clean = {"dollars": 0.0, "trades": 0}
    for t in closed(ts):
        k = next((k for k, _ in LEAK_TAGS if k in t["tags"]), None)
        b = buckets[k] if k else late if t["min"] >= 660 else clean
        b["dollars"] += t["net"]
        b["trades"] += 1
    out = [b for b in list(buckets.values()) + [late] if b["trades"]]
    for b in out + [clean]:
        b["dollars"] = round(b["dollars"], 2)
    return {"clean": clean, "leaks": out}


def patterns(ts):
    ts = sorted(closed(ts), key=lambda t: t["openTs"])
    out = []
    by_day = {}
    for t in ts:
        by_day.setdefault(t["date"], []).append(t)
    after, other = [], []
    for day in by_day.values():
        for i, t in enumerate(day):
            (after if i >= 2 and day[i - 1]["net"] < 0 and day[i - 2]["net"] < 0 else other).append(t)
    if len(after) >= 4:
        out.append({"finding": "Average net per trade after two losing trades in a row vs. otherwise",
                    "after_two_losses": round(_avg([t["net"] for t in after]), 2),
                    "otherwise": round(_avg([t["net"] for t in other]), 2), "n": len(after)})
    by_setup = {}
    for t in ts:
        if t["setup"]:
            by_setup[t["setup"]] = by_setup.get(t["setup"], 0) + t["net"]
    if len(by_setup) > 1:
        best = max(by_setup, key=by_setup.get)
        out.append({"finding": "Net P&L of best setup vs. all other setups", "best_setup": best,
                    "best_setup_net": round(by_setup[best], 2),
                    "other_setups_net": round(sum(v for k, v in by_setup.items() if k != best), 2)})
    am = [t["r"] for t in ts if t.get("r") is not None and t["min"] < 660]
    pm = [t["r"] for t in ts if t.get("r") is not None and t["min"] >= 660]
    if len(pm) >= 4 and am:
        out.append({"finding": "Expectancy in R before vs. after 11:00",
                    "before_11": round(_avg(am), 3), "after_11": round(_avg(pm), 3), "n_after": len(pm)})
    left = [t for t in ts if t.get("r") is not None and t.get("mfe") is not None
            and t["r"] > 0 and t["mfe"] >= 2 and t["r"] < 1]
    if left:
        out.append({"finding": "Winners that reached +2R but were closed under +1R", "n": len(left),
                    "dollars_left": round(sum((min(t["mfe"], t.get("targetR") or 2) - t["r"]) * t["riskD"]
                                              for t in left), 2)})
    return out


def breakdown(ts, by):
    keyf = {
        "setup": lambda t: t["setup"] or "No setup",
        "symbol": lambda t: t["sym"],
        "hour": lambda t: f"{t['time'][:2]}:00",
        "weekday": lambda t: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
            __import__("datetime").date.fromisoformat(t["date"]).weekday()],
        "emotion": lambda t: t["emotion"] or "None",
        "account": lambda t: t["acct"],
        "direction": lambda t: t["dir"],
    }
    groups = {}
    if by == "tag":
        for t in closed(ts):
            for g in (t["tags"] or ["No tags"]):
                groups.setdefault(g, []).append(t)
    else:
        f = keyf.get(by)
        if not f:
            return {"all": stats(ts)}
        for t in closed(ts):
            groups.setdefault(f(t), []).append(t)
    return {k: stats(v) for k, v in groups.items()}
