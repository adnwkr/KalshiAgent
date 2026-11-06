"""Decide whether a scheduled odds pull (or daily check) is due, catching up on skipped runs.

GitHub Actions often skips or delays cron runs, so pulls tied to an exact UTC hour get missed.
Each league has slots (UTC weekday or every day, hour, catch-up window in hours). A pull is due
when the most recent slot that started within its window has not been handled yet. The handled
slot is recorded in <league>-kalshi/data/odds_slot.txt, so each slot is pulled at most once.

  python nfl-kalshi/odds_due.py nba          -> prints yes/no
  python nfl-kalshi/odds_due.py nba --mark   -> records the current slot as handled
"""
import datetime as dt
import os
import sys

# weekday: Mon=0 ... Sun=6, None = every day
SLOTS = {
    # daily morning lines; Thu TNF close; Sun 1pm and 4pm ET closes; SNF and MNF closes
    "nfl": [(None, 12, 8), (3, 22, 3), (6, 16, 2), (6, 20, 1.5), (0, 0, 1.5), (1, 0, 1.5)],
    # late-morning lines and the close before 7pm ET tips (Oct through Jun)
    "nba": [(None, 16, 6), (None, 22, 3)],
    # daily majors check (fetch_pga.py decides whether a major is near) and golf season rebuild
    "pga": [(None, 12, 12)],
}
OFF_MONTHS = {"nba": {7, 8, 9}}


def current_slot(league, now):
    if now.month in OFF_MONTHS.get(league, set()):
        return None
    base = now.replace(minute=0, second=0, microsecond=0)
    best = None
    for back in range(0, 3):
        day = base - dt.timedelta(days=back)
        for wd, hr, win in SLOTS[league]:
            s = day.replace(hour=hr)
            if (wd is None or s.weekday() == wd) and s <= now < s + dt.timedelta(hours=win):
                if best is None or s > best:
                    best = s
    return best


def main():
    league = sys.argv[1]
    now = dt.datetime.now(dt.timezone.utc)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", f"{league}-kalshi", "data", "odds_slot.txt")
    slot = current_slot(league, now)
    if "--mark" in sys.argv:
        if slot:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").write(slot.isoformat() + "\n")
        return
    try:
        done = dt.datetime.fromisoformat(open(path).read().strip())
    except (OSError, ValueError):
        done = None
    # due only for a slot newer than the last one handled (an older, still-open slot is covered)
    print("yes" if slot and (done is None or slot > done) else "no")


if __name__ == "__main__":
    main()
