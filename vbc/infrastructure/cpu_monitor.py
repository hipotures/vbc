"""CPU/memory samples for the TUI's metrics panel when the server has no GPU.

Feeds the same sink and sample shape as GpuMonitor, so the panel shows CPU
graphs instead of GPU ones: seeing them means the server encodes on the CPU.
Reads /proc/stat, /proc/meminfo (virtualised per container by lxcfs) and,
when visible, a thermal zone.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional, Tuple

from vbc.infrastructure.gpu_monitor import GpuSample, GpuSink

logger = logging.getLogger(__name__)


def _cpu_times() -> Optional[Tuple[int, int]]:
    """(busy, total) jiffies over all CPUs."""
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    except (OSError, IndexError):
        return None
    values = [int(v) for v in fields[:8]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total - idle, total


def _mem_used_pct() -> Optional[float]:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0])
        return 100.0 * (1 - info["MemAvailable"] / info["MemTotal"])
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def _cpu_temp_c() -> Optional[float]:
    best = None
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            kind = (zone / "type").read_text().strip().lower()
            value = int((zone / "temp").read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue
        if any(k in kind for k in ("x86_pkg", "cpu", "k10temp", "coretemp", "tctl")):
            return value
        best = value if best is None else best
    return best


class CpuMonitor:
    """Samples CPU utilisation, memory use and CPU temperature every refresh_rate s."""

    def __init__(self, sink: GpuSink, refresh_rate: int = 5) -> None:
        self.sink = sink
        self.refresh_rate = refresh_rate
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cores = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()

    def _sample(self, before, after) -> GpuSample:
        util = None
        if before and after and after[1] > before[1]:
            util = 100.0 * (after[0] - before[0]) / (after[1] - before[1])
        mem = _mem_used_pct()
        temp = _cpu_temp_c()
        raw = {
            "device_name": f"CPU ({self._cores} cores) - no GPU",
            "temp": f"{temp:.0f}C" if temp is not None else "-",
            "fan_speed": "-",
            "power_draw": "-",
            "gpu_util": f"{util:.0f}%" if util is not None else "-",
            "mem_util": f"{mem:.0f}%" if mem is not None else "-",
        }
        return GpuSample(raw=raw, temp_c=temp, gpu_util_pct=util, mem_util_pct=mem)

    def _run(self) -> None:
        before = _cpu_times()
        while not self._stop.wait(self.refresh_rate):
            after = _cpu_times()
            try:
                self.sink.add_gpu_sample(self._sample(before, after))
            except Exception:  # noqa: BLE001
                logger.exception("CPU monitor sample failed")
            before = after

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="vbc-cpu-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
