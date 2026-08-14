from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ne import NEFormatError, VerificationError, index_verified, parse_ne  # noqa: E402

NE_OFFSET = 0x40
SEGMENT_TABLE_RELATIVE = 0x40
RESOURCE_TABLE_RELATIVE = 0x48
ALIGNMENT_SHIFT = 4


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _name_entry(name: str, ordinal: int) -> bytes:
    encoded = name.encode("ascii")
    return bytes([len(encoded)]) + encoded + struct.pack("<H", ordinal)


def _resource_table(named_identifiers: bool, count: int = 1) -> bytes:
    table = bytearray(struct.pack("<H", ALIGNMENT_SHIFT))
    table.extend(struct.pack("<HHI", 0, count, 0x12345678))
    for _ in range(count):
        table.extend(struct.pack("<HHHHHH", 0, 1, 0x0030, 0, 0, 0))
    table.extend(struct.pack("<H", 0))
    if named_identifiers:
        type_offset = len(table)
        table.extend(b"\x03TYP")
        identifier_offset = len(table)
        table.extend(b"\x03ONE")
        struct.pack_into("<H", table, 2, type_offset)
        for index in range(count):
            struct.pack_into("<H", table, 16 + index * 12, identifier_offset)
    else:
        struct.pack_into("<H", table, 2, 0x8006)
        for index in range(count):
            struct.pack_into("<H", table, 16 + index * 12, 0x8001 + index)
    return bytes(table)


def _entry_table() -> bytes:
    table = bytearray()
    table.extend((2, 0))  # ordinals 1 and 2 are unused
    table.extend((1, 1, 0x01))  # ordinal 3 is fixed in segment 1
    table.extend(struct.pack("<H", 4))
    table.extend((1, 0xFE, 0))  # ordinal 4 is a constant
    table.extend(struct.pack("<H", 0x1234))
    table.extend((1, 0xFF, 0x03))  # ordinal 5 is movable
    table.extend(struct.pack("<H", 0x3FCD))
    table.extend((1,))
    table.extend(struct.pack("<H", 6))
    table.extend((0,))
    return bytes(table)


def _direct_segment() -> bytes:
    segment = bytearray(16)
    for offset, next_offset in ((0, 0xFFFF), (2, 10), (6, 0xFFFF), (10, 0xFFFF)):
        struct.pack_into("<H", segment, offset, next_offset)
    return bytes(segment)


def _iterated_segment() -> bytes:
    return struct.pack("<HH", 3, 2) + b"AB" + struct.pack("<HH", 1, 1) + b"Z"


def _relocations() -> bytes:
    records = [
        # Internal movable-entry ordinal 5, linked source at offset 0.
        struct.pack("<BBHHH", 2, 0, 0, 0x00FF, 5),
        # Imported ordinal, with two linked fixup sites at offsets 2 and 10.
        struct.pack("<BBHHH", 3, 1, 2, 1, 17),
        # Imported name at imported-name-table offset 7.
        struct.pack("<BBHHH", 5, 2, 6, 1, 7),
        # Additive import has one direct fixup site and no linked list.
        struct.pack("<BBHHH", 5, 1 | 4, 8, 1, 21),
    ]
    return struct.pack("<H", len(records)) + b"".join(records)


def invented_ne(
    *,
    iterated: bool = False,
    named_resources: bool = False,
    self_loading_two_segments: bool = False,
    overlapping_resources: bool = False,
) -> bytes:
    """Build an entirely invented but structurally representative Windows NE."""

    resource_count = 2 if overlapping_resources else 1
    segment_count = 2 if self_loading_two_segments else 1
    resource_table_relative = SEGMENT_TABLE_RELATIVE + segment_count * 8
    resource_table = _resource_table(named_resources, resource_count)
    resident_table = _name_entry("SYNTHNE", 0) + _name_entry("START", 5) + b"\0"
    imported_table = b"\x06KERNEL\x04OPEN"
    entry_table = _entry_table()
    nonresident_table = _name_entry("Synthetic fixture", 0) + _name_entry("ALT", 5) + b"\0"

    resident_relative = resource_table_relative + len(resource_table)
    module_relative = _align(resident_relative + len(resident_table), 2)
    import_relative = module_relative + 2
    entry_relative = import_relative + len(imported_table)
    nonresident_offset = _align(NE_OFFSET + entry_relative + len(entry_table), 16)

    segment_data = _iterated_segment() if iterated else _direct_segment()
    second_segment_data = b"\x01\xff\x00" if self_loading_two_segments else b""
    relocation_data = _relocations()
    segment_file_offset = _align(
        nonresident_offset + len(nonresident_table), 16
    )
    first_segment_end = segment_file_offset + len(segment_data) + len(relocation_data)
    second_segment_file_offset = _align(first_segment_end, 16)
    segments_end = (
        second_segment_file_offset + len(second_segment_data)
        if self_loading_two_segments
        else first_segment_end
    )
    resource_file_offset = _align(segments_end, 16)
    file_size = resource_file_offset + 16
    data = bytearray(file_size)

    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, NE_OFFSET)
    data[NE_OFFSET : NE_OFFSET + 2] = b"NE"
    data[NE_OFFSET + 2] = 5
    data[NE_OFFSET + 3] = 60
    struct.pack_into("<H", data, NE_OFFSET + 0x04, entry_relative)
    struct.pack_into("<H", data, NE_OFFSET + 0x06, len(entry_table))
    struct.pack_into("<I", data, NE_OFFSET + 0x08, 0)
    module_flags = 0x8301 | (0x0800 if self_loading_two_segments else 0)
    struct.pack_into("<H", data, NE_OFFSET + 0x0C, module_flags)
    struct.pack_into("<H", data, NE_OFFSET + 0x0E, 1)
    struct.pack_into("<H", data, NE_OFFSET + 0x10, 32)
    struct.pack_into("<H", data, NE_OFFSET + 0x12, 16)
    struct.pack_into("<I", data, NE_OFFSET + 0x14, (1 << 16) | 6)
    struct.pack_into("<I", data, NE_OFFSET + 0x18, (1 << 16) | 16)
    struct.pack_into("<H", data, NE_OFFSET + 0x1C, segment_count)
    struct.pack_into("<H", data, NE_OFFSET + 0x1E, 1)
    struct.pack_into("<H", data, NE_OFFSET + 0x20, len(nonresident_table))
    struct.pack_into("<H", data, NE_OFFSET + 0x22, SEGMENT_TABLE_RELATIVE)
    struct.pack_into("<H", data, NE_OFFSET + 0x24, resource_table_relative)
    struct.pack_into("<H", data, NE_OFFSET + 0x26, resident_relative)
    struct.pack_into("<H", data, NE_OFFSET + 0x28, module_relative)
    struct.pack_into("<H", data, NE_OFFSET + 0x2A, import_relative)
    struct.pack_into("<I", data, NE_OFFSET + 0x2C, nonresident_offset)
    struct.pack_into("<H", data, NE_OFFSET + 0x30, 1)
    struct.pack_into("<H", data, NE_OFFSET + 0x32, ALIGNMENT_SHIFT)
    struct.pack_into("<H", data, NE_OFFSET + 0x34, 1)
    data[NE_OFFSET + 0x36] = 2
    data[NE_OFFSET + 0x37] = 0
    struct.pack_into("<H", data, NE_OFFSET + 0x3E, 0x030A)

    segment_flags = 0x0100 | 0x0040 | 0x0800
    if iterated:
        segment_flags |= 0x0008
    struct.pack_into(
        "<HHHH",
        data,
        NE_OFFSET + SEGMENT_TABLE_RELATIVE,
        segment_file_offset >> ALIGNMENT_SHIFT,
        len(segment_data),
        segment_flags,
        16,
    )
    if self_loading_two_segments:
        struct.pack_into(
            "<HHHH",
            data,
            NE_OFFSET + SEGMENT_TABLE_RELATIVE + 8,
            second_segment_file_offset >> ALIGNMENT_SHIFT,
            len(second_segment_data),
            0x0008 | 0x0040 | 0x0800,
            16,
        )

    resource_start = NE_OFFSET + resource_table_relative
    data[resource_start : resource_start + len(resource_table)] = resource_table
    resident_start = NE_OFFSET + resident_relative
    data[resident_start : resident_start + len(resident_table)] = resident_table
    module_start = NE_OFFSET + module_relative
    struct.pack_into("<H", data, module_start, 0)
    import_start = NE_OFFSET + import_relative
    data[import_start : import_start + len(imported_table)] = imported_table
    entry_start = NE_OFFSET + entry_relative
    data[entry_start : entry_start + len(entry_table)] = entry_table
    data[
        nonresident_offset : nonresident_offset + len(nonresident_table)
    ] = nonresident_table
    data[
        segment_file_offset : segment_file_offset + len(segment_data)
    ] = segment_data
    relocation_start = segment_file_offset + len(segment_data)
    data[relocation_start : relocation_start + len(relocation_data)] = relocation_data
    if self_loading_two_segments:
        data[
            second_segment_file_offset : second_segment_file_offset
            + len(second_segment_data)
        ] = second_segment_data
    data[resource_file_offset : resource_file_offset + 16] = b"synthetic-rsrc!!"

    resource_record = resource_start + 10
    for index in range(resource_count):
        struct.pack_into(
            "<H",
            data,
            resource_record + index * 12,
            resource_file_offset >> ALIGNMENT_SHIFT,
        )
    return bytes(data)


def test_indexes_direct_ne_structure_and_relocation_chains() -> None:
    data = invented_ne()
    result = parse_ne(data)

    assert result == parse_ne(data)
    json.dumps(result, sort_keys=True)
    assert result["header"]["segment_count"] == 1
    assert result["header"]["target_os"] == "windows"
    assert result["header"]["resource_segment_count"] == 1
    assert "resource_count_matches_header" not in result["header"]
    assert result["module_references"] == [
        {"index": 1, "name_offset": 0, "name": "KERNEL"}
    ]
    assert [entry["kind"] for entry in result["entry_table"]] == [
        "unused",
        "unused",
        "fixed",
        "constant",
        "movable",
    ]
    assert result["entry_table"][4]["names"] == [
        {"table": "resident", "name": "START"},
        {"table": "nonresident", "name": "ALT"},
    ]

    segment = result["segments"][0]
    assert segment["storage"] == "direct"
    assert "selfload" in segment["flags"]
    assert segment["iterated"] is None
    assert segment["relocations"][0]["target"] == {
        "kind": "internal_entry",
        "ordinal": 5,
    }
    assert segment["relocations"][1]["fixup_chain"]["offsets"] == [2, 10]
    assert segment["relocations"][1]["source_width"] == 4
    assert segment["relocations"][2]["source_width"] == 2
    assert segment["relocations"][2]["target"]["name"] == "OPEN"
    assert segment["relocations"][3]["fixup_chain"] == {
        "status": "resolved",
        "encoding": "additive",
        "offsets": [8],
    }


def test_indexes_iterated_metadata_without_claiming_a_loaded_mapping() -> None:
    result = parse_ne(invented_ne(iterated=True))
    segment = result["segments"][0]

    assert segment["storage"] == "iterated"
    assert "iterated" in segment["flags"]
    assert segment["iterated"]["record_count"] == 2
    assert segment["iterated"]["expanded_size"] == 7
    assert segment["iterated"]["records"][0]["repeat_count"] == 3
    assert segment["relocations"][0]["fixup_chain"]["status"] == "unresolved"


def test_selfloader_uses_standard_segment_one_and_custom_later_segments() -> None:
    result = parse_ne(
        invented_ne(iterated=True, self_loading_two_segments=True)
    )
    first, second = result["segments"]

    assert first["mapping_status"] == "standard_iterated_metadata_only"
    assert first["decoded_size"] == 7
    assert first["iterated"]["record_count"] == 2
    assert second["mapping_status"] == "unsupported_selfload"
    assert second["decoded_size"] is None
    assert second["iterated"]["status"] == "unsupported_selfload"
    assert second["iterated"]["records"] == []


def test_resource_names_are_omitted_by_default_and_opt_in() -> None:
    data = invented_ne(named_resources=True)
    default = parse_ne(data)
    opted_in = parse_ne(data, include_resource_names=True)

    default_type = default["resources"]["types"][0]["identifier"]
    default_id = default["resources"]["types"][0]["resources"][0]["identifier"]
    assert default_type == {"kind": "name", "offset": 24, "length": 3, "raw": 24}
    assert default_id == {"kind": "name", "offset": 28, "length": 3, "raw": 28}
    assert opted_in["resources"]["types"][0]["identifier"]["name"] == "TYP"
    assert opted_in["resources"]["types"][0]["resources"][0]["identifier"][
        "name"
    ] == "ONE"


def test_same_descriptor_hash_and_size_gate(tmp_path: Path) -> None:
    data = invented_ne()
    candidate = tmp_path / "invented.ne"
    candidate.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    result = index_verified(
        candidate,
        expected_size=len(data),
        expected_sha256=digest,
    )
    assert result["input"] == {"size": len(data), "sha256": digest}
    assert result["index"]["format"] == "NE"

    with pytest.raises(VerificationError, match="SHA-256 mismatch"):
        index_verified(candidate, expected_size=len(data), expected_sha256="0" * 64)
    with pytest.raises(VerificationError, match="size mismatch"):
        index_verified(candidate, expected_size=len(data) + 1, expected_sha256=digest)


def test_same_descriptor_gate_rejects_symlink(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("platform has no O_NOFOLLOW")
    target = tmp_path / "invented.ne"
    target.write_bytes(invented_ne())
    link = tmp_path / "link.ne"
    link.symlink_to(target)
    with pytest.raises(VerificationError, match="cannot open input safely"):
        index_verified(
            link,
            expected_size=target.stat().st_size,
            expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda data: struct.pack_into(
                "<H", data, NE_OFFSET + 0x26, RESOURCE_TABLE_RELATIVE - 1
            ),
            "resident-name table offset",
        ),
        (
            lambda data: data.__setitem__(
                NE_OFFSET + _resource_table(False).__len__() + RESOURCE_TABLE_RELATIVE,
                0xFF,
            ),
            "Pascal string is truncated",
        ),
        (
            lambda data: struct.pack_into(
                "<H", data, NE_OFFSET + SEGMENT_TABLE_RELATIVE, 0xFFFF
            ),
            "segment 1 data range is outside input",
        ),
    ],
)
def test_rejects_malformed_table_and_file_bounds(mutator: object, message: str) -> None:
    data = bytearray(invented_ne())
    assert callable(mutator)
    mutator(data)
    with pytest.raises(NEFormatError, match=message):
        parse_ne(bytes(data))


def test_rejects_iterated_expansion_beyond_allocation() -> None:
    data = bytearray(invented_ne(iterated=True))
    segment_sector = struct.unpack_from(
        "<H", data, NE_OFFSET + SEGMENT_TABLE_RELATIVE
    )[0]
    segment_start = segment_sector << ALIGNMENT_SHIFT
    struct.pack_into("<H", data, segment_start, 100)
    with pytest.raises(NEFormatError, match="expands to"):
        parse_ne(bytes(data))


def test_rejects_cyclic_relocation_source_chain() -> None:
    data = bytearray(invented_ne())
    segment_sector = struct.unpack_from(
        "<H", data, NE_OFFSET + SEGMENT_TABLE_RELATIVE
    )[0]
    segment_start = segment_sector << ALIGNMENT_SHIFT
    struct.pack_into("<H", data, segment_start, 0)
    with pytest.raises(NEFormatError, match="source chain cycles"):
        parse_ne(bytes(data))


def test_rejects_overlapping_payload_ranges() -> None:
    data = bytearray(invented_ne())
    resource_record = NE_OFFSET + RESOURCE_TABLE_RELATIVE + 10
    segment_sector = struct.unpack_from(
        "<H", data, NE_OFFSET + SEGMENT_TABLE_RELATIVE
    )[0]
    struct.pack_into("<H", data, resource_record, segment_sector)
    with pytest.raises(NEFormatError, match="payload overlap"):
        parse_ne(bytes(data))


def test_rejects_overlapping_resource_ranges_before_hashing() -> None:
    with pytest.raises(NEFormatError, match="resource payload overlap"):
        parse_ne(invented_ne(overlapping_resources=True))
