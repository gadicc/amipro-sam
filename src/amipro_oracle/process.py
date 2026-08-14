from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from time import monotonic

from .constants import EXIT_TIMEOUT
from .errors import OracleError


def _signal_process_group(process: subprocess.Popen[bytes], requested: signal.Signals) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, requested)


def run_bounded(
    command: Sequence[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    grace_seconds: float = 2.0,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    if not command or timeout_seconds <= 0 or grace_seconds < 0:
        raise ValueError("bounded process requires a command and positive timeout")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(environment) if environment is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        timed_out = False
        killed = False
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _signal_process_group(process, signal.SIGTERM)
            try:
                return_code = process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                killed = True
                _signal_process_group(process, signal.SIGKILL)
                return_code = process.wait()
    result = {
        "command": list(command),
        "exit_code": return_code,
        "timed_out": timed_out,
        "killed": killed,
        "duration_seconds": round(monotonic() - started, 6),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }
    if timed_out:
        error = OracleError(
            f"process exceeded the {timeout_seconds:g}s deadline: {command[0]}",
            exit_code=EXIT_TIMEOUT,
        )
        error.process_result = result
        raise error
    return result
