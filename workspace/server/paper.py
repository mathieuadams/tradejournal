"""Simulated broker: market / limit / stop orders, bracket stop-loss and take-profit (one cancels the other),
long and short positions, cash accounting, realized / open P&L, round-trip trades for the journal, a kill switch.
Fills happen on live prices from the market feed. State is saved as JSON."""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

START = 100_000.0


def _now():
    return time.time()


def _day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class PaperError(ValueError):
    pass


class Paper:
    def __init__(self, path, price_fn, on_change=None):
        self.path, self.price_fn, self.on_change = path, price_fn, on_change
        self.lock = threading.RLock()
        self.s = self._load()

    # ------------------------------------------------------------------ persistence
    def _fresh(self, balance=START):
        return {"cash": balance, "start": balance, "positions": {}, "orders": [], "fills": [], "trades": [],
                "settings": {"ask_before": True, "kill_switch": False, "max_position_pct": 100.0}, "day": {"date": _day(), "equity": balance}, "marks": {}}

    def _load(self):
        try:
            with open(self.path) as f:
                s = json.load(f)
            base = self._fresh()
            base.update(s)
            return base
        except Exception:
            return self._fresh()

    def _save(self):
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(self.s, f)
        os.replace(tmp, self.path)

    def _changed(self, event=None):
        self._save()
        if self.on_change:
            self.on_change(event)

    # ------------------------------------------------------------------ prices / equity
    def _px(self, sym):
        p = self.price_fn(sym)
        if p is None:
            p = self.s["marks"].get(sym)
        return p

    def snapshot(self):
        with self.lock:
            s = self.s
            pos, open_pnl, gross = [], 0.0, 0.0
            for sym, p in s["positions"].items():
                last = self._px(sym) or p["avg"]
                pnl = (last - p["avg"]) * p["qty"]
                open_pnl += pnl
                gross += abs(p["qty"] * last)
                pos.append({"symbol": sym, "qty": p["qty"], "avg": p["avg"], "last": last, "pnl": pnl, "pnl_pct": (last / p["avg"] - 1) * 100 * (1 if p["qty"] > 0 else -1) if p["avg"] else 0,
                            "value": p["qty"] * last, "opened": p.get("opened")})
            equity = s["cash"] + sum(p["qty"] * (self._px(sym) or p["avg"]) for sym, p in s["positions"].items())
            if s["day"]["date"] != _day():
                s["day"] = {"date": _day(), "equity": equity}
            return {"cash": s["cash"], "equity": equity, "balance": s["cash"] + sum(p["qty"] * p["avg"] for p in s["positions"].values()),
                    "open_pnl": open_pnl, "today_pnl": equity - s["day"]["equity"], "margin_used": gross, "buying_power": max(0.0, equity - gross),
                    "start": s["start"], "positions": pos, "orders": [o for o in s["orders"] if o["status"] == "working"],
                    "fills": s["fills"][-200:][::-1], "trades": s["trades"][-500:][::-1], "settings": s["settings"]}

    # ------------------------------------------------------------------ orders
    def place(self, req):
        with self.lock:
            s = self.s
            if s["settings"].get("kill_switch"):
                raise PaperError("Kill switch is on: new orders are blocked.")
            sym = str(req.get("symbol") or "").upper().strip()
            side = req.get("side")
            typ = req.get("type", "market")
            try:
                qty = float(req.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0
            if not sym:
                raise PaperError("Symbol is required.")
            if side not in ("buy", "sell"):
                raise PaperError("Side must be buy or sell.")
            if typ not in ("market", "limit", "stop"):
                raise PaperError("Type must be market, limit or stop.")
            if not qty > 0:
                raise PaperError("Quantity must be more than 0.")
            last = self._px(sym)
            if last is None:
                raise PaperError(f"No price yet for {sym}. Open its chart first.")
            price = req.get("price")
            if typ != "market":
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    raise PaperError(f"A {typ} order needs a price.")
                if price <= 0:
                    raise PaperError("Price must be positive.")
            sl, tp = self._num(req.get("sl")), self._num(req.get("tp"))
            ref = price if typ != "market" else last
            if sl is not None and ((side == "buy" and sl >= ref) or (side == "sell" and sl <= ref)):
                raise PaperError("Stop loss must be below the entry for a buy and above it for a sell.")
            if tp is not None and ((side == "buy" and tp <= ref) or (side == "sell" and tp >= ref)):
                raise PaperError("Take profit must be above the entry for a buy and below it for a sell.")
            self._check_margin(sym, side, qty, ref)
            o = {"id": uuid.uuid4().hex[:10], "symbol": sym, "side": side, "type": typ, "qty": qty, "price": price if typ != "market" else None,
                 "sl": sl, "tp": tp, "status": "working", "created": _now(), "source": req.get("source", "manual"), "parent": None, "oco": None, "reduce_only": False,
                 "note": str(req.get("note") or "")[:300]}
            s["orders"].append(o)
            if typ == "market":
                self._fill(o, last)
            else:
                self._try(o, last, last, last)
            self._changed({"type": "order", "order": o})
            return o

    @staticmethod
    def _num(v):
        if v in (None, ""):
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise PaperError(f"Not a number: {v}")
        return v if v > 0 else None

    def _check_margin(self, sym, side, qty, price):
        s = self.s
        cur = s["positions"].get(sym, {}).get("qty", 0)
        new = cur + (qty if side == "buy" else -qty)
        if abs(new) <= abs(cur):
            return
        snap_eq = s["cash"] + sum(p["qty"] * (self._px(x) or p["avg"]) for x, p in s["positions"].items())
        gross = sum(abs(p["qty"] * (self._px(x) or p["avg"])) for x, p in s["positions"].items() if x != sym)
        need = gross + abs(new) * price
        lim = snap_eq * s["settings"].get("max_position_pct", 100) / 100
        if need > lim + 1e-6:
            raise PaperError(f"Not enough buying power: this needs ${need:,.0f} of exposure, the account allows ${lim:,.0f}.")

    def cancel(self, oid):
        with self.lock:
            o = next((x for x in self.s["orders"] if x["id"] == oid and x["status"] == "working"), None)
            if not o:
                raise PaperError("Order not found or no longer working.")
            o["status"] = "canceled"
            self._changed({"type": "cancel", "order": o})
            return o

    def modify(self, oid, price=None, sl=None, tp=None):
        with self.lock:
            o = next((x for x in self.s["orders"] if x["id"] == oid and x["status"] == "working"), None)
            if not o:
                raise PaperError("Order not found or no longer working.")
            if price is not None and o["type"] != "market":
                o["price"] = float(price)
            if sl is not None:
                o["sl"] = self._num(sl)
            if tp is not None:
                o["tp"] = self._num(tp)
            last = self._px(o["symbol"])
            if last is not None:
                self._try(o, last, last, last)
            self._changed({"type": "modify", "order": o})
            return o

    def close(self, sym, source="manual"):
        with self.lock:
            p = self.s["positions"].get(sym)
            if not p:
                raise PaperError(f"No open position in {sym}.")
            for o in self.s["orders"]:
                if o["symbol"] == sym and o["status"] == "working" and o["reduce_only"]:
                    o["status"] = "canceled"
            last = self._px(sym)
            o = {"id": uuid.uuid4().hex[:10], "symbol": sym, "side": "sell" if p["qty"] > 0 else "buy", "type": "market", "qty": abs(p["qty"]), "price": None,
                 "sl": None, "tp": None, "status": "working", "created": _now(), "source": source, "parent": None, "oco": None, "reduce_only": True, "note": "close"}
            self.s["orders"].append(o)
            self._fill(o, last)
            self._changed({"type": "close", "symbol": sym})
            return o

    def flatten(self):
        with self.lock:
            for o in self.s["orders"]:
                if o["status"] == "working":
                    o["status"] = "canceled"
            for sym in list(self.s["positions"]):
                self.close(sym, source="flatten")
            self._changed({"type": "flatten"})

    def reset(self, balance=START):
        with self.lock:
            settings = self.s["settings"]
            self.s = self._fresh(float(balance or START))
            self.s["settings"] = settings
            self._changed({"type": "reset"})

    def settings(self, **kw):
        with self.lock:
            st = self.s["settings"]
            for k in ("ask_before", "kill_switch"):
                if k in kw and kw[k] is not None:
                    st[k] = bool(kw[k])
            if kw.get("max_position_pct"):
                st["max_position_pct"] = max(1.0, min(400.0, float(kw["max_position_pct"])))
            if kw.get("kill_switch"):
                for o in self.s["orders"]:
                    if o["status"] == "working" and not o["reduce_only"]:
                        o["status"] = "canceled"
            self._changed({"type": "settings"})
            return st

    # ------------------------------------------------------------------ fills
    def on_price(self, sym, last, high=None, low=None):
        with self.lock:
            self.s["marks"][sym] = last
            hit = False
            for o in list(self.s["orders"]):
                if o["symbol"] == sym and o["status"] == "working":
                    hit |= self._try(o, last, high if high is not None else last, low if low is not None else last)
            if hit:
                self._changed({"type": "fill", "symbol": sym})

    def _try(self, o, last, high, low):
        if o["status"] != "working":
            return False
        p = o["price"]
        if o["type"] == "limit":
            if o["side"] == "buy" and last <= p:
                return self._fill(o, min(p, last)) or True
            if o["side"] == "sell" and last >= p:
                return self._fill(o, max(p, last)) or True
        elif o["type"] == "stop":
            if o["side"] == "buy" and last >= p:
                return self._fill(o, max(p, last)) or True
            if o["side"] == "sell" and last <= p:
                return self._fill(o, min(p, last)) or True
        return False

    def _fill(self, o, price):
        s = self.s
        sym = o["symbol"]
        pos = s["positions"].get(sym)
        cur = pos["qty"] if pos else 0.0
        qty = o["qty"]
        if o["reduce_only"]:
            if cur == 0 or (cur > 0) == (o["side"] == "buy"):
                o["status"] = "canceled"
                return
            qty = min(qty, abs(cur))
        signed = qty if o["side"] == "buy" else -qty
        realized = 0.0
        t = _now()
        if cur == 0 or (cur > 0) == (signed > 0):
            avg = ((pos["avg"] * cur if pos else 0) + price * signed) / (cur + signed)
            if not pos:
                pos = {"qty": 0.0, "avg": price, "opened": t, "realized": 0.0, "entries": []}
                s["positions"][sym] = pos
            pos["qty"], pos["avg"] = cur + signed, avg
            pos.setdefault("entries", []).append({"price": price, "qty": signed, "t": t})
        else:
            closing = min(abs(signed), abs(cur))
            realized = (price - pos["avg"]) * closing * (1 if cur > 0 else -1)
            pos["realized"] = pos.get("realized", 0) + realized
            rest = cur + signed
            pos.setdefault("exits", []).append({"price": price, "qty": closing, "t": t})
            if abs(rest) < 1e-9 or (rest > 0) != (cur > 0):
                ex = pos.get("exits", [])
                s["trades"].append({"id": uuid.uuid4().hex[:10], "symbol": sym, "side": "long" if cur > 0 else "short", "qty": abs(cur), "entry": pos["avg"],
                                    "exit": sum(e["price"] * e["qty"] for e in ex) / max(1e-9, sum(e["qty"] for e in ex)), "pnl": pos["realized"],
                                    "pnl_pct": (pos["realized"] / (abs(cur) * pos["avg"]) * 100) if pos["avg"] else 0, "opened": pos.get("opened"), "closed": t, "source": o.get("source")})
                del s["positions"][sym]
                for x in s["orders"]:
                    if x["symbol"] == sym and x["status"] == "working" and x["reduce_only"]:
                        x["status"] = "canceled"
                if abs(rest) > 1e-9:
                    s["positions"][sym] = {"qty": rest, "avg": price, "opened": t, "realized": 0.0, "entries": [{"price": price, "qty": rest, "t": t}]}
            else:
                pos["qty"] = rest
        s["cash"] -= signed * price
        o["status"], o["filled_at"], o["fill_price"], o["filled_qty"] = "filled", t, price, qty
        s["fills"].append({"id": uuid.uuid4().hex[:10], "order": o["id"], "symbol": sym, "side": o["side"], "qty": qty, "price": price, "t": t, "realized": realized, "source": o.get("source")})
        if o.get("oco"):
            for x in s["orders"]:
                if x.get("oco") == o["oco"] and x["id"] != o["id"] and x["status"] == "working":
                    x["status"] = "canceled"
        if (o.get("sl") or o.get("tp")) and not o["reduce_only"]:
            group = uuid.uuid4().hex[:8]
            back = "sell" if o["side"] == "buy" else "buy"
            for kind, typ in (("sl", "stop"), ("tp", "limit")):
                if o.get(kind):
                    s["orders"].append({"id": uuid.uuid4().hex[:10], "symbol": sym, "side": back, "type": typ, "qty": qty, "price": o[kind], "sl": None, "tp": None,
                                        "status": "working", "created": t, "source": o.get("source"), "parent": o["id"], "oco": group, "reduce_only": True, "note": kind})
        s["orders"] = [x for x in s["orders"] if x["status"] == "working" or t - x.get("filled_at", x["created"]) < 86400 * 3][-500:]
        s["fills"] = s["fills"][-2000:]
