from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import stat
from contextlib import contextmanager
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .constants import EXIT_BACKEND, EXIT_DIFFERENT, EXIT_INTEGRITY
from .errors import OracleError
from .io import atomic_write, atomic_write_json, digest_json, read_json_object, sha256_file

BATCH_PLAN_SCHEMA = "amipro-oracle-real-batch-plan-v1"
BATCH_RESULT_SCHEMA = "amipro-oracle-real-batch-v1"
BATCH_PROGRESS_SCHEMA = "amipro-oracle-real-batch-progress-v1"
BATCH_STATUS_SCHEMA = "amipro-oracle-real-batch-status-v1"
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


@contextmanager
def batch_coordinator_lock(home: Path, output: Path) -> Iterator[None]:
    locks = home / "locks"
    if (
        locks.is_symlink()
        or not locks.is_dir()
        or stat.S_IMODE(locks.stat().st_mode) & 0o077
    ):
        raise OracleError(
            "oracle lock directory is missing or unsafe",
            exit_code=EXIT_INTEGRITY,
        )
    identity = hashlib.sha256(str(output.absolute()).encode("utf-8")).hexdigest()
    path = locks / f"batch-{identity}.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise OracleError(
            "cannot open the private batch coordinator lock",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise OracleError(
                "private batch coordinator lock is unsafe",
                exit_code=EXIT_INTEGRITY,
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise OracleError(
                    "this batch output is already being processed by another coordinator",
                    exit_code=EXIT_BACKEND,
                ) from exc
            raise
        yield
    finally:
        os.close(descriptor)


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
    pdf_names: set[str] = set()
    for record in records:
        candidate = _reference_pdf_relative(record).casefold()
        if candidate in pdf_names:
            raise OracleError(
                "batch sources collide after changing the extension to PDF",
                exit_code=EXIT_INTEGRITY,
            )
        pdf_names.add(candidate)
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
                "pdf": _reference_pdf_relative(item),
            }
            for item in plan["records"]
        ],
    }


def _reference_pdf_relative(record: dict[str, object]) -> str:
    source = record.get("source")
    if not isinstance(source, str) or not source.casefold().endswith(".sam"):
        raise OracleError("invalid source name for reference PDF", exit_code=EXIT_INTEGRITY)
    pure = PurePosixPath(source)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise OracleError("unsafe source name for reference PDF", exit_code=EXIT_INTEGRITY)
    filename = pure.name[:-4] + ".pdf"
    return PurePosixPath("reference-pdf", *pure.parts[:-1], filename).as_posix()


def _reference_pdf_path(
    output: Path,
    record: dict[str, object],
    *,
    create_parents: bool,
) -> tuple[str, Path]:
    relative = _reference_pdf_relative(record)
    pure = PurePosixPath(relative)
    current = output
    for part in pure.parent.parts:
        current /= part
        if not current.exists() and not current.is_symlink() and create_parents:
            current.mkdir(mode=0o700)
        if (
            current.is_symlink()
            or not current.is_dir()
            or current.stat().st_mode & 0o077
        ):
            raise OracleError(
                "reference PDF parent is unsafe",
                exit_code=EXIT_INTEGRITY,
            )
    return relative, output / relative


def _safe_failure_message(exc: BaseException) -> str:
    if isinstance(exc, OracleError):
        return str(exc)
    return f"{type(exc).__name__}: document job failed before completion"


def _batch_summary(
    plan: dict[str, Any],
    jobs: list[dict[str, object]],
    *,
    interrupted: bool = False,
    running: bool = False,
) -> dict[str, Any]:
    successes = sum(item.get("status") == "success" for item in jobs)
    failures = sum(item.get("status") in {"failure", "blocked"} for item in jobs)
    pending = int(plan["document_count"]) - len(jobs)
    if interrupted:
        status = "interrupted"
    elif pending:
        status = "running" if running else "interrupted"
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


def _progress_payload(
    plan: dict[str, Any],
    jobs: list[dict[str, object]],
    *,
    event: str,
    record: dict[str, object] | None = None,
) -> dict[str, object]:
    successes = sum(item.get("status") == "success" for item in jobs)
    failures = sum(item.get("status") in {"failure", "blocked"} for item in jobs)
    document = None
    if record is not None:
        document = {"index": record["index"], "guest": record["guest"]}
    return {
        "schema": BATCH_PROGRESS_SCHEMA,
        "event": event,
        "plan_digest": plan["plan_digest"],
        "document_count": plan["document_count"],
        "completed_count": len(jobs),
        "success_count": successes,
        "failure_count": failures,
        "pending_count": int(plan["document_count"]) - len(jobs),
        "document": document,
    }


def _publish_progress(
    output: Path,
    plan: dict[str, Any],
    jobs: list[dict[str, object]],
    *,
    event: str,
    record: dict[str, object] | None = None,
    callback: Callable[[dict[str, object]], None] | None = None,
) -> None:
    progress = _progress_payload(plan, jobs, event=event, record=record)
    atomic_write_json(output / "progress.json", progress)
    if callback is not None:
        callback(progress)


def _read_saved_plan(output: Path) -> dict[str, Any]:
    _validate_output_root(output)
    plan_path = output / "plan.json"
    if plan_path.is_symlink() or not plan_path.is_file():
        raise OracleError("batch plan is missing", exit_code=EXIT_INTEGRITY)
    try:
        plan = read_json_object(plan_path)
    except (OSError, ValueError) as exc:
        raise OracleError("batch plan is invalid", exit_code=EXIT_INTEGRITY) from exc
    count = plan.get("document_count")
    records = plan.get("records")
    identity = {
        "schema": plan.get("schema"),
        "document_count": count,
        "records": records,
    }
    if (
        plan.get("schema") != BATCH_PLAN_SCHEMA
        or type(count) is not int
        or not 1 <= count <= MAX_BATCH_DOCUMENTS
        or not isinstance(records, list)
        or len(records) != count
        or plan.get("plan_digest") != digest_json(identity)
    ):
        raise OracleError("batch plan failed integrity checks", exit_code=EXIT_INTEGRITY)
    for expected_index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise OracleError("batch plan record is invalid", exit_code=EXIT_INTEGRITY)
        source = record.get("source")
        pure = PurePosixPath(source) if isinstance(source, str) else None
        source_hash = record.get("source_sha256")
        if (
            record.get("index") != expected_index
            or record.get("guest") != staged_name(expected_index)
            or pure is None
            or pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
            or record.get("preflight") not in {"ready", "blocked"}
            or (record.get("preflight") == "ready" and source_hash is None)
            or (
                source_hash is not None
                and (not isinstance(source_hash, str) or _SHA256.fullmatch(source_hash) is None)
            )
        ):
            raise OracleError("batch plan record failed integrity checks", exit_code=EXIT_INTEGRITY)
    return plan


def _read_batch_journal(output: Path, plan: dict[str, Any]) -> dict[str, Any] | None:
    path = output / "batch.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise OracleError("batch journal is unsafe", exit_code=EXIT_INTEGRITY)
    try:
        journal = read_json_object(path)
    except (OSError, ValueError) as exc:
        raise OracleError("batch journal is invalid", exit_code=EXIT_INTEGRITY) from exc
    jobs = journal.get("jobs")
    count = int(plan["document_count"])
    if (
        journal.get("schema") != BATCH_RESULT_SCHEMA
        or journal.get("backend") != "real"
        or journal.get("baseline_eligible") is not False
        or journal.get("plan_digest") != plan["plan_digest"]
        or journal.get("document_count") != count
        or not isinstance(jobs, list)
        or len(jobs) > count
        or any(
            not isinstance(item, dict)
            or item.get("status") not in {"success", "failure", "blocked"}
            for item in jobs
        )
        or journal.get("status")
        not in {"running", "interrupted", "complete-with-failures", "success"}
        or journal.get("success_count")
        != sum(isinstance(item, dict) and item.get("status") == "success" for item in jobs)
        or journal.get("failure_count")
        != sum(
            isinstance(item, dict) and item.get("status") in {"failure", "blocked"}
            for item in jobs
        )
        or journal.get("pending_count") != count - len(jobs)
    ):
        raise OracleError("batch journal failed integrity checks", exit_code=EXIT_INTEGRITY)
    return journal


def _matching_evidence_job(
    home: Path,
    record: dict[str, object],
) -> dict[str, object] | None:
    jobs_root = home / "jobs"
    if jobs_root.is_symlink() or not jobs_root.is_dir():
        return None
    matches: list[tuple[int, Path, dict[str, Any]]] = []
    scanned = 0
    try:
        for child in jobs_root.iterdir():
            if not child.name.startswith("batch-document-"):
                continue
            scanned += 1
            if scanned > 10_000:
                raise OracleError(
                    "oracle evidence job count is outside its bound",
                    exit_code=EXIT_INTEGRITY,
                )
            if child.is_symlink() or not child.is_dir():
                continue
            inputs_path = child / "inputs.json"
            if inputs_path.is_symlink() or not inputs_path.is_file():
                continue
            try:
                inputs = read_json_object(inputs_path)
                source = inputs.get("source")
                modified = child.stat().st_mtime_ns
            except (OSError, ValueError):
                continue
            if (
                isinstance(source, dict)
                and source.get("guest_name") == record.get("guest")
                and source.get("sha256") == record.get("source_sha256")
            ):
                matches.append((modified, child, inputs))
    except OSError as exc:
        raise OracleError("cannot inspect oracle evidence jobs", exit_code=EXIT_INTEGRITY) from exc
    if not matches:
        return None
    _modified, job, _inputs = max(matches, key=lambda item: (item[0], item[1].name))
    completed = (job / "job.json").is_file() or (job / "failure.json").is_file()
    screen = job / "diagnostics" / "screen-last.png"
    screen_path: str | None = None
    screen_size: int | None = None
    screen_mtime_ns: int | None = None
    try:
        if not screen.is_symlink() and screen.is_file():
            info = screen.stat()
            screen_path = str(screen.absolute())
            screen_size = info.st_size
            screen_mtime_ns = info.st_mtime_ns
    except FileNotFoundError:
        # The observer atomically replaces this file while status is sampled.
        pass
    return {
        "evidence_job": job.name,
        "active": not completed,
        "screen_path": screen_path,
        "screen_size": screen_size,
        "screen_mtime_ns": screen_mtime_ns,
    }


def read_batch_status(output: Path, home: Path) -> dict[str, object]:
    output = output.expanduser().absolute()
    plan = _read_saved_plan(output)
    journal = _read_batch_journal(output, plan)
    completed = len(journal["jobs"]) if journal is not None else 0
    count = int(plan["document_count"])
    current: dict[str, object] | None = None
    evidence: dict[str, object] | None = None
    if completed < count:
        record = plan["records"][completed]
        current = {
            "index": record["index"],
            "guest": record["guest"],
            "preflight": record["preflight"],
        }
        evidence = _matching_evidence_job(home, record)
        if evidence is not None:
            current.update(evidence)
    successes = int(journal["success_count"]) if journal is not None else 0
    failures = int(journal["failure_count"]) if journal is not None else 0
    if evidence is not None and evidence["active"]:
        status = "running"
    elif journal is None:
        status = "starting"
    else:
        status = journal["status"]
    return {
        "schema": BATCH_STATUS_SCHEMA,
        "status": status,
        "document_count": count,
        "completed_count": completed,
        "success_count": successes,
        "failure_count": failures,
        "pending_count": count - completed,
        "current": current,
        "journal_exists": journal is not None,
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
    expected_pdf_path, pdf = _reference_pdf_path(
        output,
        record,
        create_parents=False,
    )
    pdf_path = pdf_record.get("path") if isinstance(pdf_record, dict) else None
    pure_pdf = PurePosixPath(pdf_path) if isinstance(pdf_path, str) else None
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
    progress: Callable[[dict[str, object]], None] | None = None,
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
    atomic_write_json(output / "batch.json", _batch_summary(plan, jobs, running=True))
    _publish_progress(output, plan, jobs, event="batch-started", callback=progress)
    try:
        for record in plan["records"]:
            previous = _validated_resume_result(output, record) if resume else None
            if previous is None and resume:
                previous = _validated_blocked_result(output, record)
            if previous is not None:
                jobs.append(previous)
                atomic_write_json(
                    output / "batch.json",
                    _batch_summary(plan, jobs, running=True),
                )
                _publish_progress(
                    output,
                    plan,
                    jobs,
                    event="document-reused",
                    record=record,
                    callback=progress,
                )
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
                atomic_write_json(
                    output / "batch.json",
                    _batch_summary(plan, jobs, running=True),
                )
                _publish_progress(
                    output,
                    plan,
                    jobs,
                    event="document-blocked",
                    record=record,
                    callback=progress,
                )
                continue
            source = input_root / str(record["source"])
            _publish_progress(
                output,
                plan,
                jobs,
                event="document-started",
                record=record,
                callback=progress,
            )
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
                pdf_relative, pdf_output = _reference_pdf_path(
                    output,
                    record,
                    create_parents=True,
                )
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
            atomic_write_json(
                output / "batch.json",
                _batch_summary(plan, jobs, running=True),
            )
            _publish_progress(
                output,
                plan,
                jobs,
                event=(
                    "document-success"
                    if jobs[-1].get("status") == "success"
                    else "document-failure"
                ),
                record=record,
                callback=progress,
            )
    except BaseException:
        atomic_write_json(output / "batch.json", _batch_summary(plan, jobs, interrupted=True))
        _publish_progress(
            output,
            plan,
            jobs,
            event="batch-interrupted",
            callback=progress,
        )
        raise
    summary = _batch_summary(plan, jobs)
    atomic_write_json(output / "batch.json", summary)
    _publish_progress(output, plan, jobs, event="batch-complete", callback=progress)
    return summary, EXIT_DIFFERENT if summary["failure_count"] else 0
