import os
from odds_data import get_mlb_totals
from app import ENABLE_MLB
from fastapi import APIRouter


router = APIRouter()

from mlb_model import (
    project_team_runs,
    simulate_mlb_game,
    project_f5_runs,
    simulate_run_line,
)


def normalize_team(name: str) -> str:
    """Normalize team names for matching."""
    return name.lower().replace(".", "").strip()

@router.get("/mlb/edges")
def mlb_edges():
    """
    Combine model projections with sportsbook totals.
    SAFE: manual test endpoint only.
    """

    if not ENABLE_MLB:
        return {"status": "MLB disabled"}

    api_key = os.getenv("ODDS_API_KEY")
    odds_games = get_mlb_totals(api_key)

    results = []

    for game in odds_games:

        home = game["home"]
        away = game["away"]
        book_total = game["total"]

        # --- MODEL PROJECTIONS ---
        home_proj = project_team_runs(home, away, is_home=True)
        away_proj = project_team_runs(away, home, is_home=False)

        model_total = round(home_proj + away_proj, 2)
        edge = round(model_total - book_total, 2)
        
        confidence = min(abs(edge) * 0.12, 1.0)
        
                # --- RUN LINE SIMULATION ---
        rl = simulate_run_line(home_proj, away_proj)

        home_cover_prob = rl["home_cover_prob"]
        away_cover_prob = rl["away_cover_prob"]

        # implied probability from -110 style pricing assumption
        implied = 0.523

        home_edge = round(home_cover_prob - implied, 3)
        away_edge = round(away_cover_prob - implied, 3)

        # --- F5 CALCULATION ---
        home_f5 = project_f5_runs(home_proj)
        away_f5 = project_f5_runs(away_proj)
        f5_total = round(home_f5 + away_f5, 2)

        # Determine strongest run-line edge
        rl_side = None
        rl_edge = 0

        if abs(home_edge) > abs(away_edge):
            rl_side = f"{home} -1.5"
            rl_edge = home_edge
        else:
            rl_side = f"{away} +1.5"
            rl_edge = away_edge

        results.append({
            "home": home,
            "away": away,
            "sportsbook_total": book_total,
            "model_total": model_total,
            "edge": edge,
            "confidence": confidence,
            "f5_total": f5_total,
            "run_line_home_edge": home_edge,
            "run_line_away_edge": away_edge,
            "home_cover_prob": round(home_cover_prob, 3),
            "away_cover_prob": round(away_cover_prob, 3),
        })


    # Elite alerts only (80%+)
    elite_results = [
        r for r in results
        if r["confidence"] >= 0.80
        or abs(r["run_line_home_edge"]) >= 0.08
        or abs(r["run_line_away_edge"]) >= 0.08
    ]

    # Build readable run-line output fields
    for r in elite_results:
        rl_home = r["run_line_home_edge"]
        rl_away = r["run_line_away_edge"]

        if abs(rl_home) > abs(rl_away):
            r["run_line_pick"] = f"{r['home']} -1.5"
            r["run_line_edge"] = rl_home
        else:
            r["run_line_pick"] = f"{r['away']} +1.5"
            r["run_line_edge"] = rl_away

    return elite_results

@router.get("/mlb/test")
def mlb_test():
    return {"status": "MLB router working"}


@router.get("/mlb/demo")
def mlb_demo():

    league_avg_runs = 4.5

    home_proj = project_team_runs(
        league_avg_runs,
        pitcher_factor=0.85,
        bullpen_fatigue=0.2,
        park_factor=1.05,
        wind_out=True,
        temperature=27,
    )

    away_proj = project_team_runs(
        league_avg_runs,
        pitcher_factor=1.05,
        bullpen_fatigue=0.6,
        park_factor=1.05,
        wind_out=True,
        temperature=27,
    )

    result = simulate_mlb_game(home_proj, away_proj)

        # ---------- FIRST 5 INNINGS MODEL ----------
    home_f5 = project_f5_runs(home_proj)
    away_f5 = project_f5_runs(away_proj)

    f5_total = home_f5 + away_f5

    sportsbook_f5 = 4.5  # example F5 line

    f5_edge = round(f5_total - sportsbook_f5, 2)

    f5_over = f5_total > sportsbook_f5

    sportsbook_total = 9.5   # example betting line

    over_prob = result["fair_total"] > sportsbook_total

    edge = round(result["fair_total"] - sportsbook_total, 2)

    return {
        "home_projected_runs": round(home_proj, 2),
        "away_projected_runs": round(away_proj, 2),
        "fair_total": round(result["fair_total"], 2),
        "sportsbook_total": sportsbook_total,
        "edge_runs": edge,
        "over_recommended": result["fair_total"] > sportsbook_total,
        "home_win_probability": round(result["home_win_prob"], 3),
                "f5_fair_total": round(f5_total, 2),
        "sportsbook_f5_total": sportsbook_f5,
        "f5_edge_runs": f5_edge,
        "f5_over_recommended": f5_over,

    }

@router.get("/mlb/odds")
def mlb_odds():
    """
    SAFE MLB odds test route.
    Disabled in production unless flag enabled.
    """

    if not ENABLE_MLB:
        return {"status": "MLB disabled"}

    api_key = os.getenv("ODDS_API_KEY")

    return get_mlb_totals(api_key)
