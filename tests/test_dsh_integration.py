"""Opt-in integration smoke tests against a real local DSH installation.

These tests are **excluded from the normal run**. They spawn the real ``dsh``
binary, which needs a configured profile, model credentials, and a writable DSH
home, and a real turn consumes model quota and writes a session recording into
``$DSH_HOME/sessions``. Everything deterministic lives in the other test
modules; this file only proves that the production adapters can drive a real
process end to end.

Enable explicitly:

    AUDITRAIL_DSH_INTEGRATION=1 python3 -m pytest tests/test_dsh_integration.py -q

Useful overrides:

* ``DSH_BINARY`` — absolute path to the launcher (default: ``dsh`` on ``PATH``).
* ``AUDITRAIL_DSH_INTEGRATION_PROFILE`` — the Headless/ACP profile to drive
  (default: ``headless``).
* ``AUDITRAIL_DSH_INTEGRATION_WORKSPACE`` — the workspace the agent runs in
  (default: a fresh temporary directory, so no repository file is touched).
* ``AUDITRAIL_DSH_INTEGRATION_HOME`` — the DSH home to use. When unset (the
  default) the test seeds an isolated home under the pytest temporary directory
  from the operator's real ``$DSH_HOME``/``~/.dsh``: profile configuration and
  credentials are copied, ``profiles/node_modules`` is linked rather than copied,
  and the repository's and the operator's DSH state is never written to. Set it
  to ``~/.dsh`` to drive the real home directly.
* ``AUDITRAIL_DSH_INTEGRATION_TIMEOUT`` — per-turn budget in seconds
  (default: 300).

Telemetry is forced to ``DISABLED`` for every spawned process, matching
``scripts/probe_acp_handshake.py``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from auditrail.dsh_acp import AcpClient, AcpTransport, build_acp_argv, spawn_acp_process
from auditrail.dsh_headless import DshHeadlessClient
from auditrail.dsh_runtime import DshRunRequest, DshRuntimeConfig

pytestmark = pytest.mark.integration

_ENABLED = str(os.environ.get("AUDITRAIL_DSH_INTEGRATION", "")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_DEFAULT_TIMEOUT = float(os.environ.get("AUDITRAIL_DSH_INTEGRATION_TIMEOUT", "300"))


def _resolve_binary() -> Path | None:
    configured = os.environ.get("DSH_BINARY", "").strip()
    candidate = configured or (shutil.which("dsh") or "")
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if not path.is_absolute() or not path.exists():
        return None
    return path


def _resolve_home() -> Path:
    configured = os.environ.get("AUDITRAIL_DSH_INTEGRATION_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return _seed_isolated_home()


def _source_home() -> Path:
    return Path(os.environ.get("DSH_HOME", "~/.dsh")).expanduser()


#: Files that make a profile directory usable; the heavy ``node_modules`` tree is
#: linked instead of copied.
_PROFILE_FILES = ("cordis.yml", "cordis.patch.yml", "package.json", "pnpm-workspace.yaml")
#: Home-level files a real turn needs (credentials) or that DSH rewrites.
_HOME_FILES = (".credentials.yaml", ".env", ".anonymous-user-id", "settings.yaml")


def _seed_isolated_home() -> Path:
    """Build a writable DSH home from the operator's, without touching theirs.

    DSH rewrites ``profiles/<name>/cordis.yml`` on every launch, so driving the
    operator's real home both mutates their state and fails outright under a
    read-only sandbox. Copying the small configuration files and linking the
    shared ``node_modules`` keeps the run faithful and disposable.
    """
    source = _source_home()
    target = Path(tempfile.mkdtemp(prefix="auditrail-dsh-home-"))
    (target / "profiles").mkdir(parents=True, exist_ok=True)
    (target / "sessions").mkdir(parents=True, exist_ok=True)
    profiles_root = source / "profiles"
    if profiles_root.is_dir():
        for profile_dir in profiles_root.iterdir():
            if not profile_dir.is_dir() or profile_dir.name == "node_modules":
                continue
            destination = target / "profiles" / profile_dir.name
            destination.mkdir(parents=True, exist_ok=True)
            for name in _PROFILE_FILES:
                candidate = profile_dir / name
                if candidate.is_file():
                    shutil.copy2(candidate, destination / name)
        shared = profiles_root / "node_modules"
        if shared.is_dir():
            try:
                (target / "profiles" / "node_modules").symlink_to(shared, target_is_directory=True)
            except OSError:
                pass
    for name in _HOME_FILES:
        candidate = source / name
        if candidate.is_file():
            shutil.copy2(candidate, target / name)
    return target.resolve()


_BINARY = _resolve_binary()
_SKIP_REASON = (
    "set AUDITRAIL_DSH_INTEGRATION=1 with a real dsh binary to run the DSH integration smoke tests"
)

if not _ENABLED:
    pytest.skip(_SKIP_REASON, allow_module_level=True)
if _BINARY is None:
    pytest.skip("DSH_BINARY is not an absolute existing path and dsh is not on PATH", allow_module_level=True)


@pytest.fixture(scope="module")
def dsh_env(tmp_path_factory):
    """A validated policy plus an isolated workspace for one real DSH run."""
    profile = os.environ.get("AUDITRAIL_DSH_INTEGRATION_PROFILE", "headless").strip() or "headless"
    configured_workspace = os.environ.get("AUDITRAIL_DSH_INTEGRATION_WORKSPACE", "").strip()
    if configured_workspace:
        workspace = Path(configured_workspace).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        workspace = tmp_path_factory.mktemp("dsh-integration-workspace").resolve()
    config = DshRuntimeConfig(
        binary=_BINARY,
        allowed_profiles=(profile,),
        allowed_workspace_roots=(workspace.parent,),
        default_timeout_seconds=min(_DEFAULT_TIMEOUT, 60),
        max_timeout_seconds=_DEFAULT_TIMEOUT,
    )
    # Resolved lazily so an ordinary test run never creates a DSH home.
    home = _resolve_home()
    return {
        "config": config,
        "profile": profile,
        "workspace": workspace,
        "home": home,
        "timeout": _DEFAULT_TIMEOUT,
    }


@pytest.fixture(autouse=True)
def _record_into_the_isolated_home(dsh_env, monkeypatch):
    """Point spawned DSH processes at the isolated home.

    The Headless runner and ``spawn_acp_process`` inherit the environment, so
    ``DSH_HOME`` is what decides where a session recording lands and which
    profiles are loaded.
    """
    monkeypatch.setenv("DSH_HOME", str(dsh_env["home"]))
    monkeypatch.setenv("AUDITRAIL_DSH_INTEGRATION_HOME", str(dsh_env["home"]))


# --- Headless ---------------------------------------------------------------


def test_headless_turn_produces_the_ndjson_contract(dsh_env):
    """One real Headless turn must satisfy the NDJSON protocol contract."""
    request = DshRunRequest(
        message="Reply with exactly: auditrail-integration-ok",
        profile=dsh_env["profile"],
        workspace=dsh_env["workspace"],
        timeout_seconds=min(dsh_env["timeout"], 120),
    )
    result = DshHeadlessClient(dsh_env["config"]).run(request)

    assert result.error is None, "headless turn failed: %s" % (result.error and result.error.to_dict())
    assert result.succeeded is True
    assert result.exit_code == 0
    assert result.session_id, "no session id was reported"
    assert result.turn_end_reason == "completed"

    event_types = [event.type for event in result.events]
    # The stream must open with the session event and end with a final event.
    assert event_types[0] == "session"
    assert event_types[-1] == "final"
    session_event = result.events[0]
    assert Path(str(session_event.data["cwd"])).resolve() == dsh_env["workspace"].resolve()
    assert result.final_text is not None


def test_headless_turn_records_a_durable_session(dsh_env):
    """The authoritative recording must exist and convert with ``dsh_source``."""
    from auditrail.dsh_source import find_session_file, load_session_response

    request = DshRunRequest(
        message="Reply with exactly: auditrail-integration-durable",
        profile=dsh_env["profile"],
        workspace=dsh_env["workspace"],
        timeout_seconds=min(dsh_env["timeout"], 120),
    )
    result = DshHeadlessClient(dsh_env["config"]).run(request)
    assert result.succeeded, "headless turn failed: %s" % (result.error and result.error.to_dict())

    session_path = find_session_file(dsh_env["home"], str(result.session_id))
    response = load_session_response(session_path)
    session = response["session"]
    assert session["session_id"] == result.session_id
    assert Path(str(session["workspace"])).resolve() == dsh_env["workspace"].resolve()
    assert session["messages"], "the durable recording held no messages"


def test_headless_timeout_is_reported_not_hung(dsh_env):
    """A one-second budget must fail explicitly instead of blocking."""
    request = DshRunRequest(
        message="Count from 1 to 500 slowly, one number per line.",
        profile=dsh_env["profile"],
        workspace=dsh_env["workspace"],
        timeout_seconds=1,
    )
    started = time.monotonic()
    result = DshHeadlessClient(dsh_env["config"]).run(request)
    elapsed = time.monotonic() - started

    assert result.succeeded is False
    assert result.error is not None
    # Either the turn timed out or it finished inside the budget; what matters is
    # that the process did not outlive a small multiple of the deadline.
    assert elapsed < 60, "timeout was not enforced in reasonable time"


# --- ACP --------------------------------------------------------------------


def test_acp_handshake_matches_the_documented_contract(dsh_env):
    """A real ACP process must negotiate protocol v1 with the expected features."""
    from auditrail.dsh_acp import ACP_PROTOCOL_VERSION

    profile = os.environ.get("AUDITRAIL_DSH_INTEGRATION_ACP_PROFILE", "acp").strip() or "acp"
    config = DshRuntimeConfig(
        binary=_BINARY,
        allowed_profiles=(profile,),
        allowed_workspace_roots=(dsh_env["workspace"].parent,),
        default_timeout_seconds=60,
        max_timeout_seconds=dsh_env["timeout"],
    )
    process = spawn_acp_process(
        build_acp_argv(config, profile),
        cwd=dsh_env["workspace"],
        extra_env={"DSH_TELEMETRY_MODE": "DISABLED"},
    )
    transport = AcpTransport(process).start()
    try:
        client = AcpClient(transport, config, profile=profile)
        result = client.initialize(timeout_seconds=60)
        assert result.protocol_version == ACP_PROTOCOL_VERSION
        assert result.supports_session_close
    finally:
        transport.close()
        diagnostics = transport.stderr_diagnostics.strip()
        if diagnostics:
            print("--- dsh stderr ---\n%s" % diagnostics)


def test_acp_session_and_prompt_round_trip(dsh_env):
    """A real ACP session must create, accept one prompt, and close."""
    profile = os.environ.get("AUDITRAIL_DSH_INTEGRATION_ACP_PROFILE", "acp").strip() or "acp"
    config = DshRuntimeConfig(
        binary=_BINARY,
        allowed_profiles=(profile,),
        allowed_workspace_roots=(dsh_env["workspace"].parent,),
        default_timeout_seconds=60,
        max_timeout_seconds=dsh_env["timeout"],
    )
    process = spawn_acp_process(
        build_acp_argv(config, profile),
        cwd=dsh_env["workspace"],
        extra_env={"DSH_TELEMETRY_MODE": "DISABLED"},
    )
    transport = AcpTransport(process).start()
    try:
        client = AcpClient(transport, config, profile=profile)
        client.initialize(timeout_seconds=60)
        session = client.new_session(dsh_env["workspace"], timeout_seconds=60)
        assert session.session_id

        events = []
        outcome = client.prompt(
            session.session_id,
            "Reply with exactly: auditrail-acp-integration-ok",
            timeout_seconds=dsh_env["timeout"],
            on_event=events.append,
        )
        assert outcome.cancelled is False
        assert outcome.stop_reason
        assert outcome.updates, "the turn produced no updates"
        assert any(event.type == "final" for event in outcome.updates)

        client.close_session(session.session_id, timeout_seconds=60)
    finally:
        transport.close()
        diagnostics = transport.stderr_diagnostics.strip()
        if diagnostics:
            print("--- dsh stderr ---\n%s" % diagnostics)


def test_acp_cancel_settles_without_hanging(dsh_env):
    """Cancelling a real turn must settle as cancelled, not run to its deadline."""
    profile = os.environ.get("AUDITRAIL_DSH_INTEGRATION_ACP_PROFILE", "acp").strip() or "acp"
    config = DshRuntimeConfig(
        binary=_BINARY,
        allowed_profiles=(profile,),
        allowed_workspace_roots=(dsh_env["workspace"].parent,),
        default_timeout_seconds=60,
        max_timeout_seconds=dsh_env["timeout"],
    )
    process = spawn_acp_process(
        build_acp_argv(config, profile),
        cwd=dsh_env["workspace"],
        extra_env={"DSH_TELEMETRY_MODE": "DISABLED"},
    )
    transport = AcpTransport(process).start()
    try:
        client = AcpClient(transport, config, profile=profile)
        client.initialize(timeout_seconds=60)
        session = client.new_session(dsh_env["workspace"], timeout_seconds=60)

        cancel_event = threading.Event()
        started = threading.Event()

        def cancel_soon():
            started.wait(timeout=10)
            time.sleep(1.0)
            cancel_event.set()

        threading.Thread(target=cancel_soon, daemon=True).start()
        started.set()
        outcome = client.prompt(
            session.session_id,
            "Write a very long essay about the history of computing, several thousand words.",
            timeout_seconds=min(dsh_env["timeout"], 120),
            cancel_event=cancel_event,
        )
        # The turn must settle as cancelled (locally or by the peer), never as a
        # successful completion the caller would import as a passing run.
        assert outcome.cancelled is True
    finally:
        transport.close()
        diagnostics = transport.stderr_diagnostics.strip()
        if diagnostics:
            print("--- dsh stderr ---\n%s" % diagnostics)
