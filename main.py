import ccxt
import logging
import math
import os

# ================== CONFIG ==================
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')  # Passphrase for OKX
SYMBOL = 'BTC-USDT-SWAP'
PORTFOLIO_PERCENTAGE = 0.80  # ใช้ 80% ของพอร์ต
CONTRACT_SIZE_BTC = 0.0001   # ขนาดสัญญา BTC ต่อ 1 contract
# =============================================

# Logger Setup
logging.basicConfig(
    level=logging.DEBUG,  # DEBUG เพื่อดู log ละเอียด
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger()

# OKX Setup
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',  # Futures/Swap
    }
})

# ---------------- GET CURRENT PRICE ----------------
def get_current_price() -> float:
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price = float(ticker['last'])
        logger.debug(f"📈 Current Price: {price}")
        return price
    except Exception as e:
        logger.error(f"❌ ดึงราคาไม่ได้: {e}")
        return 0.0

# ---------------- GET MARGIN INFO ----------------
def get_margin_info() -> float:
    try:
        balance = exchange.fetch_balance()
        cross_usdt = balance['info']['data'][0]['details'][0]['cashBal']
        cross_usdt = float(cross_usdt)
        logger.debug(f"💰 Available Margin (Cross): {cross_usdt} USDT")
        return cross_usdt
    except Exception as e:
        logger.error(f"❌ ดึง Margin ไม่ได้: {e}")
        return 0.0

# ---------------- CALCULATE ORDER SIZE ----------------
def calculate_order_size(available_usdt: float, price: float) -> float:
    try:
        target_usdt = available_usdt * PORTFOLIO_PERCENTAGE
        target_btc = target_usdt / price
        contracts = math.floor(target_btc / CONTRACT_SIZE_BTC)

        if contracts < 1:
            logger.warning(f"⚠️ Contracts ต่ำเกินไป: {contracts}")
            return 0

        actual_notional = contracts * CONTRACT_SIZE_BTC * price
        logger.debug(
            f"📊 Order Size: Target={target_usdt:.2f} USDT | "
            f"Contracts={contracts} | Notional={actual_notional:.2f} USDT"
        )

        return float(contracts)
    except Exception as e:
        logger.error(f"❌ คำนวณขนาดออเดอร์ไม่ได้: {e}")
        return 0

# ---------------- OPEN LONG POSITION ----------------
def open_long_position():
    try:
        price = get_current_price()
        if price == 0:
            return

        available_margin = get_margin_info()
        contracts = calculate_order_size(available_margin, price)
        if contracts == 0:
            logger.error("🚫 Margin ไม่พอหรือ contracts = 0")
            return

        # ส่งคำสั่ง
        order = exchange.create_order(
            symbol=SYMBOL,
            type='market',
            side='buy',
            amount=contracts
        )
        logger.info(f"✅ เปิด Long สำเร็จ: {order}")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการเปิด Long: {e}")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    logger.info("🚀 เริ่มระบบบอท")
    open_long_position()
