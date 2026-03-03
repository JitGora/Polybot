import websocket
import json
import threading
import time
from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console
from rich import box
from rich.text import Text

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
# The Asset ID from your logs
TARGET_ASSET_ID = "49355772724289033263187200853536661750165550914706419250143489206926535400772"

class OrderBookProcessor:
    def __init__(self):
        # We store the book as dictionaries: { "0.57": 345.2, "0.56": 1000.0 }
        self.bids = {} 
        self.asks = {}
        self.last_hash = "Waiting..."
        self.sequence = 0

    def process_message(self, message):
        try:
            data = json.loads(message)
            
            # 1. Handle Initial Snapshot ("book")
            if 'event_type' in data and data['event_type'] == 'book':
                # Clear existing
                self.bids = {}
                self.asks = {}
                
                # Populate Bids
                for item in data.get('bids', []):
                    self.bids[item['price']] = float(item['size'])
                
                # Populate Asks
                for item in data.get('asks', []):
                    self.asks[item['price']] = float(item['size'])
                
                self.last_hash = "SNAPSHOT LOADED"

            # 2. Handle Updates ("price_change")
            elif 'event_type' in data and data['event_type'] == 'price_change':
                for change in data.get('price_changes', []):
                    # Only process the asset we are watching (ignore the paired No/Yes token for clarity)
                    if change.get('asset_id') == TARGET_ASSET_ID:
                        price = change['price']
                        size = float(change['size'])
                        side = change['side'] # BUY or SELL
                        
                        # Logic: In Polymarket API, 'side' in price_change tells us which book to update
                        if side == "BUY":
                            if size == 0:
                                self.bids.pop(price, None) # Remove price level if size is 0
                            else:
                                self.bids[price] = size    # Update/Add price level
                        elif side == "SELL":
                            if size == 0:
                                self.asks.pop(price, None)
                            else:
                                self.asks[price] = size
                                
                        self.last_hash = f"Update: {change['hash'][:8]}..."
                        self.sequence += 1
                        
        except Exception as e:
            self.last_hash = f"Error: {e}"

    def get_sorted_lists(self):
        """Returns sorted list of (price, size) tuples"""
        # Bids: Highest price first (Descending)
        sorted_bids = sorted(self.bids.items(), key=lambda x: float(x[0]), reverse=True)
        # Asks: Lowest price first (Ascending)
        sorted_asks = sorted(self.asks.items(), key=lambda x: float(x[0]))
        return sorted_bids, sorted_asks

# Initialize Processor
processor = OrderBookProcessor()

# ==========================================
# 🔌 WEBSOCKET LOGIC
# ==========================================
def on_message(ws, message):
    processor.process_message(message)

def on_error(ws, error):
    print(f"Error: {error}")

def on_open(ws):
    print("✅ Connected. Sending Subscribe...")
    sub_msg = {
        "type": "subscribe",
        "channel": "market",
        "assets_ids": [TARGET_ASSET_ID]
    }
    ws.send(json.dumps(sub_msg))

def start_socket():
    ws = websocket.WebSocketApp(
        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error
    )
    ws.run_forever()

# ==========================================
# 🎨 UI LOGIC
# ==========================================
def generate_ui():
    bids, asks = processor.get_sorted_lists()
    
    # Calculate Spread
    best_bid = float(bids[0][0]) if bids else 0
    best_ask = float(asks[0][0]) if asks else 0
    spread = best_ask - best_bid
    
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold white", expand=True)
    table.add_column("Bid Size", justify="right", style="green")
    table.add_column("Bid Price", justify="right", style="bold green")
    table.add_column("Spread", justify="center", style="yellow")
    table.add_column("Ask Price", justify="left", style="bold red")
    table.add_column("Ask Size", justify="left", style="red")

    # Render top 10 rows
    for i in range(10):
        bid_p = f"{float(bids[i][0]):.2f}" if i < len(bids) else ""
        bid_s = f"{bids[i][1]:,.0f}" if i < len(bids) else ""
        
        ask_p = f"{float(asks[i][0]):.2f}" if i < len(asks) else ""
        ask_s = f"{asks[i][1]:,.0f}" if i < len(asks) else ""
        
        # Center column logic
        center = "---"
        if i == 0 and best_bid > 0 and best_ask > 0:
            center = f"{spread:.2f}"
            
        table.add_row(bid_s, bid_p, center, ask_p, ask_s)

    panel = Panel(
        table, 
        title=f"LIVE ORDERBOOK (Updates: {processor.sequence})",
        subtitle=processor.last_hash
    )
    return panel

if __name__ == "__main__":
    # Start WS in background
    t = threading.Thread(target=start_socket, daemon=True)
    t.start()
    
    # Run UI in foreground
    console = Console()
    try:
        with Live(generate_ui(), refresh_per_second=10) as live:
            while True:
                live.update(generate_ui())
                time.sleep(0.05)
    except KeyboardInterrupt:
        print("Stopped.")