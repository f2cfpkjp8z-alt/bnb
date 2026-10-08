"""One trading account ('sim' = paper money, 'live' = real/testnet Binance money).

Each account has its own balance, positions, risk limits, aggressiveness profile and
on/off switch. Both read the same real-time market data; only the broker differs.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from config import Config
from indicators import tf_seconds
from profiles import PROFILES, apply_profile
from risk import (Position, RiskManager, evaluate_exit, plan_trade, size_quote,
                  update_trail)

log = logging.getLogger("bot.account")


class Account:
    def __init__(self, name: str, venue: str, base_cfg: Config, profile: str, broker,
                 start_equity: float, ctx, state_dir: Path, enabled: bool = True):
        self.name, self.venue, self.broker, self.ctx = name, venue, broker, ctx
        self.base_cfg = base_cfg
        self.profile = profile
        self.cfg = apply_profile(base_cfg, profile)
        self.start_equity = float(start_equity)
        self.enabled = enabled
        self.positions: dict[str, Position] = {}
        self.risk = RiskManager(self.cfg)
        self.realized = 0.0
        self.sell_failures: dict[str, int] = {}
        self.tf_secs = tf_seconds(base_cfg.timeframe)
        self.state_path = Path(state_dir) / f"{name}.json"
        self.verdicts: dict[str, str] = {}       # symbol -> latest verdict (for the web board)
        self._trail_logged: dict[str, float] = {}

    # ------------------------------------------------------------------ persistence
    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        d = {"profile": self.profile, "enabled": self.enabled, "realized": self.realized,
             "risk": self.risk.to_dict(),
             "positions": {s: p.to_dict() for s, p in self.positions.items()}}
        if hasattr(self.broker, "cash"):
            d["paper_cash"] = self.broker.cash
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        os.replace(tmp, self.state_path)

    def load(self) -> bool:
        if not self.state_path.exists():
            return False
        d = json.loads(self.state_path.read_text())
        if d.get("profile") in PROFILES:
            self.set_profile(d["profile"], announce=False)
        self.enabled = d.get("enabled", self.enabled)
        self.realized = d.get("realized", 0.0)
        self.risk.load(d.get("risk", {}))
        self.positions = {s: Position.from_dict(p) for s, p in d.get("positions", {}).items()}
        if hasattr(self.broker, "cash") and "paper_cash" in d:
            self.broker.cash = d["paper_cash"]
        return True

    # ------------------------------------------------------------------ settings
    def set_profile(self, name: str, announce: bool = True) -> None:
        self.cfg = apply_profile(self.base_cfg, name)
        self.risk.cfg = self.cfg
        self.profile = name
        if announce:
            self.ctx.emit("setting", f"{self.name}: aggressiveness set to '{name}'",
                          account=self.name, data={"profile": name})
        self.save()

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        self.ctx.emit("setting", f"{self.name}: new entries turned {'ON' if on else 'OFF'}",
                      account=self.name, data={"enabled": self.enabled})
        self.save()

    def reset(self) -> None:
        """Simulation only: back to the starting balance with no positions."""
        assert self.venue == "paper", "only the simulation account can be reset"
        self.positions.clear()
        self.realized = 0.0
        self.broker.cash = self.start_equity
        self.risk = RiskManager(self.cfg)
        self.verdicts.clear()
        self.save()
        self.ctx.emit("setting", f"{self.name}: reset to {self.start_equity:.2f} USDT",
                      account=self.name)

    # ------------------------------------------------------------------ accounting
    def invested(self) -> float:
        return sum(p.cost for p in self.positions.values())

    def unrealized(self, prices: dict[str, float]) -> float:
        fee = self.cfg.fee_rate
        return sum(p.qty * prices.get(s, p.entry) * (1 - fee) - p.cost
                   for s, p in self.positions.items())

    def equity(self, prices: dict[str, float] | None = None) -> float:
        return self.start_equity + self.realized + self.unrealized(prices or {})

    def free_cash(self) -> float:
        budget = self.start_equity + self.realized - self.invested()
        return max(0.0, min(budget, self.broker.free_quote()))

    # ------------------------------------------------------------------ exits
    def manage(self, bids: dict[str, float], now: float) -> None:
        for sym, pos in list(self.positions.items()):
            px = bids.get(sym)
            if not px:
                continue
            held = int((now - pos.opened_ts) / self.tf_secs)
            res = evaluate_exit(pos, px, px, px, px, held, self.cfg)
            if res is None:
                before = pos.stop
                update_trail(pos, px, self.cfg)
                if pos.stop > before:
                    last = self._trail_logged.get(sym, before)
                    if pos.stop / last - 1 > 0.002:
                        self._trail_logged[sym] = pos.stop
                        self.ctx.emit("trail", f"{self.name}: {sym} trailing stop raised to "
                                      f"{pos.stop:.6g}", account=self.name, symbol=sym)
                    self.save()
                continue
            reason = res[1]
            if reason == "stop_gap":
                reason = "stop" if pos.stop < pos.entry else "trail"
            self.close(sym, px, reason)

    def close(self, sym: str, px: float, reason: str) -> bool:
        pos = self.positions.get(sym)
        if pos is None:
            return False
        fill = self.broker.sell(sym, pos.qty, px)
        if fill is None:
            n = self.sell_failures[sym] = self.sell_failures.get(sym, 0) + 1
            self.ctx.emit("error", f"{self.name}: SELL FAILED for {sym} (attempt {n})",
                          account=self.name, symbol=sym, level="error")
            if n >= 5:
                self.ctx.emit("error", f"{self.name}: giving up on {sym} after 5 failed sells - "
                              "CHECK BINANCE MANUALLY. Removed from the bot's books.",
                              account=self.name, symbol=sym, level="error")
                del self.positions[sym]
                self.save()
            return False
        now = time.time()
        pnl = fill.cost - pos.cost
        self.realized += pnl
        self.risk.on_close(sym, pnl, now)
        del self.positions[sym]
        self.sell_failures.pop(sym, None)
        self._trail_logged.pop(sym, None)
        self.ctx.record_trade(self, pos, fill, pnl, reason, now)
        self.save()
        return True

    def close_all(self, bids: dict[str, float], reason: str = "manual") -> None:
        for sym, pos in list(self.positions.items()):
            self.close(sym, bids.get(sym) or pos.entry, reason)

    # ------------------------------------------------------------------ entries
    def gate(self, now: float, eq: float) -> tuple[bool, str]:
        """Account-level reasons not to open anything right now."""
        if not self.enabled:
            return False, "entries are OFF for this account"
        if len(self.positions) >= self.cfg.max_positions:
            return False, f"{len(self.positions)}/{self.cfg.max_positions} positions already open"
        return self.risk.can_open(now, eq)

    def try_open(self, sym: str, ref: float, atr: float, score: float, eq: float,
                 now: float) -> tuple[bool, str]:
        ok, why = self.risk.can_open(now, eq, sym)
        if not ok:
            return False, why
        plan = plan_trade(ref, atr, self.cfg)
        if not plan:
            return False, "profit target too small to beat trading costs"
        _, _, stop_pct = plan
        size = size_quote(eq, self.free_cash(), stop_pct, self.cfg, self.broker.min_notional(sym))
        if size <= 0:
            return False, "position would be below the exchange minimum or not enough free cash"
        fill = self.broker.buy(sym, size, ref)
        if fill is None:
            return False, "order failed"
        pos = Position(sym, fill.price, fill.qty, fill.price - self.cfg.sl_atr * atr,
                       fill.price + self.cfg.tp_atr * atr, atr, now, fill.price, fill.cost, score)
        self.positions[sym] = pos
        self.risk.on_open(now)
        self.save()
        self.ctx.record_open(self, pos)
        return True, ""

    # ------------------------------------------------------------------ reporting
    def snapshot(self, prices: dict[str, float], now: float) -> dict:
        eq = self.equity(prices)
        unreal = self.unrealized(prices)
        rows = []
        for s, p in self.positions.items():
            px = prices.get(s, p.entry)
            rows.append({"symbol": s, "qty": p.qty, "entry": p.entry, "price": px,
                         "pnl_pct": (px / p.entry - 1) * 100,
                         "pnl": p.qty * px * (1 - self.cfg.fee_rate) - p.cost,
                         "stop": p.stop, "tp": p.tp, "opened_ts": p.opened_ts, "score": p.score})
        return {
            "venue": self.venue, "profile": self.profile, "enabled": self.enabled,
            "start": self.start_equity, "equity": eq, "cash": max(0.0, self.start_equity + self.realized - self.invested()),
            "realized": self.realized, "unrealized": unreal,
            "pnl": eq - self.start_equity, "pnl_pct": (eq / self.start_equity - 1) * 100,
            "invested": self.invested(), "open": rows,
            "today": {"pnl": self.risk.day_pnl, "trades": self.risk.trades_today},
            "paused_until": self.risk.pause_until if self.risk.pause_until > now else 0,
            "limits": {"entry_threshold": self.cfg.entry_threshold,
                       "risk_per_trade": self.cfg.risk_per_trade,
                       "max_positions": self.cfg.max_positions,
                       "max_trades_per_day": self.cfg.max_trades_per_day,
                       "daily_loss_cap": self.cfg.daily_loss_cap},
        }
