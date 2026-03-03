import websocket
import json
import threading
import time
from datetime import datetime
from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console
from rich.align import Align
from rich import box
from rich.text import Text

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
UP_TOKEN_ID = "111182699219113838093898037003143408510051202308797016320340900609309351465396"
DOWN_TOKEN_ID = "108088466868542673839859888637013937992477714898575323385745076156148373730151"

class MarketState:
    """Stores the state for ONE token with analytics"""
    def __init__(self, name, color):
        self.name = name
        self.color = color  # Main theme color for this side (Green or Red)
        self.bids = {}
        self.asks = {}
        self.last_update_ts = datetime.now()
        self.msg_count = 0

    def update(self, price, size, side):
        self.last_update_ts = datetime.now()
        self.msg_count += 1
        target = self.bids if side == "BUY" else self.asks
        if size == 0:
            target.pop(price, None)
        else:
            target[price] = size

    def get_snapshot(self):
        # Sort and return top 15 levels
        s_bids = sorted(self.bids.items(), key=lambda x: float(x[0]), reverse=True)[:15]
        s_asks = sorted(self.asks.items(), key=lambda x: float(x[0]))[:15]
        return s_bids, s_asks

# Initialize State
up_state = MarketState("UP (YES)", "green")
down_state = MarketState("DOWN (NO)", "red")

# ==========================================
# 🔌 WEBSOCKET LOGIC
# ==========================================
def on_message(ws, message):
    try:
        data = json.loads(message)
        
        # Helper to process a single change dictionary
        def process_change(item, is_snapshot=False):
            asset = item.get('asset_id')
            if asset == UP_TOKEN_ID:
                up_state.update(item['price'], float(item['size']), "BUY" if is_snapshot else item['side'])
                # In snapshot, bids are explicitly separated, but logic is same
            elif asset == DOWN_TOKEN_ID:
                down_state.update(item['price'], float(item['size']), "BUY" if is_snapshot else item['side'])

        # 1. SNAPSHOT
        if data.get('event_type') == 'book':
            # Polymarket snapshot separates bids/asks lists, logic slightly different
            asset = data.get('asset_id')
            target_state = up_state if asset == UP_TOKEN_ID else down_state if asset == DOWN_TOKEN_ID else None
            
            if target_state:
                # Clear and Set
                target_state.bids = {x['price']: float(x['size']) for x in data.get('bids', [])}
                target_state.asks = {x['price']: float(x['size']) for x in data.get('asks', [])}

        # 2. UPDATE
        elif data.get('event_type') == 'price_change':
            for change in data.get('price_changes', []):
                asset = change.get('asset_id')
                if asset == UP_TOKEN_ID:
                    up_state.update(change['price'], float(change['size']), change['side'])
                elif asset == DOWN_TOKEN_ID:
                    down_state.update(change['price'], float(change['size']), change['side'])
                    
    except Exception as e:
        pass # Silently ignore parsing errors in pro-mode to keep UI clean

def start_socket():
    ws = websocket.WebSocketApp(
        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_open=lambda ws: ws.send(json.dumps({
            "type": "subscribe", "channel": "market", 
            "assets_ids": [UP_TOKEN_ID, DOWN_TOKEN_ID]
        })),
        on_message=on_message
    )
    ws.run_forever()

# ==========================================
# 🎨 PRO UI BUILDER
# ==========================================
def render_token_panel(state):
    bids, asks = state.get_snapshot()
    
    # --- Metrics Calculation ---
    best_bid = float(bids[0][0]) if bids else 0
    best_ask = float(asks[0][0]) if asks else 0
    spread = best_ask - best_bid
    
    # Calculate simple "Pressure" (Total volume in top 15 levels)
    vol_bid = sum(x[1] for x in bids)
    vol_ask = sum(x[1] for x in asks)
    total_vol = vol_bid + vol_ask if (vol_bid + vol_ask) > 0 else 1
    bid_pressure = (vol_bid / total_vol) * 100
    
    # --- Header Component ---
    header_text = Text()
    header_text.append(f"{state.name}  ", style=f"bold {state.color}")
    header_text.append(f"${best_bid:.3f} ", style="bold white")
    header_text.append("vs", style="dim white")
    header_text.append(f" ${best_ask:.3f}   ", style="bold white")
    header_text.append(f"Spread: {spread:.3f}", style="black on yellow" if spread < 0.01 else "yellow")
    
    # Pressure Bar
    bar_len = 20
    fill = int((bid_pressure / 100) * bar_len)
    bar_str = "█" * fill + "░" * (bar_len - fill)
    pressure_text = Text(f"\nBuying Pressure: {bid_pressure:.0f}% [{bar_str}]", style="cyan")

    # --- Data Table (Standard Orderbook Layout) ---
    table = Table(
        box=box.SIMPLE, 
        expand=True, 
        padding=(0, 1),
        show_header=True,
        header_style="bold white on black"
    )
    # Define columns with fixed width for stability
    table.add_column("BID QTY", justify="right", style="green", width=12)
    table.add_column("BID $", justify="right", style="bold green", width=8)
    table.add_column("ASK $", justify="left", style="bold red", width=8)
    table.add_column("ASK QTY", justify="left", style="red", width=12)

    # Fill Rows (Max 15)
    for i in range(15):
        bid_q = f"{bids[i][1]:,.0f}" if i < len(bids) else ""
        bid_p = f"{float(bids[i][0]):.3f}" if i < len(bids) else ""
        ask_p = f"{float(asks[i][0]):.3f}" if i < len(asks) else ""
        ask_q = f"{asks[i][1]:,.0f}" if i < len(asks) else ""
        
        # Highlight top row
        style = "bold" if i == 0 else ""
        table.add_row(bid_q, bid_p, ask_p, ask_q, style=style)

    # Combine Header + Table
    content = Layout()
    content.split(
        Layout(Align.center(header_text + pressure_text), size=3),
        Layout(table)
    )

    return Panel(
        content,
        border_style=f"bold {state.color}",
        title=f"LIVE DATA ● {state.msg_count}",
        title_align="right"
    )

def generate_dashboard():
    layout = Layout()
    
    # Top Header
    header = Text(" ⚡ POLYMARKET LIVE TERMINAL ⚡ ", style="bold white on blue", justify="center")
    
    # Split Main Area
    main_area = Layout(name="main")
    main_area.split_row(
        Layout(render_token_panel(up_state), name="left"),
        Layout(render_token_panel(down_state), name="right")
    )
    
    layout.split(
        Layout(header, size=1),
        main_area
    )
    return layout

# ==========================================
# 🚀 EXECUTION
# ==========================================
if __name__ == "__main__":
    console = Console()
    console.clear()
    console.print("[bold yellow]🚀 Connecting to Matrix...[/]")
    
    # Start Data Stream
    t = threading.Thread(target=start_socket, daemon=True)
    t.start()
    time.sleep(1) # Warmup

    try:
        # High refresh rate for smooth feel
        with Live(generate_dashboard(), refresh_per_second=12, screen=True) as live:
            while True:
                live.update(generate_dashboard())
                time.sleep(0.08)
    except KeyboardInterrupt:
        console.print("[red]Terminated[/]")