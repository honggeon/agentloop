from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from auditrail.dsh_runtime import DshRuntimeConfig, DshValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
#: Set to a falsey value to skip ``.env`` loading. The test suite sets this so a
#: developer's local ``.env`` cannot steer a test run (or leak its credentials).
DOTENV_ENABLED_VAR = "AUDITRAIL_LOAD_DOTENV"

_FALSEY = {"0", "false", "no", "off", ""}
_dotenv_loaded = False


def load_env_file() -> bool:
    """Load the project ``.env`` at most once, unless explicitly disabled.

    Real environment variables keep precedence over the file, so an operator can
    always override a local default without editing ``.env``. An unrecognized
    guard value leaves loading enabled rather than breaking startup, and a guard
    lifted later still lets a caller retry the load.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return True
    if str(os.getenv(DOTENV_ENABLED_VAR, "true")).strip().lower() in _FALSEY:
        return False
    load_dotenv(ENV_FILE)
    _dotenv_loaded = True
    return True


load_env_file()


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
    #: The ACP interactive runtime is a separate, opt-in capability layered on
    #: top of ``dsh_runtime_enabled``. Both must be true for any ACP route to be
    #: reachable, so enabling the master switch alone never exposes ACP.
    dsh_acp_enabled: bool = False
    #: Phase 0 platform slice: Agent/TestCase config store (JSON files, no DB).
    platform_dir: Path = PROJECT_ROOT / "platform"
    #: SSRF policy for Agent endpoints. Hosts in ``agent_allowed_hosts`` are
    #: explicitly trusted (still scheme/port checked); otherwise private,
    #: loopback, link-local and metadata addresses are rejected unless
    #: ``agent_allow_private_endpoints`` is enabled.
    agent_allowed_hosts: tuple[str, ...] = ()
    agent_allow_private_endpoints: bool = False
    agent_allowed_ports: tuple[int, ...] = (80, 443)
    #: Phase 1.1 batch reliability: static concurrency ceilings. The global cap
    #: bounds concurrent case Runs across all SuiteJobs; the per-agent cap is
    #: the default for Agents without their own ``max_concurrency``. Both are
    #: fixed at startup — no dynamic autoscaling.
    platform_max_concurrency: int = 4
    platform_agent_max_concurrency: int = 1
    #: Phase 2 local admin endpoints (run-index rebuild/status). There is no
    #: production auth in this deployment; these endpoints are local-only
    #: management capabilities and say so in every response.
    local_admin_enabled: bool = True
    #: Phase 3 run-index watcher: an opt-in, debounced *polling* refresher
    #: (no OS watcher dependency in this environment). Disabled by default;
    #: failures degrade the watcher, never the main API.
    run_index_watcher_enabled: bool = False
    run_index_debounce_ms: int = 500
    run_index_max_batch: int = 1000
    run_index_poll_interval_ms: int = 2000
    #: Phase 4 local permission boundary. ``local`` (default): one configured
    #: identity (``auth_role``/``auth_actor``) authorizes requests;
    #: ``disabled`` allows everything and is a local-development-only mode
    #: that must be surfaced in API responses and the UI. Phase 5 adds the
    #: production modes ``oidc`` and ``jwt`` (server-verified Bearer tokens).
    auth_mode: str = "local"
    auth_role: str = "admin"
    auth_actor: str = ""
    #: Phase 4 ImportJob worker bounds (in-process, file-persisted).
    import_worker_enabled: bool = True
    import_lease_seconds: int = 60
    import_checkpoint_records: int = 100
    import_max_retries: int = 5

    # ------------------------------------------------------------------
    # Phase 5 production architecture (see docs/phase5-production-architecture.md)
    # ------------------------------------------------------------------
    #: Deployment shape. ``local``: developer machine, everything optional.
    #: ``single_process``: one host, API + worker processes, database storage.
    #: ``production``: multi-instance; requires database storage, a real auth
    #: mode and (for multiple instances) the redis queue mode.
    deployment_mode: str = "local"
    #: Platform metadata store. ``file``: Phase 0-4 JSON files (default).
    #: ``database``: the database is authoritative; files stay read-only
    #: compatibility. ``dual``: database is the write master, files are
    #: mirrored; reads prefer the database and degrade to files explicitly.
    storage_mode: str = "file"
    #: ImportJob execution infrastructure. ``inline``: in-process pool
    #: (single process, no cross-process claims). ``local_worker``: workers
    #: claim jobs with database transactions (cross-process, single host).
    #: ``redis``: database stays the source of truth; a Redis list is only a
    #: delivery hint on top of the database claim.
    queue_mode: str = "inline"
    #: ``sqlite:///path`` (embedded default) or ``postgresql://...`` conninfo
    #: (psycopg; TLS via sslmode parameters in the URL). Never logged.
    database_url: str = ""
    redis_url: str = ""
    #: OIDC/JWT verification settings (auth_mode=oidc|jwt). Secrets come from
    #: the environment only and never enter logs, audit events or responses.
    auth_required: bool = True
    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_audience: str = ""
    oidc_scopes: str = "openid,profile,email"
    oidc_redirect_uri: str = ""
    oidc_jwks_cache_seconds: int = 3600
    #: Provider unreachable: cached JWKS may keep validating tokens for this
    #: long after its last successful fetch; beyond it verification fails
    #: closed (expired keys are never trusted indefinitely).
    oidc_jwks_max_stale_seconds: int = 86400
    oidc_clock_skew_seconds: int = 60
    oidc_roles_claim: str = "roles"
    #: HS256 shared secret for auth_mode=jwt (local/self-issued tokens). Empty
    #: means jwt mode requires a JWKS-capable issuer just like oidc.
    jwt_secret: str = ""
    jwt_algorithms: tuple[str, ...] = ("RS256", "HS256")
    #: Single-tenant boundary: the tenant id is server-owned configuration,
    #: never accepted from requests. Multi-tenant deployment is not claimed.
    tenant_id: str = "default"
    #: Cross-process worker tuning (queue_mode=local_worker|redis).
    worker_poll_interval_ms: int = 1000
    worker_heartbeat_seconds: int = 15
    #: Backup output root for the admin backup endpoint (CLI --output is
    #: independent). Never contains secrets (see auditrail.platform_backup).
    backup_dir: Path | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Retry the load here so ``get_settings()`` still sees ``.env`` when the
    # environment guard was lifted after this module was first imported.
    load_env_file()
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
        dsh_acp_enabled=_env_bool("DSH_ACP_ENABLED", False),
        platform_dir=Path(os.getenv("AUDITRAIL_PLATFORM_DIR", str(PROJECT_ROOT / "platform"))).expanduser(),
        agent_allowed_hosts=_csv_values(os.getenv("AGENT_ALLOWED_HOSTS", "")),
        agent_allow_private_endpoints=_env_bool("AGENT_ALLOW_PRIVATE_ENDPOINTS", False),
        agent_allowed_ports=_port_values(os.getenv("AGENT_ALLOWED_PORTS", "80,443")),
        platform_max_concurrency=_positive_int("AUDITRAIL_PLATFORM_MAX_CONCURRENCY", 4),
        platform_agent_max_concurrency=_positive_int("AUDITRAIL_PLATFORM_AGENT_MAX_CONCURRENCY", 1),
        local_admin_enabled=_env_bool("AUDITRAIL_LOCAL_ADMIN", True),
        run_index_watcher_enabled=_env_bool("AUDITRAIL_RUN_INDEX_WATCHER", False),
        run_index_debounce_ms=_positive_int("AUDITRAIL_RUN_INDEX_DEBOUNCE_MS", 500),
        run_index_max_batch=_positive_int("AUDITRAIL_RUN_INDEX_MAX_BATCH", 1000),
        run_index_poll_interval_ms=_positive_int("AUDITRAIL_RUN_INDEX_POLL_INTERVAL_MS", 2000),
        auth_mode=_env_choice("AUDITRAIL_AUTH_MODE", "local", ("local", "disabled", "oidc", "jwt")),
        auth_role=_env_choice("AUDITRAIL_AUTH_ROLE", "admin", ("viewer", "operator", "admin", "owner")),
        auth_actor=os.getenv("AUDITRAIL_AUTH_ACTOR", "").strip(),
        import_worker_enabled=_env_bool("AUDITRAIL_IMPORT_WORKER", True),
        import_lease_seconds=_positive_int("AUDITRAIL_IMPORT_LEASE_SECONDS", 60),
        import_checkpoint_records=_positive_int("AUDITRAIL_IMPORT_CHECKPOINT_RECORDS", 100),
        import_max_retries=_positive_int("AUDITRAIL_IMPORT_MAX_RETRIES", 5),
        deployment_mode=_env_choice(
            "AUDITRAIL_DEPLOYMENT_MODE", "local", ("local", "single_process", "production")
        ),
        storage_mode=_env_choice("AUDITRAIL_STORAGE_MODE", "file", ("file", "database", "dual")),
        queue_mode=_env_choice("AUDITRAIL_QUEUE_MODE", "inline", ("inline", "local_worker", "redis")),
        database_url=os.getenv("AUDITRAIL_DATABASE_URL", "").strip(),
        redis_url=os.getenv("AUDITRAIL_REDIS_URL", "").strip(),
        auth_required=_env_bool("AUDITRAIL_AUTH_REQUIRED", True),
        oidc_issuer_url=os.getenv("AUDITRAIL_OIDC_ISSUER_URL", "").strip(),
        oidc_client_id=os.getenv("AUDITRAIL_OIDC_CLIENT_ID", "").strip(),
        oidc_client_secret=os.getenv("AUDITRAIL_OIDC_CLIENT_SECRET", "").strip(),
        oidc_audience=os.getenv("AUDITRAIL_OIDC_AUDIENCE", "").strip(),
        oidc_scopes=os.getenv("AUDITRAIL_OIDC_SCOPES", "openid,profile,email").strip(),
        oidc_redirect_uri=os.getenv("AUDITRAIL_OIDC_REDIRECT_URI", "").strip(),
        oidc_jwks_cache_seconds=_positive_int("AUDITRAIL_OIDC_JWKS_CACHE_SECONDS", 3600),
        oidc_jwks_max_stale_seconds=_positive_int("AUDITRAIL_OIDC_JWKS_MAX_STALE_SECONDS", 86400),
        oidc_clock_skew_seconds=int(os.getenv("AUDITRAIL_OIDC_CLOCK_SKEW_SECONDS", "60")),
        oidc_roles_claim=os.getenv("AUDITRAIL_OIDC_ROLES_CLAIM", "roles").strip() or "roles",
        jwt_secret=os.getenv("AUDITRAIL_JWT_SECRET", "").strip(),
        jwt_algorithms=_csv_values(os.getenv("AUDITRAIL_JWT_ALGORITHMS", "RS256,HS256")) or ("RS256", "HS256"),
        tenant_id=os.getenv("AUDITRAIL_TENANT_ID", "default").strip() or "default",
        worker_poll_interval_ms=_positive_int("AUDITRAIL_WORKER_POLL_INTERVAL_MS", 1000),
        worker_heartbeat_seconds=_positive_int("AUDITRAIL_WORKER_HEARTBEAT_SECONDS", 15),
        backup_dir=_optional_path(os.getenv("AUDITRAIL_BACKUP_DIR")),
    )


def resolved_database_url(settings: Settings | None = None) -> str:
    """The effective database conninfo. When unset, storage modes that need a
    database default to an embedded SQLite file under the platform directory
    (single-host; PostgreSQL is required for production multi-instance)."""
    current = settings or get_settings()
    url = str(current.database_url or "").strip()
    if url:
        return url
    return "sqlite:///%s" % (Path(current.platform_dir) / "auditrail.db")


def validate_startup_settings(settings: Settings | None = None) -> None:
    """Fail-fast startup validation. A production-shaped deployment with an
    impossible combination (e.g. ``production + disabled auth``) must refuse
    to start instead of silently running unauthenticated."""
    current = settings or get_settings()
    errors: list[str] = []
    if current.deployment_mode == "production":
        if current.auth_mode == "disabled":
            errors.append("production deployment requires a real auth mode (not AUDITRAIL_AUTH_MODE=disabled)")
        if current.storage_mode == "file":
            errors.append("production deployment requires AUDITRAIL_STORAGE_MODE=database|dual")
        if resolved_database_url(current).startswith("sqlite:"):
            errors.append("production deployment requires PostgreSQL (SQLite is single-host only)")
    if current.auth_mode in ("oidc", "jwt"):
        if not current.oidc_issuer_url and not current.jwt_secret:
            errors.append(
                "auth_mode=%s requires AUDITRAIL_OIDC_ISSUER_URL or AUDITRAIL_JWT_SECRET" % current.auth_mode
            )
        if current.auth_mode == "oidc" and not current.oidc_issuer_url:
            errors.append("auth_mode=oidc requires AUDITRAIL_OIDC_ISSUER_URL")
    if current.queue_mode in ("local_worker", "redis") and current.storage_mode == "file":
        errors.append("queue_mode=%s requires AUDITRAIL_STORAGE_MODE=database|dual" % current.queue_mode)
    if current.queue_mode == "redis" and not current.redis_url:
        errors.append("queue_mode=redis requires AUDITRAIL_REDIS_URL")
    if errors:
        raise ValueError("invalid startup configuration: %s" % "; ".join(errors))


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


def _port_values(value: str) -> tuple[int, ...]:
    ports = []
    for item in _csv_values(value):
        port = int(item)
        if not (1 <= port <= 65535):
            raise ValueError("AGENT_ALLOWED_PORTS entries must be 1-65535")
        ports.append(port)
    return tuple(sorted(set(ports)))


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


def _env_choice(name: str, default: str, choices: tuple) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized not in choices:
        raise ValueError("%s must be one of %s" % (name, list(choices)))
    return normalized


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError("%s must be positive" % name)
    return value


def build_endpoint_policy(settings: Settings | None = None) -> EndpointPolicy:
    """Build the SSRF endpoint policy from server-owned settings."""
    from auditrail.netguard import EndpointPolicy

    current = settings or get_settings()
    return EndpointPolicy(
        allowed_hosts=tuple(host.lower() for host in current.agent_allowed_hosts),
        allow_private=current.agent_allow_private_endpoints,
        allowed_ports=current.agent_allowed_ports,
    )


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

