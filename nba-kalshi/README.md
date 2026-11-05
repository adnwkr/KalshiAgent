# NBA Kalshi relay

Same relay as `nfl-kalshi/`, run for the NBA by the same workflow (`.github/workflows/nfl-kalshi.yml`). The scripts live in `nfl-kalshi/` and take a league argument (`python nfl-kalshi/fetch_kalshi.py nba`).

- `data/lines.json`: one row per game in standard NBA codes (BKN, GSW, NOP, NYK, PHX, SAS...). Kalshi win probability, the spread and total implied by where each ladder crosses 50 cents, and every tradeable rung with bid and ask. Built from the `KXNBAGAME`, `KXNBASPREAD` and `KXNBATOTAL` series.
- `data/latest.json`: every open market in those series.
- `data/series.json`: every Kalshi series starting with `KXNBA`, for discovering new market types.
- `data/history/<season>.csv`: price history for near-the-money markets, for closing line value. Season is the start year (2026 = 2026-27).
- `data/odds.json` (needs the `ODDS_API_KEY` secret): sportsbook lines from The Odds API, same fields as the NFL file. Pulled twice a day from October through June (about 11am and 5pm ET), roughly 180 credits a month.
- `data/odds_history/<season>.csv`: every book's main line at each pull.

There is no NBA model yet; `nfl_model.py` is NFL only.
