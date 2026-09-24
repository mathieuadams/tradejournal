"""Parse broker CSV exports into normalized fills.

Works with any CSV that has columns for time, symbol, side, quantity and price
(header names are matched loosely), including IBKR Flex trade exports
(signed Quantity, Buy/Sell, DateTime like 20260921;093410, IBCommission).
Times without a zone are kept as written (exchange local). Times ending in Z
or with a UTC offset are converted to US/Eastern.
"""
import csv
import io
import re
from datetime import datetime

from grouping import multiplier
from util import iso, parse_iso, sha, utc_to_ny

COLS = {
    "datetime": ["datetime", "date/time", "date time", "timestamp", "fill time", "exec time",
                 "execution time", "trade time", "transaction time", "filled at", "time placed", "time"],
    "date": ["trade date", "date", "tradedate"],
    "symbol": ["symbol", "instrument", "ticker", "contract", "underlying symbol"],
    "side": ["side", "action", "b/s", "buy/sell", "direction", "type"],
    "qty": ["qty", "quantity", "filled qty", "filledqty", "shares", "contracts", "size", "filled"],
    "price": ["price", "fill price", "avg price", "avgprice", "tradeprice", "trade price",
              "execution price", "exec price", "avg fill price"],
    "fees": ["fees", "fee", "commission", "commissions", "comm", "ibcommission", "total fees"],
    "mult": ["multiplier", "mult", "contract multiplier"],
    "account": ["account", "account id", "accountid", "account name"],
    "exec_id": ["exec id", "execution id", "execid", "tradeid", "trade id", "fill id", "id"],
}

FORMATS = [
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p", "%m/%d/%y %H:%M:%S", "%m/%d/%y %H:%M", "%m/%d/%y %I:%M:%S %p",
    "%m/%d/%y %I:%M %p", "%Y%m%d;%H%M%S", "%Y-%m-%d;%H:%M:%S", "%Y%m%d %H%M%S", "%Y%m%d %H:%M:%S",
    "%Y-%m-%d, %H:%M:%S",
]


class ParseError(Exception):
    pass


def _find(header, key, exclude=()):
    for alias in COLS[key]:
        for i, h in enumerate(header):
            if h == alias and i not in exclude:
                return i
    return -1


def parse_ts(raw):
    s = raw.strip()
    utc = False
    if s.endswith("Z"):
        s, utc = s[:-1], True
    m = re.search(r"([+-]\d{2}):?(\d{2})$", s)
    if m and ("T" in s or " " in s) and re.search(r"\d{2}:\d{2}", s[:-6]):
        from datetime import timedelta
        sign = 1 if m.group(1)[0] == "+" else -1
        off = sign * (int(m.group(1)[1:]) * 60 + int(m.group(2)))
        s = s[: m.start()]
        dt = _parse_plain(s)
        return iso(utc_to_ny(dt - timedelta(minutes=off)))
    s = re.sub(r"\s+(ET|EST|EDT|CT|CST|CDT|UTC|GMT)$", "", s)
    dt = _parse_plain(s)
    return iso(utc_to_ny(dt) if utc else dt)


def _parse_plain(s):
    s = s.strip()
    for f in FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    raise ValueError(s)


def _num(raw):
    s = (raw or "").strip().replace("$", "").replace(",", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    v = float(s)
    return -v if neg else v


def _side(raw, qty):
    s = (raw or "").strip().lower()
    if s.startswith("b") or s in ("bot", "long", "buy to open", "buy to close"):
        return "buy"
    if s.startswith("s") or s in ("sld", "short"):
        return "sell"
    if qty is not None and qty != 0:
        return "buy" if qty > 0 else "sell"
    return None


TZ_SHIFT = {"ET": 0, "CT": 1, "MT": 2, "PT": 3}  # hours to add to get US/Eastern


def _shift(ts, hours):
    if not hours:
        return ts
    from datetime import timedelta
    return iso(parse_iso(ts) + timedelta(hours=hours))


def parse_csv(text, default_account, tz="auto"):
    text = text.lstrip("\ufeff")
    if text.startswith("Account Statement for") or "\nAccount Trade History" in text:
        return parse_tos_statement(text, default_account, tz)
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ParseError("The file has a header but no data rows.")
    header = [h.strip().lower() for h in rows[0]]

    c = {k: _find(header, k) for k in COLS}
    # A separate date column plus a time-only column get combined.
    if c["datetime"] >= 0 and header[c["datetime"]] == "time" and c["date"] >= 0:
        combine = True
    else:
        combine = False
        if c["datetime"] < 0 and c["date"] >= 0:
            c["datetime"], c["date"] = c["date"], -1
    missing = [k for k in ("datetime", "symbol", "qty", "price") if c[k] < 0]
    if missing:
        names = {"datetime": "time", "symbol": "symbol", "qty": "quantity", "price": "price"}
        raise ParseError("Missing column: " + ", ".join(names[m] for m in missing)
                         + ". Found: " + ", ".join(rows[0]) + ".")

    fills, skipped, seen = [], 0, {}
    for n, r in enumerate(rows[1:], start=2):
        r = r + [""] * (len(header) - len(r))
        try:
            sym = r[c["symbol"]].strip().upper()
            qty = _num(r[c["qty"]])
            price = _num(r[c["price"]])
            if not sym or qty is None or price is None or qty == 0:
                skipped += 1
                continue
            tsraw = r[c["datetime"]]
            if combine:
                tsraw = r[c["date"]].strip() + " " + tsraw.strip()
            ts = parse_ts(tsraw)
            side = _side(r[c["side"]] if c["side"] >= 0 else "", qty)
            if side is None:
                raise ParseError(f"Row {n}: cannot tell whether this is a buy or a sell.")
            fees = abs(_num(r[c["fees"]]) or 0) if c["fees"] >= 0 else 0.0
            mult = _num(r[c["mult"]]) if c["mult"] >= 0 else None
            acct = (r[c["account"]].strip() if c["account"] >= 0 else "") or default_account
            exec_id = r[c["exec_id"]].strip() if c["exec_id"] >= 0 else ""
        except ValueError:
            raise ParseError(f"Row {n} has a time, quantity or price that can't be read.")
        qty = abs(qty)
        base = exec_id or f"{acct}|{ts}|{sym}|{side}|{qty}|{price}"
        seen[base] = seen.get(base, 0) + 1
        fid = sha(f"{base}#{seen[base]}" if not exec_id else base)
        fills.append({
            "id": fid, "acct": acct[:60], "ts": ts, "sym": sym, "side": side, "qty": qty,
            "price": price, "fees": round(fees, 4), "mult": multiplier(sym, mult),
        })
    if not fills:
        raise ParseError("No fills could be read from the file.")
    shift = TZ_SHIFT.get(tz, 0)
    for f in fills:
        f["ts"] = _shift(f["ts"], shift)
    fills.sort(key=lambda f: (f["ts"], f["id"]))
    return fills, skipped


# ---------- Schwab / thinkorswim "Account Statement" export ----------

MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _section(lines, title):
    """Rows of a titled section: the line after the title is the header, rows run until a blank line."""
    for i, line in enumerate(lines):
        if line.strip().strip('"') == title:
            out = []
            for row in lines[i + 1:]:
                if not row.strip():
                    break
                out.append(row)
            if out:
                return list(csv.reader(io.StringIO("\n".join(out))))
    return None


def _occ(root, exp, strike, kind):
    d, mon, yy = exp.split()
    strike_i = round(float(strike) * 1000)
    return f"{root}{int(yy):02d}{MONTHS[mon[:3].upper()]:02d}{int(d):02d}{kind[0].upper()}{strike_i:08d}"


def detect_shift(times):
    """Pick the US time zone that puts the most executions inside 9:30-16:00 Eastern."""
    best, best_n = 0, -1
    for hours in (0, 1, 2, 3):
        n = 0
        for t in times:
            m = (t.hour + hours) * 60 + t.minute
            n += 570 <= m <= 960
        if n > best_n:
            best, best_n = hours, n
    return best


def parse_tos_statement(text, default_account, tz="auto"):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    trades = _section(lines, "Account Trade History")
    if not trades:
        raise ParseError("This Schwab / thinkorswim statement has no Account Trade History section. "
                         "Export it with trade history included.")
    header = [h.strip().lower() for h in trades[0]]
    col = {name: header.index(name) if name in header else -1
           for name in ("exec time", "spread", "side", "qty", "pos effect", "symbol", "exp", "strike", "type", "price")}
    if min(col[k] for k in ("exec time", "side", "qty", "symbol", "price")) < 0:
        raise ParseError("The Account Trade History section is missing expected columns.")

    # Fees live in the Cash Balance section (TRD rows), keyed by execution time.
    fees_at = {}
    cash = _section(lines, "Cash Balance") or []
    if cash:
        ch = [h.strip().lower() for h in cash[0]]
        ci = {k: ch.index(k) if k in ch else -1 for k in ("date", "time", "type", "misc fees", "commissions & fees")}
        for r in cash[1:]:
            r = r + [""] * (len(ch) - len(r))
            if ci["type"] < 0 or r[ci["type"]].strip() != "TRD":
                continue
            try:
                key = iso(_parse_plain(r[ci["date"]].strip() + " " + r[ci["time"]].strip()))
            except ValueError:
                continue
            fee = sum(abs(_num(r[ci[k]]) or 0) for k in ("misc fees", "commissions & fees") if ci[k] >= 0)
            fees_at[key] = fees_at.get(key, 0.0) + fee

    rows, skipped, last_time = [], 0, ""
    for n, r in enumerate(trades[1:], start=2):
        r = r + [""] * (len(header) - len(r))
        t = r[col["exec time"]].strip() or last_time   # later legs of a spread leave the time blank
        last_time = t
        try:
            qty = _num(r[col["qty"]])
            price = _num(r[col["price"]])
            ts = iso(_parse_plain(t))
        except ValueError:
            skipped += 1
            continue
        sym = r[col["symbol"]].strip().upper()
        if not sym or not qty or price is None:
            skipped += 1
            continue
        kind = r[col["type"]].strip().upper() if col["type"] >= 0 else ""
        mult = None
        if kind in ("CALL", "PUT") and col["exp"] >= 0 and r[col["exp"]].strip():
            try:
                sym = _occ(sym, r[col["exp"]].strip(), r[col["strike"]], kind)
                mult = 100.0
            except (ValueError, KeyError):
                skipped += 1
                continue
        side = "buy" if r[col["side"]].strip().upper().startswith("B") else "sell"
        eff = r[col["pos effect"]].strip().upper() if col["pos effect"] >= 0 else ""
        rows.append({"ts": ts, "sym": sym, "side": side, "qty": abs(qty), "price": price, "mult": mult,
                     "effect": "close" if "CLOSE" in eff else "open" if "OPEN" in eff else ""})
    if not rows:
        raise ParseError("No trades could be read from the Account Trade History section.")

    # Split each timestamp's fees across its fills by quantity.
    qty_at = {}
    for f in rows:
        qty_at[f["ts"]] = qty_at.get(f["ts"], 0) + f["qty"]
    if tz == "auto":
        shift = detect_shift([parse_iso(f["ts"]) for f in rows])
    else:
        shift = TZ_SHIFT.get(tz, 0)

    fills, seen = [], {}
    for f in rows:
        fee = fees_at.get(f["ts"], 0.0) * f["qty"] / qty_at[f["ts"]]
        base = f"{default_account}|{f['ts']}|{f['sym']}|{f['side']}|{f['qty']}|{f['price']}"
        seen[base] = seen.get(base, 0) + 1
        fills.append({
            "id": sha(f"{base}#{seen[base]}"), "acct": default_account[:60], "ts": _shift(f["ts"], shift),
            "sym": f["sym"], "side": f["side"], "qty": f["qty"], "price": f["price"], "fees": round(fee, 4),
            "mult": multiplier(f["sym"], f["mult"]), "effect": f["effect"],
        })
    fills.sort(key=lambda f: (f["ts"], f["id"]))
    return fills, skipped
