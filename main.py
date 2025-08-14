import os
import sys
import time
import math
import ccxt
import logging
import traceback

# ---------- CONFIG (ENV) ----------
API_KEY   = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET    = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD  = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')

SYMBOL     = os.getenv('SYMBOL', 'BTC/USDT:USDT')  # unified symbol สำหรับ ccxt
SYMBOL_ID  = os.getenv('SYMBOL_ID', 'BTC-USDT-SWAP')  # instId ของ OKX API
LEVERAGE   = int(os.getenv('LEVERAGE', '15'))
PORTFOLIO_PERCENTAGE = float(os.getenv('PORTFOLIO_PERCENTAGE', '0.80'))
MIN_CONTRACTS = int(os.getenv('MIN_CONTRACTS', '1'))
LOOP_SLEEP_SECONDS = int(os.getenv('LOOP_SLEEP_SECONDS', '30'))

# ---------- LOGGER ----------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("okx-railway-bot")

# ---------- EXCHANGE ----------
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})
exchange.verbose = False  # 🔒 กัน API Key หลุดใน log

# ---------- FUNCTIONS ----------
def validate_api_and_resolve_market():
    try:
        if (not API_KEY or not SECRET or not PASSWORD or
            'YOUR_OKX_API_KEY_HERE' in API_KEY or
            'YOUR_OKX_SECRET_HERE' in SECRET or
            'YOUR_OKX_PASSWORD_HERE' in PASSWORD):
            logger.error("❌ ยังไม่ได้ตั้งค่า ENV: OKX_API_KEY / OKX_SECRET / OKX_PASSWORD")
            sys.exit(1)

        markets = exchange.load_markets()
        market = None
        if SYMBOL in markets:
            market = markets[SYMBOL]
        elif SYMBOL_ID and hasattr(exchange, 'markets_by_id') and SYMBOL_ID in exchange.markets_by_id:
            market = exchange.markets_by_id[SYMBOL_ID]
        else:
            raise ccxt.BadSymbol(f"ไม่พบสัญลักษณ์ (unified='{SYMBOL}', id='{SYMBOL_ID}')")

        logger.info(f"✅ ใช้ตลาด: {market['symbol']} (id={market['id']})")
        return market
    except ccxt.AuthenticationError as e:
        logger.error(f"❌ AuthenticationError: API Key/Secret/Passphrase ไม่ถูกต้องหรือสิทธิ์ไม่พอ | {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ ผิดพลาดตอน validate/load_markets: {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)

def get_available_margin_usdt():
    try:
        bal = exchange.fetch_balance({'type': 'swap'})
        data = (bal.get('info', {}).get('data') or [])
        if not data:
            return 0.0
        first = data[0]
        details = first.get('details')
        if isinstance(details, list):
            for item in details:
                if item.get('ccy') == 'USDT':
                    raw = item.get('availBal') or item.get('cashBal') or item.get('eq') or "0"
                    return float(raw) if str(raw).strip() else 0.0
        for key in ['availBal', 'cashBal', 'crossEq', 'availEq', 'eq']:
            if key in first and str(first.get(key)).strip():
                return float(first.get(key))
        return 0.0
    except Exception as e:
        logger.error(f"❌ ดึง available margin ไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0.0

def get_price(symbol):
    try:
        t = exchange.fetch_ticker(symbol)
        return float(t['last'])
    except Exception as e:
        logger.error(f"❌ ดึงราคาไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0.0

def get_contract_size(market):
    try:
        size = float(market.get('contractSize') or 0.0)
        if size > 0.001 or size <= 0:  # BTC perp มาตรฐาน 0.0001
            logger.warning(f"⚠️ contractSize ที่ได้ {size} ผิดปกติ ใช้ค่า fallback = 0.0001")
            size = 0.0001
        return size
    except:
        logger.warning("⚠️ ดึง contractSize ไม่ได้ ใช้ fallback = 0.0001")
        return 0.0001

def set_cross_leverage(market, leverage):
    try:
        res = exchange.set_leverage(leverage, market['symbol'], params={'mgnMode': 'cross'})
        logger.info(f"🔧 ตั้ง Leverage {leverage}x สำเร็จ: {res}")
    except Exception as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้: {e}")
        logger.debug(traceback.format_exc())

def calc_contracts(avail_usdt, price, contract_size):
    if price <= 0 or contract_size <= 0:
        return 0
    target_notional = avail_usdt * PORTFOLIO_PERCENTAGE * LEVERAGE
    target_btc = target_notional / price
    contracts = math.floor(target_btc / contract_size)
    logger.debug(f"📊 Calc: avail={avail_usdt} price={price} size={contract_size} contracts={contracts}")
    return contracts

def open_long(market, contracts):
    if contracts < MIN_CONTRACTS:
        logger.warning(f"⚠️ Contracts < {MIN_CONTRACTS} ไม่เปิดออเดอร์")
        return
    params = {'tdMode': 'cross', 'posSide': 'long'}
    try:
        order = exchange.create_order(market['symbol'], 'market', 'buy', contracts, None, params)
        logger.info(f"✅ เปิด Long สำเร็จ: {order}")
    except ccxt.InvalidOrder as e:
        if 'posSide' in str(e) or 'hedge' in str(e).lower():
            logger.warning("↻ ลองใหม่แบบไม่ใส่ posSide")
            order = exchange.create_order(market['symbol'], 'market', 'buy', contracts, None, {'tdMode': 'cross'})
            logger.info(f"✅ เปิด Long สำเร็จ (fallback): {order}")
        else:
            logger.error(f"❌ InvalidOrder: {e}")
            logger.debug(traceback.format_exc())
    except Exception as e:
        logger.error(f"❌ เปิด Long ไม่ได้: {e}")
        logger.debug(traceback.format_exc())

# ---------- MAIN ----------
def main():
    market = validate_api_and_resolve_market()
    while True:
        try:
            price = get_price(market['symbol'])
            avail = get_available_margin_usdt()
            contract_size = get_contract_size(market)
            logger.info(f"🫙 สรุปสถานะ | avail={avail:.4f} USDT | price={price} | contractSize={contract_size}")
            set_cross_leverage(market, LEVERAGE)
            contracts = calc_contracts(avail, price, contract_size)
            if contracts >= MIN_CONTRACTS:
                open_long(market, contracts)
            else:
                logger.warning(f"⚠️ ได้ contracts={contracts} < {MIN_CONTRACTS}")
        except Exception as e:
            logger.error(f"❌ Loop error: {e}")
            logger.debug(traceback.format_exc())
        time.sleep(LOOP_SLEEP_SECONDS)

if __name__ == "__main__":
    main()
