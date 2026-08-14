from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import threading
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from . import amipro_install as install_module
from . import oci as oci_module
from . import process as process_module
from . import windows_boot_probe as boot_module
from . import windows_bootstrap as bootstrap_module
from .config import DOSBOX_PROFILE, dosbox_config
from .constants import (
    EXIT_BACKEND,
    EXIT_INTEGRITY,
    EXIT_MISSING,
    EXIT_USAGE,
    RUNTIME_SCHEMA,
)
from .errors import OracleError
from .io import atomic_write, atomic_write_json, digest_json, read_json_object, sha256_file
from .oci import (
    BindMount,
    PodmanInvocation,
    build_podman_invocation,
    exec_podman_checked,
    run_podman_bounded,
)
from .state import StateMachine
from .windows_bootstrap import (
    GUEST_DATE,
    GUEST_TIME,
    NORMALIZED_RUNTIME_MTIME_NS,
    WINDOWS_FREE_MB,
    _cache_lock,
    _directory_fsync,
    _ensure_private_directories,
    _make_tree_read_only,
    _normalize_runtime_metadata,
    _require_verified_image,
    _tree_fsync,
    _validate_observer_evidence,
)

AMIPRO_LAUNCH_INPUT_SCHEMA = "amipro-oracle-amipro-launch-input-v1"
AMIPRO_READY_SCHEMA = "amipro-oracle-amipro-ready-v1"
AMIPRO_LAUNCH_RESULT_SCHEMA = "amipro-oracle-amipro-launch-result-v1"
AMIPRO_LAUNCH_UI_SCHEMA = "amipro-oracle-amipro-launch-driver-v1"
INNER_TIME_LIMIT_SECONDS = 100
OUTER_TIME_LIMIT_SECONDS = 120
UI_DRIVER_TIMEOUT_SECONDS = 100

PRINTER_WARNING_STATE = {
    "name": "printer-driver-warning",
    "box": [380, 414, 505, 454],
    "title_sha256": "210155d9800401f175808daf84457658cea8287a42323fd9e250f0c0934d3c9b",
}
EDITOR_READY_STATE = {
    "name": "amipro-untitled-editor",
    "box": [192, 199, 500, 400],
    "title_sha256": "49d30dbf2790ebca8637381e87ff334bdf4a99d16fc22cee015901beaad3bd89",
}
PROGRAM_MANAGER_MINIMIZED_STATE = {
    "name": "program-manager-minimized",
    "box": [205, 608, 260, 676],
    "title_sha256": "48dc908406cd8bd1ac7a6eec92828d28bb41ec8c3f4f844ba9bdaea8736759e0",
}
LAUNCH_STATES: tuple[dict[str, object], ...] = (
    PRINTER_WARNING_STATE,
    EDITOR_READY_STATE,
    PROGRAM_MANAGER_MINIMIZED_STATE,
    install_module.EXIT_WINDOWS_STATE,
)
LAUNCH_UI_PROFILE = {
    "name": "amipro-3.1-launch-screen-formatting-clean-exit-v1",
    "screen_width": install_module.SCREEN_WIDTH,
    "screen_height": install_module.SCREEN_HEIGHT,
    "autolock": False,
    "stable_samples": 2,
    "poll_seconds": 0.25,
    "states": list(LAUNCH_STATES),
    "actions": [
        "dismiss-printer-warning",
        "close-amipro",
        "exit-windows",
        "confirm-exit-windows",
    ],
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def amipro_launch_config() -> str:
    config = dosbox_config(
        runtime_free_mb=WINDOWS_FREE_MB,
        autoexec=(
            "COUNTRY 1",
            f"DATE {GUEST_DATE}",
            f"TIME {GUEST_TIME}",
            r"Z:\CONFIG.COM -SECUREMODE",
            r"C:\AMILNCH.BAT",
        ),
    )
    return config.replace("autolock=true", "autolock=false")


def amipro_launch_batch() -> bytes:
    lines = (
        "@ECHO OFF",
        r"IF EXIST C:\AMILNCH.STA DEL C:\AMILNCH.STA",
        r"IF EXIST C:\AMILNCH.OK DEL C:\AMILNCH.OK",
        r"IF EXIST C:\AMILNCH.ERR DEL C:\AMILNCH.ERR",
        r"IF NOT EXIST C:\AMIPRO\AMIPRO.EXE GOTO LAUNCH_MISSING",
        r"ECHO AMIPRO_LAUNCH_REQUESTED>C:\AMILNCH.STA",
        r"C:\WINDOWS\WIN.COM C:\AMIPRO\AMIPRO.EXE",
        "IF ERRORLEVEL 1 GOTO LAUNCH_FAILED",
        r"ECHO AMIPRO_RETURNED_ZERO>C:\AMILNCH.OK",
        "GOTO LAUNCH_DONE",
        ":LAUNCH_MISSING",
        r"ECHO AMIPRO_EXE_MISSING>C:\AMILNCH.ERR",
        "GOTO LAUNCH_DONE",
        ":LAUNCH_FAILED",
        r"ECHO AMIPRO_ERRORLEVEL_NONZERO>C:\AMILNCH.ERR",
        ":LAUNCH_DONE",
        "EXIT",
    )
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def _source_fingerprints() -> dict[str, str]:
    modules = {
        "amipro_install": Path(install_module.__file__),
        "amipro_launch_probe": Path(__file__),
        "oci": Path(oci_module.__file__),
        "process": Path(process_module.__file__),
        "windows_boot_probe": Path(boot_module.__file__),
        "windows_bootstrap": Path(bootstrap_module.__file__),
    }
    return {name: sha256_file(path) for name, path in sorted(modules.items())}


def _select_install_candidate(
    home: Path,
    checkpoint_key: str | None,
) -> tuple[Path, dict[str, Any], dict[str, Any], str]:
    parent = home / "cache" / "amipro"
    if parent.is_symlink() or not parent.is_dir():
        raise OracleError("Ami Pro install-candidate cache is missing", exit_code=EXIT_MISSING)
    if checkpoint_key is None:
        candidates: list[str] = []
        for path in sorted(parent.iterdir(), key=lambda item: item.name):
            if _SHA256.fullmatch(path.name) is None:
                continue
            if path.is_symlink() or not path.is_dir():
                raise OracleError("unsafe Ami Pro checkpoint entry", exit_code=EXIT_INTEGRITY)
            manifest_path = path / "runtime.json"
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise OracleError(
                    "Ami Pro checkpoint manifest is missing",
                    exit_code=EXIT_INTEGRITY,
                )
            try:
                manifest = read_json_object(manifest_path)
            except (OSError, ValueError) as exc:
                raise OracleError(
                    "Ami Pro checkpoint manifest is invalid",
                    exit_code=EXIT_INTEGRITY,
                ) from exc
            if manifest.get("status") == "amipro-install-candidate":
                candidates.append(path.name)
        if not candidates:
            raise OracleError(
                "run install-amipro before the Ami Pro launch probe",
                exit_code=EXIT_MISSING,
            )
        if len(candidates) != 1:
            raise OracleError(
                "multiple Ami Pro install candidates exist; pass --checkpoint-key",
                exit_code=EXIT_USAGE,
            )
        checkpoint_key = candidates[0]
    if _SHA256.fullmatch(checkpoint_key) is None:
        raise OracleError("invalid --checkpoint-key", exit_code=EXIT_USAGE)
    root = parent / checkpoint_key
    inputs_path = root / "inputs.json"
    if inputs_path.is_symlink() or not inputs_path.is_file():
        raise OracleError("Ami Pro checkpoint inputs are missing", exit_code=EXIT_INTEGRITY)
    try:
        inputs = read_json_object(inputs_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Ami Pro checkpoint inputs are invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    checkpoint, evidence_job = install_module._verify_checkpoint(
        home,
        root,
        checkpoint_key,
        inputs,
    )
    return root, checkpoint, inputs, evidence_job


def amipro_launch_inputs(
    candidate: dict[str, Any],
    image_record: dict[str, Any],
    *,
    outer_time_limit_seconds: float = OUTER_TIME_LIMIT_SECONDS,
) -> dict[str, object]:
    if (
        isinstance(outer_time_limit_seconds, bool)
        or not isinstance(outer_time_limit_seconds, (int, float))
        or not 1 <= outer_time_limit_seconds <= OUTER_TIME_LIMIT_SECONDS
    ):
        raise OracleError(
            f"Ami Pro launch timeout must be between 1 and {OUTER_TIME_LIMIT_SECONDS} seconds",
            exit_code=EXIT_USAGE,
        )
    image_id = image_record.get("image_id")
    image_digest = image_record.get("image_digest")
    lock_hash = image_record.get("lock_sha256")
    if (
        candidate.get("schema") != install_module.AMIPRO_CHECKPOINT_SCHEMA
        or candidate.get("status") != "amipro-install-candidate"
        or not isinstance(candidate.get("checkpoint_key"), str)
        or _SHA256.fullmatch(str(candidate["checkpoint_key"])) is None
        or not isinstance(image_id, str)
        or _SHA256.fullmatch(image_id) is None
        or not isinstance(image_digest, str)
        or _IMAGE_DIGEST.fullmatch(image_digest) is None
        or not isinstance(lock_hash, str)
        or _SHA256.fullmatch(lock_hash) is None
        or image_record.get("platform") != "linux/amd64"
    ):
        raise OracleError("invalid Ami Pro launch input identity", exit_code=EXIT_INTEGRITY)
    config = amipro_launch_config().encode("utf-8")
    batch = amipro_launch_batch()
    return {
        "schema": AMIPRO_LAUNCH_INPUT_SCHEMA,
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
        "driver_profile": LAUNCH_UI_PROFILE,
        "dosbox_profile": DOSBOX_PROFILE,
        "dosbox_config_sha256": hashlib.sha256(config).hexdigest(),
        "launch_batch_sha256": hashlib.sha256(batch).hexdigest(),
        "orchestrator_sha256": _source_fingerprints(),
        "guest_clock": {"date_command": GUEST_DATE, "time_command": GUEST_TIME},
        "reported_free_mb": WINDOWS_FREE_MB,
        "runtime_metadata_policy": {
            "mtime_ns": NORMALIZED_RUNTIME_MTIME_NS,
            "file_mode": "0644",
            "directory_mode": "0755",
            "timezone": "UTC",
        },
        "printer_profile": "none-screen-formatting-warning-expected",
        "inner_time_limit_seconds": INNER_TIME_LIMIT_SECONDS,
        "outer_time_limit_seconds": outer_time_limit_seconds,
    }


def _wait_launch_sentinel(runtime: Path, stop: threading.Event, deadline: float) -> None:
    launch = runtime / "AMILNCH.STA"
    while monotonic() < deadline and not stop.is_set():
        if (
            launch.is_file()
            and not launch.is_symlink()
            and launch.read_bytes() == b"AMIPRO_LAUNCH_REQUESTED\r\n"
        ):
            return
        sleep(0.1)
    raise OracleError("Ami Pro launch sentinel was not observed", exit_code=EXIT_BACKEND)


def _capture_state(
    job: Path,
    state: dict[str, object],
    filename: str,
    *,
    stop: threading.Event,
    deadline: float,
) -> dict[str, object]:
    evidence, payload = install_module._wait_installer_state(
        job / "diagnostics" / "screen-last.png",
        state,
        stop=stop,
        deadline=deadline,
    )
    path = job / "diagnostics" / filename
    atomic_write(path, payload)
    evidence["path"] = filename
    return evidence


def _drive_amipro_lifecycle(
    invocation: PodmanInvocation,
    job: Path,
    stop: threading.Event,
) -> dict[str, object]:
    deadline = monotonic() + UI_DRIVER_TIMEOUT_SECONDS
    _wait_launch_sentinel(job / "runtime", stop, deadline)
    warning = _capture_state(
        job,
        PRINTER_WARNING_STATE,
        "amipro-printer-warning.png",
        stop=stop,
        deadline=deadline,
    )
    search = exec_podman_checked(
        invocation,
        ("xdotool", "search", "--onlyvisible", "--name", "DOSBox-X"),
        environment={"DISPLAY": ":99"},
    )
    windows = [line for line in str(search["stdout"]).splitlines() if line.isdigit()]
    if search["exit_code"] != 0 or len(windows) != 1:
        raise OracleError("cannot identify the DOSBox-X UI window", exit_code=EXIT_BACKEND)
    window = windows[0]

    actions: list[dict[str, object]] = []
    for action, key in (
        ("dismiss-printer-warning", "Return"),
        ("close-amipro", "alt+F4"),
        ("exit-windows", "alt+F4"),
        ("confirm-exit-windows", "Return"),
    ):
        if action == "close-amipro":
            editor = _capture_state(
                job,
                EDITOR_READY_STATE,
                "amipro-editor-ready.png",
                stop=stop,
                deadline=deadline,
            )
        elif action == "exit-windows":
            minimized = _capture_state(
                job,
                PROGRAM_MANAGER_MINIMIZED_STATE,
                "program-manager-minimized.png",
                stop=stop,
                deadline=deadline,
            )
        elif action == "confirm-exit-windows":
            confirmation = _capture_state(
                job,
                install_module.EXIT_WINDOWS_STATE,
                "exit-windows-confirmation.png",
                stop=stop,
                deadline=deadline,
            )
        result = exec_podman_checked(
            invocation,
            ("xdotool", "key", "--window", window, key),
            environment={"DISPLAY": ":99"},
        )
        if result["exit_code"] != 0:
            raise OracleError(f"cannot perform UI action: {action}", exit_code=EXIT_BACKEND)
        actions.append({"action": action, "key": key, "exit_code": 0})
    return {
        "schema": AMIPRO_LAUNCH_UI_SCHEMA,
        "status": "success",
        "profile": LAUNCH_UI_PROFILE,
        "states": [warning, editor, minimized, confirmation],
        "actions": actions,
    }


def _invoke_launch_job(
    invocation: PodmanInvocation,
    job: Path,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, object], dict[str, object]]:
    stop = threading.Event()
    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["result"] = _drive_amipro_lifecycle(invocation, job, stop)
        except BaseException as exc:
            box["result"] = {
                "schema": AMIPRO_LAUNCH_UI_SCHEMA,
                "status": "failure",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    driver = threading.Thread(target=worker, name="amipro-launch-driver", daemon=True)
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
    value = box.get("result")
    driver_result = value if isinstance(value, dict) else {
        "schema": AMIPRO_LAUNCH_UI_SCHEMA,
        "status": "failure",
        "error_type": "DriverThreadError",
        "error": "Ami Pro launch driver did not return evidence",
    }
    if driver.is_alive():
        driver_result = {
            "schema": AMIPRO_LAUNCH_UI_SCHEMA,
            "status": "failure",
            "error_type": "DriverThreadError",
            "error": "Ami Pro launch driver thread did not stop",
        }
    atomic_write_json(job / "ui-driver.json", driver_result)
    if process_error is not None:
        if isinstance(process_error, OracleError):
            process_error.ui_driver = driver_result
        raise process_error
    if process is None:
        raise OracleError("Ami Pro launch process did not return", exit_code=EXIT_BACKEND)
    if driver_result.get("status") != "success":
        error = OracleError(
            f"Ami Pro launch driver failed: {driver_result.get('error', 'unknown error')}",
            exit_code=EXIT_BACKEND,
        )
        error.process_result = process
        error.ui_driver = driver_result
        raise error
    return process, driver_result


def _validate_ui_evidence(job: Path) -> dict[str, object]:
    path = job / "ui-driver.json"
    if path.is_symlink() or not path.is_file():
        raise OracleError("Ami Pro launch UI evidence is missing", exit_code=EXIT_INTEGRITY)
    try:
        driver = read_json_object(path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Ami Pro launch UI evidence is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    states = driver.get("states")
    actions = driver.get("actions")
    expected_actions = [
        {"action": "dismiss-printer-warning", "key": "Return", "exit_code": 0},
        {"action": "close-amipro", "key": "alt+F4", "exit_code": 0},
        {"action": "exit-windows", "key": "alt+F4", "exit_code": 0},
        {"action": "confirm-exit-windows", "key": "Return", "exit_code": 0},
    ]
    filenames = (
        "amipro-printer-warning.png",
        "amipro-editor-ready.png",
        "program-manager-minimized.png",
        "exit-windows-confirmation.png",
    )
    if (
        driver.get("schema") != AMIPRO_LAUNCH_UI_SCHEMA
        or driver.get("status") != "success"
        or driver.get("profile") != LAUNCH_UI_PROFILE
        or not isinstance(states, list)
        or len(states) != len(LAUNCH_STATES)
        or actions != expected_actions
    ):
        raise OracleError("Ami Pro launch UI evidence mismatch", exit_code=EXIT_INTEGRITY)
    observed_states: list[dict[str, object]] = []
    for state, filename in zip(LAUNCH_STATES, filenames, strict=True):
        screenshot = job / "diagnostics" / filename
        try:
            observed, _ = install_module._screen_state(screenshot, state)
        except OracleError as exc:
            raise OracleError(
                "Ami Pro launch screenshot is invalid",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        observed["path"] = filename
        observed_states.append(observed)
    if states != observed_states:
        raise OracleError("Ami Pro launch screenshots changed", exit_code=EXIT_INTEGRITY)
    return driver


def _validate_launch_return(runtime: Path) -> None:
    expected = {
        "AMILNCH.STA": b"AMIPRO_LAUNCH_REQUESTED\r\n",
        "AMILNCH.OK": b"AMIPRO_RETURNED_ZERO\r\n",
    }
    for name, payload in expected.items():
        path = runtime / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise OracleError(f"Ami Pro launch sentinel is invalid: {name}", exit_code=EXIT_BACKEND)
    if (runtime / "AMILNCH.ERR").exists() or (runtime / "AMILNCH.ERR").is_symlink():
        raise OracleError("Ami Pro launch reported a nonzero error", exit_code=EXIT_BACKEND)


def _remove_launch_controls(runtime: Path) -> None:
    for name in ("AMILNCH.STA", "AMILNCH.OK", "AMILNCH.ERR", "AMILNCH.BAT"):
        path = runtime / name
        if path.is_symlink():
            raise OracleError("launch control path became a symlink", exit_code=EXIT_INTEGRITY)
        path.unlink(missing_ok=True)


def _write_attempt(job: Path, inputs: dict[str, object], machine: StateMachine) -> None:
    atomic_write_json(
        job / "attempt.json",
        {
            "schema": "amipro-oracle-amipro-launch-attempt-v1",
            "phase": "amipro-launch",
            "inputs_digest": digest_json(inputs),
            "state": machine.state,
            "state_trace": machine.trace,
        },
    )


def _result(
    runtime: dict[str, Any],
    *,
    cache_reused: bool,
    evidence_job: str,
) -> dict[str, Any]:
    return {
        "schema": AMIPRO_LAUNCH_RESULT_SCHEMA,
        "status": runtime["status"],
        "runtime_key": runtime["runtime_key"],
        "parent_checkpoint_key": runtime["parent_checkpoint_key"],
        "cache_reused": cache_reused,
        "evidence_job": evidence_job,
        "runtime": runtime,
    }


def _load_evidence(
    home: Path,
    root: Path,
    key: str,
    runtime: dict[str, Any],
) -> str:
    receipt_path = root / "evidence-receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise OracleError("Ami Pro-ready evidence receipt is missing", exit_code=EXIT_INTEGRITY)
    try:
        receipt = read_json_object(receipt_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Ami Pro-ready evidence receipt is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    job_name = receipt.get("evidence_job")
    result_hash = receipt.get("result_sha256")
    if (
        set(receipt) != {"schema", "runtime_key", "evidence_job", "result_sha256"}
        or receipt.get("schema") != "amipro-oracle-amipro-ready-evidence-v1"
        or receipt.get("runtime_key") != key
        or not isinstance(job_name, str)
        or re.fullmatch(r"launch-amipro-[a-z0-9_-]+", job_name) is None
        or not isinstance(result_hash, str)
        or _SHA256.fullmatch(result_hash) is None
    ):
        raise OracleError("Ami Pro-ready evidence receipt mismatch", exit_code=EXIT_INTEGRITY)
    job = home / "jobs" / job_name
    result_path = job / "result.json"
    if (
        job.is_symlink()
        or not job.is_dir()
        or result_path.is_symlink()
        or not result_path.is_file()
        or sha256_file(result_path) != result_hash
    ):
        raise OracleError("Ami Pro-ready evidence result mismatch", exit_code=EXIT_INTEGRITY)
    try:
        recorded = read_json_object(result_path)
        observer = _validate_observer_evidence(job / "diagnostics")
        driver = _validate_ui_evidence(job)
    except (OSError, ValueError, OracleError) as exc:
        raise OracleError("Ami Pro-ready evidence is invalid", exit_code=EXIT_INTEGRITY) from exc
    process = recorded.get("process_result")
    trace = recorded.get("state_trace")
    if (
        recorded.get("schema") != AMIPRO_LAUNCH_RESULT_SCHEMA
        or recorded.get("status") != "amipro-ready"
        or recorded.get("runtime_key") != key
        or recorded.get("cache_reused") is not False
        or recorded.get("evidence_job") != job_name
        or recorded.get("runtime") != runtime
        or recorded.get("observer") != observer
        or recorded.get("ui_driver") != driver
        or recorded.get("evidence_stage") != "validated-before-atomic-promotion"
        or not isinstance(process, dict)
        or process.get("exit_code") != 0
        or process.get("timed_out") is not False
        or process.get("killed") is not False
        or not isinstance(trace, list)
        or not all(isinstance(event, dict) for event in trace)
        or [event.get("state") for event in trace]
        != ["created", "staged", "guest-invoked", "guest-returned", "validated"]
    ):
        raise OracleError("Ami Pro-ready evidence identity mismatch", exit_code=EXIT_INTEGRITY)
    return job_name


def _verify_ready_cache(
    home: Path,
    root: Path,
    key: str,
    inputs: dict[str, object],
) -> tuple[dict[str, Any], str]:
    paths = {
        "runtime": root / "runtime.json",
        "inputs": root / "inputs.json",
        "launch_tree": root / "launch-tree.json",
        "sealed_tree": root / "sealed-tree.json",
        "config": root / "dosbox-x.conf",
        "batch": root / "AMILNCH.BAT",
        "receipt": root / "evidence-receipt.json",
    }
    pristine = root / "pristine-c"
    expected_names = {
        "AMILNCH.BAT",
        "dosbox-x.conf",
        "evidence-receipt.json",
        "inputs.json",
        "launch-tree.json",
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
        raise OracleError("Ami Pro-ready cache has an unsafe shape", exit_code=EXIT_INTEGRITY)
    try:
        runtime = read_json_object(paths["runtime"])
        recorded_inputs = read_json_object(paths["inputs"])
        launch_tree = read_json_object(paths["launch_tree"])
        recorded_sealed = read_json_object(paths["sealed_tree"])
        config_bytes = paths["config"].read_bytes()
        batch_bytes = paths["batch"].read_bytes()
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Ami Pro-ready cache manifest is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    sealed = install_module._validate_installed_amipro(pristine)
    expected_launch_tree = install_module._unsealed_tree(sealed)
    expected_keys = {
        "backend",
        "baseline_eligible",
        "checkpoint_role",
        "inputs_digest",
        "launch_tree_digest",
        "launch_tree_manifest_digest",
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
    if (
        set(runtime) != expected_keys
        or runtime.get("schema") != AMIPRO_READY_SCHEMA
        or runtime.get("runtime_schema") != RUNTIME_SCHEMA
        or runtime.get("backend") != "real"
        or runtime.get("baseline_eligible") is not False
        or runtime.get("status") != "amipro-ready"
        or runtime.get("checkpoint_role") != "base-for-invented-document-smoke"
        or runtime.get("runtime_key") != key
        or runtime.get("inputs_digest") != digest_json(inputs)
        or key != digest_json(inputs)
        or runtime.get("parent_checkpoint_key")
        != inputs.get("install_candidate", {}).get("checkpoint_key")
        or runtime.get("printer_profile") != "none-screen-formatting-warning-expected"
        or recorded_inputs != inputs
        or config_bytes != amipro_launch_config().encode("utf-8")
        or batch_bytes != amipro_launch_batch()
        or hashlib.sha256(config_bytes).hexdigest() != inputs.get("dosbox_config_sha256")
        or hashlib.sha256(batch_bytes).hexdigest() != inputs.get("launch_batch_sha256")
        or launch_tree != expected_launch_tree
        or launch_tree.get("digest") != runtime.get("launch_tree_digest")
        or digest_json(launch_tree) != runtime.get("launch_tree_manifest_digest")
        or recorded_sealed != sealed
        or sealed.get("digest") != runtime.get("sealed_tree_digest")
        or digest_json(sealed) != runtime.get("sealed_tree_manifest_digest")
        or sealed.get("file_count") != runtime.get("tree_file_count")
        or sealed.get("directory_count") != runtime.get("tree_directory_count")
        or sealed.get("total_bytes") != runtime.get("tree_total_bytes")
        or any(int(str(entry["mode"]), 8) & 0o222 for entry in sealed["entries"])
    ):
        raise OracleError("Ami Pro-ready cache identity mismatch", exit_code=EXIT_INTEGRITY)
    evidence_job = _load_evidence(home, root, key, runtime)
    return runtime, evidence_job


def launch_amipro_ready(
    home: Path,
    image_record: dict[str, Any],
    *,
    checkpoint_key: str | None = None,
    timeout_seconds: float = OUTER_TIME_LIMIT_SECONDS,
) -> dict[str, Any]:
    _require_verified_image(image_record)
    _ensure_private_directories(home)
    ready_parent = home / "cache" / "amipro-ready"
    if ready_parent.is_symlink():
        raise OracleError("Ami Pro-ready cache parent is unsafe", exit_code=EXIT_INTEGRITY)
    if not ready_parent.exists():
        ready_parent.mkdir(mode=0o700)
    elif not ready_parent.is_dir():
        raise OracleError("Ami Pro-ready cache parent is not a directory", exit_code=EXIT_INTEGRITY)
    ready_parent.chmod(0o700)
    source_root, candidate, _candidate_inputs, _candidate_evidence = (
        _select_install_candidate(home, checkpoint_key)
    )
    inputs = amipro_launch_inputs(
        candidate,
        image_record,
        outer_time_limit_seconds=timeout_seconds,
    )
    key = digest_json(inputs)
    parent_key = str(candidate["checkpoint_key"])
    final = ready_parent / key
    with _cache_lock(home, parent_key), _cache_lock(home, key):
        source_root, checked_candidate, _candidate_inputs, _candidate_evidence = (
            _select_install_candidate(home, parent_key)
        )
        checked_inputs = amipro_launch_inputs(
            checked_candidate,
            image_record,
            outer_time_limit_seconds=timeout_seconds,
        )
        if checked_inputs != inputs:
            raise OracleError("Ami Pro candidate changed after keying", exit_code=EXIT_INTEGRITY)
        if final.exists() or final.is_symlink():
            runtime, evidence_job = _verify_ready_cache(home, final, key, inputs)
            return _result(runtime, cache_reused=True, evidence_job=evidence_job)

        job = Path(tempfile.mkdtemp(prefix=f"launch-amipro-{key[:12]}-", dir=home / "jobs"))
        job.chmod(0o700)
        _directory_fsync(home / "jobs")
        for name in ("capture", "diagnostics", "home"):
            (job / name).mkdir(mode=0o700)
        runtime_root = job / "runtime"
        shutil.copytree(
            source_root / "pristine-c",
            runtime_root,
            copy_function=shutil.copy2,
        )
        _normalize_runtime_metadata(runtime_root)
        copied = install_module._validate_installed_amipro(runtime_root)
        if copied["digest"] != candidate["guest_tree_digest"]:
            raise OracleError(
                "disposable Ami Pro copy does not match its candidate",
                exit_code=EXIT_INTEGRITY,
            )
        config_bytes = amipro_launch_config().encode("utf-8")
        batch_bytes = amipro_launch_batch()
        atomic_write(runtime_root / "AMILNCH.BAT", batch_bytes)
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
            container_name=f"amipro-oracle-launch-{suffix}",
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
            process, ui_driver = _invoke_launch_job(
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
                    "Ami Pro launch container did not exit cleanly",
                    exit_code=EXIT_BACKEND,
                )
                error.process_result = process
                raise error
            observer = _validate_observer_evidence(job / "diagnostics")
            validated_driver = _validate_ui_evidence(job)
            if ui_driver != validated_driver:
                raise OracleError("Ami Pro launch evidence changed", exit_code=EXIT_INTEGRITY)
            _validate_launch_return(runtime_root)
            machine.advance("guest-returned", evidence="AMILNCH.OK")
            _write_attempt(job, inputs, machine)
            _select_install_candidate(home, parent_key)
            raw_tree = install_module._validate_installed_amipro(runtime_root)
            atomic_write_json(job / "raw-tree.json", raw_tree)
            _remove_launch_controls(runtime_root)
            _normalize_runtime_metadata(runtime_root)
            launch_tree = install_module._validate_installed_amipro(runtime_root)
            atomic_write_json(job / "launch-tree.json", launch_tree)
            machine.advance("validated", evidence="launch-tree.json")
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
            sealed_tree = install_module._validate_installed_amipro(
                promotion / "pristine-c"
            )
            manifest: dict[str, Any] = {
                "schema": AMIPRO_READY_SCHEMA,
                "runtime_schema": RUNTIME_SCHEMA,
                "backend": "real",
                "baseline_eligible": False,
                "status": "amipro-ready",
                "checkpoint_role": "base-for-invented-document-smoke",
                "runtime_key": key,
                "parent_checkpoint_key": parent_key,
                "inputs_digest": digest_json(inputs),
                "launch_tree_digest": launch_tree["digest"],
                "launch_tree_manifest_digest": digest_json(launch_tree),
                "sealed_tree_digest": sealed_tree["digest"],
                "sealed_tree_manifest_digest": digest_json(sealed_tree),
                "tree_file_count": sealed_tree["file_count"],
                "tree_directory_count": sealed_tree["directory_count"],
                "tree_total_bytes": sealed_tree["total_bytes"],
                "printer_profile": "none-screen-formatting-warning-expected",
            }
            atomic_write_json(promotion / "runtime.json", manifest)
            atomic_write_json(promotion / "inputs.json", inputs)
            atomic_write_json(promotion / "launch-tree.json", launch_tree)
            atomic_write_json(promotion / "sealed-tree.json", sealed_tree)
            atomic_write(promotion / "dosbox-x.conf", config_bytes)
            atomic_write(promotion / "AMILNCH.BAT", batch_bytes)
            evidence_result = {
                **_result(manifest, cache_reused=False, evidence_job=job.name),
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
                    "schema": "amipro-oracle-amipro-ready-evidence-v1",
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
            machine.advance("ready", evidence=f"cache/amipro-ready/{key}/runtime.json")
            _write_attempt(job, inputs, machine)
            promotion = None
            return {
                **_result(verified, cache_reused=False, evidence_job=evidence_job),
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
                    promotion_evidence = f"cache/amipro-ready/{promotion.name}"
            if machine.state != "failed" and "failed" in machine.transitions.get(
                machine.state,
                frozenset(),
            ):
                machine.advance("failed", evidence="failure.json")
            failure: dict[str, object] = {
                "schema": "amipro-oracle-amipro-launch-failure-v1",
                "phase": "amipro-launch",
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
