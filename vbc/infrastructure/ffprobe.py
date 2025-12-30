import subprocess
import json
from pathlib import Path
from typing import Dict, Any, Optional

class FFprobeAdapter:
    """Wrapper around ffprobe to extract stream information."""

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
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(file_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed for {file_path}: {result.stderr}")
            
        data = json.loads(result.stdout)
        
        # Find video stream
        video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        if not video_stream:
            raise ValueError(f"No video stream found in {file_path}")
            
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

        return {
            "width": int(video_stream.get("width", 0)),
            "height": int(video_stream.get("height", 0)),
            "codec": video_stream.get("codec_name", "unknown"),
            "fps": fps,
            "duration": duration,
            "color_space": video_stream.get("color_space"),
        }
