from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from inventory import InventoryError, inventory_payload  # noqa: E402
from ne import VerificationError  # noqa: E402
from test_ne import invented_ne  # noqa: E402


def test_inventory_is_deterministic_and_contains_no_input_path(tmp_path: Path) -> None:
    payload = tmp_path / "volatile-payload"
    payload.mkdir()
    primary = invented_ne()
    (payload / "AMIPRO.EXE").write_bytes(primary)
    (payload / "README.TXT").write_text("not an executable\n", encoding="ascii")
    (payload / "SUBDIR").mkdir()
    digest = hashlib.sha256(primary).hexdigest()

    kwargs = {
        "primary_size": len(primary),
        "primary_sha256": digest,
        "tool_probes": [],
    }
    first = inventory_payload(payload, **kwargs)
    second = inventory_payload(payload, **kwargs)

    assert first == second
    assert first["payload_summary"] == {
        "directory_entry_count": 3,
        "regular_file_count": 2,
        "skipped_directory_count": 1,
        "regular_file_bytes": len(primary) + len("not an executable\n"),
        "ne_module_count": 1,
    }
    assert first["modules"][0]["sha256"] == digest
    assert first["tools"]["python"]["implementation"]
    assert first["tools"]["probes"] == []
    assert str(payload) not in repr(first)


def test_inventory_requires_exact_primary_digest(tmp_path: Path) -> None:
    (tmp_path / "AMIPRO.EXE").write_bytes(invented_ne())
    with pytest.raises(VerificationError, match="SHA-256 mismatch"):
        inventory_payload(
            tmp_path,
            primary_size=len(invented_ne()),
            primary_sha256="0" * 64,
            tool_probes=[],
        )


def test_inventory_rejects_nonregular_payload_entry(tmp_path: Path) -> None:
    primary = invented_ne()
    (tmp_path / "AMIPRO.EXE").write_bytes(primary)
    (tmp_path / "unsafe-link").symlink_to("AMIPRO.EXE")
    with pytest.raises(InventoryError, match="non-symlink"):
        inventory_payload(
            tmp_path,
            primary_size=len(primary),
            primary_sha256=hashlib.sha256(primary).hexdigest(),
            tool_probes=[],
        )
