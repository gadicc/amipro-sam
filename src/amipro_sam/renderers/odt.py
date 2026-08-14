"""Dependency-free OpenDocument Text renderer.

The generated archive is intentionally small, deterministic, and free of
links, macros, scripts, and externally referenced assets.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from ..errors import RenderError
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
    _paragraph_text,
    _TextOutputBudget,
)
from ..sdw import SdwDecodeError, sdw_display_size, sdw_png, sdw_preview_caption
from ..wmf import WmfDecodeError, wmf_display_size, wmf_png

__all__ = ["render"]


MIMETYPE = "application/vnd.oasis.opendocument.text"
_HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})\Z")
_MAX_TABLE_COLUMNS = 256
_MAX_TABLE_ROWS = 390
_MAX_BLOCKS_PER_LIST = 100_000
_MAX_BLOCK_DEPTH = 64
_COVERED = object()
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MAX_PAGE_TWIPS = 31_680
_MIN_PAGE_TWIPS = 1_440
_MIN_BODY_TWIPS = 720
_MIN_FURNITURE_MARGIN_TWIPS = 720
_INVALID_BLOCK_CONTAINER = object()
_BLOCK_LIMIT_OMISSION = object()
NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "config": "urn:oasis:names:tc:opendocument:xmlns:config:1.0",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
}
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)


@dataclass(frozen=True, slots=True)
class _PageGeometry:
    width_twips: int = 12_240
    height_twips: int = 15_840
    margin_left_twips: int = 1_440
    margin_right_twips: int = 1_440
    margin_top_twips: int = 1_440
    margin_bottom_twips: int = 1_440
    layout_index: int | None = None
    non_alternating: bool = False

    @property
    def body_width_twips(self) -> int:
        return self.width_twips - self.margin_left_twips - self.margin_right_twips

    @property
    def body_height_twips(self) -> int:
        return self.height_twips - self.margin_top_twips - self.margin_bottom_twips


@dataclass(frozen=True, slots=True)
class _NativePageContent:
    header_odd: Header | None = None
    header_even: Header | None = None
    footer_odd: Footer | None = None
    footer_even: Footer | None = None
    ids: frozenset[int] = frozenset()
    alternating: bool = True

    @property
    def uses_odd_even(self) -> bool:
        return self.alternating and any(
            item is not None
            for item in (
                self.header_odd,
                self.header_even,
                self.footer_odd,
                self.footer_even,
            )
        )


def _page_geometry(document: object) -> _PageGeometry:
    layouts = getattr(document, "page_layouts", None)
    if not isinstance(layouts, (list, tuple)):
        return _PageGeometry()
    for layout in layouts:
        if getattr(layout, "valid", None) is not True:
            continue
        for variant_name in ("odd", "even"):
            variant = getattr(layout, variant_name, None)
            if getattr(variant, "valid", None) is not True:
                continue
            values = tuple(
                getattr(variant, name, None)
                for name in (
                    "width_twips",
                    "height_twips",
                    "margin_left_twips",
                    "margin_right_twips",
                    "margin_top_twips",
                    "margin_bottom_twips",
                )
            )
            if not all(type(value) is int for value in values):
                continue
            width, height, left, right, top, bottom = values
            if not (
                _MIN_PAGE_TWIPS <= width <= _MAX_PAGE_TWIPS
                and _MIN_PAGE_TWIPS <= height <= _MAX_PAGE_TWIPS
            ):
                continue
            if min(left, right, top, bottom) < 0:
                continue
            if left + right >= width or top + bottom >= height:
                continue
            if width - left - right < _MIN_BODY_TWIPS:
                continue
            if height - top - bottom < _MIN_BODY_TWIPS:
                continue
            layout_index = getattr(layout, "index", None)
            if type(layout_index) is not int:
                layout_index = None
            return _PageGeometry(
                width_twips=width,
                height_twips=height,
                margin_left_twips=left,
                margin_right_twips=right,
                margin_top_twips=top,
                margin_bottom_twips=bottom,
                layout_index=layout_index,
                non_alternating=getattr(layout, "non_alternating", None) is True,
            )
    return _PageGeometry()


def _native_page_content(
    document: object,
    geometry: _PageGeometry,
) -> _NativePageContent:
    if geometry.layout_index is None:
        return _NativePageContent()
    candidates: dict[str, list[Header | Footer]] = {
        "header_odd": [],
        "header_even": [],
        "footer_odd": [],
        "footer_even": [],
    }
    for block in _safe_blocks(getattr(document, "blocks", None)):
        if not isinstance(block, Header | Footer):
            continue
        placement = getattr(block, "placement", None)
        if not isinstance(placement, str) or placement not in {"odd", "even"}:
            continue
        if getattr(block, "origin", None) != "layout":
            continue
        if getattr(block, "layout_index", None) != geometry.layout_index:
            continue
        key = f"{'header' if isinstance(block, Header) else 'footer'}_{placement}"
        candidates[key].append(block)

    selected = {
        key: (
            values[0]
            if len(values) == 1 and _native_furniture_fits(values[0], geometry)
            else None
        )
        for key, values in candidates.items()
    }
    if geometry.non_alternating:
        selected["header_even"] = None
        selected["footer_even"] = None
    ids = frozenset(
        id(value) for value in selected.values() if value is not None
    )
    return _NativePageContent(
        ids=ids,
        alternating=not geometry.non_alternating,
        **selected,
    )


def _native_furniture_fits(
    block: Header | Footer,
    geometry: _PageGeometry,
) -> bool:
    if getattr(block, "terminated", None) is not True:
        return False
    if type(getattr(block, "unknown_flag_bits", None)) is not int:
        return False
    if block.unknown_flag_bits != 0:
        return False
    frame = getattr(block, "frame", None)
    if frame is not None and (
        not isinstance(frame, Frame)
        or type(getattr(frame, "unknown_flag_bits", None)) is not int
        or frame.unknown_flag_bits != 0
    ):
        return False
    child_blocks = getattr(block, "blocks", None)
    if not isinstance(child_blocks, (list, tuple)) or not 1 <= len(child_blocks) <= 4:
        return False
    if not all(_native_paragraph_is_safe(item) for item in child_blocks):
        return False
    characters_per_line = max(20, geometry.body_width_twips // 144)
    line_count = 0
    total_characters = 0
    for paragraph in child_blocks:
        text = _bounded_native_paragraph_text(paragraph)
        if text is None:
            return False
        total_characters += len(text)
        if total_characters > 8_192:
            return False
        lines = text.splitlines() or [""]
        line_count += sum(
            max(1, math.ceil(len(line) / characters_per_line)) for line in lines
        )
    margin = (
        geometry.margin_top_twips
        if isinstance(block, Header)
        else geometry.margin_bottom_twips
    )
    required = max(_MIN_FURNITURE_MARGIN_TWIPS, 360 + line_count * 360)
    return margin >= required


def _bounded_native_paragraph_text(paragraph: Paragraph) -> str | None:
    runs = getattr(paragraph, "runs", None)
    if not isinstance(runs, (list, tuple)) or len(runs) > 1_024:
        return None
    parts: list[str] = []
    length = 0
    for run in runs:
        if not isinstance(run, TextRun) or not isinstance(run.style, CharacterStyle):
            return None
        value = run.text
        if not isinstance(value, str):
            return None
        length += len(value)
        if length > 4_096:
            return None
        parts.append(value)
    return "".join(parts)


def _native_paragraph_is_safe(value: object) -> bool:
    page_break = getattr(value, "page_break_before", None)
    list_kind = getattr(value, "list_kind", None)
    return (
        isinstance(value, Paragraph)
        and (page_break is False or page_break is None)
        and (list_kind is None or (isinstance(list_kind, str) and list_kind == ""))
        and isinstance(getattr(value, "runs", None), (list, tuple))
    )


def _safe_blocks(value: object) -> list[object]:
    if not isinstance(value, (list, tuple)):
        return [_INVALID_BLOCK_CONTAINER]
    result: list[object] = list(value[:_MAX_BLOCKS_PER_LIST])
    if len(value) > _MAX_BLOCKS_PER_LIST:
        result.append(_BLOCK_LIMIT_OMISSION)
    return result


def _normalized_blocks(blocks: list[object]) -> list[object]:
    """Discard edge breaks while preserving every explicit interior break."""

    result: list[object] = []
    index = 0
    while index < len(blocks):
        if not isinstance(blocks[index], PageBreak):
            result.append(blocks[index])
            index += 1
            continue
        end = index
        while end < len(blocks) and isinstance(blocks[end], PageBreak):
            end += 1
        if result and end < len(blocks):
            result.extend(blocks[index:end])
        index = end
    return result


def _append_native_paragraphs(
    parent: ET.Element,
    blocks: object,
    text_budget: _TextOutputBudget,
) -> None:
    for paragraph in _safe_blocks(blocks):
        if not isinstance(paragraph, Paragraph):
            continue
        node = ET.SubElement(parent, _q("text", "p"))
        _append_text(
            node,
            _paragraph_text(paragraph, text_budget, expansion_factor=5),
        )


def _frame_label(frame: Frame) -> str:
    content_kind = getattr(frame, "content_kind", None)
    if not isinstance(content_kind, str) or content_kind not in {
        "text", "table", "image", "drawing", "unknown"
    }:
        content_kind = "unknown"
    placement = getattr(frame, "placement", None)
    if not isinstance(placement, str) or placement not in {
        "anchored", "fixed-page", "repeating", "unknown"
    }:
        placement = "unknown"
    return (
        f"[Frame: {content_kind}; {placement} placement reflowed in source order; "
        "original coordinates not reproduced]"
    )


def _twips(value: int | float) -> str:
    return f"{value / 20:g}pt"


def render(document: Document, **_options: object) -> bytes:
    """Return *document* as a valid ODT package."""

    try:
        geometry = _page_geometry(document)
        native = _native_page_content(document, geometry)
        text_budget = _TextOutputBudget()
        builder = _ContentBuilder(document, geometry, native, text_budget)
        content = builder.build()
        members = [
            ("content.xml", content),
            ("styles.xml", _styles_xml(geometry, native, text_budget)),
            ("meta.xml", _meta_xml()),
            ("settings.xml", _settings_xml()),
            (
                "META-INF/manifest.xml",
                _manifest_xml([name for name, _payload in builder.generated_images]),
            ),
            *builder.generated_images,
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
    def __init__(
        self,
        document: Document,
        geometry: _PageGeometry,
        native: _NativePageContent,
        text_budget: _TextOutputBudget,
    ) -> None:
        self.document = document
        self.geometry = geometry
        self.native_ids = native.ids
        self.text_budget = text_budget
        self.automatic_styles = ET.Element(_q("office", "automatic-styles"))
        self.body_text = ET.Element(_q("office", "text"))
        self._paragraph_style_counter = 0
        self._text_style_counter = 0
        self._table_counter = 0
        self._image_counter = 0
        self._sdw_image_counter = 0
        self._active_container_ids: set[int] = set()
        self._seen_block_ids: set[int] = set()
        self.generated_images: list[tuple[str, bytes]] = []
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

        self._add_blocks(self.document.blocks)
        return _xml_bytes(root)

    def _add_blocks(self, blocks: object, *, depth: int = 0) -> None:
        if depth > _MAX_BLOCK_DEPTH:
            self._add_placeholder("[Nested content omitted: safe depth limit reached]")
            return
        if isinstance(blocks, (list, tuple)):
            container_id = id(blocks)
            if container_id in self._active_container_ids:
                self._add_placeholder(
                    "[Nested content omitted: repeated or cyclic block reference]"
                )
                return
            self._active_container_ids.add(container_id)
        else:
            container_id = None
        visible = [
            block for block in _safe_blocks(blocks) if id(block) not in self.native_ids
        ]
        blocks_to_render = _normalized_blocks(visible)
        try:
            index = 0
            while index < len(blocks_to_render):
                block = blocks_to_render[index]
                block_identity = id(block)
                if block_identity in self._seen_block_ids:
                    self._add_placeholder(
                        "[Nested content omitted: repeated or cyclic block reference]"
                    )
                    index += 1
                    continue
                self._seen_block_ids.add(block_identity)
                if isinstance(block, Paragraph) and block.list_kind is not None:
                    list_kind = block.list_kind
                    list_node = ET.SubElement(
                        self.body_text,
                        _q("text", "list"),
                        {
                            _q("text", "style-name"): (
                                "LNumber" if list_kind == "number" else "LBullet"
                            )
                        },
                    )
                    item = ET.SubElement(list_node, _q("text", "list-item"))
                    self._add_paragraph(item, block, list_item=True)
                    index += 1
                    while index < len(blocks_to_render):
                        candidate = blocks_to_render[index]
                        if id(candidate) in self._seen_block_ids:
                            break
                        if not isinstance(candidate, Paragraph) or candidate.list_kind != list_kind:
                            break
                        self._seen_block_ids.add(id(candidate))
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
                elif isinstance(block, WmfGraphic):
                    self._add_wmf(block)
                elif isinstance(block, SdwDrawing):
                    self._add_sdw(block)
                elif isinstance(block, Frame):
                    self._add_placeholder(_frame_label(block))
                    self._add_blocks(getattr(block, "blocks", None), depth=depth + 1)
                elif isinstance(block, UnsupportedObject):
                    kind = _safe_label_field(
                        block.kind, "unknown object kind", maximum=128
                    )
                    description = _safe_label_field(
                        block.description, "description unavailable", maximum=256
                    )
                    self._add_placeholder(
                        f"[Unsupported {kind}: {description}]"
                    )
                elif isinstance(block, Annotation | Footnote | Header | Footer):
                    self._add_placeholder(_container_label(block))
                    self._add_blocks(getattr(block, "blocks", None), depth=depth + 1)
                elif block is _INVALID_BLOCK_CONTAINER:
                    self._add_placeholder("[Invalid block container omitted]")
                elif block is _BLOCK_LIMIT_OMISSION:
                    self._add_placeholder(
                        "[Block content omitted at safe rendering limit]"
                    )
                else:
                    self._add_placeholder("[Unrecognized block object omitted]")
                index += 1
        finally:
            # Retaining visited containers bounds shared acyclic graphs as
            # well as direct cycles; repeated content receives the visible
            # marker at the entry check above.
            pass

    def _add_wmf(self, graphic: WmfGraphic) -> None:
        try:
            payload = wmf_png(graphic)
            width, height = wmf_display_size(
                graphic,
                max_width_in=min(6.25, self.geometry.body_width_twips / 1440),
                max_height_in=min(7.5, self.geometry.body_height_twips / 1440),
            )
        except WmfDecodeError:
            self._add_placeholder("[Invalid WMF preview]")
            return
        self._image_counter += 1
        name = f"Pictures/WMF{self._image_counter}.png"
        self.generated_images.append((name, payload))
        paragraph = ET.SubElement(self.body_text, _q("text", "p"))
        frame = ET.SubElement(
            paragraph,
            _q("draw", "frame"),
            {
                _q("draw", "name"): f"WMF{self._image_counter}",
                _q("text", "anchor-type"): "as-char",
                _q("svg", "width"): f"{width:.6g}in",
                _q("svg", "height"): f"{height:.6g}in",
            },
        )
        ET.SubElement(
            frame,
            _q("draw", "image"),
            {
                _q("xlink", "href"): name,
                _q("xlink", "type"): "simple",
                _q("xlink", "show"): "embed",
                _q("xlink", "actuate"): "onLoad",
            },
        )

    def _add_sdw(self, drawing: SdwDrawing) -> None:
        try:
            payload = sdw_png(drawing)
            width, height = sdw_display_size(
                drawing,
                max_width_in=min(6.25, self.geometry.body_width_twips / 1440),
                max_height_in=min(7.5, self.geometry.body_height_twips / 1440),
            )
        except SdwDecodeError:
            self._add_placeholder(_sdw_placeholder(drawing))
            return
        if (
            not isinstance(payload, bytes)
            or not payload.startswith(b"\x89PNG\r\n\x1a\n")
            or not _valid_sdw_display_size(width, height)
        ):
            self._add_placeholder(_sdw_placeholder(drawing))
            return
        self._sdw_image_counter += 1
        name = f"Pictures/SDW{self._sdw_image_counter}.png"
        self.generated_images.append((name, payload))

        caption = ET.SubElement(self.body_text, _q("text", "p"))
        caption.text = _clean_xml_text(sdw_preview_caption(drawing))
        paragraph = ET.SubElement(self.body_text, _q("text", "p"))
        frame = ET.SubElement(
            paragraph,
            _q("draw", "frame"),
            {
                _q("draw", "name"): f"SDW{self._sdw_image_counter}",
                _q("text", "anchor-type"): "as-char",
                _q("svg", "width"): f"{width:.6g}in",
                _q("svg", "height"): f"{height:.6g}in",
            },
        )
        title = ET.SubElement(frame, _q("svg", "title"))
        title.text = _clean_xml_text(
            _safe_label_field(drawing.alt_text, "Ami Draw object", maximum=256)
        )
        ET.SubElement(
            frame,
            _q("draw", "image"),
            {
                _q("xlink", "href"): name,
                _q("xlink", "type"): "simple",
                _q("xlink", "show"): "embed",
                _q("xlink", "actuate"): "onLoad",
            },
        )

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
        runs = paragraph.runs
        if not isinstance(runs, list | tuple):
            _append_text(node, "[Invalid paragraph runs omitted]")
            return
        seen_runs: set[int] = set()
        omitted = len(runs) > 4_096
        total_characters = 0
        for run in runs[:4_096]:
            if not isinstance(run, TextRun) or not isinstance(run.style, CharacterStyle):
                _append_text(node, "[Invalid text run omitted]")
                continue
            if id(run) in seen_runs:
                omitted = True
                continue
            seen_runs.add(id(run))
            value = run.text
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if not isinstance(value, str):
                _append_text(node, "[Invalid text run omitted]")
                continue
            paragraph_remaining = max(0, 1_000_000 - total_characters)
            prepared = self.text_budget.prepare(
                value,
                unit_limit=paragraph_remaining,
                expansion_factor=5,
            )
            value = prepared.visible
            total_characters += len(prepared.text)
            if prepared.encoding in {"bounded-text", "text-budget-limit"}:
                omitted = True
            effective = _merge_character_style(base, run.style)
            span = ET.SubElement(
                node,
                _q("text", "span"),
                {_q("text", "style-name"): self._text_style(effective)},
            )
            _append_text(span, value)
            if total_characters >= 1_000_000:
                omitted = True
                break
        if omitted:
            _append_text(node, "[Paragraph content omitted at safe rendering limit]")

    def _add_placeholder(self, text: str) -> None:
        style_name = self._placeholder_style()
        paragraph = ET.SubElement(
            self.body_text,
            _q("text", "p"),
            {_q("text", "style-name"): style_name},
        )
        _append_text(paragraph, text)

    def _add_table(self, table: Table) -> None:
        rows = _safe_table_rows(table)
        if not rows:
            self._add_placeholder(
                "[Invalid table rows omitted]"
                if not isinstance(table.rows, list | tuple)
                else "[Empty table]"
            )
            return
        if any(
            isinstance(row.cells, list | tuple)
            and len(row.cells) > _MAX_TABLE_COLUMNS
            for row in rows
        ):
            self._add_placeholder(
                "[Table cells omitted at safe 256-column limit]"
            )
            return
        try:
            grid, anchors = _layout_table(table)
        except RenderError:
            self._add_placeholder("[Table grid omitted at safe 256-column limit]")
            return
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
        for row in rows:
            if not row.is_header or header_count >= 8:
                break
            header_count += 1
        header_parent = None
        if header_count:
            header_parent = ET.SubElement(node, _q("table", "table-header-rows"))

        seen_cells: set[int] = set()
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
                repeated_cell = id(cell) in seen_cells
                seen_cells.add(id(cell))
                if repeated_cell:
                    marker = ET.SubElement(cell_node, _q("text", "p"))
                    _append_text(marker, "[Repeated table cell omitted]")
                paragraphs = (
                    cell.blocks[:4_096]
                    if isinstance(cell.blocks, list | tuple)
                    else []
                )
                valid_paragraphs: list[Paragraph] = []
                seen_paragraphs: set[int] = set()
                for item in paragraphs:
                    if not isinstance(item, Paragraph) or id(item) in seen_paragraphs:
                        continue
                    seen_paragraphs.add(id(item))
                    valid_paragraphs.append(item)
                if (
                    not isinstance(cell.blocks, list | tuple)
                    or len(valid_paragraphs) != len(paragraphs)
                    or (
                        isinstance(cell.blocks, list | tuple)
                        and len(cell.blocks) > 4_096
                    )
                ):
                    marker = ET.SubElement(cell_node, _q("text", "p"))
                    _append_text(
                        marker,
                        "[Invalid or repeated table cell content omitted]",
                    )
                if not valid_paragraphs:
                    ET.SubElement(cell_node, _q("text", "p"))
                for paragraph in valid_paragraphs if not repeated_cell else []:
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
        if isinstance(alignment, str) and alignment in {
            "left",
            "right",
            "center",
            "justify",
            "start",
            "end",
        }:
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
        if isinstance(style.font_family, str) and style.font_family:
            properties[_q("fo", "font-family")] = _clean_xml_text(
                style.font_family[:128]
            )
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
                _q("style", "width"): _twips(self.geometry.body_width_twips),
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
            {
                _q("style", "column-width"): _twips(
                    self.geometry.body_width_twips / columns
                )
            },
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


def _styles_xml(
    geometry: _PageGeometry,
    native: _NativePageContent,
    text_budget: _TextOutputBudget,
) -> bytes:
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
            _q("fo", "page-width"): _twips(geometry.width_twips),
            _q("fo", "page-height"): _twips(geometry.height_twips),
            _q("style", "print-orientation"): (
                "landscape" if geometry.width_twips > geometry.height_twips else "portrait"
            ),
            _q("fo", "margin-left"): _twips(geometry.margin_left_twips),
            _q("fo", "margin-right"): _twips(geometry.margin_right_twips),
            _q("fo", "margin-top"): _twips(geometry.margin_top_twips),
            _q("fo", "margin-bottom"): _twips(geometry.margin_bottom_twips),
            _q("style", "page-usage"): "mirrored" if native.uses_odd_even else "all",
        },
    )
    masters = ET.SubElement(root, _q("office", "master-styles"))
    master = ET.SubElement(
        masters,
        _q("style", "master-page"),
        {_q("style", "name"): "Standard", _q("style", "page-layout-name"): "PM1"},
    )
    for odd_key, even_key, odd_tag, even_tag in (
        ("header_odd", "header_even", "header", "header-left"),
        ("footer_odd", "footer_even", "footer", "footer-left"),
    ):
        odd = getattr(native, odd_key)
        even = getattr(native, even_key)
        if odd is None and even is None:
            continue
        odd_node = ET.SubElement(master, _q("style", odd_tag))
        if odd is not None:
            _append_native_paragraphs(
                odd_node, getattr(odd, "blocks", None), text_budget
            )
        else:
            # LibreOffice treats a structurally empty left/right container as
            # absent and inherits the opposite side.  An explicit empty
            # paragraph represents the intentional blank variant instead.
            ET.SubElement(odd_node, _q("text", "p"))
        even_node = ET.SubElement(master, _q("style", even_tag))
        if even is not None:
            _append_native_paragraphs(
                even_node, getattr(even, "blocks", None), text_budget
            )
        else:
            ET.SubElement(even_node, _q("text", "p"))
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


def _manifest_xml(image_names: list[str] | None = None) -> bytes:
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
    for name in image_names or []:
        ET.SubElement(
            root,
            _q("manifest", "file-entry"),
            {
                _q("manifest", "full-path"): name,
                _q("manifest", "media-type"): "image/png",
            },
        )
    return _xml_bytes(root)


def _layout_table(
    table: Table,
) -> tuple[list[list[object | TableCell | None]], list[tuple[int, int, TableCell, int, int]]]:
    rows = _safe_table_rows(table)
    row_count = len(rows)
    if not row_count:
        return [], []
    grid: list[list[object | TableCell | None]] = [[] for _ in range(row_count)]
    anchors: list[tuple[int, int, TableCell, int, int]] = []
    current_width = 0

    def ensure_width(width: int) -> None:
        nonlocal current_width
        if width > _MAX_TABLE_COLUMNS:
            raise RenderError(f"Table exceeds the safe {_MAX_TABLE_COLUMNS}-column limit")
        if width <= current_width:
            return
        for row in grid:
            row.extend([None] * (width - len(row)))
        current_width = width

    for row_index, row in enumerate(rows):
        column_index = 0
        cells = row.cells if isinstance(row.cells, list | tuple) else []
        if len(cells) > _MAX_TABLE_COLUMNS:
            raise RenderError(
                f"Table exceeds the safe {_MAX_TABLE_COLUMNS}-column limit"
            )
        for cell in cells[:_MAX_TABLE_COLUMNS]:
            if not isinstance(cell, TableCell):
                continue
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


def _safe_table_rows(table: Table) -> list[TableRow]:
    rows = table.rows
    if not isinstance(rows, list | tuple):
        return []
    result: list[TableRow] = []
    seen: set[int] = set()
    omitted = len(rows) > _MAX_TABLE_ROWS
    for row in rows[:_MAX_TABLE_ROWS]:
        if not isinstance(row, TableRow) or id(row) in seen:
            omitted = True
            continue
        seen.add(id(row))
        result.append(row)
    if omitted:
        result = result[: _MAX_TABLE_ROWS - 1]
        result.append(
            TableRow(
                cells=[
                    TableCell(
                        blocks=[
                            Paragraph(
                                runs=[
                                    TextRun(
                                        "[Table content omitted at safe rendering limit]"
                                    )
                                ]
                            )
                        ]
                    )
                ]
            )
        )
    return result


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
    styles = getattr(document, "styles", None)
    if not isinstance(name, str) or not name or not isinstance(styles, dict):
        return None
    if name not in styles:
        return None
    chain: list[StyleDefinition] = []
    seen: set[str] = set()
    current_name: str | None = name
    while current_name and current_name not in seen and len(chain) < 64:
        current = styles.get(current_name)
        if not isinstance(current, StyleDefinition) or not isinstance(
            current.character, CharacterStyle
        ):
            break
        chain.append(current)
        seen.add(current_name)
        current_name = current.parent if isinstance(current.parent, str) else None

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
    alt = _safe_label_field(image.alt_text, "Embedded image", maximum=256)
    detail = f"Image: {alt}"
    if image.reference:
        reference = _safe_label_field(
            image.reference, "invalid reference omitted", maximum=256
        )
        detail += f" (source reference not opened: {reference})"
    elif image.data is not None:
        detail += " (embedded image preserved as a placeholder)"
    return f"[{detail}]"


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


def _container_label(block: Annotation | Footnote | Header | Footer) -> str:
    if isinstance(block, Annotation):
        return "[Annotation]"
    if isinstance(block, Footnote):
        return (
            "[Footnote "
            + _safe_label_field(
                block.number, "number unavailable", maximum=64
            )
            + "]"
            if block.number is not None
            else "[Footnote]"
        )
    kind = "Header" if isinstance(block, Header) else "Footer"
    source_placement = getattr(block, "placement", None)
    if not isinstance(source_placement, str):
        source_placement = "unknown"
    placement = {
        "all": "all pages",
        "odd": "odd/right pages",
        "even": "even/left pages",
        "odd-even": "odd and even variants",
        "unknown": "placement unknown",
    }.get(source_placement, "placement unknown")
    return f"[{kind}: {placement}]"


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
    if not isinstance(value, str) or not value:
        return None
    match = _HEX_COLOR.fullmatch(value.strip())
    return f"#{match.group(1).upper()}" if match else None


def _number(value: float | None, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value) if value is not None else default
    except (TypeError, ValueError, OverflowError):
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
