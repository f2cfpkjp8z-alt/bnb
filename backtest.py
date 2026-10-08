"""Portfolio backtester that reuses the exact scoring / risk / exit code of the live bot.

Realism choices (all deliberately pessimistic):
  * signal is computed on a CLOSED candle, entry happens at the NEXT candle's open
  * fees + slippage on every market order
  * if stop and target are both inside one candle, the stop is assumed to hit first
  * same position-limits, daily-loss cap, loss-streak pause and cooldowns as live

Known limits you must keep in mind:
  * survivorship bias: the universe is *today's* volatile coins, not the coins that were
    volatile back then
  * a good backtest is not a promise. Trust the OUT-OF-SAMPLE block, never the in-sample one.

Usage:
  python backtest.py --days 60 --top 15
  python backtest.py --symbols BTC/USDT,ETH/USDT,SOL/USDT --days 90 --threshold 0.75
  python backtest.py --synthetic            # offline smoke test on random-walk data
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import time

import numpy as np
import pandas as pd

from config import BASE_DIR, Config, load_config
from indicators import compute_features, tf_seconds
from risk import (Position, RiskManager, evaluate_exit, plan_trade, size_quote,
                  update_trail)
from scanner import ohlcv_to_df, select_universe
from signals import btc_market_ok, score_frame

DATA_DIR = BASE_DIR / "data"
RESULTS_DIR = BASE_DIR / "results"
WARMUP_BARS = 800   # hourly EMA50 + slope needs ~53h of 5m bars before signals are valid


# --------------------------------------------------------------------------- data
def download(ex, symbol: str, tf: str, days: int, max_age_h: float = 12.0) -> pd.DataFrame:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{symbol.replace('/', '_')}_{tf}_{days}d.csv"
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_h * 3600:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    since = ex.milliseconds() - days * 86400 * 1000
    rows: list = []
    while True:
        batch = ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not batch:
            break
        rows += batch
        nxt = batch[-1][0] + 1
        if len(batch) < 1000 or nxt <= since:
            break
        since = nxt
    df = ohlcv_to_df(rows)
    df = df[~df.index.duplicated()].sort_index().iloc[:-1]   # drop forming candle
    df.to_csv(path)
    return df


def make_synthetic(n_syms: int = 8, days: int = 30, seed: int = 0, drift: float = 0.0,
                   vol: float = 0.0030) -> dict[str, pd.DataFrame]:
    """Random-walk candles with volatility clustering and volume spikes."""
    rng = np.random.default_rng(seed)
    n = days * 288
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    out = {}
    for k in range(n_syms):
        noise = pd.Series(rng.normal(0, 1, n)).ewm(span=200).mean().to_numpy() * 4
        scale = vol * np.exp(noise)
        ret = rng.standard_t(4, n) / np.sqrt(2) * scale + drift
        close = 100 * np.exp(np.cumsum(ret))
        open_ = np.r_[close[0], close[:-1]]
        wick = np.abs(rng.normal(0, 1, (2, n))) * scale * 0.6
        high = np.maximum(open_, close) * (1 + wick[0])
        low = np.minimum(open_, close) * (1 - wick[1])
        volume = rng.lognormal(5, 0.6, n) * (1 + 4 * (rng.random(n) < 0.03))
        out[f"SYN{k}/USDT"] = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)
    return out


# --------------------------------------------------------------------------- prep
def prepare(raw: dict[str, pd.DataFrame], btc: pd.DataFrame | None, cfg: Config):
    ok = None
    if btc is not None and cfg.btc_filter:
        bph = 3600 // tf_seconds(cfg.timeframe)
        ok = btc_market_ok(btc["close"], cfg, bph).astype(float)
    frames = {}
    for sym, df in raw.items():
        sf = score_frame(compute_features(df, cfg.htf, cfg.timeframe), cfg)
        sf[["open", "high", "low"]] = df[["open", "high", "low"]]
        if ok is not None:
            m = ok.reindex(sf.index, method="ffill").fillna(1.0) > 0.5
            sf["signal"] = sf["signal"] & m
        frames[sym] = sf
    return frames


# --------------------------------------------------------------------------- engine
def simulate(frames: dict[str, pd.DataFrame], cfg: Config, equity0: float = 100.0,
             start=None, end=None):
    syms = list(frames)
    idx = frames[syms[0]].index
    for s in syms[1:]:
        idx = idx.union(frames[s].index)
    if start is not None:
        idx = idx[idx >= start]
    if end is not None:
        idx = idx[idx < end]
    T = len(idx)
    A = {}
    for s in syms:
        f = frames[s].reindex(idx)
        A[s] = {k: f[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "score", "atr")}
        A[s]["sig"] = f["signal"].astype(float).fillna(0.0).to_numpy() > 0.5

    fee, slip = cfg.fee_rate, cfg.slippage
    rm = RiskManager(cfg)
    cash = equity0
    open_: dict[str, tuple[Position, int]] = {}
    last_c = {s: np.nan for s in syms}
    trades, eq = [], []

    def equity_now() -> float:
        return cash + sum(p.qty * last_c[s] for s, (p, _) in open_.items()
                          if not np.isnan(last_c[s]))

    def close_pos(s, pos, raw_px, reason, i):
        nonlocal cash
        fill = raw_px * (1 - slip)
        proceeds = pos.qty * fill * (1 - fee)
        pnl = proceeds - pos.cost
        cash += proceeds
        trades.append({
            "symbol": s, "entry_ts": pd.Timestamp(pos.opened_ts, unit="s", tz="UTC"),
            "exit_ts": idx[i], "entry": pos.entry, "exit": fill, "qty": pos.qty,
            "cost": pos.cost, "pnl": pnl, "pnl_pct": pnl / pos.cost * 100,
            "fees": (pos.cost - pos.qty * pos.entry) + pos.qty * fill * fee,
            "reason": reason, "score": pos.score,
            "bars": i - open_[s][1]})
        rm.on_close(s, pnl, idx[i].timestamp())
        del open_[s]

    def process_bar(s, pos, ei, i):
        a = A[s]
        res = evaluate_exit(pos, a["open"][i], a["high"][i], a["low"][i], a["close"][i],
                            i - ei, cfg)
        if res:
            close_pos(s, pos, res[0], res[1], i)
        else:
            update_trail(pos, a["high"][i], cfg)

    for i in range(T):
        ts = idx[i].timestamp()
        for s in syms:
            c = A[s]["close"][i]
            if not np.isnan(c):
                last_c[s] = c

        for s, (pos, ei) in list(open_.items()):
            if not np.isnan(A[s]["close"][i]):
                process_bar(s, pos, ei, i)

        if i >= 1 and len(open_) < cfg.max_positions:
            eqn = equity_now()
            cands = sorted(((A[s]["score"][i - 1], s) for s in syms
                            if s not in open_ and A[s]["sig"][i - 1]
                            and not np.isnan(A[s]["open"][i])), reverse=True)
            for sc, s in cands:
                if len(open_) >= cfg.max_positions:
                    break
                if not rm.can_open(ts, eqn, s)[0]:
                    continue
                a = A[s]
                entry = a["open"][i] * (1 + slip)
                plan = plan_trade(entry, a["atr"][i - 1], cfg)
                if not plan:
                    continue
                stop, tp, stop_pct = plan
                size = size_quote(eqn, cash, stop_pct, cfg, cfg.min_notional)
                if size <= 0:
                    continue
                cost = size * (1 + fee)
                cash -= cost
                pos = Position(s, entry, size / entry, stop, tp, a["atr"][i - 1], ts,
                               entry, cost, float(sc))
                open_[s] = (pos, i)
                rm.on_open(ts)
                process_bar(s, pos, i, i)      # the entry candle can already hit stop/target

        eq.append(equity_now())

    for s, (pos, ei) in list(open_.items()):    # liquidate leftovers at the end
        close_pos(s, pos, last_c[s], "end", T - 1)
    if T:
        eq[-1] = cash
    return pd.DataFrame(trades), pd.Series(eq, index=idx, name="equity")


# --------------------------------------------------------------------------- report
def metrics(trades: pd.DataFrame, eq: pd.Series, equity0: float, frames, idx_range) -> dict:
    m = {"trades": len(trades)}
    days = max((eq.index[-1] - eq.index[0]).total_seconds() / 86400, 1e-9) if len(eq) else 0
    m["days"] = days
    m["final_equity"] = float(eq.iloc[-1]) if len(eq) else equity0
    m["return_pct"] = (m["final_equity"] / equity0 - 1) * 100
    if len(eq):
        m["max_dd_pct"] = float(((eq / eq.cummax()) - 1).min() * 100)
    if len(trades):
        wins, losses = trades[trades.pnl > 0], trades[trades.pnl <= 0]
        m["win_rate_pct"] = len(wins) / len(trades) * 100
        m["avg_trade_pct"] = trades.pnl_pct.mean()
        gl = -losses.pnl.sum()
        m["profit_factor"] = (wins.pnl.sum() / gl) if gl > 0 else float("inf")
        m["fees_paid"] = trades.fees.sum()
        m["avg_hold_min"] = trades.bars.mean() * tf_seconds("5m") / 60
        m["trades_per_day"] = len(trades) / max(days, 1e-9)
        m["exits"] = trades.reason.value_counts().to_dict()
    # benchmark: equal-weight buy & hold of the same coins over the same window
    rets = []
    for f in frames.values():
        c = f["close"].reindex(idx_range).dropna()
        if len(c) > 1:
            rets.append(c.iloc[-1] / c.iloc[0] - 1)
    m["buy_hold_pct"] = float(np.mean(rets) * 100) if rets else float("nan")
    return m


def print_report(name: str, m: dict) -> None:
    print(f"\n=== {name} ({m['days']:.1f} days) ===")
    if m["trades"] == 0:
        print("  no trades")
        return
    print(f"  trades: {m['trades']}  ({m['trades_per_day']:.1f}/day)   avg hold: {m['avg_hold_min']:.0f} min")
    print(f"  win rate: {m['win_rate_pct']:.1f}%   avg trade: {m['avg_trade_pct']:+.2f}%   "
          f"profit factor: {m['profit_factor']:.2f}")
    print(f"  return: {m['return_pct']:+.2f}%   max drawdown: {m['max_dd_pct']:.2f}%   "
          f"fees paid: ${m['fees_paid']:.2f}")
    print(f"  same coins, just holding them: {m['buy_hold_pct']:+.2f}%")
    print(f"  exits: {m['exits']}")


# --------------------------------------------------------------------------- cli
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--top", type=int, default=15, help="auto-pick N volatile liquid coins")
    p.add_argument("--symbols", help="comma list, e.g. BTC/USDT,ETH/USDT")
    p.add_argument("--equity", type=float, default=100.0)
    p.add_argument("--split", type=float, default=0.6, help="in-sample fraction")
    p.add_argument("--threshold", type=float)
    p.add_argument("--fee", type=float)
    p.add_argument("--slippage", type=float)
    p.add_argument("--synthetic", action="store_true", help="offline random-walk smoke test")
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args(argv)

    cfg = load_config()
    for k, v in (("entry_threshold", a.threshold), ("fee_rate", a.fee), ("slippage", a.slippage)):
        if v is not None:
            setattr(cfg, k, v)

    btc = None
    if a.synthetic:
        raw = make_synthetic(days=a.days if a.days <= 90 else 30, seed=a.seed)
        print(f"Synthetic random-walk data, {len(raw)} coins (no real edge exists in this data).")
    else:
        import ccxt  # lazy: only needed for real data
        ex = ccxt.binance({"enableRateLimit": True})
        ex.load_markets()
        if a.symbols:
            syms = [s.strip() for s in a.symbols.split(",")]
        else:
            syms = select_universe(ex.fetch_tickers(), ex.markets, cfg)[: a.top]
        print(f"Downloading {a.days}d of {cfg.timeframe} candles for {len(syms)} coins...")
        raw = {s: download(ex, s, cfg.timeframe, a.days) for s in syms}
        if cfg.btc_filter:
            btc = raw.get("BTC/USDT")
            if btc is None:
                btc = download(ex, "BTC/USDT", cfg.timeframe, a.days)

    frames = prepare(raw, btc, cfg)
    idx = frames[next(iter(frames))].index
    for f in frames.values():
        idx = idx.union(f.index)
    if len(idx) <= WARMUP_BARS + 500:
        print("Not enough history. Use more --days.")
        return 1
    t0, t1 = idx[WARMUP_BARS], idx[-1] + pd.Timedelta(seconds=1)
    t_split = t0 + (t1 - t0) * a.split

    RESULTS_DIR.mkdir(exist_ok=True)
    out = {}
    for name, lo, hi in (("IN-SAMPLE", t0, t_split), ("OUT-OF-SAMPLE (the one that matters)", t_split, t1),
                         ("FULL PERIOD", t0, t1)):
        tr, eq = simulate(frames, cfg, a.equity, lo, hi)
        m = metrics(tr, eq, a.equity, frames, eq.index)
        print_report(name, m)
        out[name] = (tr, eq)
    tr, eq = out["FULL PERIOD"]
    tr.to_csv(RESULTS_DIR / "backtest_trades.csv", index=False)
    eq.to_csv(RESULTS_DIR / "backtest_equity.csv")
    print(f"\nSaved results/backtest_trades.csv and results/backtest_equity.csv")
    print("Settings used:", {k: getattr(cfg, k) for k in
                             ("entry_threshold", "sl_atr", "tp_atr", "fee_rate", "slippage")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
