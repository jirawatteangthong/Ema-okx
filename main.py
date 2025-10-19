# main.py
# SMC + FVG + Fibo 2-pass + Volume Profile (POC SL) + M5/M1 confirmation
# OKX Futures (ccxt) | Leverage 20 | Open 0.8 of equity (but capped by risk if needed)
# Telegram notifications & Monthly summary
#
# WARNING: This script sends real orders to OKX if API keys are set and sandbox=False.
# Test in sandbox/paper mode first.

import os
import time
import math
import json
import logging
import threading
from datetime import datetime, timedelta
from collections import defaultdict, Counter

import ccxt
import requests
import numpy as np
import pandas as pd
from dateutil import tz

# ---------------------------
# CONFIG (แก้ค่าตรงนี้ตามต้องการ)
# ---------------------------
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSPHRASE')

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID')

SYMBOL = 'BTC-USDT-SWAP'   # OKX perpetual symbol (ccxt uses this for OKX)
TIMEFRAME_H1 = '1h'
TIMEFRAME_M5 = '5m'
TIMEFRAME_M1 = '1m'
INIT_OHLCV_LIMIT = 500    # ตรวจ H1 ย้อนหลัง 500 แท่งตอนเริ่ม

LEVERAGE = 20
TARGET_PORTFOLIO_FACTOR = 0.8  # open up to 80% of equity notionally (then capped by risk)
TARGET_RISK_PCT = 0.02  # maximum risk per trade (2%) - safety cap (you can set lower)
ACTUAL_OKX_MARGIN_FACTOR = 0.06824  # as in your code: used to compute required margin

# Fibo settings
FIBO_ENTRY_MIN = 0.33
FIBO_ENTRY_MAX = 0.786
FIBO2_EXT_MIN = 1.33
FIBO2_EXT_MAX = 1.618
FIBO2_SL_LEVEL = 0.786  # 78.6 level on fibo2 used as SL when in TP2 phase
FIBO80_FALLBACK = 0.80  # fallback if VP weak

# Confirmation / structure settings
SWING_LEFT = 3
SWING_RIGHT = 3
SWING_LOOKBACK_H1 = 50  # when searching H1 swings to compute fibo1
M5_LOOKBACK = 200
CHOCH_BOS_SIDEWAY_THRESHOLD = 3  # times swapped > threshold within window => sideway
CHOCH_BOS_WINDOW = 10  # window of H1 bars to count swaps for sideway detection

# Volume Profile settings
VP_BUCKET_SIZE = None  # if None, auto based on price range; else set price bucket size
VP_MIN_SHARE_FOR_STRONG = 0.18  # if vol_in_entry_zone / total_vol >= this -> strong

# Execution / monitoring
CHECK_INTERVAL = 15  # seconds between checks in main loop (can set higher to 60)
COOLDOWN_H1_AFTER_TRADE = 3  # number of H1 bars to cooldown after TP2/SL
TP1_CLOSE_PERCENT = 0.60  # close 60% at TP1
TP2_CLOSE_PERCENT = 0.40  # remaining closed at TP2 (or on emergency close)
M1_CHOCH_DEBOUNCE = 2  # require 2 closes forming CHOCH to avoid whip

# precision / tolerance
PRICE_TOLERANCE_PCT = 0.0005  # 0.05% tolerance for hitting levels
POC_BUFFER_PCT = 0.001  # 0.1% buffer around POC for considering "break"

# Monthly stats file
STATS_FILE = 'trades_stats.json'

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger('fibo_bot')

# ---------------------------
# GLOBAL STATE
# ---------------------------
exchange = None
market_info = None

current_position = None  # dict with keys: side, size(contracts), entry_price, opened_at
pending_trade = None     # details of trade being managed (fibo levels, POC, SL etc.)
cooldown_until = None
monthly_stats = {
    'month_year': None,
    'tp_count': 0,
    'sl_count': 0,
    'total_pnl': 0.0,
    'trades': []
}


# ---------------------------
# UTIL / TELEGRAM
# ---------------------------
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or TELEGRAM_TOKEN.startswith('YOUR_'):
        logger.warning("Telegram token/chat_id not configured - skipping send.")
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        logger.info("Telegram sent: " + (msg.splitlines()[0] if msg else ''))
    except Exception as e:
        logger.error("Failed to send telegram: %s", e)


def human_dt(ts_ms):
    return datetime.fromtimestamp(ts_ms/1000).strftime('%Y-%m-%d %H:%M')


# ---------------------------
# EXCHANGE SETUP
# ---------------------------
def setup_exchange():
    global exchange, market_info
    try:
        exchange = ccxt.okx({
            'apiKey': API_KEY,
            'secret': SECRET,
            'password': PASSWORD,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',
                'adjustForTimeDifference': True,
            },
            'timeout': 30000
        })
        exchange.set_sandbox_mode(False)  # change to True to test in OKX sandbox
        exchange.load_markets()
        market_info = exchange.market(SYMBOL)
        # set leverage (cross mode)
        try:
            exchange.set_leverage(LEVERAGE, SYMBOL, params={'mgnMode': 'cross'})
            logger.info(f"Set leverage {LEVERAGE}x for {SYMBOL}")
        except Exception as e:
            logger.warning("Could not set leverage: %s", e)
    except Exception as e:
        logger.critical("Exchange setup failed: %s", e)
        raise


# ---------------------------
# DATA FETCH HELPERS
# ---------------------------
def fetch_ohlcv_safe(symbol, timeframe, limit=200):
    for i in range(3):
        try:
            data = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            return data
        except Exception as e:
            logger.warning(f"fetch_ohlcv error {e}, retrying...")
            time.sleep(5)
    raise RuntimeError("Failed to fetch ohlcv after retries")


# ---------------------------
# SWING / STRUCTURE DETECTION
# ---------------------------
def find_swings_from_ohlcv(ohlcv, left=SWING_LEFT, right=SWING_RIGHT):
    # ohlcv: list of [ts, open, high, low, close, vol]
    swings = []
    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]
    L = len(ohlcv)
    for i in range(left, L - right):
        is_high = highs[i] == max(highs[i - left:i + right + 1])
        is_low = lows[i] == min(lows[i - left:i + right + 1])
        if is_high:
            swings.append(('high', i, ohlcv[i][0], highs[i]))
        if is_low:
            swings.append(('low', i, ohlcv[i][0], lows[i]))
    return swings


def detect_bos_choch_from_swings(ohlcv, swings):
    # iterate swings in order and detect BOS / CHOCH using close-based confirm
    structure = []
    last_trend = None
    if not swings:
        return structure
    for idx in range(1, len(swings)):
        stype_prev, i_prev, ts_prev, price_prev = swings[idx - 1]
        stype, i, ts, price = swings[idx]
        close = ohlcv[i][4]
        signal = None
        trend = last_trend
        # BOS conditions (simple): break previous swing price by close
        # If current is high and close > prev_price => up BOS
        if stype == 'high' and close > price_prev:
            signal = 'BOS'
            trend = 'up'
        elif stype == 'low' and close < price_prev:
            signal = 'BOS'
            trend = 'down'
        else:
            # CHOCH detection: if last_trend existed and close crosses opposite
            if last_trend == 'up' and close < price_prev:
                signal = 'CHOCH'
                trend = 'down'
            elif last_trend == 'down' and close > price_prev:
                signal = 'CHOCH'
                trend = 'up'
        if signal:
            structure.append({
                'signal': signal,
                'trend': trend,
                'price': price,
                'timestamp': ts
            })
            last_trend = trend
    return structure


# ---------------------------
# FIBO + VOLUME PROFILE
# ---------------------------
def calc_fibo_levels(low, high):
    diff = high - low
    levels = {}
    # For Fibo1 we typically consider 0=high, 100=low for retrace down
    levels['0'] = high
    levels['100'] = low
    levels['33'] = high - 0.33 * diff
    levels['38.2'] = high - 0.382 * diff
    levels['50'] = high - 0.5 * diff
    levels['61.8'] = high - 0.618 * diff
    levels['78.6'] = high - 0.786 * diff
    # extension from low upward
    levels['ext133'] = low + 1.33 * diff
    levels['ext161.8'] = low + 1.618 * diff
    return levels


def calc_volume_profile_poc(ohlcv_bars, bucket_size=None):
    """
    Simple volume profile by price buckets.
    ohlcv_bars: list of [ts, open, high, low, close, vol]
    returns: (poc_price, buckets list [(price_center, vol)])
    """
    prices = []
    vols = []
    for b in ohlcv_bars:
        # use typical price * volume as proxy allocation across bucket
        prices.append((b[2] + b[3] + b[4]) / 3.0)  # typical
        vols.append(b[5] if b[5] is not None else 0.0)
    if not prices:
        return None, []
    min_p = min(prices)
    max_p = max(prices)
    if bucket_size is None:
        # choose bucket roughly as (range / 40) but at least tiny
        bucket_size = max((max_p - min_p) / 40.0, 0.5)
    # bucketize
    bins = defaultdict(float)
    for p, v in zip(prices, vols):
        idx = int((p - min_p) / bucket_size)
        center = min_p + (idx + 0.5) * bucket_size
        bins[center] += v
    buckets = sorted([(price, vol) for price, vol in bins.items()], key=lambda x: x[0])
    if not buckets:
        return None, []
    poc_price = max(buckets, key=lambda x: x[1])[0]
    return poc_price, buckets


def vp_zone_strength(buckets, zone_low, zone_high):
    total_vol = sum(v for p, v in buckets)
    if total_vol <= 0:
        return 0.0
    vol_in_zone = sum(v for p, v in buckets if zone_low <= p <= zone_high)
    return vol_in_zone / total_vol


# ---------------------------
# ENTRY / ORDER SIZE / MARGIN
# ---------------------------
def get_equity():
    # return available USDT in trading account
    try:
        bal = exchange.fetch_balance(params={'type': 'trade'})
        # try common path
        if 'USDT' in bal and 'total' in bal['USDT']:
            return float(bal['USDT']['total'])
        # fallback to info parsing
        info = bal.get('info', {}).get('data', [])
        for a in info:
            if a.get('ccy') == 'USDT' and a.get('type') == 'TRADE':
                return float(a.get('eq', a.get('availBal', 0.0)))
    except Exception as e:
        logger.error("Balance fetch error: %s", e)
    return 0.0


def compute_contracts_from_portfolio(equity, entry_price):
    """
    Compute contracts to open based on TARGET_PORTFOLIO_FACTOR and ACTUAL_OKX_MARGIN_FACTOR,
    then cap by risk using TARGET_RISK_PCT and SL distance later when SL known.
    Returns contracts (int-ish), notional, required_margin
    Note: we'll refine final size using SL distance when SL determined.
    """
    use_equity = equity * TARGET_PORTFOLIO_FACTOR
    target_notional = use_equity / ACTUAL_OKX_MARGIN_FACTOR  # notional we can open
    # BTC contract size (OKX) from user's original code: 0.0001 BTC per contract
    contract_size_btc = 0.0001
    base_amount_btc = target_notional / entry_price
    contracts_raw = base_amount_btc / contract_size_btc
    # round to nearest contract step (use integer)
    contracts = max(1, int(round(contracts_raw)))
    required_margin = (contracts * contract_size_btc * entry_price) * ACTUAL_OKX_MARGIN_FACTOR
    return contracts, contracts * contract_size_btc * entry_price, required_margin


def cap_size_by_risk(equity, entry_price, proposed_contracts, sl_price):
    # compute risk amount and adjust contracts so risk <= TARGET_RISK_PCT * equity
    if sl_price is None:
        return proposed_contracts
    contract_size_btc = 0.0001
    # distance in USDT per BTC
    dist = abs(entry_price - sl_price)
    if dist <= 0:
        return proposed_contracts
    # risk amount per 1 contract = dist * contract_size_btc
    risk_per_contract = dist * contract_size_btc
    max_risk_amount = equity * TARGET_RISK_PCT
    max_contracts_by_risk = int(max(1, math.floor(max_risk_amount / risk_per_contract)))
    final_contracts = min(proposed_contracts, max_contracts_by_risk)
    if final_contracts <= 0:
        return 0
    return final_contracts


# ---------------------------
# ORDER / POSITION HANDLERS (we won't set exchange TP/SL; bot will monitor and close)
# ---------------------------
def open_market_order(direction: str, contracts: int):
    """
    Open market order with given contracts (OKX swap). direction: 'long' or 'short'
    """
    side = 'buy' if direction == 'long' else 'sell'
    params = {'tdMode': 'cross'}
    try:
        verbose_amount = contracts
        amount_to_send = exchange.amount_to_precision(SYMBOL, float(contracts))
        order = exchange.create_market_order(SYMBOL, side, float(amount_to_send), params=params)
        logger.info(f"Market order placed: {side} {amount_to_send} contracts")
        time.sleep(2)
        # confirm position
        pos = get_current_position()
        return pos
    except Exception as e:
        logger.error("open_market_order failed: %s", e)
        send_telegram(f"⛔ Order Error: Failed to open market order: {e}")
        return None


def close_position_by_market(pos):
    if not pos:
        return False
    side_to_close = 'sell' if pos['side'] == 'long' else 'buy'
    amount = pos['size']
    params = {'tdMode': 'cross', 'reduceOnly': True}
    try:
        amount_prec = exchange.amount_to_precision(SYMBOL, float(amount))
        order = exchange.create_market_order(SYMBOL, side_to_close, float(amount_prec), params=params)
        logger.info(f"Sent market close order: {side_to_close} {amount_prec}")
        return True
    except Exception as e:
        logger.error("close_position_by_market failed: %s", e)
        send_telegram(f"⛔ Emergency Close Failed: {e}")
        return False


def get_current_position():
    """
    Fetch current net position for SYMBOL (OKX). Return dict or None.
    """
    try:
        positions = exchange.fetch_positions([SYMBOL])
        # in net mode should be at most one
        for p in positions:
            info = p.get('info', {})
            instId = info.get('instId') or info.get('symbol') or info.get('instrument_id')
            if instId == SYMBOL:
                pos_val = float(info.get('pos', 0))  # signed contracts? user's code had this
                if pos_val == 0:
                    return None
                entry_price = float(info.get('avgPx', 0.0))
                upl = float(info.get('upl', 0.0))
                size = abs(pos_val)
                side = 'long' if pos_val > 0 else 'short'
                return {'side': side, 'size': size, 'entry_price': entry_price, 'unrealized_pnl': upl}
        return None
    except Exception as e:
        logger.warning("get_current_position error: %s", e)
        return None


# ---------------------------
# STRATEGY LOGIC: prep fibo1, wait entry, open, manage TP1->Fibo2->TP2
# ---------------------------
def prepare_fibo1_and_vp():
    """
    Use H1 swings to prepare fibo1 and volume profile.
    Returns dict with fibo1_levels, entry_zone, poc_price, vp_buckets, swing_low/high timestamps & prices
    """
    ohlcv_h1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_H1, limit=INIT_OHLCV_LIMIT)
    swings = find_swings_from_ohlcv(ohlcv_h1, left=SWING_LEFT, right=SWING_RIGHT)
    if not swings:
        return None
    # pick most recent meaningful swing high & low pair: find last swing high and last swing low in lookback
    look = SWING_LOOKBACK_H1
    recent = ohlcv_h1[-look:]
    highs = [c[2] for c in recent]
    lows = [c[3] for c in recent]
    swing_high = max(highs)
    swing_low = min(lows)
    # For CHOCH/BOS we should have validated trend before calling this
    fibo1 = calc_fibo_levels(swing_low, swing_high)  # uses high as 0, low as 100
    entry_zone = (fibo1['33'], fibo1['78.6'])  # price range (high to lower price)
    # Volume profile on the same H1 swing range (we'll use the recent look)
    poc_price, buckets = calc_volume_profile_poc(recent, bucket_size=VP_BUCKET_SIZE)
    return {
        'ohlcv_h1': ohlcv_h1,
        'sw_low': swing_low,
        'sw_high': swing_high,
        'fibo1': fibo1,
        'entry_zone': entry_zone,
        'poc': poc_price,
        'vp_buckets': buckets
    }


def prepare_fibo2_after_tp1(tp1_price, timeframe_prefer='5m'):
    """
    After TP1 is hit at tp1_price, compute fibo2 using M5 or M1 data.
    We treat tp1_price as base=100 (low_ref for long), find a local swing high above it to compute extension.
    """
    # fetch small timeframe
    ohlcv_small = fetch_ohlcv_safe(SYMBOL, timeframe_prefer, limit=M5_LOOKBACK)
    # find highest high after TP1 (we assume TP1 occurred recently; in practice we'd store tp1 timestamp)
    highs = [b[2] for b in ohlcv_small]
    max_high = max(highs) if highs else tp1_price * 1.05
    # if max_high <= tp1_price, fallback to earlier high or a fractional extension
    if max_high <= tp1_price:
        max_high = tp1_price * 1.03  # fallback 3% above base
    low_ref = tp1_price
    high_ref = max_high
    diff = high_ref - low_ref
    fibo2 = {
        '100': low_ref,
        '78.6': low_ref + FIBO2_SL_LEVEL * diff,
        'ext133': low_ref + FIBO2_EXT_MIN * diff,
        'ext161.8': low_ref + FIBO2_EXT_MAX * diff
    }
    return fibo2, ohlcv_small


def check_price_in_zone(price, zone_low, zone_high):
    lo = min(zone_low, zone_high)
    hi = max(zone_low, zone_high)
    tol = PRICE_TOLERANCE_PCT
    return (price >= lo * (1 - tol)) and (price <= hi * (1 + tol))


# ---------------------------
# SIDEWAY DETECTION & INITIALIZATION (H1 500 scan)
# ---------------------------
def init_market_scan_and_set_trend():
    """
    On startup, fetch H1 500, detect swings and BOS/CHOCH counts, decide current trend
    Returns dict with bos_count, choch_count, latest_structure, trend_now
    """
    ohlcv = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_H1, limit=INIT_OHLCV_LIMIT)
    swings = find_swings_from_ohlcv(ohlcv, left=SWING_LEFT, right=SWING_RIGHT)
    structure = detect_bos_choch_from_swings(ohlcv, swings)
    bos_count = sum(1 for s in structure if s['signal'] == 'BOS')
    choch_count = sum(1 for s in structure if s['signal'] == 'CHOCH')
    trend_now = 'no-trend'
    latest = structure[-1] if structure else None
    if latest:
        trend_now = latest['trend']
    # decide phase
    phase = 'unknown'
    if bos_count > choch_count * 2 and bos_count + choch_count >= 3:
        phase = 'strong_trend'
    elif abs(bos_count - choch_count) < 3:
        phase = 'sideway'
    else:
        phase = 'transition'
    msg = (f"🧭 Initial Market Scan (H1 x {INIT_OHLCV_LIMIT})\n"
           f"• BOS: {bos_count}  • CHOCH: {choch_count}\n"
           f"• Latest structure: {latest['signal'] if latest else 'N/A'} "
           f"({latest['trend'] if latest else 'N/A'})\n"
           f"• Phase: {phase}\n")
    send_telegram(msg)
    return {'bos_count': bos_count, 'choch_count': choch_count, 'latest': latest, 'trend': trend_now, 'phase': phase}


# ---------------------------
# MONITOR / TRADE LOOP
# ---------------------------
def main_loop():
    global current_position, pending_trade, cooldown_until, monthly_stats
    logger.info("Starting main loop...")
    while True:
        try:
            # Skip trading during cooldown
            now = datetime.utcnow()
            if cooldown_until and now < cooldown_until:
                logger.info(f"In cooldown until {cooldown_until}, sleeping...")
                time.sleep(CHECK_INTERVAL)
                continue

            # Get current price (ticker)
            ticker = exchange.fetch_ticker(SYMBOL)
            current_price = float(ticker['last'])

            # refresh current position
            pos = get_current_position()
            current_position = pos

            # If we have no active position, look for setup
            if not current_position:
                # check sideway conditions (simple)
                # get small structure on H1 latest window
                ohlcv_h1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_H1, limit=120)
                swings = find_swings_from_ohlcv(ohlcv_h1, left=SWING_LEFT, right=SWING_RIGHT)
                structure = detect_bos_choch_from_swings(ohlcv_h1, swings)
                # check sideway by swaps in last CHOCH_BOS_WINDOW swings
                recent_struct = structure[-CHOCH_BOS_WINDOW:] if len(structure) >= CHOCH_BOS_WINDOW else structure
                swap_count = 0
                last_trend = None
                for s in recent_struct:
                    if last_trend and s['trend'] != last_trend:
                        swap_count += 1
                    last_trend = s['trend']
                # EMA proximity filter - fetch closes
                closes = [c[4] for c in ohlcv_h1[-300:]] if len(ohlcv_h1) >= 300 else [c[4] for c in ohlcv_h1]
                ema9 = pd.Series(closes).ewm(span=9, adjust=False).mean().iloc[-1] if len(closes) >= 9 else None
                ema50 = pd.Series(closes).ewm(span=50, adjust=False).mean().iloc[-1] if len(closes) >= 50 else None
                ema200 = pd.Series(closes).ewm(span=200, adjust=False).mean().iloc[-1] if len(closes) >= 200 else None
                ema_proximity = False
                if ema9 and ema50 and ema200:
                    gap1 = abs(ema9 - ema50) / ema50
                    gap2 = abs(ema50 - ema200) / ema200
                    if gap1 < 0.004 and gap2 < 0.008:
                        ema_proximity = True
                sideway = swap_count >= CHOCH_BOS_SIDEWAY_THRESHOLD or ema_proximity

                if sideway:
                    logger.info("Market in sideway by detection. Scanning but not opening positions.")
                    # Keep scanning but do not open new trades
                    time.sleep(CHECK_INTERVAL)
                    continue

                # Determine H1 trend by latest structure
                init_scan = init_market_scan_and_set_trend()
                trend = init_scan['trend']  # 'up' or 'down' or 'no-trend'
                if trend == 'no-trend':
                    logger.info("Trend unclear. Continue scanning...")
                    time.sleep(CHECK_INTERVAL)
                    continue

                # Prepare fibo1 and vp
                prep = prepare_fibo1_and_vp()
                if not prep:
                    logger.warning("Cannot prepare fibo1/vp, sleeping...")
                    time.sleep(CHECK_INTERVAL)
                    continue

                entry_low, entry_high = prep['entry_zone']
                poc = prep['poc']
                vp_buckets = prep['vp_buckets']
                fibo1 = prep['fibo1']
                # Check if current price is inside entry zone (for both long and short we need appropriate relation)
                # For long trend we expect price to be in retracement zone between high and low
                if trend == 'up':
                    in_zone = check_price_in_zone(current_price, entry_low, entry_high)
                else:
                    # for down trend, fibo entry zone similarly based on swapped high/low -> still check between entry bounds
                    in_zone = check_price_in_zone(current_price, entry_low, entry_high)

                if not in_zone:
                    logger.debug("Price not in entry zone yet. Sleeping...")
                    time.sleep(CHECK_INTERVAL)
                    continue

                # Volume profile strength
                vp_strength = vp_zone_strength(vp_buckets, min(entry_low, entry_high), max(entry_low, entry_high))
                use_fibo78_for_sl = FIBO_ENTRY_MAX
                if vp_strength < VP_MIN_SHARE_FOR_STRONG:
                    # fallback to 80%
                    use_fibo78_for_sl = FIBO80_FALLBACK
                    send_telegram(f"⚠ VP weak ({vp_strength:.2f}). Fallback using Fibo80 for SL/zone.")

                # Now wait for M5/M1 CHOCH/PA confirm
                # We check M5 then M1 for CHOCH in same direction as trend
                m5 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M5, limit=100)
                m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=200)
                # detect M5 choch (simple): last small swing structure change
                small_sw = find_swings_from_ohlcv(m5, left=1, right=1)
                small_struct = detect_bos_choch_from_swings(m5, small_sw)
                m5_confirm = False
                for s in small_struct[::-1]:
                    if s['trend'] == trend:
                        m5_confirm = True
                        break
                # also check M1 confirmation
                small_sw1 = find_swings_from_ohlcv(m1, left=1, right=1)
                small_struct1 = detect_bos_choch_from_swings(m1, small_sw1)
                m1_confirm = False
                for s in small_struct1[::-1]:
                    if s['trend'] == trend:
                        m1_confirm = True
                        break
                # also check price action simple: last candle bullish/bearish
                last_m1 = m1[-1] if m1 else None
                pa_confirm = False
                if last_m1:
                    if trend == 'up' and last_m1[4] > last_m1[1]:
                        pa_confirm = True
                    if trend == 'down' and last_m1[4] < last_m1[1]:
                        pa_confirm = True

                if not (m5_confirm or m1_confirm or pa_confirm):
                    logger.info("No M5/M1 confirmation yet. Waiting...")
                    time.sleep(CHECK_INTERVAL)
                    continue

                # All good - create position plan
                entry_price = current_price
                # SL use POC (with small buffer)
                if poc:
                    sl_price = poc * (1 - POC_BUFFER_PCT) if trend == 'up' else poc * (1 + POC_BUFFER_PCT)
                else:
                    # fallback to swing low/high whichever appropriate
                    if trend == 'up':
                        sl_price = fibo1['100']  # swing low
                    else:
                        sl_price = fibo1['0']  # swing high
                # compute contracts and cap by risk
                equity = get_equity()
                proposed_contracts, notional, req_margin = compute_contracts_from_portfolio(equity, entry_price)
                final_contracts = cap_size_by_risk(equity, entry_price, proposed_contracts, sl_price)
                if final_contracts <= 0:
                    send_telegram("⚠ Position size after risk cap is 0. Skipping trade.")
                    time.sleep(CHECK_INTERVAL)
                    continue

                # open market order
                pos = open_market_order('long' if trend == 'up' else 'short', final_contracts)
                if not pos:
                    logger.warning("Order open failed.")
                    time.sleep(CHECK_INTERVAL)
                    continue
                # record pending_trade info
                pending_trade = {
                    'side': pos['side'],
                    'entry_price': pos['entry_price'],
                    'size': pos['size'],
                    'fibo1': fibo1,
                    'poc': poc,
                    'vp_buckets': vp_buckets,
                    'sl_price': sl_price,
                    'tp1_price': fibo1['0'] if trend == 'up' else fibo1['100'],  # depending on orientation
                    'state': 'OPEN',
                    'opened_at': datetime.utcnow().isoformat(),
                    'trend': trend,
                    'fibo1_entry_zone': (entry_low, entry_high),
                    'use_fibo80_fallback': (use_fibo78_for_sl == FIBO80_FALLBACK),
                    'contracts': final_contracts
                }
                current_position = pos
                send_telegram(f"📈 ENTRY {pending_trade['side'].upper()} | Entry: {pending_trade['entry_price']:.2f} | "
                              f"SL(POC): {pending_trade['sl_price']:.2f} | TP1: {pending_trade['tp1_price']:.2f} | "
                              f"Contracts: {final_contracts}")
                logger.info("Opened position and monitoring...")

            else:
                # We have active position => manage it
                # fetch current price
                ticker = exchange.fetch_ticker(SYMBOL)
                current_price = float(ticker['last'])
                pos = current_position
                # ensure we have pending_trade reference or reconstruct minimal info
                if pending_trade is None:
                    # reconstruct minimal pending_trade from pos
                    pending_trade = {
                        'side': pos['side'],
                        'entry_price': pos['entry_price'],
                        'size': pos['size'],
                        'opened_at': datetime.utcnow().isoformat(),
                        'state': 'OPEN',
                        'contracts': pos['size'],
                        'trend': pos['side'] == 'long' and 'up' or 'down'
                    }
                # check TP1
                tp1_hit = False
                if pos['side'] == 'long':
                    if current_price >= pending_trade.get('tp1_price', float('inf')) * (1 - PRICE_TOLERANCE_PCT):
                        tp1_hit = True
                else:
                    if current_price <= pending_trade.get('tp1_price', 0) * (1 + PRICE_TOLERANCE_PCT):
                        tp1_hit = True

                if tp1_hit and pending_trade.get('state') == 'OPEN':
                    # close 60% of position (market)
                    close_amount = max(1, int(round(pending_trade['contracts'] * TP1_CLOSE_PERCENT)))
                    # build a partial close: OKX in net mode might require full close; attempt to close partial
                    # For simplicity, we will perform a market order of opposite side amount = close_amount
                    side_to_close = 'sell' if pos['side'] == 'long' else 'buy'
                    try:
                        amount_prec = exchange.amount_to_precision(SYMBOL, float(close_amount))
                        exchange.create_market_order(SYMBOL, side_to_close, float(amount_prec), params={'tdMode': 'cross', 'reduceOnly': True})
                        send_telegram(f"✅ TP1 HIT - closed {TP1_CLOSE_PERCENT*100:.0f}% at {current_price:.2f}")
                        pending_trade['state'] = 'TP1_HIT'
                        # compute Fibo2 and move SL for remaining
                        fibo2, small_ohlcv = prepare_fibo2_after_tp1(pending_trade['tp1_price'], timeframe_prefer=TIMEFRAME_M5)
                        pending_trade['fibo2'] = fibo2
                        # new SL is fibo2['78.6']
                        new_sl = fibo2['78.6']
                        pending_trade['sl_price_step2'] = new_sl
                        # move SL logically (we do not set exchange SL) - but we'll monitor and close if price <= new_sl for long
                        send_telegram(f"🔁 TP1 processed. Fibo2 computed. SL moved to Fibo2 78.6 = {new_sl:.2f}. TP2 zone = {fibo2['ext133']:.2f} - {fibo2['ext161.8']:.2f}")
                        # update cooldown guard maybe
                    except Exception as e:
                        logger.error("Failed to partially close at TP1: %s", e)
                        send_telegram(f"⚠ Partial close at TP1 failed: {e}")
                # if in TP1_HIT and Fibo2 exists, check for TP2 zone enter or M1 CHOCH emergency
                if pending_trade.get('state') == 'TP1_HIT' and 'fibo2' in pending_trade:
                    fibo2 = pending_trade['fibo2']
                    # check TP2 entry
                    tp2_zone_lo = fibo2['ext133']
                    tp2_zone_hi = fibo2['ext161.8']
                    in_tp2_zone = (current_price >= tp2_zone_lo * (1 - PRICE_TOLERANCE_PCT)) and (current_price <= tp2_zone_hi * (1 + PRICE_TOLERANCE_PCT))
                    # also check emergency M1 CHOCH opposite
                    m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=10)
                    # simple CHOCH on m1: check last 2 closes forming opposite
                    emergency = False
                    if len(m1) >= 3:
                        # detect quick structure change: look at last close vs previous swing
                        last_close = m1[-1][4]
                        prev_close = m1[-2][4]
                        if pending_trade['side'] == 'long' and last_close < prev_close:
                            # require two bars falling to avoid noise
                            if m1[-2][4] < m1[-3][4]:
                                emergency = True
                        if pending_trade['side'] == 'short' and last_close > prev_close:
                            if m1[-2][4] > m1[-3][4]:
                                emergency = True
                    if emergency:
                        send_telegram(f"⚠ M1 CHOCH opposite detected - emergency closing remaining position at {current_price:.2f}")
                        close_position_by_market(pos)
                        pending_trade['state'] = 'CLOSED_EMERGENCY'
                        add_trade_record('Emergency Close', pos, current_price)
                        cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                        current_position = None
                        pending_trade = None
                        time.sleep(5)
                        continue

                    if in_tp2_zone:
                        # close remaining
                        send_telegram(f"🏁 TP2 zone hit - closing remaining at {current_price:.2f}")
                        close_position_by_market(pos)
                        pending_trade['state'] = 'TP2_HIT'
                        add_trade_record('TP', pos, current_price)
                        # set cooldown
                        cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                        current_position = None
                        pending_trade = None
                        time.sleep(5)
                        continue

                    # also implement SL at fibo2 78.6 for remaining
                    sl2 = pending_trade.get('sl_price_step2')
                    if sl2:
                        if pending_trade['side'] == 'long' and current_price <= sl2 * (1 + PRICE_TOLERANCE_PCT):
                            send_telegram(f"🛑 SL Step2 hit (Fibo2 78.6) - closing remaining at {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL_STEP2'
                            add_trade_record('SL', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None
                            pending_trade = None
                            time.sleep(5)
                            continue
                        if pending_trade['side'] == 'short' and current_price >= sl2 * (1 - PRICE_TOLERANCE_PCT):
                            send_telegram(f"🛑 SL Step2 hit (Fibo2 78.6) - closing remaining at {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL_STEP2'
                            add_trade_record('SL', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None
                            pending_trade = None
                            time.sleep(5)
                            continue

                # else if still OPEN, check initial SL (POC or swing)
                if pending_trade.get('state') == 'OPEN':
                    slp = pending_trade.get('sl_price')
                    if slp:
                        if pending_trade['side'] == 'long' and current_price <= slp * (1 + PRICE_TOLERANCE_PCT):
                            send_telegram(f"❌ SL hit - closing at {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL'
                            add_trade_record('SL', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None
                            pending_trade = None
                            time.sleep(5)
                            continue
                        if pending_trade['side'] == 'short' and current_price >= slp * (1 - PRICE_TOLERANCE_PCT):
                            send_telegram(f"❌ SL hit - closing at {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL'
                            add_trade_record('SL', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None
                            pending_trade = None
                            time.sleep(5)
                            continue

            # loop sleep
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            logger.exception("Main loop error: %s", e)
            send_telegram(f"⛔ Bot Error: {e}")
            time.sleep(10)


# ---------------------------
# TRADES LOGGING / MONTHLY STATS
# ---------------------------
def add_trade_record(reason, pos_info, closed_price):
    global monthly_stats
    try:
        entry = pos_info.get('entry_price', 0.0)
        size = pos_info.get('size', 0.0)
        contract_size_btc = 0.0001
        pnl = 0.0
        if pos_info['side'] == 'long':
            pnl = (closed_price - entry) * size * contract_size_btc
        else:
            pnl = (entry - closed_price) * size * contract_size_btc
        # record
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        record = {
            'time': now,
            'side': pos_info['side'],
            'entry': entry,
            'closed': closed_price,
            'size': size,
            'pnl': pnl,
            'reason': reason
        }
        monthly_stats['trades'].append(record)
        monthly_stats['total_pnl'] += pnl
        if reason == 'TP':
            monthly_stats['tp_count'] += 1
        elif reason == 'SL':
            monthly_stats['sl_count'] += 1
        save_monthly_stats()
    except Exception as e:
        logger.error("add_trade_record error: %s", e)


def save_monthly_stats():
    try:
        monthly_stats['month_year'] = datetime.utcnow().strftime('%Y-%m')
        with open(STATS_FILE, 'w') as f:
            json.dump(monthly_stats, f, indent=2)
    except Exception as e:
        logger.error("Failed to save stats: %s", e)


def monthly_report_thread():
    while True:
        try:
            now = datetime.utcnow()
            # send report on 1st of month UTC 00:05 (you can change schedule)
            if now.day == 1 and now.hour == 0 and now.minute == 5:
                msg = generate_monthly_report()
                send_telegram(msg)
                time.sleep(60)  # avoid sending multiple times in same minute
        except Exception as e:
            logger.error("monthly_report_thread error: %s", e)
        time.sleep(30)


def generate_monthly_report():
    total_trades = len(monthly_stats['trades'])
    tp = monthly_stats['tp_count']
    sl = monthly_stats['sl_count']
    pnl = monthly_stats['total_pnl']
    msg = (f"📊 Monthly Summary\n"
           f"Trades: {total_trades}\nTP: {tp}\nSL: {sl}\nNet PnL: {pnl:.2f} USDT\n")
    return msg


# ---------------------------
# STARTUP / RUN
# ---------------------------
def start_bot():
    try:
        setup_exchange()
        # initial scan
        init_market_scan_and_set_trend()
        # load stats
        if os.path.exists(STATS_FILE):
            try:
                with open(STATS_FILE, 'r') as f:
                    loaded = json.load(f)
                    monthly_stats.update(loaded)
            except Exception:
                pass
        # start monthly report thread
        t = threading.Thread(target=monthly_report_thread, daemon=True)
        t.start()
        # start main loop
        main_loop()
    except Exception as e:
        logger.critical("start_bot fatal: %s", e)
        send_telegram(f"⛔ Bot Startup Error: {e}")


if __name__ == '__main__':
    logger.info("Starting Fibo2-pass SMC Bot")
    send_telegram("🤖 Bot starting up - initializing market scan...")
    start_bot()
