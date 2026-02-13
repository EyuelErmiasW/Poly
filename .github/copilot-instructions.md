<!-- Copilot instructions for AI coding agents working on this repo -->

# Polymarket Bot — Quick Onboarding for AI Agents

Purpose: give an AI agent immediately actionable knowledge to navigate, modify, and extend this trading bot.

- **Big picture**: `main.py` is the CLI entry. `polybot/` implements the core pipeline:
  - `polybot/config.py` — env-driven config (`.env` / `.env.example`).
  - `polybot/client.py` — thin wrapper around `py-clob-client`; provides `PolyClient`, `Market`, and `OrderBookSnapshot` helpers.
  - `polybot/market_analyzer.py` — fetches markets and builds `Opportunity` objects (filters by `min_liquidity`).
  - `polybot/strategies/*` — strategy implementations exposing `evaluate(opportunities) -> list[Signal]`.
  - `polybot/risk_manager.py` — sizes signals and enforces exposure limits; strategies emit size=0 and rely on this component.
  - `polybot/executor.py` — turns sized `Signal`s into orders (honors `cfg.dry_run`).
  - `polybot/bot.py` — orchestration: scanner -> strategy -> risk -> executor. Strategies are registered in `STRATEGIES` here.

- **Key dataflows & patterns**:
  - Market discovery: `PolyClient.fetch_active_markets()` -> `MarketAnalyzer.scan()` -> list of `Opportunity`.
  - Strategy contract: read-only `Opportunity` objects; set `opp.score` and return `Signal` objects. `Signal.size` is left 0 and later sized by `RiskManager.size_signal()`.
  - Execution: `Executor.execute()` calls `RiskManager.size_signal()`; if `cfg.dry_run` is true the executor logs and records fills without posting orders.
  - Prices are expressed in [0,1] (fractional shares), and sizes are in shares. Signals use `price` (limit) and `size` (shares).

- **Important conventions to follow**:
  - `DRY_RUN` defaults to `true` (see `polybot/config.py`); many developers run `python main.py --dry-run` before `--live`.
  - Strategy implementations should not compute final sizes — return `size=0` and a textual `reason` for sizing logs (see `value_finder.py`).
  - Use `BUY` / `SELL` constants from `polybot.client`.
  - Logging is the primary observability method — don't replace with print statements; follow existing logging format.

- **Integration & external dependencies**:
  - Primary external API: Polymarket Gamma API (`cfg.gamma_host`) for market discovery and the CLOB (`py-clob-client`) for orders.
  - Credentials are provided via env: `POLY_PRIVATE_KEY`, `POLY_FUNDER_ADDRESS`, `POLY_CLOB_HOST`, `POLY_GAMMA_HOST`.
  - Network calls are in `polybot/client.py` (requests + `py-clob-client`) — tests or local runs may need network mocking.

- **Developer workflows / commands** (from `README.md`):
  - Install deps: `pip install -r requirements.txt`
  - Copy and edit credentials: `cp .env.example .env`
  - Dry-run: `python main.py --dry-run` (or set `DRY_RUN=true` in `.env`)
  - Live: `python main.py --live` (5s safety pause in `main.py`)

- **Files worth reading for PRs or patches**:
  - `polybot/bot.py` — how strategies are selected and the run loop.
  - `polybot/client.py` — any change touching order placement or book parsing should go here.
  - `polybot/risk_manager.py` and `polybot/executor.py` — changes that alter sizing or execution flows.
  - `polybot/strategies/*.py` — new strategies should subclass `Strategy` and follow the signal pattern.

- **Typical change patterns & examples**:
  - To add a strategy: create a new file in `polybot/strategies/`, implement `evaluate()` to return `Signal`s, and add it to `STRATEGIES` mapping in `polybot/bot.py`.
  - To modify order formatting (e.g., use different order type), update `place_limit_order` / `place_market_order` in `polybot/client.py` and keep logging consistent.
  - To alter sizing logic, change `RiskManager.size_signal()` — it accepts a `Signal` and returns a sized `Signal` or `None`.

- **Safety notes for AI edits** (must be enforced):
  - Never remove the `--live` 5-second safety pause in `main.py` without a clear test plan.
  - Changes touching `PolyClient` authentication or order submission require cautious review — they interact with real funds.
  - Prefer adding unit tests or small smoke-mode flags when changing execution paths.

If anything here is unclear or you want examples expanded (tests, mocks, or a sample new strategy), tell me which section to iterate on.
