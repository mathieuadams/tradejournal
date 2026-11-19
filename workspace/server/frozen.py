"""Frozen strategies: a Pine strategy with its inputs locked, paper traded forward from the moment it was frozen.

Each strategy is re-run on its newest closed bars after every bar close (Node runs the same Pine interpreter as the
browser, server/pinerun.mjs). Trades that start after the freeze are its paper record. With account "paper" the
position is also mirrored in the workspace paper account (market orders for the difference)."""
import json
import os
import secrets
import shutil
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "pinerun.mjs")
WARM = 2500                     # bars of history before the newest one (indicators warm up, the overnight range etc.)


class FrozenError(Exception):
    pass


def node_bin():
    return os.environ.get("NODE_BIN") or shutil.which("node") or ""


def run_pine(payload, timeout=90):
    nb = node_bin()
    if not nb:
        return {"error": "Node.js is not installed on the server (it installs on the next restart)."}
    try:
        r = subprocess.run([nb, RUNNER], input=json.dumps(payload).encode(), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "The strategy took too long to run."}
    if r.returncode != 0:
        return {"error": (r.stderr.decode(errors="replace").strip().splitlines() or ["runner failed"])[-1][:300]}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"error": "Bad runner output."}


def metrics(trades, capital=0):
    ts = [t for t in trades if t.get("exitTime")]
    if not ts:
        return {"n": 0, "net": 0}
    nets = [t["pnl"] for t in ts]
    gp, gl = sum(v for v in nets if v > 0), -sum(v for v in nets if v < 0)
    eq = peak = dd = 0.0
    for v in nets:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(ts), "net": round(sum(nets), 2), "winRate": round(sum(1 for v in nets if v > 0) / len(ts) * 100, 1),
            "pf": round(gp / gl, 2) if gl else None, "avg": round(sum(nets) / len(ts), 2), "maxDD": round(dd, 2)}


class Frozen:
    def __init__(self, path, mkt, paper=None, runner=run_pine, interval=20, alpaca=None):
        self.path, self.mkt, self.paper, self.runner, self.alpaca = path, mkt, paper, runner, alpaca
        self.lock = threading.RLock()
        self.s = self._load()
        self.busy = set()
        if interval:
            threading.Thread(target=self._loop, args=(interval,), daemon=True).start()

    # ---------------------------------------------------------------- storage
    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {"items": {}}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.s, f)
        os.replace(tmp, self.path)

    @staticmethod
    def view(f, full=False):
        v = {k: f.get(k) for k in ("id", "name", "symbol", "tf", "inputs", "inputLabels", "strategy", "sec", "account", "status", "frozenAt", "lastRun",
                                     "lastBar", "error", "research", "open", "mirror", "logs", "capital", "broker", "brokerAt")}
        v["metrics"] = metrics(f.get("trades") or [])
        v["openPnl"] = round(sum(o.get("pnl") or 0 for o in f.get("open") or []), 2)
        if full:
            v["src"], v["trades"] = f.get("src"), f.get("trades") or []
        else:
            v["recent"] = (f.get("trades") or [])[-5:]
        return v

    def list(self):
        with self.lock:
            return [self.view(f) for f in sorted(self.s["items"].values(), key=lambda f: -f["frozenAt"])]

    def get(self, fid):
        with self.lock:
            f = self.s["items"].get(fid)
            if not f:
                raise FrozenError("No such frozen strategy.")
            return self.view(f, full=True)

    # ---------------------------------------------------------------- actions
    def create(self, b):
        src = str(b.get("src") or "")
        if "strategy(" not in src:
            raise FrozenError("Only strategies can be frozen (the script needs strategy(...)).")
        sym, tf = str(b.get("symbol") or "").upper(), str(b.get("tf") or "")
        if not sym or not tf:
            raise FrozenError("symbol and tf are required.")
        acct = b.get("account") if b.get("account") in ("ledger", "paper", "alpaca") else "ledger"
        sec = [{"sym": str(x.get("sym", "")).upper(), "tf": str(x.get("tf", "")), "key": f"{str(x.get('sym', '')).upper()}|{x.get('tf', '')}"}
               for x in (b.get("sec") or [])[:4] if x.get("sym") and x.get("tf")]
        fid = "f" + secrets.token_hex(5)
        f = {"id": fid, "name": str(b.get("name") or "Strategy")[:80], "src": src[:200000], "inputs": b.get("inputs") or None,
             "inputLabels": b.get("inputLabels") or None, "symbol": sym, "tf": tf, "strategy": b.get("strategy") or None, "sec": sec, "account": acct,
             "status": "on", "frozenAt": int(time.time()), "trades": [], "open": [], "mirror": 0, "lastRun": None, "lastBar": None, "error": None,
             "research": b.get("research") or None}
        with self.lock:
            self.s["items"][fid] = f
            self._save()
        threading.Thread(target=self.run_one, args=(fid,), daemon=True).start()
        return self.view(f)

    def action(self, fid, act):
        with self.lock:
            f = self.s["items"].get(fid)
            if not f:
                raise FrozenError("No such frozen strategy.")
            if act == "stop":
                f["status"] = "off"
                self._flat(f)
            elif act == "start":
                f["status"], f["error"] = "on", None
            elif act == "delete":
                self._flat(f)
                del self.s["items"][fid]
                self._save()
                return None
            elif act in ("ledger", "paper", "alpaca"):
                if act != f["account"]:
                    self._flat(f)
                f["account"] = act
            else:
                raise FrozenError("Unknown action.")
            self._save()
            out = self.view(f)
        if act in ("start", "alpaca", "paper"):
            threading.Thread(target=self.run_one, args=(fid,), daemon=True).start()
        return out

    def _flat(self, f):
        """Close what this strategy holds in the paper account / the Alpaca paper account."""
        if f.get("account") == "alpaca" and self.alpaca and self.alpaca.configured():
            try:
                import alpaca_paper
                f["broker"] = alpaca_paper.sync(self.alpaca, f, [], []) or "flat"
                f["brokerAt"] = int(time.time())
            except Exception as e:
                f["error"] = f"Alpaca: {e}"
        if f.get("mirror") and self.paper:
            try:
                q = f["mirror"]
                self.paper.place({"symbol": f["symbol"], "side": "sell" if q > 0 else "buy", "qty": abs(q), "type": "market", "source": "frozen", "note": f["name"]})
            except Exception as e:
                print("frozen flatten failed", f["id"], e)
        f["mirror"] = 0

    # ---------------------------------------------------------------- running
    def _loop(self, interval):
        while True:
            time.sleep(interval)
            try:
                self.tick()
            except Exception as e:  # pragma: no cover
                print("frozen tick failed", e)

    def tick(self, now=None):
        now = now or time.time()
        with self.lock:
            due = []
            for f in self.s["items"].values():
                if f["status"] != "on" or f["id"] in self.busy:
                    continue
                sec = _tf_sec(f["tf"])
                last_close = int(now // sec * sec)          # the newest bar boundary that has passed
                if not f.get("lastRun") or (f["lastRun"] < last_close + 15 <= now) or now - f["lastRun"] > 900:
                    due.append(f["id"])
        for fid in due:
            self.run_one(fid, now)

    def _alpaca(self, fid, opn, exits, last, now):
        import alpaca_paper
        with self.lock:
            f = json.loads(json.dumps(self.s["items"].get(fid) or {}))
        if not f:
            return
        try:
            if not (self.alpaca and self.alpaca.configured()):
                raise alpaca_paper.AlpacaError("no Alpaca key loaded on the workspace server")
            msg, err = alpaca_paper.sync(self.alpaca, f, opn, exits, last), None
        except Exception as e:
            msg, err = None, f"Alpaca: {e}"
        with self.lock:
            cur = self.s["items"].get(fid)
            if cur:
                if msg:
                    cur["broker"], cur["brokerAt"] = msg, int(now)
                    cur.setdefault("blog", []).append([int(now), msg])
                    cur["blog"] = cur["blog"][-50:]
                cur["error"] = err
                self._save()

    def run_one(self, fid, now=None):
        with self.lock:
            f = self.s["items"].get(fid)
            if not f or fid in self.busy:
                return
            self.busy.add(fid)
            f = json.loads(json.dumps(f))
        now = now or time.time()
        try:
            sec = _tf_sec(f["tf"])
            bars, _ = self.mkt.bars(f["symbol"], f["tf"], WARM)
            bars = [b for b in bars if b[0] + sec <= now + 1]       # closed bars only
            if not bars:
                raise FrozenError(f"No {f['symbol']} {f['tf']} bars.")
            secs = {}
            for x in f.get("sec") or []:
                xs, _ = self.mkt.bars(x["sym"], x["tf"], int(WARM * max(1, sec / _tf_sec(x["tf"])) * 1.6) + 50)
                secs[x["key"]] = [b for b in xs if b[0] + _tf_sec(x["tf"]) <= now + 1 and b[0] >= bars[0][0] - 86400 * 3]
            r = self.runner({"src": f["src"], "inputs": f.get("inputs"), "symbol": f["symbol"], "tf": f["tf"], "bars": bars, "securities": secs, "strategy": f.get("strategy")})
            if r.get("error"):
                raise FrozenError(r["error"])
            since = f["frozenAt"]
            new_closed = [t for t in r.get("trades") or [] if t["entryTime"] >= since]
            opn = [o for o in r.get("open") or [] if o["entryTime"] >= since]
            with self.lock:
                cur = self.s["items"].get(fid)
                if not cur:
                    return
                have = {(t["entryTime"], t["dir"], t.get("id")) for t in cur["trades"]}
                for t in new_closed:
                    if (t["entryTime"], t["dir"], t.get("id")) not in have:
                        cur["trades"].append(t)
                cur["trades"].sort(key=lambda t: t["entryTime"])
                cur["open"], cur["lastRun"], cur["lastBar"], cur["error"] = opn, int(now), bars[-1][0], None
                cur["logs"] = (r.get("logs") or [])[:5]
                if cur["account"] == "paper" and cur["status"] == "on" and self.paper:
                    want = sum(o["qty"] * o["dir"] for o in opn)
                    diff = want - (cur.get("mirror") or 0)
                    if abs(diff) > 1e-9:
                        try:
                            self.paper.place({"symbol": cur["symbol"], "side": "buy" if diff > 0 else "sell", "qty": abs(diff), "type": "market", "source": "frozen", "note": cur["name"]})
                            cur["mirror"] = want
                        except Exception as e:
                            cur["error"] = f"Paper account: {e}"
                self._save()
                go_alpaca = cur["account"] == "alpaca" and cur["status"] == "on"
            if go_alpaca:
                self._alpaca(fid, opn, r.get("exits") or [], bars[-1][4], now)
        except Exception as e:
            with self.lock:
                cur = self.s["items"].get(fid)
                if cur:
                    cur["error"], cur["lastRun"] = str(e)[:300], int(now)
                    self._save()
        finally:
            with self.lock:
                self.busy.discard(fid)


def _tf_sec(tf):
    import market
    return market.tf_seconds(tf)
