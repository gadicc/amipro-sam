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

from . import fat12 as fat12_module
from . import media as media_module
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
    EXPECTED_AMIPRO_EXE_SHA256,
    RUNTIME_SCHEMA,
)
from .errors import OracleError
from .fat12 import EXTRACTION_SCHEMA, extract_fat12_root_images
from .io import atomic_write, atomic_write_json, digest_json, read_json_object, sha256_file
from .media import inventory_media
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
)

AMIPRO_MEDIA_PROFILE = "owned-amipro-3.1-floppies-v1"
AMIPRO_FLAT_SCHEMA = "amipro-oracle-flat-amipro-media-v1"
AMIPRO_INPUT_SCHEMA = "amipro-oracle-amipro-install-input-v1"
AMIPRO_CHECKPOINT_SCHEMA = "amipro-oracle-amipro-install-checkpoint-v1"
AMIPRO_RESULT_SCHEMA = "amipro-oracle-amipro-install-result-v1"
AMIPRO_UI_SCHEMA = "amipro-oracle-amipro-installer-driver-v1"
EXPECTED_EXTRACTION_DIGEST = (
    "ea4371d795a595c017f681f2d0ead0d294184be0efb973a50bc583f48825dde7"
)
EXPECTED_EXTRACTION_FILES = 19
EXPECTED_EXTRACTION_BYTES = 11_614_686
INNER_TIME_LIMIT_SECONDS = 240
OUTER_TIME_LIMIT_SECONDS = 300
UI_DRIVER_TIMEOUT_SECONDS = 260
SCREEN_WIDTH = 1024
SCREEN_HEIGHT = 768

INSTALLER_STATES: tuple[dict[str, object], ...] = (
    {
        "name": "welcome",
        "box": [310, 240, 715, 263],
        "title_sha256": "9ec53960e74691a07641c34c22814adc62e3c73e3358d69dc40d6bef7adfb595",
        "keys": ["Tab", "Tab", "Tab", "Return"],
    },
    {
        "name": "confirm-names",
        "box": [338, 293, 686, 318],
        "title_sha256": "dde031baa7676f767335a3c881145f685427345771fe135cc4944445315ad45d",
        "keys": ["Return"],
    },
    {
        "name": "install-options",
        "box": [301, 232, 722, 255],
        "title_sha256": "786fc1b6119f46f0454b1ac88bc4a241ae05775e70a9b57cfb5841678fa4c203",
        "keys": ["Tab", "Tab", "Tab", "Return"],
    },
    {
        "name": "shared-tools-directory",
        "box": [286, 272, 738, 295],
        "title_sha256": "deb9595a2cbd6f2084edb21c2e957232f249c1046c95ca1a0e95efe254502d2a",
        "keys": ["Tab", "Tab", "Return"],
    },
    {
        "name": "program-group",
        "box": [313, 272, 711, 295],
        "title_sha256": "355cd90eb6ae0dbf575cfaee3736b24242f10449304dcda2c40b8dceb48fc535",
        "keys": ["Tab", "Tab", "Return"],
    },
    {
        "name": "begin-copying",
        "box": [300, 323, 723, 346],
        "title_sha256": "fba0bf288435ce49bdeafcd112c3449a4d4f70f6d2159fb4330869e3e5ad93ef",
        "keys": ["Return"],
    },
    {
        "name": "install-complete",
        "box": [324, 327, 700, 350],
        "title_sha256": "b99a1d9c2797fa0f37f1012fe999ef8b2e0e1d407736b5b97920a69bbb33d14a",
        "keys": ["Return"],
    },
)
INSTALLER_UI_PROFILE = {
    "name": "amipro-3.1-default-full-install-v1",
    "screen_width": SCREEN_WIDTH,
    "screen_height": SCREEN_HEIGHT,
    "autolock": False,
    "stable_samples": 2,
    "poll_seconds": 0.25,
    "states": list(INSTALLER_STATES),
    "post_install_exit_profile": boot_module.UI_PROFILE,
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def amipro_install_config() -> str:
    config = dosbox_config(
        runtime_free_mb=WINDOWS_FREE_MB,
        autoexec=(
            'MOUNT S "/oracle/media/amipro" -t dir -ro',
            "COUNTRY 1",
            f"DATE {GUEST_DATE}",
            f"TIME {GUEST_TIME}",
            r"Z:\CONFIG.COM -SECUREMODE",
            r"C:\AMIINST.BAT",
        ),
    )
    return config.replace("autolock=true", "autolock=false")


def amipro_install_batch() -> bytes:
    lines = (
        "@ECHO OFF",
        r"IF EXIST C:\AMIINST.OK DEL C:\AMIINST.OK",
        r"IF EXIST C:\AMIINST.ERR DEL C:\AMIINST.ERR",
        r"C:\WINDOWS\WIN.COM S:\INSTALL.EXE",
        "IF ERRORLEVEL 1 GOTO INSTALL_FAILED",
        r"ECHO INSTALL_RETURNED_ZERO>C:\AMIINST.OK",
        "GOTO INSTALL_DONE",
        ":INSTALL_FAILED",
        r"ECHO INSTALL_ERRORLEVEL_NONZERO>C:\AMIINST.ERR",
        ":INSTALL_DONE",
        "EXIT",
    )
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def _source_fingerprints() -> dict[str, str]:
    modules = {
        "amipro_install": Path(__file__),
        "fat12": Path(fat12_module.__file__),
        "media": Path(media_module.__file__),
        "oci": Path(oci_module.__file__),
        "process": Path(process_module.__file__),
        "windows_boot_probe": Path(boot_module.__file__),
        "windows_bootstrap": Path(bootstrap_module.__file__),
    }
    return {name: sha256_file(path) for name, path in sorted(modules.items())}


def _media_identity(media: dict[str, Any]) -> dict[str, object]:
    if (
        media.get("schema") != "amipro-oracle-media-v1"
        or media.get("kind") != "amipro"
        or media.get("media_profile") != AMIPRO_MEDIA_PROFILE
        or media.get("file_count") != 8
        or not isinstance(media.get("digest"), str)
        or _SHA256.fullmatch(str(media["digest"])) is None
    ):
        raise OracleError(
            "Ami Pro media is not the supported eight-floppy profile",
            exit_code=EXIT_INTEGRITY,
        )
    return {
        "schema": AMIPRO_FLAT_SCHEMA,
        "media_kind": "amipro",
        "media_profile": AMIPRO_MEDIA_PROFILE,
        "media_digest": media["digest"],
        "extractor_schema": EXTRACTION_SCHEMA,
    }


def _seal_flat_cache(root: Path) -> None:
    for name in ("extraction.json", "manifest.json", "tree.json"):
        (root / name).chmod(0o444)
    (root / "source").chmod(0o555)
    root.chmod(0o555)


def _verify_flat_cache(
    root: Path,
    *,
    expected_key: str,
    expected_identity: dict[str, object],
    require_sealed: bool = True,
) -> dict[str, Any]:
    paths = {
        "manifest": root / "manifest.json",
        "extraction": root / "extraction.json",
        "tree": root / "tree.json",
    }
    source = root / "source"
    if (
        root.is_symlink()
        or not root.is_dir()
        or {path.name for path in root.iterdir()}
        != {"extraction.json", "manifest.json", "source", "tree.json"}
        or any(path.is_symlink() or not path.is_file() for path in paths.values())
        or source.is_symlink()
        or not source.is_dir()
    ):
        raise OracleError("flat Ami Pro media cache has an unsafe shape", exit_code=EXIT_INTEGRITY)
    try:
        manifest = read_json_object(paths["manifest"])
        extraction = read_json_object(paths["extraction"])
        recorded_tree = read_json_object(paths["tree"])
    except (OSError, ValueError) as exc:
        raise OracleError(
            "flat Ami Pro media cache manifest is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    tree = inventory_media(source, kind="flattened-amipro-installer")
    if (
        set(manifest)
        != {
            "cache_key",
            "extraction_digest",
            "file_count",
            "identity",
            "schema",
            "status",
            "total_bytes",
            "tree_digest",
        }
        or manifest.get("schema") != AMIPRO_FLAT_SCHEMA
        or manifest.get("status") != "ready"
        or manifest.get("cache_key") != expected_key
        or manifest.get("identity") != expected_identity
        or manifest.get("extraction_digest") != EXPECTED_EXTRACTION_DIGEST
        or manifest.get("file_count") != EXPECTED_EXTRACTION_FILES
        or manifest.get("total_bytes") != EXPECTED_EXTRACTION_BYTES
        or extraction.get("schema") != EXTRACTION_SCHEMA
        or extraction.get("source_media_digest") != expected_identity.get("media_digest")
        or not isinstance(extraction.get("files"), list)
        or digest_json(
            {
                "schema": extraction.get("schema"),
                "source_media_digest": extraction.get("source_media_digest"),
                "files": extraction.get("files"),
            }
        )
        != extraction.get("digest")
        or extraction.get("digest") != EXPECTED_EXTRACTION_DIGEST
        or extraction.get("file_count") != EXPECTED_EXTRACTION_FILES
        or extraction.get("total_bytes") != EXPECTED_EXTRACTION_BYTES
        or recorded_tree != tree
        or tree.get("digest") != manifest.get("tree_digest")
        or tree.get("file_count") != EXPECTED_EXTRACTION_FILES
        or tree.get("total_bytes") != EXPECTED_EXTRACTION_BYTES
        or tree.get("source_writable_files") != 0
        or stat.S_IMODE(source.stat().st_mode) & 0o222
        or (
            require_sealed
            and (
                stat.S_IMODE(root.stat().st_mode) & 0o222
                or any(stat.S_IMODE(path.stat().st_mode) & 0o222 for path in paths.values())
            )
        )
    ):
        raise OracleError("flat Ami Pro media cache identity mismatch", exit_code=EXIT_INTEGRITY)
    return manifest


def ensure_flat_amipro_media(
    home: Path,
    media_root: Path,
    media: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    _ensure_private_directories(home)
    identity = _media_identity(media)
    key = digest_json(identity)
    final = home / "cache" / "media" / key
    with _cache_lock(home, key):
        if final.exists() or final.is_symlink():
            manifest = _verify_flat_cache(
                final,
                expected_key=key,
                expected_identity=identity,
                require_sealed=False,
            )
            _seal_flat_cache(final)
            return final / "source", _verify_flat_cache(
                final,
                expected_key=key,
                expected_identity=identity,
            )
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{key}.",
                suffix=".staging",
                dir=home / "cache" / "media",
            )
        )
        staging.chmod(0o700)
        extraction = extract_fat12_root_images(media_root, media, staging / "source")
        if (
            extraction.get("digest") != EXPECTED_EXTRACTION_DIGEST
            or extraction.get("file_count") != EXPECTED_EXTRACTION_FILES
            or extraction.get("total_bytes") != EXPECTED_EXTRACTION_BYTES
        ):
            raise OracleError(
                "flattened Ami Pro source does not match the pinned extraction profile",
                exit_code=EXIT_INTEGRITY,
            )
        tree = inventory_media(staging / "source", kind="flattened-amipro-installer")
        manifest: dict[str, Any] = {
            "schema": AMIPRO_FLAT_SCHEMA,
            "status": "ready",
            "cache_key": key,
            "identity": identity,
            "extraction_digest": extraction["digest"],
            "file_count": extraction["file_count"],
            "total_bytes": extraction["total_bytes"],
            "tree_digest": tree["digest"],
        }
        atomic_write_json(staging / "extraction.json", extraction)
        atomic_write_json(staging / "tree.json", tree)
        atomic_write_json(staging / "manifest.json", manifest)
        _seal_flat_cache(staging)
        _tree_fsync(staging)
        os.rename(staging, final)
        _directory_fsync(final.parent)
        return final / "source", _verify_flat_cache(
            final,
            expected_key=key,
            expected_identity=identity,
        )


def _select_windows_ready(
    home: Path,
    runtime_key: str | None,
) -> tuple[Path, dict[str, Any], dict[str, Any], str]:
    parent = home / "cache" / "windows-ready"
    if parent.is_symlink() or not parent.is_dir():
        raise OracleError("Windows-ready cache is missing", exit_code=EXIT_MISSING)
    if runtime_key is None:
        candidates = [
            path.name
            for path in parent.iterdir()
            if _SHA256.fullmatch(path.name) is not None
            and not path.is_symlink()
            and path.is_dir()
        ]
        if not candidates:
            raise OracleError("run boot-probe before installing Ami Pro", exit_code=EXIT_MISSING)
        if len(candidates) != 1:
            raise OracleError(
                "multiple Windows-ready runtimes exist; pass --runtime-key",
                exit_code=EXIT_USAGE,
            )
        runtime_key = candidates[0]
    if _SHA256.fullmatch(runtime_key) is None:
        raise OracleError("invalid --runtime-key", exit_code=EXIT_USAGE)
    root = parent / runtime_key
    inputs_path = root / "inputs.json"
    if inputs_path.is_symlink() or not inputs_path.is_file():
        raise OracleError("Windows-ready inputs are missing", exit_code=EXIT_INTEGRITY)
    try:
        inputs = read_json_object(inputs_path)
    except (OSError, ValueError) as exc:
        raise OracleError("Windows-ready inputs are invalid", exit_code=EXIT_INTEGRITY) from exc
    runtime, evidence_job = boot_module._verify_ready_cache(
        home,
        root,
        runtime_key,
        inputs,
    )
    return root, runtime, inputs, evidence_job


def amipro_install_inputs(
    windows_runtime: dict[str, Any],
    media: dict[str, Any],
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
            f"Ami Pro install timeout must be between 1 and {OUTER_TIME_LIMIT_SECONDS} seconds",
            exit_code=EXIT_USAGE,
        )
    image_id = image_record.get("image_id")
    image_digest = image_record.get("image_digest")
    lock_hash = image_record.get("lock_sha256")
    if (
        windows_runtime.get("schema") != boot_module.WINDOWS_READY_SCHEMA
        or windows_runtime.get("status") != "windows-ready"
        or not isinstance(windows_runtime.get("runtime_key"), str)
        or _SHA256.fullmatch(str(windows_runtime["runtime_key"])) is None
        or media.get("media_profile") != AMIPRO_MEDIA_PROFILE
        or flat_media.get("extraction_digest") != EXPECTED_EXTRACTION_DIGEST
        or not isinstance(image_id, str)
        or _SHA256.fullmatch(image_id) is None
        or not isinstance(image_digest, str)
        or _IMAGE_DIGEST.fullmatch(image_digest) is None
        or not isinstance(lock_hash, str)
        or _SHA256.fullmatch(lock_hash) is None
        or image_record.get("platform") != "linux/amd64"
    ):
        raise OracleError("invalid Ami Pro installer input identity", exit_code=EXIT_INTEGRITY)
    config = amipro_install_config().encode("utf-8")
    batch = amipro_install_batch()
    return {
        "schema": AMIPRO_INPUT_SCHEMA,
        "windows_runtime": {
            "runtime_key": windows_runtime["runtime_key"],
            "manifest_digest": digest_json(windows_runtime),
            "sealed_tree_digest": windows_runtime["sealed_tree_digest"],
        },
        "amipro_media": {
            "media_profile": media["media_profile"],
            "media_digest": media["digest"],
            "flat_cache_key": flat_media["cache_key"],
            "flat_extraction_digest": flat_media["extraction_digest"],
            "flat_tree_digest": flat_media["tree_digest"],
        },
        "toolchain": {
            "image_id": image_id,
            "image_digest": image_digest,
            "lock_sha256": lock_hash,
            "platform": "linux/amd64",
        },
        "installer_ui_profile": INSTALLER_UI_PROFILE,
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


def _screen_state(
    path: Path,
    state: dict[str, object],
) -> tuple[dict[str, object], bytes]:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("screen is missing")
        before = path.stat()
        if before.st_size > 16 * 1024 * 1024:
            raise OSError("screen is oversized")
        payload = path.read_bytes()
        width, height, pixels = decode_png(path)
        after = path.stat()
    except (OSError, ValueError) as exc:
        raise OracleError("installer screen evidence is invalid", exit_code=EXIT_BACKEND) from exc
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or (width, height) != (SCREEN_WIDTH, SCREEN_HEIGHT)
    ):
        raise OracleError("installer screen changed while reading", exit_code=EXIT_BACKEND)
    box = state.get("box")
    if (
        not isinstance(box, list)
        or len(box) != 4
        or not all(isinstance(value, int) for value in box)
    ):
        raise OracleError("invalid installer state crop", exit_code=EXIT_INTEGRITY)
    x0, y0, x1, y1 = box
    if not 0 <= x0 < x1 <= width or not 0 <= y0 < y1 <= height:
        raise OracleError("unsafe installer state crop", exit_code=EXIT_INTEGRITY)
    crop = b"".join(
        pixels[(row * width + x0) * 4 : (row * width + x1) * 4]
        for row in range(y0, y1)
    )
    return (
        {
            "name": state["name"],
            "box": box,
            "title_sha256": hashlib.sha256(crop).hexdigest(),
            "screen_sha256": hashlib.sha256(payload).hexdigest(),
            "screen_bytes": len(payload),
        },
        payload,
    )


def _wait_installer_state(
    screen: Path,
    state: dict[str, object],
    *,
    stop: threading.Event,
    deadline: float,
) -> tuple[dict[str, object], bytes]:
    seen_mtime: int | None = None
    stable = 0
    while monotonic() < deadline and not stop.is_set():
        try:
            evidence, payload = _screen_state(screen, state)
            mtime = screen.stat().st_mtime_ns
        except (OSError, OracleError):
            sleep(0.25)
            continue
        if evidence["title_sha256"] == state["title_sha256"]:
            if mtime != seen_mtime:
                stable += 1
                seen_mtime = mtime
            if stable >= 2:
                return evidence, payload
        else:
            stable = 0
            seen_mtime = None
        sleep(0.25)
    raise OracleError(
        f"installer did not reach expected state: {state['name']}",
        exit_code=EXIT_BACKEND,
    )


def _drive_installer(
    invocation: PodmanInvocation,
    job: Path,
    stop: threading.Event,
) -> dict[str, object]:
    deadline = monotonic() + UI_DRIVER_TIMEOUT_SECONDS
    screen = job / "diagnostics" / "screen-last.png"
    window: str | None = None
    states: list[dict[str, object]] = []
    actions: list[dict[str, object]] = []
    for index, expected in enumerate(INSTALLER_STATES, start=1):
        evidence, payload = _wait_installer_state(
            screen,
            expected,
            stop=stop,
            deadline=deadline,
        )
        snapshot = job / "diagnostics" / f"installer-{index:02d}-{expected['name']}.png"
        atomic_write(snapshot, payload)
        evidence["path"] = snapshot.name
        states.append(evidence)
        if window is None:
            search = exec_podman_checked(
                invocation,
                ("xdotool", "search", "--onlyvisible", "--name", "DOSBox-X"),
                environment={"DISPLAY": ":99"},
            )
            windows = [line for line in str(search["stdout"]).splitlines() if line.isdigit()]
            if search["exit_code"] != 0 or len(windows) != 1:
                raise OracleError("cannot identify the DOSBox-X UI window", exit_code=EXIT_BACKEND)
            window = windows[0]
        keys = expected.get("keys")
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise OracleError("invalid installer key sequence", exit_code=EXIT_INTEGRITY)
        action = exec_podman_checked(
            invocation,
            ("xdotool", "key", "--window", window, "--delay", "200", *keys),
            environment={"DISPLAY": ":99"},
        )
        if action["exit_code"] != 0:
            raise OracleError(
                f"cannot advance installer state: {expected['name']}",
                exit_code=EXIT_BACKEND,
            )
        actions.append(
            {
                "state": expected["name"],
                "keys": keys,
                "exit_code": action["exit_code"],
            }
        )
    ready_metrics, ready_payload = boot_module._wait_for_screen(
        screen,
        stop=stop,
        deadline=deadline,
        predicate=boot_module._is_program_manager_ready,
    )
    ready_path = job / "diagnostics" / "installer-program-manager-ready.png"
    atomic_write(ready_path, ready_payload)
    if window is None:
        raise OracleError("cannot identify the DOSBox-X UI window", exit_code=EXIT_BACKEND)
    exit_key = exec_podman_checked(
        invocation,
        ("xdotool", "key", "--window", window, "alt+F4"),
        environment={"DISPLAY": ":99"},
    )
    if exit_key["exit_code"] != 0:
        raise OracleError("cannot request the Program Manager exit", exit_code=EXIT_BACKEND)
    confirmation_metrics, confirmation_payload = boot_module._wait_for_screen(
        screen,
        stop=stop,
        deadline=deadline,
        predicate=lambda metrics: boot_module._is_exit_confirmation(
            metrics,
            str(ready_metrics["sha256"]),
        ),
    )
    confirmation_path = job / "diagnostics" / "installer-exit-confirmation.png"
    atomic_write(confirmation_path, confirmation_payload)
    confirm_key = exec_podman_checked(
        invocation,
        ("xdotool", "key", "--window", window, "Return"),
        environment={"DISPLAY": ":99"},
    )
    if confirm_key["exit_code"] != 0:
        raise OracleError("cannot confirm the Windows exit", exit_code=EXIT_BACKEND)
    return {
        "schema": AMIPRO_UI_SCHEMA,
        "status": "success",
        "profile": INSTALLER_UI_PROFILE,
        "states": states,
        "actions": actions,
        "program_manager_exit": {
            "profile": boot_module.UI_PROFILE,
            "ready": {"path": ready_path.name, **ready_metrics},
            "confirmation": {
                "path": confirmation_path.name,
                **confirmation_metrics,
            },
            "actions": [
                {"action": "alt-f4", "exit_code": exit_key["exit_code"]},
                {"action": "enter", "exit_code": confirm_key["exit_code"]},
            ],
        },
    }


def _invoke_install_job(
    invocation: PodmanInvocation,
    job: Path,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, object], dict[str, object]]:
    stop = threading.Event()
    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["result"] = _drive_installer(invocation, job, stop)
        except BaseException as exc:
            box["result"] = {
                "schema": AMIPRO_UI_SCHEMA,
                "status": "failure",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    driver = threading.Thread(target=worker, name="amipro-installer-driver", daemon=True)
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
        "schema": AMIPRO_UI_SCHEMA,
        "status": "failure",
        "error_type": "DriverThreadError",
        "error": "installer driver did not return evidence",
    }
    if driver.is_alive():
        driver_result = {
            "schema": AMIPRO_UI_SCHEMA,
            "status": "failure",
            "error_type": "DriverThreadError",
            "error": "installer driver thread did not stop",
        }
    atomic_write_json(job / "ui-driver.json", driver_result)
    if process_error is not None:
        if isinstance(process_error, OracleError):
            process_error.ui_driver = driver_result
        raise process_error
    if process is None:
        raise OracleError("Ami Pro installer process did not return", exit_code=EXIT_BACKEND)
    if driver_result.get("status") != "success":
        error = OracleError(
            f"Ami Pro installer driver failed: {driver_result.get('error', 'unknown error')}",
            exit_code=EXIT_BACKEND,
        )
        error.process_result = process
        error.ui_driver = driver_result
        raise error
    return process, driver_result


def _validate_ui_evidence(job: Path) -> dict[str, object]:
    path = job / "ui-driver.json"
    if path.is_symlink() or not path.is_file():
        raise OracleError("Ami Pro installer UI evidence is missing", exit_code=EXIT_INTEGRITY)
    try:
        driver = read_json_object(path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Ami Pro installer UI evidence is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    states = driver.get("states")
    actions = driver.get("actions")
    program_manager_exit = driver.get("program_manager_exit")
    if (
        driver.get("schema") != AMIPRO_UI_SCHEMA
        or driver.get("status") != "success"
        or driver.get("profile") != INSTALLER_UI_PROFILE
        or not isinstance(states, list)
        or not isinstance(actions, list)
        or len(states) != len(INSTALLER_STATES)
        or len(actions) != len(INSTALLER_STATES)
        or not isinstance(program_manager_exit, dict)
    ):
        raise OracleError("Ami Pro installer UI evidence mismatch", exit_code=EXIT_INTEGRITY)
    for index, expected in enumerate(INSTALLER_STATES, start=1):
        snapshot = job / "diagnostics" / f"installer-{index:02d}-{expected['name']}.png"
        try:
            observed, _ = _screen_state(snapshot, expected)
        except OracleError as exc:
            raise OracleError(
                "Ami Pro installer screenshot is invalid",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        observed["path"] = snapshot.name
        if states[index - 1] != observed or actions[index - 1] != {
            "state": expected["name"],
            "keys": expected["keys"],
            "exit_code": 0,
        }:
            raise OracleError("Ami Pro installer state evidence mismatch", exit_code=EXIT_INTEGRITY)
    ready_path = job / "diagnostics" / "installer-program-manager-ready.png"
    confirmation_path = job / "diagnostics" / "installer-exit-confirmation.png"
    try:
        ready, _ = boot_module._screen_metrics(ready_path)
        confirmation, _ = boot_module._screen_metrics(confirmation_path)
    except OracleError as exc:
        raise OracleError(
            "Ami Pro post-install Windows-exit evidence is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    expected_exit = {
        "profile": boot_module.UI_PROFILE,
        "ready": {"path": ready_path.name, **ready},
        "confirmation": {"path": confirmation_path.name, **confirmation},
        "actions": [
            {"action": "alt-f4", "exit_code": 0},
            {"action": "enter", "exit_code": 0},
        ],
    }
    if (
        not boot_module._is_program_manager_ready(ready)
        or not boot_module._is_exit_confirmation(confirmation, str(ready["sha256"]))
        or program_manager_exit != expected_exit
    ):
        raise OracleError(
            "Ami Pro post-install Windows-exit evidence mismatch",
            exit_code=EXIT_INTEGRITY,
        )
    return driver


def _validate_installed_amipro(runtime: Path) -> dict[str, Any]:
    tree = _validate_windows_tree(runtime)
    required_directories = (
        runtime / "AMIPRO",
        runtime / "AMIPRO" / "DOCS",
        runtime / "AMIPRO" / "DRAWSYM",
        runtime / "AMIPRO" / "ICONS",
        runtime / "AMIPRO" / "MACROS",
        runtime / "AMIPRO" / "STYLES",
        runtime / "WINDOWS" / "LOTUSAPP",
    )
    required_files = (
        runtime / "AMIPRO" / "AMIPRO.EXE",
        runtime / "WINDOWS" / "AMIPRO.INI",
        runtime / "WINDOWS" / "LOTUS.INI",
        runtime / "WINDOWS" / "WIN.INI",
    )
    if any(path.is_symlink() or not path.is_dir() for path in required_directories) or any(
        path.is_symlink() or not path.is_file() for path in required_files
    ):
        raise OracleError("Ami Pro installation topology is incomplete", exit_code=EXIT_BACKEND)
    if sha256_file(required_files[0]) != EXPECTED_AMIPRO_EXE_SHA256:
        raise OracleError("installed AMIPRO.EXE hash mismatch", exit_code=EXIT_INTEGRITY)
    amipro_ini = required_files[1].read_text(encoding="latin-1", errors="strict").casefold()
    lotus_ini = required_files[2].read_text(encoding="latin-1", errors="strict").casefold()
    win_ini = required_files[3].read_text(encoding="latin-1", errors="strict").casefold()
    required_amipro = (
        r"macrodir=c:\amipro\macros",
        r"stypath=c:\amipro\styles",
        r"docpath=c:\amipro\docs",
        r"automacroload=1,_autorun.smm!zrunmacs",
    )
    required_lotus = (
        r"amipro=c:\amipro\amipro.exe",
        r"common directory=c:\windows\lotusapp",
        r"program path=c:\windows\lotusapp\spell",
    )
    if (
        any(value not in amipro_ini for value in required_amipro)
        or any(value not in lotus_ini for value in required_lotus)
        or r"sam=c:\amipro\amipro.exe ^.sam" not in win_ini
        or any(path.name.casefold().startswith("lotustmp.") for path in runtime.iterdir())
    ):
        raise OracleError("Ami Pro installer side effects are incomplete", exit_code=EXIT_BACKEND)
    return tree


def _validate_install_return(runtime: Path) -> None:
    success = runtime / "AMIINST.OK"
    failure = runtime / "AMIINST.ERR"
    if (
        success.is_symlink()
        or not success.is_file()
        or success.read_bytes() != b"INSTALL_RETURNED_ZERO\r\n"
        or failure.exists()
        or failure.is_symlink()
    ):
        raise OracleError("Ami Pro installer return sentinel is invalid", exit_code=EXIT_BACKEND)


def _remove_install_controls(runtime: Path) -> None:
    for name in ("AMIINST.OK", "AMIINST.ERR", "AMIINST.BAT"):
        path = runtime / name
        if path.is_symlink():
            raise OracleError("installer control path became a symlink", exit_code=EXIT_INTEGRITY)
        path.unlink(missing_ok=True)


def _write_attempt(job: Path, inputs: dict[str, object], machine: StateMachine) -> None:
    atomic_write_json(
        job / "attempt.json",
        {
            "schema": "amipro-oracle-amipro-install-attempt-v1",
            "phase": "amipro-install",
            "inputs_digest": digest_json(inputs),
            "state": machine.state,
            "state_trace": machine.trace,
        },
    )


def _result(
    checkpoint: dict[str, Any],
    *,
    cache_reused: bool,
    evidence_job: str,
) -> dict[str, Any]:
    return {
        "schema": AMIPRO_RESULT_SCHEMA,
        "status": checkpoint["status"],
        "checkpoint_key": checkpoint["checkpoint_key"],
        "parent_runtime_key": checkpoint["parent_runtime_key"],
        "cache_reused": cache_reused,
        "evidence_job": evidence_job,
        "checkpoint": checkpoint,
    }


def _load_evidence(
    home: Path,
    root: Path,
    key: str,
    checkpoint: dict[str, Any],
) -> str:
    receipt_path = root / "evidence-receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise OracleError("Ami Pro install evidence receipt is missing", exit_code=EXIT_INTEGRITY)
    try:
        receipt = read_json_object(receipt_path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Ami Pro install evidence receipt is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    job_name = receipt.get("evidence_job")
    result_hash = receipt.get("result_sha256")
    if (
        set(receipt) != {"schema", "checkpoint_key", "evidence_job", "result_sha256"}
        or receipt.get("schema") != "amipro-oracle-amipro-install-evidence-v1"
        or receipt.get("checkpoint_key") != key
        or not isinstance(job_name, str)
        or re.fullmatch(r"install-amipro-[a-z0-9_-]+", job_name) is None
        or not isinstance(result_hash, str)
        or _SHA256.fullmatch(result_hash) is None
    ):
        raise OracleError("Ami Pro install evidence receipt mismatch", exit_code=EXIT_INTEGRITY)
    job = home / "jobs" / job_name
    result_path = job / "result.json"
    if (
        job.is_symlink()
        or not job.is_dir()
        or result_path.is_symlink()
        or not result_path.is_file()
        or sha256_file(result_path) != result_hash
    ):
        raise OracleError("Ami Pro install evidence result mismatch", exit_code=EXIT_INTEGRITY)
    try:
        recorded = read_json_object(result_path)
        observer = _validate_observer_evidence(job / "diagnostics")
        driver = _validate_ui_evidence(job)
    except (OSError, ValueError, OracleError) as exc:
        raise OracleError("Ami Pro install evidence is invalid", exit_code=EXIT_INTEGRITY) from exc
    process = recorded.get("process_result")
    trace = recorded.get("state_trace")
    if (
        recorded.get("schema") != AMIPRO_RESULT_SCHEMA
        or recorded.get("status") != "amipro-install-candidate"
        or recorded.get("checkpoint_key") != key
        or recorded.get("cache_reused") is not False
        or recorded.get("evidence_job") != job_name
        or recorded.get("checkpoint") != checkpoint
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
        raise OracleError("Ami Pro install evidence identity mismatch", exit_code=EXIT_INTEGRITY)
    return job_name


def _unsealed_tree(sealed: dict[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, object]] = []
    for raw in sealed.get("entries", []):
        if not isinstance(raw, dict) or raw.get("type") not in {"directory", "file"}:
            raise OracleError("Ami Pro sealed tree is invalid", exit_code=EXIT_INTEGRITY)
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
            {"schema": bootstrap_module.RUNTIME_TREE_SCHEMA, "entries": entries}
        ),
    }


def _verify_checkpoint(
    home: Path,
    root: Path,
    key: str,
    inputs: dict[str, object],
) -> tuple[dict[str, Any], str]:
    paths = {
        "runtime": root / "runtime.json",
        "inputs": root / "inputs.json",
        "guest_tree": root / "guest-tree.json",
        "sealed_tree": root / "sealed-tree.json",
        "config": root / "dosbox-x.conf",
        "batch": root / "AMIINST.BAT",
        "receipt": root / "evidence-receipt.json",
    }
    pristine = root / "pristine-c"
    expected_names = {
        "AMIINST.BAT",
        "dosbox-x.conf",
        "evidence-receipt.json",
        "guest-tree.json",
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
        raise OracleError("Ami Pro install cache has an unsafe shape", exit_code=EXIT_INTEGRITY)
    try:
        checkpoint = read_json_object(paths["runtime"])
        recorded_inputs = read_json_object(paths["inputs"])
        guest_tree = read_json_object(paths["guest_tree"])
        recorded_sealed = read_json_object(paths["sealed_tree"])
        config_bytes = paths["config"].read_bytes()
        batch_bytes = paths["batch"].read_bytes()
    except (OSError, ValueError) as exc:
        raise OracleError(
            "Ami Pro install cache manifest is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    sealed = _inventory_windows_runtime(pristine)
    expected_guest = _unsealed_tree(sealed)
    expected_keys = {
        "backend",
        "baseline_eligible",
        "checkpoint_key",
        "checkpoint_role",
        "guest_tree_digest",
        "guest_tree_manifest_digest",
        "inputs_digest",
        "parent_runtime_key",
        "printer_profile",
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
        set(checkpoint) != expected_keys
        or checkpoint.get("schema") != AMIPRO_CHECKPOINT_SCHEMA
        or checkpoint.get("runtime_schema") != RUNTIME_SCHEMA
        or checkpoint.get("backend") != "real"
        or checkpoint.get("baseline_eligible") is not False
        or checkpoint.get("status") != "amipro-install-candidate"
        or checkpoint.get("checkpoint_role") != "requires-separate-amipro-launch-probe"
        or checkpoint.get("checkpoint_key") != key
        or checkpoint.get("inputs_digest") != digest_json(inputs)
        or key != digest_json(inputs)
        or checkpoint.get("parent_runtime_key")
        != inputs.get("windows_runtime", {}).get("runtime_key")
        or checkpoint.get("printer_profile") != "none"
        or recorded_inputs != inputs
        or config_bytes != amipro_install_config().encode("utf-8")
        or batch_bytes != amipro_install_batch()
        or hashlib.sha256(config_bytes).hexdigest() != inputs.get("dosbox_config_sha256")
        or hashlib.sha256(batch_bytes).hexdigest() != inputs.get("install_batch_sha256")
        or guest_tree != expected_guest
        or guest_tree.get("digest") != checkpoint.get("guest_tree_digest")
        or digest_json(guest_tree) != checkpoint.get("guest_tree_manifest_digest")
        or recorded_sealed != sealed
        or sealed.get("digest") != checkpoint.get("sealed_tree_digest")
        or digest_json(sealed) != checkpoint.get("sealed_tree_manifest_digest")
        or sealed.get("file_count") != checkpoint.get("tree_file_count")
        or sealed.get("directory_count") != checkpoint.get("tree_directory_count")
        or sealed.get("total_bytes") != checkpoint.get("tree_total_bytes")
        or any(int(str(entry["mode"]), 8) & 0o222 for entry in sealed["entries"])
    ):
        raise OracleError("Ami Pro install cache identity mismatch", exit_code=EXIT_INTEGRITY)
    _validate_installed_amipro(pristine)
    evidence_job = _load_evidence(home, root, key, checkpoint)
    return checkpoint, evidence_job


def install_amipro_checkpoint(
    home: Path,
    media_root: Path,
    media: dict[str, Any],
    image_record: dict[str, Any],
    *,
    runtime_key: str | None = None,
    timeout_seconds: float = OUTER_TIME_LIMIT_SECONDS,
) -> dict[str, Any]:
    _require_verified_image(image_record)
    _ensure_private_directories(home)
    source, flat_media = ensure_flat_amipro_media(home, media_root, media)
    parent_root, windows_runtime, _windows_inputs, _windows_evidence = _select_windows_ready(
        home,
        runtime_key,
    )
    inputs = amipro_install_inputs(
        windows_runtime,
        media,
        flat_media,
        image_record,
        outer_time_limit_seconds=timeout_seconds,
    )
    key = digest_json(inputs)
    parent_key = str(windows_runtime["runtime_key"])
    flat_key = str(flat_media["cache_key"])
    cache_parent = home / "cache" / "amipro"
    if cache_parent.is_symlink():
        raise OracleError("Ami Pro cache parent is unsafe", exit_code=EXIT_INTEGRITY)
    if not cache_parent.exists():
        cache_parent.mkdir(mode=0o700)
    elif not cache_parent.is_dir():
        raise OracleError("Ami Pro cache parent is not a directory", exit_code=EXIT_INTEGRITY)
    cache_parent.chmod(0o700)
    final = cache_parent / key
    identity = _media_identity(media)
    with _cache_lock(home, parent_key), _cache_lock(home, flat_key), _cache_lock(home, key):
        parent_root, checked_windows, _windows_inputs, _windows_evidence = (
            _select_windows_ready(home, parent_key)
        )
        checked_flat = _verify_flat_cache(
            source.parent,
            expected_key=flat_key,
            expected_identity=identity,
        )
        checked_inputs = amipro_install_inputs(
            checked_windows,
            media,
            checked_flat,
            image_record,
            outer_time_limit_seconds=timeout_seconds,
        )
        if checked_inputs != inputs:
            raise OracleError(
                "Ami Pro installer inputs changed after keying",
                exit_code=EXIT_INTEGRITY,
            )
        if final.exists() or final.is_symlink():
            checkpoint, evidence_job = _verify_checkpoint(home, final, key, inputs)
            return _result(
                checkpoint,
                cache_reused=True,
                evidence_job=evidence_job,
            )
        job = Path(tempfile.mkdtemp(prefix=f"install-amipro-{key[:12]}-", dir=home / "jobs"))
        job.chmod(0o700)
        _directory_fsync(home / "jobs")
        for name in ("capture", "diagnostics", "home"):
            (job / name).mkdir(mode=0o700)
        runtime = job / "runtime"
        shutil.copytree(parent_root / "pristine-c", runtime, copy_function=shutil.copy2)
        _normalize_runtime_metadata(runtime)
        config_bytes = amipro_install_config().encode("utf-8")
        batch_bytes = amipro_install_batch()
        atomic_write(runtime / "AMIINST.BAT", batch_bytes)
        atomic_write(job / "dosbox-x.conf", config_bytes)
        atomic_write_json(job / "inputs.json", inputs)
        machine = StateMachine(
            initial="created",
            terminal=frozenset({"installed", "failed"}),
            transitions={
                "created": frozenset({"staged", "failed"}),
                "staged": frozenset({"guest-invoked", "failed"}),
                "guest-invoked": frozenset({"guest-returned", "failed"}),
                "guest-returned": frozenset({"validated", "failed"}),
                "validated": frozenset({"installed", "failed"}),
            },
        )
        machine.advance("staged", evidence="inputs.json")
        _write_attempt(job, inputs, machine)
        control = home / "control" / job.name
        control.mkdir(mode=0o700)
        suffix = re.sub(r"[^a-z0-9]", "", job.name[-10:].casefold())
        invocation = build_podman_invocation(
            image_record,
            container_name=f"amipro-oracle-install-{suffix}",
            oracle_root=home,
            job_root=job,
            control_root=control,
            phase="bootstrap",
            mounts=[
                BindMount(job, "/oracle/job", read_only=False),
                BindMount(source, "/oracle/media/amipro", read_only=True),
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
            process, ui_driver = _invoke_install_job(
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
                    "Ami Pro installer container did not exit cleanly",
                    exit_code=EXIT_BACKEND,
                )
                error.process_result = process
                raise error
            observer = _validate_observer_evidence(job / "diagnostics")
            validated_driver = _validate_ui_evidence(job)
            if ui_driver != validated_driver:
                raise OracleError("installer UI evidence changed", exit_code=EXIT_INTEGRITY)
            _validate_install_return(runtime)
            machine.advance("guest-returned", evidence="AMIINST.OK")
            _write_attempt(job, inputs, machine)
            _select_windows_ready(home, parent_key)
            post_flat = _verify_flat_cache(
                source.parent,
                expected_key=flat_key,
                expected_identity=identity,
            )
            if post_flat != flat_media:
                raise OracleError(
                    "Ami Pro media cache changed during install",
                    exit_code=EXIT_INTEGRITY,
                )
            raw_tree = _validate_installed_amipro(runtime)
            atomic_write_json(job / "raw-tree.json", raw_tree)
            _remove_install_controls(runtime)
            _normalize_runtime_metadata(runtime)
            guest_tree = _validate_installed_amipro(runtime)
            atomic_write_json(job / "guest-tree.json", guest_tree)
            machine.advance("validated", evidence="guest-tree.json")
            _write_attempt(job, inputs, machine)

            promotion = Path(
                tempfile.mkdtemp(
                    prefix=f".{key}.",
                    suffix=".staging",
                    dir=cache_parent,
                )
            )
            promotion.chmod(0o700)
            shutil.copytree(runtime, promotion / "pristine-c", copy_function=shutil.copy2)
            _make_tree_read_only(promotion / "pristine-c")
            sealed_tree = _validate_installed_amipro(promotion / "pristine-c")
            checkpoint: dict[str, Any] = {
                "schema": AMIPRO_CHECKPOINT_SCHEMA,
                "runtime_schema": RUNTIME_SCHEMA,
                "backend": "real",
                "baseline_eligible": False,
                "status": "amipro-install-candidate",
                "checkpoint_role": "requires-separate-amipro-launch-probe",
                "checkpoint_key": key,
                "parent_runtime_key": parent_key,
                "inputs_digest": digest_json(inputs),
                "guest_tree_digest": guest_tree["digest"],
                "guest_tree_manifest_digest": digest_json(guest_tree),
                "sealed_tree_digest": sealed_tree["digest"],
                "sealed_tree_manifest_digest": digest_json(sealed_tree),
                "tree_file_count": sealed_tree["file_count"],
                "tree_directory_count": sealed_tree["directory_count"],
                "tree_total_bytes": sealed_tree["total_bytes"],
                "printer_profile": "none",
            }
            atomic_write_json(promotion / "runtime.json", checkpoint)
            atomic_write_json(promotion / "inputs.json", inputs)
            atomic_write_json(promotion / "guest-tree.json", guest_tree)
            atomic_write_json(promotion / "sealed-tree.json", sealed_tree)
            atomic_write(promotion / "dosbox-x.conf", config_bytes)
            atomic_write(promotion / "AMIINST.BAT", batch_bytes)
            evidence_result = {
                **_result(checkpoint, cache_reused=False, evidence_job=job.name),
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
                    "schema": "amipro-oracle-amipro-install-evidence-v1",
                    "checkpoint_key": key,
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
            _directory_fsync(cache_parent)
            verified, evidence_job = _verify_checkpoint(home, final, key, inputs)
            machine.advance("installed", evidence=f"cache/amipro/{key}/runtime.json")
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
                    promotion_evidence = f"cache/amipro/{promotion.name}"
            if machine.state != "failed" and "failed" in machine.transitions.get(
                machine.state,
                frozenset(),
            ):
                machine.advance("failed", evidence="failure.json")
            failure: dict[str, object] = {
                "schema": "amipro-oracle-amipro-install-failure-v1",
                "phase": "amipro-install",
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
