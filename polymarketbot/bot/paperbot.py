import time
import requests
import json
import threading
import websocket
import sys
import re
from datetime import datetime
from collections import deque

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
REFRESH_RATE = 0.1  # 10 FPS Dashboard

# ==========================================
# 🧱 DATA ENGINE (The Truth Store)
# ==========================================
class DataCore:
    def __init__(self):
        # 1. BINANCE FUTURES (The "Future")
        self.binance_price = 0.0
        self.binance_ts = 0  
        
        # 2. POLYMARKET RELAY (The "Present")
        self.poly_binance_price = 0.0
        self.poly_binance_ts = 0
        
        # 3. CHAINLINK ORACLE (The "Judge")
        self.poly_chainlink_price = 0.0
        self.poly_chainlink_ts = 0
        
    def update_binance(self, price, ts):
        self.binance_price = float(price)
        self.binance_ts = int(ts)

    def update_poly_binance(self, price, ts):
        self.poly_binance_price = float(price)
        self.poly_binance_ts = int(ts)

    def update_poly_chainlink(self, price, ts):
        self.poly_chainlink_price = float(price)
        self.poly_chainlink_ts = int(ts)

core = DataCore()

# ==========================================
# 📡 STREAM MANAGERS
# ==========================================
def start_streams():
    # --- STREAM 1: BINANCE FUTURES (BTC/USDT) ---
    def run_binance():
        # We use Futures (@aggTrade) because it is faster than Spot
        url = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
        
        def on_msg(ws, msg):
            try:
                d = json.loads(msg)
                core.update_binance(d['p'], d['T'])
            except: pass
            
        while True:
            try:
                ws = websocket.WebSocketApp(url, on_message=on_msg)
                ws.run_forever()
            except: 
                time.sleep(1)

    # --- STREAM 2 & 3: POLYMARKET RTDS ---
    def run_poly():
        url = "wss://ws-live-data.polymarket.com"
        
        def on_open(ws):
            print("✅ Connected to Poly RTDS")
            
            # SUB A: Binance Relay (Ticker: btcusdt)
            ws.send(json.dumps({
                "action": "subscribe", 
                "subscriptions": [{"topic": "crypto_prices", "type": "*", "filters": "{\"symbol\":\"btcusdt\"}"}]
            }))
            
            # SUB B: Chainlink Oracle (Ticker: btc/usd)
            ws.send(json.dumps({
                "action": "subscribe", 
                "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": "{\"symbol\":\"btc/usd\"}"}]
            }))

        def on_msg(ws, msg):
            try:
                d = json.loads(msg)
                if d.get('type') == 'update':
                    topic = d.get('topic')
                    payload = d.get('payload', {})
                    symbol = payload.get('symbol')
                    
                    # ROUTING LOGIC based on Ticker
                    if topic == 'crypto_prices' and symbol == 'btcusdt':
                        core.update_poly_binance(payload['value'], payload['timestamp'])
                        
                    elif topic == 'crypto_prices_chainlink' and symbol == 'btc/usd':
                        core.update_poly_chainlink(payload['value'], payload['timestamp'])
            except: pass

        while True:
            try:
                ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_msg)
                ws.run_forever()
            except: 
                time.sleep(1)

    threading.Thread(target=run_binance, daemon=True).start()
    threading.Thread(target=run_poly, daemon=True).start()

# ==========================================
# 🎮 MARKET MANAGER (Auto-Rotation)
# ==========================================
class MarketManager:
    def __init__(self):
        self.session = requests.Session()
        self.clob_url = "https://clob.polymarket.com/price"
        self.base_url = "https://gamma-api.polymarket.com/events/slug/"
        
        self.window_start = 0
        self.slug = ""
        # Updated Keys for UP/DOWN
        self.ids = {"UP": None, "DOWN": None}
        self.prices = {"UP": 0.0, "DOWN": 0.0}
        self.strike = 0.0
        self.active = False
        
    def check_rotation(self):
        now = int(time.time())
        current_window = (now // 900) * 900
        
        if current_window != self.window_start:
            self.active = False
            self.window_start = current_window
            self.slug = f"btc-updown-15m-{current_window}"
            
            # PAUSE DASHBOARD FOR SETUP
            print(f"\n" + "="*60)
            print(f"🔄 NEW WINDOW: {datetime.fromtimestamp(current_window).strftime('%H:%M:%S')}")
            
            # 1. Fetch Market Details
            detected_strike = 0.0
            
            while True:
                try:
                    r = self.session.get(f"{self.base_url}{self.slug}")
                    if r.status_code == 200:
                        data = r.json()
                        market = data['markets'][0]
                        t_ids = json.loads(market['clobTokenIds'])
                        
                        # MAPPING: Index 0 is YES (UP), Index 1 is NO (DOWN)
                        self.ids["UP"] = t_ids[0]
                        self.ids["DOWN"] = t_ids[1]
                        
                        # Auto-Detect from Title
                        match = re.search(r"\$([\d,]+\.?\d*)", market['question'])
                        detected_strike = float(match.group(1).replace(",", "")) if match else 0.0
                        break
                    time.sleep(1)
                except: time.sleep(1)

            # 2. Confirm Strike (Safety First)
            print(f"⚖️  Oracle Price: ${core.poly_chainlink_price:,.2f}")
            print(f"🤖 Auto-Detect:  ${detected_strike:,.2f}")
            
            user_input = input(f"👇 Press ENTER to use ${detected_strike:,.2f}: ")
            
            if user_input.strip() == "":
                self.strike = detected_strike
            else:
                try: self.strike = float(user_input.replace(",", ""))
                except: self.strike = detected_strike
            
            self.active = True
            time.sleep(0.5)

    def fetch_live_prices(self):
        if not self.active: return
        try:
            # Fetch UP (Yes)
            r = self.session.get(f"{self.clob_url}?token_id={self.ids['UP']}&side=buy")
            if r.status_code == 200: self.prices["UP"] = float(r.json()['price'])
            
            # Fetch DOWN (No)
            r = self.session.get(f"{self.clob_url}?token_id={self.ids['DOWN']}&side=buy")
            if r.status_code == 200: self.prices["DOWN"] = float(r.json()['price'])
        except: pass

# ==========================================
# 🖥️ TERMINAL DISPLAY
# ==========================================
def render_terminal(market):
    C_RESET = "\033[0m"
    C_GREEN = "\033[92m"
    C_RED = "\033[91m"
    C_CYAN = "\033[96m"
    C_YELLOW = "\033[93m"
    C_GREY = "\033[90m"

    while True:
        market.check_rotation()
        market.fetch_live_prices()
        
        now = int(time.time() * 1000)
        
        # Calculate Lags
        age_bin = now - core.binance_ts
        age_poly_bin = now - core.poly_binance_ts
        age_chainlink = now - core.poly_chainlink_ts
        
        # Arbitrage Gap (Future vs Judge)
        gap = core.poly_chainlink_price - core.binance_price
        
        # Time Left
        time_left = (market.window_start + 900) - time.time()
        if time_left < 0: time_left = 0
        
        # Clear Screen
        sys.stdout.write(f"\033[2J\033[H")
        
        # HEADER
        print(f"{C_CYAN}📡 UNIVERSAL MARKET TERMINAL v2.1 (UP/DOWN){C_RESET}")
        print("="*60)
        print(f"{'SOURCE':<18} | {'TICKER':<10} | {'PRICE':<12} | {'AGE':<8}")
        print("-" * 60)
        
        # ROW 1: BINANCE FUTURES
        print(f"1️⃣ {C_GREEN}BINANCE FUTURES{C_RESET} | {'BTC/USDT':<10} | ${core.binance_price:<11,.2f} | {age_bin}ms")
        
        # ROW 2: POLY RTDS (BINANCE RELAY)
        lag_ui = age_poly_bin - age_bin
        print(f"2️⃣ POLY RELAY      | {'BTCUSDT':<10} | ${core.poly_binance_price:<11,.2f} | {age_poly_bin}ms {C_GREY}(+{lag_ui}ms){C_RESET}")
        
        # ROW 3: CHAINLINK (THE JUDGE)
        cl_color = C_GREEN if age_chainlink < 5000 else C_RED
        print(f"3️⃣ CHAINLINK       | {'BTC/USD':<10} | {cl_color}${core.poly_chainlink_price:<11,.2f}{C_RESET} | {age_chainlink}ms")
        
        print("-" * 60)
        
        # ARBITRAGE ALERT
        if age_chainlink > 5000 and abs(gap) > 50:
            print(f"🚨 {C_YELLOW}ARBITRAGE OPPORTUNITY DETECTED{C_RESET}")
            print(f"   Judge is STALE and ${abs(gap):.2f} away from Reality.")
        else:
            print(f"⚖️  Oracle Gap: ${gap:+.2f}")
            
        print("="*60)
        
        # MARKET SECTION
        if market.active:
            print(f"🎯 STRIKE: {C_CYAN}${market.strike:,.2f}{C_RESET}  |  ⏱️ {int(time_left//60)}:{int(time_left%60):02d}")
            
            # Winning/Losing Status
            diff = core.binance_price - market.strike
            if diff > 0:
                status = f"{C_GREEN}UP WINNING (+{diff:.2f}){C_RESET}"
            else:
                status = f"{C_RED}DOWN WINNING ({diff:.2f}){C_RESET}"
            
            print(f"📊 STATUS: {status}")
            
            # Live Prices
            print(f"\n💵 LIVE SHARES:")
            print(f"   {C_GREEN}UP:   ${market.prices['UP']:.2f}{C_RESET}")
            print(f"   {C_RED}DOWN: ${market.prices['DOWN']:.2f}{C_RESET}")
        else:
            print(f"{C_YELLOW}⚠️  WAITING FOR MARKET...{C_RESET}")
            
        time.sleep(REFRESH_RATE)

if __name__ == "__main__":
    print("⏳ Connecting to Data Streams...")
    start_streams()
    time.sleep(2)
    manager = MarketManager()
    
    try:
        render_terminal(manager)
    except KeyboardInterrupt:
        print("\n👋 Terminal Closed.")