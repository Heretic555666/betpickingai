# =====================================
# PRO EDGE MODEL — NRL & AFL
# =====================================

NRL_AVG_TOTAL = 44.0
AFL_AVG_TOTAL = 166.0

NRL_HOME_EDGE = 2.2
AFL_HOME_EDGE = 6.5


def project_total(book_total, sport):
    """
    Anchored projection using league averages
    and home advantage weighting.
    """

    if sport == "NRL":
        baseline = NRL_AVG_TOTAL
        home_edge = NRL_HOME_EDGE
    else:
        baseline = AFL_AVG_TOTAL
        home_edge = AFL_HOME_EDGE

    # blend market line with league baseline
    projected = (book_total * 0.6) + (baseline * 0.4)

    # apply home advantage effect
    projected += home_edge * 0.25

    return round(projected, 2)


def calculate_edge(model_total, book_total):
    return round(model_total - book_total, 2)


def calculate_confidence(edge, sport):
    """
    Confidence scaled to sport scoring range.
    """
    scale = 0.18 if sport == "NRL" else 0.06
    confidence = min(abs(edge) * scale, 1.0)

    return confidence

# =====================================
# WEATHER IMPACT (NRL & AFL)
# =====================================

def weather_impact_adjustment(
    weather: dict | None,
    sport: str,
    indoor=False,
    home_team: str | None = None,
    away_team: str | None = None,
) -> float:

    """
    Weather scoring adjustments.
    """

    if not weather or indoor:
        return 0.0

    wind = weather.get("wind_kph", 0)
    gust = weather.get("wind_gust_kph", wind)
    rain_now = weather.get("rain_prob_now", 0)
    rain_1h = weather.get("rain_prob_1h", 0)
    rain_2h = weather.get("rain_prob_2h", 0)
    humidity = weather.get("humidity", 50)
    temp = weather.get("temp_c", 20)

    adjust = 0.0

    # 🌧 Rain timing weighting (kickoff impact)
    rain_factor = max(rain_now, rain_1h * 0.7, rain_2h * 0.4)

    if rain_factor >= 70:
        adjust -= 5 if sport == "NRL" else 15
    elif rain_factor >= 40:
        adjust -= 3 if sport == "NRL" else 9
    elif rain_factor >= 20:
        adjust -= 1.5 if sport == "NRL" else 5

    # 💨 Wind
    if wind >= 30:
        adjust -= 6 if sport == "NRL" else 20
    elif wind >= 20:
        adjust -= 3 if sport == "NRL" else 12

    # 🌪 Gust impact
    if gust >= 45:
        adjust -= 8 if sport == "NRL" else 25
    elif gust >= 35:
        adjust -= 4 if sport == "NRL" else 14

    # 🥵 Humidity fatigue
    if humidity >= 85:
        adjust -= 3 if sport == "NRL" else 8
    elif humidity >= 70:
        adjust -= 1.5 if sport == "NRL" else 4

    # 🔥🥵 Heat + Humidity combo fatigue multiplier
    if temp >= 30 and humidity >= 70:
        combo_penalty = 2 if sport == "NRL" else 6

        # extreme tropical conditions
        if temp >= 32 and humidity >= 80:
            combo_penalty *= 1.5

        adjust -= combo_penalty

    # 🔥 Heat fatigue
    if temp >= 32:
        adjust -= 2 if sport == "NRL" else 6

    # 🌡 Heat acclimatization advantage
    if temp >= 30 and humidity >= 65:
        from nrl_afl_edges import HEAT_ACCLIMATIZED_TEAMS

        home_acclimated = home_team in HEAT_ACCLIMATIZED_TEAMS
        away_acclimated = away_team in HEAT_ACCLIMATIZED_TEAMS

        if home_acclimated and not away_acclimated:
            adjust += 1 if sport == "NRL" else 3

        elif away_acclimated and not home_acclimated:
            adjust += 1 if sport == "NRL" else 3

    return adjust


    
