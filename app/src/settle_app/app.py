"""The HTTP surface. Thin, like the CLI and the API it sits beside.

There is no matching logic in this file and there must never be any. The engine
answers the question; everything here is about getting files in, getting answers
out, and not letting the wrong person do either.

Routes are registered in small groups rather than one long factory, because the
groups are where the next feature goes and a hundred-line factory is where it
would go badly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from settle.domain.match import ENGINE_VERSION
from settle_app import APP_VERSION
from settle_app.context import AppContext
from settle_app.maintenance import sweep
from settle_app.security import csrf_token, issue_session, verify_password, verify_session
from settle_app.settings import WebSettings
from settle_app.store import SORTABLE, STATUSES

PACKAGE_DIR: Final = Path(__file__).parent
TEMPLATES_DIR: Final = PACKAGE_DIR / "templates"
STATIC_DIR: Final = PACKAGE_DIR / "static"

SESSION_COOKIE: Final = "settle_session"
CSRF_FIELD: Final = "csrf_token"

#: No scripts, inline or otherwise. "No JavaScript" is a design claim, and this
#: is the form of it a browser will actually enforce.
CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class Web:
    """Per-instance helpers the route groups share.

    Everything a handler needs that is not the request itself: who is signed
    in, what their CSRF token is, and how to render. Keeping it here is what
    lets the handlers stay short enough to read.
    """

    def __init__(self, context: AppContext) -> None:
        self.ctx = context

    # ---- identity ------------------------------------------------------

    def client_ip(self, request: Request) -> str:
        """Only trust a forwarded header when a proxy we control sets it.

        Otherwise a client picks its own rate-limit bucket by sending a header,
        and the throttle stops throttling.
        """
        if self.ctx.settings.behind_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def is_signed_in(self, request: Request) -> bool:
        key = self.ctx.session_signing_key
        if key is None:
            return True
        token = request.cookies.get(SESSION_COOKIE, "")
        return bool(token) and verify_session(token, key=key)

    def password_ok(self, candidate: str) -> bool:
        expected = self.ctx.password_digest
        if expected is None:
            return True
        return verify_password(candidate, expected=expected, secret=self.ctx.secret)

    def csrf_for(self, request: Request) -> str:
        key = self.ctx.session_signing_key
        token = request.cookies.get(SESSION_COOKIE, "")
        return csrf_token(token, key=key) if token and key else ""

    def csrf_ok(self, request: Request, submitted: str) -> bool:
        """An unauthenticated instance has no session to bind a token to."""
        if not self.ctx.auth_required:
            return True
        expected = self.csrf_for(request)
        return bool(expected) and submitted == expected

    # ---- rendering -----------------------------------------------------

    def render(
        self,
        request: Request,
        template: str,
        values: dict[str, object] | None = None,
        *,
        status_code: int = 200,
    ) -> Response:
        return _templates.TemplateResponse(
            request=request,
            name=template,
            context={
                **(values or {}),
                "csrf_token": self.csrf_for(request),
                "auth_required": self.ctx.auth_required,
                "app_version": APP_VERSION,
                "engine_version": ENGINE_VERSION,
            },
            status_code=status_code,
        )


def create_app(settings: WebSettings | None = None) -> FastAPI:
    """Build an application. One call, one instance, no import-time work."""
    context = AppContext.build(settings or WebSettings.from_env())
    web = Web(context)

    app = FastAPI(
        title="settle",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ctx = context
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Expired runs and abandoned uploads go at startup. A deployment that is
    # restarted for an update should not need a cron job to stop holding data
    # it was told to forget.
    sweep(store=context.store, artifacts=context.artifacts, settings=context.settings)

    _register_middleware(app, web)
    _register_health(app)
    _register_auth(app, web)
    _register_runs(app, web)

    # Imported here rather than at module scope: runs_routes needs Web, which
    # is defined above, and a module-level import would be a cycle.
    from settle_app import runs_routes

    runs_routes.register(app, web)
    return app


def _register_middleware(app: FastAPI, web: Web) -> None:
    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if web.ctx.settings.https_only:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


def _register_health(app: FastAPI) -> None:
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Liveness for the container. Deliberately unauthenticated."""
        return {"status": "ok", "engine": ENGINE_VERSION, "app": APP_VERSION}


def _register_auth(app: FastAPI, web: Web) -> None:
    @app.get("/", include_in_schema=False)
    def index(request: Request) -> Response:
        return redirect("/runs" if web.is_signed_in(request) else "/login")

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_form(request: Request) -> Response:
        if not web.ctx.auth_required or web.is_signed_in(request):
            return redirect("/runs")
        return web.render(request, "login.html")

    @app.post("/login", include_in_schema=False)
    def login(request: Request, password: str = Form(default="")) -> Response:
        if not web.ctx.auth_required:
            return redirect("/runs")

        client = web.client_ip(request)
        # Checked before the hash is computed. An unthrottled login form is a
        # free scrypt oracle, which is a denial-of-service tool as much as a
        # guessing one, so the throttle applies to correct passwords too.
        if not web.ctx.limiter.allows(client):
            return _throttled(web, request, client)

        if not web.password_ok(password):
            web.ctx.limiter.record_failure(client)
            web.ctx.store.log_access(ip=client, action="login-failed")
            return web.render(
                request, "login.html", {"error": "That password is not right."}, status_code=401
            )

        return _start_session(web, client)

    @app.post("/logout", include_in_schema=False)
    def logout(request: Request) -> Response:
        web.ctx.store.log_access(ip=web.client_ip(request), action="logout")
        response = redirect("/login")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response


#: Runs shown per page. Small enough to scan, large enough that a month of
#: weekly reconciliation fits on one.
PAGE_SIZE: Final = 25


def _register_runs(app: FastAPI, web: Web) -> None:
    @app.get("/runs", response_class=HTMLResponse, include_in_schema=False)
    def runs(request: Request, page: int = 1, status: str = "", sort: str = "") -> Response:
        if not web.is_signed_in(request):
            return redirect("/login")

        # Both are validated by the store against fixed sets rather than
        # interpolated, so a crafted query string cannot reach the SQL.
        wanted = status if status in STATUSES else None
        column = sort if sort in SORTABLE else "created_at"
        total = web.ctx.store.count(status=wanted)
        pages = max(1, -(-total // PAGE_SIZE))
        current = min(max(1, page), pages)

        return web.render(
            request,
            "runs.html",
            {
                "runs": web.ctx.store.list_runs(
                    limit=PAGE_SIZE,
                    offset=(current - 1) * PAGE_SIZE,
                    status=wanted,
                    sort=column,
                ),
                "total": total,
                "page": current,
                "pages": pages,
                "status": wanted or "",
                "sort": column,
                "all_statuses": sorted(STATUSES),
            },
        )


def _throttled(web: Web, request: Request, client: str) -> Response:
    retry = web.ctx.limiter.retry_after(client)
    response = web.render(
        request,
        "login.html",
        {"error": f"Too many attempts. Try again in {retry} seconds."},
        status_code=429,
    )
    response.headers["Retry-After"] = str(retry)
    return response


def _start_session(web: Web, client: str) -> Response:
    key = web.ctx.session_signing_key
    if key is None:  # pragma: no cover - unreachable when auth_required
        return redirect("/runs")

    web.ctx.limiter.reset(client)
    web.ctx.store.log_access(ip=client, action="login")
    ttl = web.ctx.settings.session_ttl_seconds
    response = redirect("/runs")
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(key=key, ttl_seconds=ttl),
        max_age=ttl,
        httponly=True,
        samesite="strict",
        secure=web.ctx.settings.https_only,
        path="/",
    )
    return response


def redirect(target: str) -> RedirectResponse:
    """303, so a POST that succeeded is not re-submitted by a refresh."""
    return RedirectResponse(target, status_code=303)


def app_from_env() -> FastAPI:  # pragma: no cover - uvicorn factory entry point
    return create_app(WebSettings.from_env())
