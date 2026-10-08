import { createBackend } from "./backend.js";

const $ = (s) => document.querySelector(s);
const NS = "http://www.w3.org/2000/svg";

/* ---------- tiny DOM helper (all dynamic text goes through textContent: bot data is untrusted) ---------- */
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k in el && k !== "list") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  for (const kid of kids) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}

/* ---------- formatting ---------- */
const nf = (n, d = 2) => (n == null || !isFinite(n) ? "–" : Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
const sg = (n, d = 2) => (n == null || !isFinite(n) ? "–" : (n > 0 ? "+" : n < 0 ? "−" : "") + nf(Math.abs(n), d));
const usd = (n, d = 2) => "$" + nf(n, d);
const px = (n) => (n == null ? "–" : n >= 100 ? nf(n, 2) : n >= 1 ? nf(n, 4) : Number(n).toPrecision(4).replace(/0+$/, ""));
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const when = (ts) => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
const dur = (sec) => { sec = Math.max(0, Math.round(sec)); return sec < 90 ? sec + "s" : sec < 5400 ? Math.round(sec / 60) + " min" : (sec / 3600).toFixed(1) + " h"; };
const nowS = () => Date.now() / 1000;
const NAME = { sim: "Simulation", live: "Real account" };
const arrow = (n) => h("span", { class: n >= 0 ? "up" : "down", "aria-hidden": "true" }, n >= 0 ? "▲ " : "▼ ");
const pnlCell = (n, text) => h("span", { class: "num" }, arrow(n), h("span", { class: "sr" }, n >= 0 ? "gain " : "loss "), text);

/* ---------- state ---------- */
const state = { status: null, curves: {}, events: [], trades: [], commands: [], pending: {}, range: "24h",
                evFilter: "all", trFilter: "all", chartTable: false, loadedOnce: false };
let backend = null;
let rafPending = false;
const schedule = () => { if (!rafPending) { rafPending = true; requestAnimationFrame(() => { rafPending = false; renderAll(); }); } };

/* ---------- toasts & dialogs ---------- */
function toast(msg, kind = "info", ms = 5500) {
  const t = h("div", { class: "toast " + kind }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), ms);
}
function confirmDialog(title, body, okLabel, danger = false) {
  const d = $("#modal");
  $("#mtitle").textContent = title; $("#mbody").textContent = body;
  const ok = $("#mok"); ok.textContent = okLabel; ok.className = "btn " + (danger ? "danger" : "primary");
  return new Promise((resolve) => {
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
    d.returnValue = "cancel"; d.showModal();
  });
}

/* ---------- commands ---------- */
async function send(cmd, args, key, label) {
  const st = state.status;
  if (st && st.commands_enabled === false) return toast("The bot has no owner ID configured (FIREBASE_OWNER_UID), so it will ignore commands.", "bad", 9000);
  try {
    const id = await backend.sendCommand(cmd, args);
    state.pending[key] = { id, cmd, args, label, t: Date.now() };
    const stale = st && nowS() - st.heartbeat > 300;
    toast(stale ? `Sent: ${label}. The bot looks offline — the command waits up to 10 minutes.` : `Sent: ${label}. Waiting for the bot…`, stale ? "bad" : "info");
  } catch (e) {
    toast("Could not send the command: " + (e.message || e), "bad", 8000);
  }
  schedule();
}
function settleCommands(list) {
  for (const [key, p] of Object.entries(state.pending)) {
    const c = list.find((x) => x.id === p.id);
    if (c && c.status === "done") { toast(`✓ ${c.result || p.label}`, "ok"); delete state.pending[key]; }
    else if (c && (c.status === "failed" || c.status === "rejected")) { toast(`✗ ${p.label}: ${c.result}`, "bad", 9000); delete state.pending[key]; }
    else if (Date.now() - p.t > 90000) { toast(`No answer from the bot for “${p.label}”. Is it running?`, "bad", 9000); delete state.pending[key]; }
  }
}

/* ---------- header & banners ---------- */
function renderHeader() {
  const st = state.status, box = $("#hstatus"), ban = $("#banners");
  box.replaceChildren(); ban.replaceChildren();
  if (!st) { box.append(h("span", { class: "pill" }, h("span", { class: "dot" }), "Waiting for the bot…")); return; }
  const age = nowS() - st.heartbeat;
  const lvl = age < 90 ? "on" : age < 300 ? "stale" : "off";
  box.append(h("span", { class: "pill" }, h("span", { class: "dot " + lvl }), (lvl === "on" ? "Bot online" : lvl === "stale" ? "Bot slow" : "Bot OFFLINE") + " · updated " + dur(age) + " ago"));
  box.append(h("span", { class: "pill" }, `${st.host || "pc"} · v${st.version || "?"}`));
  box.append(h("span", { class: "pill" }, "News filter " + (st.news_enabled ? "on" : "off")));
  if (lvl === "off") ban.append(h("div", { class: "banner crit" }, h("span", {}, "⚠"), h("div", {}, h("b", {}, "The bot is not reporting. "), "No update for " + dur(age) + ". Open positions are NOT being watched by the bot while it is off. Check that the PC is awake and the bot window is running.")));
  if (st.stop_active) ban.append(h("div", { class: "banner warn" }, h("span", {}, "⏸"), h("div", {}, h("b", {}, "STOP file is present on the PC. "), "The bot keeps analysing but opens nothing new.")));
  if (st.commands_enabled === false) ban.append(h("div", { class: "banner warn" }, h("span", {}, "🔒"), h("div", {}, h("b", {}, "Read-only. "), "Set FIREBASE_OWNER_UID in the bot's .env to let this page send commands.")));
}

/* ---------- accounts ---------- */
function stat(label, value) { return h("div", { class: "stat" }, h("div", { class: "l" }, label), h("div", { class: "v num" }, value)); }

function accountCard(name, a, st) {
  const real = name === "live";
  const key = (k) => `${k}:${name}`;
  const pend = (k) => state.pending[key(k)];
  const key1 = h("span", { class: "key", style: { background: real ? "var(--series-2)" : "var(--series-1)" } });
  const head = h("div", { class: "acct-head" }, key1, h("span", { class: "title" }, NAME[name]),
    h("span", { class: "tag" + (real ? " real" : "") }, a.venue === "paper" ? "paper money" : a.venue === "testnet" ? "Binance testnet" : "REAL money"));
  const sw = h("button", { class: "switch" + (pend("enabled") ? " pending" : ""), role: "switch", "aria-checked": String(!!a.enabled), "aria-label": `Open new trades in ${NAME[name]}`,
    onclick: async () => {
      const want = !a.enabled;
      if (real && want && !(await confirmDialog("Let the bot trade REAL money?", `The bot will open and close trades with the real USDT in your Binance account (up to ${usd(a.start, 0)}), using the “${a.profile}” profile. Stops only work while your PC and the bot are running. You can lose this money.`, "Turn real trading ON", true))) return;
      send("set_enabled", { account: name, enabled: want }, key("enabled"), `${NAME[name]}: new trades ${want ? "ON" : "OFF"}`);
    } }, h("span", { class: "track" }), h("span", {}, a.enabled ? "Trading ON" : "Trading OFF"));
  head.append(h("span", { class: "spacer" }), sw);

  const pnl = a.pnl;
  const profiles = ["conservative", "balanced", "aggressive"];
  const seg = h("div", { class: "seg", role: "group", "aria-label": `Aggressiveness, ${NAME[name]}` }, profiles.map((p) =>
    h("button", { type: "button", "aria-pressed": String(a.profile === p), class: pend("profile") && pend("profile").args.profile === p ? "pending" : "",
      onclick: async () => {
        if (a.profile === p) return;
        if (real && p === "aggressive" && !(await confirmDialog("Switch the real account to aggressive?", "Larger positions, more trades per day and a lower score needed to buy. Losses can add up faster.", "Switch to aggressive", true))) return;
        send("set_profile", { account: name, profile: p }, key("profile"), `${NAME[name]} → ${p}`);
      } }, p[0].toUpperCase() + p.slice(1))));
  const L = a.limits || {};
  const desc = (st.profiles && st.profiles[a.profile]) || "";

  const nOpen = (a.open || []).length;
  const ctl = h("div", { class: "row ctl" },
    h("button", { class: "btn small danger", disabled: !nOpen, onclick: async () => {
      if (!(await confirmDialog(`Close all ${nOpen} position(s) in ${NAME[name]}?`, real ? "They are sold at the market price on Binance right now." : "They are closed at the current simulated price.", "Close all", true))) return;
      send("close_all", { account: name }, key("closeall"), `close all in ${NAME[name]}`);
    } }, "Close all positions"));
  if (!real) ctl.append(h("button", { class: "btn small", onclick: async () => {
    if (!(await confirmDialog("Reset the simulation?", "Open positions are dropped and the balance goes back to $100. The history stays in the database.", "Reset to $100", true))) return;
    send("reset_sim", {}, key("reset"), "reset simulation");
  } }, "Reset to $100"));

  const paused = a.paused_until && a.paused_until > nowS() ? h("div", { class: "hint warnc" }, "⏸ Paused after a losing streak, resumes in " + dur(a.paused_until - nowS())) : null;
  const limitHit = a.today && L.daily_loss_cap && a.today.pnl <= -L.daily_loss_cap * a.start ? h("div", { class: "hint warnc" }, "⏸ Daily loss limit reached: no new trades until tomorrow (UTC).") : null;

  return h("div", { class: "card" },
    head,
    h("div", { class: "big num" }, usd(a.equity)),
    h("div", { class: "sub num" }, pnlCell(pnl, `${sg(pnl)} USDT (${sg(a.pnl_pct)}%)`), " since start (" + usd(a.start, 0) + ")"),
    real && a.exchange_usdt != null ? h("div", { class: "sub num muted" }, "Binance wallet, free USDT: " + usd(a.exchange_usdt)) : null,
    h("div", { class: "stats" },
      stat("Cash", usd(a.cash)), stat("In trades", usd(a.invested)), stat("Realised", sg(a.realized)),
      stat("Unrealised", sg(a.unrealized)), stat("Win rate", a.stats && a.stats.trades ? `${nf(a.stats.win_rate, 0)}% of ${a.stats.trades}` : "–"),
      stat("Today", `${sg(a.today.pnl)} · ${a.today.trades}/${L.max_trades_per_day || "?"} trades`)),
    h("div", { class: "ctl" }, h("div", { class: "label" }, h("span", {}, "Aggressiveness"), pend("profile") ? h("span", { class: "warnc" }, "waiting for the bot…") : null), seg,
      h("div", { class: "hint" }, desc)),
    paused, limitHit, ctl);
}

function renderAccounts() {
  const st = state.status, box = $("#accounts");
  box.replaceChildren();
  if (!st) { box.append(h("div", { class: "card empty" }, "No data yet. Start the bot on your PC (python bot.py).")); return; }
  box.append(accountCard("sim", st.accounts.sim, st));
  if (st.accounts.live) box.append(accountCard("live", st.accounts.live, st));
  else box.append(h("div", { class: "card unarmed" },
    h("div", { class: "acct-head" }, h("span", { class: "key", style: { background: "var(--series-2)" } }), h("span", { class: "title" }, NAME.live), h("span", { class: "tag" }, "not armed")),
    h("p", {}, "The real-money account does not exist on the PC right now, so nothing here can trade real money. This is deliberate: it can only be armed on the PC, never from this page."),
    h("p", {}, "To arm it, restart the bot with ", h("code", {}, "python bot.py --live --i-understand-live-risk"), " (or ", h("code", {}, "--testnet"), " for Binance's fake-money test network). It then starts with trading OFF, and you switch it on here.")));
}

/* ---------- chart ---------- */
function niceTicks(lo, hi, n = 4) {
  const span = hi - lo || 1, step0 = span / n, mag = 10 ** Math.floor(Math.log10(step0)), norm = step0 / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag, out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}
function nearest(pts, t) {
  let lo = 0, hi = pts.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (pts[mid][0] < t) lo = mid; else hi = mid; }
  return Math.abs(pts[lo][0] - t) <= Math.abs(pts[hi][0] - t) ? pts[lo] : pts[hi];
}
function renderRangeFilters() {
  const box = $("#rangefilters"); box.replaceChildren();
  for (const [k, label] of [["6h", "6 h"], ["24h", "24 h"], ["7d", "7 days"], ["all", "All"]])
    box.append(h("button", { class: "chip", type: "button", "aria-pressed": String(state.range === k), onclick: () => { state.range = k; schedule(); } }, label));
  box.append(h("span", { class: "spacer" }), h("button", { class: "linkbtn", type: "button", onclick: () => { state.chartTable = !state.chartTable; schedule(); } }, state.chartTable ? "Show chart" : "Show as table"));
}
function renderChart() {
  const box = $("#chart"), legend = $("#chartlegend");
  box.replaceChildren(); legend.replaceChildren();
  const rangeSec = { "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, all: 1e12 }[state.range];
  const names = ["sim", "live"].filter((n) => state.curves[n] && state.curves[n].t && state.curves[n].t.length > 1);
  if (!names.length) { box.append(h("div", { class: "empty" }, "The balance curve appears after the bot has run for a few minutes.")); return; }
  const tmax = Math.max(...names.map((n) => state.curves[n].t.at(-1))), tmin = tmax - rangeSec;
  const series = names.map((n) => ({ name: n, pts: state.curves[n].t.map((t, i) => [t, state.curves[n].v[i]]).filter((p) => p[0] >= tmin) })).filter((x) => x.pts.length > 1);
  if (!series.length) { box.append(h("div", { class: "empty" }, "No balance points in this time range yet.")); return; }
  const color = { sim: "var(--series-1)", live: "var(--series-2)" };
  if (series.length > 1) for (const x of series) legend.append(h("span", { class: "item" }, h("span", { class: "key", style: { background: color[x.name] } }), NAME[x.name]));

  if (state.chartTable) {
    const times = series[0].pts, step = Math.max(1, Math.floor(times.length / 40)), rows = [];
    for (let i = times.length - 1; i >= 0; i -= step) rows.push(times[i][0]);
    box.append(h("div", { class: "tablewrap", style: { maxHeight: "300px", overflowY: "auto" } }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Time"), series.map((x) => h("th", { class: "r" }, NAME[x.name] + " (USDT)")))),
      h("tbody", {}, rows.map((t) => h("tr", {}, h("td", {}, when(t)), series.map((x) => h("td", { class: "r num" }, nf(nearest(x.pts, t)[1], 2)))))))));
    return;
  }

  const W = Math.max(280, box.clientWidth || 700), H = W < 520 ? 230 : 290, m = { l: 52, r: W < 520 ? 56 : 120, t: 12, b: 26 };
  const all = series.flatMap((x) => x.pts), x0 = Math.min(...all.map((p) => p[0])), x1 = Math.max(...all.map((p) => p[0]));
  let y0 = Math.min(...all.map((p) => p[1])), y1 = Math.max(...all.map((p) => p[1]));
  const startV = state.status && state.status.accounts.sim ? state.status.accounts.sim.start : null;
  if (startV != null) { y0 = Math.min(y0, startV); y1 = Math.max(y1, startV); }
  const pad = Math.max((y1 - y0) * 0.12, 0.25); y0 -= pad; y1 += pad;
  const X = (t) => m.l + ((t - x0) / (x1 - x0 || 1)) * (W - m.l - m.r), Y = (v) => m.t + (1 - (v - y0) / (y1 - y0)) * (H - m.t - m.b);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Balance over time, " + series.map((x) => `${NAME[x.name]} now ${nf(x.pts.at(-1)[1])} USDT`).join(", ") });
  for (const v of niceTicks(y0, y1, 4)) {
    svg.append(s("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: "var(--grid)", "stroke-width": 1 }));
    const t = s("text", { x: m.l - 8, y: Y(v) + 4, "text-anchor": "end", fill: "var(--text-3)", "font-size": 11.5 }, nf(v, v % 1 ? 1 : 0)); svg.append(t);
  }
  if (startV != null) {
    svg.append(s("line", { x1: m.l, x2: W - m.r, y1: Y(startV), y2: Y(startV), stroke: "var(--text-3)", "stroke-width": 1, "stroke-dasharray": "4 4" }));
    svg.append(s("text", { x: m.l + 4, y: Y(startV) - 4, fill: "var(--text-3)", "font-size": 11 }, "start " + nf(startV, 0)));
  }
  const spanH = (x1 - x0) / 3600;
  for (let i = 0; i <= 4; i++) {
    const t = x0 + ((x1 - x0) * i) / 4, d = new Date(t * 1000);
    svg.append(s("text", { x: X(t), y: H - 7, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle", fill: "var(--text-3)", "font-size": 11.5 },
      spanH <= 36 ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : d.toLocaleDateString([], { month: "short", day: "numeric" })));
  }
  const labelY = [];
  for (const x of series) {
    svg.append(s("polyline", { points: x.pts.map((p) => `${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" "), fill: "none", stroke: color[x.name], "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const last = x.pts.at(-1);
    svg.append(s("circle", { cx: X(last[0]), cy: Y(last[1]), r: 4, fill: color[x.name], stroke: "var(--surface)", "stroke-width": 2 }));
    labelY.push({ x, y: Y(last[1]) });
  }
  labelY.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labelY.length; i++) if (labelY[i].y - labelY[i - 1].y < 15) labelY[i].y = labelY[i - 1].y + 15;
  for (const l of labelY) {
    const last = l.x.pts.at(-1);
    svg.append(s("text", { x: W - m.r + 10, y: l.y + 4, fill: "var(--text)", "font-size": 12.5, "font-weight": 600 }, (W < 520 ? "" : NAME[l.x.name] + " ") + nf(last[1])));
  }
  const vline = s("line", { y1: m.t, y2: H - m.b, stroke: "var(--text-3)", "stroke-width": 1, visibility: "hidden" });
  const dots = series.map((x) => s("circle", { r: 4.5, fill: color[x.name], stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }));
  svg.append(vline, ...dots);
  const hit = s("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent", style: "touch-action:pan-y" });
  svg.append(hit);
  const tip = h("div", { class: "tip", hidden: true });
  const show = (ev) => {
    const r = svg.getBoundingClientRect(), px_ = ((ev.clientX - r.left) / r.width) * W;
    const t = x0 + ((px_ - m.l) / (W - m.l - m.r)) * (x1 - x0), rows = [];
    let tx = null;
    series.forEach((x, i) => { const p = nearest(x.pts, t); tx = tx ?? p[0]; dots[i].setAttribute("cx", X(p[0])); dots[i].setAttribute("cy", Y(p[1])); dots[i].setAttribute("visibility", "visible");
      rows.push(h("div", { class: "line" }, h("span", { class: "nm" }, h("span", { class: "key", style: { background: color[x.name] } }), NAME[x.name]), h("b", {}, nf(p[1]) + " USDT"))); });
    vline.setAttribute("x1", X(tx)); vline.setAttribute("x2", X(tx)); vline.setAttribute("visibility", "visible");
    tip.replaceChildren(h("div", { class: "t" }, when(tx)), ...rows); tip.hidden = false;
    const left = (X(tx) / W) * r.width, flip = left > r.width - 190;
    tip.style.left = (flip ? left - 170 : left + 14) + "px"; tip.style.top = "8px";
  };
  const hide = () => { vline.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); tip.hidden = true; };
  hit.addEventListener("pointermove", show); hit.addEventListener("pointerdown", show); hit.addEventListener("pointerleave", hide);
  box.append(svg, tip);
}

/* ---------- positions, scanner, trades ---------- */
function renderPositions() {
  const box = $("#positions"), st = state.status;
  box.replaceChildren();
  const rows = [];
  if (st) for (const [n, a] of Object.entries(st.accounts)) for (const p of a.open || []) rows.push([n, p]);
  if (!rows.length) { box.append(h("div", { class: "empty" }, "No open positions.")); return; }
  box.append(h("table", {}, h("thead", {}, h("tr", {}, ["Account", "Coin", "Entry", "Now", "P&L", "Stop", "Target", "Held", ""].map((x, i) => h("th", { class: i > 1 && i < 8 ? "r" : "" }, x)))),
    h("tbody", {}, rows.map(([n, p]) => h("tr", {},
      h("td", {}, h("span", { class: "badge " + n }, NAME[n])), h("td", {}, h("b", {}, p.symbol)),
      h("td", { class: "r num" }, px(p.entry)), h("td", { class: "r num" }, px(p.price)),
      h("td", { class: "r" }, pnlCell(p.pnl_pct, `${sg(p.pnl_pct)}% (${sg(p.pnl, 3)})`)),
      h("td", { class: "r num" }, px(p.stop)), h("td", { class: "r num" }, px(p.tp)), h("td", { class: "r num" }, dur(nowS() - p.opened_ts)),
      h("td", {}, h("button", { class: "btn small", onclick: async () => {
        if (!(await confirmDialog(`Sell ${p.symbol} now?`, `${NAME[n]}: closes this position at the market price.`, "Sell now", true))) return;
        send("close_position", { account: n, symbol: p.symbol }, `close:${n}:${p.symbol}`, `sell ${p.symbol} (${NAME[n]})`);
      } }, "Sell now")))))));
}
function verdictCell(v) {
  if (!v) return h("td", { class: "muted" }, "–");
  if (v === "BOUGHT" || v === "position open") return h("td", {}, h("span", { class: "ok" }, "● " + (v === "BOUGHT" ? "Bought" : "In position")));
  if (v === "candidate") return h("td", {}, h("span", { class: "ok" }, "▲ candidate"));
  if (v.startsWith("blocked")) return h("td", { class: "warnc" }, "⏸ " + v.replace("blocked: ", ""));
  return h("td", { class: "muted" }, v);
}
function renderScanner() {
  const box = $("#scanner"), meta = $("#scanmeta"), st = state.status;
  box.replaceChildren(); meta.textContent = "";
  if (!st || !st.scanner || !st.scanner.board || !st.scanner.board.length) { box.append(h("div", { class: "empty" }, "Waiting for the first scan…")); return; }
  const sc = st.scanner, thS = st.accounts.sim.limits.entry_threshold, thL = st.accounts.live ? st.accounts.live.limits.entry_threshold : null;
  const next = sc.next_scan - nowS();
  meta.textContent = `last scan ${dur(nowS() - sc.ts)} ago · ${sc.n_coins} coins · next ${next > 0 ? "in " + dur(next) : "due"} · BTC filter ${sc.btc_ok ? "OK" : "BLOCKING: " + (sc.btc_note || "")}`;
  const C = ["trend", "htf", "momentum", "macd", "breakout", "volume"];
  box.append(h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Coin"), h("th", { class: "r" }, "Price"), h("th", {}, "Score"), h("th", { class: "r" }, "RSI"), h("th", {}, "Signals"), h("th", {}, "Gates"), h("th", {}, "Simulation"), st.accounts.live ? h("th", {}, "Real account") : null)),
    h("tbody", {}, sc.board.map((r) => h("tr", {},
      h("td", {}, h("b", {}, r.symbol), r.watch ? h("span", { class: "muted", title: "on your investigate list" }, " ★") : null),
      h("td", { class: "r num" }, px(r.price)),
      h("td", {}, h("span", { class: "num", style: { display: "inline-block", width: "36px" } }, nf(r.score, 2)),
        h("span", { class: "bar", title: `score ${nf(r.score, 2)}` }, h("i", { style: { width: Math.min(100, r.score * 100) + "%" } }), h("b", { style: { left: thS * 100 + "%" } }), thL != null ? h("b", { class: "live", style: { left: thL * 100 + "%" } }) : null)),
      h("td", { class: "r num" }, nf(r.rsi, 0)),
      h("td", {}, h("span", { class: "cells" }, C.map((k) => h("span", { title: `${k}: ${nf(r.components[k], 2)}`, style: { opacity: String(Math.max(0.08, r.components[k] || 0)) } })))),
      h("td", {}, r.gates ? h("span", { class: "ok" }, "✓ pass") : h("span", { class: "bad" }, "✗ failed")),
      verdictCell(r.verdict && r.verdict.sim), st.accounts.live ? verdictCell(r.verdict && r.verdict.live) : null)))));
}
function renderTrades() {
  const box = $("#trades"), f = $("#tradefilters");
  f.replaceChildren();
  for (const [k, label] of [["all", "All"], ["sim", "Simulation"], ["live", "Real account"]])
    f.append(h("button", { class: "chip", type: "button", "aria-pressed": String(state.trFilter === k), onclick: () => { state.trFilter = k; schedule(); } }, label));
  box.replaceChildren();
  const rows = state.trades.filter((t) => state.trFilter === "all" || t.account === state.trFilter).slice(0, 50);
  if (!rows.length) { box.append(h("div", { class: "empty" }, "No closed trades yet.")); return; }
  box.append(h("table", {}, h("thead", {}, h("tr", {}, ["Closed", "Account", "Coin", "Exit", "Entry", "Exit price", "P&L (USDT)", "P&L %", "Held"].map((x, i) => h("th", { class: i > 3 ? "r" : "" }, x)))),
    h("tbody", {}, rows.map((t) => h("tr", {}, h("td", {}, when(t.closed_ts)), h("td", {}, h("span", { class: "badge " + t.account }, NAME[t.account] || t.account)), h("td", {}, h("b", {}, t.symbol)),
      h("td", {}, t.reason), h("td", { class: "r num" }, px(t.entry)), h("td", { class: "r num" }, px(t.exit)),
      h("td", { class: "r" }, pnlCell(t.pnl, sg(t.pnl, 3))), h("td", { class: "r num" }, sg(t.pnl_pct) + "%"), h("td", { class: "r num" }, dur(t.closed_ts - t.opened_ts)))))));
}

/* ---------- feed, lists, commands ---------- */
const FEED = { all: null, trades: ["trade_open", "trade_close", "trail"], decisions: ["decision", "news"], scans: ["scan_result", "scan"], system: ["setting", "command", "start", "stop", "panic", "warning", "error"] };
function renderFeed() {
  const f = $("#feedfilters"), box = $("#feed");
  f.replaceChildren();
  for (const [k, label] of [["all", "Everything"], ["trades", "Trades"], ["decisions", "Decisions"], ["scans", "Scans"], ["system", "Settings & alerts"]])
    f.append(h("button", { class: "chip", type: "button", "aria-pressed": String(state.evFilter === k), onclick: () => { state.evFilter = k; schedule(); } }, label));
  const keep = FEED[state.evFilter];
  const rows = state.events.filter((e) => !keep || keep.includes(e.kind)).slice(0, 120);
  box.replaceChildren();
  if (!rows.length) { box.append(h("div", { class: "empty" }, "Nothing yet.")); return; }
  for (const e of rows) box.append(h("div", { class: "ev " + (e.level || "info") },
    h("span", { class: "time" }, clock(e.ts)), h("span", { class: "acc" }, e.account ? h("span", { class: "badge " + e.account }, e.account) : ""),
    h("span", { class: "kind muted" }, e.kind), h("span", { class: "msg" }, e.message)));
}
function chips(box, list, cmd, label) {
  box.replaceChildren();
  if (!list.length) { box.append(h("span", { class: "muted" }, "none")); return; }
  for (const sym of list) box.append(h("span", { class: "chip removable" }, sym,
    h("button", { type: "button", "aria-label": `${label} ${sym}`, title: label, onclick: () => send(cmd, { symbol: sym }, `${cmd}:${sym}`, `${label} ${sym}`) }, "×")));
}
function renderLists() {
  const st = state.status;
  chips($("#watchchips"), st ? st.watchlist || [] : [], "remove_watch", "stop investigating");
  chips($("#blockchips"), st ? st.blocklist || [] : [], "unblock", "unblock");
}
function renderCommands() {
  const box = $("#commands"); box.replaceChildren();
  if (!state.commands.length) { box.append(h("div", { class: "empty" }, "No commands sent yet.")); return; }
  box.append(h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Sent"), h("th", {}, "Command"), h("th", {}, "Status"), h("th", {}, "Bot's answer"))),
    h("tbody", {}, state.commands.map((c) => h("tr", {}, h("td", {}, when(c.createdAt / 1000)), h("td", {}, h("code", {}, c.cmd), " " + Object.values(c.args || {}).join(" ")),
      h("td", { class: c.status === "done" ? "ok" : c.status === "pending" ? "warnc" : "bad" }, c.status === "done" ? "✓ done" : c.status === "pending" ? "… waiting" : "✗ " + c.status), h("td", { class: "muted" }, c.result))))));
}

function renderAll() {
  renderHeader(); renderAccounts(); renderRangeFilters(); renderChart(); renderPositions(); renderScanner(); renderFeed(); renderTrades(); renderLists(); renderCommands();
  const dim = !state.loadedOnce; $("#app").classList.toggle("opacity-low", dim);
}

/* ---------- boot ---------- */
function watchForm(formId, inputId, cmd, label) {
  $(formId).addEventListener("submit", (e) => {
    e.preventDefault();
    const inp = $(inputId), v = inp.value.trim();
    if (!v) return;
    send(cmd, { symbol: v }, `${cmd}:${v.toUpperCase()}`, `${label} ${v.toUpperCase()}`);
    inp.value = "";
  });
}

async function start() {
  backend = await createBackend();
  if (backend.mode === "unconfigured") { $("#setup").hidden = false; return; }
  const onErr = (e) => toast("Cannot read data: " + (e && e.message ? e.message : e) + " (check firestore.rules and that you are signed in as the owner)", "bad", 12000);
  let started = false;
  backend.onAuth((user) => {
    $("#login").hidden = !!user; $("#app").hidden = !user;
    if (!user) return;
    $("#who").textContent = backend.mode === "demo" ? "demo data" : user.email || "";
    if (started) return;
    started = true;
    backend.watchStatus((v) => { state.status = v; state.loadedOnce = true; schedule(); }, onErr);
    backend.watchCurves((v) => { state.curves = v || {}; schedule(); }, onErr);
    backend.watchEvents((v) => { state.events = v; schedule(); }, onErr);
    backend.watchTrades((v) => { state.trades = v; schedule(); }, onErr);
    backend.watchCommands((v) => { state.commands = v; settleCommands(v); schedule(); }, onErr);
    schedule();
  });
  $("#loginform").addEventListener("submit", async (e) => {
    e.preventDefault(); $("#loginerr").textContent = "";
    try { await backend.signIn($("#email").value.trim(), $("#pw").value); }
    catch (err) { $("#loginerr").textContent = /invalid|credential|password|user/i.test(err.code || err.message) ? "Wrong email or password." : (err.message || "Sign-in failed."); }
  });
  $("#signout").addEventListener("click", () => backend.signOut());
  $("#scannow").addEventListener("click", () => send("scan_now", {}, "scan_now", "scan now"));
  watchForm("#watchform", "#watchin", "add_watch", "investigate");
  watchForm("#blockform", "#blockin", "block", "block");
  new ResizeObserver(() => schedule()).observe($("#chart"));
  setInterval(() => { if (!$("#app").hidden) { renderHeader(); renderScanner(); } }, 1000);
}
start().catch((e) => { console.error(e); document.body.prepend(h("pre", { style: { padding: "16px", color: "crimson" } }, "Failed to start: " + e.message)); });
