"""
VBC Modernized Overlays
=======================
Nowoczesny, estetyczny design dla paneli CONFIG, REFERENCE (dawniej LEGEND), SHORTCUTS (dawniej MENU), I/O i TUI.
Używa Rich library z kartami, tabelami i hierarchiczną strukturą.

Koncepcja:
- Prefs (C) - konfiguracja sesji w kartach tematycznych
- Ref (E) - legenda statusów i symboli
- Keys (M) - skróty klawiszowe z podziałem funkcjonalnym
- I/O (F) - foldery i ustawienia kolejki
- TUI (T) - ustawienia interfejsu terminalowego

Wszystkie panele zachowują 100% obecnej funkcjonalności, ale prezentują
ją w bardziej przejrzysty i nowoczesny sposób.
"""

from typing import List, Optional, Tuple
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich.box import ROUNDED
from vbc.config.input_dirs import render_status_icon
from vbc.ui.gpu_sparkline import (
    DEFAULT_GPU_SPARKLINE_PALETTE,
    DEFAULT_GPU_SPARKLINE_PRESET,
    build_cycle_text,
    build_palette_swatches,
    build_scale_entries,
    format_preset_label,
    get_gpu_sparkline_config,
    get_gpu_sparkline_palette,
    list_gpu_sparkline_palettes,
    list_gpu_sparkline_presets,
)


# ═══════════════════════════════════════════════════════════════════════════════
# STAŁE I STYLE
# ═══════════════════════════════════════════════════════════════════════════════

# Kolory motywu (GitHub Dark inspired)
COLORS = {
    'accent_green': '#3fb950',
    'accent_blue': '#58a6ff',
    'accent_orange': '#f0883e',
    'accent_purple': '#a371f7',
    'accent_cyan': '#79c0ff',
    'error_red': '#f85149',
    'warning_yellow': '#d29922',
    'muted': '#8b949e',
    'dim': '#6e7681',
    'border': '#30363d',
    'surface': '#161b22',
    'background': '#0d1117',
}

# Ikony sekcji
ICONS = {
    'encoding': '🎬',
    'processing': '⚡',
    'io': '📁',
    'quality': '🎯',
    'metadata': '📋',
    'logging': '📝',
    'status': '◆',
    'spinners': '◈',
    'gpu': '◈',
    'nav': '▸',
    'panels': '▸',
    'jobs': '▸',
    'tui': '◈',
}


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def make_card(title: str, content: RenderableType, icon: str = "", 
              title_color: str = "cyan", width: Optional[int] = None) -> Panel:
    """Tworzy estetyczną kartę z tytułem i zawartością."""
    title_text = f"{icon} {title}" if icon else title
    return Panel(
        content,
        title=f"[bold {title_color}]{title_text}[/]",
        title_align="left",
        border_style=COLORS['border'],
        box=ROUNDED,
        padding=(0, 1),
        width=width,
    )


def make_kv_table(rows: List[tuple], highlight_keys: set = None) -> Table:
    """Tworzy tabelę klucz-wartość dla sekcji konfiguracji."""
    highlight_keys = highlight_keys or set()
    
    table = Table(
        show_header=False,
        box=None,
        padding=(0, 1),
        expand=True,
    )
    table.add_column("Key", style=COLORS['muted'], no_wrap=True)
    table.add_column("Value", justify="right", overflow="fold")
    
    for key, value in rows:
        if key in highlight_keys:
            value_style = f"bold {COLORS['accent_green']}"
        elif value in ("None", "False", "0", "—"):
            value_style = COLORS['dim']
        else:
            value_style = "white"
        table.add_row(key, f"[{value_style}]{value}[/]")
    
    return table


def make_two_column_layout(left: RenderableType, right: RenderableType) -> Table:
    """Tworzy layout dwukolumnowy z równymi kolumnami."""
    table = Table(show_header=False, box=None, expand=True, padding=0)
    table.add_column(ratio=1)
    table.add_column(width=1)  # spacer
    table.add_column(ratio=1)
    table.add_row(left, "", right)
    return table


def make_shortcut_row(key: str, description: str, key_color: str = "white") -> Table:
    """Tworzy wiersz skrótu klawiszowego."""
    table = Table(show_header=False, box=None, padding=0, expand=True)
    table.add_column(width=12)
    table.add_column()
    
    key_badge = f"[bold {key_color} on {COLORS['border']}] {key} [/]"
    table.add_row(key_badge, description)
    return table


def parse_config_lines(lines: List[str]) -> dict:
    """Parsuje config_lines do słownika."""
    result = {}
    for line in lines:
        if ": " in line:
            parts = line.split(": ", 1)
            key = parts[0].strip()
            value = parts[1].strip() if len(parts) > 1 else ""
            result[key.lower().replace(" ", "_")] = value
    return result


def format_size(size_bytes: Optional[int]) -> str:
    """Format size: 123B, 1.2KB, 45.1MB, 3.2GB."""
    if size_bytes is None:
        return "—"
    if size_bytes == 0:
        return "0B"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0:
            return f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}PB"


# ═══════════════════════════════════════════════════════════════════════════════
# SETTINGS OVERLAY (dawniej CONFIG)
# ═══════════════════════════════════════════════════════════════════════════════

class SettingsOverlay:
    """
    Panel ustawień sesji - wyświetla konfigurację w kartach tematycznych.
    
    Karty:
    - ENCODING: encoder, preset, quality, audio, fallback
    - PROCESSING: threads, prefetch, queue sort, cpu threads
    - QUALITY & FILTERS: dynamic quality, camera filter, skip AV1, rotation
    - LOGGING: log path, debug flags
    - METADATA & CLEANUP: exiftool, analysis, autorotate, cleanup flags
    """
    
    def __init__(
        self,
        config_lines: List[str],
        spinner_frame: int = 0,
        log_path: Optional[str] = None,
        debug_enabled: bool = False,
    ):
        self.config_lines = config_lines
        self.spinner_frame = spinner_frame
        self.log_path = log_path
        self.debug_enabled = debug_enabled
        self._parsed = parse_config_lines(config_lines)
    
    def _get(self, key: str, default: str = "—") -> str:
        """Pobiera wartość z parsowanej konfiguracji."""
        return self._parsed.get(key, default)
    
    def _render_content(self) -> Group:
        """Returns content without outer Panel or footer (for tabbed overlay)."""
        # === ENCODING CARD ===
        quality_raw = self._get("quality", "")
        quality_token = quality_raw.split()[0] if quality_raw else "—"
        encoding_data = [
            ("Encoder", self._get("encoder", "").split(" | ")[0] if "encoder" in self._parsed else "—"),
            ("Preset", self._get("encoder", "").split("Preset: ")[-1] if "Preset:" in self._get("encoder", "") else "—"),
            ("Quality", quality_token),
            ("Audio", self._get("audio", "Auto (lossless->AAC 256k, AAC/MP3 copy, other->AAC 192k)")),
            ("CPU Fallback", self._get("cpu_fallback", "").split(" | ")[0] if "cpu_fallback" in self._parsed else "False"),
        ]
        encoding_card = make_card(
            "ENCODING", 
            make_kv_table(encoding_data, {"Encoder", "Quality"}),
            icon=ICONS['encoding'],
            title_color=COLORS['accent_blue']
        )
        
        # === PROCESSING CARD ===
        threads_info = self._get("threads", "1")
        prefetch = threads_info.split("(Prefetch: ")[-1].rstrip(")") if "Prefetch:" in threads_info else "1x"
        threads = threads_info.split(" ")[0] if threads_info else "1"
        
        queue_sort = self._get("queue_sort", "name")
        cpu_threads = self._get("cpu_fallback", "").split("CPU threads per worker: ")[-1] if "CPU threads" in self._get("cpu_fallback", "") else "auto"
        
        processing_data = [
            ("Threads", threads),
            ("Prefetch", prefetch),
            ("Queue Sort", queue_sort),
            ("CPU Threads", cpu_threads),
        ]
        processing_card = make_card(
            "PROCESSING",
            make_kv_table(processing_data, {"Threads"}),
            icon=ICONS['processing'],
            title_color=COLORS['accent_blue']
        )
        
        # === LOGGING CARD ===
        log_path = self.log_path or "—"
        debug = "True" if self.debug_enabled else "False"
        logging_data = [
            ("Log Path", log_path),
            ("Debug", debug),
        ]
        logging_card = make_card(
            "LOGGING",
            make_kv_table(logging_data, {"Log Path"}),
            icon=ICONS['logging'],
            title_color=COLORS['accent_blue']
        )
        
        # === QUALITY & FILTERS CARD ===
        dynamic_quality = self._get("dynamic_quality", "None")
        camera_filter = self._get("camera_filter", "None")
        skip_av1 = self._get("min_size", "").split("Skip AV1: ")[-1] if "Skip AV1:" in self._get("min_size", "") else "False"
        manual_rotation = self._get("manual_rotation", "None")
        
        quality_data = [
            ("Dynamic Quality", dynamic_quality if dynamic_quality else "None"),
            ("Camera Filter", camera_filter),
            ("Skip AV1", skip_av1),
            ("Rotation", manual_rotation),
        ]
        quality_card = make_card(
            "QUALITY & FILTERS",
            make_kv_table(quality_data, {"Dynamic Quality"}),
            icon=ICONS['quality'],
            title_color=COLORS['accent_blue']
        )
        
        # === METADATA & CLEANUP CARD (full width) ===
        metadata = self._get("metadata", "")
        analysis = "True" if "(Analysis: True)" in metadata else "False"
        metadata_method = metadata.split(" (")[0] if " (" in metadata else metadata
        autorotate = self._get("autorotate", "0 rules")
        clean_errors = self._get("clean_errors", "").split(" | ")[0] if "clean_errors" in self._parsed else "False"
        strip_unicode = self._get("clean_errors", "").split("Strip Unicode: ")[-1] if "Strip Unicode:" in self._get("clean_errors", "") else "True"
        
        meta_table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        meta_table.add_column(style=COLORS['muted'], ratio=1)
        meta_table.add_column(justify="right", ratio=1)
        meta_table.add_column(style=COLORS['muted'], ratio=1)
        meta_table.add_column(justify="right", ratio=1)
        meta_table.add_column(style=COLORS['muted'], ratio=1)
        meta_table.add_column(justify="right", ratio=1)
        
        meta_table.add_row(
            "Metadata", f"[white]{metadata_method}[/]",
            "Analysis", f"[bold {COLORS['accent_green']}]{analysis}[/]" if analysis == "True" else f"[{COLORS['dim']}]{analysis}[/]",
            "Autorotate", f"[white]{autorotate}[/]"
        )
        meta_table.add_row(
            "Clean Errors", f"[{COLORS['dim'] if clean_errors == 'False' else 'white'}]{clean_errors}[/]",
            "Strip Unicode", f"[white]{strip_unicode}[/]",
            "", ""
        )
        
        metadata_card = make_card(
            "METADATA & CLEANUP",
            meta_table,
            icon=ICONS['metadata'],
            title_color=COLORS['accent_blue']
        )
        
        # === LAYOUT ===
        # Left column: Encoding -> Logging -> Processing
        # Right column: Quality & Filters at top
        left_column = Group(
            encoding_card,
            logging_card,
            processing_card,
        )
        top_row = make_two_column_layout(left_column, quality_card)

        # Build content Group
        content = Group(
            top_row,
            metadata_card,
        )

        return content

    def render(self) -> Panel:
        """Returns complete Panel with footer (for backward compatibility)."""
        footer = Text.from_markup(
            f"[{COLORS['dim']}]Press [white on {COLORS['border']}] Esc [/] close • "
            f"[white on {COLORS['border']}] E [/] Ref • "
            f"[white on {COLORS['border']}] M [/] Keys[/]",
            justify="center"
        )

        content_with_footer = Group(
            self._render_content(),
            "",
            footer
        )

        return Panel(
            content_with_footer,
            title="[bold white]⚙ PREFS[/]",
            subtitle=f"[{COLORS['dim']}][C] to toggle[/]",
            border_style=COLORS['accent_green'],
            box=ROUNDED,
            padding=(1, 2),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# I/O OVERLAY
# ═══════════════════════════════════════════════════════════════════════════════

class IoOverlay:
    """Panel I/O - foldery i ustawienia kolejkowania."""

    def __init__(
        self,
        config_lines: List[str],
        input_dir_stats: List[Tuple[str, str, Optional[int], Optional[int]]],
        output_dir_lines: List[str],
        errors_dir_lines: List[str],
        suffix_output_dirs: Optional[str],
        suffix_errors_dirs: Optional[str],
        queue_sort: str,
        queue_seed: Optional[int],
    ):
        self.config_lines = config_lines
        self.input_dir_stats = input_dir_stats
        self.output_dir_lines = output_dir_lines
        self.errors_dir_lines = errors_dir_lines
        self.suffix_output_dirs = suffix_output_dirs
        self.suffix_errors_dirs = suffix_errors_dirs
        self.queue_sort = queue_sort
        self.queue_seed = queue_seed
        self._parsed = parse_config_lines(config_lines)

    def _get(self, key: str, default: str = "—") -> str:
        """Pobiera wartosc z parsowanej konfiguracji."""
        return self._parsed.get(key, default)

    def _render_dir_card(self, title: str, lines: List[str], suffix: Optional[str]) -> Panel:
        content_lines: List[str] = []
        if suffix:
            content_lines.append(f"[{COLORS['muted']}]Suffix[/]: [white]{suffix}[/]")
            if lines:
                content_lines.append("")
        if lines:
            content_lines.extend(lines)
        elif not suffix:
            content_lines.append(f"[{COLORS['dim']}]None[/]")
        return make_card(
            title,
            "\n".join(content_lines),
            icon=ICONS['io'],
            title_color=COLORS['accent_blue'],
        )

    def _render_content(self) -> Group:
        """Returns content without outer Panel or footer (for tabbed overlay).

        Shows INPUT/OUTPUT summary and QUEUE settings only.
        Directory listings are managed in the Dirs [D] tab.
        """
        # === INPUT/OUTPUT CARD ===
        input_folders = self._get("input_folders", "1")
        extensions = self._get("extensions", ".mp4, .mov, .avi")
        min_size = self._get("min_size", "1.0MB").split(" | ")[0] if "min_size" in self._parsed else "1.0MB"

        io_data = [
            ("Input Folders", input_folders),
            ("Extensions", extensions.split(" → ")[0] if " → " in extensions else extensions),
            ("Output", extensions.split(" → ")[-1] if " → " in extensions else ".mp4"),
            ("Min Size", min_size),
        ]
        io_card = make_card(
            "INPUT / OUTPUT",
            make_kv_table(io_data, {"Output"}),
            icon=ICONS['io'],
            title_color=COLORS['accent_blue']
        )

        # === QUEUE CARD ===
        queue_sort = self.queue_sort or "—"
        queue_seed = str(self.queue_seed) if self.queue_seed is not None else "—"
        queue_data = [
            ("Queue Sort", queue_sort),
            ("Queue Seed", queue_seed),
        ]
        queue_card = make_card(
            "QUEUE",
            make_kv_table(queue_data, {"Queue Sort"}),
            icon=ICONS['processing'],
            title_color=COLORS['accent_blue']
        )

        # === LAYOUT ===
        row1 = make_two_column_layout(io_card, queue_card)

        hint = Text.from_markup(
            f"[{COLORS['dim']}]Use [white on {COLORS['border']}] D [/] to manage input/output directories[/]",
            justify="center",
        )

        return Group(row1, hint)

    def render(self) -> Panel:
        """Returns complete Panel with footer (for backward compatibility)."""
        footer = Text.from_markup(
            f"[{COLORS['dim']}]Press [white on {COLORS['border']}] Esc [/] close • "
            f"[white on {COLORS['border']}] C [/] Prefs • "
            f"[white on {COLORS['border']}] E [/] Ref[/]",
            justify="center"
        )

        content_with_footer = Group(
            self._render_content(),
            "",
            footer
        )

        return Panel(
            content_with_footer,
            title="[bold white]📁 I/O[/]",
            subtitle=f"[{COLORS['dim']}][F] to toggle[/]",
            border_style=COLORS['accent_blue'],
            box=ROUNDED,
            padding=(1, 2),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# REFERENCE OVERLAY (dawniej LEGEND)
# ═══════════════════════════════════════════════════════════════════════════════

class ReferenceOverlay:
    """
    Panel referencyjny - legenda statusów, spinnerów i GPU graph.
    
    Sekcje:
    - STATUS CODES: fail, err, hw_cap, skip, kept, small, av1, cam + symbole wyniku
    - ACTIVE JOB INDICATORS: animowane spinnery (normalny vs rotation)
    - GPU GRAPH: metryki, skale, symbole sparkline
    """
    
    def __init__(
        self,
        spinner_frame: int = 0,
        sparkline_preset: Optional[str] = None,
        sparkline_palette: Optional[str] = None,
        sparkline_mode: str = "sparkline",
    ):
        self.spinner_frame = spinner_frame
        self.sparkline_preset = sparkline_preset
        self.sparkline_palette = sparkline_palette
        self.sparkline_mode = sparkline_mode

    def _render_content(self) -> Group:
        """Returns content without outer Panel or footer (for tabbed overlay)."""

        # === STATUS CODES ===
        status_left = Table(show_header=False, box=None, padding=(0, 1))
        status_left.add_column("Code", width=8)
        status_left.add_column("Description")
        
        status_left.add_row(
            f"[bold {COLORS['error_red']}]fail[/]",
            f"[{COLORS['muted']}]Session errors (FFmpeg crash, no space)[/]"
        )
        status_left.add_row(
            f"[bold {COLORS['error_red']}]err[/]",
            f"[{COLORS['muted']}]Historic errors (.err file on disk)[/]"
        )
        status_left.add_row(
            f"[bold {COLORS['warning_yellow']}]hw_cap[/]",
            f"[{COLORS['muted']}]Out of NVENC encoder slots[/]"
        )
        status_left.add_row(
            f"[bold {COLORS['warning_yellow']}]skip[/]",
            f"[{COLORS['muted']}]Already AV1 or camera mismatch[/]"
        )
        
        status_right = Table(show_header=False, box=None, padding=(0, 1))
        status_right.add_column("Code", width=8)
        status_right.add_column("Description")
        
        status_right.add_row(
            f"[{COLORS['muted']}]kept[/]",
            f"[{COLORS['muted']}]Original kept (low compression)[/]"
        )
        status_right.add_row(
            f"[{COLORS['muted']}]small[/]",
            f"[{COLORS['muted']}]Below min-size threshold[/]"
        )
        status_right.add_row(
            f"[{COLORS['muted']}]av1[/]",
            f"[{COLORS['muted']}]Already AV1 codec[/]"
        )
        status_right.add_row(
            f"[{COLORS['muted']}]cam[/]",
            f"[{COLORS['muted']}]Camera model filtered out[/]"
        )
        
        status_columns = make_two_column_layout(status_left, status_right)
        
        # Result symbols row
        symbols_table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
        symbols_table.add_column(justify="center", ratio=1)
        symbols_table.add_column(justify="center", ratio=1)
        symbols_table.add_column(justify="center", ratio=1)
        symbols_table.add_column(justify="center", ratio=1)
        symbols_table.add_row(
            f"[{COLORS['accent_green']}]✓[/] Success",
            f"[{COLORS['error_red']}]✗[/] Error",
            f"[{COLORS['muted']}]≡[/] Kept",
            f"[{COLORS['error_red']}]⚡[/] Interrupted"
        )
        
        status_content = Group(
            status_columns,
            Rule(style=COLORS['border']),
            symbols_table
        )
        
        status_card = Panel(
            status_content,
            title=f"[bold {COLORS['accent_orange']}]{ICONS['status']} STATUS CODES[/]",
            title_align="left",
            border_style=COLORS['border'],
            box=ROUNDED,
            padding=(0, 1),
        )
        
        # === ACTIVE JOB INDICATORS ===
        spinner_frames = "●○◉◎"
        spinner_rotating = "◐◓◑◒"
        spinner_custom = "◍◌"
        spinners_table = Table(show_header=False, box=None, padding=(0, 1))
        spinners_table.add_column(width=12)
        spinners_table.add_column()

        spinners_table.add_row(
            f"[{COLORS['accent_green']}]{' '.join(spinner_frames)}[/]",
            "Normal processing"
        )
        spinners_table.add_row(
            f"[{COLORS['accent_green']}]{' '.join(spinner_rotating)}[/]",
            "Video rotation applied"
        )
        spinners_table.add_row(
            f"[{COLORS['accent_green']}]{' '.join(spinner_custom)}[/]",
            "Custom camera settings"
        )
        
        spinners_card = Panel(
            spinners_table,
            title=f"[bold {COLORS['accent_purple']}]{ICONS['spinners']} ACTIVE JOB INDICATORS[/]",
            title_align="left",
            border_style=COLORS['border'],
            box=ROUNDED,
            padding=(0, 1),
        )
        
        # === GPU GRAPH ===
        spark_cfg = get_gpu_sparkline_config(self.sparkline_preset)
        cycle_text = build_cycle_text(spark_cfg.metrics) or "—"
        scale_entries = build_scale_entries(spark_cfg.metrics)
        scales_text = " • ".join(scale_entries) if scale_entries else "—"

        gpu_content = []
        gpu_content.append(f"[{COLORS['muted']}]Cycle: [white]{cycle_text}[/][/]")
        gpu_content.append("")
        gpu_content.append(f"[{COLORS['dim']}]Scales:[/]")
        gpu_content.append(f"  [{COLORS['muted']}]{scales_text}[/]")
        gpu_content.append("")
        gpu_content.append(f"[{COLORS['dim']}]Time:[/] [{COLORS['muted']}]left=older, right=newer (5min window)[/]")
        
        gpu_card = Panel(
            "\n".join(gpu_content),
            title=f"[bold {COLORS['accent_cyan']}]{ICONS['gpu']} GPU GRAPH[/] [{COLORS['dim']}][G][/]",
            title_align="left",
            border_style=COLORS['border'],
            box=ROUNDED,
            padding=(0, 1),
        )
        
        # === LAYOUT ===
        bottom_row = make_two_column_layout(spinners_card, gpu_card)

        # Build content without footer (footer is in tabbed overlay now)
        content = Group(
            status_card,
            bottom_row,
        )

        return content

    def render(self) -> Panel:
        """Returns complete Panel with footer (for backward compatibility)."""
        footer = Text.from_markup(
            f"[{COLORS['dim']}]Press [white on {COLORS['border']}] Esc [/] close • "
            f"[white on {COLORS['border']}] C [/] Prefs • "
            f"[white on {COLORS['border']}] M [/] Keys[/]",
            justify="center"
        )

        content_with_footer = Group(
            self._render_content(),
            "",
            footer
        )

        return Panel(
            content_with_footer,
            title="[bold white]📖 REF[/]",
            subtitle=f"[{COLORS['dim']}][E] to toggle[/]",
            border_style=COLORS['accent_orange'],
            box=ROUNDED,
            padding=(1, 2),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SHORTCUTS OVERLAY (dawniej MENU)
# ═══════════════════════════════════════════════════════════════════════════════

class ShortcutsOverlay:
    """
    Panel skrótów klawiszowych - pogrupowane tematycznie.
    
    Grupy:
    - NAVIGATION: M, Esc, Ctrl+C
    - PANELS: C, E, L, G
    - JOB CONTROL: S, R, </>, </>
    + Quick Reference z kolorowymi badge'ami
    """

    def _render_content(self) -> Group:
        """Returns content without outer Panel or footer (for tabbed overlay)."""

        key_labels = [
            "M", "Esc", "Ctrl+C",
            "C", "F", "D", "E", "L", "T", "I", "W", "P", "G",
            "[", "]", "S", "R", "< ,", "> .",
            "< >", "S", "R",
        ]
        badge_width = max(len(label) for label in key_labels)

        def key_badge(label: str, color: str = COLORS['border']) -> str:
            return f"[bold white on {color}] {label.center(badge_width)} [/]"

        # === NAVIGATION ===
        nav_table = Table(show_header=False, box=None, padding=(0, 0))
        nav_table.add_column(width=14)
        nav_table.add_column()
        
        nav_table.add_row(
            key_badge("M"),
            "Toggle Keys tab"
        )
        nav_table.add_row(
            key_badge("Esc"),
            "Close any overlay"
        )
        nav_table.add_row(
            key_badge("Ctrl+C"),
            "Immediate interrupt & exit"
        )
        
        nav_card = Panel(
            nav_table,
            title=f"[bold {COLORS['accent_green']}]{ICONS['nav']} NAVIGATION[/]",
            title_align="left",
            border_style=COLORS['border'],
            box=ROUNDED,
            padding=(0, 1),
        )
        
        # === PANELS ===
        panels_table = Table(show_header=False, box=None, padding=(0, 0))
        panels_table.add_column(width=14)
        panels_table.add_column()
        
        panels_table.add_row(
            key_badge("C"),
            "Configuration details"
        )
        panels_table.add_row(
            key_badge("F"),
            "Files: I/O summary & queue"
        )
        panels_table.add_row(
            key_badge("D"),
            "Dirs: manage input directories"
        )
        panels_table.add_row(
            key_badge("T"),
            "TUI settings"
        )
        panels_table.add_row(
            key_badge("E"),
            "Ref: legend & symbols"
        )
        panels_table.add_row(
            key_badge("L"),
            "Session logs (errors)"
        )
        panels_table.add_row(
            key_badge("I"),
            "Cycle overlay dim level"
        )
        panels_table.add_row(
            key_badge("W"),
            "Cycle GPU sparkline preset"
        )
        panels_table.add_row(
            key_badge("P"),
            "Cycle GPU sparkline palette"
        )
        panels_table.add_row(
            key_badge("G"),
            "Rotate GPU metric graph"
        )
        panels_table.add_row(
            key_badge("["),
            "Logs: previous page"
        )
        panels_table.add_row(
            key_badge("]"),
            "Logs: next page"
        )
        
        panels_card = Panel(
            panels_table,
            title=f"[bold {COLORS['accent_cyan']}]{ICONS['panels']} PANELS[/]",
            title_align="left",
            border_style=COLORS['border'],
            box=ROUNDED,
            padding=(0, 1),
        )
        
        # === JOB CONTROL (compact, stacked under NAVIGATION) ===
        jobs_table = Table(show_header=False, box=None, padding=(0, 0))
        jobs_table.add_column(width=14)
        jobs_table.add_column()

        jobs_table.add_row(
            key_badge("S"),
            "Shutdown toggle (graceful)"
        )
        jobs_table.add_row(
            key_badge("R"),
            "Refresh queue (re-scan)"
        )
        jobs_table.add_row(
            key_badge("< ,"),
            "Decrease thread count"
        )
        jobs_table.add_row(
            key_badge("> ."),
            "Increase thread count"
        )
        
        jobs_card = Panel(
            jobs_table,
            title=f"[bold {COLORS['accent_orange']}]{ICONS['jobs']} JOB CONTROL[/]",
            title_align="left",
            border_style=COLORS['border'],
            box=ROUNDED,
            padding=(0, 1),
        )
        
        # === QUICK REFERENCE ===
        quick_ref = Table(show_header=False, box=None, padding=(0, 2), expand=True)
        quick_ref.add_column(justify="center", ratio=1)
        quick_ref.add_column(justify="center", ratio=1)
        quick_ref.add_column(justify="center", ratio=1)
        
        quick_ref.add_row(
            f"{key_badge('< >', COLORS['accent_green'])} Threads",
            f"{key_badge('S', COLORS['warning_yellow'])} Shutdown",
            f"{key_badge('R', COLORS['accent_blue'])} Refresh"
        )
        
        quick_ref_card = Panel(
            Group(
                Text.from_markup(f"[{COLORS['muted']}]QUICK REFERENCE[/]", justify="center"),
                "",
                quick_ref
            ),
            border_style=COLORS['accent_purple'],
            box=ROUNDED,
            padding=(0, 1),
        )
        
        # === LAYOUT ===
        left_column = Group(nav_card, jobs_card)
        top_row = make_two_column_layout(left_column, panels_card)

        # Build content without footer (footer is in tabbed overlay now)
        content = Group(
            top_row,
            quick_ref_card,
        )

        return content

    def render(self) -> Panel:
        """Returns complete Panel with footer (for backward compatibility)."""
        footer = Text.from_markup(
            f"[{COLORS['dim']}]Press [white on {COLORS['border']}] Esc [/] close • "
            f"[white on {COLORS['border']}] C [/] Prefs • "
            f"[white on {COLORS['border']}] E [/] Ref[/]",
            justify="center"
        )

        content_with_footer = Group(
            self._render_content(),
            "",
            footer
        )

        return Panel(
            content_with_footer,
            title="[bold white]⌨ KEYS[/]",
            subtitle=f"[{COLORS['dim']}][M] to toggle[/]",
            border_style=COLORS['accent_cyan'],
            box=ROUNDED,
            padding=(1, 2),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# TUI OVERLAY
# ═══════════════════════════════════════════════════════════════════════════════

class TuiOverlay:
    """Panel ustawien TUI - wyglad i zachowanie interfejsu."""

    def __init__(
        self,
        dim_level: str = "mid",
        sparkline_preset: Optional[str] = None,
        sparkline_palette: Optional[str] = None,
        sparkline_mode: str = "sparkline",
    ):
        self.dim_level = dim_level
        self.sparkline_preset = sparkline_preset
        self.sparkline_palette = sparkline_palette
        self.sparkline_mode = sparkline_mode

    def _render_dim_levels(self) -> str:
        levels = ["light", "mid", "dark"]
        badges = []
        for level in levels:
            style = (
                f"bold white on {COLORS['accent_green']}"
                if level == self.dim_level
                else f"white on {COLORS['border']}"
            )
            badges.append(f"[{style}] {level.upper()} [/]")
        return " ".join(badges)

    def _render_sparkline_presets(self) -> str:
        presets = list_gpu_sparkline_presets()
        if not presets:
            return f"[{COLORS['dim']}]—[/]"
        selected = self.sparkline_preset or DEFAULT_GPU_SPARKLINE_PRESET
        if selected not in presets:
            selected = presets[0]
        tokens = []
        for preset in presets:
            config = get_gpu_sparkline_config(preset)
            label = config.style.blocks or format_preset_label(preset, config)
            style = (
                f"bold white on {COLORS['accent_green']}"
                if self.sparkline_mode == "sparkline" and preset == selected
                else f"white on {COLORS['border']}"
            )
            tokens.append(f"[{style}] {label} [/]")
        return " ".join(tokens)

    def _render_sparkline_palettes(self) -> str:
        palettes = list_gpu_sparkline_palettes()
        if not palettes:
            return f"[{COLORS['dim']}]—[/]"
        selected = self.sparkline_palette or DEFAULT_GPU_SPARKLINE_PALETTE
        if selected not in palettes:
            selected = palettes[0]
        tokens = []
        for palette_name in palettes:
            palette = get_gpu_sparkline_palette(palette_name)
            swatches = build_palette_swatches(palette.colors, step=2)
            if self.sparkline_mode == "palette" and palette_name == selected:
                token = (
                    f"[{COLORS['accent_green']}]>[/]"
                    f"{swatches}"
                    f"[{COLORS['accent_green']}]<[/]"
                )
            else:
                token = swatches
            tokens.append(token)
        return " ".join(tokens)

    def _render_content(self) -> Group:
        """Returns content without outer Panel or footer (for tabbed overlay)."""
        options_table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        options_table.add_column(style=COLORS['muted'], width=18)
        options_table.add_column(ratio=1)
        options_table.add_row(
            "Overlay dim",
            f"{self._render_dim_levels()}  [{COLORS['dim']}][I] cycle[/]",
        )

        options_card = make_card(
            "APPEARANCE",
            options_table,
            icon=ICONS['tui'],
            title_color=COLORS['accent_blue'],
        )

        sparkline_table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        sparkline_table.add_column(style=COLORS['muted'], width=18)
        sparkline_table.add_column(ratio=1)
        sparkline_table.add_row(
            "Sparkline",
            f"{self._render_sparkline_presets()}  [{COLORS['dim']}][W] cycle[/]",
        )
        sparkline_table.add_row(
            "Palette",
            f"{self._render_sparkline_palettes()}  [{COLORS['dim']}][P] cycle[/]",
        )

        sparkline_card = make_card(
            "SPARKLINE",
            sparkline_table,
            icon=ICONS['gpu'],
            title_color=COLORS['accent_cyan'],
        )

        hint = Text.from_markup(
            f"[{COLORS['dim']}]Changes apply immediately[/]",
            justify="center",
        )

        return Group(options_card, sparkline_card, hint)

    def render(self) -> Panel:
        """Returns complete Panel with footer (for backward compatibility)."""
        footer = Text.from_markup(
            f"[{COLORS['dim']}]Press [white on {COLORS['border']}] Esc [/] close • "
            f"[white on {COLORS['border']}] I [/] Dim level • "
            f"[white on {COLORS['border']}] W [/] Sparkline • "
            f"[white on {COLORS['border']}] P [/] Palette[/]",
            justify="center",
        )

        content_with_footer = Group(
            self._render_content(),
            "",
            footer,
        )

        return Panel(
            content_with_footer,
            title=f"[bold white]{ICONS['tui']} TUI[/]",
            subtitle=f"[{COLORS['dim']}][T] to toggle[/]",
            border_style=COLORS['accent_purple'],
            box=ROUNDED,
            padding=(1, 2),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# DIRS OVERLAY
# ═══════════════════════════════════════════════════════════════════════════════

class DirsOverlay:
    """Interactive directory manager panel (Dirs [D] tab).

    Displays all input directories (active + disabled + pending changes) with
    cursor navigation and status indicators. Output/Errors dir suffixes shown
    read-only at the bottom.

    Entry status labels:
      [ON]  green  — currently active directory
      [OFF] dim    — disabled (in disabled_input_dirs)
      [ON*] yellow — pending add or pending enable
      [DEL] red    — pending deletion
      [~]   yellow — pending toggle (active→disabled or disabled→active)
    """

    MAX_DIRS = 10  # Maximum total directories (active + disabled)

    def __init__(
        self,
        entries: List[Tuple[str, str, Optional[int], Optional[int]]],
        cursor: int,
        input_mode: bool,
        input_buffer: str,
        suffix_output_dirs: Optional[str],
        suffix_errors_dirs: Optional[str],
        output_dir_lines: List[str],
        errors_dir_lines: List[str],
        error_msg: str = "",
    ):
        self.entries = entries  # (path, status, file_count, size_bytes)
        self.cursor = max(0, min(cursor, max(0, len(entries) - 1)))
        self.input_mode = input_mode
        self.input_buffer = input_buffer
        self.suffix_output_dirs = suffix_output_dirs
        self.suffix_errors_dirs = suffix_errors_dirs
        self.output_dir_lines = output_dir_lines
        self.errors_dir_lines = errors_dir_lines
        self.error_msg = error_msg

    def _status_badge(self, status: str, fs_status: Optional[str] = None) -> str:
        """Return a Rich markup badge for the given status.

        Active dirs show filesystem status inside brackets:
          [✓] green  = active, accessible
          [✗] red    = active, path missing
          [⚡] yellow = active, no read access
        Disabled/pending use standard badges.
        """
        if status == "active":
            if fs_status == "missing":
                return f"[bold {COLORS['error_red']}][✗][/]"
            if fs_status == "no_access":
                return f"[bold {COLORS['warning_yellow']}][!][/]"
            return f"[bold {COLORS['accent_green']}][✓][/]"
        if status == "disabled":
            if fs_status == "missing":
                return f"[{COLORS['error_red']}][✗][/]"
            if fs_status == "no_access":
                return f"[{COLORS['warning_yellow']}][!][/]"
            return f"[{COLORS['dim']}][ ][/]"
        if status in ("pending_add", "pending_toggle_on"):
            return f"[bold {COLORS['warning_yellow']}][✓][/]"
        if status == "pending_toggle_off":
            if fs_status == "missing":
                return f"[bold {COLORS['error_red']}][✗][/]"
            if fs_status == "no_access":
                return f"[bold {COLORS['warning_yellow']}][!][/]"
            return f"[bold {COLORS['warning_yellow']}][ ][/]"
        if status == "pending_remove":
            return f"[bold {COLORS['error_red']}][✗][/]"
        return f"[{COLORS['dim']}][ ][/]"

    def _status_note(self, status: str) -> str:
        return ""

    def _render_input_dirs_table(self) -> Table:
        table = Table(show_header=False, box=None, padding=(0, 0), expand=True)
        table.add_column(width=6, no_wrap=True)   # cursor + badge together
        table.add_column(ratio=1)                  # path + note
        table.add_column(justify="right", width=7, no_wrap=True)   # files
        table.add_column(justify="right", width=8, no_wrap=True)   # size

        # Header row
        table.add_row(
            "",
            "",
            f"[{COLORS['dim']}]Files[/]",
            f"[{COLORS['dim']}]Size[/]",
        )

        if not self.entries:
            table.add_row("", f"[{COLORS['dim']}]No directories configured[/]", "", "")
            return table

        for idx, entry_tuple in enumerate(self.entries):
            path, status, file_count, size_bytes = entry_tuple[:4]
            fs_status = entry_tuple[4] if len(entry_tuple) > 4 else None
            is_cursor = idx == self.cursor
            arrow = f"[bold {COLORS['accent_green']}]►[/]" if is_cursor else " "
            badge = self._status_badge(status, fs_status)
            cursor_badge = f"{arrow}{badge}"
            note = self._status_note(status)

            display_path = path

            # Row styling
            if is_cursor:
                path_markup = f"[bold white]{display_path}[/]{note}"
            elif status == "disabled":
                path_markup = f"[{COLORS['dim']}]{display_path}[/]{note}"
            elif status == "pending_remove":
                path_markup = f"[{COLORS['error_red']}]{display_path}[/]{note}"
            elif status in ("pending_add", "pending_toggle_off", "pending_toggle_on"):
                path_markup = f"[{COLORS['warning_yellow']}]{display_path}[/]{note}"
            elif fs_status in ("missing", "no_access"):
                path_markup = f"[{COLORS['dim']}]{display_path}[/]{note}"
            else:
                path_markup = f"[white]{display_path}[/]{note}"

            count_str = "—" if file_count is None else str(file_count)
            size_str = format_size(size_bytes) if size_bytes is not None else "—"

            if status in ("disabled", "pending_remove") or fs_status in ("missing", "no_access"):
                count_str = f"[{COLORS['dim']}]{count_str}[/]"
                size_str = f"[{COLORS['dim']}]{size_str}[/]"

            table.add_row(cursor_badge, path_markup, count_str, size_str)

        return table

    def _render_content(self) -> Group:
        """Returns content without outer Panel or footer (for tabbed overlay)."""
        # === INPUT DIRS TABLE ===
        input_card = make_card(
            "INPUT DIRS",
            self._render_input_dirs_table(),
            icon=ICONS['io'],
            title_color=COLORS['accent_blue'],
        )

        # === OUTPUT + ERRORS summary (read-only) ===
        def _suffix_line(label: str, suffix: Optional[str], lines: List[str]) -> str:
            if suffix:
                return f"[{COLORS['muted']}]{label}:[/] [white]Suffix: {suffix}[/]"
            if lines:
                return f"[{COLORS['muted']}]{label}:[/] [white]{lines[0]}[/]"
            return f"[{COLORS['muted']}]{label}:[/] [{COLORS['dim']}]—[/]"

        out_line = _suffix_line("Output", self.suffix_output_dirs, self.output_dir_lines)
        err_line = _suffix_line("Errors", self.suffix_errors_dirs, self.errors_dir_lines)

        out_err_table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
        out_err_table.add_column(ratio=1)
        out_err_table.add_column(ratio=1)
        out_err_table.add_row(out_line, err_line)

        out_err_card = make_card(
            "OUTPUT / ERRORS DIRS",
            out_err_table,
            icon=ICONS['io'],
            title_color=COLORS['muted'],
        )

        # === ADD PATH INPUT (visible only in add mode) ===
        content_items: List[RenderableType] = [input_card, out_err_card]

        if self.input_mode:
            cursor_block = f"[bold {COLORS['accent_green']}]█[/]"
            buffer_display = self.input_buffer + cursor_block
            input_line = Text.from_markup(
                f"  [{COLORS['muted']}]Add path:[/] [white]{buffer_display}[/]"
            )
            content_items.append(input_line)

        # === FOOTER HINT ===
        if self.input_mode:
            footer_hint = Text.from_markup(
                f"[{COLORS['dim']}]"
                f"[white on {COLORS['border']}] Enter [/] confirm  "
                f"[white on {COLORS['border']}] Esc [/] cancel"
                f"[/]",
                justify="center",
            )
        else:
            footer_hint = Text.from_markup(
                f"[{COLORS['dim']}]"
                f"[white on {COLORS['border']}] ↑↓ [/] navigate  "
                f"[white on {COLORS['border']}] Space [/] toggle  "
                f"[white on {COLORS['border']}] A [/] add  "
                f"[white on {COLORS['border']}] Del [/] remove  "
                f"[white on {COLORS['border']}] S [/] apply"
                f"[/]",
                justify="center",
            )

        content_items.append(footer_hint)
        return Group(*content_items)

    def render(self) -> Panel:
        """Returns complete Panel with footer (for backward compatibility)."""
        return Panel(
            self._render_content(),
            title="[bold white]📁 DIRS[/]",
            subtitle=f"[{COLORS['dim']}][D] to toggle[/]",
            border_style=COLORS['accent_green'],
            box=ROUNDED,
            padding=(1, 2),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION - metody do podmiany w klasie Dashboard
# ═══════════════════════════════════════════════════════════════════════════════

def generate_settings_overlay(
    config_lines: List[str],
    spinner_frame: int = 0,
    log_path: Optional[str] = None,
    debug_enabled: bool = False,
) -> Panel:
    """Generuje overlay Settings (dawniej Config) dla Dashboard."""
    return SettingsOverlay(config_lines, spinner_frame, log_path, debug_enabled).render()


def generate_io_overlay(
    config_lines: List[str],
    input_dir_stats: List[Tuple[str, str, Optional[int], Optional[int]]],
    output_dir_lines: List[str],
    errors_dir_lines: List[str],
    suffix_output_dirs: Optional[str],
    suffix_errors_dirs: Optional[str],
    queue_sort: str,
    queue_seed: Optional[int],
) -> Panel:
    """Generuje overlay I/O dla Dashboard."""
    return IoOverlay(
        config_lines,
        input_dir_stats,
        output_dir_lines,
        errors_dir_lines,
        suffix_output_dirs,
        suffix_errors_dirs,
        queue_sort,
        queue_seed,
    ).render()


def generate_reference_overlay(
    spinner_frame: int = 0,
    sparkline_preset: Optional[str] = None,
    sparkline_palette: Optional[str] = None,
    sparkline_mode: str = "sparkline",
) -> Panel:
    """Generuje overlay Reference (dawniej Legend) dla Dashboard."""
    return ReferenceOverlay(
        spinner_frame,
        sparkline_preset,
        sparkline_palette,
        sparkline_mode,
    ).render()


def generate_shortcuts_overlay() -> Panel:
    """Generuje overlay Shortcuts (dawniej Menu) dla Dashboard."""
    return ShortcutsOverlay().render()


def generate_tui_overlay(
    dim_level: str = "mid",
    sparkline_preset: Optional[str] = None,
    sparkline_palette: Optional[str] = None,
    sparkline_mode: str = "sparkline",
) -> Panel:
    """Generuje overlay TUI dla Dashboard."""
    return TuiOverlay(dim_level, sparkline_preset, sparkline_palette, sparkline_mode).render()


# ═══════════════════════════════════════════════════════════════════════════════
# CONTENT RENDERING FUNCTIONS (for tabbed overlay)
# ═══════════════════════════════════════════════════════════════════════════════

def render_settings_content(
    config_lines: List[str],
    spinner_frame: int = 0,
    log_path: Optional[str] = None,
    debug_enabled: bool = False,
) -> RenderableType:
    """Render Settings tab content (without outer Panel or footer)."""
    return SettingsOverlay(config_lines, spinner_frame, log_path, debug_enabled)._render_content()


def render_reference_content(
    spinner_frame: int = 0,
    sparkline_preset: Optional[str] = None,
    sparkline_palette: Optional[str] = None,
    sparkline_mode: str = "sparkline",
) -> RenderableType:
    """Render Reference tab content (without outer Panel or footer)."""
    return ReferenceOverlay(
        spinner_frame,
        sparkline_preset,
        sparkline_palette,
        sparkline_mode,
    )._render_content()


def render_shortcuts_content() -> RenderableType:
    """Render Shortcuts tab content (without outer Panel or footer)."""
    return ShortcutsOverlay()._render_content()

def render_io_content(
    config_lines: List[str],
    input_dir_stats: List[Tuple[str, str, Optional[int], Optional[int]]],
    output_dir_lines: List[str],
    errors_dir_lines: List[str],
    suffix_output_dirs: Optional[str],
    suffix_errors_dirs: Optional[str],
    queue_sort: str,
    queue_seed: Optional[int],
) -> RenderableType:
    """Render I/O tab content (without outer Panel or footer)."""
    return IoOverlay(
        config_lines,
        input_dir_stats,
        output_dir_lines,
        errors_dir_lines,
        suffix_output_dirs,
        suffix_errors_dirs,
        queue_sort,
        queue_seed,
    )._render_content()


def render_tui_content(
    dim_level: str = "mid",
    sparkline_preset: Optional[str] = None,
    sparkline_palette: Optional[str] = None,
    sparkline_mode: str = "sparkline",
) -> RenderableType:
    """Render TUI tab content (without outer Panel or footer)."""
    return TuiOverlay(dim_level, sparkline_preset, sparkline_palette, sparkline_mode)._render_content()


def render_dirs_content(
    entries: List[Tuple[str, str, Optional[int], Optional[int]]],
    cursor: int,
    input_mode: bool,
    input_buffer: str,
    suffix_output_dirs: Optional[str],
    suffix_errors_dirs: Optional[str],
    output_dir_lines: List[str],
    errors_dir_lines: List[str],
    error_msg: str = "",
) -> RenderableType:
    """Render Dirs tab content (without outer Panel or footer).

    Args:
        entries: List of (path, status, file_count, size_bytes) from UIState.dirs_get_all_entries().
        cursor: Current cursor position (0-based).
        input_mode: True when add-path input mode is active.
        input_buffer: Current add-path input text.
        suffix_output_dirs: Output directory suffix (e.g. "_out").
        suffix_errors_dirs: Errors directory suffix (e.g. "_err").
        output_dir_lines: Explicit output directory paths (for non-suffix mode).
        errors_dir_lines: Explicit errors directory paths (for non-suffix mode).
    """
    return DirsOverlay(
        entries,
        cursor,
        input_mode,
        input_buffer,
        suffix_output_dirs,
        suffix_errors_dirs,
        output_dir_lines,
        errors_dir_lines,
        error_msg=error_msg,
    )._render_content()


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD INTEGRATION SNIPPET (UPDATED FOR TABBED OVERLAY)
# ═══════════════════════════════════════════════════════════════════════════════
"""
Tabbed overlay integration is complete. The dashboard now uses:

- render_settings_content() for Settings tab content
- render_io_content() for I/O tab content
- render_reference_content() for Reference tab content
- render_shortcuts_content() for Shortcuts tab content
- render_tui_content() for TUI tab content

Old standalone overlay functions (for backward compatibility):
- generate_settings_overlay() - returns complete Panel
- generate_io_overlay() - returns complete Panel
- generate_reference_overlay() - returns complete Panel
- generate_shortcuts_overlay() - returns complete Panel
- generate_tui_overlay() - returns complete Panel
"""


# ═══════════════════════════════════════════════════════════════════════════════
# DEMO / TEST
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from rich.console import Console
    
    console = Console()
    
    # Example config lines (z main.py)
    config_lines = [
        "Video Batch Compression - NVENC AV1 (GPU)",
        "Start: 2025-12-31 19:57:56",
        "Input folders: 1",
        "  ✓ 1. /run/media/xai/26685cd8-5a05-46bb-b70e-2bc86d5d5c43/tt",
        "Threads: 1 (Prefetch: 1x)",
        "Encoder: NVENC AV1 (GPU) | Preset: p7 (Slow/HQ)",
        "Audio: Auto (lossless->AAC 256k, AAC/MP3 copy, other->AAC 192k)",
        "Quality: CQ44 (Global Default)",
        "Dynamic Quality: DJI OsmoPocket3:40, DC-GH7:35, ILCE-7RM5:35",
        "Camera Filter: None",
        "Metadata: Deep (ExifTool + XMP) (Analysis: True)",
        "Autorotate: 1 rules loaded",
        "Manual Rotation: None",
        "Extensions: .mp4, .flv, .webm, .mov, .mkv → .mp4",
        "Queue sort: rand (seed 42)",
        "CPU fallback: True | CPU threads per worker: 4",
        "Min size: 1.0MB | Skip AV1: False",
        "Clean errors: False | Strip Unicode: True",
        "Debug logging: True",
    ]
    
    console.print("\n[bold cyan]═══ SETTINGS OVERLAY ═══[/]\n")
    console.print(generate_settings_overlay(config_lines, spinner_frame=0))
    
    console.print("\n[bold cyan]═══ REFERENCE OVERLAY ═══[/]\n")
    console.print(generate_reference_overlay(spinner_frame=0))
    
    console.print("\n[bold cyan]═══ SHORTCUTS OVERLAY ═══[/]\n")
    console.print(generate_shortcuts_overlay())
