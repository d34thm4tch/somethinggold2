"""
Gold Breakout Trading Bot for Capital.com (ATR-Confirmed Range Breakout)
===========================================================================

Runs on 15-minute candles - well suited to breakout setups around
consolidation periods and session opens/news.

LOGIC:
  - Looks at the last RANGE_LOOKBACK 15-min bars (excluding the current
    one) to define a recent consolidation range (highest high, lowest
    low).
  - Computes ATR (Average True Range) to size a "genuine move" buffer,
    so small noise breakouts don't trigger false entries.
  - If price closes above (range high + buffer)  -> breakout up -> open LONG
  - If price closes below (range low  - buffer)   -> breakout down -> open SHORT
  - If already in a position and price falls back inside the prior
    range, that's treated as a failed breakout -> close early rather
    than waiting for the full stop loss to hit.

SETUP: same as the other bots - pip install requests, set
CAPITAL_API_KEY / CAPITAL_IDENTIFIER / CAPITAL_PASSWORD as env vars
(GitHub secrets), confirm GOLD_EPIC with --find-epic if needed.

This targets the DEMO base URL only.
"""

import os
import sys
import time
import csv
import requests
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"

API_KEY = os.getenv("CAPITAL_API_KEY")
IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER")
PASSWORD = os.getenv("CAPITAL_PASSWORD")

GOLD_EPIC = "GOLD"          # use the same epic you already confirmed works
RESOLUTION = "MINUTE_15"
RANGE_LOOKBACK = 8          # last 8 x 15-min bars = ~2 hours defines the range
ATR_PERIOD = 14
BREAKOUT_BUFFER_MULTIPLIER = 0.5   # breakout must clear the range by 0.5x ATR

POSITION_SIZE = 0.1

# Stop loss / take profit sized off the same ATR used for the breakout
# buffer, so they scale with actual gold volatility rather than being a
# fixed dollar amount that's too tight on volatile days.
STOP_LOSS_ATR_MULTIPLIER = 1.0
TAKE_PROFIT_ATR_MULTIPLIER = 2.0

LOG_FILE = "breakout_trades_log.csv"
HEARTBEAT_FILE = "breakout_bot_heartbeat.csv"

# ---------------------------------------------------------------------------
# SESSION HANDLING
# ---------------------------------------------------------------------------

class CapitalSession:
    def __init__(self):
        self.cst = None
        self.security_token = None
        self.last_auth_time = 0

    def authenticate(self):
        if not all([API_KEY, IDENTIFIER, PASSWORD]):
            sys.exit("Missing CAPITAL_API_KEY / CAPITAL_IDENTIFIER / CAPITAL_PASSWORD env vars.")
        resp = requests.post(
            f"{BASE_URL}/session",
            headers={"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"},
            json={"identifier": IDENTIFIER, "password": PASSWORD, "encryptedPassword": False},
        )
        resp.raise_for_status()
        self.cst = resp.headers["CST"]
        self.security_token = resp.headers["X-SECURITY-TOKEN"]
        self.last_auth_time = time.time()
        print(f"[{datetime.now()}] Authenticated successfully.")

    def headers(self):
        if time.time() - self.last_auth_time > 8 * 60:
            self.authenticate()
        return {
            "CST": self.cst,
            "X-SECURITY-TOKEN": self.security_token,
            "Content-Type": "application/json",
        }


session = CapitalSession()

# ---------------------------------------------------------------------------
# MARKET / EPIC DISCOVERY (run once with --find-epic, optional)
# ---------------------------------------------------------------------------

def find_gold_epic():
    session.authenticate()
    resp = requests.get(f"{BASE_URL}/markets", headers=session.headers(),
                         params={"searchTerm": "gold"})
    resp.raise_for_status()
    markets = resp.json().get("markets", [])
    if not markets:
        print("No markets found for 'gold'. Try searchTerm='XAU' instead.")
        return
    print("Matching markets:")
    for m in markets:
        print(f"  epic={m.get('epic'):<15} name={m.get('instrumentName')}")

# ---------------------------------------------------------------------------
# PRICE DATA + ATR
# ---------------------------------------------------------------------------

def get_recent_bars(num_bars=100):
    resp = requests.get(
        f"{BASE_URL}/prices/{GOLD_EPIC}",
        headers=session.headers(),
        params={"resolution": RESOLUTION, "max": num_bars},
    )
    resp.raise_for_status()
    data = resp.json().get("prices", [])
    highs = [(p["highPrice"]["bid"] + p["highPrice"]["ask"]) / 2 for p in data]
    lows = [(p["lowPrice"]["bid"] + p["lowPrice"]["ask"]) / 2 for p in data]
    closes = [(p["closePrice"]["bid"] + p["closePrice"]["ask"]) / 2 for p in data]
    return highs, lows, closes


def calculate_atr(highs, lows, closes, period=ATR_PERIOD):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period

# ---------------------------------------------------------------------------
# POSITIONS
# ---------------------------------------------------------------------------

def get_open_position():
    resp = requests.get(f"{BASE_URL}/positions", headers=session.headers())
    resp.raise_for_status()
    for pos in resp.json().get("positions", []):
        if pos["market"]["epic"] == GOLD_EPIC:
            return pos
    return None


def open_position(direction, current_price, atr):
    stop_distance = atr * STOP_LOSS_ATR_MULTIPLIER
    profit_distance = atr * TAKE_PROFIT_ATR_MULTIPLIER
    if direction == "BUY":
        stop_level = current_price - stop_distance
        profit_level = current_price + profit_distance
    else:
        stop_level = current_price + stop_distance
        profit_level = current_price - profit_distance

    payload = {
        "epic": GOLD_EPIC,
        "direction": direction,
        "size": POSITION_SIZE,
        "stopLevel": round(stop_level, 2),
        "profitLevel": round(profit_level, 2),
    }
    resp = requests.post(f"{BASE_URL}/positions", headers=session.headers(), json=payload)
    resp.raise_for_status()
    deal_ref = resp.json().get("dealReference")
    print(f"[{datetime.now()}] Opened {direction} at ~{current_price:.2f} "
          f"(stop={round(stop_level,2)}, profit={round(profit_level,2)}, ATR={round(atr,2)}) (ref {deal_ref})")
    log_trade(direction, current_price, "OPEN")
    return deal_ref


def close_position(position):
    deal_id = position["position"]["dealId"]
    resp = requests.delete(f"{BASE_URL}/positions/{deal_id}", headers=session.headers())
    resp.raise_for_status()
    print(f"[{datetime.now()}] Closed position {deal_id}")
    log_trade(position["position"]["direction"], position["position"]["level"], "CLOSE")

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

def log_trade(direction, price, action):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "action", "direction", "price"])
        writer.writerow([datetime.now().isoformat(), action, direction, price])


def write_heartbeat(price, range_high, range_low, atr, position):
    file_exists = os.path.isfile(HEARTBEAT_FILE)
    with open(HEARTBEAT_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "price", "range_high", "range_low", "atr", "position_open"])
        writer.writerow([
            datetime.now().isoformat(),
            round(price, 2) if price is not None else "",
            round(range_high, 2) if range_high is not None else "",
            round(range_low, 2) if range_low is not None else "",
            round(atr, 2) if atr is not None else "",
            "yes" if position else "no",
        ])

# ---------------------------------------------------------------------------
# MAIN LOGIC (single run, invoked by GitHub Actions cron every 15 minutes)
# ---------------------------------------------------------------------------

def run_once():
    session.authenticate()
    try:
        highs, lows, closes = get_recent_bars()

        if len(closes) < RANGE_LOOKBACK + 1:
            print(f"[{datetime.now()}] Not enough data yet for range calculation.")
            return

        current_price = closes[-1]
        recent_high = max(highs[-(RANGE_LOOKBACK + 1):-1])
        recent_low = min(lows[-(RANGE_LOOKBACK + 1):-1])
        atr = calculate_atr(highs, lows, closes)
        buffer = atr * BREAKOUT_BUFFER_MULTIPLIER if atr else 0
        position = get_open_position()

        print(f"[{datetime.now()}] Price={current_price:.2f}  "
              f"RangeHigh={recent_high:.2f}  RangeLow={recent_low:.2f}  ATR={atr}")
        write_heartbeat(current_price, recent_high, recent_low, atr, position)

        breakout_up = current_price > recent_high + buffer
        breakout_down = current_price < recent_low - buffer

        if position is None:
            if atr is None:
                print(f"[{datetime.now()}] ATR not available yet, skipping any new entries this run.")
            elif breakout_up:
                open_position("BUY", current_price, atr)
            elif breakout_down:
                open_position("SELL", current_price, atr)
        else:
            direction = position["position"]["direction"]
            # Failed breakout: price fell back inside the prior range - exit early
            if direction == "BUY" and current_price < recent_high:
                close_position(position)
            elif direction == "SELL" and current_price > recent_low:
                close_position(position)

    except requests.HTTPError as e:
        print(f"[{datetime.now()}] API error: {e}")
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")


if __name__ == "__main__":
    if "--find-epic" in sys.argv:
        find_gold_epic()
    else:
        run_once()
