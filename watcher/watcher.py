"""Exit-alert watcher for the paper picks (v1: alerts only, never trades).

Every run:
  1. Reads Pending picks from the NFL, NBA and PGA Bet Logs in Notion (read-only).
  2. Keeps the picks logged at Kalshi with a ticker. For this test every suggested pick counts
     as bought at its logged all-in Cost (c), with a flat $10 order like the Notion hub record.
  3. Looks up each contract's current bid on Kalshi's public API (no account or keys needed).
  4. Applies the exit rules in watcher/config.json and pushes an ntfy alert when one trips.

It never places orders and never writes to Notion or Kalshi. Its state (peak prices, alerts
already sent, and the "would have sold" log) lives in watcher/state.json, which the workflow
carries between runs with the Actions cache.

Usage:
  python watcher/watcher.py                 normal run
  python watcher/watcher.py --dry-run       print what would be sent; send nothing, save nothing
  python watcher/watcher.py --test-notify   send one test push and exit

Environment: NOTION_TOKEN (Notion integration secret), NTFY_TOPIC, optional NTFY_SERVER.
"""
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
NOTION = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
LOG_LIMIT = 500

RULE_LABELS = {
    "take_profit": "Take profit",
    "trailing_stop": "Trailing stop",
    "pregame_lock": "Lock in before start",
}


# ---------------------------------------------------------------- HTTP helpers

def http_json(url, data=None, headers=None, tries=3):
    body = json.dumps(data).encode() if data is not None else None
    hdrs = {"Accept": "application/json", "User-Agent": "kalshi-exit-watcher"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


# ---------------------------------------------------------------- Notion (read-only)

def notion_pending(token, database_id):
    """All Pending rows logged at Kalshi in one Bet Log."""
    url = f"{NOTION}/databases/{database_id}/query"
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}
    query = {"filter": {"and": [
        {"property": "Status", "select": {"equals": "Pending"}},
        {"property": "Venue", "select": {"equals": "Kalshi"}},
    ]}, "page_size": 100}
    rows, cursor = [], None
    while True:
        if cursor:
            query["start_cursor"] = cursor
        d = http_json(url, data=query, headers=headers)
        rows += d.get("results", [])
        if not d.get("has_more"):
            return rows
        cursor = d.get("next_cursor")


def _text(prop):
    if not prop:
        return ""
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def parse_ticker(raw):
    """'KXNBASPREAD-26NOV06PHINYK-NYK4 NO' -> ('KXNBASPREAD-26NOV06PHINYK-NYK4', 'no')."""
    bits = (raw or "").replace(",", " ").split()
    if len(bits) < 2 or bits[-1].upper() not in ("YES", "NO"):
        return None, None
    return bits[0].upper(), bits[-1].lower()


def parse_start(value):
    """Notion date start -> aware UTC datetime. A date with no time (PGA) is taken as 11:00 UTC."""
    if not value:
        return None
    if len(value) == 10:
        return datetime.fromisoformat(value + "T11:00:00+00:00")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_pick(sport, page, start_property):
    props = page.get("properties", {})
    ticker, side = parse_ticker(_text(props.get("Kalshi Ticker")))
    cost = (props.get("Cost (c)") or {}).get("number")
    uid = (props.get("Pick ID") or {}).get("unique_id") or {}
    date = (props.get(start_property) or {}).get("date") or {}
    return {
        "id": page["id"],
        "sport": sport,
        "pick_id": f"{uid.get('prefix') or sport}-{uid.get('number')}" if uid.get("number") else sport,
        "pick": _text(props.get("Pick")),
        "ticker": ticker,
        "side": side,
        "cost": cost,
        "start": parse_start(date.get("start")),
        "url": page.get("url", ""),
    }


# ---------------------------------------------------------------- Kalshi (public data)

def cents(m, name):
    """Kalshi has used both integer-cent fields (yes_bid) and dollar strings (yes_bid_dollars)."""
    v = m.get(name)
    if v is None and m.get(f"{name}_dollars") is not None:
        try:
            v = round(float(m[f"{name}_dollars"]) * 100, 2)
        except ValueError:
            v = None
    return v


def kalshi_market(ticker):
    return http_json(f"{KALSHI}/markets/{urllib.parse.quote(ticker)}").get("market", {})


def sell_price(m, side):
    """Best price we could sell our side at right now, in cents, or None if there's no bid."""
    if side == "yes":
        bid = cents(m, "yes_bid")
    else:
        bid = cents(m, "no_bid")
        if bid is None:
            ask = cents(m, "yes_ask")
            bid = 100 - ask if ask is not None else None
    return bid if bid is not None and 0 < bid < 100 else None


def tradable(m):
    return str(m.get("status", "")).lower() in ("active", "open")


# ---------------------------------------------------------------- the math and the rules

def contracts(cost, order_dollars):
    return max(1, int((order_dollars * 100) // cost))


def sell_fee(bid, n, coef):
    """Kalshi taker fee per contract, in cents, for selling n contracts at `bid` cents.
    Kalshi charges ceil(coef * n * P * (1 - P)) dollars, rounded up to the cent, on the order."""
    p = bid / 100
    return math.ceil(round(coef * n * p * (1 - p) * 100, 6)) / n


def net_gain(cost, bid, cfg):
    """Cents per contract we'd keep if we sold now, after the sell fee, vs. the all-in cost."""
    n = contracts(cost, cfg["order_dollars"])
    return round(bid - sell_fee(bid, n, cfg["kalshi_fee_coef"]) - cost, 2), n


def evaluate(pick, bid, entry, now, cfg):
    """Apply the exit rules to one pick. Returns (alerts, updated state entry). Pure: no I/O."""
    entry = dict(entry or {})
    sent = set(entry.get("alerts", []))
    net, n = net_gain(pick["cost"], bid, cfg)
    peak = max(entry.get("peak_net", net), net)
    entry.update(peak_net=peak, last_bid=bid, last_net=net, last_seen=now.isoformat(timespec="seconds"))

    fired = []
    if net >= cfg["take_profit_cents"]:
        fired.append("take_profit")
    if peak >= cfg["trail_arm_cents"] and peak - net >= cfg["trail_drop_cents"]:
        fired.append("trailing_stop")
    start = pick.get("start")
    if start and timedelta(0) <= start - now <= timedelta(minutes=cfg["pregame_minutes"]) \
            and net >= cfg["pregame_min_gain_cents"]:
        fired.append("pregame_lock")

    alerts = []
    for rule in fired:
        if rule in sent:
            continue
        sent.add(rule)
        alerts.append({
            "time": now.isoformat(timespec="seconds"), "rule": rule, "sport": pick["sport"],
            "pick_id": pick["pick_id"], "pick": pick["pick"], "ticker": pick["ticker"],
            "side": pick["side"], "cost": pick["cost"], "bid": bid, "net_cents": net,
            "peak_net_cents": peak, "contracts": n, "pnl_dollars": round(net * n / 100, 2),
            "url": pick.get("url", ""),
        })
    entry["alerts"] = sorted(sent)
    return alerts, entry


def should_watch(pick, now, cfg):
    if not pick["ticker"] or not pick["side"] or not pick["cost"]:
        return False, "no Kalshi ticker, side or cost"
    start = pick.get("start")
    if start and now > start and not cfg["watch_after_start"].get(pick["sport"], False):
        return False, "already started"
    return True, ""


# ---------------------------------------------------------------- notifications

def notify(alert, dry_run=False):
    rule = RULE_LABELS.get(alert["rule"], alert["rule"])
    title = f"Kalshi exit alert: {rule} ({alert['pick_id']})"
    sign = "+" if alert["net_cents"] >= 0 else ""
    body = (f"{alert['pick']}\n"
            f"{alert['side'].upper()} bid {alert['bid']:g}c, paid {alert['cost']:g}c all-in\n"
            f"Sell now: {sign}{alert['net_cents']:g}c per contract after fee, "
            f"{sign}${alert['pnl_dollars']:.2f} on the $10 paper bet\n"
            f"Rule: {rule}. Paper test only, nothing was sold.")
    send(title, body, click=alert.get("url"),
         priority="high" if alert["rule"] != "trailing_stop" else "default",
         tags="chart_with_upwards_trend" if alert["net_cents"] >= 0 else "warning",
         dry_run=dry_run)


def send(title, body, click=None, priority="default", tags="", dry_run=False):
    if dry_run:
        print(f"[dry run] {title}\n{body}\n")
        return
    topic = os.environ["NTFY_TOPIC"]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=30):
        pass


# ---------------------------------------------------------------- run

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    print(text)
    if path:
        with open(path, "a") as f:
            f.write(text)


def main(argv):
    dry_run = "--dry-run" in argv
    if "--test-notify" in argv:
        send("Kalshi watcher test", "If you can read this, exit alerts will reach this device.",
             tags="white_check_mark", dry_run=dry_run)
        print("test notification sent" if not dry_run else "test notification printed")
        return 0

    cfg = load_json(CONFIG_PATH, None)
    state = load_json(STATE_PATH, {"positions": {}, "log": []})
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.exit("NOTION_TOKEN is not set")
    now = datetime.now(timezone.utc)

    picks, errors = [], []
    for sport, log in cfg["bet_logs"].items():
        try:
            rows = notion_pending(token, log["database_id"])
            picks += [parse_pick(sport, r, log["start_property"]) for r in rows]
        except Exception as e:
            errors.append(f"{sport} Bet Log: {e}")

    table = ["| Pick | Side | Paid | Bid | Net/contract | Peak | Note |", "|---|---|---|---|---|---|---|"]
    watched, alerts = set(), []
    for p in picks:
        ok, why = should_watch(p, now, cfg)
        if not ok:
            table.append(f"| {p['pick_id']} {p['pick'][:40]} | {p['side'] or ''} | {p['cost'] or ''} | | | | {why} |")
            continue
        try:
            m = kalshi_market(p["ticker"])
        except Exception as e:
            errors.append(f"{p['ticker']}: {e}")
            continue
        bid = sell_price(m, p["side"])
        if not tradable(m) or bid is None:
            note = "market closed" if not tradable(m) else "no bid"
            table.append(f"| {p['pick_id']} {p['pick'][:40]} | {p['side']} | {p['cost']} | | | | {note} |")
            continue
        watched.add(p["id"])
        new, state["positions"][p["id"]] = evaluate(p, bid, state["positions"].get(p["id"]), now, cfg)
        e = state["positions"][p["id"]]
        table.append(f"| {p['pick_id']} {p['pick'][:40]} | {p['side']} | {p['cost']} | {bid:g} | "
                     f"{e['last_net']:+g}c | {e['peak_net']:+g}c | {', '.join(a['rule'] for a in new)} |")
        alerts += new

    for a in alerts:
        try:
            notify(a, dry_run=dry_run)
        except Exception as e:
            errors.append(f"ntfy: {e}")

    if not dry_run:
        state["positions"] = {k: v for k, v in state["positions"].items() if k in watched}
        state["log"] = (state.get("log", []) + alerts)[-LOG_LIMIT:]
        state["last_run"] = now.isoformat(timespec="seconds")
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=1)

    lines = [f"## Exit watcher {now:%Y-%m-%d %H:%M} UTC",
             f"{len(picks)} pending Kalshi picks, {len(watched)} priced, {len(alerts)} alerts sent."]
    if picks:
        lines += [""] + table
    if errors:
        lines += ["", "**Problems:**"] + [f"- {e}" for e in errors]
    summary(lines)
    return 1 if errors and not watched and not picks else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
