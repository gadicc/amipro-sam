#!/usr/bin/env python3
"""Cross-check bounded NE metadata against an exactly manifested ``winedump``.

The command executed by this program is always ``winedump dump -x MODULE``.
Although that command prints resources and names, those sections are discarded:
only a small, documented header/segment grammar is parsed, and raw tool output is
never included in the JSON report.  Neither the module nor any vendor code is
executed.

The payload and tool are hash-gated before invocation and re-hashed afterwards.
Stdout, stderr, runtime, line count, segment count, and report detail are bounded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from evidence import EvidenceError, load_manifest, load_module
from ne import VerificationError, read_verified

SCHEMA = "amipro-winedump-crosscheck-v1"
MAX_STDOUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_OUTPUT_LINES = 100_000
MAX_SEGMENTS = 4_096
TIMEOUT_SECONDS = 15

_HEADER_PATTERNS = {
    "segment_count": re.compile(r"^Number of segments:\s+([0-9]+)\s*$"),
    "module_reference_count": re.compile(r"^Number of modrefs:\s+([0-9]+)\s*$"),
    "entry_point": re.compile(
        r"^Entry point:\s+([0-9a-fA-F]+):([0-9a-fA-F]+)\s*$"
    ),
}
_SEGMENT_HEADER = re.compile(r"^Segment ([0-9]+):\s*$")
_SEGMENT_FIELDS = {
    "file_offset": re.compile(r"^  File offset:\s+([0-9a-fA-F]{8})\s*$"),
    "stored_length": re.compile(r"^  Length:\s+([0-9a-fA-F]{8})\s*$"),
    "flags_raw": re.compile(
        r"^  Flags:\s+([0-9a-fA-F]{8})(?:\s+\([^\r\n]*\))?\s*$"
    ),
    "allocation_size": re.compile(r"^  Alloc size:\s+([0-9a-fA-F]{8})\s*$"),
}
_RELOCATION_HEADER = "  Relocations:"
# winedump right-aligns the decimal ordinal, so the indentation shrinks once
# it reaches 10.  This grammar is active only after a segment's relocation
# header; contiguous ordinal validation then fails closed on any stray line.
_RELOCATION_RECORD = re.compile(r"^\s+([0-9]+):\s+.+$")


class CrosscheckError(RuntimeError):
    """A safety gate, bounded invocation, or required invariant failed."""


def _one_match(lines: list[str], name: str, pattern: re.Pattern[str]) -> re.Match[str]:
    matches = [match for line in lines if (match := pattern.fullmatch(line))]
    if len(matches) != 1:
        raise CrosscheckError(
            f"winedump header invariant {name!r} occurred {len(matches)} times"
        )
    return matches[0]


def _finish_segment(segment: dict[str, Any]) -> dict[str, int]:
    required = {"index", *_SEGMENT_FIELDS, "relocation_ordinals"}
    missing = sorted(required - segment.keys())
    if missing:
        raise CrosscheckError(
            f"winedump segment {segment.get('index', '?')} lacks required fields: "
            + ", ".join(missing)
        )
    ordinals = segment.pop("relocation_ordinals")
    if not isinstance(ordinals, list) or ordinals != list(range(1, len(ordinals) + 1)):
        raise CrosscheckError(
            f"winedump segment {segment['index']} has incomplete relocation numbering"
        )
    segment["relocation_record_count"] = len(ordinals)
    return {name: int(value) for name, value in segment.items()}


def parse_winedump(text: str) -> dict[str, Any]:
    """Parse only stable path-free header and segment invariants.

    Resource tables, names, exports, byte dumps, and the path-bearing banner and
    trailer are deliberately ignored.  Every required invariant must appear
    exactly once, and segment/relocation ordinals must be contiguous.
    """

    lines = text.splitlines()
    if len(lines) > MAX_OUTPUT_LINES:
        raise CrosscheckError(
            f"winedump output has more than the {MAX_OUTPUT_LINES}-line parse cap"
        )

    segment_match = _one_match(
        lines, "segment_count", _HEADER_PATTERNS["segment_count"]
    )
    modref_match = _one_match(
        lines,
        "module_reference_count",
        _HEADER_PATTERNS["module_reference_count"],
    )
    entry_match = _one_match(lines, "entry_point", _HEADER_PATTERNS["entry_point"])
    segment_count = int(segment_match.group(1), 10)
    if not 1 <= segment_count <= MAX_SEGMENTS:
        raise CrosscheckError(
            f"winedump segment count is outside the 1..{MAX_SEGMENTS} cap"
        )

    segments: list[dict[str, int]] = []
    current: dict[str, Any] | None = None
    in_relocations = False
    for line in lines:
        header = _SEGMENT_HEADER.fullmatch(line)
        if header is not None:
            if current is not None:
                segments.append(_finish_segment(current))
            index = int(header.group(1), 10)
            current = {"index": index, "relocation_ordinals": []}
            in_relocations = False
            continue
        if current is None:
            continue
        if line == _RELOCATION_HEADER:
            if in_relocations:
                raise CrosscheckError(
                    f"winedump segment {current['index']} repeats relocation header"
                )
            in_relocations = True
            continue
        field_match = None
        for name, pattern in _SEGMENT_FIELDS.items():
            if (field_match := pattern.fullmatch(line)) is not None:
                if name in current:
                    raise CrosscheckError(
                        f"winedump segment {current['index']} repeats field {name!r}"
                    )
                current[name] = int(field_match.group(1), 16)
                break
        if field_match is not None:
            continue
        if in_relocations and (record := _RELOCATION_RECORD.fullmatch(line)) is not None:
            current["relocation_ordinals"].append(int(record.group(1), 10))

    if current is not None:
        segments.append(_finish_segment(current))
    expected_ordinals = list(range(1, segment_count + 1))
    observed_ordinals = [segment["index"] for segment in segments]
    if observed_ordinals != expected_ordinals:
        raise CrosscheckError(
            "winedump output does not contain one contiguous block for every segment"
        )
    return {
        "entry_point": {
            "cs": int(entry_match.group(1), 16),
            "ip": int(entry_match.group(2), 16),
        },
        "segment_count": segment_count,
        "module_reference_count": int(modref_match.group(1), 10),
        "segments": segments,
    }


def expected_invariants(index: dict[str, Any]) -> dict[str, Any]:
    """Select the local NE indexer's stable invariants for comparison."""

    header = index["header"]
    segments = [
        {
            "index": int(segment["index"]),
            "file_offset": int(segment["file_offset"] or 0),
            "stored_length": int(segment["stored_size"]),
            "flags_raw": int(segment["flags_raw"]),
            "allocation_size": int(segment["allocation_size"]),
            "relocation_record_count": len(segment["relocations"]),
        }
        for segment in index["segments"]
    ]
    return {
        "entry_point": {
            "cs": int(header["initial_cs"]),
            "ip": int(header["initial_ip"]),
        },
        "segment_count": int(header["segment_count"]),
        "module_reference_count": int(header["module_reference_count"]),
        "segments": segments,
    }


def _validate_manifest_summary(
    identity: dict[str, Any], expected: dict[str, Any]
) -> None:
    ne = identity.get("ne")
    if not isinstance(ne, dict):
        raise CrosscheckError("manifest module has no NE structural summary")
    fields = {
        "segment_count": expected["segment_count"],
        "module_reference_count": expected["module_reference_count"],
        "initial_cs": expected["entry_point"]["cs"],
        "initial_ip": expected["entry_point"]["ip"],
        "relocation_count": sum(
            segment["relocation_record_count"] for segment in expected["segments"]
        ),
    }
    inconsistent = [name for name, value in fields.items() if ne.get(name) != value]
    if inconsistent:
        raise CrosscheckError(
            "manifest NE summary disagrees with hash-gated bytes for: "
            + ", ".join(sorted(inconsistent))
        )


def compare_invariants(
    expected: dict[str, Any], observed: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return aggregate matches and bounded per-field disagreements."""

    matches: list[dict[str, Any]] = []
    disagreements: list[dict[str, Any]] = []

    for name in ("entry_point", "segment_count", "module_reference_count"):
        if expected[name] == observed[name]:
            matches.append(
                {
                    "invariant": name,
                    "expected": expected[name],
                    "observed": observed[name],
                }
            )
        else:
            disagreements.append(
                {
                    "invariant": name,
                    "expected": expected[name],
                    "observed": observed[name],
                }
            )

    expected_segments = expected["segments"]
    observed_segments = observed["segments"]
    segment_fields = (
        "file_offset",
        "stored_length",
        "flags_raw",
        "allocation_size",
        "relocation_record_count",
    )
    for field in segment_fields:
        field_disagreements = []
        for expected_segment, observed_segment in zip(
            expected_segments, observed_segments, strict=False
        ):
            if expected_segment.get(field) != observed_segment.get(field):
                field_disagreements.append(
                    {
                        "segment": expected_segment.get("index"),
                        "expected": expected_segment.get(field),
                        "observed": observed_segment.get(field),
                    }
                )
        if len(expected_segments) != len(observed_segments):
            field_disagreements.append(
                {
                    "segment": None,
                    "expected_segment_count": len(expected_segments),
                    "observed_segment_count": len(observed_segments),
                }
            )
        if field_disagreements:
            disagreements.append(
                {
                    "invariant": f"segment_{field}",
                    "details": field_disagreements[:MAX_SEGMENTS],
                    "details_complete": len(field_disagreements) <= MAX_SEGMENTS,
                }
            )
        else:
            matches.append(
                {
                    "invariant": f"segment_{field}",
                    "compared_segments": len(expected_segments),
                }
            )
    return matches, disagreements


def _run_bounded(executable: str, module_path: Path) -> dict[str, Any]:
    """Run the one allowed command while retaining at most the declared caps."""

    old_umask = os.umask(0o077)
    try:
        temporary = tempfile.TemporaryDirectory(prefix="amipro-winedump-")
    finally:
        os.umask(old_umask)
    with temporary as directory:
        environment = {
            **os.environ,
            "LC_ALL": "C",
            "LANG": "C",
            "XDG_CACHE_HOME": str(Path(directory) / "cache"),
            "XDG_CONFIG_HOME": str(Path(directory) / "config"),
            "XDG_DATA_HOME": str(Path(directory) / "data"),
        }
        try:
            process = subprocess.Popen(
                [executable, "dump", "-x", str(module_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                start_new_session=True,
            )
        except OSError as error:
            raise CrosscheckError(
                f"could not start manifested winedump ({type(error).__name__})"
            ) from error
        return _collect_bounded_process(process)


def _collect_bounded_process(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    """Drain one already-started process without exceeding output/time limits."""

    assert process.stdout is not None
    assert process.stderr is not None

    selector = selectors.DefaultSelector()
    retained = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    limits = {"stdout": MAX_STDOUT_BYTES, "stderr": MAX_STDERR_BYTES}
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, data=name)

    deadline = time.monotonic() + TIMEOUT_SECONDS
    failure: CrosscheckError | None = None
    try:
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = CrosscheckError(
                        f"winedump exceeded the {TIMEOUT_SECONDS}-second timeout"
                    )
                    break
                events = selector.select(min(remaining, 0.25))
                for key, _ in events:
                    name = key.data
                    try:
                        chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    totals[name] += len(chunk)
                    if totals[name] > limits[name]:
                        failure = CrosscheckError(
                            f"winedump {name} exceeded its {limits[name]}-byte cap"
                        )
                        break
                    retained[name].extend(chunk)
                if failure is not None:
                    break
        finally:
            selector.close()
    except BaseException:
        process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()
        raise

    if failure is not None:
        process.kill()
    try:
        exit_code = process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        if failure is None:
            failure = CrosscheckError("winedump did not terminate after output closed")
    finally:
        process.stdout.close()
        process.stderr.close()
    if failure is not None:
        raise failure
    return {
        "exit_code": exit_code,
        "stdout": bytes(retained["stdout"]),
        "stderr_bytes": totals["stderr"],
    }


def _manifested_winedump(manifest: dict[str, Any]) -> tuple[str, str, str | None]:
    probes = [
        probe
        for probe in manifest["tools"]["probes"]
        if probe.get("name") == "winedump"
    ]
    if len(probes) != 1 or probes[0].get("available") is not True:
        raise CrosscheckError("manifest does not identify one available winedump")
    identity = probes[0]
    digest = identity.get("executable_sha256")
    if not isinstance(digest, str):
        raise CrosscheckError("manifested winedump has no executable SHA-256")
    version = identity.get("version")
    if version is not None and not isinstance(version, str):
        raise CrosscheckError("manifested winedump version must be text or null")
    discovered = shutil.which("winedump")
    if discovered is None:
        raise CrosscheckError("manifested winedump is no longer available")
    try:
        executable = str(Path(discovered).resolve(strict=True))
    except OSError as error:
        raise CrosscheckError(
            f"could not resolve manifested winedump ({type(error).__name__})"
        ) from error
    read_verified(executable, expected_sha256=digest)
    return executable, digest, version


def crosscheck(manifest_path: Path, payload_dir: Path, module_name: str) -> dict[str, Any]:
    """Hash-gate, invoke, re-hash, parse, and compare one manifested module."""

    manifest, manifest_digest, manifest_size = load_manifest(manifest_path)
    module_bytes, index, identity = load_module(manifest, payload_dir, module_name)
    del module_bytes
    expected = expected_invariants(index)
    _validate_manifest_summary(identity, expected)
    executable, executable_digest, version = _manifested_winedump(manifest)
    module_path = payload_dir / module_name

    run_result: dict[str, Any] | None = None
    run_failure: Exception | None = None
    try:
        run_result = _run_bounded(executable, module_path)
    except Exception as error:  # preserve post-invocation integrity checks
        run_failure = error
    tool_after = read_verified(executable, expected_sha256=executable_digest)
    module_after = read_verified(
        module_path,
        expected_size=int(identity["size"]),
        expected_sha256=str(identity["sha256"]),
    )
    if run_failure is not None:
        raise run_failure
    assert run_result is not None
    if run_result["exit_code"] != 0:
        raise CrosscheckError(
            f"winedump returned nonzero exit code {run_result['exit_code']}"
        )
    try:
        text = run_result["stdout"].decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise CrosscheckError("winedump stdout is not valid UTF-8/ASCII") from error
    observed = parse_winedump(text)
    matches, disagreements = compare_invariants(expected, observed)
    iterated_count = sum(
        segment.get("storage") == "iterated" for segment in index["segments"]
    )
    warnings = [
        "resource, name, export, byte-dump, banner, and trailer output was discarded",
        "winedump exposes no version switch; identity is the exact executable SHA-256",
    ]
    if iterated_count:
        warnings.append(
            "iterated-segment decoding was not assessed; only stored metadata was compared"
        )
    return {
        "schema": SCHEMA,
        "status": "pass" if not disagreements else "fail",
        "module": {
            "name": module_name,
            "size": module_after.size,
            "sha256": module_after.sha256,
        },
        "manifest": {"size": manifest_size, "sha256": manifest_digest},
        "tool": {
            "name": "winedump",
            "manifest_version": version,
            "executable_size": tool_after.size,
            "executable_sha256": tool_after.sha256,
        },
        "invocation": {
            "arguments": ["dump", "-x", "MODULE"],
            "timeout_seconds": TIMEOUT_SECONDS,
            "stdout_cap_bytes": MAX_STDOUT_BYTES,
            "stderr_cap_bytes": MAX_STDERR_BYTES,
            "stdout_bytes": len(run_result["stdout"]),
            "stderr_bytes": run_result["stderr_bytes"],
            "exit_code": run_result["exit_code"],
        },
        "scope": {
            "parsed_invariants": [
                "entry_point",
                "module_reference_count",
                "segment_count",
                "segment_file_offset",
                "segment_stored_length",
                "segment_flags_raw",
                "segment_allocation_size",
                "segment_relocation_record_count",
            ],
            "segment_count": len(expected["segments"]),
            "iterated_segment_count": iterated_count,
        },
        "matches": matches,
        "disagreements": disagreements,
        "warnings": warnings,
    }


def _redact_absolute_paths(message: str, paths: list[Path]) -> str:
    redacted = message
    for path in paths:
        value = str(path)
        if value:
            redacted = redacted.replace(value, "<path>")
        try:
            resolved = str(path.resolve(strict=False))
        except OSError:
            continue
        if resolved:
            redacted = redacted.replace(resolved, "<path>")
    return re.sub(r"(?<![A-Za-z0-9])/(?:[^\s:'\"]+/?)+", "<path>", redacted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--payload-dir", required=True, type=Path)
    parser.add_argument("--module", required=True)
    args = parser.parse_args(argv)
    try:
        report = crosscheck(args.manifest, args.payload_dir, args.module)
        exit_code = 0 if report["status"] == "pass" else 1
    except (CrosscheckError, EvidenceError, VerificationError, OSError) as error:
        report = {
            "schema": SCHEMA,
            "status": "error",
            "error": {
                "type": type(error).__name__,
                "message": _redact_absolute_paths(
                    str(error), [args.manifest, args.payload_dir]
                ),
            },
        }
        exit_code = 2
    except Exception as error:  # keep even unexpected failures path-free
        report = {
            "schema": SCHEMA,
            "status": "error",
            "error": {
                "type": type(error).__name__,
                "message": "unexpected cross-check failure",
            },
        }
        exit_code = 2
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
