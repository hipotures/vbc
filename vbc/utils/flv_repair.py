import subprocess
import os
from pathlib import Path
from typing import Optional

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
        # Look for the first occurrence of "FLV" (magic bytes for FLV header)
        result = subprocess.run(
            ["grep", "-abo", "FLV", str(input_path)],
            capture_output=True, text=True
        )
        if not result.stdout:
            return False
        
        # Get the first offset (e.g., "528:FLV")
        first_line = result.stdout.splitlines()[0]
        offset = int(first_line.split(":")[0])
    except Exception:
        return False

    # 2. Extract clean FLV using tail (fast and robust)
    # tail -c +N starts from N-th byte (1-indexed). So offset 528 means start from 529.
    tail_cmd = ["tail", "-c", f"+{offset + 1}", str(input_path)]
    
    try:
        with open(output_path, "wb") as f_out:
            subprocess.run(tail_cmd, stdout=f_out, check=True)
        
        # Sanity check: is the output file significantly larger than 0?
        if not output_path.exists() or output_path.stat().st_size <= 1000:
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