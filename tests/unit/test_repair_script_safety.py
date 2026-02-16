from pathlib import Path


def test_repair_script_avoids_shell_execution():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "repair_corrupted_flv.py"
    source = script_path.read_text(encoding="utf-8")

    assert "shell=True" not in source
    assert "find_flv_header_offset" in source
    assert "copy_from_offset" in source
