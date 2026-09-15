"""Who can reach what.

This app holds other people's invoices and bank statements behind one shared
password, so the interesting cases are all the ways in that should not work.
Redirects are not followed, because the redirect itself is the assertion.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from typer.testing import CliRunner

from settle_app.app import SESSION_COOKIE, create_app
from settle_app.cli import app as cli
from settle_app.settings import WebSettings

PASSWORD = "correct-horse-battery-staple"
runner = CliRunner()


@pytest.fixture
def settings(tmp_path: Path) -> WebSettings:
    return WebSettings(data_dir=tmp_path, password=PASSWORD)


@pytest.fixture
def client(settings: WebSettings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), follow_redirects=False) as client:
        yield client


def _sign_in(client: TestClient, password: str = PASSWORD) -> None:
    response = client.post("/login", data={"password": password})
    assert response.status_code == 303, response.text


class TestHealth:
    def test_health_needs_no_password(self, client: TestClient) -> None:
        """The container healthcheck cannot log in."""
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_health_reports_both_versions(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert body["engine"]
        assert body["app"]


class TestSignedOut:
    def test_the_run_list_redirects_to_login(self, client: TestClient) -> None:
        response = client.get("/runs")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_the_run_list_leaks_nothing_in_the_redirect_body(self, client: TestClient) -> None:
        assert "Runs" not in client.get("/runs").text

    def test_the_root_sends_you_to_login(self, client: TestClient) -> None:
        assert client.get("/").headers["location"] == "/login"

    def test_the_login_page_renders(self, client: TestClient) -> None:
        response = client.get("/login")
        assert response.status_code == 200
        assert "Password" in response.text


class TestSigningIn:
    def test_the_right_password_gets_a_session(self, client: TestClient) -> None:
        response = client.post("/login", data={"password": PASSWORD})
        assert response.status_code == 303
        assert response.headers["location"] == "/runs"
        assert SESSION_COOKIE in response.cookies

    def test_the_cookie_is_not_readable_by_script_or_sent_cross_site(
        self, client: TestClient
    ) -> None:
        response = client.post("/login", data={"password": PASSWORD})
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie

    def test_the_cookie_is_not_marked_secure_without_tls(self, client: TestClient) -> None:
        """Marking it Secure over plain HTTP would stop it being sent at all."""
        response = client.post("/login", data={"password": PASSWORD})
        assert "secure" not in response.headers["set-cookie"].lower()

    def test_the_cookie_is_marked_secure_behind_tls(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path, password=PASSWORD, https_only=True)
        with TestClient(create_app(settings), follow_redirects=False) as client:
            response = client.post("/login", data={"password": PASSWORD})
            assert "secure" in response.headers["set-cookie"].lower()

    def test_the_wrong_password_is_rejected(self, client: TestClient) -> None:
        response = client.post("/login", data={"password": "not-the-password"})
        assert response.status_code == 401
        assert SESSION_COOKIE not in response.cookies

    def test_the_wrong_password_does_not_say_why(self, client: TestClient) -> None:
        """ "No such user" versus "wrong password" is free reconnaissance."""
        body = client.post("/login", data={"password": "not-the-password"}).text
        assert "not right" in body
        assert PASSWORD not in body

    def test_an_empty_password_is_rejected(self, client: TestClient) -> None:
        assert client.post("/login", data={"password": ""}).status_code == 401

    def test_a_session_opens_the_run_list(self, client: TestClient) -> None:
        _sign_in(client)
        response = client.get("/runs")
        assert response.status_code == 200
        assert "Runs" in response.text

    def test_a_forged_cookie_does_not(self, client: TestClient) -> None:
        client.cookies.set(SESSION_COOKIE, "9999999999.forged")
        assert client.get("/runs").status_code == 303

    def test_logging_out_clears_the_session(self, client: TestClient) -> None:
        _sign_in(client)
        client.post("/logout")
        assert client.get("/runs").status_code == 303


class TestThrottling:
    def test_repeated_failures_are_throttled(self, client: TestClient) -> None:
        for _ in range(5):
            client.post("/login", data={"password": "wrong"})
        response = client.post("/login", data={"password": "wrong"})
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) >= 1

    def test_the_throttle_applies_even_to_the_right_password(self, client: TestClient) -> None:
        """Otherwise a correct guess at attempt 500 still wins."""
        for _ in range(5):
            client.post("/login", data={"password": "wrong"})
        assert client.post("/login", data={"password": PASSWORD}).status_code == 429

    def test_a_success_before_the_limit_clears_the_count(self, client: TestClient) -> None:
        for _ in range(3):
            client.post("/login", data={"password": "wrong"})
        _sign_in(client)
        for _ in range(3):
            client.post("/login", data={"password": "wrong"})
        assert client.post("/login", data={"password": "wrong"}).status_code == 401


class TestClientIdentity:
    """Which address the throttle counts against.

    Trusting a forwarded header from anyone lets a client choose its own
    rate-limit bucket by sending a different value each time, which would make
    the throttle decorative.
    """

    def test_a_forged_forwarded_header_is_ignored_without_a_proxy(self, client: TestClient) -> None:
        for index in range(5):
            client.post(
                "/login",
                data={"password": "wrong"},
                headers={"x-forwarded-for": f"10.0.0.{index}"},
            )
        response = client.post(
            "/login", data={"password": "wrong"}, headers={"x-forwarded-for": "10.0.0.99"}
        )
        assert response.status_code == 429

    def test_the_forwarded_header_is_used_behind_a_proxy(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path, password=PASSWORD, behind_proxy=True)
        with TestClient(create_app(settings), follow_redirects=False) as client:
            for index in range(5):
                client.post(
                    "/login",
                    data={"password": "wrong"},
                    headers={"x-forwarded-for": f"10.0.0.{index}"},
                )
            response = client.post(
                "/login", data={"password": "wrong"}, headers={"x-forwarded-for": "10.0.0.99"}
            )
            assert response.status_code == 401

    def test_only_the_first_hop_of_the_chain_is_taken(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path, password=PASSWORD, behind_proxy=True)
        with TestClient(create_app(settings), follow_redirects=False) as client:
            for _ in range(5):
                client.post(
                    "/login",
                    data={"password": "wrong"},
                    headers={"x-forwarded-for": "10.0.0.1, 172.16.0.1, 192.168.0.1"},
                )
            response = client.post(
                "/login",
                data={"password": "wrong"},
                headers={"x-forwarded-for": "10.0.0.1, 172.16.0.9"},
            )
            assert response.status_code == 429


class TestCsrfHelper:
    """Exercised directly here; the forms that use it arrive with uploads."""

    def test_a_token_from_the_session_is_accepted(self, settings: WebSettings) -> None:
        from settle_app.app import Web
        from settle_app.context import AppContext

        web = Web(AppContext.build(settings))
        with TestClient(create_app(settings), follow_redirects=False) as client:
            client.post("/login", data={"password": PASSWORD})
            token = client.cookies.get(SESSION_COOKIE)
        assert token is not None

        request = _request_with_cookie(SESSION_COOKIE, token)
        expected = web.csrf_for(request)
        assert expected
        assert web.csrf_ok(request, expected)
        assert not web.csrf_ok(request, "not-the-token")
        assert not web.csrf_ok(request, "")

    def test_an_instance_without_a_password_has_nothing_to_bind_to(self, tmp_path: Path) -> None:
        from settle_app.app import Web
        from settle_app.context import AppContext

        web = Web(AppContext.build(WebSettings(data_dir=tmp_path)))
        request = _request_with_cookie("irrelevant", "")
        assert web.csrf_ok(request, "")

    def test_a_request_with_no_session_has_no_token(self, settings: WebSettings) -> None:
        from settle_app.app import Web
        from settle_app.context import AppContext

        web = Web(AppContext.build(settings))
        assert web.csrf_for(_request_with_cookie("settle_session", "")) == ""


def _request_with_cookie(name: str, value: str) -> Request:
    """A Request is awkward to build by hand; only the cookies are read."""

    header = f"{name}={value}".encode()
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", header)] if value else [],
            "query_string": b"",
            "client": ("1.2.3.4", 1234),
        }
    )


class TestSecurityHeaders:
    def test_scripts_are_forbidden_outright(self, client: TestClient) -> None:
        """'No JavaScript' is a design claim; this is the enforced form of it."""
        policy = client.get("/login").headers["content-security-policy"]
        assert "default-src 'none'" in policy
        assert "frame-ancestors 'none'" in policy

    def test_the_usual_headers_are_present(self, client: TestClient) -> None:
        headers = client.get("/login").headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "same-origin"

    def test_hsts_only_appears_behind_tls(self, client: TestClient, tmp_path: Path) -> None:
        assert "strict-transport-security" not in client.get("/login").headers
        secure = WebSettings(data_dir=tmp_path / "tls", password=PASSWORD, https_only=True)
        with TestClient(create_app(secure), follow_redirects=False) as tls:
            assert "strict-transport-security" in tls.get("/login").headers


class TestUnprotectedInstance:
    """A loopback instance with no password is allowed, and says so."""

    def test_no_password_means_no_login_wall(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path)
        with TestClient(create_app(settings), follow_redirects=False) as client:
            assert client.get("/runs").status_code == 200

    def test_the_footer_warns_that_it_is_unprotected(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path)
        with TestClient(create_app(settings), follow_redirects=False) as client:
            assert "unprotected" in client.get("/runs").text

    def test_the_login_page_redirects_when_there_is_nothing_to_log_into(
        self, tmp_path: Path
    ) -> None:
        settings = WebSettings(data_dir=tmp_path)
        with TestClient(create_app(settings), follow_redirects=False) as client:
            assert client.get("/login").status_code == 303


class TestAccessLog:
    def test_logins_and_failures_are_recorded(self, settings: WebSettings) -> None:
        """One shared password cannot say who. It can at least say when."""
        with TestClient(create_app(settings), follow_redirects=False) as client:
            client.post("/login", data={"password": "wrong"})
            client.post("/login", data={"password": PASSWORD})

        from settle_app.store import RunStore

        store = RunStore(settings.database_path)
        with store._connect() as connection:
            actions = [r["action"] for r in connection.execute("SELECT action FROM access_log")]
        assert actions == ["login-failed", "login"]


class TestCli:
    def test_version_prints_something(self) -> None:
        result = runner.invoke(cli, ["version"])
        assert result.exit_code == 0, result.output
        assert result.output.strip()

    def test_serve_has_help(self) -> None:
        result = runner.invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0, result.output

    def test_backup_copies_the_database(self, tmp_path: Path, settings: WebSettings) -> None:
        create_app(settings)
        destination = tmp_path / "backups" / "copy.db"
        result = runner.invoke(
            cli, ["backup", str(destination), "--data-dir", str(settings.data_dir)]
        )
        assert result.exit_code == 0, result.output
        assert destination.is_file()

    def test_backing_up_a_missing_database_is_an_error(self, tmp_path: Path) -> None:
        result = runner.invoke(
            cli, ["backup", str(tmp_path / "out.db"), "--data-dir", str(tmp_path / "empty")]
        )
        assert result.exit_code == 2
        assert "no database" in result.output

    def test_a_bad_environment_exits_rather_than_serving(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The startup refusal has to reach the operator as an exit code."""
        monkeypatch.setenv("SETTLE_WEB_HOST", "0.0.0.0")
        monkeypatch.delenv("SETTLE_WEB_PASSWORD", raising=False)
        monkeypatch.setenv("SETTLE_WEB_DATA_DIR", str(tmp_path))
        result = runner.invoke(cli, ["serve"])
        assert result.exit_code == 2
        assert "refusing to bind" in result.output
