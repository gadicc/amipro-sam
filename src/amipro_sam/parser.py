"""Tolerant, loss-preserving parser for Ami Pro 3.x SAM documents."""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .decoding import DecodedSource, decode_bytes
from .errors import ParseError, ResourceLimitError
from .limits import ParseLimits
from .model import (
    Annotation,
    Block,
    CharacterStyle,
    Diagnostic,
    Document,
    Footer,
    Footnote,
    FootnoteOptions,
    Header,
    Image,
    Paragraph,
    SdwDrawing,
    SectionRecord,
    Severity,
    SourceSpan,
    StyleDefinition,
    Table,
    TableCell,
    TableRow,
    TextRun,
    UnknownRecord,
    UnsupportedObject,
    WmfGraphic,
)
from .sdw import SdwDecodeError, decode_sdw_preview, sdw_asset_limit, validate_sdw
from .syntax import (
    MultilineContainerScanner,
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
_PARAGRAPH_LAYOUT = re.compile(r"^:#(?P<first>-?\d+)(?:,(?P<rest>-?\d+))?.*$")
_FRAME_ANCHOR = re.compile(r"(?<!<)<:(?P<kind>t|A)(?P<index>\d+)>")

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
_STRUCTURAL_SECTIONS = {
    "frm",
    "lay",
    "l1",
    "pg",
    "elay",
    "embedded",
    "newmac",
    "macro",
    "frmmac",
}
_DANGEROUS_SECTIONS = {"newmac", "macro", "frmmac"}


@dataclass(slots=True)
class _InlineState:
    style: CharacterStyle = field(default_factory=CharacterStyle)
    alignment: str | None = None
    line_spacing: float | None = None
    style_name: str | None = None
    left_indent_in: float | None = None
    first_line_indent_in: float | None = None
    page_break_before: bool = False
    unknown_tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _FrameContent:
    kind: str
    blocks: list[Block]
    source: SourceSpan


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
    try:
        size = source.stat().st_size
    except OSError:
        raise
    if size > limits.max_file_bytes:
        raise ResourceLimitError(
            f"input is {size} bytes; configured maximum is {limits.max_file_bytes}"
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
    if version_line:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "leading-preamble",
                f"ignored {decoded.line_byte_offsets[version_line]} byte(s) before [ver]",
                raw="\n".join(all_lines[:version_line]),
            )
        )
    logical_lines = _logical_lines(all_lines[version_line:], start_index=version_line)
    sections = _collect_sections(logical_lines, decoded, limits)
    document.sections = sections
    _parse_metadata_and_styles(document, sections, decoded, limits)
    if document.version is None:
        raise ParseError("malformed Ami Pro SAM document: [ver] has no version value")
    if not any(section.name.lower() == "sty" for section in sections):
        raise ParseError("malformed Ami Pro SAM document: required [sty] section is missing")
    structures = _parse_structures(
        document,
        sections,
        data,
        decoded,
        limits,
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
        )

    for index, frame in enumerate(structures.anchored_frames):
        if index in used_anchors:
            continue
        document.blocks.append(
            UnsupportedObject(
                "unplaced anchored frame",
                f"anchor target {index} was not referenced by the body; recovered content follows",
                frame.source,
            )
        )
        document.blocks.extend(frame.blocks)
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "unreferenced-anchored-frame",
                f"anchored {frame.kind} frame {index} was not referenced by [edoc]",
                frame.source,
            )
        )
    document.blocks.extend(structures.supplemental_blocks)
    _diagnose_unindexed_tail(
        document, data, data_base_offset=decoded.line_byte_offsets[version_line]
    )

    _record_unknown_main_sections(document, sections)
    if strict and any(item.severity is not Severity.INFO for item in document.diagnostics):
        first = next(item for item in document.diagnostics if item.severity is not Severity.INFO)
        raise ParseError(f"strict parsing failed: {first.code}: {first.message}")
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


def _logical_lines(lines: list[str], *, start_index: int = 0) -> list[tuple[int, str]]:
    """Exclude appended binary payloads from the line-oriented section scanner."""

    indexed = [(start_index + index, line) for index, line in enumerate(lines)]
    edoc = next(
        (index for index, (_, line) in enumerate(indexed) if line.strip().lower() == "[edoc]"),
        None,
    )
    if edoc is None:
        return indexed
    terminator = _edoc_terminator(indexed, edoc + 1)
    if terminator is None:
        terminator = next(
            (
                index
                for index in range(edoc + 1, len(indexed))
                if "\x00" in indexed[index][1]
            ),
            len(indexed),
        )
    prefix = indexed[: min(terminator + 1, len(indexed))]
    embedded = next(
        (
            index
            for index in range(len(indexed) - 1, edoc, -1)
            if indexed[index][1].strip().lower() == "[embedded]"
        ),
        None,
    )
    if embedded is not None and embedded > terminator:
        return prefix + indexed[embedded:]
    return prefix


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
    counts: dict[str, int] = {}
    for section in sections:
        name = section.name.lower()
        counts[name] = counts.get(name, 0) + 1
        values = [line.strip() for line in section.raw_lines]
        if name == "ver" and values:
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
        elif name == "sty" and values and values[0]:
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
        elif name == "charset":
            document.metadata["charset"] = " | ".join(value for value in values if value)
        elif name in {"lang", "desc", "revisions"} and values:
            document.metadata[name] = values[0]
        elif name == "fopts":
            _parse_footnote_options(document, section, values)
        elif name == "tag":
            if len(document.styles) >= limits.max_styles:
                raise ResourceLimitError(f"document exceeds {limits.max_styles} styles")
            style = _parse_style(section, decoded)
            if style:
                document.styles[style.name] = style

    if counts.get("ver", 0) != 1:
        document.diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "version-section-count",
                f"expected one [ver] section, found {counts.get('ver', 0)}",
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


def _parse_style(section: SectionRecord, decoded: DecodedSource) -> StyleDefinition | None:
    lines = section.raw_lines
    if not lines:
        return None
    name = _unescape_literal(lines[0].strip())
    if not name:
        return None
    style = StyleDefinition(name=name, raw="\n".join(lines), source=section.source)
    subsections: dict[str, list[str]] = {}
    current: str | None = None
    top_level: list[str] = []
    for line in lines[1:]:
        match = _SUBSECTION.match(line)
        if match:
            current = match.group(1).lower()
            subsections[current] = []
        elif current is None:
            top_level.append(line.strip())
        else:
            subsections[current].append(line.strip())

    font = subsections.get("fnt", [])
    if len(font) >= 4:
        family = _unescape_literal(font[0]) or None
        size = _twips_to_points(font[1])
        packed = _safe_int(font[2]) or 0
        flags = _safe_int(font[3]) or 0
        style.character = CharacterStyle(
            font_family=family,
            font_size_pt=size,
            color=f"#{packed & 255:02x}{(packed >> 8) & 255:02x}{(packed >> 16) & 255:02x}",
            bold=bool(flags & 1),
            italic=bool(flags & 2),
            underline=bool(flags & (4 | 8 | 64)),
            strike=bool(flags & 32),
        )

    alignment = subsections.get("algn", [])
    if alignment:
        align_flag = _safe_int(alignment[0]) or 0
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
            style.left_indent_in = _twips_to_inches(alignment[3])
            style.first_line_indent_in = _twips_to_inches(alignment[4])

    spacing = subsections.get("spc", [])
    if len(spacing) >= 5:
        flag = _safe_int(spacing[0]) or 0
        style.line_spacing = 1.0 if flag & 1 else 1.5 if flag & 2 else 2.0 if flag & 4 else None
        if flag & 8:
            points = _twips_to_points(spacing[1])
            style.line_spacing = points / 12.0 if points else None
        style.space_before_pt = _twips_to_points(spacing[3])
        style.space_after_pt = _twips_to_points(spacing[4])
    if top_level:
        parent_candidates = [value for value in top_level if value and not value.isdigit()]
        if parent_candidates:
            style.parent = _unescape_literal(parent_candidates[-1])
    return style


def _parse_structures(
    document: Document,
    sections: list[SectionRecord],
    data: bytes,
    decoded: DecodedSource,
    limits: ParseLimits,
    *,
    data_base_offset: int = 0,
) -> _StructureResult:
    result = _StructureResult()
    table_cells = 0
    layout_index = 0
    layout_budget = _RecordBudget(limits.max_records)
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
            result.supplemental_blocks.extend(
                _parse_layout_headers_footers(
                    document, section, layout_index, limits, layout_budget
                )
            )
            layout_index += 1
        elif name == "frm":
            frame_blocks: list[Block] = []
            has_table_marker = any(
                line.strip().lower() == "[tbl]" for line in section.raw_lines
            )
            table = _parse_table(document, section, limits)
            if table is not None:
                count = sum(len(row.cells) for row in table.rows)
                table_cells += count
                if table_cells > limits.max_table_cells:
                    raise ResourceLimitError(
                        f"document exceeds {limits.max_table_cells} table cells"
                    )
                frame_blocks.append(table)
            frame_paragraphs = _parse_frame_text(document, section)
            frame_blocks.extend(frame_paragraphs)
            if table is None and has_table_marker:
                frame_blocks.append(
                    UnsupportedObject(
                        "table frame",
                        "table metadata was found, but no cell text could be recovered",
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
                else:
                    frame_blocks.append(
                        UnsupportedObject(
                            "drawing frame",
                            "non-text frame metadata was found; the object was not activated",
                            section.source,
                        )
                    )
            frame_kind = "table" if has_table_marker else "frame"
            if _frame_flags(section) & 0x80000:
                result.anchored_frames.append(
                    _FrameContent(frame_kind, frame_blocks, section.source)
                )
            else:
                result.supplemental_blocks.append(
                    UnsupportedObject(
                        "unanchored frame",
                        "visual placement could not be reconstructed; recovered content follows",
                        section.source,
                    )
                )
                result.supplemental_blocks.extend(frame_blocks)
                document.diagnostics.append(
                    Diagnostic(
                        Severity.INFO,
                        "unanchored-frame-reflowed",
                        "unanchored frame content was placed after the main body",
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
    return result


def _parse_layout_headers_footers(
    document: Document,
    section: SectionRecord,
    layout_index: int,
    limits: ParseLimits,
    record_budget: _RecordBudget,
) -> list[Block]:
    """Recover frame-shaped header/footer streams nested in a ``[lay]`` record."""

    branch_types = {
        "hrght": (Header, "odd"),
        "hlft": (Header, "even"),
        "frght": (Footer, "odd"),
        "flft": (Footer, "even"),
    }
    depth_one_sections = [
        index
        for index, line in enumerate(section.raw_lines)
        if re.fullmatch(r"\t\[[A-Za-z][A-Za-z0-9_-]{0,63}\]\s*", line)
    ]
    markers: list[tuple[int, str]] = []
    malformed_markers: list[tuple[int, str]] = []
    for index, line in enumerate(section.raw_lines):
        name = line.strip().lower()
        if name.startswith("[") and name.endswith("]"):
            name = name[1:-1]
        indent = len(line) - len(line.lstrip("\t"))
        if name in branch_types and indent == 1:
            markers.append((index, name))
        elif name in branch_types:
            malformed_markers.append((index, name))
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "malformed-layout-branch-indentation",
                    f"[{name}] outside the evidenced layout depth was visibly reflowed "
                    "without page-placement semantics",
                    section.raw_spans[index],
                )
            )

    # Every branch creates both a typed block and one raw-preservation record.
    # Charge them before materializing either collection.
    record_budget.charge(
        len(markers) * 2 + len(malformed_markers) * 2,
        "layout header/footer parsing",
    )
    blocks: list[Block] = []
    for start, branch_name in markers:
        end = next(
            (index for index in depth_one_sections if index > start),
            len(section.raw_lines),
        )
        raw_lines = section.raw_lines[start:end]
        raw = "\n".join(raw_lines)
        source = section.raw_spans[start]
        content: list[Block] = []
        metadata_lines: list[str] = []
        terminated = True
        txt_markers = [
            index
            for index in range(start + 1, end)
            if section.raw_lines[index].strip().lower() == "[txt]"
            and len(section.raw_lines[index])
            - len(section.raw_lines[index].lstrip("\t"))
            == 2
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
        block = container_type(
            blocks=content,
            placement=placement,  # type: ignore[arg-type]
            origin="layout",
            layout_index=layout_index,
            metadata="\n".join(metadata_lines),
            raw=raw,
            terminated=terminated,
            source=source,
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
        end = next(
            (
                index
                for index in sorted(
                    depth_one_sections
                    + [marker_start for marker_start, _ in malformed_markers]
                )
                if index > start
            ),
            len(section.raw_lines),
        )
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
                document, [value], section.raw_spans[index]
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
    return blocks


def _frame_flags(section: SectionRecord) -> int:
    """Return the second numeric frame-header field (the frame flag word)."""

    values: list[int] = []
    for line in section.raw_lines:
        stripped = line.strip()
        if stripped.startswith("["):
            break
        if not stripped:
            continue
        value = _safe_int(stripped)
        if value is not None:
            values.append(value)
            if len(values) == 2:
                return values[1]
    return 0


def _parse_table(
    document: Document, section: SectionRecord, limits: ParseLimits
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
    cells: dict[tuple[int, int], TableCell] = {}
    current: tuple[int, int] | None = None
    buffer: list[str] = []
    cell_closed = False
    formula_metadata: list[tuple[str, SourceSpan]] = []

    def flush() -> None:
        nonlocal buffer
        if current is not None:
            paragraphs = _parse_plain_text_paragraphs(buffer, section.source)
            cells[current] = TableCell(blocks=paragraphs or [Paragraph()])
        buffer = []

    for line_index, line in enumerate(lines[data_marker + 1 :], start=data_marker + 1):
        stripped = line.strip()
        if _SUBSECTION.match(line) and stripped.lower() in {"[e]", "[tble]"}:
            break
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+", stripped)
        if line.startswith("\t\t\t") and match:
            flush()
            current = (
                _bounded_decimal(match.group(1), field="table row"),
                _bounded_decimal(match.group(2), field="table column"),
            )
            cell_closed = False
            if len(cells) >= limits.max_table_cells:
                raise ResourceLimitError(f"table exceeds {limits.max_table_cells} cells")
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
    if (max_row + 1) * (max_col + 1) > limits.max_table_cells:
        raise ResourceLimitError("sparse table dimensions exceed configured cell limit")
    rows: list[TableRow] = []
    for row_index in range(max_row + 1):
        rows.append(
            TableRow(
                cells=[
                    cells.get((row_index, column), TableCell())
                    for column in range(max_col + 1)
                ],
                is_header=row_index == 0,
            )
        )
    return Table(rows=rows, source=section.source)


def _parse_frame_text(document: Document, section: SectionRecord) -> list[Paragraph]:
    """Recover text streams stored inside frames, headers, and footers."""

    paragraphs: list[Paragraph] = []
    lines = section.raw_lines
    index = 0
    while index < len(lines):
        if lines[index].strip().lower() != "[txt]":
            index += 1
            continue
        index += 1
        stream: list[str] = []
        spans: list[SourceSpan] = []
        while index < len(lines) and not lines[index].startswith(">"):
            stream.append(lines[index].lstrip("\t"))
            spans.append(section.raw_spans[index])
            index += 1
        current: list[str] = []
        current_source = section.source
        for line, line_source in zip(stream, spans, strict=False):
            if not line:
                if current:
                    paragraphs.append(_parse_inline_paragraph(document, current, current_source))
                    current = []
            else:
                if not current:
                    current_source = line_source
                current.append(line)
        if current:
            paragraphs.append(_parse_inline_paragraph(document, current, current_source))
        index += 1
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
) -> dict[str, list[Block]]:
    raw = "\n".join(section.raw_lines).encode(decoded.encoding, errors="surrogateescape")
    effective_total_asset_bytes = _effective_lowerable_limit(
        limits.max_total_asset_bytes,
        ParseLimits().max_total_asset_bytes,
        "embedded asset total byte limit",
    )
    effective_sdw_asset_bytes = sdw_asset_limit(limits)
    total = 0
    count = 0
    assets: dict[str, list[Block]] = {}
    for match in _EMBEDDED_MANIFEST.finditer(raw):
        count += 1
        if count > limits.max_records:
            raise ResourceLimitError(
                f"embedded directory exceeds {limits.max_records} records"
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
        asset_is_valid = _valid_range(physical_asset_offset, asset_length, len(data))
        asset_byte_limit = (
            effective_sdw_asset_bytes
            if extension == ".sdw"
            else max(0, limits.max_embedded_asset_bytes)
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
                    f"{extension} asset offset/length is outside the input",
                    section.source,
                )
            )
        accounted = min(asset_length, asset_byte_limit)
        if total > effective_total_asset_bytes - accounted:
            raise ResourceLimitError(
                f"embedded asset total exceeds {effective_total_asset_bytes} bytes"
            )
        total += accounted
        preview_is_valid = preview_length == 0 or _valid_range(
            physical_preview_offset, preview_length, len(data)
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
            and asset_length <= limits.max_embedded_asset_bytes
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
            and asset_length <= limits.max_embedded_asset_bytes
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
    document: Document, data: bytes, *, data_base_offset: int = 0
) -> None:
    """Make an unindexed or damaged post-text payload visible to every renderer."""

    stream = data[data_base_offset:]
    try:
        decoded = stream.decode(document.encoding, errors="surrogateescape")
    except (LookupError, UnicodeError):
        return
    lines = decoded.splitlines(keepends=True)
    edoc_index = next(
        (index for index, line in enumerate(lines) if line.rstrip("\r\n").lower() == "[edoc]"),
        None,
    )
    if edoc_index is None:
        return
    indexed_lines = [
        (index, line.rstrip("\r\n")) for index, line in enumerate(lines)
    ]
    terminator_index = _edoc_terminator(indexed_lines, edoc_index + 1)
    if terminator_index is None:
        return
    tail_start = len(
        "".join(lines[: terminator_index + 1]).encode(
            document.encoding, errors="surrogateescape"
        )
    )
    tail = stream[tail_start:]
    embedded = re.search(br"(?im)^\[embedded\]\r?\n", tail)
    if embedded:
        return
    meaningful = tail.strip(b"\x00\x1a\x20\t\r\n")
    if not meaningful:
        return
    digest = hashlib.sha256(tail).hexdigest()
    description = (
        f"{len(tail)} unindexed trailing bytes (SHA-256 {digest}); "
        "content was not activated"
    )
    document.blocks.append(UnsupportedObject("unindexed binary tail", description))
    document.diagnostics.append(
        Diagnostic(
            Severity.WARNING,
            "unindexed-trailing-data",
            description,
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
    record_count = 0
    saw_outer_terminator = False
    scanner = MultilineContainerScanner()

    def count_record(count: int = 1) -> None:
        nonlocal record_count
        if record_budget is not None:
            record_budget.charge(count, f"{stream_label} parsing")
            return
        record_count += count
        if record_count > limits.max_records:
            raise ResourceLimitError(f"document exceeds {limits.max_records} content records")

    def append_text_blocks(lines: list[str], source: SourceSpan, target: list[Block]) -> None:
        if not lines:
            return
        count_record()
        text = "\n".join(lines)
        state = _initial_inline_state(document)
        cursor = 0
        for match in _FRAME_ANCHOR.finditer(text):
            prefix = text[cursor : match.start()]
            if prefix:
                paragraph = _parse_inline_paragraph(
                    document, prefix.split("\n"), source, state=state
                )
                if paragraph.text or paragraph.runs:
                    target.append(paragraph)
            target.extend(
                _resolve_frame_anchor(
                    document,
                    match.group("kind"),
                    _bounded_decimal(match.group("index"), field="frame anchor index"),
                    source,
                    anchored_frames,
                    used_anchors,
                )
            )
            cursor = match.end()
        suffix = text[cursor:]
        if suffix or cursor == 0:
            paragraph = _parse_inline_paragraph(
                document, suffix.split("\n"), source, state=state
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
            count_record(2 if record_budget is not None else 1)
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
                "recovered content follows",
                source,
            ),
            *frame.blocks,
        ]
    return list(frame.blocks)


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
) -> Paragraph:
    text = "\n".join(lines)
    state = state or _initial_inline_state(document)
    paragraph = Paragraph(source=source)
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            content = "".join(buffer)
            if paragraph.runs and paragraph.runs[-1].style == state.style:
                paragraph.runs[-1].text += content
            else:
                paragraph.runs.append(TextRun(content, copy.copy(state.style), source))
            buffer.clear()

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
                style = document.styles.get(name)
                state.style_name = name
                if style:
                    state.style = copy.copy(style.character)
                    state.alignment = style.alignment
                    state.line_spacing = style.line_spacing
                else:
                    document.diagnostics.append(
                        Diagnostic(
                            Severity.WARNING,
                            "undefined-style",
                            f"paragraph references undefined style {name!r}",
                            source,
                        )
                    )
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
        buffer.append(text[index])
        index += 1
    flush()
    paragraph.style_name = state.style_name
    paragraph.alignment = state.alignment
    paragraph.line_spacing = state.line_spacing
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
        value = float(match.group("value"))
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
        else:
            fields = descriptor.split(",")
            size = _safe_int(fields[0]) if fields else None
            if size is not None:
                state.style.font_size_pt = size / 20.0
            if len(fields) > 1 and fields[1]:
                state.style.font_family = re.sub(r"^\d", "", _unescape_literal(fields[1]))
            if len(fields) >= 5 and all(_safe_int(item) is not None for item in fields[2:5]):
                channels = [max(0, min(255, int(item))) for item in fields[2:5]]
                state.style.color = "#{:02x}{:02x}{:02x}".format(*channels)
        return None
    if match := _PARAGRAPH_LAYOUT.match(tag):
        first = _safe_int(match.group("first"))
        rest = _safe_int(match.group("rest")) if match.group("rest") else None
        if first is not None:
            state.first_line_indent_in = first / 1440.0
        if rest is not None:
            state.left_indent_in = rest / 1440.0
        return None
    if tag.startswith(":I"):
        values = tag[2:].split(",")
        if values and _safe_int(values[0]) is not None:
            state.left_indent_in = int(values[0]) / 1440.0
        if len(values) >= 3 and _safe_int(values[2]) is not None:
            state.first_line_indent_in = int(values[2]) / 1440.0
        return None
    if tag in {":s", ":S-"}:
        # Spell-check and line-spacing-reset state have no visible representation.
        return None
    if tag == ":":
        default = document.styles.get(state.style_name or "Body Text")
        state.style = copy.copy(default.character) if default else CharacterStyle()
        return None
    if tag.startswith(":p"):
        state.page_break_before = True
        return None
    if tag.startswith(":t"):
        return None
    if tag.startswith(":A"):
        state.unknown_tags.append(tag[:200])
        return None
    if tag.startswith(":X~") or tag.startswith(":Z~"):
        return None
    if tag.startswith(":X"):
        field = tag.partition(";")[2].strip()
        fallback = re.search(r'\belse\s+"([^"]*)"', field, re.IGNORECASE)
        if fallback:
            return fallback.group(1)
        if field.lower().startswith("mergefield "):
            return f"[{field}]"
        return f"[Dynamic field: {field or 'unavailable'}]"
    if tag.startswith(":D"):
        return "[Current date]"
    if tag.startswith(":P"):
        return "[Page number]"
    if re.match(r"^:[NFHh]", tag):
        state.unknown_tags.append(tag[:200])
        return f"[Unsupported multiline record: <{tag[:200]}>]"
    if tag in {";", "["}:
        # These are normally consumed by _decode_special_escape.
        return None
    state.unknown_tags.append(tag[:200])
    return None


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


def _parse_plain_text_paragraphs(lines: list[str], source: SourceSpan) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                paragraphs.append(Paragraph(runs=[TextRun("\n".join(current), source=source)]))
                current = []
        else:
            # Table frame text follows the same inline syntax, but removing commands is safer
            # than presenting control records as user content until cell anchors are modeled.
            current.append(_strip_inline_commands(line))
    if current:
        paragraphs.append(Paragraph(runs=[TextRun("\n".join(current), source=source)]))
    return paragraphs


def _strip_inline_commands(text: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("@@", index):
            result.append("@")
            index += 2
        elif text[index] == "@":
            # Named paragraph styles use ``@Style Name@``; they are controls,
            # not visible cell text.  An unmatched at sign remains literal.
            end = text.find("@", index + 1)
            if end >= 0:
                index = end + 1
            else:
                result.append("@")
                index += 1
        elif text.startswith("<<", index):
            result.append("<")
            index += 2
        elif text[index] == "<":
            special = _decode_special_escape(text, index)
            if special:
                result.append(special[0])
                index += special[1]
            else:
                end = text.find(">", index + 1)
                index = end + 1 if end >= 0 else index + 1
        else:
            result.append(text[index])
            index += 1
    return "".join(result)


def _record_unknown_main_sections(document: Document, sections: list[SectionRecord]) -> None:
    known = _KNOWN_HEADER_SECTIONS | _STRUCTURAL_SECTIONS | {"tag", "edoc"}
    for section in sections:
        name = section.name.lower()
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


def _unescape_literal(text: str) -> str:
    return (
        text.replace("@@", "@")
        .replace("<<", "<")
        .replace("<;>", ">")
        .replace("<[>", "[")
        .replace("</R>", "'")
    )
