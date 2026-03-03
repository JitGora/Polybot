import websocket
import json
import threading
import time
from datetime import datetime
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console
from rich.align import Align
from rich import box
from rich.text import Text
from rich.table import Table

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
# Market: "Will BTC hit $100k?" (Example ID)
# SWAP THIS ID FOR THE ACTIVE MARKET YOU ARE TRADING
POLY_MARKET_ID = "50255633935930752374357881813244167000624076032710629098110410004185610503371"

class DataCore:
    def __init__(self):
        # 1. BINANCE DIRECT (Speed of Light)
        self.binance_price = 0.0
        self.binance_updated = time.time()
        
        # 2. POLY ORACLE (The Referee)
        self.oracle_binance = 0.0  # Polymarket's view of Binance
        self.oracle_link = 0.0     # Chainlink (Resolution Source)
        self.oracle_updated = time.time()
        
        # 3. POLY MARKET (The Bet)
        self.poly_bid = 0.0
        self.poly_ask = 0.0
        self.poly_updated = time.time()

state = DataCore()

# ==========================================
# 📡 SOCKET 1: BINANCE DIRECT
# ==========================================
def on_binance_msg(ws, msg):
    try:
        data = json.loads(msg)
        # Binance Trade Stream: {"p": "price", ...}
        if 'p' in data:
            state.binance_price = float(data['p'])
            state.binance_updated = time.time()
    except: pass

def start_binance():
    # Stream: btcusdt@trade (Fastest granular updates)
    url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    ws = websocket.WebSocketApp(url, on_message=on_binance_msg)
    ws.run_forever()

# ==========================================
# 📡 SOCKET 2: POLYMARKET ORACLE (RTDS)
# ==========================================
def on_rtds_msg(ws, msg):
    try:
        d = json.loads(msg)
        if d.get('type') == 'update':
            payload = d.get('payload', {})
            symbol = payload.get('symbol')
            val = float(payload.get('value', 0))
            
            if symbol == 'btcusdt':
                state.oracle_binance = val
                state.oracle_updated = time.time()
            elif symbol == 'btc/usd':
                state.oracle_link = val
    except: pass

def start_rtds():
    url = "wss://ws-live-data.polymarket.com"
    def on_open(ws):
        # Subscribe to internal Binance relay AND Chainlink
        sub = {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices", "type": "*", "filters": '{"symbol":"btcusdt"}'},
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'}
            ]
        }
        ws.send(json.dumps(sub))
    
    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_rtds_msg)
    ws.run_forever()

# ==========================================
# 📡 SOCKET 3: POLYMARKET CLOB (The Bet)
# ==========================================
def on_clob_msg(ws, msg):
    try:
        data = json.loads(msg)
        # Snapshot
        if data.get('event_type') == 'book' and data.get('asset_id') == POLY_MARKET_ID:
            bids = data.get('bids', [])
            asks = data.get('asks', [])
            if bids: state.poly_bid = float(bids[0]['price'])
            if asks: state.poly_ask = float(asks[0]['price'])
            state.poly_updated = time.time()
            
        # Delta
        elif data.get('event_type') == 'price_change':
            for c in data.get('price_changes', []):
                if c.get('asset_id') == POLY_MARKET_ID:
                    if c['side'] == 'BUY': state.poly_bid = float(c['price'])
                    if c['side'] == 'SELL': state.poly_ask = float(c['price'])
                    state.poly_updated = time.time()
    except: pass

def start_clob():
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    sub = {"type": "subscribe", "channel": "market", "assets_ids": [POLY_MARKET_ID]}
    ws = websocket.WebSocketApp(url, on_open=lambda w: w.send(json.dumps(sub)), on_message=on_clob_msg)
    ws.run_forever()

# ==========================================
# 🎨 UI DASHBOARD
# ==========================================
def generate_view():
    # 1. Calculate Lag (Oracle Lag)
    # How far behind is Polymarket's internal data from Real Binance?
    oracle_lag = state.binance_price - state.oracle_binance
    lag_color = "red" if abs(oracle_lag) > 50 else "green"
    
    layout = Layout()
    layout.split_row(
        Layout(name="binance"),
        Layout(name="oracle"),
        Layout(name="poly")
    )
    
    # --- PANEL 1: BINANCE DIRECT ---
    b_txt = Text(f"\n${state.binance_price:,.2f}", style="bold cyan underline", justify="center")
    b_txt.append(f"\nSOURCE", style="dim")
    layout["binance"].update(Panel(b_txt, title="⚡ BINANCE DIRECT", border_style="cyan"))

    # --- PANEL 2: POLY RELAY (LAG MONITOR) ---
    o_txt = Text(f"\n${state.oracle_binance:,.2f}", style="bold white", justify="center")
    o_txt.append(f"\nLAG: ${oracle_lag:+.2f}", style=f"bold {lag_color}")
    if state.oracle_link:
        o_txt.append(f"\nChainlink: ${state.oracle_link:,.0f}", style="dim yellow")
        
    layout["oracle"].update(Panel(o_txt, title="🐢 POLY RELAY", border_style="white"))

    # --- PANEL 3: PREDICTION ---
    p_txt = Text(f"\nBID: {state.poly_bid:.2f} | ASK: {state.poly_ask:.2f}", style="bold green", justify="center")
    
    # Implied Odds
    mid = (state.poly_bid + state.poly_ask) / 2
    p_txt.append(f"\nODDS: {mid*100:.1f}%", style="bold green underline")
    
    layout["poly"].update(Panel(p_txt, title="🎲 PREDICTION MARKET", border_style="green"))

    # Wrapper
    main = Layout()
    main.split(
        Layout(Align.center(Text(" ⚡ TRI-SOCKET ARBITRAGE ENGINE ⚡ ", style="bold black on yellow")), size=1),
        layout
    )
    return main

# ==========================================
# 🚀 LAUNCH
# ==========================================
if __name__ == "__main__":
    Console().print("[yellow]Initializing Tri-Socket Engine...[/]")
    
    # Start all 3 threads
    threading.Thread(target=start_binance, daemon=True).start()
    threading.Thread(target=start_rtds, daemon=True).start()
    threading.Thread(target=start_clob, daemon=True).start()
    
    time.sleep(2) # Warmup
    
    try:
        with Live(generate_view(), refresh_per_second=10) as live:
            while True:
                live.update(generate_view())
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass