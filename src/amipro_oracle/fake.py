from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Any

from .config import dosbox_config_digest
from .constants import ANALYSIS_SCHEMA, JOB_SCHEMA
from .errors import OracleError
from .io import atomic_write, atomic_write_json, digest_json, sha256_file
from .media import stage_input_file
from .raster import encode_rgb_png
from .state import StateMachine
from .toolchain import lock_path

FAKE_PROFILE = {
    "id": "fake-letter-72dpi-v1",
    "page_width_pt": 612.0,
    "page_height_pt": 792.0,
    "raster_dpi": 72,
    "whitespace": "exact",
}


def _minimal_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 10 Tf 72 720 Td ({escaped}) Tj ET\n".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


def _artifact(job_root: Path, path: Path, *, kind: str) -> dict[str, object]:
    return {
        "kind": kind,
        "path": path.relative_to(job_root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _state_machine() -> StateMachine:
    return StateMachine(
        initial="created",
        terminal=frozenset({"complete"}),
        transitions={
            "created": frozenset({"staged"}),
            "staged": frozenset({"guest-ready"}),
            "guest-ready": frozenset({"printed"}),
            "printed": frozenset({"analyzed"}),
            "analyzed": frozenset({"complete"}),
        },
    )


def _partial_artifacts(job_root: Path) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(job_root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(job_root.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.name != "failure.json"
    ]


def _run_fake_job(
    source: Path,
    job_root: Path,
    *,
    staged_name: str,
    started: float,
    machine: StateMachine,
) -> dict[str, Any]:
    input_directory = job_root / "input"
    capture_directory = job_root / "capture"
    output_directory = job_root / "output"
    diagnostic_directory = job_root / "diagnostics"
    for directory in (input_directory, capture_directory, output_directory, diagnostic_directory):
        directory.mkdir(parents=True, exist_ok=False)

    staged = input_directory / staged_name
    source_info = stage_input_file(source, staged)
    if sha256_file(staged) != source_info["sha256"]:
        raise OSError("staged source hash mismatch")
    machine.advance("staged", evidence="staged source hash matches")

    log = diagnostic_directory / "fake-emulator.log"
    atomic_write(
        log,
        b"FAKE BACKEND: no proprietary software or emulator was executed.\n"
        b"Network: unavailable by construction.\n",
    )
    machine.advance("guest-ready", evidence="fake readiness sentinel")

    text = f"FAKE BACKEND {source_info['sha256']}"
    postscript = capture_directory / "output.ps"
    atomic_write(
        postscript,
        (
            "%!PS-Adobe-3.0\n%%Creator: amipro-oracle fake backend\n%%Pages: 1\n"
            "%%BoundingBox: 0 0 612 792\n%%Page: 1 1\nshowpage\n%%EOF\n"
        ).encode("ascii"),
    )
    machine.advance("printed", evidence="synthetic PostScript capture is complete")

    pdf = output_directory / "output.pdf"
    png = output_directory / "page-001.png"
    atomic_write(pdf, _minimal_pdf(text))
    color = bytes.fromhex(str(source_info["sha256"])[:6])
    atomic_write(png, encode_rgb_png(1, 1, color))
    analysis_path = output_directory / "analysis.json"
    analysis: dict[str, Any] = {
        "schema": ANALYSIS_SCHEMA,
        "backend": "fake",
        "profile": FAKE_PROFILE,
        "page_count": 1,
        "pages": [
            {
                "number": 1,
                "width_pt": 612.0,
                "height_pt": 792.0,
                "text": text,
                "text_boxes": [
                    {"text": text, "x0": 72.0, "y0": 62.0, "x1": 300.0, "y1": 74.0}
                ],
                "image_boxes": [],
                "raster": {"path": png.name, "width": 1, "height": 1},
            }
        ],
        "diagnostics": ["fake backend measurements are never baseline eligible"],
    }
    atomic_write_json(analysis_path, analysis)
    machine.advance("analyzed", evidence="synthetic analysis written")
    machine.advance("complete", evidence="fake emulator exited cleanly")

    artifacts = [
        _artifact(job_root, postscript, kind="postscript"),
        _artifact(job_root, pdf, kind="pdf"),
        _artifact(job_root, png, kind="png"),
        _artifact(job_root, analysis_path, kind="analysis"),
        _artifact(job_root, log, kind="diagnostic-log"),
    ]
    manifest: dict[str, Any] = {
        "schema": JOB_SCHEMA,
        "backend": "fake",
        "baseline_eligible": False,
        "status": "success",
        "source": {
            "name": source.name,
            "size": source_info["size"],
            "sha256": source_info["sha256"],
            "staged_name": staged_name,
        },
        "media": {
            "profile": "fake-synthetic-input-v1",
            "sha256": source_info["sha256"],
        },
        "runtime": {
            "profile": "fake-runtime-v1",
            "manifest_sha256": digest_json(
                {"backend": "fake", "config_sha256": dosbox_config_digest()}
            ),
        },
        "config_sha256": dosbox_config_digest(),
        "toolchain": {
            "backend": "fake",
            "lock_sha256": sha256_file(lock_path()),
            "oci_image_digest": None,
        },
        "process_result": {
            "exit_code": 0,
            "timed_out": False,
            "killed": False,
            "fake": True,
        },
        "analysis_path": analysis_path.relative_to(job_root).as_posix(),
        "artifacts": artifacts,
        "state_trace": machine.trace,
        "duration_seconds": round(monotonic() - started, 6),
        "diagnostics": ["FAKE BACKEND: no Ami Pro or Windows execution occurred"],
    }
    atomic_write_json(job_root / "job.json", manifest)
    return manifest


def run_fake_job(source: Path, job_root: Path, *, staged_name: str) -> dict[str, Any]:
    started = monotonic()
    machine = _state_machine()
    try:
        return _run_fake_job(
            source,
            job_root,
            staged_name=staged_name,
            started=started,
            machine=machine,
        )
    except (OSError, ValueError, OracleError) as exc:
        process_result = exc.process_result if isinstance(exc, OracleError) else None
        failure: dict[str, Any] = {
            "schema": "amipro-oracle-failure-v1",
            "backend": "fake",
            "baseline_eligible": False,
            "status": "failure",
            "source": source.name,
            "guest": staged_name,
            "error": f"{type(exc).__name__}: fake job failed before completion",
            "state_trace": machine.trace,
            "process_result": process_result,
            "artifacts": _partial_artifacts(job_root),
            "duration_seconds": round(monotonic() - started, 6),
            "diagnostics": ["fake job failed; retained all regular partial artifacts"],
        }
        atomic_write_json(job_root / "failure.json", failure)
        raise
