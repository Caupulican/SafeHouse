"""Password, sessions and brute-force throttling for the panel.

A per-launch token in the URL was enough when this was a loopback server the parent
opened themselves and closed again. It is not enough for a service that listens on the
house LAN and gets bookmarked on a phone: a bookmark is a token that never expires, and
it survives being read over a shoulder.

So: one password, stored **only** as a hash with a random salt, and a session cookie.

Everything here is pure enough to test - the clock is injected, the delays are returned
rather than slept, and nothing touches a socket. `hashlib.scrypt` and `secrets` are
standard library, which is the whole constraint: this runs on a vendored embeddable
Python with nothing installed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

AUTH_FILE = "panel-auth.json"

# scrypt at these parameters costs ~16 MB and a few tens of milliseconds per attempt,
# which is invisible to a parent logging in once and expensive for anything trying
# passwords in bulk. They are stored in the record, so raising them later does not
# invalidate an existing password.
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32

# Fallback if a build of Python has no scrypt (it needs OpenSSL). pbkdf2 is always there.
PBKDF2_ROUNDS = 600_000

# The floor, not a recommendation. It is 4 rather than 8 because the parent asked for a
# short PIN they can type one-handed on a phone, and a rule that stops them setting the
# password they want just moves the password onto a sticky note. What the floor does buy
# is that the throttle below has something to do: at six failures the source is locked out
# for fifteen minutes, so even a 4-digit PIN is not brute-forceable over the LAN in an
# afternoon. `password_advice` says out loud when a password is weak instead of refusing
# it - the honest split between "this is a bad idea" and "you may not".
MIN_PASSWORD_LEN = 4

SESSION_COOKIE = "safehouse_panel"
# Sliding: touched on every authenticated request. A phone left on the kitchen table
# stops being a way in some hours after it was last used.
SESSION_IDLE_SEC = 4 * 3600
# Absolute, whatever the activity. Re-entering the password once a day is not a burden.
SESSION_MAX_SEC = 24 * 3600

# Brute-force policy. The delay is deliberately not a straight lockout at first: a parent
# fat-fingering their password on a phone keyboard must not lock themselves out of the
# controls, while an unattended script gets slower every attempt.
THROTTLE_WINDOW_SEC = 900
THROTTLE_LOCKOUT_AFTER = 6
THROTTLE_LOCKOUT_SEC = 900
THROTTLE_MAX_DELAY_SEC = 8


# --- password ----------------------------------------------------------------

def hash_password(password: str, *, salt: bytes | None = None) -> dict:
    """Hash `password` into a self-describing record. Never returns the plaintext."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    try:
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                                r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN)
        return {"algo": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                "salt": salt.hex(), "hash": digest.hex()}
    except (ValueError, AttributeError, MemoryError):
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                     PBKDF2_ROUNDS, dklen=DKLEN)
        return {"algo": "pbkdf2_sha256", "rounds": PBKDF2_ROUNDS,
                "salt": salt.hex(), "hash": digest.hex()}


def verify_password(record: dict | None, password: str) -> bool:
    """Constant-time check of `password` against a record from `hash_password`.

    A malformed or missing record is False, never True: a corrupt auth file must lock
    the panel, not open it.
    """
    if not isinstance(record, dict):
        return False
    try:
        salt = bytes.fromhex(str(record["salt"]))
        expected = bytes.fromhex(str(record["hash"]))
        algo = str(record.get("algo", "scrypt"))
        if algo == "scrypt":
            got = hashlib.scrypt(
                password.encode("utf-8"), salt=salt,
                n=int(record.get("n", SCRYPT_N)), r=int(record.get("r", SCRYPT_R)),
                p=int(record.get("p", SCRYPT_P)), dklen=len(expected))
        elif algo == "pbkdf2_sha256":
            got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                      int(record.get("rounds", PBKDF2_ROUNDS)),
                                      dklen=len(expected))
        else:
            return False
    except (KeyError, ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(got, expected)


def generate_password(words: int = 4) -> str:
    """A password a parent can retype on a phone: five groups of four characters.

    Deliberately not a word list - shipping one would be a dictionary an attacker gets
    for free, and this is only ever typed a handful of times before it is saved in a
    password manager or written down somewhere the child does not go.
    """
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no l/i/o/0/1: they are misread
    return "-".join(
        "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(max(2, words))
    )


def password_problem(password: str) -> dict | None:
    """A reason to REFUSE this password outright, or None. Deliberately almost nothing.

    Returned as a message (`i18n.msg`) so the page can say it in the parent's language;
    `i18n.render` turns it back into English for the console.
    """
    from .i18n import msg

    if not isinstance(password, str) or not password.strip():
        return msg("pw.blank")
    if len(password) < MIN_PASSWORD_LEN:
        return msg("pw.short", n=MIN_PASSWORD_LEN)
    return None


def password_advice(password: str) -> dict | str:
    """A warning to SHOW alongside a password that was accepted, or "".

    This exists because the panel can switch off every protection in the house, the child
    has local admin on the machine it runs on, and there is no TLS on the login. A parent
    is entitled to choose a weak password anyway - it is their house - but they are also
    entitled to be told once, plainly, rather than discovering the reasoning later.
    """
    from .i18n import msg

    if password.isdigit():
        return msg("pw.advice.digits")
    if len(password) < 8:
        return msg("pw.advice.short")
    return ""


class PasswordStore:
    """`panel-auth.json`, the only place the password exists - and only as a hash."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / AUTH_FILE

    def load(self) -> dict | None:
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        return obj if isinstance(obj, dict) and obj.get("hash") else None

    @property
    def configured(self) -> bool:
        return self.load() is not None

    def save(self, password: str) -> None:
        """Write the hash atomically, then tighten the file's own ACL on Windows.

        The directory ACL already keeps standard users out, but this file is the one
        whose leak matters most, so it does not rely on inheritance alone.
        """
        from safehouse_ytbudget.atomicio import write_json

        record = hash_password(password)
        record["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_json(self.path, record)
        _restrict_to_admins(self.path)

    def verify(self, password: str) -> bool:
        return verify_password(self.load(), password)


def _restrict_to_admins(path: Path) -> None:
    """SYSTEM + Administrators only. Best effort: no icacls means no change, not a crash."""
    if os.name != "nt":
        return
    import subprocess

    for args in (
        ["icacls", str(path), "/inheritance:r"],
        ["icacls", str(path), "/grant:r", "*S-1-5-18:F"],
        ["icacls", str(path), "/grant:r", "*S-1-5-32-544:F"],
    ):
        try:
            subprocess.run(args, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return


# --- sessions ----------------------------------------------------------------

@dataclass
class _Session:
    created: float
    touched: float
    who: str


class Sessions:
    """In-memory session table. Restarting the panel logs everyone out, by design.

    Persisting sessions would mean a stolen cookie survives a reboot, and the only cost
    of not persisting is one login after the panel restarts.
    """

    def __init__(self, *, idle_sec: int = SESSION_IDLE_SEC,
                 max_sec: int = SESSION_MAX_SEC, clock=time.time) -> None:
        self._sessions: dict[str, _Session] = {}
        self.idle_sec = idle_sec
        self.max_sec = max_sec
        self._clock = clock

    def create(self, who: str = "") -> str:
        now = self._clock()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = _Session(created=now, touched=now, who=who)
        self._sweep(now)
        return token

    def validate(self, token: str | None) -> bool:
        """True if the token is a live session; touches it (sliding idle window)."""
        if not token:
            return False
        now = self._clock()
        self._sweep(now)
        session = self._sessions.get(token)
        if session is None:
            return False
        session.touched = now
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def revoke_all(self) -> int:
        """Drop every session. Used when the password changes.

        Changing a password because somebody saw it is worth nothing if the phone they
        saw it on stays signed in for another day. So every session goes, including the
        one doing the changing - `app.py` immediately issues that browser a fresh one, so
        the parent is not logged out of the page they are standing in.
        """
        count = len(self._sessions)
        self._sessions.clear()
        return count

    def count(self) -> int:
        self._sweep(self._clock())
        return len(self._sessions)

    def _sweep(self, now: float) -> None:
        for token, session in list(self._sessions.items()):
            if now - session.touched > self.idle_sec or now - session.created > self.max_sec:
                del self._sessions[token]


# --- brute force -------------------------------------------------------------

@dataclass
class Throttle:
    """Per-source-address failure tracking.

    Returns delays rather than sleeping, so the tests do not have to wait and the caller
    decides where the sleep happens.
    """

    window_sec: int = THROTTLE_WINDOW_SEC
    lockout_after: int = THROTTLE_LOCKOUT_AFTER
    lockout_sec: int = THROTTLE_LOCKOUT_SEC
    max_delay_sec: int = THROTTLE_MAX_DELAY_SEC
    _failures: dict[str, list[float]] = field(default_factory=dict)

    def _recent(self, source: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(source, []) if now - t <= self.window_sec]
        if recent:
            self._failures[source] = recent
        else:
            self._failures.pop(source, None)
        return recent

    def locked_for(self, source: str, now: float) -> int:
        """Seconds this source must wait before another attempt is even considered."""
        recent = self._recent(source, now)
        if len(recent) < self.lockout_after:
            return 0
        return max(0, int(recent[-1] + self.lockout_sec - now))

    def record_failure(self, source: str, now: float) -> int:
        """Note a failed attempt; returns how long to stall this response.

        The stall doubles per failure up to `max_delay_sec`. It is applied to the failing
        response, so a script gets slower and a parent's one typo costs half a second.
        """
        recent = self._recent(source, now)
        recent.append(now)
        self._failures[source] = recent
        return min(2 ** (len(recent) - 1), self.max_delay_sec)

    def record_success(self, source: str) -> None:
        self._failures.pop(source, None)
