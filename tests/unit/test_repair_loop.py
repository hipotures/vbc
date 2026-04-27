from pathlib import Path

from vbc.pipeline import repair as repair_mod


class _CaptureProgress:
    processing_instances = []

    def __init__(self, *args, **kwargs):
        self.tasks = {}
        self.updates = []
        self.is_processing = any(
            getattr(column, "__class__", type(column)).__name__ == "BarColumn"
            for column in args
        )
        if self.is_processing:
            self.processing_instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def add_task(self, description, total=None):
        task_id = len(self.tasks)
        self.tasks[task_id] = {"description": description, "total": total, "completed": 0}
        return task_id

    def update(self, task_id, **kwargs):
        task = self.tasks[task_id]
        task.update(kwargs)
        self.updates.append(dict(task))

    def advance(self, task_id, advance=1):
        task = self.tasks[task_id]
        task["completed"] = task.get("completed", 0) + advance
        self.updates.append(dict(task))


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

    def fake_reencode(_src: Path, out: Path, progress_callback=None):
        assert progress_callback is not None
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


def test_repair_skips_metadata_verification_failures(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    errors_dir = tmp_path / "input_err"
    input_dir.mkdir()
    errors_dir.mkdir()

    candidate = errors_dir / "video.mp4"
    candidate.write_bytes(b"x" * 10)
    err_file = errors_dir / "video.err"
    err_file.write_text(
        "Verification failed: missing VBC tags: "
        "vbcencoder, vbcfinishedat, vbcoriginalbitrate"
    )

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
        extensions=[".mp4"],
        logger=None,
    )

    assert repaired == 0
    assert calls["flv"] == 0
    assert calls["reencode"] == 0
    assert not candidate.with_suffix(".mp4.repaired").exists()


def test_repair_progress_updates_by_file_size_during_reencode(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    errors_dir = tmp_path / "input_err"
    input_dir.mkdir()
    errors_dir.mkdir()

    candidate = errors_dir / "video.flv"
    candidate.write_bytes(b"x" * 100)
    err_file = errors_dir / "video.err"
    err_file.write_text("failed")

    _CaptureProgress.processing_instances = []

    def fake_flv(_src: Path, _out: Path):
        return False

    def fake_reencode(_src: Path, out: Path, progress_callback=None):
        assert progress_callback is not None
        progress_callback(40)
        out.write_bytes(b"m" * 7)
        return True

    monkeypatch.setattr(repair_mod, "Progress", _CaptureProgress)
    monkeypatch.setattr(repair_mod, "repair_flv_file", fake_flv)
    monkeypatch.setattr(repair_mod, "repair_via_reencode", fake_reencode)

    repaired = repair_mod.process_repairs(
        input_dirs=[input_dir],
        errors_dir_map={input_dir: errors_dir},
        extensions=[".flv"],
        logger=None,
    )

    assert repaired == 1
    processing_progress = _CaptureProgress.processing_instances[0]
    assert processing_progress.tasks[0]["total"] == 100
    assert any(update.get("completed") == 40 for update in processing_progress.updates)
