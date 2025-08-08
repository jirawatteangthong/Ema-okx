import ccxt
import time
import requests
from datetime import datetime
import logging
import json
import os
import sys
import math

# ========================================================================
# 1. CONFIGURATION
# ========================================================================

API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')

SYMBOL = 'BTC-USDT-SWAP'
LEVERAGE = 10
TP_DISTANCE_POINTS = 250
SL_DISTANCE_POINTS = 400
PORTFOLIO_PERCENTAGE = 0.80

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# ========================================================================
# 2. LOGGING
# ========================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('test_bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ========================================================================
# 3. GLOBALS
# ========================================================================

exchange = None
market_info = None

# ========================================================================
# 4. EXCHANGE SETUP
# ========================================================================

def setup_exchange():
    global exchange, market_info
    try:
        if not all([API_KEY, SECRET, PASSWORD]) or API_KEY == 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING':
            raise ValueError("กรุณาตั้งค่า API Keys ใน Environment Variables หรือแก้ไขในโค้ดโดยตรง")

        exchange = ccxt.okx({
            'apiKey': API_KEY,
            'secret': SECRET,
            'password': PASSWORD,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',
                'adjustForTimeDifference': True,
            },
            'verbose': False,
            'timeout': 30000,
        })

        exchange.set_sandbox_mode(False)
        exchange.load_markets()
        market_info = exchange.market(SYMBOL)
        if not market_info:
            raise ValueError(f"ไม่พบข้อมูลตลาดสำหรับ {SYMBOL}")

        logger.info(f"✅ เชื่อมต่อกับ OKX Exchange สำเร็จ")

        try:
            exchange.set_leverage(LEVERAGE, SYMBOL, params={'mgnMode': 'cross'})
            logger.info(f"✅ ตั้งค่า Leverage เป็น {LEVERAGE}x สำเร็จ")
        except Exception as e:
            logger.warning(f"⚠️ ไม่สามารถตั้งค่า Leverage ได้: {e}")

    except Exception as e:
        logger.critical(f"❌ ไม่สามารถเชื่อมต่อ Exchange ได้: {e}")
        raise

# ========================================================================
# 5. TELEGRAM
# ========================================================================

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING':
        logger.warning("⚠️ ไม่ได้ตั้งค่า Telegram Token - ข้ามการส่งข้อความ")
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        requests.get(url, params=params, timeout=10)
        logger.info(f"📤 ส่ง Telegram: {msg[:50]}...")
    except Exception as e:
        logger.error(f"❌ ส่ง Telegram ไม่ได้: {e}")

# ========================================================================
# 6. PORTFOLIO FUNCTIONS
# ========================================================================

def get_portfolio_balance() -> float:
    """ดึงยอดคงเหลือ USDT"""
    try:
        balance_data = exchange.fetch_balance(params={'type': 'trade'})
        usdt_balance = 0.0

        if 'USDT' in balance_data and 'free' in balance_data['USDT']:
            usdt_balance = float(balance_data['USDT']['free'])
        else:
            okx_balance_info = balance_data.get('info', {}).get('data', [])
            for account in okx_balance_info:
                if account.get('ccy') == 'USDT':
                    usdt_balance = float(account.get('availBal', 0.0))
                    break

        logger.info(f"💰 ยอดคงเหลือ USDT: {usdt_balance:,.2f}")
        return usdt_balance

    except Exception as e:
        logger.error(f"❌ ไม่สามารถดึงยอดคงเหลือได้: {e}")
        return 0.0

def get_margin_info():
    """ตรวจสอบ Margin ก่อนเปิดออเดอร์"""
    try:
        balance_data = exchange.fetch_balance(params={'type': 'trade'})
        okx_balance_info = balance_data.get('info', {}).get('data', [])
        for acc in okx_balance_info:
            if acc.get('ccy') == 'USDT':
                avail = float(acc.get('availBal', 0))
                used = float(acc.get('frozenBal', 0))
                logger.info(f"📊 Margin Info: Available={avail:,.2f} USDT | Used={used:,.2f} USDT")
                return avail, used
    except Exception as e:
        logger.error(f"❌ ตรวจสอบ Margin ไม่ได้: {e}")
    return 0.0, 0.0

def get_current_position():
    """ตรวจสอบโพซิชันปัจจุบัน"""
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for pos in positions:
            pos_info = pos.get('info', {})
            pos_amount_str = pos_info.get('pos', '0')

            if float(pos_amount_str) != 0:
                return {
                    'side': 'long' if float(pos_amount_str) > 0 else 'short',
                    'size': abs(float(pos_amount_str)),
                    'entry_price': float(pos_info.get('avgPx', 0.0)),
                    'unrealized_pnl': float(pos_info.get('upl', 0.0))
                }
        return None
    except Exception as e:
        logger.error(f"❌ ไม่สามารถดึงข้อมูลโพซิชันได้: {e}")
        return None

# ========================================================================
# 7. ORDER FUNCTIONS
# ========================================================================

def calculate_order_size(available_usdt: float, price: float) -> float:
    try:
        # เงินที่จะใช้เปิดออเดอร์ ตามเปอร์เซ็นต์พอร์ต
        target_usdt = available_usdt * PORTFOLIO_PERCENTAGE

        # ขนาดสัญญา BTC ขั้นต่ำ (OKX BTCUSDT Futures)
        contract_size_btc = 0.0001

        # ✅ ใช้ Leverage คำนวณ Notional
        target_usdt_with_leverage = target_usdt * LEVERAGE

        # แปลงจาก USDT → BTC
        target_btc = target_usdt_with_leverage / price

        # ปัดให้ตรงกับ contract step
        contracts = math.floor(target_btc / contract_size_btc)

        if contracts < 1:
            logger.warning(f"⚠️ จำนวน contracts ต่ำเกินไป: {contracts}")
            return 0

        actual_notional = contracts * contract_size_btc * price
        margin_required = actual_notional / LEVERAGE

        logger.info(
            f"📊 Order Size: Target={target_usdt:,.2f} USDT | "
            f"Leverage={LEVERAGE}x | Contracts={contracts} | "
            f"Notional={actual_notional:,.2f} USDT | "
            f"Margin Required={margin_required:,.2f} USDT"
        )

        # ถ้าทุนไม่พอ ให้แจ้งเตือน
        if margin_required > available_usdt:
            logger.error(
                f"❌ Margin ไม่พอ! Available={available_usdt:.2f} USDT | ต้องใช้ {margin_required:.2f} USDT"
            )
            return 0

        return float(contracts)

    except Exception as e:
        logger.error(f"❌ คำนวณขนาดออเดอร์ไม่ได้: {e}")
        return 0

def get_current_price() -> float:
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        return float(ticker['last'])
    except Exception as e:
        logger.error(f"❌ ดึงราคาไม่ได้: {e}")
        return 0.0

# ========================================================================
# 8. TRADING
# ========================================================================

def open_long_position(current_price: float) -> bool:
    try:
        balance = get_portfolio_balance()
        if balance <= 0:
            logger.error("❌ ยอดคงเหลือไม่เพียงพอ")
            return False

        get_margin_info()  # ✅ log margin ก่อนเปิดออเดอร์

        contracts = calculate_order_size(balance, current_price)
        if contracts <= 0:
            logger.error("❌ คำนวณขนาดออเดอร์ไม่ได้")
            return False

        logger.info(f"🚀 กำลังเปิด Long {contracts} contracts ที่ราคา {current_price:,.1f}")
        order = exchange.create_market_order(
            symbol=SYMBOL,
            side='buy',
            amount=contracts,
            params={'tdMode': 'cross'}
        )

        if order and order.get('id'):
            logger.info(f"✅ เปิด Long สำเร็จ: Order ID {order.get('id')}")
            send_telegram(f"🚀 <b>เปิด Long สำเร็จ!</b>\n📊 Contracts: {contracts}\n💰 ราคาเข้า: {current_price:,.1f}\n🆔 Order ID: {order.get('id')}")
            return True
        else:
            logger.error("❌ ไม่สามารถเปิด Long ได้")
            return False

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการเปิด Long: {e}")
        send_telegram(f"❌ <b>เปิด Long ไม่สำเร็จ!</b>\nError: {str(e)[:200]}")
        return False

# ========================================================================
# 9. MAIN
# ========================================================================

def main():
    try:
        logger.info("🤖 เริ่มต้น OKX Test Bot")
        setup_exchange()

        current_pos = get_current_position()
        if current_pos:
            logger.info(f"⚠️ มีโพซิชันอยู่แล้ว: {current_pos['side'].upper()} {current_pos['size']} contracts")
            return

        current_price = get_current_price()
        if not current_price:
            logger.error("❌ ไม่สามารถดึงราคาปัจจุบันได้")
            return

        logger.info(f"💰 ราคา {SYMBOL} ปัจจุบัน: {current_price:,.1f}")
        open_long_position(current_price)

    except Exception as e:
        logger.critical(f"❌ เกิดข้อผิดพลาดร้ายแรง: {e}")
        send_telegram(f"❌ <b>เกิดข้อผิดพลาดร้ายแรง!</b>\n{str(e)[:200]}")

# ========================================================================
# 10. ENTRY
# ========================================================================

if __name__ == '__main__':
    main()
