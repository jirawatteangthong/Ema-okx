import os
import sys
import math
import time
import ccxt
import logging

# ================== CONFIG (ฝังค่าในโค้ด) ==================
API_KEY  = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET   = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSWORD_HERE_FOR_LOCAL_TESTING')

SYMBOL = 'BTC-USDT-SWAP'         # USDT-margined perp
CONTRACT_FALLBACK = 0.01         # BTC/contract (OKX BTC-USDT-SWAP)

# ไล่หา leverage ขั้นต่ำ
LEV_START = 10                   # เริ่มลองที่ 10x
LEV_STEP  = 5                    # เพิ่มทีละ 5x
LEV_MAX   = 75                  # เพดาน 125x (OKX ส่วนใหญ่รองรับ)

# ใช้ทุนแบบ conservative
PORTFOLIO_PERCENTAGE = 0.80      # ใช้สัดส่วนจาก avail_net
SAFETY_PCT = 0.85                # กันชนเพิ่ม
FIXED_BUFFER_USDT = 5.0          # กันเงินสดคงที่
FEE_RATE_TAKER = 0.001           # ประมาณการ taker fee (0.10%)

# logger
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("okx-lev-scan-bot")

# ================== EXCHANGE ==================
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})
exchange.verbose = False  # ห้าม log header ที่มีคีย์

# ================== HELPERS ==================
def get_margin_channels():
    """คืน (avail, ord_frozen, imr, mmr) จากบัญชี swap เป็น float"""
    try:
        bal = exchange.fetch_balance({'type':'swap'})
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
        # fallback
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
        logger.debug(f"📈 Price {SYMBOL}: {p}")
        return p
    except Exception as e:
        logger.error(f"❌ ดึงราคาไม่ได้: {e}")
        return 0.0

def get_contract_size(symbol):
    """OKX BTC-USDT-SWAP ปกติ 0.01 BTC/contract"""
    try:
        markets = exchange.load_markets()
        m = markets.get(symbol) or {}
        cs = float(m.get('contractSize') or 0.0)
        if cs <= 0 or cs >= 1:
            logger.warning(f"⚠️ contractSize ที่ได้ {cs} ผิดปกติ ใช้ fallback = {CONTRACT_FALLBACK}")
            return CONTRACT_FALLBACK
        return cs
    except Exception as e:
        logger.error(f"❌ ดึง contractSize ไม่ได้: {e}")
        return CONTRACT_FALLBACK

def cancel_all_open_orders(symbol):
    """ปลด ordFrozen โดยยกเลิกคำสั่งค้างทั้งหมด"""
    try:
        opens = exchange.fetch_open_orders(symbol)
        if not opens:
            return
        for o in opens:
            try:
                exchange.cancel_order(o['id'], symbol)
                logger.info(f"🧹 ยกเลิกคำสั่งค้าง: {o['id']}")
            except Exception as e:
                logger.warning(f"⚠️ ยกเลิกคำสั่ง {o.get('id')} ไม่ได้: {e}")
    except Exception as e:
        logger.warning(f"⚠️ ตรวจ open orders ไม่ได้: {e}")

def set_leverage_isolated(leverage: int):
    """ตั้ง isolated leverage สำหรับ SYMBOL"""
    try:
        res = exchange.set_leverage(leverage, SYMBOL, params={'mgnMode':'isolated'})
        logger.info(f"🔧 ตั้ง Leverage {leverage}x (isolated): {res}")
        return True
    except Exception as e:
        logger.error(f"❌ ตั้ง Leverage ไม่ได้: {e}")
        return False

def preflight_can_afford_one_contract(avail_net: float, price: float, csize: float, leverage: int) -> (bool, float):
    """คำนวณว่าพอเปิดอย่างน้อย 1 สัญญาไหม (IM + fee) และคืน need_per_ct"""
    notional_ct = price * csize
    im_ct = notional_ct / leverage if leverage > 0 else float('inf')
    fee_ct = notional_ct * FEE_RATE_TAKER
    need_ct = im_ct + fee_ct
    usable_cash = max(0.0, avail_net - FIXED_BUFFER_USDT) * PORTFOLIO_PERCENTAGE * SAFETY_PCT
    ok = (usable_cash >= need_ct)
    logger.debug(
        f"🧮 Preflight | lev={leverage}x | usable_cash={usable_cash:.4f} | "
        f"need_ct={need_ct:.4f} (im={im_ct:.4f}, fee={fee_ct:.4f})"
    )
    return ok, need_ct

def try_open_one_contract_isolated() -> bool:
    """พยายามเปิด 1 สัญญาแบบ isolated; คืน True ถ้าสำเร็จ"""
    params = {'tdMode':'isolated'}  # One-way → ไม่ส่ง posSide
    try:
        logger.debug(f"🚀 ส่งคำสั่ง: {SYMBOL} market buy 1 (isolated)")
        order = exchange.create_order(SYMBOL, 'market', 'buy', 1, None, params)
        logger.info(f"✅ เปิด Long 1 สัญญา สำเร็จ: {order}")
        return True
    except ccxt.ExchangeError as e:
        logger.error(f"❌ เปิด 1 สัญญา ไม่ได้: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ เปิด 1 สัญญา ล้มเหลว (อื่น ๆ): {e}")
        return False

# ================== MAIN (Leverage Scan) ==================
if __name__ == "__main__":
    # 1) เคลียร์คำสั่งค้าง
    cancel_all_open_orders(SYMBOL)

    # 2) ดึงสถานะเงิน
    avail, ord_frozen, imr, mmr = get_margin_channels()
    avail_net = max(0.0, avail - ord_frozen)
    logger.info(f"🔍 Margin | avail={avail:.4f} | ordFrozen={ord_frozen:.4f} | avail_net={avail_net:.4f} USDT")

    # 3) ดึงราคาและ contract size
    price = get_current_price()
    csize = get_contract_size(SYMBOL)
    logger.info(f"🫙 สรุป | price={price} | contractSize={csize}")

    if price <= 0 or csize <= 0:
        logger.error("❌ ข้อมูลราคา/contract size ไม่พร้อม")
        sys.exit(1)

    # 4) ลูปไล่เพิ่ม leverage จนกว่าจะเปิด 1 สัญญาได้
    found = False
    lev = LEV_START
    while lev <= LEV_MAX:
        # ตั้ง isolated leverage ก่อน
        ok_set = set_leverage_isolated(lev)
        if not ok_set:
            lev += LEV_STEP
            continue

        # เช็กพอเปิด 1 สัญญาไหม (คร่าว ๆ) ก่อนยิงจริง
        can, need_ct = preflight_can_afford_one_contract(avail_net, price, csize, lev)
        if not can:
            logger.info(f"ℹ️ ยังไม่พอสำหรับ 1 สัญญาที่ {lev}x (need≈{need_ct:.4f} USDT) → เพิ่มเลเวอเรจ")
            lev += LEV_STEP
            continue

        # ยิงจริง 1 สัญญา
        if try_open_one_contract_isolated():
            logger.info(f"🎯 สำเร็จ! Leverage ขั้นต่ำที่เปิดได้จริง: {lev}x (isolated)")
            found = True
            break
        else:
            # ถ้ายัง 51008 จากฝั่ง Exchange → ขยับเลเวอเรจต่อ
            logger.info(f"ℹ️ ยิงจริงไม่ผ่านที่ {lev}x → ลองเพิ่มเลเวอเรจ")
            lev += LEV_STEP
            # หน่วงนิด ลดโอกาสชน rate-limit
            time.sleep(0.5)

    if not found:
        logger.warning(
            f"🚫 ลองถึง {LEV_MAX}x แล้ว ยังเปิดไม่ได้\n"
            f"- เติม USDT เพิ่ม หรือ\n"
            f"- ลด FIXED_BUFFER_USDT/เพิ่ม PORTFOLIO_PERCENTAGE/SAFETY_PCT อย่างระวัง หรือ\n"
            f"- เปลี่ยนไปใช้สัญญาที่มี contract size เล็กกว่า"
        )
