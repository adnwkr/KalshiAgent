"""
PGA betting model: course-adjusted, recency-weighted player skill from round scores, then a Monte
Carlo simulation of the tournament (4 rounds, 36-hole cut) -> each golfer's probability to win,
finish top 5/10/20, make the cut and lead after round 1, compared against Kalshi and (majors)
sportsbook prices.

Data: data/results/rounds_<year>.csv and events.csv (ESPN, written by the GitHub Action),
      data/lines.json (Kalshi relay), data/odds.json (The Odds API, majors only).

Usage:
  python3 pga_model.py events               list open Kalshi tournaments (event keys)
  python3 pga_model.py predict [EVENT]      simulate a tournament -> preds.json (default: the next one)
  python3 pga_model.py kalshi [EVENT]       model vs every Kalshi golfer market after fees -> kalshi_edges.json
  python3 pga_model.py books [EVENT]        majors only: model vs best sportsbook winner price -> books_edges.json
  python3 pga_model.py grade picks.json     grade logged picks -> graded.json
  python3 pga_model.py backtest             walk-forward check of the probabilities over recent events
EVENT is the Kalshi event key from lines.json, e.g. WOWTC26 or MAST27.
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common"))
from kalshi_common import fee as kalshi_fee, norm_name, price_to_american, same_player  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# Tunable settings. The grading run may adjust these and record why in the PGA Lessons page.
CONFIG = {
    "halflife_days": 240,        # recency weight for rounds
    "shrink_rounds": 10,         # pseudo-rounds pulling a player toward the prior (few rounds = more pull)
    "prior_skill": 1.0,          # strokes per round worse than an average tour field, for unknown players
    "round_sd": 2.85,            # SD of one round around a player's skill (estimated by backtest)
    "skill_sd": 0.45,            # week-to-week form uncertainty added to each player's skill
    "cut_size": 65,              # top N and ties make the cut
    "no_cut_field": 80,          # fields this size or smaller play without a cut (signature events)
    "n_sims": 20000,
    "default_field": 132,        # field size assumed when the real field isn't published yet
    "major_field": 90,           # Masters-sized field (other majors use default_field)
    "kalshi_fee_coef": 0.07,     # taker fee ~ coef * P * (1-P) per $1 contract; verify on Kalshi's fee page
    "min_edge_cents": 3.0,       # model prob minus all-in cost, in cents
    "min_roi": 0.20,             # and model prob / cost - 1 at least this (long shots need a real margin)
    "min_cost": 2, "max_cost": 90,  # golf long shots trade at 2-10c, so the band is wider than NFL/NBA's 15-85c
    "min_rounds_eff": 20,        # golfers with fewer effective rounds are priced but never flagged (model is mostly prior)
    "order_dollars": 10,         # order size used for Kalshi fee rounding
}
cfg_path = os.path.join(HERE, "config.json")
if os.path.exists(cfg_path):
    CONFIG.update(json.load(open(cfg_path)))

MARKETS = {"win": "Winner", "top5": "Top 5", "top10": "Top 10", "top20": "Top 20",
           "make_cut": "Make cut", "r1_lead": "R1 leader"}


# ----------------------------------------------------------------------------- data
def load_rounds():
    files = sorted(glob.glob(os.path.join(DATA, "results", "rounds_*.csv")))
    if not files:
        sys.exit("no data/results/rounds_*.csv: the GitHub Action has not collected golf results yet")
    r = pd.concat([pd.read_csv(f, dtype={"event_id": str, "player_id": str}) for f in files], ignore_index=True)
    r = r[r.strokes.between(55, 95) & r.player.notna()].copy()
    r["start"] = pd.to_datetime(r.start)
    r["key"] = r.player.map(norm_name)
    r["er"] = r.event_id + "_" + r["round"].astype(str)
    return r


def load_events():
    p = os.path.join(DATA, "results", "events.csv")
    e = pd.read_csv(p, dtype={"event_id": str}) if os.path.exists(p) else pd.DataFrame(columns=["event_id"])
    if len(e):
        e["start"] = pd.to_datetime(e.start, errors="coerce")
        e["end"] = pd.to_datetime(e.end, errors="coerce")
    return e


def fit_skills(rounds, asof):
    """strokes = round difficulty + player skill + noise, solved by alternating means over rounds
    before `asof`. Each player's skill is a recency-weighted mean of course-adjusted scores, shrunk
    toward the prior (shrink_rounds pseudo-rounds at prior_skill), then centered so an average
    tour round is 0. Lower = better, in strokes per round."""
    r = rounds[rounds.start < asof]
    if r.empty:
        return {"skill": {}, "n_eff": {}}, CONFIG["round_sd"]
    w = 0.5 ** ((asof - r.start).dt.days.values / CONFIG["halflife_days"])
    k, prior = CONFIG["shrink_rounds"], CONFIG["prior_skill"]
    s = r.strokes.values
    keys, er = r.key.values, r.er.values
    wsum = pd.Series(w).groupby(keys).sum()
    skill = pd.Series(0.0, index=wsum.index)
    for _ in range(15):
        sk = pd.Series(keys).map(skill).values
        diff = pd.Series(s - sk).groupby(er).transform("mean").values      # round difficulty
        num = pd.Series(w * (s - diff)).groupby(keys).sum()
        skill = (num + k * prior) / (wsum + k)
        skill -= np.average(pd.Series(keys).map(skill).values, weights=w)  # average tour round = 0
    sk = pd.Series(keys).map(skill).values
    diff = pd.Series(s - sk).groupby(er).transform("mean").values
    resid = s - diff - sk
    recent = (asof - r.start).dt.days.values < 730
    return {"skill": skill.to_dict(), "n_eff": wsum.to_dict()}, float(np.std(resid[recent] if recent.any() else resid))


def match_player(name, skills):
    """Exact normalized name, else unique last name + first initial."""
    k = norm_name(name)
    if k in skills:
        return k
    parts = k.split()
    if len(parts) >= 2:
        cand = [s for s in skills if s.split() and s.split()[-1] == parts[-1] and s[0] == parts[0][0]]
        if len(cand) == 1:
            return cand[0]
    return None


def simulate(field, skills, n_sims=None, seed=7, no_cut=None):
    """field: list of display names. Returns DataFrame of probabilities per golfer."""
    n_sims = n_sims or CONFIG["n_sims"]
    rng = np.random.default_rng(seed)
    sk = skills.get("skill", {})
    keys = [match_player(n, sk) for n in field]
    mu = np.array([sk[k] if k else CONFIG["prior_skill"] for k in keys])
    F = len(field)
    form = rng.normal(0, CONFIG["skill_sd"], (n_sims, F))
    rd = rng.normal(0, CONFIG["round_sd"], (n_sims, F, 4))
    scores = mu[None, :, None] + form[:, :, None] + rd
    r1 = scores[:, :, 0]
    thirtysix = scores[:, :, :2].sum(2)
    total = scores.sum(2)
    cut = (F <= CONFIG["no_cut_field"]) if no_cut is None else no_cut
    if not cut:
        nth = np.sort(thirtysix, axis=1)[:, min(CONFIG["cut_size"], F) - 1][:, None]
        made = thirtysix <= nth
    else:
        made = np.ones_like(thirtysix, dtype=bool)
    final = np.where(made, total, np.inf)
    # position = 1 + number strictly better; ties share a position (scores are continuous, so round
    # to whole strokes first, as real scores are)
    fr = np.where(made, np.round(final), np.inf)
    order = np.sort(fr, axis=1)
    pos = np.empty_like(fr)
    for i in range(n_sims):  # searchsorted per sim gives "1 + number strictly better"
        pos[i] = np.searchsorted(order[i], fr[i], side="left") + 1
    best = fr.min(1, keepdims=True)
    tied_top = (fr == best)
    win_share = tied_top / tied_top.sum(1, keepdims=True)          # playoff: equal chance among tied
    r1r = np.round(r1)
    r1_best = r1r.min(1, keepdims=True)
    r1_tied = (r1r == r1_best)
    r1_share = r1_tied / r1_tied.sum(1, keepdims=True)             # dead heat among co-leaders
    out = pd.DataFrame({
        "golfer": field, "matched": [k is not None for k in keys],
        "skill": np.round(mu, 2),
        "rounds_eff": [round(skills.get("n_eff", {}).get(k, 0), 1) if k else 0 for k in keys],
        "win": win_share.mean(0),
        "top5": (pos <= 5).mean(0), "top10": (pos <= 10).mean(0), "top20": (pos <= 20).mean(0),
        "make_cut": made.mean(0), "r1_lead": r1_share.mean(0),
    })
    return out.sort_values("win", ascending=False).reset_index(drop=True)


# ----------------------------------------------------------------------------- Kalshi helpers
KALSHI_LINES = os.path.join(DATA, "lines.json")


def kalshi_lines():
    if not os.path.exists(KALSHI_LINES):
        sys.exit(f"missing {KALSHI_LINES}: pull the relay repo first")
    return json.load(open(KALSHI_LINES))


def pick_event(kl, key=None):
    ts = kl.get("tournaments", [])
    if key:
        t = next((t for t in ts if t["event_key"].upper() == key.upper()), None)
        if not t:
            sys.exit(f"event {key} not in lines.json; open events: {[x['event_key'] for x in ts]}")
        return t
    # default: the soonest-closing tournament with more than just a winner market
    full = [t for t in ts if len(t["markets"]) > 1] or ts
    if not full:
        sys.exit("no open PGA tournaments in lines.json")
    return sorted(full, key=lambda t: t["close_time"] or "")[0]


def field_for(t, skills=None):
    """Field for the simulation. Once ESPN publishes the official field (current.json), that list is
    the field: Kalshi golfers who aren't in it have withdrawn and are left out, and ESPN entrants
    with no Kalshi market are added. Kalshi's spelling is kept for golfers on both lists so prices
    and probabilities join. Before ESPN posts the field, Kalshi's list is used as is once make-cut
    markets are open; only futures with a partial list are padded with the best-rated players.
    Returns (names, n_padded, in_progress); the golfers dropped as withdrawn are in field_for.dropped."""
    kalshi_names = [g["name"] for g in t["golfers"]]
    names = list(kalshi_names)
    field_for.dropped = []
    in_progress = False
    cur = os.path.join(DATA, "results", "current.json")
    espn_field = False
    if os.path.exists(cur):
        c = json.load(open(cur))
        for ev in c.get("events", []):
            tokens = set(norm_name(ev["event"]["name"]).split()) - {"the", "championship", "open", "classic", "tournament"}
            if tokens & set(norm_name(t["title"] or "").split()):
                in_progress = bool(ev.get("rounds")) or ev["event"].get("status") not in ("scheduled", "pre", "")
                players = ev.get("players") or []
                if len(players) >= 30:
                    espn_field = True
                    names = []
                    used = set()
                    for p in players:  # exact spelling first; nickname match only when it is unambiguous
                        k = [n for n in kalshi_names if n not in used and norm_name(n) == norm_name(p)]
                        if not k:
                            k = [n for n in kalshi_names if n not in used and same_player(n, p)]
                            k = k if len(k) == 1 else []
                        names.append(k[0] if k else p)
                        used.update(k[:1])
                    field_for.dropped = [n for n in kalshi_names if n not in names]
    # Kalshi lists every entrant once it opens make-cut markets, so its list is the real field.
    # Only futures-style events (winner market for a partial list) still need padding.
    kalshi_field = "make_cut" in (t.get("markets") or []) and len(t["golfers"]) >= 60
    n_pad = 0
    if not espn_field and not kalshi_field and skills:
        target = CONFIG["major_field"] if "MAST" in t["event_key"].upper() else CONFIG["default_field"]
        have = {match_player(n, skills["skill"]) for n in names}
        # rank by skill among players with real recent data
        pool = sorted((k for k, n in skills["n_eff"].items() if n >= 8 and k not in have),
                      key=lambda k: skills["skill"][k])
        for k in pool[:max(0, target - len(names))]:
            names.append(k.title())
            n_pad += 1
    return names, n_pad, in_progress


def fee(c):
    return kalshi_fee(c, CONFIG["kalshi_fee_coef"], CONFIG.get("order_dollars", 10))


# ----------------------------------------------------------------------------- commands
def events_cmd():
    for t in kalshi_lines().get("tournaments", []):
        print(f"{t['event_key']}: {t['title']} | closes {t['close_time']} | {t['n_golfers']} golfers | {t['markets']}")


def predict(key=None, out_path="preds.json"):
    kl = kalshi_lines()
    t = pick_event(kl, key)
    rounds = load_rounds()
    skills, sd = fit_skills(rounds, pd.Timestamp(datetime.now(timezone.utc).date()))
    field, n_pad, in_progress = field_for(t, skills)
    sim = simulate(field, skills)
    unmatched = sim[~sim.matched].golfer.tolist()
    res = {"event_key": t["event_key"], "title": t["title"], "close_time": t["close_time"], "config": CONFIG,
           "in_progress": in_progress, "padded_golfers": n_pad, "not_in_field": field_for.dropped,
           "field_size": len(field), "cut": len(field) > CONFIG["no_cut_field"],
           "round_sd_observed": round(sd, 2), "unmatched_golfers": unmatched,
           "golfers": json.loads(sim.round(4).to_json(orient="records"))}
    json.dump(res, open(out_path, "w"), indent=1)
    print(f"{t['title']} ({t['event_key']}): field {len(field)} ({n_pad} added as likely entrants), "
          f"{'cut' if res['cut'] else 'no cut'}, {len(unmatched)} golfers with no history (priced at the prior) -> {out_path}")
    if field_for.dropped:
        print(f"  On Kalshi but not in ESPN's field (withdrawn?), not priced: {', '.join(field_for.dropped)}")
    if in_progress:
        print("  WARNING: this event has started. The model is pre-tournament only and ignores live scores; "
              "do not use it for picks until the next event.")
    for _, g in sim.head(15).iterrows():
        print(f"  {g.golfer:<24} skill {g.skill:+.2f}  win {g.win:.1%}  top10 {g.top10:.1%}  cut {g.make_cut:.0%}")
    return t, sim


def kalshi(key=None, out_path="kalshi_edges.json"):
    t, sim = predict(key, out_path="preds.json")
    probs = {norm_name(r.golfer): r for r in sim.itertuples()}
    rows = []
    for g in t["golfers"]:
        p = probs.get(norm_name(g["name"]))
        if p is None:
            continue
        for mk in MARKETS:
            q = g.get(mk)
            if not q:
                continue
            p_yes = float(getattr(p, mk))
            for side, prob, c in (("YES", p_yes, q.get("yes_ask")), ("NO", 1 - p_yes,
                                                                      100 - q["yes_bid"] if q.get("yes_bid") else None)):
                if not c or not (0 < c < 100):
                    continue
                cost = c + fee(c)
                rows.append({"golfer": g["name"], "market": mk, "market_label": MARKETS[mk], "side": side,
                             "ticker": q["ticker"], "model_p": round(prob, 4), "cost": cost,
                             "kalshi_mid": q.get("mid"), "edge_cents": round(prob * 100 - cost, 2),
                             "roi": round(prob * 100 / cost - 1, 3), "american_odds": price_to_american(cost),
                             "golfer_has_history": bool(p.matched),
                             "rounds_eff": round(float(getattr(p, "rounds_eff", 0) or 0), 1)})
    rows.sort(key=lambda x: -x["edge_cents"])
    best = {}
    for x in rows:  # one market per golfer (top-N markets on one player are the same bet)
        if (x["edge_cents"] >= CONFIG["min_edge_cents"] and x["roi"] >= CONFIG["min_roi"]
                and CONFIG["min_cost"] <= x["cost"] <= CONFIG["max_cost"] and x["golfer_has_history"]
                and x["rounds_eff"] >= CONFIG["min_rounds_eff"]):
            best.setdefault(x["golfer"], x)
    flagged = sorted(best.values(), key=lambda x: -x["edge_cents"])
    in_prog = json.load(open("preds.json")).get("in_progress")
    if in_prog:
        flagged = []
    json.dump({"event_key": t["event_key"], "title": t["title"], "kalshi_fetched_at": kalshi_lines().get("fetched_at"),
               "in_progress": in_prog, "config": CONFIG, "flagged": flagged, "all_contracts": rows}, open(out_path, "w"), indent=1)
    print(f"{len(rows)} contract sides priced, {len(flagged)} golfers with an edge >= {CONFIG['min_edge_cents']}c "
          f"and ROI >= {CONFIG['min_roi']:.0%} -> {out_path}")
    for x in flagged[:15]:
        print(f"  +{x['edge_cents']}c {x['side']} {x['golfer']} {x['market_label']} cost {x['cost']}c "
              f"model {x['model_p']:.1%} ({x['american_odds']:+d})")


def books(key=None, out_path="books_edges.json"):
    op = os.path.join(DATA, "odds.json")
    if not os.path.exists(op):
        sys.exit("no data/odds.json: sportsbook golf odds exist only during the four majors")
    od = json.load(open(op))
    t, sim = predict(key, out_path="preds.json")
    probs = {norm_name(r.golfer): r for r in sim.itertuples()}
    rows = []
    for ev in od.get("tournaments", []):
        for g in ev["golfers"]:
            p = probs.get(norm_name(g["name"]))
            if p is None or not g.get("best"):
                continue
            pr = g["best"]["price"]
            be = 100 / (pr + 100) if pr > 0 else -pr / (-pr + 100)
            rows.append({"golfer": g["name"], "market": "win", "price": pr, "book": g["best"]["book"],
                         "model_p": round(float(p.win), 4), "breakeven": round(be, 4),
                         "market_novig": g.get("median_novig"), "pinnacle": g.get("pinnacle"),
                         "edge_cents": round((float(p.win) - be) * 100, 2), "roi": round(float(p.win) / be - 1, 3)})
    rows.sort(key=lambda x: -x["edge_cents"])
    flagged = [x for x in rows if x["edge_cents"] >= CONFIG["min_edge_cents"] and x["roi"] >= CONFIG["min_roi"]]
    json.dump({"event_key": t["event_key"], "odds_fetched_at": od.get("fetched_at"), "flagged": flagged,
               "all": rows}, open(out_path, "w"), indent=1)
    print(f"{len(rows)} golfers priced at books, {len(flagged)} flagged -> {out_path}")


def finish_pos(p):
    s = str(p)
    return int(s.lstrip("T")) if s.lstrip("T").isdigit() else None


def grade(picks_path, out_path="graded.json"):
    """picks: [{"id":..., "tournament": "Name (EVENTKEY)", "start": "YYYY-MM-DD", "golfer": "...",
                "market": "win"|"top5"|"top10"|"top20"|"make_cut"|"r1_lead", "side": "YES"|"NO", "odds": 400}]
    Ties at a top-N cutoff count as Yes here; check Kalshi's settlement of ties and adjust if it differs."""
    rounds = load_rounds()
    ev = load_events()
    out = []
    for p in json.load(open(picks_path)):
        r1_tie = False
        start = pd.Timestamp(p["start"])
        cand = ev[(ev.start - start).abs() <= pd.Timedelta(days=3)] if len(ev) else ev
        name_tokens = set(norm_name(re.sub(r"\(.*?\)", "", p["tournament"])).split())
        if len(cand) > 1:
            cand = cand.assign(ov=cand.name.map(lambda n: len(set(norm_name(n).split()) & name_tokens))) \
                       .sort_values("ov", ascending=False).head(1)
        if cand.empty or cand.iloc[0].status != "final":
            out.append({**p, "result": "pending"}); continue
        e = cand.iloc[0]
        rr = rounds[rounds.event_id == e.event_id]
        me = rr[rr.key == norm_name(p["golfer"])]
        if me.empty:  # Kalshi settles a golfer who never tees off at its "last fair price"; we log it as Void
            out.append({**p, "result": "void", "note": "golfer not in final results (did not start); "
                                                        "Kalshi settles these at the last fair price"}); continue
        pos = str(me.pos.iloc[0])
        n = finish_pos(pos)
        m = p["market"]
        if m == "make_cut":
            # Kalshi GOLFCUT rules: a withdrawal or DQ before the cut is No; a golfer who made the cut
            # and withdrew later still counts as Yes. fetch_results.py marks anyone with fewer than the
            # full set of rounds "MC", so a third-round score on the card is the sign the cut was made.
            yes = pos not in ("MC", "WD", "DQ") or me["round"].nunique() >= 3
        elif m == "r1_lead":
            r1 = rr[rr["round"] == 1]
            yes = me[me["round"] == 1].strokes.min() == r1.strokes.min()
            r1_tie = yes and int((r1.strokes == r1.strokes.min()).sum()) > 1
        else:
            N = {"win": 1, "top5": 5, "top10": 10, "top20": 20}[m]
            yes = n is not None and n <= N
        hit = yes if p["side"] == "YES" else not yes
        odds = float(p.get("odds", 100))
        units = (100 / -odds if odds < 0 else odds / 100) if hit else -1
        out.append({**p, "result": "win" if hit else "loss", "units": round(units, 3), "finish": pos,
                    "event": e["name"], "tie_at_cutoff": bool((pos.startswith("T") and m.startswith("top")
                                                               and n == {"top5": 5, "top10": 10, "top20": 20}.get(m))
                                                              or (m == "r1_lead" and r1_tie))})
    json.dump(out, open(out_path, "w"), indent=1, default=str)
    for o in out:
        print(o.get("id"), o["golfer"], o["market"], o["side"], "->", o["result"], o.get("finish"), o.get("units"))


def backtest(n_events=40):
    """Walk-forward: for each recent finished event, fit skills on earlier rounds only, simulate the
    actual field, and score the probabilities against what happened."""
    rounds = load_rounds()
    evs = rounds.groupby("event_id").agg(start=("start", "first")).sort_values("start")
    evs = evs[evs.start >= evs.start.max() - pd.Timedelta(days=550)].tail(n_events)
    rec, sds = [], []
    for eid, row in evs.iterrows():
        rr = rounds[rounds.event_id == eid]
        players = rr.drop_duplicates("key")
        field = players.player.tolist()
        if len(field) < 30:
            continue
        skills, sd = fit_skills(rounds, row.start)
        sds.append(sd)
        sim = simulate(field, skills, n_sims=4000, no_cut=not (players.pos == "MC").any())
        pos = players.set_index("player").pos.astype(str)
        sim["pos"] = sim.golfer.map(pos)
        sim["n"] = sim.pos.map(finish_pos)
        sim["y_win"] = (sim.pos == "1").astype(int)
        sim["y_top10"] = (sim.n.fillna(999) <= 10).astype(int)
        sim["y_cut"] = (~sim.pos.isin(["MC", "WD", "DQ"])).astype(int)
        sim["event"] = eid
        rec.append(sim)
    r = pd.concat(rec, ignore_index=True)

    def ll(p, y):
        p = np.clip(p, 1e-4, 1 - 1e-4)
        return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())
    lines = [f"Events tested: {r.event.nunique()}, golfer-events: {len(r)}",
             f"Observed round SD after course adjustment: {np.mean(sds):.2f} (config round_sd {CONFIG['round_sd']})"]
    for m, y in (("win", "y_win"), ("top10", "y_top10"), ("make_cut", "y_cut")):
        base = r.groupby("event")[y].transform("mean")
        lines.append(f"{m}: log loss model {ll(r[m], r[y]):.4f} vs equal-chance baseline {ll(base, r[y]):.4f}; "
                     f"Brier {((r[m] - r[y]) ** 2).mean():.4f} vs {((base - r[y]) ** 2).mean():.4f}")
    r["bucket"] = pd.cut(r.top10, [0, .05, .1, .2, .35, .5, 1])
    cal = r.groupby("bucket", observed=True).agg(pred=("top10", "mean"), actual=("y_top10", "mean"), n=("y_top10", "size"))
    lines.append("Top-10 calibration (predicted vs actual): " + "; ".join(
        f"{b}: {c.pred:.1%} vs {c.actual:.1%} (n={c.n})" for b, c in cal.iterrows()))
    fav = r.loc[r.groupby("event").win.idxmax()]
    lines.append(f"Model favorite won {fav.y_win.sum()} of {len(fav)} events; finished top 10 in {fav.y_top10.mean():.0%}")
    print("\n".join(lines))
    return lines


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "predict"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    {"events": lambda: events_cmd(), "predict": lambda: predict(arg), "kalshi": lambda: kalshi(arg),
     "books": lambda: books(arg), "grade": lambda: grade(arg), "backtest": lambda: backtest()}[cmd]()
