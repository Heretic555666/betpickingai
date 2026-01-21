import requests
import os

ODDS_API_KEY = os.getenv("ODDS_API_KEY")

SPORT_KEY = "basketball_nba"
REGIONS = "us"
MARKETS = "totals,totals_q1,totals_q2,totals_q3,totals_q4"
ODDS_FORMAT = "decimal"

def fetch_nba_totals():
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds"

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "totals",
        "oddsFormat": "decimal",
    }


    res = requests.get(url, params=params, timeout=10)
    res.raise_for_status()

    try:
        return res.json()
    except ValueError:
        return None

