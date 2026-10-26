"""The internal app: worker API, health, metrics.

    uvicorn app.internal_main:app --app-dir backend --host 127.0.0.1 --port 8001

It listens on localhost only and must never be published through the Caddy reverse proxy (the public
app of module 4 runs on another port). As a second line of defence, requests carrying any header that a
reverse proxy or Cloudflare adds are refused: a worker talks to this port directly and never sends them.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from . import queue as q
from .blobstore import BlobStore
from .db import get_sessionmaker
from .metrics import render_prometheus
from .settings import Settings
from .worker_api import get_session, router

# Set by Caddy (X-Forwarded-For/-Proto/-Host), by other proxies (Forwarded, X-Real-IP) and by Cloudflare.
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip",
                 "cf-connecting-ip", "cf-ray")


def create_internal_app(settings: Optional[Settings] = None, session_factory=None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="judge internal API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.blobs = BlobStore(settings.blob_dir)
    app.state.session_factory = session_factory or get_sessionmaker()

    @app.middleware("http")
    async def refuse_proxied_traffic(request: Request, call_next):
        if any(h in request.headers for h in PROXY_HEADERS):
            return JSONResponse({"detail": "internal API is not available through a proxy"}, status_code=403)
        return await call_next(request)

    def metrics_auth(request: Request, authorization: Optional[str] = Header(None)) -> None:
        wanted = request.app.state.settings.metrics_token
        if wanted and authorization != f"Bearer {wanted}":
            raise HTTPException(401, "metrics token required")

    @app.get("/internal/health")
    def health(session=Depends(get_session)):
        session.execute(text("SELECT 1"))
        return {"ok": True}

    @app.get("/internal/metrics", response_class=PlainTextResponse, dependencies=[Depends(metrics_auth)])
    def metrics(session=Depends(get_session)):
        q.read_one_moment(session)
        return render_prometheus(q.collect_metrics(session, settings.queue))

    @app.get("/internal/status", dependencies=[Depends(metrics_auth)])
    def status(session=Depends(get_session)):
        q.read_one_moment(session)
        snap = q.load_snapshot(session, settings.queue)
        return {"load": snap.to_dict(), "metrics": q.collect_metrics(session, settings.queue),
                "workers": q.worker_table(session, settings.queue)}

    app.include_router(router)
    return app


def __getattr__(name: str):  # `uvicorn app.internal_main:app` creates the app lazily
    if name == "app":
        globals()["app"] = create_internal_app()
        return globals()["app"]
    raise AttributeError(name)
