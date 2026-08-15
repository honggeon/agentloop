import pytest

from backend.app.config import Settings, resolve_run_path


def test_resolve_run_path_accepts_child_directory(tmp_path):
    run = tmp_path / "runs" / "20260807-safe"
    run.mkdir(parents=True)
    path = resolve_run_path("20260807-safe", Settings(runs_dir=tmp_path / "runs"))
    assert path == run


def test_resolve_run_path_rejects_path_traversal(tmp_path):
    (tmp_path / "runs").mkdir()
    with pytest.raises(ValueError):
        resolve_run_path("../secret", Settings(runs_dir=tmp_path / "runs"))

