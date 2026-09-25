"""Merge derived trades with the user's journal into one read model.

R multiple, targetR and MAE/MFE (in R) need the planned stop, so they are only
filled in when the user logged a plan with a stop.
"""
import db
from datetime import date
from grouping import describe

DEFAULT_SETUPS = ["Opening range breakout", "VWAP reclaim", "Pullback", "Breakout", "Episodic pivot",
                  "Failed breakdown", "Parabolic short"]
MISTAKES = ["Revenge", "Moved stop", "Oversized", "Chased", "Early exit", "No plan", "Overtrading"]
EMOTIONS = ["Calm", "Confident", "Hesitant", "FOMO", "Frustrated", "Tired"]
DEFAULT_SETTINGS = {
    "riskPerTrade": 200,
    "setups": DEFAULT_SETUPS,
    "prop": {"enabled": False, "account": "", "start": "", "balance": 50000, "trailing": 2500,
             "target": 3000, "dailyLoss": 1000},
    "liveCoach": {"enabled": False, "premarket": True, "preclose": True, "entry": True},
    "rules": "",
    "accountSize": 0,
    "maxPositionPct": 10,
}


def merge(t, j=None, reviewed=False):
    j = j or {}
    plan = j.get("plan") or {}
    ts = t["openTs"]
    v = {
        "id": t["id"], "acct": t["acct"], "sym": t["sym"], "dir": t["dir"], "status": t["status"],
        "openTs": ts, "closeTs": t.get("closeTs"), "date": ts[:10], "time": ts[11:16],
        "min": int(ts[11:13]) * 60 + int(ts[14:16]),
        "qty": t["qty"], "entry": t["entry"], "exit": t.get("exit"), "gross": t["gross"],
        "fees": t["fees"], "net": t["net"], "hold": t.get("hold"), "mult": t.get("mult", 1),
        "setup": j.get("setup") or "", "tags": j.get("tags") or [], "emotion": j.get("emotion") or "",
        "notes": j.get("notes") or "", "plan": plan, "reviewed": reviewed,
        "r": None, "riskD": None, "targetR": None, "mae": None, "mfe": None,
    }
    d = describe(t["sym"])
    v.update(d)
    v["ctx"] = t.get("ctx")
    v["cost"] = round(t["entry"] * t["qty"] * (t.get("mult") or 1), 2)          # capital put into the position
    v["retPct"] = round(t["net"] / v["cost"] * 100, 2) if v["cost"] and t["status"] == "closed" else None
    v["dte"] = (date.fromisoformat(d["expiry"]) - date.fromisoformat(v["date"])).days if d["expiry"] else None
    stop = plan.get("stop")
    if stop is not None and t["status"] == "closed":
        rpu = abs(t["entry"] - stop)
        if rpu > 0:
            sg = 1 if t["dir"] == "Long" else -1
            v["riskD"] = round(rpu * t["qty"] * v["mult"], 2)
            v["r"] = round(t["net"] / v["riskD"], 3)
            if plan.get("target") is not None:
                v["targetR"] = round(abs(plan["target"] - t["entry"]) / rpu, 2)
            if t.get("maePx") is not None:
                v["mae"] = round(max(0.0, (t["entry"] - t["maePx"]) * sg) / rpu, 3)
            if t.get("mfePx") is not None:
                v["mfe"] = round(max(0.0, (t["mfePx"] - t["entry"]) * sg) / rpu, 3)
    return v


def load_views(sub):
    pk = db.upk(sub)
    trades = db.q_prefix(pk, "TRADE#")
    journal = {i["SK"][5:]: i for i in db.q_prefix(pk, "JRNL#")}
    reviewed = {i["SK"][7:] for i in db.q_prefix(pk, "REVIEW#")}
    return [merge(t, journal.get(t["id"]), t["id"] in reviewed) for t in trades]


def find_trade(sub, trade_id):
    for t in db.q_prefix(db.upk(sub), "TRADE#"):
        if t["id"] == trade_id:
            return t
    return None


def load_settings(sub):
    p = db.get(db.upk(sub), "PROFILE") or {}
    s = {**DEFAULT_SETTINGS, **(p.get("settings") or {})}
    s["prop"] = {**DEFAULT_SETTINGS["prop"], **(s.get("prop") or {})}
    s["liveCoach"] = {**DEFAULT_SETTINGS["liveCoach"], **(s.get("liveCoach") or {})}
    return s
