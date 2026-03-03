#!/usr/bin/env python3
"""
Polymarket BTC 15m Up/Down Terminal
- Auto-rolls BTC 15m window (btc-updown-15m-<start_ts>)
- Shows order book for UP/DOWN (CLOB WebSocket)
- Shows Binance futures/spot, Poly relay, Chainlink oracle + latencies
- Shows question, slug, remaining time, strike, and UP/DOWN status
"""

import json
import re
import sys
import threading
import time
from datetime import datetime, timezone

import requests
import websocket
from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ==============================
# ⚙️ CONFIG
# ==============================
WINDOW_SECONDS = 900          # 15 minutes
REFRESH_RATE = 0.1            # 10 FPS for UI
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_URL = "wss://ws-live-data.polymarket.com"
BINANCE_FUT_WS = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
BINANCE_SPOT_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"

console = Console()

# ==============================
# 🧱 DATA CORE
# ==============================
class DataCore:
    def __init__(self):
        # Binance futures
        self.futures_price = 0.0
        self.futures_ts = 0

        # Binance spot
        self.spot_price = 0.0
        self.spot_ts = 0

        # Polymarket relay (Binance spot mirrored)
        self.poly_relay_price = 0.0
        self.poly_relay_ts = 0

        # Chainlink oracle (Polymarket reference)
        self.oracle_price = 0.0
        self.oracle_ts = 0

        self.lock = threading.Lock()

    def update_futures(self, price, ts):
        with self.lock:
            self.futures_price = float(price)
            self.futures_ts = int(ts)

    def update_spot(self, price, ts):
        with self.lock:
            self.spot_price = float(price)
            self.spot_ts = int(ts)

    def update_relay(self, price, ts):
        with self.lock:
            self.poly_relay_price = float(price)
            self.poly_relay_ts = int(ts)

    def update_oracle(self, price, ts):
        with self.lock:
            self.oracle_price = float(price)
            self.oracle_ts = int(ts)

core = DataCore()

# ==============================
# 📡 BINANCE CLIENTS
# ==============================
class BinanceFuturesClient:
    """Binance USD‑M Futures: BTCUSDT aggTrade"""
    def __init__(self, data_core: DataCore):
        self.core = data_core
        self.url = BINANCE_FUT_WS

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        self.url,
                        on_message=lambda ws, msg: self._on_msg(msg)
                    )
                    ws.run_forever()
                except Exception:
                    time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _on_msg(self, msg: str):
        try:
            d = json.loads(msg)
            self.core.update_futures(d["p"], d["T"])
        except Exception:
            pass


class BinanceSpotClient:
    """Binance Spot: BTCUSDT trade"""
    def __init__(self, data_core: DataCore):
        self.core = data_core
        self.url = BINANCE_SPOT_WS

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        self.url,
                        on_message=lambda ws, msg: self._on_msg(msg)
                    )
                    ws.run_forever()
                except Exception:
                    time.sleep(1)
        threading.Thread(target=run, daemon=True).start()

    def _on_msg(self, msg: str):
        try:
            d = json.loads(msg)
            self.core.update_spot(d["p"], d["T"])
        except Exception:
            pass

# ==============================
# 📡 POLYMARKET RTDS CLIENT
# ==============================
class PolyDataClient:
    """Polymarket RTDS for relay (btcusdt) and Chainlink (btc/usd)."""
    def __init__(self, data_core: DataCore):
        self.core = data_core
        self.url = RTDS_URL

    def start(self):
        def on_open(ws):
            try:
                # Relay (Binance spot mirrored)
                sub1 = {
                    "action": "subscribe",
                    "subscriptions": [{
                        "topic": "crypto_prices",
                        "type": "*",
                        "filters": "{\"symbol\":\"btcusdt\"}"
                    }]
                }
                ws.send(json.dumps(sub1))
                # Chainlink oracle
                sub2 = {
                    "action": "subscribe",
                    "subscriptions": [{
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": "{\"symbol\":\"btc/usd\"}"
                    }]
                }
                ws.send(json.dumps(sub2))
            except Exception:
                pass

        def on_msg(ws, msg: str):
            try:
                d = json.loads(msg)
                if d.get("type") != "update":
                    return
                topic = d.get("topic")
                payload = d.get("payload", {})
                symbol = payload.get("symbol")
                val = payload.get("value")
                ts = payload.get("timestamp")
                if val is None or ts is None:
                    return

                if topic == "crypto_prices" and symbol == "btcusdt":
                    self.core.update_relay(val, ts)
                elif topic == "crypto_prices_chainlink" and symbol == "btc/usd":
                    self.core.update_oracle(val, ts)
            except Exception:
                pass

        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        self.url,
                        on_open=on_open,
                        on_message=on_msg,
                    )
                    ws.run_forever()
                except Exception:
                    time.sleep(1)

        threading.Thread(target=run, daemon=True).start()

# ==============================
# 🧠 MARKET MANAGER (BTC 15m)
# ==============================
class MarketManager:
    """
    Tracks the current BTC 15m up/down market by slug:
    btc-updown-15m-<window_start_unix>
    Uses Gamma events?slug endpoint to get clobTokenIds, question, etc.
    """
    def __init__(self, data_core: DataCore):
        self.core = data_core
        self.session = requests.Session()

        self.window_start = 0
        self.slug = "INITIALIZING..."
        self.title = ""
        self.question = ""
        self.end_ts = 0      # unix seconds
        self.strike = 0.0
        self.ids = {"UP": None, "DOWN": None}

    def _current_window_start(self) -> int:
        now = int(time.time())
        return (now // WINDOW_SECONDS) * WINDOW_SECONDS

    def update(self):
        """
        Call this frequently from the UI loop.
        - Detects new window
        - Fetches market metadata (once per window)
        """
        now_window = self._current_window_start()
        if now_window != self.window_start:
            # New 15m window
            self.window_start = now_window
            self.slug = f"btc-updown-15m-{self.window_start}"
            self.title = ""
            self.question = ""
            self.end_ts = self.window_start + WINDOW_SECONDS
            # Start with oracle as strike snapshot if available
            self.strike = self.core.oracle_price if self.core.oracle_price > 0 else 0.0
            self.ids = {"UP": None, "DOWN": None}

        if self.ids["UP"] is None:
            self._fetch_market_metadata()

    def _fetch_market_metadata(self):
        """Load event + market data for current slug via Gamma events?slug."""
        try:
            url = f"{GAMMA_BASE}/events?slug={self.slug}"
            r = self.session.get(url, timeout=2)
            if r.status_code != 200:
                return
            data = r.json()
            if not data:
                return

            event = data[0]
            self.title = event.get("title", "") or ""
            markets = event.get("markets") or []
            if not markets:
                return
            market = markets[0]
            self.question = market.get("question", "") or ""

            # End time
            end_str = market.get("endDate") or event.get("endDate")
            if end_str:
                try:
                    # normalize Z to +00:00
                    dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    self.end_ts = int(dt.timestamp())
                except Exception:
                    self.end_ts = self.window_start + WINDOW_SECONDS

            # Token IDs and outcomes
            raw_ids = market.get("clobTokenIds")
            if isinstance(raw_ids, str):
                token_ids = json.loads(raw_ids)
            else:
                token_ids = raw_ids
            outcomes_raw = market.get("outcomes")
            if isinstance(outcomes_raw, str):
                outcomes = json.loads(outcomes_raw)
            else:
                outcomes = outcomes_raw

            if token_ids and len(token_ids) >= 2 and outcomes and len(outcomes) >= 2:
                # Normalize to strings
                t0 = str(token_ids[0])
                t1 = str(token_ids[1])
                o0 = str(outcomes[0]).lower()
                if o0.startswith("up"):
                    self.ids["UP"] = t0
                    self.ids["DOWN"] = t1
                else:
                    self.ids["UP"] = t1
                    self.ids["DOWN"] = t0

            # Strike from question if not already set
            if self.strike <= 0:
                m = re.search(r"\$([\d,]+\.?\d*)", self.question)
                if m:
                    try:
                        self.strike = float(m.group(1).replace(",", ""))
                    except Exception:
                        pass
        except Exception:
            pass

    def time_left(self) -> float:
        if self.end_ts <= 0:
            return 0.0
        return max(0.0, self.end_ts - time.time())


# ==============================
# 📚 ORDER BOOK STATE
# ==============================
class OrderBookState:
    def __init__(self, name: str, color: str):
        self.name = name
        self.color = color
        self.bids = {}  # price -> size
        self.asks = {}  # price -> size
        self.msg_count = 0

    def reset(self):
        self.bids.clear()
        self.asks.clear()
        self.msg_count = 0

    def update_level(self, price: str, size: float, side: str):
        self.msg_count += 1
        book = self.bids if side == "BUY" else self.asks
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size

    def snapshot(self, depth: int = 15):
        s_bids = sorted(self.bids.items(), key=lambda x: float(x[0]), reverse=True)[:depth]
        s_asks = sorted(self.asks.items(), key=lambda x: float(x[0]))[:depth]
        return s_bids, s_asks


up_book = OrderBookState("UP (YES)", "green")
down_book = OrderBookState("DOWN (NO)", "red")

# ==============================
# 📡 CLOB ORDER BOOK CLIENT
# ==============================
class ClobMarketClient:
    """
    CLOB WebSocket client for market channel.
    We dynamically set the asset_ids when MarketManager learns them.
    """
    def __init__(self, market_mgr: MarketManager):
        self.market_mgr = market_mgr
        self.ws = None
        self.asset_ids = set()
        self.lock = threading.Lock()

    def start(self):
        def on_message(ws, message: str):
            try:
                data = json.loads(message)
            except Exception:
                return

            try:
                etype = data.get("event_type")
                asset = str(data.get("asset_id", ""))
                up_id = str(self.market_mgr.ids.get("UP") or "")
                down_id = str(self.market_mgr.ids.get("DOWN") or "")

                # BOOK SNAPSHOT
                if etype == "book" and asset in (up_id, down_id):
                    state = up_book if asset == up_id else down_book
                    state.bids = {
                        lvl["price"]: float(lvl["size"])
                        for lvl in data.get("bids", [])
                    }
                    state.asks = {
                        lvl["price"]: float(lvl["size"])
                        for lvl in data.get("asks", [])
                    }

                # PRICE UPDATES
                elif etype == "price_change":
                    for change in data.get("price_changes", []):
                        a = str(change.get("asset_id", ""))
                        side = change.get("side")
                        price = change.get("price")
                        size = float(change.get("size", 0))
                        if a == up_id:
                            up_book.update_level(price, size, side)
                        elif a == down_id:
                            down_book.update_level(price, size, side)
            except Exception:
                pass

        def on_open(ws):
            # Subscribe with any known asset IDs
            with self.lock:
                if self.asset_ids:
                    sub_msg = {
                        "type": "subscribe",
                        "channel": "market",
                        "assets_ids": list(self.asset_ids),
                    }
                    try:
                        ws.send(json.dumps(sub_msg))
                    except Exception:
                        pass

        def run():
            while True:
                try:
                    self.ws = websocket.WebSocketApp(
                        CLOB_WS_URL,
                        on_open=on_open,
                        on_message=on_message,
                    )
                    self.ws.run_forever()
                except Exception:
                    time.sleep(1)

        threading.Thread(target=run, daemon=True).start()

    def set_assets(self, up_id: str, down_id: str):
        """Called from main thread whenever the active market IDs change."""
        if not up_id or not down_id:
            return
        new_set = {str(up_id), str(down_id)}
        with self.lock:
            if new_set == self.asset_ids:
                return
            self.asset_ids = new_set
            if self.ws:
                try:
                    sub_msg = {
                        "type": "subscribe",
                        "channel": "market",
                        "assets_ids": list(self.asset_ids),
                    }
                    self.ws.send(json.dumps(sub_msg))
                except Exception:
                    pass

# ==============================
# 🎨 RICH COMPONENTS
# ==============================
def create_imbalance_bar(bids, asks) -> Text:
    vol_bid = sum(x[1] for x in bids)
    vol_ask = sum(x[1] for x in asks)
    total = vol_bid + vol_ask if (vol_bid + vol_ask) > 0 else 1
    bid_pct = vol_bid / total

    bar_width = 30
    fill = int(bid_pct * bar_width)
    bar_color = "green" if bid_pct >= 0.5 else "red"
    bar_str = "█" * fill + "░" * (bar_width - fill)

    return Text(
        f"BUYERS {bid_pct:.0%} [{bar_str}] SELLERS {(1 - bid_pct):.0%}",
        style=f"bold {bar_color}",
    )


def render_orderbook_panel(state: OrderBookState) -> Panel:
    bids, asks = state.snapshot()
    best_bid = float(bids[0][0]) if bids else 0.0
    best_ask = float(asks[0][0]) if asks else 0.0
    spread = best_ask - best_bid if best_ask and best_bid else 0.0

    header = Text()
    header.append(f"{state.name}\n", style=f"bold {state.color} underline")
    header.append(f"${best_bid:.3f} ", style="bold white")
    header.append("vs", style="dim")
    header.append(f" ${best_ask:.3f} ", style="bold white")

    if spread != 0:
        if spread < 0.01:
            spread_color = "green"
        elif spread < 0.03:
            spread_color = "yellow"
        else:
            spread_color = "bold red"
        header.append(f" (Sprd: {spread:.3f})", style=spread_color)

    imbalance = create_imbalance_bar(bids, asks)

    table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
    table.add_column("BID QTY", justify="right", style=state.color)
    table.add_column("PRICE", justify="center", style="bold white")
    table.add_column("ASK QTY", justify="left", style=state.color)

    depth = max(len(bids), len(asks), 12)
    depth = min(depth, 15)
    for i in range(depth):
        b_qty = f"{bids[i][1]:,.0f}" if i < len(bids) else ""
        b_prc = f"{bids[i][0]}" if i < len(bids) else ""
        a_qty = f"{asks[i][1]:,.0f}" if i < len(asks) else ""
        a_prc = f"{asks[i][0]}" if i < len(asks) else ""
        price_display = f"{b_prc} | {a_prc}".strip(" |")
        table.add_row(b_qty, price_display, a_qty)

    content = Layout()
    content.split(
        Layout(Align.center(header), size=2),
        Layout(Align.center(imbalance), size=2),
        Layout(table),
    )

    return Panel(content, border_style=state.color)


def render_prices_panel(core: DataCore, market: MarketManager) -> Panel:
    now_ms = int(time.time() * 1000)

    fut_price = core.futures_price
    spot_price = core.spot_price
    relay_price = core.poly_relay_price
    oracle_price = core.oracle_price

    ping_fut = now_ms - core.futures_ts if core.futures_ts else 0
    ping_spot = now_ms - core.spot_ts if core.spot_ts else 0
    ping_relay = now_ms - core.poly_relay_ts if core.poly_relay_ts else 0
    ping_oracle = now_ms - core.oracle_ts if core.oracle_ts else 0

    basis = fut_price - spot_price if fut_price and spot_price else 0.0
    oracle_gap = oracle_price - spot_price if oracle_price and spot_price else 0.0

    table = Table(box=box.SIMPLE_HEAD, expand=True)
    table.add_column("SOURCE", justify="left")
    table.add_column("TICKER", justify="left")
    table.add_column("PRICE", justify="right")
    table.add_column("AGE (ms)", justify="right")

    def fmt_price(p):
        return f"${p:,.2f}" if p else "-"

    table.add_row("Binance Futures", "BTC/USDT", fmt_price(fut_price), str(ping_fut))
    table.add_row("Binance Spot", "BTC/USDT", fmt_price(spot_price), str(ping_spot))
    table.add_row("Poly Relay", "BTCUSDT", fmt_price(relay_price), str(ping_relay))
    table.add_row(
        "Chainlink",
        "BTC/USD",
        fmt_price(oracle_price),
        str(ping_oracle),
    )

    extra = Text()
    extra.append(f"BASIS (Fut-Spot): {basis:+.2f}\n", style="yellow")
    extra.append(f"ORACLE GAP (vs Spot): {oracle_gap:+.2f}", style="cyan")

    body = Layout()
    body.split(
        Layout(table, size=6),
        Layout(Align.left(extra)),
    )

    return Panel(body, title="Price Feeds", border_style="blue")


def render_header_panel(market: MarketManager, core: DataCore) -> Panel:
    time_left = market.time_left()
    mins = int(time_left // 60)
    secs = int(time_left % 60)
    timer_str = f"{mins:02d}:{secs:02d}"

    timer_color = "green"
    if time_left <= 60:
        timer_color = "red"
    elif time_left <= 180:
        timer_color = "yellow"

    text = Text()
    text.append("⚡ POLYMARKET BTC 15M TERMINAL ⚡\n", style="bold blue")

    text.append(f"Slug: {market.slug}\n", style="bold white")
    if market.title:
        text.append(f"Title: {market.title}\n", style="cyan")
    if market.question:
        # Trim extremely long questions in UI
        q = market.question
        if len(q) > 160:
            q = q[:157] + "..."
        text.append(f"Q: {q}\n", style="white")

    text.append(f"⏳ Time Left: ", style="white")
    text.append(timer_str, style=f"bold {timer_color}")

    # Strike + status vs spot
    if market.strike > 0 and core.spot_price > 0:
        diff = core.spot_price - market.strike
        status = "UP winning" if diff > 0 else "DOWN winning"
        col = "green" if diff > 0 else "red"
        text.append(f"   ⚡ Strike: ${market.strike:,.2f}", style="cyan")
        text.append(f"   📊 Status: {status} ({diff:+.2f})", style=col)

    return Panel(text, border_style="magenta")


def generate_layout(market: MarketManager) -> Layout:
    layout = Layout()

    header = render_header_panel(market, core)
    prices = render_prices_panel(core, market)
    up_panel = render_orderbook_panel(up_book)
    down_panel = render_orderbook_panel(down_book)

    layout.split(
        Layout(header, name="header", size=6),
        Layout(name="middle", size=10),
        Layout(name="orderbooks"),
    )

    layout["middle"].update(prices)
    layout["orderbooks"].split_row(
        Layout(up_panel),
        Layout(down_panel),
    )

    return layout

# ==============================
# 🚀 MAIN
# ==============================
def main():
    console.print("⏳ Starting streams...", style="bold yellow")

    # Start data streams
    BinanceFuturesClient(core).start()
    BinanceSpotClient(core).start()
    market_mgr = MarketManager(core)
    PolyDataClient(core).start()
    clob_client = ClobMarketClient(market_mgr)
    clob_client.start()

    prev_slug = None

    # Give feeds a moment
    time.sleep(1.0)

    try:
        with Live(generate_layout(market_mgr), refresh_per_second=10, screen=True) as live:
            while True:
                # Update market lifecycle
                old_slug = market_mgr.slug
                market_mgr.update()
                if market_mgr.slug != old_slug:
                    # New event/window: clear order books
                    up_book.reset()
                    down_book.reset()
                    prev_slug = market_mgr.slug

                # When IDs known, ensure CLOB subscription
                if market_mgr.ids["UP"] and market_mgr.ids["DOWN"]:
                    clob_client.set_assets(market_mgr.ids["UP"], market_mgr.ids["DOWN"])

                live.update(generate_layout(market_mgr))
                time.sleep(REFRESH_RATE)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
