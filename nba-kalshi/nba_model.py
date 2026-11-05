"""
NBA betting model: recency-weighted team offense/defense ratings -> predicted margin and total,
compared against Kalshi ladders and sportsbook prices. Same interface as nfl_model.py.

Data: data/results/games_<season>.csv (ESPN results and lines, written by the GitHub Action).

Usage:
  python3 nba_model.py predict [DATE]   model vs DraftKings lines for one day's games -> preds.json
  python3 nba_model.py kalshi [DATE]    model vs every Kalshi rung after fees -> kalshi_edges.json
  python3 nba_model.py books [DATE]     model vs best sportsbook price per side + market board -> books_edges.json
  python3 nba_model.py grade picks.json grade logged picks -> graded.json
  python3 nba_model.py backtest         walk-forward backtest over the last two seasons
DATE is the US Eastern game date (YYYY-MM-DD); default today.
"""
import glob
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.stats import norm

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
ET = ZoneInfo("America/New_York")

# Tunable settings. The grading run may adjust these and record why in the NBA Lessons page.
CONFIG = {
    "halflife_days": 90,         # recency weight: a game this many days old counts half
    "season_carryover": 0.5,     # extra weight multiplier per season boundary (last season's games)
    "ridge_lambda": 2.0,         # shrinks team ratings toward average (larger = more shrinkage)
    "margin_sigma": 14.5,        # SD of final margin around prediction (for probabilities)
    "total_sigma": 18.5,
    "min_edge_spread": 3.0,      # points of fair-line edge needed to consider a side
    "min_edge_total": 4.0,
    "kalshi_fee_coef": 0.07,     # taker fee ~ coef * P * (1-P) per $1 contract; verify on Kalshi's fee page
    "min_edge_cents": 8.0,       # model prob minus all-in cost, in cents
}
cfg_path = os.path.join(HERE, "config.json")
if os.path.exists(cfg_path):
    CONFIG.update(json.load(open(cfg_path)))


# ----------------------------------------------------------------------------- data
def load_games():
    files = sorted(glob.glob(os.path.join(DATA, "results", "games_*.csv")))
    if not files:
        sys.exit("no data/results/games_*.csv: the GitHub Action has not collected NBA results yet")
    g = pd.concat([pd.read_csv(f, dtype={"game_id": str}) for f in files], ignore_index=True)
    g = g[g.season_type.isin(["regular", "playoffs", "play-in"])].copy()
    g["date"] = pd.to_datetime(g.date)
    for c in ("home_pts", "away_pts", "spread_home", "total"):
        g[c] = pd.to_numeric(g[c], errors="coerce")
    g["final"] = g.status.eq("final") & g.home_pts.notna()
    g["margin"] = g.home_pts - g.away_pts          # home minus away
    g["pts_total"] = g.home_pts + g.away_pts
    g["bet_id"] = g.date.dt.strftime("%Y-%m-%d") + " " + g.away + "@" + g.home   # Bet Log "Game ID"
    g = g.sort_values(["date", "tipoff_utc", "game_id"]).reset_index(drop=True)
    # back-to-back: the team also played the previous calendar day
    long = pd.concat([g[["game_id", "date", "home"]].rename(columns={"home": "team"}),
                      g[["game_id", "date", "away"]].rename(columns={"away": "team"})])
    long = long.sort_values(["team", "date"])
    long["prev"] = long.groupby("team").date.shift()
    long["b2b"] = ((long.date - long.prev).dt.days == 1).astype(int)
    b2b = long.set_index(["game_id", "team"]).b2b
    g["home_b2b"] = [b2b.get((i, t), 0) for i, t in zip(g.game_id, g.home)]
    g["away_b2b"] = [b2b.get((i, t), 0) for i, t in zip(g.game_id, g.away)]
    return g


TEAMS = None


def fit(hist, asof):
    """Weighted ridge on points scored. Each game gives two rows:
      home_pts = mu + off[home] + def[away] + home_adv + b2b terms
      away_pts = mu + off[away] + def[home]            + b2b terms
    off > 0 scores more than average; def > 0 allows more. Uses only games before `asof`."""
    global TEAMS
    h = hist[hist.final & (hist.date < asof)]
    if len(h) < 100:
        return None
    TEAMS = sorted(set(hist.home) | set(hist.away))
    ti = {t: i for i, t in enumerate(TEAMS)}
    n, T = len(h), len(TEAMS)
    days = (asof - h.date).dt.days.values
    cur_season = h.season.max()
    w = 0.5 ** (days / CONFIG["halflife_days"]) * CONFIG["season_carryover"] ** (cur_season - h.season.values)
    # columns: off[T], def[T], mu, home_adv, own_b2b, opp_b2b
    X = np.zeros((2 * n, 2 * T + 4))
    y = np.concatenate([h.home_pts.values, h.away_pts.values])
    hi = h.home.map(ti).values
    ai = h.away.map(ti).values
    r = np.arange(n)
    X[r, hi] = 1; X[r, T + ai] = 1
    X[n + r, ai] = 1; X[n + r, T + hi] = 1
    X[:, 2 * T] = 1
    X[r, 2 * T + 1] = 1 - h.neutral.values
    X[r, 2 * T + 2] = h.home_b2b.values; X[r, 2 * T + 3] = h.away_b2b.values
    X[n + r, 2 * T + 2] = h.away_b2b.values; X[n + r, 2 * T + 3] = h.home_b2b.values
    W = np.concatenate([w, w])
    pen = np.zeros(2 * T + 4); pen[:2 * T] = CONFIG["ridge_lambda"]
    A = X.T @ (X * W[:, None]) + np.diag(pen)
    b = np.linalg.solve(A, X.T @ (W * y))
    return {"off": dict(zip(TEAMS, b[:T])), "def": dict(zip(TEAMS, b[T:2 * T])), "mu": b[2 * T],
            "home_adv": b[2 * T + 1], "own_b2b": b[2 * T + 2], "opp_b2b": b[2 * T + 3], "n_games": n}


def score(m, df):
    df = df.copy()
    off, de = m["off"], m["def"]
    adv = m["home_adv"] * (1 - df.neutral)
    hp = m["mu"] + df.home.map(off).fillna(0) + df.away.map(de).fillna(0) + adv \
        + m["own_b2b"] * df.home_b2b + m["opp_b2b"] * df.away_b2b
    ap = m["mu"] + df.away.map(off).fillna(0) + df.home.map(de).fillna(0) \
        + m["own_b2b"] * df.away_b2b + m["opp_b2b"] * df.home_b2b
    df["pred_home_pts"], df["pred_away_pts"] = hp, ap
    df["pred_margin"] = hp - ap
    df["pred_total"] = hp + ap
    s, st = CONFIG["margin_sigma"], CONFIG["total_sigma"]
    # spread_home is the home line (negative = home favored); home covers if margin > -spread_home
    df["spread_edge"] = df.pred_margin + df.spread_home       # + means home side
    df["total_edge"] = df.pred_total - df.total               # + means over
    df["p_home_cover"] = 1 - norm.cdf(0, loc=df.spread_edge, scale=s)
    df["p_over"] = 1 - norm.cdf(0, loc=df.total_edge, scale=st)
    df["p_home_win"] = 1 - norm.cdf(0, loc=df.pred_margin, scale=s)
    return df


def day_games(g, day):
    return g[g.date == pd.Timestamp(day)]


def today_et():
    return datetime.now(ET).date().isoformat()


# ----------------------------------------------------------------------------- backtest
def backtest():
    g = load_games()
    seasons = sorted(g[g.final].season.unique())
    test_seasons = seasons[-2:]
    res = []
    for day in sorted(g[g.final & g.season.isin(test_seasons)].date.unique()):
        day = pd.Timestamp(day)
        m = fit(g, day)
        if m is None:
            continue
        res.append(score(m, day_games(g, day)[lambda d: d.final]))
    r = pd.concat(res)
    # skip each season's first three weeks, when ratings lean on last season
    start = r.groupby("season").date.transform("min")
    r = r[r.date >= start + pd.Timedelta(days=21)]
    lines = [f"Games tested: {len(r)} ({', '.join(str(s) for s in test_seasons)} seasons, after week 3)"]
    lines.append(f"Margin RMSE: model {np.sqrt(((r.margin - r.pred_margin) ** 2).mean()):.2f}"
                 f" | total RMSE: model {np.sqrt(((r.pts_total - r.pred_total) ** 2).mean()):.2f}")
    lines.append(f"Residual SD (use for sigmas): margin {np.std(r.margin - r.pred_margin):.1f}, "
                 f"total {np.std(r.pts_total - r.pred_total):.1f}")
    p = r.p_home_win.clip(0.01, 0.99)
    y = (r.margin > 0).astype(int)
    lines.append(f"Win prob log loss {-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean():.4f} "
                 f"(coin flip 0.6931), straight-up accuracy {((p > .5) == y).mean():.1%}")
    ml = r[r.spread_home.notna()]
    if len(ml):
        lines.append(f"With a closing line ({len(ml)} games): margin RMSE market "
                     f"{np.sqrt(((ml.margin + ml.spread_home) ** 2).mean()):.2f} vs model "
                     f"{np.sqrt(((ml.margin - ml.pred_margin) ** 2).mean()):.2f}")
        cov = np.sign(ml.margin + ml.spread_home)
        for thr in [0, 1, 2, 3, 4, 5]:
            pick = ml[ml.spread_edge.abs() >= thr]
            c = cov.loc[pick.index]
            won = ((np.sign(pick.spread_edge) == c) & (c != 0)).sum()
            lost = ((np.sign(pick.spread_edge) == -c) & (c != 0)).sum()
            lines.append(f"ATS edge>={thr}: {won}-{lost} ({won / max(won + lost, 1):.1%}), n={won + lost}")
        tl = ml[ml.total.notna()]
        ou = np.sign(tl.pts_total - tl.total)
        lines.append(f"Total RMSE market {np.sqrt(((tl.pts_total - tl.total) ** 2).mean()):.2f} vs model "
                     f"{np.sqrt(((tl.pts_total - tl.pred_total) ** 2).mean()):.2f}")
        for thr in [0, 2, 4, 6]:
            pick = tl[tl.total_edge.abs() >= thr]
            c = ou.loc[pick.index]
            won = ((np.sign(pick.total_edge) == c) & (c != 0)).sum()
            lost = ((np.sign(pick.total_edge) == -c) & (c != 0)).sum()
            lines.append(f"Totals edge>={thr}: {won}-{lost} ({won / max(won + lost, 1):.1%}), n={won + lost}")
    else:
        lines.append("No closing lines on file, so no ATS backtest.")
    lines.append("Break-even at -110 is 52.4%.")
    print("\n".join(lines))
    return lines


# ----------------------------------------------------------------------------- predict
def model_for(day):
    g = load_games()
    m = fit(g, pd.Timestamp(day))
    if m is None:
        sys.exit("not enough finished games to fit the model")
    return g, m, score(m, day_games(g, day))


def predict(day, out_path="preds.json"):
    g, m, sc = model_for(day)
    preds = []
    for _, r in sc.iterrows():
        preds.append({
            "game_id": r.bet_id, "espn_id": r.game_id, "date": day, "tipoff_utc": r.tipoff_utc,
            "away": r.away, "home": r.home, "status": r.status,
            "market_spread_home": None if pd.isna(r.spread_home) else float(r.spread_home),
            "market_total": None if pd.isna(r.total) else float(r.total), "line_source": r.line_source,
            "model_spread_home": round(-float(r.pred_margin), 1), "model_total": round(float(r.pred_total), 1),
            "spread_edge_pts": None if pd.isna(r.spread_edge) else round(float(r.spread_edge), 1),
            "total_edge_pts": None if pd.isna(r.total_edge) else round(float(r.total_edge), 1),
            "p_home_cover": None if pd.isna(r.p_home_cover) else round(float(r.p_home_cover), 3),
            "p_over": None if pd.isna(r.p_over) else round(float(r.p_over), 3),
            "p_home_win": round(float(r.p_home_win), 3),
            "home_b2b": int(r.home_b2b), "away_b2b": int(r.away_b2b),
            "home_net_rating": round(float(m["off"].get(r.home, 0) - m["def"].get(r.home, 0)), 1),
            "away_net_rating": round(float(m["off"].get(r.away, 0) - m["def"].get(r.away, 0)), 1),
        })
    json.dump({"date": day, "config": CONFIG, "home_adv": round(m["home_adv"], 2),
               "b2b_effect": {"own": round(m["own_b2b"], 2), "opp": round(m["opp_b2b"], 2)},
               "games": preds}, open(out_path, "w"), indent=1, default=str)
    print(f"{day}: {len(preds)} games (home court {m['home_adv']:.1f} pts, back-to-back {m['own_b2b']:+.1f}/"
          f"{m['opp_b2b']:+.1f}) -> {out_path}")
    for p in preds:
        print(f"{p['away']}@{p['home']} | mkt home {p['market_spread_home']} model {p['model_spread_home']} "
              f"| edge {p['spread_edge_pts']} | tot {p['market_total']} -> {p['model_total']}"
              f"{' | B2B ' + ('home' if p['home_b2b'] else '') + ('away' if p['away_b2b'] else '') if p['home_b2b'] or p['away_b2b'] else ''}")


# ----------------------------------------------------------------------------- kalshi
KALSHI_LINES = os.path.join(DATA, "lines.json")


def price_to_american(c):
    c = min(max(c, 0.5), 99.5)
    return round(-100 * c / (100 - c)) if c >= 50 else round(100 * (100 - c) / c)


def fee(c):
    p = c / 100
    return float(np.ceil(CONFIG["kalshi_fee_coef"] * p * (1 - p) * 100))


def trade_sides(p_yes, r):
    """Both ways to trade a rung: YES at the ask, NO at 100 - bid. Cost includes the fee."""
    out = []
    if r.get("yes_ask") and 0 < r["yes_ask"] < 100:
        c = r["yes_ask"]; out.append(("YES", p_yes, c + fee(c)))
    if r.get("yes_bid") and 0 < r["yes_bid"] < 100:
        c = 100 - r["yes_bid"]; out.append(("NO", 1 - p_yes, c + fee(c)))
    return out


def kalshi(day, lines_path=KALSHI_LINES, out_path="kalshi_edges.json"):
    if not os.path.exists(lines_path):
        sys.exit(f"missing {lines_path}: pull the relay repo first")
    kl = json.load(open(lines_path))
    g, m, sc = model_for(day)
    s_m, s_t = CONFIG["margin_sigma"], CONFIG["total_sigma"]
    rows, games = [], []
    for _, r in sc.iterrows():
        k = next((x for x in kl["games"] if x["away"] == r.away and x["home"] == r.home
                  and abs((pd.Timestamp(x["date"]) - r.date).days) <= 1), None)
        if k is None:
            continue
        pm, pt = float(r.pred_margin), float(r.pred_total)
        games.append({"game_id": r.bet_id, "away": r.away, "home": r.home,
                      "model_spread_home": round(-pm, 1), "kalshi_spread_home": k["kalshi_spread_home"],
                      "book_spread_home": None if pd.isna(r.spread_home) else float(r.spread_home),
                      "model_total": round(pt, 1), "kalshi_total": k["kalshi_total"],
                      "book_total": None if pd.isna(r.total) else float(r.total),
                      "model_win_home": round(float(r.p_home_win), 3), "kalshi_win_home": k["kalshi_win_prob_home"]})
        for team, mk in k["win_markets"].items():
            p_yes = float(r.p_home_win) if team == r.home else 1 - float(r.p_home_win)
            for side, p, cost in trade_sides(p_yes, mk):
                rows.append({"game_id": r.bet_id, "market": "moneyline", "contract": f"{team} wins", "side": side,
                             "ticker": mk["ticker"], "model_p": p, "cost": cost,
                             "bet_log": {"side": "home" if (team == r.home) == (side == "YES") else "away", "line": 0}})
        for team, ladder in k["spread_ladders"].items():
            for mk in ladder:
                x = mk["strike"]
                if x is None:
                    continue
                p_yes = 1 - norm.cdf(x, loc=pm, scale=s_m) if team == r.home else norm.cdf(-x, loc=pm, scale=s_m)
                for side, p, cost in trade_sides(p_yes, mk):
                    rows.append({"game_id": r.bet_id, "market": "spread", "contract": f"{team} wins by over {x}",
                                 "side": side, "ticker": mk["ticker"], "model_p": p, "cost": cost,
                                 "bet_log": {"side": "home" if (team == r.home) == (side == "YES") else "away",
                                             "line": -x if side == "YES" else x}})
        for mk in k["total_ladder"]:
            x = mk["strike"]
            if x is None:
                continue
            p_yes = 1 - norm.cdf(x, loc=pt, scale=s_t)
            for side, p, cost in trade_sides(p_yes, mk):
                rows.append({"game_id": r.bet_id, "market": "total", "contract": f"over {x}", "side": side,
                             "ticker": mk["ticker"], "model_p": p, "cost": cost,
                             "bet_log": {"side": "over" if side == "YES" else "under", "line": x}})
    for x in rows:
        x["model_p"] = round(float(x["model_p"]), 3)
        x["edge_cents"] = round(x["model_p"] * 100 - x["cost"], 1)
        x["american_odds"] = price_to_american(x["cost"])
    rows.sort(key=lambda x: -x["edge_cents"])
    best = {}
    for x in rows:
        if x["edge_cents"] >= CONFIG["min_edge_cents"] and 15 <= x["cost"] <= 85:
            best.setdefault((x["game_id"], x["market"]), x)
    flagged = sorted(best.values(), key=lambda x: -x["edge_cents"])
    json.dump({"date": day, "kalshi_fetched_at": kl.get("fetched_at"), "config": CONFIG, "games": games,
               "flagged": flagged, "all_rungs": rows}, open(out_path, "w"), indent=1, default=str)
    print(f"Kalshi snapshot {kl.get('fetched_at')}: {len(games)} games matched, {len(rows)} contract sides "
          f"priced, {len(flagged)} with edge >= {CONFIG['min_edge_cents']}c")
    for x in flagged[:15]:
        print(f"  +{x['edge_cents']}c {x['game_id']} {x['side']} '{x['contract']}' cost {x['cost']}c "
              f"model {x['model_p']:.0%} ({x['american_odds']:+d})")


# ----------------------------------------------------------------------------- books
ODDS_FILE = os.path.join(DATA, "odds.json")


def implied_prob(american):
    a = float(american)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def books(day, odds_path=ODDS_FILE, lines_path=KALSHI_LINES, out_path="books_edges.json"):
    if not os.path.exists(odds_path):
        sys.exit(f"missing {odds_path}: sportsbook odds not pulled yet (is ODDS_API_KEY set?)")
    od = json.load(open(odds_path))
    kl = json.load(open(lines_path)) if os.path.exists(lines_path) else {"games": []}
    g, m, sc = model_for(day)
    s_m, s_t = CONFIG["margin_sigma"], CONFIG["total_sigma"]
    board, rows = [], []
    for _, r in sc.iterrows():
        tip = pd.Timestamp(r.tipoff_utc.replace("Z", "+00:00"))
        o = next((x for x in od["games"] if x["away"] == r.away and x["home"] == r.home
                  and abs((pd.Timestamp(x["commence_time"]) - tip).total_seconds()) < 12 * 3600), None)
        if o is None:
            continue
        k = next((x for x in kl["games"] if x["away"] == r.away and x["home"] == r.home
                  and abs((pd.Timestamp(x["date"]) - r.date).days) <= 1), {})
        pm, pt = float(r.pred_margin), float(r.pred_total)
        c, pin, best = o["consensus"], o.get("pinnacle") or {}, o["best"]
        board.append({"game_id": r.bet_id, "away": r.away, "home": r.home, "n_books": o["n_books"],
                      "spread_home": {"model": round(-pm, 1), "consensus": c["spread_home"],
                                      "pinnacle": (pin.get("spread") or {}).get("home_point"),
                                      "kalshi": k.get("kalshi_spread_home")},
                      "total": {"model": round(pt, 1), "consensus": c["total"],
                                "pinnacle": (pin.get("total") or {}).get("point"), "kalshi": k.get("kalshi_total")},
                      "home_win": {"model": round(float(r.p_home_win), 3), "consensus_novig": c["novig_home_win"],
                                   "pinnacle_novig": (pin.get("ml") or {}).get("novig_home"),
                                   "kalshi": k.get("kalshi_win_prob_home")},
                      "best": best})
        sides = []
        if best.get("home_spread"):
            b = best["home_spread"]; sides.append(("spread", "home", b, 1 - norm.cdf(-b["point"], pm, s_m)))
        if best.get("away_spread"):
            b = best["away_spread"]; sides.append(("spread", "away", b, norm.cdf(b["point"], pm, s_m)))
        if best.get("over"):
            b = best["over"]; sides.append(("total", "over", b, 1 - norm.cdf(b["point"], pt, s_t)))
        if best.get("under"):
            b = best["under"]; sides.append(("total", "under", b, norm.cdf(b["point"], pt, s_t)))
        if best.get("home_ml"):
            sides.append(("moneyline", "home", best["home_ml"], float(r.p_home_win)))
        if best.get("away_ml"):
            sides.append(("moneyline", "away", best["away_ml"], 1 - float(r.p_home_win)))
        for market, side, b, p in sides:
            be = implied_prob(b["price"])
            rows.append({"game_id": r.bet_id, "market": market, "side": side, "line": b.get("point", 0),
                         "price": b["price"], "book": b["book"], "model_p": round(float(p), 3),
                         "breakeven": round(be, 3), "edge_cents": round((float(p) - be) * 100, 1)})
    rows.sort(key=lambda x: -x["edge_cents"])
    flagged = [x for x in rows if x["edge_cents"] >= CONFIG["min_edge_cents"]]
    json.dump({"date": day, "odds_fetched_at": od.get("fetched_at"), "credits_remaining": od.get("credits_remaining"),
               "board": board, "flagged": flagged, "all_sides": rows}, open(out_path, "w"), indent=1, default=str)
    print(f"Odds snapshot {od.get('fetched_at')} (credits left {od.get('credits_remaining')}): {len(board)} games, "
          f"{len(flagged)} sides with edge >= {CONFIG['min_edge_cents']}c at best price")
    for x in flagged[:12]:
        print(f"  +{x['edge_cents']}c {x['game_id']} {x['market']} {x['side']} {x['line']} {x['price']:+d} "
              f"@{x['book']} model {x['model_p']:.0%}")


# ----------------------------------------------------------------------------- grade
def grade(picks_path, out_path="graded.json"):
    """picks: [{"id":..., "game_id": "2026-11-06 PHI@NYK", "market": "spread"|"total"|"moneyline",
                "side": "home"|"away"|"over"|"under", "line": float (spread from the picked side's view;
                total number), "odds": -110}]"""
    g = load_games().set_index("bet_id")
    out = []
    for p in json.load(open(picks_path)):
        r = g.loc[p["game_id"]] if p["game_id"] in g.index else None
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        if r is None or not r.final:
            out.append({**p, "result": "pending"}); continue
        margin = r.margin if p["side"] in ("home", "over", "under") else -r.margin
        if p["market"] == "spread":
            v = margin + float(p["line"])
        elif p["market"] == "total":
            v = (r.pts_total - float(p["line"])) * (1 if p["side"] == "over" else -1)
        else:
            v = margin
        res = "win" if v > 0 else "loss" if v < 0 else "push"
        odds = float(p.get("odds", -110))
        units = (100 / -odds if odds < 0 else odds / 100) if res == "win" else (-1 if res == "loss" else 0)
        out.append({**p, "result": res, "units": round(units, 3),
                    "final": f"{r.away} {int(r.away_pts)} @ {r.home} {int(r.home_pts)}",
                    "closing_spread_home": None if pd.isna(r.spread_home) else float(r.spread_home),
                    "closing_total": None if pd.isna(r.total) else float(r.total)})
    json.dump(out, open(out_path, "w"), indent=1, default=str)
    for o in out:
        print(o.get("id"), o["game_id"], o["market"], o["side"], o.get("line"), "->", o["result"], o.get("units"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "predict"
    arg = sys.argv[2] if len(sys.argv) > 2 else today_et()
    if cmd == "backtest":
        backtest()
    elif cmd == "predict":
        predict(arg)
    elif cmd == "kalshi":
        kalshi(arg)
    elif cmd == "books":
        books(arg)
    elif cmd == "grade":
        grade(sys.argv[2])
