from __future__ import annotations

from pathlib import Path

import pytest

from amipro_oracle import windows_bootstrap as windows_module
from amipro_oracle.constants import EXIT_BACKEND, EXIT_INTEGRITY
from amipro_oracle.errors import OracleError
from amipro_oracle.io import digest_json, sha256_file
from amipro_oracle.raster import encode_rgb_png
from amipro_oracle.windows_bootstrap import (
    BOOTSTRAP_INPUT_SCHEMA,
    BOOTSTRAP_RESULT_SCHEMA,
    WINDOWS_CHECKPOINT_SCHEMA,
    WINDOWS_MEDIA_PROFILE,
    bootstrap_windows_checkpoint,
    windows_bootstrap_inputs,
    windows_setup_batch,
    windows_setup_config,
    windows_setup_shh,
)


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
        "schema": "amipro-oracle-media-v1",
        "kind": "windows-3.1",
        "media_profile": WINDOWS_MEDIA_PROFILE,
        "digest": "d" * 64,
        "file_count": 6,
    }


def _flat_manifest() -> dict[str, object]:
    return {
        "cache_key": "e" * 64,
        "extraction_digest": windows_module.EXPECTED_EXTRACTION_DIGEST,
        "tree_digest": "f" * 64,
    }


def _write_installed_windows(runtime: Path, *, sentinel: bool = True) -> None:
    system = runtime / "WINDOWS" / "SYSTEM"
    system.mkdir(parents=True, exist_ok=True)
    if sentinel:
        (runtime / "SETUP.OK").write_bytes(b"SETUP_RETURNED_ZERO\r\n")
    (runtime / "ORADATE.TXT").write_bytes(b"03/10/1992\r\n")
    (runtime / "ORATIME.TXT").write_bytes(b"3:10:00\r\n")
    (runtime / "WINDOWS" / "WIN.COM").write_bytes(b"win")
    (runtime / "WINDOWS" / "PROGMAN.EXE").write_bytes(b"progman")
    (runtime / "WINDOWS" / "WIN.INI").write_text("[windows]\r\n", encoding="latin-1")
    (runtime / "WINDOWS" / "SYSTEM.INI").write_text(
        "[boot]\r\ndisplay.drv=VGA.DRV\r\nmouse.drv=MOUSE.DRV\r\n",
        encoding="latin-1",
    )
    for name in ("KRNL386.EXE", "GDI.EXE", "USER.EXE", "VGA.DRV", "MOUSE.DRV"):
        (system / name).write_bytes(name.encode("ascii"))


def _write_observer_evidence(diagnostics: Path) -> None:
    pixels = b"\x00\x00\x00" + b"\xff\xff\xff" * (1024 * 768 - 1)
    payload = encode_rgb_png(1024, 768, pixels)
    final = diagnostics / "screen-last.png"
    visual = diagnostics / "screen-visual.png"
    first = diagnostics / "screen-0001.png"
    for path in (final, visual, first):
        path.write_bytes(payload)
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


def test_windows_setup_inputs_are_canonical_minimal_and_fail_closed() -> None:
    shh = windows_setup_shh()
    batch = windows_setup_batch()
    config = windows_setup_config()

    assert shh.endswith(b"\r\n")
    assert b"\n" not in shh.replace(b"\r\n", b"")
    assert b"showsysinfo=no" in shh
    assert b"network=nonet" in shh
    assert b"display=vga" in shh
    assert b"endopt=exit" in shh
    assert b"printers" not in shh.lower()
    assert b"tutorial" not in shh.lower()
    assert b"lanman" not in shh.lower()
    assert b"John Q. Public" not in shh

    assert 'MOUNT C "/oracle/job/runtime" -freesize 128' in config
    assert 'MOUNT S "/oracle/media/windows" -t dir -ro' in config
    assert "COUNTRY 1" in config
    assert "DATE 03/10/1992" in config
    assert "TIME 03:10:01" in config
    assert r"DATE /T > C:\ORADATE.TXT" in config
    assert r"Z:\CONFIG.COM -SECUREMODE" in config
    assert r"C:\WINSETUP.BAT" in config
    assert "SETUP.EXE" not in config
    assert config.index("MOUNT S") < config.index("-SECUREMODE") < config.index(
        "WINSETUP.BAT"
    )

    assert batch.endswith(b"\r\n")
    assert b"/I " not in batch
    assert b"/O:S:\\SETUP.INF /S:S:\\ /H:C:\\WIN31.SHH" in batch
    assert b"IF ERRORLEVEL 1 GOTO SETUP_FAILED" in batch
    assert b"SETUP_RETURNED_ZERO" in batch
    assert b"SETUP_ERRORLEVEL_NONZERO" in batch


def test_windows_bootstrap_key_covers_extraction_image_config_and_clock() -> None:
    inputs = windows_bootstrap_inputs(_windows_media(), _flat_manifest(), _image_record())
    assert inputs["schema"] == BOOTSTRAP_INPUT_SCHEMA
    assert inputs["printer_profile"] == "none"
    assert inputs["guest_clock"] == {
        "date_command": "03/10/1992",
        "time_command": "03:10:01",
        "expected_date": "03/10/1992",
        "expected_time": "3:10:00",
    }

    changed_image = _image_record()
    changed_image["image_digest"] = f"sha256:{'1' * 64}"
    changed = windows_bootstrap_inputs(_windows_media(), _flat_manifest(), changed_image)
    assert digest_json(inputs) != digest_json(changed)

    changed_flat = _flat_manifest()
    changed_flat["extraction_digest"] = "2" * 64
    changed = windows_bootstrap_inputs(_windows_media(), changed_flat, _image_record())
    assert digest_json(inputs) != digest_json(changed)

    changed_timeout = windows_bootstrap_inputs(
        _windows_media(),
        _flat_manifest(),
        _image_record(),
        outer_time_limit_seconds=600,
    )
    assert changed_timeout["outer_time_limit_seconds"] == 600
    assert digest_json(inputs) != digest_json(changed_timeout)

    with pytest.raises(OracleError):
        windows_bootstrap_inputs(
            _windows_media(),
            _flat_manifest(),
            _image_record(),
            outer_time_limit_seconds=1201,
        )


def test_observer_evidence_rejects_hash_mismatch_and_uniform_frame(
    tmp_path: Path,
) -> None:
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    _write_observer_evidence(diagnostics)
    accepted = windows_module._validate_observer_evidence(diagnostics)
    assert accepted["visual_count"] == 1

    (diagnostics / "screen-last.png").write_bytes(b"tampered")
    with pytest.raises(OracleError, match="valid evidence"):
        windows_module._validate_observer_evidence(diagnostics)

    _write_observer_evidence(diagnostics)
    black = encode_rgb_png(1024, 768, b"\x00\x00\x00" * (1024 * 768))
    (diagnostics / "screen-visual.png").write_bytes(black)
    with pytest.raises(OracleError, match="non-uniform"):
        windows_module._validate_observer_evidence(diagnostics)


def test_runtime_metadata_is_normalized_and_hardlinks_are_rejected(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _write_installed_windows(runtime)
    windows_module._normalize_runtime_metadata(runtime)
    first = windows_module._inventory_windows_runtime(runtime)
    assert {
        record["mtime_ns"] for record in first["entries"]
    } == {windows_module.NORMALIZED_RUNTIME_MTIME_NS}
    assert {record["mode"] for record in first["entries"]} <= {"0644", "0755"}

    (runtime / "SETUP.OK").touch()
    windows_module._normalize_runtime_metadata(runtime)
    assert windows_module._inventory_windows_runtime(runtime)["digest"] == first["digest"]

    (runtime / "HARDLINK.OK").hardlink_to(runtime / "SETUP.OK")
    with pytest.raises(OracleError, match="hard-linked"):
        windows_module._inventory_windows_runtime(runtime)


def test_windows_checkpoint_rejects_unverified_image_before_mutating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "oracle"
    monkeypatch.setattr(
        windows_module,
        "probe_recorded_image",
        lambda _record: {"status": "mismatch", "error": "wrong label"},
    )

    with pytest.raises(OracleError) as caught:
        bootstrap_windows_checkpoint(
            home,
            tmp_path / "media",
            _windows_media(),
            _image_record(),
        )

    assert caught.value.exit_code == EXIT_INTEGRITY
    assert not home.exists()


def test_windows_checkpoint_requires_guest_sentinel_and_reuses_verified_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "oracle"
    source = home / "cache" / "media" / ("e" * 64) / "source"
    source.mkdir(parents=True)
    calls = 0

    monkeypatch.setattr(
        windows_module,
        "ensure_flat_windows_media",
        lambda _home, _root, _media: (source, _flat_manifest()),
    )
    monkeypatch.setattr(
        windows_module,
        "_verify_flat_media_cache",
        lambda *_args, **_kwargs: _flat_manifest(),
    )
    monkeypatch.setattr(
        windows_module,
        "probe_recorded_image",
        lambda _record: {"status": "match"},
    )
    real_mkdtemp = windows_module.tempfile.mkdtemp

    def deterministic_mkdtemp(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | Path | None = None,
    ) -> str:
        if prefix is not None and prefix.startswith("bootstrap-windows-"):
            target = Path(dir) / f"{prefix}forced_name"
            target.mkdir()
            return str(target)
        return real_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)

    monkeypatch.setattr(windows_module.tempfile, "mkdtemp", deterministic_mkdtemp)

    def fake_run(invocation: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        command = list(invocation.command)  # type: ignore[attr-defined]
        mounts = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--mount"
        ]
        job_mount = next(value for value in mounts if "dst=/oracle/job" in value)
        job = Path(job_mount.split("src=", 1)[1].split(",dst=", 1)[0])
        _write_installed_windows(job / "runtime")
        _write_observer_evidence(job / "diagnostics")
        return {
            "command": ["podman", "run"],
            "exit_code": 0,
            "timed_out": False,
            "killed": False,
            "duration_seconds": 1.0,
        }

    monkeypatch.setattr(windows_module, "run_podman_bounded", fake_run)
    result = bootstrap_windows_checkpoint(
        home,
        tmp_path / "unused-media",
        _windows_media(),
        _image_record(),
    )

    assert result["schema"] == BOOTSTRAP_RESULT_SCHEMA
    assert result["status"] == "windows-install-candidate"
    assert result["checkpoint"]["schema"] == WINDOWS_CHECKPOINT_SCHEMA
    assert result["checkpoint"]["baseline_eligible"] is False
    assert result["checkpoint"]["printer_profile"] == "none"
    assert result["cache_reused"] is False
    assert result["evidence_job"]
    assert "_" in str(result["evidence_job"])
    assert result["promotion_state"] == "committed"
    assert calls == 1
    checkpoint = home / "cache" / "windows" / str(result["checkpoint_key"])
    assert (checkpoint / "pristine-c" / "WINDOWS" / "WIN.COM").is_file()
    assert (checkpoint / "runtime.json").is_file()

    again = bootstrap_windows_checkpoint(
        home,
        tmp_path / "unused-media",
        _windows_media(),
        _image_record(),
    )
    assert again["checkpoint"] == result["checkpoint"]
    assert again["cache_reused"] is True
    assert again["evidence_job"] == result["evidence_job"]
    assert calls == 1

    receipt = checkpoint / "evidence-receipt.json"
    receipt_payload = receipt.read_bytes()
    checkpoint.chmod(0o700)
    receipt.unlink()
    checkpoint.chmod(0o555)
    with pytest.raises(OracleError) as caught:
        bootstrap_windows_checkpoint(
            home,
            tmp_path / "unused-media",
            _windows_media(),
            _image_record(),
        )
    assert caught.value.exit_code == EXIT_INTEGRITY
    checkpoint.chmod(0o700)
    receipt.write_bytes(receipt_payload)
    receipt.chmod(0o444)
    checkpoint.chmod(0o555)

    evidence_job = home / "jobs" / str(result["evidence_job"])
    visual = evidence_job / "diagnostics" / "screen-visual.png"
    visual_payload = visual.read_bytes()
    visual.write_bytes(b"tampered")
    with pytest.raises(OracleError) as caught:
        bootstrap_windows_checkpoint(
            home,
            tmp_path / "unused-media",
            _windows_media(),
            _image_record(),
        )
    assert caught.value.exit_code == EXIT_INTEGRITY
    visual.write_bytes(visual_payload)

    runtime_manifest = checkpoint / "runtime.json"
    runtime_payload = runtime_manifest.read_bytes()
    manifest = windows_module.read_json_object(runtime_manifest)
    manifest["tree_file_count"] += 1
    checkpoint.chmod(0o700)
    windows_module.atomic_write_json(runtime_manifest, manifest)
    runtime_manifest.chmod(0o444)
    checkpoint.chmod(0o555)
    with pytest.raises(OracleError) as caught:
        bootstrap_windows_checkpoint(
            home,
            tmp_path / "unused-media",
            _windows_media(),
            _image_record(),
        )
    assert caught.value.exit_code == EXIT_INTEGRITY
    checkpoint.chmod(0o700)
    windows_module.atomic_write(runtime_manifest, runtime_payload)
    runtime_manifest.chmod(0o444)
    checkpoint.chmod(0o555)

    checkpoint.chmod(0o700)
    config = checkpoint / "dosbox-x.conf"
    config.chmod(0o600)
    config.write_text("tampered", encoding="utf-8")
    config.chmod(0o444)
    checkpoint.chmod(0o555)
    with pytest.raises(OracleError) as caught:
        bootstrap_windows_checkpoint(
            home,
            tmp_path / "unused-media",
            _windows_media(),
            _image_record(),
        )
    assert caught.value.exit_code == EXIT_INTEGRITY
    assert calls == 1


def test_windows_checkpoint_preserves_failure_when_exit_zero_has_no_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "oracle"
    source = home / "cache" / "media" / ("e" * 64) / "source"
    source.mkdir(parents=True)
    monkeypatch.setattr(
        windows_module,
        "ensure_flat_windows_media",
        lambda _home, _root, _media: (source, _flat_manifest()),
    )
    monkeypatch.setattr(
        windows_module,
        "_verify_flat_media_cache",
        lambda *_args, **_kwargs: _flat_manifest(),
    )
    monkeypatch.setattr(
        windows_module,
        "probe_recorded_image",
        lambda _record: {"status": "match"},
    )

    def fake_run(invocation: object, **_kwargs: object) -> dict[str, object]:
        command = list(invocation.command)  # type: ignore[attr-defined]
        mount = next(
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--mount" and "dst=/oracle/job" in command[index + 1]
        )
        job = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
        _write_installed_windows(job / "runtime", sentinel=False)
        _write_observer_evidence(job / "diagnostics")
        return {"exit_code": 0, "timed_out": False, "killed": False}

    monkeypatch.setattr(windows_module, "run_podman_bounded", fake_run)
    with pytest.raises(OracleError) as caught:
        bootstrap_windows_checkpoint(
            home,
            tmp_path / "unused-media",
            _windows_media(),
            _image_record(),
        )

    assert caught.value.exit_code == EXIT_BACKEND
    jobs = list((home / "jobs").iterdir())
    assert len(jobs) == 1
    assert (jobs[0] / "failure.json").is_file()
    failure = windows_module.read_json_object(jobs[0] / "failure.json")
    assert failure["process_result"]["exit_code"] == 0
    assert not any((home / "cache" / "windows").iterdir())
