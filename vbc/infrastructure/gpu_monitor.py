import json
import shutil
import subprocess
import threading
import time
import logging
import re
from typing import Optional
from vbc.ui.state import UIState

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

class GpuMonitor:
    """Monitors GPU metrics using nvtop -s in a background thread."""
    
    def __init__(self, state: UIState, refresh_rate: int = 5,
                 device_index: int = 0, device_name: Optional[str] = None):
        self.state = state
        self.refresh_rate = refresh_rate
        self.device_index = device_index
        self.device_name = device_name
        self.logger = logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._nvtop_available = shutil.which("nvtop") is not None

    def _poll(self):
        """Polls nvtop and updates state with compensated sleep."""
        next_tick = time.time()

        while not self._stop_event.is_set():
            try:
                # nvtop -s produces a JSON list of GPUs
                result = subprocess.run(
                    ["nvtop", "-s"],
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
                            with self.state._lock:
                                self.state.gpu_data = gpu

                                # Append history
                                self.state.gpu_history_temp.append(parse_temp(gpu.get("temp")))
                                self.state.gpu_history_pwr.append(parse_watts(gpu.get("power_draw")))
                                self.state.gpu_history_gpu.append(parse_percent(gpu.get("gpu_util")))
                                self.state.gpu_history_mem.append(parse_percent(gpu.get("mem_util")))
                                self.state.gpu_history_fan.append(parse_percent(gpu.get("fan_speed")))
            except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as e:
                # Log once if nvtop disappears during runtime (edge case)
                if isinstance(e, FileNotFoundError) and not hasattr(self, '_logged_not_found'):
                    self.logger.warning("GPU Monitor: nvtop became unavailable during runtime")
                    self._logged_not_found = True
                elif not isinstance(e, FileNotFoundError):
                    self.logger.debug(f"GPU Monitor: failed to fetch data: {e}")
                # Append None to history on error
                with self.state._lock:
                    self.state.gpu_data = None
                    self.state.gpu_history_temp.append(None)
                    self.state.gpu_history_pwr.append(None)
                    self.state.gpu_history_gpu.append(None)
                    self.state.gpu_history_mem.append(None)
                    self.state.gpu_history_fan.append(None)

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
