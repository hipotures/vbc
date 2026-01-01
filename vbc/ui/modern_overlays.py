"""
VBC Modernized Overlays
=======================
Nowoczesny, estetyczny design dla paneli CONFIG, REFERENCE (dawniej LEGEND) i SHORTCUTS (dawniej MENU).
Używa Rich library z kartami, tabelami i hierarchiczną strukturą.

Koncepcja:
- Settings (C) - konfiguracja sesji w kartach tematycznych
- Reference (L) - legenda statusów i symboli  
- Shortcuts (M) - skróty klawiszowe z podziałem funkcjonalnym

Wszystkie trzy panele zachowują 100% obecnej funkcjonalności, ale prezentują
ją w bardziej przejrzysty i nowoczesny sposób.
"""

import re
from typing import List, Optional
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich.rule import Rule
from rich.box import ROUNDED, SIMPLE, MINIMAL, HEAVY_HEAD


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
    'status': '◆',
    'spinners': '◈',
    'gpu': '◈',
    'nav': '▸',
    'panels': '▸',
    'jobs': '▸',
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


# ═══════════════════════════════════════════════════════════════════════════════
# SETTINGS OVERLAY (dawniej CONFIG)
# ═══════════════════════════════════════════════════════════════════════════════

class SettingsOverlay:
    """
    Panel ustawień sesji - wyświetla konfigurację w kartach tematycznych.
    
    Karty:
    - ENCODING: encoder, preset, quality, audio, fallback
    - PROCESSING: threads, prefetch, queue sort, cpu threads
    - INPUT/OUTPUT: folders, extensions, output format, min size
    - QUALITY & FILTERS: dynamic CQ, camera filter, skip AV1, rotation
    - METADATA & DEBUG: exiftool, analysis, autorotate, debug flags
    """
    
    def __init__(self, config_lines: List[str], spinner_frame: int = 0):
        self.config_lines = config_lines
        self.spinner_frame = spinner_frame
        self._parsed = self._parse_config(config_lines)
    
    def _parse_config(self, lines: List[str]) -> dict:
        """Parsuje config_lines do słownika."""
        result = {}
        for line in lines:
            if ": " in line:
                # Handle complex patterns like "Dynamic CQ: DJI:40, DC-GH7:35"
                parts = line.split(": ", 1)
                key = parts[0].strip()
                value = parts[1].strip() if len(parts) > 1 else ""
                result[key.lower().replace(" ", "_")] = value
        return result
    
    def _get(self, key: str, default: str = "—") -> str:
        """Pobiera wartość z parsowanej konfiguracji."""
        return self._parsed.get(key, default)
    
    def render(self) -> Panel:
        """Renderuje panel Settings."""
        
        # === HEADER ===
        title_line = self.config_lines[0] if self.config_lines else "Video Batch Compression"
        start_time = self._get("start", "")
        
        header = Table(show_header=False, box=None, expand=True, padding=0)
        header.add_column(ratio=3)
        header.add_column(justify="right", ratio=1)
        header.add_row(
            f"[bold white]{title_line}[/]",
            f"[{COLORS['accent_green']}]● Active[/]"
        )
        if start_time:
            header.add_row(
                f"[{COLORS['muted']}]Started {start_time}[/]",
                ""
            )
        
        header_panel = Panel(
            header,
            border_style=COLORS['accent_green'],
            box=ROUNDED,
            padding=(0, 1),
        )
        
        # === ENCODING CARD ===
        encoding_data = [
            ("Encoder", self._get("encoder", "").split(" | ")[0] if "encoder" in self._parsed else "—"),
            ("Preset", self._get("encoder", "").split("Preset: ")[-1] if "Preset:" in self._get("encoder", "") else "—"),
            ("Quality", f"CQ{self._get('quality', '').replace('CQ', '').split()[0]}" if "quality" in self._parsed else "—"),
            ("Audio", self._get("audio", "Copy")),
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
        
        # === QUALITY & FILTERS CARD ===
        dynamic_cq = self._get("dynamic_cq", "None")
        camera_filter = self._get("camera_filter", "None")
        skip_av1 = self._get("min_size", "").split("Skip AV1: ")[-1] if "Skip AV1:" in self._get("min_size", "") else "False"
        manual_rotation = self._get("manual_rotation", "None")
        
        quality_data = [
            ("Dynamic CQ", dynamic_cq if dynamic_cq else "None"),
            ("Camera Filter", camera_filter),
            ("Skip AV1", skip_av1),
            ("Rotation", manual_rotation),
        ]
        quality_card = make_card(
            "QUALITY & FILTERS",
            make_kv_table(quality_data, {"Dynamic CQ"}),
            icon=ICONS['quality'],
            title_color=COLORS['accent_blue']
        )
        
        # === METADATA & DEBUG CARD (full width) ===
        metadata = self._get("metadata", "")
        analysis = "True" if "(Analysis: True)" in metadata else "False"
        metadata_method = metadata.split(" (")[0] if " (" in metadata else metadata
        autorotate = self._get("autorotate", "0 rules")
        clean_errors = self._get("clean_errors", "").split(" | ")[0] if "clean_errors" in self._parsed else "False"
        strip_unicode = self._get("clean_errors", "").split("Strip Unicode: ")[-1] if "Strip Unicode:" in self._get("clean_errors", "") else "True"
        debug = self._get("debug_logging", "False")
        
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
            "Debug", f"[white]{debug}[/]"
        )
        
        metadata_card = make_card(
            "METADATA & DEBUG",
            meta_table,
            icon=ICONS['metadata'],
            title_color=COLORS['accent_blue']
        )
        
        # === LAYOUT ===
        # Row 1: Encoding + Processing (side by side)
        row1 = make_two_column_layout(encoding_card, processing_card)
        # Row 2: I/O + Quality (side by side)
        row2 = make_two_column_layout(io_card, quality_card)
        # Row 3: Metadata (full width)
        
        # Footer hint
        footer = Text.from_markup(
            f"[{COLORS['dim']}]Press [white on {COLORS['border']}] Esc [/] close • "
            f"[white on {COLORS['border']}] L [/] Reference • "
            f"[white on {COLORS['border']}] M [/] Shortcuts[/]",
            justify="center"
        )
        
        content = Group(
            header_panel,
            "",
            row1,
            "",
            row2,
            "",
            metadata_card,
            "",
            footer
        )
        
        return Panel(
            content,
            title="[bold white]⚙ SETTINGS[/]",
            subtitle=f"[{COLORS['dim']}][C] to toggle[/]",
            border_style=COLORS['accent_green'],
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
    
    def __init__(self, spinner_frame: int = 0):
        self.spinner_frame = spinner_frame
    
    def render(self) -> Panel:
        """Renderuje panel Reference."""
        
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
            "",
            Rule(style=COLORS['border']),
            "",
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
        normal_spinner = spinner_frames[self.spinner_frame % len(spinner_frames)]
        rotating_spinner = spinner_rotating[self.spinner_frame % len(spinner_rotating)]
        
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
        
        spinners_card = Panel(
            spinners_table,
            title=f"[bold {COLORS['accent_purple']}]{ICONS['spinners']} ACTIVE JOB INDICATORS[/]",
            title_align="left",
            border_style=COLORS['border'],
            box=ROUNDED,
            padding=(0, 1),
        )
        
        # === GPU GRAPH ===
        gpu_content = []
        gpu_content.append(f"[{COLORS['muted']}]Cycle: [white]temp → fan → pwr → gpu → mem[/][/]")
        gpu_content.append("")
        gpu_content.append(f"[{COLORS['dim']}]Scales:[/]")
        gpu_content.append(f"  [{COLORS['muted']}]temp: 35°C..70°C • pwr: 100W..400W • %: 0..100%[/]")
        gpu_content.append("")
        gpu_content.append(
            f"[{COLORS['dim']}]Symbols:[/] [{COLORS['accent_blue']}]▁▂▃▄▅▆▇█[/] "
            f"[{COLORS['muted']}]low→high[/]   "
            f"[{COLORS['dim']}]·[/] [{COLORS['muted']}]missing[/]"
        )
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
        
        # Footer hint
        footer = Text.from_markup(
            f"[{COLORS['dim']}]Press [white on {COLORS['border']}] Esc [/] close • "
            f"[white on {COLORS['border']}] C [/] Settings • "
            f"[white on {COLORS['border']}] M [/] Shortcuts[/]",
            justify="center"
        )
        
        content = Group(
            status_card,
            "",
            bottom_row,
            "",
            footer
        )
        
        return Panel(
            content,
            title="[bold white]📖 REFERENCE[/]",
            subtitle=f"[{COLORS['dim']}][L] to toggle[/]",
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
    - PANELS: C, L, G
    - JOB CONTROL: S, R, </>, </>
    + Quick Reference z kolorowymi badge'ami
    """
    
    def render(self) -> Panel:
        """Renderuje panel Shortcuts."""
        
        # === NAVIGATION ===
        nav_table = Table(show_header=False, box=None, padding=(0, 0))
        nav_table.add_column(width=14)
        nav_table.add_column()
        
        nav_table.add_row(
            f"[bold white on {COLORS['border']}]   M   [/]",
            "Toggle this menu"
        )
        nav_table.add_row(
            f"[bold white on {COLORS['border']}]  Esc  [/]",
            "Close any overlay"
        )
        nav_table.add_row(
            f"[bold white on {COLORS['border']}] Ctrl+C [/]",
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
            f"[bold white on {COLORS['border']}]   C   [/]",
            "Configuration details"
        )
        panels_table.add_row(
            f"[bold white on {COLORS['border']}]   L   [/]",
            "Legend & reference"
        )
        panels_table.add_row(
            f"[bold white on {COLORS['border']}]   G   [/]",
            "Rotate GPU metric graph"
        )
        
        panels_card = Panel(
            panels_table,
            title=f"[bold {COLORS['accent_cyan']}]{ICONS['panels']} PANELS[/]",
            title_align="left",
            border_style=COLORS['border'],
            box=ROUNDED,
            padding=(0, 1),
        )
        
        # === JOB CONTROL (full width, 2 columns) ===
        jobs_table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        jobs_table.add_column(width=14)
        jobs_table.add_column(ratio=1)
        jobs_table.add_column(width=14)
        jobs_table.add_column(ratio=1)
        
        jobs_table.add_row(
            f"[bold white on {COLORS['border']}]   S   [/]",
            "Shutdown toggle (graceful)",
            f"[bold white on {COLORS['border']}]   R   [/]",
            "Refresh queue (re-scan)"
        )
        jobs_table.add_row(
            f"[bold white on {COLORS['border']}]  < ,  [/]",
            "Decrease thread count",
            f"[bold white on {COLORS['border']}]  > .  [/]",
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
            f"[bold white on {COLORS['accent_green']}] < > [/] Threads",
            f"[bold white on {COLORS['warning_yellow']}]  S  [/] Shutdown",
            f"[bold white on {COLORS['accent_blue']}]  R  [/] Refresh"
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
        top_row = make_two_column_layout(nav_card, panels_card)
        
        # Footer hint
        footer = Text.from_markup(
            f"[{COLORS['dim']}]Press [white on {COLORS['border']}] Esc [/] close • "
            f"[white on {COLORS['border']}] C [/] Settings • "
            f"[white on {COLORS['border']}] L [/] Reference[/]",
            justify="center"
        )
        
        content = Group(
            top_row,
            "",
            jobs_card,
            "",
            quick_ref_card,
            "",
            footer
        )
        
        return Panel(
            content,
            title="[bold white]⌨ SHORTCUTS[/]",
            subtitle=f"[{COLORS['dim']}][M] to toggle[/]",
            border_style=COLORS['accent_cyan'],
            box=ROUNDED,
            padding=(1, 2),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION - metody do podmiany w klasie Dashboard
# ═══════════════════════════════════════════════════════════════════════════════

def generate_settings_overlay(config_lines: List[str], spinner_frame: int = 0) -> Panel:
    """Generuje overlay Settings (dawniej Config) dla Dashboard."""
    return SettingsOverlay(config_lines, spinner_frame).render()


def generate_reference_overlay(spinner_frame: int = 0) -> Panel:
    """Generuje overlay Reference (dawniej Legend) dla Dashboard."""
    return ReferenceOverlay(spinner_frame).render()


def generate_shortcuts_overlay() -> Panel:
    """Generuje overlay Shortcuts (dawniej Menu) dla Dashboard."""
    return ShortcutsOverlay().render()


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD INTEGRATION SNIPPET
# ═══════════════════════════════════════════════════════════════════════════════
"""
Aby zintegrować z istniejącym dashboard.py, zamień metody:

1. W klasie Dashboard, zamień metodę _generate_config_overlay na:

    def _generate_config_overlay(self) -> Panel:
        from vbc.ui.modern_overlays import generate_settings_overlay
        with self.state._lock:
            lines = self.state.config_lines[:]
        return generate_settings_overlay(lines, self._spinner_frame)

2. Zamień metodę _generate_legend_overlay na:

    def _generate_legend_overlay(self) -> Panel:
        from vbc.ui.modern_overlays import generate_reference_overlay
        return generate_reference_overlay(self._spinner_frame)

3. Zamień metodę _generate_menu_overlay na:

    def _generate_menu_overlay(self) -> Panel:
        from vbc.ui.modern_overlays import generate_shortcuts_overlay
        return generate_shortcuts_overlay()

4. W metodzie create_display(), zmień overlay_width z 80 na 85:

        if self.state.show_config:
            return _Overlay(layout, self._generate_config_overlay(), overlay_width=85)
        elif self.state.show_legend:
            return _Overlay(layout, self._generate_legend_overlay(), overlay_width=85)
        elif self.state.show_menu:
            return _Overlay(layout, self._generate_menu_overlay(), overlay_width=85)

Alternatywnie, zapisz ten plik jako vbc/ui/modern_overlays.py i zaimportuj funkcje.
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
        "Audio: Copy (stream copy)",
        "Quality: CQ44 (Global Default)",
        "Dynamic CQ: DJI OsmoPocket3:40, DC-GH7:35, ILCE-7RM5:35",
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
