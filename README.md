# KalshiAgent

A research agent that looks for mispriced sports contracts on [Kalshi](https://kalshi.com) (NFL, NBA and PGA Tour), explains its reasoning, logs paper picks, and grades itself against the closing price. Built as a group final project for Professor Cussatt's class at Clemson.

**Paper mode only.** Nothing here places bets or orders. Every pick is logged for tracking and learning.

## How it works

```
GitHub Action (every ~2 h)            Scheduled Claude tasks                     Notion
──────────────────────────            ──────────────────────                     ──────
Kalshi public API  ─┐                 NFL picks Thu, check Sat, grade Tue        Bet Log per sport
The Odds API       ─┼─► */data/ ───►  NBA grade + picks daily               ───► Lessons & Settings
ESPN results       ─┘   (committed)   PGA picks Wed, grade Mon                   Hub page: record, this week's picks
                                         │  model edge → web research → pick or pass
                                         └► grading: result, closing price (CLV), post-mortem, lessons
```

1. **Relay** (`.github/workflows/nfl-kalshi.yml`). Pulls Kalshi markets, sportsbook odds and ESPN results, and commits snapshots to each sport's `data/` folder. The agents run where Kalshi isn't reachable but GitHub is, so this repo is how they see prices.
2. **Models** (one per sport). Each one turns past results into fair probabilities and compares them with every Kalshi contract after fees.
   - `nfl-kalshi/nfl_model.py`: ridge regression on nflverse team efficiency. Margins are weighted for NFL key numbers (3, 7, 10, 14).
   - `nba-kalshi/nba_model.py`: recency-weighted team ratings with home court and back-to-back terms.
   - `pga-kalshi/pga_model.py`: course-adjusted round scores and a 20,000-run tournament simulation.
3. **Research layer.** The models can't see injuries, lineup news, withdrawals or weather. A scheduled Claude task researches every flagged edge on the web and keeps a pick only if nothing explains the gap away.
4. **Grading and learning.** Each pick is graded on result and on closing line value (CLV): how the price we took compared with Kalshi's last quote before the start. CLV is the main measure of whether there's a real edge, because it needs far fewer bets than win/loss to mean something. Lessons and the scoreboard live on each sport's Notion "Lessons & Settings" page, which every picks run reads first.

## Repo layout

| Path | What's there |
|---|---|
| `common/kalshi_common.py` | Fee math (Kalshi rounds the fee on the whole order), odds conversion, name matching, shared by all three models |
| `common/kalshi_close.py` | Looks up Kalshi's last quote before a start time from its candlestick endpoint, for CLV. Run through the relay's `closes` input; writes `closes/closes.json` |
| `nfl-kalshi/` | Relay scripts for NFL and NBA (`fetch_kalshi.py`, `fetch_odds.py`, `odds_due.py`), the NFL model, NFL data |
| `nba-kalshi/` | NBA model, ESPN results collector, NBA data |
| `pga-kalshi/` | Golf relay, model, ESPN round collector, PGA data |
| `prompts/` | The scheduled agent prompts and a setup guide for running them on any AI platform |
| `tests/` | Fast offline tests, run on every pull request by `.github/workflows/checks.yml` |

Each sport folder has its own README with data file formats and model commands.

## Running it yourself

```
pip install -r requirements.txt
cd pga-kalshi && python pga_model.py events && python pga_model.py kalshi <EVENT>
cd nba-kalshi && python nba_model.py kalshi
cd nfl-kalshi && python nfl_model.py download 2026 && python nfl_model.py kalshi 2026 10
python -m pytest -q tests
```

Model settings are normally written to each folder's `config.json` from the Notion settings page at run time. Without one, the defaults in each model file are used.

## Recreate the agent yourself (any AI platform)

The code in this repo has no Claude-specific parts: the relay is a GitHub Action, the models are plain Python, and the agent's memory lives in Notion. The "agent" is a set of prompts that tell an AI what to read, run, research and log. They're in [`prompts/`](prompts/), one per scheduled run, written so they work on any platform that has:

- web search,
- a Python sandbox that can clone from GitHub,
- read and write access to Notion,
- scheduled runs (or you paste the prompt into a chat when it's due).

To set it up:

1. **Data.** Either read this repo's data as is (no setup), or fork it, enable Actions and add an `ODDS_API_KEY` secret to run your own relay.
2. **Notion.** Make your own copy of the hub page, three Bet Logs and three Lessons & Settings pages. Don't point your agent at someone else's pages, or it will write into their Bet Log and record.
3. **Prompts.** Fill in the `{{...}}` placeholders (your repo and your Notion page IDs) and schedule them.

[`prompts/README.md`](prompts/README.md) has the full walkthrough: the Notion database properties, starting model settings and betting rules, and what to skip if your platform can't do something.

## Honest results so far

None of the three models beats the market's closing line on its own in backtests:

| Model | Backtest result |
|---|---|
| NFL | 51.9% against the spread at a 4-point edge, against a 52.4% break-even |
| NBA | 47-49% against the spread |
| PGA | Better than chance, and top-10 probabilities are well calibrated, but it underrates the top tier of a field |

That's why every pick has to survive the research step, and why the agent tracks CLV rather than just wins and losses. The live paper record is on the Notion hub.
