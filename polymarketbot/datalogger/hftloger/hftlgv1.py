# Event-Based Polymarket Data Logger - FIXED VERSION
# All issues resolved: RTDS prices visible, orderbook working, unlimited events

import asyncio
import websockets
import requests
import json
import time
import csv
import os
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

@dataclass
class EventMetadata:
    """Metadata for each market event"""
    slug: str
    window_start: int
    window_end: int
    strike_price: Optional[float]
    up_token_id: Optional[str]
    down_token_id: Optional[str]
    start_datetime: str
    end_datetime: str

class ConnectionStatus:
    """Track WebSocket connection status"""
    def __init__(self):
        self.binance = False
        self.rtds = False
        self.clob = False

    def __str__(self):
        b = "🟢" if self.binance else "🔴"
        r = "🟢" if self.rtds else "🔴"
        c = "🟢" if self.clob else "🔴"
        return f"Binance:{b} RTDS:{r} CLOB:{c}"

class EventBasedLogger:
    """
    FIXED: All issues resolved
    - Shows RTDS prices in console
    - Clear Binance Spot label
    - Only captures strike for events starting AFTER script launch
    - Unlimited events by default
    - Orderbook display working
    - Connection status visible
    """

    def __init__(self, 
                 output_dir='polymarket_events',
                 max_events=None,  # FIXED: Default unlimited
                 show_upcoming=10):

        self.output_dir = output_dir
        self.max_events = max_events
        self.show_upcoming = show_upcoming

        # Create output directory
        Path(output_dir).mkdir(exist_ok=True)

        # Current event
        self.current_event: Optional[EventMetadata] = None
        self.event_folder = None
        self.events_captured = 0

        # FIXED: Track when script started (for strike capture)
        self.script_start_time = int(time.time())
        self.first_window_start = None

        # Current prices
        self.binance_spot = None
        self.rtds_binance = None
        self.rtds_chainlink = None

        # Current orderbooks
        self.up_best_bid = None
        self.up_best_ask = None
        self.up_bid_size = None
        self.up_ask_size = None
        self.down_best_bid = None
        self.down_best_ask = None
        self.down_bid_size = None
        self.down_ask_size = None

        # Event counters
        self.event_tick_count = 0
        self.orderbook_up_count = 0
        self.orderbook_down_count = 0

        # FIXED: Connection status tracking
        self.connection_status = ConnectionStatus()

        # WebSocket connection tracking
        self.clob_ws = None
        self.should_reconnect_clob = False

        # Global prices file (continuous)
        self.init_global_prices_file()

    def init_global_prices_file(self):
        """Create global prices CSV (spans all events)"""
        filepath = f"{self.output_dir}/global_prices.csv"
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp_us',
                'datetime',
                'slug',
                'binance_spot',
                'rtds_binance',
                'rtds_chainlink',
                'oracle_lag',
                'rtds_lag'
            ])

    def get_upcoming_slugs(self, count=10):
        """Generate upcoming event slugs"""
        now = int(time.time())
        current_window = (now // 900) * 900

        # Start from NEXT window if we're in progress
        if now - current_window > 60:  # More than 1 minute into current window
            current_window += 900

        slugs = []
        for i in range(count):
            window_start = current_window + (i * 900)
            window_end = window_start + 900
            slug = f"btc-updown-15m-{window_start}"

            slugs.append({
                'slug': slug,
                'window_start': window_start,
                'window_end': window_end,
                'start_time': datetime.fromtimestamp(window_start).strftime('%H:%M:%S'),
                'end_time': datetime.fromtimestamp(window_end).strftime('%H:%M:%S'),
                'starts_in': window_start - now
            })

        return slugs

    def print_upcoming_events(self):
        """Print table of upcoming events"""
        slugs = self.get_upcoming_slugs(self.show_upcoming)

        print("\n" + "="*110)
        print("UPCOMING EVENTS (Will capture starting from Event #1)")
        print("="*110)
        print(f"{'#':<4} {'Start Time':<12} {'End Time':<12} {'Starts In':<15} {'Slug':<50}")
        print("-"*110)

        for i, event in enumerate(slugs, 1):
            starts_in = event['starts_in']
            if starts_in < 0:
                starts_str = "IN PROGRESS"
            elif starts_in < 60:
                starts_str = f"{starts_in}s"
            elif starts_in < 3600:
                starts_str = f"{starts_in//60}m {starts_in%60}s"
            else:
                starts_str = f"{starts_in//3600}h {(starts_in%3600)//60}m"

            print(f"{i:<4} {event['start_time']:<12} {event['end_time']:<12} {starts_str:<15} {event['slug']:<50}")

        print("="*110)

    def create_event_folder(self, slug):
        """Create folder for new event"""
        self.event_folder = f"{self.output_dir}/{slug}"
        Path(self.event_folder).mkdir(exist_ok=True)

        # Create CSV files for this event
        self._init_event_csv('orderbook_up', [
            'timestamp_us', 'datetime', 'seconds_remaining',
            'best_bid', 'best_ask', 'bid_size', 'ask_size', 'spread', 'mid'
        ])

        self._init_event_csv('orderbook_down', [
            'timestamp_us', 'datetime', 'seconds_remaining',
            'best_bid', 'best_ask', 'bid_size', 'ask_size', 'spread', 'mid'
        ])

        self._init_event_csv('prices', [
            'timestamp_us', 'datetime', 'seconds_remaining',
            'binance_spot', 'rtds_binance', 'rtds_chainlink',
            'oracle_lag', 'rtds_lag'
        ])

        self._init_event_csv('lags', [
            'timestamp_us', 'datetime', 'seconds_remaining',
            'event_type', 'oracle_lag', 'up_mid', 'down_mid', 'market_sum'
        ])

        print(f"\n✅ Created event folder: {self.event_folder}")

    def _init_event_csv(self, name, headers):
        """Initialize CSV file for event"""
        filepath = f"{self.event_folder}/{name}.csv"
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(headers)

    def save_event_metadata(self):
        """Save event metadata as JSON"""
        if not self.current_event:
            return

        metadata = {
            'slug': self.current_event.slug,
            'window_start': self.current_event.window_start,
            'window_end': self.current_event.window_end,
            'strike_price': self.current_event.strike_price,
            'up_token_id': self.current_event.up_token_id,
            'down_token_id': self.current_event.down_token_id,
            'start_datetime': self.current_event.start_datetime,
            'end_datetime': self.current_event.end_datetime,
            'total_ticks_recorded': self.event_tick_count,
            'orderbook_up_updates': self.orderbook_up_count,
            'orderbook_down_updates': self.orderbook_down_count
        }

        filepath = f"{self.event_folder}/metadata.json"
        with open(filepath, 'w') as f:
            json.dump(metadata, f, indent=2)

    def get_timestamp_us(self):
        """Get microsecond timestamp"""
        return int(time.time() * 1_000_000)

    def get_datetime_str(self):
        """Get datetime with milliseconds"""
        now = datetime.now()
        return now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

    def get_seconds_remaining(self):
        """Get seconds until event ends"""
        if not self.current_event:
            return None
        now = int(time.time())
        return self.current_event.window_end - now

    def log_to_event_csv(self, filename, data):
        """Append data to event CSV"""
        if not self.event_folder:
            return

        filepath = f"{self.event_folder}/{filename}.csv"
        with open(filepath, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(data)

    def log_to_global_prices(self):
        """Log to global prices file"""
        oracle_lag = None
        rtds_lag = None

        if self.binance_spot and self.rtds_chainlink:
            oracle_lag = self.binance_spot - self.rtds_chainlink

        if self.rtds_binance and self.rtds_chainlink:
            rtds_lag = self.rtds_binance - self.rtds_chainlink

        filepath = f"{self.output_dir}/global_prices.csv"
        with open(filepath, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                self.get_timestamp_us(),
                self.get_datetime_str(),
                self.current_event.slug if self.current_event else None,
                self.binance_spot,
                self.rtds_binance,
                self.rtds_chainlink,
                oracle_lag,
                rtds_lag
            ])

    def log_orderbook_update(self, side, best_bid, best_ask, bid_size, ask_size):
        """Log orderbook update"""
        self.event_tick_count += 1

        spread = (best_ask - best_bid) if (best_ask and best_bid) else None
        mid = ((best_ask + best_bid) / 2) if (best_ask and best_bid) else None

        # Update state
        if side == 'UP':
            self.up_best_bid = best_bid
            self.up_best_ask = best_ask
            self.up_bid_size = bid_size
            self.up_ask_size = ask_size
            self.orderbook_up_count += 1
            filename = 'orderbook_up'
        else:
            self.down_best_bid = best_bid
            self.down_best_ask = best_ask
            self.down_bid_size = bid_size
            self.down_ask_size = ask_size
            self.orderbook_down_count += 1
            filename = 'orderbook_down'

        # Log to event CSV
        self.log_to_event_csv(filename, [
            self.get_timestamp_us(),
            self.get_datetime_str(),
            self.get_seconds_remaining(),
            best_bid,
            best_ask,
            bid_size,
            ask_size,
            spread,
            mid
        ])

        # Log lag event
        self.log_lag_event(f'orderbook_{side.lower()}')

    def log_price_update(self, event_type):
        """Log price update"""
        self.event_tick_count += 1

        oracle_lag = None
        rtds_lag = None

        if self.binance_spot and self.rtds_chainlink:
            oracle_lag = self.binance_spot - self.rtds_chainlink

        if self.rtds_binance and self.rtds_chainlink:
            rtds_lag = self.rtds_binance - self.rtds_chainlink

        # Log to event prices CSV
        self.log_to_event_csv('prices', [
            self.get_timestamp_us(),
            self.get_datetime_str(),
            self.get_seconds_remaining(),
            self.binance_spot,
            self.rtds_binance,
            self.rtds_chainlink,
            oracle_lag,
            rtds_lag
        ])

        # Log to global prices
        self.log_to_global_prices()

        # Log lag event
        self.log_lag_event(event_type)

    def log_lag_event(self, event_type):
        """Log lag computation"""
        oracle_lag = None
        if self.binance_spot and self.rtds_chainlink:
            oracle_lag = self.binance_spot - self.rtds_chainlink

        up_mid = None
        down_mid = None
        market_sum = None

        if self.up_best_bid and self.up_best_ask:
            up_mid = (self.up_best_bid + self.up_best_ask) / 2

        if self.down_best_bid and self.down_best_ask:
            down_mid = (self.down_best_bid + self.down_best_ask) / 2

        if up_mid and down_mid:
            market_sum = up_mid + down_mid

        self.log_to_event_csv('lags', [
            self.get_timestamp_us(),
            self.get_datetime_str(),
            self.get_seconds_remaining(),
            event_type,
            oracle_lag,
            up_mid,
            down_mid,
            market_sum
        ])

    def print_live_dashboard(self):
        """FIXED: Print live data dashboard with ALL prices"""
        if not self.current_event:
            return

        # Calculate values
        oracle_lag = None
        rtds_lag = None

        if self.binance_spot and self.rtds_chainlink:
            oracle_lag = self.binance_spot - self.rtds_chainlink

        if self.rtds_binance and self.rtds_chainlink:
            rtds_lag = self.rtds_binance - self.rtds_chainlink

        up_mid = None
        if self.up_best_bid and self.up_best_ask:
            up_mid = (self.up_best_bid + self.up_best_ask) / 2

        down_mid = None
        if self.down_best_bid and self.down_best_ask:
            down_mid = (self.down_best_bid + self.down_best_ask) / 2

        market_sum = None
        if up_mid and down_mid:
            market_sum = up_mid + down_mid

        seconds_remaining = self.get_seconds_remaining()

        # Build dashboard (FIXED: Shows all prices and connection status)
        print(f"\r", end="")

        # Line 1: Event info
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ", end="")
        print(f"Event: {self.events_captured}", end="")
        if self.max_events:
            print(f"/{self.max_events}", end="")
        print(f" | Time: {seconds_remaining:>3}s | ", end="")
        print(f"Ticks: {self.event_tick_count:>5} | ", end="")
        print(f"Conn: {self.connection_status} || ", end="")

        # Line 1 continued: Prices
        print(f"SPOT: ${self.binance_spot:>9,.2f} " if self.binance_spot else "SPOT: WAITING ", end="")
        print(f"| RTDS-B: ${self.rtds_binance:>9,.2f} " if self.rtds_binance else "| RTDS-B: WAITING ", end="")
        print(f"| RTDS-C: ${self.rtds_chainlink:>9,.2f} " if self.rtds_chainlink else "| RTDS-C: WAITING ", end="")
        print(f"| Lag: ${oracle_lag:>+6,.0f} " if oracle_lag else "| Lag: N/A ", end="")

        # Line 1 continued: Orderbook
        print(f"|| UP: {up_mid:.4f} ({self.orderbook_up_count:>4}) " if up_mid else "|| UP: WAITING ", end="")
        print(f"| DOWN: {down_mid:.4f} ({self.orderbook_down_count:>4}) " if down_mid else "| DOWN: WAITING ", end="")
        print(f"| Sum: {market_sum:.4f} " if market_sum else "| Sum: N/A ", end="")

        # Strike on separate line if not captured yet
        if not self.current_event.strike_price:
            print(f"|| 🔒 STRIKE: WAITING...", end="")

    async def fetch_token_ids(self, slug):
        """Fetch token IDs from Gamma API"""
        print(f"\n⏳ Fetching token IDs for {slug}...")

        for attempt in range(10):  # FIXED: More retries
            try:
                url = f"https://gamma-api.polymarket.com/events?slug={slug}"
                response = requests.get(url, timeout=10)

                if response.status_code == 200 and response.json():
                    data = response.json()
                    if data and len(data) > 0:
                        market = data[0]['markets'][0]
                        token_ids = json.loads(market['clobTokenIds'])
                        outcomes = json.loads(market['outcomes'])

                        if outcomes[0].upper() in ['UP', 'YES']:
                            print(f"✅ Tokens fetched (attempt {attempt+1})")
                            return token_ids[0], token_ids[1]
                        else:
                            print(f"✅ Tokens fetched (attempt {attempt+1})")
                            return token_ids[1], token_ids[0]
            except Exception as e:
                print(f"   Retry {attempt+1}/10: {str(e)[:50]}")

            await asyncio.sleep(10)

        print(f"❌ Failed to fetch tokens after 10 attempts")
        return None, None

    async def handle_new_event(self, slug, window_start):
        """Handle transition to new event"""
        # Save previous event metadata
        if self.current_event:
            self.save_event_metadata()
            print(f"\n\n{'='*110}")
            print(f"✅ Event complete: {self.current_event.slug}")
            print(f"   Ticks recorded: {self.event_tick_count:,}")
            print(f"   Orderbook UP updates: {self.orderbook_up_count:,}")
            print(f"   Orderbook DOWN updates: {self.orderbook_down_count:,}")
            print(f"{'='*110}")

        # Check if we should stop
        if self.max_events and self.events_captured >= self.max_events:
            print(f"\n\n{'='*110}")
            print(f"REACHED MAX EVENTS: {self.max_events}")
            print(f"{'='*110}")
            print("\nShutting down...")
            os._exit(0)

        # Increment counter
        self.events_captured += 1
        self.event_tick_count = 0
        self.orderbook_up_count = 0
        self.orderbook_down_count = 0

        # Reset orderbook state
        self.up_best_bid = None
        self.up_best_ask = None
        self.down_best_bid = None
        self.down_best_ask = None

        # Create new event
        window_end = window_start + 900

        self.current_event = EventMetadata(
            slug=slug,
            window_start=window_start,
            window_end=window_end,
            strike_price=None,
            up_token_id=None,
            down_token_id=None,
            start_datetime=datetime.fromtimestamp(window_start).strftime('%Y-%m-%d %H:%M:%S'),
            end_datetime=datetime.fromtimestamp(window_end).strftime('%Y-%m-%d %H:%M:%S')
        )

        # Create folder
        self.create_event_folder(slug)

        # Fetch token IDs
        up_token, down_token = await self.fetch_token_ids(slug)

        if up_token and down_token:
            self.current_event.up_token_id = up_token
            self.current_event.down_token_id = down_token
            print(f"✅ UP Token: {up_token[:16]}...")
            print(f"✅ DOWN Token: {down_token[:16]}...")

            # Signal CLOB to reconnect with new tokens
            self.should_reconnect_clob = True
        else:
            print(f"❌ Failed to fetch token IDs - orderbook will not work")

        print(f"\n📊 LIVE DASHBOARD (Event {self.events_captured})")
        print("-"*110)

        return True

    async def monitor_events(self):
        """Monitor and transition between events"""
        # FIXED: Wait for first NEW window to start
        now = int(time.time())
        current_window = (now // 900) * 900

        # If we're more than 30 seconds into current window, wait for next
        if now - current_window > 30:
            next_window = current_window + 900
            wait_time = next_window - now
            print(f"\n⏳ Waiting {wait_time}s for next clean window to start...")
            await asyncio.sleep(wait_time + 2)  # Wait 2 extra seconds

        # Now start capturing
        current_window = 0

        while True:
            now = int(time.time())
            window_start = (now // 900) * 900
            slug = f"btc-updown-15m-{window_start}"

            # FIXED: Only capture NEW windows
            if window_start != current_window:
                current_window = window_start

                # Handle new event
                should_continue = await self.handle_new_event(slug, window_start)

                if not should_continue:
                    os._exit(0)

            await asyncio.sleep(1)

    async def monitor_binance(self):
        """Monitor Binance spot prices"""
        while True:
            try:
                async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade") as ws:
                    self.connection_status.binance = True

                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)

                        if 'p' in data:
                            self.binance_spot = float(data['p'])

                            if self.current_event:
                                self.log_price_update('binance_spot')

            except Exception as e:
                self.connection_status.binance = False
                await asyncio.sleep(5)

    async def monitor_rtds(self):
        """Monitor RTDS feeds"""
        # FIXED: Track first window for strike capture
        first_chainlink_window = None

        while True:
            try:
                async with websockets.connect("wss://ws-live-data.polymarket.com") as ws:
                    self.connection_status.rtds = True

                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "crypto_prices",
                                "type": "*",
                                "filters": "{\"symbol\":\"btcusdt\"}"
                            },
                            {
                                "topic": "crypto_prices_chainlink",
                                "type": "*",
                                "filters": "{\"symbol\":\"btc/usd\"}"
                            }
                        ]
                    }))

                    while True:
                        msg = await ws.recv()

                        try:
                            data = json.loads(msg)
                        except:
                            continue

                        # RTDS Binance
                        if data.get('topic') == 'crypto_prices' and data.get('type') == 'update':
                            self.rtds_binance = float(data['payload']['value'])

                            if self.current_event:
                                self.log_price_update('rtds_binance')

                        # RTDS Chainlink (strike capture)
                        elif data.get('topic') == 'crypto_prices_chainlink' and data.get('type') == 'update':
                            price = float(data['payload']['value'])
                            self.rtds_chainlink = price

                            # FIXED: Only capture strike for events we're tracking
                            now = int(time.time())
                            chainlink_window = (now // 900) * 900

                            # First time seeing this window?
                            if first_chainlink_window != chainlink_window:
                                first_chainlink_window = chainlink_window

                                # Is this OUR current event and we don't have strike yet?
                                if (self.current_event and 
                                    self.current_event.window_start == chainlink_window and
                                    self.current_event.strike_price is None):

                                    self.current_event.strike_price = price
                                    print(f"\n🔒 STRIKE CAPTURED: ${price:,.2f}")
                                    self.save_event_metadata()

                            if self.current_event:
                                self.log_price_update('rtds_chainlink')

            except Exception as e:
                self.connection_status.rtds = False
                await asyncio.sleep(5)

    async def monitor_orderbook(self):
        """FIXED: Monitor CLOB orderbook with better debugging"""
        while True:
            # Wait for token IDs
            while not self.current_event or not self.current_event.up_token_id:
                await asyncio.sleep(1)

            try:
                async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market") as ws:
                    self.connection_status.clob = True

                    # Subscribe to current tokens
                    subscribe_msg = {
                        "type": "market",
                        "assets_ids": [
                            self.current_event.up_token_id,
                            self.current_event.down_token_id
                        ]
                    }

                    await ws.send(json.dumps(subscribe_msg))
                    print(f"\n✅ CLOB subscribed to orderbook")

                    while True:
                        # Check if we need to reconnect (new event)
                        if self.should_reconnect_clob:
                            self.should_reconnect_clob = False
                            print(f"\n🔄 Reconnecting CLOB for new event...")
                            break

                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            # Send ping
                            await ws.ping()
                            continue

                        # Parse message
                        try:
                            data = json.loads(msg)
                        except:
                            continue

                        # FIXED: Handle list or dict
                        messages = data if isinstance(data, list) else [data]

                        for item in messages:
                            if not isinstance(item, dict):
                                continue

                            event_type = item.get('event_type')

                            # FIXED: Handle 'book' messages
                            if event_type == 'book':
                                asset_id = item.get('asset_id')
                                bids = item.get('bids', [])
                                asks = item.get('asks', [])

                                if bids and asks and self.current_event:
                                    try:
                                        best_bid = float(bids[0]['price'])
                                        best_ask = float(asks[0]['price'])
                                        bid_size = float(bids[0]['size'])
                                        ask_size = float(asks[0]['size'])

                                        if asset_id == self.current_event.up_token_id:
                                            self.log_orderbook_update('UP', best_bid, best_ask, bid_size, ask_size)
                                        elif asset_id == self.current_event.down_token_id:
                                            self.log_orderbook_update('DOWN', best_bid, best_ask, bid_size, ask_size)
                                    except (KeyError, ValueError, IndexError) as e:
                                        pass  # Skip malformed messages

            except Exception as e:
                self.connection_status.clob = False
                print(f"\n⚠️  CLOB connection lost: {str(e)[:50]}")
                await asyncio.sleep(5)

    async def live_dashboard_loop(self):
        """Print live dashboard continuously"""
        while True:
            if self.current_event:
                self.print_live_dashboard()
            await asyncio.sleep(0.3)  # Update 3x per second

    async def run(self):
        """Start the logger"""
        print("\n" + "="*110)
        print("EVENT-BASED POLYMARKET DATA LOGGER (FIXED VERSION)")
        print("="*110)
        print(f"Output Directory: {self.output_dir}/")
        print(f"Max Events: {self.max_events if self.max_events else 'Unlimited (run until Ctrl+C)'}")
        print(f"Recording Method: Event-driven (WebSocket only - NO REST)")
        print(f"Timestamp Precision: Microseconds")
        print(f"Data Source: Binance Spot (btcusdt@trade)")

        # Show upcoming events
        self.print_upcoming_events()

        print("\n⏳ Waiting for next clean event to start...")
        print("   (Will capture strike price at window start)")
        print("\n" + "="*110)

        # Start all monitors
        await asyncio.gather(
            self.monitor_events(),
            self.monitor_binance(),
            self.monitor_rtds(),
            self.monitor_orderbook(),
            self.live_dashboard_loop()
        )

# Run
async def main():
    logger = EventBasedLogger(
        output_dir='polymarket_events',
        max_events=None,  # FIXED: Unlimited by default
        show_upcoming=10
    )
    await logger.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nShutdown by user (Ctrl+C)")
