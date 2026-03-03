import websocket
import json
import threading
import time
from datetime import datetime

# Token ID for Polymarket BTC 15m "Up" outcome (changes per market)
UP_TOKEN_ID = "101430793922143632629548036643126533393228163838905823738054373382125884840455"

class PolymarketWSS:
    """Real-time Polymarket BTC 15m bid/ask streamer via WebSocket"""
    
    def __init__(self):
        self.ws = None                    # WebSocket connection
        self.last_bid = 0                 # Cache last known bid
        self.last_ask = 0                 # Cache last known ask
        
    def on_message(self, ws, message):
        """Parse incoming WS messages → extract top bid/ask → print"""
        try:
            data = json.loads(message)
            if 'data' in data and data['data']:      # Valid orderbook update
                book = data['data'][0]               # First (only) market
                # Safely extract top bid price (or keep last known)
                bid = float(book.get('bids', [{}])[0].get('price', 0)) if book.get('bids') else self.last_bid
                # Safely extract top ask price (or keep last known)  
                ask = float(book.get('asks', [{}])[0].get('price', 0)) if book.get('asks') else self.last_ask
                
                self.last_bid, self.last_ask = bid, ask  # Update cache
                timestamp = datetime.now().strftime("%H:%M:%S")
                print(f"[{timestamp}] 🟢 Up: ${bid:.3f} / ${ask:.3f}")
                
        except Exception as e:
            print(f"Parse error: {e}")            # Silent fail on bad data
    
    def on_error(self, ws, error):
        """Handle connection errors"""
        print(f"❌ WS Error: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        """Auto-reconnect on disconnect"""
        print("🔌 WS Closed - Reconnecting...")
        time.sleep(2)                        # Brief pause
        self.run()                           # Restart
    
    def on_open(self, ws):
        """Subscribe to market on connect (PUBLIC - no API key needed!)"""
        print("✅ WSS Connected! Subscribing...")
        sub_msg = {
            "type": "subscribe",
            "channel": "market", 
            "assets_ids": [UP_TOKEN_ID]      # Your specific BTC 15m market
        }
        ws.send(json.dumps(sub_msg))         # Send subscription
    
    def run(self):
        """Main WS loop with auto-reconnect"""
        websocket.enableTrace(True)          # Debug mode (disable in prod)
        self.ws = websocket.WebSocketApp(
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",  # Public WS endpoint
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()                # Blocking run

if __name__ == "__main__":
    print("🚀 Polymarket BTC 15m WEBSOCKET (Zero Latency)")
    print("Press Ctrl+C to stop\n")
    
    wss = PolymarketWSS()
    wss.run()
