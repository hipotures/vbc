import subprocess
import time
from pathlib import Path
from typing import Callable, Optional


def repair_via_reencode(
    input_path: Path,
    output_path: Path,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> bool:
    """
    Repairs a corrupted video file by performing a fast re-encode to MKV with fixed framerate.
    Useful for files with broken indices, variable framerate issues, or 'ffmpeg code 234' errors.
    
    Command: ffmpeg -i input -c:v libx264 -preset ultrafast -crf 20 -r 30 -c:a copy output.mkv
    
    Args:
        input_path: Path to the corrupted file.
        output_path: Path where the repaired .mkv file should be saved.
        progress_callback: Optional callback receiving the current output size in bytes.
        
    Returns:
        True if repair was successful and validated, False otherwise.
    """
    # Force .mkv extension for safety/compatibility
    if output_path.suffix.lower() != ".mkv":
        output_path = output_path.with_suffix(".mkv")

    cmd = [
        "ffmpeg", "-y",
        "-v", "error",
        "-err_detect", "ignore_err",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "20",
        "-r", "30",          # Force fixed 30 fps to fix timestamp issues
        "-c:a", "copy",      # Copy audio to preserve quality/speed
        "-ignore_unknown",
        str(output_path)
    ]

    try:
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while process.poll() is None:
            if progress_callback is not None and output_path.exists():
                progress_callback(output_path.stat().st_size)
            time.sleep(1)

        if progress_callback is not None and output_path.exists():
            progress_callback(output_path.stat().st_size)

        process.communicate()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd)
        
        # Validation
        if not output_path.exists() or output_path.stat().st_size <= 1000:
            if output_path.exists():
                output_path.unlink()
            return False
            
        # Quick ffprobe check
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-of", "json",
            str(output_path)
        ]
        res = subprocess.run(probe_cmd, capture_output=True, text=True)
        if res.returncode == 0 and '"codec_type": "video"' in res.stdout:
            return True
            
        if output_path.exists():
            output_path.unlink()
        return False

    except Exception:
        if output_path.exists():
            output_path.unlink()
        return False
