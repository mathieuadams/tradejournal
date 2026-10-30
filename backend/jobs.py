"""Background work (its own Lambda, 15 minutes): Databento downloads, studies, validations, the paper simulation and
the scheduled refresh. A long download continues in a new invocation when its time runs out.

Status items (user partition): JOB#data, JOB#strat#<id> -> {status: running | done | error, msg, at}."""
import json
import os
import time
from datetime import date, timedelta

import databento
import db
import futdata
import strategy
from util import iso, now_ny

INVOKE = None               # tests / dev server: a function(payload) instead of the Lambda call


def _invoke(payload):
    if INVOKE:
        return INVOKE(payload)
    import boto3
    fn = os.environ.get("JOB_FUNCTION") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    boto3.client("lambda").invoke(FunctionName=fn, InvocationType="Event", Payload=json.dumps(payload).encode())


def status(sub, name):
    it = db.get(db.upk(sub), "JOB#" + name) or {}
    return {k: v for k, v in it.items() if k not in ("PK", "SK")}


def set_status(sub, name, **kw):
    it = db.get(db.upk(sub), "JOB#" + name) or {"PK": db.upk(sub), "SK": "JOB#" + name}
    it.update(kw, at=iso(now_ny()))
    db.put(it)


def start(sub, name, job, params):
    cur = status(sub, name)
    if cur.get("status") == "running" and cur.get("at", "") > iso(now_ny() - timedelta(minutes=20)):
        return cur
    set_status(sub, name, status="running", msg="Starting…", job=job)
    _invoke({"sub": sub, "name": name, "job": job, "params": params})
    return status(sub, name)


def _deadline(ctx):
    try:
        return time.time() + ctx.get_remaining_time_in_millis() / 1000 - 90
    except Exception:
        return time.time() + 600


def handler(event, ctx=None):
    if event.get("job") == "scheduled":
        return scheduled(ctx)
    sub, name, job, p = event["sub"], event.get("name") or "data", event["job"], event.get("params") or {}
    prog = lambda m: set_status(sub, name, status="running", msg=m)
    dl = _deadline(ctx)
    try:
        if job == "candles":
            syms, start0 = list(p["syms"]), p.get("from0") or p["from"]
            while syms:
                rest = futdata.download_candles(sub, syms[0], p["from"], p["to"], prog, dl)
                if rest:                               # out of time: the rest in a new invocation
                    _invoke({**event, "params": {**p, "syms": syms, "from": rest, "from0": start0}})
                    return {"continued": rest}
                syms.pop(0)
                p = {**p, "from": start0}
            set_status(sub, name, status="done", msg=f"Candles {start0} → {p['to']} downloaded.")
        elif job == "footprint":
            todo = [(s, d) for s in p["syms"] for d in p["days"]]
            done = list(p.get("done") or [])
            for k, (s, d) in enumerate(todo):
                if f"{s}{d}" in done:
                    continue
                if time.time() > dl:
                    _invoke({**event, "params": {**p, "done": done}})
                    return {"continued": True}
                prog(f"{s}: trades of {d} ({len(done) + 1}/{len(todo)})")
                futdata.download_footprint(sub, s, d, force=bool(p.get("force")))
                done.append(f"{s}{d}")
            set_status(sub, name, status="done", msg=f"Footprint of {len(p['days'])} day(s) downloaded.")
        elif job == "study":
            strategy.study(sub, p["id"], prog)
            set_status(sub, name, status="done", msg="Study finished.")
        elif job == "validate":
            strategy.validate(sub, p["id"])
            set_status(sub, name, status="done", msg="Validation finished.")
        elif job == "paper":
            n = strategy.paper_tick(sub)
            set_status(sub, name, status="done", msg=f"{n} paper strateg{'y' if n == 1 else 'ies'} updated.")
        else:
            raise ValueError(f"unknown job {job}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        set_status(sub, name, status="error", msg=str(e)[:300])
    return {"ok": True}


def refresh(sub, footprint_today=True, ctx=None):
    """The latest candles of NQ and ES (from the last stored day), the day's footprint after the close, then the
    paper strategies."""
    st = databento.status(sub)
    if not st["connected"]:
        return "not connected"
    today = now_ny().strftime("%Y-%m-%d")
    if st["auto"]:
        for s in futdata.SYMS:
            last = futdata.last_bar_time(s)
            if not last:
                continue                               # nothing downloaded yet: the user starts with a backfill
            futdata.download_candles(sub, s, last[:10], today, None, _deadline(ctx))
    if st["autoFp"] and footprint_today and now_ny().strftime("%H:%M") >= "16:20" and date.fromisoformat(today).weekday() < 5:
        for s in futdata.SYMS:
            if futdata.last_bar_time(s):
                futdata.download_footprint(sub, s, today)
    strategy.paper_tick(sub)
    return "ok"


def scheduled(ctx=None):
    out = {}
    for sub in db.owners():
        try:
            out[sub] = refresh(sub, ctx=ctx)
        except Exception as e:
            print("refresh failed", sub, e)
            out[sub] = str(e)[:200]
    return out
