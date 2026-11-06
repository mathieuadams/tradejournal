"""Confluence: every level from the tools on one ladder, and where they stack.

Sources (for the day / now): the walls and Peak Zone (walls.py), yesterday's high / low / close and the overnight
high / low, the vectors and their current cycle's AMW peak and 80 / 50 / 20 % probability bands on 4 timeframes
(daily 200 sessions, hourly 50, 15-minute 10, 1-minute 3: cycles.py), and the extremes of the recent hourly cycles
("imprints": where swings turned). Levels within `tol` points of each other form a cluster; its score = the number of
different sources in it (an "A+" zone has 3+)."""
import cycles
import futdata
import walls

FRAMES = (("1D", 200, "Daily"), ("60", 50, "Hourly"), ("15", 10, "15-min"), ("1", 3, "1-min"))
TOL = {"NQ": 15.0, "ES": 2.0}


def build(sym, q=None):
    q = q or {}
    try:
        tol = max(0.25, min(500.0, float(q.get("tol")))) if q.get("tol") not in (None, "") else TOL[sym]
    except ValueError:
        tol = TOL[sym]
    last = futdata.last_bar_time(sym)
    day = futdata.session_of(last) if last else None
    if not day:
        return None
    L = []
    add = lambda price, src, label, kind: L.append({"price": round(price, 2), "src": src, "label": label, "kind": kind}) if price else None
    w = walls.levels(sym, day, {})
    if w:
        for x in w["levels"]:
            add(x["price"], "Walls", f"{x['label']} {'up' if x['side'] == 'up' else 'down'}", x["k"])
    ch = futdata.chart(sym, 5, 1)
    lv = (ch.get("levels") or {}).get(day) or {}
    for k, lab in (("pdh", "Yesterday's high"), ("pdl", "Yesterday's low"), ("pdc", "Yesterday's close"), ("onh", "Overnight high"), ("onl", "Overnight low")):
        add(lv.get(k), "Session", lab, k)
    price = ch["bars"][-1][4] if ch.get("bars") else None
    for tf, days, name in FRAMES:
        try:
            c = cycles.build(sym, {"tf": tf, "days": days, "show": 400})
        except Exception as e:
            print("confluence cycles failed", tf, e)
            c = None
        if not c:
            continue
        add(c["vector"][-1], name, f"{name} vector", "vector")
        cur = c.get("current")
        if cur:
            add(cur["amw"]["peak"], name, f"{name} AMW peak", "amw")
            for b in cur["bands"]:
                if b["p"] in (80, 50, 20):
                    add(b["price"], name, f"{name} {b['p']}% band", "band")
        if tf == "60":
            for m in c["marks"][-12:]:
                add(m["p"], "Imprints", f"Hourly {m['text']} ({m['t'][5:16].replace('T', ' ')})", "imprint")
    L.sort(key=lambda x: x["price"])
    clusters, cur = [], []
    for x in L:
        if cur and x["price"] - cur[0]["price"] > tol:
            clusters.append(cur)
            cur = []
        cur.append(x)
    if cur:
        clusters.append(cur)
    out = []
    for c in clusters:
        srcs = sorted(set(x["src"] for x in c))
        lo, hi = c[0]["price"], c[-1]["price"]
        out.append({"lo": lo, "hi": hi, "mid": round((lo + hi) / 2, 2), "score": len(srcs), "sources": srcs, "levels": c,
                    "dist": round((lo + hi) / 2 - price, 2) if price else None})
    return {"sym": sym, "day": day, "price": price, "t": last, "tol": tol, "levels": L, "clusters": out}
