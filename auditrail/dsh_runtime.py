"""Core contracts and validation for the local DSH runtime adapter.

This module intentionally has no dependency on the backend or FastAPI. It
provides the trusted server-side configuration boundary consumed by concrete
DSH transports such as the Headless adapter.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional


_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class DshRuntimeError:
    """A safe, structured failure suitable for a later HTTP mapping."""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class DshRuntimeException(Exception):
    """Base exception for invalid configuration or adapter misuse."""


class DshValidationError(DshRuntimeException, ValueError):
    """Trusted runtime configuration or a requested invocation is invalid."""


class DshProtocolError(DshRuntimeException):
    """DSH emitted output that violates the Headless NDJSON contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DshRuntimeConfig:
    """Server-owned execution policy for a local DSH runtime.

    The executable, profile allowlist, and workspace roots are trusted
    configuration. An untrusted caller can select only values admitted by this
    policy and can never provide launcher arguments or environment data.
    """

    binary: Path
    allowed_profiles: tuple[str, ...]
    allowed_workspace_roots: tuple[Path, ...]
    judge_profile: str = ""
    default_timeout_seconds: float = 300.0
    max_timeout_seconds: float = 900.0
    max_message_bytes: int = 64 * 1024
    max_event_bytes: int = 1024 * 1024
    max_stdout_bytes: int = 4 * 1024 * 1024
    max_stderr_bytes: int = 16 * 1024

    def __post_init__(self) -> None:
        binary = Path(self.binary).expanduser()
        if not binary.is_absolute():
            raise DshValidationError("DSH binary must be an absolute path")
        object.__setattr__(self, "binary", binary.resolve(strict=False))

        profiles = tuple(str(value).strip() for value in self.allowed_profiles if str(value).strip())
        if not profiles:
            raise DshValidationError("at least one DSH profile must be allowed")
        if len(set(profiles)) != len(profiles) or any(not _PROFILE_RE.fullmatch(value) for value in profiles):
            raise DshValidationError("DSH profile allowlist contains an invalid value")
        object.__setattr__(self, "allowed_profiles", profiles)

        judge = str(self.judge_profile or "").strip()
        if judge:
            if not _PROFILE_RE.fullmatch(judge):
                raise DshValidationError("DSH judge profile is invalid")
            if judge in profiles:
                raise DshValidationError("DSH judge profile must differ from every evaluand profile")
        object.__setattr__(self, "judge_profile", judge)

        roots: list[Path] = []
        for value in self.allowed_workspace_roots:
            root = Path(value).expanduser()
            if not root.is_absolute():
                raise DshValidationError("DSH workspace roots must be absolute paths")
            resolved = root.resolve(strict=False)
            if resolved == Path(resolved.anchor):
                raise DshValidationError("filesystem roots cannot be allowed DSH workspaces")
            roots.append(resolved)
        if not roots:
            raise DshValidationError("at least one DSH workspace root must be allowed")
        if len(set(roots)) != len(roots):
            raise DshValidationError("DSH workspace roots must be unique")
        object.__setattr__(self, "allowed_workspace_roots", tuple(roots))

        if (
            not math.isfinite(self.default_timeout_seconds)
            or not math.isfinite(self.max_timeout_seconds)
            or self.default_timeout_seconds <= 0
            or self.max_timeout_seconds <= 0
        ):
            raise DshValidationError("DSH timeouts must be finite and positive")
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise DshValidationError("default DSH timeout cannot exceed the maximum")
        for name in ("max_message_bytes", "max_event_bytes", "max_stdout_bytes", "max_stderr_bytes"):
            if getattr(self, name) <= 0:
                raise DshValidationError("DSH output and message limits must be positive")

    def validate_profile(self, profile: str) -> str:
        """Validate any server-owned profile: an evaluand or the judge profile."""
        value = str(profile or "").strip()
        allowed = set(self.allowed_profiles)
        if self.judge_profile:
            allowed.add(self.judge_profile)
        if not _PROFILE_RE.fullmatch(value) or value not in allowed:
            raise DshValidationError("DSH profile is not allowed")
        return value

    def validate_judge_profile(self, profile: str) -> str:
        """Validate the dedicated judge profile; it is not an evaluand profile."""
        value = str(profile or "").strip()
        if not self.judge_profile:
            raise DshValidationError("no DSH judge profile is configured")
        if not _PROFILE_RE.fullmatch(value) or value != self.judge_profile:
            raise DshValidationError("DSH judge profile is not allowed")
        return value

    def validate_workspace(self, workspace: str | Path) -> Path:
        value = Path(workspace).expanduser()
        if not value.is_absolute():
            raise DshValidationError("DSH workspace must be an absolute path")
        try:
            resolved = value.resolve(strict=True)
        except OSError as exc:
            raise DshValidationError("DSH workspace does not exist") from exc
        if not resolved.is_dir():
            raise DshValidationError("DSH workspace must be a directory")
        if not any(root != resolved and root in resolved.parents for root in self.allowed_workspace_roots):
            raise DshValidationError("DSH workspace is outside the allowed roots")
        return resolved

    def validate_timeout(self, timeout_seconds: Optional[float]) -> float:
        value = self.default_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if not math.isfinite(value) or value <= 0 or value > self.max_timeout_seconds:
            raise DshValidationError("DSH timeout is outside the allowed range")
        return value

    def validate_message(self, message: str) -> str:
        if not isinstance(message, str) or not message.strip():
            raise DshValidationError("DSH message must be a non-empty string")
        if "\x00" in message:
            raise DshValidationError("DSH message cannot contain NUL bytes")
        if len(message.encode("utf-8")) > self.max_message_bytes:
            raise DshValidationError("DSH message exceeds the configured limit")
        return message


@dataclass(frozen=True)
class DshRunRequest:
    """Inputs exposed by Headless; raw argv and environment are absent."""

    message: str
    profile: str
    workspace: Path
    session_id: Optional[str] = None
    timeout_seconds: Optional[float] = None


@dataclass(frozen=True)
class DshInvocation:
    """A request after all untrusted selections have been validated."""

    message: str
    profile: str
    workspace: Path
    session_id: Optional[str]
    timeout_seconds: float


def validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str):
        raise DshValidationError("invalid DSH session id")
    value = session_id
    if (
        not value
        or not value.strip()
        or len(value.encode("utf-8")) > 256
        or value.startswith("-")
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise DshValidationError("invalid DSH session id")
    return value


def validate_request(config: DshRuntimeConfig, request: DshRunRequest) -> DshInvocation:
    """Validate an evaluand turn; the judge profile is never an evaluand."""
    profile = config.validate_profile(request.profile)
    if config.judge_profile and profile == config.judge_profile:
        raise DshValidationError("the DSH judge profile cannot run an evaluand turn")
    return DshInvocation(
        message=config.validate_message(request.message),
        profile=profile,
        workspace=config.validate_workspace(request.workspace),
        session_id=validate_session_id(request.session_id) if request.session_id is not None else None,
        timeout_seconds=config.validate_timeout(request.timeout_seconds),
    )


def validate_judge_request(
    config: DshRuntimeConfig,
    request: DshRunRequest,
    *,
    judge_profile: str,
) -> DshInvocation:
    """Validate a dedicated judge turn against the same trusted DSH policy.

    The judge profile is server-owned and deliberately absent from the evaluand
    profile allowlist, so it is validated by its own identity while the message,
    workspace, session id, and timeout reuse the evaluand rules.
    """
    profile = str(judge_profile or "").strip()
    if request.profile != profile:
        raise DshValidationError("DSH judge request must use the configured judge profile")
    return DshInvocation(
        message=config.validate_message(request.message),
        profile=config.validate_judge_profile(profile),
        workspace=config.validate_workspace(request.workspace),
        session_id=validate_session_id(request.session_id) if request.session_id is not None else None,
        timeout_seconds=config.validate_timeout(request.timeout_seconds),
    )


@dataclass(frozen=True)
class DshRuntimeEvent:
    """One transport-neutral event normalized from the Headless stream."""

    type: str
    data: Mapping[str, Any] = field(default_factory=dict)
    phase: Optional[str] = None
    source_type: Optional[str] = None
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "data": dict(self.data)}
        if self.phase is not None:
            payload["phase"] = self.phase
        if self.source_type is not None:
            payload["source_type"] = self.source_type
        if self.truncated:
            payload["truncated"] = True
        return payload


@dataclass(frozen=True)
class DshExecutionResult:
    """Settled outcome of one Headless process invocation."""

    session_id: Optional[str]
    final_text: Optional[str]
    turn_end_reason: Optional[str]
    exit_code: Optional[int]
    timed_out: bool
    cancelled: bool
    events: tuple[DshRuntimeEvent, ...]
    stderr_diagnostics: str = ""
    error: Optional[DshRuntimeError] = None
    events_are_provisional: bool = True

    @property
    def succeeded(self) -> bool:
        return (
            self.error is None
            and not self.timed_out
            and not self.cancelled
            and self.exit_code == 0
            and self.session_id is not None
            and self.final_text is not None
            and self.turn_end_reason == "completed"
        )
