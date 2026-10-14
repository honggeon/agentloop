from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.config import get_settings
from backend.app.routes import dashboard, dsh, dsh_acp, dsh_chat, judge, live, runs


settings = get_settings()
app = FastAPI(title="Auditrail")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(runs.router)
app.include_router(judge.router)
app.include_router(live.router)
app.include_router(dsh.router)
app.include_router(dsh_chat.router)
app.include_router(dsh_acp.router)
app.include_router(dashboard.router)
app.router.add_event_handler("shutdown", dsh_chat.shutdown_active_operations)
app.router.add_event_handler("shutdown", dsh_acp.shutdown_acp_runtime)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
