"""Render the intermediate representation as an in-memory PDF.

Only ReportLab's built-in fonts are used.  In particular, font names and image
references found in a SAM file are never interpreted as paths or opened.
Validated WMF raster data is converted by the toolkit to a fresh in-memory PNG
before ReportLab sees it.
"""

from __future__ import annotations

import math
import re
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Image as ReportLabImage,
)
from reportlab.platypus import (
    PageBreak as ReportLabPageBreak,
)
from reportlab.platypus import (
    Paragraph as ReportLabParagraph,
)
from reportlab.platypus import (
    SimpleDocTemplate,
    TableStyle,
)
from reportlab.platypus import (
    Table as ReportLabTable,
)
from reportlab.platypus.doctemplate import LayoutError

from ..errors import RenderError
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
    SdwDrawing,
    StyleDefinition,
    Table,
    TableCell,
    UnsupportedObject,
    WmfGraphic,
)
from ..sdw import SdwDecodeError, sdw_display_size, sdw_png, sdw_preview_caption
from ..wmf import WmfDecodeError, wmf_display_size, wmf_png

__all__ = ["render"]


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})\Z")
_COVERED = object()
_MAX_TABLE_COLUMNS = 256
_FRAME_WIDTH = 6.5 * inch - 12.0  # SimpleDocTemplate's frame has 6pt side padding.
_MIN_TEXT_WIDTH = 1.25 * inch

class _InvariantCanvas(Canvas):
    """Canvas with stable timestamps, identifiers, and innocuous metadata."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs["invariant"] = 1
        if kwargs.get("pageCompression") is None:
            kwargs["pageCompression"] = 1
        super().__init__(*args, **kwargs)
        self.setTitle("")
        self.setAuthor("")
        self.setSubject("")
        self.setCreator("amipro-sam-toolkit")
        self.setProducer("amipro-sam-toolkit")


def render(document: Document, **_options: object) -> bytes:
    """Return *document* as PDF bytes.

    The renderer deliberately emits placeholders for source Image objects and
    unsupported objects.  A validated WMF preview is passed as a freshly
    generated in-memory PNG; ReportLab never opens a source-supplied filename,
    URL, OLE object, or original embedded payload.
    """

    try:
        return _render_bytes(document, conservative=False)
    except (LayoutError, IndexError) as primary_error:
        if not _is_reportlab_layout_failure(primary_error):
            raise
        # ReportLab has a few layout-sensitive failure modes for extreme legacy
        # indents, leading line breaks, and rows taller than a page.  A plain
        # second pass preserves every block's readable text and page breaks
        # without allowing one pathological construct to abort conversion.
        try:
            return _render_bytes(document, conservative=True)
        except (LayoutError, IndexError) as fallback_error:
            if not _is_reportlab_layout_failure(fallback_error):
                raise
            raise RenderError(
                "Could not render PDF safely, including plain-layout fallback"
            ) from fallback_error


def _render_bytes(document: Document, *, conservative: bool) -> bytes:
    output = BytesIO()
    template = SimpleDocTemplate(
        output,
        pagesize=LETTER,
        leftMargin=inch,
        rightMargin=inch,
        topMargin=inch,
        bottomMargin=inch,
        title="",
        author="",
        subject="",
        creator="amipro-sam-toolkit",
        producer="amipro-sam-toolkit",
        invariant=1,
        pageCompression=1,
    )
    story = _fallback_story(document) if conservative else _primary_story(document)
    if not story:
        story.append(_placeholder_flowable(""))
    template.build(story, canvasmaker=_InvariantCanvas)
    return output.getvalue()


def _primary_story(document: Document) -> list[object]:
    story: list[object] = []
    list_counters: dict[int, int] = {}
    _append_primary_blocks(document, document.blocks, story, list_counters)
    return story


def _append_primary_blocks(
    document: Document,
    blocks: list[Block],
    story: list[object],
    list_counters: dict[int, int],
) -> None:
    for block in blocks:
        if isinstance(block, Paragraph):
            if block.page_break_before:
                story.append(ReportLabPageBreak())
            marker = _list_marker(block, list_counters)
            story.append(_paragraph_flowable(document, block, marker=marker))
        elif isinstance(block, PageBreak):
            story.append(ReportLabPageBreak())
            list_counters.clear()
        elif isinstance(block, Table):
            story.append(_table_flowable(document, block))
            list_counters.clear()
        elif isinstance(block, Image):
            story.append(_placeholder_flowable(_image_placeholder(block)))
            list_counters.clear()
        elif isinstance(block, WmfGraphic):
            story.append(_wmf_flowable(block))
            list_counters.clear()
        elif isinstance(block, SdwDrawing):
            story.extend(_sdw_flowables(block))
            list_counters.clear()
        elif isinstance(block, UnsupportedObject):
            story.append(
                _placeholder_flowable(
                    f"[Unsupported {block.kind}: {block.description}]"
                )
            )
            list_counters.clear()
        elif isinstance(block, Annotation | Footnote | Header | Footer):
            story.append(_placeholder_flowable(_container_label(block)))
            list_counters.clear()
            _append_primary_blocks(document, block.blocks, story, list_counters)


def _fallback_story(document: Document) -> list[object]:
    story: list[object] = []
    list_counters: dict[int, int] = {}
    _append_fallback_blocks(document.blocks, story, list_counters)
    return story


def _append_fallback_blocks(
    blocks: list[Block], story: list[object], list_counters: dict[int, int]
) -> None:
    for block in blocks:
        if isinstance(block, Paragraph):
            if block.page_break_before:
                story.append(ReportLabPageBreak())
            marker = _list_marker(block, list_counters)
            prefix = f"{marker} " if marker else ""
            story.append(_fallback_paragraph(prefix + block.text))
        elif isinstance(block, PageBreak):
            story.append(ReportLabPageBreak())
            list_counters.clear()
        elif isinstance(block, Table):
            list_counters.clear()
            if not block.rows:
                story.append(_fallback_paragraph("[Empty table]"))
            for row in block.rows:
                label = "[Table header] " if row.is_header else "[Table row] "
                story.append(
                    _fallback_paragraph(label + " | ".join(cell.text for cell in row.cells))
                )
        elif isinstance(block, Image):
            story.append(_fallback_paragraph(_image_placeholder(block), placeholder=True))
            list_counters.clear()
        elif isinstance(block, WmfGraphic):
            story.append(_wmf_flowable(block))
            list_counters.clear()
        elif isinstance(block, SdwDrawing):
            story.extend(_sdw_flowables(block))
            list_counters.clear()
        elif isinstance(block, UnsupportedObject):
            story.append(
                _fallback_paragraph(
                    f"[Unsupported {block.kind}: {block.description}]",
                    placeholder=True,
                )
            )
            list_counters.clear()
        elif isinstance(block, Annotation | Footnote | Header | Footer):
            story.append(_fallback_paragraph(_container_label(block), placeholder=True))
            list_counters.clear()
            _append_fallback_blocks(block.blocks, story, list_counters)


def _container_label(block: Annotation | Footnote | Header | Footer) -> str:
    if isinstance(block, Annotation):
        return "[Annotation]"
    if isinstance(block, Footnote):
        return f"[Footnote {block.number}]" if block.number is not None else "[Footnote]"
    kind = "Header" if isinstance(block, Header) else "Footer"
    placement = {
        "all": "all pages",
        "odd": "odd/right pages",
        "even": "even/left pages",
        "odd-even": "odd and even variants",
        "unknown": "placement unknown",
    }.get(block.placement, "placement unknown")
    return f"[{kind}: {placement}]"


def _is_reportlab_layout_failure(error: BaseException) -> bool:
    if isinstance(error, LayoutError):
        return True
    traceback = error.__traceback__
    while traceback is not None:
        if "reportlab" in traceback.tb_frame.f_code.co_filename:
            return True
        traceback = traceback.tb_next
    return False


def _list_marker(paragraph: Paragraph, counters: dict[int, int]) -> str | None:
    if paragraph.list_kind is None:
        counters.clear()
        return None
    level = _safe_level(paragraph.list_level)
    for stale_level in [item for item in counters if item > level]:
        del counters[stale_level]
    if paragraph.list_kind == "number":
        counters[level] = counters.get(level, 0) + 1
        return f"{counters[level]}."
    counters.pop(level, None)
    return "\u2022"


def _paragraph_flowable(
    document: Document,
    paragraph: Paragraph,
    *,
    marker: str | None = None,
    force_bold: bool = False,
) -> ReportLabParagraph:
    style_definition = _resolved_style(document, paragraph.style_name)
    base_character = style_definition.character if style_definition else CharacterStyle()
    if force_bold:
        base_character = base_character.merged(bold=True)
    size = _safe_number(base_character.font_size_pt, default=11.0, minimum=1.0, maximum=200.0)
    alignment = paragraph.alignment or (style_definition.alignment if style_definition else None)
    left_indent = _first_not_none(
        paragraph.left_indent_in,
        style_definition.left_indent_in if style_definition else None,
    )
    right_indent = _first_not_none(
        paragraph.right_indent_in,
        style_definition.right_indent_in if style_definition else None,
    )
    first_indent = _first_not_none(
        paragraph.first_line_indent_in,
        style_definition.first_line_indent_in if style_definition else None,
    )
    before = _first_not_none(
        paragraph.space_before_pt,
        style_definition.space_before_pt if style_definition else None,
    )
    after = _first_not_none(
        paragraph.space_after_pt,
        style_definition.space_after_pt if style_definition else None,
    )
    line_spacing = _first_not_none(
        paragraph.line_spacing,
        style_definition.line_spacing if style_definition else None,
    )

    list_left = 18.0 * (_safe_level(paragraph.list_level) + 1)
    resolved_left = _safe_inches(left_indent)
    if marker is not None and left_indent is None:
        resolved_left = list_left
    resolved_right = _safe_inches(right_indent)
    resolved_first = _safe_inches(first_indent)
    if marker is not None and first_indent is None:
        resolved_first = -12.0
    resolved_left, resolved_right, resolved_first = _safe_paragraph_geometry(
        resolved_left,
        resolved_right,
        resolved_first,
    )

    reportlab_style = ParagraphStyle(
        name="AmiProParagraph",
        fontName=_font_name(base_character),
        fontSize=size,
        leading=_safe_leading(size, line_spacing),
        textColor=_color(base_character.color, colors.black),
        alignment={
            "left": TA_LEFT,
            "right": TA_RIGHT,
            "center": TA_CENTER,
            "justify": TA_JUSTIFY,
        }.get(alignment, TA_LEFT),
        leftIndent=resolved_left,
        rightIndent=resolved_right,
        firstLineIndent=resolved_first,
        bulletIndent=max(0.0, resolved_left - 12.0),
        spaceBefore=_safe_number(before, default=0.0, minimum=0.0, maximum=720.0),
        spaceAfter=_safe_number(after, default=6.0, minimum=0.0, maximum=720.0),
        keepWithNext=bool(paragraph.keep_with_next),
        allowWidows=1,
        allowOrphans=0,
    )
    markup = "".join(_run_markup(run.text, base_character, run.style) for run in paragraph.runs)
    if not markup:
        markup = "&#160;"
    return ReportLabParagraph(markup, reportlab_style, bulletText=marker)


def _run_markup(text: str, base: CharacterStyle, run: CharacterStyle) -> str:
    effective = _merge_character_style(base, run)
    safe_text = escape(_clean_text(text)).replace("\n", "<br/>").replace("\t", "&#160;" * 4)
    if not safe_text:
        return ""

    font_attributes = [
        f'name="{_font_name(effective)}"',
        "size=\""
        f"{_safe_number(effective.font_size_pt, default=11.0, minimum=1.0, maximum=200.0):g}"
        "\"",
    ]
    color = _hex_color(effective.color)
    if color:
        font_attributes.append(f'color="{color}"')
    result = f"<font {' '.join(font_attributes)}>{safe_text}</font>"
    if effective.underline:
        result = f"<u>{result}</u>"
    if effective.strike:
        result = f"<strike>{result}</strike>"
    if effective.superscript:
        result = f"<super>{result}</super>"
    elif effective.subscript:
        result = f"<sub>{result}</sub>"
    return result


def _placeholder_flowable(text: str) -> ReportLabParagraph:
    style = ParagraphStyle(
        name="AmiProPlaceholder",
        fontName="Helvetica-Oblique",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#555555"),
        borderColor=colors.HexColor("#B8B8B8"),
        borderWidth=0.5,
        borderPadding=5,
        backColor=colors.HexColor("#F4F4F4"),
        spaceBefore=4,
        spaceAfter=6,
    )
    safe = escape(_clean_text(text)).replace("\n", "<br/>") or "&#160;"
    return ReportLabParagraph(safe, style)


def _fallback_paragraph(text: str, *, placeholder: bool = False) -> ReportLabParagraph:
    style = ParagraphStyle(
        name="AmiProFallbackPlaceholder" if placeholder else "AmiProFallbackParagraph",
        fontName="Helvetica-Oblique" if placeholder else "Helvetica",
        fontSize=9.0,
        leading=11.0,
        textColor=colors.HexColor("#555555") if placeholder else colors.black,
        spaceBefore=2.0 if placeholder else 0.0,
        spaceAfter=4.0,
        splitLongWords=1,
        allowWidows=1,
        allowOrphans=0,
    )
    safe = escape(_clean_text(text)).replace("\n", "<br/>").replace("\t", "&#160;" * 4)
    return ReportLabParagraph(safe or "&#160;", style)


def _table_flowable(document: Document, table: Table) -> ReportLabTable | ReportLabParagraph:
    grid, anchors = _layout_table(table)
    if not grid or not grid[0]:
        return _placeholder_flowable("[Empty table]")

    data: list[list[object]] = [["" for _ in row] for row in grid]
    commands: list[tuple[object, ...]] = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#777777")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for row_index, column_index, cell, column_span, row_span in anchors:
        contents: list[ReportLabParagraph] = []
        for paragraph in cell.blocks:
            marker = "\u2022" if paragraph.list_kind == "bullet" else None
            if paragraph.list_kind == "number":
                marker = "1."
            contents.append(
                _paragraph_flowable(
                    document,
                    paragraph,
                    marker=marker,
                    force_bold=table.rows[row_index].is_header,
                )
            )
        if not contents:
            contents.append(_paragraph_flowable(document, Paragraph()))
        data[row_index][column_index] = contents
        if column_span > 1 or row_span > 1:
            commands.append(
                (
                    "SPAN",
                    (column_index, row_index),
                    (column_index + column_span - 1, row_index + row_span - 1),
                )
            )

    for row_index, row in enumerate(table.rows):
        if row_index < len(grid) and row.is_header:
            commands.extend(
                [
                    ("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#E8EEF5")),
                    ("FONTNAME", (0, row_index), (-1, row_index), "Helvetica-Bold"),
                ]
            )

    column_width = _FRAME_WIDTH / len(grid[0])
    repeat_rows = 0
    for row in table.rows:
        if not row.is_header:
            break
        repeat_rows += 1
    rendered = ReportLabTable(
        data,
        colWidths=[column_width] * len(grid[0]),
        repeatRows=repeat_rows,
        splitByRow=1,
        splitInRow=1,
        hAlign="LEFT",
    )
    rendered.spaceBefore = 4
    rendered.spaceAfter = 6
    rendered.setStyle(TableStyle(commands))
    return rendered


def _layout_table(
    table: Table,
) -> tuple[list[list[object | TableCell | None]], list[tuple[int, int, TableCell, int, int]]]:
    row_count = len(table.rows)
    if not row_count:
        return [], []
    grid: list[list[object | TableCell | None]] = [[] for _ in range(row_count)]
    anchors: list[tuple[int, int, TableCell, int, int]] = []

    def ensure_width(width: int) -> None:
        if width > _MAX_TABLE_COLUMNS:
            raise RenderError(f"Table exceeds the safe {_MAX_TABLE_COLUMNS}-column limit")
        for grid_row in grid:
            grid_row.extend([None] * (width - len(grid_row)))

    for row_index, row in enumerate(table.rows):
        column_index = 0
        for cell in row.cells:
            while column_index < len(grid[row_index]) and grid[row_index][column_index] is not None:
                column_index += 1
            column_span = _safe_span(cell.column_span, _MAX_TABLE_COLUMNS)
            row_span = _safe_span(cell.row_span, row_count - row_index)
            while True:
                ensure_width(column_index + column_span)
                occupied = any(
                    grid[target_row][target_column] is not None
                    for target_row in range(row_index, row_index + row_span)
                    for target_column in range(column_index, column_index + column_span)
                )
                if not occupied:
                    break
                column_index += 1
            grid[row_index][column_index] = cell
            for target_row in range(row_index, row_index + row_span):
                for target_column in range(column_index, column_index + column_span):
                    if target_row != row_index or target_column != column_index:
                        grid[target_row][target_column] = _COVERED
            anchors.append((row_index, column_index, cell, column_span, row_span))
            column_index += column_span
    max_columns = max((len(row) for row in grid), default=0)
    ensure_width(max_columns)
    return grid, anchors


def _merge_character_style(base: CharacterStyle, run: CharacterStyle) -> CharacterStyle:
    return CharacterStyle(
        bold=base.bold or run.bold,
        italic=base.italic or run.italic,
        underline=base.underline or run.underline,
        strike=base.strike or run.strike,
        superscript=base.superscript or run.superscript,
        subscript=(base.subscript or run.subscript) and not (base.superscript or run.superscript),
        font_family=run.font_family or base.font_family,
        font_size_pt=run.font_size_pt or base.font_size_pt,
        color=run.color or base.color,
    )


def _resolved_style(document: Document, name: str | None) -> StyleDefinition | None:
    if not name or name not in document.styles:
        return None
    chain: list[StyleDefinition] = []
    seen: set[str] = set()
    current_name: str | None = name
    while current_name and current_name not in seen and len(chain) < 64:
        current = document.styles.get(current_name)
        if current is None:
            break
        chain.append(current)
        seen.add(current_name)
        current_name = current.parent

    resolved = StyleDefinition(name=name)
    for item in reversed(chain):
        resolved.character = _merge_character_style(resolved.character, item.character)
        for attribute in (
            "alignment",
            "left_indent_in",
            "right_indent_in",
            "first_line_indent_in",
            "space_before_pt",
            "space_after_pt",
            "line_spacing",
        ):
            value = getattr(item, attribute)
            if value is not None:
                setattr(resolved, attribute, value)
    return resolved


def _font_name(style: CharacterStyle) -> str:
    requested = (style.font_family or "").casefold()
    if any(name in requested for name in ("courier", "mono", "console")):
        family = "Courier"
    elif any(name in requested for name in ("times", "serif", "roman")):
        family = "Times"
    else:
        family = "Helvetica"
    if family == "Times":
        variants = {
            (False, False): "Times-Roman",
            (True, False): "Times-Bold",
            (False, True): "Times-Italic",
            (True, True): "Times-BoldItalic",
        }
    else:
        suffix = ""
        if style.bold:
            suffix += "-Bold"
        if style.italic:
            suffix += "Oblique" if suffix else "-Oblique"
        variants = {(style.bold, style.italic): family + suffix}
    return variants[(style.bold, style.italic)]


def _image_placeholder(image: Image) -> str:
    detail = f"Image: {image.alt_text or 'Embedded image'}"
    if image.reference:
        detail += f" (source reference not opened: {image.reference})"
    elif image.data is not None:
        detail += " (embedded image preserved as a placeholder)"
    return f"[{detail}]"


def _wmf_flowable(graphic: WmfGraphic) -> object:
    try:
        payload = wmf_png(graphic)
        width, height = wmf_display_size(graphic, max_width_in=6.25, max_height_in=8.0)
    except WmfDecodeError:
        return _placeholder_flowable("[Invalid WMF preview]")
    image = ReportLabImage(BytesIO(payload), width=width * inch, height=height * inch)
    image.hAlign = "LEFT"
    return image


def _sdw_flowables(drawing: SdwDrawing) -> list[object]:
    try:
        payload = sdw_png(drawing)
        width, height = sdw_display_size(
            drawing, max_width_in=6.25, max_height_in=7.5
        )
    except SdwDecodeError:
        return [_placeholder_flowable(_sdw_placeholder(drawing))]
    if not isinstance(payload, bytes) or not _valid_sdw_display_size(width, height):
        return [_placeholder_flowable(_sdw_placeholder(drawing))]
    try:
        image = ReportLabImage(
            BytesIO(payload), width=width * inch, height=height * inch
        )
    except Exception:
        return [_placeholder_flowable(_sdw_placeholder(drawing))]
    image.hAlign = "LEFT"
    return [_placeholder_flowable(sdw_preview_caption(drawing)), image]


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


def _valid_sdw_display_size(width: object, height: object) -> bool:
    return (
        isinstance(width, int | float)
        and not isinstance(width, bool)
        and isinstance(height, int | float)
        and not isinstance(height, bool)
        and math.isfinite(width)
        and math.isfinite(height)
        and 0.0 < width <= 6.25
        and 0.0 < height <= 7.5
    )


def _clean_text(value: str) -> str:
    return _CONTROL_CHARACTERS.sub("\ufffd", value.replace("\r\n", "\n").replace("\r", "\n"))


def _hex_color(value: str | None) -> str | None:
    if not value:
        return None
    match = _HEX_COLOR.fullmatch(value.strip())
    return f"#{match.group(1).upper()}" if match else None


def _color(value: str | None, default: colors.Color) -> colors.Color:
    safe = _hex_color(value)
    return colors.HexColor(safe) if safe else default


def _safe_number(
    value: float | None,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        number = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(maximum, max(minimum, number))


def _safe_inches(value: float | None) -> float:
    return _safe_number(value, default=0.0, minimum=-20.0, maximum=20.0) * inch


def _safe_paragraph_geometry(left: float, right: float, first: float) -> tuple[float, float, float]:
    left = max(0.0, left)
    right = max(0.0, right)
    maximum_combined = _FRAME_WIDTH - _MIN_TEXT_WIDTH
    combined = left + right
    if combined > maximum_combined and combined > 0:
        scale = maximum_combined / combined
        left *= scale
        right *= scale

    available = max(_MIN_TEXT_WIDTH, _FRAME_WIDTH - left - right)
    maximum_first = max(0.0, available - _MIN_TEXT_WIDTH)
    minimum_first = -min(left, 0.5 * inch)
    first = min(maximum_first, max(minimum_first, first))
    return left, right, first


def _safe_leading(font_size: float, line_spacing: float | None) -> float:
    spacing = _safe_number(line_spacing, default=1.2, minimum=0.5, maximum=10.0)
    return max(font_size, font_size * spacing)


def _first_not_none(first: float | None, second: float | None) -> float | None:
    return first if first is not None else second


def _safe_span(value: object, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return 1


def _safe_level(value: object) -> int:
    try:
        return max(0, min(int(value), 15))
    except (TypeError, ValueError, OverflowError):
        return 0
