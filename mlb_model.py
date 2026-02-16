import numpy as np


def project_team_runs(
    offense_rating,
    pitcher_factor,
    bullpen_fatigue,
    park_factor,
    wind_out,
    temperature,
):
    runs = offense_rating

    # starting pitcher impact
    runs *= pitcher_factor

    # bullpen fatigue impact
    runs *= (1 + bullpen_fatigue * 0.15)

    # ballpark factor
    runs *= park_factor

    # weather effects
    if wind_out:
        runs *= 1.08

    if temperature > 25:
        runs *= 1.05

    return runs


def simulate_mlb_game(home_proj, away_proj, sims=50000):

    rng = np.random.default_rng()

    home_runs = rng.poisson(home_proj, sims)
    away_runs = rng.poisson(away_proj, sims)

    totals = home_runs + away_runs

    return {
        "fair_total": float(np.mean(totals)),
        "home_win_prob": float(np.mean(home_runs > away_runs)),
        "over_dist": totals.tolist()[:1000],  # small sample
    }
    
def project_f5_runs(team_runs_projection):
    """
    Estimate runs scored in first 5 innings.
    MLB average: ~55% of runs scored by inning 5.
    """
    return team_runs_projection * 0.55
