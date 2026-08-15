from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .amipro_install import (
    OUTER_TIME_LIMIT_SECONDS as AMIPRO_INSTALL_TIME_LIMIT_SECONDS,
)
from .amipro_install import install_amipro_checkpoint
from .amipro_launch_probe import (
    OUTER_TIME_LIMIT_SECONDS as AMIPRO_LAUNCH_TIME_LIMIT_SECONDS,
)
from .amipro_launch_probe import launch_amipro_ready
from .batch import (
    DEFAULT_DOCUMENT_TIMEOUT_SECONDS,
    MAX_DOCUMENT_TIMEOUT_SECONDS,
    batch_coordinator_lock,
    read_batch_status,
    run_real_batch,
)
from .compare import compare_analyses
from .constants import (
    COMPARE_SCHEMA,
    EXIT_BACKEND,
    EXIT_DIFFERENT,
    EXIT_INTEGRITY,
    EXIT_MISSING,
    EXIT_OK,
    EXIT_USAGE,
    VERSION,
)
from .document_smoke import (
    OUTER_TIME_LIMIT_SECONDS as DOCUMENT_SMOKE_TIME_LIMIT_SECONDS,
)
from .document_smoke import smoke_document
from .errors import OracleError
from .fake import run_fake_job
from .io import atomic_write_json, digest_json, read_json_object, sha256_file
from .media import inventory_media
from .native_batch import print_native_document, validate_native_batch_prerequisites
from .paths import oracle_home, repo_root
from .postscript_smoke import (
    OUTER_TIME_LIMIT_SECONDS as POSTSCRIPT_SMOKE_TIME_LIMIT_SECONDS,
)
from .postscript_smoke import print_smoke
from .printer_install import (
    OUTER_TIME_LIMIT_SECONDS as PRINTER_INSTALL_TIME_LIMIT_SECONDS,
)
from .printer_install import install_printer_ready
from .runtime import bootstrap_fake
from .toolchain import probe_recorded_image, probe_toolchain
from .windows_boot_probe import OUTER_TIME_LIMIT_SECONDS, boot_windows_ready
from .windows_bootstrap import bootstrap_windows_checkpoint

_LOCAL_ENV_KEYS = frozenset({"WIN31_MEDIA_DIR", "AMIPRO_MEDIA_DIR"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amipro-oracle",
        description="Build and run a local, isolated Ami Pro 3.1 rendering oracle.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="inspect prerequisites without changing state")
    _common(doctor, backend=True)
    doctor.add_argument("--win31-media", type=Path)
    doctor.add_argument("--amipro-media", type=Path)
    doctor.set_defaults(handler=_command_doctor)

    bootstrap = subparsers.add_parser(
        "bootstrap", help="validate media and build a content-addressed runtime"
    )
    _common(bootstrap, backend=True)
    bootstrap.add_argument("--win31-media", type=Path)
    bootstrap.add_argument("--amipro-media", type=Path)
    bootstrap.add_argument(
        "--confirm-proprietary-media-rights",
        action="store_true",
        help="affirm that you have the right to use the supplied local proprietary media",
    )
    bootstrap.set_defaults(handler=_command_bootstrap)

    boot_probe = subparsers.add_parser(
        "boot-probe",
        help="prove the installed Windows candidate reaches and exits Program Manager",
    )
    _common(boot_probe, backend=False)
    boot_probe.add_argument("--checkpoint-key")
    boot_probe.add_argument(
        "--timeout-seconds",
        type=float,
        default=OUTER_TIME_LIMIT_SECONDS,
        help=f"outer wall-clock deadline (maximum {OUTER_TIME_LIMIT_SECONDS}s)",
    )
    boot_probe.add_argument(
        "--confirm-proprietary-media-rights",
        action="store_true",
        help="affirm your right to use the cached runtime made from proprietary media",
    )
    boot_probe.set_defaults(handler=_command_boot_probe)

    install_amipro = subparsers.add_parser(
        "install-amipro",
        help="install Ami Pro into a disposable clone of a Windows-ready runtime",
    )
    _common(install_amipro, backend=False)
    install_amipro.add_argument("--amipro-media", type=Path)
    install_amipro.add_argument("--runtime-key")
    install_amipro.add_argument(
        "--timeout-seconds",
        type=float,
        default=AMIPRO_INSTALL_TIME_LIMIT_SECONDS,
        help=(
            "outer wall-clock deadline "
            f"(maximum {AMIPRO_INSTALL_TIME_LIMIT_SECONDS}s)"
        ),
    )
    install_amipro.add_argument(
        "--confirm-proprietary-media-rights",
        action="store_true",
        help="affirm your right to use the supplied local proprietary media",
    )
    install_amipro.set_defaults(handler=_command_install_amipro)

    launch_amipro = subparsers.add_parser(
        "launch-amipro",
        help="prove Ami Pro reaches its editor and exits cleanly",
    )
    _common(launch_amipro, backend=False)
    launch_amipro.add_argument("--checkpoint-key")
    launch_amipro.add_argument(
        "--timeout-seconds",
        type=float,
        default=AMIPRO_LAUNCH_TIME_LIMIT_SECONDS,
        help=(
            "outer wall-clock deadline "
            f"(maximum {AMIPRO_LAUNCH_TIME_LIMIT_SECONDS}s)"
        ),
    )
    launch_amipro.add_argument(
        "--confirm-proprietary-media-rights",
        action="store_true",
        help="affirm your right to use the cached runtime made from proprietary media",
    )
    launch_amipro.set_defaults(handler=_command_launch_amipro)

    smoke = subparsers.add_parser("smoke", help="run one invented-document lifecycle smoke test")
    _common(smoke, backend=True)
    smoke.add_argument(
        "--input", type=Path, help="invented SAM fixture; defaults to repository fixture"
    )
    smoke.add_argument("--output", type=Path, help="new fake-backend job directory")
    smoke.add_argument("--runtime-key")
    smoke.add_argument(
        "--timeout-seconds",
        type=float,
        default=DOCUMENT_SMOKE_TIME_LIMIT_SECONDS,
        help=(
            "outer wall-clock deadline "
            f"(maximum {DOCUMENT_SMOKE_TIME_LIMIT_SECONDS}s)"
        ),
    )
    smoke.add_argument(
        "--confirm-proprietary-media-rights",
        action="store_true",
        help="affirm your right to use the cached runtime made from proprietary media",
    )
    smoke.set_defaults(handler=_command_smoke)

    install_printer = subparsers.add_parser(
        "install-printer",
        help="install and lock the Windows PostScript printer profile",
    )
    _common(install_printer, backend=False)
    install_printer.add_argument("--win31-media", type=Path)
    install_printer.add_argument("--runtime-key")
    install_printer.add_argument(
        "--timeout-seconds",
        type=float,
        default=PRINTER_INSTALL_TIME_LIMIT_SECONDS,
        help=(
            "outer wall-clock deadline "
            f"(maximum {PRINTER_INSTALL_TIME_LIMIT_SECONDS}s)"
        ),
    )
    install_printer.add_argument(
        "--confirm-proprietary-media-rights",
        action="store_true",
        help="affirm your right to use the supplied local proprietary media",
    )
    install_printer.set_defaults(handler=_command_install_printer)

    print_smoke_parser = subparsers.add_parser(
        "print-smoke",
        help="print one invented SAM and derive PostScript/PDF/PNG analysis",
    )
    _common(print_smoke_parser, backend=False)
    print_smoke_parser.add_argument(
        "--input",
        type=Path,
        help="invented SAM fixture; defaults to repository fixture",
    )
    print_smoke_parser.add_argument("--runtime-key")
    print_smoke_parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=POSTSCRIPT_SMOKE_TIME_LIMIT_SECONDS,
        help=(
            "outer wall-clock deadline "
            f"(maximum {POSTSCRIPT_SMOKE_TIME_LIMIT_SECONDS}s)"
        ),
    )
    print_smoke_parser.add_argument(
        "--confirm-proprietary-media-rights",
        action="store_true",
        help="affirm your right to use the cached runtime made from proprietary media",
    )
    print_smoke_parser.set_defaults(handler=_command_print_smoke)

    batch = subparsers.add_parser("batch", help="process a directory of SAM files")
    _common(batch, backend=True)
    batch.add_argument("--input", type=Path, required=True)
    batch.add_argument("--output", type=Path, required=True)
    batch.add_argument("--runtime-key")
    batch.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_DOCUMENT_TIMEOUT_SECONDS,
        help=f"per-document wall-clock deadline (maximum {MAX_DOCUMENT_TIMEOUT_SECONDS}s)",
    )
    batch.add_argument(
        "--resume",
        action="store_true",
        help="verify the saved plan and continue incomplete or failed documents",
    )
    batch.add_argument(
        "--progress",
        action="store_true",
        help="emit privacy-safe per-document progress lines to stderr",
    )
    batch.add_argument(
        "--require-installed-fonts",
        action="store_true",
        help=(
            "block documents whose requested fonts are not installed, explicitly aliased, "
            "or enumerated as printer-device fonts"
        ),
    )
    batch.add_argument(
        "--confirm-proprietary-media-rights",
        action="store_true",
        help="affirm your right to use the cached runtime made from proprietary media",
    )
    batch.set_defaults(handler=_command_batch)

    batch_status = subparsers.add_parser(
        "batch-status",
        help="inspect a private native batch without changing it",
    )
    _common(batch_status, backend=False)
    batch_status.add_argument("--output", type=Path, required=True)
    batch_status.add_argument(
        "--screen-path",
        action="store_true",
        help="print only the current read-only observer screenshot path",
    )
    batch_status.set_defaults(handler=_command_batch_status)

    compare = subparsers.add_parser(
        "compare", help="compare normalized page, text, box, and raster measurements"
    )
    _common(compare, backend=False)
    compare.add_argument("--expected", type=Path, required=True)
    compare.add_argument("--actual", type=Path, required=True)
    compare.add_argument("--output", type=Path)
    compare.add_argument("--bbox-tolerance", type=float, default=0.5)
    compare.add_argument("--raster-rmse", type=float, default=0.01)
    compare.add_argument("--pixel-threshold", type=float, default=0.05)
    compare.add_argument("--max-different-ratio", type=float, default=0.001)
    compare.set_defaults(handler=_command_compare)
    return parser


def _common(parser: argparse.ArgumentParser, *, backend: bool) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--oracle-home", type=Path, help="dedicated local state directory")
    if backend:
        parser.add_argument(
            "--backend",
            choices=("real", "fake"),
            default=os.environ.get("AMIPRO_ORACLE_BACKEND", "real"),
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if os.environ.get("AMIPRO_ORACLE_LOAD_LOCAL_ENV") == "1":
            _load_local_env(repo_root() / ".env.local")
        return int(args.handler(args))
    except OracleError as exc:
        _print_error(args, str(exc), exc.exit_code)
        return exc.exit_code
    except (KeyError, OSError, TypeError, ValueError) as exc:
        _print_error(args, str(exc), EXIT_BACKEND)
        return EXIT_BACKEND


def _print_error(args: argparse.Namespace, message: str, exit_code: int) -> None:
    if getattr(args, "json", False):
        print(json.dumps({"status": "error", "exit_code": exit_code, "error": message}))
    else:
        print(f"amipro-oracle: error: {message}", file=sys.stderr)


def _emit(args: argparse.Namespace, result: dict[str, Any], *, text: str) -> None:
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(text)


def _configured_media(argument: Path | None, environment_name: str) -> Path | None:
    if argument is not None:
        return argument
    value = os.environ.get(environment_name)
    return Path(value) if value else None


def _load_local_env(path: Path) -> None:
    if path.is_symlink():
        raise OracleError(
            f"local environment file must not be a symlink: {path}",
            exit_code=EXIT_INTEGRITY,
        )
    if not path.exists():
        return
    if not path.is_file():
        raise OracleError(
            f"local environment path must be a file: {path}",
            exit_code=EXIT_INTEGRITY,
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise OracleError(
            f"cannot read local environment file: {path}",
            exit_code=EXIT_INTEGRITY,
        ) from exc
    for number, original in enumerate(lines, start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in _LOCAL_ENV_KEYS:
            continue
        lexer = shlex.shlex(raw_value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            values = list(lexer)
        except ValueError as exc:
            raise OracleError(
                f"invalid {key} value in {path.name} line {number}",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        if len(values) != 1 or not values[0]:
            raise OracleError(
                f"{key} in {path.name} line {number} must contain one quoted path",
                exit_code=EXIT_INTEGRITY,
            )
        os.environ.setdefault(key, values[0])


def _toolchain_image(home: Path) -> dict[str, Any] | None:
    path = home / "toolchain-image.json"
    if path.is_symlink():
        raise OracleError(f"invalid OCI image record path: {path}", exit_code=EXIT_INTEGRITY)
    if not path.exists():
        return None
    if not path.is_file():
        raise OracleError(f"invalid OCI image record path: {path}", exit_code=EXIT_INTEGRITY)
    try:
        value = read_json_object(path)
    except (OSError, ValueError) as exc:
        raise OracleError(f"invalid OCI image record: {path}", exit_code=EXIT_INTEGRITY) from exc
    if value.get("schema") != "amipro-oracle-image-v1":
        raise OracleError(f"invalid OCI image record schema: {path}", exit_code=EXIT_INTEGRITY)
    digest = value.get("image_digest")
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
    ):
        raise OracleError(f"invalid OCI image record: {path}", exit_code=EXIT_INTEGRITY)
    expected_lock = sha256_file(repo_root() / "toolchain" / "toolchain.lock.json")
    if value.get("lock_sha256") != expected_lock:
        raise OracleError(
            f"OCI image record was built from a different toolchain lock: {path}",
            exit_code=EXIT_INTEGRITY,
        )
    return value


def _command_doctor(args: argparse.Namespace) -> int:
    home = oracle_home(args.oracle_home, allow_temporary=args.backend == "fake")
    tools = probe_toolchain()
    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    media: dict[str, Any] = {}
    image_probe: dict[str, object] | None = None
    integrity_failure = False
    backend_failure = False

    windows = _configured_media(args.win31_media, "WIN31_MEDIA_DIR")
    amipro = _configured_media(args.amipro_media, "AMIPRO_MEDIA_DIR")
    if args.backend == "real":
        if windows is None:
            issues.append(
                {
                    "code": "missing-win31-media",
                    "message": "provide --win31-media PATH or set WIN31_MEDIA_DIR",
                }
            )
        else:
            media["windows"] = inventory_media(windows, kind="windows-3.1")
            if media["windows"]["source_writable_files"]:
                warnings.append(
                    {
                        "code": "writable-win31-source",
                        "message": (
                            "Windows source files are writable; oracle readers still open and "
                            "mount them read-only"
                        ),
                    }
                )
            if media["windows"].get("ignored_files"):
                warnings.append(
                    {
                        "code": "ignored-win31-directory-files",
                        "message": (
                            f"ignored {len(media['windows']['ignored_files'])} non-media "
                            "file(s); only the verified Disk01.img through Disk06.img enter "
                            "the media key"
                        ),
                    }
                )
        if amipro is None:
            issues.append(
                {
                    "code": "missing-amipro-media",
                    "message": "provide --amipro-media PATH or set AMIPRO_MEDIA_DIR",
                }
            )
        else:
            media["amipro"] = inventory_media(amipro, kind="amipro")
            if media["amipro"]["source_writable_files"]:
                warnings.append(
                    {
                        "code": "writable-amipro-source",
                        "message": (
                            "Ami Pro source files are writable; oracle readers still open and "
                            "mount them read-only"
                        ),
                    }
                )
            if media["amipro"].get("ignored_files"):
                warnings.append(
                    {
                        "code": "ignored-amipro-directory-files",
                        "message": (
                            f"ignored {len(media['amipro']['ignored_files'])} non-media file(s); "
                            "only the verified disk1.img through disk8.img enter the media key"
                        ),
                    }
                )

        image = _toolchain_image(home)
        if image is not None:
            image_probe = probe_recorded_image(image)
        image_ready = image_probe is not None and image_probe.get("status") == "match"
        image_status = image_probe.get("status") if image_probe is not None else None
        if image_status in {"invalid", "mismatch"}:
            integrity_failure = True
            issues.append(
                {
                    "code": "invalid-locked-toolchain",
                    "message": str(
                        image_probe.get("error", "recorded OCI image identity does not match")
                    ),
                }
            )
        elif image_status == "error":
            backend_failure = True
            issues.append(
                {
                    "code": "toolchain-probe-error",
                    "message": str(image_probe.get("error", "cannot inspect recorded OCI image")),
                }
            )
        elif not image_ready and not bool(tools["native_ready"]):
            issues.append(
                {
                    "code": "missing-locked-toolchain",
                    "message": (
                        "build/record the locked rootless OCI image; native tools are diagnostic "
                        "only until exact binary hashes are locked"
                    ),
                }
            )
    else:
        image = None

    result: dict[str, Any] = {
        "status": "ready" if not issues else "blocked",
        "backend": args.backend,
        "oracle_home": str(home),
        "oracle_home_exists": home.exists(),
        "toolchain": tools,
        "toolchain_image": image,
        "toolchain_image_probe": image_probe,
        "media": media,
        "issues": issues,
        "warnings": warnings,
        "mutated_state": False,
    }
    lines = [f"doctor: {result['status']} ({args.backend} backend)"]
    lines.extend(f"- {item['message']}" for item in issues)
    lines.extend(f"- warning: {item['message']}" for item in warnings)
    _emit(
        args,
        result,
        text="\n".join(lines),
    )
    if not issues:
        return EXIT_OK
    if integrity_failure:
        return EXIT_INTEGRITY
    if backend_failure:
        return EXIT_BACKEND
    return EXIT_MISSING


def _require_real_media(args: argparse.Namespace) -> tuple[Path, Path]:
    windows = _configured_media(args.win31_media, "WIN31_MEDIA_DIR")
    if windows is None:
        raise OracleError(
            "Windows 3.1 media is required: pass --win31-media PATH or set WIN31_MEDIA_DIR",
            exit_code=EXIT_MISSING,
        )
    amipro = _configured_media(args.amipro_media, "AMIPRO_MEDIA_DIR")
    if amipro is None:
        raise OracleError(
            "Ami Pro media is required: pass --amipro-media PATH or set AMIPRO_MEDIA_DIR",
            exit_code=EXIT_MISSING,
        )
    return windows, amipro


def _command_bootstrap(args: argparse.Namespace) -> int:
    home = oracle_home(args.oracle_home, allow_temporary=args.backend == "fake")
    if args.backend == "fake":
        result = bootstrap_fake(home)
        _emit(
            args,
            result,
            text=f"fake runtime ready: {result['runtime_key']} (not baseline eligible)",
        )
        return EXIT_OK

    if not args.confirm_proprietary_media_rights:
        raise OracleError(
            "real bootstrap requires --confirm-proprietary-media-rights; hashes prove identity, "
            "not your license or right to use the supplied media",
            exit_code=EXIT_USAGE,
        )
    windows_path, amipro_path = _require_real_media(args)
    windows = inventory_media(windows_path, kind="windows-3.1")
    amipro = inventory_media(amipro_path, kind="amipro")
    image = _toolchain_image(home)
    if image is None:
        raise OracleError(
            "build the locked OCI image with ./scripts/build-oracle-toolchain first",
            exit_code=EXIT_MISSING,
        )
    checkpoint = bootstrap_windows_checkpoint(
        home,
        windows_path,
        windows,
        image,
    )
    result = {
        **checkpoint,
        "amipro_media_validated": True,
        "amipro_media_digest": amipro["digest"],
        "next_phase": "program-manager-boot-probe",
    }
    _emit(
        args,
        result,
        text=(
            f"Windows install candidate ready: {checkpoint['checkpoint_key']}\n"
            "Next: run the separate Program Manager boot probe before Ami Pro installation."
        ),
    )
    return EXIT_OK


def _command_boot_probe(args: argparse.Namespace) -> int:
    if not args.confirm_proprietary_media_rights:
        raise OracleError(
            "the real boot probe requires --confirm-proprietary-media-rights",
            exit_code=EXIT_USAGE,
        )
    home = oracle_home(args.oracle_home, allow_temporary=False)
    image = _toolchain_image(home)
    if image is None:
        raise OracleError(
            "build the locked OCI image with ./scripts/build-oracle-toolchain first",
            exit_code=EXIT_MISSING,
        )
    result = boot_windows_ready(
        home,
        image,
        checkpoint_key=args.checkpoint_key,
        timeout_seconds=args.timeout_seconds,
    )
    _emit(
        args,
        result,
        text=(
            f"Windows runtime ready: {result['runtime_key']}\n"
            "Next: install Ami Pro into a disposable clone of this verified runtime."
        ),
    )
    return EXIT_OK


def _command_install_amipro(args: argparse.Namespace) -> int:
    if not args.confirm_proprietary_media_rights:
        raise OracleError(
            "the real Ami Pro install requires --confirm-proprietary-media-rights",
            exit_code=EXIT_USAGE,
        )
    media_path = _configured_media(args.amipro_media, "AMIPRO_MEDIA_DIR")
    if media_path is None:
        raise OracleError(
            "Ami Pro media is required: pass --amipro-media PATH or set AMIPRO_MEDIA_DIR",
            exit_code=EXIT_MISSING,
        )
    home = oracle_home(args.oracle_home, allow_temporary=False)
    image = _toolchain_image(home)
    if image is None:
        raise OracleError(
            "build the locked OCI image with ./scripts/build-oracle-toolchain first",
            exit_code=EXIT_MISSING,
        )
    media = inventory_media(media_path, kind="amipro")
    result = install_amipro_checkpoint(
        home,
        media_path,
        media,
        image,
        runtime_key=args.runtime_key,
        timeout_seconds=args.timeout_seconds,
    )
    _emit(
        args,
        result,
        text=(
            f"Ami Pro install candidate ready: {result['checkpoint_key']}\n"
            "Next: run a separate Ami Pro launch-and-clean-exit probe."
        ),
    )
    return EXIT_OK


def _command_launch_amipro(args: argparse.Namespace) -> int:
    if not args.confirm_proprietary_media_rights:
        raise OracleError(
            "the real Ami Pro launch probe requires --confirm-proprietary-media-rights",
            exit_code=EXIT_USAGE,
        )
    home = oracle_home(args.oracle_home, allow_temporary=False)
    image = _toolchain_image(home)
    if image is None:
        raise OracleError(
            "build the locked OCI image with ./scripts/build-oracle-toolchain first",
            exit_code=EXIT_MISSING,
        )
    result = launch_amipro_ready(
        home,
        image,
        checkpoint_key=args.checkpoint_key,
        timeout_seconds=args.timeout_seconds,
    )
    _emit(
        args,
        result,
        text=(
            f"Ami Pro runtime ready: {result['runtime_key']}\n"
            "Next: open one invented SAM in the Phase 2 smoke test."
        ),
    )
    return EXIT_OK


def _prepare_new_directory(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    if absolute.is_symlink():
        raise OracleError(f"output must not be a symlink: {path}", exit_code=EXIT_INTEGRITY)
    if absolute.exists():
        if not absolute.is_dir():
            raise OracleError(f"output is not a directory: {path}", exit_code=EXIT_INTEGRITY)
        if any(absolute.iterdir()):
            raise OracleError(f"output directory is not empty: {path}", exit_code=EXIT_DIFFERENT)
    else:
        absolute.mkdir(parents=True)
    return absolute


def _command_smoke(args: argparse.Namespace) -> int:
    fixture = args.input or (repo_root() / "tests" / "fixtures" / "synthetic-basic.sam")
    if args.backend == "fake":
        home = oracle_home(args.oracle_home, allow_temporary=True)
        bootstrap_fake(home)
        default_output = home / "jobs" / f"fake-smoke-{digest_json(str(fixture.absolute()))[:16]}"
        output = _prepare_new_directory(args.output or default_output)
        manifest = run_fake_job(fixture, output, staged_name="SMOKE.SAM")
        _emit(
            args,
            manifest,
            text=f"fake smoke complete: {output / 'job.json'} (not baseline eligible)",
        )
        return EXIT_OK
    if not args.confirm_proprietary_media_rights:
        raise OracleError(
            "the real document smoke requires --confirm-proprietary-media-rights",
            exit_code=EXIT_USAGE,
        )
    if args.output is not None:
        raise OracleError(
            "real smoke evidence is written under the isolated oracle home; omit --output",
            exit_code=EXIT_USAGE,
        )
    home = oracle_home(args.oracle_home, allow_temporary=False)
    image = _toolchain_image(home)
    if image is None:
        raise OracleError(
            "build the locked OCI image with ./scripts/build-oracle-toolchain first",
            exit_code=EXIT_MISSING,
        )
    result = smoke_document(
        home,
        image,
        fixture,
        runtime_key=args.runtime_key,
        timeout_seconds=args.timeout_seconds,
    )
    _emit(
        args,
        result,
        text=(
            f"invented-document smoke passed: {result['evidence_job']}\n"
            "Next: configure the pinned PostScript printer and prove one print capture."
        ),
    )
    return EXIT_OK


def _command_install_printer(args: argparse.Namespace) -> int:
    if not args.confirm_proprietary_media_rights:
        raise OracleError(
            "the real printer install requires --confirm-proprietary-media-rights",
            exit_code=EXIT_USAGE,
        )
    media_path = _configured_media(args.win31_media, "WIN31_MEDIA_DIR")
    if media_path is None:
        raise OracleError(
            "Windows 3.1 media is required: pass --win31-media PATH or set WIN31_MEDIA_DIR",
            exit_code=EXIT_MISSING,
        )
    home = oracle_home(args.oracle_home, allow_temporary=False)
    image = _toolchain_image(home)
    if image is None:
        raise OracleError(
            "build the locked OCI image with ./scripts/build-oracle-toolchain first",
            exit_code=EXIT_MISSING,
        )
    media = inventory_media(media_path, kind="windows-3.1")
    result = install_printer_ready(
        home,
        media_path,
        media,
        image,
        runtime_key=args.runtime_key,
        timeout_seconds=args.timeout_seconds,
    )
    _emit(
        args,
        result,
        text=(
            f"PostScript printer runtime ready: {result['runtime_key']}\n"
            "Next: print one invented SAM and validate the captured PostScript."
        ),
    )
    return EXIT_OK


def _command_print_smoke(args: argparse.Namespace) -> int:
    if not args.confirm_proprietary_media_rights:
        raise OracleError(
            "the real PostScript smoke requires --confirm-proprietary-media-rights",
            exit_code=EXIT_USAGE,
        )
    home = oracle_home(args.oracle_home, allow_temporary=False)
    image = _toolchain_image(home)
    if image is None:
        raise OracleError(
            "build the locked OCI image with ./scripts/build-oracle-toolchain first",
            exit_code=EXIT_MISSING,
        )
    fixture = args.input or (repo_root() / "tests" / "fixtures" / "synthetic-basic.sam")
    result = print_smoke(
        home,
        image,
        fixture,
        runtime_key=args.runtime_key,
        timeout_seconds=args.timeout_seconds,
    )
    _emit(
        args,
        result,
        text=(
            f"one-file PostScript smoke passed: {result['evidence_job']}\n"
            "Next: repeat print-smoke and compare both jobs to quantify nondeterminism."
        ),
    )
    return EXIT_OK


def _discover_sam_files(root: Path) -> list[Path]:
    absolute = root.expanduser().absolute()
    if absolute.is_symlink() or not absolute.is_dir():
        raise OracleError(f"batch input must be a real directory: {root}", exit_code=EXIT_MISSING)
    absolute = absolute.resolve()
    sources = sorted(
        (path for path in absolute.rglob("*") if path.suffix.casefold() == ".sam"),
        key=lambda path: (
            path.relative_to(absolute).as_posix().casefold(),
            path.relative_to(absolute).as_posix(),
        ),
    )
    if not sources:
        raise OracleError(f"no SAM files found under {root}", exit_code=EXIT_MISSING)
    if len(sources) > 99_999:
        raise OracleError("batch exceeds the 99,999 document limit", exit_code=EXIT_INTEGRITY)
    folded: set[str] = set()
    for source in sources:
        if source.is_symlink() or not source.is_file():
            raise OracleError(
                f"batch inputs must be regular, non-symlink files: {source}",
                exit_code=EXIT_INTEGRITY,
            )
        relative = source.relative_to(absolute).as_posix().casefold()
        if relative in folded:
            raise OracleError(
                f"batch inputs collide on a case-insensitive guest: {source}",
                exit_code=EXIT_INTEGRITY,
            )
        folded.add(relative)
    return sources


def _command_batch(args: argparse.Namespace) -> int:
    sources = _discover_sam_files(args.input)
    input_root = args.input.expanduser().absolute().resolve()
    output_candidate = args.output.expanduser().absolute().resolve(strict=False)
    if output_candidate == input_root or input_root in output_candidate.parents:
        raise OracleError("batch output must be outside the input tree", exit_code=EXIT_INTEGRITY)
    if args.backend == "real":
        if not args.confirm_proprietary_media_rights:
            raise OracleError(
                "real batch requires --confirm-proprietary-media-rights",
                exit_code=EXIT_USAGE,
            )
        home = oracle_home(args.oracle_home, allow_temporary=False)
        image = _toolchain_image(home)
        if image is None:
            raise OracleError(
                "build the locked OCI image with ./scripts/build-oracle-toolchain first",
                exit_code=EXIT_MISSING,
            )
        runtime_key, font_environment = validate_native_batch_prerequisites(
            home,
            image,
            args.runtime_key,
        )

        def worker(source: Path, guest: str, timeout: float) -> dict[str, Any]:
            return print_native_document(
                home,
                image,
                source,
                guest,
                timeout,
                runtime_key=runtime_key,
            )

        def progress(event: dict[str, object]) -> None:
            document = event.get("document")
            label = "batch"
            if isinstance(document, dict):
                label = (
                    f"[{document.get('index')}/{event['document_count']}] "
                    f"{document.get('guest')}"
                )
            print(
                f"{label}: {str(event['event']).replace('-', ' ')}; "
                f"{event['completed_count']} complete, "
                f"{event['success_count']} succeeded, "
                f"{event['failure_count']} failed or blocked",
                file=sys.stderr,
                flush=True,
            )

        with batch_coordinator_lock(home, output_candidate):
            result, exit_code = run_real_batch(
                sources=sources,
                input_root=input_root,
                output=output_candidate,
                worker=worker,
                timeout_seconds=args.timeout_seconds,
                resume=args.resume,
                progress=progress if args.progress else None,
                font_environment=font_environment,
                require_installed_fonts=args.require_installed_fonts,
            )
        _emit(
            args,
            result,
            text=(
                f"native batch: {result['success_count']} succeeded, "
                f"{result['failure_count']} failed or blocked, "
                f"{result['font_warning_count']} with font warnings -> {output_candidate}"
            ),
        )
        return exit_code

    if args.progress:
        raise OracleError(
            "--progress is currently supported only by the real backend",
            exit_code=EXIT_USAGE,
        )
    if args.resume:
        raise OracleError(
            "--resume is currently supported only by the real backend",
            exit_code=EXIT_USAGE,
        )
    if args.require_installed_fonts:
        raise OracleError(
            "--require-installed-fonts is supported only by the real backend",
            exit_code=EXIT_USAGE,
        )
    output = _prepare_new_directory(args.output)
    bootstrap_fake(oracle_home(args.oracle_home, allow_temporary=True))

    jobs: list[dict[str, object]] = []
    name_map: list[dict[str, str]] = []
    failures = 0
    for index, source in enumerate(sources, start=1):
        staged_name = f"DOC{index:05d}.SAM"
        relative = source.relative_to(input_root).as_posix()
        job_directory = output / "jobs" / f"{index:05d}"
        job_directory.mkdir(parents=True, exist_ok=False)
        name_map.append({"source": relative, "guest": staged_name})
        try:
            manifest = run_fake_job(source, job_directory, staged_name=staged_name)
            jobs.append(
                {
                    "source": relative,
                    "guest": staged_name,
                    "status": "success",
                    "manifest": (job_directory / "job.json").relative_to(output).as_posix(),
                    "source_sha256": manifest["source"]["sha256"],
                }
            )
        except (OSError, ValueError, OracleError) as exc:
            failures += 1
            failure_path = job_directory / "failure.json"
            error = f"{type(exc).__name__}: fake job failed before completion"
            if not failure_path.is_file():
                atomic_write_json(
                    failure_path,
                    {
                        "schema": "amipro-oracle-failure-v1",
                        "backend": "fake",
                        "baseline_eligible": False,
                        "status": "failure",
                        "source": relative,
                        "guest": staged_name,
                        "error": error,
                        "state_trace": [],
                        "process_result": None,
                        "artifacts": [],
                        "diagnostics": ["failure manifest fallback"],
                    },
                )
            else:
                error = str(read_json_object(failure_path).get("error", error))
            jobs.append(
                {
                    "source": relative,
                    "guest": staged_name,
                    "status": "failure",
                    "error": error,
                    "manifest": f"jobs/{index:05d}/failure.json",
                }
            )

    batch_manifest: dict[str, Any] = {
        "schema": "amipro-oracle-batch-v1",
        "backend": "fake",
        "baseline_eligible": False,
        "status": "failure" if failures else "success",
        "document_count": len(sources),
        "failure_count": failures,
        "name_map": name_map,
        "jobs": jobs,
    }
    atomic_write_json(output / "batch.json", batch_manifest)
    _emit(
        args,
        batch_manifest,
        text=f"fake batch: {len(sources) - failures} succeeded, {failures} failed -> {output}",
    )
    return EXIT_DIFFERENT if failures else EXIT_OK


def _command_batch_status(args: argparse.Namespace) -> int:
    if args.screen_path and args.json:
        raise OracleError(
            "--screen-path and --json cannot be combined",
            exit_code=EXIT_USAGE,
        )
    home = oracle_home(args.oracle_home, allow_temporary=False)
    result = read_batch_status(args.output, home)
    current = result.get("current")
    if args.screen_path:
        screen = current.get("screen_path") if isinstance(current, dict) else None
        if not isinstance(screen, str):
            raise OracleError(
                "the current batch document has no observer screenshot yet",
                exit_code=EXIT_MISSING,
            )
        print(screen)
        return EXIT_OK
    current_text = ""
    if isinstance(current, dict):
        current_text = (
            f"; current {current['index']}/{result['document_count']} "
            f"{current['guest']}"
        )
        if isinstance(current.get("screen_path"), str):
            current_text += f"; screen {current['screen_path']}"
    _emit(
        args,
        result,
        text=(
            f"native batch {result['status']}: {result['completed_count']}/"
            f"{result['document_count']} complete, {result['success_count']} succeeded, "
            f"{result['failure_count']} failed or blocked{current_text}"
        ),
    )
    return EXIT_OK


def _command_compare(args: argparse.Namespace) -> int:
    try:
        report = compare_analyses(
            args.expected,
            args.actual,
            bbox_tolerance=args.bbox_tolerance,
            raster_rmse=args.raster_rmse,
            pixel_threshold=args.pixel_threshold,
            max_different_ratio=args.max_different_ratio,
        )
    except ValueError as exc:
        raise OracleError(str(exc), exit_code=EXIT_INTEGRITY) from exc
    if report.get("schema") != COMPARE_SCHEMA:
        raise ValueError("internal comparison schema error")
    if args.output:
        atomic_write_json(args.output, report)
    _emit(
        args,
        report,
        text=(
            "comparison: equal"
            if report["equal"]
            else f"comparison: different ({len(report['issues'])} issue(s))"
        ),
    )
    return EXIT_OK if report["equal"] else EXIT_DIFFERENT
