import os

import pytest

from backend.app import config as config_module
from backend.app.config import (
    Settings,
    build_dsh_runtime_config,
    get_settings,
    load_env_file,
    resolve_run_path,
)


def test_dotenv_loading_is_disabled_by_the_test_guard(monkeypatch):
    """A developer's ``.env`` must not be able to steer a test run."""
    monkeypatch.setenv(config_module.DOTENV_ENABLED_VAR, "0")
    monkeypatch.setattr(config_module, "_dotenv_loaded", False)
    assert load_env_file() is False
    assert config_module._dotenv_loaded is False, "a disabled load must stay retryable"


def test_dotenv_loading_reads_the_configured_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("AUDITRAIL_TEST_DOTENV_KEY=loaded-from-file\n", encoding="utf-8")
    monkeypatch.delenv("AUDITRAIL_TEST_DOTENV_KEY", raising=False)
    monkeypatch.setenv(config_module.DOTENV_ENABLED_VAR, "1")
    monkeypatch.setattr(config_module, "ENV_FILE", env_file)
    monkeypatch.setattr(config_module, "_dotenv_loaded", False)

    assert load_env_file() is True
    assert os.environ["AUDITRAIL_TEST_DOTENV_KEY"] == "loaded-from-file"
    monkeypatch.delenv("AUDITRAIL_TEST_DOTENV_KEY", raising=False)


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
    assert settings.dsh_acp_enabled is False
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


def test_get_settings_reads_the_acp_runtime_switch(monkeypatch):
    """ACP is opt-in: unset means off, and only an explicit truthy value enables it."""
    monkeypatch.delenv("DSH_ACP_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().dsh_acp_enabled is False
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv("DSH_ACP_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert get_settings().dsh_acp_enabled is True
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv("DSH_ACP_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert get_settings().dsh_acp_enabled is False
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv("DSH_ACP_ENABLED", "sometimes")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="DSH_ACP_ENABLED"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_master_switch_alone_does_not_enable_acp(monkeypatch, tmp_path):
    """Enabling the master DSH runtime must not implicitly turn ACP on."""
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("DSH_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("DSH_BINARY", "/opt/dsh/bin/dsh")
    monkeypatch.setenv("DSH_ALLOWED_PROFILES", "headless")
    monkeypatch.setenv("DSH_ALLOWED_WORKSPACE_ROOTS", str(root))
    monkeypatch.delenv("DSH_ACP_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.dsh_runtime_enabled is True
        assert settings.dsh_acp_enabled is False
        # The master switch plus a valid allowlist is enough for Headless...
        build_dsh_runtime_config(settings)
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv("DSH_ACP_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert get_settings().dsh_acp_enabled is True
    finally:
        get_settings.cache_clear()

