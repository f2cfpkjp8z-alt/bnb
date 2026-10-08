"""The engine: one shared market scan feeding several accounts (sim + live).

Every action is written to SQLite (db.py) and mirrored to the web app (cloud.py).
"""
from __future__ import annotations

import json
import logging
import os
import platform
import time
from pathlib import Path

from account import Account
from commands import validate
from config import BASE_DIR, Config
from cloud import NullCloud
from db import DB
from indicators import tf_seconds
from profiles import PROFILES, describe
from scanner import analyze, ohlcv_to_df, select_universe
from signals import btc_market_ok

log = logging.getLogger("bot")
VERSION = "2.0"


class Engine:
    def __init__(self, cfg: Config, data_ex, news, db: DB, cloud=None,
                 state_dir: Path | None = None, flag_dir: Path | None = None):
        self.cfg, self.data_ex, self.news, self.db = cfg, data_ex, news, db
        self.cloud = cloud or NullCloud()
        self.state_dir = Path(state_dir or BASE_DIR / "state")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        flag_dir = Path(flag_dir or BASE_DIR)
        self.stop_file, self.panic_file = flag_dir / "STOP", flag_dir / "PANIC"
        self.accounts: dict[str, Account] = {}
        self.watchlist: list[str] = []
        self.blocklist: list[str] = []
        self.prices: dict[str, float] = {}
        self.tf_secs = tf_seconds(cfg.timeframe)
        self.board: list[dict] = []
        self.last_scan: dict = {}
        self.cur_scan_id: int | None = None
        self.started = time.time()
        self._next_scan = 0.0
        self._ob_cache: dict[str, tuple[bool, str]] = {}
        self._last_gate_msg: dict[str, str] = {}
        self._bal_cache: dict[str, tuple[float, float | None]] = {}
        self._stop_logged = False
        self.curves: dict[str, dict[str, list]] = {}
        self.curve_points = 2000                  # ~7 days at 5-minute resolution

    # ------------------------------------------------------------------ plumbing
    def add_account(self, acct: Account) -> None:
        self.accounts[acct.name] = acct

    def emit(self, kind: str, message: str, account: str | None = None, symbol: str | None = None,
             level: str = "info", data=None, cloud: bool = True) -> None:
        ts = time.time()
        getattr(log, level if level in ("info", "warning", "error") else "info")(
            "[%s] %s", account or "bot", message)
        try:
            self.db.event(ts, kind, message, account, symbol, level, data)
        except Exception:
            log.exception("could not write event to SQLite")
        if cloud:
            self.cloud.push_event({"ts": ts, "kind": kind, "account": account, "symbol": symbol,
                                   "level": level, "message": message, "data": data})

    def record_open(self, acct: Account, pos) -> None:
        self.emit("trade_open",
                  f"{acct.name}: BOUGHT {pos.symbol} for {pos.cost:.2f} USDT @ {pos.entry:.6g} | "
                  f"stop {pos.stop:.6g} ({(pos.stop / pos.entry - 1) * 100:+.2f}%) | "
                  f"target {pos.tp:.6g} ({(pos.tp / pos.entry - 1) * 100:+.2f}%) | score {pos.score:.2f}",
                  account=acct.name, symbol=pos.symbol,
                  data={"entry": pos.entry, "qty": pos.qty, "cost": pos.cost, "stop": pos.stop,
                        "tp": pos.tp, "score": pos.score})

    def record_trade(self, acct: Account, pos, fill, pnl: float, reason: str, now: float) -> None:
        self.db.trade(acct.name, pos.symbol, pos.opened_ts, now, pos.entry, fill.price, pos.qty,
                      pos.cost, fill.cost, reason, pos.score, acct.profile)
        self.cloud.push_trade({"account": acct.name, "symbol": pos.symbol, "opened_ts": pos.opened_ts,
                               "closed_ts": now, "entry": pos.entry, "exit": fill.price,
                               "cost": pos.cost, "pnl": pnl, "pnl_pct": pnl / pos.cost * 100,
                               "reason": reason, "score": pos.score, "profile": acct.profile})
        self.emit("trade_close",
                  f"{acct.name}: SOLD {pos.symbol} ({reason}) @ {fill.price:.6g} | P&L {pnl:+.3f} USDT "
                  f"({pnl / pos.cost * 100:+.2f}%) | held {(now - pos.opened_ts) / 60:.0f} min",
                  account=acct.name, symbol=pos.symbol,
                  data={"pnl": pnl, "reason": reason, "exit": fill.price})

    # ------------------------------------------------------------------ persistence
    def _state_file(self) -> Path:
        return self.state_dir / "engine.json"

    def save_state(self) -> None:
        self._state_file().write_text(json.dumps(
            {"watchlist": self.watchlist, "blocklist": self.blocklist}))

    def load_state(self) -> None:
        f = self._state_file()
        if f.exists():
            d = json.loads(f.read_text())
            self.watchlist, self.blocklist = d.get("watchlist", []), d.get("blocklist", [])

    # ------------------------------------------------------------------ commands
    def _acct(self, name: str) -> Account:
        if name not in self.accounts:
            raise KeyError("the live account is not armed on the PC (start the bot with --live "
                           "or --testnet)" if name == "live" else f"unknown account {name}")
        return self.accounts[name]

    def _valid_market(self, sym: str) -> bool:
        m = self.data_ex.markets.get(sym)
        return bool(m and m.get("active", True) and m.get("spot", True))

    def process_commands(self) -> None:
        now = time.time()
        for raw in self.cloud.fetch_commands():
            ok, why, cmd = validate(raw, self.cloud.owner_uid, now, self.cfg.command_max_age_seconds)
            if not ok:
                self.db.command(now, "web", str(raw.get("id")), str(raw.get("cmd")), raw.get("args"),
                                "rejected", why)
                self.cloud.ack(raw.get("id"), "rejected", why)
                self.emit("command", f"web command rejected: {why}", level="warning",
                          data={"cmd": raw.get("cmd")})
                continue
            try:
                msg = self._execute(cmd["cmd"], cmd["args"])
                status = "done"
            except Exception as e:
                msg, status = str(e), "failed"
            self.db.command(now, "web", str(cmd["id"]), cmd["cmd"], cmd["args"], status, msg)
            self.cloud.ack(cmd["id"], status, msg)
            self.emit("command", f"web command {cmd['cmd']} {cmd['args']}: {status} - {msg}",
                      level="info" if status == "done" else "warning")

    def _execute(self, name: str, a: dict) -> str:
        if name == "set_profile":
            self._acct(a["account"]).set_profile(a["profile"])
            return f"{a['account']} is now '{a['profile']}'"
        if name == "set_enabled":
            self._acct(a["account"]).set_enabled(a["enabled"])
            return f"{a['account']} entries {'ON' if a['enabled'] else 'OFF'}"
        if name in ("add_watch", "remove_watch", "block", "unblock"):
            return self._edit_lists(name, a["symbol"])
        if name == "close_position":
            acct = self._acct(a["account"])
            if a["symbol"] not in acct.positions:
                raise KeyError(f"{a['symbol']} is not open in {a['account']}")
            t = self.data_ex.fetch_tickers([a["symbol"]]).get(a["symbol"]) or {}
            px = t.get("bid") or t.get("last") or acct.positions[a["symbol"]].entry
            ok = acct.close(a["symbol"], px, "manual")
            if not ok:
                raise RuntimeError("sell failed - see the log")
            return f"closed {a['symbol']}"
        if name == "close_all":
            acct = self._acct(a["account"])
            n = len(acct.positions)
            acct.close_all(self._bids(list(acct.positions)), "manual")
            return f"closed {n} position(s)"
        if name == "reset_sim":
            self._acct("sim").reset()
            return "simulation reset"
        if name == "scan_now":
            self._next_scan = 0.0
            return "scan scheduled"
        raise KeyError(name)

    def _edit_lists(self, name: str, sym: str) -> str:
        cfg = self.cfg
        if name == "add_watch":
            if sym in self.blocklist:
                raise ValueError(f"{sym} is blocked; unblock it first")
            if not self._valid_market(sym) or sym.split("/")[0] in cfg.stable_bases:
                raise ValueError(f"{sym} is not a tradable USDT spot pair on Binance")
            if sym in self.watchlist:
                return f"{sym} already in the watchlist"
            if len(self.watchlist) >= cfg.max_watchlist:
                raise ValueError(f"watchlist is full ({cfg.max_watchlist})")
            self.watchlist.append(sym)
            self._next_scan = 0.0
            msg = f"{sym} added to the watchlist (it will be analysed every scan)"
        elif name == "remove_watch":
            if sym not in self.watchlist:
                raise ValueError(f"{sym} is not in the watchlist")
            self.watchlist.remove(sym)
            msg = f"{sym} removed from the watchlist"
        elif name == "block":
            if sym not in self.blocklist:
                self.blocklist.append(sym)
            if sym in self.watchlist:
                self.watchlist.remove(sym)
            msg = f"{sym} blocked: never traded"
        else:
            if sym not in self.blocklist:
                raise ValueError(f"{sym} is not blocked")
            self.blocklist.remove(sym)
            msg = f"{sym} unblocked"
        self.save_state()
        return msg

    # ------------------------------------------------------------------ data helpers
    def _bids(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        t = self.data_ex.fetch_tickers(symbols)
        out = {}
        for s in symbols:
            d = t.get(s) or {}
            px = d.get("bid") or d.get("last")
            if px:
                out[s] = float(px)
            if d.get("last"):
                self.prices[s] = float(d["last"])
        return out

    def manage(self) -> None:
        syms = sorted({s for a in self.accounts.values() for s in a.positions})
        if not syms:
            return
        bids = self._bids(syms)
        now = time.time()
        for a in self.accounts.values():
            a.manage(bids, now)

    def orderbook_ok(self, sym: str) -> tuple[bool, str]:
        if not self.cfg.ob_gate:
            return True, ""
        if sym in self._ob_cache:
            return self._ob_cache[sym]
        try:
            ob = self.data_ex.fetch_order_book(sym, self.cfg.ob_depth)
            bids, asks = ob["bids"], ob["asks"]
            bid_v = sum(p * q for p, q in bids)
            ask_v = sum(p * q for p, q in asks)
            spread = (asks[0][0] - bids[0][0]) / ((asks[0][0] + bids[0][0]) / 2)
            ratio = bid_v / max(ask_v, 1e-9)
            ok = ratio >= self.cfg.ob_min_ratio and spread <= 0.001
            res = (ok, "" if ok else f"order book weak (bid/ask value {ratio:.2f}, spread {spread * 100:.3f}%)")
        except Exception as e:
            res = (False, f"order book unavailable ({e})")
        self._ob_cache[sym] = res
        return res

    # ------------------------------------------------------------------ scan
    def build_universe(self, tickers: dict) -> list[str]:
        uni = [s for s in select_universe(tickers, self.data_ex.markets, self.cfg)
               if s not in self.blocklist]
        extra = [s for s in self.watchlist if s in tickers and s not in uni and s not in self.blocklist]
        return uni + extra

    def scan(self) -> None:
        cfg, now = self.cfg, time.time()
        self._ob_cache.clear()
        tickers = self.data_ex.fetch_tickers()
        for s, t in tickers.items():
            if t.get("last") and any(s in a.positions for a in self.accounts.values()):
                self.prices[s] = float(t["last"])

        btc_ok, btc_note = True, ""
        if cfg.btc_filter:
            try:
                btc = ohlcv_to_df(self.data_ex.fetch_ohlcv("BTC/USDT", cfg.timeframe, limit=60)[:-1])
                btc_ok = bool(btc_market_ok(btc["close"], cfg, 3600 // self.tf_secs).iloc[-1])
                if not btc_ok:
                    btc_note = f"BTC fell more than {-cfg.btc_drop_pct:.1f}% in the last hour"
            except Exception as e:
                log.warning("BTC check failed: %s", e)

        universe = self.build_universe(tickers)
        self.emit("scan", f"Scanning {len(universe)} coins: " +
                  ", ".join(s.split("/")[0] + ("*" if s in self.watchlist else "") for s in universe),
                  data={"universe": universe}, cloud=False)

        rows = []
        for sym in universe:
            try:
                sf = analyze(self.data_ex.fetch_ohlcv(sym, cfg.timeframe, limit=cfg.candles), cfg)
            except Exception as e:
                log.warning("data error for %s: %s", sym, e)
                continue
            if sf is None:
                continue
            last = sf.iloc[-1]
            if now - (sf.index[-1].timestamp() + self.tf_secs) > 2 * self.tf_secs:
                log.warning("stale candles for %s, skipping", sym)
                continue
            rows.append({"symbol": sym, "price": float(last.close), "score": float(last.score),
                         "gates": bool(last.gates), "rsi": float(last.rsi),
                         "atr": float(last.atr), "atr_pct": float(last.atr_pct),
                         "components": {k[2:]: round(float(last[k]), 2) for k in sf.columns
                                        if k.startswith("c_")},
                         "watch": sym in self.watchlist})
        rows.sort(key=lambda r: -r["score"])
        lo = min(a.cfg.entry_threshold for a in self.accounts.values()) if self.accounts else cfg.entry_threshold
        n_cand = sum(1 for r in rows if r["gates"] and r["score"] >= lo - cfg.boost_margin)
        self.cur_scan_id = self.db.scan(now, universe, n_cand, btc_ok, btc_note)
        self.db.analysis_rows(self.cur_scan_id, now, rows)

        top = rows[:5]
        self.emit("scan_result",
                  "Top scores: " + (", ".join(f"{r['symbol'].split('/')[0]} {r['score']:.2f}"
                                              f"{'' if r['gates'] else '*'}" for r in top) or "none")
                  + f"  | {n_cand} candidate(s)" + ("" if btc_ok else f"  | {btc_note}")
                  + "   (* = failed a hard gate)",
                  data={"top": [{k: r[k] for k in ("symbol", "score", "gates", "rsi", "price")}
                                for r in rows[:8]], "btc_ok": btc_ok})

        for acct in self.accounts.values():
            try:
                self.evaluate(acct, rows, tickers, btc_ok, btc_note, now)
            except Exception:
                log.exception("evaluation failed for %s", acct.name)

        self.board = rows
        self.last_scan = {"ts": now, "scan_id": self.cur_scan_id, "btc_ok": btc_ok,
                          "btc_note": btc_note, "n_coins": len(rows)}
        self._next_scan = (now // self.tf_secs + 1) * self.tf_secs + 5

    def decide(self, acct: Account, sym: str, score: float, action: str, reason: str) -> None:
        self.db.decision(self.cur_scan_id, time.time(), acct.name, sym, score, action, reason)
        acct.verdicts[sym] = reason if action == "skip" else "BOUGHT"
        if action == "skip":
            self.emit("decision", f"{acct.name}: skip {sym} (score {score:.2f}) - {reason}",
                      account=acct.name, symbol=sym, cloud="cooling down" not in reason)

    def evaluate(self, acct: Account, rows: list[dict], tickers: dict, btc_ok: bool,
                 btc_note: str, now: float) -> None:
        cfg, eq = acct.cfg, acct.equity(self.prices)
        acct.verdicts = {}
        for r in rows:
            if r["symbol"] in acct.positions:
                acct.verdicts[r["symbol"]] = "position open"
            elif not r["gates"]:
                acct.verdicts[r["symbol"]] = "failed a hard gate (trend / RSI / cost)"
            elif r["score"] < cfg.entry_threshold - cfg.boost_margin:
                acct.verdicts[r["symbol"]] = f"score below {cfg.entry_threshold:.2f}"
        cands = [r for r in rows if r["symbol"] not in acct.verdicts]

        ok, why = acct.gate(now, eq)
        if ok and self.stop_file.exists():
            ok, why = False, "STOP file present"
        if ok and not btc_ok:
            ok, why = False, btc_note
        if not ok:
            for r in cands:
                acct.verdicts[r["symbol"]] = f"blocked: {why}"
            changed = self._last_gate_msg.get(acct.name) != why
            self._last_gate_msg[acct.name] = why
            self.emit("decision", f"{acct.name}: no new entries - {why}", account=acct.name,
                      cloud=changed)
            return
        self._last_gate_msg[acct.name] = ""

        for r in cands:                                    # already sorted by score
            sym, base = r["symbol"], r["symbol"].split("/")[0]
            if len(acct.positions) >= cfg.max_positions:
                acct.verdicts[sym] = "position limit reached"
                continue
            score = r["score"]
            if self.news.enabled:
                verdict, reason = self.news.check(base)
                self.emit("news", f"news {base}: {verdict} - {reason}", symbol=sym, cloud=False)
                if verdict == "veto":
                    self.decide(acct, sym, score, "skip", f"news veto: {reason}")
                    continue
                if verdict == "boost":
                    score += cfg.boost_margin
            if score < cfg.entry_threshold:
                acct.verdicts[sym] = f"score below {cfg.entry_threshold:.2f}"
                continue
            ob_ok, ob_why = self.orderbook_ok(sym)
            if not ob_ok:
                self.decide(acct, sym, score, "skip", ob_why)
                continue
            t = tickers.get(sym) or {}
            ref = t.get("ask") or t.get("last") or r["price"]
            ok, why = acct.try_open(sym, float(ref), r["atr"], score, acct.equity(self.prices), now)
            if ok:
                self.prices[sym] = float(ref)
                self.decide(acct, sym, score, "buy", "all checks passed")
            else:
                self.decide(acct, sym, score, "skip", why)

    # ------------------------------------------------------------------ status / reports
    def _stats(self, name: str) -> dict:
        r = self.db.q("SELECT COUNT(*) n, COALESCE(SUM(pnl>0),0) w, COALESCE(SUM(pnl),0) p, "
                      "COALESCE(AVG(pnl_pct),0) a FROM trades WHERE account=?", (name,))[0]
        return {"trades": r["n"], "wins": r["w"], "pnl": r["p"], "avg_pct": r["a"],
                "win_rate": (r["w"] / r["n"] * 100) if r["n"] else 0.0}

    def _exchange_balance(self, acct: Account, now: float):
        if acct.venue == "paper":
            return None
        ts, val = self._bal_cache.get(acct.name, (0, None))
        if now - ts > 60:
            try:
                val = float(acct.broker.free_quote())
            except Exception as e:
                log.warning("balance fetch failed for %s: %s", acct.name, e)
            self._bal_cache[acct.name] = (now, val)
        return val

    def status(self, now: float | None = None) -> dict:
        now = now or time.time()
        accts = {}
        for n, a in self.accounts.items():
            s = a.snapshot(self.prices, now)
            s["stats"] = self._stats(n)
            s["exchange_usdt"] = self._exchange_balance(a, now)
            accts[n] = s
        board = []
        for r in self.board[:12]:
            board.append({**{k: r[k] for k in ("symbol", "price", "score", "gates", "rsi", "components",
                                               "watch")},
                          "verdict": {n: a.verdicts.get(r["symbol"], "") for n, a in self.accounts.items()}})
        return {"version": VERSION, "heartbeat": now, "started": self.started,
                "host": platform.node(), "stop_active": self.stop_file.exists(),
                "live_armed": "live" in self.accounts, "accounts": accts,
                "scanner": {**self.last_scan, "next_scan": self._next_scan, "board": board},
                "watchlist": self.watchlist, "blocklist": self.blocklist,
                "news_enabled": bool(self.news.enabled),
                "profiles": {k: describe(k) for k in PROFILES},
                "commands_enabled": bool(getattr(self.cloud, "owner_uid", None))}

    def load_curves(self) -> None:
        for n in self.accounts:
            rows = self.db.q("SELECT ts, equity FROM equity WHERE account=? ORDER BY ts DESC LIMIT ?",
                             (n, self.curve_points))[::-1]
            self.curves[n] = {"t": [round(r[0], 1) for r in rows], "v": [round(r[1], 4) for r in rows]}

    def snapshot_equity(self, now: float) -> None:
        for n, a in self.accounts.items():
            eq, un = a.equity(self.prices), a.unrealized(self.prices)
            cash = a.start_equity + a.realized - a.invested()
            self.db.equity(n, now, eq, cash, a.realized, un, len(a.positions))
            c = self.curves.setdefault(n, {"t": [], "v": []})
            c["t"].append(round(now, 1)); c["v"].append(round(eq, 4))
            del c["t"][:-self.curve_points], c["v"][:-self.curve_points]
        self.cloud.push_equity(self.curves)

    # ------------------------------------------------------------------ main loop
    def close_everything(self, reason: str) -> None:
        syms = sorted({s for a in self.accounts.values() for s in a.positions})
        bids = self._bids(syms)
        for a in self.accounts.values():
            a.close_all(bids, reason)

    def run(self, once: bool = False) -> None:
        for a in self.accounts.values():
            a.load()
        self.load_state()
        self.load_curves()
        if any(c["t"] for c in self.curves.values()):
            self.cloud.push_equity(self.curves)
        self.db.run(self.started, list(self.accounts), f"v{VERSION}")
        self.emit("start", "Bot started. " + " | ".join(
            f"{n}: {a.venue}, {a.profile}, entries {'ON' if a.enabled else 'OFF'}, "
            f"balance {a.equity(self.prices):.2f}" for n, a in self.accounts.items()))
        if self.cloud.enabled and not self.cloud.owner_uid:
            self.emit("warning", "FIREBASE_OWNER_UID is not set: the web app can show data but "
                      "cannot send commands.", level="warning")
        t_manage = t_status = t_snap = t_prune = 0.0
        errors = 0
        while True:
            try:
                now = time.time()
                if self.panic_file.exists():
                    self.emit("panic", "PANIC file found: closing everything and exiting", level="warning")
                    self.close_everything("panic")
                    self.cloud.push_status(self.status())
                    return
                self.process_commands()
                if now >= t_manage:
                    self.manage()
                    t_manage = now + self.cfg.poll_seconds
                if now >= self._next_scan:
                    if self.stop_file.exists() and not self._stop_logged:
                        self.emit("warning", "STOP file found: analysing only, no new entries",
                                  level="warning")
                    self._stop_logged = self.stop_file.exists()
                    self.scan()
                if now >= t_snap:
                    self.snapshot_equity(now)
                    t_snap = now + self.cfg.equity_snapshot_seconds
                if now >= t_status or once:
                    self.cloud.push_status(self.status(now))
                    t_status = now + self.cfg.status_push_seconds
                if now >= t_prune:
                    self.db.prune()
                    t_prune = now + 86400
                errors = 0
                if once:
                    return
            except KeyboardInterrupt:
                raise
            except Exception as e:
                errors += 1
                log.exception("loop error #%d", errors)
                self.emit("error", f"loop error: {e}", level="error", cloud=errors < 4)
                time.sleep(min(300, 5 * 2 ** min(errors, 6)))
            time.sleep(2)
