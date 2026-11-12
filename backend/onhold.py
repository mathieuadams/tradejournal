"""Overnight half hold: the 09:30 open's half of the overnight range decides the side, no target: held to the out-at
time (or the stop).

  Overnight = 18:00 (the day before) to 09:30 ET; mid = (overnight high + overnight low) / 2. Open (the close of the 09:25
  candle) in the top half: buy; in the bottom half: sell. Opens outside the overnight range take no trade. Stop at the
  midpoint (or `stop` opp / part, as onmid).
  No profit target: out at `exitAt` (10:00 ... 15:55, optimised) unless stopped. The engine is onmid's (hold "never")."""
import onmid

SETUP = "onhold"
DEFAULT = {**onmid.DEFAULT, "hold": "never", "exitAt": "15:55", "stop": "mid"}
FACTORS = [f for f in onmid.FACTORS if f[0] != "held"] + [("held", "Reached the overnight extreme")]


def clean(p):
    out = onmid.clean(p, SETUP, DEFAULT)
    out["hold"] = "never"
    return out


def describe(p):
    st = {"mid": "stop at the midpoint", "opp": "stop at the other overnight extreme",
          "part": f"stop {p['stopPart']:g}x the overnight range from the open"}[p["stop"]]
    return (f"{', '.join(p['tickers'])}: the 09:30 open in the top half of the overnight range (18:00-09:30): buy; in the bottom half: sell; "
            f"{st}; no profit target: out at {p['exitAt']}"
            + (f", no trade within {p['deadPct']:g}% of the range around the mid" if p["deadPct"] else "")
            + ("" if p["exit"] == "target" else onmid.N.ifvg._exit_txt(p)))


tickers, engine, all_trades, overlay = onmid.tickers, onmid.engine, onmid.all_trades, onmid.overlay


def decorate(x, p):
    onmid.decorate(x, p)
    x["held"] = "yes" if x["held"].startswith("yes") else "no"
