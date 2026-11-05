"""The public app: what students and admins reach through Caddy (R16, R17).

    PYTHONPATH=.:backend python -m app.public_main          # 127.0.0.1:$JUDGE_PUBLIC_PORT (default 8000)

It is a separate ASGI app on its own port: the internal worker API (app.internal_main, port 8001) is not
part of it, and Caddy proxies only this one. Startup is refused with a clear message unless
JUDGE_SECRET_KEY is a real Fernet key (not the placeholder of backend.env.example), JUDGE_PUBLIC_ORIGIN is
a bare https origin, and the public port differs from the internal one. Start it through this module, not
`uvicorn app.public_main:...`: that is what checks the port and turns off uvicorn's own proxy-header
handling (the app reads X-Forwarded-For itself, only from localhost: app.auth_api.client_ip).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import api_errors
from .admin_api import UPLOADS, router as admin_router
from .auth.totp import SecretBox, TotpConfigError
from .auth_api import SAFE_METHODS, router as auth_router
from .http_body import JSON_LIMIT, BodyLimitMiddleware
from .db import get_sessionmaker
from .settings import Settings

PLACEHOLDER_SECRET = "CHANGE_ME"  # the placeholder of JUDGE_SECRET_KEY and JUDGE_PUBLIC_ORIGIN in backend.env.example
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class PublicConfigError(RuntimeError):
    pass


def canonical_origin(raw: Optional[str]) -> str:
    """'https://Judge.Example.ru/' -> 'https://judge.example.ru', the form browsers send in Origin.

    Plain http is accepted only for a local origin (manual checks); a path, query or user part is an error.
    """
    if not raw:
        raise PublicConfigError("JUDGE_PUBLIC_ORIGIN is not set (e.g. https://judge.example.ru)")
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError as exc:
        raise PublicConfigError(f"JUDGE_PUBLIC_ORIGIN is not a valid origin: {raw!r}") from exc
    host = (parts.hostname or "").lower()
    if (parts.scheme not in ("https", "http") or not host or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise PublicConfigError(f"JUDGE_PUBLIC_ORIGIN must look like https://judge.example.ru, got {raw!r}")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        raise PublicConfigError(f"JUDGE_PUBLIC_ORIGIN must be https (http only for localhost), got {raw!r}")
    shown = f"[{host}]" if ":" in host else host
    default = {"https": 443, "http": 80}[parts.scheme]
    return f"{parts.scheme}://{shown}" + (f":{port}" if port not in (None, default) else "")


def check_config(settings: Settings) -> tuple[SecretBox, str]:
    """(the TOTP secret box, the canonical origin); PublicConfigError lists every problem at once."""
    problems, box, origin = [], None, None
    key = settings.auth.secret_key
    if not key or key.strip() == PLACEHOLDER_SECRET:
        problems.append("JUDGE_SECRET_KEY is not set or still the placeholder from backend.env.example; generate "
                        "one: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
    else:
        try:
            box = SecretBox(key)
        except TotpConfigError as exc:
            problems.append(str(exc))
    try:
        origin = canonical_origin(settings.public_origin)
        if PLACEHOLDER_SECRET.lower() in origin:  # https://CHANGE_ME.ru from backend.env.example
            problems.append(f"JUDGE_PUBLIC_ORIGIN is still the placeholder from backend.env.example ({origin}); "
                            "set it to the origin browsers use, e.g. https://judge.example.ru")
    except PublicConfigError as exc:
        problems.append(str(exc))
    for name, port in (("JUDGE_PUBLIC_PORT", settings.public_port), ("JUDGE_INTERNAL_PORT", settings.internal_port)):
        if not 1 <= port <= 65535:
            problems.append(f"{name}={port} is not a port")
    if settings.public_port == settings.internal_port:
        problems.append(f"JUDGE_PUBLIC_PORT equals the internal worker API port ({settings.internal_port}): "
                        "the worker API must never be reachable through Caddy")
    if problems:
        raise PublicConfigError("the public app cannot start:\n  - " + "\n  - ".join(problems))
    return box, origin


def create_public_app(settings: Optional[Settings] = None, session_factory=None,
                      clock: Optional[Callable[[], datetime]] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    box, origin = check_config(settings)
    app = FastAPI(title="judge", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.box = box
    app.state.origin = origin
    app.state.session_factory = session_factory or get_sessionmaker()
    app.state.clock = clock or (lambda: datetime.now(timezone.utc))

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        # R15: an unsafe request from another site (or without Origin at all) never reaches a handler
        if request.method not in SAFE_METHODS and request.headers.get("origin") != origin:
            return JSONResponse(api_errors.error_body("origin_not_allowed"), status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.add_middleware(BodyLimitMiddleware, limit=JSON_LIMIT, exempt=UPLOADS)  # outermost: before anything reads
    api_errors.install(app)
    app.include_router(auth_router)
    app.include_router(admin_router)
    return app


def main() -> int:
    settings = Settings.from_env()
    try:
        app = create_public_app(settings)
    except PublicConfigError as exc:
        print(f"judge public app: {exc}", file=sys.stderr)
        return 2
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=settings.public_port, proxy_headers=False, timeout_keep_alive=75)
    return 0


if __name__ == "__main__":
    sys.exit(main())
