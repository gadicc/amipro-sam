from __future__ import annotations

import codecs
import hashlib
import json
import random
import tracemalloc
from pathlib import Path

import pytest

from amipro_sam.cli import main
from amipro_sam.decoding import decode_bytes
from amipro_sam.errors import AmiProError, PreservationLossError, ResourceLimitError
from amipro_sam.limits import ParseLimits
from amipro_sam.model import Frame, Lossiness, Paragraph, Severity, UnsupportedObject
from amipro_sam.parser import parse_bytes
from amipro_sam.renderers import text as text_renderer


def _sam(body: bytes = b"BODY", *, extra: bytes = b"") -> bytes:
    return (
        b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n"
        + extra
        + b"[edoc]\r\n"
        + body
        + b"\r\n>\r\n"
    )


def _fixed_text_frame() -> bytes:
    return _sam(
        extra=(
            b"[frm]\r\n\t0\r\n\t0\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n\t[txt]\r\nFRAME TEXT\r\n>\r\n"
        )
    )


def _embedded(
    payload: bytes,
    *,
    extension: str = ".bin",
    prefix: bytes | None = None,
    asset_id: int = 1,
    declared_offset: int | None = None,
) -> bytes:
    header = _sam() if prefix is None else prefix
    offset = len(header) if declared_offset is None else declared_offset
    marker_offset = len(header) + len(payload) + 2
    directory = (
        f"[Embedded]\r\n{asset_id} {extension} {offset} {len(payload)} 0 0 \r\n"
        f"{marker_offset:08d}\r\n"
    ).encode("ascii")
    return header + payload + b"\r\n" + directory


def _anchored_bitmap(pixel_byte: int = 0x81) -> bytes:
    header = _sam(
        b"BEFORE<:A0>AFTER",
        extra=(
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n\t[isd]\r\n\t\t.X1\r\n"
        ),
    )
    return _embedded(b"BM" + bytes((pixel_byte,)) + b"\0" * 29, extension=".bmp", prefix=header)


def _utf8_anchored_bitmap(payload: bytes) -> bytes:
    header = _sam(
        "BEFOREπ<:A0>AFTER".encode(),
        extra=(
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n\t[isd]\r\n\t\t.X1\r\n"
        ),
    )
    return _embedded(payload, extension=".bmp", prefix=header)


def _directory_rows(count: int) -> bytes:
    header = _sam()
    payload = b"x"
    marker_offset = len(header) + len(payload) + 2
    rows = b"".join(
        f"{index + 1} .bin {len(header)} 1 0 0 \r\n".encode("ascii")
        for index in range(count)
    )
    return (
        header
        + payload
        + b"\r\n[Embedded]\r\n"
        + rows
        + f"{marker_offset:08d}\r\n".encode("ascii")
    )


def _outcome(source: bytes) -> tuple[object, ...]:
    try:
        document = parse_bytes(source)
    except AmiProError as exc:
        return (type(exc).__name__, str(exc))
    return (
        "ok",
        document.text,
        tuple(
            (item.code, item.severity.value, item.lossiness.value)
            for item in document.diagnostics
        ),
    )


def test_strict_uses_lossiness_instead_of_severity() -> None:
    lossless = parse_bytes(_sam(), strict=True)
    assert lossless.is_lossless
    assert all(item.lossiness is Lossiness.NONE for item in lossless.diagnostics)

    recovered = parse_bytes(_fixed_text_frame())
    reflow = next(
        item for item in recovered.diagnostics if item.code == "unanchored-frame-reflowed"
    )
    assert reflow.severity is Severity.INFO
    assert reflow.lossiness is Lossiness.SEMANTIC
    with pytest.raises(PreservationLossError) as caught:
        parse_bytes(_fixed_text_frame(), strict=True)
    assert caught.value.losses[0].is_lossy


@pytest.mark.parametrize(
    ("source", "diagnostic_code", "record_type", "opaque_value", "object_kind"),
    [
        (
            b"[ver]\r\n\t4\r\n\tInventedVersionTail\r\n"
            b"[sty]\r\n\t\r\n[edoc]\r\nBODY\r\n>\r\n",
            "version-fields-opaque",
            "version-fields-tail",
            "InventedVersionTail",
            "version header fields",
        ),
        (
            b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n\tInventedStylesheetTail\r\n"
            b"[edoc]\r\nBODY\r\n>\r\n",
            "stylesheet-fields-opaque",
            "stylesheet-fields-tail",
            "InventedStylesheetTail",
            "stylesheet header fields",
        ),
    ],
)
def test_recognized_header_tails_are_raw_visible_and_strictly_lossy(
    source: bytes,
    diagnostic_code: str,
    record_type: str,
    opaque_value: str,
    object_kind: str,
) -> None:
    document = parse_bytes(source)
    diagnostic = next(
        item for item in document.diagnostics if item.code == diagnostic_code
    )
    record = next(
        item for item in document.unknown_records if item.record_type == record_type
    )
    rendered = text_renderer.render(document).decode("utf-8")

    assert diagnostic.lossiness is Lossiness.SEMANTIC
    assert opaque_value in record.raw
    assert f"Unsupported {object_kind}" in rendered
    assert rendered.index(f"Unsupported {object_kind}") < rendered.index("BODY")
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_noncanonical_style_top_level_fields_stay_opaque_without_parent_guess() -> None:
    source = _sam(
        b"@Child@BODY",
        extra=(
            b"[tag]\r\nChild\r\n777\r\nParentOne\r\nParentTwo\r\n"
            b"\t[fnt]\r\n\t\tArial\r\n\t\t240\r\n\t\t0\r\n\t\t0\r\n"
        ),
    )
    document = parse_bytes(source)
    diagnostic = next(
        item
        for item in document.diagnostics
        if item.code == "style-top-level-fields-opaque"
    )
    record = next(
        item
        for item in document.unknown_records
        if item.record_type == "style-top-level-fields"
    )
    rendered = text_renderer.render(document).decode("utf-8")

    assert document.styles["Child"].parent is None
    assert document.styles["Child"].following_style is None
    assert "777" in record.raw
    assert "ParentOne" in record.raw
    assert "ParentTwo" in record.raw
    assert diagnostic.lossiness is Lossiness.SEMANTIC
    assert "Unsupported style fields" not in rendered
    assert "BODY" in rendered
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_style_unknown_flag_bits_stay_raw_diagnostic_and_keep_supported_bits() -> None:
    source = _sam(
        b"@Child@BODY",
        extra=(
            b"[tag]\r\nChild\r\n"
            b"\t[fnt]\r\n\t\tArial\r\n\t\t240\r\n\t\t0\r\n\t\t32769\r\n"
            b"\t[algn]\r\n\t\t32769\r\n\t\t0\r\n\t\t0\r\n\t\t0\r\n\t\t0\r\n"
            b"\t[spc]\r\n\t\t32769\r\n\t\t0\r\n\t\t0\r\n\t\t0\r\n\t\t0\r\n"
        ),
    )
    document = parse_bytes(source)
    diagnostic = next(
        item
        for item in document.diagnostics
        if item.code == "style-subrecord-unknown-flags"
    )
    record = next(
        item
        for item in document.unknown_records
        if item.record_type == "style-subrecord-unknown-flags"
    )
    style = document.styles["Child"]
    rendered = text_renderer.render(document).decode("utf-8")

    assert style.character.bold is True
    assert style.alignment == "left"
    assert style.line_spacing == 1.0
    assert "[fnt]" not in record.raw
    assert all(marker in record.raw for marker in ("[algn]", "[spc]"))
    assert diagnostic.lossiness is Lossiness.SEMANTIC
    assert "Unsupported style flag bits" not in rendered
    assert "BODY" in rendered
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_undecodable_text_is_content_loss_but_indexed_binary_is_not() -> None:
    textual = parse_bytes(_sam(b"A\x81B"))
    diagnostic = next(
        item for item in textual.diagnostics if item.code == "decode-undecodable-bytes"
    )
    assert diagnostic.lossiness is Lossiness.CONTENT
    with pytest.raises(PreservationLossError):
        parse_bytes(_sam(b"A\x81B"), strict=True)

    binary = parse_bytes(_anchored_bitmap(), strict=True)
    assert "BEFORE" in binary.text and "AFTER" in binary.text
    assert not any(item.code == "decode-undecodable-bytes" for item in binary.diagnostics)


def test_encoding_evidence_ends_before_indexed_or_unindexed_tail() -> None:
    indexed = parse_bytes(_utf8_anchored_bitmap(b"BM\xff" + b"\0" * 29), strict=True)
    assert indexed.encoding == "utf-8"
    assert "BEFOREπ" in indexed.text

    fake_charset = b"BM\r\n[charset]\r\nCP 1252\r\n" + b"\0" * 8
    declared = parse_bytes(_utf8_anchored_bitmap(fake_charset), strict=True)
    assert declared.encoding == "utf-8"
    assert "BEFOREπ" in declared.text

    recovered = parse_bytes(
        _sam("BEFOREπAFTER".encode())
        + b"UNINDEXED\xff\r\n[charset]\r\nCP 1252\r\n"
    )
    assert recovered.encoding == "utf-8"
    assert "BEFOREπAFTER" in recovered.text
    assert any(item.code == "unindexed-trailing-data" for item in recovered.diagnostics)


@pytest.mark.parametrize("separator", [b"\v", b"\f", b"\x1c", b"\x1d", b"\x1e"])
def test_unindexed_tail_legacy_separators_obey_line_limit(separator: bytes) -> None:
    with pytest.raises(ResourceLimitError, match="more than"):
        parse_bytes(
            _sam() + (b"x" + separator) * 20,
            limits=ParseLimits(max_lines=15, max_line_bytes=1000),
        )


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unindexed_tail_unicode_separators_obey_line_limit(separator: str) -> None:
    with pytest.raises(ResourceLimitError, match="more than"):
        parse_bytes(
            _sam("π".encode()) + (("x" + separator) * 20).encode(),
            limits=ParseLimits(max_lines=15, max_line_bytes=1000),
        )


def test_unindexed_tail_multibyte_text_cannot_bypass_line_byte_limit() -> None:
    with pytest.raises(ResourceLimitError, match="line longer"):
        parse_bytes(
            _sam("π".encode()) + ("Ņ" * 100).encode(),
            limits=ParseLimits(max_line_bytes=32),
        )


@pytest.mark.parametrize(
    ("bom", "encoding", "tail_lengths"),
    [
        (codecs.BOM_UTF16_LE, "utf-16-le", (1,)),
        (codecs.BOM_UTF32_LE, "utf-32-le", (1, 2, 3)),
    ],
)
def test_partial_multibyte_unindexed_tail_is_controlled_and_visible(
    bom: bytes, encoding: str, tail_lengths: tuple[int, ...]
) -> None:
    logical = "[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\nBODY\r\n>\r\n"
    for length in tail_lengths:
        source = bom + logical.encode(encoding) + b"X" * length
        document = parse_bytes(source)
        assert "BODY" in document.text
        assert any(
            item.code == "unindexed-trailing-data"
            for item in document.diagnostics
        )
        with pytest.raises(PreservationLossError):
            parse_bytes(source, strict=True)

    parse_bytes(_sam() + b"A" * 30 + b"\r\n", limits=ParseLimits(max_line_bytes=32))
    with pytest.raises(ResourceLimitError, match="line longer"):
        parse_bytes(
            _sam() + b"A" * 31 + b"\r\n",
            limits=ParseLimits(max_line_bytes=32),
        )


def test_unterminated_edoc_nul_tail_is_preserved_by_bounded_evidence() -> None:
    prefix = (
        b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\n"
        b"BEFORE_SENTINEL\r\n\x00OPAQUE_BOUNDARY\r\n"
    )
    omitted = b"AFTER_SENTINEL\r\nSECOND_OPAQUE_LINE\r\n"
    source = prefix + omitted
    digest = hashlib.sha256(omitted).hexdigest()

    document = parse_bytes(source)
    marker = next(
        block
        for block in document.blocks
        if isinstance(block, UnsupportedObject)
        and block.kind == "unterminated EDOC opaque tail"
    )
    diagnostic = next(
        item
        for item in document.diagnostics
        if item.code == "unterminated-edoc-opaque-tail"
    )
    rendered = text_renderer.render(document).decode("utf-8")

    assert "BEFORE_SENTINEL" in document.text
    assert "\x00OPAQUE_BOUNDARY" in document.text
    assert "AFTER_SENTINEL" not in document.text
    assert str(len(omitted)) in marker.description
    assert digest in marker.description
    assert marker.source is not None
    assert marker.source.byte_offset == len(prefix)
    assert marker.source.end_byte_offset == len(source)
    assert diagnostic.lossiness is Lossiness.CONTENT
    assert digest in rendered
    assert "BEFORE_SENTINEL" in rendered
    with pytest.raises(PreservationLossError) as caught:
        parse_bytes(source, strict=True)
    assert any(
        item.code == "unterminated-edoc-opaque-tail"
        and item.lossiness is Lossiness.CONTENT
        for item in caught.value.losses
    )


def test_literal_embedded_marker_cannot_hide_or_exempt_unindexed_tail() -> None:
    source = _sam() + b"UNINDEXED\r\n[Embedded]\r\nnot a manifest"
    document = parse_bytes(source)
    tail = next(
        item for item in document.diagnostics if item.code == "unindexed-trailing-data"
    )
    assert tail.lossiness is Lossiness.CONTENT
    assert any(
        isinstance(block, UnsupportedObject) and block.kind == "unindexed binary tail"
        for block in document.blocks
    )
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)

    with pytest.raises(ResourceLimitError, match="line longer"):
        parse_bytes(
            _sam() + b"X" * 64 + b"\r\n[Embedded]\r\ninvalid",
            limits=ParseLimits(max_line_bytes=32),
        )


def test_valid_empty_directory_is_lossless_but_damaged_pointer_is_classified() -> None:
    header = _sam()
    valid = header + f"[Embedded]\r\n{len(header):08d}\r\n".encode("ascii")
    document = parse_bytes(valid, strict=True)
    marker = next(item for item in document.diagnostics if item.code == "embedded-directory")
    assert marker.lossiness is Lossiness.NONE
    assert not any(item.code == "unindexed-trailing-data" for item in document.diagnostics)

    payload = b"opaque"
    damaged = (
        header
        + payload
        + b"\r\n[Embedded]\r\n"
        + f"1 .bin {len(header)} {len(payload)} 0 0 \r\n00000000\r\n".encode(
            "ascii"
        )
    )
    recovered = parse_bytes(damaged)
    pointer = next(
        item
        for item in recovered.diagnostics
        if item.code == "embedded-directory-pointer-mismatch"
    )
    assert pointer.lossiness is Lossiness.SEMANTIC


def test_only_declared_binary_ranges_receive_line_limit_exemption() -> None:
    payload = b"x\n" * 128
    source = _embedded(payload)
    decoded = decode_bytes(source, limits=ParseLimits(max_lines=16, max_line_bytes=32))
    assert decoded.binary_ranges

    header = _sam()
    indexed = b"OK"
    gap = b"X" * 64
    marker_offset = len(header) + len(indexed) + len(gap) + 2
    directory = (
        f"[Embedded]\r\n1 .bin {len(header)} {len(indexed)} 0 0 \r\n"
        f"{marker_offset:08d}\r\n"
    ).encode("ascii")
    with pytest.raises(ResourceLimitError, match="line longer"):
        parse_bytes(
            header + indexed + gap + b"\r\n" + directory,
            limits=ParseLimits(max_line_bytes=32),
        )


def test_manifest_ranges_must_be_inside_verified_post_edoc_envelope() -> None:
    extra = (
        b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
        b"\t1440\r\n\t1440\r\n\t[isd]\r\n\t\t.X1\r\n"
    )
    header = _sam(b"BEFORE-BMXX-<:A0>-AFTER", extra=extra)
    body_offset = header.index(b"BMXX")
    marker_offset = len(header)
    source = header + (
        f"[Embedded]\r\n1 .bmp {body_offset} 4 0 0 \r\n"
        f"{marker_offset:08d}\r\n"
    ).encode("ascii")

    document = parse_bytes(source)
    invalid = next(
        item for item in document.diagnostics if item.code == "embedded-offset-invalid"
    )
    assert invalid.lossiness is Lossiness.CONTENT
    assert "BEFORE-BMXX-" in document.text and "-AFTER" in document.text
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_embedded_directory_record_cap_is_lowerable_and_hard_bounded() -> None:
    decode_bytes(_directory_rows(1), limits=ParseLimits(max_embedded_records=1))
    with pytest.raises(ResourceLimitError, match="embedded directory"):
        decode_bytes(_directory_rows(2), limits=ParseLimits(max_embedded_records=1))

    decode_bytes(_directory_rows(4_096))
    with pytest.raises(ResourceLimitError, match="embedded directory"):
        decode_bytes(_directory_rows(4_097))


@pytest.mark.parametrize("junk_line", [b"7\r\n", b"BOGUS\r\n"])
def test_embedded_directory_physical_line_cap_includes_non_rows(
    junk_line: bytes,
) -> None:
    header = _sam()
    pointer = f"{len(header):08d}\r\n".encode("ascii")

    with pytest.raises(ResourceLimitError, match="embedded directory"):
        decode_bytes(
            header + b"[Embedded]\r\n" + junk_line * 2 + pointer,
            limits=ParseLimits(max_embedded_records=1),
        )
    with pytest.raises(ResourceLimitError, match="embedded directory"):
        decode_bytes(header + b"[Embedded]\r\n" + junk_line * 4_097 + pointer)


def test_nonadjacent_manifest_ranges_are_authorized_without_quadratic_lookup() -> None:
    header = _sam()
    count = 256
    payload = b"x!" * count
    marker_offset = len(header) + len(payload) + 2
    rows = b"".join(
        f"{index + 1} .bin {len(header) + index * 2} 1 0 0 \r\n".encode(
            "ascii"
        )
        for index in range(count)
    )
    source = (
        header
        + payload
        + b"\r\n[Embedded]\r\n"
        + rows
        + f"{marker_offset:08d}\r\n".encode("ascii")
    )

    decoded = decode_bytes(source)
    assert len(decoded.binary_ranges) == count
    document = parse_bytes(source)
    assert not any(
        item.code == "embedded-offset-invalid" for item in document.diagnostics
    )


def test_parse_limits_new_field_does_not_shift_existing_positional_fields() -> None:
    existing_values = tuple(range(1, 22))
    limits = ParseLimits(*existing_values)

    assert limits.max_embedded_asset_bytes == existing_values[7]
    assert limits.max_total_sdw_pixels == existing_values[20]
    assert limits.max_embedded_records == 4_096


@pytest.mark.parametrize("section_name", ["l1", "elay"])
def test_unimplemented_structural_sections_are_visible_losses(section_name: str) -> None:
    document = parse_bytes(_sam(extra=f"[{section_name}]\r\n\topaque\r\n".encode()))

    assert any(item.code == "unknown-section" for item in document.diagnostics)
    assert any(
        isinstance(block, UnsupportedObject) and section_name in block.description
        for block in document.blocks
    )
    with pytest.raises(PreservationLossError):
        parse_bytes(
            _sam(extra=f"[{section_name}]\r\n\topaque\r\n".encode()), strict=True
        )


def test_revisions_and_style_subrecords_are_classified() -> None:
    revisions = parse_bytes(_sam(extra=b"[revisions]\r\n\topaque-state\r\n"))
    assert any(item.code == "revisions-opaque" for item in revisions.diagnostics)
    assert any(
        isinstance(block, UnsupportedObject) and block.kind == "revision state"
        for block in revisions.blocks
    )

    style_source = _sam(
        b"@Invented@BODY",
        extra=(
            b"[tag]\r\nInvented\r\n\t[brk]\r\n\t\t1\r\n"
            b"\t[fnt]\r\n\t\tArial\r\n"
            b"\t[fnt]\r\n\t\tArial\r\n\t\t999999999999999999999\r\n"
        ),
    )
    styled = parse_bytes(style_source)
    assert any(item.code == "style-subrecords-opaque" for item in styled.diagnostics)
    assert any(item.code == "style-subrecords-malformed" for item in styled.diagnostics)
    with pytest.raises(PreservationLossError):
        parse_bytes(style_source, strict=True)


@pytest.mark.parametrize(
    "tag",
    [
        ":f" + "9" * 5_000,
        ":S+" + "9" * 500,
        ":#" + "9" * 5_000,
        ":I" + "9" * 5_000 + ",0,0",
    ],
)
def test_huge_inline_numeric_fields_are_classified_without_crashing(tag: str) -> None:
    source = _sam(f"BEFORE<{tag}>AFTER".encode())
    document = parse_bytes(source)

    assert "BEFORE" in document.text and "AFTER" in document.text
    assert "Unsupported inline command" in document.text
    assert any(item.code == "unsupported-inline-tags" for item in document.diagnostics)
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_table_definition_and_cell_header_tails_are_coalesced_and_visible() -> None:
    source = _sam(
        b"BEFORE<:t0>AFTER",
        extra=(
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n"
            b"\t[tbl]\r\n\t\t1 2 777 888\r\n"
            b"\t[data]\r\n"
            b"\t\t\t0 0 16384 17 23\r\nLEFT\r\n>\r\n"
            b"\t\t\t0 1 32768 41 59\r\nRIGHT\r\n>\r\n"
            b"\t\t[tble]\r\n"
        ),
    )
    document = parse_bytes(source)
    diagnostic = next(
        item for item in document.diagnostics if item.code == "table-fields-opaque"
    )
    records = [
        item for item in document.unknown_records if item.record_type == "table-fields"
    ]
    rendered = text_renderer.render(document).decode("utf-8")

    assert len(records) == 1
    assert "1 2 777 888" in records[0].raw
    assert "16384 17 23" in records[0].raw
    assert "32768 41 59" in records[0].raw
    assert diagnostic.lossiness is Lossiness.SEMANTIC
    assert "Unsupported table fields" in rendered
    assert "LEFT" in rendered and "RIGHT" in rendered
    assert rendered.index("Unsupported table fields") < rendered.index("LEFT")
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_coalesced_table_field_record_has_a_hard_text_bound() -> None:
    source = _sam(
        b"BEFORE<:t0>AFTER",
        extra=(
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n\t[tbl]\r\n\t\t1 1 "
            + b"7" * 20_000
            + b"\r\n\t[data]\r\n\t\t\t0 0 0\r\nCELL\r\n>\r\n"
            b"\t\t[tble]\r\n"
        ),
    )
    document = parse_bytes(source)
    record = next(
        item for item in document.unknown_records if item.record_type == "table-fields"
    )

    assert len(record.raw) <= 16_384
    assert "summary truncated" in record.raw
    assert "CELL" in document.text


@pytest.mark.parametrize(
    "tag",
    [
        ":pMYSTERY",
        ":f240,Arial,1,2,3,OPAQUE",
        ":f240,Arial,1",
        ":I1440,OPAQUE,720,TAIL",
        ":#1440,720GARBAGE",
    ],
)
def test_recognized_inline_prefixes_with_opaque_fields_remain_visible(
    tag: str,
) -> None:
    source = _sam(f"BEFORE<{tag}>AFTER".encode())
    document = parse_bytes(source)

    assert "BEFORE" in document.text and "AFTER" in document.text
    assert "Unsupported inline command" in document.text
    assert any(item.code == "unsupported-inline-tags" for item in document.diagnostics)
    assert any(tag[:200] in item.raw for item in document.unknown_records)
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_plain_page_break_command_remains_a_supported_break() -> None:
    document = parse_bytes(_sam(b"BEFORE\r\n\r\n<:p>AFTER"), strict=True)

    assert "BEFORE" in document.text and "AFTER" in document.text
    assert not any(item.code == "unsupported-inline-tags" for item in document.diagnostics)
    after = next(
        block
        for block in document.blocks
        if isinstance(block, Paragraph) and "AFTER" in block.text
    )
    assert after.page_break_before is True


def test_frame_anchors_charge_the_content_record_limit() -> None:
    exact = parse_bytes(
        _sam(b"<:t0>" * 10),
        limits=ParseLimits(max_records=10),
    )
    assert sum(
        isinstance(block, UnsupportedObject) and block.kind == "missing frame anchor"
        for block in exact.blocks
    ) == 10

    with pytest.raises(ResourceLimitError, match="content records"):
        parse_bytes(
            _sam(b"<:t0>" * 11),
            limits=ParseLimits(max_records=10),
        )


@pytest.mark.parametrize("kind", ["frame", "table"])
def test_frame_and_table_paragraphs_charge_materialization_budget(
    kind: str,
) -> None:
    paragraphs = b"X\r\n\r\n" * 100
    if kind == "frame":
        extra = (
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n\t[txt]\r\n"
            + paragraphs
            + b">\r\n"
        )
        source = _sam(b"BEFORE<:A0>AFTER", extra=extra)
    else:
        extra = (
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n\t[tbl]\r\n\t\t1 1 0 0\r\n"
            b"\t[data]\r\n\t\t\t0 0 0 0 0\r\n"
            + paragraphs
            + b">\r\n\t\t[tble]\r\n"
        )
        source = _sam(b"BEFORE<:t0>AFTER", extra=extra)

    description = "frame text parsing" if kind == "frame" else "table cell text parsing"
    with pytest.raises(ResourceLimitError, match=description):
        parse_bytes(source, limits=ParseLimits(max_records=10))


def test_opaque_geometry_tails_have_strict_semantic_classification() -> None:
    layout = _sam(
        extra=(
            b"[lay]\r\nStandard\r\n1\r\n\t[rght]\r\n"
            b"\t\t15840\r\n\t\t12240\r\n\t\t0\r\n\t\t1440\r\n"
            b"\t\t1440\r\n\t\t1\r\n\t\t1440\r\n\t\t1440\r\n"
            b"\t\t0\r\n\t\t777\r\n"
        )
    )
    layout_document = parse_bytes(layout)
    assert any(
        item.code == "page-geometry-tail-opaque"
        and item.lossiness is Lossiness.SEMANTIC
        for item in layout_document.diagnostics
    )

    frame = _sam(
        b"BEFORE<:A0>AFTER",
        extra=(
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n\t777\r\n"
            b"\t[frmlay]\r\n\t\t0\r\n\t[txt]\r\nFRAME\r\n>\r\n"
        ),
    )
    frame_document = parse_bytes(frame)
    assert any(
        item.code == "frame-layout-fields-opaque"
        and item.lossiness is Lossiness.SEMANTIC
        for item in frame_document.diagnostics
    )
    with pytest.raises(PreservationLossError):
        parse_bytes(layout, strict=True)
    with pytest.raises(PreservationLossError):
        parse_bytes(frame, strict=True)


def test_unknown_layout_subrecord_is_raw_visible_and_strictly_lossy() -> None:
    source = _sam(
        extra=(
            b"[lay]\r\nStandard\r\n1\r\n"
            b"\t[rght]\r\n"
            b"\t\t15840\r\n\t\t12240\r\n\t\t0\r\n\t\t1440\r\n"
            b"\t\t1440\r\n\t\t1\r\n\t\t1440\r\n\t\t1440\r\n"
            b"\t\t0\r\n"
            b"\t[invented]\r\n\t\topaque layout value\r\n"
        )
    )
    document = parse_bytes(source)

    diagnostic = next(
        item for item in document.diagnostics if item.code == "layout-subrecord-opaque"
    )
    record = next(
        item
        for item in document.unknown_records
        if item.record_type == "unsupported-layout-subrecord"
    )
    assert diagnostic.lossiness is Lossiness.SEMANTIC
    assert "[invented]" in record.raw and "opaque layout value" in record.raw
    assert record.source.byte_offset >= 0
    assert any(
        isinstance(block, UnsupportedObject)
        and block.kind == "unsupported layout subrecord"
        and "[invented]" in block.description
        for block in document.blocks
    )
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_unknown_frame_subrecord_is_raw_visible_and_strictly_lossy() -> None:
    source = _sam(
        b"BEFORE<:A0>AFTER",
        extra=(
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n"
            b"\t[txt]\r\nFRAME TEXT\r\n>\r\n"
            b"\t[invented]\r\n\t\topaque frame value\r\n"
        ),
    )
    document = parse_bytes(source)
    frame = next(block for block in document.blocks if isinstance(block, Frame))

    diagnostic = next(
        item for item in document.diagnostics if item.code == "frame-subrecord-opaque"
    )
    record = next(
        item
        for item in document.unknown_records
        if item.record_type == "unsupported-frame-subrecord"
    )
    assert diagnostic.lossiness is Lossiness.SEMANTIC
    assert "[invented]" in record.raw and "opaque frame value" in record.raw
    assert record.source.byte_offset >= 0
    assert any(
        isinstance(block, UnsupportedObject)
        and block.kind == "unsupported frame subrecord"
        and "[invented]" in block.description
        for block in frame.blocks
    )
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_frame_text_requires_a_standalone_container_aware_terminator() -> None:
    source = _sam(
        b"BEFORE<:A0>AFTER",
        extra=(
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n"
            b"\t[txt]\r\nFRAME\r\n>trailing\r\n"
            b"\t[invented]\r\n\t\topaque\r\n"
        ),
    )
    document = parse_bytes(source)

    assert "FRAME\n>trailing\n[invented]\nopaque" in document.text
    diagnostic = next(
        item for item in document.diagnostics if item.code == "unterminated-frame-text"
    )
    assert diagnostic.lossiness is Lossiness.SEMANTIC
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_duplicate_table_coordinates_preserve_both_values_and_are_lossy() -> None:
    source = _sam(
        b"BEFORE<:A0>AFTER",
        extra=(
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n\t[tbl]\r\n\t\t1 1 0 0\r\n"
            b"\t[data]\r\n"
            b"\t\t\t0 0 0 0 0\r\nFIRST\r\n>\r\n"
            b"\t\t\t0 0 0 0 0\r\nSECOND\r\n>\r\n\t\t[tble]\r\n"
        ),
    )
    document = parse_bytes(source)

    assert "FIRST" in document.text and "SECOND" in document.text
    assert "Duplicate table cell coordinate" in document.text
    diagnostic = next(
        item
        for item in document.diagnostics
        if item.code == "duplicate-table-cell-coordinate"
    )
    assert diagnostic.lossiness is Lossiness.SEMANTIC
    assert any(
        item.record_type == "duplicate-table-cell-coordinate"
        and "SECOND" in item.raw
        for item in document.unknown_records
    )
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


@pytest.mark.parametrize(
    ("cell_text", "visible", "diagnostic_code"),
    [
        (
            b"CELL_BEFORE<:mystery>CELL_AFTER",
            "Unsupported inline command",
            "unsupported-inline-tags",
        ),
        (b"CELL_A<CELL_B", "CELL_A<CELL_B", "unterminated-inline-tag"),
    ],
)
def test_table_cells_preserve_unknown_or_unterminated_inline_syntax(
    cell_text: bytes, visible: str, diagnostic_code: str
) -> None:
    source = _sam(
        b"BEFORE<:A0>AFTER",
        extra=(
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n\t[tbl]\r\n\t\t1 1 0 0\r\n"
            b"\t[data]\r\n\t\t\t0 0 0 0 0\r\n"
            + cell_text
            + b"\r\n>\r\n\t\t[tble]\r\n"
        ),
    )
    document = parse_bytes(source)

    assert visible in document.text
    assert any(item.code == diagnostic_code for item in document.diagnostics)
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_repeated_inline_commands_are_bounded_and_visibly_classified() -> None:
    source = _sam((b"a<+!>b<-!>" * 10_000) + b"TAIL")
    document = parse_bytes(source)

    assert "TAIL" in document.text
    assert "Additional inline commands omitted at safe parsing limit" in document.text
    diagnostic = next(
        item for item in document.diagnostics if item.code == "inline-command-limit"
    )
    assert diagnostic.lossiness is Lossiness.SEMANTIC
    paragraph = next(block for block in document.blocks if isinstance(block, Paragraph))
    assert len(paragraph.runs) <= 4_096
    assert len(document.text) < len(source) * 4
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_inline_runs_share_the_lowerable_document_materialization_budget() -> None:
    paragraph = b"a<+!>b<-!>" * 8
    source = _sam(b"\r\n\r\n".join([paragraph] * 20))

    normal = parse_bytes(_sam(b"a<+!>b<-!>"), limits=ParseLimits(max_records=3))
    assert normal.text == "ab"
    with pytest.raises(ResourceLimitError, match="inline runs.*100 materialized records"):
        parse_bytes(source, limits=ParseLimits(max_records=100))


def test_repeated_unterminated_angles_and_undefined_styles_are_coalesced() -> None:
    unterminated = parse_bytes(_sam(b"<a" * 10_000))
    assert unterminated.text.count("<a") == 10_000
    assert sum(
        item.code == "unterminated-inline-tag" for item in unterminated.diagnostics
    ) == 1

    styles = parse_bytes(_sam((b"@missing@a" * 10_000) + b"TAIL"))
    assert "TAIL" in styles.text
    assert sum(item.code == "undefined-style" for item in styles.diagnostics) == 1
    assert sum(item.code == "inline-command-limit" for item in styles.diagnostics) == 1


def test_marker_lookalikes_inside_layout_and_frame_text_are_not_subrecords() -> None:
    layout = _sam(
        extra=(
            b"[lay]\r\nStandard\r\n1\r\n"
            b"\t[hrght]\r\n\t[txt]\r\n\t[invented]\r\n\t>\r\n"
            b"\t[rght]\r\n"
            b"\t\t15840\r\n\t\t12240\r\n\t\t0\r\n\t\t1440\r\n"
            b"\t\t1440\r\n\t\t1\r\n\t\t1440\r\n\t\t1440\r\n"
            b"\t\t0\r\n"
        )
    )
    layout_document = parse_bytes(layout)
    assert "[invented]" in layout_document.text
    assert not any(
        item.code == "layout-subrecord-opaque"
        for item in layout_document.diagnostics
    )

    frame = _sam(
        b"BEFORE<:A0>AFTER",
        extra=(
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n"
            b"\t[txt]\r\nBEFORE\r\n\t[invented]\r\nAFTER\r\n>\r\n"
        ),
    )
    frame_document = parse_bytes(frame)
    assert "BEFORE\n[invented]\nAFTER" in frame_document.text
    assert not any(
        item.code == "frame-subrecord-opaque" for item in frame_document.diagnostics
    )

    table = _sam(
        b"BEFORE<:t0>AFTER",
        extra=(
            b"[frm]\r\n\t0\r\n\t524288\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n"
            b"\t[tbl]\r\n\t\t 1 1 0 0\r\n"
            b"\t[data]\r\n\t\t\t 0 0 0 0 0\r\n"
            b"BEFORE\r\n\t[invented]\r\nAFTER\r\n\t\t[e]\r\n"
        ),
    )
    table_document = parse_bytes(table)
    assert "BEFORE\n[invented]\nAFTER" in table_document.text
    assert not any(
        item.code == "frame-subrecord-opaque" for item in table_document.diagnostics
    )


def test_page_layout_prefix_tail_is_preserved_and_strictly_lossy() -> None:
    source = _sam(
        extra=(
            b"[lay]\r\nStandard\r\n1\r\n777\r\n"
            b"\t[rght]\r\n"
            b"\t\t15840\r\n\t\t12240\r\n\t\t0\r\n\t\t1440\r\n"
            b"\t\t1440\r\n\t\t1\r\n\t\t1440\r\n\t\t1440\r\n"
            b"\t\t0\r\n"
        )
    )
    document = parse_bytes(source)
    diagnostic = next(
        item for item in document.diagnostics if item.code == "page-layout-fields-opaque"
    )
    record = next(
        item
        for item in document.unknown_records
        if item.record_type == "page-layout-fields"
    )

    assert diagnostic.lossiness is Lossiness.SEMANTIC
    assert record.raw == "777"
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


@pytest.mark.parametrize(
    "subrecord",
    [
        b"\t[fnt]\r\n\t\tArial\r\n\t\t240\r\n\t\t0\r\n\t\t0\r\n\t\t777\r\n",
        b"\t[algn]\r\n\t\t1\r\n\t\t0\r\n\t\t0\r\n\t\t0\r\n\t\t0\r\n\t\t777\r\n",
        b"\t[spc]\r\n\t\t1\r\n\t\t0\r\n\t\t0\r\n\t\t0\r\n\t\t0\r\n\t\t777\r\n",
    ],
)
def test_supported_style_subrecord_tails_are_preserved_and_strictly_lossy(
    subrecord: bytes,
) -> None:
    source = _sam(extra=b"[tag]\r\nInvented\r\n" + subrecord)
    document = parse_bytes(source)
    diagnostic = next(
        item
        for item in document.diagnostics
        if item.code == "style-subrecord-fields-opaque"
    )
    record = next(
        item
        for item in document.unknown_records
        if item.record_type == "style-subrecord-tail"
    )

    assert diagnostic.lossiness is Lossiness.SEMANTIC
    assert "777" in record.raw
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_footnote_options_tail_is_preserved_and_strictly_lossy() -> None:
    source = _sam(extra=b"[fopts]\r\n0\r\n1\r\n0\r\n0\r\n777\r\n")
    document = parse_bytes(source)
    diagnostic = next(
        item
        for item in document.diagnostics
        if item.code == "footnote-options-tail-opaque"
    )
    record = next(
        item
        for item in document.unknown_records
        if item.record_type == "footnote-options-tail"
    )

    assert diagnostic.lossiness is Lossiness.SEMANTIC
    assert record.raw == "777"
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_newline_dense_indexed_payload_has_bounded_peak_memory() -> None:
    # Warm imports and codecs outside the measurement.
    parse_bytes(_embedded(b"x\n" * 8), limits=ParseLimits(max_lines=16))
    payload = b"x\n" * (512 * 1024 // 2)
    source = _embedded(payload)

    tracemalloc.start()
    document = parse_bytes(source, limits=ParseLimits(max_lines=16))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert "BODY" in document.text
    assert peak < len(source) * 8


@pytest.mark.parametrize(
    ("source", "expected_code"),
    [
        (_embedded(b"opaque"), "embedded-format-unsupported"),
        (
            _sam(
                extra=(
                    b"[frm]\r\n\t0\r\n\t0\r\n\t0\r\n\t0\r\n"
                    b"\t1440\r\n\t1440\r\n"
                )
            ),
            "drawing-frame-unsupported",
        ),
        (
            _sam(
                extra=(
                    b"[frm]\r\n\t0\r\n\t0\r\n\t0\r\n\t0\r\n"
                    b"\t1440\r\n\t1440\r\n\t[isd]\r\n\t\t.X9\r\n"
                )
            ),
            "frame-image-unavailable",
        ),
        (_sam(extra=b"[mystery]\r\n\topaque value\r\n"), "unknown-section"),
    ],
)
def test_every_parser_placeholder_has_a_classified_loss(
    source: bytes, expected_code: str
) -> None:
    document = parse_bytes(source)
    assert any(isinstance(block, UnsupportedObject) for block in document.blocks)
    diagnostic = next(item for item in document.diagnostics if item.code == expected_code)
    assert diagnostic.lossiness is not Lossiness.NONE
    assert document.is_lossy
    with pytest.raises(PreservationLossError):
        parse_bytes(source, strict=True)


def test_seeded_parser_mutations_are_controlled_and_repeatable() -> None:
    base = _sam(
        b"BEFORE\r\n<:N123\r\nNOTE\r\n>\r\nAFTER",
        extra=(
            b"[frm]\r\n\t0\r\n\t0\r\n\t0\r\n\t0\r\n"
            b"\t1440\r\n\t1440\r\n\t[tbl]\r\n\t\t 1 1 0 0\r\n"
            b"\t[data]\r\n\t\t\t 0 0 0 0 0\r\nCELL\r\n\t\t[e]\r\n"
        ),
    )
    explicit_cases = [
        base.replace(b"[sty]", b"[sty", 1),
        base.replace(b"<:N123", b"<:N" + b"9" * 64, 1),
        _embedded(b"opaque", declared_offset=10**19),
        _embedded(b"opaque")
        .replace(b"1 .bin", b"1 .bin", 1)
        .replace(b"\r\n000", b"\r\n1 .bin 0 1 0 0 \r\n000", 1),
        base.replace(b"\t\t\t 0 0", b"\t\t\t " + b"9" * 24 + b" 0", 1),
        base.replace(b"\t1440", b"\t" + b"9" * 24, 1),
        _embedded(b"ZZ" + b"\xff" * 16, extension=".bmp"),
    ]
    rng = random.Random(20260814)
    random_cases: list[bytes] = []
    for _ in range(64):
        mutated = bytearray(base)
        for _ in range(rng.randint(1, 4)):
            index = rng.randrange(len(mutated))
            mutated[index] = rng.randrange(32, 127)
        random_cases.append(bytes(mutated))

    for source in explicit_cases + random_cases:
        assert _outcome(source) == _outcome(source)


def test_inspect_reports_loss_categories_and_strict_cli_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "lossy.sam"
    source.write_bytes(_fixed_text_frame())

    assert main(["inspect", str(source), "--summary", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["successful"] == 1
    assert summary["lossy_files"] == 1
    assert summary["losses"]["semantic"] >= 1

    output = tmp_path / "strict.txt"
    assert main(
        [
            "convert",
            str(source),
            "--format",
            "text",
            "--strict",
            "--output",
            str(output),
        ]
    ) == 1
    assert not output.exists()
