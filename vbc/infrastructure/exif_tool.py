import exiftool
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any
from vbc.domain.models import VideoFile, VideoMetadata

class ExifToolAdapter:
    """Wrapper around pyexiftool for metadata extraction and manipulation."""
    
    def __init__(self):
        self.et = exiftool.ExifTool()
        self._lock = threading.Lock()

    def _get_tag(self, data: Dict[str, Any], tags: List[str]) -> Optional[Any]:
        """Tries to find the first available tag from a list of aliases."""
        for tag in tags:
            if tag in data:
                return data[tag]
        return None

    def _extract_camera_raw(self, data: Dict[str, Any]) -> Optional[str]:
        """Extract camera model/vendor with fallback tags."""
        tag_groups = [
            [
                "EXIF:Model",
                "QuickTime:Model",
                "Model",
                "CameraModelName",
                "XMP:CameraModelName",
                "DeviceModelName",
                "QuickTime:DeviceModelName",
            ],
            ["EXIF:Make", "QuickTime:Make", "Make", "XMP:Make"],
            ["QuickTime:HandlerVendorID", "HandlerVendorID", "HandlerVendorId"],
            ["Platform"],
        ]

        for tags in tag_groups:
            value = self._get_tag(data, tags)
            if value:
                value_str = str(value).strip()
                if value_str:
                    return value_str
        return None

    def extract_metadata(self, file: VideoFile) -> VideoMetadata:
        """Extracts metadata from a video file using ExifTool."""
        if not self.et.running:
            self.et.run()

        with self._lock:
            metadata_list = self.et.execute_json(str(file.path))
        if not metadata_list:
            raise ValueError(f"Could not extract metadata for {file.path}")
            
        data = metadata_list[0]
        
        width = self._get_tag(data, ["QuickTime:ImageWidth", "Track1:ImageWidth", "ImageWidth"])
        height = self._get_tag(data, ["QuickTime:ImageHeight", "Track1:ImageHeight", "ImageHeight"])
        fps = self._get_tag(data, ["QuickTime:VideoFrameRate", "VideoFrameRate"])
        # Get video codec ID (avc1=h264, hvc1=hevc, etc), not HandlerDescription which can be "Sound"
        codec_raw = self._get_tag(data, ["QuickTime:CompressorID", "CompressorID", "VideoCodec", "CompressorName"])

        # Map codec IDs to user-friendly names
        codec_map = {
            "avc1": "h264",
            "hvc1": "hevc",
            "hev1": "hevc",
            "av01": "av1",
            "vp09": "vp9",
            "vp08": "vp8"
        }
        codec = codec_map.get(str(codec_raw).lower(), str(codec_raw)) if codec_raw else "unknown"

        camera = self._extract_camera_raw(data)
        bitrate = self._get_tag(data, ["QuickTime:AvgBitrate", "AvgBitrate"])

        return VideoMetadata(
            width=int(width) if width else 0,
            height=int(height) if height else 0,
            codec=codec,
            fps=float(fps) if fps else 0.0,
            camera_model=str(camera) if camera else None,
            bitrate_kbps=float(bitrate) / 1000 if bitrate else None
        )

    def extract_exif_info(self, file: VideoFile, dynamic_cq: Dict[str, int]) -> Dict[str, Optional[object]]:
        """Extracts camera info and dynamic CQ using full ExifTool tags."""
        if not self.et.running:
            self.et.run()

        with self._lock:
            metadata_list = self.et.execute_json(str(file.path))
        if not metadata_list:
            raise ValueError(f"Could not extract metadata for {file.path}")

        tags = metadata_list[0]
        full_metadata_text = str(tags)

        camera_raw = self._extract_camera_raw(tags)

        camera_model = None
        custom_cq = None
        matched_pattern = None
        for pattern, cq_value in dynamic_cq.items():
            if pattern in full_metadata_text:
                camera_model = pattern
                custom_cq = cq_value
                matched_pattern = pattern
                break

        if not camera_model and camera_raw:
            camera_model = camera_raw

        bitrate = tags.get('QuickTime:AvgBitrate') or tags.get('AvgBitrate')
        bitrate_kbps = float(bitrate) / 1000 if bitrate else None

        return {
            "camera_model": camera_model,
            "camera_raw": camera_raw,
            "custom_cq": custom_cq,
            "bitrate_kbps": bitrate_kbps,
            "matched_pattern": matched_pattern,
        }

    def copy_metadata(self, source: Path, target: Path):
        """Copies EXIF/XMP tags from source to target."""
        if not self.et.running:
            self.et.run()
            
        # Standard command for deep EXIF/XMP copy
        cmd = [
            "-tagsFromFile", str(source),
            "-all:all",
            "-unsafe",
            "-overwrite_original",
            str(target)
        ]
        self.et.execute(*cmd)
