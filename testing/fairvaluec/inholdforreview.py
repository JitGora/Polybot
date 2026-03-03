"""
Polymarket BTC 15-Min HFT Trading System - FIXED VERSION
=========================================================

Fixes:
1. Proper orderbook resubscription on new window
2. Manual strike price input on startup
3. Robust WebSocket reconnection with proper resubscription
4. Window state management with proper cleanup

"""

import asyncio
import websockets
import aiohttp
import json
import time
from dataclasses import dataclass, field
from typing import Optional, List, Deque, Callable
from collections import deque
from enum import Enum
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('HFT')


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class Config:
    """Trading configuration"""
    
    # Signal thresholds
    oracle_lag_threshold: float = 30.0
    oracle_time_threshold: float = 60.0
    momentum_threshold: float = 0.0004
    momentum_lookback: int = 5
    liquidation_threshold: float = 100000
    
    # Position management
    profit_target: float = 0.03
    stop_loss: float = 0.025
    max_hold_time: float = 90
    
    # Risk
    position_size: float = 100.0
    max_positions: int = 1
    cooldown_seconds: float = 10.0
    
    # Timing
    min_time_remaining: float = 20.0
    max_time_remaining: float = 800.0


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class Side(Enum):
    UP = "UP"
    DOWN = "DOWN"


class SignalType(Enum):
    ORACLE_LAG = "ORACLE_LAG"
    MOMENTUM = "MOMENTUM"
    LIQUIDATION = "LIQUIDATION"


@dataclass
class Signal:
    timestamp: int
    signal_type: SignalType
    side: Side
    strength: float
    entry_price: float
    reason: str
    binance_price: float
    chainlink_price: float
    strike_price: float
    time_remaining: float


@dataclass
class Position:
    entry_time: int
    side: Side
    entry_price: float
    size: float
    cost: float
    signal_type: SignalType
    profit_target: float
    stop_loss: float
    max_exit_time: int
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    
    def update_price(self, price: float):
        self.current_price = price
        self.unrealized_pnl = (price - self.entry_price) * self.size


@dataclass
class Trade:
    entry_time: int
    exit_time: int
    side: Side
    signal_type: SignalType
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    hold_time: float
    exit_reason: str


@dataclass 
class PricePoint:
    timestamp: int
    price: float


# =============================================================================
# PRICE STATE
# =============================================================================

class PriceState:
    """Maintains current price state from all sources"""
    
    def __init__(self):
        self.binance_spot: float = 0.0
        self.binance_spot_ts: int = 0
        self.chainlink: float = 0.0
        self.chainlink_ts: int = 0
        self.price_history: Deque[PricePoint] = deque(maxlen=100)
        
        self.up_bid: float = 0.0
        self.up_ask: float = 0.0
        self.down_bid: float = 0.0
        self.down_ask: float = 0.0
        self.polymarket_ts: int = 0
    
    def update_binance(self, price: float):
        now = int(time.time() * 1000)
        self.binance_spot = price
        self.binance_spot_ts = now
        self.price_history.append(PricePoint(now, price))
    
    def update_chainlink(self, price: float):
        self.chainlink = price
        self.chainlink_ts = int(time.time() * 1000)
    
    def update_polymarket(self, side: Side, bid: float, ask: float):
        if side == Side.UP:
            self.up_bid = bid
            self.up_ask = ask
        else:
            self.down_bid = bid
            self.down_ask = ask
        self.polymarket_ts = int(time.time() * 1000)
    
    def clear_polymarket(self):
        """Clear Polymarket prices (on new window)"""
        self.up_bid = 0.0
        self.up_ask = 0.0
        self.down_bid = 0.0
        self.down_ask = 0.0
        self.polymarket_ts = 0
    
    @property
    def oracle_lag(self) -> float:
        if self.binance_spot and self.chainlink:
            return self.binance_spot - self.chainlink
        return 0.0
    
    def get_price_change(self, lookback_ms: int) -> Optional[float]:
        if len(self.price_history) < 2:
            return None
        
        now = int(time.time() * 1000)
        cutoff = now - lookback_ms
        
        old_price = None
        for point in self.price_history:
            if point.timestamp >= cutoff:
                old_price = point.price
                break
        
        if old_price is None or old_price == 0:
            return None
        
        return (self.binance_spot - old_price) / old_price
    
    def get_entry_price(self, side: Side) -> float:
        if side == Side.UP:
            return self.up_ask
        return self.down_ask
    
    def get_exit_price(self, side: Side) -> float:
        if side == Side.UP:
            return self.up_bid
        return self.down_bid
    
    @property
    def has_polymarket_data(self) -> bool:
        """Check if we have valid Polymarket orderbook data"""
        return (
            self.up_bid > 0 and self.up_ask > 0 and
            self.down_bid > 0 and self.down_ask > 0
        )


# =============================================================================
# MARKET STATE - FIXED
# =============================================================================

class MarketState:
    """Current market/window state with proper lifecycle management"""
    
    def __init__(self):
        self.slug: str = ""
        self.window_start: int = 0
        self.window_end: int = 0
        self.strike_price: float = 0.0
        self.strike_captured: bool = False
        self.strike_source: str = ""  # "auto" or "manual"
        
        self.up_token_id: str = ""
        self.down_token_id: str = ""
        self.metadata_loaded: bool = False
        self.metadata_loading: bool = False
        
        # Track subscription state
        self.orderbook_subscribed: bool = False
    
    @property
    def time_remaining(self) -> float:
        return max(0, self.window_end - time.time())
    
    @property
    def time_elapsed(self) -> float:
        return time.time() - self.window_start
    
    @property
    def is_valid(self) -> bool:
        return (
            self.strike_captured and 
            self.metadata_loaded and
            self.time_remaining > 0 and
            self.orderbook_subscribed
        )
    
    @property
    def is_ready_for_trading(self) -> bool:
        """More permissive check for display"""
        return self.strike_captured and self.time_remaining > 0
    
    def new_window(self, window_start: int):
        """Reset for new window - COMPLETE RESET"""
        self.window_start = window_start
        self.window_end = window_start + 900
        self.slug = f"btc-updown-15m-{window_start}"
        
        # Reset all state
        self.strike_price = 0.0
        self.strike_captured = False
        self.strike_source = ""
        
        self.up_token_id = ""
        self.down_token_id = ""
        self.metadata_loaded = False
        self.metadata_loading = False
        self.orderbook_subscribed = False
        
        logger.info(f"Window reset: {self.slug}")
    
    def set_strike_manual(self, price: float):
        """Set strike price manually"""
        self.strike_price = price
        self.strike_captured = True
        self.strike_source = "manual"
        logger.info(f"Strike set manually: ${price:,.2f}")
    
    def set_strike_auto(self, price: float):
        """Set strike price from Chainlink at window start"""
        if not self.strike_captured:
            self.strike_price = price
            self.strike_captured = True
            self.strike_source = "auto"
            logger.info(f"Strike captured automatically: ${price:,.2f}")
    
    def set_tokens(self, up_token: str, down_token: str):
        """Set token IDs and mark for resubscription"""
        if up_token != self.up_token_id or down_token != self.down_token_id:
            self.up_token_id = up_token
            self.down_token_id = down_token
            self.orderbook_subscribed = False  # Need to resubscribe
            logger.info(f"Tokens updated: UP={up_token[:16]}... DOWN={down_token[:16]}...")


# =============================================================================
# SIGNAL DETECTORS
# =============================================================================

class OracleLagDetector:
    def __init__(self, config: Config, price_state: PriceState, market_state: MarketState):
        self.config = config
        self.price = price_state
        self.market = market_state
        self.last_signal_time: int = 0
    
    def check(self) -> Optional[Signal]:
        if not self.market.is_valid:
            return None
        
        if not self.price.has_polymarket_data:
            return None
        
        time_remaining = self.market.time_remaining
        
        if time_remaining > self.config.oracle_time_threshold:
            return None
        if time_remaining < 10:
            return None
        
        lag = self.price.oracle_lag
        if abs(lag) < self.config.oracle_lag_threshold:
            return None
        
        strike = self.market.strike_price
        if self.price.binance_spot > strike:
            side = Side.UP
            entry_price = self.price.up_ask
        else:
            side = Side.DOWN
            entry_price = self.price.down_ask
        
        if entry_price <= 0 or entry_price >= 1:
            return None
        
        strength = min(1.0, abs(self.price.binance_spot - strike) / 100)
        
        now = int(time.time() * 1000)
        if now - self.last_signal_time < self.config.cooldown_seconds * 1000:
            return None
        
        self.last_signal_time = now
        
        return Signal(
            timestamp=now,
            signal_type=SignalType.ORACLE_LAG,
            side=side,
            strength=strength,
            entry_price=entry_price,
            reason=f"Oracle lag ${lag:+.2f}",
            binance_price=self.price.binance_spot,
            chainlink_price=self.price.chainlink,
            strike_price=strike,
            time_remaining=time_remaining
        )


class MomentumDetector:
    def __init__(self, config: Config, price_state: PriceState, market_state: MarketState):
        self.config = config
        self.price = price_state
        self.market = market_state
        self.last_signal_time: int = 0
    
    def check(self) -> Optional[Signal]:
        if not self.market.is_valid:
            return None
        
        if not self.price.has_polymarket_data:
            return None
        
        time_remaining = self.market.time_remaining
        
        if time_remaining > self.config.max_time_remaining:
            return None
        if time_remaining < self.config.min_time_remaining:
            return None
        
        lookback_ms = self.config.momentum_lookback * 1000
        price_change = self.price.get_price_change(lookback_ms)
        
        if price_change is None:
            return None
        if abs(price_change) < self.config.momentum_threshold:
            return None
        
        if price_change > 0:
            side = Side.UP
            entry_price = self.price.up_ask
        else:
            side = Side.DOWN
            entry_price = self.price.down_ask
        
        if entry_price <= 0 or entry_price >= 1:
            return None
        
        strength = min(1.0, abs(price_change) / 0.001)
        
        now = int(time.time() * 1000)
        if now - self.last_signal_time < self.config.cooldown_seconds * 1000:
            return None
        
        self.last_signal_time = now
        
        return Signal(
            timestamp=now,
            signal_type=SignalType.MOMENTUM,
            side=side,
            strength=strength,
            entry_price=entry_price,
            reason=f"Momentum {price_change*100:+.3f}%",
            binance_price=self.price.binance_spot,
            chainlink_price=self.price.chainlink,
            strike_price=self.market.strike_price,
            time_remaining=time_remaining
        )


class LiquidationDetector:
    def __init__(self, config: Config, price_state: PriceState, market_state: MarketState):
        self.config = config
        self.price = price_state
        self.market = market_state
        self.last_signal_time: int = 0
        self.pending_liq: Optional[dict] = None
    
    def on_liquidation(self, side: str, value: float):
        if value < self.config.liquidation_threshold:
            return
        
        if side == "SELL":
            signal_side = Side.DOWN
        else:
            signal_side = Side.UP
        
        self.pending_liq = {
            'timestamp': int(time.time() * 1000),
            'side': signal_side,
            'value': value,
            'liq_side': side
        }
    
    def check(self) -> Optional[Signal]:
        if self.pending_liq is None:
            return None
        
        if not self.market.is_valid:
            self.pending_liq = None
            return None
        
        if not self.price.has_polymarket_data:
            self.pending_liq = None
            return None
        
        time_remaining = self.market.time_remaining
        if time_remaining < self.config.min_time_remaining:
            self.pending_liq = None
            return None
        
        now = int(time.time() * 1000)
        if now - self.pending_liq['timestamp'] > 2000:
            self.pending_liq = None
            return None
        
        if now - self.last_signal_time < self.config.cooldown_seconds * 1000:
            return None
        
        side = self.pending_liq['side']
        entry_price = self.price.get_entry_price(side)
        
        if entry_price <= 0 or entry_price >= 1:
            self.pending_liq = None
            return None
        
        strength = min(1.0, self.pending_liq['value'] / 500000)
        
        signal = Signal(
            timestamp=now,
            signal_type=SignalType.LIQUIDATION,
            side=side,
            strength=strength,
            entry_price=entry_price,
            reason=f"Liquidation ${self.pending_liq['value']/1000:.0f}K",
            binance_price=self.price.binance_spot,
            chainlink_price=self.price.chainlink,
            strike_price=self.market.strike_price,
            time_remaining=time_remaining
        )
        
        self.last_signal_time = now
        self.pending_liq = None
        
        return signal


# =============================================================================
# POSITION MANAGER
# =============================================================================

class PositionManager:
    def __init__(self, config: Config, price_state: PriceState, market_state: MarketState):
        self.config = config
        self.price = price_state
        self.market = market_state
        self.current_position: Optional[Position] = None
        self.trades: List[Trade] = []
        self.total_pnl: float = 0.0
        self.win_count: int = 0
        self.loss_count: int = 0
    
    @property
    def has_position(self) -> bool:
        return self.current_position is not None
    
    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total if total > 0 else 0.0
    
    def can_enter(self) -> bool:
        if self.has_position:
            return False
        if self.market.time_remaining < self.config.min_time_remaining:
            return False
        return True
    
    def enter(self, signal: Signal) -> bool:
        if not self.can_enter():
            return False
        
        now = int(time.time() * 1000)
        entry_price = signal.entry_price
        shares = self.config.position_size / entry_price
        cost = shares * entry_price
        
        profit_target = entry_price + self.config.profit_target
        stop_loss = entry_price - self.config.stop_loss
        max_exit_time = now + int(self.config.max_hold_time * 1000)
        
        self.current_position = Position(
            entry_time=now,
            side=signal.side,
            entry_price=entry_price,
            size=shares,
            cost=cost,
            signal_type=signal.signal_type,
            profit_target=profit_target,
            stop_loss=stop_loss,
            max_exit_time=max_exit_time,
            current_price=entry_price
        )
        
        logger.info(
            f"ENTRY: {signal.side.value} @ ${entry_price:.4f} | "
            f"Target: ${profit_target:.4f} | Stop: ${stop_loss:.4f}"
        )
        
        return True
    
    def check_exit(self) -> Optional[str]:
        if not self.has_position:
            return None
        
        pos = self.current_position
        now = int(time.time() * 1000)
        
        current_price = self.price.get_exit_price(pos.side)
        
        # Handle missing price data
        if current_price <= 0:
            return None
        
        pos.update_price(current_price)
        
        if current_price >= pos.profit_target:
            return "PROFIT_TARGET"
        if current_price <= pos.stop_loss:
            return "STOP_LOSS"
        if now >= pos.max_exit_time:
            return "MAX_TIME"
        if self.market.time_remaining < 20:
            return "WINDOW_END"
        
        return None
    
    def exit(self, reason: str) -> Trade:
        if not self.has_position:
            raise ValueError("No position to exit")
        
        pos = self.current_position
        now = int(time.time() * 1000)
        
        exit_price = self.price.get_exit_price(pos.side)
        if exit_price <= 0:
            exit_price = pos.current_price
        
        pnl = (exit_price - pos.entry_price) * pos.size
        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0
        hold_time = (now - pos.entry_time) / 1000
        
        trade = Trade(
            entry_time=pos.entry_time,
            exit_time=now,
            side=pos.side,
            signal_type=pos.signal_type,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=pos.size,
            pnl=pnl,
            pnl_pct=pnl_pct,
            hold_time=hold_time,
            exit_reason=reason
        )
        
        self.trades.append(trade)
        self.total_pnl += pnl
        
        if pnl > 0:
            self.win_count += 1
        else:
            self.loss_count += 1
        
        logger.info(
            f"EXIT: {pos.side.value} @ ${exit_price:.4f} | "
            f"PnL: ${pnl:+.4f} | Reason: {reason}"
        )
        
        self.current_position = None
        return trade
    
    def force_close_on_window_end(self):
        """Force close position at window end"""
        if self.has_position:
            self.exit("WINDOW_END_FORCE")


# =============================================================================
# DATA COLLECTORS - FIXED
# =============================================================================

class BinanceCollector:
    """Binance data collector"""
    
    def __init__(self, price_state: PriceState, on_liquidation: Callable):
        self.price = price_state
        self.on_liquidation = on_liquidation
    
    async def run_spot(self):
        url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("✅ Connected to Binance Spot")
                    
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        if 'p' in data:
                            self.price.update_binance(float(data['p']))
                            
            except Exception as e:
                logger.warning(f"Binance Spot error: {e}")
                await asyncio.sleep(2)
    
    async def run_futures(self):
        url = "wss://fstream.binance.com/ws/btcusdt@forceOrder"
        
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("✅ Connected to Binance Futures")
                    
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        
                        if 'o' in data:
                            order = data['o']
                            side = order.get('S', '')
                            price = float(order.get('p', 0))
                            qty = float(order.get('q', 0))
                            value = price * qty
                            
                            if value > 50000:
                                logger.info(f"🔥 LIQUIDATION: {side} ${value:,.0f}")
                                self.on_liquidation(side, value)
                            
            except Exception as e:
                logger.warning(f"Binance Futures error: {e}")
                await asyncio.sleep(2)


class ChainlinkCollector:
    """Chainlink oracle collector with proper strike capture"""
    
    def __init__(self, price_state: PriceState, market_state: MarketState):
        self.price = price_state
        self.market = market_state
        self._last_window_start: int = 0
    
    async def run(self):
        url = "wss://ws-live-data.polymarket.com"
        
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("✅ Connected to Chainlink RTDS")
                    
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": "{\"symbol\":\"btc/usd\"}"
                        }]
                    }))
                    
                    while True:
                        msg = await ws.recv()
                        
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        
                        if data.get('topic') == 'crypto_prices_chainlink':
                            if data.get('type') == 'update':
                                price = float(data['payload']['value'])
                                self.price.update_chainlink(price)
                                
                                # Auto-capture strike at window transition
                                if self.market.window_start != self._last_window_start:
                                    self._last_window_start = self.market.window_start
                                    
                                    # Only auto-capture if not manually set and very early in window
                                    if not self.market.strike_captured and self.market.time_elapsed < 5:
                                        self.market.set_strike_auto(price)
                            
            except Exception as e:
                logger.warning(f"Chainlink error: {e}")
                await asyncio.sleep(2)


class PolymarketCollector:
    """
    Polymarket CLOB collector - FIXED with proper resubscription
    """
    
    def __init__(self, price_state: PriceState, market_state: MarketState):
        self.price = price_state
        self.market = market_state
        self._subscribed_tokens: tuple = ("", "")
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._subscription_lock = asyncio.Lock()
    
    async def run(self):
        """Main WebSocket loop"""
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    self._ws = ws
                    logger.info("✅ Connected to Polymarket CLOB")
                    
                    # Reset subscription state on reconnect
                    self._subscribed_tokens = ("", "")
                    self.market.orderbook_subscribed = False
                    
                    # Start subscription checker
                    checker_task = asyncio.create_task(self._subscription_checker())
                    
                    try:
                        while True:
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                                self._process_message(msg)
                            except asyncio.TimeoutError:
                                # Check if we need to resubscribe
                                await self._check_subscription()
                                continue
                    finally:
                        checker_task.cancel()
                            
            except Exception as e:
                logger.warning(f"Polymarket CLOB error: {e}")
                self._ws = None
                self.market.orderbook_subscribed = False
                await asyncio.sleep(2)
    
    async def _subscription_checker(self):
        """Periodically check if subscription needs update"""
        while True:
            await asyncio.sleep(1)
            await self._check_subscription()
    
    async def _check_subscription(self):
        """Check and update subscription if needed"""
        if not self._ws:
            return
        
        current_tokens = (self.market.up_token_id, self.market.down_token_id)
        
        # Check if tokens are valid and different from subscribed
        if not all(current_tokens):
            return
        
        if current_tokens == self._subscribed_tokens and self.market.orderbook_subscribed:
            return
        
        async with self._subscription_lock:
            try:
                # Clear old orderbook data
                self.price.clear_polymarket()
                
                # Subscribe to new tokens
                subscribe_msg = {
                    "type": "market",
                    "assets_ids": [current_tokens[0], current_tokens[1]]
                }
                
                await self._ws.send(json.dumps(subscribe_msg))
                
                self._subscribed_tokens = current_tokens
                self.market.orderbook_subscribed = True
                
                logger.info(f"📚 Subscribed to orderbook: UP={current_tokens[0][:12]}... DOWN={current_tokens[1][:12]}...")
                
            except Exception as e:
                logger.error(f"Subscription error: {e}")
                self.market.orderbook_subscribed = False
    
    def _process_message(self, msg: str):
        """Process incoming message"""
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return
        
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._process_book(item)
        elif isinstance(data, dict):
            self._process_book(data)
    
    def _process_book(self, data: dict):
        """Process orderbook update"""
        event_type = data.get('event_type')
        asset_id = data.get('asset_id')
        
        if not asset_id:
            return
        
        # Determine which side this is for
        if asset_id == self.market.up_token_id:
            side = Side.UP
        elif asset_id == self.market.down_token_id:
            side = Side.DOWN
        else:
            # Unknown token - might be from previous window
            return
        
        if event_type == 'book':
            bids = data.get('bids', [])
            asks = data.get('asks', [])
            
            best_bid = float(bids[0]['price']) if bids else 0
            best_ask = float(asks[0]['price']) if asks else 0
            
            self.price.update_polymarket(side, best_bid, best_ask)
            
        elif event_type == 'price_change':
            # Price change updates
            price = float(data.get('price', 0))
            if price > 0:
                # Update both bid and ask with same price as approximation
                current_bid = self.price.up_bid if side == Side.UP else self.price.down_bid
                current_ask = self.price.up_ask if side == Side.UP else self.price.down_ask
                
                # Estimate new bid/ask around new price
                spread = current_ask - current_bid if current_ask > current_bid else 0.01
                new_bid = price - spread / 2
                new_ask = price + spread / 2
                
                self.price.update_polymarket(side, new_bid, new_ask)


# =============================================================================
# STARTUP HELPERS
# =============================================================================

async def get_strike_from_user(market_state: MarketState) -> None:
    """
    Prompt user for strike price if needed.
    Called on startup if we're mid-window.
    """
    print("\n" + "=" * 60)
    print("  STRIKE PRICE REQUIRED")
    print("=" * 60)
    print(f"\n  Current window: {market_state.slug}")
    print(f"  Time remaining: {market_state.time_remaining:.0f} seconds")
    print("\n  The bot started mid-window and needs the strike price.")
    print("  You can find this on Polymarket or in the market question.")
    print()
    
    while True:
        try:
            user_input = input("  Enter strike price (e.g., 97500.50): $")
            strike = float(user_input.replace(",", "").strip())
            
            if strike < 10000 or strike > 200000:
                print("  Invalid price. BTC price should be between $10,000 and $200,000")
                continue
            
            market_state.set_strike_manual(strike)
            print(f"\n  ✅ Strike price set to ${strike:,.2f}")
            break
            
        except ValueError:
            print("  Invalid input. Please enter a number.")
        except KeyboardInterrupt:
            print("\n  Cancelled.")
            sys.exit(0)
    
    print("=" * 60 + "\n")


async def fetch_strike_from_question(market_state: MarketState) -> bool:
    """
    Try to fetch strike price from market question.
    Returns True if successful.
    """
    slug = market_state.slug
    
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://gamma-api.polymarket.com/events"
            params = {'slug': slug}
            
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    if data:
                        market = data[0].get('markets', [{}])[0]
                        question = market.get('question', '')
                        
                        # Parse strike from question
                        # Format: "Will BTC be above $97,850.32 at..."
                        if '$' in question:
                            import re
                            match = re.search(r'\$([0-9,]+\.?\d*)', question)
                            if match:
                                strike_str = match.group(1).replace(',', '')
                                strike = float(strike_str)
                                market_state.set_strike_manual(strike)
                                logger.info(f"Strike parsed from question: ${strike:,.2f}")
                                return True
                                
    except Exception as e:
        logger.warning(f"Could not fetch strike from question: {e}")
    
    return False


# =============================================================================
# MAIN TRADING ENGINE - FIXED
# =============================================================================

class HFTTradingEngine:
    """Main HFT trading engine with fixed window handling"""
    
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        
        # State
        self.price_state = PriceState()
        self.market_state = MarketState()
        
        # Signal detectors
        self.oracle_detector = OracleLagDetector(
            self.config, self.price_state, self.market_state
        )
        self.momentum_detector = MomentumDetector(
            self.config, self.price_state, self.market_state
        )
        self.liquidation_detector = LiquidationDetector(
            self.config, self.price_state, self.market_state
        )
        
        # Position manager
        self.position_manager = PositionManager(
            self.config, self.price_state, self.market_state
        )
        
        # Data collectors
        self.binance = BinanceCollector(
            self.price_state,
            self.liquidation_detector.on_liquidation
        )
        self.chainlink = ChainlinkCollector(
            self.price_state, self.market_state
        )
        self.polymarket = PolymarketCollector(
            self.price_state, self.market_state
        )
        
        # Window tracking
        self.current_window_start: int = 0
        self._metadata_fetch_task: Optional[asyncio.Task] = None
        self._startup_complete: bool = False
    
    async def _startup(self):
        """Handle startup sequence including strike price"""
        now = int(time.time())
        window_start = (now // 900) * 900
        
        self.current_window_start = window_start
        self.market_state.new_window(window_start)
        
        # Wait for some price data
        logger.info("Waiting for price data...")
        for _ in range(50):  # 5 seconds max
            if self.price_state.chainlink > 0:
                break
            await asyncio.sleep(0.1)
        
        # Check if we're early in the window (first 10 seconds)
        time_elapsed = time.time() - window_start
        
        if time_elapsed < 10:
            # Early enough - Chainlink price is approximately the strike
            logger.info("Starting early in window - will auto-capture strike")
        else:
            # Mid-window start - need strike price
            logger.info("Starting mid-window - need strike price")
            
            # Try to fetch from API first
            if not await fetch_strike_from_question(self.market_state):
                # Need manual input
                await get_strike_from_user(self.market_state)
        
        # Fetch metadata
        await self._fetch_metadata()
        
        self._startup_complete = True
        logger.info("✅ Startup complete")
    
    async def _check_window(self):
        """Check for new window with proper cleanup"""
        now = int(time.time())
        window_start = (now // 900) * 900
        
        if window_start == self.current_window_start:
            return
        
        # New window detected
        logger.info("=" * 50)
        logger.info(f"🔄 NEW WINDOW DETECTED")
        logger.info("=" * 50)
        
        # Close any open position
        self.position_manager.force_close_on_window_end()
        
        # Cancel any pending metadata fetch
        if self._metadata_fetch_task and not self._metadata_fetch_task.done():
            self._metadata_fetch_task.cancel()
        
        # Reset state
        self.current_window_start = window_start
        self.market_state.new_window(window_start)
        self.price_state.clear_polymarket()
        
        # Start metadata fetch for new window
        self._metadata_fetch_task = asyncio.create_task(self._fetch_metadata())
    
    async def _fetch_metadata(self):
        """Fetch market metadata with retry"""
        slug = self.market_state.slug
        
        if self.market_state.metadata_loading:
            return
        
        self.market_state.metadata_loading = True
        
        # Wait for market to appear
        await asyncio.sleep(35)
        
        for attempt in range(10):
            # Check if window changed while waiting
            if self.market_state.slug != slug:
                logger.info(f"Window changed during metadata fetch, aborting for {slug}")
                return
            
            try:
                async with aiohttp.ClientSession() as session:
                    url = "https://gamma-api.polymarket.com/events"
                    params = {'slug': slug}
                    
                    async with session.get(url, params=params, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            
                            if data:
                                market = data[0].get('markets', [{}])[0]
                                token_ids = json.loads(market.get('clobTokenIds', '[]'))
                                outcomes = json.loads(market.get('outcomes', '[]'))
                                
                                if len(token_ids) >= 2:
                                    # Check window hasn't changed
                                    if self.market_state.slug != slug:
                                        return
                                    
                                    if outcomes[0].upper() in ['UP', 'YES']:
                                        up_token = token_ids[0]
                                        down_token = token_ids[1]
                                    else:
                                        up_token = token_ids[1]
                                        down_token = token_ids[0]
                                    
                                    self.market_state.set_tokens(up_token, down_token)
                                    self.market_state.metadata_loaded = True
                                    
                                    logger.info(f"✅ Metadata loaded for {slug}")
                                    return
                
                logger.info(f"Metadata not found for {slug}, retry {attempt+1}/10")
                
            except asyncio.CancelledError:
                logger.info(f"Metadata fetch cancelled for {slug}")
                return
            except Exception as e:
                logger.warning(f"Metadata fetch error: {e}")
            
            await asyncio.sleep(10)
        
        logger.error(f"Failed to load metadata for {slug} after 10 attempts")
    
    def _check_signals(self) -> Optional[Signal]:
        """Check all signal detectors - priority order"""
        
        # Don't check if no orderbook data
        if not self.price_state.has_polymarket_data:
            return None
        
        signal = self.liquidation_detector.check()
        if signal:
            return signal
        
        signal = self.oracle_detector.check()
        if signal:
            return signal
        
        signal = self.momentum_detector.check()
        if signal:
            return signal
        
        return None
    
    async def _trading_loop(self):
        """Main trading loop"""
        logger.info("Trading loop started")
        
        while True:
            try:
                if not self._startup_complete:
                    await asyncio.sleep(0.1)
                    continue
                
                # Check window
                await self._check_window()
                
                # Check for exit if in position
                if self.position_manager.has_position:
                    exit_reason = self.position_manager.check_exit()
                    if exit_reason:
                        self.position_manager.exit(exit_reason)
                
                # Check for entry signals
                if not self.position_manager.has_position:
                    signal = self._check_signals()
                    if signal:
                        self.position_manager.enter(signal)
                
                # Display
                self._display()
                
            except Exception as e:
                logger.error(f"Trading loop error: {e}")
                import traceback
                traceback.print_exc()
            
            await asyncio.sleep(0.1)
    
    def _display(self):
        """Display current state"""
        print("\033[2J\033[H", end="")
        
        pm = self.position_manager
        ps = self.price_state
        ms = self.market_state
        
        print("=" * 70)
        print("           POLYMARKET HFT TRADING SYSTEM (FIXED)")
        print("=" * 70)
        print()
        
        # Startup status
        if not self._startup_complete:
            print("  ⏳ Starting up...")
            return
        
        # Window info
        time_left = ms.time_remaining
        mins, secs = int(time_left) // 60, int(time_left) % 60
        
        print(f"  Window:      {ms.slug}")
        print(f"  Strike:      ${ms.strike_price:,.2f} ({ms.strike_source})" if ms.strike_price else "  Strike:      ⏳ Waiting...")
        print(f"  Time:        {mins:02d}:{secs:02d}")
        
        # Status indicators
        status_parts = []
        if ms.strike_captured:
            status_parts.append("✅ Strike")
        else:
            status_parts.append("❌ Strike")
        
        if ms.metadata_loaded:
            status_parts.append("✅ Metadata")
        else:
            status_parts.append("❌ Metadata")
        
        if ms.orderbook_subscribed:
            status_parts.append("✅ Orderbook")
        else:
            status_parts.append("❌ Orderbook")
        
        print(f"  Status:      {' | '.join(status_parts)}")
        
        print()
        
        # Prices
        print("  PRICES")
        print("  " + "-" * 55)
        
        if ps.binance_spot:
            print(f"  Binance:     ${ps.binance_spot:>12,.2f}")
        else:
            print(f"  Binance:     ⏳ Waiting...")
        
        if ps.chainlink:
            print(f"  Chainlink:   ${ps.chainlink:>12,.2f}")
        else:
            print(f"  Chainlink:   ⏳ Waiting...")
        
        lag = ps.oracle_lag
        if lag and ps.binance_spot and ps.chainlink:
            color = "\033[92m" if lag > 0 else "\033[91m"
            print(f"  Oracle Lag:  {color}${lag:>+12,.2f}\033[0m")
        
        print()
        
        # Polymarket orderbook
        print("  POLYMARKET ORDERBOOK")
        print("  " + "-" * 55)
        
        if ps.has_polymarket_data:
            up_spread = ps.up_ask - ps.up_bid if ps.up_ask > ps.up_bid else 0
            down_spread = ps.down_ask - ps.down_bid if ps.down_ask > ps.down_bid else 0
            
            print(f"  UP:    Bid ${ps.up_bid:.4f}  |  Ask ${ps.up_ask:.4f}  |  Spread ${up_spread:.4f}")
            print(f"  DOWN:  Bid ${ps.down_bid:.4f}  |  Ask ${ps.down_ask:.4f}  |  Spread ${down_spread:.4f}")
        else:
            print(f"  ⏳ Waiting for orderbook data...")
            if ms.up_token_id:
                print(f"  UP Token:   {ms.up_token_id[:24]}...")
                print(f"  DOWN Token: {ms.down_token_id[:24]}...")
        
        print()
        
        # Position
        print("  POSITION")
        print("  " + "-" * 55)
        
        if pm.has_position:
            pos = pm.current_position
            pnl_color = "\033[92m" if pos.unrealized_pnl >= 0 else "\033[91m"
            hold_time = (int(time.time() * 1000) - pos.entry_time) / 1000
            
            print(f"  Side:        {pos.side.value}")
            print(f"  Entry:       ${pos.entry_price:.4f}")
            print(f"  Current:     ${pos.current_price:.4f}")
            print(f"  PnL:         {pnl_color}${pos.unrealized_pnl:+.4f}\033[0m")
            print(f"  Target:      ${pos.profit_target:.4f}")
            print(f"  Stop:        ${pos.stop_loss:.4f}")
            print(f"  Hold Time:   {hold_time:.1f}s / {self.config.max_hold_time}s")
        else:
            print("  No position")
            if ms.is_valid:
                print("  ✅ Ready for signals")
            else:
                print("  ⏳ Waiting for market data...")
        
        print()
        
        # Performance
        print("  PERFORMANCE")
        print("  " + "-" * 55)
        total_trades = pm.win_count + pm.loss_count
        print(f"  Trades:      {total_trades} (W: {pm.win_count} / L: {pm.loss_count})")
        print(f"  Win Rate:    {pm.win_rate*100:.1f}%")
        
        pnl_color = "\033[92m" if pm.total_pnl >= 0 else "\033[91m"
        print(f"  Total PnL:   {pnl_color}${pm.total_pnl:+.4f}\033[0m")
        
        if total_trades > 0:
            avg_pnl = pm.total_pnl / total_trades
            print(f"  Avg PnL:     ${avg_pnl:+.4f}")
        
        print()
        print("  Press Ctrl+C to stop")
        print("=" * 70)
    
    async def run(self):
        """Start the trading engine"""
        logger.info("Starting HFT Trading Engine")
        
        # Start data collectors first
        collector_tasks = [
            asyncio.create_task(self.binance.run_spot()),
            asyncio.create_task(self.binance.run_futures()),
            asyncio.create_task(self.chainlink.run()),
            asyncio.create_task(self.polymarket.run()),
        ]
        
        # Wait a moment for connections
        await asyncio.sleep(2)
        
        # Run startup sequence
        await self._startup()
        
        # Start trading loop
        trading_task = asyncio.create_task(self._trading_loop())
        
        tasks = collector_tasks + [trading_task]
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Shutting down...")


# =============================================================================
# ENTRY POINT
# =============================================================================

async def main():
    config = Config(
        # Signal thresholds
        oracle_lag_threshold=30.0,
        oracle_time_threshold=60.0,
        momentum_threshold=0.0004,
        liquidation_threshold=100000,
        
        # Position management
        profit_target=0.03,
        stop_loss=0.025,
        max_hold_time=90,
        
        # Risk
        position_size=100.0,
        cooldown_seconds=10.0,
        
        # Timing
        min_time_remaining=20.0,
        max_time_remaining=800.0
    )
    
    engine = HFTTradingEngine(config)
    await engine.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nTrading stopped.")