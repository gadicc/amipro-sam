"""Optional Microsoft Word DOCX renderer.

The renderer always builds a new package.  It does not import relationships,
macros, OLE objects, metadata, or externally referenced images from a source
document.
"""

from __future__ import annotations

import math
import re
from io import BytesIO
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from ..errors import RenderError
from ..model import (
    CharacterStyle,
    Image,
    PageBreak,
    Paragraph,
    StyleDefinition,
    Table,
    TableCell,
    UnsupportedObject,
)
from ..model import (
    Document as AmiProDocument,
)

__all__ = ["render"]


_HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})\Z")
_RSID_ATTRIBUTE = re.compile(rb'\s+w:rsid[A-Za-z0-9]*="[^"]*"')
_RSID_SETTINGS = re.compile(rb"<w:rsids(?:\s[^>]*)?>.*?</w:rsids\s*>", re.DOTALL)
_DOC_ID = re.compile(rb"<w14:docId(?:\s[^>]*)?/>\s*")
_SAVE_PREVIEW = re.compile(rb"<w:savePreviewPicture(?:\s[^>]*)?/>\s*")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MAX_TABLE_COLUMNS = 256
_COVERED = object()


def render(document: AmiProDocument, **_options: object) -> bytes:
    """Return *document* as DOCX bytes.

    ``python-docx`` is an optional dependency so importing this module remains
    cheap.  Selecting DOCX without the extra installed produces a direct,
    actionable error rather than an import traceback.
    """

    try:
        from docx import Document as WordDocument
        from docx.enum.section import WD_ORIENT
        from docx.shared import Inches, Pt
    except ImportError as exc:
        raise RenderError(
            "DOCX output requires the optional 'python-docx' dependency. "
            "Install it with `pip install 'amipro-sam-toolkit[docx]'` "
            "(or `pip install python-docx`)."
        ) from exc

    try:
        output_document = WordDocument()
        section = output_document.sections[0]
        section.orientation = WD_ORIENT.PORTRAIT
        section.page_width = Inches(8.5)
        section.page_height = Inches(11)
        section.top_margin = Inches(1)
        section.right_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.header_distance = Inches(0.492)
        section.footer_distance = Inches(0.492)

        normal = output_document.styles["Normal"]
        normal.font.name = "Calibri"
        normal.font.size = Pt(11)
        normal.paragraph_format.space_after = Pt(6)
        normal.paragraph_format.line_spacing = 1.1
        _scrub_core_properties(output_document.core_properties)

        for block in document.blocks:
            if isinstance(block, Paragraph):
                paragraph = output_document.add_paragraph()
                _populate_paragraph(paragraph, block, document)
            elif isinstance(block, PageBreak):
                output_document.add_page_break()
            elif isinstance(block, Table):
                _add_table(output_document, block, document)
            elif isinstance(block, Image):
                _add_placeholder(output_document, _image_placeholder(block))
            elif isinstance(block, UnsupportedObject):
                _add_placeholder(
                    output_document,
                    f"[Unsupported {block.kind}: {block.description}]",
                )

        buffer = BytesIO()
        output_document.save(buffer)
        return _sanitize_package(buffer.getvalue())
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(f"Could not render DOCX safely: {exc}") from exc


def _populate_paragraph(
    target: Any,
    source: Paragraph,
    document: AmiProDocument,
) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt

    style_definition = _resolved_style(document, source.style_name)
    if source.list_kind:
        level = _safe_level(source.list_level)
        style_suffix = "" if level == 0 else f" {min(level + 1, 3)}"
        style_name = f"List {'Number' if source.list_kind == 'number' else 'Bullet'}{style_suffix}"
        try:
            target.style = style_name
        except KeyError:
            target.style = f"List {'Number' if source.list_kind == 'number' else 'Bullet'}"

    alignment = source.alignment or (style_definition.alignment if style_definition else None)
    target.alignment = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    }.get(alignment)
    formatting = target.paragraph_format
    left = _first_not_none(
        source.left_indent_in,
        style_definition.left_indent_in if style_definition else None,
    )
    if left is None and source.list_kind:
        left = 0.5 + 0.25 * _safe_level(source.list_level)
    right = _first_not_none(
        source.right_indent_in,
        style_definition.right_indent_in if style_definition else None,
    )
    first = _first_not_none(
        source.first_line_indent_in,
        style_definition.first_line_indent_in if style_definition else None,
    )
    before = _first_not_none(
        source.space_before_pt,
        style_definition.space_before_pt if style_definition else None,
    )
    after = _first_not_none(
        source.space_after_pt,
        style_definition.space_after_pt if style_definition else None,
    )
    spacing = _first_not_none(
        source.line_spacing,
        style_definition.line_spacing if style_definition else None,
    )
    if left is not None:
        formatting.left_indent = Inches(_number(left, 0.0, -20.0, 20.0))
    if right is not None:
        formatting.right_indent = Inches(_number(right, 0.0, -20.0, 20.0))
    if first is not None:
        formatting.first_line_indent = Inches(_number(first, 0.0, -20.0, 20.0))
    if before is not None:
        formatting.space_before = Pt(_number(before, 0.0, 0.0, 720.0))
    if after is not None:
        formatting.space_after = Pt(_number(after, 0.0, 0.0, 720.0))
    if spacing is not None:
        formatting.line_spacing = _number(spacing, 1.2, 0.5, 10.0)
    formatting.page_break_before = bool(source.page_break_before)
    formatting.keep_with_next = bool(source.keep_with_next)

    base = style_definition.character if style_definition else CharacterStyle()
    for source_run in source.runs:
        run = target.add_run(_clean_xml_text(source_run.text))
        _format_run(run, _merge_character_style(base, source_run.style))


def _format_run(run: Any, style: CharacterStyle) -> None:
    from docx.enum.text import WD_UNDERLINE
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    run.bold = bool(style.bold)
    run.italic = bool(style.italic)
    run.underline = WD_UNDERLINE.SINGLE if style.underline else False
    run.font.strike = bool(style.strike)
    run.font.superscript = bool(style.superscript)
    run.font.subscript = bool(style.subscript and not style.superscript)
    if style.font_family:
        family = _clean_xml_text(style.font_family)[:128]
        run.font.name = family
        run_properties = run._element.get_or_add_rPr()
        fonts = run_properties.get_or_add_rFonts()
        for attribute in ("ascii", "hAnsi", "cs", "eastAsia"):
            fonts.set(qn(f"w:{attribute}"), family)
    if style.font_size_pt is not None:
        run.font.size = Pt(_number(style.font_size_pt, 11.0, 1.0, 200.0))
    color = _color(style.color)
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def _add_placeholder(document: Any, text: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(6)
    run = paragraph.add_run(_clean_xml_text(text))
    run.italic = True
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    properties = paragraph._p.get_or_add_pPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), "F4F4F4")
    properties.append(shading)
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:color"), "B8B8B8")
    borders.append(bottom)
    properties.append(borders)


def _add_table(document: Any, source: Table, ir_document: AmiProDocument) -> None:
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT

    grid, anchors = _layout_table(source)
    if not grid or not grid[0]:
        _add_placeholder(document, "[Empty table]")
        return
    row_count = len(grid)
    column_count = len(grid[0])
    table = document.add_table(rows=row_count, cols=column_count)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    widths = _configure_table_geometry(table, column_count)

    merged_cells: dict[tuple[int, int], Any] = {}
    for row_index, column_index, _cell, column_span, row_span in anchors:
        target = table.cell(row_index, column_index)
        if column_span > 1 or row_span > 1:
            target = target.merge(
                table.cell(row_index + row_span - 1, column_index + column_span - 1)
            )
            _set_cell_width(target, sum(widths[column_index : column_index + column_span]))
        merged_cells[(row_index, column_index)] = target

    for row_index, column_index, source_cell, _column_span, _row_span in anchors:
        target = merged_cells[(row_index, column_index)]
        target.text = ""
        target.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        if source_cell.blocks:
            for block_index, source_paragraph in enumerate(source_cell.blocks):
                paragraph = target.paragraphs[0] if block_index == 0 else target.add_paragraph()
                _populate_paragraph(paragraph, source_paragraph, ir_document)
        if source.rows[row_index].is_header:
            _shade_cell(target, "F2F4F7")
            for paragraph in target.paragraphs:
                for run in paragraph.runs:
                    run.bold = True

    for row_index, row in enumerate(source.rows):
        if row.is_header:
            _shade_header_row(table.rows[row_index])
        if row.is_header and all(
            previous.is_header for previous in source.rows[: row_index + 1]
        ):
            _set_repeat_table_header(table.rows[row_index])


def _configure_table_geometry(table: Any, column_count: int) -> list[int]:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Twips

    total_width = 9360
    base, remainder = divmod(total_width, column_count)
    widths = [base + (1 if index < remainder else 0) for index in range(column_count)]
    table_properties = table._tbl.tblPr
    _set_or_add_measure(table_properties, "w:tblW", total_width, "dxa")
    _set_or_add_measure(table_properties, "w:tblInd", 120, "dxa")
    layout = table_properties.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        table_properties.append(layout)
    layout.set(qn("w:type"), "fixed")

    cell_margins = table_properties.find(qn("w:tblCellMar"))
    if cell_margins is None:
        cell_margins = OxmlElement("w:tblCellMar")
        table_properties.append(cell_margins)
    for side, width in (("top", 80), ("start", 120), ("bottom", 80), ("end", 120)):
        _set_or_add_measure(cell_margins, f"w:{side}", width, "dxa")

    grid = table._tbl.tblGrid
    for child in list(grid):
        grid.remove(child)
    for width in widths:
        column = OxmlElement("w:gridCol")
        column.set(qn("w:w"), str(width))
        grid.append(column)
    for row in table.rows:
        for index, cell in enumerate(row.cells):
            cell.width = Twips(widths[index])
            _set_cell_width(cell, widths[index])
    return widths


def _set_cell_width(cell: Any, width: int) -> None:
    properties = cell._tc.get_or_add_tcPr()
    _set_or_add_measure(properties, "w:tcW", width, "dxa")


def _set_or_add_measure(parent: Any, tag: str, width: int, measure_type: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    child = parent.find(qn(tag))
    if child is None:
        child = OxmlElement(tag)
        parent.append(child)
    child.set(qn("w:w"), str(width))
    child.set(qn("w:type"), measure_type)


def _shade_cell(cell: Any, color: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = cell._tc.get_or_add_tcPr()
    shading = properties.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        properties.append(shading)
    shading.set(qn("w:fill"), color)


def _shade_header_row(row: Any) -> None:
    for cell in row.cells:
        _shade_cell(cell, "F2F4F7")


def _set_repeat_table_header(row: Any) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = row._tr.get_or_add_trPr()
    header = properties.find(qn("w:tblHeader"))
    if header is None:
        header = OxmlElement("w:tblHeader")
        properties.append(header)
    header.set(qn("w:val"), "true")


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
        for row in grid:
            row.extend([None] * (width - len(row)))

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


def _scrub_core_properties(core: Any) -> None:
    for attribute in (
        "author",
        "category",
        "comments",
        "content_status",
        "identifier",
        "keywords",
        "language",
        "last_modified_by",
        "subject",
        "title",
        "version",
    ):
        setattr(core, attribute, "")
    # python-docx requires a datetime when using these public setters.  Remove
    # the corresponding optional elements directly instead of inventing a
    # misleading date.
    for child in list(core._element):
        if child.tag.rsplit("}", 1)[-1] in {"created", "lastPrinted", "modified"}:
            core._element.remove(child)
    core.revision = 1


def _sanitize_package(payload: bytes) -> bytes:
    output = BytesIO()
    with ZipFile(BytesIO(payload), "r") as source, ZipFile(output, "w") as target:
        for source_info in source.infolist():
            name = source_info.filename
            if name.startswith(("/", "../")) or "/../" in name:
                raise RenderError("DOCX generator produced an unsafe package path")
            lowered = name.casefold()
            if lowered.startswith("customxml/") or lowered.startswith(
                "docprops/thumbnail."
            ):
                continue
            if (
                lowered == "docprops/custom.xml"
                or lowered.startswith("word/embeddings/")
                or lowered.startswith("word/activex/")
                or "vbaproject" in lowered
                or "macros" in lowered
            ):
                raise RenderError(f"DOCX generator unexpectedly produced active content: {name}")
            member = source.read(source_info)
            if lowered.endswith(".rels"):
                member = _sanitize_relationships(member)
            elif lowered == "[content_types].xml":
                member = _sanitize_content_types(member)
            elif lowered == "docprops/app.xml":
                member = _sanitize_extended_properties(member)
            if lowered.startswith("word/") and lowered.endswith(".xml"):
                member = _RSID_ATTRIBUTE.sub(b"", member)
                if lowered == "word/settings.xml":
                    member = _RSID_SETTINGS.sub(b"", member)
                    member = _DOC_ID.sub(b"", member)
                    member = _SAVE_PREVIEW.sub(b"", member)
            info = ZipInfo(name, _ZIP_TIMESTAMP)
            info.compress_type = source_info.compress_type or ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o600 << 16
            target.writestr(info, member)
    return output.getvalue()


def _sanitize_relationships(payload: bytes) -> bytes:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RenderError("DOCX generator produced malformed relationships XML") from exc
    namespace = root.tag.removesuffix("Relationships").strip("{}")
    if namespace:
        ET.register_namespace("", namespace)
    for relationship in list(root):
        target_mode = next(
            (
                value
                for key, value in relationship.attrib.items()
                if key.endswith("}TargetMode") or key == "TargetMode"
            ),
            "",
        )
        if target_mode.casefold() == "external":
            raise RenderError("DOCX generator unexpectedly produced an external relationship")
        relationship_type = next(
            (
                value
                for key, value in relationship.attrib.items()
                if key.endswith("}Type") or key == "Type"
            ),
            "",
        ).casefold()
        if relationship_type.endswith(("/customxml", "/metadata/thumbnail")):
            root.remove(relationship)
        elif relationship_type.endswith(
            ("/oleobject", "/package", "/attachedtemplate", "/vbaproject")
        ):
            raise RenderError("DOCX generator unexpectedly produced active content")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _sanitize_content_types(payload: bytes) -> bytes:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RenderError("DOCX generator produced malformed content-types XML") from exc
    namespace = root.tag.removesuffix("Types").strip("{}")
    if namespace:
        ET.register_namespace("", namespace)
    for element in list(root):
        part_name = element.attrib.get("PartName", "").casefold()
        extension = element.attrib.get("Extension", "").casefold()
        if part_name.startswith(("/customxml/", "/docprops/thumbnail.")) or extension in {
            "jpeg",
            "jpg",
        }:
            root.remove(element)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _sanitize_extended_properties(payload: bytes) -> bytes:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RenderError("DOCX generator produced malformed extended-properties XML") from exc
    namespace = root.tag.removesuffix("Properties").strip("{}")
    if namespace:
        ET.register_namespace("", namespace)
    for element in root:
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name == "Application":
            element.text = "amipro-sam-toolkit"
        elif local_name in {
            "AppVersion",
            "Company",
            "HyperlinkBase",
            "Manager",
            "Template",
        }:
            element.text = ""
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


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


def _resolved_style(
    document: AmiProDocument,
    name: str | None,
) -> StyleDefinition | None:
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


def _image_placeholder(image: Image) -> str:
    detail = f"Image: {image.alt_text or 'Embedded image'}"
    if image.reference:
        detail += f" (source reference not opened: {image.reference})"
    elif image.data is not None:
        detail += " (embedded image preserved as a placeholder)"
    return f"[{detail}]"


def _clean_xml_text(text: str) -> str:
    return "".join(
        character if _is_xml_character(ord(character)) else "\ufffd"
        for character in text
    )


def _is_xml_character(codepoint: int) -> bool:
    return (
        codepoint in {0x9, 0xA, 0xD}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _color(value: str | None) -> str | None:
    if not value:
        return None
    match = _HEX_COLOR.fullmatch(value.strip())
    return match.group(1).upper() if match else None


def _number(value: float | None, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(maximum, max(minimum, number))


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
