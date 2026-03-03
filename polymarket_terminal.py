#!/usr/bin/env python3
"""
Polymarket Trading Terminal - FIXED VERSION
Complete real-time trading terminal with multi-strategy consensus
"""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Tuple

import websockets
import requests

from threading import Lock

# Rich library for terminal UI
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text
from rich import box

# Data processing
import numpy as np


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class PriceData:
    """Price data from various sources"""
    binance_spot: float = 0.0
    rtds_binance: float = 0.0
    rtds_oracle: float = 0.0
    futures_perp: float = 0.0
    futures_15m: float = 0.0
    futures_1h: float = 0.0
    timestamp: float = time.time()

    @property
    def lag_rtds_binance(self):
        return self.rtds_binance - self.binance_spot if self.binance_spot > 0 else 0

    @property
    def lag_rtds_oracle(self):
        return self.rtds_oracle - self.binance_spot if self.binance_spot > 0 else 0


@dataclass
class OrderbookData:
    """Orderbook data for UP/DOWN tokens"""
    up_bid: float = 0.0
    up_ask: float = 0.0
    up_bid_size: float = 0.0
    up_ask_size: float = 0.0
    down_bid: float = 0.0
    down_ask: float = 0.0
    down_bid_size: float = 0.0
    down_ask_size: float = 0.0
    up_updates: int = 0
    down_updates: int = 0
    timestamp: float = time.time()

    @property
    def up_mid(self):
        if self.up_bid > 0 and self.up_ask > 0:
            return (self.up_bid + self.up_ask) / 2
        return 0.0

    @property
    def down_mid(self):
        if self.down_bid > 0 and self.down_ask > 0:
            return (self.down_bid + self.down_ask) / 2
        return 0.0

    @property
    def market_sum(self):
        return self.up_mid + self.down_mid if self.up_mid > 0 and self.down_mid > 0 else 0.0

    @property
    def up_spread(self):
        return self.up_ask - self.up_bid if self.up_bid > 0 and self.up_ask > 0 else 0.0

    @property
    def down_spread(self):
        return self.down_ask - self.down_bid if self.down_bid > 0 and self.down_ask > 0 else 0.0


@dataclass
class OrderFlowData:
    """Order flow metrics from Binance"""
    spot_buy_volume: float = 0.0
    spot_sell_volume: float = 0.0
    futures_buy_volume: float = 0.0
    futures_sell_volume: float = 0.0
    spot_cvd: float = 0.0
    futures_cvd: float = 0.0
    large_buy_count: int = 0
    large_sell_count: int = 0
    funding_rate: float = 0.0
    open_interest: float = 0.0
    timestamp: float = time.time()

    @property
    def spot_ratio(self):
        if self.spot_sell_volume > 0:
            return self.spot_buy_volume / self.spot_sell_volume
        return 0.0

    @property
    def futures_ratio(self):
        if self.futures_sell_volume > 0:
            return self.futures_buy_volume / self.futures_sell_volume
        return 0.0


@dataclass
class FairValue:
    """Fair value calculation result"""
    method: str
    up_fair: float
    down_fair: float
    up_edge: float
    down_edge: float
    confidence: float

    def __str__(self):
        return (
            f"{self.method}: UP={self.up_fair:.4f} ({self.up_edge:+.2%}) "
            f"DOWN={self.down_fair:.4f} ({self.down_edge:+.2%}) "
            f"Conf={self.confidence:.0%}"
        )


@dataclass
class StrategySignal:
    """Trading strategy signal"""
    strategy_name: str
    status: str  # ACTIVE, IDLE, WATCH
    signal: str  # BUY, SELL, WAIT
    side: str    # UP, DOWN, or empty
    confidence: float
    edge: float
    reason: str

    def __str__(self):
        return (
            f"{self.strategy_name}: {self.signal} {self.side} "
            f"(Conf={self.confidence:.0%}, Edge={self.edge:+.2%})"
        )


@dataclass
class EventData:
    """Current event information"""
    event_id: str = ""
    slug: str = ""
    strike_price: float = 0.0
    start_time: float = 0.0
    end_time: float = 0.0
    up_token_id: str = ""
    down_token_id: str = ""
    status: str = "UNKNOWN"

    @property
    def seconds_remaining(self):
        return max(0, self.end_time - time.time())

    @property
    def time_elapsed(self):
        return time.time() - self.start_time if self.start_time > 0 else 0


# ============================================================================
# FAIR VALUE CALCULATOR
# ============================================================================

class FairValueCalculator:
    """Calculate fair values using multiple methods"""

    @staticmethod
    def oracle_based(binance_spot: float, strike: float, seconds_remaining: float) -> Tuple[float, float, float]:
        if not binance_spot or not strike or seconds_remaining <= 0:
            return 0.5, 0.5, 0.0

        distance = binance_spot - strike
        time_factor = 1 - (seconds_remaining / 900)
        time_factor = max(0.1, min(1.0, time_factor))

        normalized_distance = distance / 100
        prob_up = 0.5 + np.tanh(normalized_distance * time_factor) * 0.49
        prob_down = 1 - prob_up

        confidence = min(0.95, 0.5 + abs(distance) / 200 * time_factor)

        return prob_up, prob_down, confidence

    @staticmethod
    def market_sum_normalized(up_mid: float, down_mid: float) -> Tuple[float, float, float]:
        if not up_mid or not down_mid:
            return 0.5, 0.5, 0.0

        market_sum = up_mid + down_mid
        if market_sum == 0:
            return 0.5, 0.5, 0.0

        fair_up = up_mid / market_sum
        fair_down = down_mid / market_sum

        sum_deviation = abs(market_sum - 1.0)
        confidence = min(0.9, sum_deviation * 10)

        return fair_up, fair_down, confidence

    @staticmethod
    def combined(binance_spot: float, strike: float, seconds_remaining: float,
                 up_mid: float, down_mid: float) -> Tuple[float, float, float]:
        oracle_up, oracle_down, oracle_conf = FairValueCalculator.oracle_based(
            binance_spot, strike, seconds_remaining
        )
        market_up, market_down, market_conf = FairValueCalculator.market_sum_normalized(
            up_mid, down_mid
        )

        if oracle_conf == 0 or market_conf == 0:
            return 0.5, 0.5, 0.0

        fair_up = 0.7 * oracle_up + 0.3 * market_up
        fair_down = 0.7 * oracle_down + 0.3 * market_down
        confidence = (oracle_conf + market_conf) / 2

        return fair_up, fair_down, confidence

    @staticmethod
    def time_weighted(binance_spot: float, strike: float, seconds_remaining: float) -> Tuple[float, float, float]:
        if not binance_spot or not strike or seconds_remaining <= 0:
            return 0.5, 0.5, 0.0

        distance = binance_spot - strike
        time_factor = 1 - (seconds_remaining / 900)
        time_factor = max(0.1, min(1.0, time_factor))
        time_weight = time_factor ** 2

        normalized_distance = distance / 100
        prob_up = 0.5 + np.tanh(normalized_distance * time_weight * 1.5) * 0.49
        prob_down = 1 - prob_up

        confidence = min(0.95, 0.3 + time_factor * 0.65)

        return prob_up, prob_down, confidence

    @staticmethod
    def futures_adjusted(binance_spot: float, futures_perp: float, strike: float,
                         seconds_remaining: float, funding_rate: float) -> Tuple[float, float, float]:
        if not binance_spot or not strike:
            return 0.5, 0.5, 0.0

        ref_price = futures_perp if futures_perp > 0 else binance_spot

        distance = ref_price - strike
        time_factor = 1 - (seconds_remaining / 900) if seconds_remaining > 0 else 0.9
        time_factor = max(0.1, min(1.0, time_factor))

        funding_adjustment = funding_rate * 10

        normalized_distance = (distance / 100) + funding_adjustment
        prob_up = 0.5 + np.tanh(normalized_distance * time_factor) * 0.49
        prob_down = 1 - prob_up

        confidence = min(0.9, 0.5 + abs(distance) / 200 * time_factor)

        return prob_up, prob_down, confidence

    @staticmethod
    def order_flow_adjusted(binance_spot: float, strike: float, seconds_remaining: float,
                            flow_ratio: float) -> Tuple[float, float, float]:
        if not binance_spot or not strike:
            return 0.5, 0.5, 0.0

        distance = binance_spot - strike
        time_factor = 1 - (seconds_remaining / 900) if seconds_remaining > 0 else 0.9
        time_factor = max(0.1, min(1.0, time_factor))

        flow_adjustment = np.log(flow_ratio) * 0.1 if flow_ratio > 0 else 0

        normalized_distance = (distance / 100) + flow_adjustment
        prob_up = 0.5 + np.tanh(normalized_distance * time_factor) * 0.49
        prob_down = 1 - prob_up

        confidence = min(
            0.9,
            0.5 + abs(distance) / 200 * time_factor + abs(flow_adjustment) * 0.5
        )

        return prob_up, prob_down, confidence


# ============================================================================
# TRADING STRATEGIES
# ============================================================================

class TradingStrategy:
    def __init__(self, name: str):
        self.name = name

    def evaluate(self, prices, orderbook, event, fair_values, order_flow):
        raise NotImplementedError


class OracleLagArbitrage(TradingStrategy):
    """Oracle lag arbitrage strategy"""

    def __init__(self, min_edge=0.05, min_time=300, max_time=840):
        super().__init__("Oracle Lag Arb")
        self.min_edge = min_edge
        self.min_time = min_time
        self.max_time = max_time

    def evaluate(self, prices, orderbook, event, fair_values, order_flow):
        seconds_remaining = event.seconds_remaining

        if seconds_remaining < self.min_time or seconds_remaining > self.max_time:
            return StrategySignal(self.name, "IDLE", "WAIT", "", 0.0, 0.0, "Time window")

        if "oracle" not in fair_values:
            return StrategySignal(self.name, "IDLE", "WAIT", "", 0.0, 0.0, "No data")

        fv = fair_values["oracle"]

        if fv.up_edge > self.min_edge:
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "UP",
                fv.confidence, fv.up_edge, f"Lag: ${prices.lag_rtds_oracle:.2f}"
            )

        if fv.down_edge > self.min_edge:
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "DOWN",
                fv.confidence, fv.down_edge, f"Lag: ${prices.lag_rtds_oracle:.2f}"
            )

        return StrategySignal(self.name, "WATCH", "WAIT", "", fv.confidence, 0.0, "Low edge")


class MarketSumArbitrage(TradingStrategy):
    """Market sum arbitrage strategy"""

    def __init__(self, sum_threshold=0.02, min_edge=0.03):
        super().__init__("Market Sum Arb")
        self.sum_threshold = sum_threshold
        self.min_edge = min_edge

    def evaluate(self, prices, orderbook, event, fair_values, order_flow):
        market_sum = orderbook.market_sum

        if market_sum == 0:
            return StrategySignal(self.name, "IDLE", "WAIT", "", 0.0, 0.0, "No data")

        sum_deviation = abs(market_sum - 1.0)

        if sum_deviation < 0.01:
            return StrategySignal(self.name, "IDLE", "WAIT", "", 0.45, 0.0, "Sum OK")

        if "market_sum" not in fair_values:
            return StrategySignal(self.name, "IDLE", "WAIT", "", 0.0, 0.0, "No FV")

        fv = fair_values["market_sum"]

        if fv.up_edge > self.min_edge:
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "UP",
                fv.confidence, fv.up_edge, f"Sum: {market_sum:.4f}"
            )

        if fv.down_edge > self.min_edge:
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "DOWN",
                fv.confidence, fv.down_edge, f"Sum: {market_sum:.4f}"
            )

        return StrategySignal(self.name, "WATCH", "WAIT", "", fv.confidence, sum_deviation, "Low edge")


class HighLagAggressive(TradingStrategy):
    """High lag aggressive strategy"""

    def __init__(self, min_lag=50, min_edge=0.06):
        super().__init__("High Lag Aggr")
        self.min_lag = min_lag
        self.min_edge = min_edge

    def evaluate(self, prices, orderbook, event, fair_values, order_flow):
        lag = abs(prices.lag_rtds_oracle)

        if lag < self.min_lag:
            return StrategySignal(self.name, "IDLE", "WAIT", "", 0.0, 0.0, f"Lag ${lag:.0f}")

        if "time_weighted" not in fair_values:
            return StrategySignal(self.name, "IDLE", "WAIT", "", 0.0, 0.0, "No data")

        fv = fair_values["time_weighted"]

        if prices.lag_rtds_oracle > self.min_lag and fv.up_edge > self.min_edge:
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "UP",
                fv.confidence, fv.up_edge, f"HIGH LAG ${lag:.0f}"
            )

        if prices.lag_rtds_oracle < -self.min_lag and fv.down_edge > self.min_edge:
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "DOWN",
                fv.confidence, fv.down_edge, f"HIGH LAG ${lag:.0f}"
            )

        return StrategySignal(self.name, "WATCH", "WAIT", "", fv.confidence, lag / 100, "Edge low")


class CombinedStrategy(TradingStrategy):
    """Combined oracle + market strategy"""

    def __init__(self, min_edge=0.04):
        super().__init__("Combined O+M")
        self.min_edge = min_edge

    def evaluate(self, prices, orderbook, event, fair_values, order_flow):
        if "combined" not in fair_values:
            return StrategySignal(self.name, "IDLE", "WAIT", "", 0.0, 0.0, "No data")

        fv = fair_values["combined"]

        if fv.up_edge > self.min_edge and fv.down_edge < 0:
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "UP",
                fv.confidence, fv.up_edge, "Both agree"
            )

        if fv.down_edge > self.min_edge and fv.up_edge < 0:
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "DOWN",
                fv.confidence, fv.down_edge, "Both agree"
            )

        return StrategySignal(self.name, "WATCH", "WAIT", "", fv.confidence, 0.0, "No consensus")


class MomentumStrategy(TradingStrategy):
    """Momentum following strategy"""

    def __init__(self):
        super().__init__("Momentum")
        self.price_history = deque(maxlen=30)

    def evaluate(self, prices, orderbook, event, fair_values, order_flow):
        self.price_history.append(prices.binance_spot)

        if len(self.price_history) < 20:
            return StrategySignal(self.name, "IDLE", "WAIT", "", 0.0, 0.0, "Building history")

        recent = np.array(list(self.price_history)[-10:])
        older = np.array(list(self.price_history)[-20:-10])

        recent_avg = np.mean(recent)
        older_avg = np.mean(older)

        momentum = (recent_avg - older_avg) / older_avg

        distance = prices.binance_spot - event.strike_price

        confidence = min(0.85, abs(momentum) * 100 + 0.3)
        edge = abs(momentum) * 2

        if momentum > 0.001 and distance > 0:
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "UP",
                confidence, edge, f"Mom: {momentum:+.3%}"
            )

        if momentum < -0.001 and distance < 0:
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "DOWN",
                confidence, edge, f"Mom: {momentum:+.3%}"
            )

        return StrategySignal(self.name, "WATCH", "WAIT", "", confidence, 0.0, "Weak momentum")


class OrderFlowStrategy(TradingStrategy):
    """Order flow imbalance strategy"""

    def __init__(self, min_ratio=1.5):
        super().__init__("Order Flow")
        self.min_ratio = min_ratio

    def evaluate(self, prices, orderbook, event, fair_values, order_flow):
        ratio = (order_flow.spot_ratio + order_flow.futures_ratio) / 2

        if ratio == 0:
            return StrategySignal(self.name, "IDLE", "WAIT", "", 0.0, 0.0, "No flow data")

        if "order_flow" not in fair_values:
            return StrategySignal(self.name, "IDLE", "WAIT", "", 0.0, 0.0, "No FV")

        fv = fair_values["order_flow"]

        confidence = min(0.90, 0.5 + abs(np.log(ratio)) * 0.2) if ratio > 0 else 0.5

        if ratio > self.min_ratio:
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "UP",
                confidence, fv.up_edge, f"Ratio: {ratio:.2f}x"
            )

        if ratio < (1 / self.min_ratio):
            return StrategySignal(
                self.name, "ACTIVE", "BUY", "DOWN",
                confidence, fv.down_edge, f"Ratio: {ratio:.2f}x"
            )

        return StrategySignal(self.name, "WATCH", "WAIT", "", confidence, 0.0, "Balanced")


# ============================================================================
# DATA MANAGER
# ============================================================================

class DataManager:
    """Manages all data feeds and calculations"""

    def __init__(self):
        self.prices = PriceData()
        self.orderbook = OrderbookData()
        self.order_flow = OrderFlowData()
        self.event = EventData()

        self.fair_values: Dict[str, FairValue] = {}
        self.signals: List[StrategySignal] = []

        self.lock = Lock()

        self.tick_count = 0
        self.start_time = time.time()

        self.strategies = [
            OracleLagArbitrage(),
            MarketSumArbitrage(),
            CombinedStrategy(),
            HighLagAggressive(),
            MomentumStrategy(),
            OrderFlowStrategy(),
        ]

        self.spot_trades = deque(maxlen=100)
        self.futures_trades = deque(maxlen=100)

    def update_binance_spot(self, price: float):
        with self.lock:
            self.prices.binance_spot = price
            self.prices.timestamp = time.time()

    def update_rtds_binance(self, price: float):
        with self.lock:
            self.prices.rtds_binance = price

    def update_rtds_oracle(self, price: float):
        with self.lock:
            self.prices.rtds_oracle = price

    def update_futures(self, perp: float = 0, m15: float = 0, h1: float = 0):
        with self.lock:
            if perp > 0:
                self.prices.futures_perp = perp
            if m15 > 0:
                self.prices.futures_15m = m15
            if h1 > 0:
                self.prices.futures_1h = h1

    def update_orderbook_up(self, bid: float, ask: float, bid_size: float, ask_size: float):
        with self.lock:
            self.orderbook.up_bid = bid
            self.orderbook.up_ask = ask
            self.orderbook.up_bid_size = bid_size
            self.orderbook.up_ask_size = ask_size
            self.orderbook.up_updates += 1
            self.orderbook.timestamp = time.time()

    def update_orderbook_down(self, bid: float, ask: float, bid_size: float, ask_size: float):
        with self.lock:
            self.orderbook.down_bid = bid
            self.orderbook.down_ask = ask
            self.orderbook.down_bid_size = bid_size
            self.orderbook.down_ask_size = ask_size
            self.orderbook.down_updates += 1

    def add_spot_trade(self, price: float, quantity: float, is_buyer_maker: bool):
        with self.lock:
            self.spot_trades.append({
                "price": price,
                "quantity": quantity,
                "side": "sell" if is_buyer_maker else "buy",
                "timestamp": time.time(),
            })
            self._update_order_flow()

    def add_futures_trade(self, price: float, quantity: float, is_buyer_maker: bool):
        with self.lock:
            self.futures_trades.append({
                "price": price,
                "quantity": quantity,
                "side": "sell" if is_buyer_maker else "buy",
                "timestamp": time.time(),
            })
            self._update_order_flow()

    def _update_order_flow(self):
        now = time.time()
        lookback = 60

        spot_buy = sum(
            t["quantity"] * t["price"] for t in self.spot_trades
            if t["side"] == "buy" and now - t["timestamp"] < lookback
        )
        spot_sell = sum(
            t["quantity"] * t["price"] for t in self.spot_trades
            if t["side"] == "sell" and now - t["timestamp"] < lookback
        )

        fut_buy = sum(
            t["quantity"] * t["price"] for t in self.futures_trades
            if t["side"] == "buy" and now - t["timestamp"] < lookback
        )
        fut_sell = sum(
            t["quantity"] * t["price"] for t in self.futures_trades
            if t["side"] == "sell" and now - t["timestamp"] < lookback
        )

        self.order_flow.spot_buy_volume = spot_buy
        self.order_flow.spot_sell_volume = spot_sell
        self.order_flow.futures_buy_volume = fut_buy
        self.order_flow.futures_sell_volume = fut_sell

        self.order_flow.spot_cvd += (spot_buy - spot_sell)
        self.order_flow.futures_cvd += (fut_buy - fut_sell)

        self.order_flow.large_buy_count = sum(
            1
            for t in list(self.spot_trades) + list(self.futures_trades)
            if t["side"] == "buy" and t["quantity"] * t["price"] > 100000
        )
        self.order_flow.large_sell_count = sum(
            1
            for t in list(self.spot_trades) + list(self.futures_trades)
            if t["side"] == "sell" and t["quantity"] * t["price"] > 100000
        )

    def calculate_fair_values(self):
        with self.lock:
            fvs: Dict[str, FairValue] = {}

            up_mid = self.orderbook.up_mid
            down_mid = self.orderbook.down_mid
            spot = self.prices.binance_spot
            strike = self.event.strike_price
            remaining = self.event.seconds_remaining

            if spot > 0 and strike > 0 and remaining > 0:
                up_fair, down_fair, conf = FairValueCalculator.oracle_based(spot, strike, remaining)
                fvs["oracle"] = FairValue(
                    "Oracle-Based",
                    up_fair,
                    down_fair,
                    up_fair - up_mid if up_mid > 0 else 0,
                    down_fair - down_mid if down_mid > 0 else 0,
                    conf,
                )

                up_fair, down_fair, conf = FairValueCalculator.time_weighted(spot, strike, remaining)
                fvs["time_weighted"] = FairValue(
                    "Time-Weighted",
                    up_fair,
                    down_fair,
                    up_fair - up_mid if up_mid > 0 else 0,
                    down_fair - down_mid if down_mid > 0 else 0,
                    conf,
                )

                up_fair, down_fair, conf = FairValueCalculator.futures_adjusted(
                    spot, self.prices.futures_perp, strike, remaining, self.order_flow.funding_rate
                )
                fvs["futures"] = FairValue(
                    "Futures-Adj",
                    up_fair,
                    down_fair,
                    up_fair - up_mid if up_mid > 0 else 0,
                    down_fair - down_mid if down_mid > 0 else 0,
                    conf,
                )

                flow_ratio = self.order_flow.spot_ratio if self.order_flow.spot_ratio > 0 else 1.0
                up_fair, down_fair, conf = FairValueCalculator.order_flow_adjusted(
                    spot, strike, remaining, flow_ratio
                )
                fvs["order_flow"] = FairValue(
                    "Order Flow",
                    up_fair,
                    down_fair,
                    up_fair - up_mid if up_mid > 0 else 0,
                    down_fair - down_mid if down_mid > 0 else 0,
                    conf,
                )

            if up_mid > 0 and down_mid > 0:
                up_fair, down_fair, conf = FairValueCalculator.market_sum_normalized(up_mid, down_mid)
                fvs["market_sum"] = FairValue(
                    "Market Sum",
                    up_fair,
                    down_fair,
                    up_fair - up_mid,
                    down_fair - down_mid,
                    conf,
                )

                if spot > 0 and strike > 0 and remaining > 0:
                    up_fair, down_fair, conf = FairValueCalculator.combined(
                        spot, strike, remaining, up_mid, down_mid
                    )
                    fvs["combined"] = FairValue(
                        "Combined",
                        up_fair,
                        down_fair,
                        up_fair - up_mid,
                        down_fair - down_mid,
                        conf,
                    )

            self.fair_values = fvs

    def evaluate_strategies(self):
        with self.lock:
            signals: List[StrategySignal] = []
            for strategy in self.strategies:
                signal = strategy.evaluate(
                    self.prices,
                    self.orderbook,
                    self.event,
                    self.fair_values,
                    self.order_flow,
                )
                signals.append(signal)

            self.signals = signals

    def get_consensus_signal(self) -> Tuple[str, str, float, float]:
        if not self.signals:
            return "WAIT", "", 0.0, 0.0

        active_signals = [s for s in self.signals if s.signal == "BUY"]
        if len(active_signals) == 0:
            return "WAIT", "", 0.0, 0.0

        up_votes = sum(1 for s in active_signals if s.side == "UP")
        down_votes = sum(1 for s in active_signals if s.side == "DOWN")

        if up_votes > down_votes:
            side = "UP"
            relevant = [s for s in active_signals if s.side == "UP"]
        elif down_votes > up_votes:
            side = "DOWN"
            relevant = [s for s in active_signals if s.side == "DOWN"]
        else:
            return "WAIT", "", 0.0, 0.0

        avg_confidence = float(np.mean([s.confidence for s in relevant]))
        avg_edge = float(np.mean([s.edge for s in relevant]))

        agreement_ratio = len(relevant) / len(self.strategies)

        if agreement_ratio >= 0.6 and avg_confidence >= 0.75:
            signal = "STRONG BUY"
        elif agreement_ratio >= 0.4 and avg_confidence >= 0.6:
            signal = "BUY"
        else:
            signal = "WEAK BUY"

        return signal, side, avg_confidence, avg_edge

    def tick(self):
        self.tick_count += 1
        self.calculate_fair_values()
        self.evaluate_strategies()


# ============================================================================
# TERMINAL UI RENDERER
# ============================================================================

class TerminalRenderer:
    def __init__(self, data_manager: DataManager):
        self.dm = data_manager
        self.console = Console()

    def make_header(self) -> Panel:
        event = self.dm.event

        if event.strike_price == 0:
            status_text = Text("WAITING FOR EVENT...", style="yellow bold")
        else:
            remaining = event.seconds_remaining
            mins = int(remaining // 60)
            secs = int(remaining % 60)

            time_text = f"{mins}m {secs}s" if remaining > 0 else "ENDED"
            time_style = "green" if remaining > 300 else "yellow" if remaining > 60 else "red"

            status_text = Text()
            status_text.append(f"EVENT: {event.slug[:40]}", style="cyan bold")
            status_text.append("  |  TIME LEFT: ", style="white")
            status_text.append(time_text, style=time_style)
            status_text.append(f"  |  STRIKE: ${event.strike_price:,.2f}", style="yellow bold")

        return Panel(status_text, box=box.DOUBLE, border_style="bright_blue")

    def make_prices_panel(self) -> Panel:
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")

        table.add_column("SOURCE", style="cyan", width=16)
        table.add_column("PRICE", justify="right", width=12)
        table.add_column("LAG", justify="right", width=10)
        table.add_column("STATUS", width=8)

        prices = self.dm.prices

        table.add_row(
            "Binance Spot",
            f"${prices.binance_spot:,.2f}" if prices.binance_spot > 0 else "---",
            "BASE",
            "🟢" if prices.binance_spot > 0 else "🔴",
        )

        lag_b = prices.lag_rtds_binance
        table.add_row(
            "RTDS Binance",
            f"${prices.rtds_binance:,.2f}" if prices.rtds_binance > 0 else "---",
            f"${lag_b:+.2f}" if prices.rtds_binance > 0 else "---",
            "🟢" if prices.rtds_binance > 0 else "🔴",
        )

        lag_o = prices.lag_rtds_oracle
        lag_text = Text(f"${lag_o:+.2f}" if prices.rtds_oracle > 0 else "---")
        if abs(lag_o) >= 50:
            lag_text.stylize("bold red")
        elif abs(lag_o) >= 20:
            lag_text.stylize("yellow")

        table.add_row(
            "RTDS Oracle",
            f"${prices.rtds_oracle:,.2f}" if prices.rtds_oracle > 0 else "---",
            lag_text,
            "🟢" if prices.rtds_oracle > 0 else "🔴",
        )

        if prices.futures_perp > 0:
            table.add_row(
                "Futures Perp",
                f"${prices.futures_perp:,.2f}",
                f"${prices.futures_perp - prices.binance_spot:+.2f}",
                "🟢",
            )

        return Panel(table, title="📊 PRICE FEEDS", border_style="blue")

    def make_orderbook_panel(self) -> Panel:
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")

        table.add_column("TOKEN", width=12)
        table.add_column("BID", justify="right", width=10)
        table.add_column("MID", justify="right", width=10)
        table.add_column("ASK", justify="right", width=10)
        table.add_column("SPREAD", justify="right", width=10)
        table.add_column("UPDATES", justify="right", width=10)

        ob = self.dm.orderbook

        up_mid_text = Text(f"{ob.up_mid:.4f}" if ob.up_mid > 0 else "---")
        if ob.up_mid > 0.6:
            up_mid_text.stylize("green bold")
        elif ob.up_mid > 0.5:
            up_mid_text.stylize("green")

        table.add_row(
            "UP",
            f"{ob.up_bid:.4f}" if ob.up_bid > 0 else "---",
            up_mid_text,
            f"{ob.up_ask:.4f}" if ob.up_ask > 0 else "---",
            f"{ob.up_spread*100:.2f}%" if ob.up_spread > 0 else "---",
            f"{ob.up_updates:,}",
        )

        down_mid_text = Text(f"{ob.down_mid:.4f}" if ob.down_mid > 0 else "---")
        if ob.down_mid > 0.6:
            down_mid_text.stylize("red bold")
        elif ob.down_mid > 0.5:
            down_mid_text.stylize("red")

        table.add_row(
            "DOWN",
            f"{ob.down_bid:.4f}" if ob.down_bid > 0 else "---",
            down_mid_text,
            f"{ob.down_ask:.4f}" if ob.down_ask > 0 else "---",
            f"{ob.down_spread*100:.2f}%" if ob.down_spread > 0 else "---",
            f"{ob.down_updates:,}",
        )

        sum_style = (
            "green"
            if abs(ob.market_sum - 1.0) < 0.02
            else "yellow"
            if abs(ob.market_sum - 1.0) < 0.05
            else "red"
        )
        sum_text = Text(f"{ob.market_sum:.4f}" if ob.market_sum > 0 else "---")
        sum_text.stylize(sum_style)

        table.add_row("SUM", "", sum_text, "", "", "")

        return Panel(table, title="💹 ORDERBOOK", border_style="magenta")

    def make_fair_values_panel(self) -> Panel:
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")

        table.add_column("METHOD", width=16)
        table.add_column("UP FAIR", justify="right", width=10)
        table.add_column("UP EDGE", justify="right", width=10)
        table.add_column("DOWN FAIR", justify="right", width=10)
        table.add_column("DOWN EDGE", justify="right", width=10)
        table.add_column("CONF", width=12)

        fvs = self.dm.fair_values

        for fv in fvs.values():
            conf_pct = int(fv.confidence * 100)
            filled = int(fv.confidence * 5)
            conf_bar = "█" * filled + "░" * (5 - filled)
            conf_text = Text(f"{conf_bar} {conf_pct}%")

            up_edge_text = Text(f"{fv.up_edge:+.2%}")
            if fv.up_edge > 0.05:
                up_edge_text.stylize("green bold")
            elif fv.up_edge > 0.02:
                up_edge_text.stylize("green")

            down_edge_text = Text(f"{fv.down_edge:+.2%}")
            if fv.down_edge > 0.05:
                down_edge_text.stylize("red bold")
            elif fv.down_edge > 0.02:
                down_edge_text.stylize("red")

            table.add_row(
                fv.method,
                f"{fv.up_fair:.4f}",
                up_edge_text,
                f"{fv.down_fair:.4f}",
                down_edge_text,
                conf_text,
            )

        if len(fvs) > 0:
            avg_up = float(np.mean([fv.up_fair for fv in fvs.values()]))
            avg_down = float(np.mean([fv.down_fair for fv in fvs.values()]))
            avg_conf = float(np.mean([fv.confidence for fv in fvs.values()]))

            up_mid = self.dm.orderbook.up_mid
            down_mid = self.dm.orderbook.down_mid

            consensus_up_edge = avg_up - up_mid if up_mid > 0 else 0
            consensus_down_edge = avg_down - down_mid if down_mid > 0 else 0

            filled = int(avg_conf * 5)
            conf_bar = "█" * filled + "░" * (5 - filled)

            consensus_up_text = Text(f"{consensus_up_edge:+.2%}")
            consensus_up_text.stylize("bold green" if consensus_up_edge > 0.05 else "green")

            consensus_down_text = Text(f"{consensus_down_edge:+.2%}")
            consensus_down_text.stylize("bold red" if consensus_down_edge > 0.05 else "red")

            table.add_row("─" * 16, "─" * 10, "─" * 10, "─" * 10, "─" * 10, "─" * 12)
            table.add_row(
                Text("CONSENSUS", style="bold yellow"),
                Text(f"{avg_up:.4f}", style="bold"),
                consensus_up_text,
                Text(f"{avg_down:.4f}", style="bold"),
                consensus_down_text,
                Text(f"{conf_bar} {int(avg_conf*100)}%", style="bold"),
            )

        return Panel(table, title="🧮 FAIR VALUES", border_style="yellow")

    def make_strategies_panel(self) -> Panel:
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")

        table.add_column("STRATEGY", width=16)
        table.add_column("STATUS", width=10)
        table.add_column("SIGNAL", width=10)
        table.add_column("SIDE", width=6)
        table.add_column("CONF", width=12)
        table.add_column("EDGE", justify="right", width=10)
        table.add_column("REASON", width=18)

        for sig in self.dm.signals:
            status_icon = "🟢" if sig.status == "ACTIVE" else "🟡" if sig.status == "WATCH" else "⚪"
            signal_icon = "🟢" if sig.signal == "BUY" else "🔴" if sig.signal == "SELL" else "⚪"

            filled = int(sig.confidence * 5)
            conf_bar = "█" * filled + "░" * (5 - filled)
            conf_text = f"{conf_bar} {int(sig.confidence*100)}%"

            edge_text = Text(f"{sig.edge:+.1%}" if sig.edge != 0 else "---")
            if sig.edge > 0.05:
                edge_text.stylize("bold green" if sig.side == "UP" else "bold red")
            elif sig.edge > 0.02:
                edge_text.stylize("green" if sig.side == "UP" else "red")

            table.add_row(
                sig.strategy_name,
                f"{status_icon} {sig.status}",
                f"{signal_icon} {sig.signal}",
                sig.side if sig.side else "---",
                conf_text,
                edge_text,
                sig.reason[:18],
            )

        return Panel(table, title="⚡ STRATEGIES", border_style="green")

    def make_consensus_panel(self) -> Panel:
        signal, side, confidence, edge = self.dm.get_consensus_signal()

        signal_text = Text()
        if signal == "STRONG BUY":
            signal_text.append("⚡ STRONG BUY ", style="bold white on green")
            signal_text.append(side, style="bold yellow")
        elif signal == "BUY":
            signal_text.append("🟢 BUY ", style="bold green")
            signal_text.append(side, style="bold yellow")
        elif signal == "WEAK BUY":
            signal_text.append("🟡 WEAK BUY ", style="bold yellow")
            signal_text.append(side, style="yellow")
        else:
            signal_text.append("⚪ WAIT", style="bold white")

        details = Text()
        details.append(f"  Confidence: {confidence:.0%}  |  Edge: {edge:+.2%}", style="white")

        combined = Text()
        combined.append(signal_text)
        combined.append("\n")
        combined.append(details)

        return Panel(combined, title="🎯 CONSENSUS SIGNAL", border_style="bright_magenta", box=box.DOUBLE)

    def make_stats_panel(self) -> Panel:
        runtime = time.time() - self.dm.start_time
        mins = int(runtime // 60)
        secs = int(runtime % 60)

        stats_text = Text()
        stats_text.append(f"Runtime: {mins}m {secs}s  |  ", style="cyan")
        stats_text.append(f"Ticks: {self.dm.tick_count:,}  |  ", style="cyan")
        if runtime > 0:
            stats_text.append(f"Rate: {self.dm.tick_count/runtime:.1f}/s", style="cyan")

        return Panel(stats_text, title="📈 STATISTICS", border_style="cyan")

    def render(self) -> Layout:
        layout = Layout()

        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=5),
        )

        layout["main"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1),
        )

        layout["left"].split_column(
            Layout(name="prices", ratio=1),
            Layout(name="orderbook", ratio=1),
        )

        layout["right"].split_column(
            Layout(name="fair_values", ratio=1),
            Layout(name="strategies", ratio=1),
        )

        layout["footer"].split_column(
            Layout(name="consensus", size=3),
            Layout(name="stats", size=2),
        )

        layout["header"].update(self.make_header())
        layout["prices"].update(self.make_prices_panel())
        layout["orderbook"].update(self.make_orderbook_panel())
        layout["fair_values"].update(self.make_fair_values_panel())
        layout["strategies"].update(self.make_strategies_panel())
        layout["consensus"].update(self.make_consensus_panel())
        layout["stats"].update(self.make_stats_panel())

        return layout


# ============================================================================
# POLYMARKET API HELPERS
# ============================================================================

def fetch_metadata_with_retry(slug: str, max_retries: int = 5):
    """
    Fetch Polymarket BTC 15m event metadata (UP/DOWN token IDs) with retry.
    Markets appear 30–60s after window start.
    """
    for attempt in range(max_retries):
        try:
            url = "https://gamma-api.polymarket.com/events"
            params = {"slug": slug}
            resp = requests.get(url, params=params, timeout=10)

            if resp.status_code == 200 and resp.json():
                data = resp.json()
                market = data[0]["markets"][0]

                token_ids = json.loads(market["clobTokenIds"])
                outcomes = json.loads(market["outcomes"])

                if outcomes[0].upper() in ["UP", "YES"]:
                    up_token = token_ids[0]
                    down_token = token_ids[1]
                else:
                    up_token = token_ids[1]
                    down_token = token_ids[0]

                return {
                    "up_token_id": up_token,
                    "down_token_id": down_token,
                    "question": market["question"],
                    "condition_id": market["conditionId"],
                }
        except Exception as e:
            print(f"[Gamma] Retry {attempt + 1}/{max_retries}: {e}")

        time.sleep(10)

    return None


class WindowDetector:
    """Window detector for 15m BTC markets"""

    def __init__(self):
        self.current_window_start = 0

    def check_window(self):
        now = int(time.time())
        window_start = (now // 900) * 900
        slug = f"btc-updown-15m-{window_start}"
        is_new = window_start != self.current_window_start
        if is_new:
            self.current_window_start = window_start
        return slug, window_start, is_new


# ============================================================================
# WEBSOCKET HANDLERS
# ============================================================================

async def binance_spot_handler(dm: DataManager):
    uri = "wss://stream.binance.com:9443/ws/btcusdt@trade"

    while True:
        try:
            async with websockets.connect(uri) as ws:
                print("[Binance Spot] Connected")
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if "p" in data:
                            price = float(data["p"])
                            quantity = float(data["q"])
                            is_buyer_maker = data["m"]
                            dm.update_binance_spot(price)
                            dm.add_spot_trade(price, quantity, is_buyer_maker)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
        except Exception as e:
            print(f"[Binance Spot] Error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


async def binance_futures_handler(dm: DataManager):
    uri = "wss://fstream.binance.com/ws/btcusdt@trade"

    while True:
        try:
            async with websockets.connect(uri) as ws:
                print("[Binance Futures] Connected")
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if "p" in data:
                            price = float(data["p"])
                            quantity = float(data["q"])
                            is_buyer_maker = data["m"]
                            dm.update_futures(perp=price)
                            dm.add_futures_trade(price, quantity, is_buyer_maker)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
        except Exception as e:
            print(f"[Binance Futures] Error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


async def rtds_handler(dm: DataManager):
    uri = "wss://ws-live-data.polymarket.com"

    current_window_start = 0
    strike_set_for_window = False

    while True:
        try:
            async with websockets.connect(uri) as ws:
                print("[RTDS] Connected")

                subscribe_msg = {
                    "action": "subscribe",
                    "subscriptions": [
                        {
                            "topic": "crypto_prices",
                            "type": "*",
                            "filters": "{\"symbol\":\"btcusdt\"}",
                        },
                        {
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": "{\"symbol\":\"btc/usd\"}",
                        },
                    ],
                }
                await ws.send(json.dumps(subscribe_msg))

                while True:
                    msg = await ws.recv()
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue

                    topic = data.get("topic")
                    msg_type = data.get("type")
                    payload = data.get("payload") or {}

                    if msg_type != "update":
                        continue

                    if topic == "crypto_prices" and payload.get("symbol") == "btcusdt":
                        try:
                            price = float(payload["value"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        dm.update_rtds_binance(price)

                    elif topic == "crypto_prices_chainlink" and payload.get("symbol") == "btc/usd":
                        try:
                            price = float(payload["value"])
                        except (KeyError, TypeError, ValueError):
                            continue

                        dm.update_rtds_oracle(price)

                        now = int(time.time())
                        window_start = (now // 900) * 900

                        if window_start != current_window_start:
                            current_window_start = window_start
                            strike_set_for_window = False

                        if not strike_set_for_window:
                            with dm.lock:
                                dm.event.strike_price = price
                                dm.event.start_time = window_start
                                dm.event.end_time = window_start + 900
                                dm.event.status = "ACTIVE"
                            strike_set_for_window = True
                            print(f"[RTDS] New window {window_start}, strike={price:.2f}")

        except Exception as e:
            print(f"[RTDS] Error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


async def window_and_metadata_loop(dm: DataManager):
    detector = WindowDetector()

    while True:
        slug, window_start, is_new = detector.check_window()

        if is_new:
            print(f"[Window] New window: {slug}")
            with dm.lock:
                dm.event.slug = slug
                dm.event.start_time = window_start
                dm.event.end_time = window_start + 900
                dm.event.status = "PENDING"
                dm.event.up_token_id = ""
                dm.event.down_token_id = ""

            meta = await asyncio.to_thread(fetch_metadata_with_retry, slug)
            if meta:
                with dm.lock:
                    dm.event.up_token_id = meta["up_token_id"]
                    dm.event.down_token_id = meta["down_token_id"]
                    dm.event.status = "ACTIVE"
                print(
                    f"[Gamma] Loaded tokens: "
                    f"UP={meta['up_token_id'][:10]}... DOWN={meta['down_token_id'][:10]}..."
                )
            else:
                print(f"[Gamma] No market yet for {slug}")

        await asyncio.sleep(1)


async def polymarket_clob_handler(dm: DataManager):
    """CLOB handler with auto-resubscribe on event switch."""
    uri = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    last_slug = None

    while True:
        try:
            async with websockets.connect(uri) as ws:
                print("[Polymarket CLOB] Connected")

                while True:
                    with dm.lock:
                        up_id = dm.event.up_token_id
                        down_id = dm.event.down_token_id
                        slug = dm.event.slug
                    if up_id and down_id:
                        break
                    await asyncio.sleep(0.5)

                last_slug = slug

                with dm.lock:
                    dm.orderbook = OrderbookData()

                subscribe_msg = {
                    "type": "market",
                    "assets_ids": [up_id, down_id],
                }
                await ws.send(json.dumps(subscribe_msg))
                print(
                    f"[Polymarket CLOB] Subscribed to slug={slug} "
                    f"UP={up_id[:10]}.. DOWN={down_id[:10]}.."
                )

                while True:
                    with dm.lock:
                        current_slug = dm.event.slug
                        current_up = dm.event.up_token_id
                        current_down = dm.event.down_token_id

                    if current_slug != last_slug and current_up and current_down:
                        print(f"[Polymarket CLOB] Detected new event {current_slug}, resubscribing...")
                        break

                    msg = await ws.recv()
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue

                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict):
                                _process_clob_message(dm, item, up_id, down_id)
                    elif isinstance(data, dict):
                        _process_clob_message(dm, data, up_id, down_id)

        except Exception as e:
            print(f"[Polymarket CLOB] Error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


def _process_clob_message(dm: DataManager, data: dict, up_id: str, down_id: str):
    if data.get("event_type") != "book":
        return

    asset_id = data.get("asset_id")
    bids = data.get("bids", []) or []
    asks = data.get("asks", []) or []

    if not bids or not asks:
        return

    try:
        best_bid = max(bids, key=lambda x: float(x["price"]))
        best_ask = min(asks, key=lambda x: float(x["price"]))
        bid_price = float(best_bid["price"])
        ask_price = float(best_ask["price"])
        bid_size = float(best_bid["size"])
        ask_size = float(best_ask["size"])
    except (KeyError, TypeError, ValueError):
        return

    if asset_id == up_id:
        dm.update_orderbook_up(bid_price, ask_price, bid_size, ask_size)
    elif asset_id == down_id:
        dm.update_orderbook_down(bid_price, ask_price, bid_size, ask_size)


# ============================================================================
# MAIN LOOP
# ============================================================================

async def calculation_loop(dm: DataManager):
    while True:
        dm.tick()
        await asyncio.sleep(1.0)


async def ui_loop(dm: DataManager):
    renderer = TerminalRenderer(dm)
    console = Console()

    with Live(renderer.render(), console=console, refresh_per_second=1) as live:
        while True:
            live.update(renderer.render())
            await asyncio.sleep(1.0)


async def main():
    print("=" * 80)
    print("POLYMARKET TRADING TERMINAL - FIXED VERSION")
    print("=" * 80)
    print()

    dm = DataManager()

    tasks = [
        asyncio.create_task(calculation_loop(dm)),
        asyncio.create_task(window_and_metadata_loop(dm)),
        asyncio.create_task(binance_spot_handler(dm)),
        asyncio.create_task(binance_futures_handler(dm)),
        asyncio.create_task(rtds_handler(dm)),
        asyncio.create_task(polymarket_clob_handler(dm)),
        asyncio.create_task(ui_loop(dm)),
    ]

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\nShutting down...")
        for task in tasks:
            task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
