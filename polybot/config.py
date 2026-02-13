"""Bot configuration loaded from environment variables."""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Polymarket connection
    private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    funder_address: str = os.getenv("POLY_FUNDER_ADDRESS", "")
    signature_type: int = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
    chain_id: int = int(os.getenv("POLY_CHAIN_ID", "137"))
    clob_host: str = os.getenv("POLY_CLOB_HOST", "https://clob.polymarket.com")
    gamma_host: str = os.getenv("POLY_GAMMA_HOST", "https://gamma-api.polymarket.com")

    # Risk / sizing
    max_trade_size: float = float(os.getenv("MAX_TRADE_SIZE", "10.0"))
    max_total_exposure: float = float(os.getenv("MAX_TOTAL_EXPOSURE", "100.0"))
    min_edge: float = float(os.getenv("MIN_EDGE", "0.05"))

    # Bot behavior
    scan_interval: int = int(os.getenv("SCAN_INTERVAL", "60"))
    strategy: str = os.getenv("STRATEGY", "value_finder")
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Backtest settings
    backtest_days: int = int(os.getenv("BACKTEST_DAYS", "7"))
    backtest_bet_size: float = float(os.getenv("BACKTEST_BET_SIZE", "1.0"))
    backtest_min_confidence: float = float(os.getenv("BACKTEST_MIN_CONFIDENCE", "0.20"))

    def validate(self) -> list[str]:
        """Return a list of configuration errors (empty = valid)."""
        errors = []
        if not self.private_key:
            errors.append("POLY_PRIVATE_KEY is required")
        if not self.funder_address:
            errors.append("POLY_FUNDER_ADDRESS is required")
        if self.max_trade_size <= 0:
            errors.append("MAX_TRADE_SIZE must be positive")
        if self.max_total_exposure <= 0:
            errors.append("MAX_TOTAL_EXPOSURE must be positive")
        return errors
