"""Passwords, sessions, CSRF and the login throttle.

The load-bearing test in this file is
``test_rotating_the_password_invalidates_existing_sessions``. With one shared
password and no user accounts, rotation is the only revocation there is. If it
did not actually revoke, the app would have no way to lock anyone out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from settle_app.security import (
    RateLimiter,
    csrf_token,
    issue_session,
    load_or_create_secret,
    password_hash,
    session_key,
    verify_csrf,
    verify_password,
    verify_session,
)

PASSWORD = "correct-horse-battery-staple"
OTHER = "a-completely-different-one"


@pytest.fixture
def secret(tmp_path: Path) -> bytes:
    return load_or_create_secret(tmp_path / "secret.key")


class TestTheInstanceSecret:
    def test_it_is_created_once_and_then_reused(self, tmp_path: Path) -> None:
        """A new secret on every restart would log everyone out every restart."""
        path = tmp_path / "secret.key"
        first = load_or_create_secret(path)
        assert load_or_create_secret(path) == first

    def test_it_is_not_world_readable(self, tmp_path: Path) -> None:
        path = tmp_path / "secret.key"
        load_or_create_secret(path)
        assert path.stat().st_mode & 0o077 == 0

    def test_two_instances_get_different_secrets(self, tmp_path: Path) -> None:
        a = load_or_create_secret(tmp_path / "a.key")
        b = load_or_create_secret(tmp_path / "b.key")
        assert a != b

    def test_a_truncated_secret_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "secret.key"
        path.write_bytes(b"too-short")
        with pytest.raises(ValueError, match="too short"):
            load_or_create_secret(path)


class TestPasswords:
    def test_the_right_password_verifies(self, secret: bytes) -> None:
        expected = password_hash(PASSWORD, secret=secret)
        assert verify_password(PASSWORD, expected=expected, secret=secret)

    def test_the_wrong_password_does_not(self, secret: bytes) -> None:
        expected = password_hash(PASSWORD, secret=secret)
        assert not verify_password(OTHER, expected=expected, secret=secret)

    def test_the_hash_is_stable_for_one_instance(self, secret: bytes) -> None:
        assert password_hash(PASSWORD, secret=secret) == password_hash(PASSWORD, secret=secret)

    def test_the_same_password_hashes_differently_on_another_instance(
        self, tmp_path: Path, secret: bytes
    ) -> None:
        elsewhere = load_or_create_secret(tmp_path / "other.key")
        assert password_hash(PASSWORD, secret=secret) != password_hash(PASSWORD, secret=elsewhere)


class TestSessions:
    def test_a_freshly_issued_token_verifies(self, secret: bytes) -> None:
        key = session_key(password_digest=password_hash(PASSWORD, secret=secret), secret=secret)
        token = issue_session(key=key, ttl_seconds=3600, now=1000.0)
        assert verify_session(token, key=key, now=1000.0)

    def test_a_token_expires(self, secret: bytes) -> None:
        key = session_key(password_digest=password_hash(PASSWORD, secret=secret), secret=secret)
        token = issue_session(key=key, ttl_seconds=60, now=1000.0)
        assert verify_session(token, key=key, now=1059.0)
        assert not verify_session(token, key=key, now=1061.0)

    def test_rotating_the_password_invalidates_existing_sessions(self, secret: bytes) -> None:
        """The only revocation mechanism this app has. It has to work."""
        before = session_key(password_digest=password_hash(PASSWORD, secret=secret), secret=secret)
        token = issue_session(key=before, ttl_seconds=3600, now=1000.0)
        after = session_key(password_digest=password_hash(OTHER, secret=secret), secret=secret)
        assert not verify_session(token, key=after, now=1000.0)

    @pytest.mark.parametrize(
        "token",
        ["", ".", "nodot", "a.b", "....", "!!!.???", "MTAwMA"],
    )
    def test_malformed_tokens_are_rejected_without_raising(self, secret: bytes, token: str) -> None:
        key = session_key(password_digest=password_hash(PASSWORD, secret=secret), secret=secret)
        assert not verify_session(token, key=key, now=1000.0)

    def test_a_tampered_signature_is_rejected(self, secret: bytes) -> None:
        key = session_key(password_digest=password_hash(PASSWORD, secret=secret), secret=secret)
        payload, _, signature = issue_session(key=key, ttl_seconds=3600, now=1000.0).partition(".")
        assert not verify_session(f"{payload}.{signature[:-2]}xx", key=key, now=1000.0)

    def test_extending_the_expiry_without_resigning_is_rejected(self, secret: bytes) -> None:
        """The obvious attack: keep the signature, change the deadline."""
        key = session_key(password_digest=password_hash(PASSWORD, secret=secret), secret=secret)
        _, _, signature = issue_session(key=key, ttl_seconds=60, now=1000.0).partition(".")
        forged_payload = "OTk5OTk5OTk5OQ"  # 9999999999
        assert not verify_session(f"{forged_payload}.{signature}", key=key, now=1000.0)

    def test_a_non_numeric_payload_is_rejected(self, secret: bytes) -> None:
        key = session_key(password_digest=password_hash(PASSWORD, secret=secret), secret=secret)
        from settle_app.security import _b64, _sign

        payload = b"tomorrow"
        token = f"{_b64(payload)}.{_b64(_sign(key, payload))}"
        assert not verify_session(token, key=key, now=1000.0)


class TestCsrf:
    def test_a_token_matches_its_own_session(self, secret: bytes) -> None:
        key = session_key(password_digest=password_hash(PASSWORD, secret=secret), secret=secret)
        session = issue_session(key=key, ttl_seconds=3600, now=1000.0)
        assert verify_csrf(csrf_token(session, key=key), session_token=session, key=key)

    def test_a_token_from_another_session_does_not(self, secret: bytes) -> None:
        key = session_key(password_digest=password_hash(PASSWORD, secret=secret), secret=secret)
        mine = issue_session(key=key, ttl_seconds=3600, now=1000.0)
        theirs = issue_session(key=key, ttl_seconds=3600, now=2000.0)
        assert not verify_csrf(csrf_token(theirs, key=key), session_token=mine, key=key)

    def test_an_empty_token_does_not(self, secret: bytes) -> None:
        key = session_key(password_digest=password_hash(PASSWORD, secret=secret), secret=secret)
        session = issue_session(key=key, ttl_seconds=3600, now=1000.0)
        assert not verify_csrf("", session_token=session, key=key)


class TestRateLimiter:
    def test_attempts_are_allowed_up_to_the_limit(self) -> None:
        limiter = RateLimiter(max_attempts=3, window_seconds=60)
        for moment in range(3):
            assert limiter.allows("1.2.3.4", now=float(moment))
            limiter.record_failure("1.2.3.4", now=float(moment))
        assert not limiter.allows("1.2.3.4", now=3.0)

    def test_the_window_ages_failures_out(self) -> None:
        limiter = RateLimiter(max_attempts=2, window_seconds=60)
        limiter.record_failure("1.2.3.4", now=0.0)
        limiter.record_failure("1.2.3.4", now=1.0)
        assert not limiter.allows("1.2.3.4", now=2.0)
        assert limiter.allows("1.2.3.4", now=62.0)

    def test_one_client_does_not_lock_out_another(self) -> None:
        limiter = RateLimiter(max_attempts=1, window_seconds=60)
        limiter.record_failure("1.2.3.4", now=0.0)
        assert not limiter.allows("1.2.3.4", now=0.0)
        assert limiter.allows("5.6.7.8", now=0.0)

    def test_a_global_ceiling_survives_many_addresses(self) -> None:
        """One address per attempt would otherwise bypass the per-client limit."""
        limiter = RateLimiter(max_attempts=5, window_seconds=60, global_max_attempts=10)
        for index in range(10):
            limiter.record_failure(f"10.0.0.{index}", now=float(index))
        assert not limiter.allows("10.0.0.200", now=10.0)

    def test_success_clears_the_bucket(self) -> None:
        limiter = RateLimiter(max_attempts=2, window_seconds=60)
        limiter.record_failure("1.2.3.4", now=0.0)
        limiter.record_failure("1.2.3.4", now=1.0)
        limiter.reset("1.2.3.4")
        assert limiter.allows("1.2.3.4", now=2.0)

    def test_retry_after_is_a_positive_whole_number_when_blocked(self) -> None:
        limiter = RateLimiter(max_attempts=1, window_seconds=60)
        limiter.record_failure("1.2.3.4", now=0.0)
        assert limiter.retry_after("1.2.3.4", now=10.0) >= 1

    def test_retry_after_is_zero_when_nothing_is_recorded(self) -> None:
        assert RateLimiter().retry_after("1.2.3.4", now=0.0) == 0

    @pytest.mark.parametrize(
        "kwargs",
        [{"max_attempts": 0}, {"global_max_attempts": 0}, {"window_seconds": 0}],
    )
    def test_nonsensical_limits_are_refused(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError, match="must be"):
            RateLimiter(**kwargs)
