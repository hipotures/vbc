import typer
import threading
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional, List

# Silence all warnings (especially from pyexiftool) to prevent UI glitches
warnings.filterwarnings("ignore")
from vbc.config.loader import load_config, load_demo_config
from vbc.infrastructure.logging import setup_logging
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.file_scanner import FileScanner
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.infrastructure.ffprobe import FFprobeAdapter
from vbc.infrastructure.ffmpeg import FFmpegAdapter
from vbc.infrastructure.housekeeping import HousekeepingService
from vbc.pipeline.orchestrator import Orchestrator
from vbc.pipeline.demo_orchestrator import DemoOrchestrator
from vbc.ui.state import UIState
from vbc.ui.manager import UIManager
from vbc.ui.dashboard import Dashboard
from vbc.ui.keyboard import KeyboardListener, ThreadControlEvent, RequestShutdown
from vbc.domain.events import (
    HardwareCapabilityExceeded, JobStarted, JobCompleted, JobFailed, DiscoveryFinished
)

app = typer.Typer(help="VBC (Video Batch Compression) - Modular Version")

@app.command()
def compress(
    input_dirs_arg: str = typer.Argument(..., help="Directory or comma-separated directories to compress"),
    config_path: Optional[Path] = typer.Option(Path("conf/vbc.yaml"), "--config", "-c", help="Path to YAML config"),
    demo: bool = typer.Option(False, "--demo", help="Run in demo mode (simulate processing, no file IO)"),
    demo_config_path: Optional[Path] = typer.Option(Path("conf/demo.yaml"), "--demo-config", help="Path to demo YAML config"),
    threads: Optional[int] = typer.Option(None, "--threads", "-t", help="Override number of threads"),
    cq: Optional[int] = typer.Option(None, "--cq", help="Override constant quality (0-63)"),
    gpu: Optional[bool] = typer.Option(None, "--gpu/--cpu", help="Enable/disable GPU acceleration"),
    clean_errors: bool = typer.Option(False, "--clean-errors", help="Remove existing .err markers and retry"),
    skip_av1: bool = typer.Option(False, "--skip-av1", help="Skip files already encoded in AV1"),
    min_size: Optional[int] = typer.Option(None, "--min-size", help="Minimum input size in bytes to process"),
    rotate_180: bool = typer.Option(False, "--rotate-180", help="Rotate output 180 degrees"),
    debug: bool = typer.Option(False, "--debug/--no-debug", help="Enable verbose debug logging")
):
    """Batch compress videos in a directory with full feature parity."""
    # Parse comma-separated folders
    input_dirs_raw = [d.strip() for d in input_dirs_arg.split(',')]

    # Convert to Path objects and deduplicate (preserve order)
    input_dirs = []
    seen = set()
    for dir_str in input_dirs_raw:
        dir_path = Path(dir_str)
        if dir_path not in seen:
            input_dirs.append(dir_path)
            seen.add(dir_path)

    # Validate all folders exist
    if not demo:
        for input_dir in input_dirs:
            if not input_dir.exists():
                typer.secho(f"Error: Directory {input_dir} does not exist.", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)

    try:
        config = load_config(config_path)
        # Apply CLI overrides
        if threads: config.general.threads = threads
        if cq: config.general.cq = cq
        if gpu is not None: config.general.gpu = gpu
        if clean_errors: config.general.clean_errors = True
        if skip_av1: config.general.skip_av1 = True
        if min_size is not None: config.general.min_size_bytes = min_size
        if debug: config.general.debug = True
        if rotate_180: config.general.manual_rotation = 180

        demo_config = load_demo_config(demo_config_path) if demo else None

        # Setup output directory and logging FIRST
        output_dir = Path("demo_out") if demo else input_dirs[0].with_name(f"{input_dirs[0].name}_out")
        logger = setup_logging(output_dir, debug=config.general.debug)
        if demo and demo_config:
            logger.info(
                f"VBC demo started: files={demo_config.files.count}, errors={demo_config.errors.total}"
            )
        else:
            logger.info(f"VBC started: input_folders={len(input_dirs)}, folders={input_dirs}")
        logger.info(f"Config: threads={config.general.threads}, cq={config.general.cq}, gpu={config.general.gpu}, debug={config.general.debug}")

        bus = EventBus()

        # UI config with backwards compatibility
        activity_feed_max = config.ui.activity_feed_max_items if hasattr(config, 'ui') else 5

        ui_state = UIState(activity_feed_max_items=activity_feed_max)
        ui_state.current_threads = config.general.threads
        ui_state.strip_unicode_display = config.general.strip_unicode_display
        ui_state.ui_title = "VBC - demo" if demo else "VBC"

        start_time = datetime.now()

        def format_size(size: int) -> str:
            for unit in ["B", "KB", "MB", "GB"]:
                if size < 1024.0:
                    return f"{size:.1f}{unit}"
                size /= 1024.0
            return f"{size:.1f}TB"

        encoder_name = "NVENC AV1 (GPU)" if config.general.gpu else "SVT-AV1 (CPU)"
        preset = "p7 (Slow/HQ)" if config.general.gpu else "6"
        metadata_method = (
            "Deep (ExifTool + XMP)" if (config.general.use_exif and config.general.copy_metadata)
            else ("Basic (FFmpeg)" if config.general.copy_metadata else "None")
        )
        dynamic_cq_info = (
            ", ".join([f"{k}:{v}" for k, v in config.general.dynamic_cq.items()])
            if config.general.dynamic_cq else "None"
        )
        camera_filter_info = ", ".join(config.general.filter_cameras) if config.general.filter_cameras else "None"
        manual_rotation = f"{config.general.manual_rotation}°" if config.general.manual_rotation is not None else "None"
        if demo and demo_config:
            demo_extensions = [entry.ext for entry in demo_config.files.extensions]
            input_dirs_display = [Path(p) for p in demo_config.input_folders] if demo_config.input_folders else [Path("DEMO")]
        else:
            demo_extensions = config.general.extensions
            input_dirs_display = input_dirs
        extensions = [ext if ext.startswith(".") else f".{ext}" for ext in demo_extensions]
        ext_list = ", ".join(extensions)

        if demo and demo_config:
            ui_state.config_lines = [
                "Video Batch Compression - demo",
                f"Start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"Input folders: {len(input_dirs_display)}",
                *[f"  {i+1}. {d}" for i, d in enumerate(input_dirs_display)],
                f"Threads: {config.general.threads} (Prefetch: {config.general.prefetch_factor}x)",
                f"Encoder: {encoder_name} | Preset: {preset}",
                "Audio: Copy (stream copy)",
                f"Quality: CQ{config.general.cq} (Global Default)",
                f"Dynamic CQ: {dynamic_cq_info}",
                f"Camera Filter: {camera_filter_info}",
                f"Metadata: {metadata_method} (Analysis: {config.general.use_exif})",
                f"Autorotate: {len(config.autorotate.patterns)} rules loaded",
                f"Manual Rotation: {manual_rotation}",
                f"Extensions: {ext_list} → .mp4",
                f"Min size: {format_size(config.general.min_size_bytes)} | Skip AV1: {config.general.skip_av1}",
                f"Demo files: {demo_config.files.count} | Errors: {demo_config.errors.total} | Kept original: {demo_config.kept_original.count}",
                f"Demo sizes: {demo_config.sizes.min_mb:.0f}-{demo_config.sizes.max_mb:.0f} MB ({demo_config.sizes.distribution})",
                f"Demo speed: {demo_config.processing.throughput_mb_s:.1f} MB/s (±{int(demo_config.processing.jitter_pct * 100)}%)",
                f"Clean errors: {config.general.clean_errors} | Strip Unicode: {config.general.strip_unicode_display}",
                f"Debug logging: {config.general.debug}",
            ]
        else:
            ui_state.config_lines = [
                f"Video Batch Compression - {encoder_name}",
                f"Start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"Input folders: {len(input_dirs_display)}",
                *[f"  {i+1}. {d}" for i, d in enumerate(input_dirs_display)],
                f"Threads: {config.general.threads} (Prefetch: {config.general.prefetch_factor}x)",
                f"Encoder: {encoder_name} | Preset: {preset}",
                "Audio: Copy (stream copy)",
                f"Quality: CQ{config.general.cq} (Global Default)",
                f"Dynamic CQ: {dynamic_cq_info}",
                f"Camera Filter: {camera_filter_info}",
                f"Metadata: {metadata_method} (Analysis: {config.general.use_exif})",
                f"Autorotate: {len(config.autorotate.patterns)} rules loaded",
                f"Manual Rotation: {manual_rotation}",
                f"Extensions: {ext_list} → .mp4",
                f"Min size: {format_size(config.general.min_size_bytes)} | Skip AV1: {config.general.skip_av1}",
                f"Clean errors: {config.general.clean_errors} | Strip Unicode: {config.general.strip_unicode_display}",
                f"Debug logging: {config.general.debug}",
            ]

        if config.general.filter_cameras and not config.general.use_exif:
            logger.warning("Camera filtering requires EXIF analysis. Enabling use_exif automatically.")
            config.general.use_exif = True
        
        ui_manager = UIManager(bus, ui_state)

        exif = None
        if demo and demo_config:
            orchestrator = DemoOrchestrator(
                config=config,
                demo_config=demo_config,
                event_bus=bus
            )
        else:
            # Housekeeping (Cleanup stale files)
            housekeeper = HousekeepingService()
            for input_dir in input_dirs:
                housekeeper.cleanup_temp_files(input_dir)
                if config.general.clean_errors:
                    # Also cleanup in output dir if it exists
                    output_dir = input_dir.with_name(f"{input_dir.name}_out")
                    if output_dir.exists():
                        housekeeper.cleanup_error_markers(output_dir)

            # Components
            scanner = FileScanner(
                extensions=config.general.extensions,
                min_size_bytes=config.general.min_size_bytes
            )
            exif = ExifToolAdapter()
            exif.et.run()  # Start ExifTool ONCE before processing
            logger.info("ExifTool started")

            ffprobe = FFprobeAdapter()
            ffmpeg = FFmpegAdapter(event_bus=bus)

            orchestrator = Orchestrator(
                config=config,
                event_bus=bus,
                file_scanner=scanner,
                exif_adapter=exif,
                ffprobe_adapter=ffprobe,
                ffmpeg_adapter=ffmpeg
            )
        
        keyboard = KeyboardListener(bus)
        
        gpu_monitor = None
        # GPU config migration: use new gpu_config if available, fallback to general.gpu
        if hasattr(config, 'gpu_config'):
            gpu_cfg = config.gpu_config
        elif config.general.gpu:
            # Backwards compatibility: use old config
            from vbc.config.models import GpuConfig
            gpu_cfg = GpuConfig(
                enabled=config.general.gpu,
                refresh_rate=config.general.gpu_refresh_rate
            )
        else:
            gpu_cfg = None

        if gpu_cfg and gpu_cfg.enabled:
            from vbc.infrastructure.gpu_monitor import GpuMonitor
            from collections import deque
            import math

            # Calculate dynamic maxlen for history
            maxlen = math.ceil(gpu_cfg.history_window_s / gpu_cfg.sample_interval_s)
            maxlen = max(60, min(2000, maxlen))  # Clamp to [60, 2000]

            # Update UIState deques with calculated maxlen
            ui_state.gpu_history_temp = deque(maxlen=maxlen)
            ui_state.gpu_history_pwr = deque(maxlen=maxlen)
            ui_state.gpu_history_gpu = deque(maxlen=maxlen)
            ui_state.gpu_history_mem = deque(maxlen=maxlen)
            ui_state.gpu_history_fan = deque(maxlen=maxlen)

            gpu_monitor = GpuMonitor(
                ui_state,
                refresh_rate=int(gpu_cfg.sample_interval_s),
                device_index=gpu_cfg.nvtop_device_index,
                device_name=gpu_cfg.nvtop_device_name
            )
            gpu_monitor.start()

        # Initialize dashboard with configuration
        panel_scale = config.ui.panel_height_scale if hasattr(config, 'ui') else 0.7
        max_active = config.ui.active_jobs_max_display if hasattr(config, 'ui') else 8
        dashboard = Dashboard(ui_state, panel_height_scale=panel_scale, max_active_jobs=max_active)

        keyboard.start()
        try:
            with dashboard:
                if demo:
                    orchestrator.run()
                else:
                    orchestrator.run(input_dirs)
                if ui_state.discovery_finished and ui_state.files_to_process == 0:
                    with ui_state._lock:
                        ui_state.info_message = (
                            "No files to process.\n\n"
                            "Check input path and filters (extensions, min size, camera filter)."
                        )
                        ui_state.show_info = True
                    threading.Event().wait(2.0)
        finally:
            keyboard.stop()
            if gpu_monitor:
                gpu_monitor.stop()
            # Cleanup ExifTool
            if exif and exif.et.running:
                exif.et.terminate()
                logger.info("ExifTool terminated")

    except KeyboardInterrupt:
        # Ctrl+C was already handled by orchestrator - just exit gracefully
        typer.secho("\n✓ Compression stopped by user (Ctrl+C)", fg=typer.colors.YELLOW)
        raise typer.Exit(code=130)

    except Exception as e:
        with open("error.log", "a") as f:
            import traceback
            traceback.print_exc(file=f)
        typer.secho(f"Fatal Error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

if __name__ == "__main__":
    app()
