"""Aggressiveness profiles.

A profile only changes HOW MUCH the bot risks and how picky it is. It never changes the
indicators, stops or targets, so one shared market scan serves every account.

HARD_LIMITS are enforced no matter where a value comes from (web app, config.json, code).
Raising them means editing this file on the PC, never from the web app.
"""
from __future__ import annotations

import dataclasses

from config import Config

PROFILES: dict[str, dict] = {
    "conservative": dict(entry_threshold=0.78, risk_per_trade=0.010, max_position_pct=0.30,
                         max_positions=2, max_trades_per_day=4, daily_loss_cap=0.03,
                         symbol_cooldown_min=90),
    "balanced":     dict(entry_threshold=0.70, risk_per_trade=0.015, max_position_pct=0.34,
                         max_positions=3, max_trades_per_day=8, daily_loss_cap=0.05,
                         symbol_cooldown_min=60),
    "aggressive":   dict(entry_threshold=0.62, risk_per_trade=0.025, max_position_pct=0.40,
                         max_positions=4, max_trades_per_day=15, daily_loss_cap=0.08,
                         symbol_cooldown_min=30),
}

# (min, max) for every value a profile may set.
HARD_LIMITS: dict[str, tuple[float, float]] = {
    "entry_threshold": (0.55, 0.95),
    "risk_per_trade": (0.002, 0.03),
    "max_position_pct": (0.05, 0.50),
    "max_positions": (1, 5),
    "max_trades_per_day": (1, 25),
    "daily_loss_cap": (0.01, 0.10),
    "symbol_cooldown_min": (5, 720),
}

DEFAULT_PROFILE = "balanced"


INT_FIELDS = ("max_positions", "max_trades_per_day", "symbol_cooldown_min")


def clamp(cfg: Config) -> Config:
    for k, (lo, hi) in HARD_LIMITS.items():
        v = min(max(getattr(cfg, k), lo), hi)
        setattr(cfg, k, int(v) if k in INT_FIELDS else float(v))
    return cfg


def apply_profile(base: Config, name: str) -> Config:
    """Returns a NEW Config with the profile applied and hard limits enforced."""
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name!r}; choose one of {list(PROFILES)}")
    return clamp(dataclasses.replace(base, **PROFILES[name]))


def describe(name: str) -> str:
    p = PROFILES[name]
    return (f"{name}: buy score >= {p['entry_threshold']:.2f}, risk {p['risk_per_trade']*100:.1f}%/trade, "
            f"max {p['max_positions']} positions, {p['max_trades_per_day']} trades/day, "
            f"daily stop -{p['daily_loss_cap']*100:.0f}%")
