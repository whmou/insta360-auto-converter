import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from insta360_converter.pipeline import (
    InsvPair,
    PlaylistManager,
    PipelineError,
    UploadLedger,
    build_stitch_command,
    build_upload_body,
    create_parser,
    discover_pairs,
    inject_spherical,
    upload_verified_file,
)


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def test_discovers_pairs_and_reports_incomplete(tmp_path: Path) -> None:
    left = tmp_path / "trip" / "VID_20220215_124434_00_025.insv"
    right = tmp_path / "trip" / "VID_20220215_124434_10_025.insv"
    orphan = tmp_path / "trip" / "VID_20220215_124500_00_026.insv"
    ignored = tmp_path / "trip" / "not-video.txt"
    for path in (left, right, orphan, ignored):
        touch(path)

    pairs, incomplete = discover_pairs(tmp_path)

    assert pairs == [InsvPair(left.resolve(), right.resolve())]
    assert incomplete == [orphan.resolve()]


def test_pairing_is_scoped_to_same_folder(tmp_path: Path) -> None:
    touch(tmp_path / "a" / "same_00_001.insv")
    touch(tmp_path / "b" / "same_10_001.insv")

    pairs, incomplete = discover_pairs(tmp_path)

    assert pairs == []
    assert len(incomplete) == 2


def test_stitch_command_keeps_paths_as_separate_arguments(tmp_path: Path) -> None:
    pair = InsvPair(tmp_path / "input folder" / "a_00_1.insv", tmp_path / "input folder" / "a_10_1.insv")
    command = build_stitch_command(
        tmp_path / "sdk" / "MediaSDKTest.exe",
        pair,
        tmp_path / "out folder" / "a.mp4",
        "5760x2880",
        200_000_000,
        "optflow",
        True,
    )

    assert str(pair.left) in command
    assert str(pair.right) in command
    assert command[-1] == "-enable_flowstate"
    accessory_index = command.index("-camera_accessory_type")
    assert command[accessory_index + 1] == "-1"
    assert "shell=True" not in command


def test_ledger_enforces_daily_cap_and_deduplicates(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    touch(video)
    ledger_path = tmp_path / "upload-ledger.json"
    ledger = UploadLedger(ledger_path, daily_limit=1)

    assert not ledger.already_uploaded(video)
    ledger.require_capacity()
    ledger.record(video, "youtube-id", "channel-id")

    reloaded = UploadLedger(ledger_path, daily_limit=1)
    assert reloaded.already_uploaded(video)
    assert reloaded.count_today() == 1
    with pytest.raises(PipelineError, match="daily upload cap"):
        reloaded.require_capacity()
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["version"] == 1


def test_invalid_daily_limit_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PipelineError):
        UploadLedger(tmp_path / "state.json", daily_limit=0)


def test_quota_day_resets_at_pacific_midnight() -> None:
    assert UploadLedger.today(datetime(2026, 7, 1, 6, 59, tzinfo=timezone.utc)) == "2026-06-30"
    assert UploadLedger.today(datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc)) == "2026-07-01"


def test_upload_default_daily_limit_is_80() -> None:
    args = create_parser().parse_args(
        [
            "upload",
            "output",
            "--token",
            "token.json",
            "--expected-channel-id",
            "channel-id",
        ]
    )
    assert args.daily_limit == 80
    assert args.privacy == "unlisted"
    assert not args.made_for_kids


def test_spatialmedia_default_can_be_a_python_package_directory() -> None:
    args = create_parser().parse_args(
        ["convert", "input", "output", "--sdk-exe", "MediaSDKTest.exe"]
    )
    assert args.spatialmedia.name == "spatialmedia"


def test_failed_metadata_injection_removes_large_partial(tmp_path: Path, monkeypatch) -> None:
    stitched = tmp_path / "video.stitched.mp4"
    final = tmp_path / "video.mp4"
    partial = tmp_path / "video.partial.mp4"
    touch(stitched)

    class Result:
        returncode = 1
        stdout = "failed"
        stderr = ""

    def failed_command(*args, **kwargs):
        touch(partial)
        return Result()

    monkeypatch.setattr("insta360_converter.pipeline._completed", failed_command)
    with pytest.raises(PipelineError, match="metadata injection failed"):
        inject_spherical(stitched, final, tmp_path / "spatialmedia")
    assert not partial.exists()


def test_upload_defaults_to_unlisted_and_not_made_for_kids(tmp_path: Path) -> None:
    body = build_upload_body(tmp_path / "dive.mp4", "unlisted")

    assert body["snippet"]["title"] == "dive"
    assert body["status"] == {
        "privacyStatus": "unlisted",
        "selfDeclaredMadeForKids": False,
    }


def test_upload_records_success_before_deleting_mp4(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "dive.mp4"
    touch(video)
    events = []

    class Ledger:
        def already_uploaded(self, path):
            return False

        def require_capacity(self):
            events.append("capacity")

        def record(self, path, video_id, channel_id, source_fingerprint=None):
            assert path.exists()
            events.append("record")

    monkeypatch.setattr("insta360_converter.pipeline.verify_360", lambda *args: events.append("verify"))
    monkeypatch.setattr("insta360_converter.pipeline.upload_video", lambda *args, **kwargs: "video-id")

    upload_verified_file(
        object(),
        "channel-id",
        video,
        Ledger(),
        tmp_path / "spatialmedia",
        "ffprobe",
        "unlisted",
        False,
        "source-key",
        delete_after_success=True,
    )

    assert events == ["capacity", "verify", "record"]
    assert not video.exists()


def test_failed_ledger_write_keeps_mp4(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "ski.mp4"
    touch(video)

    class Ledger:
        def already_uploaded(self, path):
            return False

        def require_capacity(self):
            pass

        def record(self, *args, **kwargs):
            raise PipelineError("ledger failed")

    monkeypatch.setattr("insta360_converter.pipeline.verify_360", lambda *args: None)
    monkeypatch.setattr("insta360_converter.pipeline.upload_video", lambda *args, **kwargs: "video-id")

    with pytest.raises(PipelineError, match="ledger failed"):
        upload_verified_file(
            object(),
            "channel-id",
            video,
            Ledger(),
            tmp_path / "spatialmedia",
            "ffprobe",
            "unlisted",
            False,
            delete_after_success=True,
        )

    assert video.exists()


def test_ledger_playlist_backfill_is_resumable(tmp_path: Path) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    touch(first)
    touch(second)
    ledger = UploadLedger(tmp_path / "ledger.json", daily_limit=80)
    ledger.record(first, "video-1", "channel")
    ledger.record(second, "video-2", "channel")

    assert ledger.assign_all_to_playlist("playlist") == 2
    assert [item["video_id"] for item in ledger.pending_playlist_entries("playlist")] == [
        "video-1",
        "video-2",
    ]

    ledger.mark_playlist_added("video-1", "playlist", "item-1")
    reloaded = UploadLedger(tmp_path / "ledger.json", daily_limit=80)
    assert [item["video_id"] for item in reloaded.pending_playlist_entries("playlist")] == ["video-2"]
    assert reloaded.assign_all_to_playlist("playlist") == 0


def test_playlist_manager_avoids_duplicate_insert() -> None:
    inserted = []

    class Request:
        def __init__(self, result):
            self.result = result

        def execute(self):
            return self.result

    class Playlists:
        def list(self, **kwargs):
            return Request({"items": [{"snippet": {"channelId": "channel", "title": "Trip"}}]})

    class PlaylistItems:
        def list(self, **kwargs):
            return Request(
                {"items": [{"id": "existing-item", "contentDetails": {"videoId": "existing-video"}}]}
            )

        def insert(self, **kwargs):
            inserted.append(kwargs["body"])
            return Request({"id": "new-item"})

    class Youtube:
        def playlists(self):
            return Playlists()

        def playlistItems(self):
            return PlaylistItems()

    manager = PlaylistManager(Youtube(), "playlist", "channel")

    assert manager.ensure("existing-video") == ("existing-item", False)
    assert manager.ensure("new-video") == ("new-item", True)
    assert inserted == [
        {
            "snippet": {
                "playlistId": "playlist",
                "resourceId": {"kind": "youtube#video", "videoId": "new-video"},
            }
        }
    ]


def test_playlist_failure_does_not_lose_uploaded_video(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "italy.mp4"
    touch(video)
    ledger = UploadLedger(tmp_path / "ledger.json", daily_limit=80)

    class Manager:
        playlist_id = "playlist"

        def ensure(self, video_id):
            raise PipelineError("playlist quota failed")

    monkeypatch.setattr("insta360_converter.pipeline.verify_360", lambda *args: None)
    monkeypatch.setattr("insta360_converter.pipeline.upload_video", lambda *args, **kwargs: "video-id")

    with pytest.raises(PipelineError, match="playlist quota failed"):
        upload_verified_file(
            object(),
            "channel-id",
            video,
            ledger,
            tmp_path / "spatialmedia",
            "ffprobe",
            "unlisted",
            False,
            "source-key",
            delete_after_success=True,
            playlist_manager=Manager(),
        )

    assert video.exists()
    assert ledger.source_uploaded("source-key")
    assert ledger.pending_playlist_entries("playlist")[0]["video_id"] == "video-id"
