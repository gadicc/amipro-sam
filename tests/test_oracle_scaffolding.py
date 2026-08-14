from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from amipro_oracle import cli as oracle_cli
from amipro_oracle import media as media_module
from amipro_oracle import oci as oci_module
from amipro_oracle import process as process_module
from amipro_oracle import toolchain as toolchain_module
from amipro_oracle.compare import compare_analyses
from amipro_oracle.config import dosbox_config
from amipro_oracle.constants import (
    ANALYSIS_SCHEMA,
    EXIT_BACKEND,
    EXIT_INTEGRITY,
    EXIT_MISSING,
    EXIT_TIMEOUT,
    JOB_SCHEMA,
    MEDIA_SCHEMA,
    RUNTIME_SCHEMA,
)
from amipro_oracle.errors import MediaIntegrityError, OracleError
from amipro_oracle.fake import run_fake_job
from amipro_oracle.io import sha256_file
from amipro_oracle.media import inventory_media
from amipro_oracle.oci import BindMount, build_podman_invocation, cleanup_podman_container
from amipro_oracle.process import run_bounded
from amipro_oracle.raster import encode_rgb_png
from amipro_oracle.state import StateMachine
from amipro_oracle.toolchain import probe_recorded_image

_DEFAULT_PIXELS = bytes((100, 100, 100, 100, 100, 100))


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_analysis(
    root: Path,
    *,
    text: str = "Alpha beta",
    box_text: str | None = None,
    box_offset: float = 0.0,
    pixels: bytes = _DEFAULT_PIXELS,
    extra_page: bool = False,
    include_text_box: bool = True,
) -> Path:
    root.mkdir()
    raster = root / "page-001.png"
    raster.write_bytes(encode_rgb_png(2, 1, pixels))
    page: dict[str, object] = {
        "number": 1,
        "width_pt": 612.0,
        "height_pt": 792.0,
        "text": text,
        "text_boxes": (
            [
                {
                    "text": box_text if box_text is not None else text,
                    "x0": 72.0 + box_offset,
                    "y0": 62.0,
                    "x1": 300.0,
                    "y1": 74.0,
                }
            ]
            if include_text_box
            else []
        ),
        "image_boxes": [],
        "raster": {"path": raster.name, "width": 2, "height": 1},
    }
    pages = [page]
    if extra_page:
        pages.append(
            {
                "number": 2,
                "width_pt": 612.0,
                "height_pt": 792.0,
                "text": "extra",
                "text_boxes": [],
                "image_boxes": [],
            }
        )
    analysis = {
        "schema": ANALYSIS_SCHEMA,
        "backend": "real",
        "profile": {"id": "test-profile", "whitespace": "collapse"},
        "page_count": len(pages),
        "pages": pages,
    }
    path = root / "analysis.json"
    path.write_text(json.dumps(analysis), encoding="utf-8")
    return path


def test_media_inventory_is_deterministic_and_does_not_mutate_sources(
    tmp_path: Path,
) -> None:
    media = tmp_path / "media"
    nested = media / "nested"
    nested.mkdir(parents=True)
    sources = {
        media / "B.bin": b"bravo",
        media / "a.bin": b"alpha",
        nested / "C.bin": b"charlie",
    }
    for path, payload in sources.items():
        path.write_bytes(payload)
        path.chmod(0o444)
    before = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
        for path in sources
    }

    first = inventory_media(media, kind="synthetic")
    second = inventory_media(media, kind="synthetic")

    assert first == second
    assert first["schema"] == MEDIA_SCHEMA
    assert first["file_count"] == 3
    assert first["total_bytes"] == sum(len(payload) for payload in sources.values())
    assert first["source_writable_files"] == 0
    assert [entry["path"] for entry in first["files"]] == [
        "a.bin",
        "B.bin",
        "nested/C.bin",
    ]
    assert [entry["sha256"] for entry in first["files"]] == [
        hashlib.sha256(sources[media / "a.bin"]).hexdigest(),
        hashlib.sha256(sources[media / "B.bin"]).hexdigest(),
        hashlib.sha256(sources[nested / "C.bin"]).hexdigest(),
    ]
    assert {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
        for path in sources
    } == before


def test_media_inventory_rejects_missing_symlink_and_special_entries(tmp_path: Path) -> None:
    with pytest.raises(OracleError) as missing:
        inventory_media(tmp_path / "missing", kind="synthetic")
    assert missing.value.exit_code == EXIT_MISSING

    tree = tmp_path / "tree"
    tree.mkdir()
    target = tmp_path / "target.bin"
    target.write_bytes(b"target")
    (tree / "alias.bin").symlink_to(target)
    with pytest.raises(MediaIntegrityError) as nested_link:
        inventory_media(tree, kind="synthetic")
    assert nested_link.value.exit_code == EXIT_INTEGRITY

    root_link = tmp_path / "root-link"
    root_link.symlink_to(tree, target_is_directory=True)
    with pytest.raises(MediaIntegrityError) as linked_root:
        inventory_media(root_link, kind="synthetic")
    assert linked_root.value.exit_code == EXIT_INTEGRITY

    special = tmp_path / "special"
    special.mkdir()
    os.mkfifo(special / "fifo")
    with pytest.raises(MediaIntegrityError) as special_entry:
        inventory_media(special, kind="synthetic")
    assert special_entry.value.exit_code == EXIT_INTEGRITY


def test_amipro_media_requires_a_recognized_owned_media_profile(tmp_path: Path) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "README.txt").write_text("not Ami Pro media", encoding="utf-8")

    with pytest.raises(MediaIntegrityError) as caught:
        inventory_media(unrelated, kind="amipro")

    assert caught.value.exit_code == EXIT_INTEGRITY
    assert "does not match" in str(caught.value)


def test_media_inventory_detects_mutation_while_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "disk.img"
    source.write_bytes(b"A" * 32)
    real_read = os.read
    mutated = False

    def adversarial_read(descriptor: int, count: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, count)
        if chunk and not mutated:
            mutated = True
            source.write_bytes(b"B" * 33)
        return chunk

    monkeypatch.setattr(media_module.os, "read", adversarial_read)
    with pytest.raises(MediaIntegrityError) as caught:
        inventory_media(source, kind="synthetic")

    assert mutated
    assert caught.value.exit_code == EXIT_INTEGRITY
    assert "changed while hashing" in str(caught.value)


def test_doctor_reports_missing_media_without_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "never-created"
    monkeypatch.delenv("WIN31_MEDIA_DIR", raising=False)
    monkeypatch.delenv("AMIPRO_MEDIA_DIR", raising=False)
    monkeypatch.setattr(
        oracle_cli,
        "oracle_home",
        lambda _value=None, **_kwargs: home,
    )
    monkeypatch.setattr(
        oracle_cli,
        "probe_toolchain",
        lambda: {
            "lock": {},
            "native": [],
            "oci_providers": [],
            "native_ready": False,
        },
    )

    exit_code = oracle_cli.main(["doctor", "--backend", "real", "--json"])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == EXIT_MISSING
    assert result["status"] == "blocked"
    assert result["mutated_state"] is False
    assert result["oracle_home_exists"] is False
    assert {issue["code"] for issue in result["issues"]} >= {
        "missing-win31-media",
        "missing-amipro-media",
    }
    assert not home.exists()


def test_local_env_loader_accepts_only_quoted_media_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_env = tmp_path / ".env.local"
    marker = tmp_path / "must-not-exist"
    local_env.write_text(
        "\n".join(
            (
                'WIN31_MEDIA_DIR="/media/Windows 3.1"',
                "export AMIPRO_MEDIA_DIR='/media/Ami Pro'",
                f"UNRELATED=$(touch {marker})",
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("WIN31_MEDIA_DIR", raising=False)
    monkeypatch.delenv("AMIPRO_MEDIA_DIR", raising=False)

    try:
        oracle_cli._load_local_env(local_env)

        assert os.environ["WIN31_MEDIA_DIR"] == "/media/Windows 3.1"
        assert os.environ["AMIPRO_MEDIA_DIR"] == "/media/Ami Pro"
        assert not marker.exists()
    finally:
        os.environ.pop("WIN31_MEDIA_DIR", None)
        os.environ.pop("AMIPRO_MEDIA_DIR", None)


def test_real_bootstrap_requires_rights_confirmation_and_dispatches_windows_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    windows_path = tmp_path / "windows"
    amipro_path = tmp_path / "amipro"
    monkeypatch.setattr(
        oracle_cli,
        "oracle_home",
        lambda _value=None, **_kwargs: home,
    )
    assert oracle_cli.main(["bootstrap", "--backend", "real", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2

    monkeypatch.setattr(
        oracle_cli,
        "_require_real_media",
        lambda _args: (windows_path, amipro_path),
    )
    monkeypatch.setattr(
        oracle_cli,
        "inventory_media",
        lambda _path, *, kind: {
            "kind": kind,
            "digest": "a" * 64 if kind == "windows-3.1" else "b" * 64,
        },
    )
    monkeypatch.setattr(oracle_cli, "_toolchain_image", lambda _home: {"image": True})
    monkeypatch.setattr(
        oracle_cli,
        "bootstrap_windows_checkpoint",
        lambda *_args: {
            "schema": "amipro-oracle-windows-checkpoint-v1",
            "status": "windows-install-candidate",
            "checkpoint_key": "c" * 64,
        },
    )

    assert (
        oracle_cli.main(
            [
                "bootstrap",
                "--backend",
                "real",
                "--confirm-proprietary-media-rights",
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "windows-install-candidate"
    assert result["amipro_media_validated"] is True
    assert result["next_phase"] == "program-manager-boot-probe"


def test_doctor_reports_recorded_image_mismatch_as_integrity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        oracle_cli,
        "oracle_home",
        lambda _value=None, **_kwargs: tmp_path / "home",
    )
    monkeypatch.setattr(
        oracle_cli,
        "probe_toolchain",
        lambda: {
            "lock": {},
            "native": [],
            "oci_providers": [],
            "native_ready": False,
        },
    )
    monkeypatch.setattr(oracle_cli, "_toolchain_image", lambda _home: {"record": True})
    monkeypatch.setattr(
        oracle_cli,
        "probe_recorded_image",
        lambda _record: {"status": "mismatch", "error": "image label mismatch"},
    )

    exit_code = oracle_cli.main(["doctor", "--backend", "real", "--json"])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == EXIT_INTEGRITY
    assert "invalid-locked-toolchain" in {
        issue["code"] for issue in result["issues"]
    }


def test_fake_bootstrap_smoke_and_batch_write_self_consistent_manifests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "oracle-home"
    monkeypatch.setattr(
        oracle_cli,
        "oracle_home",
        lambda _value=None, **_kwargs: home,
    )

    assert oracle_cli.main(["bootstrap", "--backend", "fake", "--json"]) == 0
    runtime_result = json.loads(capsys.readouterr().out)
    runtime_root = home / "cache" / "runtime" / runtime_result["runtime_key"]
    assert runtime_result["schema"] == RUNTIME_SCHEMA
    assert runtime_result["status"] == "ready"
    assert runtime_result["baseline_eligible"] is False
    assert _read_json(runtime_root / "runtime.json") == runtime_result
    assert (runtime_root / "dosbox-x.conf").is_file()

    source = tmp_path / "smoke.sam"
    source.write_bytes(b"[ver]\r\n\t4\r\n[edoc]\r\nsynthetic\r\n>\r\n")
    smoke_root = tmp_path / "smoke-output"
    assert (
        oracle_cli.main(
            [
                "smoke",
                "--backend",
                "fake",
                "--input",
                str(source),
                "--output",
                str(smoke_root),
                "--json",
            ]
        )
        == 0
    )
    smoke = json.loads(capsys.readouterr().out)
    assert smoke["schema"] == JOB_SCHEMA
    assert smoke["status"] == "success"
    assert smoke["baseline_eligible"] is False
    assert smoke["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert smoke["source"]["staged_name"] == "SMOKE.SAM"
    assert smoke["config_sha256"]
    assert smoke["toolchain"]["lock_sha256"]
    assert smoke["process_result"]["exit_code"] == 0
    assert smoke["process_result"]["timed_out"] is False
    assert [event["state"] for event in smoke["state_trace"]] == [
        "created",
        "staged",
        "guest-ready",
        "printed",
        "analyzed",
        "complete",
    ]
    assert _read_json(smoke_root / "job.json") == smoke
    for artifact in smoke["artifacts"]:
        artifact_path = smoke_root / artifact["path"]
        assert artifact_path.is_file()
        assert artifact["size"] == artifact_path.stat().st_size
        assert artifact["sha256"] == sha256_file(artifact_path)

    batch_input = tmp_path / "batch-input"
    (batch_input / "Nested").mkdir(parents=True)
    (batch_input / "zeta.SAM").write_bytes(b"zeta")
    (batch_input / "Nested" / "Alpha.sam").write_bytes(b"alpha")
    batch_root = tmp_path / "batch-output"
    assert (
        oracle_cli.main(
            [
                "batch",
                "--backend",
                "fake",
                "--input",
                str(batch_input),
                "--output",
                str(batch_root),
                "--json",
            ]
        )
        == 0
    )
    batch = json.loads(capsys.readouterr().out)
    assert batch["schema"] == "amipro-oracle-batch-v1"
    assert batch["status"] == "success"
    assert batch["baseline_eligible"] is False
    assert batch["document_count"] == 2
    assert batch["failure_count"] == 0
    assert batch["name_map"] == [
        {"source": "Nested/Alpha.sam", "guest": "DOC00001.SAM"},
        {"source": "zeta.SAM", "guest": "DOC00002.SAM"},
    ]
    assert _read_json(batch_root / "batch.json") == batch
    for job in batch["jobs"]:
        manifest = _read_json(batch_root / job["manifest"])
        assert manifest["schema"] == JOB_SCHEMA
        assert manifest["status"] == "success"
        assert manifest["source"]["staged_name"] == job["guest"]
        assert manifest["source"]["sha256"] == job["source_sha256"]


def test_fake_runtime_cache_poisoning_and_symlinks_exit_with_integrity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected_home = [tmp_path / "poisoned-home"]
    monkeypatch.setattr(
        oracle_cli,
        "oracle_home",
        lambda _value=None, **_kwargs: selected_home[0],
    )

    assert oracle_cli.main(["bootstrap", "--backend", "fake", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    first_runtime = (
        selected_home[0] / "cache" / "runtime" / first["runtime_key"]
    )
    (first_runtime / "runtime.json").write_text("{}", encoding="utf-8")
    assert (
        oracle_cli.main(["bootstrap", "--backend", "fake", "--json"])
        == EXIT_INTEGRITY
    )
    assert json.loads(capsys.readouterr().out)["exit_code"] == EXIT_INTEGRITY

    selected_home[0] = tmp_path / "symlinked-home"
    assert oracle_cli.main(["bootstrap", "--backend", "fake", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    second_runtime = (
        selected_home[0] / "cache" / "runtime" / second["runtime_key"]
    )
    config = second_runtime / "dosbox-x.conf"
    target = tmp_path / "copied-dosbox-x.conf"
    target.write_bytes(config.read_bytes())
    config.unlink()
    config.symlink_to(target)
    assert (
        oracle_cli.main(["bootstrap", "--backend", "fake", "--json"])
        == EXIT_INTEGRITY
    )
    assert json.loads(capsys.readouterr().out)["exit_code"] == EXIT_INTEGRITY


def test_fake_batch_rejects_case_insensitive_source_collisions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "A.sam").write_bytes(b"one")
    (source / "a.SAM").write_bytes(b"two")

    exit_code = oracle_cli.main(
        [
            "batch",
            "--backend",
            "fake",
            "--oracle-home",
            str(tmp_path / "home"),
            "--input",
            str(source),
            "--output",
            str(tmp_path / "output"),
        ]
    )

    assert exit_code == EXIT_INTEGRITY
    assert "case-insensitive guest" in capsys.readouterr().err
    assert not (tmp_path / "output").exists()


def test_fake_failure_retains_state_trace_and_partial_artifacts(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()

    with pytest.raises((OSError, OracleError)):
        run_fake_job(tmp_path / "missing.sam", job, staged_name="MISSING.SAM")

    failure = _read_json(job / "failure.json")
    assert failure["status"] == "failure"
    assert failure["baseline_eligible"] is False
    assert failure["state_trace"][0]["state"] == "created"
    assert isinstance(failure["artifacts"], list)
    assert str(tmp_path) not in (job / "failure.json").read_text(encoding="utf-8")


def test_generated_config_uses_non_overwriting_capture_directory() -> None:
    generated = dosbox_config()

    assert "captures=/oracle/job/capture" in generated
    assert "parallel1=file timeout:2000" in generated
    assert "file:" not in generated
    assert "append:" not in generated
    assert "openps:" not in generated
    assert "ipx=false" in generated
    assert "ne2000=false" in generated
    assert "backend=none" in generated
    assert "dos clipboard device enable=disabled" in generated
    assert "dos clipboard api=false" in generated
    assert "clip_mouse_button=none" in generated
    assert "startcmd=false" in generated
    assert "network redirector=false" in generated
    assert "automount=false" in generated
    assert "automountall=false" in generated
    assert "automount drive directories=false" in generated
    assert "synchronize time=false" in generated
    assert "[config]\ncountry=1" in generated
    assert "freesizecap=fixed" in generated


def test_toolchain_lock_hashes_its_build_inputs() -> None:
    lock = toolchain_module.load_lock()
    toolchain_root = toolchain_module.lock_path().parent

    assert sha256_file(toolchain_root / "Containerfile") == lock["containerfile_sha256"]
    assert (
        sha256_file(toolchain_root / "oracle-entrypoint")
        == lock["dosbox_x"]["entrypoint_sha256"]
    )


def test_recorded_image_probe_requires_current_lock_and_matching_image_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_sha256 = sha256_file(toolchain_module.lock_path())
    image_id = f"sha256:{'b' * 64}"
    image_digest = f"sha256:{'c' * 64}"
    record = {
        "provider": "podman",
        "image": "localhost/amipro-oracle-toolchain:2026.08.02-1",
        "image_id": image_id,
        "image_digest": image_digest,
        "lock_sha256": lock_sha256,
    }
    inspected_label = lock_sha256

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "info":
            return subprocess.CompletedProcess(command, 0, "true\n", "")
        assert command[1:3] == ["image", "inspect"]
        assert "org.amipro-oracle.toolchain-lock-sha256" in command[4]
        return subprocess.CompletedProcess(
            command,
            0,
            f"{image_id}\t{image_digest}\t{inspected_label}\n",
            "",
        )

    monkeypatch.setattr(toolchain_module.shutil, "which", lambda _name: "/usr/bin/podman")
    monkeypatch.setattr(toolchain_module.subprocess, "run", fake_run)

    matched = probe_recorded_image(record)
    assert matched["status"] == "match"
    assert matched["image_lock_sha256"] == lock_sha256
    assert matched["current_lock_sha256"] == lock_sha256

    inspected_label = "d" * 64
    wrong_label = probe_recorded_image(record)
    assert wrong_label["status"] == "mismatch"
    assert wrong_label["image_lock_sha256"] == "d" * 64

    record["lock_sha256"] = "e" * 64
    stale_record = probe_recorded_image(record)
    assert stale_record["status"] == "mismatch"
    assert stale_record["current_lock_sha256"] == lock_sha256


def test_native_probe_accepts_the_locked_dosbox_version_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "dosbox-x"
    executable.write_bytes(b"synthetic executable")
    monkeypatch.setattr(toolchain_module.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(
        toolchain_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            "DOSBox-X version 2026.08.02 SDL2\n",
            "",
        ),
    )

    accepted = toolchain_module._probe(
        "dosbox-x", ["--version"], "2026.08.02", None, 1
    )
    rejected = toolchain_module._probe(
        "dosbox-x", ["--version"], "2026.08.02", None, 0
    )

    assert accepted["status"] == "unverified"
    assert accepted["expected_exit_code"] == 1
    assert rejected["status"] == "mismatch"


def test_oci_invocation_is_rootless_networkless_and_mount_bounded(tmp_path: Path) -> None:
    oracle = tmp_path / "oracle"
    job = oracle / "jobs" / "job"
    control = oracle / "control"
    windows = tmp_path / "windows-media"
    amipro = tmp_path / "amipro-media"
    for directory in (job, control, windows, amipro):
        directory.mkdir(parents=True)
    record = {
        "schema": "amipro-oracle-image-v1",
        "provider": "podman",
        "platform": "linux/amd64",
        "image": "localhost/amipro-oracle-toolchain:2026.08.02-1",
        "image_digest": f"sha256:{'a' * 64}",
    }

    invocation = build_podman_invocation(
        record,
        container_name="amipro-oracle-test-1",
        oracle_root=oracle,
        job_root=job,
        control_root=control,
        phase="bootstrap",
        mounts=[
            BindMount(job, "/oracle/job", read_only=False),
            BindMount(windows, "/oracle/media/windows", read_only=True),
            BindMount(amipro, "/oracle/media/amipro", read_only=True),
        ],
        dosbox_arguments=["-defaultconf", "-conf", "/oracle/job/dosbox-x.conf"],
    )
    command = list(invocation.command)

    for required in (
        "--pull=never",
        "--network=none",
        "--read-only",
        "--read-only-tmpfs=false",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--userns=keep-id",
        "--ipc=private",
    ):
        assert required in command
    assert command.count("--mount") == 3
    mount_values = [
        command[index + 1] for index, value in enumerate(command) if value == "--mount"
    ]
    assert any("dst=/oracle/job,rw=true" in value for value in mount_values)
    assert all("ro=true" in value for value in mount_values if "dst=/oracle/media/" in value)
    assert command[-3:] == ["-defaultconf", "-conf", "/oracle/job/dosbox-x.conf"]
    assert invocation.cidfile == control / "amipro-oracle-test-1.cid"

    document = build_podman_invocation(
        record,
        container_name="amipro-oracle-test-2",
        oracle_root=oracle,
        job_root=job,
        control_root=control,
        phase="document",
        mounts=[BindMount(job, "/oracle/job", read_only=False)],
        dosbox_arguments=["-defaultconf"],
    )
    assert list(document.command).count("--mount") == 1

    with pytest.raises(OracleError, match="must not expose"):
        build_podman_invocation(
            record,
            container_name="amipro-oracle-test-3",
            oracle_root=oracle,
            job_root=job,
            control_root=control,
            phase="document",
            mounts=[
                BindMount(job, "/oracle/job", read_only=False),
                BindMount(amipro, "/oracle/media/amipro", read_only=True),
            ],
            dosbox_arguments=[],
        )

    with pytest.raises(OracleError, match="destination"):
        build_podman_invocation(
            record,
            container_name="amipro-oracle-test-4",
            oracle_root=oracle,
            job_root=job,
            control_root=control,
            phase="bootstrap",
            mounts=[
                BindMount(job, "/oracle/job", read_only=False),
                BindMount(amipro, "/oracle/../etc", read_only=True),
            ],
            dosbox_arguments=[],
        )

    repository = Path(__file__).resolve().parents[1]
    with pytest.raises(OracleError, match="narrow existing directories"):
        build_podman_invocation(
            record,
            container_name="amipro-oracle-test-5",
            oracle_root=oracle,
            job_root=job,
            control_root=control,
            phase="bootstrap",
            mounts=[
                BindMount(job, "/oracle/job", read_only=False),
                BindMount(repository, "/oracle/media/repository", read_only=True),
            ],
            dosbox_arguments=[],
        )

    with pytest.raises(OracleError, match="dedicated oracle root"):
        build_podman_invocation(
            record,
            container_name="amipro-oracle-test-6",
            oracle_root=repository,
            job_root=job,
            control_root=control,
            phase="document",
            mounts=[BindMount(job, "/oracle/job", read_only=False)],
            dosbox_arguments=[],
        )


def test_oci_cleanup_never_targets_a_name_without_a_new_cidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oracle = tmp_path / "oracle"
    job = oracle / "jobs" / "job"
    control = oracle / "control"
    job.mkdir(parents=True)
    control.mkdir()
    record = {
        "schema": "amipro-oracle-image-v1",
        "provider": "podman",
        "platform": "linux/amd64",
        "image": "localhost/amipro-oracle-toolchain:test",
        "image_digest": f"sha256:{'b' * 64}",
    }
    invocation = build_podman_invocation(
        record,
        container_name="amipro-oracle-collision-test",
        oracle_root=oracle,
        job_root=job,
        control_root=control,
        phase="document",
        mounts=[BindMount(job, "/oracle/job", read_only=False)],
        dosbox_arguments=[],
    )
    monkeypatch.setattr(
        oci_module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("cleanup must not call Podman without a cidfile"),
    )

    results = cleanup_podman_container(
        invocation,
        diagnostics_path=job / "cleanup.json",
    )

    assert results == [{"status": "skipped", "reason": "no new container cidfile"}]

def test_compare_accepts_bounded_geometry_whitespace_and_raster_differences(
    tmp_path: Path,
) -> None:
    expected = _write_analysis(
        tmp_path / "expected",
        text="Alpha\n beta",
        box_text="Alpha  beta",
    )
    actual = _write_analysis(
        tmp_path / "actual",
        text="Alpha beta",
        box_text="Alpha beta",
        box_offset=0.4,
        pixels=bytes((102, 100, 100, 100, 100, 100)),
    )

    report = compare_analyses(
        expected,
        actual,
        bbox_tolerance=0.5,
        raster_rmse=0.01,
        pixel_threshold=0.05,
        max_different_ratio=0.0,
    )

    assert report["equal"] is True
    assert report["baseline_eligible"] is False
    assert report["expected_provenance"] == "raw-analysis"
    assert report["issues"] == []
    assert report["rasters"][0]["dimensions_equal"] is True
    assert report["rasters"][0]["different_pixel_ratio"] == 0.0
    assert 0 < report["rasters"][0]["rmse"] < 0.01


def test_compare_reports_structural_and_out_of_tolerance_changes(tmp_path: Path) -> None:
    expected = _write_analysis(tmp_path / "expected", text="expected")
    actual = _write_analysis(
        tmp_path / "actual",
        text="actual",
        box_offset=1.0,
        pixels=bytes((255, 255, 255, 100, 100, 100)),
        extra_page=True,
        include_text_box=False,
    )

    report = compare_analyses(
        expected,
        actual,
        bbox_tolerance=0.5,
        raster_rmse=0.01,
        pixel_threshold=0.05,
        max_different_ratio=0.0,
    )
    codes = {issue["code"] for issue in report["issues"]}

    assert report["equal"] is False
    assert {"page-count", "page-text", "text-box-count", "page-raster"} <= codes


def test_compare_rejects_backend_mismatch_and_unverified_real_provenance(
    tmp_path: Path,
) -> None:
    expected = _write_analysis(tmp_path / "expected")
    actual = _write_analysis(tmp_path / "actual")
    actual_value = _read_json(actual)
    actual_value["backend"] = "fake"
    actual.write_text(json.dumps(actual_value), encoding="utf-8")

    mismatch = compare_analyses(expected, actual)
    assert mismatch["equal"] is False
    assert {issue["code"] for issue in mismatch["issues"]} == {"backend-mismatch"}

    source = tmp_path / "source.sam"
    source.write_bytes(b"synthetic")
    job = tmp_path / "job"
    job.mkdir()
    run_fake_job(source, job, staged_name="INPUT.SAM")
    job_manifest = _read_json(job / "job.json")
    analysis_path = job / str(job_manifest["analysis_path"])
    analysis = _read_json(analysis_path)
    analysis["backend"] = "real"
    analysis_path.write_text(json.dumps(analysis), encoding="utf-8")
    for artifact in job_manifest["artifacts"]:
        if artifact["kind"] == "analysis":
            artifact["size"] = analysis_path.stat().st_size
            artifact["sha256"] = sha256_file(analysis_path)
    job_manifest.update(
        {
            "backend": "real",
            "baseline_eligible": True,
            "media": {
                "windows": {"profile": "test-windows", "digest": "a" * 64},
            },
            "runtime": {"key": "b" * 64, "manifest_sha256": "c" * 64},
            "toolchain": {
                "lock_sha256": "d" * 64,
                "image_digest": f"sha256:{'e' * 64}",
            },
            "config_sha256": "f" * 64,
        }
    )
    (job / "job.json").write_text(json.dumps(job_manifest), encoding="utf-8")
    incomplete = compare_analyses(job, job)
    assert incomplete["equal"] is True
    assert incomplete["baseline_eligible"] is False

    job_manifest["media"]["amipro"] = {
        "profile": "test-amipro",
        "digest": "0" * 64,
    }
    (job / "job.json").write_text(json.dumps(job_manifest), encoding="utf-8")
    unverified = compare_analyses(job, job)
    assert unverified["equal"] is True
    assert unverified["baseline_eligible"] is False


def test_compare_rejects_non_finite_geometry_and_tampered_job_artifacts(
    tmp_path: Path,
) -> None:
    non_finite = _write_analysis(tmp_path / "non-finite")
    value = _read_json(non_finite)
    value["pages"][0]["width_pt"] = float("nan")
    non_finite.write_text(json.dumps(value), encoding="utf-8")
    normal = _write_analysis(tmp_path / "normal")
    with pytest.raises(ValueError, match="(finite|non-standard JSON)"):
        compare_analyses(non_finite, normal)

    source = tmp_path / "source.sam"
    source.write_bytes(b"synthetic")
    job = tmp_path / "job"
    job.mkdir()
    run_fake_job(source, job, staged_name="INPUT.SAM")
    (job / "output" / "analysis.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="(size|hash) mismatch"):
        compare_analyses(job, job)


def test_state_machine_records_evidence_and_rejects_invalid_transitions() -> None:
    machine = StateMachine(
        initial="created",
        terminal=frozenset({"complete"}),
        transitions={
            "created": frozenset({"running"}),
            "running": frozenset({"complete"}),
        },
    )

    assert machine.complete is False
    machine.advance("running", evidence="readiness sentinel")
    machine.advance("complete", evidence="process reaped")
    assert machine.complete is True
    assert machine.trace[1]["evidence"] == "readiness sentinel"
    assert machine.trace[2]["evidence"] == "process reaped"
    assert all(event["elapsed_seconds"] >= 0 for event in machine.trace)

    with pytest.raises(OracleError) as caught:
        machine.advance("running")
    assert caught.value.exit_code == EXIT_BACKEND


def test_bounded_process_captures_output_and_raises_stable_timeout(
    tmp_path: Path,
) -> None:
    stdout = tmp_path / "success" / "stdout.log"
    stderr = tmp_path / "success" / "stderr.log"
    result = run_bounded(
        [
            sys.executable,
            "-c",
            "import sys; print('standard output'); print('standard error', file=sys.stderr)",
        ],
        stdout_path=stdout,
        stderr_path=stderr,
        timeout_seconds=5,
    )
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert stdout.read_text(encoding="utf-8").strip() == "standard output"
    assert stderr.read_text(encoding="utf-8").strip() == "standard error"

    timeout_stdout = tmp_path / "timeout" / "stdout.log"
    timeout_stderr = tmp_path / "timeout" / "stderr.log"
    with pytest.raises(OracleError) as caught:
        run_bounded(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout_path=timeout_stdout,
            stderr_path=timeout_stderr,
            timeout_seconds=0.1,
            grace_seconds=0.1,
        )

    assert caught.value.exit_code == EXIT_TIMEOUT
    process_result = caught.value.process_result
    assert process_result["timed_out"] is True
    assert process_result["command"][0] == sys.executable
    assert timeout_stdout.is_file()
    assert timeout_stderr.is_file()


def test_bounded_process_truncates_logs_and_enforces_writable_tree_quota(
    tmp_path: Path,
) -> None:
    stdout = tmp_path / "truncated.stdout"
    stderr = tmp_path / "truncated.stderr"
    result = run_bounded(
        [
            sys.executable,
            "-c",
            "import sys; print('A' * 5000); print('B' * 5000, file=sys.stderr)",
        ],
        stdout_path=stdout,
        stderr_path=stderr,
        timeout_seconds=5,
        max_output_bytes=1024,
    )
    assert result["stdout_capture"]["truncated"] is True
    assert result["stderr_capture"]["truncated"] is True
    assert b"amipro-oracle omitted" in stdout.read_bytes()
    assert stdout.stat().st_size < 1200

    watched = tmp_path / "watched"
    watched.mkdir()
    with pytest.raises(OracleError) as caught:
        run_bounded(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import time; "
                    f"Path({str(watched / 'large.bin')!r}).write_bytes(b'X' * 8192); "
                    "time.sleep(5)"
                ),
            ],
            stdout_path=tmp_path / "quota.stdout",
            stderr_path=tmp_path / "quota.stderr",
            timeout_seconds=5,
            watch_path=watched,
            max_tree_bytes=1024,
        )
    assert caught.value.exit_code == EXIT_INTEGRITY
    assert caught.value.process_result["timed_out"] is False

    final_burst = tmp_path / "final-burst"
    final_burst.mkdir()
    with pytest.raises(OracleError) as caught:
        run_bounded(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(final_burst / 'large.bin')!r})"
                    ".write_bytes(b'X' * 8192)"
                ),
            ],
            stdout_path=tmp_path / "final-burst.stdout",
            stderr_path=tmp_path / "final-burst.stderr",
            timeout_seconds=5,
            watch_path=final_burst,
            max_tree_bytes=1024,
        )
    assert caught.value.exit_code == EXIT_INTEGRITY
    assert caught.value.process_result["final_tree_bytes"] >= 8192


def test_writable_tree_sampler_tolerates_concurrent_create_unlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "churn"
    root.mkdir()
    stopped = threading.Event()

    def churn() -> None:
        path = root / "temporary"
        while not stopped.is_set():
            path.write_bytes(b"x")
            path.unlink(missing_ok=True)

    worker = threading.Thread(target=churn)
    worker.start()
    try:
        for _ in range(2000):
            size, entries = process_module._bounded_tree_usage(root, 100)
            assert size >= 0
            assert entries >= 0
    finally:
        stopped.set()
        worker.join(timeout=2)
