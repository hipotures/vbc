import exiftool
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any
from vbc.domain.models import VideoFile, VideoMetadata
from vbc.config.models import DynamicQualityRule

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
                "H264:Model",
                "M2TS:Model",
            ],
            ["EXIF:Make", "QuickTime:Make", "Make", "XMP:Make", "H264:Make", "M2TS:Make"],
            ["QuickTime:HandlerVendorID", "HandlerVendorID", "HandlerVendorId"],
            ["Platform"],
        ]

        for tags in tag_groups:
            value = self._get_tag(data, tags)
            if value:
                value_str = str(value).strip()
                if value_str:
                    # Map common MTS manufacturer IDs to names
                    mts_map = {
                        "259": "Panasonic",
                        "258": "Sony",
                        "257": "Canon",
                        "260": "JVC",
                    }
                    return mts_map.get(value_str, value_str)
        return None

    def extract_tags(self, file_path: Path) -> Dict[str, Any]:
        """Extract raw ExifTool tags as a dictionary for verification checks."""
        if not self.et.running:
            self.et.run()

        with self._lock:
            metadata_list = self.et.execute_json(str(file_path))
        if not metadata_list:
            raise ValueError(f"Could not extract metadata for {file_path}")
        return metadata_list[0]

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

    def extract_exif_info(
        self,
        file: VideoFile,
        dynamic_quality: Dict[str, DynamicQualityRule],
    ) -> Dict[str, Optional[object]]:
        """Extracts camera info and dynamic quality using full ExifTool tags."""
        if not self.et.running:
            self.et.run()

        with self._lock:
            metadata_list = self.et.execute_json(str(file.path))
        if not metadata_list:
            raise ValueError(f"Could not extract metadata for {file.path}")

        tags = metadata_list[0]
        
        # Build a searchable text from tag values only
        # This keeps searching in the 'whole exif' but avoids matching dict keys
        full_metadata_text = " ".join(str(v) for v in tags.values())

        camera_raw = self._extract_camera_raw(tags)

        camera_model = None
        custom_cq = None
        matched_pattern = None

        def _rule_cq(rule: Any) -> Optional[int]:
            if isinstance(rule, DynamicQualityRule):
                return rule.cq
            if isinstance(rule, dict):
                cq_value = rule.get("cq")
                if isinstance(cq_value, int):
                    return cq_value
            return None
        
        # 1. Prioritize matching against the extracted camera model/make
        if camera_raw:
            for pattern, rule in dynamic_quality.items():
                cq_value = _rule_cq(rule)
                if pattern in camera_raw:
                    camera_model = camera_raw
                    custom_cq = cq_value
                    matched_pattern = pattern
                    break
        
        # 2. Fallback: Search in all exif values
        if custom_cq is None:
            for pattern, rule in dynamic_quality.items():
                cq_value = _rule_cq(rule)
                if pattern in full_metadata_text:
                    camera_model = pattern
                    custom_cq = cq_value
                    matched_pattern = pattern
                    break

        if not camera_model and camera_raw:
            camera_model = camera_raw

        # Check for VBC Encoder tag
        vbc_encoded = False
        # ExifTool often returns keys like "XMP:VBCEncoder" or just "VBCEncoder"
        # We check keys in the dict
        for key in tags.keys():
            k_lower = key.lower()
            if "vbcencoder" in k_lower or "vbc encoder" in k_lower:
                vbc_encoded = True
                break

        bitrate = tags.get('QuickTime:AvgBitrate') or tags.get('AvgBitrate')
        bitrate_kbps = float(bitrate) / 1000 if bitrate else None

        return {
            "camera_model": camera_model,
            "camera_raw": camera_raw,
            "custom_cq": custom_cq,
            "bitrate_kbps": bitrate_kbps,
            "matched_pattern": matched_pattern,
            "vbc_encoded": vbc_encoded,
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
