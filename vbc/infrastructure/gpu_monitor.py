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
    """One GPU reading (nvidia-smi or nvtop); ``raw`` is None when the read failed."""

    raw: Optional[Dict[str, Any]]
    temp_c: Optional[float] = None
    power_w: Optional[float] = None
    gpu_util_pct: Optional[float] = None
    mem_util_pct: Optional[float] = None
    fan_pct: Optional[float] = None
    # nvidia-smi only (None from nvtop)
    enc_util_pct: Optional[float] = None
    dec_util_pct: Optional[float] = None
    mem_used_mib: Optional[float] = None
    mem_total_mib: Optional[float] = None
    power_limit_w: Optional[float] = None
    gpu_clock_mhz: Optional[float] = None
    mem_clock_mhz: Optional[float] = None
    throttle: Optional[str] = None

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


    @classmethod
    def from_smi(cls, row: Dict[str, str]) -> "GpuSample":
        """One ``nvidia-smi --query-gpu`` row (field name -> value, nounits).

        ``raw`` keeps the nvtop-style strings the clients already show
        ("32C", "30%", "114W") plus enc/dec utilisation and memory use.
        """
        num = {k: parse_number(v) for k, v in row.items()}

        def fmt(value: Optional[float], unit: str, digits: int = 0) -> str:
            return "N/A" if value is None else f"{value:.{digits}f}{unit}"

        used, total = num.get("memory.used"), num.get("memory.total")
        raw = {
            "source": "nvidia-smi",
            "device_name": row.get("name", "GPU"),
            "temp": fmt(num.get("temperature.gpu"), "C"),
            "fan_speed": fmt(num.get("fan.speed"), "%"),
            "power_draw": fmt(num.get("power.draw"), "W"),
            "power_limit": fmt(num.get("power.limit"), "W"),
            "gpu_util": fmt(num.get("utilization.gpu"), "%"),
            "mem_util": fmt(num.get("utilization.memory"), "%"),
            "enc_util": fmt(num.get("utilization.encoder"), "%"),
            "dec_util": fmt(num.get("utilization.decoder"), "%"),
            "gpu_clock": fmt(num.get("clocks.gr"), "MHz"),
            "mem_clock": fmt(num.get("clocks.mem"), "MHz"),
            "mem_used": fmt(None if used is None else used / 1024, "GiB", 1),
            "mem_total": fmt(None if total is None else total / 1024, "GiB", 1),
        }
        throttle = row.get(SMI_THROTTLE_FIELD)
        if throttle is not None:
            raw["throttle"] = throttle
        return cls(
            raw=raw,
            temp_c=num.get("temperature.gpu"),
            power_w=num.get("power.draw"),
            gpu_util_pct=num.get("utilization.gpu"),
            mem_util_pct=num.get("utilization.memory"),
            fan_pct=num.get("fan.speed"),
            enc_util_pct=num.get("utilization.encoder"),
            dec_util_pct=num.get("utilization.decoder"),
            mem_used_mib=used,
            mem_total_mib=total,
            power_limit_w=num.get("power.limit"),
            gpu_clock_mhz=num.get("clocks.gr"),
            mem_clock_mhz=num.get("clocks.mem"),
            throttle=throttle,
        )


# nvidia-smi --query-gpu fields. The throttle field changed name between
# drivers; it is optional (dropped for good when the driver rejects it).
SMI_FIELDS = (
    "index", "name", "temperature.gpu", "fan.speed", "power.draw", "power.limit",
    "utilization.gpu", "utilization.memory", "utilization.encoder", "utilization.decoder",
    "memory.used", "memory.total", "clocks.gr", "clocks.mem",
)
SMI_THROTTLE_FIELD = "clocks_event_reasons.active"


def parse_smi_csv(text: str, fields: tuple) -> list:
    """``--format=csv,noheader,nounits`` output -> list of {field: value}."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        values = [v.strip() for v in line.split(",")]
        if len(values) != len(fields):
            continue
        rows.append(dict(zip(fields, values)))
    return rows


class GpuSink(Protocol):
    """Receiver of GPU samples (the server read model)."""

    def add_gpu_sample(self, sample: GpuSample) -> None: ...


class GpuMonitor:
    """Monitors GPU metrics (nvidia-smi, else nvtop -s) in a background thread."""

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

        # nvidia-smi first (encoder/decoder use, memory, clocks); nvtop when
        # nvidia-smi is missing or fails.
        self.smi_cmd = shutil.which("nvidia-smi")
        self._smi_fields = SMI_FIELDS + (SMI_THROTTLE_FIELD,)
        self._smi_failed = False

        # Determine nvtop binary path
        if nvtop_path:
            self.nvtop_cmd = nvtop_path
            self._nvtop_available = os.path.isfile(nvtop_path) and os.access(nvtop_path, os.X_OK)
        else:
            nvtop_which = shutil.which("nvtop")
            self.nvtop_cmd = nvtop_which or "nvtop"
            self._nvtop_available = nvtop_which is not None

    def _read_smi(self) -> Optional[GpuSample]:
        """One nvidia-smi reading of the chosen GPU; None when it cannot be read."""
        if self.smi_cmd is None or self._smi_failed:
            return None
        for _attempt in range(2):
            fields = self._smi_fields
            result = subprocess.run(
                [self.smi_cmd, f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0 and SMI_THROTTLE_FIELD in fields and "field" in (
                    result.stdout + result.stderr).lower():
                # Older/newer driver without this field name: query without it.
                self._smi_fields = SMI_FIELDS
                continue
            break
        if result.returncode != 0:
            self.logger.warning("GPU Monitor: nvidia-smi failed (%s); using nvtop",
                                (result.stderr or result.stdout).strip()[:200])
            self._smi_failed = True
            return None
        rows = parse_smi_csv(result.stdout, fields)
        row = None
        if self.device_name:
            row = next((r for r in rows if r.get("name") == self.device_name), None)
        if row is None:
            row = next((r for r in rows if r.get("index") == str(self.device_index)), None)
        return GpuSample.from_smi(row) if row else None

    def _poll(self):
        """Polls nvidia-smi (or nvtop) and forwards samples to the sink with compensated sleep."""
        next_tick = time.time()

        while not self._stop_event.is_set():
            try:
                sample = self._read_smi()
            except (OSError, subprocess.SubprocessError) as e:
                self.logger.debug(f"GPU Monitor: nvidia-smi read failed: {e}")
                sample = None
            if sample is not None:
                self.sink.add_gpu_sample(sample)
                next_tick += self.refresh_rate
                sleep_time = next_tick - time.time()
                if sleep_time > 0:
                    self._stop_event.wait(sleep_time)
                continue
            if not self._nvtop_available:
                self.sink.add_gpu_sample(GpuSample(raw=None))
                next_tick += self.refresh_rate
                sleep_time = next_tick - time.time()
                if sleep_time > 0:
                    self._stop_event.wait(sleep_time)
                continue
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

        if self.smi_cmd is None and not self._nvtop_available:
            self.logger.info("GPU Monitor started (neither nvidia-smi nor nvtop available, GPU metrics disabled)")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        self.logger.info("GPU Monitor started (%s)", "nvidia-smi" if self.smi_cmd else "nvtop")

    def stop(self):
        """Stops the monitoring thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
            self.logger.info("GPU Monitor stopped")
