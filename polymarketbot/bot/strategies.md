# Trading Bot Strategy Explanation

## Yes, This Bot Takes Automatic Paper Trades!

The bot runs completely automatically:
1. Connects to real market data
2. Analyzes every 100ms
3. Executes paper trades when conditions are met
4. Settles positions at window end
5. Logs everything to CSV files

---

## The 5 Strategies Explained

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        STRATEGY OVERVIEW                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   Strategy        Allocation   When It Trades              Risk Level      │
│   ─────────────────────────────────────────────────────────────────────    │
│   1. OracleLag       30%       Oracle is stale + gap        Medium         │
│   2. EndGame         25%       Last 45 seconds              Low-Medium     │
│   3. Momentum        20%       Strong price movement        Medium         │
│   4. VolFade         15%       After volatility spike       Higher         │
│   5. Proximity       10%       Near strike, near expiry     Medium         │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Strategy 1: Oracle Lag (30% allocation)

```
THE EDGE:
- Chainlink oracle updates every ~20-60 seconds
- Binance Spot updates every ~100ms
- When BTC moves fast, Oracle is BEHIND reality
- We can predict where Oracle will go next

ALGORITHM:

    ┌──────────────────┐
    │ Every 100ms      │
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────────────────────┐
    │ Is Oracle stale? (>8 seconds)    │──── NO ───▶ SKIP
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ Is Gap > $60?                    │──── NO ───▶ SKIP
    │ (Spot Price - Oracle Price)      │
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ Time left 2-12 minutes?          │──── NO ───▶ SKIP
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ DECISION:                        │
    │                                  │
    │ If Spot > Oracle (Gap positive): │
    │   → Oracle will RISE             │
    │   → BUY UP shares                │
    │                                  │
    │ If Spot < Oracle (Gap negative): │
    │   → Oracle will FALL             │
    │   → BUY DOWN shares              │
    └──────────────────────────────────┘

EXAMPLE:
    
    Time: 10:05:00 (7 minutes into window)
    Strike: $98,400
    
    Binance Spot:  $98,550  (real-time)
    Chainlink:     $98,420  (15 seconds old)
    
    Gap = $98,550 - $98,420 = +$130
    
    Analysis:
    - Gap > $60 ✓
    - Oracle stale > 8s ✓
    - Spot ($98,550) > Strike ($98,400) ✓
    - Oracle ($98,420) < Strike... wait, it's above!
    
    But Spot is clearly above strike, so:
    → BUY UP at $0.58
    → Confidence: 65%
    → Size: $12
```

**Code Location:**
```python
class OracleLagStrategy(BaseStrategy):
    def analyze(self, data: dict, market: dict) -> Optional[Signal]:
        # ... checks gap between spot and oracle
        # ... if gap > 60 and oracle is stale
        # ... generates BUY_UP or BUY_DOWN signal
```

---

## Strategy 2: End Game (25% allocation)

```
THE EDGE:
- In the LAST 45 seconds, Chainlink probably won't update again
- If Oracle just updated (fresh), it's likely the SETTLEMENT price
- We can see the winner before the market realizes

ALGORITHM:

    ┌──────────────────┐
    │ Every 100ms      │
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────────────────────┐
    │ Time left < 45 seconds?          │──── NO ───▶ SKIP
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ Oracle FRESH? (<12 seconds old)  │──── NO ───▶ SKIP (risky)
    └────────┬─────────────────────────┘
             │ YES (Oracle just updated = likely final!)
             ▼
    ┌──────────────────────────────────┐
    │ Gap from Strike > $25?           │──── NO ───▶ SKIP (too close)
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ DECISION:                        │
    │                                  │
    │ If Oracle > Strike:              │
    │   → UP WINS at settlement        │
    │   → BUY UP (even at $0.85!)      │
    │                                  │
    │ If Oracle < Strike:              │
    │   → DOWN WINS at settlement      │
    │   → BUY DOWN                     │
    └──────────────────────────────────┘

EXAMPLE:

    Time: 10:14:30 (30 seconds left!)
    Strike: $98,400
    
    Chainlink: $98,520 (updated 5 seconds ago - FRESH!)
    
    Analysis:
    - Time left < 45s ✓
    - Oracle fresh (5s < 12s) ✓
    - Gap = $98,520 - $98,400 = +$120 > $25 ✓
    - Oracle > Strike → UP WINS!
    
    Action:
    → BUY UP at $0.78
    → Payout if win: $1.00
    → Profit: $0.22 per share (28% return!)
    → Confidence: 88%
    → Size: $20
```

**Why this works:**
```
Timeline of last minute:

    10:14:00 ─────────────────────────────────────── 10:15:00
         │                                              │
         │  Oracle updates    Oracle probably           │
         │  at 10:14:25       WON'T update again        │
         │       ▼                   │                  │
         ├───────●───────────────────┼──────────────────┤
         │       │                   │                  │
         │       │    ◄── SAFE ZONE ──►                │
         │       │     (we trade here)                  │
         │                                              │
    Window                                          Settlement
    continues                                       happens
```

---

## Strategy 3: Momentum (20% allocation)

```
THE EDGE:
- When BTC has strong directional movement, it tends to continue
- Market odds lag behind the actual price movement
- Ride the wave!

ALGORITHM:

    ┌──────────────────┐
    │ Every 100ms      │
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────────────────────┐
    │ Time left 1.5-10 minutes?        │──── NO ───▶ SKIP
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ Momentum > $70?                  │──── NO ───▶ SKIP
    │ (Oracle gap as proxy)            │
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ Price > $30 away from strike?    │──── NO ───▶ SKIP
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ DECISION:                        │
    │                                  │
    │ Strong UP momentum + above strike│
    │   → BUY UP                       │
    │                                  │
    │ Strong DOWN momentum + below     │
    │   → BUY DOWN                     │
    └──────────────────────────────────┘

EXAMPLE:

    BTC pumping hard:
    - 30 seconds ago: $98,300
    - Now:            $98,450
    - Movement:       +$150
    - Strike:         $98,350
    
    Analysis:
    - Momentum (+$150) > $70 ✓
    - Distance from strike: +$100 > $30 ✓
    - Strong upward trend
    
    Action:
    → BUY UP at $0.62
    → Confidence: 60%
    → Size: $10
```

---

## Strategy 4: Volatility Fade (15% allocation)

```
THE EDGE:
- When one side spikes to extreme prices, it often reverts
- People panic buy/sell, creating mispricing
- Buy the "loser" cheap

ALGORITHM:

    ┌──────────────────┐
    │ Every 100ms      │
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────────────────────┐
    │ Time left > 3 minutes?           │──── NO ───▶ SKIP
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ One side SPIKED (>$0.68)?        │──── NO ───▶ SKIP
    │ Other side CHEAP (<$0.32)?       │
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ DECISION:                        │
    │                                  │
    │ If UP spiked, DOWN cheap:        │
    │   → BUY DOWN (contrarian)        │
    │                                  │
    │ If DOWN spiked, UP cheap:        │
    │   → BUY UP (contrarian)          │
    └──────────────────────────────────┘

EXAMPLE:

    Sudden BTC dump causes panic:
    
    UP price:   $0.28 (cheap!)
    DOWN price: $0.72 (spiked!)
    
    Analysis:
    - DOWN > $0.68 ✓
    - UP < $0.32 ✓
    - Possible overreaction
    
    Action:
    → BUY UP at $0.28
    → If price reverts and UP wins: $1.00 payout
    → Profit: $0.72 per share (257% return!)
    → Confidence: 52%
    → Size: $8
    
    Risk: If DOWN actually wins, lose $8
```

---

## Strategy 5: Strike Proximity (10% allocation)

```
THE EDGE:
- When price is VERY close to strike near expiry
- Small movements have BIG impact on odds
- Use futures premium/discount as direction signal

ALGORITHM:

    ┌──────────────────┐
    │ Every 100ms      │
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────────────────────┐
    │ Time left 1-3 minutes?           │──── NO ───▶ SKIP
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ Price within $25 of strike?      │──── NO ───▶ SKIP
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ Futures premium/discount > $20?  │──── NO ───▶ SKIP
    └────────┬─────────────────────────┘
             │ YES
             ▼
    ┌──────────────────────────────────┐
    │ DECISION:                        │
    │                                  │
    │ Futures premium (Fut > Spot):    │
    │   → Market expects UP            │
    │   → BUY UP                       │
    │                                  │
    │ Futures discount (Fut < Spot):   │
    │   → Market expects DOWN          │
    │   → BUY DOWN                     │
    └──────────────────────────────────┘

EXAMPLE:

    Close call situation:
    
    Strike: $98,400
    Spot:   $98,415 (only $15 above!)
    Futures: $98,445 (+$30 premium)
    Time left: 2 minutes
    
    Analysis:
    - Distance from strike: $15 < $25 ✓
    - Futures premium: $30 > $20 ✓
    - Futures traders expect price to stay up
    
    Action:
    → BUY UP at $0.52
    → Confidence: 55%
    → Size: $6
```

---

## How The Bot Decides Which Strategy to Use

```python
# The bot runs ALL strategies every cycle
# Each strategy independently decides if conditions are met

def run_strategies(self):
    for strategy in self.strategies:
        
        # Check cooldown (10 seconds between trades per strategy)
        if recently_traded(strategy):
            continue
        
        # Ask strategy to analyze
        signal = strategy.analyze(data, market)
        
        if signal:
            # Check risk limits
            can_trade, reason = self.engine.can_trade(signal.size, market)
            
            if can_trade:
                # EXECUTE THE TRADE!
                self.engine.execute_trade(signal, market)
```

---

## Complete Trade Lifecycle

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TRADE LIFECYCLE                                   │
└─────────────────────────────────────────────────────────────────────────────┘

 WINDOW START (10:00:00)
        │
        │  Strike captured from Chainlink: $98,400
        │
        ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  MONITORING PHASE (10:00:00 - 10:15:00)                                  │
 │                                                                          │
 │  Every 100ms:                                                            │
 │    1. Fetch Binance Futures price                                        │
 │    2. Fetch Binance Spot price                                           │
 │    3. Fetch Chainlink Oracle price                                       │
 │    4. Fetch UP/DOWN share prices                                         │
 │    5. Run all 5 strategies                                               │
 │    6. Execute any valid signals                                          │
 │                                                                          │
 │  Example trades during window:                                           │
 │                                                                          │
 │  10:03:22 - OracleLag:  BUY UP   $12.00 @ 0.55  (oracle lagging)        │
 │  10:07:45 - Momentum:   BUY UP   $10.00 @ 0.61  (strong pump)           │
 │  10:12:30 - EndGame:    BUY UP   $18.00 @ 0.82  (30 sec left, locked)   │
 │                                                                          │
 └──────────────────────────────────────────────────────────────────────────┘
        │
        ▼
 WINDOW END (10:15:00)
        │
        │  Settlement price from Chainlink: $98,520
        │  Strike was: $98,400
        │  
        │  Result: $98,520 > $98,400 → UP WINS!
        │
        ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  SETTLEMENT                                                              │
 │                                                                          │
 │  Position 1 (OracleLag): UP @ 0.55, Size $12.00                         │
 │    Shares: 12.00 / 0.55 = 21.82 shares                                  │
 │    Payout: 21.82 × $1.00 = $21.82                                       │
 │    P&L: $21.82 - $12.00 = +$9.82 ✅                                     │
 │                                                                          │
 │  Position 2 (Momentum): UP @ 0.61, Size $10.00                          │
 │    Shares: 10.00 / 0.61 = 16.39 shares                                  │
 │    Payout: 16.39 × $1.00 = $16.39                                       │
 │    P&L: $16.39 - $10.00 = +$6.39 ✅                                     │
 │                                                                          │
 │  Position 3 (EndGame): UP @ 0.82, Size $18.00                           │
 │    Shares: 18.00 / 0.82 = 21.95 shares                                  │
 │    Payout: 21.95 × $1.00 = $21.95                                       │
 │    P&L: $21.95 - $18.00 = +$3.95 ✅                                     │
 │                                                                          │
 │  TOTAL WINDOW P&L: +$20.16                                              │
 │                                                                          │
 └──────────────────────────────────────────────────────────────────────────┘
        │
        ▼
 NEW WINDOW STARTS (10:15:00)
        │
        │  New Strike captured...
        │  Cycle repeats...
```

---

## Risk Management Rules

```python
# Built-in protections:

1. MAX_TRADE_PCT = 10%
   # Single trade cannot exceed 10% of balance
   # $100 balance → max $10 per trade

2. MAX_POSITION_PCT = 25%
   # Total exposure to one market cannot exceed 25%
   # $100 balance → max $25 in one 15-min window

3. MAX_DAILY_LOSS_PCT = 20%
   # Bot stops trading if down 20% for the day
   # $100 start → stops if balance drops to $80

4. MIN_TRADE_SIZE = $1.00
   # Won't make trades smaller than $1

5. SIGNAL_COOLDOWN = 10 seconds
   # Same strategy can't trade again for 10 seconds
   # Prevents over-trading

6. SLIPPAGE_BPS = 50 (0.5%)
   # Simulates realistic execution
   # Buy at $0.50 → filled at $0.5025
```

---

## What Gets Logged

```
paper_trading_logs/
└── session_20240115_143022/
    │
    ├── trades.csv
    │   └── Every trade: time, strategy, side, price, size, balance
    │
    ├── pnl.csv
    │   └── Every settlement: strike, settlement, P&L, win/loss
    │
    ├── signals.csv
    │   └── Every signal (even rejected): why generated, why rejected
    │
    └── debug.log
        └── Connection events, errors, settlements
```

**Example trades.csv:**
```csv
timestamp,trade_id,market_slug,strategy,side,size_usd,entry_price,filled_price,balance_after
2024-01-15T10:03:22,a1b2c3d4,btc-updown-15m-1705312800,OracleLag,UP,12.00,0.5500,0.5528,88.00
2024-01-15T10:07:45,e5f6g7h8,btc-updown-15m-1705312800,Momentum,UP,10.00,0.6100,0.6131,78.00
```

**Example pnl.csv:**
```csv
timestamp,market_slug,strike,settlement_price,strategy,side,entry_price,size_usd,pnl,result,balance_after
2024-01-15T10:15:00,btc-updown-15m-1705312800,98400.00,98520.00,OracleLag,UP,0.5528,12.00,9.71,WIN,109.71
```

---

## Summary

| Question | Answer |
|----------|--------|
| **Is it automatic?** | ✅ Yes, fully automatic paper trading |
| **Does it use real data?** | ✅ Yes, real Binance + Polymarket streams |
| **Does it risk real money?** | ❌ No, paper trading only |
| **How many strategies?** | 5 strategies running simultaneously |
| **How often does it check?** | Every 100ms (10 times per second) |
| **What markets?** | BTC 15-minute UP/DOWN markets |
| **Where are results?** | CSV files in `paper_trading_logs/` folder |

The bot is designed to find edges in the **timing differences** between real BTC prices and the Chainlink oracle that Polymarket uses for settlement. Each strategy exploits a different aspect of this timing mismatch.