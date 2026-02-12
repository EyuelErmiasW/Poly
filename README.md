# Polymarket Trading Bot

Automated trading bot for [Polymarket](https://polymarket.com) prediction markets. Scans markets, evaluates opportunities using configurable strategies, and executes trades via the Polymarket CLOB API.

## Architecture

```
main.py              CLI entry point
polybot/
  config.py          Environment-based configuration
  client.py          Polymarket API wrapper (py-clob-client)
  market_analyzer.py Market scanner and opportunity ranker
  risk_manager.py    Position sizing and exposure limits
  executor.py        Order submission engine
  bot.py             Main loop orchestrator
  strategies/
    base.py          Strategy interface
    value_finder.py  Buy underpriced outcomes at the tails
    midpoint_scalper.py  Capture spread by posting on both sides
    volume_momentum.py   Follow high-volume markets
```

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Edit .env with your private key and funder address

# 3. Run in dry-run mode (no real trades)
python main.py --dry-run

# 4. Run live (real money — be careful)
python main.py --live
```

## Strategies

| Strategy | Idea | Best for |
|---|---|---|
| `value_finder` | Buy cheap outcomes with wide spreads | Contrarian / tail bets |
| `midpoint_scalper` | Post limit orders on both sides of midpoint | Capturing spread |
| `volume_momentum` | Buy into high-volume markets | Trend following |

Select via `STRATEGY=value_finder` in `.env` or `--strategy value_finder` on the CLI.

## Configuration

All settings live in `.env` (see `.env.example`):

- `POLY_PRIVATE_KEY` — Your wallet private key
- `POLY_FUNDER_ADDRESS` — Address holding your Polymarket funds
- `MAX_TRADE_SIZE` — Max USDC per trade (default: 10)
- `MAX_TOTAL_EXPOSURE` — Max total USDC across all positions (default: 100)
- `MIN_EDGE` — Minimum edge threshold (default: 0.05)
- `SCAN_INTERVAL` — Seconds between market scans (default: 60)
- `DRY_RUN` — Set to `true` to simulate without trading

## CLI Flags

```
python main.py --help

--strategy {value_finder,midpoint_scalper,volume_momentum}
--dry-run        Simulate trades (no real orders)
--live           Real trading mode
--once           Run one cycle and exit
```

## Risk Warning

This bot trades real money on prediction markets. Use at your own risk. Always start with `--dry-run` to verify behavior before going live. Set conservative `MAX_TRADE_SIZE` and `MAX_TOTAL_EXPOSURE` limits.
