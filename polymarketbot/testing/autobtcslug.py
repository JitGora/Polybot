import time
import requests
import json
from datetime import datetime

# ==========================================
# 🧠 SLIDING WINDOW PRE-COMPUTER
# ==========================================
class MarketWindowFetcher:
    def __init__(self, lookahead=3):
        self.lookahead = lookahead  # How many future markets to fetch
        self.market_cache = {}      # Store: { slug: {up_id, down_id, end_time} }

    def calculate_slugs(self):
        """Generates a list of slugs for Current + Next N windows"""
        now = int(time.time())
        window_size = 900  # 15 minutes
        
        # Current Window Start
        current_window_start = (now // window_size) * window_size
        
        slugs = []
        for i in range(self.lookahead + 1): # +1 to include current
            # Calculate future timestamp
            future_start = current_window_start + (i * window_size)
            slug = f"btc-updown-15m-{future_start}"
            slugs.append(slug)
            
        return slugs

    def fetch_window_details(self, slug):
        """Fetches IDs for a single specific slug"""
        # If already cached, return instant
        if slug in self.market_cache:
            return self.market_cache[slug]

        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        try:
            r = requests.get(url)
            data = r.json()
            
            if not data:
                return None # Market not created yet

            event = data[0]
            market = event['markets'][0]
            
            # Safe Parsing
            raw_ids = market.get('clobTokenIds')
            token_ids = json.loads(raw_ids)
            outcomes = json.loads(market.get('outcomes'))
            
            up_id = token_ids[0] if outcomes[0] == "Up" else token_ids[1]
            down_id = token_ids[1] if outcomes[0] == "Up" else token_ids[0]
            
            # Cache the result
            self.market_cache[slug] = {
                "up_id": up_id,
                "down_id": down_id,
                "end_date": market.get('endDate'),
                "title": event.get('title')
            }
            
            print(f"✅ CACHED: {slug} | {event.get('title')}")
            return self.market_cache[slug]

        except Exception as e:
            print(f"❌ Error fetching {slug}: {e}")
            return None

    def refresh_sliding_window(self):
        """Main function to call periodically"""
        print(f"\n🔄 Refreshing Sliding Window (Current + {self.lookahead} Future)...")
        target_slugs = self.calculate_slugs()
        
        for slug in target_slugs:
            self.fetch_window_details(slug)
            
        # Cleanup old cache (remove markets that have passed)
        current_keys = list(self.market_cache.keys())
        for k in current_keys:
            if k not in target_slugs:
                del self.market_cache[k]

    def get_active_market(self):
        """Returns the IDs for the market active RIGHT NOW"""
        # Recalculate current slug to be precise
        current_slug = self.calculate_slugs()[0] 
        return self.market_cache.get(current_slug)

# ==========================================
# 🚀 RUNNER
# ==========================================
if __name__ == "__main__":
    # Initialize Fetcher (Look 2 markets ahead)
    fetcher = MarketWindowFetcher(lookahead=2)
    
    # 1. Pre-compute everything
    fetcher.refresh_sliding_window()
    
    # 2. Access the current active market
    current = fetcher.get_active_market()
    
    if current:
        print("\n🎯 CURRENT ACTIVE MARKET:")
        print(f"   Title: {current['title']}")
        print(f"   UP:    {current['up_id']}")
        print(f"   DOWN:  {current['down_id']}")
    else:
        print("\n⚠️ Current market not found (API Lag or Transition)")

    # 3. View the Cache
    print("\n📦 FULL PRE-COMPUTED CACHE:")
    print(json.dumps(fetcher.market_cache, indent=2))