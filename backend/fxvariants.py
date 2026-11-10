"""Three strategies built on the Fractal engine (fxstrat.py), each with its own setup, defaults and optimizer search:

  smfade    Spread Monster fade: the day's daily-level wall / outlier touched and rejected intraday.
  oaride    Wave ride + Option Analyzer exit: go with a new wave (pivot or vector cross), out when the wave has run
            longer than most past waves did.
  latewave  Late counter-wave: a wave against the higher timeframe runs N candles, then turns: go with the higher
            timeframe.
Each is fxstrat with its entry fixed (the other entries' settings don't apply)."""
import types

import fxstrat

VARIANTS = {
    "smfade": {"name": "Spread Monster fade (daily levels)", "entry": ("smfade",),
               "defaults": {"entry": "smfade", "tf": 5, "macro": "none", "smLevel": "wall", "smTgt": "median", "bufTicks": 4, "maxDay": 2}},
    "oaride": {"name": "Wave ride + Option Analyzer exit", "entry": ("cross", "vector"),
               "defaults": {"entry": "cross", "tf": 15, "macro": "with", "oaExit": 0.3, "tgt": "r", "tgtR": 0}},
    "latewave": {"name": "Late counter-wave (higher-timeframe bias)", "entry": ("late",),
                 "defaults": {"entry": "late", "tf": 15, "macro": "none", "htf": "D", "htfRule": "side", "lateN": 3, "lateStopP": 0.1}},
}


def _make(key, v):
    def clean(p):
        q = {**fxstrat.DEFAULT, **v["defaults"], **(p or {})}
        if q.get("entry") not in v["entry"]:
            q["entry"] = v["entry"][0]
        out = fxstrat.clean(q)
        out["setup"] = key
        if key == "oaride" and not out["oaExit"]:
            out["oaExit"] = 0.3                              # this strategy is about that exit
        return out

    m = types.SimpleNamespace(
        DEFAULT={**fxstrat.DEFAULT, **v["defaults"]}, FACTORS=fxstrat.FACTORS, clean=clean, describe=fxstrat.describe,
        tickers=fxstrat.tickers, all_trades=fxstrat.all_trades, decorate=fxstrat.decorate, overlay=fxstrat.overlay)
    return m


MODULES = {k: _make(k, v) for k, v in VARIANTS.items()}
NAMES = {k: v["name"] for k, v in VARIANTS.items()}
