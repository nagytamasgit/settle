"""Run files on disk, and the reasons a request cannot reach anything else."""

from __future__ import annotations

from pathlib import Path

import pytest

from settle_app.artifacts import (
    ALLOCATIONS_CSV,
    DOWNLOADABLE,
    RESULT_JSON,
    ArtifactStore,
    is_run_id,
    new_run_id,
)

RUN = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "runs")


class TestRunIds:
    def test_a_generated_id_is_accepted(self) -> None:
        assert is_run_id(new_run_id())

    def test_generated_ids_differ(self) -> None:
        assert new_run_id() != new_run_id()

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "short",
            "../../etc/passwd",
            "A1B2C3D4E5F60718293A4B5C6D7E8F90",  # uppercase
            "a1b2c3d4e5f60718293a4b5c6d7e8f9",  # 31 chars
            "a1b2c3d4e5f60718293a4b5c6d7e8f90a",  # 33 chars
            "a1b2c3d4-e5f6-0718-293a-4b5c6d7e8f90",
            "g" * 32,  # not hex
        ],
    )
    def test_anything_else_is_rejected(self, value: str) -> None:
        assert not is_run_id(value)


class TestDirectories:
    def test_a_valid_id_gets_a_directory_under_the_root(self, store: ArtifactStore) -> None:
        directory = store.ensure(RUN)
        assert directory.is_dir()
        assert directory.parent == store.root

    def test_an_invalid_id_has_no_directory(self, store: ArtifactStore) -> None:
        assert store.directory("../escape") is None

    def test_ensuring_an_invalid_id_raises(self, store: ArtifactStore) -> None:
        with pytest.raises(ValueError, match="not a run id"):
            store.ensure("../escape")

    def test_ensuring_twice_is_fine(self, store: ArtifactStore) -> None:
        assert store.ensure(RUN) == store.ensure(RUN)


class TestLookup:
    def test_a_known_file_that_exists_is_found(self, store: ArtifactStore) -> None:
        (store.ensure(RUN) / ALLOCATIONS_CSV).write_text("payment_id\n", encoding="utf-8")
        found = store.path(RUN, ALLOCATIONS_CSV)
        assert found is not None
        assert found.read_text(encoding="utf-8") == "payment_id\n"

    def test_a_known_file_that_does_not_exist_is_none(self, store: ArtifactStore) -> None:
        store.ensure(RUN)
        assert store.path(RUN, RESULT_JSON) is None

    def test_an_unknown_filename_is_none_even_if_it_exists(self, store: ArtifactStore) -> None:
        """The whitelist is the control; presence on disk is not enough."""
        (store.ensure(RUN) / "secret.key").write_text("shh", encoding="utf-8")
        assert store.path(RUN, "secret.key") is None

    @pytest.mark.parametrize(
        "name",
        [
            "../secret.key",
            "../../etc/passwd",
            "..%2fsecret.key",
            "/etc/passwd",
            "result.json/../../../etc/passwd",
        ],
    )
    def test_traversal_attempts_are_none(self, store: ArtifactStore, name: str) -> None:
        store.ensure(RUN)
        assert store.path(RUN, name) is None

    def test_a_bad_run_id_is_none(self, store: ArtifactStore) -> None:
        assert store.path("../..", ALLOCATIONS_CSV) is None

    def test_available_lists_only_what_is_there_in_display_order(
        self, store: ArtifactStore
    ) -> None:
        directory = store.ensure(RUN)
        (directory / RESULT_JSON).write_text("{}", encoding="utf-8")
        (directory / ALLOCATIONS_CSV).write_text("x\n", encoding="utf-8")
        available = store.available(RUN)
        assert available == [name for name in DOWNLOADABLE if name in set(available)]
        assert set(available) == {RESULT_JSON, ALLOCATIONS_CSV}


class TestDeletion:
    def test_deleting_removes_the_directory(self, store: ArtifactStore) -> None:
        """Deleting has to really delete; the files are customer data."""
        directory = store.ensure(RUN)
        (directory / RESULT_JSON).write_text("{}", encoding="utf-8")
        assert store.delete(RUN) is True
        assert not directory.exists()

    def test_deleting_something_absent_reports_false(self, store: ArtifactStore) -> None:
        assert store.delete(RUN) is False

    def test_deleting_an_invalid_id_reports_false(self, store: ArtifactStore) -> None:
        assert store.delete("../escape") is False


class TestSize:
    def test_an_empty_run_is_zero(self, store: ArtifactStore) -> None:
        store.ensure(RUN)
        assert store.size_bytes(RUN) == 0

    def test_a_missing_run_is_zero(self, store: ArtifactStore) -> None:
        assert store.size_bytes(RUN) == 0

    def test_files_are_added_up(self, store: ArtifactStore) -> None:
        directory = store.ensure(RUN)
        (directory / RESULT_JSON).write_text("0123456789", encoding="utf-8")
        assert store.size_bytes(RUN) == 10
