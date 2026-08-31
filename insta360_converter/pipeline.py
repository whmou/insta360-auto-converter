"""Core local Insta360 stitching, spherical metadata injection, and YouTube workflow.

The conversion path is intentionally independent from YouTube. A failed upload
never causes the expensive MediaSDK conversion to be repeated.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import http.client
import json
import logging
import os
import random
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo


PAIR_MARKER = re.compile(r"_00_", re.IGNORECASE)
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_PLAYLIST_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_BROAD_SCOPE = "https://www.googleapis.com/auth/youtube"
SCOPES = [YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE]
DEFAULT_DAILY_UPLOAD_LIMIT = 80
YOUTUBE_QUOTA_TIMEZONE = ZoneInfo("America/Los_Angeles")
WINDOWS_DRIVE_REMOVABLE = 2
WINDOWS_DRIVE_FIXED = 3
MAX_LEDGER_BYTES = 16 * 1024 * 1024
MAX_LEDGER_RECORDS = 100_000
MAX_CAPTURE_BYTES = 1024 * 1024
MAX_STREAM_LOG_BYTES = 16 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 120
METADATA_TIMEOUT_SECONDS = 300
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
HEX_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class PipelineError(RuntimeError):
    """A user-actionable pipeline failure."""


class DefinitiveUploadError(PipelineError):
    """An upload failure that proves YouTube did not accept a video."""


@dataclass(frozen=True)
class InsvPair:
    left: Path
    right: Path

    @property
    def output_stem(self) -> str:
        return self.left.stem


@dataclass(frozen=True)
class Verification:
    path: str
    width: int
    height: int
    duration_seconds: float
    spherical: bool
    projection: str


def _sanitize_output(value: str, limit: int = MAX_CAPTURE_BYTES) -> str:
    """Render tool output as inert, bounded text (including in AI-visible logs)."""
    value = ANSI_ESCAPE.sub("", value)
    value = "".join(char if char in "\n\r\t" or ord(char) >= 32 else "�" for char in value)
    if len(value) > limit:
        return value[:limit] + "\n[output truncated]"
    return value


def _safe_label(value: Any, limit: int = 256) -> str:
    return re.sub(r"[\r\n\t]+", " ", _sanitize_output(str(value), limit)).strip()


def _redact_command_paths(value: str, command: Sequence[str], cwd: Path) -> str:
    candidates = [str(cwd), *(str(item) for item in command)]
    for candidate in sorted(set(candidates), key=len, reverse=True):
        if not Path(candidate).is_absolute():
            continue
        replacement = f"<{Path(candidate).name or 'local-path'}>"
        value = re.sub(re.escape(candidate), replacement, value, flags=re.IGNORECASE)
        value = re.sub(re.escape(candidate.replace("\\", "/")), replacement, value, flags=re.IGNORECASE)
    return value


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _reject_reparse_chain(path: Path, label: str) -> None:
    """Reject symlinks/junctions in the existing portion of a sensitive path."""
    absolute = Path(os.path.abspath(path.expanduser()))
    current = Path(absolute.anchor) if absolute.anchor else Path()
    for part in absolute.parts[1:] if absolute.anchor else absolute.parts:
        current /= part
        if current.exists() and _is_reparse_point(current):
            raise PipelineError(f"{label} cannot pass through a symlink or junction: {current}")


def require_fixed_local_path(path: Path, label: str) -> Path:
    """Require a non-reparse path on fixed local storage (strict on Windows)."""
    _reject_reparse_chain(path, label)
    resolved = path.expanduser().resolve()
    if sys.platform == "win32":
        if not resolved.drive or str(resolved).startswith("\\\\"):
            raise PipelineError(f"{label} must be on a fixed local Windows drive")
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{resolved.drive}\\")
        if drive_type != WINDOWS_DRIVE_FIXED:
            raise PipelineError(f"{label} must be on fixed local storage: {resolved.drive}")
    return resolved


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _boot_id() -> str:
    """Return a stable identifier for the current boot without new dependencies."""
    if sys.platform == "win32":
        uptime_ms = int(ctypes.windll.kernel32.GetTickCount64())
        boot_minute = int((time.time() - uptime_ms / 1000) // 60)
        return f"windows-{boot_minute}"
    linux_boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        return linux_boot_id.read_text(encoding="ascii").strip()
    except OSError:
        return "unknown-boot"


def _process_start_id(pid: int) -> str | None:
    if pid < 1:
        return None
    if sys.platform == "win32":
        class FILETIME(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            creation = FILETIME()
            exit_time = FILETIME()
            kernel = FILETIME()
            user = FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return str((creation.high << 32) | creation.low)
        finally:
            kernel32.CloseHandle(handle)
    try:
        process_stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields_after_name = process_stat[process_stat.rfind(")") + 2 :].split()
        return fields_after_name[19]
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return f"alive-{pid}"


def _current_attempt_owner() -> dict[str, Any]:
    pid = os.getpid()
    return {
        "host": socket.gethostname(),
        "boot_id": _boot_id(),
        "pid": pid,
        "process_start_id": _process_start_id(pid),
    }


def _attempt_owner_alive(owner: Any) -> bool | None:
    """Return True/False when provable, otherwise None for legacy owners."""
    if not isinstance(owner, dict):
        return None
    if owner.get("host") != socket.gethostname():
        return None
    if owner.get("boot_id") != _boot_id():
        return False
    try:
        pid = int(owner["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    expected_start = owner.get("process_start_id")
    actual_start = _process_start_id(pid)
    if actual_start is None:
        return False
    if not expected_start:
        return True
    return actual_start == expected_start


def _safe_unlink(
    path: Path,
    *,
    root: Path,
    expected_fingerprint: str | None = None,
    expected_identity: str | None = None,
    missing_ok: bool = False,
) -> None:
    """Unlink only the exact, ordinary artifact that was previously inspected."""
    _reject_reparse_chain(path, "Cleanup target")
    if not _contained(path, root):
        raise PipelineError(f"Refusing to remove an artifact outside its output folder: {path.name}")
    if not path.exists():
        if missing_ok:
            return
        raise PipelineError(f"Cleanup target no longer exists: {path.name}")
    if not path.is_file() or _is_reparse_point(path):
        raise PipelineError(f"Refusing to remove a non-regular artifact: {path.name}")
    if expected_fingerprint and UploadLedger.fingerprint(path) != expected_fingerprint:
        raise PipelineError(f"Refusing to remove a changed local artifact: {path.name}")
    if expected_identity and _full_file_identity(path) != expected_identity:
        raise PipelineError(f"Refusing to remove changed artifact content: {path.name}")
    path.unlink()


def _record_cleanup_ready(record: dict[str, Any]) -> bool:
    if not record.get("_cleanup_authorized"):
        return False
    playlists = record.get("playlists", {})
    return isinstance(playlists, dict) and all(
        isinstance(state, dict) and state.get("status") == "added"
        for state in playlists.values()
    )


def _atomic_json_write(path: Path, payload: dict[str, Any], *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if private:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _insv_files(source: Path, recursive: bool) -> list[Path]:
    iterator = source.rglob("*") if recursive else source.iterdir()
    return sorted(
        (path for path in iterator if path.is_file() and path.suffix.lower() == ".insv"),
        key=lambda path: str(path).lower(),
    )


def discover_pairs(source: Path, recursive: bool = True) -> tuple[list[InsvPair], list[Path]]:
    """Return deterministic `_00_`/`_10_` pairs and unpaired INSV files."""
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise PipelineError(f"Input folder does not exist: {source}")

    files = _insv_files(source, recursive)
    by_key = {str(path).lower(): path for path in files}
    pairs: list[InsvPair] = []
    paired_keys: set[str] = set()

    for left in files:
        if not PAIR_MARKER.search(left.name):
            continue
        right_name = PAIR_MARKER.sub("_10_", left.name, count=1)
        right_key = str(left.with_name(right_name)).lower()
        right = by_key.get(right_key)
        if right:
            pairs.append(InsvPair(left=left, right=right))
            paired_keys.update((str(left).lower(), str(right).lower()))

    incomplete = [path for path in files if str(path).lower() not in paired_keys]
    return pairs, incomplete


def require_unique_output_stems(pairs: Sequence[InsvPair]) -> None:
    seen: dict[str, Path] = {}
    for pair in pairs:
        key = pair.output_stem.lower()
        previous = seen.get(key)
        if previous and previous.parent != pair.left.parent:
            raise PipelineError(
                f"Two source folders would create the same MP4 name ({pair.output_stem}.mp4). "
                "Convert them to separate output folders."
            )
        seen[key] = pair.left


def removable_drive_roots() -> list[Path]:
    """Return mounted Windows drives reported as removable media."""
    if sys.platform != "win32":
        return []
    drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
    roots = []
    for index in range(26):
        if drive_mask & (1 << index):
            root = Path(f"{chr(ord('A') + index)}:\\")
            if ctypes.windll.kernel32.GetDriveTypeW(str(root)) == WINDOWS_DRIVE_REMOVABLE:
                roots.append(root)
    return roots


def find_sd_source(source: Path | None = None) -> tuple[Path, list[InsvPair], list[Path]]:
    """Resolve an explicit source or the sole removable drive containing INSV pairs."""
    if source is not None:
        resolved = source.expanduser().resolve()
        pairs, incomplete = discover_pairs(resolved)
        if not pairs:
            raise PipelineError(f"No complete INSV pairs found: {resolved}")
        return resolved, pairs, incomplete

    candidates = []
    for root in removable_drive_roots():
        try:
            pairs, incomplete = discover_pairs(root)
        except (OSError, PipelineError):
            continue
        if pairs:
            candidates.append((root.resolve(), pairs, incomplete))
    if not candidates:
        raise PipelineError(
            "No removable drive with complete INSV pairs was found. "
            "Insert the card or provide --source E:\\DCIM."
        )
    if len(candidates) > 1:
        drives = ", ".join(str(item[0]) for item in candidates)
        raise PipelineError(f"Multiple removable drives contain INSV pairs ({drives}); use --source")
    return candidates[0]


def require_local_output(source: Path, output: Path) -> None:
    """Prevent temporary MP4 files from being written back to the source card."""
    source_drive = source.resolve().drive.lower()
    output_drive = output.expanduser().resolve().drive.lower()
    if source_drive and source_drive == output_drive:
        raise PipelineError(f"Output must be on a different local drive than the SD card: {output}")
    if sys.platform == "win32" and output_drive:
        output_root = f"{output_drive}\\"
        if ctypes.windll.kernel32.GetDriveTypeW(output_root) == WINDOWS_DRIVE_REMOVABLE:
            raise PipelineError(f"Output must be on a local disk, not removable media: {output}")


def pair_fingerprint(pair: InsvPair) -> str:
    """Identify source footage without depending on the reader's assigned drive letter."""
    parts = []
    for path in (pair.left, pair.right):
        stat = path.stat()
        parts.append(f"{path.name.lower()}|{stat.st_size}|{stat.st_mtime_ns}")
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()


def _sampled_file_identity(path: Path) -> str:
    """Bind a manifest to large source media without hashing every byte."""
    info = path.stat()
    digest = hashlib.sha256()
    digest.update(path.name.lower().encode("utf-8"))
    digest.update(str(info.st_size).encode("ascii"))
    with path.open("rb") as source:
        digest.update(source.read(1024 * 1024))
        if info.st_size > 1024 * 1024:
            source.seek(max(0, info.st_size - 1024 * 1024))
            digest.update(source.read(1024 * 1024))
    return digest.hexdigest()


def _full_file_identity(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(4 * 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def pair_content_identity(pair: InsvPair) -> str:
    value = f"{_sampled_file_identity(pair.left)}|{_sampled_file_identity(pair.right)}"
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _conversion_identity(
    pair: InsvPair,
    output_size: str,
    bitrate: int,
    stitch_type: str,
    flowstate: bool,
    camera_accessory_type: int,
) -> dict[str, Any]:
    return {
        "source": {
            "left_name": pair.left.name,
            "right_name": pair.right.name,
            "left_identity": _sampled_file_identity(pair.left),
            "right_identity": _sampled_file_identity(pair.right),
        },
        "settings": {
            "output_size": output_size,
            "bitrate": bitrate,
            "stitch_type": stitch_type,
            "flowstate": flowstate,
            "camera_accessory_type": camera_accessory_type,
        },
    }


def _load_conversion_manifest(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise PipelineError(f"Conversion manifest is unexpectedly large: {path.name}")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Cannot read conversion manifest: {path.name}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise PipelineError(f"Unsupported conversion manifest: {path.name}")
    return data


def build_stitch_command(
    sdk_executable: Path,
    pair: InsvPair,
    stitched_output: Path,
    output_size: str,
    bitrate: int,
    stitch_type: str,
    flowstate: bool,
    camera_accessory_type: int = -1,
) -> list[str]:
    command = [
        str(sdk_executable),
        "-inputs",
        str(pair.left),
        str(pair.right),
        "-output",
        str(stitched_output),
        "-stitch_type",
        stitch_type,
        "-output_size",
        output_size,
        "-bitrate",
        str(bitrate),
        "-camera_accessory_type",
        str(camera_accessory_type),
    ]
    if flowstate:
        command.append("-enable_flowstate")
    return command


def run_streamed(command: Sequence[str], cwd: Path, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse_chain(log_path, "MediaSDK log")
    logging.info("Running MediaSDK for %s", Path(command[0]).name)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n[{datetime.now().astimezone().isoformat()}] ")
        log_file.write("MediaSDK invocation (paths withheld from terminal output)\n")
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        logged = 0
        truncated = False
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            safe_line = _sanitize_output(_redact_command_paths(chunk, command, cwd), 4096)
            encoded_size = len(safe_line.encode("utf-8", errors="replace"))
            if logged + encoded_size <= MAX_STREAM_LOG_BYTES:
                print(safe_line, end="", flush=True)
                log_file.write(safe_line)
                log_file.flush()
                logged += encoded_size
            elif not truncated:
                print("\n[MediaSDK output truncated; conversion continues]", flush=True)
                log_file.write("\n[MediaSDK output truncated; process continued]\n")
                log_file.flush()
                truncated = True
        return_code = process.wait()
    if return_code != 0:
        raise PipelineError(
            f"MediaSDK failed with exit code {return_code}. See log: {log_path}"
        )


def _completed(
    command: Sequence[str],
    cwd: Path | None = None,
    timeout: int = PROBE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a short helper with bounded capture; never use this for MediaSDK."""
    process = subprocess.Popen(
        list(command),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()

    def drain(stream, destination: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = MAX_CAPTURE_BYTES + 1 - len(destination)
                if remaining > 0:
                    destination.extend(chunk[:remaining])
        finally:
            stream.close()

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=drain, args=(process.stdout, stdout_buffer), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr_buffer), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise PipelineError(
            f"Helper process timed out after {timeout} seconds: {Path(command[0]).name}"
        ) from exc
    finally:
        for thread in threads:
            thread.join(timeout=5)
    stdout = _sanitize_output(stdout_buffer.decode("utf-8", "replace"))
    stderr = _sanitize_output(stderr_buffer.decode("utf-8", "replace"))
    return subprocess.CompletedProcess(list(command), return_code, stdout, stderr)


def probe_video(path: Path, ffprobe: str = "ffprobe") -> dict[str, Any]:
    result = _completed(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height:format=duration,size",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise PipelineError(f"ffprobe rejected {path}: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"ffprobe returned invalid JSON for {path}") from exc


def inspect_spherical(path: Path, spatialmedia: Path, python: str = sys.executable) -> str:
    result = _completed(
        [python, str(spatialmedia), str(path)],
        cwd=spatialmedia.parent.parent,
        timeout=METADATA_TIMEOUT_SECONDS,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0 or "Error," in output:
        raise PipelineError(f"Spherical metadata inspection failed for {path}:\n{output.strip()}")
    return output


def verify_360(
    path: Path,
    spatialmedia: Path,
    ffprobe: str = "ffprobe",
    python: str = sys.executable,
) -> Verification:
    if not path.is_file() or path.stat().st_size == 0:
        raise PipelineError(f"Output file is missing or empty: {path}")
    probe = probe_video(path, ffprobe)
    video_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        raise PipelineError(f"No video stream found in {path}")
    stream = video_streams[0]
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    duration = float(probe.get("format", {}).get("duration", 0))
    if width <= 0 or height <= 0 or duration <= 0:
        raise PipelineError(f"Invalid dimensions or duration in {path}")
    if width != height * 2:
        raise PipelineError(f"Expected a 2:1 equirectangular frame, got {width}x{height}: {path}")

    metadata = inspect_spherical(path, spatialmedia, python)
    spherical = bool(re.search(r"Spherical\s*=\s*true", metadata, re.IGNORECASE))
    projection_match = re.search(r"ProjectionType\s*=\s*([^\r\n]+)", metadata, re.IGNORECASE)
    projection = projection_match.group(1).strip() if projection_match else ""
    if not spherical or projection.lower() != "equirectangular":
        raise PipelineError(f"Required 360 metadata is missing from {path}")
    return Verification(str(path.resolve()), width, height, duration, spherical, projection)


def inject_spherical(
    stitched_input: Path,
    final_output: Path,
    spatialmedia: Path,
    python: str = sys.executable,
) -> None:
    partial = final_output.with_name(final_output.stem + ".partial" + final_output.suffix)
    if partial.exists():
        _safe_unlink(partial, root=final_output.parent, missing_ok=True)
    result = _completed(
        [python, str(spatialmedia), "-i", "--stereo=none", str(stitched_input), str(partial)],
        cwd=spatialmedia.parent.parent,
        timeout=METADATA_TIMEOUT_SECONDS,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0 or "Error," in output or not partial.is_file():
        if partial.exists():
            _safe_unlink(partial, root=final_output.parent, missing_ok=True)
        raise PipelineError(f"360 metadata injection failed:\n{output.strip()}")
    _reject_reparse_chain(final_output, "Final output")
    if final_output.exists():
        raise PipelineError(f"Refusing to replace an existing output unexpectedly: {final_output.name}")
    if not partial.is_file() or _is_reparse_point(partial):
        raise PipelineError(f"Metadata helper did not create a regular output: {partial.name}")
    partial_identity = _file_identity(partial)
    try:
        os.link(partial, final_output)
    except FileExistsError as exc:
        raise PipelineError(f"Refusing to replace an output created concurrently: {final_output.name}") from exc
    except OSError as exc:
        raise PipelineError(f"Cannot publish metadata-injected output safely: {final_output.name}") from exc
    if _file_identity(final_output) != partial_identity:
        _safe_unlink(final_output, root=final_output.parent, missing_ok=True)
        raise PipelineError(f"Published output identity check failed: {final_output.name}")
    _safe_unlink(
        partial,
        root=final_output.parent,
        expected_fingerprint=UploadLedger.fingerprint(partial),
    )


def convert_pair(
    pair: InsvPair,
    output_dir: Path,
    sdk_executable: Path,
    spatialmedia: Path,
    output_size: str,
    bitrate: int,
    stitch_type: str,
    flowstate: bool,
    camera_accessory_type: int,
    keep_stitched: bool,
    force: bool,
    ffprobe: str,
    adopt_existing: bool = False,
) -> Verification:
    output_dir.mkdir(parents=True, exist_ok=True)
    _reject_reparse_chain(output_dir, "Output folder")
    final_output = output_dir / f"{pair.output_stem}.mp4"
    stitched_output = output_dir / f"{pair.output_stem}.stitched.mp4"
    log_path = output_dir / "logs" / f"{pair.output_stem}.log"
    manifest_path = output_dir / f"{pair.output_stem}.conversion.json"
    for artifact in (final_output, stitched_output, manifest_path, log_path):
        _reject_reparse_chain(artifact, "Conversion artifact")
        if artifact.exists() and not artifact.is_file():
            raise PipelineError(f"Conversion artifact path is not a regular file: {artifact.name}")
    identity = _conversion_identity(
        pair, output_size, bitrate, stitch_type, flowstate, camera_accessory_type
    )
    expected_manifest = {
        "version": 1,
        "output": final_output.name,
        **identity,
    }

    artifacts_exist = any(path.exists() for path in (final_output, stitched_output))
    existing_manifest: dict[str, Any] | None = None
    if manifest_path.exists():
        existing_manifest = _load_conversion_manifest(manifest_path)
        comparable = {key: existing_manifest.get(key) for key in expected_manifest}
        if comparable != expected_manifest and not force:
            raise PipelineError(
                f"Output name collision or conversion settings changed for {final_output.name}. "
                "Choose another output folder, or use --force to rebuild this artifact."
            )
    elif artifacts_exist and not force and not (adopt_existing and final_output.exists()):
        raise PipelineError(
            f"Existing pre-hardening artifact has no trusted conversion manifest: {final_output.name}. "
            "Use --adopt-existing after verifying it, move it aside, or use --force to rebuild it."
        )

    if force:
        for stale in (final_output, stitched_output, manifest_path):
            if stale.exists():
                _safe_unlink(stale, root=output_dir)
    manifest = {**expected_manifest, "state": "converting", "updated_at": datetime.now().astimezone().isoformat()}

    if final_output.exists() and not force:
        logging.info("Verifying existing output: %s", final_output.name)
        verified = verify_360(final_output, spatialmedia, ffprobe)
        current_fingerprint = UploadLedger.fingerprint(final_output)
        current_identity = _full_file_identity(final_output)
        if existing_manifest is None:
            if not adopt_existing:
                raise PipelineError(f"Use --adopt-existing to trust verified output: {final_output.name}")
        elif (
            existing_manifest.get("artifact_fingerprint") != current_fingerprint
            and not adopt_existing
        ):
            raise PipelineError(
                f"Existing output changed since conversion: {final_output.name}; "
                "use --adopt-existing after verification or --force"
            )
        elif (
            existing_manifest.get("artifact_identity") not in (None, current_identity)
            and not adopt_existing
        ):
            raise PipelineError(
                f"Existing output content changed since conversion: {final_output.name}; "
                "use --adopt-existing after verification or --force"
            )
        elif existing_manifest.get("artifact_identity") is None and not adopt_existing:
            raise PipelineError(
                f"Existing manifest predates strong content identity: {final_output.name}. "
                "Use --adopt-existing once after checking it, or --force to rebuild."
            )
        manifest.update(
            {
                "state": "complete",
                "artifact_fingerprint": current_fingerprint,
                "artifact_identity": current_identity,
                "updated_at": datetime.now().astimezone().isoformat(),
            }
        )
        _atomic_json_write(manifest_path, manifest)
        if stitched_output.exists() and not keep_stitched:
            _safe_unlink(
                stitched_output,
                root=output_dir,
                expected_fingerprint=UploadLedger.fingerprint(stitched_output),
                expected_identity=_full_file_identity(stitched_output),
            )
        return verified
    _atomic_json_write(manifest_path, manifest)

    reuse_stitched = False
    if stitched_output.exists():
        logging.info("Reusing interrupted stitched intermediate: %s", stitched_output.name)
        stitched_fingerprint = UploadLedger.fingerprint(stitched_output)
        stitched_identity = _full_file_identity(stitched_output)
        stored_stitched_fingerprint = (
            existing_manifest.get("stitched_fingerprint") if existing_manifest else None
        )
        stored_stitched_identity = (
            existing_manifest.get("stitched_identity") if existing_manifest else None
        )
        binding_present = bool(stored_stitched_fingerprint and stored_stitched_identity)
        binding_matches = (
            binding_present
            and stored_stitched_fingerprint == stitched_fingerprint
            and stored_stitched_identity == stitched_identity
        )
        if binding_present and not binding_matches:
            logging.warning("Discarding replaced derived intermediate and rebuilding: %s", stitched_output.name)
            _safe_unlink(
                stitched_output,
                root=output_dir,
                expected_fingerprint=stitched_fingerprint,
                expected_identity=stitched_identity,
            )
        else:
            try:
                probe_video(stitched_output, ffprobe)
            except PipelineError:
                logging.warning("Discarding corrupt derived intermediate and rebuilding: %s", stitched_output.name)
                _safe_unlink(
                    stitched_output,
                    root=output_dir,
                    expected_fingerprint=stitched_fingerprint,
                    expected_identity=stitched_identity,
                )
            else:
                if not binding_present:
                    raise PipelineError(
                        f"Valid stitched intermediate predates identity binding: {stitched_output.name}. "
                        "Use --force to rebuild it safely."
                    )
                reuse_stitched = True
    if not reuse_stitched:
        command = build_stitch_command(
            sdk_executable,
            pair,
            stitched_output,
            output_size,
            bitrate,
            stitch_type,
            flowstate,
            camera_accessory_type,
        )
        run_streamed(command, sdk_executable.parent, log_path)
        probe_video(stitched_output, ffprobe)
    manifest.update(
        {
            "state": "stitched",
            "stitched_fingerprint": UploadLedger.fingerprint(stitched_output),
            "stitched_identity": _full_file_identity(stitched_output),
            "updated_at": datetime.now().astimezone().isoformat(),
        }
    )
    _atomic_json_write(manifest_path, manifest)
    inject_spherical(stitched_output, final_output, spatialmedia)
    verified = verify_360(final_output, spatialmedia, ffprobe)
    manifest.update(
        {
            "state": "complete",
            "artifact_fingerprint": UploadLedger.fingerprint(final_output),
            "artifact_identity": _full_file_identity(final_output),
            "updated_at": datetime.now().astimezone().isoformat(),
        }
    )
    _atomic_json_write(manifest_path, manifest)
    if not keep_stitched:
        _safe_unlink(stitched_output, root=output_dir, missing_ok=True)
    return verified


class UploadLedger:
    """Channel-bound, process-safe upload receipts and quota reservations."""

    def __init__(self, path: Path, daily_limit: int, channel_id: str | None = None) -> None:
        if daily_limit < 1:
            raise PipelineError("Daily upload limit must be at least 1")
        self.path = path.expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.daily_limit = daily_limit
        self.channel_id = channel_id
        self.data = self._load()
        if channel_id:
            self.bind_channel(channel_id)

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return isinstance(value, str) and bool(SAFE_ID.fullmatch(value))

    @classmethod
    def _validate(cls, data: Any, path: Path) -> dict[str, Any]:
        if not isinstance(data, dict) or data.get("version") != 1:
            raise PipelineError(f"Unsupported upload ledger format: {path.name}")
        uploads = data.get("uploads")
        attempts = data.setdefault("attempts", {})
        if not isinstance(uploads, dict) or not isinstance(attempts, dict):
            raise PipelineError(f"Invalid upload ledger records: {path.name}")
        if len(uploads) + len(attempts) > MAX_LEDGER_RECORDS:
            raise PipelineError(f"Upload ledger contains too many records: {path.name}")

        channels: set[str] = set()
        for fingerprint, record in uploads.items():
            if not isinstance(fingerprint, str) or not HEX_FINGERPRINT.fullmatch(fingerprint):
                raise PipelineError(f"Invalid artifact fingerprint in ledger: {path.name}")
            if not isinstance(record, dict):
                raise PipelineError(f"Invalid upload record in ledger: {path.name}")
            channel = record.get("channel_id")
            video = record.get("video_id")
            if not cls._valid_id(channel) or not cls._valid_id(video):
                raise PipelineError(f"Invalid channel or video ID in ledger: {path.name}")
            channels.add(channel)
            source = record.get("source_fingerprint")
            if source is not None and not (
                isinstance(source, str) and (HEX_FINGERPRINT.fullmatch(source) or SAFE_ID.fullmatch(source))
            ):
                raise PipelineError(f"Invalid source fingerprint in ledger: {path.name}")
            for field in ("artifact_identity", "source_identity"):
                value = record.get(field)
                if value is not None and not (
                    isinstance(value, str) and HEX_FINGERPRINT.fullmatch(value)
                ):
                    raise PipelineError(f"Invalid strong identity in ledger: {path.name}")
            stored_path = record.get("path")
            if not isinstance(stored_path, str) or len(stored_path) > 4096 or any(ord(c) < 32 for c in stored_path):
                raise PipelineError(f"Invalid artifact path in ledger: {path.name}")
            playlists = record.get("playlists", {})
            if not isinstance(playlists, dict) or any(not cls._valid_id(item) for item in playlists):
                raise PipelineError(f"Invalid playlist state in ledger: {path.name}")

        for attempt_id, attempt in attempts.items():
            if not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
                raise PipelineError(f"Invalid quota reservation in ledger: {path.name}")
            if not isinstance(attempt, dict) or attempt.get("status") not in {"pending", "succeeded", "failed"}:
                raise PipelineError(f"Invalid quota reservation state in ledger: {path.name}")
            channel = attempt.get("channel_id")
            if not cls._valid_id(channel):
                raise PipelineError(f"Invalid reservation channel in ledger: {path.name}")
            channels.add(channel)
            if not HEX_FINGERPRINT.fullmatch(str(attempt.get("artifact_fingerprint", ""))):
                raise PipelineError(f"Invalid reserved artifact in ledger: {path.name}")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(attempt.get("quota_day", ""))):
                raise PipelineError(f"Invalid reservation quota day in ledger: {path.name}")
            source = attempt.get("source_fingerprint")
            if source is not None and not (
                isinstance(source, str) and (HEX_FINGERPRINT.fullmatch(source) or SAFE_ID.fullmatch(source))
            ):
                raise PipelineError(f"Invalid reserved source in ledger: {path.name}")
            for field in ("artifact_identity", "source_identity"):
                value = attempt.get(field)
                if value is not None and not (
                    isinstance(value, str) and HEX_FINGERPRINT.fullmatch(value)
                ):
                    raise PipelineError(f"Invalid reservation identity in ledger: {path.name}")
            owner = attempt.get("owner")
            if owner is not None and not isinstance(owner, dict):
                raise PipelineError(f"Invalid reservation owner in ledger: {path.name}")
            if isinstance(owner, dict):
                for field in ("host", "boot_id", "process_start_id"):
                    value = owner.get(field)
                    if value is not None and (
                        not isinstance(value, str)
                        or len(value) > 256
                        or any(ord(c) < 32 for c in value)
                    ):
                        raise PipelineError(f"Invalid reservation owner identity: {path.name}")
                if not isinstance(owner.get("pid"), int) or owner["pid"] < 1:
                    raise PipelineError(f"Invalid reservation owner process: {path.name}")
            if attempt.get("attempt_id") not in (None, attempt_id):
                raise PipelineError(f"Mismatched reservation ID in ledger: {path.name}")
            stored_path = attempt.get("path")
            if stored_path is not None and (
                not isinstance(stored_path, str)
                or len(stored_path) > 4096
                or any(ord(c) < 32 for c in stored_path)
            ):
                raise PipelineError(f"Invalid reservation path in ledger: {path.name}")
            playlist_id = attempt.get("playlist_id")
            if playlist_id is not None and not cls._valid_id(playlist_id):
                raise PipelineError(f"Invalid reserved playlist in ledger: {path.name}")

        for fingerprint, record in uploads.items():
            attempt_id = record.get("attempt_id")
            if attempt_id is None:
                continue
            attempt = attempts.get(attempt_id)
            if (
                not attempt
                or attempt.get("status") != "succeeded"
                or attempt.get("artifact_fingerprint") != fingerprint
                or attempt.get("channel_id") != record.get("channel_id")
            ):
                raise PipelineError(f"Upload receipt has no matching quota reservation: {path.name}")

        top_channel = data.get("channel_id")
        if top_channel is not None:
            if not cls._valid_id(top_channel):
                raise PipelineError(f"Invalid ledger channel binding: {path.name}")
            channels.add(top_channel)
        if len(channels) > 1:
            raise PipelineError(f"Ledger mixes records from different YouTube channels: {path.name}")
        if channels:
            data["channel_id"] = next(iter(channels))
        return data

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            data: dict[str, Any] = {"version": 1, "uploads": {}, "attempts": {}}
            if self.channel_id:
                data["channel_id"] = self.channel_id
            return data
        try:
            if self.path.stat().st_size > MAX_LEDGER_BYTES:
                raise PipelineError(f"Upload ledger is unexpectedly large: {self.path.name}")
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError(f"Cannot read upload ledger: {self.path.name}") from exc
        return self._validate(data, self.path)

    def _save(self) -> None:
        _atomic_json_write(self.path, self.data, private=True)

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                self.data = self._load()
                yield
            finally:
                if os.name == "nt":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _bind_loaded(self, channel_id: str) -> None:
        if not self._valid_id(channel_id):
            raise PipelineError("Invalid authenticated YouTube channel ID")
        existing = self.data.get("channel_id")
        if existing and existing != channel_id:
            raise PipelineError("Upload ledger belongs to a different YouTube channel")
        self.data["channel_id"] = channel_id
        self.channel_id = channel_id

    def bind_channel(self, channel_id: str) -> None:
        with self._locked():
            self._bind_loaded(channel_id)
            self._save()

    @staticmethod
    def fingerprint(path: Path) -> str:
        stat = path.stat()
        value = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def today(when: datetime | None = None) -> str:
        """Return YouTube's quota calendar day (midnight America/Los_Angeles)."""
        instant = when or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return instant.astimezone(YOUTUBE_QUOTA_TIMEZONE).date().isoformat()

    @classmethod
    def _record_quota_day(cls, item: dict[str, Any]) -> str:
        if item.get("quota_day"):
            return str(item["quota_day"])
        uploaded_at = item.get("uploaded_at")
        if uploaded_at:
            try:
                return cls.today(datetime.fromisoformat(uploaded_at))
            except (TypeError, ValueError):
                pass
        # Compatibility with ledgers written before quota-day tracking existed.
        return str(item.get("local_date", ""))

    def _count_today_loaded(self) -> int:
        today = self.today()
        attempts = sum(
            1 for item in self.data["attempts"].values() if item.get("quota_day") == today
        )
        migrated_uploads = sum(
            1
            for item in self.data["uploads"].values()
            if not item.get("attempt_id") and self._record_quota_day(item) == today
        )
        return attempts + migrated_uploads

    def count_today(self) -> int:
        with self._locked():
            return self._count_today_loaded()

    def uploaded_record(
        self,
        path: Path,
        source_fingerprint: str | None = None,
        source_identity: str | None = None,
        artifact_identity: str | None = None,
    ) -> dict[str, Any] | None:
        if artifact_identity is not None and not HEX_FINGERPRINT.fullmatch(artifact_identity):
            raise PipelineError("Invalid full artifact identity")
        fingerprint = self.fingerprint(path)
        artifact_identity = artifact_identity or _full_file_identity(path)
        with self._locked():
            record = self.data["uploads"].get(fingerprint)
            if record and record.get("artifact_identity") not in (None, artifact_identity):
                record = None
            if record is None:
                record = next(
                    (
                        item
                        for item in self.data["uploads"].values()
                        if item.get("artifact_identity") == artifact_identity
                    ),
                    None,
                )
            if not record or (self.channel_id and record.get("channel_id") != self.channel_id):
                return None
            if source_fingerprint is not None and record.get("source_fingerprint") != source_fingerprint:
                return None
            if record.get("source_identity") is not None and (
                source_identity is None or record.get("source_identity") != source_identity
            ):
                return None
            result = dict(record)
            result["_artifact_fingerprint"] = fingerprint
            result["_artifact_identity"] = artifact_identity
            result["_cleanup_authorized"] = record.get("artifact_identity") == artifact_identity
            return result

    def already_uploaded(self, path: Path) -> bool:
        return self.uploaded_record(path) is not None

    def source_uploaded(
        self,
        source_fingerprint: str,
        source_identity: str | None = None,
    ) -> bool:
        with self._locked():
            return any(
                item.get("source_fingerprint") == source_fingerprint
                and (
                    item.get("source_identity") is None
                    or (source_identity is not None and item.get("source_identity") == source_identity)
                )
                and (not self.channel_id or item.get("channel_id") == self.channel_id)
                for item in self.data["uploads"].values()
            )

    def require_capacity(self) -> None:
        with self._locked():
            if self._count_today_loaded() >= self.daily_limit:
                raise PipelineError(
                    f"YouTube daily upload cap reached ({self.daily_limit}, Pacific quota day); "
                    "no upload was attempted."
                )

    def reserve_attempt(
        self,
        path: Path,
        channel_id: str,
        source_fingerprint: str | None = None,
        source_identity: str | None = None,
        playlist_id: str | None = None,
        artifact_identity: str | None = None,
    ) -> str:
        if source_identity is not None and not HEX_FINGERPRINT.fullmatch(source_identity):
            raise PipelineError("Invalid strong source identity")
        if playlist_id is not None and not self._valid_id(playlist_id):
            raise PipelineError("Invalid reserved playlist ID")
        if artifact_identity is not None and not HEX_FINGERPRINT.fullmatch(artifact_identity):
            raise PipelineError("Invalid full artifact identity")
        fingerprint = self.fingerprint(path)
        artifact_identity = artifact_identity or _full_file_identity(path)
        with self._locked():
            self._bind_loaded(channel_id)
            if any(
                item.get("artifact_identity") == artifact_identity
                or (
                    item.get("artifact_identity") is None
                    and key == fingerprint
                )
                for key, item in self.data["uploads"].items()
            ):
                raise PipelineError(f"Artifact is already recorded as uploaded: {path.name}")
            for attempt in self.data["attempts"].values():
                artifact_matches = (
                    attempt.get("artifact_identity") == artifact_identity
                    if attempt.get("artifact_identity")
                    else attempt.get("artifact_fingerprint") == fingerprint
                )
                source_matches = bool(
                    source_fingerprint
                    and attempt.get("source_fingerprint") == source_fingerprint
                    and (
                        not attempt.get("source_identity")
                        or (
                            source_identity is not None
                            and attempt.get("source_identity") == source_identity
                        )
                    )
                )
                if not (artifact_matches or source_matches):
                    continue
                if attempt.get("status") == "succeeded":
                    raise PipelineError(f"An upload is already reserved for this artifact: {path.name}")
                if attempt.get("status") == "pending":
                    owner_alive = _attempt_owner_alive(attempt.get("owner"))
                    state = "active" if owner_alive is True else "interrupted or unverified"
                    raise PipelineError(
                        f"An {state} upload reservation ({attempt.get('attempt_id', 'see ledger')}) "
                        f"already owns {path.name}. Inspect YouTube, then use attempt-resolve; "
                        "automatic retry is disabled to prevent duplicate uploads."
                    )
            if self._count_today_loaded() >= self.daily_limit:
                raise PipelineError(
                    f"YouTube daily upload cap reached ({self.daily_limit}, Pacific quota day); "
                    "no upload was attempted."
                )
            attempt_id = uuid.uuid4().hex
            self.data["attempts"][attempt_id] = {
                "attempt_id": attempt_id,
                "artifact_fingerprint": fingerprint,
                "artifact_identity": artifact_identity,
                "path": str(path.resolve()),
                "channel_id": channel_id,
                "quota_day": self.today(),
                "status": "pending",
                "created_at": datetime.now().astimezone().isoformat(),
                "owner": _current_attempt_owner(),
            }
            if source_fingerprint:
                self.data["attempts"][attempt_id]["source_fingerprint"] = source_fingerprint
            if source_identity:
                self.data["attempts"][attempt_id]["source_identity"] = source_identity
            if playlist_id:
                self.data["attempts"][attempt_id]["playlist_id"] = playlist_id
            self._save()
            return attempt_id

    def mark_attempt_failed(self, attempt_id: str, reason: str = "upload failed") -> None:
        with self._locked():
            attempt = self.data["attempts"].get(attempt_id)
            if not attempt:
                raise PipelineError("Cannot update missing upload reservation")
            if attempt.get("status") != "pending":
                raise PipelineError("Only a pending upload reservation can be marked failed")
            attempt["status"] = "failed"
            attempt["finished_at"] = datetime.now().astimezone().isoformat()
            attempt["reason"] = _sanitize_output(reason, 512)
            self._save()

    def pending_attempts(self) -> list[dict[str, Any]]:
        with self._locked():
            return [
                {
                    "attempt_id": attempt_id,
                    "artifact": Path(str(item.get("path", "unknown"))).name,
                    "quota_day": item.get("quota_day"),
                    "owner_alive": _attempt_owner_alive(item.get("owner")),
                }
                for attempt_id, item in self.data["attempts"].items()
                if item.get("status") == "pending"
            ]

    def resolve_attempt(
        self,
        attempt_id: str,
        *,
        confirmed_failed: bool = False,
        video_id: str | None = None,
        video_ownership_verified: bool = False,
    ) -> None:
        """Resolve an ambiguous crash only after the owner inspects YouTube."""
        if not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
            raise PipelineError("Invalid upload reservation ID")
        if confirmed_failed == bool(video_id):
            raise PipelineError("Choose exactly one resolution: --confirmed-failed or --video-id")
        with self._locked():
            attempt = self.data["attempts"].get(attempt_id)
            if not attempt or attempt.get("status") != "pending":
                raise PipelineError("Upload reservation is missing or no longer pending")
            if _attempt_owner_alive(attempt.get("owner")) is True:
                raise PipelineError("Refusing to resolve a reservation owned by a running process")
            now = datetime.now().astimezone().isoformat()
            if confirmed_failed:
                attempt["status"] = "failed"
                attempt["finished_at"] = now
                attempt["reason"] = "owner confirmed no YouTube upload exists"
                self._save()
                return
            if not self._valid_id(video_id):
                raise PipelineError("Confirmed YouTube video ID is invalid")
            if not video_ownership_verified:
                raise PipelineError("Confirmed video ownership must be verified through YouTube")
            stored_path = attempt.get("path")
            artifact_identity = attempt.get("artifact_identity")
            if not isinstance(stored_path, str) or not HEX_FINGERPRINT.fullmatch(
                str(artifact_identity or "")
            ):
                raise PipelineError(
                    "This legacy reservation cannot be adopted as succeeded; mark it confirmed failed instead"
                )
            record: dict[str, Any] = {
                "path": stored_path,
                "local_date": datetime.now().astimezone().date().isoformat(),
                "quota_day": attempt["quota_day"],
                "video_id": video_id,
                "channel_id": attempt["channel_id"],
                "uploaded_at": now,
                "source_fingerprint": attempt.get("source_fingerprint"),
                "artifact_identity": artifact_identity,
                "attempt_id": attempt_id,
            }
            if attempt.get("source_identity"):
                record["source_identity"] = attempt["source_identity"]
            if attempt.get("playlist_id"):
                record["playlists"] = {
                    attempt["playlist_id"]: {"status": "pending", "updated_at": now}
                }
            attempt["status"] = "succeeded"
            attempt["finished_at"] = now
            attempt["reason"] = "owner confirmed existing YouTube video"
            self.data["uploads"][attempt["artifact_fingerprint"]] = record
            self._save()

    def record(
        self,
        path: Path,
        video_id: str,
        channel_id: str,
        source_fingerprint: str | None = None,
        playlist_id: str | None = None,
        attempt_id: str | None = None,
        source_identity: str | None = None,
        artifact_identity: str | None = None,
    ) -> None:
        if not self._valid_id(video_id) or not self._valid_id(channel_id):
            raise PipelineError("YouTube returned an invalid video or channel ID")
        if playlist_id is not None and not self._valid_id(playlist_id):
            raise PipelineError("Invalid playlist ID")
        if source_identity is not None and not HEX_FINGERPRINT.fullmatch(source_identity):
            raise PipelineError("Invalid strong source identity")
        if artifact_identity is not None and not HEX_FINGERPRINT.fullmatch(artifact_identity):
            raise PipelineError("Invalid full artifact identity")
        artifact_fingerprint = self.fingerprint(path)
        artifact_identity = artifact_identity or _full_file_identity(path)
        record = {
            "path": str(path.resolve()),
            "local_date": datetime.now().astimezone().date().isoformat(),
            "quota_day": self.today(),
            "video_id": video_id,
            "channel_id": channel_id,
            "uploaded_at": datetime.now().astimezone().isoformat(),
            "source_fingerprint": source_fingerprint,
            "artifact_identity": artifact_identity,
        }
        if source_identity:
            record["source_identity"] = source_identity
        if attempt_id:
            record["attempt_id"] = attempt_id
        if playlist_id:
            record["playlists"] = {
                playlist_id: {"status": "pending", "updated_at": datetime.now().astimezone().isoformat()}
            }
        with self._locked():
            self._bind_loaded(channel_id)
            if attempt_id:
                attempt = self.data["attempts"].get(attempt_id)
                if (
                    not attempt
                    or attempt.get("channel_id") != channel_id
                    or attempt.get("artifact_fingerprint") != artifact_fingerprint
                    or attempt.get("artifact_identity") != artifact_identity
                ):
                    raise PipelineError("Upload reservation does not match the uploaded artifact")
                attempt["status"] = "succeeded"
                attempt["finished_at"] = datetime.now().astimezone().isoformat()
            self.data["uploads"][artifact_fingerprint] = record
            self._save()

    def assign_all_to_playlist(self, playlist_id: str) -> int:
        if not self._valid_id(playlist_id):
            raise PipelineError("Invalid playlist ID")
        changed = 0
        now = datetime.now().astimezone().isoformat()
        with self._locked():
            for record in self.data["uploads"].values():
                if self.channel_id and record.get("channel_id") != self.channel_id:
                    continue
                playlists = record.setdefault("playlists", {})
                if playlist_id not in playlists:
                    playlists[playlist_id] = {"status": "pending", "updated_at": now}
                    changed += 1
            if changed:
                self._save()
        return changed

    def pending_playlist_entries(self, playlist_id: str) -> list[dict[str, Any]]:
        with self._locked():
            return sorted(
                (
                    dict(record)
                    for record in self.data["uploads"].values()
                    if record.get("video_id")
                    and (not self.channel_id or record.get("channel_id") == self.channel_id)
                    and record.get("playlists", {}).get(playlist_id, {}).get("status") != "added"
                ),
                key=lambda record: str(record.get("uploaded_at", "")),
            )

    def mark_playlist_added(self, video_id: str, playlist_id: str, playlist_item_id: str) -> None:
        if not all(self._valid_id(item) for item in (video_id, playlist_id, playlist_item_id)):
            raise PipelineError("Invalid YouTube playlist receipt")
        with self._locked():
            for record in self.data["uploads"].values():
                if record.get("video_id") == video_id and (
                    not self.channel_id or record.get("channel_id") == self.channel_id
                ):
                    record.setdefault("playlists", {})[playlist_id] = {
                        "status": "added",
                        "playlist_item_id": playlist_item_id,
                        "updated_at": datetime.now().astimezone().isoformat(),
                    }
                    self._save()
                    return
        raise PipelineError(f"Cannot mark playlist membership; video is missing from ledger: {video_id}")


def _credentials_cover(credentials, required_scopes: Sequence[str]) -> bool:
    granted = set(credentials.scopes or [])
    return YOUTUBE_BROAD_SCOPE in granted or set(required_scopes).issubset(granted)


def load_youtube_service(
    token_path: Path,
    client_secret: Path | None = None,
    login_hint: str = "",
    scopes: Sequence[str] | None = None,
):
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise PipelineError("Install requirements-local.txt before using YouTube commands") from exc

    required_scopes = list(scopes or SCOPES)
    credentials = None
    if token_path.is_file():
        credentials = Credentials.from_authorized_user_file(str(token_path))
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            if not client_secret:
                raise PipelineError(
                    "OAuth refresh failed (the old client may have been deleted). "
                    "Run again with --client-secret to authorize the target account."
                ) from exc
            credentials = None
    if credentials and credentials.valid and not _credentials_cover(credentials, required_scopes):
        if not client_secret:
            raise PipelineError(
                "OAuth token lacks the scope required for this operation; provide --client-secret "
                "once to authorize the additional least-privilege scope."
            )
        credentials = None
    if not credentials or not credentials.valid:
        if not client_secret or not client_secret.is_file():
            raise PipelineError("OAuth token is invalid; provide --client-secret to sign in again")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), required_scopes)
        auth_options = {"access_type": "offline", "prompt": "consent"}
        if login_hint:
            auth_options["login_hint"] = login_hint
        credentials = flow.run_local_server(port=0, **auth_options)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token_payload = json.loads(credentials.to_json())
    except json.JSONDecodeError as exc:
        raise PipelineError("OAuth library returned invalid token data") from exc
    _atomic_json_write(token_path, token_payload, private=True)
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def require_channel(youtube, expected_channel_id: str) -> dict[str, str]:
    if not UploadLedger._valid_id(expected_channel_id):
        raise PipelineError("Configured YouTube channel ID is invalid")
    try:
        response = youtube.channels().list(part="id,snippet", mine=True, maxResults=1).execute()
    except Exception as exc:
        raise PipelineError(f"Cannot inspect the authenticated YouTube channel: {_safe_label(exc, 512)}") from exc
    items = response.get("items", [])
    if not items:
        raise PipelineError("OAuth account has no accessible YouTube channel")
    try:
        channel = {"id": str(items[0]["id"]), "title": _safe_label(items[0]["snippet"]["title"])}
    except (KeyError, TypeError) as exc:
        raise PipelineError("YouTube returned an invalid channel response") from exc
    if not UploadLedger._valid_id(channel["id"]):
        raise PipelineError("YouTube returned an invalid channel ID")
    if expected_channel_id and channel["id"] != expected_channel_id:
        raise PipelineError("OAuth token does not belong to the configured YouTube channel")
    return channel


def require_video_owner(youtube, video_id: str, channel_id: str) -> None:
    if not UploadLedger._valid_id(video_id) or not UploadLedger._valid_id(channel_id):
        raise PipelineError("Invalid video or channel ID")
    try:
        response = youtube.videos().list(
            part="id,snippet", id=video_id, maxResults=1
        ).execute()
    except Exception as exc:
        raise PipelineError("Cannot validate YouTube video ownership") from exc
    items = response.get("items", [])
    if not items or items[0].get("snippet", {}).get("channelId") != channel_id:
        raise PipelineError("Video is not owned by the authenticated YouTube channel")


class PlaylistManager:
    """Validate and idempotently add videos to one channel-owned playlist."""

    def __init__(self, youtube, playlist_id: str, channel_id: str) -> None:
        if not UploadLedger._valid_id(playlist_id) or not UploadLedger._valid_id(channel_id):
            raise PipelineError("Configured playlist or channel ID is invalid")
        self.youtube = youtube
        self.playlist_id = playlist_id
        self.channel_id = channel_id
        try:
            response = youtube.playlists().list(
                part="id,snippet", id=playlist_id, maxResults=1
            ).execute()
        except Exception as exc:
            raise PipelineError(f"Cannot inspect the configured YouTube playlist: {_safe_label(exc, 512)}") from exc
        items = response.get("items", [])
        if not items:
            raise PipelineError(f"YouTube playlist was not found: {playlist_id}")
        snippet = items[0].get("snippet", {})
        if snippet.get("channelId") != channel_id:
            raise PipelineError("Configured playlist is not owned by the authenticated channel")
        self.title = _safe_label(snippet.get("title", ""))
        self.items: dict[str, str] = {}
        page_token = None
        while True:
            try:
                page = youtube.playlistItems().list(
                    part="id,contentDetails",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=page_token,
                ).execute()
            except Exception as exc:
                raise PipelineError(f"Cannot list the configured YouTube playlist: {_safe_label(exc, 512)}") from exc
            for item in page.get("items", []):
                video_id = item.get("contentDetails", {}).get("videoId")
                if video_id:
                    self.items[str(video_id)] = str(item.get("id", ""))
            page_token = page.get("nextPageToken")
            if not page_token:
                break

    def _require_owned_video(self, video_id: str) -> None:
        require_video_owner(self.youtube, video_id, self.channel_id)

    def ensure(self, video_id: str, validate_owner: bool = False) -> tuple[str, bool]:
        if validate_owner:
            self._require_owned_video(video_id)
        if video_id in self.items:
            return self.items[video_id], False
        try:
            response = self.youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": self.playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            ).execute()
        except Exception as exc:
            raise PipelineError(
                f"Cannot add video to the configured playlist: {_safe_label(exc, 512)}"
            ) from exc
        item_id = str(response.get("id", ""))
        if not item_id:
            raise PipelineError(f"YouTube returned no playlist item ID for video {video_id}")
        self.items[video_id] = item_id
        return item_id, True


def sync_ledger_playlist(ledger: UploadLedger, manager: PlaylistManager) -> tuple[int, int]:
    ledger.assign_all_to_playlist(manager.playlist_id)
    added = 0
    already_present = 0
    for record in ledger.pending_playlist_entries(manager.playlist_id):
        video_id = str(record["video_id"])
        item_id, inserted = manager.ensure(video_id, validate_owner=True)
        ledger.mark_playlist_added(video_id, manager.playlist_id, item_id)
        if inserted:
            added += 1
            logging.info("Added https://youtu.be/%s to playlist %s", video_id, manager.title)
        else:
            already_present += 1
            logging.info("Playlist already contains https://youtu.be/%s", video_id)
    return added, already_present


def build_upload_body(
    path: Path,
    privacy: str,
    description: str = "",
    made_for_kids: bool = False,
) -> dict[str, Any]:
    return {
        "snippet": {"title": path.stem, "description": description},
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }


def upload_video(
    youtube,
    path: Path,
    privacy: str,
    description: str = "",
    made_for_kids: bool = False,
) -> str:
    try:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise DefinitiveUploadError("Install requirements-local.txt before uploading") from exc

    transient_errors: tuple[type[BaseException], ...] = (
        OSError,
        TimeoutError,
        socket.timeout,
        http.client.NotConnected,
        http.client.IncompleteRead,
        http.client.ImproperConnectionState,
        http.client.CannotSendRequest,
        http.client.CannotSendHeader,
        http.client.ResponseNotReady,
        http.client.BadStatusLine,
    )
    try:
        import httplib2

        transient_errors = (*transient_errors, httplib2.HttpLib2Error)
    except ImportError:
        pass

    request = youtube.videos().insert(
        part="snippet,status",
        body=build_upload_body(path, privacy, description, made_for_kids),
        media_body=MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True),
        notifySubscribers=False,
    )
    response = None
    retries = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"Upload {path.name}: {status.progress() * 100:.1f}%")
        except HttpError as exc:
            body = exc.content.decode("utf-8", errors="replace") if exc.content else ""
            if 400 <= exc.resp.status < 500:
                raise DefinitiveUploadError(
                    f"YouTube rejected upload: HTTP {exc.resp.status}: {_safe_label(body, 2048)}"
                ) from exc
            retries += 1
            if retries > 5:
                raise PipelineError("YouTube upload failed after 5 retries") from exc
            delay = random.uniform(0, 2**retries)
            logging.warning("Transient upload error; retrying in %.1f seconds", delay)
            time.sleep(delay)
        except transient_errors as exc:
            retries += 1
            if retries > 5:
                raise PipelineError(
                    "YouTube resumable upload transport failed after 5 retries; "
                    "the attempt remains pending for owner review"
                ) from exc
            delay = random.uniform(0, 2**retries)
            logging.warning(
                "Transient upload transport error; resuming the same session in %.1f seconds",
                delay,
            )
            time.sleep(delay)
    video_id = response.get("id")
    if not video_id:
        raise PipelineError(f"YouTube upload returned no video ID for {path}")
    return video_id


def upload_verified_file(
    youtube,
    channel_id: str,
    path: Path,
    ledger: UploadLedger,
    spatialmedia: Path,
    ffprobe: str,
    privacy: str,
    made_for_kids: bool,
    source_fingerprint: str | None = None,
    delete_after_success: bool = False,
    playlist_manager: PlaylistManager | None = None,
    source_identity: str | None = None,
) -> str | None:
    """Verify, upload, persist success, then optionally remove the local MP4."""
    artifact_fingerprint = UploadLedger.fingerprint(path)
    artifact_identity = _full_file_identity(path)
    uploaded_record = (
        ledger.uploaded_record(path, source_fingerprint, source_identity, artifact_identity)
        if hasattr(ledger, "uploaded_record")
        else ({} if ledger.already_uploaded(path) else None)
    )
    if uploaded_record is not None:
        logging.info("Already uploaded; skipping: %s", path.name)
        if delete_after_success and _record_cleanup_ready(uploaded_record):
            _safe_unlink(
                path,
                root=path.parent,
                expected_fingerprint=str(uploaded_record["_artifact_fingerprint"]),
                expected_identity=str(uploaded_record["_artifact_identity"]),
                missing_ok=True,
            )
            logging.info("Removed previously uploaded local MP4: %s", path.name)
        elif delete_after_success:
            logging.info(
                "Kept local MP4 until every requested playlist is confirmed and a strong receipt exists: %s",
                path.name,
            )
        return None
    ledger.require_capacity()
    verify_360(path, spatialmedia, ffprobe)
    attempt_id = None
    if hasattr(ledger, "reserve_attempt"):
        attempt_id = ledger.reserve_attempt(
            path,
            channel_id,
            source_fingerprint,
            source_identity,
            playlist_manager.playlist_id if playlist_manager else None,
            artifact_identity,
        )
    try:
        video_id = upload_video(youtube, path, privacy, made_for_kids=made_for_kids)
    except DefinitiveUploadError as exc:
        if attempt_id and hasattr(ledger, "mark_attempt_failed"):
            ledger.mark_attempt_failed(attempt_id, type(exc).__name__)
        raise
    except Exception:
        # A transport/server failure may occur after YouTube accepted the final chunk.
        # Leave the reservation pending until the owner inspects the channel.
        raise
    record_options: dict[str, Any] = {}
    if attempt_id:
        record_options["attempt_id"] = attempt_id
    if source_identity:
        record_options["source_identity"] = source_identity
    if isinstance(ledger, UploadLedger):
        record_options["artifact_identity"] = artifact_identity
    if playlist_manager:
        ledger.record(
            path,
            video_id,
            channel_id,
            source_fingerprint,
            playlist_id=playlist_manager.playlist_id,
            **record_options,
        )
    else:
        ledger.record(path, video_id, channel_id, source_fingerprint, **record_options)
    print(f"Uploaded {path.name}: https://youtu.be/{video_id}")
    if playlist_manager:
        playlist_item_id, inserted = playlist_manager.ensure(video_id)
        ledger.mark_playlist_added(video_id, playlist_manager.playlist_id, playlist_item_id)
        action = "Added to" if inserted else "Already in"
        logging.info("%s playlist %s: https://youtu.be/%s", action, playlist_manager.title, video_id)
    cleanup_record = (
        ledger.uploaded_record(path, source_fingerprint, source_identity, artifact_identity)
        if hasattr(ledger, "uploaded_record")
        else None
    )
    if delete_after_success and (
        cleanup_record is None and not hasattr(ledger, "uploaded_record")
        or cleanup_record is not None and _record_cleanup_ready(cleanup_record)
    ):
        try:
            _safe_unlink(
                path,
                root=path.parent,
                expected_fingerprint=artifact_fingerprint,
                expected_identity=artifact_identity,
            )
        except OSError as exc:
            raise PipelineError(
                f"Upload succeeded ({video_id}), but the local MP4 could not be deleted: {path}"
            ) from exc
        logging.info("Upload recorded; removed local MP4: %s", path.name)
    elif delete_after_success:
        logging.info("Upload recorded; keeping MP4 until playlist/receipt cleanup is safe: %s", path.name)
    return video_id


def _default_spatialmedia() -> Path:
    return Path(__file__).resolve().parent.parent / "apps" / "spatial-media" / "spatialmedia"


def _add_youtube_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--token", type=Path, required=True, help="OAuth token JSON (never commit it)")
    parser.add_argument("--client-secret", type=Path, help="OAuth desktop client JSON, only needed to sign in")
    parser.add_argument("--login-hint", default="")
    parser.add_argument("--expected-channel-id", required=True)


def _add_conversion_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sdk-exe", type=Path, required=True)
    parser.add_argument("--spatialmedia", type=Path, default=_default_spatialmedia())
    parser.add_argument("--output-size", default="5760x2880")
    parser.add_argument("--bitrate", type=int, default=200_000_000)
    parser.add_argument(
        "--stitch-type",
        choices=("template", "optflow", "dynamicstitch", "aistitch"),
        default="optflow",
    )
    parser.add_argument("--no-flowstate", action="store_true")
    parser.add_argument(
        "--camera-accessory-type",
        type=int,
        default=-1,
        help="MediaSDK CameraAccessoryType enum; -1 auto-detects from INSV metadata (default)",
    )
    parser.add_argument("--keep-stitched", action="store_true")
    existing = parser.add_mutually_exclusive_group()
    existing.add_argument("--force", action="store_true", help="Rebuild and replace derived artifacts")
    existing.add_argument(
        "--adopt-existing",
        action="store_true",
        help="Explicitly verify and bind an existing final 360 MP4 without reconverting it",
    )
    parser.add_argument("--ffprobe", default="ffprobe")


def _add_upload_policy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"), default="unlisted")
    parser.add_argument(
        "--made-for-kids",
        action="store_true",
        help="Declare uploads as made for kids; default is explicitly not made for kids",
    )
    parser.add_argument("--daily-limit", type=int, default=DEFAULT_DAILY_UPLOAD_LIMIT)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--playlist-id", help="Channel-owned playlist to reconcile and add every upload to")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser("discover", help="Find complete `_00_`/`_10_` INSV pairs")
    discover.add_argument("source", type=Path)
    discover.add_argument("--no-recursive", action="store_true")
    discover.add_argument("--json", action="store_true")

    convert = commands.add_parser("convert", help="Stitch, inject metadata, and verify local MP4 files")
    convert.add_argument("source", type=Path)
    convert.add_argument("output", type=Path)
    _add_conversion_args(convert)
    convert.add_argument("--limit", type=int)
    convert.add_argument("--no-recursive", action="store_true")

    verify = commands.add_parser("verify", help="Validate video structure and spherical metadata")
    verify.add_argument("files", type=Path, nargs="+")
    verify.add_argument("--spatialmedia", type=Path, default=_default_spatialmedia())
    verify.add_argument("--ffprobe", default="ffprobe")

    attempts = commands.add_parser(
        "attempts", help="List ambiguous upload reservations that require owner review"
    )
    attempts.add_argument("--ledger", type=Path, required=True)
    attempts.add_argument("--expected-channel-id", required=True)

    attempt_resolve = commands.add_parser(
        "attempt-resolve", help="Resolve a crashed upload only after inspecting YouTube"
    )
    attempt_resolve.add_argument("--ledger", type=Path, required=True)
    attempt_resolve.add_argument("--expected-channel-id", required=True)
    attempt_resolve.add_argument(
        "--token",
        type=Path,
        help="OAuth token JSON; required with --video-id for ownership verification",
    )
    attempt_resolve.add_argument("--client-secret", type=Path)
    attempt_resolve.add_argument("--login-hint", default="")
    attempt_resolve.add_argument("--attempt-id", required=True)
    resolution = attempt_resolve.add_mutually_exclusive_group(required=True)
    resolution.add_argument(
        "--confirmed-failed",
        action="store_true",
        help="Confirm no video exists on YouTube and allow a new attempt",
    )
    resolution.add_argument(
        "--video-id",
        help="Record the YouTube video ID found after the interrupted process",
    )

    auth_check = commands.add_parser("auth-check", help="Refresh OAuth and prove the target channel")
    _add_youtube_auth_args(auth_check)

    playlist_sync = commands.add_parser(
        "playlist-sync", help="Idempotently add every video in an upload ledger to a playlist"
    )
    playlist_sync.add_argument("--ledger", type=Path, required=True)
    playlist_sync.add_argument("--playlist-id", required=True)
    _add_youtube_auth_args(playlist_sync)

    upload = commands.add_parser("upload", help="Upload verified MP4 files with a persistent daily cap")
    upload.add_argument("source", type=Path)
    _add_youtube_auth_args(upload)
    upload.add_argument("--spatialmedia", type=Path, default=_default_spatialmedia())
    upload.add_argument("--ffprobe", default="ffprobe")
    _add_upload_policy_args(upload)
    upload.add_argument("--max-count", type=int)
    upload.add_argument(
        "--delete-after-upload",
        action="store_true",
        help="Delete each local MP4 only after its successful upload is stored in the ledger",
    )

    run_sd = commands.add_parser(
        "run-sd", help="Auto-discover an SD card, convert locally, upload, then remove successful MP4s"
    )
    run_sd.add_argument("output", type=Path, help="Local-disk folder for temporary MP4 files")
    run_sd.add_argument("--source", type=Path, help="SD folder; otherwise auto-detect removable media")
    _add_conversion_args(run_sd)
    _add_youtube_auth_args(run_sd)
    _add_upload_policy_args(run_sd)
    run_sd.add_argument("--max-count", type=int)
    run_sd.add_argument(
        "--keep-uploaded",
        action="store_true",
        help="Keep final MP4 files after upload instead of the default cleanup",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if args.command in ("attempts", "attempt-resolve"):
            ledger_path = require_fixed_local_path(args.ledger, "upload ledger")
            ledger = UploadLedger(
                ledger_path,
                DEFAULT_DAILY_UPLOAD_LIMIT,
                args.expected_channel_id,
            )
            if args.command == "attempts":
                print(json.dumps(ledger.pending_attempts(), ensure_ascii=False, indent=2))
            else:
                if args.video_id:
                    if not args.token:
                        raise PipelineError(
                            "--token is required with --video-id so ownership can be verified"
                        )
                    youtube = load_youtube_service(
                        args.token,
                        args.client_secret,
                        args.login_hint,
                        scopes=[YOUTUBE_READONLY_SCOPE],
                    )
                    channel = require_channel(youtube, args.expected_channel_id)
                    require_video_owner(youtube, args.video_id, channel["id"])
                ledger.resolve_attempt(
                    args.attempt_id,
                    confirmed_failed=args.confirmed_failed,
                    video_id=args.video_id,
                    video_ownership_verified=bool(args.video_id),
                )
                print("Upload reservation resolved.")
            return 0

        if args.command == "discover":
            pairs, incomplete = discover_pairs(args.source, not args.no_recursive)
            payload = {
                "pairs": [{"left": str(pair.left), "right": str(pair.right)} for pair in pairs],
                "incomplete": [str(path) for path in incomplete],
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"Complete pairs: {len(pairs)}")
                for pair in pairs:
                    print(f"  {pair.left.name}  +  {pair.right.name}")
                print(f"Unpaired INSV files: {len(incomplete)}")
            return 0

        if args.command == "convert":
            sdk_executable = args.sdk_exe.expanduser().resolve()
            spatialmedia = args.spatialmedia.expanduser().resolve()
            if not sdk_executable.is_file():
                raise PipelineError(f"MediaSDK executable not found: {sdk_executable}")
            if not spatialmedia.exists():
                raise PipelineError(f"Spatial metadata tool not found: {spatialmedia}")
            pairs, incomplete = discover_pairs(args.source, not args.no_recursive)
            if incomplete:
                logging.warning("Ignoring %d unpaired INSV files", len(incomplete))
            if args.limit is not None:
                if args.limit < 1:
                    raise PipelineError("--limit must be at least 1")
                pairs = pairs[: args.limit]
            if not pairs:
                raise PipelineError("No complete INSV pairs found")
            require_unique_output_stems(pairs)
            for index, pair in enumerate(pairs, 1):
                logging.info("Converting pair %d/%d: %s", index, len(pairs), pair.left.name)
                verified = convert_pair(
                    pair,
                    args.output.expanduser().resolve(),
                    sdk_executable,
                    spatialmedia,
                    args.output_size,
                    args.bitrate,
                    args.stitch_type,
                    not args.no_flowstate,
                    args.camera_accessory_type,
                    args.keep_stitched,
                    args.force,
                    args.ffprobe,
                    args.adopt_existing,
                )
                print(json.dumps(asdict(verified), ensure_ascii=False, indent=2))
            return 0

        if args.command == "verify":
            for path in args.files:
                verified = verify_360(
                    path.expanduser().resolve(), args.spatialmedia.expanduser().resolve(), args.ffprobe
                )
                print(json.dumps(asdict(verified), ensure_ascii=False, indent=2))
            return 0

        if args.command in ("auth-check", "playlist-sync", "upload", "run-sd"):
            if args.command == "run-sd":
                source, pairs, incomplete = find_sd_source(args.source)
                output = require_fixed_local_path(args.output, "run-sd output folder")
                require_local_output(source, output)
                if incomplete:
                    logging.warning("Ignoring %d unpaired INSV files", len(incomplete))
                if args.max_count is not None:
                    if args.max_count < 1:
                        raise PipelineError("--max-count must be at least 1")
                    pairs = pairs[: args.max_count]
                require_unique_output_stems(pairs)
                sdk_executable = args.sdk_exe.expanduser().resolve()
                spatialmedia = args.spatialmedia.expanduser().resolve()
                if not sdk_executable.is_file():
                    raise PipelineError(f"MediaSDK executable not found: {sdk_executable}")
                if not spatialmedia.exists():
                    raise PipelineError(f"Spatial metadata tool not found: {spatialmedia}")

            required_scopes = [YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE]
            if args.command == "playlist-sync":
                required_scopes = [YOUTUBE_PLAYLIST_SCOPE]
            elif getattr(args, "playlist_id", None):
                required_scopes = [YOUTUBE_UPLOAD_SCOPE, YOUTUBE_PLAYLIST_SCOPE]
            youtube = load_youtube_service(
                args.token, args.client_secret, args.login_hint, scopes=required_scopes
            )
            channel = require_channel(youtube, args.expected_channel_id)
            print(f"Authenticated YouTube channel: {channel['title']}")
            if args.command == "auth-check":
                return 0

            if args.command == "playlist-sync":
                ledger = UploadLedger(
                    args.ledger.expanduser().resolve(),
                    DEFAULT_DAILY_UPLOAD_LIMIT,
                    channel["id"],
                )
                manager = PlaylistManager(youtube, args.playlist_id, channel["id"])
                added, already_present = sync_ledger_playlist(ledger, manager)
                print(
                    f"Playlist synced: {manager.title} ({manager.playlist_id}); "
                    f"added={added}, already_present={already_present}"
                )
                return 0

            if args.command == "run-sd":
                output.mkdir(parents=True, exist_ok=True)
                ledger_path = args.ledger or output / "upload-ledger.json"
                ledger_path = require_fixed_local_path(ledger_path, "run-sd upload ledger")
                ledger = UploadLedger(ledger_path, args.daily_limit, channel["id"])
                playlist_manager = None
                if args.playlist_id:
                    playlist_manager = PlaylistManager(youtube, args.playlist_id, channel["id"])
                    added, already_present = sync_ledger_playlist(ledger, playlist_manager)
                    logging.info(
                        "Playlist startup sync complete: added=%d, already_present=%d",
                        added,
                        already_present,
                    )
                for index, pair in enumerate(pairs, 1):
                    source_key = pair_fingerprint(pair)
                    source_identity = pair_content_identity(pair)
                    final_output = output / f"{pair.output_stem}.mp4"
                    if ledger.source_uploaded(source_key, source_identity):
                        logging.info("Source pair was already uploaded; skipping: %s", pair.left.name)
                        if not args.keep_uploaded and final_output.exists():
                            receipt = ledger.uploaded_record(
                                final_output, source_key, source_identity
                            )
                            if receipt and _record_cleanup_ready(receipt):
                                _safe_unlink(
                                    final_output,
                                    root=output,
                                    expected_fingerprint=str(receipt["_artifact_fingerprint"]),
                                    expected_identity=str(receipt["_artifact_identity"]),
                                )
                                logging.info("Removed previously uploaded local MP4: %s", final_output.name)
                            else:
                                logging.warning(
                                    "Kept local MP4 because it does not exactly match the upload receipt: %s",
                                    final_output.name,
                                )
                        continue
                    try:
                        probe_video(pair.left, args.ffprobe)
                        probe_video(pair.right, args.ffprobe)
                    except PipelineError as exc:
                        logging.error("Skipping unreadable INSV pair %s: %s", pair.output_stem, exc)
                        continue
                    ledger.require_capacity()
                    logging.info("Converting SD pair %d/%d: %s", index, len(pairs), pair.left.name)
                    verified = convert_pair(
                        pair,
                        output,
                        sdk_executable,
                        spatialmedia,
                        args.output_size,
                        args.bitrate,
                        args.stitch_type,
                        not args.no_flowstate,
                        args.camera_accessory_type,
                        args.keep_stitched,
                        args.force,
                        args.ffprobe,
                        args.adopt_existing,
                    )
                    upload_verified_file(
                        youtube,
                        channel["id"],
                        Path(verified.path),
                        ledger,
                        spatialmedia,
                        args.ffprobe,
                        args.privacy,
                        args.made_for_kids,
                        source_key,
                        delete_after_success=not args.keep_uploaded,
                        playlist_manager=playlist_manager,
                        source_identity=source_identity,
                    )
                return 0

            source = args.source.expanduser().resolve()
            files = sorted(source.glob("*.mp4")) if source.is_dir() else [source]
            files = [path for path in files if ".stitched." not in path.name and ".partial." not in path.name]
            if args.max_count is not None:
                if args.max_count < 1:
                    raise PipelineError("--max-count must be at least 1")
                files = files[: args.max_count]
            if not files:
                raise PipelineError(f"No MP4 files found: {source}")
            ledger_path = args.ledger or (source if source.is_dir() else source.parent) / "upload-ledger.json"
            ledger = UploadLedger(ledger_path, args.daily_limit, channel["id"])
            playlist_manager = None
            if args.playlist_id:
                playlist_manager = PlaylistManager(youtube, args.playlist_id, channel["id"])
                added, already_present = sync_ledger_playlist(ledger, playlist_manager)
                logging.info(
                    "Playlist startup sync complete: added=%d, already_present=%d",
                    added,
                    already_present,
                )
            for path in files:
                upload_verified_file(
                    youtube,
                    channel["id"],
                    path,
                    ledger,
                    args.spatialmedia.expanduser().resolve(),
                    args.ffprobe,
                    args.privacy,
                    args.made_for_kids,
                    delete_after_success=args.delete_after_upload,
                    playlist_manager=playlist_manager,
                )
            return 0

        raise AssertionError(f"Unhandled command: {args.command}")
    except PipelineError as exc:
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
