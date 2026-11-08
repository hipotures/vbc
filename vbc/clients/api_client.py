"""Thin HTTP client of the VBC API, shared by the one-shot commands and the TUI.

Imports only ``vbc.contracts`` (never server internals). Base URL from
``--api``, then ``VBC_API``, then ``http://127.0.0.1:8770``.
"""

from __future__ import annotations

import os
import re
import socket
import time
import uuid
from typing import List, Optional, Sequence, Type, TypeVar

import httpx
from pydantic import BaseModel

from vbc import contracts as dto

DEFAULT_API = "http://127.0.0.1:8770"
T = TypeVar("T", bound=BaseModel)


def resolve_api_url(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get("VBC_API") or DEFAULT_API).rstrip("/")


class ApiUnavailable(Exception):
    """The server could not be reached (connection refused, timeout, DNS).

    ``reason`` is a short human-readable cause for status lines.
    """

    def __init__(self, message: str, reason: str = "unreachable") -> None:
        super().__init__(message)
        self.reason = reason


def _reason(exc: httpx.TransportError) -> str:
    text = str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if "Connection refused" in text:
        return "connection refused"
    if "Name or service not known" in text or "nodename nor servname" in text:
        return "unknown host"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "connection closed by the server"
    if "No route to host" in text:
        return "no route to host"
    return re.sub(r"^\[Errno -?\d+\]\s*", "", text).strip().lower() or type(exc).__name__


class ApiError(Exception):
    """The server answered with an error status."""

    def __init__(self, status: int, problem: Optional[dto.Problem], text: str = "") -> None:
        self.status = status
        self.problem = problem
        title = problem.title if problem else (text or f"HTTP {status}")
        super().__init__(f"{status} {title}")


CLIENT_HEADERS = ("X-VBC-Client-Kind", "X-VBC-Client-Id", "X-VBC-Client-Host")


class ApiClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 10.0, kind: str = "cli") -> None:
        self.base_url = resolve_api_url(base_url)
        self._timeout = timeout
        self.kind = kind
        self.session_id = uuid.uuid4().hex[:8]
        headers = dict(zip(CLIENT_HEADERS, (kind, self.session_id, socket.gethostname())))
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, headers=headers)
        self.clock_offset_ms = 0  # server_time - local time, from the last response

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def server_now_ms(self) -> int:
        """Local clock corrected to the server's (for extrapolating anchors)."""
        return int(time.time() * 1000) + self.clock_offset_ms

    # ── transport ───────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, timeout: Optional[float] = None, **kwargs) -> httpx.Response:
        try:
            response = self._http.request(method, path, timeout=timeout or self._timeout, **kwargs)
        except httpx.TransportError as exc:
            raise ApiUnavailable(f"{self.base_url}: {exc}", _reason(exc)) from exc
        if response.status_code >= 400:
            problem = None
            try:
                problem = dto.Problem.model_validate(response.json())
            except Exception:  # noqa: BLE001 - non-Problem error body (e.g. FastAPI 422)
                pass
            raise ApiError(response.status_code, problem, response.text[:300])
        return response

    def _parse(self, model: Type[T], response: httpx.Response) -> T:
        value = model.model_validate(response.json())
        server_time = getattr(value, "server_time", None)
        if server_time is not None:
            self.clock_offset_ms = server_time - int(time.time() * 1000)
        return value

    # ── reads ───────────────────────────────────────────────────────────────

    def state(self, since: Optional[int] = None, wait: float = 0.0) -> Optional[dto.StateSnapshot]:
        """``None`` when a long-poll (since + wait) timed out without a change."""
        params = {}
        if since is not None and wait > 0:
            params = {"since": since, "wait": wait}
        response = self._request("GET", "/api/v1/state", params=params, timeout=wait + self._timeout)
        if response.status_code == 204:
            return None
        return self._parse(dto.StateSnapshot, response)

    def queue(self, offset: int = 0, limit: int = 100) -> dto.QueuePage:
        return self._parse(dto.QueuePage, self._request("GET", "/api/v1/queue", params={"offset": offset, "limit": limit}))

    def recent_jobs(self, limit: int = 100) -> dto.RecentJobsPage:
        return self._parse(dto.RecentJobsPage, self._request("GET", "/api/v1/jobs/recent", params={"limit": limit}))

    def errors(self, since: int = 0, limit: int = 200) -> dto.ErrorsPage:
        return self._parse(dto.ErrorsPage, self._request("GET", "/api/v1/errors", params={"since": since, "limit": limit}))

    def logs(self, since: int = 0, limit: int = 500, level: Optional[str] = None, wait: float = 0.0) -> dto.LogsPage:
        params = {"since": since, "limit": limit, "wait": wait}
        if level:
            params["level"] = level.upper()
        response = self._request("GET", "/api/v1/logs", params=params, timeout=wait + self._timeout)
        return self._parse(dto.LogsPage, response)

    def config(self) -> dto.ConfigView:
        return self._parse(dto.ConfigView, self._request("GET", "/api/v1/config"))

    def io(self) -> dto.IoView:
        return self._parse(dto.IoView, self._request("GET", "/api/v1/io"))

    def dirs(self) -> dto.DirsState:
        return self._parse(dto.DirsState, self._request("GET", "/api/v1/dirs"))

    def gpu(self, since: int = 0) -> dto.GpuPage:
        return self._parse(dto.GpuPage, self._request("GET", "/api/v1/gpu", params={"since": since}))

    # ── commands ────────────────────────────────────────────────────────────

    def put_dirs(self, entries: Sequence[dto.DirEntry], dirs_rev: int) -> dto.DirsState:
        body = dto.PutDirsRequest(entries=list(entries)).model_dump()
        response = self._request("PUT", "/api/v1/dirs", json=body, headers={"If-Match": f'"{dirs_rev}"'})
        return self._parse(dto.DirsState, response)

    def scan_dirs(self) -> dto.DirsScanResult:
        """Offer new media mounts on the server as disabled folders."""
        return self._parse(dto.DirsScanResult, self._request("POST", "/api/v1/dirs/scan"))

    def set_threads(self, target: int) -> dto.Threads:
        response = self._request("PUT", "/api/v1/threads", json={"target": target})
        return self._parse(dto.ThreadsResult, response).threads

    def refresh(self) -> dto.Ack:
        return self._parse(dto.Ack, self._request("POST", "/api/v1/refresh"))

    def stop(self, mode: str = "graceful") -> dto.Ack:
        """Stop processing; the server keeps running (refresh resumes)."""
        return self._parse(dto.Ack, self._request("POST", "/api/v1/stop", json={"mode": mode}))

    def cancel_stop(self) -> dto.Ack:
        return self._parse(dto.Ack, self._request("DELETE", "/api/v1/stop"))

    def goodbye(self) -> None:
        """Tell the server this session ends (best effort, never raises)."""
        try:
            self._http.post("/api/v1/clients/goodbye", timeout=1.0)
        except httpx.HTTPError:
            pass

    def set_log_level(self, level: str) -> dto.Ack:
        return self._parse(dto.Ack, self._request("PUT", "/api/v1/runtime/log-level", json={"level": level.upper()}))


def extrapolated_percent(progress: dto.ProgressAnchor, now_ms: int) -> float:
    """Dead reckoning: the client-side formula from the plan."""
    return min(progress.percent + progress.rate * (now_ms - progress.at) / 1000, 99.5)


def replace_dirs(current: List[dto.DirStatus]) -> List[dto.DirEntry]:
    return [
        dto.DirEntry(path=d.path, enabled=d.enabled, metadata=d.metadata, watch=d.watch,
                     watch_mode=d.watch_mode, poll_interval_seconds=d.poll_interval_seconds)
        for d in current
    ]
