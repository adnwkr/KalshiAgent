# NBA Kalshi relay

Same relay as `nfl-kalshi/`, run for the NBA by the same workflow (`.github/workflows/nfl-kalshi.yml`). The scripts live in `nfl-kalshi/` and take a league argument (`python nfl-kalshi/fetch_kalshi.py nba`).

- `data/lines.json`: one row per game in standard NBA codes (BKN, GSW, NOP, NYK, PHX, SAS...). Kalshi win probability, the spread and total implied by where each ladder crosses 50 cents, and every tradeable rung with bid and ask. Built from the `KXNBAGAME`, `KXNBASPREAD` and `KXNBATOTAL` series.
- `data/latest.json`: every open market in those series (written each run for debugging, not committed).
- `data/series.json`: every Kalshi series starting with `KXNBA`, for discovering new market types.
- `data/history/<season>.csv`: price history for near-the-money markets, for closing line value. Season is the start year (2026 = 2026-27).
- `data/odds.json` (needs the `ODDS_API_KEY` secret): sportsbook lines from The Odds API, same fields as the NFL file. Pulled twice a day from October through June (about 11am and 5pm ET), roughly 180 credits a month.
- `data/odds_history/<season>.csv`: every book's main line at each pull.

## Model

`nba_model.py` is the NBA fair-line model: recency-weighted team offense and defense ratings (weighted ridge on points scored, with home court and back-to-back terms), fit only on games before the date being priced. Results and lines come from `data/results/games_<season>.csv`, which the workflow collects from ESPN (`fetch_results.py`).

```
python3 nba_model.py predict 2026-11-06     # model vs DraftKings lines -> preds.json
python3 nba_model.py kalshi 2026-11-06      # model vs every Kalshi rung, fees included -> kalshi_edges.json
python3 nba_model.py books 2026-11-06       # model vs best sportsbook price per side + market board -> books_edges.json
python3 nba_model.py grade picks.json       # grade Bet Log picks -> graded.json
python3 nba_model.py backtest               # walk-forward over the last two seasons
```

Backtest (2025-26 season, 1,066 games with a closing line): margin RMSE model 14.9 vs market 14.3; ATS 47-49% at every edge threshold; totals 50-52%. The model alone does not beat the closing line, so picks need a research reason (injuries, rest, lineup news) on top of the model edge.

Settings live in the Notion "NBA Lessons & Settings" page and are written to `config.json` at run time.
