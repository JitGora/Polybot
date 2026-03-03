"""
=============================================================================
👁️ ORACLE EYE v8.0 - LIFECYCLE MASTER
=============================================================================
Advanced Polymarket Arbitrage Terminal with:
- Auto-State Persistence (Resume on crash)
- Precision Strike Capture (Exact unix timestamp sync)
- "Wait-for-Next" Logic (Don't log events with unknown strikes)
- Pre-calculation of future slugs
=============================================================================
"""

import time
import requests
import json
import threading
import websocket
import sys
import re
import csv
import os
from datetime import datetime

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
REFRESH_RATE = 0.05
RECORD_INTERVAL = 0.1
WINDOW_DURATION = 900  # 15 Minutes
STATE_FILE = "oracle_state.json"
CSV_FILENAME = "btc_arb_master_log.csv"

# ==========================================
# 🧱 DATA CORE
# ==========================================
class DataCore:
    def __init__(self):
        self.futures_price = 0.0
        self.futures_ts = 0
        self.spot_price = 0.0
        self.spot_ts = 0
        self.poly_relay_price = 0.0
        self.poly_relay_ts = 0
        self.oracle_price = 0.0
        self.oracle_ts = 0
        
    def update_futures(self, p, t): self.futures_price, self.futures_ts = float(p), int(t)
    def update_spot(self, p, t): self.spot_price, self.spot_ts = float(p), int(t)
    def update_relay(self, p, t): self.poly_relay_price, self.poly_relay_ts = float(p), int(t)
    def update_oracle(self, p, t): self.oracle_price, self.oracle_ts = float(p), int(t)

core = DataCore()

# ==========================================
# 💾 STATE PERSISTENCE
# ==========================================
class StateManager:
    @staticmethod
    def save(window_ts, strike, slug):
        data = {
            "window_ts": window_ts,
            "strike": strike,
            "slug": slug,
            "saved_at": int(time.time())
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(data, f)

    @staticmethod
    def load():
        if not os.path.exists(STATE_FILE): return None
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except: return None

# ==========================================
# 📝 INTELLIGENT RECORDER
# ==========================================
class DataRecorder:
    def __init__(self):
        self.last_write = 0
        self.buffer = []
        
        # Initialize CSV header if file doesn't exist
        if not os.path.exists(CSV_FILENAME):
            with open(CSV_FILENAME, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "sys_ts", "event_slug", "true_strike", 
                    "binance_spot", "binance_fut", "poly_relay", "chainlink", 
                    "oracle_gap", "basis", "status"
                ])

    def snap(self, core, market_manager):
        # 1. ONLY record if we are in ACTIVE state (we know the strike)
        if market_manager.state != "ACTIVE": return

        now = time.time()
        if now - self.last_write < RECORD_INTERVAL: return
        self.last_write = now
        
        if core.spot_price == 0: return

        row = [
            int(now * 1000),
            market_manager.slug,
            market_manager.strike,
            core.spot_price,
            core.futures_price,
            core.poly_relay_price,
            core.oracle_price,
            round(core.spot_price - core.oracle_price, 2),
            round(core.futures_price - core.spot_price, 2),
            market_manager.state
        ]
        
        self.buffer.append(row)
        
        if len(self.buffer) >= 10:
            self._flush()

    def _flush(self):
        try:
            with open(CSV_FILENAME, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerows(self.buffer)
            self.buffer = []
        except: pass

recorder = DataRecorder()

# ==========================================
# 🧠 MARKET LIFECYCLE MANAGER (The Brain)
# ==========================================
class MarketManager:
    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://gamma-api.polymarket.com/events/slug/"
        self.clob_url = "https://clob.polymarket.com/price"
        
        self.state = "INITIALIZING"  # STATES: INITIALIZING, WAITING, ACTIVE
        self.current_window_ts = 0
        self.slug = "finding..."
        self.strike = 0.0
        self.ids = {"UP": None, "DOWN": None}
        self.prices = {"UP": 0.0, "DOWN": 0.0}
        
        self._startup_check()

    def _startup_check(self):
        """Decides whether to Resume or Wait based on State File"""
        now = time.time()
        current_window = (int(now) // WINDOW_DURATION) * WINDOW_DURATION
        saved = StateManager.load()

        # Check if we have valid saved state for THIS current window
        if saved and saved['window_ts'] == current_window:
            print("✅ FOUND VALID SAVED STATE. RESUMING...")
            self.current_window_ts = saved['window_ts']
            self.strike = saved['strike']
            self.slug = saved['slug']
            self.state = "ACTIVE"
        else:
            print("⚠️ NO VALID STATE FOR CURRENT WINDOW. SKIPPING TO NEXT.")
            self.state = "WAITING"
            self.current_window_ts = current_window + WINDOW_DURATION # Target next
            self.slug = f"btc-updown-15m-{self.current_window_ts}"

    def lifecycle_loop(self):
        now = time.time()
        
        # === STATE: WAITING ===
        # We are waiting for the next window to start
        if self.state == "WAITING":
            # 1. Pre-fetch details 60s before start
            if now >= (self.current_window_ts - 60) and self.ids["UP"] is None:
                self.slug = f"btc-updown-15m-{self.current_window_ts}"
                self.fetch_market_details(force_lookup=True)
            
            # 2. TRIGGER START
            if now >= self.current_window_ts:
                # 📸 SNAPSHOT MOMENT 📸
                # We use Chainlink price as strike. If Chainlink is lagging/zero, fallback to Spot.
                self.strike = core.oracle_price if core.oracle_price > 0 else core.spot_price
                
                self.state = "ACTIVE"
                self.slug = f"btc-updown-15m-{self.current_window_ts}"
                
                # Save state immediately
                StateManager.save(self.current_window_ts, self.strike, self.slug)
                
                # Reset IDs if we haven't found them yet (unlikely if pre-fetch worked)
                if self.ids["UP"] is None:
                    self.fetch_market_details(force_lookup=True)

        # === STATE: ACTIVE ===
        elif self.state == "ACTIVE":
            # Check if window has ended
            if now >= (self.current_window_ts + WINDOW_DURATION):
                print("🏁 EVENT FINISHED. ROLLING TO NEXT...")
                
                # Roll over variables
                self.current_window_ts += WINDOW_DURATION
                
                # 📸 SNAPSHOT NEW STRIKE FOR NEW EVENT
                self.strike = core.oracle_price if core.oracle_price > 0 else core.spot_price
                
                # Update Slug
                self.slug = f"btc-updown-15m-{self.current_window_ts}"
                
                # Clear old IDs
                self.ids = {"UP": None, "DOWN": None}
                
                # Save new state
                StateManager.save(self.current_window_ts, self.strike, self.slug)
                
                # Attempt immediate fetch
                self.fetch_market_details(force_lookup=True)

    def fetch_market_details(self, force_lookup=False):
        if self.ids["UP"] is not None and not force_lookup: return
        try:
            r = self.session.get(f"{self.base_url}{self.slug}")
            if r.status_code == 200:
                data = r.json()
                market = data['markets'][0]
                t_ids = json.loads(market['clobTokenIds'])
                self.ids["UP"] = t_ids[0]
                self.ids["DOWN"] = t_ids[1]
        except: pass

    def fetch_live_share_prices(self):
        if self.ids["UP"] is None: return
        try:
            # Polymarket CLOB often allows batch fetching, simplified here
            r = self.session.get(f"{self.clob_url}?token_id={self.ids['UP']}&side=buy")
            if r.status_code == 200: self.prices["UP"] = float(r.json()['price'])
            r = self.session.get(f"{self.clob_url}?token_id={self.ids['DOWN']}&side=buy")
            if r.status_code == 200: self.prices["DOWN"] = float(r.json()['price'])
        except: pass

# ==========================================
# 📡 DATA CLIENTS (SAME AS BEFORE)
# ==========================================
class BinanceFuturesClient:
    def __init__(self, data_core):
        self.core, self.url = data_core, "wss://fstream.binance.com/ws/btcusdt@aggTrade"
    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(self.url, on_message=lambda ws, msg: self._on_msg(msg))
                    ws.run_forever()
                except: time.sleep(1)
        threading.Thread(target=run, daemon=True).start()
    def _on_msg(self, msg):
        try: self.core.update_futures(json.loads(msg)['p'], json.loads(msg)['T'])
        except: pass

class BinanceSpotClient:
    def __init__(self, data_core):
        self.core, self.url = data_core, "wss://stream.binance.com:9443/ws/btcusdt@trade"
    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(self.url, on_message=lambda ws, msg: self._on_msg(msg))
                    ws.run_forever()
                except: time.sleep(1)
        threading.Thread(target=run, daemon=True).start()
    def _on_msg(self, msg):
        try: self.core.update_spot(json.loads(msg)['p'], json.loads(msg)['T'])
        except: pass

class PolyDataClient:
    def __init__(self, data_core):
        self.core, self.url = data_core, "wss://ws-live-data.polymarket.com"
    def start(self):
        def on_open(ws):
            ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": "crypto_prices", "type": "*", "filters": {"symbol": "btcusdt"}}, {"topic": "crypto_prices_chainlink", "type": "*", "filters": {"symbol": "btc/usd"}}] }))
        def on_msg(ws, msg):
            try:
                d = json.loads(msg)
                if d.get('type') in ['update', 'price_change']:
                    p, t = d['payload']['value'], d['payload']['timestamp']
                    if d['topic'] == 'crypto_prices': self.core.update_relay(p, t)
                    elif d['topic'] == 'crypto_prices_chainlink': self.core.update_oracle(p, t)
            except: pass
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(self.url, on_open=on_open, on_message=on_msg)
                    ws.run_forever()
                except: time.sleep(2)
        threading.Thread(target=run, daemon=True).start()

# ==========================================
# 🖥️ RENDERER
# ==========================================
def render_terminal(market):
    C_RESET, C_GREEN, C_RED, C_CYAN, C_YELLOW = "\033[0m", "\033[92m", "\033[91m", "\033[96m", "\033[93m"
    sys.stdout.write("\033[2J")

    while True:
        market.lifecycle_loop()
        market.fetch_live_share_prices()
        recorder.snap(core, market)
        
        # Calculate Timers
        now = time.time()
        time_until_start = market.current_window_ts - now
        time_until_end = (market.current_window_ts + WINDOW_DURATION) - now
        
        # Display Logic
        sys.stdout.write("\033[H")
        print(f"{C_CYAN}👁️ ORACLE EYE v8.0 PRO - LIFECYCLE MASTER{C_RESET}")
        print("="*60)
        
        # STATE HEADER
        if market.state == "WAITING":
            mins, secs = int(time_until_start // 60), int(time_until_start % 60)
            print(f"🛑 STATUS: {C_YELLOW}WAITING FOR NEXT EVENT{C_RESET}")
            print(f"⏳ NEXT START: {market.current_window_ts} (in {mins:02d}:{secs:02d})")
            print(f"🎯 NEXT SLUG:  {market.slug}")
        else:
            mins, secs = int(time_until_end // 60), int(time_until_end % 60)
            timer_col = C_RED if time_until_end < 120 else C_GREEN
            print(f"🟢 STATUS: {C_GREEN}ACTIVE LOGGING{C_RESET}")
            print(f"🎯 EVENT: {market.slug}")
            print(f"⚡ TRUE STRIKE: {C_CYAN}${market.strike:,.2f}{C_RESET}")
            print(f"⏳ EXPIRY: {timer_col}{mins:02d}:{secs:02d}{C_RESET}")

        print("-" * 60)
        print(f"{'SOURCE':<15} | {'PRICE':<10} | {'GAP vs SPOT'}")
        print(f"BINANCE SPOT    | {core.spot_price:<10.2f} | REF")
        print(f"CHAINLINK       | {core.oracle_price:<10.2f} | {core.spot_price - core.oracle_price:+.2f}")
        print(f"POLY RELAY      | {core.poly_relay_price:<10.2f} | {core.spot_price - core.poly_relay_price:+.2f}")
        
        if market.ids["UP"]:
             print(f"\nSHARES: YES ${market.prices['UP']:.2f} | NO ${market.prices['DOWN']:.2f}")
        else:
             print(f"\n{C_YELLOW}Looking for Token IDs...{C_RESET}")
             
        time.sleep(REFRESH_RATE)

if __name__ == "__main__":
    print("🚀 Booting...")
    BinanceFuturesClient(core).start()
    BinanceSpotClient(core).start()
    PolyDataClient(core).start()
    time.sleep(2)
    render_terminal(MarketManager())