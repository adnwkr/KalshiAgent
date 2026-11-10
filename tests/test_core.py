"""Fast checks that run on every pull request (see .github/workflows/checks.yml).

They use small synthetic inputs, so they need no network access and no downloaded data.
"""
import importlib
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "common"))

from kalshi_common import fee, norm_name, price_to_american  # noqa: E402


def load(folder, module):
    """Import a model module from its sport folder, without leftover config.json settings."""
    path = os.path.join(ROOT, folder)
    if path not in sys.path:
        sys.path.insert(0, path)
    cfg = os.path.join(path, "config.json")
    hidden = cfg + ".hidden"
    if os.path.exists(cfg):
        os.rename(cfg, hidden)
    try:
        return importlib.import_module(module)
    finally:
        if os.path.exists(hidden):
            os.rename(hidden, cfg)


# ----------------------------------------------------------------------------- shared helpers
def test_price_to_american():
    assert price_to_american(50) == -100
    assert price_to_american(25) == 300
    assert price_to_american(80) == -400


def test_fee_is_rounded_per_order_not_per_contract():
    # $10 at 45c buys 22 contracts: 0.07 * 22 * .45 * .55 = $0.381 -> 39c for the order, 1.77c each.
    assert fee(45) == pytest.approx(39 / 22, abs=0.01)
    # A single contract still rounds up to a whole cent.
    assert fee(45, order_dollars=0.45) == 2
    # Never more than the old per-contract rounding, and zero at the edges.
    for c in range(1, 100):
        assert 0 < fee(c) <= np.ceil(0.07 * c / 100 * (1 - c / 100) * 100)
    assert fee(0) == 0 and fee(100) == 0


def test_norm_name():
    assert norm_name("Ludvig Åberg") == "ludvig aberg"
    assert norm_name("Davis Love III") == "davis love"
    assert norm_name("Jordan L. Smith") == "jordan l smith"


# ----------------------------------------------------------------------------- NFL key numbers
def test_nfl_margin_distribution():
    m = load("nfl-kalshi", "nfl_model")
    m._KEY_RATIOS = None
    pmf = m.margin_pmf(3.0, 13.5)
    assert pmf.sum() == pytest.approx(1.0)
    # P(over 2.5) + P(under 2.5) = 1, and covering -3 excludes the push
    assert m.p_margin(3, 13.5, 2.5, ">") + m.p_margin(3, 13.5, 2.5, "<") == pytest.approx(1.0)
    win, push = m.p_margin(3, 13.5, 3, ">"), m.p_margin(3, 13.5, 3, "==")
    assert m.p_cover(3, 13.5, 3) == pytest.approx(win / (1 - push))
    # Home and away cover probabilities on the same whole-number line add to 1 once pushes are removed
    assert m.p_cover(3, 13.5, 3, "home") + m.p_cover(3, 13.5, 3, "away") == pytest.approx(1.0)


def test_nfl_key_numbers_from_history(tmp_path, monkeypatch):
    m = load("nfl-kalshi", "nfl_model")
    # Synthetic history where 3 is far more common than its neighbours
    margins = [3] * 50 + [-3] * 50 + list(range(-20, 21)) * 5
    (tmp_path / "games.csv").write_text("result\n" + "\n".join(map(str, margins)))
    monkeypatch.setattr(m, "DATA", str(tmp_path))
    m._KEY_RATIOS = None
    r = m.key_ratios()
    assert r[3] > 3 and r[2] < 1.5
    # The push on 3 is now much likelier than under a plain normal curve
    assert m.p_margin(3, 13.5, 3, "==") > 0.06
    m._KEY_RATIOS = None


# ----------------------------------------------------------------------------- PGA field and relay
def test_pga_field_not_padded_when_kalshi_lists_the_field():
    m = load("pga-kalshi", "pga_model")
    golfers = [{"name": f"Player {i}"} for i in range(116)]
    t = {"event_key": "TEST26", "title": "Test Open", "golfers": golfers,
         "markets": ["make_cut", "top10", "win"]}
    skills = {"skill": {f"star {i}": -2.0 for i in range(20)}, "n_eff": {f"star {i}": 50 for i in range(20)}}
    names, n_pad, _ = m.field_for(t, skills)
    assert n_pad == 0 and len(names) == 116


def test_pga_futures_still_padded():
    m = load("pga-kalshi", "pga_model")
    t = {"event_key": "MAST27", "title": "2027 Masters", "golfers": [{"name": "Player 1"}], "markets": ["win"]}
    skills = {"skill": {f"star {i}": -2.0 for i in range(200)}, "n_eff": {f"star {i}": 50 for i in range(200)}}
    names, n_pad, _ = m.field_for(t, skills)
    assert n_pad > 0


def test_pga_thin_history_golfers_are_never_flagged():
    m = load("pga-kalshi", "pga_model")
    assert m.CONFIG["min_rounds_eff"] >= 10


def test_relay_merges_name_variants_by_ticker():
    sys.path.insert(0, os.path.join(ROOT, "pga-kalshi"))
    f = importlib.import_module("fetch_pga")
    mk = lambda series, name: {"series_ticker": series, "event_ticker": f"{series}-TEST26",
                               "ticker": f"{series}-TEST26-JSMI", "yes_sub_title": name,
                               "event_title": "Test Open", "yes_bid": 10, "yes_ask": 12}
    out = f.summarize([mk("KXPGATOUR", "Jordan Smith"), mk("KXPGATOP10", "Jordan L. Smith"),
                       mk("KXPGAMAKECUT", "Jordan L. Smith")])
    assert out[0]["n_golfers"] == 1
    assert out[0]["golfers"][0]["name"] == "Jordan Smith"


# ----------------------------------------------------------------------------- closing prices
def test_close_picks_last_quote_before_start(monkeypatch, tmp_path):
    k = importlib.import_module("kalshi_close")
    start = "2026-11-08T18:00:00Z"
    t = int(datetime(2026, 11, 8, 18, tzinfo=timezone.utc).timestamp())

    def fake(ticker, s, e, interval):
        return [{"end_period_ts": t - 120, "yes_bid": {"close": 40}, "yes_ask": {"close": 42}, "price": {"close": 41}},
                {"end_period_ts": t - 60, "yes_bid": {"close_dollars": "0.4300"}, "yes_ask": {"close_dollars": "0.4500"},
                 "price": {}},
                {"end_period_ts": t + 60, "yes_bid": {"close": 90}, "yes_ask": {"close": 92}}]  # after start: ignored

    monkeypatch.setattr(k, "candles", fake)
    monkeypatch.setattr(k, "OUT", str(tmp_path / "closes.json"))
    k.main(f"KXNFLSPREAD-26NOV08CINATL-CIN10@{start}")
    c = json.load(open(tmp_path / "closes.json"))["KXNFLSPREAD-26NOV08CINATL-CIN10"]
    assert c["yes_bid"] == 43 and c["yes_ask"] == 45 and c["mid"] == 44 and c["minutes_before_start"] == 1


def test_same_player_and_nordic_letters():
    from kalshi_common import same_player
    assert norm_name("Rasmus Højgaard") == "rasmus hojgaard"
    assert same_player("Zach Bauchou", "Zachary Bauchou")
    assert same_player("Kris Ventura", "Kristoffer Ventura")
    assert not same_player("Nicolai Hojgaard", "Rasmus Hojgaard")
    assert same_player("Chan Kim", "Chris Kim")  # ambiguous on its own, so field_for only uses it when unique


def test_pga_espn_field_replaces_withdrawn_kalshi_golfers(tmp_path, monkeypatch):
    m = load("pga-kalshi", "pga_model")
    base = [f"Golfer {a}{b}son" for a in "abcdefg" for b in "hijklm"][:38]  # distinct letter-only names
    espn = base + ["Zachary Bauchou", "Alternate One"]
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "current.json").write_text(json.dumps({"events": [{
        "event": {"name": "Test Open", "status": "scheduled"}, "players": espn, "rounds": []}]}))
    monkeypatch.setattr(m, "DATA", str(tmp_path))
    golfers = [{"name": n} for n in base] + [{"name": "Zach Bauchou"}, {"name": "Withdrawn Guy"}]
    t = {"event_key": "TEST26", "title": "Test Open", "golfers": golfers, "markets": ["make_cut", "win"]}
    names, n_pad, _ = m.field_for(t, {"skill": {}, "n_eff": {}})
    assert len(names) == 40 and n_pad == 0
    assert "Zach Bauchou" in names and "Zachary Bauchou" not in names  # Kalshi spelling kept
    assert "Alternate One" in names and "Withdrawn Guy" not in names
    assert m.field_for.dropped == ["Withdrawn Guy"]
