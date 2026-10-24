"""HTTP client for the backend's internal worker API (see docs/WORKER_PROTOCOL.md)."""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, Optional

import requests

log = logging.getLogger("judge.client")
API = "/internal/worker/v1"


class ApiError(Exception):
    """Base class."""


class ApiTransient(ApiError):
    """Network trouble or a 5xx answer: retry later."""


class ApiAuthError(ApiError):
    """401/403: the token is wrong or revoked. Retrying will not help."""


class ApiConflict(ApiError):
    """409: the worker was superseded, or the lease on the judgement is gone."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class ApiNotFound(ApiError):
    pass


class ApiClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base = base_url.rstrip("/") + API
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.worker_id: Optional[int] = None
        self.instance: Optional[str] = None

    # ------------------------------------------------------------------ plumbing
    def _headers(self) -> dict:
        if self.worker_id is None:
            return {}
        return {"X-Worker-Id": str(self.worker_id), "X-Worker-Instance": self.instance or ""}

    def _check(self, response: requests.Response) -> requests.Response:
        code = response.status_code
        if code < 300:
            return response
        try:
            body = response.json()
            detail = body.get("detail", body)
        except ValueError:
            detail = response.text[:300]
        message = f"{response.request.method} {response.request.path_url}: {code} {detail}"
        if code in (401, 403):
            raise ApiAuthError(message)
        if code == 404:
            raise ApiNotFound(message)
        if code == 409:
            raise ApiConflict(message, code=(detail.get("code", "") if isinstance(detail, dict) else ""))
        if code >= 500 or code in (408, 429):
            raise ApiTransient(message)
        raise ApiError(message)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = self.session.request(method, self.base + path, headers=self._headers(),
                                            timeout=kwargs.pop("timeout", self.timeout), **kwargs)
        except requests.RequestException as exc:
            raise ApiTransient(f"{method} {path}: {exc}") from exc
        return self._check(response)

    def _json(self, method: str, path: str, **kwargs) -> dict:
        return self._request(method, path, **kwargs).json()

    # ------------------------------------------------------------------ protocol
    def register(self, name: str, capabilities: list, info: dict) -> dict:
        data = self._json("POST", "/register", json={"name": name, "capabilities": capabilities, "info": info})
        self.worker_id, self.instance = data["worker_id"], data["instance"]
        return data

    def deregister(self) -> None:
        self._json("POST", "/deregister")

    def claim(self) -> Optional[dict]:
        return self._json("POST", "/claim").get("job")

    def heartbeat(self, state: str, judgement_id: Optional[int], attempt: Optional[int],
                  phase: Optional[str], done: Optional[int], total: Optional[int], stats: dict) -> dict:
        return self._json("POST", "/heartbeat", json={
            "state": state, "judgement_id": judgement_id, "attempt": attempt,
            "phase": phase, "tests_done": done, "tests_total": total, "stats": stats})

    def manifest(self, version_id: int) -> dict:
        return self._json("GET", f"/problem-versions/{version_id}/manifest")

    def download_blob(self, sha256: str, write: Callable[[bytes], None]) -> None:
        try:
            with self.session.get(f"{self.base}/blobs/{sha256}", headers=self._headers(), stream=True,
                                  timeout=self.timeout) as response:
                self._check(response)
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        write(chunk)
        except requests.RequestException as exc:
            raise ApiTransient(f"GET blob {sha256[:12]}: {exc}") from exc

    def blob_bytes(self, sha256: str) -> bytes:
        parts: list = []
        self.download_blob(sha256, parts.append)
        return b"".join(parts)

    def release(self, judgement_id: int, attempt: int, reason: str) -> None:
        self._json("POST", f"/judgements/{judgement_id}/release", json={"attempt": attempt, "reason": reason})

    def submit_result(self, judgement_id: int, payload: dict, retry_for_s: float = 600.0,
                      sleep: Callable[[float], None] = time.sleep) -> dict:
        """Deliver a finished result. Transient errors are retried with backoff (the work is expensive).
        409 and auth errors are not retried."""
        deadline = time.monotonic() + retry_for_s
        delay = 1.0
        while True:
            try:
                return self._json("POST", f"/judgements/{judgement_id}/result", json=payload, timeout=60)
            except ApiTransient as exc:
                if time.monotonic() + delay > deadline:
                    raise
                log.warning("result of judgement %s not delivered (%s); retry in %.0fs", judgement_id, exc, delay)
                sleep(delay + random.random())
                delay = min(delay * 2, 30.0)
