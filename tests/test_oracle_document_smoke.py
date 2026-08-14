from __future__ import annotations

import json
from pathlib import Path

import pytest

from amipro_oracle import amipro_install as install_module
from amipro_oracle import amipro_launch_probe as launch_module
from amipro_oracle import cli as oracle_cli
from amipro_oracle import document_smoke as smoke_module
from amipro_oracle.constants import EXIT_INTEGRITY, EXPECTED_AMIPRO_EXE_SHA256
from amipro_oracle.errors import OracleError
from amipro_oracle.io import digest_json, sha256_file
from amipro_oracle.raster import encode_rgb_png

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-basic.sam"


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


def _solid_screen(variant: int) -> bytes:
    width = install_module.SCREEN_WIDTH
    height = install_module.SCREEN_HEIGHT
    color = bytes((variant, variant + 1, variant + 2))
    return encode_rgb_png(width, height, color * width * height)


def _document_screen() -> bytes:
    width = install_module.SCREEN_WIDTH
    height = install_module.SCREEN_HEIGHT
    pixels = bytearray(b"\xff\xff\xff" * width * height)
    x0, y0, x1, y1 = smoke_module.DOCUMENT_TITLE_STATE["box"]
    for row in range(y0, y1):
        for column in range(x0, x1):
            offset = (row * width + column) * 3
            pixels[offset : offset + 3] = b"\x00\x00\xaa"
    body_x, body_y, _, _ = smoke_module.DOCUMENT_BODY_BOX
    for index in range(smoke_module.MINIMUM_BODY_DARK_PIXELS + 4):
        offset = (body_y * width + body_x + index) * 3
        pixels[offset : offset + 3] = b"\x00\x00\x00"
    return encode_rgb_png(width, height, bytes(pixels))


def _state_for_payload(
    tmp_path: Path,
    payload: bytes,
    *,
    name: str,
    box: list[int],
) -> dict[str, object]:
    path = tmp_path / f"{name}.png"
    path.write_bytes(payload)
    provisional: dict[str, object] = {
        "name": name,
        "box": box,
        "title_sha256": "0" * 64,
    }
    observed, _ = install_module._screen_state(path, provisional)
    return {**provisional, "title_sha256": observed["title_sha256"]}


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


def test_native_fixture_has_a_self_consistent_directory_trailer() -> None:
    payload, identity = smoke_module.read_text_fixture(FIXTURE)

    assert len(payload) == 596
    assert identity == {
        "schema": smoke_module.TEXT_FIXTURE_SCHEMA,
        "profile": "invented-version-4-cp1252-text-only-v1",
        "staged_name": "SMOKE.SAM",
        "size": 596,
        "sha256": "22c8346b62dd3b0ad5858e752a92d4a0a1297b8dbda648c356bd5b6ab8982e49",
        "embedded_directory_offset": 574,
    }
    assert payload[574:] == b"[Embedded]\r\n00000574\r\n"


def test_fixture_validation_rejects_non_native_text_envelopes(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.sam"
    malformed.write_bytes(b"[ver]\n\t4\n[edoc]\ntext\n>\n")
    with pytest.raises(OracleError) as caught:
        smoke_module.read_text_fixture(malformed)
    assert caught.value.exit_code == EXIT_INTEGRITY

    target = tmp_path / "target.sam"
    target.write_bytes(FIXTURE.read_bytes())
    link = tmp_path / "link.sam"
    link.symlink_to(target)
    with pytest.raises(OracleError) as caught:
        smoke_module.read_text_fixture(link)
    assert caught.value.exit_code == EXIT_INTEGRITY


def test_smoke_config_batch_and_inputs_are_media_free() -> None:
    fixture = smoke_module.validate_text_fixture(FIXTURE.read_bytes())
    ready = {
        "schema": launch_module.AMIPRO_READY_SCHEMA,
        "status": "amipro-ready",
        "runtime_key": "d" * 64,
        "sealed_tree_digest": "e" * 64,
    }
    config = smoke_module.document_smoke_config()
    batch = smoke_module.document_smoke_batch()
    inputs = smoke_module.document_smoke_inputs(ready, fixture, _image_record())

    assert 'MOUNT C "/oracle/job/runtime" -freesize 128' in config
    assert "MOUNT S" not in config
    assert r"Z:\CONFIG.COM -SECUREMODE" in config
    assert r"C:\DOCSMK.BAT" in config
    assert b"WIN.COM C:\\AMIPRO\\AMIPRO.EXE C:\\ORACLE\\SMOKE.SAM" in batch
    assert inputs["fixture"] == fixture
    assert inputs["printer_profile"] == "none-screen-formatting-warning-expected"
    shorter = smoke_module.document_smoke_inputs(
        ready,
        fixture,
        _image_record(),
        outer_time_limit_seconds=60,
    )
    assert digest_json(shorter) != digest_json(inputs)
    with pytest.raises(OracleError):
        smoke_module.document_smoke_inputs(
            ready,
            fixture,
            _image_record(),
            outer_time_limit_seconds=121,
        )


def test_document_predicate_requires_title_ink_and_no_hourglass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _document_screen()
    title = _state_for_payload(
        tmp_path,
        payload,
        name="document-title",
        box=list(smoke_module.DOCUMENT_TITLE_STATE["box"]),
    )
    monkeypatch.setattr(smoke_module, "DOCUMENT_TITLE_STATE", title)
    path = tmp_path / "ready.png"
    path.write_bytes(payload)

    evidence, _ = smoke_module._document_state(path)
    assert evidence["title_sha256"] == title["title_sha256"]
    assert evidence["body_dark_pixels"] > smoke_module.MINIMUM_BODY_DARK_PIXELS
    assert evidence["loading_indicator_dark_pixels"] == 0

    pixels = bytearray(b"\xff\xff\xff" * install_module.SCREEN_WIDTH * install_module.SCREEN_HEIGHT)
    x0, y0, x1, y1 = smoke_module.LOADING_INDICATOR_BOX
    for row in range(y0, y1):
        for column in range(x0, x1):
            offset = (row * install_module.SCREEN_WIDTH + column) * 3
            pixels[offset : offset + 3] = b"\x00\x00\x00"
    loading = tmp_path / "loading.png"
    loading.write_bytes(
        encode_rgb_png(
            install_module.SCREEN_WIDTH,
            install_module.SCREEN_HEIGHT,
            bytes(pixels),
        )
    )
    loading_evidence, _ = smoke_module._document_state(loading)
    assert loading_evidence["title_sha256"] != title["title_sha256"]
    assert loading_evidence["loading_indicator_dark_pixels"] > 0


def test_document_smoke_runs_from_ready_clone_and_retains_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "oracle"
    parent = home / "cache" / "amipro-ready" / ("d" * 64)
    pristine = parent / "pristine-c"
    pristine.mkdir(parents=True)
    _installed_runtime(pristine)
    install_module._normalize_runtime_metadata(pristine)
    original_hash = install_module.sha256_file

    def installed_hash(path: Path) -> str:
        if path.name.casefold() == "amipro.exe":
            return EXPECTED_AMIPRO_EXE_SHA256
        return original_hash(path)

    monkeypatch.setattr(install_module, "sha256_file", installed_hash)
    tree = install_module._validate_installed_amipro(pristine)
    ready: dict[str, object] = {
        "schema": launch_module.AMIPRO_READY_SCHEMA,
        "status": "amipro-ready",
        "runtime_key": "d" * 64,
        "launch_tree_digest": tree["digest"],
        "sealed_tree_digest": "e" * 64,
    }
    warning_payload = _solid_screen(1)
    document_payload = _document_screen()
    manager_payload = _solid_screen(3)
    exit_payload = _solid_screen(4)
    warning = _state_for_payload(
        tmp_path,
        warning_payload,
        name="warning",
        box=[0, 0, 2, 1],
    )
    title = _state_for_payload(
        tmp_path,
        document_payload,
        name="document-title",
        box=list(smoke_module.DOCUMENT_TITLE_STATE["box"]),
    )
    manager = _state_for_payload(
        tmp_path,
        manager_payload,
        name="manager",
        box=[0, 0, 2, 1],
    )
    exit_state = _state_for_payload(
        tmp_path,
        exit_payload,
        name="exit",
        box=[0, 0, 2, 1],
    )
    profile = {
        **smoke_module.DOCUMENT_UI_PROFILE,
        "states": [warning, title, manager, exit_state],
    }

    def select(
        _home: Path,
        runtime_key: str | None,
    ) -> tuple[Path, dict[str, object], dict[str, object], str]:
        assert runtime_key in {None, "d" * 64}
        return parent, ready, {"schema": "ready-inputs"}, "launch-evidence"

    def invoke(
        _invocation: object,
        job: Path,
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, object], dict[str, object]]:
        assert timeout_seconds == smoke_module.OUTER_TIME_LIMIT_SECONDS
        runtime = job / "runtime"
        (runtime / "DOCSMK.STA").write_bytes(b"DOCUMENT_LAUNCH_REQUESTED\r\n")
        (runtime / "DOCSMK.OK").write_bytes(b"DOCUMENT_RETURNED_ZERO\r\n")
        diagnostics = job / "diagnostics"
        _write_observer(diagnostics, document_payload)
        exact = (
            (warning, warning_payload, "document-printer-warning.png"),
            (
                manager,
                manager_payload,
                "document-program-manager-minimized.png",
            ),
            (
                exit_state,
                exit_payload,
                "document-exit-windows-confirmation.png",
            ),
        )
        observed_exact: list[dict[str, object]] = []
        for state, payload, name in exact:
            path = diagnostics / name
            path.write_bytes(payload)
            observed, _ = install_module._screen_state(path, state)
            observed["path"] = name
            observed_exact.append(observed)
        document_path = diagnostics / "document-ready.png"
        document_path.write_bytes(document_payload)
        document, _ = smoke_module._document_state(document_path)
        document["path"] = "document-ready.png"
        driver = {
            "schema": smoke_module.DOCUMENT_SMOKE_UI_SCHEMA,
            "status": "success",
            "profile": profile,
            "states": [observed_exact[0], document, observed_exact[1], observed_exact[2]],
            "actions": [
                {
                    "action": "dismiss-printer-warning",
                    "key": "Return",
                    "exit_code": 0,
                },
                {
                    "action": "close-document-and-amipro",
                    "key": "alt+F4",
                    "exit_code": 0,
                },
                {"action": "exit-windows", "key": "alt+F4", "exit_code": 0},
                {
                    "action": "confirm-exit-windows",
                    "key": "Return",
                    "exit_code": 0,
                },
            ],
        }
        smoke_module.atomic_write_json(job / "ui-driver.json", driver)
        return (
            {
                "command": ["podman", "run"],
                "exit_code": 0,
                "timed_out": False,
                "killed": False,
                "duration_seconds": 1.0,
            },
            driver,
        )

    monkeypatch.setattr(smoke_module, "DOCUMENT_TITLE_STATE", title)
    monkeypatch.setattr(smoke_module, "DOCUMENT_UI_PROFILE", profile)
    monkeypatch.setattr(launch_module, "PRINTER_WARNING_STATE", warning)
    monkeypatch.setattr(launch_module, "PROGRAM_MANAGER_MINIMIZED_STATE", manager)
    monkeypatch.setattr(install_module, "EXIT_WINDOWS_STATE", exit_state)
    monkeypatch.setattr(smoke_module, "_require_verified_image", lambda _record: None)
    monkeypatch.setattr(smoke_module, "_select_ready_runtime", select)
    monkeypatch.setattr(smoke_module, "_invoke_document_job", invoke)

    result = smoke_module.smoke_document(home, _image_record(), FIXTURE)

    assert result["status"] == "document-smoke-passed"
    assert result["baseline_eligible"] is False
    assert result["runtime_key"] == "d" * 64
    assert result["fixture"]["sha256"] == sha256_file(FIXTURE)
    assert [event["state"] for event in result["state_trace"]] == [
        "created",
        "staged",
        "guest-invoked",
        "guest-returned",
        "validated",
        "complete",
    ]
    job = home / "jobs" / str(result["evidence_job"])
    assert (job / "result.json").is_file()
    assert (job / "diagnostics" / "document-ready.png").is_file()
    assert not (job / "failure.json").exists()


def test_real_smoke_cli_requires_rights_and_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "oracle"
    monkeypatch.setattr(oracle_cli, "oracle_home", lambda *_args, **_kwargs: home)

    assert oracle_cli.main(["smoke", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2

    monkeypatch.setattr(oracle_cli, "_toolchain_image", lambda _home: _image_record())
    monkeypatch.setattr(
        oracle_cli,
        "smoke_document",
        lambda *_args, **_kwargs: {
            "status": "document-smoke-passed",
            "evidence_job": "smoke-document-test",
        },
    )
    assert (
        oracle_cli.main(
            [
                "smoke",
                "--confirm-proprietary-media-rights",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "document-smoke-passed"
