from __future__ import annotations

import json
from pathlib import Path

import pytest

from amipro_oracle import amipro_install as install_module
from amipro_oracle import cli as oracle_cli
from amipro_oracle import printer_install as printer_module
from amipro_oracle.constants import EXIT_INTEGRITY, EXPECTED_AMIPRO_EXE_SHA256
from amipro_oracle.errors import OracleError
from amipro_oracle.io import digest_json, sha256_file
from amipro_oracle.raster import encode_rgb_png


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


def _windows_media() -> dict[str, object]:
    return {
        "kind": "windows-3.1",
        "media_profile": printer_module.bootstrap_module.WINDOWS_MEDIA_PROFILE,
        "file_count": 6,
        "digest": "d" * 64,
    }


def _flat_media() -> dict[str, object]:
    return {
        "schema": printer_module.bootstrap_module.FLAT_MEDIA_SCHEMA,
        "status": "ready",
        "cache_key": "e" * 64,
        "extraction_digest": "f" * 64,
        "tree_digest": "1" * 64,
    }


def _installed_runtime(runtime: Path) -> None:
    system = runtime / "WINDOWS" / "SYSTEM"
    system.mkdir(parents=True, exist_ok=True)
    (runtime / "SETUP.OK").write_bytes(b"SETUP_RETURNED_ZERO\r\n")
    (runtime / "ORADATE.TXT").write_bytes(b"03/10/1992\r\n")
    (runtime / "ORATIME.TXT").write_bytes(b"3:10:00\r\n")
    (runtime / "WINDOWS" / "WIN.COM").write_bytes(b"win")
    (runtime / "WINDOWS" / "PROGMAN.EXE").write_bytes(b"progman")
    (runtime / "WINDOWS" / "SYSTEM.INI").write_text(
        "[boot]\r\ndisplay.drv=VGA.DRV\r\n"
        "mouse.drv=MOUSE.DRV\r\nshell=PROGMAN.EXE\r\n",
        encoding="latin-1",
    )
    for name in ("KRNL386.EXE", "GDI.EXE", "USER.EXE", "VGA.DRV", "MOUSE.DRV"):
        (system / name).write_bytes(name.encode("ascii"))
    for relative in (
        "AMIPRO/DOCS",
        "AMIPRO/DRAWSYM",
        "AMIPRO/ICONS",
        "AMIPRO/MACROS",
        "AMIPRO/STYLES",
        "WINDOWS/LOTUSAPP",
    ):
        (runtime / relative).mkdir(parents=True, exist_ok=True)
    (runtime / "AMIPRO" / "AMIPRO.EXE").write_bytes(b"synthetic-not-amipro")
    (runtime / "WINDOWS" / "AMIPRO.INI").write_text(
        "macrodir=c:\\amipro\\macros\r\n"
        "stypath=c:\\amipro\\styles\r\n"
        "docpath=c:\\amipro\\docs\r\n"
        "automacroload=1,_autorun.smm!zrunmacs\r\n",
        encoding="latin-1",
    )
    (runtime / "WINDOWS" / "LOTUS.INI").write_text(
        "AMIPRO=c:\\amipro\\amipro.exe\r\n"
        "Common Directory=c:\\windows\\lotusapp\r\n"
        "Program Path=c:\\windows\\lotusapp\\spell\r\n",
        encoding="latin-1",
    )
    (runtime / "WINDOWS" / "WIN.INI").write_text(
        "[windows]\r\n"
        "[Extensions]\r\nsam=c:\\amipro\\amipro.exe ^.sam\r\n",
        encoding="latin-1",
    )


def _install_printer_files(runtime: Path) -> None:
    system = runtime / "WINDOWS" / "SYSTEM"
    for name, size in (
        ("PSCRIPT.DRV", 312_848),
        ("PSCRIPT.HLP", 43_793),
        ("TESTPS.TXT", 2_640),
    ):
        (system / name).write_bytes(b"x" * size)
    (runtime / "WINDOWS" / "WIN.INI").write_text(
        "[windows]\r\n"
        "spooler=no\r\n"
        "device=QMS ColorScript 100,pscript,LPT1:\r\n"
        "[Extensions]\r\nsam=c:\\amipro\\amipro.exe ^.sam\r\n"
        "[PostScript,LPT1]\r\nATM=placeholder\r\n"
        "[PrinterPorts]\r\n"
        "QMS ColorScript 100=pscript,LPT1:,15,90\r\n"
        "[devices]\r\nQMS ColorScript 100=pscript,LPT1:\r\n",
        encoding="latin-1",
    )
    (runtime / "WINDOWS" / "CONTROL.INI").write_text(
        "[installed]\r\n"
        "PSCRIPT.DRV=yes\r\nPSCRIPT.HLP=yes\r\nTESTPS.TXT=yes\r\n",
        encoding="latin-1",
    )


def _screen(variant: int) -> bytes:
    width = install_module.SCREEN_WIDTH
    height = install_module.SCREEN_HEIGHT
    left = bytes((variant, variant + 1, variant + 2))
    right = bytes((variant + 20, variant + 21, variant + 22))
    row = left * (width // 2) + right * (width // 2)
    return encode_rgb_png(width, height, row * height)


def _synthetic_profile(
    tmp_path: Path,
) -> tuple[tuple[dict[str, object], ...], dict[str, object], list[bytes]]:
    states: list[dict[str, object]] = []
    payloads: list[bytes] = []
    for index, name in enumerate(
        (
            "empty",
            "direct",
            "selected",
            "source",
            "installed",
            "program-manager",
            "exit-windows",
        ),
        start=1,
    ):
        payload = _screen(index)
        path = tmp_path / f"state-{index}.png"
        path.write_bytes(payload)
        provisional: dict[str, object] = {
            "name": name,
            "box": [0, 0, 2, 1],
            "title_sha256": "0" * 64,
        }
        observed, _ = install_module._screen_state(path, provisional)
        states.append({**provisional, "title_sha256": observed["title_sha256"]})
        payloads.append(payload)
    state_tuple = tuple(states)
    profile = {
        **printer_module.PRINTER_PROFILE,
        "name": "synthetic-printer-profile-v1",
        "states": list(state_tuple),
    }
    return state_tuple, profile, payloads


def _write_observer(diagnostics: Path, payload: bytes) -> None:
    final = diagnostics / "screen-last.png"
    visual = diagnostics / "screen-visual.png"
    archived = diagnostics / "screen-0001.png"
    final.write_bytes(payload)
    visual.write_bytes(payload)
    archived.write_bytes(payload)
    (diagnostics / "observer.status").write_text(
        "\n".join(
            (
                "schema=amipro-oracle-screen-observer-v1",
                "status=ok",
                "capture_count=1",
                "archived_count=1",
                "visual_count=1",
                "failure_count=0",
                f"final_sha256={sha256_file(final)}",
                f"final_bytes={final.stat().st_size}",
                "",
            )
        ),
        encoding="ascii",
    )


def test_printer_config_batch_and_key_are_pinned() -> None:
    ready = {
        "schema": printer_module.launch_module.AMIPRO_READY_SCHEMA,
        "status": "amipro-ready",
        "runtime_key": "2" * 64,
        "launch_tree_digest": "3" * 64,
        "sealed_tree_digest": "4" * 64,
    }
    config = printer_module.printer_install_config()
    batch = printer_module.printer_install_batch()
    inputs = printer_module.printer_install_inputs(
        ready,
        _windows_media(),
        _flat_media(),
        _image_record(),
    )

    assert 'MOUNT S "/oracle/media/windows" -ro' in config
    assert r"Z:\CONFIG.COM -SECUREMODE" in config
    assert r"C:\PRNINS.BAT" in config
    assert "autolock=false" in config
    assert batch.endswith(b"\r\n")
    assert b"CONTROL.EXE PRINTERS" in batch
    assert inputs["printer_profile"] == printer_module.PRINTER_PROFILE

    changed = printer_module.printer_install_inputs(
        ready,
        _windows_media(),
        _flat_media(),
        _image_record(),
        outer_time_limit_seconds=60,
    )
    assert digest_json(inputs) != digest_json(changed)
    with pytest.raises(OracleError):
        printer_module.printer_install_inputs(
            ready,
            _windows_media(),
            _flat_media(),
            _image_record(),
            outer_time_limit_seconds=91,
        )


def test_printer_checkpoint_promotes_reuses_and_rejects_tampered_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "oracle"
    parent = home / "cache" / "amipro-ready" / ("2" * 64)
    parent_runtime = parent / "pristine-c"
    parent_runtime.mkdir(parents=True)
    _installed_runtime(parent_runtime)
    install_module._normalize_runtime_metadata(parent_runtime)
    original_install_hash = install_module.sha256_file
    original_printer_hash = printer_module.sha256_file

    def install_hash(path: Path) -> str:
        if path.name.casefold() == "amipro.exe":
            return EXPECTED_AMIPRO_EXE_SHA256
        return original_install_hash(path)

    expected_printer_hashes = {
        "pscript.drv": printer_module.PSCRIPT_DRV_SHA256,
        "pscript.hlp": printer_module.PSCRIPT_HLP_SHA256,
        "testps.txt": printer_module.TESTPS_TXT_SHA256,
    }

    def printer_hash(path: Path) -> str:
        expected = expected_printer_hashes.get(path.name.casefold())
        return expected if expected is not None else original_printer_hash(path)

    monkeypatch.setattr(install_module, "sha256_file", install_hash)
    monkeypatch.setattr(printer_module, "sha256_file", printer_hash)
    parent_tree = install_module._validate_installed_amipro(parent_runtime)
    ready: dict[str, object] = {
        "schema": printer_module.launch_module.AMIPRO_READY_SCHEMA,
        "status": "amipro-ready",
        "runtime_key": "2" * 64,
        "launch_tree_digest": parent_tree["digest"],
        "sealed_tree_digest": "4" * 64,
    }
    flat_source = home / "cache" / "media" / ("e" * 64) / "source"
    flat_source.mkdir(parents=True)
    flat = _flat_media()
    states, profile, payloads = _synthetic_profile(tmp_path)
    calls = 0

    def select(
        _home: Path,
        runtime_key: str | None,
    ) -> tuple[Path, dict[str, object], dict[str, object], str]:
        assert runtime_key in {None, "2" * 64}
        return parent, ready, {"schema": "ready-inputs"}, "ready-evidence"

    def invoke(
        _invocation: object,
        job: Path,
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, object], dict[str, object]]:
        nonlocal calls
        calls += 1
        assert timeout_seconds == printer_module.OUTER_TIME_LIMIT_SECONDS
        runtime = job / "runtime"
        (runtime / "PRNINS.STA").write_bytes(b"PRINTER_INSTALL_REQUESTED\r\n")
        (runtime / "PRNINS.OK").write_bytes(b"PRINTER_INSTALL_RETURNED_ZERO\r\n")
        _install_printer_files(runtime)
        diagnostics = job / "diagnostics"
        _write_observer(diagnostics, payloads[-1])
        observed_states: list[dict[str, object]] = []
        for state, payload, filename in zip(
            states,
            payloads,
            printer_module._STATE_FILES,
            strict=True,
        ):
            path = diagnostics / filename
            path.write_bytes(payload)
            observed, _ = install_module._screen_state(path, state)
            observed["path"] = filename
            observed_states.append(observed)
        driver = {
            "schema": printer_module.PRINTER_INSTALL_UI_SCHEMA,
            "status": "success",
            "profile": profile,
            "states": observed_states,
            "actions": printer_module._expected_actions(),
        }
        printer_module.atomic_write_json(job / "ui-driver.json", driver)
        process = {
            "command": ["podman", "run"],
            "exit_code": 0,
            "timed_out": False,
            "killed": False,
            "duration_seconds": 1.0,
        }
        return process, driver

    monkeypatch.setattr(printer_module, "PRINTER_INSTALL_STATES", states)
    monkeypatch.setattr(printer_module, "PRINTER_PROFILE", profile)
    monkeypatch.setattr(printer_module, "_require_verified_image", lambda _record: None)
    monkeypatch.setattr(printer_module.smoke_module, "_select_ready_runtime", select)
    monkeypatch.setattr(
        printer_module,
        "ensure_flat_windows_media",
        lambda *_args: (flat_source, flat),
    )
    monkeypatch.setattr(
        printer_module.bootstrap_module,
        "_verify_flat_media_cache",
        lambda *_args, **_kwargs: flat,
    )
    monkeypatch.setattr(printer_module, "_invoke_job", invoke)

    result = printer_module.install_printer_ready(
        home,
        tmp_path / "media",
        _windows_media(),
        _image_record(),
    )
    assert result["status"] == "printer-ready"
    assert result["cache_reused"] is False
    assert result["promotion_state"] == "committed"
    assert result["runtime"]["baseline_eligible"] is False
    assert calls == 1

    again = printer_module.install_printer_ready(
        home,
        tmp_path / "media",
        _windows_media(),
        _image_record(),
    )
    assert again["cache_reused"] is True
    assert again["runtime"] == result["runtime"]
    assert again["evidence_job"] == result["evidence_job"]
    assert calls == 1

    job = home / "jobs" / str(result["evidence_job"])
    screenshot = job / "diagnostics" / "qms-colorscript-installed.png"
    screenshot.write_bytes(b"tampered")
    with pytest.raises(OracleError) as caught:
        printer_module.install_printer_ready(
            home,
            tmp_path / "media",
            _windows_media(),
            _image_record(),
        )
    assert caught.value.exit_code == EXIT_INTEGRITY


def test_install_printer_cli_requires_rights_and_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "oracle"
    media = tmp_path / "windows-media"
    media.mkdir()
    monkeypatch.setattr(oracle_cli, "oracle_home", lambda *_args, **_kwargs: home)

    assert oracle_cli.main(["install-printer", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2

    monkeypatch.setattr(oracle_cli, "_toolchain_image", lambda _home: _image_record())
    monkeypatch.setattr(oracle_cli, "inventory_media", lambda *_args, **_kwargs: _windows_media())
    monkeypatch.setattr(
        oracle_cli,
        "install_printer_ready",
        lambda *_args, **_kwargs: {
            "status": "printer-ready",
            "runtime_key": "5" * 64,
        },
    )
    assert (
        oracle_cli.main(
            [
                "install-printer",
                "--win31-media",
                str(media),
                "--confirm-proprietary-media-rights",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "printer-ready"
