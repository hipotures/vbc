import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from vbc.infrastructure.file_scanner import FileScanner

def test_file_scanner_basic(tmp_path):
    # Setup dummy directory structure
    (tmp_path / "video1.mp4").write_text("dummy")
    (tmp_path / "video2.mov").write_text("dummy content")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "video3.avi").write_text("dummy avi")
    (tmp_path / "ignored.txt").write_text("ignore me")
    
    scanner = FileScanner(extensions=[".mp4", ".mov", ".avi"])
    files = list(scanner.scan(tmp_path))
    
    paths = {f.path.name for f in files}
    assert "video1.mp4" in paths
    assert "video2.mov" in paths
    assert "video3.avi" in paths
    assert "ignored.txt" not in paths
    assert len(files) == 3

def test_file_scanner_min_size(tmp_path):
    f1 = tmp_path / "small.mp4"
    f1.write_text("a") # 1 byte
    
    f2 = tmp_path / "large.mp4"
    f2.write_text("" * 100 + "large content") # more than 10 bytes
    
    scanner = FileScanner(extensions=[".mp4"], min_size_bytes=10)
    files = list(scanner.scan(tmp_path))
    
    paths = {f.path.name for f in files}
    assert "large.mp4" in paths
    assert "small.mp4" not in paths

def test_file_scanner_ignore_out_dir(tmp_path):
    (tmp_path / "video.mp4").write_text("data")
    out_dir = tmp_path / "videos_out"
    out_dir.mkdir()
    (out_dir / "compressed.mp4").write_text("data")
    
    scanner = FileScanner(extensions=[".mp4"])
    files = list(scanner.scan(tmp_path))
    
    paths = {f.path.name for f in files}
    assert "video.mp4" in paths
    assert "compressed.mp4" not in paths
