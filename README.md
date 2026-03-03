# polymarket_terminal.py

Real-time **BTC 15m Polymarket decision terminal**: aggregates Binance spot/futures, Polymarket RTDS (relay + Chainlink), Gamma metadata, and CLOB orderbooks; computes multi-model fair values (oracle/time-weighted/market-sum/combined/futures/order-flow), runs 6 strategy evaluators, builds consensus (`WAIT` / `WEAK BUY` / `BUY` / `STRONG BUY`), and renders a live Rich dashboard with event timer, strike, feed lag, spreads, edges, confidence bars, and strategy reasons.

## Run
```bash
python3 polymarket_terminal.py
```

## Dependencies
```bash
pip install websockets requests rich numpy
```

## Data endpoints used
- Binance spot: `wss://stream.binance.com:9443/ws/btcusdt@trade`
- Binance futures: `wss://fstream.binance.com/ws/btcusdt@trade`
- Polymarket RTDS: `wss://ws-live-data.polymarket.com`
- Polymarket CLOB WS: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Gamma events API: `https://gamma-api.polymarket.com/events?slug=...`