import requests
import json
import time

def check_market(slug):
    # 1. Fetch Event Details
    print(f"🔍 Fetching details for: {slug}...")
    gamma_url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
    
    try:
        r = requests.get(gamma_url)
        if r.status_code != 200:
            print(f"❌ Error fetching event: {r.status_code}")
            return
        
        data = r.json()
        
        # The 'markets' list usually contains the specific binary market we want
        market = data['markets'][0]
        
        # Parse clobTokenIds (User's JSON shows it might be a stringified list)
        token_ids_raw = market.get('clobTokenIds')
        if isinstance(token_ids_raw, str):
            token_ids = json.loads(token_ids_raw)
        else:
            token_ids = token_ids_raw
            
        outcomes = json.loads(market['outcomes']) if isinstance(market['outcomes'], str) else market['outcomes']
        
        print(f"✅ Market Found: {market['question']}")
        print(f"📅 Ends: {market['endDate']}")
        print("-" * 40)

        # 2. Fetch Prices for Up (Index 0) and Down (Index 1)
        for index, outcome in enumerate(outcomes):
            token_id = token_ids[index]
            
            # fetching 'sell' side gives the highest BID (instant sell price)
            # fetching 'buy' side gives the lowest ASK (instant buy price)
            # We will fetch 'buy' to see what it costs to enter.
            clob_url = f"https://clob.polymarket.com/price?token_id={token_id}&side=buy"
            
            p_req = requests.get(clob_url)
            if p_req.status_code == 200:
                price_data = p_req.json()
                price = float(price_data['price'])
                print(f"🎫 {outcome} (ID: ...{token_id[-6:]}) | Price: ${price:.3f}")
            else:
                print(f"⚠️ Could not fetch price for {outcome}")

    except Exception as e:
        print(f"❌ Script Error: {e}")

# --- Run it ---
slug = "btc-updown-15m-1767868200" 
check_market(slug)