"""Strategies saved from the Explorer: a combination of conditions, an optional exit plan, and a mode.
  watch  every new evaluation that matches is tagged; results are tracked from the day the strategy was saved
  trade  a match turns the evaluation into a BUY even if some of the standard chart / gamma rules failed. Safety rules
         still apply: a liquid contract, no earnings soon, puts not piling in, position size, then Claude's check.
  off    kept, not applied
Results are forward-only (evaluations after the strategy was created), so they're never the data it was tuned on."""
import uuid

import db
from util import BadRequest, NotFound, iso, now_ny

SK = "STRATEGIES"
NOT_AT_ENTRY = {"taken", "claudeApprove", "claudeReject"}     # unknown when an evaluation is made
SAFETY_PREFIXES = ("Puts not piling in", "No earnings", "Not an excluded ETF", "Liquid contract", "Best contract",
                   "No call with delta", "One contract risks", "One spread risks")


def _doc(sub):
    return db.get(db.upk(sub), SK) or {}


def load(sub):
    return _doc(sub).get("items") or []


def _save(sub, items, **extra):
    d = _doc(sub)
    doc = {"PK": db.upk(sub), "SK": SK, "items": items, "active": d.get("active") or "", "activeStop": d.get("activeStop")}
    doc.update(extra)
    if doc["active"] and not any(x["id"] == doc["active"] for x in items):
        doc["active"] = ""                       # the active strategy was deleted: back to the bot rules
    db.put(doc)


STOPS = (10, 15, 20, 25, 30, 35, 40, 50, 60, 75)


def active(sub):
    """The strategy the bot trades ('' = the bot's own rules) and the chosen stop (None = the strategy's / settings')."""
    d = _doc(sub)
    sid = d.get("active") or ""
    s = next((x for x in d.get("items") or [] if x["id"] == sid), None) if sid else None
    return s, d.get("activeStop")


def active_info(sub):
    s, stop = active(sub)
    return {"id": s["id"] if s else "", "name": s["name"] if s else "Bot rules", "stop": stop,
            "exit": (s or {}).get("exit")}


def set_active(sub, body):
    sid = (body.get("strategy") or "").strip()
    items = load(sub)
    if sid and not any(x["id"] == sid for x in items):
        raise NotFound("Strategy not found.")
    stop = body.get("stop")
    stop = None if stop in (None, "", 0, "0") else float(stop)
    if stop is not None and not (5 <= stop <= 90):
        raise BadRequest("Stop must be between 5% and 90%.")
    _save(sub, items, active=sid, activeStop=stop)
    return active_info(sub)


def effective_stop(sub):
    """Stop % used to size and protect new trades: the chosen stop, else the active strategy's own stop."""
    s, stop = active(sub)
    if stop:
        return stop
    if s and (s.get("exit") or {}).get("stop"):
        return s["exit"]["stop"]
    return None


def _clean_exit(e):
    if not e:
        return None
    out = {"stop": float(e["stop"]), "target": float(e["target"]) if e.get("target") is not None else None,
           "trail": float(e["trail"]) if e.get("trail") else None, "maxDays": int(e["maxDays"]) if e.get("maxDays") else None,
           "invalidation": bool(e.get("invalidation", True))}
    if not (5 <= out["stop"] <= 90):
        raise BadRequest("Stop must be between 5% and 90%.")
    return out


def create(sub, body):
    import explorer
    conds = [c for c in (body.get("conds") or []) if isinstance(c, dict) and c.get("key") in explorer.FEATURES]
    if not conds:
        raise BadRequest("A strategy needs at least one condition.")
    bad = [c["key"] for c in conds if c["key"] in NOT_AT_ENTRY]
    if bad:
        raise BadRequest("These conditions aren't known when an evaluation is made: " + ", ".join(explorer.FEATURES[k][0] for k in bad))
    name = (body.get("name") or "").strip()[:60] or " + ".join(explorer.describe(c) for c in conds)[:60]
    items = load(sub)
    if len(items) >= 20:
        raise BadRequest("20 strategies at most: delete one first.")
    s = {"id": uuid.uuid4().hex[:10], "name": name, "conds": conds, "mode": body.get("mode") if body.get("mode") in ("watch", "trade", "off") else "watch",
         "exit": _clean_exit(body.get("exit")), "createdAt": iso(now_ny()), "backtest": body.get("backtest")}
    items.append(s)
    _save(sub, items)
    return s


def update(sub, sid, body):
    items = load(sub)
    s = next((x for x in items if x["id"] == sid), None)
    if not s:
        raise NotFound("Strategy not found.")
    if body.get("mode") in ("watch", "trade", "off"):
        s["mode"] = body["mode"]
    if "name" in body and (body.get("name") or "").strip():
        s["name"] = body["name"].strip()[:60]
    if "exit" in body:
        s["exit"] = _clean_exit(body.get("exit"))
    _save(sub, items)
    return s


def delete(sub, sid):
    items = [x for x in load(sub) if x["id"] != sid]
    _save(sub, items)
    return {"deleted": sid}


def apply(sub, rec, cfg=None):
    """Tag a new evaluation with the strategies it matches; a 'trade' match makes it a BUY when only non-safety rules
    failed, and attaches the strategy's exit plan to the proposed order."""
    import explorer
    act, act_stop = active(sub)
    if act:      # one strategy selected: it is the only one that can buy, the others just watch
        items = [{**s, "mode": "trade" if s["id"] == act["id"] else "watch"} for s in load(sub)
                 if s["id"] == act["id"] or s.get("mode") in ("watch", "trade")]
    else:
        items = [s for s in load(sub) if s.get("mode") in ("watch", "trade")]
    f = explorer.features_of(rec, explorer.bursts_by_ticker(db.upk(sub))) if items else {}
    hits = [s for s in items if all(explorer._match(f, c) for c in s["conds"])]
    if act and not any(s["id"] == act["id"] for s in hits):
        if rec.get("decision") == "BUY":
            rec["ruleDecision"] = "BUY"
            rec["decision"] = "WAIT"
            rec.setdefault("blocking", []).append(f"Not a match for the active strategy “{act['name']}”: only its matches are traded")
        rec.setdefault("checks", []).append({"group": "strategy", "ok": False, "required": True,
                                             "text": f"Active strategy “{act['name']}” not matched"})
    _stop_plan(rec, cfg, act_stop)
    if not hits:
        return rec
    rec["strategies"] = [{"id": s["id"], "name": s["name"], "mode": s["mode"]} for s in hits]
    trade = next((s for s in hits if s["mode"] == "trade"), None)
    p = rec.get("proposal")
    if trade and p:
        if trade.get("exit"):
            p["exitPlan"] = {**trade["exit"], **({"stop": act_stop} if act_stop else {})}
        safety = [b for b in rec.get("blocking") or [] if b.startswith(SAFETY_PREFIXES)]
        if rec.get("decision") != "BUY" and not safety:
            rec["ruleDecision"] = rec.get("decision")
            rec["decision"] = "BUY"
            rec["strategyBuy"] = trade["id"]
            rec["overridden"] = rec.get("blocking") or []
            rec["blocking"] = []
        rec.setdefault("checks", []).append({"group": "strategy", "ok": not safety or rec.get("decision") == "BUY", "required": False,
                                             "text": f"Strategy “{trade['name']}” matched" + (" — buying on it" if rec.get("strategyBuy") else
                                                     (f" — not bought: {safety[0]}" if safety else ""))})
    for s in hits:
        if s is not trade:
            rec.setdefault("checks", []).append({"group": "strategy", "ok": True, "required": False, "text": f"Strategy “{s['name']}” matched (watching)"})
    return rec


def _stop_plan(rec, cfg, stop):
    """A chosen stop is locked into the trade when it's proposed, so changing the selection later doesn't move it."""
    p = rec.get("proposal")
    if not p or not stop or not cfg:
        return
    import autotrader
    base = p.get("exitPlan") or {"target": autotrader.target_pct(cfg, p),
                                 "trail": cfg.get("trailPct") if cfg.get("trailAfterTarget") else None,
                                 "maxDays": None, "invalidation": True}
    p["exitPlan"] = {**base, "stop": stop}


def results(sub, days=60):
    """Forward-only results per strategy: evaluations tagged with it after it was created."""
    import explorer
    items = load(sub)
    if not items:
        return []
    rs = explorer.rows(sub, days)
    tags = {}
    for r in db.q_prefix(db.upk(sub), "BOT#"):
        for t in r.get("strategies") or []:
            tags.setdefault(t["id"], set()).add(r["id"])
    out = []
    for s in items:
        ids = tags.get(s["id"], set())
        m = [r for r in rs if r["id"] in ids and r["at"] >= s["createdAt"]]
        tagged = sum(1 for _ in ids)
        out.append({**s, "conds": [{**c, "text": explorer.describe(c)} for c in s["conds"]], "forward": explorer.stats(m), "tagged": tagged,
                    "items": sorted([{k: r[k] for k in ("id", "symbol", "at", "R", "status", "taken")} for r in m], key=lambda r: r["at"], reverse=True)[:30]})
    return out
