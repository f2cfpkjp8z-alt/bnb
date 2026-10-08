// Offline demo backend: fake but realistic data, and it reacts to commands like the real bot.
// Open index.html?demo=1  (add &live=0 to see the "real account not armed" view, &offline=1 for a dead bot).
const PROFILES = {
  conservative: { entry_threshold: 0.78, risk_per_trade: 0.010, max_positions: 2, max_trades_per_day: 4, daily_loss_cap: 0.03 },
  balanced:     { entry_threshold: 0.70, risk_per_trade: 0.015, max_positions: 3, max_trades_per_day: 8, daily_loss_cap: 0.05 },
  aggressive:   { entry_threshold: 0.62, risk_per_trade: 0.025, max_positions: 4, max_trades_per_day: 15, daily_loss_cap: 0.08 },
};
const DESC = {
  conservative: "conservative: buy score >= 0.78, risk 1.0%/trade, max 2 positions, 4 trades/day, daily stop -3%",
  balanced: "balanced: buy score >= 0.70, risk 1.5%/trade, max 3 positions, 8 trades/day, daily stop -5%",
  aggressive: "aggressive: buy score >= 0.62, risk 2.5%/trade, max 4 positions, 15 trades/day, daily stop -8%",
};
const COINS = ["PEPE", "SOL", "DOGE", "WIF", "SUI", "AVAX", "ARB", "INJ", "NEAR", "APT", "LINK", "FET"];
const PX = { PEPE: 0.0000121, SOL: 148.2, DOGE: 0.162, WIF: 1.84, SUI: 2.31, AVAX: 28.4, ARB: 0.61, INJ: 21.3, NEAR: 4.12, APT: 7.9, LINK: 14.2, FET: 1.27 };
let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
const now = () => Date.now() / 1000;

export class DemoBackend {
  constructor(q) {
    this.mode = "demo";
    this.armed = q.get("live") !== "0";
    this.offline = q.has("offline");
    this.subs = { status: [], curves: [], events: [], trades: [], commands: [] };
    this.cmds = []; this.events = []; this.trades = []; this.watch = ["WIF"]; this.block = [];
    this.accts = {
      sim: this._acct("paper", "balanced", true, 100, 3.42),
      ...(this.armed ? { live: this._acct("binance", "conservative", false, 100, -0.61) } : {}),
    };
    this.curves = { sim: this._curve(100, 103.42, 0.6), ...(this.armed ? { live: this._curve(100, 99.39, 0.3) } : {}) };
    this.beat = now() - (this.offline ? 900 : 4);
    this.board = this._board();
    this.accts.sim.open = [this._pos("SOL", 148.2, 0.9), this._pos("WIF", 1.84, -0.4)];
    this._recalc("sim");
    for (let i = 0; i < 18; i++) this._ev(["scan_result", "decision", "trade_close", "trade_open"][i % 4],
      ["Top scores: SOL 0.74, WIF 0.71, PEPE 0.63  | 2 candidate(s)", "sim: skip DOGE (score 0.72) - order book weak (bid/ask value 0.81, spread 0.052%)",
       "sim: SOLD INJ/USDT (tp) @ 21.84 | P&L +0.612 USDT (+1.84%) | held 47 min", "sim: BOUGHT SOL/USDT for 33.30 USDT @ 148.2 | stop 144.1 (-2.77%) | target 154.3 (+4.11%) | score 0.74"][i % 4],
      "sim", now() - (18 - i) * 410);
    for (let i = 0; i < 9; i++) this._trade(i);
    setInterval(() => this._tick(), 4000);
  }
  _acct(venue, profile, enabled, start, pnl) {
    return { venue, profile, enabled, start, equity: start + pnl, cash: start + pnl, realized: pnl, unrealized: 0, pnl, pnl_pct: pnl / start * 100,
      invested: 0, open: [], today: { pnl: pnl / 3, trades: 2 }, paused_until: 0, limits: { ...PROFILES[profile] },
      stats: { trades: 9, wins: 5, pnl, avg_pct: 0.31, win_rate: 55.6 }, exchange_usdt: venue === "paper" ? null : 100.0 - 0.61 };
  }
  _curve(a, b, vol) {
    const t = [], v = []; let x = a; const n = 288;
    for (let i = 0; i < n; i++) { x += (b - x) * 0.012 + (rnd() - 0.5) * vol; t.push(now() - (n - i) * 300); v.push(+x.toFixed(3)); }
    v[n - 1] = b; return { t, v };
  }
  _pos(sym, entry, pct) {
    const price = entry * (1 + pct / 100);
    return { symbol: sym + "/USDT", qty: 33 / entry, entry, price, pnl_pct: pct, pnl: 33 * pct / 100, stop: entry * 0.972, tp: entry * 1.041, opened_ts: now() - 1500, score: 0.74 };
  }
  _board() {
    return COINS.map((c, i) => {
      const score = Math.max(0.2, 0.78 - i * 0.045 + (rnd() - 0.5) * 0.05);
      const comp = { trend: rnd() > .3 ? 1 : .5, htf: rnd() > .4 ? 1 : 0, momentum: rnd() > .5 ? 1 : .5, macd: rnd() > .5 ? 1 : 0, breakout: rnd() > .6 ? 1 : 0, volume: +rnd().toFixed(2) };
      return { symbol: c + "/USDT", price: PX[c], score: +score.toFixed(2), gates: i !== 5 && i !== 9, rsi: 48 + rnd() * 28, components: comp, watch: this.watch.includes(c + "/USDT") };
    }).sort((a, b) => b.score - a.score);
  }
  _verdicts() {
    for (const r of this.board) {
      r.verdict = {};
      for (const n of Object.keys(this.accts)) {
        const a = this.accts[n], th = a.limits.entry_threshold;
        r.verdict[n] = a.open.some((p) => p.symbol === r.symbol) ? "position open" : !r.gates ? "failed a hard gate (trend / RSI / cost)"
          : r.score < th - 0.05 ? `score below ${th.toFixed(2)}` : !a.enabled ? "blocked: entries are OFF for this account" : r.score < th ? `score below ${th.toFixed(2)}` : "candidate";
      }
    }
  }
  _recalc(n) {
    const a = this.accts[n];
    a.unrealized = a.open.reduce((s, p) => s + p.pnl, 0); a.invested = a.open.reduce((s, p) => s + p.qty * p.entry, 0);
    a.equity = a.start + a.realized + a.unrealized; a.cash = a.start + a.realized - a.invested;
    a.pnl = a.equity - a.start; a.pnl_pct = a.pnl / a.start * 100;
  }
  _ev(kind, message, account, ts) { this.events.unshift({ id: "e" + Math.random(), ts: ts || now(), kind, account, symbol: null, level: "info", message }); this.events.length = Math.min(this.events.length, 150); }
  _trade(i) {
    const c = COINS[i % COINS.length], pnl = (rnd() - 0.4) * 1.4;
    this.trades.push({ id: "t" + i, account: "sim", symbol: c + "/USDT", opened_ts: now() - (i + 1) * 5400, closed_ts: now() - i * 5400 - 600, entry: PX[c], exit: PX[c] * (1 + pnl / 33),
      cost: 33, pnl, pnl_pct: pnl / 33 * 100, reason: pnl > 0.5 ? "tp" : pnl > 0 ? "trail" : "stop", score: 0.7, profile: "balanced" });
  }
  _tick() {
    if (this.offline) return;
    this.beat = now();
    for (const n of Object.keys(this.accts)) for (const p of this.accts[n].open) {
      p.pnl_pct += (rnd() - 0.48) * 0.3; p.price = p.entry * (1 + p.pnl_pct / 100); p.pnl = 33 * p.pnl_pct / 100;
    }
    for (const n of Object.keys(this.accts)) this._recalc(n);
    for (const n of Object.keys(this.curves)) { const c = this.curves[n]; c.t.push(now()); c.v.push(+this.accts[n].equity.toFixed(3)); }
    this._emit();
  }
  _emit() {
    this._verdicts();
    for (const f of this.subs.status) f(this._status());
    for (const f of this.subs.curves) f(JSON.parse(JSON.stringify(this.curves)));
    for (const f of this.subs.events) f(this.events.slice());
    for (const f of this.subs.trades) f(this.trades.slice().sort((a, b) => b.closed_ts - a.closed_ts));
    for (const f of this.subs.commands) f(this.cmds.slice());
  }
  _status() {
    return { version: "2.0", heartbeat: this.beat, started: now() - 7200, host: "demo-pc", stop_active: false, live_armed: this.armed, accounts: JSON.parse(JSON.stringify(this.accts)),
      scanner: { ts: now() - 95, scan_id: 412, btc_ok: true, btc_note: "", n_coins: this.board.length, next_scan: now() + 205, board: this.board },
      watchlist: this.watch, blocklist: this.block, news_enabled: false, profiles: DESC, commands_enabled: true };
  }
  _sub(k, fn) { this.subs[k].push(fn); setTimeout(() => this._emit(), 30); return () => {}; }
  onAuth(fn) { setTimeout(() => fn({ email: "demo@example.com", uid: "demo" }), 0); return () => {}; }
  async signIn() {} async signOut() { location.search = ""; }
  watchStatus(fn) { return this._sub("status", fn); } watchCurves(fn) { return this._sub("curves", fn); }
  watchEvents(fn) { return this._sub("events", fn); } watchTrades(fn) { return this._sub("trades", fn); }
  watchCommands(fn) { return this._sub("commands", fn); }
  async sendCommand(cmd, args) {
    const c = { id: "c" + Math.random().toString(36).slice(2, 8), cmd, args, status: "pending", result: "", createdAt: Date.now() };
    this.cmds.unshift(c); this.cmds.length = 12; this._emit();
    setTimeout(() => {
      try { c.result = this._apply(cmd, args); c.status = "done"; } catch (e) { c.result = e.message; c.status = "failed"; }
      this._ev("command", `web command ${cmd} ${JSON.stringify(args)}: ${c.status} - ${c.result}`);
      this._emit();
    }, this.offline ? 99999999 : 900);
    return c.id;
  }
  _apply(cmd, a) {
    const acct = a.account && (this.accts[a.account] || (() => { throw new Error("the live account is not armed on the PC (start the bot with --live or --testnet)"); })());
    if (cmd === "set_profile") { acct.profile = a.profile; acct.limits = { ...PROFILES[a.profile] }; return `${a.account} is now '${a.profile}'`; }
    if (cmd === "set_enabled") { acct.enabled = a.enabled; return `${a.account} entries ${a.enabled ? "ON" : "OFF"}`; }
    if (cmd === "close_all") { const n = acct.open.length; acct.open = []; this._recalc(a.account); return `closed ${n} position(s)`; }
    if (cmd === "close_position") { acct.open = acct.open.filter((p) => p.symbol !== a.symbol); this._recalc(a.account); return `closed ${a.symbol}`; }
    if (cmd === "reset_sim") { this.accts.sim = this._acct("paper", this.accts.sim.profile, true, 100, 0); return "simulation reset"; }
    if (cmd === "scan_now") return "scan scheduled";
    if (["add_watch", "remove_watch", "block", "unblock"].includes(cmd)) {
      let s = String(a.symbol || "").trim().toUpperCase().replace(/\s/g, "");
      s = s.includes("/") ? s : s.endsWith("USDT") && s.length > 4 ? s.slice(0, -4) + "/USDT" : s + "/USDT";
      if (!/^[A-Z0-9]{2,15}\/USDT$/.test(s)) throw new Error("that does not look like a coin");
      if (cmd === "add_watch") { if (s === "NOPE/USDT") throw new Error(`${s} is not a tradable USDT spot pair on Binance`); if (!this.watch.includes(s)) this.watch.push(s); return `${s} added to the watchlist (it will be analysed every scan)`; }
      if (cmd === "remove_watch") { this.watch = this.watch.filter((x) => x !== s); return `${s} removed from the watchlist`; }
      if (cmd === "block") { if (!this.block.includes(s)) this.block.push(s); this.watch = this.watch.filter((x) => x !== s); return `${s} blocked: never traded`; }
      this.block = this.block.filter((x) => x !== s); return `${s} unblocked`;
    }
    throw new Error("unknown command");
  }
}
