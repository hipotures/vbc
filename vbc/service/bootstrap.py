"""Headless composition root of ``vbc serve``.

Wiring cut out of legacy ``vbc_legacy.main.compress`` (config, input dirs,
housekeeping, adapters, orchestrator, watchers, GPU monitor) without any UI,
plus the server lifecycle from docs/architecture/service-split.md
("Process model"): the orchestrator runs in its own thread and waits for the
next refresh after every pass (idle); the process ends only on shutdown.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from vbc.config.input_dirs import can_write_output_dir_path, evaluate_input_dirs
from vbc.config.loader import load_config, load_demo_config
from vbc.config.local_registry import LocalConfigRegistry
from vbc.config.models import AppConfig
from vbc.config.overrides import CliConfigOverrides
from vbc.config.rate_control import describe_rate_target
from vbc.domain.events import InputDirsChanged, InterruptRequested, RefreshRequested, SetShutdownRequested
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.exiftool_tmp import cleanup_exiftool_tmp_files
from vbc.infrastructure.ffmpeg import (
    extract_preset,
    extract_quality_flag,
    extract_quality_value,
    infer_encoder_label,
    output_extension_for_args,
    select_encoder_args,
)
from vbc.service.commands import CommandService
from vbc.service.log_buffer import LogBuffer
from vbc.service.state_store import StateStore
from vbc.service.storage import StorageRoots

logger = logging.getLogger("vbc.server")

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


class ServerConfigError(Exception):
    """The configuration cannot be served safely (exit before binding)."""


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def apply_env_overrides(config: AppConfig, env: Mapping[str, str]) -> None:
    """HomeStack delivers deployment settings as environment variables."""
    if env.get("VBC_SERVER_HOST"):
        config.server.host = env["VBC_SERVER_HOST"]
    if env.get("VBC_SERVER_PORT"):
        config.server.port = int(env["VBC_SERVER_PORT"])
    if env.get("VBC_ALLOWED_HOSTS"):
        config.server.allowed_hosts = [h.strip() for h in env["VBC_ALLOWED_HOSTS"].split(",") if h.strip()]
    gpu = (env.get("VBC_GPU") or "").strip().lower()
    if gpu in ("auto", "1", "true", "yes"):
        # GPU whenever NVENC works here (probed at start), CPU otherwise.
        config.general.gpu = True
        config.gpu_config.enabled = True
    elif gpu in ("0", "false", "no", "cpu"):
        config.general.gpu = False
    if env.get("VBC_STORAGE_ROOTS"):
        config.storage.roots = [r.strip() for r in env["VBC_STORAGE_ROOTS"].split(",") if r.strip()]


def probe_nvenc(timeout_s: float = 30.0) -> tuple[bool, str]:
    """Encode a few frames with av1_nvenc: is GPU encoding usable right now?

    Covers every way it can be missing: no card, no device nodes in the
    container, driver libraries not injected, an older GPU without AV1 NVENC.
    """
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "color=c=black:s=1280x720:r=30:d=0.2", "-c:v", "av1_nvenc", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode == 0:
        return True, "av1_nvenc works"
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return False, (lines[-1] if lines else f"ffmpeg exit code {result.returncode}")[:300]


def configure_logging(debug: bool, log_file: Optional[Path]) -> LogBuffer:
    """stdout (container convention) + ring buffer for /logs + optional file."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    level = logging.DEBUG if debug else logging.INFO
    root.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.setLevel(level)
    root.addHandler(stream)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, mode="a")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root.addHandler(file_handler)
    buffer = LogBuffer()
    buffer.setLevel(logging.DEBUG)
    root.addHandler(buffer)
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return buffer


@dataclass
class _Dirs:
    input_dirs: List[Path] = field(default_factory=list)
    output_dir_map: Dict[Path, Path] = field(default_factory=dict)
    errors_dir_map: Dict[Path, Path] = field(default_factory=dict)


def _resolve_dirs(config: AppConfig) -> _Dirs:
    entries = [entry.path for entry in config.input_dirs if entry.enabled]
    if not entries:
        return _Dirs()
    input_dirs, _status, out_map, err_map = evaluate_input_dirs(
        entries,
        output_dirs=config.output_dirs or None,
        suffix_output_dirs=config.suffix_output_dirs,
        errors_dirs=config.errors_dirs or None,
        suffix_errors_dirs=config.suffix_errors_dirs,
    )
    return _Dirs(input_dirs=input_dirs, output_dir_map=out_map, errors_dir_map=err_map)


class VbcServer:
    """Everything `vbc serve` runs except the HTTP listener."""

    def __init__(
        self,
        config_path: Path,
        *,
        demo: bool = False,
        demo_config_path: Optional[Path] = None,
        log_file: Optional[Path] = None,
        env: Mapping[str, str] = os.environ,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        self.config_path = config_path
        self.started_at = time.time()
        self.demo = demo
        self.demo_config_path = demo_config_path
        config = load_config(config_path)
        apply_env_overrides(config, env)
        if host is not None:
            config.server.host = host
        if port is not None:
            config.server.port = port
        # Server lifecycle: wait for the next refresh after every pass.
        config.general.wait_on_finish = True
        config.general.bell_on_finish = False
        config.web_server.enabled = False
        self.config = config
        # Off loopback the API may point the server only into storage roots: the
        # configured ones plus every media mount (the volume holding the config is
        # not media). On loopback without configured roots there is no restriction.
        if is_loopback(config.server.host) and not config.storage.roots:
            self.storage_roots: Optional[StorageRoots] = None
        else:
            self.storage_roots = StorageRoots(
                config.storage.roots,
                detect_mounts=not is_loopback(config.server.host),
                exclude=[config_path.resolve()],
            )

        self.log_buffer = configure_logging(config.general.debug, log_file)
        # general.gpu means "GPU if it works here": probe once, else encode on CPU,
        # instead of failing every job (and marking files as errors) without a GPU.
        self.gpu_probe: Optional[str] = None
        if config.general.gpu and not demo:
            usable, reason = probe_nvenc()
            self.gpu_probe = reason
            if usable:
                logger.info("GPU encoding: av1_nvenc available")
            else:
                logger.warning(f"GPU encoding unavailable ({reason}); encoding on CPU (SVT-AV1)")
                config.general.gpu = False
        self.bus = EventBus()
        self.store = StateStore(self.bus, config)
        self.commands = CommandService(
            self.bus, self.store, config, config_path,
            storage_roots=self.storage_roots,
        )
        self.dirs = _resolve_dirs(config)
        self._exif = None
        self._watchers: list = []
        self._watchers_lock = threading.Lock()
        self._gpu_monitor = None
        self._thread: Optional[threading.Thread] = None
        self._finished = threading.Event()
        self._exit_requested = threading.Event()
        self._resume = threading.Event()
        self.bus.subscribe(RefreshRequested, lambda _event: self._resume.set())
        self._on_exit: List[Callable[[], None]] = []
        self._stop_timer: Optional[threading.Timer] = None
        self.orchestrator = self._build_orchestrator()
        self._build_gpu_monitor()

    # ── composition (port of legacy compress()) ─────────────────────────────

    def _build_orchestrator(self):
        config = self.config
        if self.demo:
            from vbc.pipeline.demo_orchestrator import DemoOrchestrator

            demo_path = self.demo_config_path or Path("conf/demo.yaml")
            return DemoOrchestrator(config=config, demo_config=load_demo_config(demo_path), event_bus=self.bus)

        from vbc.infrastructure.exif_tool import ExifToolAdapter
        from vbc.infrastructure.ffmpeg import FFmpegAdapter
        from vbc.infrastructure.ffprobe import FFprobeAdapter
        from vbc.infrastructure.file_scanner import FileScanner
        from vbc.infrastructure.housekeeping import HousekeepingService
        from vbc.pipeline.orchestrator import Orchestrator

        dirs = self.dirs
        removed = cleanup_exiftool_tmp_files(dirs.output_dir_map.values(), logger=logger)
        if removed:
            logger.info(f"Removed {len(removed)} stale ExifTool temp files before processing.")
        housekeeper = HousekeepingService()
        for input_dir in dirs.input_dirs:
            entry = next((e for e in config.input_dirs if Path(e.path) == input_dir), None)
            if entry is not None and entry.metadata:
                continue
            output_dir = dirs.output_dir_map.get(input_dir)
            errors_dir = dirs.errors_dir_map.get(input_dir)
            if output_dir and errors_dir:
                housekeeper.cleanup_output_markers(
                    input_dir=input_dir, output_dir=output_dir, errors_dir=errors_dir,
                    clean_errors=config.general.clean_errors, logger=logger,
                )

        self._scanner = FileScanner(
            extensions=config.general.extensions, min_size_bytes=config.general.min_size_bytes,
        )
        self._exif = ExifToolAdapter()
        orchestrator = Orchestrator(
            config=config,
            event_bus=self.bus,
            file_scanner=self._scanner,
            exif_adapter=self._exif,
            ffprobe_adapter=FFprobeAdapter(),
            ffmpeg_adapter=FFmpegAdapter(event_bus=self.bus),
            output_dir_map=dirs.output_dir_map,
            errors_dir_map=dirs.errors_dir_map,
            local_config_registry=LocalConfigRegistry(),
            cli_overrides=CliConfigOverrides(),
            config_path=self.config_path,
            input_dir_entries={Path(e.path): e for e in config.input_dirs if e.enabled},
        )
        self._build_watchers()
        self.bus.subscribe(InputDirsChanged, self._on_input_dirs_changed)
        return orchestrator

    def _on_input_dirs_changed(self, _event: InputDirsChanged) -> None:
        """Folders, their manifest and watch flags changed through the API: rebuild
        the watchers from the new entries (the orchestrator re-reads them itself)."""
        with self._watchers_lock:
            old, self._watchers = self._watchers, []
            for watcher in old:
                self.bus.unsubscribe(InputDirsChanged, watcher._on_input_dirs_changed)
                try:
                    watcher.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("watcher stop failed")
            try:
                self._build_watchers()
                for watcher in self._watchers:
                    watcher.start()
            except Exception:  # noqa: BLE001
                logger.exception("Could not start the folder watchers")

    def _build_watchers(self) -> None:
        config = self.config
        active = [Path(e.path) for e in config.input_dirs if e.enabled]
        inotify_dirs = {
            Path(e.path) for e in config.input_dirs
            if e.metadata and e.watch and e.watch_mode == "inotify"
        }
        if inotify_dirs:
            from vbc.infrastructure.manifest_watcher import ManifestWatcher

            self._watchers.append(ManifestWatcher(event_bus=self.bus, watchable_dirs=inotify_dirs, active_dirs=active))
        polling: Dict[tuple, set] = {}
        for e in config.input_dirs:
            if e.watch and e.watch_mode == "polling":
                polling.setdefault((e.metadata, e.poll_interval_seconds), set()).add(Path(e.path))
        if polling:
            from vbc.infrastructure.polling_watcher import PollingWatcher

            scanner = self._scanner
            for (metadata, interval), watch_dirs in sorted(polling.items()):
                if metadata:
                    scan_paths = lambda directory: directory.rglob("*.json")  # noqa: E731
                else:
                    scan_paths = lambda directory: (v.path for v in scanner.scan(directory))  # noqa: E731
                self._watchers.append(PollingWatcher(
                    event_bus=self.bus, scan_paths=scan_paths, watchable_dirs=watch_dirs,
                    active_dirs=active, poll_interval_seconds=interval,
                ))

    def _build_gpu_monitor(self) -> None:
        gpu_cfg = self.config.gpu_config
        if not (gpu_cfg.enabled and self.config.general.gpu):
            return
        from vbc.infrastructure.gpu_monitor import GpuMonitor

        self._gpu_monitor = GpuMonitor(
            self.store, refresh_rate=max(1, int(gpu_cfg.sample_interval_s)),
            device_index=gpu_cfg.nvtop_device_index, device_name=gpu_cfg.nvtop_device_name,
            nvtop_path=gpu_cfg.nvtop_path,
        )

    def current_storage_roots(self) -> List[Path]:
        return self.storage_roots.current() if self.storage_roots is not None else []

    @property
    def gpu_enabled(self) -> bool:
        return self._gpu_monitor is not None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def on_exit(self, callback: Callable[[], None]) -> None:
        """Called once the process should stop (orchestrator done after a shutdown)."""
        self._on_exit.append(callback)

    def start(self) -> None:
        try:
            self.commands.scan_mounts()  # new media mounts appear in Dirs, disabled
        except Exception:  # noqa: BLE001
            logger.exception("Mount scan failed")
        if self._exif is not None:
            self._exif.et.run()
            logger.info("ExifTool started")
        for watcher in self._watchers:
            watcher.start()
        if self._gpu_monitor is not None:
            self._gpu_monitor.start()
        self._thread = threading.Thread(target=self._run, name="vbc-orchestrator", daemon=True)
        self._thread.start()
        logger.info(
            f"VBC server started: input_dirs={[str(d) for d in self.dirs.input_dirs]}, demo={self.demo}"
        )

    def _run(self) -> None:
        """Run passes until the process is asked to exit.

        A pass ends only after a stop (POST /stop: graceful or interrupt) or,
        in demo mode, when the demo is done. The server then waits for a
        refresh and starts again in the same process; only a signal ends it.
        """
        try:
            while True:
                if self.demo:
                    self.orchestrator.run()
                else:
                    self.orchestrator.run(self.dirs.input_dirs)
                if self._exit_requested.is_set():
                    break
                snapshot = self.store.snapshot()
                self._resume.clear()
                if snapshot.stop_requested or snapshot.interrupt_requested:
                    logger.info("Processing stopped; waiting for a refresh to resume")
                    self.store.set_stopped()
                else:
                    self.store.set_idle()  # demo finished its files
                while not (self._resume.wait(0.5) or self._exit_requested.is_set()):
                    pass
                if self._exit_requested.is_set():
                    break
                logger.info("Refresh received: resuming processing")
                self.orchestrator.reset_after_stop()
                self.store.mark_resumed()
        except Exception as exc:  # noqa: BLE001 - reported to clients, process stays up
            from vbc.pipeline.orchestrator import VerificationAbortError

            if isinstance(exc, VerificationAbortError):
                logger.error(f"Verification abort: {exc}")
                self.store.set_failure("verification_abort", str(exc))
            else:
                logger.exception("Orchestrator crashed")
                self.store.set_failure("failed", f"{type(exc).__name__}: {exc}")
            self._exit_requested.wait()
        finally:
            self._finished.set()
            self._fire_exit()

    @property
    def orchestrator_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _fire_exit(self) -> None:
        if self._stop_timer is not None:
            self._stop_timer.cancel()
        callbacks, self._on_exit = self._on_exit, []
        for callback in callbacks:
            callback()

    def request_exit(self, mode: str = "graceful", deadline_s: Optional[float] = None) -> None:
        """Process exit (signals only; the API can stop processing but never the server).

        graceful: running jobs finish, bounded by server.stop_timeout_s, then
        they are interrupted. interrupt: running jobs are stopped now.
        """
        first = not self._exit_requested.is_set()
        self._exit_requested.set()
        if first:
            self.store.set_exiting()
        if mode == "interrupt":
            self.bus.publish(InterruptRequested())
            return
        self.bus.publish(SetShutdownRequested(requested=True))
        timeout = self.config.server.stop_timeout_s if deadline_s is None else deadline_s
        if self._stop_timer is None and timeout is not None:
            self._stop_timer = threading.Timer(timeout, self._deadline_reached)
            self._stop_timer.daemon = True
            self._stop_timer.start()

    def _deadline_reached(self) -> None:
        if not self._finished.is_set():
            logger.warning("Graceful exit deadline reached; interrupting running jobs")
            self.bus.publish(InterruptRequested())

    def close(self) -> None:
        """Release external resources after the orchestrator thread ended."""
        if self._thread is not None:
            self._thread.join(timeout=5)
        for watcher in self._watchers:
            try:
                watcher.stop()
            except Exception:  # noqa: BLE001
                logger.exception("watcher stop failed")
        if self._gpu_monitor is not None:
            self._gpu_monitor.stop()
        if self._exif is not None and self._exif.et.running:
            self._exif.et.terminate()
            logger.info("ExifTool terminated")

    # ── read views for the API (configuration window) ───────────────────────

    def config_sections(self) -> Dict[str, Dict[str, object]]:
        config = self.config
        general = config.general
        encoder_args = select_encoder_args(config, general.gpu)
        if general.quality_mode == "rate":
            quality_display = describe_rate_target(general.bps)
        else:
            value = extract_quality_value(encoder_args)
            flag = extract_quality_flag(encoder_args)
            label = "CQ" if flag == "-cq" else "CRF" if flag == "-crf" else "Q"
            quality_display = f"{label}{value}" if value is not None else None
        dump = config.model_dump(mode="json")
        return {
            "general": {k: v for k, v in dump["general"].items() if k not in ("dynamic_quality",)},
            "encoder": {
                "gpu": general.gpu,
                "gpu_probe": self.gpu_probe,
                "label": infer_encoder_label(encoder_args, general.gpu),
                "preset": extract_preset(encoder_args),
                "output_suffix": output_extension_for_args(encoder_args),
                "args": encoder_args,
            },
            "quality": {
                "mode": general.quality_mode,
                "display": quality_display,
                "dynamic_quality": dump["general"].get("dynamic_quality"),
            },
            "filters": {
                "extensions": general.extensions,
                "min_size_bytes": general.min_size_bytes,
                "filter_cameras": general.filter_cameras,
                "skip_av1": general.skip_av1,
                "min_compression_ratio": general.min_compression_ratio,
            },
            "metadata": dump["metadata"],
            "autorotate": dump["autorotate"],
            "threads": self.store.threads.model_dump(),
            "server": dump["server"],
            "storage": dump["storage"],
            "gpu_monitor": {"enabled": self.gpu_enabled, **dump["gpu_config"]},
            "demo": self._demo_section(),
            "session": {"started_at": int(self.started_at * 1000), "config_path": str(self.config_path)},
        }

    def _demo_section(self) -> Dict[str, object]:
        if not self.demo:
            return {"enabled": False}
        demo_config = getattr(self.orchestrator, "demo_config", None)
        if demo_config is None:
            return {"enabled": True}
        return {
            "enabled": True,
            "files": demo_config.files.count,
            "errors": demo_config.errors.total,
            "kept_original": demo_config.kept_original.count,
            "size_min_mb": demo_config.sizes.min_mb,
            "size_max_mb": demo_config.sizes.max_mb,
            "size_distribution": demo_config.sizes.distribution,
            "throughput_mb_s": demo_config.processing.throughput_mb_s,
            "jitter_pct": demo_config.processing.jitter_pct,
            "extensions": [e.ext for e in demo_config.files.extensions],
        }

    def io_dirs(self, pending: list) -> List[dict]:
        config = self.config
        rows = []
        for entry in config.input_dirs:
            path = Path(entry.path)
            output_dir = self.dirs.output_dir_map.get(path)
            if output_dir is None and config.suffix_output_dirs:
                output_dir = path.with_name(f"{path.name}{config.suffix_output_dirs}")
            errors_dir = self.dirs.errors_dir_map.get(path)
            if errors_dir is None and config.suffix_errors_dirs:
                errors_dir = path.with_name(f"{path.name}{config.suffix_errors_dirs}")
            count = size = 0
            for video in pending:
                try:
                    video.origin_path.relative_to(path)
                except ValueError:
                    continue
                count += 1
                size += video.size_bytes
            rows.append({
                "path": entry.path,
                "enabled": entry.enabled,
                "metadata": entry.metadata,
                "exists": path.is_dir(),
                "readable": os.access(path, os.R_OK | os.X_OK),
                "output_dir": str(output_dir) if output_dir else None,
                "errors_dir": str(errors_dir) if errors_dir else None,
                "output_writable": can_write_output_dir_path(output_dir) if output_dir else None,
                "pending_count": count,
                "pending_bytes": size,
            })
        return rows
