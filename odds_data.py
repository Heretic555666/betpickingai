import requests
import os

ODDS_API_KEY = os.getenv("ODDS_API_KEY")

print("KEY LOADED:", ODDS_API_KEY)

SPORT_KEY = "basketball_nba"
MLB_SPORT = "baseball_mlb"
NRL_SPORT_KEY = "rugbyleague_nrl"
AFL_SPORT_KEY = "aussierules_afl"
DEFAULT_REGIONS = "us"

SPORT_REGIONS = {
    "rugbyleague_nrl": "au",
    "aussierules_afl": "au",
}

MARKETS = "spreads,h2h"
ODDS_FORMAT = "decimal"


def fetch_nba_totals():
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds"

    region = DEFAULT_REGIONS

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": region,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
    }


    res = requests.get(url, params=params, timeout=10)
    res.raise_for_status()

    try:
        data = res.json()
        
        # 🔍 DEBUG — DO NOT DELETE ANYTHING ELSE
        print("ODDS RAW RESPONSE (first 3000 chars):")
        print(str(data)[:3000])

        if "message" in data:
            print(f"API error: {data['message']}")
            return None
        return data
    except ValueError:
        print("Invalid JSON response")
        return None

def get_mlb_totals(api_key: str):
    """
    Fetch MLB totals from Odds API.
    SAFE: manual call only.
    """

    import requests

    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"

    params = {
        "apiKey": api_key,
        "regions": "region",
        "markets": "totals,spreads",
        "oddsFormat": "decimal",
    }

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()

    games = r.json()
    results = []

    for game in games:
        home = game["home_team"]
        away = game["away_team"]

        for book in game["bookmakers"]:
            for market in book["markets"]:
                if market["key"] == "totals":
                    for outcome in market["outcomes"]:
                        if outcome["name"] == "Over":
                            results.append({
                                "home": home,
                                "away": away,
                                "total": outcome["point"],
                                "book": book["title"]
                            })
                if market["key"] == "spreads":
                    for outcome in market["outcomes"]:
                        if outcome["point"] == -1.5:
                            results.append({
                                "home": home,
                                "away": away,
                                "run_line": outcome["point"],
                                "run_line_price": outcome["price"],
                                "book": book["title"]
                            })

    return results

def fetch_odds_for_sport(sport_key: str):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"

    region = SPORT_REGIONS.get(sport_key, DEFAULT_REGIONS)

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": region,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
    }

    try:
        res = requests.get(url, params=params, timeout=10)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f"Odds fetch failed for {sport_key}:", e)
        return []
