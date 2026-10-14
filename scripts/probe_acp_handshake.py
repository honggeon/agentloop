"""Opt-in probe: the ACP handshake against a real local DSH installation.

This is intentionally not a pytest test or a production entry point. It exists
only to prove M6A's protocol conclusions against a real ``dsh --profile acp``
process, and it is scoped so that it never triggers a model turn:

* telemetry is forced to ``DISABLED`` so no OTel upload happens;
* only ``initialize`` is sent, because DSH's ``initialize`` handler checks image
  capability support and does not compose an agent or call a model;
* stdin is closed afterwards, which is DSH's documented shutdown request.

Run it only when the DSH binary is present and telemetry-free verification is
wanted:

    DSH_BINARY="$(command -v dsh)" python3 scripts/probe_acp_handshake.py

It needs a writable profile directory but no API credentials and no network
access. It exits non-zero when the handshake does not match the documented
contract.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from auditrail.dsh_acp import (  # noqa: E402
    ACP_PROTOCOL_VERSION,
    AcpClient,
    AcpError,
    AcpTransport,
    build_acp_argv,
    spawn_acp_process,
)
from auditrail.dsh_runtime import DshRuntimeConfig  # noqa: E402


def main() -> int:
    binary = os.environ.get("DSH_BINARY") or "dsh"
    profile = os.environ.get("DSH_ACP_PROFILE", "acp")
    workspace = Path(os.environ.get("DSH_ACP_WORKSPACE", os.getcwd())).resolve()

    config = DshRuntimeConfig(
        binary=Path(binary),
        allowed_profiles=(profile,),
        allowed_workspace_roots=(workspace.parent,),
        default_timeout_seconds=30,
        max_timeout_seconds=60,
    )
    argv = build_acp_argv(config, profile)
    print("argv: %s" % (argv,))
    print("cwd:  %s" % workspace)

    # An ACP process must own stdout exclusively, so diagnostics go to stderr.
    process = spawn_acp_process(
        argv,
        cwd=workspace,
        extra_env={"DSH_TELEMETRY_MODE": "DISABLED"},
    )
    transport = AcpTransport(process).start()
    client = AcpClient(transport, config, profile=profile)
    exit_code = 1
    try:
        result = client.initialize(timeout_seconds=30)
        print(json.dumps(result.raw, indent=2, ensure_ascii=False))
        problems = []
        if result.protocol_version != ACP_PROTOCOL_VERSION:
            problems.append("protocol version is %s" % result.protocol_version)
        if result.agent_name != "deepseek-harness-acp":
            problems.append("unexpected agentInfo.name %r" % result.agent_name)
        for capability in ("supports_session_list", "supports_session_resume", "supports_session_close"):
            if not getattr(result, capability):
                problems.append("missing %s" % capability)
        if result.session_capabilities and "load" in result.session_capabilities:
            problems.append("session/load unexpectedly advertised")
        if problems:
            print("CONTRACT MISMATCH: %s" % "; ".join(problems), file=sys.stderr)
        else:
            print("handshake OK: protocol v%d negotiated with %s" % (
                result.protocol_version,
                result.agent_name,
            ))
            exit_code = 0
    except AcpError as exc:
        print("handshake failed: %s: %s" % (exc.code, exc), file=sys.stderr)
    finally:
        # Closing stdin is DSH's supported shutdown request; then stop the group.
        transport.close()
        diagnostics = transport.stderr_diagnostics.strip()
        if diagnostics:
            print("--- stderr ---\n%s" % diagnostics, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
