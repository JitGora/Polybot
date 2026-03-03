# Polymarket Terminal Console (BTC UP/DOWN 15m)

- Real-time **terminal/console for Polymarket** market monitoring and signal viewing.

- Auto-detects the current **BTC UP/DOWN 15-minute slug**.
- Auto-sets the **strike (price to beat)** at the start of each new 15m window.
- Streams live prices from **Polymarket** and **Binance** via WebSocket.
- Streams the live **Polymarket order book** for UP and DOWN tokens.
- Compares feeds to find potential **edge opportunities** when prices look mispriced.
- Combines multiple methods into one signal: `WAIT`, `WEAK BUY`, `BUY`, `STRONG BUY`.
- Updates in real time with time left, strike, odds, spreads, gaps, confidence, and strategy agreement.
![alt text](image-1.png)
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