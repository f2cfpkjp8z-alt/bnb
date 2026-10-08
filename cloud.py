"""Cloud sync with Firebase (Firestore). Optional: without it the bot runs fine locally.

Collections:
  bot/status      one document, overwritten every ~30 s (balances, positions, scanner board...)
  events          the bot's live log (expires after 7 days if you enable a TTL policy on `expireAt`)
  trades          closed trades
  bot/equity      one document with the balance curve of each account
  commands        written by the web app, executed (after validation) by the bot

All network work happens in a background thread, so a slow or broken connection can never
delay trading. Errors are logged and swallowed.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import os
import queue
import threading
import time

log = logging.getLogger("bot.cloud")


def clean(x):
    """Make a payload Firestore-safe: plain python types, no NaN/inf, no numpy."""
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [clean(v) for v in x]
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):
        try:
            x = x.item()
        except Exception:
            return str(x)
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if x is None or isinstance(x, (bool, int, str)):
        return x
    return str(x)


class NullCloud:
    enabled = False
    owner_uid = None

    def push_status(self, status: dict) -> None: ...
    def push_event(self, ev: dict) -> None: ...
    def push_trade(self, tr: dict) -> None: ...
    def push_equity(self, curves: dict) -> None: ...
    def fetch_commands(self) -> list[dict]: return []
    def ack(self, cmd_id, status: str, result: str = "") -> None: ...
    def close(self) -> None: ...


class MemoryCloud(NullCloud):
    """In-memory stand-in used by the tests."""
    enabled = True

    def __init__(self, owner_uid: str = "owner"):
        self.owner_uid = owner_uid
        self.status, self.events, self.trades, self.equity, self.acks = None, [], [], [], []
        self.pending: list[dict] = []

    def push_status(self, status): self.status = clean(status)
    def push_event(self, ev): self.events.append(clean(ev))
    def push_trade(self, tr): self.trades.append(clean(tr))
    def push_equity(self, curves): self.equity.append(clean(curves))

    def fetch_commands(self):
        out, self.pending = self.pending, []
        return out

    def ack(self, cmd_id, status, result=""):
        self.acks.append((cmd_id, status, result))

    def send(self, cmd: str, args: dict | None = None, uid: str | None = None,
             ts: float | None = None, cid: str | None = None) -> None:
        """Test helper: what the web app would write."""
        self.pending.append({"id": cid or f"c{len(self.acks) + len(self.pending)}", "cmd": cmd,
                             "args": args or {}, "uid": uid or self.owner_uid,
                             "ts": ts if ts is not None else time.time()})


class FirebaseCloud(NullCloud):
    enabled = True

    def __init__(self, service_account_path: str, owner_uid: str | None):
        import firebase_admin                       # lazy: only needed when cloud is on
        from firebase_admin import credentials, firestore
        if not os.path.exists(service_account_path):
            raise FileNotFoundError(f"Firebase service account file not found: {service_account_path}")
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(service_account_path))
        self.fs = firestore.client()
        self.owner_uid = owner_uid
        self._q: queue.Queue = queue.Queue(maxsize=2000)
        self._cmds: queue.Queue = queue.Queue()
        self._stop = False
        self._worker = threading.Thread(target=self._run, daemon=True, name="cloud-writer")
        self._worker.start()
        self._watch = self.fs.collection("commands").where("status", "==", "pending") \
            .on_snapshot(self._on_cmds)
        log.info("Firebase connected (project %s)", self.fs.project)

    # ---- reading commands (realtime listener, no polling cost)
    def _on_cmds(self, docs, changes, read_time):
        for ch in changes:
            if ch.type.name in ("ADDED", "MODIFIED"):
                d = ch.document.to_dict() or {}
                created = d.get("createdAt")
                ts = created.timestamp() if hasattr(created, "timestamp") else None
                self._cmds.put({"id": ch.document.id, "cmd": d.get("cmd"), "args": d.get("args"),
                                "uid": d.get("uid"), "ts": ts})

    def fetch_commands(self):
        out = []
        while True:
            try:
                out.append(self._cmds.get_nowait())
            except queue.Empty:
                return out

    # ---- writing (background thread)
    def _put(self, op):
        try:
            self._q.put_nowait(op)
        except queue.Full:
            try:
                self._q.get_nowait()          # drop the oldest; the log is not critical
                self._q.put_nowait(op)
            except Exception:
                pass

    def _run(self):
        while not self._stop:
            try:
                kind, a, b = self._q.get(timeout=1)
            except queue.Empty:
                continue
            for attempt in range(3):
                try:
                    if kind == "set":
                        self.fs.document(a).set(b)
                    elif kind == "add":
                        self.fs.collection(a).add(b)
                    elif kind == "update":
                        self.fs.document(a).update(b)
                    break
                except Exception as e:
                    log.warning("firestore %s %s failed (try %d): %s", kind, a, attempt + 1, e)
                    time.sleep(2 * (attempt + 1))

    @staticmethod
    def _expiry(days: int = 7):
        return dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)

    def push_status(self, status):
        self._put(("set", "bot/status", clean(status)))

    def push_event(self, ev):
        d = clean(ev)
        d["expireAt"] = self._expiry(7)
        self._put(("add", "events", d))

    def push_trade(self, tr):
        self._put(("add", "trades", clean(tr)))

    def push_equity(self, curves):
        # one document {curves: {sim: {t: [...], v: [...]}, live: {...}}}: the web app reads it
        # with a single read instead of thousands. (Firestore forbids arrays of arrays, so
        # times and values are parallel lists.)
        self._put(("set", "bot/equity", {"curves": clean(curves), "updated": time.time()}))

    def ack(self, cmd_id, status, result=""):
        self._put(("update", f"commands/{cmd_id}", {
            "status": status, "result": str(result)[:300], "doneAt": time.time()}))

    def close(self):
        try:
            self._watch.unsubscribe()
        except Exception:
            pass
        deadline = time.time() + 5
        while not self._q.empty() and time.time() < deadline:
            time.sleep(0.2)
        self._stop = True


def build_cloud(disabled: bool = False):
    """Firebase if configured, otherwise a no-op. Never raises."""
    if disabled:
        return NullCloud()
    path = os.getenv("FIREBASE_SERVICE_ACCOUNT", "serviceAccount.json")
    if not os.path.exists(path):
        log.info("no Firebase service account file (%s): running without the web app", path)
        return NullCloud()
    try:
        return FirebaseCloud(path, os.getenv("FIREBASE_OWNER_UID"))
    except Exception as e:
        log.error("Firebase could not start (%s). Running WITHOUT the web app.", e)
        return NullCloud()
