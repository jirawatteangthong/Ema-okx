import os
import ccxt
import math
import logging

# ---------------- CONFIG ----------------
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')
SYMBOL = 'BTC-USDT-SWAP'
PORTFOLIO_PERCENTAGE = 0.30
LEVERAGE = 15

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
def get_available_margin():
    try:
        balance = exchange.fetch_balance({'type': 'swap'})
        logger.debug(f"📦 Raw Balance Info: {balance['info']}")
        
        data = balance['info']['data'][0]
        
        details = data.get('details', [])
        if details and len(details) > 0:
            detail = details[0]
            if detail.get('availEq'):
                cross_margin = float(detail['availEq'])
            elif detail.get('availBal'):
                cross_margin = float(detail['availBal'])
            else:
                raise ValueError("ไม่มีข้อมูล availEq หรือ availBal ใน details")
        else:
            raise ValueError("ไม่มี details ในข้อมูล balance")
        
        logger.debug(f"💰 Available Margin (จาก details): {cross_margin} USDT")
        return cross_margin
    except Exception as e:
        logger.error(f"❌ ดึงข้อมูล margin ไม่ได้: {e}")
        return 0.0

def get_current_price():
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price = float(ticker['last'])
        logger.debug(f"💲 Current Price: {price}")
        return price
    except Exception as e:
        logger.error(f"❌ ดึงราคาไม่ได้: {e}")
        return 0.0

def calculate_order_size(available_usdt: float, price: float) -> int:
    try:
        if price <= 0:
            logger.warning("⚠️ ราคาปัจจุบัน <= 0 ไม่สามารถคำนวณได้")
            return 0
        
        target_usdt = available_usdt * PORTFOLIO_PERCENTAGE
        contract_size_btc = 0.0001  # ขนาด 1 สัญญา = 0.0001 BTC
        target_btc = target_usdt / price
        contracts = math.floor(target_btc / contract_size_btc)
        if contracts < 1:
            logger.warning(f"⚠️ จำนวน contracts ต่ำเกินไป: {contracts}")
            return 0
        actual_notional = contracts * contract_size_btc * price
        logger.debug(f"📊 Order Size: Target={target_usdt:,.2f} USDT | Contracts={contracts} | Notional={actual_notional:,.2f} USDT")
        return contracts
    except Exception as e:
        logger.error(f"❌ คำนวณขนาดออเดอร์ไม่ได้: {e}")
        return 0

def set_leverage(leverage: int):
    try:
        res = exchange.set_leverage(leverage, SYMBOL, params={'mgnMode': 'cross'})
        logger.debug(f"🔧 ตั้ง Leverage = {leverage}x สำเร็จ: {res}")
    except Exception as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้: {e}")

def open_long(contracts: int):
    try:
        if contracts <= 0:
            logger.warning("⚠️ Contracts <= 0 ไม่เปิดออเดอร์")
            return
        params = {
            'tdMode': 'isolated',
            'ordType': 'market',
            'posSide': 'long'
        }
        order = exchange.create_order(SYMBOL, 'market', 'buy', contracts, None, params)
        logger.info(f"✅ เปิด Long สำเร็จ: {order}")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการเปิด Long: {e}")
# ---------------- MAIN ----------------
if __name__ == "__main__":
    set_leverage(LEVERAGE)
    available_margin = get_available_margin()
    price = get_current_price()
    contracts = calculate_order_size(available_margin, price)

    logger.info(f"Available margin: {available_margin}")
    logger.info(f"Price: {price}")
    logger.info(f"Calculated contracts: {contracts}")

    open_long(contracts)
