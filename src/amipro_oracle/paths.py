from __future__ import annotations

import os
from pathlib import Path

from .constants import EXIT_USAGE
from .errors import OracleError


def repo_root() -> Path:
    configured = os.environ.get("AMIPRO_ORACLE_REPO_ROOT")
    return Path(configured).resolve() if configured else Path.cwd().resolve()


def oracle_home(value: Path | None = None, *, allow_temporary: bool = False) -> Path:
    configured = value or (
        Path(os.environ["AMIPRO_ORACLE_HOME"])
        if os.environ.get("AMIPRO_ORACLE_HOME")
        else repo_root() / ".amipro-oracle"
    )
    resolved = configured.expanduser().resolve(strict=False)
    temporary = Path("/tmp").resolve()
    forbidden = {Path("/").resolve(), repo_root(), Path.home().resolve(), temporary}
    temporary_forbidden = not allow_temporary and (
        resolved == temporary or temporary in resolved.parents
    )
    if resolved in forbidden - {temporary} or temporary_forbidden:
        raise OracleError(
            f"oracle state must use a dedicated non-/tmp directory, not {resolved}",
            exit_code=EXIT_USAGE,
        )
    return resolved
