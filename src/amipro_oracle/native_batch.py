from __future__ import annotations

import hashlib
import math
import re
import shutil
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from time import monotonic
from typing import Any

from . import amipro_install as install_module
from . import amipro_launch_probe as launch_module
from . import postscript_smoke as smoke_module
from .batch import NATIVE_SAM_AUDIT_SCHEMA, _read_bounded_file, read_and_audit_source
from .config import DOSBOX_PROFILE, dosbox_config
from .constants import ANALYSIS_SCHEMA, EXIT_BACKEND, EXIT_INTEGRITY, JOB_SCHEMA
from .errors import OracleError
from .io import atomic_write, atomic_write_json, digest_json, read_json_object, sha256_file
from .oci import BindMount, PodmanInvocation, build_podman_invocation
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

NATIVE_DOCUMENT_INPUT_SCHEMA = "amipro-oracle-native-document-input-v1"
NATIVE_DOCUMENT_RESULT_SCHEMA = "amipro-oracle-native-document-result-v1"
NATIVE_DOCUMENT_UI_SCHEMA = "amipro-oracle-native-document-ui-v1"
NATIVE_POSTSCRIPT_SCHEMA = "amipro-oracle-native-postscript-v1"

MAX_PAGES = 128
MAX_NATIVE_POSTSCRIPT_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_BBOX_BYTES = 32 * 1024 * 1024
MAX_WORD_BOXES = 500_000
RASTER_DPI = 144
PRINT_DIALOG_ATTEMPTS = 3
PRINT_DIALOG_ATTEMPT_SECONDS = 5.0
EDITOR_RECONFIRM_SECONDS = 2.0

EDITOR_MENU_STATE = {
    "name": "amipro-editor-menu",
    "box": [192, 220, 832, 240],
    "title_sha256": "ac38d32894748570ea0e50690cac64535b42d92d0f6072544111ac5fac4f7017",
}
NATIVE_DOCUMENT_PROFILE = {
    "name": "amipro-3.1-native-document-postscript-v1",
    "screen_width": install_module.SCREEN_WIDTH,
    "screen_height": install_module.SCREEN_HEIGHT,
    "autolock": False,
    "stable_samples": 2,
    "poll_seconds": 0.25,
    "print_dialog_attempts": PRINT_DIALOG_ATTEMPTS,
    "print_dialog_attempt_seconds": PRINT_DIALOG_ATTEMPT_SECONDS,
    "states": [
        EDITOR_MENU_STATE,
        smoke_module.PRINT_DIALOG_STATE,
        EDITOR_MENU_STATE,
        launch_module.PROGRAM_MANAGER_MINIMIZED_STATE,
        install_module.EXIT_WINDOWS_STATE,
    ],
    "actions": [
        "open-print-dialog",
        "confirm-default-print",
        "wait-for-lpt-closure",
        "close-document-and-amipro",
        "exit-windows",
        "confirm-exit-windows",
    ],
}
ANALYSIS_PROFILE = {
    "id": "amipro31-pscript35-qms100-poppler144-native-batch-v1",
    "raster_dpi": RASTER_DPI,
    "whitespace": "pdftotext-page-text-trailing-newlines-trimmed",
    "ghostscript_device": "pdfwrite",
    "pdf_compatibility": "1.4",
    "poppler_renderer": "pdftocairo-png",
}

_PAGE_LINE = re.compile(r"^%%Page: .*? ([0-9]+)\r$", re.M)
_TRAILER_PAGES = re.compile(r"^%%Pages: ([0-9]+)\r$", re.M)
_BOUNDING_BOX = re.compile(
    r"^%%BoundingBox: (-?[0-9]+) (-?[0-9]+) (-?[0-9]+) (-?[0-9]+)\r$",
    re.M,
)
_PNG_NAME = re.compile(r"page-([0-9]+)[.]png\Z")


def validate_native_batch_prerequisites(
    home: Path,
    image_record: dict[str, Any],
    runtime_key: str | None,
) -> str:
    """Fail before creating a large batch if its global runtime is unavailable."""
    _require_verified_image(image_record)
    _root, ready, _inputs, _evidence = smoke_module._select_printer_runtime(
        home,
        runtime_key,
    )
    return str(ready["runtime_key"])


def native_document_config() -> str:
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


def native_document_batch(guest_name: str) -> bytes:
    if re.fullmatch(r"DOC[0-9]{5}[.]SAM", guest_name) is None:
        raise OracleError("invalid native batch guest name", exit_code=EXIT_INTEGRITY)
    document = rf"C:\ORACLE\{guest_name}"
    lines = (
        "@ECHO OFF",
        r"IF EXIST C:\PRTSMK.STA DEL C:\PRTSMK.STA",
        r"IF EXIST C:\PRTSMK.OK DEL C:\PRTSMK.OK",
        r"IF EXIST C:\PRTSMK.ERR DEL C:\PRTSMK.ERR",
        r"IF NOT EXIST C:\AMIPRO\AMIPRO.EXE GOTO AMIPRO_MISSING",
        f"IF NOT EXIST {document} GOTO DOCUMENT_MISSING",
        r"ECHO POSTSCRIPT_LAUNCH_REQUESTED>C:\PRTSMK.STA",
        rf"C:\WINDOWS\WIN.COM C:\AMIPRO\AMIPRO.EXE {document}",
        "IF ERRORLEVEL 1 GOTO DOCUMENT_FAILED",
        r"ECHO POSTSCRIPT_RETURNED_ZERO>C:\PRTSMK.OK",
        "GOTO DOCUMENT_DONE",
        ":AMIPRO_MISSING",
        r"ECHO AMIPRO_EXE_MISSING>C:\PRTSMK.ERR",
        "GOTO DOCUMENT_DONE",
        ":DOCUMENT_MISSING",
        r"ECHO BATCH_DOCUMENT_MISSING>C:\PRTSMK.ERR",
        "GOTO DOCUMENT_DONE",
        ":DOCUMENT_FAILED",
        r"ECHO DOCUMENT_ERRORLEVEL_NONZERO>C:\PRTSMK.ERR",
        ":DOCUMENT_DONE",
        "EXIT",
    )
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def _native_inputs(
    ready: dict[str, Any],
    audit: dict[str, object],
    image_record: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, object]:
    if audit.get("schema") != NATIVE_SAM_AUDIT_SCHEMA:
        raise OracleError("invalid native document audit", exit_code=EXIT_INTEGRITY)
    config = native_document_config().encode("utf-8")
    batch = native_document_batch(str(audit["guest_name"]))
    return {
        "schema": NATIVE_DOCUMENT_INPUT_SCHEMA,
        "printer_ready": {
            "runtime_key": ready["runtime_key"],
            "manifest_digest": digest_json(ready),
            "sealed_tree_digest": ready["sealed_tree_digest"],
            "printer_identity_digest": digest_json(ready["printer_identity"]),
        },
        "source": audit,
        "toolchain": {
            "image_id": image_record["image_id"],
            "image_digest": image_record["image_digest"],
            "lock_sha256": image_record["lock_sha256"],
            "platform": image_record["platform"],
        },
        "driver_profile": NATIVE_DOCUMENT_PROFILE,
        "analysis_profile": ANALYSIS_PROFILE,
        "dosbox_profile": DOSBOX_PROFILE,
        "dosbox_config_sha256": hashlib.sha256(config).hexdigest(),
        "batch_sha256": hashlib.sha256(batch).hexdigest(),
        "orchestrator_sha256": sha256_file(Path(__file__)),
        "guest_clock": {"date_command": GUEST_DATE, "time_command": GUEST_TIME},
        "reported_free_mb": WINDOWS_FREE_MB,
        "outer_time_limit_seconds": timeout_seconds,
    }


def _drive_native_lifecycle(
    invocation: PodmanInvocation,
    job: Path,
    stop: threading.Event,
    *,
    timeout_seconds: float,
) -> dict[str, object]:
    deadline = monotonic() + max(1.0, timeout_seconds - 5.0)
    smoke_module._wait_sentinel(job / "runtime", stop, deadline)
    before = smoke_module._capture_exact_state(
        job,
        EDITOR_MENU_STATE,
        "document-before-print.png",
        stop=stop,
        deadline=deadline,
    )
    search = smoke_module.exec_podman_checked(
        invocation,
        ("xdotool", "search", "--onlyvisible", "--name", "DOSBox-X"),
        environment={"DISPLAY": ":99"},
    )
    windows = [line for line in str(search["stdout"]).splitlines() if line.isdigit()]
    if search["exit_code"] != 0 or len(windows) != 1:
        raise OracleError("cannot identify the DOSBox-X UI window", exit_code=EXIT_BACKEND)
    window = windows[0]
    actions: list[dict[str, object]] = []

    def send_key(action: str, key: str) -> dict[str, object]:
        result = smoke_module.exec_podman_checked(
            invocation,
            ("xdotool", "key", "--window", window, key),
            environment={"DISPLAY": ":99"},
        )
        if result["exit_code"] != 0:
            raise OracleError(f"cannot perform UI action: {action}", exit_code=EXIT_BACKEND)
        return {"action": action, "key": key, "exit_code": 0}

    def press(action: str, key: str) -> None:
        actions.append(send_key(action, key))

    dialog: dict[str, object] | None = None
    last_dialog_error: OracleError | None = None
    for attempt in range(1, PRINT_DIALOG_ATTEMPTS + 1):
        send_key("open-print-dialog", "ctrl+p")
        try:
            dialog = smoke_module._capture_exact_state(
                job,
                smoke_module.PRINT_DIALOG_STATE,
                "print-dialog.png",
                stop=stop,
                deadline=min(deadline, monotonic() + PRINT_DIALOG_ATTEMPT_SECONDS),
            )
        except OracleError as exc:
            last_dialog_error = exc
            try:
                install_module._wait_installer_state(
                    job / "diagnostics" / "screen-last.png",
                    EDITOR_MENU_STATE,
                    stop=stop,
                    deadline=min(deadline, monotonic() + EDITOR_RECONFIRM_SECONDS),
                )
            except OracleError:
                raise exc
            continue
        actions.append(
            {
                "action": "open-print-dialog",
                "key": "ctrl+p",
                "exit_code": 0,
                "attempt_count": attempt,
            }
        )
        break
    if dialog is None:
        if last_dialog_error is None:
            raise OracleError("print dialog retry failed", exit_code=EXIT_BACKEND)
        raise last_dialog_error
    press("confirm-default-print", "Return")
    capture = smoke_module._wait_capture_closed(
        job,
        stop,
        deadline,
        maximum=MAX_NATIVE_POSTSCRIPT_BYTES,
    )
    actions.append({"action": "wait-for-lpt-closure", **capture})
    after = smoke_module._capture_exact_state(
        job,
        EDITOR_MENU_STATE,
        "document-after-print.png",
        stop=stop,
        deadline=deadline,
    )
    press("close-document-and-amipro", "alt+F4")
    minimized = smoke_module._capture_exact_state(
        job,
        launch_module.PROGRAM_MANAGER_MINIMIZED_STATE,
        "print-program-manager-minimized.png",
        stop=stop,
        deadline=deadline,
    )
    press("exit-windows", "alt+F4")
    confirmation = smoke_module._capture_exact_state(
        job,
        install_module.EXIT_WINDOWS_STATE,
        "print-exit-windows-confirmation.png",
        stop=stop,
        deadline=deadline,
    )
    press("confirm-exit-windows", "Return")
    return {
        "schema": NATIVE_DOCUMENT_UI_SCHEMA,
        "status": "success",
        "profile": NATIVE_DOCUMENT_PROFILE,
        "states": [before, dialog, after, minimized, confirmation],
        "actions": actions,
    }


def _validate_ui(job: Path, driver: dict[str, object]) -> None:
    path = job / "ui-driver.json"
    if path.is_symlink() or not path.is_file():
        raise OracleError("native document UI evidence is missing", exit_code=EXIT_INTEGRITY)
    try:
        stored = read_json_object(path)
    except (OSError, ValueError) as exc:
        raise OracleError(
            "native document UI evidence is invalid",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    if stored != driver:
        raise OracleError("native document UI evidence changed", exit_code=EXIT_INTEGRITY)
    actions = driver.get("actions")
    if (
        driver.get("schema") != NATIVE_DOCUMENT_UI_SCHEMA
        or driver.get("status") != "success"
        or driver.get("profile") != NATIVE_DOCUMENT_PROFILE
        or not isinstance(driver.get("states"), list)
        or not isinstance(actions, list)
        or len(actions) != 6
        or any(not isinstance(item, dict) for item in actions)
        or [item.get("action") for item in actions]
        != NATIVE_DOCUMENT_PROFILE["actions"]
    ):
        raise OracleError("native document UI evidence mismatch", exit_code=EXIT_INTEGRITY)
    observed: list[dict[str, object]] = []
    for state, name in (
        (EDITOR_MENU_STATE, "document-before-print.png"),
        (smoke_module.PRINT_DIALOG_STATE, "print-dialog.png"),
        (EDITOR_MENU_STATE, "document-after-print.png"),
        (launch_module.PROGRAM_MANAGER_MINIMIZED_STATE, "print-program-manager-minimized.png"),
        (install_module.EXIT_WINDOWS_STATE, "print-exit-windows-confirmation.png"),
    ):
        try:
            evidence, _payload = install_module._screen_state(job / "diagnostics" / name, state)
        except OracleError as exc:
            raise OracleError(
                "native document lifecycle screenshot is invalid",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        evidence["path"] = name
        observed.append(evidence)
    if driver["states"] != observed:
        raise OracleError("native document lifecycle screenshots changed", exit_code=EXIT_INTEGRITY)
    capture = actions[2]
    if (
        capture.get("lpt_close_observed") is not True
        or capture.get("stable_seconds") != 3
        or type(capture.get("size")) is not int
        or not 1 <= capture["size"] <= MAX_NATIVE_POSTSCRIPT_BYTES
    ):
        raise OracleError(
            "native document capture evidence is invalid",
            exit_code=EXIT_INTEGRITY,
        )


def validate_native_postscript(
    payload: bytes,
    *,
    guest_name: str,
) -> tuple[bytes, dict[str, object]]:
    if not 1 <= len(payload) <= MAX_NATIVE_POSTSCRIPT_BYTES:
        raise OracleError("raw PostScript size is outside its bound", exit_code=EXIT_INTEGRITY)
    leading_eot = payload.startswith(b"\x04")
    trailing_eot = payload.endswith(b"\x04")
    sanitized = payload[1:] if leading_eot else payload
    sanitized = sanitized[:-1] if trailing_eot else sanitized
    if b"\x04" in sanitized:
        raise OracleError("PostScript contains an interior EOT byte", exit_code=EXIT_INTEGRITY)
    if not sanitized.startswith(b"%!PS-Adobe-3.0\r\n") or not sanitized.endswith(b"%%EOF\r\n"):
        raise OracleError("PostScript envelope is invalid", exit_code=EXIT_INTEGRITY)
    if b"\r" in sanitized.replace(b"\r\n", b"") or b"\n" in sanitized.replace(
        b"\r\n", b""
    ):
        raise OracleError(
            "PostScript line endings are not canonical CRLF",
            exit_code=EXIT_INTEGRITY,
        )
    try:
        text = sanitized.decode("ascii")
    except UnicodeDecodeError as exc:
        raise OracleError("PostScript is not bounded ASCII", exit_code=EXIT_INTEGRITY) from exc
    title = f"Ami Pro - {guest_name}"
    if (
        "%%Creator: Windows PSCRIPT\r\n" not in text
        or f"%%Title: {title}\r\n" not in text
        or "%%Pages: (atend)\r\n" not in text
    ):
        raise OracleError("PostScript DSC identity is invalid", exit_code=EXIT_INTEGRITY)
    page_ordinals = [int(value) for value in _PAGE_LINE.findall(text)]
    trailer_pages = [int(value) for value in _TRAILER_PAGES.findall(text)]
    if (
        len(trailer_pages) != 1
        or not 1 <= trailer_pages[0] <= MAX_PAGES
        or page_ordinals != list(range(1, trailer_pages[0] + 1))
    ):
        raise OracleError("PostScript page inventory is invalid", exit_code=EXIT_INTEGRITY)
    matches = _BOUNDING_BOX.findall(text)
    if len(matches) != 1:
        raise OracleError("PostScript bounding box inventory is invalid", exit_code=EXIT_INTEGRITY)
    bounding_box = [int(value) for value in matches[0]]
    x0, y0, x1, y1 = bounding_box
    if not (-10_000 <= x0 < x1 <= 10_000 and -10_000 <= y0 < y1 <= 10_000):
        raise OracleError("PostScript bounding box is invalid", exit_code=EXIT_INTEGRITY)
    identity = {
        "schema": NATIVE_POSTSCRIPT_SCHEMA,
        "raw_size": len(payload),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "leading_eot_removed": leading_eot,
        "trailing_eot_removed": trailing_eot,
        "sanitized_size": len(sanitized),
        "sanitized_sha256": hashlib.sha256(sanitized).hexdigest(),
        "dsc_version": "3.0",
        "creator": "Windows PSCRIPT",
        "title": title,
        "pages": trailer_pages[0],
        "bounding_box": bounding_box,
        "line_endings": "CRLF",
    }
    return sanitized, identity


def _pdfinfo(payload: bytes, *, guest_name: str, pages: int) -> dict[str, str]:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise OracleError("pdfinfo output is invalid", exit_code=EXIT_INTEGRITY) from exc
    values: dict[str, str] = {}
    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    required = {
        "Title": f"Ami Pro - {guest_name}",
        "Creator": "Windows PSCRIPT",
        "Producer": "GPL Ghostscript 10.00.0",
        "Pages": str(pages),
        "Encrypted": "no",
        "JavaScript": "no",
        "PDF version": "1.4",
    }
    if any(values.get(key) != value for key, value in required.items()):
        raise OracleError("derived PDF identity is invalid", exit_code=EXIT_INTEGRITY)
    return required


def _bbox_pages(path: Path, expected_pages: int) -> list[dict[str, object]]:
    smoke_module._bounded_regular(path, "bounding-box XML", MAX_BBOX_BYTES)
    try:
        root = ET.fromstring(path.read_bytes())
    except (ET.ParseError, OSError) as exc:
        raise OracleError("bounding-box XML is invalid", exit_code=EXIT_INTEGRITY) from exc
    namespace = {"x": "http://www.w3.org/1999/xhtml"}
    elements = root.findall(".//x:page", namespace)
    if len(elements) != expected_pages:
        raise OracleError("bounding-box page count changed", exit_code=EXIT_INTEGRITY)
    pages: list[dict[str, object]] = []
    total_words = 0
    for number, element in enumerate(elements, start=1):
        try:
            width = float(element.attrib["width"])
            height = float(element.attrib["height"])
        except (KeyError, ValueError) as exc:
            raise OracleError(
                "bounding-box page geometry is invalid",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        if not (
            math.isfinite(width)
            and math.isfinite(height)
            and 1 <= width <= 10_000
            and 1 <= height <= 10_000
        ):
            raise OracleError("bounding-box page geometry is invalid", exit_code=EXIT_INTEGRITY)
        boxes: list[dict[str, object]] = []
        for word in element.findall(".//x:word", namespace):
            total_words += 1
            if total_words > MAX_WORD_BOXES:
                raise OracleError(
                    "bounding-box word count is outside its bound",
                    exit_code=EXIT_INTEGRITY,
                )
            try:
                coordinates = [
                    float(word.attrib[name]) for name in ("xMin", "yMin", "xMax", "yMax")
                ]
            except (KeyError, ValueError) as exc:
                raise OracleError("word bounding box is invalid", exit_code=EXIT_INTEGRITY) from exc
            x0, y0, x1, y1 = coordinates
            if (
                any(not math.isfinite(value) for value in coordinates)
                or not 0 <= x0 < x1 <= width
                or not 0 <= y0 < y1 <= height
            ):
                raise OracleError("word bounding box is outside its page", exit_code=EXIT_INTEGRITY)
            boxes.append(
                {
                    "text": word.text or "",
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                }
            )
        pages.append(
            {
                "number": number,
                "width_pt": width,
                "height_pt": height,
                "text_boxes": boxes,
                "image_boxes": [],
            }
        )
    return pages


def _text_pages(path: Path, expected_pages: int) -> list[str]:
    smoke_module._bounded_regular(path, "plain text", MAX_TEXT_BYTES)
    try:
        values = path.read_bytes().decode("utf-8").split("\f")
    except UnicodeDecodeError as exc:
        raise OracleError("derived PDF text is invalid", exit_code=EXIT_INTEGRITY) from exc
    if values and values[-1] == "":
        values.pop()
    values = [value.rstrip("\r\n") for value in values]
    if len(values) != expected_pages:
        raise OracleError("derived PDF text page count changed", exit_code=EXIT_INTEGRITY)
    return values


def _derive_outputs(
    home: Path,
    image_record: dict[str, Any],
    job: Path,
    *,
    guest_name: str,
    postscript: dict[str, object],
) -> tuple[dict[str, Any], dict[str, object]]:
    output = job / "output"
    tools: dict[str, object] = {}
    tools["ghostscript"] = smoke_module._run_tool(
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
    smoke_module._bounded_regular(pdf, "PDF", smoke_module.MAX_DERIVED_BYTES)
    tools["pdfinfo"] = smoke_module._run_tool(
        home,
        image_record,
        job,
        name="pdfinfo",
        entrypoint="/usr/bin/pdfinfo",
        arguments=["/oracle/job/document.pdf"],
    )
    pdfinfo_bytes = (job / "diagnostics" / "pdfinfo.stdout.log").read_bytes()
    atomic_write(output / "pdfinfo.txt", pdfinfo_bytes)
    pdf_identity = _pdfinfo(
        pdfinfo_bytes,
        guest_name=guest_name,
        pages=int(postscript["pages"]),
    )
    tools["pdffonts"] = smoke_module._run_tool(
        home,
        image_record,
        job,
        name="pdffonts",
        entrypoint="/usr/bin/pdffonts",
        arguments=["/oracle/job/document.pdf"],
    )
    pdffonts_bytes = (job / "diagnostics" / "pdffonts.stdout.log").read_bytes()
    if not 1 <= len(pdffonts_bytes) <= 1024 * 1024:
        raise OracleError("PDF font inventory is outside its bound", exit_code=EXIT_INTEGRITY)
    atomic_write(output / "pdffonts.txt", pdffonts_bytes)
    tools["pdftotext"] = smoke_module._run_tool(
        home,
        image_record,
        job,
        name="pdftotext",
        entrypoint="/usr/bin/pdftotext",
        arguments=["/oracle/job/document.pdf", "/oracle/job/text.txt"],
    )
    text = _text_pages(output / "text.txt", int(postscript["pages"]))
    tools["bbox"] = smoke_module._run_tool(
        home,
        image_record,
        job,
        name="bbox",
        entrypoint="/usr/bin/pdftotext",
        arguments=["-bbox-layout", "/oracle/job/document.pdf", "/oracle/job/bbox.html"],
    )
    pages = _bbox_pages(output / "bbox.html", int(postscript["pages"]))
    tools["raster"] = smoke_module._run_tool(
        home,
        image_record,
        job,
        name="raster",
        entrypoint="/usr/bin/pdftocairo",
        arguments=[
            "-png",
            "-r",
            str(RASTER_DPI),
            "/oracle/job/document.pdf",
            "/oracle/job/page",
        ],
    )
    rasters: dict[int, Path] = {}
    for path in output.iterdir():
        match = _PNG_NAME.fullmatch(path.name)
        if match is not None:
            rasters[int(match.group(1))] = path
    expected_numbers = set(range(1, int(postscript["pages"]) + 1))
    if set(rasters) != expected_numbers:
        raise OracleError("derived PNG page inventory changed", exit_code=EXIT_INTEGRITY)
    for page, text_value in zip(pages, text, strict=True):
        number = int(page["number"])
        raster = rasters[number]
        smoke_module._bounded_regular(raster, "PNG", smoke_module.MAX_DERIVED_BYTES)
        try:
            width, height, _pixels = decode_png(raster)
        except (OSError, ValueError) as exc:
            raise OracleError("derived PNG is invalid", exit_code=EXIT_INTEGRITY) from exc
        expected_width = round(float(page["width_pt"]) * RASTER_DPI / 72)
        expected_height = round(float(page["height_pt"]) * RASTER_DPI / 72)
        if abs(width - expected_width) > 2 or abs(height - expected_height) > 2:
            raise OracleError("derived PNG dimensions changed", exit_code=EXIT_INTEGRITY)
        page["text"] = text_value
        page["raster"] = {
            "path": raster.name,
            "width": width,
            "height": height,
        }
    analysis: dict[str, Any] = {
        "schema": ANALYSIS_SCHEMA,
        "backend": "real",
        "profile": ANALYSIS_PROFILE,
        "page_count": len(pages),
        "pages": pages,
        "pdf_identity": pdf_identity,
        "font_inventory": {
            "path": "pdffonts.txt",
            "size": len(pdffonts_bytes),
            "sha256": hashlib.sha256(pdffonts_bytes).hexdigest(),
        },
        "diagnostics": [
            "native Windows PSCRIPT output",
            "font embedding has not been cleared for public redistribution",
            "private reference output is not baseline eligible",
        ],
    }
    atomic_write_json(output / "analysis.json", analysis)
    return analysis, tools


def _artifacts(job: Path) -> list[dict[str, object]]:
    paths: list[tuple[Path, str]] = []
    for path in sorted((job / "output").iterdir(), key=lambda item: item.name):
        paths.append((path, "derived-output"))
    for relative in (
        "ui-driver.json",
        "dosbox-x.conf",
        "inputs.json",
        "diagnostics/document-before-print.png",
        "diagnostics/print-dialog.png",
        "diagnostics/document-after-print.png",
        "diagnostics/print-program-manager-minimized.png",
        "diagnostics/print-exit-windows-confirmation.png",
        "diagnostics/container.stdout.log",
        "diagnostics/container.stderr.log",
    ):
        paths.append((job / relative, "evidence"))
    artifacts: list[dict[str, object]] = []
    for path, kind in paths:
        if path.is_symlink() or not path.is_file():
            raise OracleError("native document artifact is unsafe", exit_code=EXIT_INTEGRITY)
        artifacts.append(
            {
                "kind": kind,
                "path": path.relative_to(job).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return artifacts


def _discard_success_runtime(job: Path, runtime: Path) -> None:
    if runtime.parent != job or runtime.name != "runtime" or runtime.is_symlink():
        raise OracleError("unsafe disposable runtime cleanup target", exit_code=EXIT_INTEGRITY)
    shutil.rmtree(runtime)
    if runtime.exists() or runtime.is_symlink():
        raise OracleError("disposable runtime cleanup failed", exit_code=EXIT_BACKEND)


def print_native_document(
    home: Path,
    image_record: dict[str, Any],
    source: Path,
    guest_name: str,
    timeout_seconds: float,
    *,
    runtime_key: str | None = None,
) -> dict[str, Any]:
    started = monotonic()
    payload, audit = read_and_audit_source(source, guest_name=guest_name)
    _require_verified_image(image_record)
    _ensure_private_directories(home)
    source_root, ready, _ready_inputs, _ready_evidence = smoke_module._select_printer_runtime(
        home,
        runtime_key,
    )
    inputs = _native_inputs(ready, audit, image_record, timeout_seconds=timeout_seconds)
    parent_key = str(ready["runtime_key"])
    with _cache_lock(home, parent_key):
        source_root, checked_ready, _checked_inputs, _checked_evidence = (
            smoke_module._select_printer_runtime(home, parent_key)
        )
        if _native_inputs(
            checked_ready,
            audit,
            image_record,
            timeout_seconds=timeout_seconds,
        ) != inputs:
            raise OracleError(
                "printer-ready runtime changed after keying",
                exit_code=EXIT_INTEGRITY,
            )
        job = Path(tempfile.mkdtemp(prefix="batch-document-", dir=home / "jobs"))
        job.chmod(0o700)
        _directory_fsync(home / "jobs")
        for name in ("capture", "diagnostics", "home", "output"):
            (job / name).mkdir(mode=0o700)
        runtime = job / "runtime"
        shutil.copytree(source_root / "pristine-c", runtime, copy_function=shutil.copy2)
        _normalize_runtime_metadata(runtime)
        copied_tree, copied_printer = smoke_module.printer_module._validate_printer_runtime(runtime)
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
        atomic_write(oracle_dir / guest_name, payload)
        config_bytes = native_document_config().encode("utf-8")
        batch_bytes = native_document_batch(guest_name)
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
        inner_limit = max(1, int(timeout_seconds) - 10)
        invocation = build_podman_invocation(
            image_record,
            container_name=f"amipro-oracle-batch-{suffix}",
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
                str(inner_limit),
            ],
        )
        machine.advance("guest-invoked", evidence="dosbox-x.conf")
        process: dict[str, object] | None = None
        driver: dict[str, object] | None = None
        tools: dict[str, object] | None = None
        try:
            def lifecycle(
                guest_invocation: PodmanInvocation,
                path: Path,
                stop: threading.Event,
            ) -> dict[str, object]:
                return _drive_native_lifecycle(
                    guest_invocation,
                    path,
                    stop,
                    timeout_seconds=timeout_seconds,
                )
            process, driver = smoke_module._invoke_guest(
                invocation,
                job,
                timeout_seconds=timeout_seconds,
                lifecycle=lifecycle,
            )
            if (
                process.get("exit_code") != 0
                or process.get("timed_out") is not False
                or process.get("killed") is not False
            ):
                error = OracleError(
                    "native document guest did not exit cleanly",
                    exit_code=EXIT_BACKEND,
                )
                error.process_result = process
                raise error
            observer = _validate_observer_evidence(job / "diagnostics")
            _validate_ui(job, driver)
            staged = runtime / "ORACLE" / guest_name
            if (
                staged.is_symlink()
                or not staged.is_file()
                or staged.stat().st_size != len(payload)
                or sha256_file(staged) != audit["sha256"]
            ):
                raise OracleError("staged native document changed", exit_code=EXIT_INTEGRITY)
            expected_sentinels = {
                "PRTSMK.STA": b"POSTSCRIPT_LAUNCH_REQUESTED\r\n",
                "PRTSMK.OK": b"POSTSCRIPT_RETURNED_ZERO\r\n",
            }
            for name, expected in expected_sentinels.items():
                sentinel = runtime / name
                if (
                    sentinel.is_symlink()
                    or not sentinel.is_file()
                    or sentinel.read_bytes() != expected
                ):
                    raise OracleError(
                        f"native document sentinel is invalid: {name}",
                        exit_code=EXIT_BACKEND,
                    )
            error_sentinel = runtime / "PRTSMK.ERR"
            if error_sentinel.exists() or error_sentinel.is_symlink():
                raise OracleError(
                    "native document reported a guest error",
                    exit_code=EXIT_BACKEND,
                )
            captures = smoke_module._capture_files(job / "capture")
            if len(captures) != 1:
                raise OracleError("expected exactly one LPT capture", exit_code=EXIT_INTEGRITY)
            raw = _read_bounded_file(
                captures[0],
                maximum=MAX_NATIVE_POSTSCRIPT_BYTES,
                label="raw PostScript capture",
            )
            sanitized, postscript = validate_native_postscript(raw, guest_name=guest_name)
            atomic_write(job / "output" / "document.raw.ps", raw)
            atomic_write(job / "output" / "document.ps", sanitized)
            atomic_write_json(job / "output" / "postscript-transform.json", postscript)
            machine.advance("printed", evidence="output/document.raw.ps")
            smoke_module._select_printer_runtime(home, parent_key)
            machine.advance("guest-returned", evidence="PRTSMK.OK")
            analysis, tools = _derive_outputs(
                home,
                image_record,
                job,
                guest_name=guest_name,
                postscript=postscript,
            )
            machine.advance("analyzed", evidence="output/analysis.json")
            _discard_success_runtime(job, runtime)
            machine.advance("complete", evidence="validated native PostScript/PDF/PNG analysis")
            manifest: dict[str, Any] = {
                "schema": JOB_SCHEMA,
                "result_schema": NATIVE_DOCUMENT_RESULT_SCHEMA,
                "backend": "real",
                "baseline_eligible": False,
                "status": "success",
                "source": {
                    "size": audit["size"],
                    "sha256": audit["sha256"],
                    "staged_name": guest_name,
                    "preflight": audit,
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
                "process_result": {"guest": process, "analysis_tools": tools},
                "postscript": postscript,
                "analysis_path": "output/analysis.json",
                "artifacts": _artifacts(job),
                "observer": observer,
                "ui_driver": driver,
                "state_trace": machine.trace,
                "duration_seconds": round(monotonic() - started, 6),
                "diagnostics": [
                    "private native reference output; not baseline eligible",
                    "review font embedding before any publication",
                    "disposable guest runtime removed after successful validation",
                ],
            }
            atomic_write_json(job / "job.json", manifest)
            return {
                "evidence_job": job.name,
                "job_manifest_sha256": sha256_file(job / "job.json"),
                "pdf_path": str(job / "output" / "document.pdf"),
                "page_count": analysis["page_count"],
            }
        except BaseException as exc:
            attached = getattr(exc, "process_result", None)
            if process is None and isinstance(attached, dict):
                process = attached
            if machine.state != "failed" and "failed" in machine.transitions.get(
                machine.state,
                frozenset(),
            ):
                machine.advance("failed", evidence="failure.json")
            atomic_write_json(
                job / "failure.json",
                {
                    "schema": "amipro-oracle-native-document-failure-v1",
                    "phase": "native-document",
                    "status": "failure",
                    "baseline_eligible": False,
                    "source_sha256": audit["sha256"],
                    "guest_name": guest_name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "process_result": process,
                    "tool_results": tools,
                    "ui_driver": driver,
                    "state_trace": machine.trace,
                },
            )
            raise
