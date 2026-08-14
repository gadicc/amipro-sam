from __future__ import annotations

import json
from pathlib import Path

import pytest

from amipro_oracle import amipro_install as install_module
from amipro_oracle import amipro_launch_probe as launch_module
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
        ("warning", "editor", "program-manager", "exit-windows"),
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
        "name": "synthetic-amipro-launch-v1",
        "screen_width": install_module.SCREEN_WIDTH,
        "screen_height": install_module.SCREEN_HEIGHT,
        "autolock": False,
        "stable_samples": 2,
        "poll_seconds": 0.25,
        "states": list(state_tuple),
        "actions": [
            "dismiss-printer-warning",
            "close-amipro",
            "exit-windows",
            "confirm-exit-windows",
        ],
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


def test_launch_config_batch_and_key_are_media_free_and_pinned() -> None:
    candidate = {
        "schema": install_module.AMIPRO_CHECKPOINT_SCHEMA,
        "status": "amipro-install-candidate",
        "checkpoint_key": "d" * 64,
        "guest_tree_digest": "e" * 64,
        "sealed_tree_digest": "f" * 64,
    }
    config = launch_module.amipro_launch_config()
    batch = launch_module.amipro_launch_batch()
    inputs = launch_module.amipro_launch_inputs(candidate, _image_record())

    assert 'MOUNT C "/oracle/job/runtime" -freesize 128' in config
    assert "MOUNT S" not in config
    assert "autolock=false" in config
    assert r"Z:\CONFIG.COM -SECUREMODE" in config
    assert r"C:\AMILNCH.BAT" in config
    assert batch.endswith(b"\r\n")
    assert b"WIN.COM C:\\AMIPRO\\AMIPRO.EXE" in batch
    assert b"AMIPRO_RETURNED_ZERO" in batch
    assert inputs["printer_profile"] == "none-screen-formatting-warning-expected"

    changed = launch_module.amipro_launch_inputs(
        candidate,
        _image_record(),
        outer_time_limit_seconds=60,
    )
    assert digest_json(inputs) != digest_json(changed)
    with pytest.raises(OracleError):
        launch_module.amipro_launch_inputs(
            candidate,
            _image_record(),
            outer_time_limit_seconds=121,
        )


def test_launch_checkpoint_promotes_reuses_and_rejects_tampered_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "oracle"
    parent = home / "cache" / "amipro" / ("d" * 64)
    parent_runtime = parent / "pristine-c"
    parent_runtime.mkdir(parents=True)
    _installed_runtime(parent_runtime)
    install_module._normalize_runtime_metadata(parent_runtime)
    original_install_hash = install_module.sha256_file

    def install_hash(path: Path) -> str:
        if path.name.casefold() == "amipro.exe":
            return EXPECTED_AMIPRO_EXE_SHA256
        return original_install_hash(path)

    monkeypatch.setattr(install_module, "sha256_file", install_hash)
    parent_tree = install_module._validate_installed_amipro(parent_runtime)
    candidate: dict[str, object] = {
        "schema": install_module.AMIPRO_CHECKPOINT_SCHEMA,
        "status": "amipro-install-candidate",
        "checkpoint_key": "d" * 64,
        "guest_tree_digest": parent_tree["digest"],
        "sealed_tree_digest": "f" * 64,
    }
    states, profile, payloads = _synthetic_profile(tmp_path)
    calls = 0

    def select(
        _home: Path,
        checkpoint_key: str | None,
    ) -> tuple[Path, dict[str, object], dict[str, object], str]:
        assert checkpoint_key in {None, "d" * 64}
        return parent, candidate, {"schema": "candidate-inputs"}, "install-evidence"

    def invoke(
        _invocation: object,
        job: Path,
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, object], dict[str, object]]:
        nonlocal calls
        calls += 1
        assert timeout_seconds == launch_module.OUTER_TIME_LIMIT_SECONDS
        runtime = job / "runtime"
        (runtime / "AMILNCH.STA").write_bytes(b"AMIPRO_LAUNCH_REQUESTED\r\n")
        (runtime / "AMILNCH.OK").write_bytes(b"AMIPRO_RETURNED_ZERO\r\n")
        diagnostics = job / "diagnostics"
        _write_observer(diagnostics, payloads[-1])
        filenames = (
            "amipro-printer-warning.png",
            "amipro-editor-ready.png",
            "program-manager-minimized.png",
            "exit-windows-confirmation.png",
        )
        observed_states: list[dict[str, object]] = []
        for state, payload, filename in zip(states, payloads, filenames, strict=True):
            path = diagnostics / filename
            path.write_bytes(payload)
            observed, _ = install_module._screen_state(path, state)
            observed["path"] = filename
            observed_states.append(observed)
        driver = {
            "schema": launch_module.AMIPRO_LAUNCH_UI_SCHEMA,
            "status": "success",
            "profile": profile,
            "states": observed_states,
            "actions": [
                {
                    "action": "dismiss-printer-warning",
                    "key": "Return",
                    "exit_code": 0,
                },
                {"action": "close-amipro", "key": "alt+F4", "exit_code": 0},
                {"action": "exit-windows", "key": "alt+F4", "exit_code": 0},
                {
                    "action": "confirm-exit-windows",
                    "key": "Return",
                    "exit_code": 0,
                },
            ],
        }
        launch_module.atomic_write_json(job / "ui-driver.json", driver)
        process = {
            "command": ["podman", "run"],
            "exit_code": 0,
            "timed_out": False,
            "killed": False,
            "duration_seconds": 1.0,
        }
        return process, driver

    monkeypatch.setattr(launch_module, "LAUNCH_STATES", states)
    monkeypatch.setattr(launch_module, "LAUNCH_UI_PROFILE", profile)
    monkeypatch.setattr(launch_module, "_require_verified_image", lambda _record: None)
    monkeypatch.setattr(launch_module, "_select_install_candidate", select)
    monkeypatch.setattr(launch_module, "_invoke_launch_job", invoke)

    result = launch_module.launch_amipro_ready(home, _image_record())
    assert result["status"] == "amipro-ready"
    assert result["cache_reused"] is False
    assert result["promotion_state"] == "committed"
    assert result["runtime"]["baseline_eligible"] is False
    assert result["runtime"]["checkpoint_role"] == "base-for-invented-document-smoke"
    assert calls == 1

    again = launch_module.launch_amipro_ready(home, _image_record())
    assert again["cache_reused"] is True
    assert again["runtime"] == result["runtime"]
    assert again["evidence_job"] == result["evidence_job"]
    assert calls == 1

    job = home / "jobs" / str(result["evidence_job"])
    screenshot = job / "diagnostics" / "amipro-editor-ready.png"
    screenshot.write_bytes(b"tampered")
    with pytest.raises(OracleError) as caught:
        launch_module.launch_amipro_ready(home, _image_record())
    assert caught.value.exit_code == EXIT_INTEGRITY


def test_launch_amipro_cli_requires_rights_and_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "oracle"
    monkeypatch.setattr(oracle_cli, "oracle_home", lambda *_args, **_kwargs: home)

    assert oracle_cli.main(["launch-amipro", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2

    monkeypatch.setattr(oracle_cli, "_toolchain_image", lambda _home: _image_record())
    monkeypatch.setattr(
        oracle_cli,
        "launch_amipro_ready",
        lambda *_args, **_kwargs: {
            "status": "amipro-ready",
            "runtime_key": "f" * 64,
        },
    )
    assert (
        oracle_cli.main(
            [
                "launch-amipro",
                "--confirm-proprietary-media-rights",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "amipro-ready"
