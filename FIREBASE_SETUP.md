# Web app setup (Firebase) - about 15 minutes, free tier is enough

The bot on your PC pushes its status to Firebase, and the web page reads it. When you press a
button on the page, it writes a *command*; the bot picks it up, **validates it again**, and acts.
Your PC never accepts inbound connections.

Free-tier usage is roughly 5,000 writes and a few thousand reads a day (limits: 20,000 / 50,000).

## 1. Create the project
1. Go to https://console.firebase.google.com, **Add project** (turn Google Analytics off).
2. **Build > Firestore Database > Create database**, production mode, any region near you.
3. **Build > Authentication > Get started > Email/Password > Enable**.
4. Authentication > **Users > Add user**: your email and a strong password.
   Copy the **User UID** shown in that list.
5. Optional hardening: Authentication > **Settings > User actions**, untick *Enable create (sign-up)*.

## 2. Connect the web page
1. Project settings (gear) > **General > Your apps > Web (`</>`)**, register an app (no hosting needed here).
2. Copy the `firebaseConfig` values into `webapp/firebase-config.js`
   (they are not secrets; the rules and your login protect the data).

## 3. Lock the database to you
1. Open `firestore.rules` and replace `PASTE_YOUR_FIREBASE_UID_HERE` with your UID.
2. Firestore > **Rules** tab, paste the file's content, **Publish**.
   (Or: `npm i -g firebase-tools`, `firebase login`, `firebase use --add`, `firebase deploy --only firestore:rules`.)

## 4. Connect the bot
1. Project settings > **Service accounts > Generate new private key**.
   Save the file as `serviceAccount.json` in the bot folder. **Keep it private**; it is git-ignored.
2. In `.env` set `FIREBASE_OWNER_UID=<your UID>` (and leave `FIREBASE_SERVICE_ACCOUNT=serviceAccount.json`).
3. `pip install -r requirements.txt`, then start the bot (`run_bot.bat`).
   The log should say `Firebase connected` and, within a minute, the web page fills with data.

## 5. Open the web page
* On the PC: double-click `serve_webapp.bat`, then open http://localhost:8080 and sign in.
* From anywhere (phone): `firebase deploy --only hosting`, then open the `*.web.app` address it prints.
* No setup needed to just look around: `webapp/index.html?demo=1` shows the page with fake data.

## 6. Optional: auto-delete old log entries
Firestore > **TTL** > create a policy on collection group `events`, field `expireAt`.
(The SQLite database on your PC keeps the full history either way.)

## What the web page can and cannot do
Can: switch each account's aggressiveness, turn each account's *new trades* on/off, sell positions,
reset the simulation, add coins to investigate, block coins, ask for an immediate scan.

Cannot (by design): create the real-money account, raise the hard limits in `profiles.py`, change the
capital limit, touch API keys or withdrawals. The real-money account only exists when you start the
bot with `--live --i-understand-live-risk` on your PC, and it then starts with trading OFF.

## Troubleshooting
* *"Cannot read data ... permission-denied"*: the UID in `firestore.rules` is wrong or the rules are not published.
* *Page says the bot is offline*: the bot is not running, the PC is asleep, or `serviceAccount.json` is missing.
* *Buttons say "read-only"*: `FIREBASE_OWNER_UID` is missing in `.env`.
* *Command shows "rejected: command expired"*: the bot was offline for more than 10 minutes; send it again.
* *Sign-in fails on a hosted page*: Authentication > Settings > Authorized domains must list your domain.

This was developed offline: the page and the command logic are tested against a built-in demo and a
simulated cloud, but the very first connection to your real Firebase project is yours to verify.
