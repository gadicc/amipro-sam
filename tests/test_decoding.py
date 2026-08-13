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
    source = (
        b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\nbody\r\n>\r\n"
        + payload
        + b"\r\n[Embedded]\r\n00000000\r\n"
    )

    decoded = decode_bytes(source, limits=ParseLimits(max_line_bytes=32))

    assert "[edoc]" in decoded.text


def test_text_line_limit_still_applies_before_edoc_close() -> None:
    source = b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\n" + b"x" * 64 + b"\r\n>\r\n"

    with pytest.raises(ResourceLimitError, match="byte line"):
        decode_bytes(source, limits=ParseLimits(max_line_bytes=32))
