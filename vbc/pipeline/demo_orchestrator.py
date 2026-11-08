import concurrent.futures
import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from vbc.config.models import AppConfig, DemoConfig, DemoInputFolder
from vbc.domain.events import (
    ActionMessage,
    DiscoveryErrorEntry,
    DiscoveryFinished,
    HardwareCapabilityExceeded,
    InterruptRequested,
    JobCompleted,
    JobFailed,
    JobProgressUpdated,
    JobStarted,
    ProcessingFinished,
    QueueUpdated,
    RequestShutdown,
    RefreshRequested,
    RefreshFinished,
    ThreadControlEvent,
)
from vbc.domain.models import CompressionJob, JobStatus, VideoFile, VideoMetadata
from vbc.infrastructure.event_bus import EventBus

ADJECTIVES = [
    "amber", "ancient", "azure", "brisk", "calm", "cedar", "clean", "clear",
    "crimson", "distant", "drift", "ember", "faded", "frosty", "gentle",
    "glassy", "golden", "hidden", "hollow", "ivory", "lucky", "mellow",
    "misty", "navy", "new", "plain", "quiet", "rapid", "red", "rust",
    "sandy", "silent", "silver", "sleepy", "soft", "solar", "stone",
    "sunlit", "swift", "tender", "timber", "tiny", "urban", "vivid",
    "warm", "wild", "windy", "wooden",
]

NOUNS = [
    "arch", "bay", "bluff", "bridge", "brook", "canyon", "cove", "crest",
    "delta", "dune", "field", "fjord", "forest", "gate", "glade", "glen",
    "grove", "harbor", "haven", "hill", "isle", "lagoon", "lane", "marsh",
    "mesa", "meadow", "path", "peak", "pike", "plain", "point", "pond",
    "port", "ridge", "river", "road", "rock", "shore", "spring", "stone",
    "summit", "trail", "vale", "valley", "view", "watch", "wharf",
]


class DemoOutcome(str, Enum):
    SUCCESS = "success"
    FFPROBE_FAILED = "ffprobe_failed"
    FFMPEG_FAILED = "ffmpeg_error"
    HW_CAP = "hw_cap"
    AV1_SKIP = "av1_skip"
    CAMERA_SKIP = "camera_skip"


@dataclass(frozen=True)
class DemoJobPlan:
    outcome: DemoOutcome
    fail_pct: Optional[float] = None
    kept_original: bool = False


class DemoNameGenerator:
    def __init__(self, rng: random.Random, min_words: int, max_words: int, separator: str):
        self.rng = rng
        self.min_words = min_words
        self.max_words = max_words
        self.separator = separator

    def _base_name(self) -> str:
        count = self.rng.randint(self.min_words, self.max_words)
        if count <= 1:
            return self.rng.choice(NOUNS)

        parts = [self.rng.choice(ADJECTIVES)]
        for _ in range(max(0, count - 2)):
            parts.append(self.rng.choice(NOUNS))
        parts.append(self.rng.choice(NOUNS))
        return self.separator.join(parts)

    def generate_unique(self, used: set) -> str:
        for _ in range(12):
            name = self._base_name()
            if name not in used:
                used.add(name)
                return name

        suffix = 10
        while True:
            name = f"{self._base_name()}{self.separator}{suffix}"
            if name not in used:
                used.add(name)
                return name
            suffix += 1


class DemoOrchestrator:
    def __init__(self, config: AppConfig, demo_config: DemoConfig, event_bus: EventBus):
        self.config = config
        self.demo_config = demo_config
        self.event_bus = event_bus
        self.logger = logging.getLogger(__name__)
        self._rng = random.Random(demo_config.seed)

        self._shutdown_requested = False
        self._current_max_threads = config.general.threads
        self._active_threads = 0
        self._thread_lock = threading.Condition()
        self._refresh_requested = False
        self._refresh_lock = threading.Lock()
        self._shutdown_event = threading.Event()

        self._job_plans: Dict[Path, DemoJobPlan] = {}

        self._setup_subscriptions()

    def _setup_subscriptions(self):
        self.event_bus.subscribe(RequestShutdown, self._on_shutdown_request)
        self.event_bus.subscribe(ThreadControlEvent, self._on_thread_control)
        self.event_bus.subscribe(RefreshRequested, self._on_refresh_request)
        self.event_bus.subscribe(InterruptRequested, self._on_interrupt_requested)

    def _on_shutdown_request(self, event: RequestShutdown):
        with self._thread_lock:
            if self._shutdown_requested:
                self._shutdown_requested = False
                message = "SHUTDOWN cancelled"
            else:
                self._shutdown_requested = True
                message = "SHUTDOWN requested (press S to cancel)"
            self._thread_lock.notify_all()
        self.event_bus.publish(ActionMessage(message=message))

    def _on_thread_control(self, event: ThreadControlEvent):
        if self._shutdown_requested:
            return

        old_val = self._current_max_threads
        with self._thread_lock:
            requested = self._current_max_threads + event.change
            self._current_max_threads = max(1, min(8, requested))
            self._thread_lock.notify_all()
        if self._current_max_threads != old_val:
            self.event_bus.publish(ActionMessage(message=f"Threads: {old_val} → {self._current_max_threads}"))
        elif requested > self._current_max_threads:
            self.event_bus.publish(ActionMessage(message=f"Threads: {self._current_max_threads} (max)"))
        elif requested < self._current_max_threads:
            self.event_bus.publish(ActionMessage(message=f"Threads: {self._current_max_threads} (min)"))

    def _on_refresh_request(self, event: RefreshRequested):
        with self._refresh_lock:
            self._refresh_requested = True

    def _on_interrupt_requested(self, event: InterruptRequested):
        self.logger.info("Interrupt requested (Ctrl+C) - stopping demo orchestrator...")
        self.event_bus.publish(ActionMessage(message="Ctrl+C - interrupting active compressions..."))
        self._shutdown_event.set()
        with self._thread_lock:
            self._shutdown_requested = True
            self._thread_lock.notify_all()

    def _weighted_choice(self, items: List, weight_attr: str = "weight"):
        weights = [getattr(item, weight_attr) for item in items]
        return self._rng.choices(items, weights=weights, k=1)[0]

    def _sample_size_mb(self) -> float:
        sizes = self.demo_config.sizes
        if sizes.distribution == "uniform":
            return self._rng.uniform(sizes.min_mb, sizes.max_mb)
        return self._rng.triangular(sizes.min_mb, sizes.max_mb, sizes.mode_mb)

    def _sample_bitrate_mbps(self) -> float:
        bitrate = self.demo_config.bitrate_mbps
        return self._rng.triangular(bitrate.min_mbps, bitrate.max_mbps, bitrate.mode_mbps)

    def _build_job_plans(self, files: List[VideoFile]) -> Dict[Path, DemoJobPlan]:
        total = len(files)
        error_total = min(self.demo_config.errors.total, total)
        indices = list(range(total))
        self._rng.shuffle(indices)
        error_indices = set(indices[:error_total])
        remaining = indices[error_total:]
        kept_total = min(self.demo_config.kept_original.count, len(remaining))
        kept_indices = set(remaining[:kept_total])

        plans: Dict[Path, DemoJobPlan] = {}
        for idx, vf in enumerate(files):
            if idx in error_indices:
                error_type = self._pick_error_type()
                plans[vf.path] = DemoJobPlan(
                    outcome=error_type,
                    fail_pct=self._pick_fail_pct(error_type),
                )
            else:
                plans[vf.path] = DemoJobPlan(
                    outcome=DemoOutcome.SUCCESS,
                    kept_original=idx in kept_indices,
                )
        return plans

    def _pick_error_type(self) -> DemoOutcome:
        error_types = self.demo_config.errors.types
        if not error_types:
            return DemoOutcome.FFMPEG_FAILED
        choice = self._weighted_choice(error_types, weight_attr="weight")
        return DemoOutcome(choice.type)

    def _pick_fail_pct(self, outcome: DemoOutcome) -> Optional[float]:
        if outcome == DemoOutcome.FFMPEG_FAILED:
            return self._rng.uniform(20.0, 80.0)
        if outcome == DemoOutcome.HW_CAP:
            return self._rng.uniform(10.0, 35.0)
        return None

    def _parse_size_to_bytes(self, size_str: str) -> int:
        """Parse size string like '12.5GB' to bytes."""
        import re
        size_str = size_str.strip().upper()
        match = re.match(r'^([\d.]+)\s*(GB|MB|TB|KB|B)?$', size_str)
        if not match:
            return 0
        value = float(match.group(1))
        unit = match.group(2) or "B"
        multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
        return int(value * multipliers.get(unit, 1))

    def _calculate_input_folders_totals(self) -> tuple[int, int]:
        """Calculate total files and size from input_folders mockup data.

        Returns:
            (total_files, total_size_bytes) tuple
        """
        total_files = 0
        total_size_bytes = 0

        for folder_entry in self.demo_config.input_folders:
            if isinstance(folder_entry, DemoInputFolder):
                # Only count folders with status "ok" and valid data
                if folder_entry.status in (None, "ok"):
                    if folder_entry.files is not None:
                        total_files += folder_entry.files
                    if folder_entry.size:
                        total_size_bytes += self._parse_size_to_bytes(folder_entry.size)
            # Ignore old string format - no mockup data available

        return total_files, total_size_bytes

    def _generate_files(self) -> List[VideoFile]:
        demo_files = self.demo_config.files
        name_gen = DemoNameGenerator(
            self._rng,
            demo_files.min_words,
            demo_files.max_words,
            demo_files.separator,
        )
        used_names: set = set()
        files: List[VideoFile] = []

        # Use input_folders totals if available, otherwise fall back to files.count
        total_files, _ = self._calculate_input_folders_totals()
        file_count = total_files if total_files > 0 else (demo_files.count or 0)

        if file_count == 0:
            return []

        for _ in range(file_count):
            base_name = name_gen.generate_unique(used_names)
            ext = self._weighted_choice(demo_files.extensions).ext
            if not ext.startswith("."):
                ext = f".{ext}"
            filename = f"{base_name}{ext}"

            size_mb = self._sample_size_mb()
            size_bytes = int(size_mb * 1024 * 1024)
            bitrate_mbps = self._sample_bitrate_mbps()
            duration_s = max(1.0, (size_mb * 8.0) / bitrate_mbps)

            resolution = self._weighted_choice(self.demo_config.resolutions)
            fps = self._weighted_choice(self.demo_config.fps).value
            codec = self._weighted_choice(self.demo_config.codecs).name

            metadata = VideoMetadata(
                width=resolution.width,
                height=resolution.height,
                codec=codec,
                fps=fps,
                megapixels=round((resolution.width * resolution.height) / 1_000_000),
                color_space=self._rng.choice(["bt709", "bt2020"]),
                duration=duration_s,
                bitrate_kbps=bitrate_mbps * 1000.0,
                camera_model=self._rng.choice(self.demo_config.camera_models)
                if self.demo_config.camera_models else None,
            )

            files.append(VideoFile(path=Path(filename), size_bytes=size_bytes, metadata=metadata))

        return files

    def _simulate_progress(self, job: CompressionJob, duration_s: float, target_pct: float) -> bool:
        interval = self.demo_config.processing.progress_interval_s
        start = time.monotonic()
        while True:
            if self._shutdown_event.is_set():
                job.status = JobStatus.INTERRUPTED
                job.error_message = "Interrupted by user (Ctrl+C)"
                self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
                return False

            if self._shutdown_requested:
                job.status = JobStatus.INTERRUPTED
                job.error_message = "Stopped by user"
                self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
                return False

            elapsed = time.monotonic() - start
            if elapsed >= duration_s:
                break

            progress = min(target_pct, (elapsed / duration_s) * target_pct)
            self.event_bus.publish(JobProgressUpdated(job=job, progress_percent=progress))
            time.sleep(interval)

        self.event_bus.publish(JobProgressUpdated(job=job, progress_percent=target_pct))
        return True

    def _processing_time_s(self, size_bytes: int) -> float:
        throughput = self.demo_config.processing.throughput_mb_s
        jitter = self.demo_config.processing.jitter_pct
        size_mb = size_bytes / (1024 * 1024)
        base = size_mb / throughput
        factor = self._rng.uniform(1.0 - jitter, 1.0 + jitter)
        return max(0.2, base * factor)

    def _process_file(self, video_file: VideoFile, plan: DemoJobPlan):
        with self._thread_lock:
            while self._active_threads >= self._current_max_threads:
                self._thread_lock.wait()

            if self._shutdown_requested:
                return

            self._active_threads += 1

        job = CompressionJob(source_file=video_file, status=JobStatus.PENDING)
        try:
            if plan.outcome == DemoOutcome.FFPROBE_FAILED:
                job.status = JobStatus.FAILED
                job.error_message = "File is corrupted (ffprobe failed to read). Skipped."
                self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
                return
            if plan.outcome == DemoOutcome.AV1_SKIP:
                job.status = JobStatus.SKIPPED
                job.error_message = "Already encoded in AV1"
                self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
                return
            if plan.outcome == DemoOutcome.CAMERA_SKIP:
                job.status = JobStatus.SKIPPED
                cam_model = video_file.metadata.camera_model if video_file.metadata else ""
                job.error_message = f'Camera model "{cam_model}" not in filter'
                self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
                return

            self.event_bus.publish(JobStarted(job=job))
            job.status = JobStatus.PROCESSING

            duration_s = self._processing_time_s(video_file.size_bytes)
            if plan.fail_pct is not None:
                duration_s = max(0.2, duration_s * (plan.fail_pct / 100.0))
            target_pct = plan.fail_pct or 100.0
            finished = self._simulate_progress(job, duration_s, target_pct)
            if not finished:
                return

            if plan.outcome == DemoOutcome.FFMPEG_FAILED:
                job.status = JobStatus.FAILED
                job.error_message = "ffmpeg exited with code 1"
                self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
                return

            if plan.outcome == DemoOutcome.HW_CAP:
                job.status = JobStatus.HW_CAP_LIMIT
                job.error_message = "Hardware is lacking required capabilities"
                self.event_bus.publish(HardwareCapabilityExceeded(job=job))
                return

            ratio = self._rng.uniform(self.demo_config.output_ratio.min, self.demo_config.output_ratio.max)
            if plan.kept_original:
                ratio = 1.0
                filename = video_file.path.name
                job.error_message = f"Ratio {ratio:.2f} above threshold, kept original: {filename}"

            job.status = JobStatus.COMPLETED
            job.output_size_bytes = int(video_file.size_bytes * ratio)
            self.event_bus.publish(JobCompleted(job=job))
        finally:
            with self._thread_lock:
                self._active_threads -= 1
                self._thread_lock.notify_all()

    def _build_discovery_stats(self, files_to_process: int) -> Dict[str, int]:
        discovery = self.demo_config.discovery
        files_found = (
            files_to_process
            + discovery.already_compressed
            + discovery.ignored_small
            + discovery.ignored_err
        )
        return {
            "files_found": files_found,
            "files_to_process": files_to_process,
            "already_compressed": discovery.already_compressed,
            "ignored_small": discovery.ignored_small,
            "ignored_err": discovery.ignored_err,
        }

    def run(self):
        files = self._generate_files()
        self._job_plans = self._build_job_plans(files)

        stats = self._build_discovery_stats(len(files))
        folder_count = len(self.demo_config.input_folders) if self.demo_config.input_folders else 1

        err_messages = [
            "File is corrupted or unreadable (ffprobe failed)",
            "Permission denied: cannot open file for reading",
            "Unsupported container format",
            "File truncated or incomplete",
        ]
        ignored_err_entries = [
            DiscoveryErrorEntry(
                path=Path(f"DEMO/error_{i + 1:03d}.mp4"),
                size_bytes=None,
                error_message=err_messages[i % len(err_messages)],
            )
            for i in range(stats["ignored_err"])
        ]

        self.event_bus.publish(DiscoveryFinished(
            files_found=stats["files_found"],
            files_to_process=stats["files_to_process"],
            already_compressed=stats["already_compressed"],
            ignored_small=stats["ignored_small"],
            ignored_err=stats["ignored_err"],
            ignored_err_entries=ignored_err_entries,
            ignored_av1=0,
            source_folders_count=max(1, folder_count),
        ))

        if not files:
            self.event_bus.publish(ProcessingFinished())
            return

        pending = deque(files)
        in_flight: Dict[concurrent.futures.Future, VideoFile] = {}

        self.event_bus.publish(QueueUpdated(pending_files=list(pending)))

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            def submit_batch():
                max_inflight = self.config.general.prefetch_factor * self._current_max_threads
                while len(in_flight) < max_inflight and pending and not self._shutdown_requested:
                    vf = pending.popleft()
                    plan = self._job_plans.get(vf.path, DemoJobPlan(outcome=DemoOutcome.SUCCESS))
                    future = executor.submit(self._process_file, vf, plan)
                    in_flight[future] = vf
                self.event_bus.publish(QueueUpdated(pending_files=list(pending)))

            submit_batch()

            try:
                while in_flight:
                    done, _ = concurrent.futures.wait(
                        set(in_flight.keys()),
                        timeout=0.5,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )

                    for future in done:
                        try:
                            future.result()
                        except Exception as exc:
                            self.logger.error(f"Demo future failed: {exc}")
                        del in_flight[future]

                    with self._refresh_lock:
                        if self._refresh_requested:
                            self._refresh_requested = False
                            self.event_bus.publish(RefreshFinished(added=0, removed=0))
                            self.event_bus.publish(ActionMessage(message="Refreshed: no changes (demo)"))

                    submit_batch()

                    if self._shutdown_requested and not in_flight:
                        break

            except KeyboardInterrupt:
                self.logger.info("Ctrl+C detected in demo - interrupting active jobs...")
                self.event_bus.publish(InterruptRequested())
                self.event_bus.publish(ActionMessage(message="Ctrl+C - interrupting active compressions..."))
                self._shutdown_event.set()
                self._shutdown_requested = True
                with self._thread_lock:
                    self._thread_lock.notify_all()
                for future in list(in_flight.keys()):
                    future.cancel()
                deadline = time.monotonic() + 3.0
                while True:
                    running = [f for f in in_flight if not f.done()]
                    if not running or time.monotonic() >= deadline:
                        break
                    concurrent.futures.wait(running, timeout=0.2, return_when=concurrent.futures.FIRST_COMPLETED)
                executor.shutdown(wait=False, cancel_futures=True)
                raise

            time.sleep(0.8)
            if not self._shutdown_requested:
                self.event_bus.publish(ProcessingFinished())
