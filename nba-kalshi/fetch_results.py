"""
NBA game results and schedule from ESPN's public scoreboard, for the NBA model.
Runs inside the GitHub Action (the agent's sandbox can't reach ESPN; it reads these files).

Outputs (nba-kalshi/data/results/):
  games_<season>.csv   one row per game: season (start year, 2026 = 2026-27), date (US Eastern),
                       tip-off (UTC), teams, final score, status, and the closing DraftKings spread and
                       total when ESPN lists them (pre-game lines for upcoming games)

Usage:
  python fetch_results.py              refresh the last 4 days and the next 8 days (every run)
  python fetch_results.py --backfill N also rebuild the last N seasons in full (run once)
"""
import csv
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "results")
SB = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={}&limit=100"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={}"
ET = ZoneInfo("America/New_York")
# ESPN codes -> the codes the relay and Bet Log use
CODES = {"GS": "GSW", "NY": "NYK", "NO": "NOP", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS", "BRK": "BKN",
         "PHO": "PHX", "CHO": "CHA"}
NBA_TEAMS = {"ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW", "HOU", "IND", "LAC", "LAL",
             "MEM", "MIA", "MIL", "MIN", "NOP", "NYK", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR",
             "UTA", "WAS"}
FIELDS = ["game_id", "season", "season_type", "date", "tipoff_utc", "away", "home", "away_pts", "home_pts",
          "status", "neutral", "spread_home", "total", "line_source"]


def get(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (kalshi-relay)",
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == 3:
                return None
            time.sleep(1.5 * (attempt + 1))


def code(t):
    a = (t.get("team") or {}).get("abbreviation")  # missing for playoff placeholders (TBD)
    return CODES.get(a, a)


def odds_of(comp):
    """Spread (home view, negative = home favored) and total from a competition's odds block."""
    for o in comp.get("odds") or []:
        sp, tot = o.get("spread"), o.get("overUnder")
        if sp is not None or tot is not None:
            return (float(sp) if sp is not None else None, float(tot) if tot is not None else None,
                    (o.get("provider") or {}).get("name", "espn"))
    return None, None, None


def summary_line(game_id):
    """Closing line for a finished game, from the game summary's pickcenter."""
    d = get(SUMMARY.format(game_id))
    if not d:
        return None, None, None
    for o in d.get("pickcenter") or []:
        sp, tot = o.get("spread"), o.get("overUnder")
        if sp is not None or tot is not None:
            # pickcenter spread is from the home team's view when homeTeamOdds is the favorite side
            return (float(sp) if sp is not None else None, float(tot) if tot is not None else None,
                    (o.get("provider") or {}).get("name", "espn") + " close")
    return None, None, None


def games_on(day):
    try:
        return _games_on(day)
    except Exception as ex:  # one bad day must not stop the whole run
        print(f"nba results: skipped {day} ({type(ex).__name__}: {ex})")
        return []


def _games_on(day):
    d = get(SB.format(day.strftime("%Y%m%d")))
    rows = []
    for e in (d or {}).get("events", []):
        if not e.get("competitions"):
            continue
        c = e["competitions"][0]
        teams = {t["homeAway"]: t for t in c["competitors"]}
        if "home" not in teams or "away" not in teams:
            continue
        h, a = code(teams["home"]), code(teams["away"])
        if h not in NBA_TEAMS or a not in NBA_TEAMS:  # all-star and exhibition teams
            continue
        st = c["status"]["type"]
        tip = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        sp, tot, src = odds_of(c)
        season = e.get("season", {})
        rows.append({
            "game_id": e["id"], "season": int(season.get("year", tip.year)) - 1,
            "season_type": {1: "pre", 2: "regular", 3: "playoffs", 5: "play-in"}.get(season.get("type"), season.get("type")),
            "date": tip.astimezone(ET).date().isoformat(), "tipoff_utc": tip.strftime("%Y-%m-%dT%H:%MZ"),
            "away": a, "home": h,
            "away_pts": teams["away"].get("score") if st.get("completed") else "",
            "home_pts": teams["home"].get("score") if st.get("completed") else "",
            "status": "final" if st.get("completed") else st.get("name", "").replace("STATUS_", "").lower(),
            "neutral": int(bool(c.get("neutralSite"))),
            "spread_home": sp if sp is not None else "", "total": tot if tot is not None else "",
            "line_source": src or ""})
    return rows


def season_days(start_year):
    """Every date from Oct 1 of the start year to Jun 30 of the next."""
    d, end = date(start_year, 10, 1), date(start_year + 1, 6, 30)
    while d <= end:
        yield d
        d += timedelta(days=1)


def load(season):
    p = os.path.join(OUT, f"games_{season}.csv")
    if not os.path.exists(p):
        return {}
    return {r["game_id"]: r for r in csv.DictReader(open(p))}


def save(season, rows):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, f"games_{season}.csv")
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: (r["tipoff_utc"], r["game_id"])):
            w.writerow({k: r.get(k, "") for k in FIELDS})


def merge(fetched, fill_closing):
    """Merge fetched rows into the season files. A finished game keeps the last pre-game line it
    had; if it never had one, look up the closing line in the game summary (when fill_closing)."""
    by_season = {}
    for r in fetched:
        by_season.setdefault(r["season"], []).append(r)
    total_new = 0
    for season, rows in by_season.items():
        cur = load(season)
        need = []
        for r in rows:
            old = cur.get(r["game_id"])
            if old and r["status"] == "final" and r["spread_home"] == "" and old.get("spread_home") not in ("", None):
                r["spread_home"], r["total"], r["line_source"] = old["spread_home"], old["total"], old["line_source"]
            if r["status"] == "final" and r["spread_home"] == "" and fill_closing:
                need.append(r)
            total_new += old is None
            cur[r["game_id"]] = r
        if need:
            with ThreadPoolExecutor(8) as ex:
                for r, (sp, tot, src) in zip(need, ex.map(lambda r: summary_line(r["game_id"]), need)):
                    if sp is not None or tot is not None:
                        r["spread_home"] = sp if sp is not None else ""
                        r["total"] = tot if tot is not None else ""
                        r["line_source"] = src
        save(season, cur)
        lines = sum(1 for r in cur.values() if r["status"] == "final" and r["spread_home"] != "")
        finals = sum(1 for r in cur.values() if r["status"] == "final")
        print(f"nba results {season}: {len(cur)} games, {finals} final, {lines} finals with a closing line")
    return total_new


def main():
    today = datetime.now(ET).date()
    days = [today + timedelta(days=k) for k in range(-4, 9)]
    fill = True
    if "--backfill" in sys.argv:
        n = int(sys.argv[sys.argv.index("--backfill") + 1])
        cur_start = today.year if today.month >= 7 else today.year - 1
        for y in range(cur_start - n + 1, cur_start + 1):
            days += list(season_days(y))
    days = sorted(set(days))
    with ThreadPoolExecutor(8) as ex:
        fetched = [r for rows in ex.map(games_on, days) for r in rows]
    new = merge(fetched, fill)
    print(f"nba results: {len(days)} days fetched, {len(fetched)} games seen, {new} new")


if __name__ == "__main__":
    main()
