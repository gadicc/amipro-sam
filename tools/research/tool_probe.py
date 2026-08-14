#!/usr/bin/env python3
"""Emit a deterministic inventory of local NE-analysis tool capabilities.

Only version/help probes are executed.  This module never invokes ``winedbg`` or
passes a vendor executable to a tool; format-behaviour probes belong in the
hash-gated analysis workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_VERSION_CHARS = 160
MAX_EXECUTABLE_BYTES = 64 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 5
SCHEMA = "amipro-ne-tool-probes-v1"

PYTHON_MODULE_SPECS: tuple[tuple[str, str], ...] = (
    ("pefile", "PE parser; rejects NE containers in the recorded behavior probe"),
)


@dataclass(frozen=True)
class ToolProbe:
    name: str
    available: bool
    version: str | None
    probe_args: tuple[str, ...]
    probe_exit_code: int | None
    executable_sha256: str | None
    research_context: str


TOOL_SPECS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("file", ("--version",), "recognition only; no segment or xref model"),
    ("sha256sum", ("--version",), "input-integrity utility"),
    ("winedump", ("--help",), "NE metadata/resource dumper; no disassembly"),
    ("wine", ("--version",), "version query only; vendor execution is forbidden"),
    ("objdump", ("--version",), "rejects NE container; raw binary/i8086 adapter"),
    ("objcopy", ("--version",), "container capability probe only"),
    ("llvm-objdump", ("--version",), "rejects NE container"),
    ("llvm-readobj", ("--version",), "rejects NE container"),
    ("ndisasm", ("-v",), "raw x86-16 decoder; no NE mapping or fixups"),
    ("cstool", ("-v",), "raw x86-16 decoder; no NE loader"),
    ("7z", ("i",), "archive/resource capability probe"),
    ("wrestool", ("--version",), "resource extractor; extraction remains disabled"),
    ("icotool", ("--version",), "resource extractor; extraction remains disabled"),
    ("ghidra", ("--version",), "not used without a validated custom mapping"),
    ("analyzeHeadless", ("-help",), "not used without a validated custom mapping"),
    ("radare2", ("-v",), "NE mapping must be independently validated"),
    ("rabin2", ("-v",), "NE mapping must be independently validated"),
    ("rizin", ("-v",), "unavailable tools remain unassessed"),
    ("rz-bin", ("-v",), "unavailable tools remain unassessed"),
    ("pedump", ("--version",), "NE capability must be observed, not assumed"),
    ("readpe", ("--version",), "NE capability must be observed, not assumed"),
    ("pev", ("--version",), "NE capability must be observed, not assumed"),
    ("restool", ("--version",), "resource extraction remains disabled"),
)


def _bounded_text(stdout: bytes, stderr: bytes) -> str | None:
    combined = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    lines = [" ".join(line.split()) for line in combined.splitlines() if line.strip()]
    if not lines:
        return None
    selected = next(
        (
            line
            for line in lines
            if "version" in line.casefold()
            or any(character.isdigit() for character in line)
        ),
        lines[0],
    )
    return selected[:MAX_VERSION_CHARS]


def _hash_executable(path: str) -> str | None:
    try:
        resolved = Path(path).resolve(strict=True)
        stat = resolved.stat()
        if not resolved.is_file() or not 0 <= stat.st_size <= MAX_EXECUTABLE_BYTES:
            return None
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _probe(name: str, args: tuple[str, ...], context: str, temp_root: Path) -> ToolProbe:
    executable = shutil.which(name)
    if executable is None:
        return ToolProbe(name, False, None, args, None, None, context)

    environment = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "XDG_CACHE_HOME": str(temp_root / "cache"),
        "XDG_CONFIG_HOME": str(temp_root / "config"),
        "XDG_DATA_HOME": str(temp_root / "data"),
    }
    try:
        result = subprocess.run(
            [executable, *args],
            check=False,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            env=environment,
        )
        version = _bounded_text(result.stdout, result.stderr)
        if name == "winedump":
            # Wine's dumper exposes help, but no version switch.  Recording an
            # arbitrary digit-bearing help line as a version would be worse
            # than an explicit unknown; the adjacent Wine probe identifies the
            # installed suite version without claiming they are identical.
            version = None
        return ToolProbe(
            name=name,
            available=True,
            version=version,
            probe_args=args,
            probe_exit_code=result.returncode,
            executable_sha256=_hash_executable(executable),
            research_context=context,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return ToolProbe(
            name=name,
            available=True,
            version=None,
            probe_args=args,
            probe_exit_code=None,
            executable_sha256=_hash_executable(executable),
            research_context=f"{context}; probe failed: {type(error).__name__}",
        )


def collect_tool_probes() -> list[dict[str, object]]:
    old_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="amipro-ne-tools-") as directory:
            temp_root = Path(directory)
            return [
                asdict(_probe(name, args, note, temp_root))
                for name, args, note in TOOL_SPECS
            ]
    finally:
        os.umask(old_umask)


def collect_python_module_probes() -> list[dict[str, object]]:
    """Record deterministic package versions without importing their code."""

    probes: list[dict[str, object]] = []
    for name, context in PYTHON_MODULE_SPECS:
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = None
        probes.append(
            {
                "name": name,
                "available": version is not None,
                "version": version,
                "research_context": context,
            }
        )
    return probes


def build_tool_report(
    probes: list[dict[str, object]] | None = None,
    python_modules: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Return the one canonical, deterministic tool-report schema."""

    return {
        "schema": SCHEMA,
        "python": {
            "implementation": sys.implementation.name,
            "version": ".".join(str(value) for value in sys.version_info[:3]),
        },
        "python_modules": (
            collect_python_module_probes()
            if python_modules is None
            else python_modules
        ),
        "probes": collect_tool_probes() if probes is None else probes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write JSON here instead of stdout")
    args = parser.parse_args(argv)
    payload = build_tool_report()
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
