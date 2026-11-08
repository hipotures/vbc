"""HTTP API of ``vbc serve`` (FastAPI + uvicorn).

Thin layer: translate HTTP <-> StateStore / CommandService / VbcServer.
Endpoints, status codes and semantics: docs/architecture/service-split.md
("API (v1)", "Deployment: HomeStack"). Plain HTTP, no authentication; a
Host/Origin allowlist stops browser pages from driving the API.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import shutil
import signal
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import Body, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from vbc import contracts as dto
from vbc.service.bootstrap import VbcServer
from vbc.service.commands import CommandError
from vbc.service.log_buffer import LogBuffer
from vbc.service.state_store import StateStore, to_ms

logger = logging.getLogger("vbc.server.api")

MAX_WAIT_S = 30.0
HEALTH_JSON = "application/health+json"


def _now_ms() -> int:
    return to_ms(time.time())


def _problem(status: int, title: str, detail: Optional[str] = None, errors=()) -> JSONResponse:
    body = dto.Problem(status=status, title=title, detail=detail, errors=list(errors))
    return JSONResponse(body.model_dump(), status_code=status, media_type="application/problem+json")


# ── change notification (bus thread -> event loop) ──────────────────────────


class _Notifier:
    """Wakes every long-poll waiter; ``fire`` is called via call_soon_threadsafe."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def fire(self) -> None:
        event, self._event = self._event, asyncio.Event()
        event.set()

    async def wait_until(self, predicate: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            event = self._event
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except asyncio.TimeoutError:
                return predicate()
        return True


def _threadsafe(loop: asyncio.AbstractEventLoop, fn: Callable[[], None]) -> Callable[[int], None]:
    def callback(_value: int) -> None:
        try:
            loop.call_soon_threadsafe(fn)
        except RuntimeError:  # loop closed during shutdown
            pass

    return callback


# ── Host / Origin allowlist ─────────────────────────────────────────────────


class HostPolicy:
    """IP literals always pass (DNS rebinding needs a name); names must be known."""

    def __init__(self, extra: list[str]) -> None:
        names = {"localhost", socket.gethostname(), socket.getfqdn()}
        names.update(extra)
        self.names = {n.lower().rstrip(".") for n in names if n}

    def allows(self, host: Optional[str]) -> bool:
        if not host:
            return False
        host = host.strip().lower().rstrip(".")
        if host.startswith("["):  # [v6]:port
            host = host[1:host.index("]")] if "]" in host else host
        elif host.count(":") == 1:
            host = host.split(":", 1)[0]
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return host in self.names

    def allows_origin(self, origin: Optional[str]) -> bool:
        if origin is None:
            return True
        if origin == "null":
            return False
        parts = urlsplit(origin)
        return bool(parts.hostname) and self.allows(parts.hostname)


# ── heartbeat + health (HomeStack contract) ─────────────────────────────────


class Heartbeat:
    """Every interval, one real request through the main serving path."""

    def __init__(self, url: str, interval_s: float) -> None:
        self.url = url
        self.interval_s = interval_s
        self.count = 0
        self._started = time.monotonic()
        self._last_ok: Optional[float] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="vbc-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _beat(self) -> None:
        try:
            response = httpx.get(self.url, timeout=self.interval_s, headers={"Host": "127.0.0.1"})
            if response.status_code == 200:
                self._last_ok = time.monotonic()
                self.count += 1
        except httpx.HTTPError:
            pass

    def _loop(self) -> None:
        self._stop.wait(min(1.0, self.interval_s))
        while not self._stop.is_set():
            self._beat()
            self._stop.wait(self.interval_s)

    @property
    def age_s(self) -> int:
        reference = self._last_ok if self._last_ok is not None else self._started
        return int(time.monotonic() - reference)

    @property
    def fresh(self) -> bool:
        return self.age_s <= 3 * self.interval_s


def _process_rss_bytes() -> Optional[int]:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _heartbeat_checks(beat: Heartbeat) -> dict:
    status = "pass" if beat.fresh else "fail"
    return {
        "heartbeat:age": [{"status": status, "observedValue": beat.age_s, "observedUnit": "s"}],
        "heartbeat:count": [{"status": status, "observedValue": beat.count}],
    }


# ── application ─────────────────────────────────────────────────────────────


def create_app(server: VbcServer, *, heartbeat_url: Optional[str] = None) -> FastAPI:
    store: StateStore = server.store
    logs: LogBuffer = server.log_buffer
    commands = server.commands
    policy = HostPolicy(server.config.server.allowed_hosts)
    interval = float(os.environ.get("HOMESTACK_HEARTBEAT_SECONDS") or 60)
    beat = Heartbeat(heartbeat_url or f"http://127.0.0.1:{server.config.server.port}/api/v1/state", interval)
    state_notifier: dict[str, _Notifier] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        state_notifier["state"] = _Notifier()
        state_notifier["logs"] = _Notifier()
        store.add_listener(_threadsafe(loop, state_notifier["state"].fire))
        logs.add_listener(_threadsafe(loop, state_notifier["logs"].fire))
        beat.start()
        yield
        beat.stop()
        for notifier in state_notifier.values():
            notifier.fire()

    app = FastAPI(
        title="VBC API",
        version=str(dto.SCHEMA_VERSION),
        description="Control plane of vbc serve: status and commands, never media.",
        lifespan=lifespan,
    )
    app.state.heartbeat = beat

    @app.middleware("http")
    async def host_origin_guard(request: Request, call_next):
        if not policy.allows(request.headers.get("host")):
            return _problem(403, "host not allowed", detail=request.headers.get("host"))
        if not policy.allows_origin(request.headers.get("origin")):
            return _problem(403, "origin not allowed", detail=request.headers.get("origin"))
        return await call_next(request)

    @app.exception_handler(CommandError)
    async def command_error(_request: Request, exc: CommandError):
        return JSONResponse(exc.problem.model_dump(), status_code=exc.problem.status,
                            media_type="application/problem+json")

    def envelope() -> dict:
        return {"rev": store.rev, "server_time": _now_ms()}

    # ── reads ───────────────────────────────────────────────────────────────

    @app.get("/api/v1/state", response_model=dto.StateSnapshot,
             responses={204: {"description": "long-poll timeout, nothing changed"},
                        304: {"description": "If-None-Match matches the current rev"}})
    async def get_state(
        since: Optional[int] = Query(None, ge=0),
        wait: float = Query(0.0, ge=0.0, le=MAX_WAIT_S),
        if_none_match: Optional[str] = Header(None),
    ):
        if since is not None and wait > 0:
            changed = await state_notifier["state"].wait_until(lambda: store.rev > since, wait)
            if not changed:
                return Response(status_code=204)
        snapshot = store.snapshot()
        etag = f'"{snapshot.rev}"'
        if if_none_match is not None and if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(snapshot.model_dump(mode="json"), headers={"ETag": etag})

    @app.get("/api/v1/queue", response_model=dto.QueuePage)
    async def get_queue(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        return store.queue(offset, limit)

    @app.get("/api/v1/jobs/recent", response_model=dto.RecentJobsPage)
    async def get_recent(limit: int = Query(100, ge=1, le=500)):
        return store.recent_jobs(limit)

    @app.get("/api/v1/errors", response_model=dto.ErrorsPage)
    async def get_errors(since: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
        try:
            return store.errors(since, limit)
        except StateStore.ErrorsGone as exc:
            return _problem(410, "entries no longer retained", detail=str(exc))

    @app.get("/api/v1/logs", response_model=dto.LogsPage)
    async def get_logs(
        since: int = Query(0, ge=0),
        limit: int = Query(500, ge=1, le=5000),
        level: Optional[str] = Query(None, pattern="^(DEBUG|INFO|WARNING|ERROR)$"),
        wait: float = Query(0.0, ge=0.0, le=MAX_WAIT_S),
    ):
        if wait > 0:
            await state_notifier["logs"].wait_until(lambda: logs.last_seq > since, wait)
        try:
            records = logs.read(since, limit, level)
        except LogBuffer.Gone as exc:
            return _problem(410, "records no longer retained", detail=str(exc))
        return dto.LogsPage(**envelope(), last_seq=logs.last_seq,
                            entries=[dto.LogEntry(**r.__dict__) for r in records])

    @app.get("/api/v1/config", response_model=dto.ConfigView)
    async def get_config():
        snapshot = store.snapshot()
        return dto.ConfigView(rev=snapshot.rev, server_time=snapshot.server_time,
                              config_rev=snapshot.config_rev, sections=server.config_sections())

    @app.get("/api/v1/io", response_model=dto.IoView)
    async def get_io():
        config = server.config
        return dto.IoView(
            **envelope(),
            dirs=[dto.IoDir(**row) for row in server.io_dirs(store.pending_files())],
            suffix_output_dirs=config.suffix_output_dirs,
            suffix_errors_dirs=config.suffix_errors_dirs,
            log_path=config.general.log_path,
            queue_sort=config.general.queue_sort,
            queue_seed=config.general.queue_seed,
            storage_roots=list(config.storage.roots),
        )

    @app.get("/api/v1/dirs", response_model=dto.DirsState)
    async def get_dirs():
        state = commands.dirs()
        return JSONResponse(state.model_dump(mode="json"), headers={"ETag": f'"{state.dirs_rev}"'})

    @app.get("/api/v1/gpu", response_model=dto.GpuPage)
    async def get_gpu(since: int = Query(0, ge=0)):
        return store.gpu(since, enabled=server.gpu_enabled)

    # ── commands ────────────────────────────────────────────────────────────

    @app.put("/api/v1/dirs", response_model=dto.DirsState,
             responses={412: {"model": dto.Problem}, 422: {"model": dto.Problem}, 428: {"model": dto.Problem}})
    async def put_dirs(body: dto.PutDirsRequest, if_match: Optional[str] = Header(None)):
        rev = None
        if if_match is not None:
            try:
                raw = if_match.strip()
                raw = raw[2:] if raw.startswith("W/") else raw
                rev = int(raw.strip('"'))
            except ValueError:
                return _problem(412, "If-Match must be a dirs_rev number", detail=if_match)
        state = await asyncio.to_thread(commands.put_dirs, body.entries, rev)
        return JSONResponse(state.model_dump(mode="json"), headers={"ETag": f'"{state.dirs_rev}"'})

    @app.put("/api/v1/threads", response_model=dto.ThreadsResult)
    async def put_threads(body: dto.SetThreadsRequest):
        threads = await asyncio.to_thread(commands.set_threads, body.target)
        return dto.ThreadsResult(**envelope(), threads=threads)

    @app.post("/api/v1/refresh", response_model=dto.Ack, status_code=202)
    async def post_refresh():
        await asyncio.to_thread(commands.refresh)
        return dto.Ack(**envelope())

    @app.post("/api/v1/shutdown", response_model=dto.Ack, status_code=202)
    async def post_shutdown(body: dto.ShutdownRequest = Body(default_factory=dto.ShutdownRequest)):
        await asyncio.to_thread(server.request_stop, body.mode)
        return dto.Ack(**envelope())

    @app.delete("/api/v1/shutdown", response_model=dto.Ack, status_code=202)
    async def delete_shutdown():
        await asyncio.to_thread(server.cancel_stop)
        return dto.Ack(**envelope())

    @app.put("/api/v1/runtime/log-level", response_model=dto.Ack)
    async def put_log_level(body: dto.LogLevelRequest):
        commands.set_log_level(body.level)
        return dto.Ack(**envelope())

    # ── health + telemetry (HomeStack) ──────────────────────────────────────

    @app.get("/health/live")
    async def health_live():
        ok = beat.fresh
        return JSONResponse({"status": "pass" if ok else "fail", "checks": _heartbeat_checks(beat)},
                            status_code=200 if ok else 503, media_type=HEALTH_JSON)

    @app.get("/health/ready")
    async def health_ready():
        checks = _heartbeat_checks(beat)
        ok = beat.fresh
        for tool in ("ffmpeg", "ffprobe", "exiftool"):
            present = shutil.which(tool) is not None
            ok &= present
            checks[f"{tool}:executable"] = [{"status": "pass" if present else "fail"}]
        for root in server.config.storage.roots:
            usable = os.path.isdir(root) and os.access(root, os.W_OK | os.X_OK)
            ok &= usable
            checks[f"storage:{root}"] = [{"status": "pass" if usable else "fail"}]
        snapshot_status = store.snapshot().run_status
        orchestrator_ok = server.orchestrator_alive and snapshot_status != "failed"
        ok &= orchestrator_ok
        checks["orchestrator:thread"] = [{"status": "pass" if orchestrator_ok else "fail"}]
        return JSONResponse({"status": "pass" if ok else "fail", "checks": checks},
                            status_code=200 if ok else 503, media_type=HEALTH_JSON)

    @app.get("/.well-known/homestack/status")
    async def homestack_status():
        return JSONResponse(
            {"schema": 3, "status": "pass" if beat.fresh else "fail", "process_rss_bytes": _process_rss_bytes()},
            headers={"Cache-Control": "no-store"},
        )

    return app


# ── process: uvicorn with VBC's own signal semantics ────────────────────────


class _UvicornServer(uvicorn.Server):
    """First SIGTERM/SIGINT: graceful orchestrator stop with deadline; second
    SIGINT: interrupt running jobs. The HTTP server exits only after the
    orchestrator thread ended, and signals are not re-raised (exit code 0)."""

    def __init__(self, config: uvicorn.Config, vbc: VbcServer) -> None:
        super().__init__(config)
        self._vbc = vbc
        self._signals = 0

    def handle_exit(self, sig: int, frame) -> None:  # noqa: ANN001 - signal handler signature
        self._signals += 1
        if not self._vbc.orchestrator_alive:
            self.should_exit = True
            if self._signals > 1:
                self.force_exit = True
            return
        if self._signals == 1:
            logger.info(f"signal {signal.Signals(sig).name}: graceful shutdown")
            threading.Thread(target=self._vbc.request_stop, args=("graceful",), daemon=True).start()
        else:
            logger.warning("second signal: interrupting running jobs")
            threading.Thread(target=self._vbc.request_stop, args=("interrupt",), daemon=True).start()


def run_server(vbc: VbcServer) -> int:
    host, port = vbc.config.server.host, vbc.config.server.port
    beat_host = "127.0.0.1" if host in ("0.0.0.0", "::", "127.0.0.1", "localhost") else host
    app = create_app(vbc, heartbeat_url=f"http://{beat_host}:{port}/api/v1/state")
    config = uvicorn.Config(
        app, host=host, port=port, log_config=None, access_log=False,
        timeout_graceful_shutdown=3, lifespan="on",
    )
    server = _UvicornServer(config, vbc)
    vbc.on_exit(lambda: setattr(server, "should_exit", True))
    vbc.start()
    try:
        server.run()
    finally:
        if vbc.orchestrator_alive:  # e.g. the port could not be bound
            vbc.request_stop("interrupt")
        vbc.close()
    return 0 if server.started else 1
