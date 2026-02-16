import subprocess
from pathlib import Path

from vbc.utils.flv_repair_core import copy_from_offset, find_flv_header_offset


def repair_flv_file(input_path: Path, output_path: Path, keep_intermediate=False) -> bool:
    """
    Repairs a FLV/MP4 file by removing the text error prefix and saving as a clean .flv.
    
    Args:
        input_path: Path to the corrupted file.
        output_path: Path where the repaired .flv file should be saved.
        keep_intermediate: Ignored in this version as we produce only one file.
        
    Returns:
        True if repair was successful, False otherwise.
    """
    # Ensure output has .flv extension if we're just cutting
    if output_path.suffix.lower() != ".flv":
        output_path = output_path.with_suffix(".flv")

    # 1. Find FLV offset
    try:
        offset = find_flv_header_offset(input_path)
        if offset is None:
            return False
    except Exception:
        return False

    # 2. Extract clean FLV by copying bytes from marker offset
    try:
        written = copy_from_offset(input_path, output_path, offset)
        
        # Sanity check: is the output file significantly larger than 0?
        if written <= 1000 or not output_path.exists():
            if output_path.exists():
                output_path.unlink()
            return False

        # Verify with ffprobe to ensure the file is actually readable and has video
        # This prevents "repair loops" where we restore a file that VBC will reject again.
        probe_cmd = [
            "ffprobe", 
            "-v", "error", 
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-of", "json",
            str(output_path)
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
        
        if probe_result.returncode != 0:
            if output_path.exists():
                output_path.unlink()
            return False
            
        import json
        probe_data = json.loads(probe_result.stdout)
        if not probe_data.get("streams"):
            # No video stream found - VBC will reject this file anyway
            if output_path.exists():
                output_path.unlink()
            return False

        return True
    except Exception:
        if output_path.exists():
            output_path.unlink()
        return False
