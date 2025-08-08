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

# 1. การตั้งค่าพื้นฐาน (CONFIGURATION)

# ========================================================================

# — API Keys & Credentials (ดึงจาก Environment Variables เพื่อความปลอดภัย) —

# ตรวจสอบให้แน่ใจว่าได้ตั้งค่าใน Environment Variables: OKX_API_KEY, OKX_SECRET, OKX_PASSWORD

API_KEY = os.getenv(‘OKX_API_KEY’, ‘YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING’)
SECRET = os.getenv(‘OKX_SECRET’, ‘YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING’)
PASSWORD = os.getenv(‘OKX_PASSWORD’, ‘YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING’)  # Passphrase for OKX

# — Trade Parameters —

SYMBOL = ‘BTC-USDT-SWAP’
LEVERAGE = 10
TP_DISTANCE_POINTS = 250
SL_DISTANCE_POINTS = 400
PORTFOLIO_PERCENTAGE = 0.50  # ลดเหลือ 50% เพื่อความปลอดภัย

# — Telegram Settings —

TELEGRAM_TOKEN = os.getenv(‘TELEGRAM_TOKEN’, ‘YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING’)
TELEGRAM_CHAT_ID = os.getenv(‘TELEGRAM_CHAT_ID’, ‘YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING’)

# ========================================================================

# 2. การตั้งค่า Logging

# ========================================================================

logging.basicConfig(
level=logging.INFO,
format=’%(asctime)s - %(levelname)s - %(message)s’,
handlers=[
logging.FileHandler(‘test_bot.log’, encoding=‘utf-8’),
logging.StreamHandler(sys.stdout)
]
)
logger = logging.getLogger(**name**)

# ========================================================================

# 3. Global Variables

# ========================================================================

exchange = None
market_info = None
current_position_details = None

# ========================================================================

# 4. Exchange Setup

# ========================================================================

def setup_exchange():
global exchange, market_info
try:
if not all([API_KEY, SECRET, PASSWORD]) or API_KEY == ‘YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING’:
raise ValueError(“กรุณาตั้งค่า API Keys ใน Environment Variables หรือแก้ไขในโค้ดโดยตรง”)

```
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
    
    # ตั้งค่า Leverage
    try:
        result = exchange.set_leverage(LEVERAGE, SYMBOL, params={'mgnMode': 'cross'})
        logger.info(f"✅ ตั้งค่า Leverage เป็น {LEVERAGE}x สำเร็จ")
    except Exception as e:
        logger.warning(f"⚠️ ไม่สามารถตั้งค่า Leverage ได้: {e}")
        
except Exception as e:
    logger.critical(f"❌ ไม่สามารถเชื่อมต่อ Exchange ได้: {e}")
    raise
```

# ========================================================================

# 5. Telegram Functions

# ========================================================================

def send_telegram(msg: str):
if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == ‘YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING’:
logger.warning(“⚠️ ไม่ได้ตั้งค่า Telegram Token - ข้ามการส่งข้อความ”)
return

```
try:
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    logger.info(f"📤 ส่ง Telegram: {msg[:50]}...")
except Exception as e:
    logger.error(f"❌ ส่ง Telegram ไม่ได้: {e}")
```

# ========================================================================

# 6. Portfolio & Position Functions

# ========================================================================

def get_portfolio_balance() -> float:
“”“ดึงยอดคงเหลือ USDT”””
try:
balance_data = exchange.fetch_balance(params={‘type’: ‘trade’})

```
    usdt_balance = 0.0
    if 'USDT' in balance_data and 'free' in balance_data['USDT']:
        usdt_balance = float(balance_data['USDT']['free'])
    else:
        # ใช้ OKX raw data
        okx_balance_info = balance_data.get('info', {}).get('data', [])
        for account in okx_balance_info:
            if account.get('ccy') == 'USDT' and account.get('type') == 'TRADE':
                usdt_balance = float(account.get('availBal', 0.0))
                break
    
    logger.info(f"💰 ยอดคงเหลือ USDT: {usdt_balance:,.2f}")
    return usdt_balance
    
except Exception as e:
    logger.error(f"❌ ไม่สามารถดึงยอดคงเหลือได้: {e}")
    return 0.0
```

def get_current_position():
“”“ตรวจสอบโพซิชันปัจจุบัน”””
try:
positions = exchange.fetch_positions([SYMBOL])

```
    for pos in positions:
        pos_info = pos.get('info', {})
        pos_amount_str = pos_info.get('pos', '0')
        
        if float(pos_amount_str) != 0:
            pos_amount = abs(float(pos_amount_str))
            side = 'long' if float(pos_amount_str) > 0 else 'short'
            entry_price = float(pos_info.get('avgPx', 0.0))
            unrealized_pnl = float(pos_info.get('upl', 0.0))
            
            return {
                'side': side,
                'size': pos_amount,
                'entry_price': entry_price,
                'unrealized_pnl': unrealized_pnl
            }
    
    return None
    
except Exception as e:
    logger.error(f"❌ ไม่สามารถดึงข้อมูลโพซิชันได้: {e}")
    return None
```

# ========================================================================

# 7. Order Calculation Functions

# ========================================================================

def calculate_order_size(available_usdt: float, price: float) -> float:
“”“คำนวณขนาดออเดอร์จากเปอร์เซ็นต์ของพอร์ต โดยคำนวณ margin ที่ต้องใช้”””
try:
# OKX BTC-USDT-SWAP: 1 contract = 0.0001 BTC
contract_size_btc = 0.0001

```
    # คำนวณ margin factor สำหรับ OKX (ประมาณ 6.8% สำหรับ 15x leverage)
    # จากข้อมูลในโค้ดเดิม: 92.11 USDT margin สำหรับ 1349.69 USDT notional
    margin_factor = 0.06824
    
    # เก็บ buffer 10% เพื่อความปลอดภัย
    usable_usdt = available_usdt * 0.9
    
    # คำนวณ notional value สูงสุดที่สามารถเปิดได้
    max_notional = usable_usdt / margin_factor
    
    # คำนวณเป้าหมาย (ใช้เปอร์เซ็นต์ของพอร์ต)
    target_notional = min(max_notional, available_usdt * PORTFOLIO_PERCENTAGE / margin_factor)
    
    # คำนวณจำนวน contracts
    target_btc = target_notional / price
    contracts = target_btc / contract_size_btc
    
    # ปัดเศษลง
    contracts = math.floor(contracts)
    
    if contracts < 1:
        logger.warning(f"⚠️ จำนวน contracts ต่ำเกินไป: {contracts}")
        return 0
    
    # คำนวณค่าจริงหลังปัดเศษ
    actual_notional = contracts * contract_size_btc * price
    required_margin = actual_notional * margin_factor
    
    # ตรวจสอบว่า margin เพียงพอหรือไม่
    if required_margin > usable_usdt:
        logger.warning(f"⚠️ Margin ไม่เพียงพอ: ต้องการ {required_margin:.2f} มี {usable_usdt:.2f}")
        # ลดจำนวน contracts
        contracts = math.floor(usable_usdt / margin_factor / contract_size_btc / price)
        actual_notional = contracts * contract_size_btc * price
        required_margin = actual_notional * margin_factor
    
    logger.info(f"📊 คำนวณออเดอร์:")
    logger.info(f"   - Available USDT: {available_usdt:,.2f}")
    logger.info(f"   - Usable USDT (90%): {usable_usdt:,.2f}")
    logger.info(f"   - Contracts: {contracts}")
    logger.info(f"   - Notional Value: {actual_notional:,.2f} USDT")
    logger.info(f"   - Required Margin: {required_margin:,.2f} USDT")
    logger.info(f"   - Margin Ratio: {(required_margin/available_usdt)*100:.1f}%")
    
    return float(contracts)
    
except Exception as e:
    logger.error(f"❌ คำนวณขนาดออเดอร์ไม่ได้: {e}")
    return 0
```

def check_margin_requirements(contracts: float, price: float, available_usdt: float) -> bool:
“”“ตรวจสอบว่า margin เพียงพอหรือไม่”””
try:
contract_size_btc = 0.0001
margin_factor = 0.06824

```
    notional = contracts * contract_size_btc * price
    required_margin = notional * margin_factor
    
    logger.info(f"🔍 ตรวจสอบ Margin:")
    logger.info(f"   - Contracts: {contracts}")
    logger.info(f"   - Notional: {notional:,.2f} USDT")
    logger.info(f"   - Required Margin: {required_margin:,.2f} USDT")
    logger.info(f"   - Available: {available_usdt:,.2f} USDT")
    logger.info(f"   - Margin Ratio: {(required_margin/available_usdt)*100:.1f}%")
    
    if required_margin > available_usdt * 0.95:  # เก็บ buffer 5%
        logger.warning("⚠️ Margin ไม่เพียงพอ!")
        return False
    
    return True
    
except Exception as e:
    logger.error(f"❌ ตรวจสอบ margin ไม่ได้: {e}")
    return False
```

# ========================================================================

# 8. Trading Functions

# ========================================================================

def open_long_position(current_price: float) -> bool:
“”“เปิดโพซิชัน Long”””
try:
balance = get_portfolio_balance()
if balance <= 0:
logger.error(“❌ ยอดคงเหลือไม่เพียงพอ”)
return False

```
    contracts = calculate_order_size(balance, current_price)
    if contracts <= 0:
        logger.error("❌ คำนวณขนาดออเดอร์ไม่ได้")
        return False
    
    # ตรวจสอบ margin ก่อนเปิดออเดอร์
    if not check_margin_requirements(contracts, current_price, balance):
        logger.error("❌ Margin ไม่เพียงพอสำหรับการเปิดออเดอร์")
        send_telegram(f"❌ <b>Margin ไม่เพียงพอ!</b>\n"
                     f"📊 ต้องการ contracts: {contracts}\n"
                     f"💰 ยอดคงเหลือ: {balance:,.2f} USDT\n"
                     f"💡 ลองลดเปอร์เซ็นต์การใช้พอร์ต")
        return False
    
    logger.info(f"🚀 กำลังเปิด Long {contracts} contracts ที่ราคา {current_price:,.1f}")
    
    # สร้างออเดอร์ Market Buy
    order = exchange.create_market_order(
        symbol=SYMBOL,
        side='buy',
        amount=contracts,
        params={
            'tdMode': 'cross',
        }
    )
    
    if order and order.get('id'):
        logger.info(f"✅ เปิด Long สำเร็จ: Order ID {order.get('id')}")
        send_telegram(f"🚀 <b>เปิด Long สำเร็จ!</b>\n"
                     f"📊 Contracts: {contracts}\n"
                     f"💰 ราคาเข้า: {current_price:,.1f}\n"
                     f"🆔 Order ID: {order.get('id')}")
        
        # รอให้ออเดอร์ fill และตั้ง TP/SL
        time.sleep(3)
        return set_tp_sl_for_long(current_price, contracts)
    else:
        logger.error("❌ ไม่สามารถเปิด Long ได้")
        return False
        
except Exception as e:
    logger.error(f"❌ เกิดข้อผิดพลาดในการเปิด Long: {e}")
    send_telegram(f"❌ <b>เปิด Long ไม่สำเร็จ!</b>\nError: {str(e)[:200]}")
    return False
```

def set_tp_sl_for_long(entry_price: float, contracts: float) -> bool:
“”“ตั้ง TP/SL สำหรับโพซิชัน Long”””
try:
tp_price = entry_price + TP_DISTANCE_POINTS
sl_price = entry_price - SL_DISTANCE_POINTS

```
    logger.info(f"📋 กำลังตั้ง TP/SL:")
    logger.info(f"   - TP: {tp_price:,.1f} (+{TP_DISTANCE_POINTS} points)")
    logger.info(f"   - SL: {sl_price:,.1f} (-{SL_DISTANCE_POINTS} points)")
    
    current_price = get_current_price()
    if not current_price:
        logger.error("❌ ไม่สามารถดึงราคาปัจจุบันได้")
        return False
    
    # ตั้ง Take Profit
    try:
        tp_order = exchange.create_order(
            symbol=SYMBOL,
            type='TAKE_PROFIT_MARKET',
            side='sell',
            amount=contracts,
            price=current_price,
            params={
                'triggerPrice': tp_price,
                'tdMode': 'cross',
                'reduceOnly': True,
            }
        )
        logger.info(f"✅ ตั้ง TP สำเร็จ: {tp_price:,.1f}")
    except Exception as e:
        logger.error(f"❌ ตั้ง TP ไม่สำเร็จ: {e}")
        return False
    
    # ตั้ง Stop Loss
    try:
        sl_order = exchange.create_order(
            symbol=SYMBOL,
            type='STOP_LOSS_MARKET',
            side='sell',
            amount=contracts,
            price=current_price,
            params={
                'triggerPrice': sl_price,
                'tdMode': 'cross',
                'reduceOnly': True,
            }
        )
        logger.info(f"✅ ตั้ง SL สำเร็จ: {sl_price:,.1f}")
    except Exception as e:
        logger.error(f"❌ ตั้ง SL ไม่สำเร็จ: {e}")
        return False
    
    send_telegram(f"📋 <b>ตั้ง TP/SL สำเร็จ!</b>\n"
                 f"🎯 TP: {tp_price:,.1f} (+{TP_DISTANCE_POINTS})\n"
                 f"🛡️ SL: {sl_price:,.1f} (-{SL_DISTANCE_POINTS})")
    return True
    
except Exception as e:
    logger.error(f"❌ ตั้ง TP/SL ไม่สำเร็จ: {e}")
    return False
```

def get_current_price() -> float:
“”“ดึงราคาปัจจุบัน”””
try:
ticker = exchange.fetch_ticker(SYMBOL)
return float(ticker[‘last’])
except Exception as e:
logger.error(f”❌ ดึงราคาไม่ได้: {e}”)
return 0.0

# ========================================================================

# 9. Main Function

# ========================================================================

def main():
“”“ฟังก์ชันหลัก - ทดสอบเปิด Long ทันที”””
try:
logger.info(“🤖 เริ่มต้น OKX Test Bot”)

```
    # Setup Exchange
    setup_exchange()
    
    # ตรวจสอบโพซิชันปัจจุบัน
    current_pos = get_current_position()
    if current_pos:
        logger.info(f"⚠️ มีโพซิชันอยู่แล้ว: {current_pos['side'].upper()} {current_pos['size']} contracts")
        send_telegram(f"⚠️ <b>มีโพซิชันอยู่แล้ว!</b>\n"
                     f"📊 {current_pos['side'].upper()}: {current_pos['size']} contracts\n"
                     f"💰 Entry: {current_pos['entry_price']:,.1f}\n"
                     f"📈 PnL: {current_pos['unrealized_pnl']:,.2f} USDT")
        return
    
    # ดึงราคาปัจจุบัน
    current_price = get_current_price()
    if not current_price:
        logger.error("❌ ไม่สามารถดึงราคาปัจจุบันได้")
        return
    
    logger.info(f"💰 ราคา {SYMBOL} ปัจจุบัน: {current_price:,.1f}")
    
    # ดึงยอดคงเหลือ
    balance = get_portfolio_balance()
    if balance <= 0:
        logger.error("❌ ยอดคงเหลือไม่เพียงพอ")
        return
    
    # แจ้งข้อมูลการทดสอบ
    target_usdt = balance * PORTFOLIO_PERCENTAGE
    # คำนวณ margin ที่จะใช้จริง
    margin_factor = 0.06824
    estimated_margin = (target_usdt / margin_factor) * margin_factor
    
    send_telegram(f"🧪 <b>เริ่มทดสอบบอท OKX</b>\n"
                 f"💰 ยอดคงเหลือ: {balance:,.2f} USDT\n"
                 f"📊 เป้าหมาย: {PORTFOLIO_PERCENTAGE*100}% ของพอร์ต\n"
                 f"🔧 Margin ประมาณ: {estimated_margin:,.2f} USDT\n"
                 f"💰 ราคา BTC: {current_price:,.1f}\n"
                 f"🚀 กำลังเปิด Long...")
    
    # เปิด Long ทันที
    success = open_long_position(current_price)
    
    if success:
        logger.info("✅ ทดสอบสำเร็จ! โพซิชันถูกเปิดและตั้ง TP/SL แล้ว")
        
        # ตรวจสอบโพซิชันหลังเปิด
        time.sleep(2)
        final_pos = get_current_position()
        if final_pos:
            send_telegram(f"✅ <b>ทดสอบสำเร็จ!</b>\n"
                         f"📊 โพซิชัน: {final_pos['side'].upper()}\n"
                         f"📈 ขนาด: {final_pos['size']} contracts\n"
                         f"💰 Entry: {final_pos['entry_price']:,.1f}\n"
                         f"📊 PnL: {final_pos['unrealized_pnl']:,.2f} USDT")
    else:
        logger.error("❌ ทดสอบไม่สำเร็จ")
        send_telegram("❌ <b>ทดสอบไม่สำเร็จ!</b>\nกรุณาตรวจสอบ logs")
    
except Exception as e:
    logger.critical(f"❌ เกิดข้อผิดพลาดร้ายแรง: {e}")
    send_telegram(f"❌ <b>เกิดข้อผิดพลาดร้ายแรง!</b>\n{str(e)[:200]}")
```

# ========================================================================

# 10. Entry Point

# ========================================================================

if **name** == ‘**main**’:
main()
