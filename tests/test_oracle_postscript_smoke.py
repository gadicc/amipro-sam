from __future__ import annotations

import json
from pathlib import Path

import pytest

from amipro_oracle import cli as oracle_cli
from amipro_oracle import postscript_smoke as print_module
from amipro_oracle.constants import ANALYSIS_SCHEMA, EXIT_INTEGRITY
from amipro_oracle.errors import OracleError
from amipro_oracle.io import digest_json


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


def _fixture() -> dict[str, object]:
    return {
        "schema": print_module.document_module.TEXT_FIXTURE_SCHEMA,
        "profile": "invented-version-4-cp1252-text-only-v1",
        "staged_name": "SMOKE.SAM",
        "size": 4584,
        "sha256": "d" * 64,
        "embedded_directory_offset": 4562,
    }


def _ready() -> dict[str, object]:
    return {
        "schema": print_module.printer_module.PRINTER_READY_SCHEMA,
        "status": "printer-ready",
        "runtime_key": "e" * 64,
        "inputs_digest": "f" * 64,
        "printer_profile": print_module.printer_module.PRINTER_PROFILE["name"],
        "printer_tree_digest": "1" * 64,
        "sealed_tree_digest": "2" * 64,
        "printer_identity": {"profile": "synthetic-printer"},
    }


def _postscript(*, interior_eot: bool = False) -> bytes:
    middle = b"\x04" if interior_eot else b""
    return (
        b"\x04%!PS-Adobe-3.0\r\n"
        b"%%Creator: Windows PSCRIPT\r\n"
        b"%%Title: Ami Pro - SMOKE.SAM\r\n"
        b"%%BoundingBox: 14 91 582 782\r\n"
        b"%%Pages: (atend)\r\n"
        b"%%Page: 1 1\r\n"
        + middle
        + b"showpage\r\n%%Trailer\r\n%%Pages: 1\r\n%%EOF\r\n\x04"
    )


def test_postscript_config_batch_and_inputs_are_pinned() -> None:
    config = print_module.postscript_smoke_config()
    batch = print_module.postscript_smoke_batch()
    inputs = print_module.postscript_smoke_inputs(_ready(), _fixture(), _image_record())

    assert 'MOUNT C "/oracle/job/runtime" -freesize 128' in config
    assert "parallel1=file timeout:2000" in config
    assert r"Z:\CONFIG.COM -SECUREMODE" in config
    assert r"C:\PRTSMK.BAT" in config
    assert batch.endswith(b"\r\n")
    assert b"AMIPRO.EXE C:\\ORACLE\\SMOKE.SAM" in batch
    assert inputs["analysis_profile"] == print_module.ANALYSIS_PROFILE

    changed = print_module.postscript_smoke_inputs(
        _ready(),
        _fixture(),
        _image_record(),
        outer_time_limit_seconds=60,
    )
    assert digest_json(inputs) != digest_json(changed)
    with pytest.raises(OracleError):
        print_module.postscript_smoke_inputs(
            _ready(),
            _fixture(),
            _image_record(),
            outer_time_limit_seconds=131,
        )


def test_postscript_validation_preserves_raw_and_records_boundary_eot() -> None:
    raw = _postscript()
    sanitized, identity = print_module.validate_postscript(raw)

    assert sanitized == raw[1:-1]
    assert identity["leading_eot_removed"] is True
    assert identity["trailing_eot_removed"] is True
    assert identity["pages"] == 1
    assert identity["bounding_box"] == [14, 91, 582, 782]

    with pytest.raises(OracleError, match="interior EOT") as caught:
        print_module.validate_postscript(_postscript(interior_eot=True))
    assert caught.value.exit_code == EXIT_INTEGRITY
    with pytest.raises(OracleError, match="bounding box"):
        print_module.validate_postscript(raw.replace(b"14 91 582 782", b"0 0 10 10"))


def test_analysis_parsers_require_exact_pdf_identity_and_boxes(tmp_path: Path) -> None:
    pdfinfo = b"\n".join(
        (
            b"Title: Ami Pro - SMOKE.SAM",
            b"Creator: Windows PSCRIPT",
            b"Producer: GPL Ghostscript 10.00.0",
            b"Pages: 1",
            b"Encrypted: no",
            b"JavaScript: no",
            b"Page size: 595 x 842 pts (A4)",
            b"Page rot: 0",
            b"PDF version: 1.4",
            b"",
        )
    )
    assert print_module._parse_pdfinfo(pdfinfo)["Pages"] == "1"
    fonts = (
        b"name type encoding emb sub uni object ID\n"
        b"------------------------------------------\n"
        b"[none] Type 3 Custom yes no no 22 0\n"
    )
    assert print_module._parse_pdffonts(fonts)[0]["type"] == "Type 3"
    words = "".join(
        f'<word xMin="{10 + index}.0" yMin="20.0" '
        f'xMax="{10.5 + index}" yMax="21.0">{word}</word>'
        for index, word in enumerate(print_module.EXPECTED_WORDS)
    )
    bbox = tmp_path / "bbox.html"
    bbox.write_text(
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>'
        f'<page width="595.0" height="842.0"><flow>{words}</flow></page>'
        "</doc></body></html>",
        encoding="utf-8",
    )
    boxes, width, height = print_module._parse_bbox(bbox)
    assert (width, height) == (595.0, 842.0)
    assert [box["text"] for box in boxes] == list(print_module.EXPECTED_WORDS)


def test_print_smoke_writes_a_verifiable_real_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "oracle"
    parent = home / "cache" / "printer-ready" / ("e" * 64)
    parent_runtime = parent / "pristine-c"
    parent_runtime.mkdir(parents=True)
    (parent_runtime / "base.dat").write_bytes(b"synthetic runtime")
    source = tmp_path / "synthetic.sam"
    prefix = b"[ver]\r\n\t4\r\n[edoc]\r\n<invented>\r\n\r\n"
    source.write_bytes(
        prefix + b"[Embedded]\r\n" + f"{len(prefix):08d}".encode("ascii") + b"\r\n"
    )
    fixture_payload, fixture = print_module.document_module.read_text_fixture(source)
    ready = _ready()
    ready["printer_tree_digest"] = "synthetic-tree"
    printer_identity = ready["printer_identity"]

    def select(
        _home: Path,
        runtime_key: str | None,
    ) -> tuple[Path, dict[str, object], dict[str, object], str]:
        assert runtime_key in {None, "e" * 64}
        return parent, ready, {"schema": "printer-inputs"}, "printer-evidence"

    def validate_runtime(_runtime: Path) -> tuple[dict[str, object], dict[str, object]]:
        return {"digest": "synthetic-tree"}, printer_identity

    driver = {
        "schema": print_module.POSTSCRIPT_SMOKE_UI_SCHEMA,
        "status": "success",
    }

    def invoke(
        _invocation: object,
        job: Path,
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, object], dict[str, object]]:
        assert timeout_seconds == print_module.OUTER_TIME_LIMIT_SECONDS
        runtime = job / "runtime"
        (runtime / "PRTSMK.STA").write_bytes(b"POSTSCRIPT_LAUNCH_REQUESTED\r\n")
        (runtime / "PRTSMK.OK").write_bytes(b"POSTSCRIPT_RETURNED_ZERO\r\n")
        (job / "capture" / "krnl386_000.prt").write_bytes(_postscript())
        (job / "diagnostics" / "container.stdout.log").write_bytes(b"")
        (job / "diagnostics" / "container.stderr.log").write_bytes(
            b"Parallel 1: File closed.\n"
        )
        print_module.atomic_write_json(job / "ui-driver.json", driver)
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
    ) -> tuple[dict[str, object], dict[str, object]]:
        output = job / "output"
        for name, content in (
            ("document.pdf", b"%PDF synthetic"),
            ("pdfinfo.txt", b"synthetic pdfinfo"),
            ("pdffonts.txt", b"synthetic fonts"),
            ("text.txt", b"invented text"),
            ("bbox.html", b"<html/>"),
            ("page-001.png", b"synthetic png"),
        ):
            (output / name).write_bytes(content)
        analysis: dict[str, object] = {
            "schema": ANALYSIS_SCHEMA,
            "backend": "real",
            "profile": print_module.ANALYSIS_PROFILE,
            "page_count": 1,
            "pages": [],
        }
        print_module.atomic_write_json(output / "analysis.json", analysis)
        return analysis, {"synthetic": {"exit_code": 0}}

    monkeypatch.setattr(print_module, "_invoke_guest", invoke)
    monkeypatch.setattr(print_module, "_require_verified_image", lambda _record: None)
    monkeypatch.setattr(print_module, "_select_printer_runtime", select)
    monkeypatch.setattr(print_module.printer_module, "_validate_printer_runtime", validate_runtime)
    monkeypatch.setattr(print_module, "_validate_observer_evidence", lambda _path: {"status": "ok"})
    monkeypatch.setattr(print_module, "_validate_ui_evidence", lambda _job: driver)
    monkeypatch.setattr(print_module, "_derive_outputs", derive)
    monkeypatch.setattr(print_module, "_artifact_inventory", lambda _job: [])

    result = print_module.print_smoke(home, _image_record(), source)
    assert result["status"] == "success"
    assert result["backend"] == "real"
    assert result["baseline_eligible"] is False
    assert result["source"]["sha256"] == fixture["sha256"]
    assert result["postscript"]["pages"] == 1
    assert (home / "jobs" / result["evidence_job"] / "job.json").is_file()
    assert fixture_payload == source.read_bytes()


def test_print_smoke_cli_requires_rights_and_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "oracle"
    monkeypatch.setattr(oracle_cli, "oracle_home", lambda *_args, **_kwargs: home)

    assert oracle_cli.main(["print-smoke", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2

    monkeypatch.setattr(oracle_cli, "_toolchain_image", lambda _home: _image_record())
    monkeypatch.setattr(
        oracle_cli,
        "print_smoke",
        lambda *_args, **_kwargs: {
            "status": "success",
            "evidence_job": "print-smoke-test",
        },
    )
    assert (
        oracle_cli.main(
            ["print-smoke", "--confirm-proprietary-media-rights", "--json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "success"
