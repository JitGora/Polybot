import websocket
import json
import time
import sys
import requests
import threading
import math
import numpy as np
from scipy.stats import norm
from collections import deque
from datetime import datetime

# ==========================================
# 🧠 PERSISTENT MEMORY (The Data Lake)
# ==========================================
class MarketMemory:
    def __init__(self):
        # This data survives across market switches
        self.btc_price = 0.0
        self.tick_history = deque(maxlen=2000) # Stores last ~30 mins of ticks
        self.last_update = 0
    
    def add_tick(self, price, timestamp):
        self.btc_price = price
        self.tick_history.append((timestamp, price))
        self.last_update = time.time()

    def get_velocity(self, seconds_back=10):
        """Calculates $ moved per second over the last X seconds"""
        if len(self.tick_history) < 10: return 0.0
        
        # Filter for data within the time window
        limit_time = (time.time() * 1000) - (seconds_back * 1000)
        recent_ticks = [x for x in self.tick_history if x[0] >= limit_time]
        
        if len(recent_ticks) < 2: return 0.0
        
        start_p = recent_ticks[0][1]
        end_p = recent_ticks[-1][1]
        duration = (recent_ticks[-1][0] - recent_ticks[0][0]) / 1000
        
        if duration == 0: return 0.0
        return (end_p - start_p) / duration

    def get_volatility(self):
        """Standard Deviation of returns"""
        if len(self.tick_history) < 50: return 0.0001
        prices = np.array([x[1] for x in self.tick_history])
        returns = np.diff(prices) / prices[:-1]
        return np.std(returns)

memory = MarketMemory()

# ==========================================
# 📡 BINANCE STREAM (Background Daemon)
# ==========================================
class BinanceThread(threading.Thread):
    def run(self):
        # We use a while loop to auto-reconnect if internet drops
        while True:
            try:
                ws = websocket.WebSocketApp(
                    "wss://stream.binance.com:9443/ws/btcusdt@trade",
                    on_message=self.on_message,
                    on_error=self.on_error
                )
                ws.run_forever()
            except:
                time.sleep(2)

    def on_message(self, ws, message):
        data = json.loads(message)
        memory.add_tick(float(data['p']), int(data['T']))

    def on_error(self, ws, error): pass

# ==========================================
# 🎮 POLYMARKET CONTROLLER (Hot Swappable)
# ==========================================
class PolyController:
    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://gamma-api.polymarket.com/events/slug/"
        self.clob_url = "https://clob.polymarket.com/price"
        
        # Current Market State
        self.current_slug = ""
        self.expiry = 0
        self.strike = 0.0
        self.id_yes = None
        self.id_no = None
        self.active = False
        
        # Prices
        self.price_yes = 0.0
        self.price_no = 0.0

    def get_active_slug(self):
        """Calculates what the current 15m slug SHOULD be"""
        now = int(time.time())
        window_start = (now // 900) * 900
        return f"btc-updown-15m-{window_start}", window_start + 900

    def refresh_market(self):
        """Checks if we need to switch to a new market"""
        target_slug, target_expiry = self.get_active_slug()
        
        # If we are already on the correct market, do nothing
        if target_slug == self.current_slug and self.active:
            return

        print(f"\n🔄 SWITCHING MARKETS...")
        print(f"   Old: {self.current_slug}")
        print(f"   New: {target_slug}")
        
        try:
            r = self.session.get(f"{self.base_url}{target_slug}")
            if r.status_code != 200:
                print("⚠️  New market not created yet. Retrying...")
                self.active = False
                return

            data = r.json()
            market = data['markets'][0]
            t_ids = json.loads(market['clobTokenIds'])
            
            # UPDATE STATE
            self.current_slug = target_slug
            self.expiry = target_expiry
            self.id_yes = t_ids[0]
            self.id_no = t_ids[1]
            
            # AUTO-DETECT STRIKE (Parsing the title)
            # Format usually: "Bitcoin > $90,123.45 on ..."
            title = market['question']
            import re
            match = re.search(r"\$([\d,]+\.?\d*)", title)
            if match:
                clean_num = match.group(1).replace(",", "")
                self.strike = float(clean_num)
                print(f"✅ LOCKED STRIKE: ${self.strike}")
                self.active = True
            else:
                print("❌ Could not parse strike price! Enter Manually:")
                self.strike = float(input(">>> "))
                self.active = True
                
        except Exception as e:
            print(f"❌ Error switching: {e}")
            self.active = False

    def fetch_prices(self):
        if not self.active: return
        try:
            # Fetch YES
            r = self.session.get(f"{self.clob_url}?token_id={self.id_yes}&side=buy")
            if r.status_code == 200: self.price_yes = float(r.json()['price'])
            
            # Fetch NO
            r = self.session.get(f"{self.clob_url}?token_id={self.id_no}&side=buy")
            if r.status_code == 200: self.price_no = float(r.json()['price'])
        except: pass

# ==========================================
# 🖥️ DASHBOARD & MATH
# ==========================================
def run_dashboard(poly):
    # Colors
    C_RESET = "\033[0m"
    C_GREEN = "\033[92m"
    C_RED = "\033[91m"
    C_CYAN = "\033[96m"
    
    # 1. Gather Data
    btc = memory.btc_price
    vol = memory.get_volatility()
    vel = memory.get_velocity(seconds_back=10) # 10s instant momentum
    
    # 2. Probability Math (With Velocity Drift)
    time_left = poly.expiry - time.time()
    if time_left < 0: time_left = 0
    
    # Drift: Project price 30s into future based on velocity
    projected_price = btc + (vel * 30)
    
    # Z-Score
    if vol == 0: vol = 0.0001
    d2 = (math.log(projected_price / poly.strike)) / (vol * math.sqrt(time_left) if time_left > 0 else 1)
    
    fair_yes = norm.cdf(d2)
    fair_no = 1.0 - fair_yes
    
    # 3. Edges
    edge_yes = fair_yes - poly.price_yes
    edge_no = fair_no - poly.price_no
    
    # 4. Render HUD
    # Header
    os_cmd = "\r"
    status_line = f"{C_CYAN}⏱️ {int(time_left)}s | 💰 ${btc:,.2f} | 🎯 ${poly.strike:,.2f} | 🌊 Vel: ${vel:+.2f}/s{C_RESET}"
    
    # Yes Line
    col_y = C_GREEN if edge_yes > 0.10 else C_RESET
    line_yes = f"YES | Fair: {fair_yes:.0%} vs Mkt: {poly.price_yes:.0%} | EDGE: {col_y}{edge_yes*100:+.0f}%{C_RESET}"
    
    # No Line
    col_n = C_GREEN if edge_no > 0.10 else C_RESET
    line_no = f"NO  | Fair: {fair_no:.0%} vs Mkt: {poly.price_no:.0%} | EDGE: {col_n}{edge_no*100:+.0f}%{C_RESET}"
    
    # Print Overwrite (5 lines)
    print(f"\033[5A\r{'-'*60}\n{status_line}\n{'-'*60}\n{line_yes}\033[K\n{line_no}\033[K")

# ==========================================
# 🚀 MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    # 1. Start Persistent Data Engine
    print("📡 Connecting to Binance Stream...")
    bt = BinanceThread()
    bt.daemon = True
    bt.start()
    
    # 2. Wait for Data buffer
    while len(memory.tick_history) < 10:
        sys.stdout.write(f"\r⏳ Buffering Data... {len(memory.tick_history)} ticks")
        sys.stdout.flush()
        time.sleep(0.5)
        
    print("\n✅ Data Engine Ready.")
    
    # 3. Start Poly Controller
    poly = PolyController()
    
    # 4. Make space for Dashboard
    print("\n" * 6) 
    
    try:
        while True:
            # Check for new market window every loop
            poly.refresh_market()
            
            # Fetch latest prices
            poly.fetch_prices()
            
            # Update Screen
            if poly.active:
                run_dashboard(poly)
            else:
                print(f"\r⚠️ Waiting for Market Data... ({datetime.now().strftime('%H:%M:%S')})", end="")
            
            time.sleep(1) # Fast refresh
            
    except KeyboardInterrupt:
        print("\n🛑 Bot Stopped.")