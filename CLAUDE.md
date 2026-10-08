# CLAUDE.md — project handoff

## What this is
Binance **spot** trading bot (Python) for a ~100 USDT budget. Scans many coins on 5-min candles, picks pairs automatically, trades often. Runs on the owner's Windows PC (folder `D:\GD\bnB`). Optional Gemini Flash Lite news filter (no key yet → `--no-news` / unset `GEMINI_API_KEY`).

## Design
- Two accounts share one market scan:
  - `sim`: paper money, starts 100 USDT, never touches Binance. Always on.
  - `live`: real Binance (or `--testnet`). Exists ONLY if the bot is started with `--live --i-understand-live-risk` (or `--testnet`). Starts with entries OFF. The web app can toggle entries but can never create the account.
- Strategy: EMA/RSI/MACD/Bollinger/ATR/volume z-score + 1h trend -> weighted score 0-1, hard gates (trend>0, RSI<78, TP >= 2.5x round-trip cost), BTC-drop filter, order-book gate, ATR stops (SL 1.5, TP 3.0), trailing stop, 6h time stop. Fees 0.1%/side + 0.05% slippage.
- Profiles (conservative/balanced/aggressive) in `profiles.py` change only risk/threshold/limits; always clamped by `HARD_LIMITS`. Raising limits = edit the file on the PC, never from the web.
- Storage: SQLite `data/bot.db` (events, scans, analysis, decisions, trades, equity, commands). Reports: `python report.py --help`.
- Web app (`webapp/`, Firebase Auth + Firestore): live status, balances per mode, switch profile/entries, add/remove/block pairs, close positions. Commands go through the `commands` collection; `commands.py` whitelists and validates (owner UID, 600s expiry, arg checks), bot acks. `?demo=1` runs the UI with fake data.
- Firestore layout: `bot/status` (every 30s), `bot/equity` (one doc, parallel arrays, 2000 pts), `events`, `trades`, `commands`.

## File map
config.py, profiles.py, account.py, engine.py, commands.py, cloud.py (Null/Memory/Firebase), db.py, bot.py (CLI), report.py, signals.py, indicators.py, risk.py, scanner.py, broker.py (PaperBroker, CcxtBroker), news.py, backtest.py, tests/ (18 tests + fake exchange), webapp/, firestore.rules, FIREBASE_SETUP.md, README.md, *.bat helpers.

## Run
```
git pull
.\setup.bat          # venv + requirements
.\run_bot.bat        # sim only (safe)
python bot.py --once # single scan
python bot.py --status
python -m pytest tests -q
```
Kill switches: create file `STOP` (no new entries) or `PANIC` (close all and exit) in the bot folder.

## Status / caveats
- Everything was built and tested offline with fakes (FakeExchange, MemoryCloud) because the dev sandbox cannot reach Binance, Firebase or PyPI. **The first run against real Binance and real Firebase is untested** — expect small fixes; do them first.
- Firebase not configured yet: fill `webapp/firebase-config.js`, set owner UID in `firestore.rules` and `FIREBASE_OWNER_UID` in `.env`, service account JSON on the PC (see FIREBASE_SETUP.md). Use `--no-cloud` until then.
- Binance public market data needs no API key. Keys only for live/testnet; create them without withdrawal permission.

## Rules for Claude
- Commit and push **straight to main** (no branches, no PRs) unless told otherwise.
- Never place real trades or arm live mode; the owner does that themselves on the PC after watching sim for weeks.
- Never print or commit secrets (.env, serviceAccount*.json are gitignored).
- Owner prefers short answers and minimal text.
- If a linked computer with a PowerShell/shell tool is available, run the setup there yourself (git pull, setup.bat, start bot, fix errors) instead of asking the owner to type commands.
- Commit messages end with the attribution lines from the session reminder.
