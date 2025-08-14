import os
import ccxt
import math
import logging
import time

# ---------------- CONFIG ----------------
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')
SYMBOL = 'BTC-USDT-SWAP'

PORTFOLIO_PERCENTAGE = 0.80
LEVERAGE = 15
SAFETY_PCT = float(os.getenv('SAFETY_PCT', '0.8'))
FIXED_BUFFER_USDT = float(os.getenv('FIXED_BUFFER_USDT', '3.0'))
FEE_RATE_TAKER = float(os.getenv('FEE_RATE_TAKER', '0.0005'))
RETRY_STEP  = float(os.getenv('RETRY_STEP', '0.85'))
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '6'))

# ---------------- LOGGER ----------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger()

# ---------------- INIT EXCHANGE ----------------
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True
})
exchange.options['defaultType'] = 'swap'

# ---------------- FUNCTIONS ----------------
def get_margin_channels():
    try:
        bal = exchange.fetch_balance({'type':'swap'})
        data = (bal.get('info', {}).get('data') or [])
        if not data:
            return 0.0, 0.0, 0.0, 0.0
        first = data[0]
        details = first.get('details')
        if isinstance(details, list):
            for item in details:
                if item.get('ccy') == 'USDT':
                    avail = float(item.get('availBal') or 0)
                    ord_frozen = float(item.get('ordFrozen') or 0)
                    imr = float(item.get('imr') or 0)
                    mmr = float(item.get('mmr') or 0)
                    return avail, ord_frozen, imr, mmr
        avail = float(first.get('availBal') or first.get('cashBal') or first.get('eq') or 0)
        ord_frozen = float(first.get('ordFrozen') or 0)
        imr = float(first.get('imr') or 0)
        mmr = float(first.get('mmr') or 0)
        return avail, ord_frozen, imr, mmr
    except Exception as e:
        logger.error(f"❌ get_margin_channels error: {e}")
        return 0.0, 0.0, 0.0, 0.0

def get_current_price():
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price = float(ticker['last'])
        logger.debug(f"📈 Current Price {SYMBOL}: {price}")
        return price
    except Exception as e:
        logger.error(f"❌ ดึงราคาไม่ได้: {e}")
        return 0.0

def get_contract_size(symbol):
    try:
        markets = exchange.load_markets()
        market = markets.get(symbol)
        if market and 'contractSize' in market:
            cs = float(market['contractSize'])
            if cs > 0:
                return cs
        logger.warning(f"⚠️ contractSize ที่ได้ {cs} ผิดปกติ ใช้ค่า fallback = 0.0001")
        return 0.0001
    except Exception as e:
        logger.error(f"❌ ดึง contractSize ไม่ได้: {e}")
        return 0.0001

def calc_contracts_by_margin(avail_usdt: float, price: float, contract_size: float) -> int:
    if price <= 0 or contract_size <= 0:
        return 0
    effective_avail = max(0.0, avail_usdt - FIXED_BUFFER_USDT)
    usable_cash = effective_avail * PORTFOLIO_PERCENTAGE * SAFETY_PCT
    notional_per_ct = price * contract_size
    im_per_ct = notional_per_ct / LEVERAGE
    fee_per_ct = notional_per_ct * FEE_RATE_TAKER
    need_per_ct = im_per_ct + fee_per_ct
    if need_per_ct <= 0:
        return 0
    max_ct = math.floor(usable_cash / need_per_ct)
    logger.debug(
        f"🧮 Sizing by margin | avail={avail_usdt:.4f}, eff_avail={effective_avail:.4f}, usable={usable_cash:.4f}, "
        f"notional_ct={notional_per_ct:.4f}, im_ct={im_per_ct:.4f}, fee_ct={fee_per_ct:.6f}, need_ct={need_per_ct:.4f}, "
        f"max_ct={max_ct}"
    )
    return max_ct

def set_leverage(leverage: int):
    try:
        res = exchange.set_leverage(leverage, SYMBOL, params={'mgnMode': 'cross'})
        logger.info(f"🔧 ตั้ง Leverage {leverage}x สำเร็จ: {res}")
    except Exception as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้: {e}")

def get_position_mode():
    try:
        resp = exchange.private_get_account_config()
        pos_mode = resp['data'][0]['posMode']
        logger.info(f"📌 Position Mode: {pos_mode}")
        return pos_mode
    except Exception as e:
        logger.error(f"❌ ดึง Position Mode ไม่ได้: {e}")
        return 'net'

def open_long(contracts: int, pos_mode: str):
    if contracts <= 0:
        logger.warning("⚠️ Contracts <= 0 ไม่เปิดออเดอร์")
        return
    params = {'tdMode': 'cross', 'ordType': 'market'}
    if pos_mode == 'long_short_mode':
        params['posSide'] = 'long'
    for attempt in range(MAX_RETRIES):
        try:
            order = exchange.create_order(SYMBOL, 'market', 'buy', contracts, None, params)
            logger.info(f"✅ เปิด Long สำเร็จ: {order}")
            return
        except ccxt.BaseError as e:
            err_msg = str(e)
            logger.error(f"❌ เปิด Long ไม่ได้: {err_msg}")
            if '51000' in err_msg and pos_mode != 'long_short_mode':
                logger.warning("↻ ลองใหม่แบบไม่ใส่ posSide (บัญชีอยู่ใน One-way/Net mode)")
                params.pop('posSide', None)
                continue
            elif '51008' in err_msg and contracts > 1:
                new_ct = max(1, math.floor(contracts * RETRY_STEP))
                logger.warning(f"↻ ลดสัญญาแล้วลองใหม่: {contracts} → {new_ct}")
                contracts = new_ct
                time.sleep(1)
                continue
            else:
                break

# ---------------- MAIN ----------------
if __name__ == "__main__":
    set_leverage(LEVERAGE)
    pos_mode = get_position_mode()
    avail, ord_frozen, imr, mmr = get_margin_channels()
    logger.info(f"🔍 Margin channels | avail={avail:.4f} | ordFrozen={ord_frozen:.4f} | imr={imr:.4f} | mmr={mmr:.4f}")
    price = get_current_price()
    contract_size = get_contract_size(SYMBOL)
    logger.info(f"🫙 สรุปสถานะ | avail={avail:.4f} USDT | price={price} | contractSize={contract_size}")
    contracts = calc_contracts_by_margin(avail, price, contract_size)
    open_long(contracts, pos_mode)
