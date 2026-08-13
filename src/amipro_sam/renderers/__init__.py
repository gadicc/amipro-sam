"""Output renderers for the shared intermediate representation.

Renderers are loaded lazily so that optional output dependencies are only
needed when their format is actually selected.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

from ..errors import RenderError
from ..model import Document

Renderer = Callable[..., bytes]

_FORMAT_MODULES = {
    "html": "html",
    "markdown": "markdown",
    "text": "text",
    "json": "json",
    "pdf": "pdf",
    "odt": "odt",
    "docx": "docx",
}
_ALIASES = {"htm": "html", "md": "markdown", "txt": "text"}


def _lazy_renderer(format_name: str) -> Renderer:
    def render(document: Document, **options: object) -> bytes:
        module = import_module(f"{__name__}.{_FORMAT_MODULES[format_name]}")
        return module.render(document, **options)

    render.__name__ = f"render_{format_name}"
    render.__doc__ = f"Render a document as {format_name}."
    return render


# Public and useful for callers offering their own format picker.  Values are
# lazy callables rather than imported modules, keeping ReportLab/python-docx
# optional for users who only need the standard-library formats.
RENDERERS: dict[str, Renderer] = {
    name: _lazy_renderer(name) for name in _FORMAT_MODULES
}


def get_renderer(format_name: str) -> Renderer:
    """Return the renderer for *format_name* or raise a user-facing error."""

    normalized = _ALIASES.get(format_name.strip().lower(), format_name.strip().lower())
    try:
        return RENDERERS[normalized]
    except KeyError as exc:
        choices = ", ".join(sorted(RENDERERS))
        raise RenderError(
            f"unknown output format {format_name!r}; choose one of: {choices}"
        ) from exc


__all__ = ["RENDERERS", "Renderer", "get_renderer"]
