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
from . import amipro_launch_probe as launch_module
from . import oci as oci_module
from . import process as process_module
from . import windows_bootstrap as bootstrap_module
from .config import DOSBOX_PROFILE, dosbox_config
from .constants import EXIT_BACKEND, EXIT_INTEGRITY, EXIT_MISSING, EXIT_USAGE
from .errors import OracleError
from .io import (
    atomic_write,
    atomic_write_json,
    digest_json,
    read_json_object,
    sha256_file,
)
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

DOCUMENT_SMOKE_INPUT_SCHEMA = "amipro-oracle-document-smoke-input-v1"
DOCUMENT_SMOKE_RESULT_SCHEMA = "amipro-oracle-document-smoke-result-v1"
DOCUMENT_SMOKE_UI_SCHEMA = "amipro-oracle-document-smoke-driver-v1"
TEXT_FIXTURE_SCHEMA = "amipro-oracle-native-text-fixture-v1"
INNER_TIME_LIMIT_SECONDS = 100
OUTER_TIME_LIMIT_SECONDS = 120
UI_DRIVER_TIMEOUT_SECONDS = 100
MAX_FIXTURE_BYTES = 1024 * 1024

DOCUMENT_TITLE_STATE = {
    "name": "smoke-document-title",
    "box": [192, 199, 832, 221],
    "title_sha256": "8a0158c8051d3c7a42c55cad6bbe21b65be54182d3d1da4a34c389cb41c07b03",
}
DOCUMENT_BODY_BOX = [200, 270, 790, 390]
LOADING_INDICATOR_BOX = [500, 420, 525, 460]
MINIMUM_BODY_DARK_PIXELS = 16
DOCUMENT_UI_PROFILE = {
    "name": "amipro-3.1-invented-document-lifecycle-v1",
    "screen_width": install_module.SCREEN_WIDTH,
    "screen_height": install_module.SCREEN_HEIGHT,
    "autolock": False,
    "stable_samples": 2,
    "poll_seconds": 0.25,
    "states": [
        launch_module.PRINTER_WARNING_STATE,
        DOCUMENT_TITLE_STATE,
        launch_module.PROGRAM_MANAGER_MINIMIZED_STATE,
        install_module.EXIT_WINDOWS_STATE,
    ],
    "document_readiness": {
        "body_box": DOCUMENT_BODY_BOX,
        "minimum_body_dark_pixels": MINIMUM_BODY_DARK_PIXELS,
        "loading_indicator_box": LOADING_INDICATOR_BOX,
        "loading_indicator_dark_pixels": 0,
    },
    "actions": [
        "dismiss-printer-warning",
        "close-document-and-amipro",
        "exit-windows",
        "confirm-exit-windows",
    ],
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def document_smoke_config() -> str:
    config = dosbox_config(
        runtime_free_mb=WINDOWS_FREE_MB,
        autoexec=(
            "COUNTRY 1",
            f"DATE {GUEST_DATE}",
            f"TIME {GUEST_TIME}",
            r"Z:\CONFIG.COM -SECUREMODE",
            r"C:\DOCSMK.BAT",
        ),
    )
    return config.replace("autolock=true", "autolock=false")


def document_smoke_batch() -> bytes:
    lines = (
        "@ECHO OFF",
        r"IF EXIST C:\DOCSMK.STA DEL C:\DOCSMK.STA",
        r"IF EXIST C:\DOCSMK.OK DEL C:\DOCSMK.OK",
        r"IF EXIST C:\DOCSMK.ERR DEL C:\DOCSMK.ERR",
        r"IF NOT EXIST C:\AMIPRO\AMIPRO.EXE GOTO AMIPRO_MISSING",
        r"IF NOT EXIST C:\ORACLE\SMOKE.SAM GOTO DOCUMENT_MISSING",
        r"ECHO DOCUMENT_LAUNCH_REQUESTED>C:\DOCSMK.STA",
        r"C:\WINDOWS\WIN.COM C:\AMIPRO\AMIPRO.EXE C:\ORACLE\SMOKE.SAM",
        "IF ERRORLEVEL 1 GOTO DOCUMENT_FAILED",
        r"ECHO DOCUMENT_RETURNED_ZERO>C:\DOCSMK.OK",
        "GOTO DOCUMENT_DONE",
        ":AMIPRO_MISSING",
        r"ECHO AMIPRO_EXE_MISSING>C:\DOCSMK.ERR",
        "GOTO DOCUMENT_DONE",
        ":DOCUMENT_MISSING",
        r"ECHO SMOKE_DOCUMENT_MISSING>C:\DOCSMK.ERR",
        "GOTO DOCUMENT_DONE",
        ":DOCUMENT_FAILED",
        r"ECHO DOCUMENT_ERRORLEVEL_NONZERO>C:\DOCSMK.ERR",
        ":DOCUMENT_DONE",
        "EXIT",
    )
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_text_fixture(path: Path) -> tuple[bytes, dict[str, object]]:
    absolute = path.expanduser().absolute()
    try:
        initial = absolute.lstat()
    except FileNotFoundError as exc:
        raise OracleError("invented SAM fixture is missing", exit_code=EXIT_MISSING) from exc
    if not stat.S_ISREG(initial.st_mode) or stat.S_ISLNK(initial.st_mode):
        raise OracleError(
            "invented SAM fixture must be a regular non-symlink file",
            exit_code=EXIT_INTEGRITY,
        )
    if not 1 <= initial.st_size <= MAX_FIXTURE_BYTES:
        raise OracleError(
            f"invented SAM fixture must be between 1 and {MAX_FIXTURE_BYTES} bytes",
            exit_code=EXIT_INTEGRITY,
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise OracleError(
            "cannot open invented SAM fixture safely",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    try:
        before = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(initial) or not stat.S_ISREG(before.st_mode):
            raise OracleError(
                "invented SAM fixture changed before reading",
                exit_code=EXIT_INTEGRITY,
            )
        chunks: list[bytes] = []
        remaining = initial.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise OracleError(
                    "invented SAM fixture was truncated while reading",
                    exit_code=EXIT_INTEGRITY,
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise OracleError(
                "invented SAM fixture grew while reading",
                exit_code=EXIT_INTEGRITY,
            )
        after = os.fstat(descriptor)
        if _file_identity(after) != _file_identity(initial):
            raise OracleError(
                "invented SAM fixture changed while reading",
                exit_code=EXIT_INTEGRITY,
            )
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    identity = validate_text_fixture(payload)
    return payload, identity


def validate_text_fixture(payload: bytes) -> dict[str, object]:
    if not 1 <= len(payload) <= MAX_FIXTURE_BYTES or b"\x00" in payload:
        raise OracleError(
            "invented SAM must be a bounded text-only fixture",
            exit_code=EXIT_INTEGRITY,
        )
    without_crlf = payload.replace(b"\r\n", b"")
    if b"\r" in without_crlf or b"\n" in without_crlf:
        raise OracleError("invented SAM must use canonical CRLF", exit_code=EXIT_INTEGRITY)
    marker = b"[Embedded]\r\n"
    offset = payload.rfind(marker)
    if offset <= 0 or payload.count(marker) != 1 or offset > 99_999_999:
        raise OracleError(
            "invented SAM has no canonical embedded-directory trailer",
            exit_code=EXIT_INTEGRITY,
        )
    trailer = marker + f"{offset:08d}".encode("ascii") + b"\r\n"
    if payload[offset:] != trailer:
        raise OracleError(
            "invented SAM embedded-directory offset is inconsistent",
            exit_code=EXIT_INTEGRITY,
        )
    prefix = payload[:offset]
    if (
        not prefix.startswith(b"[ver]\r\n\t4\r\n")
        or b"[edoc]\r\n" not in prefix
        or not prefix.endswith(b">\r\n\r\n")
    ):
        raise OracleError(
            "invented SAM lacks the canonical version-4 text envelope",
            exit_code=EXIT_INTEGRITY,
        )
    return {
        "schema": TEXT_FIXTURE_SCHEMA,
        "profile": "invented-version-4-cp1252-text-only-v1",
        "staged_name": "SMOKE.SAM",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "embedded_directory_offset": offset,
    }


def _source_fingerprints() -> dict[str, str]:
    modules = {
        "amipro_install": Path(install_module.__file__),
        "amipro_launch_probe": Path(launch_module.__file__),
        "document_smoke": Path(__file__),
        "oci": Path(oci_module.__file__),
        "process": Path(process_module.__file__),
        "windows_bootstrap": Path(bootstrap_module.__file__),
    }
    return {name: sha256_file(path) for name, path in sorted(modules.items())}


def _select_ready_runtime(
    home: Path,
    runtime_key: str | None,
) -> tuple[Path, dict[str, Any], dict[str, object], str]:
    parent = home / "cache" / "amipro-ready"
    if parent.is_symlink() or not parent.is_dir():
        raise OracleError("Ami Pro-ready cache is missing", exit_code=EXIT_MISSING)
    if runtime_key is None:
        candidates: list[str] = []
        for path in sorted(parent.iterdir(), key=lambda item: item.name):
            if _SHA256.fullmatch(path.name) is None:
                continue
            if path.is_symlink() or not path.is_dir():
                raise OracleError("unsafe Ami Pro-ready cache entry", exit_code=EXIT_INTEGRITY)
            candidates.append(path.name)
        if not candidates:
            raise OracleError("run launch-amipro before smoke", exit_code=EXIT_MISSING)
        if len(candidates) != 1:
            raise OracleError(
                "multiple Ami Pro-ready runtimes exist; pass --runtime-key",
                exit_code=EXIT_USAGE,
            )
        runtime_key = candidates[0]
    if _SHA256.fullmatch(runtime_key) is None:
        raise OracleError("invalid --runtime-key", exit_code=EXIT_USAGE)
    root = parent / runtime_key
    inputs_path = root / "inputs.json"
    if inputs_path.is_symlink() or not inputs_path.is_file():
        raise OracleError("Ami Pro-ready inputs are missing", exit_code=EXIT_INTEGRITY)
    try:
        inputs = read_json_object(inputs_path)
    except (OSError, ValueError) as exc:
        raise OracleError("Ami Pro-ready inputs are invalid", exit_code=EXIT_INTEGRITY) from exc
    runtime, evidence_job = launch_module._verify_ready_cache(
        home,
        root,
        runtime_key,
        inputs,
    )
    return root, runtime, inputs, evidence_job


def document_smoke_inputs(
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
            f"document smoke timeout must be between 1 and {OUTER_TIME_LIMIT_SECONDS} seconds",
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
        or fixture.get("schema") != TEXT_FIXTURE_SCHEMA
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
        raise OracleError("invalid document smoke input identity", exit_code=EXIT_INTEGRITY)
    config = document_smoke_config().encode("utf-8")
    batch = document_smoke_batch()
    return {
        "schema": DOCUMENT_SMOKE_INPUT_SCHEMA,
        "amipro_ready": {
            "runtime_key": ready["runtime_key"],
            "manifest_digest": digest_json(ready),
            "sealed_tree_digest": ready["sealed_tree_digest"],
        },
        "fixture": fixture,
        "toolchain": {
            "image_id": image_id,
            "image_digest": image_digest,
            "lock_sha256": lock_hash,
            "platform": "linux/amd64",
        },
        "driver_profile": DOCUMENT_UI_PROFILE,
        "dosbox_profile": DOSBOX_PROFILE,
        "dosbox_config_sha256": hashlib.sha256(config).hexdigest(),
        "smoke_batch_sha256": hashlib.sha256(batch).hexdigest(),
        "orchestrator_sha256": _source_fingerprints(),
        "guest_clock": {"date_command": GUEST_DATE, "time_command": GUEST_TIME},
        "reported_free_mb": WINDOWS_FREE_MB,
        "printer_profile": "none-screen-formatting-warning-expected",
        "inner_time_limit_seconds": INNER_TIME_LIMIT_SECONDS,
        "outer_time_limit_seconds": outer_time_limit_seconds,
    }


def _crop_pixels(
    pixels: bytes,
    width: int,
    box: list[int],
) -> bytes:
    x0, y0, x1, y1 = box
    return b"".join(
        pixels[(row * width + x0) * 4 : (row * width + x1) * 4]
        for row in range(y0, y1)
    )


def _document_state(path: Path) -> tuple[dict[str, object], bytes]:
    title, payload = install_module._screen_state(path, DOCUMENT_TITLE_STATE)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".document-smoke-screen-",
        suffix=".png",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        width, height, pixels = decode_png(temporary)
    except (OSError, ValueError) as exc:
        raise OracleError("document screen evidence is invalid", exit_code=EXIT_BACKEND) from exc
    finally:
        temporary.unlink(missing_ok=True)
    if (width, height) != (install_module.SCREEN_WIDTH, install_module.SCREEN_HEIGHT):
        raise OracleError("document screen dimensions changed", exit_code=EXIT_BACKEND)
    body = _crop_pixels(pixels, width, DOCUMENT_BODY_BOX)
    loading = _crop_pixels(pixels, width, LOADING_INDICATOR_BOX)
    body_dark = sum(
        max(body[index : index + 3]) < 128 for index in range(0, len(body), 4)
    )
    loading_dark = sum(
        max(loading[index : index + 3]) < 128 for index in range(0, len(loading), 4)
    )
    return (
        {
            **title,
            "body_box": DOCUMENT_BODY_BOX,
            "body_dark_pixels": body_dark,
            "loading_indicator_box": LOADING_INDICATOR_BOX,
            "loading_indicator_dark_pixels": loading_dark,
        },
        payload,
    )


def _wait_document_state(
    screen: Path,
    *,
    stop: threading.Event,
    deadline: float,
) -> tuple[dict[str, object], bytes]:
    seen_mtime: int | None = None
    stable = 0
    while monotonic() < deadline and not stop.is_set():
        try:
            evidence, payload = _document_state(screen)
            mtime = screen.stat().st_mtime_ns
        except (OSError, OracleError):
            sleep(0.25)
            continue
        if (
            evidence["title_sha256"] == DOCUMENT_TITLE_STATE["title_sha256"]
            and int(evidence["body_dark_pixels"]) >= MINIMUM_BODY_DARK_PIXELS
            and evidence["loading_indicator_dark_pixels"] == 0
        ):
            if mtime != seen_mtime:
                stable += 1
                seen_mtime = mtime
            if stable >= 2:
                return evidence, payload
        else:
            stable = 0
            seen_mtime = None
        sleep(0.25)
    raise OracleError("invented document did not reach ready state", exit_code=EXIT_BACKEND)


def _wait_sentinel(runtime: Path, stop: threading.Event, deadline: float) -> None:
    sentinel = runtime / "DOCSMK.STA"
    while monotonic() < deadline and not stop.is_set():
        if (
            sentinel.is_file()
            and not sentinel.is_symlink()
            and sentinel.read_bytes() == b"DOCUMENT_LAUNCH_REQUESTED\r\n"
        ):
            return
        sleep(0.1)
    raise OracleError("document launch sentinel was not observed", exit_code=EXIT_BACKEND)


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


def _drive_document_lifecycle(
    invocation: PodmanInvocation,
    job: Path,
    stop: threading.Event,
) -> dict[str, object]:
    deadline = monotonic() + UI_DRIVER_TIMEOUT_SECONDS
    _wait_sentinel(job / "runtime", stop, deadline)
    warning = _capture_exact_state(
        job,
        launch_module.PRINTER_WARNING_STATE,
        "document-printer-warning.png",
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

    press("dismiss-printer-warning", "Return")
    document, payload = _wait_document_state(
        job / "diagnostics" / "screen-last.png",
        stop=stop,
        deadline=deadline,
    )
    atomic_write(job / "diagnostics" / "document-ready.png", payload)
    document["path"] = "document-ready.png"
    press("close-document-and-amipro", "alt+F4")
    minimized = _capture_exact_state(
        job,
        launch_module.PROGRAM_MANAGER_MINIMIZED_STATE,
        "document-program-manager-minimized.png",
        stop=stop,
        deadline=deadline,
    )
    press("exit-windows", "alt+F4")
    confirmation = _capture_exact_state(
        job,
        install_module.EXIT_WINDOWS_STATE,
        "document-exit-windows-confirmation.png",
        stop=stop,
        deadline=deadline,
    )
    press("confirm-exit-windows", "Return")
    return {
        "schema": DOCUMENT_SMOKE_UI_SCHEMA,
        "status": "success",
        "profile": DOCUMENT_UI_PROFILE,
        "states": [warning, document, minimized, confirmation],
        "actions": actions,
    }


def _invoke_document_job(
    invocation: PodmanInvocation,
    job: Path,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, object], dict[str, object]]:
    stop = threading.Event()
    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["result"] = _drive_document_lifecycle(invocation, job, stop)
        except BaseException as exc:
            box["result"] = {
                "schema": DOCUMENT_SMOKE_UI_SCHEMA,
                "status": "failure",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    driver = threading.Thread(target=worker, name="document-smoke-driver", daemon=True)
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
        "schema": DOCUMENT_SMOKE_UI_SCHEMA,
        "status": "failure",
        "error_type": "DriverThreadError",
        "error": "document smoke driver did not return evidence",
    }
    if driver.is_alive():
        driver_result = {
            "schema": DOCUMENT_SMOKE_UI_SCHEMA,
            "status": "failure",
            "error_type": "DriverThreadError",
            "error": "document smoke driver thread did not stop",
        }
    atomic_write_json(job / "ui-driver.json", driver_result)
    if process_error is not None:
        if isinstance(process_error, OracleError):
            process_error.ui_driver = driver_result
        raise process_error
    if process is None:
        raise OracleError("document smoke process did not return", exit_code=EXIT_BACKEND)
    if driver_result.get("status") != "success":
        error = OracleError(
            f"document smoke driver failed: {driver_result.get('error', 'unknown error')}",
            exit_code=EXIT_BACKEND,
        )
        error.process_result = process
        error.ui_driver = driver_result
        raise error
    return process, driver_result


def _validate_ui_evidence(job: Path) -> dict[str, object]:
    path = job / "ui-driver.json"
    if path.is_symlink() or not path.is_file():
        raise OracleError("document UI evidence is missing", exit_code=EXIT_INTEGRITY)
    try:
        driver = read_json_object(path)
    except (OSError, ValueError) as exc:
        raise OracleError("document UI evidence is invalid", exit_code=EXIT_INTEGRITY) from exc
    expected_actions = [
        {"action": "dismiss-printer-warning", "key": "Return", "exit_code": 0},
        {"action": "close-document-and-amipro", "key": "alt+F4", "exit_code": 0},
        {"action": "exit-windows", "key": "alt+F4", "exit_code": 0},
        {"action": "confirm-exit-windows", "key": "Return", "exit_code": 0},
    ]
    states = driver.get("states")
    if (
        driver.get("schema") != DOCUMENT_SMOKE_UI_SCHEMA
        or driver.get("status") != "success"
        or driver.get("profile") != DOCUMENT_UI_PROFILE
        or driver.get("actions") != expected_actions
        or not isinstance(states, list)
        or len(states) != 4
    ):
        raise OracleError("document UI evidence mismatch", exit_code=EXIT_INTEGRITY)
    exact = (
        (
            launch_module.PRINTER_WARNING_STATE,
            "document-printer-warning.png",
        ),
        (
            launch_module.PROGRAM_MANAGER_MINIMIZED_STATE,
            "document-program-manager-minimized.png",
        ),
        (
            install_module.EXIT_WINDOWS_STATE,
            "document-exit-windows-confirmation.png",
        ),
    )
    observed_exact: list[dict[str, object]] = []
    for state, filename in exact:
        try:
            observed, _ = install_module._screen_state(job / "diagnostics" / filename, state)
        except OracleError as exc:
            raise OracleError(
                "document lifecycle screenshot is invalid",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        observed["path"] = filename
        observed_exact.append(observed)
    try:
        document, _ = _document_state(job / "diagnostics" / "document-ready.png")
    except OracleError as exc:
        raise OracleError("document-ready screenshot is invalid", exit_code=EXIT_INTEGRITY) from exc
    if (
        document["title_sha256"] != DOCUMENT_TITLE_STATE["title_sha256"]
        or int(document["body_dark_pixels"]) < MINIMUM_BODY_DARK_PIXELS
        or document["loading_indicator_dark_pixels"] != 0
    ):
        raise OracleError(
            "document-ready screenshot failed its predicate",
            exit_code=EXIT_INTEGRITY,
        )
    document["path"] = "document-ready.png"
    observed_states = [observed_exact[0], document, observed_exact[1], observed_exact[2]]
    if states != observed_states:
        raise OracleError("document lifecycle screenshots changed", exit_code=EXIT_INTEGRITY)
    return driver


def _validate_guest_return(runtime: Path, fixture: dict[str, object]) -> None:
    expected = {
        "DOCSMK.STA": b"DOCUMENT_LAUNCH_REQUESTED\r\n",
        "DOCSMK.OK": b"DOCUMENT_RETURNED_ZERO\r\n",
    }
    for name, payload in expected.items():
        path = runtime / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise OracleError(f"document smoke sentinel is invalid: {name}", exit_code=EXIT_BACKEND)
    if (runtime / "DOCSMK.ERR").exists() or (runtime / "DOCSMK.ERR").is_symlink():
        raise OracleError("document smoke reported a guest error", exit_code=EXIT_BACKEND)
    source = runtime / "ORACLE" / "SMOKE.SAM"
    if (
        source.is_symlink()
        or not source.is_file()
        or source.stat().st_size != fixture["size"]
        or sha256_file(source) != fixture["sha256"]
    ):
        raise OracleError("staged invented document changed in the guest", exit_code=EXIT_INTEGRITY)


def smoke_document(
    home: Path,
    image_record: dict[str, Any],
    source: Path,
    *,
    runtime_key: str | None = None,
    timeout_seconds: float = OUTER_TIME_LIMIT_SECONDS,
) -> dict[str, Any]:
    payload, fixture = read_text_fixture(source)
    _require_verified_image(image_record)
    _ensure_private_directories(home)
    source_root, ready, _ready_inputs, _ready_evidence = _select_ready_runtime(
        home,
        runtime_key,
    )
    inputs = document_smoke_inputs(
        ready,
        fixture,
        image_record,
        outer_time_limit_seconds=timeout_seconds,
    )
    parent_key = str(ready["runtime_key"])
    with _cache_lock(home, parent_key):
        source_root, checked_ready, _checked_inputs, _checked_evidence = (
            _select_ready_runtime(home, parent_key)
        )
        checked = document_smoke_inputs(
            checked_ready,
            fixture,
            image_record,
            outer_time_limit_seconds=timeout_seconds,
        )
        if checked != inputs:
            raise OracleError(
                "Ami Pro-ready runtime changed after keying",
                exit_code=EXIT_INTEGRITY,
            )
        job = Path(tempfile.mkdtemp(prefix="smoke-document-", dir=home / "jobs"))
        job.chmod(0o700)
        _directory_fsync(home / "jobs")
        for name in ("capture", "diagnostics", "home"):
            (job / name).mkdir(mode=0o700)
        runtime = job / "runtime"
        shutil.copytree(source_root / "pristine-c", runtime, copy_function=shutil.copy2)
        _normalize_runtime_metadata(runtime)
        copied = install_module._validate_installed_amipro(runtime)
        if copied["digest"] != ready["launch_tree_digest"]:
            raise OracleError(
                "disposable Ami Pro copy does not match its ready runtime",
                exit_code=EXIT_INTEGRITY,
            )
        oracle_dir = runtime / "ORACLE"
        if oracle_dir.is_symlink() or (oracle_dir.exists() and not oracle_dir.is_dir()):
            raise OracleError("unsafe guest staging directory", exit_code=EXIT_INTEGRITY)
        oracle_dir.mkdir(mode=0o700, exist_ok=True)
        atomic_write(oracle_dir / "SMOKE.SAM", payload)
        config_bytes = document_smoke_config().encode("utf-8")
        batch_bytes = document_smoke_batch()
        atomic_write(runtime / "DOCSMK.BAT", batch_bytes)
        atomic_write(job / "dosbox-x.conf", config_bytes)
        atomic_write_json(job / "inputs.json", inputs)
        machine = StateMachine(
            initial="created",
            terminal=frozenset({"complete", "failed"}),
            transitions={
                "created": frozenset({"staged", "failed"}),
                "staged": frozenset({"guest-invoked", "failed"}),
                "guest-invoked": frozenset({"guest-returned", "failed"}),
                "guest-returned": frozenset({"validated", "failed"}),
                "validated": frozenset({"complete", "failed"}),
                "complete": frozenset({"failed"}),
            },
        )
        machine.advance("staged", evidence="inputs.json")
        control = home / "control" / job.name
        control.mkdir(mode=0o700)
        suffix = re.sub(r"[^a-z0-9]", "", job.name[-10:].casefold())
        invocation = build_podman_invocation(
            image_record,
            container_name=f"amipro-oracle-smoke-{suffix}",
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
        try:
            process, ui_driver = _invoke_document_job(
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
                    "document smoke container did not exit cleanly",
                    exit_code=EXIT_BACKEND,
                )
                error.process_result = process
                raise error
            observer = _validate_observer_evidence(job / "diagnostics")
            validated_driver = _validate_ui_evidence(job)
            if ui_driver != validated_driver:
                raise OracleError("document smoke evidence changed", exit_code=EXIT_INTEGRITY)
            _validate_guest_return(runtime, fixture)
            machine.advance("guest-returned", evidence="DOCSMK.OK")
            tree = install_module._validate_installed_amipro(runtime)
            atomic_write_json(job / "document-tree.json", tree)
            machine.advance("validated", evidence="document-tree.json")
            machine.advance("complete", evidence="validated document lifecycle evidence")
            result: dict[str, Any] = {
                "schema": DOCUMENT_SMOKE_RESULT_SCHEMA,
                "backend": "real",
                "baseline_eligible": False,
                "status": "document-smoke-passed",
                "runtime_key": parent_key,
                "evidence_job": job.name,
                "inputs_digest": digest_json(inputs),
                "fixture": fixture,
                "document_tree_digest": tree["digest"],
                "observer": observer,
                "ui_driver": ui_driver,
                "process_result": process,
                "state_trace": machine.trace,
            }
            atomic_write_json(job / "result.json", result)
            return result
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
                    "schema": "amipro-oracle-document-smoke-failure-v1",
                    "phase": "document-smoke",
                    "status": "failure",
                    "baseline_eligible": False,
                    "inputs_digest": digest_json(inputs),
                    "fixture": fixture,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "process_result": process,
                    "ui_driver": ui_driver,
                    "state_trace": machine.trace,
                },
            )
            raise
