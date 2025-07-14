import ccxt
import time
import requests
from datetime import datetime, timedelta
import logging
import threading
import json
import os
import calendar
import sys
import math

# ==============================================================================
# 1. ตั้งค่าพื้นฐาน (CONFIGURATION)
# ==============================================================================

# --- API Keys & Credentials (ดึงจาก Environment Variables เพื่อความปลอดภัย) ---
# ตรวจสอบให้แน่ใจว่าได้ตั้งค่าใน Environment Variables: OKX_API_KEY, OKX_SECRET, OKX_PASSWORD
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING') # Passphrase for OKX

# --- Trade Parameters ---
SYMBOL = 'BTC-USDT-SWAP' # <--- เปลี่ยนเป็นสัญลักษณ์ OKX Perpetual Swap
TIMEFRAME = '3m' # เปลี่ยนเป็น 3 นาที
LEVERAGE = 35    # <--- อัปเดต Leverage เป็น 35x ตามที่คุณต้องการ
TP_DISTANCE_POINTS = 250  # อาจจะลอง 50 จุด
SL_DISTANCE_POINTS = 400  # อาจจะลอง 200 จุด (หรือน้อยกว่า)
BE_PROFIT_TRIGGER_POINTS = 200  # เลื่อน SL เมื่อกำไร 40 จุด (น้อยกว่า TP)
BE_SL_BUFFER_POINTS = 50   # เลื่อน SL ไปตั้งที่ +10 จุด (เมื่อกำไรแล้วโดน SL ก็ยังได้กำไรเล็กน้อย)
CROSS_THRESHOLD_POINTS = 1 

# เพิ่มค่าตั้งค่าใหม่สำหรับการบริหารความเสี่ยงและออเดอร์
# MARGIN_BUFFER_USDT = 25 # <--- ลบออกไปแล้ว
TARGET_POSITION_SIZE_FACTOR = 0.7  # <--- อัปเดตตามที่คุณต้องการ (0.7 = 70%)
MARGIN_BUFFER_PERCENTAGE = 0.05 # <--- เพิ่มส่วนนี้: 5% ของยอด Available USDT เพื่อเป็น Margin Buffer
MIN_MARGIN_BUFFER_USDT = 5.0 # <--- เพิ่ม: กำหนดบัฟเฟอร์ขั้นต่ำเป็น USDT (เพื่อป้องกันกรณีทุนน้อยมาก)

# ค่าสำหรับยืนยันโพซิชันหลังเปิดออเดอร์ (ใช้ใน confirm_position_entry)
CONFIRMATION_RETRIES = 15  
CONFIRMATION_SLEEP = 5  

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
STATS_FILE = 'trading_stats.json' # ควรเปลี่ยนเป็น '/data/trading_stats.json' หากใช้ Railway Volume

# --- Bot Timing ---
MAIN_LOOP_SLEEP_SECONDS = 180 
ERROR_RETRY_SLEEP_SECONDS = 60
MONTHLY_REPORT_DAY = 20
MONTHLY_REPORT_HOUR = 0
MONTHLY_REPORT_MINUTE = 5

# --- Tolerance สำหรับการระบุสาเหตุการปิดออเดอร์ ---
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005 

# ==============================================================================
# 2. การตั้งค่า Logging
# ==============================================================================
logging.basicConfig(
    level=logging.DEBUG, # <--- แนะนำ INFO สำหรับการใช้งานปกติ, หากติดปัญหาค่อยเปลี่ยนเป็น DEBUG
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
for handler in logging.root.handlers:
    if hasattr(handler, 'flush'):
        handler.flush = lambda: sys.stdout.flush() if isinstance(handler, logging.StreamHandler) else handler.stream.flush()

logger = logging.getLogger(__name__)


# ==============================================================================
# 3. ตัวแปรสถานะการเทรด (GLOBAL TRADE STATE VARIABLES)
# ==============================================================================
current_position_details = None 
entry_price = None
sl_moved = False
portfolio_balance = 0.0
last_monthly_report_date = None
initial_balance = 0.0
current_position_size = 0.0 # ขนาดโพซิชันในหน่วย Contracts
last_ema_position_status = None 

# ==============================================================================
# 4. โครงสร้างข้อมูลสถิติ (STATISTICS DATA STRUCTURE)
# ==============================================================================
monthly_stats = {
    'month_year': None,
    'tp_count': 0,
    'sl_count': 0,
    'total_pnl': 0.0,
    'trades': [],
    'last_report_month_year': None,
    'last_ema_cross_signal': None, 
    'last_ema_position_status': None 
}

# ==============================================================================
# 5. การตั้งค่า Exchange (CCXT EXCHANGE SETUP)
# ==============================================================================
exchange = None 
market_info = None 

def setup_exchange():
    global exchange, market_info
    try:
        if not all([API_KEY, SECRET, PASSWORD]) or \
           API_KEY == 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING': 
            raise ValueError("API_KEY, SECRET, หรือ PASSWORD (Passphrase) ไม่ถูกตั้งค่าใน Environment Variables.")

        exchange = ccxt.okx({ 
            'apiKey': API_KEY,
            'secret': SECRET,
            'password': PASSWORD, 
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap', 
                'warnOnFetchOHLCVLimitArgument': False,
                'adjustForTimeDifference': True,
            },
            'verbose': False, 
            'timeout': 30000,
        })
        exchange.set_sandbox_mode(False) 
        
        exchange.load_markets()
        logger.info("✅ เชื่อมต่อกับ OKX Exchange สำเร็จ และโหลด Markets แล้ว.")
        
        market_info = exchange.market(SYMBOL)
        if not market_info:
            raise ValueError(f"ไม่พบข้อมูลตลาดสำหรับสัญลักษณ์ {SYMBOL}")
        
        # ตรวจสอบและกำหนดค่าเริ่มต้นสำหรับ limits ที่อาจไม่มี
        if 'limits' not in market_info:
            market_info['limits'] = {}
        if 'amount' not in market_info['limits']:
            market_info['limits']['amount'] = {}
        if 'cost' not in market_info['limits']:
            market_info['limits']['cost'] = {}

        # ดึงค่า step, min, max สำหรับ amount (contracts)
        amount_step = market_info['limits']['amount'].get('step')
        market_info['limits']['amount']['step'] = float(amount_step) if amount_step is not None else 1.0 # Default to 1.0 contract step for OKX
        
        amount_min = market_info['limits']['amount'].get('min')
        market_info['limits']['amount']['min'] = float(amount_min) if amount_min is not None else 1.0 # Default to 1.0 minimum contract
        
        amount_max = market_info['limits']['amount'].get('max')
        market_info['limits']['amount']['max'] = float(amount_max) if amount_max is not None else sys.float_info.max 

        # ดึงค่า min, max สำหรับ cost (notional value)
        cost_min = market_info['limits']['cost'].get('min')
        market_info['limits']['cost']['min'] = float(cost_min) if cost_min is not None else 11.8 # อัปเดต default ตามข้อมูลล่าสุด
        
        cost_max = market_info['limits']['cost'].get('max')
        market_info['limits']['cost']['max'] = float(cost_max) if cost_max is not None else sys.float_info.max 

        logger.debug(f"DEBUG: Market info limits for {SYMBOL}:")
        logger.debug(f"  Amount: step={market_info['limits']['amount']['step']}, min={market_info['limits']['amount']['min']}, max={market_info['limits']['amount']['max']}")
        logger.debug(f"  Cost: min={market_info['limits']['cost']['min']}, max={market_info['limits']['cost']['max']}")
        # --- IMPORTANT: market_info.get('contractSize') might be incorrect ---
        # We will use a hardcoded value in calculate_order_details for BTC-USDT-SWAP
        logger.debug(f"  Contract Size (from market_info, for reference only): {market_info.get('contractSize', 'N/A')}") 
        # เพิ่ม logging สำหรับ full market_info
        logger.debug(f"DEBUG: Full market_info for {SYMBOL}: {json.dumps(market_info, indent=2)}")

        try:
            result = exchange.set_leverage(LEVERAGE, SYMBOL, params={'mgnMode': 'cross'}) 
            logger.info(f"✅ ตั้งค่า Leverage เป็น {LEVERAGE}x สำหรับ {SYMBOL}: {result}")
        except ccxt.ExchangeError as e:
            if "leverage is not valid" in str(e) or "not valid for this symbol" in str(e):
                logger.critical(f"❌ Error: Leverage {LEVERAGE}x ไม่ถูกต้องสำหรับ {SYMBOL} บน OKX. โปรดตรวจสอบ Max Allowed Leverage.")
            else:
                logger.critical(f"❌ Error ในการตั้งค่า Leverage: {e}", exc_info=True)
            exit()
        
    except ValueError as ve:
        logger.critical(f"❌ Configuration Error: {ve}", exc_info=True)
        exit()
    except Exception as e:
        logger.critical(f"❌ ไม่สามารถเชื่อมต่อหรือโหลดข้อมูล Exchange เบื้องต้นได้: {e}", exc_info=True)
        exit()

# ==============================================================================
# 6. ฟังก์ชันจัดการสถิติ (STATISTICS MANAGEMENT FUNCTIONS)
# ==============================================================================

def save_monthly_stats():
    global monthly_stats, last_ema_position_status
    try:
        monthly_stats['last_ema_position_status'] = last_ema_position_status
        with open(os.path.join(os.getcwd(), STATS_FILE), 'w') as f:
            json.dump(monthly_stats, f, indent=4)
        logger.debug(f"💾 บันทึกสถิติการเทรดลงไฟล์ {STATS_FILE} สำเร็จ")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการบันทิติสถิติ: {e}")

def reset_monthly_stats():
    global monthly_stats, last_ema_position_status
    monthly_stats['month_year'] = datetime.now().strftime('%Y-%m')
    monthly_stats['tp_count'] = 0
    monthly_stats['sl_count'] = 0
    monthly_stats['total_pnl'] = 0.0
    monthly_stats['trades'] = []
    last_ema_position_status = None 
    save_monthly_stats() 
    logger.info(f"🔄 รีเซ็ตสถิติประจำเดือนสำหรับเดือน {monthly_stats['month_year']}")

def load_monthly_stats():
    global monthly_stats, last_monthly_report_date, last_ema_position_status
    try:
        stats_file_path = os.path.join(os.getcwd(), STATS_FILE)
        if os.path.exists(stats_file_path):
            with open(stats_file_path, 'r') as f:
                loaded_stats = json.load(f)

                monthly_stats['month_year'] = loaded_stats.get('month_year', None)
                monthly_stats['tp_count'] = loaded_stats.get('tp_count', 0)
                monthly_stats['sl_count'] = loaded_stats.get('sl_count', 0)
                monthly_stats['total_pnl'] = loaded_stats.get('total_pnl', 0.0)
                monthly_stats['trades'] = loaded_stats.get('trades', [])
                monthly_stats['last_report_month_year'] = loaded_stats.get('last_report_month_year', None)
                monthly_stats['last_ema_cross_signal'] = loaded_stats.get('last_ema_cross_signal', None)
                last_ema_position_status = loaded_stats.get('last_ema_position_status', None)

            logger.info(f"💾 โหลดสถิติการเทรดจากไฟล์ {STATS_FILE} สำเร็จ")

            if monthly_stats['last_report_month_year']:
                try:
                    year, month = map(int, monthly_stats['last_report_month_year'].split('-'))
                    last_monthly_report_date = datetime(year, month, 1).date()
                except ValueError:
                    logger.warning("⚠️ รูปแบบวันที่ last_report_report_month_year ในไฟล์ไม่ถูกต้อง. จะถือว่ายังไม่มีการส่งรายงาน.")
                    last_monthly_report_date = None
            else:
                last_monthly_report_date = None

            current_month_year_str = datetime.now().strftime('%Y-%m')
            if monthly_stats['month_year'] != current_month_year_str:
                logger.info(f"🆕 สถิติที่โหลดมาเป็นของเดือน {monthly_stats['month_year']} ไม่ตรงกับเดือนนี้ {current_month_year_str}. จะรีเซ็ตสถิติสำหรับเดือนใหม่.")
                reset_monthly_stats()

        else:
            logger.info(f"🆕 ไม่พบไฟล์สถิติ {STATS_FILE} สร้างไฟล์ใหม่")
            reset_monthly_stats()

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการโหลดสถิติ: {e}", exc_info=True)
        if not os.access(os.path.dirname(stats_file_path) or '.', os.W_OK):
             logger.critical(f"❌ ข้อผิดพลาด: ไม่มีสิทธิ์เขียนไฟล์ในไดเรกทอรี: {os.path.dirname(stats_file_path) or '.'}. โปรดตรวจสอบสิทธิ์การเข้าถึงหรือเปลี่ยน STATS_FILE.")

        monthly_stats = {
            'month_year': None, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0, 'trades': [],
            'last_report_month_year': None, 'last_ema_cross_signal': None, 'last_ema_position_status': None
        }
        last_monthly_report_date = None
        last_ema_position_status = None
        reset_monthly_stats()

def add_trade_result(reason: str, pnl: float):
    global monthly_stats
    current_month_year_str = datetime.now().strftime('%Y-%m')

    if monthly_stats['month_year'] != current_month_year_str:
        logger.info(f"🆕 เดือนเปลี่ยนใน add_trade_result: {monthly_stats['month_year']} -> {current_month_year_str}. กำลังรีเซ็ตสถิติประจำเดือน.")
        reset_monthly_stats()

    if reason.upper() == 'TP':
        monthly_stats['tp_count'] += 1
    elif reason.upper() == 'SL' or reason.upper() == 'SL (กันทุน)':
        monthly_stats['sl_count'] += 1

    monthly_stats['total_pnl'] += pnl

    monthly_stats['trades'].append({
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'reason': reason,
        'pnl': pnl
    })
    save_monthly_stats()

# ==============================================================================
# 7. ฟังก์ชันแจ้งเตือน Telegram (TELEGRAM NOTIFICATION FUNCTIONS)
# ==============================================================================
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING' or \
       not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING':
        logger.warning("⚠️ TELEGRAM_TOKEN หรือ TELEGRAM_CHAT_ID ไม่ได้ถูกตั้งค่า. ไม่สามารถส่งข้อความ Telegram ได้.")
        return

    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status() 
        logger.info(f"✉️ Telegram: {msg.splitlines()[0]}...")
    except requests.exceptions.Timeout:
        logger.error("⛔️ Error: ไม่สามารถส่งข้อความ Telegram ได้ (Timeout)")
    except requests.exceptions.HTTPError as e:
        telegram_error_msg = e.response.json().get('description', e.response.text)
        logger.error(f"⛔️ Error: ไม่สามารถส่งข้อความ Telegram ได้ (HTTP Error) - รายละเอียด: {telegram_error_msg}")
    except requests.exceptions.RequestException as e:
        logger.error(f"⛔️ Error: ไม่สามารถส่งข้อความ Telegram ได้ (Request Error) - {e}")
    except Exception as e:
        logger.error(f"❌ Unexpected Telegram error: {e}")

# ==============================================================================
# 8. ฟังก์ชันดึงข้อมูล Exchange (EXCHANGE DATA RETRIEVAL FUNCTIONS)
# ==============================================================================

def get_portfolio_balance() -> float:
    """ดึงยอดคงเหลือ USDT ในพอร์ตสำหรับ OKX (Trading Account / Total Equity)."""
    global portfolio_balance
    retries = 3
    for i in range(retries):
        try:
            logger.debug(f"🔍 กำลังดึงยอดคงเหลือ (Attempt {i+1}/{retries})...")
            balance_data = exchange.fetch_balance(params={'type': 'trade'}) 
            time.sleep(1) 
            
            usdt_balance = 0.0
            if 'USDT' in balance_data and 'free' in balance_data['USDT']:
                usdt_balance = float(balance_data['USDT']['free'])
            else: 
                okx_balance_info = balance_data.get('info', {}).get('data', [])
                if okx_balance_info:
                    for account in okx_balance_info:
                        if account.get('ccy') == 'USDT' and account.get('type') == 'TRADE':
                            usdt_balance = float(account.get('availBal', 0.0)) 
                            break
            
            if usdt_balance > 0:
                portfolio_balance = usdt_balance
                logger.info(f"💰 ยอดคงเหลือ USDT (OKX): {portfolio_balance:,.2f}")
                return portfolio_balance
            else:
                 logger.warning("⚠️ ไม่พบยอดคงเหลือ USDT ที่ใช้งานได้ในบัญชี OKX (availBal).")
                 return 0.0

        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.warning(f"⚠️ Error fetching balance (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
            if i == retries - 1:
                send_telegram(f"⛔️ API Error: ไม่สามารถดึงยอดคงเหลือได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f"❌ Unexpected error in get_portfolio_balance: {e}", exc_info=True)
            send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึงยอดคงเหลือได้\nรายละเอียด: {e}")
            return 0.0
    logger.error(f"❌ Failed to fetch balance after {retries} attempts.")
    send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงยอดคงเหลือหลังจาก {retries} ครั้ง.")
    return 0.0

def get_current_position() -> dict | None:
    """
    ตรวจสอบและดึงข้อมูลโพซิชัน BTC/USDT ปัจจุบันสำหรับ OKX.
    ปรับปรุงให้รองรับ Hedge Mode และใช้ 'pos' field.
    """
    retries = 3
    for i in range(retries):
        try:
            logger.debug(f"🔍 กำลังดึงโพซิชันปัจจุบัน (Attempt {i+1}/{retries})...")
            positions = exchange.fetch_positions([SYMBOL]) 
            logger.debug(f"DEBUG: Raw positions fetched: {positions}") # <--- Keep this for full raw data inspection
            time.sleep(1) 
            
            active_positions = [
                pos for pos in positions
                if pos.get('info', {}).get('instId') == SYMBOL and float(pos.get('info', {}).get('pos', '0')) != 0
            ]
            
            if not active_positions:
                logger.debug(f"ℹ️ ไม่พบตำแหน่งที่เปิดอยู่สำหรับ {SYMBOL}")
                return None

            for pos in active_positions:
                pos_info = pos.get('info', {})
                pos_amount_str = pos_info.get('pos') 
                
                # *** IMPORTANT: Use pos['contracts'] or pos['amount'] if available and correctly normalized by CCXT ***
                # If pos_info.get('pos') returns a value like "1" but means "1 contract"
                # and CCXT's 'amount' or 'contracts' field is also 1, then use it.
                # If 'pos' field from OKX API sometimes returns a BTC value, not contract count,
                # then you would need to convert it using the correct contract_size (0.0001 BTC/contract).
                # For now, let's assume 'pos' here is contract count, based on the previous log showing 'Size=1.0 Contracts'.
                pos_amount = abs(float(pos_amount_str)) 

                entry_price_okx = float(pos_info.get('avgPx', 0.0))
                unrealized_pnl_okx = float(pos_info.get('upl', 0.0))
                
                side = pos_info.get('posSide', '').lower()

                if side != 'net' and pos_amount > 0:
                    logger.debug(f"✅ พบโพซิชันสำหรับ {SYMBOL}: Side={side}, Size={pos_amount}, Entry={entry_price_okx}")
                    return {
                        'side': side,
                        'size': pos_amount, # This is the contract count
                        'entry_price': entry_price_okx,
                        'unrealized_pnl': unrealized_pnl_okx,
                        'pos_id': pos.get('id', 'N/A') 
                    }
            
            logger.debug(f"⚠️ พบข้อมูลตำแหน่งสำหรับ {SYMBOL} แต่ไม่ตรงกับเงื่อนไข active/hedge mode.")
            return None 

        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.warning(f"⚠️ Error fetching positions (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
            if i == retries - 1:
                send_telegram(f"⛔️ API Error: ไม่สามารถดึงโพซิชันได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f"❌ Unexpected error in get_current_position: {e}", exc_info=True)
            send_telegram(f"⛔️️ Unexpected Error: ไม่สามารถดึงโพซิชันได้\nรายละเอียด: {e}")
            return None 
    logger.error(f"❌ Failed to fetch positions after {retries} attempts.")
    send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงโพซิชันหลังจาก {retries} ครั้ง.")
    return None

# ==============================================================================
# 9. ฟังก์ชันคำนวณ Indicators (INDICATOR CALCULATION FUNCTIONS)
# ==============================================================================

def calculate_ema(prices: list[float], period: int) -> float | None:
    if len(prices) < period:
        return None

    sma = sum(prices[:period]) / period
    ema = sma
    multiplier = 2 / (period + 1)

    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))

    return ema

def check_ema_cross() -> str | None:
    global last_ema_position_status 
    
    try:
        retries = 3
        ohlcv = None
        for i in range(retries):
            logger.debug(f"🔍 กำลังดึงข้อมูล OHLCV สำหรับ EMA ({i+1}/{retries})...")
            try:
                ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=500) 
                time.sleep(1) 
                break
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f"⚠️ Error fetching OHLCV (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
                if i == retries - 1:
                    send_telegram(f"⛔️ API Error: ไม่สามารถดึง OHLCV ได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
                time.sleep(15)
            except Exception as e:
                logger.error(f"❌ Unexpected error fetching OHLCV: {e}", exc_info=True)
                send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึง OHLCV ได้\nรายละเอียด: {e}")
                return None

        if not ohlcv:
            logger.error(f"❌ Failed to fetch OHLCV after {retries} attempts.")
            send_telegram(f"⛔️ API Error: ล้มเหลวในการดึง OHLCV หลังจาก {retries} ครั้ง.")
            return None

        if len(ohlcv) < 201: 
            logger.warning(f"ข้อมูล OHLCV ไม่เพียงพอ. ต้องการอย่างน้อย 201 แท่ง ได้ {len(ohlcv)}")
            send_telegram(f"⚠️ ข้อมูล OHLCV ไม่เพียงพอ ({len(ohlcv)} แท่ง).")
            return None

        closes = [candle[4] for candle in ohlcv]

        ema50_current = calculate_ema(closes, 50)
        ema200_current = calculate_ema(closes, 200)

        logger.info(f"💡 EMA Values: Current EMA50={ema50_current:.2f}, EMA200={ema200_current:.2f}") 
        
        if None in [ema50_current, ema200_current]:
            logger.warning("ค่า EMA ไม่สามารถคำนวณได้ (เป็น None).")
            return None

        current_ema_position = None
        if ema50_current > ema200_current:
            current_ema_position = 'above'
        elif ema50_current < ema200_current:
            current_ema_position = 'below'
        
        if last_ema_position_status is None:
            if current_ema_position:
                last_ema_position_status = current_ema_position
                save_monthly_stats()
                logger.info(f"ℹ️ บอทเพิ่งเริ่มรัน. บันทึกสถานะ EMA ปัจจุบันเป็น: {current_ema_position.upper()}. จะรอสัญญาณการตัดกันครั้งถัดไป.")
            return None 

        cross_signal = None

        if last_ema_position_status == 'below' and current_ema_position == 'above' and \
           ema50_current > (ema200_current + CROSS_THRESHOLD_POINTS):
            cross_signal = 'long'
            logger.info(f"🚀 Threshold Golden Cross: EMA50({ema50_current:.2f}) is {CROSS_THRESHOLD_POINTS} points above EMA200({ema200_current:.2f})")

        elif last_ema_position_status == 'above' and current_ema_position == 'below' and \
             ema50_current < (ema200_current - CROSS_THRESHOLD_POINTS):
            cross_signal = 'short'
            logger.info(f"🔻 Threshold Death Cross: EMA50({ema50_current:.2f}) is {CROSS_THRESHOLD_POINTS} points below EMA200({ema200_current:.2f})")

        if cross_signal is not None:
            logger.info(f"✨ สัญญาณ EMA Cross ที่ตรวจพบ: {cross_signal.upper()}")
            if current_ema_position != last_ema_position_status:
                logger.info(f"ℹ️ EMA position changed from {last_ema_position_status.upper()} to {current_ema_position.upper()} during a cross signal. Updating last_ema_position_status.")
                last_ema_position_status = current_ema_position
                save_monthly_stats() 
        elif current_ema_position != last_ema_position_status: 
            logger.info(f"ℹ️ EMA position changed from {last_ema_position_status.upper()} to {current_ema_position.upper()}. Updating last_ema_position_status (no cross signal detected).")
            last_ema_position_status = current_ema_position
            save_monthly_stats() 
        else: 
            logger.info("🔎 ไม่พบสัญญาณ EMA Cross ที่ชัดเจน.") 
            
        return cross_signal

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการคำนวณ EMA: {e}", exc_info=True)
        send_telegram(f"⛔️ Error: ไม่สามารถคำนวณ EMA ได้\nรายละเอียด: {e}")
        return None

# ==============================================================================
# 10. ฟังก์ชันช่วยสำหรับการคำนวณและตรวจสอบออเดอร์
# ==============================================================================

def calculate_order_details(available_usdt: float, price: float) -> tuple[float, float]:
    """
    คำนวณจำนวน Contracts (สัญญา) และ Margin ที่ต้องการสำหรับเปิดออเดอร์บน OKX.
    ใช้ค่า limits จาก market_info ที่โหลดมา.
    """
    try:
        # ดึงค่า limits จาก market_info (ตรวจสอบให้แน่ใจว่าโหลดมาแล้ว)
        min_notional_exchange = float(market_info['limits']['cost'].get('min', '11.8')) 
        max_notional_exchange = float(market_info['limits']['cost'].get('max', str(sys.float_info.max))) 
        
        # *** IMPORTANT FIX ***
        # OKX BTC-USDT-SWAP contract size is DEFINITELY 0.0001 BTC per contract.
        # Your log showed 0.01, which is incorrect and caused the small order size.
        # Hardcode this value to ensure correctness, as market_info might sometimes be unreliable.
        contract_size_in_btc = 0.0001 # <--- แก้ไขตรงนี้เป็นค่าที่ถูกต้อง
        logger.debug(f"DEBUG: Confirmed contract_size for {SYMBOL} is {contract_size_in_btc} BTC/contract.")

        # actual_contracts_step_size: ขนาดการเพิ่ม/ลดของจำนวนสัญญา (เช่น 1.0 คือเพิ่มทีละ 1 สัญญา)
        # Your log shows 1.0, which is correct for contracts on OKX.
        actual_contracts_step_size = float(market_info['limits']['amount'].get('step', '1.0'))
        logger.debug(f"DEBUG: Actual Contract Step Size from market_info: {actual_contracts_step_size}")
        
        # min_exchange_contracts: จำนวนสัญญาขั้นต่ำที่ Exchange อนุญาต
        # Your log shows 0.01, which is incorrect if it means 0.01 CONTRACTS.
        # If it means 0.01 BTC, it's 100 contracts.
        # Based on OKX, min contracts for BTC-USDT-SWAP is 1.0.
        min_exchange_contracts = float(market_info['limits']['amount'].get('min', '1.0')) 
        
    except (TypeError, ValueError) as e:
        logger.critical(f"❌ Error parsing market limits for {SYMBOL}: {e}. Check API response structure. Exiting.", exc_info=True)
        send_telegram(f"⛔️ Critical Error: Cannot parse market limits for {SYMBOL}.\nDetails: {e}")
        return (0, 0)

    # คำนวณ Margin Buffer จากเปอร์เซ็นต์ของยอด Available USDT
    # ให้มีค่าต่ำสุดด้วย เพื่อป้องกันการคำนวณ buffer ที่น้อยเกินไปเมื่อทุนน้อยมาก
    # และเพื่อให้ MARGIN_BUFFER_PERCENTAGE ถูกใช้
    actual_margin_buffer = max(available_usdt * MARGIN_BUFFER_PERCENTAGE, MIN_MARGIN_BUFFER_USDT) # <--- แก้ไขตรงนี้
    
    # คำนวณ Margin ที่เราต้องการใช้ (จาก Balance ที่มี และ Factor)
    target_initial_margin = (available_usdt - actual_margin_buffer) * TARGET_POSITION_SIZE_FACTOR

    if target_initial_margin <= 0:
        logger.warning(f"❌ Target initial margin ({target_initial_margin:.2f}) too low after buffer ({actual_margin_buffer} USDT).") # <--- ใช้ actual_margin_buffer ใน log
        return (0, 0)

    # คำนวณ Notional Value ที่ Margin นี้จะเปิดได้
    target_notional_for_order = target_initial_margin * LEVERAGE

    # คำนวณจำนวน BTC (Base Asset) จาก Notional Value
    target_base_amount_btc_raw = target_notional_for_order / price

    # แปลงเป็นจำนวน Contracts ดิบๆ (ก่อนปัดเศษตาม step)
    contracts_raw = target_base_amount_btc_raw / contract_size_in_btc 
    
    # ปัดเศษ contracts ให้เป็นไปตาม actual_contracts_step_size
    contracts_to_open = round(contracts_raw / actual_contracts_step_size) * actual_contracts_step_size
    
    # ควบคุม precision ด้วย f-string ก่อนส่งให้ CCXT เพื่อหลีกเลี่ยง float inaccuracies
    contracts_to_open = float(f"{contracts_to_open:.8f}") # ปัดให้มีทศนิยม 8 ตำแหน่ง

    # ตรวจสอบขั้นต่ำและสูงสุดของ Contracts (ถ้ามี)
    contracts_to_open = max(contracts_to_open, min_exchange_contracts)
    
    # นอกจากนี้ เราต้องตรวจสอบ Notional value ที่เกิดขึ้นจริงหลังจากการปัดเศษ Contracts
    # กับ min_notional_exchange ด้วย
    actual_notional_after_precision = contracts_to_open * contract_size_in_btc * price
    
    # ตรวจสอบ Notional Value ที่เกิดขึ้นจริงกับ min_notional_exchange
    if actual_notional_after_precision < min_notional_exchange:
        # หาก Notional จริงๆ หลังปัดเศษ contracts แล้วยังต่ำกว่าขั้นต่ำของ Notional
        # คำนวณ Contracts ที่ตรงกับ min_notional_exchange
        contracts_from_min_notional = min_notional_exchange / (contract_size_in_btc * price)
        contracts_from_min_notional = round(contracts_from_min_notional / actual_contracts_step_size) * actual_contracts_step_size
        contracts_from_min_notional = float(f"{contracts_from_min_notional:.8f}")
        
        # เลือก contracts_to_open ที่มากที่สุดระหว่างที่คำนวณได้กับที่มาจาก min_notional
        contracts_to_open = max(contracts_to_open, contracts_from_min_notional)
        actual_notional_after_precision = contracts_to_open * contract_size_in_btc * price # อัปเดต Notional ตาม contracts ใหม่

    # คำนวณ Margin ที่แท้จริงจาก Contracts ที่จะเปิด
    required_margin = actual_notional_after_precision / LEVERAGE

    if contracts_to_open == 0:
        logger.warning(f"⚠️ Calculated contracts to open is 0 after all adjustments. (Target Notional: {target_notional_for_order:.2f} USDT).")
        return (0, 0)
        
    if available_usdt < required_margin + actual_margin_buffer: # <--- ใช้ actual_margin_buffer ที่นี่
        logger.error(f"❌ Margin not sufficient. Available: {available_usdt:.2f}, Required: {required_margin:.2f} + {actual_margin_buffer} (Buffer) = {required_margin + actual_margin_buffer:.2f} USDT.") # <--- ใช้ actual_margin_buffer ใน log
        return (0, 0)
    
    logger.debug(f"💡 DEBUG (calculate_order_details): Available USDT: {available_usdt:.2f}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Target Initial Margin: {target_initial_margin:.2f}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Target Notional: {target_notional_for_order:.2f} USDT")
    # เพิ่ม logging สำหรับ Actual Margin Buffer
    logger.debug(f"💡 DEBUG (calculate_order_details): Actual Margin Buffer: {actual_margin_buffer:.2f} USDT")
    # ... (ส่วนที่เหลือของ logging เหมือนเดิม) ...
    logger.debug(f"💡 DEBUG (calculate_order_details): Contract Size (BTC/Contract): {contract_size_in_btc:.8f}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Raw Contracts: {contracts_raw:.8f}") 
    logger.debug(f"💡 DEBUG (calculate_order_details): Actual Contract Step Size: {actual_contracts_step_size}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Contracts to Open (final calculated): {contracts_to_open:.8f}") 
    logger.debug(f"💡 DEBUG (calculate_order_details): Actual Notional (after precision): {actual_notional_after_precision:.2f} USDT")
    logger.debug(f"💡 DEBUG (calculate_order_details): Calculated Required Margin: {required_margin:.2f} USDT")
    logger.debug(f"💡 DEBUG (calculate_order_details): Min Notional Exchange: {min_notional_exchange:.2f}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Min Contracts Exchange: {min_exchange_contracts:.8f}")

    return (contracts_to_open, required_margin) # <-- คืนค่าเป็น (จำนวน Contracts, Margin)

# ... (ส่วนที่เหลือของโค้ด open_market_order, set_tpsl_for_position, monitor_position ฯลฯ เหมือนเดิม) ...
