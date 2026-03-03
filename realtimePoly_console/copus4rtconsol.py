"""
================================================================================
🏛️ POLYMARKET BTC 15-MINUTE PROFESSIONAL TERMINAL v7.0
================================================================================

FEATURES:
- Real-time orderbook visualization (YES/NO)
- Automatic market rotation every 15 minutes
- Arbitrage detection (YES + NO < $1.00)
- Multi-source price feeds with latency tracking
- Strike price extraction and status display
- Sliding window pre-computation for seamless transitions

DATA STREAMS:
1. BINANCE FUTURES:  wss://fstream.binance.com/ws/btcusdt@aggTrade
2. BINANCE SPOT:     wss://stream.binance.com:9443/ws/btcusdt@trade  
3. POLY RELAY:       wss://ws-live-data.polymarket.com (Binance relay)
4. CHAINLINK ORACLE: wss://ws-live-data.polymarket.com (Settlement price)
5. CLOB ORDERBOOK:   wss://ws-subscriptions-clob.polymarket.com/ws/market
"""

import json
import time
import threading
import re
import sys
from datetime import datetime
from typing import Dict, Optional, Tuple, List

import requests
import websocket
from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console
from rich.align import Align
from rich import box
from rich.text import Text

# ============================================================
# ⚙️ CONFIGURATION
# ============================================================
WINDOW_SIZE = 900          # 15 minutes in seconds
REFRESH_RATE = 0.1         # 10 FPS UI refresh
LOOKAHEAD_WINDOWS = 2      # Pre-fetch next N windows

# API Endpoints
EVENT_SLUG_BASE = "https://gamma-api.polymarket.com/events/slug/"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_PRICE_URL = "https://clob.polymarket.com/price"

# Arbitrage thresholds
ARB_THRESHOLD = 0.995      # Flag if YES + NO < this
POLY_FEE = 0.02            # 2% fee on winnings

# ============================================================
# 🧱 DATA CORE - All Price Feeds
# ============================================================
class DataCore:
    """Central repository for all real-time price data"""
    def __init__(self):
        self.lock = threading.Lock()
        
        # Binance Futures (fastest, leading indicator)
        self.futures_price = 0.0
        self.futures_ts = 0
        
        # Binance Spot (what Chainlink tracks)
        self.spot_price = 0.0
        self.spot_ts = 0
        
        # Polymarket Relay (Binance via Poly infrastructure)
        self.poly_relay_price = 0.0
        self.poly_relay_ts = 0
        
        # Chainlink Oracle (THE settlement price)
        self.oracle_price = 0.0
        self.oracle_ts = 0
        
        # Connection status
        self.ws_status = {
            'futures': False,
            'spot': False,
            'poly': False,
            'clob': False
        }

    def update_futures(self, price, ts):
        with self.lock:
            self.futures_price = float(price)
            self.futures_ts = int(ts)
            self.ws_status['futures'] = True

    def update_spot(self, price, ts):
        with self.lock:
            self.spot_price = float(price)
            self.spot_ts = int(ts)
            self.ws_status['spot'] = True

    def update_relay(self, price, ts):
        with self.lock:
            self.poly_relay_price = float(price)
            self.poly_relay_ts = int(ts)

    def update_oracle(self, price, ts):
        with self.lock:
            self.oracle_price = float(price)
            self.oracle_ts = int(ts)
            self.ws_status['poly'] = True

    def get_latencies(self) -> Dict[str, int]:
        """Get latencies for all feeds in ms"""
        now_ms = int(time.time() * 1000)
        with self.lock:
            return {
                'futures': now_ms - self.futures_ts if self.futures_ts else -1,
                'spot': now_ms - self.spot_ts if self.spot_ts else -1,
                'relay': now_ms - self.poly_relay_ts if self.poly_relay_ts else -1,
                'oracle': now_ms - self.oracle_ts if self.oracle_ts else -1,
            }

core = DataCore()

# ============================================================
# 📊 ORDERBOOK STATE
# ============================================================
class OrderbookState:
    """Manages orderbook state for a single outcome (YES or NO)"""
    def __init__(self, name: str, color: str):
        self.name = name
        self.color = color
        self.bids: Dict[str, float] = {}  # price -> size
        self.asks: Dict[str, float] = {}
        self.msg_count = 0
        self.last_update = 0.0

    def update(self, price: str, size: float, side: str):
        """Update a single price level"""
        self.msg_count += 1
        self.last_update = time.time()
        target = self.bids if side == "BUY" else self.asks
        if size == 0:
            target.pop(price, None)
        else:
            target[price] = size

    def set_snapshot(self, bids: list, asks: list):
        """Set full orderbook snapshot"""
        self.bids = {x['price']: float(x['size']) for x in bids}
        self.asks = {x['price']: float(x['size']) for x in asks}
        self.last_update = time.time()

    def get_snapshot(self, depth: int = 15) -> Tuple[list, list]:
        """Get sorted orderbook snapshot"""
        sorted_bids = sorted(self.bids.items(), key=lambda x: float(x[0]), reverse=True)[:depth]
        sorted_asks = sorted(self.asks.items(), key=lambda x: float(x[0]))[:depth]
        return sorted_bids, sorted_asks

    def best_bid(self) -> float:
        if not self.bids:
            return 0.0
        return max(float(p) for p in self.bids.keys())

    def best_ask(self) -> float:
        if not self.asks:
            return 0.0
        return min(float(p) for p in self.asks.keys())

    def spread(self) -> float:
        ba, bb = self.best_ask(), self.best_bid()
        return ba - bb if ba and bb else 0.0

    def total_bid_volume(self) -> float:
        return sum(self.bids.values())

    def total_ask_volume(self) -> float:
        return sum(self.asks.values())

    def clear(self):
        """Clear orderbook for market transition"""
        self.bids.clear()
        self.asks.clear()
        self.msg_count = 0

# Initialize orderbook states
up_book = OrderbookState("UP (YES)", "green")
down_book = OrderbookState("DOWN (NO)", "red")

# ============================================================
# 🧠 SLIDING WINDOW MARKET MANAGER
# ============================================================
class MarketWindowManager:
    """
    Manages BTC 15-minute market lifecycle:
    - Pre-computes upcoming windows
    - Handles automatic market rotation
    - Fetches token IDs and metadata
    """
    def __init__(self, lookahead: int = LOOKAHEAD_WINDOWS):
        self.lookahead = lookahead
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; PolyTerminal/7.0)'
        })
        
        # Cache for pre-computed markets
        self.market_cache: Dict[str, dict] = {}
        
        # Current active market
        self.window_start = 0
        self.slug = "INITIALIZING..."
        self.title = ""
        self.question = ""
        self.end_date: Optional[datetime] = None
        
        # Token IDs
        self.up_token_id: Optional[str] = None
        self.down_token_id: Optional[str] = None
        
        # Prices
        self.up_price = 0.0
        self.down_price = 0.0
        self.up_ask_size = 0.0
        self.down_ask_size = 0.0
        
        # Strike price (from question or oracle snapshot)
        self.strike = 0.0
        
        # Status
        self.status = "Initializing..."
        self.last_fetch = 0.0

    def calculate_slugs(self) -> List[str]:
        """Generate slugs for current + future windows"""
        now = int(time.time())
        current_window = (now // WINDOW_SIZE) * WINDOW_SIZE
        
        slugs = []
        for i in range(self.lookahead + 1):
            future_start = current_window + (i * WINDOW_SIZE)
            slugs.append(f"btc-updown-15m-{future_start}")
        return slugs

    def lifecycle_check(self) -> bool:
        """Check if we need to transition to a new market. Returns True if transitioned."""
        now = int(time.time())
        current_window = (now // WINDOW_SIZE) * WINDOW_SIZE
        
        if current_window != self.window_start:
            # NEW WINDOW - Transition!
            old_slug = self.slug
            self.window_start = current_window
            self.slug = f"btc-updown-15m-{current_window}"
            
            # Clear old data
            self.up_token_id = None
            self.down_token_id = None
            self.up_price = 0.0
            self.down_price = 0.0
            self.title = ""
            self.question = ""
            self.end_date = None
            
            # Snapshot oracle price as strike if available
            if core.oracle_price > 0:
                self.strike = core.oracle_price
            else:
                self.strike = 0.0
            
            # Clear orderbooks
            up_book.clear()
            down_book.clear()
            
            self.status = f"Transitioning to {self.slug}..."
            return True
        
        return False

    def fetch_from_cache_or_api(self):
        """Load market details from cache or fetch from API"""
        if self.up_token_id is not None:
            return  # Already loaded
        
        # Check cache first
        if self.slug in self.market_cache:
            cached = self.market_cache[self.slug]
            self._apply_cached_data(cached)
            return
        
        # Fetch from API
        self._fetch_from_api()

    def _apply_cached_data(self, cached: dict):
        """Apply cached market data"""
        self.up_token_id = cached.get('up_id')
        self.down_token_id = cached.get('down_id')
        self.title = cached.get('title', '')
        self.question = cached.get('question', '')
        
        if cached.get('end_date'):
            try:
                self.end_date = datetime.fromisoformat(
                    cached['end_date'].replace('Z', '+00:00')
                )
            except:
                pass
        
        self._extract_strike_from_question()
        self.status = "Loaded from cache"

    def _fetch_from_api(self):
        """Fetch market details from Polymarket API"""
        try:
            self.status = "Fetching market..."
            url = f"{EVENT_SLUG_BASE}{self.slug}"
            r = self.session.get(url, timeout=10)
            
            if r.status_code != 200:
                self.status = f"API Error: {r.status_code}"
                return
            
            data = r.json()
            if not data or 'markets' not in data:
                self.status = "Market not found (may be creating...)"
                return
            
            market = data['markets'][0]
            
            # Parse token IDs
            raw_ids = market.get('clobTokenIds', '[]')
            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            
            outcomes = market.get('outcomes', '[]')
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            
            if len(token_ids) >= 2 and len(outcomes) >= 2:
                # Determine which is UP and which is DOWN
                if outcomes[0].lower() in ['up', 'yes']:
                    self.up_token_id = token_ids[0]
                    self.down_token_id = token_ids[1]
                else:
                    self.up_token_id = token_ids[1]
                    self.down_token_id = token_ids[0]
            
            self.title = data.get('title', '')
            self.question = market.get('question', '')
            
            if market.get('endDate'):
                try:
                    self.end_date = datetime.fromisoformat(
                        market['endDate'].replace('Z', '+00:00')
                    )
                except:
                    pass
            
            self._extract_strike_from_question()
            
            # Cache for future use
            self.market_cache[self.slug] = {
                'up_id': self.up_token_id,
                'down_id': self.down_token_id,
                'title': self.title,
                'question': self.question,
                'end_date': market.get('endDate'),
            }
            
            self.status = "Ready"
            self.last_fetch = time.time()
            
        except requests.exceptions.Timeout:
            self.status = "API Timeout"
        except Exception as e:
            self.status = f"Error: {str(e)[:30]}"

    def _extract_strike_from_question(self):
        """Extract strike price from question text"""
        if self.question and self.strike == 0:
            match = re.search(r'\$([\d,]+\.?\d*)', self.question)
            if match:
                try:
                    self.strike = float(match.group(1).replace(',', ''))
                except:
                    pass

    def refresh_sliding_window(self):
        """Pre-fetch upcoming windows"""
        target_slugs = self.calculate_slugs()
        
        for slug in target_slugs:
            if slug not in self.market_cache:
                try:
                    url = f"{GAMMA_EVENTS_URL}?slug={slug}"
                    r = self.session.get(url, timeout=5)
                    if r.status_code == 200:
                        data = r.json()
                        if data:
                            event = data[0]
                            market = event['markets'][0]
                            
                            raw_ids = market.get('clobTokenIds', '[]')
                            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
                            
                            outcomes = market.get('outcomes', '[]')
                            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                            
                            if outcomes[0].lower() in ['up', 'yes']:
                                up_id, down_id = token_ids[0], token_ids[1]
                            else:
                                up_id, down_id = token_ids[1], token_ids[0]
                            
                            self.market_cache[slug] = {
                                'up_id': up_id,
                                'down_id': down_id,
                                'title': event.get('title', ''),
                                'question': market.get('question', ''),
                                'end_date': market.get('endDate'),
                            }
                except:
                    pass
        
        # Cleanup old cache entries
        for key in list(self.market_cache.keys()):
            if key not in target_slugs:
                del self.market_cache[key]

    def fetch_share_prices(self):
        """Fetch current share prices from CLOB API"""
        if not self.up_token_id:
            return
        
        try:
            # UP price
            r = self.session.get(
                f"{CLOB_PRICE_URL}?token_id={self.up_token_id}&side=buy",
                timeout=3
            )
            if r.status_code == 200:
                self.up_price = float(r.json().get('price', 0))
            
            # DOWN price
            r = self.session.get(
                f"{CLOB_PRICE_URL}?token_id={self.down_token_id}&side=buy",
                timeout=3
            )
            if r.status_code == 200:
                self.down_price = float(r.json().get('price', 0))
                
        except:
            pass

    def time_remaining(self) -> float:
        """Get seconds remaining in current window"""
        if self.end_date:
            return max(0, (self.end_date.timestamp() - time.time()))
        return max(0, (self.window_start + WINDOW_SIZE) - time.time())

    def time_remaining_str(self) -> str:
        """Get formatted time remaining"""
        t = self.time_remaining()
        mins = int(t // 60)
        secs = int(t % 60)
        return f"{mins:02d}:{secs:02d}"

market = MarketWindowManager()

# ============================================================
# 📡 WEBSOCKET CLIENTS
# ============================================================
class BinanceFuturesClient:
    """Binance USD-M Futures - Fastest price feed"""
    def __init__(self):
        self.url = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
        self.connected = False

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        self.url,
                        on_message=lambda ws, msg: self._on_msg(msg),
                        on_open=lambda ws: setattr(self, 'connected', True),
                        on_close=lambda ws, *args: setattr(self, 'connected', False),
                        on_error=lambda ws, e: None,
                    )
                    ws.run_forever(ping_interval=30)
                except:
                    self.connected = False
                    time.sleep(2)
        threading.Thread(target=run, daemon=True).start()

    def _on_msg(self, msg):
        try:
            d = json.loads(msg)
            core.update_futures(d['p'], d['T'])
        except:
            pass


class BinanceSpotClient:
    """Binance Spot - What Chainlink actually tracks"""
    def __init__(self):
        self.url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        self.connected = False

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        self.url,
                        on_message=lambda ws, msg: self._on_msg(msg),
                        on_open=lambda ws: setattr(self, 'connected', True),
                        on_close=lambda ws, *args: setattr(self, 'connected', False),
                        on_error=lambda ws, e: None,
                    )
                    ws.run_forever(ping_interval=30)
                except:
                    self.connected = False
                    time.sleep(2)
        threading.Thread(target=run, daemon=True).start()

    def _on_msg(self, msg):
        try:
            d = json.loads(msg)
            core.update_spot(d['p'], d['T'])
        except:
            pass


class PolyLiveDataClient:
    """Polymarket Live Data - Relay + Chainlink Oracle"""
    def __init__(self):
        self.url = "wss://ws-live-data.polymarket.com"
        self.connected = False

    def start(self):
        def on_open(ws):
            self.connected = True
            # Subscribe to Binance relay
            ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [{
                    "topic": "crypto_prices",
                    "type": "*",
                    "filters": '{"symbol":"btcusdt"}'
                }]
            }))
            # Subscribe to Chainlink oracle
            ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [{
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": '{"symbol":"btc/usd"}'
                }]
            }))

        def on_msg(ws, msg):
            try:
                d = json.loads(msg)
                if d.get('type') == 'update':
                    topic = d.get('topic')
                    payload = d.get('payload', {})
                    symbol = payload.get('symbol')
                    
                    if topic == 'crypto_prices' and symbol == 'btcusdt':
                        core.update_relay(payload['value'], payload['timestamp'])
                    elif topic == 'crypto_prices_chainlink' and symbol == 'btc/usd':
                        core.update_oracle(payload['value'], payload['timestamp'])
            except:
                pass

        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        self.url,
                        on_open=on_open,
                        on_message=on_msg,
                        on_close=lambda ws, *args: setattr(self, 'connected', False),
                        on_error=lambda ws, e: None,
                    )
                    ws.run_forever(ping_interval=30)
                except:
                    self.connected = False
                    time.sleep(2)

        threading.Thread(target=run, daemon=True).start()


class PolyClobClient:
    """Polymarket CLOB Orderbook WebSocket"""
    def __init__(self):
        self.url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        self.ws = None
        self.subscribed_ids: set = set()
        self.connected = False
        self.lock = threading.Lock()

    def start(self):
        def run():
            while True:
                try:
                    self.ws = websocket.WebSocketApp(
                        self.url,
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_close=lambda ws, *args: self._on_close(),
                        on_error=lambda ws, e: None,
                    )
                    self.ws.run_forever(ping_interval=30)
                except:
                    self.connected = False
                    time.sleep(2)

        threading.Thread(target=run, daemon=True).start()

    def _on_open(self, ws):
        self.connected = True
        core.ws_status['clob'] = True
        self._subscribe_current()

    def _on_close(self):
        self.connected = False
        core.ws_status['clob'] = False
        self.subscribed_ids.clear()

    def _subscribe_current(self):
        """Subscribe to current market's token IDs"""
        with self.lock:
            ids_to_sub = []
            
            if market.up_token_id and market.up_token_id not in self.subscribed_ids:
                ids_to_sub.append(market.up_token_id)
            if market.down_token_id and market.down_token_id not in self.subscribed_ids:
                ids_to_sub.append(market.down_token_id)
            
            if ids_to_sub and self.ws:
                try:
                    msg = {
                        "type": "subscribe",
                        "channel": "market",
                        "assets_ids": ids_to_sub
                    }
                    self.ws.send(json.dumps(msg))
                    self.subscribed_ids.update(ids_to_sub)
                except:
                    pass

    def resubscribe(self):
        """Called when market transitions to new window"""
        with self.lock:
            self.subscribed_ids.clear()
        self._subscribe_current()

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            event_type = data.get('event_type')
            
            if event_type == 'book':
                # Full orderbook snapshot
                asset_id = data.get('asset_id')
                if asset_id == market.up_token_id:
                    up_book.set_snapshot(data.get('bids', []), data.get('asks', []))
                elif asset_id == market.down_token_id:
                    down_book.set_snapshot(data.get('bids', []), data.get('asks', []))
                    
            elif event_type == 'price_change':
                # Incremental update
                for change in data.get('price_changes', []):
                    asset_id = change.get('asset_id')
                    price = change.get('price')
                    size = float(change.get('size', 0))
                    side = change.get('side')
                    
                    if asset_id == market.up_token_id:
                        up_book.update(price, size, side)
                    elif asset_id == market.down_token_id:
                        down_book.update(price, size, side)
                        
        except:
            pass

clob_client = PolyClobClient()

# ============================================================
# 🎨 RICH UI COMPONENTS
# ============================================================
def create_imbalance_bar(bids: list, asks: list) -> Text:
    """Create visual bid/ask imbalance indicator"""
    vol_bid = sum(x[1] for x in bids)
    vol_ask = sum(x[1] for x in asks)
    total = vol_bid + vol_ask if (vol_bid + vol_ask) > 0 else 1
    
    bid_pct = vol_bid / total
    bar_width = 30
    fill = int(bid_pct * bar_width)
    
    bar_color = "green" if bid_pct >= 0.5 else "red"
    bar_str = "█" * fill + "░" * (bar_width - fill)
    
    return Text(
        f"BUY {bid_pct:.0%} [{bar_str}] SELL {1-bid_pct:.0%}",
        style=f"bold {bar_color}"
    )


def render_orderbook_panel(book: OrderbookState) -> Panel:
    """Render single orderbook panel"""
    bids, asks = book.get_snapshot(12)
    
    best_bid = book.best_bid()
    best_ask = book.best_ask()
    spread = book.spread()
    
    # Header with BBO and spread
    header = Text()
    header.append(f"{book.name}\n", style=f"bold {book.color} underline")
    header.append(f"${best_bid:.4f} ", style="bold white")
    header.append("× ", style="dim")
    header.append(f"${best_ask:.4f}", style="bold white")
    
    spread_color = "green" if spread < 0.01 else "yellow" if spread < 0.03 else "red"
    header.append(f"  (spread: {spread:.4f})", style=spread_color)
    
    # Imbalance bar
    imbalance = create_imbalance_bar(bids, asks)
    
    # Orderbook table
    table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
    table.add_column("BID QTY", justify="right", style="green")
    table.add_column("BID", justify="right", style="bold green")
    table.add_column("ASK", justify="left", style="bold red")
    table.add_column("ASK QTY", justify="left", style="red")
    
    max_rows = max(len(bids), len(asks), 10)
    for i in range(min(max_rows, 12)):
        b_qty = f"{bids[i][1]:,.0f}" if i < len(bids) else ""
        b_prc = f"{float(bids[i][0]):.4f}" if i < len(bids) else ""
        a_prc = f"{float(asks[i][0]):.4f}" if i < len(asks) else ""
        a_qty = f"{asks[i][1]:,.0f}" if i < len(asks) else ""
        table.add_row(b_qty, b_prc, a_prc, a_qty)
    
    # Stats footer
    stats = Text()
    stats.append(f"Msgs: {book.msg_count} ", style="dim")
    if book.last_update > 0:
        age = time.time() - book.last_update
        stats.append(f"| Age: {age:.1f}s", style="dim")
    
    # Combine into layout
    content = Layout()
    content.split(
        Layout(Align.center(header), size=2),
        Layout(Align.center(imbalance), size=1),
        Layout(table),
        Layout(Align.center(stats), size=1),
    )
    
    return Panel(content, border_style=book.color)


def render_price_feeds_panel() -> Panel:
    """Render all price feeds with latencies"""
    latencies = core.get_latencies()
    
    table = Table(box=box.SIMPLE, expand=True, show_header=True)
    table.add_column("SOURCE", style="cyan", width=18)
    table.add_column("PRICE", justify="right", style="bold white", width=14)
    table.add_column("LATENCY", justify="right", width=10)
    table.add_column("STATUS", justify="center", width=8)
    
    def lat_style(ms: int) -> str:
        if ms < 0:
            return "dim"
        if ms < 100:
            return "green"
        if ms < 500:
            return "yellow"
        return "red"
    
    def status_dot(connected: bool) -> Text:
        return Text("●", style="green" if connected else "red")
    
    # Futures
    table.add_row(
        "⚡ Binance Futures",
        f"${core.futures_price:,.2f}" if core.futures_price > 0 else "-",
        f"{latencies['futures']}ms" if latencies['futures'] >= 0 else "-",
        status_dot(core.ws_status['futures'])
    )
    
    # Spot
    table.add_row(
        "📊 Binance Spot",
        f"${core.spot_price:,.2f}" if core.spot_price > 0 else "-",
        f"{latencies['spot']}ms" if latencies['spot'] >= 0 else "-",
        status_dot(core.ws_status['spot'])
    )
    
    # Poly Relay
    table.add_row(
        "🔄 Poly Relay",
        f"${core.poly_relay_price:,.2f}" if core.poly_relay_price > 0 else "-",
        f"{latencies['relay']}ms" if latencies['relay'] >= 0 else "-",
        status_dot(core.ws_status['poly'])
    )
    
    # Chainlink Oracle
    oracle_style = "bold green" if latencies['oracle'] < 5000 and latencies['oracle'] >= 0 else "bold red"
    table.add_row(
        Text("🔮 Chainlink Oracle", style=oracle_style),
        Text(f"${core.oracle_price:,.2f}" if core.oracle_price > 0 else "-", style=oracle_style),
        f"{latencies['oracle']}ms" if latencies['oracle'] >= 0 else "-",
        status_dot(core.ws_status['poly'])
    )
    
    # Add analytics below
    analytics = Text()
    if core.futures_price > 0 and core.spot_price > 0:
        basis = core.futures_price - core.spot_price
        analytics.append(f"BASIS (Fut-Spot): ", style="dim")
        analytics.append(f"${basis:+.2f}\n", style="yellow")
    
    if core.oracle_price > 0 and core.spot_price > 0:
        oracle_gap = core.oracle_price - core.spot_price
        gap_style = "green" if abs(oracle_gap) < 10 else "yellow" if abs(oracle_gap) < 50 else "red"
        analytics.append(f"ORACLE GAP: ", style="dim")
        analytics.append(f"${oracle_gap:+.2f}", style=gap_style)
    
    layout = Layout()
    layout.split(
        Layout(table, ratio=3),
        Layout(Align.left(analytics), ratio=1),
    )
    
    return Panel(layout, title="📡 PRICE FEEDS", border_style="cyan")


def render_market_info_panel() -> Panel:
    """Render current market information"""
    time_left = market.time_remaining()
    time_str = market.time_remaining_str()
    timer_style = "bold green" if time_left > 60 else "bold yellow" if time_left > 30 else "bold red blink"
    
    info = Text()
    info.append("🎯 CURRENT MARKET\n", style="bold blue underline")
    info.append(f"Slug: ", style="dim")
    info.append(f"{market.slug}\n", style="white")
    
    if market.title:
        info.append(f"Title: ", style="dim")
        info.append(f"{market.title}\n", style="white")
    
    if market.question:
        info.append(f"Q: ", style="dim")
        info.append(f"{market.question[:60]}{'...' if len(market.question) > 60 else ''}\n", style="italic")
    
    info.append(f"\n⏱️ TIME LEFT: ", style="white")
    info.append(f"{time_str}\n", style=timer_style)
    
    if market.strike > 0:
        info.append(f"⚡ STRIKE: ", style="white")
        info.append(f"${market.strike:,.2f}\n", style="bold cyan")
        
        # Current status
        if core.oracle_price > 0:
            diff = core.oracle_price - market.strike
            if diff > 0:
                info.append(f"📈 STATUS: ", style="white")
                info.append(f"UP +${diff:.2f}", style="bold green")
            else:
                info.append(f"📉 STATUS: ", style="white")
                info.append(f"DOWN ${diff:.2f}", style="bold red")
    
    info.append(f"\n\n💵 SHARE PRICES:\n", style="white")
    info.append(f"   UP:   ", style="dim")
    info.append(f"${market.up_price:.4f}\n" if market.up_price > 0 else "-\n", style="green")
    info.append(f"   DOWN: ", style="dim")
    info.append(f"${market.down_price:.4f}" if market.down_price > 0 else "-", style="red")
    
    info.append(f"\n\n📊 Status: {market.status}", style="dim")
    
    return Panel(info, title="📋 MARKET INFO", border_style="blue")


def check_arbitrage() -> Optional[Panel]:
    """Check for arbitrage opportunity and return alert panel if found"""
    up_ask = up_book.best_ask()
    down_ask = down_book.best_ask()
    
    if up_ask <= 0 or down_ask <= 0:
        return None
    
    total_cost = up_ask + down_ask
    
    if total_cost < ARB_THRESHOLD:
        gross_profit = 1.0 - total_cost
        gross_pct = (gross_profit / total_cost) * 100
        
        # Net after fees (fee on winning side = fee * (1 - cost of winner))
        min_ask = min(up_ask, down_ask)
        fee = POLY_FEE * (1.0 - min_ask)
        net_profit = gross_profit - fee
        net_pct = (net_profit / total_cost) * 100
        
        # Get available size
        _, up_asks = up_book.get_snapshot(1)
        _, down_asks = down_book.get_snapshot(1)
        up_size = up_asks[0][1] if up_asks else 0
        down_size = down_asks[0][1] if down_asks else 0
        max_size = min(up_size, down_size)
        
        content = Text()
        content.append("🚨 ARBITRAGE DETECTED 🚨\n\n", style="bold white")
        content.append(f"BUY UP (YES):   ${up_ask:.4f}  (size: {up_size:,.0f})\n", style="green")
        content.append(f"BUY DOWN (NO):  ${down_ask:.4f}  (size: {down_size:,.0f})\n", style="red")
        content.append(f"\nTOTAL COST:     ${total_cost:.4f}\n", style="bold white")
        content.append(f"GUARANTEED:     $1.0000\n", style="bold cyan")
        content.append(f"\nGROSS PROFIT:   ${gross_profit:.4f} ({gross_pct:.2f}%)\n", style="bold green")
        content.append(f"NET (after 2%): ${net_profit:.4f} ({net_pct:.2f}%)\n", style="bold yellow")
        content.append(f"\nMAX SIZE:       {max_size:,.0f} shares", style="white")
        
        return Panel(
            Align.center(content),
            title="💰 ARBITRAGE OPPORTUNITY 💰",
            border_style="bold green on red",
            box=box.DOUBLE
        )
    
    return None


def render_header() -> Panel:
    """Render header with connection status"""
    header = Text()
    header.append("🏛️ POLYMARKET BTC 15-MIN TERMINAL ", style="bold cyan")
    header.append("v7.0", style="dim")
    header.append(" | ", style="dim")
    header.append(datetime.now().strftime("%H:%M:%S"), style="white")
    
    # Connection indicators
    header.append(" | WS: ", style="dim")
    header.append("F", style="green" if core.ws_status['futures'] else "red")
    header.append("S", style="green" if core.ws_status['spot'] else "red")
    header.append("P", style="green" if core.ws_status['poly'] else "red")
    header.append("C", style="green" if core.ws_status['clob'] else "red")
    
    return Panel(header, box=box.MINIMAL)


def generate_dashboard() -> Layout:
    """Generate complete dashboard layout"""
    # Check for market transition
    if market.lifecycle_check():
        clob_client.resubscribe()
    
    # Ensure we have market data
    market.fetch_from_cache_or_api()
    
    # Resubscribe if we now have token IDs
    if market.up_token_id and market.up_token_id not in clob_client.subscribed_ids:
        clob_client.resubscribe()
    
    layout = Layout()
    
    # Check for arbitrage first
    arb_panel = check_arbitrage()
    
    if arb_panel:
        # Arbitrage mode - show alert prominently
        layout.split(
            Layout(render_header(), size=3),
            Layout(arb_panel, size=12),
            Layout(name="books"),
            Layout(name="bottom", size=12),
        )
    else:
        # Normal mode
        layout.split(
            Layout(render_header(), size=3),
            Layout(name="books"),
            Layout(name="bottom", size=14),
        )
    
    # Orderbooks side by side
    layout["books"].split_row(
        Layout(render_orderbook_panel(up_book)),
        Layout(render_orderbook_panel(down_book)),
    )
    
    # Bottom section - info + prices
    layout["bottom"].split_row(
        Layout(render_market_info_panel(), ratio=1),
        Layout(render_price_feeds_panel(), ratio=1),
    )
    
    return layout

# ============================================================
# 🔄 BACKGROUND TASKS
# ============================================================
def background_updater():
    """Background thread for periodic updates"""
    while True:
        try:
            # Refresh sliding window cache
            market.refresh_sliding_window()
            
            # Fetch share prices from REST API
            market.fetch_share_prices()
            
            time.sleep(5)
        except:
            time.sleep(5)

# ============================================================
# 🚀 MAIN ENTRY POINT
# ============================================================
def main():
    console = Console()
    
    console.print("\n[bold cyan]🏛️ POLYMARKET BTC 15-MIN TERMINAL v7.0[/bold cyan]")
    console.print("[dim]=" * 55 + "[/dim]\n")
    
    # Test API connectivity
    console.print("[yellow]Testing API connectivity...[/yellow]")
    try:
        r = requests.get(f"{GAMMA_EVENTS_URL}?limit=1", timeout=10)
        if r.status_code == 200:
            console.print("[green]✓ Polymarket API reachable[/green]")
        else:
            console.print(f"[red]✗ API returned {r.status_code}[/red]")
    except Exception as e:
        console.print(f"[red]✗ API error: {e}[/red]")
        console.print("[yellow]Continuing anyway...[/yellow]")
    
    # Start WebSocket clients
    console.print("[yellow]Starting WebSocket streams...[/yellow]")
    
    BinanceFuturesClient().start()
    console.print("[green]  ✓ Binance Futures[/green]")
    
    BinanceSpotClient().start()
    console.print("[green]  ✓ Binance Spot[/green]")
    
    PolyLiveDataClient().start()
    console.print("[green]  ✓ Polymarket Live Data[/green]")
    
    clob_client.start()
    console.print("[green]  ✓ Polymarket CLOB[/green]")
    
    # Initial market fetch
    console.print("[yellow]Fetching current market...[/yellow]")
    market.lifecycle_check()
    market.fetch_from_cache_or_api()
    
    if market.up_token_id:
        console.print(f"[green]✓ Market loaded: {market.slug}[/green]")
    else:
        console.print("[yellow]⚠ Market still loading...[/yellow]")
    
    # Pre-fetch upcoming windows
    console.print("[yellow]Pre-fetching upcoming windows...[/yellow]")
    market.refresh_sliding_window()
    console.print(f"[green]✓ Cached {len(market.market_cache)} windows[/green]")
    
    # Start background updater
    threading.Thread(target=background_updater, daemon=True).start()
    
    # Give WebSockets time to connect
    console.print("\n[bold green]Starting terminal...[/bold green]")
    time.sleep(2)
    
    # Main display loop
    try:
        with Live(
            generate_dashboard(),
            refresh_per_second=int(1 / REFRESH_RATE),
            screen=True,
            console=console,
        ) as live:
            while True:
                live.update(generate_dashboard())
                time.sleep(REFRESH_RATE)
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")


if __name__ == "__main__":
    main()