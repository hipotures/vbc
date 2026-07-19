import subprocess
import json
import threading
import time
from pathlib import Path
from typing import Dict, Any, Optional

class FFprobeAdapter:
    """Wrapper around ffprobe to extract stream information."""

    @staticmethod
    def _estimate_timeout(file_path: Path) -> int:
        """Estimate timeout based on file size (bytes)."""
        rate_bytes = 10 * 1024 * 1024  # 10 MiB/s baseline
        try:
            size_bytes = file_path.stat().st_size
        except OSError:
            return 30
        return max(1, (size_bytes + rate_bytes - 1) // rate_bytes)

    @staticmethod
    def _estimate_frame_scan_timeout(file_path: Path) -> int:
        """Allow enough time for ffprobe to decode and count every video frame."""
        rate_bytes = 1024 * 1024  # 1 MiB/s conservative decode baseline
        try:
            size_bytes = file_path.stat().st_size
        except OSError:
            return 30
        return max(30, (size_bytes + rate_bytes - 1) // rate_bytes)

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _parse_duration_tag(cls, value: Any) -> float:
        if value is None:
            return 0.0
        text = str(value).strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            pass
        if ":" in text:
            parts = text.split(":")
            if len(parts) in (2, 3):
                try:
                    parts_f = [float(p) for p in parts]
                except ValueError:
                    return 0.0
                if len(parts_f) == 2:
                    minutes, seconds = parts_f
                    return minutes * 60 + seconds
                hours, minutes, seconds = parts_f
                return hours * 3600 + minutes * 60 + seconds
        return 0.0

    @classmethod
    def _parse_time_base_duration(cls, duration_ts: Any, time_base: Any) -> float:
        if duration_ts is None or time_base is None:
            return 0.0
        time_base_text = str(time_base)
        if "/" not in time_base_text:
            return 0.0
        num_text, den_text = time_base_text.split("/", 1)
        num = cls._to_float(num_text)
        den = cls._to_float(den_text)
        if den == 0:
            return 0.0
        base_seconds = num / den
        ticks = cls._to_float(duration_ts)
        if ticks <= 0:
            return 0.0
        return ticks * base_seconds

    def get_stream_info(self, file_path: Path) -> Dict[str, Any]:
        """Executes ffprobe and parses JSON output."""
        cmd = [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(file_path)
        ]
        timeout_s = self._estimate_timeout(file_path)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ffprobe timed out after {timeout_s}s for {file_path}") from exc
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            detail = err if err else "unknown error (no stderr)"
            raise RuntimeError(f"ffprobe failed for {file_path}: {detail}")
            
        data = json.loads(result.stdout)
        
        # Find streams
        streams = data.get("streams", [])
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        if not video_stream:
            raise ValueError(f"No video stream found in {file_path}")
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if audio_stream is None:
            audio_codec = "no-audio"
        else:
            audio_codec = audio_stream.get("codec_name") or "unknown"
            
        # Parse FPS (prefer avg_frame_rate; r_frame_rate is often timebase)
        fps = 0.0
        fps_str = video_stream.get("avg_frame_rate", "0/0")
        if "/" in fps_str:
            try:
                num, den = map(float, fps_str.split("/"))
                if den != 0:
                    candidate = num / den
                    if candidate <= 240:
                        fps = round(candidate)
            except ValueError:
                fps = 0.0
        else:
            try:
                candidate = float(fps_str)
                if candidate <= 240:
                    fps = round(candidate)
            except ValueError:
                fps = 0.0

        # Duration fallback order: format.duration, format tags, stream.duration, stream tags, duration_ts/time_base, bitrate/size
        fmt = data.get("format", {})
        duration = self._to_float(fmt.get("duration"))
        if duration <= 0:
            tags = fmt.get("tags", {}) or {}
            duration = self._parse_duration_tag(tags.get("DURATION") or tags.get("duration"))
        if duration <= 0:
            duration = self._to_float(video_stream.get("duration"))
        if duration <= 0:
            tags = video_stream.get("tags", {}) or {}
            duration = self._parse_duration_tag(tags.get("DURATION") or tags.get("duration"))
        if duration <= 0:
            duration = self._parse_time_base_duration(video_stream.get("duration_ts"), video_stream.get("time_base"))
        if duration <= 0:
            bit_rate = self._to_float(fmt.get("bit_rate") or video_stream.get("bit_rate"))
            size = self._to_float(fmt.get("size"))
            if bit_rate > 0 and size > 0:
                duration = (size * 8) / bit_rate

        # Check for VBC tags in format or stream tags
        format_tags = fmt.get("tags", {}) or {}
        stream_tags = video_stream.get("tags", {}) or {}
        
        # Check for VBC Encoder tag (case-insensitive check for key presence)
        vbc_encoded = False
        for tags_dict in (format_tags, stream_tags):
            for k, v in tags_dict.items():
                if k.lower() in ("vbcencoder", "vbc encoder"):
                    vbc_encoded = True
                    break
            if vbc_encoded:
                break

        bitrate_bps = self._to_float(fmt.get("bit_rate") or video_stream.get("bit_rate"))

        return {
            "width": int(video_stream.get("width", 0)),
            "height": int(video_stream.get("height", 0)),
            "codec": video_stream.get("codec_name", "unknown"),
            "audio_codec": audio_codec,
            "fps": fps,
            "duration": duration,
            "bitrate_kbps": (bitrate_bps / 1000.0) if bitrate_bps > 0 else None,
            "color_space": video_stream.get("color_space"),
            "pix_fmt": video_stream.get("pix_fmt"),
            "vbc_encoded": vbc_encoded,
        }

    @staticmethod
    def _packet_count(stream: Dict[str, Any] | None) -> int:
        if not stream:
            return 0
        try:
            return max(0, int(stream.get("nb_read_packets") or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _parse_fps(stream: Dict[str, Any] | None) -> float:
        if not stream:
            return 0.0
        value = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/0")
        try:
            if "/" in value:
                numerator, denominator = value.split("/", 1)
                denominator_value = float(denominator)
                fps = float(numerator) / denominator_value if denominator_value else 0.0
            else:
                fps = float(value)
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
        return round(fps) if 0 < fps <= 240 else 0.0

    def get_part_info(self, file_path: Path) -> Dict[str, Any]:
        """Probe one manifest input, including stream packet counts."""
        cmd = [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-count_packets",
            "-show_streams",
            "-show_format",
            str(file_path),
        ]
        timeout_s = self._estimate_timeout(file_path)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ffprobe timed out after {timeout_s}s for {file_path}") from exc
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or "unknown error (no stderr)"
            raise RuntimeError(f"ffprobe failed for {file_path}: {detail}")

        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt = data.get("format", {}) or {}

        duration = self._to_float(fmt.get("duration"))
        start_time = self._to_float(fmt.get("start_time"))
        if start_time > 0 and duration > start_time:
            duration -= start_time
        if duration <= 0 and video_stream:
            duration = self._to_float(video_stream.get("duration"))
        if duration <= 0 and video_stream:
            duration = self._parse_time_base_duration(
                video_stream.get("duration_ts"),
                video_stream.get("time_base"),
            )

        bitrate_bps = self._to_float(
            fmt.get("bit_rate") or (video_stream or {}).get("bit_rate")
        )
        return {
            "has_video_stream": video_stream is not None,
            "has_audio_stream": audio_stream is not None,
            "width": int((video_stream or {}).get("width") or 0),
            "height": int((video_stream or {}).get("height") or 0),
            "codec": (video_stream or {}).get("codec_name") or "unknown",
            "audio_codec": (audio_stream or {}).get("codec_name") if audio_stream else None,
            "fps": self._parse_fps(video_stream),
            "duration": max(0.0, duration),
            "bitrate_kbps": (bitrate_bps / 1000.0) if bitrate_bps > 0 else None,
            "color_space": (video_stream or {}).get("color_space"),
            "pix_fmt": (video_stream or {}).get("pix_fmt"),
            "video_packets": self._packet_count(video_stream),
            "audio_packets": self._packet_count(audio_stream),
        }

    def get_video_frame_count(
        self,
        file_path: Path,
        shutdown_event: Optional[threading.Event] = None,
    ) -> int:
        """Decode the primary video stream and return its presentable frame count."""
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-count_frames",
            "-show_entries", "stream=nb_read_frames",
            "-of", "json",
            str(file_path),
        ]
        timeout_s = self._estimate_frame_scan_timeout(file_path)
        if shutdown_event and shutdown_event.is_set():
            raise InterruptedError(f"ffprobe frame scan interrupted for {file_path}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + timeout_s
        while True:
            if shutdown_event and shutdown_event.is_set():
                process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise InterruptedError(f"ffprobe frame scan interrupted for {file_path}")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise RuntimeError(
                    f"ffprobe frame scan timed out after {timeout_s}s for {file_path}"
                )
            try:
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        if process.returncode != 0:
            detail = (stderr or "").strip() or "unknown error (no stderr)"
            raise RuntimeError(f"ffprobe frame scan failed for {file_path}: {detail}")

        data = json.loads(stdout)
        streams = data.get("streams", [])
        if not streams:
            raise ValueError(f"No video stream found in {file_path}")
        try:
            frame_count = int(streams[0].get("nb_read_frames") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid video frame count for {file_path}") from exc
        if frame_count <= 0:
            raise ValueError(f"No decodable video frames found in {file_path}")
        return frame_count

    def get_video_packet_duration(self, file_path: Path) -> float:
        """Return normalized video timeline length for silence synthesis."""
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,dts_time,duration_time",
            "-of", "json",
            str(file_path),
        ]
        timeout_s = self._estimate_timeout(file_path)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ffprobe timed out after {timeout_s}s for {file_path}") from exc
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or "unknown error (no stderr)"
            raise RuntimeError(f"ffprobe packet scan failed for {file_path}: {detail}")

        packets = json.loads(result.stdout).get("packets", [])
        starts: list[float] = []
        ends: list[float] = []
        for packet in packets:
            start = self._to_float(packet.get("pts_time") or packet.get("dts_time"))
            packet_duration = max(0.0, self._to_float(packet.get("duration_time")))
            starts.append(start)
            ends.append(start + packet_duration)
        if not starts:
            return 0.0
        duration = max(ends) - min(starts)
        return max(0.0, duration)
