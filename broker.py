"""Order execution. PaperBroker simulates fills; CcxtBroker talks to Binance (testnet or live).

Both return a Fill whose `cost` field means:
  buy : total quote currency spent, fees included
  sell: total quote currency received, fees deducted
and whose `qty` is the base-asset amount actually held (buy: net of fees paid in the base
asset, which is how Binance charges unless you pay with BNB) or actually sold.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from config import Config

log = logging.getLogger("bot.broker")


@dataclass
class Fill:
    price: float
    qty: float
    cost: float


class PaperBroker:
    name = "paper"

    def __init__(self, cfg: Config, cash: float):
        self.cfg, self.cash = cfg, cash

    def free_quote(self) -> float:
        return self.cash

    def min_notional(self, symbol: str) -> float:
        return self.cfg.min_notional

    def buy(self, symbol: str, quote: float, ref_price: float) -> Fill | None:
        price = ref_price * (1 + self.cfg.slippage)
        cost = quote * (1 + self.cfg.fee_rate)
        if cost > self.cash:
            return None
        self.cash -= cost
        return Fill(price, quote / price, cost)

    def sell(self, symbol: str, qty: float, ref_price: float) -> Fill | None:
        price = ref_price * (1 - self.cfg.slippage)
        proceeds = qty * price * (1 - self.cfg.fee_rate)
        self.cash += proceeds
        return Fill(price, qty, proceeds)


class CcxtBroker:
    """Spot market orders on Binance via ccxt. testnet=True uses testnet.binance.vision."""

    def __init__(self, cfg: Config, api_key: str, secret: str, testnet: bool):
        import ccxt  # lazy import
        self.cfg = cfg
        self.name = "testnet" if testnet else "live"
        self.ex = ccxt.binance({"apiKey": api_key, "secret": secret, "enableRateLimit": True,
                                "options": {"defaultType": "spot"}})
        if testnet:
            self.ex.set_sandbox_mode(True)
        self.ex.load_markets()

    def free_quote(self) -> float:
        return float(self.ex.fetch_balance()["free"].get(self.cfg.quote, 0.0) or 0.0)

    def min_notional(self, symbol: str) -> float:
        try:
            v = self.ex.market(symbol)["limits"]["cost"]["min"]
            return float(v) if v else self.cfg.min_notional
        except Exception:
            return self.cfg.min_notional

    # ---- helpers
    def _fees(self, order: dict, base: str, quote: str, avg: float, cost: float):
        """-> (fee paid in base, fee paid in quote). Unknown fee currencies (e.g. BNB) are
        approximated with the configured fee rate."""
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        fb = fq = 0.0
        for f in fees:
            c, cur = float(f.get("cost") or 0.0), f.get("currency")
            if cur == base:
                fb += c
            elif cur == quote:
                fq += c
            elif c:
                fq += cost * self.cfg.fee_rate
        return fb, fq

    def _complete(self, order: dict, symbol: str) -> dict:
        if order.get("average") is None or order.get("filled") is None:
            order = self.ex.fetch_order(order["id"], symbol)
        return order

    def buy(self, symbol: str, quote: float, ref_price: float) -> Fill | None:
        base, q = symbol.split("/")
        amount = float(self.ex.amount_to_precision(symbol, quote / ref_price))
        if amount <= 0:
            return None
        order = self._complete(self.ex.create_order(symbol, "market", "buy", amount), symbol)
        filled = float(order["filled"] or 0)
        if filled <= 0:
            log.error("buy %s not filled: %s", symbol, order)
            return None
        cost = float(order["cost"])
        avg = float(order["average"] or cost / filled)
        fb, fq = self._fees(order, base, q, avg, cost)
        return Fill(avg, filled - fb, cost + fq + fb * avg)

    def sell(self, symbol: str, qty: float, ref_price: float) -> Fill | None:
        base, q = symbol.split("/")
        free = float(self.ex.fetch_balance()["free"].get(base, 0.0) or 0.0)
        amount = float(self.ex.amount_to_precision(symbol, min(qty, free)))
        m = self.ex.market(symbol)
        min_amt = (m.get("limits", {}).get("amount", {}) or {}).get("min") or 0
        if amount <= 0 or amount < min_amt or amount * ref_price < self.min_notional(symbol):
            log.error("cannot sell %s: amount %s below exchange minimum (dust)", symbol, amount)
            return None
        order = self._complete(self.ex.create_order(symbol, "market", "sell", amount), symbol)
        filled = float(order["filled"] or 0)
        if filled <= 0:
            log.error("sell %s not filled: %s", symbol, order)
            return None
        cost = float(order["cost"])
        avg = float(order["average"] or cost / filled)
        _, fq = self._fees(order, base, q, avg, cost)
        return Fill(avg, filled, cost - fq)
