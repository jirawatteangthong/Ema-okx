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

API_KEY = “YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING”
SECRET = “YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING”
PASSWORD = “YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING”

SYMBOL = “BTC-USDT-SWAP”
LEVERAGE = 10
TP_DISTANCE_POINTS = 250
SL_DISTANCE_POINTS = 400
PORTFOLIO_PERCENTAGE = 0.80

TELEGRAM_TOKEN = “YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING”
TELEGRAM_CHAT_ID = “YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING”

# ========================================================================

# 2. LOGGING

# ========================================================================

logging.basicConfig(
level=logging.INFO,
format=”%(asctime)s - %(levelname)s - %(message)s”,
handlers=[
logging.FileHandler(“test_bot.log”, encoding=“utf-8”),
logging.StreamHandler(sys.stdout)
]
)
logger = logging.getLogger(**name**)

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
if not all([API_KEY, SECRET, PASSWORD]) or API_KEY == “YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING”:
raise ValueError(“กรุณาตั้งค่า API Keys ใน Environment Variables หรือแก้ไขในโค้ดโดยตรง”)

```
    exchange = ccxt.okx({
        "apiKey": API_KEY,
        "secret": SECRET,
        "password": PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "adjustForTimeDifference": True,
        },
        "verbose": False,
        "timeout": 30000,
    })

    exchange.set_sandbox_mode(False)
    exchange.load_markets()
    market_info = exchange.market(SYMBOL)
    if not market_info:
        raise ValueError(f"ไม่พบข้อมูลตลาดสำหรับ {SYMBOL}")

    logger.info(f"✅ เชื่อมต่อกับ OKX Exchange สำเร็จ")

    try:
        exchange.set_leverage(LEVERAGE, SYMBOL, params={"mgnMode": "cross"})
        logger.info(f"✅ ตั้งค่า Leverage เป็น {LEVERAGE}x สำเร็จ")
    except Exception as e:
        logger.warning(f"⚠️ ไม่สามารถตั้งค่า Leverage ได้: {e}")

except Exception as e:
    logger.critical(f"❌ ไม่สามารถเชื่อมต่อ Exchange ได้: {e}")
    raise
```

# ========================================================================

# 5. TELEGRAM

# ========================================================================

def send_telegram(msg: str):
if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == “YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING”:
logger.warning(“⚠️ ไม่ได้ตั้งค่า Telegram Token - ข้ามการส่งข้อความ”)
return
try:
url = f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage”
params = {“chat_id”: TELEGRAM_CHAT_ID, “text”: msg, “parse_mode”: “HTML”}
requests.get(url, params=params, timeout=10)
logger.info(f”📤 ส่ง Telegram: {msg[:50]}…”)
except Exception as e:
logger.error(f”❌ ส่ง Telegram ไม่ได้: {e}”)

# ========================================================================

# 6. DETAILED BALANCE & MARGIN ANALYSIS

# ========================================================================

def get_detailed_balance_info():
“”“ดึงข้อมูล Balance และ Margin แบบละเอียด”””
try:
logger.info(”=” * 60)
logger.info(“🔍 DETAILED BALANCE & MARGIN ANALYSIS”)
logger.info(”=” * 60)

```
    # 1. ดึงข้อมูล Balance
    balance_data = exchange.fetch_balance(params={"type": "trade"})
    logger.info(f"📋 Raw Balance Data: {json.dumps(balance_data, indent=2)}")
    
    # 2. แยกวิเคราะห์ USDT
    usdt_info = balance_data.get("USDT", {})
    logger.info(f"💰 USDT Info from balance_data['USDT']: {usdt_info}")
    
    # 3. ข้อมูลจาก OKX raw data
    okx_raw_data = balance_data.get("info", {})
    logger.info(f"🏛️  OKX Raw Info: {json.dumps(okx_raw_data, indent=2)}")
    
    # 4. วิเคราะห์ account data
    account_data = okx_raw_data.get("data", [])
    logger.info(f"📊 Number of accounts: {len(account_data)}")
    
    total_equity = 0
    available_balance = 0
    used_balance = 0
    
    for i, acc in enumerate(account_data):
        logger.info(f"📈 Account {i+1}: {json.dumps(acc, indent=2)}")
        
        if acc.get("ccy") == "USDT":
            eq = float(acc.get("eq", 0))  # Total equity
            avail = float(acc.get("availBal", 0))  # Available balance
            frozen = float(acc.get("frozenBal", 0))  # Used/frozen balance
            
            total_equity += eq
            available_balance += avail
            used_balance += frozen
            
            logger.info(f"💎 USDT Account Details:")
            logger.info(f"   - Total Equity (eq): {eq:,.2f} USDT")
            logger.info(f"   - Available Balance (availBal): {avail:,.2f} USDT")  
            logger.info(f"   - Frozen Balance (frozenBal): {frozen:,.2f} USDT")
            logger.info(f"   - Account Type: {acc.get('type', 'N/A')}")
    
    # 5. สรุปข้อมูล
    logger.info("=" * 60)
    logger.info("📊 BALANCE SUMMARY:")
    logger.info(f"   💰 Total USDT Equity: {total_equity:,.2f}")
    logger.info(f"   ✅ Available for Trading: {available_balance:,.2f}")
    logger.info(f"   🔒 Used/Frozen: {used_balance:,.2f}")
    logger.info("=" * 60)
    
    return available_balance, total_equity, used_balance
    
except Exception as e:
    logger.error(f"❌ Error in detailed balance analysis: {e}")
    import traceback
    logger.error(f"🔥 Full traceback: {traceback.format_exc()}")
    return 0, 0, 0
```

def analyze_margin_requirements(contracts: float, price: float):
“”“วิเคราะห์ Margin Requirements แบบละเอียด”””
try:
logger.info(”=” * 60)
logger.info(“🔍 MARGIN REQUIREMENTS ANALYSIS”)
logger.info(”=” * 60)

```
    # 1. ข้อมูลพื้นฐาน
    contract_size_btc = 0.0001
    notional_value = contracts * contract_size_btc * price
    
    logger.info(f"📊 Basic Calculation:")
    logger.info(f"   - Contracts: {contracts}")
    logger.info(f"   - Contract Size: {contract_size_btc} BTC")
    logger.info(f"   - Price: {price:,.2f} USDT")
    logger.info(f"   - Notional Value: {notional_value:,.2f} USDT")
    
    # 2. คำนวณ Margin ตามวิธีต่างๆ
    logger.info(f"🧮 Margin Calculations:")
    
    # วิธีที่ 1: ใช้ Leverage
    margin_by_leverage = notional_value / LEVERAGE
    logger.info(f"   📐 Method 1 (Notional/Leverage): {margin_by_leverage:,.2f} USDT")
    
    # วิธีที่ 2: ใช้ Margin Factor (จากโค้ดเดิม)
    margin_factor = 0.06824
    margin_by_factor = notional_value * margin_factor
    logger.info(f"   📐 Method 2 (Margin Factor): {margin_by_factor:,.2f} USDT")
    
    # วิธีที่ 3: คำนวณตาม OKX formula
    # Initial Margin Rate = 1/Leverage + Maintenance Margin Rate
    maintenance_margin_rate = 0.005  # 0.5% for BTC
    initial_margin_rate = (1/LEVERAGE) + maintenance_margin_rate
    margin_by_okx_formula = notional_value * initial_margin_rate
    logger.info(f"   📐 Method 3 (OKX Formula): {margin_by_okx_formula:,.2f} USDT")
    
    # 4. ดึงข้อมูล Position Requirements จาก Exchange
    try:
        # ลองดู account info
        account_info = exchange.fetch_account_data()
        logger.info(f"🏦 Account Info: {json.dumps(account_info, indent=2)}")
    except:
        logger.info("⚠️ Cannot fetch account info")
    
    # 5. ลองดู Market Info
    logger.info(f"🏪 Market Info for {SYMBOL}:")
    logger.info(f"   - Contract Size: {market_info.get('contractSize', 'N/A')}")
    logger.info(f"   - Limits: {market_info.get('limits', 'N/A')}")
    logger.info(f"   - Precision: {market_info.get('precision', 'N/A')}")
    
    logger.info("=" * 60)
    
    return margin_by_leverage, margin_by_factor, margin_by_okx_formula
    
except Exception as e:
    logger.error(f"❌ Error in margin analysis: {e}")
    import traceback
    logger.error(f"🔥 Full traceback: {traceback.format_exc()}")
    return 0, 0, 0
```

# ========================================================================

# 7. PORTFOLIO FUNCTIONS

# ========================================================================

def get_portfolio_balance() -> float:
“”“ดึงยอดคงเหลือ USDT”””
try:
balance_data = exchange.fetch_balance(params={“type”: “trade”})
usdt_balance = 0.0

```
    if "USDT" in balance_data and "free" in balance_data["USDT"]:
        usdt_balance = float(balance_data["USDT"]["free"])
    else:
        okx_balance_info = balance_data.get("info", {}).get("data", [])
        for account in okx_balance_info:
            if account.get("ccy") == "USDT":
                usdt_balance = float(account.get("availBal", 0.0))
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
for pos in positions:
pos_info = pos.get(“info”, {})
pos_amount_str = pos_info.get(“pos”, “0”)

```
        if float(pos_amount_str) != 0:
            return {
                "side": "long" if float(pos_amount_str) > 0 else "short",
                "size": abs(float(pos_amount_str)),
                "entry_price": float(pos_info.get("avgPx", 0.0)),
                "unrealized_pnl": float(pos_info.get("upl", 0.0))
            }
    return None
except Exception as e:
    logger.error(f"❌ ไม่สามารถดึงข้อมูลโพซิชันได้: {e}")
    return None
```

# ========================================================================

# 8. ORDER FUNCTIONS WITH DETAILED ANALYSIS

# ========================================================================

def calculate_order_size(available_usdt: float, price: float) -> float:
try:
logger.info(”=” * 60)
logger.info(“🧮 ORDER SIZE CALCULATION”)
logger.info(”=” * 60)

```
    # พารามิเตอร์พื้นฐาน
    contract_size_btc = 0.0001
    target_usdt = available_usdt * PORTFOLIO_PERCENTAGE
    
    logger.info(f"📊 Input Parameters:")
    logger.info(f"   - Available USDT: {available_usdt:,.2f}")
    logger.info(f"   - Portfolio %: {PORTFOLIO_PERCENTAGE*100}%")
    logger.info(f"   - Target USDT: {target_usdt:,.2f}")
    logger.info(f"   - Leverage: {LEVERAGE}x")
    logger.info(f"   - Current Price: {price:,.2f}")
    logger.info(f"   - Contract Size: {contract_size_btc} BTC")
    
    # วิธีที่ 1: คำนวณแบบง่าย (ใช้ target_usdt เป็น margin)
    margin_method1 = target_usdt
    notional_method1 = margin_method1 * LEVERAGE
    btc_amount_method1 = notional_method1 / price
    contracts_method1 = math.floor(btc_amount_method1 / contract_size_btc)
    
    logger.info(f"🔄 Method 1 (Target USDT as Margin):")
    logger.info(f"   - Margin: {margin_method1:,.2f} USDT")
    logger.info(f"   - Notional: {notional_method1:,.2f} USDT")
    logger.info(f"   - BTC Amount: {btc_amount_method1:.6f} BTC")
    logger.info(f"   - Contracts: {contracts_method1}")
    
    # วิธีที่ 2: คำนวณแบบโค้ดเดิม (leverage กับ notional)
    target_usdt_with_leverage = target_usdt * LEVERAGE
    target_btc_method2 = target_usdt_with_leverage / price
    contracts_method2 = math.floor(target_btc_method2 / contract_size_btc)
    
    logger.info(f"🔄 Method 2 (Original Code Logic):")
    logger.info(f"   - Target USDT * Leverage: {target_usdt_with_leverage:,.2f}")
    logger.info(f"   - BTC Amount: {target_btc_method2:.6f} BTC")
    logger.info(f"   - Contracts: {contracts_method2}")
    
    # วิธีที่ 3: คำนวณจาก available balance โดยตรง
    safety_factor = 0.8  # ใช้แค่ 80% เพื่อความปลอดภัย
    max_margin = available_usdt * safety_factor
    max_notional = max_margin * LEVERAGE
    max_btc = max_notional / price
    contracts_method3 = math.floor(max_btc / contract_size_btc)
    
    logger.info(f"🔄 Method 3 (Conservative Approach):")
    logger.info(f"   - Safety Factor: {safety_factor*100}%")
    logger.info(f"   - Max Margin: {max_margin:,.2f} USDT")
    logger.info(f"   - Max Notional: {max_notional:,.2f} USDT")
    logger.info(f"   - BTC Amount: {max_btc:.6f} BTC")
    logger.info(f"   - Contracts: {contracts_method3}")
    
    # เลือกใช้วิธีที่ปลอดภัยที่สุด
    final_contracts = min(contracts_method1, contracts_method2, contracts_method3)
    
    if final_contracts < 1:
        logger.warning(f"⚠️ จำนวน contracts ต่ำเกินไป: {final_contracts}")
        return 0
    
    # คำนวณค่าจริงหลังจากเลือก contracts
    final_notional = final_contracts * contract_size_btc * price
    final_margin_required = final_notional / LEVERAGE
    
    logger.info(f"✅ FINAL CALCULATION:")
    logger.info(f"   - Selected Contracts: {final_contracts}")
    logger.info(f"   - Final Notional: {final_notional:,.2f} USDT")
    logger.info(f"   - Final Margin Required: {final_margin_required:,.2f} USDT")
    logger.info(f"   - Margin Usage: {(final_margin_required/available_usdt)*100:.1f}%")
    
    # ตรวจสอบความปลอดภัย
    if final_margin_required > available_usdt * 0.9:  # ไม่ใช้เกิน 90%
        logger.error(f"❌ Margin ไม่ปลอดภัย! Required={final_margin_required:.2f} | Available={available_usdt:.2f}")
        return 0
    
    # วิเคราะห์ margin requirements แบบละเอียด
    analyze_margin_requirements(final_contracts, price)
    
    logger.info("=" * 60)
    
    return float(final_contracts)

except Exception as e:
    logger.error(f"❌ คำนวณขนาดออเดอร์ไม่ได้: {e}")
    import traceback
    logger.error(f"🔥 Full traceback: {traceback.format_exc()}")
    return 0
```

def get_current_price() -> float:
try:
ticker = exchange.fetch_ticker(SYMBOL)
return float(ticker[“last”])
except Exception as e:
logger.error(f”❌ ดึงราคาไม่ได้: {e}”)
return 0.0

# ========================================================================

# 9. TRADING WITH DETAILED LOGGING

# ========================================================================

def set_tp_sl(entry_price: float, contracts: float) -> bool:
“”“ตั้ง TP/SL สำหรับโพซิชัน Long”””
try:
tp_price = entry_price + TP_DISTANCE_POINTS
sl_price = entry_price - SL_DISTANCE_POINTS

```
    logger.info(f"📋 กำลังตั้ง TP/SL: TP={tp_price:,.1f} | SL={sl_price:,.1f}")
    
    current_price = get_current_price()
    if not current_price:
        logger.error("❌ ไม่สามารถดึงราคาปัจจุบันได้")
        return False
    
    # ตั้ง Take Profit
    try:
        tp_order = exchange.create_order(
            symbol=SYMBOL,
            type="TAKE_PROFIT_MARKET",
            side="sell",
            amount=contracts,
            price=current_price,
            params={
                "triggerPrice": tp_price,
                "tdMode": "cross",
                "reduceOnly": True,
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
            type="STOP_LOSS_MARKET",
            side="sell",
            amount=contracts,
            price=current_price,
            params={
                "triggerPrice": sl_price,
                "tdMode": "cross",
                "reduceOnly": True,
            }
        )
        logger.info(f"✅ ตั้ง SL สำเร็จ: {sl_price:,.1f}")
    except Exception as e:
        logger.error(f"❌ ตั้ง SL ไม่สำเร็จ: {e}")
        return False
    
    send_telegram(f"📋 <b>ตั้ง TP/SL สำเร็จ!</b>\n🎯 TP: {tp_price:,.1f} (+{TP_DISTANCE_POINTS})\n🛡️ SL: {sl_price:,.1f} (-{SL_DISTANCE_POINTS})")
    return True
    
except Exception as e:
    logger.error(f"❌ ตั้ง TP/SL ไม่สำเร็จ: {e}")
    return False
```

def open_long_position(current_price: float) -> bool:
try:
# 1. ดึงข้อมูล Balance แบบละเอียด
available_balance, total_equity, used_balance = get_detailed_balance_info()

```
    if available_balance <= 0:
        logger.error("❌ ยอดคงเหลือไม่เพียงพอ")
        return False

    # 2. คำนวณขนาดออเดอร์
    contracts = calculate_order_size(available_balance, current_price)
    if contracts <= 0:
        logger.error("❌ คำนวณขนาดออเดอร์ไม่ได้")
        return False

    # 3. ตรวจสอบขั้นสุดท้ายก่อนส่งออเดอร์
    logger.info("=" * 60)
    logger.info("🚀 FINAL ORDER PREPARATION")
    logger.info("=" * 60)
    
    final_notional = contracts * 0.0001 * current_price
    final_margin = final_notional / LEVERAGE
    
    logger.info(f"📋 Final Order Details:")
    logger.info(f"   - Symbol: {SYMBOL}")
    logger.info(f"   - Side: BUY (Long)")
    logger.info(f"   - Contracts: {contracts}")
    logger.info(f"   - Price: {current_price:,.1f}")
    logger.info(f"   - Notional Value: {final_notional:,.2f} USDT")
    logger.info(f"   - Required Margin: {final_margin:,.2f} USDT")
    logger.info(f"   - Available Balance: {available_balance:,.2f} USDT")
    logger.info(f"   - Margin Utilization: {(final_margin/available_balance)*100:.1f}%")

    # 4. ส่งออเดอร์
    logger.info(f"🚀 กำลังเปิด Long {contracts} contracts ที่ราคา {current_price:,.1f}")
    
    order_params = {"tdMode": "cross"}
    logger.info(f"📤 Order Params: {order_params}")
    
    order = exchange.create_market_order(
        symbol=SYMBOL,
        side="buy",
        amount=contracts,
        params=order_params
    )

    logger.info(f"📨 Order Response: {json.dumps(order, indent=2)}")

    if order and order.get("id"):
        logger.info(f"✅ เปิด Long สำเร็จ: Order ID {order.get('id')}")
        send_telegram(f"🚀 <b>เปิด Long สำเร็จ!</b>\n📊 Contracts: {contracts}\n💰 ราคาเข้า: {current_price:,.1f}\n🆔 Order ID: {order.get('id')}")
        
        # รอให้ออเดอร์ fill และตั้ง TP/SL
        time.sleep(3)
        
        # ตั้ง TP/SL
        tp_sl_success = set_tp_sl(current_price, contracts)
        if tp_sl_success:
            logger.info("✅ เปิด Long และตั้ง TP/SL สำเร็จ")
            return True
        else:
            logger.warning("⚠️ เปิด Long สำเร็จ แต่ตั้ง TP/SL ไม่สำเร็จ")
            send_telegram("⚠️ <b>เปิด Long สำเร็จ แต่ตั้ง TP/SL ไม่สำเร็จ!</b>\nกรุณาตั้งด้วยมือ")
            return True
    else:
        logger.error("❌ ไม่สามารถเปิด Long ได้")
        return False

except Exception as e:
    logger.error(f"❌ เกิดข้อผิดพลาดในการเปิด Long: {e}")
    import traceback
    logger.error(f"🔥 Full traceback: {traceback.format_exc()}")
    send_telegram(f"❌ <b>เปิด Long ไม่สำเร็จ!</b>\nError: {str(e)[:200]}")
    return False
```

# ========================================================================

# 10. MAIN

# ========================================================================

def main():
try:
logger.info(“🤖 เริ่มต้น OKX Test Bot”)
setup_exchange()

```
    current_pos = get_current_position()
    if current_pos:
        logger.info(f"⚠️ มีโพซิชันอยู่แล้ว: {current_pos['side'].upper()} {current_pos['size']} contracts")
        return

    current_price = get_current_price()
    if not current_price:
        logger.error("❌ ไม่สามารถดึงราคาปัจจุบันได้")
        return

    logger.info(f"💰 ราคา {SYMBOL} ปัจจุบัน: {current_price:,.1f}")
    
    success = open_long_position(current_price)
    
    if success:
        # ตรวจสอบโพซิชันหลังเปิด
        time.sleep(2)
        final_pos = get_current_position()
        if final_pos:
            send_telegram(f"✅ <b>ทดสอบสำเร็จ!</b>\n📊 โพซิชัน: {final_pos['side'].upper()}\n📈 ขนาด: {final_pos['size']} contracts\n💰 Entry: {final_pos['entry_price']:,.1f}\n📊 PnL: {final_pos['unrealized_pnl']:,.2f} USDT")
            logger.info("✅ ทดสอบสำเร็จ! โพซิชันถูกเปิดและตั้ง TP/SL แล้ว")
        else:
            logger.warning("⚠️ ไม่พบโพซิชันหลังจากเปิด")
    else:
        logger.error("❌ ทดสอบไม่สำเร็จ")

except Exception as e:
    logger.critical(f"❌ เกิดข้อผิดพลาดร้ายแรง: {e}")
    send_telegram(f"❌ <b>เกิดข้อผิดพลาดร้ายแรง!</b>\n{str(e)[:200]}")
```

# ========================================================================

# 11. ENTRY

# ========================================================================

if **name** == “**main**”:
main()
