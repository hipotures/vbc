import json
import os
import shutil
import subprocess
import threading
import time
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

# Number parsing regex
NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)")

def parse_number(s: str) -> Optional[float]:
    """Parse number from string like '52C', '30%', '112W'."""
    if not s:
        return None
    s = str(s).strip()
    if s in {"N/A", "--", "??"}:
        return None
    m = NUM_RE.search(s)
    return float(m.group(1)) if m else None

def parse_temp(s: str) -> Optional[float]:
    """'52C' → 52.0, '??' → None"""
    return parse_number(s)

def parse_percent(s: str) -> Optional[float]:
    """'30%' → 30.0"""
    return parse_number(s)

def parse_watts(s: str) -> Optional[float]:
    """'112W' → 112.0"""
    return parse_number(s)

def parse_fan_speed(s: str) -> Optional[float]:
    """'35%' → 35.0, 'CPU Fan' → None, 'N/A' → None"""
    result = parse_number(s)
    # If we got a valid number, return it
    if result is not None:
        return result
    # If it's a non-empty string without a number, still return None
    # (the UI will handle displaying "-" for None values)
    return None

@dataclass(frozen=True)
class GpuSample:
    """One nvtop reading; ``raw`` is None when the read failed."""

    raw: Optional[Dict[str, Any]]
    temp_c: Optional[float] = None
    power_w: Optional[float] = None
    gpu_util_pct: Optional[float] = None
    mem_util_pct: Optional[float] = None
    fan_pct: Optional[float] = None

    @classmethod
    def from_nvtop(cls, gpu: Dict[str, Any]) -> "GpuSample":
        return cls(
            raw=gpu,
            temp_c=parse_temp(gpu.get("temp")),
            power_w=parse_watts(gpu.get("power_draw")),
            gpu_util_pct=parse_percent(gpu.get("gpu_util")),
            mem_util_pct=parse_percent(gpu.get("mem_util")),
            fan_pct=parse_fan_speed(gpu.get("fan_speed")),
        )


class GpuSink(Protocol):
    """Receiver of GPU samples (the server read model)."""

    def add_gpu_sample(self, sample: GpuSample) -> None: ...


class GpuMonitor:
    """Monitors GPU metrics using nvtop -s in a background thread."""

    def __init__(self, sink: GpuSink, refresh_rate: int = 5,
                 device_index: int = 0, device_name: Optional[str] = None,
                 nvtop_path: Optional[str] = None):
        self.sink = sink
        self.refresh_rate = refresh_rate
        self.device_index = device_index
        self.device_name = device_name
        self.logger = logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Determine nvtop binary path
        if nvtop_path:
            self.nvtop_cmd = nvtop_path
            self._nvtop_available = os.path.isfile(nvtop_path) and os.access(nvtop_path, os.X_OK)
        else:
            nvtop_which = shutil.which("nvtop")
            self.nvtop_cmd = nvtop_which or "nvtop"
            self._nvtop_available = nvtop_which is not None

    def _poll(self):
        """Polls nvtop and forwards samples to the sink with compensated sleep."""
        next_tick = time.time()

        while not self._stop_event.is_set():
            try:
                # nvtop -s produces a JSON list of GPUs
                result = subprocess.run(
                    [self.nvtop_cmd, "-s"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                if result.stdout:
                    data = json.loads(result.stdout)
                    if isinstance(data, list) and len(data) > 0:
                        gpu = None

                        # Priority: device_name > device_index
                        if self.device_name:
                            for d in data:
                                if d.get("device_name") == self.device_name:
                                    gpu = d
                                    break

                        if gpu is None and 0 <= self.device_index < len(data):
                            gpu = data[self.device_index]

                        if gpu:
                            self.sink.add_gpu_sample(GpuSample.from_nvtop(gpu))
            except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as e:
                # Log once if nvtop disappears during runtime (edge case)
                if isinstance(e, FileNotFoundError) and not hasattr(self, '_logged_not_found'):
                    self.logger.warning("GPU Monitor: nvtop became unavailable during runtime")
                    self._logged_not_found = True
                elif not isinstance(e, FileNotFoundError):
                    self.logger.debug(f"GPU Monitor: failed to fetch data: {e}")
                # A failed read is recorded as an empty sample (gap in history)
                self.sink.add_gpu_sample(GpuSample(raw=None))

            # Compensated sleep
            next_tick += self.refresh_rate
            sleep_time = next_tick - time.time()
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)

    def start(self):
        """Starts the monitoring thread."""
        if self._thread is not None:
            return

        if not self._nvtop_available:
            self.logger.info("GPU Monitor started (nvtop not available, GPU metrics disabled)")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        self.logger.info("GPU Monitor started")

    def stop(self):
        """Stops the monitoring thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
            self.logger.info("GPU Monitor stopped")
