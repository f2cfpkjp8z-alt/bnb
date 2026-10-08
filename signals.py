"""Multi-signal scoring. Long-only (spot). Same function is used by backtest and live bot."""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import Config


def components(f: pd.DataFrame) -> pd.DataFrame:
    """Each component is in [0, 1]. NaN (indicator warm-up) always scores 0."""
    c = pd.DataFrame(index=f.index)

    full = (f.ema_fast > f.ema_mid) & (f.ema_mid > f.ema_slow) & (f.close > f.ema_slow)
    half = (f.ema_fast > f.ema_mid) & (f.close > f.ema_mid)
    c["trend"] = np.select([full, half], [1.0, 0.5], 0.0)

    c["htf"] = f.htf_up.astype(float)

    r = f.rsi
    c["momentum"] = np.select(
        [(r >= 52) & (r <= 70), ((r > 70) & (r <= 76)) | ((r >= 45) & (r < 52))],
        [1.0, 0.5], 0.0)

    h = f.macd_hist
    c["macd"] = np.select([(h > 0) & (h > h.shift(1)), h > 0], [1.0, 0.5], 0.0)

    c["breakout"] = np.select([f.close > f.hh20, f.bb_pct > 0.8], [1.0, 0.5], 0.0)

    c["volume"] = (f.vol_z / 3.0).clip(0, 1).fillna(0.0)
    return c


def score_frame(f: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Returns a frame with: score (0..1), gates (bool), signal (bool), atr, atr_pct, close."""
    comp = components(f)
    w = pd.Series(cfg.weights, dtype=float)
    w = w / w.sum()
    score = (comp[list(w.index)] * w).sum(axis=1)

    # Hard gates: must hold regardless of how high the score is.
    edge_ok = (f.atr_pct * cfg.tp_atr) >= cfg.min_edge_multiple * cfg.round_trip_cost
    gates = (comp["trend"] > 0) & (f.rsi < cfg.rsi_max) & edge_ok

    out = f[["close", "atr", "atr_pct", "rsi"]].copy()
    out["score"] = score
    out["gates"] = gates.fillna(False)
    out["signal"] = out["gates"] & (score >= cfg.entry_threshold)
    for k in comp.columns:
        out[f"c_{k}"] = comp[k]
    return out


def btc_market_ok(btc_close: pd.Series, cfg: Config, bars_per_hour: int) -> pd.Series:
    """False while BTC is dumping (fell more than btc_drop_pct over the last hour)."""
    if not cfg.btc_filter:
        return pd.Series(True, index=btc_close.index)
    ret = (btc_close / btc_close.shift(bars_per_hour) - 1.0) * 100.0
    return ~(ret < cfg.btc_drop_pct).fillna(False)
