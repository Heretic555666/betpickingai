from fastapi import APIRouter
import requests
from datetime import datetime, timezone
import math
import re
import os

# -------------------------
# NAME NORMALIZATION
# -------------------------
def canon(name: str) -> str:
    return name.lower().replace(".", "").strip()

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
        "markets": "totals,spreads,h2h",
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
# Coordinates = arena locations (lat, lon)

TEAM_COORDS = {
    "ATL": (33.7573, -84.3963),   # Hawks – State Farm Arena
    "BOS": (42.3662, -71.0621),   # Celtics – TD Garden
    "BKN": (40.6826, -73.9754),   # Nets – Barclays Center
    "CHA": (35.2251, -80.8392),   # Hornets – Spectrum Center
    "CHI": (41.8807, -87.6742),   # Bulls – United Center
    "CLE": (41.4965, -81.6882),   # Cavaliers – Rocket Mortgage FieldHouse
    "DAL": (32.7905, -96.8104),   # Mavericks – American Airlines Center
    "DEN": (39.7487, -105.0077),  # Nuggets – Ball Arena (ALTITUDE)
    "DET": (42.3411, -83.0553),   # Pistons – Little Caesars Arena
    "GSW": (37.7680, -122.3877),  # Warriors – Chase Center
    "HOU": (29.7508, -95.3621),   # Rockets – Toyota Center
    "IND": (39.7639, -86.1555),   # Pacers – Gainbridge Fieldhouse
    "LAC": (34.0430, -118.2673),  # Clippers – Crypto.com Arena
    "LAL": (34.0430, -118.2673),  # Lakers – Crypto.com Arena
    "MEM": (35.1382, -90.0506),   # Grizzlies – FedExForum
    "MIA": (25.7814, -80.1870),   # Heat – Kaseya Center
    "MIL": (43.0451, -87.9180),   # Bucks – Fiserv Forum
    "MIN": (44.9795, -93.2760),   # Timberwolves – Target Center
    "NOP": (29.9489, -90.0819),   # Pelicans – Smoothie King Center
    "NYK": (40.7505, -73.9934),   # Knicks – Madison Square Garden
    "OKC": (35.4634, -97.5151),   # Thunder – Paycom Center
    "ORL": (28.5392, -81.3839),   # Magic – Kia Center
    "PHI": (39.9012, -75.1720),   # 76ers – Wells Fargo Center
    "PHX": (33.4457, -112.0712),  # Suns – Footprint Center
    "POR": (45.5316, -122.6668),  # Trail Blazers – Moda Center
    "SAC": (38.5802, -121.4997),  # Kings – Golden 1 Center
    "SAS": (29.4269, -98.4375),   # Spurs – Frost Bank Center
    "TOR": (43.6435, -79.3791),   # Raptors – Scotiabank Arena
    "UTA": (40.7683, -111.9011),  # Jazz – Delta Center (ALTITUDE)
    "WAS": (38.8981, -77.0209),   # Wizards – Capital One Arena
}

# -------------------------
# STAR PLAYER MAP (TIERED – FULL LEAGUE)
# -------------------------

STAR_PLAYERS = {
    "TIER_1": {
        # Half-court anchors
        "ATL": ["Kristaps Porzingis"],
        "BOS": ["Jayson Tatum"],
        "BKN": ["Cam Thomas"],
        "CHA": ["LaMelo Ball"],
        "CHI": ["Nikola Vucevic"],
        "CLE": ["Donovan Mitchell"],
        "DAL": ["Anthony Davis"],
        "DEN": ["Nikola Jokic"],
        "DET": ["Cade Cunningham"],
        "GSW": ["Stephen Curry"],
        "HOU": ["Alperen Sengun"],
        "IND": ["Tyrese Haliburton"],
        "LAC": ["Kawhi Leonard"],
        "LAL": ["LeBron James"],
        "MEM": ["Ja Morant"],
        "MIA": ["Bam Adebayo"],
        "MIL": ["Giannis Antetokounmpo"],
        "MIN": ["Anthony Edwards"],
        "NOP": ["Zion Williamson"],
        "NYK": ["Jalen Brunson"],
        "OKC": ["Shai Gilgeous-Alexander"],
        "ORL": ["Paolo Banchero"],
        "PHI": ["Joel Embiid"],
        "PHX": ["Devin Booker"],
        "POR": ["Damian Lillard"],
        "SAC": ["Domantas Sabonis"],
        "SAS": ["Victor Wembanyama"],
        "TOR": ["Scottie Barnes"],
        "UTA": ["Lauri Markkanen"],
        "WAS": ["Trae Young"],
    },
    "TIER_2": {
        # Flow / secondary engines
        "ATL": ["CJ McCollum"],
        "BOS": ["Jaylen Brown"],
        "CHA": ["Brandon Miller"],
        "CHI": ["Zach LaVine"],
        "CLE": ["Darius Garland"],
        "DEN": ["Jamal Murray"],
        "DET": ["Jaden Ivey"],
        "GSW": ["Jimmy Butler III"],
        "HOU": ["Kevin Durant"],
        "IND": ["Pascal Siakam"],
        "LAC": ["James Harden"],
        "LAL": ["Luka Doncic"],
        "MEM": ["Jaren Jackson Jr."],
        "MIA": ["Tyler Herro"],
        "MIL": ["Kyle Kuzma"],
        "NOP": ["Dejounte Murray"],
        "NYK": ["Karl-Anthony Towns"],
        "OKC": ["Jalen Williams"],
        "ORL": ["Franz Wagner"],
        "PHI": ["Tyrese Maxey"],
        "POR": ["Anfernee Simons"],
        "SAC": ["DeMar DeRozan"],
        "SAS": ["De'Aaron Fox"],
        "TOR": ["Brandon Ingram"],
        "WAS": ["Jordan Poole"],
    },
}

# -------------------------
# STAR LOOKUPS (TIER-AWARE)
# -------------------------

STAR_LOOKUP = {}
STAR_TIER_LOOKUP = {}

for tier, team_map in STAR_PLAYERS.items():
    for abbr, players in team_map.items():
        for p in players:
            STAR_LOOKUP.setdefault(abbr, []).append(p)
            STAR_TIER_LOOKUP[p.lower().replace(".", "")] = tier

all_stars = []

for tier_map in STAR_PLAYERS.values():        # TIER_1, TIER_2
    for players in tier_map.values():         # team -> [players]
        all_stars.extend(players)

assert len(all_stars) == len(set(all_stars)), "Duplicate star across teams!"


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

        tier_1_out = False
        tier_2_out = False
        secondary_out = False
        minutes_factor = 1.0


        questionable = False
        doubtful = False

        
        for p in players:
            name = canon(p.get("playerName", ""))
            status = p.get("status", "").upper()

            team_stars = [canon(p) for p in STAR_LOOKUP.get(abbr, [])]


            if status == "QUESTIONABLE":
                questionable = True

            if name in team_stars and status == "OUT":
                tier = STAR_TIER_LOOKUP.get(name)
                if tier == "TIER_1":
                    tier_1_out = True
                    minutes_factor -= 0.10
                elif tier == "TIER_2":
                    tier_2_out = True
                    minutes_factor -= 0.06
            elif status == "OUT":
                secondary_out = True
                minutes_factor -= 0.03


        out[abbr] = {
            "tier_1_out": tier_1_out,
            "tier_2_out": tier_2_out,
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

    team_uncertain = (
        injury_map.get(home, {}).get("questionable", False)
        or injury_map.get(home, {}).get("doubtful", False)
        or injury_map.get(away, {}).get("questionable", False)
        or injury_map.get(away, {}).get("doubtful", False)
    )

    return not team_uncertain


def is_back_to_back(team_abbr: str, game_time: datetime, games: list) -> bool:
    """
    Returns True if the team played a game within the last ~24–26 hours.
    """
    for g in games:
        if team_abbr not in (
            g["homeTeam"]["teamTricode"],
            g["awayTeam"]["teamTricode"],
        ):
            continue

        prev_tip = g.get("gameTimeUTC")
        if not prev_tip:
            continue

        prev_time = datetime.fromisoformat(
            prev_tip.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        diff_hours = (game_time - prev_time).total_seconds() / 3600
        if 0 < diff_hours <= 26:
            return True

    return False


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

        team_a_b2b = is_back_to_back(home, game_time, games)
        team_b_b2b = is_back_to_back(away, game_time, games)

        inputs.append(
            {
                "team_a": g["homeTeam"]["teamName"],
                "team_b": g["awayTeam"]["teamName"],
                "game_time": game_time,
                "lineups_confirmed": lineups_ok,
                "home_team": "A",
                "team_a_travel_km": 0,
                "team_b_travel_km": round(travel_km, 1),
                "team_a_b2b": team_a_b2b,
                "team_b_b2b": team_b_b2b,
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

# --- BACKWARD COMPATIBILITY ALIAS ---
lineups_confirmed_for_game = lineups_confirmed
