import requests
import json
from datetime import datetime

# LIVE Gamma API - No authentication required
response = requests.get("https://gamma-api.polymarket.com/markets?active=true&limit=200")
live_markets = response.json()

# Save raw data
with open('live_polymarket_markets.json', 'w') as f:
    json.dump(live_markets, f, indent=2)

with open('live_polymarket_markets.txt', 'w') as f:
    for market in live_markets[:50]:
        f.write(f"ID: {market.get('id', 'N/A')}\n")
        f.write(f"Title: {market.get('question', 'N/A')}\n")
        f.write(f"Prices: {market.get('outcomePrices', 'N/A')}\n")
        f.write("-"*60 + "\n")

# Console summary
crypto_count = sum(1 for m in live_markets if any(x in m.get('question', '').upper() for x in ['BITCOIN', 'ETH', 'BTC', 'ETH']))
fifteen_min = sum(1 for m in live_markets if '15m' in m.get('question', '').lower() or any(t in m.get('question', '') for t in ['6:', '7:']))

print("\n" + "="*50)
print("🔥 LIVE POLYMARKET SUMMARY (Gamma API)")
print("="*50)
print(f"📊 Total active markets:  {len(live_markets)}")
print(f"₿  Crypto markets:        {crypto_count}")
print(f"⏱️  15-min markets:       {fifteen_min}")
print(f"💾 Saved: live_polymarket_markets.*")
print(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("="*50)

# Show examples
crypto_examples = [m['question'][:60] + '...' for m in live_markets[:10] if any(x in m.get('question', '').upper() for x in ['BITCOIN', 'ETH'])]
if crypto_examples:
    print("\n🚀 LIVE CRYPTO MARKETS:")
    for i, ex in enumerate(crypto_examples[:3], 1):
        print(f"   {i}. {ex}")
