import typer
import threading
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple
from vbc.config.loader import load_config, load_demo_config
from vbc.config.overrides import CliConfigOverrides
from vbc.config.local_registry import LocalConfigRegistry
from vbc.infrastructure.logging import setup_logging
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.file_scanner import FileScanner
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.infrastructure.ffprobe import FFprobeAdapter
from vbc.infrastructure.ffmpeg import (
    FFmpegAdapter,
    select_encoder_args,
    extract_quality_value,
    extract_quality_flag,
    extract_preset,
    output_extension_for_args,
    infer_encoder_label,
    replace_quality_value,
)
from vbc.infrastructure.housekeeping import HousekeepingService
from vbc.pipeline.orchestrator import Orchestrator
from vbc.pipeline.demo_orchestrator import DemoOrchestrator
from vbc.pipeline.error_file_mover import move_failed_files, collect_error_entries
from vbc.pipeline.repair import process_repairs
from vbc.ui.state import UIState
from vbc.ui.manager import UIManager
from vbc.ui.dashboard import Dashboard
from vbc.ui.keyboard import KeyboardListener
from vbc.config.input_dirs import (
    parse_cli_input_dirs,
    normalize_input_dir_entries,
    normalize_output_dir_entries,
    normalize_errors_dir_entries,
    dedupe_preserve_order,
    validate_input_dir_entries,
    validate_output_dirs,
    validate_errors_dirs,
    evaluate_input_dirs,
    build_input_dir_lines,
    STATUS_OK,
    STATUS_NO_ACCESS,
    STATUS_MISSING,
    can_write_output_dir_path,
)
from vbc.config.models import validate_queue_sort, DemoInputFolder

# Silence all warnings (especially from pyexiftool) to prevent UI glitches
warnings.filterwarnings("ignore")

app = typer.Typer(help="VBC (Video Batch Compression) - Modular Version")

@app.command()
def compress(
    input_dirs_arg: Optional[str] = typer.Argument(
        None,
        help="Directory or comma-separated directories to compress (optional if set in config)"
    ),
    config_path: Optional[Path] = typer.Option(Path("conf/vbc.yaml"), "--config", "-c", help="Path to YAML config"),
    demo: bool = typer.Option(False, "--demo", help="Run in demo mode (simulate processing, no file IO)"),
    demo_config_path: Optional[Path] = typer.Option(Path("conf/demo.yaml"), "--demo-config", help="Path to demo YAML config"),
    threads: Optional[int] = typer.Option(None, "--threads", "-t", help="Override number of threads"),
    quality: Optional[int] = typer.Option(None, "--quality", help="Override quality (GPU CQ / CPU CRF, 0-63)"),
    gpu: Optional[bool] = typer.Option(None, "--gpu/--cpu", help="Enable/disable GPU acceleration"),
    queue_sort: Optional[str] = typer.Option(
        None,
        "--queue-sort",
        help="Queue sorting mode (name, rand, dir, size, size-asc, size-desc, ext)"
    ),
    queue_seed: Optional[int] = typer.Option(
        None,
        "--queue-seed",
        help="Seed for deterministic random queue sorting (rand mode)"
    ),
    log_path: Optional[Path] = typer.Option(
        None,
        "--log-path",
        help="Path to log file (overrides config)"
    ),
    clean_errors: bool = typer.Option(False, "--clean-errors", help="Remove existing .err markers and retry"),
    skip_av1: bool = typer.Option(False, "--skip-av1", help="Skip files already encoded in AV1"),
    min_size: Optional[int] = typer.Option(None, "--min-size", help="Minimum input size in bytes to process"),
    min_ratio: Optional[float] = typer.Option(None, "--min-ratio", help="Minimum compression ratio required (0.0-1.0)"),
    camera: Optional[str] = typer.Option(None, "--camera", help="Comma-separated list of camera models to filter"),
    rotate_180: bool = typer.Option(False, "--rotate-180", help="Rotate output 180 degrees"),
    debug: bool = typer.Option(False, "--debug/--no-debug", help="Enable verbose debug logging")
):
    """Batch compress videos in a directory with full feature parity."""
    cli_input_dirs = parse_cli_input_dirs(input_dirs_arg)

    try:
        config = load_config(config_path)
        # Validate queue_sort first if provided
        validated_queue_sort = None
        if queue_sort is not None:
            try:
                validated_queue_sort = validate_queue_sort(queue_sort, config.general.extensions)
            except ValueError as exc:
                typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)

        # Create CLI overrides object (for per-job config resolution)
        cli_overrides = CliConfigOverrides(
            threads=threads,
            quality=quality,
            gpu=gpu,
            queue_sort=validated_queue_sort,
            queue_seed=queue_seed,
            log_path=str(log_path) if log_path else None,
            clean_errors=clean_errors,
            skip_av1=skip_av1,
            min_size=min_size,
            min_ratio=min_ratio,
            camera=[c.strip() for c in camera.split(",") if c.strip()] if camera else None,
            debug=debug,
            rotate_180=rotate_180,
        )

        # Apply CLI overrides to global config
        if threads:
            config.general.threads = threads
        if quality is not None:
            config.gpu_encoder.common_args = replace_quality_value(config.gpu_encoder.common_args, quality)
            config.gpu_encoder.advanced_args = replace_quality_value(config.gpu_encoder.advanced_args, quality)
            config.cpu_encoder.common_args = replace_quality_value(config.cpu_encoder.common_args, quality)
            config.cpu_encoder.advanced_args = replace_quality_value(config.cpu_encoder.advanced_args, quality)
        if gpu is not None:
            config.general.gpu = gpu
        if validated_queue_sort is not None:
            config.general.queue_sort = validated_queue_sort
        if queue_seed is not None:
            config.general.queue_seed = queue_seed
        if log_path is not None:
            config.general.log_path = str(log_path)
        if clean_errors:
            config.general.clean_errors = True
        if skip_av1:
            config.general.skip_av1 = True
        if min_size is not None:
            config.general.min_size_bytes = min_size
        if min_ratio is not None:
            config.general.min_compression_ratio = min_ratio
        if camera:
            config.general.filter_cameras = [c.strip() for c in camera.split(",") if c.strip()]
        if debug:
            config.general.debug = True
        if rotate_180:
            config.general.manual_rotation = 180

        demo_config = load_demo_config(demo_config_path) if demo else None

        input_dir_status_entries: List[Tuple[str, str]] = []
        requested_input_dirs: List[str] = []
        output_dir_map: dict = {}
        errors_dir_map: dict = {}
        output_dirs_entries: List[str] = []
        errors_dirs_entries: List[str] = []
        suffix_output_dirs: Optional[str] = None
        suffix_errors_dirs: Optional[str] = None
        if not demo:
            if input_dirs_arg is not None:
                requested_input_dirs = cli_input_dirs
            else:
                requested_input_dirs = normalize_input_dir_entries(config.input_dirs or [])

            def align_dir_entries(dir_entries: List[str], label: str) -> List[str]:
                if not dir_entries:
                    return []
                unique_inputs = dedupe_preserve_order(requested_input_dirs)
                if len(dir_entries) == len(requested_input_dirs):
                    deduped_dirs: List[str] = []
                    seen_inputs = set()
                    for input_entry, dir_entry in zip(requested_input_dirs, dir_entries):
                        if input_entry in seen_inputs:
                            continue
                        seen_inputs.add(input_entry)
                        deduped_dirs.append(dir_entry)
                    return deduped_dirs
                if len(dir_entries) == len(unique_inputs):
                    return dir_entries
                typer.secho(
                    f"Error: {label} count must match input_dirs count.",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)

            output_dirs_entries = normalize_output_dir_entries(config.output_dirs or [])
            errors_dirs_entries = normalize_errors_dir_entries(config.errors_dirs or [])
            output_dirs_entries = align_dir_entries(output_dirs_entries, "output_dirs")
            errors_dirs_entries = align_dir_entries(errors_dirs_entries, "errors_dirs")
            requested_input_dirs = dedupe_preserve_order(requested_input_dirs)
            suffix_output_dirs = config.suffix_output_dirs
            suffix_errors_dirs = config.suffix_errors_dirs
            if input_dirs_arg is not None and not requested_input_dirs:
                typer.secho(
                    "Error: No input directories provided on the command line.",
                    fg=typer.colors.RED,
                    err=True
                )
                raise typer.Exit(code=1)
            if input_dirs_arg is None and not requested_input_dirs:
                typer.secho(
                    "Error: No input directories provided in CLI or config.",
                    fg=typer.colors.RED,
                    err=True
                )
                raise typer.Exit(code=1)
            try:
                validate_input_dir_entries(requested_input_dirs)
            except ValueError as exc:
                typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
            if output_dirs_entries and suffix_output_dirs:
                typer.secho(
                    "Error: output_dirs cannot be used with suffix_output_dirs.",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
            if not output_dirs_entries and not suffix_output_dirs:
                typer.secho(
                    "Error: suffix_output_dirs must be set when output_dirs is empty.",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
            if errors_dirs_entries and suffix_errors_dirs:
                typer.secho(
                    "Error: errors_dirs cannot be used with suffix_errors_dirs.",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
            if not errors_dirs_entries and not suffix_errors_dirs:
                typer.secho(
                    "Error: suffix_errors_dirs must be set when errors_dirs is empty.",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
            if output_dirs_entries:
                try:
                    validate_output_dirs(output_dirs_entries)
                except ValueError as exc:
                    typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
                    raise typer.Exit(code=1)
            if errors_dirs_entries:
                try:
                    validate_errors_dirs(errors_dirs_entries)
                except ValueError as exc:
                    typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
                    raise typer.Exit(code=1)
            config.output_dirs = output_dirs_entries
            config.errors_dirs = errors_dirs_entries
            input_dirs, input_dir_status_entries, output_dir_map, errors_dir_map = evaluate_input_dirs(
                requested_input_dirs,
                output_dirs=output_dirs_entries if output_dirs_entries else None,
                suffix_output_dirs=suffix_output_dirs if not output_dirs_entries else None,
                errors_dirs=errors_dirs_entries if errors_dirs_entries else None,
                suffix_errors_dirs=suffix_errors_dirs if not errors_dirs_entries else None,
            )
            if not input_dirs:
                typer.secho(
                    "Error: No valid input directories found (missing or inaccessible).",
                    fg=typer.colors.RED,
                    err=True
                )
                raise typer.Exit(code=1)
        else:
            input_dirs = []

        # Setup output directory and logging FIRST
        if demo:
            output_dir = Path("demo_out")
        else:
            output_dir = output_dir_map.get(input_dirs[0])
            if output_dir is None:
                output_dir = input_dirs[0].with_name(f"{input_dirs[0].name}_out")
        log_path_value = Path(config.general.log_path) if config.general.log_path else None
        logger = setup_logging(output_dir, debug=config.general.debug, log_path=log_path_value)
        if demo and demo_config:
            logger.info(
                f"VBC demo started: files={demo_config.files.count}, errors={demo_config.errors.total}"
            )
        else:
            logger.info(f"VBC started: input_folders={len(input_dirs)}, folders={input_dirs}")
        encoder_args = select_encoder_args(config, config.general.gpu)
        quality_value = extract_quality_value(encoder_args)
        quality_flag = extract_quality_flag(encoder_args)
        quality_label = "CQ" if quality_flag == "-cq" else "CRF" if quality_flag == "-crf" else "Q"
        quality_display = f"{quality_label}{quality_value}" if quality_value is not None else "unknown"
        logger.info(
            f"Config: threads={config.general.threads}, quality={quality_display}, "
            f"gpu={config.general.gpu}, debug={config.general.debug}"
        )

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

        def build_output_status_entries(entries: List[str]) -> List[Tuple[str, str]]:
            status_entries: List[Tuple[str, str]] = []
            for entry in entries:
                path = Path(entry)
                status = STATUS_OK if can_write_output_dir_path(path) else STATUS_NO_ACCESS
                status_entries.append((status, entry))
            return status_entries

        def build_initial_input_dir_stats(
            status_entries: List[Tuple[str, str]],
        ) -> List[Tuple[str, str, Optional[int], Optional[int]]]:
            stats: List[Tuple[str, str, Optional[int], Optional[int]]] = []
            for status, entry in status_entries:
                stats.append((status, entry, None, None))
            return stats


        preset_value = extract_preset(encoder_args) or "—"
        encoder_name = infer_encoder_label(encoder_args, config.general.gpu)
        output_suffix = output_extension_for_args(encoder_args)
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
        if config.general.queue_sort == "rand" and config.general.queue_seed is not None:
            queue_sort_info = f"rand (seed {config.general.queue_seed})"
        else:
            queue_sort_info = config.general.queue_sort

        def parse_demo_size_to_bytes(size_str: Optional[str]) -> Optional[int]:
            """Parse demo size string (e.g., '12.5GB', '100MB') to bytes."""
            if not size_str:
                return None
            size_str = size_str.strip().upper()
            import re
            match = re.match(r'^([\d.]+)\s*(GB|MB|TB|KB)?$', size_str)
            if not match:
                return None
            value = float(match.group(1))
            unit = match.group(2) or "B"
            multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
            return int(value * multipliers.get(unit, 1))

        def build_demo_input_dir_stats() -> Tuple[List[Tuple[str, str, Optional[int], Optional[int]]], List[str]]:
            """Build demo input_dir_stats and input_dir_lines from demo_config.input_folders."""
            stats: List[Tuple[str, str, Optional[int], Optional[int]]] = []
            lines: List[str] = []

            for idx, folder_entry in enumerate(demo_config.input_folders):
                if isinstance(folder_entry, str):
                    # Old format: simple string
                    folder_name = folder_entry
                    status = STATUS_OK
                    files_count = None
                    size_bytes = None
                elif isinstance(folder_entry, DemoInputFolder):
                    # New format: DemoInputFolder with mockup data
                    folder_name = folder_entry.name
                    status_str = folder_entry.status or "ok"
                    if status_str == "ok":
                        status = STATUS_OK
                    elif status_str == "nonexist":
                        status = STATUS_MISSING
                    elif status_str == "norw":
                        status = STATUS_NO_ACCESS
                    else:
                        status = STATUS_OK
                    files_count = folder_entry.files
                    size_bytes = parse_demo_size_to_bytes(folder_entry.size)
                else:
                    # Fallback
                    folder_name = str(folder_entry)
                    status = STATUS_OK
                    files_count = None
                    size_bytes = None

                stats.append((status, folder_name, files_count, size_bytes))

                # Build display line with status icon
                from vbc.config.input_dirs import render_status_icon
                icon = render_status_icon(status)
                lines.append(f"  {icon}{idx + 1}. {folder_name}")

            return stats, lines

        if demo and demo_config:
            demo_extensions = [entry.ext for entry in demo_config.files.extensions]
            demo_dir_stats, demo_dir_lines = build_demo_input_dir_stats()
            input_dir_count = len(demo_dir_stats)
            input_dir_lines = demo_dir_lines
        else:
            demo_extensions = config.general.extensions
            input_dir_count = len(input_dir_status_entries)
            input_dir_lines = build_input_dir_lines(input_dir_status_entries)
        output_dir_status_entries: List[Tuple[str, str]] = []
        errors_dir_status_entries: List[Tuple[str, str]] = []
        if output_dirs_entries:
            output_dir_status_entries = build_output_status_entries(output_dirs_entries)
        if errors_dirs_entries:
            errors_dir_status_entries = build_output_status_entries(errors_dirs_entries)

        output_dir_lines = build_input_dir_lines(output_dir_status_entries) if output_dir_status_entries else []
        errors_dir_lines = build_input_dir_lines(errors_dir_status_entries) if errors_dir_status_entries else []
        ui_suffix_output_dirs = None if output_dirs_entries else suffix_output_dirs
        ui_suffix_errors_dirs = None if errors_dirs_entries else suffix_errors_dirs
        extensions = [ext if ext.startswith(".") else f".{ext}" for ext in demo_extensions]
        if demo and demo_config:
            input_dir_stats = demo_dir_stats
        else:
            input_dir_stats = build_initial_input_dir_stats(input_dir_status_entries)
        ext_list = ", ".join(extensions)

        if demo and demo_config:
            ui_state.config_lines = [
                "Video Batch Compression - demo",
                f"Start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"Input folders: {input_dir_count}",
                *input_dir_lines,
                f"Threads: {config.general.threads} (Prefetch: {config.general.prefetch_factor}x)",
                f"Encoder: {encoder_name} | Preset: {preset_value}",
                "Audio: Auto (lossless->AAC 256k, AAC/MP3 copy, other->AAC 192k)",
                f"Quality: {quality_display} (Default)",
                f"Dynamic CQ: {dynamic_cq_info}",
                f"Camera Filter: {camera_filter_info}",
                f"Metadata: {metadata_method} (Analysis: {config.general.use_exif})",
                f"Autorotate: {len(config.autorotate.patterns)} rules loaded",
                f"Manual Rotation: {manual_rotation}",
                f"Extensions: {ext_list} → {output_suffix}",
                f"Queue sort: {queue_sort_info}",
                f"CPU fallback: {config.general.cpu_fallback} | CPU threads per worker: {config.general.ffmpeg_cpu_threads or 'auto'}",
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
                f"Input folders: {input_dir_count}",
                *input_dir_lines,
                f"Threads: {config.general.threads} (Prefetch: {config.general.prefetch_factor}x)",
                f"Encoder: {encoder_name} | Preset: {preset_value}",
                "Audio: Auto (lossless->AAC 256k, AAC/MP3 copy, other->AAC 192k)",
                f"Quality: {quality_display} (Default)",
                f"Dynamic CQ: {dynamic_cq_info}",
                f"Camera Filter: {camera_filter_info}",
                f"Metadata: {metadata_method} (Analysis: {config.general.use_exif})",
                f"Autorotate: {len(config.autorotate.patterns)} rules loaded",
                f"Manual Rotation: {manual_rotation}",
                f"Extensions: {ext_list} → {output_suffix}",
                f"Queue sort: {queue_sort_info}",
                f"CPU fallback: {config.general.cpu_fallback} | CPU threads per worker: {config.general.ffmpeg_cpu_threads or 'auto'}",
                f"Min size: {format_size(config.general.min_size_bytes)} | Skip AV1: {config.general.skip_av1}",
                f"Clean errors: {config.general.clean_errors} | Strip Unicode: {config.general.strip_unicode_display}",
                f"Debug logging: {config.general.debug}",
            ]

        ui_state.io_input_dir_stats = input_dir_stats
        ui_state.io_output_dir_lines = output_dir_lines
        ui_state.io_errors_dir_lines = errors_dir_lines
        ui_state.io_suffix_output_dirs = ui_suffix_output_dirs
        ui_state.io_suffix_errors_dirs = ui_suffix_errors_dirs
        ui_state.io_queue_sort = config.general.queue_sort
        ui_state.io_queue_seed = config.general.queue_seed
        ui_state.log_path = config.general.log_path
        ui_state.debug_enabled = config.general.debug

        if config.general.filter_cameras and not config.general.use_exif:
            logger.warning("Camera filtering requires EXIF analysis. Enabling use_exif automatically.")
            config.general.use_exif = True

        UIManager(bus, ui_state, demo_mode=demo)

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
                output_dir = output_dir_map.get(input_dir)
                if output_dir is None and config.suffix_output_dirs:
                    output_dir = input_dir.with_name(f"{input_dir.name}{config.suffix_output_dirs}")
                errors_dir = errors_dir_map.get(input_dir)
                if errors_dir is None and config.suffix_errors_dirs:
                    errors_dir = input_dir.with_name(f"{input_dir.name}{config.suffix_errors_dirs}")
                if output_dir and errors_dir:
                    housekeeper.cleanup_output_markers(
                        input_dir=input_dir,
                        output_dir=output_dir,
                        errors_dir=errors_dir,
                        clean_errors=config.general.clean_errors,
                        logger=logger,
                    )

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

            # Create local config registry
            local_registry = LocalConfigRegistry()

            orchestrator = Orchestrator(
                config=config,
                event_bus=bus,
                file_scanner=scanner,
                exif_adapter=exif,
                ffprobe_adapter=ffprobe,
                ffmpeg_adapter=ffmpeg,
                output_dir_map=output_dir_map,
                local_config_registry=local_registry,
                cli_overrides=cli_overrides,
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

        processing_finished = False
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
                processing_finished = True
        finally:
            keyboard.stop()
            if gpu_monitor:
                gpu_monitor.stop()
            # Cleanup ExifTool
            if exif and exif.et.running:
                exif.et.terminate()
                logger.info("ExifTool terminated")
            if processing_finished and not demo and errors_dir_map:
                error_entries = collect_error_entries(
                    input_dirs,
                    output_dir_map,
                    errors_dir_map,
                )
                moved_files = []
                if error_entries:
                    if len(error_entries) > 100:
                        confirm = typer.confirm(
                            f"Found {len(error_entries)} .err files. Move failed files to errors dirs?",
                            default=False,
                        )
                        if not confirm:
                            logger.info("Skipping failed file relocation (user declined).")
                            error_entries = []
                    if error_entries:
                        moved_files = move_failed_files(
                            input_dirs,
                            output_dir_map,
                            errors_dir_map,
                            config.general.extensions,
                            logger=logger,
                            error_entries=error_entries,
                        )

                if config.general.repair_corrupted_flv and moved_files:
                    process_repairs(
                        input_dirs,
                        errors_dir_map,
                        config.general.extensions,
                        logger=logger,
                        target_files=moved_files,
                    )

            # Warning for files skipped because they were already encoded by VBC
            if not demo and orchestrator and getattr(orchestrator, "skipped_vbc_count", 0) > 0:
                from rich.console import Console
                console = Console()
                console.print(f"\n[bold yellow]Warning: {orchestrator.skipped_vbc_count} files were skipped because they were already encoded by VBC.[/bold yellow]")

    except KeyboardInterrupt:
        # Ctrl+C was already handled by orchestrator - just exit gracefully
        typer.secho("\n✓ Compression stopped by user (Ctrl+C)", fg=typer.colors.YELLOW)
        raise typer.Exit(code=130)

    except typer.Exit:
        raise

    except Exception as e:
        with open("error.log", "a") as f:
            import traceback
            traceback.print_exc(file=f)
        typer.secho(f"Fatal Error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

if __name__ == "__main__":
    app()
