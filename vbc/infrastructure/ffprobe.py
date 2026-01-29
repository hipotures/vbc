import subprocess
import json
from pathlib import Path
from typing import Dict, Any

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

        return {
            "width": int(video_stream.get("width", 0)),
            "height": int(video_stream.get("height", 0)),
            "codec": video_stream.get("codec_name", "unknown"),
            "audio_codec": audio_codec,
            "fps": fps,
            "duration": duration,
            "color_space": video_stream.get("color_space"),
            "pix_fmt": video_stream.get("pix_fmt"),
            "vbc_encoded": vbc_encoded,
        }
