import os
import sys
import time
import math
import ccxt
import logging
import traceback

# ========= ENV CONFIG =========
API_KEY   = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET    = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD  = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')

SYMBOL     = os.getenv('SYMBOL', 'BTC/USDT:USDT')      # unified symbol (ccxt)
SYMBOL_ID  = os.getenv('SYMBOL_ID', 'BTC-USDT-SWAP')   # instId ของ OKX
LEVERAGE   = int(os.getenv('LEVERAGE', '15'))
PORTFOLIO_PERCENTAGE = float(os.getenv('PORTFOLIO_PERCENTAGE', '0.80'))
MIN_CONTRACTS = int(os.getenv('MIN_CONTRACTS', '1'))
LOOP_SLEEP_SECONDS = int(os.getenv('LOOP_SLEEP_SECONDS', '30'))

# กันพลาด “เงินไม่พอ” + ลดสัญญาอัตโนมัติเมื่อโดน 51008
SAFETY_PCT  = float(os.getenv('SAFETY_PCT', '0.85'))   # ใช้ notional แค่ 85% ของที่คำนวณได้
RETRY_STEP  = float(os.getenv('RETRY_STEP', '0.90'))   # ลดสัญญาครั้งละ 10%
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '5'))       # ลองไม่เกิน 5 ครั้ง

# ========= LOGGER =========
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("okx-oneway-bot")

# ========= EXCHANGE =========
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})
exchange.verbose = False  # 🔒 ห้ามพิมพ์ header ที่มีคีย์

# ========= HELPERS =========
def resolve_market():
    """โหลดตลาด + แก้สัญลักษณ์ รองรับทั้ง unified และ id"""
    try:
        if (not API_KEY or not SECRET or not PASSWORD or
            'YOUR_OKX_API_KEY_HERE' in API_KEY or
            'YOUR_OKX_SECRET_HERE' in SECRET or
            'YOUR_OKX_PASSWORD_HERE' in PASSWORD):
            logger.error("❌ ยังไม่ได้ตั้งค่า ENV: OKX_API_KEY / OKX_SECRET / OKX_PASSWORD")
            sys.exit(1)

        markets = exchange.load_markets()
        if SYMBOL in markets:
            mkt = markets[SYMBOL]
            logger.info(f"✅ ใช้ตลาด: {mkt['symbol']} (id={mkt['id']})")
            return mkt
        # ลองหาโดย id
        mbid = getattr(exchange, 'markets_by_id', None) or {}
        if SYMBOL_ID in mbid:
            mkt = mbid[SYMBOL_ID]
            logger.info(f"✅ ใช้ตลาด (จาก id): {mkt['symbol']} (id={mkt['id']})")
            return mkt

        raise ccxt.BadSymbol(f"ไม่พบสัญลักษณ์ (unified='{SYMBOL}', id='{SYMBOL_ID}')")
    except ccxt.AuthenticationError as e:
        logger.error(f"❌ AuthenticationError (API Key/Passphrase): {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ ผิดพลาดตอน load_markets/resolve_market: {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)

def get_available_margin_usdt() -> float:
    """ดึง USDT ที่ใช้ได้ใน Futures Cross จาก availBal (ไม่ log header/ข้อมูลดิบยาว)"""
    try:
        bal = exchange.fetch_balance({'type': 'swap'})
        data = (bal.get('info', {}).get('data') or [])
        if not data:
            return 0.0
        first = data[0]
        details = first.get('details')
        if isinstance(details, list):
            for item in details:
                if item.get('ccy') == 'USDT':
                    raw = item.get('availBal') or item.get('cashBal') or item.get('eq') or "0"
                    val = float(raw) if str(raw).strip() else 0.0
                    return val
        # fallback บางบัญชี
        for key in ['availBal', 'cashBal', 'crossEq', 'availEq', 'eq']:
            raw = first.get(key)
            if raw is not None and str(raw).strip():
                return float(raw)
        return 0.0
    except Exception as e:
        logger.error(f"❌ ดึง available margin ไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0.0

def get_price(symbol) -> float:
    try:
        t = exchange.fetch_ticker(symbol)
        return float(t['last'])
    except Exception as e:
        logger.error(f"❌ ดึงราคาไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0.0

def get_contract_size(market) -> float:
    """กัน contractSize เพี้ยน: BTC perp ใช้ 0.0001 เป็น fallback"""
    try:
        size = float(market.get('contractSize') or 0.0)
        if size > 0.001 or size <= 0:
            logger.warning(f"⚠️ contractSize ที่ได้ {size} ผิดปกติ ใช้ค่า fallback = 0.0001")
            return 0.0001
        return size
    except Exception:
        logger.warning("⚠️ ดึง contractSize ไม่ได้ ใช้ fallback = 0.0001")
        return 0.0001

def set_cross_leverage(market, leverage: int):
    try:
        res = exchange.set_leverage(leverage, market['symbol'], params={'mgnMode': 'cross'})
        logger.info(f"🔧 ตั้ง Leverage {leverage}x สำเร็จ: {res}")
    except Exception as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้: {e}")
        logger.debug(traceback.format_exc())

def calc_contracts(avail_usdt: float, price: float, contract_size: float) -> int:
    """ใช้ notional = avail * pct * lev * safety แล้วแปลงเป็นจำนวนสัญญา"""
    if price <= 0 or contract_size <= 0:
        return 0
    target_notional = avail_usdt * PORTFOLIO_PERCENTAGE * LEVERAGE * SAFETY_PCT
    target_btc = target_notional / price
    contracts = math.floor(target_btc / contract_size)
    logger.debug(
        f"📊 Calc: avail={avail_usdt:.4f}, pct={PORTFOLIO_PERCENTAGE}, lev={LEVERAGE}, "
        f"safety={SAFETY_PCT}, notional={target_notional:.4f}, btc={target_btc:.8f}, "
        f"size={contract_size}, contracts={contracts}"
    )
    return contracts

def open_long_oneway(market, contracts: int) -> bool:
    """
    เปิด Long สำหรับ One-way mode (ไม่ส่ง posSide)
    มี auto-retry ลดสัญญาเมื่อเจอ 51008: Insufficient margin
    """
    if contracts < MIN_CONTRACTS:
        logger.warning(f"⚠️ Contracts < {MIN_CONTRACTS}: {contracts} ไม่เปิดออเดอร์")
        return False

    params = {'tdMode': 'cross'}
    attempt = 0
    current = int(contracts)

    while attempt <= MAX_RETRIES and current >= MIN_CONTRACTS:
        try:
            logger.debug(f"🚀 ส่งคำสั่ง: {market['symbol']} market buy {current} (attempt {attempt+1})")
            order = exchange.create_order(market['symbol'], 'market', 'buy', current, None, params)
            logger.info(f"✅ เปิด Long สำเร็จ: {order}")
            return True
        except ccxt.ExchangeError as e:
            msg = str(e)
            logger.error(f"❌ เปิด Long ไม่ได้: {msg}")
            # ถ้าเงินไม่พอ (51008 / Insufficient) → ลดสัญญาแล้วลองใหม่
            if ('51008' in msg) or ('Insufficient' in msg):
                attempt += 1
                next_contracts = int(math.floor(current * RETRY_STEP))
                if next_contracts >= current:
                    next_contracts = current - 1
                if next_contracts < MIN_CONTRACTS:
                    logger.warning(f"⚠️ ลดจนต่ำกว่า {MIN_CONTRACTS} แล้ว ยกเลิก")
                    return False
                logger.warning(f"↻ ลดสัญญาแล้วลองใหม่: {current} → {next_contracts}")
                current = next_contracts
                continue
            # ถ้าเป็น error อื่นหยุด
            return False
        except Exception as e:
            logger.error(f"❌ เปิด Long ล้มเหลว (อื่น ๆ): {e}")
            logger.debug(traceback.format_exc())
            return False

    logger.warning(f"⚠️ ลองครบ {MAX_RETRIES} ครั้งแล้วยังไม่ผ่าน")
    return False

def debug_margin_channels():
    """ออปชัน: ดูว่ามี ordFrozen/imr/mmr กิน margin ไปไหม (ช่วยวิเคราะห์ 51008)"""
    try:
        bal = exchange.fetch_balance({'type':'swap'})
        data = (bal.get('info', {}).get('data') or [])
        if data and 'details' in data[0]:
            for item in data[0]['details']:
                if item.get('ccy') == 'USDT':
                    useful = {k: item.get(k) for k in ['availBal','cashBal','eq','ordFrozen','imr','mmr','mgnRatio']}
                    logger.info(f"🔍 USDT channels: {useful}")
    except Exception:
        pass

# ========= MAIN LOOP =========
def main():
    logger.info("🚀 เริ่มบอท (One-way) + keep-alive loop")
    market = resolve_market()

    while True:
        try:
            price = get_price(market['symbol'])
            avail = get_available_margin_usdt()
            csize = get_contract_size(market)
            logger.info(f"🫙 สรุปสถานะ | avail={avail:.4f} USDT | price={price} | contractSize={csize}")

            # ตั้ง Leverage (กันโดน reset ด้วยการตั้งซ้ำเป็นครั้งคราวก็ได้)
            set_cross_leverage(market, LEVERAGE)

            # คำนวณสัญญา + เปิด
            contracts = calc_contracts(avail, price, csize)
            if contracts >= MIN_CONTRACTS:
                ok = open_long_oneway(market, contracts)
                if not ok:
                    debug_margin_channels()  # ช่วยดูช่อง freeze
            else:
                logger.warning(f"⚠️ ได้ contracts={contracts} < {MIN_CONTRACTS} (เพิ่ม LEVERAGE/เปอร์เซ็นต์ หรือเติม USDT)")

        except Exception as loop_err:
            logger.error(f"❌ Loop error: {loop_err}")
            logger.debug(traceback.format_exc())

        time.sleep(LOOP_SLEEP_SECONDS)

if __name__ == "__main__":
    main()
