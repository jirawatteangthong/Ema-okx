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

# EMA settings (ปรับได้)
TFM = os.getenv('TFM', '15m')
EMA_FAST = int(os.getenv('EMA_FAST', '50'))
EMA_SLOW = int(os.getenv('EMA_SLOW', '200'))

# Risk/Sizing (ล็อค)
PORTFOLIO_PERCENTAGE = 0.80        # ใช้ 80% ของพอร์ต
LEVERAGE = 40
MARGIN_MODE = 'isolated'
FEE_RATE_TAKER = 0.001
FIXED_BUFFER_USDT = 2.0            # กันเศษเล็กน้อย

# TP/SL/Trailing (ปรับได้)
TP_POINTS = float(os.getenv('TP_POINTS', '300'))
SL_POINTS = float(os.getenv('SL_POINTS', '500'))
TRAIL_POINTS = float(os.getenv('TRAIL_POINTS', '200'))
BE_OFFSET = float(os.getenv('BE_OFFSET', '50'))

# Loop interval
POLL_INTERVAL_SECONDS = float(os.getenv('POLL_INTERVAL_SECONDS', '3'))

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID', '')

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
        limit = max(EMA_SLOW + 5, 60)
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

def notify_close(side, contracts, entry_px, exit_px):
    pnl = (exit_px - entry_px) if side=='long' else (entry_px - exit_px)
    txt = f"✅ CLOSE {side.upper()} {contracts}\nentry≈{entry_px} exit≈{exit_px}\nPnL/contract≈{pnl:.2f}"
    log.info(txt); tg(txt)

# ================== MAIN (Armed Cross Logic + Trailing) ==================
if __name__ == "__main__":
    tg("🤖 เริ่มบอท EMA okx")
    set_isolated_leverage()
    cancel_all_open_orders()
    contract_size = get_contract_size()

    # สถานะภายใน
    in_pos = False
    pos_side = 'flat'
    pos_ct = 0
    entry_px = None
    high_water = None  # สำหรับ long
    low_water = None   # สำหรับ short

    # ขั้นตอนที่ 1: ตอนรันบอท "เช็คว่า EMA อยู่ฝั่งไหน" เพื่อ arm
    # - ถ้า fast < slow → เตรียมเปิดเฉพาะ long (รอ cross up)
    # - ถ้า fast > slow → เตรียมเปิดเฉพาะ short (รอ cross down)
    armed_side = None   # 'long' หรือ 'short'

    while True:
        try:
            f_prev, s_prev, f_now, s_now = fetch_ema_set()
            if None in (f_prev, s_prev, f_now, s_now):
                time.sleep(POLL_INTERVAL_SECONDS); continue

            price = get_price()
            avail_net = get_avail_net_usdt()

            # อัปเดตฝั่งที่ arm ถ้ายังไม่กำหนด หรือเพิ่งปิดโพซิชันไป
            if armed_side is None and not in_pos:
                armed_side = 'long' if f_now < s_now else 'short'
                log.info(f"🎯 Armed side: {armed_side.upper()} (fast={f_now:.2f}, slow={s_now:.2f})")

            # ถ้ามีโพซิชันอยู่ → จัดการ TP/SL/Trailing
            if in_pos:
                if pos_side == 'long':
                    tp = entry_px + TP_POINTS
                    base_sl = entry_px - SL_POINTS
                    high_water = price if high_water is None else max(high_water, price)
                    trail_sl = max(entry_px + BE_OFFSET, high_water - TRAIL_POINTS)
                    eff_sl = max(base_sl, trail_sl)
                    if price >= tp or price <= eff_sl:
                        side_to_close = 'long'
                        o = close_market(side_to_close, pos_ct)
                        notify_close(side_to_close, pos_ct, entry_px, price)
                        cancel_all_open_orders()
                        # reset state + กลับไปขั้นตอน 1 (arm ใหม่)
                        in_pos = False; pos_side='flat'; pos_ct=0; entry_px=None; high_water=None; low_water=None
                        armed_side = None  # บังคับให้เช็คฝั่งใหม่
                else:  # short
                    tp = entry_px - TP_POINTS
                    base_sl = entry_px + SL_POINTS
                    low_water = price if low_water is None else min(low_water, price)
                    trail_sl = min(entry_px - BE_OFFSET, low_water + TRAIL_POINTS)
                    eff_sl = min(base_sl, trail_sl)
                    if price <= tp or price >= eff_sl:
                        side_to_close = 'short'
                        o = close_market(side_to_close, pos_ct)
                        notify_close(side_to_close, pos_ct, entry_px, price)
                        cancel_all_open_orders()
                        in_pos = False; pos_side='flat'; pos_ct=0; entry_px=None; high_water=None; low_water=None
                        armed_side = None  # บังคับให้เช็คฝั่งใหม่

            # ถ้าไม่มีโพซิชันอยู่ → รอสัญญาณ cross "ตามฝั่งที่ arm ไว้" เท่านั้น
            if not in_pos and armed_side:
                cross_up   = (f_prev <= s_prev) and (f_now > s_now)
                cross_down = (f_prev >= s_prev) and (f_now < s_now)

                should_open = (
                    (armed_side == 'long'  and cross_up) or
                    (armed_side == 'short' and cross_down)
                )

                if should_open:
                    contracts = calc_contracts(price, contract_size, avail_net)
                    if contracts < 1:
                        log.warning("Margin ไม่พอเปิด 1 สัญญา");  time.sleep(POLL_INTERVAL_SECONDS); continue
                    side = 'buy' if armed_side == 'long' else 'sell'
                    order = open_market(side, contracts)
                    in_pos = True
                    pos_side = 'long' if side == 'buy' else 'short'
                    pos_ct = contracts
                    entry_px = price
                    high_water = price if pos_side == 'long' else None
                    low_water  = price if pos_side == 'short' else None
                    notify_open(pos_side, pos_ct, entry_px)
                    # หลังเปิด ห้ามเปิดเพิ่ม จนกว่าจะปิดและ arm ใหม่

            time.sleep(POLL_INTERVAL_SECONDS)

        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
