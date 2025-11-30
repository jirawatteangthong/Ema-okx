# main.py
# SMC + Fibo + M1 POC (pullback) + SL Hierarchy + Strict Zone + One-shot Alerts (C2)
# Entry: (M1 CHOCH or MACD(12,26,9) cross) on closed candle, same trend
# OKX Futures (ccxt) | isolated | leverage 20 | risk-capped for small account (31 USDT)
# Telegram & Monthly Summary / PAPER MODE

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

# =========================
# PAPER / DRY TRADING MODE
# =========================
# True  = โหมดทดลอง (ไม่ส่งคำสั่งไป OKX จริง แต่ใช้ราคาจริง)
# False = โหมดเงินจริง
PAPER_TRADING = True
PAPER_START_BALANCE = 50.0  # ทุนสมมติสำหรับ paper mode (USDT)

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
OKX_MARGIN_MODE = 'isolated'  # <- ตามที่ยืนยัน

# ขนาดพอร์ตเล็ก (31 USDT): ปรับให้รอดง่าย
TARGET_PORTFOLIO_FACTOR = 0.25    # ใช้ 25% ของ equity
TARGET_RISK_PCT = 0.005           # เสี่ยง 0.5% ต่อไม้
ACTUAL_OKX_MARGIN_FACTOR = 0.07   # ปัจจุบัน

# Alerts / Toggles True=เปิด False=ปิด
STEP_ALERT = True  # แจ้งเตือน
USE_M1_CHOCH_CONFIRM = True
USE_MACD_CONFIRM = True
USE_POC_FILTER = True  # H1 close vs POC(M1) cancel (ตามทิศ)

# MACD STD
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Fibo
FIBO2_EXT_MIN = 1.33
FIBO2_EXT_MAX = 1.618
FIBO2_SL_LEVEL = 0.786

# Structure (swing)
SWING_LEFT = 3
SWING_RIGHT = 3
SWING_LOOKBACK_H1 = 50
M5_LOOKBACK = 200

# Execution
CHECK_INTERVAL = 30
COOLDOWN_H1_AFTER_TRADE = 3  # hours
TP1_CLOSE_PERCENT = 0.60
TP2_CLOSE_PERCENT = 0.40

# Precision
PRICE_TOLERANCE_PCT = 0.0005
POC_BUFFER_PCT = 0.001  # สำหรับ H1 cancel
POC_ENTRY_BUFFER_PCT = 0.001  # ไม่ใช้แล้ว
POC_SL_BUFFER_PCT = 0.001     # ไม่ใช้แล้ว
POC_SL_HARD_BUFFER_PCT = 0.001  # ไม่ใช้แล้ว

# ตั้งจริง (0.10%)
POC_SL_BUFFER = 0.001  # 0.1%

STATS_FILE = 'trades_stats.json'

# ---------------------------
# LOGGING
# ---------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger('smc_m1poc_bot')

# ---------------------------
# GLOBAL STATE
# ---------------------------
exchange = None
market_info = None

current_position = None       # ใช้ทั้ง paper และ real
pending_trade = None
cooldown_until = None

monthly_stats = {
    'month_year': None,
    'tp_count': 0,
    'sl_count': 0,
    'be_count': 0,       # กันทุน (Break-even)
    'total_pnl': 0.0,
    'trades': []
}

# wallet จำลองสำหรับ PAPER MODE
paper_wallet = {
    'balance': PAPER_START_BALANCE,
    'equity': PAPER_START_BALANCE,
}

# STEP Machine
smc_state = {
    'step': 1,               # 1:H1 SMC, 2:Fibo+POC rule, 3:M1 Confirm, 99:in-position
    'bias': None,            # 'up'|'down'
    'latest_h1_event': None,
    'fibo1': None,
    'entry_zone': None,      # (low, high)
    'poc_m1': None,          # POC จาก M1 pullback
}

# One-shot alerts
last_notices = set()

# ---------------------------
# TELEGRAM
# ---------------------------
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or TELEGRAM_TOKEN.startswith('YOUR_'):
        logger.warning("Telegram token/chat_id not configured - skip send.")
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        requests.get(url, params=params, timeout=10)
    except Exception as e:
        logger.error("Telegram send error: %s", e)

def alert_once(key: str, message: str):
    if STEP_ALERT and key not in last_notices:
        last_notices.add(key)
        send_telegram(message)

def reset_alerts(prefix: str | None = None):
    global last_notices
    if prefix is None:
        last_notices.clear()
    else:
        last_notices = {k for k in last_notices if not k.startswith(prefix)}

# ---------------------------
# EXCHANGE
# ---------------------------
def setup_exchange():
    global exchange, market_info
    exchange = ccxt.okx({
        'apiKey': API_KEY,
        'secret': SECRET,
        'password': PASSWORD,
        'enableRateLimit': True,
        'options': {'defaultType': 'swap', 'adjustForTimeDifference': True},
        'timeout': 30000
    })
    exchange.set_sandbox_mode(False)  # ต้องการราคาจริง แม้จะใช้ paper
    exchange.load_markets()
    market_info = exchange.market(SYMBOL)
    try:
        if not PAPER_TRADING:
            exchange.set_leverage(LEVERAGE, SYMBOL, params={'mgnMode': OKX_MARGIN_MODE})
            logger.info(f"Set leverage {LEVERAGE}x {OKX_MARGIN_MODE} for {SYMBOL}")
        else:
            logger.info("PAPER MODE: skip setting leverage on real account.")
    except Exception as e:
        logger.warning("Set leverage failed: %s", e)

# ---------------------------
# DATA
# ---------------------------
def fetch_ohlcv_safe(symbol, timeframe, limit=200):
    for _ in range(3):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            logger.warning(f"fetch_ohlcv error: {e}; retry...")
            time.sleep(5)
    raise RuntimeError("fetch_ohlcv failed")

# ---------------------------
# SMC (Swing Only)
# ---------------------------
def _pivot_marks(ohlcv, left=3, right=3):
    highs = [c[2] for c in ohlcv]; lows = [c[3] for c in ohlcv]
    L = len(ohlcv); sh, sl = {}, {}
    for i in range(left, L - right):
        if highs[i] == max(highs[i-left:i+right+1]): sh[i] = highs[i]
        if lows[i]  == min(lows[i-left:i+right+1]) : sl[i] = lows[i]
    return sh, sl

def compute_smc_events(ohlcv, left=SWING_LEFT, right=SWING_RIGHT):
    if not ohlcv: return []
    sh, sl = _pivot_marks(ohlcv, left, right)
    last_high = None; crossed_high = True
    last_low  = None; crossed_low  = True
    bias = None
    evs = []
    for i in range(len(ohlcv)):
        if i in sh: last_high = sh[i]; crossed_high = False
        if i in sl: last_low  = sl[i]; crossed_low  = False
        c = ohlcv[i][4]; t = ohlcv[i][0]
        if (last_high is not None) and (not crossed_high) and (c > last_high):
            sig = 'BOS' if bias in (None, 'up') else 'CHOCH'
            bias = 'up'; crossed_high = True
            evs.append({'idx': i,'time': t,'price': last_high,'kind': 'high','signal': sig,'bias_after': bias})
        if (last_low  is not None) and (not crossed_low)  and (c < last_low):
            sig = 'BOS' if bias in (None, 'down') else 'CHOCH'
            bias = 'down'; crossed_low = True
            evs.append({'idx': i,'time': t,'price': last_low,'kind': 'low','signal': sig,'bias_after': bias})
    return evs

def latest_smc_state(ohlcv, left=SWING_LEFT, right=SWING_RIGHT):
    evs = compute_smc_events(ohlcv, left, right)
    if not evs: return {'latest_event': None, 'bias': None}
    return {'latest_event': evs[-1], 'bias': evs[-1]['bias_after']}

# ---------------------------
# FIBO / VP / POC
# ---------------------------
def calc_fibo_levels_from_low_high(low, high):
    diff = high - low
    return {
        '0': high, '100': low,
        '33': high - 0.33*diff,
        '38.2': high - 0.382*diff,
        '50': high - 0.5*diff,
        '61.8': high - 0.618*diff,
        '71.8': high - 0.718*diff,   # เพิ่มตาม requirement
        '78.6': high - 0.786*diff,
        '80':  high - 0.80*diff,
        'ext133': low + 1.33*diff,
        'ext161.8': low + 1.618*diff,
    }

def prepare_fibo1(ohlcv_h1):
    look = min(SWING_LOOKBACK_H1, len(ohlcv_h1))
    recent = ohlcv_h1[-look:]
    swing_high = max(b[2] for b in recent)
    swing_low  = min(b[3] for b in recent)
    fibo1 = calc_fibo_levels_from_low_high(swing_low, swing_high)
    entry_zone = (fibo1['33'], fibo1['78.6'])
    return fibo1, entry_zone

def macd_values(closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    s = pd.Series(closes)
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line.values, signal_line.values, hist.values

def macd_cross_dir_closed(ohlcv_small):
    if not ohlcv_small or len(ohlcv_small) < 3: return None
    data = ohlcv_small[:-1]  # ตัดแท่งกำลังก่อตัว
    closes = [b[4] for b in data]
    macd_line, signal_line, _ = macd_values(closes)
    if len(macd_line) < 2: return None
    prev_diff = macd_line[-2] - signal_line[-2]
    curr_diff = macd_line[-1] - signal_line[-1]
    if prev_diff <= 0 and curr_diff > 0: return 'up'
    if prev_diff >= 0 and curr_diff < 0: return 'down'
    return None

def m1_choch_in_direction_closed(direction: str):
    m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=300)
    if not m1 or len(m1) < 5: return False
    evs = compute_smc_events(m1[:-1], left=1, right=1)
    if not evs: return False
    ev = evs[-1]
    return (ev['signal'].upper() == 'CHOCH') and (ev['bias_after'] == direction)

def strict_in_zone(price, zone):
    lo, hi = min(zone), max(zone)
    return (price >= lo * (1 - PRICE_TOLERANCE_PCT)) and (price <= hi * (1 + PRICE_TOLERANCE_PCT))

# ---- M1 POC (เฉพาะช่วงย่อ) ----
def calc_volume_profile_poc_from_bars(bars, bucket_size=None):
    prices, vols = [], []
    for b in bars:
        prices.append((b[2] + b[3] + b[4]) / 3.0)
        vols.append(b[5] if b[5] is not None else 0.0)
    if not prices: return None
    min_p, max_p = min(prices), max(prices)
    if bucket_size is None:
        bucket_size = max((max_p - min_p) / 40.0, 0.5)
    bins = defaultdict(float)
    for p, v in zip(prices, vols):
        idx = int((p - min_p) / bucket_size)
        center = min_p + (idx + 0.5)*bucket_size
        bins[center] += v
    if not bins: return None
    poc_price = max(bins.items(), key=lambda x: x[1])[0]
    return poc_price

def m1_pullback_subset_for_poc(m1, fibo1, bias):
    """
    เลือกเฉพาะแท่ง M1 ในช่วงย่อเข้าหาโซน (ตาม bias) เพื่อหา POC
    - สำหรับ uptrend: เลือกแท่งที่ Close อยู่ระหว่าง Fibo 61.8 ถึง 100
    - สำหรับ downtrend: สลับด้าน
    """
    if not m1: return []
    if bias == 'up':
        selected = [b for b in m1 if fibo1['100'] <= b[4] <= fibo1['0']]
        selected = [b for b in selected if fibo1['100'] <= b[4] <= fibo1['61.8']]
    else:
        selected = [b for b in m1 if fibo1['100'] <= b[4] <= fibo1['0']]
        selected = [b for b in selected if fibo1['38.2'] <= b[4] <= fibo1['0']]
    return selected

def compute_m1_poc_for_pullback(fibo1, bias):
    m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=300)
    subset = m1_pullback_subset_for_poc(m1, fibo1, bias)
    if not subset: return None
    poc = calc_volume_profile_poc_from_bars(subset, bucket_size=None)
    return poc

def price_in_range(p, a, b):
    return min(a,b) <= p <= max(a,b)

# ---------------------------
# SIZE / RISK
# ---------------------------
def get_equity():
    # ในโหมด PAPER ใช้ balance จำลอง
    if PAPER_TRADING:
        return float(paper_wallet['equity'])
    try:
        bal = exchange.fetch_balance(params={'type': 'trade'})
        if 'USDT' in bal and 'total' in bal['USDT']:
            return float(bal['USDT']['total'])
        info = bal.get('info', {}).get('data', [])
        for a in info:
            if a.get('ccy') == 'USDT' and a.get('type') == 'TRADE':
                return float(a.get('eq', a.get('availBal', 0.0)))
    except Exception as e:
        logger.error("Balance error: %s", e)
    return 0.0

def compute_contracts_from_portfolio(equity, entry_price):
    use_equity = equity * TARGET_PORTFOLIO_FACTOR
    target_notional = use_equity / ACTUAL_OKX_MARGIN_FACTOR
    contract_size_btc = 0.0001
    base_amount_btc = target_notional / entry_price
    contracts = max(1, int(round(base_amount_btc / contract_size_btc)))
    required_margin = (contracts * contract_size_btc * entry_price) * ACTUAL_OKX_MARGIN_FACTOR
    return contracts, contracts * contract_size_btc * entry_price, required_margin

def cap_size_by_risk(equity, entry_price, proposed_contracts, sl_price):
    if sl_price is None: return proposed_contracts
    contract_size_btc = 0.0001
    dist = abs(entry_price - sl_price)
    if dist <= 0: return proposed_contracts
    risk_per_contract = dist * contract_size_btc
    max_risk = equity * TARGET_RISK_PCT
    max_contracts = int(max(1, math.floor(max_risk / risk_per_contract)))
    return min(proposed_contracts, max_contracts) if max_contracts > 0 else 0

# ---------------------------
# ORDERS
# ---------------------------
def open_market_order(direction: str, contracts: int):
    """
    ถ้า PAPER_TRADING = True -> จำลองการเปิดออเดอร์ (ไม่ยิงไป OKX)
    ถ้า False -> ส่งคำสั่งจริง
    """
    side = 'long' if direction == 'long' else 'short'

    if PAPER_TRADING:
        try:
            ticker = exchange.fetch_ticker(SYMBOL)
            entry_price = float(ticker['last'])
        except Exception as e:
            logger.error("[PAPER] fetch_ticker failed on open: %s", e)
            return None
        pos = {
            'side': side,
            'size': float(contracts),
            'entry_price': entry_price,
            'unrealized_pnl': 0.0
        }
        logger.info(f"[PAPER] OPEN {side.upper()} {contracts} @ {entry_price:.2f}")
        return pos

    # -------- REAL ORDER --------
    real_side = 'buy' if direction == 'long' else 'sell'
    params = {'tdMode': OKX_MARGIN_MODE}
    try:
        amount_to_send = exchange.amount_to_precision(SYMBOL, float(contracts))
        exchange.create_market_order(SYMBOL, real_side, float(amount_to_send), params=params)
        time.sleep(2)
        return get_current_position()
    except Exception as e:
        logger.error("open_market_order failed: %s", e)
        send_telegram(f"⛔ เปิดออเดอร์ล้มเหลว: {e}")
        return None

def close_position_by_market(pos):
    """
    PAPER: คำนวณ PnL และอัปเดต paper_wallet
    REAL : ส่ง reduceOnly order
    """
    if not pos:
        return False

    if PAPER_TRADING:
        try:
            ticker = exchange.fetch_ticker(SYMBOL)
            exit_price = float(ticker['last'])
        except Exception as e:
            logger.error("[PAPER] fetch_ticker failed on close: %s", e)
            return False

        cs = 0.0001
        side = pos['side']
        size = float(pos['size'])
        entry = float(pos['entry_price'])
        pnl = (exit_price - entry) * size * cs if side == 'long' else (entry - exit_price) * size * cs

        paper_wallet['balance'] += pnl
        paper_wallet['equity'] = paper_wallet['balance']

        logger.info(f"[PAPER] CLOSE {side.upper()} @ {exit_price:.2f} | PnL={pnl:.4f} | BAL={paper_wallet['balance']:.2f}")
        return True

    # -------- REAL CLOSE --------
    side_to_close = 'sell' if pos['side'] == 'long' else 'buy'
    params = {'tdMode': OKX_MARGIN_MODE, 'reduceOnly': True}
    try:
        amount_prec = exchange.amount_to_precision(SYMBOL, float(pos['size']))
        exchange.create_market_order(SYMBOL, side_to_close, float(amount_prec), params=params)
        return True
    except Exception as e:
        logger.error("close_position_by_market failed: %s", e)
        send_telegram(f"⛔ ปิดออเดอร์ล้มเหลว: {e}")
        return False

def get_current_position():
    """
    PAPER: ใช้ current_position ในหน่วยความจำ
    REAL : ดึงจาก OKX
    """
    global current_position
    if PAPER_TRADING:
        return current_position

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
# INIT
# ---------------------------
def init_market_scan_and_set_trend():
    ohlcv = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_H1, limit=INIT_OHLCV_LIMIT)
    st = latest_smc_state(ohlcv, left=SWING_LEFT, right=SWING_RIGHT)
    smc_state['latest_h1_event'] = st['latest_event']
    smc_state['bias'] = st['bias']
    if smc_state['bias'] is None:
        smc_state['step'] = 1
        reset_alerts(); alert_once("STEP1_WAIT", "🧭 [STEP1] รอ H1 SMC (BOS/CHOCH)")
    else:
        smc_state['step'] = 2
        reset_alerts(); alert_once("STEP1_OK", f"🧭 [STEP1→OK] H1 {smc_state['latest_h1_event']['signal']} → เทรนด์ = {smc_state['bias'].upper()} (ไป STEP2)")
    return st

# ---------------------------
# STATS
# ---------------------------
def add_trade_record(reason, pos_info, closed_price):
    global monthly_stats
    try:
        entry = float(pos_info.get('entry_price', 0.0))
        size = float(pos_info.get('size', 0.0))
        cs = 0.0001
        side = pos_info['side']
        pnl = (closed_price - entry) * size * cs if side == 'long' else (entry - closed_price) * size * cs

        record = {
            'time': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'side': side,
            'entry': entry,
            'closed': closed_price,
            'size': size,
            'pnl': pnl,
            'reason': reason
        }
        monthly_stats['trades'].append(record)
        monthly_stats['total_pnl'] += pnl

        # กันทุน: ถ้า pnl ใกล้ 0 มาก ๆ ให้ถือว่า BE
        if abs(pnl) < 0.1:
            monthly_stats['be_count'] += 1
        else:
            r = reason.upper()
            if r.startswith('TP'):
                monthly_stats['tp_count'] += 1
            elif 'SL' in r:
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
        logger.error("save stats error: %s", e)

def monthly_report_thread():
    """
    ส่งสรุปรายเดือนทุกวันที่ 20 เวลา 00:05 (UTC)
    """
    sent_for_month = None
    while True:
        try:
            now = datetime.utcnow()
            key = now.strftime('%Y-%m')
            if now.day == 20 and now.hour == 0 and now.minute == 5:
                if sent_for_month != key:
                    msg = generate_monthly_report()
                    send_telegram(msg)
                    sent_for_month = key
                    time.sleep(60)
        except Exception as e:
            logger.error("monthly_report_thread error: %s", e)
        time.sleep(30)

def generate_monthly_report():
    t = len(monthly_stats['trades'])
    tp = monthly_stats['tp_count']
    sl = monthly_stats['sl_count']
    be = monthly_stats['be_count']
    pnl = monthly_stats['total_pnl']
    return (
        f"📊 สรุปรายเดือน (Paper={PAPER_TRADING})\n"
        f"จำนวนเทรด: {t}\n"
        f"TP: {tp}\n"
        f"SL: {sl}\n"
        f"กันทุน (BE): {be}\n"
        f"PnL สุทธิ: {pnl:.2f} USDT\n"
        f"Balance ปัจจุบัน (paper): {paper_wallet['balance']:.2f} USDT" if PAPER_TRADING else ""
    )

# ---------------------------
# HELPERS (POC FILTER & SL RULE)
# ---------------------------
def check_poc_filter_h1_close_vs_poc(bias: str, poc_price: float, ohlcv_h1_recent):
    """
    ใช้ POC จาก M1 (pullback) เป็น level
    ถ้าแท่ง H1 ล่าสุดปิด 'ผิดฝั่ง' POC ⇒ ยกเลิก setup
    """
    if not USE_POC_FILTER or poc_price is None or not ohlcv_h1_recent or len(ohlcv_h1_recent) < 2:
        return True
    last_closed = ohlcv_h1_recent[-2]
    ts = int(last_closed[0]); c = float(last_closed[4])
    if bias == 'up' and c < poc_price * (1 - PRICE_TOLERANCE_PCT):
        alert_once(f"POC_CANCEL_{ts}", "❌ [POC] H1 ปิดต่ำกว่า M1 POC → ยกเลิก Long Setup (กลับ STEP1)")
        return False
    if bias == 'down' and c > poc_price * (1 + PRICE_TOLERANCE_PCT):
        alert_once(f"POC_CANCEL_{ts}", "❌ [POC] H1 ปิดสูงกว่า M1 POC → ยกเลิก Short Setup (กลับ STEP1)")
        return False
    return True

def derive_sl_from_rules(fibo1, bias, poc_m1, price_now, m1=None):
    """
    ตามลำดับ:
    1) ถ้า POC อยู่ใน 80–100 ⇒ SL = ใต้ POC 0.10%
    2) ถ้าแตะ 80–100 และมีสวิง M1 ในโซน ⇒ SL = Swing Low/High (ตามทิศ)
    3) ยังไม่ถึง 80–100 ⇒ SL = Fibo 80
    """
    fibo_80 = fibo1['80']
    fibo_100 = fibo1['100']
    in_80_100 = price_in_range(price_now, fibo_80, fibo_100)

    # 1) POC in 80–100?
    if poc_m1 is not None and price_in_range(poc_m1, fibo_80, fibo_100):
        if bias == 'up':
            return poc_m1 * (1 - 0.001)  # 0.10% ใต้ POC
        else:
            return poc_m1 * (1 + 0.001)  # เหนือ POC 0.10%

    # 2) price touches 80–100 and has swing M1 in zone
    if m1 is None:
        m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=300)
    m1_closed = m1[:-1] if len(m1) > 1 else m1
    if in_80_100:
        zone_bars = [b for b in m1_closed if price_in_range(b[4], fibo_80, fibo_100)]
        if zone_bars:
            lows = [b[3] for b in zone_bars]; highs = [b[2] for b in zone_bars]
            if bias == 'up':
                return min(lows)  # swing low ภายในโซน
            else:
                return max(highs) # swing high ภายในโซน

    # 3) not yet 80–100 → SL = Fibo 80
    return fibo_80

def reset_to_step1():
    smc_state.update({
        'step': 1,
        'bias': None,
        'latest_h1_event': None,
        'fibo1': None,
        'entry_zone': None,
        'poc_m1': None
    })
    reset_alerts()
    alert_once("STEP1_WAIT", "🔁 [RESET] กลับ STEP1: รอ H1 SMC (BOS/CHOCH)")

# ---------------------------
# MAIN LOOP
# ---------------------------
def main_loop():
    global current_position, pending_trade, cooldown_until
    logger.info("Main loop started")
    while True:
        try:
            now = datetime.utcnow()
            if cooldown_until and now < cooldown_until:
                time.sleep(CHECK_INTERVAL); continue

            # Price & Position
            ticker = exchange.fetch_ticker(SYMBOL)
            current_price = float(ticker['last'])
            pos = get_current_position()
            current_position = pos

            # H1 SMC
            ohlcv_h1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_H1, limit=180)
            st = latest_smc_state(ohlcv_h1, left=SWING_LEFT, right=SWING_RIGHT)
            latest = st['latest_event']; bias = st['bias']

            # ถ้ามีโพซิชันและเจอ CHOCH ตรงข้าม → ปิดทันที + reset
            if current_position and latest and latest['signal'].upper() == 'CHOCH':
                if (current_position['side']=='long' and latest['bias_after']=='down') or (current_position['side']=='short' and latest['bias_after']=='up'):
                    send_telegram(f"⚠ CHOCH สวนทาง → ปิด {current_position['side']} @ {current_price:.2f} และรีเซ็ต")
                    close_position_by_market(current_position)
                    add_trade_record('CHoCH Close', current_position, current_price)
                    cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                    current_position = None; pending_trade = None
                    reset_to_step1()
                    time.sleep(CHECK_INTERVAL); continue

            # ไม่มีโพซิชัน → STEP Machine
            if not current_position:
                # STEP1
                if smc_state['step'] == 1:
                    if bias is None:
                        alert_once("STEP1_WAIT", "🧭 [STEP1] รอ H1 SMC (BOS/CHOCH)")
                        time.sleep(CHECK_INTERVAL); continue
                    smc_state['bias'] = bias
                    smc_state['latest_h1_event'] = latest
                    smc_state['step'] = 2
                    reset_alerts()
                    alert_once("STEP1_OK", f"🧭 [STEP1→OK] H1 {latest['signal']} → เทรนด์ = {bias.upper()} (ไป STEP2)")

                # STEP2: Fibo + POC(M1) rule + รอเข้าโซน
                if smc_state['step'] == 2:
                    fibo1, entry_zone = prepare_fibo1(ohlcv_h1)
                    smc_state['fibo1'] = fibo1
                    smc_state['entry_zone'] = entry_zone

                    # คำนวณ POC จาก M1 pullback ตาม bias
                    poc_m1 = compute_m1_poc_for_pullback(fibo1, smc_state['bias'])
                    smc_state['poc_m1'] = poc_m1

                    # H1 close vs M1 POC cancel
                    if not check_poc_filter_h1_close_vs_poc(smc_state['bias'], poc_m1, ohlcv_h1):
                        smc_state['step'] = 1
                        time.sleep(CHECK_INTERVAL); continue

                    alert_once("STEP2_WAIT", "⌛ [STEP2] รอราคาเข้าโซน Fibo (H1)")

                    if strict_in_zone(current_price, entry_zone):
                        # POC Rule: ถ้า POC อยู่ต่ำกว่า 71.8% (uptrend) → ไม่นับเป็น POC ใช้งาน
                        if poc_m1 is not None and smc_state['bias'] == 'up':
                            if poc_m1 < fibo1['71.8']:
                                poc_m1 = None  # ignore

                        smc_state['poc_m1'] = poc_m1
                        reset_alerts("STEP2_")
                        alert_once("STEP2_INZONE", "📏 [STEP2] เข้าโซน Fibo 33–78.6 (H1) → ไป STEP3")
                        smc_state['step'] = 3
                        reset_alerts()
                    else:
                        time.sleep(CHECK_INTERVAL); continue

                # STEP3: รอ M1 Confirm (C2) — (CHOCH_closed OR MACD_closed) AND strict zone
                if smc_state['step'] == 3:
                    alert_once("STEP3_WAIT", "🧪 [STEP3] รอ M1 Confirm")

                    if not strict_in_zone(current_price, smc_state['entry_zone']):
                        alert_once("STEP3_OUTZONE", "⏸️ [STEP3] ราคาออกนอกโซน Fibo → รอกลับเข้าโซน")
                        time.sleep(CHECK_INTERVAL); continue

                    direction = 'up' if smc_state['bias']=='up' else 'down'
                    choch_ok = m1_choch_in_direction_closed(direction) if USE_M1_CHOCH_CONFIRM else False
                    m1_data = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=300)
                    macd_dir = macd_cross_dir_closed(m1_data) if USE_MACD_CONFIRM else None
                    macd_ok = (macd_dir == direction) if USE_MACD_CONFIRM else False

                    if ((USE_M1_CHOCH_CONFIRM and choch_ok) or (USE_MACD_CONFIRM and macd_ok)):
                        # สร้าง SL ตามกฎ
                        sl_price = derive_sl_from_rules(smc_state['fibo1'], smc_state['bias'], smc_state['poc_m1'], current_price, m1_data)
                        equity = get_equity()
                        proposed, _, _ = compute_contracts_from_portfolio(equity, current_price)
                        final_contracts = cap_size_by_risk(equity, current_price, proposed, sl_price)
                        if final_contracts <= 0:
                            send_telegram("⚠ ขนาดสัญญาหลัง cap risk = 0 ข้ามเทรดนี้")
                            smc_state['step'] = 1
                            time.sleep(CHECK_INTERVAL); continue

                        pos = open_market_order('long' if direction=='up' else 'short', final_contracts)
                        if not pos:
                            time.sleep(CHECK_INTERVAL); continue

                        tp1_price = smc_state['fibo1']['0'] if direction=='up' else smc_state['fibo1']['100']
                        pending_trade = {
                            'side': pos['side'], 'entry_price': pos['entry_price'], 'size': pos['size'],
                            'fibo1': smc_state['fibo1'], 'poc_m1': smc_state['poc_m1'],
                            'sl_price': sl_price, 'tp1_price': tp1_price,
                            'state': 'OPEN', 'opened_at': datetime.utcnow().isoformat(),
                            'trend': direction, 'contracts': final_contracts
                        }
                        current_position = pos
                        reset_alerts()
                        send_telegram(
                            f"📈 [ENTRY] {pending_trade['side'].upper()} @ {pending_trade['entry_price']:.2f} "
                            f"| SL: {pending_trade['sl_price']:.2f} | TP1: {pending_trade['tp1_price']:.2f} "
                            f"| Qty: {final_contracts} | PAPER={PAPER_TRADING}"
                        )
                        smc_state['step'] = 99
                        time.sleep(CHECK_INTERVAL); continue
                    else:
                        time.sleep(CHECK_INTERVAL); continue

            # มีโพซิชัน → จัดการ TP/SL
            if current_position:
                ticker = exchange.fetch_ticker(SYMBOL)
                current_price = float(ticker['last'])
                pos = current_position

                if pending_trade is None:
                    pending_trade = {
                        'side': pos['side'],
                        'entry_price': pos['entry_price'],
                        'size': pos['size'],
                        'opened_at': datetime.utcnow().isoformat(),
                        'state': 'OPEN',
                        'contracts': pos['size'],
                        'trend': 'up' if pos['side']=='long' else 'down'
                    }

                # TP1
                tp1_hit = False
                if pos['side']=='long':
                    if current_price >= pending_trade.get('tp1_price', float('inf')) * (1 - PRICE_TOLERANCE_PCT):
                        tp1_hit = True
                else:
                    if current_price <= pending_trade.get('tp1_price', 0) * (1 + PRICE_TOLERANCE_PCT):
                        tp1_hit = True

                if tp1_hit and pending_trade['state']=='OPEN':
                    close_amt = max(1, int(round(pending_trade['contracts'] * TP1_CLOSE_PERCENT)))
                    side_to_close = 'sell' if pos['side']=='long' else 'buy'
                    try:
                        if PAPER_TRADING:
                            # ปิดบางส่วนใน paper: ลด size ใน current_position
                            remaining = pending_trade['contracts'] - close_amt
                            if remaining < 1:
                                remaining = 1
                            current_position['size'] = remaining
                            pending_trade['contracts'] = remaining
                        else:
                            amt_prec = exchange.amount_to_precision(SYMBOL, float(close_amt))
                            exchange.create_market_order(
                                SYMBOL, side_to_close, float(amt_prec),
                                params={'tdMode': OKX_MARGIN_MODE, 'reduceOnly': True}
                            )
                        send_telegram(f"✅ [TP1] ปิด {TP1_CLOSE_PERCENT*100:.0f}% @ {current_price:.2f}")
                        pending_trade['state'] = 'TP1_HIT'
                        # fibo2
                        ohlcv_small = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M5, limit=M5_LOOKBACK)
                        highs = [b[2] for b in ohlcv_small] if ohlcv_small else []
                        base = pending_trade['tp1_price']
                        hh = max(highs) if highs else base*1.05
                        if hh <= base: hh = base*1.03
                        diff = hh - base
                        fibo2 = {
                            '100': base,
                            '78.6': base + FIBO2_SL_LEVEL*diff,
                            'ext133': base + FIBO2_EXT_MIN*diff,
                            'ext161.8': base + FIBO2_EXT_MAX*diff
                        }
                        pending_trade['fibo2'] = fibo2
                        pending_trade['sl_price_step2'] = fibo2['78.6']
                        send_telegram(
                            f"🔁 [SL] เลื่อนไป Fibo2 78.6 = {fibo2['78.6']:.2f} "
                            f"| TP2 {fibo2['ext133']:.2f}-{fibo2['ext161.8']:.2f}"
                        )
                    except Exception as e:
                        logger.error("TP1 partial close failed: %s", e)
                        send_telegram(f"⚠ ปิดบางส่วน TP1 ล้มเหลว: {e}")

                # TP2 / Emergency / SL2
                if pending_trade.get('state')=='TP1_HIT' and 'fibo2' in pending_trade:
                    fibo2 = pending_trade['fibo2']
                    lo, hi = fibo2['ext133'], fibo2['ext161.8']
                    in_tp2 = (current_price >= lo * (1 - PRICE_TOLERANCE_PCT)) and (current_price <= hi * (1 + PRICE_TOLERANCE_PCT))

                    # emergency momentum m1
                    m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=10)
                    emergency = False
                    if len(m1) >= 3:
                        last, prev, prev2 = m1[-1][4], m1[-2][4], m1[-3][4]
                        if pending_trade['side']=='long' and last<prev and prev<prev2: emergency = True
                        if pending_trade['side']=='short' and last>prev and prev>prev2: emergency = True

                    if emergency:
                        send_telegram(f"⚠ [EMERGENCY] ปิดส่วนที่เหลือ @ {current_price:.2f}")
                        close_position_by_market(pos)
                        pending_trade['state'] = 'CLOSED_EMERGENCY'
                        add_trade_record('Emergency Close', pos, current_price)
                        cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                        current_position = None; pending_trade = None
                        reset_to_step1()
                        time.sleep(5); continue

                    if in_tp2:
                        send_telegram(f"🏁 [TP2] ปิดส่วนที่เหลือ @ {current_price:.2f}")
                        close_position_by_market(pos)
                        pending_trade['state'] = 'TP2_HIT'
                        add_trade_record('TP2', pos, current_price)
                        cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                        current_position = None; pending_trade = None
                        reset_to_step1()
                        time.sleep(5); continue

                    sl2 = pending_trade.get('sl_price_step2')
                    if sl2:
                        if pending_trade['side']=='long' and current_price <= sl2 * (1 + PRICE_TOLERANCE_PCT):
                            send_telegram(f"🛑 [SL2] ปิดส่วนที่เหลือ @ {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL_STEP2'
                            add_trade_record('SL_STEP2', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None; pending_trade = None
                            reset_to_step1()
                            time.sleep(5); continue
                        if pending_trade['side']=='short' and current_price >= sl2 * (1 - PRICE_TOLERANCE_PCT):
                            send_telegram(f"🛑 [SL2] ปิดส่วนที่เหลือ @ {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL_STEP2'
                            add_trade_record('SL_STEP2', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None; pending_trade = None
                            reset_to_step1()
                            time.sleep(5); continue

                # SL เริ่มต้น
                if pending_trade and pending_trade.get('state')=='OPEN':
                    slp = pending_trade.get('sl_price')
                    if slp:
                        if pending_trade['side']=='long' and current_price <= slp * (1 + PRICE_TOLERANCE_PCT):
                            send_telegram(f"❌ [SL] ปิด @ {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL'
                            add_trade_record('SL', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None; pending_trade = None
                            reset_to_step1()
                            time.sleep(5); continue
                        if pending_trade['side']=='short' and current_price >= slp * (1 - PRICE_TOLERANCE_PCT):
                            send_telegram(f"❌ [SL] ปิด @ {current_price:.2f}")
                            close_position_by_market(pos)
                            pending_trade['state'] = 'SL'
                            add_trade_record('SL', pos, current_price)
                            cooldown_until = datetime.utcnow() + timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            current_position = None; pending_trade = None
                            reset_to_step1()
                            time.sleep(5); continue

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            logger.exception("Main loop error: %s", e)
            send_telegram(f"⛔ ข้อผิดพลาดในบอท: {e}")
            time.sleep(10)

# ---------------------------
# START
# ---------------------------
def start_bot():
    try:
        setup_exchange()
        init_market_scan_and_set_trend()
        if os.path.exists(STATS_FILE):
            try:
                with open(STATS_FILE, 'r') as f:
                    monthly_stats.update(json.load(f))
            except Exception:
                pass
        threading.Thread(target=monthly_report_thread, daemon=True).start()
        main_loop()
    except Exception as e:
        logger.critical("start_bot fatal: %s", e)
        send_telegram(f"⛔ เริ่มบอทล้มเหลว: {e}")

if __name__ == '__main__':
    logger.info("Starting SMC + M1 POC Bot (isolated, TH alerts, one-shot)")
    mode_txt = "🧪 โหมดทดลอง (PAPER) — ไม่เปิดออเดอร์จริง" if PAPER_TRADING else "💰 โหมดเงินจริง"
    send_telegram(
        f"🤖 เริ่มบอท: SMC + Strict Zone + M1 POC SL-rules + (M1 CHOCH หรือ MACD ปิดแท่ง) + One-shot Alerts "
        f"+ isolated\n{mode_txt}"
    )
    start_bot()
