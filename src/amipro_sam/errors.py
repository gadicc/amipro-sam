"""Public exception hierarchy."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Diagnostic


class AmiProError(Exception):
    """Base exception for expected converter failures."""


class DecodeError(AmiProError):
    """The input byte stream could not be decoded safely."""


class ParseError(AmiProError):
    """The input is not a supported or recoverable SAM document."""


class PreservationLossError(ParseError):
    """Strict parsing found one or more explicitly classified losses."""

    def __init__(self, losses: tuple[Diagnostic, ...]) -> None:
        self.losses = losses
        first = losses[0]
        super().__init__(
            f"strict parsing found {len(losses)} preservation loss(es); "
            f"first: {first.code}: {first.message}"
        )


class ResourceLimitError(AmiProError):
    """A configured parser or renderer safety limit was exceeded."""


class RenderError(AmiProError):
    """A parsed document could not be rendered in the requested format."""
