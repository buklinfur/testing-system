"""Request body limits of the public app (docs/ADMIN_API.md, "Тело запроса").

Uvicorn does not limit request bodies, so:
* BodyLimitMiddleware reads the body of every request itself, up to `limit` bytes, before the app sees it: a larger
  Content-Length is refused at once (nothing is read), a chunked body is counted and refused as soon as it passes the
  limit (the rest is never read). The app then gets the body replayed from memory (at most `limit` bytes).
* Upload routes (raw zip or CSV) are exempt; they call save_body, which streams into a file with its own limit.
"""
from __future__ import annotations

import json
from typing import BinaryIO, Iterable

from starlette.requests import Request

from .api_errors import ApiError, error_body

JSON_LIMIT = 1024 * 1024


def _too_large(limit: int) -> ApiError:
    return ApiError(413, "too_large", f"the request body is larger than {limit // 1024} KB"
                    if limit < 1024 * 1024 else f"the request body is larger than {limit // (1024 * 1024)} MB")


def declared_length(headers: Iterable) -> int | None:
    for name, value in headers:
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodyLimitMiddleware:
    def __init__(self, app, limit: int = JSON_LIMIT, exempt: frozenset = frozenset()):
        """exempt: (method, path) pairs that read their body themselves (uploads)."""
        self.app, self.limit, self.exempt = app, limit, exempt

    async def _refuse(self, send) -> None:
        err = _too_large(self.limit)
        body = json.dumps(error_body(err.code, err.message)).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store"), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or (scope["method"], scope["path"]) in self.exempt:
            await self.app(scope, receive, send)
            return
        length = declared_length(scope.get("headers", []))
        if length is not None and length > self.limit:
            await self._refuse(send)
            return
        chunks, total, more = [], 0, True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.limit:
                await self._refuse(send)
                return
            chunks.append(chunk)
            more = message.get("more_body", False)
        body, sent = b"".join(chunks), False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()  # after the body: only a disconnect can come

        await self.app(scope, replay, send)


async def save_body(request: Request, dest: BinaryIO, limit: int) -> int:
    """Stream the request body into `dest`; ApiError 413 as soon as it is larger than `limit` (by Content-Length
    before reading anything, else by counting). Returns the number of bytes written."""
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > limit:
        raise _too_large(limit)
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _too_large(limit)
        dest.write(chunk)
    dest.flush()
    return total


def require_type(request: Request, *allowed: str) -> None:
    """415 unless the Content-Type (without parameters) is one of `allowed`."""
    kind = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if kind not in allowed:
        raise ApiError(415, "unsupported_media_type", f"send the body as {' or '.join(allowed)}")
