"""
================================================================================
⚡ POLYMARKET BTC 15-MIN FAIR VALUE CALCULATOR & TRADING SYSTEM
================================================================================

FAIR VALUE MODELS:

1. DISTANCE MODEL:     Based on current price vs strike (naive)
2. VOLATILITY MODEL:   Uses historical volatility to estimate probability  
3. BLACK-SCHOLES:      Options-pricing inspired model
4. MOMENTUM MODEL:     Adjusts for price trend direction
5. ENSEMBLE MODEL:     Weighted combination of all models

EDGE DETECTION:
- Fair Value vs Market Price = Edge
- Edge > Threshold = Trading Signal

"""

import json
import time
import threading
import re
import math
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Deque
from collections import deque
from scipy import stats  # For normal distribution CDF
import numpy as np

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
WINDOW_SIZE = 900              # 15 minutes in seconds
REFRESH_RATE = 0.1             # 10 FPS
VOLATILITY_LOOKBACK = 300      # 5 minutes of price history for vol calc

# API Endpoints
EVENT_SLUG_BASE = "https://gamma-api.polymarket.com/events/slug/"
CLOB_PRICE_URL = "https://clob.polymarket.com/price"

# Trading Thresholds
POLY_FEE = 0.02                # 2% fee on winnings
MIN_EDGE_TO_TRADE = 0.03       # 3% edge minimum
HIGH_CONFIDENCE_EDGE = 0.08    # 8% edge = high confidence

# Volatility defaults (annualized, will be calculated from data)
DEFAULT_BTC_VOLATILITY = 0.60  # 60% annualized vol as fallback

# ============================================================
# 📊 DATA STRUCTURES
# ============================================================
@dataclass
class PriceTick:
    """Single price observation"""
    timestamp: float  # Unix timestamp in seconds
    price: float
    source: str  # 'spot', 'futures', 'oracle'

@dataclass
class FairValueEstimate:
    """Fair value calculation result"""
    model_name: str
    fair_value_up: float      # 0.0 to 1.0
    fair_value_down: float    # 0.0 to 1.0 (should be 1 - up)
    confidence: float         # 0.0 to 1.0
    inputs: Dict              # Model inputs for debugging

@dataclass
class TradingSignal:
    """Trading signal based on fair value vs market price"""
    side: str                 # 'UP' or 'DOWN'
    action: str               # 'BUY' or 'SELL'
    market_price: float
    fair_value: float
    edge: float               # fair_value - market_price (for BUY)
    edge_pct: float           # edge as percentage of market price
    net_edge: float           # edge after fees
    confidence: str           # 'HIGH', 'MEDIUM', 'LOW'
    size_recommendation: float
    reason: str

@dataclass
class VolatilityEstimate:
    """Volatility calculation result"""
    realized_vol_1m: float    # 1-minute realized vol (annualized)
    realized_vol_5m: float    # 5-minute realized vol (annualized)
    implied_vol: float        # Implied from option-like pricing
    vol_regime: str           # 'LOW', 'NORMAL', 'HIGH', 'EXTREME'

# ============================================================
# 🧱 ENHANCED DATA CORE
# ============================================================
class DataCore:
    """Central data repository with price history for volatility"""
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
        
        # Price history (for volatility calculation)
        self.spot_history: Deque[PriceTick] = deque(maxlen=1000)
        self.futures_history: Deque[PriceTick] = deque(maxlen=1000)
        self.oracle_history: Deque[PriceTick] = deque(maxlen=1000)
        
        # Orderbooks
        self.up_bids: Dict[str, float] = {}
        self.up_asks: Dict[str, float] = {}
        self.down_bids: Dict[str, float] = {}
        self.down_asks: Dict[str, float] = {}
        
        # Calculated values (updated periodically)
        self.volatility: Optional[VolatilityEstimate] = None
        
        # Stats
        self.messages = 0
        self.connected = {'futures': False, 'spot': False, 'poly': False, 'clob': False}

    def update_spot(self, price: float, ts: int):
        with self.lock:
            self.spot_price = price
            self.spot_ts = ts
            self.spot_history.append(PriceTick(ts / 1000, price, 'spot'))
            self.connected['spot'] = True
            self.messages += 1

    def update_futures(self, price: float, ts: int):
        with self.lock:
            self.futures_price = price
            self.futures_ts = ts
            self.futures_history.append(PriceTick(ts / 1000, price, 'futures'))
            self.connected['futures'] = True
            self.messages += 1

    def update_oracle(self, price: float, ts: int):
        with self.lock:
            self.oracle_price = price
            self.oracle_ts = ts
            self.oracle_history.append(PriceTick(ts / 1000, price, 'oracle'))
            self.connected['poly'] = True
            self.messages += 1

    def update_relay(self, price: float, ts: int):
        with self.lock:
            self.relay_price = price
            self.relay_ts = ts
            self.messages += 1

    def update_orderbook(self, side: str, bids: list, asks: list):
        with self.lock:
            if side == 'UP':
                self.up_bids = {b['price']: float(b['size']) for b in bids}
                self.up_asks = {a['price']: float(a['size']) for a in asks}
            else:
                self.down_bids = {b['price']: float(b['size']) for b in bids}
                self.down_asks = {a['price']: float(a['size']) for a in asks}
            self.connected['clob'] = True
            self.messages += 1

    def get_best_prices(self) -> Dict:
        """Get best bid/ask for UP and DOWN"""
        with self.lock:
            return {
                'up_bid': max(float(p) for p in self.up_bids.keys()) if self.up_bids else 0,
                'up_ask': min(float(p) for p in self.up_asks.keys()) if self.up_asks else 0,
                'down_bid': max(float(p) for p in self.down_bids.keys()) if self.down_bids else 0,
                'down_ask': min(float(p) for p in self.down_asks.keys()) if self.down_asks else 0,
            }

    def get_price_history(self, source: str, lookback_seconds: float) -> List[PriceTick]:
        """Get recent price history for volatility calculation"""
        with self.lock:
            now = time.time()
            cutoff = now - lookback_seconds
            
            if source == 'spot':
                return [t for t in self.spot_history if t.timestamp > cutoff]
            elif source == 'futures':
                return [t for t in self.futures_history if t.timestamp > cutoff]
            elif source == 'oracle':
                return [t for t in self.oracle_history if t.timestamp > cutoff]
            return []

    def get_oracle_lag_seconds(self) -> float:
        """Get oracle lag in seconds"""
        if self.oracle_ts == 0:
            return -1
        return time.time() - (self.oracle_ts / 1000)

core = DataCore()

# ============================================================
# 📈 VOLATILITY CALCULATOR
# ============================================================
class VolatilityCalculator:
    """
    Calculates realized volatility from price history.
    Uses log returns and annualizes based on time period.
    """
    
    @staticmethod
    def calculate_realized_volatility(prices: List[float], period_seconds: float) -> float:
        """
        Calculate annualized realized volatility from price series.
        
        Args:
            prices: List of prices (chronological order)
            period_seconds: Time span of the price series in seconds
        
        Returns:
            Annualized volatility as decimal (e.g., 0.60 = 60%)
        """
        if len(prices) < 2:
            return DEFAULT_BTC_VOLATILITY
        
        # Calculate log returns
        log_returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0 and prices[i] > 0:
                log_returns.append(math.log(prices[i] / prices[i-1]))
        
        if len(log_returns) < 2:
            return DEFAULT_BTC_VOLATILITY
        
        # Standard deviation of returns
        std_dev = np.std(log_returns)
        
        # Annualize: multiply by sqrt(periods_per_year)
        # If we have N returns over period_seconds, each return covers period_seconds/N seconds
        # Periods per year = 365.25 * 24 * 3600 / (period_seconds / len(log_returns))
        seconds_per_year = 365.25 * 24 * 3600
        periods_per_year = seconds_per_year / (period_seconds / len(log_returns))
        
        annualized_vol = std_dev * math.sqrt(periods_per_year)
        
        # Clamp to reasonable range
        return max(0.10, min(3.0, annualized_vol))
    
    @staticmethod
    def calculate_from_core(lookback_seconds: float = 300) -> VolatilityEstimate:
        """Calculate volatility from DataCore price history"""
        
        # Get spot price history
        spot_ticks = core.get_price_history('spot', lookback_seconds)
        spot_prices = [t.price for t in spot_ticks]
        
        # Calculate for different windows
        vol_1m = VolatilityCalculator.calculate_realized_volatility(
            [t.price for t in core.get_price_history('spot', 60)], 60
        )
        
        vol_5m = VolatilityCalculator.calculate_realized_volatility(
            spot_prices, lookback_seconds
        )
        
        # Determine regime
        if vol_5m < 0.30:
            regime = 'LOW'
        elif vol_5m < 0.60:
            regime = 'NORMAL'
        elif vol_5m < 1.00:
            regime = 'HIGH'
        else:
            regime = 'EXTREME'
        
        return VolatilityEstimate(
            realized_vol_1m=vol_1m,
            realized_vol_5m=vol_5m,
            implied_vol=vol_5m,  # For now, use realized as proxy
            vol_regime=regime
        )

# ============================================================
# 🧮 FAIR VALUE MODELS
# ============================================================
class FairValueModels:
    """
    Collection of fair value calculation models.
    Each model estimates the probability that BTC will be above strike at expiry.
    """
    
    @staticmethod
    def distance_model(
        current_price: float,
        strike: float,
        time_remaining: float,
        volatility: float
    ) -> FairValueEstimate:
        """
        Simple model: probability based purely on distance from strike.
        Uses normal distribution assumption.
        
        This is essentially a simplified Black-Scholes without interest rates.
        """
        if current_price <= 0 or strike <= 0 or time_remaining <= 0:
            return FairValueEstimate(
                model_name="DISTANCE",
                fair_value_up=0.5,
                fair_value_down=0.5,
                confidence=0.0,
                inputs={}
            )
        
        # Convert annualized vol to vol for remaining time
        # time_remaining is in seconds, convert to years
        time_years = time_remaining / (365.25 * 24 * 3600)
        
        # Standard deviation of price movement expected
        # σ_period = σ_annual * √(time_years)
        sigma_period = volatility * math.sqrt(time_years)
        
        # How many standard deviations is strike from current price?
        # Using log-normal assumption
        if sigma_period > 0:
            # d2 in Black-Scholes terms (simplified, no risk-free rate)
            d2 = (math.log(current_price / strike)) / sigma_period
            
            # Probability of finishing above strike = N(d2)
            prob_up = stats.norm.cdf(d2)
        else:
            # No time left, pure distance
            prob_up = 1.0 if current_price > strike else 0.0
        
        # Clamp to reasonable range
        prob_up = max(0.001, min(0.999, prob_up))
        
        # Confidence based on distance (further = more confident)
        distance_pct = abs(current_price - strike) / strike
        confidence = min(1.0, distance_pct * 10)  # 10% distance = full confidence
        
        return FairValueEstimate(
            model_name="DISTANCE",
            fair_value_up=prob_up,
            fair_value_down=1.0 - prob_up,
            confidence=confidence,
            inputs={
                'current_price': current_price,
                'strike': strike,
                'time_remaining': time_remaining,
                'volatility': volatility,
                'sigma_period': sigma_period,
                'd2': d2 if sigma_period > 0 else None
            }
        )
    
    @staticmethod
    def time_decay_model(
        current_price: float,
        strike: float,
        time_remaining: float
    ) -> FairValueEstimate:
        """
        Model that emphasizes time decay.
        As time approaches 0, fair value approaches 0 or 1 based on current position.
        
        Uses exponential decay of uncertainty.
        """
        if current_price <= 0 or strike <= 0:
            return FairValueEstimate(
                model_name="TIME_DECAY",
                fair_value_up=0.5,
                fair_value_down=0.5,
                confidence=0.0,
                inputs={}
            )
        
        # Distance from strike as percentage
        distance_pct = (current_price - strike) / strike
        
        # Time decay factor: as time → 0, certainty → 1
        # Using exponential: certainty = 1 - e^(-k/time)
        # k controls how fast certainty increases as time decreases
        k = 30  # With 30 seconds left, we're about 63% certain
        
        if time_remaining > 0:
            certainty = 1.0 - math.exp(-k / time_remaining)
        else:
            certainty = 1.0
        
        # Base probability from distance (sigmoid function)
        # Steeper sigmoid as certainty increases
        steepness = 0.0001 + (certainty * 0.001)  # Ranges from 0.0001 to 0.0011
        base_prob = 1.0 / (1.0 + math.exp(-steepness * (current_price - strike)))
        
        # Blend toward 0 or 1 based on certainty
        if current_price > strike:
            prob_up = base_prob + (1.0 - base_prob) * certainty
        else:
            prob_up = base_prob * (1.0 - certainty)
        
        prob_up = max(0.001, min(0.999, prob_up))
        
        return FairValueEstimate(
            model_name="TIME_DECAY",
            fair_value_up=prob_up,
            fair_value_down=1.0 - prob_up,
            confidence=certainty,
            inputs={
                'current_price': current_price,
                'strike': strike,
                'time_remaining': time_remaining,
                'distance_pct': distance_pct,
                'certainty': certainty
            }
        )
    
    @staticmethod
    def momentum_model(
        current_price: float,
        strike: float,
        time_remaining: float,
        price_history: List[PriceTick],
        base_volatility: float
    ) -> FairValueEstimate:
        """
        Model that incorporates price momentum/trend.
        If price is trending toward strike, reduce winning probability.
        If trending away, increase it.
        """
        if len(price_history) < 10 or current_price <= 0 or strike <= 0:
            # Fall back to distance model
            return FairValueModels.distance_model(current_price, strike, time_remaining, base_volatility)
        
        # Calculate recent trend (last 30 seconds)
        recent = [t for t in price_history if t.timestamp > time.time() - 30]
        if len(recent) < 5:
            recent = price_history[-10:]
        
        prices = [t.price for t in recent]
        
        # Linear regression for trend
        x = list(range(len(prices)))
        slope, _, _, _, _ = stats.linregress(x, prices)
        
        # Normalize slope to "dollars per second"
        time_span = recent[-1].timestamp - recent[0].timestamp if len(recent) > 1 else 1
        slope_per_second = slope * len(prices) / max(time_span, 1)
        
        # Project price at expiry based on trend
        projected_price = current_price + (slope_per_second * time_remaining)
        
        # Get base probability from distance model
        base_estimate = FairValueModels.distance_model(current_price, strike, time_remaining, base_volatility)
        
        # Adjust based on trend
        # If trending up and currently above strike, increase UP probability
        # If trending down and currently below strike, increase DOWN probability
        trend_adjustment = 0.0
        
        if slope_per_second > 0:  # Uptrend
            trend_strength = min(1.0, abs(slope_per_second) / 10)  # $10/sec = max strength
            trend_adjustment = trend_strength * 0.1  # Max 10% adjustment
        elif slope_per_second < 0:  # Downtrend
            trend_strength = min(1.0, abs(slope_per_second) / 10)
            trend_adjustment = -trend_strength * 0.1
        
        adjusted_prob_up = base_estimate.fair_value_up + trend_adjustment
        adjusted_prob_up = max(0.001, min(0.999, adjusted_prob_up))
        
        return FairValueEstimate(
            model_name="MOMENTUM",
            fair_value_up=adjusted_prob_up,
            fair_value_down=1.0 - adjusted_prob_up,
            confidence=base_estimate.confidence * 0.9,  # Slightly less confident due to trend uncertainty
            inputs={
                'current_price': current_price,
                'strike': strike,
                'slope_per_second': slope_per_second,
                'projected_price': projected_price,
                'trend_adjustment': trend_adjustment,
                'base_prob_up': base_estimate.fair_value_up
            }
        )
    
    @staticmethod
    def oracle_adjusted_model(
        spot_price: float,
        oracle_price: float,
        strike: float,
        time_remaining: float,
        oracle_lag_seconds: float,
        volatility: float
    ) -> FairValueEstimate:
        """
        Model that accounts for oracle lag.
        Settlement is based on oracle, not spot.
        If oracle lags significantly, we need to estimate where oracle will be at expiry.
        """
        if oracle_price <= 0 or strike <= 0:
            return FairValueModels.distance_model(spot_price, strike, time_remaining, volatility)
        
        # Estimate oracle catch-up
        # Oracle typically lags spot by a few seconds
        # We assume oracle will converge toward spot
        
        if oracle_lag_seconds > 0 and spot_price > 0:
            # How much has spot moved since oracle's timestamp?
            price_gap = spot_price - oracle_price
            
            # Assume oracle will catch up linearly over remaining time
            # But cap the catch-up expectation
            expected_oracle_move = price_gap * min(1.0, time_remaining / max(oracle_lag_seconds, 1))
            
            # Estimated oracle price at expiry
            estimated_oracle_at_expiry = oracle_price + expected_oracle_move
        else:
            estimated_oracle_at_expiry = oracle_price
        
        # Use the estimated oracle price for fair value calculation
        estimate = FairValueModels.distance_model(
            estimated_oracle_at_expiry, strike, time_remaining, volatility
        )
        
        # Reduce confidence if oracle is lagging significantly
        lag_penalty = min(0.3, oracle_lag_seconds / 10)  # Up to 30% confidence penalty
        
        return FairValueEstimate(
            model_name="ORACLE_ADJ",
            fair_value_up=estimate.fair_value_up,
            fair_value_down=estimate.fair_value_down,
            confidence=max(0.1, estimate.confidence - lag_penalty),
            inputs={
                'spot_price': spot_price,
                'oracle_price': oracle_price,
                'oracle_lag_seconds': oracle_lag_seconds,
                'price_gap': spot_price - oracle_price,
                'estimated_oracle_at_expiry': estimated_oracle_at_expiry,
                'strike': strike
            }
        )
    
    @staticmethod
    def ensemble_model(
        spot_price: float,
        oracle_price: float,
        strike: float,
        time_remaining: float,
        volatility: float,
        price_history: List[PriceTick],
        oracle_lag_seconds: float
    ) -> FairValueEstimate:
        """
        Ensemble model that combines all models with weighted averaging.
        Weights are based on model confidence and market conditions.
        """
        # Get all model estimates
        distance = FairValueModels.distance_model(spot_price, strike, time_remaining, volatility)
        time_decay = FairValueModels.time_decay_model(spot_price, strike, time_remaining)
        momentum = FairValueModels.momentum_model(spot_price, strike, time_remaining, price_history, volatility)
        oracle_adj = FairValueModels.oracle_adjusted_model(
            spot_price, oracle_price, strike, time_remaining, oracle_lag_seconds, volatility
        )
        
        # Dynamic weighting based on conditions
        weights = {
            'distance': 0.25,
            'time_decay': 0.25,
            'momentum': 0.20,
            'oracle_adj': 0.30
        }
        
        # Adjust weights based on time remaining
        if time_remaining < 60:  # Last minute
            weights['time_decay'] = 0.40
            weights['distance'] = 0.15
            weights['momentum'] = 0.15
            weights['oracle_adj'] = 0.30
        elif time_remaining < 180:  # Last 3 minutes
            weights['time_decay'] = 0.30
            weights['oracle_adj'] = 0.35
        
        # If oracle is lagging, increase oracle_adj weight
        if oracle_lag_seconds > 3:
            weights['oracle_adj'] = min(0.50, weights['oracle_adj'] + 0.15)
            # Normalize other weights
            other_total = 1.0 - weights['oracle_adj']
            for k in ['distance', 'time_decay', 'momentum']:
                weights[k] = weights[k] * other_total / (1.0 - 0.30)
        
        # Calculate weighted average
        estimates = [
            (distance, weights['distance']),
            (time_decay, weights['time_decay']),
            (momentum, weights['momentum']),
            (oracle_adj, weights['oracle_adj'])
        ]
        
        total_weight = sum(w * e.confidence for e, w in estimates)
        if total_weight == 0:
            total_weight = 1.0
        
        weighted_prob_up = sum(e.fair_value_up * w * e.confidence for e, w in estimates) / total_weight
        weighted_confidence = sum(e.confidence * w for e, w in estimates)
        
        weighted_prob_up = max(0.001, min(0.999, weighted_prob_up))
        
        return FairValueEstimate(
            model_name="ENSEMBLE",
            fair_value_up=weighted_prob_up,
            fair_value_down=1.0 - weighted_prob_up,
            confidence=weighted_confidence,
            inputs={
                'weights': weights,
                'distance_fv': distance.fair_value_up,
                'time_decay_fv': time_decay.fair_value_up,
                'momentum_fv': momentum.fair_value_up,
                'oracle_adj_fv': oracle_adj.fair_value_up,
                'model_confidences': {
                    'distance': distance.confidence,
                    'time_decay': time_decay.confidence,
                    'momentum': momentum.confidence,
                    'oracle_adj': oracle_adj.confidence
                }
            }
        )

# ============================================================
# 📊 EDGE CALCULATOR & SIGNAL GENERATOR
# ============================================================
class EdgeCalculator:
    """
    Calculates trading edge (fair value - market price) and generates signals.
    """
    
    @staticmethod
    def calculate_edge(
        fair_value: float,
        market_price: float,
        side: str  # 'UP' or 'DOWN'
    ) -> Tuple[float, float]:
        """
        Calculate edge for buying a side.
        
        Returns: (edge_absolute, edge_percentage)
        """
        if market_price <= 0:
            return 0.0, 0.0
        
        edge = fair_value - market_price
        edge_pct = edge / market_price
        
        return edge, edge_pct
    
    @staticmethod
    def calculate_net_edge(
        fair_value: float,
        market_price: float
    ) -> float:
        """
        Calculate edge after Polymarket fees.
        Fee is 2% on winnings (profit), not on payout.
        """
        if market_price <= 0:
            return 0.0
        
        gross_edge = fair_value - market_price
        
        # Expected profit = (fair_value * $1.00) - market_price
        # But we only pay fee on winning trades
        # Expected fee = fair_value * fee * (1 - market_price)
        expected_profit = fair_value * 1.0 - market_price
        expected_fee = fair_value * POLY_FEE * (1.0 - market_price)
        
        net_edge = expected_profit - expected_fee
        
        return net_edge
    
    @staticmethod
    def generate_signals(
        fair_value_estimate: FairValueEstimate,
        market_prices: Dict[str, float],  # 'up_bid', 'up_ask', 'down_bid', 'down_ask'
        available_sizes: Dict[str, float] = None
    ) -> List[TradingSignal]:
        """
        Generate trading signals based on fair value vs market prices.
        """
        signals = []
        
        fv_up = fair_value_estimate.fair_value_up
        fv_down = fair_value_estimate.fair_value_down
        
        # Check BUY UP opportunity
        up_ask = market_prices.get('up_ask', 0)
        if up_ask > 0:
            edge, edge_pct = EdgeCalculator.calculate_edge(fv_up, up_ask, 'UP')
            net_edge = EdgeCalculator.calculate_net_edge(fv_up, up_ask)
            
            if net_edge > MIN_EDGE_TO_TRADE:
                confidence = 'HIGH' if net_edge > HIGH_CONFIDENCE_EDGE else 'MEDIUM' if net_edge > MIN_EDGE_TO_TRADE * 1.5 else 'LOW'
                
                signals.append(TradingSignal(
                    side='UP',
                    action='BUY',
                    market_price=up_ask,
                    fair_value=fv_up,
                    edge=edge,
                    edge_pct=edge_pct,
                    net_edge=net_edge,
                    confidence=confidence,
                    size_recommendation=available_sizes.get('up_ask_size', 0) if available_sizes else 0,
                    reason=f"FV ${fv_up:.3f} > Ask ${up_ask:.3f}, {net_edge:.1%} net edge"
                ))
        
        # Check BUY DOWN opportunity
        down_ask = market_prices.get('down_ask', 0)
        if down_ask > 0:
            edge, edge_pct = EdgeCalculator.calculate_edge(fv_down, down_ask, 'DOWN')
            net_edge = EdgeCalculator.calculate_net_edge(fv_down, down_ask)
            
            if net_edge > MIN_EDGE_TO_TRADE:
                confidence = 'HIGH' if net_edge > HIGH_CONFIDENCE_EDGE else 'MEDIUM' if net_edge > MIN_EDGE_TO_TRADE * 1.5 else 'LOW'
                
                signals.append(TradingSignal(
                    side='DOWN',
                    action='BUY',
                    market_price=down_ask,
                    fair_value=fv_down,
                    edge=edge,
                    edge_pct=edge_pct,
                    net_edge=net_edge,
                    confidence=confidence,
                    size_recommendation=available_sizes.get('down_ask_size', 0) if available_sizes else 0,
                    reason=f"FV ${fv_down:.3f} > Ask ${down_ask:.3f}, {net_edge:.1%} net edge"
                ))
        
        # Check SELL UP opportunity (if we could short or already hold)
        up_bid = market_prices.get('up_bid', 0)
        if up_bid > 0 and up_bid > fv_up:
            edge = up_bid - fv_up
            edge_pct = edge / fv_up if fv_up > 0 else 0
            
            if edge_pct > MIN_EDGE_TO_TRADE:
                signals.append(TradingSignal(
                    side='UP',
                    action='SELL',
                    market_price=up_bid,
                    fair_value=fv_up,
                    edge=edge,
                    edge_pct=edge_pct,
                    net_edge=edge * 0.98,  # Approximate
                    confidence='MEDIUM',
                    size_recommendation=0,
                    reason=f"Bid ${up_bid:.3f} > FV ${fv_up:.3f}, overpriced"
                ))
        
        # Sort by net edge
        signals.sort(key=lambda x: x.net_edge, reverse=True)
        
        return signals

# ============================================================
# 🎯 MARKET MANAGER (Simplified from previous)
# ============================================================
class MarketManager:
    def __init__(self):
        self.session = requests.Session()
        self.window_start = 0
        self.slug = ""
        self.question = ""
        self.strike = 0.0
        self.up_token_id = None
        self.down_token_id = None
        self.status = "Initializing..."

    def time_remaining(self) -> float:
        return max(0, (self.window_start + WINDOW_SIZE) - time.time())

    def lifecycle_check(self) -> bool:
        current = (int(time.time()) // WINDOW_SIZE) * WINDOW_SIZE
        if current != self.window_start:
            self.window_start = current
            self.slug = f"btc-updown-15m-{current}"
            self.up_token_id = None
            self.down_token_id = None
            if core.oracle_price > 0:
                self.strike = core.oracle_price
            self.status = "New window..."
            return True
        return False

    def fetch_market(self):
        if self.up_token_id:
            return
        try:
            r = self.session.get(f"{EVENT_SLUG_BASE}{self.slug}", timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data and 'markets' in data:
                    m = data['markets'][0]
                    ids = json.loads(m.get('clobTokenIds', '[]'))
                    outcomes = json.loads(m.get('outcomes', '[]'))
                    if outcomes[0].lower() in ['up', 'yes']:
                        self.up_token_id, self.down_token_id = ids[0], ids[1]
                    else:
                        self.up_token_id, self.down_token_id = ids[1], ids[0]
                    self.question = m.get('question', '')
                    match = re.search(r'\$([\d,]+)', self.question)
                    if match:
                        self.strike = float(match.group(1).replace(',', ''))
                    self.status = "Ready"
        except:
            pass

market = MarketManager()

# ============================================================
# 📡 WEBSOCKET CLIENTS (Same as before, condensed)
# ============================================================
class BinanceSpotClient:
    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        "wss://stream.binance.com:9443/ws/btcusdt@trade",
                        on_message=lambda ws, m: self._on_msg(m)
                    )
                    ws.run_forever(ping_interval=30)
                except: time.sleep(1)
        threading.Thread(target=run, daemon=True).start()
    
    def _on_msg(self, msg):
        try:
            d = json.loads(msg)
            core.update_spot(float(d['p']), int(d['T']))
        except: pass

class BinanceFuturesClient:
    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        "wss://fstream.binance.com/ws/btcusdt@aggTrade",
                        on_message=lambda ws, m: self._on_msg(m)
                    )
                    ws.run_forever(ping_interval=30)
                except: time.sleep(1)
        threading.Thread(target=run, daemon=True).start()
    
    def _on_msg(self, msg):
        try:
            d = json.loads(msg)
            core.update_futures(float(d['p']), int(d['T']))
        except: pass

class PolyDataClient:
    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        "wss://ws-live-data.polymarket.com",
                        on_open=self._on_open,
                        on_message=lambda ws, m: self._on_msg(m)
                    )
                    ws.run_forever(ping_interval=30)
                except: time.sleep(1)
        threading.Thread(target=run, daemon=True).start()
    
    def _on_open(self, ws):
        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices", "type": "*", "filters": '{"symbol":"btcusdt"}'},
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'}
            ]
        }))
    
    def _on_msg(self, msg):
        try:
            d = json.loads(msg)
            if d.get('type') == 'update':
                p = d.get('payload', {})
                if d.get('topic') == 'crypto_prices':
                    core.update_relay(float(p['value']), int(p['timestamp']))
                elif d.get('topic') == 'crypto_prices_chainlink':
                    core.update_oracle(float(p['value']), int(p['timestamp']))
        except: pass

class PolyClobClient:
    def __init__(self):
        self.ws = None
        self.subscribed = set()
    
    def start(self):
        def run():
            while True:
                try:
                    self.ws = websocket.WebSocketApp(
                        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                        on_open=lambda ws: self._subscribe(),
                        on_message=self._on_msg
                    )
                    self.ws.run_forever(ping_interval=30)
                except: time.sleep(1)
        threading.Thread(target=run, daemon=True).start()
    
    def _subscribe(self):
        ids = [i for i in [market.up_token_id, market.down_token_id] if i and i not in self.subscribed]
        if ids and self.ws:
            try:
                self.ws.send(json.dumps({"type": "subscribe", "channel": "market", "assets_ids": ids}))
                self.subscribed.update(ids)
            except: pass
    
    def resubscribe(self):
        self.subscribed.clear()
        self._subscribe()
    
    def _on_msg(self, ws, msg):
        try:
            d = json.loads(msg)
            if d.get('event_type') == 'book':
                side = 'UP' if d.get('asset_id') == market.up_token_id else 'DOWN'
                core.update_orderbook(side, d.get('bids', []), d.get('asks', []))
        except: pass

clob = PolyClobClient()

# ============================================================
# 🎨 RICH UI
# ============================================================
def render_fair_value_panel(estimates: List[FairValueEstimate]) -> Panel:
    """Render fair value calculations from all models"""
    table = Table(box=box.SIMPLE, expand=True)
    table.add_column("MODEL", style="cyan", width=12)
    table.add_column("FV UP", justify="right", style="green", width=8)
    table.add_column("FV DOWN", justify="right", style="red", width=8)
    table.add_column("CONF", justify="right", width=6)
    table.add_column("KEY INPUT", style="dim")
    
    for est in estimates:
        # Get most relevant input to display
        key_input = ""
        if 'd2' in est.inputs and est.inputs['d2']:
            key_input = f"d2={est.inputs['d2']:.2f}"
        elif 'certainty' in est.inputs:
            key_input = f"cert={est.inputs['certainty']:.1%}"
        elif 'slope_per_second' in est.inputs:
            key_input = f"trend=${est.inputs['slope_per_second']:.1f}/s"
        elif 'price_gap' in est.inputs:
            key_input = f"gap=${est.inputs['price_gap']:.0f}"
        
        table.add_row(
            est.model_name,
            f"${est.fair_value_up:.3f}",
            f"${est.fair_value_down:.3f}",
            f"{est.confidence:.0%}",
            key_input
        )
    
    return Panel(table, title="📐 FAIR VALUE MODELS", border_style="cyan")


def render_edge_panel(signals: List[TradingSignal], market_prices: Dict) -> Panel:
    """Render edge analysis and trading signals"""
    content = Text()
    
    # Market prices
    content.append("MARKET PRICES\n", style="bold white underline")
    content.append(f"UP:   Bid ${market_prices.get('up_bid', 0):.4f} | Ask ${market_prices.get('up_ask', 0):.4f}\n", style="green")
    content.append(f"DOWN: Bid ${market_prices.get('down_bid', 0):.4f} | Ask ${market_prices.get('down_ask', 0):.4f}\n\n", style="red")
    
    # Sum check
    up_ask = market_prices.get('up_ask', 0)
    down_ask = market_prices.get('down_ask', 0)
    total = up_ask + down_ask
    content.append(f"SUM (UP+DOWN): ${total:.4f} ", style="white")
    if total < 0.995:
        content.append(f"← ARB ${1-total:.4f}!\n\n", style="bold green")
    else:
        content.append("\n\n", style="dim")
    
    # Signals
    if signals:
        content.append("🎯 TRADING SIGNALS\n", style="bold yellow underline")
        for sig in signals[:3]:
            edge_style = "bold green" if sig.net_edge > 0.05 else "green" if sig.net_edge > 0.03 else "yellow"
            content.append(f"\n{sig.action} {sig.side} @ ${sig.market_price:.4f}\n", style=edge_style)
            content.append(f"  FV: ${sig.fair_value:.3f} | Edge: {sig.net_edge:.1%} net\n", style="white")
            content.append(f"  {sig.reason}\n", style="dim")
            content.append(f"  Confidence: {sig.confidence}\n", style="cyan")
    else:
        content.append("No trading signals (edge < 3%)\n", style="dim")
    
    return Panel(content, title="📊 EDGE ANALYSIS", border_style="yellow")


def render_market_panel() -> Panel:
    """Render market state"""
    time_left = market.time_remaining()
    time_str = f"{int(time_left//60):02d}:{int(time_left%60):02d}"
    
    content = Text()
    
    # Timer
    timer_style = "bold green" if time_left > 60 else "bold yellow" if time_left > 30 else "bold red blink"
    content.append(f"⏱️ {time_str}\n\n", style=timer_style)
    
    # Prices
    content.append(f"Spot:   ${core.spot_price:>10,.2f}\n", style="white")
    content.append(f"Oracle: ${core.oracle_price:>10,.2f}\n", style="cyan")
    content.append(f"Strike: ${market.strike:>10,.2f}\n\n", style="yellow")
    
    # Status
    if market.strike > 0 and core.oracle_price > 0:
        diff = core.oracle_price - market.strike
        if diff > 0:
            content.append(f"📈 UP +${diff:.0f}", style="bold green")
        else:
            content.append(f"📉 DOWN ${diff:.0f}", style="bold red")
    
    # Volatility
    if core.volatility:
        content.append(f"\n\nVol: {core.volatility.realized_vol_5m:.0%} ({core.volatility.vol_regime})", style="dim")
    
    return Panel(content, title="🎯 MARKET", border_style="blue")


def render_volatility_panel() -> Panel:
    """Render volatility information"""
    vol = core.volatility
    
    if not vol:
        return Panel(Text("Calculating...", style="dim"), title="📈 VOLATILITY", border_style="dim")
    
    content = Text()
    content.append(f"1-Min Vol:  {vol.realized_vol_1m:.0%} (annualized)\n", style="white")
    content.append(f"5-Min Vol:  {vol.realized_vol_5m:.0%} (annualized)\n", style="white")
    content.append(f"\nRegime: ", style="dim")
    
    regime_style = {
        'LOW': 'green',
        'NORMAL': 'white',
        'HIGH': 'yellow',
        'EXTREME': 'bold red'
    }.get(vol.vol_regime, 'white')
    content.append(vol.vol_regime, style=regime_style)
    
    # Interpretation
    content.append("\n\n", style="white")
    if vol.vol_regime == 'LOW':
        content.append("→ Prices stable, fair values reliable", style="dim italic")
    elif vol.vol_regime == 'HIGH':
        content.append("→ High uncertainty, wider confidence bands", style="dim italic")
    elif vol.vol_regime == 'EXTREME':
        content.append("→ Extreme moves possible, be cautious!", style="dim italic")
    
    return Panel(content, title="📈 VOLATILITY", border_style="magenta")


def generate_dashboard() -> Layout:
    """Generate complete fair value dashboard"""
    # Lifecycle
    if market.lifecycle_check():
        clob.resubscribe()
    market.fetch_market()
    if market.up_token_id and market.up_token_id not in clob.subscribed:
        clob.resubscribe()
    
    # Calculate volatility
    core.volatility = VolatilityCalculator.calculate_from_core()
    vol = core.volatility.realized_vol_5m if core.volatility else DEFAULT_BTC_VOLATILITY
    
    # Get price history for momentum
    price_history = core.get_price_history('spot', 60)
    
    # Calculate all fair value estimates
    estimates = []
    
    if core.spot_price > 0 and market.strike > 0:
        time_remaining = market.time_remaining()
        oracle_lag = core.get_oracle_lag_seconds()
        
        estimates.append(FairValueModels.distance_model(
            core.spot_price, market.strike, time_remaining, vol
        ))
        
        estimates.append(FairValueModels.time_decay_model(
            core.spot_price, market.strike, time_remaining
        ))
        
        estimates.append(FairValueModels.momentum_model(
            core.spot_price, market.strike, time_remaining, price_history, vol
        ))
        
        estimates.append(FairValueModels.oracle_adjusted_model(
            core.spot_price, core.oracle_price, market.strike,
            time_remaining, oracle_lag, vol
        ))
        
        estimates.append(FairValueModels.ensemble_model(
            core.spot_price, core.oracle_price, market.strike,
            time_remaining, vol, price_history, oracle_lag
        ))
    
    # Get market prices
    market_prices = core.get_best_prices()
    
    # Generate signals using ensemble model
    signals = []
    ensemble = next((e for e in estimates if e.model_name == "ENSEMBLE"), None)
    if ensemble:
        signals = EdgeCalculator.generate_signals(ensemble, market_prices)
    
    # Build layout
    layout = Layout()
    
    header = Text()
    header.append("📐 BTC 15-MIN FAIR VALUE CALCULATOR ", style="bold cyan")
    header.append(datetime.now().strftime("%H:%M:%S"), style="dim")
    
    layout.split(
        Layout(Panel(header, box=box.MINIMAL), size=3),
        Layout(name="main"),
    )
    
    layout["main"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=1),
    )
    
    layout["left"].split(
        Layout(render_fair_value_panel(estimates), ratio=1),
        Layout(render_edge_panel(signals, market_prices), ratio=1),
    )
    
    layout["right"].split(
        Layout(render_market_panel()),
        Layout(render_volatility_panel()),
    )
    
    return layout

# ============================================================
# 🚀 MAIN
# ============================================================
def main():
    console = Console()
    
    console.print("\n[bold cyan]📐 BTC 15-MIN FAIR VALUE CALCULATOR[/bold cyan]")
    console.print("[dim]Real-time probability estimation for fast-resolving markets[/dim]\n")
    
    # Check for scipy
    try:
        from scipy import stats
        console.print("[green]✓ scipy loaded[/green]")
    except ImportError:
        console.print("[red]✗ scipy required: pip install scipy[/red]")
        return
    
    # Start streams
    console.print("[yellow]Starting price feeds...[/yellow]")
    BinanceSpotClient().start()
    BinanceFuturesClient().start()
    PolyDataClient().start()
    clob.start()
    
    # Init market
    market.lifecycle_check()
    market.fetch_market()
    
    console.print("[bold green]Starting calculator...[/bold green]")
    time.sleep(2)
    
    try:
        with Live(generate_dashboard(), refresh_per_second=10, screen=True) as live:
            while True:
                live.update(generate_dashboard())
                time.sleep(REFRESH_RATE)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")


if __name__ == "__main__":
    main()