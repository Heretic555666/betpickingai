from fastapi import APIRouter
import requests
from datetime import datetime, timezone
import math
import re
import os

# -------------------------
# TEAM NAME → ABBREVIATION
# -------------------------

TEAM_ABBR_MAP = {
    "atlanta hawks": "ATL",
    "boston celtics": "BOS",
    "brooklyn nets": "BKN",
    "charlotte hornets": "CHA",
    "chicago bulls": "CHI",
    "cleveland cavaliers": "CLE",
    "dallas mavericks": "DAL",
    "denver nuggets": "DEN",
    "detroit pistons": "DET",
    "golden state warriors": "GSW",
    "houston rockets": "HOU",
    "indiana pacers": "IND",
    "los angeles clippers": "LAC",
    "la clippers": "LAC",
    "los angeles lakers": "LAL",
    "la lakers": "LAL",
    "memphis grizzlies": "MEM",
    "miami heat": "MIA",
    "milwaukee bucks": "MIL",
    "minnesota timberwolves": "MIN",
    "new orleans pelicans": "NOP",
    "new york knicks": "NYK",
    "oklahoma city thunder": "OKC",
    "orlando magic": "ORL",
    "philadelphia 76ers": "PHI",
    "phoenix suns": "PHX",
    "portland trail blazers": "POR",
    "sacramento kings": "SAC",
    "san antonio spurs": "SAS",
    "toronto raptors": "TOR",
    "utah jazz": "UTA",
    "washington wizards": "WAS",
}

# Aliases → canonical names
TEAM_ALIASES = {
    "lakers": "los angeles lakers",
    "warriors": "golden state warriors",
    "clippers": "los angeles clippers",
    "knicks": "new york knicks",
    "pelicans": "new orleans pelicans",
    "spurs": "san antonio spurs",
    "suns": "phoenix suns",
    "bucks": "milwaukee bucks",
    "celtics": "boston celtics",
    "nets": "brooklyn nets",
    "heat": "miami heat",
    "bulls": "chicago bulls",
    "nuggets": "denver nuggets",
    "mavericks": "dallas mavericks",
    "timberwolves": "minnesota timberwolves",
    "wolves": "minnesota timberwolves",
    "thunder": "oklahoma city thunder",
    "raptors": "toronto raptors",
    "jazz": "utah jazz",
    "kings": "sacramento kings",
    "grizzlies": "memphis grizzlies",
    "rockets": "houston rockets",
    "pacers": "indiana pacers",
    "pistons": "detroit pistons",
    "magic": "orlando magic",
    "sixers": "philadelphia 76ers",
    "76ers": "philadelphia 76ers",
    "wizards": "washington wizards",
}


def normalize_team_name(name: str) -> str | None:
    if not name:
        return None

    # normalize text
    clean = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()

    # direct match
    if clean in TEAM_ABBR_MAP:
        return clean

    # alias match
    if clean in TEAM_ALIASES:
        return TEAM_ALIASES[clean]

    # partial fuzzy match (safe)
    for key in TEAM_ABBR_MAP:
        if clean in key:
            return key

    return None


def team_name_to_abbr(name: str) -> str | None:
    canonical = normalize_team_name(name)
    if not canonical:
        return None
    return TEAM_ABBR_MAP.get(canonical)


def fetch_nba_totals_odds():
    """
    Fetches game + quarter totals from The Odds API.
    Returns dict keyed by (home_abbr, away_abbr).
    """
    ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    ODDS_API_KEY = os.getenv("ODDS_API_KEY")
    
    if not ODDS_API_KEY:
        print("❌ ODDS_API_KEY missing")
        return {}
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "totals",
        "oddsFormat": "decimal",
    }

    try:
        res = requests.get(ODDS_API_BASE, params=params, timeout=10)
        if res.status_code != 200:
            print("❌ Odds API bad status:", res.status_code)
            try:
                print("Response body:", res.json())
            except ValueError:
                print("Response text:", res.text)
            return {}
        if not res.text or not res.text.strip():
            print("⚠️ Odds API returned empty response (no odds live yet)")
            return {}
        games = res.json()
    except Exception as e:
        print("❌ Odds API error:", e)
        return {}

    odds_map = {}
    for game in games:
        home_abbr = team_name_to_abbr(game["home_team"])
        away_abbr = team_name_to_abbr(game["away_team"])
        if not home_abbr or not away_abbr:
            continue

        markets = {}
        for bookmaker in game.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                outcomes = market.get("outcomes", [])
                if len(outcomes) < 2:
                    print(
                        f"⏳ Totals market found but no prices yet for "
                        f"{game['home_team']} vs {game['away_team']}"
                    )
                    continue
                markets[market["key"]] = {
                    "line": outcomes[0].get("point"),
                    "over_odds": outcomes[0].get("price"),
                    "under_odds": outcomes[1].get("price"),
                }

        odds_map[(home_abbr, away_abbr)] = markets
    return odds_map



router = APIRouter(prefix="/auto/nba", tags=["NBA Auto"])

NBA_TIMEZONE = timezone.utc


def get_nba_game_time(team_a: str, team_b: str, date_str: str):
    url = f"https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_{date_str}.json"
    res = requests.get(url, timeout=10)

    try:
        data = res.json()
    except ValueError:
        return None

    games = data.get("scoreboard", {}).get("games", [])

    for game in games:
        home = game["homeTeam"]["teamName"]
        away = game["awayTeam"]["teamName"]

        if team_a in (home, away) and team_b in (home, away):
            tip = game.get("gameTimeUTC")
            if tip:
                return datetime.fromisoformat(
                    tip.replace("Z", "+00:00")
                ).astimezone(NBA_TIMEZONE)

    return None


# -------------------------
# NBA ENDPOINTS
# -------------------------
SCOREBOARD_URL = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
INJURY_URL = "https://cdn.nba.com/static/json/liveData/injuries/injuryReport_00.json"


# -------------------------
# TEAM COORDINATES (TRAVEL)
# -------------------------
TEAM_COORDS = {
    "LAL": (34.0430, -118.2673),
    "GSW": (37.7680, -122.3877),
    "PHX": (33.4457, -112.0712),
    "DEN": (39.7487, -105.0077),
    "BOS": (42.3662, -71.0621),
    "MIL": (43.0451, -87.9180),
    "DAL": (32.7905, -96.8104),
}


# -------------------------
# STAR PLAYER MAP
# -------------------------
STAR_PLAYERS = {
    "LAL": ["LeBron James", "Luka Doncic"],
    "DEN": ["Nikola Jokic", "Jamal Murray"],
    "BOS": ["Jayson Tatum", "Jaylen Brown"],
    "MIL": ["Giannis Antetokounmpo"],
    "PHX": ["Devin Booker"],
    "GSW": ["Stephen Curry"],
    "HOU": ["Kevin Durant"]
}


# -------------------------
# HELPERS
# -------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


def get_injury_context():
    try:
        res = requests.get(INJURY_URL, timeout=10)
        if not res.text or not res.text.strip():
            return {}

        data = res.json()
    except (Exception, ValueError):
        return {}

    teams = data.get("injuryReport", {}).get("teams", [])
    out = {}

    for team in teams:
        abbr = team.get("teamTricode")
        players = team.get("players", [])

        star_out = False
        secondary_out = False
        minutes_factor = 1.0

        questionable = False
        doubtful = False

        for p in players:
            name = p.get("playerName", "")
            status = p.get("status", "").upper()

            if status == "QUESTIONABLE":
                questionable = True
            elif status == "DOUBTFUL":
                doubtful = True
            elif status == "OUT":
                if name in STAR_PLAYERS.get(abbr, []):
                    star_out = True
                    minutes_factor -= 0.08
                else:
                    secondary_out = True
                    minutes_factor -= 0.03


        out[abbr] = {
            "star_out": star_out,
            "secondary_out": secondary_out,
            "questionable": questionable,
            "doubtful": doubtful,
            "minutes_factor": round(max(minutes_factor, 0.85), 2),
}

    return out


def lineups_confirmed_for_game(home_abbr: str, away_abbr: str) -> bool:
    """
    Lineups are considered confirmed ONLY when:
    - No players are QUESTIONABLE
    - No players are DOUBTFUL
    OUT players are allowed (stars can be out)
    """

    injury_map = get_injury_context()

    for team in (home_abbr, away_abbr):
        team_data = injury_map.get(team)
        if not team_data:
            return False

        # Only block if still uncertain
        if team_data.get("questionable") or team_data.get("doubtful"):
            return False

    return True



def lineups_confirmed(game_time_utc: datetime, injury_map: dict, home: str, away: str) -> bool:
    """
    Lineups are considered confirmed if:
    - Within 30 minutes of tip-off
    - No star players are OUT or DOUBTFUL
    """
    if not game_time_utc:
        return False

    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)

    if (game_time_utc - now_utc).total_seconds() > 1800:
        return False  # too early

    home_star_out = injury_map.get(home, {}).get("star_out", False)
    away_star_out = injury_map.get(away, {}).get("star_out", False)

    return not (home_star_out or away_star_out)


# -------------------------
# BUILD MODEL INPUTS (CRITICAL)
# -------------------------
def build_model_inputs():
    res = requests.get(SCOREBOARD_URL, timeout=10)

    if res.status_code != 200 or not res.text.strip():
        print("⚠️ NBA scoreboard API returned empty response")
        return []

    try:
        data = res.json()
    except ValueError:
        print("⚠️ NBA scoreboard returned invalid JSON")
        return []

    games = data.get("scoreboard", {}).get("games", [])

    injury_map = get_injury_context()
    inputs = []

    for g in games:
        home = g["homeTeam"]["teamTricode"]
        away = g["awayTeam"]["teamTricode"]

        tip = g.get("gameTimeUTC")
        if not tip:
            continue

        game_time = datetime.fromisoformat(
            tip.replace("Z", "+00:00")
        ).astimezone(NBA_TIMEZONE)

        travel_km = 0
        if home in TEAM_COORDS and away in TEAM_COORDS:
            travel_km = haversine_km(
                TEAM_COORDS[away][0],
                TEAM_COORDS[away][1],
                TEAM_COORDS[home][0],
                TEAM_COORDS[home][1],
            )

        lineups_ok = lineups_confirmed(
            game_time_utc=game_time,
            injury_map=injury_map,
            home=home,
            away=away,
        )

        inputs.append(
            {
                "team_a": g["homeTeam"]["teamName"],
                "team_b": g["awayTeam"]["teamName"],
                "game_time": game_time,
                "lineups_confirmed": lineups_ok,
                "home_team": "A",
                "team_a_travel_km": 0,
                "team_b_travel_km": round(travel_km, 1),
                "team_a_star_out": injury_map.get(home, {}).get("star_out", False),
                "team_b_star_out": injury_map.get(away, {}).get("star_out", False),
                "team_a_secondary_out": injury_map.get(home, {}).get(
                    "secondary_out", False
                ),
                "team_b_secondary_out": injury_map.get(away, {}).get(
                    "secondary_out", False
                ),
                "team_a_minutes_factor": injury_map.get(home, {}).get(
                    "minutes_factor", 1.0
                ),
                "team_b_minutes_factor": injury_map.get(away, {}).get(
                    "minutes_factor", 1.0
                ),
                "meta": {
                    "game_id": g["gameId"],
                    "start_time_utc": g["gameTimeUTC"],
                },
            }
        )

    return inputs


# -------------------------
# DEBUG ENDPOINT
# -------------------------
@router.get("/today")
def nba_today_debug():
    games = build_model_inputs()
    return {
        "date": datetime.utcnow().date().isoformat(),
        "games_found": len(games),
        "games": games,
    }
