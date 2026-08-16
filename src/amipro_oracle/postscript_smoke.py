from __future__ import annotations

import hashlib
import math
import re
import shutil
import stat
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from . import amipro_install as install_module
from . import amipro_launch_probe as launch_module
from . import document_smoke as document_module
from . import oci as oci_module
from . import printer_install as printer_module
from . import process as process_module
from .config import DOSBOX_PROFILE, dosbox_config
from .constants import (
    ANALYSIS_SCHEMA,
    EXIT_BACKEND,
    EXIT_INTEGRITY,
    EXIT_MISSING,
    EXIT_USAGE,
    JOB_SCHEMA,
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
from .raster import decode_png
from .state import StateMachine
from .windows_bootstrap import (
    GUEST_DATE,
    GUEST_TIME,
    WINDOWS_FREE_MB,
    _cache_lock,
    _directory_fsync,
    _ensure_private_directories,
    _normalize_runtime_metadata,
    _require_verified_image,
    _validate_observer_evidence,
)

POSTSCRIPT_SMOKE_INPUT_SCHEMA = "amipro-oracle-postscript-smoke-input-v1"
POSTSCRIPT_SMOKE_RESULT_SCHEMA = "amipro-oracle-postscript-smoke-result-v1"
POSTSCRIPT_SMOKE_UI_SCHEMA = "amipro-oracle-postscript-smoke-driver-v1"
POSTSCRIPT_TRANSFORM_SCHEMA = "amipro-oracle-postscript-transform-v1"
INNER_TIME_LIMIT_SECONDS = 110
OUTER_TIME_LIMIT_SECONDS = 130
UI_DRIVER_TIMEOUT_SECONDS = 105
TOOL_TIME_LIMIT_SECONDS = 30
MAX_POSTSCRIPT_BYTES = 16 * 1024 * 1024
MAX_DERIVED_BYTES = 32 * 1024 * 1024
RASTER_DPI = 144

PRINT_DIALOG_STATE = {
    "name": "amipro-print-dialog",
    # Exclude the dialog's one-pixel outer border: it can contain document pixels.
    "box": [323, 283, 703, 310],
    "title_sha256": "b0357f1478f331967d808b552322f497a8aff80945631df904829b7312d129fa",
}
EXPECTED_BOUNDING_BOX = [14, 91, 582, 782]
EXPECTED_TEXT = "NATIVE SMOKE DOCUMENT\nINVENTED CONTENT ONLY"
EXPECTED_WORDS = (
    "NATIVE",
    "SMOKE",
    "DOCUMENT",
    "INVENTED",
    "CONTENT",
    "ONLY",
)
ANALYSIS_PROFILE = {
    "id": "amipro31-pscript35-qms100-a4-poppler144-v1",
    "page_width_pt": 595.0,
    "page_height_pt": 842.0,
    "postscript_bounding_box": EXPECTED_BOUNDING_BOX,
    "raster_dpi": RASTER_DPI,
    "raster_width": 1190,
    "raster_height": 1684,
    "whitespace": "exact",
    "ghostscript_device": "pdfwrite",
    "pdf_compatibility": "1.4",
    "poppler_renderer": "pdftocairo-png",
}
POSTSCRIPT_UI_PROFILE = {
    "name": "amipro-3.1-one-page-postscript-lifecycle-v1",
    "screen_width": install_module.SCREEN_WIDTH,
    "screen_height": install_module.SCREEN_HEIGHT,
    "autolock": False,
    "stable_samples": 2,
    "poll_seconds": 0.25,
    "states": [
        document_module.DOCUMENT_TITLE_STATE,
        PRINT_DIALOG_STATE,
        document_module.DOCUMENT_TITLE_STATE,
        launch_module.PROGRAM_MANAGER_MINIMIZED_STATE,
        install_module.EXIT_WINDOWS_STATE,
    ],
    "document_readiness": document_module.DOCUMENT_UI_PROFILE["document_readiness"],
    "actions": [
        "open-print-dialog",
        "confirm-default-print",
        "wait-for-lpt-closure",
        "close-document-and-amipro",
        "exit-windows",
        "confirm-exit-windows",
    ],
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def postscript_smoke_config() -> str:
    config = dosbox_config(
        runtime_free_mb=WINDOWS_FREE_MB,
        autoexec=(
            "COUNTRY 1",
            f"DATE {GUEST_DATE}",
            f"TIME {GUEST_TIME}",
            r"Z:\CONFIG.COM -SECUREMODE",
            r"C:\PRTSMK.BAT",
        ),
    )
    return config.replace("autolock=true", "autolock=false")


def postscript_smoke_batch() -> bytes:
    lines = (
        "@ECHO OFF",
        r"IF EXIST C:\PRTSMK.STA DEL C:\PRTSMK.STA",
        r"IF EXIST C:\PRTSMK.OK DEL C:\PRTSMK.OK",
        r"IF EXIST C:\PRTSMK.ERR DEL C:\PRTSMK.ERR",
        r"IF NOT EXIST C:\AMIPRO\AMIPRO.EXE GOTO AMIPRO_MISSING",
        r"IF NOT EXIST C:\ORACLE\SMOKE.SAM GOTO DOCUMENT_MISSING",
        r"ECHO POSTSCRIPT_LAUNCH_REQUESTED>C:\PRTSMK.STA",
        r"C:\WINDOWS\WIN.COM C:\AMIPRO\AMIPRO.EXE C:\ORACLE\SMOKE.SAM",
        "IF ERRORLEVEL 1 GOTO DOCUMENT_FAILED",
        r"ECHO POSTSCRIPT_RETURNED_ZERO>C:\PRTSMK.OK",
        "GOTO DOCUMENT_DONE",
        ":AMIPRO_MISSING",
        r"ECHO AMIPRO_EXE_MISSING>C:\PRTSMK.ERR",
        "GOTO DOCUMENT_DONE",
        ":DOCUMENT_MISSING",
        r"ECHO SMOKE_DOCUMENT_MISSING>C:\PRTSMK.ERR",
        "GOTO DOCUMENT_DONE",
        ":DOCUMENT_FAILED",
        r"ECHO DOCUMENT_ERRORLEVEL_NONZERO>C:\PRTSMK.ERR",
        ":DOCUMENT_DONE",
        "EXIT",
    )
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def _source_fingerprints() -> dict[str, str]:
    modules = {
        "amipro_install": Path(install_module.__file__),
        "amipro_launch_probe": Path(launch_module.__file__),
        "document_smoke": Path(document_module.__file__),
        "oci": Path(oci_module.__file__),
        "postscript_smoke": Path(__file__),
        "printer_install": Path(printer_module.__file__),
        "process": Path(process_module.__file__),
    }
    return {name: sha256_file(path) for name, path in sorted(modules.items())}


def _select_printer_runtime(
    home: Path,
    runtime_key: str | None,
) -> tuple[Path, dict[str, Any], dict[str, object], str]:
    parent = home / "cache" / "printer-ready"
    if parent.is_symlink() or not parent.is_dir():
        raise OracleError("printer-ready cache is missing", exit_code=EXIT_MISSING)
    if runtime_key is None:
        candidates: list[str] = []
        for path in sorted(parent.iterdir(), key=lambda item: item.name):
            if _SHA256.fullmatch(path.name) is None:
                continue
            if path.is_symlink() or not path.is_dir():
                raise OracleError("unsafe printer-ready cache entry", exit_code=EXIT_INTEGRITY)
            candidates.append(path.name)
        if not candidates:
            raise OracleError("run install-printer before print-smoke", exit_code=EXIT_MISSING)
        if len(candidates) != 1:
            raise OracleError(
                "multiple printer-ready runtimes exist; pass --runtime-key",
                exit_code=EXIT_USAGE,
            )
        runtime_key = candidates[0]
    if _SHA256.fullmatch(runtime_key) is None:
        raise OracleError("invalid --runtime-key", exit_code=EXIT_USAGE)
    root = parent / runtime_key
    inputs_path = root / "inputs.json"
    if inputs_path.is_symlink() or not inputs_path.is_file():
        raise OracleError("printer-ready inputs are missing", exit_code=EXIT_INTEGRITY)
    try:
        inputs = read_json_object(inputs_path)
    except (OSError, ValueError) as exc:
        raise OracleError("printer-ready inputs are invalid", exit_code=EXIT_INTEGRITY) from exc
    runtime, evidence_job = printer_module._verify_cache(
        home,
        root,
        runtime_key,
        inputs,
    )
    return root, runtime, inputs, evidence_job


def postscript_smoke_inputs(
    ready: dict[str, Any],
    fixture: dict[str, object],
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
            f"PostScript smoke timeout must be between 1 and {OUTER_TIME_LIMIT_SECONDS} seconds",
            exit_code=EXIT_USAGE,
        )
    image_id = image_record.get("image_id")
    image_digest = image_record.get("image_digest")
    lock_hash = image_record.get("lock_sha256")
    if (
        ready.get("schema") != printer_module.PRINTER_READY_SCHEMA
        or ready.get("status") != "printer-ready"
        or not isinstance(ready.get("runtime_key"), str)
        or _SHA256.fullmatch(str(ready["runtime_key"])) is None
        or fixture.get("schema") != document_module.TEXT_FIXTURE_SCHEMA
        or not isinstance(fixture.get("sha256"), str)
        or _SHA256.fullmatch(str(fixture["sha256"])) is None
        or not isinstance(image_id, str)
        or _SHA256.fullmatch(image_id) is None
        or not isinstance(image_digest, str)
        or _IMAGE_DIGEST.fullmatch(image_digest) is None
        or not isinstance(lock_hash, str)
        or _SHA256.fullmatch(lock_hash) is None
        or image_record.get("platform") != "linux/amd64"
    ):
        raise OracleError("invalid PostScript smoke input identity", exit_code=EXIT_INTEGRITY)
    config = postscript_smoke_config().encode("utf-8")
    batch = postscript_smoke_batch()
    return {
        "schema": POSTSCRIPT_SMOKE_INPUT_SCHEMA,
        "printer_ready": {
            "runtime_key": ready["runtime_key"],
            "manifest_digest": digest_json(ready),
            "sealed_tree_digest": ready["sealed_tree_digest"],
            "printer_identity_digest": digest_json(ready["printer_identity"]),
        },
        "fixture": fixture,
        "toolchain": {
            "image_id": image_id,
            "image_digest": image_digest,
            "lock_sha256": lock_hash,
            "platform": "linux/amd64",
        },
        "driver_profile": POSTSCRIPT_UI_PROFILE,
        "analysis_profile": ANALYSIS_PROFILE,
        "dosbox_profile": DOSBOX_PROFILE,
        "dosbox_config_sha256": hashlib.sha256(config).hexdigest(),
        "smoke_batch_sha256": hashlib.sha256(batch).hexdigest(),
        "orchestrator_sha256": _source_fingerprints(),
        "guest_clock": {"date_command": GUEST_DATE, "time_command": GUEST_TIME},
        "reported_free_mb": WINDOWS_FREE_MB,
        "inner_time_limit_seconds": INNER_TIME_LIMIT_SECONDS,
        "outer_time_limit_seconds": outer_time_limit_seconds,
        "tool_time_limit_seconds": TOOL_TIME_LIMIT_SECONDS,
    }


def _wait_sentinel(runtime: Path, stop: threading.Event, deadline: float) -> None:
    sentinel = runtime / "PRTSMK.STA"
    while monotonic() < deadline and not stop.is_set():
        if (
            sentinel.is_file()
            and not sentinel.is_symlink()
            and sentinel.read_bytes() == b"POSTSCRIPT_LAUNCH_REQUESTED\r\n"
        ):
            return
        sleep(0.1)
    raise OracleError("PostScript smoke sentinel was not observed", exit_code=EXIT_BACKEND)


def _capture_exact_state(
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


def _capture_document_state(
    job: Path,
    filename: str,
    *,
    stop: threading.Event,
    deadline: float,
) -> dict[str, object]:
    evidence, payload = document_module._wait_document_state(
        job / "diagnostics" / "screen-last.png",
        stop=stop,
        deadline=deadline,
    )
    atomic_write(job / "diagnostics" / filename, payload)
    evidence["path"] = filename
    return evidence


def _capture_files(capture: Path) -> list[Path]:
    if capture.is_symlink() or not capture.is_dir():
        raise OracleError("LPT capture directory is unsafe", exit_code=EXIT_INTEGRITY)
    paths: list[Path] = []
    for entry in capture.iterdir():
        info = entry.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OracleError("LPT capture contains an unsafe entry", exit_code=EXIT_INTEGRITY)
        paths.append(entry)
    return sorted(paths, key=lambda item: item.name)


def _wait_capture_closed(
    job: Path,
    stop: threading.Event,
    deadline: float,
    *,
    maximum: int = MAX_POSTSCRIPT_BYTES,
) -> dict[str, object]:
    capture = job / "capture"
    log = job / "diagnostics" / "container.stderr.log"
    signature: tuple[str, int, int] | None = None
    stable_since: float | None = None
    while monotonic() < deadline and not stop.is_set():
        try:
            paths = _capture_files(capture)
            closed = b"Parallel 1: File closed." in log.read_bytes()
            if len(paths) == 1 and closed:
                info = paths[0].stat()
                current = (paths[0].name, info.st_size, info.st_mtime_ns)
                if current != signature:
                    signature = current
                    stable_since = monotonic()
                elif stable_since is not None and monotonic() - stable_since >= 3:
                    if not 1 <= info.st_size <= maximum:
                        raise OracleError(
                            "PostScript capture size is outside its bound",
                            exit_code=EXIT_INTEGRITY,
                        )
                    return {
                        "path": paths[0].name,
                        "size": info.st_size,
                        "stable_seconds": 3,
                        "lpt_close_observed": True,
                    }
            else:
                signature = None
                stable_since = None
        except FileNotFoundError:
            signature = None
            stable_since = None
        sleep(0.25)
    raise OracleError("one stable closed LPT capture was not observed", exit_code=EXIT_BACKEND)


def _drive_print_lifecycle(
    invocation: PodmanInvocation,
    job: Path,
    stop: threading.Event,
) -> dict[str, object]:
    deadline = monotonic() + UI_DRIVER_TIMEOUT_SECONDS
    _wait_sentinel(job / "runtime", stop, deadline)
    before = _capture_document_state(
        job,
        "document-before-print.png",
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

    def press(action: str, key: str) -> None:
        result = exec_podman_checked(
            invocation,
            ("xdotool", "key", "--window", window, key),
            environment={"DISPLAY": ":99"},
        )
        if result["exit_code"] != 0:
            raise OracleError(f"cannot perform UI action: {action}", exit_code=EXIT_BACKEND)
        actions.append({"action": action, "key": key, "exit_code": 0})

    press("open-print-dialog", "ctrl+p")
    dialog = _capture_exact_state(
        job,
        PRINT_DIALOG_STATE,
        "print-dialog.png",
        stop=stop,
        deadline=deadline,
    )
    press("confirm-default-print", "Return")
    capture = _wait_capture_closed(job, stop, deadline)
    actions.append({"action": "wait-for-lpt-closure", **capture})
    after = _capture_document_state(
        job,
        "document-after-print.png",
        stop=stop,
        deadline=deadline,
    )
    press("close-document-and-amipro", "alt+F4")
    minimized = _capture_exact_state(
        job,
        launch_module.PROGRAM_MANAGER_MINIMIZED_STATE,
        "print-program-manager-minimized.png",
        stop=stop,
        deadline=deadline,
    )
    press("exit-windows", "alt+F4")
    confirmation = _capture_exact_state(
        job,
        install_module.EXIT_WINDOWS_STATE,
        "print-exit-windows-confirmation.png",
        stop=stop,
        deadline=deadline,
    )
    press("confirm-exit-windows", "Return")
    return {
        "schema": POSTSCRIPT_SMOKE_UI_SCHEMA,
        "status": "success",
        "profile": POSTSCRIPT_UI_PROFILE,
        "states": [before, dialog, after, minimized, confirmation],
        "actions": actions,
    }


def _invoke_guest(
    invocation: PodmanInvocation,
    job: Path,
    *,
    timeout_seconds: float,
    lifecycle: Callable[
        [PodmanInvocation, Path, threading.Event], dict[str, object]
    ] = _drive_print_lifecycle,
) -> tuple[dict[str, object], dict[str, object]]:
    stop = threading.Event()
    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["result"] = lifecycle(invocation, job, stop)
        except BaseException as exc:
            box["result"] = {
                "schema": POSTSCRIPT_SMOKE_UI_SCHEMA,
                "status": "failure",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    driver = threading.Thread(target=worker, name="postscript-smoke-driver", daemon=True)
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
        "schema": POSTSCRIPT_SMOKE_UI_SCHEMA,
        "status": "failure",
        "error_type": "DriverThreadError",
        "error": "PostScript smoke driver did not return evidence",
    }
    if driver.is_alive():
        driver_result = {
            "schema": POSTSCRIPT_SMOKE_UI_SCHEMA,
            "status": "failure",
            "error_type": "DriverThreadError",
            "error": "PostScript smoke driver thread did not stop",
        }
    atomic_write_json(job / "ui-driver.json", driver_result)
    if process_error is not None:
        if isinstance(process_error, OracleError):
            process_error.ui_driver = driver_result
        raise process_error
    if process is None:
        raise OracleError("PostScript smoke process did not return", exit_code=EXIT_BACKEND)
    if driver_result.get("status") != "success":
        error = OracleError(
            f"PostScript smoke driver failed: {driver_result.get('error', 'unknown error')}",
            exit_code=EXIT_BACKEND,
        )
        error.process_result = process
        error.ui_driver = driver_result
        raise error
    return process, driver_result


def _validate_document_screenshot(path: Path) -> dict[str, object]:
    try:
        evidence, _ = document_module._document_state(path)
    except OracleError as exc:
        raise OracleError(
            "printed document screenshot is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    if not document_module._document_is_ready(evidence):
        raise OracleError("printed document screenshot is not ready", exit_code=EXIT_INTEGRITY)
    evidence["path"] = path.name
    return evidence


def _validate_ui_evidence(job: Path) -> dict[str, object]:
    path = job / "ui-driver.json"
    if path.is_symlink() or not path.is_file():
        raise OracleError("PostScript UI evidence is missing", exit_code=EXIT_INTEGRITY)
    try:
        driver = read_json_object(path)
    except (OSError, ValueError) as exc:
        raise OracleError("PostScript UI evidence is invalid", exit_code=EXIT_INTEGRITY) from exc
    actions = driver.get("actions")
    if (
        driver.get("schema") != POSTSCRIPT_SMOKE_UI_SCHEMA
        or driver.get("status") != "success"
        or driver.get("profile") != POSTSCRIPT_UI_PROFILE
        or not isinstance(actions, list)
        or len(actions) != 6
        or [item.get("action") for item in actions if isinstance(item, dict)]
        != POSTSCRIPT_UI_PROFILE["actions"]
    ):
        raise OracleError("PostScript UI evidence mismatch", exit_code=EXIT_INTEGRITY)
    before = _validate_document_screenshot(job / "diagnostics" / "document-before-print.png")
    after = _validate_document_screenshot(job / "diagnostics" / "document-after-print.png")
    exact_states: list[dict[str, object]] = []
    for state, name in (
        (PRINT_DIALOG_STATE, "print-dialog.png"),
        (launch_module.PROGRAM_MANAGER_MINIMIZED_STATE, "print-program-manager-minimized.png"),
        (install_module.EXIT_WINDOWS_STATE, "print-exit-windows-confirmation.png"),
    ):
        try:
            observed, _ = install_module._screen_state(job / "diagnostics" / name, state)
        except OracleError as exc:
            raise OracleError(
                "PostScript lifecycle screenshot is invalid",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        observed["path"] = name
        exact_states.append(observed)
    observed_states = [before, exact_states[0], after, exact_states[1], exact_states[2]]
    if driver.get("states") != observed_states:
        raise OracleError("PostScript lifecycle screenshots changed", exit_code=EXIT_INTEGRITY)
    capture_action = actions[2]
    if (
        capture_action.get("lpt_close_observed") is not True
        or capture_action.get("stable_seconds") != 3
        or type(capture_action.get("size")) is not int
        or not 1 <= capture_action["size"] <= MAX_POSTSCRIPT_BYTES
    ):
        raise OracleError(
            "PostScript capture completion evidence is invalid",
            exit_code=EXIT_INTEGRITY,
        )
    return driver


def _validate_guest_return(runtime: Path, fixture: dict[str, object]) -> None:
    expected = {
        "PRTSMK.STA": b"POSTSCRIPT_LAUNCH_REQUESTED\r\n",
        "PRTSMK.OK": b"POSTSCRIPT_RETURNED_ZERO\r\n",
    }
    for name, payload in expected.items():
        path = runtime / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise OracleError(
                f"PostScript smoke sentinel is invalid: {name}",
                exit_code=EXIT_BACKEND,
            )
    if (runtime / "PRTSMK.ERR").exists() or (runtime / "PRTSMK.ERR").is_symlink():
        raise OracleError("PostScript smoke reported a guest error", exit_code=EXIT_BACKEND)
    source = runtime / "ORACLE" / "SMOKE.SAM"
    if (
        source.is_symlink()
        or not source.is_file()
        or source.stat().st_size != fixture["size"]
        or sha256_file(source) != fixture["sha256"]
    ):
        raise OracleError("staged print fixture changed in the guest", exit_code=EXIT_INTEGRITY)


def validate_postscript(payload: bytes) -> tuple[bytes, dict[str, object]]:
    if not 1 <= len(payload) <= MAX_POSTSCRIPT_BYTES:
        raise OracleError("raw PostScript size is outside its bound", exit_code=EXIT_INTEGRITY)
    leading_eot = payload.startswith(b"\x04")
    trailing_eot = payload.endswith(b"\x04")
    sanitized = payload[1:] if leading_eot else payload
    sanitized = sanitized[:-1] if trailing_eot else sanitized
    if b"\x04" in sanitized:
        raise OracleError("PostScript contains an interior EOT byte", exit_code=EXIT_INTEGRITY)
    if not sanitized.startswith(b"%!PS-Adobe-3.0\r\n") or not sanitized.endswith(
        b"%%EOF\r\n"
    ):
        raise OracleError("PostScript envelope is invalid", exit_code=EXIT_INTEGRITY)
    without_crlf = sanitized.replace(b"\r\n", b"")
    if b"\r" in without_crlf or b"\n" in without_crlf:
        raise OracleError(
            "PostScript line endings are not canonical CRLF",
            exit_code=EXIT_INTEGRITY,
        )
    try:
        text = sanitized.decode("ascii")
    except UnicodeDecodeError as exc:
        raise OracleError("PostScript is not bounded ASCII", exit_code=EXIT_INTEGRITY) from exc
    required = (
        "%%Creator: Windows PSCRIPT\r\n",
        "%%Title: Ami Pro - SMOKE.SAM\r\n",
        "%%Pages: (atend)\r\n",
        "%%Page: 1 1\r\n",
        "%%Pages: 1\r\n",
    )
    if any(value not in text for value in required) or text.count("%%Page: 1 1\r\n") != 1:
        raise OracleError("PostScript DSC identity is invalid", exit_code=EXIT_INTEGRITY)
    match = re.search(
        r"^%%BoundingBox: (-?\d+) (-?\d+) (-?\d+) (-?\d+)\r$",
        text,
        re.M,
    )
    if match is None or [int(value) for value in match.groups()] != EXPECTED_BOUNDING_BOX:
        raise OracleError("PostScript bounding box changed", exit_code=EXIT_INTEGRITY)
    identity = {
        "schema": POSTSCRIPT_TRANSFORM_SCHEMA,
        "raw_size": len(payload),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "leading_eot_removed": leading_eot,
        "trailing_eot_removed": trailing_eot,
        "sanitized_size": len(sanitized),
        "sanitized_sha256": hashlib.sha256(sanitized).hexdigest(),
        "dsc_version": "3.0",
        "creator": "Windows PSCRIPT",
        "title": "Ami Pro - SMOKE.SAM",
        "pages": 1,
        "bounding_box": EXPECTED_BOUNDING_BOX,
        "line_endings": "CRLF",
    }
    return sanitized, identity


def _run_tool(
    home: Path,
    image_record: dict[str, Any],
    job: Path,
    *,
    name: str,
    entrypoint: str,
    arguments: list[str],
) -> dict[str, object]:
    if re.fullmatch(r"[a-z0-9-]+", name) is None:
        raise OracleError("invalid analysis tool name", exit_code=EXIT_INTEGRITY)
    output = job / "output"
    control = home / "control" / job.name / name
    control.mkdir(mode=0o700, parents=True, exist_ok=True)
    if control.is_symlink() or not control.is_dir() or control.stat().st_mode & 0o077:
        raise OracleError("analysis tool control directory is unsafe", exit_code=EXIT_INTEGRITY)
    suffix = re.sub(r"[^a-z0-9]", "", job.name[-8:].casefold())
    invocation = build_podman_invocation(
        image_record,
        container_name=f"amipro-oracle-{name}-{suffix}",
        oracle_root=home,
        job_root=output,
        control_root=control,
        phase="document",
        mounts=[BindMount(output, "/oracle/job", read_only=False)],
        dosbox_arguments=arguments,
        entrypoint=entrypoint,
    )
    result = run_podman_bounded(
        invocation,
        stdout_path=job / "diagnostics" / f"{name}.stdout.log",
        stderr_path=job / "diagnostics" / f"{name}.stderr.log",
        cleanup_path=job / "diagnostics" / f"{name}.cleanup.json",
        timeout_seconds=TOOL_TIME_LIMIT_SECONDS,
    )
    if (
        result.get("exit_code") != 0
        or result.get("timed_out") is not False
        or result.get("killed") is not False
    ):
        error = OracleError(f"locked analysis tool failed: {name}", exit_code=EXIT_BACKEND)
        error.process_result = result
        raise error
    return result


def _bounded_regular(path: Path, label: str, maximum: int = MAX_DERIVED_BYTES) -> None:
    if (
        path.is_symlink()
        or not path.is_file()
        or not 1 <= path.stat().st_size <= maximum
    ):
        raise OracleError(f"derived {label} is unsafe", exit_code=EXIT_INTEGRITY)


def _parse_pdfinfo(payload: bytes) -> dict[str, object]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OracleError("pdfinfo output is invalid", exit_code=EXIT_INTEGRITY) from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    expected = {
        "Title": "Ami Pro - SMOKE.SAM",
        "Creator": "Windows PSCRIPT",
        "Producer": "GPL Ghostscript 10.00.0",
        "Pages": "1",
        "Page size": "595 x 842 pts (A4)",
        "Page rot": "0",
        "PDF version": "1.4",
        "Encrypted": "no",
        "JavaScript": "no",
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise OracleError("derived PDF identity changed", exit_code=EXIT_INTEGRITY)
    return expected


def _parse_pdffonts(payload: bytes) -> list[dict[str, object]]:
    try:
        lines = [line for line in payload.decode("utf-8").splitlines() if line.strip()]
    except UnicodeDecodeError as exc:
        raise OracleError("pdffonts output is invalid", exit_code=EXIT_INTEGRITY) from exc
    if len(lines) != 3:
        raise OracleError("derived PDF font inventory changed", exit_code=EXIT_INTEGRITY)
    fields = lines[2].split()
    if fields[:7] != ["[none]", "Type", "3", "Custom", "yes", "no", "no"]:
        raise OracleError("derived PDF font profile changed", exit_code=EXIT_INTEGRITY)
    return [
        {
            "name": None,
            "type": "Type 3",
            "encoding": "Custom",
            "embedded": True,
            "subset": False,
            "unicode_map": False,
        }
    ]


def _parse_bbox(path: Path) -> tuple[list[dict[str, object]], float, float]:
    _bounded_regular(path, "bounding-box XML", 1024 * 1024)
    try:
        root = ET.fromstring(path.read_bytes())
    except (ET.ParseError, OSError) as exc:
        raise OracleError("bounding-box XML is invalid", exit_code=EXIT_INTEGRITY) from exc
    namespace = {"x": "http://www.w3.org/1999/xhtml"}
    pages = root.findall(".//x:page", namespace)
    if len(pages) != 1:
        raise OracleError("bounding-box page count changed", exit_code=EXIT_INTEGRITY)
    try:
        width = float(pages[0].attrib["width"])
        height = float(pages[0].attrib["height"])
    except (KeyError, ValueError) as exc:
        raise OracleError(
            "bounding-box page geometry is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    if (
        not math.isfinite(width)
        or not math.isfinite(height)
        or (width, height) != (595.0, 842.0)
    ):
        raise OracleError("bounding-box page geometry changed", exit_code=EXIT_INTEGRITY)
    boxes: list[dict[str, object]] = []
    for word in pages[0].findall(".//x:word", namespace):
        text = word.text or ""
        try:
            coordinates = [float(word.attrib[name]) for name in ("xMin", "yMin", "xMax", "yMax")]
        except (KeyError, ValueError) as exc:
            raise OracleError("word bounding box is invalid", exit_code=EXIT_INTEGRITY) from exc
        if any(not math.isfinite(value) for value in coordinates):
            raise OracleError("word bounding box is non-finite", exit_code=EXIT_INTEGRITY)
        x0, y0, x1, y1 = coordinates
        if not 0 <= x0 < x1 <= width or not 0 <= y0 < y1 <= height:
            raise OracleError("word bounding box is outside its page", exit_code=EXIT_INTEGRITY)
        boxes.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1})
    if tuple(str(box["text"]) for box in boxes) != EXPECTED_WORDS:
        raise OracleError("derived PDF text boxes changed", exit_code=EXIT_INTEGRITY)
    return boxes, width, height


def _derive_outputs(
    home: Path,
    image_record: dict[str, Any],
    job: Path,
) -> tuple[dict[str, Any], dict[str, object]]:
    output = job / "output"
    tools: dict[str, object] = {}
    tools["ghostscript"] = _run_tool(
        home,
        image_record,
        job,
        name="ghostscript",
        entrypoint="/usr/bin/gs",
        arguments=[
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            "-dAutoRotatePages=/None",
            "-dOmitInfoDate=true",
            "-dOmitID=true",
            "-dOmitXMP=true",
            "-sOutputFile=/oracle/job/document.pdf",
            "/oracle/job/document.ps",
        ],
    )
    pdf = output / "document.pdf"
    _bounded_regular(pdf, "PDF")
    tools["pdfinfo"] = _run_tool(
        home,
        image_record,
        job,
        name="pdfinfo",
        entrypoint="/usr/bin/pdfinfo",
        arguments=["/oracle/job/document.pdf"],
    )
    pdfinfo_bytes = (job / "diagnostics" / "pdfinfo.stdout.log").read_bytes()
    atomic_write(output / "pdfinfo.txt", pdfinfo_bytes)
    pdf_identity = _parse_pdfinfo(pdfinfo_bytes)
    tools["pdffonts"] = _run_tool(
        home,
        image_record,
        job,
        name="pdffonts",
        entrypoint="/usr/bin/pdffonts",
        arguments=["/oracle/job/document.pdf"],
    )
    pdffonts_bytes = (job / "diagnostics" / "pdffonts.stdout.log").read_bytes()
    atomic_write(output / "pdffonts.txt", pdffonts_bytes)
    fonts = _parse_pdffonts(pdffonts_bytes)
    tools["pdftotext"] = _run_tool(
        home,
        image_record,
        job,
        name="pdftotext",
        entrypoint="/usr/bin/pdftotext",
        arguments=["/oracle/job/document.pdf", "/oracle/job/text.txt"],
    )
    text_path = output / "text.txt"
    _bounded_regular(text_path, "plain text", 1024 * 1024)
    expected_text_bytes = (EXPECTED_TEXT + "\n\n\f").encode("utf-8")
    if text_path.read_bytes() != expected_text_bytes:
        raise OracleError("derived PDF text changed", exit_code=EXIT_INTEGRITY)
    tools["bbox"] = _run_tool(
        home,
        image_record,
        job,
        name="bbox",
        entrypoint="/usr/bin/pdftotext",
        arguments=[
            "-bbox-layout",
            "/oracle/job/document.pdf",
            "/oracle/job/bbox.html",
        ],
    )
    boxes, width, height = _parse_bbox(output / "bbox.html")
    tools["raster"] = _run_tool(
        home,
        image_record,
        job,
        name="raster",
        entrypoint="/usr/bin/pdftocairo",
        arguments=[
            "-png",
            "-singlefile",
            "-r",
            str(RASTER_DPI),
            "/oracle/job/document.pdf",
            "/oracle/job/page-001",
        ],
    )
    png = output / "page-001.png"
    _bounded_regular(png, "PNG")
    try:
        raster_width, raster_height, _pixels = decode_png(png)
    except (OSError, ValueError) as exc:
        raise OracleError("derived PNG is invalid", exit_code=EXIT_INTEGRITY) from exc
    if (raster_width, raster_height) != (
        ANALYSIS_PROFILE["raster_width"],
        ANALYSIS_PROFILE["raster_height"],
    ):
        raise OracleError("derived PNG dimensions changed", exit_code=EXIT_INTEGRITY)
    analysis: dict[str, Any] = {
        "schema": ANALYSIS_SCHEMA,
        "backend": "real",
        "profile": ANALYSIS_PROFILE,
        "page_count": 1,
        "pages": [
            {
                "number": 1,
                "width_pt": width,
                "height_pt": height,
                "text": EXPECTED_TEXT,
                "text_boxes": boxes,
                "image_boxes": [],
                "raster": {
                    "path": png.name,
                    "width": raster_width,
                    "height": raster_height,
                },
            }
        ],
        "pdf_identity": pdf_identity,
        "fonts": fonts,
        "diagnostics": [
            "native Windows PSCRIPT output",
            "embedded unnamed Type 3 font; public redistribution not authorized",
            "initial measurement is not baseline eligible",
        ],
    }
    atomic_write_json(output / "analysis.json", analysis)
    return analysis, tools


def _artifact(job: Path, path: Path, *, kind: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise OracleError("job artifact is unsafe", exit_code=EXIT_INTEGRITY)
    return {
        "kind": kind,
        "path": path.relative_to(job).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _artifact_inventory(job: Path) -> list[dict[str, object]]:
    files = (
        ("output/document.raw.ps", "postscript-raw"),
        ("output/document.ps", "postscript"),
        ("output/postscript-transform.json", "postscript-transform"),
        ("output/document.pdf", "pdf"),
        ("output/pdfinfo.txt", "pdf-metadata"),
        ("output/pdffonts.txt", "font-inventory"),
        ("output/text.txt", "text"),
        ("output/bbox.html", "text-boxes"),
        ("output/page-001.png", "png"),
        ("output/analysis.json", "analysis"),
        ("ui-driver.json", "ui-evidence"),
        ("diagnostics/document-before-print.png", "ui-screenshot"),
        ("diagnostics/print-dialog.png", "ui-screenshot"),
        ("diagnostics/document-after-print.png", "ui-screenshot"),
        ("diagnostics/print-program-manager-minimized.png", "ui-screenshot"),
        ("diagnostics/print-exit-windows-confirmation.png", "ui-screenshot"),
        ("diagnostics/container.stdout.log", "diagnostic-log"),
        ("diagnostics/container.stderr.log", "diagnostic-log"),
        ("dosbox-x.conf", "configuration"),
        ("inputs.json", "inputs"),
    )
    return [_artifact(job, job / relative, kind=kind) for relative, kind in files]


def print_smoke(
    home: Path,
    image_record: dict[str, Any],
    source: Path,
    *,
    runtime_key: str | None = None,
    timeout_seconds: float = OUTER_TIME_LIMIT_SECONDS,
) -> dict[str, Any]:
    started = monotonic()
    payload, fixture = document_module.read_text_fixture(source)
    _require_verified_image(image_record)
    _ensure_private_directories(home)
    source_root, ready, _ready_inputs, _ready_evidence = _select_printer_runtime(
        home,
        runtime_key,
    )
    inputs = postscript_smoke_inputs(
        ready,
        fixture,
        image_record,
        outer_time_limit_seconds=timeout_seconds,
    )
    parent_key = str(ready["runtime_key"])
    with _cache_lock(home, parent_key):
        source_root, checked_ready, _checked_inputs, _checked_evidence = (
            _select_printer_runtime(home, parent_key)
        )
        checked = postscript_smoke_inputs(
            checked_ready,
            fixture,
            image_record,
            outer_time_limit_seconds=timeout_seconds,
        )
        if checked != inputs:
            raise OracleError(
                "printer-ready runtime changed after keying",
                exit_code=EXIT_INTEGRITY,
            )
        job = Path(tempfile.mkdtemp(prefix="print-smoke-", dir=home / "jobs"))
        job.chmod(0o700)
        _directory_fsync(home / "jobs")
        for name in ("capture", "diagnostics", "home", "output"):
            (job / name).mkdir(mode=0o700)
        runtime = job / "runtime"
        shutil.copytree(source_root / "pristine-c", runtime, copy_function=shutil.copy2)
        _normalize_runtime_metadata(runtime)
        copied_tree, copied_printer = printer_module._validate_printer_runtime(runtime)
        if (
            copied_tree["digest"] != ready["printer_tree_digest"]
            or copied_printer != ready["printer_identity"]
        ):
            raise OracleError(
                "disposable printer copy does not match its runtime",
                exit_code=EXIT_INTEGRITY,
            )
        oracle_dir = runtime / "ORACLE"
        if oracle_dir.is_symlink() or (oracle_dir.exists() and not oracle_dir.is_dir()):
            raise OracleError("unsafe guest staging directory", exit_code=EXIT_INTEGRITY)
        oracle_dir.mkdir(mode=0o700, exist_ok=True)
        atomic_write(oracle_dir / "SMOKE.SAM", payload)
        config_bytes = postscript_smoke_config().encode("utf-8")
        batch_bytes = postscript_smoke_batch()
        atomic_write(runtime / "PRTSMK.BAT", batch_bytes)
        atomic_write(job / "dosbox-x.conf", config_bytes)
        atomic_write_json(job / "inputs.json", inputs)
        machine = StateMachine(
            initial="created",
            terminal=frozenset({"complete", "failed"}),
            transitions={
                "created": frozenset({"staged", "failed"}),
                "staged": frozenset({"guest-invoked", "failed"}),
                "guest-invoked": frozenset({"printed", "failed"}),
                "printed": frozenset({"guest-returned", "failed"}),
                "guest-returned": frozenset({"analyzed", "failed"}),
                "analyzed": frozenset({"complete", "failed"}),
            },
        )
        machine.advance("staged", evidence="inputs.json")
        control = home / "control" / job.name / "guest"
        control.mkdir(mode=0o700, parents=True)
        suffix = re.sub(r"[^a-z0-9]", "", job.name[-10:].casefold())
        invocation = build_podman_invocation(
            image_record,
            container_name=f"amipro-oracle-print-{suffix}",
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
        process: dict[str, object] | None = None
        ui_driver: dict[str, object] | None = None
        observer: dict[str, object] | None = None
        tool_results: dict[str, object] | None = None
        try:
            process, ui_driver = _invoke_guest(invocation, job, timeout_seconds=timeout_seconds)
            if (
                process.get("exit_code") != 0
                or process.get("timed_out") is not False
                or process.get("killed") is not False
            ):
                error = OracleError("PostScript guest did not exit cleanly", exit_code=EXIT_BACKEND)
                error.process_result = process
                raise error
            observer = _validate_observer_evidence(job / "diagnostics")
            validated_driver = _validate_ui_evidence(job)
            if ui_driver != validated_driver:
                raise OracleError("PostScript UI evidence changed", exit_code=EXIT_INTEGRITY)
            _validate_guest_return(runtime, fixture)
            captures = _capture_files(job / "capture")
            if len(captures) != 1:
                raise OracleError("expected exactly one LPT capture", exit_code=EXIT_INTEGRITY)
            raw = captures[0].read_bytes()
            sanitized, transform = validate_postscript(raw)
            atomic_write(job / "output" / "document.raw.ps", raw)
            atomic_write(job / "output" / "document.ps", sanitized)
            atomic_write_json(job / "output" / "postscript-transform.json", transform)
            machine.advance("printed", evidence="output/document.raw.ps")
            _select_printer_runtime(home, parent_key)
            machine.advance("guest-returned", evidence="PRTSMK.OK")
            analysis, tool_results = _derive_outputs(home, image_record, job)
            machine.advance("analyzed", evidence="output/analysis.json")
            machine.advance("complete", evidence="validated PostScript/PDF/PNG analysis")
            artifacts = _artifact_inventory(job)
            manifest: dict[str, Any] = {
                "schema": JOB_SCHEMA,
                "result_schema": POSTSCRIPT_SMOKE_RESULT_SCHEMA,
                "backend": "real",
                "baseline_eligible": False,
                "status": "success",
                "source": {
                    "name": source.name,
                    "size": fixture["size"],
                    "sha256": fixture["sha256"],
                    "staged_name": fixture["staged_name"],
                },
                "media": {
                    "profile": ready["printer_profile"],
                    "sha256": ready["inputs_digest"],
                },
                "runtime": {
                    "profile": ready["printer_profile"],
                    "runtime_key": parent_key,
                    "manifest_sha256": digest_json(ready),
                    "sealed_tree_digest": ready["sealed_tree_digest"],
                },
                "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
                "toolchain": inputs["toolchain"],
                "process_result": {"guest": process, "analysis_tools": tool_results},
                "postscript": transform,
                "analysis_path": "output/analysis.json",
                "artifacts": artifacts,
                "observer": observer,
                "ui_driver": ui_driver,
                "state_trace": machine.trace,
                "duration_seconds": round(monotonic() - started, 6),
                "diagnostics": [
                    "native capture is local and not baseline eligible",
                    "PDF embeds an unnamed Type 3 font derived by Windows PSCRIPT",
                ],
            }
            atomic_write_json(job / "job.json", manifest)
            return {**manifest, "evidence_job": job.name, "analysis": analysis}
        except BaseException as exc:
            attached = getattr(exc, "process_result", None)
            if process is None and isinstance(attached, dict):
                process = attached
            attached_driver = getattr(exc, "ui_driver", None)
            if ui_driver is None and isinstance(attached_driver, dict):
                ui_driver = attached_driver
            if machine.state != "failed" and "failed" in machine.transitions.get(
                machine.state,
                frozenset(),
            ):
                machine.advance("failed", evidence="failure.json")
            atomic_write_json(
                job / "failure.json",
                {
                    "schema": "amipro-oracle-postscript-smoke-failure-v1",
                    "phase": "postscript-smoke",
                    "status": "failure",
                    "baseline_eligible": False,
                    "inputs_digest": digest_json(inputs),
                    "fixture": fixture,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "process_result": process,
                    "tool_results": tool_results,
                    "ui_driver": ui_driver,
                    "state_trace": machine.trace,
                },
            )
            raise
