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
        return scheduled(ctx, live=bool(event.get("live")))
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
        elif job == "book":
            todo = [(s_, d) for s_ in p["syms"] for d in p["days"]]
            done = list(p.get("done") or [])
            for s_, d in todo:
                if f"{s_}{d}" in done:
                    continue
                if time.time() > dl - 240:                 # a day of the book can take a few minutes: start it fresh
                    _invoke({**event, "params": {**p, "done": done}})
                    return {"continued": True}
                prog(f"{s_}: order book of {d} ({len(done) + 1}/{len(todo)})")
                futdata.download_book(sub, s_, d, p.get("window") or "rth", force=bool(p.get("force")))
                done.append(f"{s_}{d}")
            set_status(sub, name, status="done", msg=f"Order book of {len(p['days'])} day(s) downloaded.")
        elif job == "mmbackfill":                       # rebuild past days of the market maker data (Unusual Whales)
            import uwhales
            todo = [(s_, d) for s_ in p["syms"] for d in p["days"]]
            done = list(p.get("done") or [])
            for s_, d in todo:
                if f"{s_}{d}" in done:
                    continue
                if time.time() > dl - 300:
                    _invoke({**event, "params": {**p, "done": done}})
                    return {"continued": True}
                prog(f"MM backfill {s_} {d} ({len(done) + 1}/{len(todo)})")
                try:
                    uwhales.backfill_day(sub, s_, d, prog)
                except Exception as e:                 # a holiday or a day without data: note it, go on
                    print("mm backfill", s_, d, e)
                done.append(f"{s_}{d}")
            set_status(sub, name, status="done", msg=f"MM data rebuilt for {len(p['days'])} day(s).")
        elif job == "opt0dte":                          # a strategy's trades on 0DTE options: download the option minutes
            import opt0dte
            ts = strategy.trades(sub, p["id"], "study")
            if not opt0dte.job(sub, ts, prog, dl, _invoke, event, int(p.get("dte") or 0), p.get("und") or "etf"):
                return {"continued": True}
            set_status(sub, name, status="done", msg=f"{int(p.get('dte') or 0)}DTE option prices downloaded.")
        elif job == "fundrefresh":                      # SEC EDGAR fundamentals of the screen's tickers
            import fundamentals
            syms, done, bad = list(p["syms"]), list(p.get("done") or []), list(p.get("bad") or [])
            for s_ in syms:
                if s_ in done:
                    continue
                if time.time() > dl:
                    _invoke({**event, "params": {**p, "done": done, "bad": bad}})
                    return {"continued": True}
                prog(f"SEC EDGAR {len(done) + 1}/{len(syms)}: {s_}")
                try:
                    fundamentals.company(sub, s_, force=True)
                except Exception as e:                 # an unknown ticker or a foreign filer: note it, go on
                    print("fund refresh", s_, e)
                    bad.append(f"{s_}: {str(e)[:120]}")
                done.append(s_)
            set_status(sub, name, status="error" if bad and len(bad) == len(syms) else "done",
                       msg=f"Fundamentals updated for {len(syms) - len(bad)} of {len(syms)} ticker(s)." + (" Skipped: " + "; ".join(bad[:6]) if bad else ""))
        elif job == "fundtoday":                        # releases reacting at a day's open
            import fundamentals
            r = fundamentals.today_list(sub, p["day"], prog, dl)
            flagged = sum(1 for x in r["rows"] if x.get("flag"))
            set_status(sub, name, status="done", msg=f"{p['day']}: {len(r['rows'])} release(s), {flagged} flagged.")
        elif job == "fundprep":                         # score an older study for the backtest
            import backtest
            backtest.prepare(sub, prog)
            set_status(sub, name, status="done", msg="Study ready for the backtest (scores and daily paths).")
        elif job == "fundopt":                          # the earnings strategy optimizer
            import backtest
            if not backtest.opt_job(sub, prog, dl):
                _invoke(event)
                return {"continued": True}
            set_status(sub, name, status="done", msg="Optimizer: every setting of every leg tested (train / test).")
        elif job == "fundexport":                       # the study's downloads (review file + CSV)
            import fundamentals
            prog("Writing the export files…")
            fundamentals.build_exports(sub)
            set_status(sub, name, status="done", msg="Export files ready.")
        elif job == "fundstudy":                        # the earnings event study
            import fundamentals
            try:
                r = fundamentals.run_study(sub, p, prog, dl)
            except TimeoutError:                        # fundamentals and prices are cached as they come: go on in a new run
                if int(p.get("round") or 0) >= 40:
                    raise RuntimeError("The study ran out of time 40 times: fewer tickers or years.")
                _invoke({**event, "params": {**p, "round": int(p.get("round") or 0) + 1}})
                return {"continued": True}
            set_status(sub, name, status="done", msg=f"Event study: {r['n']} releases, {r['tickers']} tickers vs {r['bench']}.")
        elif job == "study":
            strategy.study(sub, p["id"], prog)
            set_status(sub, name, status="done", msg="Study finished.")
        elif job == "search":
            import search
            return search.job(sub, p["id"], event, _invoke)
        elif job == "optimize":
            if not strategy.optimize(sub, p["id"], prog, dl):
                _invoke(event)                         # out of time: the next settings in a new run
                return {"continued": True}
            set_status(sub, name, status="done", msg="Optimization finished.")
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
    """The latest candles, the footprint after the close, then the paper strategies; the outcome is recorded (JOB#refresh)
    so the pages can show when the candles were last updated and any error."""
    try:
        out = _refresh(sub, footprint_today, ctx)
        rng = {}
        try:
            rng = databento.dataset_range(databento.key(sub), "ohlcv-1m")
            databento.note_range(sub, rng)
        except Exception as e:
            print("range check failed", e)
        set_status(sub, "refresh", status="done" if out == "ok" else "skipped", msg=out,
                   last={s: futdata.last_bar_time(s) for s in futdata.SYMS}, avail=rng.get("end"), schemas=rng.get("schemas"))
        return out
    except Exception as e:
        set_status(sub, "refresh", status="error", msg=str(e)[:300], last={s: futdata.last_bar_time(s) for s in futdata.SYMS})
        raise


def _refresh(sub, footprint_today=True, ctx=None):
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


def market_open(t=None):
    """CME Globex for NQ / ES: Sunday 18:00 to Friday 17:00 ET, closed 17:00-18:00 each day."""
    t = t or now_ny()
    wd, hm = t.weekday(), t.strftime("%H:%M")
    if wd == 5 or (wd == 6 and hm < "18:00") or (wd == 4 and hm >= "17:00"):
        return False
    return not ("17:00" <= hm < "18:00")


def scheduled(ctx=None, live=False):
    if live and not market_open():
        return {"skipped": "market closed"}
    out = {}
    for sub in db.owners():
        try:
            out[sub] = refresh(sub, ctx=ctx)
        except Exception as e:
            print("refresh failed", sub, e)
            out[sub] = str(e)[:200]
        if live:                                       # market maker snapshots (Unusual Whales) during the stock session
            try:
                import uwhales
                uwhales.scheduled_snapshots(sub)
            except Exception as e:
                print("mm snapshot failed", sub, e)
    return out
