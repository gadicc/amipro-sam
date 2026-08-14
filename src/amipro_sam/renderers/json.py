"""Deterministic, bounded JSON serialization of the shared document model."""

from __future__ import annotations

import json

from ..model import Document, _jsonable, _TextOutputBudget

__all__ = ["render"]


_MAX_JSON_RECURSION = 64
_MAX_JSON_ITEMS = 100_000
_MAX_JSON_INTEGER_BITS = 1_024
_MAX_JSON_TEXT_CHARACTERS = 4_000_000


def render(document: Document, **_options: object) -> bytes:
    """Return a stable, human-readable UTF-8 JSON dump of *document*.

    The renderer owns its serialization instead of relying on arbitrary
    ``str()`` implementations.  This keeps manually constructed IR bounded and
    deterministic while leaving the representation of valid model values
    unchanged.
    """

    serialized = json.dumps(
        _jsonable(
            document,
            max_items=_MAX_JSON_ITEMS,
            max_integer_bits=_MAX_JSON_INTEGER_BITS,
            max_recursion=_MAX_JSON_RECURSION,
            _text_budget=_TextOutputBudget(remaining=_MAX_JSON_TEXT_CHARACTERS),
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    return (serialized + "\n").encode("utf-8", errors="backslashreplace")
