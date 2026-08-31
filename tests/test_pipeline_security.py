import json
import os
import sys
import types
from pathlib import Path

import pytest

from insta360_converter.pipeline import (
    DefinitiveUploadError,
    InsvPair,
    PlaylistManager,
    PipelineError,
    UploadLedger,
    Verification,
    YOUTUBE_BROAD_SCOPE,
    YOUTUBE_PLAYLIST_SCOPE,
    YOUTUBE_UPLOAD_SCOPE,
    _credentials_cover,
    _conversion_identity,
    _full_file_identity,
    _safe_unlink,
    _sampled_file_identity,
    _sanitize_output,
    convert_pair,
    require_unique_output_stems,
    upload_verified_file,
    upload_video,
)


def _write(path: Path, value: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def test_v1_ledger_migrates_and_binds_to_its_record_channel(tmp_path: Path) -> None:
    video = _write(tmp_path / "video.mp4")
    fingerprint = UploadLedger.fingerprint(video)
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "uploads": {
                    fingerprint: {
                        "path": str(video),
                        "video_id": "video-id",
                        "channel_id": "channel-id",
                        "quota_day": UploadLedger.today(),
                        "uploaded_at": "2026-08-31T00:00:00+00:00",
                        "source_fingerprint": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    ledger = UploadLedger(path, 80, "channel-id")

    assert ledger.already_uploaded(video)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    assert persisted["channel_id"] == "channel-id"
    assert persisted["uploads"][fingerprint]["video_id"] == "video-id"


def test_mixed_channel_ledger_is_rejected(tmp_path: Path) -> None:
    def record(channel: str, video: str) -> dict:
        return {
            "path": str(tmp_path / f"{video}.mp4"),
            "video_id": video,
            "channel_id": channel,
            "uploaded_at": "2026-08-31T00:00:00+00:00",
            "source_fingerprint": None,
        }

    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "uploads": {
                    "a" * 64: record("channel-one", "video-one"),
                    "b" * 64: record("channel-two", "video-two"),
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PipelineError, match="different YouTube channels"):
        UploadLedger(path, 80)


def test_atomic_reservation_counts_failed_attempt_and_blocks_next(tmp_path: Path) -> None:
    first = _write(tmp_path / "first.mp4")
    second = _write(tmp_path / "second.mp4")
    path = tmp_path / "ledger.json"
    process_one = UploadLedger(path, 1, "channel-id")
    process_two = UploadLedger(path, 1, "channel-id")

    attempt = process_one.reserve_attempt(first, "channel-id")
    process_one.mark_attempt_failed(attempt, "network failure")

    assert process_two.count_today() == 1
    with pytest.raises(PipelineError, match="daily upload cap"):
        process_two.reserve_attempt(second, "channel-id")


def test_second_process_cannot_reserve_same_pending_artifact(tmp_path: Path) -> None:
    video = _write(tmp_path / "video.mp4")
    path = tmp_path / "ledger.json"
    first = UploadLedger(path, 80, "channel-id")
    second = UploadLedger(path, 80, "channel-id")
    first.reserve_attempt(video, "channel-id", "source-key")

    with pytest.raises(PipelineError, match="active upload reservation"):
        second.reserve_attempt(video, "channel-id", "source-key")


def test_dead_pending_attempt_requires_explicit_owner_resolution(tmp_path: Path, monkeypatch) -> None:
    video = _write(tmp_path / "video.mp4")
    ledger = UploadLedger(tmp_path / "ledger.json", 80, "channel-id")
    attempt = ledger.reserve_attempt(video, "channel-id", "source-key")
    monkeypatch.setattr("insta360_converter.pipeline._attempt_owner_alive", lambda owner: False)

    with pytest.raises(PipelineError, match="automatic retry is disabled"):
        ledger.reserve_attempt(video, "channel-id", "source-key")

    ledger.resolve_attempt(attempt, confirmed_failed=True)
    replacement = ledger.reserve_attempt(video, "channel-id", "source-key")
    assert replacement != attempt


def test_old_dead_pending_attempt_still_requires_resolution_next_quota_day(
    tmp_path: Path, monkeypatch
) -> None:
    quota_day = ["2026-08-30"]
    monkeypatch.setattr(UploadLedger, "today", staticmethod(lambda when=None: quota_day[0]))
    video = _write(tmp_path / "video.mp4")
    ledger = UploadLedger(tmp_path / "ledger.json", 80, "channel-id")
    attempt = ledger.reserve_attempt(video, "channel-id")
    quota_day[0] = "2026-08-31"
    monkeypatch.setattr("insta360_converter.pipeline._attempt_owner_alive", lambda owner: False)

    with pytest.raises(PipelineError, match="attempt-resolve"):
        ledger.reserve_attempt(video, "channel-id")

    ledger.resolve_attempt(attempt, confirmed_failed=True)
    ledger.reserve_attempt(video, "channel-id")
    assert ledger.count_today() == 1
    persisted = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert persisted["attempts"][attempt]["quota_day"] == "2026-08-30"
    assert persisted["attempts"][attempt]["status"] == "failed"


def test_confirmed_video_resolution_requires_remote_ownership_proof(
    tmp_path: Path, monkeypatch
) -> None:
    video = _write(tmp_path / "video.mp4")
    ledger = UploadLedger(tmp_path / "ledger.json", 80, "channel-id")
    attempt = ledger.reserve_attempt(video, "channel-id")
    monkeypatch.setattr("insta360_converter.pipeline._attempt_owner_alive", lambda owner: False)

    with pytest.raises(PipelineError, match="ownership must be verified"):
        ledger.resolve_attempt(attempt, video_id="video-id")


def test_failed_api_attempt_is_persisted_conservatively(tmp_path: Path, monkeypatch) -> None:
    video = _write(tmp_path / "video.mp4")
    ledger = UploadLedger(tmp_path / "ledger.json", 80, "channel-id")
    monkeypatch.setattr("insta360_converter.pipeline.verify_360", lambda *args: None)

    def fail_upload(*args, **kwargs):
        raise DefinitiveUploadError("API rejected upload")

    monkeypatch.setattr("insta360_converter.pipeline.upload_video", fail_upload)
    with pytest.raises(PipelineError, match="API rejected"):
        upload_verified_file(
            object(),
            "channel-id",
            video,
            ledger,
            tmp_path / "spatialmedia",
            "ffprobe",
            "unlisted",
            False,
        )

    reloaded = UploadLedger(ledger.path, 80, "channel-id")
    assert reloaded.count_today() == 1
    assert next(iter(reloaded.data["attempts"].values()))["status"] == "failed"


def test_ambiguous_transport_failure_remains_pending(tmp_path: Path, monkeypatch) -> None:
    video = _write(tmp_path / "video.mp4")
    ledger = UploadLedger(tmp_path / "ledger.json", 80, "channel-id")
    monkeypatch.setattr("insta360_converter.pipeline.verify_360", lambda *args: None)
    monkeypatch.setattr(
        "insta360_converter.pipeline.upload_video",
        lambda *args, **kwargs: (_ for _ in ()).throw(PipelineError("connection lost")),
    )

    with pytest.raises(PipelineError, match="connection lost"):
        upload_verified_file(
            object(),
            "channel-id",
            video,
            ledger,
            tmp_path / "spatialmedia",
            "ffprobe",
            "unlisted",
            False,
        )

    persisted = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert next(iter(persisted["attempts"].values()))["status"] == "pending"


def test_transient_transport_error_reuses_same_resumable_request(tmp_path: Path, monkeypatch) -> None:
    video = _write(tmp_path / "video.mp4")
    errors_module = types.ModuleType("googleapiclient.errors")
    http_module = types.ModuleType("googleapiclient.http")

    class FakeHttpError(Exception):
        pass

    errors_module.HttpError = FakeHttpError
    http_module.MediaFileUpload = lambda *args, **kwargs: object()
    monkeypatch.setitem(sys.modules, "googleapiclient.errors", errors_module)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http_module)
    monkeypatch.setattr("insta360_converter.pipeline.time.sleep", lambda seconds: None)
    calls = []

    class Request:
        def next_chunk(self):
            calls.append("chunk")
            if len(calls) < 3:
                raise OSError("temporary socket failure")
            return None, {"id": "video-id"}

    request = Request()

    class Videos:
        def insert(self, **kwargs):
            return request

    class Youtube:
        def videos(self):
            return Videos()

    assert upload_video(Youtube(), video, "unlisted") == "video-id"
    assert calls == ["chunk", "chunk", "chunk"]


def test_artifact_receipt_is_invalidated_when_file_changes(tmp_path: Path) -> None:
    video = _write(tmp_path / "video.mp4")
    ledger = UploadLedger(tmp_path / "ledger.json", 80, "channel-id")
    ledger.record(video, "video-id", "channel-id")

    video.write_bytes(b"replacement")

    assert not ledger.already_uploaded(video)


def test_preserved_size_and_mtime_replacement_fails_strong_identity(tmp_path: Path) -> None:
    video = _write(tmp_path / "video.mp4", b"original")
    original_stat = video.stat()
    weak_fingerprint = UploadLedger.fingerprint(video)
    ledger = UploadLedger(tmp_path / "ledger.json", 80, "channel-id")
    ledger.record(video, "video-id", "channel-id")

    video.write_bytes(b"replaced")
    os.utime(video, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert UploadLedger.fingerprint(video) == weak_fingerprint
    assert not ledger.already_uploaded(video)


def test_middle_only_replacement_fails_full_artifact_identity(tmp_path: Path) -> None:
    video = _write(tmp_path / "large.mp4", b"a" * (3 * 1024 * 1024))
    original_stat = video.stat()
    ledger = UploadLedger(tmp_path / "ledger.json", 80, "channel-id")
    ledger.record(video, "video-id", "channel-id")
    sampled_before = _sampled_file_identity(video)

    with video.open("r+b") as target:
        target.seek(1536 * 1024)
        target.write(b"b" * 4096)
    os.utime(video, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert _sampled_file_identity(video) == sampled_before
    assert not ledger.already_uploaded(video)


def test_legacy_receipt_deduplicates_but_cannot_authorize_cleanup(tmp_path: Path, monkeypatch) -> None:
    video = _write(tmp_path / "video.mp4")
    fingerprint = UploadLedger.fingerprint(video)
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "version": 1,
                "uploads": {
                    fingerprint: {
                        "path": str(video),
                        "video_id": "video-id",
                        "channel_id": "channel-id",
                        "uploaded_at": "2026-08-31T00:00:00+00:00",
                        "source_fingerprint": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ledger = UploadLedger(ledger_path, 80, "channel-id")
    monkeypatch.setattr(
        "insta360_converter.pipeline.upload_video",
        lambda *args, **kwargs: pytest.fail("legacy receipt must suppress duplicate upload"),
    )

    upload_verified_file(
        object(),
        "channel-id",
        video,
        ledger,
        tmp_path / "spatialmedia",
        "ffprobe",
        "unlisted",
        False,
        delete_after_success=True,
    )

    assert video.exists()


def test_pending_playlist_prevents_restart_cleanup_without_playlist_flag(
    tmp_path: Path, monkeypatch
) -> None:
    video = _write(tmp_path / "video.mp4")
    ledger = UploadLedger(tmp_path / "ledger.json", 80, "channel-id")
    ledger.record(video, "video-id", "channel-id", playlist_id="playlist-id")
    monkeypatch.setattr(
        "insta360_converter.pipeline.upload_video",
        lambda *args, **kwargs: pytest.fail("existing upload must not repeat"),
    )

    upload_verified_file(
        object(),
        "channel-id",
        video,
        ledger,
        tmp_path / "spatialmedia",
        "ffprobe",
        "unlisted",
        False,
        delete_after_success=True,
        playlist_manager=None,
    )
    assert video.exists()

    ledger.mark_playlist_added("video-id", "playlist-id", "playlist-item-id")
    upload_verified_file(
        object(),
        "channel-id",
        video,
        ledger,
        tmp_path / "spatialmedia",
        "ffprobe",
        "unlisted",
        False,
        delete_after_success=True,
        playlist_manager=None,
    )
    assert not video.exists()


def test_cleanup_rechecks_exact_artifact_fingerprint(tmp_path: Path) -> None:
    video = _write(tmp_path / "video.mp4")
    fingerprint = UploadLedger.fingerprint(video)
    video.write_bytes(b"changed")

    with pytest.raises(PipelineError, match="changed local artifact"):
        _safe_unlink(video, root=tmp_path, expected_fingerprint=fingerprint)
    assert video.exists()


def test_duplicate_recursive_output_names_are_rejected(tmp_path: Path) -> None:
    first = InsvPair(tmp_path / "a" / "same_00_001.insv", tmp_path / "a" / "same_10_001.insv")
    second = InsvPair(tmp_path / "b" / "same_00_001.insv", tmp_path / "b" / "same_10_001.insv")

    with pytest.raises(PipelineError, match="same MP4 name"):
        require_unique_output_stems([first, second])


def test_pre_hardening_output_requires_explicit_force(tmp_path: Path) -> None:
    pair = InsvPair(_write(tmp_path / "input_00_001.insv"), _write(tmp_path / "input_10_001.insv"))
    output = tmp_path / "output"
    _write(output / "input_00_001.mp4")

    with pytest.raises(PipelineError, match="pre-hardening artifact"):
        convert_pair(
            pair,
            output,
            tmp_path / "MediaSDKTest.exe",
            tmp_path / "spatialmedia",
            "5760x2880",
            200_000_000,
            "optflow",
            True,
            -1,
            False,
            False,
            "ffprobe",
        )


def test_verified_pre_hardening_final_can_be_explicitly_adopted(tmp_path: Path, monkeypatch) -> None:
    pair = InsvPair(_write(tmp_path / "input_00_001.insv"), _write(tmp_path / "input_10_001.insv"))
    output = tmp_path / "output"
    final = _write(output / "input_00_001.mp4", b"verified-video")
    monkeypatch.setattr(
        "insta360_converter.pipeline.verify_360",
        lambda *args: Verification(str(final), 5760, 2880, 1.0, True, "equirectangular"),
    )

    verified = convert_pair(
        pair,
        output,
        tmp_path / "MediaSDKTest.exe",
        tmp_path / "spatialmedia",
        "5760x2880",
        200_000_000,
        "optflow",
        True,
        -1,
        False,
        False,
        "ffprobe",
        adopt_existing=True,
    )

    manifest = json.loads((output / "input_00_001.conversion.json").read_text(encoding="utf-8"))
    assert verified.path == str(final)
    assert manifest["state"] == "complete"
    assert manifest["artifact_identity"] == _full_file_identity(final)


def test_corrupt_manifest_bound_intermediate_is_rebuilt(tmp_path: Path, monkeypatch) -> None:
    pair = InsvPair(_write(tmp_path / "input_00_001.insv"), _write(tmp_path / "input_10_001.insv"))
    output = tmp_path / "output"
    output.mkdir()
    stitched = _write(output / "input_00_001.stitched.mp4", b"corrupt")
    final = output / "input_00_001.mp4"
    expected = {
        "version": 1,
        "output": final.name,
        **_conversion_identity(pair, "5760x2880", 200_000_000, "optflow", True, -1),
        "state": "converting",
    }
    (output / "input_00_001.conversion.json").write_text(json.dumps(expected), encoding="utf-8")
    events = []

    def probe(path, ffprobe):
        if Path(path) == stitched and stitched.read_bytes() == b"corrupt":
            raise PipelineError("bad intermediate")
        return {"streams": [{"codec_type": "video"}], "format": {"duration": "1"}}

    def run(command, cwd, log_path):
        events.append("rebuild")
        target = Path(command[command.index("-output") + 1])
        target.write_bytes(b"fresh")

    def inject(stitched_input, final_output, spatialmedia):
        final_output.write_bytes(b"verified-final")

    monkeypatch.setattr("insta360_converter.pipeline.probe_video", probe)
    monkeypatch.setattr("insta360_converter.pipeline.run_streamed", run)
    monkeypatch.setattr("insta360_converter.pipeline.inject_spherical", inject)
    monkeypatch.setattr(
        "insta360_converter.pipeline.verify_360",
        lambda *args: Verification(str(final), 5760, 2880, 1.0, True, "equirectangular"),
    )

    convert_pair(
        pair,
        output,
        tmp_path / "MediaSDKTest.exe",
        tmp_path / "spatialmedia",
        "5760x2880",
        200_000_000,
        "optflow",
        True,
        -1,
        True,
        False,
        "ffprobe",
    )

    assert events == ["rebuild"]
    assert stitched.read_bytes() == b"fresh"


def test_valid_but_replaced_stitched_intermediate_is_not_reused(tmp_path: Path, monkeypatch) -> None:
    pair = InsvPair(_write(tmp_path / "input_00_001.insv"), _write(tmp_path / "input_10_001.insv"))
    output = tmp_path / "output"
    output.mkdir()
    stitched = _write(output / "input_00_001.stitched.mp4", b"original")
    final = output / "input_00_001.mp4"
    expected = {
        "version": 1,
        "output": final.name,
        **_conversion_identity(pair, "5760x2880", 200_000_000, "optflow", True, -1),
        "state": "stitched",
        "stitched_fingerprint": UploadLedger.fingerprint(stitched),
        "stitched_identity": _full_file_identity(stitched),
    }
    (output / "input_00_001.conversion.json").write_text(json.dumps(expected), encoding="utf-8")
    stitched.write_bytes(b"replaced-valid")
    events = []
    monkeypatch.setattr(
        "insta360_converter.pipeline.probe_video",
        lambda *args: {"streams": [{"codec_type": "video"}], "format": {"duration": "1"}},
    )

    def run(command, cwd, log_path):
        events.append("rebuild")
        Path(command[command.index("-output") + 1]).write_bytes(b"rebuilt")

    monkeypatch.setattr("insta360_converter.pipeline.run_streamed", run)
    monkeypatch.setattr(
        "insta360_converter.pipeline.inject_spherical",
        lambda stitched_input, final_output, spatialmedia: final_output.write_bytes(b"final"),
    )
    monkeypatch.setattr(
        "insta360_converter.pipeline.verify_360",
        lambda *args: Verification(str(final), 5760, 2880, 1.0, True, "equirectangular"),
    )

    convert_pair(
        pair,
        output,
        tmp_path / "MediaSDKTest.exe",
        tmp_path / "spatialmedia",
        "5760x2880",
        200_000_000,
        "optflow",
        True,
        -1,
        True,
        False,
        "ffprobe",
    )

    assert events == ["rebuild"]
    assert stitched.read_bytes() == b"rebuilt"


def test_terminal_output_is_inert_and_bounded() -> None:
    value = "\x1b[31mred\x1b[0m\x00" + "x" * 100

    rendered = _sanitize_output(value, 20)

    assert "\x1b" not in rendered
    assert "\x00" not in rendered
    assert rendered.endswith("[output truncated]")


def test_oauth_scope_compatibility_accepts_existing_broad_token() -> None:
    class Credentials:
        scopes = [YOUTUBE_BROAD_SCOPE]

    assert _credentials_cover(Credentials(), [YOUTUBE_UPLOAD_SCOPE, YOUTUBE_PLAYLIST_SCOPE])


def test_ledger_playlist_video_must_belong_to_authenticated_channel() -> None:
    class Request:
        def __init__(self, result):
            self.result = result

        def execute(self):
            return self.result

    class Playlists:
        def list(self, **kwargs):
            return Request({"items": [{"snippet": {"channelId": "channel-id", "title": "Trip"}}]})

    class PlaylistItems:
        def list(self, **kwargs):
            return Request(
                {"items": [{"id": "existing-item", "contentDetails": {"videoId": "video-id"}}]}
            )

        def insert(self, **kwargs):
            raise AssertionError("foreign video must not be inserted")

    class Videos:
        def list(self, **kwargs):
            return Request({"items": [{"snippet": {"channelId": "foreign-channel"}}]})

    class Youtube:
        def playlists(self):
            return Playlists()

        def playlistItems(self):
            return PlaylistItems()

        def videos(self):
            return Videos()

    manager = PlaylistManager(Youtube(), "playlist-id", "channel-id")
    with pytest.raises(PipelineError, match="not owned"):
        manager.ensure("video-id", validate_owner=True)
