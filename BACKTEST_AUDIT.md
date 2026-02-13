# Backtest Results Audit — Why the Numbers Are Wrong

## Summary

The reported backtest results (60.3% win rate, +20.7% ROI) are unreliable due to
fundamental flaws in how trades are simulated. **There is no actual backtesting engine
in this codebase.** Results can only come from dry-run mode, which makes assumptions
that dramatically inflate performance.

## Critical Issues

### 1. No Backtester Exists

There is no historical data replay, no simulation engine, and no settlement tracking
anywhere in the codebase. The only "simulation" is dry-run mode in `executor.py`,
which was designed for smoke-testing — not performance measurement.

### 2. 100% Fill Rate Assumption

`executor.py:52` calls `self.risk.record_fill(sized)` immediately in dry-run mode,
assuming every limit order fills at the desired price. In practice:

- `ValueFinder` places orders inside the spread — these often sit unfilled
- `CryptoOracle` prices below the ask — no guarantee of execution
- `MidpointScalper` places on both sides — one side failing means the other is naked

Real Polymarket fill rates on limit orders are significantly below 100%.

### 3. No Settlement Verification

The codebase has no concept of market resolution. Nothing checks whether a purchased
outcome settled to 1.0 (win) or 0.0 (loss). The "win rate" metric cannot be
calculated from this code alone.

### 4. No Fee Modeling

Polymarket trading fees are not accounted for anywhere. On small trades ($1-$10)
with thin edges (3-5%), fees can consume the entire expected profit.

### 5. No Slippage or Market Impact

Orders assume zero market impact. The minimum liquidity threshold is only $500 USDC
(`market_analyzer.py:31`), meaning even small orders can move the book.

### 6. Naive Probability Model (CryptoOracle)

- Logistic approximation of normal CDF (`crypto_oracle.py:102`)
- Assumes normally distributed returns (crypto has fat tails)
- Volatility estimated from only 24 hourly candles — unstable
- No drift term
- "Time decay bonus" (lines 241-244) inflates edge for near-expiry trades

### 7. Extremely Low Confidence Threshold (FastCrypto)

`MIN_CONFIDENCE = 0.25` (`fast_crypto.py:231`) lets through nearly every signal.
On 50/50 up-or-down markets, random guessing yields ~50% win rate. A 60% reported
rate with this loose filter is not statistically meaningful.

### 8. Potential Lookahead Bias

No historical data infrastructure exists. Any "backtest" uses live/current market
data, which risks testing against known outcomes.

## What a Real Backtest Would Need

1. Historical market data with timestamps (order books, prices, settlements)
2. Realistic fill simulation (order book depth, queue position, partial fills)
3. Fee modeling (maker/taker fees per trade)
4. Slippage modeling (market impact on thin books)
5. Settlement verification against actual market outcomes
6. Out-of-sample testing (train/test split on historical data)
7. Statistical significance testing (are results distinguishable from chance?)

## Conclusion

The reported results are not actionable. A realistic accounting of fills, fees,
and slippage would likely reduce the ROI to break-even or negative.
