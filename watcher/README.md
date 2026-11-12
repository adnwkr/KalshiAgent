# Exit watcher (paper alerts)

Watches every **Pending** pick logged at **Kalshi** in the NFL, NBA and PGA Bet Logs and sends a phone push through [ntfy](https://ntfy.sh) when selling early would lock in a gain. For this test, every suggested pick counts as bought at its logged all-in `Cost (c)`, with the same flat $10 order as the Notion hub record.

**Alerts only.** It never places orders and only reads from Notion and Kalshi. Sportsbook picks have no Kalshi contract to price, so they're skipped.

## How it works

`.github/workflows/exit-watcher.yml` runs `watcher.py` every 10 minutes (GitHub sometimes runs scheduled jobs a few minutes late):

1. Read Pending Kalshi picks from the three Bet Logs (Notion API, read-only).
2. Get each contract's current bid from Kalshi's public API (no account needed).
3. Work out what selling now would net per contract after Kalshi's fee, against the cost paid.
4. Check the exit rules and push an alert the first time each rule trips for a pick.

| Rule (in `config.json`) | Default | Fires when |
|---|---|---|
| `take_profit_cents` | 15 | Selling now nets at least 15c per contract |
| `trail_arm_cents`, `trail_drop_cents` | 10, 8 | The gain once reached 10c+, then fell 8c+ from its peak |
| `pregame_minutes`, `pregame_min_gain_cents` | 30, 5 | Start is within 30 minutes and the position is up 5c+ |
| `watch_after_start` | NFL/NBA off, PGA on | Whether to keep watching once the game or tournament has started |

State (peak gains, alerts already sent, and a log of every "would have sold" alert) is carried between runs in the Actions cache as `watcher/state.json`. Each run's table of picks, bids and gains shows on the run's summary page in the Actions tab.

## Setup

1. **ntfy:** subscribe to your topic in the ntfy app, then add it as the repo secret `NTFY_TOPIC` (Settings > Secrets and variables > Actions).
2. **Notion:** at notion.so/profile/integrations, create an internal integration with only **Read content** access and copy its secret. On the "Kalshi Betting Agents" hub page, open the ... menu > Connections and add the integration (the sport pages and Bet Logs inherit it). Add the secret as the repo secret `NOTION_TOKEN`.
3. **Test:** Actions tab > "Exit watcher (paper alerts)" > Run workflow, with "Only send a test push" ticked. A test push should arrive. Then run it again unticked and check the run summary lists your pending picks.

Until both secrets exist, the workflow skips without failing.

## Run it locally

```
NOTION_TOKEN=... NTFY_TOPIC=... python watcher/watcher.py --dry-run
```

`--dry-run` prints the alerts instead of sending them and doesn't save state. Tests: `python -m pytest -q tests/test_watcher.py`.
