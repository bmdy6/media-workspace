"""Standalone application using the existing Media authentication and image workflow."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from media.core import image_jobs
from media.core.config import get_config
from media.database import init_db
from .api.auth_router import router as auth_router
from .api.admin_router import router as admin_router
from .api.chat_router import router as chat_router
from .api.image_router import router as image_router
from .api.capabilities_router import router as capabilities_router
from .api.voice_router import router as voice_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if len(get_config("SECRET_KEY", required=True)) < 32:
        raise RuntimeError("SECRET_KEY must contain at least 32 characters")
    init_db()
    image_jobs.start()
    try:
        yield
    finally:
        image_jobs.stop()


app = FastAPI(title="Media Workspace", version="0.1.0", lifespan=lifespan)
origins = [value.strip() for value in os.environ.get("MEDIA_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if value.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["Authorization", "Content-Type"])
for router in (auth_router, admin_router, chat_router, image_router, capabilities_router, voice_router):
    app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok", "image_jobs_ready": image_jobs.available()}


FRONTEND_DIST = Path(os.environ.get("MEDIA_FRONTEND_DIST", str(Path(__file__).resolve().parents[3] / "frontend" / "dist"))).resolve()
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "Unknown API route")
    target = (FRONTEND_DIST / path).resolve()
    if not target.is_relative_to(FRONTEND_DIST):
        raise HTTPException(404)
    if target.is_file():
        return FileResponse(target)
    index = FRONTEND_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    raise HTTPException(404, "Build the frontend or start the Vite development server")
