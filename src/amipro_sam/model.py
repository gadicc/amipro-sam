"""Intermediate representation shared by all output formats.

The model deliberately retains source locations and unknown records.  Renderers
are consumers of this model; no renderer needs to understand raw SAM syntax.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeAlias


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class SourceSpan:
    line: int
    column: int
    byte_offset: int
    end_byte_offset: int


@dataclass(slots=True)
class Diagnostic:
    severity: Severity
    code: str
    message: str
    source: SourceSpan | None = None
    raw: str | None = None


@dataclass(slots=True)
class TwipRect:
    """A source rectangle in Ami Pro's top-left-origin twip coordinates.

    ``valid`` records the parser's bounded-validation result.  Consumers must
    still use ``is_usable`` before allocating or positioning output because IR
    instances can also be constructed by callers.
    """

    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0
    valid: bool = False
    reason: str = ""

    @property
    def is_usable(self) -> bool:
        values = (self.left, self.top, self.right, self.bottom)
        return (
            self.valid is True
            and all(type(value) is int for value in values)
            and all(-32768 <= value <= 32767 for value in values)
            and self.right > self.left
            and self.bottom > self.top
            and self.right - self.left <= 31680
            and self.bottom - self.top <= 31680
        )

    @property
    def width_twips(self) -> int | None:
        return self.right - self.left if self.is_usable else None

    @property
    def height_twips(self) -> int | None:
        return self.bottom - self.top if self.is_usable else None


@dataclass(slots=True)
class PageVariantGeometry:
    """Typed nine-field ``[rght]``/``[lft]`` page geometry.

    The raw strings remain available even when validation fails.  ``page_rect``
    and ``content_rect`` are derived only from a complete, bounded field set.
    """

    side: Literal["odd", "even"] = "odd"
    height_twips: int | None = None
    width_twips: int | None = None
    reserved: int | None = None
    margin_left_twips: int | None = None
    margin_bottom_twips: int | None = None
    display_unit: int | None = None
    margin_top_twips: int | None = None
    margin_right_twips: int | None = None
    flags: int | None = None
    page_rect: TwipRect | None = None
    content_rect: TwipRect | None = None
    valid: bool = False
    reason: str = ""
    raw_fields: tuple[str, ...] = ()
    source: SourceSpan | None = None


@dataclass(slots=True)
class PageLayout:
    """One source-order ``[lay]`` definition and its page variants."""

    index: int = 0
    name: str = ""
    flags: int | None = None
    paper_kind: Literal[
        "letter", "legal", "a3", "a4", "a5", "b5", "custom", "unknown"
    ] = "unknown"
    orientation: Literal["portrait", "landscape", "unknown"] = "unknown"
    non_alternating: bool = False
    mirrored: bool = False
    second_header: bool = False
    second_footer: bool = False
    unknown_flag_bits: int = 0
    odd: PageVariantGeometry | None = None
    even: PageVariantGeometry | None = None
    valid: bool = False
    reason: str = ""
    raw: str = ""
    source: SourceSpan | None = None

    @property
    def primary_geometry(self) -> PageVariantGeometry | None:
        """Return the first usable odd/right geometry, then even/left."""

        if self.odd is not None and self.odd.valid:
            return self.odd
        if self.even is not None and self.even.valid:
            return self.even
        return None


@dataclass(slots=True)
class OpaquePageHints:
    """Uninterpreted, version-dependent ``[pg]`` pagination hints."""

    raw: str = ""
    source: SourceSpan | None = None


@dataclass(slots=True)
class CharacterStyle:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    superscript: bool = False
    subscript: bool = False
    font_family: str | None = None
    font_size_pt: float | None = None
    color: str | None = None

    def merged(self, **changes: Any) -> CharacterStyle:
        values = asdict(self)
        values.update(changes)
        return CharacterStyle(**values)


@dataclass(slots=True)
class TextRun:
    text: str
    style: CharacterStyle = field(default_factory=CharacterStyle)
    source: SourceSpan | None = None


@dataclass(slots=True)
class Paragraph:
    runs: list[TextRun] = field(default_factory=list)
    style_name: str | None = None
    alignment: Literal["left", "right", "center", "justify"] | None = None
    left_indent_in: float | None = None
    right_indent_in: float | None = None
    first_line_indent_in: float | None = None
    space_before_pt: float | None = None
    space_after_pt: float | None = None
    line_spacing: float | None = None
    page_break_before: bool = False
    keep_with_next: bool = False
    list_kind: Literal["bullet", "number"] | None = None
    list_level: int = 0
    source: SourceSpan | None = None

    @property
    def text(self) -> str:
        if not isinstance(self.runs, list | tuple):
            return "[Invalid paragraph runs omitted]"
        parts: list[str] = []
        seen: set[int] = set()
        omitted = len(self.runs) > 4_096
        total = 0
        for run in self.runs[:4_096]:
            if not isinstance(run, TextRun):
                omitted = True
                continue
            if id(run) in seen:
                omitted = True
                continue
            seen.add(id(run))
            value = run.text
            if isinstance(value, str):
                pass
            elif isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            else:
                omitted = True
                continue
            total += len(value)
            if total > 1_000_000:
                parts.append(value[: max(0, 1_000_000 - (total - len(value)))])
                omitted = True
                break
            parts.append(value)
        if omitted:
            parts.append("[Paragraph content omitted at safe rendering limit]")
        return "".join(parts)


@dataclass(slots=True)
class PageBreak:
    source: SourceSpan | None = None


@dataclass(slots=True)
class TableCell:
    blocks: list[Paragraph] = field(default_factory=list)
    column_span: int = 1
    row_span: int = 1

    @property
    def text(self) -> str:
        if not isinstance(self.blocks, list | tuple):
            return "[Invalid table cell content omitted]"
        parts: list[str] = []
        seen: set[int] = set()
        omitted = len(self.blocks) > 4_096
        for block in self.blocks[:4_096]:
            if not isinstance(block, Paragraph) or id(block) in seen:
                omitted = True
                continue
            seen.add(id(block))
            parts.append(block.text)
        if omitted:
            parts.append("[Table cell content omitted at safe rendering limit]")
        return "\n".join(parts)


@dataclass(slots=True)
class TableRow:
    cells: list[TableCell] = field(default_factory=list)
    is_header: bool = False


@dataclass(slots=True)
class Table:
    rows: list[TableRow] = field(default_factory=list)
    source: SourceSpan | None = None


@dataclass(slots=True)
class Image:
    reference: str | None = None
    data: bytes | None = None
    media_type: str | None = None
    alt_text: str = "Embedded image"
    width_in: float | None = None
    height_in: float | None = None
    source: SourceSpan | None = None


@dataclass(slots=True)
class WmfGraphic:
    """A decoded, inert preview of a validated WMF raster operation.

    ``rgb_data`` is top-down packed RGB generated by the parser.  Raw WMF
    records never cross this trust boundary into a renderer.
    """

    width_px: int
    height_px: int
    rgb_data: bytes
    source_sha256: str
    operations: tuple[str, ...] = ()
    record_count: int = 0
    placeable: bool = False
    width_in: float | None = None
    height_in: float | None = None
    alt_text: str = "Embedded WMF preview"
    source: SourceSpan | None = None


@dataclass(slots=True)
class SdwRecordSummary:
    """Evidence-backed envelope information for one Ami Draw record.

    Record operation semantics are deliberately not assigned here.  ``offset``
    is relative to the start of the preserved SDW payload.
    """

    record_type: int
    byte_length: int
    depth: int
    offset: int
    point_count: int | None = None


@dataclass(slots=True)
class SdwPreview:
    """A bounded grayscale/index rendering of an observed ``SS`` companion."""

    width_px: int
    height_px: int
    rgb_data: bytes
    source_sha256: str
    bits_per_plane: int
    plane_count: int
    stride: int
    opaque_header: tuple[int, int, int, int]


@dataclass(slots=True)
class SdwDrawing:
    """Structured, inert preservation metadata for an indexed Ami Draw object.

    The original vector and companion bytes remain data, never renderer input.
    Only ``preview`` crosses into renderers after independent validation.
    """

    asset_id: str
    declared_offset: int
    declared_length: int
    data: bytes | None = None
    source_sha256: str | None = None
    signature_family: str = "unavailable"
    header_field_1: int | None = None
    header_field_2: int | None = None
    direct_record_count: int | None = None
    bounds: tuple[int, int, int, int] | None = None
    declared_stream_length: int | None = None
    records: list[SdwRecordSummary] = field(default_factory=list)
    trailing_bytes: int = 0
    status: Literal["validated", "malformed", "unavailable"] = "unavailable"
    reason: str = ""
    companion_data: bytes | None = None
    companion_sha256: str | None = None
    preview: SdwPreview | None = None
    alt_text: str = "Ami Draw object"
    source: SourceSpan | None = None


@dataclass(slots=True)
class UnsupportedObject:
    kind: str
    description: str
    source: SourceSpan | None = None


@dataclass(slots=True)
class Frame:
    """A typed frame whose readable contents remain in source anchor order.

    Geometry is in integer twips.  No value of ``layer_role`` claims that an
    unanchored frame is a page background; that source encoding is unknown.
    """

    blocks: list[Block] = field(default_factory=list)
    content_kind: Literal["text", "table", "image", "drawing", "unknown"] = (
        "unknown"
    )
    placement: Literal["anchored", "fixed-page", "repeating", "unknown"] = (
        "unknown"
    )
    region: Literal["body", "header", "footer", "unknown"] = "body"
    layer_role: Literal["unknown"] = "unknown"
    anchor_index: int | None = None
    page_number: int | None = None
    flags: int | None = None
    unknown_flag_bits: int = 0
    bounds: TwipRect | None = None
    opaque: bool | None = None
    wrap_around: bool | None = None
    raw_header_fields: tuple[str, ...] = ()
    frame_layout_fields: tuple[str, ...] = ()
    raw: str = ""
    source: SourceSpan | None = None


@dataclass(slots=True)
class Annotation:
    """An inline Ami Pro note with readable nested content and opaque metadata."""

    blocks: list[Block] = field(default_factory=list)
    metadata: str = ""
    raw: str = ""
    terminated: bool = True
    source: SourceSpan | None = None


@dataclass(slots=True)
class Footnote:
    """An inline Ami Pro footnote.

    Footnote numbering is intentionally left unset when the source does not
    provide an independently verified number.
    """

    blocks: list[Block] = field(default_factory=list)
    metadata: str = ""
    raw: str = ""
    terminated: bool = True
    number: int | None = None
    source: SourceSpan | None = None


@dataclass(slots=True)
class Header:
    """Header content from the body stream or a page-layout branch."""

    blocks: list[Block] = field(default_factory=list)
    placement: Literal["all", "odd", "even", "odd-even", "unknown"] = "unknown"
    origin: Literal["body", "layout"] = "body"
    layout_index: int | None = None
    flags: int | None = None
    unknown_flag_bits: int = 0
    metadata: str = ""
    raw: str = ""
    terminated: bool = True
    source: SourceSpan | None = None
    frame: Frame | None = None


@dataclass(slots=True)
class Footer:
    """Footer content from the body stream or a page-layout branch."""

    blocks: list[Block] = field(default_factory=list)
    placement: Literal["all", "odd", "even", "odd-even", "unknown"] = "unknown"
    origin: Literal["body", "layout"] = "body"
    layout_index: int | None = None
    flags: int | None = None
    unknown_flag_bits: int = 0
    metadata: str = ""
    raw: str = ""
    terminated: bool = True
    source: SourceSpan | None = None
    frame: Frame | None = None


Block: TypeAlias = (
    Paragraph
    | PageBreak
    | Table
    | Image
    | WmfGraphic
    | SdwDrawing
    | UnsupportedObject
    | Frame
    | Annotation
    | Footnote
    | Header
    | Footer
)


@dataclass(slots=True)
class StyleDefinition:
    name: str
    parent: str | None = None
    character: CharacterStyle = field(default_factory=CharacterStyle)
    alignment: Literal["left", "right", "center", "justify"] | None = None
    left_indent_in: float | None = None
    right_indent_in: float | None = None
    first_line_indent_in: float | None = None
    space_before_pt: float | None = None
    space_after_pt: float | None = None
    line_spacing: float | None = None
    raw: str | None = None
    source: SourceSpan | None = None


@dataclass(slots=True)
class UnknownRecord:
    section: str | None
    record_type: str
    raw: str
    source: SourceSpan
    reason: str


@dataclass(slots=True)
class SectionRecord:
    name: str
    source: SourceSpan
    raw_lines: list[str] = field(default_factory=list)
    raw_spans: list[SourceSpan] = field(default_factory=list)


@dataclass(slots=True)
class FootnoteOptions:
    flags: int
    collect_at_page_end: bool
    reset_number_each_page: bool
    separator_line: bool
    start_number: int
    separator_length_in: float
    indent_in: float
    unknown_flag_bits: int = 0
    raw: str = ""
    source: SourceSpan | None = None


@dataclass(slots=True)
class Document:
    source_name: str
    encoding: str
    version: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    footnote_options: FootnoteOptions | None = None
    page_layouts: list[PageLayout] = field(default_factory=list)
    page_hints: list[OpaquePageHints] = field(default_factory=list)
    styles: dict[str, StyleDefinition] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    unknown_records: list[UnknownRecord] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    sections: list[SectionRecord] = field(default_factory=list)
    source_directory: Path | None = None
    original_size: int = 0
    newline: str = "\r\n"

    @property
    def text(self) -> str:
        return _blocks_text(self.blocks)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


_MAX_TEXT_RECURSION = 64


def _blocks_text(
    blocks: list[Block],
    *,
    _active: set[int] | None = None,
    _depth: int = 0,
) -> str:
    if not isinstance(blocks, list):
        return "[Invalid nested content omitted]"
    if _depth >= _MAX_TEXT_RECURSION:
        return "[Nested content depth limit reached]"
    active = set() if _active is None else _active
    identity = id(blocks)
    if identity in active:
        return "[Recursive content omitted]"
    active.add(identity)
    parts: list[str] = []
    for block in blocks[:100_000]:
            if isinstance(block, Paragraph):
                parts.append(block.text)
            elif isinstance(block, PageBreak):
                parts.append("\f")
            elif isinstance(block, Table):
                rows = block.rows
                if not isinstance(rows, list | tuple):
                    parts.append("[Invalid table rows omitted]")
                    continue
                seen_rows: set[int] = set()
                seen_cells: set[int] = set()
                omitted = len(rows) > 390
                for row in rows[:390]:
                    if not isinstance(row, TableRow):
                        omitted = True
                        continue
                    if id(row) in seen_rows:
                        omitted = True
                        continue
                    seen_rows.add(id(row))
                    cells = row.cells
                    if not isinstance(cells, list | tuple):
                        parts.append("[Invalid table row omitted]")
                        continue
                    if len(cells) > 256:
                        parts.append("[Table cells omitted at safe 256-column limit]")
                        omitted = True
                        continue
                    values: list[str] = []
                    for cell in cells[:256]:
                        if not isinstance(cell, TableCell):
                            omitted = True
                            continue
                        if id(cell) in seen_cells:
                            values.append("[Repeated table cell omitted]")
                            omitted = True
                            continue
                        seen_cells.add(id(cell))
                        values.append(cell.text)
                    parts.append(
                        "\t".join(values)
                    )
                if omitted:
                    parts.append("[Table content omitted at safe rendering limit]")
            elif isinstance(block, Image):
                parts.append(f"[{block.alt_text}]")
            elif isinstance(block, WmfGraphic):
                parts.append(
                    f"[WMF preview: {block.width_px} x {block.height_px} pixels]"
                )
            elif isinstance(block, SdwDrawing):
                if (
                    isinstance(block.preview, SdwPreview)
                    and isinstance(block.preview.width_px, int)
                    and not isinstance(block.preview.width_px, bool)
                    and isinstance(block.preview.height_px, int)
                    and not isinstance(block.preview.height_px, bool)
                    and block.preview.width_px > 0
                    and block.preview.height_px > 0
                ):
                    status = block.status if isinstance(block.status, str) else "unavailable"
                    parts.append(
                        "[Ami Draw companion preview: "
                        f"{block.preview.width_px} x {block.preview.height_px} pixels; "
                        f"grayscale/index rendering; vector status={status}]"
                    )
                else:
                    parts.append(
                        "[Ami Draw object: vector payload preserved; rendering unavailable]"
                    )
            elif isinstance(block, UnsupportedObject):
                parts.append(f"[Unsupported {block.kind}: {block.description}]")
            elif isinstance(block, Frame):
                parts.append(
                    _blocks_text(block.blocks, _active=active, _depth=_depth + 1)
                )
            elif isinstance(block, Annotation):
                parts.append(
                    "[Annotation]\n"
                    + _blocks_text(block.blocks, _active=active, _depth=_depth + 1)
                )
            elif isinstance(block, Footnote):
                label = (
                    f"Footnote {block.number}"
                    if block.number is not None
                    else "Footnote"
                )
                parts.append(
                    f"[{label}]\n"
                    + _blocks_text(block.blocks, _active=active, _depth=_depth + 1)
                )
            elif isinstance(block, Header | Footer):
                label = type(block).__name__
                parts.append(
                    f"[{label}: {block.placement} pages]\n"
                    + _blocks_text(block.blocks, _active=active, _depth=_depth + 1)
                )
    return "\n".join(parts)


_MAX_JSON_RECURSION = 64


def _jsonable(
    value: Any,
    *,
    _active: set[int] | None = None,
    _depth: int = 0,
) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"length": len(value), "encoding": "not-inlined"}
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"encoding": "non-finite-number"}
    if _depth >= _MAX_JSON_RECURSION:
        return {
            "encoding": "nested-depth-limit",
            "type": type(value).__name__,
        }
    if is_dataclass(value) or isinstance(value, dict | list | tuple):
        active = set() if _active is None else _active
        identity = id(value)
        if identity in active:
            return {
                "encoding": "recursive-reference",
                "type": type(value).__name__,
            }
        active.add(identity)
        if is_dataclass(value):
            result = {
                item.name: _jsonable(
                    getattr(value, item.name),
                    _active=active,
                    _depth=_depth + 1,
                )
                for item in fields(value)
            }
            result["type"] = type(value).__name__
            return result
        if isinstance(value, dict):
            return {
                str(key): _jsonable(item, _active=active, _depth=_depth + 1)
                for key, item in list(value.items())[:100_000]
            }
        return [
            _jsonable(item, _active=active, _depth=_depth + 1)
            for item in value[:100_000]
        ]
    return {"encoding": "unsupported-value", "type": type(value).__name__}
