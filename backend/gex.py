"""Options positioning for one ticker: gamma exposure (GEX), delta exposure (DEX), walls, gamma flip, max pain.

Data: Schwab option chain when Schwab is connected with the Market Data product, otherwise Alpaca
(open interest from the contracts endpoint + greeks/IV from option snapshots). Open interest is the previous
session's close, so the numbers describe positioning as of this morning, not live.

Convention (the common "dealers are long calls and short puts" assumption):
  GEX per contract = gamma * OI * 100 * spot^2 * 1%     (+ for calls, - for puts)   -> $ of hedging per 1% move
  DEX per contract = delta * OI * 100 * spot            (calls +, puts -)            -> $ delta held by option buyers
Positive net GEX: dealers tend to sell rallies and buy dips (moves dampened).
Negative net GEX: dealers hedge in the direction of the move (moves amplified).
"""
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from util import BadRequest, Unavailable, now_ny

R = 0.04  # risk-free rate used for Black-Scholes greeks when a source doesn't provide them


def _n(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def _N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_greeks(S, K, T, iv, is_call):
    if S <= 0 or K <= 0 or T <= 0 or not iv or iv <= 0:
        return None, None
    st = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (R + iv * iv / 2) * T) / st
    gamma = _n(d1) / (S * st)
    delta = _N(d1) if is_call else _N(d1) - 1
    return delta, gamma


def _years(exp):
    end = datetime.strptime(exp + " 16:00", "%Y-%m-%d %H:%M")
    return max((end - now_ny()).total_seconds() / (365 * 86400), 1 / (365 * 24))


def _get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise Unavailable(f"Option data request failed ({e.code}): {e.read().decode(errors='replace')[:160]}")
    except urllib.error.URLError as e:
        raise Unavailable(f"Option data couldn't be reached: {e.reason}")


# ---------- sources ----------

def from_schwab(sub, symbol, max_days):
    import db
    import schwab
    item = db.get(db.upk(sub), schwab.SK)
    if not item or not schwab.app_config():
        return None
    try:
        access = schwab._access_token(sub, item)
    except Exception:
        return None
    today = now_ny().date()
    q = urllib.parse.urlencode({"symbol": symbol, "contractType": "ALL", "includeUnderlyingQuote": "true",
                                "strategy": "SINGLE", "range": "ALL", "fromDate": today.isoformat(),
                                "toDate": (today + timedelta(days=max_days)).isoformat()})
    try:
        data = _get(f"https://api.schwabapi.com/marketdata/v1/chains?{q}",
                    {"Authorization": f"Bearer {access}", "Accept": "application/json"})
    except Unavailable:
        return None   # usually: the Schwab app doesn't have the Market Data product; fall back to Alpaca
    spot = data.get("underlyingPrice") or (data.get("underlying") or {}).get("last")
    out = []
    for key, is_call in (("callExpDateMap", True), ("putExpDateMap", False)):
        for exp_key, strikes in (data.get(key) or {}).items():
            exp = exp_key.split(":")[0]
            for k, arr in strikes.items():
                for c in arr:
                    out.append({"sym": (c.get("symbol") or "").replace(" ", ""), "call": is_call, "strike": float(k),
                                "exp": exp, "oi": c.get("openInterest") or 0,
                                "iv": (c.get("volatility") or 0) / 100 if (c.get("volatility") or 0) > 0 else None,
                                "delta": c.get("delta") if c.get("delta") not in (None, -999.0) else None,
                                "gamma": c.get("gamma") if c.get("gamma") not in (None, -999.0) else None,
                                "volume": c.get("totalVolume") or 0,
                                "mark": c.get("mark") if (c.get("mark") or 0) > 0 else
                                ((c.get("bid") or 0) + (c.get("ask") or 0)) / 2 or None})
    return {"source": "Schwab", "spot": spot, "contracts": out}


def from_alpaca(creds, symbol, max_days, spot):
    h = {"APCA-API-KEY-ID": creds["key"], "APCA-API-SECRET-KEY": creds["secret"]}
    base = {"paper": "https://paper-api.alpaca.markets", "live": "https://api.alpaca.markets"}[creds.get("env", "paper")]
    today = now_ny().date()
    lo, hi = round(spot * 0.5, 2), round(spot * 1.5, 2)
    contracts, token = {}, None
    for _ in range(20):
        q = {"underlying_symbols": symbol, "expiration_date_gte": today.isoformat(),
             "expiration_date_lte": (today + timedelta(days=max_days)).isoformat(),
             "strike_price_gte": lo, "strike_price_lte": hi, "limit": 10000}
        if token:
            q["page_token"] = token
        d = _get(f"{base}/v2/options/contracts?" + urllib.parse.urlencode(q), h)
        for c in d.get("option_contracts") or []:
            contracts[c["symbol"]] = {"sym": c["symbol"], "call": c["type"] == "call", "strike": float(c["strike_price"]),
                                      "exp": c["expiration_date"], "oi": int(float(c.get("open_interest") or 0)),
                                      "oiDate": c.get("open_interest_date"), "iv": None, "delta": None, "gamma": None, "volume": 0,
                                      "mark": float(c["close_price"]) if c.get("close_price") else None}
        token = d.get("next_page_token")
        if not token:
            break
    token = None
    for _ in range(40):
        q = {"feed": "indicative", "limit": 1000, "expiration_date_lte": (today + timedelta(days=max_days)).isoformat(),
             "strike_price_gte": lo, "strike_price_lte": hi}
        if token:
            q["page_token"] = token
        d = _get(f"https://data.alpaca.markets/v1beta1/options/snapshots/{urllib.parse.quote(symbol)}?" + urllib.parse.urlencode(q), h)
        for sym, snap in (d.get("snapshots") or {}).items():
            c = contracts.get(sym)
            if not c:
                continue
            g = snap.get("greeks") or {}
            c["delta"], c["gamma"], c["iv"] = g.get("delta"), g.get("gamma"), snap.get("impliedVolatility")
            c["volume"] = (snap.get("dailyBar") or {}).get("v") or 0
            qt = snap.get("latestQuote") or {}
            if qt.get("bp") and qt.get("ap"):
                c["mark"] = (qt["bp"] + qt["ap"]) / 2
            elif (snap.get("latestTrade") or {}).get("p"):
                c["mark"] = snap["latestTrade"]["p"]
        token = d.get("next_page_token")
        if not token:
            break
    return {"source": "Alpaca", "spot": spot, "contracts": list(contracts.values())}


def expected_moves(contracts, spot, limit=10):
    """Expected move per expiration from the at-the-money straddle (what the market charges for a move),
    plus the implied-volatility version (about a one-standard-deviation range, ~68% probability)."""
    by_exp = {}
    for c in contracts:
        by_exp.setdefault(c["exp"], []).append(c)
    out = []
    for exp in sorted(by_exp)[:limit]:
        cs = by_exp[exp]
        ks = sorted({c["strike"] for c in cs})
        if not ks:
            continue
        k = min(ks, key=lambda x: abs(x - spot))
        call = next((c for c in cs if c["call"] and c["strike"] == k), None)
        put = next((c for c in cs if not c["call"] and c["strike"] == k), None)
        T = _years(exp)
        dte = max(0, (datetime.strptime(exp, "%Y-%m-%d").date() - now_ny().date()).days)
        straddle = (call["mark"] + put["mark"]) if call and put and call.get("mark") and put.get("mark") else None
        ivs = [x.get("iv") for x in (call, put) if x and x.get("iv")]
        iv = sum(ivs) / len(ivs) if ivs else None
        iv_move = spot * iv * math.sqrt(T) if iv else None
        move = straddle if straddle else iv_move
        if not move:
            continue
        out.append({"exp": exp, "dte": dte, "atmStrike": k, "straddle": round(straddle, 2) if straddle else None,
                    "ivAtm": round(iv, 4) if iv else None, "ivMove": round(iv_move, 2) if iv_move else None,
                    "move": round(move, 2), "pct": round(move / spot * 100, 2),
                    "upper": round(spot + move, 2), "lower": round(spot - move, 2)})
    daily = None
    near = next((m for m in out if m["ivAtm"] and m["dte"] >= 1), None)
    if near:
        d = spot * near["ivAtm"] / math.sqrt(252)
        daily = {"move": round(d, 2), "pct": round(d / spot * 100, 2), "upper": round(spot + d, 2), "lower": round(spot - d, 2)}
    return {"byExpiration": out, "daily": daily}


def spot_price(symbol):
    from charts import _yahoo
    now = now_ny()
    for tf, back in (("5m", 4), ("1d", 10)):
        try:
            bars = _yahoo(symbol.replace(".", "-"), tf, now - timedelta(days=back), now + timedelta(hours=1))
        except Exception:
            bars = []
        if bars:
            return bars[-1]["c"]
    return None


# ---------- math ----------

def compute(contracts, spot):
    rows = []
    for c in contracts:
        if not c["oi"]:
            continue
        T = _years(c["exp"])
        d, g = c.get("delta"), c.get("gamma")
        if (d is None or g is None) and c.get("iv"):
            bd, bg = bs_greeks(spot, c["strike"], T, c["iv"], c["call"])
            d = d if d is not None else bd
            g = g if g is not None else bg
        if g is None:
            continue
        sign = 1 if c["call"] else -1
        gex = sign * g * c["oi"] * 100 * spot * spot * 0.01
        dex = (d or 0) * c["oi"] * 100 * spot
        rows.append({**c, "T": T, "gex": gex, "dex": dex, "gammaUsed": g, "deltaUsed": d})
    if not rows:
        raise BadRequest("No contracts with open interest and greeks were found for this ticker and range.")

    by = {}
    for r in rows:
        b = by.setdefault(r["strike"], {"strike": r["strike"], "callGex": 0.0, "putGex": 0.0, "callOi": 0, "putOi": 0,
                                        "callVol": 0, "putVol": 0, "dex": 0.0})
        if r["call"]:
            b["callGex"] += r["gex"]; b["callOi"] += r["oi"]; b["callVol"] += r["volume"] or 0
        else:
            b["putGex"] += r["gex"]; b["putOi"] += r["oi"]; b["putVol"] += r["volume"] or 0
        b["dex"] += r["dex"]
    strikes = sorted(by.values(), key=lambda b: b["strike"])
    for b in strikes:
        b["netGex"] = b["callGex"] + b["putGex"]

    # gamma flip: where total GEX would change sign if spot moved (re-pricing gamma with each contract's IV)
    flip = None
    with_iv = [r for r in rows if r.get("iv")]
    if with_iv:
        grid = [spot * (0.85 + 0.005 * i) for i in range(61)]
        tot = []
        for S in grid:
            t = 0.0
            for r in with_iv:
                _, g = bs_greeks(S, r["strike"], r["T"], r["iv"], r["call"])
                if g:
                    t += (1 if r["call"] else -1) * g * r["oi"] * 100 * S * S * 0.01
            tot.append(t)
        crossings = [grid[i] - tot[i] * (grid[i + 1] - grid[i]) / (tot[i + 1] - tot[i])
                     for i in range(len(grid) - 1) if tot[i] == 0 or (tot[i] < 0) != (tot[i + 1] < 0)]
        if crossings:
            flip = min(crossings, key=lambda x: abs(x - spot))
        profile = [{"price": round(S, 2), "gex": round(t)} for S, t in zip(grid, tot)]
    else:
        profile = []

    # max pain: strike where option holders' total payoff at expiry is smallest (nearest expiration only)
    nearest = min(r["exp"] for r in rows)
    near = [r for r in rows if r["exp"] == nearest]
    cand = sorted({r["strike"] for r in near})
    pain = min(cand, key=lambda K: sum(r["oi"] * max(0, (K - r["strike"]) if r["call"] else (r["strike"] - K)) for r in near)) if cand else None

    call_wall = max(strikes, key=lambda b: b["callGex"])
    put_wall = min(strikes, key=lambda b: b["putGex"])
    net = sum(b["netGex"] for b in strikes)
    c_oi, p_oi = sum(b["callOi"] for b in strikes), sum(b["putOi"] for b in strikes)
    c_v, p_v = sum(b["callVol"] for b in strikes), sum(b["putVol"] for b in strikes)
    exps = sorted({r["exp"] for r in rows})
    return {
        "spot": round(spot, 2), "netGex": round(net), "netDex": round(sum(b["dex"] for b in strikes)),
        "regime": "positive" if net > 0 else "negative",
        "gammaFlip": round(flip, 2) if flip else None,
        "callWall": call_wall["strike"], "putWall": put_wall["strike"],
        "maxPain": pain, "maxPainExpiry": nearest,
        "callOi": c_oi, "putOi": p_oi, "putCallOi": round(p_oi / c_oi, 2) if c_oi else None,
        "callVolume": c_v, "putVolume": p_v, "putCallVolume": round(p_v / c_v, 2) if c_v else None,
        "expirations": exps, "contracts": len(rows),
        "strikes": [{k: (round(v) if isinstance(v, float) and k != "strike" else v) for k, v in b.items()} for b in strikes],
        "profile": profile,
    }


def run(sub, symbol, max_days=45, expiry=None, strikes_each_side=None):
    symbol = (symbol or "").strip().upper()
    if not symbol or len(symbol) > 8 or not symbol.replace(".", "").isalnum():
        raise BadRequest("Enter a ticker symbol, e.g. NVDA.")
    max_days = max(1, min(int(max_days or 45), 400))
    data = from_schwab(sub, symbol, max_days)
    if not data or not data["contracts"]:
        import alpaca
        creds = alpaca.creds(sub)
        if not creds:
            raise BadRequest("Gamma exposure needs option data: connect Alpaca (free) or Schwab with the Market Data product in Settings → Brokers.")
        spot = spot_price(symbol)
        if not spot:
            raise BadRequest(f"No price found for {symbol}.")
        data = from_alpaca(creds, symbol, max_days, spot)
    spot = data["spot"] or spot_price(symbol)
    contracts = data["contracts"]
    all_exps = sorted({c["exp"] for c in contracts if c.get("oi")})
    em = expected_moves(contracts, spot)
    if expiry:
        contracts = [c for c in contracts if c["exp"] == expiry]
    if strikes_each_side:
        n = int(strikes_each_side)
        ks = sorted({c["strike"] for c in contracts})
        below = [k for k in ks if k <= spot][-n:]
        above = [k for k in ks if k > spot][:n]
        keep = set(below + above)
        contracts = [c for c in contracts if c["strike"] in keep]
    if not contracts:
        raise BadRequest(f"No option contracts found for {symbol} in the next {max_days} days.")
    out = compute(contracts, spot)
    out["expirations"] = all_exps
    out["expectedMove"] = em
    out.update({"symbol": symbol, "source": data["source"], "maxDays": max_days, "expiry": expiry,
                "strikesEachSide": int(strikes_each_side) if strikes_each_side else None,
                "asOf": now_ny().strftime("%Y-%m-%d %H:%M"),
                "oiDate": max((c.get("oiDate") or "" for c in contracts), default="") or None})
    return out
