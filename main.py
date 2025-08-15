import os
import time
import math
import logging
import requests
import ccxt

# ================== CONFIG ==================
API_KEY  = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET   = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')

SYMBOL = 'BTC-USDT-SWAP'           # OKX USDT Perp | amount = #contracts

# EMA settings (ค่าเริ่มตามที่ขอ)
TFM = os.getenv('TFM', '1m')      # '1m','5m','15m','1h',...
EMA_FAST = int(os.getenv('EMA_FAST', '9'))
EMA_SLOW = int(os.getenv('EMA_SLOW', '25'))

# Risk/Sizing (ล็อค)
PORTFOLIO_PERCENTAGE = 0.80        # ใช้ 80% ของพอร์ต
LEVERAGE = 40
MARGIN_MODE = 'isolated'
FEE_RATE_TAKER = 0.001
FIXED_BUFFER_USDT = 2.0            # กันเศษเล็กน้อย

# TP/SL/Trailing (ปรับได้)
TP_POINTS = float(os.getenv('TP_POINTS', '111'))     # +300 จุด
SL_POINTS = float(os.getenv('SL_POINTS', '500'))     # -500 จุด
TRAIL_POINTS = float(os.getenv('TRAIL_POINTS', '99'))
BE_OFFSET = float(os.getenv('BE_OFFSET', '-10'))      # กันทุน +50

# Loop interval
POLL_INTERVAL_SECONDS = float(os.getenv('POLL_INTERVAL_SECONDS', '3'))

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID', '')

# Log ทุกติ๊ก
LOG_EVERY_TICK = True

# ================== LOGGER ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger('ema-armed-bot')

# ================== EXCHANGE ==================
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})
exchange.verbose = False

# ================== HELPERS ==================
def tg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")

def set_isolated_leverage():
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL, params={'mgnMode': MARGIN_MODE})
        log.info(f"Leverage {LEVERAGE}x ({MARGIN_MODE}) set")
    except Exception as e:
        log.error(f"Set leverage failed: {e}")

def cancel_all_open_orders():
    try:
        opens = exchange.fetch_open_orders(SYMBOL)
        for o in opens:
            try:
                exchange.cancel_order(o['id'], SYMBOL)
                log.info(f"Canceled open order: {o['id']}")
            except Exception as e:
                log.warning(f"Cancel order {o.get('id')} failed: {e}")
    except Exception as e:
        log.warning(f"Fetch open orders failed: {e}")

def get_price() -> float:
    try:
        return float(exchange.fetch_ticker(SYMBOL)['last'])
    except Exception as e:
        log.error(f"Fetch price failed: {e}")
        return 0.0

def get_contract_size() -> float:
    try:
        m = exchange.load_markets().get(SYMBOL) or {}
        cs = float(m.get('contractSize') or 0.0)
        return cs if 0 < cs < 1 else 0.01  # BTC-USDT-SWAP ปกติ 0.01
    except Exception:
        return 0.01

def get_avail_net_usdt() -> float:
    try:
        bal = exchange.fetch_balance({'type': 'swap'})
        data = (bal.get('info', {}).get('data') or [])
        if not data: return 0.0
        first = data[0]
        details = first.get('details')
        avail, ord_frozen = 0.0, 0.0
        if isinstance(details, list):
            for item in details:
                if item.get('ccy') == 'USDT':
                    avail = float(item.get('availBal') or 0)
                    ord_frozen = float(item.get('ordFrozen') or 0)
                    break
        if not details:
            avail = float(first.get('availBal') or first.get('cashBal') or first.get('eq') or 0)
            ord_frozen = float(first.get('ordFrozen') or 0)
        return max(0.0, avail - ord_frozen)
    except Exception as e:
        log.error(f"Fetch balance failed: {e}")
        return 0.0

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
        limit = max(EMA_SLOW + 5, 400)  # พอสำหรับ EMA200
        ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TFM, limit=limit)
        closes = [c[4] for c in ohlcv]
        if len(closes) < EMA_SLOW + 2:
            return (None, None, None, None)

        def ema(vals, n):
            k = 2/(n+1)
            e = vals[0]
            for v in vals[1:]:
                e = v*k + e*(1-k)
            return e

        fast_now = ema(closes, EMA_FAST)
        slow_now = ema(closes, EMA_SLOW)
        fast_prev = ema(closes[:-1], EMA_FAST)
        slow_prev = ema(closes[:-1], EMA_SLOW)
        return (fast_prev, slow_prev, fast_now, slow_now)
    except Exception as e:
        log.error(f"Fetch EMA failed: {e}")
        return (None, None, None, None)

def get_position():
    """return ('flat'|'long'|'short', contracts, entry_price|None)"""
    try:
        pos = exchange.fetch_positions([SYMBOL])
        for p in pos:
            amt = float(p.get('contracts') or 0)
            if amt != 0:
                side = 'long' if amt > 0 else 'short'
                entry = float(p.get('entryPrice') or 0) or None
                return side, abs(int(amt)), entry
        return 'flat', 0, None
    except Exception:
        return 'flat', 0, None

def open_market(side: str, contracts: int):
    params = {'tdMode': MARGIN_MODE}
    order = exchange.create_order(SYMBOL, 'market', side, contracts, None, params)
    return order

def close_market(current_side: str, contracts: int):
    if current_side == 'flat' or contracts <= 0:
        return None
    side = 'sell' if current_side == 'long' else 'buy'
    params = {'tdMode': MARGIN_MODE, 'reduceOnly': True}
    return exchange.create_order(SYMBOL, 'market', side, contracts, None, params)

def notify_open(side, contracts, price):
    txt = f"🚀 OPEN {side.upper()} {contracts}\npx≈{price} | TF={TFM} | EMA {EMA_FAST}/{EMA_SLOW}"
    log.info(txt); tg(txt)

# --- NEW: แจ้งตอนเลื่อน SL ---
def notify_sl_move(side, old_sl, new_sl, reason):  # <-- NEW
    emoji = "🛡️" if reason == 'BE' else "🌀"  # BE=กันทุน, TRAIL=ตามเทรนด์
    txt = f"{emoji} {side.upper()} SL moved ({reason}) {old_sl:.1f} → {new_sl:.1f}"
    log.info(txt); tg(txt)

# --- NEW: ปิดโพซิชัน พร้อมระบุ TP หรือ SL และคำนวณ PnL เป็น USDT ---
def notify_close(side, contracts, entry_px, exit_px, contract_size, reason):  # <-- NEW
    price_diff = (exit_px - entry_px) if side == 'long' else (entry_px - exit_px)
    pnl_per_contract = price_diff * contract_size
    pnl_total = pnl_per_contract * contracts
    flag = "🎉 TP" if reason == 'TP' else "🔥 SL"
    txt = (f"✅ CLOSE {side.upper()} {contracts} | {flag}\n"
           f"entry≈{entry_px} exit≈{exit_px}\n"
           f"PnL/contract≈{pnl_per_contract:.2f} USDT | Total≈{pnl_total:.2f} USDT")
    log.info(txt); tg(txt)

def log_tick_status(armed_side, f_now, s_now, in_pos, pos_side, price):
    try:
        side_txt = 'NONE' if armed_side is None else armed_side.upper()
        if not in_pos:
            log.info(f"📊 Waiting... side={side_txt} | fast={f_now:.2f} | slow={s_now:.2f}")
        else:
            log.info(f"📊 In-Position {pos_side.upper()} | px≈{price:.2f} | fast={f_now:.2f} | slow={s_now:.2f}")
    except Exception:
        pass

# ================== MAIN (Armed Cross + Trailing) ==================
if __name__ == "__main__":
    # เริ่มงาน + แสดงหัว log ตามที่ต้องการ
    tg("🤖 บอทเริ่มทำงาน 💰")
    log.info("🤖 บอทเริ่มทำงาน 💰")
    set_isolated_leverage()
    cancel_all_open_orders()
    contract_size = get_contract_size()

    # ดึงยอดเริ่มต้นและ EMA เพื่อพิมพ์ตาม format
    start_balance = get_avail_net_usdt()
    f_prev, s_prev, f_now, s_now = fetch_ema_set()
    log.info(f"💰 ยอดเริ่มต้น ≈ {start_balance:.2f} USDT")
    log.info(f"📉Ema{EMA_FAST}/{EMA_SLOW} | fast={f_now if f_now else 0:.2f} | slow={s_now if s_now else 0:.2f}")
    log.info(f"🎉TP +{TP_POINTS} | 🔥SL -{SL_POINTS} | 🌀Trail {TRAIL_POINTS} | 🛡️BE +{BE_OFFSET}")
    log.info("🔍 กำลังรอเปิดออเดอร์...")

    # สถานะภายใน
    in_pos = False
    pos_side = 'flat'
    pos_ct = 0
    entry_px = None
    high_water = None  # long
    low_water  = None  # short
    armed_side = None  # 'long'|'short'
    curr_sl = None     # <-- NEW: เก็บ SL ปัจจุบันเพื่อแจ้งเมื่อขยับ

    while True:
        try:
            f_prev, s_prev, f_now, s_now = fetch_ema_set()
            if None in (f_prev, s_prev, f_now, s_now):
                if LOG_EVERY_TICK:
                    log_tick_status(armed_side, f_now or 0.0, s_now or 0.0, in_pos, pos_side, get_price())
                time.sleep(POLL_INTERVAL_SECONDS); continue

            price = get_price()
            avail_net = get_avail_net_usdt()

            # กำหนดฝั่งที่ arm เมื่อยังไม่ตั้ง หรือหลังปิดออเดอร์
            if armed_side is None and not in_pos:
                armed_side = 'long' if f_now < s_now else 'short'
                log.info(f"🎯 Armed side: {armed_side.upper()} (fast={f_now:.2f}, slow={s_now:.2f})")

            # จัดการ TP/SL/Trailing เมื่อมีโพซิชัน
            if in_pos:
                if pos_side == 'long':
                    tp = entry_px + TP_POINTS
                    base_sl = entry_px - SL_POINTS

                    # อัปเดต high_water
                    high_water = price if high_water is None else max(high_water, price)

                    # เปิดใช้งาน BE/Trail เมื่อกำไรถึงเกณฑ์
                    be_active = high_water >= entry_px + BE_OFFSET
                    trail_active = high_water >= entry_px + TRAIL_POINTS

                    # รวม candidate ของ SL
                    eff_sl_candidates = [base_sl]
                    if be_active:
                        eff_sl_candidates.append(entry_px + BE_OFFSET)
                    if trail_active:
                        eff_sl_candidates.append(high_water - TRAIL_POINTS)

                    eff_sl = max(eff_sl_candidates)

                    # แจ้งเมื่อ SL ถูกยกขึ้น
                    if curr_sl is None:
                        curr_sl = base_sl  # ตั้งต้นเป็น base ก่อน
                    if eff_sl > curr_sl + 1e-9:  # มีการยก SL จริง
                        reason = 'TRAIL' if trail_active and abs(eff_sl - (high_water - TRAIL_POINTS)) < 1e-9 else 'BE'
                        notify_sl_move('long', curr_sl, eff_sl, reason)  # <-- NEW
                        curr_sl = eff_sl

                    # ปิดเมื่อชน TP หรือ SL
                    if price >= tp:
                        o = close_market('long', pos_ct)
                        notify_close('long', pos_ct, entry_px, price, contract_size, reason='TP')  # <-- NEW
                        cancel_all_open_orders()
                        in_pos = False; pos_side='flat'; pos_ct=0; entry_px=None
                        high_water=None; low_water=None; armed_side=None; curr_sl=None
                    elif price <= eff_sl:
                        o = close_market('long', pos_ct)
                        notify_close('long', pos_ct, entry_px, price, contract_size, reason='SL')  # <-- NEW
                        cancel_all_open_orders()
                        in_pos = False; pos_side='flat'; pos_ct=0; entry_px=None
                        high_water=None; low_water=None; armed_side=None; curr_sl=None

                else:  # short
                    tp = entry_px - TP_POINTS
                    base_sl = entry_px + SL_POINTS

                    # อัปเดต low_water
                    low_water = price if low_water is None else min(low_water, price)

                    # เปิดใช้งาน BE/Trail เมื่อกำไรถึงเกณฑ์
                    be_active = low_water <= entry_px - BE_OFFSET
                    trail_active = (entry_px - low_water) >= TRAIL_POINTS

                    eff_sl_candidates = [base_sl]
                    if be_active:
                        eff_sl_candidates.append(entry_px - BE_OFFSET)
                    if trail_active:
                        eff_sl_candidates.append(low_water + TRAIL_POINTS)

                    eff_sl = min(eff_sl_candidates)

                    # แจ้งเมื่อ SL ถูกลดลง (สำหรับ short การขยับ SL = ลดราคา SL ลง)
                    if curr_sl is None:
                        curr_sl = base_sl
                    if eff_sl < curr_sl - 1e-9:
                        reason = 'TRAIL' if trail_active and abs(eff_sl - (low_water + TRAIL_POINTS)) < 1e-9 else 'BE'
                        notify_sl_move('short', curr_sl, eff_sl, reason)  # <-- NEW
                        curr_sl = eff_sl

                    # ปิดเมื่อชน TP หรือ SL
                    if price <= tp:
                        o = close_market('short', pos_ct)
                        notify_close('short', pos_ct, entry_px, price, contract_size, reason='TP')  # <-- NEW
                        cancel_all_open_orders()
                        in_pos = False; pos_side='flat'; pos_ct=0; entry_px=None
                        high_water=None; low_water=None; armed_side=None; curr_sl=None
                    elif price >= eff_sl:
                        o = close_market('short', pos_ct)
                        notify_close('short', pos_ct, entry_px, price, contract_size, reason='SL')  # <-- NEW
                        cancel_all_open_orders()
                        in_pos = False; pos_side='flat'; pos_ct=0; entry_px=None
                        high_water=None; low_water=None; armed_side=None; curr_sl=None

            # เปิดออเดอร์ทันทีเมื่อ cross ตรงกับฝั่งที่ arm
            if not in_pos and armed_side:
                cross_up   = (f_prev <= s_prev) and (f_now > s_now)
                cross_down = (f_prev >= s_prev) and (f_now < s_now)
                should_open = ((armed_side == 'long' and cross_up) or
                               (armed_side == 'short' and cross_down))
                if should_open:
                    contracts = calc_contracts(price, contract_size, avail_net)
                    if contracts < 1:
                        log.warning("Margin ไม่พอเปิด 1 สัญญา")
                    else:
                        side = 'buy' if armed_side == 'long' else 'sell'
                        order = open_market(side, contracts)
                        in_pos = True
                        pos_side = 'long' if side == 'buy' else 'short'
                        pos_ct = contracts
                        entry_px = price
                        high_water = price if pos_side == 'long' else None
                        low_water  = price if pos_side == 'short' else None
                        curr_sl = None  # เริ่มใหม่ (จะตั้ง base ในลูปจัดการ)  # <-- NEW
                        notify_open(pos_side, pos_ct, entry_px)

            # พิมพ์สถานะทุก ๆ รอบตาม interval
            if LOG_EVERY_TICK:
                log_tick_status(armed_side, f_now, s_now, in_pos, pos_side, price)

        except Exception as e:
            log.error(f"Loop error: {e}")

        # sleep จุดเดียวท้ายลูป
        time.sleep(POLL_INTERVAL_SECONDS)
