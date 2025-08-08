import ccxt
import time
import requests
import logging
import json
import os
import sys
import math
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum

# ================================

# Configuration & Data Classes

# ================================

@dataclass
class TradingConfig:
“”“Trading configuration parameters”””
symbol: str = ‘BTC-USDT-SWAP’
leverage: int = 15
tp_distance_points: int = 250
sl_distance_points: int = 400
portfolio_percentage: float = 0.50
margin_factor: float = 0.06824  # OKX BTC-USDT-SWAP margin factor
safety_buffer: float = 0.10     # Keep 10% buffer
contract_size_btc: float = 0.0001

@dataclass
class APICredentials:
“”“API credentials configuration”””
api_key: str
secret: str
password: str
telegram_token: Optional[str] = None
telegram_chat_id: Optional[str] = None

class OrderSide(Enum):
“”“Order side enumeration”””
BUY = “buy”
SELL = “sell”

class PositionSide(Enum):
“”“Position side enumeration”””
LONG = “long”
SHORT = “short”

# ================================

# Exception Classes

# ================================

class TradingBotError(Exception):
“”“Base exception for trading bot”””
pass

class InsufficientBalanceError(TradingBotError):
“”“Raised when balance is insufficient”””
pass

class OrderExecutionError(TradingBotError):
“”“Raised when order execution fails”””
pass

class PositionError(TradingBotError):
“”“Raised when position operations fail”””
pass

# ================================

# Logger Setup

# ================================

def setup_logger() -> logging.Logger:
“”“Setup professional logging configuration”””
logger = logging.getLogger(‘OKXTradingBot’)
logger.setLevel(logging.INFO)

```
# Remove existing handlers
for handler in logger.handlers[:]:
    logger.removeHandler(handler)

# Create formatter
formatter = logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# File handler
file_handler = logging.FileHandler('okx_trading_bot.log', encoding='utf-8')
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
console_handler.setLevel(logging.INFO)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

return logger
```

# ================================

# Configuration Manager

# ================================

class ConfigManager:
“”“Manages configuration and credentials”””

```
@staticmethod
def load_credentials() -> APICredentials:
    """Load API credentials from environment variables"""
    api_key = os.getenv('OKX_API_KEY')
    secret = os.getenv('OKX_SECRET')
    password = os.getenv('OKX_PASSWORD')
    
    if not all([api_key, secret, password]):
        raise ValueError(
            "Missing required environment variables: OKX_API_KEY, OKX_SECRET, OKX_PASSWORD"
        )
    
    return APICredentials(
        api_key=api_key,
        secret=secret,
        password=password,
        telegram_token=os.getenv('TELEGRAM_TOKEN'),
        telegram_chat_id=os.getenv('TELEGRAM_CHAT_ID')
    )

@staticmethod
def load_trading_config() -> TradingConfig:
    """Load trading configuration with environment variable overrides"""
    config = TradingConfig()
    
    # Override with environment variables if present
    config.portfolio_percentage = float(os.getenv('PORTFOLIO_PERCENTAGE', config.portfolio_percentage))
    config.leverage = int(os.getenv('LEVERAGE', config.leverage))
    config.tp_distance_points = int(os.getenv('TP_DISTANCE', config.tp_distance_points))
    config.sl_distance_points = int(os.getenv('SL_DISTANCE', config.sl_distance_points))
    
    return config
```

# ================================

# Notification Manager

# ================================

class NotificationManager:
“”“Handles Telegram notifications”””

```
def __init__(self, credentials: APICredentials, logger: logging.Logger):
    self.credentials = credentials
    self.logger = logger
    self.enabled = bool(credentials.telegram_token and credentials.telegram_chat_id)
    
    if not self.enabled:
        self.logger.warning("Telegram notifications disabled - missing credentials")

def send_message(self, message: str, priority: str = "INFO") -> bool:
    """Send message via Telegram"""
    if not self.enabled:
        self.logger.info(f"TELEGRAM[{priority}]: {message}")
        return False
    
    try:
        url = f'https://api.telegram.org/bot{self.credentials.telegram_token}/sendMessage'
        params = {
            'chat_id': self.credentials.telegram_chat_id,
            'text': f"🤖 <b>OKX Bot</b> | {priority}\n\n{message}",
            'parse_mode': 'HTML'
        }
        
        response = requests.post(url, params=params, timeout=10)
        response.raise_for_status()
        
        self.logger.info(f"Telegram message sent: {message[:50]}...")
        return True
        
    except Exception as e:
        self.logger.error(f"Failed to send Telegram message: {e}")
        return False

def send_trade_alert(self, action: str, details: Dict[str, Any]) -> None:
    """Send formatted trade alert"""
    message = f"🔔 <b>{action.upper()}</b>\n"
    for key, value in details.items():
        message += f"• {key}: {value}\n"
    
    self.send_message(message, "TRADE")

def send_error_alert(self, error: str, details: Optional[str] = None) -> None:
    """Send error alert"""
    message = f"❌ <b>ERROR</b>\n{error}"
    if details:
        message += f"\n\nDetails: {details}"
    
    self.send_message(message, "ERROR")
```

# ================================

# Exchange Manager

# ================================

class OKXExchangeManager:
“”“Manages OKX exchange operations”””

```
def __init__(self, credentials: APICredentials, config: TradingConfig, logger: logging.Logger):
    self.credentials = credentials
    self.config = config
    self.logger = logger
    self.exchange: Optional[ccxt.okx] = None
    self.market_info: Optional[Dict] = None

def initialize(self) -> None:
    """Initialize exchange connection"""
    try:
        self.exchange = ccxt.okx({
            'apiKey': self.credentials.api_key,
            'secret': self.credentials.secret,
            'password': self.credentials.password,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',
                'adjustForTimeDifference': True,
            },
            'verbose': False,
            'timeout': 30000,
        })
        
        self.exchange.set_sandbox_mode(False)
        self.exchange.load_markets()
        
        self.market_info = self.exchange.market(self.config.symbol)
        if not self.market_info:
            raise ValueError(f"Market not found: {self.config.symbol}")
        
        # Set leverage
        try:
            self.exchange.set_leverage(
                self.config.leverage, 
                self.config.symbol, 
                params={'mgnMode': 'cross'}
            )
            self.logger.info(f"Leverage set to {self.config.leverage}x")
        except Exception as e:
            self.logger.warning(f"Failed to set leverage: {e}")
        
        self.logger.info("Exchange initialized successfully")
        
    except Exception as e:
        raise TradingBotError(f"Failed to initialize exchange: {e}")

def get_balance(self) -> float:
    """Get USDT balance"""
    try:
        balance_data = self.exchange.fetch_balance(params={'type': 'trade'})
        
        # Try standard format first
        if 'USDT' in balance_data and 'free' in balance_data['USDT']:
            return float(balance_data['USDT']['free'])
        
        # Try OKX raw format
        okx_data = balance_data.get('info', {}).get('data', [])
        for account in okx_data:
            if account.get('ccy') == 'USDT' and account.get('type') == 'TRADE':
                return float(account.get('availBal', 0.0))
        
        return 0.0
        
    except Exception as e:
        raise TradingBotError(f"Failed to fetch balance: {e}")

def get_current_price(self) -> float:
    """Get current market price"""
    try:
        ticker = self.exchange.fetch_ticker(self.config.symbol)
        return float(ticker['last'])
    except Exception as e:
        raise TradingBotError(f"Failed to fetch price: {e}")

def get_position(self) -> Optional[Dict[str, Any]]:
    """Get current position"""
    try:
        positions = self.exchange.fetch_positions([self.config.symbol])
        
        for pos in positions:
            pos_info = pos.get('info', {})
            pos_amount_str = pos_info.get('pos', '0')
            
            if float(pos_amount_str) != 0:
                return {
                    'side': PositionSide.LONG if float(pos_amount_str) > 0 else PositionSide.SHORT,
                    'size': abs(float(pos_amount_str)),
                    'entry_price': float(pos_info.get('avgPx', 0.0)),
                    'unrealized_pnl': float(pos_info.get('upl', 0.0))
                }
        
        return None
        
    except Exception as e:
        raise PositionError(f"Failed to fetch position: {e}")

def create_market_order(self, side: OrderSide, amount: float) -> Dict[str, Any]:
    """Create market order"""
    try:
        order = self.exchange.create_market_order(
            symbol=self.config.symbol,
            side=side.value,
            amount=amount,
            params={'tdMode': 'cross'}
        )
        
        if not order or not order.get('id'):
            raise OrderExecutionError("Order execution failed - no order ID returned")
        
        return order
        
    except Exception as e:
        raise OrderExecutionError(f"Failed to create market order: {e}")

def create_conditional_order(self, order_type: str, side: OrderSide, amount: float, 
                           trigger_price: float, current_price: float) -> Dict[str, Any]:
    """Create conditional order (TP/SL)"""
    try:
        order = self.exchange.create_order(
            symbol=self.config.symbol,
            type=order_type,
            side=side.value,
            amount=amount,
            price=current_price,
            params={
                'triggerPrice': trigger_price,
                'tdMode': 'cross',
                'reduceOnly': True,
            }
        )
        
        return order
        
    except Exception as e:
        raise OrderExecutionError(f"Failed to create {order_type} order: {e}")
```

# ================================

# Position Manager

# ================================

class PositionManager:
“”“Manages position calculations and operations”””

```
def __init__(self, config: TradingConfig, logger: logging.Logger):
    self.config = config
    self.logger = logger

def calculate_position_size(self, available_usdt: float, price: float) -> Tuple[float, Dict[str, float]]:
    """Calculate optimal position size and return details"""
    try:
        # Calculate usable amount with safety buffer
        usable_usdt = available_usdt * (1 - self.config.safety_buffer)
        
        # Calculate maximum notional based on margin requirements
        max_notional = usable_usdt / self.config.margin_factor
        
        # Calculate target notional based on portfolio percentage
        target_notional = min(
            max_notional, 
            available_usdt * self.config.portfolio_percentage / self.config.margin_factor
        )
        
        # Calculate contracts
        target_btc = target_notional / price
        contracts = math.floor(target_btc / self.config.contract_size_btc)
        
        if contracts < 1:
            raise InsufficientBalanceError("Calculated contract size too small")
        
        # Calculate actual values
        actual_notional = contracts * self.config.contract_size_btc * price
        required_margin = actual_notional * self.config.margin_factor
        
        # Validate margin requirements
        if required_margin > usable_usdt:
            raise InsufficientBalanceError("Insufficient margin for calculated position")
        
        details = {
            'contracts': float(contracts),
            'notional_value': actual_notional,
            'required_margin': required_margin,
            'margin_ratio': (required_margin / available_usdt) * 100,
            'usable_usdt': usable_usdt
        }
        
        return float(contracts), details
        
    except Exception as e:
        if isinstance(e, (InsufficientBalanceError,)):
            raise
        raise TradingBotError(f"Position calculation failed: {e}")

def calculate_tp_sl_prices(self, entry_price: float, side: PositionSide) -> Tuple[float, float]:
    """Calculate TP and SL prices"""
    if side == PositionSide.LONG:
        tp_price = entry_price + self.config.tp_distance_points
        sl_price = entry_price - self.config.sl_distance_points
    else:
        tp_price = entry_price - self.config.tp_distance_points
        sl_price = entry_price + self.config.sl_distance_points
    
    return tp_price, sl_price
```

# ================================

# Main Trading Bot

# ================================

class OKXTradingBot:
“”“Main trading bot class”””

```
def __init__(self):
    self.logger = setup_logger()
    self.config = ConfigManager.load_trading_config()
    self.credentials = ConfigManager.load_credentials()
    self.notifier = NotificationManager(self.credentials, self.logger)
    self.exchange_manager = OKXExchangeManager(self.credentials, self.config, self.logger)
    self.position_manager = PositionManager(self.config, self.logger)

def initialize(self) -> None:
    """Initialize the trading bot"""
    self.logger.info("=" * 60)
    self.logger.info("OKX Trading Bot - Professional Version")
    self.logger.info("=" * 60)
    
    try:
        self.exchange_manager.initialize()
        self.notifier.send_message("🚀 Trading bot initialized successfully")
        self.logger.info("Bot initialization completed")
    except Exception as e:
        self.logger.critical(f"Bot initialization failed: {e}")
        self.notifier.send_error_alert("Bot initialization failed", str(e))
        raise

def check_existing_position(self) -> Optional[Dict[str, Any]]:
    """Check for existing positions"""
    try:
        position = self.exchange_manager.get_position()
        if position:
            self.logger.info(f"Existing position found: {position['side'].value.upper()} "
                           f"{position['size']} contracts")
            
            self.notifier.send_trade_alert("EXISTING POSITION DETECTED", {
                "Side": position['side'].value.upper(),
                "Size": f"{position['size']} contracts",
                "Entry Price": f"{position['entry_price']:,.1f}",
                "Unrealized PnL": f"{position['unrealized_pnl']:+,.2f} USDT"
            })
        
        return position
        
    except Exception as e:
        self.logger.error(f"Failed to check existing position: {e}")
        return None

def execute_long_trade(self) -> bool:
    """Execute a long trade with TP/SL"""
    try:
        # Get market data
        current_price = self.exchange_manager.get_current_price()
        balance = self.exchange_manager.get_balance()
        
        self.logger.info(f"Market data - Price: {current_price:,.1f}, Balance: {balance:,.2f} USDT")
        
        # Calculate position size
        contracts, position_details = self.position_manager.calculate_position_size(balance, current_price)
        
        self.logger.info(f"Position calculation:")
        for key, value in position_details.items():
            if isinstance(value, float):
                self.logger.info(f"  {key}: {value:,.2f}")
            else:
                self.logger.info(f"  {key}: {value}")
        
        # Send pre-trade notification
        self.notifier.send_trade_alert("PREPARING LONG TRADE", {
            "Contracts": f"{contracts}",
            "Notional Value": f"{position_details['notional_value']:,.2f} USDT",
            "Required Margin": f"{position_details['required_margin']:,.2f} USDT",
            "Margin Ratio": f"{position_details['margin_ratio']:.1f}%",
            "Entry Price": f"{current_price:,.1f}"
        })
        
        # Execute market order
        self.logger.info(f"Executing LONG order: {contracts} contracts at {current_price:,.1f}")
        order = self.exchange_manager.create_market_order(OrderSide.BUY, contracts)
        
        self.logger.info(f"Order executed successfully - ID: {order.get('id')}")
        
        # Wait for order to fill
        time.sleep(3)
        
        # Set TP/SL
        tp_price, sl_price = self.position_manager.calculate_tp_sl_prices(current_price, PositionSide.LONG)
        
        self.logger.info(f"Setting TP/SL - TP: {tp_price:,.1f}, SL: {sl_price:,.1f}")
        
        # Create TP order
        tp_order = self.exchange_manager.create_conditional_order(
            'TAKE_PROFIT_MARKET', OrderSide.SELL, contracts, tp_price, current_price
        )
        
        # Create SL order
        sl_order = self.exchange_manager.create_conditional_order(
            'STOP_LOSS_MARKET', OrderSide.SELL, contracts, sl_price, current_price
        )
        
        # Send success notification
        self.notifier.send_trade_alert("LONG TRADE EXECUTED", {
            "Order ID": order.get('id'),
            "Contracts": f"{contracts}",
            "Entry Price": f"{current_price:,.1f}",
            "Take Profit": f"{tp_price:,.1f} (+{self.config.tp_distance_points})",
            "Stop Loss": f"{sl_price:,.1f} (-{self.config.sl_distance_points})",
            "TP Order ID": tp_order.get('id'),
            "SL Order ID": sl_order.get('id')
        })
        
        self.logger.info("Long trade executed successfully with TP/SL")
        return True
        
    except Exception as e:
        error_msg = f"Long trade execution failed: {e}"
        self.logger.error(error_msg)
        self.notifier.send_error_alert("TRADE EXECUTION FAILED", str(e))
        return False

def run_test_trade(self) -> None:
    """Run a test trade"""
    try:
        self.initialize()
        
        # Check for existing positions
        existing_position = self.check_existing_position()
        if existing_position:
            self.logger.info("Existing position detected - skipping new trade")
            return
        
        # Execute long trade
        success = self.execute_long_trade()
        
        if success:
            # Verify final position
            time.sleep(2)
            final_position = self.exchange_manager.get_position()
            
            if final_position:
                self.notifier.send_trade_alert("TRADE VERIFICATION", {
                    "Final Position": final_position['side'].value.upper(),
                    "Size": f"{final_position['size']} contracts",
                    "Entry Price": f"{final_position['entry_price']:,.1f}",
                    "Current PnL": f"{final_position['unrealized_pnl']:+,.2f} USDT"
                })
                
                self.logger.info("✅ Test trade completed successfully")
            else:
                self.logger.warning("⚠️ Position verification failed")
        else:
            self.logger.error("❌ Test trade failed")
            
    except Exception as e:
        self.logger.critical(f"Critical error in test trade: {e}")
        self.notifier.send_error_alert("CRITICAL ERROR", str(e))
```

# ================================

# Entry Point

# ================================

def main():
“”“Main entry point”””
try:
bot = OKXTradingBot()
bot.run_test_trade()
except KeyboardInterrupt:
print(”\n🛑 Bot stopped by user”)
except Exception as e:
print(f”💥 Fatal error: {e}”)
sys.exit(1)

if **name** == ‘**main**’:
main()
