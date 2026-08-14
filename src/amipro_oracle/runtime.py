from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import DOSBOX_PROFILE, dosbox_config, dosbox_config_digest
from .constants import EXIT_INTEGRITY, RUNTIME_SCHEMA
from .errors import OracleError
from .io import atomic_write, atomic_write_json, digest_json, read_json_object
from .toolchain import load_lock


def fake_runtime_key() -> str:
    return digest_json(
        {
            "schema": RUNTIME_SCHEMA,
            "backend": "fake",
            "toolchain": load_lock(),
            "dosbox_config_sha256": dosbox_config_digest(),
        }
    )


def bootstrap_fake(home: Path) -> dict[str, Any]:
    key = fake_runtime_key()
    runtime = home / "cache" / "runtime" / key
    manifest_path = runtime / "runtime.json"
    expected: dict[str, Any] = {
        "schema": RUNTIME_SCHEMA,
        "backend": "fake",
        "baseline_eligible": False,
        "status": "ready",
        "runtime_key": key,
        "toolchain": load_lock(),
        "dosbox_profile": DOSBOX_PROFILE,
        "dosbox_config_sha256": dosbox_config_digest(),
        "diagnostics": ["fake runtime contains no proprietary media or guest"],
    }
    for directory in (home, home / "cache", home / "cache" / "runtime"):
        if directory.is_symlink():
            raise OracleError(
                f"fake runtime cache parents must not be symlinks: {directory}",
                exit_code=EXIT_INTEGRITY,
            )
    if runtime.is_symlink():
        raise OracleError(
            f"fake runtime cache must not be a symlink: {runtime}",
            exit_code=EXIT_INTEGRITY,
        )
    if runtime.exists():
        if not runtime.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
            raise OracleError(
                f"fake runtime manifest integrity failure: {manifest_path}",
                exit_code=EXIT_INTEGRITY,
            )
        try:
            existing = read_json_object(manifest_path)
        except (OSError, ValueError) as exc:
            raise OracleError(
                f"fake runtime manifest integrity failure: {manifest_path}",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        if existing != expected:
            raise OracleError(
                f"fake runtime cache integrity failure: {manifest_path}",
                exit_code=EXIT_INTEGRITY,
            )
        config_path = runtime / "dosbox-x.conf"
        try:
            config_matches = (
                not config_path.is_symlink()
                and config_path.is_file()
                and config_path.read_text(encoding="utf-8") == dosbox_config()
            )
        except (OSError, UnicodeError):
            config_matches = False
        if not config_matches:
            raise OracleError(
                f"fake runtime config integrity failure: {config_path}",
                exit_code=EXIT_INTEGRITY,
            )
        return existing
    try:
        runtime.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise OracleError(
            f"fake runtime cache appeared during bootstrap: {runtime}",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    atomic_write(runtime / "dosbox-x.conf", dosbox_config().encode("utf-8"))
    atomic_write_json(manifest_path, expected)
    return expected
