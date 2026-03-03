"""
=============================================================================
🤖 POLYMARKET PAPER TRADING BOT v1.0
=============================================================================
Automated trading system for BTC 15-minute UP/DOWN markets
- Real-time data from Binance Spot, Polymarket RTDS, and Chainlink
- Automated signal generation based on momentum + oracle lag
- Paper trading with full trade logging and analytics
- Live dashboard with performance metrics
"""

import time
import requests
import json
import threading
import websocket
import sys
from datetime import datetime, timezone
from collections import deque
import csv
import os

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
class Config:
    # Trading Parameters
    STARTING_CAPITAL = 1000.0  # USD
    RISK_PER_TRADE = 0.03  # 3% of capital per trade
    MAX_POSITION_SIZE = 100  # Max shares per trade

    # Signal Thresholds
    MIN_PRICE_MOVE = 80  # Minimum BTC price move in USD
    MIN_ORACLE_LAG = 50  # Minimum oracle lag in USD
    MAX_ENTRY_TIME = 90  # Max seconds into window to enter
    EXIT_TIME = 810  # Exit all positions at 13:30 (810 seconds)

    # Entry Limits
    MAX_ENTRY_PRICE = 0.60  # Never buy above this probability
    MIN_ENTRY_PRICE = 0.45  # Never buy below this (too risky)
    TARGET_ENTRY = 0.53  # Ideal entry price

    # Exit Rules
    STOP_LOSS_THRESHOLD = 0.40  # Exit if drops below
    PROFIT_TARGET = 0.65  # Take profit at
    TRAILING_STOP = 0.08  # Trail by 8 cents

    # Files
    TRADE_LOG = "trade_history.csv"
    METRICS_LOG = "performance_metrics.json"

# ==========================================
# 📊 DATA CORE
# ==========================================
class DataCore:
    def __init__(self):
        self.spot_price = 0.0
        self.spot_ts = 0
        self.poly_relay_price = 0.0
        self.poly_relay_ts = 0
        self.oracle_price = 0.0
        self.oracle_ts = 0

        # Price history for analytics
        self.spot_history = deque(maxlen=100)
        self.oracle_history = deque(maxlen=100)

    def update_spot(self, price, ts):
        self.spot_price = float(price)
        self.spot_ts = int(ts)
        self.spot_history.append((ts, float(price)))

    def update_relay(self, price, ts):
        self.poly_relay_price = float(price)
        self.poly_relay_ts = int(ts)

    def update_oracle(self, price, ts):
        self.oracle_price = float(price)
        self.oracle_ts = int(ts)
        self.oracle_history.append((ts, float(price)))

    def get_spot_velocity(self, window_seconds=60):
        """Calculate price change over last N seconds"""
        if len(self.spot_history) < 2:
            return 0.0
        now = int(time.time() * 1000)
        cutoff = now - (window_seconds * 1000)
        recent = [p for t, p in self.spot_history if t > cutoff]
        if len(recent) < 2:
            return 0.0
        return recent[-1] - recent[0]

    def get_oracle_lag(self):
        """Calculate current oracle vs spot gap"""
        return self.oracle_price - self.spot_price

core = DataCore()

# ==========================================
# 📡 WEBSOCKET CLIENTS
# ==========================================
class BinanceSpotClient:
    def __init__(self, data_core):
        self.core = data_core
        self.url = "wss://stream.binance.com:9443/ws/btcusdt@trade"

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        self.url,
                        on_message=lambda ws, msg: self._on_msg(msg)
                    )
                    ws.run_forever()
                except:
                    time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _on_msg(self, msg):
        try:
            d = json.loads(msg)
            self.core.update_spot(d['p'], d['T'])
        except:
            pass

class PolyDataClient:
    def __init__(self, data_core):
        self.core = data_core
        self.url = "wss://ws-live-data.polymarket.com"

    def start(self):
        def on_open(ws):
            # Subscribe to Binance relay and Chainlink oracle
            ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [{
                    "topic": "crypto_prices",
                    "type": "*",
                    "filters": "{\"symbol\":\"btcusdt\"}"
                }]
            }))
            ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [{
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": "{\"symbol\":\"btc/usd\"}"
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
                        self.core.update_relay(payload['value'], payload['timestamp'])
                    elif topic == 'crypto_prices_chainlink' and symbol == 'btc/usd':
                        self.core.update_oracle(payload['value'], payload['timestamp'])
            except:
                pass

        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        self.url,
                        on_open=on_open,
                        on_message=on_msg
                    )
                    ws.run_forever()
                except:
                    time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

# ==========================================
# 🎯 MARKET MANAGER
# ==========================================
class MarketManager:
    def __init__(self):
        self.session = requests.Session()
        self.window_start = 0
        self.slug = ""
        self.strike_price = 0.0
        self.ids = {"UP": None, "DOWN": None}
        self.prices = {"UP": 0.0, "DOWN": 0.0}
        self.market_created = False

    def check_window_transition(self):
        """Returns True if window changed"""
        now = int(time.time())
        current_window = (now // 900) * 900

        if current_window != self.window_start:
            self.window_start = current_window
            self.slug = f"btc-updown-15m-{current_window}"
            self.ids = {"UP": None, "DOWN": None}
            self.strike_price = 0.0
            self.market_created = False

            # Snapshot oracle price as strike
            if core.oracle_price > 0:
                self.strike_price = core.oracle_price
            return True
        return False

    def fetch_market_details(self):
        """Fetch token IDs and strike price"""
        if self.market_created:
            return True

        try:
            url = f"https://gamma-api.polymarket.com/events/slug/{self.slug}"
            r = self.session.get(url, timeout=3)

            if r.status_code == 200:
                data = r.json()
                market = data['markets'][0]

                # Get token IDs
                t_ids = json.loads(market['clobTokenIds'])
                outcomes = json.loads(market['outcomes'])

                if outcomes[0] == "Up":
                    self.ids["UP"] = t_ids[0]
                    self.ids["DOWN"] = t_ids[1]
                else:
                    self.ids["UP"] = t_ids[1]
                    self.ids["DOWN"] = t_ids[0]

                # Extract strike from question if available
                import re
                match = re.search(r'\$([\d,]+\.?\d*)', market['question'])
                if match and self.strike_price == 0.0:
                    self.strike_price = float(match.group(1).replace(',', ''))

                self.market_created = True
                return True
        except:
            pass
        return False

    def fetch_live_prices(self):
        """Get current YES/NO share prices"""
        if not self.market_created:
            return

        try:
            # Get UP price
            r = self.session.get(
                f"https://clob.polymarket.com/price?token_id={self.ids['UP']}&side=buy",
                timeout=2
            )
            if r.status_code == 200:
                self.prices["UP"] = float(r.json()['price'])

            # Get DOWN price
            r = self.session.get(
                f"https://clob.polymarket.com/price?token_id={self.ids['DOWN']}&side=buy",
                timeout=2
            )
            if r.status_code == 200:
                self.prices["DOWN"] = float(r.json()['price'])
        except:
            pass

    def get_time_in_window(self):
        """Seconds elapsed in current window"""
        return int(time.time()) - self.window_start

# ==========================================
# 🧠 TRADING STRATEGY
# ==========================================
class TradingStrategy:
    def __init__(self, market_manager):
        self.market = market_manager
        self.window_start_price = 0.0
        self.entry_checked = False

    def reset_for_new_window(self):
        """Reset state when window changes"""
        self.window_start_price = core.spot_price
        self.entry_checked = False

    def should_enter_trade(self):
        """
        Returns: (should_trade, direction, confidence)
        direction: 'UP' or 'DOWN' or None
        confidence: 0.0 to 1.0
        """
        # Only check once per window during entry period
        if self.entry_checked:
            return False, None, 0.0

        time_in_window = self.market.get_time_in_window()

        # Must be within entry window
        if time_in_window > Config.MAX_ENTRY_TIME:
            self.entry_checked = True
            return False, None, 0.0

        # Need valid data
        if self.window_start_price == 0 or core.spot_price == 0:
            return False, None, 0.0

        # Calculate signals
        price_move = core.spot_price - self.window_start_price
        oracle_lag = abs(core.get_oracle_lag())
        spot_velocity = core.get_spot_velocity(60)

        # Determine direction
        direction = 'UP' if price_move > 0 else 'DOWN'

        # Check thresholds
        move_strength = abs(price_move) >= Config.MIN_PRICE_MOVE
        oracle_lagging = oracle_lag >= Config.MIN_ORACLE_LAG
        momentum_confirmed = abs(spot_velocity) >= Config.MIN_PRICE_MOVE * 0.7

        # Calculate confidence score
        confidence = 0.0
        if move_strength:
            confidence += 0.4
        if oracle_lagging:
            confidence += 0.3
        if momentum_confirmed:
            confidence += 0.3

        # Need at least 70% confidence
        if confidence >= 0.7:
            # Check if share price is in acceptable range
            share_price = self.market.prices.get(direction, 0)

            if Config.MIN_ENTRY_PRICE <= share_price <= Config.MAX_ENTRY_PRICE:
                self.entry_checked = True
                return True, direction, confidence

        return False, None, confidence

# ==========================================
# 💼 POSITION MANAGER
# ==========================================
class Position:
    def __init__(self, direction, entry_price, size, strike_price, entry_time):
        self.direction = direction
        self.entry_price = entry_price
        self.size = size
        self.strike_price = strike_price
        self.entry_time = entry_time
        self.exit_price = None
        self.exit_time = None
        self.pnl = 0.0
        self.status = 'OPEN'  # OPEN, CLOSED, SETTLED
        self.exit_reason = None
        self.highest_price = entry_price

    def update_trailing_stop(self, current_price):
        """Update highest price for trailing stop"""
        if current_price > self.highest_price:
            self.highest_price = current_price

    def check_exit_conditions(self, current_price, time_in_window):
        """
        Returns: (should_exit, reason)
        """
        # Force exit near window end
        if time_in_window >= Config.EXIT_TIME:
            return True, "TIME_EXIT"

        # Stop loss
        if current_price < Config.STOP_LOSS_THRESHOLD:
            return True, "STOP_LOSS"

        # Profit target
        if current_price >= Config.PROFIT_TARGET:
            return True, "PROFIT_TARGET"

        # Trailing stop
        if self.highest_price - current_price >= Config.TRAILING_STOP:
            return True, "TRAILING_STOP"

        return False, None

    def close(self, exit_price, reason):
        """Close position"""
        self.exit_price = exit_price
        self.exit_time = datetime.now()
        self.exit_reason = reason
        self.status = 'CLOSED'

        # Calculate P&L
        self.pnl = (exit_price - self.entry_price) * self.size

    def settle(self, won):
        """Settle at end of window"""
        self.status = 'SETTLED'
        final_price = 1.0 if won else 0.0
        self.pnl = (final_price - self.entry_price) * self.size

class PositionManager:
    def __init__(self):
        self.current_position = None
        self.capital = Config.STARTING_CAPITAL
        self.trade_history = []

    def open_position(self, direction, entry_price, strike_price, confidence):
        """Open new position"""
        if self.current_position is not None:
            return False

        # Calculate position size
        risk_amount = self.capital * Config.RISK_PER_TRADE
        size = min(int(risk_amount / entry_price), Config.MAX_POSITION_SIZE)

        if size == 0:
            return False

        self.current_position = Position(
            direction=direction,
            entry_price=entry_price,
            size=size,
            strike_price=strike_price,
            entry_time=datetime.now()
        )

        return True

    def update_position(self, current_price, time_in_window):
        """Update position and check exit conditions"""
        if self.current_position is None:
            return

        self.current_position.update_trailing_stop(current_price)

        should_exit, reason = self.current_position.check_exit_conditions(
            current_price, time_in_window
        )

        if should_exit:
            self.close_position(current_price, reason)

    def close_position(self, exit_price, reason):
        """Close current position"""
        if self.current_position is None:
            return

        self.current_position.close(exit_price, reason)
        self.capital += self.current_position.pnl
        self.trade_history.append(self.current_position)
        self.current_position = None

    def settle_position(self, final_spot_price):
        """Settle position at window end"""
        if self.current_position is None:
            return

        won = (
            (self.current_position.direction == 'UP' and 
             final_spot_price > self.current_position.strike_price) or
            (self.current_position.direction == 'DOWN' and 
             final_spot_price <= self.current_position.strike_price)
        )

        self.current_position.settle(won)
        self.capital += self.current_position.pnl
        self.trade_history.append(self.current_position)
        self.current_position = None

# ==========================================
# 📝 LOGGING SYSTEM
# ==========================================
class TradeLogger:
    def __init__(self):
        self.init_csv()

    def init_csv(self):
        """Initialize CSV file with headers"""
        if not os.path.exists(Config.TRADE_LOG):
            with open(Config.TRADE_LOG, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'window_start', 'direction', 'entry_price',
                    'exit_price', 'size', 'strike_price', 'final_spot_price',
                    'pnl', 'capital_after', 'exit_reason', 'duration_seconds'
                ])

    def log_trade(self, position, capital):
        """Log completed trade"""
        with open(Config.TRADE_LOG, 'a', newline='') as f:
            writer = csv.writer(f)

            duration = (position.exit_time - position.entry_time).total_seconds()

            writer.writerow([
                position.entry_time.isoformat(),
                datetime.fromtimestamp(position.entry_time.timestamp() // 900 * 900).isoformat(),
                position.direction,
                position.entry_price,
                position.exit_price,
                position.size,
                position.strike_price,
                core.spot_price,
                f"{position.pnl:.2f}",
                f"{capital:.2f}",
                position.exit_reason,
                int(duration)
            ])

    def save_metrics(self, metrics):
        """Save performance metrics"""
        with open(Config.METRICS_LOG, 'w') as f:
            json.dump(metrics, f, indent=2)

logger = TradeLogger()

# ==========================================
# 📊 ANALYTICS ENGINE
# ==========================================
class Analytics:
    @staticmethod
    def calculate_metrics(trade_history, current_capital):
        """Calculate comprehensive performance metrics"""
        if not trade_history:
            return {
                'total_trades': 0,
                'win_rate': 0.0,
                'avg_pnl': 0.0,
                'total_pnl': 0.0,
                'capital': current_capital,
                'roi': 0.0
            }

        total = len(trade_history)
        wins = len([t for t in trade_history if t.pnl > 0])
        losses = len([t for t in trade_history if t.pnl <= 0])

        win_rate = (wins / total * 100) if total > 0 else 0

        total_pnl = sum(t.pnl for t in trade_history)
        avg_pnl = total_pnl / total if total > 0 else 0

        winning_trades = [t.pnl for t in trade_history if t.pnl > 0]
        losing_trades = [t.pnl for t in trade_history if t.pnl <= 0]

        avg_win = sum(winning_trades) / len(winning_trades) if winning_trades else 0
        avg_loss = sum(losing_trades) / len(losing_trades) if losing_trades else 0

        roi = ((current_capital - Config.STARTING_CAPITAL) / Config.STARTING_CAPITAL * 100)

        return {
            'total_trades': total,
            'wins': wins,
            'losses': losses,
            'win_rate': win_rate,
            'avg_pnl': avg_pnl,
            'total_pnl': total_pnl,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'capital': current_capital,
            'starting_capital': Config.STARTING_CAPITAL,
            'roi': roi,
            'exit_reasons': {
                'STOP_LOSS': len([t for t in trade_history if t.exit_reason == 'STOP_LOSS']),
                'PROFIT_TARGET': len([t for t in trade_history if t.exit_reason == 'PROFIT_TARGET']),
                'TRAILING_STOP': len([t for t in trade_history if t.exit_reason == 'TRAILING_STOP']),
                'TIME_EXIT': len([t for t in trade_history if t.exit_reason == 'TIME_EXIT'])
            }
        }

# ==========================================
# 🎨 DASHBOARD
# ==========================================
def render_dashboard(market, strategy, position_mgr):
    """Render live terminal dashboard"""
    os.system('cls' if os.name == 'nt' else 'clear')

    C_GREEN = "\033[92m"
    C_RED = "\033[91m"
    C_CYAN = "\033[96m"
    C_YELLOW = "\033[93m"
    C_RESET = "\033[0m"

    print(f"{C_CYAN}╔{'═'*78}╗{C_RESET}")
    print(f"{C_CYAN}║{' '*20}🤖 POLYMARKET PAPER TRADING BOT{' '*25}║{C_RESET}")
    print(f"{C_CYAN}╚{'═'*78}╝{C_RESET}\n")

    # === MARKET STATUS ===
    time_in_window = market.get_time_in_window()
    time_left = 900 - time_in_window
    mins, secs = time_left // 60, time_left % 60

    timer_color = C_GREEN if time_left > 300 else C_YELLOW if time_left > 60 else C_RED

    print(f"📊 CURRENT WINDOW: {market.slug}")
    print(f"⏱️  TIME REMAINING: {timer_color}{mins:02d}:{secs:02d}{C_RESET}")
    print(f"⚡ STRIKE PRICE: ${market.strike_price:,.2f}" if market.strike_price > 0 else "⚡ STRIKE: Waiting...")
    print()

    # === LIVE DATA ===
    print(f"{'DATA SOURCE':<20} | {'PRICE':<12} | {'AGE (ms)':<10}")
    print("-" * 50)

    now_ms = int(time.time() * 1000)
    spot_age = now_ms - core.spot_ts
    oracle_age = now_ms - core.oracle_ts

    print(f"BINANCE SPOT         | ${core.spot_price:>10,.2f} | {spot_age}")
    print(f"CHAINLINK ORACLE     | ${core.oracle_price:>10,.2f} | {oracle_age}")

    oracle_lag = core.get_oracle_lag()
    lag_color = C_YELLOW if abs(oracle_lag) > Config.MIN_ORACLE_LAG else C_RESET
    print(f"\nORACLE LAG: {lag_color}${oracle_lag:+.2f}{C_RESET}")

    # Price movement
    price_move = core.spot_price - strategy.window_start_price if strategy.window_start_price > 0 else 0
    move_color = C_GREEN if price_move > 0 else C_RED
    print(f"PRICE MOVE: {move_color}${price_move:+.2f}{C_RESET}")
    print()

    # === POLYMARKET PRICES ===
    if market.market_created:
        print(f"💵 SHARE PRICES: {C_GREEN}UP ${market.prices['UP']:.3f}{C_RESET} | {C_RED}DOWN ${market.prices['DOWN']:.3f}{C_RESET}")
        total = market.prices['UP'] + market.prices['DOWN']
        arb_opportunity = abs(1.0 - total) > 0.02
        if arb_opportunity:
            print(f"   {C_YELLOW}⚠️  ARB OPPORTUNITY: Total = ${total:.3f}{C_RESET}")
    print()

    # === CURRENT POSITION ===
    if position_mgr.current_position:
        pos = position_mgr.current_position
        dir_color = C_GREEN if pos.direction == 'UP' else C_RED
        current_price = market.prices.get(pos.direction, 0)
        unrealized_pnl = (current_price - pos.entry_price) * pos.size
        pnl_color = C_GREEN if unrealized_pnl > 0 else C_RED

        print(f"{C_CYAN}═══ ACTIVE POSITION ═══{C_RESET}")
        print(f"Direction: {dir_color}{pos.direction}{C_RESET}")
        print(f"Entry: ${pos.entry_price:.3f} | Current: ${current_price:.3f}")
        print(f"Size: {pos.size} shares")
        print(f"Unrealized P&L: {pnl_color}${unrealized_pnl:+.2f}{C_RESET}")
        print(f"Trailing Stop: ${pos.highest_price:.3f} (-${Config.TRAILING_STOP})")
    else:
        print(f"{C_YELLOW}⏸️  NO ACTIVE POSITION{C_RESET}")
    print()

    # === PERFORMANCE METRICS ===
    metrics = Analytics.calculate_metrics(position_mgr.trade_history, position_mgr.capital)

    print(f"{C_CYAN}═══ PERFORMANCE ═══{C_RESET}")
    print(f"Capital: ${metrics['capital']:,.2f} (ROI: {metrics['roi']:+.1f}%)")
    print(f"Total Trades: {metrics['total_trades']} | Win Rate: {metrics['win_rate']:.1f}%")

    if metrics['total_trades'] > 0:
        print(f"Wins: {C_GREEN}{metrics['wins']}{C_RESET} | Losses: {C_RED}{metrics['losses']}{C_RESET}")
        print(f"Avg P&L: ${metrics['avg_pnl']:+.2f}")
        print(f"Total P&L: {C_GREEN if metrics['total_pnl'] > 0 else C_RED}${metrics['total_pnl']:+.2f}{C_RESET}")

    print(f"\n{C_CYAN}Press Ctrl+C to stop and export results{C_RESET}")

# ==========================================
# 🚀 MAIN BOT LOOP
# ==========================================
def run_bot():
    """Main trading bot loop"""
    print("🚀 Starting Paper Trading Bot...")

    # Initialize components
    market = MarketManager()
    strategy = TradingStrategy(market)
    position_mgr = PositionManager()

    # Start data streams
    BinanceSpotClient(core).start()
    PolyDataClient(core).start()

    print("⏳ Connecting to data streams...")
    time.sleep(3)

    print("✅ Bot is live! Starting paper trading...\n")

    last_window = 0

    try:
        while True:
            # Check for new window
            if market.check_window_transition():
                # Settle any open position from previous window
                if position_mgr.current_position:
                    position_mgr.settle_position(core.spot_price)
                    logger.log_trade(position_mgr.current_position, position_mgr.capital)

                # Reset strategy
                strategy.reset_for_new_window()

                # Try to fetch new market details
                market.fetch_market_details()

            # Update market prices
            market.fetch_live_prices()

            # === TRADING LOGIC ===
            time_in_window = market.get_time_in_window()

            # Check for entry
            if position_mgr.current_position is None and market.market_created:
                should_enter, direction, confidence = strategy.should_enter_trade()

                if should_enter:
                    entry_price = market.prices.get(direction, 0)
                    if entry_price > 0:
                        success = position_mgr.open_position(
                            direction, entry_price, market.strike_price, confidence
                        )
                        # Position opened - will show in dashboard

            # Update existing position
            if position_mgr.current_position and market.market_created:
                current_price = market.prices.get(position_mgr.current_position.direction, 0)
                position_mgr.update_position(current_price, time_in_window)

                # If position was closed, log it
                if position_mgr.current_position is None and len(position_mgr.trade_history) > 0:
                    logger.log_trade(position_mgr.trade_history[-1], position_mgr.capital)

            # Render dashboard
            render_dashboard(market, strategy, position_mgr)

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\n🛑 Bot stopped by user")

        # Final settlement
        if position_mgr.current_position:
            current_price = market.prices.get(position_mgr.current_position.direction, 0)
            position_mgr.close_position(current_price, "MANUAL_EXIT")
            logger.log_trade(position_mgr.trade_history[-1], position_mgr.capital)

        # Save final metrics
        metrics = Analytics.calculate_metrics(position_mgr.trade_history, position_mgr.capital)
        logger.save_metrics(metrics)

        print("\n📊 Final Performance Summary:")
        print(f"   Starting Capital: ${Config.STARTING_CAPITAL:,.2f}")
        print(f"   Final Capital: ${metrics['capital']:,.2f}")
        print(f"   Total P&L: ${metrics['total_pnl']:+.2f}")
        print(f"   ROI: {metrics['roi']:+.1f}%")
        print(f"   Win Rate: {metrics['win_rate']:.1f}%")
        print(f"   Total Trades: {metrics['total_trades']}")
        print(f"\n📁 Trade history saved to: {Config.TRADE_LOG}")
        print(f"📁 Metrics saved to: {Config.METRICS_LOG}")

if __name__ == "__main__":
    run_bot()