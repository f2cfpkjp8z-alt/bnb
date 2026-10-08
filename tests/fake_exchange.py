"""Minimal stand-in for a ccxt exchange so the bot loop can be tested offline."""
from __future__ import annotations

import time

import pandas as pd

from backtest import make_synthetic


class FakeExchange:
    def __init__(self, n_syms: int = 6, seed: int = 1, vol: float = 0.004):
        raw = make_synthetic(n_syms, days=5, seed=seed, vol=vol)
        now = pd.Timestamp(int(time.time() // 300 * 300), unit="s", tz="UTC")  # forming candle
        self.data, self.override = {}, {}
        for k, (_, df) in enumerate(raw.items()):
            df = df.copy()
            df.index = pd.date_range(end=now, periods=len(df), freq="5min")
            self.data["BTC/USDT" if k == 0 else f"COIN{k}/USDT"] = df
        self.markets = {s: {"active": True, "spot": True} for s in self.data}

    # --- helpers for tests
    def last_close(self, sym):
        return float(self.data[sym]["close"].iloc[-2])     # last CLOSED candle

    def set_price(self, sym, px):
        self.override[sym] = px

    # --- ccxt-like API
    def load_markets(self):
        return self.markets

    def milliseconds(self):
        return int(time.time() * 1000)

    def _ticker(self, sym):
        df = self.data[sym]
        px = self.override.get(sym, self.last_close(sym))
        w = df.iloc[-289:]
        return {"symbol": sym, "last": px, "bid": px * 0.9999, "ask": px * 1.0001,
                "high": float(w["high"].max()), "low": float(w["low"].min()), "quoteVolume": 5e8}

    def fetch_tickers(self, symbols=None):
        return {s: self._ticker(s) for s in (symbols or self.data)}

    def fetch_ohlcv(self, sym, tf, since=None, limit=1000):
        df = self.data[sym].iloc[-limit:]
        return [[int(t.timestamp() * 1000), r.open, r.high, r.low, r.close, r.volume]
                for t, r in zip(df.index, df.itertuples())]

    def fetch_order_book(self, sym, limit=20):
        px = self.override.get(sym, self.last_close(sym))
        return {"bids": [[px * (1 - 0.0001 * (i + 1)), 100.0] for i in range(limit)],
                "asks": [[px * (1 + 0.0001 * (i + 1)), 80.0] for i in range(limit)]}
