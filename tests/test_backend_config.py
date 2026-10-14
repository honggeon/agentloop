import pytest

from backend.app.config import (
    Settings,
    build_dsh_runtime_config,
    get_settings,
    resolve_run_path,
)


def test_resolve_run_path_accepts_child_directory(tmp_path):
    run = tmp_path / "runs" / "20260807-safe"
    run.mkdir(parents=True)
    path = resolve_run_path("20260807-safe", Settings(runs_dir=tmp_path / "runs"))
    assert path == run


def test_resolve_run_path_rejects_path_traversal(tmp_path):
    (tmp_path / "runs").mkdir()
    with pytest.raises(ValueError):
        resolve_run_path("../secret", Settings(runs_dir=tmp_path / "runs"))


def test_dsh_runtime_defaults_do_not_enable_local_execution():
    settings = Settings()
    assert settings.dsh_runtime_enabled is False
    assert settings.dsh_binary is None
    assert settings.dsh_allowed_profiles == ()
    assert settings.dsh_allowed_workspace_roots == ()
    with pytest.raises(ValueError, match="disabled"):
        build_dsh_runtime_config(settings)


def test_get_settings_loads_fixed_dsh_runtime_policy(monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("DSH_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("DSH_BINARY", "/opt/dsh/bin/dsh")
    monkeypatch.setenv("DSH_ALLOWED_PROFILES", "headless,review")
    monkeypatch.setenv("DSH_ALLOWED_WORKSPACE_ROOTS", str(root))
    monkeypatch.setenv("DSH_DEFAULT_TIMEOUT_SECONDS", "40")
    monkeypatch.setenv("DSH_MAX_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("DSH_MAX_CONCURRENCY", "2")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        runtime = build_dsh_runtime_config(settings)
    finally:
        get_settings.cache_clear()

    assert settings.dsh_runtime_enabled is True
    assert settings.dsh_max_concurrency == 2
    assert str(runtime.binary) == "/opt/dsh/bin/dsh"
    assert runtime.allowed_profiles == ("headless", "review")
    assert runtime.allowed_workspace_roots == (root.resolve(),)
    assert runtime.default_timeout_seconds == 40
    assert runtime.max_timeout_seconds == 120


def test_enabled_dsh_runtime_requires_fixed_binary():
    with pytest.raises(ValueError, match="DSH_BINARY"):
        build_dsh_runtime_config(Settings(dsh_runtime_enabled=True))

