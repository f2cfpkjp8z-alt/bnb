"""Position planning, sizing, exit rules and account-level risk limits.
Used identically by the backtester and the live bot."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

from config import Config


@dataclass
class Position:
    symbol: str
    entry: float
    qty: float
    stop: float
    tp: float
    atr: float
    opened_ts: float            # epoch seconds
    highest: float
    cost: float = 0.0           # quote currency spent incl. fee
    score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Position":
        return Position(**d)


def plan_trade(entry: float, atr: float, cfg: Config):
    """Returns (stop, tp, stop_pct) or None if the plan is not viable."""
    if not (entry > 0 and atr > 0):
        return None
    stop = entry - cfg.sl_atr * atr
    tp = entry + cfg.tp_atr * atr
    if stop <= 0:
        return None
    tp_pct = (tp - entry) / entry
    if tp_pct < cfg.min_edge_multiple * cfg.round_trip_cost:
        return None                          # target too small to beat costs
    return stop, tp, (entry - stop) / entry


def size_quote(equity: float, cash: float, stop_pct: float, cfg: Config,
               min_notional: float) -> float:
    """Quote amount (USDT) to spend. 0 means 'skip this trade'."""
    risk_amt = equity * cfg.risk_per_trade
    size = risk_amt / (stop_pct + cfg.round_trip_cost)
    size = min(size, equity * cfg.max_position_pct, cash / (1 + cfg.fee_rate) * 0.98)
    if size < min_notional * cfg.min_notional_buffer:
        return 0.0
    return size


def evaluate_exit(pos: Position, o: float, h: float, l: float, c: float,
                  bars_held: int, cfg: Config):
    """Returns (raw_exit_price, reason) or None. Pessimistic: if stop and target are both
    inside one candle, the stop is assumed to hit first."""
    if o <= pos.stop:
        return o, "stop_gap"
    if l <= pos.stop:
        return pos.stop, "stop" if pos.stop < pos.entry else "trail"
    if h >= pos.tp:
        return max(o, pos.tp), "tp"
    if bars_held >= cfg.max_hold_bars:
        return c, "time"
    return None


def update_trail(pos: Position, h: float, cfg: Config) -> None:
    pos.highest = max(pos.highest, h)
    if pos.highest >= pos.entry + cfg.trail_trigger_atr * pos.atr:
        pos.stop = max(pos.stop, pos.highest - cfg.trail_atr * pos.atr)


class RiskManager:
    """Daily loss cap, trade-count cap, loss-streak pause and per-symbol cooldown."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.day: str | None = None
        self.day_start_equity = 0.0
        self.day_pnl = 0.0
        self.trades_today = 0
        self.consec_losses = 0
        self.pause_until = 0.0
        self.cooldowns: dict[str, float] = {}

    @staticmethod
    def _day(ts: float) -> str:
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")

    def roll_day(self, ts: float, equity: float) -> None:
        d = self._day(ts)
        if d != self.day:
            self.day, self.day_start_equity = d, equity
            self.day_pnl, self.trades_today = 0.0, 0

    def can_open(self, ts: float, equity: float, symbol: str | None = None):
        self.roll_day(ts, equity)
        c = self.cfg
        if self.day_pnl <= -c.daily_loss_cap * self.day_start_equity:
            return False, "daily loss cap reached"
        if self.trades_today >= c.max_trades_per_day:
            return False, "max trades per day reached"
        if ts < self.pause_until:
            return False, "paused after losing streak"
        if symbol and ts < self.cooldowns.get(symbol, 0.0):
            return False, f"{symbol} cooling down"
        return True, ""

    def on_open(self, ts: float) -> None:
        self.trades_today += 1

    def on_close(self, symbol: str, pnl: float, ts: float) -> None:
        self.day_pnl += pnl
        self.cooldowns[symbol] = ts + self.cfg.symbol_cooldown_min * 60
        if pnl < 0:
            self.consec_losses += 1
            if self.consec_losses >= self.cfg.max_consec_losses:
                self.pause_until = ts + self.cfg.pause_minutes * 60
                self.consec_losses = 0
        else:
            self.consec_losses = 0

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "day", "day_start_equity", "day_pnl", "trades_today",
            "consec_losses", "pause_until", "cooldowns")}

    def load(self, d: dict) -> None:
        for k, v in d.items():
            setattr(self, k, v)
