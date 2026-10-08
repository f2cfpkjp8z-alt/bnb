# bnb - Binance scanner bot with simulation, real mode, reports and a web control panel

Spot, long-only, built for a small account (about 100 USDT). It scans the most volatile liquid USDT
pairs every 5 minutes, scores each coin from six signals, and buys only when the combined score passes
a threshold and every safety check agrees. Each trade has a stop, a target, a trailing stop and a time stop.

## Two accounts, one market feed
| | Simulation (`sim`) | Real (`live`) |
|---|---|---|
| Money | paper, **starts at 100 USDT** | your Binance USDT (capped by `capital_limit`, default 100) |
| Orders | never sent to Binance | market orders on Binance (or the testnet with `--testnet`) |
| Exists when | always | only if you start the bot with `--live --i-understand-live-risk` |
| Starts with trading | ON | **OFF** until you switch it on in the web app |
| Default aggressiveness | balanced | conservative |

Both read the same real-time prices and the same analysis, so the simulation shows what the real account
would have done. Everything either account does is written to SQLite (`data/bot.db`) and mirrored to the web app.

## Quick start (Windows)
1. `setup.bat` (once: creates `.venv`, installs requirements, runs the offline tests)
2. `run_bot.bat` - simulation only. Safe: no keys needed.
3. `run_report.bat` - balances, P&L and the latest actions from the database.
4. Web app: follow **FIREBASE_SETUP.md**. To just look at it first: `serve_webapp.bat`, then open `http://localhost:8080/?demo=1`.

## Aggressiveness
`conservative`, `balanced`, `aggressive` change how picky and how big the bot is (buy score needed, risk per
trade, max positions, trades per day, daily loss stop). They never change the indicators or the stop/target
distances. Hard limits in `profiles.py` apply to every value, whatever the source.

## Reports (SQLite)
```
python report.py summary            # balance, return, win rate, profit factor, by coin / day / exit reason
python report.py trades --limit 30
python report.py events --follow    # live tail of everything the bot does and decides
python report.py scan               # latest scan: every coin, its score and signals, what was decided
python report.py decisions          # why coins were bought or skipped
python report.py export             # CSV of every table into reports/
python report.py sql "SELECT symbol, SUM(pnl) FROM trades GROUP BY symbol"
```
You can also open `data/bot.db` in DB Browser for SQLite. Tables: runs, events, scans, analysis,
decisions, trades, equity, commands. Reports open the file read-only, so they work while the bot runs.

## Web app
Shows balance and P&L per account, balance chart, open positions, the live scanner board (what is being
analysed and why each coin is or is not bought), the activity feed, trade history, and lets you change
aggressiveness, switch each account's trading on/off, sell positions, reset the simulation, add coins to
investigate and block coins. See `FIREBASE_SETUP.md` for what it can and cannot do.

## Other commands
```
python bot.py --once                     one scan, then exit
python bot.py --status                   balances and open positions
python bot.py --close-all sim|live|all   sell everything
python bot.py --live --i-understand-live-risk       arm the real account (starts OFF)
python bot.py --testnet                  use Binance testnet as the "real" account
python bot.py --no-cloud --no-news       local only
python backtest.py --days 60 --top 15    historical test (look only at the OUT-OF-SAMPLE block)
python tests/test_basic.py               offline tests
```
Kill switches (create a file in this folder): `STOP` = analyse only, open nothing; `PANIC` = close everything and exit.

## Honest limits
* **Stops are software stops.** They work only while the bot and your PC are running and online. If the PC
  sleeps or the bot is closed with a trade open, Binance has no protection in place. Turn off sleep.
* **Costs are the enemy.** A round trip costs about 0.3% (fees + slippage). More trades means more cost to earn back.
* **The signal weights are plain heuristics, not a proven edge.** Run the simulation for weeks before trusting it.
  On random data the backtester loses roughly the fees, which is the correct result.
* **Backtests use today's volatile coins** (survivorship bias). Trust only out-of-sample numbers.
* **Not yet exercised against live Binance or a real Firebase project.** It was developed in a sandbox that cannot
  reach either, using a simulated exchange and cloud. That is why the simulation account comes first, then testnet,
  then a small real amount.
* Most small retail bots lose money. Only use money you can afford to lose entirely. Check that Binance and
  API trading are available for your account and country.

## Files
`bot.py` start here - `engine.py` shared scan and decisions - `account.py` one account - `profiles.py` aggressiveness
and hard limits - `signals.py`/`indicators.py` scoring - `risk.py` sizing and exits - `broker.py` paper / Binance orders -
`db.py` + `report.py` SQLite - `cloud.py` + `commands.py` Firebase sync and command validation - `news.py` Gemini filter -
`backtest.py` - `webapp/` control panel - `firestore.rules`, `firebase.json` - `tests/`.
