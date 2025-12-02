# ============================================================
# SMC + Fibo Zone + M1 Confirm
# PAPER MODE ONLY
# SL = H1 Swing (20 bars lookback, max 1000 points from entry)
# ============================================================

import os
import time
import json
import math
import logging
import requests
from datetime import datetime, UTC
from collections import defaultdict

import ccxt
import pandas as pd
import numpy as np

# =========================
# CONFIG
# =========================
SYMBOL = 'BTC-USDT-SWAP'

TIMEFRAME_H1 = '1h'
TIMEFRAME_M1 = '1m'

CHECK_INTERVAL = 30

# PAPER MODE
PAPER_TRADING = True
PAPER_START_BALANCE = 50.0

# SWING SL RULE
H1_SWING_LOOKBACK = 20
MAX_SL_POINTS = 1000        # จุด (BTC = dollars)

# FIBO ZONE
FIBO_ZONE_LOW  = 0.33
FIBO_ZONE_HIGH = 0.786

# TELEGRAM
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# =========================
# LOG
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("paper_smc")

# =========================
# TELEGRAM (one-shot)
# =========================
_sent = set()

def send_telegram(msg: str, tag: str | None = None):
    if tag and tag in _sent:
        return
    if tag:
        _sent.add(tag)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("[TG] " + msg)
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(
            url,
            params={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram error: {e}")

# =========================
# EXCHANGE (public)
# =========================
exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})
exchange.load_markets()

# =========================
# PAPER WALLET
# =========================
paper_wallet = {
    "balance": PAPER_START_BALANCE,
    "equity": PAPER_START_BALANCE
}

current_pos = None
stats = {
    "tp": 0,
    "sl": 0,
    "be": 0,
    "trades": []
}

# =========================
# HELPERS
# =========================
def fetch_ohlcv(tf, limit):
    time.sleep(0.3)
    return exchange.fetch_ohlcv(SYMBOL, tf, limit=limit)

def now_utc():
    return datetime.now(UTC)

# -------------------------
# SWING
# -------------------------
def find_h1_swings(ohlcv, left=3, right=3):
    highs = [c[2] for c in ohlcv]
    lows  = [c[3] for c in ohlcv]
    out = []
    n = len(ohlcv)

    for i in range(left, n-right):
        if highs[i] == max(highs[i-left:i+right+1]):
            out.append(("high", i, highs[i]))
        if lows[i] == min(lows[i-left:i+right+1]):
            out.append(("low", i, lows[i]))
    return out

def sl_from_h1_swing(ohlcv_h1, side, entry):
    swings = find_h1_swings(ohlcv_h1)
    last_idx = len(ohlcv_h1) - 1

    for stype, idx, price in reversed(swings):
        if last_idx - idx > H1_SWING_LOOKBACK:
            continue

        if side == "long" and stype == "low" and price < entry:
            if entry - price <= MAX_SL_POINTS:
                return price

        if side == "short" and stype == "high" and price > entry:
            if price - entry <= MAX_SL_POINTS:
                return price

    # fallback
    return entry - MAX_SL_POINTS if side == "long" else entry + MAX_SL_POINTS

# -------------------------
# FIBO
# -------------------------
def calc_fibo(lo, hi):
    diff = hi - lo
    return {
        "0": hi,
        "33": hi - diff * 0.33,
        "78.6": hi - diff * 0.786,
        "100": lo
    }

def in_fibo_zone(price, fibo):
    lo = min(fibo["33"], fibo["78.6"])
    hi = max(fibo["33"], fibo["78.6"])
    return lo <= price <= hi

# -------------------------
# MACD (closed candle)
# -------------------------
def macd_cross_dir(closes):
    s = pd.Series(closes)
    ema_fast = s.ewm(span=12).mean()
    ema_slow = s.ewm(span=26).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=9).mean()

    if len(macd) < 2:
        return None

    prev = macd.iloc[-2] - signal.iloc[-2]
    curr = macd.iloc[-1] - signal.iloc[-1]

    if prev <= 0 and curr > 0:
        return "up"
    if prev >= 0 and curr < 0:
        return "down"
    return None

# =========================
# MAIN LOOP
# =========================
log.info("🧪 PAPER MODE STARTED")

send_telegram(
    "🧪 PAPER MODE START\n"
    "SL = H1 Swing (20 bars, max 1000 pts)\n"
    "No real orders",
    tag="start"
)

while True:
    try:
        price = exchange.fetch_ticker(SYMBOL)["last"]

        # ----------------------------------
        # STEP 1: H1 Trend (simple BOS idea)
        # ----------------------------------
        h1 = fetch_ohlcv(TIMEFRAME_H1, 100)
        hi = max(c[2] for c in h1[-20:])
        lo = min(c[3] for c in h1[-20:])

        side = "long" if price > (hi + lo) / 2 else "short"

        # ----------------------------------
        # STEP 2: Fibo Zone
        # ----------------------------------
        fibo = calc_fibo(lo, hi)
        if not in_fibo_zone(price, fibo):
            time.sleep(CHECK_INTERVAL)
            continue

        # ----------------------------------
        # STEP 3: M1 MACD Confirm (closed)
        # ----------------------------------
        m1 = fetch_ohlcv(TIMEFRAME_M1, 120)
        closes = [c[4] for c in m1[:-1]]
        macd_dir = macd_cross_dir(closes)

        if (side == "long" and macd_dir != "up") or (side == "short" and macd_dir != "down"):
            time.sleep(CHECK_INTERVAL)
            continue

        # ----------------------------------
        # ENTRY (PAPER)
        # ----------------------------------
        if current_pos is None:
            sl = sl_from_h1_swing(h1, side, price)

            current_pos = {
                "side": side,
                "entry": price,
                "sl": sl,
                "time": now_utc()
            }

            send_telegram(
                f"📈 ENTRY {side.upper()}\n"
                f"Entry: {price:.2f}\n"
                f"SL (H1 Swing): {sl:.2f}",
                tag=f"entry:{int(time.time())}"
            )

        # ----------------------------------
        # MANAGE SL
        # ----------------------------------
        if current_pos:
            side = current_pos["side"]
            entry = current_pos["entry"]
            sl = current_pos["sl"]

            hit = (price <= sl) if side == "long" else (price >= sl)
            if hit:
                pnl = (price - entry) if side == "long" else (entry - price)

                if abs(pnl) < 5:
                    stats["be"] += 1
                    result = "BE"
                elif pnl > 0:
                    stats["tp"] += 1
                    result = "TP"
                else:
                    stats["sl"] += 1
                    result = "SL"

                stats["trades"].append({
                    "side": side,
                    "entry": entry,
                    "exit": price,
                    "pnl": pnl,
                    "result": result,
                    "time": now_utc().isoformat()
                })

                send_telegram(
                    f"❌ EXIT {result}\n"
                    f"Exit: {price:.2f}\nPnL: {pnl:.2f}",
                    tag=f"exit:{int(time.time())}"
                )

                current_pos = None

        # ----------------------------------
        # MONTHLY REPORT (20th)
        # ----------------------------------
        now = now_utc()
        if now.day == 20 and now.hour == 0 and now.minute < 2:
            send_telegram(
                f"📊 MONTHLY PAPER REPORT\n"
                f"TP: {stats['tp']}\n"
                f"SL: {stats['sl']}\n"
                f"BE: {stats['be']}",
                tag="monthly"
            )

        time.sleep(CHECK_INTERVAL)

    except Exception as e:
        log.exception(f"Main loop error: {e}")
        time.sleep(10)
