from unittest.mock import MagicMock
from typer.testing import CliRunner

from vbc.config.models import AppConfig, GeneralConfig, AutoRotateConfig
from vbc import main as vbc_main


def test_main_missing_input_dir_exits(tmp_path):
    runner = CliRunner()
    config_path = tmp_path / "vbc.yaml"
    config_path.write_text("general: {}\n")
    result = runner.invoke(vbc_main.app, ["--config", str(config_path)])

    assert result.exit_code != 0
    assert "No input directories provided" in result.output


def test_main_compress_applies_overrides(tmp_path, monkeypatch):
    runner = CliRunner()
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    output_dir = tmp_path / "input_out"
    output_dir.mkdir()

    created = {}

    def fake_load_config(_path):
        return AppConfig(
            general=GeneralConfig(threads=4, cq=45, gpu=True, use_exif=False, copy_metadata=False),
            autorotate=AutoRotateConfig(patterns={}),
        )

    def fake_setup_logging(path, debug=False, log_path=None):
        created["log_path"] = path
        created["log_debug"] = debug
        created["log_file"] = log_path
        return MagicMock()

    class DummyExif:
        def __init__(self):
            self.et = MagicMock()
            self.et.running = True
            created["exif"] = self

    class DummyOrchestrator:
        def __init__(self, config, event_bus, file_scanner, exif_adapter, ffprobe_adapter, ffmpeg_adapter):
            created["config"] = config
            created["orchestrator"] = self

        def run(self, directory):
            created["run_dir"] = directory

    class DummyKeyboard:
        def __init__(self, _bus):
            pass

        def start(self):
            created["keyboard_started"] = True

        def stop(self):
            created["keyboard_stopped"] = True

    class DummyDashboard:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc_val, _exc_tb):
            return False

    class DummyHousekeeper:
        def cleanup_temp_files(self, directory):
            created["cleanup_temp"] = directory

        def cleanup_error_markers(self, directory):
            created["cleanup_err"] = directory

    monkeypatch.setattr(vbc_main, "load_config", fake_load_config)
    monkeypatch.setattr(vbc_main, "setup_logging", fake_setup_logging)
    monkeypatch.setattr(vbc_main, "ExifToolAdapter", DummyExif)
    monkeypatch.setattr(vbc_main, "Orchestrator", DummyOrchestrator)
    monkeypatch.setattr(vbc_main, "KeyboardListener", DummyKeyboard)
    monkeypatch.setattr(vbc_main, "Dashboard", DummyDashboard)
    monkeypatch.setattr(vbc_main, "HousekeepingService", DummyHousekeeper)
    monkeypatch.setattr(vbc_main, "FFprobeAdapter", MagicMock)
    monkeypatch.setattr(vbc_main, "FFmpegAdapter", MagicMock)
    monkeypatch.setattr(vbc_main, "FileScanner", MagicMock)
    monkeypatch.setattr(vbc_main, "UIManager", MagicMock)

    result = runner.invoke(
        vbc_main.app,
        [
            str(input_dir),
            "--threads",
            "2",
            "--cq",
            "30",
            "--cpu",
            "--queue-sort",
            "size-desc",
            "--queue-seed",
            "99",
            "--clean-errors",
            "--skip-av1",
            "--min-size",
            "123",
            "--rotate-180",
            "--debug",
        ],
    )

    assert result.exit_code == 0
    config = created["config"]
    assert config.general.threads == 2
    assert config.general.cq == 30
    assert config.general.gpu is False
    assert config.general.queue_sort == "size-desc"
    assert config.general.queue_seed == 99
    assert config.general.clean_errors is True
    assert config.general.skip_av1 is True
    assert config.general.min_size_bytes == 123
    assert config.general.manual_rotation == 180
    assert config.general.debug is True
    assert created["run_dir"] == [input_dir]
    assert created["log_path"] == output_dir
    assert created["log_debug"] is True
    assert created["keyboard_started"] is True
    assert created["keyboard_stopped"] is True
    assert created["cleanup_temp"] == input_dir
    assert created["cleanup_err"] == output_dir
    assert created["exif"].et.run.called
    assert created["exif"].et.terminate.called


def test_main_uses_config_input_dirs_when_cli_missing(tmp_path, monkeypatch):
    runner = CliRunner()
    input_dir_a = tmp_path / "input_a"
    input_dir_b = tmp_path / "input_b"
    input_dir_a.mkdir()
    input_dir_b.mkdir()

    created = {}

    def fake_load_config(_path):
        return AppConfig(
            general=GeneralConfig(threads=2, cq=45, gpu=True, use_exif=False, copy_metadata=False),
            input_dirs=[str(input_dir_a), str(input_dir_b), str(input_dir_a)],
            autorotate=AutoRotateConfig(patterns={}),
        )

    def fake_setup_logging(path, debug=False, log_path=None):
        created["log_path"] = path
        created["log_file"] = log_path
        return MagicMock()

    class DummyExif:
        def __init__(self):
            self.et = MagicMock()
            self.et.running = True
            created["exif"] = self

    class DummyOrchestrator:
        def __init__(self, config, event_bus, file_scanner, exif_adapter, ffprobe_adapter, ffmpeg_adapter):
            created["config"] = config

        def run(self, directory):
            created["run_dir"] = directory

    class DummyKeyboard:
        def __init__(self, _bus):
            pass

        def start(self):
            created["keyboard_started"] = True

        def stop(self):
            created["keyboard_stopped"] = True

    class DummyDashboard:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc_val, _exc_tb):
            return False

    class DummyHousekeeper:
        def cleanup_temp_files(self, _directory):
            pass

        def cleanup_error_markers(self, _directory):
            pass

    monkeypatch.setattr(vbc_main, "load_config", fake_load_config)
    monkeypatch.setattr(vbc_main, "setup_logging", fake_setup_logging)
    monkeypatch.setattr(vbc_main, "ExifToolAdapter", DummyExif)
    monkeypatch.setattr(vbc_main, "Orchestrator", DummyOrchestrator)
    monkeypatch.setattr(vbc_main, "KeyboardListener", DummyKeyboard)
    monkeypatch.setattr(vbc_main, "Dashboard", DummyDashboard)
    monkeypatch.setattr(vbc_main, "HousekeepingService", DummyHousekeeper)
    monkeypatch.setattr(vbc_main, "FFprobeAdapter", MagicMock)
    monkeypatch.setattr(vbc_main, "FFmpegAdapter", MagicMock)
    monkeypatch.setattr(vbc_main, "FileScanner", MagicMock)
    monkeypatch.setattr(vbc_main, "UIManager", MagicMock)

    result = runner.invoke(vbc_main.app, [])

    assert result.exit_code == 0
    assert created["run_dir"] == [input_dir_a, input_dir_b]
    assert created["log_path"] == input_dir_a.with_name(f"{input_dir_a.name}_out")
