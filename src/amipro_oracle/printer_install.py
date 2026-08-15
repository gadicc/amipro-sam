from __future__ import annotations

import configparser
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
from . import amipro_launch_probe as launch_module
from . import document_smoke as smoke_module
from . import oci as oci_module
from . import process as process_module
from . import windows_bootstrap as bootstrap_module
from .config import DOSBOX_PROFILE, dosbox_config
from .constants import EXIT_BACKEND, EXIT_INTEGRITY, EXIT_USAGE, RUNTIME_SCHEMA
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
    ensure_flat_windows_media,
)

PRINTER_INSTALL_INPUT_SCHEMA = "amipro-oracle-printer-install-input-v1"
PRINTER_READY_SCHEMA = "amipro-oracle-printer-ready-v1"
PRINTER_INSTALL_RESULT_SCHEMA = "amipro-oracle-printer-install-result-v1"
PRINTER_INSTALL_UI_SCHEMA = "amipro-oracle-printer-install-driver-v1"
INNER_TIME_LIMIT_SECONDS = 75
OUTER_TIME_LIMIT_SECONDS = 90
UI_DRIVER_TIMEOUT_SECONDS = 75

PRINTER_MODEL = "QMS ColorScript 100"
PRINTER_PORT = "LPT1:"
PSCRIPT_DRV_SHA256 = "469a11a947b98716b5aba63e170754c2b1f055ce7e03101c6748c1b1a97ac25d"
PSCRIPT_HLP_SHA256 = "ec64312970e6369f12577f4e4f9b9187ad19ec51b3f1dda55b8d26ed000d63a9"
TESTPS_TXT_SHA256 = "b291cb10bdb2ca62c6e5a70ae393deecb60221c1e74310f5eb29dc1ec55fa151"


def _whole_screen_state(name: str, digest: str) -> dict[str, object]:
    return {
        "name": name,
        "box": [0, 0, install_module.SCREEN_WIDTH, install_module.SCREEN_HEIGHT],
        "title_sha256": digest,
    }


PRINTER_INITIAL_STATE = _whole_screen_state(
    "printer-control-empty",
    "4c4e444dabb1e014053b93dc3d7e64787acd308daad13b7cd991b599ea0797e5",
)
PRINT_MANAGER_DISABLED_STATE = _whole_screen_state(
    "printer-control-direct-to-port",
    "df492a98280f4abd9bf1ae130433695c9925d06308820789c8314db6c5c1f84d",
)
PRINTER_SELECTED_STATE = _whole_screen_state(
    "qms-colorscript-selected",
    "86ae275d41edd1b1a761a1bbdf71bc1cdb7b1ad5651233c9a626d3f5b7a3819a",
)
DRIVER_SOURCE_STATE = _whole_screen_state(
    "pscript-driver-source-prompt",
    "7bdf97fc3f83d36d3aaa449c3191accb5aba662b9d4bd25f2516398777d740ab",
)
PRINTER_INSTALLED_STATE = _whole_screen_state(
    "qms-colorscript-installed",
    "3a656fd702e5248ece654fc486607ae75beaf85df6b8a454b8c7dd983c114834",
)
PRINTER_INSTALL_STATES: tuple[dict[str, object], ...] = (
    PRINTER_INITIAL_STATE,
    PRINT_MANAGER_DISABLED_STATE,
    PRINTER_SELECTED_STATE,
    DRIVER_SOURCE_STATE,
    PRINTER_INSTALLED_STATE,
    launch_module.PROGRAM_MANAGER_MINIMIZED_STATE,
    install_module.EXIT_WINDOWS_STATE,
)
PRINTER_PROFILE = {
    "name": "windows-3.1-qms-colorscript-100-lpt1-direct-v1",
    "model": PRINTER_MODEL,
    "port": PRINTER_PORT,
    "driver": "PSCRIPT.DRV",
    "driver_sha256": PSCRIPT_DRV_SHA256,
    "paper_and_orientation": "driver-default-pending-first-postscript-validation",
    "print_manager": False,
    "ctrl_d": "driver-default; preserve raw and accept only optional boundary EOT",
    "screen_width": install_module.SCREEN_WIDTH,
    "screen_height": install_module.SCREEN_HEIGHT,
    "autolock": False,
    "stable_samples": 2,
    "poll_seconds": 0.25,
    "states": list(PRINTER_INSTALL_STATES),
    "actions": [
        "disable-print-manager",
        "focus-model-list",
        "select-qms-colorscript-100",
        "install-selected-model",
        "replace-source-path-with-s-drive",
        "confirm-source-path",
        "close-control-panel",
        "exit-windows",
        "confirm-exit-windows",
    ],
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def printer_install_config() -> str:
    config = dosbox_config(
        runtime_free_mb=WINDOWS_FREE_MB,
        autoexec=(
            'MOUNT S "/oracle/media/windows" -ro',
            "COUNTRY 1",
            f"DATE {GUEST_DATE}",
            f"TIME {GUEST_TIME}",
            r"Z:\CONFIG.COM -SECUREMODE",
            r"C:\PRNINS.BAT",
        ),
    )
    return config.replace("autolock=true", "autolock=false")


def printer_install_batch() -> bytes:
    lines = (
        "@ECHO OFF",
        r"IF EXIST C:\PRNINS.STA DEL C:\PRNINS.STA",
        r"IF EXIST C:\PRNINS.OK DEL C:\PRNINS.OK",
        r"IF EXIST C:\PRNINS.ERR DEL C:\PRNINS.ERR",
        r"IF NOT EXIST C:\WINDOWS\CONTROL.EXE GOTO CONTROL_MISSING",
        r"ECHO PRINTER_INSTALL_REQUESTED>C:\PRNINS.STA",
        r"C:\WINDOWS\WIN.COM C:\WINDOWS\CONTROL.EXE PRINTERS",
        "IF ERRORLEVEL 1 GOTO INSTALL_FAILED",
        r"ECHO PRINTER_INSTALL_RETURNED_ZERO>C:\PRNINS.OK",
        "GOTO INSTALL_DONE",
        ":CONTROL_MISSING",
        r"ECHO CONTROL_EXE_MISSING>C:\PRNINS.ERR",
        "GOTO INSTALL_DONE",
        ":INSTALL_FAILED",
        r"ECHO PRINTER_INSTALL_ERRORLEVEL_NONZERO>C:\PRNINS.ERR",
        ":INSTALL_DONE",
        "EXIT",
    )
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def _source_fingerprints() -> dict[str, str]:
    modules = {
        "amipro_install": Path(install_module.__file__),
        "amipro_launch_probe": Path(launch_module.__file__),
        "document_smoke": Path(smoke_module.__file__),
        "oci": Path(oci_module.__file__),
        "printer_install": Path(__file__),
        "process": Path(process_module.__file__),
        "windows_bootstrap": Path(bootstrap_module.__file__),
    }
    return {name: sha256_file(path) for name, path in sorted(modules.items())}


def printer_install_inputs(
    ready: dict[str, Any],
    windows_media: dict[str, Any],
    flat_media: dict[str, Any],
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
            f"printer install timeout must be between 1 and {OUTER_TIME_LIMIT_SECONDS} seconds",
            exit_code=EXIT_USAGE,
        )
    image_id = image_record.get("image_id")
    image_digest = image_record.get("image_digest")
    lock_hash = image_record.get("lock_sha256")
    if (
        ready.get("schema") != launch_module.AMIPRO_READY_SCHEMA
        or ready.get("status") != "amipro-ready"
        or not isinstance(ready.get("runtime_key"), str)
        or _SHA256.fullmatch(str(ready["runtime_key"])) is None
        or windows_media.get("kind") != "windows-3.1"
        or windows_media.get("media_profile") != bootstrap_module.WINDOWS_MEDIA_PROFILE
        or windows_media.get("file_count") != 6
        or not isinstance(windows_media.get("digest"), str)
        or flat_media.get("schema") != bootstrap_module.FLAT_MEDIA_SCHEMA
        or flat_media.get("status") != "ready"
        or not isinstance(image_id, str)
        or _SHA256.fullmatch(image_id) is None
        or not isinstance(image_digest, str)
        or _IMAGE_DIGEST.fullmatch(image_digest) is None
        or not isinstance(lock_hash, str)
        or _SHA256.fullmatch(lock_hash) is None
        or image_record.get("platform") != "linux/amd64"
    ):
        raise OracleError("invalid printer-install input identity", exit_code=EXIT_INTEGRITY)
    config = printer_install_config().encode("utf-8")
    batch = printer_install_batch()
    return {
        "schema": PRINTER_INSTALL_INPUT_SCHEMA,
        "amipro_ready": {
            "runtime_key": ready["runtime_key"],
            "manifest_digest": digest_json(ready),
            "launch_tree_digest": ready["launch_tree_digest"],
            "sealed_tree_digest": ready["sealed_tree_digest"],
        },
        "windows_media": {
            "profile": windows_media["media_profile"],
            "digest": windows_media["digest"],
        },
        "flat_media": {
            "cache_key": flat_media["cache_key"],
            "extraction_digest": flat_media["extraction_digest"],
            "tree_digest": flat_media["tree_digest"],
        },
        "toolchain": {
            "image_id": image_id,
            "image_digest": image_digest,
            "lock_sha256": lock_hash,
            "platform": "linux/amd64",
        },
        "printer_profile": PRINTER_PROFILE,
        "dosbox_profile": DOSBOX_PROFILE,
        "dosbox_config_sha256": hashlib.sha256(config).hexdigest(),
        "install_batch_sha256": hashlib.sha256(batch).hexdigest(),
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


def _wait_sentinel(runtime: Path, stop: threading.Event, deadline: float) -> None:
    sentinel = runtime / "PRNINS.STA"
    while monotonic() < deadline and not stop.is_set():
        if (
            sentinel.is_file()
            and not sentinel.is_symlink()
            and sentinel.read_bytes() == b"PRINTER_INSTALL_REQUESTED\r\n"
        ):
            return
        sleep(0.1)
    raise OracleError("printer install sentinel was not observed", exit_code=EXIT_BACKEND)


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
    atomic_write(job / "diagnostics" / filename, payload)
    evidence["path"] = filename
    return evidence


def _drive_printer_install(
    invocation: PodmanInvocation,
    job: Path,
    stop: threading.Event,
) -> dict[str, object]:
    deadline = monotonic() + UI_DRIVER_TIMEOUT_SECONDS
    _wait_sentinel(job / "runtime", stop, deadline)
    states = [
        _capture_state(
            job,
            PRINTER_INITIAL_STATE,
            "printer-control-empty.png",
            stop=stop,
            deadline=deadline,
        )
    ]
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

    def key(action: str, value: str) -> None:
        result = exec_podman_checked(
            invocation,
            ("xdotool", "key", "--window", window, value),
            environment={"DISPLAY": ":99"},
        )
        if result["exit_code"] != 0:
            raise OracleError(f"cannot perform UI action: {action}", exit_code=EXIT_BACKEND)
        actions.append({"action": action, "key": value, "exit_code": 0})

    key("disable-print-manager", "alt+u")
    states.append(
        _capture_state(
            job,
            PRINT_MANAGER_DISABLED_STATE,
            "print-manager-disabled.png",
            stop=stop,
            deadline=deadline,
        )
    )
    key("focus-model-list", "Tab")
    key("select-qms-colorscript-100", "q")
    states.append(
        _capture_state(
            job,
            PRINTER_SELECTED_STATE,
            "qms-colorscript-selected.png",
            stop=stop,
            deadline=deadline,
        )
    )
    key("install-selected-model", "Return")
    states.append(
        _capture_state(
            job,
            DRIVER_SOURCE_STATE,
            "driver-source-prompt.png",
            stop=stop,
            deadline=deadline,
        )
    )
    key("replace-source-path-with-s-drive", "ctrl+a")
    typed = exec_podman_checked(
        invocation,
        (
            "xdotool",
            "type",
            "--window",
            window,
            "--delay",
            "35",
            "--clearmodifiers",
            "S:\\",
        ),
        environment={"DISPLAY": ":99"},
    )
    if typed["exit_code"] != 0:
        raise OracleError("cannot enter the printer source path", exit_code=EXIT_BACKEND)
    actions.append(
        {
            "action": "type-source-path",
            "value": "S:\\",
            "exit_code": 0,
        }
    )
    key("confirm-source-path", "Return")
    states.append(
        _capture_state(
            job,
            PRINTER_INSTALLED_STATE,
            "qms-colorscript-installed.png",
            stop=stop,
            deadline=deadline,
        )
    )
    key("close-control-panel", "alt+F4")
    states.append(
        _capture_state(
            job,
            launch_module.PROGRAM_MANAGER_MINIMIZED_STATE,
            "program-manager-minimized.png",
            stop=stop,
            deadline=deadline,
        )
    )
    key("exit-windows", "alt+F4")
    states.append(
        _capture_state(
            job,
            install_module.EXIT_WINDOWS_STATE,
            "exit-windows-confirmation.png",
            stop=stop,
            deadline=deadline,
        )
    )
    key("confirm-exit-windows", "Return")
    return {
        "schema": PRINTER_INSTALL_UI_SCHEMA,
        "status": "success",
        "profile": PRINTER_PROFILE,
        "states": states,
        "actions": actions,
    }


def _invoke_job(
    invocation: PodmanInvocation,
    job: Path,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, object], dict[str, object]]:
    stop = threading.Event()
    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["result"] = _drive_printer_install(invocation, job, stop)
        except BaseException as exc:
            box["result"] = {
                "schema": PRINTER_INSTALL_UI_SCHEMA,
                "status": "failure",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    driver = threading.Thread(target=worker, name="printer-install-driver", daemon=True)
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
        "schema": PRINTER_INSTALL_UI_SCHEMA,
        "status": "failure",
        "error_type": "DriverThreadError",
        "error": "printer install driver did not return evidence",
    }
    if driver.is_alive():
        driver_result = {
            "schema": PRINTER_INSTALL_UI_SCHEMA,
            "status": "failure",
            "error_type": "DriverThreadError",
            "error": "printer install driver thread did not stop",
        }
    atomic_write_json(job / "ui-driver.json", driver_result)
    if process_error is not None:
        if isinstance(process_error, OracleError):
            process_error.ui_driver = driver_result
        raise process_error
    if process is None:
        raise OracleError("printer install process did not return", exit_code=EXIT_BACKEND)
    if driver_result.get("status") != "success":
        error = OracleError(
            f"printer install driver failed: {driver_result.get('error', 'unknown error')}",
            exit_code=EXIT_BACKEND,
        )
        error.process_result = process
        error.ui_driver = driver_result
        raise error
    return process, driver_result


_STATE_FILES = (
    "printer-control-empty.png",
    "print-manager-disabled.png",
    "qms-colorscript-selected.png",
    "driver-source-prompt.png",
    "qms-colorscript-installed.png",
    "program-manager-minimized.png",
    "exit-windows-confirmation.png",
)


def _expected_actions() -> list[dict[str, object]]:
    return [
        {"action": "disable-print-manager", "key": "alt+u", "exit_code": 0},
        {"action": "focus-model-list", "key": "Tab", "exit_code": 0},
        {"action": "select-qms-colorscript-100", "key": "q", "exit_code": 0},
        {"action": "install-selected-model", "key": "Return", "exit_code": 0},
        {"action": "replace-source-path-with-s-drive", "key": "ctrl+a", "exit_code": 0},
        {"action": "type-source-path", "value": "S:\\", "exit_code": 0},
        {"action": "confirm-source-path", "key": "Return", "exit_code": 0},
        {"action": "close-control-panel", "key": "alt+F4", "exit_code": 0},
        {"action": "exit-windows", "key": "alt+F4", "exit_code": 0},
        {"action": "confirm-exit-windows", "key": "Return", "exit_code": 0},
    ]


def _validate_ui_evidence(job: Path) -> dict[str, object]:
    path = job / "ui-driver.json"
    if path.is_symlink() or not path.is_file():
        raise OracleError("printer UI evidence is missing", exit_code=EXIT_INTEGRITY)
    try:
        driver = read_json_object(path)
    except (OSError, ValueError) as exc:
        raise OracleError("printer UI evidence is invalid", exit_code=EXIT_INTEGRITY) from exc
    states = driver.get("states")
    if (
        driver.get("schema") != PRINTER_INSTALL_UI_SCHEMA
        or driver.get("status") != "success"
        or driver.get("profile") != PRINTER_PROFILE
        or driver.get("actions") != _expected_actions()
        or not isinstance(states, list)
        or len(states) != len(PRINTER_INSTALL_STATES)
    ):
        raise OracleError("printer UI evidence mismatch", exit_code=EXIT_INTEGRITY)
    observed: list[dict[str, object]] = []
    for state, filename in zip(PRINTER_INSTALL_STATES, _STATE_FILES, strict=True):
        try:
            value, _ = install_module._screen_state(job / "diagnostics" / filename, state)
        except OracleError as exc:
            raise OracleError(
                "printer lifecycle screenshot is invalid",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        value["path"] = filename
        observed.append(value)
    if states != observed:
        raise OracleError("printer lifecycle screenshots changed", exit_code=EXIT_INTEGRITY)
    return driver


def _validate_return(runtime: Path) -> None:
    expected = {
        "PRNINS.STA": b"PRINTER_INSTALL_REQUESTED\r\n",
        "PRNINS.OK": b"PRINTER_INSTALL_RETURNED_ZERO\r\n",
    }
    for name, payload in expected.items():
        path = runtime / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise OracleError(
                f"printer install sentinel is invalid: {name}",
                exit_code=EXIT_BACKEND,
            )
    if (runtime / "PRNINS.ERR").exists() or (runtime / "PRNINS.ERR").is_symlink():
        raise OracleError("printer install reported a guest error", exit_code=EXIT_BACKEND)


def _read_ini(path: Path) -> configparser.RawConfigParser:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise OracleError(
            f"printer configuration file is unsafe: {path.name}",
            exit_code=EXIT_INTEGRITY,
        )
    parser = configparser.RawConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(path.read_text(encoding="latin-1"))
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise OracleError(
            f"printer configuration file is invalid: {path.name}",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    return parser


def _validate_printer_runtime(runtime: Path) -> tuple[dict[str, Any], dict[str, object]]:
    tree = install_module._validate_installed_amipro(runtime)
    expected_files = {
        "PSCRIPT.DRV": (312_848, PSCRIPT_DRV_SHA256),
        "PSCRIPT.HLP": (43_793, PSCRIPT_HLP_SHA256),
        "TESTPS.TXT": (2_640, TESTPS_TXT_SHA256),
    }
    system = runtime / "WINDOWS" / "SYSTEM"
    files: dict[str, dict[str, object]] = {}
    for name, (size, expected_hash) in expected_files.items():
        path = system / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            raise OracleError(f"installed printer file is invalid: {name}", exit_code=EXIT_BACKEND)
        digest = sha256_file(path)
        if digest != expected_hash:
            raise OracleError(
                f"installed printer file hash mismatch: {name}",
                exit_code=EXIT_INTEGRITY,
            )
        files[name] = {"size": size, "sha256": digest}
    win = _read_ini(runtime / "WINDOWS" / "WIN.INI")
    control = _read_ini(runtime / "WINDOWS" / "CONTROL.INI")
    try:
        values = {
            "spooler": win.get("windows", "spooler"),
            "default_device": win.get("windows", "device"),
            "printer_port": win.get("PrinterPorts", PRINTER_MODEL),
            "device": win.get("devices", PRINTER_MODEL),
            "postscript_atm": win.get("PostScript,LPT1", "ATM"),
            "installed_driver": control.get("installed", "PSCRIPT.DRV"),
            "installed_help": control.get("installed", "PSCRIPT.HLP"),
            "installed_test": control.get("installed", "TESTPS.TXT"),
        }
    except (configparser.Error, KeyError) as exc:
        raise OracleError(
            "printer INI configuration is incomplete",
            exit_code=EXIT_BACKEND,
        ) from exc
    expected_values = {
        "spooler": "no",
        "default_device": f"{PRINTER_MODEL},pscript,{PRINTER_PORT}",
        "printer_port": f"pscript,{PRINTER_PORT},15,90",
        "device": f"pscript,{PRINTER_PORT}",
        "postscript_atm": "placeholder",
        "installed_driver": "yes",
        "installed_help": "yes",
        "installed_test": "yes",
    }
    if values != expected_values:
        raise OracleError("printer INI configuration mismatch", exit_code=EXIT_BACKEND)
    identity: dict[str, object] = {
        "profile": PRINTER_PROFILE["name"],
        "model": PRINTER_MODEL,
        "port": PRINTER_PORT,
        "print_manager": False,
        "files": files,
        "ini_values": values,
        "win_ini_sha256": sha256_file(runtime / "WINDOWS" / "WIN.INI"),
        "control_ini_sha256": sha256_file(runtime / "WINDOWS" / "CONTROL.INI"),
    }
    return tree, identity


def _remove_controls(runtime: Path) -> None:
    for name in ("PRNINS.BAT", "PRNINS.STA", "PRNINS.OK", "PRNINS.ERR"):
        path = runtime / name
        if path.is_symlink():
            raise OracleError("printer control path became a symlink", exit_code=EXIT_INTEGRITY)
        path.unlink(missing_ok=True)


def _write_attempt(job: Path, inputs: dict[str, object], machine: StateMachine) -> None:
    atomic_write_json(
        job / "attempt.json",
        {
            "schema": "amipro-oracle-printer-install-attempt-v1",
            "phase": "printer-install",
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
        "schema": PRINTER_INSTALL_RESULT_SCHEMA,
        "status": runtime["status"],
        "runtime_key": runtime["runtime_key"],
        "parent_runtime_key": runtime["parent_runtime_key"],
        "cache_reused": cache_reused,
        "evidence_job": evidence_job,
        "runtime": runtime,
    }


def _load_evidence(home: Path, root: Path, key: str, runtime: dict[str, Any]) -> str:
    receipt_path = root / "evidence-receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise OracleError("printer-ready evidence receipt is missing", exit_code=EXIT_INTEGRITY)
    try:
        receipt = read_json_object(receipt_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "printer-ready evidence receipt is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    job_name = receipt.get("evidence_job")
    result_hash = receipt.get("result_sha256")
    if (
        set(receipt) != {"schema", "runtime_key", "evidence_job", "result_sha256"}
        or receipt.get("schema") != "amipro-oracle-printer-ready-evidence-v1"
        or receipt.get("runtime_key") != key
        or not isinstance(job_name, str)
        or re.fullmatch(r"install-printer-[a-z0-9_-]+", job_name) is None
        or not isinstance(result_hash, str)
        or _SHA256.fullmatch(result_hash) is None
    ):
        raise OracleError("printer-ready evidence receipt mismatch", exit_code=EXIT_INTEGRITY)
    job = home / "jobs" / job_name
    result_path = job / "result.json"
    if (
        job.is_symlink()
        or not job.is_dir()
        or result_path.is_symlink()
        or not result_path.is_file()
        or sha256_file(result_path) != result_hash
    ):
        raise OracleError("printer-ready evidence result mismatch", exit_code=EXIT_INTEGRITY)
    try:
        recorded = read_json_object(result_path)
        observer = _validate_observer_evidence(job / "diagnostics")
        driver = _validate_ui_evidence(job)
    except (OSError, ValueError, OracleError) as exc:
        raise OracleError("printer-ready evidence is invalid", exit_code=EXIT_INTEGRITY) from exc
    process = recorded.get("process_result")
    trace = recorded.get("state_trace")
    if (
        recorded.get("schema") != PRINTER_INSTALL_RESULT_SCHEMA
        or recorded.get("status") != "printer-ready"
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
        or [event.get("state") for event in trace if isinstance(event, dict)]
        != ["created", "staged", "guest-invoked", "guest-returned", "validated"]
    ):
        raise OracleError("printer-ready evidence identity mismatch", exit_code=EXIT_INTEGRITY)
    return job_name


def _verify_cache(
    home: Path,
    root: Path,
    key: str,
    inputs: dict[str, object],
) -> tuple[dict[str, Any], str]:
    paths = {
        "runtime": root / "runtime.json",
        "inputs": root / "inputs.json",
        "printer_tree": root / "printer-tree.json",
        "sealed_tree": root / "sealed-tree.json",
        "config": root / "dosbox-x.conf",
        "batch": root / "PRNINS.BAT",
        "receipt": root / "evidence-receipt.json",
    }
    pristine = root / "pristine-c"
    expected_names = {
        "PRNINS.BAT",
        "dosbox-x.conf",
        "evidence-receipt.json",
        "inputs.json",
        "printer-tree.json",
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
        raise OracleError("printer-ready cache has an unsafe shape", exit_code=EXIT_INTEGRITY)
    try:
        runtime = read_json_object(paths["runtime"])
        recorded_inputs = read_json_object(paths["inputs"])
        printer_tree = read_json_object(paths["printer_tree"])
        recorded_sealed = read_json_object(paths["sealed_tree"])
        config_bytes = paths["config"].read_bytes()
        batch_bytes = paths["batch"].read_bytes()
    except (OSError, ValueError) as exc:
        raise OracleError(
            "printer-ready cache manifest is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    sealed, printer = _validate_printer_runtime(pristine)
    expected_printer_tree = install_module._unsealed_tree(sealed)
    expected_keys = {
        "backend",
        "baseline_eligible",
        "checkpoint_role",
        "inputs_digest",
        "parent_runtime_key",
        "printer_identity",
        "printer_profile",
        "printer_tree_digest",
        "printer_tree_manifest_digest",
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
        or runtime.get("schema") != PRINTER_READY_SCHEMA
        or runtime.get("runtime_schema") != RUNTIME_SCHEMA
        or runtime.get("backend") != "real"
        or runtime.get("baseline_eligible") is not False
        or runtime.get("status") != "printer-ready"
        or runtime.get("checkpoint_role") != "base-for-one-file-postscript-smoke"
        or runtime.get("runtime_key") != key
        or runtime.get("inputs_digest") != digest_json(inputs)
        or key != digest_json(inputs)
        or runtime.get("parent_runtime_key")
        != inputs.get("amipro_ready", {}).get("runtime_key")
        or runtime.get("printer_profile") != PRINTER_PROFILE["name"]
        or runtime.get("printer_identity") != printer
        or recorded_inputs != inputs
        or config_bytes != printer_install_config().encode("utf-8")
        or batch_bytes != printer_install_batch()
        or hashlib.sha256(config_bytes).hexdigest() != inputs.get("dosbox_config_sha256")
        or hashlib.sha256(batch_bytes).hexdigest() != inputs.get("install_batch_sha256")
        or printer_tree != expected_printer_tree
        or printer_tree.get("digest") != runtime.get("printer_tree_digest")
        or digest_json(printer_tree) != runtime.get("printer_tree_manifest_digest")
        or recorded_sealed != sealed
        or sealed.get("digest") != runtime.get("sealed_tree_digest")
        or digest_json(sealed) != runtime.get("sealed_tree_manifest_digest")
        or sealed.get("file_count") != runtime.get("tree_file_count")
        or sealed.get("directory_count") != runtime.get("tree_directory_count")
        or sealed.get("total_bytes") != runtime.get("tree_total_bytes")
        or any(int(str(entry["mode"]), 8) & 0o222 for entry in sealed["entries"])
    ):
        raise OracleError("printer-ready cache identity mismatch", exit_code=EXIT_INTEGRITY)
    return runtime, _load_evidence(home, root, key, runtime)


def install_printer_ready(
    home: Path,
    media_root: Path,
    windows_media: dict[str, Any],
    image_record: dict[str, Any],
    *,
    runtime_key: str | None = None,
    timeout_seconds: float = OUTER_TIME_LIMIT_SECONDS,
) -> dict[str, Any]:
    _require_verified_image(image_record)
    _ensure_private_directories(home)
    source_root, ready, _ready_inputs, _ready_evidence = smoke_module._select_ready_runtime(
        home,
        runtime_key,
    )
    flat_source, flat_media = ensure_flat_windows_media(home, media_root, windows_media)
    inputs = printer_install_inputs(
        ready,
        windows_media,
        flat_media,
        image_record,
        outer_time_limit_seconds=timeout_seconds,
    )
    key = digest_json(inputs)
    parent_key = str(ready["runtime_key"])
    flat_key = str(flat_media["cache_key"])
    ready_parent = home / "cache" / "printer-ready"
    if ready_parent.is_symlink():
        raise OracleError("printer-ready cache parent is unsafe", exit_code=EXIT_INTEGRITY)
    if not ready_parent.exists():
        ready_parent.mkdir(mode=0o700)
    elif not ready_parent.is_dir():
        raise OracleError("printer-ready cache parent is not a directory", exit_code=EXIT_INTEGRITY)
    ready_parent.chmod(0o700)
    final = ready_parent / key
    with _cache_lock(home, parent_key), _cache_lock(home, flat_key), _cache_lock(home, key):
        source_root, checked_ready, _checked_inputs, _checked_evidence = (
            smoke_module._select_ready_runtime(home, parent_key)
        )
        checked_flat = bootstrap_module._verify_flat_media_cache(
            flat_source.parent,
            expected_key=flat_key,
            expected_identity=bootstrap_module._media_cache_identity(windows_media),
        )
        checked = printer_install_inputs(
            checked_ready,
            windows_media,
            checked_flat,
            image_record,
            outer_time_limit_seconds=timeout_seconds,
        )
        if checked != inputs:
            raise OracleError("printer inputs changed after keying", exit_code=EXIT_INTEGRITY)
        if final.exists() or final.is_symlink():
            runtime, evidence_job = _verify_cache(home, final, key, inputs)
            return _result(runtime, cache_reused=True, evidence_job=evidence_job)

        job = Path(tempfile.mkdtemp(prefix=f"install-printer-{key[:12]}-", dir=home / "jobs"))
        job.chmod(0o700)
        _directory_fsync(home / "jobs")
        for name in ("capture", "diagnostics", "home"):
            (job / name).mkdir(mode=0o700)
        runtime_root = job / "runtime"
        shutil.copytree(source_root / "pristine-c", runtime_root, copy_function=shutil.copy2)
        _normalize_runtime_metadata(runtime_root)
        copied = install_module._validate_installed_amipro(runtime_root)
        if copied["digest"] != ready["launch_tree_digest"]:
            raise OracleError(
                "printer runtime copy does not match its parent",
                exit_code=EXIT_INTEGRITY,
            )
        config_bytes = printer_install_config().encode("utf-8")
        batch_bytes = printer_install_batch()
        atomic_write(runtime_root / "PRNINS.BAT", batch_bytes)
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
            container_name=f"amipro-oracle-printer-{suffix}",
            oracle_root=home,
            job_root=job,
            control_root=control,
            phase="bootstrap",
            mounts=[
                BindMount(job, "/oracle/job", read_only=False),
                BindMount(flat_source, "/oracle/media/windows", read_only=True),
            ],
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
            process, ui_driver = _invoke_job(invocation, job, timeout_seconds=timeout_seconds)
            if (
                process.get("exit_code") != 0
                or process.get("timed_out") is not False
                or process.get("killed") is not False
            ):
                error = OracleError(
                    "printer container did not exit cleanly",
                    exit_code=EXIT_BACKEND,
                )
                error.process_result = process
                raise error
            observer = _validate_observer_evidence(job / "diagnostics")
            validated_driver = _validate_ui_evidence(job)
            if ui_driver != validated_driver:
                raise OracleError("printer UI evidence changed", exit_code=EXIT_INTEGRITY)
            _validate_return(runtime_root)
            machine.advance("guest-returned", evidence="PRNINS.OK")
            _write_attempt(job, inputs, machine)
            smoke_module._select_ready_runtime(home, parent_key)
            bootstrap_module._verify_flat_media_cache(
                flat_source.parent,
                expected_key=flat_key,
                expected_identity=bootstrap_module._media_cache_identity(windows_media),
            )
            raw_tree, raw_printer = _validate_printer_runtime(runtime_root)
            atomic_write_json(job / "raw-tree.json", raw_tree)
            atomic_write_json(job / "printer-identity.json", raw_printer)
            _remove_controls(runtime_root)
            _normalize_runtime_metadata(runtime_root)
            printer_tree, printer_identity = _validate_printer_runtime(runtime_root)
            atomic_write_json(job / "printer-tree.json", printer_tree)
            machine.advance("validated", evidence="printer-tree.json")
            _write_attempt(job, inputs, machine)

            promotion = Path(
                tempfile.mkdtemp(prefix=f".{key}.", suffix=".staging", dir=ready_parent)
            )
            promotion.chmod(0o700)
            shutil.copytree(
                runtime_root,
                promotion / "pristine-c",
                copy_function=shutil.copy2,
            )
            _make_tree_read_only(promotion / "pristine-c")
            sealed_tree, sealed_printer = _validate_printer_runtime(promotion / "pristine-c")
            if sealed_printer != printer_identity:
                raise OracleError(
                    "printer identity changed while sealing",
                    exit_code=EXIT_INTEGRITY,
                )
            manifest: dict[str, Any] = {
                "schema": PRINTER_READY_SCHEMA,
                "runtime_schema": RUNTIME_SCHEMA,
                "backend": "real",
                "baseline_eligible": False,
                "status": "printer-ready",
                "checkpoint_role": "base-for-one-file-postscript-smoke",
                "runtime_key": key,
                "parent_runtime_key": parent_key,
                "inputs_digest": digest_json(inputs),
                "printer_profile": PRINTER_PROFILE["name"],
                "printer_identity": printer_identity,
                "printer_tree_digest": printer_tree["digest"],
                "printer_tree_manifest_digest": digest_json(printer_tree),
                "sealed_tree_digest": sealed_tree["digest"],
                "sealed_tree_manifest_digest": digest_json(sealed_tree),
                "tree_file_count": sealed_tree["file_count"],
                "tree_directory_count": sealed_tree["directory_count"],
                "tree_total_bytes": sealed_tree["total_bytes"],
            }
            atomic_write_json(promotion / "runtime.json", manifest)
            atomic_write_json(promotion / "inputs.json", inputs)
            atomic_write_json(promotion / "printer-tree.json", printer_tree)
            atomic_write_json(promotion / "sealed-tree.json", sealed_tree)
            atomic_write(promotion / "dosbox-x.conf", config_bytes)
            atomic_write(promotion / "PRNINS.BAT", batch_bytes)
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
                    "schema": "amipro-oracle-printer-ready-evidence-v1",
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
            verified, evidence_job = _verify_cache(home, final, key, inputs)
            machine.advance("ready", evidence=f"cache/printer-ready/{key}/runtime.json")
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
                    promotion_evidence = f"cache/printer-ready/{promotion.name}"
            if machine.state != "failed" and "failed" in machine.transitions.get(
                machine.state,
                frozenset(),
            ):
                machine.advance("failed", evidence="failure.json")
            failure: dict[str, object] = {
                "schema": "amipro-oracle-printer-install-failure-v1",
                "phase": "printer-install",
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
