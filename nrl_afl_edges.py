from odds_data import fetch_odds_for_sport
from nrl_afl_model import project_total, calculate_edge, calculate_confidence


def get_edges(sport_key, sport_name):
    games = fetch_odds_for_sport(sport_key)
    edges = []

    for game in games or []:
        book_total = None

        for book in game.get("bookmakers", []):
            for market in book.get("markets", []):
                if market["key"] == "totals":
                    book_total = market["outcomes"][0]["point"]
                    break

        if book_total is None:
            continue

        model_total = project_total(book_total, sport_name)
        edge = calculate_edge(model_total, book_total)
        confidence = calculate_confidence(edge, sport_name)

        edges.append({
            "sport": sport_name,
            "home": game["home_team"],
            "away": game["away_team"],
            "book_total": book_total,
            "model_total": model_total,
            "edge": edge,
            "confidence": confidence
        })

    return edges
