from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from auditrail.dsh_runtime import DshRuntimeConfig, DshValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    runs_dir: Path = PROJECT_ROOT / "runs"
    backend_host: str = "0.0.0.0"
    backend_port: int = 18080
    frontend_port: int = 5111
    cors_origins: list[str] | None = None
    dsh_home: Path = Path("~/.dsh").expanduser()
    dsh_runtime_enabled: bool = False
    dsh_binary: Path | None = None
    dsh_allowed_profiles: tuple[str, ...] = ()
    dsh_allowed_workspace_roots: tuple[Path, ...] = ()
    dsh_default_timeout_seconds: int = 300
    dsh_max_timeout_seconds: int = 900
    dsh_max_concurrency: int = 1
    dsh_judge_profile: str = ""
    dsh_judge_workspace: Path | None = None
    dsh_acp_enabled: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    origins = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3080,http://127.0.0.1:3080",
    )
    return Settings(
        runs_dir=Path(os.getenv("AUDITRAIL_RUNS_DIR", str(PROJECT_ROOT / "runs"))).expanduser(),
        backend_host=os.getenv("BACKEND_HOST", "0.0.0.0"),
        backend_port=int(os.getenv("BACKEND_PORT", "18080")),
        frontend_port=int(os.getenv("FRONTEND_PORT", "5111")),
        cors_origins=[item.strip() for item in origins.split(",") if item.strip()],
        dsh_home=Path(os.getenv("DSH_HOME", "~/.dsh")).expanduser(),
        dsh_runtime_enabled=_env_bool("DSH_RUNTIME_ENABLED", False),
        dsh_binary=_optional_path(os.getenv("DSH_BINARY")),
        dsh_allowed_profiles=_csv_values(os.getenv("DSH_ALLOWED_PROFILES", "")),
        dsh_allowed_workspace_roots=tuple(
            Path(value).expanduser() for value in _csv_values(os.getenv("DSH_ALLOWED_WORKSPACE_ROOTS", ""))
        ),
        dsh_default_timeout_seconds=_positive_int("DSH_DEFAULT_TIMEOUT_SECONDS", 300),
        dsh_max_timeout_seconds=_positive_int("DSH_MAX_TIMEOUT_SECONDS", 900),
        dsh_max_concurrency=_positive_int("DSH_MAX_CONCURRENCY", 1),
        dsh_judge_profile=os.getenv("DSH_JUDGE_PROFILE", "").strip(),
        dsh_judge_workspace=_optional_path(os.getenv("DSH_JUDGE_WORKSPACE")),
        dsh_acp_enabled=_env_bool("DSH_ACP_ENABLED", True),
    )


def build_dsh_runtime_config(settings: Settings | None = None) -> DshRuntimeConfig:
    """Build the core policy only when local DSH execution is explicitly enabled.

    ``DSH_JUDGE_PROFILE`` is included as the dedicated judge profile so the
    trusted policy itself rejects judges that reuse an evaluand profile.
    """
    current = settings or get_settings()
    if not current.dsh_runtime_enabled:
        raise ValueError("local DSH runtime is disabled")
    if current.dsh_binary is None:
        raise ValueError("DSH_BINARY is required when local DSH runtime is enabled")
    if current.dsh_default_timeout_seconds > current.dsh_max_timeout_seconds:
        raise ValueError("DSH default timeout cannot exceed the maximum")
    try:
        return DshRuntimeConfig(
            binary=current.dsh_binary,
            allowed_profiles=current.dsh_allowed_profiles,
            allowed_workspace_roots=current.dsh_allowed_workspace_roots,
            judge_profile=current.dsh_judge_profile,
            default_timeout_seconds=current.dsh_default_timeout_seconds,
            max_timeout_seconds=current.dsh_max_timeout_seconds,
        )
    except DshValidationError as exc:
        raise ValueError(str(exc)) from exc


def build_dsh_judge_settings(settings: Settings | None = None) -> Settings:
    """Validate the server-owned DSH judge policy before any judge work starts.

    The judge needs an explicitly configured, dedicated profile and workspace so
    a judge session can never resume an evaluated session. Validation is
    centralized here; the engine re-checks the same invariants defensively.
    """
    current = settings or get_settings()
    build_dsh_runtime_config(current)
    if not current.dsh_judge_profile:
        raise ValueError("DSH_JUDGE_PROFILE is required to run the DSH judge")
    if current.dsh_judge_profile in current.dsh_allowed_profiles:
        raise ValueError("DSH judge profile must differ from every evaluand profile")
    if current.dsh_judge_workspace is None:
        raise ValueError("DSH_JUDGE_WORKSPACE is required to run the DSH judge")
    workspace = current.dsh_judge_workspace.expanduser()
    if not workspace.exists() or not workspace.is_dir():
        raise ValueError("configured DSH judge workspace does not exist")
    try:
        build_dsh_runtime_config(current).validate_workspace(workspace)
    except DshValidationError as exc:
        raise ValueError(str(exc)) from exc
    return current


def _csv_values(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _optional_path(value: str | None) -> Path | None:
    text = str(value or "").strip()
    return Path(text).expanduser() if text else None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("%s must be a boolean" % name)


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError("%s must be positive" % name)
    return value


def resolve_run_path(run_id: str, settings: Settings | None = None) -> Path:
    if "/" in run_id or "\\" in run_id or run_id in {"", ".", ".."}:
        raise ValueError("invalid run_id")
    current = settings or get_settings()
    root = current.runs_dir.resolve()
    path = (root / run_id).resolve()
    if root != path and root not in path.parents:
        raise ValueError("invalid run_id")
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError("missing run %s" % run_id)
    return path

