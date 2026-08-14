from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from evidence import (  # noqa: E402
    EvidenceError,
    _verified_decoder_path,
    evidence_packet,
    load_manifest,
    load_module,
    search_tokens,
)
from inventory import (  # noqa: E402
    PRIMARY_NAME,
    PRIMARY_SHA256,
    PRIMARY_SIZE,
    SCHEMA,
    inventory_payload,
)
from ne import VerificationError, parse_ne  # noqa: E402
from test_ne import ALIGNMENT_SHIFT, NE_OFFSET, SEGMENT_TABLE_RELATIVE, invented_ne  # noqa: E402


def _token_ne() -> bytes:
    data = bytearray(invented_ne())
    sector = struct.unpack_from("<H", data, NE_OFFSET + SEGMENT_TABLE_RELATIVE)[0]
    start = sector << ALIGNMENT_SHIFT
    data[start + 12 : start + 15] = b"[x]"
    return bytes(data)


def _manifest(tmp_path: Path) -> tuple[Path, Path, bytes]:
    payload = tmp_path / "payload"
    payload.mkdir()
    module = _token_ne()
    (payload / "AMIPRO.EXE").write_bytes(module)
    report = inventory_payload(
        payload,
        primary_size=len(module),
        primary_sha256=hashlib.sha256(module).hexdigest(),
        tool_probes=[],
    )
    report["trust_anchor"] = {
        "name": PRIMARY_NAME,
        "size": PRIMARY_SIZE,
        "sha256": PRIMARY_SHA256,
    }
    report["modules"][0]["size"] = PRIMARY_SIZE
    report["modules"][0]["sha256"] = PRIMARY_SHA256
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    return payload, path, module


def test_search_reports_exact_token_and_full_scope() -> None:
    data = _token_ne()
    report = search_tokens(data, parse_ne(data), ["[x]"])
    result = report["results"][0]
    assert result["token_hit_count"] == 1
    assert result["token_hits"][0]["offset"] == 12
    assert report["search_scope"]["direct_segment_ranges"][0]["logical_end_exclusive"] == 16


def test_packet_limits_and_relocation_annotation() -> None:
    data = invented_ne()
    packet = evidence_packet(
        data,
        parse_ne(data),
        claim_id="synthetic.fixup",
        segment_number=1,
        offset=0,
        byte_count=12,
        decoder_names=(),
    )
    assert len(bytes.fromhex(packet["raw_bytes_hex"])) == 12
    assert packet["relocation_annotations"][0]["fixup_offset"] == 0
    with pytest.raises(EvidenceError, match="between 1 and 64"):
        evidence_packet(
            data,
            parse_ne(data),
            claim_id="synthetic.too-large",
            segment_number=1,
            offset=0,
            byte_count=65,
            decoder_names=(),
        )


def test_manifest_stage_two_gate_detects_changed_module(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    module = _token_ne()
    (payload / "AMIPRO.EXE").write_bytes(module)
    manifest = inventory_payload(
        payload,
        primary_size=len(module),
        primary_sha256=hashlib.sha256(module).hexdigest(),
        tool_probes=[],
    )
    load_module(manifest, payload, "AMIPRO.EXE")
    module_path = payload / "AMIPRO.EXE"
    changed = bytearray(module_path.read_bytes())
    changed[-1] ^= 1
    module_path.write_bytes(changed)
    with pytest.raises(VerificationError, match="SHA-256 mismatch"):
        load_module(manifest, payload, "AMIPRO.EXE")


def test_manifest_schema_and_module_path_are_constrained(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "trust_anchor": {
                    "name": PRIMARY_NAME,
                    "size": PRIMARY_SIZE,
                    "sha256": PRIMARY_SHA256,
                },
                "modules": [],
                "payload_summary": {"ne_module_count": 0},
                "tools": {
                    "schema": "amipro-ne-tool-probes-v1",
                    "python": {"implementation": "cpython", "version": "3.test"},
                    "probes": [],
                },
            }
        )
    )
    with pytest.raises(EvidenceError, match="module list is empty"):
        load_manifest(bad)
    _, good, _ = _manifest(tmp_path)
    manifest, _, _ = load_manifest(good)
    with pytest.raises(EvidenceError, match="simple basename"):
        load_module(manifest, tmp_path, "../AMIPRO.EXE")


def test_manifest_rejects_primary_module_identity_forgery(tmp_path: Path) -> None:
    _, path, _ = _manifest(tmp_path)
    report = json.loads(path.read_text(encoding="utf-8"))
    report["modules"][0]["size"] += 1
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(EvidenceError, match="does not match the primary"):
        load_manifest(path)


def test_decoder_binary_must_match_manifest_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "synthetic-decoder"
    executable.write_bytes(b"invented decoder bytes")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    monkeypatch.setattr("evidence.shutil.which", lambda _: str(executable))
    identity = {"available": True, "executable_sha256": digest, "version": "test"}
    assert _verified_decoder_path("synthetic", identity) == (str(executable), digest)
    identity["executable_sha256"] = "0" * 64
    with pytest.raises(VerificationError, match="SHA-256 mismatch"):
        _verified_decoder_path("synthetic", identity)


def test_direct_selfload_mapping_is_refused() -> None:
    data = invented_ne()
    index = parse_ne(data)
    index["segments"][0]["mapping_status"] = "unsupported_selfload"
    report = search_tokens(data, index, ["[x]"])
    assert report["search_scope"]["skipped_unmapped_segment_numbers"] == [1]


def test_packet_relocation_overlap_and_fanout_are_bounded() -> None:
    data = invented_ne()
    index = parse_ne(data)
    relocation = index["segments"][0]["relocations"][0]
    relocation["source_width"] = 4
    relocation["fixup_chain"]["offsets"] = [0]
    packet = evidence_packet(
        data,
        index,
        claim_id="synthetic.overlap",
        segment_number=1,
        offset=2,
        byte_count=4,
        decoder_names=(),
    )
    assert packet["relocation_annotations"][0]["source_width"] == 4
    relocation["fixup_chain"]["offsets"] = [0] * 25
    with pytest.raises(EvidenceError, match="more than 24"):
        evidence_packet(
            data,
            index,
            claim_id="synthetic.fanout",
            segment_number=1,
            offset=0,
            byte_count=4,
            decoder_names=(),
        )


def test_packet_resolves_internal_far_call_candidate() -> None:
    data = bytearray(invented_ne())
    index = parse_ne(bytes(data))
    segment = index["segments"][0]
    start = int(segment["file_offset"])
    data[start : start + 5] = b"\x9a\x34\x12\xff\xff"
    relocation = segment["relocations"][0]
    relocation["fixup_chain"]["offsets"] = [3]
    relocation["source_type"] = "selector"
    relocation["source_width"] = 2
    relocation["target"] = {"kind": "internal", "segment": 1, "offset": 0}
    annotation = evidence_packet(
        bytes(data),
        index,
        claim_id="synthetic.far-call",
        segment_number=1,
        offset=0,
        byte_count=8,
        decoder_names=(),
    )["relocation_annotations"][0]
    assert annotation["far_transfer_candidate"] == {
        "instruction_offset": 0,
        "opcode": "call",
        "target_segment": 1,
        "target_offset": 0x1234,
        "warning": (
            "candidate derived from opcode and preceding offset word; "
            "validate instruction boundary and control flow"
        ),
    }
