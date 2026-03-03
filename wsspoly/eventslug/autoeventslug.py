import time
import requests
import json

class PolySlugGenerator:
    def __init__(self):
        self.base_url = "https://gamma-api.polymarket.com/events/slug/"
        self.clob_url = "https://clob.polymarket.com/price"

    def get_current_slug(self):
        # 1. Get current time
        now = int(time.time())
        
        # 2. Round down to the nearest 15 minutes (900 seconds)
        # This gives the START time of the current 15m candle
        window_start = (now // 900) * 900
        
        # 3. Construct Slug
        return f"btc-updown-15m-{window_start}"

    def fetch_market_prices(self):
        slug = self.get_current_slug()
        print(f"🔄 Generated Slug: {slug}")
        
        try:
            # Step A: Get Event Details (to get Token IDs)
            r = requests.get(f"{self.base_url}{slug}")
            if r.status_code != 200:
                print(f"⚠️ Market not found yet (HTTP {r.status_code}). check if market is live.")
                return

            data = r.json()
            market = data['markets'][0] # usually the first market in the list
            
            # Extract Token IDs
            token_ids_raw = market.get('clobTokenIds')
            token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
            outcomes = json.loads(market['outcomes']) if isinstance(market['outcomes'], str) else market['outcomes']

            print(f"✅ Market: {market['question']}")
            print("-" * 40)

            # Step B: Get Prices for UP and DOWN
            prices = {}
            for index, outcome in enumerate(outcomes):
                t_id = token_ids[index]
                # 'side=buy' gets the current lowest Ask (entry price)
                p_res = requests.get(f"{self.clob_url}?token_id={t_id}&side=buy")
                
                if p_res.status_code == 200:
                    price = float(p_res.json()['price'])
                    prices[outcome] = price
                    print(f"🎫 {outcome:<4} | Price: ${price:.3f} | ID: ...{t_id[-6:]}")
                else:
                    print(f"❌ Failed to fetch price for {outcome}")

            return prices

        except Exception as e:
            print(f"❌ Error: {e}")

# --- Run ---
if __name__ == "__main__":
    poly = PolySlugGenerator()
    poly.fetch_market_prices()