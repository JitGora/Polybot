"""
Polymarket BTC 15-Min Fair Value Trading System
================================================

A complete system for calculating real-time fair value probabilities
and generating trading signals for Polymarket BTC 15-minute markets.

Components:
1. Multi-source data collection (Binance, Chainlink, Polymarket)
2. Volatility estimation engine
3. Drift/momentum detection from order flow
4. Probability calculation engine
5. Edge detection and signal generation

Author: Trading System Builder
Version: 1.0
"""

import asyncio
import websockets
import aiohttp
import json
import time
import math
import csv
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Callable, Any
from collections import deque
from datetime import datetime
from enum import Enum
from abc import ABC, abstractmethod

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('FairValueSystem')


# =============================================================================
# SECTION 1: DATA STRUCTURES
# =============================================================================

class Side(Enum):
    UP = "UP"
    DOWN = "DOWN"


@dataclass
class Trade:
    """Single trade record"""
    timestamp: int  # milliseconds
    price: float
    quantity: float
    is_buyer_maker: bool  # True = sell aggressor, False = buy aggressor
    
    @property
    def side(self) -> str:
        return "SELL" if self.is_buyer_maker else "BUY"


@dataclass
class OrderBookLevel:
    """Single orderbook level"""
    price: float
    size: float


@dataclass
class OrderBook:
    """Full orderbook state"""
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)
    timestamp: int = 0
    
    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None
    
    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None
    
    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None
    
    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None
    
    def bid_depth(self, levels: int = 5) -> float:
        """Total bid size in top N levels"""
        return sum(level.size for level in self.bids[:levels])
    
    def ask_depth(self, levels: int = 5) -> float:
        """Total ask size in top N levels"""
        return sum(level.size for level in self.asks[:levels])
    
    def imbalance(self, levels: int = 5) -> float:
        """Book imbalance: positive = bid heavy, negative = ask heavy"""
        bid_depth = self.bid_depth(levels)
        ask_depth = self.ask_depth(levels)
        total = bid_depth + ask_depth
        if total == 0:
            return 0
        return (bid_depth - ask_depth) / total


@dataclass
class MarketState:
    """Current state of a Polymarket market"""
    slug: str
    window_start: int
    window_end: int
    strike_price: Optional[float] = None
    up_token_id: Optional[str] = None
    down_token_id: Optional[str] = None
    condition_id: Optional[str] = None
    question: Optional[str] = None
    
    # Orderbooks
    up_orderbook: OrderBook = field(default_factory=OrderBook)
    down_orderbook: OrderBook = field(default_factory=OrderBook)
    
    @property
    def time_remaining(self) -> float:
        """Seconds until window end"""
        return max(0, self.window_end - time.time())
    
    @property
    def time_elapsed(self) -> float:
        """Seconds since window start"""
        return time.time() - self.window_start
    
    @property
    def time_fraction_remaining(self) -> float:
        """Fraction of window remaining (0 to 1)"""
        return self.time_remaining / 900


@dataclass
class PriceState:
    """Current price state from all sources"""
    binance_spot: Optional[float] = None
    binance_spot_timestamp: int = 0
    
    chainlink_oracle: Optional[float] = None
    chainlink_timestamp: int = 0
    
    binance_futures: Optional[float] = None
    binance_futures_timestamp: int = 0
    
    @property
    def oracle_lag(self) -> Optional[float]:
        """Difference between Binance spot and Chainlink"""
        if self.binance_spot and self.chainlink_oracle:
            return self.binance_spot - self.chainlink_oracle
        return None
    
    @property
    def futures_basis(self) -> Optional[float]:
        """Futures premium/discount vs spot"""
        if self.binance_spot and self.binance_futures:
            return self.binance_futures - self.binance_spot
        return None


@dataclass
class FairValue:
    """Calculated fair value and edge"""
    timestamp: int
    
    # Probabilities
    prob_up: float
    prob_down: float
    
    # Confidence
    confidence: float  # 0 to 1
    
    # Model inputs
    distance_to_strike: float  # in price terms
    distance_in_sigma: float   # standardized
    volatility_15m: float
    drift_estimate: float
    time_remaining: float
    
    # Edge calculations (if market prices available)
    up_market_price: Optional[float] = None
    down_market_price: Optional[float] = None
    up_edge: Optional[float] = None
    down_edge: Optional[float] = None
    
    @property
    def best_edge(self) -> Optional[float]:
        """Highest absolute edge"""
        edges = [e for e in [self.up_edge, self.down_edge] if e is not None]
        if not edges:
            return None
        return max(edges, key=abs)
    
    @property
    def recommended_side(self) -> Optional[Side]:
        """Side with positive edge"""
        if self.up_edge and self.up_edge > 0:
            return Side.UP
        if self.down_edge and self.down_edge > 0:
            return Side.DOWN
        return None


@dataclass
class Signal:
    """Trading signal"""
    timestamp: int
    side: Side
    action: str  # "BUY" or "SELL"
    
    entry_price: float
    fair_value: float
    edge: float
    confidence: float
    
    # Context
    strike_price: float
    current_price: float
    time_remaining: float
    volatility: float
    
    # Sizing suggestion
    kelly_fraction: float
    suggested_size_pct: float  # of bankroll
    
    def __str__(self):
        return (
            f"SIGNAL: {self.action} {self.side.value} @ ${self.entry_price:.4f} | "
            f"FV: ${self.fair_value:.4f} | Edge: {self.edge*100:.2f}% | "
            f"Conf: {self.confidence*100:.1f}% | Size: {self.suggested_size_pct*100:.1f}%"
        )


# =============================================================================
# SECTION 2: EVENT BUS
# =============================================================================

class EventBus:
    """Simple pub/sub event bus for component communication"""
    
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
    
    def subscribe(self, event_type: str, callback: Callable):
        """Subscribe to an event type"""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
    
    async def publish(self, event_type: str, data: Any):
        """Publish an event to all subscribers"""
        if event_type in self._subscribers:
            for callback in self._subscribers[event_type]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(data)
                    else:
                        callback(data)
                except Exception as e:
                    logger.error(f"Event handler error: {e}")


# =============================================================================
# SECTION 3: DATA COLLECTORS
# =============================================================================

class BaseWebSocketCollector(ABC):
    """Base class for WebSocket data collectors"""
    
    def __init__(self, event_bus: EventBus, name: str):
        self.event_bus = event_bus
        self.name = name
        self.logger = logging.getLogger(name)
        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
    
    @property
    @abstractmethod
    def ws_url(self) -> str:
        """WebSocket URL to connect to"""
        pass
    
    @abstractmethod
    async def on_connect(self, ws: websockets.WebSocketClientProtocol):
        """Called after connection established"""
        pass
    
    @abstractmethod
    async def on_message(self, data: Any):
        """Called for each parsed message"""
        pass
    
    async def run(self):
        """Main run loop with reconnection"""
        self._running = True
        retry_delay = 1
        max_retry_delay = 30
        
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                ) as ws:
                    self._ws = ws
                    self.logger.info(f"Connected to {self.ws_url}")
                    retry_delay = 1
                    
                    await self.on_connect(ws)
                    
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            
                            # Parse JSON with error handling
                            try:
                                data = json.loads(msg)
                            except json.JSONDecodeError:
                                continue
                            
                            await self.on_message(data)
                            
                        except asyncio.TimeoutError:
                            # Send ping to keep alive
                            await ws.ping()
                            
            except asyncio.CancelledError:
                self._running = False
                break
            except Exception as e:
                self.logger.warning(f"Connection error: {e}, retrying in {retry_delay}s")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)
    
    def stop(self):
        """Stop the collector"""
        self._running = False


class BinanceSpotCollector(BaseWebSocketCollector):
    """Collects Binance spot trades and orderbook"""
    
    def __init__(self, event_bus: EventBus):
        super().__init__(event_bus, "BinanceSpot")
        self.trades: deque = deque(maxlen=1000)
        self.orderbook = OrderBook()
        self.last_price: Optional[float] = None
    
    @property
    def ws_url(self) -> str:
        return "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@depth20@100ms"
    
    async def on_connect(self, ws):
        self.logger.info("Subscribed to BTCUSDT trade and depth streams")
    
    async def on_message(self, data: Any):
        if 'stream' not in data:
            return
        
        stream = data['stream']
        payload = data['data']
        
        if 'trade' in stream:
            await self._handle_trade(payload)
        elif 'depth' in stream:
            await self._handle_depth(payload)
    
    async def _handle_trade(self, data: dict):
        """Handle trade message"""
        trade = Trade(
            timestamp=data['T'],
            price=float(data['p']),
            quantity=float(data['q']),
            is_buyer_maker=data['m']
        )
        
        self.trades.append(trade)
        self.last_price = trade.price
        
        await self.event_bus.publish('binance_spot_trade', trade)
        await self.event_bus.publish('binance_spot_price', trade.price)
    
    async def _handle_depth(self, data: dict):
        """Handle orderbook depth update"""
        self.orderbook.bids = [
            OrderBookLevel(float(p), float(s)) 
            for p, s in data.get('bids', [])
        ]
        self.orderbook.asks = [
            OrderBookLevel(float(p), float(s)) 
            for p, s in data.get('asks', [])
        ]
        self.orderbook.timestamp = int(time.time() * 1000)
        
        await self.event_bus.publish('binance_spot_orderbook', self.orderbook)


class BinanceFuturesCollector(BaseWebSocketCollector):
    """Collects Binance futures data including funding, OI, liquidations"""
    
    def __init__(self, event_bus: EventBus):
        super().__init__(event_bus, "BinanceFutures")
        self.trades: deque = deque(maxlen=1000)
        self.last_price: Optional[float] = None
        self.funding_rate: Optional[float] = None
        self.open_interest: Optional[float] = None
    
    @property
    def ws_url(self) -> str:
        return "wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@forceOrder"
    
    async def on_connect(self, ws):
        self.logger.info("Subscribed to BTCUSDT futures streams")
        # Start funding rate polling
        asyncio.create_task(self._poll_funding())
    
    async def on_message(self, data: Any):
        if 'stream' not in data:
            return
        
        stream = data['stream']
        payload = data['data']
        
        if 'aggTrade' in stream:
            await self._handle_trade(payload)
        elif 'forceOrder' in stream:
            await self._handle_liquidation(payload)
    
    async def _handle_trade(self, data: dict):
        """Handle aggregate trade"""
        trade = Trade(
            timestamp=data['T'],
            price=float(data['p']),
            quantity=float(data['q']),
            is_buyer_maker=data['m']
        )
        
        self.trades.append(trade)
        self.last_price = trade.price
        
        await self.event_bus.publish('binance_futures_trade', trade)
        await self.event_bus.publish('binance_futures_price', trade.price)
    
    async def _handle_liquidation(self, data: dict):
        """Handle liquidation event"""
        order = data.get('o', {})
        liq_data = {
            'timestamp': int(time.time() * 1000),
            'side': order.get('S'),  # BUY or SELL
            'price': float(order.get('p', 0)),
            'quantity': float(order.get('q', 0)),
            'value': float(order.get('p', 0)) * float(order.get('q', 0))
        }
        
        self.logger.info(f"LIQUIDATION: {liq_data['side']} ${liq_data['value']:,.0f}")
        await self.event_bus.publish('binance_liquidation', liq_data)
    
    async def _poll_funding(self):
        """Poll funding rate periodically"""
        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    # Get funding rate
                    async with session.get(
                        "https://fapi.binance.com/fapi/v1/fundingRate",
                        params={"symbol": "BTCUSDT", "limit": 1}
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data:
                                self.funding_rate = float(data[0]['fundingRate'])
                                await self.event_bus.publish('funding_rate', self.funding_rate)
                    
                    # Get open interest
                    async with session.get(
                        "https://fapi.binance.com/fapi/v1/openInterest",
                        params={"symbol": "BTCUSDT"}
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            self.open_interest = float(data['openInterest'])
                            await self.event_bus.publish('open_interest', self.open_interest)
                            
            except Exception as e:
                self.logger.error(f"Funding poll error: {e}")
            
            await asyncio.sleep(60)  # Poll every minute


class ChainlinkCollector(BaseWebSocketCollector):
    """Collects Chainlink oracle prices from Polymarket RTDS"""
    
    def __init__(self, event_bus: EventBus):
        super().__init__(event_bus, "Chainlink")
        self.last_price: Optional[float] = None
        self.last_timestamp: int = 0
    
    @property
    def ws_url(self) -> str:
        return "wss://ws-live-data.polymarket.com"
    
    async def on_connect(self, ws):
        subscribe_msg = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": "{\"symbol\":\"btc/usd\"}"
                },
                {
                    "topic": "crypto_prices",
                    "type": "*",
                    "filters": "{\"symbol\":\"btcusdt\"}"
                }
            ]
        }
        await ws.send(json.dumps(subscribe_msg))
        self.logger.info("Subscribed to Chainlink and Binance relay")
    
    async def on_message(self, data: Any):
        if not isinstance(data, dict):
            return
        
        topic = data.get('topic')
        msg_type = data.get('type')
        
        if msg_type != 'update':
            return
        
        payload = data.get('payload', {})
        
        if topic == 'crypto_prices_chainlink':
            price = float(payload.get('value', 0))
            if price > 0:
                self.last_price = price
                self.last_timestamp = int(time.time() * 1000)
                await self.event_bus.publish('chainlink_price', price)
        
        elif topic == 'crypto_prices':
            price = float(payload.get('value', 0))
            if price > 0:
                await self.event_bus.publish('polymarket_binance_relay', price)


class PolymarketOrderbookCollector(BaseWebSocketCollector):
    """Collects Polymarket CLOB orderbook data"""
    
    def __init__(self, event_bus: EventBus):
        super().__init__(event_bus, "PolymarketCLOB")
        self.up_token_id: Optional[str] = None
        self.down_token_id: Optional[str] = None
        self.up_orderbook = OrderBook()
        self.down_orderbook = OrderBook()
        self._subscribed = False
    
    @property
    def ws_url(self) -> str:
        return "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    def set_tokens(self, up_token_id: str, down_token_id: str):
        """Set token IDs and trigger resubscription"""
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
        self._subscribed = False
        
        # Resubscribe if connected
        if self._ws:
            asyncio.create_task(self._subscribe())
    
    async def on_connect(self, ws):
        if self.up_token_id and self.down_token_id:
            await self._subscribe()
    
    async def _subscribe(self):
        """Subscribe to current token pair"""
        if not self._ws or not self.up_token_id or not self.down_token_id:
            return
        
        subscribe_msg = {
            "type": "market",
            "assets_ids": [self.up_token_id, self.down_token_id]
        }
        await self._ws.send(json.dumps(subscribe_msg))
        self._subscribed = True
        self.logger.info(f"Subscribed to tokens: UP={self.up_token_id[:20]}...")
    
    async def on_message(self, data: Any):
        # Handle list messages
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._process_message(item)
        elif isinstance(data, dict):
            await self._process_message(data)
    
    async def _process_message(self, data: dict):
        """Process a single orderbook message"""
        event_type = data.get('event_type')
        asset_id = data.get('asset_id')
        
        if not asset_id:
            return
        
        # Determine side
        if asset_id == self.up_token_id:
            side = Side.UP
            orderbook = self.up_orderbook
        elif asset_id == self.down_token_id:
            side = Side.DOWN
            orderbook = self.down_orderbook
        else:
            return
        
        if event_type == 'book':
            # Full book snapshot
            orderbook.bids = [
                OrderBookLevel(float(b['price']), float(b['size']))
                for b in data.get('bids', [])
            ]
            orderbook.asks = [
                OrderBookLevel(float(a['price']), float(a['size']))
                for a in data.get('asks', [])
            ]
            
            # Sort
            orderbook.bids.sort(key=lambda x: x.price, reverse=True)
            orderbook.asks.sort(key=lambda x: x.price)
            orderbook.timestamp = int(time.time() * 1000)
            
            await self.event_bus.publish(f'polymarket_orderbook_{side.value}', orderbook)
        
        elif event_type in ['price_change', 'last_trade_price']:
            price = float(data.get('price', 0))
            if price > 0:
                await self.event_bus.publish(f'polymarket_price_{side.value}', price)


# =============================================================================
# SECTION 4: MARKET STATE MANAGER
# =============================================================================

class MarketStateManager:
    """Manages Polymarket market state and metadata"""
    
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.logger = logging.getLogger("MarketState")
        
        self.current_market: Optional[MarketState] = None
        self.strike_captured = False
        self._pending_chainlink: Optional[float] = None
        
        # Subscribe to events
        event_bus.subscribe('chainlink_price', self._on_chainlink_price)
    
    def _get_current_window(self) -> tuple:
        """Get current window timestamps"""
        now = int(time.time())
        window_start = (now // 900) * 900
        window_end = window_start + 900
        slug = f"btc-updown-15m-{window_start}"
        return slug, window_start, window_end
    
    async def check_window(self) -> bool:
        """Check for new window, returns True if new window detected"""
        slug, window_start, window_end = self._get_current_window()
        
        if self.current_market and self.current_market.slug == slug:
            return False
        
        # New window detected
        self.logger.info(f"NEW WINDOW: {slug}")
        
        self.current_market = MarketState(
            slug=slug,
            window_start=window_start,
            window_end=window_end
        )
        self.strike_captured = False
        
        # Capture strike from pending Chainlink price
        if self._pending_chainlink:
            self.current_market.strike_price = self._pending_chainlink
            self.strike_captured = True
            self.logger.info(f"STRIKE CAPTURED: ${self._pending_chainlink:,.2f}")
        
        await self.event_bus.publish('new_window', self.current_market)
        
        # Fetch metadata after delay
        asyncio.create_task(self._fetch_metadata_delayed())
        
        return True
    
    def _on_chainlink_price(self, price: float):
        """Handle Chainlink price update"""
        self._pending_chainlink = price
        
        # Capture strike if new window and not yet captured
        if self.current_market and not self.strike_captured:
            self.current_market.strike_price = price
            self.strike_captured = True
            self.logger.info(f"STRIKE CAPTURED: ${price:,.2f}")
    
    async def _fetch_metadata_delayed(self):
        """Fetch market metadata with retry"""
        if not self.current_market:
            return
        
        slug = self.current_market.slug
        
        # Wait for market to appear
        await asyncio.sleep(35)
        
        for attempt in range(5):
            try:
                async with aiohttp.ClientSession() as session:
                    url = "https://gamma-api.polymarket.com/events"
                    params = {'slug': slug}
                    
                    async with session.get(url, params=params, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            
                            if data and len(data) > 0:
                                await self._parse_metadata(data[0])
                                return
                
                self.logger.info(f"Metadata not found, retry {attempt+1}/5")
                await asyncio.sleep(10)
                
            except Exception as e:
                self.logger.error(f"Metadata fetch error: {e}")
                await asyncio.sleep(10)
    
    async def _parse_metadata(self, data: dict):
        """Parse market metadata"""
        if not self.current_market:
            return
        
        markets = data.get('markets', [])
        if not markets:
            return
        
        market = markets[0]
        
        # Parse token IDs
        token_ids = json.loads(market.get('clobTokenIds', '[]'))
        outcomes = json.loads(market.get('outcomes', '[]'))
        
        if len(token_ids) < 2 or len(outcomes) < 2:
            return
        
        # Map to UP/DOWN
        if outcomes[0].upper() in ['UP', 'YES']:
            self.current_market.up_token_id = token_ids[0]
            self.current_market.down_token_id = token_ids[1]
        else:
            self.current_market.up_token_id = token_ids[1]
            self.current_market.down_token_id = token_ids[0]
        
        self.current_market.condition_id = market.get('conditionId')
        self.current_market.question = market.get('question')
        
        self.logger.info(f"Metadata loaded: UP={self.current_market.up_token_id[:20]}...")
        
        await self.event_bus.publish('metadata_loaded', self.current_market)


# =============================================================================
# SECTION 5: VOLATILITY ENGINE
# =============================================================================

class VolatilityEngine:
    """Calculates realized and expected volatility"""
    
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.logger = logging.getLogger("Volatility")
        
        # Price history for vol calculation
        self.price_history: deque = deque(maxlen=10000)  # ~2.7 hours at 1/sec
        self.returns_1m: deque = deque(maxlen=60)
        self.returns_5m: deque = deque(maxlen=12)
        self.returns_15m: deque = deque(maxlen=96)  # 24 hours of 15m periods
        
        # Calculated volatilities
        self.realized_vol_1m: float = 0.0015  # 0.15% default
        self.realized_vol_5m: float = 0.0015
        self.realized_vol_15m: float = 0.0015
        
        # Current vol estimate
        self.current_vol_15m: float = 0.0015
        
        # Tracking
        self.last_price: Optional[float] = None
        self.last_1m_close: Optional[float] = None
        self.last_5m_close: Optional[float] = None
        self.last_15m_close: Optional[float] = None
        
        # Subscribe to price updates
        event_bus.subscribe('binance_spot_price', self._on_price)
    
    def _on_price(self, price: float):
        """Handle price update"""
        now = int(time.time())
        
        self.price_history.append((now, price))
        
        if self.last_price:
            # Calculate return
            ret = (price - self.last_price) / self.last_price
            
            # Update 1m returns every second (simplified)
            self.returns_1m.append(ret)
        
        self.last_price = price
        
        # Check for period closes
        self._check_period_close(now, price)
        
        # Recalculate volatility
        self._update_volatility()
    
    def _check_period_close(self, now: int, price: float):
        """Check and record period closes"""
        # 1-minute close
        if now % 60 == 0:
            if self.last_1m_close:
                ret = math.log(price / self.last_1m_close)
                self.returns_1m.append(ret)
            self.last_1m_close = price
        
        # 5-minute close
        if now % 300 == 0:
            if self.last_5m_close:
                ret = math.log(price / self.last_5m_close)
                self.returns_5m.append(ret)
            self.last_5m_close = price
        
        # 15-minute close
        if now % 900 == 0:
            if self.last_15m_close:
                ret = math.log(price / self.last_15m_close)
                self.returns_15m.append(ret)
            self.last_15m_close = price
    
    def _update_volatility(self):
        """Recalculate volatility estimates"""
        # 1m realized vol (scaled to 15m)
        if len(self.returns_1m) >= 5:
            std_1m = self._std(list(self.returns_1m))
            self.realized_vol_1m = std_1m * math.sqrt(15)  # Scale to 15m
        
        # 5m realized vol (scaled to 15m)
        if len(self.returns_5m) >= 3:
            std_5m = self._std(list(self.returns_5m))
            self.realized_vol_5m = std_5m * math.sqrt(3)  # Scale to 15m
        
        # 15m realized vol
        if len(self.returns_15m) >= 4:
            self.realized_vol_15m = self._std(list(self.returns_15m))
        
        # Weighted average with more weight on recent
        # Recent 1m vol is most reactive, 15m is most stable
        self.current_vol_15m = (
            0.4 * self.realized_vol_1m +
            0.35 * self.realized_vol_5m +
            0.25 * self.realized_vol_15m
        )
        
        # Floor at 0.05% (very low vol) and cap at 2% (extreme)
        self.current_vol_15m = max(0.0005, min(0.02, self.current_vol_15m))
    
    def _std(self, values: List[float]) -> float:
        """Calculate standard deviation"""
        if len(values) < 2:
            return 0.0015
        
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
        return math.sqrt(variance)
    
    def get_vol_for_time(self, seconds_remaining: float) -> float:
        """Get volatility scaled for remaining time"""
        # Scale vol to remaining time
        # If 15m vol is 0.15%, and we have 5 min left, vol is 0.15% * sqrt(5/15) = 0.087%
        time_fraction = seconds_remaining / 900
        return self.current_vol_15m * math.sqrt(time_fraction)
    
    def get_vol_regime(self) -> str:
        """Get current volatility regime"""
        if self.current_vol_15m < 0.001:
            return "LOW"
        elif self.current_vol_15m < 0.002:
            return "NORMAL"
        elif self.current_vol_15m < 0.004:
            return "HIGH"
        else:
            return "EXTREME"


# =============================================================================
# SECTION 6: DRIFT ENGINE (ORDER FLOW)
# =============================================================================

class DriftEngine:
    """Estimates price drift from order flow analysis"""
    
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.logger = logging.getLogger("Drift")
        
        # CVD tracking
        self.spot_cvd: float = 0.0
        self.futures_cvd: float = 0.0
        self.cvd_history: deque = deque(maxlen=300)  # 5 min at 1/sec
        
        # Book imbalance
        self.spot_book_imbalance: float = 0.0
        self.futures_book_imbalance: float = 0.0
        
        # Trade intensity
        self.recent_trades: deque = deque(maxlen=100)
        self.trade_intensity: float = 0.0  # trades per second
        
        # Funding/basis
        self.funding_rate: float = 0.0
        self.futures_basis: float = 0.0
        
        # Liquidations
        self.recent_liquidations: deque = deque(maxlen=20)
        self.liq_pressure: float = 0.0  # positive = long liqs, negative = short liqs
        
        # Final drift estimate
        self.drift_estimate: float = 0.0  # in bps
        
        # Subscribe to events
        event_bus.subscribe('binance_spot_trade', self._on_spot_trade)
        event_bus.subscribe('binance_futures_trade', self._on_futures_trade)
        event_bus.subscribe('binance_spot_orderbook', self._on_spot_book)
        event_bus.subscribe('binance_liquidation', self._on_liquidation)
        event_bus.subscribe('funding_rate', self._on_funding)
    
    def _on_spot_trade(self, trade: Trade):
        """Handle spot trade"""
        now = int(time.time())
        
        # Update CVD
        delta = trade.quantity * trade.price
        if trade.side == "BUY":
            self.spot_cvd += delta
        else:
            self.spot_cvd -= delta
        
        self.cvd_history.append((now, self.spot_cvd))
        self.recent_trades.append(now)
        
        # Update trade intensity
        cutoff = now - 60
        self.recent_trades = deque(
            [t for t in self.recent_trades if t > cutoff],
            maxlen=100
        )
        self.trade_intensity = len(self.recent_trades) / 60
        
        self._update_drift()
    
    def _on_futures_trade(self, trade: Trade):
        """Handle futures trade"""
        delta = trade.quantity * trade.price
        if trade.side == "BUY":
            self.futures_cvd += delta
        else:
            self.futures_cvd -= delta
        
        self._update_drift()
    
    def _on_spot_book(self, orderbook: OrderBook):
        """Handle orderbook update"""
        self.spot_book_imbalance = orderbook.imbalance(levels=5)
        self._update_drift()
    
    def _on_liquidation(self, liq_data: dict):
        """Handle liquidation event"""
        now = int(time.time())
        
        # Track liquidation pressure
        # SELL side liq = longs getting liquidated = bearish
        # BUY side liq = shorts getting liquidated = bullish
        value = liq_data['value']
        if liq_data['side'] == 'SELL':
            self.liq_pressure -= value
        else:
            self.liq_pressure += value
        
        self.recent_liquidations.append((now, liq_data))
        
        # Decay old liquidations
        cutoff = now - 300  # 5 min lookback
        self.recent_liquidations = deque(
            [(t, l) for t, l in self.recent_liquidations if t > cutoff],
            maxlen=20
        )
        
        self._update_drift()
    
    def _on_funding(self, rate: float):
        """Handle funding rate update"""
        self.funding_rate = rate
        self._update_drift()
    
    def _update_drift(self):
        """Calculate overall drift estimate"""
        # Component weights
        weights = {
            'cvd_spot': 0.25,
            'cvd_futures': 0.15,
            'book_imbalance': 0.20,
            'funding': 0.10,
            'liquidations': 0.30
        }
        
        drift = 0.0
        
        # CVD momentum (look at recent change)
        if len(self.cvd_history) >= 2:
            recent_cvd = self.cvd_history[-1][1]
            old_cvd = self.cvd_history[0][1]
            cvd_change = recent_cvd - old_cvd
            
            # Normalize to -1 to 1 range (rough)
            cvd_signal = max(-1, min(1, cvd_change / 1000000))  # $1M normalizer
            drift += weights['cvd_spot'] * cvd_signal * 50  # Scale to bps
        
        # Book imbalance
        drift += weights['book_imbalance'] * self.spot_book_imbalance * 30
        
        # Funding pressure (high positive funding = bearish for longs)
        funding_signal = -self.funding_rate * 10000  # funding is usually small
        drift += weights['funding'] * max(-1, min(1, funding_signal)) * 20
        
        # Liquidation pressure
        if self.liq_pressure != 0:
            liq_signal = max(-1, min(1, self.liq_pressure / 500000))  # $500k normalizer
            drift += weights['liquidations'] * liq_signal * 50
        
        self.drift_estimate = drift
    
    def get_drift_for_time(self, seconds_remaining: float) -> float:
        """Get drift scaled for remaining time"""
        # Drift decays with time (momentum fades)
        time_factor = min(1.0, seconds_remaining / 300)  # Full effect for 5+ min
        return self.drift_estimate * time_factor
    
    def get_signal_strength(self) -> float:
        """Get overall signal strength (0 to 1)"""
        signals = [
            abs(self.spot_book_imbalance),
            min(1, abs(self.drift_estimate) / 50),
            min(1, self.trade_intensity / 10)  # Normalize around 10 trades/sec
        ]
        return sum(signals) / len(signals)


# =============================================================================
# SECTION 7: PROBABILITY ENGINE
# =============================================================================

class ProbabilityEngine:
    """Calculates fair value probabilities"""
    
    def __init__(
        self,
        event_bus: EventBus,
        volatility_engine: VolatilityEngine,
        drift_engine: DriftEngine,
        market_manager: MarketStateManager
    ):
        self.event_bus = event_bus
        self.volatility_engine = volatility_engine
        self.drift_engine = drift_engine
        self.market_manager = market_manager
        self.logger = logging.getLogger("Probability")
        
        # Current price state
        self.price_state = PriceState()
        
        # Last calculated fair value
        self.last_fair_value: Optional[FairValue] = None
        
        # Subscribe to price events
        event_bus.subscribe('binance_spot_price', self._on_binance_price)
        event_bus.subscribe('chainlink_price', self._on_chainlink_price)
        event_bus.subscribe('binance_futures_price', self._on_futures_price)
    
    def _on_binance_price(self, price: float):
        self.price_state.binance_spot = price
        self.price_state.binance_spot_timestamp = int(time.time() * 1000)
    
    def _on_chainlink_price(self, price: float):
        self.price_state.chainlink_oracle = price
        self.price_state.chainlink_timestamp = int(time.time() * 1000)
    
    def _on_futures_price(self, price: float):
        self.price_state.binance_futures = price
        self.price_state.binance_futures_timestamp = int(time.time() * 1000)
    
    def calculate_fair_value(self) -> Optional[FairValue]:
        """Calculate current fair value"""
        market = self.market_manager.current_market
        
        if not market or not market.strike_price:
            return None
        
        if not self.price_state.binance_spot:
            return None
        
        # Get current price (use Binance as leading indicator)
        current_price = self.price_state.binance_spot
        strike = market.strike_price
        time_remaining = market.time_remaining
        
        if time_remaining <= 0:
            return None
        
        # Get volatility for remaining time
        vol = self.volatility_engine.get_vol_for_time(time_remaining)
        
        # Get drift estimate
        drift_bps = self.drift_engine.get_drift_for_time(time_remaining)
        drift = drift_bps / 10000  # Convert to decimal
        
        # Calculate distance to strike
        distance = current_price - strike
        distance_pct = distance / strike
        
        # Calculate probability using log-normal model
        # P(S_T > K) = N(d) where d = [ln(S/K) + (μ - σ²/2)T] / (σ√T)
        
        if vol > 0:
            # Time in periods (we're already using 15m vol, time_remaining in seconds)
            T = time_remaining / 900  # Fraction of 15m period remaining
            
            # Adjusted volatility for remaining time
            vol_T = vol  # Already scaled in get_vol_for_time
            
            if vol_T > 0:
                d = (math.log(current_price / strike) + (drift - 0.5 * vol_T ** 2) * T) / (vol_T * math.sqrt(T))
            else:
                d = 10 if current_price > strike else -10
            
            # Calculate probability using normal CDF approximation
            prob_up = self._normal_cdf(d)
        else:
            # Zero vol - pure delta
            prob_up = 1.0 if current_price > strike else 0.0
        
        # Clamp probabilities
        prob_up = max(0.001, min(0.999, prob_up))
        prob_down = 1.0 - prob_up
        
        # Calculate standardized distance
        distance_in_sigma = distance_pct / vol if vol > 0 else 0
        
        # Confidence based on signal strength and time
        signal_strength = self.drift_engine.get_signal_strength()
        time_confidence = min(1.0, time_remaining / 600)  # Full confidence with 10+ min
        vol_regime = self.volatility_engine.get_vol_regime()
        vol_confidence = 1.0 if vol_regime in ["NORMAL", "LOW"] else 0.7
        
        confidence = (signal_strength + time_confidence + vol_confidence) / 3
        
        # Build fair value object
        fv = FairValue(
            timestamp=int(time.time()),
            prob_up=prob_up,
            prob_down=prob_down,
            confidence=confidence,
            distance_to_strike=distance,
            distance_in_sigma=distance_in_sigma,
            volatility_15m=self.volatility_engine.current_vol_15m,
            drift_estimate=drift_bps,
            time_remaining=time_remaining
        )
        
        # Add market prices if available
        if market.up_orderbook.best_ask:
            fv.up_market_price = market.up_orderbook.best_ask
            fv.up_edge = prob_up - fv.up_market_price
        
        if market.down_orderbook.best_ask:
            fv.down_market_price = market.down_orderbook.best_ask
            fv.down_edge = prob_down - fv.down_market_price
        
        self.last_fair_value = fv
        return fv
    
    def _normal_cdf(self, x: float) -> float:
        """Approximation of standard normal CDF"""
        # Abramowitz and Stegun approximation
        a1 = 0.254829592
        a2 = -0.284496736
        a3 = 1.421413741
        a4 = -1.453152027
        a5 = 1.061405429
        p = 0.3275911
        
        sign = 1 if x >= 0 else -1
        x = abs(x)
        
        t = 1.0 / (1.0 + p * x)
        y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2)
        
        return 0.5 * (1.0 + sign * y)


# =============================================================================
# SECTION 8: SIGNAL GENERATOR
# =============================================================================

class SignalGenerator:
    """Generates trading signals based on fair value and edge"""
    
    def __init__(
        self,
        event_bus: EventBus,
        probability_engine: ProbabilityEngine,
        market_manager: MarketStateManager,
        min_edge: float = 0.03,  # 3% minimum edge
        min_confidence: float = 0.4,
        max_kelly: float = 0.25  # Max 25% of bankroll
    ):
        self.event_bus = event_bus
        self.probability_engine = probability_engine
        self.market_manager = market_manager
        self.logger = logging.getLogger("Signal")
        
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.max_kelly = max_kelly
        
        # Signal history
        self.signals: List[Signal] = []
        
        # Cooldown to avoid rapid fire signals
        self.last_signal_time: int = 0
        self.signal_cooldown: int = 10  # seconds
    
    def check_for_signal(self) -> Optional[Signal]:
        """Check if conditions warrant a trading signal"""
        now = int(time.time())
        
        # Cooldown check
        if now - self.last_signal_time < self.signal_cooldown:
            return None
        
        # Get fair value
        fv = self.probability_engine.calculate_fair_value()
        if not fv:
            return None
        
        # Get market state
        market = self.market_manager.current_market
        if not market or not market.strike_price:
            return None
        
        # Check confidence threshold
        if fv.confidence < self.min_confidence:
            return None
        
        # Check for edge
        signal = None
        
        # Check UP edge
        if fv.up_edge and fv.up_edge > self.min_edge:
            if market.up_orderbook.best_ask:
                signal = self._create_signal(
                    side=Side.UP,
                    action="BUY",
                    entry_price=market.up_orderbook.best_ask,
                    fair_value=fv.prob_up,
                    edge=fv.up_edge,
                    fv=fv,
                    market=market
                )
        
        # Check DOWN edge
        if fv.down_edge and fv.down_edge > self.min_edge:
            if market.down_orderbook.best_ask:
                # Only if no UP signal or DOWN edge is bigger
                if not signal or fv.down_edge > fv.up_edge:
                    signal = self._create_signal(
                        side=Side.DOWN,
                        action="BUY",
                        entry_price=market.down_orderbook.best_ask,
                        fair_value=fv.prob_down,
                        edge=fv.down_edge,
                        fv=fv,
                        market=market
                    )
        
        # Check for selling overpriced shares (negative edge on opposite side)
        if fv.up_edge and fv.up_edge < -self.min_edge:
            if market.up_orderbook.best_bid:
                sell_signal = self._create_signal(
                    side=Side.UP,
                    action="SELL",
                    entry_price=market.up_orderbook.best_bid,
                    fair_value=fv.prob_up,
                    edge=-fv.up_edge,  # Positive edge when selling overpriced
                    fv=fv,
                    market=market
                )
                if not signal or sell_signal.edge > signal.edge:
                    signal = sell_signal
        
        if signal:
            self.signals.append(signal)
            self.last_signal_time = now
            self.logger.info(str(signal))
            asyncio.create_task(self.event_bus.publish('signal', signal))
        
        return signal
    
    def _create_signal(
        self,
        side: Side,
        action: str,
        entry_price: float,
        fair_value: float,
        edge: float,
        fv: FairValue,
        market: MarketState
    ) -> Signal:
        """Create a trading signal"""
        # Calculate Kelly fraction
        # Kelly = (p * b - q) / b where b = odds, p = win prob, q = lose prob
        # For binary options: b = (1 - entry_price) / entry_price
        
        if entry_price > 0 and entry_price < 1:
            b = (1 - entry_price) / entry_price
            p = fair_value
            q = 1 - p
            
            kelly = (p * b - q) / b if b > 0 else 0
            kelly = max(0, min(self.max_kelly, kelly))
        else:
            kelly = 0
        
        # Adjust Kelly by confidence
        kelly *= fv.confidence
        
        return Signal(
            timestamp=int(time.time()),
            side=side,
            action=action,
            entry_price=entry_price,
            fair_value=fair_value,
            edge=edge,
            confidence=fv.confidence,
            strike_price=market.strike_price,
            current_price=self.probability_engine.price_state.binance_spot or 0,
            time_remaining=fv.time_remaining,
            volatility=fv.volatility_15m,
            kelly_fraction=kelly,
            suggested_size_pct=kelly
        )


# =============================================================================
# SECTION 9: DATA LOGGER
# =============================================================================

class DataLogger:
    """Logs all system data to CSV files"""
    
    def __init__(
        self,
        event_bus: EventBus,
        price_state: PriceState,
        volatility_engine: VolatilityEngine,
        drift_engine: DriftEngine,
        probability_engine: ProbabilityEngine,
        market_manager: MarketStateManager,
        output_dir: str = "."
    ):
        self.event_bus = event_bus
        self.price_state = price_state
        self.volatility_engine = volatility_engine
        self.drift_engine = drift_engine
        self.probability_engine = probability_engine
        self.market_manager = market_manager
        self.output_dir = output_dir
        self.logger = logging.getLogger("DataLogger")
        
        # Initialize CSV files
        self._init_csvs()
        
        # Subscribe to signals
        event_bus.subscribe('signal', self._log_signal)
    
    def _init_csvs(self):
        """Initialize CSV files with headers"""
        # Main data log
        self.data_file = f"{self.output_dir}/fair_value_data.csv"
        with open(self.data_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'datetime', 'slug', 'window_start', 'time_remaining',
                'strike_price', 'binance_spot', 'chainlink_oracle', 'oracle_lag',
                'vol_15m', 'vol_regime', 'drift_bps', 'signal_strength',
                'prob_up', 'prob_down', 'up_market', 'down_market',
                'up_edge', 'down_edge', 'confidence'
            ])
        
        # Signal log
        self.signal_file = f"{self.output_dir}/signals.csv"
        with open(self.signal_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'datetime', 'slug', 'side', 'action',
                'entry_price', 'fair_value', 'edge', 'confidence',
                'strike', 'current_price', 'time_remaining', 'volatility',
                'kelly', 'suggested_size'
            ])
    
    def log_state(self):
        """Log current system state"""
        now = int(time.time())
        market = self.market_manager.current_market
        fv = self.probability_engine.last_fair_value
        
        with open(self.data_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                now,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                market.slug if market else '',
                market.window_start if market else 0,
                market.time_remaining if market else 0,
                market.strike_price if market else 0,
                self.price_state.binance_spot or 0,
                self.price_state.chainlink_oracle or 0,
                self.price_state.oracle_lag or 0,
                self.volatility_engine.current_vol_15m,
                self.volatility_engine.get_vol_regime(),
                self.drift_engine.drift_estimate,
                self.drift_engine.get_signal_strength(),
                fv.prob_up if fv else 0,
                fv.prob_down if fv else 0,
                fv.up_market_price if fv else 0,
                fv.down_market_price if fv else 0,
                fv.up_edge if fv else 0,
                fv.down_edge if fv else 0,
                fv.confidence if fv else 0
            ])
    
    def _log_signal(self, signal: Signal):
        """Log trading signal"""
        market = self.market_manager.current_market
        
        with open(self.signal_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                signal.timestamp,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                market.slug if market else '',
                signal.side.value,
                signal.action,
                signal.entry_price,
                signal.fair_value,
                signal.edge,
                signal.confidence,
                signal.strike_price,
                signal.current_price,
                signal.time_remaining,
                signal.volatility,
                signal.kelly_fraction,
                signal.suggested_size_pct
            ])


# =============================================================================
# SECTION 10: DISPLAY
# =============================================================================

class ConsoleDisplay:
    """Real-time console display"""
    
    def __init__(
        self,
        market_manager: MarketStateManager,
        price_state: PriceState,
        volatility_engine: VolatilityEngine,
        drift_engine: DriftEngine,
        probability_engine: ProbabilityEngine
    ):
        self.market_manager = market_manager
        self.price_state = price_state
        self.volatility_engine = volatility_engine
        self.drift_engine = drift_engine
        self.probability_engine = probability_engine
    
    def render(self):
        """Render current state to console"""
        market = self.market_manager.current_market
        fv = self.probability_engine.last_fair_value
        
        # Clear screen (cross-platform)
        print("\033[2J\033[H", end="")
        
        print("=" * 80)
        print("           POLYMARKET BTC 15-MIN FAIR VALUE TRADING SYSTEM")
        print("=" * 80)
        print()
        
        # Market Info
        if market:
            time_left = int(market.time_remaining)
            mins = time_left // 60
            secs = time_left % 60
            
            print(f"  MARKET: {market.slug}")
            print(f"  STRIKE: ${market.strike_price:,.2f}" if market.strike_price else "  STRIKE: Pending...")
            print(f"  TIME:   {mins:02d}:{secs:02d} remaining")
            print()
        else:
            print("  MARKET: Waiting for window...")
            print()
        
        # Prices
        print("  ┌─────────────────────────────────────────────────────────┐")
        print("  │                       PRICES                           │")
        print("  ├─────────────────────────────────────────────────────────┤")
        
        binance = self.price_state.binance_spot
        chainlink = self.price_state.chainlink_oracle
        oracle_lag = self.price_state.oracle_lag
        
        print(f"  │  Binance Spot:    ${binance:>12,.2f}" if binance else "  │  Binance Spot:    Waiting...")
        print(f"  │  Chainlink Oracle: ${chainlink:>11,.2f}" if chainlink else "  │  Chainlink Oracle: Waiting...")
        
        if oracle_lag:
            lag_color = "\033[92m" if oracle_lag > 0 else "\033[91m"
            print(f"  │  Oracle Lag:      {lag_color}${oracle_lag:>+12,.2f}\033[0m")
        
        print("  └─────────────────────────────────────────────────────────┘")
        print()
        
        # Volatility & Drift
        print("  ┌───────────────────────────┬─────────────────────────────┐")
        print("  │       VOLATILITY          │          DRIFT              │")
        print("  ├───────────────────────────┼─────────────────────────────┤")
        
        vol = self.volatility_engine.current_vol_15m * 100
        regime = self.volatility_engine.get_vol_regime()
        drift = self.drift_engine.drift_estimate
        strength = self.drift_engine.get_signal_strength()
        
        print(f"  │  15m Vol: {vol:>6.3f}%          │  Drift: {drift:>+6.1f} bps            │")
        print(f"  │  Regime:  {regime:<10s}       │  Signal: {strength:>5.2f}               │")
        
        print("  └───────────────────────────┴─────────────────────────────┘")
        print()
        
        # Fair Value
        if fv:
            print("  ┌─────────────────────────────────────────────────────────┐")
            print("  │                     FAIR VALUE                         │")
            print("  ├─────────────────────────────────────────────────────────┤")
            
            print(f"  │  P(UP):   {fv.prob_up*100:>6.2f}%    Market: {fv.up_market_price*100 if fv.up_market_price else 0:>5.1f}%    Edge: {fv.up_edge*100 if fv.up_edge else 0:>+5.1f}%  │")
            print(f"  │  P(DOWN): {fv.prob_down*100:>6.2f}%    Market: {fv.down_market_price*100 if fv.down_market_price else 0:>5.1f}%    Edge: {fv.down_edge*100 if fv.down_edge else 0:>+5.1f}%  │")
            print(f"  │  Confidence: {fv.confidence*100:>5.1f}%                                     │")
            print(f"  │  Distance: {fv.distance_in_sigma:>+5.2f}σ ({fv.distance_to_strike:>+.2f})                     │")
            
            print("  └─────────────────────────────────────────────────────────┘")
            
            # Recommendation
            if fv.best_edge and abs(fv.best_edge) > 0.03:
                side = fv.recommended_side
                if side:
                    print()
                    print(f"  \033[92m>>> OPPORTUNITY: BUY {side.value} (Edge: {fv.best_edge*100:.1f}%) <<<\033[0m")
        
        print()
        print("  Press Ctrl+C to stop")
        print("=" * 80)


# =============================================================================
# SECTION 11: MAIN TRADING SYSTEM
# =============================================================================

class TradingSystem:
    """Main trading system orchestrator"""
    
    def __init__(self, output_dir: str = "."):
        self.logger = logging.getLogger("TradingSystem")
        self.output_dir = output_dir
        
        # Event bus
        self.event_bus = EventBus()
        
        # Market state manager
        self.market_manager = MarketStateManager(self.event_bus)
        
        # Data collectors
        self.binance_spot = BinanceSpotCollector(self.event_bus)
        self.binance_futures = BinanceFuturesCollector(self.event_bus)
        self.chainlink = ChainlinkCollector(self.event_bus)
        self.polymarket_clob = PolymarketOrderbookCollector(self.event_bus)
        
        # Engines
        self.volatility_engine = VolatilityEngine(self.event_bus)
        self.drift_engine = DriftEngine(self.event_bus)
        
        self.probability_engine = ProbabilityEngine(
            self.event_bus,
            self.volatility_engine,
            self.drift_engine,
            self.market_manager
        )
        
        # Signal generator
        self.signal_generator = SignalGenerator(
            self.event_bus,
            self.probability_engine,
            self.market_manager,
            min_edge=0.03,
            min_confidence=0.4
        )
        
        # Data logger
        self.data_logger = DataLogger(
            self.event_bus,
            self.probability_engine.price_state,
            self.volatility_engine,
            self.drift_engine,
            self.probability_engine,
            self.market_manager,
            self.output_dir
        )
        
        # Display
        self.display = ConsoleDisplay(
            self.market_manager,
            self.probability_engine.price_state,
            self.volatility_engine,
            self.drift_engine,
            self.probability_engine
        )
        
        # Wire up orderbook subscription on metadata load
        self.event_bus.subscribe('metadata_loaded', self._on_metadata_loaded)
        
        # Update orderbooks in market state
        self.event_bus.subscribe('polymarket_orderbook_UP', self._on_up_orderbook)
        self.event_bus.subscribe('polymarket_orderbook_DOWN', self._on_down_orderbook)
    
    async def _on_metadata_loaded(self, market: MarketState):
        """Handle metadata loaded event"""
        if market.up_token_id and market.down_token_id:
            self.polymarket_clob.set_tokens(
                market.up_token_id,
                market.down_token_id
            )
    
    def _on_up_orderbook(self, orderbook: OrderBook):
        """Update UP orderbook in market state"""
        if self.market_manager.current_market:
            self.market_manager.current_market.up_orderbook = orderbook
    
    def _on_down_orderbook(self, orderbook: OrderBook):
        """Update DOWN orderbook in market state"""
        if self.market_manager.current_market:
            self.market_manager.current_market.down_orderbook = orderbook
    
    async def _main_loop(self):
        """Main processing loop"""
        while True:
            try:
                # Check for new window
                await self.market_manager.check_window()
                
                # Check for signals
                self.signal_generator.check_for_signal()
                
                # Log data
                self.data_logger.log_state()
                
                # Update display
                self.display.render()
                
            except Exception as e:
                self.logger.error(f"Main loop error: {e}")
            
            await asyncio.sleep(1)
    
    async def run(self):
        """Start the trading system"""
        self.logger.info("Starting Polymarket Fair Value Trading System")
        
        # Start all tasks
        tasks = [
            asyncio.create_task(self.binance_spot.run()),
            asyncio.create_task(self.binance_futures.run()),
            asyncio.create_task(self.chainlink.run()),
            asyncio.create_task(self.polymarket_clob.run()),
            asyncio.create_task(self._main_loop())
        ]
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            self.logger.info("Shutting down...")
            for task in tasks:
                task.cancel()


# =============================================================================
# SECTION 12: ENTRY POINT
# =============================================================================

async def main():
    """Entry point"""
    system = TradingSystem(output_dir=".")
    await system.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete.")