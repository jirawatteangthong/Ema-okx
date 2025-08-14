import os
import ccxt
import math
import logging
import traceback

# ---------------- CONFIG ----------------
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')
SYMBOL = 'BTC-USDT-SWAP'
PORTFOLIO_PERCENTAGE = 0.80
LEVERAGE = 10

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

try:
    exchange.load_markets()
    logger.debug("✅ โหลดข้อมูลตลาดสำเร็จ")
except Exception as e:
    logger.error(f"❌ โหลดตลาดไม่สำเร็จ: {e}")
    exit()

# ---------------- FUNCTIONS ----------------
def get_available_margin():
    try:
        balance = exchange.fetch_balance({'type': 'swap'})
        logger.debug(f"📦 Raw Balance Info: {balance['info']}")

        data = balance['info']['data'][0]

        # ดึง availBal ก่อน เพราะตรงกับที่ในแอปโชว์ Available
        raw_value = (
            data.get('availBal') or  # ยอดที่ใช้ได้
            data.get('cashBal') or
            data.get('crossEq') or
            data.get('availEq') or
            data.get('eq') or
            "0"
        )

        cross_margin = float(raw_value) if raw_value.strip() else 0.0
        logger.debug(f"💰 Available Margin (Cross): {cross_margin} USDT")
        return cross_margin

    except Exception as e:
        logger.error(f"❌ ดึงข้อมูล margin ไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0.0
        
def get_current_price():
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price = float(ticker['last'])
        logger.debug(f"📈 Current Price {SYMBOL}: {price}")
        return price
    except Exception as e:
        logger.error(f"❌ ดึงราคาไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0.0

def calculate_order_size(available_usdt: float, price: float) -> float:
    try:
        target_usdt = available_usdt * PORTFOLIO_PERCENTAGE
        contract_size_btc = 0.0001  # OKX BTCUSDT-SWAP contract size
        target_btc = target_usdt / price
        contracts = math.floor(target_btc / contract_size_btc)

        if contracts < 1:
            logger.warning(f"⚠️ Contracts ต่ำกว่า 1: {contracts} | Target BTC={target_btc} | Contract size BTC={contract_size_btc}")
            return 0

        actual_notional = contracts * contract_size_btc * price
        logger.debug(f"📊 Order Size: Target={target_usdt:.2f} USDT | Contracts={contracts} | Notional={actual_notional:.2f} USDT")
        return float(contracts)

    except Exception as e:
        logger.error(f"❌ คำนวณขนาดออเดอร์ไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0

def set_leverage(leverage: int):
    try:
        res = exchange.set_leverage(leverage, SYMBOL, params={'mgnMode': 'cross'})
        logger.debug(f"🔧 ตั้ง Leverage = {leverage}x สำเร็จ: {res}")
    except Exception as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้: {e}")
        logger.debug(traceback.format_exc())

def open_long(contracts: float):
    try:
        if contracts <= 0:
            logger.warning("⚠️ Contracts <= 0 ไม่เปิดออเดอร์")
            return

        params = {
            'tdMode': 'cross',   # Cross Margin
            'posSide': 'long'    # เปิดฝั่ง Long
        }

        logger.debug(f"🚀 ส่งคำสั่ง: symbol={SYMBOL}, type=market, side=buy, amount={contracts}, params={params}")
        
        order = exchange.create_order(
            symbol=SYMBOL,
            type='market',
            side='buy',
            amount=contracts,
            price=None,
            params=params
        )
        logger.info(f"✅ เปิด Long สำเร็จ: {order}")

    except ccxt.BaseError as e:
        logger.error(f"❌ API Error: {str(e)}")
        logger.debug(traceback.format_exc())
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดทั่วไปในการเปิด Long: {e}")
        logger.debug(traceback.format_exc())

# ---------------- MAIN ----------------
if __name__ == "__main__":
    logger.info("🚀 เริ่มบอทเปิด Long ทันที")
    set_leverage(LEVERAGE)
    available_margin = get_available_margin()
    price = get_current_price()
    contracts = calculate_order_size(available_margin, price)
    open_long(contracts)
