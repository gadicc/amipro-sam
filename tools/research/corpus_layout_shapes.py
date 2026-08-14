#!/usr/bin/env python3
"""Emit privacy-preserving corpus aggregates for layout and table records.

This research entry point accepts an explicit directory of lawfully held SAM
files, reads regular non-symlink ``.sam`` files, and emits deterministic JSON
containing only counts and bounded numeric distributions.  It reuses the
converter's bounded decoder and parser for source-section framing and page
geometry, but scans the raw section records itself rather than consuming the
converter's semantic table model.  It never emits an input path, file name,
document text, style name, source metadata, raw command, or per-document row.

The report is corpus correlation, not executable-confirmed semantics.  In
particular, the font-size, text-density, table-topology, and field
cross-tabs are hypothesis discriminators; they are not a reason to change
converter behavior by themselves.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import stat
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from amipro_sam.decoding import decode_bytes  # noqa: E402
from amipro_sam.errors import AmiProError  # noqa: E402
from amipro_sam.limits import ParseLimits  # noqa: E402
from amipro_sam.model import SectionRecord  # noqa: E402
from amipro_sam.parser import parse_bytes  # noqa: E402

SCHEMA = "amipro-private-corpus-layout-shapes-v2"

# These are hard ceilings, not observations.  Lower ParseLimits are supplied to
# the project decoder/parser so a future increase in library defaults cannot
# silently widen this research tool's resource envelope.
MAX_DIRECTORY_ENTRIES = 100_000
MAX_DIRECTORY_DEPTH = 32
MAX_SELECTED_FILES = 4_096
MAX_NAME_BYTES = 255
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_LINES_PER_FILE = 1_000_000
MAX_RECORDS_PER_FILE = 1_000_000
MAX_COMMANDS = 250_000
MAX_COMMANDS_PER_STORAGE_UNIT = 4_096
MAX_BLANK_BOUNDARIES = 1_000_000
MAX_MATERIAL_SCAN_CHARACTERS = 512 * 1024 * 1024
MAX_TABLES = 16_384
MAX_TABLE_STRUCTURAL_RECORDS = 1_000_000
MAX_TABLE_RECORD_FIELDS = 32
MAX_TABLE_TOKEN_CHARACTERS = 64
MAX_ANALYZED_TABLE_ROWS = 4_000
MAX_ANALYZED_TABLE_COLUMNS = 256
MAX_ANALYZED_TABLE_CELLS = 100_000
MAX_TABLE_INDEX_WORK = 2_000_000
MAX_TABLE_GRID_COORDINATE_WORK = 2_000_000
MAX_MERGE_TOPOLOGY_COORDINATE_WORK = 2_000_000
MAX_TABLE_EXTENT_INDEX_WORK = 2_000_000
MAX_COMMAND_PAYLOAD_CHARS = 128
MAX_NUMERIC_TOKEN_DIGITS = 9
MAX_NUMERIC_VALUE = 2**31 - 1
MAX_DISTINCT_VALUES = 8_192
MAX_PUBLISHED_GROUPS = 32
MIN_PUBLISHED_GROUP_COUNT = 5

_COMMAND = re.compile(r"(?<!<)<:(?P<kind>[#I])(?P<payload>[^>\r\n]*)>")
_REGION_NUMERIC = re.compile(
    rf"(?P<first>[0-9]{{1,{MAX_NUMERIC_TOKEN_DIGITS}}}),"
    rf"(?P<width>[0-9]{{1,{MAX_NUMERIC_TOKEN_DIGITS}}})"
)
_INDENT_NUMERIC = re.compile(
    rf"(?P<a>[0-9]{{1,{MAX_NUMERIC_TOKEN_DIGITS}}}),"
    rf"(?P<b>[0-9]{{1,{MAX_NUMERIC_TOKEN_DIGITS}}}),"
    rf"(?P<c>[0-9]{{1,{MAX_NUMERIC_TOKEN_DIGITS}}}),"
    rf"(?P<d>[0-9]{{1,{MAX_NUMERIC_TOKEN_DIGITS}}})"
)
_FONT_SIZE_COMMAND = re.compile(
    rf"(?<!<)<:f(?P<size>[0-9]{{1,{MAX_NUMERIC_TOKEN_DIGITS}}})(?:,|>)"
)
_ANY_INLINE = re.compile(r"(?<!<)<[^>\r\n]{0,4096}>")
_STYLE_TOKEN = re.compile(r"(?<!@)@[^@\r\n]{1,256}@(?!@)")
_BLANK_BOUNDARY = re.compile(r"(?:\r\n|[\n\r])[ \t]*(?:\r\n|[\n\r])")
_STRUCTURAL_LINE = re.compile(r"[ \t]*(?:\[[^\]\r\n]{1,128}\]|>)[ \t]*")
_NESTED_MARKER = re.compile(r"^[ \t]+\[([A-Za-z][A-Za-z0-9_-]{0,63})\][ \t]*$")
_DATA_HEADER_START = re.compile(r"^\t{3,}[ \t]*[0-9]+[ \t]+[0-9]+(?:[ \t]|$)")
_UNSIGNED_TOKEN = re.compile(rf"[0-9]{{1,{MAX_NUMERIC_TOKEN_DIGITS}}}")
_SIGNED_TOKEN = re.compile(rf"-?[0-9]{{1,{MAX_NUMERIC_TOKEN_DIGITS}}}")
_INLINE_ALIGNMENT = re.compile(r"(?<!<)<\+(?P<code>[@ABC])>")


class CorpusAnalysisError(RuntimeError):
    """A sanitized, path-free corpus safety failure."""


@dataclass(frozen=True, slots=True)
class _SelectedInput:
    path: Path
    size: int
    device: int
    inode: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class _RegionObservation:
    first: int
    width: int
    body_width: int | None
    material_characters: int
    following_font_size: int | None


@dataclass(frozen=True, slots=True)
class _IndentObservation:
    values: tuple[int, int, int, int]
    unit_key: tuple[int, ...]
    before_material: bool
    unit_has_region: bool
    unit_has_font: bool


@dataclass(frozen=True, slots=True)
class _UnitFeatures:
    has_region: bool
    has_font: bool
    material_characters: int


@dataclass(slots=True)
class _WorkBudget:
    material_scan_characters: int = 0

    def reserve_material_scan(self, characters: int) -> None:
        if characters < 0:
            raise CorpusAnalysisError("a material-scan length was negative")
        self.material_scan_characters += characters
        if self.material_scan_characters > MAX_MATERIAL_SCAN_CHARACTERS:
            raise CorpusAnalysisError("material-density scans exceed the aggregate work cap")


@dataclass(frozen=True, slots=True)
class _TableCellObservation:
    fields: tuple[int, ...]
    has_nonwhitespace_body: bool
    has_material: bool
    inline_alignments: tuple[str, ...]
    post_close_metadata_lines: int

    @property
    def coordinate(self) -> tuple[int, int]:
        return self.fields[0], self.fields[1]


@dataclass(frozen=True, slots=True)
class _TableObservation:
    definition: tuple[int, ...] | None
    rows: tuple[tuple[int, ...], ...]
    columns: tuple[tuple[int, ...], ...]
    cells: tuple[_TableCellObservation, ...]
    has_data_marker: bool
    frame_header: tuple[int, ...] | None


@dataclass(slots=True)
class _RecordShape:
    candidate_count: int = 0
    canonical_count: int = 0
    over_field_cap_count: int = 0
    over_token_cap_count: int = 0
    nonnumeric_or_out_of_bounds_count: int = 0
    arities: Counter[int] = dataclass_field(default_factory=Counter)


@dataclass(slots=True)
class _TableScan:
    tables: list[_TableObservation] = dataclass_field(default_factory=list)
    shapes: dict[str, _RecordShape] = dataclass_field(
        default_factory=lambda: {
            "tbl": _RecordShape(),
            "h": _RecordShape(),
            "w": _RecordShape(),
            "data": _RecordShape(),
        }
    )
    table_markers: int = 0
    data_markers: int = 0
    structural_records: int = 0

    def reserve_record(self) -> None:
        self.structural_records += 1
        if self.structural_records > MAX_TABLE_STRUCTURAL_RECORDS:
            raise CorpusAnalysisError("table structural records exceed the safety cap")


def _parser_limits() -> ParseLimits:
    return ParseLimits(
        max_file_bytes=MAX_FILE_BYTES,
        max_line_bytes=4 * 1024 * 1024,
        max_lines=MAX_LINES_PER_FILE,
        max_records=MAX_RECORDS_PER_FILE,
        max_container_depth=64,
        max_styles=10_000,
        max_table_cells=100_000,
        max_embedded_asset_bytes=16 * 1024 * 1024,
        max_total_asset_bytes=64 * 1024 * 1024,
        max_wmf_records=10_000,
        max_wmf_objects=4_096,
        max_wmf_palette_entries=4_096,
        max_wmf_dimension=4_096,
        max_wmf_pixels=4_000_000,
        max_total_wmf_pixels=8_000_000,
        max_sdw_records=10_000,
        max_sdw_depth=32,
        max_sdw_points=1_000_000,
        max_sdw_dimension=4_096,
        max_sdw_pixels=4_000_000,
        max_total_sdw_pixels=8_000_000,
        max_embedded_records=4_096,
    )


def _bounded_counter_increment(counter: Counter[Any], key: Any) -> None:
    if key not in counter and len(counter) >= MAX_DISTINCT_VALUES:
        raise CorpusAnalysisError("a numeric distribution exceeded its distinct-value cap")
    counter[key] += 1


def _bounded_counter(values: Iterable[Any]) -> Counter[Any]:
    counter: Counter[Any] = Counter()
    for value in values:
        _bounded_counter_increment(counter, value)
    return counter


def _enumerate_inputs(root: Path) -> tuple[list[_SelectedInput], dict[str, int]]:
    try:
        root_info = root.lstat()
    except OSError as error:
        raise CorpusAnalysisError("cannot stat the corpus directory") from error
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise CorpusAnalysisError("the corpus input must be a non-symlink directory")

    selected: list[_SelectedInput] = []
    counts = Counter()
    total_bytes = 0
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        if depth > MAX_DIRECTORY_DEPTH:
            raise CorpusAnalysisError("the corpus directory depth exceeds the safety cap")
        try:
            iterator = os.scandir(directory)
        except OSError as error:
            raise CorpusAnalysisError("cannot enumerate a corpus directory") from error
        entries: list[os.DirEntry[str]] = []
        try:
            for entry in iterator:
                counts["directory_entries_seen"] += 1
                if counts["directory_entries_seen"] > MAX_DIRECTORY_ENTRIES:
                    raise CorpusAnalysisError(
                        "the corpus directory-entry count exceeds the safety cap"
                    )
                entries.append(entry)
        except OSError as error:
            raise CorpusAnalysisError("cannot enumerate a corpus directory") from error
        finally:
            iterator.close()
        entries.sort(key=lambda entry: os.fsencode(entry.name))
        for entry in entries:
            if len(os.fsencode(entry.name)) > MAX_NAME_BYTES:
                raise CorpusAnalysisError("a corpus entry name exceeds the safety cap")
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise CorpusAnalysisError("cannot stat a corpus entry") from error
            if stat.S_ISLNK(info.st_mode):
                counts["skipped_symlinks"] += 1
                continue
            if stat.S_ISDIR(info.st_mode):
                counts["directories_seen"] += 1
                pending.append((Path(entry.path), depth + 1))
                continue
            if not stat.S_ISREG(info.st_mode):
                counts["skipped_nonregular_entries"] += 1
                continue
            if Path(entry.name).suffix.casefold() != ".sam":
                counts["skipped_nonmatching_regular_files"] += 1
                continue
            if info.st_size > MAX_FILE_BYTES:
                raise CorpusAnalysisError("a selected input exceeds the per-file byte cap")
            total_bytes += info.st_size
            if total_bytes > MAX_TOTAL_BYTES:
                raise CorpusAnalysisError("selected inputs exceed the total byte cap")
            selected.append(
                _SelectedInput(
                    Path(entry.path),
                    info.st_size,
                    info.st_dev,
                    info.st_ino,
                    info.st_mtime_ns,
                )
            )
            if len(selected) > MAX_SELECTED_FILES:
                raise CorpusAnalysisError("the selected-file count exceeds the safety cap")

    selected.sort(key=lambda item: os.fsencode(item.path.relative_to(root)))
    counts["selected_regular_files"] = len(selected)
    return selected, dict(sorted(counts.items()))


def _read_input(item: _SelectedInput) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(item.path, flags)
    except OSError as error:
        raise CorpusAnalysisError("cannot safely open a selected input") from error
    try:
        before = os.fstat(descriptor)
        enumerated_identity = (item.device, item.inode, item.size, item.modified_ns)
        opened_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        if not stat.S_ISREG(before.st_mode) or opened_identity != enumerated_identity:
            raise CorpusAnalysisError("a selected input changed before it was read")
        if before.st_size > MAX_FILE_BYTES:
            raise CorpusAnalysisError("a selected input exceeds the per-file byte cap")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise CorpusAnalysisError("a selected input ended during its bounded read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise CorpusAnalysisError("a selected input grew during its bounded read")
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_before != identity_after:
            raise CorpusAnalysisError("a selected input changed during its bounded read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _primary_body_width(document: Any) -> int | None:
    for layout in document.page_layouts:
        geometry = layout.primary_geometry
        if geometry is None or geometry.content_rect is None:
            continue
        width = geometry.content_rect.width_twips
        if type(width) is int and 0 < width <= MAX_NUMERIC_VALUE:
            return width
    return None


def _blank_boundaries(text: str) -> tuple[list[int], list[int]]:
    starts: list[int] = []
    ends: list[int] = []
    for match in _BLANK_BOUNDARY.finditer(text):
        if len(starts) >= MAX_BLANK_BOUNDARIES:
            raise CorpusAnalysisError("blank-line boundaries exceed the safety cap")
        starts.append(match.start())
        ends.append(match.end())
    return starts, ends


def _unit_bounds(
    position: int,
    text_length: int,
    boundary_starts: Sequence[int],
    boundary_ends: Sequence[int],
) -> tuple[int, int]:
    next_index = bisect.bisect_left(boundary_starts, position)
    end = boundary_starts[next_index] if next_index < len(boundary_starts) else text_length
    previous_index = bisect.bisect_right(boundary_ends, position) - 1
    start = boundary_ends[previous_index] if previous_index >= 0 else 0
    return start, end


def _material_character_count(text: str, work_budget: _WorkBudget) -> int:
    # Preserve literal ``@@`` as one material character while removing bounded
    # style selectors.  No resulting character is ever emitted.
    work_budget.reserve_material_scan(len(text))
    text = text.replace("@@", "\u0000")
    text = _ANY_INLINE.sub("", text)
    text = _STYLE_TOKEN.sub("", text)
    material = 0
    for line in text.splitlines():
        if _STRUCTURAL_LINE.fullmatch(line):
            continue
        material += sum(not char.isspace() for char in line)
    return material


def _nested_marker_name(line: str) -> str | None:
    match = _NESTED_MARKER.fullmatch(line)
    return match.group(1).casefold() if match is not None else None


def _classify_table_record(
    scan: _TableScan,
    family: str,
    line: str,
    *,
    expected_fields: int,
) -> tuple[int, ...] | None:
    scan.reserve_record()
    shape = scan.shapes[family]
    shape.candidate_count += 1
    tokens: list[str] = []
    for token_match in re.finditer(r"\S+", line):
        if len(tokens) >= MAX_TABLE_RECORD_FIELDS:
            shape.over_field_cap_count += 1
            return None
        token = token_match.group(0)
        if len(token) > MAX_TABLE_TOKEN_CHARACTERS:
            shape.over_token_cap_count += 1
            return None
        tokens.append(token)
    _bounded_counter_increment(shape.arities, len(tokens))
    if any(_UNSIGNED_TOKEN.fullmatch(token) is None for token in tokens):
        shape.nonnumeric_or_out_of_bounds_count += 1
        return None
    if len(tokens) != expected_fields:
        return None
    values = tuple(int(token) for token in tokens)
    if any(value > MAX_NUMERIC_VALUE for value in values):
        shape.nonnumeric_or_out_of_bounds_count += 1
        return None
    shape.canonical_count += 1
    return values


def _frame_header_fields(section: SectionRecord) -> tuple[int, ...] | None:
    fields: list[int] = []
    for line in section.raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _nested_marker_name(line) is not None:
            break
        if len(stripped) > MAX_TABLE_TOKEN_CHARACTERS or _SIGNED_TOKEN.fullmatch(stripped) is None:
            return None
        value = int(stripped)
        if abs(value) > MAX_NUMERIC_VALUE:
            return None
        fields.append(value)
        if len(fields) == 6:
            return tuple(fields)
        if len(fields) >= MAX_TABLE_RECORD_FIELDS:
            break
    return tuple(fields) if len(fields) >= 6 else None


def _scan_data_cells(
    scan: _TableScan,
    lines: Sequence[str],
    start: int,
    end: int,
    work_budget: _WorkBudget,
) -> tuple[_TableCellObservation, ...]:
    cells: list[_TableCellObservation] = []
    current_fields: tuple[int, ...] | None = None
    current_body: list[str] = []
    current_closed = False
    current_metadata_lines = 0
    current_started = False

    def flush() -> None:
        nonlocal current_fields, current_body, current_closed
        nonlocal current_metadata_lines, current_started
        if current_started and current_fields is not None:
            body = "\n".join(current_body)
            alignment_names = {
                "@": "left",
                "A": "right",
                "B": "center",
                "C": "justify",
            }
            alignments = tuple(
                alignment_names[match.group("code")]
                for match in _INLINE_ALIGNMENT.finditer(body)
            )
            cells.append(
                _TableCellObservation(
                    fields=current_fields,
                    has_nonwhitespace_body=any(line.strip() for line in current_body),
                    has_material=_material_character_count(body, work_budget) > 0,
                    inline_alignments=alignments,
                    post_close_metadata_lines=current_metadata_lines,
                )
            )
        current_fields = None
        current_body = []
        current_closed = False
        current_metadata_lines = 0
        current_started = False

    for line in lines[start:end]:
        marker_name = _nested_marker_name(line)
        if marker_name in {"e", "tble"}:
            break
        if _DATA_HEADER_START.match(line):
            flush()
            current_started = True
            current_fields = _classify_table_record(
                scan,
                "data",
                line.strip(),
                expected_fields=12,
            )
            continue
        if not current_started:
            continue
        stripped = line.strip()
        if stripped == ">":
            current_closed = True
        elif current_closed:
            if stripped:
                current_metadata_lines += 1
        else:
            current_body.append(line.lstrip("\t"))
    flush()
    return tuple(cells)


def _scan_table_section(
    scan: _TableScan,
    section: SectionRecord,
    table_marker: int,
    table_end: int,
    work_budget: _WorkBudget,
) -> _TableObservation:
    lines = section.raw_lines
    data_marker = next(
        (
            index
            for index in range(table_marker + 1, table_end)
            if _nested_marker_name(lines[index]) == "data"
        ),
        None,
    )
    definition_end = data_marker if data_marker is not None else table_end
    definition: tuple[int, ...] | None = None
    rows: list[tuple[int, ...]] = []
    columns: list[tuple[int, ...]] = []
    subsection = "tbl"
    for line in lines[table_marker + 1 : definition_end]:
        marker_name = _nested_marker_name(line)
        if marker_name is not None:
            subsection = marker_name if marker_name in {"h", "w"} else ""
            continue
        if not line.strip():
            continue
        if subsection == "tbl":
            values = _classify_table_record(
                scan,
                "tbl",
                line.strip(),
                expected_fields=9,
            )
            if values is not None and definition is None:
                definition = values
        elif subsection == "h":
            values = _classify_table_record(
                scan,
                "h",
                line.strip(),
                expected_fields=7,
            )
            if values is not None:
                rows.append(values)
        elif subsection == "w":
            values = _classify_table_record(
                scan,
                "w",
                line.strip(),
                expected_fields=5,
            )
            if values is not None:
                columns.append(values)

    cells: tuple[_TableCellObservation, ...] = ()
    if data_marker is not None:
        scan.data_markers += 1
        cells = _scan_data_cells(
            scan,
            lines,
            data_marker + 1,
            table_end,
            work_budget,
        )
    return _TableObservation(
        definition=definition,
        rows=tuple(rows),
        columns=tuple(columns),
        cells=cells,
        has_data_marker=data_marker is not None,
        frame_header=_frame_header_fields(section),
    )


def _scan_tables(
    scan: _TableScan,
    sections: Sequence[SectionRecord],
    work_budget: _WorkBudget,
) -> None:
    for section in sections:
        if section.name.casefold() != "frm":
            continue
        table_markers: list[int] = []
        for index, line in enumerate(section.raw_lines):
            if _nested_marker_name(line) != "tbl":
                continue
            scan.table_markers += 1
            if scan.table_markers > MAX_TABLES:
                raise CorpusAnalysisError("table markers exceed the safety cap")
            table_markers.append(index)
        for position, table_marker in enumerate(table_markers):
            table_end = (
                table_markers[position + 1]
                if position + 1 < len(table_markers)
                else len(section.raw_lines)
            )
            scan.tables.append(
                _scan_table_section(
                    scan,
                    section,
                    table_marker,
                    table_end,
                    work_budget,
                )
            )


def _safe_values(groups: Iterable[str]) -> tuple[int, ...] | None:
    values = tuple(int(group) for group in groups)
    if any(value > MAX_NUMERIC_VALUE for value in values):
        return None
    return values


def _following_font_size(text: str, command_end: int, unit_end: int) -> int | None:
    match = _FONT_SIZE_COMMAND.search(text, command_end, unit_end)
    if match is None:
        return None
    value = int(match.group("size"))
    return value if value <= MAX_NUMERIC_VALUE else None


def _top_counter(counter: Counter[Any]) -> dict[str, Any]:
    eligible = [
        (key, count)
        for key, count in counter.items()
        if count >= MIN_PUBLISHED_GROUP_COUNT
    ]
    eligible.sort(key=lambda item: (-item[1], item[0]))
    published = eligible[:MAX_PUBLISHED_GROUPS]
    published_count = sum(count for _, count in published)
    total = sum(counter.values())
    return {
        "groups": [
            {"value": list(key) if isinstance(key, tuple) else key, "count": count}
            for key, count in published
        ],
        "other_or_suppressed_count": total - published_count,
        "distinct_value_count": len(counter),
        "publication_rule": {
            "minimum_group_count": MIN_PUBLISHED_GROUP_COUNT,
            "maximum_groups": MAX_PUBLISHED_GROUPS,
        },
    }


def _numeric_distribution(values: Sequence[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "minimum": None, "maximum": None, **_top_counter(Counter())}
    counter: Counter[int] = Counter()
    for value in values:
        _bounded_counter_increment(counter, value)
    return {
        "count": len(values),
        "minimum": min(values),
        "maximum": max(values),
        **_top_counter(counter),
    }


def _quantiles(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"minimum": None, "median": None, "maximum": None}
    return {
        "minimum": round(min(values), 6),
        "median": round(float(median(values)), 6),
        "maximum": round(max(values), 6),
    }


def _pearson(xs: Sequence[int], ys: Sequence[int]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    x_square = sum((x - x_mean) ** 2 for x in xs)
    y_square = sum((y - y_mean) ** 2 for y in ys)
    denominator = math.sqrt(x_square * y_square)
    return round(numerator / denominator, 6) if denominator else None


def _region_report(observations: Sequence[_RegionObservation]) -> dict[str, Any]:
    first_values = [item.first for item in observations]
    widths = [item.width for item in observations]
    first_counter = _bounded_counter(first_values)
    dominant_count = max(first_counter.values(), default=0)
    dominant_first = min(
        (value for value, count in first_counter.items() if count == dominant_count),
        default=None,
    )

    usable = [item for item in observations if item.body_width is not None]
    deltas = [item.width - int(item.body_width) for item in usable]
    tolerance_counts = {
        str(tolerance): sum(abs(delta) <= tolerance for delta in deltas)
        for tolerance in range(0, 6)
    }
    five_twip = [item for item in usable if abs(item.width - int(item.body_width)) <= 5]
    nonfull = [item for item in usable if abs(item.width - int(item.body_width)) > 5]

    multiplier_counter: Counter[int] = Counter()
    exact_multiple_count = 0
    if dominant_first:
        for value in first_values:
            if value % dominant_first == 0:
                exact_multiple_count += 1
                _bounded_counter_increment(multiplier_counter, value // dominant_first)

    font_pairs: Counter[tuple[int, int]] = Counter()
    font_linked = [item for item in observations if item.following_font_size not in {None, 0}]
    for item in font_linked:
        _bounded_counter_increment(font_pairs, (int(item.following_font_size), item.first))
    ratios = [item.first / int(item.following_font_size) for item in font_linked]

    width_counter = _bounded_counter(widths)
    top_widths = [
        value
        for value, _ in sorted(width_counter.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    text_density_groups: list[dict[str, Any]] = []
    if dominant_first:
        for width in top_widths:
            for multiplier in range(1, 7):
                lengths = [
                    item.material_characters
                    for item in observations
                    if item.width == width and item.first == dominant_first * multiplier
                ]
                if len(lengths) < MIN_PUBLISHED_GROUP_COUNT:
                    continue
                text_density_groups.append(
                    {
                        "width": width,
                        "dominant_first_multiplier": multiplier,
                        "count": len(lengths),
                        "material_character_minimum": min(lengths),
                        "material_character_median": float(median(lengths)),
                        "material_character_maximum": max(lengths),
                    }
                )

    return {
        "bounded_arity_two_command_count": len(observations),
        "first_field": _numeric_distribution(first_values),
        "second_field": {
            **_numeric_distribution(widths),
            "zero_count": sum(width == 0 for width in widths),
            "positive_count": sum(width > 0 for width in widths),
        },
        "page_body_comparison": {
            "commands_with_usable_primary_body_width": len(usable),
            "second_minus_body_width": _numeric_distribution(deltas),
            "absolute_delta_at_most_twips": tolerance_counts,
            "within_five_twips": len(five_twip),
            "within_three_twips": tolerance_counts["3"],
            "five_but_not_three_twips": len(five_twip) - tolerance_counts["3"],
            "five_but_not_three_signed_deltas": _top_counter(
                _bounded_counter(
                    item.width - int(item.body_width)
                    for item in five_twip
                    if abs(item.width - int(item.body_width)) > 3
                )
            ),
            "outside_five_twips": len(nonfull),
            "outside_five_first_plus_width_at_most_body": sum(
                item.first + item.width <= int(item.body_width) for item in nonfull
            ),
            "outside_five_first_plus_width_exceeds_body": sum(
                item.first + item.width > int(item.body_width) for item in nonfull
            ),
            "outside_five_width_exceeds_body": sum(
                item.width > int(item.body_width) for item in nonfull
            ),
            "outside_five_first_at_least_body": sum(
                item.first >= int(item.body_width) for item in nonfull
            ),
            "all_usable_first_plus_width_exceeds_body": sum(
                item.first + item.width > int(item.body_width) for item in usable
            ),
        },
        "dominant_first_field_quantum": {
            "value": dominant_first,
            "exact_multiple_count": exact_multiple_count,
            "not_exact_multiple_count": len(observations) - exact_multiple_count,
            "multiplier_distribution": _top_counter(multiplier_counter),
        },
        "following_font_size": {
            "linked_command_count": len(font_linked),
            "font_size_to_first_field_pairs": _top_counter(font_pairs),
            "first_divided_by_font_size": _quantiles(ratios),
            "within_ten_twips_of_1_18_times_font_size": sum(
                abs(item.first - 1.18 * int(item.following_font_size)) <= 10
                for item in font_linked
            ),
            "within_twenty_twips_of_1_18_times_font_size": sum(
                abs(item.first - 1.18 * int(item.following_font_size)) <= 20
                for item in font_linked
            ),
        },
        "material_density": {
            "pearson_first_field_vs_material_character_count": _pearson(
                first_values,
                [item.material_characters for item in observations],
            ),
            "top_width_quantum_groups": text_density_groups,
            "material_character_definition": (
                "non-whitespace characters in the same blank-delimited storage unit "
                "after bounded inline commands, style selectors, and structural-only "
                "lines are removed"
            ),
        },
    }


def _indent_report(observations: Sequence[_IndentObservation]) -> dict[str, Any]:
    tuples = _bounded_counter(item.values for item in observations)
    field_values = [[item.values[index] for item in observations] for index in range(4)]
    units = _bounded_counter(item.unit_key for item in observations)
    return {
        "bounded_arity_four_command_count": len(observations),
        "tuple_distribution": _top_counter(tuples),
        "fields": [
            {
                "zero_count": sum(value == 0 for value in values),
                **_numeric_distribution(values),
            }
            for values in field_values
        ],
        "fourth_field_zero_nonzero_strata": {
            "zero": sum(value == 0 for value in field_values[3]),
            "nonzero": sum(value != 0 for value in field_values[3]),
        },
        "blank_delimited_unit_association": {
            "distinct_units": len(units),
            "units_with_multiple_indent_commands": sum(count > 1 for count in units.values()),
            "commands_before_all_material_characters": sum(
                item.before_material for item in observations
            ),
            "commands_with_region_in_same_unit": sum(
                item.unit_has_region for item in observations
            ),
            "commands_with_font_command_in_same_unit": sum(
                item.unit_has_font for item in observations
            ),
        },
    }


def _record_shape_report(shape: _RecordShape, expected_fields: int) -> dict[str, Any]:
    return {
        "candidate_count": shape.candidate_count,
        "expected_field_count": expected_fields,
        "canonical_exact_count": shape.canonical_count,
        "noncanonical_count": shape.candidate_count - shape.canonical_count,
        "arity_distribution": _top_counter(shape.arities),
        "over_field_cap_count": shape.over_field_cap_count,
        "over_token_character_cap_count": shape.over_token_cap_count,
        "nonnumeric_or_out_of_bounds_count": shape.nonnumeric_or_out_of_bounds_count,
    }


def _bit_frequency(values: Sequence[int]) -> dict[str, Any]:
    counts: Counter[int] = Counter()
    for value in values:
        for bit_index in range(31):
            bit = 1 << bit_index
            if value & bit:
                counts[bit] += 1
    published = [
        {"bit_value": bit, "count": count}
        for bit, count in sorted(counts.items())
        if count >= MIN_PUBLISHED_GROUP_COUNT
    ]
    return {
        "records": len(values),
        "published_bits": published,
        "other_or_suppressed_set_bit_occurrences": (
            sum(counts.values()) - sum(item["count"] for item in published)
        ),
        "publication_rule": {"minimum_count": MIN_PUBLISHED_GROUP_COUNT},
    }


def _safe_declared_grid(definition: tuple[int, ...]) -> bool:
    rows, columns = definition[:2]
    return (
        1 <= rows <= MAX_ANALYZED_TABLE_ROWS
        and 1 <= columns <= MAX_ANALYZED_TABLE_COLUMNS
        and rows * columns <= MAX_ANALYZED_TABLE_CELLS
    )


def _table_index_report(
    tables: Sequence[_TableObservation],
    *,
    family: str,
) -> dict[str, Any]:
    is_row = family == "h"
    records = [
        record
        for table in tables
        for record in (table.rows if is_row else table.columns)
    ]
    declared_position = 0 if is_row else 1
    default_dimension_position = 2 if is_row else 4
    default_gutter_position = 3 if is_row else 5
    declared_records_compared = 0
    within_records = 0
    declared_tables_with_records = 0
    tables_all_within = 0
    tables_complete = 0
    tables_eligible_for_complete_set = 0
    tables_max_matches = 0
    duplicate_indexes = 0
    comparable_tables = 0
    dimension_matches = 0
    gutter_matches = 0
    data_dimension_matches = 0
    data_comparable_records = 0
    data_tables_compared = 0
    data_tables_complete = 0
    data_tables_max_matches = 0
    opposite_records_compared = 0
    opposite_records_within = 0
    opposite_tables_with_records = 0
    opposite_tables_all_within = 0
    differing_dimension_records_compared = 0
    differing_dimension_records_within = 0
    differing_dimension_tables_with_records = 0
    differing_dimension_tables_all_within = 0
    index_work = 0
    for table in tables:
        table_records = table.rows if is_row else table.columns
        if table.definition is None:
            continue
        declared = table.definition[declared_position]
        opposite_declared = table.definition[1 - declared_position]
        comparable_tables += 1
        indexes = [record[0] for record in table_records]
        unique_indexes = set(indexes)
        duplicate_indexes += len(indexes) - len(unique_indexes)
        record_within = sum(0 <= index < declared for index in indexes)
        declared_records_compared += len(indexes)
        within_records += record_within
        if indexes:
            declared_tables_with_records += 1
            if record_within == len(indexes):
                tables_all_within += 1
        complete_index_set = False
        completeness_limit = (
            MAX_ANALYZED_TABLE_ROWS if is_row else MAX_ANALYZED_TABLE_COLUMNS
        )
        if 1 <= declared <= completeness_limit:
            index_work += declared
            if index_work > MAX_TABLE_INDEX_WORK:
                raise CorpusAnalysisError("table index-set checks exceed the work cap")
            tables_eligible_for_complete_set += 1
            complete_index_set = unique_indexes == set(range(declared))
            if complete_index_set:
                tables_complete += 1
        if indexes and max(indexes) == declared - 1:
            tables_max_matches += 1
        opposite_record_within = sum(
            0 <= index < opposite_declared for index in indexes
        )
        opposite_records_compared += len(indexes)
        opposite_records_within += opposite_record_within
        if indexes:
            opposite_tables_with_records += 1
            if opposite_record_within == len(indexes):
                opposite_tables_all_within += 1
        if declared != opposite_declared:
            differing_dimension_records_compared += len(indexes)
            differing_dimension_records_within += opposite_record_within
            if indexes:
                differing_dimension_tables_with_records += 1
                if opposite_record_within == len(indexes):
                    differing_dimension_tables_all_within += 1
        default_dimension = table.definition[default_dimension_position]
        default_gutter = table.definition[default_gutter_position]
        dimension_matches += sum(record[1] == default_dimension for record in table_records)
        gutter_matches += sum(record[2] == default_gutter for record in table_records)
        if table.has_data_marker:
            data_tables_compared += 1
            data_comparable_records += len(table_records)
            data_dimension_matches += sum(
                record[1] == default_dimension for record in table_records
            )
            if complete_index_set:
                data_tables_complete += 1
            if indexes and max(indexes) == declared - 1:
                data_tables_max_matches += 1
    return {
        "canonical_record_count": len(records),
        "index_field": _numeric_distribution([record[0] for record in records]),
        "dimension_field": _numeric_distribution([record[1] for record in records]),
        "gutter_field": _numeric_distribution([record[2] for record in records]),
        "flag_field": {
            **_numeric_distribution([record[3] for record in records]),
            "bit_frequency": _bit_frequency([record[3] for record in records]),
        },
        "tail_fields": [
            {"field_index": index, **_numeric_distribution([record[index] for record in records])}
            for index in range(4, 7 if is_row else 5)
        ],
        "declared_bound_checks": {
            "tables_with_canonical_definition": comparable_tables,
            "records_compared": declared_records_compared,
            "records_not_compared_without_canonical_definition": (
                len(records) - declared_records_compared
            ),
            "records_with_index_inside_declared_bound": within_records,
            "records_with_index_outside_declared_bound": (
                declared_records_compared - within_records
            ),
            "tables_with_records_compared": declared_tables_with_records,
            "tables_with_all_indexes_inside_declared_bound": tables_all_within,
            "tables_with_any_index_outside_declared_bound": (
                declared_tables_with_records - tables_all_within
            ),
            "tables_eligible_for_complete_zero_based_set_check": (
                tables_eligible_for_complete_set
            ),
            "tables_with_complete_zero_based_index_set": tables_complete,
            "tables_with_max_index_equal_declared_count_minus_one": tables_max_matches,
            "duplicate_index_records": duplicate_indexes,
        },
        "opposite_declared_dimension_check": {
            "opposite_dimension": "declared_columns" if is_row else "declared_rows",
            "records_compared": opposite_records_compared,
            "records_with_index_inside_opposite_declared_bound": (
                opposite_records_within
            ),
            "records_with_index_outside_opposite_declared_bound": (
                opposite_records_compared - opposite_records_within
            ),
            "tables_with_records_compared": opposite_tables_with_records,
            "tables_with_all_indexes_inside_opposite_declared_bound": (
                opposite_tables_all_within
            ),
            "tables_with_any_index_outside_opposite_declared_bound": (
                opposite_tables_with_records - opposite_tables_all_within
            ),
            "different_declared_dimensions": {
                "records_compared": differing_dimension_records_compared,
                "records_with_index_inside_opposite_declared_bound": (
                    differing_dimension_records_within
                ),
                "records_with_index_outside_opposite_declared_bound": (
                    differing_dimension_records_compared
                    - differing_dimension_records_within
                ),
                "tables_with_records_compared": (
                    differing_dimension_tables_with_records
                ),
                "tables_with_all_indexes_inside_opposite_declared_bound": (
                    differing_dimension_tables_all_within
                ),
                "tables_with_any_index_outside_opposite_declared_bound": (
                    differing_dimension_tables_with_records
                    - differing_dimension_tables_all_within
                ),
            },
            "counting_rule": (
                "compare exact field0 indexes with the other declared table dimension; "
                "table-level counts include only tables containing an exact record, "
                "and the nested stratum excludes equal row/column dimensions"
            ),
        },
        "default_correlations": {
            "records_compared": declared_records_compared,
            "records_not_compared_without_canonical_definition": (
                len(records) - declared_records_compared
            ),
            "dimension_equals_table_default": dimension_matches,
            "dimension_differs_from_table_default": (
                declared_records_compared - dimension_matches
            ),
            "gutter_equals_table_default": gutter_matches,
            "gutter_differs_from_table_default": (
                declared_records_compared - gutter_matches
            ),
            "data_bearing_records_compared": data_comparable_records,
            "data_bearing_tables_compared": data_tables_compared,
            "data_bearing_tables_with_complete_zero_based_index_set": (
                data_tables_complete
            ),
            "data_bearing_tables_with_max_index_equal_declared_count_minus_one": (
                data_tables_max_matches
            ),
            "data_bearing_dimension_equals_table_default": data_dimension_matches,
            "data_bearing_dimension_differs_from_table_default": (
                data_comparable_records - data_dimension_matches
            ),
        },
    }


def _table_coordinate_report(tables: Sequence[_TableObservation]) -> dict[str, Any]:
    cells = [cell for table in tables for cell in table.cells]
    rows = [cell.fields[0] for cell in cells]
    columns = [cell.fields[1] for cell in cells]
    declared_capacity = 0
    comparable_records = 0
    records_inside = 0
    comparable_tables = 0
    tables_with_cell_records = 0
    tables_all_inside = 0
    tables_min_row_zero = 0
    tables_min_column_zero = 0
    tables_max_row_matches = 0
    tables_max_column_matches = 0
    full_grids = 0
    tables_eligible_for_full_grid_check = 0
    duplicate_coordinates = 0
    swapped_records_compared = 0
    swapped_records_inside = 0
    swapped_tables_with_records = 0
    swapped_tables_all_inside = 0
    differing_dimension_records_compared = 0
    differing_dimension_records_inside = 0
    differing_dimension_tables_with_records = 0
    differing_dimension_tables_all_inside = 0
    grid_coordinate_work = 0
    for table in tables:
        if table.definition is None or not table.has_data_marker:
            continue
        comparable_tables += 1
        declared_rows, declared_columns = table.definition[:2]
        declared_capacity += declared_rows * declared_columns
        coordinates = [cell.coordinate for cell in table.cells]
        unique_coordinates = set(coordinates)
        duplicate_coordinates += len(coordinates) - len(unique_coordinates)
        comparable_records += len(coordinates)
        inside = sum(
            0 <= row < declared_rows and 0 <= column < declared_columns
            for row, column in coordinates
        )
        records_inside += inside
        if coordinates:
            tables_with_cell_records += 1
            if inside == len(coordinates):
                tables_all_inside += 1
        swapped_inside = sum(
            0 <= row < declared_columns and 0 <= column < declared_rows
            for row, column in coordinates
        )
        swapped_records_compared += len(coordinates)
        swapped_records_inside += swapped_inside
        if coordinates:
            swapped_tables_with_records += 1
            if swapped_inside == len(coordinates):
                swapped_tables_all_inside += 1
        if declared_rows != declared_columns:
            differing_dimension_records_compared += len(coordinates)
            differing_dimension_records_inside += swapped_inside
            if coordinates:
                differing_dimension_tables_with_records += 1
                if swapped_inside == len(coordinates):
                    differing_dimension_tables_all_inside += 1
        if coordinates:
            tables_min_row_zero += min(row for row, _ in coordinates) == 0
            tables_min_column_zero += min(column for _, column in coordinates) == 0
            tables_max_row_matches += (
                max(row for row, _ in coordinates) == declared_rows - 1
            )
            tables_max_column_matches += (
                max(column for _, column in coordinates) == declared_columns - 1
            )
        if _safe_declared_grid(table.definition):
            grid_coordinate_work += declared_rows * declared_columns
            if grid_coordinate_work > MAX_TABLE_GRID_COORDINATE_WORK:
                raise CorpusAnalysisError("table full-grid checks exceed the work cap")
            tables_eligible_for_full_grid_check += 1
            if unique_coordinates == {
                (row, column)
                for row in range(declared_rows)
                for column in range(declared_columns)
            }:
                full_grids += 1
    return {
        "canonical_cell_record_count": len(cells),
        "row_coordinate": _numeric_distribution(rows),
        "column_coordinate": _numeric_distribution(columns),
        "tables_compared": comparable_tables,
        "declared_cell_capacity": declared_capacity,
        "cell_records_compared": comparable_records,
        "cell_records_not_compared_without_canonical_definition": (
            len(cells) - comparable_records
        ),
        "records_inside_declared_grid": records_inside,
        "records_outside_declared_grid": comparable_records - records_inside,
        "tables_with_cell_records_compared": tables_with_cell_records,
        "tables_with_all_records_inside_declared_grid": tables_all_inside,
        "tables_with_any_record_outside_declared_grid": (
            tables_with_cell_records - tables_all_inside
        ),
        "tables_with_minimum_row_zero": tables_min_row_zero,
        "tables_with_minimum_column_zero": tables_min_column_zero,
        "tables_with_max_row_equal_declared_rows_minus_one": tables_max_row_matches,
        "tables_with_max_column_equal_declared_columns_minus_one": (
            tables_max_column_matches
        ),
        "tables_eligible_for_complete_rectangular_set_check": (
            tables_eligible_for_full_grid_check
        ),
        "tables_with_complete_rectangular_coordinate_set": full_grids,
        "duplicate_coordinate_records": duplicate_coordinates,
        "swapped_declared_grid_check": {
            "records_compared": swapped_records_compared,
            "records_inside_swapped_declared_grid": swapped_records_inside,
            "records_outside_swapped_declared_grid": (
                swapped_records_compared - swapped_records_inside
            ),
            "tables_with_cell_records_compared": swapped_tables_with_records,
            "tables_with_all_records_inside_swapped_declared_grid": (
                swapped_tables_all_inside
            ),
            "tables_with_any_record_outside_swapped_declared_grid": (
                swapped_tables_with_records - swapped_tables_all_inside
            ),
            "different_declared_dimensions": {
                "records_compared": differing_dimension_records_compared,
                "records_inside_swapped_declared_grid": (
                    differing_dimension_records_inside
                ),
                "records_outside_swapped_declared_grid": (
                    differing_dimension_records_compared
                    - differing_dimension_records_inside
                ),
                "tables_with_cell_records_compared": (
                    differing_dimension_tables_with_records
                ),
                "tables_with_all_records_inside_swapped_declared_grid": (
                    differing_dimension_tables_all_inside
                ),
                "tables_with_any_record_outside_swapped_declared_grid": (
                    differing_dimension_tables_with_records
                    - differing_dimension_tables_all_inside
                ),
            },
            "counting_rule": (
                "reinterpret exact data field0 as a column index and field1 as a row "
                "index; table-level counts include only tables containing an exact "
                "cell record, and the nested stratum excludes equal dimensions"
            ),
        },
    }


def _validate_table_merges(
    tables: Sequence[_TableObservation],
    *,
    require_raw_body_empty: bool,
) -> dict[str, int]:
    complete_anchors = 0
    covered_members = 0
    incomplete_anchors = 0
    uncovered_members = 0
    overlapping_candidate_rectangles = 0
    anchors_not_topology_checked = 0
    tables_topology_checked = 0
    topology_coordinate_work = 0
    for table in tables:
        coordinate_map: dict[tuple[int, int], _TableCellObservation] = {}
        for cell in table.cells:
            coordinate_map.setdefault(cell.coordinate, cell)
        if not coordinate_map:
            continue
        if table.definition is not None:
            row_count, column_count = table.definition[:2]
            safe_grid = _safe_declared_grid(table.definition)
        else:
            row_count = max(row for row, _ in coordinate_map) + 1
            column_count = max(column for _, column in coordinate_map) + 1
            safe_grid = (
                row_count <= MAX_ANALYZED_TABLE_ROWS
                and column_count <= MAX_ANALYZED_TABLE_COLUMNS
                and row_count * column_count <= MAX_ANALYZED_TABLE_CELLS
            )
        if not safe_grid:
            anchors_not_topology_checked += sum(
                bool(cell.fields[2] & 0x100) for cell in coordinate_map.values()
            )
            continue
        tables_topology_checked += 1
        covered: set[tuple[int, int]] = set()
        for (row, column), cell in sorted(coordinate_map.items()):
            if not cell.fields[2] & 0x100:
                continue
            row_span = max(1, cell.fields[3])
            column_span = max(1, cell.fields[4])
            span_in_bounds = (
                row_span <= MAX_ANALYZED_TABLE_ROWS
                and column_span <= MAX_ANALYZED_TABLE_COLUMNS
                and row_span * column_span <= MAX_ANALYZED_TABLE_CELLS
                and row + row_span <= row_count
                and column + column_span <= column_count
            )
            if span_in_bounds:
                topology_coordinate_work += row_span * column_span
                if topology_coordinate_work > MAX_MERGE_TOPOLOGY_COORDINATE_WORK:
                    raise CorpusAnalysisError("table merge-topology checks exceed the work cap")
            rectangle = (
                {
                    (target_row, target_column)
                    for target_row in range(row, row + row_span)
                    for target_column in range(column, column + column_span)
                    if (target_row, target_column) != (row, column)
                }
                if span_in_bounds
                else set()
            )
            overlap = bool(rectangle.intersection(covered))
            overlapping_candidate_rectangles += overlap
            valid = span_in_bounds and not overlap
            if valid:
                for target_row, target_column in rectangle:
                    member = coordinate_map.get((target_row, target_column))
                    if (
                        member is None
                        or not member.fields[2] & 0x80
                        or member.fields[2] & 0x100
                        or member.fields[3] != target_row - row
                        or member.fields[4] != target_column - column
                        or (
                            member.has_nonwhitespace_body
                            if require_raw_body_empty
                            else member.has_material
                        )
                    ):
                        valid = False
                        break
            if valid:
                complete_anchors += 1
                covered_members += len(rectangle)
                covered.update(rectangle)
            else:
                incomplete_anchors += 1
        uncovered_members += sum(
            bool(cell.fields[2] & 0x80)
            and not bool(cell.fields[2] & 0x100)
            and coordinate not in covered
            for coordinate, cell in coordinate_map.items()
        )
    return {
        "candidate_anchors_forming_complete_bounded_rectangles": complete_anchors,
        "member_records_covered_by_complete_rectangles": covered_members,
        "candidate_anchors_incomplete_or_invalid": incomplete_anchors,
        "member_records_not_covered_by_complete_rectangles": uncovered_members,
        "overlapping_candidate_rectangles": overlapping_candidate_rectangles,
        "tables_topology_checked_within_safe_grid_caps": tables_topology_checked,
        "anchors_not_checked_due_to_grid_caps": anchors_not_topology_checked,
    }


def _table_merge_report(tables: Sequence[_TableObservation]) -> dict[str, Any]:
    all_cells = [cell for table in tables for cell in table.cells]
    anchors = [cell for cell in all_cells if cell.fields[2] & 0x100]
    members = [
        cell
        for cell in all_cells
        if cell.fields[2] & 0x80 and not cell.fields[2] & 0x100
    ]
    raw_empty_validation = _validate_table_merges(
        tables,
        require_raw_body_empty=True,
    )
    material_empty_validation = _validate_table_merges(
        tables,
        require_raw_body_empty=False,
    )
    return {
        "anchor_bit_0x100_records": len(anchors),
        "member_bit_0x80_without_anchor_records": len(members),
        "anchors_also_carrying_member_bit_0x80": sum(
            bool(cell.fields[2] & 0x80) for cell in anchors
        ),
        "anchor_row_count_field": _numeric_distribution(
            [cell.fields[3] for cell in anchors]
        ),
        "anchor_column_count_field": _numeric_distribution(
            [cell.fields[4] for cell in anchors]
        ),
        "anchor_row_column_count_pairs": _top_counter(
            _bounded_counter((cell.fields[3], cell.fields[4]) for cell in anchors)
        ),
        "member_relative_offset_pairs": _top_counter(
            _bounded_counter((cell.fields[3], cell.fields[4]) for cell in members)
        ),
        "raw_body_empty_validation": raw_empty_validation,
        "material_empty_validation": material_empty_validation,
        "validation_rule": (
            "field3/field4 are tested as row/column span counts for bit0x100 records; "
            "each non-anchor coordinate must carry bit0x80, store its zero-based "
            "relative row/column offset in fields3/4; results are stratified between "
            "strictly empty raw bodies and bodies containing controls but no material"
        ),
    }


def _table_frame_tail_report(tables: Sequence[_TableObservation]) -> dict[str, Any]:
    tail_pairs: Counter[tuple[int, int]] = Counter()
    equal_tails = 0
    height_compared = 0
    height_exact = 0
    height_within_three = 0
    width_compared = 0
    width_exact = 0
    width_within_three = 0
    tables_skipped_by_grid_caps = 0
    extent_index_work = 0
    for table in tables:
        definition = table.definition
        if definition is None:
            continue
        tail = (definition[7], definition[8])
        _bounded_counter_increment(tail_pairs, tail)
        equal_tails += tail[0] == tail[1]
        header = table.frame_header
        if header is None or len(header) < 6:
            continue
        if not _safe_declared_grid(definition):
            tables_skipped_by_grid_caps += 1
            continue
        extent_index_work += definition[0] + definition[1]
        if extent_index_work > MAX_TABLE_EXTENT_INDEX_WORK:
            raise CorpusAnalysisError("table effective-extent checks exceed the work cap")
        row_overrides = {record[0]: record for record in table.rows}
        column_overrides = {record[0]: record for record in table.columns}
        row_total = sum(
            (
                row_overrides[index][1] + row_overrides[index][2]
                if index in row_overrides
                else definition[2] + definition[3]
            )
            for index in range(definition[0])
        )
        column_total = sum(
            (
                column_overrides[index][1] + column_overrides[index][2]
                if index in column_overrides
                else definition[4] + definition[5]
            )
            for index in range(definition[1])
        )
        outer_width = header[4] - header[2]
        outer_height = header[5] - header[3]
        height_gap = outer_height - row_total
        width_gap = outer_width - column_total
        height_compared += 1
        width_compared += 1
        height_exact += height_gap == tail[0]
        width_exact += width_gap == tail[0]
        height_within_three += abs(height_gap - tail[0]) <= 3
        width_within_three += abs(width_gap - tail[0]) <= 3
    return {
        "tail_pair_distribution": _top_counter(tail_pairs),
        "definitions_with_equal_field7_and_field8": equal_tails,
        "definitions_with_unequal_field7_and_field8": sum(tail_pairs.values()) - equal_tails,
        "outer_height_minus_effective_rows": {
            "tables_compared": height_compared,
            "equals_field7": height_exact,
            "within_three_twips_of_field7": height_within_three,
        },
        "outer_width_minus_effective_columns": {
            "tables_compared": width_compared,
            "equals_field7": width_exact,
            "within_three_twips_of_field7": width_within_three,
        },
        "tables_skipped_by_grid_caps": tables_skipped_by_grid_caps,
        "effective_extent_rule": (
            "sum each declared index using its explicit dimension+gutter record, "
            "otherwise the table default dimension+gutter"
        ),
    }


def _table_cell_field_report(tables: Sequence[_TableObservation]) -> dict[str, Any]:
    cells = [cell for table in tables for cell in table.cells]
    format_flags = [cell.fields[2] for cell in cells]
    field_values = {
        index: [cell.fields[index] for cell in cells]
        for index in range(5, 12)
    }
    field5_field9 = _bounded_counter(
        (bool(cell.fields[5]), bool(cell.fields[9])) for cell in cells
    )
    nonzero_pairs = _bounded_counter(
        (cell.fields[5], cell.fields[9])
        for cell in cells
        if cell.fields[5] or cell.fields[9]
    )
    alignment_pairs: Counter[tuple[str, int]] = Counter()
    expected_alignment_low_flag = {
        "left": 0x08,
        "right": 0x10,
        "center": 0x18,
        "justify": 0x20,
    }
    alignment_matches = 0
    cell_alignment_kind_observations = 0
    raw_alignment_command_occurrences = sum(
        len(cell.inline_alignments) for cell in cells
    )
    for cell in cells:
        low_flag = cell.fields[2] & 0x38
        for alignment in sorted(set(cell.inline_alignments)):
            _bounded_counter_increment(alignment_pairs, (alignment, low_flag))
            cell_alignment_kind_observations += 1
            alignment_matches += low_flag == expected_alignment_low_flag[alignment]
    raw_body_matrix = _bounded_counter(
        (cell.fields[7], cell.has_nonwhitespace_body) for cell in cells
    )
    material_matrix = _bounded_counter(
        (cell.fields[7], cell.has_material) for cell in cells
    )
    metadata_cells = [cell for cell in cells if cell.post_close_metadata_lines]
    border_values = field_values[6]
    nibble_distributions = []
    for nibble_index in range(4):
        nibble_distributions.append(
            {
                "nibble_index_low_to_high": nibble_index,
                **_numeric_distribution(
                    [(value >> (4 * nibble_index)) & 0xF for value in border_values]
                ),
            }
        )
    return {
        "format_flag_field2": {
            **_numeric_distribution(format_flags),
            "bit_frequency": _bit_frequency(format_flags),
        },
        "inline_alignment_low_flag_correlation": {
            "raw_command_occurrences": raw_alignment_command_occurrences,
            "cell_alignment_kind_observations": cell_alignment_kind_observations,
            "expected_low_flag_matches": alignment_matches,
            "other_low_flag_pairs": cell_alignment_kind_observations - alignment_matches,
            "pairs": _top_counter(alignment_pairs),
            "low_flag_mask": 0x38,
            "pair_counting_rule": (
                "at most one observation per cell and established inline alignment kind"
            ),
        },
        "fields5_through11": [
            {"field_index": index, **_numeric_distribution(field_values[index])}
            for index in range(5, 12)
        ],
        "field5_field9_nonzero_relationship": {
            "boolean_matrix": _top_counter(field5_field9),
            "nonzero_value_pairs": _top_counter(nonzero_pairs),
        },
        "field6_low_nibble_structure": {
            "values_with_no_bits_above_low_16": sum(value <= 0xFFFF for value in border_values),
            "values_with_bits_above_low_16": sum(value > 0xFFFF for value in border_values),
            "nibbles": nibble_distributions,
        },
        "field7_material_presence": {
            "raw_nonwhitespace_body_matrix": _top_counter(raw_body_matrix),
            "material_after_control_stripping_matrix": _top_counter(material_matrix),
            "binary_field_records": sum(cell.fields[7] in {0, 1} for cell in cells),
            "nonbinary_field_records": sum(cell.fields[7] not in {0, 1} for cell in cells),
        },
        "field8_binary_pattern": {
            "zero_count": sum(cell.fields[8] == 0 for cell in cells),
            "one_count": sum(cell.fields[8] == 1 for cell in cells),
            "other_count": sum(cell.fields[8] not in {0, 1} for cell in cells),
        },
        "fields10_and11_zero_pattern": {
            "field10_zero_count": sum(cell.fields[10] == 0 for cell in cells),
            "field11_zero_count": sum(cell.fields[11] == 0 for cell in cells),
            "records": len(cells),
        },
        "post_close_metadata": {
            "cells_with_nonblank_post_close_lines": len(metadata_cells),
            "nonblank_post_close_line_count": sum(
                cell.post_close_metadata_lines for cell in metadata_cells
            ),
            "such_cells_with_field7_equal_one": sum(
                cell.fields[7] == 1 for cell in metadata_cells
            ),
        },
    }


def _table_report(scan: _TableScan) -> dict[str, Any]:
    tables = scan.tables
    definitions = [table.definition for table in tables if table.definition is not None]
    definition_fields = [
        {
            "field_index": index,
            **_numeric_distribution([definition[index] for definition in definitions]),
        }
        for index in range(9)
    ]
    return {
        "table_marker_count": scan.table_markers,
        "data_marker_count": scan.data_markers,
        "tables_without_data_marker": scan.table_markers - scan.data_markers,
        "record_shapes": {
            "tbl": _record_shape_report(scan.shapes["tbl"], 9),
            "h": _record_shape_report(scan.shapes["h"], 7),
            "w": _record_shape_report(scan.shapes["w"], 5),
            "data": _record_shape_report(scan.shapes["data"], 12),
        },
        "table_definition": {
            "canonical_count": len(definitions),
            "fields": definition_fields,
            "declared_rows": _numeric_distribution(
                [definition[0] for definition in definitions]
            ),
            "declared_columns": _numeric_distribution(
                [definition[1] for definition in definitions]
            ),
            "declared_cell_capacity": sum(
                definition[0] * definition[1] for definition in definitions
            ),
            "field6_flags": {
                **_numeric_distribution([definition[6] for definition in definitions]),
                "bit_frequency": _bit_frequency(
                    [definition[6] for definition in definitions]
                ),
            },
        },
        "coordinate_bounds": _table_coordinate_report(tables),
        "row_h_records": _table_index_report(tables, family="h"),
        "column_w_records": _table_index_report(tables, family="w"),
        "tails_and_frame_extent_correlation": _table_frame_tail_report(tables),
        "merge_topology": _table_merge_report(tables),
        "data_cell_fields": _table_cell_field_report(tables),
        "interpretation_boundary": (
            "field positions, flags, topology, and correlations are reported; labels such "
            "as span, border, shading, content, and protection remain hypotheses unless "
            "independently confirmed"
        ),
    }


def analyze_corpus(corpus_dir: Path) -> dict[str, Any]:
    """Analyze a corpus without returning paths, names, text, or raw records."""

    selected, selection_counts = _enumerate_inputs(corpus_dir)
    limits = _parser_limits()
    regions: list[_RegionObservation] = []
    indents: list[_IndentObservation] = []
    shape_counts: dict[str, Counter[str]] = {
        "region": Counter(),
        "indent": Counter(),
    }
    documents_with_region = 0
    documents_with_indent = 0
    documents_with_region_and_usable_body = 0
    parseable_documents = 0
    parse_failures = 0
    decoded_documents = 0
    decode_failures = 0
    total_commands = 0
    work_budget = _WorkBudget()
    table_scan = _TableScan()

    for item in selected:
        data = _read_input(item)
        try:
            decoded = decode_bytes(data, limits=limits)
        except AmiProError:
            decode_failures += 1
            continue
        decoded_documents += 1
        body_width: int | None = None
        try:
            document = parse_bytes(data, source_name="<private-sample>", limits=limits)
        except AmiProError:
            parse_failures += 1
        else:
            parseable_documents += 1
            body_width = _primary_body_width(document)
            _scan_tables(table_scan, document.sections, work_budget)

        text = decoded.text
        boundary_starts, boundary_ends = _blank_boundaries(text)
        file_regions: list[_RegionObservation] = []
        file_indents: list[_IndentObservation] = []
        command_matches: list[re.Match[str]] = []
        for command in _COMMAND.finditer(text):
            total_commands += 1
            if total_commands > MAX_COMMANDS:
                raise CorpusAnalysisError("the inline-command count exceeds the safety cap")
            command_matches.append(command)

        command_units: list[tuple[re.Match[str], tuple[int, int]]] = []
        unit_kinds: dict[tuple[int, int], set[str]] = defaultdict(set)
        unit_command_counts: dict[tuple[int, int], int] = {}
        for command in command_matches:
            unit_start, unit_end = _unit_bounds(
                command.start(),
                len(text),
                boundary_starts,
                boundary_ends,
            )
            key = (unit_start, unit_end)
            command_units.append((command, key))
            unit_kinds[key].add(command.group("kind"))
            unit_command_counts[key] = unit_command_counts.get(key, 0) + 1
            if unit_command_counts[key] > MAX_COMMANDS_PER_STORAGE_UNIT:
                raise CorpusAnalysisError(
                    "a blank-delimited storage unit exceeds the target-command cap"
                )

        unit_features: dict[tuple[int, int], _UnitFeatures] = {}
        for (unit_start, unit_end), kinds in unit_kinds.items():
            unit_text = text[unit_start:unit_end]
            unit_features[(unit_start, unit_end)] = _UnitFeatures(
                has_region="#" in kinds,
                has_font=_FONT_SIZE_COMMAND.search(unit_text) is not None,
                material_characters=_material_character_count(unit_text, work_budget),
            )

        for command, unit_key in command_units:
            payload = command.group("payload")
            kind = command.group("kind")
            family = "region" if kind == "#" else "indent"
            if len(payload) > MAX_COMMAND_PAYLOAD_CHARS:
                shape_counts[family]["payload_over_length_cap"] += 1
                continue
            arity = payload.count(",") + 1
            shape_counts[family][f"observed_arity_{min(arity, 9)}"] += 1
            unit_start, unit_end = unit_key
            features = unit_features[unit_key]
            if kind == "#":
                match = _REGION_NUMERIC.fullmatch(payload)
                if match is None:
                    shape_counts[family]["not_bounded_exact_arity"] += 1
                    continue
                values = _safe_values((match.group("first"), match.group("width")))
                if values is None:
                    shape_counts[family]["numeric_out_of_bounds"] += 1
                    continue
                shape_counts[family]["bounded_exact_arity"] += 1
                observation = _RegionObservation(
                    first=values[0],
                    width=values[1],
                    body_width=body_width,
                    material_characters=features.material_characters,
                    following_font_size=_following_font_size(text, command.end(), unit_end),
                )
                file_regions.append(observation)
            else:
                match = _INDENT_NUMERIC.fullmatch(payload)
                if match is None:
                    shape_counts[family]["not_bounded_exact_arity"] += 1
                    continue
                values = _safe_values(match.groups())
                if values is None:
                    shape_counts[family]["numeric_out_of_bounds"] += 1
                    continue
                shape_counts[family]["bounded_exact_arity"] += 1
                file_indents.append(
                    _IndentObservation(
                        values=(values[0], values[1], values[2], values[3]),
                        unit_key=unit_key,
                        before_material=(
                            _material_character_count(
                                text[unit_start : command.start()], work_budget
                            )
                            == 0
                        ),
                        unit_has_region=features.has_region,
                        unit_has_font=features.has_font,
                    )
                )

        if file_regions:
            documents_with_region += 1
            if body_width is not None:
                documents_with_region_and_usable_body += 1
        if file_indents:
            documents_with_indent += 1
        regions.extend(file_regions)
        # Unit offsets are document-local.  Prefix them with a numeric document
        # ordinal so unit counts cannot collide, without exposing an identifier.
        ordinal = decoded_documents - 1
        indents.extend(
            _IndentObservation(
                values=item_observation.values,
                unit_key=(ordinal, *item_observation.unit_key),
                before_material=item_observation.before_material,
                unit_has_region=item_observation.unit_has_region,
                unit_has_font=item_observation.unit_has_font,
            )
            for item_observation in file_indents
        )

    return {
        "schema": SCHEMA,
        "method": {
            "sample_filter": (
                "recursively selected regular, non-symlink files whose suffix "
                "case-folds to .sam; all other regular files and all symlinks or "
                "nonregular entries were excluded"
            ),
            "command_filter": (
                "scanned the decoder's logical text envelope for non-escaped, "
                "case-sensitive <:#...> and <:I...> forms; every exact two-field or "
                "four-field bounded unsigned-decimal shape remains an observation; "
                "zero and nonzero field values are stratified without semantic gating"
            ),
            "body_width_filter": (
                "used the first parser-validated odd/right page content rectangle, "
                "falling back within that source-order layout to its parser-validated "
                "even/left rectangle, then continuing through source-order layouts; "
                "commands in parse failures or documents without such geometry are "
                "excluded only from body-width comparisons"
            ),
            "unit_filter": (
                "a storage unit is bounded by blank physical lines in decoded logical "
                "text; material counts remove bounded inline commands, style selectors, "
                "and structural-only lines"
            ),
            "table_filter": (
                "within parser-accepted files, scanned source-order [frm] sections for "
                "indented [tbl], [h], [w], and [data] structures; only exact bounded "
                "unsigned numeric records enter field and topology aggregates, while "
                "all candidate arities are counted"
            ),
            "privacy": (
                "no paths, names, text, source metadata, raw commands, per-document "
                "rows, timestamps, or corpus digest are emitted; cross-tab and value "
                "histograms publish at most 32 groups and suppress groups with count < 5; "
                "global numeric extrema are retained as coarse bounds and can be driven "
                "by a single observation"
            ),
            "interpretation_boundary": (
                "all results are private-corpus correlations and do not confirm field semantics"
            ),
        },
        "hard_limits": {
            "directory_entries": MAX_DIRECTORY_ENTRIES,
            "directory_depth": MAX_DIRECTORY_DEPTH,
            "selected_files": MAX_SELECTED_FILES,
            "name_bytes": MAX_NAME_BYTES,
            "file_bytes": MAX_FILE_BYTES,
            "total_bytes": MAX_TOTAL_BYTES,
            "lines_per_file": MAX_LINES_PER_FILE,
            "records_per_file": MAX_RECORDS_PER_FILE,
            "target_commands": MAX_COMMANDS,
            "target_commands_per_storage_unit": MAX_COMMANDS_PER_STORAGE_UNIT,
            "blank_line_boundaries_per_file": MAX_BLANK_BOUNDARIES,
            "aggregate_material_scan_characters": MAX_MATERIAL_SCAN_CHARACTERS,
            "command_payload_characters": MAX_COMMAND_PAYLOAD_CHARS,
            "numeric_token_digits": MAX_NUMERIC_TOKEN_DIGITS,
            "numeric_value": MAX_NUMERIC_VALUE,
            "distinct_values_per_distribution": MAX_DISTINCT_VALUES,
            "tables": MAX_TABLES,
            "table_structural_records": MAX_TABLE_STRUCTURAL_RECORDS,
            "table_record_fields": MAX_TABLE_RECORD_FIELDS,
            "table_token_characters": MAX_TABLE_TOKEN_CHARACTERS,
            "analyzed_table_rows": MAX_ANALYZED_TABLE_ROWS,
            "analyzed_table_columns": MAX_ANALYZED_TABLE_COLUMNS,
            "analyzed_table_cells": MAX_ANALYZED_TABLE_CELLS,
            "table_index_work": MAX_TABLE_INDEX_WORK,
            "table_grid_coordinate_work": MAX_TABLE_GRID_COORDINATE_WORK,
            "merge_topology_coordinate_work": MAX_MERGE_TOPOLOGY_COORDINATE_WORK,
            "table_extent_index_work": MAX_TABLE_EXTENT_INDEX_WORK,
        },
        "sample": {
            **selection_counts,
            "decoded_selected_files": decoded_documents,
            "decode_failures": decode_failures,
            "parser_accepted_files": parseable_documents,
            "parser_rejected_files": parse_failures,
        },
        "paragraph_region": {
            "documents_with_bounded_arity_two_command": documents_with_region,
            "documents_with_bounded_arity_two_command_and_usable_body_width": (
                documents_with_region_and_usable_body
            ),
            "shape_counts": dict(sorted(shape_counts["region"].items())),
            **_region_report(regions),
        },
        "four_field_I_command": {
            "documents_with_bounded_arity_four_command": documents_with_indent,
            "shape_counts": dict(sorted(shape_counts["indent"].items())),
            **_indent_report(indents),
        },
        "tables": _table_report(table_scan),
    }


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Emit path-free numeric aggregates for private SAM layout and table records."
        )
    )
    parser.add_argument(
        "corpus_dir",
        type=Path,
        help="explicit directory containing private .sam inputs (never echoed in JSON)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_argument_parser().parse_args(argv)
    try:
        report = analyze_corpus(arguments.corpus_dir)
    except CorpusAnalysisError as error:
        print(f"corpus analysis failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
