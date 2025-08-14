import os
import math
import time
import ccxt
import logging

# ---------------- CONFIG (ฝังค่าในโค้ด) ----------------
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET  = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')

SYMBOL = 'BTC-USDT-SWAP'   # คุณใช้อันนี้อยู่และ fetch ราคาได้จริงตาม log

# การจัดการทุน/ความเสี่ยง
PORTFOLIO_PERCENTAGE = 0.80   # ใช้ทุนกี่ % ของ available
LEVERAGE = 15                 # เลเวอเรจ
SAFETY_PCT = 0.70             # กันชนเพิ่มจากสัดส่วนทุน (conservative)
FIXED_BUFFER_USDT = 7.0       # กันเงินสดคงที่ (กันค่าธรรมเนียม/เศษต่าง ๆ)
FEE_RATE_TAKER = 0.001        # ประมาณการ taker fee (0.10%) ให้เผื่อเยอะนิดเพื่อลด 51008
HEADROOM = 0.90               # ยิงเริ่มต้นแค่ 90% ของ theoretical contracts
RETRY_STEP = 0.80             # ลดสัญญาครั้งละ 20% ถ้าเจอ 51008
MAX_RETRIES = 8               # ลดได้มากสุด 8 ครั้ง

# ---------------- LOGGER ----------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("okx-oneway-bot")

# ---------------- INIT EXCHANGE ----------------
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})
exchange.verbose = False  # ปิด header logging กันคีย์หลุด

# ---------------- FUNCTIONS ----------------
def get_margin_channels():
    """
    คืน (avail, ord_frozen, imr, mmr) เป็น float
    - availBal: เงินใช้ได้
    - ordFrozen: เงินที่ถูกแช่โดยคำสั่งค้าง
    - imr/mmr: initial/maintenance margin รวม
    """
    try:
        bal = exchange.fetch_balance({'type': 'swap'})
        data = (bal.get('info', {}).get('data') or [])
        if not data:
            return 0.0, 0.0, 0.0, 0.0
        first = data[0]
        details = first.get('details')

        if isinstance(details, list):
            for item in details:
                if item.get('ccy') == 'USDT':
                    avail = float(item.get('availBal') or 0)
                    ord_frozen = float(item.get('ordFrozen') or 0)
                    imr = float(item.get('imr') or 0)
                    mmr = float(item.get('mmr') or 0)
                    return avail, ord_frozen, imr, mmr

        # fallback (บางบัญชีรวมเป็นชั้นบน)
        avail = float(first.get('availBal') or first.get('cashBal') or first.get('eq') or 0)
        ord_frozen = float(first.get('ordFrozen') or 0)
        imr = float(first.get('imr') or 0)
        mmr = float(first.get('mmr') or 0)
        return avail, ord_frozen, imr, mmr
    except Exception as e:
        logger.error(f"❌ get_margin_channels error: {e}")
        return 0.0, 0.0, 0.0, 0.0

def get_current_price():
    try:
        t = exchange.fetch_ticker(SYMBOL)
        p = float(t['last'])
        logger.debug(f"📈 Current Price {SYMBOL}: {p}")
        return p
    except Exception as e:
        logger.error(f"❌ ดึงราคาไม่ได้: {e}")
        return 0.0

def get_contract_size(symbol):
    """
    ดึง contractSize จากตลาด; ถ้าผิดปกติให้ fallback = 0.0001 (BTC perp)
    ป้องกันบั๊กตัวแปร cs ที่ยังไม่กำหนด
    """
    try:
        markets = exchange.load_markets()
        m = markets.get(symbol) or {}
        cs = float(m.get('contractSize') or 0.0)
        # BTC perp ปกติ ~ 0.0001 BTC/contract
        if cs <= 0 or cs > 0.001:
            logger.warning(f"⚠️ contractSize ที่ได้ {cs} ผิดปกติ ใช้ค่า fallback = 0.0001")
            return 0.0001
        return cs
    except Exception as e:
        logger.error(f"❌ ดึง contractSize ไม่ได้: {e}")
        return 0.0001

def set_leverage(leverage: int):
    try:
        res = exchange.set_leverage(leverage, SYMBOL, params={'mgnMode': 'cross'})
        logger.info(f"🔧 ตั้ง Leverage {leverage}x สำเร็จ: {res}")
    except Exception as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้: {e}")

def get_position_mode():
    try:
        resp = exchange.private_get_account_config()
        pos_mode = resp['data'][0]['posMode']  # 'net_mode' หรือ 'long_short_mode'
        logger.info(f"📌 Position Mode: {pos_mode}")
        return pos_mode
    except Exception as e:
        logger.error(f"❌ ดึง Position Mode ไม่ได้: {e}")
        return 'net_mode'

def cancel_all_open_orders(symbol: str):
    """ยกเลิกคำสั่งค้างทั้งหมด เพื่อปล่อย ordFrozen ก่อนคำนวณ"""
    try:
        opens = exchange.fetch_open_orders(symbol)
        if not opens:
            return
        ids = [o['id'] for o in opens if o.get('id')]
        for oid in ids:
            try:
                exchange.cancel_order(oid, symbol)
                logger.info(f"🧹 ยกเลิกคำสั่งค้าง: {oid}")
            except Exception as e:
                logger.warning(f"⚠️ ยกเลิกคำสั่ง {oid} ไม่ได้: {e}")
    except Exception as e:
        logger.warning(f"⚠️ ตรวจ open orders ไม่ได้: {e}")

def calc_contracts_by_margin(avail_usdt: float, price: float, contract_size: float) -> int:
    """
    สร้างขนาดสัญญาจากมาร์จิ้นจริง:
    - effective_avail = avail - FIXED_BUFFER_USDT
    - usable_cash = effective_avail * PORTFOLIO_PERCENTAGE * SAFETY_PCT
    - need_per_ct = (price * contract_size)/LEVERAGE + fee
    - max_ct = floor((usable_cash / need_per_ct) * HEADROOM)
    """
    if price <= 0 or contract_size <= 0:
        return 0

    effective_avail = max(0.0, avail_usdt - FIXED_BUFFER_USDT)
    usable_cash = effective_avail * PORTFOLIO_PERCENTAGE * SAFETY_PCT

    notional_per_ct = price * contract_size
    im_per_ct = notional_per_ct / LEVERAGE
    fee_per_ct = notional_per_ct * FEE_RATE_TAKER
    need_per_ct = im_per_ct + fee_per_ct
    if need_per_ct <= 0:
        return 0

    theoretical = usable_cash / need_per_ct
    logger.debug(
        f"🧮 Sizing by margin | avail={avail_usdt:.4f}, eff_avail={effective_avail:.4f}, usable={usable_cash:.4f}, "
        f"notional_ct={notional_per_ct:.4f}, im_ct={im_per_ct:.4f}, fee_ct={fee_per_ct:.6f}, need_ct={need_per_ct:.4f}, "
        f"theoretical={theoretical:.4f}"
    )

    max_ct = int(math.floor(theoretical * HEADROOM))
    if max_ct < 0:
        max_ct = 0
    logger.debug(f"✅ max_ct(final)={max_ct}")
    return max_ct

def open_long(contracts: int, pos_mode: str):
    """
    เปิด Long แบบ One-way (ไม่ส่ง posSide) + auto-retry สำหรับ 51008
    ถ้าเจอ posSide error (กรณีหลงส่งมาจาก hedge code) จะตัด posSide ทิ้งแล้วลองใหม่
    """
    if contracts <= 0:
        logger.warning("⚠️ Contracts <= 0 ไม่เปิดออเดอร์")
        return

    params = {'tdMode': 'cross'}  # One-way: ไม่ส่ง posSide
    current = int(contracts)
    attempt = 0

    while attempt < MAX_RETRIES and current >= 1:
        try:
            logger.debug(f"🚀 ส่งคำสั่ง: {SYMBOL} market buy {current} (attempt {attempt+1})")
            order = exchange.create_order(SYMBOL, 'market', 'buy', current, None, params)
            logger.info(f"✅ เปิด Long สำเร็จ: {order}")
            return
        except ccxt.ExchangeError as e:
            msg = str(e)
            logger.error(f"❌ เปิด Long ไม่ได้: {msg}")

            # ถ้ามี posSide error โผล่ (กันพลาด)
            if 'posSide' in msg or 'Position mode' in msg or '51000' in msg:
                params.pop('posSide', None)
                logger.warning("↻ ตัด posSide ออก (One-way) แล้วลองใหม่")
                attempt += 1
                continue

            # เงินไม่พอ → ลดสัญญาแรงขึ้นตาม RETRY_STEP
            if '51008' in msg or 'Insufficient' in msg:
                attempt += 1
                next_ct = int(math.floor(current * RETRY_STEP))
                if next_ct >= current:
                    next_ct = current - 1
                if next_ct < 1:
                    logger.warning("⚠️ ลดจนต่ำกว่า 1 แล้ว ยกเลิก")
                    return
                logger.warning(f"↻ ลดสัญญาแล้วลองใหม่: {current} → {next_ct}")
                current = next_ct
                time.sleep(0.5)
                continue

            # error อื่น หยุด
            return
        except Exception as e:
            logger.error(f"❌ เปิด Long ล้มเหลว (อื่น ๆ): {e}")
            return

# ---------------- MAIN ----------------
if __name__ == "__main__":
    # ตั้งเลเวอเรจก่อน
    set_leverage(LEVERAGE)

    # ตรวจ mode (ใช้ one-way ก็ได้ค่า 'net_mode')
    pos_mode = get_position_mode()

    # 1) ยกเลิกคำสั่งค้างก่อน (ปล่อย ordFrozen)
    cancel_all_open_orders(SYMBOL)

    # 2) ดึงช่อง margin
    avail, ord_frozen, imr, mmr = get_margin_channels()
    logger.info(f"🔍 Margin channels | avail={avail:.4f} | ordFrozen={ord_frozen:.4f} | imr={imr:.4f} | mmr={mmr:.4f}")

    # 3) ใช้ avail_net เพื่อ sizing (ปกติ ordFrozen จะ 0 ถ้ายกเลิกหมดแล้ว)
    avail_net = max(0.0, avail - ord_frozen)
    logger.info(f"🧮 ใช้ avail_net สำหรับ sizing = {avail_net:.4f} USDT")

    # 4) ราคา + contract size
    price = get_current_price()
    csize = get_contract_size(SYMBOL)
    logger.info(f"🫙 สรุปสถานะ | avail_net={avail_net:.4f} USDT | price={price} | contractSize={csize}")

    # 5) คำนวณสัญญาแบบ conservative + headroom แล้วเปิด (auto-retry)
    contracts = calc_contracts_by_margin(avail_net, price, csize)
    open_long(contracts, pos_mode)
