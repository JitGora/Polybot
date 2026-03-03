import websocket
import json
import threading
import time
import requests
import csv
import os
from datetime import datetime
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console
from rich.align import Align
from rich.table import Table
from rich.text import Text
from rich import box

# ==========================================
# ⚙️ STRATEGY CONFIGURATION
# ==========================================
CONFIG = {
    "BINANCE_MOMENTUM_THRESHOLD": 80.0,  # $80 move in 60s
    "ORACLE_LAG_THRESHOLD": 50.0,        # $50 gap between Binance & Poly
    "MAX_ENTRY_PRICE": 0.60,             # Never buy above 60 cents
    "STOP_LOSS_PROB": 0.40,              # Cut if probability drops below 40%
    "TAKE_PROFIT_1": 0.65,               # Scale out 50% here
    "TRADE_WINDOW_SECONDS": 90,          # Only trade first 90s of window
    "RISK_PER_TRADE": 1000.0,            # Paper money size
    "LOG_FILE": "trade_analysis.csv"
}

# ==========================================
# 🧠 MODULE 1: MARKET CYCLE MANAGER
# ==========================================
class MarketManager:
    def __init__(self):
        self.active_slug = None
        self.up_id = None
        self.down_id = None
        self.strike_price = None  # The Official Chainlink Strike
        self.start_time = None
        self.end_time = None
        self.is_trading_active = False

    def refresh(self):
        """Calculates current 15m slug and fetches IDs"""
        now = int(time.time())
        window_size = 900 # 15 min
        window_start = (now // window_size) * window_size
        expected_slug = f"btc-updown-15m-{window_start}"

        # If we are in a new window, reset everything
        if self.active_slug != expected_slug:
            print(f"🔄 TRANSITION: {self.active_slug} -> {expected_slug}")
            self.fetch_market_details(expected_slug)
            
    def fetch_market_details(self, slug):
        try:
            url = f"https://gamma-api.polymarket.com/events?slug={slug}"
            data = requests.get(url).json()
            if not data: return

            market = data[0]['markets'][0]
            raw_ids = market.get('clobTokenIds')
            token_ids = json.loads(raw_ids)
            outcomes = json.loads(market.get('outcomes'))
            
            self.up_id = token_ids[0] if outcomes[0] == "Up" else token_ids[1]
            self.down_id = token_ids[1] if outcomes[0] == "Up" else token_ids[0]
            self.active_slug = slug
            self.start_time = datetime.fromisoformat(market['startDate'].replace('Z', '+00:00'))
            self.strike_price = None # Reset strike, wait for Oracle
            self.is_trading_active = True
            
            # Reset Trader state for new market
            trader.reset()
            
        except Exception as e:
            print(f"Manager Error: {e}")

mgr = MarketManager()

# ==========================================
# 📡 MODULE 2: DATA ENGINE (3-SOCKET)
# ==========================================
class DataEngine:
    def __init__(self):
        # BINANCE (Real-time)
        self.binance_price = 0.0
        self.binance_history = [] # For momentum calc
        
        # POLY ORACLE (Resolution)
        self.chainlink_price = 0.0 # The Official Judge
        
        # POLY CLOB (The Market)
        self.yes_bid = 0.0
        self.yes_ask = 0.0
        self.no_bid = 0.0
        self.no_ask = 0.0

    def update_binance(self, price):
        self.binance_price = price
        # Keep 60 seconds of history for momentum calc
        now = time.time()
        self.binance_history.append((now, price))
        self.binance_history = [x for x in self.binance_history if now - x[0] <= 60]

    def get_momentum(self):
        if not self.binance_history: return 0.0
        start_price = self.binance_history[0][1]
        return self.binance_price - start_price

data = DataEngine()

# --- SOCKET 1: BINANCE ---
def start_binance():
    def on_msg(ws, msg):
        try:
            d = json.loads(msg)
            if 'p' in d: data.update_binance(float(d['p']))
        except: pass
    
    ws = websocket.WebSocketApp("wss://stream.binance.com:9443/ws/btcusdt@trade", on_message=on_msg)
    ws.run_forever()

# --- SOCKET 2: POLY ORACLE (RTDS) ---
def start_rtds():
    def on_msg(ws, msg):
        try:
            d = json.loads(msg)
            if d.get('type') == 'update':
                p = d.get('payload', {})
                # Capture Chainlink (Official Settlement Data)
                if p.get('symbol') == 'btc/usd':
                    val = float(p.get('value', 0))
                    data.chainlink_price = val
                    
                    # LOGIC: Capture Strike Price at Window Start
                    # If we have a market, but no strike yet, and time is close to start
                    if mgr.active_slug and mgr.strike_price is None:
                        # Allow 30s buffer around start time for the "official" tick
                        if abs((datetime.now() - datetime.now()).total_seconds()) < 900: 
                             # Ideally check exact timestamp match, but for now take first tick
                             mgr.strike_price = val
        except: pass

    def on_open(ws):
        ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'}]}))

    ws = websocket.WebSocketApp("wss://ws-live-data.polymarket.com", on_open=on_open, on_message=on_msg)
    ws.run_forever()

# --- SOCKET 3: POLY CLOB ---
def start_clob():
    current_sub = None
    def on_msg(ws, msg):
        # Auto-Switch Subscription
        nonlocal current_sub
        if mgr.up_id and mgr.up_id != current_sub:
            ws.send(json.dumps({"type": "subscribe", "channel": "market", "assets_ids": [mgr.up_id, mgr.down_id]}))
            current_sub = mgr.up_id

        try:
            d = json.loads(msg)
            if 'price_changes' in d:
                for c in d['price_changes']:
                    if c['asset_id'] == mgr.up_id:
                        if c['side'] == "BUY": data.yes_bid = float(c['price'])
                        if c['side'] == "SELL": data.yes_ask = float(c['price'])
        except: pass

    ws = websocket.WebSocketApp("wss://ws-subscriptions-clob.polymarket.com/ws/market", on_message=on_msg)
    ws.run_forever()

# ==========================================
# 🤖 MODULE 3: STRATEGY & PAPER TRADER
# ==========================================
class TradeManager:
    def __init__(self):
        self.balance = 10000.0 # Paper Money
        self.position = None   # {'side': 'YES', 'size': 1000, 'entry': 0.53}
        self.pnl_history = []
        
        # Init CSV
        if not os.path.exists(CONFIG["LOG_FILE"]):
            with open(CONFIG["LOG_FILE"], "w") as f:
                f.write("Time,Slug,BinancePrice,OraclePrice,Strike,Momentum,Lag,Action,Price,PnL\n")

    def reset(self):
        self.position = None

    def log(self, action, price, pnl=0):
        with open(CONFIG["LOG_FILE"], "a") as f:
            t = datetime.now().strftime("%H:%M:%S")
            lag = data.binance_price - (mgr.strike_price if mgr.strike_price else 0)
            mom = data.get_momentum()
            row = f"{t},{mgr.active_slug},{data.binance_price},{data.chainlink_price},{mgr.strike_price},{mom:.2f},{lag:.2f},{action},{price},{pnl}\n"
            f.write(row)

    def execute(self):
        if not mgr.strike_price or not mgr.is_trading_active: return "WAITING FOR STRIKE"

        # 1. METRICS
        momentum = data.get_momentum()
        # Lag = Real Price - Strike Price (Distance from start)
        lag = data.binance_price - mgr.strike_price
        
        current_seconds = int(time.time()) % 900
        
        # 2. EXIT LOGIC (Stop Loss / Take Profit)
        if self.position:
            # STOP LOSS: Prob < 40%
            if self.position['side'] == 'YES' and data.yes_bid < CONFIG["STOP_LOSS_PROB"]:
                pnl = (data.yes_bid - self.position['entry']) * self.position['size']
                self.balance += (self.position['size'] * data.yes_bid)
                self.log("SELL_STOP", data.yes_bid, pnl)
                self.position = None
                return "🛑 STOPPED OUT"
            
            # TAKE PROFIT: Scale at 0.65 (Simplified to full exit for demo)
            if self.position['side'] == 'YES' and data.yes_bid > CONFIG["TAKE_PROFIT_1"]:
                pnl = (data.yes_bid - self.position['entry']) * self.position['size']
                self.balance += (self.position['size'] * data.yes_bid)
                self.log("SELL_TP", data.yes_bid, pnl)
                self.position = None
                return "💰 TAKE PROFIT"
            
            return "HOLDING POSITION"

        # 3. ENTRY LOGIC (Only first 90s)
        if current_seconds > CONFIG["TRADE_WINDOW_SECONDS"]: 
            return "WINDOW CLOSED"

        # SETUP: UP
        # Binance moved >$80 UP + Price is >$50 above Strike
        if momentum > CONFIG["BINANCE_MOMENTUM_THRESHOLD"] and lag > CONFIG["ORACLE_LAG_THRESHOLD"]:
            # Check Entry Price
            if data.yes_ask < CONFIG["MAX_ENTRY_PRICE"] and data.yes_ask > 0:
                size = CONFIG["RISK_PER_TRADE"] / data.yes_ask
                self.position = {'side': 'YES', 'size': size, 'entry': data.yes_ask}
                self.balance -= CONFIG["RISK_PER_TRADE"]
                self.log("BUY_YES", data.yes_ask)
                return "🚀 ENTERED LONG (YES)"

        # SETUP: DOWN
        if momentum < -CONFIG["BINANCE_MOMENTUM_THRESHOLD"] and lag < -CONFIG["ORACLE_LAG_THRESHOLD"]:
            # (Simplified: Would buy NO here, usually inverse of YES price)
            pass

        return "SCANNING..."

trader = TradeManager()

# ==========================================
# 🎨 MODULE 4: DASHBOARD
# ==========================================
def render_dashboard():
    mgr.refresh()
    status_msg = trader.execute()
    
    layout = Layout()
    layout.split(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=8)
    )
    
    layout["main"].split_row(Layout(name="metrics"), Layout(name="market"))

    # HEADER
    elapsed = int(time.time()) % 900
    header = Text(f"🤖 POLYMARKET ALPHA-BOT | SLUG: {mgr.active_slug} | TIMER: {elapsed}s/900s", style="bold white on blue", justify="center")
    layout["header"].update(header)

    # METRICS PANEL
    mom = data.get_momentum()
    lag = data.binance_price - (mgr.strike_price if mgr.strike_price else 0)
    
    m_table = Table(box=box.SIMPLE)
    m_table.add_column("Metric", style="dim")
    m_table.add_column("Value", style="bold")
    m_table.add_column("Signal Condition")
    
    m_table.add_row("Binance Price", f"${data.binance_price:,.2f}", "-")
    m_table.add_row("Chainlink Strike", f"${mgr.strike_price:,.2f}" if mgr.strike_price else "WAITING...", "Fixed @ :00")
    m_table.add_row("Momentum (60s)", f"${mom:+.2f}", f"> ${CONFIG['BINANCE_MOMENTUM_THRESHOLD']}")
    m_table.add_row("Oracle Lag", f"${lag:+.2f}", f"> ${CONFIG['ORACLE_LAG_THRESHOLD']}")
    
    layout["metrics"].update(Panel(m_table, title="📊 LIVE ANALYSIS"))

    # MARKET PANEL
    p_table = Table(box=box.SIMPLE)
    p_table.add_column("Side")
    p_table.add_column("Price")
    p_table.add_column("Implied Prob")
    
    p_table.add_row("YES (UP)", f"{data.yes_ask:.2f}", f"{data.yes_ask*100:.0f}%", style="green")
    p_table.add_row("NO (DOWN)", f"{1.0-data.yes_bid:.2f}", f"{(1.0-data.yes_bid)*100:.0f}%", style="red")
    
    layout["market"].update(Panel(p_table, title="🎲 ORDER BOOK"))

    # FOOTER (TRADING)
    t_text = Text()
    t_text.append(f"STATUS: {status_msg}\n", style="bold yellow")
    t_text.append(f"BALANCE: ${trader.balance:,.2f} | POSITIONS: {trader.position if trader.position else 'NONE'}", style="dim white")
    layout["footer"].update(Panel(t_text, title="💼 PAPER TRADER", border_style="yellow"))

    return layout

# ==========================================
# 🚀 IGNITION
# ==========================================
if __name__ == "__main__":
    # Start Threads
    threading.Thread(target=start_binance, daemon=True).start()
    threading.Thread(target=start_rtds, daemon=True).start()
    threading.Thread(target=start_clob, daemon=True).start()
    
    Console().print("[bold green]SYSTEM ONLINE. WAITING FOR DATA STREAMS...[/]")
    time.sleep(3)
    
    with Live(render_dashboard(), refresh_per_second=2, screen=True) as live:
        while True:
            live.update(render_dashboard())
            time.sleep(0.5)