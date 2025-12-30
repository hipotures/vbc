import os
import sys
import logging
import argparse
import tempfile
import subprocess
import json
import shutil
from pathlib import Path
from datetime import datetime
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, SpinnerColumn, DownloadColumn

def setup_logging():
    log_filename = f"fix_vbc_tags_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_filepath = os.path.join(tempfile.gettempdir(), log_filename)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    fh = logging.FileHandler(log_filepath, encoding='utf-8')
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    
    logger.addHandler(fh)
    logger.addHandler(ch)
    
    return log_filepath

def check_free_space(path, min_gb=10):
    try:
        stat = shutil.disk_usage(os.path.dirname(path))
        free_gb = stat.free / (1024**3)
        return free_gb >= min_gb, free_gb
    except Exception:
        return True, 999

def get_file_dates(filepath):
    try:
        stat = os.stat(filepath)
        mtime = datetime.fromtimestamp(stat.st_mtime)
        try:
            ctime = datetime.fromtimestamp(stat.st_birthtime)
        except AttributeError:
            ctime = datetime.fromtimestamp(stat.st_ctime)
        
        latest = max(mtime, ctime)
        offset = datetime.now().astimezone().strftime('%z')
        offset_formatted = f"{offset[:3]}:{offset[3:]}"
        return latest.strftime('%Y:%m:%d %H:%M:%S') + offset_formatted
    except Exception:
        return datetime.now().strftime('%Y:%m:%d %H:%M:%S') + "+01:00"

def get_existing_tags(filepath, config_path):
    try:
        cmd = ["exiftool", "-m"]
        if config_path:
            cmd.extend(["-config", str(config_path)])
        cmd.extend(["-XMP:VBCEncoder", "-j", filepath])
        
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        if data and "VBCEncoder" in data[0]:
            return data[0]["VBCEncoder"]
    except Exception:
        pass
    return None

def main():
    parser = argparse.ArgumentParser(description='Fix/Add missing VBC tags to MP4 files (Byte-based progress).')
    parser.add_argument('root_dir', help='Directory to scan recursively')
    parser.add_argument('--no-dry-run', action='store_true', help='Actually write tags to files.')
    parser.add_argument('--min-space', type=int, default=10, help='Minimum free space in GB (default: 10)')
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    log_file = setup_logging()

    if dry_run:
        logging.warning("RUNNING IN DRY-RUN MODE - No metadata will be modified.")
    else:
        logging.warning(f"RUNNING IN EXECUTION MODE - Writing metadata tags (Min space: {args.min_space}GB).")

    root_path = os.path.abspath(args.root_dir)
    if not os.path.isdir(root_path):
        logging.error(f"Directory does not exist: {root_path}")
        sys.exit(1)

    script_dir = Path(__file__).resolve().parent
    config_path = script_dir.parent / "conf" / "exiftool.conf"
    if not config_path.exists():
        logging.error(f"ExifTool config not found at: {config_path}")
        sys.exit(1)

    # 1. Discovery with sizes
    print("Scanning directory and calculating sizes...")
    file_list = [] # List of (path, size)
    total_bytes = 0
    for root, dirs, files in os.walk(root_path):
        for filename in files:
            if filename.lower().endswith('.mp4'):
                fpath = os.path.join(root, filename)
                try:
                    fsize = os.path.getsize(fpath)
                    file_list.append((fpath, fsize))
                    total_bytes += fsize
                except Exception:
                    continue
    
    total_files = len(file_list)
    if total_files == 0:
        logging.warning("No MP4 files found.")
        sys.exit(0)

    total_gb = total_bytes / (1024**3)
    print(f"Found {total_files} files ({total_gb:.2f} GB total).")

    stats = {
        'total': total_files,
        'total_bytes': total_bytes,
        'processed': 0,
        'tagged': 0,
        'skipped_has_tags': 0,
        'skipped_empty': 0,
        'skipped_invalid': 0,
        'skipped_error': 0
    }

    interrupted = False
    with Progress(
        TextColumn("[bold blue]{task.fields[cur_file]}/{task.fields[total_files]}"),
        TextColumn("[bold magenta]({task.fields[new]} new / {task.fields[skip]} skip)"),
        BarColumn(bar_width=None),
        DownloadColumn(), # Shows processed bytes / total bytes (e.g. 1.2/10.5 GB)
        TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
        TimeRemainingColumn(),
        SpinnerColumn(),
        expand=True
    ) as progress:
        
        task = progress.add_task(
            "Processing", 
            total=total_bytes, 
            cur_file=0, 
            total_files=total_files,
            new=0, 
            skip=0
        )
        
        try:
            for filepath, old_size in sorted(file_list):
                filename = os.path.basename(filepath)
                progress.update(task, cur_file=stats['processed'] + 1)
                
                # 1. Check empty
                if old_size == 0:
                    logging.info(f"Skipping empty: {filename}")
                    stats['skipped_empty'] += 1
                    stats['processed'] += 1
                    progress.update(task, skip=stats['skipped_has_tags'] + stats['skipped_empty'] + stats['skipped_invalid'])
                    progress.advance(task, 0) # size is 0 anyway
                    continue

                # 2. Check free space
                if not dry_run:
                    has_space, free_gb = check_free_space(filepath, min_gb=args.min_space)
                    if not has_space:
                        progress.console.print(f"\n[bold red]CRITICAL ERROR: Low disk space ({free_gb:.2f} GB free).")
                        logging.error(f"ABORTED due to low disk space: {free_gb:.2f} GB free.")
                        break

                # 3. Check existing tags
                existing = get_existing_tags(filepath, config_path)
                if existing:
                    logging.info(f"Skipping (has tags): {filename}")
                    stats['skipped_has_tags'] += 1
                    stats['processed'] += 1
                    progress.update(task, skip=stats['skipped_has_tags'] + stats['skipped_empty'] + stats['skipped_invalid'])
                    progress.advance(task, old_size) # We skip writing, so we "instantly" advance
                    continue

                # 4. Tagging
                finished_at = get_file_dates(filepath)
                tags = {
                    "XMP:VBCEncoder": "NVENC AV1 (GPU)",
                    "XMP:VBCFinishedAt": finished_at,
                    "XMP:VBCOriginalName": filename,
                    "XMP:VBCOriginalSize": -1
                }

                if dry_run:
                    logging.info(f"[DRY-RUN] Tagging: {filename}")
                    stats['tagged'] += 1
                    # In dry run we also "advance" instantly because no real writing occurs
                    progress.advance(task, old_size)
                else:
                    try:
                        cmd = ["exiftool", "-config", str(config_path), "-m", "-overwrite_original"]
                        for k, v in tags.items():
                            cmd.append(f"-{k}={v}")
                        cmd.append(filepath)
                        
                        subprocess.run(cmd, capture_output=True, text=True, check=True)
                        
                        # Size safety check: (1% of old size) + 10 KB buffer
                        new_size = os.path.getsize(filepath)
                        diff_bytes = abs(new_size - old_size)
                        allowed_diff = (0.01 * old_size) + 10240 # 1% + 10KB
                        
                        if diff_bytes > allowed_diff:
                            diff_pct = (diff_bytes / old_size) if old_size > 0 else 0
                            err = f"CRITICAL: Size changed significantly by {diff_bytes} bytes ({diff_pct:.2%}). Limit: {allowed_diff:.0f} bytes."
                            progress.console.print(f"\n[bold red]{err} for {filename}")
                            logging.error(f"{err} for {filepath}")
                            sys.exit(1)

                        logging.info(f"TAGGED: {filename}")
                        stats['tagged'] += 1
                        progress.advance(task, old_size)
                    except subprocess.CalledProcessError as e:
                        error_msg = e.stderr.strip() if e.stderr else str(e)
                        if "Not a valid" in error_msg or "looks more like a FLV" in error_msg:
                            logging.warning(f"SKIPPED (Invalid Format): {filename}")
                            stats['skipped_invalid'] += 1
                            progress.update(task, skip=stats['skipped_has_tags'] + stats['skipped_empty'] + stats['skipped_invalid'])
                            progress.advance(task, old_size)
                        else:
                            progress.console.print(f"\n[bold red]ERROR tagging {filename}: {error_msg}")
                            logging.error(f"FAILED {filepath}: {error_msg}")
                            stats['skipped_error'] += 1
                            break
                    except Exception as e:
                        progress.console.print(f"\n[bold red]ERROR: {str(e)}")
                        logging.error(f"UNEXPECTED FAILED {filepath}: {str(e)}")
                        break
                
                stats['processed'] += 1
                progress.update(task, new=stats['tagged'])
                
        except KeyboardInterrupt:
            progress.console.print("\n[bold yellow]Interrupt received. Stopping...[/bold yellow]")
            interrupted = True

    report_title = "VBC TAG FIX REPORT" + (" (INTERRUPTED)" if interrupted else "")
    report = f"""
========================================
{report_title}
========================================
Log file: {log_file}
Config:   {config_path}
Total MP4 files found:      {stats['total']} ({total_gb:.2f} GB)
Files analyzed:             {stats['processed']}
Files newly tagged:         {stats['tagged']}
Files already tagged:       {stats['skipped_has_tags']}
Invalid format skipped:     {stats['skipped_invalid']}
Empty files skipped:        {stats['skipped_empty']}
Errors:                     {stats['skipped_error']}
----------------------------------------
Sum check:
{stats['processed']} == {stats['tagged'] + stats['skipped_has_tags'] + stats['skipped_invalid'] + stats['skipped_empty'] + stats['skipped_error']}
========================================
"""
    logging.info(report)
    print(report)

if __name__ == "__main__":
    main()