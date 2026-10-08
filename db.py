"""SQLite report database. Both accounts ('sim' and 'live') write here.

Open it with any SQLite viewer (DB Browser for SQLite, VS Code SQLite extension) or use
report.py. Tables:
  runs        one row per bot start
  events      everything the bot did/decided, in plain text (the live log)
  scans       one row per market scan
  analysis    every coin analysed in every scan (score, gates, indicators)
  decisions   per account: why a coin was bought or skipped
  trades      closed trades with P&L
  equity      equity curve snapshots per account
  commands    commands received from the web app and what happened to them
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, ts REAL, iso TEXT, accounts TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL, iso TEXT, account TEXT, level TEXT, kind TEXT,
  symbol TEXT, message TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS scans(
  id INTEGER PRIMARY KEY, ts REAL, iso TEXT, n_coins INTEGER, n_candidates INTEGER,
  btc_ok INTEGER, universe TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS analysis(
  id INTEGER PRIMARY KEY, scan_id INTEGER, ts REAL, symbol TEXT, price REAL, score REAL,
  gates INTEGER, rsi REAL, atr_pct REAL, components TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY, scan_id INTEGER, ts REAL, iso TEXT, account TEXT, symbol TEXT,
  score REAL, action TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY, account TEXT, symbol TEXT, opened_ts REAL, closed_ts REAL,
  closed_iso TEXT, entry REAL, exit REAL, qty REAL, cost REAL, proceeds REAL, pnl REAL,
  pnl_pct REAL, reason TEXT, score REAL, profile TEXT);
CREATE TABLE IF NOT EXISTS equity(
  id INTEGER PRIMARY KEY, account TEXT, ts REAL, iso TEXT, equity REAL, cash REAL,
  realized REAL, unrealized REAL, open_positions INTEGER);
CREATE TABLE IF NOT EXISTS commands(
  id INTEGER PRIMARY KEY, ts REAL, iso TEXT, source TEXT, cmd_id TEXT, cmd TEXT, args TEXT,
  status TEXT, result TEXT);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_trades_acc ON trades(account, closed_ts);
CREATE INDEX IF NOT EXISTS ix_equity_acc ON equity(account, ts);
CREATE INDEX IF NOT EXISTS ix_analysis_scan ON analysis(scan_id);
"""


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _j(x) -> str | None:
    return None if x is None else json.dumps(x, default=str, separators=(",", ":"))


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, timeout=30)
        self.con.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.con.execute("PRAGMA journal_mode=WAL")      # readers never block the bot
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.executescript(SCHEMA)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def _ins(self, sql: str, args: tuple) -> int:
        cur = self.con.execute(sql, args)
        self.con.commit()
        return cur.lastrowid

    # ------------------------------------------------------------------ writers
    def run(self, ts: float, accounts: list[str], note: str = "") -> int:
        return self._ins("INSERT INTO runs(ts,iso,accounts,note) VALUES(?,?,?,?)",
                         (ts, iso(ts), ",".join(accounts), note))

    def event(self, ts: float, kind: str, message: str, account: str | None = None,
              symbol: str | None = None, level: str = "info", data=None) -> int:
        return self._ins(
            "INSERT INTO events(ts,iso,account,level,kind,symbol,message,data) VALUES(?,?,?,?,?,?,?,?)",
            (ts, iso(ts), account, level, kind, symbol, message, _j(data)))

    def scan(self, ts: float, universe: list[str], n_candidates: int, btc_ok: bool,
             note: str = "") -> int:
        return self._ins(
            "INSERT INTO scans(ts,iso,n_coins,n_candidates,btc_ok,universe,note) VALUES(?,?,?,?,?,?,?)",
            (ts, iso(ts), len(universe), n_candidates, int(btc_ok), _j(universe), note))

    def analysis_rows(self, scan_id: int, ts: float, rows: list[dict]) -> None:
        self.con.executemany(
            "INSERT INTO analysis(scan_id,ts,symbol,price,score,gates,rsi,atr_pct,components) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(scan_id, ts, r["symbol"], r["price"], r["score"], int(r["gates"]), r["rsi"],
              r["atr_pct"], _j(r["components"])) for r in rows])
        self.con.commit()

    def decision(self, scan_id: int | None, ts: float, account: str, symbol: str, score: float,
                 action: str, reason: str) -> int:
        return self._ins(
            "INSERT INTO decisions(scan_id,ts,iso,account,symbol,score,action,reason) "
            "VALUES(?,?,?,?,?,?,?,?)", (scan_id, ts, iso(ts), account, symbol, score, action, reason))

    def trade(self, account: str, symbol: str, opened_ts: float, closed_ts: float, entry: float,
              exit_: float, qty: float, cost: float, proceeds: float, reason: str, score: float,
              profile: str) -> int:
        pnl = proceeds - cost
        return self._ins(
            "INSERT INTO trades(account,symbol,opened_ts,closed_ts,closed_iso,entry,exit,qty,cost,"
            "proceeds,pnl,pnl_pct,reason,score,profile) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account, symbol, opened_ts, closed_ts, iso(closed_ts), entry, exit_, qty, cost, proceeds,
             pnl, pnl / cost * 100 if cost else 0.0, reason, score, profile))

    def equity(self, account: str, ts: float, equity: float, cash: float, realized: float,
               unrealized: float, open_positions: int) -> int:
        return self._ins(
            "INSERT INTO equity(account,ts,iso,equity,cash,realized,unrealized,open_positions) "
            "VALUES(?,?,?,?,?,?,?,?)", (account, ts, iso(ts), equity, cash, realized, unrealized,
                                        open_positions))

    def command(self, ts: float, source: str, cmd_id: str, cmd: str, args, status: str,
                result: str = "") -> int:
        return self._ins(
            "INSERT INTO commands(ts,iso,source,cmd_id,cmd,args,status,result) VALUES(?,?,?,?,?,?,?,?)",
            (ts, iso(ts), source, cmd_id, cmd, _j(args), status, result))

    # ------------------------------------------------------------------ readers
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    def prune(self, keep_days: int = 60, now: float | None = None) -> None:
        """Keep the per-coin analysis table from growing forever (trades/equity are kept)."""
        import time as _t
        cut = (now or _t.time()) - keep_days * 86400
        self.con.execute("DELETE FROM analysis WHERE ts < ?", (cut,))
        self.con.execute("DELETE FROM events WHERE ts < ? AND kind IN ('scan','decision')", (cut,))
        self.con.commit()
