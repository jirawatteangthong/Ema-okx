import os
import math
import logging
import ccxt

# ------------------ CONFIG ------------------
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')

SYMBOL = 'BTC-USDT-SWAP'
PORTFOLIO_PERCENTAGE = 0.80
LEVERAGE = 15
CONTRACT_SIZE_BTC = 0.0001

# ------------------ LOGGER ------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger()

# ------------------ EXCHANGE ------------------
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap'
    }
})

# ------------------ FUNCTIONS ------------------
def get_available_margin():
    try:
        balance = exchange.fetch_balance({'type': 'swap'})
        cross_margin = float(balance['info']['data'][0]['crossEq'])
        logger.debug(f"💰 Available Margin (Cross): {cross_margin} USDT")
        return cross_margin
    except Exception as e:
        logger.error(f"❌ ดึงข้อมูล margin ไม่ได้: {e}")
        return 0.0

def get_current_price():
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price = float(ticker['last'])
        logger.debug(f"💵 Current Price: {price}")
        return price
    except Exception as e:
        logger.error(f"❌ ดึงราคาไม่ได้: {e}")
        return 0.0

def calculate_order_size(available_usdt, price):
    try:
        target_usdt = available_usdt * PORTFOLIO_PERCENTAGE * LEVERAGE
        target_btc = target_usdt / price
        contracts = math.floor(target_btc / CONTRACT_SIZE_BTC)

        if contracts < 1:
            logger.warning(f"⚠️ Contracts ต่ำเกินไป: {contracts}")
            return 0

        actual_notional = contracts * CONTRACT_SIZE_BTC * price
        logger.debug(f"📊 Order Size: Target={target_usdt:,.2f} USDT | Contracts={contracts} | Notional={actual_notional:,.2f} USDT")
        return contracts
    except Exception as e:
        logger.error(f"❌ คำนวณ order size ไม่ได้: {e}")
        return 0

def open_long(contracts):
    try:
        order = exchange.create_market_buy_order(
            symbol=SYMBOL,
            amount=contracts
        )
        logger.info(f"✅ เปิด Long สำเร็จ: {order}")
    except Exception as e:
        logger.error(f"❌ เปิด Long ไม่ได้: {e}")

# ------------------ MAIN ------------------
def main():
    logger.info("🚀 เริ่มบอท เปิด Long ทันที")
    available_margin = get_available_margin()
    price = get_current_price()
    if price <= 0 or available_margin <= 0:
        logger.error("❌ Margin หรือราคาไม่ถูกต้อง หยุดทำงาน")
        return

    contracts = calculate_order_size(available_margin, price)
    if contracts > 0:
        open_long(contracts)
    else:
        logger.error("❌ ไม่มี contracts เพียงพอในการเปิดออเดอร์")

if __name__ == "__main__":
    main()
