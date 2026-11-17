# Demo replay (class video)

Replays one past agent run so the full loop can be recorded: model edges, research, Notion log, push.

Picks (from the live Bet Logs, with the research saved at the time): NFL #6 NO -7.5 vs CLE, NFL #3 CIN -9.5 vs ATL, NBA #6 HOU -18.5 vs UTA, PGA #5 Dumont de Chassart to miss the cut. They are logged to the separate **Demo Run** Bet Logs in Notion, so the live record isn't affected.

Run from the Actions tab: **Demo replay (class video) > Run workflow**, then pick a step:

1. `scan`: reprices each pick from the relay's history snapshot (Kalshi candlesticks if the snapshot didn't keep that market), applies the fee and flags edges over the bar.
2. `research`: prints the research saved for each flagged edge.
3. (the agent logs the picks to the Demo Run Bet Logs in Notion)
4. `notify`: reads Pending demo picks from Notion and sends the new-pick push to the picks topic.

To reset for another take, delete the rows in the three Demo Bet Logs.
