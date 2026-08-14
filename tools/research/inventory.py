#!/usr/bin/env python3
"""Create a deterministic, path-free manifest of signature-confirmed NE modules.

The payload directory is volatile input.  Every regular file is opened with the
same-descriptor safety gate; the bytes that are hashed are the bytes inspected
for an NE signature and structurally indexed.  The committed manifest contains
only module identities and bounded structural summaries, never payload bytes,
resource names, or absolute paths.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import struct
import sys
from pathlib import Path
from typing import Any

from ne import MAX_FILE_SIZE, NEFormatError, VerificationError, parse_ne, read_verified
from tool_probe import build_tool_report

SCHEMA = "amipro-ne-module-manifest-v1"
PRIMARY_NAME = "AMIPRO.EXE"
PRIMARY_SIZE = 888_224
PRIMARY_SHA256 = "555506d1558d61579d5c6fee8bf5fa9d960aa05a20a5d171240ac2e0ea73cbbd"
MAX_DIRECTORY_ENTRIES = 4_096
MAX_NAME_BYTES = 255
MAX_TOTAL_BYTES = 512 * 1024 * 1024

ROLE_RULES: dict[str, dict[str, object]] = {
    "AMIPRO.EXE": {
        "role": "primary_document_application",
        "required_exports": ["SAMMYTEXTPROC"],
    },
    "W4W33F.DLL": {
        "role": "word_for_word_reader_filter_candidate",
        "required_exports": ["FILTERFROM", "WFWFROM"],
    },
    "W4W33T.DLL": {
        "role": "word_for_word_writer_filter_candidate",
        "required_exports": ["FILTERTO", "WFWTO"],
    },
    "AMIFM.EXE": {"role": "file_manager_candidate", "required_exports": []},
    "AMIPROUI.DLL": {"role": "user_interface_candidate", "required_exports": []},
    "AMIPRINT.EXE": {"role": "print_pipeline_candidate", "required_exports": []},
    "AMIENV.DLL": {"role": "environment_support_candidate", "required_exports": []},
    "AMIFONT.DLL": {"role": "font_metrics_candidate", "required_exports": []},
    "AMILOTUS.DLL": {"role": "lotus_integration_candidate", "required_exports": []},
}


class InventoryError(RuntimeError):
    """The payload cannot safely produce the requested manifest."""


def _looks_like_ne(data: bytes) -> bool:
    if len(data) < 0x40 or data[:2] != b"MZ":
        return False
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    return offset <= len(data) - 2 and data[offset : offset + 2] == b"NE"


def _export_names(index: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for entry in index["exports"]:
        for item in entry["names"]:
            names.add(str(item["name"]))
    return names


def _role_summary(name: str, index: dict[str, Any]) -> dict[str, object] | None:
    rule = ROLE_RULES.get(name.upper())
    if rule is None:
        return None
    required = [str(value) for value in rule["required_exports"]]
    observed = _export_names(index)
    if not required:
        status = "candidate_only"
    elif set(required) <= observed:
        status = "directional_exports_observed"
    else:
        status = "required_exports_incomplete"
    return {
        "role": rule["role"],
        "status": status,
        "required_exports_observed": sorted(name for name in required if name in observed),
        "required_exports_missing": sorted(name for name in required if name not in observed),
    }


def _module_summary(name: str, size: int, sha256: str, index: dict[str, Any]) -> dict[str, object]:
    segments = index["segments"]
    header = index["header"]
    direct_count = sum(segment["storage"] == "direct" for segment in segments)
    iterated_count = sum(segment["storage"] == "iterated" for segment in segments)
    selfload_count = sum("selfload" in segment["flags"] for segment in segments)
    unsupported_mapping_count = sum(
        segment["decoded_size"] is None for segment in segments
    )
    relocation_count = sum(len(segment["relocations"]) for segment in segments)
    role = _role_summary(name, index)
    result: dict[str, object] = {
        "name": name,
        "size": size,
        "sha256": sha256,
        "container": "NE",
        "suffix": Path(name).suffix.upper(),
        "ne": {
            "header_offset": index["mz"]["new_executable_header_offset"],
            "linker_version": header["linker_version"],
            "target_os": header["target_os"],
            "expected_windows_version": header["expected_windows_version"],
            "module_flags_raw": header["module_flags_raw"],
            "initial_cs": header["initial_cs"],
            "initial_ip": header["initial_ip"],
            "automatic_data_segment": header["automatic_data_segment"],
            "segment_count": header["segment_count"],
            "direct_segment_count": direct_count,
            "iterated_segment_count": iterated_count,
            "selfload_segment_count": selfload_count,
            "unsupported_loaded_mapping_count": unsupported_mapping_count,
            "relocation_count": relocation_count,
            "module_reference_count": header["module_reference_count"],
            "resource_count": index["resources"]["resource_count"],
            "named_export_count": len(index["exports"]),
        },
    }
    if role is not None:
        result["role_evidence"] = role
    return result


def _directory_entries(payload_dir: Path) -> list[os.DirEntry[str]]:
    try:
        info = payload_dir.lstat()
    except OSError as error:
        raise InventoryError(f"cannot stat payload directory: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InventoryError("payload path must be a non-symlink directory")
    try:
        entries = list(os.scandir(payload_dir))
    except OSError as error:
        raise InventoryError(f"cannot enumerate payload directory: {error}") from error
    if len(entries) > MAX_DIRECTORY_ENTRIES:
        raise InventoryError(
            f"payload directory has {len(entries)} entries; cap is {MAX_DIRECTORY_ENTRIES}"
        )
    for entry in entries:
        if len(os.fsencode(entry.name)) > MAX_NAME_BYTES:
            raise InventoryError(f"payload contains a name longer than {MAX_NAME_BYTES} bytes")
    return sorted(entries, key=lambda entry: os.fsencode(entry.name))


def inventory_payload(
    payload_dir: Path,
    *,
    primary_name: str = PRIMARY_NAME,
    primary_size: int = PRIMARY_SIZE,
    primary_sha256: str = PRIMARY_SHA256,
    tool_probes: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Inventory one flat payload directory using a mandatory primary trust anchor."""

    modules: list[dict[str, object]] = []
    regular_file_count = 0
    skipped_directory_count = 0
    total_bytes = 0
    primary_seen = False
    for entry in _directory_entries(payload_dir):
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise InventoryError(f"cannot stat payload entry {entry.name!r}: {error}") from error
        if stat.S_ISDIR(info.st_mode):
            skipped_directory_count += 1
            continue
        if not stat.S_ISREG(info.st_mode) or entry.is_symlink():
            raise InventoryError(f"payload entry {entry.name!r} is not a regular non-symlink file")
        regular_file_count += 1

        is_primary = entry.name.casefold() == primary_name.casefold()
        remaining_total = MAX_TOTAL_BYTES - total_bytes
        verified = read_verified(
            Path(entry.path),
            expected_size=primary_size if is_primary else None,
            expected_sha256=primary_sha256 if is_primary else None,
            max_file_size=min(MAX_FILE_SIZE, remaining_total),
        )
        total_bytes += verified.size
        if is_primary:
            if primary_seen:
                raise InventoryError("payload contains duplicate case-folded primary names")
            primary_seen = True
        if not _looks_like_ne(verified.data):
            continue
        try:
            index = parse_ne(verified.data)
        except NEFormatError as error:
            raise InventoryError(
                f"NE structural validation failed for {entry.name!r}: {error}"
            ) from error
        modules.append(
            _module_summary(entry.name, verified.size, verified.sha256, index)
        )

    if not primary_seen:
        raise InventoryError(f"trusted primary {primary_name!r} is absent")
    modules.sort(key=lambda item: os.fsencode(str(item["name"])))
    tools = build_tool_report(tool_probes)
    return {
        "schema": SCHEMA,
        "selection": {
            "scope": "nonrecursive_regular_files_with_mz_ne_signature",
            "maximum_directory_entries": MAX_DIRECTORY_ENTRIES,
            "maximum_name_bytes": MAX_NAME_BYTES,
            "maximum_file_bytes": MAX_FILE_SIZE,
            "maximum_total_bytes": MAX_TOTAL_BYTES,
            "resource_names_included": False,
        },
        "trust_anchor": {
            "name": primary_name,
            "size": primary_size,
            "sha256": primary_sha256,
        },
        "payload_summary": {
            "directory_entry_count": regular_file_count + skipped_directory_count,
            "regular_file_count": regular_file_count,
            "skipped_directory_count": skipped_directory_count,
            "regular_file_bytes": total_bytes,
            "ne_module_count": len(modules),
        },
        "modules": modules,
        "tools": tools,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--payload-dir",
        type=Path,
        default=os.environ.get("AMIPRO_PAYLOAD_DIR"),
        required="AMIPRO_PAYLOAD_DIR" not in os.environ,
    )
    parser.add_argument("--output", type=Path, help="write JSON here instead of stdout")
    args = parser.parse_args(argv)
    try:
        report = inventory_payload(args.payload_dir)
    except (InventoryError, NEFormatError, VerificationError, OSError) as error:
        print(f"inventory failed: {error}", file=sys.stderr)
        return 2
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(encoded)
    else:
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
