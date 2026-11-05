"""
PGA Tour round-by-round results from ESPN's public golf scoreboard, for the PGA model.
Runs inside the GitHub Action (the agent's sandbox can't reach ESPN; it reads these files).

Outputs (pga-kalshi/data/results/):
  events.csv          one row per event: id, name, start and end date, season, status, field size
  rounds_<year>.csv   one row per player per round for FINISHED events: strokes, plus the player's
                      final position (e.g. 1, T7) or MC/WD/DQ
  current.json        this week's event (field and live round scores), refreshed every run

Usage:
  python fetch_results.py                current event every run; current year rebuilt when --daily
  python fetch_results.py --daily        also rebuild the current year's finished events
  python fetch_results.py --backfill N   rebuild the last N calendar years in full (run once)
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "results")
SB = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard"
EV_FIELDS = ["event_id", "name", "start", "end", "season", "status", "field", "rounds_played"]
RD_FIELDS = ["event_id", "start", "player_id", "player", "round", "strokes", "pos", "total_to_par"]


def get(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (kalshi-relay)",
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception:
            if attempt == 3:
                return None
            time.sleep(2 * (attempt + 1))


def to_par(s):
    s = str(s or "").strip()
    if s in ("E", "0"):
        return 0
    try:
        return int(s.replace("+", ""))
    except ValueError:
        return None


def parse_event(e):
    """Rounds and positions for one event. ESPN lists competitors in finishing order; players who
    miss the cut have only two rounds. Positions with ties are computed from the final to-par."""
    c = e["competitions"][0]
    st = c.get("status", e.get("status", {})).get("type", {})
    players = []
    for p in c.get("competitors", []):
        if p.get("type", "athlete") != "athlete":  # team events (Zurich Classic) list pairs, not players
            continue
        rounds = []
        for ls in p.get("linescores", []) or []:
            v, per = ls.get("value"), ls.get("period")
            holes = ls.get("linescores") or []
            # a round counts once it has 18 holes, or a stroke total and no hole detail (older events)
            if v and per and (len(holes) >= 18 or (not holes and v >= 55)):
                rounds.append((int(per), int(v)))
        players.append({"id": p.get("id"), "name": (p.get("athlete") or {}).get("displayName"),
                        "order": p.get("order", 999), "par": to_par(p.get("score")), "rounds": rounds})
    n_rounds = max((len(p["rounds"]) for p in players), default=0)
    # final positions: players with the full set of rounds are ranked by to-par, ties shared
    full = [p for p in players if len(p["rounds"]) == n_rounds and p["par"] is not None]
    full.sort(key=lambda p: (p["par"], p["order"]))
    pos = {}
    for i, p in enumerate(full):
        better = sum(1 for q in full if q["par"] < p["par"])
        tied = sum(1 for q in full if q["par"] == p["par"]) > 1
        # the winner is whoever ESPN lists first (playoffs break ties for 1st)
        if better == 0:
            n_top = sum(1 for q in full if q["par"] == p["par"])
            pos[p["id"]] = "1" if p is full[0] else ("2" if n_top == 2 else "T2")
        else:
            pos[p["id"]] = ("T" if tied else "") + str(better + 1)
    for p in players:
        if p["id"] not in pos:
            pos[p["id"]] = "MC" if 0 < len(p["rounds"]) < n_rounds else "WD"
    start = (e.get("date") or c.get("date") or "")[:10]
    ev = {"event_id": e["id"], "name": e.get("name"), "start": start, "end": (e.get("endDate") or "")[:10],
          "season": (e.get("season") or {}).get("year", start[:4]), "status": st.get("name", "").replace("STATUS_", "").lower(),
          "field": len(players), "rounds_played": n_rounds}
    rows = [{"event_id": e["id"], "start": start, "player_id": p["id"], "player": p["name"], "round": r,
             "strokes": s, "pos": pos[p["id"]], "total_to_par": p["par"]}
            for p in players for r, s in sorted(p["rounds"])]
    return ev, rows


def write_csv(path, fields, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def rebuild_year(year, events):
    d = get(f"{SB}?dates={year}")
    if not d:
        print(f"pga results {year}: fetch failed")
        return
    rows = []
    for e in d.get("events", []):
        if not e.get("competitions"):
            continue
        try:
            ev, rr = parse_event(e)
        except Exception as ex:
            print(f"pga results: skipped event {e.get('name')} ({type(ex).__name__}: {ex})")
            continue
        events[ev["event_id"]] = ev
        if ev["status"] == "final":
            rows += rr
    rows.sort(key=lambda r: (r["start"], r["event_id"], r["player"] or "", int(r["round"])))
    write_csv(os.path.join(OUT, f"rounds_{year}.csv"), RD_FIELDS, rows)
    n_final = sum(1 for e in events.values() if str(e["start"]).startswith(str(year)) and e["status"] == "final")
    print(f"pga results {year}: {n_final} finished events, {len(rows)} player-rounds")


def main():
    os.makedirs(OUT, exist_ok=True)
    evp = os.path.join(OUT, "events.csv")
    events = {r["event_id"]: r for r in csv.DictReader(open(evp))} if os.path.exists(evp) else {}
    year = datetime.now(timezone.utc).year
    years = []
    if "--backfill" in sys.argv:
        n = int(sys.argv[sys.argv.index("--backfill") + 1])
        years = list(range(year - n + 1, year + 1))
    elif "--daily" in sys.argv:
        years = [year]
    for y in years:
        rebuild_year(y, events)

    # this week's event, live
    d = get(SB)
    cur = []
    for e in (d or {}).get("events", []):
        if not e.get("competitions"):
            continue
        ev, rr = parse_event(e)
        events.setdefault(ev["event_id"], ev).update({k: ev[k] for k in ("status", "rounds_played", "field")})
        cur.append({"event": ev, "players": sorted({r["player"] for r in rr} |
                                                   {(p.get("athlete") or {}).get("displayName")
                                                    for p in e["competitions"][0].get("competitors", [])} - {None}),
                    "rounds": rr})
    json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "events": cur},
              open(os.path.join(OUT, "current.json"), "w"), indent=1)
    write_csv(evp, EV_FIELDS, sorted(events.values(), key=lambda e: (str(e["start"]), e["event_id"])))
    print(f"pga results: {len(events)} events on file; current: {[c['event']['name'] for c in cur]}")


if __name__ == "__main__":
    main()
