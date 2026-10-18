"""DSH judge engine.

Runs one dedicated, read-only DSH judge turn through the safe Headless adapter
and returns a strict JSON proposal. The engine never writes run files: the
normalization, evidence validation, deterministic caps, and report writing
remain in ``auditrail.agents.tools.write_judge_report``.

Isolation rules enforced here:

* the judge profile is a server-owned setting and must differ from every
  evaluand profile, so a judge session can never resume an evaluated session;
* the judge workspace is a server-owned directory strictly beneath an allowed
  root and must differ from the evaluated workspace;
* the judge turn receives only bounded, embedded run evidence and no tools.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..agents.schemas import missing_judge_dimensions
from ..dsh_headless import DshHeadlessClient, run_subprocess
from ..dsh_runtime import (
    DshExecutionResult,
    DshRunRequest,
    DshRuntimeConfig,
    DshValidationError,
    validate_judge_request,
)
from ..dsh_source import find_session_file, read_events
from .base import DSH_ENGINE, JudgeExecutionError, JudgeRequest, JudgeResult, JudgeUnavailableError
from .prompting import build_dimension_repair_note, build_dsh_judge_prompt
from .proposals import JudgeProposalError, parse_judge_proposal

_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class DshJudgeEngine:
    """Run the configured dedicated DSH judge profile as a judge engine."""

    id = DSH_ENGINE
    label = "DSH 裁判"

    def __init__(
        self,
        settings: Any = None,
        *,
        runner: Any = None,
        client: Optional[DshHeadlessClient] = None,
        client_factory: Any = None,
    ):
        self._settings = settings
        self._runner = runner or run_subprocess
        self._client = client
        self._client_factory = client_factory

    def available(self) -> Optional[str]:
        try:
            self._resolve(None)
        except JudgeUnavailableError as exc:
            return str(exc)
        return None

    def run(self, request: JudgeRequest) -> JudgeResult:
        try:
            config, profile, workspace = self._resolve(Path(request.run_dir))
        except JudgeUnavailableError as exc:
            raise JudgeUnavailableError("dsh_unavailable", str(exc)) from exc

        result, invocation = self._run_turn(config, profile, workspace, build_dsh_judge_prompt(request.run_dir))
        proposal = self._parse_proposal(result)
        missing = missing_judge_dimensions(proposal)
        if missing:
            # One bounded repair attempt on the same bounded run evidence: the
            # note names the omitted dimensions and demands the full report.
            # Strictly parseable JSON is not enough — a report-shaped proposal
            # that omits dimensions is not publishable.
            repair_prompt = build_dsh_judge_prompt(request.run_dir) + build_dimension_repair_note(missing)
            result, invocation = self._run_turn(config, profile, workspace, repair_prompt)
            proposal = self._parse_proposal(result)
            missing = missing_judge_dimensions(proposal)
            if missing:
                raise JudgeExecutionError(
                    "incomplete_proposal",
                    "judge proposal is still incomplete after one repair attempt; "
                    "missing dimensions: %s" % ", ".join(missing),
                )
        return JudgeResult(engine=self.id, proposal=proposal, metadata=self._metadata(result, invocation))

    def _run_turn(
        self,
        config: DshRuntimeConfig,
        profile: str,
        workspace: Path,
        message: str,
    ) -> tuple[DshExecutionResult, Any]:
        run_request = DshRunRequest(
            message=message,
            profile=profile,
            workspace=workspace,
            timeout_seconds=self._timeout_seconds(),
        )
        invocation = validate_judge_request(config, run_request, judge_profile=profile)
        result: DshExecutionResult = self._client_for(config).run(run_request, invocation=invocation)
        if not result.succeeded:
            raise self._execution_error(result)
        return result, invocation

    @staticmethod
    def _parse_proposal(result: DshExecutionResult) -> dict[str, Any]:
        try:
            return parse_judge_proposal(result.final_text)
        except JudgeProposalError as exc:
            raise JudgeExecutionError("invalid_proposal", str(exc)) from exc

    # -- configuration -------------------------------------------------

    def _resolve(self, run_dir: Optional[Path]) -> tuple[DshRuntimeConfig, str, Path]:
        settings = self._settings
        if settings is None:
            raise JudgeUnavailableError("dsh_disabled", "local DSH runtime is disabled")
        if not getattr(settings, "dsh_runtime_enabled", False):
            raise JudgeUnavailableError("dsh_disabled", "local DSH runtime is disabled")

        profile = str(getattr(settings, "dsh_judge_profile", "") or "").strip()
        if not profile:
            raise JudgeUnavailableError(
                "judge_profile_not_configured",
                "DSH_JUDGE_PROFILE is required to run the DSH judge",
            )
        if not _PROFILE_RE.fullmatch(profile):
            raise JudgeUnavailableError("judge_profile_invalid", "DSH judge profile is invalid")

        evaluand_profiles = tuple(str(value).strip() for value in getattr(settings, "dsh_allowed_profiles", ()) or ())
        if profile in evaluand_profiles:
            raise JudgeUnavailableError(
                "judge_profile_not_isolated",
                "DSH judge profile must differ from every evaluand profile",
            )

        config = _build_runtime_config(settings)

        workspace = getattr(settings, "dsh_judge_workspace", None)
        if workspace is None:
            raise JudgeUnavailableError(
                "judge_workspace_not_configured",
                "DSH_JUDGE_WORKSPACE is required to run the DSH judge",
            )
        workspace = Path(workspace).expanduser()
        if not workspace.exists() or not workspace.is_dir():
            raise JudgeUnavailableError(
                "judge_workspace_missing",
                "configured DSH judge workspace does not exist",
            )
        if run_dir is not None and workspace.resolve(strict=False) == Path(run_dir).resolve(strict=False):
            raise JudgeUnavailableError(
                "judge_workspace_reused",
                "judge workspace must differ from the evaluated workspace",
            )
        try:
            resolved = config.validate_workspace(workspace)
        except DshValidationError as exc:
            raise JudgeUnavailableError("judge_workspace_invalid", str(exc)) from exc
        return config, profile, resolved

    def _timeout_seconds(self) -> float:
        value = getattr(self._settings, "dsh_default_timeout_seconds", None)
        return float(value) if value is not None else 300.0

    def _client_for(self, config: DshRuntimeConfig) -> DshHeadlessClient:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            return self._client_factory(config, runner=self._runner)
        return DshHeadlessClient(config, runner=self._runner)

    # -- results -------------------------------------------------------

    def _execution_error(self, result: DshExecutionResult) -> JudgeExecutionError:
        error = result.error
        code = error.code if error is not None else "dsh_execution_failed"
        message = error.message if error is not None else "DSH judge execution failed"
        if result.timed_out:
            return JudgeExecutionError("dsh_timeout", "DSH judge execution exceeded the configured timeout")
        if result.cancelled:
            return JudgeExecutionError("dsh_cancelled", "DSH judge execution was cancelled")
        if code == "spawn_failed":
            return JudgeUnavailableError("dsh_spawn_failed", message)
        return JudgeExecutionError(code, message)

    def _metadata(self, result: DshExecutionResult, invocation: Any) -> dict[str, Any]:
        model, version = self._recording_provenance(str(result.session_id or ""))
        metadata = {
            "runtime": "dsh",
            "profile": invocation.profile,
            "workspace": str(invocation.workspace),
            "session_id": str(result.session_id or ""),
            "exit_code": result.exit_code,
            "turn_end_reason": str(result.turn_end_reason or ""),
            "events": len(result.events),
        }
        if model:
            metadata["model"] = model
        if version:
            metadata["version"] = version
        return metadata

    def _recording_provenance(self, session_id: str) -> tuple[str, str]:
        """Read the judge model/version from the durable recording, never argv."""
        dsh_home = getattr(self._settings, "dsh_home", None)
        if dsh_home is None or not session_id:
            return "", ""
        try:
            session_path = find_session_file(Path(dsh_home), session_id)
            events = read_events(session_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return "", ""
        model = ""
        version = ""
        for event in events:
            if version == "" and isinstance(event.get("version"), (str, int)):
                version = str(event["version"])
            if model == "" and event.get("type") == "request/header":
                data = event.get("data") or {}
                header = data.get("header") if isinstance(data.get("header"), dict) else {}
                config = header.get("config") if isinstance(header.get("config"), dict) else {}
                for source in (config, data):
                    value = source.get("model") or source.get("modelId")
                    if isinstance(value, str) and value:
                        model = value
                        break
        return model, version


def _build_runtime_config(settings: Any) -> DshRuntimeConfig:
    """Build the trusted DSH policy without importing the backend layer."""
    binary = getattr(settings, "dsh_binary", None)
    if binary is None:
        raise JudgeUnavailableError("dsh_disabled", "DSH_BINARY is required when local DSH runtime is enabled")
    try:
        return DshRuntimeConfig(
            binary=binary,
            allowed_profiles=tuple(getattr(settings, "dsh_allowed_profiles", ()) or ()),
            allowed_workspace_roots=tuple(getattr(settings, "dsh_allowed_workspace_roots", ()) or ()),
            judge_profile=str(getattr(settings, "dsh_judge_profile", "") or ""),
            default_timeout_seconds=float(getattr(settings, "dsh_default_timeout_seconds", 300) or 300),
            max_timeout_seconds=float(getattr(settings, "dsh_max_timeout_seconds", 900) or 900),
        )
    except DshValidationError as exc:
        raise JudgeUnavailableError("dsh_policy_invalid", str(exc)) from exc
