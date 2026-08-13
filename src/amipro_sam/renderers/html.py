"""Safe, self-contained HTML rendering of the shared document model."""

from __future__ import annotations

import base64
import html
import math
import re
import struct
import zlib

from ..model import (
    Annotation,
    Block,
    CharacterStyle,
    Diagnostic,
    Document,
    Footer,
    Footnote,
    Frame,
    Header,
    Image,
    PageBreak,
    PageLayout,
    PageVariantGeometry,
    Paragraph,
    SdwDrawing,
    StyleDefinition,
    Table,
    TableCell,
    TableRow,
    TextRun,
    TwipRect,
    UnsupportedObject,
    WmfGraphic,
)
from ..sdw import SdwDecodeError, sdw_display_size, sdw_png, sdw_preview_caption
from ..wmf import WmfDecodeError, wmf_display_size, wmf_png

__all__ = ["render"]


_HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})\Z")
_HEADING_NUMBER = re.compile(
    r"(?:^|\b)(?:heading|head|h)\s*[-_:]?\s*([1-6])(?:\b|$)", re.IGNORECASE
)
_MAX_EMBEDDED_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_IMAGE_DIMENSION = 100_000
_MAX_IMAGE_PIXELS = 100_000_000
_MAX_IMAGE_PARTS = 100_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}
_BMP_HEADER_SIZES = {40, 52, 56, 108, 124}
_MAX_RENDER_DEPTH = 32
_MAX_RENDER_BLOCKS = 100_000
_MAX_TABLE_ROWS = 390
_MAX_TABLE_CELLS = 100_000
_MAX_PAGE_TWIPS = 22 * 1_440
_MIN_PAGE_TWIPS = 1_440
_MIN_CONTENT_TWIPS = 720
_DEFAULT_PAGE_BOX = (8.5, 11.0, 1.0, 1.0, 1.0, 1.0)
_CSS = """\
:root{color-scheme:light;--paper:#fff;--ink:#202124;--muted:#5f6368;--line:#c9cdd2;--note:#fff8dc}
*{box-sizing:border-box}
html{background:#eceff1;color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
body{margin:0;padding:2rem}
main,.conversion-warnings{max-width:8.5in;margin:0 auto;background:var(--paper);padding:0.8in;
box-shadow:0 1px 8px #0002}
p,h1,h2,h3,h4,h5,h6{overflow-wrap:anywhere;white-space:pre-wrap}
h1,h2,h3,h4,h5,h6{line-height:1.2}
ol,ul{overflow-wrap:anywhere}
.level-1{margin-left:1.5rem}.level-2{margin-left:3rem}.level-3{margin-left:4.5rem}
.level-4{margin-left:6rem}.level-5{margin-left:7.5rem}.level-6{margin-left:9rem}
.level-7,.level-8,.level-9,.level-10,.level-11,.level-12,.level-13,.level-14,.level-15{
margin-left:10.5rem}
table{border-collapse:collapse;width:100%;margin:1rem 0;table-layout:auto}
th,td{border:1px solid var(--line);padding:.35rem .5rem;text-align:left;vertical-align:top;
overflow-wrap:anywhere;white-space:pre-wrap}
th{background:#f3f5f7;font-weight:600}
.page-break{break-after:page;border:0;border-top:1px dashed #9aa0a6;margin:2rem 0;height:0}
.placeholder{border:1px solid var(--line);background:#f7f7f7;color:var(--muted);
font-style:italic;padding:.55rem .7rem;margin:.75rem 0;overflow-wrap:anywhere;
white-space:pre-wrap}
.annotation,.footnote,.document-header,.document-footer{border-left:.2rem solid var(--line);
padding:.35rem .75rem;margin:.75rem 0;background:#fafafa}
.print-header,.print-footer{display:none}
.document-frame{border:1px solid var(--line);background:#fff;padding:.45rem .7rem;
margin:.75rem 0;max-width:100%;overflow-wrap:anywhere}
.container-label{font-weight:600;color:var(--muted);margin:.15rem 0 .5rem}
figure{margin:1rem 0}figure img{display:block;max-width:100%;height:auto}
figcaption{color:var(--muted);font-size:.9rem;margin-top:.35rem}
.conversion-warnings{margin-top:1.5rem;background:var(--note);box-shadow:none;
border:1px solid #e4d38a;padding:1rem 1.25rem}
.conversion-warnings h2{font-size:1.1rem;margin-top:0}
.conversion-warnings code{overflow-wrap:anywhere}
@media print{html{background:#fff}body{padding:0}main{box-shadow:none;max-width:none}
thead{display:table-header-group}tr{break-inside:avoid;page-break-inside:avoid}
.print-header,.print-footer{display:block;position:fixed;left:0;right:0;font-size:8pt}
.print-header{top:0}.print-footer{bottom:0}
.conversion-warnings{break-before:page}.page-break{visibility:hidden}}
"""


def render(
    document: Document,
    *,
    include_warnings: bool = True,
    **_options: object,
) -> bytes:
    """Return a complete UTF-8 HTML document.

    Source strings are always escaped.  No external files are opened, and an
    image is embedded only when its bytes pass a bounded structural validator
    for PNG, JPEG, GIF, or BMP.
    """

    title = _document_title(document)
    language = _language(document)
    promoted_furniture = _promoted_layout_furniture(document)
    body = _render_blocks(document, promoted_furniture=promoted_furniture)
    furniture = _layout_furniture(document, promoted_furniture)
    page_css = _page_css(document)
    warnings = _warnings(document) if include_warnings and document.diagnostics else ""
    result = (
        "<!doctype html>\n"
        f'<html lang="{_attribute(language)}">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta http-equiv="Content-Security-Policy" '
        'content="default-src &#39;none&#39;; img-src data:; style-src &#39;unsafe-inline&#39;; '
        'base-uri &#39;none&#39;; form-action &#39;none&#39;; object-src &#39;none&#39;">\n'
        '<meta name="referrer" content="no-referrer">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_text(title)}</title>\n"
        f"<style>{_CSS}{page_css}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{furniture}<main>\n{body}</main>\n"
        f"{warnings}"
        "</body>\n"
        "</html>\n"
    )
    return result.encode("utf-8", errors="backslashreplace")


def _render_blocks(
    document: Document,
    blocks: list[Block] | None = None,
    *,
    depth: int = 0,
    active: set[int] | None = None,
    promoted_furniture: set[int] | None = None,
) -> str:
    if depth >= _MAX_RENDER_DEPTH:
        return '<div class="placeholder">[Nested content omitted at safe depth limit]</div>\n'
    result: list[str] = []
    blocks = _safe_blocks(document.blocks if blocks is None else blocks)
    blocks = [block for block in blocks if id(block) not in promoted_furniture]
    blocks = _trim_edge_page_breaks(blocks)
    active = set() if active is None else active
    promoted_furniture = set() if promoted_furniture is None else promoted_furniture
    has_content = False
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if isinstance(block, Paragraph):
            if block.page_break_before and has_content:
                result.append(_page_break())
            if block.list_kind is not None:
                kind = block.list_kind
                level = max(0, min(_integer(block.list_level, 0), 15))
                items = [block]
                index += 1
                while index < len(blocks):
                    candidate = blocks[index]
                    if not isinstance(candidate, Paragraph):
                        break
                    candidate_level = max(
                        0, min(_integer(candidate.list_level, 0), 15)
                    )
                    if (
                        candidate.page_break_before
                        or candidate.list_kind != kind
                        or candidate_level != level
                    ):
                        break
                    items.append(candidate)
                    index += 1
                result.append(_list(document, kind, level, items))
                has_content = True
                continue
            result.append(_paragraph(document, block))
        elif isinstance(block, PageBreak):
            result.append(_page_break())
        elif isinstance(block, Table):
            result.append(_table(document, block))
        elif isinstance(block, Image):
            result.append(_image(block))
        elif isinstance(block, WmfGraphic):
            result.append(_wmf_graphic(block))
        elif isinstance(block, SdwDrawing):
            result.append(_sdw_graphic(block))
        elif isinstance(block, Frame):
            result.append(
                _frame(
                    document,
                    block,
                    depth=depth,
                    active=active,
                    promoted_furniture=promoted_furniture,
                )
            )
        elif isinstance(block, UnsupportedObject):
            label = f"Unsupported {block.kind}: {block.description}"
            result.append(f'<div class="placeholder">[{_text(label)}]</div>\n')
        elif isinstance(block, Annotation):
            nested = _nested_html(
                document,
                block,
                block.blocks,
                depth,
                active,
                promoted_furniture,
            )
            result.append(
                '<aside class="annotation" role="note">\n'
                '<p class="container-label">Annotation</p>\n'
                f"{nested}</aside>\n"
            )
        elif isinstance(block, Footnote):
            label = f"Footnote {block.number}" if block.number is not None else "Footnote"
            nested = _nested_html(
                document,
                block,
                block.blocks,
                depth,
                active,
                promoted_furniture,
            )
            result.append(
                '<aside class="footnote" role="doc-footnote">\n'
                f'<p class="container-label">{_text(label)}</p>\n'
                f"{nested}</aside>\n"
            )
        elif isinstance(block, Header | Footer):
            if id(block) in promoted_furniture:
                index += 1
                continue
            tag = "header" if isinstance(block, Header) else "footer"
            css_class = "document-header" if tag == "header" else "document-footer"
            placement = _choice(block.placement, {"all", "odd", "even", "odd-even"}, "unknown")
            label = f"{tag.title()} - {_placement_label(placement)}"
            nested = _nested_html(
                document,
                block,
                block.blocks,
                depth,
                active,
                promoted_furniture,
            )
            result.append(
                f'<{tag} class="{css_class}" data-placement="{_attribute(placement)}">\n'
                f'<p class="container-label">{_text(label)}</p>\n'
                f"{nested}</{tag}>\n"
            )
        if not isinstance(block, PageBreak):
            has_content = True
        index += 1
    return "".join(result)


def _frame(
    document: Document,
    frame: Frame,
    *,
    depth: int,
    active: set[int],
    promoted_furniture: set[int],
) -> str:
    kind = _choice(frame.content_kind, {"text", "table", "image", "drawing"}, "unknown")
    placement = _choice(
        frame.placement, {"anchored", "fixed-page", "repeating"}, "unknown"
    )
    region = _choice(frame.region, {"body", "header", "footer"}, "unknown")
    label = f"Frame - {placement}; {kind}; {region} region"
    page_number = frame.page_number
    if type(page_number) is int and 1 <= page_number <= 1_000_000:
        label += f"; source page {page_number}"
    style = ""
    width = _frame_width_inches(frame)
    if width is not None:
        style = f' style="width:{width:g}in"'
    nested = _nested_html(
        document,
        frame,
        frame.blocks,
        depth,
        active,
        promoted_furniture,
    )
    return (
        f'<section class="document-frame" data-placement="{_attribute(placement)}"'
        f' data-content-kind="{_attribute(kind)}"{style}>\n'
        f'<p class="container-label">{_text(label)}</p>\n'
        f"{nested}</section>\n"
    )


def _nested_html(
    document: Document,
    owner: object,
    blocks: object,
    depth: int,
    active: set[int],
    promoted_furniture: set[int],
) -> str:
    identity = id(owner)
    if identity in active:
        return '<div class="placeholder">[Repeated or recursive content omitted]</div>\n'
    active.add(identity)
    return _render_blocks(
        document,
        blocks,  # type: ignore[arg-type]
        depth=depth + 1,
        active=active,
        promoted_furniture=promoted_furniture,
    )


def _safe_blocks(value: object) -> list[Block]:
    if not isinstance(value, list | tuple):
        return []
    return list(value[:_MAX_RENDER_BLOCKS])


def _trim_edge_page_breaks(blocks: list[Block]) -> list[Block]:
    start = 0
    end = len(blocks)
    while start < end and isinstance(blocks[start], PageBreak):
        start += 1
    while end > start and isinstance(blocks[end - 1], PageBreak):
        end -= 1
    return blocks[start:end]


def _choice(value: object, choices: set[str], default: str) -> str:
    return value if isinstance(value, str) and value in choices else default


def _layout_furniture(document: Document, promoted: set[int]) -> str:
    parts: list[str] = []
    for block in _safe_blocks(document.blocks):
        if not isinstance(block, Header | Footer) or id(block) not in promoted:
            continue
        tag = "header" if isinstance(block, Header) else "footer"
        placement = _choice(block.placement, {"all", "odd", "even", "odd-even"}, "unknown")
        parts.append(
            f'<{tag} class="print-{tag}" data-placement="{_attribute(placement)}">\n'
            f"{_render_blocks(document, block.blocks, promoted_furniture=promoted)}</{tag}>\n"
        )
    return "".join(parts)


def _promoted_layout_furniture(document: Document) -> set[int]:
    """Keep page furniture inline in HTML.

    Browser fixed-position boxes do not reserve space in the print content
    area and cannot reliably represent Ami Pro's odd/even variants.  The page
    geometry still becomes bounded ``@page`` CSS, while every header/footer
    remains visible once in source order without overlap or duplication.
    """

    return set()


def _safe_html_furniture(
    block: Header | Footer,
    page_box: tuple[float, float, float, float, float, float],
    margin: float,
) -> bool:
    blocks = block.blocks
    if not isinstance(blocks, list | tuple) or not 1 <= len(blocks) <= 8:
        return False
    if not all(isinstance(item, Paragraph) for item in blocks):
        return False
    width = page_box[0] - page_box[3] - page_box[5]
    characters_per_line = max(20, int(width * 10))
    line_count = 0
    total_characters = 0
    for paragraph in blocks:
        try:
            value = paragraph.text
        except (AttributeError, TypeError):
            return False
        if not isinstance(value, str) or len(value) > 4_096:
            return False
        total_characters += len(value)
        if total_characters > 8_192:
            return False
        lines = value.splitlines() or [""]
        line_count += sum(max(1, math.ceil(len(line) / characters_per_line)) for line in lines)
    # Browser fixed-position line boxes and the normal body region need real
    # clearance, not merely the nominal font height.  Half an inch is the
    # smallest margin for which a one-line repeated item is promoted.
    required_margin = max(0.5, 0.125 + line_count * 0.25)
    return margin >= required_margin


def _frame_width_inches(frame: Frame) -> float | None:
    bounds = frame.bounds
    if not isinstance(bounds, TwipRect) or bounds.valid is not True or not bounds.is_usable:
        return None
    width = bounds.width_twips
    # Narrow absolute source frames are reflowed at the document width.  Using
    # their literal width can turn a few kilobytes of text into hundreds of
    # print pages in a browser.
    if type(width) is not int or not 1_800 <= width <= _MAX_PAGE_TWIPS:
        return None
    return width / 1440.0


def _placement_label(value: str) -> str:
    return {
        "all": "all pages",
        "odd": "odd/right pages",
        "even": "even/left pages",
        "odd-even": "odd and even variants",
        "unknown": "placement unknown",
    }.get(value, "placement unknown")


def _paragraph(document: Document, paragraph: Paragraph) -> str:
    heading = _heading_level(paragraph.style_name)
    tag = f"h{heading}" if heading else "p"
    attributes = _paragraph_attributes(document, paragraph)
    content = _paragraph_content(document, paragraph)
    return f"<{tag}{attributes}>{content}</{tag}>\n"


def _list(
    document: Document,
    kind: str,
    level: int,
    items: list[Paragraph],
) -> str:
    tag = "ol" if kind == "number" else "ul"
    class_name = f' class="level-{level}"' if level else ""
    parts = [f"<{tag}{class_name}>\n"]
    for item in items:
        attributes = _paragraph_attributes(document, item, omit_indentation=True)
        parts.append(f"<li{attributes}>{_paragraph_content(document, item)}</li>\n")
    parts.append(f"</{tag}>\n")
    return "".join(parts)


def _paragraph_content(document: Document, paragraph: Paragraph) -> str:
    base = _named_character_style(document, paragraph.style_name)
    runs = paragraph.runs
    if not isinstance(runs, list | tuple):
        return _text(paragraph.text)
    parts: list[str] = []
    for run in runs[:_MAX_RENDER_BLOCKS]:
        if not isinstance(run, TextRun) or not isinstance(run.style, CharacterStyle):
            parts.append(_text("[Invalid text run omitted]"))
            continue
        value = run.text
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if not isinstance(value, str):
            parts.append(_text("[Invalid text run omitted]"))
            continue
        parts.append(_run(value, base, run.style))
    return "".join(parts)


def _run(text: str, base: CharacterStyle, run: CharacterStyle) -> str:
    style = _merge_character_style(base, run)
    value = _text(text).replace("\n", "<br>\n")
    if not value:
        return ""

    css: list[str] = []
    family = _font_stack(style.font_family)
    if family:
        css.append(f"font-family:{family}")
    size = _number(style.font_size_pt, minimum=1.0, maximum=200.0)
    if size is not None:
        css.append(f"font-size:{size:g}pt")
    color = _color(style.color)
    if color:
        css.append(f"color:{color}")
    if css:
        value = f'<span style="{";".join(css)}">{value}</span>'
    if style.underline:
        value = f"<u>{value}</u>"
    if style.strike:
        value = f"<s>{value}</s>"
    if style.superscript:
        value = f"<sup>{value}</sup>"
    elif style.subscript:
        value = f"<sub>{value}</sub>"
    if style.italic:
        value = f"<em>{value}</em>"
    if style.bold:
        value = f"<strong>{value}</strong>"
    return value


def _paragraph_attributes(
    document: Document,
    paragraph: Paragraph,
    *,
    omit_indentation: bool = False,
) -> str:
    definitions = _style_chain(document, paragraph.style_name)

    def inherited(name: str) -> float | str | None:
        result: float | str | None = None
        for definition in definitions:
            candidate = getattr(definition, name)
            if candidate is not None:
                result = candidate
        explicit = getattr(paragraph, name)
        return explicit if explicit is not None else result

    css: list[str] = []
    alignment = inherited("alignment")
    if alignment in {"left", "right", "center", "justify"}:
        css.append(f"text-align:{alignment}")
    if not omit_indentation:
        for attribute, css_name in (
            ("left_indent_in", "margin-left"),
            ("right_indent_in", "margin-right"),
            ("first_line_indent_in", "text-indent"),
        ):
            number = _number(inherited(attribute), minimum=-20.0, maximum=20.0)
            if number is not None:
                css.append(f"{css_name}:{number:g}in")
    for attribute, css_name in (
        ("space_before_pt", "margin-top"),
        ("space_after_pt", "margin-bottom"),
    ):
        number = _number(inherited(attribute), minimum=0.0, maximum=720.0)
        if number is not None:
            css.append(f"{css_name}:{number:g}pt")
    line_spacing = _number(inherited("line_spacing"), minimum=0.5, maximum=10.0)
    if line_spacing is not None:
        css.append(f"line-height:{line_spacing:g}")
    if paragraph.keep_with_next:
        css.append("break-after:avoid")
    return f' style="{";".join(css)}"' if css else ""


def _table(document: Document, table: Table) -> str:
    rows = table.rows
    if not isinstance(rows, list | tuple):
        return '<div class="placeholder">[Invalid table rows omitted]</div>\n'
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
        return '<div class="placeholder">[Empty table]</div>\n'
    if any(
        isinstance(row.cells, list | tuple) and len(row.cells) > 256
        for row in safe_rows
    ):
        return (
            '<div class="placeholder">'
            '[Table cells omitted at safe 256-column limit]'
            '</div>\n'
        )
    result = ["<table>\n"]
    header_rows = 0
    for row in safe_rows:
        if not row.is_header:
            break
        header_rows += 1
    seen_cells: set[int] = set()
    if header_rows:
        result.append("<thead>\n")
        for row in safe_rows[:header_rows]:
            result.append(
                _table_row(document, row.cells, header=True, seen_cells=seen_cells)
            )
        result.append("</thead>\n")
    result.append("<tbody>\n")
    for row in safe_rows[header_rows:]:
        result.append(
            _table_row(document, row.cells, header=False, seen_cells=seen_cells)
        )
    result.append("</tbody>\n</table>\n")
    if omitted:
        result.append(
            '<div class="placeholder">[Table content omitted at safe rendering limit]</div>\n'
        )
    return "".join(result)


def _table_row(
    document: Document,
    cells: list[object],
    *,
    header: bool,
    seen_cells: set[int],
) -> str:
    tag = "th" if header else "td"
    result = ["<tr>"]
    if not isinstance(cells, list | tuple):
        return '<tr><td><div class="placeholder">[Invalid table row omitted]</div></td></tr>\n'
    for cell in cells[: min(_MAX_TABLE_CELLS, 256)]:
        if not isinstance(cell, TableCell):
            continue
        if id(cell) in seen_cells:
            result.append(
                f'<{tag}><div class="placeholder">[Repeated table cell omitted]</div></{tag}>'
            )
            continue
        seen_cells.add(id(cell))
        column_span = max(1, min(_integer(getattr(cell, "column_span", 1), 1), 256))
        row_span = max(1, min(_integer(getattr(cell, "row_span", 1), 1), 65534))
        attributes = ""
        if column_span > 1:
            attributes += f' colspan="{column_span}"'
        if row_span > 1:
            attributes += f' rowspan="{row_span}"'
        blocks = getattr(cell, "blocks", [])
        safe_blocks = _safe_blocks(blocks)[:4_096]
        paragraphs: list[Paragraph] = []
        seen_paragraphs: set[int] = set()
        for item in safe_blocks:
            if not isinstance(item, Paragraph) or id(item) in seen_paragraphs:
                continue
            seen_paragraphs.add(id(item))
            paragraphs.append(item)
        invalid_content = (
            not isinstance(blocks, list | tuple)
            or len(paragraphs) != len(safe_blocks)
            or (isinstance(blocks, list | tuple) and len(blocks) > 4_096)
        )
        content = (
            '<div class="placeholder">[Invalid table cell content omitted]</div>'
            if invalid_content
            else ""
        )
        content += "".join(_paragraph(document, item) for item in paragraphs)
        result.append(f"<{tag}{attributes}>{content}</{tag}>")
    result.append("</tr>\n")
    return "".join(result)


def _image(image: Image) -> str:
    validated = _validated_image(image.data)
    alt = image.alt_text or "Embedded image"
    if validated is None:
        if image.data is not None:
            reason = "embedded data was not a validated PNG, JPEG, GIF, or BMP"
        elif image.reference:
            reason = f"external reference not loaded: {image.reference}"
        else:
            reason = "image data was unavailable"
        return (
            '<div class="placeholder">'
            f"[Image: {_text(alt)} ({_text(reason)})]"
            "</div>\n"
        )

    media_type, data = validated
    encoded = base64.b64encode(data).decode("ascii")
    css: list[str] = []
    width = _number(image.width_in, minimum=0.01, maximum=100.0)
    height = _number(image.height_in, minimum=0.01, maximum=100.0)
    if width is not None:
        css.append(f"width:{width:g}in")
    if height is not None:
        css.append(f"height:{height:g}in")
    style = f' style="{";".join(css)}"' if css else ""
    return (
        "<figure>"
        f'<img src="data:{media_type};base64,{encoded}" alt="{_attribute(alt)}"{style}>'
        f"<figcaption>{_text(alt)}</figcaption>"
        "</figure>\n"
    )


def _wmf_graphic(graphic: WmfGraphic) -> str:
    try:
        data = wmf_png(graphic)
        width, height = wmf_display_size(graphic)
    except WmfDecodeError:
        return '<div class="placeholder">[Invalid WMF preview]</div>\n'
    encoded = base64.b64encode(data).decode("ascii")
    alt = graphic.alt_text or "Embedded WMF preview"
    return (
        '<figure class="wmf-preview">'
        f'<img src="data:image/png;base64,{encoded}" alt="{_attribute(alt)}" '
        f'style="width:{width:g}in;height:{height:g}in">'
        f"<figcaption>{_text(alt)}</figcaption>"
        "</figure>\n"
    )


def _sdw_graphic(drawing: SdwDrawing) -> str:
    try:
        data = sdw_png(drawing)
        width, height = sdw_display_size(drawing)
    except SdwDecodeError:
        return f'<div class="placeholder">{_text(_sdw_placeholder(drawing))}</div>\n'
    if (
        not isinstance(data, bytes)
        or not data.startswith(_PNG_SIGNATURE)
        or not _valid_display_size(width, height)
    ):
        return f'<div class="placeholder">{_text(_sdw_placeholder(drawing))}</div>\n'
    encoded = base64.b64encode(data).decode("ascii")
    alt = _safe_sdw_field(drawing.alt_text, "Ami Draw object", maximum=256)
    return (
        '<figure class="sdw-preview">'
        f'<img src="data:image/png;base64,{encoded}" alt="{_attribute(alt)}" '
        f'style="width:{width:g}in;height:{height:g}in">'
        f"<figcaption>{_text(sdw_preview_caption(drawing))}</figcaption>"
        "</figure>\n"
    )


def _sdw_placeholder(drawing: SdwDrawing) -> str:
    alt = _safe_sdw_field(drawing.alt_text, "Ami Draw object", maximum=256)
    status = _safe_sdw_field(drawing.status, "unavailable", maximum=64)
    reason = _safe_sdw_field(drawing.reason, "preview unavailable", maximum=256)
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


def _safe_sdw_field(value: object, default: str, *, maximum: int) -> str:
    if isinstance(value, str):
        result = value
    elif isinstance(value, bytes):
        result = value.decode("utf-8", errors="replace")
    elif isinstance(value, (bool, int, float)):
        try:
            result = str(value)
        except (TypeError, ValueError, OverflowError):
            return default
    else:
        return default
    result = " ".join(result.split())[:maximum]
    return result or default


def _valid_display_size(width: object, height: object) -> bool:
    return (
        isinstance(width, int | float)
        and not isinstance(width, bool)
        and isinstance(height, int | float)
        and not isinstance(height, bool)
        and math.isfinite(width)
        and math.isfinite(height)
        and 0.0 < width <= 100.0
        and 0.0 < height <= 100.0
    )


def _warnings(document: Document) -> str:
    parts = [
        '<aside class="conversion-warnings" aria-label="Conversion warnings">\n',
        "<h2>Conversion warnings</h2>\n<ul>\n",
    ]
    for diagnostic in document.diagnostics:
        location = ""
        if diagnostic.source is not None:
            location = (
                f" at line {diagnostic.source.line}, column "
                f"{diagnostic.source.column}"
            )
        parts.append(
            "<li>"
            f"<strong>{_text(_diagnostic_severity(diagnostic))}</strong> "
            f"<code>{_text(diagnostic.code)}</code>{_text(location)}: "
            f"{_text(diagnostic.message)}"
            "</li>\n"
        )
    parts.append("</ul>\n</aside>\n")
    return "".join(parts)


def _diagnostic_severity(diagnostic: Diagnostic) -> str:
    value = getattr(diagnostic.severity, "value", diagnostic.severity)
    return str(value).capitalize()


def _page_break() -> str:
    return '<hr class="page-break" aria-label="Page break">\n'


def _page_css(document: Document) -> str:
    layout, page_box, odd, even = _page_layout_boxes(document)
    if layout is not None and layout.non_alternating is True:
        even = None
    width, height, top, right, bottom, left = page_box or _DEFAULT_PAGE_BOX
    result = (
        f"\n@page{{size:{width:g}in {height:g}in;"
        f"margin:{top:g}in {right:g}in {bottom:g}in {left:g}in}}\n"
    )
    for selector, candidate in (("right", odd), ("left", even)):
        if candidate is None or candidate[:2] != (width, height):
            continue
        result += (
            f"@page:{selector}{{margin:{candidate[2]:g}in {candidate[3]:g}in "
            f"{candidate[4]:g}in {candidate[5]:g}in}}\n"
        )
    return (
        result
        + f"main,.conversion-warnings{{max-width:{width:g}in;"
        f"padding:{top:g}in {right:g}in {bottom:g}in {left:g}in}}\n"
        "@media print{main,.conversion-warnings{padding:0}}\n"
    )


def _page_box(document: Document) -> tuple[float, float, float, float, float, float]:
    _layout, page_box, _odd, _even = _page_layout_boxes(document)
    return page_box or _DEFAULT_PAGE_BOX


def _page_layout_boxes(
    document: Document,
) -> tuple[
    PageLayout | None,
    tuple[float, float, float, float, float, float] | None,
    tuple[float, float, float, float, float, float] | None,
    tuple[float, float, float, float, float, float] | None,
]:
    layouts = getattr(document, "page_layouts", None)
    if not isinstance(layouts, list | tuple):
        return None, None, None, None
    for index, layout in enumerate(layouts):
        if index >= 256:
            break
        if not isinstance(layout, PageLayout) or layout.valid is not True:
            continue
        odd = _validated_page_box(layout.odd)
        even = _validated_page_box(layout.even)
        primary = odd or even
        if primary is not None:
            return layout, primary, odd, even
    return None, None, None, None


def _validated_page_box(
    geometry: object,
) -> tuple[float, float, float, float, float, float] | None:
    if not isinstance(geometry, PageVariantGeometry) or geometry.valid is not True:
        return None
    values = (
        geometry.width_twips,
        geometry.height_twips,
        geometry.margin_top_twips,
        geometry.margin_right_twips,
        geometry.margin_bottom_twips,
        geometry.margin_left_twips,
    )
    if not all(type(value) is int for value in values):
        return None
    width, height, top, right, bottom, left = values
    assert all(isinstance(value, int) for value in values)
    if not (
        _MIN_PAGE_TWIPS <= width <= _MAX_PAGE_TWIPS
        and _MIN_PAGE_TWIPS <= height <= _MAX_PAGE_TWIPS
        and min(top, right, bottom, left) >= 0
        and max(top, right, bottom, left) <= _MAX_PAGE_TWIPS
        and width - left - right >= _MIN_CONTENT_TWIPS
        and height - top - bottom >= _MIN_CONTENT_TWIPS
    ):
        return None
    return tuple(value / 1440.0 for value in values)  # type: ignore[return-value]


def _document_title(document: Document) -> str:
    for key in ("title", "Title", "subject", "Subject"):
        value = document.metadata.get(key)
        if value:
            return value
    return document.source_name or "Converted Ami Pro document"


def _language(document: Document) -> str:
    value = document.metadata.get("language") or document.metadata.get("lang") or "en"
    # BCP-47 is broader, but this subset avoids putting arbitrary content in
    # the root attribute and covers common preservation metadata.
    return value if re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", value) else "en"


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
    return next(
        (item for key, item in document.styles.items() if key.casefold() == folded),
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


def _font_stack(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.casefold()
    if any(item in normalized for item in ("courier", "mono", "console", "typewriter")):
        return "ui-monospace,monospace"
    if any(item in normalized for item in ("times", "roman", "serif", "garamond", "bookman")):
        return "Georgia,serif"
    return "Arial,sans-serif"


def _color(value: str | None) -> str | None:
    if not value:
        return None
    match = _HEX_COLOR.fullmatch(value.strip())
    return f"#{match.group(1).lower()}" if match else None


def _number(value: object, *, minimum: float, maximum: float) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    if number is None or not math.isfinite(number):
        return None
    return min(maximum, max(minimum, number))


def _integer(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _clean(value: str) -> str:
    normalized = str(value).replace("\r\n", "\n").replace("\r", "\n")
    result: list[str] = []
    for character in normalized:
        codepoint = ord(character)
        if (
            (codepoint < 32 and character not in {"\n", "\t"})
            or 0x7F <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
        ):
            result.append("\ufffd")
        else:
            result.append(character)
    return "".join(result)


def _text(value: object) -> str:
    return html.escape(_clean(str(value)), quote=False)


def _attribute(value: object) -> str:
    return html.escape(_clean(str(value)), quote=True)


def _validated_image(data: bytes | None) -> tuple[str, bytes] | None:
    if not isinstance(data, bytes) or not data or len(data) > _MAX_EMBEDDED_IMAGE_BYTES:
        return None
    if data.startswith(_PNG_SIGNATURE) and _valid_png(data):
        return "image/png", data
    if data.startswith(b"\xff\xd8") and _valid_jpeg(data):
        return "image/jpeg", data
    if data[:6] in {b"GIF87a", b"GIF89a"} and _valid_gif(data):
        return "image/gif", data
    if data.startswith(b"BM") and _valid_bmp(data):
        return "image/bmp", data
    return None


def _valid_dimensions(width: int, height: int) -> bool:
    return (
        0 < width <= _MAX_IMAGE_DIMENSION
        and 0 < height <= _MAX_IMAGE_DIMENSION
        and width * height <= _MAX_IMAGE_PIXELS
    )


def _valid_bmp(data: bytes) -> bool:
    """Validate a bounded Windows DIB without decoding its pixels.

    Ami Pro's parser currently recovers BMP assets.  Only the uncompressed
    BI_RGB and BI_BITFIELDS variants are accepted here; RLE, JPEG/PNG-in-BMP,
    OS/2 headers, device-linked profiles, and truncated pixel arrays stay
    visible as placeholders.
    """

    if len(data) < 58 or data[:2] != b"BM":
        return False
    try:
        file_size, reserved_one, reserved_two, pixel_offset = struct.unpack_from(
            "<IHHI", data, 2
        )
        dib_size = struct.unpack_from("<I", data, 14)[0]
    except struct.error:
        return False
    if (
        file_size != len(data)
        or reserved_one != 0
        or reserved_two != 0
        or dib_size not in _BMP_HEADER_SIZES
        or 14 + dib_size > len(data)
    ):
        return False

    try:
        width, signed_height, planes, bits_per_pixel, compression, image_size = (
            struct.unpack_from("<iiHHII", data, 18)
        )
        colors_used = struct.unpack_from("<I", data, 46)[0]
    except struct.error:
        return False
    height = abs(signed_height)
    if (
        width <= 0
        or signed_height == 0
        or not _valid_dimensions(width, height)
        or planes != 1
        or compression not in {0, 3}  # BI_RGB or BI_BITFIELDS
    ):
        return False
    if compression == 0 and bits_per_pixel not in {1, 4, 8, 16, 24, 32}:
        return False
    if compression == 3 and bits_per_pixel not in {16, 32}:
        return False

    header_end = 14 + dib_size
    masks_end = header_end
    if compression == 3:
        # BITMAPINFOHEADER stores its masks immediately after the 40-byte DIB;
        # later Windows headers include them at the same offsets in the DIB.
        mask_offset = 14 + 40
        masks_end = max(masks_end, mask_offset + 12)
        if masks_end > len(data):
            return False
        red, green, blue = struct.unpack_from("<III", data, mask_offset)
        channel_limit = (1 << bits_per_pixel) - 1
        if (
            not red
            or not green
            or not blue
            or (red | green | blue) > channel_limit
            or red & green
            or red & blue
            or green & blue
            or not all(_contiguous_mask(item) for item in (red, green, blue))
        ):
            return False

    maximum_palette = 1 << bits_per_pixel if bits_per_pixel <= 8 else 0
    if bits_per_pixel <= 8:
        palette_entries = colors_used or maximum_palette
        if palette_entries > maximum_palette:
            return False
    else:
        # Some writers attach a small optimization palette to high-color BMPs.
        # Bound it by both an ordinary byte-sized palette and the file itself.
        if colors_used > 256:
            return False
        palette_entries = colors_used
    palette_end = masks_end + palette_entries * 4
    if pixel_offset < palette_end or pixel_offset > len(data):
        return False

    row_bytes = ((width * bits_per_pixel + 31) // 32) * 4
    required_pixels = row_bytes * height
    if required_pixels <= 0 or pixel_offset + required_pixels > len(data):
        return False
    if image_size not in {0, required_pixels}:
        return False

    # V5 profiles can point to opaque extra payloads.  They are unnecessary for
    # faithful readable display and broaden the parser surface, so reject them.
    if dib_size == 124:
        profile_data, profile_size = struct.unpack_from("<II", data, 14 + 112)
        if profile_data != 0 or profile_size != 0:
            return False
    return True


def _contiguous_mask(mask: int) -> bool:
    while mask and mask & 1 == 0:
        mask >>= 1
    return mask != 0 and mask & (mask + 1) == 0


def _valid_png(data: bytes) -> bool:
    if len(data) < 45 or not data.startswith(_PNG_SIGNATURE):
        return False
    offset = len(_PNG_SIGNATURE)
    parts = 0
    saw_header = False
    saw_data = False
    while offset + 12 <= len(data) and parts < _MAX_IMAGE_PARTS:
        parts += 1
        length = struct.unpack_from(">I", data, offset)[0]
        chunk_type = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data) or not all(
            65 <= character <= 90 or 97 <= character <= 122 for character in chunk_type
        ):
            return False
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(payload, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            return False
        if not saw_header:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width, height, depth, color_type, compression, filter_method, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                not _valid_dimensions(width, height)
                or depth not in valid_depths.get(color_type, set())
                or compression != 0
                or filter_method != 0
                or interlace not in {0, 1}
            ):
                return False
            saw_header = True
        elif chunk_type == b"IHDR":
            return False
        if chunk_type == b"IDAT":
            saw_data = True
        if chunk_type == b"IEND":
            return length == 0 and saw_header and saw_data and end == len(data)
        offset = end
    return False


def _valid_gif(data: bytes) -> bool:
    if len(data) < 14 or data[:6] not in {b"GIF87a", b"GIF89a"}:
        return False
    width, height = struct.unpack_from("<HH", data, 6)
    if not _valid_dimensions(width, height):
        return False
    packed = data[10]
    offset = 13
    if packed & 0x80:
        offset += 3 * (2 ** ((packed & 0x07) + 1))
    saw_image = False
    parts = 0
    while offset < len(data) and parts < _MAX_IMAGE_PARTS:
        parts += 1
        marker = data[offset]
        offset += 1
        if marker == 0x3B:
            return saw_image and offset == len(data)
        if marker == 0x2C:
            if offset + 9 > len(data):
                return False
            image_width, image_height = struct.unpack_from("<HH", data, offset + 4)
            if not _valid_dimensions(image_width, image_height):
                return False
            image_packed = data[offset + 8]
            offset += 9
            if image_packed & 0x80:
                offset += 3 * (2 ** ((image_packed & 0x07) + 1))
            if offset >= len(data) or not 2 <= data[offset] <= 12:
                return False
            offset += 1
            offset = _skip_sub_blocks(data, offset)
            if offset < 0:
                return False
            saw_image = True
        elif marker == 0x21:
            if offset >= len(data):
                return False
            offset += 1  # Extension function byte.
            offset = _skip_sub_blocks(data, offset)
            if offset < 0:
                return False
        else:
            return False
    return False


def _skip_sub_blocks(data: bytes, offset: int) -> int:
    parts = 0
    while offset < len(data) and parts < _MAX_IMAGE_PARTS:
        parts += 1
        size = data[offset]
        offset += 1
        if size == 0:
            return offset
        if offset + size > len(data):
            return -1
        offset += size
    return -1


def _valid_jpeg(data: bytes) -> bool:
    if len(data) < 14 or not data.startswith(b"\xff\xd8"):
        return False
    offset = 2
    saw_frame = False
    saw_scan = False
    parts = 0
    in_scan = False
    while offset < len(data) and parts < _MAX_IMAGE_PARTS:
        parts += 1
        if in_scan:
            marker_at = data.find(b"\xff", offset)
            if marker_at < 0 or marker_at + 1 >= len(data):
                return False
            marker_offset = marker_at
        else:
            if data[offset] != 0xFF:
                return False
            marker_offset = offset

        while marker_offset < len(data) and data[marker_offset] == 0xFF:
            marker_offset += 1
        if marker_offset >= len(data):
            return False
        marker = data[marker_offset]
        offset = marker_offset + 1
        if in_scan and marker == 0x00:
            in_scan = True
            continue
        if 0xD0 <= marker <= 0xD7:
            if not in_scan:
                return False
            continue
        in_scan = False
        if marker == 0xD9:
            return saw_frame and saw_scan and offset == len(data)
        if marker in {0x01, 0xD8}:
            return False
        if offset + 2 > len(data):
            return False
        length = struct.unpack_from(">H", data, offset)[0]
        if length < 2 or offset + length > len(data):
            return False
        payload_start = offset + 2
        payload_end = offset + length
        if marker in _JPEG_SOF_MARKERS:
            if length < 8:
                return False
            height, width = struct.unpack_from(">HH", data, payload_start + 1)
            if not _valid_dimensions(width, height):
                return False
            saw_frame = True
        if marker == 0xDA:
            if not saw_frame or length < 6:
                return False
            saw_scan = True
            in_scan = True
        offset = payload_end
    return False
