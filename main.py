import os
import time
import math
import logging
import requests
import ccxt
from datetime import datetime
from pathlib import Path
import csv

# ================== CONFIG (ปรับค่าตรงนี้) ==================
API_KEY  = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET   = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')

SYMBOL = 'BTC-USDT-SWAP'  # OKX USDT Perp | amount = #contracts (contractSize ~ 0.01 BTC)

# ===== EMA SETTINGS =====
TFM = '1h'                 # ใช้ 1h
EMA_FAST = 9               # EMA 9
EMA_SLOW = 50              # EMA 50

# ===== RISK / SIZING =====
PORTFOLIO_PERCENTAGE = 0.80
LEVERAGE = 40
MARGIN_MODE = 'isolated'
FEE_RATE_TAKER = 0.001
FIXED_BUFFER_USDT = 2.0

# ===== TP / SL (3 STEP) =====
TP_POINTS = 700.0

# Step1: ถึง Trigger → เลื่อน SL เป็น NEW_SL
SL_STEP1_TRIGGER_LONG  = 200.0
SL_STEP1_NEW_SL_LONG   = -900.0
SL_STEP1_TRIGGER_SHORT = 200.0
SL_STEP1_NEW_SL_SHORT  = 900.0

# Step2:
SL_STEP2_TRIGGER_LONG  = 350.0
SL_STEP2_NEW_SL_LONG   = -400.0
SL_STEP2_TRIGGER_SHORT = 350.0
SL_STEP2_NEW_SL_SHORT  = 400.0

# Step3: (นับเป็น TP ตอนโดนปิด)
SL_STEP3_TRIGGER_LONG  = 510.0
SL_STEP3_NEW_SL_LONG   = 460.0
SL_STEP3_TRIGGER_SHORT = 510.0
SL_STEP3_NEW_SL_SHORT  = -460.0

# Manual TP Alert
MANUAL_TP_ALERT_POINTS = 1000.0
MANUAL_TP_ALERT_INTERVAL_SEC = 600
ENABLE_MANUAL_TP_ALERT = True

# ===== LOOP INTERVAL =====
POLL_INTERVAL_SECONDS = float(os.getenv('POLL_INTERVAL_SECONDS', '3'))

# ===== TELEGRAM =====
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID', '')

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger('ema-cross-bot')

# ===== MONTHLY STATS (CSV) =====
STATS_FILE = Path('okx_monthly_stats.csv')

# ================== EXCHANGE INIT ==================
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})
exchange.verbose = False

# ================== TELEGRAM ==================
def tg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")

def _fmt_num(x, digits=2):
    try: return f"{float(x):,.{digits}f}"
    except Exception: return str(x)

def notify_startup_banner(start_balance: float):
    base_sl_pts = max(abs(SL_STEP1_NEW_SL_LONG), abs(SL_STEP1_NEW_SL_SHORT))
    lines = [
        "🤖 บอทเริ่มทำงาน 💰",
        f"💵 ยอดเริ่มต้น: {_fmt_num(start_balance, 2)} USDT",
        f"📊 TF: {TFM} | Leverage: {LEVERAGE}x",
        f"📈 EMA Fast: {EMA_FAST}",
        f"📉 EMA Slow: {EMA_SLOW}",
        f"❌ SL เริ่มต้น: {int(base_sl_pts)} points",
        f"🚀 • Step 1: {int(SL_STEP1_TRIGGER_LONG)}pts → SL {int(SL_STEP1_NEW_SL_LONG)}pts",
        f"🔥 • Step 2: {int(SL_STEP2_TRIGGER_LONG)}pts → SL {int(SL_STEP2_NEW_SL_LONG)}pts",
        f"🎉 • Step 3 (TP): {int(SL_STEP3_TRIGGER_LONG)}pts → SL {int(SL_STEP3_NEW_SL_LONG)}pts",
        f"⏰ Manual TP Alert: {int(MANUAL_TP_ALERT_POINTS)} points (จะแจ้งเตือนปิดกำไร)",
        "🔎 กำลังรอเปิดออเดอร์..."
    ]
    tg("\n".join(lines))

def notify_open_detailed(pos_side, contracts, entry_px):
    tg(
        "🎯 เปิดโพซิชันสำเร็จ!\n"
        f"📌 {pos_side.upper()} | 📊 Size: {contracts:.8f} Contracts\n"
        f"💵 Entry: {_fmt_num(entry_px, 2)}\n"
        "📈 P&L: 0.00 USDT"
    )

def notify_set_sl(direction: str, sl_price: float, size_contracts: float):
    tg(
        "✅ ตั้ง SL สำเร็จ!\n"
        f"📌 {direction.upper()} | 🛑 SL: {_fmt_num(sl_price, 2)}\n"
        f"🧱 Size: {size_contracts:.8f}"
    )

def notify_manual_tp_alert(side: str, entry_px: float, cur_px: float, gain_points: float):
    tg(
        "🔔 Manual TP Alert!\n"
        f"💵 กำไรปัจจุบัน: +{int(gain_points)} points\n"
        f"📈 Entry: {_fmt_num(entry_px, 2)} → Now: {_fmt_num(cur_px, 2)}\n"
        "💡 แนะนำปิดกำไรด้วยมือ 🔥"
    )

def notify_close(side, contracts, entry_px, exit_px, contract_size, reason):
    price_diff = (exit_px - entry_px) if side == 'long' else (entry_px - exit_px)
    pnl_per_contract = price_diff * contract_size
    pnl_total = pnl_per_contract * contracts
    flag = "🎉 TP" if reason == 'TP' else ("🔥 SL" if reason == 'SL' else "✋ MANUAL")
    tg(
        f"✅ CLOSE {side.upper()} {contracts} | {flag}\n"
        f"entry≈{entry_px:.2f} | exit≈{exit_px:.2f}\n"
        f"PnL/contract≈{pnl_per_contract:.2f} USDT | Total≈{pnl_total:.2f} USDT"
    )

# ================== MONTHLY STATS ==================
def _ensure_stats_file():
    if not STATS_FILE.exists():
        with STATS_FILE.open('w', newline='') as f:
            csv.writer(f).writerow(['timestamp','month','symbol','side','reason','contracts','entry','exit','pnl_usdt'])

def add_trade_result(side: str, reason: str, contracts: int, entry_px: float, exit_px: float, contract_size: float):
    price_diff = (exit_px - entry_px) if side == 'long' else (entry_px - exit_px)
    pnl_per_contract = price_diff * contract_size
    pnl_total = pnl_per_contract * contracts
    _ensure_stats_file()
    with STATS_FILE.open('a', newline='') as f:
        csv.writer(f).writerow([
            datetime.utcnow().isoformat(), datetime.utcnow().strftime('%Y-%m'),
            SYMBOL, side, reason, contracts, f'{entry_px:.2f}', f'{exit_px:.2f}', f'{pnl_total:.2f}'
        ])

# ================== OKX HELPERS ==================
def set_isolated_leverage():
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL, params={'mgnMode': MARGIN_MODE})
        log.info(f"Leverage {LEVERAGE}x ({MARGIN_MODE}) set")
    except Exception as e:
        log.error(f"Set leverage failed: {e}")

def cancel_all_open_orders():
    try:
        for o in exchange.fetch_open_orders(SYMBOL):
            try:
                exchange.cancel_order(o['id'], SYMBOL)
            except Exception:
                pass
        log.info("Canceled all open orders")
    except Exception as e:
        log.warning(f"Fetch open orders failed: {e}")

def get_price() -> float:
    try: return float(exchange.fetch_ticker(SYMBOL)['last'])
    except Exception as e:
        log.error(f"Fetch price failed: {e}"); return 0.0

def get_contract_size() -> float:
    try:
        m = exchange.load_markets().get(SYMBOL) or {}
        cs = float(m.get('contractSize') or 0.01)
        return cs if cs > 0 else 0.01
    except Exception:
        return 0.01

def get_avail_net_usdt() -> float:
    try:
        bal = exchange.fetch_balance({'type': 'swap'})
        data = (bal.get('info', {}).get('data') or [])
        if not data: return 0.0
        first = data[0]; details = first.get('details')
        if isinstance(details, list):
            for item in details:
                if item.get('ccy') == 'USDT':
                    avail = float(item.get('availBal') or 0)
                    frozen = float(item.get('ordFrozen') or 0)
                    return max(0.0, avail - frozen)
        avail = float(first.get('availBal') or first.get('cashBal') or first.get('eq') or 0)
        frozen = float(first.get('ordFrozen') or 0)
        return max(0.0, avail - frozen)
    except Exception as e:
        log.error(f"Fetch balance failed: {e}"); return 0.0

def calc_contracts(price: float, contract_size: float, avail_net: float) -> int:
    usable = max(0.0, avail_net - FIXED_BUFFER_USDT) * PORTFOLIO_PERCENTAGE
    notional_ct = price * contract_size
    im_ct = notional_ct / LEVERAGE
    fee_ct = notional_ct * FEE_RATE_TAKER
    need_ct = im_ct + fee_ct
    if need_ct <= 0: return 0
    return max(int(math.floor(usable / need_ct)), 0)

def fetch_ema_set():
    """return (fast_prev, slow_prev, fast_now, slow_now)"""
    try:
        limit = max(EMA_SLOW + 5, 400)
        ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TFM, limit=limit)
        closes = [c[4] for c in ohlcv]
        if len(closes) < EMA_SLOW + 2: return (None, None, None, None)
        def ema(vals, n):
            k = 2/(n+1); e = vals[0]
            for v in vals[1:]: e = v*k + e*(1-k)
            return e
        fast_now = ema(closes, EMA_FAST)
        slow_now = ema(closes, EMA_SLOW)
        fast_prev = ema(closes[:-1], EMA_FAST)
        slow_prev = ema(closes[:-1], EMA_SLOW)
        return (fast_prev, slow_prev, fast_now, slow_now)
    except Exception as e:
        log.error(f"Fetch EMA failed: {e}")
        return (None, None, None, None)

def open_market(side: str, contracts: int):
    return exchange.create_order(SYMBOL, 'market', side, contracts, None, {'tdMode': MARGIN_MODE})

def close_market(current_side: str, contracts: int):
    if current_side == 'flat' or contracts <= 0: return None
    side = 'sell' if current_side == 'long' else 'buy'
    return exchange.create_order(SYMBOL, 'market', side, contracts, None, {'tdMode': MARGIN_MODE, 'reduceOnly': True})

# ================== LOGGING HELPERS ==================
def log_tick_status(side_hint, f_now, s_now, in_pos, pos_side, price):
    side_txt = 'NONE' if side_hint is None else str(side_hint).upper()
    if not in_pos:
        log.info(f"📊 Waiting... side={side_txt} | fast={f_now:.2f} | slow={s_now:.2f}")
    else:
        log.info(f"📊 In-Position {pos_side.upper()} | px≈{price:.2f} | fast={f_now:.2f} | slow={s_now:.2f}")

# ================== MAIN ==================
if __name__ == "__main__":
    # Startup
    set_isolated_leverage()
    cancel_all_open_orders()
    contract_size = get_contract_size()

    start_balance = get_avail_net_usdt()
    notify_startup_banner(start_balance)

    # ===== INTERNAL STATE =====
    in_pos = False
    pos_side = 'flat'
    pos_ct = 0
    entry_px = None
    high_water = None   # สำหรับ long
    low_water  = None   # สำหรับ short
    curr_sl = None
    sl_step = 0
    last_manual_alert_ts = 0.0

    # 👉 คุม “รอการตัดครั้งใหม่”
    last_cross_dir = None   # 'long' | 'short' | None

    while True:
        try:
            f_prev, s_prev, f_now, s_now = fetch_ema_set()
            if None in (f_prev, s_prev, f_now, s_now):
                if True:
                    log_tick_status(last_cross_dir, f_now or 0.0, s_now or 0.0, in_pos, pos_side, get_price())
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            price = get_price()
            avail_net = get_avail_net_usdt()

            # ===== ตั้งค่าเริ่ม last_cross_dir (กันเข้าโดยยังไม่เกิดการตัดจริง) =====
            if last_cross_dir is None and not in_pos:
                if f_now > s_now: last_cross_dir = 'long'
                elif f_now < s_now: last_cross_dir = 'short'
                else: last_cross_dir = None
                log.info(f"🎯 Armed side: {last_cross_dir.upper() if last_cross_dir else 'NONE'} (fast={f_now:.2f}, slow={s_now:.2f})")

            # ===== POSITION MANAGEMENT =====
            if in_pos:
                if pos_side == 'long':
                    tp = entry_px + TP_POINTS
                    base_sl = entry_px - abs(SL_STEP1_NEW_SL_LONG)
                    high_water = price if high_water is None else max(high_water, price)
                    pnl_pts = price - entry_px

                    desired_sl = base_sl; step_target = 0
                    if pnl_pts >= SL_STEP1_TRIGGER_LONG: desired_sl = entry_px + SL_STEP1_NEW_SL_LONG; step_target = 1
                    if pnl_pts >= SL_STEP2_TRIGGER_LONG: desired_sl = entry_px + SL_STEP2_NEW_SL_LONG; step_target = 2
                    if pnl_pts >= SL_STEP3_TRIGGER_LONG: desired_sl = entry_px + SL_STEP3_NEW_SL_LONG; step_target = 3

                    if curr_sl is None:
                        curr_sl = base_sl; notify_set_sl('long', curr_sl, pos_ct)
                    if desired_sl > curr_sl + 1e-9:
                        curr_sl = desired_sl; sl_step = step_target; notify_set_sl('long', curr_sl, pos_ct)

                    if ENABLE_MANUAL_TP_ALERT and pnl_pts >= MANUAL_TP_ALERT_POINTS:
                        now = time.time()
                        if now - last_manual_alert_ts >= MANUAL_TP_ALERT_INTERVAL_SEC:
                            last_manual_alert_ts = now
                            notify_manual_tp_alert('long', entry_px, price, pnl_pts)

                    if price >= tp:
                        close_market('long', pos_ct)
                        notify_close('long', pos_ct, entry_px, price, contract_size, reason='TP')
                        add_trade_result('long', 'TP', pos_ct, entry_px, price, contract_size)
                        cancel_all_open_orders()

                        # รีเซ็ตเหมือนเริ่มใหม่ + ตั้งฝั่ง EMA ปัจจุบัน
                        last_cross_dir = 'long' if f_now > s_now else ('short' if f_now < s_now else None)
                        in_pos=False; pos_side='flat'; pos_ct=0; entry_px=None
                        high_water=None; low_water=None; curr_sl=None; sl_step=0

                    elif price <= curr_sl:
                        close_market('long', pos_ct)
                        reason = 'TP' if sl_step == 3 else 'SL'
                        notify_close('long', pos_ct, entry_px, price, contract_size, reason=reason)
                        add_trade_result('long', reason, pos_ct, entry_px, price, contract_size)
                        cancel_all_open_orders()

                        last_cross_dir = 'long' if f_now > s_now else ('short' if f_now < s_now else None)
                        in_pos=False; pos_side='flat'; pos_ct=0; entry_px=None
                        high_water=None; low_water=None; curr_sl=None; sl_step=0

                else:  # SHORT
                    tp = entry_px - TP_POINTS
                    base_sl = entry_px + abs(SL_STEP1_NEW_SL_SHORT)
                    low_water = price if low_water is None else min(low_water, price)
                    pnl_pts = entry_px - price

                    desired_sl = base_sl; step_target = 0
                    if pnl_pts >= SL_STEP1_TRIGGER_SHORT: desired_sl = entry_px + SL_STEP1_NEW_SL_SHORT; step_target = 1
                    if pnl_pts >= SL_STEP2_TRIGGER_SHORT: desired_sl = entry_px + SL_STEP2_NEW_SL_SHORT; step_target = 2
                    if pnl_pts >= SL_STEP3_TRIGGER_SHORT: desired_sl = entry_px + SL_STEP3_NEW_SL_SHORT; step_target = 3

                    if curr_sl is None:
                        curr_sl = base_sl; notify_set_sl('short', curr_sl, pos_ct)
                    if desired_sl < curr_sl - 1e-9:
                        curr_sl = desired_sl; sl_step = step_target; notify_set_sl('short', curr_sl, pos_ct)

                    if ENABLE_MANUAL_TP_ALERT and pnl_pts >= MANUAL_TP_ALERT_POINTS:
                        now = time.time()
                        if now - last_manual_alert_ts >= MANUAL_TP_ALERT_INTERVAL_SEC:
                            last_manual_alert_ts = now
                            notify_manual_tp_alert('short', entry_px, price, pnl_pts)

                    if price <= tp:
                        close_market('short', pos_ct)
                        notify_close('short', pos_ct, entry_px, price, contract_size, reason='TP')
                        add_trade_result('short', 'TP', pos_ct, entry_px, price, contract_size)
                        cancel_all_open_orders()

                        last_cross_dir = 'long' if f_now > s_now else ('short' if f_now < s_now else None)
                        in_pos=False; pos_side='flat'; pos_ct=0; entry_px=None
                        high_water=None; low_water=None; curr_sl=None; sl_step=0

                    elif price >= curr_sl:
                        close_market('short', pos_ct)
                        reason = 'TP' if sl_step == 3 else 'SL'
                        notify_close('short', pos_ct, entry_px, price, contract_size, reason=reason)
                        add_trade_result('short', reason, pos_ct, entry_px, price, contract_size)
                        cancel_all_open_orders()

                        last_cross_dir = 'long' if f_now > s_now else ('short' if f_now < s_now else None)
                        in_pos=False; pos_side='flat'; pos_ct=0; entry_px=None
                        high_water=None; low_water=None; curr_sl=None; sl_step=0

            # ===== ENTRY: เปิดเมื่อเกิด “การตัดครั้งใหม่” เท่านั้น =====
            if not in_pos and last_cross_dir:
                cross_up   = (f_prev <= s_prev) and (f_now > s_now)
                cross_down = (f_prev >= s_prev) and (f_now < s_now)

                open_dir = None
                if cross_up and last_cross_dir != 'long':
                    open_dir = 'long'
                    log.info("✅ SIGNAL: EMA Cross UP → เปิด LONG")
                elif cross_down and last_cross_dir != 'short':
                    open_dir = 'short'
                    log.info("✅ SIGNAL: EMA Cross DOWN → เปิด SHORT")

                if open_dir:
                    contracts = calc_contracts(price, contract_size, avail_net)
                    if contracts < 1:
                        log.warning("Margin ไม่พอเปิด 1 สัญญา")
                    else:
                        side = 'buy' if open_dir == 'long' else 'sell'
                        open_market(side, contracts)
                        in_pos = True
                        pos_side = open_dir
                        pos_ct = contracts
                        entry_px = price
                        high_water = price if pos_side == 'long' else None
                        low_water  = price if pos_side == 'short' else None
                        curr_sl = None
                        sl_step = 0
                        last_cross_dir = open_dir  # จำว่าตัดไปทางนี้แล้ว
                        notify_open_detailed(pos_side, pos_ct, entry_px)

            # ===== LOG =====
            log_tick_status(last_cross_dir, f_now, s_now, in_pos, pos_side, price)

        except Exception as e:
            log.error(f"Loop error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)
