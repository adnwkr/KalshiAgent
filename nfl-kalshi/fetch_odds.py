"""
Pull NFL or NBA sportsbook odds from The Odds API (https://the-odds-api.com) and write a
per-game summary the betting agent reads from GitHub.

Needs ODDS_API_KEY in the environment (a GitHub Actions secret). Without it, the
script logs that and exits cleanly so the Kalshi relay still runs.

Credits: one call with 3 markets and up to 10 named bookmakers costs 3 credits.
The workflow only calls this at chosen times (see ODDS_HOURS in the workflow).

Usage: python fetch_odds.py [nfl|nba]   (default nfl)

Outputs (nfl-kalshi/data/ or nba-kalshi/data/):
  odds_raw.json   the API response, trimmed
  odds.json       one row per game: every book's line and price, consensus line,
                  no-vig probabilities, best available price per side, Pinnacle if listed
  odds_history/<season>.csv  main-line snapshots for closing line value
"""
import csv
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPORTS = {"nfl": "americanfootball_nfl", "nba": "basketball_nba"}
LEAGUE = (sys.argv[1] if len(sys.argv) > 1 else "nfl").lower()
if LEAGUE not in SPORTS:
    sys.exit(f"unknown league {LEAGUE!r}; use one of {sorted(SPORTS)}")
OUT = os.path.join(REPO, f"{LEAGUE}-kalshi", "data")
URL = f"https://api.the-odds-api.com/v4/sports/{SPORTS[LEAGUE]}/odds"
# 10 bookmakers count as one region (3 credits for 3 markets). Unknown keys are ignored.
BOOKS = "draftkings,fanduel,betmgm,williamhill_us,espnbet,fanatics,betrivers,bovada,lowvig,pinnacle"
# US state-licensed books. Offshore books (bovada, lowvig, pinnacle) are kept for consensus and as
# sharp price signals, but "best" prices only come from these so the agent never logs an offshore bet.
LICENSED = {"draftkings", "fanduel", "betmgm", "williamhill_us", "espnbet", "fanatics", "betrivers"}

NFL_TEAMS = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LA", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}
NBA_TEAMS = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN", "Charlotte Hornets": "CHA",
    "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE", "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN",
    "Detroit Pistons": "DET", "Golden State Warriors": "GSW", "Houston Rockets": "HOU",
    "Indiana Pacers": "IND", "Los Angeles Clippers": "LAC", "LA Clippers": "LAC",
    "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM", "Miami Heat": "MIA",
    "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS", "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA", "Washington Wizards": "WAS",
}
TEAMS = NBA_TEAMS if LEAGUE == "nba" else NFL_TEAMS


def implied(american):
    a = float(american)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def novig(p1, p2):
    s = p1 + p2
    return (p1 / s, p2 / s) if s else (None, None)


def summarize(games):
    out = []
    for g in games:
        home, away = TEAMS.get(g["home_team"], g["home_team"]), TEAMS.get(g["away_team"], g["away_team"])
        books = {}
        for b in g.get("bookmakers", []):
            row = {"title": b.get("title"), "last_update": b.get("last_update")}
            for m in b.get("markets", []):
                o = {x["name"]: x for x in m.get("outcomes", [])}
                if m["key"] == "h2h" and g["home_team"] in o and g["away_team"] in o:
                    ph, pa = o[g["home_team"]]["price"], o[g["away_team"]]["price"]
                    row["ml"] = {"home": ph, "away": pa,
                                 "novig_home": round(novig(implied(ph), implied(pa))[0], 4)}
                elif m["key"] == "spreads" and g["home_team"] in o and g["away_team"] in o:
                    h, a = o[g["home_team"]], o[g["away_team"]]
                    row["spread"] = {"home_point": h.get("point"), "home_price": h["price"],
                                     "away_point": a.get("point"), "away_price": a["price"],
                                     "novig_home_cover": round(novig(implied(h["price"]), implied(a["price"]))[0], 4)}
                elif m["key"] == "totals" and "Over" in o and "Under" in o:
                    ov, un = o["Over"], o["Under"]
                    row["total"] = {"point": ov.get("point"), "over_price": ov["price"],
                                    "under_price": un["price"],
                                    "novig_over": round(novig(implied(ov["price"]), implied(un["price"]))[0], 4)}
            books[b["key"]] = row

        def med(vals):
            vals = [v for v in vals if v is not None]
            return round(statistics.median(vals), 2) if vals else None

        bettable = {k: r for k, r in books.items() if k in LICENSED}

        def best(side_key, point_key, price_key):
            """Best line for the bettor: highest point (more points for spreads), then best price."""
            cands = [(r["spread"][point_key], r["spread"][price_key], k) for k, r in bettable.items()
                     if "spread" in r and r["spread"].get(point_key) is not None]
            if not cands:
                return None
            p, pr, k = max(cands, key=lambda c: (c[0], implied(c[1]) * -1))
            return {"point": p, "price": pr, "book": k}

        def best_total(side):
            pk = "over_price" if side == "over" else "under_price"
            cands = [(r["total"]["point"], r["total"][pk], k) for k, r in bettable.items()
                     if "total" in r and r["total"].get("point") is not None]
            if not cands:
                return None
            # over wants the lowest number, under the highest
            key = (lambda c: (-c[0], -implied(c[1]))) if side == "over" else (lambda c: (c[0], -implied(c[1])))
            p, pr, k = max(cands, key=key)
            return {"point": p, "price": pr, "book": k}

        def best_ml(side):
            cands = [(r["ml"][side], k) for k, r in bettable.items() if "ml" in r]
            if not cands:
                return None
            pr, k = min(cands, key=lambda c: implied(c[0]))
            return {"price": pr, "book": k}

        pin = books.get("pinnacle")
        out.append({
            "odds_api_id": g["id"], "commence_time": g["commence_time"], "away": away, "home": home,
            "n_books": len(books),
            "consensus": {
                "spread_home": med([r["spread"]["home_point"] for r in books.values() if "spread" in r]),
                "total": med([r["total"]["point"] for r in books.values() if "total" in r]),
                "novig_home_win": med([r["ml"]["novig_home"] for r in books.values() if "ml" in r]),
                "novig_home_cover": med([r["spread"]["novig_home_cover"] for r in books.values() if "spread" in r]),
                "novig_over": med([r["total"]["novig_over"] for r in books.values() if "total" in r]),
            },
            "pinnacle": {k: pin[k] for k in ("spread", "total", "ml") if k in pin} if pin else None,
            "best": {"home_spread": best("home", "home_point", "home_price"),
                     "away_spread": best("away", "away_point", "away_price"),
                     "over": best_total("over"), "under": best_total("under"),
                     "home_ml": best_ml("home"), "away_ml": best_ml("away")},
            "books": books,
        })
    return sorted(out, key=lambda r: r["commence_time"])


def main():
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        print("odds: ODDS_API_KEY not set, skipping")
        return 0
    params = {"apiKey": key, "markets": "h2h,spreads,totals", "oddsFormat": "american",
              "bookmakers": BOOKS, "dateFormat": "iso"}
    req = urllib.request.Request(f"{URL}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": "kalshi-relay"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            games = json.load(r)
            used, remaining = r.headers.get("x-requests-used"), r.headers.get("x-requests-remaining")
    except urllib.error.HTTPError as e:  # never print the URL: it contains the API key
        print(f"odds: HTTP {e.code} {e.reason} (401 = bad key, 429 = out of credits)")
        return 1
    except Exception as e:
        print(f"odds: request failed ({type(e).__name__})")
        return 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(os.path.join(OUT, "odds_history"), exist_ok=True)
    json.dump({"fetched_at": now, "games": games}, open(os.path.join(OUT, "odds_raw.json"), "w"))
    summary = summarize(games)
    json.dump({"fetched_at": now, "credits_used": used, "credits_remaining": remaining, "games": summary},
              open(os.path.join(OUT, "odds.json"), "w"), indent=1)

    season = datetime.now(timezone.utc).year if datetime.now(timezone.utc).month >= 6 \
        else datetime.now(timezone.utc).year - 1
    hist = os.path.join(OUT, "odds_history", f"{season}.csv")
    new = not os.path.exists(hist)
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["fetched_at", "commence_time", "away", "home", "book", "spread_home", "spread_home_price",
                        "spread_away_price", "total", "over_price", "under_price", "ml_home", "ml_away"])
        for g in summary:
            for k, r in g["books"].items():
                s, t, m = r.get("spread", {}), r.get("total", {}), r.get("ml", {})
                w.writerow([now, g["commence_time"], g["away"], g["home"], k, s.get("home_point"),
                            s.get("home_price"), s.get("away_price"), t.get("point"), t.get("over_price"),
                            t.get("under_price"), m.get("home"), m.get("away")])
    print(f"odds {LEAGUE}: {len(summary)} games, credits used {used}, remaining {remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
