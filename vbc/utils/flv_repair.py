import subprocess
import os
from pathlib import Path
from typing import Optional

def repair_flv_file(input_path: Path, output_path: Path, keep_intermediate=False) -> bool:
    """
    Repairs a FLV/MP4 file that has a text error prefix (e.g. 'upstream request timeout').
    
    Args:
        input_path: Path to the corrupted file.
        output_path: Path where the repaired file should be saved.
        keep_intermediate: If True, intermediate .flv and .mkv files are not deleted.
        
    Returns:
        True if repair was successful (output_path exists), False otherwise.
    """
    temp_dir = output_path.parent
    
    # 1. Find FLV offset
    try:
        # Look for the first occurrence of "FLV" (magic bytes for FLV header)
        # Using grep -abo to find the byte offset
        result = subprocess.run(
            ["grep", "-abo", "FLV", str(input_path)],
            capture_output=True, text=True
        )
        if not result.stdout:
            return False
        
        # Get the first offset (e.g., "24:FLV")
        first_line = result.stdout.splitlines()[0]
        offset = int(first_line.split(":")[0])
    except Exception:
        return False

    # 2. Extract clean FLV using tail (fast)
    clean_flv = temp_dir / f"{input_path.stem}.clean.flv"
    # tail -c +N starts from N-th byte (1-indexed). So offset 24 means start from 25.
    tail_cmd = ["tail", "-c", f"+{offset + 1}", str(input_path)]
    
    with open(clean_flv, "wb") as f_out:
        try:
            subprocess.run(tail_cmd, stdout=f_out, check=True)
        except subprocess.CalledProcessError:
            return False

    # 3. Remux to MKV (Safe harbor)
    recovered_mkv = temp_dir / f"{input_path.stem}.recovered.mkv"
    mkv_cmd = [
        "ffmpeg", "-y", "-v", "warning",
        "-err_detect", "ignore_err",
        "-i", str(clean_flv),
        "-c", "copy",
        str(recovered_mkv)
    ]
    
    mkv_success = False
    try:
        subprocess.run(mkv_cmd, capture_output=True, check=True)
        mkv_success = True
    except subprocess.CalledProcessError:
        pass

    if not mkv_success:
        if not keep_intermediate and clean_flv.exists():
            clean_flv.unlink()
        return False

    # 4. Remux to MP4 (Final) or whatever the output path extension is
    # We use output_path directly
    mp4_cmd = [
        "ffmpeg", "-y", "-v", "warning",
        "-i", str(recovered_mkv),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_path)
    ]
    
    success = False
    try:
        subprocess.run(mp4_cmd, capture_output=True, check=True)
        success = True
    except subprocess.CalledProcessError:
        # If MP4 fails but MKV worked, we could consider returning MKV? 
        # But the function signature expects output_path.
        pass

    # Cleanup
    if not keep_intermediate:
        if clean_flv.exists():
            clean_flv.unlink()
        if recovered_mkv.exists():
            recovered_mkv.unlink()

    return success
