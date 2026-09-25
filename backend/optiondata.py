"""Option contract volume from Alpaca's options market data (needs Alpaca keys; history starts Feb 2024)."""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from util import parse_iso

URL = "https://data.alpaca.markets/v1beta1/options/bars"


def daily_volume(creds, occ, entry_date):
    d = parse_iso(entry_date + "T00:00:00")
    q = urllib.parse.urlencode({"symbols": occ, "timeframe": "1Day", "start": (d - timedelta(days=14)).strftime("%Y-%m-%d"),
                                "end": (d + timedelta(days=1)).strftime("%Y-%m-%d"), "limit": 100})
    req = urllib.request.Request(f"{URL}?{q}", headers={"APCA-API-KEY-ID": creds["key"], "APCA-API-SECRET-KEY": creds["secret"]})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None
    bars = (data.get("bars") or {}).get(occ) or []
    day = next((b for b in bars if b["t"][:10] == entry_date), None)
    before = [b["v"] for b in bars if b["t"][:10] < entry_date][-5:]
    if not day:
        return None
    avg5 = sum(before) / len(before) if before else None
    return {"optVolume": day["v"], "optTrades": day.get("n"), "optAvgVolume5": round(avg5, 1) if avg5 else None,
            "optVolRatio": round(day["v"] / avg5, 2) if avg5 else None}
