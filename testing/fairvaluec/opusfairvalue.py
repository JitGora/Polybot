"""
================================================================================
⚡ POLYMARKET BTC 15-MIN FAIR VALUE SYSTEM v2.0 - COMPLETE
================================================================================

ENHANCEMENTS:
1. Real-time volatility (EWMA + realized)
2. Order imbalance signals  
3. Near-expiry jump risk model
4. Oracle update prediction
5. Historical calibration tracking
6. FIXED event transition bug

"""

import json
import time
import threading
import math
import re
import os
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
WINDOW_SIZE = 900
POLY_FEE = 0.02
MIN_EDGE = 0.020
GAMMA_API = "https://gamma-api.polymarket.com/events"
HISTORY_FILE = "fv_history.json"

EWMA_ALPHA = 0.06
MIN_VOL = 0.15
MAX_VOL = 2.00
DEFAULT_VOL = 0.50

# ============================================================
# 📊 DATA STRUCTURES
# ============================================================
@dataclass
class PriceTick:
    price: float
    timestamp_ms: int

@dataclass
class OracleUpdate:
    price: float
    timestamp_ms: int
    price_change: float = 0.0
    time_since_last: float = 0.0

@dataclass
class FairValue:
    model: str
    fv_up: float
    fv_down: float
    confidence: float
    details: Dict = field(default_factory=dict)

@dataclass
class Signal:
    side: str
    action: str
    price: float
    fair_value: float
    edge: float
    net_edge: float
    size: float
    confidence: str
    reasons: List[str] = field(default_factory=list)

@dataclass
class WindowResult:
    slug: str
    strike: float
    final_oracle: float
    outcome: str
    fv_at_60s: float = 0.5
    fv_at_30s: float = 0.5
    fv_at_10s: float = 0.5

# ============================================================
# 🧱 DATA CORE
# ============================================================
class DataCore:
    def __init__(self):
        self.lock = threading.RLock()
        
        # Prices
        self.futures_price = 0.0
        self.futures_ts = 0
        self.spot_price = 0.0
        self.spot_ts = 0
        self.relay_price = 0.0
        self.relay_ts = 0
        self.oracle_price = 0.0
        self.oracle_ts = 0
        
        # History
        self.spot_history: Deque[PriceTick] = deque(maxlen=1000)
        self.oracle_history: Deque[OracleUpdate] = deque(maxlen=100)
        self.last_oracle_price = 0.0
        self.last_oracle_ts = 0
        
        # Orderbook
        self.up_bids: Dict[str, float] = {}
        self.up_asks: Dict[str, float] = {}
        self.down_bids: Dict[str, float] = {}
        self.down_asks: Dict[str, float] = {}
        
        # Volatility
        self.ewma_variance = 0.0025
        self.vol_ewma = DEFAULT_VOL
        self.vol_1m = DEFAULT_VOL
        self.vol_5m = DEFAULT_VOL
        self.vol_regime = "NORMAL"
        
        # Imbalance
        self.up_imbalance = 0.0
        self.down_imbalance = 0.0
        self.liquidity_score = 0.0
        
        # Status
        self.connected = {'futures': False, 'spot': False, 'relay': False, 'oracle': False, 'clob': False}
        self.msg_count = 0
        
        # Calibration
        self.fv_snapshots: Dict[int, float] = {}
        self.window_results: List[WindowResult] = []

    def update_futures(self, price: float, ts: int):
        with self.lock:
            self.futures_price = price
            self.futures_ts = ts
            self.connected['futures'] = True
            self.msg_count += 1

    def update_spot(self, price: float, ts: int):
        with self.lock:
            old = self.spot_price
            self.spot_price = price
            self.spot_ts = ts
            self.spot_history.append(PriceTick(price, ts))
            self.connected['spot'] = True
            self.msg_count += 1
            
            # Update EWMA vol
            if old > 0:
                ret = math.log(price / old)
                self.ewma_variance = EWMA_ALPHA * (ret ** 2) + (1 - EWMA_ALPHA) * self.ewma_variance
                ticks_per_year = 10 * 60 * 60 * 24 * 365.25
                self.vol_ewma = math.sqrt(self.ewma_variance * ticks_per_year)
                self.vol_ewma = max(MIN_VOL, min(MAX_VOL, self.vol_ewma))

    def update_relay(self, price: float, ts: int):
        with self.lock:
            self.relay_price = price
            self.relay_ts = ts
            self.connected['relay'] = True
            self.msg_count += 1

    def update_oracle(self, price: float, ts: int):
        with self.lock:
            # Track update pattern
            if self.last_oracle_ts > 0:
                change = price - self.last_oracle_price
                since = (ts - self.last_oracle_ts) / 1000
                self.oracle_history.append(OracleUpdate(price, ts, change, since))
            
            self.last_oracle_price = self.oracle_price
            self.last_oracle_ts = self.oracle_ts
            self.oracle_price = price
            self.oracle_ts = ts
            self.connected['oracle'] = True
            self.msg_count += 1

    def set_book(self, side: str, bids: list, asks: list):
        with self.lock:
            if side == 'UP':
                self.up_bids = {b['price']: float(b['size']) for b in bids}
                self.up_asks = {a['price']: float(a['size']) for a in asks}
            else:
                self.down_bids = {b['price']: float(b['size']) for b in bids}
                self.down_asks = {a['price']: float(a['size']) for a in asks}
            self.connected['clob'] = True
            self.msg_count += 1
            self._update_imbalance()

    def update_level(self, side: str, price: str, size: float, book_side: str):
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
            self._update_imbalance()

    def _update_imbalance(self):
        up_bid_vol = sum(self.up_bids.values())
        up_ask_vol = sum(self.up_asks.values())
        up_total = up_bid_vol + up_ask_vol
        self.up_imbalance = (up_bid_vol - up_ask_vol) / up_total if up_total > 0 else 0
        
        down_bid_vol = sum(self.down_bids.values())
        down_ask_vol = sum(self.down_asks.values())
        down_total = down_bid_vol + down_ask_vol
        self.down_imbalance = (down_bid_vol - down_ask_vol) / down_total if down_total > 0 else 0
        
        total_vol = up_total + down_total
        self.liquidity_score = min(1.0, total_vol / 10000) if total_vol > 0 else 0

    def clear_books(self):
        """Clear orderbooks on window transition"""
        with self.lock:
            self.up_bids.clear()
            self.up_asks.clear()
            self.down_bids.clear()
            self.down_asks.clear()
            self.up_imbalance = 0.0
            self.down_imbalance = 0.0
            self.liquidity_score = 0.0

    def best_prices(self) -> Dict:
        with self.lock:
            up_asks = [float(p) for p in self.up_asks.keys()]
            down_asks = [float(p) for p in self.down_asks.keys()]
            
            up_ask = min(up_asks) if up_asks else 0
            down_ask = min(down_asks) if down_asks else 0
            
            up_size = 0
            if up_ask > 0:
                for p, s in self.up_asks.items():
                    if abs(float(p) - up_ask) < 0.0001:
                        up_size = s
                        break
            
            down_size = 0
            if down_ask > 0:
                for p, s in self.down_asks.items():
                    if abs(float(p) - down_ask) < 0.0001:
                        down_size = s
                        break
            
            return {
                'up_bid': max((float(p) for p in self.up_bids.keys()), default=0),
                'up_ask': up_ask,
                'up_size': up_size,
                'down_bid': max((float(p) for p in self.down_bids.keys()), default=0),
                'down_ask': down_ask,
                'down_size': down_size,
            }

    def oracle_lag_sec(self) -> float:
        if self.oracle_ts == 0:
            return -1
        return (time.time() * 1000 - self.oracle_ts) / 1000

    def spot_history_prices(self, seconds: float) -> List[float]:
        with self.lock:
            cutoff = time.time() * 1000 - seconds * 1000
            return [t.price for t in self.spot_history if t.timestamp_ms > cutoff]

    def oracle_stats(self) -> Dict:
        with self.lock:
            if len(self.oracle_history) < 3:
                return {'avg_interval': 5.0, 'updates_per_min': 12}
            intervals = [u.time_since_last for u in self.oracle_history if u.time_since_last > 0]
            avg = sum(intervals) / len(intervals) if intervals else 5.0
            return {'avg_interval': avg, 'updates_per_min': 60 / avg if avg > 0 else 12}

    def update_realized_vol(self):
        """Update realized volatility from price history"""
        with self.lock:
            for seconds, attr in [(60, 'vol_1m'), (300, 'vol_5m')]:
                prices = self.spot_history_prices(seconds)
                if len(prices) >= 10:
                    returns = [math.log(prices[i]/prices[i-1]) for i in range(1, len(prices)) if prices[i-1] > 0]
                    if len(returns) >= 5:
                        mean = sum(returns) / len(returns)
                        var = sum((r - mean) ** 2 for r in returns) / len(returns)
                        std = math.sqrt(var)
                        per_year = 365.25 * 24 * 3600 / (seconds / len(returns))
                        vol = std * math.sqrt(per_year)
                        setattr(self, attr, max(MIN_VOL, min(MAX_VOL, vol)))
            
            # Update regime
            vol = max(self.vol_ewma, self.vol_1m)
            if vol < 0.30:
                self.vol_regime = "LOW"
            elif vol < 0.60:
                self.vol_regime = "NORMAL"
            elif vol < 1.00:
                self.vol_regime = "HIGH"
            else:
                self.vol_regime = "EXTREME"

    def best_vol(self) -> float:
        return 0.4 * self.vol_ewma + 0.35 * self.vol_1m + 0.25 * self.vol_5m

core = DataCore()

# ============================================================
# 🎯 MARKET MANAGER - FIXED EVENT TRANSITION
# ============================================================
class MarketManager:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'FVBot/2.0'})
        
        self.window_start = 0
        self.slug = ""
        self.strike = 0.0
        self.strike_captured = False
        self.up_token: Optional[str] = None
        self.down_token: Optional[str] = None
        self.question = ""
        self.status = "Init..."
        self.cache: Dict = {}
        self.tokens_fetched = False

    def current_window(self) -> int:
        return (int(time.time()) // WINDOW_SIZE) * WINDOW_SIZE

    def time_left(self) -> float:
        return max(0, (self.window_start + WINDOW_SIZE) - time.time())

    def lifecycle(self, clob_ws) -> bool:
        """Check for window transition. Returns True if new window."""
        current = self.current_window()
        
        if current != self.window_start:
            # === NEW WINDOW ===
            
            # Save previous result
            if self.window_start > 0 and self.strike > 0:
                self._save_result()
            
            # Reset everything
            self.window_start = current
            self.slug = f"btc-updown-15m-{current}"
            self.up_token = None
            self.down_token = None
            self.question = ""
            self.strike_captured = False
            self.tokens_fetched = False
            
            # Capture strike
            if core.oracle_price > 0:
                self.strike = core.oracle_price
                self.strike_captured = True
                self.status = f"Strike: ${self.strike:,.2f}"
            else:
                self.strike = 0.0
                self.status = "Waiting for oracle..."
            
            # Clear orderbooks
            core.clear_books()
            
            # Clear CLOB subscriptions
            if clob_ws:
                clob_ws.clear_subs()
            
            # Clear FV snapshots
            core.fv_snapshots.clear()
            
            return True
        
        # Try capture strike in first 3 seconds
        if not self.strike_captured and core.oracle_price > 0:
            elapsed = time.time() - self.window_start
            if elapsed < 3.0:
                self.strike = core.oracle_price
                self.strike_captured = True
                self.status = f"Strike: ${self.strike:,.2f}"
        
        return False

    def _save_result(self):
        try:
            outcome = "UP" if core.oracle_price > self.strike else "DOWN"
            result = WindowResult(
                slug=self.slug,
                strike=self.strike,
                final_oracle=core.oracle_price,
                outcome=outcome,
                fv_at_60s=core.fv_snapshots.get(60, 0.5),
                fv_at_30s=core.fv_snapshots.get(30, 0.5),
                fv_at_10s=core.fv_snapshots.get(10, 0.5)
            )
            core.window_results.append(result)
            self._persist()
        except:
            pass

    def _persist(self):
        try:
            data = [{'slug': r.slug, 'strike': r.strike, 'oracle': r.final_oracle, 
                     'outcome': r.outcome, 'fv60': r.fv_at_60s, 'fv30': r.fv_at_30s, 
                     'fv10': r.fv_at_10s} for r in core.window_results[-100:]]
            with open(HISTORY_FILE, 'w') as f:
                json.dump(data, f)
        except:
            pass

    def load_history(self):
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, 'r') as f:
                    data = json.load(f)
                for r in data:
                    core.window_results.append(WindowResult(
                        r['slug'], r['strike'], r['oracle'], r['outcome'],
                        r.get('fv60', 0.5), r.get('fv30', 0.5), r.get('fv10', 0.5)
                    ))
        except:
            pass

    def fetch_tokens(self) -> bool:
        if self.up_token and self.tokens_fetched:
            return True
        
        # Check cache
        if self.slug in self.cache:
            c = self.cache[self.slug]
            self.up_token = c['up']
            self.down_token = c['down']
            self.question = c.get('q', '')
            self._extract_strike()
            self.status = "Ready"
            self.tokens_fetched = True
            return True
        
        try:
            r = self.session.get(f"{GAMMA_API}?slug={self.slug}", timeout=10)
            if r.status_code != 200:
                self.status = f"API {r.status_code}"
                return False
            
            data = r.json()
            if not data:
                self.status = "Not found"
                return False
            
            event = data[0]
            mkt = event.get('markets', [{}])[0]
            
            ids = json.loads(mkt.get('clobTokenIds', '[]'))
            outcomes = json.loads(mkt.get('outcomes', '[]'))
            
            if len(ids) < 2:
                self.status = "Bad data"
                return False
            
            if outcomes[0].lower() in ['up', 'yes']:
                self.up_token, self.down_token = ids[0], ids[1]
            else:
                self.up_token, self.down_token = ids[1], ids[0]
            
            self.question = mkt.get('question', '')
            self.cache[self.slug] = {'up': self.up_token, 'down': self.down_token, 'q': self.question}
            self._extract_strike()
            self.status = "Ready"
            self.tokens_fetched = True
            return True
        except Exception as e:
            self.status = f"Err: {str(e)[:20]}"
            return False

    def _extract_strike(self):
        if self.strike > 0:
            return
        if self.question:
            m = re.search(r'\$([\d,]+\.?\d*)', self.question)
            if m:
                try:
                    self.strike = float(m.group(1).replace(',', ''))
                except:
                    pass

    def outcome_status(self) -> Tuple[str, float]:
        if self.strike <= 0 or core.oracle_price <= 0:
            return "UNKNOWN", 0.0
        diff = core.oracle_price - self.strike
        return ("UP", diff) if diff > 0 else ("DOWN", abs(diff))

    def snapshot_fv(self, fv: float):
        t = int(self.time_left())
        if t in [60, 30, 10]:
            core.fv_snapshots[t] = fv

market = MarketManager()

# ============================================================
# 📐 FAIR VALUE ENGINE - ENHANCED
# ============================================================
class FVEngine:
    @staticmethod
    def ncdf(x: float) -> float:
        a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
        p = 0.3275911
        sign = 1 if x >= 0 else -1
        x = abs(x)
        t = 1.0 / (1.0 + p * x)
        y = 1.0 - (((((a5*t + a4)*t) + a3)*t + a2)*t + a1)*t * math.exp(-x*x/2)
        return 0.5 * (1.0 + sign * y)

    @staticmethod
    def distance(price: float, strike: float, time_sec: float, vol: float) -> FairValue:
        if price <= 0 or strike <= 0 or time_sec <= 0:
            return FairValue("DISTANCE", 0.5, 0.5, 0.0)
        
        t_years = time_sec / (365.25 * 24 * 3600)
        sigma = vol * math.sqrt(t_years)
        
        if sigma < 0.0001:
            p = 1.0 if price > strike else 0.0
            return FairValue("DISTANCE", p, 1-p, 1.0, {'d2': 'inf'})
        
        d2 = math.log(price / strike) / sigma
        p = FVEngine.ncdf(d2)
        p = max(0.001, min(0.999, p))
        conf = min(1.0, abs(d2) / 3)
        
        return FairValue("DISTANCE", p, 1-p, conf, {'d2': f"{d2:.2f}", 'vol': f"{vol:.0%}"})

    @staticmethod
    def time_decay(price: float, strike: float, time_sec: float) -> FairValue:
        if price <= 0 or strike <= 0:
            return FairValue("TIME_DECAY", 0.5, 0.5, 0.0)
        
        distance = price - strike
        
        k = 60
        certainty = 1.0 / (1.0 + math.exp(-k / max(time_sec, 0.1) + 2)) if time_sec > 0 else 1.0
        
        steepness = 0.00002 * (1 + certainty * 4)
        base_p = 1.0 / (1.0 + math.exp(-steepness * distance))
        
        if distance > 0:
            p = base_p + (1.0 - base_p) * certainty * 0.85
        else:
            p = base_p * (1.0 - certainty * 0.85)
        
        p = max(0.001, min(0.999, p))
        return FairValue("TIME_DECAY", p, 1-p, certainty, {'cert': f"{certainty:.0%}"})

    @staticmethod
    def momentum(price: float, strike: float, time_sec: float, vol: float) -> FairValue:
        base = FVEngine.distance(price, strike, time_sec, vol)
        
        prices_30 = core.spot_history_prices(30)
        prices_10 = core.spot_history_prices(10)
        
        if len(prices_30) < 5 or len(prices_10) < 3:
            return FairValue("MOMENTUM", base.fv_up, base.fv_down, base.confidence * 0.5, {'reason': 'low data'})
        
        vel_30 = (prices_30[-1] - prices_30[0]) / 30 if len(prices_30) > 1 else 0
        vel_10 = (prices_10[-1] - prices_10[0]) / 10 if len(prices_10) > 1 else 0
        accel = vel_10 - vel_30
        
        projected = price + vel_10 * min(time_sec, 60)
        proj_fv = FVEngine.distance(projected, strike, time_sec, vol)
        
        strength = min(0.3, abs(vel_10) / 20)
        consistency = 1.0 if vel_10 * vel_30 > 0 else 0.5
        blend = strength * consistency
        
        p = base.fv_up * (1 - blend) + proj_fv.fv_up * blend
        p = max(0.001, min(0.999, p))
        
        return FairValue("MOMENTUM", p, 1-p, base.confidence * 0.85, 
                        {'vel': f"${vel_10:.1f}/s", 'accel': f"${accel:.2f}/s²"})

    @staticmethod
    def imbalance_adj(base_fv: float) -> FairValue:
        up_sig = core.up_imbalance
        down_sig = -core.down_imbalance
        net = (up_sig + down_sig) / 2
        
        adj = net * 0.06  # Max ±6%
        p = max(0.001, min(0.999, base_fv + adj))
        
        conf = core.liquidity_score * 0.8
        
        return FairValue("IMBALANCE", p, 1-p, conf, 
                        {'adj': f"{adj:+.1%}", 'liq': f"{core.liquidity_score:.0%}"})

    @staticmethod
    def oracle_pred(spot: float, oracle: float, strike: float, time_sec: float, vol: float) -> FairValue:
        if oracle <= 0 or strike <= 0:
            return FairValue("ORACLE", 0.5, 0.5, 0.0)
        
        gap = spot - oracle
        lag = core.oracle_lag_sec()
        stats = core.oracle_stats()
        
        expected_updates = time_sec / stats['avg_interval'] if stats['avg_interval'] > 0 else time_sec / 5
        
        if expected_updates >= 1:
            remaining_gap = gap * (0.6 ** expected_updates)
            predicted = spot - remaining_gap
        else:
            prob_update = min(1.0, time_sec / stats['avg_interval']) if stats['avg_interval'] > 0 else 0.5
            predicted = prob_update * (oracle + gap * 0.4) + (1 - prob_update) * oracle
        
        base = FVEngine.distance(predicted, strike, time_sec, vol)
        
        lag_penalty = min(0.4, lag / 15) if lag > 0 else 0
        conf = base.confidence * (1 - lag_penalty)
        
        return FairValue("ORACLE", base.fv_up, base.fv_down, conf, 
                        {'pred': f"${predicted:,.0f}", 'lag': f"{lag:.1f}s"})

    @staticmethod
    def near_expiry(price: float, strike: float, time_sec: float, vol: float) -> FairValue:
        if time_sec > 60 or price <= 0 or strike <= 0:
            return FairValue("NEAR_EXP", 0.5, 0.5, 0.0, {'active': False})
        
        distance = price - strike
        abs_dist = abs(distance)
        
        t_years = time_sec / (365.25 * 24 * 3600)
        max_move = 3 * vol * price * math.sqrt(t_years)
        
        # Also check recent actual moves
        recent = core.spot_history_prices(60)
        if len(recent) >= 10:
            actual_range = max(recent) - min(recent)
            max_move = max(max_move, actual_range * math.sqrt(time_sec / 60))
        
        if abs_dist > max_move:
            cross_prob = 0.01
        elif abs_dist > max_move * 0.7:
            cross_prob = 0.05
        elif abs_dist > max_move * 0.5:
            cross_prob = 0.15
        elif abs_dist > max_move * 0.3:
            cross_prob = 0.30
        else:
            cross_prob = 0.45
        
        p = 1.0 - cross_prob if distance > 0 else cross_prob
        p = max(0.001, min(0.999, p))
        
        if time_sec < 10 and abs_dist > max_move:
            conf = 0.98
        elif time_sec < 30 and abs_dist > max_move * 0.7:
            conf = 0.92
        else:
            conf = 0.80
        
        return FairValue("NEAR_EXP", p, 1-p, conf, 
                        {'max_move': f"${max_move:.0f}", 'cross': f"{cross_prob:.0%}"})

    @staticmethod
    def ensemble(spot: float, oracle: float, strike: float, time_sec: float) -> Tuple[FairValue, List[FairValue]]:
        core.update_realized_vol()
        vol = core.best_vol()
        
        estimates = []
        
        dist = FVEngine.distance(oracle, strike, time_sec, vol)
        estimates.append(dist)
        
        td = FVEngine.time_decay(oracle, strike, time_sec)
        estimates.append(td)
        
        mom = FVEngine.momentum(oracle, strike, time_sec, vol)
        estimates.append(mom)
        
        orc = FVEngine.oracle_pred(spot, oracle, strike, time_sec, vol)
        estimates.append(orc)
        
        base_p = (dist.fv_up + td.fv_up + orc.fv_up) / 3
        imb = FVEngine.imbalance_adj(base_p)
        estimates.append(imb)
        
        near = FVEngine.near_expiry(oracle, strike, time_sec, vol)
        if time_sec <= 60:
            estimates.append(near)
        
        # Dynamic weights
        if time_sec <= 10:
            w = {'DISTANCE': 0.05, 'TIME_DECAY': 0.10, 'MOMENTUM': 0.05, 
                 'ORACLE': 0.15, 'IMBALANCE': 0.10, 'NEAR_EXP': 0.55}
        elif time_sec <= 30:
            w = {'DISTANCE': 0.10, 'TIME_DECAY': 0.15, 'MOMENTUM': 0.10, 
                 'ORACLE': 0.20, 'IMBALANCE': 0.10, 'NEAR_EXP': 0.35}
        elif time_sec <= 60:
            w = {'DISTANCE': 0.15, 'TIME_DECAY': 0.20, 'MOMENTUM': 0.10, 
                 'ORACLE': 0.25, 'IMBALANCE': 0.10, 'NEAR_EXP': 0.20}
        elif time_sec <= 180:
            w = {'DISTANCE': 0.20, 'TIME_DECAY': 0.25, 'MOMENTUM': 0.15, 
                 'ORACLE': 0.25, 'IMBALANCE': 0.15}
        else:
            w = {'DISTANCE': 0.25, 'TIME_DECAY': 0.20, 'MOMENTUM': 0.20, 
                 'ORACLE': 0.20, 'IMBALANCE': 0.15}
        
        # Vol regime adjustments
        if core.vol_regime == "EXTREME":
            if 'MOMENTUM' in w:
                w['MOMENTUM'] *= 0.5
        elif core.vol_regime == "LOW":
            if 'MOMENTUM' in w:
                w['MOMENTUM'] *= 1.3
        
        # Oracle lag adjustment
        lag = core.oracle_lag_sec()
        if lag > 5 and 'ORACLE' in w:
            reduction = min(0.12, lag / 40)
            w['ORACLE'] -= reduction
            w['DISTANCE'] = w.get('DISTANCE', 0.2) + reduction
        
        # Normalize
        total = sum(w.values())
        w = {k: v / total for k, v in w.items()}
        
        # Weighted average
        weighted_sum = 0
        conf_sum = 0
        for e in estimates:
            wt = w.get(e.model, 0)
            weighted_sum += e.fv_up * wt * e.confidence
            conf_sum += wt * e.confidence
        
        fv = weighted_sum / conf_sum if conf_sum > 0 else 0.5
        fv = max(0.001, min(0.999, fv))
        
        # Liquidity adjustment
        liq_adj = 0.5 + 0.5 * core.liquidity_score
        conf = min(1.0, conf_sum) * liq_adj
        
        return FairValue("ENSEMBLE", fv, 1 - fv, conf, 
                        {'vol': f"{vol:.0%}", 'regime': core.vol_regime}), estimates

# ============================================================
# 📈 SIGNAL GENERATOR
# ============================================================
class SignalGen:
    @staticmethod
    def net_edge(fv: float, price: float) -> float:
        if price <= 0:
            return 0.0
        gross = fv - price
        fee = fv * POLY_FEE * (1.0 - price)
        return gross - fee

    @staticmethod
    def generate(ensemble: FairValue, prices: Dict, estimates: List[FairValue]) -> List[Signal]:
        signals = []
        
        for side, ask_key, size_key, fv_attr in [
            ('UP', 'up_ask', 'up_size', 'fv_up'),
            ('DOWN', 'down_ask', 'down_size', 'fv_down')
        ]:
            ask = prices.get(ask_key, 0)
            if ask <= 0:
                continue
            
            fv = getattr(ensemble, fv_attr)
            net = SignalGen.net_edge(fv, ask)
            
            if net >= MIN_EDGE:
                reasons = [f"FV ${fv:.3f} > Ask ${ask:.3f}"]
                
                for e in estimates:
                    e_fv = getattr(e, fv_attr)
                    if e_fv > ask + 0.02:
                        detail = list(e.details.values())[0] if e.details else ""
                        reasons.append(f"{e.model}: ${e_fv:.3f} {detail}")
                
                imb = core.up_imbalance if side == 'UP' else core.down_imbalance
                if imb > 0.2:
                    reasons.append(f"Buy pressure: {imb:+.2f}")
                
                conf = "HIGH" if net > 0.06 else "MEDIUM" if net > 0.035 else "LOW"
                
                signals.append(Signal(
                    side, "BUY", ask, fv, fv - ask, net,
                    prices.get(size_key, 0), conf, reasons
                ))
        
        signals.sort(key=lambda s: s.net_edge, reverse=True)
        return signals

# ============================================================
# 📡 WEBSOCKETS
# ============================================================
class BinanceFuturesWS:
    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        "wss://fstream.binance.com/ws/btcusdt@aggTrade",
                        on_message=lambda ws, m: self._msg(m)
                    )
                    ws.run_forever(ping_interval=30)
                except: pass
                time.sleep(1)
        threading.Thread(target=run, daemon=True).start()
    
    def _msg(self, msg):
        try:
            d = json.loads(msg)
            core.update_futures(float(d['p']), int(d['T']))
        except: pass


class BinanceSpotWS:
    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        "wss://stream.binance.com:9443/ws/btcusdt@trade",
                        on_message=lambda ws, m: self._msg(m)
                    )
                    ws.run_forever(ping_interval=30)
                except: pass
                time.sleep(1)
        threading.Thread(target=run, daemon=True).start()
    
    def _msg(self, msg):
        try:
            d = json.loads(msg)
            core.update_spot(float(d['p']), int(d['T']))
        except: pass


class PolyDataWS:
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
                except: pass
                time.sleep(1)
        threading.Thread(target=run, daemon=True).start()
    
    def _open(self, ws):
        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{"topic": "crypto_prices", "type": "*", "filters": '{"symbol":"btcusdt"}'}]
        }))
        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'}]
        }))
    
    def _msg(self, ws, msg):
        try:
            d = json.loads(msg)
            if d.get('type') == 'update':
                topic = d.get('topic')
                payload = d.get('payload', {})
                val = payload.get('value')
                ts = payload.get('timestamp')
                if val and ts:
                    if topic == 'crypto_prices' and payload.get('symbol') == 'btcusdt':
                        core.update_relay(float(val), int(ts))
                    elif topic == 'crypto_prices_chainlink' and payload.get('symbol') == 'btc/usd':
                        core.update_oracle(float(val), int(ts))
        except: pass


class PolyClobWS:
    def __init__(self):
        self.ws = None
        self.subscribed: set = set()
        self.lock = threading.Lock()
    
    def start(self):
        def run():
            while True:
                try:
                    self.ws = websocket.WebSocketApp(
                        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                        on_open=lambda ws: self._sub(),
                        on_message=self._msg,
                        on_close=lambda ws, c, m: self.clear_subs()
                    )
                    self.ws.run_forever(ping_interval=30)
                except: pass
                time.sleep(1)
        threading.Thread(target=run, daemon=True).start()
    
    def clear_subs(self):
        with self.lock:
            self.subscribed.clear()
    
    def _sub(self):
        with self.lock:
            tokens = []
            if market.up_token and market.up_token not in self.subscribed:
                tokens.append(market.up_token)
            if market.down_token and market.down_token not in self.subscribed:
                tokens.append(market.down_token)
            
            if tokens and self.ws:
                try:
                    self.ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": "market", 
                        "assets_ids": tokens
                    }))
                    self.subscribed.update(tokens)
                except: pass
    
    def try_subscribe(self):
        """Called from main loop to subscribe if needed"""
        if market.up_token and market.up_token not in self.subscribed:
            self._sub()
    
    def _msg(self, ws, msg):
        try:
            d = json.loads(msg)
            evt = d.get('event_type')
            
            if evt == 'book':
                asset = d.get('asset_id')
                if asset == market.up_token:
                    core.set_book('UP', d.get('bids', []), d.get('asks', []))
                elif asset == market.down_token:
                    core.set_book('DOWN', d.get('bids', []), d.get('asks', []))
            
            elif evt == 'price_change':
                for c in d.get('price_changes', []):
                    asset = c.get('asset_id')
                    if asset == market.up_token:
                        core.update_level('UP', c.get('price'), float(c.get('size', 0)), c.get('side'))
                    elif asset == market.down_token:
                        core.update_level('DOWN', c.get('price'), float(c.get('size', 0)), c.get('side'))
        except: pass

clob_ws = PolyClobWS()

# ============================================================
# 🎨 UI
# ============================================================
def fmt_time(sec: float) -> Text:
    m, s = int(sec // 60), int(sec % 60)
    style = "bold green" if sec > 120 else "bold yellow" if sec > 60 else "bold red" if sec > 30 else "bold red blink"
    return Text(f"{m:02d}:{s:02d}", style=style)


def render_prices() -> Panel:
    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("", width=18)
    t.add_column("PRICE", justify="right", width=12)
    t.add_column("LAG", justify="right", width=8)
    
    now = int(time.time() * 1000)
    
    def row(name, price, ts, hl=False):
        if ts == 0:
            lag, sty = "-", "dim"
        else:
            l = now - ts
            lag = f"{l}ms"
            sty = "green" if l < 100 else "yellow" if l < 500 else "red"
        pstr = f"${price:,.2f}" if price > 0 else "-"
        nsty = "bold cyan" if hl else "white"
        t.add_row(Text(name, style=nsty), Text(pstr, style=nsty), Text(lag, style=sty))
    
    row("⚡ Futures", core.futures_price, core.futures_ts)
    row("📊 Spot", core.spot_price, core.spot_ts)
    row("🔄 Relay", core.relay_price, core.relay_ts)
    row("🔮 CHAINLINK", core.oracle_price, core.oracle_ts, hl=True)
    
    return Panel(t, title="PRICES", border_style="cyan")


def render_vol() -> Panel:
    c = Text()
    c.append(f"EWMA:  {core.vol_ewma:.0%}\n", style="white")
    c.append(f"1-min: {core.vol_1m:.0%}\n", style="white")
    c.append(f"5-min: {core.vol_5m:.0%}\n", style="white")
    
    sty = {"LOW": "green", "NORMAL": "white", "HIGH": "yellow", "EXTREME": "bold red"}.get(core.vol_regime, "white")
    c.append(f"\n{core.vol_regime}", style=sty)
    
    return Panel(c, title="📈 VOL", border_style="magenta")


def render_imb() -> Panel:
    c = Text()
    
    for name, val, color in [("UP", core.up_imbalance, "green"), ("DN", core.down_imbalance, "red")]:
        fill = int((val + 1) / 2 * 10)
        bar = "█" * fill + "░" * (10 - fill)
        sty = "green" if val > 0.1 else "red" if val < -0.1 else "white"
        c.append(f"{name} [{bar}] {val:+.2f}\n", style=sty)
    
    c.append(f"\nLiq: {core.liquidity_score:.0%}", style="dim")
    
    return Panel(c, title="⚖️ FLOW", border_style="yellow")


def render_market() -> Panel:
    c = Text()
    c.append(f"{market.slug}\n", style="dim")
    c.append("\n⏱️ ", style="white")
    c.append_text(fmt_time(market.time_left()))
    
    if market.strike > 0:
        c.append(f"\n\n⚡ ${market.strike:,.2f}", style="cyan")
        if market.strike_captured:
            c.append(" ✓", style="green")
        
        side, margin = market.outcome_status()
        if side == "UP":
            c.append(f"\n📈 +${margin:.0f}", style="bold green")
        elif side == "DOWN":
            c.append(f"\n📉 -${margin:.0f}", style="bold red")
    
    c.append(f"\n\n{market.status}", style="dim")
    
    return Panel(c, title="🎯 MARKET", border_style="blue")


def render_fv(ens: FairValue, ests: List[FairValue]) -> Panel:
    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("MODEL", width=10)
    t.add_column("UP", justify="right", style="green", width=7)
    t.add_column("DN", justify="right", style="red", width=7)
    t.add_column("C", justify="right", width=4)
    t.add_column("", width=12)
    
    for e in ests:
        detail = list(e.details.values())[0] if e.details else ""
        t.add_row(e.model, f"${e.fv_up:.3f}", f"${e.fv_down:.3f}", f"{e.confidence:.0%}", str(detail)[:12])
    
    t.add_row(
        Text("ENSEMBLE", style="bold"),
        Text(f"${ens.fv_up:.3f}", style="bold green"),
        Text(f"${ens.fv_down:.3f}", style="bold red"),
        Text(f"{ens.confidence:.0%}", style="bold"),
        ""
    )
    
    return Panel(t, title="📐 FAIR VALUE", border_style="magenta")


def render_book() -> Panel:
    p = core.best_prices()
    c = Text()
    
    c.append("UP  ", style="bold green")
    c.append(f"${p['up_bid']:.3f} × ${p['up_ask']:.3f}\n", style="green")
    c.append("DN  ", style="bold red")
    c.append(f"${p['down_bid']:.3f} × ${p['down_ask']:.3f}\n", style="red")
    
    total = p['up_ask'] + p['down_ask']
    c.append(f"\nΣ ${total:.4f} ", style="white")
    if 0 < total < 0.995:
        c.append(f"ARB!", style="bold green")
    
    return Panel(c, title="📚 BOOK", border_style="yellow")


def render_signals(sigs: List[Signal]) -> Panel:
    if not sigs:
        return Panel(Text("No signals", style="dim"), title="SIGNALS", border_style="dim")
    
    c = Text()
    for i, s in enumerate(sigs[:2]):
        sty = "bold green" if s.side == "UP" else "bold red"
        c.append(f"#{i+1} BUY ", style="bold")
        c.append(f"{s.side}\n", style=sty)
        c.append(f"  ${s.price:.4f} → ${s.fair_value:.3f}\n", style="white")
        c.append(f"  Edge: {s.edge:.1%}, Net: ", style="white")
        c.append(f"{s.net_edge:.1%}\n", style="bold green" if s.net_edge > 0.04 else "green")
        c.append(f"  {s.confidence} | {s.size:,.0f}\n", style="dim")
        for r in s.reasons[:2]:
            c.append(f"  • {r}\n", style="dim italic")
        c.append("\n")
    
    bdr = "bold green" if sigs[0].net_edge > 0.04 else "yellow"
    return Panel(c, title="🎯 SIGNALS", border_style=bdr)


def render_calib() -> Panel:
    results = core.window_results
    if len(results) < 3:
        return Panel(Text(f"Collecting... ({len(results)})", style="dim"), title="CALIB", border_style="dim")
    
    n = len(results)
    c60 = sum(1 for r in results if (r.fv_at_60s > 0.5) == (r.outcome == "UP"))
    c30 = sum(1 for r in results if (r.fv_at_30s > 0.5) == (r.outcome == "UP"))
    c10 = sum(1 for r in results if (r.fv_at_10s > 0.5) == (r.outcome == "UP"))
    
    c = Text()
    c.append(f"n={n}\n", style="dim")
    c.append(f"@60s: {c60/n:.0%}\n", style="white")
    c.append(f"@30s: {c30/n:.0%}\n", style="white")
    c.append(f"@10s: {c10/n:.0%}", style="white")
    
    return Panel(c, title="📊 CALIB", border_style="dim")


def render_stats() -> Panel:
    c = Text()
    c.append(f"{core.msg_count:,} msgs\n", style="dim")
    c.append("WS: ", style="dim")
    for k, v in core.connected.items():
        c.append(k[0].upper(), style="green" if v else "red")
    return Panel(c, title="SYS", border_style="dim")


def dashboard() -> Layout:
    # Check lifecycle
    changed = market.lifecycle(clob_ws)
    
    # Fetch tokens
    market.fetch_tokens()
    
    # Subscribe
    clob_ws.try_subscribe()
    
    # Calculate FV
    ens = FairValue("ENSEMBLE", 0.5, 0.5, 0.0, {})
    ests = []
    sigs = []
    
    if market.strike > 0 and core.oracle_price > 0:
        ens, ests = FVEngine.ensemble(core.spot_price, core.oracle_price, market.strike, market.time_left())
        sigs = SignalGen.generate(ens, core.best_prices(), ests)
        market.snapshot_fv(ens.fv_up)
    
    # Layout
    layout = Layout()
    
    header = Text()
    header.append("⚡ BTC 15-MIN FV v2.0 ", style="bold cyan")
    header.append(datetime.now().strftime("%H:%M:%S"), style="dim")
    
    layout.split(
        Layout(Panel(header, box=box.MINIMAL), size=3),
        Layout(name="body")
    )
    
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="mid"),
        Layout(name="right")
    )
    
    layout["left"].split(
        Layout(render_market()),
        Layout(render_prices()),
    )
    
    layout["mid"].split(
        Layout(render_fv(ens, ests)),
        Layout(render_book()),
        Layout(name="mid_bot")
    )
    
    layout["mid"]["mid_bot"].split_row(
        Layout(render_vol()),
        Layout(render_imb())
    )
    
    layout["right"].split(
        Layout(render_signals(sigs), ratio=3),
        Layout(render_calib()),
        Layout(render_stats())
    )
    
    return layout


# ============================================================
# 🚀 MAIN
# ============================================================
def main():
    console = Console()
    
    console.print("\n[bold cyan]⚡ BTC 15-MIN FAIR VALUE SYSTEM v2.0[/bold cyan]")
    console.print("[dim]Enhanced: Real-time vol, Imbalance, Near-expiry, Oracle prediction[/dim]\n")
    
    # Load history
    market.load_history()
    console.print(f"[dim]Loaded {len(core.window_results)} historical results[/dim]")
    
    # Start WebSockets
    console.print("[yellow]Starting feeds...[/yellow]")
    BinanceFuturesWS().start()
    BinanceSpotWS().start()
    PolyDataWS().start()
    clob_ws.start()
    console.print("[green]✓ All WebSockets started[/green]")
    
    # Init market
    market.lifecycle(clob_ws)
    market.fetch_tokens()
    
    if market.up_token:
        console.print(f"[green]✓ {market.slug}[/green]")
    
    time.sleep(2)
    console.print("[bold green]Starting...[/bold green]\n")
    
    try:
        with Live(dashboard(), refresh_per_second=10, screen=True) as live:
            while True:
                live.update(dashboard())
                time.sleep(0.1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")


if __name__ == "__main__":
    main()