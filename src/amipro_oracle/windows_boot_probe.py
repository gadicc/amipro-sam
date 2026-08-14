from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from . import oci as oci_module
from . import process as process_module
from . import windows_bootstrap as bootstrap_module
from .config import DOSBOX_PROFILE, dosbox_config
from .constants import EXIT_BACKEND, EXIT_INTEGRITY, EXIT_MISSING, EXIT_USAGE, RUNTIME_SCHEMA
from .errors import OracleError
from .io import atomic_write, atomic_write_json, digest_json, read_json_object, sha256_file
from .oci import (
    BindMount,
    PodmanInvocation,
    build_podman_invocation,
    exec_podman_checked,
    run_podman_bounded,
)
from .raster import decode_png
from .state import StateMachine
from .windows_bootstrap import (
    GUEST_DATE,
    GUEST_TIME,
    NORMALIZED_RUNTIME_MTIME_NS,
    WINDOWS_FREE_MB,
    _cache_lock,
    _directory_fsync,
    _ensure_private_directories,
    _inventory_windows_runtime,
    _make_tree_read_only,
    _normalize_runtime_metadata,
    _require_verified_image,
    _tree_fsync,
    _validate_observer_evidence,
    _validate_windows_tree,
    verify_windows_install_candidate,
)

WINDOWS_READY_SCHEMA = "amipro-oracle-windows-ready-v1"
BOOT_INPUT_SCHEMA = "amipro-oracle-windows-boot-input-v1"
BOOT_RESULT_SCHEMA = "amipro-oracle-windows-boot-result-v1"
UI_DRIVER_SCHEMA = "amipro-oracle-program-manager-driver-v1"
INNER_TIME_LIMIT_SECONDS = 60
OUTER_TIME_LIMIT_SECONDS = 90
UI_DRIVER_TIMEOUT_SECONDS = 40
SCREEN_WIDTH = 1024
SCREEN_HEIGHT = 768
STABLE_SCREEN_SAMPLES = 3
SCREEN_POLL_SECONDS = 0.5
MINIMUM_LAUNCH_SECONDS = 6
READY_MINIMUM_DISTINCT_COLORS = 12
READY_MINIMUM_VGA_BLUE_PIXELS = 10_000
CONFIRMATION_MINIMUM_VGA_BLUE_PIXELS = 5_000
MINIMUM_LIGHT_PIXELS = 100_000
MINIMUM_GRAY_PIXELS = 100_000

UI_PROFILE = {
    "name": "program-manager-stable-screen-alt-f4-enter-v1",
    "screen_width": SCREEN_WIDTH,
    "screen_height": SCREEN_HEIGHT,
    "stable_samples": STABLE_SCREEN_SAMPLES,
    "poll_seconds": SCREEN_POLL_SECONDS,
    "minimum_launch_seconds": MINIMUM_LAUNCH_SECONDS,
    "ready_minimum_distinct_colors": READY_MINIMUM_DISTINCT_COLORS,
    "ready_minimum_vga_blue_pixels": READY_MINIMUM_VGA_BLUE_PIXELS,
    "confirmation_minimum_vga_blue_pixels": CONFIRMATION_MINIMUM_VGA_BLUE_PIXELS,
    "minimum_light_pixels": MINIMUM_LIGHT_PIXELS,
    "minimum_gray_pixels": MINIMUM_GRAY_PIXELS,
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def windows_boot_config() -> str:
    return dosbox_config(
        runtime_free_mb=WINDOWS_FREE_MB,
        autoexec=(
            "COUNTRY 1",
            f"DATE {GUEST_DATE}",
            f"TIME {GUEST_TIME}",
            r"Z:\CONFIG.COM -SECUREMODE",
            r"C:\WINBOOT.BAT",
        ),
    )


def windows_boot_batch() -> bytes:
    lines = (
        "@ECHO OFF",
        r"IF EXIST C:\BOOT.START DEL C:\BOOT.START",
        r"IF EXIST C:\BOOT.OK DEL C:\BOOT.OK",
        r"IF EXIST C:\BOOT.ERR DEL C:\BOOT.ERR",
        r"IF NOT EXIST C:\WINDOWS\WIN.COM GOTO BOOT_MISSING",
        r"ECHO WINDOWS_LAUNCH_REQUESTED>C:\BOOT.START",
        r"C:\WINDOWS\WIN.COM",
        "IF ERRORLEVEL 1 GOTO BOOT_FAILED",
        r"ECHO WINDOWS_RETURNED_ZERO>C:\BOOT.OK",
        "GOTO BOOT_DONE",
        ":BOOT_MISSING",
        r"ECHO WINDOWS_WIN_COM_MISSING>C:\BOOT.ERR",
        "GOTO BOOT_DONE",
        ":BOOT_FAILED",
        r"ECHO WINDOWS_ERRORLEVEL_NONZERO>C:\BOOT.ERR",
        ":BOOT_DONE",
        "EXIT",
    )
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def _source_fingerprints() -> dict[str, str]:
    paths = {
        "boot_probe": Path(__file__),
        "oci": Path(oci_module.__file__),
        "process": Path(process_module.__file__),
        "windows_bootstrap": Path(bootstrap_module.__file__),
    }
    return {name: sha256_file(path) for name, path in sorted(paths.items())}


def windows_boot_inputs(
    candidate: dict[str, Any],
    image_record: dict[str, Any],
    *,
    outer_time_limit_seconds: float = OUTER_TIME_LIMIT_SECONDS,
) -> dict[str, object]:
    image_id = image_record.get("image_id")
    image_digest = image_record.get("image_digest")
    lock_hash = image_record.get("lock_sha256")
    if (
        not isinstance(image_id, str)
        or _SHA256.fullmatch(image_id) is None
        or not isinstance(image_digest, str)
        or _IMAGE_DIGEST.fullmatch(image_digest) is None
        or not isinstance(lock_hash, str)
        or _SHA256.fullmatch(lock_hash) is None
        or image_record.get("platform") != "linux/amd64"
    ):
        raise OracleError("invalid verified OCI image identity", exit_code=EXIT_INTEGRITY)
    if (
        candidate.get("schema") != bootstrap_module.WINDOWS_CHECKPOINT_SCHEMA
        or candidate.get("status") != "windows-install-candidate"
        or not isinstance(candidate.get("checkpoint_key"), str)
        or _SHA256.fullmatch(str(candidate["checkpoint_key"])) is None
    ):
        raise OracleError("invalid Windows install candidate", exit_code=EXIT_INTEGRITY)
    if (
        isinstance(outer_time_limit_seconds, bool)
        or not isinstance(outer_time_limit_seconds, (int, float))
        or not 1 <= outer_time_limit_seconds <= OUTER_TIME_LIMIT_SECONDS
    ):
        raise OracleError(
            f"boot-probe timeout must be between 1 and {OUTER_TIME_LIMIT_SECONDS} seconds",
            exit_code=EXIT_USAGE,
        )
    config = windows_boot_config().encode("utf-8")
    batch = windows_boot_batch()
    return {
        "schema": BOOT_INPUT_SCHEMA,
        "install_candidate": {
            "checkpoint_key": candidate["checkpoint_key"],
            "manifest_digest": digest_json(candidate),
            "guest_tree_digest": candidate["guest_tree_digest"],
            "sealed_tree_digest": candidate["sealed_tree_digest"],
        },
        "toolchain": {
            "image_id": image_id,
            "image_digest": image_digest,
            "lock_sha256": lock_hash,
            "platform": "linux/amd64",
        },
        "driver_profile": UI_PROFILE,
        "dosbox_profile": DOSBOX_PROFILE,
        "dosbox_config_sha256": hashlib.sha256(config).hexdigest(),
        "boot_batch_sha256": hashlib.sha256(batch).hexdigest(),
        "orchestrator_sha256": _source_fingerprints(),
        "guest_clock": {"date_command": GUEST_DATE, "time_command": GUEST_TIME},
        "reported_free_mb": WINDOWS_FREE_MB,
        "runtime_metadata_policy": {
            "mtime_ns": NORMALIZED_RUNTIME_MTIME_NS,
            "file_mode": "0644",
            "directory_mode": "0755",
            "timezone": "UTC",
        },
        "inner_time_limit_seconds": INNER_TIME_LIMIT_SECONDS,
        "outer_time_limit_seconds": outer_time_limit_seconds,
    }


def _select_install_candidate(
    home: Path,
    checkpoint_key: str | None,
) -> tuple[Path, dict[str, Any], dict[str, Any], str]:
    parent = home / "cache" / "windows"
    if parent.is_symlink() or not parent.is_dir():
        raise OracleError("Windows install-candidate cache is missing", exit_code=EXIT_MISSING)
    if checkpoint_key is None:
        candidates: list[str] = []
        for path in sorted(parent.iterdir(), key=lambda item: item.name):
            if _SHA256.fullmatch(path.name) is None:
                continue
            if path.is_symlink() or not path.is_dir():
                raise OracleError("unsafe Windows checkpoint entry", exit_code=EXIT_INTEGRITY)
            manifest_path = path / "runtime.json"
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise OracleError(
                    "Windows checkpoint manifest is missing",
                    exit_code=EXIT_INTEGRITY,
                )
            try:
                manifest = read_json_object(manifest_path)
            except (OSError, ValueError) as exc:
                raise OracleError(
                    "Windows checkpoint manifest is invalid",
                    exit_code=EXIT_INTEGRITY,
                ) from exc
            if manifest.get("status") == "windows-install-candidate":
                candidates.append(path.name)
        if not candidates:
            raise OracleError(
                "run bootstrap to create a Windows install candidate first",
                exit_code=EXIT_MISSING,
            )
        if len(candidates) != 1:
            raise OracleError(
                "multiple Windows candidates exist; pass --checkpoint-key",
                exit_code=EXIT_USAGE,
            )
        checkpoint_key = candidates[0]
    if _SHA256.fullmatch(checkpoint_key) is None:
        raise OracleError("invalid --checkpoint-key", exit_code=EXIT_USAGE)
    return verify_windows_install_candidate(home, checkpoint_key)


def _screen_metrics(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        if path.is_symlink() or not path.is_file():
            raise OracleError("screen evidence is missing or unsafe", exit_code=EXIT_BACKEND)
        before = path.stat()
        if before.st_size > 16 * 1024 * 1024:
            raise OracleError("screen evidence is missing or unsafe", exit_code=EXIT_BACKEND)
        payload = path.read_bytes()
        width, height, pixels = decode_png(path)
        after = path.stat()
    except (OSError, ValueError) as exc:
        raise OracleError("screen evidence is not a valid PNG", exit_code=EXIT_BACKEND) from exc
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise OracleError("screen evidence changed while reading", exit_code=EXIT_BACKEND)
    colors = Counter(
        tuple(pixels[offset : offset + 3])
        for offset in range(0, len(pixels), 4)
    )
    return (
        {
            "width": width,
            "height": height,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "distinct_colors": len(colors),
            "black_pixels": colors[(0, 0, 0)],
            "white_pixels": colors[(255, 255, 255)],
            "gray_pixels": colors[(195, 199, 203)],
            "vga_blue_pixels": colors[(0, 0, 170)],
        },
        payload,
    )


def _is_program_manager_ready(metrics: dict[str, object]) -> bool:
    return (
        metrics.get("width") == SCREEN_WIDTH
        and metrics.get("height") == SCREEN_HEIGHT
        and int(metrics.get("distinct_colors", 0))
        >= READY_MINIMUM_DISTINCT_COLORS
        and int(metrics.get("vga_blue_pixels", 0))
        >= READY_MINIMUM_VGA_BLUE_PIXELS
        and int(metrics.get("white_pixels", 0)) >= MINIMUM_LIGHT_PIXELS
        and int(metrics.get("gray_pixels", 0)) >= MINIMUM_GRAY_PIXELS
    )


def _is_exit_confirmation(
    metrics: dict[str, object],
    ready_sha256: str,
) -> bool:
    return (
        metrics.get("width") == SCREEN_WIDTH
        and metrics.get("height") == SCREEN_HEIGHT
        and metrics.get("sha256") != ready_sha256
        and int(metrics.get("distinct_colors", 0))
        >= READY_MINIMUM_DISTINCT_COLORS
        and int(metrics.get("vga_blue_pixels", 0))
        >= CONFIRMATION_MINIMUM_VGA_BLUE_PIXELS
        and int(metrics.get("white_pixels", 0)) >= MINIMUM_LIGHT_PIXELS
        and int(metrics.get("gray_pixels", 0)) >= MINIMUM_GRAY_PIXELS
    )


def _wait_for_screen(
    path: Path,
    *,
    stop: threading.Event,
    deadline: float,
    predicate: Callable[[dict[str, object]], bool],
    minimum_time: float = 0,
) -> tuple[dict[str, object], bytes]:
    previous_sha256: str | None = None
    stable = 0
    while monotonic() < deadline and not stop.is_set():
        if monotonic() < minimum_time:
            sleep(SCREEN_POLL_SECONDS)
            continue
        try:
            metrics, payload = _screen_metrics(path)
        except OracleError:
            sleep(SCREEN_POLL_SECONDS)
            continue
        if predicate(metrics):
            current_sha256 = str(metrics["sha256"])
            stable = stable + 1 if current_sha256 == previous_sha256 else 1
            previous_sha256 = current_sha256
            if stable >= STABLE_SCREEN_SAMPLES:
                return metrics, payload
        else:
            previous_sha256 = None
            stable = 0
        sleep(SCREEN_POLL_SECONDS)
    raise OracleError("guest UI did not reach the required stable state", exit_code=EXIT_BACKEND)


def _drive_program_manager_exit(
    invocation: PodmanInvocation,
    job: Path,
    stop: threading.Event,
) -> dict[str, object]:
    started = monotonic()
    deadline = started + UI_DRIVER_TIMEOUT_SECONDS
    launch = job / "runtime" / "BOOT.START"
    while monotonic() < deadline and not stop.is_set():
        if launch.is_file() and launch.read_bytes() == b"WINDOWS_LAUNCH_REQUESTED\r\n":
            break
        sleep(0.1)
    else:
        raise OracleError("Windows launch sentinel was not observed", exit_code=EXIT_BACKEND)
    ready_metrics, ready_payload = _wait_for_screen(
        job / "diagnostics" / "screen-last.png",
        stop=stop,
        deadline=deadline,
        predicate=_is_program_manager_ready,
        minimum_time=started + MINIMUM_LAUNCH_SECONDS,
    )
    ready_path = job / "diagnostics" / "program-manager-ready.png"
    atomic_write(ready_path, ready_payload)
    search = exec_podman_checked(
        invocation,
        ("xdotool", "search", "--onlyvisible", "--name", "DOSBox-X"),
        environment={"DISPLAY": ":99"},
    )
    windows = [line for line in str(search["stdout"]).splitlines() if line.isdigit()]
    if search["exit_code"] != 0 or len(windows) != 1:
        raise OracleError("cannot identify the DOSBox-X UI window", exit_code=EXIT_BACKEND)
    window = windows[0]
    exit_key = exec_podman_checked(
        invocation,
        ("xdotool", "key", "--window", window, "alt+F4"),
        environment={"DISPLAY": ":99"},
    )
    if exit_key["exit_code"] != 0:
        raise OracleError("cannot request the Program Manager exit", exit_code=EXIT_BACKEND)
    confirmation_metrics, confirmation_payload = _wait_for_screen(
        job / "diagnostics" / "screen-last.png",
        stop=stop,
        deadline=deadline,
        predicate=lambda metrics: _is_exit_confirmation(
            metrics,
            str(ready_metrics["sha256"]),
        ),
    )
    confirmation_path = job / "diagnostics" / "exit-windows-confirmation.png"
    atomic_write(confirmation_path, confirmation_payload)
    confirm_key = exec_podman_checked(
        invocation,
        ("xdotool", "key", "--window", window, "Return"),
        environment={"DISPLAY": ":99"},
    )
    if confirm_key["exit_code"] != 0:
        raise OracleError("cannot confirm the Windows exit", exit_code=EXIT_BACKEND)
    return {
        "schema": UI_DRIVER_SCHEMA,
        "status": "success",
        "profile": UI_PROFILE,
        "ready": {"path": ready_path.name, **ready_metrics},
        "confirmation": {
            "path": confirmation_path.name,
            **confirmation_metrics,
        },
        "actions": [
            {"action": "alt-f4", "exit_code": exit_key["exit_code"]},
            {"action": "enter", "exit_code": confirm_key["exit_code"]},
        ],
        "elapsed_seconds": round(monotonic() - started, 6),
    }


def _invoke_boot_job(
    invocation: PodmanInvocation,
    job: Path,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, object], dict[str, object]]:
    stop = threading.Event()
    driver_box: dict[str, object] = {}

    def worker() -> None:
        try:
            driver_box["result"] = _drive_program_manager_exit(invocation, job, stop)
        except BaseException as exc:
            driver_box["result"] = {
                "schema": UI_DRIVER_SCHEMA,
                "status": "failure",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    driver = threading.Thread(target=worker, name="amipro-program-manager-driver", daemon=True)
    driver.start()
    process: dict[str, object] | None = None
    process_error: BaseException | None = None
    try:
        process = run_podman_bounded(
            invocation,
            stdout_path=job / "diagnostics" / "container.stdout.log",
            stderr_path=job / "diagnostics" / "container.stderr.log",
            cleanup_path=job / "diagnostics" / "container-cleanup.json",
            timeout_seconds=timeout_seconds,
        )
    except BaseException as exc:
        process_error = exc
        attached = getattr(exc, "process_result", None)
        if isinstance(attached, dict):
            process = attached
    finally:
        stop.set()
        driver.join(timeout=7)
    if driver.is_alive():
        driver_result: dict[str, object] = {
            "schema": UI_DRIVER_SCHEMA,
            "status": "failure",
            "error_type": "DriverThreadError",
            "error": "UI driver thread did not stop",
        }
    else:
        value = driver_box.get("result")
        driver_result = value if isinstance(value, dict) else {
            "schema": UI_DRIVER_SCHEMA,
            "status": "failure",
            "error_type": "DriverThreadError",
            "error": "UI driver did not return evidence",
        }
    atomic_write_json(job / "ui-driver.json", driver_result)
    if process_error is not None:
        if isinstance(process_error, OracleError):
            process_error.ui_driver = driver_result
        raise process_error
    if process is None:
        raise OracleError("Windows boot process did not return", exit_code=EXIT_BACKEND)
    if driver_result.get("status") != "success":
        error = OracleError(
            f"Program Manager UI driver failed: {driver_result.get('error', 'unknown error')}",
            exit_code=EXIT_BACKEND,
        )
        error.process_result = process
        raise error
    return process, driver_result


def _validate_ui_evidence(job: Path) -> dict[str, object]:
    path = job / "ui-driver.json"
    if path.is_symlink() or not path.is_file():
        raise OracleError("Program Manager UI evidence is missing", exit_code=EXIT_INTEGRITY)
    try:
        driver = read_json_object(path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Program Manager UI evidence is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    if (
        driver.get("schema") != UI_DRIVER_SCHEMA
        or driver.get("status") != "success"
        or driver.get("profile") != UI_PROFILE
        or driver.get("actions")
        != [
            {"action": "alt-f4", "exit_code": 0},
            {"action": "enter", "exit_code": 0},
        ]
        or not isinstance(driver.get("ready"), dict)
        or not isinstance(driver.get("confirmation"), dict)
    ):
        raise OracleError("Program Manager UI evidence identity mismatch", exit_code=EXIT_INTEGRITY)
    ready_path = job / "diagnostics" / "program-manager-ready.png"
    confirmation_path = job / "diagnostics" / "exit-windows-confirmation.png"
    try:
        ready, _ = _screen_metrics(ready_path)
        confirmation, _ = _screen_metrics(confirmation_path)
    except OracleError as exc:
        raise OracleError(
            "Program Manager screenshots are invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    if (
        driver["ready"] != {"path": ready_path.name, **ready}
        or driver["confirmation"] != {"path": confirmation_path.name, **confirmation}
        or not _is_program_manager_ready(ready)
        or not _is_exit_confirmation(confirmation, str(ready["sha256"]))
    ):
        raise OracleError(
            "Program Manager screenshots do not match UI evidence",
            exit_code=EXIT_INTEGRITY,
        )
    return driver


def _validate_boot_return(runtime: Path) -> None:
    expected = {
        "BOOT.START": b"WINDOWS_LAUNCH_REQUESTED\r\n",
        "BOOT.OK": b"WINDOWS_RETURNED_ZERO\r\n",
    }
    for name, payload in expected.items():
        path = runtime / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise OracleError(f"Windows boot sentinel is invalid: {name}", exit_code=EXIT_BACKEND)
    if (runtime / "BOOT.ERR").exists() or (runtime / "BOOT.ERR").is_symlink():
        raise OracleError("Windows boot reported a nonzero error", exit_code=EXIT_BACKEND)
    system_ini = (runtime / "WINDOWS" / "SYSTEM.INI").read_text(
        encoding="latin-1",
        errors="strict",
    )
    normalized = "".join(system_ini.casefold().split())
    if "shell=progman.exe" not in normalized:
        raise OracleError(
            "Windows SYSTEM.INI does not select Program Manager",
            exit_code=EXIT_BACKEND,
        )


def _remove_boot_controls(runtime: Path) -> None:
    for name in ("BOOT.START", "BOOT.OK", "BOOT.ERR", "WINBOOT.BAT"):
        path = runtime / name
        if path.is_symlink():
            raise OracleError("boot control path became a symlink", exit_code=EXIT_INTEGRITY)
        path.unlink(missing_ok=True)


def _write_attempt(job: Path, inputs: dict[str, object], machine: StateMachine) -> None:
    atomic_write_json(
        job / "attempt.json",
        {
            "schema": "amipro-oracle-windows-boot-attempt-v1",
            "phase": "program-manager-boot",
            "inputs_digest": digest_json(inputs),
            "state": machine.state,
            "state_trace": machine.trace,
        },
    )


def _ready_result(
    runtime: dict[str, Any],
    *,
    cache_reused: bool,
    evidence_job: str,
) -> dict[str, Any]:
    return {
        "schema": BOOT_RESULT_SCHEMA,
        "status": runtime["status"],
        "runtime_key": runtime["runtime_key"],
        "parent_checkpoint_key": runtime["parent_checkpoint_key"],
        "cache_reused": cache_reused,
        "evidence_job": evidence_job,
        "runtime": runtime,
    }


def _load_ready_evidence(
    home: Path,
    root: Path,
    key: str,
    runtime: dict[str, Any],
) -> str:
    receipt_path = root / "evidence-receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise OracleError("Windows-ready evidence receipt is missing", exit_code=EXIT_INTEGRITY)
    try:
        receipt = read_json_object(receipt_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Windows-ready evidence receipt is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    job_name = receipt.get("evidence_job")
    result_sha256 = receipt.get("result_sha256")
    if (
        set(receipt) != {"schema", "runtime_key", "evidence_job", "result_sha256"}
        or receipt.get("schema") != "amipro-oracle-windows-ready-evidence-v1"
        or receipt.get("runtime_key") != key
        or not isinstance(job_name, str)
        or re.fullmatch(r"boot-windows-[a-z0-9_-]+", job_name) is None
        or not isinstance(result_sha256, str)
        or _SHA256.fullmatch(result_sha256) is None
    ):
        raise OracleError("Windows-ready evidence receipt mismatch", exit_code=EXIT_INTEGRITY)
    job = home / "jobs" / job_name
    result_path = job / "result.json"
    if (
        job.is_symlink()
        or not job.is_dir()
        or result_path.is_symlink()
        or not result_path.is_file()
        or sha256_file(result_path) != result_sha256
    ):
        raise OracleError("Windows-ready evidence result mismatch", exit_code=EXIT_INTEGRITY)
    try:
        result = read_json_object(result_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Windows-ready evidence result is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    try:
        observer = _validate_observer_evidence(job / "diagnostics")
        driver = _validate_ui_evidence(job)
    except OracleError as exc:
        raise OracleError(
            "Windows-ready visual evidence is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    process = result.get("process_result")
    trace = result.get("state_trace")
    expected_result_keys = {
        "cache_reused",
        "evidence_job",
        "evidence_stage",
        "observer",
        "parent_checkpoint_key",
        "process_result",
        "runtime",
        "runtime_key",
        "schema",
        "state_trace",
        "status",
        "ui_driver",
    }
    if (
        set(result) != expected_result_keys
        or result.get("schema") != BOOT_RESULT_SCHEMA
        or result.get("status") != "windows-ready"
        or result.get("runtime_key") != key
        or result.get("cache_reused") is not False
        or result.get("evidence_job") != job_name
        or result.get("runtime") != runtime
        or result.get("evidence_stage") != "validated-before-atomic-promotion"
        or result.get("observer") != observer
        or result.get("ui_driver") != driver
        or not isinstance(process, dict)
        or process.get("exit_code") != 0
        or process.get("timed_out") is not False
        or process.get("killed") is not False
        or not isinstance(trace, list)
        or not all(isinstance(event, dict) for event in trace)
        or [event.get("state") for event in trace]
        != ["created", "staged", "guest-invoked", "guest-returned", "validated"]
    ):
        raise OracleError("Windows-ready evidence identity mismatch", exit_code=EXIT_INTEGRITY)
    return job_name


def _unsealed_tree_from_read_only(sealed: dict[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, object]] = []
    for raw in sealed.get("entries", []):
        if not isinstance(raw, dict) or raw.get("type") not in {"directory", "file"}:
            raise OracleError("Windows-ready sealed tree is invalid", exit_code=EXIT_INTEGRITY)
        entry = dict(raw)
        entry["mode"] = "0755" if raw["type"] == "directory" else "0644"
        entries.append(entry)
    return {
        "schema": bootstrap_module.RUNTIME_TREE_SCHEMA,
        "entries": entries,
        "file_count": sealed.get("file_count"),
        "directory_count": sealed.get("directory_count"),
        "total_bytes": sealed.get("total_bytes"),
        "digest": digest_json(
            {
                "schema": bootstrap_module.RUNTIME_TREE_SCHEMA,
                "entries": entries,
            }
        ),
    }


def _verify_ready_cache(
    home: Path,
    root: Path,
    key: str,
    inputs: dict[str, object],
) -> tuple[dict[str, Any], str]:
    paths = {
        "runtime": root / "runtime.json",
        "inputs": root / "inputs.json",
        "boot_tree": root / "boot-tree.json",
        "sealed_tree": root / "sealed-tree.json",
        "config": root / "dosbox-x.conf",
        "batch": root / "WINBOOT.BAT",
        "receipt": root / "evidence-receipt.json",
    }
    pristine = root / "pristine-c"
    expected_names = {
        "WINBOOT.BAT",
        "boot-tree.json",
        "dosbox-x.conf",
        "evidence-receipt.json",
        "inputs.json",
        "pristine-c",
        "runtime.json",
        "sealed-tree.json",
    }
    if (
        root.is_symlink()
        or not root.is_dir()
        or {path.name for path in root.iterdir()} != expected_names
        or any(path.is_symlink() or not path.is_file() for path in paths.values())
        or pristine.is_symlink()
        or not pristine.is_dir()
        or stat.S_IMODE(root.stat().st_mode) & 0o222
        or stat.S_IMODE(pristine.stat().st_mode) & 0o222
        or any(stat.S_IMODE(path.stat().st_mode) & 0o222 for path in paths.values())
    ):
        raise OracleError("Windows-ready cache has an unsafe shape", exit_code=EXIT_INTEGRITY)
    try:
        runtime = read_json_object(paths["runtime"])
        recorded_inputs = read_json_object(paths["inputs"])
        boot_tree = read_json_object(paths["boot_tree"])
        recorded_sealed = read_json_object(paths["sealed_tree"])
        config_bytes = paths["config"].read_bytes()
        batch_bytes = paths["batch"].read_bytes()
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Windows-ready cache manifest is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    expected_keys = {
        "backend",
        "baseline_eligible",
        "boot_tree_digest",
        "boot_tree_manifest_digest",
        "checkpoint_role",
        "inputs_digest",
        "parent_checkpoint_key",
        "printer_profile",
        "runtime_key",
        "runtime_schema",
        "schema",
        "sealed_tree_digest",
        "sealed_tree_manifest_digest",
        "status",
        "tree_directory_count",
        "tree_file_count",
        "tree_total_bytes",
    }
    sealed = _inventory_windows_runtime(pristine)
    expected_boot_tree = _unsealed_tree_from_read_only(sealed)
    if (
        set(runtime) != expected_keys
        or runtime.get("schema") != WINDOWS_READY_SCHEMA
        or runtime.get("runtime_schema") != RUNTIME_SCHEMA
        or runtime.get("backend") != "real"
        or runtime.get("baseline_eligible") is not False
        or runtime.get("status") != "windows-ready"
        or runtime.get("checkpoint_role") != "base-for-ami-pro-installation"
        or runtime.get("runtime_key") != key
        or runtime.get("inputs_digest") != digest_json(inputs)
        or key != digest_json(inputs)
        or recorded_inputs != inputs
        or config_bytes != windows_boot_config().encode("utf-8")
        or batch_bytes != windows_boot_batch()
        or hashlib.sha256(config_bytes).hexdigest() != inputs.get("dosbox_config_sha256")
        or hashlib.sha256(batch_bytes).hexdigest() != inputs.get("boot_batch_sha256")
        or boot_tree != expected_boot_tree
        or boot_tree.get("digest") != runtime.get("boot_tree_digest")
        or digest_json(boot_tree) != runtime.get("boot_tree_manifest_digest")
        or recorded_sealed != sealed
        or sealed.get("digest") != runtime.get("sealed_tree_digest")
        or digest_json(sealed) != runtime.get("sealed_tree_manifest_digest")
        or sealed.get("file_count") != runtime.get("tree_file_count")
        or sealed.get("directory_count") != runtime.get("tree_directory_count")
        or sealed.get("total_bytes") != runtime.get("tree_total_bytes")
        or any(int(str(entry["mode"]), 8) & 0o222 for entry in sealed["entries"])
    ):
        raise OracleError("Windows-ready cache identity mismatch", exit_code=EXIT_INTEGRITY)
    _validate_windows_tree(pristine)
    evidence_job = _load_ready_evidence(home, root, key, runtime)
    return runtime, evidence_job


def boot_windows_ready(
    home: Path,
    image_record: dict[str, Any],
    *,
    checkpoint_key: str | None = None,
    timeout_seconds: float = OUTER_TIME_LIMIT_SECONDS,
) -> dict[str, Any]:
    _require_verified_image(image_record)
    _ensure_private_directories(home)
    ready_parent = home / "cache" / "windows-ready"
    if ready_parent.is_symlink():
        raise OracleError("Windows-ready cache parent is unsafe", exit_code=EXIT_INTEGRITY)
    if not ready_parent.exists():
        ready_parent.mkdir(mode=0o700)
    elif not ready_parent.is_dir():
        raise OracleError("Windows-ready cache parent is not a directory", exit_code=EXIT_INTEGRITY)
    ready_parent.chmod(0o700)
    source_root, candidate, _candidate_inputs, _candidate_evidence = _select_install_candidate(
        home,
        checkpoint_key,
    )
    inputs = windows_boot_inputs(
        candidate,
        image_record,
        outer_time_limit_seconds=timeout_seconds,
    )
    key = digest_json(inputs)
    final = ready_parent / key
    parent_key = str(candidate["checkpoint_key"])
    with _cache_lock(home, parent_key), _cache_lock(home, key):
        source_root, candidate, _candidate_inputs, _candidate_evidence = (
            verify_windows_install_candidate(home, parent_key)
        )
        checked_inputs = windows_boot_inputs(
            candidate,
            image_record,
            outer_time_limit_seconds=timeout_seconds,
        )
        if checked_inputs != inputs:
            raise OracleError("Windows candidate changed after keying", exit_code=EXIT_INTEGRITY)
        if final.exists() or final.is_symlink():
            runtime, evidence_job = _verify_ready_cache(home, final, key, inputs)
            return _ready_result(runtime, cache_reused=True, evidence_job=evidence_job)

        job = Path(
            tempfile.mkdtemp(prefix=f"boot-windows-{key[:12]}-", dir=home / "jobs")
        )
        job.chmod(0o700)
        _directory_fsync(home / "jobs")
        for relative in ("capture", "diagnostics", "home"):
            (job / relative).mkdir(mode=0o700)
        runtime_root = job / "runtime"
        shutil.copytree(
            source_root / "pristine-c",
            runtime_root,
            copy_function=shutil.copy2,
        )
        _normalize_runtime_metadata(runtime_root)
        copied = _inventory_windows_runtime(runtime_root)
        if copied["digest"] != candidate["guest_tree_digest"]:
            raise OracleError(
                "disposable Windows copy does not match candidate",
                exit_code=EXIT_INTEGRITY,
            )
        config_bytes = windows_boot_config().encode("utf-8")
        batch_bytes = windows_boot_batch()
        atomic_write(runtime_root / "WINBOOT.BAT", batch_bytes)
        atomic_write(job / "dosbox-x.conf", config_bytes)
        atomic_write_json(job / "inputs.json", inputs)
        machine = StateMachine(
            initial="created",
            terminal=frozenset({"ready", "failed"}),
            transitions={
                "created": frozenset({"staged", "failed"}),
                "staged": frozenset({"guest-invoked", "failed"}),
                "guest-invoked": frozenset({"guest-returned", "failed"}),
                "guest-returned": frozenset({"validated", "failed"}),
                "validated": frozenset({"ready", "failed"}),
            },
        )
        machine.advance("staged", evidence="inputs.json")
        _write_attempt(job, inputs, machine)
        control = home / "control" / job.name
        control.mkdir(mode=0o700)
        suffix = re.sub(r"[^a-z0-9]", "", job.name[-10:].casefold())
        invocation = build_podman_invocation(
            image_record,
            container_name=f"amipro-oracle-boot-{suffix}",
            oracle_root=home,
            job_root=job,
            control_root=control,
            phase="document",
            mounts=[BindMount(job, "/oracle/job", read_only=False)],
            dosbox_arguments=[
                "-defaultconf",
                "-conf",
                "/oracle/job/dosbox-x.conf",
                "-fastlaunch",
                "-exit",
                "-time-limit",
                str(INNER_TIME_LIMIT_SECONDS),
            ],
        )
        machine.advance("guest-invoked", evidence="dosbox-x.conf")
        _write_attempt(job, inputs, machine)
        process: dict[str, object] | None = None
        observer: dict[str, object] | None = None
        ui_driver: dict[str, object] | None = None
        promotion: Path | None = None
        try:
            process, ui_driver = _invoke_boot_job(
                invocation,
                job,
                timeout_seconds=timeout_seconds,
            )
            if (
                process.get("exit_code") != 0
                or process.get("timed_out") is not False
                or process.get("killed") is not False
            ):
                error = OracleError(
                    "Windows boot container did not exit cleanly",
                    exit_code=EXIT_BACKEND,
                )
                error.process_result = process
                raise error
            observer = _validate_observer_evidence(job / "diagnostics")
            validated_driver = _validate_ui_evidence(job)
            if ui_driver != validated_driver:
                raise OracleError(
                    "UI driver result changed after execution",
                    exit_code=EXIT_INTEGRITY,
                )
            _validate_boot_return(runtime_root)
            machine.advance("guest-returned", evidence="BOOT.OK")
            _write_attempt(job, inputs, machine)
            verify_windows_install_candidate(home, parent_key)
            raw_tree = _inventory_windows_runtime(runtime_root)
            atomic_write_json(job / "raw-tree.json", raw_tree)
            _remove_boot_controls(runtime_root)
            _normalize_runtime_metadata(runtime_root)
            boot_tree = _validate_windows_tree(runtime_root)
            atomic_write_json(job / "boot-tree.json", boot_tree)
            machine.advance("validated", evidence="boot-tree.json")
            _write_attempt(job, inputs, machine)

            promotion = Path(
                tempfile.mkdtemp(
                    prefix=f".{key}.",
                    suffix=".staging",
                    dir=ready_parent,
                )
            )
            promotion.chmod(0o700)
            shutil.copytree(
                runtime_root,
                promotion / "pristine-c",
                copy_function=shutil.copy2,
            )
            _make_tree_read_only(promotion / "pristine-c")
            sealed_tree = _validate_windows_tree(promotion / "pristine-c")
            manifest: dict[str, Any] = {
                "schema": WINDOWS_READY_SCHEMA,
                "runtime_schema": RUNTIME_SCHEMA,
                "backend": "real",
                "baseline_eligible": False,
                "status": "windows-ready",
                "checkpoint_role": "base-for-ami-pro-installation",
                "runtime_key": key,
                "parent_checkpoint_key": parent_key,
                "inputs_digest": digest_json(inputs),
                "boot_tree_digest": boot_tree["digest"],
                "boot_tree_manifest_digest": digest_json(boot_tree),
                "sealed_tree_digest": sealed_tree["digest"],
                "sealed_tree_manifest_digest": digest_json(sealed_tree),
                "tree_file_count": sealed_tree["file_count"],
                "tree_directory_count": sealed_tree["directory_count"],
                "tree_total_bytes": sealed_tree["total_bytes"],
                "printer_profile": "none",
            }
            atomic_write_json(promotion / "runtime.json", manifest)
            atomic_write_json(promotion / "inputs.json", inputs)
            atomic_write_json(promotion / "boot-tree.json", boot_tree)
            atomic_write_json(promotion / "sealed-tree.json", sealed_tree)
            atomic_write(promotion / "dosbox-x.conf", config_bytes)
            atomic_write(promotion / "WINBOOT.BAT", batch_bytes)
            evidence_result = {
                **_ready_result(manifest, cache_reused=False, evidence_job=job.name),
                "observer": observer,
                "ui_driver": ui_driver,
                "process_result": process,
                "state_trace": machine.trace,
                "evidence_stage": "validated-before-atomic-promotion",
            }
            result_path = job / "result.json"
            atomic_write_json(result_path, evidence_result)
            atomic_write_json(
                promotion / "evidence-receipt.json",
                {
                    "schema": "amipro-oracle-windows-ready-evidence-v1",
                    "runtime_key": key,
                    "evidence_job": job.name,
                    "result_sha256": sha256_file(result_path),
                },
            )
            for path in promotion.iterdir():
                if path.is_file():
                    path.chmod(0o444)
            _tree_fsync(promotion)
            promotion.chmod(0o555)
            _directory_fsync(promotion)
            os.rename(promotion, final)
            promotion = final
            _directory_fsync(ready_parent)
            verified, evidence_job = _verify_ready_cache(home, final, key, inputs)
            machine.advance("ready", evidence=f"cache/windows-ready/{key}/runtime.json")
            _write_attempt(job, inputs, machine)
            promotion = None
            return {
                **_ready_result(verified, cache_reused=False, evidence_job=evidence_job),
                "observer": observer,
                "ui_driver": ui_driver,
                "process_result": process,
                "state_trace": machine.trace,
                "promotion_state": "committed",
            }
        except BaseException as exc:
            attached_process = getattr(exc, "process_result", None)
            if process is None and isinstance(attached_process, dict):
                process = attached_process
            attached_driver = getattr(exc, "ui_driver", None)
            if ui_driver is None and isinstance(attached_driver, dict):
                ui_driver = attached_driver
            promotion_evidence: str | None = None
            if promotion is not None and promotion.exists():
                failed = job / "failed-promotion"
                try:
                    os.rename(promotion, failed)
                    promotion_evidence = "failed-promotion"
                except OSError:
                    promotion_evidence = f"cache/windows-ready/{promotion.name}"
            if machine.state != "failed" and "failed" in machine.transitions.get(
                machine.state,
                frozenset(),
            ):
                machine.advance("failed", evidence="failure.json")
            failure: dict[str, object] = {
                "schema": "amipro-oracle-windows-boot-failure-v1",
                "phase": "program-manager-boot",
                "status": "failure",
                "baseline_eligible": False,
                "inputs_digest": digest_json(inputs),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "state_trace": machine.trace,
            }
            if process is not None:
                failure["process_result"] = process
            if observer is not None:
                failure["observer"] = observer
            if ui_driver is not None:
                failure["ui_driver"] = ui_driver
            if promotion_evidence is not None:
                failure["promotion_evidence"] = promotion_evidence
            atomic_write_json(job / "failure.json", failure)
            _write_attempt(job, inputs, machine)
            raise
