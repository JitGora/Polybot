# ==========================================
# 🧠 MARKET MANAGER (v9.1 - ANTI-BAN PROTECTION)
# ==========================================
class MarketManager:
    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://gamma-api.polymarket.com/events/slug/"
        self.clob_url = "https://clob.polymarket.com/price"
        
        self.state = "INITIALIZING"
        self.current_window_ts = 0
        self.slug = "finding..."
        self.strike = 0.0
        
        self.ids = {"UP": None, "DOWN": None}
        self.prices = {"UP": 0.0, "DOWN": 0.0}
        
        # Pre-Heat Buffer
        self.next_slug = None
        self.next_ids = {"UP": None, "DOWN": None}
        
        # 🛡️ SAFETY: Rate Limiting
        self.last_fetch_ts = 0 
        
        self._startup_check()

    def _startup_check(self):
        now = time.time()
        current_window = (int(now) // WINDOW_DURATION) * WINDOW_DURATION
        saved = StateManager.load()

        if saved and saved['window_ts'] == current_window:
            print("✅ RESUMING ACTIVE SESSION...")
            self.current_window_ts = saved['window_ts']
            self.strike = saved['strike']
            self.slug = saved['slug']
            self.state = "ACTIVE"
        else:
            print("⚠️ STARTING FRESH. WAITING FOR NEXT WINDOW...")
            self.state = "WAITING"
            self.current_window_ts = current_window + WINDOW_DURATION 
            self.slug = f"btc-updown-15m-{self.current_window_ts}"

    def lifecycle_loop(self):
        now = time.time()
        
        # WAITING STATE
        if self.state == "WAITING":
            if now >= self.current_window_ts:
                self._activate_new_market()

        # ACTIVE STATE
        elif self.state == "ACTIVE":
            time_left = (self.current_window_ts + WINDOW_DURATION) - now
            
            # Pre-Heat Logic
            if time_left < 60 and self.next_ids["UP"] is None:
                self._preheat_next_market()
                
            # Rollover Logic
            if now >= (self.current_window_ts + WINDOW_DURATION):
                print(f"🔄 ROLLOVER DETECTED AT {int(now)}")
                self._switch_to_next_market()

    def _activate_new_market(self):
        # ⚡ STRIKE LOGIC: Prefer Chainlink. Fallback to Spot if CL is 0.
        self.strike = core.oracle_price if core.oracle_price > 0 else core.spot_price
        
        self.state = "ACTIVE"
        self.slug = f"btc-updown-15m-{self.current_window_ts}"
        StateManager.save(self.current_window_ts, self.strike, self.slug)
        self.fetch_market_details(self.slug, is_next=False)

    def _preheat_next_market(self):
        next_ts = self.current_window_ts + WINDOW_DURATION
        self.next_slug = f"btc-updown-15m-{next_ts}"
        self.fetch_market_details(self.next_slug, is_next=True)

    def _switch_to_next_market(self):
        self.current_window_ts += WINDOW_DURATION
        # Snapshot new strike
        self.strike = core.oracle_price if core.oracle_price > 0 else core.spot_price
        
        # Swap Buffer
        self.slug = self.next_slug
        if self.next_ids["UP"]:
            self.ids = self.next_ids
            print("⚡ INSTANT SWITCH (IDs Pre-Loaded)")
        else:
            print("⚠️ BUFFER MISS - Fetching Live...")
            self.ids = {"UP": None, "DOWN": None}
            self.fetch_market_details(self.slug, is_next=False)
            
        self.next_slug = None
        self.next_ids = {"UP": None, "DOWN": None}
        self.prices = {"UP": 0.0, "DOWN": 0.0}
        StateManager.save(self.current_window_ts, self.strike, self.slug)

    def fetch_market_details(self, slug_to_fetch, is_next=False):
        try:
            r = self.session.get(f"{self.base_url}{slug_to_fetch}")
            if r.status_code == 200:
                data = r.json()
                market = data['markets'][0]
                t_ids = json.loads(market['clobTokenIds'])
                if is_next:
                    self.next_ids["UP"] = t_ids[0]
                    self.next_ids["DOWN"] = t_ids[1]
                else:
                    self.ids["UP"] = t_ids[0]
                    self.ids["DOWN"] = t_ids[1]
        except: pass

    def fetch_live_share_prices(self):
        # 🛡️ RATE LIMITER: Only fetch once every 1.0 seconds
        if time.time() - self.last_fetch_ts < 1.0: return
        self.last_fetch_ts = time.time()
        
        if self.ids["UP"] is None: return
        try:
            r = self.session.get(f"{self.clob_url}?token_id={self.ids['UP']}&side=buy")
            if r.status_code == 200: self.prices["UP"] = float(r.json()['price'])
            
            r = self.session.get(f"{self.clob_url}?token_id={self.ids['DOWN']}&side=buy")
            if r.status_code == 200: self.prices["DOWN"] = float(r.json()['price'])
        except: pass