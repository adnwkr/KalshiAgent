"""Look up Kalshi's last quote before an event started, for closing line value (CLV).

The relay snapshots every 2 hours, but GitHub often skips scheduled runs, so the last snapshot
before kickoff or the first tee time can be hours old. This script asks Kalshi's public
candlestick endpoint for the minute-by-minute bid and ask instead, and records the last quote
at or before each start time.

Usage (run by the relay workflow when its `closes` input is set):
    python common/kalshi_close.py "KXNFLSPREAD-26NOV08CINATL-CIN10@2026-11-08T14:30:00Z,TICKER@ISO,..."

Writes closes/closes.json in the repo root, one entry per ticker (existing entries are kept):
    {"KX...": {"start": iso, "as_of": iso of the quote, "yes_bid": c, "yes_ask": c, "mid": c,
               "last_price": c or null, "minutes_before_start": n, "source": "candlesticks"}}
Prices are in cents. A ticker with no quote in the 2 days before its start is recorded with
"error" so the grading run can fall back to the relay's history file and say so.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://api.elections.kalshi.com/trade-api/v2"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "closes", "closes.json")


def get(path, **params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "kalshi-relay"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404) or attempt == 3:
                raise
        except Exception:
            if attempt == 3:
                raise
        time.sleep(2 * (attempt + 1))


def cents(d, key="close"):
    """Candle fields come as integer cents ("close") or dollar strings ("close_dollars")."""
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if v is None and d.get(f"{key}_dollars") is not None:
        try:
            v = float(d[f"{key}_dollars"]) * 100
        except (TypeError, ValueError):
            v = None
    return None if v is None else round(float(v), 2)


def candles(ticker, start_ts, end_ts, interval):
    series = ticker.split("-", 1)[0]
    params = dict(start_ts=start_ts, end_ts=end_ts, period_interval=interval)
    try:
        return get(f"/series/{series}/markets/{ticker}/candlesticks", **params).get("candlesticks", [])
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # settled markets may have moved to the historical partition
        return get(f"/historical/markets/{ticker}/candlesticks", **params).get("candlesticks", [])


def close_for(ticker, start_iso):
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    t = int(start.timestamp())
    # minute candles for the last 6 hours, then hourly candles for 2 days if the market was quiet
    for span, interval in ((6 * 3600, 1), (48 * 3600, 60)):
        rows = [c for c in candles(ticker, t - span, t, interval) if c.get("end_period_ts", 0) <= t]
        for c in sorted(rows, key=lambda c: c["end_period_ts"], reverse=True):
            bid, ask = cents(c.get("yes_bid")), cents(c.get("yes_ask"))
            if bid is None or ask is None or ask <= 0:
                continue
            as_of = datetime.fromtimestamp(c["end_period_ts"], tz=timezone.utc)
            return {"start": start_iso, "as_of": as_of.isoformat().replace("+00:00", "Z"),
                    "yes_bid": bid, "yes_ask": ask, "mid": round((bid + ask) / 2, 2),
                    "last_price": cents(c.get("price")),
                    "minutes_before_start": round((t - c["end_period_ts"]) / 60),
                    "source": f"candlesticks ({interval}m)"}
    return {"start": start_iso, "error": "no quote in the 2 days before start"}


def main(arg):
    items = [x.strip() for x in arg.replace("\n", ",").split(",") if x.strip()]
    out = json.load(open(OUT)) if os.path.exists(OUT) else {}
    for item in items:
        if "@" not in item:
            print(f"skip {item!r}: expected TICKER@ISO-start-time")
            continue
        ticker, start_iso = item.split("@", 1)
        try:
            out[ticker] = close_for(ticker, start_iso)
        except Exception as e:  # keep going; the grading run falls back to the relay history
            out[ticker] = {"start": start_iso, "error": f"{type(e).__name__}: {e}"[:200]}
        print(ticker, out[ticker])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(dict(sorted(out.items())), open(OUT, "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CLOSES", ""))
