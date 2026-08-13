"""Complete, presentation-neutral plain-text extraction."""

from __future__ import annotations

import re

from ..model import Document, Image, PageBreak, Paragraph, Table, UnsupportedObject

__all__ = ["render"]


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


def render(document: Document, **_options: object) -> bytes:
    """Return readable UTF-8 text, retaining every block in source order.

    Tables use tab-separated rows, explicit page boundaries use form feeds,
    and objects that cannot be represented are emitted as visible labels.
    """

    chunks: list[str] = []
    list_counters: dict[int, int] = {}
    for block in document.blocks:
        if isinstance(block, Paragraph):
            if block.page_break_before:
                chunks.append("\f")
                list_counters.clear()
            chunks.append(_paragraph(block, list_counters))
        elif isinstance(block, PageBreak):
            chunks.append("\f")
            list_counters.clear()
        elif isinstance(block, Table):
            chunks.append(_table(block))
            list_counters.clear()
        elif isinstance(block, Image):
            chunks.append(_image_placeholder(block))
            list_counters.clear()
        elif isinstance(block, UnsupportedObject):
            chunks.append(
                _clean(f"[Unsupported {block.kind}: {block.description}]")
            )
            list_counters.clear()

    if not chunks:
        return b""
    return ("\n\n".join(chunks) + "\n").encode(
        "utf-8", errors="backslashreplace"
    )


def _paragraph(paragraph: Paragraph, counters: dict[int, int]) -> str:
    text = _clean(paragraph.text)
    if paragraph.list_kind is None:
        counters.clear()
        return text

    level = max(0, min(_integer(paragraph.list_level, 0), 15))
    for stale in [item for item in counters if item > level]:
        del counters[stale]
    if paragraph.list_kind == "number":
        counters[level] = counters.get(level, 0) + 1
        marker = f"{counters[level]}."
    else:
        counters.pop(level, None)
        marker = "-"
    indent = "  " * level
    continuation = " " * (len(indent) + len(marker) + 1)
    lines = text.split("\n")
    first = lines[0] if lines else ""
    result = f"{indent}{marker} {first}"
    if len(lines) > 1:
        result += "\n" + "\n".join(continuation + line for line in lines[1:])
    return result


def _table(table: Table) -> str:
    if not table.rows:
        return "[Empty table]"
    rows: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            # A physical newline would split the TSV row, so retain paragraph
            # boundaries with a readable slash separator instead.
            value = _clean(cell.text).replace("\t", "    ").replace("\n", " / ")
            cells.append(value)
            cells.extend("" for _ in range(max(1, _integer(cell.column_span, 1)) - 1))
        rows.append("\t".join(cells))
    return "\n".join(rows)


def _image_placeholder(image: Image) -> str:
    alt = _clean(image.alt_text or "Embedded image")
    if image.data is not None:
        return f"[Image: {alt} (embedded image data)]"
    if image.reference:
        reference = _clean(image.reference)
        return f"[Image: {alt} (external reference not loaded: {reference})]"
    return f"[Image: {alt}]"


def _clean(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARACTERS.sub("\ufffd", normalized)


def _integer(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default
