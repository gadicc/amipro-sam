#!/usr/bin/env python3
"""Hash-gated exact-token search and bounded x86-16 evidence packets for NE modules.

This tool deliberately does not decompile, extract resources, follow arbitrary
paths, or reconstruct self-loading/iterated images.  Raw instruction windows
are capped at 64 bytes and 32 decoded instructions.  Search hits are structural
leads; candidate immediate-word occurrences are not called code references
until an analyst validates the surrounding instruction and control flow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from inventory import PRIMARY_NAME, PRIMARY_SHA256, PRIMARY_SIZE, SCHEMA as MANIFEST_SCHEMA
from ne import NEFormatError, VerificationError, parse_ne, read_verified
from tool_probe import SCHEMA as TOOL_SCHEMA

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_PACKET_BYTES = 64
MAX_PACKET_INSTRUCTIONS = 32
MAX_PATTERNS = 4
MAX_PATTERN_BYTES = 96
MAX_SEARCH_HITS_PER_PATTERN = 24
MAX_XREF_FANOUT = 24
MAX_XREF_DEPTH = 4
MAX_MODULES_IN_MANIFEST = 512
MAX_TOOL_PROBES = 64
DECODER_TIMEOUT_SECONDS = 5


class EvidenceError(RuntimeError):
    """A manifest, module, address, or bounded analysis request is invalid."""


def _manifest_bytes(path: Path) -> tuple[bytes, str]:
    verified = read_verified(path, max_file_size=MAX_MANIFEST_BYTES)
    return verified.data, verified.sha256


def load_manifest(path: Path) -> tuple[dict[str, Any], str, int]:
    """Load and validate the deterministic public manifest and return its digest."""

    raw, digest = _manifest_bytes(path)
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"manifest is not valid UTF-8 JSON: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise EvidenceError(f"manifest schema must be {MANIFEST_SCHEMA!r}")
    trust_anchor = manifest.get("trust_anchor")
    if not isinstance(trust_anchor, dict) or trust_anchor != {
        "name": PRIMARY_NAME,
        "size": PRIMARY_SIZE,
        "sha256": PRIMARY_SHA256,
    }:
        raise EvidenceError("manifest does not carry the exact AMIPRO.EXE trust anchor")
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not 1 <= len(modules) <= MAX_MODULES_IN_MANIFEST:
        raise EvidenceError("manifest module list is empty, missing, or exceeds its cap")
    names: set[str] = set()
    folded_names: set[str] = set()
    for item in modules:
        if not isinstance(item, dict):
            raise EvidenceError("manifest module entry is not an object")
        name = item.get("name")
        if not isinstance(name, str) or Path(name).name != name or not name:
            raise EvidenceError("manifest module name is not a simple basename")
        if name in names:
            raise EvidenceError(f"manifest repeats module {name!r}")
        folded = name.casefold()
        if folded in folded_names:
            raise EvidenceError(f"manifest repeats case-folded module {name!r}")
        names.add(name)
        folded_names.add(folded)
        size = item.get("size")
        sha256 = item.get("sha256")
        if not isinstance(size, int) or size < 0:
            raise EvidenceError(f"manifest size for {name!r} is invalid")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise EvidenceError(f"manifest SHA-256 for {name!r} is invalid")
    primary = [item for item in modules if item.get("name") == PRIMARY_NAME]
    if len(primary) != 1 or (
        primary[0].get("size"), primary[0].get("sha256")
    ) != (PRIMARY_SIZE, PRIMARY_SHA256):
        raise EvidenceError("manifest module list does not match the primary trust anchor")
    payload_summary = manifest.get("payload_summary")
    if not isinstance(payload_summary, dict) or payload_summary.get(
        "ne_module_count"
    ) != len(modules):
        raise EvidenceError("manifest payload summary does not match its module list")
    tools = manifest.get("tools")
    if not isinstance(tools, dict) or tools.get("schema") != TOOL_SCHEMA:
        raise EvidenceError(f"manifest tools schema must be {TOOL_SCHEMA!r}")
    runtime = tools.get("python")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("version"), str):
        raise EvidenceError("manifest Python runtime identity is missing")
    probes = tools.get("probes")
    if not isinstance(probes, list) or len(probes) > MAX_TOOL_PROBES:
        raise EvidenceError("manifest tool-probe list is missing or exceeds its cap")
    tool_names: set[str] = set()
    for probe in probes:
        if not isinstance(probe, dict) or not isinstance(probe.get("name"), str):
            raise EvidenceError("manifest tool probe is invalid")
        tool_name = str(probe["name"])
        if tool_name in tool_names:
            raise EvidenceError(f"manifest repeats tool probe {tool_name!r}")
        tool_names.add(tool_name)
        available = probe.get("available")
        executable_hash = probe.get("executable_sha256")
        if not isinstance(available, bool):
            raise EvidenceError(f"manifest availability for tool {tool_name!r} is invalid")
        if available and not (
            isinstance(executable_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", executable_hash)
        ):
            raise EvidenceError(
                f"manifest executable SHA-256 for available tool {tool_name!r} is invalid"
            )
        if not available and executable_hash is not None:
            raise EvidenceError(
                f"manifest unavailable tool {tool_name!r} has an executable digest"
            )
    return manifest, digest, len(raw)


def _decoder_identities(manifest: dict[str, Any]) -> dict[str, dict[str, object]]:
    probes = manifest["tools"]["probes"]
    return {str(probe["name"]): probe for probe in probes if probe["name"] in DECODERS}


def _payload_root(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as error:
        raise EvidenceError(f"cannot stat payload directory: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise EvidenceError("payload path must be a non-symlink directory")
    return path


def load_module(
    manifest: dict[str, Any], payload_dir: Path, module_name: str
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """Apply the stage-two size/digest gate, then index those exact module bytes."""

    if Path(module_name).name != module_name or not module_name:
        raise EvidenceError("module name must be a simple basename")
    matches = [item for item in manifest["modules"] if item["name"] == module_name]
    if len(matches) != 1:
        raise EvidenceError(f"module {module_name!r} is not uniquely listed in the manifest")
    identity = matches[0]
    root = _payload_root(payload_dir)
    verified = read_verified(
        root / module_name,
        expected_size=int(identity["size"]),
        expected_sha256=str(identity["sha256"]),
    )
    index = parse_ne(verified.data)
    return verified.data, index, identity


def _direct_segment(
    data: bytes, index: dict[str, Any], segment_number: int
) -> tuple[bytes, dict[str, Any]]:
    segments = index["segments"]
    if not 1 <= segment_number <= len(segments):
        raise EvidenceError(f"segment must be between 1 and {len(segments)}")
    segment = segments[segment_number - 1]
    if segment.get("mapping_status") != "direct" or segment["file_offset"] is None:
        raise EvidenceError(
            f"segment {segment_number} has no validated direct stored-to-logical mapping"
        )
    start = int(segment["file_offset"])
    size = int(segment["stored_size"])
    return data[start : start + size], segment


def _find_offsets(haystack: bytes, needle: bytes):
    """Yield overlapping match offsets without materializing them."""

    cursor = 0
    while True:
        offset = haystack.find(needle, cursor)
        if offset < 0:
            return
        yield offset
        cursor = offset + 1


def search_tokens(
    data: bytes,
    index: dict[str, Any],
    patterns: list[str],
) -> dict[str, object]:
    """Search every validated direct segment for exact ASCII tokens and word leads."""

    if not 1 <= len(patterns) <= MAX_PATTERNS:
        raise EvidenceError(f"provide between 1 and {MAX_PATTERNS} patterns")
    encoded: list[tuple[str, bytes]] = []
    for pattern in patterns:
        try:
            value = pattern.encode("ascii")
        except UnicodeEncodeError as error:
            raise EvidenceError("search patterns must be ASCII") from error
        if not 1 <= len(value) <= MAX_PATTERN_BYTES:
            raise EvidenceError(
                f"search patterns must be between 1 and {MAX_PATTERN_BYTES} bytes"
            )
        if any(byte < 0x20 or byte > 0x7E for byte in value):
            raise EvidenceError("search patterns must use printable ASCII")
        encoded.append((pattern, value))

    ranges: list[dict[str, object]] = []
    direct_views: list[tuple[bytes, dict[str, Any]]] = []
    skipped: list[int] = []
    for segment in index["segments"]:
        if segment.get("mapping_status") != "direct" or segment["file_offset"] is None:
            skipped.append(int(segment["index"]))
            continue
        start = int(segment["file_offset"])
        size = int(segment["stored_size"])
        view = data[start : start + size]
        direct_views.append((view, segment))
        ranges.append(
            {
                "segment": segment["index"],
                "kind": segment["kind"],
                "logical_start": 0,
                "logical_end_exclusive": size,
                "file_offset": start,
                "size": size,
            }
        )

    results: list[dict[str, object]] = []
    for pattern, value in encoded:
        raw_hits: list[dict[str, int]] = []
        token_hit_count = 0
        for view, segment in direct_views:
            for offset in _find_offsets(view, value):
                token_hit_count += 1
                if len(raw_hits) < MAX_SEARCH_HITS_PER_PATTERN:
                    raw_hits.append(
                        {
                            "segment": int(segment["index"]),
                            "offset": offset,
                            "file_offset": int(segment["file_offset"]) + offset,
                        }
                    )

        candidate_targets: dict[int, list[tuple[int, int]]] = {}
        for hit in raw_hits:
            target_offset = int(hit["offset"])
            if target_offset <= 0xFFFF:
                candidate_targets.setdefault(target_offset, []).append(
                    (int(hit["segment"]), target_offset)
                )
        candidate_hits: list[dict[str, int]] = []
        candidate_total = 0
        candidate_scan_complete = True
        if not candidate_targets:
            direct_code_views: list[tuple[bytes, dict[str, Any]]] = []
        else:
            direct_code_views = direct_views
        for view, source_segment in direct_code_views:
            if source_segment["kind"] != "code" or len(view) < 2:
                continue
            for source_offset in range(len(view) - 1):
                word_value = view[source_offset] | (view[source_offset + 1] << 8)
                targets = candidate_targets.get(word_value)
                if targets is None:
                    continue
                for target_segment, target_offset in targets:
                    candidate_total += 1
                    if candidate_total <= MAX_XREF_FANOUT:
                        candidate_hits.append(
                            {
                                "source_segment": int(source_segment["index"]),
                                "source_offset": source_offset,
                                "source_file_offset": int(source_segment["file_offset"])
                                + source_offset,
                                "target_segment": target_segment,
                                "target_offset": target_offset,
                            }
                        )
                    else:
                        candidate_scan_complete = False
                        break
                if not candidate_scan_complete:
                    break
            if not candidate_scan_complete:
                break
        results.append(
            {
                "pattern": pattern,
                "encoding": "ascii_exact",
                "token_hit_count": token_hit_count,
                "token_hits": raw_hits,
                "token_hits_truncated": token_hit_count > len(raw_hits),
                "candidate_immediate_word_hit_count": candidate_total,
                "candidate_immediate_word_hits": candidate_hits,
                "candidate_scan_complete": candidate_scan_complete,
                "candidate_hits_truncated": not candidate_scan_complete,
                "candidate_targets_limited_to_reported_token_hits": (
                    token_hit_count > len(raw_hits)
                ),
                "candidate_warning": (
                    "raw little-endian word occurrences; validate instruction boundaries, "
                    "segment-register context, relocations, and control flow; an incomplete "
                    "scan cannot support negative evidence"
                ),
            }
        )
    return {
        "search_scope": {
            "direct_segment_ranges": ranges,
            "skipped_unmapped_segment_numbers": skipped,
            "maximum_patterns": MAX_PATTERNS,
            "maximum_pattern_bytes": MAX_PATTERN_BYTES,
            "maximum_reported_hits_per_pattern": MAX_SEARCH_HITS_PER_PATTERN,
            "maximum_candidate_xref_fanout": MAX_XREF_FANOUT,
            "maximum_xref_depth": MAX_XREF_DEPTH,
        },
        "results": results,
    }


def _instruction_lines(output: str, pattern: re.Pattern[str]) -> list[str]:
    lines = [
        " ".join(match.group(0).split())
        for line in output.splitlines()
        if (match := pattern.search(line))
    ]
    return lines[:MAX_PACKET_INSTRUCTIONS]


def _verified_decoder_path(name: str, identity: dict[str, object]) -> tuple[str, str] | None:
    if identity.get("available") is not True:
        return None
    expected = identity.get("executable_sha256")
    if not isinstance(expected, str):
        raise EvidenceError(f"manifest has no executable digest for decoder {name!r}")
    discovered = shutil.which(name)
    if discovered is None:
        raise EvidenceError(f"manifested decoder {name!r} is no longer available")
    try:
        resolved = str(Path(discovered).resolve(strict=True))
    except OSError as error:
        raise EvidenceError(f"cannot resolve decoder {name!r}: {error}") from error
    read_verified(resolved, expected_sha256=expected)
    return resolved, expected


def _decoder_result(
    identity: dict[str, object],
    digest: str | None,
    *,
    available: bool,
    exit_code: int | None = None,
    instructions: list[str] | None = None,
) -> dict[str, object]:
    return {
        "available": available,
        "manifest_version": identity.get("version"),
        "executable_sha256": digest,
        "exit_code": exit_code,
        "instructions": [] if instructions is None else instructions,
    }


def _decode_ndisasm(
    window: bytes, origin: int, identity: dict[str, object]
) -> dict[str, object]:
    verified = _verified_decoder_path("ndisasm", identity)
    if verified is None:
        return _decoder_result(identity, None, available=False)
    executable, digest = verified
    result = subprocess.run(
        [executable, "-b", "16", "-o", hex(origin), "-"],
        input=window,
        capture_output=True,
        check=False,
        timeout=DECODER_TIMEOUT_SECONDS,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    pattern = re.compile(r"^[0-9A-Fa-f]{8}\s+[0-9A-Fa-f]+\s+.+$")
    read_verified(executable, expected_sha256=digest)
    return _decoder_result(
        identity,
        digest,
        available=True,
        exit_code=result.returncode,
        instructions=_instruction_lines(result.stdout.decode("utf-8", "replace"), pattern),
    )


def _decode_cstool(
    window: bytes, origin: int, identity: dict[str, object]
) -> dict[str, object]:
    verified = _verified_decoder_path("cstool", identity)
    if verified is None:
        return _decoder_result(identity, None, available=False)
    executable, digest = verified
    result = subprocess.run(
        [executable, "x16", window.hex()],
        capture_output=True,
        check=False,
        timeout=DECODER_TIMEOUT_SECONDS,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    instructions: list[str] = []
    pattern = re.compile(r"^\s*([0-9a-fA-F]+)\s+([0-9a-fA-F ]+)\s{2,}(.+)$")
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        address = origin + int(match.group(1), 16)
        byte_text = "".join(match.group(2).split()).upper()
        instruction = " ".join(match.group(3).split())
        instructions.append(f"{address:08X} {byte_text} {instruction}")
        if len(instructions) == MAX_PACKET_INSTRUCTIONS:
            break
    read_verified(executable, expected_sha256=digest)
    return _decoder_result(
        identity,
        digest,
        available=True,
        exit_code=result.returncode,
        instructions=instructions,
    )


def _decode_objdump(
    window: bytes, origin: int, identity: dict[str, object]
) -> dict[str, object]:
    verified = _verified_decoder_path("objdump", identity)
    if verified is None:
        return _decoder_result(identity, None, available=False)
    executable, digest = verified
    old_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="amipro-ne-window-") as directory:
            path = Path(directory) / "window.bin"
            path.write_bytes(window)
            result = subprocess.run(
                [
                    executable,
                    "-D",
                    "-b",
                    "binary",
                    "-m",
                    "i8086",
                    f"--adjust-vma={origin}",
                    str(path),
                ],
                capture_output=True,
                check=False,
                timeout=DECODER_TIMEOUT_SECONDS,
                env={**os.environ, "LC_ALL": "C", "LANG": "C"},
            )
    finally:
        os.umask(old_umask)
    pattern = re.compile(r"^\s*[0-9a-fA-F]+:\s+(?:[0-9a-fA-F]{2}\s+)+.+$")
    read_verified(executable, expected_sha256=digest)
    return _decoder_result(
        identity,
        digest,
        available=True,
        exit_code=result.returncode,
        instructions=_instruction_lines(result.stdout.decode("utf-8", "replace"), pattern),
    )


DECODERS = {
    "ndisasm": _decode_ndisasm,
    "cstool": _decode_cstool,
    "objdump": _decode_objdump,
}


def evidence_packet(
    data: bytes,
    index: dict[str, Any],
    *,
    claim_id: str,
    segment_number: int,
    offset: int,
    byte_count: int,
    decoder_names: tuple[str, ...] = ("ndisasm", "cstool", "objdump"),
    decoder_identities: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    """Create one capped packet skeleton from a validated direct segment."""

    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", claim_id):
        raise EvidenceError("claim id must be 1-64 lowercase identifier characters")
    if not 1 <= byte_count <= MAX_PACKET_BYTES:
        raise EvidenceError(f"byte count must be between 1 and {MAX_PACKET_BYTES}")
    view, segment = _direct_segment(data, index, segment_number)
    if offset < 0 or offset > len(view) or byte_count > len(view) - offset:
        raise EvidenceError("packet window is outside initialized direct-segment bytes")
    window = view[offset : offset + byte_count]
    annotations: list[dict[str, object]] = []
    window_end = offset + byte_count
    for relocation in segment["relocations"]:
        chain = relocation["fixup_chain"]
        for fixup_offset in chain.get("offsets", []):
            source_width = relocation.get("source_width")
            width = int(source_width) if source_width is not None else 1
            fixup_start = int(fixup_offset)
            if fixup_start < window_end and fixup_start + width > offset:
                if len(annotations) == MAX_XREF_FANOUT:
                    raise EvidenceError(
                        f"packet overlaps more than {MAX_XREF_FANOUT} relocation sites"
                    )
                annotation: dict[str, object] = {
                    "fixup_offset": fixup_offset,
                    "source_width": source_width,
                    "source_type": relocation["source_type"],
                    "source_type_raw": relocation["source_type_raw"],
                    "target": relocation["target"],
                    "additive": relocation["additive"],
                }
                target = relocation["target"]
                if (
                    relocation["source_type"] == "selector"
                    and fixup_start >= 3
                    and view[fixup_start - 3] in {0x9A, 0xEA}
                    and isinstance(target, dict)
                    and target.get("kind") == "internal"
                ):
                    annotation["far_transfer_candidate"] = {
                        "instruction_offset": fixup_start - 3,
                        "opcode": "call" if view[fixup_start - 3] == 0x9A else "jump",
                        "target_segment": target["segment"],
                        "target_offset": int.from_bytes(
                            view[fixup_start - 2 : fixup_start], "little"
                        ),
                        "warning": (
                            "candidate derived from opcode and preceding offset word; "
                            "validate instruction boundary and control flow"
                        ),
                    }
                annotations.append(annotation)
    decoders: dict[str, object] = {}
    if decoder_names and decoder_identities is None:
        raise EvidenceError("decoder identities from a validated manifest are required")
    for name in decoder_names:
        decoder = DECODERS.get(name)
        if decoder is None:
            raise EvidenceError(f"unknown decoder {name!r}")
        identity = (decoder_identities or {}).get(name)
        if identity is None:
            raise EvidenceError(f"manifest has no identity for requested decoder {name!r}")
        decoders[name] = decoder(window, offset, identity)
    return {
        "claim_id": claim_id,
        "address": {"segment": segment_number, "offset": offset},
        "mapping": {
            "storage": "direct",
            "file_offset": int(segment["file_offset"]) + offset,
            "byte_count": byte_count,
        },
        "raw_bytes_hex": window.hex(),
        "relocation_annotations": annotations,
        "decoders": decoders,
        "review_fields": {
            "cross_reference_path": [],
            "inferred_behavior": None,
            "alternative_explanations": [],
            "negative_evidence": [],
            "confidence": "open",
        },
        "hard_limits": {
            "raw_bytes": MAX_PACKET_BYTES,
            "decoded_instructions_per_decoder": MAX_PACKET_INSTRUCTIONS,
            "strings": MAX_PATTERNS,
            "string_bytes": MAX_PATTERN_BYTES,
            "xref_depth": MAX_XREF_DEPTH,
            "xref_fanout": MAX_XREF_FANOUT,
            "relocation_annotations": MAX_XREF_FANOUT,
        },
    }


def _parse_int(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an integer (decimal or 0x-prefixed)") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--payload-dir",
        type=Path,
        default=os.environ.get("AMIPRO_PAYLOAD_DIR"),
        required="AMIPRO_PAYLOAD_DIR" not in os.environ,
    )
    parser.add_argument("--module", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    search = subparsers.add_parser("search", help="find exact ASCII tokens and word leads")
    search.add_argument("patterns", nargs="+")
    packet = subparsers.add_parser("packet", help="emit one bounded evidence packet")
    packet.add_argument("--claim-id", required=True)
    packet.add_argument("--segment", type=int, required=True)
    packet.add_argument("--offset", type=_parse_int, required=True)
    packet.add_argument("--byte-count", type=int, default=MAX_PACKET_BYTES)
    args = parser.parse_args(argv)

    try:
        manifest, manifest_digest, manifest_size = load_manifest(args.manifest)
        data, index, identity = load_module(manifest, args.payload_dir, args.module)
        if args.command == "search":
            analysis = search_tokens(data, index, args.patterns)
        else:
            analysis = evidence_packet(
                data,
                index,
                claim_id=args.claim_id,
                segment_number=args.segment,
                offset=args.offset,
                byte_count=args.byte_count,
                decoder_identities=_decoder_identities(manifest),
            )
    except (
        EvidenceError,
        NEFormatError,
        VerificationError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"evidence analysis failed: {error}", file=sys.stderr)
        return 2

    report = {
        "schema": "amipro-ne-evidence-output-v1",
        "manifest": {
            "size": manifest_size,
            "sha256": manifest_digest,
        },
        "module": {
            "name": identity["name"],
            "size": identity["size"],
            "sha256": identity["sha256"],
        },
        "analysis": analysis,
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
