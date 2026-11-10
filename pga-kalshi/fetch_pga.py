"""
Pull PGA Tour markets from Kalshi's public API (no auth needed for market data), plus
sportsbook winner odds from The Odds API during major weeks, and write snapshots the
betting agent reads from GitHub.

Golf is a field, not a matchup: each Kalshi event is one tournament and each market is one
golfer ("Will X win / finish top 10 / make the cut"). Series pulled are in TARGETS.

Outputs (pga-kalshi/data/):
  series.json   every Kalshi series starting with KXPGA or KXGOLF (for discovery)
  latest.json   every open market in the target series
  lines.json    one row per tournament, one entry per golfer: mid price for each market type,
                plus win probability normalized so the field sums to 100%
  history/<year>.csv   prices for golfers at 3c or more, for closing line value
  odds.json     (majors only, needs ODDS_API_KEY) each book's winner price per golfer, no-vig
                probability, median across books, best US-licensed price, and the Kalshi mid
  odds_history/<year>.csv  every book's winner price at each pull

Usage:
  python fetch_pga.py                 Kalshi only (every workflow run)
  python fetch_pga.py --odds-only     sportsbook odds only, matched against the lines.json already
                                      on disk: checks The Odds API (free event lookup) and pulls
                                      winner odds if a major starts within 7 days or is in progress
                                      (1 credit per major per pull)
  add --force to pull winner odds for every listed major right now
"""
import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://api.elections.kalshi.com/trade-api/v2"
ODDS = "https://api.the-odds-api.com/v4/sports"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common"))
from kalshi_common import norm_name  # noqa: E402
UA = {"Accept": "application/json", "User-Agent": "kalshi-relay"}

# Kalshi series -> market type. Other golf series are listed in series.json but not pulled.
TARGETS = {"KXPGATOUR": "win", "KXPGATOP5": "top5", "KXPGATOP10": "top10", "KXPGATOP20": "top20",
           "KXPGAMAKECUT": "make_cut", "KXPGAR1LEAD": "r1_lead"}
# The Odds API only covers the four majors, winner market only.
MAJORS = ["golf_masters_tournament_winner", "golf_pga_championship_winner", "golf_us_open_winner",
          "golf_the_open_championship_winner"]
BOOKS = "draftkings,fanduel,betmgm,williamhill_us,espnbet,fanatics,betrivers,bovada,lowvig,pinnacle"
LICENSED = {"draftkings", "fanduel", "betmgm", "williamhill_us", "espnbet", "fanatics", "betrivers"}


# ---------- Kalshi ----------

def get(path, **params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}" if params else f"{BASE}{path}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


def paged(path, key, page=200, **params):
    out, cursor = [], None
    while True:
        p = dict(params, limit=page)
        if cursor:
            p["cursor"] = cursor
        d = get(path, **p)
        out += d.get(key, [])
        cursor = d.get("cursor")
        if not cursor:
            return out


def cents(m, name):
    v = m.get(name)
    if v is None and m.get(f"{name}_dollars") is not None:
        try:
            v = round(float(m[f"{name}_dollars"]) * 100, 2)
        except ValueError:
            v = None
    return v


def mid(m):
    """Mid of the yes bid and ask. An empty book shows bid 0 / ask 100, which is no price, not 50c,
    so return None for that and for quotes wider than 30c. A long shot with no bid and an ask of 3c
    or less is priced at half the ask. Last trade is used only when no quote fields exist at all."""
    b, a, last = cents(m, "yes_bid"), cents(m, "yes_ask"), cents(m, "last_price")
    if b is None and a is None:
        return last
    if b is not None and a is not None and 0 < b <= a < 100:
        return (b + a) / 2 if a - b <= 30 else None
    if (b is None or b <= 0) and a is not None and 0 < a <= 3:
        return a / 2
    return None




def golfer(m):
    return (m.get("yes_sub_title") or m.get("subtitle") or m["ticker"].rsplit("-", 1)[-1]).strip()


def summarize(markets):
    """One row per tournament. The series share an event suffix (KXPGATOUR-BAC26 and
    KXPGATOP10-BAC26 are the same tournament), so rows are keyed on that suffix."""
    events = {}
    for m in markets:
        kind = TARGETS.get(m.get("series_ticker"))
        if kind is None or "-" not in m.get("event_ticker", ""):
            continue
        key = m["event_ticker"].split("-", 1)[1]
        ev = events.setdefault(key, {"title": None, "close_time": None, "golfers": {}})
        if kind == "win" or not ev["title"]:
            ev["title"] = m.get("event_title") or ev["title"]
        if kind == "win":
            ev["close_time"] = m.get("close_time")
        name = golfer(m)
        # Key on the ticker's golfer code (e.g. JSMI): Kalshi sometimes spells a name differently
        # across series ("Jordan L. Smith" vs "Jordan Smith"), which used to split one golfer in two.
        code = m["ticker"].rsplit("-", 1)[-1]
        g = ev["golfers"].setdefault(code, {"name": name})
        if kind == "win":
            g["name"] = name
        g[kind] = {"mid": mid(m), "yes_bid": cents(m, "yes_bid"), "yes_ask": cents(m, "yes_ask"),
                   "last": cents(m, "last_price"), "volume": m.get("volume"), "ticker": m["ticker"]}

    out = []
    for key, ev in events.items():
        field = list(ev["golfers"].values())
        tot = sum(g["win"]["mid"] for g in field if "win" in g and g["win"]["mid"])
        for g in field:
            w = g.get("win", {}).get("mid")
            g["win_prob_norm"] = round(w / tot, 4) if w and tot else None
        field.sort(key=lambda g: -(g.get("win", {}).get("mid") or 0))
        out.append({"event_key": key, "title": ev["title"], "close_time": ev["close_time"],
                    "win_overround": round(tot, 1) if tot else None,
                    "markets": sorted({k for g in field for k in g if k in TARGETS.values()}),
                    "n_golfers": len(field), "golfers": field})
    return sorted(out, key=lambda r: (r["close_time"] or "", r["event_key"]))


def kalshi(now):
    os.makedirs(os.path.join(OUT, "history"), exist_ok=True)
    all_series = get("/series").get("series", [])
    golf = [s for s in all_series if str(s.get("ticker", "")).upper().startswith(("KXPGA", "KXGOLF"))]
    json.dump({"fetched_at": now, "series": [{k: s.get(k) for k in ("ticker", "title", "category", "frequency")}
                                             for s in golf]},
              open(os.path.join(OUT, "series.json"), "w"), indent=1)
    found = {s["ticker"] for s in golf}
    targets = [t for t in TARGETS if t in found]
    missing = [t for t in TARGETS if t not in found]
    if missing:
        print("WARNING: series not found on Kalshi:", missing)

    markets = []
    for t in targets:
        for ev in paged("/events", "events", series_ticker=t, status="open", with_nested_markets="true"):
            for m in ev.get("markets", []):
                m["event_title"] = ev.get("title")
                m["series_ticker"] = t
                markets.append(m)
    keep = ("ticker", "event_ticker", "series_ticker", "event_title", "title", "subtitle", "yes_sub_title",
            "status", "open_time", "close_time", "expected_expiration_time", "yes_bid", "yes_ask",
            "no_bid", "no_ask", "last_price", "yes_bid_dollars", "yes_ask_dollars", "last_price_dollars",
            "volume", "volume_24h", "open_interest", "liquidity")
    slim = [{k: m.get(k) for k in keep if m.get(k) is not None} for m in markets]
    json.dump({"fetched_at": now, "series_pulled": targets, "markets": slim},
              open(os.path.join(OUT, "latest.json"), "w"), indent=1)
    lines = summarize(slim)
    json.dump({"fetched_at": now, "tournaments": lines}, open(os.path.join(OUT, "lines.json"), "w"), indent=1)

    hist = os.path.join(OUT, "history", f"{now[:4]}.csv")
    new = not os.path.exists(hist)
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["fetched_at", "ticker", "event_ticker", "golfer", "yes_bid", "yes_ask", "last_price", "volume"])
        for m in slim:
            p = mid(m)
            if p is not None and p >= 3 and TARGETS.get(m["series_ticker"]) in ("win", "top5", "top10", "top20"):
                w.writerow([now, m["ticker"], m.get("event_ticker"), golfer(m), cents(m, "yes_bid"),
                            cents(m, "yes_ask"), cents(m, "last_price"), m.get("volume")])
    print(f"{len(golf)} golf series, pulled {targets}, {len(slim)} open markets, {len(lines)} tournaments")
    return lines


# ---------- The Odds API (majors only) ----------

def odds_get(path, **params):
    req = urllib.request.Request(f"{ODDS}{path}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": "kalshi-relay"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r), r.headers.get("x-requests-used"), r.headers.get("x-requests-remaining")


def implied(american):
    a = float(american)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def odds(now, kalshi_lines, force):
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        print("odds pga: ODDS_API_KEY not set, skipping")
        return 0
    t = datetime.now(timezone.utc)
    due, used, remaining = [], None, None
    try:
        for sport in MAJORS:
            # the events endpoint lists the tournament and its start time without spending credits
            evs, used, remaining = odds_get(f"/{sport}/events", apiKey=key, dateFormat="iso")
            for e in evs:
                start = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
                if force or (t - timedelta(days=4) <= start <= t + timedelta(days=7)):
                    due.append(sport)
                    break
        if not due:
            print(f"odds pga: no major within 7 days, nothing pulled (credits remaining {remaining})")
            return 0
        pulled = []
        for sport in due:
            games, used, remaining = odds_get(f"/{sport}/odds", apiKey=key, markets="outrights",
                                              oddsFormat="american", bookmakers=BOOKS, dateFormat="iso")
            pulled += [dict(g, sport_key=sport) for g in games]
    except urllib.error.HTTPError as e:  # never print the URL: it contains the API key
        print(f"odds pga: HTTP {e.code} {e.reason} (401 = bad key, 429 = out of credits)")
        return 1
    except Exception as e:
        print(f"odds pga: request failed ({type(e).__name__})")
        return 1

    # Kalshi winner mids by golfer, to set beside the books for the same major
    kmid = {}
    for ev in kalshi_lines:
        for g in ev["golfers"]:
            if g.get("win", {}).get("mid") is not None:
                kmid.setdefault(norm_name(g["name"]), []).append(
                    {"event_key": ev["event_key"], "title": ev["title"], "mid": g["win"]["mid"],
                     "win_prob_norm": g["win_prob_norm"]})

    summary = []
    for g in pulled:
        per = {}  # golfer -> book -> price
        for b in g.get("bookmakers", []):
            for m in b.get("markets", []):
                if m["key"] != "outrights":
                    continue
                outs = m.get("outcomes", [])
                field = sum(implied(o["price"]) for o in outs) or None
                for o in outs:
                    per.setdefault(o["name"], {})[b["key"]] = {
                        "price": o["price"], "novig": round(implied(o["price"]) / field, 4) if field else None}
        rows = []
        for name, books in per.items():
            bet = [(v["price"], k) for k, v in books.items() if k in LICENSED]
            best = max(bet, key=lambda c: c[0]) if bet else None
            nv = [v["novig"] for v in books.values() if v["novig"] is not None]
            rows.append({"name": name, "median_novig": round(statistics.median(nv), 4) if nv else None,
                         "best": {"price": best[0], "book": best[1]} if best else None,
                         "pinnacle": books.get("pinnacle", {}).get("price"),
                         "kalshi": kmid.get(norm_name(name)), "books": books})
        rows.sort(key=lambda r: -(r["median_novig"] or 0))
        summary.append({"sport_key": g["sport_key"], "odds_api_id": g["id"], "title": g.get("sport_title"),
                        "commence_time": g["commence_time"], "n_books": len(g.get("bookmakers", [])),
                        "golfers": rows})

    os.makedirs(os.path.join(OUT, "odds_history"), exist_ok=True)
    json.dump({"fetched_at": now, "credits_used": used, "credits_remaining": remaining, "tournaments": summary},
              open(os.path.join(OUT, "odds.json"), "w"), indent=1)
    hist = os.path.join(OUT, "odds_history", f"{now[:4]}.csv")
    new = not os.path.exists(hist)
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["fetched_at", "sport_key", "commence_time", "golfer", "book", "price"])
        for ev in summary:
            for r in ev["golfers"]:
                for k, v in r["books"].items():
                    w.writerow([now, ev["sport_key"], ev["commence_time"], r["name"], k, v["price"]])
    print(f"odds pga: pulled {due}, credits used {used}, remaining {remaining}")
    return 0


def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if "--odds-only" in sys.argv:
        try:
            lines = json.load(open(os.path.join(OUT, "lines.json")))["tournaments"]
        except (OSError, ValueError, KeyError):
            lines = []
        return odds(now, lines, force="--force" in sys.argv)
    kalshi(now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
