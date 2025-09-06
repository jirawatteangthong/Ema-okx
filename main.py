# ================== imports ==================
import ccxt, time, requests, logging, json, os, sys, math, calendar, threading
from datetime import datetime

# ================== CONFIG (ปรับได้) ==================
# --- OKX API ---
API_KEY  = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET   = os.getenv('OKX_SECRET',    'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD',  'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')

SYMBOL_ID_ENV = 'BTC-USDT-SWAP'
# Timeframes
TIMEFRAME_H1      = '1h'
TIMEFRAME_M5      = '5m'

# Leverage / Margin
LEVERAGE          = 40
OKX_MARGIN_MODE   = 'isolated'  # 'isolated' หรือ 'cross'
TARGET_POSITION_SIZE_FACTOR = 0.8     # ใช้ % ของ Free USDT
MARGIN_BUFFER_USDT = 5                 # กันเงินไม่ใช้ทั้งหมด

# เพดาน notional ต่อแผน (ปรับได้)
MAX_NOTIONAL = 100000.0  #เพดานสูงสุด usdt

# ---- EMA/MACD Parameters ----
EMA_FAST_H1   = 10
EMA_SLOW_H1   = 50
EMA200_M5     = 200
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL   = 9

# ---- EMA accuracy / snapshot logging ----
LOOKBACK_H1_BARS = 1000
LOOKBACK_M5_BARS = 1500
DIAG_LOG_INTERVAL_SEC = 180

# ---- SL เริ่มต้นจาก Swing M5 (soft SL ในบอท) ----
SWING_LOOKBACK_M5   = 50
SL_EXTRA_POINTS     = 200.0
MAX_INITIAL_SL_POINTS = 1234

# ---- Trailing SL Steps ----
STEP1_TRIGGER   = 450.0
STEP1_SL_OFFSET = -200.0
STEP2_TRIGGER   = 700.0
STEP2_SL_OFFSET = +300.0
STEP3_TRIGGER   = 950.0
STEP3_SL_OFFSET = +750.0
MANUAL_CLOSE_ALERT_TRIGGER = 1300.0
AUTO_CLOSE_TRIGGER = 1400.0

# ---- สัญญาณ H1 ใหม่ระหว่างถือโพซิชัน (สวนทิศ) ----
NEW_SIGNAL_ACTION    = 'close_now'    # 'tighten_sl' หรือ 'close_now'
NEW_SIGNAL_SL_OFFSET = 100.0

# ---- สวิตช์ยืนยัน H1 สวนกี่แท่งปิด ----
H1_OPP_CONFIRM_BARS = 2

# ---- Snapshot logging (INFO) ----
SNAPSHOT_LOG_INTERVAL_SEC = 30

# ---- Loop/Timing ----
FAST_LOOP_SECONDS     = 3

# ---- Telegram ----
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# ---- Stats / Monthly report ----
STATS_FILE = 'trading_stats.json'
MONTHLY_REPORT_DAY    = 25
MONTHLY_REPORT_HOUR   = 0
MONTHLY_REPORT_MINUTE = 5

# ---- Debug ----
DEBUG_CALC = True

# ---- Entry Band (เติมครั้งเดียวเมื่อกลับเข้ากรอบ) ----
ENTRY_BAND_PTS = 200.0         # [entry±200]
ENTRY_BAND_STOP_EXTRA = 100.0  # ถ้าหลุดฝั่งสวนเพิ่ม 100 → หยุดเติม (long: entry-300, short: entry+300)

# ================== logging ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log', encoding='utf-8'), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)
def dbg(tag: str, **kw):
    if not DEBUG_CALC:
        return
    try:
        logger.info(f"[DBG:{tag}] " + json.dumps(kw, ensure_ascii=False, default=str))
    except Exception:
        logger.info(f"[DBG:{tag}] {kw}")

# ================== GLOBAL STATE ==================
exchange = None
market_info = None
SYMBOL_ID = None       # OKX id, e.g. 'BTC-USDT-SWAP'
SYMBOL_U  = None       # unified, e.g. 'BTC/USDT:USDT'

last_snapshot_log_ts = 0.0
last_diag_log_ts = 0.0

# Baseline H1 (จากแท่งปิด)
h1_baseline_dir = None
h1_baseline_bar_ts = None

# Position
position = None  # {'side','entry','contracts','sl','step','opened_at'}

# Entry plan (H1→M5)
entry_plan = {
    'h1_dir': None, 'h1_bar_ts': None, 'stage': 'idle',
    'm5_last_bar_ts': None, 'm5_touch_ts': None, 'macd_initial': None
}

last_manual_tp_alert_ts = 0.0

# Monthly report helpers
last_monthly_report_date = None
initial_balance = 0.0

# ตัวนับ H1 สวนทาง
h1_opp_count = 0
h1_opp_last_ts = None
h1_opp_dir = None

# ปิดโพซิชันด้วยเหตุ H1 สวน → re-arm จาก H1 ใหม่ทันที
next_plan_after_forced_close = False

# Entry Band / เติมครั้งเดียวเมื่อกลับเข้ากรอบ
fill_plan = {
    'target_notional': 0.0,
    'entry_ref': None,
    'band_low': None,
    'band_high': None,
    'add_disabled': False
}

# ================== Telegram ==================
def send_telegram(msg: str):
    if (not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith('YOUR_') or
        not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID.startswith('YOUR_')):
        logger.warning("⚠ TELEGRAM creds not set; skip send.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        requests.get(url, params=params, timeout=10).raise_for_status()
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def fmt_usd(x):
    try: return f"{float(x):,.2f}"
    except: return str(x)

# De-dup notifications
_notif_sent = {}
def send_once(tag: str, msg: str):
    if _notif_sent.get(tag): return
    send_telegram(msg); _notif_sent[tag] = True
def clear_notif(prefix: str):
    for k in list(_notif_sent.keys()):
        if k.startswith(prefix):
            _notif_sent.pop(k, None)

# ================== Exchange Setup (OKX) ==================
def setup_exchange():
    global exchange, market_info, SYMBOL_ID, SYMBOL_U
    if (not API_KEY or not SECRET or not PASSWORD or
        'YOUR_' in API_KEY or 'YOUR_' in SECRET or 'YOUR_' in PASSWORD):
        send_telegram("⛔ Critical: API key/secret/password not set."); sys.exit(1)

    exchange = ccxt.okx({
        'apiKey': API_KEY,
        'secret': SECRET,
        'password': PASSWORD,
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}
    })
    exchange.load_markets()

    # รับเป็น id จาก ENV แล้ว map เป็น unified เพื่อใช้งานทุกจุด
    m = exchange.market(SYMBOL_ID_ENV)
    market_info = m
    SYMBOL_ID = m['id']            # 'BTC-USDT-SWAP'
    SYMBOL_U  = m['symbol']        # 'BTC/USDT:USDT'

    try:
        exchange.set_leverage(LEVERAGE, SYMBOL_U, params={'mgnMode': OKX_MARGIN_MODE})
    except Exception as e:
        logger.error(f"set_leverage failed: {e}")
        send_telegram(f"⛔ set_leverage failed: {e}")

def decimal_price(v: float) -> float:
    if not market_info: return round(v, 2)
    try:
        return float(exchange.price_to_precision(SYMBOL_U, v))
    except Exception:
        return round(v, 2)

def _get_contract_size() -> float:
    try:
        cs = float((market_info or {}).get('contractSize') or 0.01)
        return cs if cs > 0 else 0.01
    except Exception:
        return 0.01

# ================== Balance Helpers (OKX) ==================
def get_free_usdt() -> float | None:
    """ดึง free USDT ในบัญชี swap (OKX)"""
    try:
        bal = exchange.fetch_balance({'type':'swap'})
    except Exception:
        try:
            bal = exchange.fetch_balance()
        except Exception:
            return None
    try:
        data = (bal.get('info', {}).get('data') or [])
        if data:
            first = data[0]
            details = first.get('details')
            if isinstance(details, list):
                for item in details:
                    if item.get('ccy') == 'USDT':
                        avail = float(item.get('availBal') or 0)
                        frozen = float(item.get('ordFrozen') or 0)
                        return max(0.0, avail - frozen)
            avail = float(first.get('availBal') or first.get('cashBal') or first.get('eq') or 0)
            frozen = float(first.get('ordFrozen') or 0)
            return max(0.0, avail - frozen)
    except Exception:
        pass

    v = (bal.get('USDT',{}) or {}).get('free', None)
    if v is not None:
        try: return float(v)
        except: pass
    for key in ('free','total'):
        v=(bal.get(key,{}) or {}).get('USDT', None)
        if v is not None:
            try: return float(v)
            except: pass
    return None

def get_portfolio_balance() -> float:
    v = get_free_usdt()
    return float(v) if v is not None else 0.0

# ================== Sizing Helpers ==================
def contracts_from_notional(price: float, notional: float) -> float:
    """แปลง notional(USDT) -> จำนวนสัญญา ตาม contractSize"""
    cs = _get_contract_size()
    if price <= 0 or cs <= 0 or notional <= 0:
        return 0.0
    cts = notional / (price * cs)
    try:
        return float(exchange.amount_to_precision(SYMBOL_U, cts))
    except Exception:
        return max(0.0, cts)

def approx_position_notional(price: float) -> float:
    """ประมาณ notional ปัจจุบัน: contracts * price * contractSize"""
    try:
        pos = fetch_position()
        if not pos: return 0.0
        cs = _get_contract_size()
        return float(pos['contracts']) * price * cs
    except Exception:
        return 0.0

# ================== Indicators (EMA = SMA-seed) ==================
def ema_series(values, period):
    n = int(period)
    if len(values) < n:
        return None
    sma = sum(values[:n]) / n
    k = 2 / (n + 1)
    out = [None] * (n - 1) + [sma]
    e = sma
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def last_ema(values, period):
    es = ema_series(values, period)
    return es[-1] if es else None

def macd_from_closes(closes):
    if len(closes) < MACD_SLOW + MACD_SIGNAL + 2: return None
    ef = ema_series(closes, MACD_FAST)
    es = ema_series(closes, MACD_SLOW)
    if not ef or not es: return None
    dif = []
    for i in range(len(closes)):
        if i >= len(ef) or i >= len(es) or ef[i] is None or es[i] is None:
            continue
        dif.append(ef[i] - es[i])
    dea = ema_series(dif, MACD_SIGNAL)
    if not dea or len(dea) < 2 or len(dif) < 2: return None
    return dif[-2], dif[-1], dea[-2], dea[-1]

def macd_cross_up(dif_prev, dif_now, dea_prev, dea_now):   return (dif_prev<=dea_prev) and (dif_now>dea_now)
def macd_cross_down(dif_prev, dif_now, dea_prev, dea_now): return (dif_prev>=dea_prev) and (dif_now<dea_now)

def find_recent_swing_low_high_m5(ohlcv_m5, lookback=SWING_LOOKBACK_M5, k=2):
    if len(ohlcv_m5) < lookback + 2*k + 1:
        look = ohlcv_m5[:]
    else:
        look = ohlcv_m5[-lookback:]
    highs=[c[2] for c in look]; lows=[c[3] for c in look]
    swing_low=swing_high=None
    for i in range(k, len(look)-k):
        if all(lows[i]<=lows[i-j] for j in range(1,k+1)) and all(lows[i]<=lows[i+j] for j in range(1,k+1)):
            swing_low = look[i][3]
        if all(highs[i]>=highs[i-j] for j in range(1,k+1)) and all(highs[i]>=highs[i+j] for j in range(1,k+1)):
            swing_high = look[i][2]
    if swing_low is None: swing_low=min(lows)
    if swing_high is None: swing_high=max(highs)
    return swing_low, swing_high

def log_indicator_snapshot():
    try:
        price_now = exchange.fetch_ticker(SYMBOL_U)['last']

        # H1 (แท่งปิด)
        limit_h1 = max(LOOKBACK_H1_BARS, EMA_SLOW_H1 + 50)
        o_h1 = exchange.fetch_ohlcv(SYMBOL_U, timeframe=TIMEFRAME_H1, limit=limit_h1)
        ema_fast_h1 = ema_slow_h1 = h1_close = h1_bar_ts = None
        h1_dir = None
        if o_h1 and len(o_h1) >= 3:
            h1_bar_ts = o_h1[-2][0]
            h1_closes = [c[4] for c in o_h1[:-1]]
            ema_fast_h1 = last_ema(h1_closes, EMA_FAST_H1)
            ema_slow_h1 = last_ema(h1_closes, EMA_SLOW_H1)
            h1_close = h1_closes[-1] if h1_closes else None
            if (ema_fast_h1 is not None) and (ema_slow_h1 is not None):
                h1_dir = 'long' if ema_fast_h1 > ema_slow_h1 else 'short' if ema_fast_h1 < ema_slow_h1 else None

        # M5 (แท่งปิด)
        limit_m5 = max(LOOKBACK_M5_BARS, EMA200_M5 + 50)
        o_m5 = exchange.fetch_ohlcv(SYMBOL_U, timeframe=TIMEFRAME_M5, limit=limit_m5)
        ema200_m5 = m5_close = m5_bar_ts = None
        macd_vals = None
        if o_m5 and len(o_m5) >= EMA200_M5 + 5:
            m5_bar_ts = o_m5[-2][0]
            m5_closes = [c[4] for c in o_m5[:-1]]
            m5_close = m5_closes[-1]
            ema200_m5 = last_ema(m5_closes, EMA200_M5)
            macd_vals = macd_from_closes(m5_closes)

        payload = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "price": price_now,
            "H1": {
                "bar_ts": h1_bar_ts, "ema_fast": ema_fast_h1,
                "ema_slow": ema_slow_h1, "close": h1_close, "dir": h1_dir
            },
            "M5": {
                "bar_ts": m5_bar_ts, "ema200": ema200_m5, "close": m5_close,
            }
        }
        if macd_vals:
            dif_p, dif_n, dea_p, dea_n = macd_vals
            payload["M5"]["macd"] = {"dif_prev": dif_p, "dif_now": dif_n, "dea_prev": dea_p, "dea_now": dea_n}
        else:
            payload["M5"]["macd"] = None

        logger.info("[SNAPSHOT] " + json.dumps(payload, ensure_ascii=False, default=str))

    except Exception as e:
        logger.error(f"snapshot log error: {e}")

def log_ema_warmup_diagnostics():
    try:
        o = exchange.fetch_ohlcv(SYMBOL_U, timeframe=TIMEFRAME_H1, limit=max(LOOKBACK_H1_BARS, 1200))
        if not o or len(o) < EMA_SLOW_H1 + 3:
            return
        closes = [c[4] for c in o[:-1]]
        packs = {"60bars":closes[-60:], "300bars":closes[-300:], "1000bars":closes[-1000:]}
        out = {k: {"ema10": last_ema(arr, 10), "ema50": last_ema(arr, 50), "close": arr[-1] if arr else None}
               for k, arr in packs.items()}
        logger.info("[EMA_WARMUP_DIAG_H1] " + json.dumps(out, ensure_ascii=False, default=str))
    except Exception as e:
        logger.error(f"ema warmup diag error: {e}")

# ================== Position/Orders (OKX style) ==================
def fetch_position():
    try:
        ps = exchange.fetch_positions([SYMBOL_U])
        for p in ps:
            sym_u  = p.get('symbol')
            instId = ((p.get('info') or {}).get('instId'))
            if (sym_u == SYMBOL_U) or (instId == SYMBOL_ID):
                contracts = abs(float(p.get('contracts',0) or 0))
                if contracts != 0:
                    return {'side':p.get('side'),
                            'contracts':contracts,
                            'entry':float(p.get('entryPrice',0) or 0)}
        return None
    except Exception as e:
        logger.error(f"fetch_position error: {e}"); return None

def cancel_all_open_orders(max_retry=3):
    for _ in range(max_retry):
        try:
            orders = exchange.fetch_open_orders(SYMBOL_U)
            if not orders: return
            for o in orders:
                try: exchange.cancel_order(o['id'], SYMBOL_U); time.sleep(0.05)
                except Exception as e: logger.warning(f"cancel warn: {e}")
        except Exception as e:
            logger.error(f"cancel_all_open_orders error: {e}"); time.sleep(0.2)

def _create_market_order_with_fallback(side_ccxt: str, qty: float, extra_params: dict):
    """ลองส่งแบบ hedged (posSide) ก่อน ถ้าไม่ผ่าน ค่อยส่งแบบ one-way"""
    params_hedged = dict(extra_params)
    # map posSide ตาม side ของ position
    if side_ccxt.lower() == 'buy':
        params_hedged['posSide'] = 'long'
    else:
        params_hedged['posSide'] = 'short'
    try:
        return exchange.create_market_order(SYMBOL_U, side_ccxt, qty, None, params_hedged)
    except Exception:
        return exchange.create_market_order(SYMBOL_U, side_ccxt, qty, None, extra_params)

def set_sl_close_position(side: str, stop_price: float):
    """Soft SL: เก็บค่าไว้ แจ้งเตือน; monitor จะ reduceOnly market เองเมื่อถึงราคา"""
    try:
        sp = decimal_price(stop_price)
        send_telegram("✅ ตั้ง SL สำเร็จ!\n"
                      f"📊 Direction: <b>{side.upper()}</b>\n"
                      f"🛡 SL: <code>{fmt_usd(sp)}</code>")
        return True
    except Exception as e:
        logger.error(f"set_sl_close_position error: {e}")
        send_telegram(f"❌ SL Error: {e}"); return False

def open_market(side: str, price_now: float):
    """เปิดไม้แรก: ยิงครั้งเดียวเต็มจำนวนที่ 'พอ' แต่ไม่เกิน MAX_NOTIONAL และตั้งกรอบสำหรับ 'เติมครั้งเดียว' ภายหลัง"""
    global position, fill_plan

    bal = get_free_usdt() or 0.0
    usable = max(0.0, (bal - MARGIN_BUFFER_USDT)) * TARGET_POSITION_SIZE_FACTOR
    affordable_notional = usable * LEVERAGE
    target_notional = min(affordable_notional, MAX_NOTIONAL)
    if target_notional <= 0:
        send_telegram("⛔ ไม่พอ margin เปิดออเดอร์"); return False

    qty = contracts_from_notional(price_now, target_notional)
    if qty <= 0:
        send_telegram("⛔ คำนวณจำนวนสัญญาไม่ได้"); return False

    side_ccxt = 'buy' if side=='long' else 'sell'
    try:
        _create_market_order_with_fallback(side_ccxt, qty, {'tdMode': OKX_MARGIN_MODE})
        time.sleep(0.6)
        pos = fetch_position()
        if not pos or pos.get('side') != side:
            send_telegram("⛔ ยืนยันโพซิชันไม่สำเร็จ"); return False

        position = {'side': side,'entry': float(pos['entry']),'contracts': float(pos['contracts']),
                    'sl': None,'step': 0,'opened_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

        entry_ref = float(position['entry'])
        fill_plan = {
            'target_notional': float(target_notional),
            'entry_ref': entry_ref,
            'band_low': float(entry_ref - ENTRY_BAND_PTS),
            'band_high': float(entry_ref + ENTRY_BAND_PTS),
            'add_disabled': False
        }

        send_telegram(
            "✅ เปิดโพซิชัน <b>{}</b>\n"
            f"📦 Size: <code>{position['contracts']:.6f}</code>\n"
            f"🎯 Entry: <code>{fmt_usd(entry_ref)}</code>\n"
            f"🧰 Cap: <code>{fmt_usd(fill_plan['target_notional'])} USDT</code>\n"
            f"📎 Band: <code>[{fmt_usd(fill_plan['band_low'])}, {fmt_usd(fill_plan['band_high'])}]</code>"
            .format(side.upper())
        )

        # SL เริ่มต้น (soft SL)
        ohlcv_m5 = exchange.fetch_ohlcv(SYMBOL_U, timeframe=TIMEFRAME_M5, limit=max(SWING_LOOKBACK_M5, 60))
        swing_low, swing_high = find_recent_swing_low_high_m5(ohlcv_m5)
        raw_sl = (swing_low - SL_EXTRA_POINTS) if side=='long' else (swing_high + SL_EXTRA_POINTS)
        sl0 = max(raw_sl, entry_ref - MAX_INITIAL_SL_POINTS) if side=='long' \
              else min(raw_sl, entry_ref + MAX_INITIAL_SL_POINTS)
        if set_sl_close_position(side, sl0):
            position['sl'] = float(sl0)

        dbg("OPEN_SET_SL0",
            side=side, entry=entry_ref, qty=position['contracts'],
            target_notional=fill_plan['target_notional'],
            band_low=fill_plan['band_low'], band_high=fill_plan['band_high'])
        return True

    except Exception as e:
        logger.error(f"open_market error (OKX): {e}")
        send_telegram(f"❌ Open order error (OKX): {e}")
        return False

def safe_close_position(reason: str = "") -> bool:
    """ปิดโพซิชันแบบปลอดภัย (OKX): ยกเลิกคำสั่งค้าง -> reduceOnly market -> ยืนยันปิด"""
    global position
    try:
        pos = fetch_position()
        if not pos:
            cancel_all_open_orders()
            position = None
            return True

        side = pos['side']
        qty  = float(pos['contracts'])
        if qty <= 0:
            cancel_all_open_orders()
            position = None
            return True

        cancel_all_open_orders()
        close_side = 'sell' if side == 'long' else 'buy'
        try:
            _create_market_order_with_fallback(close_side, qty, {'reduceOnly': True, 'tdMode': OKX_MARGIN_MODE})
        except Exception:
            _create_market_order_with_fallback(close_side, qty, {'reduceOnly': True})

        time.sleep(1.0)
        for _ in range(10):
            time.sleep(0.5)
            if not fetch_position():
                break

        if not fetch_position():
            send_telegram(f"✅ ปิดโพซิชันสำเร็จ (reduceOnly) {('— '+reason) if reason else ''}")
            position = None
            clear_notif("step:"); clear_notif("m5touch:"); clear_notif("h1cross:")
            return True
        else:
            send_telegram("⚠️ ยังตรวจพบโพซิชันค้างหลังสั่งปิด ลองใหม่อีกรอบ")
            return False
    except Exception as e:
        logger.error(f"safe_close_position error: {e}")
        send_telegram(f"⛔ ปิดโพซิชันแบบปลอดภัยล้มเหลว: {e}")
        return False

def tighten_sl_for_new_signal(side: str, price_now: float):
    if NEW_SIGNAL_ACTION == 'close_now':
        try:
            new_dir, new_ts, extra_h1 = get_h1_dir_closed()
            ok = safe_close_position(reason="H1 new opposite signal (reduceOnly)")
            if ok:
                send_telegram("⛑️ ตรวจพบสัญญาณ H1 ใหม่ → <b>ปิดโพซิชันทันที (reduceOnly)</b>")
                if new_dir:
                    global entry_plan
                    entry_plan = {
                        'h1_dir': new_dir,
                        'h1_bar_ts': new_ts,
                        'stage': 'armed',
                        'm5_last_bar_ts': None,
                        'm5_touch_ts': None,
                        'macd_initial': None
                    }
                    send_telegram(
                        f"🔄 ใช้สัญญาณ H1 ใหม่ต่อทันที → <b>{new_dir.upper()}</b>\n"
                        f"รอ M5 แตะ EMA200 + MACD เพื่อเข้าออเดอร์"
                    )
                return True
            else:
                return False
        except Exception as e:
            logger.error(f"close_now error: {e}")
            send_telegram(f"🦠 close_now error: {e}")
            return False
    else:
        new_sl = (price_now - NEW_SIGNAL_SL_OFFSET) if side=='long' else (price_now + NEW_SIGNAL_SL_OFFSET)
        ok = set_sl_close_position(side, new_sl)
        if ok:
            send_telegram("⛑️ ตรวจพบสัญญาณ H1 ใหม่ → บังคับ SL ใกล้ราคา")
        return ok

# ================== H1 (แท่งปิด) & Baseline ==================
def get_h1_dir_closed() -> tuple[str | None, int | None, dict]:
    limit = max(LOOKBACK_H1_BARS, EMA_SLOW_H1 + 50)
    o = exchange.fetch_ohlcv(SYMBOL_U, timeframe=TIMEFRAME_H1, limit=limit)
    if not o or len(o) < EMA_SLOW_H1 + 3:
        return None, None, {}
    ts = o[-2][0]
    closes = [c[4] for c in o[:-1]]
    ema_fast = last_ema(closes, EMA_FAST_H1)
    ema_slow = last_ema(closes, EMA_SLOW_H1)
    close_last = closes[-1] if closes else None
    direction = None
    if (ema_fast is not None) and (ema_slow is not None):
        direction = 'long' if ema_fast > ema_slow else 'short' if ema_fast < ema_slow else None
    extra = {'ema_fast_h1': ema_fast, 'ema_slow_h1': ema_slow, 'h1_close': close_last}
    dbg("H1_CLOSED", ts=ts, **extra, dir=direction)
    return direction, ts, extra

def _reset_h1_opp_counter():
    global h1_opp_count, h1_opp_last_ts, h1_opp_dir
    h1_opp_count = 0
    h1_opp_last_ts = None
    h1_opp_dir = None

def update_h1_opposite_counter(pos_side: str):
    global h1_opp_count, h1_opp_last_ts, h1_opp_dir
    cur_dir, h1_ts, _ = get_h1_dir_closed()
    if h1_ts is None:
        return None
    opp_dir = 'short' if pos_side == 'long' else 'long'
    if cur_dir == opp_dir:
        if h1_opp_last_ts != h1_ts:
            if h1_opp_dir == cur_dir:
                h1_opp_count += 1
            else:
                h1_opp_dir = cur_dir
                h1_opp_count = 1
            h1_opp_last_ts = h1_ts
    else:
        _reset_h1_opp_counter()
    return {'dir': cur_dir, 'ts': h1_ts, 'count': h1_opp_count}

def reset_h1_baseline():
    global h1_baseline_dir, h1_baseline_bar_ts, entry_plan
    d, ts, extra = get_h1_dir_closed()
    h1_baseline_dir, h1_baseline_bar_ts = d, ts
    entry_plan = {
        'h1_dir': None, 'h1_bar_ts': None, 'stage':'idle',
        'm5_last_bar_ts': None, 'm5_touch_ts': None, 'macd_initial': None
    }
    clear_notif("h1cross:"); clear_notif("m5touch:"); clear_notif("step:")
    _reset_h1_opp_counter()
    dbg("BASELINE_SET", baseline_dir=d, baseline_ts=ts, **(extra or {}))

# ================== Entry Logic (H1→M5) ==================
def check_m5_env():
    limit = max(LOOKBACK_M5_BARS, EMA200_M5 + 50)
    o = exchange.fetch_ohlcv(SYMBOL_U, timeframe=TIMEFRAME_M5, limit=limit)
    if not o or len(o) < EMA200_M5 + 5:
        return None
    ts = o[-2][0]
    closes = [c[4] for c in o[:-1]]
    highs  = [c[2] for c in o[:-1]]
    lows   = [c[3] for c in o[:-1]]

    close_now = closes[-1]
    ema200    = last_ema(closes, EMA200_M5)
    macd      = macd_from_closes(closes)

    if macd:
        dif_p, dif_n, dea_p, dea_n = macd
        dbg("M5_ENV", ts=ts, close=close_now, ema200=ema200,
            dif_prev=dif_p, dif_now=dif_n, dea_prev=dea_p, dea_now=dea_n)
    else:
        dbg("M5_ENV", ts=ts, close=close_now, ema200=ema200, macd=None)

    return {'ts': ts, 'close': close_now, 'high': highs[-1], 'low': lows[-1], 'ema200': ema200, 'macd': macd}

def handle_entry_logic(price_now: float):
    global entry_plan, h1_baseline_dir

    if h1_baseline_dir is None:
        reset_h1_baseline()
        return

    env = check_m5_env()
    if not env or env['ema200'] is None or env['macd'] is None:
        return

    m5_ts  = env['ts']
    close  = env['close']
    high   = env['high']
    low    = env['low']
    ema200 = env['ema200']
    dif_p, dif_n, dea_p, dea_n = env['macd']

    if entry_plan['m5_last_bar_ts'] == m5_ts:
        return
    entry_plan['m5_last_bar_ts'] = m5_ts

    cur_dir, h1_ts, extra_h1 = get_h1_dir_closed()
    dbg("H1_UPDATE_ON_M5_CLOSE", cur_dir=cur_dir, ts=h1_ts, extra=extra_h1, baseline=h1_baseline_dir)

    if entry_plan['stage'] == 'idle' or entry_plan['h1_dir'] is None:
        if (h1_baseline_dir is None) or (cur_dir is None) or (cur_dir == h1_baseline_dir):
            return
        entry_plan = {
            'h1_dir': cur_dir, 'h1_bar_ts': h1_ts, 'stage': 'armed',
            'm5_last_bar_ts': m5_ts, 'm5_touch_ts': None, 'macd_initial': None
        }
        send_once(f"h1cross:{h1_ts}:{cur_dir}",
                  f"🧭 H1 CROSS จาก baseline → <b>{cur_dir.upper()}</b>\nรอ M5 แตะ EMA200 + MACD")
    else:
        want_now = entry_plan['h1_dir']
        if (cur_dir is None):
            entry_plan = {
                'h1_dir': None, 'h1_bar_ts': None, 'stage': 'idle',
                'm5_last_bar_ts': m5_ts, 'm5_touch_ts': None, 'macd_initial': None
            }
            send_telegram("🚧 EMA H1 ไม่ชัดเจน → ยกเลิกแผนชั่วคราวและรอสัญญาณใหม่")
            return
        if cur_dir != want_now:
            entry_plan = {
                'h1_dir': cur_dir, 'h1_bar_ts': h1_ts, 'stage': 'armed',
                'm5_last_bar_ts': m5_ts, 'm5_touch_ts': None, 'macd_initial': None
            }
            send_telegram("🚧 EMA H1 เปลี่ยนสัญญาณ → ใช้สัญญาณใหม่และเริ่มหาเงื่อนไข M5 ต่อ")

    if entry_plan['stage'] == 'idle' or entry_plan['h1_dir'] is None:
        return

    want = entry_plan['h1_dir']
    plan_tag = f"{entry_plan['h1_bar_ts']}:{want}"

    if entry_plan['stage'] == 'armed':
        if want == 'long':
            touched = (low <= ema200)
            macd_initial_ok = (dif_n < dea_n)
            dbg("M5_ARMED_CHECK", want=want, low=low, ema200=ema200, dif_now=dif_n, dea_now=dea_n,
                touched=touched, macd_initial_ok=macd_initial_ok)
            if touched and macd_initial_ok:
                entry_plan.update(stage='wait_macd_cross', m5_touch_ts=m5_ts, macd_initial='buy-<')
                send_once(f"m5touch:{plan_tag}", "⏳ M5 แตะ/เลย EMA200 ลง → รอ DIF ตัดขึ้นเพื่อเข้า <b>LONG</b>")
                return
        else:
            touched = (high >= ema200)
            macd_initial_ok = (dif_n > dea_n)
            dbg("M5_ARMED_CHECK", want=want, high=high, ema200=ema200, dif_now=dif_n, dea_now=dea_n,
                touched=touched, macd_initial_ok=macd_initial_ok)
            if touched and macd_initial_ok:
                entry_plan.update(stage='wait_macd_cross', m5_touch_ts=m5_ts, macd_initial='sell->')
                send_once(f"m5touch:{plan_tag}", "⏳ M5 แตะ/เลย EMA200 ขึ้น → รอ DIF ตัดลงเพื่อเข้า <b>SHORT</b>")
                return

    elif entry_plan['stage'] == 'wait_macd_cross':
        crossed = macd_cross_up(dif_p, dif_n, dea_p, dea_n) if want == 'long' \
                  else macd_cross_down(dif_p, dif_n, dea_p, dea_n)
        dbg("M5_WAIT_MACD", want=want, crossed=crossed, dif_prev=dif_p, dif_now=dif_n, dea_prev=dea_p, dea_now=dea_n)
        if crossed:
            ok = open_market(want, price_now)
            dbg("OPEN_MARKET", side=want, ok=ok, price_now=price_now)
            entry_plan.update(stage='idle', m5_touch_ts=None, macd_initial=None)
            if not ok:
                send_telegram("⛔ เปิดออเดอร์ไม่สำเร็จ")

# ================== Fill once when back-in-band ==================
def maybe_fill_remaining(price_now: float):
    """ถ้าราคา 'กลับเข้ากรอบ' และยังไม่เต็ม cap -> เติมครั้งเดียวให้ครบ; ถ้าหลุด band stop -> หยุดเติม"""
    global fill_plan, position
    if not position: return
    if fill_plan.get('target_notional', 0.0) <= 0: return
    if fill_plan.get('add_disabled', False): return

    side = position['side']
    entry_ref = float(fill_plan['entry_ref'])
    band_low  = float(fill_plan['band_low'])
    band_high = float(fill_plan['band_high'])

    if side == 'long':
        band_stop = entry_ref - (ENTRY_BAND_PTS + ENTRY_BAND_STOP_EXTRA)
        if price_now < band_stop:
            fill_plan['add_disabled'] = True
            send_telegram(f"🧯 หยุดเติม (LONG): หลุด band stop <code>{fmt_usd(band_stop)}</code>")
            return
        if (price_now < band_low) or (price_now > band_high):
            return
    else:
        band_stop = entry_ref + (ENTRY_BAND_PTS + ENTRY_BAND_STOP_EXTRA)
        if price_now > band_stop:
            fill_plan['add_disabled'] = True
            send_telegram(f"🧯 หยุดเติม (SHORT): หลุด band stop <code>{fmt_usd(band_stop)}</code>")
            return
        if (price_now < band_low) or (price_now > band_high):
            return

    target = float(fill_plan['target_notional'])
    current_est = approx_position_notional(price_now)
    remain = max(0.0, target - current_est)
    if remain <= 1e-6:
        return

    bal = get_free_usdt() or 0.0
    usable = max(0.0, (bal - MARGIN_BUFFER_USDT)) * TARGET_POSITION_SIZE_FACTOR
    affordable = usable * LEVERAGE
    take_notional = min(remain, affordable)
    if take_notional <= 0:
        return

    qty = contracts_from_notional(price_now, take_notional)
    if qty <= 0:
        return

    side_ccxt = 'buy' if side == 'long' else 'sell'
    try:
        _create_market_order_with_fallback(side_ccxt, qty, {'tdMode': OKX_MARGIN_MODE})
        time.sleep(0.5)
        pos = fetch_position()
        if not pos:
            send_telegram("⚠️ เติมไม่สำเร็จ: ตรวจไม่เจอตำแหน่งหลังเติม"); return
        send_telegram(
            "➕ เติมโพซิชันครั้งเดียว (กลับเข้ากรอบ)\n"
            f"🧰 เติมเพิ่ม≈ <code>{fmt_usd(take_notional)} USDT</code>\n"
            f"📌 ราคาเติม≈ <code>{fmt_usd(price_now)}</code>"
        )
        fill_plan['add_disabled'] = True
    except Exception as e:
        logger.error(f"fill_remaining error: {e}")
        send_telegram(f"❌ เติมไม่สำเร็จ: {e}")

# ================== Monitoring & Trailing ==================
def monitor_position_and_trailing(price_now: float):
    global position, last_manual_tp_alert_ts, next_plan_after_forced_close

    pos_real = fetch_position()
    if not pos_real:
        cancel_all_open_orders(max_retry=3)
        if position:
            side  = position['side']
            entry = float(position['entry'])
            step  = int(position.get('step', 0))
            delta = (price_now - entry) if side=='long' else (entry - price_now)
            pnl_usdt = float(delta * position['contracts'] * _get_contract_size())
            send_telegram(
                "📊 ปิดโพซิชัน <b>{}</b>\n"
                "Entry: <code>{}</code> → Last: <code>{}</code>\n"
                "PnL: <b>{:+,.2f} USDT</b>\n"
                "🧹 เคลียร์คำสั่งเก่าแล้ว"
                .format(side.upper(), fmt_usd(entry), fmt_usd(price_now), pnl_usdt)
            )
            add_trade_close_usdt(step, pnl_usdt, side, entry, price_now, position['contracts'])
        position = None

        if next_plan_after_forced_close:
            cur_dir, h1_ts, extra_h1 = get_h1_dir_closed()
            entry_plan.update({
                'h1_dir': cur_dir, 'h1_bar_ts': h1_ts,
                'stage': 'armed' if cur_dir else 'idle',
                'm5_last_bar_ts': None, 'm5_touch_ts': None, 'macd_initial': None
            })
            send_telegram("🔁 ปิดตามสัญญาณ H1 ใหม่แล้ว → เริ่มหาเงื่อนไข M5 ต่อทันที")
            next_plan_after_forced_close = False
        else:
            reset_h1_baseline()
        return

    if position:
        position['contracts'] = float(pos_real['contracts'])
        position['entry']     = float(pos_real['entry'])

    if position:
        if H1_OPP_CONFIRM_BARS <= 1:
            h1_dir_now, _, extra_h1 = get_h1_dir_closed()
            is_opp = h1_dir_now and ((h1_dir_now=='long' and position['side']=='short') or
                                     (h1_dir_now=='short' and position['side']=='long'))
            if is_opp:
                dbg("H1_NEW_SIGNAL_WHILE_HOLD", pos_side=position['side'], h1_dir_now=h1_dir_now, extra=extra_h1)
                ok = tighten_sl_for_new_signal(position['side'], price_now)
                if ok and NEW_SIGNAL_ACTION == 'close_now':
                    globals()['next_plan_after_forced_close'] = True
                    send_telegram("⚠️ ตรวจพบ H1 สวนทาง → ปิดและจะใช้สัญญาณใหม่ต่อทันที")
        else:
            info = update_h1_opposite_counter(position['side'])
            if info and info.get('count', 0) >= H1_OPP_CONFIRM_BARS:
                dbg("H1_OPPOSITE_CONFIRMED", pos_side=position['side'], h1_dir=info['dir'],
                    count=info['count'], ts=info['ts'])
                ok = tighten_sl_for_new_signal(position['side'], price_now)
                if ok and NEW_SIGNAL_ACTION == 'close_now':
                    globals()['next_plan_after_forced_close'] = True
                    send_once(f"h1opp{H1_OPP_CONFIRM_BARS}:{position['opened_at']}",
                              f"⚠️ H1 สวนทางยืนยันครบ <b>{H1_OPP_CONFIRM_BARS} แท่งปิด</b> → ปิดและเตรียมแผนใหม่ทันที")

    if not position: return
    side, entry = position['side'], position['entry']
    pnl_pts = (price_now - entry) if side=='long' else (entry - price_now)

    if position['step'] < 1 and pnl_pts >= STEP1_TRIGGER:
        new_sl = (entry + STEP1_SL_OFFSET) if side=='long' else (entry - STEP1_SL_OFFSET)
        if set_sl_close_position(side, new_sl):
            position['sl']=new_sl; position['step']=1
            send_once(f"step:1:{position['opened_at']}", "🚦 Step1 → เลื่อน SL มา <code>{}</code>".format(fmt_usd(new_sl)))
    elif position['step'] < 2 and pnl_pts >= STEP2_TRIGGER:
        new_sl = (entry + STEP2_SL_OFFSET) if side=='long' else (entry - STEP2_SL_OFFSET)
        if set_sl_close_position(side, new_sl):
            position['sl']=new_sl; position['step']=2
            send_once(f"step:2:{position['opened_at']}", "🚦 Step2 → SL = <code>{}</code>  🤑<b>TP</b>".format(fmt_usd(new_sl)))
            add_tp_reached(2, entry, new_sl)
    elif position['step'] < 3 and pnl_pts >= STEP3_TRIGGER:
        new_sl = (entry + STEP3_SL_OFFSET) if side=='long' else (entry - STEP3_SL_OFFSET)
        if set_sl_close_position(side, new_sl):
            position['sl']=new_sl; position['step']=3
            send_once(f"step:3:{position['opened_at']}", "💶 Step3 → SL = <code>{}</code>  💵<b>TP</b>".format(fmt_usd(new_sl)))
            add_tp_reached(3, entry, new_sl)

    if pnl_pts >= AUTO_CLOSE_TRIGGER:
        tag = f"autoclose:{position['opened_at']}"
        if not _notif_sent.get(tag):
            send_once(tag, f"🛎️ Auto-Close → กำไรถึง <b>{int(AUTO_CLOSE_TRIGGER)}</b> pts: ปิดโพซิชันทันที")
            ok = safe_close_position(reason=f"auto-close {int(AUTO_CLOSE_TRIGGER)} pts")
            if not ok:
                send_telegram("⚠️ Auto-close มีปัญหา โปรดตรวจสอบ")
        return

    if pnl_pts >= MANUAL_CLOSE_ALERT_TRIGGER:
        now = time.time()
        if now - last_manual_tp_alert_ts >= 30:
            globals()['last_manual_tp_alert_ts'] = now
            send_telegram("🚨 กำไรเกินเป้าแล้ว <b>{:.0f} pts</b>\nพิจารณา <b>ปิดโพซิชัน</b> ".format(MANUAL_CLOSE_ALERT_TRIGGER))

    if position:
        maybe_fill_remaining(price_now)

# ================== Monthly Stats ==================
monthly_stats = {
    'month_year': None,
    'sl0_close': 0, 'sl1_close': 0, 'sl2_close': 0, 'sl3_close': 0,
    'tp_close': 0, 'tp_reached': 0,
    'pnl_usdt_plus': 0.0, 'pnl_usdt_minus': 0.0,
    'trades': [], 'last_report_month_year': None
}

def save_monthly_stats():
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(monthly_stats, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"save stats error: {e}")

def _ensure_month():
    this_my = datetime.now().strftime('%Y-%m')
    if monthly_stats.get('month_year') != this_my:
        monthly_stats['month_year'] = this_my
        monthly_stats.update({
            'sl0_close':0,'sl1_close':0,'sl2_close':0,'sl3_close':0,
            'tp_close':0,'tp_reached':0,
            'pnl_usdt_plus':0.0,'pnl_usdt_minus':0.0,'trades':[]
        })
        save_monthly_stats()

def add_trade_close_usdt(close_step: int, pnl_usdt: float, side: str, entry: float, last: float, qty: float):
    _ensure_month()
    step_key = f"sl{max(0, min(3, int(close_step)))}_close"
    monthly_stats[step_key] += 1
    if pnl_usdt >= 0:
        monthly_stats['pnl_usdt_plus']  += float(pnl_usdt)
    else:
        monthly_stats['pnl_usdt_minus'] += float(pnl_usdt)
    if close_step >= 2 and pnl_usdt >= 0:
        monthly_stats['tp_close'] += 1
    monthly_stats['trades'].append({
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'side': side, 'entry': entry, 'close': last,
        'qty': qty, 'close_step': close_step,
        'pnl_usdt': pnl_usdt
    })
    save_monthly_stats()

def add_tp_reached(step: int, entry: float, sl_new: float):
    if step not in (2,3): return
    _ensure_month()
    monthly_stats['tp_reached'] += 1
    monthly_stats['trades'].append({
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'event': f'tp_step_{step}', 'entry': entry, 'sl_now': sl_new
    })
    save_monthly_stats()

# ================== Monthly Report (Telegram) ==================
def monthly_report():
    global last_monthly_report_date, monthly_stats, initial_balance
    now = datetime.now()
    current_month_year = now.strftime('%Y-%m')

    if last_monthly_report_date and last_monthly_report_date.year == now.year and last_monthly_report_date.month == now.month:
        return

    report_day_of_month = min(MONTHLY_REPORT_DAY, calendar.monthrange(now.year, now.month)[1])
    if not (now.day == report_day_of_month and now.hour == MONTHLY_REPORT_HOUR and now.minute == MONTHLY_REPORT_MINUTE):
        return

    try:
        balance = get_portfolio_balance()
        _ensure_month()
        ms = monthly_stats
        pnl_plus  = float(ms['pnl_usdt_plus'])
        pnl_minus = float(ms['pnl_usdt_minus'])
        pnl_net   = pnl_plus + pnl_minus
        pnl_from_start = balance - initial_balance if initial_balance > 0 else pnl_net

        message = (
            f"📊 <b>รายงานสรุปผลประจำเดือน - {now.strftime('%B %Y')}</b>\n"
            f"<b>🔹 ปิดชน SL0:</b> <code>{ms['sl0_close']}</code> ครั้ง\n"
            f"<b>🔹 ปิดชน SL1:</b> <code>{ms['sl1_close']}</code> ครั้ง\n"
            f"<b>🔹 ปิดชน SL2:</b> <code>{ms['sl2_close']}</code> ครั้ง\n"
            f"<b>🔹 ปิดชน SL3:</b> <code>{ms['sl3_close']}</code> ครั้ง\n"
            f"<b>🎯 ปิดแบบ TP (step≥2, กำไร):</b> <code>{ms['tp_close']}</code> ครั้ง\n"
            f"<b>🎯 แตะ TP (ระหว่างถือ):</b> <code>{ms['tp_reached']}</code> ครั้ง\n"
            f"<b>💚 ยอดบวก:</b> <code>{pnl_plus:,.2f} USDT</code>\n"
            f"<b>❤️ ยอดลบ:</b> <code>{pnl_minus:,.2f} USDT</code>\n"
            f"<b>Σ กำไรสุทธิเดือนนี้:</b> <code>{pnl_net:+,.2f} USDT</code>\n"
            f"<b>💼 คงเหลือปัจจุบัน:</b> <code>{balance:,.2f} USDT</code>\n"
            f"<b>↔︎ จากยอดเริ่มต้น:</b> <code>{pnl_from_start:+,.2f} USDT</code>\n"
            f"<b>⏱ บอทยังทำงานปกติ</b> ✅\n"
            f"<b>เวลา:</b> <code>{now.strftime('%H:%M')}</code>"
        )
        send_telegram(message)
        last_monthly_report_date = now.date()
        monthly_stats['last_report_month_year'] = current_month_year
        save_monthly_stats()
        logger.info("✅ ส่งรายงานประจำเดือนแล้ว.")
    except Exception as e:
        logger.error(f"❌ monthly report error: {e}", exc_info=True)
        send_telegram(f"⛔️ Error: ไม่สามารถส่งรายงานประจำเดือนได้\nรายละเอียด: {e}")

def monthly_report_scheduler():
    logger.info("⏰ เริ่ม Monthly Report Scheduler.")
    while True:
        try:
            monthly_report()
        except Exception as e:
            logger.error(f"monthly_report scheduler error: {e}")
        time.sleep(60)

# ================== Startup Banner ==================
def send_startup_banner():
    try:
        bal = get_portfolio_balance()
        bal_txt = fmt_usd(bal) if (bal is not None) else "—"
        send_telegram(
            "🤖 บอทเริ่มทำงาน 💰\n"
            f"🪙 Exchange: OKX ({OKX_MARGIN_MODE}, {LEVERAGE}x)\n"
            f"💵 ยอดเริ่มต้น: {bal_txt} USDT\n"
            f"📊 H1 EMA: {EMA_FAST_H1}/{EMA_SLOW_H1}\n"
            f"🧠 M5 : {EMA200_M5} | MACD: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}\n"
            f"🛡 SL เริ่มต้นจาก Swing{SWING_LOOKBACK_M5} แท่ง ±{int(SL_EXTRA_POINTS)} pts (≤ {int(MAX_INITIAL_SL_POINTS)} pts)\n"
            f"🚦 Step1: +{int(STEP1_TRIGGER)} → SL {int(STEP1_SL_OFFSET)} pts\n"
            f"🚦 Step2: +{int(STEP2_TRIGGER)} → SL +{int(STEP2_SL_OFFSET)} pts (TP)\n"
            f"🎯 Step3: +{int(STEP3_TRIGGER)} → SL +{int(STEP3_SL_OFFSET)} pts (TP)\n"
            f"📎 Cap (MAX_NOTIONAL): {int(MAX_NOTIONAL)} USDT | Band: ±{int(ENTRY_BAND_PTS)} (+{int(ENTRY_BAND_STOP_EXTRA)} stop)"
        )
    except Exception as e:
        logger.error(f"banner error: {e}")

# ================== main ==================
def main():
    global initial_balance
    setup_exchange()
    initial_balance = get_portfolio_balance() or 0.0
    send_startup_banner()
    reset_h1_baseline()

    threading.Thread(target=monthly_report_scheduler, daemon=True).start()

    while True:
        try:
            price_now = exchange.fetch_ticker(SYMBOL_U)['last']
            if position:
                monitor_position_and_trailing(price_now)
            else:
                handle_entry_logic(price_now)

            global last_snapshot_log_ts, last_diag_log_ts
            now_ts = time.time()
            if now_ts - last_snapshot_log_ts >= SNAPSHOT_LOG_INTERVAL_SEC:
                last_snapshot_log_ts = now_ts
                log_indicator_snapshot()
            if now_ts - last_diag_log_ts >= DIAG_LOG_INTERVAL_SEC:
                last_diag_log_ts = now_ts
                log_ema_warmup_diagnostics()

            time.sleep(FAST_LOOP_SECONDS)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"main loop error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
