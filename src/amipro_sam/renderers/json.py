"""Deterministic JSON serialization of the shared document model."""

from __future__ import annotations

import json

from ..model import Document

__all__ = ["render"]


def render(document: Document, **_options: object) -> bytes:
    """Return a stable, human-readable UTF-8 JSON dump of *document*."""

    serialized = json.dumps(
        document.to_dict(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return (serialized + "\n").encode("utf-8", errors="backslashreplace")
