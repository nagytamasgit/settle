"""Configuration, and the startup refusals.

Most of this file is about the ways an instance can be configured wrongly,
because that is most of what the module does. The one that matters is the last
class: an instance bound to a public address with no password would serve other
people's bank statements to anyone who found it, and it must not start.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from settle_app.settings import MIN_PASSWORD_LENGTH, WebConfigError, WebSettings

GOOD_PASSWORD = "correct-horse-battery-staple"


class TestDefaults:
    def test_a_bare_loopback_instance_needs_no_password(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path)
        assert settings.password is None
        assert settings.is_loopback

    def test_paths_hang_off_the_data_directory(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path)
        assert settings.database_path == tmp_path / "settle.db"
        assert settings.runs_dir == tmp_path / "runs"
        assert settings.secret_path == tmp_path / "secret.key"

    def test_the_model_tier_is_off_unless_asked_for(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path)
        assert settings.model_mapper is False
        assert settings.model_samples is False


class TestRefusals:
    def test_binding_publicly_without_a_password_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(WebConfigError, match="refusing to bind"):
            WebSettings(data_dir=tmp_path, host="0.0.0.0")

    def test_binding_publicly_with_a_password_is_allowed(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path, host="0.0.0.0", password=GOOD_PASSWORD)
        assert not settings.is_loopback

    def test_a_short_password_is_refused_and_the_source_is_named(self, tmp_path: Path) -> None:
        with pytest.raises(WebConfigError, match="SETTLE_WEB_PASSWORD"):
            WebSettings(data_dir=tmp_path, password="short")

    def test_the_floor_is_enforced_exactly(self, tmp_path: Path) -> None:
        WebSettings(data_dir=tmp_path, password="x" * MIN_PASSWORD_LENGTH)
        with pytest.raises(WebConfigError):
            WebSettings(data_dir=tmp_path, password="x" * (MIN_PASSWORD_LENGTH - 1))

    def test_sending_samples_without_the_model_tier_is_refused(self, tmp_path: Path) -> None:
        """Otherwise the setting reads as enabled but does nothing."""
        with pytest.raises(WebConfigError, match="would never be sent"):
            WebSettings(data_dir=tmp_path, model_samples=True)

    @pytest.mark.parametrize(
        "field", ["max_upload_bytes", "max_rows", "max_columns", "session_ttl_seconds"]
    )
    def test_nonsensical_limits_are_refused(self, tmp_path: Path, field: str) -> None:
        with pytest.raises(WebConfigError, match=field):
            WebSettings(data_dir=tmp_path, **{field: 0})

    def test_a_zero_retention_window_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(WebConfigError, match="retention_days"):
            WebSettings(data_dir=tmp_path, retention_days=0)

    def test_keeping_runs_forever_is_spelled_none(self, tmp_path: Path) -> None:
        assert WebSettings(data_dir=tmp_path, retention_days=None).retention_days is None


class TestFromEnvironment:
    def test_an_empty_environment_gives_a_working_local_instance(self) -> None:
        settings = WebSettings.from_env({})
        assert settings.host == "127.0.0.1"
        assert settings.password is None
        assert settings.data_dir == Path("data")

    def test_values_are_read_and_converted(self, tmp_path: Path) -> None:
        settings = WebSettings.from_env(
            {
                "SETTLE_WEB_DATA_DIR": str(tmp_path),
                "SETTLE_WEB_PASSWORD": GOOD_PASSWORD,
                "SETTLE_WEB_HOST": "0.0.0.0",
                "SETTLE_WEB_PORT": "9000",
                "SETTLE_WEB_MAX_ROWS": "1234",
                "SETTLE_WEB_RETENTION_DAYS": "30",
                "SETTLE_WEB_BEHIND_PROXY": "1",
                "SETTLE_WEB_HTTPS": "true",
                "SETTLE_WEB_MODEL_MAPPER": "yes",
            }
        )
        assert settings.port == 9000
        assert settings.max_rows == 1234
        assert settings.retention_days == 30
        assert settings.behind_proxy is True
        assert settings.https_only is True
        assert settings.model_mapper is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "off", "nonsense"])
    def test_only_affirmative_words_turn_a_flag_on(self, value: str) -> None:
        settings = WebSettings.from_env({"SETTLE_WEB_BEHIND_PROXY": value})
        assert settings.behind_proxy is False

    def test_a_non_numeric_limit_names_the_variable(self) -> None:
        with pytest.raises(WebConfigError, match="SETTLE_WEB_MAX_ROWS"):
            WebSettings.from_env({"SETTLE_WEB_MAX_ROWS": "lots"})

    def test_a_non_numeric_retention_names_the_variable(self) -> None:
        with pytest.raises(WebConfigError, match="SETTLE_WEB_RETENTION_DAYS"):
            WebSettings.from_env({"SETTLE_WEB_RETENTION_DAYS": "forever"})

    def test_the_password_can_come_from_a_file(self, tmp_path: Path) -> None:
        """Docker secrets are files; an env var shows up in `docker inspect`."""
        secret = tmp_path / "password"
        secret.write_text(f"  {GOOD_PASSWORD}\n", encoding="utf-8")
        settings = WebSettings.from_env(
            {"SETTLE_WEB_PASSWORD_FILE": str(secret), "SETTLE_WEB_DATA_DIR": str(tmp_path)}
        )
        assert settings.password == GOOD_PASSWORD

    def test_the_file_wins_over_the_variable(self, tmp_path: Path) -> None:
        secret = tmp_path / "password"
        secret.write_text(GOOD_PASSWORD, encoding="utf-8")
        settings = WebSettings.from_env(
            {
                "SETTLE_WEB_PASSWORD_FILE": str(secret),
                "SETTLE_WEB_PASSWORD": "the-other-password",
            }
        )
        assert settings.password == GOOD_PASSWORD

    def test_an_unreadable_password_file_is_an_error_not_a_silent_fallback(
        self, tmp_path: Path
    ) -> None:
        """Falling back to no password here would silently unlock the box."""
        with pytest.raises(WebConfigError, match="cannot read"):
            WebSettings.from_env({"SETTLE_WEB_PASSWORD_FILE": str(tmp_path / "missing")})

    def test_a_short_password_from_a_file_names_the_file(self, tmp_path: Path) -> None:
        secret = tmp_path / "password"
        secret.write_text("tiny", encoding="utf-8")
        with pytest.raises(WebConfigError, match="password"):
            WebSettings.from_env({"SETTLE_WEB_PASSWORD_FILE": str(secret)})
