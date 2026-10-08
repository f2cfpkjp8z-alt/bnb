"""Validation of commands that arrive from the web app.

The web app can only ask for things on this whitelist, with validated arguments. Nothing a
command carries is ever executed as code, and nothing can raise the hard limits in
profiles.py or change API keys, capital limits or withdrawal settings.
"""
from __future__ import annotations

import re

from profiles import PROFILES

ACCOUNTS = ("sim", "live")
_BASE_RE = re.compile(r"^[A-Z0-9]{2,15}$")


def normalize_symbol(raw, quote: str = "USDT") -> str | None:
    """'sol', 'SOLUSDT', 'sol/usdt' -> 'SOL/USDT'. None if it does not look like a coin."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().upper().replace(" ", "")
    if "/" in s:
        base, q = s.split("/", 1)
        if q != quote:
            return None
    elif s.endswith(quote) and len(s) > len(quote):
        base = s[: -len(quote)]
    else:
        base = s
    return f"{base}/{quote}" if _BASE_RE.match(base) else None


def _account(a):
    return a if a in ACCOUNTS else None


# cmd -> {arg: validator(value) -> cleaned value or None}
SPEC = {
    "set_profile": {"account": _account, "profile": lambda v: v if v in PROFILES else None},
    "set_enabled": {"account": _account, "enabled": lambda v: v if isinstance(v, bool) else None},
    "add_watch": {"symbol": normalize_symbol},
    "remove_watch": {"symbol": normalize_symbol},
    "block": {"symbol": normalize_symbol},
    "unblock": {"symbol": normalize_symbol},
    "close_position": {"account": _account, "symbol": normalize_symbol},
    "close_all": {"account": _account},
    "reset_sim": {},
    "scan_now": {},
}


def validate(raw: dict, owner_uid: str | None, now: float, max_age: float):
    """-> (ok, reason, clean). `raw` is {id, cmd, args, uid, ts}."""
    if not owner_uid:
        return False, "FIREBASE_OWNER_UID is not set on the PC; commands are disabled", None
    if raw.get("uid") != owner_uid:
        return False, "command was not sent by the owner account", None
    ts = raw.get("ts")
    if not isinstance(ts, (int, float)):
        return False, "missing timestamp", None
    if now - ts > max_age:
        return False, f"command expired ({int(now - ts)}s old)", None
    if ts - now > 120:
        return False, "command timestamp is in the future", None
    name, args = raw.get("cmd"), raw.get("args") or {}
    if name not in SPEC or not isinstance(args, dict):
        return False, f"unknown command {name!r}", None
    if set(args) - set(SPEC[name]):
        return False, f"unexpected arguments: {sorted(set(args) - set(SPEC[name]))}", None
    clean = {}
    for k, fn in SPEC[name].items():
        v = fn(args.get(k))
        if v is None:
            return False, f"invalid or missing argument '{k}'", None
        clean[k] = v
    return True, "", {"id": raw.get("id"), "cmd": name, "args": clean}
