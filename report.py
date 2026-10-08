"""Reports from the bot's SQLite database (works while the bot is running).

  python report.py summary                  # balances, P&L, win rate for sim and live
  python report.py summary --account sim --days 7
  python report.py trades --limit 30
  python report.py events --limit 40        # what the bot did / decided
  python report.py events --follow          # live tail of the bot's actions
  python report.py scan                     # latest market scan: every coin and its score
  python report.py decisions --limit 30     # why coins were bought or skipped
  python report.py export                   # CSV files of every table in reports/
  python report.py sql "SELECT * FROM trades WHERE pnl < 0"
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path

from config import BASE_DIR, load_config

TABLES = ["runs", "events", "scans", "analysis", "decisions", "trades", "equity", "commands"]


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if not path.exists():
        sys.exit(f"No database at {path}. Start the bot first (python bot.py).")
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def table(rows: list[list], headers: list[str]) -> str:
    if not rows:
        return "  (nothing yet)"
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    w = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * x for x in w)]
    out += ["  ".join(c.ljust(w[i]) for i, c in enumerate(r)) for r in cells]
    return "\n".join(out)


def f(x, nd=2, sign=False) -> str:
    if x is None:
        return ""
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def accounts_in(con, want: str) -> list[str]:
    if want != "all":
        return [want]
    names = [r[0] for r in con.execute(
        "SELECT account FROM equity UNION SELECT account FROM trades ORDER BY 1") if r[0]]
    return names or ["sim"]


def since_ts(days: float | None) -> float:
    return time.time() - days * 86400 if days else 0.0


# --------------------------------------------------------------------------- commands
def cmd_summary(con, a) -> None:
    cut = since_ts(a.days)
    for acc in accounts_in(con, a.account):
        print(f"\n===== {acc.upper()} account" + (f" (last {a.days:g} days)" if a.days else "") + " =====")
        e = con.execute("SELECT * FROM equity WHERE account=? ORDER BY ts DESC LIMIT 1", (acc,)).fetchone()
        e0 = con.execute("SELECT * FROM equity WHERE account=? ORDER BY ts LIMIT 1", (acc,)).fetchone()
        if e:
            print(f"Balance now : {e['equity']:.2f} USDT   (cash {e['cash']:.2f}, open positions "
                  f"{e['open_positions']}, unrealised {e['unrealized']:+.2f})   as of {e['iso']} UTC")
        if e and e0:
            print(f"Since start : {e['equity'] - e0['equity']:+.2f} USDT "
                  f"({(e['equity'] / e0['equity'] - 1) * 100:+.2f}%)   first snapshot {e0['iso']} UTC")
            mx = con.execute("SELECT MAX(equity) m FROM equity WHERE account=?", (acc,)).fetchone()["m"]
            dd = con.execute("SELECT MIN(equity / (SELECT MAX(equity) FROM equity e2 WHERE e2.account=e.account "
                             "AND e2.ts<=e.ts) - 1) d FROM equity e WHERE account=?", (acc,)).fetchone()["d"]
            print(f"Peak / max drawdown : {mx:.2f} / {(dd or 0) * 100:.2f}%")
        t = con.execute("SELECT COUNT(*) n, COALESCE(SUM(pnl>0),0) w, COALESCE(SUM(pnl),0) p, "
                        "COALESCE(AVG(pnl_pct),0) a, COALESCE(SUM(CASE WHEN pnl>0 THEN pnl END),0) gp, "
                        "COALESCE(-SUM(CASE WHEN pnl<=0 THEN pnl END),0) gl, MIN(pnl) worst, MAX(pnl) best "
                        "FROM trades WHERE account=? AND closed_ts>=?", (acc, cut)).fetchone()
        if not t["n"]:
            print("Trades      : none closed yet")
            continue
        pf = f"{t['gp'] / t['gl']:.2f}" if t["gl"] > 0 else "inf"
        print(f"Trades      : {t['n']}   wins {t['w']} ({t['w'] / t['n'] * 100:.0f}%)   "
              f"realised P&L {t['p']:+.2f} USDT   avg {t['a']:+.2f}%/trade   profit factor {pf}")
        print(f"Best / worst: {t['best']:+.3f} / {t['worst']:+.3f} USDT")
        print("\nBy exit reason:")
        print(table([[r["reason"], r["n"], f(r["p"], 3, True)] for r in con.execute(
            "SELECT reason, COUNT(*) n, SUM(pnl) p FROM trades WHERE account=? AND closed_ts>=? "
            "GROUP BY reason ORDER BY n DESC", (acc, cut))], ["reason", "trades", "pnl USDT"]))
        print("\nBy coin (top 10 by trades):")
        print(table([[r["symbol"], r["n"], f(r["p"], 3, True), f(r["w"] * 100, 0) + "%"] for r in con.execute(
            "SELECT symbol, COUNT(*) n, SUM(pnl) p, AVG(pnl>0) w FROM trades WHERE account=? AND closed_ts>=? "
            "GROUP BY symbol ORDER BY n DESC LIMIT 10", (acc, cut))], ["coin", "trades", "pnl USDT", "win rate"]))
        print("\nBy day (UTC):")
        print(table([[r["d"], r["n"], f(r["p"], 3, True)] for r in con.execute(
            "SELECT substr(closed_iso,1,10) d, COUNT(*) n, SUM(pnl) p FROM trades WHERE account=? AND closed_ts>=? "
            "GROUP BY d ORDER BY d DESC LIMIT 14", (acc, cut))], ["day", "trades", "pnl USDT"]))


def cmd_trades(con, a) -> None:
    q = "SELECT * FROM trades WHERE closed_ts>=?"
    args: list = [since_ts(a.days)]
    if a.account != "all":
        q += " AND account=?"
        args.append(a.account)
    rows = con.execute(q + " ORDER BY closed_ts DESC LIMIT ?", (*args, a.limit)).fetchall()
    print(table([[r["closed_iso"], r["account"], r["symbol"], r["reason"], f(r["entry"], 6), f(r["exit"], 6),
                  f(r["cost"], 2), f(r["pnl"], 3, True), f(r["pnl_pct"], 2, True) + "%", f(r["score"], 2),
                  r["profile"]] for r in rows],
                ["closed (UTC)", "acct", "coin", "exit", "entry", "exit px", "cost", "pnl", "pnl %", "score",
                 "profile"]))


def _events_query(a):
    q, args = "SELECT * FROM events WHERE 1=1", []
    if a.account != "all":
        q += " AND (account=? OR account IS NULL)"
        args.append(a.account)
    if a.kind:
        ks = a.kind.split(",")
        q += f" AND kind IN ({','.join('?' * len(ks))})"
        args += ks
    return q, args


def _print_events(rows) -> None:
    for r in rows:
        print(f"{r['iso']}  {(r['account'] or '-'):4}  {r['kind']:12} {r['message']}")


def cmd_events(con, a) -> None:
    q, args = _events_query(a)
    rows = list(reversed(con.execute(q + " ORDER BY id DESC LIMIT ?", (*args, a.limit)).fetchall()))
    _print_events(rows)
    if not a.follow:
        return
    last = rows[-1]["id"] if rows else 0
    try:
        while True:
            time.sleep(2)
            new = con.execute(q + " AND id>? ORDER BY id", (*args, last)).fetchall()
            if new:
                _print_events(new)
                last = new[-1]["id"]
    except KeyboardInterrupt:
        pass


def cmd_scan(con, a) -> None:
    s = con.execute("SELECT * FROM scans " + ("WHERE id=? " if a.id else "") + "ORDER BY id DESC LIMIT 1",
                    (a.id,) if a.id else ()).fetchone()
    if not s:
        print("No scans yet.")
        return
    print(f"Scan #{s['id']} at {s['iso']} UTC: {s['n_coins']} coins, {s['n_candidates']} candidate(s), "
          f"BTC filter {'OK' if s['btc_ok'] else 'BLOCKING ' + (s['note'] or '')}")
    rows = con.execute("SELECT * FROM analysis WHERE scan_id=? ORDER BY score DESC", (s["id"],)).fetchall()
    comp_keys = ["trend", "htf", "momentum", "macd", "breakout", "volume"]
    out = []
    for r in rows:
        c = json.loads(r["components"] or "{}")
        out.append([r["symbol"], f(r["price"], 6), f(r["score"], 2), "yes" if r["gates"] else "NO",
                    f(r["rsi"], 0), *[f(c.get(k), 1) for k in comp_keys]])
    print(table(out, ["coin", "price", "score", "gates", "RSI", *comp_keys]))
    ds = con.execute("SELECT account, symbol, action, reason FROM decisions WHERE scan_id=?", (s["id"],)).fetchall()
    if ds:
        print("\nDecisions in this scan:")
        print(table([[d["account"], d["symbol"], d["action"], d["reason"]] for d in ds],
                    ["acct", "coin", "action", "reason"]))


def cmd_decisions(con, a) -> None:
    q, args = "SELECT * FROM decisions WHERE 1=1", []
    if a.account != "all":
        q += " AND account=?"
        args.append(a.account)
    rows = con.execute(q + " ORDER BY id DESC LIMIT ?", (*args, a.limit)).fetchall()
    print(table([[r["iso"], r["account"], r["symbol"], f(r["score"], 2), r["action"], r["reason"]]
                 for r in rows], ["time (UTC)", "acct", "coin", "score", "action", "reason"]))


def cmd_export(con, a) -> None:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for t in TABLES:
        cur = con.execute(f"SELECT * FROM {t}")
        with open(out / f"{t}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([d[0] for d in cur.description])
            w.writerows(cur)
        print(f"wrote {out / (t + '.csv')}")


def cmd_sql(con, a) -> None:
    cur = con.execute(a.query)          # connection is read-only, so this cannot change anything
    rows = cur.fetchmany(a.limit)
    print(table([list(r) for r in rows], [d[0] for d in cur.description]))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--db", help="path to bot.db (default from config)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(fn=fn)
        return sp

    for name, fn in (("summary", cmd_summary), ("trades", cmd_trades)):
        sp = add(name, fn)
        sp.add_argument("--account", choices=["sim", "live", "all"], default="all")
        sp.add_argument("--days", type=float)
        sp.add_argument("--limit", type=int, default=30)
    sp = add("events", cmd_events)
    sp.add_argument("--account", choices=["sim", "live", "all"], default="all")
    sp.add_argument("--kind", help="comma list, e.g. trade_open,trade_close,decision")
    sp.add_argument("--limit", type=int, default=40)
    sp.add_argument("--follow", "-f", action="store_true")
    sp = add("scan", cmd_scan)
    sp.add_argument("--id", type=int, help="scan number (default: latest)")
    sp = add("decisions", cmd_decisions)
    sp.add_argument("--account", choices=["sim", "live", "all"], default="all")
    sp.add_argument("--limit", type=int, default=30)
    sp = add("export", cmd_export)
    sp.add_argument("--out", default=str(BASE_DIR / "reports"))
    sp = add("sql", cmd_sql)
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=100)

    a = p.parse_args(argv)
    con = connect(a.db or BASE_DIR / load_config().db_path)
    a.fn(con, a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
