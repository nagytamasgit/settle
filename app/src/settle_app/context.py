"""What every request needs, assembled once at startup.

FastAPI's dependency system can build these per request; there is no reason to.
The store, the artefact root and the derived keys are all immutable for the life
of the process, and building them once means the expensive part — scrypt over
the configured password — happens at startup rather than on every login attempt.
"""

from __future__ import annotations

from dataclasses import dataclass

from settle_app.artifacts import ArtifactStore
from settle_app.security import (
    RateLimiter,
    load_or_create_secret,
    password_hash,
    session_key,
)
from settle_app.settings import WebSettings
from settle_app.store import RunStore


@dataclass(frozen=True, slots=True)
class AppContext:
    """Process-wide state, reachable from a request via ``app.state.ctx``."""

    settings: WebSettings
    store: RunStore
    artifacts: ArtifactStore
    limiter: RateLimiter
    #: Per-instance key material, read once. Kept here rather than re-read per
    #: request so a login attempt is one scrypt, not a scrypt plus a disk read.
    secret: bytes
    #: ``None`` when no password is configured, which only loopback allows.
    password_digest: bytes | None
    session_signing_key: bytes | None

    @property
    def auth_required(self) -> bool:
        return self.password_digest is not None

    @classmethod
    def build(cls, settings: WebSettings) -> AppContext:
        """Create the data directory, migrate, and derive the keys."""
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        secret = load_or_create_secret(settings.secret_path)

        store = RunStore(settings.database_path)
        # Before the socket binds, so a failed migration is a process that
        # exits rather than one serving a schema it cannot read.
        store.migrate()

        digest = (
            None if settings.password is None else password_hash(settings.password, secret=secret)
        )
        signing = None if digest is None else session_key(password_digest=digest, secret=secret)

        return cls(
            settings=settings,
            store=store,
            artifacts=ArtifactStore(settings.runs_dir),
            limiter=RateLimiter(),
            secret=secret,
            password_digest=digest,
            session_signing_key=signing,
        )
