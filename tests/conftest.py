"""Shared test isolation and hermeticity guards.

The module-level statements here run before any test module is collected, which
is the only point early enough to stop the application from reading the
developer's local ``.env`` at import time. Without the guard, a local ``.env``
that enables the DSH runtime, repoints ``AUDITRAIL_RUNS_DIR``, or names a remote
LLM proxy log would silently change what a test run exercises.

The fixtures below then make that guarantee enforceable rather than incidental:
a test that tries to reach the network through a familiar egress command fails
loudly instead of quietly depending on the operator's environment.
"""

from __future__ import annotations

import os
import subprocess

import pytest

# Must run at import time: ``backend.app.main`` triggers ``get_settings()``
# while a test module is being imported, which is after this file is loaded.
# Forced, not defaulted: a stray value in the operator's shell must not be able
# to re-enable ``.env`` loading for the test run.
os.environ["AUDITRAIL_LOAD_DOTENV"] = "0"

#: Egress commands that no deterministic test may run. ``auditrail.llm_proxy``
#: can import a proxy log from a remote host with ``scp``, and a developer's
#: ``.env`` is exactly where such a remote path lives.
_NETWORK_EGRESS_COMMANDS = frozenset({"scp", "sftp", "ssh", "rsync", "curl", "wget"})


@pytest.fixture(autouse=True)
def no_network_egress(monkeypatch):
    """Fail a test that shells out to a network transfer command.

    This is a violation detector, not a sandbox: it only intercepts commands that
    move data off the machine by name, so an offline ``subprocess`` use in a test
    keeps working. A failure here means the test (or the code under test) picked
    up configuration from outside the repository's control.
    """
    real_run = subprocess.run

    def guarded_run(command, *args, **kwargs):
        name = _command_name(command)
        if name in _NETWORK_EGRESS_COMMANDS:
            raise AssertionError(
                "test attempted network egress via %r; deterministic tests must not reach the network" % name
            )
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)


def _command_name(command) -> str:
    if isinstance(command, (str, bytes, os.PathLike)):
        return os.path.basename(os.fsdecode(command))
    try:
        first = next(iter(command))
    except (TypeError, StopIteration):
        return ""
    return os.path.basename(os.fsdecode(first))


def full_judge_dimensions(**overrides):
    """A complete 13-dimension judge submission for judge-pipeline tests.

    Every dimension key is present with ``source="judge"``. Dimensions the
    judge could not evidence stay ``status="unknown"``/``score=None`` — a
    legitimate verdict, distinct from a missing field. ``overrides`` replaces
    whole dimension entries, e.g. ``full_judge_dimensions(verification={...})``.
    """
    from auditrail.agents.schemas import DIMENSION_DEFINITIONS

    dimensions = {
        key: {
            "score": None,
            "status": "unknown",
            "summary": "证据不足",
            "evidence": [],
            "recommendation": "",
            "source": "judge",
        }
        for key in DIMENSION_DEFINITIONS
    }
    dimensions.update(overrides)
    return dimensions
