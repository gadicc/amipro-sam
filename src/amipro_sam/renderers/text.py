"""Complete, presentation-neutral plain-text extraction."""

from __future__ import annotations

import re

from ..model import (
    Annotation,
    Document,
    Footer,
    Footnote,
    Frame,
    Header,
    Image,
    PageBreak,
    Paragraph,
    SdwDrawing,
    Table,
    TableCell,
    TableRow,
    UnsupportedObject,
    WmfGraphic,
    _paragraph_text,
    _table_cell_text,
    _TextOutputBudget,
)
from ..sdw import SdwDecodeError, sdw_display_size, sdw_preview_caption
from ..wmf import WmfDecodeError, wmf_display_size
from .structure_labels import show_container_label

__all__ = ["render"]


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
_MAX_RENDER_DEPTH = 32
_MAX_RENDER_BLOCKS = 100_000
_MAX_TABLE_ROWS = 390

def render(
    document: Document,
    *,
    show_structure_labels: bool = False,
    **_options: object,
) -> bytes:
    """Return readable UTF-8 text, retaining every block in source order.

    Tables use tab-separated rows, explicit page boundaries use form feeds,
    and objects that cannot be represented are emitted as visible labels.
    """

    rendered = _render_blocks(
        document.blocks,
        seen=set(),
        seen_blocks=set(),
        text_budget=_TextOutputBudget(),
        show_structure_labels=show_structure_labels,
    )
    if not rendered:
        return b""
    return (rendered + "\n").encode("utf-8", errors="backslashreplace")


def _render_blocks(
    blocks: object,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
    seen_blocks: set[int] | None = None,
    text_budget: _TextOutputBudget | None = None,
    show_structure_labels: bool = False,
) -> str:
    if depth >= _MAX_RENDER_DEPTH:
        return "[Nested content omitted at safe depth limit]"
    if not isinstance(blocks, list | tuple):
        return "[Invalid nested content omitted]"
    seen = set() if seen is None else seen
    seen_blocks = set() if seen_blocks is None else seen_blocks
    text_budget = _TextOutputBudget() if text_budget is None else text_budget
    identity = id(blocks)
    if identity in seen:
        return "[Repeated or recursive content omitted]"
    seen.add(identity)
    chunks: list[str] = []
    list_counters: dict[int, int] = {}
    block_limit_reached = len(blocks) > _MAX_RENDER_BLOCKS
    for block in blocks[:_MAX_RENDER_BLOCKS]:
        block_identity = id(block)
        if block_identity in seen_blocks:
            chunks.append("[Repeated block object omitted]")
            list_counters.clear()
            continue
        seen_blocks.add(block_identity)
        if isinstance(block, Paragraph):
            if block.page_break_before:
                chunks.append("\f")
                list_counters.clear()
            chunks.append(_paragraph(block, list_counters, text_budget))
        elif isinstance(block, PageBreak):
            chunks.append("\f")
            list_counters.clear()
        elif isinstance(block, Table):
            chunks.append(_table(block, text_budget))
            list_counters.clear()
        elif isinstance(block, Image):
            chunks.append(_image_placeholder(block))
            list_counters.clear()
        elif isinstance(block, WmfGraphic):
            chunks.append(_wmf_placeholder(block))
            list_counters.clear()
        elif isinstance(block, SdwDrawing):
            chunks.append(_sdw_marker(block))
            list_counters.clear()
        elif isinstance(block, UnsupportedObject):
            kind = _safe_label_field(
                block.kind, "unknown object kind", maximum=128
            )
            description = _safe_label_field(
                block.description, "description unavailable", maximum=256
            )
            chunks.append(
                _clean(f"[Unsupported {kind}: {description}]")
            )
            list_counters.clear()
        elif isinstance(block, Frame):
            content = _render_blocks(
                block.blocks,
                depth=depth + 1,
                seen=seen,
                seen_blocks=seen_blocks,
                text_budget=text_budget,
                show_structure_labels=show_structure_labels,
            )
            chunks.append(
                _marked_container(_frame_marker(block), content)
                if show_container_label(block, requested=show_structure_labels)
                else content
            )
            list_counters.clear()
        elif isinstance(block, Annotation):
            chunks.append(
                _marked_container(
                    "[Annotation]",
                    _render_blocks(
                        block.blocks,
                        depth=depth + 1,
                        seen=seen,
                        seen_blocks=seen_blocks,
                        text_budget=text_budget,
                        show_structure_labels=show_structure_labels,
                    ),
                )
            )
            list_counters.clear()
        elif isinstance(block, Footnote):
            marker = (
                "[Footnote "
                + _safe_label_field(
                    block.number, "number unavailable", maximum=64
                )
                + "]"
                if block.number is not None
                else "[Footnote]"
            )
            chunks.append(
                _marked_container(
                    marker,
                    _render_blocks(
                        block.blocks,
                        depth=depth + 1,
                        seen=seen,
                        seen_blocks=seen_blocks,
                        text_budget=text_budget,
                        show_structure_labels=show_structure_labels,
                    ),
                )
            )
            list_counters.clear()
        elif isinstance(block, Header | Footer):
            kind = "Header" if isinstance(block, Header) else "Footer"
            marker = f"[{kind}: {_placement_label(block.placement)}]"
            content = _render_blocks(
                block.blocks,
                depth=depth + 1,
                seen=seen,
                seen_blocks=seen_blocks,
                text_budget=text_budget,
                show_structure_labels=show_structure_labels,
            )
            chunks.append(
                _marked_container(marker, content)
                if show_container_label(block, requested=show_structure_labels)
                else content
            )
            list_counters.clear()
        else:
            chunks.append(
                _clean(f"[Unrecognized block object omitted: {type(block).__name__}]")
            )
            list_counters.clear()

    if block_limit_reached:
        omitted = len(blocks) - _MAX_RENDER_BLOCKS
        chunks.append(
            "[Block content omitted at safe rendering limit: "
            f"{omitted} additional block(s)]"
        )

    return "\n\n".join(chunks)


def _marked_container(marker: str, content: str) -> str:
    return f"{marker}\n{content}\n[End {marker[1:-1]}]" if content else marker


def _placement_label(value: str) -> str:
    if not isinstance(value, str):
        return "placement unknown"
    return {
        "all": "all pages",
        "odd": "odd/right pages",
        "even": "even/left pages",
        "odd-even": "odd and even variants",
        "unknown": "placement unknown",
    }.get(value, "placement unknown")


def _frame_marker(frame: Frame) -> str:
    placement = frame.placement if isinstance(frame.placement, str) and frame.placement in {
        "anchored",
        "fixed-page",
        "repeating",
    } else "unknown placement"
    region = (
        frame.region
        if isinstance(frame.region, str) and frame.region in {"body", "header", "footer"}
        else "unknown region"
    )
    kind = frame.content_kind if isinstance(frame.content_kind, str) and frame.content_kind in {
        "text",
        "table",
        "image",
        "drawing",
    } else "unknown content"
    return f"[Frame: {placement}; {region}; {kind}; geometry reflowed]"


def _paragraph(
    paragraph: Paragraph,
    counters: dict[int, int],
    text_budget: _TextOutputBudget,
) -> str:
    text = _clean(_paragraph_text(paragraph, text_budget))
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


def _table(table: Table, text_budget: _TextOutputBudget) -> str:
    source_rows = table.rows
    if not isinstance(source_rows, list | tuple):
        return "[Invalid table rows omitted]"
    seen_rows: set[int] = set()
    safe_rows: list[TableRow] = []
    omitted = len(source_rows) > _MAX_TABLE_ROWS
    for row in source_rows[:_MAX_TABLE_ROWS]:
        if not isinstance(row, TableRow) or id(row) in seen_rows:
            omitted = True
            continue
        seen_rows.add(id(row))
        safe_rows.append(row)
    if not safe_rows:
        return "[Empty table]"
    rows: list[str] = []
    seen_cells: set[int] = set()
    for row in safe_rows:
        cells: list[str] = []
        source_cells = row.cells if isinstance(row.cells, list | tuple) else []
        if len(source_cells) > 256:
            rows.append("[Table cells omitted at safe 256-column limit]")
            omitted = True
            continue
        for cell in source_cells[:256]:
            if not isinstance(cell, TableCell):
                omitted = True
                continue
            if id(cell) in seen_cells:
                cells.append("[Repeated table cell omitted]")
                omitted = True
                continue
            seen_cells.add(id(cell))
            # A physical newline would split the TSV row, so retain paragraph
            # boundaries with a readable slash separator instead.
            value = _clean(_table_cell_text(cell, text_budget)).replace(
                "\t", "    "
            ).replace("\n", " / ")
            cells.append(value)
            cells.extend(
                ""
                for _ in range(
                    max(1, min(_integer(cell.column_span, 1), 256)) - 1
                )
            )
        rows.append("\t".join(cells))
    if omitted:
        rows.append("[Table content omitted at safe rendering limit]")
    return "\n".join(rows)


def _image_placeholder(image: Image) -> str:
    alt = _safe_label_field(image.alt_text, "Embedded image", maximum=256)
    if image.data is not None:
        return f"[Image: {alt} (embedded image data)]"
    if image.reference:
        reference = _safe_label_field(
            image.reference, "invalid external reference omitted", maximum=256
        )
        return f"[Image: {alt} (external reference not loaded: {reference})]"
    return f"[Image: {alt}]"


def _wmf_placeholder(graphic: WmfGraphic) -> str:
    try:
        wmf_display_size(graphic)
    except WmfDecodeError:
        return "[Invalid WMF preview]"
    alt = _safe_label_field(
        graphic.alt_text, "Embedded WMF preview", maximum=256
    )
    return f"[WMF preview: {alt} ({graphic.width_px} x {graphic.height_px} pixels)]"


def _sdw_marker(drawing: SdwDrawing) -> str:
    try:
        sdw_display_size(drawing)
    except SdwDecodeError:
        return _clean(_sdw_placeholder(drawing))
    alt = _safe_label_field(drawing.alt_text, "Ami Draw object", maximum=256)
    return _clean(f"[{sdw_preview_caption(drawing)}: {alt}]")


def _sdw_placeholder(drawing: SdwDrawing) -> str:
    alt = _safe_label_field(drawing.alt_text, "Ami Draw object", maximum=256)
    status = _safe_label_field(drawing.status, "unavailable", maximum=64)
    reason = _safe_label_field(drawing.reason, "preview unavailable", maximum=256)
    details = [
        f"Ami Draw object: {alt}",
        "no valid companion preview",
        "vector payload not rendered",
        f"status={status}",
        f"reason={reason}",
    ]
    length = drawing.declared_length
    if isinstance(length, int) and not isinstance(length, bool) and 0 <= length <= 2**63 - 1:
        details.append(f"declared length={length} bytes")
    digest = drawing.source_sha256
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        details.append(f"SHA-256={digest.lower()}")
    return "[" + "; ".join(details) + "]"


def _safe_label_field(value: object, default: str, *, maximum: int) -> str:
    if isinstance(value, str):
        result = value[: maximum * 4]
    elif isinstance(value, bytes):
        result = value[: maximum * 4].decode("utf-8", errors="replace")
    elif isinstance(value, bool | float):
        try:
            result = str(value)
        except (TypeError, ValueError, OverflowError):
            return default
    elif isinstance(value, int):
        bits = value.bit_length()
        result = (
            f"[oversized integer omitted: {bits} bits]"
            if bits > 1_024
            else str(value)
        )
    else:
        return default
    result = " ".join(result.split())[:maximum]
    return result or default


def _clean(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return "[Invalid text content omitted]"
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARACTERS.sub("\ufffd", normalized)


def _integer(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default
