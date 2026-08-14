from __future__ import annotations

import codecs

import pytest

from amipro_sam.decoding import decode_bytes
from amipro_sam.errors import ResourceLimitError
from amipro_sam.limits import ParseLimits


def test_charset_description_selects_cp1252() -> None:
    source = b"[ver]\r\n\t4\r\n[charset]\r\n\t82\r\n\tANSI (Windows, IBM CP 1252)\r\n\xe9"
    decoded = decode_bytes(source)
    assert decoded.encoding == "cp1252"
    assert decoded.text.endswith("é")


def test_bom_wins_over_legacy_default() -> None:
    source = codecs.BOM_UTF8 + "[ver]\n\t4\n[sty]\n\t\n[edoc]\nSnowman ☃".encode()
    decoded = decode_bytes(source)
    assert decoded.encoding == "utf-8"
    assert "☃" in decoded.text


@pytest.mark.parametrize(
    ("encoding", "bom", "payload_encoding"),
    [
        ("utf-8-sig", codecs.BOM_UTF8, "utf-8"),
        ("utf-16", codecs.BOM_UTF16_LE, "utf-16-le"),
        ("utf-32", codecs.BOM_UTF32_LE, "utf-32-le"),
    ],
)
def test_explicit_bom_codecs_keep_exact_line_offsets(
    encoding: str, bom: bytes, payload_encoding: str
) -> None:
    content = "[ver]\n\t4\n[sty]\n"
    source = bom + content.encode(payload_encoding)
    decoded = decode_bytes(source, encoding=encoding)

    expected: list[int] = []
    cursor = len(bom)
    for line in content.splitlines(keepends=True):
        expected.append(cursor)
        cursor += len(line.encode(payload_encoding))
    assert decoded.line_byte_offsets == expected


def test_undefined_cp1252_byte_is_preserved() -> None:
    source = b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\nA\x81B"
    decoded = decode_bytes(source)
    assert "\udc81" in decoded.text
    assert any(item.code == "decode-undecodable-bytes" for item in decoded.diagnostics)


def test_resource_limits_apply_before_decoding() -> None:
    with pytest.raises(ResourceLimitError):
        decode_bytes(b"[ver]" * 5, limits=ParseLimits(max_file_bytes=8))


def test_binary_payload_line_uses_asset_limit_not_text_line_limit() -> None:
    payload = b"BM" + b"\0" * 128
    prefix = b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\nbody\r\n>\r\n"
    marker_offset = len(prefix) + len(payload) + 2
    directory = (
        f"[Embedded]\r\n1 .bmp {len(prefix)} {len(payload)} 0 0 \r\n"
        f"{marker_offset:08d}\r\n"
    ).encode("ascii")
    source = prefix + payload + b"\r\n" + directory

    decoded = decode_bytes(source, limits=ParseLimits(max_line_bytes=32))

    assert "[edoc]" in decoded.text


def test_indexed_span_cannot_fragment_an_oversized_unindexed_line() -> None:
    prefix = b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\nbody\r\n>\r\n"
    payload = b"A" * 60 + b"x" + b"B" * 60 + b"\r\n"
    asset_offset = len(prefix) + 60
    marker_offset = len(prefix) + len(payload)
    directory = (
        f"[Embedded]\r\n1 .bin {asset_offset} 1 0 0 \r\n"
        f"{marker_offset:08d}\r\n"
    ).encode("ascii")

    with pytest.raises(ResourceLimitError, match="line longer"):
        decode_bytes(
            prefix + payload + directory,
            limits=ParseLimits(max_line_bytes=64),
        )


def test_text_line_limit_still_applies_before_edoc_close() -> None:
    source = b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\n" + b"x" * 64 + b"\r\n>\r\n"

    with pytest.raises(ResourceLimitError, match="byte line"):
        decode_bytes(source, limits=ParseLimits(max_line_bytes=32))


def test_undecodable_utf8_tail_uses_source_bytes_for_line_limit() -> None:
    prefix = "[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\nπ\r\n>\r\n".encode()
    tail = b"\xff" * 8 + b"\r\n"
    marker_offset = len(prefix) + len(tail)
    directory = f"[Embedded]\r\n{marker_offset:08d}\r\n".encode()

    decoded = decode_bytes(
        prefix + tail + directory,
        limits=ParseLimits(max_line_bytes=16),
    )

    assert decoded.encoding == "utf-8"
    assert decoded.unindexed_ranges == ((len(prefix), marker_offset),)


@pytest.mark.parametrize(
    ("bom", "encoding", "partial"),
    [
        (codecs.BOM_UTF16_LE, "utf-16-le", b"X"),
        (codecs.BOM_UTF32_LE, "utf-32-le", b"XYZ"),
    ],
)
def test_partial_multibyte_line_uses_exact_source_length_and_span(
    bom: bytes, encoding: str, partial: bytes
) -> None:
    source = bom + partial

    decoded = decode_bytes(
        source,
        limits=ParseLimits(max_line_bytes=len(partial)),
    )

    assert decoded.encoding == encoding
    assert decoded.line_byte_offsets == [len(bom)]
    assert decoded.span_for_line(0, decoded.text).end_byte_offset == len(source)


@pytest.mark.parametrize(
    ("bom", "encoding", "asset"),
    [
        (codecs.BOM_UTF16_LE, "utf-16-le", b"X"),
        (codecs.BOM_UTF32_LE, "utf-32-le", b"XYZ"),
    ],
)
def test_multibyte_directory_restarts_after_odd_length_binary(
    bom: bytes, encoding: str, asset: bytes
) -> None:
    logical = "[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\nBODY\r\n>\r\n"
    prefix = bom + logical.encode(encoding)
    separator = "\r\n".encode(encoding)
    marker_offset = len(prefix) + len(asset) + len(separator)
    base_offset = len(bom)
    directory = (
        f"[Embedded]\r\n"
        f"1 .bin {len(prefix) - base_offset} {len(asset)} 0 0 \r\n"
        f"{marker_offset - base_offset:08d}\r\n"
    ).encode(encoding)

    decoded = decode_bytes(prefix + asset + separator + directory)

    assert decoded.directory_byte_offset == marker_offset
    assert decoded.directory_pointer_valid is True
    assert decoded.binary_ranges == ((len(prefix), len(prefix) + len(asset)),)


def test_multibyte_binary_gap_cannot_fragment_an_oversized_text_line() -> None:
    encoding = "utf-16-le"
    bom = codecs.BOM_UTF16_LE
    logical = "[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\nBODY\r\n>\r\n"
    prefix = bom + logical.encode(encoding)
    before = ("A" * 80).encode(encoding)
    asset = b"x"
    after = (("B" * 80) + "\r\n").encode(encoding)
    marker_offset = len(prefix) + len(before) + len(asset) + len(after)
    base_offset = len(bom)
    directory = (
        f"[Embedded]\r\n"
        f"1 .bin {len(prefix) + len(before) - base_offset} 1 0 0 \r\n"
        f"{marker_offset - base_offset:08d}\r\n"
    ).encode(encoding)

    with pytest.raises(ResourceLimitError, match="line longer"):
        decode_bytes(
            prefix + before + asset + after + directory,
            limits=ParseLimits(max_line_bytes=128),
        )


def test_trailing_false_marker_does_not_hide_valid_directory() -> None:
    prefix = b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\nBODY\r\n>\r\n"
    asset = b"opaque"
    marker_offset = len(prefix) + len(asset) + 2
    directory = (
        f"[Embedded]\r\n1 .bin {len(prefix)} {len(asset)} 0 0 \r\n"
        f"{marker_offset:08d}\r\n"
    ).encode()
    false_marker = b"[Embedded]\r\nnot a manifest"

    decoded = decode_bytes(
        prefix + asset + b"\r\n" + directory + false_marker
    )

    assert decoded.directory_byte_offset == marker_offset
    assert decoded.directory_pointer_valid is False
    assert decoded.binary_ranges == ((len(prefix), len(prefix) + len(asset)),)
