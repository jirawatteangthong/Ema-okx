import ccxt
import time
import requests
import logging
import os
import math

API_KEY = “YOUR_API_KEY_HERE”
SECRET = “YOUR_SECRET_HERE”
PASSWORD = “YOUR_PASSWORD_HERE”
TELEGRAM_TOKEN = “YOUR_TELEGRAM_TOKEN”
TELEGRAM_CHAT_ID = “YOUR_CHAT_ID”

SYMBOL = “BTC-USDT-SWAP”
LEVERAGE = 15
TP_POINTS = 250
SL_POINTS = 400
PORTFOLIO_PCT = 0.5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

def telegram(msg):
if TELEGRAM_TOKEN == “YOUR_TELEGRAM_TOKEN”:
print(f”TELEGRAM: {msg}”)
return
try:
url = f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage”
requests.post(url, {“chat_id”: TELEGRAM_CHAT_ID, “text”: msg}, timeout=5)
except:
pass

def main():
if API_KEY == “YOUR_API_KEY_HERE”:
print(“ERROR: Set your API keys first!”)
return

```
try:
    exchange = ccxt.okx({
        "apiKey": API_KEY,
        "secret": SECRET, 
        "password": PASSWORD,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
        "sandbox": False
    })
    
    exchange.load_markets()
    
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL, params={"mgnMode": "cross"})
    except:
        pass
    
    # Check existing position
    positions = exchange.fetch_positions([SYMBOL])
    for pos in positions:
        if float(pos.get("info", {}).get("pos", 0)) != 0:
            print("Position already exists!")
            telegram("Position already exists!")
            return
    
    # Get balance
    balance_data = exchange.fetch_balance(params={"type": "trade"})
    balance = 0
    if "USDT" in balance_data:
        balance = balance_data["USDT"]["free"]
    else:
        for acc in balance_data.get("info", {}).get("data", []):
            if acc.get("ccy") == "USDT":
                balance = float(acc.get("availBal", 0))
                break
    
    if balance < 10:
        print("Balance too low!")
        return
    
    # Get price
    ticker = exchange.fetch_ticker(SYMBOL)
    price = ticker["last"]
    
    # Calculate size
    usable = balance * 0.9
    margin_factor = 0.06824
    max_notional = usable / margin_factor
    target_notional = balance * PORTFOLIO_PCT / margin_factor
    notional = min(max_notional, target_notional)
    contracts = math.floor(notional / price / 0.0001)
    
    if contracts < 1:
        print("Position too small!")
        return
    
    print(f"Opening {contracts} contracts at {price}")
    
    # Open long
    order = exchange.create_market_order(SYMBOL, "buy", contracts, None, None, {"tdMode": "cross"})
    
    print(f"Order placed: {order['id']}")
    
    time.sleep(3)
    
    # Set TP/SL
    tp_price = price + TP_POINTS
    sl_price = price - SL_POINTS
    
    exchange.create_order(SYMBOL, "TAKE_PROFIT_MARKET", "sell", contracts, price, {
        "triggerPrice": tp_price, "tdMode": "cross", "reduceOnly": True
    })
    
    exchange.create_order(SYMBOL, "STOP_LOSS_MARKET", "sell", contracts, price, {
        "triggerPrice": sl_price, "tdMode": "cross", "reduceOnly": True
    })
    
    msg = f"LONG OPENED: {contracts} contracts at {price}, TP: {tp_price}, SL: {sl_price}"
    print(msg)
    telegram(msg)
    
except Exception as e:
    error = f"ERROR: {e}"
    print(error)
    telegram(error)
```

if **name** == “**main**”:
main()
