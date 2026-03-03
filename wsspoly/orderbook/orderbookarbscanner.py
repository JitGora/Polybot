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
# ⚙️ CONFIGURATION (Example: Will Trump win?)
# ==========================================
UP_TOKEN_ID = "14197653263366219460228392048577973708611537403654355619013699932113332937727"
DOWN_TOKEN_ID = "87377448089926155828967105198474486114762911404505021291871037774420878245020"

class MarketState:
    def __init__(self, name, color):
        self.name = name
        self.color = color
        self.bids = {}
        self.asks = {}
        self.msg_count = 0

    def update(self, price, size, side):
        self.msg_count += 1
        target = self.bids if side == "BUY" else self.asks
        if size == 0:
            target.pop(price, None)
        else:
            target[price] = size

    def get_snapshot(self):
        # Return Top 15 levels sorted
        s_bids = sorted(self.bids.items(), key=lambda x: float(x[0]), reverse=True)[:15]
        s_asks = sorted(self.asks.items(), key=lambda x: float(x[0]))[:15]
        return s_bids, s_asks

# Initialize States
up_state = MarketState("UP (YES)", "green")
down_state = MarketState("DOWN (NO)", "red")

# ==========================================
# 🔌 WEBSOCKET LOGIC
# ==========================================
def on_message(ws, message):
    try:
        data = json.loads(message)
        
        # 1. SNAPSHOT
        if data.get('event_type') == 'book':
            asset = data.get('asset_id')
            state = up_state if asset == UP_TOKEN_ID else down_state if asset == DOWN_TOKEN_ID else None
            if state:
                state.bids = {x['price']: float(x['size']) for x in data.get('bids', [])}
                state.asks = {x['price']: float(x['size']) for x in data.get('asks', [])}

        # 2. UPDATE
        elif data.get('event_type') == 'price_change':
            for change in data.get('price_changes', []):
                asset = change.get('asset_id')
                if asset == UP_TOKEN_ID:
                    up_state.update(change['price'], float(change['size']), change['side'])
                elif asset == DOWN_TOKEN_ID:
                    down_state.update(change['price'], float(change['size']), change['side'])

    except Exception:
        pass 

def start_socket():
    ws = websocket.WebSocketApp(
        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_open=lambda ws: ws.send(json.dumps({
            "type": "subscribe", "channel": "market", "assets_ids": [UP_TOKEN_ID, DOWN_TOKEN_ID]
        })),
        on_message=on_message
    )
    ws.run_forever()

# ==========================================
# 🎨 VISUALIZATION COMPONENTS
# ==========================================
def create_imbalance_bar(bids, asks):
    """Visualizes the 'Battle' between buyers and sellers"""
    vol_bid = sum(x[1] for x in bids)
    vol_ask = sum(x[1] for x in asks)
    total = vol_bid + vol_ask if (vol_bid + vol_ask) > 0 else 1
    
    bid_pct = (vol_bid / total)
    
    # Create the ASCII Bar
    bar_width = 30
    fill = int(bid_pct * bar_width)
    
    # Color logic: Green if Buyers > 50%, Red if Sellers > 50%
    bar_color = "green" if bid_pct >= 0.5 else "red"
    
    bar_str = "█" * fill + "░" * (bar_width - fill)
    return Text(f"BUYERS {bid_pct:.0%} [{bar_str}] SELLERS {1-bid_pct:.0%}", style=f"bold {bar_color}")

def render_panel(state):
    bids, asks = state.get_snapshot()
    best_bid = float(bids[0][0]) if bids else 0
    best_ask = float(asks[0][0]) if asks else 0
    spread = best_ask - best_bid
    
    # Header Info
    header_info = Text()
    header_info.append(f"{state.name}\n", style=f"bold {state.color} underline")
    header_info.append(f"${best_bid:.3f} ", style="bold white")
    header_info.append("vs", style="dim")
    header_info.append(f" ${best_ask:.3f} ", style="bold white")
    
    # Warning for wide spreads
    spread_color = "green" if spread < 0.01 else "yellow" if spread < 0.03 else "bold red"
    header_info.append(f"(Sprd: {spread:.3f})", style=spread_color)

    # Imbalance Bar
    imbalance = create_imbalance_bar(bids, asks)

    # Table
    table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0,1))
    table.add_column("BID QTY", justify="right", style=f"{state.color}")
    table.add_column("PRICE", justify="center", style="bold white")
    table.add_column("ASK QTY", justify="left", style=f"{state.color}")

    for i in range(12):
        b_qty = f"{bids[i][1]:,.0f}" if i < len(bids) else ""
        b_prc = f"{bids[i][0]}" if i < len(bids) else ""
        
        a_qty = f"{asks[i][1]:,.0f}" if i < len(asks) else ""
        a_prc = f"{asks[i][0]}" if i < len(asks) else ""
        
        # Merge prices into center column if they are close, else split logic needed
        # For simplicity, we show Bid Price | Ask Price in one row is tricky if levels differ.
        # Better: Show just Bid Price and Ask Price separately or standard book view.
        # Let's do the "Ladder" view: Bid Qty | Price | Ask Qty 
        # But prices often don't match. Let's do Standard Split:
        
        price_display = f"{b_prc} | {a_prc}"
        table.add_row(b_qty, price_display, a_qty)

    content = Layout()
    content.split(
        Layout(Align.center(header_info), size=2),
        Layout(Align.center(imbalance), size=2),
        Layout(table)
    )
    
    return Panel(content, border_style=state.color)

def check_arbitrage():
    """Checks if Yes + No is cheaper than $1.00"""
    bids_up, asks_up = up_state.get_snapshot()
    bids_down, asks_down = down_state.get_snapshot()
    
    if asks_up and asks_down:
        best_ask_up = float(asks_up[0][0])
        best_ask_down = float(asks_down[0][0])
        total_cost = best_ask_up + best_ask_down
        
        if total_cost < 0.995: # 0.5% profit margin threshold
            profit = (1.00 - total_cost) * 100
            return Panel(
                Align.center(f"🚨 ARBITRAGE DETECTED 🚨\nBUY YES: ${best_ask_up} | BUY NO: ${best_ask_down}\nCOST: ${total_cost:.3f} | PROFIT: {profit:.1f}%"),
                style="bold white on red",
                box=box.DOUBLE
            )
    return None

def generate_dashboard():
    layout = Layout()
    
    # Header
    arb_panel = check_arbitrage()
    top_element = arb_panel if arb_panel else Align.center(Text("⚡ POLYMARKET TERMINAL LIVE ⚡", style="bold blue on black"))
    
    layout.split(
        Layout(top_element, size=3),
        Layout(name="main")
    )
    
    layout["main"].split_row(
        Layout(render_panel(up_state)),
        Layout(render_panel(down_state))
    )
    
    return layout

# ==========================================
# 🚀 RUN
# ==========================================
if __name__ == "__main__":
    t = threading.Thread(target=start_socket, daemon=True)
    t.start()
    
    console = Console()
    try:
        with Live(generate_dashboard(), refresh_per_second=10, screen=True) as live:
            while True:
                live.update(generate_dashboard())
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass