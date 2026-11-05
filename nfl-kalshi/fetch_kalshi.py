"""
Pull NFL or NBA markets from Kalshi's public API (no auth needed for market data) and
write snapshots the betting agent reads from GitHub.

Usage: python fetch_kalshi.py [nfl|nba]   (default nfl)

Outputs (in nfl-kalshi/data/ or nba-kalshi/data/):
  series.json   every Kalshi series whose ticker starts with KXNFL / KXNBA (for discovery)
  latest.json   all open markets in the game-level series, full detail
  lines.json    per-game summary: implied win prob, market-implied spread and total
  history/<season>.csv  compact price history for near-the-money markets (for CLV)
"""
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from statistics import NormalDist

BASE = "https://api.elections.kalshi.com/trade-api/v2"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Full-game series per league (quarter/half/prop series are listed in series.json but not pulled).
# sigma: SD of final margin, used only to back a spread out of the win price near pick'em.
# codes: Kalshi team codes that differ from the codes the agent uses (nflverse for NFL).
LEAGUES = {
    "nfl": {"prefix": "KXNFL", "sigma": 13.5,
            "targets": {"KXNFLGAME": "win", "KXNFLSPREAD": "spread", "KXNFLTOTAL": "total"},
            "codes": {"JAC": "JAX", "LAR": "LA", "WSH": "WAS"}},
    "nba": {"prefix": "KXNBA", "sigma": 12.0,
            "targets": {"KXNBAGAME": "win", "KXNBASPREAD": "spread", "KXNBATOTAL": "total"},
            "codes": {"BRK": "BKN", "PHO": "PHX", "CHO": "CHA", "GS": "GSW", "NO": "NOP", "NY": "NYK",
                      "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}},
}
LEAGUE = (sys.argv[1] if len(sys.argv) > 1 else "nfl").lower()
if LEAGUE not in LEAGUES:
    sys.exit(f"unknown league {LEAGUE!r}; use one of {sorted(LEAGUES)}")
CFG = LEAGUES[LEAGUE]
TARGETS = CFG["targets"]
OUT = os.path.join(REPO, f"{LEAGUE}-kalshi", "data")


def get(path, **params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}" if params else f"{BASE}{path}"
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "kalshi-relay"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # rate limit or transient error
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
    """Kalshi has used both integer-cent fields (yes_bid) and dollar strings (yes_bid_dollars)."""
    v = m.get(name)
    if v is None and m.get(f"{name}_dollars") is not None:
        try:
            v = round(float(m[f"{name}_dollars"]) * 100, 2)
        except ValueError:
            v = None
    return v


def mid(m):
    b, a, last = cents(m, "yes_bid"), cents(m, "yes_ask"), cents(m, "last_price")
    if b is not None and a is not None and a > 0 and a >= b:
        return (b + a) / 2
    return last


def strike(m):
    for k in ("floor_strike", "cap_strike", "functional_strike"):
        if m.get(k) not in (None, ""):
            try:
                return float(m[k])
            except (TypeError, ValueError):
                pass
    nums = re.findall(r"(\d+(?:\.\d+)?)", m.get("yes_sub_title") or m.get("subtitle") or "")
    return float(nums[-1]) if nums else None


def implied_line(points):
    """points: [(strike, p_yes)] where yes = 'over strike'. Return strike where p = 0.5."""
    pts = sorted((s, p) for s, p in points if s is not None and p is not None)
    for (s1, p1), (s2, p2) in zip(pts, pts[1:]):
        if (p1 - 0.5) * (p2 - 0.5) <= 0 and p1 != p2:
            return round(s1 + (0.5 - p1) * (s2 - s1) / (p2 - p1), 1)
    return None


MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def nv(code):
    return CFG["codes"].get(code, code)


def rung(m):
    return {"strike": strike(m), "yes_bid": cents(m, "yes_bid"), "yes_ask": cents(m, "yes_ask"),
            "last": cents(m, "last_price"), "ticker": m["ticker"]}


def summarize(markets):
    """One row per game. Kalshi lists winner, spread and total as separate events that share a
    key like 26NOV01ARIDAL (date + away code + home code)."""
    games = {}
    for m in markets:
        kind = TARGETS.get(m.get("series_ticker"))
        if kind is None or "-" not in m.get("event_ticker", ""):
            continue
        key = m["event_ticker"].split("-", 1)[1]
        g = games.setdefault(key, {"win": {}, "total": [], "spread": {}, "close_time": m.get("close_time"),
                                   "expected_expiration_time": m.get("expected_expiration_time")})
        suffix = m["ticker"].rsplit("-", 1)[-1]
        if kind == "win":
            g["win"][suffix] = m
        elif kind == "total":
            g["total"].append(m)
        elif kind == "spread":
            team = re.match(r"[A-Z]+", suffix).group(0) if re.match(r"[A-Z]+", suffix) else suffix
            g["spread"].setdefault(team, []).append(m)

    out = []
    for key, g in games.items():
        mt = re.match(r"(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$", key)
        if not mt:
            continue
        yy, mon, dd, teams = mt.groups()
        codes = sorted(set(g["win"]) | set(g["spread"]), key=len, reverse=True)
        away = next((c for c in codes if teams.startswith(c) and teams[len(c):] in codes), None)
        home = teams[len(away):] if away else None
        if not away:  # fall back to splitting on any known code
            for c in codes:
                if teams.startswith(c):
                    away, home = c, teams[len(c):]
                    break
        date = f"20{yy}-{MONTHS[mon]:02d}-{dd}"

        # win probabilities (mid prices, normalized so the two sides sum to 1)
        pw = {c: mid(m) for c, m in g["win"].items() if mid(m) is not None}
        tot = sum(pw.values())
        win_home = round(pw[home] / tot, 3) if home in pw and tot else None

        # spread: each team's ladder is P(team wins by more than X); the median margin is where it
        # crosses 50%. Convert to nflverse/sportsbook convention: spread_home negative = home favored.
        spread_home = None
        for c, ladder in g["spread"].items():
            med = implied_line([(strike(m), (mid(m) or 0) / 100) for m in ladder])
            if med is not None:
                spread_home = -med if c == home else med
                break
        spread_source = "spread ladder" if spread_home is not None else None
        if spread_home is None and win_home is not None and 0.02 < win_home < 0.98:
            # near pick'em: no ladder crosses 50%, so back out a spread from the win probability
            spread_home = round(-CFG["sigma"] * NormalDist().inv_cdf(win_home), 1)
            spread_source = "win prob"
        total = implied_line([(strike(m), (mid(m) or 0) / 100) for m in g["total"]])

        row = {
            "game_key": key, "date": date, "away": nv(away) if away else None,
            "home": nv(home) if home else None,
        }
        if LEAGUE == "nfl":
            row["nflverse_match"] = f"{nv(away)}@{nv(home)} on/near {date}" if away else None
        row.update({
            "kalshi_win_prob_home": win_home,
            "kalshi_spread_home": spread_home, "spread_source": spread_source, "kalshi_total": total,
            "close_time": g["close_time"], "expected_expiration_time": g["expected_expiration_time"],
            "win_markets": {nv(c): rung(m) for c, m in g["win"].items()},
            "spread_ladders": {nv(c): sorted((rung(m) for m in l), key=lambda r: r["strike"] or 0)
                               for c, l in g["spread"].items()},
            "total_ladder": sorted((rung(m) for m in g["total"]), key=lambda r: r["strike"] or 0),
        })
        out.append(row)
    return sorted(out, key=lambda r: (r["date"], r["game_key"]))


def main():
    os.makedirs(os.path.join(OUT, "history"), exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    all_series = get("/series").get("series", [])
    nfl = [s for s in all_series if str(s.get("ticker", "")).upper().startswith(CFG["prefix"])]
    json.dump({"fetched_at": now, "series": [{k: s.get(k) for k in ("ticker", "title", "category", "frequency")}
                                             for s in nfl]},
              open(os.path.join(OUT, "series.json"), "w"), indent=1)
    found = {s["ticker"] for s in nfl}
    targets = [t for t in TARGETS if t in found]
    missing = [t for t in TARGETS if t not in found]
    if missing:
        print("WARNING: series not found on Kalshi:", missing)

    markets = []
    for t in targets:
        events = paged("/events", "events", series_ticker=t, status="open", with_nested_markets="true")
        for ev in events:
            for m in ev.get("markets", []):
                m["event_title"] = ev.get("title")
                m["series_ticker"] = t
                markets.append(m)
    keep = ("ticker", "event_ticker", "series_ticker", "event_title", "title", "subtitle", "yes_sub_title",
            "no_sub_title", "status", "open_time", "close_time", "expected_expiration_time",
            "floor_strike", "cap_strike", "strike_type", "yes_bid", "yes_ask", "no_bid", "no_ask",
            "last_price", "yes_bid_dollars", "yes_ask_dollars", "last_price_dollars", "volume",
            "volume_24h", "open_interest", "liquidity")
    slim = [{k: m.get(k) for k in keep if m.get(k) is not None} for m in markets]
    json.dump({"fetched_at": now, "series_pulled": targets, "markets": slim},
              open(os.path.join(OUT, "latest.json"), "w"), indent=1)
    lines = summarize(slim)
    json.dump({"fetched_at": now, "games": lines}, open(os.path.join(OUT, "lines.json"), "w"), indent=1)

    season = datetime.now(timezone.utc).year if datetime.now(timezone.utc).month >= 6 \
        else datetime.now(timezone.utc).year - 1
    hist = os.path.join(OUT, "history", f"{season}.csv")
    new = not os.path.exists(hist)
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["fetched_at", "ticker", "event_ticker", "yes_bid", "yes_ask", "last_price", "volume"])
        for m in slim:
            p = mid(m)
            if (p is not None and 10 <= p <= 90) or TARGETS.get(m.get("series_ticker")) == "win":
                w.writerow([now, m["ticker"], m.get("event_ticker"), cents(m, "yes_bid"),
                            cents(m, "yes_ask"), cents(m, "last_price"), m.get("volume")])
    print(f"{len(nfl)} {LEAGUE.upper()} series, pulled {targets}, {len(slim)} open markets, {len(lines)} events")


if __name__ == "__main__":
    main()
