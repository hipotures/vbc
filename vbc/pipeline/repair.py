import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn
from rich.console import Console
from vbc.utils.flv_repair import repair_flv_file
from vbc.utils.reencode_repair import repair_via_reencode

def process_repairs(
    input_dirs: List[Path],
    errors_dir_map: Dict[Path, Path],
    extensions: List[str],
    logger: Optional[logging.Logger] = None,
    target_files: Optional[List[Path]] = None,
) -> int:
    """
    Scans error directories for corrupted files and attempts to repair them.
    Strategy 1: Text prefix removal (for FLV/MP4 stream dumps).
    Strategy 2: Fast re-encode (for corrupted MP4/MOV containers).
    
    Args:
        input_dirs: List of source input directories.
        errors_dir_map: Mapping from input_dir to errors_dir.
        extensions: List of video extensions to scan for.
        logger: Logger instance.
        target_files: Optional list of specific files to repair.
        
    Returns:
        Number of successfully repaired files.
    """
    console = Console()
    total_repaired = 0
    candidates_to_repair = []

    # 1. Scan for candidates first
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console
    ) as scan_progress:
        scan_task = scan_progress.add_task("Scanning for repairable files...", total=None)
        
        for input_dir in input_dirs:
            errors_dir = errors_dir_map.get(input_dir)
            if not errors_dir or not errors_dir.exists():
                continue
                
            # If target_files is provided, use it. Otherwise, scan all extensions.
            files_to_check = []
            if target_files is not None:
                for t in target_files:
                    try:
                        if errors_dir in t.parents or t.parent == errors_dir:
                            files_to_check.append(t)
                    except Exception:
                        pass
            else:
                for ext in extensions:
                    if not ext.startswith("."):
                        ext = f".{ext}"
                    files_to_check.extend(errors_dir.rglob(f"*{ext}"))
            
            for candidate in files_to_check:
                if not candidate.exists():
                    continue

                # Check if already repaired
                repaired_marker = candidate.with_suffix(candidate.suffix + ".repaired")
                if repaired_marker.exists():
                    continue
                
                # Check error file content to decide strategy
                err_file = candidate.with_suffix(".err")
                error_code = ""
                if err_file.exists():
                    try:
                        err_content = err_file.read_text()
                        if "code 234" in err_content or "Invalid argument" in err_content:
                            error_code = "234"
                    except Exception:
                        pass

                try:
                    rel_path = candidate.relative_to(errors_dir)
                except ValueError:
                    rel_path = Path(candidate.name)
                
                dest_path = input_dir / rel_path
                candidates_to_repair.append((candidate, dest_path, repaired_marker, error_code))
    
    # Deduplicate
    seen_candidates = set()
    unique_candidates = []
    for c, dp, rm, ec in candidates_to_repair:
        if c not in seen_candidates:
            unique_candidates.append((c, dp, rm, ec))
            seen_candidates.add(c)
    candidates_to_repair = unique_candidates

    if not candidates_to_repair:
        return 0

    if logger:
        logger.info(f"Found {len(candidates_to_repair)} files eligible for repair.")
    
    if target_files is not None:
        console.print(f"[bold cyan]Attempting to repair {len(candidates_to_repair)} failed files from this session.[/bold cyan]")
    else:
        console.print(f"[bold cyan]Found {len(candidates_to_repair)} failed files in error directories.[/bold cyan]")

    # 2. Process repairs
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console
    ) as progress:
        task = progress.add_task("Repairing corrupted files", total=len(candidates_to_repair))
        
        for candidate, dest_path, repaired_marker, error_code in candidates_to_repair:
            progress.update(task, description=f"Repairing [yellow]{candidate.name}[/yellow]")
            
            success = False
            repaired_file_path = None
            
            # STRATEGY 1: FLV Prefix Cut (Fast)
            # Try this if no specific error code OR if it looks like it might be an FLV dump
            if not error_code:
                temp_flv = candidate.with_suffix(".repaired_temp.flv")
                try:
                    if repair_flv_file(candidate, temp_flv):
                        success = True
                        repaired_file_path = temp_flv
                        dest_path = dest_path.with_suffix(".flv") # Update dest extension
                except Exception:
                    pass

            # STRATEGY 2: Re-encode (Slow, Fallback)
            # Use if Strategy 1 failed OR if we have specific corruption error code (234)
            if not success:
                temp_mkv = candidate.with_suffix(".repaired_temp.mkv")
                try:
                    # Inform user this might take longer
                    if logger: logger.info(f"Attempting re-encode repair for {candidate.name}")
                    if repair_via_reencode(candidate, temp_mkv):
                        success = True
                        repaired_file_path = temp_mkv
                        dest_path = dest_path.with_suffix(".mkv") # Update dest extension
                except Exception:
                    pass
            
            if success and repaired_file_path:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    import shutil
                    shutil.move(str(repaired_file_path), str(dest_path))
                    repaired_marker.touch()
                    if logger:
                        logger.info(f"Repaired and restored: {candidate.name} -> {dest_path}")
                    total_repaired += 1
                except Exception as e:
                    if logger:
                        logger.error(f"Failed to move repaired file: {e}")
                    if repaired_file_path.exists(): repaired_file_path.unlink()
            
            progress.advance(task)

    if total_repaired > 0:
        summary_msg = f"Repaired {total_repaired}/{len(candidates_to_repair)} files."
        console.print(f"[bold green]✔ {summary_msg}[/bold green]")
        console.print(f"\n[bold white]Please re-run VBC to compress the repaired files restored to source folders.[/bold white]")
        if logger:
            logger.info(summary_msg)
    elif target_files is not None:
        # If we targeted specific files but repaired none, user needs to know
        summary_msg = f"Repaired 0/{len(candidates_to_repair)} files."
        console.print(f"[yellow]⚠ {summary_msg} (Files unreadable or missing video stream)[/yellow]")
        if logger:
            logger.info(summary_msg)

    return total_repaired