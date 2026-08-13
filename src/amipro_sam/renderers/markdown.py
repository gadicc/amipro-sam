"""Conservative CommonMark-oriented rendering."""

from __future__ import annotations

import re

from ..model import (
    Annotation,
    Block,
    CharacterStyle,
    Document,
    Footer,
    Footnote,
    Header,
    Image,
    PageBreak,
    Paragraph,
    StyleDefinition,
    Table,
    UnsupportedObject,
)

__all__ = ["render"]


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HEADING_NUMBER = re.compile(
    r"(?:^|\b)(?:heading|head|h)\s*[-_:]?\s*([1-6])(?:\b|$)", re.IGNORECASE
)


def render(document: Document, **_options: object) -> bytes:
    """Return CommonMark-like Markdown without source-controlled raw HTML."""

    rendered = _render_blocks(document, document.blocks)
    if not rendered:
        return b""
    return (rendered + "\n").encode("utf-8", errors="backslashreplace")


def _render_blocks(document: Document, blocks: list[Block]) -> str:
    chunks: list[str] = []
    counters: dict[int, int] = {}
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if isinstance(block, Paragraph):
            if block.page_break_before:
                chunks.append("[Page break]")
                counters.clear()
            if block.list_kind is not None:
                items: list[str] = []
                while index < len(blocks):
                    candidate = blocks[index]
                    if (
                        not isinstance(candidate, Paragraph)
                        or candidate.list_kind is None
                        or (candidate.page_break_before and items)
                    ):
                        break
                    items.append(_paragraph(document, candidate, counters))
                    index += 1
                chunks.append("\n".join(items))
                continue
            chunks.append(_paragraph(document, block, counters))
        elif isinstance(block, PageBreak):
            chunks.append("[Page break]")
            counters.clear()
        elif isinstance(block, Table):
            chunks.append(_table(document, block))
            counters.clear()
        elif isinstance(block, Image):
            chunks.append(_image_placeholder(block))
            counters.clear()
        elif isinstance(block, UnsupportedObject):
            chunks.append(
                _escape_text(f"[Unsupported {block.kind}: {block.description}]")
            )
            counters.clear()
        elif isinstance(block, Annotation):
            chunks.append(_marked_container("[Annotation]", _render_blocks(document, block.blocks)))
            counters.clear()
        elif isinstance(block, Footnote):
            marker = f"[Footnote {block.number}]" if block.number is not None else "[Footnote]"
            chunks.append(_marked_container(marker, _render_blocks(document, block.blocks)))
            counters.clear()
        elif isinstance(block, Header | Footer):
            kind = "Header" if isinstance(block, Header) else "Footer"
            marker = f"[{kind}: {_placement_label(block.placement)}]"
            chunks.append(_marked_container(marker, _render_blocks(document, block.blocks)))
            counters.clear()
        index += 1

    return "\n\n".join(chunks)


def _marked_container(marker: str, content: str) -> str:
    return f"{marker}\n\n{content}" if content else marker


def _placement_label(value: str) -> str:
    return {
        "all": "all pages",
        "odd": "odd/right pages",
        "even": "even/left pages",
        "odd-even": "odd and even variants",
        "unknown": "placement unknown",
    }.get(value, "placement unknown")


def _paragraph(
    document: Document, paragraph: Paragraph, counters: dict[int, int]
) -> str:
    content = _paragraph_inline(document, paragraph)
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


def _paragraph_inline(document: Document, paragraph: Paragraph) -> str:
    base = _named_character_style(document, paragraph.style_name)
    if not paragraph.runs:
        return ""
    return "".join(_run(run.text, base, run.style) for run in paragraph.runs)


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


def _table(document: Document, table: Table) -> str:
    if not table.rows:
        return _escape_text("[Empty table]")

    matrix: list[list[str]] = []
    for row in table.rows:
        rendered_row: list[str] = []
        for cell in row.cells:
            paragraphs = [_paragraph_inline(document, item) for item in cell.blocks]
            value = "<br>".join(item.replace("\n", "<br>") for item in paragraphs)
            rendered_row.append(value)
            rendered_row.extend(
                "" for _ in range(max(1, _integer(cell.column_span, 1)) - 1)
            )
        matrix.append(rendered_row)

    width = max((len(row) for row in matrix), default=0)
    if width == 0:
        return _escape_text("[Empty table]")
    for row in matrix:
        row.extend("" for _ in range(width - len(row)))

    if table.rows[0].is_header:
        header = matrix[0]
        body = matrix[1:]
    else:
        header = [""] * width
        body = matrix
    lines = [_markdown_row(header), _markdown_row(["---"] * width)]
    lines.extend(_markdown_row(row) for row in body)
    return "\n".join(lines)


def _markdown_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _image_placeholder(image: Image) -> str:
    alt = image.alt_text or "Embedded image"
    if image.data is not None:
        detail = f"[Image: {alt} (embedded image data)]"
    elif image.reference:
        detail = f"[Image: {alt} (external reference not loaded: {image.reference})]"
    else:
        detail = f"[Image: {alt}]"
    return _escape_text(detail)


def _heading_level(style_name: str | None) -> int | None:
    if not style_name:
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
        result = _merge_character_style(result, definition.character)
    return result


def _style_chain(
    document: Document, style_name: str | None
) -> list[StyleDefinition]:
    result: list[StyleDefinition] = []
    seen: set[str] = set()
    current = _find_style(document, style_name)
    while current is not None and current.name.casefold() not in seen and len(result) < 64:
        seen.add(current.name.casefold())
        result.append(current)
        current = _find_style(document, current.parent)
    result.reverse()
    return result


def _find_style(document: Document, name: str | None) -> StyleDefinition | None:
    if not name:
        return None
    if name in document.styles:
        return document.styles[name]
    folded = name.casefold()
    return next((item for key, item in document.styles.items() if key.casefold() == folded), None)


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


def _clean(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARACTERS.sub("\ufffd", normalized)


def _integer(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default
