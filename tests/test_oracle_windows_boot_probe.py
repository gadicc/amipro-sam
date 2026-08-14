from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from amipro_oracle import cli as oracle_cli
from amipro_oracle import oci as oci_module
from amipro_oracle import windows_boot_probe as boot_module
from amipro_oracle.constants import EXIT_INTEGRITY
from amipro_oracle.errors import OracleError
from amipro_oracle.io import digest_json, sha256_file
from amipro_oracle.oci import PodmanInvocation, exec_podman_checked
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


def _installed_windows(runtime: Path) -> None:
    system = runtime / "WINDOWS" / "SYSTEM"
    system.mkdir(parents=True, exist_ok=True)
    (runtime / "SETUP.OK").write_bytes(b"SETUP_RETURNED_ZERO\r\n")
    (runtime / "ORADATE.TXT").write_bytes(b"03/10/1992\r\n")
    (runtime / "ORATIME.TXT").write_bytes(b"3:10:00\r\n")
    (runtime / "WINDOWS" / "WIN.COM").write_bytes(b"win")
    (runtime / "WINDOWS" / "PROGMAN.EXE").write_bytes(b"progman")
    (runtime / "WINDOWS" / "WIN.INI").write_text("[windows]\r\n", encoding="latin-1")
    (runtime / "WINDOWS" / "SYSTEM.INI").write_text(
        "[boot]\r\ndisplay.drv=VGA.DRV\r\nmouse.drv=MOUSE.DRV\r\nshell=PROGMAN.EXE\r\n",
        encoding="latin-1",
    )
    for name in ("KRNL386.EXE", "GDI.EXE", "USER.EXE", "VGA.DRV", "MOUSE.DRV"):
        (system / name).write_bytes(name.encode("ascii"))


def _screen(*, blue_pixels: int, variant: int) -> bytes:
    count = boot_module.SCREEN_WIDTH * boot_module.SCREEN_HEIGHT
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
    fill = bytes((variant, 0, 0))
    pixels.extend(fill * (count - consumed))
    return encode_rgb_png(boot_module.SCREEN_WIDTH, boot_module.SCREEN_HEIGHT, bytes(pixels))


def _write_observer(diagnostics: Path, ready: bytes, confirmation: bytes) -> None:
    final = diagnostics / "screen-last.png"
    visual = diagnostics / "screen-visual.png"
    archived = (diagnostics / "screen-0001.png", diagnostics / "screen-0002.png")
    final.write_bytes(confirmation)
    visual.write_bytes(ready)
    archived[0].write_bytes(ready)
    archived[1].write_bytes(confirmation)
    (diagnostics / "observer.status").write_text(
        "\n".join(
            (
                "schema=amipro-oracle-screen-observer-v1",
                "status=ok",
                "capture_count=2",
                "archived_count=2",
                "visual_count=2",
                "failure_count=0",
                f"final_sha256={sha256_file(final)}",
                f"final_bytes={final.stat().st_size}",
                "",
            )
        ),
        encoding="ascii",
    )


def _candidate(home: Path) -> tuple[Path, dict[str, object], dict[str, object], str]:
    key = "d" * 64
    root = home / "cache" / "windows" / key
    runtime = root / "pristine-c"
    runtime.mkdir(parents=True)
    _installed_windows(runtime)
    boot_module._normalize_runtime_metadata(runtime)
    tree = boot_module._inventory_windows_runtime(runtime)
    manifest: dict[str, object] = {
        "schema": boot_module.bootstrap_module.WINDOWS_CHECKPOINT_SCHEMA,
        "status": "windows-install-candidate",
        "checkpoint_key": key,
        "guest_tree_digest": tree["digest"],
        "sealed_tree_digest": "e" * 64,
    }
    (root / "runtime.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return root, manifest, {"schema": "candidate-inputs"}, "candidate-evidence"


def _write_boot_success(job: Path) -> tuple[dict[str, object], dict[str, object]]:
    runtime = job / "runtime"
    (runtime / "BOOT.STA").write_bytes(b"WINDOWS_LAUNCH_REQUESTED\r\n")
    (runtime / "BOOT.OK").write_bytes(b"WINDOWS_RETURNED_ZERO\r\n")
    ready_payload = _screen(blue_pixels=20_000, variant=1)
    confirmation_payload = _screen(blue_pixels=8_000, variant=2)
    diagnostics = job / "diagnostics"
    _write_observer(diagnostics, ready_payload, confirmation_payload)
    ready_path = diagnostics / "program-manager-ready.png"
    confirmation_path = diagnostics / "exit-windows-confirmation.png"
    ready_path.write_bytes(ready_payload)
    confirmation_path.write_bytes(confirmation_payload)
    ready, _ = boot_module._screen_metrics(ready_path)
    confirmation, _ = boot_module._screen_metrics(confirmation_path)
    driver = {
        "schema": boot_module.UI_DRIVER_SCHEMA,
        "status": "success",
        "profile": boot_module.UI_PROFILE,
        "ready": {"path": ready_path.name, **ready},
        "confirmation": {"path": confirmation_path.name, **confirmation},
        "actions": [
            {"action": "alt-f4", "exit_code": 0},
            {"action": "enter", "exit_code": 0},
        ],
        "elapsed_seconds": 1.0,
    }
    boot_module.atomic_write_json(job / "ui-driver.json", driver)
    process = {
        "command": ["podman", "run"],
        "exit_code": 0,
        "timed_out": False,
        "killed": False,
        "duration_seconds": 1.0,
    }
    return process, driver


def test_boot_config_batch_and_key_are_media_free_and_fail_closed() -> None:
    config = boot_module.windows_boot_config()
    batch = boot_module.windows_boot_batch()
    candidate = {
        "schema": boot_module.bootstrap_module.WINDOWS_CHECKPOINT_SCHEMA,
        "status": "windows-install-candidate",
        "checkpoint_key": "d" * 64,
        "guest_tree_digest": "e" * 64,
        "sealed_tree_digest": "f" * 64,
    }
    inputs = boot_module.windows_boot_inputs(candidate, _image_record())

    assert 'MOUNT C "/oracle/job/runtime" -freesize 128' in config
    assert "MOUNT S" not in config
    assert r"Z:\CONFIG.COM -SECUREMODE" in config
    assert r"C:\WINBOOT.BAT" in config
    assert batch.endswith(b"\r\n")
    assert b"C:\\WINDOWS\\WIN.COM" in batch
    assert b"BOOT.STA" in batch
    assert b"BOOT.START" not in batch
    assert b"WINDOWS_RETURNED_ZERO" in batch
    assert inputs["outer_time_limit_seconds"] == boot_module.OUTER_TIME_LIMIT_SECONDS

    changed = boot_module.windows_boot_inputs(
        candidate,
        _image_record(),
        outer_time_limit_seconds=60,
    )
    assert digest_json(inputs) != digest_json(changed)
    with pytest.raises(OracleError):
        boot_module.windows_boot_inputs(
            candidate,
            _image_record(),
            outer_time_limit_seconds=91,
        )


def test_program_manager_screen_profile_requires_ready_and_confirmation_states(
    tmp_path: Path,
) -> None:
    ready_path = tmp_path / "ready.png"
    confirmation_path = tmp_path / "confirmation.png"
    ready_path.write_bytes(_screen(blue_pixels=20_000, variant=1))
    confirmation_path.write_bytes(_screen(blue_pixels=8_000, variant=2))
    ready, _ = boot_module._screen_metrics(ready_path)
    confirmation, _ = boot_module._screen_metrics(confirmation_path)

    assert boot_module._is_program_manager_ready(ready)
    assert boot_module._is_exit_confirmation(confirmation, str(ready["sha256"]))
    assert not boot_module._is_exit_confirmation(ready, str(ready["sha256"]))

    ready_path.write_bytes(b"not a png")
    with pytest.raises(OracleError):
        boot_module._screen_metrics(ready_path)


def test_checked_podman_exec_binds_to_new_cid_and_instance_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cidfile = tmp_path / "container.cid"
    cidfile.write_text("1" * 64, encoding="ascii")
    invocation = PodmanInvocation(("podman", "run"), "amipro-oracle-test", cidfile, tmp_path)
    calls: list[list[str]] = []
    label = ["amipro-oracle-test"]

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["container", "exists"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, 0, f"{label[0]}\n", "")
        return subprocess.CompletedProcess(command, 0, "42\n", "")

    monkeypatch.setattr(oci_module.shutil, "which", lambda _name: "/usr/bin/podman")
    monkeypatch.setattr(oci_module.subprocess, "run", fake_run)
    result = exec_podman_checked(
        invocation,
        ("xdotool", "search", "DOSBox-X"),
        environment={"DISPLAY": ":99"},
    )

    assert result["exit_code"] == 0
    assert result["stdout"] == "42\n"
    assert calls[-1][:3] == ["/usr/bin/podman", "exec", "--env=DISPLAY=:99"]
    assert calls[-1][3] == "1" * 64

    label[0] = "amipro-oracle-different"
    with pytest.raises(OracleError) as caught:
        exec_podman_checked(invocation, ("true",))
    assert caught.value.exit_code == EXIT_INTEGRITY


def test_boot_probe_promotes_reuses_and_rejects_tampered_visual_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "oracle"
    source_root, candidate, candidate_inputs, candidate_evidence = _candidate(home)
    calls = 0

    def verify(_home: Path, checkpoint_key: str) -> tuple[Path, dict, dict, str]:
        assert checkpoint_key == candidate["checkpoint_key"]
        return source_root, candidate, candidate_inputs, candidate_evidence

    def invoke(
        _invocation: object,
        job: Path,
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, object], dict[str, object]]:
        nonlocal calls
        calls += 1
        assert timeout_seconds == boot_module.OUTER_TIME_LIMIT_SECONDS
        return _write_boot_success(job)

    monkeypatch.setattr(boot_module, "_require_verified_image", lambda _record: None)
    monkeypatch.setattr(boot_module, "verify_windows_install_candidate", verify)
    monkeypatch.setattr(boot_module, "_invoke_boot_job", invoke)

    result = boot_module.boot_windows_ready(home, _image_record())
    assert result["status"] == "windows-ready"
    assert result["cache_reused"] is False
    assert result["promotion_state"] == "committed"
    assert result["runtime"]["checkpoint_role"] == "base-for-ami-pro-installation"
    assert calls == 1

    again = boot_module.boot_windows_ready(home, _image_record())
    assert again["cache_reused"] is True
    assert again["runtime"] == result["runtime"]
    assert again["evidence_job"] == result["evidence_job"]
    assert calls == 1

    job = home / "jobs" / str(result["evidence_job"])
    visual = job / "diagnostics" / "program-manager-ready.png"
    visual.write_bytes(b"tampered")
    with pytest.raises(OracleError) as caught:
        boot_module.boot_windows_ready(home, _image_record())
    assert caught.value.exit_code == EXIT_INTEGRITY


def test_boot_probe_cli_requires_rights_and_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "oracle"
    monkeypatch.setattr(oracle_cli, "oracle_home", lambda *_args, **_kwargs: home)

    assert oracle_cli.main(["boot-probe", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2

    monkeypatch.setattr(oracle_cli, "_toolchain_image", lambda _home: _image_record())
    monkeypatch.setattr(
        oracle_cli,
        "boot_windows_ready",
        lambda *_args, **_kwargs: {
            "status": "windows-ready",
            "runtime_key": "f" * 64,
        },
    )
    assert (
        oracle_cli.main(
            [
                "boot-probe",
                "--confirm-proprietary-media-rights",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "windows-ready"
