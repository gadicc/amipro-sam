"""Private, integrity-checked differential analysis for completed native batches.

This module deliberately lives with the oracle.  It consumes converter output, but the
converter never imports it and never depends on the private corpus or proprietary runtime.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import sys
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .compare import (
    _normalize_text,
    _verify_job_artifacts,
    compare_analyses,
    load_analysis,
)
from .constants import ANALYSIS_SCHEMA, EXIT_BACKEND, EXIT_INTEGRITY, EXIT_MISSING, JOB_SCHEMA
from .errors import OracleError
from .io import atomic_write_json, digest_json, read_json_object, sha256_file
from .native_batch import (
    ANALYSIS_PROFILE as NATIVE_ANALYSIS_PROFILE,
)
from .native_batch import (
    MAX_PAGES,
    RASTER_DPI,
    _bbox_pages,
    _text_pages,
)
from .postscript_smoke import MAX_DERIVED_BYTES, _bounded_regular, _run_tool
from .process import run_bounded
from .raster import _validated_png_dimensions
from .toolchain import lock_path, probe_recorded_image

PRIVATE_CORPUS_RUN_SCHEMA = "amipro-oracle-private-corpus-run-v1"
PRIVATE_CORPUS_DOCUMENT_SCHEMA = "amipro-oracle-private-corpus-document-v1"
PRIVATE_CORPUS_AGGREGATE_SCHEMA = "amipro-oracle-private-corpus-aggregate-v1"
PRIVATE_CORPUS_FAILURES_SCHEMA = "amipro-oracle-private-corpus-native-failures-v1"

_BATCH_SCHEMAS = {
    "amipro-oracle-real-batch-plan-v1": {
        "result": "amipro-oracle-real-batch-v1",
        "document": "amipro-oracle-real-batch-document-v1",
        "failure": "amipro-oracle-real-batch-failure-v1",
        "native_result": "amipro-oracle-native-document-result-v1",
        "plan_identity": ("schema", "document_count", "records"),
    },
    "amipro-oracle-real-batch-plan-v2": {
        "result": "amipro-oracle-real-batch-v2",
        "document": "amipro-oracle-real-batch-document-v2",
        "failure": "amipro-oracle-real-batch-failure-v2",
        "native_result": "amipro-oracle-native-document-result-v2",
        "plan_identity": (
            "schema",
            "document_count",
            "font_environment",
            "font_policy",
            "records",
        ),
    },
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EVIDENCE_JOB = re.compile(r"batch-document-[a-z0-9_-]+\Z")
_RASTER_NAME = re.compile(r"page-([0-9]+)[.]png\Z")
_MAX_DOCUMENTS = 99_999
_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_PDF_BYTES = 64 * 1024 * 1024
_MAX_CONVERTER_SECONDS = 300.0
_MIN_PRIVACY_GROUP = 5
_PINNED_PILLOW_VERSION = "12.3.0"

_FIXTURE_TARGETS = {
    "page-count": "one explicit page-break variable with otherwise identical flowing text",
    "page-geometry": "one paper-size or orientation variable with fixed margins and content",
    "page-text": "one character-decoding or text-presence variable in a single line",
    "text-box-count": "one word-segmentation or line-wrapping variable in a single paragraph",
    "text-box-position": "one margin, indent, alignment, or line-spacing variable",
    "text-box-content": "one inline text or encoding variable with fixed geometry",
    "image-box-count": "one invented embedded-image record with fixed page geometry",
    "image-box-position": "one invented image anchoring or offset variable",
    "page-raster": "one visible formatting variable after geometry and text are held constant",
    "missing-page-raster": "one fixed-DPI raster-production case with a single page",
    "conversion-failure": "one minimal parser or renderer structure isolated from other records",
    "analysis-failure": "one bounded PDF-analysis case with a single generated page",
}


@dataclass(frozen=True)
class NativeDocument:
    index: int
    source: Path
    source_size: int
    source_sha256: str
    result: dict[str, Any]
    reference_pdf: Path
    evidence_job: Path
    evidence_manifest_sha256: str
    analysis_path: Path
    analysis: dict[str, Any]


@dataclass(frozen=True)
class NativeSelection:
    plan_digest: str
    batch_sha256: str
    documents: tuple[NativeDocument, ...]
    failures: tuple[dict[str, object], ...]
    analysis_profile: dict[str, object]
    toolchain: dict[str, object]
    source_inventory_digest: str


def _integrity(message: str) -> OracleError:
    return OracleError(message, exit_code=EXIT_INTEGRITY)


def _toolchain_identity(image_record: dict[str, Any]) -> dict[str, object]:
    return {
        key: image_record.get(key)
        for key in ("image_id", "image_digest", "lock_sha256", "platform")
    }


def _private_regular(path: Path, *, label: str, maximum: int) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise _integrity(f"{label} is missing") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or not 1 <= info.st_size <= maximum
    ):
        raise _integrity(f"{label} must be a bounded regular non-symlink file")
    return info


def _safe_relative(value: object, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise _integrity(f"{label} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise _integrity(f"{label} is unsafe")
    return path


def _join_private(root: Path, relative: PurePosixPath, *, label: str) -> Path:
    resolved_root = root.resolve(strict=True)
    candidate = resolved_root.joinpath(*relative.parts)
    resolved = candidate.resolve(strict=False)
    if resolved_root not in resolved.parents:
        raise _integrity(f"{label} escapes its private root")
    current = resolved_root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise _integrity(f"{label} has an unsafe parent")
    return candidate


def _read_object(path: Path, *, label: str, maximum: int = 64 * 1024 * 1024) -> dict[str, Any]:
    _private_regular(path, label=label, maximum=maximum)
    try:
        return read_json_object(path)
    except (OSError, ValueError) as exc:
        raise _integrity(f"{label} is invalid") from exc


def _discover_sources(source_root: Path) -> list[tuple[str, Path]]:
    root = source_root.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise OracleError("source corpus must be a real directory", exit_code=EXIT_MISSING)
    root = root.resolve()
    sources: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        if path.suffix.casefold() != ".sam":
            continue
        if path.is_symlink() or not path.is_file():
            raise _integrity("source corpus contains an unsafe SAM entry")
        relative = path.relative_to(root).as_posix()
        _safe_relative(relative, label="source corpus entry")
        sources.append((relative, path))
        if len(sources) > _MAX_DOCUMENTS:
            raise _integrity("source corpus exceeds the document bound")
    sources.sort(key=lambda item: (item[0].casefold(), item[0]))
    folded = [relative.casefold() for relative, _path in sources]
    if len(folded) != len(set(folded)):
        raise _integrity("source corpus contains case-insensitive name collisions")
    return sources


def _expected_reference_pdf(record: dict[str, object]) -> PurePosixPath:
    source = _safe_relative(record.get("source"), label="batch source")
    if not source.name.casefold().endswith(".sam"):
        raise _integrity("batch source does not have a SAM extension")
    return PurePosixPath("reference-pdf", *source.parts[:-1], source.name[:-4] + ".pdf")


def _verify_source(path: Path, record: dict[str, object]) -> tuple[int, str]:
    info = _private_regular(path, label="source document", maximum=_MAX_SOURCE_BYTES)
    expected_size = record.get("source_size")
    expected_hash = record.get("source_sha256")
    digest = sha256_file(path)
    if (
        type(expected_size) is not int
        or expected_size != info.st_size
        or not isinstance(expected_hash, str)
        or _SHA256.fullmatch(expected_hash) is None
        or expected_hash != digest
    ):
        raise _integrity("source document identity changed since the native batch")
    return info.st_size, digest


def _current_source_identity(path: Path) -> tuple[int, str]:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or not 0 <= info.st_size <= _MAX_SOURCE_BYTES
    ):
        raise _integrity("blocked source document is no longer a bounded regular file")
    return info.st_size, sha256_file(path)


def _verify_evidence(
    home: Path,
    image_record: dict[str, Any],
    result: dict[str, Any],
    record: dict[str, object],
    reference_pdf: Path,
    *,
    native_result_schema: str,
) -> tuple[Path, str, Path, dict[str, Any], dict[Path, dict[str, Any]]]:
    evidence_name = result.get("evidence_job")
    expected_manifest_hash = result.get("job_manifest_sha256")
    if (
        not isinstance(evidence_name, str)
        or _EVIDENCE_JOB.fullmatch(evidence_name) is None
        or not isinstance(expected_manifest_hash, str)
        or _SHA256.fullmatch(expected_manifest_hash) is None
    ):
        raise _integrity("native result has an invalid evidence identity")
    evidence_job = home / "jobs" / evidence_name
    if evidence_job.is_symlink() or not evidence_job.is_dir():
        raise _integrity("native evidence job is missing or unsafe")
    manifest_path = evidence_job / "job.json"
    _private_regular(manifest_path, label="native evidence manifest", maximum=64 * 1024 * 1024)
    if sha256_file(manifest_path) != expected_manifest_hash:
        raise _integrity("native evidence manifest identity changed")
    manifest = _read_object(manifest_path, label="native evidence manifest")
    source = manifest.get("source")
    if (
        manifest.get("schema") != JOB_SCHEMA
        or manifest.get("result_schema") != native_result_schema
        or manifest.get("backend") != "real"
        or manifest.get("status") != "success"
        or manifest.get("baseline_eligible") is not False
        or not isinstance(source, dict)
        or source.get("size") != record.get("source_size")
        or source.get("sha256") != record.get("source_sha256")
        or source.get("staged_name") != record.get("guest")
        or manifest.get("toolchain") != _toolchain_identity(image_record)
    ):
        raise _integrity("native evidence manifest disagrees with the batch identity")
    try:
        artifacts = _verify_job_artifacts(manifest_path, manifest)
    except ValueError as exc:
        raise _integrity("native evidence artifact inventory is invalid") from exc
    native_pdf = evidence_job / "output" / "document.pdf"
    if native_pdf not in artifacts or native_pdf.is_symlink() or not native_pdf.is_file():
        raise _integrity("native evidence PDF is absent from the artifact inventory")
    if (
        native_pdf.stat().st_size != reference_pdf.stat().st_size
        or sha256_file(native_pdf) != sha256_file(reference_pdf)
    ):
        raise _integrity("native reference PDF is not the verified evidence PDF")
    analysis, _source = load_analysis(manifest_path)
    analysis_path = evidence_job / str(manifest.get("analysis_path"))
    if analysis.get("profile") != NATIVE_ANALYSIS_PROFILE:
        raise _integrity("native analysis does not use the pinned batch profile")
    pages = analysis.get("pages")
    if (
        not isinstance(pages, list)
        or analysis.get("page_count") != len(pages)
        or not 1 <= len(pages) <= MAX_PAGES
        or result.get("pdf", {}).get("page_count") != len(pages)
    ):
        raise _integrity("native analysis page identity is invalid")
    return evidence_job, expected_manifest_hash, analysis_path, analysis, artifacts


def _failure_class(status: str, error: object) -> str:
    value = str(error).casefold()
    if status == "blocked":
        if "active/external-content preflight" in value:
            return "blocked-active-or-external-content"
        if "font" in value and "resolve" in value:
            return "blocked-unresolved-font"
        if "source" in value or "file" in value:
            return "blocked-source-integrity"
        return "blocked-other"
    if "timeout" in value or "deadline" in value:
        return "failed-timeout"
    if "print dialog" in value or "screen" in value or "state" in value:
        return "failed-ui-state"
    if "postscript" in value or "capture" in value or "print" in value:
        return "failed-print-capture"
    if "analysis" in value or "pdf" in value or "png" in value:
        return "failed-derived-analysis"
    return "failed-other"


def select_native_documents(
    *,
    home: Path,
    batch_root: Path,
    source_root: Path,
    image_record: dict[str, Any],
    expected_successes: int,
    expected_failures: int,
) -> NativeSelection:
    """Verify a completed native batch and select only its successful documents."""
    if (
        isinstance(expected_successes, bool)
        or isinstance(expected_failures, bool)
        or expected_successes < 1
        or expected_failures < 0
        or expected_successes + expected_failures > _MAX_DOCUMENTS
    ):
        raise _integrity("expected native outcome counts are invalid")
    batch_root = batch_root.expanduser().absolute()
    if batch_root.is_symlink() or not batch_root.is_dir():
        raise OracleError("native batch is missing or unsafe", exit_code=EXIT_MISSING)
    batch_root = batch_root.resolve()
    if batch_root.stat().st_mode & 0o077:
        raise _integrity("native batch must remain private")
    plan_path = batch_root / "plan.json"
    journal_path = batch_root / "batch.json"
    plan = _read_object(plan_path, label="native batch plan")
    schema = _BATCH_SCHEMAS.get(str(plan.get("schema")))
    if schema is None:
        raise _integrity("native batch plan schema is unsupported")
    records = plan.get("records")
    count = plan.get("document_count")
    identity = {key: plan.get(key) for key in schema["plan_identity"]}
    if (
        type(count) is not int
        or not 1 <= count <= _MAX_DOCUMENTS
        or not isinstance(records, list)
        or len(records) != count
        or plan.get("plan_digest") != digest_json(identity)
    ):
        raise _integrity("native batch plan identity is invalid")
    journal = _read_object(journal_path, label="native batch journal")
    jobs = journal.get("jobs")
    if (
        journal.get("schema") != schema["result"]
        or journal.get("backend") != "real"
        or journal.get("baseline_eligible") is not False
        or journal.get("status") not in {"success", "complete-with-failures"}
        or journal.get("plan_digest") != plan.get("plan_digest")
        or journal.get("document_count") != count
        or journal.get("pending_count") != 0
        or not isinstance(jobs, list)
        or len(jobs) != count
    ):
        raise _integrity("native batch journal identity is invalid")
    successes = sum(isinstance(job, dict) and job.get("status") == "success" for job in jobs)
    failures = sum(
        isinstance(job, dict) and job.get("status") in {"failure", "blocked"} for job in jobs
    )
    if (
        journal.get("success_count") != successes
        or journal.get("failure_count") != failures
        or successes != expected_successes
        or failures != expected_failures
    ):
        raise _integrity("native batch outcome counts do not match the requested phase")

    discovered = _discover_sources(source_root)
    expected_names = [
        record.get("source") if isinstance(record, dict) else None for record in records
    ]
    if [name for name, _path in discovered] != expected_names:
        raise _integrity("source corpus inventory differs from the native batch plan")
    source_by_name = dict(discovered)
    inventory: list[dict[str, object]] = []
    selected: list[NativeDocument] = []
    failed: list[dict[str, object]] = []
    selected_profile: dict[str, object] | None = None
    selected_toolchain: dict[str, object] | None = None

    for expected_index, (record_value, job_value) in enumerate(zip(records, jobs, strict=True), 1):
        if not isinstance(record_value, dict) or not isinstance(job_value, dict):
            raise _integrity("native batch record is invalid")
        record = record_value
        job = job_value
        if (
            record.get("index") != expected_index
            or record.get("guest") != f"DOC{expected_index:05d}.SAM"
            or job.get("index") != expected_index
            or job.get("source") != record.get("source")
            or job.get("guest") != record.get("guest")
            or job.get("source_sha256") != record.get("source_sha256")
            or job.get("backend") != "real"
            or job.get("baseline_eligible") is not False
        ):
            raise _integrity("native batch record and result identities disagree")
        status = job.get("status")
        if status not in {"success", "failure", "blocked"}:
            raise _integrity("native batch result status is invalid")
        result_filename = "result.json" if status == "success" else "failure.json"
        saved_result = _read_object(
            batch_root / "jobs" / f"{expected_index:05d}" / result_filename,
            label="native per-document result",
        )
        if saved_result != job:
            raise _integrity("native per-document result differs from the batch journal")
        source_path = source_by_name[str(record["source"])]
        if record.get("source_sha256") is None and status != "success":
            source_size, source_hash = _current_source_identity(source_path)
        else:
            source_size, source_hash = _verify_source(source_path, record)
        inventory.append({"index": expected_index, "size": source_size, "sha256": source_hash})
        if status != "success":
            expected_failure_schema = schema["failure"]
            if job.get("schema") != expected_failure_schema:
                raise _integrity("native failure result schema is invalid")
            failed.append(
                {
                    "index": expected_index,
                    "status": status,
                    "class": _failure_class(str(status), job.get("error")),
                    "exit_code": job.get("exit_code"),
                }
            )
            continue
        if job.get("schema") != schema["document"]:
            raise _integrity("native success result schema is invalid")
        pdf_record = job.get("pdf")
        expected_pdf = _expected_reference_pdf(record)
        if not isinstance(pdf_record, dict) or pdf_record.get("path") != expected_pdf.as_posix():
            raise _integrity("native reference PDF path identity is invalid")
        reference_pdf = _join_private(batch_root, expected_pdf, label="native reference PDF")
        pdf_info = _private_regular(
            reference_pdf, label="native reference PDF", maximum=_MAX_PDF_BYTES
        )
        if (
            type(pdf_record.get("size")) is not int
            or pdf_record.get("size") != pdf_info.st_size
            or not isinstance(pdf_record.get("sha256"), str)
            or _SHA256.fullmatch(str(pdf_record["sha256"])) is None
            or sha256_file(reference_pdf) != pdf_record.get("sha256")
        ):
            raise _integrity("native reference PDF identity changed")
        evidence_job, manifest_hash, analysis_path, analysis, _artifacts = _verify_evidence(
            home,
            image_record,
            job,
            record,
            reference_pdf,
            native_result_schema=str(schema["native_result"]),
        )
        profile = analysis.get("profile")
        manifest = _read_object(evidence_job / "job.json", label="native evidence manifest")
        toolchain = manifest.get("toolchain")
        if not isinstance(profile, dict) or not isinstance(toolchain, dict):
            raise _integrity("native evidence lacks analysis or toolchain identity")
        if selected_profile is None:
            selected_profile = profile
            selected_toolchain = toolchain
        elif profile != selected_profile or toolchain != selected_toolchain:
            raise _integrity("native success set uses incompatible analysis toolchains")
        selected.append(
            NativeDocument(
                index=expected_index,
                source=source_path,
                source_size=source_size,
                source_sha256=source_hash,
                result=job,
                reference_pdf=reference_pdf,
                evidence_job=evidence_job,
                evidence_manifest_sha256=manifest_hash,
                analysis_path=analysis_path,
                analysis=analysis,
            )
        )
    if len(selected) != expected_successes or len(failed) != expected_failures:
        raise _integrity("native selection count changed during verification")
    if selected_profile is None or selected_toolchain is None:
        raise _integrity("native batch contains no verified analysis profile")
    return NativeSelection(
        plan_digest=str(plan["plan_digest"]),
        batch_sha256=sha256_file(journal_path),
        documents=tuple(selected),
        failures=tuple(failed),
        analysis_profile=selected_profile,
        toolchain=selected_toolchain,
        source_inventory_digest=digest_json(inventory),
    )


def load_verified_image(home: Path) -> dict[str, Any]:
    path = home / "toolchain-image.json"
    record = _read_object(path, label="locked toolchain image record")
    if (
        record.get("schema") != "amipro-oracle-image-v1"
        or not isinstance(record.get("image_digest"), str)
        or _IMAGE_DIGEST.fullmatch(str(record["image_digest"])) is None
        or record.get("lock_sha256") != sha256_file(lock_path())
    ):
        raise _integrity("locked toolchain image record is invalid")
    probe = probe_recorded_image(record)
    if probe.get("status") != "match":
        raise OracleError(
            "locked rootless OCI analysis image is unavailable or changed",
            exit_code=EXIT_BACKEND,
        )
    return record


def converter_profile(repo_root: Path, *, timeout_seconds: float) -> dict[str, object]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not 1 <= timeout_seconds <= _MAX_CONVERTER_SECONDS
    ):
        raise _integrity("converter timeout is outside its bound")
    component = repo_root / "src" / "amipro_sam"
    files = sorted(
        path
        for path in component.rglob("*")
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
    )
    if not files or len(files) > 10_000:
        raise _integrity("converter component inventory is invalid")
    inventory = [
        {
            "path": path.relative_to(repo_root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in files
    ]
    return {
        "id": "current-converter-pdf-subprocess-v1",
        "component_digest": digest_json(inventory),
        "python": sys.version.split()[0],
        "timeout_seconds": float(timeout_seconds),
        "maximum_pdf_bytes": _MAX_PDF_BYTES,
        "maximum_writable_tree_bytes": 128 * 1024 * 1024,
        "maximum_writable_tree_entries": 2_000,
        "command": "python -m amipro_sam convert INPUT --format pdf --output OUTPUT",
    }


def measurement_profile(repo_root: Path) -> dict[str, object]:
    try:
        import PIL
    except ImportError as exc:
        raise OracleError(
            "private corpus comparison requires the oracle-analysis extra",
            exit_code=EXIT_MISSING,
        ) from exc
    if PIL.__version__ != _PINNED_PILLOW_VERSION:
        raise _integrity(
            "private corpus comparison requires pinned "
            f"Pillow {_PINNED_PILLOW_VERSION}, found {PIL.__version__}"
        )
    files = [
        repo_root / "src" / "amipro_oracle" / name
        for name in (
            "compare.py",
            "native_batch.py",
            "oci.py",
            "postscript_smoke.py",
            "private_corpus.py",
            "process.py",
            "raster.py",
        )
    ]
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise _integrity("private comparison implementation inventory is invalid")
    inventory = [
        {
            "path": path.relative_to(repo_root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in files
    ]
    return {
        "id": "private-corpus-measurements-v1",
        "implementation_digest": digest_json(inventory),
        "normalized_text_hash": "unicode-nfc-whitespace-collapse-sha256-v1",
        "raster_difference": "pillow-histogram-equivalent-v1",
        "pillow_version": PIL.__version__,
        "privacy_minimum_group_size": _MIN_PRIVACY_GROUP,
    }


def _redacted_process(result: dict[str, object]) -> dict[str, object]:
    return {
        key: result.get(key)
        for key in (
            "exit_code",
            "timed_out",
            "killed",
            "duration_seconds",
            "stdout_capture",
            "stderr_capture",
            "final_tree_bytes",
            "final_tree_entries",
        )
        if key in result
    }


def _run_converter(
    document: NativeDocument,
    job: Path,
    repo_root: Path,
    *,
    timeout_seconds: float,
) -> dict[str, object]:
    output = job / "output"
    diagnostics = job / "diagnostics"
    output.mkdir(mode=0o700)
    diagnostics.mkdir(mode=0o700)
    destination = output / "document.pdf"
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    source_root = str(repo_root / "src")
    environment["PYTHONPATH"] = source_root if not existing else source_root + os.pathsep + existing
    result = run_bounded(
        [
            sys.executable,
            "-m",
            "amipro_sam",
            "convert",
            str(document.source),
            "--format",
            "pdf",
            "--output",
            str(destination),
        ],
        cwd=repo_root,
        environment=environment,
        stdout_path=diagnostics / "converter.stdout.log",
        stderr_path=diagnostics / "converter.stderr.log",
        timeout_seconds=timeout_seconds,
        max_output_bytes=1024 * 1024,
        watch_path=job,
        max_tree_bytes=128 * 1024 * 1024,
        max_tree_entries=2_000,
    )
    if result.get("exit_code") != 0:
        return {"status": "failure", "process": _redacted_process(result)}
    info = _private_regular(destination, label="converter PDF", maximum=_MAX_PDF_BYTES)
    return {
        "status": "success",
        "process": _redacted_process(result),
        "pdf": {"size": info.st_size, "sha256": sha256_file(destination)},
    }


def _pdf_page_count(path: Path, *, maximum: int = MAX_PAGES) -> int:
    _private_regular(path, label="pdfinfo output", maximum=1024 * 1024)
    try:
        lines = path.read_bytes().decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise _integrity("pdfinfo output is invalid") from exc
    values: dict[str, str] = {}
    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    try:
        pages = int(values["Pages"])
    except (KeyError, ValueError) as exc:
        raise _integrity("pdfinfo output lacks a valid page count") from exc
    if (
        not 1 <= pages <= maximum
        or values.get("Encrypted") != "no"
        or values.get("JavaScript", "no") != "no"
    ):
        raise _integrity("converter PDF safety or page bound is invalid")
    return pages


def _analyze_converter_pdf(
    home: Path,
    image_record: dict[str, Any],
    job: Path,
    profile: dict[str, object],
) -> tuple[Path, dict[str, Any]]:
    output = job / "output"
    tools: dict[str, object] = {}
    tools["pdfinfo"] = _run_tool(
        home,
        image_record,
        job,
        name="pdfinfo",
        entrypoint="/usr/bin/pdfinfo",
        arguments=["/oracle/job/document.pdf"],
    )
    pages_count = _pdf_page_count(job / "diagnostics" / "pdfinfo.stdout.log")
    tools["pdftotext"] = _run_tool(
        home,
        image_record,
        job,
        name="pdftotext",
        entrypoint="/usr/bin/pdftotext",
        arguments=["/oracle/job/document.pdf", "/oracle/job/text.txt"],
    )
    text_pages = _text_pages(output / "text.txt", pages_count)
    tools["bbox"] = _run_tool(
        home,
        image_record,
        job,
        name="bbox",
        entrypoint="/usr/bin/pdftotext",
        arguments=["-bbox-layout", "/oracle/job/document.pdf", "/oracle/job/bbox.html"],
    )
    pages = _bbox_pages(
        output / "bbox.html",
        pages_count,
        allow_bounded_off_page=True,
    )
    tools["raster"] = _run_tool(
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
        match = _RASTER_NAME.fullmatch(path.name)
        if match is not None:
            rasters[int(match.group(1))] = path
    if set(rasters) != set(range(1, pages_count + 1)):
        raise _integrity("converter raster page inventory is invalid")
    for page, text in zip(pages, text_pages, strict=True):
        number = int(page["number"])
        raster = rasters[number]
        _bounded_regular(raster, "converter page raster", MAX_DERIVED_BYTES)
        try:
            width, height = _validated_png_dimensions(raster)
        except (OSError, ValueError) as exc:
            raise _integrity("converter page raster is invalid") from exc
        expected_width = round(float(page["width_pt"]) * RASTER_DPI / 72)
        expected_height = round(float(page["height_pt"]) * RASTER_DPI / 72)
        if abs(width - expected_width) > 2 or abs(height - expected_height) > 2:
            raise _integrity("converter page raster dimensions disagree with PDF geometry")
        page["text"] = text
        page["raster"] = {"path": raster.name, "width": width, "height": height}
    analysis: dict[str, Any] = {
        "schema": ANALYSIS_SCHEMA,
        "backend": "real",
        "profile": profile,
        "page_count": len(pages),
        "pages": pages,
        "diagnostics": [
            "current converter PDF analyzed by the native batch's locked Poppler profile",
            "private differential output; not baseline eligible",
        ],
    }
    analysis_path = output / "analysis.json"
    atomic_write_json(analysis_path, analysis)
    return analysis_path, analysis


def _normalized_text_hashes(analysis: dict[str, Any]) -> dict[str, object]:
    pages = analysis.get("pages")
    if not isinstance(pages, list):
        raise _integrity("analysis lacks pages for normalized text hashes")
    normalized: list[str] = []
    for page in pages:
        if not isinstance(page, dict):
            raise _integrity("analysis page is invalid")
        collapsed = _normalize_text(page.get("text", ""), "collapse")
        normalized.append(unicodedata.normalize("NFC", collapsed))
    hashes = [hashlib.sha256(value.encode("utf-8")).hexdigest() for value in normalized]
    combined = "\f".join(normalized)
    return {
        "policy": "unicode-nfc-whitespace-collapse-sha256-v1",
        "document": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        "pages": hashes,
    }


def _comparison_metrics(report: dict[str, object]) -> dict[str, object]:
    issues = report.get("issues")
    rasters = report.get("rasters")
    if not isinstance(issues, list) or not isinstance(rasters, list):
        raise _integrity("comparison result is invalid")
    issue_counts: Counter[str] = Counter()
    affected_pages: dict[str, set[int]] = defaultdict(set)
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("code"), str):
            raise _integrity("comparison issue is invalid")
        code = str(issue["code"])
        issue_counts[code] += 1
        page = issue.get("page")
        if type(page) is int:
            affected_pages[code].add(page)
    raster_rmse = [
        float(item["rmse"])
        for item in rasters
        if isinstance(item, dict) and item.get("rmse") is not None
    ]
    raster_ratios = [
        float(item["different_pixel_ratio"])
        for item in rasters
        if isinstance(item, dict) and item.get("different_pixel_ratio") is not None
    ]
    return {
        "mismatch_classes": sorted(issue_counts),
        "issue_counts": dict(sorted(issue_counts.items())),
        "affected_page_counts": {
            code: len(pages) for code, pages in sorted(affected_pages.items())
        },
        "raster": {
            "pages_measured": len(rasters),
            "maximum_rmse": max(raster_rmse, default=None),
            "maximum_different_pixel_ratio": max(raster_ratios, default=None),
        },
    }


def _saved_document(
    path: Path,
    *,
    run_digest: str,
    document: NativeDocument,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    saved = _read_object(path, label="private comparison document result")
    if (
        saved.get("schema") != PRIVATE_CORPUS_DOCUMENT_SCHEMA
        or saved.get("run_digest") != run_digest
        or saved.get("source_identity")
        != {"size": document.source_size, "sha256": document.source_sha256}
        or saved.get("native", {}).get("evidence_manifest_sha256")
        != document.evidence_manifest_sha256
        or saved.get("status") not in {"compared", "conversion-failure", "analysis-failure"}
    ):
        raise _integrity("saved private comparison document identity is invalid")
    if saved.get("status") == "analysis-failure":
        return None
    if saved.get("status") == "compared":
        pdf = path.parent / "output" / "document.pdf"
        analysis = path.parent / "output" / "analysis.json"
        pdf_identity = saved.get("converter", {}).get("pdf")
        if (
            not isinstance(pdf_identity, dict)
            or _private_regular(pdf, label="saved converter PDF", maximum=_MAX_PDF_BYTES).st_size
            != pdf_identity.get("size")
            or sha256_file(pdf) != pdf_identity.get("sha256")
            or analysis.is_symlink()
            or not analysis.is_file()
        ):
            raise _integrity("saved converter analysis artifacts changed")
    return saved


def _run_document(
    *,
    home: Path,
    image_record: dict[str, Any],
    repo_root: Path,
    output: Path,
    document: NativeDocument,
    run_digest: str,
    timeout_seconds: float,
    resume: bool,
    thresholds: dict[str, float],
) -> dict[str, Any]:
    job = output / "documents" / f"doc-{document.index:05d}"
    result_path = job / "result.json"
    if resume:
        saved = _saved_document(result_path, run_digest=run_digest, document=document)
        if saved is not None:
            return saved
    if job.exists() or job.is_symlink():
        expected_parent = (output / "documents").resolve(strict=True)
        if (
            not resume
            or job.is_symlink()
            or not job.is_dir()
            or job.resolve().parent != expected_parent
            or re.fullmatch(r"doc-[0-9]{5}", job.name) is None
        ):
            raise _integrity("private comparison document workspace already exists")
        # A result.json is the commit marker. An interrupted workspace contains
        # only reproducible generated artifacts and is replaced on resume.
        shutil.rmtree(job)
    job.mkdir(mode=0o700, parents=True)
    base: dict[str, Any] = {
        "schema": PRIVATE_CORPUS_DOCUMENT_SCHEMA,
        "run_digest": run_digest,
        "source_identity": {"size": document.source_size, "sha256": document.source_sha256},
        "native": {
            "evidence_manifest_sha256": document.evidence_manifest_sha256,
            "reference_pdf": {
                "size": document.reference_pdf.stat().st_size,
                "sha256": sha256_file(document.reference_pdf),
            },
            "analysis": {
                "sha256": sha256_file(document.analysis_path),
                "page_count": document.analysis["page_count"],
                "normalized_text_hashes": _normalized_text_hashes(document.analysis),
            },
        },
    }
    try:
        converter = _run_converter(
            document,
            job,
            repo_root,
            timeout_seconds=timeout_seconds,
        )
    except OracleError as exc:
        converter = {
            "status": "failure",
            "exit_code": exc.exit_code,
            "failure_class": "converter-timeout" if exc.exit_code == 5 else "converter-bound",
            "process": _redacted_process(getattr(exc, "process_result", {})),
        }
    if converter.get("status") != "success":
        result = {
            **base,
            "status": "conversion-failure",
            "converter": converter,
            "comparison": {"mismatch_classes": ["conversion-failure"]},
        }
        atomic_write_json(result_path, result)
        return result
    try:
        analysis_path, analysis = _analyze_converter_pdf(
            home,
            image_record,
            job,
            dict(document.analysis["profile"]),
        )
        comparison = compare_analyses(
            document.evidence_job / "job.json",
            analysis_path,
            bbox_tolerance=thresholds["bbox_tolerance"],
            raster_rmse=thresholds["raster_rmse"],
            pixel_threshold=thresholds["pixel_threshold"],
            max_different_ratio=thresholds["max_different_ratio"],
            raster_backend="pillow",
        )
    except (OSError, ValueError, OracleError) as exc:
        result = {
            **base,
            "status": "analysis-failure",
            "converter": converter,
            "analysis_failure": {
                "class": "locked-analysis-failure",
                "exit_code": getattr(exc, "exit_code", EXIT_BACKEND),
            },
            "comparison": {"mismatch_classes": ["analysis-failure"]},
        }
        atomic_write_json(result_path, result)
        return result
    result = {
        **base,
        "status": "compared",
        "converter": {
            **converter,
            "analysis": {
                "sha256": sha256_file(analysis_path),
                "page_count": analysis["page_count"],
                "normalized_text_hashes": _normalized_text_hashes(analysis),
            },
        },
        "comparison": {
            "equal": comparison["equal"],
            "baseline_eligible": comparison["baseline_eligible"],
            "thresholds": comparison["thresholds"],
            "issues": comparison["issues"],
            "rasters": comparison["rasters"],
            **_comparison_metrics(comparison),
        },
    }
    atomic_write_json(result_path, result)
    return result


def _suppressed_groups(counter: Counter[str], *, minimum: int) -> dict[str, object]:
    reported = [
        {"class": name, "documents": count}
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        if count >= minimum
    ]
    suppressed = [count for count in counter.values() if count < minimum]
    return {
        "reported": reported,
        "suppressed": {
            "class_count": len(suppressed),
            "document_occurrences": sum(suppressed),
        },
    }


def build_aggregate(
    results: list[dict[str, Any]],
    failures: tuple[dict[str, object], ...],
    *,
    minimum_group_size: int = _MIN_PRIVACY_GROUP,
) -> dict[str, object]:
    if minimum_group_size < _MIN_PRIVACY_GROUP:
        raise _integrity(f"privacy groups must contain at least {_MIN_PRIVACY_GROUP} documents")
    mismatch_documents: Counter[str] = Counter()
    issue_occurrences: Counter[str] = Counter()
    compared = 0
    equal = 0
    conversion_failures = 0
    analysis_failures = 0
    for result in results:
        status = result.get("status")
        comparison = result.get("comparison")
        if not isinstance(comparison, dict):
            raise _integrity("private document result lacks comparison data")
        classes = comparison.get("mismatch_classes")
        if not isinstance(classes, list) or any(not isinstance(value, str) for value in classes):
            raise _integrity("private document mismatch classes are invalid")
        mismatch_documents.update(set(classes))
        counts = comparison.get("issue_counts", {})
        if isinstance(counts, dict):
            for name, count in counts.items():
                if isinstance(name, str) and type(count) is int and count >= 0:
                    issue_occurrences[name] += count
        if status == "compared":
            compared += 1
            equal += int(comparison.get("equal") is True)
        elif status == "conversion-failure":
            conversion_failures += 1
        elif status == "analysis-failure":
            analysis_failures += 1
        else:
            raise _integrity("private document result status is invalid")
    mismatch_groups = _suppressed_groups(mismatch_documents, minimum=minimum_group_size)
    for item in mismatch_groups["reported"]:
        assert isinstance(item, dict)
        name = str(item["class"])
        item["issue_occurrences"] = issue_occurrences.get(name, int(item["documents"]))
        item["frequency_percent"] = round(100 * int(item["documents"]) / len(results), 1)
        item["impact_scope"] = (
            "whole-document"
            if name in {"conversion-failure", "analysis-failure", "page-count"}
            else "page-or-layout"
        )
    failure_counter = Counter(str(item["class"]) for item in failures)
    failure_status = Counter(str(item["status"]) for item in failures)
    priorities: list[dict[str, object]] = []
    for item in mismatch_groups["reported"][:10]:
        assert isinstance(item, dict)
        name = str(item["class"])
        target = _FIXTURE_TARGETS.get(name)
        if target is None:
            continue
        priorities.append(
            {
                "rank": len(priorities) + 1,
                "mismatch_class": name,
                "documents": item["documents"],
                "invented_atomic_fixture": target,
            }
        )
    aggregate: dict[str, object] = {
        "schema": PRIVATE_CORPUS_AGGREGATE_SCHEMA,
        "privacy": {
            "minimum_group_size": minimum_group_size,
            "detail_location": "ignored-private-workspace-only",
            "excluded": [
                "document-text",
                "source-paths",
                "filenames",
                "personal-identifiers",
                "timestamps",
                "rare-groups",
            ],
        },
        "native_outcomes": {
            "successful_selected": len(results),
            "failed_or_blocked_separate": len(failures),
            "failed": failure_status.get("failure", 0),
            "blocked": failure_status.get("blocked", 0),
            "classes": _suppressed_groups(failure_counter, minimum=minimum_group_size),
        },
        "differential_outcomes": {
            "selected": len(results),
            "compared": compared,
            "equal_under_thresholds": equal,
            "different_under_thresholds": compared - equal,
            "conversion_failures": conversion_failures,
            "analysis_failures": analysis_failures,
            "native_converter_pdf_byte_comparisons": 0,
        },
        "mismatch_classes": mismatch_groups,
        "fixture_priorities": priorities,
    }
    audit_aggregate(aggregate)
    return aggregate


def audit_aggregate(report: dict[str, object]) -> None:
    """Fail closed if an aggregate contains detail-bearing keys or rare groups."""
    forbidden_keys = {
        "path",
        "filename",
        "source",
        "guest",
        "index",
        "sha256",
        "timestamp",
        "text",
        "error",
        "duration_seconds",
    }

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() in forbidden_keys:
                    raise _integrity("aggregate contains a detail-bearing field")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(report)
    if report.get("schema") != PRIVATE_CORPUS_AGGREGATE_SCHEMA:
        raise _integrity("aggregate schema is invalid")
    privacy = report.get("privacy")
    minimum = privacy.get("minimum_group_size") if isinstance(privacy, dict) else None
    if type(minimum) is not int or minimum < _MIN_PRIVACY_GROUP:
        raise _integrity("aggregate privacy threshold is invalid")
    for section_name in ("mismatch_classes",):
        section = report.get(section_name)
        if not isinstance(section, dict) or not isinstance(section.get("reported"), list):
            raise _integrity("aggregate grouped section is invalid")
        for item in section["reported"]:
            if not isinstance(item, dict) or int(item.get("documents", 0)) < minimum:
                raise _integrity("aggregate exposes a rare group")


def _prepare_output(home: Path, output: Path, *, resume: bool) -> Path:
    namespace = home / "private-comparisons"
    if not namespace.exists() and not namespace.is_symlink():
        namespace.mkdir(mode=0o700)
    if namespace.is_symlink() or not namespace.is_dir() or namespace.stat().st_mode & 0o077:
        raise _integrity("private comparison namespace is unsafe")
    resolved_namespace = namespace.resolve()
    candidate = output.expanduser().absolute().resolve(strict=False)
    if resolved_namespace not in candidate.parents:
        raise _integrity("comparison output must stay below the ignored private-comparisons root")
    if resume:
        if candidate.is_symlink() or not candidate.is_dir() or candidate.stat().st_mode & 0o077:
            raise _integrity("resumable private comparison output is unsafe")
    else:
        if candidate.exists() or candidate.is_symlink():
            raise _integrity("private comparison output already exists; pass --resume")
        candidate.mkdir(mode=0o700, parents=True)
        (candidate / "documents").mkdir(mode=0o700)
    return candidate


def run_private_corpus_comparison(
    *,
    home: Path,
    repo_root: Path,
    batch_root: Path,
    source_root: Path,
    output: Path,
    expected_successes: int,
    expected_failures: int,
    converter_timeout_seconds: float = 60.0,
    workers: int = 2,
    resume: bool = False,
    progress: Any = None,
    bbox_tolerance: float = 0.5,
    raster_rmse: float = 0.01,
    pixel_threshold: float = 0.05,
    max_different_ratio: float = 0.001,
) -> dict[str, object]:
    if isinstance(workers, bool) or not 1 <= workers <= 4:
        raise _integrity("comparison worker count must be between one and four")
    home = home.expanduser().resolve(strict=True)
    image_record = load_verified_image(home)
    selection = select_native_documents(
        home=home,
        batch_root=batch_root,
        source_root=source_root,
        image_record=image_record,
        expected_successes=expected_successes,
        expected_failures=expected_failures,
    )
    if selection.toolchain != _toolchain_identity(image_record):
        raise _integrity("native evidence and current locked analysis image identities differ")
    output = _prepare_output(home, output, resume=resume)
    profile = converter_profile(repo_root, timeout_seconds=converter_timeout_seconds)
    measurements = measurement_profile(repo_root)
    thresholds = {
        "bbox_tolerance": float(bbox_tolerance),
        "raster_rmse": float(raster_rmse),
        "pixel_threshold": float(pixel_threshold),
        "max_different_ratio": float(max_different_ratio),
    }
    if any(value < 0 for value in thresholds.values()) or any(
        thresholds[name] > 1
        for name in ("raster_rmse", "pixel_threshold", "max_different_ratio")
    ):
        raise _integrity("comparison thresholds are invalid")
    run_identity = {
        "schema": PRIVATE_CORPUS_RUN_SCHEMA,
        "batch_plan_digest": selection.plan_digest,
        "batch_journal_sha256": selection.batch_sha256,
        "source_inventory_digest": selection.source_inventory_digest,
        "native_success_count": len(selection.documents),
        "native_failure_count": len(selection.failures),
        "toolchain": image_record,
        "analysis_profile": selection.analysis_profile,
        "converter_profile": profile,
        "measurement_profile": measurements,
        "comparison_thresholds": thresholds,
        "pdf_byte_equality": False,
    }
    run_digest = digest_json(run_identity)
    run_manifest = {
        **run_identity,
        "run_digest": run_digest,
        "inputs": {
            "native_batch": str(batch_root.expanduser().absolute()),
            "source_corpus": str(source_root.expanduser().absolute()),
        },
    }
    run_path = output / "run.json"
    if resume:
        if _read_object(run_path, label="private comparison run manifest") != run_manifest:
            raise _integrity("private comparison inputs or profiles changed since the saved run")
    else:
        atomic_write_json(run_path, run_manifest)
        atomic_write_json(
            output / "native-failures.json",
            {"schema": PRIVATE_CORPUS_FAILURES_SCHEMA, "records": selection.failures},
        )
    def compare_document(document: NativeDocument) -> dict[str, Any]:
        return _run_document(
            home=home,
            image_record=image_record,
            repo_root=repo_root,
            output=output,
            document=document,
            run_digest=run_digest,
            timeout_seconds=converter_timeout_seconds,
            resume=resume,
            thresholds=thresholds,
        )

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="private-corpus") as executor:
        for ordinal, result in enumerate(executor.map(compare_document, selection.documents), 1):
            results.append(result)
            event = {
                "schema": "amipro-oracle-private-corpus-progress-v1",
                "completed": ordinal,
                "selected": len(selection.documents),
                "compared": sum(item.get("status") == "compared" for item in results),
                "conversion_failures": sum(
                    item.get("status") == "conversion-failure" for item in results
                ),
                "analysis_failures": sum(
                    item.get("status") == "analysis-failure" for item in results
                ),
            }
            atomic_write_json(output / "progress.json", event)
            if progress is not None:
                progress(event)
    aggregate = build_aggregate(results, selection.failures)
    atomic_write_json(output / "aggregate.json", aggregate)
    return aggregate
