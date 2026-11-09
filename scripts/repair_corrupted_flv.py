#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path
import argparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vbc.utils.flv_repair_core import copy_from_offset, find_flv_header_offset


def run_cmd(cmd, check=True):
    print(f"Executing: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=True, text=True)

def repair_file(input_path: Path, output_dir: Path, keep_intermediate=False):
    print(f"\n[+] Processing: {input_path.name}")
    
    # 1. Find FLV offset
    try:
        offset = find_flv_header_offset(input_path)
        if offset is None:
            print(f"[-] Error: No FLV header found in {input_path}")
            return False

        print(f"[*] Found FLV header at offset: {offset}")
    except Exception as e:
        print(f"[-] Search failed: {e}")
        return False

    # 2. Extract clean FLV by copying bytes from FLV marker offset
    clean_flv = output_dir / f"{input_path.stem}.clean.flv"
    print(f"[*] Extracting clean FLV (skipping {offset} bytes)...")
    try:
        written = copy_from_offset(input_path, clean_flv, offset)
        if written <= 0:
            print("[-] Extraction failed: no bytes written")
            return False
    except Exception as e:
        print(f"[-] Extraction failed: {e}")
        return False

    # 3. Verify with ffprobe
    try:
        run_cmd(["ffprobe", "-hide_banner", "-v", "error", "-show_format", "-show_streams", str(clean_flv)])
        print("[+] Verification successful: FLV is readable.")
    except subprocess.CalledProcessError:
        print("[-] Warning: ffprobe failed on extracted FLV. The file might be truncated. Trying to remux anyway...")

    # 4. Remux to MKV (Safe harbor - handles missing metadata/indices better)
    recovered_mkv = output_dir / f"{input_path.stem}.recovered.mkv"
    print("[*] Remuxing to MKV...")
    mkv_cmd = [
        "ffmpeg", "-y", "-v", "warning",
        "-err_detect", "ignore_err",
        "-i", str(clean_flv),
        "-c", "copy",
        str(recovered_mkv)
    ]
    try:
        run_cmd(mkv_cmd)
    except subprocess.CalledProcessError as e:
        print(f"[-] MKV Remux failed: {e.stderr}")
        return False

    # 5. Remux to MP4 (Final - for compatibility)
    recovered_mp4 = output_dir / f"{input_path.stem}.recovered.mp4"
    print("[*] Finalizing to MP4...")
    mp4_cmd = [
        "ffmpeg", "-y", "-v", "warning",
        "-i", str(recovered_mkv),
        "-c", "copy",
        "-movflags", "+faststart",
        str(recovered_mp4)
    ]
    try:
        run_cmd(mp4_cmd)
        print(f"[SUCCESS] Repaired file saved to: {recovered_mp4}")
    except subprocess.CalledProcessError as e:
        print(f"[-] MP4 Finalization failed: {e.stderr}")
        print(f"[!] MKV was created, but MP4 failed. Keeping MKV: {recovered_mkv}")
        return True # Still a partial success

    # Cleanup
    if not keep_intermediate:
        if clean_flv.exists(): clean_flv.unlink()
        if recovered_mkv.exists(): recovered_mkv.unlink()
        print("[*] Cleaned up intermediate files.")

    return True

def main():
    parser = argparse.ArgumentParser(description="Repair corrupted FLV/MP4 files with a text error prefix (e.g. 'upstream request timeout').")
    parser.add_argument("input", help="Input file path or directory containing corrupted files")
    parser.add_argument("--out", "-o", help="Output directory for repaired files", default="repaired_videos")
    parser.add_argument("--keep", action="store_true", help="Keep intermediate .flv and .mkv files (useful for debugging)")
    
    args = parser.parse_args()
    
    input_path = Path(args.input)
    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)

    if input_path.is_file():
        repair_file(input_path, output_dir, args.keep)
    elif input_path.is_dir():
        # Process common extensions for these corrupted files
        files_to_process = list(input_path.glob("*.mp4")) + list(input_path.glob("*.flv"))
        if not files_to_process:
            print(f"[-] No .mp4 or .flv files found in {input_path}")
            return

        print(f"[*] Found {len(files_to_process)} files to process.")
        success_count = 0
        for f in files_to_process:
            if repair_file(f, output_dir, args.keep):
                success_count += 1
        
        print(f"\n[DONE] Successfully repaired {success_count}/{len(files_to_process)} files.")
    else:
        print(f"[-] Invalid input path: {args.input}")
        sys.exit(1)

if __name__ == "__main__":
    main()
