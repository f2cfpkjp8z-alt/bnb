"""Run with:  python tests/test_basic.py     (no pytest, no network, no ccxt/firebase needed)"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profiles                                               # noqa: E402
import report                                                 # noqa: E402
from account import Account                                   # noqa: E402
from backtest import make_synthetic, prepare, simulate        # noqa: E402
from broker import PaperBroker                                # noqa: E402
from cloud import MemoryCloud, clean                          # noqa: E402
from commands import normalize_symbol, validate               # noqa: E402
from config import Config                                     # noqa: E402
from db import DB                                             # noqa: E402
from engine import Engine                                     # noqa: E402
from fake_exchange import FakeExchange                        # noqa: E402
from indicators import compute_features                       # noqa: E402
from risk import (Position, RiskManager, evaluate_exit, plan_trade,   # noqa: E402
                  size_quote, update_trail)
from scanner import select_universe                           # noqa: E402
from signals import score_frame                               # noqa: E402

logging.basicConfig(level=logging.ERROR)
PASS = []


def test(fn):
    fn()
    PASS.append(fn.__name__)
    print(f"PASS  {fn.__name__}")


# ============================================================================ strategy core
@test
def features_have_no_lookahead():
    cfg = Config()
    df = list(make_synthetic(1, days=10, seed=3).values())[0]
    n = 2000
    a, b = compute_features(df.iloc[:n]), compute_features(df)
    for col in a.columns:
        assert np.allclose(a[col].to_numpy(float), b[col].iloc[:n].to_numpy(float),
                           equal_nan=True, rtol=1e-9, atol=1e-9), f"look-ahead in {col}"
    sa, sb = score_frame(a, cfg), score_frame(b.iloc[:n], cfg)
    assert np.allclose(sa["score"].to_numpy(), sb["score"].to_numpy(), equal_nan=True)


@test
def random_walk_has_no_edge_after_costs():
    cfg = Config()
    avgs = []
    for seed in (0, 1, 2):
        raw = make_synthetic(8, days=30, seed=seed)
        tr, _ = simulate(prepare(raw, raw["SYN0/USDT"], cfg), cfg, 100.0)
        assert len(tr) > 30
        avgs.append(tr.pnl_pct.mean())
    assert np.mean(avgs) < 0.1, "suspiciously profitable on random data"


@test
def exit_rules():
    cfg = Config()
    p = Position("X/USDT", 100, 1, stop=98, tp=104, atr=1.3, opened_ts=0, highest=100)
    assert evaluate_exit(p, 100, 105, 97, 100, 0, cfg) == (98, "stop")
    assert evaluate_exit(p, 97, 99, 96, 98, 0, cfg) == (97, "stop_gap")
    assert evaluate_exit(p, 100, 104.5, 99, 104, 0, cfg) == (104, "tp")
    assert evaluate_exit(p, 100, 101, 99, 100.5, cfg.max_hold_bars, cfg)[1] == "time"
    assert evaluate_exit(p, 100, 101, 99, 100.5, 3, cfg) is None
    update_trail(p, 101.8, cfg)
    assert p.stop > 100


@test
def sizing_and_plan():
    cfg = Config()
    assert plan_trade(100, 0.05, cfg) is None
    stop, tp, sp = plan_trade(100, 0.5, cfg)
    s = size_quote(100, 100, sp, cfg, 5.0)
    assert 5.5 <= s <= 34.0, s
    assert size_quote(100, 3.0, sp, cfg, 5.0) == 0.0


@test
def risk_manager_limits():
    cfg = Config()
    rm = RiskManager(cfg)
    t = 1_800_000_000.0
    assert rm.can_open(t, 100.0)[0]
    rm.on_open(t); rm.on_close("A/USDT", -1.0, t)
    assert "cooling" in rm.can_open(t + 10, 100.0, "A/USDT")[1]
    for i in range(2):
        rm.on_open(t); rm.on_close(f"B{i}/USDT", -1.0, t)
    assert "paused" in rm.can_open(t + 10, 100.0)[1]
    rm2 = RiskManager(cfg)
    rm2.can_open(t, 100.0)
    rm2.on_close("C/USDT", -5.5, t)
    assert "daily loss cap" in rm2.can_open(t + 1, 94.5)[1]
    assert rm2.can_open(t + 86400, 94.5)[0]


# ============================================================================ profiles
@test
def profiles_apply_and_clamp():
    base = Config()
    before = base.risk_per_trade
    agg = profiles.apply_profile(base, "aggressive")
    con = profiles.apply_profile(base, "conservative")
    assert base.risk_per_trade == before, "base config must not be modified"
    assert con.entry_threshold > agg.entry_threshold and con.risk_per_trade < agg.risk_per_trade
    assert agg.max_positions > con.max_positions
    # indicator / exit settings must be identical so one shared scan serves every account
    assert (agg.sl_atr, agg.tp_atr, agg.rsi_max) == (con.sl_atr, con.tp_atr, con.rsi_max)
    wild = Config(risk_per_trade=0.9, max_positions=99, daily_loss_cap=0.9, entry_threshold=0.0,
                  max_position_pct=1.0, max_trades_per_day=999)
    profiles.clamp(wild)
    for k, (lo, hi) in profiles.HARD_LIMITS.items():
        assert lo <= getattr(wild, k) <= hi, k
    assert isinstance(wild.max_positions, int)
    try:
        profiles.apply_profile(base, "yolo")
        raise AssertionError("unknown profile accepted")
    except ValueError:
        pass
    for name in profiles.PROFILES:       # every shipped profile is itself inside the hard limits
        c = profiles.apply_profile(base, name)
        for k in profiles.HARD_LIMITS:
            assert getattr(c, k) == profiles.PROFILES[name][k], (name, k)


# ============================================================================ commands validation
@test
def symbol_normalisation():
    assert normalize_symbol("sol") == "SOL/USDT"
    assert normalize_symbol("solusdt") == "SOL/USDT"
    assert normalize_symbol(" Sol/usdt ") == "SOL/USDT"
    for bad in ("SOL/BTC", "", "a", "SOL;DROP TABLE", "../../etc", None, 5, "SOL/USDT/X"):
        assert normalize_symbol(bad) is None, bad


@test
def command_validation_rules():
    now = time.time()
    ok = lambda **kw: validate({"id": "1", "uid": "me", "ts": now, **kw}, "me", now, 600)  # noqa: E731
    assert ok(cmd="set_profile", args={"account": "sim", "profile": "aggressive"})[0]
    assert not ok(cmd="set_profile", args={"account": "sim", "profile": "yolo"})[0]
    assert not ok(cmd="set_profile", args={"account": "root", "profile": "balanced"})[0]
    assert not ok(cmd="set_profile", args={"account": "sim"})[0]
    assert not ok(cmd="set_profile", args={"account": "sim", "profile": "balanced", "x": 1})[0]
    assert not ok(cmd="set_enabled", args={"account": "live", "enabled": "true"})[0]   # must be a real bool
    assert not ok(cmd="rm_rf", args={})[0]
    assert not ok(cmd="exec", args={"code": "import os"})[0]
    assert not validate({"id": "1", "uid": "attacker", "ts": now, "cmd": "scan_now"}, "me", now, 600)[0]
    assert not validate({"id": "1", "uid": "me", "ts": now - 3600, "cmd": "scan_now"}, "me", now, 600)[0]
    assert not validate({"id": "1", "uid": "me", "ts": now + 9999, "cmd": "scan_now"}, "me", now, 600)[0]
    assert not validate({"id": "1", "uid": "me", "ts": None, "cmd": "scan_now"}, "me", now, 600)[0]
    assert not validate({"id": "1", "uid": None, "ts": now, "cmd": "scan_now"}, None, now, 600)[0], \
        "with no owner configured every command must be refused"


@test
def cloud_payloads_are_plain_python():
    d = clean({"a": np.float64(1.5), "b": float("nan"), "c": [np.int64(3), float("inf")],
               "d": {"e": np.bool_(True)}, 5: "x"})
    assert d == {"a": 1.5, "b": None, "c": [3, None], "d": {"e": True}, "5": "x"}
    json.dumps(d)


# ============================================================================ engine (offline)
def loosen_profiles():
    """Tests need trades on random data, so open the buy threshold. Restored by restore()."""
    saved = (dict(profiles.PROFILES["balanced"]), dict(profiles.PROFILES["conservative"]),
             profiles.HARD_LIMITS["entry_threshold"])
    profiles.PROFILES["balanced"]["entry_threshold"] = 0.0
    profiles.PROFILES["conservative"]["entry_threshold"] = 0.0
    profiles.HARD_LIMITS["entry_threshold"] = (0.0, 0.95)
    return saved


def restore(saved):
    profiles.PROFILES["balanced"].update(saved[0])
    profiles.PROFILES["conservative"].update(saved[1])
    profiles.HARD_LIMITS["entry_threshold"] = saved[2]


class StubNews:
    enabled = True

    def __init__(self, veto=()):
        self.veto, self.calls = set(veto), []

    def check(self, coin):
        self.calls.append(coin)
        return ("veto", "test headline") if coin in self.veto else ("neutral", "ok")


def make_engine(live=True, news=None, db_path=":memory:", cloud=None, **cfg_kw):
    tmp = Path(tempfile.mkdtemp(prefix="bnbtest_"))
    cfg = Config(btc_filter=False, min_quote_volume=1.0, **cfg_kw)
    ex = FakeExchange()
    cloud = cloud or MemoryCloud("owner")
    eng = Engine(cfg, ex, news or StubNews(), DB(db_path), cloud, state_dir=tmp / "state", flag_dir=tmp)
    eng.add_account(Account("sim", "paper", cfg, "balanced", PaperBroker(cfg, 100.0), 100.0, eng,
                            eng.state_dir, enabled=True))
    if live:
        eng.add_account(Account("live", "binance", cfg, "conservative", PaperBroker(cfg, 100.0), 100.0,
                                eng, eng.state_dir, enabled=False))
    return eng, ex, cloud, tmp


@test
def engine_scans_logs_and_trades_in_simulation():
    saved = loosen_profiles()
    try:
        eng, ex, cloud, tmp = make_engine()
        eng.scan()
        sim, live = eng.accounts["sim"], eng.accounts["live"]
        assert 1 <= len(sim.positions) <= sim.cfg.max_positions, sim.positions
        assert not live.positions, "live starts with entries OFF and must not trade"
        # everything is on record
        q = lambda s: eng.db.q(s)[0][0]                                    # noqa: E731
        assert q("SELECT COUNT(*) FROM scans") == 1
        assert q("SELECT COUNT(*) FROM analysis") == len(ex.data)
        assert q("SELECT COUNT(*) FROM decisions WHERE account='sim' AND action='buy'") == len(sim.positions)
        assert q("SELECT COUNT(*) FROM events WHERE kind='trade_open'") == len(sim.positions)
        assert q("SELECT COUNT(*) FROM events WHERE kind='scan_result'") == 1
        assert any(e["kind"] == "trade_open" for e in cloud.events), "web app must see the trade"
        # balance: started with 100, only fees/slippage show up until prices move
        eq = sim.equity(eng.prices)
        assert 99.0 < eq < 100.0, eq
        assert sim.invested() <= 100.0
        # close one through the stop, one through the target
        syms = list(sim.positions)
        ex.set_price(syms[0], sim.positions[syms[0]].stop * 0.995)
        winner = syms[1] if len(syms) > 1 else None
        if winner:
            ex.set_price(winner, sim.positions[winner].tp * 1.002)
        eng.manage()
        assert syms[0] not in sim.positions
        assert sim.realized < 0 or winner
        n_closed = q("SELECT COUNT(*) FROM trades WHERE account='sim'")
        assert n_closed == len(syms) - len(sim.positions)
        total = eng.db.q("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE account='sim'")[0][0]
        assert abs(total - sim.realized) < 1e-9, "DB and in-memory books must agree"
        assert abs(sim.equity(eng.prices) - (100 + sim.realized + sim.unrealized(eng.prices))) < 1e-9
    finally:
        restore(saved)


@test
def live_account_needs_web_switch_and_has_own_profile():
    saved = loosen_profiles()
    try:
        eng, ex, cloud, tmp = make_engine()
        live = eng.accounts["live"]
        assert live.profile == "conservative" and not live.enabled
        cloud.send("set_enabled", {"account": "live", "enabled": True})
        cloud.send("set_profile", {"account": "live", "profile": "aggressive"})
        cloud.send("set_profile", {"account": "sim", "profile": "conservative"})
        eng.process_commands()
        assert live.enabled and live.profile == "aggressive"
        assert eng.accounts["sim"].profile == "conservative", "accounts must be independent"
        assert live.cfg.max_positions == profiles.PROFILES["aggressive"]["max_positions"]
        assert live.risk.cfg is live.cfg, "risk manager must follow the new profile"
        assert [a[1] for a in cloud.acks] == ["done"] * 3
        eng.scan()
        assert live.positions, "after the switch the live account trades"
        # state survives a restart
        live.save()
        again = Account("live", "binance", eng.cfg, "conservative", PaperBroker(eng.cfg, 100.0), 100.0,
                        eng, eng.state_dir, enabled=False)
        assert again.load() and again.enabled and again.profile == "aggressive"
        assert set(again.positions) == set(live.positions)
    finally:
        restore(saved)


@test
def commands_are_checked_before_anything_happens():
    eng, ex, cloud, tmp = make_engine(live=False)
    sim = eng.accounts["sim"]
    cloud.send("set_profile", {"account": "sim", "profile": "aggressive"}, uid="intruder")
    cloud.send("set_profile", {"account": "sim", "profile": "aggressive"}, ts=time.time() - 7200)
    cloud.send("set_profile", {"account": "sim", "profile": "ludicrous"})
    cloud.send("set_enabled", {"account": "live", "enabled": True})            # live not armed on the PC
    cloud.send("add_watch", {"symbol": "NOPE/USDT"})                           # not a real market
    cloud.send("add_watch", {"symbol": "x; DROP TABLE trades"})
    cloud.send("shell", {"cmd": "calc.exe"})
    eng.process_commands()
    statuses = [a[1] for a in cloud.acks]
    assert statuses == ["rejected", "rejected", "rejected", "failed", "failed", "rejected", "rejected"], statuses
    assert sim.profile == "balanced", "a rejected command must change nothing"
    assert "not armed" in cloud.acks[3][2]
    assert eng.db.q("SELECT COUNT(*) FROM commands")[0][0] == 7, "every command is logged"
    # no owner configured -> the web app is read-only
    eng2, _, cloud2, _ = make_engine(live=False, cloud=MemoryCloud(owner_uid=None))
    cloud2.owner_uid = None
    cloud2.send("set_profile", {"account": "sim", "profile": "aggressive"}, uid=None)
    eng2.process_commands()
    assert cloud2.acks[0][1] == "rejected" and eng2.accounts["sim"].profile == "balanced"


@test
def watchlist_blocklist_and_scan_now():
    saved = loosen_profiles()
    try:
        eng, ex, cloud, tmp = make_engine(live=False, scan_top_n=3)
        uni = select_universe(ex.fetch_tickers(), ex.markets, eng.cfg)
        outside = [s for s in ex.data if s not in uni]
        assert outside, "need a coin outside the top-3"
        w = outside[0]
        cloud.send("add_watch", {"symbol": w.split("/")[0].lower()})
        eng.process_commands()
        assert w in eng.watchlist and cloud.acks[-1][1] == "done"
        eng.scan()
        analysed = {r[0] for r in eng.db.q("SELECT symbol FROM analysis")}
        assert w in analysed, "watched coin must be analysed every scan"
        # blocking removes it and stops any trading in it
        cloud.send("block", {"symbol": w})
        cloud.send("block", {"symbol": uni[0]})
        eng.process_commands()
        assert w not in eng.watchlist and set(eng.blocklist) == {w, uni[0]}
        eng.db.con.execute("DELETE FROM analysis"); eng.db.con.commit()
        eng.scan()
        analysed = {r[0] for r in eng.db.q("SELECT symbol FROM analysis")}
        assert w not in analysed and uni[0] not in analysed
        cloud.send("add_watch", {"symbol": uni[0]})                         # blocked -> must fail
        eng.process_commands()
        assert cloud.acks[-1][1] == "failed" and "blocked" in cloud.acks[-1][2]
        # persisted
        eng2 = Engine(eng.cfg, ex, StubNews(), DB(":memory:"), MemoryCloud(), state_dir=eng.state_dir,
                      flag_dir=tmp)
        eng2.load_state()
        assert set(eng2.blocklist) == {w, uni[0]}
        cloud.send("scan_now")
        eng._next_scan = time.time() + 999
        eng.process_commands()
        assert eng._next_scan == 0.0
    finally:
        restore(saved)


@test
def news_veto_and_orderbook_gate_block_entries():
    saved = loosen_profiles()
    try:
        probe, *_ = make_engine(live=False)
        probe.scan()
        bases = [s.split("/")[0] for s in probe.accounts["sim"].positions]
        assert bases
        eng, ex, cloud, tmp = make_engine(live=False, news=StubNews(veto=bases))
        eng.scan()
        assert not (set(bases) & {s.split("/")[0] for s in eng.accounts["sim"].positions})
        vetoed = eng.db.q("SELECT reason FROM decisions WHERE action='skip'")
        assert any("news veto" in r[0] for r in vetoed)
        # a thin order book also blocks entry
        eng3, ex3, *_ = make_engine(live=False)
        ex3.fetch_order_book = lambda sym, limit=20: {"bids": [[100.0, 1.0]], "asks": [[100.0005, 50.0]]}
        eng3.scan()
        assert not eng3.accounts["sim"].positions
        assert any("order book" in r[0] for r in eng3.db.q("SELECT reason FROM decisions"))
    finally:
        restore(saved)


@test
def loss_limits_and_kill_switches():
    saved = loosen_profiles()
    try:
        eng, ex, cloud, tmp = make_engine(live=False)
        sim = eng.accounts["sim"]
        eng.stop_file.write_text("x")                                       # STOP: analyse, never buy
        eng.scan()
        assert not sim.positions
        assert eng.db.q("SELECT COUNT(*) FROM analysis")[0][0] > 0, "STOP still analyses"
        assert any("STOP" in e["message"] for e in cloud.events)
        eng.stop_file.unlink()
        sim.risk.day_pnl = -0.06 * 100                                      # beyond the 5% daily cap
        sim.risk.day_start_equity = 100.0
        sim.risk.day = sim.risk._day(time.time())
        eng.scan()
        assert not sim.positions, "daily loss cap must stop new entries"
        sim.risk.day_pnl = 0.0
        eng._next_scan = 0
        eng.scan()
        assert sim.positions
        eng.panic_file.write_text("x")
        eng.run(once=True)                                                  # PANIC: flatten and return
        assert not sim.positions
    finally:
        restore(saved)


@test
def simulation_reset_and_status_payload():
    saved = loosen_profiles()
    try:
        eng, ex, cloud, tmp = make_engine()
        eng.scan()
        eng.snapshot_equity(time.time())
        st = eng.status()
        json.dumps(clean(st))                                               # must be serialisable
        assert set(st["accounts"]) == {"sim", "live"} and st["live_armed"]
        s = st["accounts"]["sim"]
        for k in ("equity", "cash", "realized", "unrealized", "pnl", "open", "stats", "limits", "profile"):
            assert k in s, k
        assert st["scanner"]["board"] and "verdict" in st["scanner"]["board"][0]
        assert set(st["profiles"]) == set(profiles.PROFILES)
        cloud.send("close_all", {"account": "sim"})
        cloud.send("reset_sim")
        eng.process_commands()
        sim = eng.accounts["sim"]
        assert not sim.positions and sim.realized == 0.0 and sim.broker.cash == 100.0
        assert abs(sim.equity(eng.prices) - 100.0) < 1e-9
        # engine without a live account reports that honestly
        eng2, *_ = make_engine(live=False)
        assert eng2.status()["live_armed"] is False and "live" not in eng2.status()["accounts"]
    finally:
        restore(saved)


@test
def balance_curve_is_one_compact_document_and_survives_restart():
    path = str(Path(tempfile.mkdtemp()) / "bot.db")
    eng, ex, cloud, tmp = make_engine(db_path=path)
    t0 = time.time()
    for i in range(5):
        eng.snapshot_equity(t0 + i * 300)
    last = cloud.equity[-1]
    assert set(last) == {"sim", "live"} and len(last["sim"]["t"]) == 5 == len(last["sim"]["v"])
    assert last["sim"]["v"][0] == 100.0 and last["sim"]["t"] == sorted(last["sim"]["t"])
    json.dumps(last)
    eng.curve_points = 3
    eng.snapshot_equity(t0 + 2000)
    assert len(cloud.equity[-1]["sim"]["t"]) == 3, "curve is capped"
    eng.db.close()
    eng2, _, cloud2, _ = make_engine(db_path=path, cloud=MemoryCloud("owner"))
    eng2.load_curves()
    assert len(eng2.curves["sim"]["t"]) == 6, "history is reloaded from SQLite after a restart"


@test
def reports_read_the_database():
    saved = loosen_profiles()
    try:
        path = str(Path(tempfile.mkdtemp()) / "bot.db")
        eng, ex, cloud, tmp = make_engine(db_path=path)
        eng.accounts["live"].enabled = True
        eng.scan()
        for s in list(eng.accounts["sim"].positions):
            eng.accounts["sim"].close(s, ex.last_close(s) * 0.99, "stop")
        eng.snapshot_equity(time.time())

        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                report.main(["--db", path, *argv])
            return buf.getvalue()

        out = run("summary")
        assert "SIM account" in out and "LIVE account" in out and "Balance now" in out
        assert "By exit reason" in out
        assert "stop" in run("trades")
        assert "BOUGHT" in run("events", "--kind", "trade_open")
        assert "COIN" in run("scan") or "BTC" in run("scan")
        assert run("decisions", "--account", "sim").strip()
        outdir = Path(tempfile.mkdtemp())
        run("export", "--out", str(outdir))
        assert (outdir / "trades.csv").exists() and (outdir / "analysis.csv").exists()
        assert "n" in run("sql", "SELECT COUNT(*) AS n FROM trades")
        try:                                                                # reports can never modify data
            run("sql", "DELETE FROM trades")
            raise AssertionError("report connection is writable")
        except sqlite3.OperationalError:
            pass
        # the bot keeps writing while a report holds the file open (WAL)
        eng.db.event(time.time(), "note", "still writing")
    finally:
        restore(saved)


print(f"\n{len(PASS)} tests passed")
