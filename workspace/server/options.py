"""Option chains for the workspace's Options panel.

Alpaca (the key saved on the journal's Data page):
  contracts      https://paper-api.alpaca.markets/v2/options/contracts  (expirations, strikes, open interest)
  quotes/greeks  https://data.alpaca.markets/v1beta1/options/snapshots/{underlying}?feed=indicative
Greeks / IV missing from a snapshot are filled with Black-Scholes from the mid quote. Without a key (simulated data) the
chain is priced with Black-Scholes on the simulated underlying, with a volatility smile.
"""
import math
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
R = 0.04                      # risk-free rate for the Black-Scholes fills
DATA = "https://data.alpaca.markets/v1beta1/"


# ------------------------------------------------------------------------------------------- Black-Scholes
def _n(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _pdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def bs(S, K, T, v, call, r=R):
    """price, delta, gamma, theta (per day), vega (per 1 vol point)"""
    if T <= 0 or v <= 0 or S <= 0 or K <= 0:
        intr = max(0.0, (S - K) if call else (K - S))
        return intr, (1.0 if call and S > K else -1.0 if not call and S < K else 0.0), 0.0, 0.0, 0.0
    sq = v * math.sqrt(T)
    d1 = (math.log(S / K) + (r + v * v / 2) * T) / sq
    d2 = d1 - sq
    if call:
        price, delta = S * _n(d1) - K * math.exp(-r * T) * _n(d2), _n(d1)
        theta = -S * _pdf(d1) * v / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * _n(d2)
    else:
        price, delta = K * math.exp(-r * T) * _n(-d2) - S * _n(-d1), _n(d1) - 1
        theta = -S * _pdf(d1) * v / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * _n(-d2)
    return price, delta, _pdf(d1) / (S * sq), theta / 365, S * _pdf(d1) * math.sqrt(T) / 100


def implied_vol(price, S, K, T, call):
    intr = max(0.0, (S - K) if call else (K - S))
    if price <= intr + 1e-6 or T <= 0:
        return None
    lo, hi = 0.005, 5.0
    if bs(S, K, T, hi, call)[0] < price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs(S, K, T, mid, call)[0] > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def years_to(exp, now=None):
    """Time to 16:00 New York on the expiration day, in years (at least 5 minutes)."""
    now = now or datetime.now(NY)
    end = datetime(int(exp[:4]), int(exp[5:7]), int(exp[8:10]), 16, tzinfo=NY)
    return max(300.0, (end - now).total_seconds()) / (365 * 86400)


def occ(und, exp, call, k):
    return f"{und}{exp[2:4]}{exp[5:7]}{exp[8:10]}{'C' if call else 'P'}{int(round(k * 1000)):08d}"


def parse_occ(sym):
    k = float(sym[-8:]) / 1000
    call = sym[-9] == "C"
    d = sym[-15:-9]
    return sym[:-15], f"20{d[:2]}-{d[2:4]}-{d[4:6]}", call, k


# ------------------------------------------------------------------------------------------- chains
class Options:
    def __init__(self, alpaca=None, last=None):
        self.alp, self.last = alpaca, last          # alpaca_paper.AlpacaPaper (trading + data) · fn(sym) -> price
        self.cache, self.lock = {}, threading.Lock()

    def live(self):
        return bool(self.alp and self.alp.configured())

    def _cached(self, key, ttl, fn):
        with self.lock:
            hit = self.cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        v = fn()
        with self.lock:
            self.cache[key] = (time.time(), v)
            if len(self.cache) > 200:
                self.cache.pop(min(self.cache, key=lambda k: self.cache[k][0]))
        return v

    # ---- contracts (expirations, strikes, open interest)
    def contracts(self, und):
        def get():
            out, tok, today = [], None, datetime.now(NY).date().isoformat()
            for _ in range(20):
                q = {"underlying_symbols": und, "status": "active", "expiration_date_gte": today, "limit": 10000, "page_token": tok}
                r = self.alp.req("GET", "options/contracts?" + urllib.parse.urlencode({k: v for k, v in q.items() if v})) or {}
                for c in r.get("option_contracts") or []:
                    out.append({"symbol": c["symbol"], "exp": c["expiration_date"], "k": float(c["strike_price"]), "call": c["type"] == "call",
                                "oi": int(float(c.get("open_interest") or 0)), "close": float(c.get("close_price") or 0) or None})
                tok = r.get("next_page_token")
                if not tok:
                    break
            return out
        return self._cached(("contracts", und), 1800, get)

    def expirations(self, und):
        now = datetime.now(NY)
        today = now.date().isoformat()
        xs = fake_expirations(und) if not self.live() else sorted({c["exp"] for c in self.contracts(und)})
        return [e for e in xs if e > today or (e == today and now.hour < 16)]       # today's expire at 16:00

    def chain(self, und, exp):
        spot = self.last(und) if self.last else None
        if not spot:
            raise ValueError(f"No price for {und}")
        if not self.live():
            return self._fake_chain(und, exp, spot)
        return self._cached(("chain", und, exp), 8, lambda: self._live_chain(und, exp, spot))

    def _live_chain(self, und, exp, spot):
        cs = {c["symbol"]: c for c in self.contracts(und) if c["exp"] == exp}
        snaps, tok = {}, None
        for _ in range(10):
            q = {"feed": "indicative", "expiration_date": exp, "limit": 1000, "page_token": tok}
            r = self._data(f"options/snapshots/{und}?" + urllib.parse.urlencode({k: v for k, v in q.items() if v})) or {}
            snaps.update(r.get("snapshots") or {})
            tok = r.get("next_page_token")
            if not tok:
                break
        T = years_to(exp)
        rows = {}
        for sym in set(cs) | set(snaps):
            try:
                _, e, call, k = parse_occ(sym)
            except (ValueError, IndexError):
                continue
            if e != exp:
                continue
            x, c = snaps.get(sym) or {}, cs.get(sym) or {}
            qt, tr, g = x.get("latestQuote") or {}, x.get("latestTrade") or {}, x.get("greeks") or {}
            bid, ask = float(qt.get("bp") or 0), float(qt.get("ap") or 0)
            leg = {"symbol": sym, "bid": bid, "ask": ask, "last": float(tr.get("p") or 0) or c.get("close"), "oi": c.get("oi"),
                   "vol": int((x.get("dailyBar") or {}).get("v") or 0), "iv": x.get("impliedVolatility"),
                   "delta": g.get("delta"), "gamma": g.get("gamma"), "theta": g.get("theta"), "vega": g.get("vega")}
            _fill(leg, spot, k, T, call)
            rows.setdefault(k, {"strike": k})["call" if call else "put"] = leg
        return {"symbol": und, "exp": exp, "spot": spot, "T": T, "source": "alpaca", "rows": [rows[k] for k in sorted(rows)]}

    def _data(self, path):
        return self.alp.req("GET", path, base=DATA)       # the data host, same key

    def _fake_chain(self, und, exp, spot):
        T = years_to(exp)
        step = strike_step(spot)
        atm = round(spot / step) * step
        rows = []
        for i in range(-30, 31):
            k = round(atm + i * step, 2)
            if k <= 0:
                continue
            row = {"strike": k}
            for call in (True, False):
                v = 0.18 + 0.35 * (math.log(k / spot)) ** 2 * 10 + (0.04 if k < spot else 0)       # a smile with put skew
                p, d, g, th, ve = bs(spot, k, T, v, call)
                sp = max(0.01, round(p * 0.02, 2))
                leg = {"symbol": occ(und, exp, call, k), "bid": max(0.0, round(p - sp, 2)), "ask": round(p + sp, 2), "last": round(p, 2), "oi": int(5000 * math.exp(-abs(i) / 8)),
                       "vol": int(2000 * math.exp(-abs(i) / 5)), "iv": v, "delta": d, "gamma": g, "theta": th, "vega": ve, "model": True}
                row["call" if call else "put"] = leg
            rows.append(row)
        return {"symbol": und, "exp": exp, "spot": spot, "T": T, "source": "simulated", "rows": rows}


def _fill(leg, spot, k, T, call):
    mid = (leg["bid"] + leg["ask"]) / 2 if leg["bid"] > 0 and leg["ask"] > 0 else (leg["last"] or 0)
    if leg.get("iv") is None and mid > 0:
        leg["iv"] = implied_vol(mid, spot, k, T, call)
        leg["model"] = True
    if leg.get("delta") is None and leg.get("iv"):
        _, leg["delta"], leg["gamma"], leg["theta"], leg["vega"] = bs(spot, k, T, leg["iv"], call)
        leg["model"] = True
    for f in ("iv", "delta", "gamma", "theta", "vega"):
        if leg.get(f) is not None:
            leg[f] = float(leg[f])


def strike_step(spot):
    return 0.5 if spot < 25 else 1.0 if spot < 200 else 1.0 if spot < 1000 else 5.0


def fake_expirations(und, today=None):
    d = today or datetime.now(NY).date()
    out, daily = [], und in ("SPY", "QQQ", "IWM")
    x = d
    while len(out) < 14 and (x - d).days < 400:
        if x.weekday() < 5 and (daily and (x - d).days <= 10 or x.weekday() == 4):
            out.append(x.isoformat())
        x += timedelta(days=1)
    for m in range(3, 13, 3):                      # quarterlies further out (third Friday)
        y, mo = d.year + (d.month - 1 + m) // 12, (d.month - 1 + m) % 12 + 1
        f = date(y, mo, 15)
        f += timedelta(days=(4 - f.weekday()) % 7)
        if f.isoformat() not in out:
            out.append(f.isoformat())
    return sorted(out)
