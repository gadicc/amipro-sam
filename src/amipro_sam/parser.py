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
    Block,
    CharacterStyle,
    Diagnostic,
    Document,
    Image,
    Paragraph,
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
)

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
_MULTILINE_CONTAINER = re.compile(r"(?<!<)<:(?P<kind>[NFHh])")

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
    for index in range(start, len(lines)):
        line = lines[index][1]
        if line.startswith(">"):
            if note_depth:
                note_depth -= 1
                continue
            return index
        note_depth += _multiline_container_openers(line)
    return None


def _multiline_container_openers(line: str) -> int:
    """Count known containers whose closing ``>`` occurs on a later line."""

    # These Ami Pro records are terminated by a later standalone ``>``.  A
    # header opener can itself contain a nested formatting tag such as
    # ``<:H<*->``; that inner angle bracket does not close the container.
    return sum(1 for _ in _MULTILINE_CONTAINER.finditer(line))


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
) -> dict[str, list[Block]]:
    raw = "\n".join(section.raw_lines).encode(decoded.encoding, errors="surrogateescape")
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
        if asset_length > limits.max_embedded_asset_bytes:
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "embedded-asset-too-large",
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
        accounted = min(asset_length, limits.max_embedded_asset_bytes)
        if total > limits.max_total_asset_bytes - accounted:
            raise ResourceLimitError(
                f"embedded asset total exceeds {limits.max_total_asset_bytes} bytes"
            )
        total += accounted
        preview_is_valid = preview_length == 0 or _valid_range(
            physical_preview_offset, preview_length, len(data)
        )
        if not preview_is_valid:
            document.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    "preview-offset-invalid",
                    "embedded preview offset/length is outside the input",
                    section.source,
                )
            )
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
        if (
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
) -> None:
    paragraph_lines: list[str] = []
    paragraph_source = section.source
    record_count = 0
    container_kind: str | None = None
    container_depth = 0
    container_lines: list[str] = []
    container_raw: list[str] = []
    container_source = section.source

    def flush() -> None:
        nonlocal paragraph_lines, record_count
        if not paragraph_lines:
            return
        record_count += 1
        if record_count > limits.max_records:
            raise ResourceLimitError(f"document exceeds {limits.max_records} content records")
        text = "\n".join(paragraph_lines)
        state = _initial_inline_state(document)
        cursor = 0
        for match in _FRAME_ANCHOR.finditer(text):
            prefix = text[cursor : match.start()]
            if prefix:
                paragraph = _parse_inline_paragraph(
                    document,
                    prefix.split("\n"),
                    paragraph_source,
                    state=state,
                )
                if paragraph.text or paragraph.runs:
                    document.blocks.append(paragraph)
            document.blocks.extend(
                _resolve_frame_anchor(
                    document,
                    match.group("kind"),
                    _bounded_decimal(match.group("index"), field="frame anchor index"),
                    paragraph_source,
                    anchored_frames,
                    used_anchors,
                )
            )
            cursor = match.end()
        suffix = text[cursor:]
        if suffix or cursor == 0:
            paragraph = _parse_inline_paragraph(
                document,
                suffix.split("\n"),
                paragraph_source,
                state=state,
            )
            if paragraph.text or paragraph.runs:
                document.blocks.append(paragraph)
        paragraph_lines = []

    for line, line_source in zip(section.raw_lines, section.raw_spans, strict=False):
        if container_depth:
            container_raw.append(line)
            if line.startswith(">"):
                container_depth -= 1
                if container_depth == 0:
                    _emit_multiline_container(
                        document,
                        container_kind or "unknown",
                        container_lines,
                        container_raw,
                        container_source,
                        limits,
                    )
                    container_kind = None
                    container_lines = []
                    container_raw = []
                continue
            container_depth += _multiline_container_openers(line)
            container_lines.append(line)
            continue

        opener = _MULTILINE_CONTAINER.search(line)
        if opener:
            prefix = line[: opener.start()]
            if prefix:
                if not paragraph_lines:
                    paragraph_source = line_source
                paragraph_lines.append(prefix)
            flush()
            container_kind = opener.group("kind")
            container_depth = _multiline_container_openers(line)
            container_lines = []
            container_raw = [line[opener.start() :]]
            container_source = line_source
            continue
        if line.startswith(">"):
            flush()
            break
        if not line:
            flush()
        else:
            if not paragraph_lines:
                paragraph_source = line_source
            paragraph_lines.append(line)
    if container_depth:
        _emit_multiline_container(
            document,
            container_kind or "unknown",
            container_lines,
            container_raw,
            container_source,
            limits,
            terminated=False,
        )
    flush()


def _emit_multiline_container(
    document: Document,
    kind: str,
    lines: list[str],
    raw_lines: list[str],
    source: SourceSpan,
    limits: ParseLimits,
    *,
    terminated: bool = True,
) -> None:
    labels = {"N": "annotation", "F": "footnote", "H": "header/footer", "h": "header/footer"}
    label = labels.get(kind, "multiline record")
    document.blocks.append(
        UnsupportedObject(
            label,
            f"{label} placement metadata was flattened; recovered text follows",
            source,
        )
    )
    document.unknown_records.append(
        UnknownRecord(
            section="edoc",
            record_type=f"multiline-{label.replace('/', '-')}",
            raw="\n".join(raw_lines),
            source=source,
            reason="container metadata is not yet interpreted; readable content was reflowed",
        )
    )
    document.diagnostics.append(
        Diagnostic(
            Severity.WARNING,
            "multiline-container-reflowed" if terminated else "unterminated-multiline-container",
            f"{label} text was recovered without its original placement"
            if terminated
            else f"unterminated {label} text was recovered to the end of [edoc]",
            source,
        )
    )

    paragraph_lines: list[str] = []
    paragraphs = 0

    def flush() -> None:
        nonlocal paragraph_lines, paragraphs
        if not paragraph_lines:
            return
        paragraphs += 1
        if paragraphs > limits.max_records:
            raise ResourceLimitError(
                f"multiline container exceeds {limits.max_records} content records"
            )
        paragraph = _parse_inline_paragraph(document, paragraph_lines, source)
        if paragraph.text or paragraph.runs:
            document.blocks.append(paragraph)
        paragraph_lines = []

    for line in lines:
        if line:
            paragraph_lines.append(line)
        else:
            flush()
    flush()


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
    if tag.lower().startswith(":f"):
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
