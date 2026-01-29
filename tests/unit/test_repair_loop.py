from pathlib import Path

from vbc.pipeline import repair as repair_mod


def test_repair_skips_when_mkv_exists(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    errors_dir = tmp_path / "input_err"
    input_dir.mkdir()
    errors_dir.mkdir()

    candidate = errors_dir / "video.flv"
    candidate.write_bytes(b"x" * 10)
    err_file = errors_dir / "video.err"
    err_file.write_text("failed")

    dest_mkv = input_dir / "video.mkv"
    dest_mkv.write_bytes(b"m" * 5)

    calls = {"flv": 0, "reencode": 0}

    def fake_flv(*_args, **_kwargs):
        calls["flv"] += 1
        return True

    def fake_reencode(*_args, **_kwargs):
        calls["reencode"] += 1
        return True

    monkeypatch.setattr(repair_mod, "repair_flv_file", fake_flv)
    monkeypatch.setattr(repair_mod, "repair_via_reencode", fake_reencode)

    repaired = repair_mod.process_repairs(
        input_dirs=[input_dir],
        errors_dir_map={input_dir: errors_dir},
        extensions=[".flv"],
        logger=None,
    )

    assert repaired == 0
    assert calls["flv"] == 0
    assert calls["reencode"] == 0
    assert not candidate.with_suffix(".flv.repaired").exists()


def test_repair_outputs_mkv_and_cleans_temp_flv(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    errors_dir = tmp_path / "input_err"
    input_dir.mkdir()
    errors_dir.mkdir()

    candidate = errors_dir / "video.flv"
    candidate.write_bytes(b"x" * 10)
    err_file = errors_dir / "video.err"
    err_file.write_text("failed")

    temp_flv = candidate.with_suffix(".repaired_temp.flv")
    temp_mkv = candidate.with_suffix(".repaired_temp.mkv")

    def fake_flv(_src: Path, out: Path):
        out.write_bytes(b"f" * 5)
        return True

    def fake_reencode(_src: Path, out: Path):
        out.write_bytes(b"m" * 7)
        return True

    monkeypatch.setattr(repair_mod, "repair_flv_file", fake_flv)
    monkeypatch.setattr(repair_mod, "repair_via_reencode", fake_reencode)

    repaired = repair_mod.process_repairs(
        input_dirs=[input_dir],
        errors_dir_map={input_dir: errors_dir},
        extensions=[".flv"],
        logger=None,
    )

    dest_mkv = input_dir / "video.mkv"
    assert repaired == 1
    assert dest_mkv.exists()
    assert dest_mkv.read_bytes() == b"m" * 7
    assert not temp_flv.exists()
    assert not temp_mkv.exists()
    assert candidate.with_suffix(".flv.repaired").exists()
