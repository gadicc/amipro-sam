from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import DOSBOX_PROFILE, dosbox_config
from .constants import (
    EXIT_BACKEND,
    EXIT_INTEGRITY,
    EXIT_MISSING,
    EXIT_USAGE,
    HASH_CHUNK_BYTES,
    MAX_MEDIA_FILE_BYTES,
    RUNTIME_SCHEMA,
)
from .errors import OracleError
from .fat12 import EXTRACTION_SCHEMA, extract_fat12_root_images
from .io import atomic_write, atomic_write_json, digest_json, read_json_object, sha256_file
from .media import inventory_media
from .oci import BindMount, build_podman_invocation, run_podman_bounded
from .process import DEFAULT_MAX_TREE_BYTES, DEFAULT_MAX_TREE_ENTRIES
from .raster import decode_png
from .state import StateMachine
from .toolchain import probe_recorded_image

WINDOWS_CHECKPOINT_SCHEMA = "amipro-oracle-windows-checkpoint-v1"
FLAT_MEDIA_SCHEMA = "amipro-oracle-flat-media-cache-v1"
BOOTSTRAP_INPUT_SCHEMA = "amipro-oracle-windows-bootstrap-input-v1"
BOOTSTRAP_RESULT_SCHEMA = "amipro-oracle-windows-bootstrap-result-v1"
RUNTIME_TREE_SCHEMA = "amipro-oracle-windows-tree-v1"
WINDOWS_MEDIA_PROFILE = "supplied-windows-3.1-english-six-floppy-v1"
EXPECTED_EXTRACTION_DIGEST = (
    "362e55b05f737072f61f11b385b5214cb96354e8115e21ef938f8142e3d80504"
)
EXPECTED_EXTRACTION_FILES = 467
EXPECTED_EXTRACTION_BYTES = 8_305_739
GUEST_DATE = "03/10/1992"
GUEST_TIME = "03:10:01"
GUEST_DATE_REPORTED = "03/10/1992"
GUEST_TIME_REPORTED = "3:10:00"
WINDOWS_FREE_MB = 128
NORMALIZED_RUNTIME_MTIME_NS = 700_197_000_000_000_000
INNER_TIME_LIMIT_SECONDS = 900
OUTER_TIME_LIMIT_SECONDS = 1200

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def windows_setup_shh() -> bytes:
    lines = (
        "[sysinfo]",
        "showsysinfo=no",
        "",
        "[configuration]",
        "machine=ibm_compatible",
        "display=vga",
        "mouse=ps2mouse",
        "network=nonet",
        "keyboard=t4s0enha",
        "language=enu",
        "kblayout=nodll",
        "",
        "[windir]",
        r"C:\WINDOWS",
        "",
        "[userinfo]",
        '"Ami Pro Oracle"',
        '"Local"',
        "",
        "[endinstall]",
        "configfiles=save",
        "endopt=exit",
    )
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def windows_setup_config() -> str:
    return dosbox_config(
        runtime_free_mb=WINDOWS_FREE_MB,
        autoexec=(
            'MOUNT S "/oracle/media/windows" -t dir -ro',
            "COUNTRY 1",
            f"DATE {GUEST_DATE}",
            f"TIME {GUEST_TIME}",
            r"DATE /T > C:\ORADATE.TXT",
            r"TIME /T > C:\ORATIME.TXT",
            r"Z:\CONFIG.COM -SECUREMODE",
            r"C:\WINSETUP.BAT",
        ),
    )


def windows_setup_batch() -> bytes:
    lines = (
        "@ECHO OFF",
        r"S:\SETUP.EXE /C /O:S:\SETUP.INF /S:S:\ /H:C:\WIN31.SHH",
        "IF ERRORLEVEL 1 GOTO SETUP_FAILED",
        r"ECHO SETUP_RETURNED_ZERO>C:\SETUP.OK",
        "GOTO SETUP_DONE",
        ":SETUP_FAILED",
        r"ECHO SETUP_ERRORLEVEL_NONZERO>C:\SETUP.ERR",
        ":SETUP_DONE",
        "EXIT",
    )
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def _require_verified_image(image_record: dict[str, Any]) -> None:
    probe = probe_recorded_image(image_record)
    status = probe.get("status")
    if status == "match":
        return
    if status == "missing":
        exit_code = EXIT_MISSING
    elif status == "error":
        exit_code = EXIT_BACKEND
    else:
        exit_code = EXIT_INTEGRITY
    detail = probe.get("error") or "recorded OCI image identity does not match"
    raise OracleError(f"locked OCI image verification failed: {detail}", exit_code=exit_code)


def _ensure_private_directories(home: Path) -> None:
    directories = (
        home,
        home / "cache",
        home / "cache" / "media",
        home / "cache" / "windows",
        home / "control",
        home / "jobs",
        home / "locks",
    )
    for directory in directories:
        if directory.is_symlink():
            raise OracleError(
                f"oracle state directory must not be a symlink: {directory}",
                exit_code=EXIT_INTEGRITY,
            )
        if directory.exists():
            if not directory.is_dir():
                raise OracleError(
                    f"oracle state path must be a directory: {directory}",
                    exit_code=EXIT_INTEGRITY,
                )
        else:
            directory.mkdir(mode=0o700)
        directory.chmod(0o700)


@contextmanager
def _cache_lock(home: Path, name: str) -> Iterator[None]:
    if _SHA256.fullmatch(name) is None:
        raise OracleError("invalid cache lock identity", exit_code=EXIT_INTEGRITY)
    path = home / "locks" / f"{name}.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise OracleError(
            f"cannot open cache lock: {path}", exit_code=EXIT_INTEGRITY
        ) from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise OracleError(
                    f"bootstrap for cache key {name} is already running",
                    exit_code=EXIT_BACKEND,
                ) from exc
            raise
        yield
    finally:
        os.close(descriptor)


def _directory_fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tree_fsync(root: Path) -> None:
    directories: list[Path] = []
    for current, names, files in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        directories.append(directory)
        for name in [*names, *files]:
            path = directory / name
            if path.is_symlink():
                raise OracleError(
                    f"cannot persist a cache tree containing a symlink: {path}",
                    exit_code=EXIT_INTEGRITY,
                )
        for name in files:
            path = directory / name
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(path, flags)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OracleError(
                        f"cannot persist a non-file cache entry: {path}",
                        exit_code=EXIT_INTEGRITY,
                    )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        _directory_fsync(directory)


def _media_cache_identity(media: dict[str, Any]) -> dict[str, object]:
    if (
        media.get("kind") != "windows-3.1"
        or media.get("media_profile") != WINDOWS_MEDIA_PROFILE
        or media.get("file_count") != 6
        or not isinstance(media.get("digest"), str)
    ):
        raise OracleError(
            "Windows media is not the supported six-floppy profile",
            exit_code=EXIT_INTEGRITY,
        )
    return {
        "schema": FLAT_MEDIA_SCHEMA,
        "media_kind": "windows-3.1",
        "media_profile": WINDOWS_MEDIA_PROFILE,
        "media_digest": media["digest"],
        "extractor_schema": EXTRACTION_SCHEMA,
    }


def _verify_flat_media_cache(
    root: Path,
    *,
    expected_key: str,
    expected_identity: dict[str, object],
    require_sealed: bool = True,
) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    extraction_path = root / "extraction.json"
    tree_path = root / "tree.json"
    source = root / "source"
    if (
        root.is_symlink()
        or not root.is_dir()
        or manifest_path.is_symlink()
        or not manifest_path.is_file()
        or extraction_path.is_symlink()
        or not extraction_path.is_file()
        or tree_path.is_symlink()
        or not tree_path.is_file()
        or source.is_symlink()
        or not source.is_dir()
        or {path.name for path in root.iterdir()}
        != {"extraction.json", "manifest.json", "source", "tree.json"}
    ):
        raise OracleError(
            f"flat Windows media cache has an unsafe shape: {root}",
            exit_code=EXIT_INTEGRITY,
        )
    try:
        manifest = read_json_object(manifest_path)
        extraction = read_json_object(extraction_path)
        recorded_tree = read_json_object(tree_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            f"flat Windows media cache manifest is invalid: {manifest_path}",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    if (
        manifest.get("schema") != FLAT_MEDIA_SCHEMA
        or manifest.get("status") != "ready"
        or manifest.get("cache_key") != expected_key
        or manifest.get("identity") != expected_identity
        or manifest.get("extraction_digest") != EXPECTED_EXTRACTION_DIGEST
        or manifest.get("file_count") != EXPECTED_EXTRACTION_FILES
        or manifest.get("total_bytes") != EXPECTED_EXTRACTION_BYTES
    ):
        raise OracleError(
            f"flat Windows media cache identity mismatch: {manifest_path}",
            exit_code=EXIT_INTEGRITY,
        )
    tree = inventory_media(source, kind="flattened-windows-setup")
    if (
        extraction.get("schema") != EXTRACTION_SCHEMA
        or extraction.get("source_media_digest")
        != expected_identity.get("media_digest")
        or not isinstance(extraction.get("files"), list)
        or digest_json(
            {
                "schema": extraction.get("schema"),
                "source_media_digest": extraction.get("source_media_digest"),
                "files": extraction.get("files"),
            }
        )
        != extraction.get("digest")
        or extraction.get("digest") != EXPECTED_EXTRACTION_DIGEST
        or extraction.get("file_count") != EXPECTED_EXTRACTION_FILES
        or extraction.get("total_bytes") != EXPECTED_EXTRACTION_BYTES
        or recorded_tree != tree
        or tree["digest"] != manifest.get("tree_digest")
        or tree["file_count"] != EXPECTED_EXTRACTION_FILES
        or tree["total_bytes"] != EXPECTED_EXTRACTION_BYTES
        or tree["source_writable_files"] != 0
        or stat.S_IMODE(source.stat().st_mode) & 0o222
        or (
            require_sealed
            and (
                stat.S_IMODE(root.stat().st_mode) & 0o222
                or any(
                    stat.S_IMODE(path.stat().st_mode) & 0o222
                    for path in (manifest_path, extraction_path, tree_path)
                )
            )
        )
    ):
        raise OracleError(
            f"flat Windows media cache content mismatch: {source}",
            exit_code=EXIT_INTEGRITY,
        )
    return manifest


def _seal_flat_media_cache(root: Path) -> None:
    for name in ("extraction.json", "manifest.json", "tree.json"):
        (root / name).chmod(0o444)
    (root / "source").chmod(0o555)
    root.chmod(0o555)


def ensure_flat_windows_media(
    home: Path,
    media_root: Path,
    media: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    _ensure_private_directories(home)
    identity = _media_cache_identity(media)
    key = digest_json(identity)
    final = home / "cache" / "media" / key
    with _cache_lock(home, key):
        if final.exists() or final.is_symlink():
            manifest = _verify_flat_media_cache(
                final,
                expected_key=key,
                expected_identity=identity,
                require_sealed=False,
            )
            _seal_flat_media_cache(final)
            return final / "source", _verify_flat_media_cache(
                final,
                expected_key=key,
                expected_identity=identity,
            )
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{key}.",
                suffix=".staging",
                dir=home / "cache" / "media",
            )
        )
        staging.chmod(0o700)
        source = staging / "source"
        extraction = extract_fat12_root_images(media_root, media, source)
        if (
            extraction["digest"] != EXPECTED_EXTRACTION_DIGEST
            or extraction["file_count"] != EXPECTED_EXTRACTION_FILES
            or extraction["total_bytes"] != EXPECTED_EXTRACTION_BYTES
        ):
            raise OracleError(
                "flattened Windows source does not match the pinned extraction profile",
                exit_code=EXIT_INTEGRITY,
            )
        tree = inventory_media(source, kind="flattened-windows-setup")
        source.chmod(0o555)
        manifest: dict[str, Any] = {
            "schema": FLAT_MEDIA_SCHEMA,
            "status": "ready",
            "cache_key": key,
            "identity": identity,
            "extraction_digest": extraction["digest"],
            "file_count": extraction["file_count"],
            "total_bytes": extraction["total_bytes"],
            "tree_digest": tree["digest"],
        }
        atomic_write_json(staging / "extraction.json", extraction)
        atomic_write_json(staging / "tree.json", tree)
        atomic_write_json(staging / "manifest.json", manifest)
        for path in (staging / "extraction.json", staging / "tree.json", staging / "manifest.json"):
            path.chmod(0o444)
        _directory_fsync(staging)
        _seal_flat_media_cache(staging)
        os.rename(staging, final)
        _directory_fsync(final.parent)
        return final / "source", _verify_flat_media_cache(
            final,
            expected_key=key,
            expected_identity=identity,
        )


def windows_bootstrap_inputs(
    windows_media: dict[str, Any],
    flat_media: dict[str, Any],
    image_record: dict[str, Any],
    *,
    outer_time_limit_seconds: float = OUTER_TIME_LIMIT_SECONDS,
) -> dict[str, object]:
    if (
        isinstance(outer_time_limit_seconds, bool)
        or not isinstance(outer_time_limit_seconds, (int, float))
        or not 1 <= outer_time_limit_seconds <= OUTER_TIME_LIMIT_SECONDS
    ):
        raise OracleError(
            "Windows bootstrap timeout must be between 1 and "
            f"{OUTER_TIME_LIMIT_SECONDS} seconds",
            exit_code=EXIT_USAGE,
        )
    image_id = image_record.get("image_id")
    image_digest = image_record.get("image_digest")
    lock_hash = image_record.get("lock_sha256")
    if (
        not isinstance(image_id, str)
        or not image_id
        or not isinstance(image_digest, str)
        or _IMAGE_DIGEST.fullmatch(image_digest) is None
        or not isinstance(lock_hash, str)
        or _SHA256.fullmatch(lock_hash) is None
        or image_record.get("platform") != "linux/amd64"
    ):
        raise OracleError("invalid verified OCI image identity", exit_code=EXIT_INTEGRITY)
    config = windows_setup_config().encode("utf-8")
    shh = windows_setup_shh()
    batch = windows_setup_batch()
    return {
        "schema": BOOTSTRAP_INPUT_SCHEMA,
        "windows_media": {
            "profile": windows_media["media_profile"],
            "digest": windows_media["digest"],
        },
        "flat_media": {
            "cache_key": flat_media["cache_key"],
            "extraction_schema": EXTRACTION_SCHEMA,
            "extraction_digest": flat_media["extraction_digest"],
            "tree_digest": flat_media["tree_digest"],
        },
        "toolchain": {
            "image_id": image_id,
            "image_digest": image_digest,
            "lock_sha256": lock_hash,
            "platform": "linux/amd64",
        },
        "installer_driver": "windows-3.1-setup-shh-batch-v2",
        "runtime_layout": "mounted-folder-v1",
        "printer_profile": "none",
        "dosbox_profile": DOSBOX_PROFILE,
        "dosbox_config_sha256": hashlib.sha256(config).hexdigest(),
        "setup_shh_sha256": hashlib.sha256(shh).hexdigest(),
        "setup_batch_sha256": hashlib.sha256(batch).hexdigest(),
        "guest_clock": {
            "date_command": GUEST_DATE,
            "time_command": GUEST_TIME,
            "expected_date": GUEST_DATE_REPORTED,
            "expected_time": GUEST_TIME_REPORTED,
        },
        "reported_free_mb": WINDOWS_FREE_MB,
        "runtime_metadata_policy": {
            "mtime_ns": NORMALIZED_RUNTIME_MTIME_NS,
            "file_mode": "0644",
            "directory_mode": "0755",
            "timezone": "UTC",
        },
        "inner_time_limit_seconds": INNER_TIME_LIMIT_SECONDS,
        "outer_time_limit_seconds": outer_time_limit_seconds,
    }


def _write_attempt(job: Path, inputs: dict[str, object], machine: StateMachine) -> None:
    atomic_write_json(
        job / "attempt.json",
        {
            "schema": "amipro-oracle-bootstrap-attempt-v1",
            "phase": "windows-setup",
            "inputs_digest": digest_json(inputs),
            "state": machine.state,
            "state_trace": machine.trace,
        },
    )


def _load_evidence_job(
    home: Path,
    checkpoint_root: Path,
    checkpoint_key: str,
    checkpoint: dict[str, Any],
) -> str:
    receipt_path = checkpoint_root / "evidence-receipt.json"
    if not receipt_path.exists():
        raise OracleError(
            "Windows checkpoint has no preserved evidence receipt",
            exit_code=EXIT_INTEGRITY,
        )
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise OracleError("Windows evidence receipt is unsafe", exit_code=EXIT_INTEGRITY)
    if stat.S_IMODE(receipt_path.stat().st_mode) & 0o222:
        raise OracleError("Windows evidence receipt is writable", exit_code=EXIT_INTEGRITY)
    try:
        receipt = read_json_object(receipt_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Windows evidence receipt is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    job_name = receipt.get("evidence_job")
    result_sha256 = receipt.get("result_sha256")
    if (
        set(receipt)
        != {"schema", "checkpoint_key", "evidence_job", "result_sha256"}
        or receipt.get("schema") != "amipro-oracle-windows-evidence-receipt-v1"
        or receipt.get("checkpoint_key") != checkpoint_key
        or not isinstance(job_name, str)
        or re.fullmatch(r"bootstrap-windows-[a-z0-9_-]+", job_name) is None
        or not isinstance(result_sha256, str)
        or _SHA256.fullmatch(result_sha256) is None
    ):
        raise OracleError(
            "Windows evidence receipt identity mismatch",
            exit_code=EXIT_INTEGRITY,
        )
    job = home / "jobs" / job_name
    result_path = job / "result.json"
    if (
        job.is_symlink()
        or not job.is_dir()
        or result_path.is_symlink()
        or not result_path.is_file()
        or sha256_file(result_path) != result_sha256
    ):
        raise OracleError(
            "Windows evidence result does not match its receipt",
            exit_code=EXIT_INTEGRITY,
        )
    try:
        result = read_json_object(result_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Windows evidence result is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    if (
        result.get("schema") != BOOTSTRAP_RESULT_SCHEMA
        or result.get("status") != "windows-install-candidate"
        or result.get("checkpoint_key") != checkpoint_key
        or result.get("cache_reused") is not False
        or result.get("evidence_job") != job_name
        or result.get("checkpoint") != checkpoint
        or result.get("evidence_stage") != "validated-before-atomic-promotion"
        or not isinstance(result.get("observer"), dict)
    ):
        raise OracleError(
            "Windows evidence result identity mismatch",
            exit_code=EXIT_INTEGRITY,
        )
    try:
        observed = _validate_observer_evidence(job / "diagnostics")
    except OracleError as exc:
        raise OracleError(
            "Windows checkpoint observer evidence is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    if result["observer"] != observed:
        raise OracleError(
            "Windows checkpoint observer summary mismatch",
            exit_code=EXIT_INTEGRITY,
        )
    return job_name


def _bootstrap_result(
    checkpoint: dict[str, Any],
    *,
    cache_reused: bool,
    evidence_job: str | None,
) -> dict[str, Any]:
    return {
        "schema": BOOTSTRAP_RESULT_SCHEMA,
        "status": checkpoint["status"],
        "checkpoint_key": checkpoint["checkpoint_key"],
        "cache_reused": cache_reused,
        "evidence_job": evidence_job,
        "checkpoint": checkpoint,
    }


def _runtime_stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _hash_runtime_file(path: Path, expected: os.stat_result) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OracleError(
            f"cannot open generated Windows file safely: {path}",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _runtime_stat_identity(before) != _runtime_stat_identity(expected)
        ):
            raise OracleError(
                f"generated Windows file changed before hashing: {path}",
                exit_code=EXIT_INTEGRITY,
            )
        while chunk := os.read(descriptor, HASH_CHUNK_BYTES):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _runtime_stat_identity(after) != _runtime_stat_identity(before):
            raise OracleError(
                f"generated Windows file changed while hashing: {path}",
                exit_code=EXIT_INTEGRITY,
            )
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _collect_runtime_entries(root: Path) -> list[tuple[Path, str, os.stat_result]]:
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise OracleError(
            f"cannot inspect generated Windows runtime: {root}",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise OracleError(
            f"generated Windows runtime must be a real directory: {root}",
            exit_code=EXIT_INTEGRITY,
        )
    entries = [(root, ".", root_info)]
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(
                    iterator,
                    key=lambda child: (child.name.casefold(), child.name),
                )
        except OSError as exc:
            raise OracleError(
                f"cannot inspect generated Windows directory: {directory}",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        for child in children:
            path = Path(child.path)
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise OracleError(
                    f"cannot inspect generated Windows entry: {path}",
                    exit_code=EXIT_INTEGRITY,
                ) from exc
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif not stat.S_ISREG(info.st_mode):
                raise OracleError(
                    f"generated Windows tree contains a non-file entry: {relative}",
                    exit_code=EXIT_INTEGRITY,
                )
            elif info.st_nlink != 1:
                raise OracleError(
                    f"generated Windows tree contains a hard-linked file: {relative}",
                    exit_code=EXIT_INTEGRITY,
                )
            entries.append((path, relative, info))
            if len(entries) > DEFAULT_MAX_TREE_ENTRIES:
                raise OracleError(
                    "generated Windows tree exceeds the "
                    f"{DEFAULT_MAX_TREE_ENTRIES} entry limit",
                    exit_code=EXIT_INTEGRITY,
                )
    return sorted(entries, key=lambda item: (item[1].casefold(), item[1]))


def _inventory_windows_runtime(root: Path) -> dict[str, Any]:
    first = _collect_runtime_entries(root)
    casefolded: set[str] = set()
    records: list[dict[str, object]] = []
    total_bytes = 0
    file_count = 0
    directory_count = 0
    for path, relative, info in first:
        folded = relative.casefold()
        if folded in casefolded:
            raise OracleError(
                f"generated Windows paths collide case-insensitively: {relative}",
                exit_code=EXIT_INTEGRITY,
            )
        casefolded.add(folded)
        common: dict[str, object] = {
            "path": relative,
            "mode": f"{stat.S_IMODE(info.st_mode):04o}",
            "mtime_ns": info.st_mtime_ns,
            "dos_mtime_2s": info.st_mtime_ns // 2_000_000_000,
        }
        if stat.S_ISDIR(info.st_mode):
            directory_count += 1
            records.append({**common, "type": "directory"})
        else:
            if info.st_size > MAX_MEDIA_FILE_BYTES:
                raise OracleError(
                    f"generated Windows file is too large: {relative}",
                    exit_code=EXIT_INTEGRITY,
                )
            total_bytes += info.st_size
            if total_bytes > DEFAULT_MAX_TREE_BYTES:
                raise OracleError(
                    "generated Windows tree exceeds its byte limit",
                    exit_code=EXIT_INTEGRITY,
                )
            file_count += 1
            records.append(
                {
                    **common,
                    "type": "file",
                    "size": info.st_size,
                    "sha256": _hash_runtime_file(path, info),
                }
            )
    second = _collect_runtime_entries(root)
    if [
        (relative, _runtime_stat_identity(info)) for _, relative, info in first
    ] != [
        (relative, _runtime_stat_identity(info)) for _, relative, info in second
    ]:
        raise OracleError(
            f"generated Windows tree changed while hashing: {root}",
            exit_code=EXIT_INTEGRITY,
        )
    identity = {"schema": RUNTIME_TREE_SCHEMA, "entries": records}
    return {
        **identity,
        "digest": digest_json(identity),
        "file_count": file_count,
        "directory_count": directory_count,
        "total_bytes": total_bytes,
    }


def _validate_windows_tree(runtime: Path) -> dict[str, Any]:
    tree = _inventory_windows_runtime(runtime)
    files = {
        str(record["path"]).casefold(): runtime / str(record["path"])
        for record in tree["entries"]
        if record["type"] == "file"
    }
    required = (
        "setup.ok",
        "oradate.txt",
        "oratime.txt",
        "windows/win.com",
        "windows/progman.exe",
        "windows/win.ini",
        "windows/system.ini",
        "windows/system/krnl386.exe",
        "windows/system/gdi.exe",
        "windows/system/user.exe",
        "windows/system/vga.drv",
        "windows/system/mouse.drv",
    )
    missing = [relative for relative in required if relative not in files]
    if missing:
        raise OracleError(
            f"Windows Setup returned without required files: {', '.join(missing)}",
            exit_code=EXIT_BACKEND,
        )
    if "setup.err" in files:
        raise OracleError("Windows Setup reported a nonzero error level", exit_code=EXIT_BACKEND)
    if files["setup.ok"].read_bytes() != b"SETUP_RETURNED_ZERO\r\n":
        raise OracleError("Windows Setup completion sentinel is invalid", exit_code=EXIT_BACKEND)
    date_report = files["oradate.txt"].read_text(encoding="ascii").strip()
    time_report = files["oratime.txt"].read_text(encoding="ascii").strip()
    if date_report != GUEST_DATE_REPORTED or time_report != GUEST_TIME_REPORTED:
        raise OracleError(
            f"guest clock probe mismatch: {date_report!r} {time_report!r}",
            exit_code=EXIT_BACKEND,
        )
    system_ini = files["windows/system.ini"].read_text(
        encoding="latin-1", errors="strict"
    ).casefold()
    if "display.drv=vga.drv" not in system_ini.replace(" ", ""):
        raise OracleError("Windows SYSTEM.INI does not select VGA.DRV", exit_code=EXIT_BACKEND)
    return tree


def _validate_observer_evidence(diagnostics: Path) -> dict[str, object]:
    status_path = diagnostics / "observer.status"
    final_path = diagnostics / "screen-last.png"
    visual_path = diagnostics / "screen-visual.png"
    for path in (status_path, final_path, visual_path):
        if path.is_symlink() or not path.is_file():
            raise OracleError(
                f"screen observer evidence is missing or unsafe: {path.name}",
                exit_code=EXIT_BACKEND,
            )
    if status_path.stat().st_size > 4096:
        raise OracleError("screen observer status is oversized", exit_code=EXIT_BACKEND)
    values: dict[str, str] = {}
    for line in status_path.read_text(encoding="ascii").splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            raise OracleError("screen observer status is invalid", exit_code=EXIT_BACKEND)
        values[key] = value
    expected_keys = {
        "schema",
        "status",
        "capture_count",
        "archived_count",
        "visual_count",
        "failure_count",
        "final_sha256",
        "final_bytes",
    }
    if set(values) != expected_keys:
        raise OracleError("screen observer status fields are invalid", exit_code=EXIT_BACKEND)
    try:
        captures = int(values["capture_count"])
        archived = int(values["archived_count"])
        visual = int(values["visual_count"])
        failures = int(values["failure_count"])
        final_bytes = int(values["final_bytes"])
    except ValueError as exc:
        raise OracleError(
            "screen observer counters are invalid", exit_code=EXIT_BACKEND
        ) from exc
    if (
        values["schema"] != "amipro-oracle-screen-observer-v1"
        or values["status"] != "ok"
        or captures < 1
        or not 1 <= archived <= 256
        or visual < 1
        or failures != 0
        or final_bytes != final_path.stat().st_size
        or _SHA256.fullmatch(values["final_sha256"]) is None
        or sha256_file(final_path) != values["final_sha256"]
    ):
        raise OracleError("screen observer did not produce valid evidence", exit_code=EXIT_BACKEND)
    try:
        width, height, pixels = decode_png(visual_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "screen observer visual capture is invalid",
            exit_code=EXIT_BACKEND,
        ) from exc
    first = pixels[:3]
    nonuniform = any(
        pixels[offset : offset + 3] != first
        for offset in range(4, len(pixels), 4)
    )
    if (width, height) != (1024, 768) or not nonuniform:
        raise OracleError(
            "screen observer never captured a non-uniform 1024x768 display",
            exit_code=EXIT_BACKEND,
        )
    archived_paths = sorted(
        path
        for path in diagnostics.iterdir()
        if re.fullmatch(r"screen-[0-9]{4}\.png", path.name) is not None
    )
    if len(archived_paths) != archived or any(
        path.is_symlink() or not path.is_file() for path in archived_paths
    ):
        raise OracleError(
            "screen observer archive does not match its status",
            exit_code=EXIT_BACKEND,
        )
    archived_files = [
        {
            "path": path.name,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for path in archived_paths
    ]
    return {
        "schema": values["schema"],
        "capture_count": captures,
        "archived_count": archived,
        "archived_files": archived_files,
        "visual_count": visual,
        "failure_count": failures,
        "final_sha256": values["final_sha256"],
        "final_bytes": final_bytes,
        "visual_sha256": sha256_file(visual_path),
        "visual_bytes": visual_path.stat().st_size,
    }


def _make_tree_read_only(root: Path) -> None:
    directories: list[Path] = []
    for current, names, files in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        directories.append(directory)
        for name in [*names, *files]:
            path = directory / name
            if path.is_symlink():
                raise OracleError(
                    f"generated runtime contains a symlink: {path}",
                    exit_code=EXIT_INTEGRITY,
                )
        for name in files:
            (directory / name).chmod(0o444)
    for directory in reversed(directories):
        directory.chmod(0o555)


def _normalize_runtime_metadata(root: Path) -> None:
    _inventory_windows_runtime(root)
    directories: list[Path] = []
    for current, names, files in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        directories.append(directory)
        for name in [*names, *files]:
            path = directory / name
            if path.is_symlink():
                raise OracleError(
                    f"generated runtime contains a symlink: {path}",
                    exit_code=EXIT_INTEGRITY,
                )
        for name in files:
            path = directory / name
            path.chmod(0o644)
            os.utime(
                path,
                ns=(NORMALIZED_RUNTIME_MTIME_NS, NORMALIZED_RUNTIME_MTIME_NS),
                follow_symlinks=False,
            )
    for directory in reversed(directories):
        directory.chmod(0o755)
        os.utime(
            directory,
            ns=(NORMALIZED_RUNTIME_MTIME_NS, NORMALIZED_RUNTIME_MTIME_NS),
            follow_symlinks=False,
        )


def _verify_windows_checkpoint(
    root: Path,
    expected_key: str,
    expected_inputs: dict[str, object],
) -> dict[str, Any]:
    manifest_path = root / "runtime.json"
    inputs_path = root / "inputs.json"
    guest_tree_path = root / "guest-tree.json"
    sealed_tree_path = root / "sealed-tree.json"
    config_path = root / "dosbox-x.conf"
    shh_path = root / "WIN31.SHH"
    batch_path = root / "WINSETUP.BAT"
    receipt_path = root / "evidence-receipt.json"
    pristine = root / "pristine-c"
    expected_names = {
        "WIN31.SHH",
        "WINSETUP.BAT",
        "dosbox-x.conf",
        "evidence-receipt.json",
        "guest-tree.json",
        "inputs.json",
        "pristine-c",
        "runtime.json",
        "sealed-tree.json",
    }
    files = (
        manifest_path,
        inputs_path,
        guest_tree_path,
        sealed_tree_path,
        config_path,
        shh_path,
        batch_path,
        receipt_path,
    )
    if (
        root.is_symlink()
        or not root.is_dir()
        or {path.name for path in root.iterdir()} != expected_names
        or any(path.is_symlink() or not path.is_file() for path in files)
        or pristine.is_symlink()
        or not pristine.is_dir()
        or stat.S_IMODE(root.stat().st_mode) & 0o222
        or stat.S_IMODE(pristine.stat().st_mode) & 0o222
        or any(stat.S_IMODE(path.stat().st_mode) & 0o222 for path in files)
    ):
        raise OracleError(
            f"Windows checkpoint cache has an unsafe shape: {root}",
            exit_code=EXIT_INTEGRITY,
        )
    try:
        manifest = read_json_object(manifest_path)
        recorded_inputs = read_json_object(inputs_path)
        config_bytes = config_path.read_bytes()
        shh_bytes = shh_path.read_bytes()
        batch_bytes = batch_path.read_bytes()
    except (OSError, ValueError) as exc:
        raise OracleError(
            f"Windows checkpoint manifest is invalid: {manifest_path}",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    expected_manifest_keys = {
        "backend",
        "baseline_eligible",
        "checkpoint_key",
        "checkpoint_role",
        "guest_tree_digest",
        "guest_tree_manifest_digest",
        "inputs_digest",
        "printer_profile",
        "runtime_schema",
        "schema",
        "sealed_tree_digest",
        "sealed_tree_manifest_digest",
        "status",
        "tree_directory_count",
        "tree_file_count",
        "tree_total_bytes",
    }
    if (
        set(manifest) != expected_manifest_keys
        or set(recorded_inputs) != set(expected_inputs)
        or manifest.get("schema") != WINDOWS_CHECKPOINT_SCHEMA
        or manifest.get("status") != "windows-install-candidate"
        or manifest.get("checkpoint_key") != expected_key
        or manifest.get("checkpoint_role")
        != "requires-separate-program-manager-boot-probe"
        or manifest.get("runtime_schema") != RUNTIME_SCHEMA
        or manifest.get("backend") != "real"
        or manifest.get("baseline_eligible") is not False
        or manifest.get("printer_profile") != "none"
        or manifest.get("inputs_digest") != digest_json(expected_inputs)
        or expected_key != digest_json(expected_inputs)
        or recorded_inputs != expected_inputs
        or config_bytes != windows_setup_config().encode("utf-8")
        or shh_bytes != windows_setup_shh()
        or batch_bytes != windows_setup_batch()
        or hashlib.sha256(config_bytes).hexdigest()
        != expected_inputs.get("dosbox_config_sha256")
        or hashlib.sha256(shh_bytes).hexdigest()
        != expected_inputs.get("setup_shh_sha256")
        or hashlib.sha256(batch_bytes).hexdigest()
        != expected_inputs.get("setup_batch_sha256")
    ):
        raise OracleError(
            f"Windows checkpoint identity mismatch: {manifest_path}",
            exit_code=EXIT_INTEGRITY,
        )
    try:
        guest_tree = read_json_object(guest_tree_path)
        recorded_sealed_tree = read_json_object(sealed_tree_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            f"Windows checkpoint tree manifest is invalid: {root}",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    sealed_tree = _inventory_windows_runtime(pristine)
    if (
        guest_tree.get("schema") != RUNTIME_TREE_SCHEMA
        or digest_json(
            {
                "schema": guest_tree.get("schema"),
                "entries": guest_tree.get("entries"),
            }
        )
        != guest_tree.get("digest")
        or guest_tree.get("digest") != manifest.get("guest_tree_digest")
        or digest_json(guest_tree) != manifest.get("guest_tree_manifest_digest")
        or recorded_sealed_tree != sealed_tree
        or sealed_tree["digest"] != manifest.get("sealed_tree_digest")
        or digest_json(sealed_tree) != manifest.get("sealed_tree_manifest_digest")
        or sealed_tree["file_count"] != manifest.get("tree_file_count")
        or sealed_tree["directory_count"] != manifest.get("tree_directory_count")
        or sealed_tree["total_bytes"] != manifest.get("tree_total_bytes")
        or any(
            int(str(record["mode"]), 8) & 0o222
            for record in sealed_tree["entries"]
        )
    ):
        raise OracleError(
            f"Windows checkpoint tree mismatch: {pristine}",
            exit_code=EXIT_INTEGRITY,
        )
    _validate_windows_tree(pristine)
    return manifest


def verify_windows_install_candidate(
    home: Path,
    checkpoint_key: str,
) -> tuple[Path, dict[str, Any], dict[str, Any], str]:
    if _SHA256.fullmatch(checkpoint_key) is None:
        raise OracleError("invalid Windows checkpoint key", exit_code=EXIT_INTEGRITY)
    root = home / "cache" / "windows" / checkpoint_key
    if root.parent != home / "cache" / "windows":
        raise OracleError("unsafe Windows checkpoint path", exit_code=EXIT_INTEGRITY)
    inputs_path = root / "inputs.json"
    if inputs_path.is_symlink() or not inputs_path.is_file():
        raise OracleError("Windows checkpoint inputs are missing", exit_code=EXIT_INTEGRITY)
    try:
        inputs = read_json_object(inputs_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Windows checkpoint inputs are invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    manifest = _verify_windows_checkpoint(root, checkpoint_key, inputs)
    evidence_job = _load_evidence_job(
        home,
        root,
        checkpoint_key,
        manifest,
    )
    return root, manifest, inputs, evidence_job


def bootstrap_windows_checkpoint(
    home: Path,
    windows_media_root: Path,
    windows_media: dict[str, Any],
    image_record: dict[str, Any],
    *,
    timeout_seconds: float = OUTER_TIME_LIMIT_SECONDS,
) -> dict[str, Any]:
    _require_verified_image(image_record)
    _ensure_private_directories(home)
    source, flat_manifest = ensure_flat_windows_media(
        home,
        windows_media_root,
        windows_media,
    )
    inputs = windows_bootstrap_inputs(
        windows_media,
        flat_manifest,
        image_record,
        outer_time_limit_seconds=timeout_seconds,
    )
    key = digest_json(inputs)
    final = home / "cache" / "windows" / key
    flat_key = flat_manifest.get("cache_key")
    if not isinstance(flat_key, str) or _SHA256.fullmatch(flat_key) is None:
        raise OracleError("invalid flat media cache key", exit_code=EXIT_INTEGRITY)
    expected_flat_identity = _media_cache_identity(windows_media)
    with _cache_lock(home, key), _cache_lock(home, flat_key):
        checked_flat = _verify_flat_media_cache(
            source.parent,
            expected_key=flat_key,
            expected_identity=expected_flat_identity,
        )
        if checked_flat != flat_manifest:
            raise OracleError(
                "flat media cache changed after bootstrap keying",
                exit_code=EXIT_INTEGRITY,
        )
        if final.exists() or final.is_symlink():
            checkpoint = _verify_windows_checkpoint(final, key, inputs)
            return _bootstrap_result(
                checkpoint,
                cache_reused=True,
                evidence_job=_load_evidence_job(home, final, key, checkpoint),
            )

        job = Path(
            tempfile.mkdtemp(
                prefix=f"bootstrap-windows-{key[:12]}-",
                dir=home / "jobs",
            )
        )
        job.chmod(0o700)
        _directory_fsync(home / "jobs")
        control = home / "control" / job.name
        control.mkdir(mode=0o700)
        runtime = job / "runtime"
        for directory in (
            runtime,
            job / "capture",
            job / "diagnostics",
            job / "home",
        ):
            directory.mkdir(mode=0o700)
        config_bytes = windows_setup_config().encode("utf-8")
        shh_bytes = windows_setup_shh()
        batch_bytes = windows_setup_batch()
        atomic_write(job / "dosbox-x.conf", config_bytes)
        atomic_write(runtime / "WIN31.SHH", shh_bytes)
        atomic_write(runtime / "WINSETUP.BAT", batch_bytes)
        atomic_write_json(job / "inputs.json", inputs)
        machine = StateMachine(
            initial="created",
            terminal=frozenset({"checkpointed", "failed"}),
            transitions={
                "created": frozenset({"staged", "failed"}),
                "staged": frozenset({"guest-invoked", "failed"}),
                "guest-invoked": frozenset({"guest-returned", "failed"}),
                "guest-returned": frozenset({"validated", "failed"}),
                "validated": frozenset({"checkpointed", "failed"}),
            },
        )
        machine.advance("staged", evidence="inputs.json")
        _write_attempt(job, inputs, machine)

        suffix = re.sub(r"[^a-z0-9]", "", job.name[-10:].casefold())
        container_name = f"amipro-oracle-win-{suffix}"
        invocation = build_podman_invocation(
            image_record,
            container_name=container_name,
            oracle_root=home,
            job_root=job,
            control_root=control,
            phase="bootstrap",
            mounts=[
                BindMount(job, "/oracle/job", read_only=False),
                BindMount(source, "/oracle/media/windows", read_only=True),
            ],
            dosbox_arguments=[
                "-defaultconf",
                "-conf",
                "/oracle/job/dosbox-x.conf",
                "-fastlaunch",
                "-exit",
                "-time-limit",
                str(INNER_TIME_LIMIT_SECONDS),
            ],
        )
        machine.advance("guest-invoked", evidence="dosbox-x.conf")
        _write_attempt(job, inputs, machine)
        process: dict[str, object] | None = None
        observer: dict[str, object] | None = None
        candidate: Path | None = None
        try:
            process = run_podman_bounded(
                invocation,
                stdout_path=job / "diagnostics" / "container.stdout.log",
                stderr_path=job / "diagnostics" / "container.stderr.log",
                cleanup_path=job / "diagnostics" / "container-cleanup.json",
                timeout_seconds=timeout_seconds,
            )
            if process["exit_code"] != 0:
                error = OracleError(
                    f"Windows Setup container exited {process['exit_code']}",
                    exit_code=EXIT_BACKEND,
                )
                error.process_result = process
                raise error
            observer = _validate_observer_evidence(job / "diagnostics")
            atomic_write_json(job / "observer.json", observer)
            machine.advance("guest-returned", evidence="observer.json")
            _write_attempt(job, inputs, machine)
            post_run_flat = _verify_flat_media_cache(
                source.parent,
                expected_key=flat_key,
                expected_identity=expected_flat_identity,
            )
            if post_run_flat != flat_manifest:
                raise OracleError(
                    "flat media cache changed while Windows Setup was running",
                    exit_code=EXIT_INTEGRITY,
                )
            raw_tree = _validate_windows_tree(runtime)
            atomic_write_json(job / "raw-tree.json", raw_tree)
            _normalize_runtime_metadata(runtime)
            guest_tree = _validate_windows_tree(runtime)
            atomic_write_json(job / "guest-tree.json", guest_tree)
            machine.advance("validated", evidence="guest-tree.json")
            _write_attempt(job, inputs, machine)

            candidate = Path(
                tempfile.mkdtemp(
                    prefix=f".{key}.",
                    suffix=".staging",
                    dir=home / "cache" / "windows",
                )
            )
            candidate.chmod(0o700)
            shutil.copytree(
                runtime,
                candidate / "pristine-c",
                copy_function=shutil.copy2,
            )
            _make_tree_read_only(candidate / "pristine-c")
            sealed_tree = _validate_windows_tree(candidate / "pristine-c")
            manifest: dict[str, Any] = {
                "schema": WINDOWS_CHECKPOINT_SCHEMA,
                "runtime_schema": RUNTIME_SCHEMA,
                "backend": "real",
                "baseline_eligible": False,
                "status": "windows-install-candidate",
                "checkpoint_role": "requires-separate-program-manager-boot-probe",
                "checkpoint_key": key,
                "inputs_digest": digest_json(inputs),
                "guest_tree_digest": guest_tree["digest"],
                "guest_tree_manifest_digest": digest_json(guest_tree),
                "sealed_tree_digest": sealed_tree["digest"],
                "sealed_tree_manifest_digest": digest_json(sealed_tree),
                "tree_file_count": sealed_tree["file_count"],
                "tree_directory_count": sealed_tree["directory_count"],
                "tree_total_bytes": sealed_tree["total_bytes"],
                "printer_profile": "none",
            }
            atomic_write_json(candidate / "inputs.json", inputs)
            atomic_write_json(candidate / "guest-tree.json", guest_tree)
            atomic_write_json(candidate / "sealed-tree.json", sealed_tree)
            atomic_write(candidate / "dosbox-x.conf", config_bytes)
            atomic_write(candidate / "WIN31.SHH", shh_bytes)
            atomic_write(candidate / "WINSETUP.BAT", batch_bytes)
            atomic_write_json(candidate / "runtime.json", manifest)
            evidence_result = {
                **_bootstrap_result(
                    manifest,
                    cache_reused=False,
                    evidence_job=job.name,
                ),
                "observer": observer,
                "process_result": process,
                "state_trace": machine.trace,
                "evidence_stage": "validated-before-atomic-promotion",
            }
            result_path = job / "result.json"
            atomic_write_json(result_path, evidence_result)
            atomic_write_json(
                candidate / "evidence-receipt.json",
                {
                    "schema": "amipro-oracle-windows-evidence-receipt-v1",
                    "checkpoint_key": key,
                    "evidence_job": job.name,
                    "result_sha256": sha256_file(result_path),
                },
            )
            for path in candidate.iterdir():
                if path.is_file():
                    path.chmod(0o444)
            _tree_fsync(candidate)
            candidate.chmod(0o555)
            _directory_fsync(candidate)
            os.rename(candidate, final)
            candidate = final
            _directory_fsync(final.parent)
            checkpoint = _verify_windows_checkpoint(final, key, inputs)
            evidence_job = _load_evidence_job(
                home,
                final,
                key,
                checkpoint,
            )
            machine.advance("checkpointed", evidence=f"cache/windows/{key}/runtime.json")
            _write_attempt(job, inputs, machine)
            candidate = None
            return {
                **_bootstrap_result(
                    checkpoint,
                    cache_reused=False,
                    evidence_job=evidence_job,
                ),
                "observer": observer,
                "process_result": process,
                "state_trace": machine.trace,
                "promotion_state": "committed",
            }
        except BaseException as exc:
            promotion_evidence: str | None = None
            promotion_preservation_error: str | None = None
            if candidate is not None and candidate.exists():
                failed_promotion = job / "failed-promotion"
                try:
                    os.rename(candidate, failed_promotion)
                    promotion_evidence = "failed-promotion"
                except OSError as preservation_exc:
                    promotion_evidence = f"cache/windows/{candidate.name}"
                    promotion_preservation_error = (
                        f"{type(preservation_exc).__name__}: {preservation_exc}"
                    )
            if machine.state != "failed" and "failed" in machine.transitions.get(
                machine.state, frozenset()
            ):
                machine.advance("failed", evidence="failure.json")
            failure: dict[str, object] = {
                "schema": "amipro-oracle-bootstrap-failure-v1",
                "phase": "windows-setup",
                "status": "failure",
                "baseline_eligible": False,
                "inputs_digest": digest_json(inputs),
                "error_type": type(exc).__name__,
                "error": str(exc).replace(str(windows_media_root), "$WIN31_MEDIA"),
                "state_trace": machine.trace,
            }
            if isinstance(exc, OracleError) and exc.process_result is not None:
                failure["process_result"] = exc.process_result
            elif process is not None:
                failure["process_result"] = process
            if observer is not None:
                failure["observer"] = observer
            if promotion_evidence is not None:
                failure["promotion_evidence"] = promotion_evidence
            if promotion_preservation_error is not None:
                failure["promotion_preservation_error"] = promotion_preservation_error
            atomic_write_json(job / "failure.json", failure)
            _write_attempt(job, inputs, machine)
            raise
