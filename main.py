import os
import sys
import time
import math
import ccxt
import logging
import traceback

# ---------- CONFIG (env) ----------
API_KEY   = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET    = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD  = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')
SYMBOL    = os.getenv('SYMBOL', 'BTC-USDT-SWAP')

LEVERAGE               = int(os.getenv('LEVERAGE', '15'))
PORTFOLIO_PERCENTAGE   = float(os.getenv('PORTFOLIO_PERCENTAGE', '0.80'))  # ใช้กี่ % ของ margin
OPEN_ON_START          = os.getenv('OPEN_ON_START', 'true').lower() == 'true'  # เปิด Long ตอนสตาร์ท 1 ครั้ง
REOPEN_EVERY_MINUTES   = int(os.getenv('REOPEN_EVERY_MINUTES', '0'))  # 0 = ไม่เปิดซ้ำอัตโนมัติ
MIN_CONTRACTS          = int(os.getenv('MIN_CONTRACTS', '1'))         # ขั้นต่ำ 1 สัญญา
LOOP_SLEEP_SECONDS     = int(os.getenv('LOOP_SLEEP_SECONDS', '30'))    # จังหวะ heartbeat

# ---------- LOGGER ----------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("okx-railway-bot")

# ---------- EXCHANGE ----------
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

# ---------- HELPERS ----------
def validate_api_and_load_markets():
    try:
        if (not API_KEY or not SECRET or not PASSWORD or
            'YOUR_OKX_API_KEY_HERE' in API_KEY or
            'YOUR_OKX_SECRET_HERE' in SECRET or
            'YOUR_OKX_PASSWORD_HERE' in PASSWORD):
            logger.error("❌ ยังไม่ได้ตั้งค่า ENV: OKX_API_KEY / OKX_SECRET / OKX_PASSWORD")
            sys.exit(1)

        markets = exchange.load_markets()
        if SYMBOL not in markets:
            logger.error(f"❌ ไม่พบสัญลักษณ์ {SYMBOL} ในตลาด OKX")
            sys.exit(1)
        logger.debug("✅ load_markets ผ่าน")

        # ทดสอบ fetch_balance เพื่อยืนยันสิทธิ์ API
        b = exchange.fetch_balance({'type': 'swap'})
        logger.debug(f"✅ API พร้อมใช้งาน (fetch_balance ผ่าน): {b.get('info')}")
    except ccxt.AuthenticationError as e:
        logger.error(f"❌ AuthenticationError: API Key/Secret/Passphrase ไม่ถูกต้องหรือสิทธิ์ไม่พอ | {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ ผิดพลาดตอน validate/load_markets: {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)

def get_available_margin_usdt() -> float:
    """ดึง USDT ที่ใช้ได้ใน Futures Cross จาก availBal (รองรับ payload หลายแบบ)"""
    try:
        bal = exchange.fetch_balance({'type': 'swap'})
        info = bal.get('info', {})
        logger.debug(f"📦 Raw Balance Info: {info}")
        data = (info.get('data') or [])
        if not data:
            return 0.0

        first = data[0]
        details = first.get('details')

        # 1) ปกติอยู่ใน details (list per-ccy)
        if isinstance(details, list):
            for item in details:
                if item.get('ccy') == 'USDT':
                    raw = item.get('availBal') or item.get('cashBal') or item.get('eq') or "0"
                    val = float(raw) if str(raw).strip() else 0.0
                    logger.debug(f"💰 Available Margin via details.availBal: {val}")
                    return val

        # 2) บางบัญชีไม่มี details → อ่านจากชั้นบน
        for key in ['availBal', 'cashBal', 'crossEq', 'availEq', 'eq']:
            raw = first.get(key)
            if raw is not None and str(raw).strip():
                try:
                    val = float(raw)
                    logger.debug(f"💰 Available Margin via {key}: {val}")
                    return val
                except:
                    pass

        logger.warning("⚠️ ไม่พบ USDT availBal/cashBal/crossEq/availEq/eq")
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
            size = 0.0001  # BTC-USDT-SWAP โดยทั่วไป
            logger.warning(f"⚠️ contractSize ไม่เจอ ใช้ fallback {size}")
        else:
            logger.debug(f"📐 contractSize = {size} BTC/contract")
        return size
    except Exception as e:
        logger.error(f"❌ ดึง contractSize ไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0.0001

def get_price(symbol: str) -> float:
    try:
        t = exchange.fetch_ticker(symbol)
        p = float(t['last'])
        logger.debug(f"📈 Price {symbol}: {p}")
        return p
    except Exception as e:
        logger.error(f"❌ ดึงราคาไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0.0

def set_cross_leverage(leverage: int):
    try:
        res = exchange.set_leverage(leverage, SYMBOL, params={'mgnMode': 'cross'})
        logger.info(f"🔧 ตั้ง Leverage {leverage}x สำเร็จ: {res}")
    except ccxt.ExchangeError as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        # ไม่ exit เพื่อให้ service ไม่ crash; จะลองใหม่รอบถัดไป
        raise

def calc_contracts(available_usdt: float, price: float, contract_size_btc: float) -> int:
    try:
        if price <= 0 or contract_size_btc <= 0:
            return 0
        target_notional = available_usdt * PORTFOLIO_PERCENTAGE * LEVERAGE  # ใช้ margin พร้อม leverage
        target_btc = target_notional / price
        contracts = math.floor(target_btc / contract_size_btc)
        logger.debug(
            f"📊 Calc | avail={available_usdt:.4f} USDT, pct={PORTFOLIO_PERCENTAGE}, lev={LEVERAGE}, "
            f"notional={target_notional:.4f}, target_btc={target_btc:.8f}, size={contract_size_btc}, "
            f"contracts={contracts}"
        )
        return max(0, int(contracts))
    except Exception as e:
        logger.error(f"❌ คำนวณสัญญาไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return 0

def open_long(contracts: int):
    if contracts < MIN_CONTRACTS:
        logger.warning(f"⚠️ Contracts < {MIN_CONTRACTS}: {contracts} ไม่เปิดออเดอร์")
        return False

    params = {'tdMode': 'cross', 'posSide': 'long'}  # ถ้าไม่ใช่ Hedge จะ fallback
    try:
        logger.debug(f"🚀 ส่งคำสั่ง (with posSide): {SYMBOL}, market, buy, {contracts}, {params}")
        order = exchange.create_order(SYMBOL, 'market', 'buy', contracts, None, params)
        logger.info(f"✅ เปิด Long สำเร็จ: {order}")
        return True
    except ccxt.InvalidOrder as e:
        msg = str(e)
        logger.error(f"❌ InvalidOrder: {msg}")
        logger.debug(traceback.format_exc())
        if 'posSide' in msg or 'hedge' in msg.lower() or 'Position mode' in msg:
            try:
                params2 = {'tdMode': 'cross'}
                logger.warning("↻ ลองใหม่แบบไม่ใส่ posSide (บัญชีอาจไม่ได้เปิด Hedge Mode)")
                order = exchange.create_order(SYMBOL, 'market', 'buy', contracts, None, params2)
                logger.info(f"✅ เปิด Long สำเร็จ (fallback): {order}")
                return True
            except Exception as e2:
                logger.error(f"❌ เปิด Long (fallback) ไม่ได้: {e2}")
                logger.debug(traceback.format_exc())
                return False
        return False
    except Exception as e:
        logger.error(f"❌ เปิด Long ไม่ได้: {e}")
        logger.debug(traceback.format_exc())
        return False

# ---------- MAIN LOOP ----------
def main():
    logger.info("🚀 เริ่มบอทบน Railway (keep-alive loop)")
    validate_api_and_load_markets()

    opened_once = False
    last_open_ts = 0.0
    backoff = 5  # วินาที (จะเพิ่มเมื่อเจอ error เพื่อกันลูปเด้งรัว)

    while True:
        try:
            price = get_price(SYMBOL)
            avail = get_available_margin_usdt()
            size  = get_contract_size(SYMBOL)

            logger.info(f"🫙 สรุปสถานะ | avail={avail:.4f} USDT | price={price} | contractSize={size}")

            # ตั้ง leverage ทุก ๆ รอบแรก หรือทุก 30 นาที (กันโดน reset)
            try:
                set_cross_leverage(LEVERAGE)
            except Exception:
                logger.warning("⚠️ ตั้ง Leverage รอบนี้ไม่สำเร็จ (จะลองใหม่อัตโนมัติรอบถัดไป)")

            # ตัดสินใจเปิด Long
            should_open = False
            if OPEN_ON_START and not opened_once:
                should_open = True
            elif REOPEN_EVERY_MINUTES > 0 and (time.time() - last_open_ts) >= (REOPEN_EVERY_MINUTES * 60):
                should_open = True

            if should_open:
                contracts = calc_contracts(avail, price, size)
                if contracts >= MIN_CONTRACTS:
                    ok = open_long(contracts)
                    if ok:
                        opened_once = True
                        last_open_ts = time.time()
                else:
                    logger.warning(
                        f"⚠️ ได้ contracts={contracts} < {MIN_CONTRACTS} "
                        f"(ลองเพิ่ม LEVERAGE/PORTFOLIO_PERCENTAGE หรือเติม USDT เข้าบัญชี Futures)"
                    )

            # reset backoff เมื่อทุกอย่างปกติ
            backoff = 5

        except Exception as loop_err:
            logger.error(f"❌ Loop error: {loop_err}")
            logger.debug(traceback.format_exc())
            backoff = min(backoff * 2, 300)  # เพิ่มเป็น 10/20/... สูงสุด 300 วิ

        # keep-alive
        time.sleep(max(LOOP_SLEEP_SECONDS, backoff))

if __name__ == "__main__":
    main()
