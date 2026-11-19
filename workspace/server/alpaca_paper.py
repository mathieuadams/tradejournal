"""The user's Alpaca PAPER account (the key saved on the journal's Data page): https://paper-api.alpaca.markets only.

Frozen strategies with account "alpaca" are mirrored here: the Alpaca position in the symbol is brought to the strategy's
position (market order in regular hours, marketable limit with extended_hours outside them), then its stop / target are
placed as a stop, limit or OCO order so they trigger between bar closes. Orders carry client_order_id "fz-<id>-...".
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

PAPER = "https://paper-api.alpaca.markets/v2/"
NY = ZoneInfo("America/New_York")


class AlpacaError(Exception):
    pass


class AlpacaPaper:
    def __init__(self, key, secret, base=PAPER, opener=None):
        self.key, self.secret, self.base = key, secret, base
        self.opener = opener or urllib.request.urlopen

    def configured(self):
        return bool(self.key and self.secret)

    def req(self, method, path, body=None, timeout=20):
        if not self.configured():
            raise AlpacaError("No Alpaca key: save one on the journal's Data page (the workspace loads it at its next restart).")
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret, "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with self.opener(r, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 404 and method == "GET":
                return None
            try:
                msg = json.loads(e.read()).get("message")
            except Exception:
                msg = None
            if e.code in (401, 403):
                msg = "Alpaca refused the key for paper trading (is it a paper-account key?)" + (f": {msg}" if msg else "")
            raise AlpacaError(msg or f"Alpaca HTTP {e.code}")
        except urllib.error.URLError as e:
            raise AlpacaError(f"Alpaca unreachable: {e.reason}")

    # ------------------------------------------------------------------ reads
    def account(self):
        a = self.req("GET", "account") or {}
        return {"number": a.get("account_number"), "status": a.get("status"), "equity": float(a.get("equity") or 0), "cash": float(a.get("cash") or 0),
                "buying_power": float(a.get("buying_power") or 0), "last_equity": float(a.get("last_equity") or 0)}

    def positions(self):
        return [{"symbol": p["symbol"], "qty": float(p["qty"]), "avg": float(p["avg_entry_price"]), "price": float(p.get("current_price") or 0),
                 "upl": float(p.get("unrealized_pl") or 0)} for p in self.req("GET", "positions") or []]

    def position(self, sym):
        p = self.req("GET", f"positions/{sym}")
        return float(p["qty"]) if p else 0.0

    def orders(self, status="open", limit=100):
        return self.req("GET", f"orders?status={status}&limit={limit}&nested=true&direction=desc") or []

    def tagged(self, tag, sym=None):
        return [o for o in self.orders() if (o.get("client_order_id") or "").startswith(tag) and (not sym or o.get("symbol") == sym)]

    # ------------------------------------------------------------------ writes
    def cancel_tagged(self, tag, sym=None):
        n = 0
        for o in self.tagged(tag, sym):
            try:
                self.req("DELETE", f"orders/{o['id']}")
                n += 1
            except AlpacaError:
                pass
        if n:
            time.sleep(0.6)                     # (cancels are asynchronous; the shares they held free up)
        return n

    @staticmethod
    def rth(now=None):
        d = datetime.fromtimestamp(now or time.time(), NY)
        m = d.hour * 60 + d.minute
        return d.weekday() < 5 and 570 <= m < 960

    def order(self, sym, qty, tag, last=None, now=None):
        """Buy (qty > 0) / sell (qty < 0) now. Regular hours: market. Otherwise a limit 0.2% through the last price, extended hours."""
        side, q = ("buy" if qty > 0 else "sell"), abs(qty)
        body = {"symbol": sym, "qty": _q(q), "side": side, "client_order_id": f"{tag}-{int(time.time() * 1000) % 10**10}"}
        if self.rth(now) or not last:
            body.update(type="market", time_in_force="day")
        else:
            body.update(type="limit", time_in_force="day", extended_hours=True, limit_price=_px(last * (1.002 if qty > 0 else 0.998)))
        return self.req("POST", "orders", body)

    def protect(self, sym, qty, stop, limit, tag):
        """The position's exits: OCO (target + stop), or a single stop / limit, good till canceled."""
        side, q = ("sell" if qty > 0 else "buy"), _q(abs(qty))
        cid = f"{tag}-x{int(time.time() * 1000) % 10**10}"
        if stop and limit:
            body = {"symbol": sym, "qty": q, "side": side, "type": "limit", "time_in_force": "gtc", "order_class": "oco", "client_order_id": cid,
                    "take_profit": {"limit_price": _px(limit)}, "stop_loss": {"stop_price": _px(stop)}}
        elif stop:
            body = {"symbol": sym, "qty": q, "side": side, "type": "stop", "stop_price": _px(stop), "time_in_force": "gtc", "client_order_id": cid}
        elif limit:
            body = {"symbol": sym, "qty": q, "side": side, "type": "limit", "limit_price": _px(limit), "time_in_force": "gtc", "client_order_id": cid}
        else:
            return None
        return self.req("POST", "orders", body)

    def wait_fill(self, oid, secs=12):
        end = time.time() + secs
        while time.time() < end:
            o = self.req("GET", f"orders/{oid}") or {}
            if o.get("status") in ("filled", "canceled", "rejected", "expired"):
                return o
            time.sleep(1)
        return None


def _q(q):
    return str(int(q)) if float(q).is_integer() else f"{q:.6f}".rstrip("0")


def _px(p):
    return f"{p:.2f}" if p >= 1 else f"{p:.4f}"


def exit_levels(exits, d):
    """The stop and target of the open position from the strategy's working exits (nearest of each)."""
    stops = [x["stop"] for x in exits if x.get("dir") == d and x.get("stop")]
    lims = [x["limit"] for x in exits if x.get("dir") == d and x.get("limit")]
    stop = (max(stops) if d > 0 else min(stops)) if stops else None
    lim = (min(lims) if d > 0 else max(lims)) if lims else None
    return stop, lim


def sync(ap, f, opn, exits, last=None, now=None):
    """Bring the Alpaca paper position in f's symbol to the strategy's, then make sure its exits are working.
    Returns a short log line (or None when nothing had to change)."""
    sym, tag = f["symbol"], "fz-" + f["id"]
    want = sum(o["qty"] * o["dir"] for o in opn)
    have = ap.position(sym)
    log = []
    if abs(want - have) > 1e-9:
        ap.cancel_tagged(tag, sym)
        o = ap.order(sym, want - have, tag, last, now)
        log.append(f"{'buy' if want > have else 'sell'} {_q(abs(want - have))} {sym} ({o.get('type')})")
        done = ap.wait_fill(o["id"]) if o.get("type") == "market" else None
        if not done or done.get("status") != "filled":
            return "; ".join(log + ["exits after the fill"])
        have = want
    if abs(want) > 1e-9:
        d = 1 if want > 0 else -1
        stop, lim = exit_levels(exits, d)
        if not (stop or lim):
            return "; ".join(log) or None
        cur = ap.tagged(tag, sym)
        ok = any(_matches(o, stop, lim) for o in cur)
        if not ok:
            if cur:
                ap.cancel_tagged(tag, sym)
            ap.protect(sym, want, stop, lim, tag)
            log.append(f"exits: stop {stop and _px(stop)} target {lim and _px(lim)}")
    else:
        if ap.tagged(tag, sym):
            ap.cancel_tagged(tag, sym)
            log.append("flat: exits canceled")
    return "; ".join(log) or None


def _matches(o, stop, lim):
    """Is this working order the exit we want (same stop / target, to the cent)?"""
    def eq(a, b):
        return (a is None and b is None) or (a is not None and b is not None and abs(float(a) - float(b)) < 0.006)
    legs = [o] + (o.get("legs") or [])
    st = next((x.get("stop_price") for x in legs if x.get("stop_price")), None)
    li = next((x.get("limit_price") for x in legs if x.get("type") == "limit" and x.get("limit_price")), None)
    return eq(st, stop) and eq(li, lim)
