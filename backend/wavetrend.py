"""WaveTrend [Krypt] (Pine v3) - the same port as wtKrypt() in the frontend: two lines (sig, ma) and the buy / sell dots."""
import math

NA = float("nan")


def _f(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


def _nz(x, y):
    return x if _f(x) else y


def _truthy(x):                                        # JS truthiness of a number (NaN and 0 are false)
    return _f(x) and x != 0


def wt(close, n1=10, n2=21, smoothing=4, maperiod=4, ob=0.75, os_=-0.75):
    N = len(close)

    def xema(src, ln):
        m, r = 2 / (ln + 1), [NA] * N
        for i in range(N):
            r[i] = m * src[i] + (1 - m) * _nz(r[i - 1] if i else NA, src[i]) if _f(src[i]) else NA
        return r

    def ss(src, lower):
        a1 = math.exp(-math.pi * math.sqrt(2) / lower)
        c2, c3 = 2 * a1 * math.cos(math.sqrt(2) * math.pi / lower), -a1 * a1
        c1, f = (1 - c2 - c3) / 2, [NA] * N
        for i in range(N):
            s1 = src[i - 1] if i else NA
            f1 = f[i - 1] if i else NA
            f2 = f[i - 2] if i > 1 else NA
            f[i] = _nz(c1 * (src[i] + _nz(s1, src[i])) + c2 * f1 + c3 * _nz(f2, f1), src[i])
        return f

    def window(s, n, fn):
        out = []
        for i in range(N):
            if i < n - 1:
                out.append(NA)
                continue
            w = s[i - n + 1:i + 1]
            out.append(fn(w) if all(_f(x) for x in w) else NA)
        return out

    def step(xs, vals):
        r = [NA] * N
        for i in range(N):
            r[i] = _nz(vals[i] if xs[i] else (r[i - 1] if i else NA), vals[i])
        return r

    def cas(xs, start, wh, up):
        r = [NA] * N
        for i in range(N):
            x, x1, r1 = xs[i], xs[i - 1] if i else NA, r[i - 1] if i else NA
            st = start if isinstance(start, bool) else start[i]
            w = wh if isinstance(wh, bool) else wh[i]
            mv = (x > x1) if up else (x < x1)
            hold = (x >= x1) if up else (x <= x1)
            d = (x - x1) if up else (x1 - x)
            v = d if (st and mv and not _truthy(r1) and w) else ((r1 + d) if (hold and w) else 0)
            r[i] = abs(_nz(v, 0))
        return r

    price = [math.log(c) if i and _f(close[i - 1]) and _f(c) and c > 0 else NA for i, c in enumerate(close)]
    pe = xema(price, n1)
    delta = [p - e if _f(p) and _f(e) else NA for p, e in zip(price, pe)]
    d = xema([abs(x) if _f(x) else NA for x in delta], n1)
    ci = [NA if not _f(d[i]) else 0 if d[i] == 0 else (delta[i] / d[i] if _f(delta[i]) else NA) for i in range(N)]
    sig = ss(xema(ci, n2), smoothing)
    ma = window(sig, maperiod, lambda w: sum(w) / len(w))
    mid = (ob + os_) / 2
    exoff, hu, hl, req = (ob - mid) / 7, mid + 0.61803398875, mid - 0.61803398875, 5
    ssig, sma_ = window(sig, req, sum), window(ma, req, sum)
    gt = lambda a, b: _f(a) and _f(b) and a > b
    lt = lambda a, b: _f(a) and _f(b) and a < b
    ge = lambda a, b: _f(a) and _f(b) and a >= b
    le = lambda a, b: _f(a) and _f(b) and a <= b
    xo = lambda i: i > 0 and gt(sig[i], ma[i]) and le(sig[i - 1], ma[i - 1])
    xu = lambda i: i > 0 and lt(sig[i], ma[i]) and ge(sig[i - 1], ma[i - 1])
    allb = [lt(ma[i], ob) and xo(i) and i > 0 and lt(ssig[i - 1], sma_[i - 1]) for i in range(N)]
    allt = [gt(ma[i], os_) and xu(i) and i > 0 and gt(ssig[i - 1], sma_[i - 1]) for i in range(N)]
    bsteps, tsteps = step(allb, ma), step(allt, ma)
    pb_ = step(allb, window(price, req, min))
    pt_ = step(allt, window(price, req, max))
    cb, cdp = cas(pb_, True, True, True), cas(pt_, True, True, False)
    ct, cdb = cas(tsteps, True, True, True), cas(bsteps, True, True, False)
    hb = [math.sqrt(x * y) for x, y in zip(cdb, cb)]
    hr = [math.sqrt(x * y) for x, y in zip(ct, cdp)]
    hbull = [x / (1 + y) for x, y in zip(hb, hr)]
    hbear = [x / (1 + y) for x, y in zip(hr, hb)]
    pbull = cas(bsteps, [i > 0 and (lt(bsteps[i - 1], os_) or gt(hbull[i - 1], hbear[i - 1])) for i in range(N)], [lt(x, ob) for x in sig], True)
    pbear = cas(tsteps, [i > 0 and (gt(tsteps[i - 1], ob) or gt(hbear[i - 1], hbull[i - 1])) for i in range(N)], [gt(x, os_) for x in sig], False)
    btr = [x / (1 + y) for x, y in zip(pbull, pbear)]
    st_ = [x / (1 + y) for x, y in zip(pbear, pbull)]
    buy = [bool(allb[i] and ((lt(ma[i], mid) and (lt(sig[i - 1], os_ - exoff) or btr[i] > hbear[i])) or (lt(ma[i], hu) and hbull[i] > st_[i])))
           for i in range(N)]
    sell = [bool(allt[i] and ((gt(ma[i], mid) and (gt(sig[i - 1], ob + exoff) or st_[i] > hbull[i])) or (gt(ma[i], hl) and hbear[i] > btr[i])))
            for i in range(N)]
    return {"sig": sig, "ma": ma, "buy": buy, "sell": sell}


def dots(bars, n=1):
    """(buy, sell) per candle: buy = a WaveTrend buy dot on this candle or one of the n-1 before it; sell = a sell dot on this candle."""
    r = wt([b["c"] for b in bars])
    n = max(1, int(n or 1))
    win = lambda xs: [any(xs[max(0, i - n + 1):i + 1]) for i in range(len(xs))]
    return win(r["buy"]), r["sell"]
