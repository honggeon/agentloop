from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    runs_dir: Path = PROJECT_ROOT / "runs"
    backend_host: str = "0.0.0.0"
    backend_port: int = 18080
    cors_origins: list[str] | None = None
    dsh_home: Path = Path("~/.dsh").expanduser()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    origins = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3080,http://127.0.0.1:3080",
    )
    return Settings(
        runs_dir=Path(os.getenv("AGENTLOOP_RUNS_DIR", str(PROJECT_ROOT / "runs"))).expanduser(),
        backend_host=os.getenv("BACKEND_HOST", "0.0.0.0"),
        backend_port=int(os.getenv("BACKEND_PORT", "18080")),
        cors_origins=[item.strip() for item in origins.split(",") if item.strip()],
        dsh_home=Path(os.getenv("DSH_HOME", "~/.dsh")).expanduser(),
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

