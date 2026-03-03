import asyncio
import json
import time
import sys
import logging
import math
import aiohttp
import websockets
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ============================================================================
# CONFIGURATION
# ============================================================================

# Logging Setup (Cleaner for Terminal Dashboard)
logging.basicConfig(level=logging.ERROR) 
logger = logging.getLogger("PolyBot")

CONSTANTS = {
    "WINDOW_SECONDS": 900,         # 15 Minutes
    "LAG_THRESHOLD": 50.0,         # USD difference to trigger signal
    "MIN_EV": 0.05,                # Minimum Expected Value (5%)
    "FRESH_WINDOW_LIMIT": 15,      # Only trade if we catch window within first 15s
    "URLS": {
        "BINANCE": "wss://stream.binance.com:9443/ws/btcusdt@trade",
        "RTDS": "wss://ws-live-data.polymarket.com", #
        "CLOB": "wss://ws-subscriptions-clob.polymarket.com/ws/market", #
        "GAMMA": "https://gamma-api.polymarket.com" #
    },
    "FEES": {
        "MAX_FEE": 0.0156,         # 1.56% max fee at 0.50 probability
        "MIN_FEE": 0.001           # Lower fee at extremes (approx)
    }
}

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Orderbook:
    bids: List[tuple] = field(default_factory=list) # List of (price, size)
    asks: List[tuple] = field(default_factory=list)

    def best_bid(self) -> float: return self.bids[0][0] if self.bids else 0.0
    def best_ask(self) -> float: return self.asks[0][0] if self.asks else 0.0
    
@dataclass
class MarketState:
    """Represents the current 15-minute trading window"""
    slug: str = "Initializing..."
    window_start: int = 0
    strike_price: Optional[float] = None
    up_token: Optional[str] = None
    down_token: Optional[str] = None
    status: str = "WAITING" # WAITING, LIVE, ENDED
    
    # Orderbooks for this specific window
    books: Dict[str, Orderbook] = field(default_factory=lambda: {"UP": Orderbook(), "DOWN": Orderbook()})

# ============================================================================
# COMPONENT 1: BACKGROUND PRICE ENGINE (Always On)
# ============================================================================

class PriceEngine:
    """
    Runs continuously in the background.
    Maintains the 'Latest Known Price' in memory at all times.
    """
    def __init__(self):
        self.binance_btc = 0.0      # Fast Spot (<50ms)
        self.chainlink_btc = 0.0    # Slow Oracle (~5s lag)
        self.last_update_binance = 0
        self.last_update_chainlink = 0

    async def start(self):
        await asyncio.gather(self._stream_binance(), self._stream_chainlink())

    async def _stream_binance(self):
        while True:
            try:
                async with websockets.connect(CONSTANTS["URLS"]["BINANCE"]) as ws:
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        if 'p' in data:
                            self.binance_btc = float(data['p'])
                            self.last_update_binance = time.time()
            except Exception:
                await asyncio.sleep(1)

    async def _stream_chainlink(self):
        """RTDS Connection with 5s Keep-Alive"""
        while True:
            try:
                async with websockets.connect(CONSTANTS["URLS"]["RTDS"]) as ws:
                    # Subscribe to Chainlink BTC/USD
                    sub_msg = {
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": "{\"symbol\":\"btc/usd\"}"
                        }]
                    }
                    await ws.send(json.dumps(sub_msg))
                    
                    last_ping = time.time()
                    
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=6)
                            data = json.loads(msg)
                            
                            if data.get('topic') == 'crypto_prices_chainlink' and data.get('type') == 'update':
                                val = float(data['payload']['value'])
                                self.chainlink_btc = val
                                self.last_update_chainlink = time.time()
                                
                        except asyncio.TimeoutError:
                            # Strict 5s Ping Requirement for RTDS
                            if time.time() - last_ping > 5:
                                await ws.ping()
                                last_ping = time.time()
                                
            except Exception:
                await asyncio.sleep(1)

# ============================================================================
# COMPONENT 2: MARKET MANAGER (Logic Layer)
# ============================================================================

class MarketManager:
    """
    Manages the lifecycle of the 15-minute window.
    Snapshots prices from PriceEngine instantly at window start.
    """
    def __init__(self, price_engine: PriceEngine):
        self.pe = price_engine
        self.state = MarketState()
        self.clob_ws = None

    def calculate_fee(self, price: float) -> float:
        """Dynamic Taker Fee: Peaks at 1.56% near 0.50"""
        dist_from_edge = min(price, 1 - price) # 0.0 to 0.5
        # Linear approximation of the fee curve
        # 0.50 prob -> 1.56% fee
        # 0.05 prob -> ~0.2% fee
        fee = CONSTANTS["FEES"]["MIN_FEE"] + (dist_from_edge * 2 * (CONSTANTS["FEES"]["MAX_FEE"] - CONSTANTS["FEES"]["MIN_FEE"]))
        return fee

    def get_window_details(self):
        now = int(time.time())
        window_size = CONSTANTS["WINDOW_SECONDS"]
        window_start = (now // window_size) * window_size
        slug = f"btc-updown-15m-{window_start}"
        return slug, window_start, now

    async def fetch_tokens(self, slug):
        """Fetches Token IDs from Gamma API"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{CONSTANTS['URLS']['GAMMA']}/events?slug={slug}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data:
                            m = data[0]['markets'][0]
                            ids = json.loads(m['clobTokenIds'])
                            outcomes = json.loads(m['outcomes'])
                            
                            if outcomes[0].upper() in ['UP', 'YES']:
                                return ids[0], ids[1]
                            return ids[1], ids[0]
        except Exception:
            return None, None
        return None, None

    async def run_lifecycle(self):
        while True:
            slug, start_ts, now = self.get_window_details()
            
            # --- NEW WINDOW DETECTION ---
            if self.state.slug != slug:
                # 1. Reset State
                self.state = MarketState(slug=slug, window_start=start_ts)
                
                # 2. Check if we are "Fresh" (started strictly at :00, :15 etc)
                seconds_into_window = now - start_ts
                
                if seconds_into_window <= CONSTANTS["FRESH_WINDOW_LIMIT"]:
                    # --- ZERO-WAIT SNAPSHOT ---
                    # We grab the price NOW. We do not await a socket message.
                    if self.pe.chainlink_btc > 0:
                        self.state.strike_price = self.pe.chainlink_btc
                        self.state.status = "LIVE"
                    else:
                        self.state.status = "ERROR_NO_FEED"
                else:
                    self.state.status = "WAITING_NEXT_WINDOW"

                # 3. Fetch Tokens
                if self.state.status == "LIVE":
                    up, down = await self.fetch_tokens(slug)
                    if up and down:
                        self.state.up_token = up
                        self.state.down_token = down
                        if self.clob_ws:
                            await self.subscribe_clob(up, down)

            await asyncio.sleep(0.1)

    async def stream_clob(self):
        """Maintains connection to Orderbook with 10s Ping"""
        while True:
            try:
                async with websockets.connect(CONSTANTS["URLS"]["CLOB"]) as ws:
                    self.clob_ws = ws
                    
                    if self.state.up_token:
                        await self.subscribe_clob(self.state.up_token, self.state.down_token)
                    
                    last_ping = time.time()
                    
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=11)
                            data = json.loads(msg)
                            
                            msgs = data if isinstance(data, list) else [data]
                            for m in msgs:
                                if m.get('event_type') == 'book': #
                                    self._update_book(m)
                                    
                        except asyncio.TimeoutError:
                            # Strict 10s Ping Requirement for CLOB
                            if time.time() - last_ping > 10:
                                await ws.ping()
                                last_ping = time.time()
                                
            except Exception:
                self.clob_ws = None
                await asyncio.sleep(1)

    async def subscribe_clob(self, up_id, down_id):
        if self.clob_ws:
            msg = {"type": "market", "assets_ids": [up_id, down_id]} #
            await self.clob_ws.send(json.dumps(msg))

    def _update_book(self, data):
        asset = data.get('asset_id')
        if not asset: return
        
        if asset == self.state.up_token: side = "UP"
        elif asset == self.state.down_token: side = "DOWN"
        else: return
        
        bids = []
        for b in data.get('bids', []):
            bids.append((float(b['price']), float(b['size'])))
            
        asks = []
        for a in data.get('asks', []):
            asks.append((float(a['price']), float(a['size'])))
            
        self.state.books[side].bids = bids
        self.state.books[side].asks = asks

# ============================================================================
# COMPONENT 3: TERMINAL DASHBOARD
# ============================================================================

async def run_dashboard(pe: PriceEngine, mm: MarketManager):
    """Refreshes the terminal UI"""
    while True:
        sys.stdout.write("\033[2J\033[H")
        
        st = mm.state
        status_icon = "🟢" if st.status == "LIVE" else "🔴"
        print(f"{status_icon} POLYMARKET BTC 15-MIN BOT | {st.status}")
        print(f"Slug: {st.slug}")
        
        if st.window_start > 0:
            elapsed = time.time() - st.window_start
            remaining = 900 - elapsed
            print(f"Time: {int(remaining//60)}m {int(remaining%60)}s remaining")
        print("-" * 50)

        strike = st.strike_price if st.strike_price else 0.0
        spot = pe.binance_btc
        oracle = pe.chainlink_btc
        lag = spot - oracle
        
        print(f"STRIKE (Locked):    ${strike:,.2f}")
        print(f"SPOT (Binance):     ${spot:,.2f}")
        print(f"ORACLE (Chainlink): ${oracle:,.2f}")
        
        lag_color = "\033[91m" if abs(lag) > CONSTANTS["LAG_THRESHOLD"] else "\033[92m"
        print(f"ORACLE LAG:         {lag_color}${lag:+.2f}\033[0m")
        print("-" * 50)

        if st.status == "LIVE" and st.up_token:
            up_b = st.books["UP"]
            down_b = st.books["DOWN"]
            
            print(f"UP:   Bid ${up_b.best_bid():.3f} | Ask ${up_b.best_ask():.3f}")
            print(f"DOWN: Bid ${down_b.best_bid():.3f} | Ask ${down_b.best_ask():.3f}")
            
            # SIGNAL LOGIC
            print("\n--- SIGNALS ---")
            
            if abs(lag) > CONSTANTS["LAG_THRESHOLD"]:
                direction = "UP" if lag > 0 else "DOWN"
                print(f"🚀 LAG SIGNAL: {direction} (Spot is ${abs(lag):.0f} {'higher' if lag>0 else 'lower'})")
                
                target_book = up_b if direction == "UP" else down_b
                price = target_book.best_ask()
                
                if price > 0:
                    # Calculate EV with New Fee Logic
                    fee = mm.calculate_fee(price)
                    # Simple assumption: Large lag = 90% win prob for short term scalp
                    ev = (0.90 * (1 - price)) - (0.10 * price) - fee
                    
                    if ev > CONSTANTS["MIN_EV"]:
                        print(f"✅ ACTION: BUY {direction} @ {price:.3f} | Fee: {fee:.2%} | EV: {ev:.2f}")
                    else:
                        print(f"⚠️ EV too low ({ev:.2f}) due to price/fees.")
            else:
                print("💤 No significant latency edge.")
                
        else:
            print("Waiting for next window start to capture fresh Strike...")

        await asyncio.sleep(0.2)

async def main():
    price_engine = PriceEngine()
    market_manager = MarketManager(price_engine)
    
    await asyncio.gather(
        price_engine.start(),
        market_manager.run_lifecycle(),
        market_manager.stream_clob(),
        run_dashboard(price_engine, market_manager)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot Stopped.")