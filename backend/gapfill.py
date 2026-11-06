"""Regular-hours gap fill: fade a small opening gap back to yesterday's close (the small-gap version of onrev).

  A gap between `minPct` and `maxPct`% (default 0.1-0.5%) at the 09:30 open: fade it at the open, target yesterday's
  16:00 close (`fill`), stop `stopGap` x the gap past the open, out at `exitAt`."""
import onrev

SETUP = "gapfill"
DEFAULT = {**onrev.DEFAULT, "minPct": 0.1, "maxPct": 0.5, "fill": 1.0, "stopGap": 1.5, "exitAt": "13:00"}
FACTORS = onrev.FACTORS


def clean(p):
    return onrev.clean(p, SETUP, DEFAULT)


describe, tickers, engine, decorate, overlay = onrev.describe, onrev.tickers, onrev.engine, onrev.decorate, onrev.overlay


def all_trades(sub, p, cells=None, prog=None):
    return onrev.all_trades(sub, p, cells, prog)
