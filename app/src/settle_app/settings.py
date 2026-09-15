"""Everything the app reads from its environment, in one object.

The engine keeps its policy in :class:`~settle.domain.config.MatchConfig` so
that "why did it do that?" has one answer. This is the same idea for the
deployment: no module reaches for ``os.environ`` on its own, so the answer to
"what is this instance actually configured to do?" is one dataclass.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

#: A shared password protecting other people's bank statements should not be
#: guessable in an afternoon. This is a floor, not a recommendation.
MIN_PASSWORD_LENGTH: Final = 12

#: Addresses that only the machine itself can reach. Binding anywhere else
#: without a password is refused at startup.
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost", ""})

DEFAULT_MAX_UPLOAD_BYTES: Final = 8 * 1024 * 1024
DEFAULT_MAX_ROWS: Final = 50_000
DEFAULT_MAX_COLUMNS: Final = 128
DEFAULT_SESSION_TTL_SECONDS: Final = 12 * 60 * 60


class WebConfigError(Exception):
    """The instance is configured in a way that is unsafe or cannot work.

    Raised before the socket binds, so a misconfigured deployment is a
    container that exits rather than one that serves.
    """


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Resolved configuration for one instance."""

    data_dir: Path
    #: ``None`` means no password is set, which is only allowed on loopback.
    password: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000

    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_rows: int = DEFAULT_MAX_ROWS
    max_columns: int = DEFAULT_MAX_COLUMNS
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS

    #: Delete runs older than this. ``None`` keeps them forever, which is a
    #: choice the operator should make deliberately — see docs/DEPLOY.md.
    retention_days: int | None = None

    #: Trust ``X-Forwarded-For`` for the client IP. Only true behind a proxy
    #: you control; otherwise a client can forge its own rate-limit bucket.
    behind_proxy: bool = False
    #: Mark the session cookie ``Secure``. Separate from ``behind_proxy``
    #: because a proxy can terminate TLS or not.
    https_only: bool = False

    #: The column mapper's model tier. Off by default: the app is complete
    #: without it, and enabling it sends data to a third party.
    model_mapper: bool = False
    #: Send a few sample values along with the column headers. Headers are
    #: rarely personal data; sample values are customer names and payment
    #: references, so this is opt-in on top of opt-in.
    model_samples: bool = False

    #: Populated by :meth:`from_env` when the password came from a file, so
    #: the error message can name it.
    password_source: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if self.password is not None and len(self.password) < MIN_PASSWORD_LENGTH:
            where = self.password_source or "SETTLE_WEB_PASSWORD"
            raise WebConfigError(
                f"the password in {where} is {len(self.password)} characters; "
                f"at least {MIN_PASSWORD_LENGTH} are required"
            )
        if self.password is None and not self.is_loopback:
            raise WebConfigError(
                f"refusing to bind {self.host} without a password. This instance "
                "would serve other people's invoices and bank statements to "
                "anyone who found it. Set SETTLE_WEB_PASSWORD (or "
                "SETTLE_WEB_PASSWORD_FILE), or bind 127.0.0.1 and reach it over "
                "an SSH tunnel."
            )
        if self.model_samples and not self.model_mapper:
            raise WebConfigError(
                "SETTLE_WEB_MODEL_SAMPLES is set but SETTLE_WEB_MODEL_MAPPER is "
                "not; sample values would never be sent anywhere"
            )
        for name in ("max_upload_bytes", "max_rows", "max_columns", "session_ttl_seconds"):
            if getattr(self, name) < 1:
                raise WebConfigError(f"{name} must be at least 1")
        if self.retention_days is not None and self.retention_days < 1:
            raise WebConfigError("retention_days must be at least 1, or unset to keep forever")

    @property
    def is_loopback(self) -> bool:
        """True when only this machine can reach the bound address."""
        return self.host in LOOPBACK_HOSTS

    @property
    def database_path(self) -> Path:
        return self.data_dir / "settle.db"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def secret_path(self) -> Path:
        """Per-instance key material, generated once on first start."""
        return self.data_dir / "secret.key"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WebSettings:
        """Build settings from the environment, failing loudly on nonsense."""
        source = os.environ if env is None else env
        password, where = _read_password(source)
        return cls(
            data_dir=Path(source.get("SETTLE_WEB_DATA_DIR", "data")),
            password=password,
            password_source=where,
            host=source.get("SETTLE_WEB_HOST", "127.0.0.1"),
            port=_positive_int(source, "SETTLE_WEB_PORT", 8000),
            max_upload_bytes=_positive_int(
                source, "SETTLE_WEB_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES
            ),
            max_rows=_positive_int(source, "SETTLE_WEB_MAX_ROWS", DEFAULT_MAX_ROWS),
            max_columns=_positive_int(source, "SETTLE_WEB_MAX_COLUMNS", DEFAULT_MAX_COLUMNS),
            session_ttl_seconds=_positive_int(
                source, "SETTLE_WEB_SESSION_TTL", DEFAULT_SESSION_TTL_SECONDS
            ),
            retention_days=_optional_int(source, "SETTLE_WEB_RETENTION_DAYS"),
            behind_proxy=_flag(source, "SETTLE_WEB_BEHIND_PROXY"),
            https_only=_flag(source, "SETTLE_WEB_HTTPS"),
            model_mapper=_flag(source, "SETTLE_WEB_MODEL_MAPPER"),
            model_samples=_flag(source, "SETTLE_WEB_MODEL_SAMPLES"),
        )


def _read_password(env: Mapping[str, str]) -> tuple[str | None, str]:
    """Prefer the file, so the secret never appears in ``docker inspect``."""
    path = env.get("SETTLE_WEB_PASSWORD_FILE", "").strip()
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise WebConfigError(f"SETTLE_WEB_PASSWORD_FILE: cannot read {path}: {exc}") from exc
        return text.strip(), f"SETTLE_WEB_PASSWORD_FILE ({path})"
    value = env.get("SETTLE_WEB_PASSWORD", "")
    return (value, "SETTLE_WEB_PASSWORD") if value else (None, "")


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise WebConfigError(f"{name}: expected a whole number, got {raw!r}") from exc


def _optional_int(env: Mapping[str, str], name: str) -> int | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise WebConfigError(f"{name}: expected a whole number, got {raw!r}") from exc
