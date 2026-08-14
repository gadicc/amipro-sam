"""Conservative CommonMark-oriented rendering."""

from __future__ import annotations

import re

from ..model import (
    Annotation,
    CharacterStyle,
    Document,
    Footer,
    Footnote,
    Frame,
    Header,
    Image,
    PageBreak,
    Paragraph,
    SdwDrawing,
    StyleDefinition,
    Table,
    TableCell,
    TableRow,
    TextRun,
    UnsupportedObject,
    WmfGraphic,
    _TextOutputBudget,
)
from ..sdw import SdwDecodeError, sdw_display_size, sdw_preview_caption
from ..wmf import WmfDecodeError, wmf_display_size

__all__ = ["render"]


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HEADING_NUMBER = re.compile(
    r"(?:^|\b)(?:heading|head|h)\s*[-_:]?\s*([1-6])(?:\b|$)", re.IGNORECASE
)
_MAX_RENDER_DEPTH = 32
_MAX_RENDER_BLOCKS = 100_000
_MAX_TABLE_ROWS = 390
_MAX_PARAGRAPH_RUNS = 4_096
_MAX_PARAGRAPH_TEXT = 1_000_000

def render(document: Document, **_options: object) -> bytes:
    """Return CommonMark-like Markdown without source-controlled raw HTML."""

    rendered = _render_blocks(
        document,
        document.blocks,
        seen=set(),
        seen_blocks=set(),
        text_budget=_TextOutputBudget(),
    )
    if not rendered:
        return b""
    return (rendered + "\n").encode("utf-8", errors="backslashreplace")


def _render_blocks(
    document: Document,
    blocks: object,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
    seen_blocks: set[int] | None = None,
    text_budget: _TextOutputBudget | None = None,
) -> str:
    if depth >= _MAX_RENDER_DEPTH:
        return _escape_text("[Nested content omitted at safe depth limit]")
    if not isinstance(blocks, list | tuple):
        return _escape_text("[Invalid nested content omitted]")
    seen = set() if seen is None else seen
    seen_blocks = set() if seen_blocks is None else seen_blocks
    text_budget = _TextOutputBudget() if text_budget is None else text_budget
    identity = id(blocks)
    if identity in seen:
        return _escape_text("[Repeated or recursive content omitted]")
    seen.add(identity)
    chunks: list[str] = []
    counters: dict[int, int] = {}
    index = 0
    block_limit_reached = len(blocks) > _MAX_RENDER_BLOCKS
    safe_blocks = blocks[:_MAX_RENDER_BLOCKS]
    while index < len(safe_blocks):
        block = safe_blocks[index]
        block_identity = id(block)
        if block_identity in seen_blocks:
            chunks.append(_escape_text("[Repeated block object omitted]"))
            counters.clear()
            index += 1
            continue
        seen_blocks.add(block_identity)
        if isinstance(block, Paragraph):
            if block.page_break_before:
                chunks.append("[Page break]")
                counters.clear()
            if block.list_kind is not None:
                items = [_paragraph(document, block, counters, text_budget)]
                index += 1
                while index < len(safe_blocks):
                    candidate = safe_blocks[index]
                    if id(candidate) in seen_blocks:
                        break
                    if (
                        not isinstance(candidate, Paragraph)
                        or candidate.list_kind is None
                        or (candidate.page_break_before and items)
                    ):
                        break
                    seen_blocks.add(id(candidate))
                    items.append(
                        _paragraph(document, candidate, counters, text_budget)
                    )
                    index += 1
                chunks.append("\n".join(items))
                continue
            chunks.append(_paragraph(document, block, counters, text_budget))
        elif isinstance(block, PageBreak):
            chunks.append("[Page break]")
            counters.clear()
        elif isinstance(block, Table):
            chunks.append(_table(document, block, text_budget))
            counters.clear()
        elif isinstance(block, Image):
            chunks.append(_image_placeholder(block))
            counters.clear()
        elif isinstance(block, WmfGraphic):
            chunks.append(_wmf_placeholder(block))
            counters.clear()
        elif isinstance(block, SdwDrawing):
            chunks.append(_sdw_marker(block))
            counters.clear()
        elif isinstance(block, UnsupportedObject):
            kind = _safe_label_field(
                block.kind, "unknown object kind", maximum=128
            )
            description = _safe_label_field(
                block.description, "description unavailable", maximum=256
            )
            chunks.append(
                _escape_text(f"[Unsupported {kind}: {description}]")
            )
            counters.clear()
        elif isinstance(block, Frame):
            chunks.append(
                _marked_container(
                    _frame_marker(block),
                    _render_blocks(
                        document,
                        block.blocks,
                        depth=depth + 1,
                        seen=seen,
                        seen_blocks=seen_blocks,
                        text_budget=text_budget,
                    ),
                )
            )
            counters.clear()
        elif isinstance(block, Annotation):
            chunks.append(
                _marked_container(
                    "[Annotation]",
                    _render_blocks(
                        document,
                        block.blocks,
                        depth=depth + 1,
                        seen=seen,
                        seen_blocks=seen_blocks,
                        text_budget=text_budget,
                    ),
                )
            )
            counters.clear()
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
                        document,
                        block.blocks,
                        depth=depth + 1,
                        seen=seen,
                        seen_blocks=seen_blocks,
                        text_budget=text_budget,
                    ),
                )
            )
            counters.clear()
        elif isinstance(block, Header | Footer):
            kind = "Header" if isinstance(block, Header) else "Footer"
            marker = f"[{kind}: {_placement_label(block.placement)}]"
            chunks.append(
                _marked_container(
                    marker,
                    _render_blocks(
                        document,
                        block.blocks,
                        depth=depth + 1,
                        seen=seen,
                        seen_blocks=seen_blocks,
                        text_budget=text_budget,
                    ),
                )
            )
            counters.clear()
        else:
            chunks.append(
                _escape_text(
                    f"[Unrecognized block object omitted: {type(block).__name__}]"
                )
            )
            counters.clear()
        index += 1

    if block_limit_reached:
        omitted = len(blocks) - _MAX_RENDER_BLOCKS
        chunks.append(
            _escape_text(
                "[Block content omitted at safe rendering limit: "
                f"{omitted} additional block(s)]"
            )
        )

    return "\n\n".join(chunks)


def _marked_container(marker: str, content: str) -> str:
    return f"{marker}\n\n{content}" if content else marker


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
    document: Document,
    paragraph: Paragraph,
    counters: dict[int, int],
    text_budget: _TextOutputBudget,
) -> str:
    content = _paragraph_inline(document, paragraph, text_budget)
    heading = _heading_level(paragraph.style_name)
    if paragraph.list_kind is not None:
        level = max(0, min(_integer(paragraph.list_level, 0), 15))
        for stale in [item for item in counters if item > level]:
            del counters[stale]
        if paragraph.list_kind == "number":
            counters[level] = counters.get(level, 0) + 1
            marker = f"{counters[level]}."
        else:
            counters.pop(level, None)
            marker = "-"
        indent = "    " * level
        continuation = " " * (len(indent) + len(marker) + 1)
        lines = content.split("\n")
        rendered = f"{indent}{marker} {lines[0] if lines else ''}"
        if len(lines) > 1:
            rendered += "\n" + "\n".join(
                continuation + line for line in lines[1:]
            )
        return rendered

    counters.clear()
    if heading is not None:
        return f"{'#' * heading} {content}"
    return _protect_block_prefixes(content)


def _paragraph_inline(
    document: Document,
    paragraph: Paragraph,
    text_budget: _TextOutputBudget,
) -> str:
    base = _named_character_style(document, paragraph.style_name)
    runs = paragraph.runs
    if not isinstance(runs, list | tuple):
        return _escape_text("[Invalid paragraph runs omitted]")
    values: list[str] = []
    paragraph_remaining = _MAX_PARAGRAPH_TEXT
    omitted = len(runs) > _MAX_PARAGRAPH_RUNS
    for run in runs[:_MAX_PARAGRAPH_RUNS]:
        if not isinstance(run, TextRun):
            values.append(_escape_text("[Invalid text run omitted]"))
            continue
        if not isinstance(run.style, CharacterStyle):
            values.append(_escape_text("[Invalid text run style omitted]"))
            continue
        if isinstance(run.text, bytes):
            run_text = run.text.decode("utf-8", errors="replace")
        elif isinstance(run.text, str):
            run_text = run.text
        else:
            values.append(_escape_text("[Invalid text run content omitted]"))
            continue
        prepared = text_budget.prepare(
            run_text,
            unit_limit=paragraph_remaining,
            expansion_factor=2,
        )
        if prepared.visible:
            values.append(_run(prepared.visible, base, run.style))
        paragraph_remaining -= len(prepared.text)
        if prepared.encoding in {"bounded-text", "text-budget-limit"}:
            omitted = True
        if paragraph_remaining <= 0:
            omitted = True
            break
    if omitted:
        values.append(
            _escape_text("[Paragraph content omitted at safe rendering limit]")
        )
    return "".join(values)


def _run(text: str, base: CharacterStyle, run: CharacterStyle) -> str:
    effective = _merge_character_style(base, run)
    value = _escape_text(_clean(text)).replace("\n", "  \n")
    if not value:
        return ""
    if effective.superscript:
        value = f"<sup>{value}</sup>"
    elif effective.subscript:
        value = f"<sub>{value}</sub>"
    if effective.underline:
        value = f"<u>{value}</u>"
    if effective.strike:
        value = f"~~{value}~~"
    if effective.italic:
        value = f"*{value}*"
    if effective.bold:
        value = f"**{value}**"
    return value


def _table(
    document: Document,
    table: Table,
    text_budget: _TextOutputBudget,
) -> str:
    rows = table.rows
    if not isinstance(rows, list | tuple):
        return _escape_text("[Invalid table rows omitted]")
    seen_rows: set[int] = set()
    safe_rows: list[TableRow] = []
    omitted = len(rows) > _MAX_TABLE_ROWS
    for row in rows[:_MAX_TABLE_ROWS]:
        if not isinstance(row, TableRow) or id(row) in seen_rows:
            omitted = True
            continue
        seen_rows.add(id(row))
        safe_rows.append(row)
    if not safe_rows:
        return _escape_text("[Empty table]")

    matrix: list[list[str]] = []
    seen_cells: set[int] = set()
    for row in safe_rows:
        rendered_row: list[str] = []
        cells = row.cells if isinstance(row.cells, list | tuple) else []
        if len(cells) > 256:
            matrix.append(
                [_escape_text("[Table cells omitted at safe 256-column limit]")]
            )
            omitted = True
            continue
        for cell in cells[:256]:
            if not isinstance(cell, TableCell):
                omitted = True
                continue
            if id(cell) in seen_cells:
                rendered_row.append(_escape_text("[Repeated table cell omitted]"))
                omitted = True
                continue
            seen_cells.add(id(cell))
            blocks = cell.blocks if isinstance(cell.blocks, list | tuple) else []
            paragraphs = [
                _paragraph_inline(document, item, text_budget)
                for item in blocks[:_MAX_RENDER_BLOCKS]
                if isinstance(item, Paragraph)
            ]
            value = "<br>".join(item.replace("\n", "<br>") for item in paragraphs)
            rendered_row.append(value)
            rendered_row.extend(
                ""
                for _ in range(
                    max(1, min(_integer(cell.column_span, 1), 256)) - 1
                )
            )
        matrix.append(rendered_row)

    width = max((len(row) for row in matrix), default=0)
    if width == 0:
        return _escape_text("[Empty table]")
    for row in matrix:
        row.extend("" for _ in range(width - len(row)))

    if safe_rows[0].is_header:
        header = matrix[0]
        body = matrix[1:]
    else:
        header = [""] * width
        body = matrix
    lines = [_markdown_row(header), _markdown_row(["---"] * width)]
    lines.extend(_markdown_row(row) for row in body)
    if omitted:
        lines.append(_escape_text("[Table content omitted at safe rendering limit]"))
    return "\n".join(lines)


def _markdown_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _image_placeholder(image: Image) -> str:
    alt = _safe_label_field(image.alt_text, "Embedded image", maximum=256)
    if image.data is not None:
        detail = f"[Image: {alt} (embedded image data)]"
    elif image.reference:
        reference = _safe_label_field(
            image.reference, "invalid reference omitted", maximum=256
        )
        detail = f"[Image: {alt} (external reference not loaded: {reference})]"
    else:
        detail = f"[Image: {alt}]"
    return _escape_text(detail)


def _wmf_placeholder(graphic: WmfGraphic) -> str:
    try:
        wmf_display_size(graphic)
    except WmfDecodeError:
        return _escape_text("[Invalid WMF preview]")
    alt = _safe_label_field(
        graphic.alt_text, "Embedded WMF preview", maximum=256
    )
    return _escape_text(
        f"[WMF preview: {alt} ({graphic.width_px} x {graphic.height_px} pixels)]"
    )


def _sdw_marker(drawing: SdwDrawing) -> str:
    try:
        sdw_display_size(drawing)
    except SdwDecodeError:
        return _escape_text(_sdw_placeholder(drawing))
    alt = _safe_label_field(drawing.alt_text, "Ami Draw object", maximum=256)
    return _escape_text(f"[{sdw_preview_caption(drawing)}: {alt}]")


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


def _heading_level(style_name: str | None) -> int | None:
    if not isinstance(style_name, str) or not style_name:
        return None
    normalized = " ".join(style_name.strip().split()).casefold()
    match = _HEADING_NUMBER.search(normalized)
    if match:
        return int(match.group(1))
    if normalized in {"title", "document title", "chapter title", "chapter heading"}:
        return 1
    if normalized in {"heading", "head"}:
        return 1
    if normalized in {"subtitle", "sub title", "subhead", "subheading"}:
        return 2
    return None


def _protect_block_prefixes(value: str) -> str:
    value = re.sub(r"(?m)^(\s*)(#{1,6}|>|[-+])(?=\s)", r"\1\\\2", value)
    return re.sub(r"(?m)^(\s*)(\d+)\.(?=\s)", r"\1\2\\.", value)


def _escape_text(value: str) -> str:
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_[\]{}|])", r"\\\1", value)


def _named_character_style(
    document: Document, style_name: str | None
) -> CharacterStyle:
    result = CharacterStyle()
    for definition in _style_chain(document, style_name):
        if isinstance(definition.character, CharacterStyle):
            result = _merge_character_style(result, definition.character)
    return result


def _style_chain(
    document: Document, style_name: str | None
) -> list[StyleDefinition]:
    result: list[StyleDefinition] = []
    seen: set[str] = set()
    current = _find_style(document, style_name)
    while current is not None and len(result) < 64:
        if not isinstance(current.name, str):
            break
        folded_name = current.name.casefold()
        if folded_name in seen:
            break
        seen.add(folded_name)
        result.append(current)
        current = _find_style(document, current.parent)
    result.reverse()
    return result


def _find_style(document: Document, name: str | None) -> StyleDefinition | None:
    if not isinstance(name, str) or not name:
        return None
    styles = getattr(document, "styles", None)
    if not isinstance(styles, dict):
        return None
    if name in styles:
        candidate = styles[name]
        return candidate if isinstance(candidate, StyleDefinition) else None
    folded = name.casefold()
    return next(
        (
            item
            for key, item in styles.items()
            if isinstance(key, str)
            and key.casefold() == folded
            and isinstance(item, StyleDefinition)
        ),
        None,
    )


def _merge_character_style(base: CharacterStyle, override: CharacterStyle) -> CharacterStyle:
    superscript = base.superscript or override.superscript
    return CharacterStyle(
        bold=base.bold or override.bold,
        italic=base.italic or override.italic,
        underline=base.underline or override.underline,
        strike=base.strike or override.strike,
        superscript=superscript,
        subscript=(base.subscript or override.subscript) and not superscript,
        font_family=override.font_family or base.font_family,
        font_size_pt=override.font_size_pt or base.font_size_pt,
        color=override.color or base.color,
    )


def _clean(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return ""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARACTERS.sub("\ufffd", normalized)


def _integer(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default
