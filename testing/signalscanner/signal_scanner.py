#!/usr/bin/env python3
"""
BTC 15-Min Oracle Lag Trade Identifier & Logger
Identifies profitable trades and logs them for backtesting analysis
"""

import time
import json
import requests
import csv
from datetime import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass, asdict
import threading
from collections import deque

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class MarketState:
    """Current market state"""
    slug: str = ""
    window_start: int = 0
    strike: float = 0.0
    up_token: str = ""
    down_token: str = ""
    time_left: int = 0

    def is_active(self) -> bool:
        return self.strike > 0 and self.up_token != ""


@dataclass
class PriceData:
    """Real-time price data"""
    timestamp: int
    spot: float = 0.0
    oracle: float = 0.0
    futures: float = 0.0
    up_ask: float = 0.0
    up_bid: float = 0.0
    up_size: float = 0.0
    down_ask: float = 0.0
    down_bid: float = 0.0
    down_size: float = 0.0

    def oracle_lag(self) -> float:
        return self.spot - self.oracle

    def is_complete(self) -> bool:
        return all([
            self.spot > 0,
            self.oracle > 0,
            self.up_ask > 0,
            self.down_ask > 0
        ])


@dataclass
class TradeSignal:
    """Identified trade opportunity"""
    timestamp: int
    window_start: int
    slug: str
    side: str  # 'UP' or 'DOWN'
    entry_price: float
    fair_value: float
    gross_edge: float
    net_edge: float
    oracle_lag: float
    confidence: float
    strike: float
    spot: float
    oracle: float
    time_left: int
    available_size: float
    spread: float

    # For backtesting validation
    actual_outcome: Optional[str] = None  # 'WIN' or 'LOSS'
    exit_price: Optional[float] = None
    actual_pnl: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================================
# MARKET MANAGER
# ============================================================================

class MarketManager:
    """Manages current 15-min market window"""

    GAMMA_API = "https://gamma-api.polymarket.com"
    WINDOW_SIZE = 900  # 15 minutes

    def __init__(self):
        self.state = MarketState()

    def get_current_window(self) -> int:
        """Calculate current 15-min window timestamp"""
        current_time = int(time.time())
        return (current_time // self.WINDOW_SIZE) * self.WINDOW_SIZE

    def time_left(self) -> int:
        """Seconds remaining in current window"""
        if self.state.window_start == 0:
            return 0
        return max(0, (self.state.window_start + self.WINDOW_SIZE) - int(time.time()))

    def update(self) -> bool:
        """Check for new window and fetch tokens"""
        window = self.get_current_window()

        if window != self.state.window_start:
            self.state.window_start = window
            self.state.slug = f"btc-updown-15m-{window}"
            return self.fetch_tokens()

        self.state.time_left = self.time_left()
        return self.state.is_active()

    def fetch_tokens(self) -> bool:
        """Fetch token IDs and strike from Gamma API"""
        try:
            url = f"{self.GAMMA_API}/events"
            params = {'slug': self.state.slug}

            # Retry up to 5 times (market creation takes 30-60 sec)
            for attempt in range(5):
                r = requests.get(url, params=params, timeout=10)

                if r.status_code == 200 and r.json():
                    data = r.json()[0]
                    market = data['markets'][0]

                    # Parse tokens
                    token_ids = json.loads(market['clobTokenIds'])
                    outcomes = json.loads(market['outcomes'])

                    if outcomes[0].lower() in ['up', 'yes']:
                        self.state.up_token = token_ids[0]
                        self.state.down_token = token_ids[1]
                    else:
                        self.state.up_token = token_ids[1]
                        self.state.down_token = token_ids[0]

                    # Extract strike from question
                    import re
                    question = market['question']
                    match = re.search(r'\$([\d,]+\.?\d*)', question)
                    if match:
                        self.state.strike = float(match.group(1).replace(',', ''))

                    print(f"\n[MARKET] New window: {self.state.slug}")
                    print(f"[MARKET] Strike: ${self.state.strike:,.2f}")
                    print(f"[MARKET] UP: {self.state.up_token[:8]}...")
                    print(f"[MARKET] DOWN: {self.state.down_token[:8]}...")
                    return True

                if attempt < 4:
                    print(f"[MARKET] Waiting for market creation... ({attempt+1}/5)")
                    time.sleep(10)

            print(f"[ERROR] Market not found: {self.state.slug}")
            return False

        except Exception as e:
            print(f"[ERROR] Fetch tokens failed: {e}")
            return False


# ============================================================================
# PRICE FEED MANAGER
# ============================================================================

class PriceFeedManager:
    """Manages real-time price data from APIs"""

    CLOB_API = "https://clob.polymarket.com"
    BINANCE_SPOT = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    BINANCE_FUTURES = "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT"

    def __init__(self):
        self.prices = PriceData(timestamp=int(time.time()))
        self.oracle_prices = deque(maxlen=100)  # Track oracle updates

    def update_spot(self):
        """Fetch Binance spot price"""
        try:
            r = requests.get(self.BINANCE_SPOT, timeout=5)
            data = r.json()
            self.prices.spot = float(data['price'])
        except Exception as e:
            print(f"[ERROR] Spot price: {e}")

    def update_futures(self):
        """Fetch Binance futures price"""
        try:
            r = requests.get(self.BINANCE_FUTURES, timeout=5)
            data = r.json()
            self.prices.futures = float(data['price'])
        except Exception as e:
            print(f"[ERROR] Futures price: {e}")

    def update_oracle(self):
        """Simulate oracle price (use Polymarket relay in production)"""
        # In production, connect to: wss://ws-live-data.polymarket.com
        # For now, simulate with slight lag
        if self.prices.spot > 0:
            # Oracle lags spot by 5-15 seconds typically
            if len(self.oracle_prices) > 0:
                prev_oracle = self.oracle_prices[-1]
                # Catch up 70% of the way to spot
                self.prices.oracle = prev_oracle + (self.prices.spot - prev_oracle) * 0.15
            else:
                self.prices.oracle = self.prices.spot * 0.9998  # Start slightly behind

            self.oracle_prices.append(self.prices.oracle)

    def update_orderbook(self, up_token: str, down_token: str):
        """Fetch orderbook from CLOB API"""
        try:
            # UP orderbook
            r = requests.get(f"{self.CLOB_API}/book", params={'token_id': up_token}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                asks = data.get('asks', [])
                bids = data.get('bids', [])

                if asks:
                    best_ask = min(float(a['price']) for a in asks)
                    self.prices.up_ask = best_ask
                    self.prices.up_size = sum(float(a['size']) for a in asks if float(a['price']) == best_ask)

                if bids:
                    self.prices.up_bid = max(float(b['price']) for b in bids)

            # DOWN orderbook
            r = requests.get(f"{self.CLOB_API}/book", params={'token_id': down_token}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                asks = data.get('asks', [])
                bids = data.get('bids', [])

                if asks:
                    best_ask = min(float(a['price']) for a in asks)
                    self.prices.down_ask = best_ask
                    self.prices.down_size = sum(float(a['size']) for a in asks if float(a['price']) == best_ask)

                if bids:
                    self.prices.down_bid = max(float(b['price']) for b in bids)

        except Exception as e:
            print(f"[ERROR] Orderbook: {e}")

    def update_all(self, market: MarketState):
        """Update all price feeds"""
        self.prices.timestamp = int(time.time())

        self.update_spot()
        self.update_futures()
        self.update_oracle()

        if market.is_active():
            self.update_orderbook(market.up_token, market.down_token)


# ============================================================================
# SIGNAL GENERATOR
# ============================================================================

class SignalGenerator:
    """Identifies profitable trade opportunities"""

    def __init__(self, min_edge: float = 0.05):
        self.min_edge = min_edge  # 5% minimum net edge
        self.signals_found = []

    def calculate_signal(self, market: MarketState, prices: PriceData) -> Optional[TradeSignal]:
        """Calculate if there's a tradeable opportunity"""

        # Only trade first 10 minutes
        time_left = market.time_left
        if time_left < 600 or time_left > 890:
            return None

        # Need complete price data
        if not prices.is_complete():
            return None

        # Calculate oracle lag
        lag = prices.oracle_lag()

        # Only trade significant lag (>$20)
        if abs(lag) < 20:
            return None

        # Project where oracle will be
        projected_oracle = prices.oracle + (lag * 0.7)

        # Calculate true probability
        if projected_oracle > market.strike:
            # BTC likely UP
            distance_pct = (projected_oracle - market.strike) / market.strike
            true_prob_up = 0.50 + min(0.45, distance_pct * 50)

            # Calculate edge
            gross_edge = true_prob_up - prices.up_ask
            fee = true_prob_up * 0.02 * (1 - prices.up_ask)
            net_edge = gross_edge - fee

            if net_edge >= self.min_edge:
                spread = prices.up_ask - prices.up_bid

                signal = TradeSignal(
                    timestamp=prices.timestamp,
                    window_start=market.window_start,
                    slug=market.slug,
                    side='UP',
                    entry_price=prices.up_ask,
                    fair_value=true_prob_up,
                    gross_edge=gross_edge,
                    net_edge=net_edge,
                    oracle_lag=lag,
                    confidence=min(1.0, abs(lag) / 50),
                    strike=market.strike,
                    spot=prices.spot,
                    oracle=prices.oracle,
                    time_left=time_left,
                    available_size=prices.up_size,
                    spread=spread
                )
                return signal

        else:
            # BTC likely DOWN
            distance_pct = (market.strike - projected_oracle) / market.strike
            true_prob_down = 0.50 + min(0.45, distance_pct * 50)

            gross_edge = true_prob_down - prices.down_ask
            fee = true_prob_down * 0.02 * (1 - prices.down_ask)
            net_edge = gross_edge - fee

            if net_edge >= self.min_edge:
                spread = prices.down_ask - prices.down_bid

                signal = TradeSignal(
                    timestamp=prices.timestamp,
                    window_start=market.window_start,
                    slug=market.slug,
                    side='DOWN',
                    entry_price=prices.down_ask,
                    fair_value=true_prob_down,
                    gross_edge=gross_edge,
                    net_edge=net_edge,
                    oracle_lag=lag,
                    confidence=min(1.0, abs(lag) / 50),
                    strike=market.strike,
                    spot=prices.spot,
                    oracle=prices.oracle,
                    time_left=time_left,
                    available_size=prices.down_size,
                    spread=spread
                )
                return signal

        return None


# ============================================================================
# TRADE LOGGER
# ============================================================================

class TradeLogger:
    """Logs all identified trades to CSV"""

    def __init__(self, filename: str = "trade_signals.csv"):
        self.filename = filename
        self.signals_logged = 0

        # Create CSV with headers
        with open(filename, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'timestamp', 'datetime', 'window_start', 'slug', 'side',
                'entry_price', 'fair_value', 'gross_edge', 'net_edge',
                'oracle_lag', 'confidence', 'strike', 'spot', 'oracle',
                'time_left', 'available_size', 'spread',
                'actual_outcome', 'exit_price', 'actual_pnl'
            ])
            writer.writeheader()

        print(f"[LOGGER] Created log file: {filename}")

    def log_signal(self, signal: TradeSignal):
        """Append signal to CSV"""
        with open(self.filename, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'timestamp', 'datetime', 'window_start', 'slug', 'side',
                'entry_price', 'fair_value', 'gross_edge', 'net_edge',
                'oracle_lag', 'confidence', 'strike', 'spot', 'oracle',
                'time_left', 'available_size', 'spread',
                'actual_outcome', 'exit_price', 'actual_pnl'
            ])

            row = signal.to_dict()
            row['datetime'] = datetime.fromtimestamp(signal.timestamp).strftime('%Y-%m-%d %H:%M:%S')
            writer.writerow(row)

        self.signals_logged += 1

        print(f"\n{'='*70}")
        print(f"[SIGNAL #{self.signals_logged}] {signal.side} @ ${signal.entry_price:.4f}")
        print(f"{'='*70}")
        print(f"Time: {datetime.fromtimestamp(signal.timestamp).strftime('%H:%M:%S')}")
        print(f"Window: {signal.time_left}s left")
        print(f"Strike: ${signal.strike:,.2f}")
        print(f"Spot: ${signal.spot:,.2f} | Oracle: ${signal.oracle:,.2f}")
        print(f"Oracle Lag: ${signal.oracle_lag:+.2f}")
        print(f"Fair Value: {signal.fair_value:.4f} | Entry: {signal.entry_price:.4f}")
        print(f"Gross Edge: {signal.gross_edge*100:+.2f}% | Net Edge: {signal.net_edge*100:+.2f}%")
        print(f"Confidence: {signal.confidence*100:.1f}%")
        print(f"Available: {signal.available_size:.0f} shares | Spread: {signal.spread*100:.2f}%")
        print(f"{'='*70}")

    def print_summary(self):
        """Print summary statistics"""
        print(f"\n{'='*70}")
        print(f"SESSION SUMMARY")
        print(f"{'='*70}")
        print(f"Total signals identified: {self.signals_logged}")
        print(f"Log file: {self.filename}")
        print(f"{'='*70}")


# ============================================================================
# MAIN SIGNAL SCANNER
# ============================================================================

class SignalScanner:
    """Main scanner loop"""

    def __init__(self, min_edge: float = 0.05, update_interval: float = 2.0):
        self.market = MarketManager()
        self.prices = PriceFeedManager()
        self.generator = SignalGenerator(min_edge=min_edge)
        self.logger = TradeLogger()
        self.update_interval = update_interval
        self.running = False
        self.signals_this_window = set()  # Avoid duplicate signals

    def run(self, duration_minutes: Optional[int] = None):
        """Run scanner for specified duration (None = infinite)"""
        self.running = True
        start_time = time.time()

        print(f"\n{'='*70}")
        print(f"BTC 15-MIN ORACLE LAG SIGNAL SCANNER")
        print(f"{'='*70}")
        print(f"Minimum edge: {self.generator.min_edge*100:.1f}%")
        print(f"Update interval: {self.update_interval}s")
        if duration_minutes:
            print(f"Duration: {duration_minutes} minutes")
        print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*70}")

        try:
            while self.running:
                # Check for new window
                new_window = self.market.update()

                if new_window:
                    self.signals_this_window.clear()
                    print(f"\n[INFO] New window detected, clearing signal cache")

                # Update prices
                if self.market.state.is_active():
                    self.prices.update_all(self.market.state)

                    # Check for signal
                    signal = self.generator.calculate_signal(
                        self.market.state,
                        self.prices.prices
                    )

                    if signal:
                        # Create unique key to avoid duplicates
                        signal_key = f"{signal.window_start}_{signal.side}_{signal.entry_price:.4f}"

                        if signal_key not in self.signals_this_window:
                            self.logger.log_signal(signal)
                            self.signals_this_window.add(signal_key)

                # Check duration
                if duration_minutes:
                    elapsed_minutes = (time.time() - start_time) / 60
                    if elapsed_minutes >= duration_minutes:
                        print(f"\n[INFO] Duration limit reached ({duration_minutes} min)")
                        break

                time.sleep(self.update_interval)

        except KeyboardInterrupt:
            print(f"\n[INFO] Stopped by user")

        finally:
            self.running = False
            self.logger.print_summary()
            print(f"\nStopped: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


# ============================================================================
# ANALYZER
# ============================================================================

class SignalAnalyzer:
    """Analyze logged signals"""

    @staticmethod
    def analyze(filename: str = "trade_signals.csv"):
        """Generate analysis report"""
        try:
            import pandas as pd

            df = pd.read_csv(filename)

            if len(df) == 0:
                print("No signals found in log file.")
                return

            print(f"\n{'='*70}")
            print(f"SIGNAL ANALYSIS REPORT")
            print(f"{'='*70}")
            print(f"Total signals: {len(df)}")
            print(f"Time range: {df['datetime'].iloc[0]} to {df['datetime'].iloc[-1]}")
            print(f"")

            print(f"SIDE DISTRIBUTION:")
            print(df['side'].value_counts())
            print(f"")

            print(f"EDGE STATISTICS:")
            print(f"  Avg net edge: {df['net_edge'].mean()*100:.2f}%")
            print(f"  Max net edge: {df['net_edge'].max()*100:.2f}%")
            print(f"  Min net edge: {df['net_edge'].min()*100:.2f}%")
            print(f"  Std dev: {df['net_edge'].std()*100:.2f}%")
            print(f"")

            print(f"ORACLE LAG STATISTICS:")
            print(f"  Avg lag: ${df['oracle_lag'].mean():.2f}")
            print(f"  Max lag: ${df['oracle_lag'].max():.2f}")
            print(f"  Min lag: ${df['oracle_lag'].min():.2f}")
            print(f"")

            print(f"CONFIDENCE STATISTICS:")
            print(f"  Avg confidence: {df['confidence'].mean()*100:.1f}%")
            print(f"  High confidence (>70%): {len(df[df['confidence'] > 0.7])}")
            print(f"")

            print(f"LIQUIDITY:")
            print(f"  Avg available size: {df['available_size'].mean():.0f} shares")
            print(f"  Min available size: {df['available_size'].min():.0f} shares")
            print(f"")

            print(f"SIGNALS PER PHASE:")
            df['phase'] = (df['time_left'] // 180).astype(int)  # 0-5 phases
            print(df['phase'].value_counts().sort_index())
            print(f"  (0=last 3min, 1=9-12min, 2=6-9min, 3=3-6min, 4=0-3min)")
            print(f"")

            print(f"{'='*70}")

        except ImportError:
            print("Install pandas for analysis: pip install pandas")
        except Exception as e:
            print(f"Analysis error: {e}")


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import sys

    # Configuration
    MIN_EDGE = 0.05  # 5% minimum net edge
    UPDATE_INTERVAL = 2.0  # Check every 2 seconds
    DURATION_HOURS = 3  # Run for 3 hours (adjust as needed)

    print("""
╔══════════════════════════════════════════════════════════════════╗
║     BTC 15-MIN ORACLE LAG SIGNAL IDENTIFIER & LOGGER            ║
╚══════════════════════════════════════════════════════════════════╝

This tool:
  ✓ Monitors BTC 15-min Polymarket markets
  ✓ Identifies oracle lag arbitrage opportunities
  ✓ Logs all signals with complete data to CSV
  ✓ Tracks edge, confidence, liquidity

After running, analyze results with:
  python script.py analyze
    """)

    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        # Analyze mode
        SignalAnalyzer.analyze()
    else:
        # Scanning mode
        scanner = SignalScanner(
            min_edge=MIN_EDGE,
            update_interval=UPDATE_INTERVAL
        )

        scanner.run(duration_minutes=DURATION_HOURS * 60)
