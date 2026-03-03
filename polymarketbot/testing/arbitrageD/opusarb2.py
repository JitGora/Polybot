"""
================================================================================
⚡ CERTAINTY HARVESTER - FIXED VERSION
================================================================================

FLOW:
1. Start price streams (Oracle, Spot)
2. Wait for prices to arrive
3. Get current window slug
4. Fetch token IDs from API
5. Subscribe to orderbook
6. Start scanning for opportunities

If joining mid-window: Ask user for strike price
"""

import json
import time
import threading
from datetime import datetime
from typing import Optional, Tuple, Dict
from dataclasses import dataclass

import requests
import websocket
from rich.live import Live
from rich.panel import Panel
from rich.console import Console
from rich.text import Text
from rich.layout import Layout
from rich.prompt import Prompt
from rich import box

# ============================================================
# ⚙️ CONFIG
# ============================================================
WINDOW_SIZE = 900
MAX_TIME_TO_TRADE = 180  # Last 3 minutes
MIN_DISTANCE = 50        # $50 minimum
MAX_ENTRY_PRICE = 0.97   # Don't pay more than 97¢
MIN_PROFIT = 0.02        # 2% minimum
GAMMA_API = "https://gamma-api.polymarket.com/events"

# ============================================================
# 🧱 DATA STORE
# ============================================================
class Data:
    def __init__(self):
        self.lock = threading.RLock()
        
        # Prices
        self.oracle = 0.0
        self.oracle_ts = 0
        self.spot = 0.0
        self.spot_ts = 0
        
        # Orderbook - store full book
        self.up_bids: Dict[str, float] = {}
        self.up_asks: Dict[str, float] = {}
        self.down_bids: Dict[str, float] = {}
        self.down_asks: Dict[str, float] = {}
        
        # Status
        self.oracle_connected = False
        self.spot_connected = False
        self.clob_connected = False
        self.clob_subscribed = False
        
        # Messages for debugging
        self.last_oracle_msg = ""
        self.last_clob_msg = ""
        self.msg_count = 0

    def set_oracle(self, price: float, ts: int):
        with self.lock:
            self.oracle = price
            self.oracle_ts = ts
            self.oracle_connected = True

    def set_spot(self, price: float, ts: int):
        with self.lock:
            self.spot = price
            self.spot_ts = ts
            self.spot_connected = True

    def set_book(self, side: str, bids: list, asks: list):
        with self.lock:
            if side == 'UP':
                self.up_bids = {b['price']: float(b['size']) for b in bids}
                self.up_asks = {a['price']: float(a['size']) for a in asks}
            else:
                self.down_bids = {b['price']: float(b['size']) for b in bids}
                self.down_asks = {a['price']: float(a['size']) for a in asks}
            self.clob_connected = True
            self.msg_count += 1

    def update_level(self, side: str, price: str, size: float, book_side: str):
        with self.lock:
            if side == 'UP':
                target = self.up_bids if book_side == 'BUY' else self.up_asks
            else:
                target = self.down_bids if book_side == 'BUY' else self.down_asks
            
            if size == 0:
                target.pop(price, None)
            else:
                target[price] = size
            self.msg_count += 1

    def best_ask(self, side: str) -> Tuple[float, float]:
        """Returns (price, size) for best ask"""
        with self.lock:
            asks = self.up_asks if side == 'UP' else self.down_asks
            if not asks:
                return 0.0, 0.0
            
            # Find minimum price
            min_price = min(float(p) for p in asks.keys())
            
            # Find size at that price
            for p, s in asks.items():
                if abs(float(p) - min_price) < 0.0001:
                    return min_price, s
            
            return min_price, 0.0

    def best_bid(self, side: str) -> Tuple[float, float]:
        """Returns (price, size) for best bid"""
        with self.lock:
            bids = self.up_bids if side == 'UP' else self.down_bids
            if not bids:
                return 0.0, 0.0
            
            max_price = max(float(p) for p in bids.keys())
            
            for p, s in bids.items():
                if abs(float(p) - max_price) < 0.0001:
                    return max_price, s
            
            return max_price, 0.0

    def clear_books(self):
        with self.lock:
            self.up_bids.clear()
            self.up_asks.clear()
            self.down_bids.clear()
            self.down_asks.clear()
            self.clob_subscribed = False

    def oracle_lag(self) -> float:
        if self.oracle_ts == 0:
            return -1
        return (time.time() * 1000 - self.oracle_ts) / 1000

data = Data()

# ============================================================
# 🎯 MARKET STATE
# ============================================================
class Market:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'CertaintyBot/1.0'
        
        self.window_start = 0
        self.slug = ""
        self.strike = 0.0
        self.strike_source = ""  # "oracle", "api", "user"
        
        self.up_token: Optional[str] = None
        self.down_token: Optional[str] = None
        
        self.status = "Starting..."
        self.error = ""
        
        self.cache: Dict[str, dict] = {}

    def current_window_start(self) -> int:
        return (int(time.time()) // WINDOW_SIZE) * WINDOW_SIZE

    def time_remaining(self) -> float:
        if self.window_start == 0:
            return WINDOW_SIZE
        return max(0, (self.window_start + WINDOW_SIZE) - time.time())

    def time_elapsed(self) -> float:
        if self.window_start == 0:
            return 0
        return time.time() - self.window_start

    def check_new_window(self) -> bool:
        """Returns True if new window started"""
        current = self.current_window_start()
        
        if current != self.window_start:
            old = self.window_start
            self.window_start = current
            self.slug = f"btc-updown-15m-{current}"
            
            # Only reset tokens if this is a real transition (not first load)
            if old > 0:
                self.up_token = None
                self.down_token = None
                self.strike = 0.0
                self.strike_source = ""
                data.clear_books()
            
            # Try to capture strike from oracle
            if data.oracle > 0 and self.time_elapsed() < 3:
                self.strike = data.oracle
                self.strike_source = "oracle"
            
            self.status = "New window"
            return True
        
        return False

    def set_strike_from_user(self, strike: float):
        """User provides strike price"""
        self.strike = strike
        self.strike_source = "user"
        self.status = f"Strike set: ${strike:,.0f}"

    def fetch_tokens(self) -> bool:
        """Fetch UP/DOWN token IDs"""
        if self.up_token and self.down_token:
            return True
        
        if not self.slug:
            self.slug = f"btc-updown-15m-{self.current_window_start()}"
        
        # Check cache
        if self.slug in self.cache:
            c = self.cache[self.slug]
            self.up_token = c['up']
            self.down_token = c['down']
            if self.strike == 0 and 'strike' in c:
                self.strike = c['strike']
                self.strike_source = "api"
            self.status = "Tokens loaded (cache)"
            return True
        
        # Fetch from API
        try:
            self.status = "Fetching tokens..."
            url = f"{GAMMA_API}?slug={self.slug}"
            r = self.session.get(url, timeout=10)
            
            if r.status_code != 200:
                self.error = f"API error: {r.status_code}"
                self.status = "API error"
                return False
            
            resp = r.json()
            if not resp:
                self.error = "Market not found"
                self.status = "Not found"
                return False
            
            event = resp[0]
            mkt = event.get('markets', [{}])[0]
            
            # Parse token IDs
            raw_ids = mkt.get('clobTokenIds', '[]')
            ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            
            raw_outcomes = mkt.get('outcomes', '[]')
            outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
            
            if len(ids) < 2 or len(outcomes) < 2:
                self.error = "Invalid market data"
                self.status = "Bad data"
                return False
            
            # Determine which is UP/DOWN
            if outcomes[0].lower() in ['up', 'yes']:
                self.up_token = ids[0]
                self.down_token = ids[1]
            else:
                self.up_token = ids[1]
                self.down_token = ids[0]
            
            # Try to get strike from question
            question = mkt.get('question', '')
            if self.strike == 0:
                import re
                m = re.search(r'\$([\d,]+\.?\d*)', question)
                if m:
                    try:
                        self.strike = float(m.group(1).replace(',', ''))
                        self.strike_source = "api"
                    except:
                        pass
            
            # Cache it
            self.cache[self.slug] = {
                'up': self.up_token,
                'down': self.down_token,
                'strike': self.strike
            }
            
            self.status = "Tokens ready"
            self.error = ""
            return True
            
        except Exception as e:
            self.error = str(e)[:50]
            self.status = "Fetch error"
            return False

    def winning_side(self) -> Tuple[str, float]:
        """Returns (side, distance) or ("UNKNOWN", 0)"""
        if self.strike <= 0 or data.oracle <= 0:
            return "UNKNOWN", 0.0
        
        diff = data.oracle - self.strike
        if diff > 0:
            return "UP", diff
        else:
            return "DOWN", abs(diff)

market = Market()

# ============================================================
# 📡 WEBSOCKET: ORACLE (Chainlink via Polymarket)
# ============================================================
class OracleWS:
    def __init__(self):
        self.ws = None
        self.connected = False
    
    def start(self):
        def run():
            while True:
                try:
                    self.ws = websocket.WebSocketApp(
                        "wss://ws-live-data.polymarket.com",
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close
                    )
                    self.ws.run_forever(ping_interval=30, ping_timeout=10)
                except Exception as e:
                    data.last_oracle_msg = f"Error: {e}"
                time.sleep(2)
        
        threading.Thread(target=run, daemon=True).start()
    
    def _on_open(self, ws):
        self.connected = True
        data.last_oracle_msg = "Connected, subscribing..."
        
        # Subscribe to Chainlink BTC/USD
        msg = {
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": '{"symbol":"btc/usd"}'
            }]
        }
        ws.send(json.dumps(msg))
        data.last_oracle_msg = "Subscribed to chainlink"
    
    def _on_message(self, ws, message):
        try:
            d = json.loads(message)
            data.last_oracle_msg = f"Msg: {d.get('type', '?')}"
            
            if d.get('type') == 'update':
                topic = d.get('topic', '')
                payload = d.get('payload', {})
                
                if topic == 'crypto_prices_chainlink':
                    symbol = payload.get('symbol', '')
                    if symbol == 'btc/usd':
                        price = float(payload.get('value', 0))
                        ts = int(payload.get('timestamp', 0))
                        data.set_oracle(price, ts)
                        data.last_oracle_msg = f"Oracle: ${price:,.0f}"
        except Exception as e:
            data.last_oracle_msg = f"Parse err: {e}"
    
    def _on_error(self, ws, error):
        data.last_oracle_msg = f"WS Error: {error}"
        self.connected = False
    
    def _on_close(self, ws, close_code, close_msg):
        data.last_oracle_msg = f"Closed: {close_code}"
        self.connected = False

# ============================================================
# 📡 WEBSOCKET: SPOT (Binance)
# ============================================================
class SpotWS:
    def __init__(self):
        self.connected = False
    
    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        "wss://stream.binance.com:9443/ws/btcusdt@trade",
                        on_message=self._on_message,
                        on_open=lambda ws: setattr(self, 'connected', True),
                        on_close=lambda ws, c, m: setattr(self, 'connected', False)
                    )
                    ws.run_forever(ping_interval=30)
                except:
                    pass
                time.sleep(2)
        
        threading.Thread(target=run, daemon=True).start()
    
    def _on_message(self, ws, message):
        try:
            d = json.loads(message)
            data.set_spot(float(d['p']), int(d['T']))
        except:
            pass

# ============================================================
# 📡 WEBSOCKET: CLOB (Polymarket Orderbook)
# ============================================================
class ClobWS:
    def __init__(self):
        self.ws = None
        self.subscribed_tokens: set = set()
        self.connected = False
    
    def start(self):
        def run():
            while True:
                try:
                    self.ws = websocket.WebSocketApp(
                        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close
                    )
                    self.ws.run_forever(ping_interval=30, ping_timeout=10)
                except Exception as e:
                    data.last_clob_msg = f"Error: {e}"
                time.sleep(2)
        
        threading.Thread(target=run, daemon=True).start()
    
    def _on_open(self, ws):
        self.connected = True
        self.subscribed_tokens.clear()
        data.last_clob_msg = "CLOB connected"
        # Don't subscribe here - wait for tokens
    
    def subscribe(self):
        """Subscribe to current market tokens"""
        if not self.ws or not self.connected:
            data.last_clob_msg = "Cannot subscribe - not connected"
            return
        
        if not market.up_token or not market.down_token:
            data.last_clob_msg = "Cannot subscribe - no tokens"
            return
        
        tokens_to_sub = []
        
        if market.up_token not in self.subscribed_tokens:
            tokens_to_sub.append(market.up_token)
        if market.down_token not in self.subscribed_tokens:
            tokens_to_sub.append(market.down_token)
        
        if not tokens_to_sub:
            data.last_clob_msg = "Already subscribed"
            return
        
        try:
            msg = {
                "type": "subscribe",
                "channel": "market",
                "assets_ids": tokens_to_sub
            }
            self.ws.send(json.dumps(msg))
            self.subscribed_tokens.update(tokens_to_sub)
            data.clob_subscribed = True
            data.last_clob_msg = f"Subscribed to {len(tokens_to_sub)} tokens"
        except Exception as e:
            data.last_clob_msg = f"Subscribe error: {e}"
    
    def clear(self):
        """Clear subscriptions for new window"""
        self.subscribed_tokens.clear()
        data.clob_subscribed = False
    
    def _on_message(self, ws, message):
        try:
            d = json.loads(message)
            evt = d.get('event_type', '')
            
            if evt == 'book':
                # Full orderbook snapshot
                asset = d.get('asset_id', '')
                bids = d.get('bids', [])
                asks = d.get('asks', [])
                
                if asset == market.up_token:
                    data.set_book('UP', bids, asks)
                    data.last_clob_msg = f"UP book: {len(bids)}b/{len(asks)}a"
                elif asset == market.down_token:
                    data.set_book('DOWN', bids, asks)
                    data.last_clob_msg = f"DOWN book: {len(bids)}b/{len(asks)}a"
            
            elif evt == 'price_change':
                # Incremental update
                for change in d.get('price_changes', []):
                    asset = change.get('asset_id', '')
                    price = change.get('price', '')
                    size = float(change.get('size', 0))
                    side = change.get('side', '')  # BUY or SELL
                    
                    if asset == market.up_token:
                        data.update_level('UP', price, size, side)
                    elif asset == market.down_token:
                        data.update_level('DOWN', price, size, side)
                
                data.last_clob_msg = f"Updated {len(d.get('price_changes', []))} levels"
        
        except Exception as e:
            data.last_clob_msg = f"Parse error: {e}"
    
    def _on_error(self, ws, error):
        data.last_clob_msg = f"CLOB error: {error}"
        self.connected = False
    
    def _on_close(self, ws, close_code, close_msg):
        data.last_clob_msg = f"CLOB closed: {close_code}"
        self.connected = False
        self.subscribed_tokens.clear()

clob_ws = ClobWS()

# ============================================================
# 📊 OPPORTUNITY DETECTION
# ============================================================
@dataclass
class Opportunity:
    side: str
    price: float
    size: float
    fair_value: float
    profit_pct: float
    distance: float
    time_left: float
    confidence: str

def calculate_fair_value(distance: float, time_sec: float) -> float:
    """Simple fair value based on distance and time"""
    if time_sec <= 0:
        return 1.0 if distance > 0 else 0.0
    
    abs_dist = abs(distance)
    
    # Estimate: BTC moves ~$30/min normally, up to $100 in volatile conditions
    vol_per_min = 40
    time_min = time_sec / 60
    max_move = vol_per_min * (time_min ** 0.5) * 3  # 3 sigma
    
    if abs_dist > max_move * 1.5:
        certainty = 0.99
    elif abs_dist > max_move:
        certainty = 0.96
    elif abs_dist > max_move * 0.7:
        certainty = 0.90
    elif abs_dist > max_move * 0.5:
        certainty = 0.80
    else:
        certainty = 0.65
    
    return certainty if distance > 0 else (1 - certainty)

def get_confidence(distance: float, time_sec: float) -> str:
    abs_dist = abs(distance)
    
    if time_sec < 30:
        if abs_dist > 40: return "ULTRA_SAFE"
        if abs_dist > 25: return "VERY_SAFE"
        if abs_dist > 15: return "SAFE"
    elif time_sec < 60:
        if abs_dist > 70: return "ULTRA_SAFE"
        if abs_dist > 50: return "VERY_SAFE"
        if abs_dist > 30: return "SAFE"
    elif time_sec < 120:
        if abs_dist > 100: return "ULTRA_SAFE"
        if abs_dist > 70: return "VERY_SAFE"
        if abs_dist > 50: return "SAFE"
    else:
        if abs_dist > 150: return "VERY_SAFE"
        if abs_dist > 100: return "SAFE"
    
    return "RISKY"

def scan_opportunity() -> Optional[Opportunity]:
    """Look for trading opportunity"""
    time_left = market.time_remaining()
    
    # Only scan in trading window
    if time_left > MAX_TIME_TO_TRADE:
        return None
    if time_left < 5:  # Too close to expiry
        return None
    
    # Need data
    if market.strike <= 0 or data.oracle <= 0:
        return None
    
    # Get winning side
    side, distance = market.winning_side()
    if side == "UNKNOWN":
        return None
    
    # Check minimum distance
    if distance < MIN_DISTANCE:
        return None
    
    # Get confidence
    conf = get_confidence(distance, time_left)
    if conf == "RISKY":
        return None
    
    # Get price
    price, size = data.best_ask(side)
    if price <= 0 or price > MAX_ENTRY_PRICE:
        return None
    
    # Calculate profit
    fv = calculate_fair_value(distance if side == "UP" else -distance, time_left)
    gross = 1.0 - price
    fee = 0.02 * gross  # 2% fee on profit
    net = gross - fee
    profit_pct = net / price
    
    if profit_pct < MIN_PROFIT:
        return None
    
    return Opportunity(
        side=side,
        price=price,
        size=size,
        fair_value=fv,
        profit_pct=profit_pct,
        distance=distance,
        time_left=time_left,
        confidence=conf
    )

# ============================================================
# 🎨 TERMINAL UI
# ============================================================
def format_time(sec: float) -> Text:
    m, s = int(sec // 60), int(sec % 60)
    if sec > 180:
        style = "dim"
    elif sec > 60:
        style = "yellow"
    elif sec > 30:
        style = "bold yellow"
    else:
        style = "bold red blink"
    return Text(f"{m:02d}:{s:02d}", style=style)

def render_status() -> Panel:
    c = Text()
    
    c.append("🎯 CERTAINTY HARVESTER\n\n", style="bold cyan")
    
    # Market info
    c.append(f"Window: ", style="dim")
    c.append(f"{market.slug}\n", style="white")
    
    c.append("Time: ", style="dim")
    c.append_text(format_time(market.time_remaining()))
    c.append("\n")
    
    # Strike
    if market.strike > 0:
        c.append(f"\nStrike: ${market.strike:,.0f}", style="cyan")
        c.append(f" ({market.strike_source})\n", style="dim")
    else:
        c.append("\nStrike: ", style="dim")
        c.append("NOT SET\n", style="red")
    
    # Oracle
    if data.oracle > 0:
        c.append(f"Oracle: ${data.oracle:,.0f}", style="white")
        lag = data.oracle_lag()
        if lag >= 0:
            lag_style = "green" if lag < 2 else "yellow" if lag < 5 else "red"
            c.append(f" ({lag:.1f}s)\n", style=lag_style)
        else:
            c.append("\n")
    else:
        c.append("Oracle: ", style="dim")
        c.append("waiting...\n", style="yellow")
    
    # Current status
    if market.strike > 0 and data.oracle > 0:
        side, dist = market.winning_side()
        if side == "UP":
            c.append(f"\n📈 UP +${dist:.0f}", style="bold green")
        else:
            c.append(f"\n📉 DOWN -${dist:.0f}", style="bold red")
    
    c.append(f"\n\n{market.status}", style="dim")
    if market.error:
        c.append(f"\n{market.error}", style="red")
    
    return Panel(c, title="STATUS", border_style="blue")

def render_prices() -> Panel:
    c = Text()
    
    # UP
    c.append("UP:\n", style="bold green")
    up_bid, up_bid_sz = data.best_bid('UP')
    up_ask, up_ask_sz = data.best_ask('UP')
    
    if up_bid > 0 or up_ask > 0:
        c.append(f"  Bid: ${up_bid:.4f} ({up_bid_sz:,.0f})\n", style="green")
        c.append(f"  Ask: ${up_ask:.4f} ({up_ask_sz:,.0f})\n", style="green")
    else:
        c.append("  No data\n", style="dim")
    
    # DOWN
    c.append("\nDOWN:\n", style="bold red")
    down_bid, down_bid_sz = data.best_bid('DOWN')
    down_ask, down_ask_sz = data.best_ask('DOWN')
    
    if down_bid > 0 or down_ask > 0:
        c.append(f"  Bid: ${down_bid:.4f} ({down_bid_sz:,.0f})\n", style="red")
        c.append(f"  Ask: ${down_ask:.4f} ({down_ask_sz:,.0f})\n", style="red")
    else:
        c.append("  No data\n", style="dim")
    
    # Sum check
    if up_ask > 0 and down_ask > 0:
        total = up_ask + down_ask
        c.append(f"\nSum: ${total:.4f}", style="white")
        if total < 0.995:
            c.append(" ARB!", style="bold green")
    
    return Panel(c, title="ORDERBOOK", border_style="yellow")

def render_opportunity(opp: Optional[Opportunity]) -> Panel:
    time_left = market.time_remaining()
    
    if time_left > MAX_TIME_TO_TRADE:
        c = Text()
        c.append("⏳ WAITING\n\n", style="dim")
        c.append("Scanning starts in last 3 minutes\n", style="dim")
        c.append(f"Current: ", style="dim")
        c.append_text(format_time(time_left))
        return Panel(c, title="OPPORTUNITY", border_style="dim")
    
    if not opp:
        c = Text()
        c.append("🔍 SCANNING...\n\n", style="yellow")
        
        # Show what's missing
        if market.strike <= 0:
            c.append("❌ No strike price\n", style="red")
        elif data.oracle <= 0:
            c.append("❌ No oracle price\n", style="red")
        else:
            side, dist = market.winning_side()
            if side != "UNKNOWN":
                if dist < MIN_DISTANCE:
                    c.append(f"⚠ Distance ${dist:.0f} < ${MIN_DISTANCE} min\n", style="yellow")
                else:
                    price, _ = data.best_ask(side)
                    if price <= 0:
                        c.append(f"❌ No {side} ask price\n", style="red")
                    elif price > MAX_ENTRY_PRICE:
                        c.append(f"⚠ {side} price ${price:.4f} > ${MAX_ENTRY_PRICE}\n", style="yellow")
        
        return Panel(c, title="OPPORTUNITY", border_style="yellow")
    
    # We have an opportunity!
    c = Text()
    c.append("🚨 OPPORTUNITY!\n\n", style="bold green")
    
    side_style = "bold green" if opp.side == "UP" else "bold red"
    c.append("Side: ", style="white")
    c.append(f"{opp.side}\n", style=side_style)
    
    c.append(f"Price: ${opp.price:.4f}\n", style="white")
    c.append(f"Size: {opp.size:,.0f}\n", style="dim")
    
    c.append(f"\nFair Value: ${opp.fair_value:.3f}\n", style="cyan")
    
    profit_style = "bold green" if opp.profit_pct > 0.03 else "green"
    c.append("Profit: ", style="white")
    c.append(f"{opp.profit_pct:.1%}\n", style=profit_style)
    
    conf_style = "bold green" if opp.confidence == "ULTRA_SAFE" else "green" if opp.confidence == "VERY_SAFE" else "yellow"
    c.append("Confidence: ", style="white")
    c.append(f"{opp.confidence}\n", style=conf_style)
    
    c.append(f"\nDistance: ${opp.distance:.0f}\n", style="dim")
    c.append(f"Time: {opp.time_left:.0f}s\n", style="dim")
    
    border = "bold green" if opp.confidence in ["ULTRA_SAFE", "VERY_SAFE"] else "yellow"
    return Panel(c, title="🎯 TRADE", border_style=border)

def render_debug() -> Panel:
    c = Text()
    
    # Connections
    c.append("Connections:\n", style="bold white")
    c.append("  Oracle: ", style="dim")
    c.append("●" if data.oracle_connected else "○", style="green" if data.oracle_connected else "red")
    c.append("\n")
    
    c.append("  Spot: ", style="dim")
    c.append("●" if data.spot_connected else "○", style="green" if data.spot_connected else "red")
    c.append("\n")
    
    c.append("  CLOB: ", style="dim")
    c.append("●" if data.clob_connected else "○", style="green" if data.clob_connected else "red")
    c.append("\n")
    
    c.append("  Subscribed: ", style="dim")
    c.append("●" if data.clob_subscribed else "○", style="green" if data.clob_subscribed else "red")
    c.append("\n")
    
    # Tokens
    c.append(f"\nTokens:\n", style="bold white")
    if market.up_token:
        c.append(f"  UP: {market.up_token[:20]}...\n", style="dim")
    else:
        c.append("  UP: none\n", style="red")
    if market.down_token:
        c.append(f"  DN: {market.down_token[:20]}...\n", style="dim")
    else:
        c.append("  DN: none\n", style="red")
    
    # Messages
    c.append(f"\nMsgs: {data.msg_count}\n", style="dim")
    c.append(f"Oracle: {data.last_oracle_msg}\n", style="dim")
    c.append(f"CLOB: {data.last_clob_msg}\n", style="dim")
    
    return Panel(c, title="DEBUG", border_style="dim")

def build_dashboard() -> Layout:
    layout = Layout()
    
    # Lifecycle & subscription logic
    new_window = market.check_new_window()
    if new_window:
        clob_ws.clear()
    
    # Fetch tokens if needed
    if not market.up_token:
        market.fetch_tokens()
    
    # Subscribe to orderbook if we have tokens but not subscribed
    if market.up_token and not data.clob_subscribed and clob_ws.connected:
        clob_ws.subscribe()
    
    # Scan for opportunity
    opp = scan_opportunity()
    
    # Build layout
    header = Text()
    header.append("⚡ CERTAINTY HARVESTER ", style="bold cyan")
    header.append(datetime.now().strftime("%H:%M:%S"), style="dim")
    
    layout.split(
        Layout(Panel(header, box=box.MINIMAL), size=3),
        Layout(name="body")
    )
    
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right")
    )
    
    layout["left"].split(
        Layout(render_status()),
        Layout(render_debug())
    )
    
    layout["right"].split(
        Layout(render_prices()),
        Layout(render_opportunity(opp))
    )
    
    return layout

# ============================================================
# 🚀 MAIN
# ============================================================
def main():
    console = Console()
    
    console.print("\n[bold cyan]⚡ CERTAINTY HARVESTER[/bold cyan]")
    console.print("[dim]Buy certainty in the last 3 minutes, collect 2-4% profit[/dim]\n")
    
    # Initialize market
    market.window_start = market.current_window_start()
    market.slug = f"btc-updown-15m-{market.window_start}"
    
    # Start WebSockets
    console.print("[yellow]Starting connections...[/yellow]")
    
    oracle_ws = OracleWS()
    oracle_ws.start()
    console.print("  [green]✓[/green] Oracle WebSocket")
    
    spot_ws = SpotWS()
    spot_ws.start()
    console.print("  [green]✓[/green] Spot WebSocket")
    
    clob_ws.start()
    console.print("  [green]✓[/green] CLOB WebSocket")
    
    # Wait for initial data
    console.print("\n[yellow]Waiting for data...[/yellow]")
    for i in range(10):
        time.sleep(0.5)
        if data.oracle > 0:
            console.print(f"  [green]✓[/green] Oracle: ${data.oracle:,.0f}")
            break
    else:
        console.print("  [yellow]⚠[/yellow] Oracle not yet received")
    
    # Fetch tokens
    console.print("\n[yellow]Fetching market...[/yellow]")
    if market.fetch_tokens():
        console.print(f"  [green]✓[/green] Tokens loaded")
        console.print(f"  [dim]UP: {market.up_token[:30]}...[/dim]")
        console.print(f"  [dim]DN: {market.down_token[:30]}...[/dim]")
    else:
        console.print(f"  [red]✗[/red] Failed: {market.error}")
    
    # Check if we're mid-window and need strike
    elapsed = market.time_elapsed()
    if elapsed > 5 and market.strike == 0:
        console.print("\n[yellow]Joined mid-window, strike not captured.[/yellow]")
        
        # Try to get from API
        if market.strike > 0:
            console.print(f"  [green]✓[/green] Got from API: ${market.strike:,.0f}")
        else:
            # Ask user
            strike_input = Prompt.ask(
                "[cyan]Enter strike price (or press Enter to skip)[/cyan]",
                default=""
            )
            if strike_input:
                try:
                    market.set_strike_from_user(float(strike_input.replace(',', '').replace('$', '')))
                    console.print(f"  [green]✓[/green] Strike set: ${market.strike:,.0f}")
                except:
                    console.print("  [red]✗[/red] Invalid input")
    
    # Give time for CLOB subscription
    console.print("\n[yellow]Subscribing to orderbook...[/yellow]")
    time.sleep(2)
    
    if data.clob_subscribed:
        console.print("  [green]✓[/green] Subscribed")
    else:
        console.print("  [yellow]⚠[/yellow] Will subscribe when connected")
    
    console.print("\n[bold green]Starting terminal...[/bold green]")
    time.sleep(1)
    
    # Main loop
    try:
        with Live(build_dashboard(), refresh_per_second=10, screen=True) as live:
            while True:
                live.update(build_dashboard())
                time.sleep(0.1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")

if __name__ == "__main__":
    main()