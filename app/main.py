"""FastAPI application: web UI (/), JSON API (/api, docs at /docs)."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR
from .db import SessionLocal, init_db
from .knowledge.base import ensure_sources
from .web import routes_api, routes_ui


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with SessionLocal() as db:
        ensure_sources(db)
    yield


app = FastAPI(
    title="TMD CDE Platform",
    description="AI-assisted, human-in-the-loop Common Data Element development for TMD research "
                "(knowledge sources -> agents -> two-layer CDE repository).",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "web" / "static")), name="static")
app.include_router(routes_api.router)
app.include_router(routes_ui.router)
