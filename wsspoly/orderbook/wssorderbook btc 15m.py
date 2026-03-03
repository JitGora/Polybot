import websocket
import json
import time
from datetime import datetime

# Your BTC 15m Up token ID (swap for new markets)
UP_TOKEN_ID = "49355772724289033263187200853536661750165550914706419250143489206926535400772"

class PolymarketOrderbookWSS:
    """Streams COMPLETE orderbook (10+ levels bids/asks) - not just top spread"""
    
    def __init__(self):
        self.orderbook = {"bids": [], "asks": []}  # Live book state
        
    def on_message(self, ws, message):
        """Parse FULL orderbook → print top 5 levels + spread"""
        try:
            data = json.loads(message)
            if 'data' in data and data['data']:
                book = data['data'][0]
                
                # Update local orderbook
                self.orderbook['bids'] = book.get('bids', [])[:5]  # Top 5 bids
                self.orderbook['asks'] = book.get('asks', [])[:5]  # Top 5 asks
                
                # Print formatted orderbook
                self.print_orderbook()
                
        except Exception as e:
            print(f"❌ Parse error: {e}")
    
    def print_orderbook(self):
        """Pretty print full orderbook ladder"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{timestamp}] 📊 FULL ORDERBOOK (Top 5 levels)")
        print("=" * 60)
        
        # Header
        print(f"{'Price':>8} {'Bid Size':>10} {' ':>8} {'Ask Size':>10} {'Price':>8}")
        print("-" * 60)
        
        # Bids (descending) vs Asks (ascending)
        for i in range(5):
            bid_price = float(self.orderbook['bids'][i]['price']) if i < len(self.orderbook['bids']) else 0
            bid_size = float(self.orderbook['bids'][i]['size']) if i < len(self.orderbook['bids']) else 0
            ask_price = float(self.orderbook['asks'][i]['price']) if i < len(self.orderbook['asks']) else 0
            ask_size = float(self.orderbook['asks'][i]['size']) if i < len(self.orderbook['asks']) else 0
            
            print(f"{bid_price:>8.3f} {bid_size:>10.0f} | {ask_size:>10.0f} {ask_price:>8.3f}")
        
        # Spread summary
        if self.orderbook['bids'] and self.orderbook['asks']:
            spread = float(self.orderbook['asks'][0]['price']) - float(self.orderbook['bids'][0]['price'])
            print(f"\n💰 SPREAD: ${spread:.3f}  |  Best: ${float(self.orderbook['bids'][0]['price']):.3f} / ${float(self.orderbook['asks'][0]['price']):.3f}")
        print("-" * 60 + "\n")
    
    def on_error(self, ws, error):
        print(f"❌ WS Error: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        print("🔌 WS Closed - Reconnecting...")
        time.sleep(2)
        self.run()
    
    def on_open(self, ws):
        print("✅ WSS Connected! Subscribing to FULL orderbook...")
        sub_msg = {
            "type": "subscribe",
            "channel": "market",
            "assets_ids": [UP_TOKEN_ID]
        }
        ws.send(json.dumps(sub_msg))
    
    def run(self):
        """Start WebSocket with full orderbook streaming"""
        websocket.enableTrace(True)  # Debug (remove in prod)
        self.ws = websocket.WebSocketApp(
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()

if __name__ == "__main__":
    print("🚀 Polymarket BTC 15m FULL ORDERBOOK WEBSOCKET")
    print("📈 Streams 5+ levels bids/asks + live spread\n")
    
    wss = PolymarketOrderbookWSS()
    wss.run()
