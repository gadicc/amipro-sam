from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from amipro_oracle import batch as batch_module
from amipro_oracle import cli as cli_module
from amipro_oracle import native_batch as native_module
from amipro_oracle.constants import EXIT_DIFFERENT, EXIT_INTEGRITY
from amipro_oracle.errors import OracleError
from amipro_oracle.io import atomic_write_json, read_json_object


def _image_record() -> dict[str, object]:
    return {
        "schema": "amipro-oracle-image-v1",
        "provider": "podman",
        "platform": "linux/amd64",
        "image": "localhost/amipro-oracle-toolchain:test",
        "image_id": "a" * 64,
        "image_digest": f"sha256:{'b' * 64}",
        "lock_sha256": "c" * 64,
    }


def _ready() -> dict[str, object]:
    return {
        "schema": native_module.smoke_module.printer_module.PRINTER_READY_SCHEMA,
        "status": "printer-ready",
        "runtime_key": "e" * 64,
        "inputs_digest": "f" * 64,
        "printer_profile": native_module.smoke_module.printer_module.PRINTER_PROFILE["name"],
        "printer_tree_digest": "synthetic-tree",
        "sealed_tree_digest": "2" * 64,
        "printer_identity": {"profile": "synthetic-printer"},
    }


def _sam(*extra: bytes) -> bytes:
    return b"[ver]\r\n\t4\r\n[edoc]\r\nInvented batch content\r\n" + b"".join(extra)


def _write_source(root: Path, name: str, payload: bytes | None = None) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_sam() if payload is None else payload)
    return path


def test_native_preflight_is_fail_closed_for_active_and_external_content() -> None:
    safe = batch_module.audit_native_sam(_sam(), guest_name="DOC00001.SAM")
    assert safe["status"] == "safe-to-open-in-isolated-native-oracle"
    assert safe["policies"]["source_directory_guest_mount"] is False

    blocked = (
        _sam(b"[macro]\r\nRunMe\r\n"),
        _sam(b"[files]\r\nC:\\PRIVATE\\OTHER.SAM\r\n"),
        _sam(b"[sty]\r\nC:\\PRIVATE\\STYLE.STY\r\n"),
        _sam(b"[port]\r\nLPT1: C:\\PRIVATE\\OUT.PS\r\n"),
        _sam(b'<:X"external expression">\r\n'),
        _sam(b"[Embedded]\r\nOLE1 .OLE 1 2 3 4\r\n"),
    )
    for payload in blocked:
        with pytest.raises(OracleError, match="preflight") as caught:
            batch_module.audit_native_sam(payload, guest_name="DOC00001.SAM")
        assert caught.value.exit_code == EXIT_INTEGRITY


def test_batch_plan_is_deterministic_and_hashes_blocked_sources(tmp_path: Path) -> None:
    root = tmp_path / "input"
    safe = _write_source(root, "a.sam")
    blocked_payload = _sam(b"[newmac]\r\nNeverRun\r\n")
    blocked = _write_source(root, "nested/b.sam", blocked_payload)

    first = batch_module.build_batch_plan([safe, blocked], root)
    second = batch_module.build_batch_plan([safe, blocked], root)

    assert first == second
    assert [item["guest"] for item in first["records"]] == [
        "DOC00001.SAM",
        "DOC00002.SAM",
    ]
    assert first["records"][1]["preflight"] == "blocked"
    assert first["records"][1]["source_sha256"] == hashlib.sha256(
        blocked_payload
    ).hexdigest()
    assert batch_module._name_map(first)["records"] == [
        {
            "index": 1,
            "source": "a.sam",
            "guest": "DOC00001.SAM",
            "pdf": "reference-pdf/a.pdf",
        },
        {
            "index": 2,
            "source": "nested/b.sam",
            "guest": "DOC00002.SAM",
            "pdf": "reference-pdf/nested/b.pdf",
        },
    ]


def test_real_batch_continues_then_resumes_without_repeating_success(
    tmp_path: Path,
) -> None:
    root = tmp_path / "input"
    sources = [_write_source(root, "a.sam"), _write_source(root, "b.sam")]
    output = tmp_path / "private-output"
    calls: list[str] = []
    progress: list[dict[str, object]] = []

    def first_worker(source: Path, guest: str, timeout: float) -> dict[str, object]:
        assert timeout == 45
        calls.append(guest)
        if guest == "DOC00002.SAM":
            raise OracleError("synthetic native failure", exit_code=5)
        pdf = tmp_path / f"{guest}.pdf"
        pdf.write_bytes(b"%PDF invented one")
        return {
            "evidence_job": f"batch-document-{guest[:-4].casefold()}",
            "job_manifest_sha256": "a" * 64,
            "pdf_path": str(pdf),
            "page_count": 1,
        }

    summary, exit_code = batch_module.run_real_batch(
        sources=sources,
        input_root=root,
        output=output,
        worker=first_worker,
        timeout_seconds=45,
        progress=progress.append,
    )
    assert exit_code == EXIT_DIFFERENT
    assert summary["success_count"] == 1
    assert summary["failure_count"] == 1
    assert calls == ["DOC00001.SAM", "DOC00002.SAM"]
    assert [item["event"] for item in progress] == [
        "batch-started",
        "document-started",
        "document-success",
        "document-started",
        "document-failure",
        "batch-complete",
    ]
    assert progress[-1]["completed_count"] == 2
    assert read_json_object(output / "progress.json") == progress[-1]
    first_failure = output / "jobs/00002/attempts/0001/failure.json"
    assert first_failure.is_file()
    assert (output / "reference-pdf/a.pdf").is_file()

    calls.clear()

    def resumed_worker(source: Path, guest: str, timeout: float) -> dict[str, object]:
        calls.append(guest)
        pdf = tmp_path / f"resumed-{guest}.pdf"
        pdf.write_bytes(b"%PDF invented two")
        return {
            "evidence_job": f"batch-document-resumed-{guest[:-4].casefold()}",
            "job_manifest_sha256": "b" * 64,
            "pdf_path": str(pdf),
            "page_count": 2,
        }

    resumed, resumed_exit = batch_module.run_real_batch(
        sources=sources,
        input_root=root,
        output=output,
        worker=resumed_worker,
        timeout_seconds=45,
        resume=True,
    )
    assert resumed_exit == 0
    assert resumed["success_count"] == 2
    assert calls == ["DOC00002.SAM"]
    assert first_failure.is_file()
    assert (output / "jobs/00002/attempts/0002/result.json").is_file()
    assert (output / "reference-pdf/b.pdf").is_file()
    assert not (output / "jobs/00002/failure.json").exists()


def test_batch_status_finds_the_active_read_only_observer_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "private-input"
    source = _write_source(root, "private-name.sam")
    plan = batch_module.build_batch_plan([source], root)
    output = tmp_path / "private-output"
    output.mkdir(mode=0o700)
    output.chmod(0o700)
    for name in ("jobs", "reference-pdf"):
        (output / name).mkdir(mode=0o700)
        (output / name).chmod(0o700)
    atomic_write_json(output / "plan.json", plan)
    atomic_write_json(output / "name-map.json", batch_module._name_map(plan))

    home = tmp_path / "oracle"
    evidence = home / "jobs" / "batch-document-active_1"
    diagnostics = evidence / "diagnostics"
    diagnostics.mkdir(parents=True)
    audit = plan["records"][0]["audit"]
    atomic_write_json(evidence / "inputs.json", {"source": audit})
    screen = diagnostics / "screen-last.png"
    screen.write_bytes(b"invented observer frame")

    status = batch_module.read_batch_status(output, home)
    assert status["status"] == "running"
    assert status["completed_count"] == 0
    assert status["current"] == {
        "index": 1,
        "guest": "DOC00001.SAM",
        "preflight": "ready",
        "evidence_job": evidence.name,
        "active": True,
        "screen_path": str(screen.absolute()),
        "screen_size": len(b"invented observer frame"),
        "screen_mtime_ns": screen.stat().st_mtime_ns,
    }
    assert "private-name" not in json.dumps(status)

    monkeypatch.setattr(cli_module, "oracle_home", lambda *_args, **_kwargs: home)
    assert (
        cli_module.main(
            ["batch-status", "--output", str(output), "--screen-path"]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == str(screen.absolute())


def test_resume_rejects_tampered_pdf_path_and_records_interrupt(tmp_path: Path) -> None:
    root = tmp_path / "input"
    source = _write_source(root, "a.sam")
    output = tmp_path / "private-output"

    def worker(_source: Path, _guest: str, _timeout: float) -> dict[str, object]:
        pdf = tmp_path / "source.pdf"
        pdf.write_bytes(b"%PDF local")
        return {
            "evidence_job": "batch-document-evidence",
            "job_manifest_sha256": "c" * 64,
            "pdf_path": str(pdf),
            "page_count": 1,
        }

    batch_module.run_real_batch(
        sources=[source],
        input_root=root,
        output=output,
        worker=worker,
    )
    result_path = output / "jobs/00001/result.json"
    result = read_json_object(result_path)
    result["pdf"]["path"] = "/tmp/escape.pdf"
    atomic_write_json(result_path, result)
    with pytest.raises(OracleError, match="integrity"):
        batch_module.run_real_batch(
            sources=[source],
            input_root=root,
            output=output,
            worker=worker,
            resume=True,
        )

    interrupted_output = tmp_path / "interrupted"

    def interrupt(_source: Path, _guest: str, _timeout: float) -> dict[str, object]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        batch_module.run_real_batch(
            sources=[source],
            input_root=root,
            output=interrupted_output,
            worker=interrupt,
        )
    assert read_json_object(interrupted_output / "batch.json")["status"] == "interrupted"


def test_reference_pdf_paths_preserve_names_without_following_parent_links(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    reference = output / "reference-pdf"
    reference.mkdir(parents=True, mode=0o700)
    record = {"source": "Letters/Original Name.SAM"}

    relative, path = batch_module._reference_pdf_path(
        output,
        record,
        create_parents=True,
    )
    assert relative == "reference-pdf/Letters/Original Name.pdf"
    assert path == reference / "Letters" / "Original Name.pdf"
    assert (reference / "Letters").stat().st_mode & 0o077 == 0

    (reference / "Letters").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (reference / "Letters").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OracleError, match="parent is unsafe"):
        batch_module._reference_pdf_path(
            output,
            record,
            create_parents=True,
        )
    assert not (outside / "Original Name.pdf").exists()


def _postscript(guest: str, pages: int) -> bytes:
    page_lines = b"".join(
        f"%%Page: {number} {number}\r\nshowpage\r\n".encode("ascii")
        for number in range(1, pages + 1)
    )
    return (
        b"\x04%!PS-Adobe-3.0\r\n"
        b"%%Creator: Windows PSCRIPT\r\n"
        + f"%%Title: Ami Pro - {guest}\r\n".encode("ascii")
        + b"%%BoundingBox: 14 20 582 782\r\n"
        + b"%%Pages: (atend)\r\n"
        + page_lines
        + b"%%Trailer\r\n"
        + f"%%Pages: {pages}\r\n".encode("ascii")
        + b"%%EOF\r\n\x04"
    )


def test_native_batch_config_and_variable_postscript_validation() -> None:
    config = native_module.native_document_config()
    batch = native_module.native_document_batch("DOC00042.SAM")
    assert r"C:\PRTSMK.BAT" in config
    assert b"C:\\ORACLE\\DOC00042.SAM" in batch

    raw = _postscript("DOC00042.SAM", 2)
    sanitized, identity = native_module.validate_native_postscript(
        raw,
        guest_name="DOC00042.SAM",
    )
    assert sanitized == raw[1:-1]
    assert identity["pages"] == 2
    assert identity["bounding_box"] == [14, 20, 582, 782]

    with pytest.raises(OracleError, match="page inventory"):
        native_module.validate_native_postscript(
            raw.replace(b"%%Page: 2 2", b"%%Page: 2 3"),
            guest_name="DOC00042.SAM",
        )


def test_native_document_worker_retains_a_verifiable_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "oracle"
    parent = home / "cache" / "printer-ready" / ("e" * 64)
    runtime = parent / "pristine-c"
    runtime.mkdir(parents=True)
    (runtime / "base.dat").write_bytes(b"synthetic runtime")
    source = _write_source(tmp_path, "source.sam")
    ready = _ready()

    def select(
        _home: Path,
        runtime_key: str | None,
    ) -> tuple[Path, dict[str, object], dict[str, object], str]:
        assert runtime_key in {None, "e" * 64}
        return parent, ready, {"schema": "printer-inputs"}, "printer-evidence"

    def validate_runtime(_runtime: Path) -> tuple[dict[str, object], dict[str, object]]:
        return {"digest": "synthetic-tree"}, ready["printer_identity"]

    driver = {
        "schema": native_module.NATIVE_DOCUMENT_UI_SCHEMA,
        "status": "success",
        "profile": native_module.NATIVE_DOCUMENT_PROFILE,
        "states": [],
        "actions": [],
    }

    def invoke(
        _invocation: object,
        job: Path,
        *,
        timeout_seconds: float,
        lifecycle: object,
    ) -> tuple[dict[str, object], dict[str, object]]:
        assert timeout_seconds == 45
        assert callable(lifecycle)
        guest_runtime = job / "runtime"
        (guest_runtime / "PRTSMK.STA").write_bytes(b"POSTSCRIPT_LAUNCH_REQUESTED\r\n")
        (guest_runtime / "PRTSMK.OK").write_bytes(b"POSTSCRIPT_RETURNED_ZERO\r\n")
        (job / "capture" / "krnl386_000.prt").write_bytes(
            _postscript("DOC00001.SAM", 1)
        )
        (job / "diagnostics" / "container.stdout.log").write_bytes(b"")
        (job / "diagnostics" / "container.stderr.log").write_bytes(
            b"Parallel 1: File closed.\n"
        )
        atomic_write_json(job / "ui-driver.json", driver)
        return {
            "exit_code": 0,
            "timed_out": False,
            "killed": False,
            "duration_seconds": 1.0,
        }, driver

    def derive(
        _home: Path,
        _record: dict[str, object],
        job: Path,
        *,
        guest_name: str,
        postscript: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        assert guest_name == "DOC00001.SAM"
        assert postscript["pages"] == 1
        (job / "output" / "document.pdf").write_bytes(b"%PDF synthetic")
        analysis = {
            "schema": "amipro-oracle-analysis-v1",
            "backend": "real",
            "profile": native_module.ANALYSIS_PROFILE,
            "page_count": 1,
            "pages": [],
        }
        atomic_write_json(job / "output" / "analysis.json", analysis)
        return analysis, {"synthetic": {"exit_code": 0}}

    monkeypatch.setattr(native_module, "_require_verified_image", lambda _record: None)
    monkeypatch.setattr(native_module.smoke_module, "_select_printer_runtime", select)
    monkeypatch.setattr(
        native_module.smoke_module.printer_module,
        "_validate_printer_runtime",
        validate_runtime,
    )
    monkeypatch.setattr(native_module.smoke_module, "_invoke_guest", invoke)
    monkeypatch.setattr(
        native_module,
        "_validate_observer_evidence",
        lambda _path: {"status": "ok"},
    )
    monkeypatch.setattr(native_module, "_validate_ui", lambda _job, _driver: None)
    monkeypatch.setattr(native_module, "_derive_outputs", derive)
    monkeypatch.setattr(native_module, "_artifacts", lambda _job: [])

    result = native_module.print_native_document(
        home,
        _image_record(),
        source,
        "DOC00001.SAM",
        45,
    )
    assert result["page_count"] == 1
    assert result["evidence_job"].startswith("batch-document-")
    job = home / "jobs" / result["evidence_job"]
    assert (job / "job.json").is_file()
    assert not (job / "runtime").exists()
    assert result["job_manifest_sha256"] == hashlib.sha256(
        (job / "job.json").read_bytes()
    ).hexdigest()


def test_real_batch_cli_requires_rights_and_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = tmp_path / "input"
    _write_source(source_root, "a.sam")
    output = tmp_path / "output"
    home = tmp_path / "oracle"
    monkeypatch.setattr(cli_module, "oracle_home", lambda *_args, **_kwargs: home)
    monkeypatch.setattr(cli_module, "_toolchain_image", lambda _home: {"image": True})
    monkeypatch.setattr(
        cli_module,
        "validate_native_batch_prerequisites",
        lambda _home, _image, runtime_key: runtime_key or "e" * 64,
    )
    observed: dict[str, object] = {}

    def run(**kwargs: object) -> tuple[dict[str, object], int]:
        observed.update(kwargs)
        callback = kwargs.get("progress")
        if callable(callback):
            callback(
                {
                    "event": "document-success",
                    "document_count": 1,
                    "completed_count": 1,
                    "success_count": 1,
                    "failure_count": 0,
                    "document": {"index": 1, "guest": "DOC00001.SAM"},
                }
            )
        return {
            "schema": batch_module.BATCH_RESULT_SCHEMA,
            "status": "success",
            "success_count": 1,
            "failure_count": 0,
        }, 0

    monkeypatch.setattr(cli_module, "run_real_batch", run)
    assert (
        cli_module.main(
            [
                "batch",
                "--input",
                str(source_root),
                "--output",
                str(output),
                "--json",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2

    assert (
        cli_module.main(
            [
                "batch",
                "--input",
                str(source_root),
                "--output",
                str(output),
                "--runtime-key",
                "d" * 64,
                "--timeout-seconds",
                "45",
                "--progress",
                "--confirm-proprietary-media-rights",
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["success_count"] == 1
    assert "DOC00001.SAM: document success" in captured.err
    assert observed["timeout_seconds"] == 45
    assert observed["resume"] is False
    assert callable(observed["progress"])
