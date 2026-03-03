"""
================================================================================
⚡ POLYMARKET BTC 15-MIN FAST ARBITRAGE DETECTOR v1.0
================================================================================

ARBITRAGE TYPES DETECTED:

1. ORDERBOOK ARB:     YES + NO < $1.00 (guaranteed profit)
2. DEPTH ARB:         Sweep multiple levels for profit
3. ORACLE LAG ARB:    Chainlink behind spot, outcome predictable
4. TIME DECAY ARB:    Near expiry, mispriced vs current price
5. CROSS-WINDOW ARB:  Current vs next window inconsistencies

STRATEGY:
- Buy YES + NO when sum < $1.00, guaranteed $1.00 payout
- When oracle lags and outcome is certain, buy winning side cheap
- Near expiry, if price clearly above/below strike, exploit slow traders
"""

import json
import time
import threading
import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import deque
import statistics

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
WINDOW_SIZE = 900              # 15 minutes
REFRESH_RATE = 0.05            # 20 FPS for fast detection
LOOKAHEAD_WINDOWS = 2

# API Endpoints
EVENT_SLUG_BASE = "https://gamma-api.polymarket.com/events/slug/"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_PRICE_URL = "https://clob.polymarket.com/price"

# Arbitrage Thresholds
ORDERBOOK_ARB_THRESHOLD = 0.998    # YES + NO < this = arb
DEPTH_ARB_MIN_PROFIT = 0.003       # 0.3% min for depth arb
ORACLE_LAG_THRESHOLD_MS = 3000     # Oracle considered lagging if > 3s
ORACLE_LAG_PRICE_DIFF = 50         # $50 diff = high confidence
TIME_DECAY_WINDOW_SECS = 60        # Last 60 seconds of window
POLY_FEE = 0.02                    # 2% fee on winnings
MIN_PROFIT_AFTER_FEES = 0.002      # 0.2% minimum net profit

# ============================================================
# 📊 DATA STRUCTURES
# ============================================================
@dataclass
class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity"""
    arb_type: str                    # ORDERBOOK, DEPTH, ORACLE_LAG, TIME_DECAY, CROSS_WINDOW
    confidence: str                  # HIGH, MEDIUM, LOW
    gross_profit_pct: float
    net_profit_pct: float
    max_size: float                  # Maximum executable size
    entry_actions: List[str]         # What to buy
    exit_info: str                   # How you profit
    urgency: str                     # IMMEDIATE, FAST, NORMAL
    detected_at: float = field(default_factory=time.time)
    expires_in: float = 0.0          # Seconds until opportunity expires
    
    def is_valid(self) -> bool:
        return self.net_profit_pct >= MIN_PROFIT_AFTER_FEES

@dataclass 
class PriceLevel:
    """Single price level in orderbook"""
    price: float
    size: float

@dataclass
class OrderbookSnapshot:
    """Complete orderbook state"""
    bids: List[PriceLevel]
    asks: List[PriceLevel]
    timestamp: float
    
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0
    
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0
    
    def spread(self) -> float:
        return self.best_ask() - self.best_bid()
    
    def bid_volume(self, levels: int = 5) -> float:
        return sum(l.size for l in self.bids[:levels])
    
    def ask_volume(self, levels: int = 5) -> float:
        return sum(l.size for l in self.asks[:levels])

# ============================================================
# 🧱 CORE DATA MANAGER
# ============================================================
class DataCore:
    """Central data repository with price history"""
    def __init__(self):
        self.lock = threading.RLock()
        
        # Current prices
        self.futures_price = 0.0
        self.futures_ts = 0
        self.spot_price = 0.0
        self.spot_ts = 0
        self.relay_price = 0.0
        self.relay_ts = 0
        self.oracle_price = 0.0
        self.oracle_ts = 0
        
        # Price history (for trend detection)
        self.spot_history: deque = deque(maxlen=100)
        self.oracle_history: deque = deque(maxlen=100)
        
        # Orderbooks
        self.up_book: Optional[OrderbookSnapshot] = None
        self.down_book: Optional[OrderbookSnapshot] = None
        
        # Connection status
        self.connected = {
            'futures': False,
            'spot': False,
            'poly': False,
            'clob': False
        }
        
        # Stats
        self.messages_received = 0
        self.arb_opportunities_found = 0

    def update_futures(self, price: float, ts: int):
        with self.lock:
            self.futures_price = price
            self.futures_ts = ts
            self.connected['futures'] = True
            self.messages_received += 1

    def update_spot(self, price: float, ts: int):
        with self.lock:
            self.spot_price = price
            self.spot_ts = ts
            self.spot_history.append((ts, price))
            self.connected['spot'] = True
            self.messages_received += 1

    def update_relay(self, price: float, ts: int):
        with self.lock:
            self.relay_price = price
            self.relay_ts = ts
            self.messages_received += 1

    def update_oracle(self, price: float, ts: int):
        with self.lock:
            self.oracle_price = price
            self.oracle_ts = ts
            self.oracle_history.append((ts, price))
            self.connected['poly'] = True
            self.messages_received += 1

    def update_orderbook(self, side: str, bids: list, asks: list):
        """Update orderbook for UP or DOWN"""
        with self.lock:
            book = OrderbookSnapshot(
                bids=[PriceLevel(float(b['price']), float(b['size'])) for b in bids],
                asks=[PriceLevel(float(a['price']), float(a['size'])) for a in asks],
                timestamp=time.time()
            )
            # Sort properly
            book.bids.sort(key=lambda x: x.price, reverse=True)
            book.asks.sort(key=lambda x: x.price)
            
            if side == 'UP':
                self.up_book = book
            else:
                self.down_book = book
            
            self.connected['clob'] = True
            self.messages_received += 1

    def update_book_level(self, side: str, outcome: str, price: str, size: float, book_side: str):
        """Update single level in orderbook"""
        with self.lock:
            book = self.up_book if outcome == 'UP' else self.down_book
            if not book:
                return
            
            target = book.bids if book_side == 'BUY' else book.asks
            price_f = float(price)
            
            # Find and update or remove level
            found = False
            for i, level in enumerate(target):
                if abs(level.price - price_f) < 0.0001:
                    if size == 0:
                        target.pop(i)
                    else:
                        target[i] = PriceLevel(price_f, size)
                    found = True
                    break
            
            if not found and size > 0:
                target.append(PriceLevel(price_f, size))
                # Re-sort
                if book_side == 'BUY':
                    target.sort(key=lambda x: x.price, reverse=True)
                else:
                    target.sort(key=lambda x: x.price)
            
            book.timestamp = time.time()
            self.messages_received += 1

    def get_oracle_lag_ms(self) -> int:
        """Get oracle lag vs current time"""
        if self.oracle_ts == 0:
            return -1
        return int(time.time() * 1000) - self.oracle_ts

    def get_spot_oracle_diff(self) -> float:
        """Get difference between spot and oracle"""
        if self.spot_price == 0 or self.oracle_price == 0:
            return 0.0
        return self.spot_price - self.oracle_price

    def get_spot_trend(self, window_ms: int = 5000) -> float:
        """Get spot price trend over last N ms (positive = up, negative = down)"""
        with self.lock:
            if len(self.spot_history) < 2:
                return 0.0
            
            now_ms = int(time.time() * 1000)
            cutoff = now_ms - window_ms
            
            recent = [(ts, p) for ts, p in self.spot_history if ts > cutoff]
            if len(recent) < 2:
                return 0.0
            
            return recent[-1][1] - recent[0][1]

core = DataCore()

# ============================================================
# 🎯 MARKET MANAGER
# ============================================================
class MarketManager:
    """Manages current and upcoming 15-minute windows"""
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'PolyArbBot/1.0'})
        
        # Current window
        self.window_start = 0
        self.slug = ""
        self.question = ""
        self.strike = 0.0
        self.up_token_id: Optional[str] = None
        self.down_token_id: Optional[str] = None
        
        # Next window (for cross-window arb)
        self.next_slug = ""
        self.next_up_token_id: Optional[str] = None
        self.next_down_token_id: Optional[str] = None
        self.next_strike = 0.0
        
        # Share prices (from REST API)
        self.up_price = 0.0
        self.down_price = 0.0
        self.next_up_price = 0.0
        self.next_down_price = 0.0
        
        # Cache
        self.cache: Dict[str, dict] = {}
        self.status = "Initializing..."

    def get_current_window_start(self) -> int:
        return (int(time.time()) // WINDOW_SIZE) * WINDOW_SIZE

    def time_remaining(self) -> float:
        """Seconds until current window expires"""
        return max(0, (self.window_start + WINDOW_SIZE) - time.time())

    def time_remaining_str(self) -> str:
        t = self.time_remaining()
        return f"{int(t//60):02d}:{int(t%60):02d}"

    def is_near_expiry(self) -> bool:
        """Are we in the last 60 seconds?"""
        return self.time_remaining() <= TIME_DECAY_WINDOW_SECS

    def lifecycle_check(self) -> bool:
        """Check and handle window transitions. Returns True if transitioned."""
        current = self.get_current_window_start()
        
        if current != self.window_start:
            # Transition!
            self.window_start = current
            self.slug = f"btc-updown-15m-{current}"
            self.next_slug = f"btc-updown-15m-{current + WINDOW_SIZE}"
            
            # Capture strike from oracle
            if core.oracle_price > 0:
                self.strike = core.oracle_price
            
            # Clear token IDs (need to refetch)
            self.up_token_id = None
            self.down_token_id = None
            self.next_up_token_id = None
            self.next_down_token_id = None
            
            # Clear orderbooks
            core.up_book = None
            core.down_book = None
            
            self.status = "Window transition..."
            return True
        
        return False

    def fetch_market_data(self):
        """Fetch current and next window market data"""
        # Current window
        if self.up_token_id is None:
            self._fetch_window(self.slug, is_next=False)
        
        # Next window
        if self.next_up_token_id is None:
            self._fetch_window(self.next_slug, is_next=True)

    def _fetch_window(self, slug: str, is_next: bool):
        """Fetch single window data"""
        # Check cache
        if slug in self.cache:
            cached = self.cache[slug]
            if is_next:
                self.next_up_token_id = cached['up_id']
                self.next_down_token_id = cached['down_id']
            else:
                self.up_token_id = cached['up_id']
                self.down_token_id = cached['down_id']
                self.question = cached.get('question', '')
                self._extract_strike()
            return
        
        try:
            r = self.session.get(f"{EVENT_SLUG_BASE}{slug}", timeout=5)
            if r.status_code != 200:
                return
            
            data = r.json()
            if not data or 'markets' not in data:
                return
            
            market = data['markets'][0]
            
            raw_ids = market.get('clobTokenIds', '[]')
            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            
            outcomes = market.get('outcomes', '[]')
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            
            if len(token_ids) < 2:
                return
            
            if outcomes[0].lower() in ['up', 'yes']:
                up_id, down_id = token_ids[0], token_ids[1]
            else:
                up_id, down_id = token_ids[1], token_ids[0]
            
            # Cache
            self.cache[slug] = {
                'up_id': up_id,
                'down_id': down_id,
                'question': market.get('question', ''),
            }
            
            if is_next:
                self.next_up_token_id = up_id
                self.next_down_token_id = down_id
            else:
                self.up_token_id = up_id
                self.down_token_id = down_id
                self.question = market.get('question', '')
                self._extract_strike()
            
            self.status = "Ready"
            
        except Exception as e:
            self.status = f"Error: {str(e)[:20]}"

    def _extract_strike(self):
        """Extract strike price from question"""
        if self.question and self.strike == 0:
            match = re.search(r'\$([\d,]+\.?\d*)', self.question)
            if match:
                try:
                    self.strike = float(match.group(1).replace(',', ''))
                except:
                    pass

    def fetch_share_prices(self):
        """Fetch current share prices from CLOB REST API"""
        if self.up_token_id:
            try:
                r = self.session.get(f"{CLOB_PRICE_URL}?token_id={self.up_token_id}&side=buy", timeout=2)
                if r.status_code == 200:
                    self.up_price = float(r.json().get('price', 0))
            except:
                pass
            
            try:
                r = self.session.get(f"{CLOB_PRICE_URL}?token_id={self.down_token_id}&side=buy", timeout=2)
                if r.status_code == 200:
                    self.down_price = float(r.json().get('price', 0))
            except:
                pass

    def get_outcome_status(self) -> Tuple[str, float]:
        """Determine current UP/DOWN status based on oracle vs strike"""
        if self.strike == 0 or core.oracle_price == 0:
            return "UNKNOWN", 0.0
        
        diff = core.oracle_price - self.strike
        if diff > 0:
            return "UP", diff
        else:
            return "DOWN", abs(diff)

market = MarketManager()

# ============================================================
# 🔍 ARBITRAGE DETECTION ENGINE
# ============================================================
class ArbitrageDetector:
    """Detects various arbitrage opportunities"""
    
    def __init__(self):
        self.opportunities: List[ArbitrageOpportunity] = []
        self.last_scan = 0.0
        self.scans_performed = 0
    
    def scan_all(self) -> List[ArbitrageOpportunity]:
        """Run all arbitrage scans"""
        self.opportunities.clear()
        self.scans_performed += 1
        self.last_scan = time.time()
        
        # 1. Orderbook arbitrage (YES + NO < $1)
        self._scan_orderbook_arb()
        
        # 2. Depth arbitrage (sweep multiple levels)
        self._scan_depth_arb()
        
        # 3. Oracle lag arbitrage
        self._scan_oracle_lag_arb()
        
        # 4. Time decay arbitrage (near expiry)
        self._scan_time_decay_arb()
        
        # Filter valid opportunities
        valid = [o for o in self.opportunities if o.is_valid()]
        
        # Sort by net profit
        valid.sort(key=lambda x: x.net_profit_pct, reverse=True)
        
        core.arb_opportunities_found = len(valid)
        return valid

    def _scan_orderbook_arb(self):
        """Scan for YES + NO < $1.00 at best ask"""
        if not core.up_book or not core.down_book:
            return
        
        up_ask = core.up_book.best_ask()
        down_ask = core.down_book.best_ask()
        
        if up_ask <= 0 or down_ask <= 0:
            return
        
        total_cost = up_ask + down_ask
        
        if total_cost < ORDERBOOK_ARB_THRESHOLD:
            gross_profit = 1.0 - total_cost
            gross_pct = gross_profit / total_cost
            
            # Calculate fees (fee on winner = 2% of (1 - winning_side_cost))
            min_cost = min(up_ask, down_ask)
            fee = POLY_FEE * (1.0 - min_cost)
            net_profit = gross_profit - fee
            net_pct = net_profit / total_cost
            
            # Max size limited by smaller side
            up_size = core.up_book.asks[0].size if core.up_book.asks else 0
            down_size = core.down_book.asks[0].size if core.down_book.asks else 0
            max_size = min(up_size, down_size)
            
            self.opportunities.append(ArbitrageOpportunity(
                arb_type="ORDERBOOK",
                confidence="HIGH",
                gross_profit_pct=gross_pct,
                net_profit_pct=net_pct,
                max_size=max_size,
                entry_actions=[
                    f"BUY UP @ ${up_ask:.4f} (size: {up_size:,.0f})",
                    f"BUY DOWN @ ${down_ask:.4f} (size: {down_size:,.0f})",
                ],
                exit_info=f"Hold until expiry. Guaranteed $1.00 payout. Cost: ${total_cost:.4f}",
                urgency="IMMEDIATE",
                expires_in=market.time_remaining(),
            ))

    def _scan_depth_arb(self):
        """Scan for arbitrage by sweeping multiple price levels"""
        if not core.up_book or not core.down_book:
            return
        
        # Try sweeping up to 5 levels
        for depth in range(2, 6):
            total_cost = 0.0
            total_size = float('inf')
            
            up_asks = core.up_book.asks[:depth]
            down_asks = core.down_book.asks[:depth]
            
            if len(up_asks) < depth or len(down_asks) < depth:
                continue
            
            # Calculate weighted average cost
            up_cost = sum(a.price * a.size for a in up_asks) / sum(a.size for a in up_asks) if up_asks else 0
            down_cost = sum(a.price * a.size for a in down_asks) / sum(a.size for a in down_asks) if down_asks else 0
            
            total_cost = up_cost + down_cost
            
            if total_cost < ORDERBOOK_ARB_THRESHOLD:
                gross_profit = 1.0 - total_cost
                gross_pct = gross_profit / total_cost
                
                min_cost = min(up_cost, down_cost)
                fee = POLY_FEE * (1.0 - min_cost)
                net_profit = gross_profit - fee
                net_pct = net_profit / total_cost
                
                if net_pct >= DEPTH_ARB_MIN_PROFIT:
                    max_size = min(sum(a.size for a in up_asks), sum(a.size for a in down_asks))
                    
                    self.opportunities.append(ArbitrageOpportunity(
                        arb_type="DEPTH",
                        confidence="MEDIUM",
                        gross_profit_pct=gross_pct,
                        net_profit_pct=net_pct,
                        max_size=max_size,
                        entry_actions=[
                            f"SWEEP {depth} levels UP (avg ${up_cost:.4f})",
                            f"SWEEP {depth} levels DOWN (avg ${down_cost:.4f})",
                        ],
                        exit_info=f"Hold until expiry. Avg cost: ${total_cost:.4f}",
                        urgency="FAST",
                        expires_in=market.time_remaining(),
                    ))
                    break  # Only report best depth arb

    def _scan_oracle_lag_arb(self):
        """Detect when oracle lags spot and outcome is predictable"""
        oracle_lag = core.get_oracle_lag_ms()
        spot_oracle_diff = core.get_spot_oracle_diff()
        
        if oracle_lag < ORACLE_LAG_THRESHOLD_MS:
            return  # Oracle is fresh enough
        
        if abs(spot_oracle_diff) < ORACLE_LAG_PRICE_DIFF:
            return  # Not enough price difference
        
        if market.strike == 0:
            return
        
        # Current spot vs strike
        spot_vs_strike = core.spot_price - market.strike
        
        # If spot is way above strike and oracle is lagging behind spot
        # Then UP is likely to win, but maybe priced low
        if spot_vs_strike > 0 and spot_oracle_diff > 0:
            # Spot above strike AND spot ahead of oracle = UP winning
            # Check if UP is underpriced
            up_ask = core.up_book.best_ask() if core.up_book else 0
            if up_ask > 0 and up_ask < 0.70:  # Underpriced given high confidence
                implied_profit = 1.0 - up_ask
                
                # Confidence based on margin
                confidence = "HIGH" if spot_vs_strike > 100 else "MEDIUM" if spot_vs_strike > 50 else "LOW"
                
                self.opportunities.append(ArbitrageOpportunity(
                    arb_type="ORACLE_LAG",
                    confidence=confidence,
                    gross_profit_pct=implied_profit / up_ask,
                    net_profit_pct=(implied_profit - POLY_FEE * implied_profit) / up_ask,
                    max_size=core.up_book.asks[0].size if core.up_book and core.up_book.asks else 0,
                    entry_actions=[
                        f"BUY UP @ ${up_ask:.4f}",
                        f"Oracle lags by {oracle_lag}ms",
                        f"Spot ${core.spot_price:,.0f} vs Strike ${market.strike:,.0f}",
                    ],
                    exit_info=f"UP likely wins. Oracle will catch up. Spot +${spot_vs_strike:.0f} above strike.",
                    urgency="FAST",
                    expires_in=market.time_remaining(),
                ))
        
        elif spot_vs_strike < 0 and spot_oracle_diff < 0:
            # Spot below strike AND spot behind oracle = DOWN winning
            down_ask = core.down_book.best_ask() if core.down_book else 0
            if down_ask > 0 and down_ask < 0.70:
                implied_profit = 1.0 - down_ask
                
                confidence = "HIGH" if abs(spot_vs_strike) > 100 else "MEDIUM" if abs(spot_vs_strike) > 50 else "LOW"
                
                self.opportunities.append(ArbitrageOpportunity(
                    arb_type="ORACLE_LAG",
                    confidence=confidence,
                    gross_profit_pct=implied_profit / down_ask,
                    net_profit_pct=(implied_profit - POLY_FEE * implied_profit) / down_ask,
                    max_size=core.down_book.asks[0].size if core.down_book and core.down_book.asks else 0,
                    entry_actions=[
                        f"BUY DOWN @ ${down_ask:.4f}",
                        f"Oracle lags by {oracle_lag}ms",
                        f"Spot ${core.spot_price:,.0f} vs Strike ${market.strike:,.0f}",
                    ],
                    exit_info=f"DOWN likely wins. Oracle will catch up. Spot ${spot_vs_strike:.0f} below strike.",
                    urgency="FAST",
                    expires_in=market.time_remaining(),
                ))

    def _scan_time_decay_arb(self):
        """Detect mispricing near expiry when outcome is near-certain"""
        if not market.is_near_expiry():
            return
        
        time_left = market.time_remaining()
        outcome, margin = market.get_outcome_status()
        
        if outcome == "UNKNOWN" or margin < 30:
            return  # Not confident enough
        
        # Near expiry with clear outcome - winning side should be > 0.90
        # If priced lower, that's an opportunity
        
        if outcome == "UP":
            up_ask = core.up_book.best_ask() if core.up_book else 0
            if up_ask > 0 and up_ask < 0.90:
                implied_profit = 1.0 - up_ask
                
                confidence = "HIGH" if margin > 100 and time_left < 30 else "MEDIUM"
                
                self.opportunities.append(ArbitrageOpportunity(
                    arb_type="TIME_DECAY",
                    confidence=confidence,
                    gross_profit_pct=implied_profit / up_ask,
                    net_profit_pct=(implied_profit - POLY_FEE * implied_profit) / up_ask,
                    max_size=core.up_book.asks[0].size if core.up_book and core.up_book.asks else 0,
                    entry_actions=[
                        f"BUY UP @ ${up_ask:.4f}",
                        f"Only {time_left:.0f}s left!",
                        f"Oracle +${margin:.0f} above strike",
                    ],
                    exit_info=f"UP almost certain to win. {time_left:.0f}s to expiry.",
                    urgency="IMMEDIATE",
                    expires_in=time_left,
                ))
        
        elif outcome == "DOWN":
            down_ask = core.down_book.best_ask() if core.down_book else 0
            if down_ask > 0 and down_ask < 0.90:
                implied_profit = 1.0 - down_ask
                
                confidence = "HIGH" if margin > 100 and time_left < 30 else "MEDIUM"
                
                self.opportunities.append(ArbitrageOpportunity(
                    arb_type="TIME_DECAY",
                    confidence=confidence,
                    gross_profit_pct=implied_profit / down_ask,
                    net_profit_pct=(implied_profit - POLY_FEE * implied_profit) / down_ask,
                    max_size=core.down_book.asks[0].size if core.down_book and core.down_book.asks else 0,
                    entry_actions=[
                        f"BUY DOWN @ ${down_ask:.4f}",
                        f"Only {time_left:.0f}s left!",
                        f"Oracle ${margin:.0f} below strike",
                    ],
                    exit_info=f"DOWN almost certain to win. {time_left:.0f}s to expiry.",
                    urgency="IMMEDIATE",
                    expires_in=time_left,
                ))

detector = ArbitrageDetector()

# ============================================================
# 📡 WEBSOCKET CLIENTS
# ============================================================
class BinanceFuturesClient:
    def __init__(self):
        self.url = "wss://fstream.binance.com/ws/btcusdt@aggTrade"

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        self.url,
                        on_message=lambda ws, msg: self._on_msg(msg),
                        on_error=lambda ws, e: None,
                    )
                    ws.run_forever(ping_interval=30)
                except:
                    time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _on_msg(self, msg):
        try:
            d = json.loads(msg)
            core.update_futures(float(d['p']), int(d['T']))
        except:
            pass


class BinanceSpotClient:
    def __init__(self):
        self.url = "wss://stream.binance.com:9443/ws/btcusdt@trade"

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        self.url,
                        on_message=lambda ws, msg: self._on_msg(msg),
                        on_error=lambda ws, e: None,
                    )
                    ws.run_forever(ping_interval=30)
                except:
                    time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _on_msg(self, msg):
        try:
            d = json.loads(msg)
            core.update_spot(float(d['p']), int(d['T']))
        except:
            pass


class PolyDataClient:
    def __init__(self):
        self.url = "wss://ws-live-data.polymarket.com"

    def start(self):
        def on_open(ws):
            ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [
                    {"topic": "crypto_prices", "type": "*", "filters": '{"symbol":"btcusdt"}'},
                    {"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'},
                ]
            }))

        def on_msg(ws, msg):
            try:
                d = json.loads(msg)
                if d.get('type') == 'update':
                    topic = d.get('topic')
                    payload = d.get('payload', {})
                    if topic == 'crypto_prices' and payload.get('symbol') == 'btcusdt':
                        core.update_relay(float(payload['value']), int(payload['timestamp']))
                    elif topic == 'crypto_prices_chainlink' and payload.get('symbol') == 'btc/usd':
                        core.update_oracle(float(payload['value']), int(payload['timestamp']))
            except:
                pass

        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(self.url, on_open=on_open, on_message=on_msg, on_error=lambda ws, e: None)
                    ws.run_forever(ping_interval=30)
                except:
                    time.sleep(1)

        threading.Thread(target=run, daemon=True).start()


class PolyClobClient:
    def __init__(self):
        self.url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        self.ws = None
        self.subscribed = set()

    def start(self):
        def run():
            while True:
                try:
                    self.ws = websocket.WebSocketApp(
                        self.url,
                        on_open=lambda ws: self._subscribe(),
                        on_message=self._on_msg,
                        on_error=lambda ws, e: None,
                    )
                    self.ws.run_forever(ping_interval=30)
                except:
                    time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _subscribe(self):
        ids = []
        if market.up_token_id and market.up_token_id not in self.subscribed:
            ids.append(market.up_token_id)
        if market.down_token_id and market.down_token_id not in self.subscribed:
            ids.append(market.down_token_id)
        
        if ids and self.ws:
            try:
                self.ws.send(json.dumps({"type": "subscribe", "channel": "market", "assets_ids": ids}))
                self.subscribed.update(ids)
            except:
                pass

    def resubscribe(self):
        self.subscribed.clear()
        self._subscribe()

    def _on_msg(self, ws, msg):
        try:
            data = json.loads(msg)
            event = data.get('event_type')
            
            if event == 'book':
                asset = data.get('asset_id')
                if asset == market.up_token_id:
                    core.update_orderbook('UP', data.get('bids', []), data.get('asks', []))
                elif asset == market.down_token_id:
                    core.update_orderbook('DOWN', data.get('bids', []), data.get('asks', []))
                    
            elif event == 'price_change':
                for c in data.get('price_changes', []):
                    asset = c.get('asset_id')
                    outcome = 'UP' if asset == market.up_token_id else 'DOWN' if asset == market.down_token_id else None
                    if outcome:
                        core.update_book_level(c.get('side'), outcome, c.get('price'), float(c.get('size', 0)), c.get('side'))
        except:
            pass

clob_client = PolyClobClient()

# ============================================================
# 🎨 UI RENDERING
# ============================================================
def render_arbitrage_panel(opportunities: List[ArbitrageOpportunity]) -> Panel:
    """Render detected arbitrage opportunities"""
    if not opportunities:
        content = Text()
        content.append("🔍 SCANNING FOR ARBITRAGE...\n\n", style="dim")
        content.append(f"Scans: {detector.scans_performed}\n", style="dim")
        content.append(f"Messages: {core.messages_received:,}\n", style="dim")
        
        # Show current market state
        if core.up_book and core.down_book:
            up_ask = core.up_book.best_ask()
            down_ask = core.down_book.best_ask()
            total = up_ask + down_ask
            content.append(f"\nUP Ask: ${up_ask:.4f}\n", style="green")
            content.append(f"DOWN Ask: ${down_ask:.4f}\n", style="red")
            content.append(f"Total: ${total:.4f} ", style="white")
            if total < 1.0:
                content.append(f"(GAP: ${1-total:.4f})", style="bold green")
            else:
                content.append(f"(NO ARB)", style="dim")
        
        return Panel(content, title="📊 ARBITRAGE STATUS", border_style="dim")
    
    # We have opportunities!
    layout = Layout()
    panels = []
    
    for i, opp in enumerate(opportunities[:3]):  # Top 3
        opp_text = Text()
        
        # Type and confidence
        type_colors = {
            "ORDERBOOK": "bold green",
            "DEPTH": "bold cyan",
            "ORACLE_LAG": "bold yellow",
            "TIME_DECAY": "bold magenta",
        }
        opp_text.append(f"#{i+1} ", style="bold white")
        opp_text.append(f"{opp.arb_type}", style=type_colors.get(opp.arb_type, "white"))
        opp_text.append(f" [{opp.confidence}]\n", style="dim")
        
        # Profit
        opp_text.append(f"Gross: {opp.gross_profit_pct:.2%} | ", style="green")
        opp_text.append(f"Net: {opp.net_profit_pct:.2%}\n", style="bold green")
        
        # Size and urgency
        opp_text.append(f"Max Size: {opp.max_size:,.0f} | ", style="yellow")
        urgency_style = "bold red blink" if opp.urgency == "IMMEDIATE" else "bold yellow" if opp.urgency == "FAST" else "white"
        opp_text.append(f"Urgency: {opp.urgency}\n", style=urgency_style)
        
        # Actions
        opp_text.append("\n📋 ACTIONS:\n", style="bold white")
        for action in opp.entry_actions:
            opp_text.append(f"  → {action}\n", style="cyan")
        
        # Exit info
        opp_text.append(f"\n💰 {opp.exit_info}\n", style="italic")
        
        # Expires
        if opp.expires_in > 0:
            opp_text.append(f"⏱️ Expires in: {opp.expires_in:.0f}s", style="red" if opp.expires_in < 30 else "yellow")
        
        border = "bold red" if opp.urgency == "IMMEDIATE" else "bold yellow" if opp.urgency == "FAST" else "green"
        panels.append(Panel(opp_text, border_style=border))
    
    if len(panels) == 1:
        return Panel(panels[0], title="🚨 ARBITRAGE DETECTED 🚨", border_style="bold red")
    
    content = Layout()
    content.split(*[Layout(p) for p in panels])
    return Panel(content, title=f"🚨 {len(opportunities)} ARBITRAGE OPPORTUNITIES 🚨", border_style="bold red")


def render_orderbook_mini(book: Optional[OrderbookSnapshot], name: str, color: str) -> Panel:
    """Render compact orderbook"""
    if not book:
        return Panel(Text("Waiting for data...", style="dim"), title=name, border_style="dim")
    
    table = Table(box=box.SIMPLE, expand=True, show_header=False, padding=0)
    table.add_column("Bid", justify="right", style="green", width=12)
    table.add_column("Price", justify="center", style="white", width=10)
    table.add_column("Ask", justify="left", style="red", width=12)
    
    for i in range(min(5, max(len(book.bids), len(book.asks)))):
        bid_sz = f"{book.bids[i].size:,.0f}" if i < len(book.bids) else ""
        bid_px = f"{book.bids[i].price:.3f}" if i < len(book.bids) else ""
        ask_px = f"{book.asks[i].price:.3f}" if i < len(book.asks) else ""
        ask_sz = f"{book.asks[i].size:,.0f}" if i < len(book.asks) else ""
        table.add_row(bid_sz, f"{bid_px}|{ask_px}", ask_sz)
    
    spread = book.spread()
    spread_style = "green" if spread < 0.01 else "yellow" if spread < 0.03 else "red"
    
    header = Text()
    header.append(f"{name} ", style=f"bold {color}")
    header.append(f"Spread: {spread:.4f}", style=spread_style)
    
    content = Layout()
    content.split(
        Layout(Align.center(header), size=1),
        Layout(table),
    )
    
    return Panel(content, border_style=color)


def render_prices_panel() -> Panel:
    """Render price feeds compactly"""
    now_ms = int(time.time() * 1000)
    
    content = Text()
    content.append("PRICE FEEDS\n", style="bold cyan underline")
    
    # Spot
    spot_lag = (now_ms - core.spot_ts) if core.spot_ts else -1
    content.append(f"Spot:    ${core.spot_price:>10,.2f}", style="white")
    content.append(f"  {spot_lag:>4}ms\n", style="green" if spot_lag < 100 else "yellow")
    
    # Futures  
    fut_lag = (now_ms - core.futures_ts) if core.futures_ts else -1
    content.append(f"Futures: ${core.futures_price:>10,.2f}", style="white")
    content.append(f"  {fut_lag:>4}ms\n", style="green" if fut_lag < 100 else "yellow")
    
    # Oracle (most important)
    oracle_lag = (now_ms - core.oracle_ts) if core.oracle_ts else -1
    oracle_style = "bold green" if oracle_lag < 3000 else "bold red"
    content.append(f"Oracle:  ${core.oracle_price:>10,.2f}", style=oracle_style)
    content.append(f"  {oracle_lag:>4}ms", style=oracle_style)
    
    if oracle_lag > 3000:
        content.append(" ⚠️ LAGGING!", style="bold red blink")
    
    content.append("\n")
    
    # Oracle vs Spot diff
    if core.spot_price > 0 and core.oracle_price > 0:
        diff = core.oracle_price - core.spot_price
        content.append(f"\nOracle Gap: ", style="dim")
        content.append(f"${diff:+.2f}", style="yellow" if abs(diff) > 20 else "green")
    
    return Panel(content, title="📡", border_style="cyan")


def render_market_panel() -> Panel:
    """Render market info compactly"""
    time_left = market.time_remaining()
    time_str = market.time_remaining_str()
    
    content = Text()
    
    # Timer (BIG)
    timer_style = "bold green" if time_left > 60 else "bold yellow" if time_left > 30 else "bold red blink"
    content.append(f"⏱️ {time_str}\n", style=timer_style)
    
    # Strike
    if market.strike > 0:
        content.append(f"Strike: ${market.strike:,.2f}\n", style="cyan")
        
        # Status
        outcome, margin = market.get_outcome_status()
        if outcome == "UP":
            content.append(f"📈 UP +${margin:.0f}", style="bold green")
        elif outcome == "DOWN":
            content.append(f"📉 DOWN -${margin:.0f}", style="bold red")
        else:
            content.append("⏳ Waiting...", style="dim")
    
    content.append(f"\n\n{market.slug}", style="dim")
    
    return Panel(content, title="🎯 MARKET", border_style="blue")


def render_stats_panel() -> Panel:
    """Render stats"""
    content = Text()
    content.append(f"Messages: {core.messages_received:,}\n", style="dim")
    content.append(f"Scans: {detector.scans_performed}\n", style="dim")
    content.append(f"Arbs Found: {core.arb_opportunities_found}\n", style="green" if core.arb_opportunities_found > 0 else "dim")
    
    # Connection status
    content.append("\nWS: ", style="dim")
    content.append("F", style="green" if core.connected['futures'] else "red")
    content.append("S", style="green" if core.connected['spot'] else "red")
    content.append("P", style="green" if core.connected['poly'] else "red")
    content.append("C", style="green" if core.connected['clob'] else "red")
    
    return Panel(content, title="📊", border_style="dim")


def generate_dashboard() -> Layout:
    """Generate complete dashboard"""
    # Lifecycle check
    if market.lifecycle_check():
        clob_client.resubscribe()
    
    # Fetch market data if needed
    market.fetch_market_data()
    
    # Resubscribe if needed
    if market.up_token_id and market.up_token_id not in clob_client.subscribed:
        clob_client.resubscribe()
    
    # Run arbitrage detection
    opportunities = detector.scan_all()
    
    # Build layout
    layout = Layout()
    
    # Header
    header = Text()
    header.append("⚡ BTC 15-MIN ARBITRAGE DETECTOR ", style="bold cyan")
    header.append(datetime.now().strftime("%H:%M:%S.%f")[:-3], style="dim")
    
    layout.split(
        Layout(Panel(header, box=box.MINIMAL), size=3),
        Layout(name="main"),
        Layout(name="bottom", size=8),
    )
    
    # Main area - arbitrage panel
    layout["main"].update(render_arbitrage_panel(opportunities))
    
    # Bottom - orderbooks + info
    layout["bottom"].split_row(
        Layout(render_orderbook_mini(core.up_book, "UP", "green"), ratio=1),
        Layout(render_orderbook_mini(core.down_book, "DOWN", "red"), ratio=1),
        Layout(name="info", ratio=1),
    )
    
    layout["bottom"]["info"].split(
        Layout(render_market_panel()),
        Layout(render_prices_panel()),
        Layout(render_stats_panel()),
    )
    
    return layout

# ============================================================
# 🔄 BACKGROUND TASKS
# ============================================================
def background_updater():
    """Background updates"""
    while True:
        try:
            market.fetch_share_prices()
            time.sleep(2)
        except:
            time.sleep(2)

# ============================================================
# 🚀 MAIN
# ============================================================
def main():
    console = Console()
    
    console.print("\n[bold cyan]⚡ BTC 15-MIN FAST ARBITRAGE DETECTOR[/bold cyan]")
    console.print("[dim]Designed for fast-resolving Polymarket markets[/dim]\n")
    
    # Start all WebSocket clients
    console.print("[yellow]Starting price feeds...[/yellow]")
    BinanceFuturesClient().start()
    BinanceSpotClient().start()
    PolyDataClient().start()
    clob_client.start()
    
    # Initialize market
    console.print("[yellow]Loading market...[/yellow]")
    market.lifecycle_check()
    market.fetch_market_data()
    
    if market.up_token_id:
        console.print(f"[green]✓ Market: {market.slug}[/green]")
        console.print(f"[green]✓ Strike: ${market.strike:,.2f}[/green]")
    
    # Start background thread
    threading.Thread(target=background_updater, daemon=True).start()
    
    console.print("\n[bold green]Starting scanner...[/bold green]")
    time.sleep(1)
    
    try:
        with Live(generate_dashboard(), refresh_per_second=int(1/REFRESH_RATE), screen=True) as live:
            while True:
                live.update(generate_dashboard())
                time.sleep(REFRESH_RATE)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")


if __name__ == "__main__":
    main()