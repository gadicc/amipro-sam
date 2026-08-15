from __future__ import annotations

import os
import signal
import stat
import subprocess
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from time import monotonic, sleep

from .constants import EXIT_INTEGRITY, EXIT_TIMEOUT
from .errors import OracleError

DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_TREE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_TREE_ENTRIES = 20_000
_POLL_SECONDS = 0.25
_READ_BYTES = 64 * 1024


def _signal_process_group(process: subprocess.Popen[bytes], requested: signal.Signals) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, requested)


def _drain_bounded(
    stream: object,
    path: Path,
    maximum: int,
    result: dict[str, object],
) -> None:
    head_limit = maximum // 2
    tail_limit = maximum - head_limit
    total = 0
    head_written = 0
    tail = bytearray()
    try:
        with path.open("wb") as handle:
            while True:
                chunk = os.read(stream.fileno(), _READ_BYTES)  # type: ignore[attr-defined]
                if not chunk:
                    break
                total += len(chunk)
                wrote = False
                if head_written < head_limit:
                    selected = chunk[: head_limit - head_written]
                    handle.write(selected)
                    head_written += len(selected)
                    chunk = chunk[len(selected) :]
                    wrote = bool(selected)
                if chunk:
                    tail.extend(chunk)
                    if len(tail) > tail_limit:
                        del tail[: len(tail) - tail_limit]
                if wrote:
                    handle.flush()
            omitted = max(0, total - head_written - len(tail))
            if omitted:
                marker = (
                    f"\n[amipro-oracle omitted {omitted} output bytes]\n"
                ).encode("ascii")
                handle.write(marker)
            handle.write(tail)
            handle.flush()
            os.fsync(handle.fileno())
        result.update(
            {
                "bytes": total,
                "captured_bytes": head_written + len(tail),
                "truncated": bool(omitted),
            }
        )
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"


def _bounded_tree_usage(root: Path, maximum_entries: int) -> tuple[int, int]:
    total = 0
    entries = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            iterator_context = os.scandir(directory)
        except FileNotFoundError:
            continue
        with iterator_context as iterator:
            for child in iterator:
                try:
                    info = child.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                entries += 1
                if entries > maximum_entries:
                    return total, entries
                if stat.S_ISDIR(info.st_mode):
                    stack.append(Path(child.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                else:
                    raise OracleError(
                        f"watched process tree contains an unsafe entry: {child.path}",
                        exit_code=EXIT_INTEGRITY,
                    )
    return total, entries


def _terminate_process(
    process: subprocess.Popen[bytes], grace_seconds: float
) -> tuple[int, bool]:
    _signal_process_group(process, signal.SIGTERM)
    try:
        return process.wait(timeout=grace_seconds), False
    except subprocess.TimeoutExpired:
        _signal_process_group(process, signal.SIGKILL)
        return process.wait(), True


def run_bounded(
    command: Sequence[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    grace_seconds: float = 2.0,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    watch_path: Path | None = None,
    max_tree_bytes: int = DEFAULT_MAX_TREE_BYTES,
    max_tree_entries: int = DEFAULT_MAX_TREE_ENTRIES,
) -> dict[str, object]:
    if (
        not command
        or timeout_seconds <= 0
        or grace_seconds < 0
        or max_output_bytes < 1024
        or max_tree_bytes < 1024
        or max_tree_entries < 1
    ):
        raise ValueError("bounded process requires a command and positive timeout")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    watched = watch_path.expanduser().absolute() if watch_path is not None else None
    if watched is not None and (watched.is_symlink() or not watched.is_dir()):
        raise ValueError("bounded process watch_path must be a real directory")
    started = monotonic()
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout_capture: dict[str, object] = {}
    stderr_capture: dict[str, object] = {}
    stdout_thread = threading.Thread(
        target=_drain_bounded,
        args=(process.stdout, stdout_path, max_output_bytes, stdout_capture),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_bounded,
        args=(process.stderr, stderr_path, max_output_bytes, stderr_capture),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    killed = False
    limit_error: OracleError | None = None
    final_tree_bytes: int | None = None
    final_tree_entries: int | None = None
    try:
        while process.poll() is None:
            elapsed = monotonic() - started
            if elapsed >= timeout_seconds:
                timed_out = True
                return_code, killed = _terminate_process(process, grace_seconds)
                break
            if watched is not None:
                tree_bytes, tree_entries = _bounded_tree_usage(
                    watched, max_tree_entries
                )
                if tree_bytes > max_tree_bytes or tree_entries > max_tree_entries:
                    return_code, killed = _terminate_process(process, grace_seconds)
                    limit_error = OracleError(
                        "process exceeded its writable-tree quota "
                        f"({tree_bytes} bytes, {tree_entries} entries)",
                        exit_code=EXIT_INTEGRITY,
                    )
                    break
            sleep(min(_POLL_SECONDS, max(0.01, timeout_seconds - elapsed)))
        else:
            return_code = process.returncode
    except BaseException:
        if process.poll() is None:
            _terminate_process(process, grace_seconds)
        raise
    finally:
        _signal_process_group(process, signal.SIGTERM)
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            _signal_process_group(process, signal.SIGKILL)
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        if stdout_thread.is_alive():
            stdout_capture["error"] = "stdout capture thread did not stop"
        if stderr_thread.is_alive():
            stderr_capture["error"] = "stderr capture thread did not stop"
    if watched is not None:
        final_tree_bytes, final_tree_entries = _bounded_tree_usage(
            watched,
            max_tree_entries,
        )
        if (
            limit_error is None
            and not timed_out
            and (
                final_tree_bytes > max_tree_bytes
                or final_tree_entries > max_tree_entries
            )
        ):
            limit_error = OracleError(
                "process exceeded its writable-tree quota "
                f"({final_tree_bytes} bytes, {final_tree_entries} entries)",
                exit_code=EXIT_INTEGRITY,
            )
    result = {
        "command": list(command),
        "exit_code": return_code,
        "timed_out": timed_out,
        "killed": killed,
        "duration_seconds": round(monotonic() - started, 6),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "stdout_capture": stdout_capture,
        "stderr_capture": stderr_capture,
    }
    if final_tree_bytes is not None and final_tree_entries is not None:
        result["final_tree_bytes"] = final_tree_bytes
        result["final_tree_entries"] = final_tree_entries
    if "error" in stdout_capture or "error" in stderr_capture:
        raise OracleError("failed to capture bounded process output", exit_code=EXIT_INTEGRITY)
    if limit_error is not None:
        limit_error.process_result = result
        raise limit_error
    if timed_out:
        error = OracleError(
            f"process exceeded the {timeout_seconds:g}s deadline: {command[0]}",
            exit_code=EXIT_TIMEOUT,
        )
        error.process_result = result
        raise error
    return result
