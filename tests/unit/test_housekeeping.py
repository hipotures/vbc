import pytest
from pathlib import Path
from unittest.mock import patch
from vbc.infrastructure.housekeeping import HousekeepingService

def test_housekeeping_cleanup_tmp(tmp_path):
    (tmp_path / "file1.tmp").write_text("data")
    (tmp_path / "file2.mp4").write_text("data")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "file3.tmp").write_text("data")
    
    service = HousekeepingService()
    service.cleanup_temp_files(tmp_path)
    
    assert not (tmp_path / "file1.tmp").exists()
    assert (tmp_path / "file2.mp4").exists()
    assert not (tmp_path / "subdir" / "file3.tmp").exists()

def test_housekeeping_cleanup_err(tmp_path):
    (tmp_path / "file1.err").write_text("error")
    
    service = HousekeepingService()
    service.cleanup_error_markers(tmp_path)
    
    assert not (tmp_path / "file1.err").exists()

def test_housekeeping_handles_oserror(tmp_path):
    f = tmp_path / "protected.tmp"
    f.write_text("data")
    
    service = HousekeepingService()
    with patch.object(Path, 'unlink', side_effect=OSError("Permission denied")):
        # Should not raise exception
        service.cleanup_temp_files(tmp_path)
        assert f.exists()
