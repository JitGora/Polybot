
#!/usr/bin/env python3
"""
BTC 15-Min Scanner v4.0 - TRIPLE WEBSOCKET FEEDS
✓ Binance WSS - Direct spot price (fastest)
✓ RTDS Binance - Polymarket's Binance feed
✓ RTDS Chainlink - Official oracle (settlement)
✓ All real-time WebSocket connections
"""

import time
import json
import requests
import csv
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, asdict
from collections import deque
import os
import sys
import asyncio
import websockets
import threading

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class MarketState:
    slug: str = ""
    window_start: int = 0
    window_end: int = 0
    strike: float = 0.0
    up_token: str = ""
    down_token: str = ""
    manual_strike: bool = False

    def time_left(self) -> int:
        if self.window_start == 0:
            return 0
        return max(0, self.window_end - int(time.time()))

    def is_active(self) -> bool:
        return self.strike > 0 and self.up_token != ""


@dataclass
class PriceData:
    timestamp: int
    binance_spot: float = 0.0        # Direct Binance WSS (fastest)
    rtds_binance: float = 0.0         # RTDS Binance feed
    rtds_chainlink: float = 0.0       # RTDS Chainlink oracle (official)
    futures: float = 0.0
    up_ask: float = 0.0
    up_bid: float = 0.0
    up_size: float = 0.0
    down_ask: float = 0.0
    down_bid: float = 0.0
    down_size: float = 0.0

    def oracle_lag(self) -> float:
        """Lag between fastest (Binance) and oracle (Chainlink)"""
        return self.binance_spot - self.rtds_chainlink

    def rtds_lag(self) -> float:
        """Lag between RTDS feeds (Binance vs Chainlink)"""
        return self.rtds_binance - self.rtds_chainlink

    def is_complete(self) -> bool:
        return all([
            self.binance_spot > 0,
            self.rtds_chainlink > 0,
            self.up_ask > 0,
            self.down_ask > 0
        ])


@dataclass
class TradeSignal:
    timestamp: int
    window_start: int
    slug: str
    side: str
    entry_price: float
    fair_value: float
    gross_edge: float
    net_edge: float
    oracle_lag: float
    rtds_lag: float
    confidence: float
    strike: float
    binance_spot: float
    rtds_binance: float
    rtds_chainlink: float
    time_left: int
    available_size: float
    spread: float

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================================
# TRIPLE WEBSOCKET PRICE FEEDS
# ============================================================================

class TripleWebSocketFeeds:
    """
    Manages 3 WebSocket connections:
    1. Binance WSS (wss://stream.binance.com:9443)
    2. RTDS Binance (wss://ws-live-data.polymarket.com)
    3. RTDS Chainlink (wss://ws-live-data.polymarket.com)
    """

    BINANCE_WSS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    RTDS_WSS = "wss://ws-live-data.polymarket.com"

    def __init__(self):
        # Price storage
        self.binance_spot = 0.0
        self.rtds_binance = 0.0
        self.rtds_chainlink = 0.0
        self.futures = 0.0

        # Connection status
        self.running = False
        self.binance_connected = False
        self.rtds_connected = False

        # Threads
        self.binance_thread = None
        self.rtds_thread = None

        # History
        self.price_history = deque(maxlen=1000)

    def start(self):
        """Start all WebSocket connections"""
        if not self.running:
            self.running = True

            # Start Binance WSS
            self.binance_thread = threading.Thread(
                target=self._run_binance_ws, 
                daemon=True
            )
            self.binance_thread.start()

            # Start RTDS WSS
            self.rtds_thread = threading.Thread(
                target=self._run_rtds_ws,
                daemon=True
            )
            self.rtds_thread.start()

            print("[WSS] Starting all WebSocket feeds...")

    def stop(self):
        """Stop all connections"""
        self.running = False
        print("[WSS] Stopping all feeds...")

    # ========================================================================
    # BINANCE WEBSOCKET (Direct)
    # ========================================================================

    def _run_binance_ws(self):
        """Run Binance WebSocket in asyncio loop"""
        asyncio.run(self._binance_ws_loop())

    async def _binance_ws_loop(self):
        """Binance WebSocket connection loop"""
        while self.running:
            try:
                async with websockets.connect(self.BINANCE_WSS) as ws:
                    print("[BINANCE-WSS] ✓ Connected")
                    self.binance_connected = True

                    while self.running:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=10.0)
                            data = json.loads(message)

                            # Binance trade format: {"p": "97850.00", "q": "0.001", ...}
                            if 'p' in data:
                                price = float(data['p'])
                                self.binance_spot = price

                                self.price_history.append({
                                    'time': int(time.time()),
                                    'binance_spot': price,
                                    'source': 'binance_wss'
                                })

                        except asyncio.TimeoutError:
                            # Send ping
                            await ws.ping()
                        except Exception as e:
                            print(f"[BINANCE-WSS] Error: {e}")
                            break

            except Exception as e:
                self.binance_connected = False
                print(f"[BINANCE-WSS] Connection lost: {e}")
                if self.running:
                    await asyncio.sleep(5)

    # ========================================================================
    # POLYMARKET RTDS WEBSOCKET (Binance + Chainlink)
    # ========================================================================

    def _run_rtds_ws(self):
        """Run RTDS WebSocket in asyncio loop"""
        asyncio.run(self._rtds_ws_loop())

    async def _rtds_ws_loop(self):
        """RTDS WebSocket connection loop"""
        while self.running:
            try:
                async with websockets.connect(self.RTDS_WSS) as ws:
                    print("[RTDS] ✓ Connected")
                    self.rtds_connected = True

                    # Subscribe to BOTH feeds
                    subscribe_msg = {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "crypto_prices",
                                "type": "*",
                                "filters": "{\"symbol\":\"btc/usd\"}"
                            },
                            {
                                "topic": "crypto_prices_chainlink",
                                "type": "*",
                                "filters": "{\"symbol\":\"btc/usd\"}"
                            }
                        ]
                    }

                    await ws.send(json.dumps(subscribe_msg))
                    print("[RTDS] ✓ Subscribed to Binance + Chainlink feeds")

                    last_ping = time.time()

                    while self.running:
                        # Send ping every 5 seconds
                        if time.time() - last_ping > 5:
                            await ws.send(json.dumps({"action": "ping"}))
                            last_ping = time.time()

                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            data = json.loads(message)

                            # RTDS Binance feed
                            if data.get('topic') == 'crypto_prices':
                                if data.get('type') == 'update':
                                    payload = data.get('payload', {})
                                    if payload.get('symbol') == 'btc/usd':
                                        price = float(payload.get('value', 0))
                                        if price > 0:
                                            self.rtds_binance = price

                                            self.price_history.append({
                                                'time': int(time.time()),
                                                'rtds_binance': price,
                                                'source': 'rtds_binance'
                                            })

                            # RTDS Chainlink oracle
                            elif data.get('topic') == 'crypto_prices_chainlink':
                                if data.get('type') == 'update':
                                    payload = data.get('payload', {})
                                    if payload.get('symbol') == 'btc/usd':
                                        price = float(payload.get('value', 0))
                                        if price > 0:
                                            self.rtds_chainlink = price

                                            self.price_history.append({
                                                'time': int(time.time()),
                                                'rtds_chainlink': price,
                                                'source': 'rtds_chainlink'
                                            })

                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            continue

            except Exception as e:
                self.rtds_connected = False
                print(f"[RTDS] Connection lost: {e}")
                if self.running:
                    await asyncio.sleep(5)

    # ========================================================================
    # HELPER METHODS
    # ========================================================================

    def get_prices(self) -> dict:
        """Get all current prices"""
        # Update futures via REST (futures don't have good WSS)
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT", timeout=2)
            self.futures = float(r.json()['price'])
        except:
            pass

        return {
            'binance_spot': self.binance_spot,
            'rtds_binance': self.rtds_binance,
            'rtds_chainlink': self.rtds_chainlink,
            'futures': self.futures
        }

    def get_strike_price(self) -> float:
        """Strike = RTDS Chainlink at window start"""
        return self.rtds_chainlink if self.rtds_chainlink > 0 else self.binance_spot


# ============================================================================
# MARKET MANAGER
# ============================================================================

class MarketManager:
    GAMMA_API = "https://gamma-api.polymarket.com"
    WINDOW_SIZE = 900

    def __init__(self, feeds: TripleWebSocketFeeds):
        self.state = MarketState()
        self.last_api_call = 0
        self.feeds = feeds

    def get_current_window(self) -> tuple:
        current_time = int(time.time())
        window_start = (current_time // self.WINDOW_SIZE) * self.WINDOW_SIZE
        window_end = window_start + self.WINDOW_SIZE
        return window_start, window_end

    def update(self, manual_strike: Optional[float] = None) -> bool:
        try:
            window_start, window_end = self.get_current_window()

            if window_start != self.state.window_start:
                self.state.window_start = window_start
                self.state.window_end = window_end
                self.state.slug = f"btc-updown-15m-{window_start}"

                if manual_strike:
                    self.state.strike = manual_strike
                    self.state.manual_strike = True
                else:
                    # Strike = RTDS Chainlink price
                    self.state.strike = self.feeds.get_strike_price()
                    self.state.manual_strike = False

                return self.fetch_tokens()

            return self.state.is_active()
        except:
            return False

    def fetch_tokens(self) -> bool:
        try:
            now = time.time()
            if now - self.last_api_call < 5:
                return self.state.up_token != ""
            self.last_api_call = now

            url = f"{self.GAMMA_API}/events"
            r = requests.get(url, params={'slug': self.state.slug}, timeout=5)

            if r.status_code == 200 and r.json():
                data = r.json()[0]
                market = data['markets'][0]

                token_ids = json.loads(market['clobTokenIds'])
                outcomes = json.loads(market['outcomes'])

                if outcomes[0].lower() in ['up', 'yes']:
                    self.state.up_token = token_ids[0]
                    self.state.down_token = token_ids[1]
                else:
                    self.state.up_token = token_ids[1]
                    self.state.down_token = token_ids[0]

                return True
            return False
        except:
            return False


# ============================================================================
# PRICE FEED MANAGER
# ============================================================================

class PriceFeedManager:
    CLOB_API = "https://clob.polymarket.com"

    def __init__(self, feeds: TripleWebSocketFeeds):
        self.prices = PriceData(timestamp=int(time.time()))
        self.feeds = feeds
        self.update_count = 0

    def update_all(self, market: MarketState):
        """Update all price data"""
        self.prices.timestamp = int(time.time())
        self.update_count += 1

        # Get prices from WebSocket feeds
        prices = self.feeds.get_prices()
        self.prices.binance_spot = prices['binance_spot']
        self.prices.rtds_binance = prices['rtds_binance']
        self.prices.rtds_chainlink = prices['rtds_chainlink']
        self.prices.futures = prices['futures']

        # Update orderbook
        if market.is_active():
            self._update_orderbook(market)

    def _update_orderbook(self, market: MarketState):
        """Fetch orderbook from CLOB API"""
        try:
            # UP orderbook
            r = requests.get(f"{self.CLOB_API}/book", 
                           params={'token_id': market.up_token}, timeout=2)
            if r.status_code == 200:
                data = r.json()
                asks = data.get('asks', [])
                if asks:
                    self.prices.up_ask = min(float(a['price']) for a in asks)
                    self.prices.up_size = sum(float(a['size']) for a in asks 
                                            if float(a['price']) == self.prices.up_ask)
                bids = data.get('bids', [])
                if bids:
                    self.prices.up_bid = max(float(b['price']) for b in bids)

            # DOWN orderbook
            r = requests.get(f"{self.CLOB_API}/book", 
                           params={'token_id': market.down_token}, timeout=2)
            if r.status_code == 200:
                data = r.json()
                asks = data.get('asks', [])
                if asks:
                    self.prices.down_ask = min(float(a['price']) for a in asks)
                    self.prices.down_size = sum(float(a['size']) for a in asks 
                                              if float(a['price']) == self.prices.down_ask)
                bids = data.get('bids', [])
                if bids:
                    self.prices.down_bid = max(float(b['price']) for b in bids)
        except:
            pass


# ============================================================================
# SIGNAL GENERATOR
# ============================================================================

class SignalGenerator:
    def __init__(self, min_edge: float = 0.05):
        self.min_edge = min_edge

    def calculate_signal(self, market: MarketState, prices: PriceData) -> Optional[TradeSignal]:
        """Calculate trading signal based on oracle lag"""

        time_left = market.time_left()

        # Only trade first 10 minutes
        if time_left < 600 or time_left > 890:
            return None

        if not prices.is_complete():
            return None

        # Calculate lag (Binance spot vs RTDS Chainlink)
        oracle_lag = prices.oracle_lag()
        rtds_lag = prices.rtds_lag()

        if abs(oracle_lag) < 20:
            return None

        # Project where Chainlink oracle will move
        projected_oracle = prices.rtds_chainlink + (oracle_lag * 0.7)

        # Compare to strike
        if projected_oracle > market.strike:
            distance_pct = (projected_oracle - market.strike) / market.strike
            true_prob_up = 0.50 + min(0.45, distance_pct * 50)

            gross_edge = true_prob_up - prices.up_ask
            fee = true_prob_up * 0.02 * (1 - prices.up_ask)
            net_edge = gross_edge - fee

            if net_edge >= self.min_edge:
                return TradeSignal(
                    timestamp=prices.timestamp,
                    window_start=market.window_start,
                    slug=market.slug,
                    side='UP',
                    entry_price=prices.up_ask,
                    fair_value=true_prob_up,
                    gross_edge=gross_edge,
                    net_edge=net_edge,
                    oracle_lag=oracle_lag,
                    rtds_lag=rtds_lag,
                    confidence=min(1.0, abs(oracle_lag) / 50),
                    strike=market.strike,
                    binance_spot=prices.binance_spot,
                    rtds_binance=prices.rtds_binance,
                    rtds_chainlink=prices.rtds_chainlink,
                    time_left=time_left,
                    available_size=prices.up_size,
                    spread=prices.up_ask - prices.up_bid if prices.up_bid > 0 else 0
                )

        else:
            distance_pct = (market.strike - projected_oracle) / market.strike
            true_prob_down = 0.50 + min(0.45, distance_pct * 50)

            gross_edge = true_prob_down - prices.down_ask
            fee = true_prob_down * 0.02 * (1 - prices.down_ask)
            net_edge = gross_edge - fee

            if net_edge >= self.min_edge:
                return TradeSignal(
                    timestamp=prices.timestamp,
                    window_start=market.window_start,
                    slug=market.slug,
                    side='DOWN',
                    entry_price=prices.down_ask,
                    fair_value=true_prob_down,
                    gross_edge=gross_edge,
                    net_edge=net_edge,
                    oracle_lag=oracle_lag,
                    rtds_lag=rtds_lag,
                    confidence=min(1.0, abs(oracle_lag) / 50),
                    strike=market.strike,
                    binance_spot=prices.binance_spot,
                    rtds_binance=prices.rtds_binance,
                    rtds_chainlink=prices.rtds_chainlink,
                    time_left=time_left,
                    available_size=prices.down_size,
                    spread=prices.down_ask - prices.down_bid if prices.down_bid > 0 else 0
                )

        return None


# ============================================================================
# TRADE LOGGER
# ============================================================================

class TradeLogger:
    def __init__(self, filename: str = "signals_triple_wss.csv"):
        self.filename = filename
        self.signals_logged = 0
        self.signals_this_session = []

        with open(filename, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'timestamp', 'datetime', 'window_start', 'slug', 'side',
                'entry_price', 'fair_value', 'gross_edge', 'net_edge',
                'oracle_lag', 'rtds_lag', 'confidence', 'strike',
                'binance_spot', 'rtds_binance', 'rtds_chainlink',
                'time_left', 'available_size', 'spread'
            ])
            writer.writeheader()

    def log_signal(self, signal: TradeSignal):
        with open(self.filename, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'timestamp', 'datetime', 'window_start', 'slug', 'side',
                'entry_price', 'fair_value', 'gross_edge', 'net_edge',
                'oracle_lag', 'rtds_lag', 'confidence', 'strike',
                'binance_spot', 'rtds_binance', 'rtds_chainlink',
                'time_left', 'available_size', 'spread'
            ])

            row = signal.to_dict()
            row['datetime'] = datetime.fromtimestamp(signal.timestamp).strftime('%Y-%m-%d %H:%M:%S')
            writer.writerow(row)

        self.signals_logged += 1
        self.signals_this_session.append(signal)


# ============================================================================
# LIVE DASHBOARD
# ============================================================================

class LiveDashboard:
    def __init__(self):
        self.start_time = time.time()

    def clear_screen(self):
        os.system('cls' if os.name == 'nt' else 'clear')

    def render(self, market: MarketState, prices: PriceData, feeds: TripleWebSocketFeeds,
               logger: TradeLogger, scanner_status: str):
        """Render live dashboard with 3 price sources"""
        self.clear_screen()

        elapsed = time.time() - self.start_time
        elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"

        print("╔" + "═"*78 + "╗")
        print(f"║ {'BTC 15-MIN TRIPLE WSS SCANNER':^78} ║")
        print("╠" + "═"*78 + "╣")
        print(f"║ Time: {datetime.now().strftime('%H:%M:%S'):>10}  |  Elapsed: {elapsed_str:>8}  |  Signals: {logger.signals_logged:>3}  |  Status: {scanner_status:>12} ║")
        print("╚" + "═"*78 + "╝")

        # WebSocket Status
        print("\n┌─ WEBSOCKET STATUS " + "─"*56 + "┐")
        binance_status = "✓ Connected" if feeds.binance_connected else "✗ Disconnected"
        rtds_status = "✓ Connected" if feeds.rtds_connected else "✗ Disconnected"
        print(f"│ Binance WSS: {binance_status:<20}  │  RTDS: {rtds_status:<25} │")
        print("└" + "─"*78 + "┘")

        # Market Info
        print("\n┌─ MARKET " + "─"*68 + "┐")
        if market.is_active():
            time_left = market.time_left()
            time_left_str = f"{time_left}s ({time_left//60}m {time_left%60}s)"
            print(f"│ Window: {market.slug[:40]:<40}  Time: {time_left_str:>12} │")
            print(f"│ Strike: ${market.strike:>10,.2f} (RTDS Chainlink at window start){'':>18} │")
        else:
            print(f"│ {'⏳ Waiting for market data...':^76} │")
        print("└" + "─"*78 + "┘")

        # Triple Price Feeds
        print("\n┌─ TRIPLE PRICE FEEDS " + "─"*54 + "┐")
        if prices.binance_spot > 0:
            oracle_lag = prices.oracle_lag()
            rtds_lag = prices.rtds_lag()

            print(f"│ 1. Binance WSS:     ${prices.binance_spot:>10,.2f}  (Direct, fastest)        │")
            print(f"│ 2. RTDS Binance:    ${prices.rtds_binance:>10,.2f}  (Polymarket's Binance)   │")
            print(f"│ 3. RTDS Chainlink:  ${prices.rtds_chainlink:>10,.2f}  (Official Oracle) ✓     │")
            print(f"│ Futures:            ${prices.futures:>10,.2f}                             │")
            print(f"│ ───────────────────────────────────────────────────────────────────────  │")
            print(f"│ Oracle Lag:  {oracle_lag:>+7.2f}  (Binance - Chainlink)                   │")
            print(f"│ RTDS Lag:    {rtds_lag:>+7.2f}  (RTDS Binance - Chainlink)               │")

            if prices.up_ask > 0:
                print(f"│ ───────────────────────────────────────────────────────────────────────  │")
                print(f"│ UP Ask:  ${prices.up_ask:>7.4f} ({prices.up_size:>5.0f} shares)                          │")
                print(f"│ DOWN Ask: ${prices.down_ask:>7.4f} ({prices.down_size:>5.0f} shares)                          │")
        else:
            print(f"│ {'⏳ Connecting to WebSocket feeds...':^76} │")
        print("└" + "─"*78 + "┘")

        # Signals
        print("\n┌─ SIGNALS " + "─"*66 + "┐")
        if logger.signals_logged > 0:
            signals = logger.signals_this_session
            avg_edge = sum(s.net_edge for s in signals) / len(signals) * 100
            max_edge = max(s.net_edge for s in signals) * 100

            up_count = sum(1 for s in signals if s.side == 'UP')
            down_count = sum(1 for s in signals if s.side == 'DOWN')

            print(f"│ Total: {logger.signals_logged:>3}  │  UP: {up_count:>2}  DOWN: {down_count:>2}  │  Avg: {avg_edge:>5.2f}%  Max: {max_edge:>5.2f}%  │")

            # Recent signals table
            print("│ ───────────────────────────────────────────────────────────────────────  │")
            print("│ #  │ Time  │ Side │ Entry   │ Edge  │ O-Lag  │ R-Lag  │ Conf │")

            recent = signals[-3:]
            for i, sig in enumerate(recent, 1):
                time_str = datetime.fromtimestamp(sig.timestamp).strftime('%H:%M')
                print(f"│ {logger.signals_logged - len(recent) + i:>2} │ {time_str} │ {sig.side:>4} │ "
                      f"${sig.entry_price:.4f} │ {sig.net_edge*100:>4.1f}% │ {sig.oracle_lag:>+6.2f} │ "
                      f"{sig.rtds_lag:>+6.2f} │ {sig.confidence*100:>3.0f}% │")
        else:
            print(f"│ {'No signals yet. Requirement: Net edge ≥ 5.0%':^76} │")
        print("└" + "─"*78 + "┘")

        print(f"\n💾 Log: {logger.filename}  │  Press Ctrl+C to stop")


# ============================================================================
# MAIN SCANNER
# ============================================================================

class SignalScanner:
    def __init__(self, min_edge: float = 0.05):
        self.feeds = TripleWebSocketFeeds()
        self.market = MarketManager(self.feeds)
        self.prices = PriceFeedManager(self.feeds)
        self.generator = SignalGenerator(min_edge=min_edge)
        self.logger = TradeLogger()
        self.dashboard = LiveDashboard()
        self.running = False
        self.signals_this_window = set()
        self.manual_strike = None

    def prompt_strike(self):
        print("\n" + "="*70)
        print("STRIKE PRICE ENTRY")
        print("="*70)
        print("\nWaiting for WebSocket connections...")
        time.sleep(3)

        prices = self.feeds.get_prices()
        if prices['rtds_chainlink'] > 0:
            print(f"Current RTDS Chainlink: ${prices['rtds_chainlink']:,.2f}")

        choice = input("\nEnter strike (or press Enter for auto): ").strip()

        if choice:
            try:
                strike = float(choice.replace('$', '').replace(',', ''))
                if 50000 < strike < 200000:
                    self.manual_strike = strike
                    print(f"✓ Using manual strike: ${strike:,.2f}")
                    return
            except:
                pass

        print("⏳ Using RTDS Chainlink for strike...")

    def run(self):
        self.running = True

        # Start WebSocket feeds
        self.feeds.start()

        self.prompt_strike()

        status = "Starting..."
        last_update = time.time()

        try:
            while self.running:
                now = time.time()

                if now - last_update >= 1.0:
                    new_window = self.market.update(self.manual_strike)

                    if new_window:
                        self.signals_this_window.clear()
                        status = "New window!"
                        self.manual_strike = None

                    self.prices.update_all(self.market.state)

                    if self.market.state.is_active():
                        time_left = self.market.state.time_left()

                        if time_left < 600:
                            status = "Window ending"
                        elif not self.prices.prices.is_complete():
                            status = "Loading..."
                        else:
                            signal = self.generator.calculate_signal(
                                self.market.state,
                                self.prices.prices
                            )

                            if signal:
                                key = f"{signal.window_start}_{signal.side}_{signal.entry_price:.4f}"

                                if key not in self.signals_this_window:
                                    self.logger.log_signal(signal)
                                    self.signals_this_window.add(key)
                                    status = "🎯 SIGNAL!"
                            else:
                                oracle_lag = abs(self.prices.prices.oracle_lag())
                                if oracle_lag < 20:
                                    status = "Lag <$20"
                                else:
                                    status = "Scanning..."
                    else:
                        status = "Waiting..."

                    last_update = now

                self.dashboard.render(
                    self.market.state,
                    self.prices.prices,
                    self.feeds,
                    self.logger,
                    status
                )

                time.sleep(0.5)

        except KeyboardInterrupt:
            print("\n\n✋ Stopped by user")
        finally:
            self.running = False
            self.feeds.stop()
            self.print_summary()

    def print_summary(self):
        print("\n" + "="*70)
        print("SESSION SUMMARY")
        print("="*70)
        print(f"Signals logged: {self.logger.signals_logged}")
        print(f"Log file: {self.logger.filename}")

        if self.logger.signals_logged > 0:
            signals = self.logger.signals_this_session
            avg = sum(s.net_edge for s in signals) / len(signals) * 100
            print(f"Avg edge: {avg:.2f}%")
            print(f"Max edge: {max(s.net_edge for s in signals)*100:.2f}%")

        print("="*70)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════════╗
║        BTC 15-MIN TRIPLE WEBSOCKET SCANNER v4.0                  ║
╚══════════════════════════════════════════════════════════════════╝

TRIPLE LIVE FEEDS:
✓ Binance WSS - Direct spot (wss://stream.binance.com)
✓ RTDS Binance - Polymarket's Binance feed
✓ RTDS Chainlink - Official oracle (settlement)

REAL-TIME UPDATES:
✓ All prices via WebSocket (sub-second latency)
✓ Oracle lag = Binance - RTDS Chainlink
✓ RTDS lag = RTDS Binance - RTDS Chainlink

INSTALLATION:
pip install websockets

Starting scanner...
    """)

    scanner = SignalScanner(min_edge=0.05)
    scanner.run()