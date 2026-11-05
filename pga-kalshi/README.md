# PGA Kalshi relay

Kalshi PGA Tour markets for every weekly event, plus sportsbook winner odds during the four majors. Run by the same workflow as NFL and NBA (`.github/workflows/nfl-kalshi.yml`), every 2 hours.

Golf is a field, not a matchup: each Kalshi event is one tournament and each market is one golfer.

- `data/lines.json`: one row per open tournament (keyed on Kalshi's event suffix, e.g. `MAST27`), with every golfer's mid price, bid, ask and volume for each market type: `win` (KXPGATOUR), `top5`, `top10`, `top20`, `make_cut` and `r1_lead`. `win_prob_norm` rescales the winner prices so the field sums to 100%, and `win_overround` shows how far over 100 the raw prices sum.
- `data/latest.json`: every open market in those series.
- `data/series.json`: every Kalshi series starting with `KXPGA` or `KXGOLF`, for discovering new market types.
- `data/history/<year>.csv`: prices for golfers at 3 cents or more in the win and top 5/10/20 markets, for closing line value.
- `data/odds.json` (majors only, needs the `ODDS_API_KEY` secret): each book's winner price per golfer, the no-vig probability, the median across books, the best US-licensed price, Pinnacle, and the matching Kalshi winner price. The Odds API only covers the Masters, PGA Championship, US Open and Open Championship, winner market only.
- `data/odds_history/<year>.csv`: every book's winner price at each pull.

Odds credits: once a day the script looks up each major's start date (free) and pulls winner odds only when one starts within 7 days or is in progress, at 1 credit per major. About 30 credits a year. Ticking "Also pull sportsbook odds now" pulls every listed major at once (up to 4 credits).

Run by hand: `python pga-kalshi/fetch_pga.py` (Kalshi) or `python pga-kalshi/fetch_pga.py --odds-only [--force]` (sportsbook odds).

There is no golf model yet.
