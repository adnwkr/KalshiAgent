"""Offline checks for the exit-alert watcher (watcher/watcher.py). No network needed."""
import importlib.util
import os
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("watcher", os.path.join(ROOT, "watcher", "watcher.py"))
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)

CFG = {"order_dollars": 10, "kalshi_fee_coef": 0.07, "take_profit_cents": 15, "trail_arm_cents": 10,
       "trail_drop_cents": 8, "pregame_minutes": 30, "pregame_min_gain_cents": 5,
       "watch_after_start": {"NFL": False, "NBA": False, "PGA": True}}
NOW = datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)


def pick(cost=44.0, start=None, sport="NFL"):
    return {"id": "p1", "sport": sport, "pick_id": f"{sport}-1", "pick": "BUF -2.5 at KC", "ticker": "KXT-1",
            "side": "yes", "cost": cost, "start": start or NOW + timedelta(days=1), "url": ""}


def test_parse_ticker():
    assert w.parse_ticker("KXNBASPREAD-26NOV06PHINYK-NYK4 NO") == ("KXNBASPREAD-26NOV06PHINYK-NYK4", "no")
    assert w.parse_ticker("kxpgatop10-mast27-ssch yes") == ("KXPGATOP10-MAST27-SSCH", "yes")
    assert w.parse_ticker("") == (None, None)
    assert w.parse_ticker("KXNFLGAME-26NOV15BUFKC-BUF") == (None, None)


def test_sell_price_sides():
    m = {"yes_bid": 60, "yes_ask": 62, "no_bid": 38}
    assert w.sell_price(m, "yes") == 60
    assert w.sell_price(m, "no") == 38
    assert w.sell_price({"yes_ask_dollars": "0.62"}, "no") == 38
    assert w.sell_price({"yes_bid": 0, "yes_ask": 100}, "yes") is None


def test_sell_fee_is_charged_on_the_order():
    # 22 contracts at 62c: ceil(0.07 * 22 * .62 * .38 * 100) = 37c total, about 1.68c each
    assert abs(w.sell_fee(62, 22, 0.07) - 37 / 22) < 1e-9


def test_take_profit_fires_once():
    alerts, entry = w.evaluate(pick(), 62, None, NOW, CFG)
    assert [a["rule"] for a in alerts] == ["take_profit"]
    assert alerts[0]["net_cents"] >= 15 and alerts[0]["contracts"] == 22
    again, _ = w.evaluate(pick(), 63, entry, NOW, CFG)
    assert again == []


def test_no_alert_on_small_move():
    alerts, entry = w.evaluate(pick(), 50, None, NOW, CFG)
    assert alerts == [] and entry["peak_net"] < 15


def test_trailing_stop_after_peak():
    _, entry = w.evaluate(pick(), 57, None, NOW, CFG)       # about +11.8c: armed, no take profit
    alerts, entry = w.evaluate(pick(), 48, entry, NOW, CFG)  # gave back 8c+ from the peak
    assert [a["rule"] for a in alerts] == ["trailing_stop"]
    assert entry["peak_net"] > entry["last_net"]


def test_pregame_lock():
    soon = pick(start=NOW + timedelta(minutes=20))
    alerts, _ = w.evaluate(soon, 51, None, NOW, CFG)
    assert [a["rule"] for a in alerts] == ["pregame_lock"]


def test_should_watch_skips_started_games_but_not_golf():
    started = NOW - timedelta(hours=1)
    assert w.should_watch(pick(start=started), NOW, CFG)[0] is False
    assert w.should_watch(pick(start=started, sport="PGA"), NOW, CFG)[0] is True
    assert w.should_watch(dict(pick(), ticker=None), NOW, CFG)[0] is False


def test_parse_pick_from_notion_row():
    page = {"id": "abc", "url": "https://notion.so/abc", "properties": {
        "Pick": {"title": [{"plain_text": "Spurs -14.5 at Bulls"}]},
        "Kalshi Ticker": {"rich_text": [{"plain_text": "KXNBASPREAD-26NOV11SASCHI-SAS14 YES"}]},
        "Cost (c)": {"number": 54.8},
        "Pick ID": {"unique_id": {"prefix": "NBA", "number": 5}},
        "Tip-off": {"date": {"start": "2026-11-12T01:00:00.000Z"}},
    }}
    p = w.parse_pick("NBA", page, "Tip-off")
    assert p["ticker"] == "KXNBASPREAD-26NOV11SASCHI-SAS14" and p["side"] == "yes"
    assert p["cost"] == 54.8 and p["pick_id"] == "NBA-5"
    assert p["start"] == datetime(2026, 11, 12, 1, 0, tzinfo=timezone.utc)
    assert w.parse_start("2026-11-13").hour == 11


def _p(pid, sport="NFL", venue="Kalshi"):
    return {"id": pid, "sport": sport, "pick_id": f"{sport}-{pid}", "pick": f"Pick {pid}", "venue": venue,
            "side": "yes", "cost": 30.0, "odds": 233, "stake": 0.5, "confidence": "Low",
            "reasoning": "Model likes it. More detail here.", "start": NOW + timedelta(days=2)}


def test_new_picks_first_run_is_silent():
    new, seen = w.find_new_picks([_p("a"), _p("b")], {}, ["NFL"])
    assert new == [] and sorted(seen["NFL"]) == ["a", "b"]


def test_new_picks_detects_only_new_ones():
    new, seen = w.find_new_picks([_p("a"), _p("c", venue="Sportsbook")], {"NFL": ["a", "b"]}, ["NFL"])
    assert [p["id"] for p in new] == ["c"] and sorted(seen["NFL"]) == ["a", "c"]


def test_new_picks_keeps_list_when_a_bet_log_fails():
    new, seen = w.find_new_picks([], {"NFL": ["a"], "NBA": ["x"]}, ["NBA"])
    assert new == [] and seen["NFL"] == ["a"] and seen["NBA"] == []


def test_format_new_picks():
    title, body = w.format_new_picks("NFL", [_p("a"), _p("b", venue="Sportsbook")])
    assert title == "New NFL picks (2)" and title.isascii()
    assert "Kalshi YES at 30c" in body and "Sportsbook +233" in body
    assert "Why: Model likes it." in body and "Not betting advice" in body
