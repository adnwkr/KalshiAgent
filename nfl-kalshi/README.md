# NFL Kalshi relay

The same workflow also pulls NBA lines into `../nba-kalshi/data/` (see that folder's README).

GitHub Action that pulls Kalshi's public NFL market data every 2 hours and commits it here, so the NFL betting agent's scheduled runs (which can reach GitHub but not Kalshi) get live lines.

- `data/lines.json`: one row per game in nflverse team codes. Kalshi win probability, the spread and total implied by where each ladder crosses 50 cents, and every tradeable rung with bid and ask.
- `data/latest.json`: every open market in the game, spread and total series, with bid, ask, last price and volume.
- `data/series.json`: every Kalshi series starting with `KXNFL`, for discovering new market types.
- `data/history/<season>.csv`: price history for near-the-money markets, used for closing line value.
- `data/odds.json` (needs the `ODDS_API_KEY` secret): sportsbook lines from The Odds API for up to 10 books, with the consensus line, no-vig probabilities, Pinnacle if listed, and the best available price for every side. Pulled about 12 times a week (daily, plus before each kickoff window), roughly 160 of the free plan's 500 monthly credits.
- `data/odds_history/<season>.csv`: every book's main line at each pull, used for sportsbook closing line value.

Run it by hand from the Actions tab ("Kalshi relay (NFL + NBA)" > Run workflow); tick "Also pull NFL and NBA sportsbook odds now" to force an odds pull for both leagues. Prices are in cents (50 = 50% implied). Kalshi fees are not included.

## Model

`nfl_model.py` is the NFL betting agent's fair-line model (nflverse data, ridge regression on team efficiency).

```
python3 nfl_model.py download 2026        # nflverse schedules and play-by-play into data/
python3 nfl_model.py predict 2026 8       # model vs sportsbook lines -> preds.json
python3 nfl_model.py kalshi 2026 8        # model vs every Kalshi rung, fees included -> kalshi_edges.json
python3 nfl_model.py books 2026 8         # model vs best sportsbook price per side, plus a market board -> books_edges.json
python3 nfl_model.py grade picks.json     # grade Bet Log picks -> graded.json
python3 nfl_model.py backtest             # walk-forward backtest 2019 onward
```

Settings live in the Notion "Lessons & Settings" page and are written to `config.json` at run time.
