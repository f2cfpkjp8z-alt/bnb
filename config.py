"""Central configuration. Override any field with a config.json file next to this file."""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _default_weights() -> dict:
    # Heuristic weights (normalised to 1 at runtime). NOT optimised: tuning them on
    # the same data you test on is how you fool yourself. See README.
    return {
        "trend": 0.25,     # EMA9 > EMA21 > EMA50, price above EMA50
        "htf": 0.15,       # 1h trend up (only completed 1h candles are used)
        "momentum": 0.15,  # RSI in the "strong but not overbought" zone
        "macd": 0.10,      # MACD histogram positive and rising
        "breakout": 0.20,  # close above 20-bar high / upper Bollinger zone
        "volume": 0.15,    # volume spike (z-score)
    }


@dataclass
class Config:
    # ---- universe / scanning -------------------------------------------------
    quote: str = "USDT"
    min_quote_volume: float = 30_000_000   # 24h volume floor in USDT (liquidity)
    scan_top_n: int = 20                   # analyse the N most volatile liquid pairs
    stable_bases: tuple = ("USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR",
                           "USD1", "BUSD", "PYUSD", "XUSD", "USDE", "EURI", "UST")
    blacklist: tuple = ()                  # extra base assets to never trade, e.g. ("LUNC",)
    timeframe: str = "5m"
    htf: str = "1h"
    candles: int = 1000                    # candles fetched per symbol (Binance max 1000)

    # ---- signal --------------------------------------------------------------
    weights: dict = field(default_factory=_default_weights)
    entry_threshold: float = 0.70          # combined score needed (0..1)
    rsi_max: float = 78.0                  # never buy above this RSI
    boost_margin: float = 0.05             # news "boost" adds this to the score

    # ---- trade plan (ATR based) ---------------------------------------------
    sl_atr: float = 1.5
    tp_atr: float = 3.0
    trail_trigger_atr: float = 1.2         # start trailing after +1.2 ATR
    trail_atr: float = 1.0                 # trail 1 ATR below the highest price
    max_hold_bars: int = 72                # 72 x 5m = 6h time stop
    min_edge_multiple: float = 2.5         # TP distance must be >= 2.5x round-trip cost

    # ---- costs (assume the worst, not the best) ------------------------------
    fee_rate: float = 0.001                # 0.1% per side (0.075% if you pay with BNB)
    slippage: float = 0.0005               # 0.05% per side on market orders

    # ---- risk / sizing -------------------------------------------------------
    capital_limit: float = 100.0           # the bot never uses more than this (USDT)
    risk_per_trade: float = 0.015          # risk 1.5% of bot equity per trade
    max_position_pct: float = 0.34         # one position <= 34% of bot equity
    max_positions: int = 3
    daily_loss_cap: float = 0.05           # stop opening trades after -5% on the day
    max_trades_per_day: int = 8
    max_consec_losses: int = 3
    pause_minutes: int = 60                # pause after max_consec_losses
    symbol_cooldown_min: int = 60          # don't re-enter the same coin for 1h
    min_notional: float = 5.0              # fallback; real value read from exchange
    min_notional_buffer: float = 1.1

    # ---- filters -------------------------------------------------------------
    btc_filter: bool = True                # no new entries if BTC dropped fast
    btc_drop_pct: float = -1.5             # ... more than 1.5% over the last hour
    ob_gate: bool = True                   # order-book imbalance gate (live only)
    ob_min_ratio: float = 0.9              # bid_value / ask_value must be >= this
    ob_depth: int = 20

    # ---- news (Gemini Flash Lite) -------------------------------------------
    gemini_model: str = "gemini-2.5-flash-lite"
    news_ttl_seconds: int = 1800

    # ---- loop ----------------------------------------------------------------
    poll_seconds: int = 20                 # how often open positions are checked

    # ---- accounts / reporting / cloud ----------------------------------------
    sim_start_balance: float = 100.0       # the simulation account always starts here
    sim_profile: str = "balanced"          # conservative | balanced | aggressive
    live_profile: str = "conservative"     # the real account starts cautious
    db_path: str = "data/bot.db"           # SQLite report database (both accounts)
    status_push_seconds: int = 30          # how often status goes to the web app
    equity_snapshot_seconds: int = 300     # equity curve resolution
    command_max_age_seconds: int = 600     # web commands older than this are ignored
    max_watchlist: int = 15                # extra pairs the web app may add

    # ---- derived -------------------------------------------------------------
    @property
    def round_trip_cost(self) -> float:
        return 2 * (self.fee_rate + self.slippage)


def _load_env_file(path: Path) -> None:
    """Tiny .env reader (no dependency). Does not override real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: Path | None = None) -> Config:
    _load_env_file(BASE_DIR / ".env")
    cfg = Config()
    path = path or (BASE_DIR / "config.json")
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        types = {f.name: f.type for f in dataclasses.fields(Config)}
        for k, v in raw.items():
            if k not in types:
                raise KeyError(f"Unknown config key in {path.name}: {k}")
            if isinstance(getattr(cfg, k), tuple):
                v = tuple(v)
            setattr(cfg, k, v)
    return cfg
