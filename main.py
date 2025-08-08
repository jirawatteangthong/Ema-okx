import ccxt
import time
import requests
import logging
import os
import sys
import math

# API Keys

API_KEY = os.getenv('OKX_API_KEY', 'YOUR_API_KEY_HERE')
SECRET = os.getenv('OKX_SECRET', 'YOUR_SECRET_HERE')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_PASSWORD_HERE')

# Telegram

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID')

# Trading Settings

SYMBOL = ‘BTC-USDT-SWAP’
LEVERAGE = 15
TP_POINTS = 250
SL_POINTS = 400
PORTFOLIO_PCT = 0.5  # Use 50% of portfolio

# Setup logging

logging.basicConfig(level=logging.INFO, format=’%(asctime)s - %(levelname)s - %(message)s’)
logger = logging.getLogger(**name**)

def send_telegram(msg):
if TELEGRAM_TOKEN == ‘YOUR_TELEGRAM_TOKEN’:
logger.info(f”TELEGRAM: {msg}”)
return

```
try:
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    requests.post(url, {
        'chat_id': TELEGRAM_CHAT_ID, 
        'text': msg,
        'parse_mode': 'HTML'
    }, timeout=10)
    logger.info("Telegram sent")
except:
    logger.error("Telegram failed")
```

def setup_exchange():
global exchange

```
if API_KEY == 'YOUR_API_KEY_HERE':
    raise ValueError("Please set your API keys!")

exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'sandbox': False
})

exchange.load_markets()

# Set leverage
try:
    exchange.set_leverage(LEVERAGE, SYMBOL, params={'mgnMode': 'cross'})
    logger.info(f"Leverage set to {LEVERAGE}x")
except Exception as e:
    logger.warning(f"Leverage setting failed: {e}")

return exchange
```

def get_balance():
try:
balance = exchange.fetch_balance(params={‘type’: ‘trade’})

```
    # Try standard format
    if 'USDT' in balance and 'free' in balance['USDT']:
        return balance['USDT']['free']
    
    # Try OKX format
    for acc in balance.get('info', {}).get('data', []):
        if acc.get('ccy') == 'USDT' and acc.get('type') == 'TRADE':
            return float(acc.get('availBal', 0))
    
    return 0
except Exception as e:
    logger.error(f"Balance error: {e}")
    return 0
```

def get_price():
try:
ticker = exchange.fetch_ticker(SYMBOL)
return ticker[‘last’]
except Exception as e:
logger.error(f”Price error: {e}”)
return 0

def get_position():
try:
positions = exchange.fetch_positions([SYMBOL])
for pos in positions:
pos_size = float(pos.get(‘info’, {}).get(‘pos’, 0))
if pos_size != 0:
return {
‘side’: ‘long’ if pos_size > 0 else ‘short’,
‘size’: abs(pos_size),
‘entry’: float(pos.get(‘info’, {}).get(‘avgPx’, 0)),
‘pnl’: float(pos.get(‘info’, {}).get(‘upl’, 0))
}
return None
except Exception as e:
logger.error(f”Position error: {e}”)
return None

def calculate_size(balance, price):
# OKX: 1 contract = 0.0001 BTC
contract_btc = 0.0001

```
# Margin calculation (6.8% for 15x leverage)
margin_factor = 0.06824

# Use 90% of balance for safety
usable = balance * 0.9

# Calculate max contracts we can afford
max_notional = usable / margin_factor
target_notional = balance * PORTFOLIO_PCT / margin_factor

notional = min(max_notional, target_notional)
btc_amount = notional / price
contracts = math.floor(btc_amount / contract_btc)

if contracts < 1:
    return 0

# Final check
actual_notional = contracts * contract_btc * price
required_margin = actual_notional * margin_factor

if required_margin > usable:
    contracts = math.floor(usable / margin_factor / contract_btc / price)

logger.info(f"Calculated: {contracts} contracts, margin: {required_margin:.2f}")
return contracts
```

def open_long():
try:
balance = get_balance()
price = get_price()

```
    logger.info(f"Balance: {balance:.2f}, Price: {price:.1f}")
    
    if balance < 10:
        raise Exception("Balance too low")
    
    contracts = calculate_size(balance, price)
    if contracts < 1:
        raise Exception("Position size too small")
    
    # Open position
    logger.info(f"Opening LONG {contracts} contracts at {price:.1f}")
    
    order = exchange.create_market_order(
        SYMBOL, 'buy', contracts,
        params={'tdMode': 'cross'}
    )
    
    if not order.get('id'):
        raise Exception("Order failed")
    
    logger.info(f"Order success: {order['id']}")
    
    # Wait and set TP/SL
    time.sleep(3)
    
    tp_price = price + TP_POINTS
    sl_price = price - SL_POINTS
    
    # Take Profit
    tp_order = exchange.create_order(
        SYMBOL, 'TAKE_PROFIT_MARKET', 'sell', contracts,
        price, params={
            'triggerPrice': tp_price,
            'tdMode': 'cross',
            'reduceOnly': True
        }
    )
    
    # Stop Loss  
    sl_order = exchange.create_order(
        SYMBOL, 'STOP_LOSS_MARKET', 'sell', contracts,
        price, params={
            'triggerPrice': sl_price,
            'tdMode': 'cross', 
            'reduceOnly': True
        }
    )
    
    send_telegram(f"""
```

🚀 <b>LONG OPENED</b>
📊 Size: {contracts} contracts
💰 Entry: {price:.1f}
🎯 TP: {tp_price:.1f} (+{TP_POINTS})
🛡️ SL: {sl_price:.1f} (-{SL_POINTS})
🆔 Order: {order[‘id’]}
“””)

```
    logger.info("Long position opened with TP/SL")
    return True
    
except Exception as e:
    error_msg = f"Long failed: {e}"
    logger.error(error_msg)
    send_telegram(f"❌ <b>FAILED</b>\n{error_msg}")
    return False
```

def main():
try:
logger.info(“🚀 Starting OKX Bot”)

```
    # Setup
    setup_exchange()
    
    # Check existing position
    pos = get_position()
    if pos:
        logger.info(f"Position exists: {pos['side']} {pos['size']} contracts")
        send_telegram(f"⚠️ <b>Position exists</b>\n{pos['side'].upper()}: {pos['size']} contracts\nPnL: {pos['pnl']:+.2f}")
        return
    
    # Open long
    success = open_long()
    
    if success:
        # Verify
        time.sleep(2)
        final_pos = get_position()
        if final_pos:
            send_telegram(f"✅ <b>SUCCESS</b>\nPosition: {final_pos['side'].upper()}\nSize: {final_pos['size']}\nPnL: {final_pos['pnl']:+.2f}")
            logger.info("✅ Test completed successfully")
        else:
            logger.warning("⚠️ Position not found after opening")
    else:
        logger.error("❌ Test failed")
        
except Exception as e:
    logger.critical(f"Critical error: {e}")
    send_telegram(f"💥 <b>CRITICAL ERROR</b>\n{e}")
```

if **name** == ‘**main**’:
main()
