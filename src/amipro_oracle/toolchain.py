from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .io import sha256_file
from .paths import repo_root

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TOOLCHAIN_LOCK_LABEL = "org.amipro-oracle.toolchain-lock-sha256"


def lock_path() -> Path:
    return repo_root() / "toolchain" / "toolchain.lock.json"


def load_lock() -> dict[str, Any]:
    with lock_path().open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or value.get("schema") != "amipro-oracle-toolchain-v1":
        raise ValueError(f"invalid toolchain lock: {lock_path()}")
    return value


def _probe(
    executable: str,
    arguments: list[str],
    expected: str,
    expected_sha256: str | None,
) -> dict[str, object]:
    path = shutil.which(executable)
    result: dict[str, object] = {
        "name": executable,
        "path": path,
        "expected": expected,
        "status": "missing" if path is None else "unknown",
    }
    if path is None:
        return result
    try:
        process = subprocess.run(
            [path, *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3,
        )
        output = process.stdout.strip()
        actual_sha256 = sha256_file(Path(path))
        version_matches = process.returncode == 0 and expected in output
        hash_matches = expected_sha256 is not None and actual_sha256 == expected_sha256
        result.update(
            {
                "exit_code": process.returncode,
                "output": output[:2000],
                "sha256": actual_sha256,
                "expected_sha256": expected_sha256,
                "status": (
                    "match"
                    if version_matches and hash_matches
                    else "unverified"
                    if version_matches and expected_sha256 is None
                    else "mismatch"
                ),
            }
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.update({"status": "error", "error": str(exc)})
    return result


def probe_toolchain() -> dict[str, object]:
    lock = load_lock()
    probes = [
        _probe(
            str(item["executable"]),
            [str(argument) for argument in item["arguments"]],
            str(item["expected_output"]),
            str(item["expected_sha256"])
            if item.get("expected_sha256") is not None
            else None,
        )
        for item in lock["native_probes"]
    ]
    providers = [
        {"name": name, "path": shutil.which(name), "available": shutil.which(name) is not None}
        for name in ("podman", "docker")
    ]
    return {
        "lock": lock,
        "native": probes,
        "oci_providers": providers,
        "native_ready": all(item["status"] == "match" for item in probes),
    }


def probe_recorded_image(record: dict[str, Any]) -> dict[str, object]:
    provider = record.get("provider")
    image = record.get("image")
    expected_id = record.get("image_id")
    expected_digest = record.get("image_digest")
    recorded_lock_sha256 = record.get("lock_sha256")
    identity_is_valid = provider == "podman" and all(
        isinstance(value, str) and value for value in (image, expected_id, expected_digest)
    )
    lock_is_valid = (
        isinstance(recorded_lock_sha256, str)
        and _SHA256.fullmatch(recorded_lock_sha256) is not None
    )
    if not identity_is_valid or not lock_is_valid:
        return {"status": "invalid", "error": "invalid recorded OCI provider or identity"}
    current_lock_sha256 = sha256_file(lock_path())
    if recorded_lock_sha256 != current_lock_sha256:
        return {
            "status": "mismatch",
            "provider": provider,
            "image": image,
            "error": "recorded OCI image uses a different toolchain lock",
            "recorded_lock_sha256": recorded_lock_sha256,
            "current_lock_sha256": current_lock_sha256,
        }
    executable = shutil.which(provider)
    if executable is None:
        return {"status": "missing", "provider": provider, "image": image}
    try:
        info = subprocess.run(
            [executable, "info", "--format", "{{.Host.Security.Rootless}}"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
        if info.returncode != 0 or info.stdout.strip() != "true":
            return {
                "status": "mismatch",
                "provider": provider,
                "image": image,
                "error": "Podman provider is unavailable or is not rootless",
                "output": info.stdout[:2000],
            }
        process = subprocess.run(
            [
                executable,
                "image",
                "inspect",
                "--format",
                (
                    "{{.Id}}\t{{.Digest}}\t"
                    f'{{{{index .Config.Labels "{_TOOLCHAIN_LOCK_LABEL}"}}}}'
                ),
                image,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "provider": provider, "image": image, "error": str(exc)}
    output = process.stdout.strip()
    fields = output.split("\t")
    actual_id, actual_digest, image_lock_sha256 = (
        fields if len(fields) == 3 else ("", "", "")
    )
    matches = (
        process.returncode == 0
        and len(fields) == 3
        and actual_id == expected_id
        and actual_digest == expected_digest
        and image_lock_sha256 == recorded_lock_sha256
    )
    return {
        "status": "match" if matches else "mismatch",
        "provider": provider,
        "image": image,
        "exit_code": process.returncode,
        "expected_id": expected_id,
        "actual_id": actual_id,
        "expected_digest": expected_digest,
        "actual_digest": actual_digest,
        "recorded_lock_sha256": recorded_lock_sha256,
        "current_lock_sha256": current_lock_sha256,
        "image_lock_sha256": image_lock_sha256,
        "output": output[:2000],
    }
