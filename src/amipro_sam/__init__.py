"""Preservation-oriented tools for Lotus Ami Pro SAM documents."""

from .errors import PreservationLossError
from .limits import ParseLimits
from .model import Lossiness
from .parser import parse_bytes, parse_file

__all__ = [
    "Lossiness",
    "ParseLimits",
    "PreservationLossError",
    "parse_bytes",
    "parse_file",
]
__version__ = "0.1.0"
