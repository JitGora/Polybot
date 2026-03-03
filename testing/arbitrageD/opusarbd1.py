import json
import time
import threading
import re
import sys
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict

import requests
import websocket
from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console
from rich.align import Align
from rich import box
from rich.text import Text

# ============================================================
# ⚙️ CONFIGURATION
# ============================================================
REFRESH_RATE = 0.3
CLOB_PRICE_URL = "https://clob.polymarket.com/price"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

MIN_PROFIT_THRESHOLD = 0.003  # 0.3% minimum profit
POLY_FEE = 0.02
SLIPPAGE_BUFFER = 0.003

console = Console()

# ============================================================
# 📊 DATA STRUCTURES
# ============================================================
@dataclass
class OutcomeData:
    token_id: str
    name: str
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0

@dataclass
class MarketData:
    market_id: str
    question: str
    slug: str
    outcomes: Dict[str, OutcomeData] = field(default_factory=dict)
    end_date: Optional[str] = None
    active: bool = True

@dataclass 
class ArbitrageOpportunity:
    arb_type: str
    market_id: str
    question: str
    total_cost: float
    gross_profit_pct: float
    net_profit_pct: float
    max_size: float
    actions: List[str]
    detected_at: float = field(default_factory=time.time)

# ============================================================
# 🧠 CORE DATA MANAGER
# ============================================================
class ArbitrageCore:
    def __init__(self):
        self.lock = threading.RLock()
        self.markets: Dict[str, MarketData] = {}
        self.token_to_market: Dict[str, str] = {}
        self.opportunities: List[ArbitrageOpportunity] = []
        self.prices = {'binance': 0.0, 'binance_ts': 0}
        self.ws_messages = 0
        self.status = "Initializing..."

    def add_market(self, market: MarketData):
        with self.lock:
            self.markets[market.market_id] = market
            for outcome in market.outcomes.values():
                self.token_to_market[outcome.token_id] = market.market_id

    def get_active_markets(self) -> List[MarketData]:
        with self.lock:
            return list(self.markets.values())

core = ArbitrageCore()

# ============================================================
# 📡 MARKET FETCHER (with debugging)
# ============================================================
class MarketFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
    def fetch_active_markets(self, limit=50) -> int:
        """Fetch active markets from Polymarket API"""
        core.status = "Fetching markets from API..."
        
        try:
            params = {
                'active': 'true',
                'closed': 'false',
                'limit': limit,
            }
            
            console.print(f"[dim]Requesting: {GAMMA_EVENTS_URL}[/dim]")
            
            r = self.session.get(GAMMA_EVENTS_URL, params=params, timeout=15)
            
            console.print(f"[dim]Response status: {r.status_code}[/dim]")
            
            if r.status_code != 200:
                core.status = f"API Error: {r.status_code}"
                console.print(f"[red]API returned status {r.status_code}[/red]")
                return 0
            
            events = r.json()
            console.print(f"[green]Got {len(events)} events[/green]")
            
            count = 0
            for event in events:
                for market_data in event.get('markets', []):
                    if self._process_market(market_data, event):
                        count += 1
            
            core.status = f"Loaded {count} markets"
            return count
                    
        except requests.exceptions.Timeout:
            core.status = "API Timeout"
            console.print("[red]Request timed out[/red]")
            return 0
        except requests.exceptions.ConnectionError as e:
            core.status = "Connection Error"
            console.print(f"[red]Connection error: {e}[/red]")
            return 0
        except json.JSONDecodeError as e:
            core.status = "JSON Parse Error"
            console.print(f"[red]JSON decode error: {e}[/red]")
            return 0
        except Exception as e:
            core.status = f"Error: {str(e)[:30]}"
            console.print(f"[red]Unexpected error: {e}[/red]")
            return 0
    
    def _process_market(self, market_data: dict, event: dict) -> bool:
        """Process a single market"""
        try:
            market_id = market_data.get('id')
            question = market_data.get('question', '')
            
            if not market_id:
                return False
            
            outcomes_raw = market_data.get('outcomes', '[]')
            token_ids_raw = market_data.get('clobTokenIds', '[]')
            
            if isinstance(outcomes_raw, str):
                outcomes_list = json.loads(outcomes_raw)
            else:
                outcomes_list = outcomes_raw
                
            if isinstance(token_ids_raw, str):
                token_ids = json.loads(token_ids_raw)
            else:
                token_ids = token_ids_raw
            
            if len(outcomes_list) != len(token_ids) or len(outcomes_list) == 0:
                return False
            
            outcomes = {}
            for name, token_id in zip(outcomes_list, token_ids):
                outcomes[name] = OutcomeData(token_id=token_id, name=name)
            
            market = MarketData(
                market_id=market_id,
                question=question,
                slug=event.get('slug', ''),
                outcomes=outcomes,
                end_date=market_data.get('endDate'),
                active=True
            )
            
            core.add_market(market)
            return True
            
        except Exception as e:
            return False
    
    def fetch_prices_batch(self, markets: List[MarketData]):
        """Fetch prices for markets"""
        core.status = "Fetching prices..."
        
        for market in markets[:30]:  # Limit to avoid rate limits
            for outcome in market.outcomes.values():
                try:
                    # Get ask (buy) price
                    r = self.session.get(
                        CLOB_PRICE_URL,
                        params={'token_id': outcome.token_id, 'side': 'buy'},
                        timeout=5
                    )
                    if r.status_code == 200:
                        data = r.json()
                        outcome.best_ask = float(data.get('price', 0))
                    
                    # Get bid (sell) price
                    r = self.session.get(
                        CLOB_PRICE_URL,
                        params={'token_id': outcome.token_id, 'side': 'sell'},
                        timeout=5
                    )
                    if r.status_code == 200:
                        data = r.json()
                        outcome.best_bid = float(data.get('price', 0))
                        
                    time.sleep(0.05)  # Rate limit
                    
                except Exception:
                    pass
        
        core.status = "Ready"

fetcher = MarketFetcher()

# ============================================================
# 🔍 ARBITRAGE DETECTOR
# ============================================================
class ArbitrageDetector:
    def scan_all(self) -> List[ArbitrageOpportunity]:
        opportunities = []
        
        for market in core.get_active_markets():
            opp = self.check_market(market)
            if opp:
                opportunities.append(opp)
        
        core.opportunities = opportunities
        return opportunities

    def check_market(self, market: MarketData) -> Optional[ArbitrageOpportunity]:
        """Check if a market has arbitrage opportunity"""
        if len(market.outcomes) < 2:
            return None
        
        outcomes = list(market.outcomes.values())
        
        # Get ask prices (cost to buy)
        asks = [o.best_ask for o in outcomes if o.best_ask > 0]
        
        if len(asks) != len(outcomes):
            return None  # Missing price data
        
        total_cost = sum(asks)
        
        if total_cost <= 0 or total_cost >= 1.0:
            return None
        
        # Calculate profit
        gross_profit = 1.0 - total_cost
        gross_profit_pct = gross_profit / total_cost
        
        # Account for fees
        cheapest = min(asks)
        fee = POLY_FEE * (1.0 - cheapest)
        net_profit = gross_profit - fee - (total_cost * SLIPPAGE_BUFFER)
        net_profit_pct = net_profit / total_cost
        
        if net_profit_pct < MIN_PROFIT_THRESHOLD:
            return None
        
        # Build actions
        actions = []
        for o in outcomes:
            actions.append(f"BUY {o.name} @ ${o.best_ask:.4f}")
        actions.append(f"TOTAL: ${total_cost:.4f} → RETURN: $1.00")
        
        return ArbitrageOpportunity(
            arb_type="INTRA" if len(outcomes) == 2 else "MULTI",
            market_id=market.market_id,
            question=market.question,
            total_cost=total_cost,
            gross_profit_pct=gross_profit_pct,
            net_profit_pct=net_profit_pct,
            max_size=min(o.ask_size for o in outcomes if o.ask_size > 0) if any(o.ask_size > 0 for o in outcomes) else 0,
            actions=actions
        )

detector = ArbitrageDetector()

# ============================================================
# 📡 BINANCE WEBSOCKET
# ============================================================
class BinanceClient:
    def __init__(self):
        self.url = "wss://stream.binance.com:9443/ws/btcusdt@trade"

    def start(self):
        def run():
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        self.url,
                        on_message=lambda ws, msg: self._on_msg(msg),
                        on_error=lambda ws, e: None,
                    )
                    ws.run_forever()
                except Exception:
                    time.sleep(2)
        threading.Thread(target=run, daemon=True).start()

    def _on_msg(self, msg):
        try:
            d = json.loads(msg)
            core.prices['binance'] = float(d["p"])
            core.prices['binance_ts'] = int(d["T"])
            core.ws_messages += 1
        except Exception:
            pass

# ============================================================
# 📡 POLYMARKET CLOB WEBSOCKET  
# ============================================================
class PolyClobClient:
    def __init__(self):
        self.url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        self.ws = None
        self.subscribed = set()

    def start(self):
        def run():
            while True:
                try:
                    self.ws = websocket.WebSocketApp(
                        self.url,
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=lambda ws, e: None,
                    )
                    self.ws.run_forever()
                except Exception:
                    time.sleep(2)
        threading.Thread(target=run, daemon=True).start()

    def _on_open(self, ws):
        self.subscribe_all()

    def subscribe_all(self):
        token_ids = list(core.token_to_market.keys())
        if not token_ids or not self.ws:
            return
        
        new_ids = [t for t in token_ids if t not in self.subscribed][:50]
        if new_ids:
            try:
                msg = {"type": "subscribe", "channel": "market", "assets_ids": new_ids}
                self.ws.send(json.dumps(msg))
                self.subscribed.update(new_ids)
            except:
                pass

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            event_type = data.get('event_type')
            core.ws_messages += 1
            
            if event_type == 'book':
                asset_id = data.get('asset_id')
                market_id = core.token_to_market.get(asset_id)
                if market_id and market_id in core.markets:
                    market = core.markets[market_id]
                    for outcome in market.outcomes.values():
                        if outcome.token_id == asset_id:
                            bids = data.get('bids', [])
                            asks = data.get('asks', [])
                            if bids:
                                best = max(bids, key=lambda x: float(x.get('price', 0)))
                                outcome.best_bid = float(best.get('price', 0))
                                outcome.bid_size = float(best.get('size', 0))
                            if asks:
                                best = min(asks, key=lambda x: float(x.get('price', 999)))
                                outcome.best_ask = float(best.get('price', 0))
                                outcome.ask_size = float(best.get('size', 0))
                            break
                            
            elif event_type == 'price_change':
                for change in data.get('price_changes', []):
                    asset_id = change.get('asset_id')
                    market_id = core.token_to_market.get(asset_id)
                    if market_id and market_id in core.markets:
                        market = core.markets[market_id]
                        for outcome in market.outcomes.values():
                            if outcome.token_id == asset_id:
                                price = float(change.get('price', 0))
                                size = float(change.get('size', 0))
                                if change.get('side') == 'BUY':
                                    outcome.best_bid = price
                                    outcome.bid_size = size
                                else:
                                    outcome.best_ask = price
                                    outcome.ask_size = size
                                break
        except Exception:
            pass

clob_client = PolyClobClient()

# ============================================================
# 🎨 UI RENDERING
# ============================================================
def render_dashboard() -> Layout:
    opportunities = detector.scan_all()
    
    layout = Layout()
    layout.split(
        Layout(name="header", size=4),
        Layout(name="main"),
        Layout(name="footer", size=4)
    )
    
    # Header
    header = Text()
    header.append("🎯 POLYMARKET ARBITRAGE DETECTOR\n", style="bold cyan")
    header.append(f"Markets: {len(core.markets)} | ", style="white")
    header.append(f"WS Msgs: {core.ws_messages} | ", style="dim")
    header.append(f"BTC: ${core.prices['binance']:,.2f} | " if core.prices['binance'] > 0 else "BTC: -- | ", style="yellow")
    header.append(f"Status: {core.status}", style="green" if "Ready" in core.status else "yellow")
    layout["header"].update(Panel(header))
    
    # Main - split into opportunities and market list
    layout["main"].split_row(
        Layout(name="opps", ratio=2),
        Layout(name="markets", ratio=1)
    )
    
    # Opportunities panel
    if opportunities:
        opp_table = Table(box=box.ROUNDED, expand=True)
        opp_table.add_column("TYPE", style="cyan", width=6)
        opp_table.add_column("MARKET", style="white", max_width=40)
        opp_table.add_column("COST", justify="right")
        opp_table.add_column("GROSS%", justify="right", style="green")
        opp_table.add_column("NET%", justify="right", style="bold green")
        
        for opp in sorted(opportunities, key=lambda x: x.net_profit_pct, reverse=True)[:10]:
            opp_table.add_row(
                opp.arb_type,
                opp.question[:38] + "..." if len(opp.question) > 38 else opp.question,
                f"${opp.total_cost:.4f}",
                f"{opp.gross_profit_pct:.2%}",
                f"{opp.net_profit_pct:.2%}"
            )
        
        layout["opps"].update(Panel(opp_table, title="🚨 ARBITRAGE OPPORTUNITIES", border_style="red"))
    else:
        no_opp = Text("No arbitrage opportunities found\n\n", style="dim")
        no_opp.append("Scanning for YES + NO < $1.00...\n", style="italic")
        no_opp.append(f"Checked {len(core.markets)} markets", style="dim")
        layout["opps"].update(Panel(no_opp, title="🔍 SCANNER", border_style="dim"))
    
    # Market list panel
    market_table = Table(box=box.SIMPLE, expand=True)
    market_table.add_column("MARKET", max_width=25)
    market_table.add_column("YES", justify="right")
    market_table.add_column("NO", justify="right")
    market_table.add_column("SUM", justify="right")
    
    markets = core.get_active_markets()
    for market in sorted(markets, key=lambda m: sum(o.best_ask for o in m.outcomes.values() if o.best_ask > 0))[:15]:
        if len(market.outcomes) != 2:
            continue
        outcomes = list(market.outcomes.values())
        yes_ask = outcomes[0].best_ask
        no_ask = outcomes[1].best_ask
        total = yes_ask + no_ask
        
        if total > 0:
            sum_style = "bold green" if total < 1.0 else "white"
            market_table.add_row(
                market.question[:23] + "...",
                f"${yes_ask:.3f}" if yes_ask > 0 else "-",
                f"${no_ask:.3f}" if no_ask > 0 else "-",
                Text(f"${total:.3f}", style=sum_style)
            )
    
    layout["markets"].update(Panel(market_table, title="📊 MARKETS", border_style="blue"))
    
    # Footer - best opportunity details
    footer = Text()
    if opportunities:
        best = max(opportunities, key=lambda x: x.net_profit_pct)
        footer.append("🚨 BEST OPPORTUNITY: ", style="bold red")
        footer.append(f"{best.net_profit_pct:.2%} profit ", style="bold green")
        footer.append(f"| Cost: ${best.total_cost:.4f} | ", style="white")
        footer.append(f"{best.question[:50]}...", style="dim")
    else:
        footer.append("Monitoring markets for arbitrage (YES + NO < $1.00)...", style="dim italic")
    
    layout["footer"].update(Panel(footer))
    
    return layout

# ============================================================
# 🚀 MAIN
# ============================================================
def background_refresh():
    """Background thread to refresh data"""
    while True:
        try:
            time.sleep(30)
            fetcher.fetch_active_markets(limit=50)
            time.sleep(5)
            fetcher.fetch_prices_batch(core.get_active_markets())
            clob_client.subscribe_all()
        except Exception:
            pass

if __name__ == "__main__":
    console.print("\n[bold cyan]🎯 POLYMARKET ARBITRAGE DETECTOR[/bold cyan]")
    console.print("[dim]=" * 50 + "[/dim]\n")
    
    # Test API connectivity first
    console.print("[yellow]Testing API connectivity...[/yellow]")
    
    try:
        test_r = requests.get("https://gamma-api.polymarket.com/events?limit=1", timeout=10)
        console.print(f"[green]✓ API reachable (status: {test_r.status_code})[/green]")
    except Exception as e:
        console.print(f"[red]✗ API unreachable: {e}[/red]")
        console.print("[yellow]Check your internet connection or try a VPN[/yellow]")
        sys.exit(1)
    
    # Start WebSocket clients
    console.print("[yellow]Starting WebSocket streams...[/yellow]")
    BinanceClient().start()
    clob_client.start()
    console.print("[green]✓ WebSockets started[/green]")
    
    # Fetch initial markets
    console.print("[yellow]Fetching markets...[/yellow]")
    count = fetcher.fetch_active_markets(limit=50)
    
    if count == 0:
        console.print("[red]✗ No markets loaded. API may be having issues.[/red]")
        console.print("[yellow]Starting anyway with empty market list...[/yellow]")
    else:
        console.print(f"[green]✓ Loaded {count} markets[/green]")
    
    # Fetch initial prices
    console.print("[yellow]Fetching prices...[/yellow]")
    fetcher.fetch_prices_batch(core.get_active_markets())
    console.print("[green]✓ Prices loaded[/green]")
    
    # Start background refresh
    threading.Thread(target=background_refresh, daemon=True).start()
    
    console.print("\n[bold green]Starting dashboard...[/bold green]\n")
    time.sleep(1)
    
    # Main display loop
    try:
        with Live(render_dashboard(), refresh_per_second=3, screen=True) as live:
            while True:
                live.update(render_dashboard())
                time.sleep(REFRESH_RATE)
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")