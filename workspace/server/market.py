"""Market data for the workspace: history at any timeframe and live 1-minute bars.

Providers
  FakeMarket    synthetic, deterministic per symbol, ticks 4x a second (DATA=fake, the default without Alpaca keys)
  AlpacaMarket  Alpaca REST history + the Alpaca WebSocket stream for live trades / minute bars (stocks and crypto)

Both expose: search(q), bars(symbol, tf, limit, before) -> (bars, more), quotes(symbols), subscribe(symbols), last(symbol)
and call on_bar(symbol, bar1m) for every live update of the current minute bar. Bars are [t, o, h, l, c, v] with t in
epoch seconds (start of the bar). Timeframes: 1m 5m 22m 1h 4h 1D 2D 1W 1M ... (any multiple).
"""
import bisect
import hashlib
import json
import math
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    NY = timezone(timedelta(hours=-5))

UNIT_SEC = {"s": 1, "m": 60, "h": 3600, "D": 86400, "W": 604800, "M": 2592000}


def parse_tf(tf):
    m = re.fullmatch(r"\s*(\d*)\s*([smhDWM])\s*", str(tf))
    if not m:
        raise ValueError(f"Unknown timeframe '{tf}'. Use e.g. 1m, 5m, 22m, 1h, 4h, 1D, 1W, 1M.")
    n = int(m.group(1) or 1)
    if n <= 0 or n > 1000:
        raise ValueError("Timeframe multiplier out of range")
    return n, m.group(2)


def tf_seconds(tf):
    n, u = parse_tf(tf)
    return n * UNIT_SEC[u]


def ny_date(t):
    return datetime.fromtimestamp(t, NY).date()


def bucket_start(t, n, unit):
    """Start (epoch seconds) of the bar containing t."""
    if unit in ("s", "m", "h"):
        sec = n * UNIT_SEC[unit]
        return t - (t % sec)
    d = datetime.fromtimestamp(t, NY)
    if unit == "D":
        day0 = datetime(d.year, d.month, d.day, tzinfo=NY)
        if n > 1:
            k = (day0.toordinal() - 1) // n * n + 1
            day0 = datetime.fromordinal(k).replace(tzinfo=NY)
        return int(day0.timestamp())
    if unit == "W":
        day0 = datetime(d.year, d.month, d.day, tzinfo=NY) - timedelta(days=d.weekday())
        if n > 1:
            wk = day0.toordinal() // 7
            day0 = datetime.fromordinal((wk - wk % n) * 7 + 1).replace(tzinfo=NY)
            day0 -= timedelta(days=day0.weekday())
        return int(day0.timestamp())
    months = d.year * 12 + d.month - 1
    months -= months % n
    return int(datetime(months // 12, months % 12 + 1, 1, tzinfo=NY).timestamp())


def aggregate(bars, n, unit):
    out = []
    for b in bars:
        k = bucket_start(b[0], n, unit)
        if out and out[-1][0] == k:
            o = out[-1]
            o[2] = max(o[2], b[2]); o[3] = min(o[3], b[3]); o[4] = b[4]; o[5] += b[5]
        else:
            out.append([k, b[1], b[2], b[3], b[4], b[5]])
    return out


def _seed(sym):
    return int(hashlib.sha256(sym.encode()).hexdigest()[:12], 16)


CATALOG = [
    ("AAPL", "Apple Inc.", 352.0), ("MSFT", "Microsoft Corporation", 515.0), ("NVDA", "NVIDIA Corporation", 157.0),
    ("AMZN", "Amazon.com Inc.", 258.0), ("META", "Meta Platforms Inc.", 620.0), ("GOOG", "Alphabet Inc. Class C", 280.0),
    ("TSLA", "Tesla Inc.", 380.0), ("AMD", "Advanced Micro Devices", 508.0), ("PLTR", "Palantir Technologies Inc.", 160.0),
    ("NFLX", "Netflix Inc.", 1150.0), ("AVGO", "Broadcom Inc.", 340.0), ("COIN", "Coinbase Global", 310.0),
    ("SMCI", "Super Micro Computer", 45.0), ("MSTR", "Strategy Inc.", 280.0), ("HOOD", "Robinhood Markets", 130.0),
    ("SPY", "SPDR S&P 500 ETF", 680.0), ("QQQ", "Invesco QQQ Trust", 610.0), ("IWM", "iShares Russell 2000 ETF", 245.0),
    ("DIA", "SPDR Dow Jones ETF", 470.0), ("TLT", "iShares 20+ Year Treasury", 90.0), ("GLD", "SPDR Gold Shares", 375.0),
    ("JPM", "JPMorgan Chase", 310.0), ("BAC", "Bank of America", 52.0), ("XOM", "Exxon Mobil", 115.0), ("UNH", "UnitedHealth", 330.0),
    ("BTC/USD", "Bitcoin", 95000.0), ("ETH/USD", "Ethereum", 3400.0), ("SOL/USD", "Solana", 160.0), ("DOGE/USD", "Dogecoin", 0.18),
]
NAMES = {s: n for s, n, _ in CATALOG}


def is_crypto(sym):
    return "/" in sym


# ---------------------------------------------------------------------------------------------------- base
class Market:
    def __init__(self):
        self.on_bar = None
        self.subs = set()
        self.cur = {}  # symbol -> current 1m bar [t,o,h,l,c,v]
        self.lock = threading.RLock()

    def last(self, sym):
        b = self.cur.get(sym)
        return b[4] if b else None

    def search(self, q):
        q = (q or "").strip().upper()
        res = [{"symbol": s, "name": n, "type": "crypto" if is_crypto(s) else "stock"} for s, n, _ in CATALOG
               if not q or q in s or q in n.upper()]
        return res[:30]

    def subscribe(self, symbols):
        self.subs = set(symbols)

    def emit(self, sym, bar):
        if self.on_bar:
            self.on_bar(sym, list(bar))


# ---------------------------------------------------------------------------------------------------- fake
class FakeMarket(Market):
    name = "fake"
    DAYS_1M = 75

    def __init__(self, tick=0.25):
        super().__init__()
        self.tick = tick
        self.m1 = {}     # symbol -> list of 1m bars
        self.daily = {}  # symbol -> daily bars before the 1m window
        self.rng = {}
        threading.Thread(target=self._loop, daemon=True).start()

    def _profile(self, sym):
        base = next((p for s, _, p in CATALOG if s == sym), None)
        r = random.Random(_seed(sym))
        if base is None:
            base = round(r.uniform(20, 400), 2)
        vol = 0.9 if is_crypto(sym) else r.uniform(0.25, 0.7)
        return base, vol

    def _ensure(self, sym):
        with self.lock:
            if sym in self.m1:
                return
            base, vol = self._profile(sym)
            r = random.Random(_seed(sym) + 1)
            now = int(time.time()) // 60 * 60
            n = self.DAYS_1M * 1440
            sig = vol / math.sqrt(365 * 1440)
            # walk backward from the base price so the current price sits near the catalog price
            closes = [base]
            drift_cycle = r.uniform(0.6, 1.4)
            for k in range(n - 1):
                trend = 0.00002 * math.sin(k / (1440 * 9 * drift_cycle))
                closes.append(closes[-1] / math.exp(r.gauss(trend, sig)))
            closes.reverse()
            bars = []
            prev = closes[0]
            t0 = now - (n - 1) * 60
            for k, c in enumerate(closes):
                t = t0 + k * 60
                lt = datetime.fromtimestamp(t, NY)
                mins = lt.hour * 60 + lt.minute
                if not is_crypto(sym) and lt.weekday() >= 5:
                    prev = c
                    continue
                rth = 570 <= mins < 960 and lt.weekday() < 5
                act = (1.0 if is_crypto(sym) else (1.0 if rth else 0.18)) * (1.6 if mins in range(570, 600) or mins in range(930, 960) else 1)
                o = prev
                spread = abs(r.gauss(0, sig * 0.8)) * c
                h, l = max(o, c) + spread * r.random(), min(o, c) - spread * r.random()
                v = int(max(1, r.lognormvariate(math.log(4000 * act), 0.6)))
                bars.append([t, round(o, 4), round(h, 4), round(l, 4), round(c, 4), v])
                prev = c
            self.m1[sym] = bars
            # daily history before the 1m window
            d, p, out = 1600, bars[0][1], []
            day0 = bucket_start(bars[0][0], 1, "D")
            sig_d = vol / math.sqrt(252)
            for k in range(d):
                c = p
                o = c / math.exp(r.gauss(0.0003, sig_d * 0.6))
                h, l = max(o, c) * (1 + abs(r.gauss(0, sig_d * 0.4))), min(o, c) * (1 - abs(r.gauss(0, sig_d * 0.4)))
                t = day0 - (k + 1) * 86400
                if not is_crypto(sym) and datetime.fromtimestamp(t + 43200, NY).weekday() >= 5:
                    p = o
                    continue
                out.append([bucket_start(t + 43200, 1, "D"), round(o, 4), round(h, 4), round(l, 4), round(c, 4), int(r.lognormvariate(math.log(3e6), 0.5))])
                p = o
            out.reverse()
            self.daily[sym] = out
            self.cur[sym] = bars[-1]
            self.rng[sym] = random.Random(_seed(sym) + 2)

    def bars(self, sym, tf, limit=1500, before=None):
        n, unit = parse_tf(tf)
        if unit == "s":
            raise ValueError("Second timeframes are not available")
        self._ensure(sym)
        with self.lock:
            m1 = self.m1[sym]
            if unit in ("s", "m", "h"):
                need = limit * n * (UNIT_SEC[unit] // 60 or 1) + n * UNIT_SEC[unit] // 60
                end = len(m1) if before is None else bisect.bisect_left(m1, [before])
                start = max(0, end - need)
                agg = aggregate(m1[start:end], n, unit)
                if before is not None:
                    agg = [b for b in agg if b[0] < before]
                if start > 0 and agg:
                    agg = agg[1:]  # first bucket may be partial
                return agg[-limit:], start > 0
            days = self.daily[sym] + aggregate(m1, 1, "D")
            if before is not None:
                days = [b for b in days if b[0] < before]
            agg = aggregate(days, n, unit) if (n, unit) != (1, "D") else days
            return [list(b) for b in agg[-limit:]], len(agg) > limit

    def quotes(self, symbols):
        out = {}
        for s in symbols:
            self._ensure(s)
            m1 = self.m1[s]
            last = m1[-1]
            day0 = bucket_start(last[0], 1, "D")
            i = bisect.bisect_left(m1, [day0])
            today = m1[i:] or [last]
            prev = m1[i - 1][4] if i > 0 else today[0][1]
            out[s] = {"last": last[4], "prev": prev, "open": today[0][1], "high": max(b[2] for b in today), "low": min(b[3] for b in today),
                      "volume": sum(b[5] for b in today), "name": NAMES.get(s, s)}
        return out

    def subscribe(self, symbols):
        for s in symbols:
            self._ensure(s)
        self.subs = set(symbols)

    def _loop(self):
        while True:
            time.sleep(self.tick)
            for sym in list(self.subs):
                try:
                    self._step(sym)
                except Exception as e:  # pragma: no cover
                    print("fake tick failed", sym, e)

    def _step(self, sym):
        with self.lock:
            m1, r = self.m1[sym], self.rng[sym]
            _, vol = self._profile(sym)
            now = int(time.time())
            t = now - now % 60
            last = m1[-1]
            px = last[4] * math.exp(r.gauss(0, vol / math.sqrt(365 * 1440 * 240)))
            px = round(px, 4 if px < 5 else 2)
            size = int(r.lognormvariate(math.log(60), 0.8))
            if t > last[0]:
                while last[0] + 60 < t:  # fill gaps (e.g. after the laptop slept)
                    last = [last[0] + 60, last[4], last[4], last[4], last[4], 0]
                    m1.append(last)
                bar = [t, last[4], max(last[4], px), min(last[4], px), px, size]
                m1.append(bar)
            else:
                bar = last
                bar[2] = max(bar[2], px); bar[3] = min(bar[3], px); bar[4] = px; bar[5] += size
            self.cur[sym] = bar
        self.emit(sym, bar)


# ---------------------------------------------------------------------------------------------------- alpaca
class AlpacaError(Exception):
    pass


class AlpacaMarket(Market):
    name = "alpaca"
    DATA = "https://data.alpaca.markets"
    STREAM = "wss://stream.data.alpaca.markets"

    def __init__(self, key, secret, feed="iex"):
        super().__init__()
        self.key, self.secret, self.feed = key, secret, feed
        self.assets = None
        self.pending = {}
        self.ws = {}
        self.want = {"stocks": set(), "crypto": set()}
        threading.Thread(target=self._flush_loop, daemon=True).start()
        for kind in ("stocks", "crypto"):
            threading.Thread(target=self._stream_loop, args=(kind,), daemon=True).start()

    def _get(self, path, params):
        url = self.DATA + path + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read() or b"{}").get("message", "")
            except Exception:
                msg = ""
            raise AlpacaError(f"Alpaca {e.code}: {msg or e.reason}")
        except urllib.error.URLError as e:
            raise AlpacaError(f"Alpaca unreachable: {e.reason}")

    def search(self, q):
        if self.assets is None:
            try:
                req = urllib.request.Request("https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity",
                                             headers={"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret})
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.assets = [(a["symbol"], a.get("name") or a["symbol"]) for a in json.loads(r.read()) if a.get("tradable")]
            except Exception:
                self.assets = []
        q = (q or "").strip().upper()
        if not q:
            return super().search(q)
        exact = [{"symbol": s, "name": n, "type": "stock"} for s, n in self.assets if s == q]
        starts = [{"symbol": s, "name": n, "type": "stock"} for s, n in self.assets if s.startswith(q) and s != q]
        names = [{"symbol": s, "name": n, "type": "stock"} for s, n in self.assets if q in n.upper() and not s.startswith(q)]
        crypto = [x for x in super().search(q) if x["type"] == "crypto"]
        return (exact + starts + crypto + names)[:30] or super().search(q)

    @staticmethod
    def _plan(n, unit):
        """Alpaca timeframe to request and the aggregation to apply on top of it."""
        if unit == "m":
            if n <= 59:
                return f"{n}Min", None
            if n % 60 == 0 and n // 60 <= 23:
                return f"{n // 60}Hour", None
            base = max(d for d in (1, 5, 15, 30) if n % d == 0)
            return f"{base}Min", (n, "m")
        if unit == "h":
            return (f"{n}Hour", None) if n <= 23 else ("1Hour", (n, "h"))
        if unit == "D":
            return "1Day", (None if n == 1 else (n, "D"))
        if unit == "W":
            return "1Week", (None if n == 1 else (n, "W"))
        if unit == "M":
            return (f"{n}Month", None) if n in (1, 2, 3, 4, 6, 12) else ("1Month", (n, "M"))
        return "1Min", (n, unit)

    def bars(self, sym, tf, limit=1500, before=None):
        n, unit = parse_tf(tf)
        tfa, agg = self._plan(n, unit)
        sec = n * UNIT_SEC[unit]
        crypto = is_crypto(sym)
        factor = 1.15 if crypto else (3.2 if sec < 86400 else 1.6)
        end = datetime.fromtimestamp(before, timezone.utc) if before else datetime.now(timezone.utc)
        start = end - timedelta(seconds=sec * limit * factor + 4 * 86400)
        params = {"timeframe": tfa, "start": start.isoformat().replace("+00:00", "Z"), "end": (end - timedelta(seconds=1)).isoformat().replace("+00:00", "Z") if before else None,
                  "limit": 10000, "sort": "asc"}
        rows, token, pages = [], None, 0
        while True:
            if crypto:
                d = self._get("/v1beta3/crypto/us/bars", {**params, "symbols": sym, "page_token": token})
                chunk = (d.get("bars") or {}).get(sym) or []
            else:
                d = self._get(f"/v2/stocks/{urllib.parse.quote(sym)}/bars", {**params, "adjustment": "split", "feed": self.feed, "page_token": token})
                chunk = d.get("bars") or []
            rows += chunk
            token, pages = d.get("next_page_token"), pages + 1
            if not token or pages > 30:
                break
        out = []
        for b in rows:
            t = int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp())
            out.append([t, b["o"], b["h"], b["l"], b["c"], b.get("v", 0)])
        if unit in ("D", "W", "M"):
            for b in out:
                b[0] = bucket_start(b[0] + 43200, 1, "D") if unit == "D" else bucket_start(b[0] + 43200, 1, unit)
        if agg:
            out = aggregate(out, *agg)
        if before:
            out = [b for b in out if b[0] < before]
        # merge the live minute into the newest bar
        cur = self.cur.get(sym)
        if cur and not before and out:
            k = bucket_start(cur[0], n, unit)
            if out[-1][0] == k:
                o = out[-1]; o[2] = max(o[2], cur[2]); o[3] = min(o[3], cur[3]); o[4] = cur[4]
            elif out[-1][0] < k:
                out.append([k, cur[1], cur[2], cur[3], cur[4], cur[5]])
        return out[-limit:], len(rows) > 0

    def quotes(self, symbols):
        out = {}
        st = [s for s in symbols if not is_crypto(s)]
        cr = [s for s in symbols if is_crypto(s)]
        if st:
            d = self._get("/v2/stocks/snapshots", {"symbols": ",".join(st), "feed": self.feed})
            for s, q in d.items():
                if not q:
                    continue
                db, pb, lt = q.get("dailyBar") or {}, q.get("prevDailyBar") or {}, q.get("latestTrade") or {}
                out[s] = {"last": lt.get("p") or db.get("c"), "prev": pb.get("c"), "open": db.get("o"), "high": db.get("h"), "low": db.get("l"), "volume": db.get("v"), "name": NAMES.get(s, s)}
        if cr:
            d = self._get("/v1beta3/crypto/us/snapshots", {"symbols": ",".join(cr)}).get("snapshots", {})
            for s, q in d.items():
                db, pb, lt = q.get("dailyBar") or {}, q.get("prevDailyBar") or {}, q.get("latestTrade") or {}
                out[s] = {"last": lt.get("p") or db.get("c"), "prev": pb.get("c"), "open": db.get("o"), "high": db.get("h"), "low": db.get("l"), "volume": db.get("v"), "name": NAMES.get(s, s)}
        return out

    def subscribe(self, symbols):
        self.subs = set(symbols)
        self.want = {"stocks": {s for s in symbols if not is_crypto(s)}, "crypto": {s for s in symbols if is_crypto(s)}}
        for s in symbols:
            if s not in self.cur:
                threading.Thread(target=self._seed_cur, args=(s,), daemon=True).start()

    def _seed_cur(self, s):
        try:
            b, _ = self.bars(s, "1m", 2)
            if b and s not in self.cur:
                self.cur[s] = list(b[-1])
        except Exception:
            pass

    # live: trades build the current minute bar, the official minute bar replaces it when it closes
    def _trade(self, sym, price, size, ts):
        t = int(ts) - int(ts) % 60
        with self.lock:
            bar = self.cur.get(sym)
            if not bar or t > bar[0]:
                bar = [t, price, price, price, price, size]
            elif t == bar[0]:
                bar[2] = max(bar[2], price); bar[3] = min(bar[3], price); bar[4] = price; bar[5] += size
            else:
                return
            self.cur[sym] = bar
            self.pending[sym] = bar

    def _minute(self, sym, b):
        t = int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp())
        with self.lock:
            bar = [t, b["o"], b["h"], b["l"], b["c"], b.get("v", 0)]
            cur = self.cur.get(sym)
            if not cur or cur[0] <= t:
                self.cur[sym] = bar if (not cur or cur[0] == t) else cur
            self.pending[sym + "#" + str(t)] = bar

    def _flush_loop(self):
        while True:
            time.sleep(0.25)
            with self.lock:
                p, self.pending = self.pending, {}
            for k, bar in p.items():
                self.emit(k.split("#")[0], bar)

    def _stream_loop(self, kind):
        from wsclient import WebSocket, WSClosed
        url = f"{self.STREAM}/v2/{self.feed}" if kind == "stocks" else f"{self.STREAM}/v1beta3/crypto/us"
        backoff = 2
        while True:
            if not self.want[kind]:
                time.sleep(1)
                continue
            ws = None
            try:
                ws = WebSocket(url)
                ws.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
                have = set()
                ws.sock.settimeout(1.0)
                while True:
                    want = self.want[kind]
                    if want != have:
                        add, rem = sorted(want - have), sorted(have - want)
                        if add:
                            ws.send(json.dumps({"action": "subscribe", "trades": add, "bars": add, "updatedBars": add}))
                        if rem:
                            ws.send(json.dumps({"action": "unsubscribe", "trades": rem, "bars": rem, "updatedBars": rem}))
                        have = set(want)
                    try:
                        msg = ws.recv()
                    except (TimeoutError, OSError) as e:
                        if isinstance(e, WSClosed):
                            raise
                        if "timed out" in str(e):
                            continue
                        raise
                    for m in json.loads(msg):
                        typ = m.get("T")
                        if typ == "t":
                            ts = datetime.fromisoformat(m["t"][:26].rstrip("Z") + "+00:00").timestamp()
                            self._trade(m["S"], m["p"], m.get("s", 0), ts)
                        elif typ in ("b", "u"):
                            self._minute(m["S"], m)
                        elif typ == "error":
                            print("alpaca stream error:", m)
                            if m.get("code") in (402, 404, 409):
                                time.sleep(30)
                backoff = 2
            except Exception as e:
                print(f"alpaca {kind} stream: {e}; reconnecting in {backoff}s")
                time.sleep(backoff)
                backoff = min(60, backoff * 2)
            finally:
                if ws:
                    ws.close()
