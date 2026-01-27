from fastapi import FastAPI
from pydantic import BaseModel
import numpy as np
from datetime import datetime, timedelta, timezone
import asyncio
import requests
import os
import anyio

from dotenv import load_dotenv

from nba_data import (
    fetch_nba_totals_odds,
    build_model_inputs,
    team_name_to_abbr,
    router as nba_router,
    lineups_confirmed,
    get_injury_context,
    get_nba_game_time,
)

load_dotenv()

# =========================================================
# ENV / APP SETUP
# =========================================================

load_dotenv()

app = FastAPI(title="BetPicking AI MVP")
app.include_router(nba_router)

SIMULATIONS = 50_000
DEFAULT_BANKROLL = 1000

AEST = timezone(timedelta(hours=10))

# -------------------------
# STAR TIER PACE MODIFIERS
# -------------------------

PACE_MODIFIERS = {
    "TIER_1_OUT": {
        "pace": +1.5,      # faster game
        "variance": +0.8,  # more chaos
    },
    "TIER_2_OUT": {
        "pace": -1.0,      # slower game
        "variance": +0.2,  # uglier offense
    },
}

# -------------------------
# DEFENSIVE TOTALS IMPACT
# -------------------------

DEF_TOTALS_IMPACT = {
    "DEF_TIER_1": -1.25,
    "DEF_TIER_2": -0.75,
}

# =========================================================
# TELEGRAM
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram_alert(
    message: str,
    pace_adjust: float | None = None,
    variance_adjust: float | None = None,
):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠ Telegram not configured")
        return

    # ----------------------------------
    # FAST / SLOW TAG LOGIC (HERE)
    # ----------------------------------
    if pace_adjust is not None:
        if pace_adjust >= 1.0:
            message += "\n🔥 FAST game environment"
        elif pace_adjust <= -1.0:
            message += "\n🐢 SLOW game environment"

    # Optional numeric debug (keep or remove later)
    if pace_adjust is not None or variance_adjust is not None:
        message += (
            f"\n\n⚙ Pace adj: {pace_adjust:+.2f}"
            f" | Var adj: {variance_adjust:+.2f}"
        )

    try:
        res = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": int(TELEGRAM_CHAT_ID),
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=5,
        )
        print("✅ Telegram sent:", res.status_code)
    except Exception as e:
        print("❌ Telegram failed:", e)




# =========================================================
# ALERT STATE
# =========================================================

SENT_ALERTS: set[str] = set()
PREGAME_ALERTS: dict = {}

ALERT_DAY = datetime.now(timezone.utc).date()


def reset_alerts_if_new_day():
    global ALERT_DAY, SENT_ALERTS
    today = datetime.now(timezone.utc).date()
    if today != ALERT_DAY:
        SENT_ALERTS.clear()
        ALERT_DAY = today
        print("🔄 Alert cache reset")


# =========================================================
# MARKET CONFIG
# =========================================================

MARKET_CONFIG = {
    "game": {"mean_factor": 1.0, "sd": 12, "early_alert": True},
    "spread": {"sd": 12},
    "h2h": {},
    "q1":   {"mean_factor": 0.25, "sd": 6,  "early_alert": False},
    "q2":   {"mean_factor": 0.25, "sd": 6,  "early_alert": False},
    "q3":   {"mean_factor": 0.25, "sd": 6,  "early_alert": False},
    "q4":   {"mean_factor": 0.25, "sd": 6,  "early_alert": False},
}

# =========================================================
# INPUT MODEL
# =========================================================

class SimulationRequest(BaseModel):
    team_a: str
    team_b: str
    game_time: datetime

    base_team_a_points: float = 115
    base_team_b_points: float = 112
    home_team: str = "A"

    # travel & fatigue (safe defaults)
    team_a_travel_km: float = 0
    team_b_travel_km: float = 0
    team_a_b2b: bool = False
    team_b_b2b: bool = False


# =========================================================
# MATH HELPERS
# =========================================================

def implied_prob(odds: float) -> float:
    return 1 / odds


def calibrate_prob(p: float, strength: float = 0.65) -> float:
    return 0.5 + (p - 0.5) * strength


def cap_edge(e: float, cap: float = 0.12) -> float:
    return max(min(e, cap), -cap)


def percentile_position(arr, line):
    return round(float(np.mean(arr < line) * 100), 1)


def lean_signal(edge, pct):
    if edge >= 0.005:
        return "BET"
    if pct <= 45:
        return "WATCH OVER"
    if pct >= 55:
        return "WATCH UNDER"
    return "PASS"


def confidence_score(edge, fair, line, pct):
    return int(min(abs(edge) * 400 + abs(fair - line) * 3 + abs(50 - pct), 100))


def confidence_tier(score):
    if score >= 70:
        return "ELITE"
    if score >= 63:
        return "VERY STRONG"
    if score >= 58:
        return "STRONG"
    if score >= 54:
        return "LEAN"
    return "NOISE"

# =========================================================
# SPREAD / H2H TIERS
# =========================================================

def win_prob_tier(win_pct: float) -> str:
    """
    Tier purely from win probability.
    Conservative by design.
    """
    if win_pct >= 66:
        return "ELITE"
    if win_pct >= 61:
        return "VERY STRONG"
    if win_pct >= 57:
        return "STRONG"
    if win_pct >= 54:
        return "LEAN"
    return "NO BET"

# =========================================================
# BET FILTERS (BANKROLL PROTECTION)
# =========================================================

def allow_h2h_bet(win_pct: float, fair_odds: float, tier: str) -> bool:
    # block weak confidence
    if tier in ("NO BET", "LEAN"):
        return False

    # block heavy favorites (vig trap)
    if fair_odds < 1.45:
        return False

    # block thin edges
    if win_pct < 52:
        return False

    return True


def allow_spread_bet(win_pct: float, tier: str) -> bool:
    if tier in ("NO BET", "LEAN"):
        return False

    if win_pct < 55:
        return False

    return True


# =========================================================
# CORE SIMULATION LOGIC (single source of truth)
# =========================================================

def run_simulation(req: SimulationRequest):
    rng = np.random.default_rng()
    reset_alerts_if_new_day()

   

    home_abbr = team_name_to_abbr(req.team_a)
    away_abbr = team_name_to_abbr(req.team_b)
    if not home_abbr or not away_abbr:
        print(f"❌ Invalid team names: {req.team_a} vs {req.team_b}")
        return None

    game_id = f"{home_abbr}_vs_{away_abbr}"

    # =========================================================
    # FINAL RUN WINDOW (10 MIN / 2 MIN ONLY)
    # =========================================================

    now = datetime.now(timezone.utc)

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")

    game_time = get_nba_game_time(
        req.team_a,
        req.team_b,
        date_str,
    )


    if not game_time:
        print(f"SKIP {game_id} | game time not found")
        return {"game": game_id, "markets": {}}

    minutes_to_tip = (game_time - now).total_seconds() / 60

    if 9 <= minutes_to_tip <= 11:
        window = "10m"
    elif 1 <= minutes_to_tip <= 3:
        window = "2m"
    else:
        window = None

    # Only allow final decision windows
    if not (
        9 <= minutes_to_tip <= 11 or
        1 <= minutes_to_tip <= 3
    ):
        print(
            f"SKIP {game_id} | not in final window "
            f"({minutes_to_tip:.1f} min to tip)"
        )
        return {"game": game_id, "markets": {}}
    
    odds_map = fetch_nba_totals_odds()
    print(f"ODDS MAP GAMES: {list(odds_map.keys())}")

    matchup_odds = (
        odds_map.get((home_abbr, away_abbr))
        or odds_map.get((away_abbr, home_abbr))
    )

    if not matchup_odds:
        print(f"SKIP {game_id} | no markets posted yet")
        return {"game": game_id, "markets": {}}


    has_core_market = any(
        k in matchup_odds
        for k in (
            "totals",
            "spreads",
            "totals_q1",
            "totals_q2",
            "totals_q3",
            "totals_q4",
            "totals_h1",

        )    
    )

    if not has_core_market:
        print(f"❌ No usable markets for {game_id}")
        return None


    adj_a = req.base_team_a_points
    adj_b = req.base_team_b_points

    # -------------------------
    # HOME COURT
    # -------------------------
    if req.home_team == "A":
        adj_a += 2.5
    else:
        adj_b += 2.5

    # NOTE: team_b is always the away team in this model
    # NOTE: Fatigue applied BEFORE market scaling
    # -------------------------
    # BACK-TO-BACK FATIGUE 
    # -------------------------
    if getattr(req, "team_a_b2b", False):
        # home team less impacted, away team more impacted
        adj_a -= 1.0 if req.home_team == "A" else 2.0

    if getattr(req, "team_b_b2b", False):
        adj_b -= 1.0 if req.home_team == "B" else 2.0

    # -------------------------
    # TRAVEL × B2B STACK (AWAY ONLY)
    # -------------------------
    if (
        getattr(req, "team_b_b2b", False)
        and getattr(req, "team_b_travel_km", 0) > 800
    ):
        adj_b -= 0.5

    # -------------------------
    # PURE TRAVEL FATIGUE (AWAY)
    # -------------------------
    if getattr(req, "team_b_travel_km", 0) > 1500:
        adj_b -= 0.75
    elif getattr(req, "team_b_travel_km", 0) > 800:
        adj_b -= 0.4

    # -------------------------
    # ALTITUDE EDGE (HOME ONLY)
    # -------------------------
    HIGH_ALTITUDE = {"DEN", "UTA"}

    if home_abbr in HIGH_ALTITUDE:
        adj_a += 0.6
        adj_b -= 0.4

    results = {}

    # -------------------------
    # INJURY CONTEXT (TIER-AWARE)
    # -------------------------

    injury_map = get_injury_context()

    home_ctx = injury_map.get(home_abbr, {})
    away_ctx = injury_map.get(away_abbr, {})

    pace_adjust = 0.0
    variance_adjust = 0.0

    for ctx in (home_ctx, away_ctx):
        if ctx.get("tier_1_out"):
            pace_adjust += PACE_MODIFIERS["TIER_1_OUT"]["pace"]
            variance_adjust += PACE_MODIFIERS["TIER_1_OUT"]["variance"]

        if ctx.get("tier_2_out"):
            pace_adjust += PACE_MODIFIERS["TIER_2_OUT"]["pace"]
            variance_adjust += PACE_MODIFIERS["TIER_2_OUT"]["variance"]

    if pace_adjust != 0 or variance_adjust != 0:
        print(
            f"[PACE DEBUG] {home_abbr} vs {away_abbr} | "
            f"pace_adjust={pace_adjust:.2f}, variance_adjust={variance_adjust:.2f} | "
            f"home_ctx={home_ctx} | away_ctx={away_ctx}"
        )
    
    # -------------------------
    # DEFENSIVE TOTALS ADJUST
    # -------------------------

    def_totals_adjust = 0.0

    for ctx in (home_ctx, away_ctx):
        if ctx.get("def_tier_1_out"):
            def_totals_adjust += abs(DEF_TOTALS_IMPACT["DEF_TIER_1"])

        if ctx.get("def_tier_2_out"):
            def_totals_adjust += abs(DEF_TOTALS_IMPACT["DEF_TIER_2"])

    for market, cfg in MARKET_CONFIG.items():

        # =========================
        # FULL GAME SPREAD (NO ODDS)
        # =========================
        if market == "spread":
            spread_mean = adj_a - adj_b
            spread_sd = cfg["sd"] * (1 + variance_adjust)

            margins = rng.normal(spread_mean, spread_sd, SIMULATIONS)

            fair_spread = round(-float(np.mean(margins)), 1)

            home_win_prob = float(np.mean(margins > 0))
            away_win_prob = 1 - home_win_prob

            home_pct = round(home_win_prob * 100, 1)
            away_pct = round(away_win_prob * 100, 1)

            results["spread"] = {
                "fair_spread": fair_spread,
                "home_win_pct": home_pct,
                "away_win_pct": away_pct,
            }

            # --------
            # TIERS
            # --------
            win_pct = max(home_pct, away_pct)

            if abs(fair_spread) < 2.0:
                continue

            tier = win_prob_tier(win_pct)

            if tier not in ("ELITE", "VERY STRONG", "STRONG"):
                continue


            # --------
            # FILTERS
            # --------
            if not allow_spread_bet(win_pct, tier):
                continue

            # --------
            # TELEGRAM
            # --------
            pick_side = "HOME" if home_pct > away_pct else "AWAY"
            pick_emoji = "🏠" if pick_side == "HOME" else "✈️"

            key = f"{game_id}_spread_{fair_spread}_{tier}"

            if key not in SENT_ALERTS:
                SENT_ALERTS.add(key)

                message = (
                    f"📏 FULL GAME SPREAD — {tier}\n"
                    f"{pick_emoji} PICK: {pick_side}\n\n"
                    f"{req.team_a} vs {req.team_b}\n"
                    f"📐 Fair Spread: {fair_spread}\n"
                    f"🏠 Home win %: {home_pct}%\n"
                    f"✈️ Away win %: {away_pct}%\n"
                    f"🏥 Injuries Included: YES\n"
                )

                send_telegram_alert(
                    message,
                    pace_adjust=pace_adjust,
                    variance_adjust=variance_adjust,
                )

            continue

        # =========================
        # H2H / MONEYLINE (NO ODDS)
        # =========================
        if market == "h2h":
            home_pct = results["spread"]["home_win_pct"]
            away_pct = results["spread"]["away_win_pct"]

            home_prob = home_pct / 100
            away_prob = away_pct / 100

            if home_prob <= 0 or away_prob <= 0:
                continue

            fair_home = round(1 / home_prob, 2)
            fair_away = round(1 / away_prob, 2)

            results["h2h"] = {
                "home_win_pct": home_pct,
                "away_win_pct": away_pct,
                "fair_home_odds": fair_home,
                "fair_away_odds": fair_away,
            }

            pick_team = req.team_a if home_pct > away_pct else req.team_b
            pick_pct = max(home_pct, away_pct)
            pick_odds = fair_home if home_pct > away_pct else fair_away

            tier = win_prob_tier(pick_pct)

            if not allow_h2h_bet(pick_pct, pick_odds, tier):
                continue

            key = f"{game_id}_h2h_{pick_pct}_{tier}"

            if key not in SENT_ALERTS:
                SENT_ALERTS.add(key)

                message = (
                    f"💰 MONEYLINE — {tier}\n"
                    f"🏆 PICK: {pick_team}\n\n"
                    f"{req.team_a} vs {req.team_b}\n"
                    f"📊 Win Prob: {pick_pct}%\n"
                    f"📈 Fair Odds: {pick_odds}\n"
                    f"🏥 Injuries Included: YES\n"
                )

                send_telegram_alert(
                    message,
                    pace_adjust=pace_adjust,
                    variance_adjust=variance_adjust,
                )

            continue

        

        if market == "game":
            odds = matchup_odds.get("totals")
        elif market == "q1":
            odds = matchup_odds.get("totals_q1")
        elif market == "q2":
            odds = matchup_odds.get("totals_q2")
        elif market == "q3":
            odds = matchup_odds.get("totals_q3")
        elif market == "q4":
            odds = matchup_odds.get("totals_q4")
        else:
            odds = None

        if not odds:
            print(f"SKIP {game_id} | {market} not available")
            continue

        market_line = odds["line"]
        market_odds = odds["over_odds"]

        mean = (adj_a + adj_b) * cfg["mean_factor"]
        adj_mean = (
            mean
            + pace_adjust * cfg["mean_factor"]
            + def_totals_adjust * cfg["mean_factor"]
        )

        adj_sd = cfg["sd"] * (1 + variance_adjust)

        totals = rng.normal(adj_mean, adj_sd, SIMULATIONS)


        fair = float(np.mean(totals))
        raw_over = float(np.mean(totals > market_line))
        cal_over = calibrate_prob(raw_over)

        over_prob = cal_over
        under_prob = 1 - cal_over

        bet_side = "OVER" if over_prob > under_prob else "UNDER"
        side_emoji = "⬆️" if bet_side == "OVER" else "⬇️"

        edge = cap_edge(cal_over - implied_prob(market_odds))
        pct = percentile_position(totals, market_line)


        tier = confidence_tier(confidence_score(edge, fair, market_line, pct))
        signal = lean_signal(edge, pct)
        
        # -------------------------
        # DEFENSIVE CONFIDENCE BUMP (TOTALS / OVER ONLY)
        # -------------------------

        if bet_side == "OVER" and tier in ("LEAN", "STRONG", "VERY STRONG"):
            if (
                home_ctx.get("def_tier_1_out")
                or away_ctx.get("def_tier_1_out")
                or home_ctx.get("def_tier_2_out")
                or away_ctx.get("def_tier_2_out")
            ):
                tier = {
                    "LEAN": "STRONG",
                    "STRONG": "VERY STRONG",
                    "VERY STRONG": "ELITE",
                }.get(tier, tier)

        results[market] = {
            "line": market_line,
            "fair": round(fair, 2),
            "edge": round(edge, 4),
            "pct": pct,
            "tier": tier,
            "signal": signal,
            "pace_adjust": round(pace_adjust, 2),
            "variance_adjust": round(variance_adjust, 2),
        }

        # -------------------------
        # MARKET LABEL + EMOJI
        # -------------------------

        market_label = {
            "game": "🏀 FULL GAME TOTAL",
            "q1": "⏱️ Q1 TOTAL",
            "q2": "⏱️ Q2 TOTAL",
            "q3": "⏱️ Q3 TOTAL",
            "q4": "⏱️ Q4 TOTAL",
            "spread": "📏 FULL GAME SPREAD",
            "h2h": "💰 MONEYLINE",
        }.get(market, market.upper())

        
        # -------------------------
        # BET STAGE
        # -------------------------

        confirmed = lineups_confirmed(
            game_time_utc=req.game_time,
            injury_map=get_injury_context(),
            home=home_abbr,
            away=away_abbr,
        )

        bet_stage = "CONFIRMED" if confirmed else "EARLY"
        stage_emoji = "🔥" if confirmed else "📢"
      
        
        # -------------------------
        # DEDUPLICATION KEY
        # -------------------------

        key = f"{game_id}_{market}_{market_line}_{bet_stage}"

        if key in SENT_ALERTS:
            print("DEDUPED:", key)
            continue

        SENT_ALERTS.add(key)

        # -------------------------
        # CONTEXT TAGS (DISPLAY ONLY)
        # -------------------------
        b2b_tags = []
        if getattr(req, "team_a_b2b", False):
            b2b_tags.append(f"{req.team_a} B2B")
        if getattr(req, "team_b_b2b", False):
            b2b_tags.append(f"{req.team_b} B2B")

        b2b_label = f"🔁 Back-to-Back: {', '.join(b2b_tags)}\n" if b2b_tags else ""
 
        # -------------------------
        # TELEGRAM MESSAGE
        # -------------------------

        message = (
            f"{stage_emoji} {bet_stage} {market_label}\n"
            f"{side_emoji} PICK: {bet_side}\n\n"
            f"{req.team_a} vs {req.team_b}\n"
            f"{b2b_label}"
            f"✈️ Away Travel: {round(req.team_b_travel_km)} km\n"
            f"📈 Line: {market_line}\n"
            f"🎯 Fair: {round(fair, 2)}\n"
            f"⚡ Edge: {round(edge * 100, 2)}%\n"
            f"🏆 Tier: {tier}\n"
            f"📊 Percentile: {pct}%\n"
            f"🏥 Injuries Included: YES\n"
        )

                # -------------------------
        # SEND / QUEUE ALERT
        # -------------------------

        # ❌ EARLY alerts disabled to reduce usage
        if key not in PREGAME_ALERTS:
            PREGAME_ALERTS[key] = {
                "game_time": req.game_time,
                "message": message,
                "home_abbr": home_abbr,
                "away_abbr": away_abbr,
                "sent_10": False,
                "sent_2": False,
            }


    return {"game": game_id, "markets": results}


# =========================================================
# API ENDPOINT
# =========================================================

@app.post("/simulate")
async def simulate(req: SimulationRequest):
    # run the core (sync) simulation in a worker thread
    return await anyio.to_thread.run_sync(run_simulation, req)


# =========================================================
# PRE-GAME ALERT SCHEDULER
# =========================================================

async def pregame_alert_scheduler():
    while True:
        now = datetime.now(timezone.utc)

        for key, alert in list(PREGAME_ALERTS.items()):
            game_time = alert["game_time"]

            # 🔔 10-minute alert
            if not alert.get("sent_10") and now >= game_time - timedelta(minutes=10):
                confirmed = lineups_confirmed(
                    game_time_utc=alert["game_time"],
                    injury_map=get_injury_context(),
                    home=alert["home_abbr"],
                    away=alert["away_abbr"],
            )

                    
                
                prefix = "⏰ 10 MIN 🏀 FULL GAME TOTAL\n"
                prefix += "✅ Lineups confirmed\n\n" if confirmed else "⏳ Lineups pending\n\n"
                msg = alert["message"].replace("📢 EARLY", "⏰ 10 MIN")
                send_telegram_alert(prefix + msg)
                alert["sent_10"] = True

            # 🚨 2-minute alert
            if not alert.get("sent_2") and now >= game_time - timedelta(minutes=2):
                confirmed = lineups_confirmed(
                    game_time_utc=alert["game_time"],
                    injury_map=get_injury_context(),
                    home=alert["home_abbr"],
                    away=alert["away_abbr"],
            )

                prefix = "🚨 2 MIN 🏀 FULL GAME TOTAL\n"
                prefix += "✅ Lineups confirmed\n\n" if confirmed else "⏳ Lineups pending\n\n"
                msg = alert["message"].replace("📢 EARLY", "🚨 2 MIN")
                send_telegram_alert(prefix + msg)
                alert["sent_2"] = True

        await asyncio.sleep(60)


# =========================================================
# DAILY AUTO-RUN (FIXED)
# =========================================================

AUTO_RUN_TIME = "05:12"


async def daily_auto_run():
    print("⏰ Daily auto-run scheduler active")
    last_run_date = None

    while True:
        now = datetime.now()

        run_hour, run_minute = map(int, AUTO_RUN_TIME.split(":"))
        run_time = now.replace(
            hour=run_hour, minute=run_minute, second=0, microsecond=0
        )

        should_run = now >= run_time and last_run_date != now.date()

        if should_run:
            try:
                print("🚀 Running daily auto-run")

                # pull today's games from NBA API
                games = build_model_inputs()
                for g in games or []:
                    req = SimulationRequest(
                        team_a=g["team_a"],
                        team_b=g["team_b"],
                        game_time=g["game_time"],
                        home_team=g.get("home_team", "A"),

                        team_a_travel_km=g.get("team_a_travel_km", 0),
                        team_b_travel_km=g.get("team_b_travel_km", 0),
                        team_a_b2b=g.get("team_a_b2b", False),
                        team_b_b2b=g.get("team_b_b2b", False),
                )

                    result = await anyio.to_thread.run_sync(run_simulation, req)
                    print("✅ Auto-run game complete:", result)

                last_run_date = now.date()
                print("✅ Daily auto-run complete")
            except Exception as e:
                print("❌ Auto-run error:", e)

        await asyncio.sleep(60)

async def live_game_monitor():
    print("📡 Live game monitor active")

    while True:
        try:
            games = build_model_inputs()
            for g in games or []:
                req = SimulationRequest(
                    team_a=g["team_a"],
                    team_b=g["team_b"],
                    game_time=g["game_time"],
                    home_team=g.get("home_team", "A"),
                    team_a_travel_km=g.get("team_a_travel_km", 0),
                    team_b_travel_km=g.get("team_b_travel_km", 0),
                    team_a_b2b=g.get("team_a_b2b", False),
                    team_b_b2b=g.get("team_b_b2b", False),
                )

                await anyio.to_thread.run_sync(run_simulation, req)

        except Exception as e:
            print("❌ Live monitor error:", e)

        # every 3 minutes (safe, low usage)
        await asyncio.sleep(180)

# =========================================================
# STARTUP
# =========================================================

@app.on_event("startup")
async def startup():
    asyncio.create_task(pregame_alert_scheduler())
    asyncio.create_task(daily_auto_run())
    asyncio.create_task(live_game_monitor())

@app.get("/test/telegram")
def test_telegram():
    send_telegram_alert("✅ Telegram test successful")
    return {"ok": True}

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "betpicking-ai",
        "time": datetime.now(timezone.utc).isoformat(),
    }

from fastapi import Response

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)
