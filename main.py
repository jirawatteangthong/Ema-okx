import os
import sys
import ccxt
import math
import logging
import traceback

# ---------------- CONFIG ----------------
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')

SYMBOL = 'BTC-USDT-SWAP'
PORTFOLIO_PERCENTAGE = 0.80  # ใช้กี่ % ของ margin
LEVERAGE = 15                # เลเวอเรจที่ต้องการ
MIN_CONTRACTS = 1            # ขั้นต่ำ 1 สัญญา

# ---------------- LOGGER ----------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("okx-long-bot")

# ---------------- INIT EXCHANGE ----------------
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

# ---------------- HELPERS ----------------
def validate_api_keys_and_load_markets():
    try:
        # 1) ตรวจว่า ENV ใส่มาจริง
        if (not API_KEY or not SECRET or not PASSWORD or
            'YOUR_OKX_API_KEY_HERE' in API_KEY or
            'YOUR_OKX_SECRET_HERE' in SECRET or
            'YOUR_OKX_PASSWORD_HERE' in PASSWORD):
            logger.error("❌ ยังไม่ได้ตั้งค่า OKX_API_KEY/OKX_SECRET/OKX_PASSWORD ใน ENV (.env)")
            sys.exit(1)

        # 2) โหลดตลาด
        markets = exchange.load_markets()
        if SYMBOL not in markets:
            logger.error(f"❌ ไม่รู้จักสัญลักษณ์ {SYMBOL} ในตลาด OKX")
            sys.exit(1)
        logger.debug("✅ โหลดตลาดสำเร็จ")

        # 3) ทดสอบสิทธิ์ API ด้วยการดึง balance
        b = exchange.fetch_balance({'type': 'swap'})
        logger.debug(f"✅ API ใช้งานได้ (fetch_balance ผ่าน) snapshot: {b.get('info')}")
    except ccxt.AuthenticationError as e:
        logger.error(f"❌ AuthenticationError: ตรวจ API Key/Secret/Passphrase อีกครั้ง | {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)
    except ccxt.ExchangeError as e:
        logger.error(f"❌ ExchangeError (load_markets/fetch_balance): {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ ผิดพลาดระหว่าง validate/load_markets: {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)

def get_available_margin_usdt():
    """
    ดึง USDT ที่ 'ใช้ได้' ในบัญชี Futures (Cross) จากฟิลด์ availBal ของสกุล USDT
    รองรับทั้งกรณีมี/ไม่มี details ใน payload
    """
    try:
        balance = exchange.fetch_balance({'type': 'swap'})
        info = balance.get('info', {})
        logger.debug(f"📦 Raw Balance Info: {info}")

        data_list = info.get('data') or []
        if not data_list:
            logger.warning("⚠️ ไม่พบ data ใน balance['info']")
            return 0.0

        # โครงสร้างยอดนิยมของ OKX v5: data[0]['details'] เป็นลิสต์ per-ccy
        first = data_list[0]
        details = first.get('details')

        # 1) กรณีมี details
        if isinstance(details, list) and details:
            for item in details:
                ccy = item.get('ccy')
                if ccy == 'USDT':
                    raw = item.get('availBal') or item.get('cashBal') or item.get('eq') or "0"
                    val = float(raw) if str(raw).strip() else 0.0
                    logger.debug(f"💰 Available Margin (USDT via details.availBal): {val}")
                    return val

        # 2) กรณีไม่มี details ให้ลองอ่าน key บนชั้น data[0] (บางบัญชีรวมเหมา)
        for key in ['availBal', 'cashBal', 'crossEq', 'availEq', 'eq']:
            if key in first and str(first.get(key)).strip():
                try:
                    val = float(first.get(key))
                    logger.debug(f"💰 Available Margin (USDT via {key}): {val}")
                    return val
                except:
                    pass

        logger.warning("⚠️ ไม่พบ USDT availBal/cashBal/crossEq/availEq/eq ที่ใช้ได้ใน payload")
        return 0.0

    except Exception as e:
        logger.error(f"❌ ดึง available margin ไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0.0

def get_contract_size(symbol: str) -> float:
    try:
        m = exchange.market(symbol)
        size = float(m.get('contractSize') or 0.0)
        if size <= 0:
            # สำหรับ BTC-USDT-SWAP โดยทั่วไป = 0.0001 BTC/contract
            size = 0.0001
            logger.warning(f"⚠️ ไม่พบ contractSize จากตลาด ใช้ค่า fallback = {size}")
        else:
            logger.debug(f"📐 contractSize = {size} BTC/contract")
        return size
    except Exception as e:
        logger.error(f"❌ ดึง contractSize ไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        # fallback ปลอดภัย
        return 0.0001

def get_current_price(symbol: str) -> float:
    try:
        t = exchange.fetch_ticker(symbol)
        price = float(t['last'])
        logger.debug(f"📈 Current Price {symbol}: {price}")
        return price
    except Exception as e:
        logger.error(f"❌ ดึงราคาปัจจุบันไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0.0

def set_leverage(leverage: int):
    try:
        # ccxt okx mapping: set_leverage(leverage, symbol, params={'mgnMode': 'cross'})
        res = exchange.set_leverage(leverage, SYMBOL, params={'mgnMode': 'cross'})
        logger.info(f"🔧 ตั้ง Leverage {leverage}x สำเร็จ: {res}")
    except ccxt.AuthenticationError as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้ (Auth): {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)
    except ccxt.InvalidOrder as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้ (InvalidOrder): {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)
    except ccxt.ExchangeError as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้ (ExchangeError): {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้ (อื่น ๆ): {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)

def calculate_order_contracts(available_usdt: float, price: float, contract_size_btc: float) -> int:
    """
    คำนวณจำนวนสัญญาแบบ 'ใช้ margin * percentage * leverage'
    เพื่อให้สอดคล้องกับ Futures (notional = margin * leverage)
    """
    try:
        if price <= 0 or contract_size_btc <= 0:
            logger.warning(f"⚠️ price/contract_size ไม่ถูกต้อง price={price}, contract_size={contract_size_btc}")
            return 0

        # notional ที่ต้องการใช้
        target_notional_usdt = available_usdt * PORTFOLIO_PERCENTAGE * LEVERAGE
        target_btc = target_notional_usdt / price
        contracts = math.floor(target_btc / contract_size_btc)

        logger.debug(
            f"📊 Calc contracts | avail_usdt={available_usdt:.4f}, pct={PORTFOLIO_PERCENTAGE}, lev={LEVERAGE}, "
            f"target_notional={target_notional_usdt:.4f} USDT, target_btc={target_btc:.8f}, "
            f"contract_size={contract_size_btc}, contracts={contracts}"
        )
        return int(contracts)
    except Exception as e:
        logger.error(f"❌ คำนวณสัญญาไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0

def open_long(contracts: int):
    """
    เปิด Long แบบ Market
    หมายเหตุ:
    - ถ้าไม่ได้เปิด Hedge Mode แต่ส่ง posSide อาจ error; เราจะลองส่งแบบมี posSide ก่อน
      ถ้า error ที่ชี้ชัดว่าโหมดไม่ตรง จะลองส่งแบบไม่ใส่ posSide ให้เอง
    """
    if contracts < MIN_CONTRACTS:
        logger.warning(f"⚠️ Contracts ต่ำกว่า {MIN_CONTRACTS}: {contracts} ไม่เปิดออเดอร์")
        return

    params = {
        'tdMode': 'cross',
        'posSide': 'long',  # ถ้าไม่ได้ใช้ Hedge Mode อาจ error
    }

    logger.debug(f"🚀 ส่งคำสั่ง: symbol={SYMBOL}, type=market, side=buy, amount={contracts}, params={params}")
    try:
        order = exchange.create_order(SYMBOL, 'market', 'buy', contracts, None, params)
        logger.info(f"✅ เปิด Long สำเร็จ: {order}")
    except ccxt.InvalidOrder as e:
        msg = str(e)
        logger.error(f"❌ InvalidOrder ตอนเปิด Long: {msg}")
        logger.debug(traceback.format_exc())
        # เผื่อกรณี posSide ไม่รองรับ (ไม่ได้เปิด Hedge Mode)
        if 'posSide' in msg or 'Position mode' in msg or 'Hedge' in msg:
            try:
                logger.warning("↻ ลองใหม่โดยไม่ใส่ posSide (เผื่อบัญชีไม่ได้เปิด Hedge Mode)")
                params_fallback = {'tdMode': 'cross'}
                order = exchange.create_order(SYMBOL, 'market', 'buy', contracts, None, params_fallback)
                logger.info(f"✅ เปิด Long สำเร็จ (fallback no posSide): {order}")
            except Exception as e2:
                logger.error(f"❌ เปิด Long ไม่ได้ (fallback): {e2}")
                logger.debug(traceback.format_exc())
        # ถ้าเป็นอย่างอื่นก็ไม่ retry
    except ccxt.AuthenticationError as e:
        logger.error(f"❌ เปิด Long ไม่ได้ (Auth): {e}")
        logger.debug(traceback.format_exc())
    except ccxt.ExchangeError as e:
        logger.error(f"❌ เปิด Long ไม่ได้ (ExchangeError): {e}")
        logger.debug(traceback.format_exc())
    except Exception as e:
        logger.error(f"❌ เปิด Long ไม่ได้ (อื่น ๆ): {e}")
        logger.debug(traceback.format_exc())

# ---------------- MAIN ----------------
if __name__ == "__main__":
    logger.info("🚀 เริ่มบอท: ตั้ง Leverage และเปิด Long ทันที")
    validate_api_keys_and_load_markets()

    # ตั้ง Leverage ก่อน
    set_leverage(LEVERAGE)

    # ดึงราคากับ margin
    price = get_current_price(SYMBOL)
    if price <= 0:
        logger.error("❌ ราคาไม่ถูกต้อง หยุดการทำงาน")
        sys.exit(1)

    available_usdt = get_available_margin_usdt()
    if available_usdt <= 0:
        logger.error("❌ Available Margin = 0 USDT (เช็กว่าเงินอยู่บัญชี Futures Cross แล้วหรือยัง)")
        sys.exit(1)

    # ดึง contract size
    contract_size = get_contract_size(SYMBOL)
    if contract_size <= 0:
        logger.error("❌ contractSize ผิดพลาด")
        sys.exit(1)

    # คำนวณจำนวนสัญญา
    contracts = calculate_order_contracts(available_usdt, price, contract_size)
    if contracts < MIN_CONTRACTS:
        logger.warning(
            f"⚠️ Contracts ต่ำกว่า {MIN_CONTRACTS}: {contracts} "
            f"(ลองเพิ่ม PORTFOLIO_PERCENTAGE หรือ LEVERAGE / เติม margin เพิ่ม)"
        )
        sys.exit(0)

    # เปิด Long
    open_long(contracts)
