from __future__ import annotations

import hashlib
import stat
import struct
from pathlib import Path

import pytest

from amipro_oracle import media as media_module
from amipro_oracle.constants import EXIT_INTEGRITY, MEDIA_SCHEMA
from amipro_oracle.errors import MediaIntegrityError
from amipro_oracle.fat12 import EXTRACTION_SCHEMA, extract_fat12_root_images
from amipro_oracle.io import digest_json
from amipro_oracle.media import inventory_media


def _set_fat12_entry(fat: bytearray, cluster: int, value: int) -> None:
    offset = cluster + cluster // 2
    if cluster & 1:
        pair = fat[offset] | (fat[offset + 1] << 8)
        pair = (pair & 0x000F) | ((value & 0x0FFF) << 4)
    else:
        pair = fat[offset] | (fat[offset + 1] << 8)
        pair = (pair & 0xF000) | (value & 0x0FFF)
    fat[offset] = pair & 0xFF
    fat[offset + 1] = pair >> 8


def _fat12_image(files: list[tuple[str, bytes]]) -> bytes:
    bytes_per_sector = 512
    total_sectors = 40
    image = bytearray(bytes_per_sector * total_sectors)
    image[:3] = b"\xeb\x3c\x90"
    image[3:11] = b"PYTEST  "
    struct.pack_into("<H", image, 11, bytes_per_sector)
    image[13] = 1
    struct.pack_into("<H", image, 14, 1)
    image[16] = 2
    struct.pack_into("<H", image, 17, 16)
    struct.pack_into("<H", image, 19, total_sectors)
    image[21] = 0xF0
    struct.pack_into("<H", image, 22, 1)
    image[510:512] = b"\x55\xaa"

    fat = bytearray(bytes_per_sector)
    fat[:3] = b"\xf0\xff\xff"
    root_start = 3 * bytes_per_sector
    data_start = 4 * bytes_per_sector
    next_cluster = 2
    for index, (name, payload) in enumerate(files):
        stem, _, extension = name.partition(".")
        entry = root_start + index * 32
        image[entry : entry + 8] = stem.upper().encode("ascii").ljust(8, b" ")
        image[entry + 8 : entry + 11] = extension.upper().encode("ascii").ljust(3, b" ")
        image[entry + 11] = 0x20
        dos_time = (3 << 11) | (4 << 5) | 3
        dos_date = ((2020 - 1980) << 9) | (1 << 5) | 2
        struct.pack_into("<H", image, entry + 22, dos_time)
        struct.pack_into("<H", image, entry + 24, dos_date)
        cluster_count = (len(payload) + bytes_per_sector - 1) // bytes_per_sector
        struct.pack_into("<H", image, entry + 26, next_cluster if cluster_count else 0)
        struct.pack_into("<I", image, entry + 28, len(payload))
        for position in range(cluster_count):
            cluster = next_cluster + position
            following = 0xFFF if position + 1 == cluster_count else cluster + 1
            _set_fat12_entry(fat, cluster, following)
            start = data_start + (cluster - 2) * bytes_per_sector
            chunk = payload[position * bytes_per_sector : (position + 1) * bytes_per_sector]
            image[start : start + len(chunk)] = chunk
        next_cluster += cluster_count
    image[bytes_per_sector : 2 * bytes_per_sector] = fat
    image[2 * bytes_per_sector : 3 * bytes_per_sector] = fat
    return bytes(image)


def _inventory(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    identity = {
        "schema": MEDIA_SCHEMA,
        "kind": "synthetic-fat12",
        "files": [
            {
                "path": path.name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    return {**identity, "digest": digest_json(identity)}


def test_fat12_extraction_is_verified_collision_free_and_deterministic(
    tmp_path: Path,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    image = media / "Disk01.img"
    image.write_bytes(
        _fat12_image(
            [
                ("ALPHA.TXT", b"alpha"),
                ("BIG.BIN", bytes(range(256)) * 3),
            ]
        )
    )
    image.chmod(0o666)

    first = extract_fat12_root_images(media, _inventory(image), tmp_path / "first")
    second = extract_fat12_root_images(media, _inventory(image), tmp_path / "second")

    assert first == second
    assert first["schema"] == EXTRACTION_SCHEMA
    assert first["file_count"] == 2
    assert first["total_bytes"] == 773
    assert (tmp_path / "first" / "ALPHA.TXT").read_bytes() == b"alpha"
    assert (tmp_path / "first" / "BIG.BIN").read_bytes() == bytes(range(256)) * 3
    assert stat.S_IMODE((tmp_path / "first" / "ALPHA.TXT").stat().st_mode) == 0o444
    assert first["files"][0]["modified_utc"] == "2020-01-02T03:04:06Z"


def test_fat12_extraction_rechecks_inventory_hash_and_rejects_cross_disk_collision(
    tmp_path: Path,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    first = media / "Disk01.img"
    second = media / "Disk02.img"
    first.write_bytes(_fat12_image([("SAME.TXT", b"one")]))
    second.write_bytes(_fat12_image([("same.txt", b"two")]))
    first_record = _inventory(first)["files"][0]
    second_record = _inventory(second)["files"][0]
    identity = {
        "schema": MEDIA_SCHEMA,
        "kind": "synthetic-fat12",
        "files": [first_record, second_record],
    }
    combined = {**identity, "digest": digest_json(identity)}

    with pytest.raises(MediaIntegrityError, match="duplicate case-insensitive") as collision:
        extract_fat12_root_images(media, combined, tmp_path / "collision")
    assert collision.value.exit_code == EXIT_INTEGRITY

    poisoned = _inventory(first)
    poisoned["files"][0]["sha256"] = "0" * 64
    poisoned_identity = {
        "schema": poisoned["schema"],
        "kind": poisoned["kind"],
        "files": poisoned["files"],
    }
    poisoned["digest"] = digest_json(poisoned_identity)
    with pytest.raises(MediaIntegrityError, match="inventory hash") as changed:
        extract_fat12_root_images(media, poisoned, tmp_path / "changed")
    assert changed.value.exit_code == EXIT_INTEGRITY

    for unsafe in ("/", ".."):
        escaped_identity = {
            "schema": MEDIA_SCHEMA,
            "kind": "synthetic-fat12",
            "files": [
                {
                    "path": unsafe,
                    "size": first.stat().st_size,
                    "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
                }
            ],
        }
        escaped = {**escaped_identity, "digest": digest_json(escaped_identity)}
        with pytest.raises(MediaIntegrityError, match="invalid floppy-image identity"):
            extract_fat12_root_images(media, escaped, tmp_path / f"unsafe-{unsafe!r}")


def test_windows_inventory_selects_only_the_exact_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "windows"
    media.mkdir()
    first = media / "Disk01.img"
    second = media / "Disk02.img"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    (media / "provenance.txt").write_text("not guest media", encoding="utf-8")
    expected = {
        "disk01.img": hashlib.sha256(b"one").hexdigest(),
        "disk02.img": hashlib.sha256(b"two").hexdigest(),
    }
    monkeypatch.setattr(media_module, "EXPECTED_WIN31_FLOPPY_SHA256", expected)

    result = inventory_media(media, kind="windows-3.1")

    assert result["media_profile"] == "supplied-windows-3.1-english-six-floppy-v1"
    assert [record["path"] for record in result["files"]] == ["Disk01.img", "Disk02.img"]
    assert [record["path"] for record in result["ignored_files"]] == ["provenance.txt"]
    assert result["file_count"] == 2

    second.write_bytes(b"wrong")
    with pytest.raises(MediaIntegrityError, match="six-floppy"):
        inventory_media(media, kind="windows-3.1")


def test_fat12_extraction_bounds_forged_inventory_before_reading(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    records = [
        {"path": f"disk{index:02d}.img", "size": 0, "sha256": "0" * 64}
        for index in range(65)
    ]
    identity = {"schema": MEDIA_SCHEMA, "kind": "synthetic-fat12", "files": records}

    with pytest.raises(MediaIntegrityError, match="64 image limit"):
        extract_fat12_root_images(
            media,
            {**identity, "digest": digest_json(identity)},
            tmp_path / "output",
        )
