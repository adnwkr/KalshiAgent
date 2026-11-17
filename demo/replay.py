"""Demo replay: re-runs a past agent run step by step for the class demo video.

The live agent's steps are: read Kalshi prices from the relay, compare them with the model,
flag edges, research each one, log picks to Notion, and announce new picks on ntfy. This
script replays those steps for the picks in demo/picks.json using the data saved at the time:

  python demo/replay.py scan       reprice each pick from the relay's history snapshot (or Kalshi's
                                   candlesticks when the snapshot didn't keep that market), apply
                                   the fee, compare with the model and flag edges
  python demo/replay.py research   print the research the agent saved for each flagged edge
  python demo/replay.py notify     read Pending rows from the Demo Run Bet Logs in Notion and send
                                   the new-pick push to NTFY_PICKS_TOPIC (same format as the watcher)
  add --dry-run to notify to print the push instead of sending it

It never touches the live Bet Logs, the watcher's state, or Kalshi orders.
Environment for notify: NOTION_TOKEN, NTFY_PICKS_TOPIC (optional NTFY_SERVER).
"""
import csv
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "common"))
from kalshi_common import fee, price_to_american  # noqa: E402

HISTORY = {s: os.path.join(ROOT, f"{s.lower()}-kalshi", "data", "history", "2026.csv") for s in ("NFL", "NBA", "PGA")}
ICON = {"NFL": "🏈", "NBA": "🏀", "PGA": "⛳"}


def load():
    with open(os.path.join(HERE, "picks.json")) as f:
        return json.load(f)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def say(text="", pause=0.0):
    print(text, flush=True)
    if pause:
        time.sleep(pause)


def summary(md):
    """Write Markdown to the Actions run page (shown without opening the log)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def snapshot_rows(sport, snapshot_iso):
    """All rows of the latest relay snapshot at or before snapshot_iso for one sport."""
    target = snapshot_iso.replace("Z", "+00:00")
    best, rows = None, []
    with open(HISTORY[sport], newline="") as f:
        for r in csv.DictReader(f):
            t = r["fetched_at"]
            if t > target:
                continue
            if best is None or t > best:
                best, rows = t, [r]
            elif t == best:
                rows.append(r)
    return best, rows


def quote_from_snapshot(rows, ticker):
    for r in rows:
        if r["ticker"] == ticker and r.get("yes_ask") not in (None, ""):
            return float(r["yes_bid"] or 0), float(r["yes_ask"])
    return None


def quote_from_candles(ticker, at_iso):
    try:
        close = load_module("kalshi_close", os.path.join(ROOT, "common", "kalshi_close.py"))
        q = close.close_for(ticker, at_iso)
        if "error" not in q:
            return q["yes_bid"], q["yes_ask"], q["as_of"]
    except Exception as e:
        say(f"     (candlestick lookup failed: {e})")
    return None


def scan(cfg):
    thr = cfg["edge_threshold_c"]
    flagged = 0
    table, loaded = [], []
    for sport in ("NFL", "NBA", "PGA"):
        picks = [p for p in cfg["picks"] if p["sport"] == sport]
        if not picks:
            continue
        snap_at, rows = snapshot_rows(sport, picks[0]["snapshot"])
        events = {r["event_ticker"] for r in rows}
        loaded.append(f"| {ICON[sport]} {sport} | {snap_at or 'n/a'} | {len(rows)} | {len(events)} |")
        say(f"\n{ICON[sport]} {sport}  relay snapshot {snap_at or 'n/a'}", 0.6)
        say(f"   {len(rows)} Kalshi contracts across {len(events)} markets loaded from "
            f"{sport.lower()}-kalshi/data/history", 0.6)
        say(f"   model prices from that day's run, repriced against this snapshot after Kalshi's fee "
            f"(edge bar {thr:g}c)", 1.0)
        for p in picks:
            say(f"\n   ▸ {p['game']}", 0.4)
            q = quote_from_snapshot(rows, p["ticker"])
            source = f"relay snapshot {snap_at}"
            if q is None:
                c = quote_from_candles(p["ticker"], p["snapshot"])
                if c:
                    q, source = (c[0], c[1]), f"Kalshi candlesticks {c[2]}"
            if q is None:
                raw = p.get("logged_raw_c")
                source = "price logged at the time (market not in the snapshot)"
            else:
                yes_bid, yes_ask = q
                raw = yes_ask if p["side"] == "yes" else round(100 - yes_bid, 2)
            if raw is None:
                say("     no price available, skipped")
                continue
            ladder = sorted({r["ticker"] for r in rows if r["event_ticker"] == p["ticker"].rsplit("-", 1)[0]})
            if len(ladder) > 1:
                say(f"     {len(ladder)} rungs listed on this game; best edge found on:", 0.4)
            all_in = round(raw + fee(raw), 2)
            prob = p["model_prob"] * 100
            edge = round(prob - all_in, 1)
            say(f"     {p['ticker']} {p['side'].upper()}")
            say(f"     ask {raw:g}c + fee {fee(raw):g}c = {all_in:g}c all-in ({price_to_american(all_in):+d})  [{source}]")
            if "model_fair_line" in p:
                say(f"     model fair line {p['model_fair_line']:+g} vs market {p['market_line']:+g}"
                    f"  ({p['edge_pts']:g} pts)")
            say(f"     model {prob:.1f}% to pay  vs  {all_in:g}c cost  ->  edge {edge:+.1f}c", 0.5)
            table.append(f"| {ICON[sport]} {p['pick'].split(' (')[0]} | `{p['ticker']}` {p['side'].upper()} | "
                         f"{raw:g}c + {fee(raw):g}c = **{all_in:g}c** | {prob:.1f}% | **{edge:+.1f}c** | "
                         f"{'⚑ Research' if edge >= thr else 'Pass'} |")
            if edge >= thr:
                flagged += 1
                say(f"     ⚑ FLAGGED for research: {p['pick']}", 0.8)
            else:
                say(f"     below the {thr:g}c bar, pass", 0.5)
    say(f"\n{flagged} edge(s) flagged. Next: research each one before logging.")
    summary("## 📡 Agent run: model vs. Kalshi\n\n"
            "Relay snapshots loaded (`*/data/history`):\n\n"
            "| Sport | Snapshot | Contracts | Markets |\n|---|---|---|---|\n" + "\n".join(loaded) +
            f"\n\n### Edges (bar: {thr:g}c after Kalshi's fee)\n\n"
            "| Pick | Contract | Ask + fee = all-in | Model | Edge | Result |\n|---|---|---|---|---|---|\n" +
            "\n".join(table) +
            f"\n\n**{flagged} edge(s) flagged for research.** Model prices are from that day's run; "
            "prices are from the saved snapshot.")


def research(cfg):
    md = ["## 🔎 Research on flagged edges\n"]
    for i, p in enumerate(cfg["picks"], 1):
        n = p["notion"]
        md.append(f"### {ICON[p['sport']]} {p['pick'].split(' (')[0]}\n\n{p['research']}\n\n" +
                  "".join(f"- {src}\n" for src in p.get("sources", [])) +
                  f"\n> ✅ Nothing explains the edge away. Log it: **{n['Confidence']} confidence, "
                  f"{n['Stake (u)']:g}u**, factors: {', '.join(n['Factors'])}\n")
        say(f"\n{ICON[p['sport']]} [{i}] {p['pick']}", 0.5)
        say("   " + p["research"], 1.0)
        for s in p.get("sources", []):
            say(f"   source: {s}")
        n = p["notion"]
        say(f"   verdict: nothing explains the edge away -> log it ({n['Confidence']} confidence, "
            f"{n['Stake (u)']:g}u)", 0.8)
    summary("\n".join(md))


def notify(cfg, dry_run=False):
    w = load_module("watcher", os.path.join(ROOT, "watcher", "watcher.py"))
    token = os.environ.get("NOTION_TOKEN")
    topic = os.environ.get("NTFY_PICKS_TOPIC")
    if not token:
        sys.exit("NOTION_TOKEN is not set")
    if not topic and not dry_run:
        sys.exit("NTFY_PICKS_TOPIC is not set")
    sent = 0
    summary("## 📣 New-pick push" + (" (dry run, not sent)" if dry_run else " sent to the picks topic") + "\n")
    for sport, log in cfg["demo_logs"].items():
        try:
            rows = w.notion_pending(token, log["database_id"])
        except Exception as e:
            say(f"{sport} Demo Bet Log: could not read ({e}). Is the Demo Run page shared with the "
                f"Notion integration behind NOTION_TOKEN?")
            continue
        picks = [w.parse_pick(sport, r, log["start_property"]) for r in rows]
        if not picks:
            say(f"{sport}: no pending demo picks")
            continue
        title, body = w.format_new_picks(sport, picks)
        say(f"{ICON[sport]} {sport}: {len(picks)} new pick(s) -> push")
        summary(f"### {ICON[sport]} {title}\n\n```\n{body}\n```\n")
        w.send(title, body, click=cfg.get("demo_page"), priority="default",
               tags="football" if sport == "NFL" else "basketball" if sport == "NBA" else "golf",
               dry_run=dry_run, topic=topic or "picks")
        sent += len(picks)
    say(f"\n{sent} pick(s) announced" + (" (dry run, nothing sent)" if dry_run else ""))


def main(argv):
    step = argv[1] if len(argv) > 1 else "scan"
    cfg = load()
    if step == "scan":
        scan(cfg)
    elif step == "research":
        research(cfg)
    elif step == "notify":
        notify(cfg, dry_run="--dry-run" in argv)
    elif step == "all":
        scan(cfg)
        research(cfg)
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
