"""Tolerant, loss-preserving parser for Ami Pro 3.x SAM documents."""

from __future__ import annotations

import copy
import hashlib
import math
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from pathlib import Path

from .decoding import DecodedSource, decode_bytes
from .errors import ParseError, PreservationLossError, ResourceLimitError
from .limits import ParseLimits
from .model import (
    Annotation,
    Block,
    CharacterStyle,
    Document,
    Footer,
    Footnote,
    FootnoteOptions,
    Frame,
    Header,
    Image,
    Lossiness,
    OpaquePageHints,
    PageLayout,
    PageVariantGeometry,
    Paragraph,
    SdwDrawing,
    SectionRecord,
    Severity,
    SourceSpan,
    StyleDefinition,
    Table,
    TableCell,
    TableColumnDefinition,
    TableDefinition,
    TableRow,
    TextRun,
    TwipRect,
    UnknownRecord,
    UnsupportedObject,
    WmfGraphic,
)
from .model import Diagnostic as _DiagnosticRecord
from .sdw import SdwDecodeError, decode_sdw_preview, sdw_asset_limit, validate_sdw
from .syntax import (
    MultilineContainerScanner,
    parse_embedded_manifest_row,
)
from .wmf import WmfDecodeError, decode_wmf

_MAIN_SECTION = re.compile(r"^\[([A-Za-z][A-Za-z0-9_-]{0,63})\]$")
_SUBSECTION = re.compile(r"^\s+\[([A-Za-z][A-Za-z0-9_-]{0,63})\]\s*$")
_EMBEDDED_MANIFEST = re.compile(
    rb"(?m)^\s*(?P<id>\d+)\s+(?P<ext>\.[A-Za-z0-9]{1,8})\s+"
    rb"(?P<asset_offset>\d+)\s+(?P<asset_length>\d+)\s+"
    rb"(?P<preview_offset>\d+)\s+(?P<preview_length>\d+)\s*$"
)
_FONT_TAG = re.compile(
    r"^:f(?P<size>-?\d+)?(?:,(?P<family>[^,]*),(?P<red>-?\d+),(?P<green>-?\d+),(?P<blue>-?\d+))?$",
    re.IGNORECASE,
)
_LINE_SPACING = re.compile(r"^:S\+(?P<value>-?\d+(?:\.\d+)?)$", re.IGNORECASE)
_PARAGRAPH_LAYOUT = re.compile(r"^:#(?P<first>-?\d+)(?:,(?P<rest>-?\d+))?$")
_FRAME_ANCHOR = re.compile(r"(?<!<)<:(?P<kind>t|A)(?P<index>\d+)>")
_MAX_INLINE_COMMANDS_PER_PARAGRAPH = 4_095
_MAX_OPAQUE_TABLE_FIELD_ENTRIES = 256
_MAX_OPAQUE_TABLE_FIELD_CHARS = 16_384
_LAYOUT_TABBED_MARKER = re.compile(
    r"^(?P<indent>\t*)\[(?P<name>[A-Za-z][A-Za-z0-9_-]{0,63})\]\s*$"
)
_LAYOUT_BRANCH_MARKER = re.compile(
    r"^(?P<indent>[ \t]*)\[(?P<name>hrght|hlft|frght|flft)\]\s*$",
    re.IGNORECASE,
)

_KNOWN_LAYOUT_SUBRECORDS = {
    "rght",
    "lft",
    "hrght",
    "hlft",
    "frght",
    "flft",
    "lyfrm",
    "frmlay",
    "txt",
}
_KNOWN_FRAME_SUBRECORDS = {
    "tbl",
    "data",
    "e",
    "tble",
    "txt",
    "isd",
    "btmap",
    "frmlay",
    "lyfrm",
}

_KNOWN_HEADER_SECTIONS = {
    "ver",
    "sty",
    "files",
    "charset",
    "revisions",
    "prn",
    "port",
    "lang",
    "desc",
    "fopts",
    "lnopts",
    "docopts",
    "gramstyle",
    "fldnames",
    "paranum",
    "recfile",
    "toc",
    "book",
    "master",
    "docvars",
    "chint",
    "ehint",
}
_OPAQUE_HEADER_SECTIONS = {
    "book",
    "chint",
    "docopts",
    "docvars",
    "ehint",
    "files",
    "fldnames",
    "gramstyle",
    "lnopts",
    "master",
    "paranum",
    "port",
    "prn",
    "recfile",
    "toc",
}
_STRUCTURAL_SECTIONS = {
    "frm",
    "lay",
    "pg",
    "embedded",
    "newmac",
    "macro",
    "frmmac",
}
_DANGEROUS_SECTIONS = {"newmac", "macro", "frmmac"}
_LOSSLESS_DIAGNOSTICS = {
    "decode-selected",
    "embedded-directory",
    "frame-field-summary-truncated",
    "missing-edoc",
    "page-geometry-summary-truncated",
    "page-layout-summary-truncated",
}
_CONTENT_LOSS_DIAGNOSTICS = {
    "embedded-asset-too-large",
    "embedded-companion-unsupported",
    "embedded-format-unsupported",
    "embedded-offset-invalid",
    "frame-anchor-out-of-range",
    "frame-image-unavailable",
    "preview-offset-invalid",
    "sdw-asset-too-large",
    "sdw-asset-unavailable",
    "sdw-preview-too-large",
    "unterminated-edoc-opaque-tail",
    "unindexed-trailing-data",
}


def Diagnostic(  # noqa: N802 - parser-local factory mirrors the model constructor
    severity: Severity,
    code: str,
    message: str,
    source: SourceSpan | None = None,
    raw: str | None = None,
    *,
    lossiness: Lossiness | None = None,
) -> _DiagnosticRecord:
    """Create a parser diagnostic with an explicit preservation classification."""

    if lossiness is None:
        if code in _LOSSLESS_DIAGNOSTICS:
            lossiness = Lossiness.NONE
        elif code in _CONTENT_LOSS_DIAGNOSTICS or code.startswith("wmf-"):
            lossiness = Lossiness.CONTENT
        else:
            lossiness = Lossiness.SEMANTIC
    return _DiagnosticRecord(
        severity=severity,
        code=code,
        message=message,
        source=source,
        raw=raw,
        lossiness=lossiness,
    )


@dataclass(slots=True)
class _InlineState:
    style: CharacterStyle = field(default_factory=CharacterStyle)
    alignment: str | None = None
    line_spacing: float | None = None
    style_name: str | None = None
    left_indent_in: float | None = None
    first_line_indent_in: float | None = None
    region_x_twips: int | None = None
    region_width_twips: int | None = None
    inline_indent_twips: tuple[int, int, int, int] | None = None
    page_break_before: bool = False
    unknown_tags: list[str] = field(default_factory=list)
    unapplied_tags: list[str] = field(default_factory=list)
    open_dynamic_fields: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _FrameContent:
    kind: str
    frame: Frame
    source: SourceSpan

    @property
    def blocks(self) -> list[Block]:
        return self.frame.blocks


@dataclass(slots=True)
class _StructureResult:
    anchored_frames: list[_FrameContent] = field(default_factory=list)
    supplemental_blocks: list[Block] = field(default_factory=list)


@dataclass(slots=True)
class _OpenContainer:
    kind: str
    metadata: str
    source: SourceSpan
    raw_lines: list[str] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    paragraph_lines: list[str] = field(default_factory=list)
    paragraph_source: SourceSpan | None = None


@dataclass(slots=True)
class _RecordBudget:
    limit: int
    used: int = 0

    def charge(self, count: int, description: str) -> None:
        self.used += count
        if self.used > self.limit:
            raise ResourceLimitError(
                f"{description} exceeds {self.limit} materialized records"
            )


def _effective_lowerable_limit(
    configured: object, hard_limit: int, description: str
) -> int:
    """Clamp a caller limit to its built-in ceiling and reject invalid settings."""

    if (
        isinstance(configured, bool)
        or not isinstance(configured, int)
        or configured < 0
    ):
        raise ResourceLimitError(
            f"{description} must be configured as a nonnegative integer"
        )
    return min(configured, hard_limit)


def parse_file(
    path: str | Path,
    *,
    limits: ParseLimits | None = None,
    encoding: str | None = None,
    strict: bool = False,
) -> Document:
    source = Path(path)
    limits = limits or ParseLimits()
    file_limit = _effective_lowerable_limit(
        limits.max_file_bytes,
        ParseLimits().max_file_bytes,
        "input byte limit",
    )
    try:
        size = source.stat().st_size
    except OSError:
        raise
    if size > file_limit:
        raise ResourceLimitError(
            f"input is {size} bytes; configured maximum is {file_limit}"
        )
    data = source.read_bytes()
    return parse_bytes(
        data,
        source_name=source.name,
        source_directory=source.resolve().parent,
        limits=limits,
        encoding=encoding,
        strict=strict,
    )


def parse_bytes(
    data: bytes,
    *,
    source_name: str = "<memory>",
    source_directory: Path | None = None,
    limits: ParseLimits | None = None,
    encoding: str | None = None,
    strict: bool = False,
) -> Document:
    limits = limits or ParseLimits()
    if not data:
        raise ParseError("input is empty")
    decoded = decode_bytes(data, limits=limits, encoding=encoding)
    all_lines = decoded.text.splitlines()
    if not all_lines:
        raise ParseError("not an Ami Pro SAM document: expected [ver] at byte offset 0")
    version_line = next(
        (index for index, line in enumerate(all_lines) if line.strip().lower() == "[ver]"), None
    )
    if version_line is None or decoded.line_byte_offsets[version_line] > 4096:
        raise ParseError("not an Ami Pro SAM document: expected [ver] near byte offset 0")
    if version_line and not any(
        line.strip().lower() == "[sty]" for line in all_lines[version_line + 1 : version_line + 6]
    ):
        raise ParseError("not an Ami Pro SAM document: recovery preamble is not followed by [sty]")

    document = Document(
        source_name=source_name,
        source_directory=source_directory,
        encoding=decoded.encoding,
        original_size=len(data),
        newline=decoded.newline,
        diagnostics=list(decoded.diagnostics),
    )
    if decoded.directory_pointer_valid is False:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "embedded-directory-pointer-mismatch",
                "[Embedded] rows were retained, but the terminal directory pointer "
                "does not match the marker byte offset",
            )
        )
    if version_line:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "leading-preamble",
                f"ignored {decoded.line_byte_offsets[version_line]} byte(s) before [ver]",
                raw="\n".join(all_lines[:version_line]),
            )
        )
    logical_lines, nul_opaque_tail = _logical_lines(
        all_lines[version_line:], start_index=version_line
    )
    sections = _collect_sections(logical_lines, decoded, limits)
    document.sections = sections
    _parse_metadata_and_styles(document, sections, decoded, limits)
    if document.version is None:
        raise ParseError("malformed Ami Pro SAM document: [ver] has no version value")
    if not any(section.name.lower() == "sty" for section in sections):
        raise ParseError("malformed Ami Pro SAM document: required [sty] section is missing")
    materialization_budget = _RecordBudget(
        _effective_lowerable_limit(
            limits.max_records,
            ParseLimits().max_records,
            "content record limit",
        )
    )
    structures = _parse_structures(
        document,
        sections,
        data,
        decoded,
        limits,
        record_budget=materialization_budget,
        data_base_offset=decoded.line_byte_offsets[version_line],
    )
    edoc_sections = [section for section in sections if section.name.lower() == "edoc"]
    used_anchors: set[int] = set()
    if not edoc_sections:
        document.diagnostics.append(
            Diagnostic(Severity.WARNING, "missing-edoc", "document has no [edoc] text section")
        )
    else:
        _parse_text_stream(
            document,
            edoc_sections[0],
            limits,
            anchored_frames=structures.anchored_frames,
            used_anchors=used_anchors,
            record_budget=materialization_budget,
        )
    if nul_opaque_tail is not None:
        materialization_budget.charge(1, "unterminated EDOC opaque-tail recovery")
        _preserve_nul_opaque_tail(
            document,
            data,
            decoded,
            start_line=nul_opaque_tail[0],
            end_line=nul_opaque_tail[1],
        )

    for index, frame in enumerate(structures.anchored_frames):
        if index in used_anchors:
            continue
        document.blocks.append(
            UnsupportedObject(
                "unplaced anchored frame",
                f"anchor target {index} was not referenced by the body; recovered frame follows",
                frame.source,
            )
        )
        document.blocks.append(frame.frame)
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "unreferenced-anchored-frame",
                f"anchored {frame.kind} frame {index} was not referenced by [edoc]",
                frame.source,
            )
        )
    document.blocks.extend(structures.supplemental_blocks)
    _diagnose_unindexed_tail(document, data, decoded)

    _record_unknown_main_sections(document, sections)
    if strict and document.preservation_losses:
        raise PreservationLossError(document.preservation_losses)
    return document


def _collect_sections(
    lines: list[tuple[int, str]], decoded: DecodedSource, limits: ParseLimits
) -> list[SectionRecord]:
    sections: list[SectionRecord] = []
    current: SectionRecord | None = None
    for index, line in lines:
        match = _MAIN_SECTION.fullmatch(line.strip()) if line == line.lstrip() else None
        if match:
            current = SectionRecord(
                name=match.group(1),
                source=decoded.span_for_line(index, line),
                raw_lines=[],
                raw_spans=[],
            )
            sections.append(current)
            if len(sections) > limits.max_records:
                raise ResourceLimitError(f"document exceeds {limits.max_records} section records")
        elif current is not None:
            current.raw_lines.append(line)
            current.raw_spans.append(decoded.span_for_line(index, line))
    return sections


def _logical_lines(
    lines: list[str], *, start_index: int = 0
) -> tuple[list[tuple[int, str]], tuple[int, int | None] | None]:
    """Return scanner lines plus any byte range hidden by NUL-tail recovery.

    The line range uses absolute decoded line indexes.  Its optional end is the
    first line retained again for an appended ``[Embedded]`` directory; ``None``
    means the omitted range continues through the physical end of the source.
    """

    indexed = [(start_index + index, line) for index, line in enumerate(lines)]
    edoc = next(
        (index for index, (_, line) in enumerate(indexed) if line.strip().lower() == "[edoc]"),
        None,
    )
    if edoc is None:
        return indexed, None
    terminator = _edoc_terminator(indexed, edoc + 1)
    nul_boundary: int | None = None
    if terminator is None:
        nul_boundary = next(
            (
                index
                for index in range(edoc + 1, len(indexed))
                if "\x00" in indexed[index][1]
            ),
            None,
        )
        terminator = len(indexed) if nul_boundary is None else nul_boundary
    prefix = indexed[: min(terminator + 1, len(indexed))]
    embedded = next(
        (
            index
            for index in range(len(indexed) - 1, edoc, -1)
            if indexed[index][1].strip().lower() == "[embedded]"
        ),
        None,
    )
    opaque_tail: tuple[int, int | None] | None = None
    if nul_boundary is not None:
        omitted_start = nul_boundary + 1
        omitted_end = (
            embedded
            if embedded is not None and embedded > terminator
            else len(indexed)
        )
        if omitted_start < omitted_end:
            opaque_tail = (
                indexed[omitted_start][0],
                indexed[omitted_end][0] if omitted_end < len(indexed) else None,
            )
    if embedded is not None and embedded > terminator:
        return prefix + indexed[embedded:], opaque_tail
    return prefix, opaque_tail


def _preserve_nul_opaque_tail(
    document: Document,
    data: bytes,
    decoded: DecodedSource,
    *,
    start_line: int,
    end_line: int | None,
) -> None:
    """Represent bytes skipped after an unterminated EDOC NUL boundary."""

    start = decoded.line_byte_offsets[start_line]
    end = len(data) if end_line is None else decoded.line_byte_offsets[end_line]
    if end <= start:
        return
    length = end - start
    digest = hashlib.sha256(memoryview(data)[start:end]).hexdigest()
    source = SourceSpan(
        line=start_line + 1,
        column=1,
        byte_offset=start,
        end_byte_offset=end,
    )
    description = (
        f"{length} byte(s) after an unterminated [edoc] NUL recovery boundary "
        f"(SHA-256 {digest}); source bytes were retained as opaque evidence"
    )
    document.blocks.append(
        UnsupportedObject("unterminated EDOC opaque tail", description, source)
    )
    document.diagnostics.append(
        Diagnostic(
            Severity.WARNING,
            "unterminated-edoc-opaque-tail",
            description,
            source,
            lossiness=Lossiness.CONTENT,
        )
    )


def _edoc_terminator(lines: list[tuple[int, str]], start: int) -> int | None:
    """Find the outer EDOC close while skipping multiline annotation closes."""

    note_depth = 0
    scanner = MultilineContainerScanner()
    for index in range(start, len(lines)):
        line = lines[index][1]
        scan = scanner.scan_line(line)
        if scan.standalone_terminator:
            if note_depth:
                note_depth -= 1
                continue
            return index
        note_depth += int(scan.opener is not None)
    return None


def _parse_metadata_and_styles(
    document: Document,
    sections: list[SectionRecord],
    decoded: DecodedSource,
    limits: ParseLimits,
) -> None:
    section_counts: dict[str, int] = {}
    for section in sections:
        normalized = section.name.lower()
        section_counts[normalized] = section_counts.get(normalized, 0) + 1

    counts: dict[str, int] = {}
    for section in sections:
        name = section.name.lower()
        counts[name] = counts.get(name, 0) + 1
        values = [line.strip() for line in section.raw_lines]
        if name == "ver":
            if values:
                document.version = values[0]
                if values[0] not in {"3", "4"}:
                    document.diagnostics.append(
                        Diagnostic(
                            Severity.WARNING,
                            "unverified-version",
                            f"format version {values[0]!r} has not been verified",
                            section.source,
                        )
                    )
            _record_opaque_fields(
                document,
                section,
                section.raw_lines[1:],
                record_type="version-fields-tail",
                diagnostic_code="version-fields-opaque",
                object_kind="version header fields",
                description=(
                    "fields after the supported [ver] version were preserved "
                    "without interpretation; raw data remains in JSON"
                ),
            )
        elif name == "sty":
            if values and values[0]:
                document.metadata["stylesheet"] = values[0]
                document.diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        "external-stylesheet-not-loaded",
                        "external stylesheet reference was preserved but not followed",
                        section.source,
                        values[0],
                    )
                )
            _record_opaque_fields(
                document,
                section,
                section.raw_lines[1:],
                record_type="stylesheet-fields-tail",
                diagnostic_code="stylesheet-fields-opaque",
                object_kind="stylesheet header fields",
                description=(
                    "fields after the supported [sty] stylesheet reference were "
                    "preserved without interpretation; raw data remains in JSON"
                ),
            )
        elif name == "charset":
            document.metadata["charset"] = " | ".join(value for value in values if value)
        elif name in {"lang", "desc"} and values:
            document.metadata[name] = values[0]
            if len([value for value in values if value]) > 1:
                document.diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        "metadata-values-opaque",
                        f"additional [{section.name}] values remain opaque",
                        section.source,
                    )
                )
        elif name == "revisions":
            canonical_no_revisions = section_counts[name] == 1 and values == ["0"]
            if canonical_no_revisions:
                document.metadata[name] = "0"
                continue
            document.metadata[name] = next((value for value in values if value), "")
            document.blocks.append(
                UnsupportedObject(
                    "revision state",
                    "revision metadata was preserved, but changes were not interpreted",
                    section.source,
                )
            )
            document.unknown_records.append(
                UnknownRecord(
                    section=section.name,
                    record_type="revision-state",
                    raw="\n".join(section.raw_lines),
                    source=section.source,
                    reason="revision state is preserved raw but not interpreted",
                )
            )
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "revisions-opaque",
                    "revision state was preserved without semantic interpretation",
                    section.source,
                )
            )
        elif name == "l1":
            l1_value = _canonical_l1_value(section)
            if section_counts[name] == 1 and l1_value is not None:
                document.l1_value = l1_value
        elif name == "fopts":
            _parse_footnote_options(document, section, values)
        elif name == "tag":
            if len(document.styles) >= limits.max_styles:
                raise ResourceLimitError(f"document exceeds {limits.max_styles} styles")
            style = _parse_style(document, section, decoded)
            if style:
                document.styles[style.name] = style
        elif name in _OPAQUE_HEADER_SECTIONS and any(values):
            document.unknown_records.append(
                UnknownRecord(
                    section=section.name,
                    record_type="known-opaque-section",
                    raw="\n".join(section.raw_lines),
                    source=section.source,
                    reason="known section is preserved raw but its semantics are not interpreted",
                )
            )
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "known-section-opaque",
                    f"[{section.name}] is preserved without semantic interpretation",
                    section.source,
                )
            )

    if counts.get("ver", 0) != 1:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "version-section-count",
                f"expected one [ver] section, found {counts.get('ver', 0)}",
            )
        )


def _record_opaque_fields(
    document: Document,
    section: SectionRecord,
    raw_fields: list[str],
    *,
    record_type: str,
    diagnostic_code: str,
    object_kind: str,
    description: str,
    body_placeholder: bool = True,
) -> None:
    opaque_fields = [field for field in raw_fields if field.strip()]
    if not opaque_fields:
        return
    raw = "\n".join(opaque_fields)
    document.unknown_records.append(
        UnknownRecord(
            section=section.name,
            record_type=record_type,
            raw=raw,
            source=section.source,
            reason="fields outside the supported prefix were preserved without interpretation",
        )
    )
    if body_placeholder:
        document.blocks.append(UnsupportedObject(object_kind, description, section.source))
    document.diagnostics.append(
        Diagnostic(
            Severity.WARNING,
            diagnostic_code,
            description,
            section.source,
            raw,
            lossiness=Lossiness.SEMANTIC,
        )
    )


def _parse_footnote_options(
    document: Document, section: SectionRecord, values: list[str]
) -> None:
    raw = "\n".join(section.raw_lines)
    parsed = [_bounded_small_signed(value) for value in values[:4]]
    if len(values) < 4 or any(value is None for value in parsed):
        document.unknown_records.append(
            UnknownRecord(
                section="fopts",
                record_type="footnote-options",
                raw=raw,
                source=section.source,
                reason="malformed footnote options were preserved without interpretation",
            )
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "malformed-footnote-options",
                "[fopts] requires four bounded integer fields",
                section.source,
            )
        )
        return
    flags, start, separator, indent = (int(value) for value in parsed)
    if not (0 <= flags <= 0xFFFF and 0 <= start <= 9999):
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "footnote-options-out-of-range",
                "[fopts] flags or start number are outside documented bounds",
                section.source,
            )
        )
        return
    if not (0 <= separator <= 32767 and 0 <= indent <= 32767):
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "footnote-options-out-of-range",
                "[fopts] separator length or indent are outside documented bounds",
                section.source,
            )
        )
        return
    opaque_tail = [value for value in values[4:] if value]
    if opaque_tail:
        document.unknown_records.append(
            UnknownRecord(
                section="fopts",
                record_type="footnote-options-tail",
                raw="\n".join(opaque_tail),
                source=section.source,
                reason=(
                    "fields after the supported four-field prefix were preserved "
                    "without interpretation"
                ),
            )
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "footnote-options-tail-opaque",
                "[fopts] fields after the supported four-field prefix were "
                "preserved without semantic interpretation",
                section.source,
            )
        )
    unknown_bits = flags & ~7
    document.footnote_options = FootnoteOptions(
        flags=flags,
        collect_at_page_end=bool(flags & 1),
        reset_number_each_page=bool(flags & 2),
        separator_line=bool(flags & 4),
        start_number=start,
        separator_length_in=separator / 1440.0,
        indent_in=indent / 1440.0,
        unknown_flag_bits=unknown_bits,
        raw=raw,
        source=section.source,
    )
    if unknown_bits:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "footnote-options-unknown-flags",
                f"[fopts] has unsupported flag bits 0x{unknown_bits:x}",
                section.source,
            )
        )


def _bounded_small_signed(value: str) -> int | None:
    if not re.fullmatch(r"[+-]?\d{1,20}", value):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _bounded_inline_int(value: str) -> int | None:
    parsed = _bounded_small_signed(value)
    if parsed is None or not -(2**31) <= parsed <= 2**31 - 1:
        return None
    return parsed


def _bounded_ordinary_name(value: str) -> str | None:
    decoded = _unescape_literal(value.strip())
    if not decoded or len(decoded) > 256:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        return None
    if decoded == ">" or _MAIN_SECTION.fullmatch(decoded):
        return None
    return decoded


def _canonical_l1_value(section: SectionRecord) -> int | None:
    if len(section.raw_lines) != 1:
        return None
    parsed = _bounded_inline_int(section.raw_lines[0].strip())
    return parsed if parsed is not None and parsed >= 0 else None


_MAX_PAGE_TWIPS = 22 * 1440
_MIN_PAGE_TWIPS = 1440
_MIN_CONTENT_TWIPS = 720
_MIN_FRAME_COORD = -32768
_MAX_FRAME_COORD = 32767
_MAX_TYPED_GEOMETRY_FIELDS = 1024
_PAGE_LAYOUT_FEATURE_FLAGS = 256 | 512 | 1024 | 2048 | 4096
_STYLE_CHARACTER_KNOWN_FLAGS = 1 | 2 | 4 | 8 | 64 | 128 | 256 | 512 | 0xC000
_STYLE_ALIGNMENT_KNOWN_FLAGS = 1 | 2 | 4 | 8
_STYLE_SPACING_KNOWN_FLAGS = 1 | 2 | 4 | 8
_FRAME_KNOWN_FLAGS = (
    1
    | 2
    | 4
    | 64
    | 128
    | 256
    | 512
    | 2048
    | 4096
    | 8192
    | 65536
    | 524288
)


def _parse_page_layout(
    document: Document, section: SectionRecord, layout_index: int
) -> PageLayout:
    """Parse the documented, bounded subset of one ``[lay]`` record."""

    prefix, prefix_truncated = _prefix_fields(section.raw_lines)
    if prefix_truncated:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "page-layout-summary-truncated",
                f"[lay] typed prefix was capped at {_MAX_TYPED_GEOMETRY_FIELDS} fields; "
                "the complete record remains in raw form",
                section.source,
            )
        )
    name = _unescape_literal(prefix[0]) if prefix else ""
    flags = _bounded_small_signed(prefix[1]) if len(prefix) > 1 else None
    if len(prefix) > 2 or prefix_truncated:
        document.unknown_records.append(
            UnknownRecord(
                section=section.name,
                record_type="page-layout-fields",
                raw="\n".join(prefix[2:]),
                source=section.source,
                reason=(
                    "fields after the supported layout name and flag word were "
                    "preserved without interpretation"
                ),
            )
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "page-layout-fields-opaque",
                "[lay] fields after the supported name-and-flags prefix were "
                "preserved without semantic interpretation",
                section.source,
            )
        )
    if flags is not None and not 0 <= flags <= 0x7FFFFFFF:
        flags = None
    if flags is None:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "malformed-page-layout-flags",
                "[lay] has no bounded nonnegative flag word; geometry was retained independently",
                section.source,
            )
        )

    paper_codes = {
        1: "letter",
        2: "legal",
        3: "a3",
        4: "a4",
        5: "a5",
        6: "b5",
        7: "custom",
    }
    paper_kind = paper_codes.get(flags & 0xFF, "unknown") if flags is not None else "unknown"
    odd = _parse_page_variant(document, section, "rght", "odd")
    even = _parse_page_variant(document, section, "lft", "even")
    layout = PageLayout(
        index=layout_index,
        name=name,
        flags=flags,
        paper_kind=paper_kind,  # type: ignore[arg-type]
        orientation=(
            "landscape"
            if flags is not None and flags & 256
            else "portrait"
            if flags is not None
            else "unknown"
        ),
        non_alternating=bool(flags is not None and flags & 512),
        mirrored=bool(flags is not None and flags & 1024),
        second_header=bool(flags is not None and flags & 2048),
        second_footer=bool(flags is not None and flags & 4096),
        unknown_flag_bits=(flags & ~(0xFF | _PAGE_LAYOUT_FEATURE_FLAGS)) if flags else 0,
        odd=odd,
        even=even,
        valid=bool((odd and odd.valid) or (even and even.valid)),
        raw="\n".join(section.raw_lines),
        source=section.source,
    )
    if not layout.valid:
        layout.reason = "no complete, bounded right/odd or left/even page geometry"
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "page-layout-unusable",
                layout.reason,
                section.source,
            )
        )
    if layout.unknown_flag_bits:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "page-layout-unknown-flags",
                f"[lay] has unsupported flag bits 0x{layout.unknown_flag_bits:x}",
                section.source,
            )
        )
    if paper_kind == "unknown" and flags is not None:
        document.diagnostics.append(
            Diagnostic(
                Severity.INFO,
                "unknown-page-size-code",
                f"[lay] page-size code {flags & 0xFF} remains uninterpreted",
                section.source,
            )
        )
    return layout


def _parse_page_variant(
    document: Document,
    section: SectionRecord,
    marker_name: str,
    side: str,
) -> PageVariantGeometry | None:
    marker_indexes = [
        index
        for index, line in enumerate(section.raw_lines)
        if (match := _LAYOUT_TABBED_MARKER.fullmatch(line)) is not None
        and len(match.group("indent")) == 1
        and match.group("name").lower() == marker_name
    ]
    if not marker_indexes:
        return None
    duplicate = len(marker_indexes) > 1
    if duplicate:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "duplicate-page-variant",
                f"[lay] has {len(marker_indexes)} [{marker_name}] branches; "
                "only the first geometry is typed",
                section.raw_spans[marker_indexes[1]],
            )
        )
    start = marker_indexes[0]
    end = next(
        (
            index
            for index in range(start + 1, len(section.raw_lines))
            if re.fullmatch(
                r"\t\[[A-Za-z][A-Za-z0-9_-]{0,63}\]\s*",
                section.raw_lines[index],
            )
        ),
        len(section.raw_lines),
    )
    raw_fields: list[str] = []
    omitted_fields = 0
    for line in section.raw_lines[start + 1 : end]:
        value = line.strip()
        if not value:
            continue
        if value.startswith("[") or value == ">":
            break
        if len(raw_fields) < _MAX_TYPED_GEOMETRY_FIELDS:
            raw_fields.append(value)
        else:
            omitted_fields += 1
    source = section.raw_spans[start]
    geometry = PageVariantGeometry(
        side=side,  # type: ignore[arg-type]
        raw_fields=tuple(raw_fields),
        source=source,
    )
    if omitted_fields:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "page-geometry-summary-truncated",
                f"[{marker_name}] typed field summary was capped at {_MAX_TYPED_GEOMETRY_FIELDS}; "
                f"{omitted_fields} additional field(s) remain in the raw [lay] record",
                source,
            )
        )
    if len(raw_fields) > 9 or omitted_fields:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "page-geometry-tail-opaque",
                f"[{marker_name}] fields after the typed nine-field prefix were "
                "preserved without semantic interpretation",
                source,
            )
        )
    parsed = [_bounded_small_signed(value) for value in raw_fields[:9]]
    if len(raw_fields) < 9 or any(value is None for value in parsed):
        geometry.reason = "page variant requires a nine-field bounded integer prefix"
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "malformed-page-geometry",
                f"[{marker_name}] {geometry.reason}",
                source,
                "\n".join(raw_fields),
            )
        )
        return geometry

    (
        height,
        width,
        reserved,
        margin_left,
        margin_bottom,
        display_unit,
        margin_top,
        margin_right,
        flags,
    ) = (int(value) for value in parsed)
    geometry.height_twips = height
    geometry.width_twips = width
    geometry.reserved = reserved
    geometry.margin_left_twips = margin_left
    geometry.margin_bottom_twips = margin_bottom
    geometry.display_unit = display_unit
    geometry.margin_top_twips = margin_top
    geometry.margin_right_twips = margin_right
    geometry.flags = flags

    values_are_bounded = all(-(2**31) <= value <= 2**31 - 1 for value in parsed)
    dimensions_are_bounded = (
        _MIN_PAGE_TWIPS <= width <= _MAX_PAGE_TWIPS
        and _MIN_PAGE_TWIPS <= height <= _MAX_PAGE_TWIPS
    )
    margins_are_bounded = (
        0 <= margin_left < width
        and 0 <= margin_right < width
        and 0 <= margin_top < height
        and 0 <= margin_bottom < height
        and width - margin_left - margin_right >= _MIN_CONTENT_TWIPS
        and height - margin_top - margin_bottom >= _MIN_CONTENT_TWIPS
    )
    if not (values_are_bounded and dimensions_are_bounded and margins_are_bounded):
        geometry.reason = "page dimensions or margins are outside safe supported bounds"
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "invalid-page-geometry",
                f"[{marker_name}] {geometry.reason}",
                source,
                "\n".join(raw_fields),
            )
        )
        return geometry

    geometry.page_rect = TwipRect(0, 0, width, height, True)
    geometry.content_rect = TwipRect(
        margin_left,
        margin_top,
        width - margin_right,
        height - margin_bottom,
        True,
    )
    geometry.valid = (
        not duplicate
        and geometry.page_rect.is_usable
        and geometry.content_rect.is_usable
    )
    if not geometry.valid:
        geometry.reason = (
            "duplicate page variants make source geometry ambiguous"
            if duplicate
            else "derived page rectangle failed bounded validation"
        )
        return geometry
    if display_unit not in {1, 2, 3, 4}:
        document.diagnostics.append(
            Diagnostic(
                Severity.INFO,
                "unknown-page-display-unit",
                f"[{marker_name}] display-unit code {display_unit} remains uninterpreted",
                source,
            )
        )
    return geometry


def _parse_style(
    document: Document, section: SectionRecord, decoded: DecodedSource
) -> StyleDefinition | None:
    lines = section.raw_lines
    if not lines:
        return None
    name = _unescape_literal(lines[0].strip())
    if not name:
        return None
    style = StyleDefinition(name=name, raw="\n".join(lines), source=section.source)
    subsections: dict[str, list[str]] = {}
    duplicate_subrecords: set[str] = set()
    current: str | None = None
    current_indent = 0
    top_level: list[tuple[str, str]] = []
    for line in lines[1:]:
        match = _SUBSECTION.match(line)
        if match:
            current = match.group(1).lower()
            current_indent = len(line) - len(line.lstrip())
            if current in subsections:
                duplicate_subrecords.add(current)
            subsections[current] = []
        elif current is not None and len(line) - len(line.lstrip()) > current_indent:
            subsections[current].append(line.strip())
        else:
            current = None
            top_level.append((line.strip(), line))

    unsupported_subrecords = sorted(set(subsections) - {"fnt", "algn", "spc"})
    if unsupported_subrecords:
        document.unknown_records.append(
            UnknownRecord(
                section=section.name,
                record_type="style-subrecord",
                raw=" ".join(f"[{name}]" for name in unsupported_subrecords),
                source=section.source,
                reason="style subrecord semantics are preserved raw but not interpreted",
            )
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "style-subrecords-opaque",
                "preserved unsupported style subrecord(s): "
                + ", ".join(f"[{name}]" for name in unsupported_subrecords),
                section.source,
            )
        )

    malformed_subrecords: set[str] = set(duplicate_subrecords)
    font = subsections.get("fnt", [])
    if "fnt" in subsections and (
        len(font) < 4
        or _bounded_inline_int(font[1]) is None
        or _bounded_inline_int(font[2]) is None
        or _bounded_inline_int(font[3]) is None
    ):
        malformed_subrecords.add("fnt")
    alignment = subsections.get("algn", [])
    if "algn" in subsections and (
        len(alignment) < 5
        or any(_bounded_inline_int(value) is None for value in alignment[:5])
    ):
        malformed_subrecords.add("algn")
    spacing = subsections.get("spc", [])
    if "spc" in subsections and (
        len(spacing) < 5
        or any(_bounded_inline_int(value) is None for value in spacing[:5])
    ):
        malformed_subrecords.add("spc")
    if malformed_subrecords:
        document.unknown_records.append(
            UnknownRecord(
                section=section.name,
                record_type="malformed-style-subrecord",
                raw=" ".join(f"[{name}]" for name in sorted(malformed_subrecords)),
                source=section.source,
                reason="style subrecord was duplicate, incomplete, or malformed",
            )
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "style-subrecords-malformed",
                "preserved duplicate or malformed style subrecord(s): "
                + ", ".join(f"[{name}]" for name in sorted(malformed_subrecords)),
                section.source,
            )
        )

    # KOffice's public importer notes identify the final two common [spc]
    # values as a structural sentinel and the default text-tightness value.
    # Only that exact neutral tail is accepted; other values remain semantic
    # losses because renderers do not implement spacing control/tightness.
    supported_spacing_count = (
        7
        if len(spacing) >= 7 and spacing[5] == "1" and spacing[6] == "100"
        else 5
    )
    interpreted_field_counts = {
        "fnt": 4,
        "algn": 5,
        "spc": supported_spacing_count,
    }
    opaque_field_tails = {
        subrecord: fields[interpreted_field_counts[subrecord] :]
        for subrecord, fields in subsections.items()
        if subrecord in interpreted_field_counts
        and any(fields[interpreted_field_counts[subrecord] :])
    }
    if opaque_field_tails:
        document.unknown_records.append(
            UnknownRecord(
                section=section.name,
                record_type="style-subrecord-tail",
                raw="\n".join(
                    f"[{subrecord}]\n" + "\n".join(fields)
                    for subrecord, fields in sorted(opaque_field_tails.items())
                ),
                source=section.source,
                reason=(
                    "fields after supported style-subrecord prefixes were preserved "
                    "without interpretation"
                ),
            )
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "style-subrecord-fields-opaque",
                "preserved trailing fields without semantic interpretation in: "
                + ", ".join(f"[{name}]" for name in sorted(opaque_field_tails)),
                section.source,
            )
        )

    supported_flag_fields = {
        "fnt": (font, 3, _STYLE_CHARACTER_KNOWN_FLAGS),
        "algn": (alignment, 0, _STYLE_ALIGNMENT_KNOWN_FLAGS),
        "spc": (spacing, 0, _STYLE_SPACING_KNOWN_FLAGS),
    }
    unknown_flag_fields: list[tuple[str, str, int]] = []
    for subrecord, (fields, field_index, known_mask) in supported_flag_fields.items():
        if subrecord in malformed_subrecords or len(fields) <= field_index:
            continue
        parsed_flag = _bounded_inline_int(fields[field_index])
        if parsed_flag is None:
            continue
        unknown_bits = parsed_flag & ~known_mask
        if unknown_bits:
            unknown_flag_fields.append(
                (subrecord, fields[field_index], unknown_bits)
            )
    if unknown_flag_fields:
        raw = "\n".join(
            f"[{subrecord}]\n{value}"
            for subrecord, value, _unknown_bits in unknown_flag_fields
        )
        affected = ", ".join(f"[{name}]" for name, _value, _bits in unknown_flag_fields)
        details = ", ".join(
            f"[{subrecord}]=0x{unknown_bits:x}"
            for subrecord, _value, unknown_bits in unknown_flag_fields
        )
        description = (
            f"style {name!r} has unsupported flag bits in {affected}; "
            "supported formatting bits were retained and raw data remains in JSON"
        )
        document.unknown_records.append(
            UnknownRecord(
                section=section.name,
                record_type="style-subrecord-unknown-flags",
                raw=raw,
                source=section.source,
                reason=(
                    "unsupported style flag bits were preserved while the verified "
                    "flag subset remained typed"
                ),
            )
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "style-subrecord-unknown-flags",
                f"{description}: {details}",
                section.source,
                raw,
                lossiness=Lossiness.SEMANTIC,
            )
        )

    if len(font) >= 4:
        family = _unescape_literal(font[0]) or None
        size_value = _bounded_inline_int(font[1])
        size = size_value / 20.0 if size_value is not None else None
        packed = _bounded_inline_int(font[2]) or 0
        flags = _bounded_inline_int(font[3]) or 0
        style.character = CharacterStyle(
            font_family=family,
            font_size_pt=size,
            color=f"#{packed & 255:02x}{(packed >> 8) & 255:02x}{(packed >> 16) & 255:02x}",
            bold=bool(flags & 1),
            italic=bool(flags & 2),
            underline=bool(flags & (4 | 8 | 64)),
            strike=bool(flags & 128),
            superscript=bool(flags & 256),
            subscript=bool(flags & 512),
        )

    if alignment:
        align_flag = _bounded_inline_int(alignment[0]) or 0
        style.alignment = (
            "justify"
            if align_flag & 8
            else "center"
            if align_flag & 4
            else "right"
            if align_flag & 2
            else "left"
        )
        if len(alignment) >= 5:
            all_indent = _bounded_inline_int(alignment[2])
            first_position = _bounded_inline_int(alignment[3])
            rest_position = _bounded_inline_int(alignment[4])
            if first_position is not None and rest_position is not None:
                style.left_indent_in = rest_position / 1440.0
                style.first_line_indent_in = (
                    first_position - rest_position
                ) / 1440.0
            if all_indent not in {None, 0}:
                raw = "[algn]\n" + "\n".join(alignment[:5])
                document.unknown_records.append(
                    UnknownRecord(
                        section=section.name,
                        record_type="style-alignment-all-indent",
                        raw=raw,
                        source=section.source,
                        reason=(
                            "the documented all-indent field was preserved but its "
                            "both-side layout semantics were not applied"
                        ),
                    )
                )
                document.diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        "style-alignment-all-indent-unapplied",
                        f"style {name!r} has unapplied both-side indentation",
                        section.source,
                        raw,
                        lossiness=Lossiness.SEMANTIC,
                    )
                )

    if len(spacing) >= 5:
        flag = _bounded_inline_int(spacing[0]) or 0
        style.line_spacing = 1.0 if flag & 1 else 1.5 if flag & 2 else 2.0 if flag & 4 else None
        if flag & 8:
            point_twips = _bounded_inline_int(spacing[1])
            points = point_twips / 20.0 if point_twips is not None else None
            style.line_spacing = points / 12.0 if points else None
        before = _bounded_inline_int(spacing[3])
        after = _bounded_inline_int(spacing[4])
        style.space_before_pt = before / 20.0 if before is not None else None
        style.space_after_pt = after / 20.0 if after is not None else None
    if top_level:
        shortcut = _bounded_inline_int(top_level[0][0]) if len(top_level) == 4 else None
        following = (
            _bounded_ordinary_name(top_level[1][0]) if len(top_level) == 4 else None
        )
        canonical_envelope = (
            shortcut is not None
            and shortcut >= 0
            and following is not None
            and top_level[2][0] == "0"
            and top_level[3][0] == "0"
        )
        if canonical_envelope:
            style.shortcut_key = shortcut
            style.following_style = following
        else:
            _record_opaque_fields(
                document,
                section,
                [raw for _value, raw in top_level],
                record_type="style-top-level-fields",
                diagnostic_code="style-top-level-fields-opaque",
                object_kind="style fields",
                description=(
                    f"uninterpreted top-level fields in style {name!r} were preserved; "
                    "raw data remains in JSON"
                ),
                body_placeholder=False,
            )
    return style


def _direct_subrecords_outside_content(
    section: SectionRecord,
    *,
    text_marker_indents: set[int],
    suppress_table_data: bool = False,
) -> list[tuple[int, str]]:
    """Find exact depth-one subrecords without treating readable content as syntax."""

    direct: list[tuple[int, str]] = []
    in_text_stream = False
    text_scanner = MultilineContainerScanner()
    text_container_depth = 0
    in_table_data = False

    for index, line in enumerate(section.raw_lines):
        if in_text_stream:
            scan = text_scanner.scan_line(line)
            if scan.standalone_terminator:
                if text_container_depth:
                    text_container_depth -= 1
                else:
                    in_text_stream = False
            else:
                text_container_depth += int(scan.opener is not None)
            continue

        marker = _LAYOUT_TABBED_MARKER.fullmatch(line)
        if in_table_data:
            if marker is not None and marker.group("name").lower() in {"e", "tble"}:
                in_table_data = False
                if len(marker.group("indent")) == 1:
                    direct.append((index, marker.group("name").lower()))
            continue
        if marker is None:
            continue

        indent = len(marker.group("indent"))
        name = marker.group("name").lower()
        if name == "txt" and indent in text_marker_indents:
            if indent == 1:
                direct.append((index, name))
            in_text_stream = True
            text_scanner = MultilineContainerScanner()
            text_container_depth = 0
            continue
        if suppress_table_data and name == "data" and indent == 1:
            direct.append((index, name))
            in_table_data = True
            continue
        if indent == 1:
            direct.append((index, name))
    return direct


def _classify_opaque_subrecords(
    document: Document,
    section: SectionRecord,
    direct_subrecords: list[tuple[int, str]],
    *,
    known: set[str],
    scope: str,
    budget: _RecordBudget,
    known_positions: set[int] | None = None,
) -> list[Block]:
    """Preserve unsupported nested records with bounded, visible placeholders."""

    unsupported = [
        (position, index, name)
        for position, (index, name) in enumerate(direct_subrecords)
        if name not in known and (known_positions is None or index not in known_positions)
    ]
    budget.charge(len(unsupported) * 2, f"unsupported [{scope}] subrecord parsing")
    blocks: list[Block] = []
    for position, start, name in unsupported:
        end = (
            direct_subrecords[position + 1][0]
            if position + 1 < len(direct_subrecords)
            else len(section.raw_lines)
        )
        raw = "\n".join(section.raw_lines[start:end])
        source = section.raw_spans[start]
        document.unknown_records.append(
            UnknownRecord(
                section=f"{scope}/{name}",
                record_type=f"unsupported-{scope}-subrecord",
                raw=raw,
                source=source,
                reason=(
                    f"[{name}] {scope} subrecord was preserved raw but its semantics "
                    "are not interpreted"
                ),
            )
        )
        blocks.append(
            UnsupportedObject(
                f"unsupported {scope} subrecord",
                f"[{name}] was preserved without semantic interpretation",
                source,
            )
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                f"{scope}-subrecord-opaque",
                f"unsupported [{name}] subrecord was preserved without semantic "
                "interpretation",
                source,
            )
        )
    return blocks


def _parse_frame_name(
    section: SectionRecord,
    direct_subrecords: list[tuple[int, str]],
) -> tuple[str | None, set[int]]:
    """Type one exact bounded [frmname] record, leaving all other forms opaque."""

    markers = [
        (position, index)
        for position, (index, name) in enumerate(direct_subrecords)
        if name == "frmname"
    ]
    if len(markers) != 1:
        return None, set()
    position, start = markers[0]
    end = (
        direct_subrecords[position + 1][0]
        if position + 1 < len(direct_subrecords)
        else len(section.raw_lines)
    )
    payload = section.raw_lines[start + 1 : end]
    if len(payload) != 1:
        return None, set()
    line = payload[0]
    if len(line) - len(line.lstrip()) <= 1:
        return None, set()
    name = _bounded_ordinary_name(line)
    return (name, {start}) if name is not None else (None, set())


def _parse_structures(
    document: Document,
    sections: list[SectionRecord],
    data: bytes,
    decoded: DecodedSource,
    limits: ParseLimits,
    *,
    record_budget: _RecordBudget,
    data_base_offset: int = 0,
) -> _StructureResult:
    result = _StructureResult()
    table_cells = 0
    layout_index = 0
    nested_record_budget = record_budget
    wmf_pixel_budget = _RecordBudget(
        min(
            ParseLimits().max_total_wmf_pixels,
            max(0, limits.max_total_wmf_pixels),
        )
    )
    sdw_pixel_budget = _RecordBudget(
        _effective_lowerable_limit(
            limits.max_total_sdw_pixels,
            ParseLimits().max_total_sdw_pixels,
            "document-wide SDW pixel limit",
        )
    )
    assets: dict[str, list[Block]] = {}
    for section in sections:
        if section.name.lower() == "embedded":
            assets.update(
                _parse_embedded_manifest(
                    document,
                    section,
                    data,
                    decoded,
                    limits,
                    data_base_offset=data_base_offset,
                    wmf_pixel_budget=wmf_pixel_budget,
                    sdw_pixel_budget=sdw_pixel_budget,
                    fallback_blocks=result.supplemental_blocks,
                )
            )

    for section in sections:
        name = section.name.lower()
        if name in _DANGEROUS_SECTIONS:
            result.supplemental_blocks.append(
                UnsupportedObject("macro", "macro content was not executed", section.source)
            )
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "active-content-disabled",
                    f"[{section.name}] content was preserved as an inert diagnostic",
                    section.source,
                )
            )
        elif name == "lay":
            document.page_layouts.append(
                _parse_page_layout(document, section, layout_index)
            )
            result.supplemental_blocks.extend(
                _parse_layout_headers_footers(
                    document, section, layout_index, limits, nested_record_budget
                )
            )
            layout_index += 1
        elif name == "pg":
            document.page_hints.append(
                OpaquePageHints(
                    raw="\n".join(section.raw_lines), source=section.source
                )
            )
            if section.raw_lines:
                document.diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        "opaque-page-hints",
                        "version-dependent [pg] page hints were preserved but not interpreted",
                        section.source,
                    )
                )
        elif name == "frm":
            direct_subrecords = _direct_subrecords_outside_content(
                section,
                text_marker_indents={1},
                suppress_table_data=True,
            )
            frame_name, typed_name_positions = _parse_frame_name(
                section, direct_subrecords
            )
            frame_blocks = _classify_opaque_subrecords(
                document,
                section,
                direct_subrecords,
                known=_KNOWN_FRAME_SUBRECORDS,
                scope="frame",
                budget=nested_record_budget,
                known_positions=typed_name_positions,
            )
            has_table_marker = any(
                line.strip().lower() == "[tbl]" for line in section.raw_lines
            )
            table = _parse_table(
                document,
                section,
                limits,
                record_budget=nested_record_budget,
                fallback_blocks=frame_blocks,
            )
            if table is not None:
                count = sum(len(row.cells) for row in table.rows)
                table_cells += count
                if table_cells > limits.max_table_cells:
                    raise ResourceLimitError(
                        f"document exceeds {limits.max_table_cells} table cells"
                    )
                frame_blocks.append(table)
            frame_paragraphs = _parse_frame_text(
                document,
                section,
                record_budget=nested_record_budget,
            )
            frame_blocks.extend(frame_paragraphs)
            is_image = False
            if table is None and has_table_marker:
                frame_blocks.append(
                    UnsupportedObject(
                        "table frame",
                        "table metadata was found, but no cell text could be recovered",
                        section.source,
                    )
                )
                document.diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        "table-frame-content-unavailable",
                        "table frame metadata was found, but no cell text could be recovered",
                        section.source,
                    )
                )
            elif table is None and not frame_paragraphs:
                is_image = any(
                    line.strip().lower() in {"[isd]", "[btmap]"}
                    for line in section.raw_lines
                )
                if is_image:
                    asset_id = _frame_asset_id(section)
                    if asset_id is not None and asset_id in assets:
                        frame_blocks.extend(assets.pop(asset_id))
                    else:
                        frame_blocks.append(
                            UnsupportedObject(
                                "frame image",
                                "image frame metadata was found, but no usable indexed "
                                "asset was available",
                                section.source,
                            )
                        )
                        document.diagnostics.append(
                            Diagnostic(
                                Severity.WARNING,
                                "frame-image-unavailable",
                                "image frame metadata had no usable indexed asset",
                                section.source,
                            )
                        )
                else:
                    frame_blocks.append(
                        UnsupportedObject(
                            "drawing frame",
                            "non-text frame metadata was found; the object was not activated",
                            section.source,
                        )
                    )
                    document.diagnostics.append(
                        Diagnostic(
                            Severity.WARNING,
                            "drawing-frame-unsupported",
                            "non-text frame semantics are not interpreted",
                            section.source,
                        )
                    )
            frame_kind = "table" if has_table_marker else "frame"
            content_kind = (
                "table"
                if has_table_marker
                else "text"
                if frame_paragraphs
                else "image"
                if is_image
                else "drawing"
            )
            frame = _build_frame(
                document,
                section,
                frame_blocks,
                content_kind=content_kind,
                name=frame_name,
            )
            if frame.placement == "anchored":
                frame.anchor_index = len(result.anchored_frames)
                result.anchored_frames.append(
                    _FrameContent(frame_kind, frame, section.source)
                )
            else:
                result.supplemental_blocks.append(
                    UnsupportedObject(
                        "unanchored frame",
                        "unsupported or invalid visual placement; recovered frame follows",
                        section.source,
                    )
                )
                result.supplemental_blocks.append(frame)
                document.diagnostics.append(
                    Diagnostic(
                        Severity.INFO,
                        "unanchored-frame-reflowed",
                        "unanchored frame was retained after the main body",
                        section.source,
                    )
                )

    for asset_id, blocks in assets.items():
        result.supplemental_blocks.append(
            UnsupportedObject(
                "unreferenced embedded asset",
                f"indexed asset {asset_id} was not associated with a frame; "
                "preserved content follows",
            )
        )
        result.supplemental_blocks.extend(blocks)
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "unreferenced-embedded-asset",
                f"indexed asset {asset_id} was retained without a frame association",
            )
        )
    return result


def _parse_layout_headers_footers(
    document: Document,
    section: SectionRecord,
    layout_index: int,
    limits: ParseLimits,
    record_budget: _RecordBudget,
) -> list[Block]:
    """Recover frame-shaped header/footer streams nested in a ``[lay]`` record."""

    opaque_subrecord_blocks = _classify_opaque_subrecords(
        document,
        section,
        _direct_subrecords_outside_content(
            section,
            text_marker_indents={1, 2},
        ),
        known=_KNOWN_LAYOUT_SUBRECORDS,
        scope="layout",
        budget=record_budget,
    )

    branch_types = {
        "hrght": (Header, "odd"),
        "hlft": (Header, "even"),
        "frght": (Footer, "odd"),
        "flft": (Footer, "even"),
    }
    # Scan once.  Exact marker syntax is significant: bracket-looking body
    # text such as ``[[hrght]]`` is not a layout branch, and marker-looking
    # lines inside a terminated [txt] stream remain content.  This also avoids
    # the former per-branch full-section rescans.
    structural_sections: list[tuple[int, int, str]] = []
    branch_events: list[tuple[int, str, bool]] = []
    in_text_stream = False
    text_stream_indent = 0
    text_scanner = MultilineContainerScanner()
    text_container_depth = 0
    for index, line in enumerate(section.raw_lines):
        if in_text_stream:
            structural = _LAYOUT_TABBED_MARKER.fullmatch(line)
            structural_boundary = (
                structural is not None
                and len(structural.group("indent")) <= text_stream_indent
                and structural.group("name").lower() in _KNOWN_LAYOUT_SUBRECORDS
            )
            if not structural_boundary:
                scan = text_scanner.scan_line(line)
                if scan.standalone_terminator:
                    if text_container_depth:
                        text_container_depth -= 1
                    else:
                        in_text_stream = False
                else:
                    text_container_depth += int(scan.opener is not None)
                continue
            # A same/shallower exact subsection marker bounds a corrupt [txt]
            # stream even when its close is missing.  Content normally sits
            # one indentation level deeper, so marker-looking body text at
            # that level remains readable text.
            in_text_stream = False

        marker_match = _LAYOUT_TABBED_MARKER.fullmatch(line)
        if marker_match is not None:
            indent = len(marker_match.group("indent"))
            name = marker_match.group("name").lower()
            structural_sections.append((index, indent, name))
            if name in branch_types:
                branch_events.append((index, name, indent == 1))
            if name == "txt" and indent in {1, 2}:
                in_text_stream = True
                text_stream_indent = indent
                text_scanner = MultilineContainerScanner()
                text_container_depth = 0
            continue

        branch_match = _LAYOUT_BRANCH_MARKER.fullmatch(line)
        if branch_match is not None:
            name = branch_match.group("name").lower()
            branch_events.append((index, name, False))

    markers = [(index, name) for index, name, valid in branch_events if valid]
    malformed_markers = [
        (index, name) for index, name, valid in branch_events if not valid
    ]
    for index, name in malformed_markers:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "malformed-layout-branch-indentation",
                f"[{name}] outside the evidenced layout depth was visibly reflowed "
                "without page-placement semantics",
                section.raw_spans[index],
            )
        )

    sibling_continuations = {"lyfrm", "frmlay", "txt"}
    branch_positions = [index for index, _name, _valid in branch_events]
    depth_one_positions = [
        index for index, indent, _name in structural_sections if indent == 1
    ]
    text_positions = [
        index
        for index, indent, name in structural_sections
        if name == "txt" and indent in {1, 2}
    ]
    depth_one_noncontinuations = [
        index
        for index, indent, name in structural_sections
        if indent == 1 and name not in sibling_continuations
    ]

    def merged_positions(first: list[int], second: list[int]) -> list[int]:
        """Merge two source-ordered position lists without sorting or duplicates."""

        result: list[int] = []
        first_index = 0
        second_index = 0
        while first_index < len(first) or second_index < len(second):
            left = first[first_index] if first_index < len(first) else None
            right = second[second_index] if second_index < len(second) else None
            if right is None or (left is not None and left <= right):
                value = left
                first_index += 1
                if right == value:
                    second_index += 1
            else:
                value = right
                second_index += 1
            if value is not None and (not result or result[-1] != value):
                result.append(value)
        return result

    def branch_ends(
        starts: list[tuple[int, str]], boundaries: list[int]
    ) -> dict[int, int]:
        result: dict[int, int] = {}
        boundary_index = 0
        for start, _name in starts:
            while (
                boundary_index < len(boundaries)
                and boundaries[boundary_index] <= start
            ):
                boundary_index += 1
            result[start] = (
                boundaries[boundary_index]
                if boundary_index < len(boundaries)
                else len(section.raw_lines)
            )
        return result

    valid_ends = branch_ends(
        markers,
        merged_positions(branch_positions, depth_one_noncontinuations),
    )
    malformed_ends = branch_ends(
        malformed_markers,
        merged_positions(branch_positions, depth_one_positions),
    )

    # Every branch creates both a typed block and one raw-preservation record.
    # Charge them before materializing either collection.
    record_budget.charge(
        len(markers) * 2 + len(malformed_markers) * 2,
        "layout header/footer parsing",
    )
    blocks: list[Block] = []
    for start, branch_name in markers:
        # Both public shapes occur: layout records can nest [lyfrm]/[frmlay]/
        # [txt] below the H/F marker, or place those three records as siblings.
        # Only those explicitly evidenced sibling records belong to the branch.
        end = valid_ends[start]
        raw_lines = section.raw_lines[start:end]
        raw = "\n".join(raw_lines)
        source = section.raw_spans[start]
        content: list[Block] = []
        metadata_lines: list[str] = []
        terminated = True
        txt_markers = text_positions[
            bisect_right(text_positions, start) : bisect_left(text_positions, end)
        ]
        if not txt_markers:
            terminated = False
            metadata_lines = raw_lines[1:]
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "layout-header-footer-without-text",
                    f"[{branch_name}] placement metadata had no [txt] stream",
                    source,
                )
            )
        else:
            if len(txt_markers) > 1:
                document.diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        "multiple-layout-text-streams-reflowed",
                        f"[{branch_name}] has {len(txt_markers)} [txt] streams; "
                        "all were retained in source order",
                        source,
                    )
                )
            metadata_lines = section.raw_lines[start + 1 : txt_markers[0]]
            for marker_position, txt_index in enumerate(txt_markers):
                boundary = (
                    txt_markers[marker_position + 1]
                    if marker_position + 1 < len(txt_markers)
                    else end
                )
                stream_lines = [
                    (index, section.raw_lines[index].lstrip("\t"))
                    for index in range(txt_index + 1, boundary)
                ]
                terminator = _edoc_terminator(stream_lines, 0)
                selected = (
                    stream_lines[: terminator + 1]
                    if terminator is not None
                    else stream_lines
                )
                stream_section = SectionRecord(
                    name=f"lay/{branch_name}/txt",
                    source=section.raw_spans[txt_index],
                    raw_lines=[line for _, line in selected],
                    raw_spans=[section.raw_spans[index] for index, _ in selected],
                )
                _parse_text_stream(
                    document,
                    stream_section,
                    limits,
                    anchored_frames=[],
                    used_anchors=set(),
                    output_blocks=content,
                    record_budget=record_budget,
                    stream_label=f"[lay/{branch_name}/txt]",
                    diagnose_outer_termination=False,
                )
                if terminator is None:
                    terminated = False
                    document.diagnostics.append(
                        Diagnostic(
                            Severity.WARNING,
                            "unterminated-layout-header-footer",
                            f"[{branch_name}] [txt] stream reached the branch boundary",
                            source,
                        )
                    )

        container_type, placement = branch_types[branch_name]
        branch_header_fields, branch_header_truncated = _nested_record_fields(
            raw_lines, "lyfrm"
        )
        branch_layout_fields, branch_layout_truncated = _nested_record_fields(
            raw_lines, "frmlay"
        )
        branch_frame = _build_frame_from_fields(
            document,
            content,
            content_kind="text",
            region="header" if container_type is Header else "footer",
            raw_header_fields=branch_header_fields,
            frame_layout_fields=branch_layout_fields,
            header_fields_truncated=branch_header_truncated,
            frame_layout_fields_truncated=branch_layout_truncated,
            raw=raw,
            source=source,
        )
        block = container_type(
            blocks=content,
            placement=placement,  # type: ignore[arg-type]
            origin="layout",
            layout_index=layout_index,
            metadata="\n".join(metadata_lines),
            raw=raw,
            terminated=terminated,
            source=source,
            frame=branch_frame,
        )
        blocks.append(block)
        document.unknown_records.append(
            UnknownRecord(
                section=f"lay/{branch_name}",
                record_type="layout-header" if container_type is Header else "layout-footer",
                raw=raw,
                source=source,
                reason="raw page-placement/frame records retained with typed header/footer content",
            )
        )

    for start, branch_name in malformed_markers:
        end = malformed_ends[start]
        raw_lines = section.raw_lines[start:end]
        source = section.raw_spans[start]
        blocks.append(
            UnsupportedObject(
                "malformed layout header/footer",
                f"[{branch_name}] indentation prevented reliable page placement; "
                "readable content follows",
                source,
            )
        )
        for index, line in enumerate(raw_lines[1:], start + 1):
            value = line.strip()
            if (
                not value
                or value == ">"
                or re.fullmatch(r"\[(?:lyfrm|frmlay|txt)\]", value, re.IGNORECASE)
            ):
                continue
            record_budget.charge(1, "layout header/footer parsing")
            paragraph = _parse_inline_paragraph(
                document,
                [value],
                section.raw_spans[index],
                record_budget=record_budget,
                record_label="layout header/footer inline runs",
            )
            if paragraph.text or paragraph.runs:
                blocks.append(paragraph)
        document.unknown_records.append(
            UnknownRecord(
                section=f"lay/{branch_name}",
                record_type="malformed-layout-header-footer",
                raw="\n".join(raw_lines),
                source=source,
                reason="malformed layout indentation prevented typed placement",
            )
        )
    blocks.extend(opaque_subrecord_blocks)
    return blocks


def _prefix_fields(lines: list[str]) -> tuple[tuple[str, ...], bool]:
    values: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") or stripped == ">":
            break
        if stripped:
            if len(values) == _MAX_TYPED_GEOMETRY_FIELDS:
                return tuple(values), True
            values.append(stripped)
    return tuple(values), False


def _nested_record_fields(
    lines: list[str], record_name: str
) -> tuple[tuple[str, ...], bool]:
    marker = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().lower() == f"[{record_name.lower()}]"
        ),
        None,
    )
    if marker is None:
        return (), False
    return _prefix_fields(lines[marker + 1 :])


def _build_frame(
    document: Document,
    section: SectionRecord,
    blocks: list[Block],
    *,
    content_kind: str,
    name: str | None = None,
) -> Frame:
    header_fields, header_truncated = _prefix_fields(section.raw_lines)
    layout_fields, layout_truncated = _nested_record_fields(
        section.raw_lines, "frmlay"
    )
    return _build_frame_from_fields(
        document,
        blocks,
        content_kind=content_kind,
        region="body",
        raw_header_fields=header_fields,
        frame_layout_fields=layout_fields,
        header_fields_truncated=header_truncated,
        frame_layout_fields_truncated=layout_truncated,
        raw="\n".join(section.raw_lines),
        source=section.source,
        name=name,
    )


def _build_frame_from_fields(
    document: Document,
    blocks: list[Block],
    *,
    content_kind: str,
    region: str,
    raw_header_fields: tuple[str, ...],
    frame_layout_fields: tuple[str, ...],
    header_fields_truncated: bool = False,
    frame_layout_fields_truncated: bool = False,
    raw: str,
    source: SourceSpan,
    name: str | None = None,
) -> Frame:
    if header_fields_truncated or frame_layout_fields_truncated:
        labels = []
        if header_fields_truncated:
            labels.append("frame header")
        if frame_layout_fields_truncated:
            labels.append("[frmlay]")
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "frame-field-summary-truncated",
                f"typed {' and '.join(labels)} summary was capped at "
                f"{_MAX_TYPED_GEOMETRY_FIELDS} fields; the complete frame remains raw",
                source,
            )
        )
    if len(raw_header_fields) > 6 or frame_layout_fields:
        labels = []
        if len(raw_header_fields) > 6:
            labels.append("frame-header tail")
        if frame_layout_fields:
            labels.append("[frmlay]")
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "frame-layout-fields-opaque",
                f"preserved {' and '.join(labels)} fields without semantic "
                "interpretation",
                source,
            )
        )
    parsed = [_bounded_small_signed(value) for value in raw_header_fields[:6]]
    page_number = parsed[0] if parsed and parsed[0] is not None else None
    flags = parsed[1] if len(parsed) > 1 and parsed[1] is not None else None
    if flags is not None and not 0 <= flags <= 0x7FFFFFFF:
        flags = None

    bounds: TwipRect | None = None
    if len(raw_header_fields) >= 6 and len(parsed) == 6 and all(
        value is not None for value in parsed
    ):
        left, top, right, bottom = (int(value) for value in parsed[2:6])
        coordinates_bounded = all(
            _MIN_FRAME_COORD <= value <= _MAX_FRAME_COORD
            for value in (left, top, right, bottom)
        )
        span_bounded = (
            0 < right - left <= _MAX_PAGE_TWIPS
            and 0 < bottom - top <= _MAX_PAGE_TWIPS
        )
        valid = coordinates_bounded and span_bounded
        reason = "" if valid else "frame edges or extents are outside safe supported bounds"
        bounds = TwipRect(left, top, right, bottom, valid, reason)
        if not valid:
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "invalid-frame-geometry",
                    reason,
                    source,
                    "\n".join(raw_header_fields[:6]),
                )
            )
    else:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "incomplete-frame-geometry",
                "[frm]/[lyfrm] requires six bounded header fields for positioning",
                source,
                "\n".join(raw_header_fields),
            )
        )

    if flags is not None and flags & 2048 and flags & 4096:
        parsed_region = "unknown"
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "conflicting-frame-region",
                "frame flag word selects both header and footer; enclosing branch "
                "wins when available",
                source,
            )
        )
    elif flags is not None and flags & 2048:
        parsed_region = "header"
    elif flags is not None and flags & 4096:
        parsed_region = "footer"
    else:
        parsed_region = region
    if region in {"header", "footer"}:
        parsed_region = region

    placement = (
        "anchored"
        if flags is not None and flags & 524288
        else "repeating"
        if flags is not None and flags & 256
        else "fixed-page"
        if page_number is not None
        else "unknown"
    )
    unknown_flag_bits = (flags & ~_FRAME_KNOWN_FLAGS) if flags is not None else 0
    if unknown_flag_bits:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "frame-unknown-flags",
                f"frame has unsupported flag bits 0x{unknown_flag_bits:x}",
                source,
            )
        )
    return Frame(
        blocks=blocks,
        content_kind=content_kind,  # type: ignore[arg-type]
        placement=placement,
        region=parsed_region,  # type: ignore[arg-type]
        page_number=page_number,
        flags=flags,
        unknown_flag_bits=unknown_flag_bits,
        bounds=bounds,
        opaque=bool(flags & 64) if flags is not None else None,
        wrap_around=bool(flags & 128) if flags is not None else None,
        raw_header_fields=raw_header_fields,
        frame_layout_fields=frame_layout_fields,
        raw=raw,
        source=source,
        name=name,
    )


def _parse_table(
    document: Document,
    section: SectionRecord,
    limits: ParseLimits,
    *,
    record_budget: _RecordBudget,
    fallback_blocks: list[Block],
) -> Table | None:
    lines = section.raw_lines
    table_marker = next(
        (i for i, line in enumerate(lines) if line.strip().lower() == "[tbl]"), None
    )
    data_marker = next(
        (i for i, line in enumerate(lines) if line.strip().lower() == "[data]"), None
    )
    if table_marker is None or data_marker is None:
        return None
    table_definition: TableDefinition | None = None
    row_definitions: dict[int, tuple[int, int, int, tuple[int, ...]]] = {}
    column_definitions: dict[int, TableColumnDefinition] = {}
    cells: dict[tuple[int, int], TableCell] = {}
    current: tuple[int, int] | None = None
    current_format: tuple[int, ...] | None = None
    current_source: SourceSpan | None = None
    current_header = ""
    buffer: list[str] = []
    cell_closed = False
    cell_records = 0
    formula_metadata: list[tuple[str, SourceSpan]] = []
    opaque_field_fragments: list[str] = []
    opaque_field_entries = 0
    opaque_field_chars = 0
    opaque_field_truncated = False
    partial_formatting = False

    def retain_opaque_fields(label: str, raw_fields: str) -> None:
        nonlocal opaque_field_entries, opaque_field_chars, opaque_field_truncated
        value = raw_fields.strip()
        if not value:
            return
        opaque_field_entries += 1
        if (
            len(opaque_field_fragments) >= _MAX_OPAQUE_TABLE_FIELD_ENTRIES
            or opaque_field_chars >= _MAX_OPAQUE_TABLE_FIELD_CHARS
        ):
            opaque_field_truncated = True
            return
        fragment = f"{label}\n{value}"
        separator_chars = int(bool(opaque_field_fragments))
        remaining = (
            _MAX_OPAQUE_TABLE_FIELD_CHARS
            - opaque_field_chars
            - separator_chars
        )
        if remaining <= 0:
            opaque_field_truncated = True
            return
        if len(fragment) > remaining:
            fragment = fragment[:remaining]
            opaque_field_truncated = True
        opaque_field_fragments.append(fragment)
        opaque_field_chars += separator_chars + len(fragment)

    def exact_nonnegative_integers(raw: str, count: int) -> tuple[int, ...] | None:
        fields = raw.strip().split()
        if len(fields) != count:
            return None
        parsed = tuple(_bounded_inline_int(value) for value in fields)
        if any(value is None or value < 0 for value in parsed):
            return None
        return tuple(int(value) for value in parsed if value is not None)

    subsection = "tbl"
    for line in lines[table_marker + 1 : data_marker]:
        stripped = line.strip()
        if not stripped:
            continue
        marker = _SUBSECTION.match(line)
        if marker is not None:
            name = marker.group(1).lower()
            if name in {"h", "w"}:
                subsection = name
            elif name == "e":
                subsection = ""
            else:
                subsection = "unknown"
                retain_opaque_fields("[table subsection]", stripped)
            continue
        if subsection == "tbl" and table_definition is None:
            values = exact_nonnegative_integers(stripped, 9)
            valid = (
                values is not None
                and 1 <= values[0] <= 4_000
                and 1 <= values[1] <= 256
                and values[0] * values[1] <= limits.max_table_cells
                and all(value <= 32_767 for value in values[2:6])
            )
            if valid and values is not None:
                table_definition = TableDefinition(
                    declared_rows=values[0],
                    declared_columns=values[1],
                    default_row_height_twips=values[2],
                    default_row_gutter_twips=values[3],
                    default_column_width_twips=values[4],
                    default_column_gutter_twips=values[5],
                    flags=values[6],
                    reserved_fields=values[7:],
                )
                partial_formatting = bool(
                    values[2]
                    or values[3]
                    or values[6]
                    or any(values[7:])
                )
            else:
                retain_opaque_fields("[tbl]", stripped)
            continue
        if subsection == "h":
            values = exact_nonnegative_integers(stripped, 7)
            valid = (
                values is not None
                and values[0] not in row_definitions
                and values[0] < (table_definition.declared_rows if table_definition else 4_000)
                and values[1] <= 32_767
                and values[2] <= 32_767
            )
            if valid and values is not None:
                row_definitions[values[0]] = (
                    values[1],
                    values[2],
                    values[3],
                    values[4:],
                )
                partial_formatting = partial_formatting or bool(
                    values[1]
                    or values[2]
                    or values[3] & ~0x10
                    or any(values[4:])
                )
            else:
                retain_opaque_fields("[h]", stripped)
            continue
        if subsection == "w":
            values = exact_nonnegative_integers(stripped, 5)
            valid = (
                values is not None
                and values[0] not in column_definitions
                and values[0]
                < (table_definition.declared_columns if table_definition else 256)
                and values[1] <= 32_767
                and values[2] <= 32_767
            )
            if valid and values is not None:
                column_definitions[values[0]] = TableColumnDefinition(
                    index=values[0],
                    width_twips=values[1],
                    gutter_twips=values[2],
                    flags=values[3],
                    reserved_fields=values[4:],
                )
                partial_formatting = partial_formatting or bool(
                    values[3] or any(values[4:])
                )
            else:
                retain_opaque_fields("[w]", stripped)
            continue
        retain_opaque_fields("[tbl]", stripped)

    if table_definition is None and not any(
        fragment.startswith("[tbl]") for fragment in opaque_field_fragments
    ):
        retain_opaque_fields("[tbl]", "[missing table definition]")

    def formatted_cell(blocks: list[Paragraph]) -> TableCell:
        nonlocal partial_formatting
        values = current_format
        if values is None:
            return TableCell(blocks=blocks)
        flags = values[2]
        alignment_flags = flags & (8 | 16 | 32)
        alignment = {
            8: "left",
            16: "right",
            24: "center",
            32: "justify",
        }.get(alignment_flags)
        partial_formatting = partial_formatting or bool(
            flags & ~(8 | 16 | 32 | 128 | 256)
            or alignment_flags not in {0, 8, 16, 24, 32}
            or values[5]
            or values[6]
            or values[7]
            or values[8]
            or any(values[9:])
            or ((values[3] != 0 or values[4] != 0) and not flags & 0x80)
        )
        return TableCell(
            blocks=blocks,
            alignment=alignment,
            format_flags=flags,
            joined_row_count=values[3],
            joined_column_count=values[4],
            shading_index=values[5],
            border_word=values[6],
            content_flags=values[7],
            protected=bool(values[8]),
            reserved_fields=values[9:],
        )

    def flush() -> None:
        nonlocal buffer, current_format
        if current is not None:
            source = current_source or section.source
            paragraphs = _parse_plain_text_paragraphs(
                document,
                buffer,
                source,
                record_budget=record_budget,
            )
            if paragraphs:
                recovered = paragraphs
            else:
                record_budget.charge(1, "table cell text parsing")
                recovered = [Paragraph(source=source)]
            if current in cells:
                record_budget.charge(1, "duplicate table cell recovery")
                cells[current].blocks.append(
                    Paragraph(
                        runs=[
                            TextRun(
                                "[Duplicate table cell coordinate; values retained "
                                "in source order]",
                                source=source,
                            )
                        ],
                        source=source,
                    )
                )
                cells[current].blocks.extend(recovered)
                document.unknown_records.append(
                    UnknownRecord(
                        section="frm/data",
                        record_type="duplicate-table-cell-coordinate",
                        raw="\n".join([current_header, *buffer]),
                        source=source,
                        reason=(
                            "duplicate table coordinate was preserved by appending "
                            "both readable values in source order"
                        ),
                    )
                )
                document.diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        "duplicate-table-cell-coordinate",
                        f"duplicate table coordinate {current!r} was reflowed in "
                        "source order",
                        source,
                    )
                )
            else:
                cells[current] = formatted_cell(recovered)
        buffer = []
        current_format = None

    for line_index, line in enumerate(lines[data_marker + 1 :], start=data_marker + 1):
        stripped = line.strip()
        if _SUBSECTION.match(line) and stripped.lower() in {"[e]", "[tble]"}:
            break
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+", stripped)
        if line.startswith("\t\t\t") and match:
            flush()
            cell_records += 1
            if cell_records > limits.max_table_cells:
                raise ResourceLimitError(
                    f"table exceeds {limits.max_table_cells} cell records"
                )
            current = (
                _bounded_decimal(match.group(1), field="table row"),
                _bounded_decimal(match.group(2), field="table column"),
            )
            values = exact_nonnegative_integers(stripped, 12)
            within_declared_grid = table_definition is None or (
                current[0] < table_definition.declared_rows
                and current[1] < table_definition.declared_columns
            )
            if (
                values is not None
                and values[:2] == current
                and within_declared_grid
            ):
                current_format = values
            else:
                current_format = None
                retain_opaque_fields(
                    f"[data cell {current[0]} {current[1]}]",
                    stripped[match.end() :],
                )
            current_source = section.raw_spans[line_index]
            current_header = line
            cell_closed = False
        elif current is not None:
            if stripped == ">":
                cell_closed = True
                continue
            if cell_closed:
                if stripped:
                    formula_metadata.append((line, section.raw_spans[line_index]))
                continue
            buffer.append(line.lstrip("\t"))
    flush()
    if opaque_field_fragments:
        raw = "\n".join(opaque_field_fragments)
        if opaque_field_truncated:
            marker = (
                "[Opaque table-field summary truncated; complete fields remain "
                "in the raw frame record]"
            )
            raw = raw[: max(0, _MAX_OPAQUE_TABLE_FIELD_CHARS - len(marker) - 1)]
            raw = f"{raw}\n{marker}" if raw else marker
        description = (
            f"{opaque_field_entries} table-definition or cell-header field set(s) "
            "were preserved without interpretation; readable cell content follows"
        )
        record_budget.charge(2, "table opaque-field preservation")
        document.unknown_records.append(
            UnknownRecord(
                section="frm/tbl",
                record_type="table-fields",
                raw=raw,
                source=section.source,
                reason=(
                    "table definition and cell-header fields outside row/column "
                    "coordinates are preserved raw but not interpreted"
                ),
            )
        )
        fallback_blocks.append(
            UnsupportedObject("table fields", description, section.source)
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "table-fields-opaque",
                description,
                section.source,
                raw,
                lossiness=Lossiness.SEMANTIC,
            )
        )
    elif table_definition is not None and partial_formatting:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "table-formatting-partial",
                "typed table geometry and independently supported formatting were "
                "applied; reserved, border, shading, protection, or other fields "
                "remain preserved without full rendering semantics",
                section.source,
                lossiness=Lossiness.SEMANTIC,
            )
        )
    if formula_metadata:
        for raw, source in formula_metadata:
            document.unknown_records.append(
                UnknownRecord(
                    section="frm/data",
                    record_type="table-formula",
                    raw=raw,
                    source=source,
                    reason=(
                        "formula metadata was preserved but not recalculated; "
                        "the cached cell value was rendered"
                    ),
                )
            )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "table-formula-not-recalculated",
                f"preserved {len(formula_metadata)} table formula record(s); "
                "rendered cached values",
                section.source,
            )
        )
    if not cells:
        return None
    max_row = max(row for row, _ in cells)
    max_col = max(column for _, column in cells)
    row_count = max(
        max_row + 1,
        table_definition.declared_rows if table_definition is not None else 0,
    )
    column_count = max(
        max_col + 1,
        table_definition.declared_columns if table_definition is not None else 0,
    )
    if row_count * column_count > limits.max_table_cells:
        raise ResourceLimitError("sparse table dimensions exceed configured cell limit")

    covered: set[tuple[int, int]] = set()
    unresolved_merges = False
    for (row_index, column_index), cell in sorted(cells.items()):
        flags = cell.format_flags if type(cell.format_flags) is int else 0
        if not flags & 0x100:
            continue
        row_span = max(1, cell.joined_row_count or 0)
        column_span = max(1, cell.joined_column_count or 0)
        rectangle = {
            (target_row, target_column)
            for target_row in range(row_index, row_index + row_span)
            for target_column in range(column_index, column_index + column_span)
            if (target_row, target_column) != (row_index, column_index)
        }
        valid = (
            row_index + row_span <= row_count
            and column_index + column_span <= column_count
            and not rectangle.intersection(covered)
        )
        if valid:
            for target_row, target_column in rectangle:
                member = cells.get((target_row, target_column))
                member_flags = (
                    member.format_flags
                    if member is not None and type(member.format_flags) is int
                    else 0
                )
                if (
                    member is None
                    or not member_flags & 0x80
                    or member_flags & 0x100
                    or member.joined_row_count != target_row - row_index
                    or member.joined_column_count != target_column - column_index
                    or bool(member.text)
                ):
                    valid = False
                    break
        if valid:
            cell.row_span = row_span
            cell.column_span = column_span
            covered.update(rectangle)
        else:
            unresolved_merges = True
    unresolved_merges = unresolved_merges or any(
        type(cell.format_flags) is int
        and cell.format_flags & 0x80
        and not cell.format_flags & 0x100
        and coordinate not in covered
        for coordinate, cell in cells.items()
    )
    if unresolved_merges:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "table-merge-unresolved",
                "one or more connected-cell records did not form a complete, "
                "bounded rectangle and were retained as ordinary cells",
                section.source,
                lossiness=Lossiness.SEMANTIC,
            )
        )

    rows: list[TableRow] = []
    for row_index in range(row_count):
        row_definition = row_definitions.get(row_index)
        rows.append(
            TableRow(
                cells=[
                    cells.get((row_index, column), TableCell())
                    for column in range(column_count)
                    if (row_index, column) not in covered
                ],
                is_header=(
                    bool(row_definition[2] & 0x10)
                    if row_definition is not None
                    else row_index == 0 and table_definition is None
                ),
                height_twips=row_definition[0] if row_definition is not None else None,
                gutter_twips=row_definition[1] if row_definition is not None else None,
                flags=row_definition[2] if row_definition is not None else None,
                reserved_fields=(
                    row_definition[3] if row_definition is not None else ()
                ),
            )
        )
    return Table(
        rows=rows,
        source=section.source,
        definition=table_definition,
        columns=[column_definitions[index] for index in sorted(column_definitions)],
    )


def _parse_frame_text(
    document: Document,
    section: SectionRecord,
    *,
    record_budget: _RecordBudget,
) -> list[Paragraph]:
    """Recover text streams stored inside frames, headers, and footers."""

    paragraphs: list[Paragraph] = []
    lines = section.raw_lines
    index = 0
    while index < len(lines):
        if lines[index].strip().lower() != "[txt]":
            index += 1
            continue
        index += 1
        scanner = MultilineContainerScanner()
        container_depth = 0
        end: int | None = None
        for line_index in range(index, len(lines)):
            scan = scanner.scan_line(lines[line_index].lstrip("\t"))
            if scan.standalone_terminator:
                if container_depth:
                    container_depth -= 1
                else:
                    end = line_index
                    break
            else:
                container_depth += int(scan.opener is not None)
        terminated = end is not None
        if end is None:
            end = len(lines)
        stream = [lines[line_index].lstrip("\t") for line_index in range(index, end)]
        spans = [section.raw_spans[line_index] for line_index in range(index, end)]
        if not terminated:
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "unterminated-frame-text",
                    "[frm] [txt] stream reached the frame boundary without a "
                    "standalone terminator; readable content was retained",
                    section.raw_spans[index - 1],
                )
            )
        current: list[str] = []
        current_source = section.source
        for line, line_source in zip(stream, spans, strict=False):
            if not line:
                if current:
                    record_budget.charge(1, "frame text parsing")
                    paragraphs.append(
                        _parse_inline_paragraph(
                            document,
                            current,
                            current_source,
                            record_budget=record_budget,
                            record_label="frame inline runs",
                        )
                    )
                    current = []
            else:
                if not current:
                    current_source = line_source
                current.append(line)
        if current:
            record_budget.charge(1, "frame text parsing")
            paragraphs.append(
                _parse_inline_paragraph(
                    document,
                    current,
                    current_source,
                    record_budget=record_budget,
                    record_label="frame inline runs",
                )
            )
        index = end + 1 if terminated else len(lines)
    return [paragraph for paragraph in paragraphs if paragraph.text or paragraph.runs]


def _parse_embedded_manifest(
    document: Document,
    section: SectionRecord,
    data: bytes,
    decoded: DecodedSource,
    limits: ParseLimits,
    *,
    data_base_offset: int = 0,
    wmf_pixel_budget: _RecordBudget | None = None,
    sdw_pixel_budget: _RecordBudget | None = None,
    fallback_blocks: list[Block] | None = None,
) -> dict[str, list[Block]]:
    raw = "\n".join(section.raw_lines).encode(decoded.encoding, errors="surrogateescape")
    effective_total_asset_bytes = _effective_lowerable_limit(
        limits.max_total_asset_bytes,
        ParseLimits().max_total_asset_bytes,
        "embedded asset total byte limit",
    )
    effective_asset_bytes = _effective_lowerable_limit(
        limits.max_embedded_asset_bytes,
        ParseLimits().max_embedded_asset_bytes,
        "embedded asset byte limit",
    )
    effective_sdw_asset_bytes = sdw_asset_limit(limits)
    effective_manifest_records = min(
        limits.max_records,
        _effective_lowerable_limit(
            limits.max_embedded_records,
            ParseLimits().max_embedded_records,
            "embedded-directory record limit",
        ),
    )
    total = 0
    count = 0
    assets: dict[str, list[Block]] = {}
    last_nonempty_index = next(
        (
            index
            for index in range(len(section.raw_lines) - 1, -1, -1)
            if section.raw_lines[index].strip()
        ),
        None,
    )
    pointer_index = (
        last_nonempty_index
        if last_nonempty_index is not None
        and re.fullmatch(
            r"\d{1,20}", section.raw_lines[last_nonempty_index].strip()
        )
        else None
    )
    malformed_count = 0
    malformed_sample: list[str] = []
    for index, line in enumerate(section.raw_lines):
        if not line.strip() or index == pointer_index:
            continue
        if malformed_count >= effective_manifest_records:
            raise ResourceLimitError(
                "embedded directory exceeds the configured record limit"
            )
        if parse_embedded_manifest_row(line) is None:
            malformed_count += 1
            if len(malformed_sample) < 32:
                malformed_sample.append(line)
    if malformed_count:
        document.unknown_records.append(
            UnknownRecord(
                section=section.name,
                record_type="malformed-manifest-row",
                raw="\n".join(malformed_sample),
                source=section.source,
                reason=(
                    "directory row was not interpreted; the complete section remains "
                    "available in the raw section records"
                ),
            )
        )
        if fallback_blocks is not None:
            fallback_blocks.append(
                UnsupportedObject(
                    "malformed embedded directory",
                    f"{malformed_count} non-pointer row(s) were preserved but "
                    "could not be interpreted",
                    section.source,
                )
            )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "embedded-directory-malformed",
                f"preserved {malformed_count} malformed embedded-directory row(s)",
                section.source,
            )
        )
    for match in _EMBEDDED_MANIFEST.finditer(raw):
        count += 1
        if count > effective_manifest_records:
            raise ResourceLimitError(
                f"embedded directory exceeds {effective_manifest_records} records"
            )
        asset_id = match.group("id").decode("ascii")
        asset_blocks: list[Block] = []
        asset_offset = _bounded_decimal(
            match.group("asset_offset"), field="embedded asset offset"
        )
        asset_length = _bounded_decimal(
            match.group("asset_length"), field="embedded asset length"
        )
        preview_offset = _bounded_decimal(
            match.group("preview_offset"), field="embedded companion offset"
        )
        preview_length = _bounded_decimal(
            match.group("preview_length"), field="embedded companion length"
        )
        extension = match.group("ext").decode("ascii", errors="replace").lower()
        physical_asset_offset = asset_offset + data_base_offset
        physical_preview_offset = preview_offset + data_base_offset
        asset_is_valid = _valid_range(
            physical_asset_offset, asset_length, len(data)
        ) and _range_is_verified(
            physical_asset_offset, asset_length, decoded.binary_ranges
        )
        asset_byte_limit = (
            effective_sdw_asset_bytes
            if extension == ".sdw"
            else effective_asset_bytes
        )
        if asset_length > asset_byte_limit:
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "sdw-asset-too-large"
                    if extension == ".sdw"
                    else "embedded-asset-too-large",
                    f"{extension} asset of {asset_length} bytes was not loaded",
                    section.source,
                )
            )
        elif not asset_is_valid:
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "embedded-offset-invalid",
                    f"{extension} asset offset/length is outside a validated "
                    "post-EDOC payload interval",
                    section.source,
                )
            )
        accounted = min(asset_length, asset_byte_limit)
        if total > effective_total_asset_bytes - accounted:
            raise ResourceLimitError(
                f"embedded asset total exceeds {effective_total_asset_bytes} bytes"
            )
        total += accounted
        preview_is_valid = preview_length == 0 or (
            _valid_range(physical_preview_offset, preview_length, len(data))
            and _range_is_verified(
                physical_preview_offset, preview_length, decoded.binary_ranges
            )
        )
        sdw_preview_data: bytes | None = None
        if not preview_is_valid:
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "preview-offset-invalid",
                    "embedded preview offset/length is outside the input",
                    section.source,
                )
            )
        elif extension == ".sdw" and preview_length > effective_sdw_asset_bytes:
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "sdw-preview-too-large",
                    f"Ami Draw companion of {preview_length} bytes was not loaded",
                    section.source,
                )
            )
        elif extension == ".sdw" and preview_length:
            if total > effective_total_asset_bytes - preview_length:
                raise ResourceLimitError(
                    f"embedded asset total exceeds {effective_total_asset_bytes} bytes"
                )
            total += preview_length
            sdw_preview_data = data[
                physical_preview_offset : physical_preview_offset + preview_length
            ]
        elif preview_length:
            asset_blocks.append(
                UnsupportedObject(
                    kind="embedded companion data",
                    description=(
                        f"{preview_length} opaque bytes at offset {preview_offset}; "
                        "preserved in the source but not interpreted"
                    ),
                    source=section.source,
                )
            )
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "embedded-companion-unsupported",
                    "opaque embedded companion data was not interpreted",
                    section.source,
                )
            )
        if extension == ".sdw":
            sdw_data = (
                data[physical_asset_offset : physical_asset_offset + asset_length]
                if asset_is_valid and asset_length <= effective_sdw_asset_bytes
                else None
            )
            asset_blocks.insert(
                0,
                _parse_sdw_asset(
                    document,
                    asset_id=asset_id,
                    asset_offset=asset_offset,
                    asset_length=asset_length,
                    asset_data=sdw_data,
                    companion_data=sdw_preview_data,
                    limits=limits,
                    pixel_budget=sdw_pixel_budget,
                    source=section.source,
                ),
            )
        elif (
            extension == ".bmp"
            and asset_is_valid
            and asset_length <= effective_asset_bytes
            and data[physical_asset_offset : physical_asset_offset + 2] == b"BM"
        ):
            asset_blocks.insert(
                0,
                Image(
                    data=data[
                        physical_asset_offset : physical_asset_offset + asset_length
                    ],
                    media_type="image/bmp",
                    alt_text=f"Embedded bitmap {asset_id}",
                    source=section.source,
                )
            )
        elif (
            extension == ".wmf"
            and asset_is_valid
            and asset_length <= effective_asset_bytes
        ):
            asset_data = data[
                physical_asset_offset : physical_asset_offset + asset_length
            ]
            try:
                def reserve_pixels(count: int) -> None:
                    if wmf_pixel_budget is not None:
                        if count > wmf_pixel_budget.limit - wmf_pixel_budget.used:
                            raise WmfDecodeError(
                                "total-pixel-limit",
                                "decoded WMF pixels exceed the document-wide limit",
                            )
                        wmf_pixel_budget.used += count

                graphic: WmfGraphic = decode_wmf(
                    asset_data,
                    limits=limits,
                    source=section.source,
                    alt_text=f"Embedded WMF preview {asset_id}",
                    reserve_pixels=reserve_pixels,
                )
            except WmfDecodeError as exc:
                digest = hashlib.sha256(asset_data).hexdigest()
                description = (
                    f"{asset_length} bytes; SHA-256 {digest}; "
                    f"safe preview unavailable: {exc}"
                )
                asset_blocks.insert(
                    0,
                    UnsupportedObject(
                        kind="embedded wmf",
                        description=description,
                        source=section.source,
                    ),
                )
                document.diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        f"wmf-{exc.code}",
                        str(exc),
                        section.source,
                    )
                )
            else:
                asset_blocks.insert(0, graphic)
        else:
            asset_blocks.insert(
                0,
                UnsupportedObject(
                    kind=f"embedded {extension.lstrip('.') or 'object'}",
                    description=f"{asset_length} bytes at offset {asset_offset}; not activated",
                    source=section.source,
                )
            )
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "embedded-format-unsupported",
                    f"embedded {extension or 'object'} format was not interpreted",
                    section.source,
                )
            )
        if asset_id in assets:
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "duplicate-embedded-id",
                    f"embedded asset id {asset_id} appeared more than once; "
                    "all entries were preserved",
                    section.source,
                )
            )
            assets[asset_id].extend(asset_blocks)
        else:
            assets[asset_id] = asset_blocks
    if not count and section.raw_lines:
        # A numeric line still marks the start of Ami Pro's appended-object directory.
        document.diagnostics.append(
            Diagnostic(
                Severity.INFO,
                "embedded-directory",
                "appended-object directory found with no extractable manifest entries",
                section.source,
            )
        )
    return assets


def _parse_sdw_asset(
    document: Document,
    *,
    asset_id: str,
    asset_offset: int,
    asset_length: int,
    asset_data: bytes | None,
    companion_data: bytes | None,
    limits: ParseLimits,
    pixel_budget: _RecordBudget | None,
    source: SourceSpan,
) -> SdwDrawing:
    """Preserve one Ami Draw row and materialize only a validated companion preview."""

    drawing = SdwDrawing(
        asset_id=asset_id,
        declared_offset=asset_offset,
        declared_length=asset_length,
        data=asset_data,
        source_sha256=hashlib.sha256(asset_data).hexdigest() if asset_data is not None else None,
        signature_family=(
            "ascii-variant"
            if asset_data is not None
            and asset_data.startswith(b"AMI_METAFILE_FORMAT VERSION")
            else "common-sm-family"
            if asset_data is not None
            and len(asset_data) >= 4
            and asset_data[:2] == b"SM"
            and asset_data[3] == 1
            else "unrecognized"
            if asset_data is not None
            else "unavailable"
        ),
        status="unavailable" if asset_data is None else "malformed",
        reason=(
            "declared payload was outside the input or exceeded the configured byte limit"
            if asset_data is None
            else "structural validation did not complete"
        ),
        companion_data=companion_data,
        companion_sha256=(
            hashlib.sha256(companion_data).hexdigest()
            if companion_data is not None
            else None
        ),
        alt_text=f"Ami Draw object {asset_id}",
        source=source,
    )

    if asset_data is None:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "sdw-asset-unavailable",
                "Ami Draw payload could not be loaded from its declared bounded range",
                source,
            )
        )
    else:
        try:
            validation = validate_sdw(asset_data, limits=limits)
        except SdwDecodeError as exc:
            drawing.reason = str(exc)
            document.diagnostics.append(
                Diagnostic(Severity.WARNING, f"sdw-{exc.code}", str(exc), source)
            )
        else:
            drawing.signature_family = validation.signature_family
            drawing.header_field_1 = validation.header_field_1
            drawing.header_field_2 = validation.header_field_2
            drawing.direct_record_count = validation.direct_record_count
            drawing.bounds = validation.bounds
            drawing.declared_stream_length = validation.declared_stream_length
            drawing.records = validation.records
            drawing.trailing_bytes = validation.trailing_bytes
            drawing.status = "validated"
            drawing.reason = "vector operation semantics are not sufficiently documented"
            if validation.trailing_bytes:
                document.diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        "sdw-trailing-data",
                        f"preserved {validation.trailing_bytes} byte(s) after the declared "
                        "Ami Draw stream",
                        source,
                    )
                )
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "sdw-vector-unsupported",
                    "Ami Draw structure was validated and preserved, but unverified vector "
                    "operation semantics were not rendered",
                    source,
                )
            )

    if companion_data is not None:
        try:
            def reserve_pixels(count: int) -> None:
                if pixel_budget is not None:
                    if count > pixel_budget.limit - pixel_budget.used:
                        raise SdwDecodeError(
                            "total-pixel-limit",
                            "decoded SDW companion pixels exceed the document-wide limit",
                        )
                    pixel_budget.used += count

            drawing.preview = decode_sdw_preview(
                companion_data,
                limits=limits,
                reserve_pixels=reserve_pixels,
            )
        except SdwDecodeError as exc:
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    f"sdw-preview-{exc.code}",
                    str(exc),
                    source,
                )
            )
    return drawing


def _diagnose_unindexed_tail(
    document: Document, data: bytes, decoded: DecodedSource
) -> None:
    """Make an unindexed or damaged post-text payload visible to every renderer."""

    ranges = decoded.unindexed_ranges
    if not ranges:
        return
    ignored = {0, 9, 10, 13, 26, 32}
    if not any(
        value not in ignored
        for start, end in ranges
        for value in memoryview(data)[start:end]
    ):
        return
    digest_state = hashlib.sha256()
    total = 0
    for start, end in ranges:
        digest_state.update(memoryview(data)[start:end])
        total += end - start
    digest = digest_state.hexdigest()
    description = (
        f"{total} unindexed trailing bytes (SHA-256 {digest}); "
        "content was not activated"
    )
    document.blocks.append(UnsupportedObject("unindexed binary tail", description))
    document.diagnostics.append(
        Diagnostic(
            Severity.WARNING,
            "unindexed-trailing-data",
            description,
            lossiness=Lossiness.CONTENT,
        )
    )


def _frame_asset_id(section: SectionRecord) -> str | None:
    for index, line in enumerate(section.raw_lines):
        if line.strip().lower() != "[isd]" or index + 1 >= len(section.raw_lines):
            continue
        match = re.search(r"\.X(\d+)\b", section.raw_lines[index + 1], re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _parse_text_stream(
    document: Document,
    section: SectionRecord,
    limits: ParseLimits,
    *,
    anchored_frames: list[_FrameContent],
    used_anchors: set[int],
    output_blocks: list[Block] | None = None,
    record_budget: _RecordBudget | None = None,
    stream_label: str = "[edoc]",
    diagnose_outer_termination: bool = True,
) -> None:
    root_blocks = document.blocks if output_blocks is None else output_blocks
    top_lines: list[str] = []
    top_source = section.source
    stack: list[_OpenContainer] = []
    saw_outer_terminator = False
    scanner = MultilineContainerScanner()
    active_record_budget = record_budget or _RecordBudget(
        _effective_lowerable_limit(
            limits.max_records,
            ParseLimits().max_records,
            "content record limit",
        )
    )

    def count_record(count: int = 1) -> None:
        active_record_budget.charge(count, f"{stream_label} content records")

    def append_text_blocks(lines: list[str], source: SourceSpan, target: list[Block]) -> None:
        if not lines:
            return
        text = "\n".join(lines)
        state = _initial_inline_state(document)
        cursor = 0
        for match in _FRAME_ANCHOR.finditer(text):
            prefix = text[cursor : match.start()]
            if prefix:
                count_record()
                paragraph = _parse_inline_paragraph(
                    document,
                    prefix.split("\n"),
                    source,
                    state=state,
                    record_budget=active_record_budget,
                    record_label=f"{stream_label} inline runs",
                )
                if paragraph.text or paragraph.runs:
                    target.append(paragraph)
            count_record()
            resolved = _resolve_frame_anchor(
                document,
                match.group("kind"),
                _bounded_decimal(match.group("index"), field="frame anchor index"),
                source,
                anchored_frames,
                used_anchors,
            )
            if len(resolved) > 1:
                count_record(len(resolved) - 1)
            target.extend(resolved)
            cursor = match.end()
        suffix = text[cursor:]
        if suffix or cursor == 0:
            count_record()
            paragraph = _parse_inline_paragraph(
                document,
                suffix.split("\n"),
                source,
                state=state,
                record_budget=active_record_budget,
                record_label=f"{stream_label} inline runs",
            )
            if paragraph.text or paragraph.runs:
                target.append(paragraph)

    def flush_top() -> None:
        nonlocal top_lines
        append_text_blocks(top_lines, top_source, root_blocks)
        top_lines = []

    def flush_container(state: _OpenContainer) -> None:
        append_text_blocks(
            state.paragraph_lines,
            state.paragraph_source or state.source,
            state.blocks,
        )
        state.paragraph_lines = []
        state.paragraph_source = None

    def finish_container(*, terminated: bool) -> None:
        state = stack.pop()
        flush_container(state)
        block = _make_multiline_container(
            document,
            state,
            terminated=terminated,
            record_section=stream_label.strip("[]").lower(),
            stream_label=stream_label,
        )
        if stack:
            stack[-1].blocks.append(block)
        else:
            root_blocks.append(block)

    for line, line_source in zip(section.raw_lines, section.raw_spans, strict=False):
        scan = scanner.scan_line(line)
        if scan.standalone_terminator:
            if stack:
                stack[-1].raw_lines.append(line)
                finish_container(terminated=True)
            else:
                flush_top()
                saw_outer_terminator = True
                break
            continue

        if line.lstrip().startswith(">"):
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "malformed-container-terminator" if stack else "malformed-edoc-terminator",
                    "a terminator with trailing data was retained as readable text",
                    line_source,
                    raw=line,
                )
            )

        opener = scan.opener
        if opener:
            prefix = line[: opener.start()]
            if stack:
                current = stack[-1]
                # Retain the nested opener in the parent's direct raw fragment,
                # but keep the nested payload only in its owning child.  The
                # enclosing SectionRecord remains the single lossless stream.
                current.raw_lines.append(line)
                if prefix:
                    if not current.paragraph_lines:
                        current.paragraph_source = line_source
                    current.paragraph_lines.append(prefix)
                flush_container(current)
            else:
                if prefix:
                    if not top_lines:
                        top_source = line_source
                    top_lines.append(prefix)
                flush_top()

            if len(stack) >= limits.max_container_depth:
                raise ResourceLimitError(
                    f"multiline container nesting exceeds {limits.max_container_depth} levels"
                )
            # A typed container plus its raw-preservation record are both
            # charged in layout streams, which share a cross-branch budget.
            count_record(2)
            kind = opener.group("kind")
            if stack and (kind in {"H", "h"} or stack[-1].kind in {"H", "h"}):
                document.diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        "unsupported-nested-header-footer",
                        "nested header/footer records are not valid in the documented grammar; "
                        "nested content was preserved",
                        line_source,
                    )
                )
            stack.append(
                _OpenContainer(
                    kind=kind,
                    metadata=line[opener.end() :],
                    source=line_source,
                    raw_lines=[line[opener.start() :]],
                )
            )
            continue

        if stack:
            current = stack[-1]
            current.raw_lines.append(line)
            if line:
                if not current.paragraph_lines:
                    current.paragraph_source = line_source
                current.paragraph_lines.append(line)
            else:
                flush_container(current)
        elif line:
            if not top_lines:
                top_source = line_source
            top_lines.append(line)
        else:
            flush_top()

    while stack:
        finish_container(terminated=False)
    flush_top()
    if not saw_outer_terminator and diagnose_outer_termination:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "unterminated-edoc",
                f"{stream_label} text reached the end of its stream without an outer terminator",
                section.source,
            )
        )


def _make_multiline_container(
    document: Document,
    state: _OpenContainer,
    *,
    terminated: bool,
    record_section: str = "edoc",
    stream_label: str = "[edoc]",
) -> Block:
    labels = {"N": "annotation", "F": "footnote", "H": "header", "h": "footer"}
    label = labels[state.kind]
    raw = "\n".join(state.raw_lines)
    metadata = state.metadata.strip()
    document.unknown_records.append(
        UnknownRecord(
            section=record_section,
            record_type=f"multiline-{label}",
            raw=raw,
            source=state.source,
            reason=(
                "direct raw multiline fragments retained alongside the typed "
                "representation; the enclosing section retains the complete stream"
            ),
        )
    )
    if not terminated:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                f"unterminated-{label}",
                f"unterminated {label} content was recovered to the end of {stream_label}",
                state.source,
            )
        )

    if state.kind == "N":
        if not re.fullmatch(r"[+-]?\d+", metadata):
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "annotation-metadata-opaque",
                    "annotation placement/edit metadata was preserved without interpretation",
                    state.source,
                )
            )
        return Annotation(state.blocks, metadata, raw, terminated, state.source)
    if state.kind == "F":
        if state.metadata:
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "footnote-metadata-unsupported",
                    "unexpected footnote opener metadata was preserved without interpretation",
                    state.source,
                )
            )
        return Footnote(state.blocks, metadata, raw, terminated, source=state.source)

    flags = _bounded_optional_flag(metadata)
    placement = _header_footer_placement(flags)
    unknown_bits = flags & ~0x1F if flags is not None else 0
    if flags is None:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                f"{label}-placement-unsupported",
                f"{label} placement metadata was preserved without interpretation",
                state.source,
            )
        )
    elif unknown_bits:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                f"{label}-unknown-flag-bits",
                f"{label} has unsupported placement flag bits 0x{unknown_bits:x}",
                state.source,
            )
        )
    if flags is not None and (
        state.kind == "H" and flags & 1 or state.kind == "h" and flags & 2
    ):
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "header-footer-kind-flag-mismatch",
                f"{label} command conflicts with its header/footer type flag; "
                "the command kind was retained",
                state.source,
            )
        )
    if state.kind == "H":
        return Header(
            state.blocks,
            placement,
            "body",
            flags=flags,
            unknown_flag_bits=unknown_bits,
            metadata=metadata,
            raw=raw,
            terminated=terminated,
            source=state.source,
        )
    return Footer(
        state.blocks,
        placement,
        "body",
        flags=flags,
        unknown_flag_bits=unknown_bits,
        metadata=metadata,
        raw=raw,
        terminated=terminated,
        source=state.source,
    )


def _bounded_optional_flag(value: str) -> int | None:
    if not re.fullmatch(r"[+-]?\d{1,20}", value):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _header_footer_placement(flags: int | None) -> str:
    if flags is None:
        return "unknown"
    odd = bool(flags & 4)
    even = bool(flags & 8)
    if flags & 16 or odd and even:
        return "odd-even"
    if odd:
        return "odd"
    if even:
        return "even"
    return "all"


def _resolve_frame_anchor(
    document: Document,
    anchor_kind: str,
    anchor_index: int,
    source: SourceSpan,
    anchored_frames: list[_FrameContent],
    used_anchors: set[int],
) -> list[Block]:
    if anchor_index >= len(anchored_frames):
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "frame-anchor-out-of-range",
                f"body anchor {anchor_kind}{anchor_index} has no matching frame",
                source,
            )
        )
        return [
            UnsupportedObject(
                "missing frame anchor",
                f"body anchor {anchor_kind}{anchor_index} has no matching frame",
                source,
            )
        ]
    if anchor_index in used_anchors:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "duplicate-frame-anchor",
                f"body anchor {anchor_kind}{anchor_index} references an already placed frame",
                source,
            )
        )
        return [
            UnsupportedObject(
                "duplicate frame anchor",
                f"body anchor {anchor_kind}{anchor_index} was not duplicated",
                source,
            )
        ]

    frame = anchored_frames[anchor_index]
    expects_table = anchor_kind == "t"
    is_table = frame.kind == "table"
    used_anchors.add(anchor_index)
    if expects_table != is_table:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "frame-anchor-kind-mismatch",
                f"body anchor {anchor_kind}{anchor_index} targets a {frame.kind} frame",
                source,
            )
        )
        return [
            UnsupportedObject(
                "frame anchor mismatch",
                f"body anchor {anchor_kind}{anchor_index} targets a {frame.kind}; "
                "recovered frame follows",
                source,
            ),
            frame.frame,
        ]
    return [frame.frame]


def _initial_inline_state(document: Document) -> _InlineState:
    state = _InlineState()
    body_default = document.styles.get("Body Text")
    if body_default:
        state.style = copy.copy(body_default.character)
        state.alignment = body_default.alignment
        state.line_spacing = body_default.line_spacing
        state.style_name = body_default.name
    return state


def _parse_inline_paragraph(
    document: Document,
    lines: list[str],
    source: SourceSpan,
    *,
    state: _InlineState | None = None,
    record_budget: _RecordBudget | None = None,
    record_label: str = "inline runs",
) -> Paragraph:
    # SAM uses a blank physical line as the paragraph delimiter.  Nonblank
    # physical lines inside that record are storage continuations and may even
    # split a word, so introducing a newline (or a space) corrupts the text.
    text = "".join(lines)
    state = state or _initial_inline_state(document)
    paragraph = Paragraph(source=source)
    buffer: list[str] = []
    pending_style: CharacterStyle | None = None
    pending_chunks: list[str] = []
    inline_commands = 0
    inline_commands_omitted = False
    undefined_style_count = 0
    undefined_styles: set[str] = set()

    def finish_run() -> None:
        nonlocal pending_style, pending_chunks
        if pending_style is not None and pending_chunks:
            if record_budget is not None:
                record_budget.charge(1, record_label)
            paragraph.runs.append(TextRun("".join(pending_chunks), pending_style, source))
        pending_style = None
        pending_chunks = []

    def flush() -> None:
        nonlocal pending_style, pending_chunks
        if buffer:
            content = "".join(buffer)
            current_style = copy.copy(state.style)
            if pending_style is not None and pending_style == current_style:
                pending_chunks.append(content)
            else:
                finish_run()
                pending_style = current_style
                pending_chunks = [content]
            buffer.clear()

    def allow_inline_command() -> bool:
        nonlocal inline_commands, inline_commands_omitted
        inline_commands += 1
        if inline_commands <= _MAX_INLINE_COMMANDS_PER_PARAGRAPH:
            return True
        if not inline_commands_omitted:
            inline_commands_omitted = True
            buffer.append("[Additional inline commands omitted at safe parsing limit]")
        return False

    index = 0
    while index < len(text):
        if text.startswith("@@", index):
            buffer.append("@")
            index += 2
            continue
        if text.startswith("<<", index):
            buffer.append("<")
            index += 2
            continue
        if text[index] == "@":
            end = text.find("@", index + 1)
            if end >= 0:
                flush()
                name = _unescape_literal(text[index + 1 : end])
                if not allow_inline_command():
                    index = end + 1
                    continue
                style = document.styles.get(name)
                state.style_name = name
                if style:
                    state.style = copy.copy(style.character)
                    state.alignment = style.alignment
                    state.line_spacing = style.line_spacing
                else:
                    undefined_style_count += 1
                    if len(undefined_styles) < 256:
                        undefined_styles.add(name[:200])
                index = end + 1
                continue
        if text[index] == "<":
            special = _decode_special_escape(text, index)
            if special is not None:
                character, consumed = special
                buffer.append(character)
                index += consumed
                continue
            end = text.find(">", index + 1)
            if end >= 0:
                flush()
                tag = text[index + 1 : end]
                if allow_inline_command():
                    visible = _apply_inline_tag(tag, state, document, source)
                    if visible:
                        buffer.append(visible)
                index = end + 1
                continue
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "unterminated-inline-tag",
                    "unterminated '<' was retained as text",
                    source,
                )
            )
            buffer.append(text[index:])
            index = len(text)
            break
        buffer.append(text[index])
        index += 1
    flush()
    finish_run()
    paragraph.style_name = state.style_name
    paragraph.alignment = state.alignment
    paragraph.line_spacing = state.line_spacing
    paragraph.region_x_twips = state.region_x_twips
    paragraph.region_width_twips = state.region_width_twips
    paragraph.inline_indent_twips = state.inline_indent_twips
    paragraph.left_indent_in = state.left_indent_in
    paragraph.first_line_indent_in = state.first_line_indent_in
    paragraph.page_break_before = state.page_break_before
    if state.style_name:
        lowered = state.style_name.lower()
        paragraph.list_kind = (
            "bullet" if "bullet" in lowered else "number" if "number" in lowered else None
        )
    if state.unknown_tags:
        unique = sorted(set(state.unknown_tags))
        document.unknown_records.append(
            UnknownRecord(
                section="edoc",
                record_type="inline-tag",
                raw=" ".join(f"<{tag}>" for tag in unique),
                source=source,
                reason="inline command not yet interpreted; text around it was retained",
            )
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "unsupported-inline-tags",
                f"retained text around {len(unique)} unsupported inline command type(s)",
                source,
            )
        )
        state.unknown_tags.clear()
    if state.unapplied_tags:
        unique = sorted(set(state.unapplied_tags))
        document.unknown_records.append(
            UnknownRecord(
                section="edoc",
                record_type="typed-inline-command-unapplied",
                raw=" ".join(f"<{tag}>" for tag in unique),
                source=source,
                reason=(
                    "inline command was parsed atomically, but its layout semantics "
                    "were not applied"
                ),
            )
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "inline-command-semantics-unapplied",
                f"preserved {len(unique)} typed inline command form(s) without "
                "applying uncorroborated layout semantics",
                source,
                " ".join(f"<{tag}>" for tag in unique),
                lossiness=Lossiness.SEMANTIC,
            )
        )
        state.unapplied_tags.clear()
    if undefined_style_count:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "undefined-style",
                f"paragraph contains {undefined_style_count} reference(s) to "
                f"{len(undefined_styles)} retained undefined style name(s)",
                source,
                " ".join(sorted(undefined_styles)),
            )
        )
    if inline_commands_omitted:
        document.unknown_records.append(
            UnknownRecord(
                section="edoc",
                record_type="inline-command-limit",
                raw=f"more than {_MAX_INLINE_COMMANDS_PER_PARAGRAPH} inline commands",
                source=source,
                reason=(
                    "additional inline command semantics were not materialized after "
                    "the safe per-paragraph limit"
                ),
            )
        )
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "inline-command-limit",
                f"paragraph exceeded the safe {_MAX_INLINE_COMMANDS_PER_PARAGRAPH} "
                "inline-command limit; surrounding text and one visible marker "
                "were retained",
                source,
            )
        )
    if paragraph.text or paragraph.runs:
        state.page_break_before = False
    return paragraph


def _apply_inline_tag(
    tag: str, state: _InlineState, document: Document, source: SourceSpan
) -> str | None:
    toggles = {
        "+!": ("bold", True),
        "-!": ("bold", False),
        '+"': ("italic", True),
        '-"': ("italic", False),
        "+#": ("underline", True),
        "-#": ("underline", False),
        "+)": ("underline", True),
        "-)": ("underline", False),
        "+$": ("underline", True),
        "-$": ("underline", False),
        "+&": ("superscript", True),
        "-&": ("superscript", False),
        "+'": ("subscript", True),
        "-'": ("subscript", False),
        "+%": ("strike", True),
        "-%": ("strike", False),
    }
    if tag in toggles:
        attribute, value = toggles[tag]
        if attribute == "superscript" and value:
            state.style.subscript = False
        if attribute == "subscript" and value:
            state.style.superscript = False
        setattr(state.style, attribute, value)
        return None
    alignments = {"+@": "left", "+A": "right", "+B": "center", "+C": "justify"}
    if tag in alignments:
        state.alignment = alignments[tag]
        return None
    if match := _LINE_SPACING.match(tag):
        raw_value = match.group("value")
        try:
            value = float(raw_value) if len(raw_value) <= 32 else math.nan
        except (OverflowError, ValueError):
            value = math.nan
        if not math.isfinite(value) or abs(value) > 1_000_000:
            state.unknown_tags.append(tag[:200])
            return _unsupported_inline_marker(tag)
        state.line_spacing = (
            1.0
            if value == -1
            else 1.5
            if value == -2
            else 2.0
            if value == -3
            else value / 20.0
        )
        return None
    if tag.startswith(":f"):
        descriptor = tag[2:]
        if not descriptor:
            default = document.styles.get(state.style_name or "Body Text")
            state.style = copy.copy(default.character) if default else CharacterStyle()
            return None
        fields = descriptor.split(",")
        compact = len(fields) == 3 and fields[2] == ""
        if len(fields) not in {1, 2, 5} and not compact:
            state.unknown_tags.append(tag[:200])
            return _unsupported_inline_marker(tag)
        size = _bounded_inline_int(fields[0]) if fields[0] else None
        if fields[0] and size is None:
            state.unknown_tags.append(tag[:200])
            return _unsupported_inline_marker(tag)
        family: str | None = None
        if len(fields) > 1 and fields[1]:
            family = re.sub(r"^\d", "", _unescape_literal(fields[1]))
            if len(family) > 256:
                state.unknown_tags.append(tag[:200])
                return _unsupported_inline_marker(tag)
        channels: list[int] | None = None
        if len(fields) == 5:
            parsed_channels = [_bounded_inline_int(item) for item in fields[2:5]]
            if any(item is None for item in parsed_channels):
                state.unknown_tags.append(tag[:200])
                return _unsupported_inline_marker(tag)
            channels = [max(0, min(255, item)) for item in parsed_channels if item is not None]
        # Omitted font-command groups restore that property from the current
        # paragraph style.  Mutate only after the complete command has passed
        # shape and value validation so hostile prefixes cannot partially alter
        # following text.
        default = document.styles.get(state.style_name or "Body Text")
        default_character = default.character if default else CharacterStyle()
        replacement = copy.copy(state.style)
        replacement.font_size_pt = (
            size / 20.0 if size is not None else default_character.font_size_pt
        )
        replacement.font_family = (
            family if family is not None else default_character.font_family
        )
        replacement.color = (
            "#{:02x}{:02x}{:02x}".format(*channels)
            if channels is not None
            else default_character.color
        )
        state.style = replacement
        return None
    if match := _PARAGRAPH_LAYOUT.match(tag):
        raw_x = match.group("first")
        raw_width = match.group("rest")
        x = _bounded_inline_int(raw_x)
        width = _bounded_inline_int(raw_width) if raw_width is not None else None
        if x is None or width is None or x < 0 or width <= 0:
            state.unknown_tags.append(tag[:200])
            return _unsupported_inline_marker(tag)
        state.region_x_twips = x
        state.region_width_twips = width
        if not any(
            diagnostic.code == "paragraph-region-reflowed"
            for diagnostic in document.diagnostics
        ):
            document.diagnostics.append(
                Diagnostic(
                    Severity.INFO,
                    "paragraph-region-reflowed",
                    "paragraph region geometry is retained and safely reflowed "
                    "against renderer container widths",
                    source,
                    f"<{tag[:200]}>",
                    lossiness=Lossiness.SEMANTIC,
                )
            )
        return None
    if tag.startswith(":I"):
        values = tag[2:].split(",")
        parsed = [_bounded_inline_int(value) for value in values]
        valid = (
            len(values) == 4
            and all(value is not None and value >= 0 for value in parsed)
            and parsed[3] == 0
        )
        if not valid:
            state.unknown_tags.append(tag[:200])
            return _unsupported_inline_marker(tag)
        state.inline_indent_twips = tuple(parsed)  # type: ignore[arg-type]
        state.unapplied_tags.append(tag[:200])
        return None
    if tag == ":s":
        # Spell-check state is intentionally nonprinting.
        return None
    if tag == ":S-":
        # Restore the current paragraph style's default line spacing.
        default = document.styles.get(state.style_name or "Body Text")
        state.line_spacing = default.line_spacing if default else None
        return None
    if tag == ":":
        default = document.styles.get(state.style_name or "Body Text")
        state.style = copy.copy(default.character) if default else CharacterStyle()
        return None
    if tag == ":p":
        state.page_break_before = True
        return None
    if tag.startswith(":p"):
        state.page_break_before = True
        state.unknown_tags.append(tag[:200])
        return _unsupported_inline_marker(tag)
    if tag.startswith(":t"):
        state.unknown_tags.append(tag[:200])
        return _unsupported_inline_marker(tag)
    if tag.startswith(":A"):
        state.unknown_tags.append(tag[:200])
        return _unsupported_inline_marker(tag)
    if tag.startswith(":X~"):
        descriptor = tag[3:]
        if descriptor and descriptor in state.open_dynamic_fields:
            state.open_dynamic_fields.remove(descriptor)
            return None
        state.unknown_tags.append(tag[:200])
        return _unsupported_inline_marker(tag)
    if tag.startswith(":Z~"):
        state.unknown_tags.append(tag[:200])
        return _unsupported_inline_marker(tag)
    if tag.startswith(":X"):
        state.unknown_tags.append(tag[:200])
        descriptor = tag[2:]
        if descriptor:
            state.open_dynamic_fields.append(descriptor)
        field = tag.partition(";")[2].strip()
        fallback = re.search(r'\belse\s+"([^"]*)"', field, re.IGNORECASE)
        if fallback:
            return fallback.group(1)
        if field.lower().startswith("mergefield "):
            return f"[{field}]"
        return f"[Dynamic field: {field or 'unavailable'}]"
    if tag.startswith(":D"):
        state.unknown_tags.append(tag[:200])
        return "[Current date]"
    if tag.startswith(":P"):
        state.unknown_tags.append(tag[:200])
        return "[Page number]"
    if re.match(r"^:[NFHh]", tag):
        state.unknown_tags.append(tag[:200])
        return f"[Unsupported multiline record: <{tag[:200]}>]"
    if tag in {";", "["}:
        # These are normally consumed by _decode_special_escape.
        return None
    state.unknown_tags.append(tag[:200])
    return _unsupported_inline_marker(tag)


def _unsupported_inline_marker(tag: str) -> str:
    visible_tag = re.sub(r"[\x00-\x1f\x7f]", "�", tag[:120])
    return f"[Unsupported inline command: <{visible_tag}>]"


def _decode_special_escape(text: str, index: int) -> tuple[str, int] | None:
    if text.startswith("<;>", index):
        return ">", 3
    if text.startswith("<[>", index):
        return "[", 3
    if text.startswith("</R>", index):
        return "'", 4
    if index + 3 < len(text) and text[index + 1] == "/" and text[index + 3] == ">":
        return chr((ord(text[index + 2]) + 0x40) & 0xFF), 4
    if index + 3 < len(text) and text[index + 1] == "\\" and text[index + 3] == ">":
        return chr(ord(text[index + 2]) | 0x80), 4
    return None


def _parse_plain_text_paragraphs(
    document: Document,
    lines: list[str],
    source: SourceSpan,
    *,
    record_budget: _RecordBudget,
) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                record_budget.charge(1, "table cell text parsing")
                paragraphs.append(
                    _parse_inline_paragraph(
                        document,
                        current,
                        source,
                        record_budget=record_budget,
                        record_label="table cell inline runs",
                    )
                )
                current = []
        else:
            current.append(line)
    if current:
        record_budget.charge(1, "table cell text parsing")
        paragraphs.append(
            _parse_inline_paragraph(
                document,
                current,
                source,
                record_budget=record_budget,
                record_label="table cell inline runs",
            )
        )
    return paragraphs


def _record_unknown_main_sections(document: Document, sections: list[SectionRecord]) -> None:
    known = _KNOWN_HEADER_SECTIONS | _STRUCTURAL_SECTIONS | {"tag", "edoc"}
    l1_count = sum(section.name.lower() == "l1" for section in sections)
    for section in sections:
        name = section.name.lower()
        if name == "elay" and not section.raw_lines:
            continue
        if (
            name == "l1"
            and l1_count == 1
            and _canonical_l1_value(section) is not None
        ):
            continue
        if name not in known:
            document.unknown_records.append(
                UnknownRecord(
                    section=section.name,
                    record_type="section",
                    raw="\n".join(section.raw_lines),
                    source=section.source,
                    reason="section syntax is not yet interpreted",
                )
            )
            document.blocks.append(
                UnsupportedObject(
                    "unknown section",
                    f"[{section.name}] semantics are not interpreted; raw data remains in JSON",
                    section.source,
                )
            )
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "unknown-section",
                    f"preserved unknown [{section.name}] section",
                    section.source,
                )
            )


def _safe_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _bounded_decimal(value: str | bytes, *, field: str) -> int:
    """Parse an untrusted non-negative decimal without huge-integer work."""

    raw = value.decode("ascii") if isinstance(value, bytes) else value
    if len(raw) > 20:
        raise ResourceLimitError(f"{field} has more than 20 decimal digits")
    try:
        return int(raw)
    except ValueError as exc:
        raise ParseError(f"invalid {field}: {raw!r}") from exc


def _twips_to_points(value: str) -> float | None:
    number = _safe_int(value)
    return number / 20.0 if number is not None else None


def _twips_to_inches(value: str) -> float | None:
    number = _safe_int(value)
    return number / 1440.0 if number is not None else None


def _valid_range(offset: int, length: int, total: int) -> bool:
    return offset >= 0 and length >= 0 and offset <= total and length <= total - offset


def _range_is_verified(
    offset: int, length: int, ranges: tuple[tuple[int, int], ...]
) -> bool:
    if length <= 0:
        return False
    end = offset + length
    low = 0
    high = len(ranges)
    while low < high:
        middle = (low + high) // 2
        if ranges[middle][0] <= offset:
            low = middle + 1
        else:
            high = middle
    if low == 0:
        return False
    start, range_end = ranges[low - 1]
    return start <= offset and end <= range_end


def _unescape_literal(text: str) -> str:
    return (
        text.replace("@@", "@")
        .replace("<<", "<")
        .replace("<;>", ">")
        .replace("<[>", "[")
        .replace("</R>", "'")
    )
