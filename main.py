import os
import time
import math
import logging
import requests
import ccxt
from datetime import datetime
from pathlib import Path
import csv

# ================== CONFIG ==================
API_KEY  = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET   = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')

SYMBOL = 'BTC-USDT-SWAP'

# ===== EMA SETTINGS =====
TFM = '15m'
EMA_FAST = 50
EMA_SLOW = 200

# ===== RISK / SIZE =====
PORTFOLIO_PERCENTAGE = 0.80
LEVERAGE = 40
MARGIN_MODE = 'isolated'
FEE_RATE_TAKER = 0.001
FIXED_BUFFER_USDT = 2.0

# ===== TP / SL (3 STEP) =====
TP_POINTS = 700.0

SL_STEP1_TRIGGER_LONG  = 200.0
SL_STEP1_NEW_SL_LONG   = -900.0
SL_STEP1_TRIGGER_SHORT = 200.0
SL_STEP1_NEW_SL_SHORT  = 900.0

SL_STEP2_TRIGGER_LONG  = 350.0
SL_STEP2_NEW_SL_LONG   = -400.0
SL_STEP2_TRIGGER_SHORT = 350.0
SL_STEP2_NEW_SL_SHORT  = 400.0

SL_STEP3_TRIGGER_LONG  = 510.0
SL_STEP3_NEW_SL_LONG   = 460.0
SL_STEP3_TRIGGER_SHORT = 510.0
SL_STEP3_NEW_SL_SHORT  = -460.0

MANUAL_TP_ALERT_POINTS = 1000.0
MANUAL_TP_ALERT_INTERVAL_SEC = 600

# ===== LOOP INTERVAL =====
POLL_INTERVAL_SECONDS = 3

# ===== TELEGRAM =====
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID', '')

# ===== MONTHLY STATS =====
STATS_FILE = Path('okx_monthly_stats.csv')

# ================== LOGGER ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger('ema-bot')

# ================== EXCHANGE ==================
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

# ================== TELEGRAM ==================
def tg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
    except: pass

def _fmt_num(x, digits=2):
    try: return f"{float(x):,.{digits}f}"
    except: return str(x)

def notify_open(side, contracts, entry_px):
    tg(f"🎯 เปิดโพซิชัน {side.upper()} | {contracts} contracts | entry≈{_fmt_num(entry_px,2)}")

def notify_set_sl(side, sl, ct):
    tg(f"✅ ตั้ง SL {side.upper()} | SL≈{_fmt_num(sl,2)} | size={ct}")

def notify_close(side, contracts, entry_px, exit_px, contract_size, reason):
    pnl_per_ct = (exit_px-entry_px) if side=='long' else (entry_px-exit_px)
    pnl_total = pnl_per_ct * contract_size * contracts
    tg(f"✅ CLOSE {side.upper()} {contracts} | {reason}\nEntry={entry_px:.2f} | Exit={exit_px:.2f}\nPnL={pnl_total:.2f} USDT")

# ================== HELPERS ==================
def set_leverage():
    try: exchange.set_leverage(LEVERAGE, SYMBOL, params={'mgnMode': MARGIN_MODE})
    except: pass

def cancel_all_orders():
    try:
        for o in exchange.fetch_open_orders(SYMBOL):
            exchange.cancel_order(o['id'], SYMBOL)
    except: pass

def get_price(): return float(exchange.fetch_ticker(SYMBOL)['last'])
def get_contract_size():
    try: return float(exchange.load_markets()[SYMBOL]['contractSize'])
    except: return 0.01

def get_avail_usdt():
    try:
        bal = exchange.fetch_balance({'type':'swap'})
        for d in bal['info']['data'][0]['details']:
            if d['ccy']=='USDT': return float(d['availBal'])
    except: return 0.0
    return 0.0

def calc_contracts(px, cs, avail):
    usable = max(0, avail-FIXED_BUFFER_USDT)*PORTFOLIO_PERCENTAGE
    notional = px*cs
    im = notional/LEVERAGE
    fee = notional*FEE_RATE_TAKER
    need = im+fee
    return max(int(math.floor(usable/need)),0)

def fetch_ema_set():
    limit = max(EMA_SLOW+5, 400)
    closes = [c[4] for c in exchange.fetch_ohlcv(SYMBOL, timeframe=TFM, limit=limit)]
    def ema(vals,n):
        k=2/(n+1); e=vals[0]
        for v in vals[1:]: e=v*k+e*(1-k)
        return e
    return ema(closes[:-1],EMA_FAST), ema(closes[:-1],EMA_SLOW), ema(closes,EMA_FAST), ema(closes,EMA_SLOW)

def open_market(side, ct): return exchange.create_order(SYMBOL,'market',side,ct,None,{'tdMode':MARGIN_MODE})
def close_market(cur_side, ct):
    side='sell' if cur_side=='long' else 'buy'
    return exchange.create_order(SYMBOL,'market',side,ct,None,{'tdMode':MARGIN_MODE,'reduceOnly':True})

# ================== MAIN ==================
if __name__=="__main__":
    set_leverage(); cancel_all_orders()
    cs = get_contract_size()

    in_pos=False; pos_side='flat'; pos_ct=0; entry_px=None
    curr_sl=None; sl_step=0; last_cross_dir=None

    while True:
        try:
            f_prev,s_prev,f_now,s_now = fetch_ema_set()
            px = get_price(); avail=get_avail_usdt()

            # RESET last_cross_dir หลังปิดโพซิชัน
            if not in_pos:
                if f_now>s_now: last_cross_dir='long'
                elif f_now<s_now: last_cross_dir='short'
                else: last_cross_dir=None

            # MANAGE POSITION
            if in_pos:
                if pos_side=='long':
                    tp=entry_px+TP_POINTS
                    pnl=px-entry_px
                    desired_sl=entry_px-abs(SL_STEP1_NEW_SL_LONG)
                    if pnl>=SL_STEP1_TRIGGER_LONG: desired_sl=entry_px+SL_STEP1_NEW_SL_LONG; sl_step=1
                    if pnl>=SL_STEP2_TRIGGER_LONG: desired_sl=entry_px+SL_STEP2_NEW_SL_LONG; sl_step=2
                    if pnl>=SL_STEP3_TRIGGER_LONG: desired_sl=entry_px+SL_STEP3_NEW_SL_LONG; sl_step=3
                    if curr_sl is None: curr_sl=desired_sl; notify_set_sl('long',curr_sl,pos_ct)
                    if desired_sl>curr_sl: curr_sl=desired_sl; notify_set_sl('long',curr_sl,pos_ct)
                    if px>=tp or px<=curr_sl:
                        reason='TP' if px>=tp or sl_step==3 else 'SL'
                        close_market('long',pos_ct); notify_close('long',pos_ct,entry_px,px,cs,reason)
                        cancel_all_orders(); in_pos=False; pos_side='flat'; pos_ct=0; entry_px=None; curr_sl=None; sl_step=0

                else: # short
                    tp=entry_px-TP_POINTS
                    pnl=entry_px-px
                    desired_sl=entry_px+abs(SL_STEP1_NEW_SL_SHORT)
                    if pnl>=SL_STEP1_TRIGGER_SHORT: desired_sl=entry_px+SL_STEP1_NEW_SL_SHORT; sl_step=1
                    if pnl>=SL_STEP2_TRIGGER_SHORT: desired_sl=entry_px+SL_STEP2_NEW_SL_SHORT; sl_step=2
                    if pnl>=SL_STEP3_TRIGGER_SHORT: desired_sl=entry_px+SL_STEP3_NEW_SL_SHORT; sl_step=3
                    if curr_sl is None: curr_sl=desired_sl; notify_set_sl('short',curr_sl,pos_ct)
                    if desired_sl<curr_sl: curr_sl=desired_sl; notify_set_sl('short',curr_sl,pos_ct)
                    if px<=tp or px>=curr_sl:
                        reason='TP' if px<=tp or sl_step==3 else 'SL'
                        close_market('short',pos_ct); notify_close('short',pos_ct,entry_px,px,cs,reason)
                        cancel_all_orders(); in_pos=False; pos_side='flat'; pos_ct=0; entry_px=None; curr_sl=None; sl_step=0

            # ENTRY LOGIC
            if not in_pos and last_cross_dir:
                cross_up=(f_prev<=s_prev and f_now>s_now)
                cross_down=(f_prev>=s_prev and f_now<s_now)
                open_dir=None
                if cross_up and last_cross_dir!='long': open_dir='long'
                elif cross_down and last_cross_dir!='short': open_dir='short'
                if open_dir:
                    ct=calc_contracts(px,cs,avail)
                    if ct>=1:
                        side='buy' if open_dir=='long' else 'sell'
                        open_market(side,ct)
                        in_pos=True; pos_side=open_dir; pos_ct=ct; entry_px=px
                        curr_sl=None; sl_step=0
                        notify_open(open_dir,pos_ct,entry_px)
                        last_cross_dir=open_dir

            log.info(f"📊 in_pos={in_pos} side={pos_side} f={f_now:.2f} s={s_now:.2f} px={px:.2f}")

        except Exception as e:
            log.error(f"Loop error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)
