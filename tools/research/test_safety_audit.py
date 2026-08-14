from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from safety_audit import (  # noqa: E402
    SYNTHETIC_SAM_ALLOWLIST,
    AuditError,
    _allowlisted,
    audit,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repository() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory(prefix="amipro-safety-test-")
    root = Path(temporary.name)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Synthetic Test")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    (root / ".gitignore").write_text("mydocs\n", encoding="utf-8")
    (root / "README.md").write_text("invented test repository\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "README.md")
    _git(root, "commit", "-qm", "synthetic baseline")
    return temporary, root


def _invented_ne() -> bytes:
    data = bytearray(128)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x42] = b"NE"
    return bytes(data)


def test_synthetic_fixture_allowlist_is_digest_specific() -> None:
    path, digest = next(iter(SYNTHETIC_SAM_ALLOWLIST.items()))
    assert _allowlisted(path, digest)
    assert not _allowlisted(path, "0" * 64)


def test_audit_ignores_private_directory_without_opening_it() -> None:
    temporary, root = _repository()
    try:
        private = root / "mydocs"
        private.mkdir()
        private_file = private / "private.sam"
        private_file.write_bytes(b"private bytes must not be inspected")
        private_file.chmod(0)
        report = audit(root)
        assert report["ok"] is True
        assert report["private_path_count_not_read"] == 0
    finally:
        private_file.chmod(0o600)
        temporary.cleanup()


def test_audit_detects_staged_ne_blob_without_vendor_bytes() -> None:
    temporary, root = _repository()
    try:
        candidate = root / "invented.bin"
        candidate.write_bytes(_invented_ne())
        _git(root, "add", candidate.name)
        report = audit(root)
        assert report["ok"] is False
        findings = report["findings"]
        assert any(
            finding["path"] == candidate.name
            and "NE executable" in finding["reason"]
            for finding in findings
        )
    finally:
        temporary.cleanup()


def test_audit_refuses_untracked_symlink() -> None:
    temporary, root = _repository()
    try:
        os.symlink("README.md", root / "candidate-link")
        try:
            audit(root)
        except AuditError as error:
            assert "non-symlink" in str(error)
        else:
            raise AssertionError("audit accepted an untracked symlink")
    finally:
        temporary.cleanup()


def test_audit_flags_vendor_icon_asset_without_real_vendor_bytes() -> None:
    temporary, root = _repository()
    try:
        candidate = root / "AMIPRO.ICN"
        candidate.write_bytes(b"invented icon-like bytes\n")
        report = audit(root)
        assert report["ok"] is False
        assert any(
            finding["path"] == candidate.name
            and "asset extension .icn" in finding["reason"]
            for finding in report["findings"]
        )
    finally:
        temporary.cleanup()
