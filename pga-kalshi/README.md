# PGA Kalshi relay

Kalshi PGA Tour markets for every weekly event, plus sportsbook winner odds during the four majors. Run by the same workflow as NFL and NBA (`.github/workflows/nfl-kalshi.yml`), every 2 hours.

Golf is a field, not a matchup: each Kalshi event is one tournament and each market is one golfer.

- `data/lines.json`: one row per open tournament (keyed on Kalshi's event suffix, e.g. `MAST27`), with every golfer's mid price, bid, ask and volume for each market type: `win` (KXPGATOUR), `top5`, `top10`, `top20`, `make_cut` and `r1_lead`. `win_prob_norm` rescales the winner prices so the field sums to 100%, and `win_overround` shows how far over 100 the raw prices sum.
- `data/latest.json`: every open market in those series (written each run for debugging, not committed).
- `data/series.json`: every Kalshi series starting with `KXPGA` or `KXGOLF`, for discovering new market types.
- `data/history/<year>.csv`: prices for golfers at 3 cents or more in the win and top 5/10/20 markets, for closing line value.
- `data/odds.json` (majors only, needs the `ODDS_API_KEY` secret): each book's winner price per golfer, the no-vig probability, the median across books, the best US-licensed price, Pinnacle, and the matching Kalshi winner price. The Odds API only covers the Masters, PGA Championship, US Open and Open Championship, winner market only.
- `data/odds_history/<year>.csv`: every book's winner price at each pull.

Odds credits: once a day the script looks up each major's start date (free) and pulls winner odds only when one starts within 7 days or is in progress, at 1 credit per major. About 30 credits a year. Ticking "Also pull sportsbook odds now" pulls every listed major at once (up to 4 credits).

Run by hand: `python pga-kalshi/fetch_pga.py` (Kalshi) or `python pga-kalshi/fetch_pga.py --odds-only [--force]` (sportsbook odds).

## Model

`pga_model.py` rates every golfer from round scores (each round adjusted for how hard the course played that day, recency-weighted, shrunk toward a prior for players with few rounds), then simulates the tournament 20,000 times (4 rounds, top 65 and ties make the 36-hole cut, no cut for small fields) to get each golfer's chance to win, finish top 5/10/20, make the cut and lead after round 1. Round scores come from `data/results/rounds_<year>.csv`, collected from ESPN by `fetch_results.py`.

```
python3 pga_model.py events                 # open Kalshi tournaments
python3 pga_model.py predict WOWTC26        # simulate -> preds.json
python3 pga_model.py kalshi WOWTC26         # model vs every Kalshi golfer market, fees included -> kalshi_edges.json
python3 pga_model.py books MAST27           # majors only: model vs best book winner price -> books_edges.json
python3 pga_model.py grade picks.json       # grade Bet Log picks -> graded.json
python3 pga_model.py backtest               # walk-forward over recent events
```

Backtest (39 recent events): beats an equal-chance baseline on win, top 10 and make cut; top-10 probabilities are well calibrated (e.g. 7.5% predicted vs 7.1% actual). The model is pre-tournament only: once an event starts it flags nothing. It does not know course history, injuries or withdrawals.

Settings live in the Notion "PGA Lessons & Settings" page and are written to `config.json` at run time.
