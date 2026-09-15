"""The two-product claim, checked rather than asserted.

The repository ships two distributions. ``settle-engine`` is the expert's
product: a pure engine with no UI, no database and no state. ``settle-app`` is
this one, which has all three. That split is only worth anything if it is real,
and the way it stops being real is gradual — one import, one "obviously useful
in both" helper — so it is tested here instead of being promised in a README.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_SRC = REPO_ROOT / "src" / "settle"

#: Installing the engine must not drag in a web stack. These are the packages
#: that would mean it had.
WEB_ONLY_PACKAGES = ("fastapi", "starlette", "jinja2", "uvicorn", "multipart")


class TestTheEngineDoesNotKnowAboutTheApp:
    def test_no_engine_module_mentions_settle_app(self) -> None:
        """A single import would make the engine unshippable on its own."""
        offenders = [
            path.relative_to(REPO_ROOT)
            for path in ENGINE_SRC.rglob("*.py")
            if "settle_app" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"the engine references the app in {offenders}"

    def test_importing_the_engine_does_not_import_a_web_stack(self) -> None:
        """Run in a fresh interpreter, because this test session imports both."""
        probe = (
            "import sys; import settle; import settle.domain.match;"
            "import settle.io.csv_io; import settle.cli;"
            f"leaked=[n for n in {WEB_ONLY_PACKAGES!r} if n in sys.modules];"
            "print(','.join(leaked))"
        )
        finished = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        )
        assert finished.stdout.strip() == "", (
            f"importing the engine pulled in {finished.stdout.strip()}; "
            "the api extra is optional and must stay lazily imported"
        )


class TestTheDistributionsStaySeparate:
    def test_the_engine_requires_nothing_from_the_web_stack(self) -> None:
        """Optional extras may use FastAPI. The base install may not."""
        manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        required = " ".join(manifest["project"]["dependencies"]).lower()
        for package in WEB_ONLY_PACKAGES:
            assert package not in required, (
                f"{package} is a required dependency of settle-engine; "
                "it belongs in an extra or in settle-app"
            )

    def test_the_app_depends_on_the_engine_explicitly(self) -> None:
        manifest = tomllib.loads((REPO_ROOT / "app" / "pyproject.toml").read_text(encoding="utf-8"))
        required = " ".join(manifest["project"]["dependencies"]).lower()
        assert "settle-engine" in required

    def test_the_two_distributions_have_different_names(self) -> None:
        engine = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        app = tomllib.loads((REPO_ROOT / "app" / "pyproject.toml").read_text(encoding="utf-8"))
        assert engine["project"]["name"] == "settle-engine"
        assert app["project"]["name"] == "settle-app"
