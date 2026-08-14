from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import (
    EXIT_INTEGRITY,
    EXIT_MISSING,
    EXPECTED_AMIPRO_EXE_SHA256,
    EXPECTED_AMIPRO_FLOPPY_SHA256,
    EXPECTED_AMIPRO_PAYLOAD_MEDIA_DIGEST,
    HASH_CHUNK_BYTES,
    MAX_MEDIA_FILE_BYTES,
    MAX_MEDIA_FILES,
    MAX_MEDIA_TREE_BYTES,
    MEDIA_SCHEMA,
)
from .errors import MediaIntegrityError, OracleError
from .io import digest_json


@dataclass(frozen=True)
class _Entry:
    absolute: Path
    relative: str
    identity: tuple[int, int, int, int, int]
    writable: bool


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _safe_relative(path: Path) -> str:
    value = PurePosixPath(*path.parts).as_posix()
    if value in {"", "."} or value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise MediaIntegrityError(
            f"unsafe media-relative path: {value!r}", exit_code=EXIT_INTEGRITY
        )
    return value


def _collect(root: Path) -> list[_Entry]:
    try:
        root_info = root.lstat()
    except FileNotFoundError as exc:
        raise OracleError(f"media path does not exist: {root}", exit_code=EXIT_MISSING) from exc

    if stat.S_ISLNK(root_info.st_mode):
        raise MediaIntegrityError(
            f"media root must not be a symlink: {root}", exit_code=EXIT_INTEGRITY
        )
    if stat.S_ISREG(root_info.st_mode):
        paths = [(root, "media")]
    elif stat.S_ISDIR(root_info.st_mode):
        paths: list[tuple[Path, str]] = []
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as iterator:
                    children = sorted(
                        iterator, key=lambda item: (item.name.casefold(), item.name)
                    )
            except OSError as exc:
                raise MediaIntegrityError(
                    f"cannot inventory media directory {directory}: {exc}",
                    exit_code=EXIT_INTEGRITY,
                ) from exc
            for child in children:
                child_path = Path(child.path)
                try:
                    info = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise MediaIntegrityError(
                        f"cannot inspect media entry {child_path}: {exc}",
                        exit_code=EXIT_INTEGRITY,
                    ) from exc
                if stat.S_ISLNK(info.st_mode):
                    raise MediaIntegrityError(
                        f"media trees must not contain symlinks: {child_path}",
                        exit_code=EXIT_INTEGRITY,
                    )
                if stat.S_ISDIR(info.st_mode):
                    stack.append(child_path)
                elif stat.S_ISREG(info.st_mode):
                    paths.append((child_path, _safe_relative(child_path.relative_to(root))))
                else:
                    raise MediaIntegrityError(
                        f"media trees may contain only regular files and directories: {child_path}",
                        exit_code=EXIT_INTEGRITY,
                    )
    else:
        raise MediaIntegrityError(
            f"media root must be a regular file or directory: {root}",
            exit_code=EXIT_INTEGRITY,
        )

    if not paths:
        raise MediaIntegrityError(f"media path contains no files: {root}", exit_code=EXIT_INTEGRITY)
    if len(paths) > MAX_MEDIA_FILES:
        raise MediaIntegrityError(
            f"media tree exceeds the {MAX_MEDIA_FILES} file limit", exit_code=EXIT_INTEGRITY
        )

    entries: list[_Entry] = []
    casefolded_paths: set[str] = set()
    total = 0
    for absolute, relative in sorted(
        paths, key=lambda item: (item[1].casefold(), item[1])
    ):
        folded = relative.casefold()
        if folded in casefolded_paths:
            raise MediaIntegrityError(
                f"media paths collide on a case-insensitive guest: {relative}",
                exit_code=EXIT_INTEGRITY,
            )
        casefolded_paths.add(folded)
        info = absolute.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise MediaIntegrityError(
                f"media entry changed type during inventory: {absolute}",
                exit_code=EXIT_INTEGRITY,
            )
        if info.st_size > MAX_MEDIA_FILE_BYTES:
            raise MediaIntegrityError(
                f"media file exceeds the {MAX_MEDIA_FILE_BYTES} byte limit: {absolute}",
                exit_code=EXIT_INTEGRITY,
            )
        total += info.st_size
        if total > MAX_MEDIA_TREE_BYTES:
            raise MediaIntegrityError(
                f"media tree exceeds the {MAX_MEDIA_TREE_BYTES} byte limit",
                exit_code=EXIT_INTEGRITY,
            )
        entries.append(
            _Entry(
                absolute=absolute,
                relative=relative,
                identity=_identity(info),
                writable=bool(info.st_mode & 0o222),
            )
        )
    return entries


def _hash_entry(entry: _Entry) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(entry.absolute, flags)
    except OSError as exc:
        raise MediaIntegrityError(
            f"cannot open media read-only: {entry.absolute}: {exc}", exit_code=EXIT_INTEGRITY
        ) from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if _identity(before) != entry.identity or not stat.S_ISREG(before.st_mode):
            raise MediaIntegrityError(
                f"media file changed before hashing: {entry.absolute}", exit_code=EXIT_INTEGRITY
            )
        while True:
            chunk = os.read(descriptor, HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _identity(after) != entry.identity:
            raise MediaIntegrityError(
                f"media file changed while hashing: {entry.absolute}", exit_code=EXIT_INTEGRITY
            )
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def inventory_media(path: Path, *, kind: str) -> dict[str, Any]:
    root = path.expanduser().absolute()
    first = _collect(root)
    records = [
        {
            "path": entry.relative,
            "size": entry.identity[2],
            "sha256": _hash_entry(entry),
            "source_writable": entry.writable,
        }
        for entry in first
    ]
    second = _collect(root)
    if [(item.relative, item.identity) for item in first] != [
        (item.relative, item.identity) for item in second
    ]:
        raise MediaIntegrityError(
            f"media tree changed while hashing: {root}", exit_code=EXIT_INTEGRITY
        )

    digest_input = {
        "schema": MEDIA_SCHEMA,
        "kind": kind,
        "files": [
            {"path": record["path"], "size": record["size"], "sha256": record["sha256"]}
            for record in records
        ],
    }
    result: dict[str, Any] = {
        **digest_input,
        "digest": digest_json(digest_input),
        "file_count": len(records),
        "total_bytes": sum(int(record["size"]) for record in records),
        "source_writable_files": sum(bool(record["source_writable"]) for record in records),
    }

    if kind == "amipro":
        executable_hashes = [
            str(record["sha256"])
            for record in records
            if PurePosixPath(str(record["path"])).name.casefold() == "amipro.exe"
        ]
        result["amipro_exe_sha256"] = executable_hashes[0] if executable_hashes else None
        if executable_hashes and any(
            value != EXPECTED_AMIPRO_EXE_SHA256 for value in executable_hashes
        ):
            raise MediaIntegrityError(
                "AMIPRO.EXE does not match the expected SHA-256",
                exit_code=EXIT_INTEGRITY,
            )
        hashes_by_path = {
            str(record["path"]).casefold(): str(record["sha256"]) for record in records
        }
        supplied_floppies = {
            name: hashes_by_path.get(name) for name in EXPECTED_AMIPRO_FLOPPY_SHA256
        }
        if supplied_floppies == EXPECTED_AMIPRO_FLOPPY_SHA256:
            selected_records = [
                record
                for record in records
                if str(record["path"]).casefold() in EXPECTED_AMIPRO_FLOPPY_SHA256
            ]
            selected_input = {
                "schema": MEDIA_SCHEMA,
                "kind": kind,
                "files": [
                    {
                        "path": record["path"],
                        "size": record["size"],
                        "sha256": record["sha256"],
                    }
                    for record in selected_records
                ],
            }
            ignored_records = [record for record in records if record not in selected_records]
            result.update(
                {
                    **selected_input,
                    "digest": digest_json(selected_input),
                    "file_count": len(selected_records),
                    "total_bytes": sum(int(record["size"]) for record in selected_records),
                    "source_writable_files": sum(
                        bool(record["source_writable"]) for record in selected_records
                    ),
                    "source_inventory_digest": digest_json(digest_input),
                    "ignored_files": ignored_records,
                }
            )
            result["media_profile"] = "owned-amipro-3.1-floppies-v1"
        elif (
            executable_hashes
            and result["digest"] == EXPECTED_AMIPRO_PAYLOAD_MEDIA_DIGEST
            and result["file_count"] == 678
            and result["total_bytes"] == 22_964_970
        ):
            result["media_profile"] = "owned-amipro-3.1-extracted-payload-v1"
        else:
            raise MediaIntegrityError(
                "Ami Pro media does not match the supplied eight-floppy set or extracted payload",
                exit_code=EXIT_INTEGRITY,
            )
    return result


def hash_input_file(path: Path) -> dict[str, object]:
    absolute = path.expanduser().absolute()
    try:
        info = absolute.lstat()
    except FileNotFoundError as exc:
        raise OracleError(f"input does not exist: {path}", exit_code=EXIT_MISSING) from exc
    if not stat.S_ISREG(info.st_mode):
        raise MediaIntegrityError(
            f"input must be a regular, non-symlink file: {path}", exit_code=EXIT_INTEGRITY
        )
    entry = _Entry(
        absolute=absolute,
        relative="input",
        identity=_identity(info),
        writable=bool(info.st_mode & 0o222),
    )
    return {"size": info.st_size, "sha256": _hash_entry(entry)}


def stage_input_file(
    source: Path, destination: Path, *, max_bytes: int = 512 * 1024 * 1024
) -> dict[str, object]:
    absolute = source.expanduser().absolute()
    try:
        info = absolute.lstat()
    except FileNotFoundError as exc:
        raise OracleError(f"input does not exist: {source}", exit_code=EXIT_MISSING) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        raise MediaIntegrityError(
            f"input must be a regular file no larger than {max_bytes} bytes: {source}",
            exit_code=EXIT_INTEGRITY,
        )
    entry = _Entry(
        absolute=absolute,
        relative="input",
        identity=_identity(info),
        writable=bool(info.st_mode & 0o222),
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise MediaIntegrityError(
            f"cannot open input read-only: {source}: {exc}", exit_code=EXIT_INTEGRITY
        ) from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    copied = 0
    try:
        before = os.fstat(source_descriptor)
        if _identity(before) != entry.identity or not stat.S_ISREG(before.st_mode):
            raise MediaIntegrityError(
                f"input changed before staging: {source}", exit_code=EXIT_INTEGRITY
            )
        with os.fdopen(destination_descriptor, "wb") as output:
            while True:
                chunk = os.read(source_descriptor, HASH_CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise MediaIntegrityError(
                        f"input exceeds the {max_bytes} byte staging limit: {source}",
                        exit_code=EXIT_INTEGRITY,
                    )
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(source_descriptor)
        if _identity(after) != entry.identity or copied != info.st_size:
            raise MediaIntegrityError(
                f"input changed while staging: {source}", exit_code=EXIT_INTEGRITY
            )
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_descriptor)
    return {"size": copied, "sha256": digest.hexdigest()}
