from __future__ import annotations

from pathlib import Path

import pytest

from amipro_oracle import batch as batch_module
from amipro_oracle.constants import EXIT_DIFFERENT
from amipro_oracle.errors import OracleError
from amipro_oracle.font_audit import (
    classify_document_fonts,
    font_environment_from_runtime,
)


def _runtime(root: Path) -> Path:
    runtime = root / "runtime"
    system = runtime / "WINDOWS" / "SYSTEM"
    amipro = runtime / "AMIPRO"
    system.mkdir(parents=True)
    amipro.mkdir()
    (system / "ARIAL.FOT").write_bytes(b"invented registration wrapper")
    (system / "ARIAL.TTF").write_bytes(b"invented font placeholder")
    (system / "ARIALBD.FOT").write_bytes(b"invented bold registration wrapper")
    (system / "ARIALBD.TTF").write_bytes(b"invented bold font placeholder")
    (system / "COURE.FON").write_bytes(b"invented bitmap font placeholder")
    (runtime / "WINDOWS" / "WIN.INI").write_text(
        "[fonts]\r\n"
        "Arial (TrueType)=ARIAL.FOT\r\n"
        "Arial Bold (TrueType)=ARIALBD.FOT\r\n"
        "Courier 10,12,15 (VGA res)=COURE.FON\r\n"
        "[FontSubstitutes]\r\n"
        "Helvetica=Arial\r\n",
        encoding="latin-1",
    )
    return runtime


def _environment(tmp_path: Path) -> dict[str, object]:
    return font_environment_from_runtime(
        _runtime(tmp_path),
        runtime_key="b" * 64,
        sealed_tree_digest="c" * 64,
        printer_profile="invented-printer-profile",
        printer_model="Invented PostScript Printer",
        printer_identity_digest="a" * 64,
        printer_device_families=("AvantGarde",),
    )


def _sam() -> bytes:
    return (
        b"[ver]\r\n\t4\r\n"
        b"[tag]\r\n\tBody Text\r\n\t2\r\n"
        b"\t[fnt]\r\n\t\tArial\r\n\t\t240\r\n\t\t0\r\n\t\t0\r\n"
        b"[edoc]\r\n"
        b"<:f240,Helvetica,>Alias "
        b"<:f240,AvantGarde,>Device "
        b"<:f240,1Courier,>Installed "
        b"<:f240,Missing Face,>Missing\r\n>\r\n"
    )


def test_runtime_font_inventory_and_document_classification(tmp_path: Path) -> None:
    environment = _environment(tmp_path)

    assert environment["registered_face_count"] == 3
    assert environment["font_binary_count"] == 3
    assert environment["registration_wrapper_count"] == 2
    assert environment["installed_families"] == ["Arial", "Arial Bold", "Courier"]
    assert environment["explicit_substitutes"] == [
        {"source": "Helvetica", "target": "Arial"}
    ]

    resolution = classify_document_fonts(_sam(), environment)
    assert resolution["fidelity"] == "degraded-or-unknown"
    assert resolution["status_counts"] == {
        "explicit-alias": 1,
        "installed": 2,
        "native-substitution-unresolved": 1,
        "printer-device": 1,
    }
    assert resolution["unresolved_families"] == ["Missing Face"]
    assert resolution["strict_blocker_count"] == 1
    assert resolution["scan"]["truncated"] is False


def test_strict_font_policy_blocks_only_unresolved_documents(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    input_root = tmp_path / "input"
    input_root.mkdir()
    unresolved = input_root / "unresolved.sam"
    unresolved.write_bytes(_sam())
    resolved = input_root / "resolved.sam"
    resolved.write_bytes(_sam().replace(b"Missing Face", b"Arial"))

    permissive = batch_module.build_batch_plan(
        [unresolved],
        input_root,
        font_environment=environment,
    )
    assert permissive["records"][0]["preflight"] == "ready"
    assert permissive["records"][0]["font_resolution"]["fidelity"] == (
        "degraded-or-unknown"
    )

    strict = batch_module.build_batch_plan(
        [unresolved, resolved],
        input_root,
        font_environment=environment,
        require_installed_fonts=True,
    )
    assert strict["font_policy"]["require_installed_fonts"] is True
    assert strict["records"][0]["preflight"] == "blocked"
    assert strict["records"][0]["font_resolution"]["strict_blocker_count"] == 1
    assert strict["records"][1]["preflight"] == "ready"

    output = tmp_path / "strict-output"

    def must_not_run(_source: Path, _guest: str, _timeout: float) -> dict[str, object]:
        raise AssertionError("strict preflight executed a blocked native document")

    summary, exit_code = batch_module.run_real_batch(
        sources=[unresolved],
        input_root=input_root,
        output=output,
        worker=must_not_run,
        font_environment=environment,
        require_installed_fonts=True,
    )
    assert exit_code == EXIT_DIFFERENT
    assert summary["failure_count"] == 1
    assert summary["font_warning_count"] == 1
    assert summary["jobs"][0]["status"] == "blocked"

    resumed, resumed_exit = batch_module.run_real_batch(
        sources=[unresolved],
        input_root=input_root,
        output=output,
        worker=must_not_run,
        font_environment=environment,
        require_installed_fonts=True,
        resume=True,
    )
    assert resumed_exit == EXIT_DIFFERENT
    assert resumed == summary


def test_font_scan_is_bounded_and_strict_mode_treats_truncation_as_a_blocker(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    payload = (
        b"[ver]\r\n\t4\r\n[edoc]\r\n"
        + b"<:f240,Arial,>x" * 4_097
        + b"\r\n>\r\n"
    )

    resolution = classify_document_fonts(payload, environment)

    assert resolution["scan"]["truncated"] is True
    assert resolution["requested_occurrence_count"] == 4_096
    assert resolution["strict_blocker_count"] == 1

    literal_and_malformed = classify_document_fonts(
        b"[ver]\r\n\t4\r\n[edoc]\r\n"
        b"<<:f240,Literal Font,> "
        + b"<:f240," + b"x" * 1_025 + b",>\r\n>\r\n",
        environment,
    )
    assert literal_and_malformed["requested_family_count"] == 0
    assert literal_and_malformed["scan"]["malformed_count"] == 1
    assert literal_and_malformed["strict_blocker_count"] == 1

    tampered = dict(environment)
    tampered["installed_families"] = ["Missing Face"]
    with pytest.raises(OracleError, match="invalid font environment"):
        classify_document_fonts(_sam(), tampered)
