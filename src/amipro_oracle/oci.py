from __future__ import annotations

import math
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import EXIT_BACKEND, EXIT_INTEGRITY, EXIT_MISSING
from .errors import OracleError
from .io import atomic_write_json
from .paths import repo_root
from .process import run_bounded

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_INSTANCE = re.compile(r"amipro-oracle-[a-z0-9][a-z0-9-]{0,62}\Z")
_ANALYSIS_ENTRYPOINTS = frozenset(
    {
        "/usr/bin/gs",
        "/usr/bin/pdfinfo",
        "/usr/bin/pdffonts",
        "/usr/bin/pdftocairo",
        "/usr/bin/pdftotext",
    }
)


@dataclass(frozen=True)
class BindMount:
    source: Path
    destination: str
    read_only: bool


@dataclass(frozen=True)
class PodmanInvocation:
    command: tuple[str, ...]
    container_name: str
    cidfile: Path
    job_root: Path


def _mount_argument(mount: BindMount) -> str:
    supplied_source = mount.source.expanduser()
    if supplied_source.is_symlink():
        raise OracleError(
            f"OCI mount source must not be a symlink: {supplied_source}",
            exit_code=EXIT_INTEGRITY,
        )
    source = supplied_source.resolve(strict=True)
    if not source.is_dir() or source in {
        Path("/"),
        Path.home().resolve(),
        repo_root(),
    }:
        raise OracleError(
            f"OCI mounts must be narrow existing directories: {source}",
            exit_code=EXIT_INTEGRITY,
        )
    if any(character in str(source) for character in (",", "\n", "\r", "\x00")):
        raise OracleError(
            f"OCI mount path is not representable: {source}", exit_code=EXIT_INTEGRITY
        )
    destination = PurePosixPath(mount.destination)
    if (
        not destination.is_absolute()
        or ".." in destination.parts
        or destination.as_posix() != mount.destination
        or destination == PurePosixPath("/oracle")
        or PurePosixPath("/oracle") not in destination.parents
        or any(character in mount.destination for character in (",", "\n", "\r", "\x00"))
    ):
        raise OracleError(
            f"OCI mount destination must be below /oracle: {mount.destination!r}",
            exit_code=EXIT_INTEGRITY,
        )
    mode = "ro=true" if mount.read_only else "rw=true"
    return f"type=bind,src={source},dst={destination.as_posix()},{mode}"


def build_podman_invocation(
    image_record: dict[str, Any],
    *,
    container_name: str,
    oracle_root: Path,
    job_root: Path,
    control_root: Path,
    phase: str,
    mounts: list[BindMount],
    dosbox_arguments: list[str],
    entrypoint: str | None = None,
) -> PodmanInvocation:
    if image_record.get("schema") != "amipro-oracle-image-v1":
        raise OracleError("invalid OCI image record schema", exit_code=EXIT_INTEGRITY)
    if image_record.get("provider") != "podman":
        raise OracleError(
            "only the locked rootless Podman provider is supported", exit_code=EXIT_MISSING
        )
    if image_record.get("platform") != "linux/amd64":
        raise OracleError("the locked OCI platform must be linux/amd64", exit_code=EXIT_INTEGRITY)
    image = image_record.get("image")
    digest = image_record.get("image_digest")
    if (
        not isinstance(image, str)
        or not image
        or "@" in image
        or any(character.isspace() or character == "\x00" for character in image)
        or not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
    ):
        raise OracleError("invalid locked OCI image identity", exit_code=EXIT_INTEGRITY)
    if _INSTANCE.fullmatch(container_name) is None:
        raise OracleError(
            "container name must match amipro-oracle-[a-z0-9-]",
            exit_code=EXIT_INTEGRITY,
        )
    if not mounts:
        raise OracleError("OCI invocation requires explicit mounts", exit_code=EXIT_INTEGRITY)
    if entrypoint is not None and entrypoint not in _ANALYSIS_ENTRYPOINTS:
        raise OracleError("unsupported OCI analysis entrypoint", exit_code=EXIT_INTEGRITY)

    mount_arguments = [_mount_argument(mount) for mount in mounts]
    destinations = [mount.destination for mount in mounts]
    if len(destinations) != len(set(destinations)):
        raise OracleError("OCI mount destinations must be unique", exit_code=EXIT_INTEGRITY)
    writable = [mount.destination for mount in mounts if not mount.read_only]
    if writable != ["/oracle/job"]:
        raise OracleError(
            "the disposable /oracle/job directory must be the only writable bind mount",
            exit_code=EXIT_INTEGRITY,
        )
    extra_mounts = [mount for mount in mounts if mount.destination != "/oracle/job"]
    if phase == "document" and extra_mounts:
        raise OracleError(
            "document execution must not expose source media or cache mounts",
            exit_code=EXIT_INTEGRITY,
        )
    if phase == "bootstrap" and any(
        not mount.read_only or not mount.destination.startswith("/oracle/media/")
        for mount in extra_mounts
    ):
        raise OracleError(
            "bootstrap extras must be read-only mounts below /oracle/media",
            exit_code=EXIT_INTEGRITY,
        )
    if phase not in {"bootstrap", "document"}:
        raise OracleError(f"unsupported OCI phase: {phase!r}", exit_code=EXIT_INTEGRITY)

    if any(
        path.expanduser().is_symlink() for path in (oracle_root, job_root, control_root)
    ):
        raise OracleError("oracle/job/control roots must not be symlinks", exit_code=EXIT_INTEGRITY)
    resolved_oracle = oracle_root.expanduser().resolve(strict=True)
    if resolved_oracle in {Path("/"), Path.home().resolve(), repo_root()}:
        raise OracleError("OCI state requires a dedicated oracle root", exit_code=EXIT_INTEGRITY)
    resolved_job = job_root.expanduser().resolve(strict=True)
    job_mount = next(mount for mount in mounts if mount.destination == "/oracle/job")
    if job_mount.source.expanduser().resolve(strict=True) != resolved_job:
        raise OracleError(
            "job_root must be the writable /oracle/job mount",
            exit_code=EXIT_INTEGRITY,
        )
    resolved_control = control_root.expanduser().resolve(strict=True)
    job_namespaces = (
        resolved_oracle / "jobs",
        resolved_oracle / "private-comparisons",
    )
    controls_root = resolved_oracle / "control"
    if not any(namespace in resolved_job.parents for namespace in job_namespaces) or (
        resolved_control != controls_root and controls_root not in resolved_control.parents
    ):
        raise OracleError(
            "job/control roots must stay below a private oracle job namespace and control",
            exit_code=EXIT_INTEGRITY,
        )
    if (
        resolved_control == resolved_job
        or resolved_control in resolved_job.parents
        or resolved_job in resolved_control.parents
    ):
        raise OracleError(
            "container control files must be outside the guest-writable job tree",
            exit_code=EXIT_INTEGRITY,
        )
    cidfile = resolved_control / f"{container_name}.cid"
    if cidfile.exists() or cidfile.is_symlink():
        raise OracleError(f"container cidfile already exists: {cidfile}", exit_code=EXIT_INTEGRITY)

    for argument in dosbox_arguments:
        if not isinstance(argument, str) or "\x00" in argument:
            raise OracleError("invalid DOSBox-X argument", exit_code=EXIT_INTEGRITY)

    command = [
        "podman",
        "run",
        "--rm",
        "--pull=never",
        "--platform=linux/amd64",
        "--network=none",
        "--read-only",
        "--read-only-tmpfs=false",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=1g",
        "--memory-swap=1g",
        "--cpus=1",
        "--userns=keep-id",
        "--ipc=private",
        "--hostname=amipro-oracle",
        f"--name={container_name}",
        f"--label=org.amipro-oracle.instance={container_name}",
        f"--cidfile={cidfile}",
        "--env=HOME=/oracle/job/home",
        "--env=LANG=C.UTF-8",
        "--env=LC_ALL=C.UTF-8",
        "--env=TZ=UTC",
        "--env=DISPLAY=:99",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=128m",
        "--tmpfs=/run:rw,nosuid,nodev,noexec,size=16m",
        "--tmpfs=/dev/shm:rw,nosuid,nodev,noexec,size=64m",
    ]
    for argument in mount_arguments:
        command.extend(("--mount", argument))
    if entrypoint is not None:
        command.append(f"--entrypoint={entrypoint}")
    command.append(f"{image}@{digest}")
    command.extend(dosbox_arguments)
    return PodmanInvocation(tuple(command), container_name, cidfile, resolved_job)


def exec_podman_checked(
    invocation: PodmanInvocation,
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: float = 5,
) -> dict[str, object]:
    if (
        not command
        or isinstance(command, (str, bytes))
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 30
        or _INSTANCE.fullmatch(invocation.container_name) is None
        or any(
            not isinstance(argument, str) or not argument or "\x00" in argument
            for argument in command
        )
    ):
        raise OracleError("invalid bounded Podman exec command", exit_code=EXIT_INTEGRITY)
    environment = environment or {}
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None
        or "\x00" in value
        for key, value in environment.items()
    ):
        raise OracleError("invalid bounded Podman exec environment", exit_code=EXIT_INTEGRITY)
    executable = shutil.which("podman")
    if executable is None:
        raise OracleError("rootless Podman is required", exit_code=EXIT_MISSING)
    if invocation.cidfile.is_symlink() or not invocation.cidfile.is_file():
        raise OracleError("container cidfile is not available", exit_code=EXIT_BACKEND)
    if invocation.cidfile.stat().st_size > 128:
        raise OracleError("container cidfile is oversized", exit_code=EXIT_INTEGRITY)
    try:
        container_id = invocation.cidfile.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise OracleError("cannot read container cidfile", exit_code=EXIT_INTEGRITY) from exc
    if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
        raise OracleError("container cidfile is invalid", exit_code=EXIT_INTEGRITY)
    try:
        exists = subprocess.run(
            [executable, "container", "exists", container_id],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
        inspect = subprocess.run(
            [
                executable,
                "inspect",
                "--format",
                '{{index .Config.Labels "org.amipro-oracle.instance"}}',
                container_id,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OracleError(
            f"cannot verify running container identity: {exc}",
            exit_code=EXIT_BACKEND,
        ) from exc
    if exists.returncode != 0 or inspect.returncode != 0:
        raise OracleError("oracle container is no longer running", exit_code=EXIT_BACKEND)
    if inspect.stdout.strip() != invocation.container_name:
        raise OracleError("oracle container identity label mismatch", exit_code=EXIT_INTEGRITY)
    exec_command = [executable, "exec"]
    exec_command.extend(f"--env={key}={value}" for key, value in sorted(environment.items()))
    exec_command.extend((container_id, *command))
    try:
        process = subprocess.run(
            exec_command,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OracleError(
            f"bounded Podman exec failed: {exc}",
            exit_code=EXIT_BACKEND,
        ) from exc
    return {
        "command": ["podman", "exec", *command],
        "exit_code": process.returncode,
        "stdout": process.stdout[:4000],
        "stderr": process.stderr[:4000],
    }


def cleanup_podman_container(
    invocation: PodmanInvocation,
    *,
    diagnostics_path: Path,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    if invocation.cidfile.is_symlink() or not invocation.cidfile.is_file():
        results.append({"status": "skipped", "reason": "no new container cidfile"})
        atomic_write_json(diagnostics_path, {"cleanup": results})
        return results
    executable = shutil.which("podman")
    if executable is None:
        results.append({"status": "skipped", "reason": "Podman disappeared before cleanup"})
        atomic_write_json(diagnostics_path, {"cleanup": results})
        return results
    container_id = invocation.cidfile.read_text(encoding="ascii").strip()
    if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
        results.append({"status": "skipped", "reason": "invalid container cidfile"})
        atomic_write_json(diagnostics_path, {"cleanup": results})
        return results
    try:
        exists = subprocess.run(
            [executable, "container", "exists", container_id],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
        if exists.returncode == 1:
            results.append({"status": "absent", "container_id": container_id})
            atomic_write_json(diagnostics_path, {"cleanup": results})
            return results
        if exists.returncode != 0:
            results.append(
                {
                    "status": "skipped",
                    "reason": "cannot determine whether the container still exists",
                    "exit_code": exists.returncode,
                    "output": exists.stdout[:4000],
                }
            )
            atomic_write_json(diagnostics_path, {"cleanup": results})
            return results
        inspect = subprocess.run(
            [
                executable,
                "inspect",
                "--format",
                "{{index .Config.Labels \"org.amipro-oracle.instance\"}}",
                container_id,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        results.append({"status": "skipped", "reason": f"container inspect failed: {exc}"})
        atomic_write_json(diagnostics_path, {"cleanup": results})
        return results
    if inspect.returncode != 0 or inspect.stdout.strip() != invocation.container_name:
        results.append(
            {
                "status": "skipped",
                "reason": "container identity label did not match",
                "exit_code": inspect.returncode,
                "output": inspect.stdout[:4000],
            }
        )
        atomic_write_json(diagnostics_path, {"cleanup": results})
        return results
    actions = (
        ("stop", "--time", "2", container_id),
        ("kill", container_id),
        ("rm", "--force", container_id),
    )
    for action in actions:
        try:
            process = subprocess.run(
                [executable, *action],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
            )
            results.append(
                {
                    "command": ["podman", *action],
                    "exit_code": process.returncode,
                    "output": process.stdout[:4000],
                }
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({"command": ["podman", *action], "error": str(exc)})
    atomic_write_json(diagnostics_path, {"cleanup": results})
    return results


def run_podman_bounded(
    invocation: PodmanInvocation,
    *,
    stdout_path: Path,
    stderr_path: Path,
    cleanup_path: Path,
    timeout_seconds: float,
    grace_seconds: float = 2.0,
) -> dict[str, object]:
    executable = shutil.which("podman")
    if executable is None:
        raise OracleError("rootless Podman is required", exit_code=EXIT_MISSING)
    try:
        info = subprocess.run(
            [executable, "info", "--format", "{{.Host.Security.Rootless}}"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OracleError(f"cannot verify rootless Podman: {exc}", exit_code=EXIT_MISSING) from exc
    if info.returncode != 0 or info.stdout.strip() != "true":
        raise OracleError("Podman is unavailable or is not rootless", exit_code=EXIT_MISSING)
    command = (executable, *invocation.command[1:])
    try:
        result = run_bounded(
            command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=timeout_seconds,
            grace_seconds=grace_seconds,
            watch_path=invocation.job_root,
        )
    except BaseException as exc:
        cleanup = cleanup_podman_container(invocation, diagnostics_path=cleanup_path)
        if isinstance(exc, OracleError) and exc.process_result is not None:
            exc.process_result["container_cleanup"] = cleanup
        raise
    cleanup = cleanup_podman_container(invocation, diagnostics_path=cleanup_path)
    result["container_cleanup"] = cleanup
    return result
