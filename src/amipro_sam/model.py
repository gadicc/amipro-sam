"""Intermediate representation shared by all output formats.

The model deliberately retains source locations and unknown records.  Renderers
are consumers of this model; no renderer needs to understand raw SAM syntax.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from itertools import islice
from pathlib import Path
from typing import Any, Literal, TypeAlias


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Lossiness(str, Enum):
    """Independent preservation-loss classification for diagnostics.

    Severity describes how urgently a condition should be reported.  Lossiness
    describes whether a conversion can still represent the source construct.
    Strict parsing deliberately uses this classification rather than severity.
    """

    NONE = "none"
    SEMANTIC = "semantic"
    CONTENT = "content"


_MAX_OUTPUT_TEXT_CHARACTERS = 4_000_000
_MAX_OUTPUT_TEXT_UNIT = 1_000_000
_MIN_TRACKED_TEXT_ALIAS = 4_096
_FOLLOWING_TEXT_RESERVE = 65_536
_REPEATED_TEXT_OMISSION = (
    "[Repeated text value omitted at safe document rendering limit]"
)
_OUTPUT_TEXT_OMISSION = "[Text content omitted at safe document rendering limit]"
_OUTPUT_TEXT_UNIT_OMISSION = "[Text value truncated at safe rendering limit]"


@dataclass(frozen=True, slots=True)
class _BudgetedText:
    """One renderer-visible result from a cumulative text budget."""

    text: str = ""
    marker: str | None = None
    encoding: str | None = None
    original_length: int = 0

    @property
    def visible(self) -> str:
        return self.text + (self.marker or "")


@dataclass(slots=True)
class _TextOutputBudget:
    """Bound cumulative source text while retaining later small content.

    Large immutable strings are tracked by identity.  Manually constructed IR
    can otherwise attach one multi-kilobyte string to thousands of distinct
    owners and multiply it at every renderer boundary.  A small reserve keeps
    omission markers and ordinary trailing text visible after a large value is
    rejected.
    """

    remaining: int = _MAX_OUTPUT_TEXT_CHARACTERS
    seen_large_text_ids: set[int] = field(default_factory=set)

    def prepare(
        self,
        value: str,
        *,
        unit_limit: int = _MAX_OUTPUT_TEXT_UNIT,
        expansion_factor: int = 1,
    ) -> _BudgetedText:
        original_length = len(value)
        if original_length >= _MIN_TRACKED_TEXT_ALIAS:
            identity = id(value)
            if identity in self.seen_large_text_ids:
                return self._marker(
                    _REPEATED_TEXT_OMISSION,
                    encoding="repeated-text-reference",
                    original_length=original_length,
                )
            self.seen_large_text_ids.add(identity)

        safe_unit_limit = max(0, min(unit_limit, _MAX_OUTPUT_TEXT_UNIT))
        safe_expansion_factor = max(1, min(expansion_factor, 8))
        truncated = original_length > safe_unit_limit
        candidate = value[:safe_unit_limit] if truncated else value
        marker = _OUTPUT_TEXT_UNIT_OMISSION if truncated else None
        required = (
            len(candidate) * safe_expansion_factor
            + (len(marker) if marker else 0)
        )
        reserve = min(_FOLLOWING_TEXT_RESERVE, self.remaining)
        can_use_reserve = required <= 4_096
        available = self.remaining if can_use_reserve else self.remaining - reserve
        if required <= max(0, available):
            self.remaining -= required
            return _BudgetedText(
                candidate,
                marker,
                "bounded-text" if truncated else None,
                original_length,
            )
        return self._marker(
            _OUTPUT_TEXT_OMISSION,
            encoding="text-budget-limit",
            original_length=original_length,
        )

    def _marker(
        self,
        marker: str,
        *,
        encoding: str,
        original_length: int,
    ) -> _BudgetedText:
        if len(marker) > self.remaining:
            return _BudgetedText(
                encoding=encoding,
                original_length=original_length,
            )
        self.remaining -= len(marker)
        return _BudgetedText(
            marker=marker,
            encoding=encoding,
            original_length=original_length,
        )


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
    lossiness: Lossiness = Lossiness.NONE

    @property
    def is_lossy(self) -> bool:
        value = getattr(self.lossiness, "value", self.lossiness)
        return value != Lossiness.NONE.value


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
        return _paragraph_text(self, _TextOutputBudget())


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
        return _table_cell_text(self, _TextOutputBudget())


def _paragraph_text(
    paragraph: Paragraph,
    text_budget: _TextOutputBudget,
    *,
    expansion_factor: int = 1,
) -> str:
    runs = paragraph.runs
    if not isinstance(runs, list | tuple):
        return "[Invalid paragraph runs omitted]"
    parts: list[str] = []
    seen: set[int] = set()
    omitted = len(runs) > 4_096
    paragraph_remaining = 1_000_000
    for run in runs[:4_096]:
        if not isinstance(run, TextRun) or id(run) in seen:
            omitted = True
            continue
        seen.add(id(run))
        value = run.text
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if not isinstance(value, str):
            omitted = True
            continue
        prepared = text_budget.prepare(
            value,
            unit_limit=paragraph_remaining,
            expansion_factor=expansion_factor,
        )
        if prepared.visible:
            parts.append(prepared.visible)
        paragraph_remaining -= len(prepared.text)
        if prepared.encoding in {"bounded-text", "text-budget-limit"}:
            omitted = True
        if paragraph_remaining <= 0:
            omitted = True
            break
    if omitted:
        parts.append("[Paragraph content omitted at safe rendering limit]")
    return "".join(parts)


def _table_cell_text(
    cell: TableCell,
    text_budget: _TextOutputBudget,
    *,
    expansion_factor: int = 1,
) -> str:
    blocks = cell.blocks
    if not isinstance(blocks, list | tuple):
        return "[Invalid table cell content omitted]"
    parts: list[str] = []
    seen: set[int] = set()
    omitted = len(blocks) > 4_096
    for block in blocks[:4_096]:
        if not isinstance(block, Paragraph) or id(block) in seen:
            omitted = True
            continue
        seen.add(id(block))
        parts.append(
            _paragraph_text(
                block,
                text_budget,
                expansion_factor=expansion_factor,
            )
        )
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

    @property
    def is_lossy(self) -> bool:
        return any(
            isinstance(item, Diagnostic) and item.is_lossy
            for item in self.diagnostics
        )

    @property
    def is_lossless(self) -> bool:
        return not self.is_lossy

    @property
    def preservation_losses(self) -> tuple[Diagnostic, ...]:
        return tuple(
            item
            for item in self.diagnostics
            if isinstance(item, Diagnostic) and item.is_lossy
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(
            self,
            max_items=_MAX_JSON_ITEMS,
            max_integer_bits=_MAX_JSON_INTEGER_BITS,
            max_recursion=_MAX_JSON_RECURSION,
        )


_MAX_TEXT_RECURSION = 64
_MAX_TEXT_BLOCKS = 100_000


def _blocks_text(
    blocks: list[Block],
    *,
    _active_containers: set[int] | None = None,
    _seen_containers: set[int] | None = None,
    _active_blocks: set[int] | None = None,
    _seen_blocks: set[int] | None = None,
    _reported_repeated_blocks: set[int] | None = None,
    _text_budget: _TextOutputBudget | None = None,
    _depth: int = 0,
) -> str:
    if not isinstance(blocks, list):
        return "[Invalid nested content omitted]"
    if _depth >= _MAX_TEXT_RECURSION:
        return "[Nested content depth limit reached]"
    active_containers = set() if _active_containers is None else _active_containers
    seen_containers = set() if _seen_containers is None else _seen_containers
    active_blocks = set() if _active_blocks is None else _active_blocks
    seen_blocks = set() if _seen_blocks is None else _seen_blocks
    reported_repeated_blocks = (
        set() if _reported_repeated_blocks is None else _reported_repeated_blocks
    )
    text_budget = _TextOutputBudget() if _text_budget is None else _text_budget
    identity = id(blocks)
    if identity in active_containers:
        return "[Recursive content omitted]"
    if identity in seen_containers:
        return "[Repeated content container omitted]"
    active_containers.add(identity)
    seen_containers.add(identity)

    def nested(nested_blocks: list[Block]) -> str:
        return _blocks_text(
            nested_blocks,
            _active_containers=active_containers,
            _seen_containers=seen_containers,
            _active_blocks=active_blocks,
            _seen_blocks=seen_blocks,
            _reported_repeated_blocks=reported_repeated_blocks,
            _text_budget=text_budget,
            _depth=_depth + 1,
        )

    try:
        parts: list[str] = []
        for block in blocks[:_MAX_TEXT_BLOCKS]:
            block_identity = id(block)
            if block_identity in active_blocks:
                parts.append("[Recursive content omitted]")
                continue
            if block_identity in seen_blocks:
                if block_identity not in reported_repeated_blocks:
                    parts.append("[Repeated block omitted at safe text limit]")
                    reported_repeated_blocks.add(block_identity)
                continue
            active_blocks.add(block_identity)
            seen_blocks.add(block_identity)
            try:
                if isinstance(block, Paragraph):
                    parts.append(_paragraph_text(block, text_budget))
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
                            values.append(_table_cell_text(cell, text_budget))
                        parts.append("\t".join(values))
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
                        status = (
                            block.status if isinstance(block.status, str) else "unavailable"
                        )
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
                    parts.append(nested(block.blocks))
                elif isinstance(block, Annotation):
                    parts.append("[Annotation]\n" + nested(block.blocks))
                elif isinstance(block, Footnote):
                    label = (
                        f"Footnote {block.number}"
                        if block.number is not None
                        else "Footnote"
                    )
                    parts.append(f"[{label}]\n" + nested(block.blocks))
                elif isinstance(block, Header | Footer):
                    label = type(block).__name__
                    parts.append(
                        f"[{label}: {block.placement} pages]\n" + nested(block.blocks)
                    )
                else:
                    parts.append("[Unrecognized block omitted]")
            finally:
                active_blocks.remove(block_identity)
        if len(blocks) > _MAX_TEXT_BLOCKS:
            parts.append("[Block content omitted at safe text limit]")
        return "\n".join(parts)
    finally:
        active_containers.remove(identity)


_MAX_JSON_RECURSION = 64
_MAX_JSON_ITEMS = 100_000
_MAX_JSON_INTEGER_BITS = 1_024
_JSON_BLOCK_TYPES = (
    Paragraph,
    PageBreak,
    Table,
    Image,
    WmfGraphic,
    SdwDrawing,
    UnsupportedObject,
    Frame,
    Annotation,
    Footnote,
    Header,
    Footer,
)
_JSON_BLOCK_OWNERS = (Document, Frame, Annotation, Footnote, Header, Footer)


def _jsonable(
    value: Any,
    *,
    max_items: int = _MAX_JSON_ITEMS,
    max_integer_bits: int = _MAX_JSON_INTEGER_BITS,
    max_recursion: int = _MAX_JSON_RECURSION,
    _active: set[int] | None = None,
    _expanded: set[int] | None = None,
    _text_budget: _TextOutputBudget | None = None,
    _depth: int = 0,
    _block_container: bool = False,
) -> Any:
    """Return a bounded JSON-compatible tree for model and renderer APIs.

    ``SourceSpan`` aliases are expected because one physical SAM record may
    produce several typed IR nodes.  They are fixed-size and are therefore
    serialized at every use.  Other repeated dataclasses and containers become
    explicit references after their first expansion, which prevents a small
    aliased object graph from multiplying into an unbounded JSON tree.
    """

    text_budget = _TextOutputBudget() if _text_budget is None else _text_budget
    if isinstance(value, Enum):
        return _jsonable(
            value.value,
            max_items=max_items,
            max_integer_bits=max_integer_bits,
            max_recursion=max_recursion,
            _active=_active,
            _expanded=_expanded,
            _text_budget=text_budget,
            _depth=_depth,
        )
    if isinstance(value, Path):
        return _jsonable(
            str(value),
            max_items=max_items,
            max_integer_bits=max_integer_bits,
            max_recursion=max_recursion,
            _active=_active,
            _expanded=_expanded,
            _text_budget=text_budget,
            _depth=_depth,
        )
    if isinstance(value, bytes):
        return {"length": len(value), "encoding": "not-inlined"}
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        prepared = text_budget.prepare(value, expansion_factor=6)
        if prepared.encoding is None:
            return prepared.text
        result: dict[str, Any] = {
            "encoding": prepared.encoding,
            "message": prepared.marker or _OUTPUT_TEXT_OMISSION,
            "original_length": prepared.original_length,
        }
        if prepared.text:
            result["text"] = prepared.text
        return result
    if isinstance(value, int):
        if value.bit_length() <= max_integer_bits:
            return value
        return {
            "encoding": "bounded-integer",
            "sign": "negative" if value < 0 else "positive",
            "bits": value.bit_length(),
        }
    if isinstance(value, float):
        return value if math.isfinite(value) else {"encoding": "non-finite-number"}

    if _depth >= max_recursion:
        return {"encoding": "nested-depth-limit", "type": _json_type_name(value)}

    if _block_container and not isinstance(value, list | tuple):
        return {
            "encoding": "invalid-block-container",
            "message": "[Invalid block container omitted]",
            "type": _json_type_name(value),
        }

    if is_dataclass(value) or isinstance(value, dict | list | tuple):
        active = set() if _active is None else _active
        expanded = set() if _expanded is None else _expanded
        identity = id(value)
        if identity in active:
            return {"encoding": "recursive-reference", "type": _json_type_name(value)}
        repeat_safe = isinstance(value, SourceSpan) or (
            isinstance(value, tuple) and not value
        )
        if identity in expanded and not repeat_safe:
            return {
                "encoding": "repeated-reference",
                "message": "[Repeated value omitted to bound JSON expansion]",
                "type": _json_type_name(value),
            }
        active.add(identity)
        expanded.add(identity)
        try:
            if is_dataclass(value):
                result: dict[str, Any] = {}
                block_owner = isinstance(value, _JSON_BLOCK_OWNERS)
                for item in fields(value):
                    try:
                        field_value = getattr(value, item.name)
                    except (AttributeError, TypeError, ValueError, OverflowError):
                        field_value = _UnreadableJsonValue()
                    result[item.name] = _jsonable(
                        field_value,
                        max_items=max_items,
                        max_integer_bits=max_integer_bits,
                        max_recursion=max_recursion,
                        _active=active,
                        _expanded=expanded,
                        _text_budget=text_budget,
                        _depth=_depth + 1,
                        _block_container=block_owner and item.name == "blocks",
                    )
                result["type"] = _json_type_name(value)
                return result

            if isinstance(value, dict):
                return _json_mapping(
                    value,
                    active=active,
                    expanded=expanded,
                    depth=_depth,
                    max_items=max_items,
                    max_integer_bits=max_integer_bits,
                    max_recursion=max_recursion,
                    text_budget=text_budget,
                )

            items = list(value[:max_items])
            result_items: list[Any] = []
            for item in items:
                if _block_container and not isinstance(item, _JSON_BLOCK_TYPES):
                    result_items.append(
                        {
                            "encoding": "unrecognized-block-object",
                            "message": "[Unrecognized block object omitted]",
                            "type": _json_type_name(item),
                        }
                    )
                else:
                    result_items.append(
                        _jsonable(
                            item,
                            max_items=max_items,
                            max_integer_bits=max_integer_bits,
                            max_recursion=max_recursion,
                            _active=active,
                            _expanded=expanded,
                            _text_budget=text_budget,
                            _depth=_depth + 1,
                        )
                    )
            if len(value) > max_items:
                result_items.append(
                    {
                        "encoding": "block-limit" if _block_container else "sequence-limit",
                        "message": "[Content omitted at safe JSON item limit]",
                        "omitted_count": len(value) - max_items,
                    }
                )
            return result_items
        finally:
            active.remove(identity)

    return {"encoding": "unsupported-value", "type": _json_type_name(value)}


def _json_mapping(
    value: dict[Any, Any],
    *,
    active: set[int],
    expanded: set[int],
    depth: int,
    max_items: int,
    max_integer_bits: int,
    max_recursion: int,
    text_budget: _TextOutputBudget,
) -> Any:
    item_count = len(value)
    limited = list(islice(value.items(), max_items))
    direct_string_keys = all(isinstance(key, str) for key, _item in limited)
    direct_key_characters = (
        sum(len(key) for key, _item in limited) if direct_string_keys else 0
    )
    if (
        direct_string_keys
        and direct_key_characters <= _MAX_OUTPUT_TEXT_UNIT
        and item_count <= max_items
    ):
        return {
            key: _jsonable(
                item,
                max_items=max_items,
                max_integer_bits=max_integer_bits,
                max_recursion=max_recursion,
                _active=active,
                _expanded=expanded,
                _text_budget=text_budget,
                _depth=depth + 1,
            )
            for key, item in limited
        }

    entries = [
        {
            "key": _json_mapping_key(
                key,
                max_integer_bits=max_integer_bits,
                text_budget=text_budget,
            ),
            "value": _jsonable(
                item,
                max_items=max_items,
                max_integer_bits=max_integer_bits,
                max_recursion=max_recursion,
                _active=active,
                _expanded=expanded,
                _text_budget=text_budget,
                _depth=depth + 1,
            ),
        }
        for key, item in limited
    ]
    result: dict[str, Any] = {"encoding": "mapping-entries", "entries": entries}
    if item_count > max_items:
        result["omitted_count"] = item_count - max_items
        result["message"] = "[Mapping entries omitted at safe JSON item limit]"
    return result


def _json_mapping_key(
    value: object,
    *,
    max_integer_bits: int,
    text_budget: _TextOutputBudget,
) -> dict[str, Any]:
    if isinstance(value, str):
        return {
            "type": "str",
            "value": _jsonable(
                value,
                max_integer_bits=max_integer_bits,
                _text_budget=text_budget,
            ),
        }
    if value is None:
        return {"type": "none"}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {
            "type": "int",
            "value": _jsonable(
                value,
                max_integer_bits=max_integer_bits,
                _text_budget=text_budget,
            ),
        }
    if isinstance(value, float):
        return {
            "type": "float",
            "value": _jsonable(value, _text_budget=text_budget),
        }
    if isinstance(value, Enum):
        return {
            "type": "enum",
            "enum_type": _json_type_name(value),
            "value": _jsonable(
                value.value,
                max_integer_bits=max_integer_bits,
                _text_budget=text_budget,
            ),
        }
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value), "encoding": "not-inlined"}
    return {"type": _json_type_name(value), "encoding": "unsupported-key"}


def _json_type_name(value: object) -> str:
    name = getattr(type(value), "__name__", "unknown")
    if not isinstance(name, str):
        return "unknown"
    safe = "".join(
        character for character in name[:64] if character.isalnum() or character == "_"
    )
    return safe or "unknown"


class _UnreadableJsonValue:
    pass
