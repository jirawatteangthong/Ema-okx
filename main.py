import ccxt
import logging
import traceback
import time

# ==== CONFIG ====
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')
SYMBOL = 'BTC-USDT-SWAP'
LEVERAGE = 10
ORDER_SIZE_USDT = 50  # จำนวนทุนที่จะใช้เปิดต่อครั้ง
MIN_CONTRACTS = 1     # ต้องมากกว่า 1 ถึงจะเปิด

# ==== LOGGER ====
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# ==== CONNECT OKX ====
exchange = ccxt.okx({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "password": PASSWORD,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

# ==== ฟังก์ชันดึง Margin ====
def get_available_margin():
    try:
        balance = exchange.fetch_balance({'type': 'swap'})
        logger.debug(f"📦 Raw Balance Info: {balance['info']}")

        data_list = balance['info']['data']
        usdt_info = None
        for item in data_list:
            if item.get('ccy') == 'USDT':
                usdt_info = item
                break

        if not usdt_info:
            logger.warning("⚠️ ไม่พบข้อมูล USDT ในบัญชี Futures Cross")
            return 0.0

        raw_value = usdt_info.get('availBal') or "0"
        cross_margin = float(raw_value) if raw_value.strip() else 0.0
        logger.debug(f"💰 Available Margin (Cross): {cross_margin} USDT")
        return cross_margin

    except Exception as e:
        logger.error(f"❌ ดึงข้อมูล margin ไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0.0

# ==== ฟังก์ชันดึง Contract Size ====
def get_contract_size(symbol):
    try:
        markets = exchange.load_markets()
        market_info = markets.get(symbol)
        if market_info and 'contractSize' in market_info:
            return float(market_info['contractSize'])
        logger.warning(f"⚠️ ไม่พบ contractSize ของ {symbol}")
        return 0.0
    except Exception as e:
        logger.error(f"❌ ดึง contract size ไม่ได้: {e}")
        return 0.0

# ==== ฟังก์ชันดึงราคาปัจจุบัน ====
def get_current_price(symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        return float(ticker['last'])
    except Exception as e:
        logger.error(f"❌ ดึงราคาปัจจุบันไม่ได้: {e}")
        return 0.0

# ==== ฟังก์ชันเปิด Long ====
def open_long(symbol, contracts):
    try:
        logger.debug(f"🚀 ส่งคำสั่ง Long: {contracts} contracts")
        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=contracts
        )
        logger.info(f"✅ เปิด Long สำเร็จ: {order}")
        return order
    except Exception as e:
        logger.error(f"❌ เปิด Long ล้มเหลว: {e}")
        logger.debug(traceback.format_exc())
        return None

# ==== MAIN ====
def main():
    logger.info("=== เริ่มบอท เปิด Long ทันที ===")

    # 1) ตั้ง leverage
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL, {'mgnMode': 'cross'})
        logger.debug(f"⚙️ ตั้ง Leverage {LEVERAGE}x เรียบร้อย")
    except Exception as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้: {e}")
        return

    # 2) ดึง available margin
    available_margin = get_available_margin()
    if available_margin <= 0:
        logger.warning("⚠️ Margin เป็น 0 ไม่สามารถเปิดออเดอร์ได้")
        return

    # 3) ดึงราคา + contract size
    price = get_current_price(SYMBOL)
    contract_size = get_contract_size(SYMBOL)
    if price <= 0 or contract_size <= 0:
        logger.warning("⚠️ ข้อมูลราคา หรือ contract size ไม่ถูกต้อง")
        return

    # 4) คำนวณ contracts
    target_btc = ORDER_SIZE_USDT / price
    contracts = target_btc / contract_size
    logger.debug(f"🎯 Target BTC={target_btc} | Contracts={contracts} | Contract size BTC={contract_size}")

    if contracts < MIN_CONTRACTS:
        logger.warning(f"⚠️ Contracts ต่ำกว่า {MIN_CONTRACTS}: {contracts}")
        return

    # 5) เปิด Long
    open_long(SYMBOL, contracts)

if __name__ == "__main__":
    main()
