"""Passwords, sessions and CSRF, using nothing but the standard library.

Deliberately pure: every function here takes its inputs and returns a value,
including the clock. That is what makes the failure paths — an expired token, a
tampered signature, a rotated password — cheap to test, and they are most of
what this module is.

One design note worth stating, because it is the part that is easy to get
wrong. The session signing key is *derived from the password*. Sessions
therefore survive a restart, which they must, but rotating the password
invalidates every outstanding session, which it must. With one shared password
and no user accounts, rotation is the only revocation mechanism there is; if it
did not actually revoke, there would be none.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Final

SCRYPT_N: Final = 2**14
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
KEY_BYTES: Final = 32
SECRET_BYTES: Final = 32

_SESSION_CONTEXT: Final = b"settle-app/session-v1"
_CSRF_CONTEXT: Final = b"settle-app/csrf-v1"


def load_or_create_secret(path: Path) -> bytes:
    """Per-instance key material, generated once and then reused.

    Without a stable secret every restart would log everyone out and invalidate
    every CSRF token. It is created ``0600`` and never leaves the data volume.
    """
    if path.exists():
        secret = path.read_bytes()
        if len(secret) < SECRET_BYTES:
            raise ValueError(f"{path} is too short to be the instance secret; refusing to use it")
        return secret
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(SECRET_BYTES)
    path.write_bytes(secret)
    path.chmod(0o600)
    return secret


def password_hash(password: str, *, secret: bytes) -> bytes:
    """scrypt, salted with the instance secret so the result is stable here."""
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=secret,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
    )


def verify_password(candidate: str, *, expected: bytes, secret: bytes) -> bool:
    """Constant-time comparison, so a wrong guess leaks nothing by timing."""
    return hmac.compare_digest(password_hash(candidate, secret=secret), expected)


def session_key(*, password_digest: bytes, secret: bytes) -> bytes:
    """The signing key for session cookies, bound to the current password."""
    return hmac.new(secret, _SESSION_CONTEXT + password_digest, hashlib.sha256).digest()


def issue_session(*, key: bytes, ttl_seconds: int, now: float | None = None) -> str:
    """A signed, self-describing token. No server-side session table."""
    expires_at = int((time.time() if now is None else now) + ttl_seconds)
    payload = str(expires_at).encode("ascii")
    return f"{_b64(payload)}.{_b64(_sign(key, payload))}"


def verify_session(token: str, *, key: bytes, now: float | None = None) -> bool:
    """True only for a well-formed, correctly signed, unexpired token."""
    payload = _verified_payload(token, key=key)
    if payload is None:
        return False
    try:
        expires_at = int(payload)
    except ValueError:
        return False
    return (time.time() if now is None else now) < expires_at


def csrf_token(session_token: str, *, key: bytes) -> str:
    """Bound to the session, so it cannot be replayed into someone else's."""
    material = _CSRF_CONTEXT + session_token.encode("ascii")
    return _b64(hmac.new(key, material, hashlib.sha256).digest())


def verify_csrf(token: str, *, session_token: str, key: bytes) -> bool:
    return hmac.compare_digest(token, csrf_token(session_token, key=key))


def _sign(key: bytes, payload: bytes) -> bytes:
    return hmac.new(key, payload, hashlib.sha256).digest()


def _verified_payload(token: str, *, key: bytes) -> bytes | None:
    encoded, _, signature = token.partition(".")
    if not encoded or not signature:
        return None
    try:
        payload = _unb64(encoded)
        provided = _unb64(signature)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(provided, _sign(key, payload)):
        return None
    return payload


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class RateLimiter:
    """Throttles login attempts, per client and in total.

    The per-client bucket is the useful one; the global ceiling is there so a
    botnet spread across many addresses still cannot turn the login form into a
    free scrypt oracle. Failures count, successes clear.

    In-memory and per-process, which is the right size for one container. It
    forgets everything on restart, so a restart is a free reset — acceptable
    when the alternative is a table to maintain, and noted here rather than
    discovered later.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: float = 300.0,
        global_max_attempts: int = 100,
    ) -> None:
        if max_attempts < 1 or global_max_attempts < 1:
            raise ValueError("attempt limits must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._max = max_attempts
        self._window = window_seconds
        self._global_max = global_max_attempts
        self._failures: defaultdict[str, deque[float]] = defaultdict(deque)
        self._global: deque[float] = deque()

    def allows(self, client: str, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        self._expire(moment)
        return len(self._failures[client]) < self._max and len(self._global) < self._global_max

    def record_failure(self, client: str, *, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        self._expire(moment)
        self._failures[client].append(moment)
        self._global.append(moment)

    def reset(self, client: str) -> None:
        """Called on a successful login; one bad guess should not linger."""
        self._failures.pop(client, None)

    def retry_after(self, client: str, *, now: float | None = None) -> int:
        """Whole seconds until the oldest relevant failure ages out."""
        moment = time.time() if now is None else now
        self._expire(moment)
        oldest = [bucket[0] for bucket in (self._failures.get(client), self._global) if bucket]
        if not oldest:
            return 0
        return max(1, int(min(oldest) + self._window - moment) + 1)

    def _expire(self, now: float) -> None:
        cutoff = now - self._window
        for client, bucket in list(self._failures.items()):
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                del self._failures[client]
        while self._global and self._global[0] <= cutoff:
            self._global.popleft()
