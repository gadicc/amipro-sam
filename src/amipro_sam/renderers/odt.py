"""Dependency-free OpenDocument Text renderer.

The generated archive is intentionally small, deterministic, and free of
links, macros, scripts, and externally referenced assets.
"""

from __future__ import annotations

import math
import re
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from ..errors import RenderError
from ..model import (
    CharacterStyle,
    Document,
    Image,
    PageBreak,
    Paragraph,
    StyleDefinition,
    Table,
    TableCell,
    UnsupportedObject,
)

__all__ = ["render"]


MIMETYPE = "application/vnd.oasis.opendocument.text"
_HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})\Z")
_MAX_TABLE_COLUMNS = 256
_COVERED = object()
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "config": "urn:oasis:names:tc:opendocument:xmlns:config:1.0",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)


def render(document: Document, **_options: object) -> bytes:
    """Return *document* as a valid ODT package."""

    try:
        content = _ContentBuilder(document).build()
        members = [
            ("content.xml", content),
            ("styles.xml", _styles_xml()),
            ("meta.xml", _meta_xml()),
            ("settings.xml", _settings_xml()),
            ("META-INF/manifest.xml", _manifest_xml()),
        ]
        output = BytesIO()
        with ZipFile(output, "w") as archive:
            _write_member(archive, "mimetype", MIMETYPE.encode("ascii"), ZIP_STORED)
            for name, payload in members:
                _write_member(archive, name, payload, ZIP_DEFLATED)
        return output.getvalue()
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(f"Could not render ODT safely: {exc}") from exc


class _ContentBuilder:
    def __init__(self, document: Document) -> None:
        self.document = document
        self.automatic_styles = ET.Element(_q("office", "automatic-styles"))
        self.body_text = ET.Element(_q("office", "text"))
        self._paragraph_style_counter = 0
        self._text_style_counter = 0
        self._table_counter = 0
        self._define_list_styles()
        self._define_table_cell_styles()

    def build(self) -> bytes:
        root = ET.Element(
            _q("office", "document-content"),
            {
                _q("office", "version"): "1.3",
            },
        )
        root.append(self.automatic_styles)
        office_body = ET.SubElement(root, _q("office", "body"))
        office_body.append(self.body_text)

        index = 0
        while index < len(self.document.blocks):
            block = self.document.blocks[index]
            if isinstance(block, Paragraph) and block.list_kind is not None:
                list_kind = block.list_kind
                list_node = ET.SubElement(
                    self.body_text,
                    _q("text", "list"),
                    {_q("text", "style-name"): "LNumber" if list_kind == "number" else "LBullet"},
                )
                while index < len(self.document.blocks):
                    candidate = self.document.blocks[index]
                    if not isinstance(candidate, Paragraph) or candidate.list_kind != list_kind:
                        break
                    item = ET.SubElement(list_node, _q("text", "list-item"))
                    self._add_paragraph(item, candidate, list_item=True)
                    index += 1
                continue
            if isinstance(block, Paragraph):
                self._add_paragraph(self.body_text, block)
            elif isinstance(block, PageBreak):
                paragraph = ET.SubElement(
                    self.body_text,
                    _q("text", "p"),
                    {_q("text", "style-name"): self._page_break_style()},
                )
                paragraph.text = ""
            elif isinstance(block, Table):
                self._add_table(block)
            elif isinstance(block, Image):
                self._add_placeholder(_image_placeholder(block))
            elif isinstance(block, UnsupportedObject):
                self._add_placeholder(f"[Unsupported {block.kind}: {block.description}]")
            index += 1
        return _xml_bytes(root)

    def _add_paragraph(
        self,
        parent: ET.Element,
        paragraph: Paragraph,
        *,
        list_item: bool = False,
    ) -> None:
        style_definition = _resolved_style(self.document, paragraph.style_name)
        style_name = self._paragraph_style(paragraph, style_definition, list_item=list_item)
        node = ET.SubElement(parent, _q("text", "p"), {_q("text", "style-name"): style_name})
        base = style_definition.character if style_definition else CharacterStyle()
        for run in paragraph.runs:
            effective = _merge_character_style(base, run.style)
            span = ET.SubElement(
                node,
                _q("text", "span"),
                {_q("text", "style-name"): self._text_style(effective)},
            )
            _append_text(span, run.text)

    def _add_placeholder(self, text: str) -> None:
        style_name = self._placeholder_style()
        paragraph = ET.SubElement(
            self.body_text,
            _q("text", "p"),
            {_q("text", "style-name"): style_name},
        )
        _append_text(paragraph, text)

    def _add_table(self, table: Table) -> None:
        grid, anchors = _layout_table(table)
        if not grid or not grid[0]:
            self._add_placeholder("[Empty table]")
            return
        self._table_counter += 1
        table_name = f"Table{self._table_counter}"
        node = ET.SubElement(
            self.body_text,
            _q("table", "table"),
            {
                _q("table", "name"): table_name,
                _q("table", "style-name"): self._table_style(table_name),
            },
        )
        ET.SubElement(
            node,
            _q("table", "table-column"),
            {
                _q("table", "style-name"): self._table_column_style(
                    table_name, len(grid[0])
                ),
                _q("table", "number-columns-repeated"): str(len(grid[0])),
            },
        )
        anchor_map = {
            (row, column): (cell, col_span, row_span)
            for row, column, cell, col_span, row_span in anchors
        }

        header_count = 0
        for row in table.rows:
            if not row.is_header:
                break
            header_count += 1
        header_parent = None
        if header_count:
            header_parent = ET.SubElement(node, _q("table", "table-header-rows"))

        for row_index, grid_row in enumerate(grid):
            row_parent = (
                header_parent
                if header_parent is not None and row_index < header_count
                else node
            )
            row_node = ET.SubElement(row_parent, _q("table", "table-row"))
            for column_index, slot in enumerate(grid_row):
                if slot is _COVERED:
                    ET.SubElement(row_node, _q("table", "covered-table-cell"))
                    continue
                anchor = anchor_map.get((row_index, column_index))
                if anchor is None:
                    empty_cell = ET.SubElement(
                        row_node,
                        _q("table", "table-cell"),
                        {
                            _q("table", "style-name"): (
                                "TableHeaderCell" if row_index < header_count else "TableCell"
                            )
                        },
                    )
                    ET.SubElement(empty_cell, _q("text", "p"))
                    continue
                cell, column_span, row_span = anchor
                attributes = {
                    _q("office", "value-type"): "string",
                    _q("table", "style-name"): (
                        "TableHeaderCell" if row_index < header_count else "TableCell"
                    ),
                }
                if column_span > 1:
                    attributes[_q("table", "number-columns-spanned")] = str(column_span)
                if row_span > 1:
                    attributes[_q("table", "number-rows-spanned")] = str(row_span)
                cell_node = ET.SubElement(row_node, _q("table", "table-cell"), attributes)
                if not cell.blocks:
                    ET.SubElement(cell_node, _q("text", "p"))
                for paragraph in cell.blocks:
                    self._add_paragraph(cell_node, paragraph)

    def _paragraph_style(
        self,
        paragraph: Paragraph,
        style_definition: StyleDefinition | None,
        *,
        list_item: bool,
    ) -> str:
        self._paragraph_style_counter += 1
        name = f"P{self._paragraph_style_counter}"
        node = ET.SubElement(
            self.automatic_styles,
            _q("style", "style"),
            {_q("style", "name"): name, _q("style", "family"): "paragraph"},
        )
        properties: dict[str, str] = {}
        alignment = paragraph.alignment or (
            style_definition.alignment if style_definition else None
        )
        if alignment:
            properties[_q("fo", "text-align")] = alignment
        left = _first_not_none(
            paragraph.left_indent_in,
            style_definition.left_indent_in if style_definition else None,
        )
        if left is None and list_item:
            left = 0.25 * _safe_level(paragraph.list_level)
        right = _first_not_none(
            paragraph.right_indent_in,
            style_definition.right_indent_in if style_definition else None,
        )
        first = _first_not_none(
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
        spacing = _first_not_none(
            paragraph.line_spacing,
            style_definition.line_spacing if style_definition else None,
        )
        if left is not None:
            properties[_q("fo", "margin-left")] = _inches(left)
        if right is not None:
            properties[_q("fo", "margin-right")] = _inches(right)
        if first is not None:
            properties[_q("fo", "text-indent")] = _inches(first)
        if before is not None:
            properties[_q("fo", "margin-top")] = _points(before)
        if after is not None:
            properties[_q("fo", "margin-bottom")] = _points(after)
        if spacing is not None:
            properties[_q("fo", "line-height")] = f"{_number(spacing, 1.2, 0.5, 10.0) * 100:g}%"
        if paragraph.page_break_before:
            properties[_q("fo", "break-before")] = "page"
        if paragraph.keep_with_next:
            properties[_q("fo", "keep-with-next")] = "always"
        ET.SubElement(node, _q("style", "paragraph-properties"), properties)
        return name

    def _text_style(self, style: CharacterStyle) -> str:
        self._text_style_counter += 1
        name = f"T{self._text_style_counter}"
        node = ET.SubElement(
            self.automatic_styles,
            _q("style", "style"),
            {_q("style", "name"): name, _q("style", "family"): "text"},
        )
        properties: dict[str, str] = {}
        if style.bold:
            properties[_q("fo", "font-weight")] = "bold"
        if style.italic:
            properties[_q("fo", "font-style")] = "italic"
        if style.underline:
            properties[_q("style", "text-underline-style")] = "solid"
        if style.strike:
            properties[_q("style", "text-line-through-style")] = "solid"
        if style.superscript:
            properties[_q("style", "text-position")] = "super 58%"
        elif style.subscript:
            properties[_q("style", "text-position")] = "sub 58%"
        if style.font_family:
            properties[_q("fo", "font-family")] = _clean_xml_text(style.font_family[:128])
        if style.font_size_pt is not None:
            properties[_q("fo", "font-size")] = _points(
                style.font_size_pt,
                default=11.0,
                minimum=1.0,
                maximum=200.0,
            )
        color = _color(style.color)
        if color:
            properties[_q("fo", "color")] = color
        ET.SubElement(node, _q("style", "text-properties"), properties)
        return name

    def _page_break_style(self) -> str:
        name = "PageBreak"
        if self.automatic_styles.find(f"*[@{{{NS['style']}}}name='{name}']") is None:
            node = ET.SubElement(
                self.automatic_styles,
                _q("style", "style"),
                {_q("style", "name"): name, _q("style", "family"): "paragraph"},
            )
            ET.SubElement(
                node,
                _q("style", "paragraph-properties"),
                {_q("fo", "break-before"): "page"},
            )
        return name

    def _placeholder_style(self) -> str:
        name = "Placeholder"
        if self.automatic_styles.find(f"*[@{{{NS['style']}}}name='{name}']") is None:
            node = ET.SubElement(
                self.automatic_styles,
                _q("style", "style"),
                {_q("style", "name"): name, _q("style", "family"): "paragraph"},
            )
            ET.SubElement(
                node,
                _q("style", "paragraph-properties"),
                {
                    _q("fo", "background-color"): "#F4F4F4",
                    _q("fo", "border"): "0.5pt solid #B8B8B8",
                    _q("fo", "padding"): "5pt",
                    _q("fo", "margin-top"): "4pt",
                    _q("fo", "margin-bottom"): "6pt",
                },
            )
            ET.SubElement(
                node,
                _q("style", "text-properties"),
                {
                    _q("fo", "font-size"): "9pt",
                    _q("fo", "font-style"): "italic",
                    _q("fo", "color"): "#555555",
                },
            )
        return name

    def _table_style(self, table_name: str) -> str:
        name = f"{table_name}Style"
        node = ET.SubElement(
            self.automatic_styles,
            _q("style", "style"),
            {_q("style", "name"): name, _q("style", "family"): "table"},
        )
        ET.SubElement(
            node,
            _q("style", "table-properties"),
            {
                _q("style", "width"): "6.5in",
                _q("table", "align"): "left",
                _q("style", "may-break-between-rows"): "true",
            },
        )
        return name

    def _table_column_style(self, table_name: str, columns: int) -> str:
        name = f"{table_name}Column"
        node = ET.SubElement(
            self.automatic_styles,
            _q("style", "style"),
            {_q("style", "name"): name, _q("style", "family"): "table-column"},
        )
        ET.SubElement(
            node,
            _q("style", "table-column-properties"),
            {_q("style", "column-width"): f"{6.5 / columns:g}in"},
        )
        return name

    def _define_list_styles(self) -> None:
        for name, numbered in (("LBullet", False), ("LNumber", True)):
            list_style = ET.SubElement(
                self.automatic_styles,
                _q("text", "list-style"),
                {_q("style", "name"): name},
            )
            for level in range(1, 11):
                if numbered:
                    level_style = ET.SubElement(
                        list_style,
                        _q("text", "list-level-style-number"),
                        {
                            _q("text", "level"): str(level),
                            _q("style", "num-format"): "1",
                            _q("style", "num-suffix"): ".",
                        },
                    )
                else:
                    level_style = ET.SubElement(
                        list_style,
                        _q("text", "list-level-style-bullet"),
                        {_q("text", "level"): str(level), _q("text", "bullet-char"): "\u2022"},
                    )
                ET.SubElement(
                    level_style,
                    _q("style", "list-level-properties"),
                    {
                        _q("text", "space-before"): f"{0.25 * level:g}in",
                        _q("text", "min-label-width"): "0.25in",
                    },
                )

    def _define_table_cell_styles(self) -> None:
        for name, header in (("TableCell", False), ("TableHeaderCell", True)):
            node = ET.SubElement(
                self.automatic_styles,
                _q("style", "style"),
                {_q("style", "name"): name, _q("style", "family"): "table-cell"},
            )
            properties = {
                _q("fo", "border"): "0.5pt solid #777777",
                _q("fo", "padding"): "4pt",
                _q("style", "vertical-align"): "middle",
            }
            if header:
                properties[_q("fo", "background-color")] = "#E8EEF5"
            ET.SubElement(node, _q("style", "table-cell-properties"), properties)
            if header:
                ET.SubElement(
                    node,
                    _q("style", "text-properties"),
                    {_q("fo", "font-weight"): "bold"},
                )


def _styles_xml() -> bytes:
    root = ET.Element(
        _q("office", "document-styles"),
        {_q("office", "version"): "1.3"},
    )
    styles = ET.SubElement(root, _q("office", "styles"))
    default = ET.SubElement(
        styles,
        _q("style", "default-style"),
        {_q("style", "family"): "paragraph"},
    )
    ET.SubElement(
        default,
        _q("style", "paragraph-properties"),
        {_q("fo", "margin-bottom"): "6pt", _q("fo", "line-height"): "120%"},
    )
    ET.SubElement(
        default,
        _q("style", "text-properties"),
        {_q("fo", "font-family"): "Liberation Serif", _q("fo", "font-size"): "11pt"},
    )
    automatic = ET.SubElement(root, _q("office", "automatic-styles"))
    page_layout = ET.SubElement(
        automatic,
        _q("style", "page-layout"),
        {_q("style", "name"): "PM1"},
    )
    ET.SubElement(
        page_layout,
        _q("style", "page-layout-properties"),
        {
            _q("fo", "page-width"): "8.5in",
            _q("fo", "page-height"): "11in",
            _q("style", "print-orientation"): "portrait",
            _q("fo", "margin"): "1in",
        },
    )
    masters = ET.SubElement(root, _q("office", "master-styles"))
    ET.SubElement(
        masters,
        _q("style", "master-page"),
        {_q("style", "name"): "Standard", _q("style", "page-layout-name"): "PM1"},
    )
    return _xml_bytes(root)


def _meta_xml() -> bytes:
    root = ET.Element(
        _q("office", "document-meta"),
        {_q("office", "version"): "1.3"},
    )
    meta = ET.SubElement(root, _q("office", "meta"))
    generator = ET.SubElement(meta, _q("meta", "generator"))
    generator.text = "amipro-sam-toolkit"
    return _xml_bytes(root)


def _settings_xml() -> bytes:
    root = ET.Element(
        _q("office", "document-settings"),
        {_q("office", "version"): "1.3"},
    )
    ET.SubElement(root, _q("office", "settings"))
    return _xml_bytes(root)


def _manifest_xml() -> bytes:
    root = ET.Element(
        _q("manifest", "manifest"),
        {_q("manifest", "version"): "1.3"},
    )
    for path, media_type in (
        ("/", MIMETYPE),
        ("content.xml", "text/xml"),
        ("styles.xml", "text/xml"),
        ("meta.xml", "text/xml"),
        ("settings.xml", "text/xml"),
    ):
        ET.SubElement(
            root,
            _q("manifest", "file-entry"),
            {
                _q("manifest", "full-path"): path,
                _q("manifest", "media-type"): media_type,
            },
        )
    return _xml_bytes(root)


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


def _append_text(node: ET.Element, text: str) -> None:
    clean = _clean_xml_text(text.replace("\r\n", "\n").replace("\r", "\n"))
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        value = "".join(buffer)
        if len(node):
            node[-1].tail = (node[-1].tail or "") + value
        else:
            node.text = (node.text or "") + value
        buffer.clear()

    for character in clean:
        if character not in {" ", "\t", "\n"}:
            buffer.append(character)
            continue
        flush()
        if character == " ":
            ET.SubElement(node, _q("text", "s"))
        elif character == "\t":
            ET.SubElement(node, _q("text", "tab"))
        else:
            ET.SubElement(node, _q("text", "line-break"))
    flush()


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


def _image_placeholder(image: Image) -> str:
    detail = f"Image: {image.alt_text or 'Embedded image'}"
    if image.reference:
        detail += f" (source reference not opened: {image.reference})"
    elif image.data is not None:
        detail += " (embedded image preserved as a placeholder)"
    return f"[{detail}]"


def _write_member(archive: ZipFile, name: str, payload: bytes, compression: int) -> None:
    info = ZipInfo(name, _ZIP_TIMESTAMP)
    info.compress_type = compression
    info.create_system = 0
    info.external_attr = 0o600 << 16
    archive.writestr(info, payload)


def _xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True, short_empty_elements=True)


def _q(prefix: str, local_name: str) -> str:
    return f"{{{NS[prefix]}}}{local_name}"


def _color(value: str | None) -> str | None:
    if not value:
        return None
    match = _HEX_COLOR.fullmatch(value.strip())
    return f"#{match.group(1).upper()}" if match else None


def _number(value: float | None, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(maximum, max(minimum, number))


def _inches(value: float | None) -> str:
    return f"{_number(value, 0.0, -20.0, 20.0):g}in"


def _points(
    value: float | None,
    *,
    default: float = 0.0,
    minimum: float = 0.0,
    maximum: float = 720.0,
) -> str:
    return f"{_number(value, default, minimum, maximum):g}pt"


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
