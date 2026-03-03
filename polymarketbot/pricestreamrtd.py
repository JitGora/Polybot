"""
=============================================================================
🏛️ POLYMARKET GROUND TRUTH TERMINAL (v6.0 - Spot + Futures)
=============================================================================

DATA STREAMS:
1. BINANCE FUTURES (Leading Indicator): wss://fstream.binance.com/ws/btcusdt@aggTrade
2. BINANCE SPOT (Oracle Source):        wss://stream.binance.com:9443/ws/btcusdt@trade
3. POLY RELAY binance spot btcusdt but from polymarket RTDS:                 wss://ws-live-data.polymarket.com
4. CHAINLINK (The Judge) adn the price polymakrt uses for its strike price adn live price it shows on its gui RTDS:               wss://ws-live-data.polymarket.com

"""

import time
import requests
import json
import threading
import websocket
import sys
import re
from datetime import datetime

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
REFRESH_RATE = 0.1  # 10 FPS

# ==========================================
# 🧱 DATA CORE
# ==========================================
class DataCore:
    def __init__(self):
        # 1. FUTURES
        self.futures_price = 0.0
        self.futures_ts = 0  
        
        # 2. SPOT (New!)
        self.spot_price = 0.0
        self.spot_ts = 0

        # 3. POLY RELAY
        self.poly_relay_price = 0.0
        self.poly_relay_ts = 0
        
        # 4. ORACLE
        self.oracle_price = 0.0
        self.oracle_ts = 0
        
    def update_futures(self, price, ts):
        self.futures_price = float(price)
        self.futures_ts = int(ts)

    def update_spot(self, price, ts):
        self.spot_price = float(price)
        self.spot_ts = int(ts)

    def update_relay(self, price, ts):
        self.poly_relay_price = float(price)
        self.poly_relay_ts = int(ts)

    def update_oracle(self, price, ts):
        self.oracle_price = float(price)
        self.oracle_ts = int(ts)

core = DataCore()

# ==========================================
# 📡 MODULE 1: BINANCE CLIENTS
# ==========================================
class BinanceFuturesClient:
    """Connects to Binance USD-M Futures (Fastest)"""
    def __init__(self, data_core):
        self.core = data_core
        self.url = "wss://fstream.binance.com/ws/btcusdt@aggTrade"

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(self.url, on_message=lambda ws, msg: self._on_msg(msg))
                    ws.run_forever()
                except: time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _on_msg(self, msg):
        try:
            d = json.loads(msg)
            self.core.update_futures(d['p'], d['T'])
        except: pass

class BinanceSpotClient:
    """Connects to Binance Spot (The Asset Chainlink tracks)"""
    def __init__(self, data_core):
        self.core = data_core
        self.url = "wss://stream.binance.com:9443/ws/btcusdt@trade"

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(self.url, on_message=lambda ws, msg: self._on_msg(msg))
                    ws.run_forever()
                except: time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _on_msg(self, msg):
        try:
            d = json.loads(msg)
            self.core.update_spot(d['p'], d['T'])
        except: pass

# ==========================================
# 📡 MODULE 2: POLYMARKET CLIENT
# ==========================================
class PolyDataClient:
    def __init__(self, data_core):
        self.core = data_core
        self.url = "wss://ws-live-data.polymarket.com"

    def start(self):
        def on_open(ws):
            ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": "crypto_prices", "type": "*", "filters": "{\"symbol\":\"btcusdt\"}"}]}))
            ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": "{\"symbol\":\"btc/usd\"}"}]}))

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
            except: pass

        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(self.url, on_open=on_open, on_message=on_msg)
                    ws.run_forever()
                except: time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

# ==========================================
# 🎮 MODULE 3: MARKET MANAGER
# ==========================================
class MarketManager:
    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://gamma-api.polymarket.com/events/slug/"
        self.clob_url = "https://clob.polymarket.com/price"
        self.window_start = 0
        self.slug = "INITIALIZING..."
        self.ids = {"UP": None, "DOWN": None}
        self.prices = {"UP": 0.0, "DOWN": 0.0}
        self.strike = 0.0
        
    def lifecycle_check(self):
        now = int(time.time())
        current_window = (now // 900) * 900
        if current_window != self.window_start:
            self.window_start = current_window
            self.slug = f"btc-updown-15m-{current_window}"
            self.ids = {"UP": None, "DOWN": None}
            self.strike = 0.0
            # Snapshot Oracle if available
            if core.oracle_price > 0: self.strike = core.oracle_price

    def fetch_market_details(self):
        if self.ids["UP"] is not None: return
        try:
            r = self.session.get(f"{self.base_url}{self.slug}")
            if r.status_code == 200:
                data = r.json()
                market = data['markets'][0]
                t_ids = json.loads(market['clobTokenIds'])
                self.ids["UP"] = t_ids[0]
                self.ids["DOWN"] = t_ids[1]
                match = re.search(r"\$([\d,]+\.?\d*)", market['question'])
                if match: self.strike = float(match.group(1).replace(",", ""))
        except: pass

    def fetch_live_share_prices(self):
        if self.ids["UP"] is None: return
        try:
            r = self.session.get(f"{self.clob_url}?token_id={self.ids['UP']}&side=buy")
            if r.status_code == 200: self.prices["UP"] = float(r.json()['price'])
            r = self.session.get(f"{self.clob_url}?token_id={self.ids['DOWN']}&side=buy")
            if r.status_code == 200: self.prices["DOWN"] = float(r.json()['price'])
        except: pass

# ==========================================
# 🖥️ MODULE 4: TERMINAL UI
# ==========================================
def render_terminal(market):
    C_RESET = "\033[0m"
    C_GREEN = "\033[92m"
    C_RED = "\033[91m"
    C_CYAN = "\033[96m"
    C_YELLOW = "\033[93m"
    C_GREY = "\033[90m"

    while True:
        market.lifecycle_check()
        market.fetch_market_details()
        market.fetch_live_share_prices()
        
        now = int(time.time() * 1000)
        
        # Latencies
        ping_fut = now - core.futures_ts
        ping_spot = now - core.spot_ts
        ping_relay = now - core.poly_relay_ts
        ping_oracle = now - core.oracle_ts
        
        # Diffs
        basis = core.futures_price - core.spot_price # Futures Premium
        oracle_gap = core.oracle_price - core.spot_price # Oracle vs Real Spot
        
        # Timer
        time_left = (market.window_start + 900) - time.time()
        if time_left < 0: time_left = 0
        mins, secs = int(time_left // 60), int(time_left % 60)
        timer_str = f"{mins:02d}:{secs:02d}"
        timer_col = C_GREEN if time_left > 60 else C_RED

        # DRAW
        sys.stdout.write(f"\033[2J\033[H")
        print(f"{C_CYAN}📡 UNIVERSAL MARKET TERMINAL v6.0 (SPOT+FUTURES){C_RESET}")
        print("="*60)
        print(f"{'SOURCE':<18} | {'TICKER':<10} | {'PRICE':<12} | {'AGE':<8}")
        print("-" * 60)
        
        # 1. FUTURES
        print(f"1️⃣ {C_GREEN}BINANCE FUTURES{C_RESET} | {'BTC/USDT':<10} | ${core.futures_price:<11,.2f} | {ping_fut}ms")
        
        # 2. SPOT (New)
        print(f"2️⃣ {C_CYAN}BINANCE SPOT{C_RESET}    | {'BTC/USDT':<10} | ${core.spot_price:<11,.2f} | {ping_spot}ms")
        
        # 3. RELAY
        print(f"3️⃣ POLY RELAY      | {'BTCUSDT':<10} | ${core.poly_relay_price:<11,.2f} | {ping_relay}ms")
        
        # 4. ORACLE
        cl_col = C_GREEN if ping_oracle < 5000 else C_RED
        print(f"4️⃣ CHAINLINK       | {'BTC/USD':<10} | {cl_col}${core.oracle_price:<11,.2f}{C_RESET} | {ping_oracle}ms")
        
        print("-" * 60)
        
        # ANALYTICS
        print(f"📊 BASIS (Fut-Spot):  {C_YELLOW}${basis:+.2f}{C_RESET}")
        print(f"⚖️  ORACLE GAP:       ${oracle_gap:+.2f} (vs Spot)")
        
        if ping_oracle > 5000 and abs(oracle_gap) > 50:
            print(f"\n🚨 {C_YELLOW}ARBITRAGE: Oracle lagging Spot by ${abs(oracle_gap):.2f}!{C_RESET}")

        print("="*60)
        print(f"🎯 EVENT: {market.slug}")
        print(f"⏳ TIMER: {timer_col}{timer_str}{C_RESET}")
        
        if market.strike > 0:
            print(f"⚡ STRIKE: {C_CYAN}${market.strike:,.2f}{C_RESET}")
            # Status based on SPOT (Chainlink tracks Spot closer than Futures)
            diff = core.spot_price - market.strike
            status = f"{C_GREEN}UP WINNING (+{diff:.2f}){C_RESET}" if diff > 0 else f"{C_RED}DOWN WINNING ({diff:.2f}){C_RESET}"
            print(f"📊 STATUS (vs Spot): {status}")
        
        if market.ids["UP"]:
            print(f"\n💵 SHARES: {C_GREEN}UP ${market.prices['UP']:.2f}{C_RESET} | {C_RED}DOWN ${market.prices['DOWN']:.2f}{C_RESET}")
        else:
            print(f"\n{C_YELLOW}🔍 Finding Market Tokens...{C_RESET}")

        time.sleep(REFRESH_RATE)

if __name__ == "__main__":
    print("⏳ Starting Streams...")
    BinanceFuturesClient(core).start()
    BinanceSpotClient(core).start()
    PolyDataClient(core).start()
    
    time.sleep(1)
    render_terminal(MarketManager())