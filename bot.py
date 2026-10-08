"""Start the bot.  Two accounts run side by side on the same real-time market data:

  sim   simulated money, starts at 100 USDT, no orders ever reach Binance   (always on)
  live  real Binance money, only exists if you arm it:  --live --i-understand-live-risk
        (or --testnet to use Binance's fake-money testnet instead of real money)

  python bot.py                                   # simulation only (safe default)
  python bot.py --live --i-understand-live-risk   # simulation + real account
  python bot.py --once                            # one scan, then exit
  python bot.py --status                          # balances and open positions
  python bot.py --close-all sim|live|all          # sell everything and exit

The live account starts with new entries OFF. Turn them on from the web app (or with
--live-on). Kill switches (create the file in this folder):
  STOP   -> keep analysing, open nothing new      PANIC -> close everything and exit

Reports: python report.py --help     Web app: see FIREBASE_SETUP.md
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys

from account import Account
from broker import CcxtBroker, PaperBroker
from cloud import build_cloud
from config import BASE_DIR, load_config
from db import DB
from engine import Engine
from news import NewsFilter
from profiles import PROFILES

log = logging.getLogger("bot")
LOG_DIR = BASE_DIR / "logs"


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=2_000_000, backupCount=3,
                                              encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)
    for noisy in ("urllib3", "google", "grpc", "firebase_admin"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_engine(args) -> Engine:
    import ccxt
    cfg = load_config()
    if args.capital:
        cfg.capital_limit = args.capital
    if args.sim_profile:
        cfg.sim_profile = args.sim_profile
    if args.live_profile:
        cfg.live_profile = args.live_profile

    data_ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    data_ex.load_markets()
    news = NewsFilter(None if args.no_news else os.getenv("GEMINI_API_KEY"),
                      cfg.gemini_model, cfg.news_ttl_seconds)
    db = DB(BASE_DIR / cfg.db_path)
    cloud = build_cloud(disabled=args.no_cloud)
    eng = Engine(cfg, data_ex, news, db, cloud)

    eng.add_account(Account("sim", "paper", cfg, cfg.sim_profile, PaperBroker(cfg, cfg.sim_start_balance),
                            cfg.sim_start_balance, eng, eng.state_dir, enabled=True))

    if args.live or args.testnet:
        testnet = bool(args.testnet)
        prefix = "BINANCE_TESTNET" if testnet else "BINANCE"
        key, secret = os.getenv(f"{prefix}_API_KEY"), os.getenv(f"{prefix}_API_SECRET")
        if not key or not secret:
            sys.exit(f"Missing {prefix}_API_KEY / {prefix}_API_SECRET in .env")
        if not testnet and not args.i_understand_live_risk:
            sys.exit("--live trades REAL money. Add --i-understand-live-risk once you have watched the "
                     "simulation account for a few weeks.")
        broker = CcxtBroker(cfg, key, secret, testnet=testnet)
        live = Account("live", "testnet" if testnet else "binance", cfg, cfg.live_profile, broker,
                       cfg.capital_limit, eng, eng.state_dir, enabled=False)
        eng.add_account(live)
    return eng


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--live", action="store_true", help="arm the real-money account")
    p.add_argument("--testnet", action="store_true", help="arm the live account on Binance testnet")
    p.add_argument("--i-understand-live-risk", action="store_true")
    p.add_argument("--live-on", action="store_true", help="start with live entries ON")
    p.add_argument("--capital", type=float, help="USDT the live account may use (default 100)")
    p.add_argument("--sim-profile", choices=list(PROFILES))
    p.add_argument("--live-profile", choices=list(PROFILES))
    p.add_argument("--once", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--close-all", choices=["sim", "live", "all"])
    p.add_argument("--no-news", action="store_true")
    p.add_argument("--no-cloud", action="store_true", help="do not connect to Firebase")
    args = p.parse_args(argv)

    setup_logging()
    eng = build_engine(args)

    if args.status or args.close_all:
        for a in eng.accounts.values():
            a.load()
        eng.load_state()
        if args.close_all:
            targets = list(eng.accounts) if args.close_all == "all" else [args.close_all]
            for n in targets:
                if n not in eng.accounts:
                    log.error("account %s is not armed", n)
                    continue
                a = eng.accounts[n]
                a.close_all(eng._bids(list(a.positions)), "manual")
        else:
            eng._bids(sorted({s for a in eng.accounts.values() for s in a.positions}))
            for n, s in eng.status()["accounts"].items():
                log.info("%s [%s, %s, entries %s]: balance %.2f USDT (%+.2f%%) | realised %+.2f | "
                         "open %d", n, s["venue"], s["profile"], "ON" if s["enabled"] else "OFF",
                         s["equity"], s["pnl_pct"], s["realized"], len(s["open"]))
                for o in s["open"]:
                    log.info("   %-12s entry %.6g now %.6g (%+.2f%%) stop %.6g target %.6g",
                             o["symbol"], o["entry"], o["price"], o["pnl_pct"], o["stop"], o["tp"])
        eng.cloud.close()
        return 0

    if args.live_on and "live" in eng.accounts:
        eng.accounts["live"].load()
        eng.accounts["live"].enabled = True
    try:
        eng.run(once=args.once)
    except KeyboardInterrupt:
        for a in eng.accounts.values():
            a.save()
        eng.emit("stop", "stopped by user. Open positions are NOT closed and have no stop orders "
                 "on the exchange: restart the bot or run --close-all.", level="warning")
    finally:
        eng.cloud.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
