"""One error format for the whole public app (/api/auth and /api/admin; docs/ADMIN_API.md):

    {"detail": "<code>", "message": "<text for a person>"?, "problems": ["...", ...]?}

`detail` is a stable snake_case code for programs; `message` and `problems` are optional. Errors of the request
body become 422 `invalid_request` with one problem per field; 404 and 405 of the router become `not_found` and
`method_not_allowed`.

A value the database refuses as data (sqlalchemy DataError: an int4 id out of range, a too long string, a number
beyond Numeric(8, 2)) is 422 `invalid_request` as well, never a 500 (D-088). It should never happen: every input is
bounded before a handler runs (`app.ids`, the rules of the services), so the handler logs it as an error of our code
- the method and the route template, never the values.
"""
from __future__ import annotations

import logging
import re
from http import HTTPStatus
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("judge.api")
OUT_OF_RANGE = "value out of range or too long"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: Optional[str] = None, problems: Optional[list] = None,
                 headers: Optional[dict] = None, **extra):
        super().__init__(code)
        self.status, self.code, self.message, self.problems = status, code, message, problems
        self.headers, self.extra = headers, extra


def error_body(code: str, message: Optional[str] = None, problems: Optional[list] = None, **extra) -> dict:
    body = {"detail": code}
    if message:
        body["message"] = message
    if problems:
        body["problems"] = list(problems)
    body.update(extra)
    return body


def _code(status: int, detail) -> str:
    """A code for an HTTPException: its own detail if it already is one, else the snake_case HTTP phrase."""
    if isinstance(detail, str) and re.fullmatch(r"[a-z][a-z0-9_]*", detail):
        return detail
    try:
        return HTTPStatus(status).phrase.lower().replace(" ", "_").replace("-", "_")
    except ValueError:
        return "error"


def _where(loc) -> str:
    return ".".join(str(part) for part in loc if part != "body") or "body"


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError):
        return JSONResponse(error_body(exc.code, exc.message, exc.problems, **exc.extra), status_code=exc.status,
                            headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        return JSONResponse(error_body(_code(exc.status_code, exc.detail)), status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError):
        problems = [f"{_where(e.get('loc', ()))}: {e.get('msg', 'invalid')}" for e in exc.errors()]
        return JSONResponse(error_body("invalid_request", problems=problems), status_code=422)

    @app.exception_handler(DataError)
    async def data_error(request: Request, _exc: DataError):
        # get_db has already rolled the transaction back (the exception went through its yield)
        route = request.scope.get("route")
        log.error("the database refused a value of %s %s: an input without bounds (D-088)", request.method,
                  getattr(route, "path", "?"))
        return JSONResponse(error_body("invalid_request", problems=[OUT_OF_RANGE]), status_code=422)
