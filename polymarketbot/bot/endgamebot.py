#!/usr/bin/env python3
"""
=============================================================================
🤖 POLYMARKET BTC 15-MIN PAPER TRADING BOT v2.1 (FIXED)
=============================================================================

Verified data streams:
1. BINANCE FUTURES: wss://fstream.binance.com/ws/btcusdt@aggTrade
2. BINANCE SPOT:    wss://stream.binance.com:9443/ws/btcusdt@trade  
3. POLY RELAY:      wss://ws-live-data.polymarket.com (crypto_prices)
4. CHAINLINK:       wss://ws-live-data.polymarket.com (crypto_prices_chainlink)

=============================================================================
"""

import time
import requests
import json
import threading
import websocket
import sys
import re
import os
import csv
import uuid
import statistics
from datetime import datetime
from collections import deque
from threading import Lock
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from enum import Enum

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================

class Config:
    # Timing
    REFRESH_RATE = 0.1
    RECONNECT_DELAY = 2
    WINDOW_DURATION = 900
    
    # Paper Trading
    STARTING_CAPITAL = 100.0
    SLIPPAGE_BPS = 50
    
    # Risk Management
    MAX_POSITION_PCT = 25
    MAX_DAILY_LOSS_PCT = 20
    MAX_TRADE_PCT = 10
    MIN_TRADE_SIZE = 1.0
    
    # Strategy
    SIGNAL_COOLDOWN = 10
    
    # Logging
    LOG_DIR = "paper_trading_logs"
    
    # API Endpoints
    BINANCE_FUTURES_WS = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
    BINANCE_SPOT_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    POLYMARKET_WS = "wss://ws-live-data.polymarket.com"
    GAMMA_API = "https://gamma-api.polymarket.com/events/slug/"
    CLOB_API = "https://clob.polymarket.com/price"


# ==========================================
# 📁 LOGGING SYSTEM
# ==========================================

class TradingLogger:
    def __init__(self):
        self.log_dir = Config.LOG_DIR
        os.makedirs(self.log_dir, exist_ok=True)
        
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = os.path.join(self.log_dir, f"session_{self.session_id}")
        os.makedirs(self.session_dir, exist_ok=True)
        
        self._lock = Lock()
        self._init_logs()
    
    def _init_logs(self):
        # trades.csv
        with open(os.path.join(self.session_dir, "trades.csv"), 'w', newline='') as f:
            csv.writer(f).writerow([
                'timestamp', 'trade_id', 'market_slug', 'strategy',
                'side', 'size_usd', 'entry_price', 'filled_price', 'balance_after'
            ])
        
        # pnl.csv
        with open(os.path.join(self.session_dir, "pnl.csv"), 'w', newline='') as f:
            csv.writer(f).writerow([
                'timestamp', 'market_slug', 'strike', 'settlement_price',
                'strategy', 'side', 'entry_price', 'size_usd', 'pnl', 'result', 'balance_after'
            ])
        
        # signals.csv
        with open(os.path.join(self.session_dir, "signals.csv"), 'w', newline='') as f:
            csv.writer(f).writerow([
                'timestamp', 'strategy', 'signal_type', 'confidence',
                'size', 'reason', 'spot_price', 'oracle_price', 'oracle_age_ms',
                'strike', 'time_left', 'up_price', 'down_price', 'executed', 'reject_reason'
            ])
        
        # debug.log
        with open(os.path.join(self.session_dir, "debug.log"), 'w') as f:
            f.write(f"=== Session {self.session_id} ===\n")
    
    def log_trade(self, data: dict):
        with self._lock:
            path = os.path.join(self.session_dir, "trades.csv")
            with open(path, 'a', newline='') as f:
                csv.writer(f).writerow([
                    data.get('timestamp', ''), data.get('trade_id', ''),
                    data.get('market_slug', ''), data.get('strategy', ''),
                    data.get('side', ''), data.get('size_usd', ''),
                    data.get('entry_price', ''), data.get('filled_price', ''),
                    data.get('balance_after', '')
                ])
    
    def log_pnl(self, data: dict):
        with self._lock:
            path = os.path.join(self.session_dir, "pnl.csv")
            with open(path, 'a', newline='') as f:
                csv.writer(f).writerow([
                    data.get('timestamp', ''), data.get('market_slug', ''),
                    data.get('strike', ''), data.get('settlement_price', ''),
                    data.get('strategy', ''), data.get('side', ''),
                    data.get('entry_price', ''), data.get('size_usd', ''),
                    data.get('pnl', ''), data.get('result', ''),
                    data.get('balance_after', '')
                ])
    
    def log_signal(self, data: dict):
        with self._lock:
            path = os.path.join(self.session_dir, "signals.csv")
            with open(path, 'a', newline='') as f:
                csv.writer(f).writerow([
                    data.get('timestamp', ''), data.get('strategy', ''),
                    data.get('signal_type', ''), data.get('confidence', ''),
                    data.get('size', ''), data.get('reason', ''),
                    data.get('spot_price', ''), data.get('oracle_price', ''),
                    data.get('oracle_age_ms', ''), data.get('strike', ''),
                    data.get('time_left', ''), data.get('up_price', ''),
                    data.get('down_price', ''), data.get('executed', ''),
                    data.get('reject_reason', '')
                ])
    
    def debug(self, msg: str):
        with self._lock:
            path = os.path.join(self.session_dir, "debug.log")
            with open(path, 'a') as f:
                f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    
    def get_session_path(self) -> str:
        return self.session_dir


# ==========================================
# 🧱 DATA CORE
# ==========================================

class DataCore:
    def __init__(self):
        self._lock = Lock()
        
        # Futures
        self._futures_price = 0.0
        self._futures_ts = 0
        self._futures_connected = False
        
        # Spot
        self._spot_price = 0.0
        self._spot_ts = 0
        self._spot_connected = False
        
        # Poly Relay
        self._relay_price = 0.0
        self._relay_ts = 0
        
        # Oracle (Chainlink)
        self._oracle_price = 0.0
        self._oracle_ts = 0
        self._poly_connected = False
        
        # History
        self._spot_history = deque(maxlen=600)
        self._oracle_history = deque(maxlen=50)
    
    def update_futures(self, price: str, ts: int):
        with self._lock:
            self._futures_price = float(price)
            self._futures_ts = int(ts)
            self._futures_connected = True
    
    def update_spot(self, price: str, ts: int):
        with self._lock:
            self._spot_price = float(price)
            self._spot_ts = int(ts)
            self._spot_connected = True
            self._spot_history.append({'time': time.time(), 'price': self._spot_price})
    
    def update_relay(self, price: float, ts: int):
        with self._lock:
            self._relay_price = float(price)
            self._relay_ts = int(ts)
    
    def update_oracle(self, price: float, ts: int):
        with self._lock:
            new_price = float(price)
            if new_price != self._oracle_price:
                self._oracle_history.append({'time': time.time(), 'price': new_price})
            self._oracle_price = new_price
            self._oracle_ts = int(ts)
            self._poly_connected = True
    
    def set_futures_connected(self, status: bool):
        with self._lock:
            self._futures_connected = status
    
    def set_spot_connected(self, status: bool):
        with self._lock:
            self._spot_connected = status
    
    def set_poly_connected(self, status: bool):
        with self._lock:
            self._poly_connected = status
    
    def get_snapshot(self) -> dict:
        now_ms = int(time.time() * 1000)
        with self._lock:
            # Safe age calculations
            def safe_age(ts):
                if ts <= 0:
                    return 999999  # Return large number if never received
                age = now_ms - ts
                return max(0, age)  # Never negative
            
            return {
                'timestamp': time.time(),
                'futures_price': self._futures_price,
                'futures_ts': self._futures_ts,
                'futures_age_ms': safe_age(self._futures_ts),
                'futures_connected': self._futures_connected,
                'spot_price': self._spot_price,
                'spot_ts': self._spot_ts,
                'spot_age_ms': safe_age(self._spot_ts),
                'spot_connected': self._spot_connected,
                'relay_price': self._relay_price,
                'relay_ts': self._relay_ts,
                'relay_age_ms': safe_age(self._relay_ts),
                'oracle_price': self._oracle_price,
                'oracle_ts': self._oracle_ts,
                'oracle_age_ms': safe_age(self._oracle_ts),
                'poly_connected': self._poly_connected,
                'basis': self._futures_price - self._spot_price if self._futures_price > 0 and self._spot_price > 0 else 0,
                'oracle_gap': self._oracle_price - self._spot_price if self._oracle_price > 0 and self._spot_price > 0 else 0,
            }
    
    def get_momentum(self, seconds: int = 30) -> float:
        cutoff = time.time() - seconds
        with self._lock:
            history = [p for p in self._spot_history if p['time'] > cutoff]
        if len(history) < 5:
            return 0.0
        return history[-1]['price'] - history[0]['price']


# ==========================================
# 📡 WEBSOCKET CLIENTS
# ==========================================

class BinanceFuturesClient:
    def __init__(self, core: DataCore, logger: TradingLogger):
        self.core = core
        self.logger = logger
        self.url = Config.BINANCE_FUTURES_WS
        self.ws = None
        self.running = True
    
    def _on_message(self, ws, msg):
        try:
            d = json.loads(msg)
            self.core.update_futures(d['p'], d['T'])
        except Exception as e:
            self.logger.debug(f"Futures msg error: {e}")
    
    def _on_error(self, ws, error):
        self.logger.debug(f"Futures error: {error}")
        self.core.set_futures_connected(False)
    
    def _on_close(self, ws, status, msg):
        self.core.set_futures_connected(False)
    
    def _on_open(self, ws):
        self.logger.debug("Futures connected")
        self.core.set_futures_connected(True)
    
    def run(self):
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                self.logger.debug(f"Futures exception: {e}")
            
            if self.running:
                time.sleep(Config.RECONNECT_DELAY)
    
    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()


class BinanceSpotClient:
    def __init__(self, core: DataCore, logger: TradingLogger):
        self.core = core
        self.logger = logger
        self.url = Config.BINANCE_SPOT_WS
        self.ws = None
        self.running = True
    
    def _on_message(self, ws, msg):
        try:
            d = json.loads(msg)
            self.core.update_spot(d['p'], d['T'])
        except Exception as e:
            self.logger.debug(f"Spot msg error: {e}")
    
    def _on_error(self, ws, error):
        self.logger.debug(f"Spot error: {error}")
        self.core.set_spot_connected(False)
    
    def _on_close(self, ws, status, msg):
        self.core.set_spot_connected(False)
    
    def _on_open(self, ws):
        self.logger.debug("Spot connected")
        self.core.set_spot_connected(True)
    
    def run(self):
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                self.logger.debug(f"Spot exception: {e}")
            
            if self.running:
                time.sleep(Config.RECONNECT_DELAY)
    
    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()


class PolymarketClient:
    def __init__(self, core: DataCore, logger: TradingLogger):
        self.core = core
        self.logger = logger
        self.url = Config.POLYMARKET_WS
        self.ws = None
        self.running = True
    
    def _on_open(self, ws):
        self.logger.debug("Polymarket connected")
        self.core.set_poly_connected(True)
        
        try:
            # Subscribe to relay
            ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [{
                    "topic": "crypto_prices",
                    "type": "*",
                    "filters": "{\"symbol\":\"btcusdt\"}"
                }]
            }))
            
            # Subscribe to oracle
            ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [{
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": "{\"symbol\":\"btc/usd\"}"
                }]
            }))
        except Exception as e:
            self.logger.debug(f"Subscription error: {e}")
    
    def _on_message(self, ws, msg):
        try:
            d = json.loads(msg)
            if d.get('type') == 'update':
                topic = d.get('topic', '')
                payload = d.get('payload', {})
                symbol = payload.get('symbol', '')
                
                if 'value' not in payload or 'timestamp' not in payload:
                    return
                
                if topic == 'crypto_prices' and symbol == 'btcusdt':
                    self.core.update_relay(payload['value'], payload['timestamp'])
                elif topic == 'crypto_prices_chainlink' and symbol == 'btc/usd':
                    self.core.update_oracle(payload['value'], payload['timestamp'])
        except Exception as e:
            self.logger.debug(f"Poly msg error: {e}")
    
    def _on_error(self, ws, error):
        self.logger.debug(f"Poly error: {error}")
        self.core.set_poly_connected(False)
    
    def _on_close(self, ws, status, msg):
        self.core.set_poly_connected(False)
    
    def run(self):
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                self.logger.debug(f"Poly exception: {e}")
            
            if self.running:
                time.sleep(Config.RECONNECT_DELAY)
    
    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()


# ==========================================
# 🎮 MARKET MANAGER
# ==========================================

class MarketManager:
    def __init__(self, core: DataCore, logger: TradingLogger):
        self.core = core
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'PolyBot/2.0'})
        
        self._lock = Lock()
        self._window_start = 0
        self._slug = ""
        self._token_ids = {"UP": None, "DOWN": None}
        self._prices = {"UP": 0.5, "DOWN": 0.5}
        self._strike = 0.0
        self._active = False
        self._market_found = False
    
    @property
    def window_start(self) -> int:
        with self._lock:
            return self._window_start
    
    @property
    def window_end(self) -> int:
        with self._lock:
            return self._window_start + Config.WINDOW_DURATION
    
    @property
    def slug(self) -> str:
        with self._lock:
            return self._slug
    
    @property
    def strike(self) -> float:
        with self._lock:
            return self._strike
    
    @property
    def time_left(self) -> float:
        return max(0, self.window_end - time.time())
    
    def get_state(self) -> dict:
        with self._lock:
            # Only truly active if we have strike and tokens
            truly_active = (
                self._active and 
                self._market_found and 
                self._strike > 0 and
                self._token_ids.get("UP") is not None
            )
            
            return {
                'active': truly_active,  # Use validated active state
                'window_start': self._window_start,
                'window_end': self._window_start + Config.WINDOW_DURATION,
                'time_left': max(0, self._window_start + Config.WINDOW_DURATION - time.time()),
                'slug': self._slug,
                'strike': self._strike,
                'up_price': self._prices['UP'],
                'down_price': self._prices['DOWN'],
                'market_found': self._market_found
            }
    
    def lifecycle_check(self) -> bool:
        """Returns True if new window started"""
        now = int(time.time())
        current_window = (now // 900) * 900
        
        with self._lock:
            if current_window == self._window_start:
                return False
            
            # Store old strike before resetting (for settlement)
            old_strike = self._strike
            
            # NEW WINDOW
            self._window_start = current_window
            self._slug = f"btc-updown-15m-{current_window}"
            self._token_ids = {"UP": None, "DOWN": None}
            self._prices = {"UP": 0.5, "DOWN": 0.5}
            self._active = False
            self._market_found = False
            self._strike = 0.0  # Will be set when market is found
        
        self.logger.debug(f"New window: {self._slug}")
        return True
    
    def fetch_market_details(self) -> bool:
        with self._lock:
            if self._market_found:
                return True
            slug = self._slug
        
        try:
            r = self.session.get(f"{Config.GAMMA_API}{slug}", timeout=10)
            
            if r.status_code == 200:
                data = r.json()
                if 'markets' in data and len(data['markets']) > 0:
                    market = data['markets'][0]
                    token_ids = json.loads(market['clobTokenIds'])
                    
                    # Parse strike from question - THIS IS THE REAL STRIKE
                    question = market.get('question', '')
                    match = re.search(r"\$([\d,]+\.?\d*)", question)
                    detected_strike = float(match.group(1).replace(",", "")) if match else 0.0
                    
                    with self._lock:
                        self._token_ids["UP"] = token_ids[0]
                        self._token_ids["DOWN"] = token_ids[1]
                        self._market_found = True
                        self._active = True
                        
                        # ALWAYS use the strike from market question
                        if detected_strike > 0:
                            self._strike = detected_strike
                        else:
                            # Fallback to oracle only if parsing failed
                            oracle_data = self.core.get_snapshot()
                            self._strike = oracle_data['oracle_price']
                    
                    self.logger.debug(f"Market found, strike: ${self._strike:.2f}")
                    return True
        except Exception as e:
            self.logger.debug(f"Gamma API error: {e}")
        
        return False
    
    def fetch_share_prices(self):
        with self._lock:
            if not self._active:
                return
            up_id = self._token_ids.get("UP")
            down_id = self._token_ids.get("DOWN")
        
        if not up_id or not down_id:
            return
        
        try:
            r = self.session.get(f"{Config.CLOB_API}?token_id={up_id}&side=buy", timeout=5)
            if r.status_code == 200:
                with self._lock:
                    self._prices["UP"] = float(r.json().get('price', 0.5))
            
            r = self.session.get(f"{Config.CLOB_API}?token_id={down_id}&side=buy", timeout=5)
            if r.status_code == 200:
                with self._lock:
                    self._prices["DOWN"] = float(r.json().get('price', 0.5))
        except Exception as e:
            self.logger.debug(f"CLOB error: {e}")


# ==========================================
# 🎯 TRADING TYPES
# ==========================================

class Side(Enum):
    UP = "UP"
    DOWN = "DOWN"

class SignalType(Enum):
    BUY_UP = "BUY_UP"
    BUY_DOWN = "BUY_DOWN"
    HOLD = "HOLD"

@dataclass
class Signal:
    signal_type: SignalType
    confidence: float
    suggested_size: float
    reason: str
    strategy_name: str
    timestamp: float = field(default_factory=time.time)

@dataclass
class Position:
    position_id: str
    strategy_name: str
    market_slug: str
    window_start: int
    side: Side
    entry_price: float
    size_usd: float
    entry_time: float
    strike: float


# ==========================================
# 📊 STRATEGIES
# ==========================================

class BaseStrategy:
    def __init__(self, name: str, allocation: float):
        self.name = name
        self.allocation = allocation
        self.enabled = True
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.pnl_history = []
    
    def analyze(self, data: dict, market: dict) -> Optional[Signal]:
        raise NotImplementedError
    
    def record_result(self, pnl: float, win: bool):
        self.pnl_history.append(pnl)
        self.total_pnl += pnl
        if win:
            self.wins += 1
        else:
            self.losses += 1
    
    def get_stats(self) -> dict:
        total = self.wins + self.losses
        win_rate = self.wins / total if total > 0 else 0.0  # Safe division
        
        return {
            'name': self.name,
            'trades': total,
            'wins': self.wins,
            'losses': self.losses,
            'win_rate': win_rate,
            'pnl': self.total_pnl
        }


class OracleLagStrategy(BaseStrategy):
    """Exploits delay between Binance and Chainlink oracle"""
    
    def __init__(self):
        super().__init__("OracleLag", 0.30)
        self.min_gap = 60
        self.oracle_min_age = 8
        self.min_time = 120
        self.max_time = 720
        self.max_price = 0.72
    
    def analyze(self, data: dict, market: dict) -> Optional[Signal]:
        # Validation checks at the start
        if not market.get('active'):
            return None
        
        # Check for valid prices
        spot = data.get('spot_price', 0)
        oracle = data.get('oracle_price', 0)
        strike = market.get('strike', 0)
        up_price = market.get('up_price', 0)
        down_price = market.get('down_price', 0)
        oracle_age = data['oracle_age_ms'] / 1000
        time_left = market['time_left']
        
        # All must be positive
        if spot <= 0 or oracle <= 0 or strike <= 0:
            return None
        
        if up_price <= 0 or up_price >= 1 or down_price <= 0 or down_price >= 1:
            return None
        
        if time_left < self.min_time or time_left > self.max_time:
            return None
        if oracle_age < self.oracle_min_age:
            return None
        
        gap = spot - oracle
        
        # Bullish: Spot above Oracle
        if gap > self.min_gap:
            if spot > strike and oracle < strike and up_price < self.max_price:
                conf = min(0.55 + (gap - self.min_gap) / 250, 0.85)
                size = self._calc_size(conf, up_price)
                return Signal(
                    SignalType.BUY_UP, conf, size,
                    f"Gap +${gap:.0f}, oracle stale {oracle_age:.0f}s",
                    self.name
                )
            elif spot > strike and up_price < 0.65:
                conf = min(0.45 + (gap - self.min_gap) / 350, 0.70)
                size = self._calc_size(conf, up_price)
                return Signal(
                    SignalType.BUY_UP, conf, size,
                    f"Gap +${gap:.0f}, momentum UP",
                    self.name
                )
        
        # Bearish: Spot below Oracle
        elif gap < -self.min_gap:
            if spot < strike and oracle > strike and down_price < self.max_price:
                conf = min(0.55 + (abs(gap) - self.min_gap) / 250, 0.85)
                size = self._calc_size(conf, down_price)
                return Signal(
                    SignalType.BUY_DOWN, conf, size,
                    f"Gap ${gap:.0f}, oracle stale {oracle_age:.0f}s",
                    self.name
                )
            elif spot < strike and down_price < 0.65:
                conf = min(0.45 + (abs(gap) - self.min_gap) / 350, 0.70)
                size = self._calc_size(conf, down_price)
                return Signal(
                    SignalType.BUY_DOWN, conf, size,
                    f"Gap ${gap:.0f}, momentum DOWN",
                    self.name
                )
        
        return None
    
    def _calc_size(self, conf: float, price: float) -> float:
        edge = conf - price
        if edge <= 0:
            return Config.MIN_TRADE_SIZE
        base = 8.0
        multiplier = 1 + (edge / (1 - price)) * 0.25 * 4
        return max(Config.MIN_TRADE_SIZE, min(base * multiplier, 25.0))


class EndGameStrategy(BaseStrategy):
    """Trades in final seconds when oracle is likely locked"""
    
    def __init__(self):
        super().__init__("EndGame", 0.25)
        self.entry_window = 45
        self.oracle_max_age = 12
        self.min_gap = 25
        self.max_price = 0.92
    
    def analyze(self, data: dict, market: dict) -> Optional[Signal]:
        # Validation checks at the start
        if not market.get('active'):
            return None
        
        time_left = market['time_left']
        oracle_age = data['oracle_age_ms'] / 1000
        
        if time_left > self.entry_window:
            return None
        if oracle_age > self.oracle_max_age:
            return None
        
        # Check for valid prices
        oracle = data.get('oracle_price', 0)
        strike = market.get('strike', 0)
        up_price = market.get('up_price', 0)
        down_price = market.get('down_price', 0)
        
        # All must be positive
        if oracle <= 0 or strike <= 0:
            return None
        
        if up_price <= 0 or up_price >= 1 or down_price <= 0 or down_price >= 1:
            return None
        
        gap = oracle - strike
        
        if gap > self.min_gap and up_price < self.max_price:
            conf = min(0.75 + abs(gap) / 400, 0.95)
            profit_pct = (1.0 - up_price) / up_price
            size = min(12 * (1 + profit_pct), 30.0)
            return Signal(
                SignalType.BUY_UP, conf, size,
                f"ENDGAME: Oracle ${oracle:.0f} > Strike ${strike:.0f}",
                self.name
            )
        
        elif gap < -self.min_gap and down_price < self.max_price:
            conf = min(0.75 + abs(gap) / 400, 0.95)
            profit_pct = (1.0 - down_price) / down_price
            size = min(12 * (1 + profit_pct), 30.0)
            return Signal(
                SignalType.BUY_DOWN, conf, size,
                f"ENDGAME: Oracle ${oracle:.0f} < Strike ${strike:.0f}",
                self.name
            )
        
        return None


class MomentumStrategy(BaseStrategy):
    """Rides strong directional moves"""
    
    def __init__(self):
        super().__init__("Momentum", 0.20)
        self.momentum_threshold = 70
        self.min_time = 90
        self.max_time = 600
        self.max_price = 0.68
        self.min_distance = 30
    
    def analyze(self, data: dict, market: dict) -> Optional[Signal]:
        # Validation checks at the start
        if not market.get('active'):
            return None
        
        time_left = market['time_left']
        if time_left < self.min_time or time_left > self.max_time:
            return None
        
        # Check for valid prices
        spot = data.get('spot_price', 0)
        strike = market.get('strike', 0)
        up_price = market.get('up_price', 0)
        down_price = market.get('down_price', 0)
        
        # All must be positive
        if spot <= 0 or strike <= 0:
            return None
        
        if up_price <= 0 or up_price >= 1 or down_price <= 0 or down_price >= 1:
            return None
        
        momentum = data.get('oracle_gap', 0)
        distance = spot - strike
        
        if momentum > self.momentum_threshold and distance > self.min_distance:
            if up_price < self.max_price:
                conf = min(0.50 + abs(momentum) / 300, 0.72)
                return Signal(
                    SignalType.BUY_UP, conf, 10.0,
                    f"Momentum +${momentum:.0f}, ${distance:.0f} above strike",
                    self.name
                )
        
        elif momentum < -self.momentum_threshold and distance < -self.min_distance:
            if down_price < self.max_price:
                conf = min(0.50 + abs(momentum) / 300, 0.72)
                return Signal(
                    SignalType.BUY_DOWN, conf, 10.0,
                    f"Momentum ${momentum:.0f}, ${abs(distance):.0f} below strike",
                    self.name
                )
        
        return None


class VolatilityFadeStrategy(BaseStrategy):
    """Trades mean reversion after volatility spikes"""
    
    def __init__(self):
        super().__init__("VolFade", 0.15)
        self.cheap_price = 0.32
        self.spike_price = 0.68
        self.min_time = 180
    
    def analyze(self, data: dict, market: dict) -> Optional[Signal]:
        # Validation checks at the start
        if not market.get('active'):
            return None
        
        if market['time_left'] < self.min_time:
            return None
        
        # Check for valid prices
        up_price = market.get('up_price', 0)
        down_price = market.get('down_price', 0)
        
        if up_price <= 0 or up_price >= 1 or down_price <= 0 or down_price >= 1:
            return None
        
        if up_price > self.spike_price and down_price < self.cheap_price:
            conf = 0.48 + (0.5 - down_price) * 0.4
            return Signal(
                SignalType.BUY_DOWN, conf, 8.0,
                f"UP spike {up_price:.2f}, DOWN cheap {down_price:.2f}",
                self.name
            )
        
        if down_price > self.spike_price and up_price < self.cheap_price:
            conf = 0.48 + (0.5 - up_price) * 0.4
            return Signal(
                SignalType.BUY_UP, conf, 8.0,
                f"DOWN spike {down_price:.2f}, UP cheap {up_price:.2f}",
                self.name
            )
        
        return None


class StrikeProximityStrategy(BaseStrategy):
    """Trades when price is close to strike near expiry"""
    
    def __init__(self):
        super().__init__("Proximity", 0.10)
        self.proximity = 25
        self.min_time = 60
        self.max_time = 180
        self.min_momentum = 20
    
    def analyze(self, data: dict, market: dict) -> Optional[Signal]:
        # Validation checks at the start
        if not market.get('active'):
            return None
        
        time_left = market['time_left']
        if time_left < self.min_time or time_left > self.max_time:
            return None
        
        # Check for valid prices
        spot = data.get('spot_price', 0)
        strike = market.get('strike', 0)
        up_price = market.get('up_price', 0)
        down_price = market.get('down_price', 0)
        
        # All must be positive
        if spot <= 0 or strike <= 0:
            return None
        
        if up_price <= 0 or up_price >= 1 or down_price <= 0 or down_price >= 1:
            return None
        
        distance = spot - strike
        if abs(distance) > self.proximity:
            return None
        
        momentum = data.get('basis', 0)
        
        if momentum > self.min_momentum and distance > 0 and up_price < 0.60:
            conf = min(0.52 + abs(momentum) / 200, 0.65)
            return Signal(
                SignalType.BUY_UP, conf, 6.0,
                f"Close to strike +${distance:.0f}, futures premium",
                self.name
            )
        
        if momentum < -self.min_momentum and distance < 0 and down_price < 0.60:
            conf = min(0.52 + abs(momentum) / 200, 0.65)
            return Signal(
                SignalType.BUY_DOWN, conf, 6.0,
                f"Close to strike ${distance:.0f}, futures discount",
                self.name
            )
        
        return None


# ==========================================
# 💰 PAPER TRADING ENGINE
# ==========================================

class PaperTradingEngine:
    def __init__(self, capital: float, logger: TradingLogger):
        self.logger = logger
        self.starting_capital = capital
        self.balance = capital
        self.peak_balance = capital
        
        self._lock = Lock()
        self.positions: Dict[str, List[Position]] = {}
        self.all_trades = []
        self.all_settlements = []
        
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.last_reset = datetime.now().date()
    
    def can_trade(self, size: float, market_slug: str) -> Tuple[bool, str]:
        # Daily reset
        if datetime.now().date() != self.last_reset:
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.last_reset = datetime.now().date()
        
        with self._lock:
            max_loss = self.starting_capital * Config.MAX_DAILY_LOSS_PCT / 100
            if self.daily_pnl < -max_loss:
                return False, "Daily loss limit"
            
            max_trade = self.balance * Config.MAX_TRADE_PCT / 100
            if size > max_trade:
                return False, f"Size > {Config.MAX_TRADE_PCT}%"
            
            if size < Config.MIN_TRADE_SIZE:
                return False, "Size too small"
            
            exposure = sum(p.size_usd for p in self.positions.get(market_slug, []))
            max_market = self.balance * Config.MAX_POSITION_PCT / 100
            if exposure + size > max_market:
                return False, "Market exposure limit"
            
            if size > self.balance:
                return False, "Insufficient balance"
            
            return True, "OK"
    
    def execute_trade(self, signal: Signal, market: dict) -> Optional[Position]:
        # Validate strike before trading
        strike = market.get('strike', 0)
        if strike <= 0:
            self.logger.debug("Cannot trade: strike is 0")
            return None
        
        side = Side.UP if signal.signal_type == SignalType.BUY_UP else Side.DOWN
        price = market['up_price'] if side == Side.UP else market['down_price']
        
        # Validate price
        if price <= 0 or price >= 1:
            self.logger.debug(f"Invalid price: {price}")
            return None
        
        # Slippage
        filled = price * (1 + Config.SLIPPAGE_BPS / 10000)
        filled = min(filled, 0.99)
        
        size = min(signal.suggested_size, self.balance * 0.95)
        if size < Config.MIN_TRADE_SIZE:
            return None
        
        pos_id = str(uuid.uuid4())[:8]
        
        with self._lock:
            position = Position(
                position_id=pos_id,
                strategy_name=signal.strategy_name,
                market_slug=market['slug'],
                window_start=market['window_start'],
                side=side,
                entry_price=filled,
                size_usd=size,
                entry_time=time.time(),
                strike=market['strike']
            )
            
            if market['slug'] not in self.positions:
                self.positions[market['slug']] = []
            self.positions[market['slug']].append(position)
            
            self.balance -= size
            self.daily_trades += 1
            self.all_trades.append(position)
        
        self.logger.log_trade({
            'timestamp': datetime.now().isoformat(),
            'trade_id': pos_id,
            'market_slug': market['slug'],
            'strategy': signal.strategy_name,
            'side': side.value,
            'size_usd': f"{size:.2f}",
            'entry_price': f"{price:.4f}",
            'filled_price': f"{filled:.4f}",
            'balance_after': f"{self.balance:.2f}"
        })
        
        return position
    
    def settle_market(self, market_slug: str, strike: float, settlement: float, 
                      strategies: List[BaseStrategy]) -> List[dict]:
        results = []
        
        # Validate inputs
        if strike <= 0 or settlement <= 0:
            self.logger.debug(f"Invalid settlement: strike={strike}, settlement={settlement}")
            return results
        
        with self._lock:
            # Pop returns empty list if key doesn't exist (safe)
            positions = self.positions.pop(market_slug, [])
            
            if not positions:
                return results
            
            # Determine winner ONCE
            up_wins = settlement > strike
            
            self.logger.debug(
                f"Settling {market_slug}: strike=${strike:.2f}, "
                f"settlement=${settlement:.2f}, UP wins={up_wins}"
            )
            
            for pos in positions:
                # Calculate shares owned
                shares = pos.size_usd / pos.entry_price
                
                if pos.side == Side.UP:
                    if up_wins:
                        # WIN: Get $1 per share
                        payout = shares * 1.0
                        pnl = payout - pos.size_usd  # Profit = payout - cost
                        win = True
                    else:
                        # LOSE: Get $0
                        payout = 0.0
                        pnl = -pos.size_usd  # Loss = full cost
                        win = False
                else:  # Side.DOWN
                    if not up_wins:  # DOWN wins when settlement <= strike
                        # WIN
                        payout = shares * 1.0
                        pnl = payout - pos.size_usd
                        win = True
                    else:
                        # LOSE
                        payout = 0.0
                        pnl = -pos.size_usd
                        win = False
                
                # Add payout to balance (cost was already deducted when opening position)
                self.balance += payout
                self.daily_pnl += pnl
                
                # Update peak AFTER adding payout
                if self.balance > self.peak_balance:
                    self.peak_balance = self.balance
                
                # Update strategy stats
                for s in strategies:
                    if s.name == pos.strategy_name:
                        s.record_result(pnl, win)
                        break
                
                result = {
                    'position': pos,
                    'settlement': settlement,
                    'pnl': pnl,
                    'win': win
                }
                results.append(result)
                self.all_settlements.append(result)
                
                self.logger.log_pnl({
                    'timestamp': datetime.now().isoformat(),
                    'market_slug': market_slug,
                    'strike': f"{strike:.2f}",
                    'settlement_price': f"{settlement:.2f}",
                    'strategy': pos.strategy_name,
                    'side': pos.side.value,
                    'entry_price': f"{pos.entry_price:.4f}",
                    'size_usd': f"{pos.size_usd:.2f}",
                    'pnl': f"{pnl:.2f}",
                    'result': 'WIN' if win else 'LOSS',
                    'balance_after': f"{self.balance:.2f}"
                })
        
        return results
    
    def get_stats(self) -> dict:
        with self._lock:
            pnl = self.balance - self.starting_capital
            total_settled = len(self.all_settlements)
            wins = sum(1 for s in self.all_settlements if s['win'])
            losses = total_settled - wins
            
            return {
                'balance': self.balance,
                'pnl': pnl,
                'pnl_pct': (pnl / self.starting_capital * 100) if self.starting_capital > 0 else 0,
                'daily_pnl': self.daily_pnl,
                'trades': len(self.all_trades),
                'settled': total_settled,
                'wins': wins,
                'losses': losses,
                'win_rate': (wins / total_settled * 100) if total_settled > 0 else 0,  # Safe
                'open_positions': sum(len(p) for p in self.positions.values()),
                'peak': self.peak_balance
            }


# ==========================================
# 🤖 MAIN BOT
# ==========================================

class TradingBot:
    def __init__(self, capital: float = 100.0):
        self.logger = TradingLogger()
        self.core = DataCore()
        self.market = MarketManager(self.core, self.logger)
        self.engine = PaperTradingEngine(capital, self.logger)
        
        self.strategies: List[BaseStrategy] = [
            OracleLagStrategy(),
            EndGameStrategy(),
            MomentumStrategy(),
            VolatilityFadeStrategy(),
            StrikeProximityStrategy(),
        ]
        
        self.futures_client = None
        self.spot_client = None
        self.poly_client = None
        
        self.running = False
        self.last_window = 0
        self.last_signal_time: Dict[str, float] = {}
    
    def start_streams(self):
        self.futures_client = BinanceFuturesClient(self.core, self.logger)
        self.spot_client = BinanceSpotClient(self.core, self.logger)
        self.poly_client = PolymarketClient(self.core, self.logger)
        
        threading.Thread(target=self.futures_client.run, daemon=True).start()
        threading.Thread(target=self.spot_client.run, daemon=True).start()
        threading.Thread(target=self.poly_client.run, daemon=True).start()
    
    def stop_streams(self):
        if self.futures_client:
            self.futures_client.stop()
        if self.spot_client:
            self.spot_client.stop()
        if self.poly_client:
            self.poly_client.stop()
    
    def settle_previous_window(self):
        """Settle positions from previous window"""
        state = self.market.get_state()
        current = state['window_start']
        
        if current != self.last_window and self.last_window > 0:
            data = self.core.get_snapshot()
            settlement_price = data['oracle_price']
            
            if settlement_price > 0:
                old_slug = f"btc-updown-15m-{self.last_window}"
                
                # Get positions for this market
                positions = self.engine.positions.get(old_slug, [])
                
                if positions:
                    # Use the STORED strike from the position, not current oracle
                    stored_strike = positions[0].strike  # All positions have same strike
                    
                    results = self.engine.settle_market(
                        old_slug,
                        stored_strike,      # ← Use stored strike from position
                        settlement_price,   # ← Use current oracle as settlement
                        self.strategies
                    )
                    
                    for r in results:
                        pos = r['position']
                        status = "✅" if r['win'] else "❌"
                        self.logger.debug(
                            f"Settled: {pos.strategy_name} {pos.side.value} {status} "
                            f"Strike: ${stored_strike:.2f}, Settlement: ${settlement_price:.2f}, "
                            f"PnL: ${r['pnl']:.2f}"
                        )
        
        self.last_window = current
    
    def run_strategies(self):
        """Run all strategies and execute signals"""
        data = self.core.get_snapshot()
        state = self.market.get_state()
        
        # Skip if market not ready
        if not state.get('active') or state.get('strike', 0) <= 0:
            return
        
        now = time.time()
        
        for strategy in self.strategies:
            if not strategy.enabled:
                continue
            
            # Cooldown check
            last = self.last_signal_time.get(strategy.name, 0)
            if now - last < Config.SIGNAL_COOLDOWN:
                continue
            
            try:
                signal = strategy.analyze(data, state)
                
                if signal:
                    # Log signal
                    signal_data = {
                        'timestamp': datetime.now().isoformat(),
                        'strategy': strategy.name,
                        'signal_type': signal.signal_type.value,
                        'confidence': f"{signal.confidence:.3f}",
                        'size': f"{signal.suggested_size:.2f}",
                        'reason': signal.reason,
                        'spot_price': f"{data['spot_price']:.2f}",
                        'oracle_price': f"{data['oracle_price']:.2f}",
                        'oracle_age_ms': str(data['oracle_age_ms']),
                        'strike': f"{state['strike']:.2f}",
                        'time_left': f"{state['time_left']:.0f}",
                        'up_price': f"{state['up_price']:.4f}",
                        'down_price': f"{state['down_price']:.4f}",
                        'executed': '',
                        'reject_reason': ''
                    }
                    
                    # Try to execute
                    can_trade, reason = self.engine.can_trade(signal.suggested_size, state['slug'])
                    
                    if can_trade:
                        position = self.engine.execute_trade(signal, state)
                        if position:
                            self.last_signal_time[strategy.name] = now
                            signal_data['executed'] = 'YES'
                        else:
                            signal_data['executed'] = 'NO'
                            signal_data['reject_reason'] = 'Execution failed'
                    else:
                        signal_data['executed'] = 'NO'
                        signal_data['reject_reason'] = reason
                    
                    self.logger.log_signal(signal_data)
                    
            except Exception as e:
                self.logger.debug(f"Strategy {strategy.name} error: {e}")
    
    def run(self):
        """Main bot loop"""
        self.running = True
        self.start_streams()
        time.sleep(3)  # Wait for connections
        
        while self.running:
            try:
                # Check for new window
                self.market.lifecycle_check()
                
                # Fetch market details
                if not self.market.get_state()['market_found']:
                    self.market.fetch_market_details()
                
                # Fetch share prices
                self.market.fetch_share_prices()
                
                # Settle previous window
                self.settle_previous_window()
                
                # Run strategies
                self.run_strategies()
                
                time.sleep(Config.REFRESH_RATE)
                
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.logger.debug(f"Main loop error: {e}")
                time.sleep(1)
        
        self.stop_streams()


# ==========================================
# 🖥️ TERMINAL DISPLAY
# ==========================================

class TerminalDisplay:
    RESET = "\033[0m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    YELLOW = "\033[93m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    
    def __init__(self, bot: TradingBot):
        self.bot = bot
    
    def clear(self):
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    
    def render(self):
        C = TerminalDisplay
        
        data = self.bot.core.get_snapshot()
        market = self.bot.market.get_state()
        stats = self.bot.engine.get_stats()
        
        self.clear()
        
        # Header
        print(f"{C.CYAN}{C.BOLD}╔══════════════════════════════════════════════════════════════╗{C.RESET}")
        print(f"{C.CYAN}{C.BOLD}║         🤖 POLYMARKET PAPER TRADING BOT v2.1                ║{C.RESET}")
        print(f"{C.CYAN}{C.BOLD}╚══════════════════════════════════════════════════════════════╝{C.RESET}")
        
        # Connections
        fut = f"{C.GREEN}●{C.RESET}" if data['futures_connected'] else f"{C.RED}●{C.RESET}"
        spot = f"{C.GREEN}●{C.RESET}" if data['spot_connected'] else f"{C.RED}●{C.RESET}"
        poly = f"{C.GREEN}●{C.RESET}" if data['poly_connected'] else f"{C.RED}●{C.RESET}"
        print(f"\n📡 Futures {fut}  Spot {spot}  Polymarket {poly}")
        
        # Prices
        print(f"\n{'─'*64}")
        print(f"{'SOURCE':<18} {'PRICE':>14} {'AGE':>12} {'GAP':>12}")
        print(f"{'─'*64}")
        
        print(f"{'BINANCE FUTURES':<18} ${data['futures_price']:>12,.2f} {data['futures_age_ms']:>10}ms {'---':>12}")
        print(f"{'BINANCE SPOT':<18} ${data['spot_price']:>12,.2f} {data['spot_age_ms']:>10}ms {'---':>12}")
        print(f"{'POLY RELAY':<18} ${data['relay_price']:>12,.2f} {data['relay_age_ms']:>10}ms {'---':>12}")
        
        gap = data['oracle_gap']
        gap_col = C.GREEN if abs(gap) < 50 else C.YELLOW if abs(gap) < 100 else C.RED
        oracle_col = C.GREEN if data['oracle_age_ms'] < 5000 else C.RED
        print(f"{'CHAINLINK ORACLE':<18} {oracle_col}${data['oracle_price']:>12,.2f}{C.RESET} {data['oracle_age_ms']:>10}ms {gap_col}${gap:>+10.0f}{C.RESET}")
        
        # Market
        print(f"\n{'═'*64}")
        if market['active']:
            time_left = market['time_left']
            mins = int(time_left // 60)
            secs = int(time_left % 60)
            time_col = C.GREEN if time_left > 300 else C.YELLOW if time_left > 60 else C.RED
            
            print(f"🎯 STRIKE: {C.CYAN}${market['strike']:,.2f}{C.RESET}   ⏱️ {time_col}{mins}:{secs:02d}{C.RESET}")
            
            diff = data['spot_price'] - market['strike']
            if diff > 0:
                print(f"📊 STATUS: {C.GREEN}▲ UP WINNING (+${diff:.2f}){C.RESET}")
            else:
                print(f"📊 STATUS: {C.RED}▼ DOWN WINNING (${diff:.2f}){C.RESET}")
            
            print(f"\n💵 SHARES: {C.GREEN}UP {market['up_price']:.4f}{C.RESET}  {C.RED}DOWN {market['down_price']:.4f}{C.RESET}")
        else:
            print(f"{C.YELLOW}⚠️  Finding market...{C.RESET}")
        
        # Account
        print(f"\n{'═'*64}")
        pnl_col = C.GREEN if stats['pnl'] >= 0 else C.RED
        daily_col = C.GREEN if stats['daily_pnl'] >= 0 else C.RED
        
        print(f"{C.BOLD}💰 ACCOUNT{C.RESET}")
        print(f"Balance: ${stats['balance']:.2f}  |  P&L: {pnl_col}${stats['pnl']:+.2f} ({stats['pnl_pct']:+.1f}%){C.RESET}")
        print(f"Daily:   {daily_col}${stats['daily_pnl']:+.2f}{C.RESET}  |  Win Rate: {stats['win_rate']:.1f}% ({stats['wins']}W/{stats['losses']}L)")
        print(f"Open:    {stats['open_positions']} positions  |  Trades: {stats['trades']}")
        
        # Strategies
        print(f"\n{'═'*64}")
        print(f"{C.BOLD}📊 STRATEGIES{C.RESET}")
        print(f"{'─'*64}")
        print(f"{'NAME':<12} {'TRADES':>8} {'WIN%':>8} {'P&L':>12} {'STATUS':>10}")
        print(f"{'─'*64}")
        
        for s in self.bot.strategies:
            st = s.get_stats()
            pnl_c = C.GREEN if st['pnl'] >= 0 else C.RED
            status = f"{C.GREEN}ON{C.RESET}" if s.enabled else f"{C.RED}OFF{C.RESET}"
            print(f"{st['name']:<12} {st['trades']:>8} {st['win_rate']*100:>7.1f}% {pnl_c}${st['pnl']:>+10.2f}{C.RESET} {status:>10}")
        
        # Recent Trades
        print(f"\n{'═'*64}")
        print(f"{C.BOLD}📜 RECENT TRADES{C.RESET}")
        
        recent = self.bot.engine.all_trades[-5:] if self.bot.engine.all_trades else []
        if recent:
            for t in reversed(recent):
                time_str = datetime.fromtimestamp(t.entry_time).strftime("%H:%M:%S")
                side_col = C.GREEN if t.side == Side.UP else C.RED
                print(f"  {time_str} {t.strategy_name:<12} {side_col}{t.side.value:>4}{C.RESET} ${t.size_usd:.2f} @ {t.entry_price:.4f}")
        else:
            print(f"  {C.GREY}No trades yet...{C.RESET}")
        
        # Footer
        print(f"\n{'═'*64}")
        print(f"{C.GREY}Logs: {self.bot.logger.get_session_path()}{C.RESET}")
        print(f"{C.GREY}Press Ctrl+C to exit{C.RESET}")


# ==========================================
# 🚀 MAIN
# ==========================================

def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║         🤖 POLYMARKET BTC PAPER TRADING BOT v2.1            ║
║                                                              ║
║  Strategies:                                                 ║
║  • OracleLag  - Exploits Chainlink delay                    ║
║  • EndGame    - Final seconds when oracle locked            ║
║  • Momentum   - Rides strong moves                          ║
║  • VolFade    - Mean reversion after spikes                 ║
║  • Proximity  - Trades near strike                          ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    # Get capital
    try:
        inp = input("💰 Starting capital (default $100): ").strip()
        capital = float(inp) if inp else 100.0
    except:
        capital = 100.0
    
    print(f"\n⚙️  Starting with ${capital:.2f}...")
    print("📁 Logs will be saved to: paper_trading_logs/")
    print("\n" + "="*64 + "\n")
    
    # Create bot
    bot = TradingBot(capital=capital)
    display = TerminalDisplay(bot)
    
    # Run bot in background
    bot_thread = threading.Thread(target=bot.run, daemon=True)
    bot_thread.start()
    
    # Display loop
    try:
        while True:
            display.render()
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n\n👋 Shutting down...")
        bot.running = False
        time.sleep(1)
        
        # Final stats
        stats = bot.engine.get_stats()
        print(f"\n{'='*64}")
        print(f"📊 FINAL RESULTS")
        print(f"{'='*64}")
        print(f"Starting: ${bot.engine.starting_capital:.2f}")
        print(f"Final:    ${stats['balance']:.2f}")
        print(f"P&L:      ${stats['pnl']:+.2f} ({stats['pnl_pct']:+.1f}%)")
        print(f"Trades:   {stats['trades']}")
        print(f"Win Rate: {stats['win_rate']:.1f}%")
        print(f"\n📁 Logs: {bot.logger.get_session_path()}")
        print("="*64)


if __name__ == "__main__":
    main()