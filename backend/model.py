"""Forward-looking earnings model, tested walk-forward (no look-ahead).

Two questions, two feature sets:
  pre   before the release (numbers unknown): the last reported quarter, consensus growth expected, what the stock did
        into the release (returns 1 / 5 / 21 / 63 sessions vs the benchmark, trend, volume, accumulation), its own past
        reactions, timing. Targets: day 0, size of the day-0 move, 5 days, 1 month, 2 months (vs the benchmark).
  post  after day 0 (numbers out): the reported quarter, surprises, guidance, the day-0 reaction and the run-up into it.
        Targets: the drift after day 0 (5 / 20 / 40 sessions, vs the benchmark).

Model: ridge regression on standardised, capped features (missing values set to a neutral value, with a flag per block).
Walk-forward: releases in date order; each one is predicted by a model fitted only on releases whose outcome was known
before its prediction time (the base close before the release; the day-0 close for "post"), refitted every quarter.
The first MIN_TRAIN releases only train. Scorecard per target: direction hit rate against always guessing the training
mean's sign, rank correlation (information coefficient) per quarter with its t-stat, top-minus-bottom fifth spread per
quarter, out-of-sample R² against the training mean, calibration by decile, and the final model's coefficients."""
import math

# key, low cap, high cap, transform, neutral value, block
F = {
    "prevRevYoY": (-0.5, 2, None, 0.0, "fund"), "prevRevAccel": (-0.5, 0.5, None, 0.0, "fund"), "prevEpsYoY": (-3, 5, None, 0.0, "fund"),
    "prevAccruals": (-0.3, 0.3, None, 0.0, "fund"),
    "revYoY": (-0.5, 2, None, 0.0, "fund"), "revAccel": (-0.5, 0.5, None, 0.0, "fund"), "revQoQ": (-0.5, 1, None, 0.0, "fund"),
    "epsYoY": (-3, 5, None, 0.0, "fund"), "epsAccel": (-5, 5, None, 0.0, "fund"), "accruals": (-0.3, 0.3, None, 0.0, "fund"),
    "estEpsG": (-3, 5, None, 0.0, "est"), "estRevG": (-0.5, 2, None, 0.0, "est"),
    "epsSurprise": (-50, 50, None, 0.0, "surp"), "revSurprise": (-20, 20, None, 0.0, "surp"), "guideN": (-1, 1, None, 0.0, "guide"),
    "pre1ab": (-0.2, 0.2, None, 0.0, "px"), "pre5ab": (-0.3, 0.3, None, 0.0, "px"), "pre21ab": (-0.5, 0.5, None, 0.0, "px"),
    "pre63ab": (-0.8, 0.8, None, 0.0, "px"), "offHigh": (-0.8, 0, None, -0.1, "px"), "sma50": (-0.5, 0.5, None, 0.0, "px"),
    "sma200": (-0.8, 1, None, 0.0, "px"), "vol20": (0.002, 0.15, "log", 0.02, "px"), "relVol1": (0.1, 10, "log", 1.0, "px"),
    "relVol5": (0.1, 10, "log", 1.0, "px"), "relVol20": (0.1, 10, "log", 1.0, "px"), "cmf20": (-1, 1, None, 0.0, "px"),
    "obv20": (-1, 1, None, 0.0, "px"),
    "ownAbs": (0, 0.4, None, 0.05, "own"), "ownMean": (-0.2, 0.2, None, 0.0, "own"), "prevR1ab": (-0.4, 0.4, None, 0.0, "own"),
    "amc": (0, 1, None, 0.0, None),
    "r1ab": (-0.4, 0.4, None, 0.0, None), "gap": (-0.4, 0.4, None, 0.0, None), "z1": (-15, 15, None, 0.0, None),
    "vol0": (0.1, 20, "log", 1.0, None),
}
LABEL = {"prevRevYoY": "Revenue YoY, last reported quarter", "prevRevAccel": "Revenue YoY change, last quarter", "prevEpsYoY": "EPS YoY, last quarter",
         "prevAccruals": "Accruals, last quarter", "revYoY": "Revenue YoY", "revAccel": "Revenue YoY change", "revQoQ": "Revenue QoQ",
         "epsYoY": "EPS YoY", "epsAccel": "EPS acceleration", "accruals": "Accrual ratio", "estEpsG": "Consensus EPS growth",
         "estRevG": "Consensus revenue growth", "epsSurprise": "EPS surprise", "revSurprise": "Revenue surprise", "guideN": "Guidance (raise +1 / lower −1)",
         "pre1ab": "Day before vs bench", "pre5ab": "5 days before vs bench", "pre21ab": "1 month before vs bench", "pre63ab": "3 months before vs bench",
         "offHigh": "Distance from 52w high", "sma50": "vs 50-day average", "sma200": "vs 200-day average", "vol20": "Volatility (log)",
         "relVol1": "Volume day before (log × avg)", "relVol5": "Volume 5 days (log × avg)", "relVol20": "Volume 20 days (log × avg)",
         "cmf20": "Accumulation (CMF 20d)", "obv20": "On-balance volume 20d", "ownAbs": "Own average |day-0 move|", "ownMean": "Own average day-0 move",
         "prevR1ab": "Last release's day-0 move", "amc": "After the close", "r1ab": "Day-0 move vs bench", "gap": "Day-0 gap", "z1": "Day-0 move (σ)",
         "vol0": "Day-0 volume (log × avg)", "miss_fund": "No fundamentals", "miss_est": "No consensus", "miss_surp": "No surprise data",
         "miss_guide": "No guidance", "miss_own": "No past releases"}

PRE = ["prevRevYoY", "prevRevAccel", "prevEpsYoY", "prevAccruals", "estEpsG", "estRevG", "pre1ab", "pre5ab", "pre21ab", "pre63ab",
       "offHigh", "sma50", "sma200", "vol20", "relVol1", "relVol5", "relVol20", "cmf20", "obv20", "ownAbs", "ownMean", "prevR1ab", "amc"]
POST = ["revYoY", "revAccel", "revQoQ", "epsYoY", "epsAccel", "accruals", "epsSurprise", "revSurprise", "guideN", "r1ab", "gap", "z1",
        "vol0", "pre21ab", "pre63ab", "offHigh", "relVol5", "cmf20", "ownMean", "amc"]
TARGETS = [  # key, label, model, outcome known on, value
    ("r1ab", "Day 0 vs bench", "pre", "t1", lambda r: r.get("r1ab")),
    ("abs1", "Size of the day-0 move", "pre", "t1", lambda r: abs(r["r1ab"]) if r.get("r1ab") is not None else None),
    ("r5ab", "5 days vs bench", "pre", "t5", lambda r: r.get("r5ab")),
    ("r20ab", "1 month vs bench", "pre", "t20", lambda r: r.get("r20ab")),
    ("r40ab", "2 months vs bench", "pre", "t40", lambda r: r.get("r40ab")),
    ("drift5", "Drift d0→d5", "post", "t5", lambda r: r.get("drift5")),
    ("drift20", "Drift d0→d20", "post", "t20", lambda r: r.get("drift20")),
    ("drift40", "Drift d0→d40", "post", "t40", lambda r: r.get("drift40")),
]
MIN_TRAIN = 300
LAMBDA = 0.10                                          # ridge penalty on the correlation scale
Y_CAP = 0.5


def _guide(r):
    g = r.get("guide")
    return {"raise": 1.0, "lower": -1.0, "maintain": 0.0, "initiate": 0.0}.get(g) if g not in (None, "none") else None


def features(r, keys):
    """The capped / transformed feature vector, neutral values for missing ones, plus one 'missing' flag per block used."""
    x, miss = [], {}
    for k in keys:
        lo, hi, tf, neu, blk = F[k]
        v = _guide(r) if k == "guideN" else (1.0 if r.get("timing") == "amc" else 0.0) if k == "amc" else r.get(k)
        if isinstance(v, (int, float)) and math.isfinite(v):
            v = min(hi, max(lo, v))
        else:
            v = None
            if blk:
                miss[blk] = 1.0
        if v is None:
            v = neu
        if tf == "log":
            v = math.log(max(v, 1e-6))
        x.append(float(v))
    return x, miss


def blocks(keys):
    return sorted({F[k][4] for k in keys if F[k][4]})


def vec(r, keys, blks):
    x, miss = features(r, keys)
    return x + [miss.get(b, 0.0) for b in blks]


def names(keys, blks):
    return list(keys) + ["miss_" + b for b in blks]


class Sums:
    """Running sums for ridge regression on standardised features."""

    def __init__(self, p):
        self.n, self.p = 0, p
        self.sx = [0.0] * p
        self.sxx = [[0.0] * p for _ in range(p)]
        self.sy = self.sxy = None
        self.sy, self.syy = 0.0, 0.0
        self.sxy = [0.0] * p

    def add(self, x, y):
        self.n += 1
        self.sy += y
        self.syy += y * y
        for i, xi in enumerate(x):
            self.sx[i] += xi
            self.sxy[i] += xi * y
            row = self.sxx[i]
            for j in range(i, self.p):
                row[j] += xi * x[j]

    def fit(self, lam=LAMBDA):
        n, p = self.n, self.p
        mu = [s / n for s in self.sx]
        ym = self.sy / n
        cov = [[0.0] * p for _ in range(p)]
        for i in range(p):
            for j in range(i, p):
                cov[i][j] = cov[j][i] = self.sxx[i][j] / n - mu[i] * mu[j]
        sd = [math.sqrt(cov[i][i]) if cov[i][i] > 1e-12 else 0.0 for i in range(p)]
        A = [[0.0] * p for _ in range(p)]
        b = [0.0] * p
        for i in range(p):
            for j in range(p):
                A[i][j] = cov[i][j] / (sd[i] * sd[j]) if sd[i] and sd[j] else 0.0
            A[i][i] += lam
            b[i] = (self.sxy[i] / n - mu[i] * ym) / sd[i] if sd[i] else 0.0
        beta = _solve(A, b)
        return {"mu": mu, "sd": sd, "beta": beta, "ym": ym, "n": n}


def _solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            continue
        M[c], M[piv] = M[piv], M[c]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def predict(fit, x):
    return fit["ym"] + sum(b * (xi - m) / s for b, xi, m, s in zip(fit["beta"], x, fit["mu"], fit["sd"]) if s)


def _rank(xs):
    o = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(o):
        j = i
        while j + 1 < len(o) and xs[o[j + 1]] == xs[o[i]]:
            j += 1
        for k in range(i, j + 1):
            r[o[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a, b):
    if len(a) < 5:
        return None
    ra, rb = _rank(a), _rank(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return cov / math.sqrt(va * vb) if va and vb else None


def _mt(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 2:
        return {"mean": xs[0] if xs else None, "t": None, "n": n}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return {"mean": m, "t": m / (sd / math.sqrt(n)) if sd else None, "n": n, "pos": sum(1 for x in xs if x > 0) / n}


def _quarter(d):
    return d[:4] + "Q" + str((int(d[5:7]) - 1) // 3 + 1)


def walk_forward(rows, keys, target, known, pred_on, min_train=MIN_TRAIN, cap=Y_CAP):
    """Out-of-sample predictions: [(row, prediction, baseline (training mean), actual)]."""
    blks = blocks(keys)
    data = []
    for r in rows:
        y = target(r)
        if y is None or not r.get(known) or not r.get(pred_on):
            continue
        data.append((r, vec(r, keys, blks), y, r[known], r[pred_on]))
    train = sorted(data, key=lambda d: d[3])           # by the date the outcome became known
    test = sorted(data, key=lambda d: d[4])            # by the prediction time
    S = Sums(len(keys) + len(blks))
    ti, fit, fit_q, fit_n, out = 0, None, None, 0, []
    lo, hi = (-cap, cap) if cap else (None, None)
    for r, x, y, _, when in test:
        while ti < len(train) and train[ti][3] < when:  # only outcomes known strictly before this prediction
            yy = train[ti][2]
            S.add(train[ti][1], min(hi, max(lo, yy)) if cap else yy)
            ti += 1
        if S.n < min_train:
            continue
        q = _quarter(when)
        if fit is None or q != fit_q or S.n > fit_n * 1.25:
            fit, fit_q, fit_n = S.fit(), q, S.n
        out.append((r, predict(fit, x), fit["ym"], y, when))
    return out


def score(preds, size=False):
    """The scorecard of out-of-sample predictions."""
    if len(preds) < 30:
        return {"n": len(preds)}
    p = [x[1] for x in preds]
    y = [x[3] for x in preds]
    base = [x[2] for x in preds]
    res = {"n": len(preds), "from": preds[0][4], "to": preds[-1][4]}
    ym_all = sum(y) / len(y)
    sse = sum((a - b) ** 2 for a, b in zip(y, p))
    sse0 = sum((a - b) ** 2 for a, b in zip(y, base))
    res["r2"] = 1 - sse / sse0 if sse0 else None
    res["mae"], res["mae0"] = sum(abs(a - b) for a, b in zip(y, p)) / len(y), sum(abs(a - b) for a, b in zip(y, base)) / len(y)
    res["ic"] = spearman(p, y)
    if size:
        own = [(x[0].get("ownAbs"), x[3]) for x in preds if x[0].get("ownAbs") is not None]
        res["icOwn"] = spearman([a for a, _ in own], [b for _, b in own]) if len(own) >= 30 else None
    else:
        nz = [(a, b, c) for a, b, c in zip(p, y, base) if b != 0]
        res["hit"] = sum(1 for a, b, _ in nz if (a > 0) == (b > 0)) / len(nz) if nz else None
        res["hitBase"] = sum(1 for _, b, c in nz if (c > 0) == (b > 0)) / len(nz) if nz else None
        res["up"] = sum(1 for b in y if b > 0) / len(y)
    byq = {}
    for x in preds:
        byq.setdefault(_quarter(x[4]), []).append(x)
    ics, spreads, quint = [], [], [[] for _ in range(5)]
    for q, xs in sorted(byq.items()):
        if len(xs) < 15:
            continue
        ic = spearman([x[1] for x in xs], [x[3] for x in xs])
        if ic is not None:
            ics.append(ic)
        if len(xs) >= 25:
            srt = sorted(xs, key=lambda x: x[1])
            k = len(srt) // 5
            groups = [srt[i * k:(i + 1) * k] if i < 4 else srt[4 * k:] for i in range(5)]
            means = [sum(x[3] for x in g) / len(g) for g in groups]
            spreads.append(means[4] - means[0])
            for i in range(5):
                quint[i].append(means[i])
    res["icQ"] = _mt(ics)
    res["spread"] = _mt(spreads)
    res["quintiles"] = [sum(v) / len(v) if v else None for v in quint]
    srt = sorted(zip(p, y), key=lambda t: t[0])         # calibration: deciles of the prediction
    dec = []
    for i in range(10):
        g = srt[i * len(srt) // 10:(i + 1) * len(srt) // 10]
        if g:
            dec.append({"pred": sum(a for a, _ in g) / len(g), "actual": sum(b for _, b in g) / len(g), "n": len(g)})
    res["deciles"] = dec
    res["mean"] = ym_all
    t = res["icQ"].get("t")
    good = t is not None and t >= 2 and (res["r2"] or 0) > 0 and (size or (res.get("hit") or 0) > (res.get("hitBase") or 0))
    res["verdict"] = "edge" if good else "weak" if t is not None and t >= 1 and res["icQ"]["mean"] > 0 else "none"
    return res


def evaluate(rows, prog=None, min_train=MIN_TRAIN):
    """Walk-forward scorecards for every target, and the final models (fitted on everything) for the projection."""
    out = {"minTrain": min_train, "lambda": LAMBDA, "targets": [], "final": {}}
    for key, label, kind, known, fn in TARGETS:
        if prog:
            prog(f"Walk-forward model: {label}…")
        keys = PRE if kind == "pre" else POST
        pred_on = "base" if kind == "pre" else "day0"
        preds = walk_forward(rows, keys, fn, known, pred_on, min_train, cap=Y_CAP)
        sc = score(preds, size=key == "abs1")
        blks = blocks(keys)
        data = [(vec(r, keys, blks), fn(r)) for r in rows if fn(r) is not None]
        fin = None
        if len(data) >= min_train:
            S = Sums(len(keys) + len(blks))
            for x, y in data:
                S.add(x, min(Y_CAP, max(-Y_CAP, y)))
            fin = S.fit()
            nm = names(keys, blks)
            sc["coef"] = sorted(({"key": k, "label": LABEL.get(k, k), "beta": b} for k, b in zip(nm, fin["beta"])),
                                key=lambda c: -abs(c["beta"]))[:12]
            out["final"][key] = {"keys": keys, "blocks": blks, **fin}
        out["targets"].append({"key": key, "label": label, "kind": kind, **sc})
    return out


def apply(mdl, now):
    """The final models applied to a snapshot taken before a release (pre-release targets only), with their scorecard."""
    if not mdl:
        return None
    res = []
    for t in mdl.get("targets") or []:
        fin = (mdl.get("final") or {}).get(t["key"])
        if t["kind"] != "pre" or not fin:
            continue
        x = vec(now, fin["keys"], fin["blocks"])
        res.append({"key": t["key"], "label": t["label"], "pred": predict(fin, x), "verdict": t.get("verdict"), "hit": t.get("hit"),
                    "hitBase": t.get("hitBase"), "ic": (t.get("icQ") or {}).get("mean"), "icT": (t.get("icQ") or {}).get("t"),
                    "r2": t.get("r2"), "n": t.get("n"), "mae": t.get("mae")})
    return res
