from odds_data import fetch_odds_for_sport
from nrl_afl_model import (
    project_total,
    calculate_edge,
    calculate_confidence,
    weather_impact_adjustment,
)

import requests

# Stadium coordinates for precise weather
STADIUM_COORDS = {
    # NRL
    "Brisbane Broncos": (-27.4648, 153.0157),
    "Sydney Roosters": (-33.8915, 151.2243),
    "Melbourne Storm": (-37.8165, 144.9830),
    "South Sydney Rabbitohs": (-33.8915, 151.2243),
    "Penrith Panthers": (-33.7513, 150.6941),
    "Parramatta Eels": (-33.8076, 151.0036),
    "Canterbury Bulldogs": (-33.8915, 151.2243),
    "Wests Tigers": (-33.8915, 151.2243),
    "Newcastle Knights": (-32.9283, 151.7817),
    "Gold Coast Titans": (-28.1040, 153.4250),
    "North Queensland Cowboys": (-19.2589, 146.8169),
    "Canberra Raiders": (-35.2496, 149.1420),

    # AFL
    "Richmond": (-37.8199, 144.9834),
    "Collingwood": (-37.8199, 144.9834),
    "Carlton": (-37.8199, 144.9834),
    "Melbourne": (-37.8199, 144.9834),
    "Essendon": (-37.7513, 144.8890),
    "Geelong": (-38.1576, 144.3540),
    "West Coast": (-31.9505, 115.8605),
    "Fremantle": (-31.9505, 115.8605),
    "Adelaide": (-34.9154, 138.5967),
    "Port Adelaide": (-34.9154, 138.5967),
    "Brisbane Lions": (-27.4648, 153.0157),
    "Gold Coast Suns": (-28.1040, 153.4250),
    "Sydney Swans": (-33.8915, 151.2243),
    "GWS Giants": (-33.8420, 151.0440),
}

# Indoor / roof stadiums where weather impact is minimal
INDOOR_STADIUMS = {
    "Melbourne": True,
    "Collingwood": True,
    "Carlton": True,
    "Essendon": True,
}


# Teams acclimatized to tropical heat & humidity
HEAT_ACCLIMATIZED_TEAMS = {
    # NRL
    "Brisbane Broncos",
    "North Queensland Cowboys",
    "Gold Coast Titans",

    # AFL
    "Brisbane Lions",
    "Gold Coast Suns",
}

def get_weather(team_name: str):
    """
    Fetch weather with timing & humidity.
    """

    coords = STADIUM_COORDS.get(team_name)

    if not coords:
        return None

    lat, lon = coords

    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "precipitation_probability,relativehumidity_2m",
            "current_weather": True,
        }

        r = requests.get(url, params=params, timeout=3)
        data = r.json()

        current = data.get("current_weather", {})
        hourly = data.get("hourly", {})

        rain_probs = hourly.get("precipitation_probability", [])
        humidity = hourly.get("relativehumidity_2m", [])

        rain_next_hours = rain_probs[:3] if rain_probs else [0]
        humidity_now = humidity[0] if humidity else 50

        return {
            "temp_c": current.get("temperature"),
            "wind_kph": current.get("windspeed"),
            "wind_gust_kph": current.get("windspeed", 0) * 1.35,
            "rain_prob_now": rain_next_hours[0],
            "rain_prob_1h": rain_next_hours[1] if len(rain_next_hours) > 1 else 0,
            "rain_prob_2h": rain_next_hours[2] if len(rain_next_hours) > 2 else 0,
            "humidity": humidity_now,
        }

    except Exception:
        return None




def get_edges(sport_key, sport_name):
    games = fetch_odds_for_sport(sport_key)
    edges = []

    for game in games or []:
        book_total = None
        spread_line = None
        home_ml = None
        away_ml = None

        for book in game.get("bookmakers", []):
            for market in book.get("markets", []):

                if market["key"] == "totals":
                    book_total = market["outcomes"][0]["point"]

                elif market["key"] == "spreads":
                    spread_line = market["outcomes"][0]["point"]

                elif market["key"] == "h2h":
                    home_ml = market["outcomes"][0]["price"]
                    away_ml = market["outcomes"][1]["price"]

        if book_total is None:
            continue


        model_total = project_total(book_total, sport_name)

        # Weather adjustment (safe fallback if unavailable)
        weather = get_weather(game["home_team"])
        indoor = INDOOR_STADIUMS.get(game["home_team"], False)
        home_team = game["home_team"]
        away_team = game["away_team"]

        weather_adj = weather_impact_adjustment(
            weather,
            sport_name,
            indoor,
            home_team,
            away_team,
        )



        model_total += weather_adj

        edge = calculate_edge(model_total, book_total)
        confidence = calculate_confidence(edge, sport_name)

        # -------- MONEYLINE EDGE --------
        ml_pick = None
        ml_edge = None

        if home_ml and away_ml:
            implied_home = 1 / home_ml
            implied_away = 1 / away_ml

            # simple model win probability from totals edge
            home_win_prob = 0.5 + (model_total - book_total) * 0.01
            home_win_prob = max(min(home_win_prob, 0.75), 0.25)
            away_win_prob = 1 - home_win_prob

            home_ml_edge = home_win_prob - implied_home
            away_ml_edge = away_win_prob - implied_away

            if abs(home_ml_edge) > abs(away_ml_edge):
                ml_pick = game["home_team"]
                ml_edge = round(home_ml_edge, 3)
            else:
                ml_pick = game["away_team"]
                ml_edge = round(away_ml_edge, 3)

        # -------- SPREAD EDGE --------
        spread_edge = None

        if spread_line is not None:
            projected_margin = (model_total - book_total) * 0.6
            spread_edge = round(projected_margin - spread_line, 2)

                       
            home_ml_edge = home_win_prob - implied_home
            away_ml_edge = away_win_prob - implied_away

            if abs(home_ml_edge) > abs(away_ml_edge):
                ml_pick = game["home_team"]
                ml_edge = round(home_ml_edge, 3)
            else:
                ml_pick = game["away_team"]
                ml_edge = round(away_ml_edge, 3)

        edges.append({
            "sport": sport_name,
            "home": game["home_team"],
            "away": game["away_team"],
            "book_total": book_total,
            "model_total": model_total,
            "edge": edge,
            "confidence": confidence,
            "ml_pick": ml_pick,
            "ml_edge": ml_edge,
            "spread_edge": spread_edge,
        })


    filtered = [
        e for e in edges
        if e["confidence"] >= 0.75
        or (e["ml_edge"] and abs(e["ml_edge"]) >= 0.05)
        or (e["spread_edge"] and abs(e["spread_edge"]) >= 4)
    ]

    return filtered

