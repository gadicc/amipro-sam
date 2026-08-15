from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import EXIT_BACKEND, EXIT_DIFFERENT, EXIT_INTEGRITY
from .errors import OracleError
from .io import atomic_write, atomic_write_json, digest_json, read_json_object, sha256_file

BATCH_PLAN_SCHEMA = "amipro-oracle-real-batch-plan-v1"
BATCH_RESULT_SCHEMA = "amipro-oracle-real-batch-v1"
BATCH_DOCUMENT_SCHEMA = "amipro-oracle-real-batch-document-v1"
BATCH_FAILURE_SCHEMA = "amipro-oracle-real-batch-failure-v1"
NATIVE_SAM_AUDIT_SCHEMA = "amipro-oracle-native-sam-audit-v1"

MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_REFERENCE_PDF_BYTES = 64 * 1024 * 1024
MAX_BATCH_DOCUMENTS = 99_999
DEFAULT_DOCUMENT_TIMEOUT_SECONDS = 180.0
MAX_DOCUMENT_TIMEOUT_SECONDS = 600.0

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_STAGED_NAME = re.compile(r"DOC[0-9]{5}[.]SAM\Z")
_SECTION_LINE = re.compile(rb"(?m)^\[([^\]\r\n]{1,64})\][ \t]*\r?$")
_EMBEDDED_ROW = re.compile(
    rb"(?m)^\s*\S+\s+(\.[A-Za-z0-9_]{1,16})\s+\d+\s+\d+\s+\d+\s+\d+\s*$"
)
_DANGEROUS_SECTIONS = frozenset({"newmac", "macro", "frmmac", "dde", "ole", "link"})
_EXTERNAL_PATH_SECTIONS = frozenset({"sty", "files", "book", "master", "recfile"})
_PRINTER_SECTIONS = frozenset({"prn", "port"})
_ACTIVE_EMBEDDED_EXTENSIONS = frozenset({".ole"})
_PATH_SYNTAX = re.compile(rb"(?:[A-Za-z]:[\\/]|\\\\|/|\.\.|[a-z]+://)", re.I)
_DYNAMIC_EXPRESSION = re.compile(rb"<:[XZ](?:~)?")


def staged_name(index: int) -> str:
    if isinstance(index, bool) or not 1 <= index <= MAX_BATCH_DOCUMENTS:
        raise OracleError("batch document index is outside its bound", exit_code=EXIT_INTEGRITY)
    return f"DOC{index:05d}.SAM"


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_bounded_file(path: Path, *, maximum: int, label: str) -> bytes:
    absolute = path.expanduser().absolute()
    try:
        initial = absolute.lstat()
    except FileNotFoundError as exc:
        raise OracleError(f"{label} is missing", exit_code=EXIT_INTEGRITY) from exc
    if not stat.S_ISREG(initial.st_mode) or stat.S_ISLNK(initial.st_mode):
        raise OracleError(
            f"{label} must be a regular non-symlink file",
            exit_code=EXIT_INTEGRITY,
        )
    if not 1 <= initial.st_size <= maximum:
        raise OracleError(
            f"{label} must be between 1 and {maximum} bytes",
            exit_code=EXIT_INTEGRITY,
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise OracleError(f"cannot open {label} safely", exit_code=EXIT_INTEGRITY) from exc
    try:
        before = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(initial) or not stat.S_ISREG(before.st_mode):
            raise OracleError(f"{label} changed before reading", exit_code=EXIT_INTEGRITY)
        chunks: list[bytes] = []
        remaining = initial.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise OracleError(f"{label} was truncated while reading", exit_code=EXIT_INTEGRITY)
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise OracleError(f"{label} grew while reading", exit_code=EXIT_INTEGRITY)
        after = os.fstat(descriptor)
        if _file_identity(after) != _file_identity(initial):
            raise OracleError(f"{label} changed while reading", exit_code=EXIT_INTEGRITY)
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _read_bounded_source(path: Path) -> bytes:
    return _read_bounded_file(path, maximum=MAX_DOCUMENT_BYTES, label="batch source")


def _section_payloads(payload: bytes) -> dict[str, list[bytes]]:
    matches = list(_SECTION_LINE.finditer(payload))
    sections: dict[str, list[bytes]] = {}
    for index, match in enumerate(matches):
        name = match.group(1).decode("latin-1").strip().casefold()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(payload)
        sections.setdefault(name, []).append(payload[match.end() : end].strip(b" \t\r\n"))
    return sections


def audit_native_sam(payload: bytes, *, guest_name: str) -> dict[str, object]:
    if _STAGED_NAME.fullmatch(guest_name) is None:
        raise OracleError("invalid DOS-safe batch name", exit_code=EXIT_INTEGRITY)
    if not 1 <= len(payload) <= MAX_DOCUMENT_BYTES:
        raise OracleError("native SAM size is outside its bound", exit_code=EXIT_INTEGRITY)
    sections = _section_payloads(payload)
    if "ver" not in sections or "edoc" not in sections:
        raise OracleError(
            "native batch input lacks the required version or document-text section",
            exit_code=EXIT_INTEGRITY,
        )
    blocked: list[str] = []
    for name in sorted(_DANGEROUS_SECTIONS & sections.keys()):
        blocked.append(f"active-section:{name}")
    for name in sorted(_EXTERNAL_PATH_SECTIONS & sections.keys()):
        if any(value for value in sections[name]):
            blocked.append(f"external-metadata:{name}")
    for name in sorted(_PRINTER_SECTIONS & sections.keys()):
        if any(_PATH_SYNTAX.search(value) for value in sections[name] if value):
            blocked.append(f"external-printer-path:{name}")
    if _DYNAMIC_EXPRESSION.search(payload):
        blocked.append("active-inline:dynamic-expression")
    extensions = sorted(
        {match.group(1).decode("ascii").casefold() for match in _EMBEDDED_ROW.finditer(payload)}
    )
    for extension in sorted(_ACTIVE_EMBEDDED_EXTENSIONS & set(extensions)):
        blocked.append(f"active-embedded:{extension}")
    if blocked:
        raise OracleError(
            "native batch input failed active/external-content preflight: " + ", ".join(blocked),
            exit_code=EXIT_INTEGRITY,
        )
    return {
        "schema": NATIVE_SAM_AUDIT_SCHEMA,
        "status": "safe-to-open-in-isolated-native-oracle",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "guest_name": guest_name,
        "section_count": sum(len(values) for values in sections.values()),
        "embedded_extensions": extensions,
        "policies": {
            "active_sections": "rejected",
            "ole_payloads": "rejected",
            "external_stylesheet_file_book_master_merge_metadata": "rejected-when-nonempty",
            "dynamic_expressions": "rejected",
            "printer_metadata_paths": "rejected",
            "source_directory_guest_mount": False,
        },
    }


def read_and_audit_source(path: Path, *, guest_name: str) -> tuple[bytes, dict[str, object]]:
    payload = _read_bounded_source(path)
    return payload, audit_native_sam(payload, guest_name=guest_name)


def _relative_source(source: Path, root: Path) -> str:
    relative = source.relative_to(root).as_posix()
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise OracleError("unsafe batch source path", exit_code=EXIT_INTEGRITY)
    return relative


def build_batch_plan(sources: list[Path], input_root: Path) -> dict[str, Any]:
    if not 1 <= len(sources) <= MAX_BATCH_DOCUMENTS:
        raise OracleError("batch document count is outside its bound", exit_code=EXIT_INTEGRITY)
    records: list[dict[str, object]] = []
    for index, source in enumerate(sources, start=1):
        guest = staged_name(index)
        relative = _relative_source(source, input_root)
        try:
            payload = _read_bounded_source(source)
        except OracleError as exc:
            records.append(
                {
                    "index": index,
                    "source": relative,
                    "guest": guest,
                    "preflight": "blocked",
                    "error": str(exc),
                    "exit_code": exc.exit_code,
                }
            )
            continue
        identity = {
            "source_size": len(payload),
            "source_sha256": hashlib.sha256(payload).hexdigest(),
        }
        try:
            audit = audit_native_sam(payload, guest_name=guest)
        except OracleError as exc:
            records.append(
                {
                    "index": index,
                    "source": relative,
                    "guest": guest,
                    **identity,
                    "preflight": "blocked",
                    "error": str(exc),
                    "exit_code": exc.exit_code,
                }
            )
        else:
            records.append(
                {
                    "index": index,
                    "source": relative,
                    "guest": guest,
                    "preflight": "ready",
                    **identity,
                    "audit": audit,
                }
            )
    identity = {
        "schema": BATCH_PLAN_SCHEMA,
        "document_count": len(records),
        "records": records,
    }
    return {**identity, "plan_digest": digest_json(identity)}


def _validate_output_root(output: Path) -> None:
    if output.is_symlink() or not output.is_dir():
        raise OracleError("batch output is not a real directory", exit_code=EXIT_INTEGRITY)
    if output.stat().st_mode & 0o077:
        raise OracleError("real batch output must be private (mode 0700)", exit_code=EXIT_INTEGRITY)
    for name in ("jobs", "reference-pdf"):
        child = output / name
        if child.is_symlink() or not child.is_dir() or child.stat().st_mode & 0o077:
            raise OracleError(
                f"batch output has an unsafe {name} directory",
                exit_code=EXIT_INTEGRITY,
            )


def _name_map(plan: dict[str, Any]) -> dict[str, object]:
    return {
        "schema": "amipro-oracle-batch-name-map-v1",
        "records": [
            {
                "index": item["index"],
                "source": item["source"],
                "guest": item["guest"],
                "pdf": f"reference-pdf/{str(item['guest'])[:-4]}.pdf",
            }
            for item in plan["records"]
        ],
    }


def _safe_failure_message(exc: BaseException) -> str:
    if isinstance(exc, OracleError):
        return str(exc)
    return f"{type(exc).__name__}: document job failed before completion"


def _batch_summary(
    plan: dict[str, Any],
    jobs: list[dict[str, object]],
    *,
    interrupted: bool = False,
) -> dict[str, Any]:
    successes = sum(item.get("status") == "success" for item in jobs)
    failures = sum(item.get("status") in {"failure", "blocked"} for item in jobs)
    pending = int(plan["document_count"]) - len(jobs)
    if interrupted or pending:
        status = "interrupted"
    elif failures:
        status = "complete-with-failures"
    else:
        status = "success"
    return {
        "schema": BATCH_RESULT_SCHEMA,
        "backend": "real",
        "baseline_eligible": False,
        "status": status,
        "plan_digest": plan["plan_digest"],
        "document_count": plan["document_count"],
        "success_count": successes,
        "failure_count": failures,
        "pending_count": pending,
        "jobs": jobs,
    }


def _validated_resume_result(
    output: Path,
    record: dict[str, object],
) -> dict[str, object] | None:
    index = int(record["index"])
    path = output / "jobs" / f"{index:05d}" / "result.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise OracleError("unsafe batch resume result", exit_code=EXIT_INTEGRITY)
    try:
        result = read_json_object(path)
    except (OSError, ValueError) as exc:
        raise OracleError("invalid batch resume result", exit_code=EXIT_INTEGRITY) from exc
    pdf_record = result.get("pdf")
    expected_pdf_path = f"reference-pdf/{str(record['guest'])[:-4]}.pdf"
    pdf_path = pdf_record.get("path") if isinstance(pdf_record, dict) else None
    pure_pdf = PurePosixPath(pdf_path) if isinstance(pdf_path, str) else None
    pdf = output / expected_pdf_path
    if (
        result.get("schema") != BATCH_DOCUMENT_SCHEMA
        or result.get("status") != "success"
        or result.get("backend") != "real"
        or result.get("baseline_eligible") is not False
        or result.get("index") != index
        or result.get("source") != record.get("source")
        or result.get("guest") != record.get("guest")
        or result.get("source_sha256") != record.get("source_sha256")
        or not isinstance(result.get("job_manifest_sha256"), str)
        or _SHA256.fullmatch(result["job_manifest_sha256"]) is None
        or not isinstance(result.get("evidence_job"), str)
        or re.fullmatch(r"batch-document-[a-z0-9_-]+", result["evidence_job"]) is None
        or not isinstance(pdf_record, dict)
        or pdf_path != expected_pdf_path
        or pure_pdf is None
        or pure_pdf.is_absolute()
        or any(part in {"", ".", ".."} for part in pure_pdf.parts)
        or not isinstance(pdf_record.get("sha256"), str)
        or _SHA256.fullmatch(pdf_record["sha256"]) is None
        or type(pdf_record.get("size")) is not int
        or not 1 <= pdf_record["size"] <= MAX_REFERENCE_PDF_BYTES
        or type(pdf_record.get("page_count")) is not int
        or not 1 <= pdf_record["page_count"] <= 128
        or pdf.is_symlink()
        or not pdf.is_file()
        or pdf.stat().st_size != pdf_record["size"]
        or sha256_file(pdf) != pdf_record["sha256"]
    ):
        raise OracleError("batch resume result failed integrity checks", exit_code=EXIT_INTEGRITY)
    return result


def _validated_blocked_result(
    output: Path,
    record: dict[str, object],
) -> dict[str, object] | None:
    if record.get("preflight") != "blocked":
        return None
    index = int(record["index"])
    path = output / "jobs" / f"{index:05d}" / "failure.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise OracleError("unsafe blocked batch result", exit_code=EXIT_INTEGRITY)
    try:
        result = read_json_object(path)
    except (OSError, ValueError) as exc:
        raise OracleError("invalid blocked batch result", exit_code=EXIT_INTEGRITY) from exc
    expected = {
        "schema": BATCH_FAILURE_SCHEMA,
        "backend": "real",
        "baseline_eligible": False,
        "status": "blocked",
        "index": index,
        "source": record["source"],
        "source_size": record.get("source_size"),
        "source_sha256": record.get("source_sha256"),
        "guest": record["guest"],
        "error": record["error"],
        "exit_code": record["exit_code"],
    }
    if result != expected:
        raise OracleError("blocked batch result failed integrity checks", exit_code=EXIT_INTEGRITY)
    return result


def _next_attempt_directory(job_root: Path) -> Path:
    attempts = job_root / "attempts"
    if attempts.is_symlink() or (attempts.exists() and not attempts.is_dir()):
        raise OracleError("unsafe batch attempt directory", exit_code=EXIT_INTEGRITY)
    attempts.mkdir(mode=0o700, exist_ok=True)
    for index in range(1, 10_000):
        candidate = attempts / f"{index:04d}"
        if not candidate.exists() and not candidate.is_symlink():
            candidate.mkdir(mode=0o700)
            return candidate
    raise OracleError("batch attempt count is outside its bound", exit_code=EXIT_INTEGRITY)


def run_real_batch(
    *,
    sources: list[Path],
    input_root: Path,
    output: Path,
    worker: Callable[[Path, str, float], dict[str, Any]],
    timeout_seconds: float = DEFAULT_DOCUMENT_TIMEOUT_SECONDS,
    resume: bool = False,
) -> tuple[dict[str, Any], int]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 1 <= timeout_seconds <= MAX_DOCUMENT_TIMEOUT_SECONDS
    ):
        raise OracleError(
            f"batch document timeout must be between 1 and {MAX_DOCUMENT_TIMEOUT_SECONDS} seconds",
            exit_code=EXIT_INTEGRITY,
        )
    plan = build_batch_plan(sources, input_root)
    if resume:
        _validate_output_root(output)
        plan_path = output / "plan.json"
        if plan_path.is_symlink() or not plan_path.is_file():
            raise OracleError("resumable batch plan is missing", exit_code=EXIT_INTEGRITY)
        try:
            stored_plan = read_json_object(plan_path)
        except (OSError, ValueError) as exc:
            raise OracleError("resumable batch plan is invalid", exit_code=EXIT_INTEGRITY) from exc
        if stored_plan != plan:
            raise OracleError("batch inputs changed since the saved plan", exit_code=EXIT_INTEGRITY)
        name_map_path = output / "name-map.json"
        if name_map_path.is_symlink() or not name_map_path.is_file():
            raise OracleError("resumable batch name map is missing", exit_code=EXIT_INTEGRITY)
        try:
            stored_name_map = read_json_object(name_map_path)
        except (OSError, ValueError) as exc:
            raise OracleError(
                "resumable batch name map is invalid",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        if stored_name_map != _name_map(plan):
            raise OracleError("resumable batch name map changed", exit_code=EXIT_INTEGRITY)
    else:
        if output.exists() or output.is_symlink():
            raise OracleError(
                "batch output already exists; pass --resume",
                exit_code=EXIT_INTEGRITY,
            )
        output.mkdir(mode=0o700, parents=True)
        output.chmod(0o700)
        (output / "jobs").mkdir(mode=0o700)
        (output / "reference-pdf").mkdir(mode=0o700)
        atomic_write_json(output / "plan.json", plan)
        atomic_write_json(output / "name-map.json", _name_map(plan))
    jobs: list[dict[str, object]] = []
    try:
        for record in plan["records"]:
            previous = _validated_resume_result(output, record) if resume else None
            if previous is None and resume:
                previous = _validated_blocked_result(output, record)
            if previous is not None:
                jobs.append(previous)
                atomic_write_json(output / "batch.json", _batch_summary(plan, jobs))
                continue
            index = int(record["index"])
            job_root = output / "jobs" / f"{index:05d}"
            if job_root.exists() or job_root.is_symlink():
                if not resume or job_root.is_symlink() or not job_root.is_dir():
                    raise OracleError(
                        "unsafe existing batch job directory",
                        exit_code=EXIT_INTEGRITY,
                    )
            else:
                job_root.mkdir(mode=0o700)
            attempt_root = _next_attempt_directory(job_root)
            if record["preflight"] != "ready":
                failure = {
                    "schema": BATCH_FAILURE_SCHEMA,
                    "backend": "real",
                    "baseline_eligible": False,
                    "status": "blocked",
                    "index": index,
                    "source": record["source"],
                    "source_size": record.get("source_size"),
                    "source_sha256": record.get("source_sha256"),
                    "guest": record["guest"],
                    "error": record["error"],
                    "exit_code": record["exit_code"],
                }
                atomic_write_json(attempt_root / "failure.json", failure)
                atomic_write_json(job_root / "failure.json", failure)
                jobs.append(failure)
                atomic_write_json(output / "batch.json", _batch_summary(plan, jobs))
                continue
            source = input_root / str(record["source"])
            try:
                _payload, current_audit = read_and_audit_source(
                    source,
                    guest_name=str(record["guest"]),
                )
                if current_audit != record["audit"]:
                    raise OracleError(
                        "batch source changed after planning",
                        exit_code=EXIT_INTEGRITY,
                    )
                native = worker(source, str(record["guest"]), float(timeout_seconds))
                evidence_job = native.get("evidence_job")
                pdf_source_value = native.get("pdf_path")
                if (
                    not isinstance(evidence_job, str)
                    or re.fullmatch(r"batch-document-[a-z0-9_-]+", evidence_job) is None
                    or not isinstance(pdf_source_value, str)
                    or not isinstance(native.get("job_manifest_sha256"), str)
                    or _SHA256.fullmatch(native["job_manifest_sha256"]) is None
                    or type(native.get("page_count")) is not int
                    or not 1 <= native["page_count"] <= 128
                ):
                    raise OracleError(
                        "native batch worker returned invalid evidence",
                        exit_code=EXIT_BACKEND,
                    )
                pdf_source = Path(pdf_source_value)
                pdf_payload = _read_bounded_file(
                    pdf_source,
                    maximum=MAX_REFERENCE_PDF_BYTES,
                    label="native batch PDF",
                )
                pdf_relative = f"reference-pdf/{str(record['guest'])[:-4]}.pdf"
                pdf_output = output / pdf_relative
                atomic_write(pdf_output, pdf_payload)
                pdf = {
                    "path": pdf_relative,
                    "size": pdf_output.stat().st_size,
                    "sha256": sha256_file(pdf_output),
                    "page_count": native.get("page_count"),
                }
                result = {
                    "schema": BATCH_DOCUMENT_SCHEMA,
                    "backend": "real",
                    "baseline_eligible": False,
                    "status": "success",
                    "index": index,
                    "source": record["source"],
                    "source_size": record["source_size"],
                    "source_sha256": record["source_sha256"],
                    "guest": record["guest"],
                    "evidence_job": evidence_job,
                    "job_manifest_sha256": native.get("job_manifest_sha256"),
                    "pdf": pdf,
                }
                atomic_write_json(attempt_root / "result.json", result)
                atomic_write_json(job_root / "result.json", result)
                (job_root / "failure.json").unlink(missing_ok=True)
                jobs.append(result)
            except (OSError, OracleError, TypeError, ValueError) as exc:
                exit_code = exc.exit_code if isinstance(exc, OracleError) else EXIT_BACKEND
                failure = {
                    "schema": BATCH_FAILURE_SCHEMA,
                    "backend": "real",
                    "baseline_eligible": False,
                    "status": "failure",
                    "index": index,
                    "source": record["source"],
                    "source_sha256": record.get("source_sha256"),
                    "guest": record["guest"],
                    "error": _safe_failure_message(exc),
                    "exit_code": exit_code,
                }
                atomic_write_json(attempt_root / "failure.json", failure)
                atomic_write_json(job_root / "failure.json", failure)
                jobs.append(failure)
            atomic_write_json(output / "batch.json", _batch_summary(plan, jobs))
    except BaseException:
        atomic_write_json(output / "batch.json", _batch_summary(plan, jobs, interrupted=True))
        raise
    summary = _batch_summary(plan, jobs)
    atomic_write_json(output / "batch.json", summary)
    return summary, EXIT_DIFFERENT if summary["failure_count"] else 0
