#!/usr/bin/env python3
"""Bounded structural indexer for 16-bit MZ/NE executable containers.

The indexer never executes or writes input bytes.  ``index_verified`` hashes and
parses the bytes read from one already-open file descriptor, with before/after
``fstat`` checks and optional mandatory size/digest expectations.

Resource payloads are represented only by offsets, sizes, and flags.  Resource
identifier strings are validated but omitted unless ``include_resource_names``
is explicitly requested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_FILE_SIZE = 64 * 1024 * 1024
MAX_SEGMENTS = 4_096
MAX_MODULE_REFERENCES = 4_096
MAX_NAMES = 16_384
MAX_ENTRY_POINTS = 65_535
MAX_RESOURCE_TYPES = 512
MAX_RESOURCES = 4_096
MAX_TOTAL_RESOURCE_BYTES = 64 * 1024 * 1024
MAX_RELOCATIONS_PER_SEGMENT = 16_384
MAX_TOTAL_RELOCATIONS = 65_536
MAX_FIXUP_SITES_PER_RELOCATION = 32_768
MAX_TOTAL_FIXUP_SITES = 262_144
MAX_ITERATED_RECORDS_PER_SEGMENT = 16_384
MAX_TOTAL_ITERATED_RECORDS = 65_536
MAX_DECODED_SEGMENT_SIZE = 65_536
MAX_TOTAL_DECODED_SIZE = 64 * 1024 * 1024
MAX_SHIFT = 31
READ_CHUNK_SIZE = 1024 * 1024

NE_HEADER_SIZE = 0x40
SEGMENT_TABLE_ENTRY_SIZE = 8
RELOCATION_ENTRY_SIZE = 8

SEGMENT_FLAGS: tuple[tuple[int, str], ...] = (
    (0x0001, "data"),
    (0x0002, "allocated"),
    (0x0004, "loaded"),
    (0x0008, "iterated"),
    (0x0010, "moveable"),
    (0x0020, "shareable"),
    (0x0040, "preload"),
    (0x0080, "read_or_execute_only"),
    (0x0100, "relocation_data"),
    (0x0800, "selfload"),
    (0x1000, "discardable"),
    (0x2000, "32_bit"),
)

MODULE_FLAGS: tuple[tuple[int, str], ...] = (
    (0x0001, "single_data"),
    (0x0002, "multiple_data"),
    (0x0010, "win32"),
    (0x0800, "selfload"),
    (0x2000, "link_error"),
    (0x4000, "call_wep"),
    (0x8000, "library"),
)

RESOURCE_FLAGS: tuple[tuple[int, str], ...] = (
    (0x0010, "moveable"),
    (0x0020, "pure"),
    (0x0040, "preload"),
)

SOURCE_TYPES = {
    0: ("low_byte", 1),
    2: ("selector", 2),
    3: ("pointer_16_16", 4),
    5: ("offset_16", 2),
    11: ("pointer_16_32", 6),
    13: ("offset_32", 4),
}

TARGET_OS = {
    0: "unknown",
    1: "os2",
    2: "windows",
    3: "european_ms_dos_4",
    4: "windows_386",
    5: "borland_operating_system_services",
}


class NEFormatError(ValueError):
    """Input is not a structurally valid, supported NE container."""


class VerificationError(RuntimeError):
    """An input failed the same-descriptor stat/hash gate."""


@dataclass(frozen=True)
class VerifiedInput:
    """Bytes and identity captured from a single stable file descriptor."""

    data: bytes
    size: int
    sha256: str
    device: int
    inode: int

    def public_metadata(self) -> dict[str, object]:
        """Return deterministic metadata suitable for a manifest or report."""

        return {"size": self.size, "sha256": self.sha256}


def _range(data: bytes, offset: int, size: int, label: str) -> tuple[int, int]:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise NEFormatError(
            f"{label} range is outside input: offset={offset:#x}, size={size:#x}"
        )
    return offset, offset + size


def _u8(data: bytes, offset: int, label: str) -> int:
    _range(data, offset, 1, label)
    return data[offset]


def _u16(data: bytes, offset: int, label: str) -> int:
    _range(data, offset, 2, label)
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int, label: str) -> int:
    _range(data, offset, 4, label)
    return struct.unpack_from("<I", data, offset)[0]


def _bounded_count(value: int, maximum: int, label: str) -> int:
    if value > maximum:
        raise NEFormatError(f"{label} {value} exceeds cap {maximum}")
    return value


def _shifted(value: int, shift: int, label: str) -> int:
    if not 0 <= shift <= MAX_SHIFT:
        raise NEFormatError(f"{label} shift {shift} exceeds cap {MAX_SHIFT}")
    result = value << shift
    if result > MAX_FILE_SIZE:
        raise NEFormatError(f"{label} shifted value {result:#x} exceeds file-size cap")
    return result


def _flag_names(value: int, definitions: tuple[tuple[int, str], ...]) -> list[str]:
    return [name for mask, name in definitions if value & mask]


def _decode_name(value: bytes) -> str:
    return value.decode("latin-1")


def _pascal_at(
    data: bytes,
    offset: int,
    limit: int,
    label: str,
) -> tuple[bytes, int]:
    if offset < 0 or offset >= limit or limit > len(data):
        raise NEFormatError(f"{label} offset {offset:#x} is outside its table")
    length = data[offset]
    end = offset + 1 + length
    if end > limit:
        raise NEFormatError(f"{label} Pascal string is truncated")
    return data[offset + 1 : end], end


def _parse_name_table(
    data: bytes,
    start: int,
    limit: int,
    label: str,
) -> tuple[list[dict[str, object]], int]:
    entries: list[dict[str, object]] = []
    cursor = start
    while True:
        if cursor >= limit:
            raise NEFormatError(f"{label} has no terminating zero-length name")
        length = data[cursor]
        if length == 0:
            return entries, cursor + 1
        _bounded_count(len(entries) + 1, MAX_NAMES, f"{label} name count")
        raw, string_end = _pascal_at(data, cursor, limit, f"{label} name")
        if string_end + 2 > limit:
            raise NEFormatError(f"{label} ordinal is truncated")
        ordinal = _u16(data, string_end, f"{label} ordinal")
        entries.append(
            {
                "file_offset": cursor,
                "table_offset": cursor - start,
                "name": _decode_name(raw),
                "ordinal": ordinal,
            }
        )
        cursor = string_end + 2


def _parse_imported_names(
    data: bytes,
    start: int,
    end: int,
) -> tuple[list[dict[str, object]], dict[int, dict[str, object]]]:
    names: list[dict[str, object]] = []
    by_offset: dict[int, dict[str, object]] = {}
    cursor = start
    while cursor < end:
        _bounded_count(len(names) + 1, MAX_NAMES, "imported-name count")
        raw, next_cursor = _pascal_at(data, cursor, end, "imported name")
        item: dict[str, object] = {
            "offset": cursor - start,
            "file_offset": cursor,
            "name": _decode_name(raw),
            "empty": not raw,
        }
        names.append(item)
        by_offset[cursor - start] = item
        cursor = next_cursor
    return names, by_offset


def _resource_identifier(
    data: bytes,
    raw: int,
    table_start: int,
    metadata_end: int,
    table_end: int,
    include_name: bool,
    label: str,
) -> dict[str, object]:
    if raw & 0x8000:
        return {"kind": "numeric", "id": raw & 0x7FFF, "raw": raw}

    name_offset = raw
    absolute = table_start + name_offset
    if absolute < metadata_end:
        raise NEFormatError(f"{label} name points into resource metadata")
    value, _ = _pascal_at(data, absolute, table_end, f"{label} resource name")
    result: dict[str, object] = {
        "kind": "name",
        "offset": name_offset,
        "length": len(value),
        "raw": raw,
    }
    if include_name:
        result["name"] = _decode_name(value)
    return result


def _parse_resources(
    data: bytes,
    start: int,
    end: int,
    include_names: bool,
) -> tuple[
    dict[str, object],
    list[tuple[int, int, str]],
    list[dict[str, object]],
]:
    if start == end:
        return {
            "alignment_shift": None,
            "types": [],
            "resource_count": 0,
            "total_unique_payload_bytes": 0,
        }, [], []
    _range(data, start, 2, "resource alignment shift")
    shift = _u16(data, start, "resource alignment shift")
    if shift > MAX_SHIFT:
        raise NEFormatError(f"resource alignment shift {shift} exceeds cap {MAX_SHIFT}")

    cursor = start + 2
    raw_types: list[tuple[int, int, list[tuple[int, ...]]]] = []
    total_resources = 0
    for type_index in range(MAX_RESOURCE_TYPES + 1):
        if cursor + 2 > end:
            raise NEFormatError("resource type table has no terminator")
        type_id = _u16(data, cursor, "resource type id")
        if type_id == 0:
            cursor += 2
            break
        if type_index == MAX_RESOURCE_TYPES:
            raise NEFormatError(f"resource type count exceeds cap {MAX_RESOURCE_TYPES}")
        if cursor + 8 > end:
            raise NEFormatError("resource type record overlaps the resident-name table")
        count = _u16(data, cursor + 2, "resource type count")
        _bounded_count(count, MAX_RESOURCES, "resources in one type")
        total_resources += count
        _bounded_count(total_resources, MAX_RESOURCES, "total resource count")
        reserved = _u32(data, cursor + 4, "resource type reserved field")
        cursor += 8
        records: list[tuple[int, ...]] = []
        for _ in range(count):
            if cursor + 12 > end:
                raise NEFormatError("resource record overlaps the resident-name table")
            records.append(struct.unpack_from("<HHHHHH", data, cursor))
            cursor += 12
        raw_types.append((type_id, reserved, records))
    else:  # pragma: no cover - loop always exits or raises at the explicit cap
        raise NEFormatError("resource type table did not terminate")

    metadata_end = cursor
    if metadata_end > end:
        raise NEFormatError("resource metadata overlaps the resident-name table")

    payload_ranges: list[tuple[int, int, str]] = []
    resource_items: list[dict[str, object]] = []
    types: list[dict[str, object]] = []
    resource_index = 0
    for type_id, reserved, records in raw_types:
        resources: list[dict[str, object]] = []
        for raw_offset, raw_length, flags, resource_id, handle, usage in records:
            resource_index += 1
            file_offset = _shifted(raw_offset, shift, "resource offset")
            size = _shifted(raw_length, shift, "resource length")
            _range(data, file_offset, size, f"resource {resource_index} payload")
            if size:
                payload_ranges.append(
                    (file_offset, file_offset + size, f"resource {resource_index} payload")
                )
            item: dict[str, object] = {
                "index": resource_index,
                "identifier": _resource_identifier(
                    data,
                    resource_id,
                    start,
                    metadata_end,
                    end,
                    include_names,
                    f"resource {resource_index}",
                ),
                "offset_units": raw_offset,
                "length_units": raw_length,
                "file_offset": file_offset,
                "size": size,
                "flags_raw": flags,
                "flags": _flag_names(flags, RESOURCE_FLAGS),
                "handle": handle,
                "usage": usage,
            }
            resources.append(item)
            resource_items.append(item)
        types.append(
            {
                "identifier": _resource_identifier(
                    data,
                    type_id,
                    start,
                    metadata_end,
                    end,
                    include_names,
                    "resource type",
                ),
                "reserved": reserved,
                "resources": resources,
            }
        )

    _validate_no_overlaps(payload_ranges, "resource payload")
    total_resource_bytes = sum(end - start for start, end, _ in payload_ranges)
    _bounded_count(
        total_resource_bytes,
        MAX_TOTAL_RESOURCE_BYTES,
        "total unique resource payload bytes",
    )
    return {
        "alignment_shift": shift,
        "metadata_size": metadata_end - start,
        "resource_count": total_resources,
        "total_unique_payload_bytes": total_resource_bytes,
        "types": types,
    }, payload_ranges, resource_items


def _parse_entry_table(
    data: bytes,
    start: int,
    size: int,
    segment_count: int,
) -> tuple[list[dict[str, object]], int]:
    _, end = _range(data, start, size, "entry table")
    if size == 0:
        return [], 0

    entries: list[dict[str, object]] = []
    cursor = start
    ordinal = 1
    movable_count = 0
    terminated = False
    while cursor < end:
        count = data[cursor]
        cursor += 1
        if count == 0:
            terminated = True
            if any(data[cursor:end]):
                raise NEFormatError("entry table has nonzero bytes after its terminator")
            break
        if cursor >= end:
            raise NEFormatError("entry-table bundle header is truncated")
        indicator = data[cursor]
        cursor += 1
        _bounded_count(
            len(entries) + count,
            MAX_ENTRY_POINTS,
            "entry-table ordinal count",
        )

        if indicator == 0:
            for _ in range(count):
                entries.append({"ordinal": ordinal, "kind": "unused", "names": []})
                ordinal += 1
            continue

        record_size = 6 if indicator == 0xFF else 3
        if count * record_size > end - cursor:
            raise NEFormatError("entry-table bundle records are truncated")
        for _ in range(count):
            flags = data[cursor]
            if indicator == 0xFF:
                interrupt = _u16(data, cursor + 1, "movable-entry interrupt")
                if interrupt != 0x3FCD:
                    raise NEFormatError(
                        f"movable entry {ordinal} has invalid INT 3F marker {interrupt:#06x}"
                    )
                segment = data[cursor + 3]
                offset = _u16(data, cursor + 4, "movable-entry offset")
                if not 1 <= segment <= segment_count:
                    raise NEFormatError(
                        f"movable entry {ordinal} references segment {segment}"
                    )
                kind = "movable"
                value: dict[str, object] = {"segment": segment, "offset": offset}
                movable_count += 1
            elif indicator == 0xFE:
                kind = "constant"
                value = {"value": _u16(data, cursor + 1, "constant-entry value")}
            else:
                if not 1 <= indicator <= segment_count:
                    raise NEFormatError(
                        f"fixed entry {ordinal} references segment {indicator}"
                    )
                kind = "fixed"
                value = {
                    "segment": indicator,
                    "offset": _u16(data, cursor + 1, "fixed-entry offset"),
                }
            entries.append(
                {
                    "ordinal": ordinal,
                    "kind": kind,
                    "flags_raw": flags,
                    "exported": bool(flags & 0x01),
                    "uses_shared_data": bool(flags & 0x02),
                    "parameter_words": flags >> 3,
                    "names": [],
                    **value,
                }
            )
            cursor += record_size
            ordinal += 1

    if not terminated:
        raise NEFormatError("entry table has no terminating zero bundle")
    return entries, movable_count


def _parse_iterated_segment(
    data: bytes,
    start: int,
    stored_size: int,
    allocation_size: int,
    segment_index: int,
) -> dict[str, object]:
    _, end = _range(data, start, stored_size, f"segment {segment_index} iterated data")
    records: list[dict[str, object]] = []
    cursor = start
    expanded_size = 0
    while cursor < end:
        _bounded_count(
            len(records) + 1,
            MAX_ITERATED_RECORDS_PER_SEGMENT,
            f"segment {segment_index} iterated record count",
        )
        _range(data, cursor, 4, f"segment {segment_index} iterated record header")
        repeat_count, chunk_size = struct.unpack_from("<HH", data, cursor)
        if repeat_count == 0 or chunk_size == 0:
            raise NEFormatError(
                f"segment {segment_index} iterated record has a zero repeat/length"
            )
        chunk_start = cursor + 4
        _range(data, chunk_start, chunk_size, f"segment {segment_index} iterated chunk")
        if chunk_start + chunk_size > end:
            raise NEFormatError(f"segment {segment_index} iterated chunk is truncated")
        record_expanded_size = repeat_count * chunk_size
        if record_expanded_size > MAX_DECODED_SEGMENT_SIZE - expanded_size:
            raise NEFormatError(
                f"segment {segment_index} iterated expansion exceeds "
                f"{MAX_DECODED_SEGMENT_SIZE} bytes"
            )
        records.append(
            {
                "record_file_offset": cursor,
                "chunk_file_offset": chunk_start,
                "repeat_count": repeat_count,
                "chunk_size": chunk_size,
                "expanded_offset": expanded_size,
                "expanded_size": record_expanded_size,
            }
        )
        expanded_size += record_expanded_size
        cursor = chunk_start + chunk_size

    if expanded_size > allocation_size:
        raise NEFormatError(
            f"segment {segment_index} expands to {expanded_size} bytes but allocation is "
            f"{allocation_size} bytes"
        )
    return {"record_count": len(records), "expanded_size": expanded_size, "records": records}


def _parse_relocations(
    data: bytes,
    start: int,
    segment_index: int,
    segment_count: int,
    module_count: int,
    allocation_size: int,
    imported_names: dict[int, dict[str, object]],
    module_names: dict[int, str],
    direct_image: tuple[int, int] | None,
    fixup_site_budget: int,
) -> tuple[list[dict[str, object]], int, int]:
    count = _u16(data, start, f"segment {segment_index} relocation count")
    _bounded_count(
        count,
        MAX_RELOCATIONS_PER_SEGMENT,
        f"segment {segment_index} relocation count",
    )
    _range(
        data,
        start + 2,
        count * RELOCATION_ENTRY_SIZE,
        f"segment {segment_index} relocation entries",
    )
    relocations: list[dict[str, object]] = []
    fixup_site_count = 0
    cursor = start + 2
    for relocation_index in range(1, count + 1):
        source_raw, flags, source_offset, target1, target2 = struct.unpack_from(
            "<BBHHH", data, cursor
        )
        source_value = source_raw & 0x7F
        source_name, source_width = SOURCE_TYPES.get(source_value, ("unknown", None))
        if source_width is not None and source_offset + source_width > allocation_size:
            raise NEFormatError(
                f"segment {segment_index} relocation {relocation_index} source is outside "
                "the allocated segment"
            )

        if flags & 0x04:
            fixup_site_count += 1
            if fixup_site_count > fixup_site_budget:
                raise NEFormatError("total relocation fixup-site cap exceeded")
            fixup_chain: dict[str, object] = {
                "status": "resolved",
                "encoding": "additive",
                "offsets": [source_offset],
            }
        elif direct_image is None:
            fixup_site_count += 1
            if fixup_site_count > fixup_site_budget:
                raise NEFormatError("total relocation fixup-site cap exceeded")
            fixup_chain = {
                "status": "unresolved",
                "encoding": "linked",
                "initial_offset": source_offset,
                "reason": "loaded mapping is unavailable for iterated or self-loading segment",
            }
        else:
            image_start, initialized_size = direct_image
            offsets: list[int] = []
            seen: set[int] = set()
            chain_offset = source_offset
            while chain_offset != 0xFFFF:
                if chain_offset in seen:
                    raise NEFormatError(
                        f"segment {segment_index} relocation {relocation_index} source chain "
                        f"cycles at {chain_offset:#x}"
                    )
                _bounded_count(
                    len(offsets) + 1,
                    MAX_FIXUP_SITES_PER_RELOCATION,
                    f"segment {segment_index} relocation {relocation_index} fixup-site count",
                )
                if fixup_site_count + len(offsets) + 1 > fixup_site_budget:
                    raise NEFormatError("total relocation fixup-site cap exceeded")
                if chain_offset + 2 > initialized_size:
                    raise NEFormatError(
                        f"segment {segment_index} relocation {relocation_index} source chain "
                        f"offset {chain_offset:#x} is outside initialized bytes"
                    )
                if (
                    source_width is not None
                    and chain_offset + source_width > allocation_size
                ):
                    raise NEFormatError(
                        f"segment {segment_index} relocation {relocation_index} fixup at "
                        f"{chain_offset:#x} exceeds the allocated segment"
                    )
                seen.add(chain_offset)
                offsets.append(chain_offset)
                chain_offset = _u16(
                    data,
                    image_start + chain_offset,
                    f"segment {segment_index} relocation {relocation_index} chain link",
                )
            fixup_chain = {
                "status": "resolved",
                "encoding": "linked",
                "offsets": offsets,
                "terminator": 0xFFFF,
            }
            fixup_site_count += len(offsets)

        target_kind = flags & 0x03
        if target_kind == 0:
            target_segment = target1 & 0xFF
            reserved = target1 >> 8
            if reserved:
                raise NEFormatError(
                    f"segment {segment_index} relocation {relocation_index} has a nonzero "
                    "internal-target reserved byte"
                )
            if target_segment == 0xFF:
                target: dict[str, object] = {
                    "kind": "internal_entry",
                    "ordinal": target2,
                }
            else:
                if not 1 <= target_segment <= segment_count:
                    raise NEFormatError(
                        f"segment {segment_index} relocation {relocation_index} references "
                        f"internal segment {target_segment}"
                    )
                target = {
                    "kind": "internal",
                    "segment": target_segment,
                    "offset": target2,
                }
        elif target_kind in {1, 2}:
            module_index = target1
            if not 1 <= module_index <= module_count:
                raise NEFormatError(
                    f"segment {segment_index} relocation {relocation_index} references "
                    f"module {module_index}"
                )
            if target_kind == 1:
                target = {
                    "kind": "import_ordinal",
                    "module_index": module_index,
                    "module": module_names[module_index],
                    "ordinal": target2,
                }
            else:
                imported = imported_names.get(target2)
                if imported is None:
                    raise NEFormatError(
                        f"segment {segment_index} relocation {relocation_index} import-name "
                        f"offset {target2:#x} is not at a name boundary"
                    )
                target = {
                    "kind": "import_name",
                    "module_index": module_index,
                    "module": module_names[module_index],
                    "name_offset": target2,
                    "name": imported["name"],
                }
        else:
            target = {"kind": "os_fixup", "target1": target1, "target2": target2}

        relocations.append(
            {
                "index": relocation_index,
                "file_offset": cursor,
                "source_type_raw": source_raw,
                "source_type": source_name,
                "source_width": source_width,
                "source_offset": source_offset,
                "flags_raw": flags,
                "additive": bool(flags & 0x04),
                "fixup_chain": fixup_chain,
                "target": target,
            }
        )
        cursor += RELOCATION_ENTRY_SIZE
    return relocations, cursor, fixup_site_count


def _validate_no_overlaps(ranges: list[tuple[int, int, str]], label: str) -> None:
    ordered = sorted((start, end, name) for start, end, name in ranges if start != end)
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise NEFormatError(
                f"{label} overlap: {previous[2]} [{previous[0]:#x},{previous[1]:#x}) and "
                f"{current[2]} [{current[0]:#x},{current[1]:#x})"
            )


def _validate_payload_vs_metadata(
    payload_ranges: list[tuple[int, int, str]],
    metadata_ranges: list[tuple[int, int, str]],
) -> None:
    for payload_start, payload_end, payload_name in payload_ranges:
        for metadata_start, metadata_end, metadata_name in metadata_ranges:
            if payload_start < metadata_end and metadata_start < payload_end:
                raise NEFormatError(
                    f"payload/metadata overlap: {payload_name} and {metadata_name}"
                )


def _attach_entry_names(
    entries: list[dict[str, object]],
    resident_names: list[dict[str, object]],
    nonresident_names: list[dict[str, object]],
) -> None:
    for table_name, names in (
        ("resident", resident_names),
        ("nonresident", nonresident_names),
    ):
        for item in names:
            ordinal = int(item["ordinal"])
            if ordinal == 0:
                continue
            if ordinal > len(entries):
                raise NEFormatError(
                    f"{table_name} name {item['name']!r} references missing ordinal {ordinal}"
                )
            entry_names = entries[ordinal - 1]["names"]
            assert isinstance(entry_names, list)
            entry_names.append({"table": table_name, "name": item["name"]})


def parse_ne(data: bytes, *, include_resource_names: bool = False) -> dict[str, Any]:
    """Parse one bounded byte string into a deterministic, JSON-friendly NE index."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if len(data) > MAX_FILE_SIZE:
        raise NEFormatError(f"input size exceeds cap {MAX_FILE_SIZE}")
    _range(data, 0, 0x40, "MZ header")
    if data[:2] != b"MZ":
        raise NEFormatError("missing MZ signature")

    ne_offset = _u32(data, 0x3C, "MZ e_lfanew")
    if ne_offset < 0x40:
        raise NEFormatError("MZ e_lfanew overlaps the DOS header")
    _range(data, ne_offset, NE_HEADER_SIZE, "NE header")
    if data[ne_offset : ne_offset + 2] != b"NE":
        raise NEFormatError("MZ e_lfanew does not point to an NE signature")

    linker_major = data[ne_offset + 2]
    linker_minor = data[ne_offset + 3]
    entry_offset = _u16(data, ne_offset + 0x04, "entry-table offset")
    entry_size = _u16(data, ne_offset + 0x06, "entry-table size")
    checksum = _u32(data, ne_offset + 0x08, "NE checksum")
    module_flags = _u16(data, ne_offset + 0x0C, "module flags")
    automatic_data_segment = _u16(data, ne_offset + 0x0E, "automatic data segment")
    heap_size = _u16(data, ne_offset + 0x10, "initial heap size")
    stack_size = _u16(data, ne_offset + 0x12, "initial stack size")
    csip = _u32(data, ne_offset + 0x14, "initial CS:IP")
    sssp = _u32(data, ne_offset + 0x18, "initial SS:SP")
    segment_count = _bounded_count(
        _u16(data, ne_offset + 0x1C, "segment count"), MAX_SEGMENTS, "segment count"
    )
    module_count = _bounded_count(
        _u16(data, ne_offset + 0x1E, "module-reference count"),
        MAX_MODULE_REFERENCES,
        "module-reference count",
    )
    for field_name, segment_number in (
        ("automatic data segment", automatic_data_segment),
        ("initial CS", csip >> 16),
        ("initial SS", sssp >> 16),
    ):
        if segment_number > segment_count:
            raise NEFormatError(
                f"{field_name} {segment_number} exceeds segment count {segment_count}"
            )
    nonresident_size = _u16(data, ne_offset + 0x20, "nonresident-name size")
    segment_offset = _u16(data, ne_offset + 0x22, "segment-table offset")
    resource_offset = _u16(data, ne_offset + 0x24, "resource-table offset")
    resident_offset = _u16(data, ne_offset + 0x26, "resident-name offset")
    module_offset = _u16(data, ne_offset + 0x28, "module-reference offset")
    import_offset = _u16(data, ne_offset + 0x2A, "imported-name offset")
    nonresident_offset = _u32(data, ne_offset + 0x2C, "nonresident-name offset")
    declared_movable_entries = _u16(data, ne_offset + 0x30, "movable-entry count")
    alignment_shift = _u16(data, ne_offset + 0x32, "file alignment shift")
    if alignment_shift > MAX_SHIFT:
        raise NEFormatError(
            f"file alignment shift {alignment_shift} exceeds cap {MAX_SHIFT}"
        )
    resource_segment_count = _u16(data, ne_offset + 0x34, "resource-segment count")
    target_os_value = data[ne_offset + 0x36]
    other_flags = data[ne_offset + 0x37]
    return_thunks_offset = _u16(data, ne_offset + 0x38, "return-thunks offset")
    segment_ref_bytes_offset = _u16(data, ne_offset + 0x3A, "segment-ref offset")
    minimum_code_swap_area = _u16(data, ne_offset + 0x3C, "minimum code swap area")
    expected_version = _u16(data, ne_offset + 0x3E, "expected Windows version")

    relative_offsets = [
        ("segment table", segment_offset),
        ("resource table", resource_offset),
        ("resident-name table", resident_offset),
        ("module-reference table", module_offset),
        ("imported-name table", import_offset),
        ("entry table", entry_offset),
    ]
    previous = NE_HEADER_SIZE
    for name, offset in relative_offsets:
        if offset < previous:
            raise NEFormatError(
                f"{name} offset {offset:#x} precedes the prior NE table boundary {previous:#x}"
            )
        previous = offset

    segment_start = ne_offset + segment_offset
    resource_start = ne_offset + resource_offset
    resident_start = ne_offset + resident_offset
    module_start = ne_offset + module_offset
    import_start = ne_offset + import_offset
    entry_start = ne_offset + entry_offset

    _range(data, segment_start, segment_count * SEGMENT_TABLE_ENTRY_SIZE, "segment table")
    if segment_start + segment_count * SEGMENT_TABLE_ENTRY_SIZE > resource_start:
        raise NEFormatError("segment table overlaps the resource table")
    _range(data, resource_start, resident_start - resource_start, "resource table")
    _range(data, resident_start, module_start - resident_start, "resident-name area")
    _range(data, module_start, module_count * 2, "module-reference table")
    if module_start + module_count * 2 > import_start:
        raise NEFormatError("module-reference table overlaps the imported-name table")
    _range(data, import_start, entry_start - import_start, "imported-name table")
    _, entry_end = _range(data, entry_start, entry_size, "entry table")
    if nonresident_size:
        _range(data, nonresident_offset, nonresident_size, "nonresident-name table")

    resources, resource_payload_ranges, resource_items = _parse_resources(
        data, resource_start, resident_start, include_resource_names
    )
    resident_names, resident_end = _parse_name_table(
        data, resident_start, module_start, "resident-name table"
    )
    imported_names, imported_by_offset = _parse_imported_names(
        data, import_start, entry_start
    )

    module_references: list[dict[str, object]] = []
    module_names: dict[int, str] = {}
    for index in range(1, module_count + 1):
        name_offset = _u16(
            data, module_start + (index - 1) * 2, f"module reference {index} name offset"
        )
        imported = imported_by_offset.get(name_offset)
        if imported is None:
            raise NEFormatError(
                f"module reference {index} offset {name_offset:#x} is not at an imported-name "
                "boundary"
            )
        name = str(imported["name"])
        module_names[index] = name
        module_references.append(
            {"index": index, "name_offset": name_offset, "name": name}
        )

    entries, movable_count = _parse_entry_table(
        data, entry_start, entry_size, segment_count
    )
    if movable_count != declared_movable_entries:
        raise NEFormatError(
            f"movable-entry count mismatch: header={declared_movable_entries}, "
            f"table={movable_count}"
        )

    if nonresident_size:
        nonresident_limit = nonresident_offset + nonresident_size
        nonresident_names, nonresident_end = _parse_name_table(
            data,
            nonresident_offset,
            nonresident_limit,
            "nonresident-name table",
        )
        if any(data[nonresident_end:nonresident_limit]):
            raise NEFormatError("nonresident-name table has nonzero bytes after its terminator")
    else:
        nonresident_names = []
        nonresident_end = nonresident_offset

    _attach_entry_names(entries, resident_names, nonresident_names)

    segments: list[dict[str, object]] = []
    segment_payload_ranges: list[tuple[int, int, str]] = []
    total_decoded_size = 0
    total_relocations = 0
    total_fixup_sites = 0
    total_iterated_records = 0
    internal_entry_targets: list[tuple[int, int, int]] = []
    internal_segment_targets: list[tuple[int, int, int, int]] = []
    for index in range(1, segment_count + 1):
        table_entry = segment_start + (index - 1) * SEGMENT_TABLE_ENTRY_SIZE
        sector, length_field, flags, allocation_field = struct.unpack_from(
            "<HHHH", data, table_entry
        )
        allocation_size = allocation_field or 65_536
        storage = "iterated" if flags & 0x0008 else "direct"
        file_offset: int | None
        if sector == 0:
            file_offset = None
            stored_size = 0
            if flags & 0x0100:
                raise NEFormatError(f"segment {index} has relocations but no file image")
        else:
            file_offset = _shifted(sector, alignment_shift, f"segment {index} offset")
            stored_size = length_field or 65_536
            _range(data, file_offset, stored_size, f"segment {index} data")

        custom_selfload_mapping = bool(module_flags & 0x0800) and index > 1
        if storage == "iterated" and file_offset is not None:
            if custom_selfload_mapping:
                iterated = {
                    "status": "unsupported_selfload",
                    "record_count": None,
                    "expanded_size": None,
                    "records": [],
                    "reason": "module self-loader controls the stored-to-loaded mapping",
                }
                decoded_size = None
                mapping_status = "unsupported_selfload"
            else:
                iterated = _parse_iterated_segment(
                    data, file_offset, stored_size, allocation_size, index
                )
                decoded_size = int(iterated["expanded_size"])
                mapping_status = "standard_iterated_metadata_only"
                total_iterated_records += int(iterated["record_count"])
                _bounded_count(
                    total_iterated_records,
                    MAX_TOTAL_ITERATED_RECORDS,
                    "total iterated-record count",
                )
        else:
            iterated = None
            if file_offset is None:
                decoded_size = 0
                mapping_status = "no_file_image"
            elif custom_selfload_mapping:
                decoded_size = None
                mapping_status = "unsupported_selfload"
            else:
                decoded_size = stored_size
                mapping_status = "direct"
            if decoded_size is not None and decoded_size > allocation_size:
                raise NEFormatError(
                    f"segment {index} stores {decoded_size} bytes but allocation is "
                    f"{allocation_size} bytes"
                )

        if decoded_size is not None:
            total_decoded_size += decoded_size
            if total_decoded_size > MAX_TOTAL_DECODED_SIZE:
                raise NEFormatError(
                    f"total decoded segment size exceeds cap {MAX_TOTAL_DECODED_SIZE}"
                )

        relocations: list[dict[str, object]] = []
        relocation_range: dict[str, int] | None = None
        payload_end = file_offset + stored_size if file_offset is not None else 0
        if flags & 0x0100:
            assert file_offset is not None
            relocations, reloc_end, fixup_sites = _parse_relocations(
                data,
                payload_end,
                index,
                segment_count,
                module_count,
                allocation_size,
                imported_by_offset,
                module_names,
                (
                    (file_offset, stored_size)
                    if storage == "direct" and not custom_selfload_mapping
                    else None
                ),
                MAX_TOTAL_FIXUP_SITES - total_fixup_sites,
            )
            total_fixup_sites += fixup_sites
            total_relocations += len(relocations)
            _bounded_count(total_relocations, MAX_TOTAL_RELOCATIONS, "total relocation count")
            relocation_range = {"offset": payload_end, "size": reloc_end - payload_end}
            payload_end = reloc_end
            for relocation in relocations:
                target = relocation["target"]
                assert isinstance(target, dict)
                if target["kind"] == "internal_entry":
                    internal_entry_targets.append(
                        (index, int(relocation["index"]), int(target["ordinal"]))
                    )
                elif target["kind"] == "internal":
                    internal_segment_targets.append(
                        (
                            index,
                            int(relocation["index"]),
                            int(target["segment"]),
                            int(target["offset"]),
                        )
                    )

        if file_offset is not None:
            segment_payload_ranges.append(
                (file_offset, payload_end, f"segment {index} data/relocations")
            )
        segments.append(
            {
                "index": index,
                "sector": sector,
                "file_offset": file_offset,
                "stored_size_field": length_field,
                "stored_size": stored_size,
                "allocation_size_field": allocation_field,
                "allocation_size": allocation_size,
                "decoded_size": decoded_size,
                "storage": storage,
                "mapping_status": mapping_status,
                "iterated": iterated,
                "flags_raw": flags,
                "flags": _flag_names(flags, SEGMENT_FLAGS),
                "kind": "data" if flags & 0x0001 else "code",
                "relocation_table": relocation_range,
                "relocations": relocations,
            }
        )

    for segment_index, relocation_index, ordinal in internal_entry_targets:
        if not 1 <= ordinal <= len(entries) or entries[ordinal - 1]["kind"] == "unused":
            raise NEFormatError(
                f"segment {segment_index} relocation {relocation_index} references missing "
                f"internal ordinal {ordinal}"
            )

    for source_segment, relocation_index, target_segment, target_offset in (
        internal_segment_targets
    ):
        if target_offset >= int(segments[target_segment - 1]["allocation_size"]):
            raise NEFormatError(
                f"segment {source_segment} relocation {relocation_index} target offset "
                f"{target_offset:#x} exceeds segment {target_segment}"
            )

    for entry in entries:
        if entry["kind"] not in {"fixed", "movable"}:
            continue
        segment_index = int(entry["segment"])
        if int(entry["offset"]) >= int(segments[segment_index - 1]["allocation_size"]):
            raise NEFormatError(
                f"entry ordinal {entry['ordinal']} offset exceeds segment {segment_index}"
            )

    metadata_ranges = [
        (0, ne_offset + NE_HEADER_SIZE, "MZ stub and NE header"),
        (
            segment_start,
            segment_start + segment_count * SEGMENT_TABLE_ENTRY_SIZE,
            "segment table",
        ),
        (resource_start, resident_start, "resource table"),
        (resident_start, resident_end, "resident-name table"),
        (module_start, module_start + module_count * 2, "module-reference table"),
        (import_start, entry_start, "imported-name table"),
        (entry_start, entry_end, "entry table"),
    ]
    if nonresident_size:
        metadata_ranges.append(
            (
                nonresident_offset,
                nonresident_offset + nonresident_size,
                "nonresident-name table",
            )
        )
    _validate_no_overlaps(metadata_ranges, "metadata")
    payload_ranges = [*segment_payload_ranges, *resource_payload_ranges]
    _validate_no_overlaps(payload_ranges, "payload")
    _validate_payload_vs_metadata(payload_ranges, metadata_ranges)
    for item in resource_items:
        file_offset = int(item["file_offset"])
        size = int(item["size"])
        item["sha256"] = hashlib.sha256(
            memoryview(data)[file_offset : file_offset + size]
        ).hexdigest()

    exports = [entry for entry in entries if entry["names"]]
    header = {
        "linker_version": {"major": linker_major, "minor": linker_minor},
        "checksum": checksum,
        "module_flags_raw": module_flags,
        "module_flags": _flag_names(module_flags, MODULE_FLAGS),
        "application_type": (module_flags >> 8) & 0x03,
        "automatic_data_segment": automatic_data_segment,
        "initial_heap_size": heap_size,
        "initial_stack_size": stack_size,
        "initial_cs": csip >> 16,
        "initial_ip": csip & 0xFFFF,
        "initial_ss": sssp >> 16,
        "initial_sp": sssp & 0xFFFF,
        "segment_count": segment_count,
        "module_reference_count": module_count,
        "nonresident_name_table_size": nonresident_size,
        "table_offsets": {
            "entry_table_relative": entry_offset,
            "segment_table_relative": segment_offset,
            "resource_table_relative": resource_offset,
            "resident_name_table_relative": resident_offset,
            "module_reference_table_relative": module_offset,
            "imported_name_table_relative": import_offset,
            "nonresident_name_table_absolute": nonresident_offset,
        },
        "entry_table_size": entry_size,
        "movable_entry_count": declared_movable_entries,
        "file_alignment_shift": alignment_shift,
        "file_alignment": 1 << alignment_shift,
        "resource_segment_count": resource_segment_count,
        "target_os_raw": target_os_value,
        "target_os": TARGET_OS.get(target_os_value, "unknown"),
        "other_flags_raw": other_flags,
        "return_thunks_offset": return_thunks_offset,
        "segment_reference_bytes_offset": segment_ref_bytes_offset,
        "minimum_code_swap_area": minimum_code_swap_area,
        "expected_windows_version": {
            "raw": expected_version,
            "major": expected_version >> 8,
            "minor": expected_version & 0xFF,
        },
    }
    table_ranges = [
        {"name": name, "offset": start, "size": end - start}
        for start, end, name in sorted(metadata_ranges)
        if start != end
    ]
    return {
        "schema": "amipro-ne-structural-index-v1",
        "format": "NE",
        "file_size": len(data),
        "options": {"resource_names_included": include_resource_names},
        "mz": {"new_executable_header_offset": ne_offset},
        "header": header,
        "table_ranges": table_ranges,
        "segments": segments,
        "module_references": module_references,
        "imported_names": imported_names,
        "resident_names": resident_names,
        "nonresident_names": nonresident_names,
        "entry_table": entries,
        "exports": exports,
        "resources": resources,
    }


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _normalize_sha256(expected_sha256: str | None) -> str | None:
    if expected_sha256 is None:
        return None
    normalized = expected_sha256.casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise VerificationError("expected SHA-256 must contain exactly 64 hexadecimal digits")
    return normalized


def read_verified(
    path: os.PathLike[str] | str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    max_file_size: int = MAX_FILE_SIZE,
) -> VerifiedInput:
    """Read and hash a stable regular file through one descriptor.

    The returned bytes are exactly those hashed.  The descriptor's identity and
    mutation-sensitive stat fields must be unchanged between the two ``fstat``
    calls.  A final read also detects growth not yet reflected in the initial
    size.
    """

    if not 0 <= max_file_size <= MAX_FILE_SIZE:
        raise VerificationError(f"max_file_size must be between 0 and {MAX_FILE_SIZE}")
    if expected_size is not None and expected_size < 0:
        raise VerificationError("expected size cannot be negative")
    normalized_digest = _normalize_sha256(expected_sha256)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise VerificationError(f"cannot open input safely: {error}") from error

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise VerificationError("input is not a regular file")
        if before.st_size > max_file_size:
            raise VerificationError(
                f"input size {before.st_size} exceeds cap {max_file_size}"
            )
        if expected_size is not None and before.st_size != expected_size:
            raise VerificationError(
                f"input size mismatch: expected {expected_size}, observed {before.st_size}"
            )

        chunks: list[bytes] = []
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(READ_CHUNK_SIZE, remaining))
            if not chunk:
                raise VerificationError("input became shorter while it was read")
            remaining -= len(chunk)
            chunks.append(chunk)
            digest.update(chunk)
        if os.read(descriptor, 1):
            raise VerificationError("input grew while it was read")
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise VerificationError("input metadata changed while it was read")
    finally:
        os.close(descriptor)

    observed_digest = digest.hexdigest()
    if normalized_digest is not None and observed_digest != normalized_digest:
        raise VerificationError(
            f"input SHA-256 mismatch: expected {normalized_digest}, observed {observed_digest}"
        )
    payload = b"".join(chunks)
    return VerifiedInput(
        data=payload,
        size=len(payload),
        sha256=observed_digest,
        device=before.st_dev,
        inode=before.st_ino,
    )


def index_verified(
    path: os.PathLike[str] | str,
    *,
    expected_size: int,
    expected_sha256: str,
    include_resource_names: bool = False,
) -> dict[str, object]:
    """Apply the mandatory size/digest gate and index those exact bytes."""

    verified = read_verified(
        path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    return {
        "input": verified.public_metadata(),
        "index": parse_ne(
            verified.data,
            include_resource_names=include_resource_names,
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--expected-size", type=int, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--include-resource-names", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = index_verified(
            args.input,
            expected_size=args.expected_size,
            expected_sha256=args.expected_sha256,
            include_resource_names=args.include_resource_names,
        )
    except (NEFormatError, VerificationError, OSError) as error:
        print(f"NE indexing failed: {error}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
