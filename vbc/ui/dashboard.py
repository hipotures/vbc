import re
import threading
import time
import unicodedata
from datetime import datetime
from typing import Optional, List, Tuple, Any
from rich.live import Live
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.layout import Layout
from rich.progress_bar import ProgressBar
from rich.align import Align
from rich.segment import Segment
from rich._loop import loop_last
from rich.text import Text
from rich.rule import Rule
from rich.box import ROUNDED, SIMPLE
from rich.style import Style
from vbc.ui.state import UIState
from vbc.domain.models import JobStatus
from vbc.ui.gpu_sparkline import (
    PALETTE_GLYPH,
    get_gpu_sparkline_config,
    get_gpu_sparkline_palette,
    render_sparkline,
)
from vbc.ui.modern_overlays import (
    render_settings_content,
    render_reference_content,
    render_shortcuts_content,
    render_io_content,
    render_tui_content,
)

# Layout Constants
TOP_BAR_LINES = 3  # Status, Gap, KPI
FOOTER_LINES = 1   # Health counters
MIN_2COL_W = 110   # Breakpoint for 2-column layout

# Panel content min/max heights (lines within frame)
PROGRESS_MIN = 2   # Done/Total + bar
PROGRESS_MAX = 3   # 3 lines total: Header, Bar, Gap/Action
ACTIVE_MIN = 1
ACTIVITY_MIN = 1
QUEUE_MIN = 1

def is_wide_char(char: str) -> bool:
    """Check if Unicode character is wide (takes 2 terminal columns)."""
    if not char:
        return False
    # East Asian Width categories: F(ull), W(ide) = 2 cols, others = 1 col
    width = unicodedata.east_asian_width(char[0])
    return width in ('F', 'W')

def format_icon(icon: str) -> str:
    """Format icon with appropriate spacing (wide chars don't need trailing space)."""
    if is_wide_char(icon):
        return icon  # No space needed (e.g., ⚡)
    else:
        return f"{icon} "  # Add space (e.g., ✓ )

class _Overlay:
    """Render overlay panel centered over a background renderable."""
    def __init__(self, background, overlay, overlay_width: int, dim_level: Optional[str] = None):
        self.background = background
        self.overlay = overlay
        self.overlay_width = overlay_width
        self.dim_level = dim_level

    def _slice_line(self, line, start: int, end: int):
        if start >= end:
            return []
        result = []
        pos = 0
        for segment in line:
            seg_len = segment.cell_length
            if seg_len == 0:
                if result:
                    result.append(segment)
                continue
            seg_end = pos + seg_len
            if seg_end <= start:
                pos = seg_end
                continue
            if pos >= end:
                break
            cut_start = max(start - pos, 0)
            cut_end = min(end - pos, seg_len)
            if cut_start == 0 and cut_end == seg_len:
                result.append(segment)
            else:
                _, right = segment.split_cells(cut_start)
                mid_len = cut_end - cut_start
                mid, _ = right.split_cells(mid_len)
                result.append(mid)
            pos = seg_end
        return result

    def __rich_console__(self, console, options):
        width, height = options.size
        bg_lines = console.render_lines(self.background, options, pad=True)
        bg_lines = Segment.set_shape(bg_lines, width, height)
        if self.dim_level:
            dim_style = Style(dim=True)
            wash_color = {
                "light": "#6a6a6a",
                "mid": "#5a5a5a",
                "dark": "#2f2f2f",
            }.get(self.dim_level, "#5a5a5a")
            wash_style = Style(color=wash_color)
            bg_lines = [
                list(Segment.apply_style(line, dim_style, post_style=wash_style))
                for line in bg_lines
            ]
        
        overlay_lines = console.render_lines(
            self.overlay,
            options.update(width=self.overlay_width),
            pad=True
        )
        overlay_lines = [
            Segment.adjust_line_length(line, self.overlay_width) for line in overlay_lines
        ]

        overlay_height = len(overlay_lines)
        left = max((width - self.overlay_width) // 2, 0)
        # Align to top instead of center to prevent overflow on small terminals
        top = 1  # Small margin from top

        for idx, overlay_line in enumerate(overlay_lines):
            target_row = top + idx
            if target_row < 0 or target_row >= height:
                continue
            bg_line = bg_lines[target_row]
            left_seg = self._slice_line(bg_line, 0, left)
            right_seg = self._slice_line(bg_line, left + self.overlay_width, width)
            bg_lines[target_row] = left_seg + overlay_line + right_seg

        for last, line in loop_last(bg_lines):
            yield from line
            if not last:
                yield Segment.line()


class Dashboard:
    """Adaptive UI implementation with dynamic density control."""

    def __init__(self, state: UIState, panel_height_scale: float = 0.7, max_active_jobs: int = 8):
        self.state = state
        self.panel_height_scale = panel_height_scale  # UI scale factor
        self.max_active_jobs = max_active_jobs  # Max jobs to reserve space for
        self.console = Console()
        self._live: Optional[Live] = None
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop_refresh = threading.Event()
        self._ui_lock = threading.Lock()
        self._spinner_frame = 0

    # --- Formatters ---

    def format_size(self, size: int) -> str:
        """Format size: 123B, 1.2KB, 45.1MB, 3.2GB."""
        if size == 0:
            return "0B"
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        idx = 0
        val = float(size)
        while val >= 1024.0 and idx < len(units) - 1:
            val /= 1024.0
            idx += 1
        
        if idx < 2: # B, KB -> no decimal usually, but let's stick to spec
            if idx == 0: return f"{int(val)}B"
            return f"{val:.1f}KB"
        return f"{val:.1f}{units[idx]}"

    def format_time(self, seconds: float) -> str:
        """Format time: 59s, 01m 01s, 1h 01m."""
        if seconds is None:
            return "--:--"
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds // 60):02d}m {int(seconds % 60):02d}s"
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60):02d}m"
            
    def format_global_eta(self, seconds: float) -> str:
        """Format global ETA: hh:mm or mm:ss."""
        if seconds is None:
            return "--:--"
        if seconds < 60:
            return f"{int(seconds):02d}s"
        elif seconds < 3600:
            return f"{int(seconds // 60):02d}m {int(seconds % 60):02d}s"
        else:
            return f"{int(seconds // 3600):02d}h {int((seconds % 3600) // 60):02d}m"

    def format_resolution(self, metadata) -> str:
        if metadata and metadata.width and metadata.height:
            megapixels = round((metadata.width * metadata.height) / 1_000_000)
            return f"{megapixels}M"
        return ""

    def format_fps(self, metadata) -> str:
        if metadata and metadata.fps:
            return f"{int(metadata.fps)}fps"
        return ""
        
    def _sanitize_filename(self, filename: str, max_len: int = 30) -> str:
        """Sanitize and truncate filename: prefix...suffix."""
        if self.state.strip_unicode_display:
            filename = "".join(c for c in filename if ord(c) < 128)
        filename = filename.lstrip()
        
        if len(filename) <= max_len:
            return filename
            
        part_len = (max_len - 1) // 2
        return f"{filename[:part_len]}…{filename[-part_len:]}"

    # --- Render Logic ---

    def _render_list(self, items: List[Any], available_lines: int,
                     levels: List[Tuple[str, int]], render_func, show_more: bool = True) -> Table:
        """Generic list renderer with density degradation.

        Args:
            items: Items to render
            available_lines: Available content lines (without borders)
            levels: List of (level_name, lines_per_item) tuples
            render_func: Function to render each item
            show_more: If False, never show "...+N more" line (default True)
        """
        table = Table(show_header=False, box=None, padding=(0, 0), expand=True)
        table.add_column("Content", ratio=1)
        
        if available_lines <= 0:
            return table
            
        if not items:
            # table.add_row("[dim]Empty[/]") 
            # Better to show nothing for empty lists to save space visual noise
            return table

        selected_level = levels[-1][0] # Default to lowest density
        items_to_show = []
        has_more = False
        more_count = 0
        
        # Select highest density that allows showing at least 1 item
        for level_name, lines_per_item in levels:
            max_items = available_lines // lines_per_item
            if max_items >= 1:
                selected_level = level_name
                # Check if we need "more" line
                if len(items) <= max_items:
                    items_to_show = items
                    has_more = False
                elif show_more and available_lines >= lines_per_item + 1: # Reserve 1 line for "... +N more"
                     max_items_res = (available_lines - 1) // lines_per_item
                     if max_items_res >= 1:
                         items_to_show = items[:max_items_res]
                         more_count = len(items) - max_items_res
                         has_more = True
                     else:
                         # Not enough space for more line, show what fits
                         items_to_show = items[:max_items]
                         has_more = False # Or just implicit cut
                else:
                    # show_more=False or not enough space: just show what fits
                    items_to_show = items[:max_items]
                    has_more = False
                    more_count = 0
                break
        
        # Render items
        for i, item in enumerate(items_to_show):
            content = render_func(item, selected_level)
            table.add_row(content)
            # Add spacer if needed? No, strict lines packing.
            
        if has_more and more_count > 0:
            table.add_row(f"[dim]… +{more_count} more")
            
        return table

    def _render_active_job(self, job, level: str) -> RenderableType:
        """Render active job with dynamic layout based on available width."""
        # Calculate panel width based on layout mode
        term_w = self.console.size.width
        if term_w >= MIN_2COL_W:
            panel_w = max(40, (term_w // 2) - 4)  # 2-column mode: half width
        else:
            panel_w = max(40, term_w - 4)  # 1-column mode: full width

        spinner_frames = "●○◉◎"
        spinner_rotating = "◐◓◑◒"
        use_spinner = spinner_rotating if (job.rotation_angle or 0) > 0 else spinner_frames

        # Metadata
        meta = job.source_file.metadata
        dur_sec = meta.duration if meta else 0
        dur = self.format_time(dur_sec)
        fps = self.format_fps(meta)
        size = self.format_size(job.source_file.size_bytes)

        # Progress
        pct = job.progress_percent or 0.0

        # ETA calculation
        eta_str = "--:--"
        start_key = job.source_file.path.name
        if start_key in self.state.job_start_times:
            elapsed = (datetime.now() - self.state.job_start_times[start_key]).total_seconds()
            if 0 < pct < 100 and elapsed > 0:
                eta_seconds = (elapsed / pct) * (100 - pct)
                eta_str = self.format_time(eta_seconds)

        # Build 3 elements: name, metadata, progress bar
        filename_max = max(30, panel_w - 3)  # Uniform truncation for both modes
        filename = self._sanitize_filename(job.source_file.path.name, max_len=filename_max)
        spinner = use_spinner[(self._spinner_frame + hash(filename)) % len(use_spinner)]

        name_line = f"[green]{spinner}[/] {filename}"
        meta_text = f"dur {dur} • {fps} • in {size}"

        # Calculate widths for layout decision
        # panel_w already accounts for borders/spacing, use directly
        usable_width = panel_w

        # Calculate actual widths (visible characters, not markup)
        name_width = len(filename) + 3  # spinner + space
        meta_width = len(meta_text)  # actual metadata text length

        # Progress components: bar + percent + bullet + eta
        pct_width = 5  # "100.0%" (without leading space)
        bullet_width = 1  # "•"
        eta_width = 5  # "00:00"
        bar_min_width = 15  # Minimum bar width

        # Grid has 6 columns, so 5 spaces between them
        column_spacing = 5

        # Fixed columns (everything except bar)
        fixed_columns = name_width + meta_width + pct_width + bullet_width + eta_width

        # Calculate available space for progress bar
        bar_available = usable_width - fixed_columns - column_spacing

        # Decide layout based on available width
        # In narrow mode (1-column), always use 2-line layout (skip 1-line option)
        is_narrow_mode = term_w < MIN_2COL_W

        # Option 1: Everything in 1 line (name + meta + progress) - ONLY in wide mode
        if not is_narrow_mode and bar_available >= bar_min_width:
            # 1 line: spinner filename • dur ... • fps • size [===] 8.4% • 07:46
            bar = ProgressBar(total=100, completed=int(pct), width=bar_available)
            l1_grid = Table.grid(padding=(0, 1))
            l1_grid.add_row(
                f"[green]{spinner}[/] {filename}",
                f"[dim]{meta_text}[/]",
                bar,
                f"{pct:>5.1f}%",
                "•",
                eta_str
            )
            return l1_grid
        # Option 2: Name + meta on L1 (meta right-aligned), progress on L2
        else:
            # Check if name + meta fits on L1
            if (name_width + meta_width + 1) <= usable_width:  # +1 for separator
                # 2 lines: name + meta (right-aligned) | progress bar
                # Recalculate filename to fit with metadata
                available_for_name = usable_width - meta_width - 5  # -5 for spinner + spacing + separator
                filename_2line = self._sanitize_filename(job.source_file.path.name, max_len=max(20, available_for_name))

                # L1: use grid with name on left, meta on right
                l1_grid = Table.grid(padding=(0, 1), expand=True)
                l1_grid.add_column(ratio=1)  # Name (flex)
                l1_grid.add_column(justify="right")  # Meta (right-aligned)
                l1_grid.add_row(
                    f"[green]{spinner}[/] {filename_2line}",
                    f"[dim]{meta_text}[/]"
                )

                # L2: full-width progress bar
                # L2: bar + pct + bullet + eta
                fixed_l2 = pct_width + bullet_width + eta_width
                column_spacing_l2 = 3  # 4 columns = 3 spaces
                bar_available_l2 = usable_width - fixed_l2 - column_spacing_l2
                bar = ProgressBar(total=100, completed=int(pct), width=bar_available_l2)
                l2_grid = Table.grid(padding=(0, 1))
                l2_grid.add_row(bar, f"{pct:>5.1f}%", "•", eta_str)
                return Group(l1_grid, l2_grid)
            else:
                # 3 lines: name | metadata | progress
                # L3: " " + bar + pct + bullet + eta
                indent_width_l3 = 1  # " "
                fixed_l3 = indent_width_l3 + pct_width + bullet_width + eta_width
                column_spacing_l3 = 3  # 4 columns = 3 spaces
                bar_available_l3 = usable_width - fixed_l3 - column_spacing_l3
                bar = ProgressBar(total=100, completed=int(pct), width=max(bar_min_width, bar_available_l3))

                l2 = f"  [dim]{meta_text}[/]"
                l3_grid = Table.grid(padding=(0, 1))
                l3_grid.add_row(" ", bar, f"{pct:>5.1f}%", "•", eta_str)
                return Group(name_line, l2, l3_grid)

    def _render_activity_item(self, job, level: str) -> RenderableType:
        """Render activity feed item with dynamic width."""
        # Calculate panel width based on layout mode
        term_w = self.console.size.width
        if term_w >= MIN_2COL_W:
            panel_w = max(40, (term_w // 2) - 4)  # 2-column mode: half width
        else:
            panel_w = max(40, term_w - 4)  # 1-column mode: full width

        if job.status == JobStatus.COMPLETED:
            icon = "[green]✓[/]"
            in_s = job.source_file.size_bytes
            out_s = job.output_size_bytes
            diff = in_s - out_s
            ratio = (diff / in_s) * 100 if in_s > 0 else 0
            dur = self.format_time(job.duration_seconds)

            s_in = self.format_size(in_s)
            s_out = self.format_size(out_s)

            # ✓ is 1-column, needs space
            if level == "A": # 2 lines
                # L1: ✓ filename (only icon + space needed, ~3 chars)
                # L2: size → size (ratio%) • duration
                filename_max = max(25, panel_w - 3)  # Reserve only for icon + space
                filename = self._sanitize_filename(job.source_file.path.name, max_len=filename_max)
                l1 = f"{icon} {filename}"
                l2 = f"  [green]{s_in} → {s_out} ({ratio:.1f}%) • {dur}[/]"
                return Group(l1, l2)
            else: # B: 1 line
                # ✓ filename  |  size → size (ratio%) • duration (right-aligned)
                filename_max = max(20, panel_w - 42)  # Reserve for icon + stats
                filename = self._sanitize_filename(job.source_file.path.name, max_len=filename_max)
                grid = Table.grid(padding=(0, 1), expand=True)
                grid.add_column(ratio=1)  # Filename (left, flex)
                grid.add_column(justify="right")  # Stats (right-aligned)
                grid.add_row(
                    f"{icon} {filename}",
                    f"[green]{s_in} → {s_out} ({ratio:.1f}%) • {dur}[/]"
                )
                return grid

        elif job.status == JobStatus.SKIPPED:
            # Kept original logic usually means ratio check or similar
            icon = "[dim]≡[/]"
            reason = "kept"
            if level == "A":
                filename_max = max(25, panel_w - 3)
                filename = self._sanitize_filename(job.source_file.path.name, max_len=filename_max)
                return Group(f"{icon} {filename}", f"  [dim]{reason} (below threshold)[/]")
            else:
                filename_max = max(20, panel_w - 15)
                filename = self._sanitize_filename(job.source_file.path.name, max_len=filename_max)
                grid = Table.grid(padding=(0, 1), expand=True)
                grid.add_column(ratio=1)
                grid.add_column(justify="right")
                grid.add_row(f"{icon} {filename}", f"[dim]{reason}[/]")
                return grid

        elif job.status == JobStatus.FAILED:
            icon = "[red]✗[/]"
            err = job.error_message or "error"
            if level == "A":
                filename_max = max(25, panel_w - 3)
                filename = self._sanitize_filename(job.source_file.path.name, max_len=filename_max)
                return Group(f"{icon} {filename}", f"  [red]{err}[/]")
            else:
                filename_max = max(20, panel_w - 15)
                filename = self._sanitize_filename(job.source_file.path.name, max_len=filename_max)
                grid = Table.grid(padding=(0, 1), expand=True)
                grid.add_column(ratio=1)
                grid.add_column(justify="right")
                grid.add_row(f"{icon} {filename}", f"[red]err[/]")
                return grid

        elif job.status == JobStatus.INTERRUPTED:
             # ⚡ is 2-column wide, no space needed
             icon = "[red]⚡[/]"
             filename_max = max(25, panel_w - 15)
             filename = self._sanitize_filename(job.source_file.path.name, max_len=filename_max)
             grid = Table.grid(padding=(0, 1), expand=True)
             grid.add_column(ratio=1)
             grid.add_column(justify="right")
             grid.add_row(f"{icon}{filename}", f"[red]INTERRUPTED[/]")
             return grid

        filename_max = max(25, panel_w - 3)
        filename = self._sanitize_filename(job.source_file.path.name, max_len=filename_max)
        return f"? {filename}"

    def _render_queue_item(self, file, level: str) -> RenderableType:
        """Render queue item (always 1 line) with dynamic filename width."""
        size = self.format_size(file.size_bytes)
        fps = self.format_fps(file.metadata)

        # Calculate available width for filename based on layout mode
        term_w = self.console.size.width
        if term_w >= MIN_2COL_W:
            panel_w = max(40, (term_w // 2) - 4)  # 2-column mode: half width
        else:
            panel_w = max(40, term_w - 4)  # 1-column mode: full width

        # Reserve space for: "» " (2) + size (9) + " " (1) + fps (6) + spacing (2) = ~20
        reserved = 20
        filename_max = max(20, panel_w - reserved)
        filename = self._sanitize_filename(file.path.name, max_len=filename_max)

        # Use grid with padding between columns
        grid = Table.grid(padding=(0, 1), expand=True)  # 1 space padding between columns
        grid.add_column(ratio=1)  # Filename (flex)
        grid.add_column(justify="right", width=9)  # Size (fixed 9 chars)
        grid.add_column(justify="right", width=6)  # FPS (fixed 6 chars)
        grid.add_row(
            f"[dim]»[/] {filename}",
            f"[dim]{size}[/]",
            f"[dim]{fps}[/]" if fps else ""
        )
        return grid

    # --- Panel Generators ---

    def _generate_top_bar(self) -> Panel:
        """Status, KPI, Hints + GPU Metrics."""
        with self.state._lock:
            # L1: Status + Threads
            indicator = "[green]●[/]"
            if self.state.finished:
                status = "[green]FINISHED[/]"
            elif self.state.interrupt_requested:
                status = "[bright_red]INTERRUPTED[/]"
                indicator = "[red]![/]"
            elif self.state.shutdown_requested:
                status = "[yellow]SHUTTING DOWN[/]"
                indicator = "[yellow]◐[/]"
            else:
                status = "[bright_cyan]ACTIVE[/]"
            
            # 1. Prepare KPI variables
            active_threads = len(self.state.active_jobs)
            paused = "" # Logic for paused could be added here
            
            eta_str = "--:--"
            throughput_str = "0.0 MB/s"
            
            if self.state.processing_start_time and self.state.completed_count > 0:
                elapsed = (datetime.now() - self.state.processing_start_time).total_seconds()
                if elapsed > 0:
                    total = self.state.files_to_process
                    done = self.state.completed_count + self.state.failed_count
                    rem = total - done
                    if rem > 0 and done > 0:
                        avg = elapsed / done
                        eta_str = self.format_global_eta(avg * rem)
                    
                    tp = self.state.total_input_bytes / elapsed
                    throughput_str = f"{tp / 1024 / 1024:.1f} MB/s"
            
            saved = self.format_size(self.state.space_saved_bytes)
            ratio = self.state.compression_ratio

            # Thread display: show single number or transition
            # During shutdown, target is 0; otherwise use configured threads
            target_threads = 0 if self.state.shutdown_requested else self.state.current_threads

            if active_threads == target_threads:
                threads_display = str(active_threads)
            else:
                threads_display = f"{active_threads} → {target_threads}"

            # 2. Build Left Content (Fixed 3 lines)
            l1 = f"{indicator} {status} • Threads: {threads_display}{paused}"
            l2 = f"ETA: {eta_str} • {throughput_str} • {saved} saved ({(1-ratio)*100:.1f}%)"
            l3 = "[dim]Press M for menu[/]"
            left_content = f"{l1}\n{l2}\n{l3}"

            # 3. GPU Metrics (Right Side)
            if self.state.gpu_data:
                g = self.state.gpu_data
                
                def _p(s):
                    m = re.search(r"(\d+\.?\d*)", str(s))
                    return float(m.group(1)) if m else 0.0

                def _c(val, norm, high, op_le=False):
                    if op_le: # <= norm
                        if val <= norm: return "green"
                    else: # < norm
                        if val < norm: return "green"
                    if val > high: return "red"
                    return "yellow"

                # Parse values
                t_val = _p(g.get("temp", "0"))
                f_val = _p(g.get("fan_speed", "0"))
                p_val = _p(g.get("power_draw", "0"))
                gu_val = _p(g.get("gpu_util", "0"))
                mu_val = _p(g.get("mem_util", "0"))

                # Colors
                t_col = _c(t_val, 55, 65)
                f_col = _c(f_val, 50, 75, op_le=True)
                p_col = _c(p_val, 250, 380)
                gu_col = _c(gu_val, 30, 60)
                mu_col = _c(mu_val, 30, 60)

                # Format GPU lines
                gl1 = f"[dim]{g.get('device_name', 'GPU')}[/]"

                # GL2: Current metrics with reverse highlighting for selected metric
                with self.state._lock:
                    metric_idx = self.state.gpu_sparkline_metric_idx
                    sparkline_preset = self.state.gpu_sparkline_preset
                    sparkline_palette = self.state.gpu_sparkline_palette
                    sparkline_mode = self.state.gpu_sparkline_mode

                # Build GL2 with conditional reverse for active metric
                # metric_idx follows the GPU sparkline metric order
                temp_str = f"[{t_col}]{g.get('temp', '??')}[/]"
                fan_str = f"[{f_col}]fan {g.get('fan_speed', '??')}[/]"
                pwr_str = f"[{p_col}]pwr {g.get('power_draw', '??')}[/]"
                gpu_str = f"[{gu_col}]gpu {g.get('gpu_util', '??')}[/]"
                mem_str = f"[{mu_col}]mem {g.get('mem_util', '??')}[/]"

                # Apply reverse to selected metric (order: temp → fan → pwr → gpu → mem)
                if metric_idx == 0:
                    temp_str = f"[reverse]{temp_str}[/]"
                elif metric_idx == 1:
                    fan_str = f"[reverse]{fan_str}[/]"
                elif metric_idx == 2:
                    pwr_str = f"[reverse]{pwr_str}[/]"
                elif metric_idx == 3:
                    gpu_str = f"[reverse]{gpu_str}[/]"
                elif metric_idx == 4:
                    mem_str = f"[reverse]{mem_str}[/]"

                gl2 = f"{temp_str} • {fan_str} • {pwr_str} • {gpu_str} • {mem_str}"

                spark_cfg = get_gpu_sparkline_config(sparkline_preset)
                palette = get_gpu_sparkline_palette(sparkline_palette)
                if spark_cfg.metrics:
                    metric_idx = metric_idx % len(spark_cfg.metrics)
                else:
                    metric_idx = 0

                # GL3: Sparkline (without label)
                with self.state._lock:
                    if spark_cfg.metrics:
                        metric = spark_cfg.metrics[metric_idx]
                        history = getattr(self.state, metric.history_attr)
                    else:
                        metric = None
                        history = []

                    # Calculate sparkline length (full width, no label)
                    term_w = self.console.size.width
                    gpu_panel_w = max(20, (term_w // 2) - 4)
                    spark_len = max(1, gpu_panel_w)

                    if metric is None:
                        spark = " " * spark_len
                        gl3 = spark
                    else:
                        if sparkline_mode == "palette":
                            spark = render_sparkline(
                                history,
                                spark_len,
                                metric.min_val,
                                metric.max_val,
                                spark_cfg.style,
                                palette=palette.colors,
                                glyph=PALETTE_GLYPH,
                            )
                            gl3 = spark or " " * spark_len
                        else:
                            spark = render_sparkline(
                                history,
                                spark_len,
                                metric.min_val,
                                metric.max_val,
                                spark_cfg.style,
                            )
                            gl3 = f"[dim cyan]{spark}[/]" if spark else " " * spark_len

                gpu_content = f"{gl1}\n{gl2}\n{gl3}"

                # Create Grid for two columns
                grid = Table.grid(expand=True)
                grid.add_column(ratio=1) # Left
                grid.add_column(justify="right") # Right
                grid.add_row(left_content, gpu_content)
                content = grid
            else:
                content = left_content
            
        return Panel(content, border_style="cyan", title=self.state.ui_title)

    def _generate_progress(self, h_lines: int) -> Panel:
        """Progress bar + counters (size-based progress)."""
        with self.state._lock:
            # Liczby plików (header: session/total/all)

            # Oblicz całkowity rozmiar i przetworzony rozmiar
            total_size_bytes = 0
            processed_size_bytes = 0

            # Pending files
            for file in self.state.pending_files:
                total_size_bytes += file.size_bytes

            # Active jobs
            for job in self.state.active_jobs:
                total_size_bytes += job.source_file.size_bytes

            # Completed files
            processed_size_bytes = self.state.total_input_bytes
            total_size_bytes += processed_size_bytes

            # Progress % (oparty na rozmiarach, nie liczbie plików)
            pct = 0.0
            if total_size_bytes > 0:
                pct = (processed_size_bytes / total_size_bytes) * 100

            # Elapsed time
            elapsed_str = "--:--"
            if self.state.processing_start_time:
                elapsed = (datetime.now() - self.state.processing_start_time).total_seconds()
                elapsed_str = self.format_time(elapsed)

            # Header (liczby plików + source folders jeśli > 1)
            session_done = max(0, self.state.completed_count - self.state.session_completed_base)
            completed_since_discovery = max(
                0, self.state.completed_count - self.state.completed_count_at_last_discovery
            )
            total_done = self.state.already_compressed_count + completed_since_discovery
            if total_done < self.state.completed_count:
                total_done = self.state.completed_count
            total_files = self.state.total_files_found
            if total_files > 0:
                total_done = min(total_done, total_files)
            if self.state.source_folders_count > 1:
                header = f"Done: {session_done}/{total_done}/{total_files} • Sources: {self.state.source_folders_count}"
            else:
                header = f"Done: {session_done}/{total_done}/{total_files}"

            # Progress bar (skalowany do 0-10000 aby uniknąć problemów z dużymi liczbami)
            if total_size_bytes > 0:
                scaled_total = 10000
                scaled_processed = int((processed_size_bytes / total_size_bytes) * 10000)
            else:
                scaled_total = 100
                scaled_processed = 0

            bar = ProgressBar(total=scaled_total, completed=scaled_processed, width=None)

            # Format rozmiarów
            processed_str = self.format_size(processed_size_bytes)
            total_str = self.format_size(total_size_bytes)
            sizes_str = f"{processed_str}/{total_str}"

            # Bar + rozmiary + bullet + procent + bullet + czas
            bar_grid = Table.grid(padding=(0, 1))
            bar_grid.add_row(bar, sizes_str, "•", f"{pct:.1f}%", "•", elapsed_str)

            rows = [header, bar_grid, ""]
            content = Group(*rows)

        return Panel(content, title="PROGRESS", border_style="cyan")

    def _generate_active_jobs_panel(self, h_lines: int) -> Panel:
        with self.state._lock:
            # Limit jobs to max_active_jobs to avoid "...+N more" when at exactly the limit
            jobs = self.state.active_jobs[:self.max_active_jobs]
            # Dynamic layout: reserve space based on terminal width
            term_w = self.console.size.width
            if term_w >= MIN_2COL_W:
                # Wide mode: can use 1-3 lines per job
                levels = [("dynamic", 3), ("compact", 2)]
            else:
                # Narrow mode: always 2 lines per job (max 8 jobs)
                levels = [("dynamic", 2)]
            # Never show "...+N more" for active jobs panel
            table = self._render_list(jobs, h_lines, levels, self._render_active_job, show_more=False)
            return Panel(table, title="ACTIVE JOBS", border_style="cyan")

    def _generate_activity_panel(self, h_lines: int) -> Panel:
        with self.state._lock:
            jobs = list(self.state.recent_jobs) # already sorted roughly
            # In narrow mode (1-column), use more compact levels
            term_w = self.console.size.width
            if term_w >= MIN_2COL_W:
                levels = [("A", 2), ("B", 1)]  # 2-column: both levels
            else:
                levels = [("B", 1)]  # 1-column: only 1-line format
            table = self._render_list(jobs, h_lines, levels, self._render_activity_item)
            return Panel(table, title="ACTIVITY FEED", border_style="cyan")

    def _generate_queue_panel(self, h_lines: int) -> Panel:
        with self.state._lock:
            files = list(self.state.pending_files)
            levels = [("A", 1)]
            table = self._render_list(files, h_lines, levels, self._render_queue_item)
            return Panel(table, title="QUEUE", border_style="cyan")
            
    def _generate_footer(self) -> RenderableType:
        with self.state._lock:
            # Session stats
            failed = self.state.failed_count
            skipped = self.state.skipped_count
            
            # Persistent/Discovery stats
            err = self.state.ignored_err_count
            hw = self.state.hw_cap_count
            kept = self.state.min_ratio_skip_count
            small = self.state.ignored_small_count
            av1 = self.state.ignored_av1_count
            cam = self.state.cam_skipped_count
            
            # Show all stats (including zeros) on start or when legend is active
            show_zeros = False
            if self.state.discovery_finished and self.state.discovery_finished_time:
                 if (datetime.now() - self.state.discovery_finished_time).total_seconds() < 5:
                     show_zeros = True

            # Also show all stats when overlay is active (especially Reference tab)
            if self.state.show_overlay:
                show_zeros = True
            
            parts = []
            
            # Helper to add part
            def add(val, label, style):
                if val > 0 or show_zeros:
                    parts.append(f"[{style}]{label}:{val}[/]")

            add(failed, "fail", "red")
            add(err, "err", "red")
            add(hw, "hw_cap", "yellow")
            add(skipped, "skip", "yellow")
            add(kept, "kept", "dim white")
            add(small, "small", "dim white")
            add(av1, "av1", "dim white")
            add(cam, "cam", "dim white")
            
            health_text = " • ".join(parts) if parts else "[green]Health: OK[/]"
            
            # Left side: Last Action with fading
            action_text = ""
            if self.state.last_action and self.state.last_action_time:
                age = (datetime.now() - self.state.last_action_time).total_seconds()
                if age < 15:
                    if age < 5:
                        style = "white"        # Stage 1: Bright
                    elif age < 10:
                        style = "grey70"       # Stage 2: Dim
                    else:
                        style = "grey30"       # Stage 3: Fading out
                    
                    action_text = f"[{style}]{self.state.last_action}[/]"

            grid = Table.grid(expand=True)
            grid.add_column(width=1) # Left padding
            grid.add_column(justify="left", ratio=1)
            grid.add_column(justify="right", ratio=1)
            grid.add_column(width=1) # Right padding
            grid.add_row("", action_text, health_text, "")
            
            return grid

    def _generate_tabbed_overlay(self) -> Panel:
        """Generate unified tabbed overlay with dynamic width."""

        with self.state._lock:
            active_tab = self.state.active_tab
            config_lines = self.state.config_lines[:]
            dim_level = self.state.overlay_dim_level
            input_dir_stats = self.state.io_input_dir_stats[:]
            output_dir_lines = self.state.io_output_dir_lines[:]
            errors_dir_lines = self.state.io_errors_dir_lines[:]
            suffix_output_dirs = self.state.io_suffix_output_dirs
            suffix_errors_dirs = self.state.io_suffix_errors_dirs
            queue_sort = self.state.io_queue_sort
            queue_seed = self.state.io_queue_seed
            log_path = self.state.log_path
            debug_enabled = self.state.debug_enabled
            sparkline_preset = self.state.gpu_sparkline_preset
            sparkline_palette = self.state.gpu_sparkline_palette
            sparkline_mode = self.state.gpu_sparkline_mode

        # Get console dimensions for responsive sizing
        w = self.console.size.width
        pw = max(85, w - 10)  # Use full width minus small margins

        # === TAB HEADER ===
        tabs_table = Table(show_header=False, box=None, expand=True, padding=0)
        tabs_table.add_column(ratio=1)
        tabs_table.add_column(ratio=1)
        tabs_table.add_column(ratio=1)
        tabs_table.add_column(ratio=1)
        tabs_table.add_column(ratio=1)

        def tab_style(tab_id: str) -> Tuple[str, str, Any]:
            """Return (text_style, border_style, box_type) for tab."""
            if tab_id == active_tab:
                return ("bold white", "green", ROUNDED)
            return ("dim", "dim", SIMPLE)

        shortcuts_text, shortcuts_border, shortcuts_box = tab_style("shortcuts")
        settings_text, settings_border, settings_box = tab_style("settings")
        io_text, io_border, io_box = tab_style("io")
        tui_text, tui_border, tui_box = tab_style("tui")
        reference_text, reference_border, reference_box = tab_style("reference")

        # Tab order: Shortcuts (M), Settings (C), I/O (F), TUI (T), Reference (L)
        tabs_table.add_row(
            Panel(
                f"[{shortcuts_text}]⌨ Shortcuts[/] [{shortcuts_text}][M][/]",
                border_style=shortcuts_border,
                box=shortcuts_box,
                padding=(0, 1),
            ),
            Panel(
                f"[{settings_text}]⚙ Settings[/] [{settings_text}][C][/]",
                border_style=settings_border,
                box=settings_box,
                padding=(0, 1),
            ),
            Panel(
                f"[{io_text}]📁 I/O[/] [{io_text}][F][/]",
                border_style=io_border,
                box=io_box,
                padding=(0, 1),
            ),
            Panel(
                f"[{tui_text}]◈ TUI[/] [{tui_text}][T][/]",
                border_style=tui_border,
                box=tui_box,
                padding=(0, 1),
            ),
            Panel(
                f"[{reference_text}]📖 Reference[/] [{reference_text}][L][/]",
                border_style=reference_border,
                box=reference_box,
                padding=(0, 1),
            ),
        )

        # === ACTIVE TAB CONTENT ===
        if active_tab == "shortcuts":
            content = render_shortcuts_content()
        elif active_tab == "settings":
            content = render_settings_content(config_lines, self._spinner_frame, log_path, debug_enabled)
        elif active_tab == "io":
            content = render_io_content(
                config_lines,
                input_dir_stats,
                output_dir_lines,
                errors_dir_lines,
                suffix_output_dirs,
                suffix_errors_dirs,
                queue_sort,
                queue_seed,
            )
        elif active_tab == "tui":
            content = render_tui_content(dim_level, sparkline_preset, sparkline_palette, sparkline_mode)
        else:  # reference
            content = render_reference_content(
                self._spinner_frame,
                sparkline_preset,
                sparkline_palette,
                sparkline_mode,
            )

        # === FOOTER ===
        footer = Text.from_markup(
            "[dim]Press [white on #30363d] Tab [/] next • "
            "[white on #30363d] Esc [/] close[/]",
            justify="center"
        )

        # === COMPOSE ===
        full_content = Group(
            tabs_table,
            Rule(style="#30363d"),
            "",
            content,
            "",
            Rule(style="#30363d"),
            footer,
        )

        return Panel(
            full_content,
            border_style="cyan",
            box=ROUNDED,
            padding=(1, 2),
            width=pw,  # Dynamic width
        )

    # --- Main Layout Engine ---

    def create_display(self):
        w, h = self.console.size
        
        # 1. Determine fixed heights
        top_h = TOP_BAR_LINES + 2 # +2 for border
        foot_h = FOOTER_LINES 
        
        # Hint logic
        show_hint = h >= 18
        if not show_hint:
            # We need to hack the top bar content if we hide hint, 
            # but for now simpler is just keep Top Bar as is, 
            # or recreate it without line 3.
            # Let's handle it by passing param to _generate_top_bar if needed, 
            # but spec says "Top bar (2-3 lines)". Let's assume Top Bar is elastic based on logic inside.
            pass
            
        fixed_h = top_h + foot_h
        h_work = max(0, h - fixed_h)
        # Scale applied inside panel sizing, not to h_work

        # 2. Determine Mode
        is_2col = w >= MIN_2COL_W

        # 3. Allocation

        # Defaults
        h_progress = 0
        h_active = 0
        h_activity = 0
        h_queue = 0

        layout = Layout()
        layout.split_column(
            Layout(name="top", size=top_h),        # Top status bar
            Layout(name="middle"),                 # Main content (flex)
            Layout(name="bottom", size=foot_h)     # Bottom status
        )
        
        if is_2col:
            # 2 Columns
            layout["middle"].split_row(
                Layout(name="left"),     # Progress + Active Jobs
                Layout(name="right")     # Activity Feed + Queue
            )
            
            # Left column: Progress (fixed) + Active (fixed for 8 jobs)
            h_progress_frame = PROGRESS_MAX + 2  # 3 + 2 = 5
            h_active_frame = (self.max_active_jobs * 3) + 2  # 8 × 3 + 2 = 26
            h_left_total = h_progress_frame + h_active_frame  # 5 + 26 = 31

            # CRITICAL: Clamp to available h_work
            if h_left_total > h_work:
                h_left_total = h_work
                h_active_frame = max(ACTIVE_MIN + 2, h_work - h_progress_frame)
                h_progress_frame = h_work - h_active_frame

            # Right column: Activity (fixed) + Queue (adjusts to match left column height)
            max_activity_items = self.state.recent_jobs.maxlen
            h_activity_frame = (max_activity_items * 2) + 2  # N × 2 + 2

            # Queue adjusts so right column height = left column height
            h_queue_frame = h_left_total - h_activity_frame

            # Hide queue if too small
            if h_queue_frame < (QUEUE_MIN + 2):
                h_queue_frame = 0
                # Don't expand activity - keep it fixed, right column will be shorter
            
            # Update Layouts
            layout["left"].split_column(
                Layout(name="progress", size=h_progress_frame),
                Layout(name="active", size=h_active_frame)
            )
            
            right_splits = [Layout(name="activity", size=h_activity_frame)]
            if h_queue_frame > 0:
                right_splits.append(Layout(name="queue", size=h_queue_frame))
            layout["right"].split_column(*right_splits)
            
            # Content Heights (remove 2 for borders)
            h_progress = max(0, h_progress_frame - 2)
            h_active = max(0, h_active_frame - 2)
            h_activity = max(0, h_activity_frame - 2)
            h_queue = max(0, h_queue_frame - 2)

        else:
            # 1 Column (Stack)
            # Progress > Active > Activity > Queue
            h_rem = h_work

            h_progress_frame = min(PROGRESS_MAX + 2, h_rem)
            h_rem -= h_progress_frame

            # Dynamic sizing: ACTIVE and ACTIVITY take only what they need, QUEUE gets the rest
            with self.state._lock:
                actual_jobs = len(self.state.active_jobs)
                actual_activity = len(self.state.recent_jobs)

            # ACTIVE JOBS: 2 lines per job in narrow mode (max 8 jobs), +2 for borders if not empty
            active_content = min(actual_jobs, self.max_active_jobs)
            h_active_frame = (active_content * 2 + 2) if active_content > 0 else 0

            # ACTIVITY FEED: 1 line per item (max 5), +2 for borders if not empty
            activity_content = min(actual_activity, 5)
            h_activity_frame = (activity_content + 2) if activity_content > 0 else 0

            # QUEUE: Gets remaining space (minimum 3 lines for borders + at least 1 item)
            h_queue_frame = h_rem - h_active_frame - h_activity_frame
            if h_queue_frame < 3:  # Not enough for even empty queue
                h_queue_frame = 0

            splits = [Layout(name="progress", size=h_progress_frame)]
            if h_active_frame > 0:
                splits.append(Layout(name="active", size=h_active_frame))
            if h_activity_frame > 0:
                splits.append(Layout(name="activity", size=h_activity_frame))
            if h_queue_frame > 0:
                splits.append(Layout(name="queue", size=h_queue_frame))

            layout["middle"].split_column(*splits)

            h_progress = max(0, h_progress_frame - 2)
            h_active = max(0, h_active_frame - 2)
            h_activity = max(0, h_activity_frame - 2)
            h_queue = max(0, h_queue_frame - 2)

        # 4. Generate Content
        layout["top"].update(self._generate_top_bar())

        # Middle components
        def safe_update(name, content):
            try:
                pass 
            except: pass

        # Assign directly based on tree structure we just built
        if is_2col:
             layout["left"]["progress"].update(self._generate_progress(h_progress))
             layout["left"]["active"].update(self._generate_active_jobs_panel(h_active))
             layout["right"]["activity"].update(self._generate_activity_panel(h_activity))
             if h_queue_frame > 0:
                 layout["right"]["queue"].update(self._generate_queue_panel(h_queue))
        else:
             layout["middle"]["progress"].update(self._generate_progress(h_progress))
             if h_active_frame > 0:
                 layout["middle"]["active"].update(self._generate_active_jobs_panel(h_active))
             if h_activity_frame > 0:
                 layout["middle"]["activity"].update(self._generate_activity_panel(h_activity))
             if h_queue_frame > 0:
                 layout["middle"]["queue"].update(self._generate_queue_panel(h_queue))

        # Footer
        layout["bottom"].update(self._generate_footer())

        # Overlay
        if self.state.show_overlay:
            w = self.console.size.width
            overlay_w = max(85, w - 10)  # Dynamic width, full console minus margins
            return _Overlay(
                layout,
                self._generate_tabbed_overlay(),
                overlay_width=overlay_w,
                dim_level=self.state.overlay_dim_level,
            )
        elif self.state.show_info:
             info = Panel(Align.center(self.state.info_message), title="NOTICE", border_style="yellow", width=60)
             return _Overlay(layout, info, overlay_width=60)

        return layout

    def _refresh_loop(self):
        while not self._stop_refresh.is_set():
            if self._live:
                self._spinner_frame = (self._spinner_frame + 1) % 5
                try:
                    display = self.create_display()
                    with self._ui_lock:
                        self._live.update(display)
                except Exception:
                    pass # Resilience
            time.sleep(0.5)

    def start(self):
        self._live = Live(self.create_display(), console=self.console, refresh_per_second=4)
        self._live.start()
        self._stop_refresh.clear()
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()
        return self

    def stop(self):
        self._stop_refresh.set()
        if self._refresh_thread:
            self._refresh_thread.join(timeout=1.0)
        if self._live:
            # Final update to show INTERRUPTED/FINISHED state
            try:
                self._live.update(self.create_display())
            except:
                pass
            self._live.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False
