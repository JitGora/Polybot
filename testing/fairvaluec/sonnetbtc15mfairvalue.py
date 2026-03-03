
"""
================================================================================
⚡ BTC 15-MIN FAIR VALUE SYSTEM - ENHANCED v2.0
================================================================================

IMPROVEMENTS:
- Fixed orderbook race condition on window transitions
- Dynamic Yang-Zhang volatility estimation (replaces hardcoded 0.55)
- Order Flow Imbalance (OFI) model for microstructure signals
- VWAP mean reversion model
- Jump-diffusion detection and continuation
- Futures lead-lag relationship modeling
- Implied volatility calibration from market prices
- Adaptive ensemble weighting based on market conditions
- Enhanced error handling and logging

DATA STREAMS:
1. BINANCE FUTURES:  wss://fstream.binance.com/ws/btcusdt@aggTrade
2. BINANCE SPOT:     wss://stream.binance.com:9443/ws/btcusdt@trade
3. POLY RELAY:       wss://ws-live-data.polymarket.com (topic: crypto_prices)
4. CHAINLINK ORACLE: wss://ws-live-data.polymarket.com (topic: crypto_prices_chainlink)
"""

import json
import time
import threading
import math
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Deque
from collections import deque
from dataclasses import dataclass, field

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
# ⚙️ CONFIG
# ============================================================
WINDOW_SIZE = 900  # 15 minutes in seconds
POLY_FEE = 0.02    # 2% taker fee
MIN_EDGE = 0.025   # 2.5% minimum net edge to signal
GAMMA_API = "https://gamma-api.polymarket.com/events"

# ============================================================
# 📊 DATA STRUCTURES
# ============================================================
@dataclass
class PriceTick:
    """Individual price observation with timestamp"""
    price: float
    timestamp_ms: int

@dataclass
class FairValue:
    """Fair value estimate from a pricing model"""
    model: str
    fv_up: float
    fv_down: float
    confidence: float

@dataclass
class Signal:
    """Trading signal with edge calculation"""
    side: str
    action: str
    price: float
    fair_value: float
    edge: float
    net_edge: float
    size: float
    confidence: str
    reason: str

@dataclass
class OrderFlowState:
    """Order flow imbalance tracking"""
    trade_imbalance: float = 0.0  # +1 = all buys, -1 = all sells
    timestamp: int = 0

@dataclass
class JumpState:
    """Price jump detection state"""
    last_jump_time: int = 0
    jump_direction: int = 0  # +1 up, -1 down
    jump_magnitude: float = 0.0

# ============================================================
# 🧱 DATA CORE - ALL 4 PRICES + MICROSTRUCTURE
# ============================================================
class DataCore:
    """Central data repository for all price feeds and market data"""

    def __init__(self):
        self.lock = threading.RLock()

        # Price feeds
        self.futures_price: float = 0.0
        self.futures_ts: int = 0
        self.spot_price: float = 0.0
        self.spot_ts: int = 0
        self.relay_price: float = 0.0
        self.relay_ts: int = 0
        self.oracle_price: float = 0.0
        self.oracle_ts: int = 0

        # Price history for analytics
        self.spot_history: Deque[PriceTick] = deque(maxlen=1000)

        # Microstructure state
        self.ofi_state = OrderFlowState()
        self.jump_state = JumpState()

        # Orderbook
        self.up_bids: Dict[str, float] = {}
        self.up_asks: Dict[str, float] = {}
        self.down_bids: Dict[str, float] = {}
        self.down_asks: Dict[str, float] = {}

        # Connection status
        self.connected = {
            'futures': False,
            'spot': False,
            'relay': False,
            'oracle': False,
            'clob': False
        }
        self.msg_count: int = 0

    def update_futures(self, price: float, ts: int, is_buy: bool = True, size: float = 0):
        """Update futures price and track order flow"""
        with self.lock:
            self.futures_price = price
            self.futures_ts = ts
            self.connected['futures'] = True
            self.msg_count += 1

            # Track order flow imbalance
            if size > 0:
                decay = 0.95
                self.ofi_state.trade_imbalance *= decay
                self.ofi_state.trade_imbalance += (1 if is_buy else -1) * size / 1000
                self.ofi_state.timestamp = ts

    def update_spot(self, price: float, ts: int):
        """Update spot price and maintain history"""
        with self.lock:
            self.spot_price = price
            self.spot_ts = ts
            self.spot_history.append(PriceTick(price, ts))
            self.connected['spot'] = True
            self.msg_count += 1

            # Detect price jumps
            self._detect_jump()

    def update_relay(self, price: float, ts: int):
        """Update Polymarket relay price"""
        with self.lock:
            self.relay_price = price
            self.relay_ts = ts
            self.connected['relay'] = True
            self.msg_count += 1

    def update_oracle(self, price: float, ts: int):
        """Update Chainlink oracle price (settlement reference)"""
        with self.lock:
            self.oracle_price = price
            self.oracle_ts = ts
            self.connected['oracle'] = True
            self.msg_count += 1

    def _detect_jump(self):
        """Detect large price jumps for jump-diffusion model"""
        if len(self.spot_history) < 20:
            return

        recent = list(self.spot_history)[-20:]  # Last 2 seconds @ 10 ticks/sec
        price_change = (recent[-1].price - recent[0].price) / recent[0].price
        time_elapsed = (recent[-1].timestamp_ms - recent[0].timestamp_ms) / 1000

        # Jump threshold: >0.15% in <5 seconds
        if abs(price_change) > 0.0015 and time_elapsed < 5:
            self.jump_state = JumpState(
                last_jump_time=recent[-1].timestamp_ms,
                jump_direction=1 if price_change > 0 else -1,
                jump_magnitude=abs(price_change)
            )

    def set_book(self, side: str, bids: list, asks: list):
        """Set full orderbook snapshot"""
        with self.lock:
            if side == 'UP':
                self.up_bids = {b['price']: float(b['size']) for b in bids}
                self.up_asks = {a['price']: float(a['size']) for a in asks}
            else:
                self.down_bids = {b['price']: float(b['size']) for b in bids}
                self.down_asks = {a['price']: float(a['size']) for a in asks}
            self.connected['clob'] = True
            self.msg_count += 1

    def update_level(self, side: str, price: str, size: float, book_side: str):
        """Update individual orderbook level"""
        with self.lock:
            if side == 'UP':
                target = self.up_bids if book_side == 'BUY' else self.up_asks
            else:
                target = self.down_bids if book_side == 'BUY' else self.down_asks

            if size == 0:
                target.pop(price, None)
            else:
                target[price] = size
            self.msg_count += 1

    def best_prices(self) -> Dict:
        """Get best bid/ask prices from orderbook"""
        with self.lock:
            up_ask_price = min((float(p) for p in self.up_asks.keys()), default=0)
            down_ask_price = min((float(p) for p in self.down_asks.keys()), default=0)
            return {
                'up_bid': max((float(p) for p in self.up_bids.keys()), default=0),
                'up_ask': up_ask_price,
                'up_ask_size': self.up_asks.get(f"{up_ask_price:.2f}", 
                                                 self.up_asks.get(str(up_ask_price), 0)) if up_ask_price else 0,
                'down_bid': max((float(p) for p in self.down_bids.keys()), default=0),
                'down_ask': down_ask_price,
                'down_ask_size': self.down_asks.get(f"{down_ask_price:.2f}", 
                                                     self.down_asks.get(str(down_ask_price), 0)) if down_ask_price else 0,
            }

    def oracle_lag_sec(self) -> float:
        """Calculate oracle latency in seconds"""
        if self.oracle_ts == 0:
            return -1
        return (time.time() * 1000 - self.oracle_ts) / 1000

    def get_history(self, seconds: float) -> List[PriceTick]:
        """Get price history for specified time window"""
        with self.lock:
            cutoff = time.time() * 1000 - seconds * 1000
            return [t for t in self.spot_history if t.timestamp_ms > cutoff]

core = DataCore()

# ============================================================
# 🎯 MARKET MANAGER
# ============================================================
class MarketManager:
    """Manages market windows, strikes, and token IDs"""

    def __init__(self):
        self.session = requests.Session()
        self.window_start: int = 0
        self.slug: str = ""
        self.strike: float = 0.0
        self.strike_captured: bool = False
        self.up_token: Optional[str] = None
        self.down_token: Optional[str] = None
        self.question: str = ""
        self.status: str = "Init..."
        self.cache: Dict = {}

    def current_window(self) -> int:
        """Get current 15-minute window timestamp"""
        return (int(time.time()) // WINDOW_SIZE) * WINDOW_SIZE

    def time_left(self) -> float:
        """Seconds remaining in current window"""
        return max(0, (self.window_start + WINDOW_SIZE) - time.time())

    def lifecycle(self) -> bool:
        """
        Manage window lifecycle. Returns True when new window starts.
        Resets tokens and captures strike price at window start.
        """
        current = self.current_window()
        if current != self.window_start:
            self.window_start = current
            self.slug = f"btc-updown-15m-{current}"
            self.up_token = None
            self.down_token = None
            self.strike_captured = False

            # Capture strike from oracle at window start
            if core.oracle_price > 0:
                self.strike = core.oracle_price
                self.strike_captured = True
                self.status = f"Strike: ${self.strike:,.2f}"
            else:
                self.strike = 0.0
                self.status = "Waiting for oracle..."

            # Clear orderbooks for new market
            with core.lock:
                core.up_bids.clear()
                core.up_asks.clear()
                core.down_bids.clear()
                core.down_asks.clear()

            return True

        # Try to capture strike in first 2 seconds if missed
        if not self.strike_captured and core.oracle_price > 0:
            elapsed = time.time() - self.window_start
            if elapsed < 2.0:
                self.strike = core.oracle_price
                self.strike_captured = True
                self.status = f"Strike: ${self.strike:,.2f}"

        return False

    def fetch_tokens(self) -> bool:
        """Fetch token IDs from Polymarket API"""
        if self.up_token:
            return True

        # Check cache first
        if self.slug in self.cache:
            c = self.cache[self.slug]
            self.up_token = c['up']
            self.down_token = c['down']
            self.question = c.get('q', '')
            return True

        try:
            r = self.session.get(f"{GAMMA_API}?slug={self.slug}", timeout=5)
            if r.status_code != 200:
                return False

            data = r.json()
            if not data:
                return False

            event = data[0]
            mkt = event.get('markets', [{}])[0]

            ids = json.loads(mkt.get('clobTokenIds', '[]'))
            outcomes = json.loads(mkt.get('outcomes', '[]'))

            if len(ids) < 2:
                return False

            # Map outcomes to token IDs
            if outcomes[0].lower() in ['up', 'yes']:
                self.up_token, self.down_token = ids[0], ids[1]
            else:
                self.up_token, self.down_token = ids[1], ids[0]

            self.question = mkt.get('question', '')
            self.cache[self.slug] = {'up': self.up_token, 'down': self.down_token, 'q': self.question}

            # Extract strike from question as backup
            if not self.strike_captured and self.question:
                m = re.search(r'\$([\d,]+\.?\d*)', self.question)
                if m:
                    self.strike = float(m.group(1).replace(',', ''))

            self.status = "Ready"
            return True

        except Exception as e:
            self.status = f"Error: {str(e)[:20]}"
            return False

    def outcome_status(self) -> Tuple[str, float]:
        """Current outcome based on oracle vs strike"""
        if self.strike <= 0 or core.oracle_price <= 0:
            return "UNKNOWN", 0.0
        diff = core.oracle_price - self.strike
        return ("UP", diff) if diff > 0 else ("DOWN", abs(diff))

market = MarketManager()

# ============================================================
# 📐 VOLATILITY CALCULATIONS
# ============================================================
class VolatilityEngine:
    """Advanced volatility estimation methods"""

    @staticmethod
    def yang_zhang(history: List[PriceTick]) -> float:
        """
        Yang-Zhang volatility estimator - 14x more efficient than close-to-close.
        Uses OHLC data to capture overnight jumps and intraday range.
        """
        if len(history) < 20:
            return 0.55  # Fallback

        # Convert tick data to 1-minute OHLC bars
        bars = []
        i = 0
        while i < len(history):
            window = history[i:min(i+60, len(history))]  # ~60 ticks/minute
            if len(window) < 2:
                i += 60
                continue

            prices = [t.price for t in window]
            bars.append({
                'open': prices[0],
                'high': max(prices),
                'low': min(prices),
                'close': prices[-1]
            })
            i += 60

        if len(bars) < 15:
            return 0.55

        n = len(bars)
        k = 0.34 / (1.34 + (n+1)/(n-1))

        # Overnight volatility (open-to-close jumps)
        overnight = sum((math.log(bars[i]['open']/bars[i-1]['close']))**2 
                        for i in range(1, n)) / (n-1) if n > 1 else 0

        # Rogers-Satchell intraday volatility
        rs = sum(
            math.log(bars[i]['high']/bars[i]['close']) * 
            math.log(bars[i]['high']/bars[i]['open']) +
            math.log(bars[i]['low']/bars[i]['close']) * 
            math.log(bars[i]['low']/bars[i]['open'])
            for i in range(n)
        ) / n

        # Yang-Zhang combined estimator
        sigma_squared = overnight + k * overnight + (1-k) * rs

        # Annualize (minutes to years)
        annual_vol = math.sqrt(max(0, sigma_squared) * 365 * 24 * 60)

        # Clamp to reasonable bounds
        return max(0.20, min(2.00, annual_vol))

    @staticmethod
    def implied_from_market(up_price: float, down_price: float, 
                           strike: float, oracle: float, time_sec: float) -> float:
        """
        Reverse-engineer implied volatility from market prices.
        Market aggregates all trader expectations - often more accurate than historical.
        """
        if up_price <= 0 or down_price <= 0 or strike <= 0 or oracle <= 0:
            return 0.55

        # Market-implied probability
        p_market = up_price / (up_price + down_price)

        # Edge cases
        t_years = time_sec / (365.25 * 24 * 3600)
        if t_years <= 0.0001:  # Less than 3 seconds
            return 0.55

        # Binary search for volatility that matches market price
        vol_low, vol_high = 0.10, 2.00
        for _ in range(20):  # 20 iterations = 0.0001 precision
            vol_mid = (vol_low + vol_high) / 2
            sigma = vol_mid * math.sqrt(t_years)

            if sigma <= 0:
                break

            # Black-Scholes style probability
            d2 = math.log(oracle / strike) / sigma
            p_model = FVEngine.ncdf(d2)

            if p_model < p_market:
                vol_low = vol_mid
            else:
                vol_high = vol_mid

        implied = (vol_low + vol_high) / 2
        return max(0.20, min(1.50, implied))

    @staticmethod
    def calculate_vwap(history: List[PriceTick]) -> float:
        """
        Calculate Volume-Weighted Average Price.
        Uses tick frequency as volume proxy (more ticks = more volume).
        """
        if len(history) < 10:
            return 0

        total_pv = 0
        total_v = 0

        for i, tick in enumerate(history):
            # Volume proxy: price change magnitude * 100
            if i == 0:
                vol = 1.0
            else:
                vol = abs(tick.price - history[i-1].price) * 100 + 0.1

            total_pv += tick.price * vol
            total_v += vol

        return total_pv / total_v if total_v > 0 else history[-1].price

# ============================================================
# 📐 FAIR VALUE ENGINE - 8 MODELS
# ============================================================
class FVEngine:
    """Ensemble of 8 fair value pricing models"""

    @staticmethod
    def ncdf(x: float) -> float:
        """
        Normal CDF approximation (Abramowitz & Stegun).
        Used for probability calculations in option-style models.
        """
        a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
        p = 0.3275911
        sign = 1 if x >= 0 else -1
        x = abs(x)
        t = 1.0 / (1.0 + p * x)
        y = 1.0 - (((((a5*t + a4)*t) + a3)*t + a2)*t + a1)*t * math.exp(-x*x/2)
        return 0.5 * (1.0 + sign * y)

    @staticmethod
    def distance(price: float, strike: float, time_sec: float, vol: float) -> FairValue:
        """
        MODEL 1: Black-Scholes style distance model.
        Uses lognormal distribution to estimate probability of finishing above strike.
        """
        if price <= 0 or strike <= 0 or time_sec <= 0:
            return FairValue("DISTANCE", 0.5, 0.5, 0.0)

        t_years = time_sec / (365.25 * 24 * 3600)
        sigma = vol * math.sqrt(t_years)

        if sigma <= 0:
            # Deterministic: either definitely above or below
            p = 1.0 if price > strike else 0.0
            return FairValue("DISTANCE", p, 1-p, 1.0)

        # d2 from Black-Scholes (drift-adjusted)
        d2 = math.log(price / strike) / sigma
        p = FVEngine.ncdf(d2)
        p = max(0.001, min(0.999, p))

        return FairValue("DISTANCE", p, 1-p, abs(p - 0.5) * 2)

    @staticmethod
    def time_decay(price: float, strike: float, time_sec: float) -> FairValue:
        """
        MODEL 2: Time decay model with sigmoid certainty.
        As expiry approaches, probability converges to 1 (if above) or 0 (if below).
        """
        if price <= 0 or strike <= 0:
            return FairValue("TIME_DECAY", 0.5, 0.5, 0.0)

        # Certainty increases exponentially as time runs out
        k = 30.0
        certainty = 1.0 - math.exp(-k / max(time_sec, 0.1))

        # Sigmoid steepness increases with certainty
        steepness = 0.00005 + certainty * 0.0005
        base = 1.0 / (1.0 + math.exp(-steepness * (price - strike)))

        # Adjust probability based on time remaining
        if price > strike:
            p = base + (1.0 - base) * certainty * 0.8
        else:
            p = base * (1.0 - certainty * 0.8)

        p = max(0.001, min(0.999, p))
        return FairValue("TIME_DECAY", p, 1-p, certainty)

    @staticmethod
    def momentum(price: float, strike: float, time_sec: float, 
                 history: List[PriceTick], vol: float) -> FairValue:
        """
        MODEL 3: Price momentum model with linear regression.
        Trends tend to persist over 5-15 minute timeframes.
        """
        base = FVEngine.distance(price, strike, time_sec, vol)

        if len(history) < 10:
            return FairValue("MOMENTUM", base.fv_up, base.fv_down, base.confidence * 0.5)

        # Use last 30 ticks for trend calculation
        prices = [t.price for t in history[-30:]]
        n = len(prices)

        if n < 5:
            return base

        # Linear regression slope
        x_sum = sum(range(n))
        y_sum = sum(prices)
        xy_sum = sum(i * p for i, p in enumerate(prices))
        x2_sum = sum(i * i for i in range(n))

        denom = n * x2_sum - x_sum * x_sum
        if denom == 0:
            return base

        slope = (n * xy_sum - x_sum * y_sum) / denom
        slope_sec = slope * 10  # Scale to per-second (assuming ~10 ticks/sec)

        # Adjust probability based on momentum direction
        adj = 0.0
        if slope_sec > 0 and price > strike:
            adj = min(0.10, slope_sec / 50)
        elif slope_sec < 0 and price < strike:
            adj = max(-0.10, slope_sec / 50)

        p = max(0.001, min(0.999, base.fv_up + adj))
        return FairValue("MOMENTUM", p, 1-p, base.confidence * 0.85)

    @staticmethod
    def oracle_lag(spot: float, oracle: float, strike: float, time_sec: float, 
                   lag_sec: float, vol: float) -> FairValue:
        """
        MODEL 4: Oracle lag arbitrage model.
        Chainlink oracle lags spot by 3-10 seconds. Predict where it will settle.
        """
        if oracle <= 0 or strike <= 0:
            return FVEngine.distance(spot, strike, time_sec, vol)

        gap = spot - oracle

        # Estimate how much of the gap will close during lag period
        if lag_sec > 0 and time_sec > 0:
            catch_up = min(1.0, lag_sec / 5.0) * 0.8  # 80% catch-up over 5 seconds
            estimated_oracle = oracle + gap * catch_up
        else:
            estimated_oracle = oracle

        # Calculate probability with estimated oracle
        base = FVEngine.distance(estimated_oracle, strike, time_sec, vol)

        # Confidence penalty for excessive lag (data becomes stale)
        penalty = min(0.3, lag_sec / 10)

        return FairValue("ORACLE_LAG", base.fv_up, base.fv_down, 
                        max(0.1, base.confidence - penalty))

    @staticmethod
    def order_flow(price: float, strike: float, time_sec: float, 
                   ofi: float, vol: float) -> FairValue:
        """
        MODEL 5: Order Flow Imbalance model.
        Aggressive buy orders predict short-term upward price pressure.
        """
        base = FVEngine.distance(price, strike, time_sec, vol)

        # OFI effect strongest in last 5 minutes
        time_weight = 1.0 - min(1.0, time_sec / 300)
        ofi_effect = ofi * 0.15 * time_weight  # Cap at ±15%

        p = base.fv_up + ofi_effect
        p = max(0.001, min(0.999, p))

        # Higher confidence when OFI signal is strong
        conf = base.confidence * (1 + abs(ofi) * 0.3)

        return FairValue("ORDER_FLOW", p, 1-p, min(1.0, conf))

    @staticmethod
    def vwap_reversion(price: float, strike: float, vwap: float, 
                       time_sec: float) -> FairValue:
        """
        MODEL 6: VWAP mean reversion model.
        Prices tend to revert to VWAP over 5-15 minute windows.
        """
        if vwap == 0:
            return FairValue("VWAP", 0.5, 0.5, 0.0)

        # Calculate relative distances
        price_to_vwap = (price - vwap) / vwap
        strike_to_vwap = (strike - vwap) / vwap

        # Reversion logic
        if strike > vwap and price < vwap:
            # Strike above VWAP, price below: expect rise toward VWAP
            boost = min(0.20, abs(strike_to_vwap) * 10)
            p = 0.50 + boost
        elif strike < vwap and price > vwap:
            # Strike below VWAP, price above: expect fall toward VWAP
            boost = min(0.20, abs(strike_to_vwap) * 10)
            p = 0.50 - boost
        else:
            p = 0.50

        # Confidence increases with time (more room for reversion)
        conf = min(1.0, time_sec / 300)

        return FairValue("VWAP", max(0.001, min(0.999, p)), 1-p, conf)

    @staticmethod
    def jump_continuation(jump: JumpState, strike: float, oracle: float, 
                         time_sec: float) -> FairValue:
        """
        MODEL 7: Jump-diffusion continuation model.
        Large price jumps exhibit momentum over 30-120 seconds.
        Critical for news events (Fed announcements, etc.)
        """
        if jump.last_jump_time == 0:
            return FairValue("JUMP", 0.5, 0.5, 0.0)

        # Time since jump
        now = time.time() * 1000
        seconds_since = (now - jump.last_jump_time) / 1000

        # Exponential decay with 60-second half-life
        decay = math.exp(-seconds_since / 60)

        # Boost probability if jump direction aligns with outcome
        if jump.jump_direction > 0 and oracle > strike:
            boost = jump.jump_magnitude * 20 * decay  # 1% jump = +20% prob
            p = 0.50 + min(0.30, boost)
        elif jump.jump_direction < 0 and oracle < strike:
            boost = jump.jump_magnitude * 20 * decay
            p = 0.50 + min(0.30, boost)
        else:
            p = 0.50

        # Confidence proportional to jump size and recency
        conf = min(1.0, jump.jump_magnitude * 100 * decay)

        return FairValue("JUMP", max(0.001, min(0.999, p)), 1-p, conf)

    @staticmethod
    def futures_lead(futures_price: float, spot_price: float, oracle_price: float,
                    strike: float, time_sec: float, vol: float) -> FairValue:
        """
        MODEL 8: Futures lead-lag relationship.
        Binance Futures leads spot by 1-3 seconds. Use to predict oracle movement.
        """
        if futures_price == 0 or spot_price == 0 or oracle_price == 0:
            return FairValue("FUTURES", 0.5, 0.5, 0.0)

        # Futures-spot basis (premium/discount)
        basis = (futures_price - spot_price) / spot_price

        # Project where spot will move (70% of basis typically closes)
        projected_spot = spot_price * (1 + basis * 0.7)

        # Estimate where oracle will be after lag period
        projected_oracle = oracle_price + (projected_spot - spot_price) * 0.5

        # Calculate probability with projected oracle
        base = FVEngine.distance(projected_oracle, strike, time_sec, vol)

        # Confidence based on basis strength
        conf = min(1.0, abs(basis) * 1000)  # 0.1% basis = 0.1 confidence

        return FairValue("FUTURES", base.fv_up, base.fv_down, conf)

    @staticmethod
    def ensemble(spot: float, oracle: float, strike: float, time_sec: float, 
                 history: List[PriceTick], lag_sec: float, 
                 futures: float) -> Tuple[FairValue, List[FairValue]]:
        """
        Ensemble combiner with adaptive weighting.
        Weights adjust based on market conditions and time remaining.
        """
        # Calculate analytics
        vol_hist = VolatilityEngine.yang_zhang(history)
        vwap = VolatilityEngine.calculate_vwap(history)
        ofi = core.ofi_state.trade_imbalance
        jump = core.jump_state

        # Generate all model estimates
        estimates = [
            FVEngine.distance(oracle, strike, time_sec, vol_hist),
            FVEngine.time_decay(oracle, strike, time_sec),
            FVEngine.momentum(oracle, strike, time_sec, history, vol_hist),
            FVEngine.oracle_lag(spot, oracle, strike, time_sec, lag_sec, vol_hist),
            FVEngine.order_flow(oracle, strike, time_sec, ofi, vol_hist),
            FVEngine.vwap_reversion(oracle, strike, vwap, time_sec),
            FVEngine.jump_continuation(jump, strike, oracle, time_sec),
            FVEngine.futures_lead(futures, spot, oracle, strike, time_sec, vol_hist),
        ]

        # Base weights
        w = {
            'DISTANCE': 0.15,
            'TIME_DECAY': 0.15,
            'MOMENTUM': 0.10,
            'ORACLE_LAG': 0.20,
            'ORDER_FLOW': 0.15,
            'VWAP': 0.10,
            'JUMP': 0.10,
            'FUTURES': 0.05,
        }

        # ADAPTIVE WEIGHTING based on market conditions

        # Near expiry: certainty dominates
        if time_sec < 60:
            w['TIME_DECAY'] = 0.30
            w['ORDER_FLOW'] = 0.25
            w['DISTANCE'] = 0.10

        # High oracle lag: lag model and futures lead become critical
        if lag_sec > 5:
            w['ORACLE_LAG'] = 0.35
            w['FUTURES'] = 0.15
            w['DISTANCE'] = 0.10

        # Strong order flow: microstructure signal dominates
        if abs(ofi) > 0.5:
            w['ORDER_FLOW'] = 0.30
            w['FUTURES'] = 0.10

        # Recent jump: jump continuation is strongest signal
        if jump.last_jump_time > 0:
            seconds_since = (time.time() * 1000 - jump.last_jump_time) / 1000
            if seconds_since < 30:
                w['JUMP'] = 0.35
                w['MOMENTUM'] = 0.15

        # Renormalize weights to sum to 1.0
        total_w = sum(w.values())
        w = {k: v/total_w for k, v in w.items()}

        # Calculate confidence-weighted ensemble
        weighted_conf = sum(w.get(e.model, 0) * e.confidence for e in estimates)
        if weighted_conf == 0:
            weighted_conf = 1

        fv_up = sum(e.fv_up * w.get(e.model, 0) * e.confidence 
                    for e in estimates) / weighted_conf
        fv_up = max(0.001, min(0.999, fv_up))

        conf = sum(w.get(e.model, 0) * e.confidence for e in estimates)

        return FairValue("ENSEMBLE", fv_up, 1 - fv_up, conf), estimates

# ============================================================
# 📈 SIGNAL GENERATOR
# ============================================================
class SignalGen:
    """Generate trading signals from fair value estimates"""

    @staticmethod
    def net_edge(fv: float, price: float) -> float:
        """
        Calculate net edge after fees.
        Polymarket charges 2% on notional, which varies with price.
        """
        if price <= 0:
            return 0.0
        gross = fv - price
        fee = fv * POLY_FEE * (1.0 - price)  # Fee structure
        return gross - fee

    @staticmethod
    def generate(ensemble: FairValue, prices: Dict) -> List[Signal]:
        """
        Generate buy signals when fair value exceeds ask price.
        Only signals above MIN_EDGE threshold to filter noise.
        """
        signals = []

        # UP side signal
        up_ask = prices.get('up_ask', 0)
        if up_ask > 0:
            net = SignalGen.net_edge(ensemble.fv_up, up_ask)
            if net >= MIN_EDGE:
                conf = "HIGH" if net > 0.06 else "MEDIUM" if net > 0.035 else "LOW"
                signals.append(Signal(
                    "UP", "BUY", up_ask, ensemble.fv_up,
                    ensemble.fv_up - up_ask, net,
                    prices.get('up_ask_size', 0), conf,
                    f"FV ${ensemble.fv_up:.3f} > Ask ${up_ask:.3f}"
                ))

        # DOWN side signal
        down_ask = prices.get('down_ask', 0)
        if down_ask > 0:
            net = SignalGen.net_edge(ensemble.fv_down, down_ask)
            if net >= MIN_EDGE:
                conf = "HIGH" if net > 0.06 else "MEDIUM" if net > 0.035 else "LOW"
                signals.append(Signal(
                    "DOWN", "BUY", down_ask, ensemble.fv_down,
                    ensemble.fv_down - down_ask, net,
                    prices.get('down_ask_size', 0), conf,
                    f"FV ${ensemble.fv_down:.3f} > Ask ${down_ask:.3f}"
                ))

        # Sort by net edge (best signals first)
        signals.sort(key=lambda s: s.net_edge, reverse=True)
        return signals

# ============================================================
# 📡 WEBSOCKET CLIENTS
# ============================================================
class BinanceFuturesWS:
    """Binance Futures WebSocket - leading price indicator"""

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        "wss://fstream.binance.com/ws/btcusdt@aggTrade",
                        on_message=lambda ws, m: self._msg(m)
                    )
                    ws.run_forever(ping_interval=30)
                except Exception as e:
                    print(f"[FUTURES] Connection error: {e}")
                time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _msg(self, msg):
        try:
            d = json.loads(msg)
            price = float(d['p'])
            ts = int(d['T'])
            is_buy = d['m'] == False  # m=true means market sell
            size = float(d.get('q', 0))
            core.update_futures(price, ts, is_buy, size)
        except Exception as e:
            pass


class BinanceSpotWS:
    """Binance Spot WebSocket - tracks Chainlink oracle source"""

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        "wss://stream.binance.com:9443/ws/btcusdt@trade",
                        on_message=lambda ws, m: self._msg(m)
                    )
                    ws.run_forever(ping_interval=30)
                except Exception as e:
                    print(f"[SPOT] Connection error: {e}")
                time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _msg(self, msg):
        try:
            d = json.loads(msg)
            core.update_spot(float(d['p']), int(d['T']))
        except Exception as e:
            pass


class PolyDataWS:
    """
    Polymarket Live Data WebSocket.
    Subscribes to:
    1. crypto_prices (Binance relay via Polymarket)
    2. crypto_prices_chainlink (Settlement oracle - THE IMPORTANT ONE)
    """

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        "wss://ws-live-data.polymarket.com",
                        on_open=self._open,
                        on_message=self._msg
                    )
                    ws.run_forever(ping_interval=30)
                except Exception as e:
                    print(f"[POLY] Connection error: {e}")
                time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _open(self, ws):
        """Subscribe to both price feeds on connection"""
        # Binance relay
        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices",
                "type": "*",
                "filters": '{"symbol":"btcusdt"}'
            }]
        }))

        # Chainlink oracle (settlement price)
        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": '{"symbol":"btc/usd"}'
            }]
        }))
        print("[POLY] Subscribed to price feeds")

    def _msg(self, ws, msg):
        try:
            d = json.loads(msg)

            if d.get('type') == 'update':
                topic = d.get('topic')
                payload = d.get('payload', {})
                symbol = payload.get('symbol', '')
                value = payload.get('value')
                ts = payload.get('timestamp')

                if value is None or ts is None:
                    return

                # Binance relay
                if topic == 'crypto_prices' and symbol == 'btcusdt':
                    core.update_relay(float(value), int(ts))

                # Chainlink oracle (THIS IS THE SETTLEMENT PRICE)
                elif topic == 'crypto_prices_chainlink' and symbol == 'btc/usd':
                    core.update_oracle(float(value), int(ts))

        except Exception as e:
            pass


class PolyClobWS:
    """Polymarket CLOB (Central Limit Order Book) WebSocket"""

    def __init__(self):
        self.ws = None
        self.subscribed: set = set()
        self.token_map: Dict[str, str] = {}  # ✅ NEW: Cache asset_id -> side mapping

    def start(self):
        def run():
            while True:
                try:
                    self.ws = websocket.WebSocketApp(
                        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                        on_open=lambda ws: self._sub(),
                        on_message=self._msg,
                        on_close=lambda ws, c, m: self._on_close()
                    )
                    self.ws.run_forever(ping_interval=30)
                except Exception as e:
                    print(f"[CLOB] Connection error: {e}")
                time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _on_close(self):
        """Clear subscription state on disconnect"""
        self.subscribed.clear()
        print("[CLOB] Connection closed, cleared subscriptions")

    def _sub(self):
        """
        Subscribe to orderbook feeds for UP and DOWN tokens.
        FIXED: Now checks for valid tokens before subscribing.
        """
        tokens = []

        if market.up_token and market.up_token not in self.subscribed:
            tokens.append(market.up_token)
        if market.down_token and market.down_token not in self.subscribed:
            tokens.append(market.down_token)

        if not tokens:
            return  # No tokens to subscribe

        if not self.ws:
            print("[ERROR] CLOB WebSocket not connected")
            return

        try:
            msg = json.dumps({
                "type": "subscribe",
                "channel": "market",
                "assets_ids": tokens
            })
            self.ws.send(msg)
            self.subscribed.update(tokens)
            print(f"[CLOB] ✓ Subscribed to {len(tokens)} tokens: {tokens[0][:8]}...")
        except Exception as e:
            print(f"[ERROR] CLOB subscription failed: {e}")

    def resub(self):
        """
        Clear and resubscribe for new market window.
        FIXED: Now waits for tokens to be fetched before subscribing.
        """
        print(f"[CLOB] Resubscribing for new window...")
        self.subscribed.clear()

        # Wait up to 1 second for tokens to be fetched
        for _ in range(10):
            if market.up_token and market.down_token:
                break
            time.sleep(0.1)

        if market.up_token:
            self._sub()
        else:
            print("[WARN] Tokens not available yet, will retry")

    def _msg(self, ws, msg):
        """Process orderbook messages"""
        try:
            d = json.loads(msg)
            evt = d.get('event_type')

            # Log unknown events for debugging
            if evt not in ['book', 'price_change', 'last_trade_price']:
                # print(f"[CLOB] Unknown event: {evt}")
                return

            # Full book snapshot
            if evt == 'book':
                asset = d.get('asset_id')
                bids = d.get('bids', [])
                asks = d.get('asks', [])

                if asset == market.up_token:
                    core.set_book('UP', bids, asks)
                    print(f"[CLOB] ✓ UP book: {len(bids)} bids, {len(asks)} asks")
                elif asset == market.down_token:
                    core.set_book('DOWN', bids, asks)
                    print(f"[CLOB] ✓ DOWN book: {len(bids)} bids, {len(asks)} asks")

            # Incremental level updates
            elif evt == 'price_change':
                for c in d.get('price_changes', []):
                    asset = c.get('asset_id')

                    # ✅ Use cached mapping
                    side = self.token_map.get(asset)

                    if side:
                        core.update_level(side, c.get('price'), 
                                        float(c.get('size', 0)), c.get('side'))
                        core.update_level(side, c.get('price'), 
                                        float(c.get('size', 0)), c.get('side'))

        except Exception as e:
            print(f"[ERROR] CLOB message parse: {e}")

clob_ws = PolyClobWS()

# ============================================================
# 🎨 UI RENDERING
# ============================================================
def fmt_timer(sec: float) -> Text:
    """Format countdown timer with color coding"""
    m, s = int(sec // 60), int(sec % 60)
    if sec > 120:
        style = "bold green"
    elif sec > 60:
        style = "bold yellow"
    elif sec > 30:
        style = "bold red"
    else:
        style = "bold red blink"
    return Text(f"{m:02d}:{s:02d}", style=style)


def render_prices() -> Panel:
    """Render all 4 price feed sources with latency"""
    table = Table(box=box.SIMPLE, expand=True)
    table.add_column("SOURCE", style="cyan", width=18)
    table.add_column("PRICE", justify="right", width=14)
    table.add_column("LAG", justify="right", width=10)
    table.add_column("", width=3)

    now = int(time.time() * 1000)

    def row(name: str, price: float, ts: int, is_oracle: bool = False):
        """Helper to format a price feed row"""
        if ts == 0:
            lag_str, lag_style = "-", "dim"
            status = Text("○", style="red")
        else:
            lag = now - ts
            if lag < 100:
                lag_str, lag_style = f"{lag}ms", "green"
            elif lag < 500:
                lag_str, lag_style = f"{lag}ms", "yellow"
            elif lag < 3000:
                lag_str, lag_style = f"{lag}ms", "yellow"
            else:
                lag_str, lag_style = f"{lag/1000:.1f}s", "red"
            status = Text("●", style="green")

        price_str = f"${price:,.2f}" if price > 0 else "-"

        # Highlight oracle row
        if is_oracle:
            name_text = Text(name, style="bold cyan")
            price_text = Text(price_str, style="bold cyan")
        else:
            name_text = Text(name)
            price_text = Text(price_str)

        table.add_row(name_text, price_text, Text(lag_str, style=lag_style), status)

    # 4 price feeds
    row("⚡ Binance Futures", core.futures_price, core.futures_ts)
    row("📊 Binance Spot", core.spot_price, core.spot_ts)
    row("🔄 Poly Relay", core.relay_price, core.relay_ts)
    row("🔮 CHAINLINK", core.oracle_price, core.oracle_ts, is_oracle=True)

    # Analytics
    analytics = Text()

    # Futures-spot basis
    if core.futures_price > 0 and core.spot_price > 0:
        basis = core.futures_price - core.spot_price
        analytics.append(f"\nBasis (Fut-Spot): ", style="dim")
        analytics.append(f"${basis:+.2f}\n", style="yellow")

    # Oracle gap and lag
    if core.oracle_price > 0 and core.spot_price > 0:
        gap = core.oracle_price - core.spot_price
        gap_style = "green" if abs(gap) < 10 else "yellow" if abs(gap) < 50 else "red"
        analytics.append(f"Oracle Gap: ", style="dim")
        analytics.append(f"${gap:+.2f}", style=gap_style)

        lag = core.oracle_lag_sec()
        if lag > 3:
            analytics.append(f" ⚠️ {lag:.1f}s lag", style="red")

    # Order flow
    ofi = core.ofi_state.trade_imbalance
    if abs(ofi) > 0.1:
        ofi_style = "green" if ofi > 0 else "red"
        analytics.append(f"\nOrder Flow: ", style="dim")
        analytics.append(f"{ofi:+.2f}", style=ofi_style)

    content = Layout()
    content.split(
        Layout(table, ratio=3),
        Layout(analytics, ratio=1)
    )

    return Panel(content, title="📡 PRICE FEEDS (4 Sources)", border_style="cyan")


def render_market() -> Panel:
    """Render market window status and strike info"""
    content = Text()

    content.append("🎯 MARKET\n\n", style="bold blue underline")
    content.append(f"Slug: {market.slug}\n", style="dim")

    content.append("\n⏱️ TIME: ", style="white")
    content.append_text(fmt_timer(market.time_left()))

    if market.strike > 0:
        content.append(f"\n\n⚡ STRIKE: ", style="white")
        content.append(f"${market.strike:,.2f}", style="bold cyan")
        if market.strike_captured:
            content.append(" ✓", style="green")

        side, margin = market.outcome_status()
        if side == "UP":
            content.append(f"\n📈 STATUS: UP +${margin:.2f}", style="bold green")
        elif side == "DOWN":
            content.append(f"\n📉 STATUS: DOWN -${margin:.2f}", style="bold red")

    content.append(f"\n\n{market.status}", style="dim")

    return Panel(content, title="MARKET", border_style="blue")


def render_fv(ensemble: FairValue, estimates: List[FairValue]) -> Panel:
    """Render fair value estimates from all models"""
    table = Table(box=box.SIMPLE, expand=True)
    table.add_column("MODEL", style="cyan", width=12)
    table.add_column("FV UP", justify="right", style="green", width=8)
    table.add_column("FV DOWN", justify="right", style="red", width=8)
    table.add_column("CONF", justify="right", width=6)

    for e in estimates:
        table.add_row(e.model, f"${e.fv_up:.3f}", f"${e.fv_down:.3f}", f"{e.confidence:.0%}")

    # Highlight ensemble result
    table.add_row(
        Text("ENSEMBLE", style="bold"),
        Text(f"${ensemble.fv_up:.3f}", style="bold green"),
        Text(f"${ensemble.fv_down:.3f}", style="bold red"),
        Text(f"{ensemble.confidence:.0%}", style="bold")
    )

    return Panel(table, title="📐 FAIR VALUE (8 Models)", border_style="magenta")


def render_book() -> Panel:
    """Render orderbook best bid/ask"""
    p = core.best_prices()

    content = Text()
    content.append("UP (YES)\n", style="bold green underline")
    content.append(f"  Bid: ${p['up_bid']:.4f}  Ask: ${p['up_ask']:.4f}\n", style="green")

    content.append("\nDOWN (NO)\n", style="bold red underline")
    content.append(f"  Bid: ${p['down_bid']:.4f}  Ask: ${p['down_ask']:.4f}\n", style="red")

    # Arbitrage check
    total = p['up_ask'] + p['down_ask']
    content.append(f"\n📊 SUM: ${total:.4f} ", style="white")
    if total > 0 and total < 0.995:
        content.append(f"(ARB ${1-total:.4f}!)", style="bold green")

    return Panel(content, title="📚 ORDERBOOK", border_style="yellow")


def render_signals(signals: List[Signal]) -> Panel:
    """Render top trading signals"""
    if not signals:
        return Panel(Text("No signals (edge < 2.5%)", style="dim"), 
                    title="📈 SIGNALS", border_style="dim")

    content = Text()
    for i, s in enumerate(signals[:3]):  # Top 3 signals
        side_style = "bold green" if s.side == "UP" else "bold red"
        content.append(f"#{i+1} {s.action} ", style="bold")
        content.append(f"{s.side}\n", style=side_style)
        content.append(f"   ${s.price:.4f} → FV ${s.fair_value:.3f}\n", style="white")
        content.append(f"   Edge: {s.edge:.1%} gross, ", style="white")

        edge_style = "bold green" if s.net_edge > 0.05 else "green"
        content.append(f"{s.net_edge:.1%} net\n", style=edge_style)
        content.append(f"   {s.confidence} | Size: {s.size:,.0f}\n\n", style="dim")

    border = "bold green" if signals[0].net_edge > 0.035 else "yellow"
    return Panel(content, title="🎯 SIGNALS", border_style=border)


def render_stats() -> Panel:
    """Render connection and message statistics"""
    content = Text()
    content.append(f"Messages: {core.msg_count:,}\n", style="dim")
    content.append("WS: ", style="dim")

    # Connection status indicators
    content.append("F", style="green" if core.connected['futures'] else "red")
    content.append("S", style="green" if core.connected['spot'] else "red")
    content.append("R", style="green" if core.connected['relay'] else "red")
    content.append("O", style="green" if core.connected['oracle'] else "red")
    content.append("C", style="green" if core.connected['clob'] else "red")

    # Volatility info
    if len(core.spot_history) > 20:
        history = core.get_history(60)
        vol = VolatilityEngine.yang_zhang(history)
        content.append(f"\nVol: {vol:.1%}", style="yellow")

    return Panel(content, title="STATS", border_style="dim")


def dashboard() -> Layout:
    """
    Main dashboard renderer.
    FIXED: Correct order - fetch tokens BEFORE subscribing to CLOB.
    """
    # Lifecycle management
    new_window = market.lifecycle()  # Detect new window
    market.fetch_tokens()            # ✅ Fetch tokens FIRST

    if new_window:                   # ✅ THEN subscribe with valid tokens
        clob_ws.resub()

    # Subscribe if tokens exist but not subscribed yet
    if market.up_token and market.up_token not in clob_ws.subscribed:
        clob_ws._sub()

    # Calculate fair values

    # ✅ Health check for stale subscriptions
    if market.up_token and market.up_token in clob_ws.subscribed:
        prices = core.best_prices()
        if prices['up_ask'] == 0 and market.time_left() < 890:
            print("[WARN] ⚠️  No orderbook data, forcing resub...")
            clob_ws.resub()
        clob_ws._sub()

    # Calculate fair values

    # ✅ Health check for stale subscriptions
    if market.up_token and market.up_token in clob_ws.subscribed:
        prices = core.best_prices()
        if prices['up_ask'] == 0 and market.time_left() < 890:
            print("[WARN] ⚠️  No orderbook data, forcing resub...")
            clob_ws.resub()
        clob_ws._sub()

    # Calculate fair values

    # ✅ Health check for stale subscriptions
    if market.up_token and market.up_token in clob_ws.subscribed:
        prices = core.best_prices()
        if prices['up_ask'] == 0 and market.time_left() < 890:
            print("[WARN] ⚠️  No orderbook data, forcing resub...")
            clob_ws.resub()
        clob_ws._sub()

    # Calculate fair values
    ensemble = FairValue("ENSEMBLE", 0.5, 0.5, 0.0)
    estimates = []
    signals = []

    if market.strike > 0 and core.oracle_price > 0:
        history = core.get_history(60)

        ensemble, estimates = FVEngine.ensemble(
            core.spot_price, 
            core.oracle_price, 
            market.strike,
            market.time_left(), 
            history, 
            core.oracle_lag_sec(), 
            core.futures_price
        )

        signals = SignalGen.generate(ensemble, core.best_prices())

    # Build layout
    layout = Layout()

    # Header
    header = Text()
    header.append("⚡ BTC 15-MIN FAIR VALUE SYSTEM v2.0 ", style="bold cyan")
    header.append(datetime.now().strftime("%H:%M:%S"), style="dim")

    layout.split(
        Layout(Panel(header, box=box.MINIMAL), size=3),
        Layout(name="main")
    )

    # Three-column layout
    layout["main"].split_row(
        Layout(name="left"),
        Layout(name="center"),
        Layout(name="right")
    )

    layout["left"].split(
        Layout(render_market()),
        Layout(render_stats()),
    )

    layout["center"].split(
        Layout(render_prices()),
        Layout(render_book()),
    )

    layout["right"].split(
        Layout(render_fv(ensemble, estimates)),
        Layout(render_signals(signals)),
    )

    return layout

# ============================================================
# 🚀 MAIN
# ============================================================
def main():
    """Initialize and run the trading system"""
    console = Console()

    console.print("\n[bold cyan]⚡ BTC 15-MIN FAIR VALUE SYSTEM v2.0[/bold cyan]")
    console.print("[dim]Enhanced with 8 models, dynamic volatility, and microstructure signals[/dim]\n")

    console.print("[yellow]Starting feeds...[/yellow]")
    BinanceFuturesWS().start()
    BinanceSpotWS().start()
    PolyDataWS().start()
    clob_ws.start()

    console.print("[green]✓ All WebSockets started[/green]")

    # Initialize market
    market.lifecycle()
    market.fetch_tokens()

    console.print(f"[green]✓ Market: {market.slug}[/green]")

    time.sleep(2)
    console.print("[bold green]Starting dashboard...[/bold green]\n")

    try:
        with Live(dashboard(), refresh_per_second=10, screen=True) as live:
            while True:
                live.update(dashboard())
                time.sleep(0.1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")


if __name__ == "__main__":
    main()