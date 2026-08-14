from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import EXIT_INTEGRITY, HASH_CHUNK_BYTES
from .errors import MediaIntegrityError
from .io import atomic_write, digest_json

EXTRACTION_SCHEMA = "amipro-oracle-fat12-extraction-v1"
_MAX_FLOPPY_BYTES = 4 * 1024 * 1024
_MAX_IMAGE_COUNT = 64
_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_EXTRACTED_FILES = 4096


@dataclass(frozen=True)
class _FatFile:
    name: str
    payload: bytes
    attributes: int
    modified_utc: str
    modified_timestamp: float


def _fail(message: str) -> MediaIntegrityError:
    return MediaIntegrityError(message, exit_code=EXIT_INTEGRITY)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_verified_image(path: Path, *, expected_size: int, expected_hash: str) -> bytes:
    try:
        before_path = path.lstat()
    except OSError as exc:
        raise _fail(f"cannot inspect floppy image {path}: {exc}") from exc
    if not stat.S_ISREG(before_path.st_mode) or before_path.st_size != expected_size:
        raise _fail(f"floppy image changed after inventory: {path}")
    if expected_size > _MAX_FLOPPY_BYTES:
        raise _fail(f"floppy image exceeds the {_MAX_FLOPPY_BYTES} byte limit: {path}")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _fail(f"cannot open floppy image read-only: {path}: {exc}") from exc
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if _identity(before) != _identity(before_path) or not stat.S_ISREG(before.st_mode):
            raise _fail(f"floppy image changed before extraction: {path}")
        total = 0
        while True:
            chunk = os.read(descriptor, HASH_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_FLOPPY_BYTES:
                raise _fail(f"floppy image exceeds the extraction limit: {path}")
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _identity(after) != _identity(before_path):
            raise _fail(f"floppy image changed during extraction: {path}")
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) != expected_size or digest.hexdigest() != expected_hash:
        raise _fail(f"floppy image no longer matches its inventory hash: {path}")
    return payload


def _u16(payload: bytes, offset: int) -> int:
    return struct.unpack_from("<H", payload, offset)[0]


def _u32(payload: bytes, offset: int) -> int:
    return struct.unpack_from("<I", payload, offset)[0]


def _fat12_value(fat: bytes, cluster: int) -> int:
    offset = cluster + cluster // 2
    if offset + 1 >= len(fat):
        raise _fail("FAT12 cluster entry is outside the allocation table")
    pair = fat[offset] | (fat[offset + 1] << 8)
    return (pair >> 4) & 0xFFF if cluster & 1 else pair & 0xFFF


def _dos_timestamp(entry: bytes) -> tuple[str, float]:
    raw_time = _u16(entry, 22)
    raw_date = _u16(entry, 24)
    year = 1980 + ((raw_date >> 9) & 0x7F)
    month = (raw_date >> 5) & 0x0F
    day = raw_date & 0x1F
    hour = (raw_time >> 11) & 0x1F
    minute = (raw_time >> 5) & 0x3F
    second = (raw_time & 0x1F) * 2
    try:
        value = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError as exc:
        raise _fail("invalid FAT12 directory timestamp") from exc
    return value.isoformat().replace("+00:00", "Z"), value.timestamp()


def _dos_name(entry: bytes) -> str:
    raw_stem = bytearray(entry[:8])
    if raw_stem and raw_stem[0] == 0x05:
        raw_stem[0] = 0xE5
    stem = bytes(raw_stem).rstrip(b" ").decode("cp437")
    extension = entry[8:11].rstrip(b" ").decode("cp437")
    name = f"{stem}.{extension}" if extension else stem
    candidate = PurePosixPath(name)
    if (
        not stem
        or name in {".", ".."}
        or len(candidate.parts) != 1
        or any(character in name for character in ("/", "\\", "\x00", "\n", "\r"))
    ):
        raise _fail(f"unsafe FAT12 root filename: {name!r}")
    return name


def _parse_root(payload: bytes) -> list[_FatFile]:
    if len(payload) < 512 or payload[510:512] != b"\x55\xaa":
        raise _fail("floppy image has no valid DOS boot-sector signature")
    bytes_per_sector = _u16(payload, 11)
    sectors_per_cluster = payload[13]
    reserved_sectors = _u16(payload, 14)
    fat_count = payload[16]
    root_entries = _u16(payload, 17)
    total_sectors = _u16(payload, 19) or _u32(payload, 32)
    sectors_per_fat = _u16(payload, 22)
    if (
        bytes_per_sector not in {512, 1024, 2048, 4096}
        or sectors_per_cluster == 0
        or sectors_per_cluster & (sectors_per_cluster - 1)
        or reserved_sectors == 0
        or fat_count < 2
        or root_entries == 0
        or sectors_per_fat == 0
        or total_sectors * bytes_per_sector != len(payload)
    ):
        raise _fail("unsupported or inconsistent FAT12 BIOS parameter block")

    root_sectors = (root_entries * 32 + bytes_per_sector - 1) // bytes_per_sector
    first_fat = reserved_sectors * bytes_per_sector
    fat_bytes = sectors_per_fat * bytes_per_sector
    root_start = (reserved_sectors + fat_count * sectors_per_fat) * bytes_per_sector
    data_start_sector = reserved_sectors + fat_count * sectors_per_fat + root_sectors
    data_sectors = total_sectors - data_start_sector
    cluster_count = data_sectors // sectors_per_cluster
    cluster_bytes = sectors_per_cluster * bytes_per_sector
    if cluster_count <= 0 or cluster_count >= 4085:
        raise _fail("image is not a FAT12 filesystem")
    if root_start + root_entries * 32 > len(payload):
        raise _fail("FAT12 root directory is outside the image")

    fat = payload[first_fat : first_fat + fat_bytes]
    if len(fat) != fat_bytes or fat[:1] != payload[21:22] or fat[1:3] != b"\xff\xff":
        raise _fail("invalid FAT12 allocation-table header")
    for index in range(1, fat_count):
        start = first_fat + index * fat_bytes
        if payload[start : start + fat_bytes] != fat:
            raise _fail("FAT12 allocation-table copies differ")
    _fat12_value(fat, cluster_count + 1)

    files: list[_FatFile] = []
    names: set[str] = set()
    allocated: set[int] = set()
    for index in range(root_entries):
        start = root_start + index * 32
        entry = payload[start : start + 32]
        if entry[0] == 0x00:
            break
        if entry[0] == 0xE5:
            continue
        attributes = entry[11]
        if attributes == 0x0F:
            raise _fail("long filenames are not supported in the pinned FAT12 profile")
        if attributes & 0x08:
            continue
        if attributes & 0x10:
            raise _fail("subdirectories are not supported in the pinned FAT12 profile")
        name = _dos_name(entry)
        folded = name.casefold()
        if folded in names:
            raise _fail(f"duplicate case-insensitive FAT12 filename: {name}")
        names.add(folded)
        first_cluster = _u16(entry, 26)
        file_size = _u32(entry, 28)
        required_clusters = (file_size + cluster_bytes - 1) // cluster_bytes
        if required_clusters == 0:
            if first_cluster != 0:
                raise _fail(f"zero-length FAT12 file has a cluster: {name}")
            file_payload = b""
        else:
            if first_cluster < 2 or first_cluster >= cluster_count + 2:
                raise _fail(f"invalid first FAT12 cluster for {name}")
            pieces: list[bytes] = []
            cluster = first_cluster
            seen: set[int] = set()
            for position in range(required_clusters):
                if cluster < 2 or cluster >= cluster_count + 2 or cluster in seen:
                    raise _fail(f"invalid or cyclic FAT12 chain for {name}")
                if cluster in allocated:
                    raise _fail(f"cross-linked FAT12 cluster for {name}")
                seen.add(cluster)
                allocated.add(cluster)
                offset = (data_start_sector + (cluster - 2) * sectors_per_cluster)
                offset *= bytes_per_sector
                pieces.append(payload[offset : offset + cluster_bytes])
                next_cluster = _fat12_value(fat, cluster)
                if position + 1 < required_clusters:
                    if next_cluster >= 0xFF8:
                        raise _fail(f"short FAT12 chain for {name}")
                    cluster = next_cluster
                elif next_cluster < 0xFF8:
                    raise _fail(f"overlong FAT12 chain for {name}")
            file_payload = b"".join(pieces)[:file_size]
        modified_utc, modified_timestamp = _dos_timestamp(entry)
        files.append(
            _FatFile(
                name=name,
                payload=file_payload,
                attributes=attributes,
                modified_utc=modified_utc,
                modified_timestamp=modified_timestamp,
            )
        )
    return files


def extract_fat12_root_images(
    media_root: Path,
    media_inventory: dict[str, Any],
    destination: Path,
) -> dict[str, Any]:
    if media_inventory.get("schema") != "amipro-oracle-media-v1":
        raise _fail("invalid media inventory schema for FAT12 extraction")
    records = media_inventory.get("files")
    if not isinstance(records, list) or not records:
        raise _fail("media inventory has no files for FAT12 extraction")
    if len(records) > _MAX_IMAGE_COUNT:
        raise _fail(f"FAT12 extraction exceeds the {_MAX_IMAGE_COUNT} image limit")
    normalized_records: list[dict[str, object]] = []
    aggregate_bytes = 0
    for record in records:
        if not isinstance(record, dict):
            raise _fail("invalid file record in media inventory")
        relative = record.get("path")
        size = record.get("size")
        expected_hash = record.get("sha256")
        path = PurePosixPath(relative) if isinstance(relative, str) else None
        if (
            path is None
            or path.is_absolute()
            or path.as_posix() != relative
            or relative in {"", ".", ".."}
            or len(path.parts) != 1
            or not isinstance(size, int)
            or size < 0
            or not isinstance(expected_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        ):
            raise _fail("invalid floppy-image identity in media inventory")
        aggregate_bytes += size
        if aggregate_bytes > _MAX_IMAGE_BYTES:
            raise _fail(f"FAT12 extraction exceeds the {_MAX_IMAGE_BYTES} byte media limit")
        normalized_records.append(
            {"path": relative, "size": size, "sha256": expected_hash}
        )
    identity = {
        "schema": media_inventory["schema"],
        "kind": media_inventory.get("kind"),
        "files": normalized_records,
    }
    if media_inventory.get("digest") != digest_json(identity):
        raise _fail("FAT12 media inventory digest does not match its file records")
    root = media_root.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise _fail(f"FAT12 media root must be a real directory: {root}")
    if destination.exists() or destination.is_symlink():
        raise _fail(f"FAT12 extraction destination must not exist: {destination}")
    destination.mkdir(parents=True, exist_ok=False)

    output_records: list[dict[str, object]] = []
    output_names: set[str] = set()
    try:
        for record in normalized_records:
            relative = str(record["path"])
            size = int(record["size"])
            expected_hash = str(record["sha256"])
            image = _read_verified_image(
                root / relative,
                expected_size=size,
                expected_hash=expected_hash,
            )
            for extracted in _parse_root(image):
                folded = extracted.name.casefold()
                if folded in output_names:
                    raise _fail(
                        f"floppy images contain a duplicate case-insensitive file: "
                        f"{extracted.name}"
                    )
                output_names.add(folded)
                if len(output_names) > _MAX_EXTRACTED_FILES:
                    raise _fail(
                        f"FAT12 extraction exceeds the {_MAX_EXTRACTED_FILES} file limit"
                    )
                output = destination / extracted.name
                atomic_write(output, extracted.payload)
                output.chmod(0o444)
                os.utime(
                    output,
                    (extracted.modified_timestamp, extracted.modified_timestamp),
                    follow_symlinks=False,
                )
                output_records.append(
                    {
                        "path": extracted.name,
                        "size": len(extracted.payload),
                        "sha256": hashlib.sha256(extracted.payload).hexdigest(),
                        "source_image": relative,
                        "attributes": extracted.attributes,
                        "modified_utc": extracted.modified_utc,
                    }
                )
    except BaseException:
        # Preserve partial extraction for forensic inspection; callers place it in an ignored job.
        raise

    identity = {
        "schema": EXTRACTION_SCHEMA,
        "source_media_digest": media_inventory.get("digest"),
        "files": output_records,
    }
    return {
        **identity,
        "digest": digest_json(identity),
        "file_count": len(output_records),
        "total_bytes": sum(int(record["size"]) for record in output_records),
    }
