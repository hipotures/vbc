import json
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from pydantic import ValidationError

from vbc.config.models import AppConfig, GeneralConfig
from vbc.domain.models import (
    CompressionJob,
    CompressionManifest,
    MetadataRequest,
    MultipartPart,
    VideoFile,
    VideoMetadata,
    JobStatus,
)
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.ffmpeg import FFmpegAdapter, select_encoder_args
from vbc.infrastructure.file_scanner import FileScanner
from vbc.pipeline.orchestrator import Orchestrator


def _manifest(inputs: list[Path], output_path: Path, source_policy: str = "keep") -> dict:
    return {
        "schema_version": 1,
        "request_id": "ttracker-user_20260718_120000",
        "created_at": "2026-07-18T12:10:00+02:00",
        "producer": {
            "app": "ttracker",
            "username": "user",
            "recording_id": "user_20260718_120000",
            "source_size_bytes": 300,
            "source_latest_mtime_ns": 123,
        },
        "operation": "concat_transcode",
        "inputs": [str(path) for path in inputs],
        "output_path": str(output_path),
        "source_policy": source_policy,
        "compression_profile": "tiktok",
        "error_policy": {"missing_input": "fail"},
    }


def _part_info(width=640, height=1280, *, video_packets=10, audio_packets=10):
    return {
        "has_video_stream": video_packets > 0,
        "has_audio_stream": audio_packets > 0,
        "width": width,
        "height": height,
        "codec": "h264",
        "audio_codec": "aac" if audio_packets else None,
        "fps": 25.0,
        "duration": 10.0,
        "bitrate_kbps": 1000.0,
        "color_space": "bt709",
        "pix_fmt": "yuv420p",
        "video_packets": video_packets,
        "audio_packets": audio_packets,
    }


def _orchestrator(
    tmp_path,
    ffprobe,
    *,
    metadata_overrides=None,
    min_size=0,
    dynamic_quality=None,
):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    output_dir = tmp_path / "metadata_out"
    error_dir = tmp_path / "metadata_err"
    config = AppConfig(
        general=GeneralConfig(
            threads=1,
            gpu=False,
            use_exif=False,
            min_size_bytes=min_size,
            dynamic_quality=dynamic_quality or {},
        ),
        input_dirs=[
            {
                "path": str(metadata_dir),
                "enabled": True,
                "metadata": True,
                "idle_interval": 60,
            }
        ],
        metadata=metadata_overrides or {},
    )
    orchestrator = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=FileScanner(config.general.extensions, min_size),
        exif_adapter=MagicMock(),
        ffprobe_adapter=ffprobe,
        ffmpeg_adapter=MagicMock(),
        output_dir_map={metadata_dir: output_dir},
        errors_dir_map={metadata_dir: error_dir},
    )
    return orchestrator, metadata_dir, output_dir, error_dir


def test_manifest_schema_is_strict_and_requires_unique_absolute_inputs(tmp_path):
    part = tmp_path / "part001.mp4"
    output = tmp_path / "output.mp4"
    payload = _manifest([part], output)
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        CompressionManifest.model_validate(payload)

    payload.pop("unexpected")
    payload["inputs"] = [str(part), str(part)]
    with pytest.raises(ValidationError, match="unique"):
        CompressionManifest.model_validate(payload)


def test_metadata_hot_reload_keeps_last_valid_policy(tmp_path):
    config_path = tmp_path / "vbc.yaml"
    config_path.write_text("metadata:\n  audio_only: ignore\n")
    config = AppConfig(general=GeneralConfig(threads=1))
    orchestrator = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
        config_path=config_path,
    )

    assert orchestrator._load_current_metadata_config().audio_only == "ignore"
    config_path.write_text("metadata: [invalid")
    assert orchestrator._load_current_metadata_config().audio_only == "ignore"


def test_metadata_discovery_queues_proxy_then_hydrates_visible_job(tmp_path):
    ffprobe = MagicMock()
    orchestrator, metadata_dir, _, _ = _orchestrator(
        tmp_path,
        ffprobe,
        metadata_overrides={"audio_only": "ignore"},
        min_size=250,
    )
    part1 = tmp_path / "part001.mp4"
    part2 = tmp_path / "part002.mp4"
    audio = tmp_path / "part003.mp4"
    part1.write_bytes(b"a" * 100)
    part2.write_bytes(b"b" * 200)
    audio.write_bytes(b"c" * 50)
    output = tmp_path / "recording.mp4"
    manifest_path = metadata_dir / "request.json"
    manifest_path.write_text(json.dumps(_manifest([part1, part2, audio], output)))
    ffprobe.get_part_info.side_effect = [
        _part_info(320, 640),
        _part_info(640, 1280),
        {
            **_part_info(video_packets=0, audio_packets=5),
            "has_video_stream": False,
            "width": 0,
            "height": 0,
        },
    ]

    files, stats = orchestrator._perform_discovery(metadata_dir)

    assert stats["files_to_process"] == 1
    assert len(files) == 1
    video = files[0]
    assert video.path == output
    assert video.identity_path == manifest_path
    assert video.size_bytes == 350
    assert video.part_count == 3
    assert video.metadata is None
    assert video.metadata_request.parts == []
    ffprobe.get_part_info.assert_not_called()

    metadata = orchestrator._get_metadata(video)

    assert metadata is not None
    assert video.size_bytes == 300
    assert video.metadata.width == 640
    assert video.metadata.height == 1280
    assert video.metadata_request.ignored_inputs == [audio]
    assert video.metadata_request.effective_input_paths == [part1, part2]
    assert ffprobe.get_part_info.call_count == 3

    refreshed_files, _ = orchestrator._perform_discovery(metadata_dir)
    assert orchestrator._get_metadata(refreshed_files[0]) is not None
    assert ffprobe.get_part_info.call_count == 3


def test_metadata_preflight_routes_mixed_orientation_to_error(tmp_path):
    ffprobe = MagicMock()
    orchestrator, metadata_dir, _, error_dir = _orchestrator(tmp_path, ffprobe)
    part1 = tmp_path / "part001.mp4"
    part2 = tmp_path / "part002.mp4"
    part1.write_bytes(b"a")
    part2.write_bytes(b"b")
    manifest_path = metadata_dir / "request.json"
    manifest_path.write_text(
        json.dumps(_manifest([part1, part2], tmp_path / "recording.mp4"))
    )
    ffprobe.get_part_info.side_effect = [
        _part_info(640, 1280),
        _part_info(1280, 640),
    ]

    files, stats = orchestrator._perform_discovery(metadata_dir)

    assert len(files) == 1
    assert stats["ignored_err"] == 0
    assert orchestrator._get_metadata(files[0]) is None
    assert not manifest_path.exists()
    assert (error_dir / "request.json").exists()
    assert "orientations" in (error_dir / "request.err").read_text()


def test_metadata_min_size_uses_effective_aggregate_and_leaves_manifest(tmp_path):
    ffprobe = MagicMock()
    orchestrator, metadata_dir, _, error_dir = _orchestrator(
        tmp_path,
        ffprobe,
        min_size=250,
    )
    part1 = tmp_path / "part001.mp4"
    part2 = tmp_path / "part002.mp4"
    part1.write_bytes(b"a" * 100)
    part2.write_bytes(b"b" * 100)
    manifest_path = metadata_dir / "request.json"
    manifest_path.write_text(
        json.dumps(_manifest([part1, part2], tmp_path / "recording.mp4"))
    )
    ffprobe.get_part_info.side_effect = [_part_info(), _part_info()]

    files, stats = orchestrator._perform_discovery(metadata_dir)

    assert files == []
    assert stats["ignored_small"] == 1
    assert manifest_path.exists()
    assert not error_dir.exists()


def test_tagged_output_completes_delete_policy_before_missing_input_check(tmp_path):
    ffprobe = MagicMock()
    orchestrator, metadata_dir, output_dir, _ = _orchestrator(
        tmp_path,
        ffprobe,
        metadata_overrides={"source_policy": "delete_after_success"},
    )
    source = tmp_path / "part001.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "recording.mp4"
    output.write_bytes(b"encoded")
    manifest_path = metadata_dir / "request.json"
    manifest_path.write_text(json.dumps(_manifest([source], output)))
    orchestrator._verify_output_file = MagicMock(return_value=(True, None))

    files, stats = orchestrator._perform_discovery(metadata_dir)

    assert files == []
    assert stats["already_compressed"] == 1
    assert not source.exists()
    assert not manifest_path.exists()
    assert (output_dir / "request.json").exists()
    ffprobe.get_part_info.assert_not_called()


def test_refresh_does_not_remove_tmp_for_inflight_manifest(tmp_path):
    ffprobe = MagicMock()
    orchestrator, metadata_dir, _, _ = _orchestrator(tmp_path, ffprobe)
    part = tmp_path / "part001.mp4"
    part.write_bytes(b"source")
    output = tmp_path / "recording.mp4"
    tmp_output = output.with_suffix(".tmp")
    tmp_output.write_bytes(b"active")
    manifest_path = metadata_dir / "request.json"
    manifest_path.write_text(json.dumps(_manifest([part], output)))
    orchestrator._manifest_inflight.add(manifest_path)

    files, _ = orchestrator._perform_discovery(metadata_dir)

    assert files == []
    assert tmp_output.read_bytes() == b"active"
    assert manifest_path.exists()
    ffprobe.get_part_info.assert_not_called()


def test_multipart_ffmpeg_command_normalizes_video_and_synthesizes_silence(tmp_path):
    part1 = MultipartPart(
        path=tmp_path / "part001.mp4",
        width=320,
        height=640,
        codec="h264",
        audio_codec="aac",
        fps=25,
        duration=10,
        video_packets=10,
        audio_packets=10,
    )
    part2 = MultipartPart(
        path=tmp_path / "part002.mp4",
        width=640,
        height=1280,
        codec="h264",
        fps=50,
        duration=0.04,
        video_packets=1,
        audio_packets=0,
    )
    payload = CompressionManifest.model_validate(
        _manifest([part1.path, part2.path], tmp_path / "recording.mp4")
    )
    request = MetadataRequest(
        manifest_path=tmp_path / "request.json",
        metadata_dir=tmp_path,
        success_dir=tmp_path / "out",
        error_dir=tmp_path / "err",
        manifest=payload,
        parts=[part1, part2],
        source_policy="keep",
        compression_profile="tiktok",
        audio_only="ignore",
        target_width=640,
        target_height=1280,
    )
    video = VideoFile(
        path=Path(payload.output_path),
        size_bytes=300,
        metadata=VideoMetadata(width=640, height=1280, codec="h264", fps=25),
        metadata_request=request,
    )
    job = CompressionJob(source_file=video, output_path=video.path)
    config = AppConfig(general=GeneralConfig(threads=1, gpu=False))
    adapter = FFmpegAdapter(event_bus=MagicMock())

    command = adapter._build_command(
        job,
        config,
        select_encoder_args(config, use_gpu=False),
        use_gpu=False,
    )
    filter_graph = command[command.index("-filter_complex") + 1]

    assert command.count("-i") == 2
    assert command[2:6] == [
        "-filter_buffered_frames",
        "2048",
        "-reinit_filter",
        "0",
    ]
    assert "scale=640:1280:force_original_aspect_ratio=decrease" in filter_graph
    assert "pad=640:1280" in filter_graph
    assert "apad,atrim=duration=10.000000" in filter_graph
    assert "anullsrc=r=48000:cl=stereo" in filter_graph
    assert "concat=n=2:v=1:a=1" in filter_graph
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert command[-1].endswith("recording.tmp")


def test_multipart_compress_dispatches_to_sequential_staging(tmp_path):
    parts = [
        MultipartPart(
            path=tmp_path / f"part{index:03d}.mp4",
            width=640,
            height=1280,
            codec="h264",
            audio_codec="aac",
            fps=25,
            duration=10,
            video_packets=250,
            audio_packets=100,
        )
        for index in (1, 2)
    ]
    payload = CompressionManifest.model_validate(
        _manifest([part.path for part in parts], tmp_path / "recording.mp4")
    )
    request = MetadataRequest(
        manifest_path=tmp_path / "request.json",
        metadata_dir=tmp_path,
        success_dir=tmp_path / "out",
        error_dir=tmp_path / "err",
        manifest=payload,
        parts=parts,
        source_policy="keep",
        compression_profile="tiktok",
        audio_only="ignore",
        target_width=640,
        target_height=1280,
    )
    video = VideoFile(
        path=Path(payload.output_path),
        size_bytes=300,
        metadata=VideoMetadata(
            width=640,
            height=1280,
            codec="h264",
            fps=25,
            duration=20,
        ),
        metadata_request=request,
    )
    job = CompressionJob(source_file=video, output_path=video.path)
    config = AppConfig(general=GeneralConfig(threads=1, gpu=False))
    adapter = FFmpegAdapter(event_bus=MagicMock())
    adapter._compress_multipart_staged = MagicMock()

    adapter.compress(job, config, use_gpu=False)

    adapter._compress_multipart_staged.assert_called_once_with(
        job,
        config,
        False,
        None,
        None,
        None,
        None,
    )


def test_staged_multipart_cleans_segments_and_finalizes_output(tmp_path):
    parts = [
        MultipartPart(
            path=tmp_path / f"part{index:03d}.mp4",
            width=640,
            height=1280,
            codec="h264",
            audio_codec="aac",
            fps=25,
            duration=10,
            video_packets=250,
            audio_packets=100,
        )
        for index in (1, 2)
    ]
    payload = CompressionManifest.model_validate(
        _manifest([part.path for part in parts], tmp_path / "recording.mp4")
    )
    request = MetadataRequest(
        manifest_path=tmp_path / "request.json",
        metadata_dir=tmp_path,
        success_dir=tmp_path / "out",
        error_dir=tmp_path / "err",
        manifest=payload,
        parts=parts,
        source_policy="keep",
        compression_profile="tiktok",
        audio_only="ignore",
        target_width=640,
        target_height=1280,
    )
    video = VideoFile(
        path=Path(payload.output_path),
        size_bytes=300,
        metadata=VideoMetadata(
            width=640,
            height=1280,
            codec="h264",
            fps=25,
            duration=20,
        ),
        metadata_request=request,
    )
    job = CompressionJob(source_file=video, output_path=video.path)
    config = AppConfig(general=GeneralConfig(threads=1, gpu=False))
    adapter = FFmpegAdapter(event_bus=MagicMock())
    encoded_parts = []
    encoded_outputs = []
    existing_mp4 = tmp_path / ".recording.mp4.vbc-part001.mp4"
    existing_mp4.write_bytes(b"must-not-delete")

    def fake_compress(part_job, *_args, **_kwargs):
        encoded_parts.append(part_job.source_file.metadata_request.parts[0].path)
        encoded_outputs.append(part_job.output_path)
        part_job.output_path.write_bytes(b"segment")
        part_job.status = JobStatus.COMPLETED
        part_job.expected_video_frames = 250

    def fake_concat(cmd, concat_text, _shutdown_event):
        assert ".vbc-part001.tmp" in concat_text
        assert ".vbc-part002.tmp" in concat_text
        assert ".vbc-part001.mp4" not in concat_text
        Path(cmd[-1]).write_bytes(b"joined")
        return 0, False, ""

    adapter.compress = MagicMock(side_effect=fake_compress)
    adapter._run_concat_copy = MagicMock(side_effect=fake_concat)

    adapter._compress_multipart_staged(
        job,
        config,
        use_gpu=False,
        quality=None,
        rate_control=None,
        rotate=None,
        shutdown_event=None,
    )

    assert encoded_parts == [part.path for part in parts]
    assert all(path.suffix == ".tmp" for path in encoded_outputs)
    assert job.status == JobStatus.COMPLETED
    assert job.expected_video_frames == 500
    assert job.output_path.read_bytes() == b"joined"
    assert existing_mp4.read_bytes() == b"must-not-delete"
    assert not list(tmp_path.glob("*.vbc-part*.tmp"))


def test_staged_multipart_interrupt_keeps_manifest_job_retryable(tmp_path):
    parts = [
        MultipartPart(
            path=tmp_path / f"part{index:03d}.mp4",
            width=640,
            height=1280,
            codec="h264",
            audio_codec="aac",
            fps=25,
            duration=10,
            video_packets=250,
            audio_packets=100,
        )
        for index in (1, 2)
    ]
    payload = CompressionManifest.model_validate(
        _manifest([part.path for part in parts], tmp_path / "recording.mp4")
    )
    request = MetadataRequest(
        manifest_path=tmp_path / "request.json",
        metadata_dir=tmp_path,
        success_dir=tmp_path / "out",
        error_dir=tmp_path / "err",
        manifest=payload,
        parts=parts,
        source_policy="keep",
        compression_profile="tiktok",
        audio_only="ignore",
        target_width=640,
        target_height=1280,
    )
    video = VideoFile(
        path=Path(payload.output_path),
        size_bytes=300,
        metadata=VideoMetadata(
            width=640,
            height=1280,
            codec="h264",
            fps=25,
            duration=20,
        ),
        metadata_request=request,
    )
    job = CompressionJob(source_file=video, output_path=video.path)
    config = AppConfig(general=GeneralConfig(threads=1, gpu=False))
    adapter = FFmpegAdapter(event_bus=MagicMock())
    calls = 0

    def fake_compress(part_job, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            part_job.output_path.write_bytes(b"segment")
            part_job.status = JobStatus.COMPLETED
        else:
            part_job.output_path.with_suffix(".tmp").write_bytes(b"partial")
            part_job.status = JobStatus.INTERRUPTED
            part_job.error_message = "Interrupted by user (Ctrl+C)"

    adapter.compress = MagicMock(side_effect=fake_compress)
    adapter._run_concat_copy = MagicMock()

    adapter._compress_multipart_staged(
        job,
        config,
        use_gpu=False,
        quality=None,
        rate_control=None,
        rotate=None,
        shutdown_event=None,
    )

    assert job.status == JobStatus.INTERRUPTED
    assert job.error_message == "Interrupted by user (Ctrl+C)"
    assert not job.output_path.exists()
    assert not list(tmp_path.glob("*.vbc-part*"))
    adapter._run_concat_copy.assert_not_called()


def test_metadata_verification_rejects_dropped_video_frames(tmp_path):
    ffprobe = MagicMock()
    orchestrator, _, _, _ = _orchestrator(tmp_path, ffprobe)
    output = tmp_path / "recording.mp4"
    output.write_bytes(b"encoded")
    ffprobe.get_stream_info.return_value = {}
    ffprobe.get_part_info.return_value = {
        "has_video_stream": True,
        "video_packets": 5,
    }
    orchestrator.exif_adapter.extract_tags.return_value = {
        "XMP:VBCOriginalName": "part001.mp4",
        "XMP:VBCOriginalSize": "100",
        "XMP:VBCQuality": "CQ45",
        "XMP:VBCOriginalBitrate": "1 Mbps",
        "XMP:VBCEncoder": "NVENC AV1 (GPU)",
        "XMP:VBCFinishedAt": "2026-07-18T22:00:00+02:00",
    }

    verified, error = orchestrator._verify_output_file(
        output,
        expected_video_frames=10,
    )

    assert not verified
    assert error == "video frame count mismatch: expected 10, got 5"


def test_metadata_verification_accepts_configured_frame_loss(tmp_path, caplog):
    ffprobe = MagicMock()
    orchestrator, _, _, _ = _orchestrator(tmp_path, ffprobe)
    output = tmp_path / "recording.mp4"
    output.write_bytes(b"encoded")
    ffprobe.get_stream_info.return_value = {}
    ffprobe.get_part_info.return_value = {
        "has_video_stream": True,
        "video_packets": 9,
    }
    orchestrator.exif_adapter.extract_tags.return_value = {
        "XMP:VBCOriginalName": "part001.mp4",
        "XMP:VBCOriginalSize": "100",
        "XMP:VBCQuality": "CQ45",
        "XMP:VBCOriginalBitrate": "1 Mbps",
        "XMP:VBCEncoder": "NVENC AV1 (GPU)",
        "XMP:VBCFinishedAt": "2026-07-18T22:00:00+02:00",
    }

    verified, error = orchestrator._verify_output_file(
        output,
        expected_video_frames=10,
        max_dropped_frames=1,
    )
    verified_again, error_again = orchestrator._verify_output_file(
        output,
        expected_video_frames=10,
        max_dropped_frames=1,
    )

    assert verified
    assert error is None
    assert verified_again
    assert error_again is None
    ffprobe.get_part_info.assert_called_once_with(
        output,
        scan_packet_timeline=False,
    )
    assert "OUTPUT_FRAME_LOSS_ACCEPTED" in caplog.text


def test_metadata_verification_never_accepts_extra_video_frames(tmp_path):
    ffprobe = MagicMock()
    orchestrator, _, _, _ = _orchestrator(tmp_path, ffprobe)
    output = tmp_path / "recording.mp4"
    output.write_bytes(b"encoded")
    ffprobe.get_stream_info.return_value = {}
    ffprobe.get_part_info.return_value = {
        "has_video_stream": True,
        "video_packets": 11,
    }

    verified, error = orchestrator._verify_output_file(
        output,
        expected_video_frames=10,
        max_dropped_frames=1,
    )

    assert not verified
    assert error == "video frame count mismatch: expected 10, got 11"


def test_metadata_process_routes_success_and_deletes_all_original_inputs(tmp_path):
    ffprobe = MagicMock()
    orchestrator, metadata_dir, output_dir, _ = _orchestrator(
        tmp_path,
        ffprobe,
        metadata_overrides={
            "audio_only": "ignore",
            "source_policy": "delete_after_success",
            "compression_profile": "bulk",
        },
        dynamic_quality={"bulk": {"cq": 27}},
    )
    part = tmp_path / "part001.mp4"
    ignored = tmp_path / "part002.mp4"
    part.write_bytes(b"video")
    ignored.write_bytes(b"audio")
    output = tmp_path / "recording.mp4"
    manifest_path = metadata_dir / "request.json"
    payload = CompressionManifest.model_validate(
        _manifest([part, ignored], output, source_policy="keep")
    )
    request = MetadataRequest(
        manifest_path=manifest_path,
        metadata_dir=metadata_dir,
        success_dir=output_dir,
        error_dir=tmp_path / "metadata_err",
        manifest=payload,
        parts=[
            MultipartPart(
                path=part,
                width=640,
                height=1280,
                codec="h264",
                audio_codec="aac",
                fps=25,
                duration=1,
                video_packets=25,
                audio_packets=10,
            )
        ],
        ignored_inputs=[ignored],
        source_policy="keep",
        compression_profile="tiktok",
        audio_only="ignore",
        target_width=640,
        target_height=1280,
    )
    manifest_path.write_text(payload.model_dump_json())
    video = VideoFile(
        path=output,
        size_bytes=10,
        metadata=VideoMetadata(width=640, height=1280, codec="h264", fps=25),
        metadata_request=request,
    )

    def compress(job, *_args, **kwargs):
        assert kwargs["quality"] == 27
        job.output_path.write_bytes(b"encoded")
        job.status = JobStatus.COMPLETED
        job.expected_video_frames = 23

    orchestrator.ffmpeg_adapter.compress.side_effect = compress
    orchestrator._write_vbc_tags = MagicMock()
    orchestrator._verify_output_file = MagicMock(return_value=(True, None))
    orchestrator._verify_output_tags = MagicMock(return_value=(True, None))

    orchestrator._process_metadata_request(video)

    assert orchestrator._verify_output_file.call_args_list == [
        call(
            output,
            expected_video_frames=23,
            max_dropped_frames=0,
            require_vbc_tags=False,
        ),
    ]
    orchestrator._verify_output_tags.assert_called_once_with(output)
    assert output.exists()
    assert not part.exists()
    assert not ignored.exists()
    assert not manifest_path.exists()
    assert (output_dir / "request.json").exists()


def test_metadata_process_backs_up_untagged_output_before_compression(tmp_path):
    ffprobe = MagicMock()
    orchestrator, metadata_dir, output_dir, _ = _orchestrator(tmp_path, ffprobe)
    part = tmp_path / "part001.mp4"
    part.write_bytes(b"video")
    output = tmp_path / "recording.mp4"
    output.write_bytes(b"old")
    manifest_path = metadata_dir / "request.json"
    payload = CompressionManifest.model_validate(_manifest([part], output))
    request = MetadataRequest(
        manifest_path=manifest_path,
        metadata_dir=metadata_dir,
        success_dir=output_dir,
        error_dir=tmp_path / "metadata_err",
        manifest=payload,
        parts=[
            MultipartPart(
                path=part,
                width=640,
                height=1280,
                codec="h264",
                fps=25,
                duration=1,
                video_packets=25,
                audio_packets=0,
            )
        ],
        source_policy="keep",
        compression_profile="tiktok",
        audio_only="fail",
        target_width=640,
        target_height=1280,
    )
    manifest_path.write_text(payload.model_dump_json())
    video = VideoFile(
        path=output,
        size_bytes=5,
        metadata=VideoMetadata(width=640, height=1280, codec="h264", fps=25),
        metadata_request=request,
    )

    def compress(job, *_args, **_kwargs):
        job.output_path.write_bytes(b"new")
        job.status = JobStatus.COMPLETED
        job.expected_video_frames = 25

    orchestrator.ffmpeg_adapter.compress.side_effect = compress
    orchestrator._write_vbc_tags = MagicMock()
    orchestrator._verify_output_file = MagicMock(
        side_effect=[(False, "no tags"), (True, None)]
    )
    orchestrator._verify_output_tags = MagicMock(return_value=(True, None))

    orchestrator._process_metadata_request(video)

    assert output.read_bytes() == b"new"
    assert (tmp_path / "recording_1.mp4").read_bytes() == b"old"
    assert (output_dir / "request.json").exists()


def test_metadata_process_interruption_leaves_manifest_and_sources(tmp_path):
    ffprobe = MagicMock()
    orchestrator, metadata_dir, _, error_dir = _orchestrator(tmp_path, ffprobe)
    part = tmp_path / "part001.mp4"
    part.write_bytes(b"video")
    output = tmp_path / "recording.mp4"
    manifest_path = metadata_dir / "request.json"
    payload = CompressionManifest.model_validate(_manifest([part], output))
    request = MetadataRequest(
        manifest_path=manifest_path,
        metadata_dir=metadata_dir,
        success_dir=tmp_path / "metadata_out",
        error_dir=error_dir,
        manifest=payload,
        parts=[
            MultipartPart(
                path=part,
                width=640,
                height=1280,
                codec="h264",
                fps=25,
                duration=1,
                video_packets=25,
                audio_packets=0,
            )
        ],
        source_policy="keep",
        compression_profile="tiktok",
        audio_only="fail",
        target_width=640,
        target_height=1280,
    )
    manifest_path.write_text(payload.model_dump_json())
    video = VideoFile(
        path=output,
        size_bytes=5,
        metadata=VideoMetadata(width=640, height=1280, codec="h264", fps=25),
        metadata_request=request,
    )

    def interrupt(job, *_args, **_kwargs):
        job.status = JobStatus.INTERRUPTED
        job.error_message = "Interrupted by user (Ctrl+C)"

    orchestrator.ffmpeg_adapter.compress.side_effect = interrupt

    orchestrator._process_metadata_request(video)

    assert manifest_path.exists()
    assert part.exists()
    assert not error_dir.exists()


def test_metadata_compression_failure_routes_json_and_request_err(tmp_path):
    ffprobe = MagicMock()
    orchestrator, metadata_dir, _, error_dir = _orchestrator(tmp_path, ffprobe)
    part = tmp_path / "part001.mp4"
    part.write_bytes(b"video")
    output = tmp_path / "recording.mp4"
    manifest_path = metadata_dir / "request.json"
    payload = CompressionManifest.model_validate(_manifest([part], output))
    request = MetadataRequest(
        manifest_path=manifest_path,
        metadata_dir=metadata_dir,
        success_dir=tmp_path / "metadata_out",
        error_dir=error_dir,
        manifest=payload,
        parts=[
            MultipartPart(
                path=part,
                width=640,
                height=1280,
                codec="h264",
                fps=25,
                duration=1,
                video_packets=25,
                audio_packets=0,
            )
        ],
        source_policy="keep",
        compression_profile="tiktok",
        audio_only="fail",
        target_width=640,
        target_height=1280,
    )
    manifest_path.write_text(payload.model_dump_json())
    video = VideoFile(
        path=output,
        size_bytes=5,
        metadata=VideoMetadata(width=640, height=1280, codec="h264", fps=25),
        metadata_request=request,
    )

    def fail(job, *_args, **_kwargs):
        job.status = JobStatus.FAILED
        job.error_message = "ffmpeg exited with code 1"

    orchestrator.ffmpeg_adapter.compress.side_effect = fail

    orchestrator._process_metadata_request(video)

    assert part.exists()
    assert not manifest_path.exists()
    assert (error_dir / "request.json").exists()
    assert (error_dir / "request.err").read_text() == "ffmpeg exited with code 1"
    assert not output.with_suffix(".err").exists()
