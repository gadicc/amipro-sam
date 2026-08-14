#!/usr/bin/env python3
"""Fail if Git-visible candidates resemble proprietary Ami Pro/guest assets.

Ignored paths are obtained from Git's own candidate lists and are never walked.
In particular, this tool never opens ``mydocs/``.  Findings are conservative
review prompts; the tool deletes or modifies nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

MAX_CANDIDATES = 50_000
MAX_BLOB_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BLOB_BYTES = 512 * 1024 * 1024

OPEN_FONT_ALLOWLIST = {
    "src/amipro_sam/assets/fonts/AmiProPreservationCJK-Regular.ttf": (
        "267a6ba550900fec48fd45d8a4fd5f8941f6cff5db9a0f8b313d3b31966da2c0"
    ),
    "src/amipro_sam/assets/fonts/DejaVuSans-Bold.ttf": (
        "6b4f83ef68e461c05a8d8b218177936226a32f746044cfc10e4b9351c4a9415d"
    ),
    "src/amipro_sam/assets/fonts/DejaVuSans-BoldOblique.ttf": (
        "6d26ecff69d04ad88af75bb046370d6f52d8908a97632cee8cc8682638dc9758"
    ),
    "src/amipro_sam/assets/fonts/DejaVuSans-Oblique.ttf": (
        "6c4bf004bd06ad8b16ac3be38627e6cfd7f7da01b6563ddf6d385f227a8f28ac"
    ),
    "src/amipro_sam/assets/fonts/DejaVuSans.ttf": (
        "8a301f4fc28b4cadd8668f41c61217e200ffd3e069d2912966b5a2903ab09434"
    ),
}

SYNTHETIC_SAM_ALLOWLIST = {
    "tests/fixtures/synthetic-basic.sam": (
        "5b2a9df523ce36a7b79e2b5a1071b6a5df768531357f8e33b62ec2ec33679b52"
    )
}

RISKY_EXTENSIONS = {
    ".arj",
    ".bmp",
    ".bmt",
    ".cab",
    ".dll",
    ".exe",
    ".flt",
    ".fon",
    ".hlp",
    ".ima",
    ".img",
    ".icn",
    ".iso",
    ".qcow2",
    ".sdw",
    ".smi",
    ".smm",
    ".str",
    ".tbl",
    ".vhd",
    ".vhdx",
    ".vmdk",
}
VENDOR_NAME = re.compile(r"^(?:ami|lotus|lts|w4w)", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    scope: str
    path: str
    reason: str
    sha256: str | None = None


class AuditError(RuntimeError):
    pass


def _run_git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        input=input_bytes,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace")[:500]
        raise AuditError(f"git {' '.join(args)} failed: {message}")
    return result.stdout


def _nul_paths(value: bytes) -> list[str]:
    paths = [item.decode("utf-8", errors="surrogateescape") for item in value.split(b"\0") if item]
    if len(paths) > MAX_CANDIDATES:
        raise AuditError(f"candidate count exceeds {MAX_CANDIDATES}")
    return paths


def _is_private_path(path: str) -> bool:
    return any(part.casefold() == "mydocs" for part in PurePosixPath(path).parts)


def _blob_from_index(root: Path, path: str) -> bytes:
    size_raw = _run_git(root, "cat-file", "-s", f":{path}")
    try:
        size = int(size_raw.strip())
    except ValueError as error:
        raise AuditError(f"invalid staged blob size for {path!r}") from error
    if not 0 <= size <= MAX_BLOB_BYTES:
        raise AuditError(f"staged blob {path!r} exceeds {MAX_BLOB_BYTES} bytes")
    return _run_git(root, "cat-file", "blob", f":{path}")


def _blob_from_worktree(root: Path, path: str) -> bytes:
    candidate = root / path
    try:
        info = candidate.lstat()
    except OSError as error:
        raise AuditError(f"cannot stat untracked candidate {path!r}: {error}") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise AuditError(f"untracked candidate is not a regular non-symlink file: {path!r}")
    if not 0 <= info.st_size <= MAX_BLOB_BYTES:
        raise AuditError(f"untracked candidate {path!r} exceeds {MAX_BLOB_BYTES} bytes")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise AuditError(f"cannot open untracked candidate {path!r}: {error}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BLOB_BYTES:
            raise AuditError(
                f"untracked candidate changed or exceeds the byte cap: {path!r}"
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise AuditError(f"untracked candidate shortened during read: {path!r}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AuditError(f"untracked candidate grew during read: {path!r}")
        after = os.fstat(descriptor)
        def identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
        if identity(before) != identity(after) or identity(info) != identity(before):
            raise AuditError(f"untracked candidate changed during read: {path!r}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _blob_from_head(root: Path, path: str) -> bytes:
    size_raw = _run_git(root, "cat-file", "-s", f"HEAD:{path}")
    try:
        size = int(size_raw.strip())
    except ValueError as error:
        raise AuditError(f"invalid tracked blob size for {path!r}") from error
    if not 0 <= size <= MAX_BLOB_BYTES:
        raise AuditError(f"tracked blob {path!r} exceeds {MAX_BLOB_BYTES} bytes")
    return _run_git(root, "cat-file", "blob", f"HEAD:{path}")


def _executable_signature(data: bytes) -> str | None:
    if len(data) < 64 or data[:2] != b"MZ":
        return None
    new_header = struct.unpack_from("<I", data, 0x3C)[0]
    if new_header + 2 > len(data):
        return "MZ executable with out-of-range secondary header"
    signature = data[new_header : new_header + 4]
    if signature[:2] == b"NE":
        return "Windows NE executable/container signature"
    if signature == b"PE\0\0":
        return "Windows PE executable/container signature"
    if signature[:2] in {b"LE", b"LX"}:
        return "Windows/OS2 linear executable/container signature"
    return "DOS MZ executable/container signature"


def _allowlisted(path: str, digest: str) -> bool:
    expected = OPEN_FONT_ALLOWLIST.get(path) or SYNTHETIC_SAM_ALLOWLIST.get(path)
    return expected == digest


def _inspect(scope: str, path: str, data: bytes) -> list[Finding]:
    digest = hashlib.sha256(data).hexdigest()
    if _allowlisted(path, digest):
        return []
    findings: list[Finding] = []
    suffix = PurePosixPath(path).suffix.casefold()
    basename = PurePosixPath(path).name
    signature = _executable_signature(data)
    if signature:
        findings.append(Finding(scope, path, signature, digest))
    if suffix in RISKY_EXTENSIONS:
        findings.append(Finding(scope, path, f"review-required asset extension {suffix}", digest))
    if suffix == ".sam":
        findings.append(
            Finding(scope, path, "SAM document is not the reviewed synthetic fixture", digest)
        )
    if suffix == ".ttf":
        findings.append(
            Finding(scope, path, "font is not in the reviewed open-font allowlist", digest)
        )
    if VENDOR_NAME.match(basename) and suffix in RISKY_EXTENSIONS:
        findings.append(Finding(scope, path, "vendor-like basename and asset extension", digest))
    if data.startswith(b"`\xea"):
        findings.append(Finding(scope, path, "ARJ archive signature", digest))
    if data.startswith(b"MSCF"):
        findings.append(Finding(scope, path, "CAB archive signature", digest))
    return findings


def audit(root: Path) -> dict[str, object]:
    tracked = _nul_paths(_run_git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD"))
    staged = _nul_paths(
        _run_git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    )
    untracked = _nul_paths(_run_git(root, "ls-files", "--others", "--exclude-standard", "-z"))

    findings: list[Finding] = []
    skipped_private_count = 0
    total_blob_bytes = 0
    staged_set = set(staged)
    for scope, paths, loader in (
        ("tracked", [path for path in tracked if path not in staged_set], _blob_from_head),
        ("staged", staged, _blob_from_index),
        ("untracked", untracked, _blob_from_worktree),
    ):
        for path in paths:
            if _is_private_path(path):
                skipped_private_count += 1
                findings.append(
                    Finding(
                        scope,
                        "mydocs/<redacted>",
                        "private mydocs path is Git-visible; content and filename not read",
                    )
                )
                continue
            data = loader(root, path)
            total_blob_bytes += len(data)
            if total_blob_bytes > MAX_TOTAL_BLOB_BYTES:
                raise AuditError(
                    f"audited blob bytes exceed {MAX_TOTAL_BLOB_BYTES}"
                )
            findings.extend(_inspect(scope, path, data))

    return {
        "schema": "amipro-repository-safety-audit-v1",
        "candidate_counts": {
            "tracked": len(tracked),
            "staged": len(staged),
            "untracked_nonignored": len(untracked),
        },
        "private_path_count_not_read": skipped_private_count,
        "findings": [
            asdict(item)
            for item in sorted(findings, key=lambda item: (item.path, item.reason))
        ],
        "ok": not findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        report = audit(args.repo.resolve())
    except (AuditError, OSError, subprocess.SubprocessError) as error:
        print(f"safety audit failed: {error}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
