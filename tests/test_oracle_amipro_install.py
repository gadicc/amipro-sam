from __future__ import annotations

import json
from pathlib import Path

import pytest

from amipro_oracle import amipro_install as install_module
from amipro_oracle import cli as oracle_cli
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


def _media() -> dict[str, object]:
    return {
        "schema": "amipro-oracle-media-v1",
        "kind": "amipro",
        "media_profile": install_module.AMIPRO_MEDIA_PROFILE,
        "digest": "d" * 64,
        "file_count": 8,
    }


def _flat_media() -> dict[str, object]:
    return {
        "schema": install_module.AMIPRO_FLAT_SCHEMA,
        "status": "ready",
        "cache_key": "e" * 64,
        "extraction_digest": install_module.EXPECTED_EXTRACTION_DIGEST,
        "file_count": install_module.EXPECTED_EXTRACTION_FILES,
        "total_bytes": install_module.EXPECTED_EXTRACTION_BYTES,
        "tree_digest": "f" * 64,
    }


def _windows_ready() -> dict[str, object]:
    return {
        "schema": install_module.boot_module.WINDOWS_READY_SCHEMA,
        "status": "windows-ready",
        "runtime_key": "1" * 64,
        "sealed_tree_digest": "2" * 64,
    }


def _installed_windows(runtime: Path) -> None:
    system = runtime / "WINDOWS" / "SYSTEM"
    system.mkdir(parents=True, exist_ok=True)
    (runtime / "SETUP.OK").write_bytes(b"SETUP_RETURNED_ZERO\r\n")
    (runtime / "ORADATE.TXT").write_bytes(b"03/10/1992\r\n")
    (runtime / "ORATIME.TXT").write_bytes(b"3:10:00\r\n")
    (runtime / "WINDOWS" / "WIN.COM").write_bytes(b"win")
    (runtime / "WINDOWS" / "PROGMAN.EXE").write_bytes(b"progman")
    (runtime / "WINDOWS" / "WIN.INI").write_text(
        "[windows]\r\n",
        encoding="latin-1",
    )
    (runtime / "WINDOWS" / "SYSTEM.INI").write_text(
        "[boot]\r\ndisplay.drv=VGA.DRV\r\n"
        "mouse.drv=MOUSE.DRV\r\nshell=PROGMAN.EXE\r\n",
        encoding="latin-1",
    )
    for name in ("KRNL386.EXE", "GDI.EXE", "USER.EXE", "VGA.DRV", "MOUSE.DRV"):
        (system / name).write_bytes(name.encode("ascii"))


def _install_amipro(runtime: Path) -> None:
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
        "[AmiPro]\r\n"
        "macrodir=c:\\amipro\\macros\r\n"
        "stypath=c:\\amipro\\styles\r\n"
        "docpath=c:\\amipro\\docs\r\n"
        "automacroload=1,_autorun.smm!zrunmacs\r\n",
        encoding="latin-1",
    )
    (runtime / "WINDOWS" / "LOTUS.INI").write_text(
        "[Lotus Applications]\r\n"
        "AMIPRO=c:\\amipro\\amipro.exe\r\n"
        "Common Directory=c:\\windows\\lotusapp\r\n"
        "Program Path=c:\\windows\\lotusapp\\spell\r\n",
        encoding="latin-1",
    )
    with (runtime / "WINDOWS" / "WIN.INI").open("a", encoding="latin-1") as handle:
        handle.write("[Extensions]\r\nsam=c:\\amipro\\amipro.exe ^.sam\r\n")
    (runtime / "AMIINST.OK").write_bytes(b"INSTALL_RETURNED_ZERO\r\n")


def _screen() -> bytes:
    width = install_module.SCREEN_WIDTH
    height = install_module.SCREEN_HEIGHT
    row = b"\x10\x20\x30" * (width // 2) + b"\x90\x80\x70" * (width // 2)
    return encode_rgb_png(width, height, row * height)


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


def _synthetic_ui_profile(
    tmp_path: Path,
) -> tuple[tuple[dict[str, object], ...], dict[str, object], bytes]:
    payload = _screen()
    screen = tmp_path / "screen.png"
    screen.write_bytes(payload)
    provisional: dict[str, object] = {
        "name": "synthetic-dialog",
        "box": [0, 0, 2, 1],
        "title_sha256": "0" * 64,
        "keys": ["Return"],
    }
    observed, _ = install_module._screen_state(screen, provisional)
    state = {**provisional, "title_sha256": observed["title_sha256"]}
    states = (state,)
    profile = {
        "name": "synthetic-installer-profile-v1",
        "screen_width": install_module.SCREEN_WIDTH,
        "screen_height": install_module.SCREEN_HEIGHT,
        "autolock": False,
        "stable_samples": 2,
        "poll_seconds": 0.25,
        "states": list(states),
        "post_install_exit_profile": install_module.boot_module.UI_PROFILE,
    }
    return states, profile, payload


def _program_manager_screen(*, blue_pixels: int, variant: int) -> bytes:
    count = install_module.SCREEN_WIDTH * install_module.SCREEN_HEIGHT
    colors = [
        (index * 11 % 256, index * 17 % 256, index * 23 % 256)
        for index in range(1, 14)
    ]
    pixels = bytearray()
    pixels.extend(b"\xff\xff\xff" * 180_000)
    pixels.extend(b"\xc3\xc7\xcb" * 180_000)
    pixels.extend(b"\x00\x00\xaa" * blue_pixels)
    for color in colors:
        pixels.extend(bytes(color))
    consumed = 360_000 + blue_pixels + len(colors)
    pixels.extend(bytes((variant, 0, 0)) * (count - consumed))
    return encode_rgb_png(
        install_module.SCREEN_WIDTH,
        install_module.SCREEN_HEIGHT,
        bytes(pixels),
    )


def test_install_config_batch_and_key_are_pinned() -> None:
    config = install_module.amipro_install_config()
    batch = install_module.amipro_install_batch()
    inputs = install_module.amipro_install_inputs(
        _windows_ready(),
        _media(),
        _flat_media(),
        _image_record(),
    )

    assert 'MOUNT S "/oracle/media/amipro" -t dir -ro' in config
    assert 'MOUNT C "/oracle/job/runtime" -freesize 128' in config
    assert "autolock=false" in config
    assert r"Z:\CONFIG.COM -SECUREMODE" in config
    assert r"C:\AMIINST.BAT" in config
    assert batch.endswith(b"\r\n")
    assert b"C:\\WINDOWS\\WIN.COM S:\\INSTALL.EXE" in batch
    assert b"INSTALL_RETURNED_ZERO" in batch
    assert inputs["inner_time_limit_seconds"] == install_module.INNER_TIME_LIMIT_SECONDS
    assert inputs["outer_time_limit_seconds"] == install_module.OUTER_TIME_LIMIT_SECONDS

    changed = install_module.amipro_install_inputs(
        _windows_ready(),
        _media(),
        _flat_media(),
        _image_record(),
        outer_time_limit_seconds=60,
    )
    assert digest_json(inputs) != digest_json(changed)
    with pytest.raises(OracleError):
        install_module.amipro_install_inputs(
            _windows_ready(),
            _media(),
            _flat_media(),
            _image_record(),
            outer_time_limit_seconds=301,
        )


def test_installer_crop_profile_rejects_changed_pixels(tmp_path: Path) -> None:
    states, _profile, payload = _synthetic_ui_profile(tmp_path)
    screen = tmp_path / "screen.png"
    screen.write_bytes(payload)

    observed, _ = install_module._screen_state(screen, states[0])
    assert observed["title_sha256"] == states[0]["title_sha256"]

    changed = bytearray(payload)
    changed[-16] ^= 1
    screen.write_bytes(changed)
    with pytest.raises(OracleError):
        install_module._screen_state(screen, states[0])


def test_install_checkpoint_promotes_reuses_and_rejects_tampered_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "oracle"
    parent = home / "cache" / "windows-ready" / ("1" * 64)
    parent_runtime = parent / "pristine-c"
    parent_runtime.mkdir(parents=True)
    _installed_windows(parent_runtime)
    install_module._normalize_runtime_metadata(parent_runtime)
    flat_source = home / "cache" / "media" / ("e" * 64) / "source"
    flat_source.mkdir(parents=True)
    states, profile, screen_payload = _synthetic_ui_profile(tmp_path)
    calls = 0

    def select(
        _home: Path,
        runtime_key: str | None,
    ) -> tuple[Path, dict[str, object], dict[str, object], str]:
        assert runtime_key in {None, "1" * 64}
        return parent, _windows_ready(), {"schema": "ready-inputs"}, "boot-evidence"

    def invoke(
        _invocation: object,
        job: Path,
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, object], dict[str, object]]:
        nonlocal calls
        calls += 1
        assert timeout_seconds == install_module.OUTER_TIME_LIMIT_SECONDS
        _install_amipro(job / "runtime")
        diagnostics = job / "diagnostics"
        _write_observer(diagnostics, screen_payload)
        snapshot = diagnostics / "installer-01-synthetic-dialog.png"
        snapshot.write_bytes(screen_payload)
        observed, _ = install_module._screen_state(snapshot, states[0])
        observed["path"] = snapshot.name
        driver = {
            "schema": install_module.AMIPRO_UI_SCHEMA,
            "status": "success",
            "profile": profile,
            "states": [observed],
            "actions": [
                {"state": "synthetic-dialog", "keys": ["Return"], "exit_code": 0}
            ],
        }
        ready_path = diagnostics / "installer-program-manager-ready.png"
        confirmation_path = diagnostics / "installer-exit-confirmation.png"
        ready_path.write_bytes(_program_manager_screen(blue_pixels=20_000, variant=1))
        confirmation_path.write_bytes(
            _program_manager_screen(blue_pixels=8_000, variant=2)
        )
        ready, _ = install_module.boot_module._screen_metrics(ready_path)
        confirmation, _ = install_module.boot_module._screen_metrics(confirmation_path)
        driver["program_manager_exit"] = {
            "profile": install_module.boot_module.UI_PROFILE,
            "ready": {"path": ready_path.name, **ready},
            "confirmation": {"path": confirmation_path.name, **confirmation},
            "actions": [
                {"action": "alt-f4", "exit_code": 0},
                {"action": "enter", "exit_code": 0},
            ],
        }
        install_module.atomic_write_json(job / "ui-driver.json", driver)
        process = {
            "command": ["podman", "run"],
            "exit_code": 0,
            "timed_out": False,
            "killed": False,
            "duration_seconds": 1.0,
        }
        return process, driver

    original_sha256 = sha256_file

    def synthetic_hash(path: Path) -> str:
        if path.name.casefold() == "amipro.exe":
            return EXPECTED_AMIPRO_EXE_SHA256
        return original_sha256(path)

    monkeypatch.setattr(install_module, "INSTALLER_STATES", states)
    monkeypatch.setattr(install_module, "INSTALLER_UI_PROFILE", profile)
    monkeypatch.setattr(install_module, "_require_verified_image", lambda _record: None)
    monkeypatch.setattr(install_module, "_select_windows_ready", select)
    monkeypatch.setattr(
        install_module,
        "ensure_flat_amipro_media",
        lambda *_args: (flat_source, _flat_media()),
    )
    monkeypatch.setattr(
        install_module,
        "_verify_flat_cache",
        lambda *_args, **_kwargs: _flat_media(),
    )
    monkeypatch.setattr(install_module, "_invoke_install_job", invoke)
    monkeypatch.setattr(install_module, "sha256_file", synthetic_hash)

    result = install_module.install_amipro_checkpoint(
        home,
        tmp_path / "media",
        _media(),
        _image_record(),
    )
    assert result["status"] == "amipro-install-candidate"
    assert result["cache_reused"] is False
    assert result["promotion_state"] == "committed"
    assert result["checkpoint"]["baseline_eligible"] is False
    assert result["checkpoint"]["checkpoint_role"] == (
        "requires-separate-amipro-launch-probe"
    )
    assert calls == 1

    again = install_module.install_amipro_checkpoint(
        home,
        tmp_path / "media",
        _media(),
        _image_record(),
    )
    assert again["cache_reused"] is True
    assert again["checkpoint"] == result["checkpoint"]
    assert again["evidence_job"] == result["evidence_job"]
    assert calls == 1

    evidence = home / "jobs" / str(result["evidence_job"])
    screenshot = evidence / "diagnostics" / "installer-01-synthetic-dialog.png"
    screenshot.write_bytes(b"tampered")
    with pytest.raises(OracleError) as caught:
        install_module.install_amipro_checkpoint(
            home,
            tmp_path / "media",
            _media(),
            _image_record(),
        )
    assert caught.value.exit_code == EXIT_INTEGRITY


def test_install_amipro_cli_requires_rights_and_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "oracle"
    media_path = tmp_path / "media"
    media_path.mkdir()
    monkeypatch.setattr(oracle_cli, "oracle_home", lambda *_args, **_kwargs: home)

    assert oracle_cli.main(["install-amipro", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2

    monkeypatch.setattr(oracle_cli, "_toolchain_image", lambda _home: _image_record())
    monkeypatch.setattr(oracle_cli, "inventory_media", lambda *_args, **_kwargs: _media())
    monkeypatch.setattr(
        oracle_cli,
        "install_amipro_checkpoint",
        lambda *_args, **_kwargs: {
            "status": "amipro-install-candidate",
            "checkpoint_key": "f" * 64,
        },
    )
    assert (
        oracle_cli.main(
            [
                "install-amipro",
                "--amipro-media",
                str(media_path),
                "--confirm-proprietary-media-rights",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "amipro-install-candidate"
