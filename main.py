# main.py
# SMC + FVG + Fibo 2-pass + Volume Profile (POC SL) + Strict Zone + M1 CHOCH + MACD confirm
# OKX Futures (ccxt) | Leverage 20 | Open 0.8 of equity (risk-capped)
# Telegram notifications & Monthly summary

import os
import time
import math
import json
import logging
import threading
from datetime import datetime, timedelta
from collections import defaultdict

import ccxt
import requests
import numpy as np
import pandas as pd

# ---------------------------
# CONFIG
# ---------------------------
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSPHRASE')

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID')

SYMBOL = 'BTC-USDT-SWAP'
TIMEFRAME_H1 = '1h'
TIMEFRAME_M5 = '5m'
TIMEFRAME_M1 = '1m'
INIT_OHLCV_LIMIT = 500

LEVERAGE = 20
TARGET_PORTFOLIO_FACTOR = 0.8
TARGET_RISK_PCT = 0.02
ACTUAL_OKX_MARGIN_FACTOR = 0.06824

# --- Options (เปิด/ปิด confirm ได้ทีละตัว) ---
STEP_ALERT = True              # แจ้งทุกขั้นตอน (ช่วงทดสอบ). ปิดแล้วเหลือเฉพาะ Entry/TP/SL/Move SL
USE_M1_CHOCH_CONFIRM = True    # ต้องมี M1 CHOCH ตามทิศ
USE_MACD_CONFIRM = True        # ต้องมี MACD cross ตามทิศ
USE_POC_FILTER = True          # ถ้าราคาปิดแท่ง H1 ทะลุ POC ผิดฝั่ง => ยกเลิก setup

# --- MACD settings (STD) ---
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Fibo settings
FIBO_ENTRY_MIN = 0.33
FIBO_ENTRY_MAX = 0.786
FIBO2_EXT_MIN = 1.33
FIBO2_EXT_MAX = 1.618
FIBO2_SL_LEVEL = 0.786
FIBO80_FALLBACK = 0.80

# Structure settings (LuxAlgo-like swing only)
SWING_LEFT = 3
SWING_RIGHT = 3
SWING_LOOKBACK_H1 = 50
M5_LOOKBACK = 200

# Execution / monitoring
CHECK_INTERVAL = 15  # seconds
COOLDOWN_H1_AFTER_TRADE = 3    # hours
TP1_CLOSE_PERCENT = 0.60
TP2_CLOSE_PERCENT = 0.40

# precision / tolerance
PRICE_TOLERANCE_PCT = 0.0005
POC_BUFFER_PCT = 0.001

STATS_FILE = 'trades_stats.json'

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger('fibo_bot')

# ---------------------------
# GLOBAL STATE
# ---------------------------
exchange = None
market_info = None

current_position = None
pending_trade = None
cooldown_until = None
monthly_stats = {
    'month_year': None,
    'tp_count': 0,
    'sl_count': 0,
    'total_pnl': 0.0,
    'trades': []
}

# Step machine state
smc_state = {
    'step': 1,               # 1: wait H1 SMC, 2: wait Fibo zone (+POC filter), 3: wait M1 CHOCH+MACD
    'bias': None,            # 'up' | 'down'
    'latest_h1_event': None, # dict from SMC engine
    'fibo1': None,
    'entry_zone': None,      # (low, high)
    'poc': None,
    'use_fibo80_fallback': False,
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

def alert_step(text: str):
    if STEP_ALERT:
        send_telegram(text)

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
            'options': {'defaultType': 'swap', 'adjustForTimeDifference': True},
            'timeout': 30000
        })
        exchange.set_sandbox_mode(False)  # set True for sandbox
        exchange.load_markets()
        market_info = exchange.market(SYMBOL)
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
    for _ in range(3):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            logger.warning(f"fetch_ohlcv error {e}, retrying...")
            time.sleep(5)
    raise RuntimeError("Failed to fetch ohlcv after retries")

# ---------------------------
# SMC (Swing Only, LuxAlgo-like)
# ---------------------------
def _pivot_marks(ohlcv, left=3, right=3):
    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]
    L = len(ohlcv)
    swing_high_idx = {}
    swing_low_idx = {}
    for i in range(left, L - right):
        if highs[i] == max(highs[i-left:i+right+1]):
            swing_high_idx[i] = highs[i]
        if lows[i] == min(lows[i-left:i+right+1]):
            swing_low_idx[i] = lows[i]
    return swing_high_idx, swing_low_idx

def compute_smc_events(ohlcv, left=SWING_LEFT, right=SWING_RIGHT):
    if not ohlcv:
        return []
    swing_high_idx, swing_low_idx = _pivot_marks(ohlcv, left, right)

    last_high_level = None; last_high_index = None; crossed_high = True
    last_low_level = None;  last_low_index = None;  crossed_low = True

    bias = None  # 'up'|'down'
    events = []
    for i in range(len(ohlcv)):
        if i in swing_high_idx:
            last_high_level = swing_high_idx[i]; last_high_index = i; crossed_high = False
        if i in swing_low_idx:
            last_low_level = swing_low_idx[i];  last_low_index = i;  crossed_low = False

        close_i = ohlcv[i][4]; ts_i = ohlcv[i][0]
        if (last_high_level is not None) and (not crossed_high) and (close_i > last_high_level):
            signal = 'BOS' if bias in (None, 'up') else 'CHOCH'
            bias = 'up'; crossed_high = True
            events.append({'idx': i,'time': ts_i,'price': last_high_level,'kind': 'high','signal': signal,'bias_after': bias})
        if (last_low_level  is not None) and (not crossed_low)  and (close_i < last_low_level):
            signal = 'BOS' if bias in (None, 'down') else 'CHOCH'
            bias = 'down'; crossed_low = True
            events.append({'idx': i,'time': ts_i,'price': last_low_level,'kind': 'low','signal': signal,'bias_after': bias})
    return events

def latest_smc_state(ohlcv, left=SWING_LEFT, right=SWING_RIGHT):
    evs = compute_smc_events(ohlcv, left, right)
    if not evs:
        return {'latest_event': None, 'bias': None}
    latest = evs[-1]
    return {'latest_event': latest, 'bias': latest['bias_after']}

# ---------------------------
# SWING / FIBO / VP
# ---------------------------
def calc_fibo_levels(low, high):
    diff = high - low
    levels = {
        '0': high,
        '100': low,
        '33': high - 0.33 * diff,
        '38.2': high - 0.382 * diff,
        '50': high - 0.5 * diff,
        '61.8': high - 0.618 * diff,
        '78.6': high - 0.786 * diff,
        'ext133': low + 1.33 * diff,
        'ext161.8': low + 1.618 * diff,
    }
    return levels

def calc_volume_profile_poc(ohlcv_bars, bucket_size=None):
    prices, vols = [], []
    for b in ohlcv_bars:
        prices.append((b[2] + b[3] + b[4]) / 3.0)
        vols.append(b[5] if b[5] is not None else 0.0)
    if not prices:
        return None, []
    min_p, max_p = min(prices), max(prices)
    if bucket_size is None:
        bucket_size = max((max_p - min_p) / 40.0, 0.5)
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
    if total_vol <= 0: return 0.0
    vol_in_zone = sum(v for p, v in buckets if zone_low <= p <= zone_high)
    return vol_in_zone / total_vol

def prepare_fibo1_and_vp():
    ohlcv_h1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_H1, limit=INIT_OHLCV_LIMIT)
    if not ohlcv_h1: return None
    look = min(SWING_LOOKBACK_H1, len(ohlcv_h1))
    recent = ohlcv_h1[-look:]
    highs = [c[2] for c in recent]; lows = [c[3] for c in recent]
    swing_high = max(highs); swing_low = min(lows)
    fibo1 = calc_fibo_levels(swing_low, swing_high)
    entry_zone = (fibo1['33'], fibo1['78.6'])
    poc_price, buckets = calc_volume_profile_poc(recent, bucket_size=None)
    return {'ohlcv_h1': ohlcv_h1,'sw_low': swing_low,'sw_high': swing_high,'fibo1': fibo1,'entry_zone': entry_zone,'poc': poc_price,'vp_buckets': buckets}

# ---------------------------
# MACD & M1 CHOCH Confirm
# ---------------------------
def macd_values(closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    s = pd.Series(closes)
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line.values, signal_line.values, hist.values

def macd_cross_dir(ohlcv_small):
    """Return 'up' if MACD crosses up on last bar, 'down' if crosses down, else None."""
    if not ohlcv_small or len(ohlcv_small) < 3: return None
    closes = [b[4] for b in ohlcv_small]
    macd_line, signal_line, _ = macd_values(closes)
    if len(macd_line) < 2: return None
    prev_diff = macd_line[-2] - signal_line[-2]
    curr_diff = macd_line[-1] - signal_line[-1]
    if prev_diff <= 0 and curr_diff > 0:
        return 'up'
    if prev_diff >= 0 and curr_diff < 0:
        return 'down'
    return None

def m1_choch_in_direction(direction: str):
    """True if latest M1 event is CHOCH toward direction ('up'|'down')."""
    m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=200)
    st = latest_smc_state(m1, left=1, right=1)
    ev = st['latest_event']
    if not ev: return False
    return (ev['signal'].upper() == 'CHOCH') and (ev['bias_after'] == direction)

# ---------------------------
# ENTRY / ORDER SIZE / MARGIN
# ---------------------------
def get_equity():
    try:
        bal = exchange.fetch_balance(params={'type': 'trade'})
        if 'USDT' in bal and 'total' in bal['USDT']:
            return float(bal['USDT']['total'])
        info = bal.get('info', {}).get('data', [])
        for a in info:
            if a.get('ccy') == 'USDT' and a.get('type') == 'TRADE':
                return float(a.get('eq', a.get('availBal', 0.0)))
    except Exception as e:
        logger.error("Balance fetch error: %s", e)
    return 0.0

def compute_contracts_from_portfolio(equity, entry_price):
    use_equity = equity * TARGET_PORTFOLIO_FACTOR
    target_notional = use_equity / ACTUAL_OKX_MARGIN_FACTOR
    contract_size_btc = 0.0001
    base_amount_btc = target_notional / entry_price
    contracts_raw = base_amount_btc / contract_size_btc
    contracts = max(1, int(round(contracts_raw)))
    required_margin = (contracts * contract_size_btc * entry_price) * ACTUAL_OKX_MARGIN_FACTOR
    return contracts, contracts * contract_size_btc * entry_price, required_margin

def cap_size_by_risk(equity, entry_price, proposed_contracts, sl_price):
    if sl_price is None: return proposed_contracts
    contract_size_btc = 0.0001
    dist = abs(entry_price - sl_price)
    if dist <= 0: return proposed_contracts
    risk_per_contract = dist * contract_size_btc
    max_risk_amount = equity * TARGET_RISK_PCT
    max_contracts_by_risk = int(max(1, math.floor(max_risk_amount / risk_per_contract)))
    final_contracts = min(proposed_contracts, max_contracts_by_risk)
    return 0 if final_contracts <= 0 else final_contracts

# ---------------------------
# ORDER / POSITION HANDLERS
# ---------------------------
def open_market_order(direction: str, contracts: int):
    side = 'buy' if direction == 'long' else 'sell'
    params = {'tdMode': 'cross'}
    try:
        amount_to_send = exchange.amount_to_precision(SYMBOL, float(contracts))
        exchange.create_market_order(SYMBOL, side, float(amount_to_send), params=params)
        logger.info(f"Market order placed: {side} {amount_to_send} contracts")
        time.sleep(2)
        pos = get_current_position()
        return pos
    except Exception as e:
        logger.error("open_market_order failed: %s", e)
        send_telegram(f"⛔ Order Error: Failed to open market order: {e}")
        return None

def close_position_by_market(pos):
    if not pos: return False
    side_to_close = 'sell' if pos['side'] == 'long' else 'buy'
    amount = pos['size']; params = {'tdMode': 'cross', 'reduceOnly': True}
    try:
        amount_prec = exchange.amount_to_precision(SYMBOL, float(amount))
        exchange.create_market_order(SYMBOL, side_to_close, float(amount_prec), params=params)
        logger.info(f"Sent market close order: {side_to_close} {amount_prec}")
        return True
    except Exception as e:
        logger.error("close_position_by_market failed: %s", e)
        send_telegram(f"⛔ Emergency Close Failed: {e}")
        return False

def get_current_position():
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for p in positions:
            info = p.get('info', {})
            instId = info.get('instId') or info.get('symbol') or info.get('instrument_id')
            if instId == SYMBOL:
                pos_val = float(info.get('pos', 0))
                if pos_val == 0: return None
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
# INIT / SUMMARY
# ---------------------------
def init_market_scan_and_set_trend():
    ohlcv = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_H1, limit=INIT_OHLCV_LIMIT)
    st = latest_smc_state(ohlcv, left=SWING_LEFT, right=SWING_RIGHT)
    smc_state['latest_h1_event'] = st['latest_event']
    smc_state['bias'] = st['bias']
    # Decide step on start
    if smc_state['bias'] is None:
        smc_state['step'] = 1
        alert_step("🧭 [STEP1] รอ H1 SMC (BOS/CHOCH)")
    else:
        smc_state['step'] = 2  # already have H1 direction → go find Fibo zone
        alert_step(f"🧭 [STEP1→OK] H1 {smc_state['latest_h1_event']['signal']} → Trend = {smc_state['bias'].upper()} (ไป Step2)")
    return st

# ---------------------------
# TRADES LOGGING / MONTHLY STATS
# ---------------------------
def add_trade_record(reason, pos_info, closed_price):
    global monthly_stats
    try:
        entry = pos_info.get('entry_price', 0.0)
        size = pos_info.get('size', 0.0)
        contract_size_btc = 0.0001
        pnl = (closed_price - entry) * size * contract_size_btc if pos_info['side']=='long' else (entry - closed_price) * size * contract_size_btc
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        record = {'time': now,'side': pos_info['side'],'entry': entry,'closed': closed_price,'size': size,'pnl': pnl,'reason': reason}
        monthly_stats['trades'].append(record)
        monthly_stats['total_pnl'] += pnl
        if reason == 'TP': monthly_stats['tp_count'] += 1
        elif reason == 'SL': monthly_stats['sl_count'] += 1
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
            if now.day == 1 and now.hour == 0 and now.minute == 5:
                msg = generate_monthly_report()
                send_telegram(msg)
                time.sleep(60)
        except Exception as e:
            logger.error("monthly_report_thread error: %s", e)
        time.sleep(30)

def generate_monthly_report():
    total_trades = len(monthly_stats['trades'])
    tp = monthly_stats['tp_count']; sl = monthly_stats['sl_count']; pnl = monthly_stats['total_pnl']
    return (f"📊 Monthly Summary\nTrades: {total_trades}\nTP: {tp}\nSL: {sl}\nNet PnL: {pnl:.2f} USDT\n")

# ---------------------------
# STEP MACHINE HELPERS
# ---------------------------
def check_poc_filter(bias: str, poc: float, ohlcv_h1_recent):
    """Return True if setup still valid; False if should cancel by POC close filter."""
    if not USE_POC_FILTER or poc is None or not ohlcv_h1_recent:
        return True
    # last closed H1 bar:
    if len(ohlcv_h1_recent) < 2: return True
    last_closed = ohlcv_h1_recent[-2]  # [ts, o, h, l, c, v]
    c = float(last_closed[4])
    if bias == 'up' and c < poc * (1 - PRICE_TOLERANCE_PCT):
        alert_step("❌ [POC CANCEL] H1 Close < POC → ยกเลิก Long Setup (กลับไป STEP1)")
        return False
    if bias == 'down' and c > poc * (1 + PRICE_TOLERANCE_PCT):
        alert_step("❌ [POC CANCEL] H1 Close > POC → ยกเลิก Short Setup (กลับไป STEP1)")
        return False
    return True

def strict_in_zone(price, zone):
    lo, hi = min(zone), max(zone)
    return (price >= lo * (1 - PRICE_TOLERANCE_PCT)) and (price <= hi * (1 + PRICE_TOLERANCE_PCT))

# ---------------------------
# MAIN LOOP
# ---------------------------
def main_loop():
    global current_position, pending_trade, cooldown_until, smc_state
    logger.info("Starting main loop...")
    while True:
        try:
            now = datetime.utcnow()
            if cooldown_until and now < cooldown_until:
                logger.info(f"In cooldown until {cooldown_until}, sleeping...")
                time.sleep(CHECK_INTERVAL); continue

            # Position refresh & price
            ticker = exchange.fetch_ticker(SYMBOL)
            current_price = float(ticker['last'])
            pos = get_current_position()
            current_position = pos

            # H1 SMC state
            ohlcv_h1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_H1, limit=180)
            st = latest_smc_state(ohlcv_h1, left=SWING_LEFT, right=SWING_RIGHT)
            latest = st['latest_event']; bias = st['bias']  # may be None

            # If holding & CHOCH opposite → close and reset to step1
            if current_position and latest and latest['signal'].upper() == 'CHOCH':
                if (current_position['side'] == 'long' and latest['bias_after'] == 'down') or (current_position['side'] == 'short' and latest['bias_after'] == 'up'):
                    send_telegram(f"⚠ CHOCH opposite → closing {current_position['side']} @ {current_price:.2f} and RESET")
                    close_position_by_market(current_position)
                    add_trade_record('CHoCH Close', current_position, current_price)
                    current_position = None; pending_trade = None
                    cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                    smc_state.update({'step': 1,'bias': None,'latest_h1_event': None,'fibo1': None,'entry_zone': None,'poc': None,'use_fibo80_fallback': False})
                    alert_step("🔁 [RESET] กลับไป STEP1 (รอ H1 BOS/CHOCH ใหม่)")
                    time.sleep(CHECK_INTERVAL); continue

            # If no position → run step machine
            if not current_position:
                # STEP 1: wait H1 BOS/CHOCH
                if smc_state['step'] == 1:
                    if bias is None:
                        alert_step("🧭 [STEP1] รอ H1 SMC (BOS/CHOCH)")
                        time.sleep(CHECK_INTERVAL); continue
                    smc_state['bias'] = bias
                    smc_state['latest_h1_event'] = latest
                    smc_state['step'] = 2
                    alert_step(f"🧭 [STEP1→OK] H1 {latest['signal']} → Trend = {bias.upper()} (ไป STEP2)")

                # STEP 2: prepare fibo1 & VP; wait price in zone + POC filter
                if smc_state['step'] == 2:
                    prep = prepare_fibo1_and_vp()
                    if not prep:
                        logger.info("prepare_fibo1_and_vp failed; waiting...")
                        time.sleep(CHECK_INTERVAL); continue

                    entry_zone = prep['entry_zone']; poc = prep['poc']; fibo1 = prep['fibo1']
                    smc_state['entry_zone'] = entry_zone; smc_state['poc'] = poc; smc_state['fibo1'] = fibo1

                    # POC filter (cancel if last closed H1 invalidates)
                    if not check_poc_filter(smc_state['bias'], poc, prep['ohlcv_h1']):
                        # reset to step1
                        smc_state.update({'step': 1,'bias': None,'latest_h1_event': None,'fibo1': None,'entry_zone': None,'poc': None,'use_fibo80_fallback': False})
                        time.sleep(CHECK_INTERVAL); continue

                    # wait price inside zone
                    if strict_in_zone(current_price, entry_zone):
                        alert_step("📏 [STEP2] ราคาเข้าโซน Fibo 33–78.6 (H1) + POC Check Passed → ไป STEP3")
                        smc_state['step'] = 3
                    else:
                        alert_step("⌛ [STEP2] รอราคาเข้าโซน Fibo (H1)")
                        time.sleep(CHECK_INTERVAL); continue

                # STEP 3: wait M1 CHOCH & MACD cross (strict zone check)
                if smc_state['step'] == 3:
                    direction = 'up' if smc_state['bias'] == 'up' else 'down'
                    # Still in zone?
                    if not strict_in_zone(current_price, smc_state['entry_zone']):
                        alert_step("⏸️ [STEP3] ราคาออกนอกโซน Fibo → รอกลับเข้าโซนก่อน")
                        time.sleep(CHECK_INTERVAL); continue

                    choch_ok = True
                    macd_ok = True

                    if USE_M1_CHOCH_CONFIRM:
                        choch_ok = m1_choch_in_direction(direction)
                        if choch_ok:
                            alert_step(f"✅ [STEP3] M1 CHOCH {direction.upper()} แล้ว")
                        else:
                            alert_step(f"⌛ [STEP3] รอ M1 CHOCH {direction.upper()}")

                    if USE_MACD_CONFIRM:
                        m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=200)
                        macd_dir = macd_cross_dir(m1)
                        macd_ok = (macd_dir == direction)
                        if macd_ok:
                            alert_step(f"✅ [STEP3] MACD Cross {direction.upper()} แล้ว")
                        else:
                            alert_step(f"⌛ [STEP3] รอ MACD Cross {direction.upper()}")

                    if (choch_ok and macd_ok):
                        # Build SL from POC (SL1)
                        poc = smc_state['poc']
                        fibo1 = smc_state['fibo1']
                        trend = smc_state['bias']
                        sl_price = None
                        if poc:
                            sl_price = poc * (1 - POC_BUFFER_PCT) if trend == 'up' else poc * (1 + POC_BUFFER_PCT)
                        else:
                            sl_price = fibo1['100'] if trend == 'up' else fibo1['0']

                        equity = get_equity()
                        proposed_contracts, _, _ = compute_contracts_from_portfolio(equity, current_price)
                        final_contracts = cap_size_by_risk(equity, current_price, proposed_contracts, sl_price)
                        if final_contracts <= 0:
                            send_telegram("⚠ Position size after risk cap is 0. Skip entry.")
                            smc_state['step'] = 1
                            time.sleep(CHECK_INTERVAL); continue

                        pos = open_market_order('long' if trend == 'up' else 'short', final_contracts)
                        if not pos:
                            logger.warning("Order open failed.")
                            time.sleep(CHECK_INTERVAL); continue

                        tp1_price = fibo1['0'] if trend == 'up' else fibo1['100']
                        pending_trade = {
                            'side': pos['side'],
                            'entry_price': pos['entry_price'],
                            'size': pos['size'],
                            'fibo1': fibo1,
                            'poc': poc,
                            'sl_price': sl_price,
                            'tp1_price': tp1_price,
                            'state': 'OPEN',
                            'opened_at': datetime.utcnow().isoformat(),
                            'trend': trend,
                            'contracts': final_contracts
                        }
                        current_position = pos
                        send_telegram(f"📈 [ENTRY] {pending_trade['side'].upper()} @ {pending_trade['entry_price']:.2f} | SL(POC): {pending_trade['sl_price']:.2f} | TP1: {pending_trade['tp1_price']:.2f} | Qty: {final_contracts}")
                        # after entry, machine will switch to position management below
                        smc_state['step'] = 99  # in position sentinel
                        time.sleep(CHECK_INTERVAL); continue
                    else:
                        time.sleep(CHECK_INTERVAL); continue

            # --- Manage open position (TP1 -> SL BE -> TP2 + POC SL) ---
            if current_position:
                ticker = exchange.fetch_ticker(SYMBOL)
                current_price = float(ticker['last'])
                pos = current_position
                if pending_trade is None:
                    pending_trade = {'side': pos['side'],'entry_price': pos['entry_price'],'size': pos['size'],'opened_at': datetime.utcnow().isoformat(),'state': 'OPEN','contracts': pos['size'],'trend': pos['side']=='long' and 'up' or 'down'}

                # TP1
                tp1_hit = False
                if pos['side'] == 'long':
                    if current_price >= pending_trade.get('tp1_price', float('inf')) * (1 - PRICE_TOLERANCE_PCT):
                        tp1_hit = True
                else:
                    if current_price <= pending_trade.get('tp1_price', 0) * (1 + PRICE_TOLERANCE_PCT):
                        tp1_hit = True

                if tp1_hit and pending_trade.get('state') == 'OPEN':
                    close_amount = max(1, int(round(pending_trade['contracts'] * TP1_CLOSE_PERCENT)))
                    side_to_close = 'sell' if pos['side'] == 'long' else 'buy'
                    try:
                        amount_prec = exchange.amount_to_precision(SYMBOL, float(close_amount))
                        exchange.create_market_order(SYMBOL, side_to_close, float(amount_prec), params={'tdMode': 'cross', 'reduceOnly': True})
                        send_telegram(f"✅ [TP1] closed {TP1_CLOSE_PERCENT*100:.0f}% @ {current_price:.2f}")
                        pending_trade['state'] = 'TP1_HIT'
                        # fibo2
                        ohlcv_small = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M5, limit=M5_LOOKBACK)
                        highs = [b[2] for b in ohlcv_small] if ohlcv_small else []
                        base = pending_trade['tp1_price']
                        max_high = max(highs) if highs else base * 1.05
                        if max_high <= base: max_high = base * 1.03
                        diff = max_high - base
                        fibo2 = {'100': base,'78.6': base + FIBO2_SL_LEVEL * diff,'ext133': base + FIBO2_EXT_MIN * diff,'ext161.8': base + FIBO2_EXT_MAX * diff}
                        pending_trade['fibo2'] = fibo2
                        pending_trade['sl_price_step2'] = fibo2['78.6']
                        send_telegram(f"🔁 [MOVE SL] → Fibo2 78.6 = {fibo2['78.6']:.2f} | TP2 zone {fibo2['ext133']:.2f}-{fibo2['ext161.8']:.2f}")
                    except Exception as e:
                        logger.error("Partial close TP1 failed: %s", e)
                        send_telegram(f"⚠ Partial close at TP1 failed: {e}")

                # TP2 / Emergency / SL monitoring
                if pending_trade.get('state') == 'TP1_HIT' and 'fibo2' in pending_trade:
                    fibo2 = pending_trade['fibo2']
                    tp2_lo, tp2_hi = fibo2['ext133'], fibo2['ext161.8']
                    in_tp2_zone = (current_price >= tp2_lo * (1 - PRICE_TOLERANCE_PCT)) and (current_price <= tp2_hi * (1 + PRICE_TOLERANCE_PCT))
                    # emergency: quick opposite m1 momentum
                    m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=10)
                    emergency = False
                    if len(m1) >= 3:
                        last, prev, prev2 = m1[-1][4], m1[-2][4], m1[-3][4]
                        if pending_trade['side']=='long' and last<prev and prev<prev2: emergency = True
                        if pending_trade['side']=='short' and last>prev and prev>prev2: emergency = True

                    if emergency:
                        send_telegram(f"⚠ [EMERGENCY] close remaining @ {current_price:.2f}")
                        close_position_by_market(pos)
                        pending_trade['state'] = 'CLOSED_EMERGENCY'
                        add_trade_record('Emergency Close', pos, current_price)
                        cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                        current_position = None; pending_trade = None
                        smc_state.update({'step': 1,'bias': None,'latest_h1_event': None,'fibo1': None,'entry_zone': None,'poc': None,'use_fibo80_fallback': False})
                        alert_step("🔁 [RESET] กลับ STEP1")
                        time.sleep(5); continue

                    if in_tp2_zone:
                        send_telegram(f"🏁 [TP2] close remaining @ {current_price:.2f}")
                        close_position_by_market(pos)
                        pending_trade['state'] = 'TP2_HIT'
                        add_trade_record('TP', pos, current_price)
                        cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                        current_position = None; pending_trade = None
                        smc_state.update({'step': 1,'bias': None,'latest_h1_event': None,'fibo1': None,'entry_zone': None,'poc': None,'use_fibo80_fallback': False})
                        alert_step("🔁 [RESET] กลับ STEP1")
                        time.sleep(5); continue

                    # SL step2
                    sl2 = pending_trade.get('sl_price_step2')
                    if sl2:
                        if pending_trade['side']=='long' and current_price <= sl2 * (1 + PRICE_TOLERANCE_PCT):
                            send_telegram(f"🛑 [SL2] close remaining @ {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL_STEP2'
                            add_trade_record('SL', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None; pending_trade = None
                            smc_state.update({'step': 1,'bias': None,'latest_h1_event': None,'fibo1': None,'entry_zone': None,'poc': None,'use_fibo80_fallback': False})
                            alert_step("🔁 [RESET] กลับ STEP1")
                            time.sleep(5); continue
                        if pending_trade['side']=='short' and current_price >= sl2 * (1 - PRICE_TOLERANCE_PCT):
                            send_telegram(f"🛑 [SL2] close remaining @ {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL_STEP2'
                            add_trade_record('SL', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None; pending_trade = None
                            smc_state.update({'step': 1,'bias': None,'latest_h1_event': None,'fibo1': None,'entry_zone': None,'poc': None,'use_fibo80_fallback': False})
                            alert_step("🔁 [RESET] กลับ STEP1")
                            time.sleep(5); continue

                # Initial SL (POC) while OPEN
                if pending_trade and pending_trade.get('state') == 'OPEN':
                    slp = pending_trade.get('sl_price')
                    if slp:
                        if pending_trade['side']=='long' and current_price <= slp * (1 + PRICE_TOLERANCE_PCT):
                            send_telegram(f"❌ [SL] close @ {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL'
                            add_trade_record('SL', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None; pending_trade = None
                            smc_state.update({'step': 1,'bias': None,'latest_h1_event': None,'fibo1': None,'entry_zone': None,'poc': None,'use_fibo80_fallback': False})
                            alert_step("🔁 [RESET] กลับ STEP1")
                            time.sleep(5); continue
                        if pending_trade['side']=='short' and current_price >= slp * (1 - PRICE_TOLERANCE_PCT):
                            send_telegram(f"❌ [SL] close @ {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL'
                            add_trade_record('SL', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None; pending_trade = None
                            smc_state.update({'step': 1,'bias': None,'latest_h1_event': None,'fibo1': None,'entry_zone': None,'poc': None,'use_fibo80_fallback': False})
                            alert_step("🔁 [RESET] กลับ STEP1")
                            time.sleep(5); continue

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            logger.exception("Main loop error: %s", e)
            send_telegram(f"⛔ Bot Error: {e}")
            time.sleep(10)

# ---------------------------
# STARTUP / RUN
# ---------------------------
def start_bot():
    try:
        setup_exchange()
        init_market_scan_and_set_trend()
        if os.path.exists(STATS_FILE):
            try:
                with open(STATS_FILE, 'r') as f:
                    loaded = json.load(f); monthly_stats.update(loaded)
            except Exception:
                pass
        t = threading.Thread(target=monthly_report_thread, daemon=True)
        t.start()
        main_loop()
    except Exception as e:
        logger.critical("start_bot fatal: %s", e)
        send_telegram(f"⛔ Bot Startup Error: {e}")

if __name__ == '__main__':
    logger.info("Starting SMC Fibo Bot (Swing SMC + Strict Zone + M1 CHOCH + MACD STD)")
    send_telegram("🤖 Bot starting: Swing SMC + Strict Zone + M1 CHOCH + MACD(12,26,9) + POC filter")
    start_bot()
