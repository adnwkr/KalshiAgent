# Agent prompts and setup

This folder has everything you need to run the betting agents yourself, on Claude or any other AI platform. The Python code in this repo does the data work and the math. The prompts here are the "agent" part: they tell an AI what to read, what to run, what to research and what to write back.

| Prompt | When it runs (US Eastern) | What it does |
|---|---|---|
| [nfl-picks.md](nfl-picks.md) | Thu 8:43 AM, Sep to Feb | Runs the NFL model, researches each edge, logs picks |
| [nfl-saturday-check.md](nfl-saturday-check.md) | Sat 10:14 AM, Sep to Feb | Re-checks picks after Friday's injury report, voids broken ones |
| [nfl-grade.md](nfl-grade.md) | Tue 8:40 AM, Sep to Feb | Grades the week, closing line value, scoreboard, lessons |
| [nba-daily.md](nba-daily.md) | Daily 1:08 PM, Oct to Jun | Grades yesterday, then picks today |
| [pga-picks.md](pga-picks.md) | Wed 12:15 PM | Picks for this week's tournament |
| [pga-grade.md](pga-grade.md) | Mon 9:43 AM | Grades last week's tournament |

You don't need all six. One sport's picks prompt plus its grading prompt is a complete agent.

## 1. What your AI platform needs

| Capability | Used for | If your platform doesn't have it |
|---|---|---|
| Web search and page fetch | The research step (injuries, lineups, weather, withdrawals) | Not optional. Without research the agent is just the model, and the model alone doesn't beat the market. |
| A code sandbox with Python, git and internet | Cloning this repo, `pip install`, running the models | Run the model commands yourself and paste the JSON output into the chat. |
| Notion read and write | The Bet Log, Lessons & Settings and hub pages | Connect Notion through your platform's integration or connector, and share your pages with it. |
| Scheduled runs | Running the prompts automatically | Paste the prompt into a new chat at the scheduled time. |
| GitHub Actions write access (optional) | Restarting the data relay when GitHub skips a run, and exact Kalshi closing prices | Skip it. The prompts say to continue with the data already in the repo. |

## 2. Get the data

The agents read prices from this repo, not from Kalshi directly, because many AI sandboxes can't reach Kalshi but can reach GitHub. Pick one option:

**Option A: read this repo (easiest).** Use `adnwkr/KalshiAgent` as `{{REPO}}`. It's public, so no setup is needed. You can't start its relay, so skip the relay steps. Grading falls back to the 2-hourly price history for closing prices, which is a little less exact.

**Option B: run your own relay (full features).**
1. Fork this repo on GitHub.
2. In your fork's **Actions** tab, enable workflows. GitHub turns off Actions and scheduled runs on forks until you do.
3. Get a free API key from [The Odds API](https://the-odds-api.com) (500 credits a month, and this schedule uses about 340). Add it under **Settings > Secrets and variables > Actions** as `ODDS_API_KEY`. Without it the relay still pulls Kalshi and ESPN data but skips sportsbook odds.
4. Run the "Kalshi relay (NFL + NBA + PGA)" workflow once by hand to check that it commits fresh data.
5. Use `your-username/KalshiAgent` as `{{REPO}}`. If your AI can call the GitHub API, give it a fine-grained token with **Actions: read and write** on your fork so it can start the relay.

## 3. Set up Notion

Each person needs their **own** copy of the Notion pages. If you point the prompts at someone else's pages, your agent will write picks into their Bet Log and change their record.

**Fastest way:** if you have access to the original "Kalshi Betting Agents" page, use **Duplicate** (or "Duplicate to" another workspace) on the hub page. That copies the sport pages, Bet Logs and Lessons pages. In the copy:
- Delete all rows in each Bet Log.
- On each Lessons & Settings page, clear the Scoreboard, Lessons and Changelog sections. Keep Model settings, Betting rules and Backtest baseline.
- Reset the Betting record table on the hub to 0-0.

**Or build it from scratch.** Make this structure:

```
Kalshi Betting Agents        (hub page)
├── NFL Betting Agent        (page)
│   ├── NFL Bet Log          (database)
│   └── NFL Lessons & Settings (page)
├── NBA Betting Agent
│   ├── NBA Bet Log
│   └── NBA Lessons & Settings
└── PGA Betting Agent
    ├── PGA Bet Log
    └── PGA Lessons & Settings
```

### Hub page

Add a table with the heading **Betting record**:

| Sport | Record (W-L) | Wagered | Profit | ROI | Avg CLV |
|---|---|---|---|---|---|
| 🏈 NFL | 0-0 | $0 | $0.00 | n/a | No graded picks yet |
| 🏀 NBA | 0-0 | $0 | $0.00 | n/a | No graded picks yet |
| ⛳ PGA | 0-0 | $0 | $0.00 | n/a | No graded picks yet |
| **Total** | 0-0 | $0 | $0.00 | n/a | |

Under it, add a "Last updated" date. The grading prompts update this table assuming a flat $10 on every bet. Optionally add a linked view of each Bet Log filtered to Pending picks plus picks graded in the last 7 days, so the hub shows this week's picks.

### Bet Log databases

All three share these properties:

| Property | Type | Notes |
|---|---|---|
| Pick | Title | Plain language plus the contract or book |
| Pick ID | ID (auto-increment) | |
| Market | Select | NFL/NBA: Spread, Total, Moneyline. PGA: Winner, Top 5, Top 10, Top 20, Make cut, R1 leader |
| Side | Select | NFL/NBA: home, away, over, under. PGA: Yes, No |
| Odds | Number | American odds at time of pick |
| Stake (u) | Number | |
| Venue | Select | Kalshi, Sportsbook |
| Kalshi Ticker | Text | Ticker plus YES/NO |
| Cost (c) | Number | All-in Kalshi cost in cents, fee included |
| Model Prob | Number | 0 to 1 |
| Confidence | Select | Low, Medium, High |
| Factors | Multi-select | See below |
| Reasoning | Text | |
| Sources | Text | |
| Status | Select | Pending, Win, Loss, Push, Void (PGA: Dead heat instead of Push) |
| Mode | Select | Paper, Real |
| Units | Number | |
| Closing Price (c) | Number | Kalshi price just before the start |
| Post-mortem | Text | |

Then add the sport-specific ones:

- **NFL:** Season (Number), Week (Number), Game ID (Text, nflverse id like `2026_08_BAL_BUF`), Kickoff (Date with time), Line (Number), Model Fair Line (Number), Model Edge (pts) (Number), Closing Line (Number), CLV (pts) (Number), Final Score (Text). Factors: Model edge, Injury, QB change, Weather, Line move, Rest/Travel, Matchup, Situational.
- **NBA:** Season (Number, start year), Game ID (Text, like `2026-11-06 PHI@NYK`), Tip-off (Date with time), Line, Model Fair Line, Model Edge (pts), Closing Line, CLV (pts) (all Number), Final Score (Text). Factors: Model edge, Injury, Rest/Back-to-back, Load management, Line move, Matchup, Travel, Situational.
- **PGA:** Tournament (Text, name plus Kalshi event code like `The Masters (MAST27)`), Start (Date), Golfer (Text), Market Prob (Number), Edge (pct pts) (Number), CLV (c) (Number), Finish (Text, like T7 or MC). Factors: Model edge, Course fit, Recent form, Weather/Wave, Injury/WD, Line move, Situational.

### Lessons & Settings pages

This page is the agent's memory. Every picks run reads it first and every grading run updates it. Use these sections, in this order:

1. **Model settings**: a JSON code block. The prompts save it as `config.json` next to the model. Start with the defaults below.
2. **Betting rules**: a bullet list (starter rules below).
3. **Scoreboard**: "(Updated by the grading run.)"
4. **Lessons**: "None yet." Each lesson needs a sample size, and under 15 picks it's a "watch", not a rule.
5. **Backtest baseline**: copy the numbers from the main README's "Honest results so far" or run the model's `backtest` command.
6. **Changelog**: one line, "Page created. Paper mode."

Starting Model settings:

```json
// NFL
{"halflife_games": 6, "season_carryover": 0.6, "margin_sigma": 13.5, "total_sigma": 13.0, "ridge_alpha": 5.0,
 "min_edge_spread": 4.0, "min_edge_total": 4.0, "kalshi_fee_coef": 0.07, "min_edge_cents": 6.0, "key_numbers": true, "order_dollars": 10}

// NBA
{"halflife_days": 90, "season_carryover": 0.5, "ridge_lambda": 2.0, "margin_sigma": 14.5, "total_sigma": 18.5,
 "min_edge_spread": 3.0, "min_edge_total": 4.0, "kalshi_fee_coef": 0.07, "min_edge_cents": 6.0}

// PGA
{"halflife_days": 240, "shrink_rounds": 10, "prior_skill": 1.0, "round_sd": 2.85, "skill_sd": 0.45, "cut_size": 65,
 "no_cut_field": 80, "n_sims": 20000, "default_field": 132, "major_field": 90, "kalshi_fee_coef": 0.07,
 "min_edge_cents": 3.0, "min_roi": 0.2, "min_cost": 2, "max_cost": 90, "min_rounds_eff": 20, "order_dollars": 10}
```

(Leave the `// NFL` comment lines out of the Notion code block; JSON doesn't allow comments.)

Starter Betting rules (all sports; edit to taste):

- Mode: PAPER. Log every pick with Mode = Paper until you change this line.
- Max picks: NFL 4 per week, NBA 3 per day, PGA 3 per tournament, counting picks already logged. Zero picks is fine.
- A pick needs a model edge at or above the threshold AND no research finding that explains the gap away (for example a starter ruled out).
- Research can create a pick without a model edge only for a confirmed late injury or lineup change the line hasn't absorbed. Tag it Situational and stake 0.5u.
- Stakes: 1u standard, 0.5u for Low confidence, 1.5u max for High. Never chase losses.
- Pricing source: Kalshi first, from the relay's `data/lines.json`. If the snapshot is more than 6 hours old, say so and fall back to sportsbook lines.
- Kalshi spreads and totals are ladders. Log only the single best rung per game and market. In golf, log one market per golfer per tournament.
- Line shopping: compare the Kalshi all-in cost with the best book price in `data/odds.json` and log whichever is better.
- Venues: log picks only at Kalshi or US state-licensed books (DraftKings, FanDuel, BetMGM, Caesars, ESPN Bet, Fanatics, BetRivers). Offshore books in the feed (Pinnacle, Bovada, LowVig) are price signals only.
- Market check: if the model disagrees with Pinnacle or the no-vig consensus by 3+ points, a pick needs a specific research reason the market is wrong.
- NBA only, optional "Lean" tier: an edge that fails only the market check may be logged at 0.5u, Low confidence, with "Lean" at the start of the Reasoning.
- PGA only: check how Kalshi settles ties (dead heats) near a top-N cutoff and how it handles withdrawals. Golfers with thin PGA Tour history are priced but never flagged.

## 4. Fill in the placeholders

Each prompt uses these placeholders. Replace them with your own values (a Notion page's ID is the 32-character string at the end of its URL; for a database, use its URL or ID).

| Placeholder | Value |
|---|---|
| `{{REPO}}` | `adnwkr/KalshiAgent` (option A) or your fork (option B) |
| `{{HUB_PAGE}}` | Your hub page |
| `{{NFL_LESSONS_PAGE}}`, `{{NBA_LESSONS_PAGE}}`, `{{PGA_LESSONS_PAGE}}` | Each Lessons & Settings page |
| `{{NFL_BET_LOG}}`, `{{NBA_BET_LOG}}`, `{{PGA_BET_LOG}}` | Each Bet Log database |

## 5. Run it

Schedule each prompt on your platform at the times in the table above, or paste it into a new chat when it's due. Do a test run first: the picks prompt should read your Lessons page, run the model, and either log picks or explain why it passed. Then check that the rows look right in your Bet Log.

A few things that trip people up:
- **Season years.** NFL and NBA seasons are named by the year they start, so the 2026-27 NBA season is `2026`.
- **Paper mode.** Nothing here places bets. Keep it that way unless you understand the risks and the laws where you live.
- **Small samples.** A 5-2 week means very little. The grading prompts judge picks mostly on closing line value (did the price move your way before the start?), which needs far fewer bets to mean something.
