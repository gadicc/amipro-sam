"""Byte decoding with conservative legacy-codepage handling."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

from .errors import DecodeError, ResourceLimitError
from .limits import ParseLimits
from .model import Diagnostic, Severity, SourceSpan
from .syntax import MultilineContainerScanner

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


@dataclass(slots=True)
class DecodedSource:
    text: str
    encoding: str
    newline: str
    line_byte_offsets: list[int]
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def span_for_line(self, line_index: int, raw_line: str) -> SourceSpan:
        start = self.line_byte_offsets[line_index]
        encoded = raw_line.encode(self.encoding, errors="surrogateescape")
        return SourceSpan(
            line=line_index + 1,
            column=1,
            byte_offset=start,
            end_byte_offset=start + len(encoded),
        )


def decode_bytes(
    data: bytes,
    *,
    limits: ParseLimits | None = None,
    encoding: str | None = None,
) -> DecodedSource:
    limits = limits or ParseLimits()
    if len(data) > limits.max_file_bytes:
        raise ResourceLimitError(
            f"input is {len(data)} bytes; configured maximum is {limits.max_file_bytes}"
        )

    diagnostics: list[Diagnostic] = []
    selected, bom_length, reason = _select_encoding(data, override=encoding)
    payload = data[bom_length:]
    try:
        text = payload.decode(selected, errors="surrogateescape")
    except LookupError as exc:
        raise DecodeError(f"unknown text encoding: {selected}") from exc

    surrogate_count = sum(1 for char in text if 0xDC80 <= ord(char) <= 0xDCFF)
    if surrogate_count:
        diagnostics.append(
            Diagnostic(
                severity=Severity.WARNING,
                code="decode-undecodable-bytes",
                message=(
                    f"preserved {surrogate_count} undecodable byte(s) as lossless "
                    "surrogate code points"
                ),
            )
        )

    if reason != "explicit override":
        diagnostics.append(
            Diagnostic(
                severity=Severity.INFO,
                code="decode-selected",
                message=f"decoded as {selected} ({reason})",
            )
        )

    # Build locations from decoded lines: ``str.splitlines`` recognizes a few legacy
    # control separators that ``bytes.splitlines`` does not, and SAM binary tails can
    # contain them. Re-encoding with surrogateescape is byte-for-byte reversible.
    text_lines = text.splitlines(keepends=True)
    binary_start, binary_end = _binary_line_window(text_lines)
    textual_line_count = len(text_lines) - max(0, binary_end - binary_start)
    if textual_line_count > limits.max_lines:
        raise ResourceLimitError(
            f"input has more than {limits.max_lines} lines; parsing stopped"
        )
    offsets: list[int] = []
    position = bom_length
    for line_index, line in enumerate(text_lines):
        raw_line = line.encode(selected, errors="surrogateescape")
        is_binary_payload = binary_start <= line_index < binary_end
        if not is_binary_payload and len(raw_line) > limits.max_line_bytes:
            raise ResourceLimitError(
                f"input contains a {len(raw_line)}-byte line; "
                f"configured maximum is {limits.max_line_bytes}"
            )
        offsets.append(position)
        position += len(raw_line)
    if (not text_lines or (payload and not payload.endswith((b"\r", b"\n")))) and not offsets:
        # splitlines includes the final unterminated line; only an empty input needs a slot.
        offsets.append(bom_length)

    newline = "\r\n" if b"\r\n" in payload else "\n" if b"\n" in payload else "\r"
    return DecodedSource(
        text=text,
        encoding=selected,
        newline=newline,
        line_byte_offsets=offsets,
        diagnostics=diagnostics,
    )


def _binary_line_window(lines: list[str]) -> tuple[int, int]:
    """Locate the post-EDOC payload so binary bytes do not count as text lines."""

    normalized = [line.rstrip("\r\n") for line in lines]
    edoc = next(
        (index for index, line in enumerate(normalized) if line.strip().lower() == "[edoc]"),
        None,
    )
    if edoc is None:
        return len(lines), len(lines)

    depth = 0
    terminator: int | None = None
    scanner = MultilineContainerScanner()
    for index in range(edoc + 1, len(normalized)):
        line = normalized[index]
        scan = scanner.scan_line(line)
        if scan.standalone_terminator:
            if depth:
                depth -= 1
                continue
            terminator = index
            break
        depth += int(scan.opener is not None)
    if terminator is None:
        return len(lines), len(lines)

    embedded = next(
        (
            index
            for index in range(len(normalized) - 1, terminator, -1)
            if normalized[index].strip().lower() == "[embedded]"
        ),
        len(lines),
    )
    return terminator + 1, embedded


def _select_encoding(data: bytes, *, override: str | None) -> tuple[str, int, str]:
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

    charset_match = _CHARSET_SECTION.search(data[:64 * 1024])
    if charset_match and (match := _CODEPAGE_TEXT.search(charset_match.group("body"))):
        codepage = int(match.group(1))
        candidate = _CODEPAGE_NAMES.get(codepage, f"cp{codepage}")
        try:
            return codecs.lookup(candidate).name, 0, f"[charset] description names CP {codepage}"
        except LookupError:
            pass

    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "cp1252", 0, "conservative Ami Pro Western default"
    else:
        # ASCII is valid in both; keep the historically likely label for diagnostics.
        if all(byte < 0x80 for byte in data):
            return "cp1252", 0, "ASCII-compatible Ami Pro Western default"
        return "utf-8", 0, "valid UTF-8 heuristic"
