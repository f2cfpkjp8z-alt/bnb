"""Universe selection (which coins to look at) and per-coin analysis."""
from __future__ import annotations

import pandas as pd

from config import Config
from indicators import compute_features
from signals import score_frame


def ohlcv_to_df(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop(columns="ts").astype(float)


def select_universe(tickers: dict, markets: dict, cfg: Config) -> list[str]:
    """Liquid USDT spot pairs, ranked by 24h range (a cheap volatility proxy)."""
    ranked = []
    for sym, t in tickers.items():
        if not sym.endswith("/" + cfg.quote) or ":" in sym:
            continue
        m = markets.get(sym)
        if m is None or not m.get("active", True) or not m.get("spot", True):
            continue
        base = sym.split("/")[0]
        if base in cfg.stable_bases or base in cfg.blacklist:
            continue
        qv, hi, lo, last = (t.get("quoteVolume"), t.get("high"), t.get("low"), t.get("last"))
        if not qv or not hi or not lo or not last or qv < cfg.min_quote_volume:
            continue
        ranked.append(((hi - lo) / last, sym))
    ranked.sort(reverse=True)
    return [s for _, s in ranked[: cfg.scan_top_n]]


def analyze(rows: list, cfg: Config) -> pd.DataFrame | None:
    """rows: raw ccxt OHLCV including the still-forming last candle (which is dropped)."""
    if not rows or len(rows) < 120:
        return None
    df = ohlcv_to_df(rows[:-1])
    feats = compute_features(df, cfg.htf, cfg.timeframe)
    return score_frame(feats, cfg)
