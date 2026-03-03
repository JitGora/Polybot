#!/usr/bin/env python3
"""
Signal Data Analyzer v1.0
Analyzes trade signals from triple WSS scanner
"""

import pandas as pd
import numpy as np
from datetime import datetime
import matplotlib.pyplot as plt
from typing import Dict, List, Tuple
import json

class SignalAnalyzer:
    """Comprehensive analysis of trading signals"""

    def __init__(self, csv_file: str):
        """Load signal data from CSV"""
        self.df = pd.read_csv(csv_file)
        self.df['datetime'] = pd.to_datetime(self.df['datetime'])

        print(f"✓ Loaded {len(self.df)} signals from {csv_file}")
        print(f"  Date range: {self.df['datetime'].min()} to {self.df['datetime'].max()}")
        print(f"  UP signals: {len(self.df[self.df['side']=='UP'])}")
        print(f"  DOWN signals: {len(self.df[self.df['side']=='DOWN'])}")

    # ========================================================================
    # 1. BASIC STATISTICS
    # ========================================================================

    def basic_stats(self) -> Dict:
        """Calculate basic statistics"""
        stats = {
            'total_signals': len(self.df),
            'avg_net_edge': self.df['net_edge'].mean() * 100,
            'median_net_edge': self.df['net_edge'].median() * 100,
            'max_edge': self.df['net_edge'].max() * 100,
            'min_edge': self.df['net_edge'].min() * 100,
            'avg_oracle_lag': self.df['oracle_lag'].mean(),
            'avg_rtds_lag': self.df['rtds_lag'].mean(),
            'avg_confidence': self.df['confidence'].mean() * 100,
            'avg_spread': self.df['spread'].mean() * 100,
            'side_distribution': self.df['side'].value_counts().to_dict()
        }

        print("\n" + "="*70)
        print("BASIC STATISTICS")
        print("="*70)
        print(f"Total signals: {stats['total_signals']}")
        print(f"Avg net edge: {stats['avg_net_edge']:.2f}%")
        print(f"Median net edge: {stats['median_net_edge']:.2f}%")
        print(f"Edge range: {stats['min_edge']:.2f}% to {stats['max_edge']:.2f}%")
        print(f"Avg oracle lag: ${stats['avg_oracle_lag']:.2f}")
        print(f"Avg RTDS lag: ${stats['avg_rtds_lag']:.2f}")
        print(f"Avg confidence: {stats['avg_confidence']:.1f}%")
        print(f"Avg spread: {stats['avg_spread']:.3f}%")
        print(f"UP signals: {stats['side_distribution'].get('UP', 0)}")
        print(f"DOWN signals: {stats['side_distribution'].get('DOWN', 0)}")

        return stats

    # ========================================================================
    # 2. EDGE DISTRIBUTION ANALYSIS
    # ========================================================================

    def edge_distribution(self) -> Dict:
        """Analyze edge distribution"""
        print("\n" + "="*70)
        print("EDGE DISTRIBUTION")
        print("="*70)

        # Edge buckets
        buckets = [0.05, 0.07, 0.10, 0.15, 0.20, 1.0]
        labels = ['5-7%', '7-10%', '10-15%', '15-20%', '>20%']

        self.df['edge_bucket'] = pd.cut(
            self.df['net_edge'], 
            bins=[0] + buckets,
            labels=labels
        )

        distribution = self.df['edge_bucket'].value_counts().sort_index()

        print("\nNet Edge Distribution:")
        for bucket, count in distribution.items():
            pct = count / len(self.df) * 100
            print(f"  {bucket:>8}: {count:>3} signals ({pct:>5.1f}%)")

        # High-edge signals (>10%)
        high_edge = self.df[self.df['net_edge'] > 0.10]
        print(f"\nHigh-edge signals (>10%): {len(high_edge)} ({len(high_edge)/len(self.df)*100:.1f}%)")
        print(f"  Avg edge: {high_edge['net_edge'].mean()*100:.2f}%")
        print(f"  Avg oracle lag: ${high_edge['oracle_lag'].mean():.2f}")

        return distribution.to_dict()

    # ========================================================================
    # 3. ORACLE LAG VS EDGE ANALYSIS
    # ========================================================================

    def lag_vs_edge_analysis(self):
        """Analyze relationship between lag and edge"""
        print("\n" + "="*70)
        print("ORACLE LAG VS EDGE ANALYSIS")
        print("="*70)

        # Create lag buckets
        lag_buckets = [20, 30, 40, 50, 75, 100, 1000]
        lag_labels = ['$20-30', '$30-40', '$40-50', '$50-75', '$75-100', '>$100']

        self.df['lag_bucket'] = pd.cut(
            self.df['oracle_lag'].abs(),
            bins=[0] + lag_buckets,
            labels=lag_labels
        )

        # Group by lag bucket
        lag_analysis = self.df.groupby('lag_bucket').agg({
            'net_edge': ['count', 'mean', 'median', 'max'],
            'confidence': 'mean',
            'oracle_lag': 'mean'
        }).round(4)

        print("\nEdge by Oracle Lag Range:")
        print(lag_analysis)

        # Correlation
        correlation = self.df[['oracle_lag', 'net_edge', 'confidence']].corr()
        print("\nCorrelations:")
        print(correlation)

        return lag_analysis

    # ========================================================================
    # 4. TIMING ANALYSIS
    # ========================================================================

    def timing_analysis(self):
        """Analyze optimal entry timing within 15-min window"""
        print("\n" + "="*70)
        print("TIMING ANALYSIS (Entry within 15-min window)")
        print("="*70)

        # Calculate seconds into window
        self.df['seconds_into_window'] = 900 - self.df['time_left']

        # Time buckets (0-2min, 2-4min, etc.)
        time_buckets = [0, 120, 240, 360, 480, 600, 900]
        time_labels = ['0-2min', '2-4min', '4-6min', '6-8min', '8-10min', '10-15min']

        self.df['time_bucket'] = pd.cut(
            self.df['seconds_into_window'],
            bins=time_buckets,
            labels=time_labels
        )

        timing_stats = self.df.groupby('time_bucket').agg({
            'net_edge': ['count', 'mean', 'median'],
            'oracle_lag': 'mean',
            'confidence': 'mean'
        }).round(4)

        print("\nSignals by Entry Timing:")
        print(timing_stats)

        # Find optimal window
        optimal = timing_stats['net_edge']['mean'].idxmax()
        print(f"\n✓ Optimal entry window: {optimal}")

        return timing_stats

    # ========================================================================
    # 5. WIN RATE ESTIMATION
    # ========================================================================

    def estimate_win_rate(self):
        """Estimate win rate based on confidence scores"""
        print("\n" + "="*70)
        print("WIN RATE ESTIMATION")
        print("="*70)

        # High confidence signals (>70%)
        high_conf = self.df[self.df['confidence'] > 0.70]
        med_conf = self.df[(self.df['confidence'] > 0.50) & (self.df['confidence'] <= 0.70)]
        low_conf = self.df[self.df['confidence'] <= 0.50]

        print(f"High confidence (>70%): {len(high_conf)} signals")
        print(f"  Avg edge: {high_conf['net_edge'].mean()*100:.2f}%")
        print(f"  Avg oracle lag: ${high_conf['oracle_lag'].abs().mean():.2f}")
        print(f"  Expected win rate: ~{high_conf['confidence'].mean()*100:.0f}%")

        print(f"\nMedium confidence (50-70%): {len(med_conf)} signals")
        print(f"  Avg edge: {med_conf['net_edge'].mean()*100:.2f}%")
        print(f"  Expected win rate: ~{med_conf['confidence'].mean()*100:.0f}%")

        print(f"\nLow confidence (<50%): {len(low_conf)} signals")

        # Conservative win rate estimate
        estimated_wr = self.df['confidence'].mean()
        print(f"\nOverall estimated win rate: {estimated_wr*100:.1f}%")

        return estimated_wr

    # ========================================================================
    # 6. EXPECTED VALUE CALCULATION
    # ========================================================================

    def calculate_expected_value(self, position_size: float = 100):
        """Calculate expected profit per trade"""
        print("\n" + "="*70)
        print(f"EXPECTED VALUE (per ${position_size} position)")
        print("="*70)

        # Simple EV calculation
        avg_edge = self.df['net_edge'].mean()
        avg_confidence = self.df['confidence'].mean()

        # Expected profit per trade
        ev_per_trade = position_size * avg_edge

        # With win rate consideration
        win_payout = position_size * (1 / self.df['entry_price'].mean() - 1)
        lose_loss = position_size

        ev_realistic = (avg_confidence * win_payout) - ((1 - avg_confidence) * lose_loss)

        print(f"\nSimple EV (edge only):")
        print(f"  ${ev_per_trade:.2f} per trade")
        print(f"  ${ev_per_trade * len(self.df):.2f} total (all {len(self.df)} signals)")

        print(f"\nRealistic EV (with win rate):")
        print(f"  ${ev_realistic:.2f} per trade")
        print(f"  ROI: {ev_realistic/position_size*100:.2f}%")

        # High-edge only
        high_edge = self.df[self.df['net_edge'] > 0.10]
        if len(high_edge) > 0:
            ev_high = position_size * high_edge['net_edge'].mean()
            print(f"\nHigh-edge signals only (>10%):")
            print(f"  ${ev_high:.2f} per trade")
            print(f"  {len(high_edge)} opportunities")

        return ev_per_trade

    # ========================================================================
    # 7. HOURLY PATTERN ANALYSIS
    # ========================================================================

    def hourly_patterns(self):
        """Find patterns by hour of day"""
        print("\n" + "="*70)
        print("HOURLY PATTERNS (UTC)")
        print("="*70)

        self.df['hour'] = self.df['datetime'].dt.hour

        hourly = self.df.groupby('hour').agg({
            'net_edge': ['count', 'mean', 'max'],
            'oracle_lag': 'mean'
        }).round(4)

        print("\nSignals by Hour:")
        print(hourly)

        # Best hours
        best_hours = hourly['net_edge']['mean'].nlargest(5)
        print("\n✓ Best hours for trading:")
        for hour, edge in best_hours.items():
            count = hourly.loc[hour, ('net_edge', 'count')]
            print(f"  {hour:02d}:00 UTC - Avg edge: {edge*100:.2f}% ({int(count)} signals)")

        return hourly

    # ========================================================================
    # 8. SIDE BIAS ANALYSIS
    # ========================================================================

    def side_bias_analysis(self):
        """Analyze UP vs DOWN performance"""
        print("\n" + "="*70)
        print("UP vs DOWN SIDE ANALYSIS")
        print("="*70)

        side_stats = self.df.groupby('side').agg({
            'net_edge': ['count', 'mean', 'median', 'max'],
            'oracle_lag': 'mean',
            'confidence': 'mean',
            'spread': 'mean'
        }).round(4)

        print(side_stats)

        # Which side is more profitable?
        up_edge = self.df[self.df['side']=='UP']['net_edge'].mean()
        down_edge = self.df[self.df['side']=='DOWN']['net_edge'].mean()

        if up_edge > down_edge:
            print(f"\n✓ UP signals more profitable: {up_edge*100:.2f}% vs {down_edge*100:.2f}%")
        else:
            print(f"\n✓ DOWN signals more profitable: {down_edge*100:.2f}% vs {up_edge*100:.2f}%")

        return side_stats

    # ========================================================================
    # 9. FILTERING RULES
    # ========================================================================

    def optimal_filters(self):
        """Find optimal filtering rules"""
        print("\n" + "="*70)
        print("OPTIMAL FILTERING RULES")
        print("="*70)

        filters = []

        # Test different thresholds
        for min_edge in [0.05, 0.07, 0.10]:
            for min_lag in [20, 30, 40, 50]:
                for min_conf in [0.50, 0.60, 0.70]:
                    filtered = self.df[
                        (self.df['net_edge'] >= min_edge) &
                        (self.df['oracle_lag'].abs() >= min_lag) &
                        (self.df['confidence'] >= min_conf)
                    ]

                    if len(filtered) > 0:
                        filters.append({
                            'min_edge': min_edge,
                            'min_lag': min_lag,
                            'min_conf': min_conf,
                            'signals': len(filtered),
                            'avg_edge': filtered['net_edge'].mean(),
                            'pct_of_total': len(filtered) / len(self.df)
                        })

        # Convert to DataFrame and sort
        filter_df = pd.DataFrame(filters)
        filter_df = filter_df.sort_values('avg_edge', ascending=False)

        print("\nTop 10 Filter Combinations (by avg edge):")
        print(filter_df.head(10).to_string(index=False))

        # Recommended filter
        recommended = filter_df[filter_df['signals'] >= len(self.df) * 0.2].iloc[0]
        print("\n✓ RECOMMENDED FILTER:")
        print(f"  Min edge: {recommended['min_edge']*100:.0f}%")
        print(f"  Min oracle lag: ${recommended['min_lag']:.0f}")
        print(f"  Min confidence: {recommended['min_conf']*100:.0f}%")
        print(f"  → {int(recommended['signals'])} signals ({recommended['pct_of_total']*100:.0f}% of total)")
        print(f"  → Avg edge: {recommended['avg_edge']*100:.2f}%")

        return recommended

    # ========================================================================
    # 10. BACKTEST SIMULATION
    # ========================================================================

    def backtest(self, starting_capital: float = 1000, 
                 position_size: float = 100,
                 min_edge: float = 0.05,
                 min_confidence: float = 0.50):
        """Simulate trading with filters"""
        print("\n" + "="*70)
        print(f"BACKTEST SIMULATION")
        print("="*70)
        print(f"Starting capital: ${starting_capital}")
        print(f"Position size: ${position_size}")
        print(f"Min edge filter: {min_edge*100:.0f}%")
        print(f"Min confidence: {min_confidence*100:.0f}%")

        # Filter signals
        trades = self.df[
            (self.df['net_edge'] >= min_edge) &
            (self.df['confidence'] >= min_confidence)
        ].copy()

        print(f"\nTrades executed: {len(trades)}")

        # Simple P&L calculation (conservative)
        # Assume: win = +edge%, lose = -100%
        trades['win_prob'] = trades['confidence']
        trades['expected_pnl'] = position_size * trades['net_edge']

        # Total expected profit
        total_expected = trades['expected_pnl'].sum()
        roi = total_expected / starting_capital * 100

        print(f"\nExpected Results:")
        print(f"  Total profit: ${total_expected:.2f}")
        print(f"  ROI: {roi:.2f}%")
        print(f"  Profit per trade: ${total_expected/len(trades):.2f}")

        # Best and worst trades
        print(f"\nBest trade: {trades['net_edge'].max()*100:.2f}% edge")
        print(f"Worst trade: {trades['net_edge'].min()*100:.2f}% edge")

        # Risk metrics
        print(f"\nRisk Metrics:")
        print(f"  Max position at risk: ${position_size}")
        print(f"  Total capital at risk: ${position_size * len(trades):.2f}")
        print(f"  Risk/Reward ratio: 1:{total_expected/position_size:.2f}")

        return total_expected

    # ========================================================================
    # FULL REPORT
    # ========================================================================

    def full_report(self):
        """Generate complete analysis report"""
        print("\n" + "╔" + "═"*68 + "╗")
        print("║" + " "*20 + "FULL ANALYSIS REPORT" + " "*28 + "║")
        print("╚" + "═"*68 + "╝")

        self.basic_stats()
        self.edge_distribution()
        self.lag_vs_edge_analysis()
        self.timing_analysis()
        self.estimate_win_rate()
        self.calculate_expected_value()
        self.hourly_patterns()
        self.side_bias_analysis()
        recommended = self.optimal_filters()
        self.backtest(
            min_edge=recommended['min_edge'],
            min_confidence=recommended['min_conf']
        )

        print("\n" + "="*70)
        print("ANALYSIS COMPLETE")
        print("="*70)


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python analyze_signals.py signals_triple_wss.csv")
        sys.exit(1)

    csv_file = sys.argv[1]

    analyzer = SignalAnalyzer(csv_file)
    analyzer.full_report()

    # Save summary to JSON
    summary = {
        'total_signals': len(analyzer.df),
        'avg_edge': float(analyzer.df['net_edge'].mean() * 100),
        'avg_oracle_lag': float(analyzer.df['oracle_lag'].mean()),
        'date_range': {
            'start': str(analyzer.df['datetime'].min()),
            'end': str(analyzer.df['datetime'].max())
        }
    }

    with open('analysis_summary.json', 'w') as f:
        json.dump(summary, f, indent=2)

    print("\n✓ Summary saved to: analysis_summary.json")
