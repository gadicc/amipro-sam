"""Byte decoding with conservative legacy-codepage handling."""

from __future__ import annotations

import codecs
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field, replace

from .errors import DecodeError, ResourceLimitError
from .limits import ParseLimits
from .model import Diagnostic, Lossiness, Severity, SourceSpan
from .syntax import (
    MultilineContainerScanner,
    parse_embedded_manifest_row,
)

_CHARSET_SECTION = re.compile(
    rb"(?ims)^\[charset\][ \t]*\r?\n(?P<body>.*?)(?=^\[[A-Za-z][^\]\r\n]*\]|\Z)"
)
_CODEPAGE_TEXT = re.compile(rb"(?i)(?:CP|CODE[ -]?PAGE)[ \t]*([0-9]{3,5})\b")
_CODEPAGE_NAMES = {
    437: "cp437",
    850: "cp850",
    852: "cp852",
    855: "cp855",
    857: "cp857",
    860: "cp860",
    861: "cp861",
    863: "cp863",
    865: "cp865",
    866: "cp866",
    874: "cp874",
    932: "cp932",
    936: "gbk",
    949: "cp949",
    950: "cp950",
    1250: "cp1250",
    1251: "cp1251",
    1252: "cp1252",
    1253: "cp1253",
    1254: "cp1254",
    1255: "cp1255",
    1256: "cp1256",
    1257: "cp1257",
    1258: "cp1258",
}
_LINE_BREAK = re.compile(r"\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]")
_EMBEDDED_MARKER_LINE = re.compile(
    r"(?i)(?:\A|(?<=[\r\n]))\[embedded\][ \t]*(?=\r|\n|\Z)"
)
_LINE_ENDINGS = "\n\v\f\r\x1c\x1d\x1e\x85\u2028\u2029"
_MAX_DIRECTORY_CANDIDATES = 8
_FIXED_WIDTH_CODECS = {
    "utf-16-le": 2,
    "utf-16-be": 2,
    "utf-32-le": 4,
    "utf-32-be": 4,
}


@dataclass(slots=True)
class DecodedSource:
    text: str
    encoding: str
    newline: str
    line_byte_offsets: list[int]
    diagnostics: list[Diagnostic] = field(default_factory=list)
    binary_ranges: tuple[tuple[int, int], ...] = ()
    unindexed_ranges: tuple[tuple[int, int], ...] = ()
    directory_byte_offset: int | None = None
    directory_pointer_valid: bool | None = None
    tail_byte_offset: int | None = None
    source_byte_length: int | None = None

    def span_for_line(self, line_index: int, raw_line: str) -> SourceSpan:
        start = self.line_byte_offsets[line_index]
        encoded_length = len(raw_line.encode(self.encoding, errors="surrogateescape"))
        end = start + encoded_length
        if self.source_byte_length is not None:
            end = min(end, self.source_byte_length)
        return SourceSpan(
            line=line_index + 1,
            column=1,
            byte_offset=start,
            end_byte_offset=end,
        )


def decode_bytes(
    data: bytes,
    *,
    limits: ParseLimits | None = None,
    encoding: str | None = None,
) -> DecodedSource:
    limits = limits or ParseLimits()
    defaults = ParseLimits()
    file_limit = _effective_limit(
        limits.max_file_bytes, defaults.max_file_bytes, "input byte limit"
    )
    line_limit = _effective_limit(
        limits.max_lines, defaults.max_lines, "text line limit"
    )
    line_byte_limit = _effective_limit(
        limits.max_line_bytes, defaults.max_line_bytes, "text line byte limit"
    )
    record_limit = _effective_limit(
        limits.max_records, defaults.max_records, "record limit"
    )
    embedded_record_limit = _effective_limit(
        limits.max_embedded_records,
        defaults.max_embedded_records,
        "embedded-directory record limit",
    )
    limits = replace(
        limits,
        max_file_bytes=file_limit,
        max_lines=line_limit,
        max_line_bytes=line_byte_limit,
        max_records=record_limit,
        max_embedded_records=embedded_record_limit,
    )
    if len(data) > file_limit:
        raise ResourceLimitError(
            f"input is {len(data)} bytes; configured maximum is {file_limit}"
        )

    diagnostics: list[Diagnostic] = []
    encoding_evidence_end = len(data)
    if encoding is None and not _starts_with_supported_bom(data):
        # Encoding evidence ends with the verified outer EDOC close. Appended
        # payload and damaged/unindexed tail bytes cannot change how readable
        # body text is decoded. UTF-8+surrogateescape is used only to find the
        # ASCII-compatible structure and round-trips every source byte.
        encoding_evidence_end = _encoding_text_end(data)
    selected, bom_length, reason = _select_encoding(
        data,
        override=encoding,
        evidence_end=encoding_evidence_end,
    )
    payload = data[bom_length:]
    decode_failure_offset: int | None = None
    try:
        text = payload.decode(selected, errors="surrogateescape")
    except UnicodeDecodeError as exc:
        # Multibyte codecs cannot apply surrogateescape to every truncated code
        # unit. A replacement decode is safe for structural recovery because
        # the original bytes remain authoritative and appended tail ranges are
        # diagnosed from ``data`` below.
        decode_failure_offset = bom_length + exc.start
        text = payload.decode(selected, errors="replace")
    except LookupError as exc:
        raise DecodeError(f"unknown text encoding: {selected}") from exc

    (
        logical_text,
        offsets,
        binary_ranges,
        unindexed_ranges,
        directory_byte_offset,
        directory_pointer_valid,
        tail_byte_offset,
        directory_decode_failure,
    ) = _logical_text_envelope(
        text,
        data,
        encoding=selected,
        bom_length=bom_length,
        limits=limits,
    )
    text_surrogate_count = sum(
        1 for char in logical_text if 0xDC80 <= ord(char) <= 0xDCFF
    )
    decode_failure_in_text = bool(
        directory_decode_failure
        or (
            decode_failure_offset is not None
            and (tail_byte_offset is None or decode_failure_offset < tail_byte_offset)
        )
    )
    if text_surrogate_count or decode_failure_in_text:
        details = []
        if text_surrogate_count:
            details.append(
                f"preserved {text_surrogate_count} undecodable textual byte(s) "
                "as surrogate code points"
            )
        if decode_failure_in_text:
            details.append("replaced an invalid multibyte textual sequence")
        diagnostics.append(
            Diagnostic(
                severity=Severity.WARNING,
                code="decode-undecodable-bytes",
                message="; ".join(details),
                lossiness=Lossiness.CONTENT,
            )
        )
    if reason != "explicit override":
        diagnostics.append(
            Diagnostic(
                severity=Severity.INFO,
                code="decode-selected",
                message=f"decoded as {selected} ({reason})",
                lossiness=Lossiness.NONE,
            )
        )

    newline = (
        "\r\n"
        if "\r\n" in logical_text
        else "\n"
        if "\n" in logical_text
        else "\r"
    )
    return DecodedSource(
        text=logical_text,
        encoding=selected,
        newline=newline,
        line_byte_offsets=offsets,
        diagnostics=diagnostics,
        binary_ranges=binary_ranges,
        unindexed_ranges=unindexed_ranges,
        directory_byte_offset=directory_byte_offset,
        directory_pointer_valid=directory_pointer_valid,
        tail_byte_offset=tail_byte_offset,
        source_byte_length=len(data),
    )


def _effective_limit(configured: object, hard_limit: int, description: str) -> int:
    if isinstance(configured, bool) or not isinstance(configured, int) or configured < 0:
        raise ResourceLimitError(
            f"{description} must be configured as a nonnegative integer"
        )
    return min(configured, hard_limit)


def _logical_text_envelope(
    text: str,
    data: bytes,
    *,
    encoding: str,
    bom_length: int,
    limits: ParseLimits,
) -> tuple[
    str,
    list[int],
    tuple[tuple[int, int], ...],
    tuple[tuple[int, int], ...],
    int | None,
    bool | None,
    int | None,
    bool,
]:
    """Remove only validated indexed payload spans from line-oriented text.

    The full byte stream is decoded once for legacy code-page detection, but
    newline-dense payloads are never expanded into a list of Python strings.
    Only manifest-authorized, in-range asset bytes receive the binary exemption
    from textual line limits.
    """

    offsets: list[int] = []
    byte_position = bom_length
    version_byte_offset = bom_length
    version_seen = False
    edoc_seen = False
    depth = 0
    scanner = MultilineContainerScanner()
    tail_char_offset: int | None = None
    tail_byte_offset: int | None = None
    prefix_line_count = 0

    for _char_start, char_end, line in _iter_text_lines(text):
        raw_line_length = _source_line_length(
            line,
            encoding=encoding,
            available_bytes=len(data) - byte_position,
        )
        _check_line_size(raw_line_length, limits)
        prefix_line_count += 1
        if prefix_line_count > limits.max_lines:
            raise ResourceLimitError(
                f"input has more than {limits.max_lines} lines; parsing stopped"
            )
        offsets.append(byte_position)
        body = _without_line_ending(line)
        if body.strip().lower() == "[ver]" and not version_seen:
            version_byte_offset = byte_position
            version_seen = True
        if not edoc_seen:
            edoc_seen = body.strip().lower() == "[edoc]"
        else:
            scan = scanner.scan_line(body)
            if scan.standalone_terminator:
                if depth:
                    depth -= 1
                else:
                    tail_char_offset = char_end
                    tail_byte_offset = byte_position + raw_line_length
                    break
            else:
                depth += int(scan.opener is not None)
        byte_position += raw_line_length

    if tail_char_offset is None or tail_byte_offset is None:
        # No outer EDOC close means every physical line is still textual.
        if not offsets:
            offsets.append(bom_length)
        return text, offsets, (), (), None, None, None, False

    prefix_text = text[:tail_char_offset]
    directory_text = ""
    directory_offsets: list[int] = []
    directory_byte_offset: int | None = None
    directory_pointer_valid: bool | None = None
    directory_decode_failure = False
    binary_ranges: tuple[tuple[int, int], ...] = ()
    for marker_byte_offset in reversed(
        _embedded_marker_candidates(
            data,
            start=tail_byte_offset,
            encoding=encoding,
        )
    ):
        directory_bytes = data[marker_byte_offset:]
        candidate_decode_failure = False
        try:
            candidate_text = directory_bytes.decode(
                encoding, errors="surrogateescape"
            )
        except UnicodeDecodeError:
            candidate_text = directory_bytes.decode(encoding, errors="replace")
            candidate_decode_failure = True
        if _EMBEDDED_MARKER_LINE.match(candidate_text) is None:
            continue
        scan = _scan_embedded_directory(
            candidate_text,
            source_byte_length=len(directory_bytes),
            marker_byte_offset=marker_byte_offset,
            base_byte_offset=version_byte_offset,
            tail_byte_offset=tail_byte_offset,
            data_length=len(data),
            encoding=encoding,
            limits=limits,
            existing_line_count=prefix_line_count,
        )
        if scan is None:
            continue
        (
            directory_text,
            directory_offsets,
            binary_ranges,
            directory_pointer_valid,
        ) = scan
        directory_byte_offset = marker_byte_offset
        directory_decode_failure = candidate_decode_failure
        break

    payload_end = directory_byte_offset if directory_byte_offset is not None else len(data)
    unindexed_ranges = _complement_ranges(
        tail_byte_offset,
        payload_end,
        binary_ranges,
    )
    total_line_count = (
        prefix_line_count
        + len(directory_offsets)
        + _check_raw_text_ranges(
            data,
            unindexed_ranges,
            limits,
            encoding=encoding,
        )
    )
    if total_line_count > limits.max_lines:
        raise ResourceLimitError(
            f"input has more than {limits.max_lines} lines; parsing stopped"
        )

    separator = ""
    if directory_text and prefix_text and prefix_text[-1] not in _LINE_ENDINGS:
        separator = "\n"
    logical_text = prefix_text + separator + directory_text
    offsets.extend(directory_offsets)
    return (
        logical_text,
        offsets,
        binary_ranges,
        unindexed_ranges,
        directory_byte_offset,
        directory_pointer_valid,
        tail_byte_offset,
        directory_decode_failure,
    )


def _iter_text_lines(
    text: str, *, start: int = 0, end: int | None = None
) -> Iterator[tuple[int, int, str]]:
    stop = len(text) if end is None else min(end, len(text))
    cursor = start
    for match in _LINE_BREAK.finditer(text, start, stop):
        yield cursor, match.end(), text[cursor : match.end()]
        cursor = match.end()
    if cursor < stop:
        yield cursor, stop, text[cursor:stop]


def _without_line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line and line[-1] in _LINE_ENDINGS:
        return line[:-1]
    return line


def _source_line_length(
    line: str,
    *,
    encoding: str,
    available_bytes: int,
) -> int:
    """Return an exact source length, including a truncated final code unit."""

    encoded_length = len(line.encode(encoding, errors="surrogateescape"))
    return min(encoded_length, max(0, available_bytes))


def _check_line_size(raw_line_length: int, limits: ParseLimits) -> None:
    if raw_line_length > limits.max_line_bytes:
        raise ResourceLimitError(
            f"input contains a {raw_line_length}-byte line; "
            f"configured maximum is {limits.max_line_bytes}"
        )


def _embedded_marker_candidates(
    data: bytes,
    *,
    start: int,
    encoding: str,
) -> tuple[int, ...]:
    """Return a bounded suffix of codec-valid, line-start marker candidates."""

    parts: list[bytes] = []
    for character in "[embedded]":
        variants = {
            character.lower().encode(encoding),
            character.upper().encode(encoding),
        }
        encoded_variants = sorted(variants)
        if len(encoded_variants) == 1:
            parts.append(re.escape(encoded_variants[0]))
        else:
            parts.append(
                b"(?:"
                + b"|".join(re.escape(item) for item in encoded_variants)
                + b")"
            )
    marker = re.compile(b"".join(parts))
    encoded_newlines = (
        "\r".encode(encoding),
        "\n".encode(encoding),
    )
    candidates: deque[int] = deque(maxlen=_MAX_DIRECTORY_CANDIDATES)
    for match in marker.finditer(data, start):
        offset = match.start()
        if any(
            offset >= len(newline)
            and data[offset - len(newline) : offset] == newline
            for newline in encoded_newlines
        ):
            candidates.append(offset)
    return tuple(candidates)


def _scan_embedded_directory(
    text: str,
    *,
    source_byte_length: int,
    marker_byte_offset: int,
    base_byte_offset: int,
    tail_byte_offset: int,
    data_length: int,
    encoding: str,
    limits: ParseLimits,
    existing_line_count: int,
) -> tuple[
    str,
    list[int],
    tuple[tuple[int, int], ...],
    bool,
] | None:
    offsets: list[int] = []
    ranges: list[tuple[int, int]] = []
    row_count = 0
    last_nonempty = ""
    byte_position = marker_byte_offset
    line_count = existing_line_count
    for line_index, (_, _, line) in enumerate(_iter_text_lines(text)):
        if line_index >= min(limits.max_records, limits.max_embedded_records) + 2:
            raise ResourceLimitError(
                "embedded directory exceeds the configured record limit"
            )
        raw_line_length = _source_line_length(
            line,
            encoding=encoding,
            available_bytes=(
                source_byte_length - (byte_position - marker_byte_offset)
            ),
        )
        _check_line_size(raw_line_length, limits)
        line_count += 1
        if line_count > limits.max_lines:
            raise ResourceLimitError(
                f"input has more than {limits.max_lines} lines; parsing stopped"
            )
        offsets.append(byte_position)
        body = _without_line_ending(line)
        if line_index:
            stripped = body.strip()
            if stripped:
                last_nonempty = stripped
            row = parse_embedded_manifest_row(body)
            if row is not None:
                row_count += 1
                asset_offset, asset_length, preview_offset, preview_length = row
                for relative_offset, length in (
                    (asset_offset, asset_length),
                    (preview_offset, preview_length),
                ):
                    physical = base_byte_offset + relative_offset
                    if (
                        length > 0
                        and tail_byte_offset <= physical <= marker_byte_offset
                        and length <= marker_byte_offset - physical
                        and physical <= data_length
                    ):
                        ranges.append((physical, physical + length))
        byte_position += raw_line_length

    pointer_valid = bool(
        re.fullmatch(r"\d{1,20}", last_nonempty)
        and int(last_nonempty) == marker_byte_offset - base_byte_offset
    )
    if not row_count and not pointer_valid:
        return None
    return text, offsets, _merge_ranges(ranges), pointer_valid


def _merge_ranges(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _complement_ranges(
    start: int,
    end: int,
    covered: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    cursor = start
    for range_start, range_end in covered:
        if cursor < range_start:
            result.append((cursor, range_start))
        cursor = max(cursor, range_end)
    if cursor < end:
        result.append((cursor, end))
    return tuple(result)


def _check_raw_text_ranges(
    data: bytes,
    ranges: tuple[tuple[int, int], ...],
    limits: ParseLimits,
    *,
    encoding: str,
) -> int:
    """Apply text limits across undeclared ranges while skipping indexed bytes.

    An indexed span is exempt from text decoding, but it is not a logical line
    boundary.  Keeping one line-length/count state across the complementary
    ranges prevents tiny declared spans from fragmenting an oversized textual
    record into individually acceptable pieces.
    """

    fixed_width = _FIXED_WIDTH_CODECS.get(encoding)
    if fixed_width is None:
        return _check_raw_encoded_ranges(
            data,
            ranges,
            limits,
            encoding=encoding,
        )

    line_count = 0
    line_length = 0
    carry = ""

    def consume(decoded_text: str, *, final: bool) -> None:
        nonlocal carry, line_count, line_length
        value = carry + decoded_text
        carry = ""
        if not final and value.endswith("\r"):
            carry = "\r"
            value = value[:-1]
        cursor = 0
        for match in _LINE_BREAK.finditer(value):
            piece = value[cursor : match.end()]
            line_length += len(piece.encode(encoding, errors="replace"))
            if line_length > limits.max_line_bytes:
                raise ResourceLimitError(
                    f"input contains a line longer than {limits.max_line_bytes} bytes; "
                    "configured maximum was exceeded"
                )
            line_count += 1
            line_length = 0
            cursor = match.end()
        remainder = value[cursor:]
        if remainder:
            line_length += len(remainder.encode(encoding, errors="replace"))
            if line_length > limits.max_line_bytes:
                raise ResourceLimitError(
                    f"input contains a line longer than {limits.max_line_bytes} bytes; "
                    "configured maximum was exceeded"
                )

    for start, end in ranges:
        decode_end = end
        decode_end -= (end - start) % fixed_width
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        cursor = start
        while cursor < decode_end:
            chunk_end = min(decode_end, cursor + 64 * 1024)
            consume(decoder.decode(data[cursor:chunk_end], final=False), final=False)
            cursor = chunk_end
        # Finalize the codec state for this undeclared range, but retain text
        # line state (including a possible CR) across the skipped indexed span.
        consume(decoder.decode(b"", final=True), final=False)
        partial_length = end - decode_end
        if partial_length:
            # A partial fixed-width unit cannot itself be a decoded separator.
            # It is still source text and counts by its exact byte length. Flush
            # a pending CR first so a later range cannot join an LF across it.
            if carry:
                consume("", final=True)
            line_length += partial_length
            if line_length > limits.max_line_bytes:
                raise ResourceLimitError(
                    f"input contains a line longer than {limits.max_line_bytes} bytes; "
                    "configured maximum was exceeded"
                )
    if carry:
        consume("", final=True)
    if ranges and line_length:
        line_count += 1
    return line_count


def _check_raw_encoded_ranges(
    data: bytes,
    ranges: tuple[tuple[int, int], ...],
    limits: ParseLimits,
    *,
    encoding: str,
) -> int:
    """Count encoded line records without lossy decode/re-encode accounting."""

    tokens: set[bytes] = set()
    for separator in ("\r\n", *_LINE_ENDINGS):
        try:
            encoded = separator.encode(encoding)
        except UnicodeEncodeError:
            continue
        if encoded:
            tokens.add(encoded)
    ordered_tokens = sorted(tokens, key=lambda item: (-len(item), item))
    pattern = re.compile(
        b"(?:" + b"|".join(re.escape(item) for item in ordered_tokens) + b")"
    )
    overlap = max(len(item) for item in ordered_tokens) - 1
    carry = b""
    line_count = 0
    line_length = 0

    def add_length(length: int) -> None:
        nonlocal line_length
        line_length += length
        if line_length > limits.max_line_bytes:
            raise ResourceLimitError(
                f"input contains a line longer than {limits.max_line_bytes} bytes; "
                "configured maximum was exceeded"
            )

    def consume(chunk: bytes, *, final: bool) -> None:
        nonlocal carry, line_count, line_length
        value = carry + chunk
        carry = b""
        process_limit = len(value) if final else max(0, len(value) - overlap)
        cursor = 0
        for match in pattern.finditer(value):
            if match.start() >= process_limit:
                break
            if not final and match.end() > process_limit:
                add_length(match.start() - cursor)
                carry = value[match.start() :]
                return
            add_length(match.end() - cursor)
            line_count += 1
            line_length = 0
            cursor = match.end()
        add_length(process_limit - cursor)
        carry = value[process_limit:]

    for start, end in ranges:
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + 64 * 1024)
            consume(data[cursor:chunk_end], final=False)
            cursor = chunk_end
    consume(b"", final=True)
    if ranges and line_length:
        line_count += 1
    return line_count


def _select_encoding(
    data: bytes,
    *,
    override: str | None,
    evidence_end: int | None = None,
) -> tuple[str, int, str]:
    if override:
        try:
            canonical = codecs.lookup(override).name
        except LookupError as exc:
            raise DecodeError(f"unknown text encoding: {override}") from exc
        if canonical == "utf-8-sig":
            bom_length = len(codecs.BOM_UTF8) if data.startswith(codecs.BOM_UTF8) else 0
            return "utf-8", bom_length, "explicit override"
        if canonical in {"utf-16", "utf-32"}:
            candidates = (
                (codecs.BOM_UTF32_LE, "utf-32-le"),
                (codecs.BOM_UTF32_BE, "utf-32-be"),
                (codecs.BOM_UTF16_LE, "utf-16-le"),
                (codecs.BOM_UTF16_BE, "utf-16-be"),
            )
            for bom, name in candidates:
                if data.startswith(bom) and name.startswith(canonical):
                    return name, len(bom), "explicit override"
            raise DecodeError(f"explicit {canonical} input requires a byte-order mark")
        return canonical, 0, "explicit override"

    bom_encodings = (
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    )
    for bom, name in bom_encodings:
        if data.startswith(bom):
            return name, len(bom), "byte-order mark"

    logical_end = len(data) if evidence_end is None else min(len(data), evidence_end)
    charset_match = _CHARSET_SECTION.search(data[: min(64 * 1024, logical_end)])
    if charset_match and (match := _CODEPAGE_TEXT.search(charset_match.group("body"))):
        codepage = int(match.group(1))
        candidate = _CODEPAGE_NAMES.get(codepage, f"cp{codepage}")
        try:
            return codecs.lookup(candidate).name, 0, f"[charset] description names CP {codepage}"
        except LookupError:
            pass

    valid_utf8, ascii_only = _utf8_probe(data, logical_end)
    if not valid_utf8:
        return "cp1252", 0, "conservative Ami Pro Western default"
    # ASCII is valid in both; keep the historically likely label for diagnostics.
    if ascii_only:
        return "cp1252", 0, "ASCII-compatible Ami Pro Western default"
    return "utf-8", 0, "valid UTF-8 heuristic"


def _starts_with_supported_bom(data: bytes) -> bool:
    return any(
        data.startswith(bom)
        for bom in (
            codecs.BOM_UTF32_LE,
            codecs.BOM_UTF32_BE,
            codecs.BOM_UTF8,
            codecs.BOM_UTF16_LE,
            codecs.BOM_UTF16_BE,
        )
    )


def _utf8_probe(data: bytes, end: int) -> tuple[bool, bool]:
    """Validate logical bytes as UTF-8 without copying an entire large input."""

    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    ascii_only = True
    cursor = 0
    try:
        while cursor < end:
            chunk_end = min(end, cursor + 1024 * 1024)
            chunk = data[cursor:chunk_end]
            ascii_only = ascii_only and chunk.isascii()
            decoder.decode(chunk, final=False)
            cursor = chunk_end
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False, ascii_only
    return True, ascii_only


def _encoding_text_end(data: bytes) -> int:
    """Return the byte boundary of the outer EDOC close for encoding evidence."""

    text = data.decode("utf-8", errors="surrogateescape")
    byte_position = 0
    edoc_seen = False
    depth = 0
    scanner = MultilineContainerScanner()
    for _start, _end, line in _iter_text_lines(text):
        raw_line = line.encode("utf-8", errors="surrogateescape")
        body = _without_line_ending(line)
        if not edoc_seen:
            edoc_seen = body.strip().lower() == "[edoc]"
            byte_position += len(raw_line)
            continue
        scan = scanner.scan_line(body)
        if scan.standalone_terminator:
            if depth:
                depth -= 1
            else:
                return byte_position + len(raw_line)
        else:
            depth += int(scan.opener is not None)
        byte_position += len(raw_line)
    return len(data)
