"""Which earnings universe a request or job works on: "spy" (the S&P 500, benchmark SPY — the original one, kept at
the original storage keys) or "qqq" (the Nasdaq-100, benchmark QQQ). Each has its own settings, event study, model,
backtest, optimizer and real-option results; company filings and price files are shared."""
SPACES = {
    "spy": {"label": "SPY universe", "index": "sp500", "indexName": "S&P 500", "bench": "SPY"},
    "qqq": {"label": "QQQ universe", "index": "nasdaq100", "indexName": "Nasdaq-100", "bench": "QQQ"},
}
_cur = "spy"


def set(s):                                            # noqa: A001 — per request / per job
    global _cur
    _cur = s if s in SPACES else "spy"
    return _cur


def get():
    return _cur


def info():
    return {"id": _cur, **SPACES[_cur]}


def pre(sub):
    """The storage prefix of this universe's per-user files."""
    return f"fund/u/{sub}" if _cur == "spy" else f"fund/u/{sub}/{_cur}"


def sk(base):
    """A DynamoDB sort key for this universe."""
    return base if _cur == "spy" else f"{base}#{_cur}"


def job(name):
    """The job slot: the two universes can run at the same time."""
    return name if _cur == "spy" or name != "fund" else f"fund-{_cur}"
