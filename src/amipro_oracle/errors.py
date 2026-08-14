from __future__ import annotations

from .constants import EXIT_BACKEND


class OracleError(Exception):
    """Expected command failure with a stable process exit code."""

    def __init__(self, message: str, *, exit_code: int = EXIT_BACKEND) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.process_result: dict[str, object] | None = None


class MediaIntegrityError(OracleError):
    """A supplied media tree was unsafe, inconsistent, or changed during hashing."""
