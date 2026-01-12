import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn
from rich.console import Console
from vbc.utils.flv_repair import repair_flv_file

def process_repairs(
    input_dirs: List[Path],
    errors_dir_map: Dict[Path, Path],
    extensions: List[str],
    logger: Optional[logging.Logger] = None,
) -> int:
    """
    Scans error directories for corrupted FLV/MP4 files (with text prefix),
    repairs them, and moves the repaired version back to the source input directory.
    
    Args:
        input_dirs: List of source input directories.
        errors_dir_map: Mapping from input_dir to errors_dir.
        extensions: List of video extensions to scan for.
        logger: Logger instance.
        
    Returns:
        Number of successfully repaired files.
    """
    console = Console()
    total_repaired = 0
    candidates_to_repair = []

    # 1. Scan for candidates first (using a spinner)
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
                
            # Find all video files in errors_dir
            for ext in extensions:
                if not ext.startswith("."):
                    ext = f".{ext}"
                for candidate in errors_dir.rglob(f"*{ext}"):
                    # Check if already repaired
                    repaired_marker = candidate.with_suffix(candidate.suffix + ".repaired")
                    if repaired_marker.exists():
                        continue
                    
                    # Store candidate info
                    try:
                        rel_path = candidate.relative_to(errors_dir)
                    except ValueError:
                        rel_path = Path(candidate.name)
                    
                    dest_path = input_dir / rel_path
                    candidates_to_repair.append((candidate, dest_path, repaired_marker))
    
    if not candidates_to_repair:
        return 0

    if logger:
        logger.info(f"Found {len(candidates_to_repair)} files eligible for repair.")
    
    console.print(f"[bold cyan]Found {len(candidates_to_repair)} failed files eligible for repair attempt.[/bold cyan]")

    # 2. Process repairs with progress bar
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console
    ) as progress:
        task = progress.add_task("Repairing corrupted files", total=len(candidates_to_repair))
        
        for candidate, dest_path, repaired_marker in candidates_to_repair:
            progress.update(task, description=f"Repairing [yellow]{candidate.name}[/yellow]")
            
            if logger:
                logger.debug(f"Attempting repair: {candidate}")

            # Try to repair directly to destination
            # We create a temporary output path first to ensure atomicity
            temp_output = candidate.with_suffix(".repaired_temp.mp4")
            
            success = repair_flv_file(candidate, temp_output)
            
            if success:
                # Move temp output to final destination
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                
                try:
                    shutil.move(str(temp_output), str(dest_path))
                    
                    # Create marker in errors_dir
                    repaired_marker.touch()
                    
                    if logger:
                        logger.info(f"Repaired: {candidate.name} -> {dest_path}")
                    total_repaired += 1
                except Exception as e:
                    if logger:
                        logger.error(f"Failed to move repaired file {temp_output} to {dest_path}: {e}")
                    if temp_output.exists():
                        temp_output.unlink()
            else:
                # Not a repairable file or repair failed
                if temp_output.exists():
                    temp_output.unlink()
            
            progress.advance(task)

    summary_msg = f"Repaired {total_repaired}/{len(candidates_to_repair)} files."
    if total_repaired > 0:
        console.print(f"[bold green]✔ {summary_msg}[/bold green]")
        console.print(f"\n[bold white]Please re-run VBC to compress the repaired files restored to source folders.[/bold white]")
        if logger:
            logger.info(summary_msg)
    else:
        console.print(f"[yellow]⚠ {summary_msg} (No repairable content found)[/yellow]")
        if logger:
            logger.info(summary_msg)

    return total_repaired