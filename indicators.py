"""Vectorised indicators. Every value at row i uses only data up to and including row i
(no look-ahead), so the same code is valid for backtests and for live trading."""
from __future__ import annotations

import numpy as np
import pandas as pd


def tf_to_pandas(tf: str) -> str:
    """'5m' -> '5min', '1h' -> '1h', '1d' -> '1D' (pandas offset aliases)."""
    n, unit = tf[:-1], tf[-1]
    return {"m": f"{n}min", "h": f"{n}h", "d": f"{n}D"}[unit]


def tf_seconds(tf: str) -> int:
    n, unit = int(tf[:-1]), tf[-1]
    return n * {"m": 60, "h": 3600, "d": 86400}[unit]


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + gain / loss)
    out = out.where(loss != 0, 100.0)
    return out.where(gain.notna())


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def compute_features(df: pd.DataFrame, htf: str = "1h", tf: str = "5m") -> pd.DataFrame:
    """df: columns open, high, low, close, volume; UTC DatetimeIndex of CLOSED candles."""
    f = pd.DataFrame(index=df.index)
    c = df["close"]
    f["close"] = c
    f["open"], f["high"], f["low"] = df["open"], df["high"], df["low"]
    f["ema_fast"], f["ema_mid"], f["ema_slow"] = ema(c, 9), ema(c, 21), ema(c, 50)
    f["rsi"] = rsi(c, 14)

    macd = ema(c, 12) - ema(c, 26)
    f["macd_hist"] = macd - macd.ewm(span=9, adjust=False, min_periods=9).mean()

    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    upper, lower = mid + 2 * sd, mid - 2 * sd
    f["bb_pct"] = (c - lower) / (upper - lower).replace(0, np.nan)

    f["atr"] = atr(df, 14)
    f["atr_pct"] = f["atr"] / c
    f["hh20"] = df["high"].rolling(20).max().shift(1)   # previous 20 highs, excl. current
    v = df["volume"]
    f["vol_z"] = (v - v.rolling(50).mean()) / v.rolling(50).std().replace(0, np.nan)

    # Higher-timeframe trend, using ONLY completed higher-TF candles (no look-ahead):
    # an htf candle labelled H closes at H+htf, so its value becomes usable at H+htf.
    h = c.resample(tf_to_pandas(htf)).last().dropna()
    h_ema = ema(h, 50)
    h_up = ((h > h_ema) & (h_ema > h_ema.shift(3))).astype(float)
    h_up.index = h_up.index + pd.to_timedelta(tf_to_pandas(htf))
    f["htf_up"] = h_up.reindex(f.index.union(h_up.index)).ffill().reindex(f.index).fillna(0.0)
    return f
