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

# =========================================================
# TELEGRAM
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠ Telegram not configured")
        return

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
    if score >= 86:
        return "ELITE"
    if score >= 71:
        return "VERY STRONG"
    if score >= 60:
        return "STRONG"
    if score >= 41:
        return "LEAN"
    return "NOISE"


# =========================================================
# CORE SIMULATION LOGIC (single source of truth)
# =========================================================

def run_simulation(req: SimulationRequest):
    rng = np.random.default_rng()
    reset_alerts_if_new_day()

    odds_map = fetch_nba_totals_odds()
    print(f"ODDS MAP GAMES: {list(odds_map.keys())}")

    home_abbr = team_name_to_abbr(req.team_a)
    away_abbr = team_name_to_abbr(req.team_b)
    if not home_abbr or not away_abbr:
        print(f"❌ Invalid team names: {req.team_a} vs {req.team_b}")
        return None

    game_id = f"{home_abbr}_vs_{away_abbr}"

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

    # NOTE: Fatigue applied BEFORE market scaling
    # -------------------------
    # BACK-TO-BACK FATIGUE 
    # -------------------------
    if getattr(req, "team_a_b2b", False):
        # home team less impacted, away team more impacted
        adj_a -= 1.0 if req.home_team == "A" else 2.0

    if getattr(req, "team_b_b2b", False):
        adj_b -= 1.0 if req.home_team == "B" else 2.0

    results = {}

    for market, cfg in MARKET_CONFIG.items():

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
        totals = rng.normal(mean, cfg["sd"], SIMULATIONS)

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

        results[market] = {
            "line": market_line,
            "fair": round(fair, 2),
            "edge": round(edge, 4),
            "pct": pct,
            "tier": tier,
            "signal": signal,
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
                    )
                    result = await anyio.to_thread.run_sync(run_simulation, req)
                    print("✅ Auto-run game complete:", result)

                last_run_date = now.date()
                print("✅ Daily auto-run complete")
            except Exception as e:
                print("❌ Auto-run error:", e)

        await asyncio.sleep(60)


# =========================================================
# STARTUP
# =========================================================

@app.on_event("startup")
async def startup():
    asyncio.create_task(pregame_alert_scheduler())
    asyncio.create_task(daily_auto_run())


@app.get("/test/telegram")
def test_telegram():
    send_telegram_alert("✅ Telegram test successful")
    return {"ok": True}
