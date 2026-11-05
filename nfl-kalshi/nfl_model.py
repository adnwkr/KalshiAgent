"""
NFL betting model: team efficiency ratings -> predicted margin and total,
compared against the market spread and total.

Data: nflverse (schedules + play-by-play) downloaded from GitHub releases.

Usage:
  python3 nfl_model.py download            # fetch/refresh data
  python3 nfl_model.py backtest            # walk-forward backtest by season
  python3 nfl_model.py predict SEASON WEEK # predictions for one week -> preds.json
  python3 nfl_model.py kalshi SEASON WEEK  # price Kalshi ladders with the model -> kalshi_edges.json
  python3 nfl_model.py books SEASON WEEK   # model vs best sportsbook prices (The Odds API) -> books_edges.json
  python3 nfl_model.py grade picks.json    # grade logged picks -> graded.json
"""
import json
import os
import subprocess
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.linear_model import Ridge

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
FIRST_SEASON = 2016
REL = "https://github.com/nflverse/nflverse-data/releases/download"

# Tunable settings. The grading run may adjust these and record why in the lessons page.
CONFIG = {
    "halflife_games": 6,         # EWMA half-life for team form
    "season_carryover": 0.6,     # how much of last season's rating survives to week 1
    "margin_sigma": 13.5,        # SD of margin around prediction (for probabilities)
    "total_sigma": 13.0,
    "ridge_alpha": 5.0,
    "min_edge_spread": 4.0,      # points of fair-line edge needed to consider a side
    "min_edge_total": 4.0,
    "kalshi_fee_coef": 0.07,      # taker fee ~ coef * P * (1-P) per $1 contract; verify on Kalshi's fee page
    "min_edge_cents": 10.0,      # model prob minus all-in cost, in cents (~4 pts of spread near 50c)
}
cfg_path = os.path.join(HERE, "config.json")
if os.path.exists(cfg_path):
    CONFIG.update(json.load(open(cfg_path)))


# ----------------------------------------------------------------------------- data
def download(current_season):
    os.makedirs(DATA, exist_ok=True)
    jobs = [("schedules/games.csv", "games.csv")]
    for y in range(FIRST_SEASON, current_season + 1):
        f = f"pbp_{y}.parquet"
        if y >= current_season - 0 or not os.path.exists(os.path.join(DATA, f)):
            jobs.append((f"pbp/play_by_play_{y}.parquet", f))
    procs = [subprocess.Popen(["curl", "-sfL", "-o", os.path.join(DATA, out), f"{REL}/{src}"])
             for src, out in jobs]
    for p in procs:
        p.wait()
    print(f"downloaded {len(jobs)} files")


def load_games():
    g = pd.read_csv(os.path.join(DATA, "games.csv"))
    g = g[(g.season >= FIRST_SEASON)].copy()
    g["gameday"] = pd.to_datetime(g.gameday)
    return g


def team_game_stats(seasons):
    cols = ["game_id", "season", "week", "posteam", "defteam", "epa", "success",
            "pass", "rush", "play_type"]
    frames = []
    for y in seasons:
        f = os.path.join(DATA, f"pbp_{y}.parquet")
        if not os.path.exists(f):
            continue
        p = pd.read_parquet(f, columns=cols)
        p = p[p.play_type.isin(["pass", "run"]) & p.epa.notna() & p.posteam.notna()]
        frames.append(p)
    p = pd.concat(frames)
    p["pass_epa"] = np.where(p["pass"] == 1, p.epa, np.nan)
    p["rush_epa"] = np.where(p["rush"] == 1, p.epa, np.nan)
    off = p.groupby(["game_id", "posteam"]).agg(
        off_epa=("epa", "mean"), off_sr=("success", "mean"),
        off_pass=("pass_epa", "mean"), off_rush=("rush_epa", "mean"),
        plays=("epa", "size")).reset_index().rename(columns={"posteam": "team"})
    de = p.groupby(["game_id", "defteam"]).agg(
        def_epa=("epa", "mean"), def_sr=("success", "mean"),
        def_pass=("pass_epa", "mean"), def_rush=("rush_epa", "mean")
    ).reset_index().rename(columns={"defteam": "team"})
    return off.merge(de, on=["game_id", "team"])


def long_games(g, stats):
    """One row per team per played game, ordered in time."""
    played = g[g.result.notna()]
    home = played.assign(team=played.home_team, opp=played.away_team,
                         pts_for=played.home_score, pts_against=played.away_score)
    away = played.assign(team=played.away_team, opp=played.home_team,
                         pts_for=played.away_score, pts_against=played.home_score)
    lg = pd.concat([home, away])[["game_id", "season", "week", "gameday", "team", "opp",
                                   "pts_for", "pts_against"]]
    lg["margin"] = lg.pts_for - lg.pts_against
    lg = lg.merge(stats, on=["game_id", "team"], how="left")
    return lg.sort_values(["team", "gameday"]).reset_index(drop=True)


RATE_COLS = ["off_epa", "def_epa", "off_sr", "def_sr", "off_pass", "def_pass",
             "off_rush", "def_rush", "margin", "pts_for", "pts_against", "plays"]


def pregame_ratings(lg, g):
    """For every scheduled game, each team's EWMA ratings using only earlier games."""
    hl = CONFIG["halflife_games"]
    carry = CONFIG["season_carryover"]
    league_mean = lg[RATE_COLS].mean()
    import bisect
    a = 1 - 0.5 ** (1 / hl)
    out = {}
    for team, df in lg.groupby("team"):
        state = league_mean.copy()
        last_season = None
        days, seasons_, states = [], [], []   # state AFTER each game
        for _, r in df.iterrows():
            if last_season is not None and r.season != last_season:
                state = carry * state + (1 - carry) * league_mean
            vals = r[RATE_COLS].astype(float).fillna(league_mean)
            state = (1 - a) * state + a * vals
            last_season = r.season
            days.append(r.gameday); seasons_.append(r.season); states.append(state.copy())
        out[team] = (days, seasons_, states)

    def lookup(team, day, season):
        if team not in out:
            return league_mean
        days, seasons_, states = out[team]
        k = bisect.bisect_left(days, day) - 1   # last game strictly before this day
        if k < 0:
            return league_mean
        st = states[k]
        if seasons_[k] != season:
            st = carry * st + (1 - carry) * league_mean
        return st

    rows = []
    for _, r in g.iterrows():
        h = lookup(r.home_team, r.gameday, r.season)
        a = lookup(r.away_team, r.gameday, r.season)
        row = {"game_id": r.game_id}
        for c in RATE_COLS:
            row[f"h_{c}"] = h[c]
            row[f"a_{c}"] = a[c]
        rows.append(row)
    return pd.DataFrame(rows)


def build_dataset():
    g = load_games()
    seasons = sorted(g.season.unique())
    stats = team_game_stats(seasons)
    lg = long_games(g, stats)
    rt = pregame_ratings(lg, g)
    d = g.merge(rt, on="game_id")
    # margin features (home minus away); off_epa good, def_epa allowed bad
    d["x_epa"] = (d.h_off_epa - d.h_def_epa) - (d.a_off_epa - d.a_def_epa)
    d["x_sr"] = (d.h_off_sr - d.h_def_sr) - (d.a_off_sr - d.a_def_sr)
    d["x_pass"] = (d.h_off_pass - d.h_def_pass) - (d.a_off_pass - d.a_def_pass)
    d["x_margin"] = d.h_margin - d.a_margin
    d["x_rest"] = (d.home_rest - d.away_rest).clip(-7, 7).fillna(0)
    d["x_neutral"] = (d.location == "Neutral").astype(int)
    d["x_div"] = d.div_game.fillna(0)
    # total features
    d["t_off"] = d.h_off_epa + d.a_off_epa
    d["t_def"] = d.h_def_epa + d.a_def_epa
    d["t_pts"] = d.h_pts_for + d.a_pts_for + d.h_pts_against + d.a_pts_against
    d["t_plays"] = d.h_plays + d.a_plays
    d["t_dome"] = d.roof.isin(["dome", "closed"]).astype(int)
    d["t_wind"] = d.wind.fillna(0).clip(0, 30)
    d["t_cold"] = (d.temp.fillna(60) < 35).astype(int)
    return d


# Independent fair-line model: the market line is NOT an input, so the edge is a real second opinion.
SPREAD_X = ["x_epa", "x_sr", "x_pass", "x_margin", "x_rest", "x_neutral", "x_div"]
TOTAL_X = ["t_off", "t_def", "t_pts", "t_plays", "t_dome", "t_wind", "t_cold"]


def fit(train):
    tr = train[train.result.notna() & train.spread_line.notna() & (train.week > 2)]
    ms = Ridge(alpha=CONFIG["ridge_alpha"]).fit(tr[SPREAD_X], tr.result)
    tt = train[train.total.notna() & train.total_line.notna() & (train.week > 2)]
    mt = Ridge(alpha=CONFIG["ridge_alpha"]).fit(tt[TOTAL_X], tt.total)
    return ms, mt


def score(ms, mt, df):
    df = df.copy()
    df["pred_margin"] = ms.predict(df[SPREAD_X])
    df["pred_total"] = mt.predict(df[TOTAL_X])
    df["spread_edge"] = df.pred_margin - df.spread_line   # + means home side
    df["total_edge"] = df.pred_total - df.total_line     # + means over
    s, st = CONFIG["margin_sigma"], CONFIG["total_sigma"]
    df["p_home_cover"] = 1 - norm.cdf(0, loc=df.spread_edge, scale=s)
    df["p_over"] = 1 - norm.cdf(0, loc=df.total_edge, scale=st)
    df["p_home_win"] = 1 - norm.cdf(0, loc=df.pred_margin, scale=s)
    return df


# ----------------------------------------------------------------------------- backtest
def backtest():
    d = build_dataset()
    res = []
    for season in range(2019, int(d[d.result.notna()].season.max()) + 1):
        train = d[d.season < season]
        test = d[(d.season == season) & d.result.notna() & d.spread_line.notna() & (d.week > 2)]
        if test.empty:
            continue
        ms, mt = fit(train)
        res.append(score(ms, mt, test))
    r = pd.concat(res)
    r["home_cov"] = np.sign(r.result - r.spread_line)
    r["over_hit"] = np.sign(r.total - r.total_line)
    lines = []
    for thr in [0, 2, 3, 4, 5, 6]:
        pick = r[r.spread_edge.abs() >= thr]
        won = ((np.sign(pick.spread_edge) == pick.home_cov) & (pick.home_cov != 0)).sum()
        lost = ((np.sign(pick.spread_edge) == -pick.home_cov) & (pick.home_cov != 0)).sum()
        lines.append(f"ATS edge>={thr}: {won}-{lost} ({won/max(won+lost,1):.1%}), n={won+lost}")
    for thr in [0, 2, 3, 4, 5]:
        pick = r[r.total_edge.abs() >= thr]
        won = ((np.sign(pick.total_edge) == pick.over_hit) & (pick.over_hit != 0)).sum()
        lost = ((np.sign(pick.total_edge) == -pick.over_hit) & (pick.over_hit != 0)).sum()
        lines.append(f"Totals edge>={thr}: {won}-{lost} ({won/max(won+lost,1):.1%}), n={won+lost}")
    mkt = np.sqrt(((r.result - r.spread_line) ** 2).mean())
    mod = np.sqrt(((r.result - r.pred_margin) ** 2).mean())
    lines.append(f"Margin RMSE: market {mkt:.2f} vs model {mod:.2f}")
    lines.append("Break-even at -110 is 52.4%.")
    by = r.assign(pick_win=(np.sign(r.spread_edge) == r.home_cov)).query("spread_edge.abs()>=4 and home_cov!=0")
    lines.append("By season (ATS edge>=4): " + ", ".join(
        f"{s}: {v:.1%}" for s, v in by.groupby("season").pick_win.mean().items()))
    print("\n".join(lines))
    return lines


# ----------------------------------------------------------------------------- predict
def predict(season, week, out_path="preds.json"):
    d = build_dataset()
    ms, mt = fit(d[d.result.notna()])
    wk = d[(d.season == season) & (d.week == week)]
    sc = score(ms, mt, wk)
    preds = []
    for _, r in sc.iterrows():
        home_fav = r.spread_line > 0
        preds.append({
            "game_id": r.game_id, "gameday": str(r.gameday.date()), "gametime": r.gametime,
            "away": r.away_team, "home": r.home_team,
            "market_spread_home": None if pd.isna(r.spread_line) else -float(r.spread_line),
            "market_total": None if pd.isna(r.total_line) else float(r.total_line),
            "model_margin_home": round(float(r.pred_margin), 1),
            "model_total": round(float(r.pred_total), 1),
            "spread_edge_pts": None if pd.isna(r.spread_edge) else round(float(r.spread_edge), 1),
            "total_edge_pts": None if pd.isna(r.total_edge) else round(float(r.total_edge), 1),
            "p_home_cover": None if pd.isna(r.p_home_cover) else round(float(r.p_home_cover), 3),
            "p_over": None if pd.isna(r.p_over) else round(float(r.p_over), 3),
            "p_home_win": round(float(r.p_home_win), 3),
            "home_qb": r.home_qb_name, "away_qb": r.away_qb_name,
            "roof": r.roof, "temp": None if pd.isna(r.temp) else r.temp,
            "wind": None if pd.isna(r.wind) else r.wind,
            "home_epa_net": round(float(r.h_off_epa - r.h_def_epa), 3),
            "away_epa_net": round(float(r.a_off_epa - r.a_def_epa), 3),
        })
    json.dump({"season": season, "week": week, "config": CONFIG, "games": preds},
              open(out_path, "w"), indent=1, default=str)
    print(f"wrote {len(preds)} games to {out_path}")
    for p in preds:
        print(p["away"], "@", p["home"], "| mkt home", p["market_spread_home"],
              "model home", -p["model_margin_home"], "| edge", p["spread_edge_pts"],
              "| tot", p["market_total"], "->", p["model_total"])


# ----------------------------------------------------------------------------- kalshi
KALSHI_LINES = os.path.join(HERE, "data", "lines.json")


def price_to_american(c):
    """Kalshi cost in cents (incl. fee) -> American odds for the Bet Log and grader."""
    c = min(max(c, 0.5), 99.5)
    return round(-100 * c / (100 - c)) if c >= 50 else round(100 * (100 - c) / c)


def kalshi(season, week, lines_path=KALSHI_LINES, out_path="kalshi_edges.json"):
    """Compare model probabilities with Kalshi prices for every rung of every ladder.
    Buying YES at the ask or NO at (100 - yes_bid); all-in cost includes the taker fee."""
    if not os.path.exists(lines_path):
        sys.exit(f"missing {lines_path}: pull the relay repo first")
    kl = json.load(open(lines_path))
    d = build_dataset()
    ms, mt = fit(d[d.result.notna()])
    wk = score(ms, mt, d[(d.season == season) & (d.week == week)])
    s_m, s_t, coef = CONFIG["margin_sigma"], CONFIG["total_sigma"], CONFIG["kalshi_fee_coef"]

    def fee(c):  # cents per contract, rounded up to the next cent
        p = c / 100
        return float(np.ceil(coef * p * (1 - p) * 100))

    def sides(p_yes, r):
        """Both ways to trade a rung: YES at ask, NO at 100 - bid."""
        out = []
        if r.get("yes_ask"):
            c = r["yes_ask"]; cost = c + fee(c)
            out.append(("YES", p_yes, cost))
        if r.get("yes_bid") is not None:
            c = 100 - r["yes_bid"]; cost = c + fee(c)
            out.append(("NO", 1 - p_yes, cost))
        return out

    rows, games = [], []
    for _, r in wk.iterrows():
        k = next((g for g in kl["games"] if g["away"] == r.away_team and g["home"] == r.home_team
                  and abs((pd.Timestamp(g["date"]) - r.gameday).days) <= 3), None)
        if k is None:
            continue
        pm, pt = float(r.pred_margin), float(r.pred_total)
        games.append({"game_id": r.game_id, "away": r.away_team, "home": r.home_team,
                      "model_spread_home": round(-pm, 1), "kalshi_spread_home": k["kalshi_spread_home"],
                      "book_spread_home": None if pd.isna(r.spread_line) else -float(r.spread_line),
                      "model_total": round(pt, 1), "kalshi_total": k["kalshi_total"],
                      "book_total": None if pd.isna(r.total_line) else float(r.total_line),
                      "model_win_home": round(float(r.p_home_win), 3),
                      "kalshi_win_home": k["kalshi_win_prob_home"]})
        # moneyline
        for team, m in k["win_markets"].items():
            p_yes = float(r.p_home_win) if team == r.home_team else 1 - float(r.p_home_win)
            for side, p, cost in sides(p_yes, m):
                rows.append({"game_id": r.game_id, "market": "moneyline", "contract": f"{team} wins",
                             "side": side, "ticker": m["ticker"], "model_p": p, "cost": cost})
        # spread ladders: "TEAM wins by over X"
        for team, ladder in k["spread_ladders"].items():
            for m in ladder:
                x = m["strike"]
                if x is None:
                    continue
                if team == r.home_team:
                    p_yes = 1 - norm.cdf(x, loc=pm, scale=s_m)
                else:
                    p_yes = norm.cdf(-x, loc=pm, scale=s_m)
                for side, p, cost in sides(p_yes, m):
                    rows.append({"game_id": r.game_id, "market": "spread",
                                 "contract": f"{team} wins by over {x}", "side": side,
                                 "ticker": m["ticker"], "model_p": p, "cost": cost,
                                 "bet_log": {"side": "home" if (team == r.home_team) == (side == "YES") else "away",
                                             "line": -x if side == "YES" else x}})
        # total ladder: "over X"
        for m in k["total_ladder"]:
            x = m["strike"]
            if x is None:
                continue
            p_yes = 1 - norm.cdf(x, loc=pt, scale=s_t)
            for side, p, cost in sides(p_yes, m):
                rows.append({"game_id": r.game_id, "market": "total", "contract": f"over {x}",
                             "side": side, "ticker": m["ticker"], "model_p": p, "cost": cost,
                             "bet_log": {"side": "over" if side == "YES" else "under", "line": x}})
    for x in rows:
        x["model_p"] = round(float(x["model_p"]), 3)
        x["edge_cents"] = round(x["model_p"] * 100 - x["cost"], 1)
        x["american_odds"] = price_to_american(x["cost"])
    rows.sort(key=lambda x: -x["edge_cents"])
    # rungs on one ladder are near-duplicates: keep the best rung per game and market
    best = {}
    for x in rows:
        if x["edge_cents"] >= CONFIG["min_edge_cents"] and 15 <= x["cost"] <= 85:
            best.setdefault((x["game_id"], x["market"]), x)
    flagged = sorted(best.values(), key=lambda x: -x["edge_cents"])
    json.dump({"season": season, "week": week, "kalshi_fetched_at": kl.get("fetched_at"),
               "config": CONFIG, "games": games, "flagged": flagged, "all_rungs": rows},
              open(out_path, "w"), indent=1, default=str)
    print(f"Kalshi snapshot {kl.get('fetched_at')}: {len(games)} games matched, "
          f"{len(rows)} contract sides priced, {len(flagged)} with edge >= {CONFIG['min_edge_cents']}c")
    for g in games:
        print(f"{g['away']}@{g['home']}: spread model {g['model_spread_home']} | kalshi {g['kalshi_spread_home']}"
              f" | book {g['book_spread_home']} || total model {g['model_total']} | kalshi {g['kalshi_total']}")
    for x in flagged[:15]:
        print(f"  +{x['edge_cents']}c {x['game_id']} {x['side']} '{x['contract']}' cost {x['cost']}c "
              f"model {x['model_p']:.0%} ({x['american_odds']:+d})")


# ----------------------------------------------------------------------------- books
ODDS_FILE = os.path.join(HERE, "data", "odds.json")


def implied_prob(american):
    a = float(american)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def books(season, week, odds_path=ODDS_FILE, lines_path=KALSHI_LINES, out_path="books_edges.json"):
    """Market board per game (model, consensus no-vig, Pinnacle, Kalshi) and the model's edge
    against the BEST available sportsbook price for every side."""
    if not os.path.exists(odds_path):
        sys.exit(f"missing {odds_path}: sportsbook odds not pulled yet (is ODDS_API_KEY set?)")
    od = json.load(open(odds_path))
    kl = json.load(open(lines_path)) if os.path.exists(lines_path) else {"games": []}
    d = build_dataset()
    ms, mt = fit(d[d.result.notna()])
    wk = score(ms, mt, d[(d.season == season) & (d.week == week)])
    s_m, s_t = CONFIG["margin_sigma"], CONFIG["total_sigma"]
    board, rows = [], []
    for _, r in wk.iterrows():
        o = next((g for g in od["games"] if g["away"] == r.away_team and g["home"] == r.home_team
                  and abs((pd.Timestamp(g["commence_time"]).tz_localize(None) - r.gameday).days) <= 3), None)
        if o is None:
            continue
        k = next((g for g in kl["games"] if g["away"] == r.away_team and g["home"] == r.home_team
                  and abs((pd.Timestamp(g["date"]) - r.gameday).days) <= 3), {})
        pm, pt = float(r.pred_margin), float(r.pred_total)
        c, pin, best = o["consensus"], o.get("pinnacle") or {}, o["best"]
        board.append({
            "game_id": r.game_id, "away": r.away_team, "home": r.home_team, "n_books": o["n_books"],
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
            rows.append({"game_id": r.game_id, "market": market, "side": side, "line": b.get("point", 0),
                         "price": b["price"], "book": b["book"], "model_p": round(float(p), 3),
                         "breakeven": round(be, 3), "edge_cents": round((float(p) - be) * 100, 1)})
    rows.sort(key=lambda x: -x["edge_cents"])
    flagged = [x for x in rows if x["edge_cents"] >= CONFIG["min_edge_cents"]]
    json.dump({"season": season, "week": week, "odds_fetched_at": od.get("fetched_at"),
               "credits_remaining": od.get("credits_remaining"), "board": board,
               "flagged": flagged, "all_sides": rows}, open(out_path, "w"), indent=1, default=str)
    print(f"Odds snapshot {od.get('fetched_at')} (credits left {od.get('credits_remaining')}): "
          f"{len(board)} games, {len(flagged)} sides with edge >= {CONFIG['min_edge_cents']}c at best price")
    for g in board:
        sp, t = g["spread_home"], g["total"]
        print(f"{g['away']}@{g['home']}: spread model {sp['model']} | books {sp['consensus']} | pin {sp['pinnacle']}"
              f" | kalshi {sp['kalshi']} || total model {t['model']} | books {t['consensus']} | kalshi {t['kalshi']}")
    for x in flagged[:12]:
        print(f"  +{x['edge_cents']}c {x['game_id']} {x['market']} {x['side']} {x['line']} {x['price']:+d} "
              f"@{x['book']} model {x['model_p']:.0%}")


# ----------------------------------------------------------------------------- grade
def grade(picks_path, out_path="graded.json"):
    """picks: [{"id":..., "game_id":..., "market":"spread"|"total"|"moneyline",
                "side":"home"|"away"|"over"|"under", "line":float (spread from the picked
                side's view, e.g. -3.5; total number), "odds":-110}]"""
    g = load_games().set_index("game_id")
    picks = json.load(open(picks_path))
    out = []
    for p in picks:
        r = g.loc[p["game_id"]] if p["game_id"] in g.index else None
        if r is None or pd.isna(r.result):
            out.append({**p, "result": "pending"}); continue
        margin = r.result if p["side"] in ("home", "over", "under") else -r.result
        if p["market"] == "spread":
            v = margin + float(p["line"])
        elif p["market"] == "total":
            v = (r.total - float(p["line"])) * (1 if p["side"] == "over" else -1)
        else:
            v = margin
        res = "win" if v > 0 else "loss" if v < 0 else "push"
        odds = float(p.get("odds", -110))
        units = (100 / -odds if odds < 0 else odds / 100) if res == "win" else (-1 if res == "loss" else 0)
        out.append({**p, "result": res, "units": round(units, 3),
                    "final": f"{r.away_team} {int(r.away_score)} @ {r.home_team} {int(r.home_score)}",
                    "closing_spread_home": None if pd.isna(r.spread_line) else -float(r.spread_line),
                    "closing_total": None if pd.isna(r.total_line) else float(r.total_line)})
    json.dump(out, open(out_path, "w"), indent=1, default=str)
    for o in out:
        print(o.get("id"), o["game_id"], o["market"], o["side"], o.get("line"), "->", o["result"], o.get("units"))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "download":
        download(int(sys.argv[2]) if len(sys.argv) > 2 else 2026)
    elif cmd == "backtest":
        backtest()
    elif cmd == "predict":
        predict(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "kalshi":
        kalshi(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "books":
        books(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "grade":
        grade(sys.argv[2])
