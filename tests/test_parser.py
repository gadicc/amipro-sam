from __future__ import annotations

import random
from pathlib import Path

import pytest

from amipro_sam.errors import ParseError, ResourceLimitError
from amipro_sam.limits import ParseLimits
from amipro_sam.model import Frame, Image, Paragraph, Table, UnsupportedObject
from amipro_sam.parser import parse_bytes, parse_file

STYLE = """[tag]
\tBody Text
\t2
\t[fnt]
\t\tTimes New Roman
\t\t240
\t\t0
\t\t49152
\t[algn]
\t\t1
\t\t1
\t\t0
\t\t0
\t\t0
\t[spc]
\t\t1
\t\t240
\t\t1
\t\t0
\t\t0
\tBody Text
\t0
\t0
"""

FIXTURES = Path(__file__).parent / "fixtures"


def sam(body: str, *, extra: str = "") -> bytes:
    text = (
        "[ver]\n\t4\n[sty]\n\t\n[files]\n[charset]\n\t82\n"
        "\tANSI (Windows, IBM CP 1252)\n"
        + STYLE
        + extra
        + "[edoc]\n"
        + body
        + "\n>\n"
    )
    return text.replace("\n", "\r\n").encode("cp1252")


def test_synthetic_fixture_end_to_end() -> None:
    document = parse_file(FIXTURES / "synthetic-basic.sam")
    paragraphs = [block for block in document.blocks if isinstance(block, Paragraph)]

    assert [paragraph.text for paragraph in paragraphs] == [
        "Synthetic preservation sample",
        "Plain text with bold, italic, underline, and literal <markup>.",
        "Second paragraph with trailing text.",
    ]
    assert any(run.text == "bold" and run.style.bold for run in paragraphs[1].runs)
    assert any(run.text == "italic" and run.style.italic for run in paragraphs[1].runs)


def test_inline_formatting_and_escapes() -> None:
    document = parse_bytes(
        sam("@Body Text@Plain <+!>bold<-!> <+\">italic<-\"> <<tag<;> @@")
    )
    paragraph = next(block for block in document.blocks if isinstance(block, Paragraph))
    assert paragraph.text == "Plain bold italic <tag> @"
    bold = next(run for run in paragraph.runs if run.text == "bold")
    italic = next(run for run in paragraph.runs if run.text == "italic")
    assert bold.style.bold is True
    assert italic.style.italic is True
    assert document.version == "4"
    assert document.encoding == "cp1252"


def test_nonblank_physical_lines_are_paragraph_continuations() -> None:
    document = parse_bytes(
        sam("@Body Text@hel\nlo with \nspace\n\nnext paragraph")
    )
    paragraphs = [block for block in document.blocks if isinstance(block, Paragraph)]

    assert [paragraph.text for paragraph in paragraphs] == [
        "hello with space",
        "next paragraph",
    ]


def test_physical_line_continuations_apply_inside_frame_text() -> None:
    extra = """[frm]
\t1
\t[txt]
hel
lo
>
"""
    document = parse_bytes(sam("body", extra=extra))
    frame = next(block for block in document.blocks if isinstance(block, Frame))
    paragraph = next(block for block in frame.blocks if isinstance(block, Paragraph))

    assert paragraph.text == "hello"


def test_physical_line_continuations_apply_inside_table_cells() -> None:
    extra = """[frm]
\t3
\t[tbl]
\t\t 1 1 0 0
\t[data]
\t\t\t 0 0 0 0 0
hel
lo
\t\t[e]
"""
    document = parse_bytes(sam("body", extra=extra))
    frame = next(block for block in document.blocks if isinstance(block, Frame))
    table = next(block for block in frame.blocks if isinstance(block, Table))

    assert table.rows[0].cells[0].text == "hello"


def test_html_like_source_is_only_text_in_ir() -> None:
    document = parse_bytes(sam("Hello <[>script<;>alert(1)<< /script<;>"))
    assert "[script>alert(1)< /script>" in document.text


def test_unknown_inline_tag_keeps_surrounding_text_and_diagnostic() -> None:
    document = parse_bytes(sam("before<:mystery>after"))
    assert "before[Unsupported inline command: <:mystery>]after" in document.text
    assert any(item.record_type == "inline-tag" for item in document.unknown_records)
    assert any(item.code == "unsupported-inline-tags" for item in document.diagnostics)


def test_dynamic_field_is_inert_and_uses_fallback() -> None:
    document = parse_bytes(
        sam('<:X3,-32768;if Defined x x else "Recipient Name" endif><:X~3>')
    )
    assert "Recipient Name" in document.text
    assert "Defined" not in document.text


def test_table_cells_are_recovered() -> None:
    extra = """[frm]
\t1
\t[tbl]
\t\t 2 2 0 0
\t[data]
\t\t\t 0 0 16384 0 0
alpha
\t\t\t 0 1 16384 0 0
beta
\t\t\t 1 0 16384 0 0
gamma
\t\t\t 1 1 16384 0 0
delta
\t\t[e]
"""
    document = parse_bytes(sam("body", extra=extra))
    frame = next(block for block in document.blocks if isinstance(block, Frame))
    table = next(block for block in frame.blocks if isinstance(block, Table))
    assert [[cell.text for cell in row.cells] for row in table.rows] == [
        ["alpha", "beta"],
        ["gamma", "delta"],
    ]


def test_table_cell_style_markers_are_controls_not_visible_text() -> None:
    extra = """[frm]
\t3
\t[tbl]
\t\t 1 2 0 0
\t[data]
\t\t\t 0 0 0 0 0
@Body Text@Value
\t\t\t 0 1 0 0 0
@@literal
\t\t[e]
"""
    document = parse_bytes(sam("body", extra=extra))
    frame = next(block for block in document.blocks if isinstance(block, Frame))
    table = next(block for block in frame.blocks if isinstance(block, Table))

    assert [cell.text for cell in table.rows[0].cells] == ["Value", "@literal"]


def test_table_formula_metadata_does_not_leak_below_cached_value() -> None:
    extra = """[frm]
\t3
\t[tbl]
\t\t 1 1 0 0
\t[data]
\t\t\t 0 0 0 0 0
3819,00
>
@@sum(B1..B19)
\t\t[e]
"""
    document = parse_bytes(sam("body", extra=extra))
    frame = next(block for block in document.blocks if isinstance(block, Frame))
    table = next(block for block in frame.blocks if isinstance(block, Table))

    assert table.rows[0].cells[0].text == "3819,00"
    assert "@sum" not in document.text
    assert any(record.record_type == "table-formula" for record in document.unknown_records)
    assert any(
        item.code == "table-formula-not-recalculated" for item in document.diagnostics
    )


def test_huge_table_coordinate_fails_with_a_toolkit_resource_error() -> None:
    coordinate = "9" * 5000
    extra = f"""[frm]
\t3
\t[tbl]
\t\t 1 1 0 0
\t[data]
\t\t\t {coordinate} 0 0 0 0
value
\t\t[e]
"""

    with pytest.raises(ResourceLimitError, match="table row"):
        parse_bytes(sam("body", extra=extra))


def test_frame_text_is_recovered() -> None:
    extra = """[frm]
\t1
\t[txt]
frame heading

frame body
>
"""
    document = parse_bytes(sam("main body", extra=extra))
    assert document.text.index("main body") < document.text.index("frame heading")
    assert "frame body" in document.text
    assert any(item.code == "unanchored-frame-reflowed" for item in document.diagnostics)


def test_anchored_tables_are_spliced_at_body_anchors_in_reference_order() -> None:
    extra = """[frm]
\t3
\t524288
\t[tbl]
\t\t 1 1 0 0
\t[data]
\t\t\t 0 0 0 0 0
first table
\t\t[e]
[frm]
\t3
\t524288
\t[tbl]
\t\t 1 1 0 0
\t[data]
\t\t\t 0 0 0 0 0
second table
\t\t[e]
"""
    document = parse_bytes(
        sam("before\n\n<:t1>\n\nmiddle\n\n<:t0>\n\nafter", extra=extra)
    )

    assert document.text.index("before") < document.text.index("second table")
    assert document.text.index("second table") < document.text.index("middle")
    assert document.text.index("middle") < document.text.index("first table")
    assert document.text.index("first table") < document.text.index("after")


def test_bad_body_anchor_is_visible_and_diagnostic() -> None:
    document = parse_bytes(sam("before<:t9>after"))

    assert "before" in document.text and "after" in document.text
    assert "missing frame anchor" in document.text
    assert any(item.code == "frame-anchor-out-of-range" for item in document.diagnostics)


def test_multiline_note_close_does_not_terminate_main_text() -> None:
    document = parse_bytes(
        sam(
            "before <:N711933632,,65535,1,1\n"
            "annotation text\n"
            ">\n"
            "after"
        )
    )

    assert document.text.index("before") < document.text.index("annotation text")
    assert document.text.index("annotation text") < document.text.index("after")
    assert any(item.code == "annotation-metadata-opaque" for item in document.diagnostics)


def test_multiline_header_close_does_not_terminate_main_text() -> None:
    document = parse_bytes(sam("lead\n\n<:H<*->\nheader text\n>\nbody text\n\nthe end"))

    assert "header text" in document.text
    assert "body text" in document.text
    assert "the end" in document.text
    assert document.text.index("body text") < document.text.index("the end")


def test_macro_section_is_never_executed() -> None:
    document = parse_bytes(sam("safe", extra="[macro]\n\tRUN something\n"))
    assert any(
        isinstance(block, UnsupportedObject) and block.kind == "macro"
        for block in document.blocks
    )
    assert any(item.code == "active-content-disabled" for item in document.diagnostics)


def test_embedded_bitmap_is_bounded_and_extracted() -> None:
    prefix = sam("picture")
    # The parser only claims byte preservation here; image render tests use a valid bitmap.
    bitmap = b"BM" + b"\0" * 30
    preview = b"SS" + b"\0" * 10 + b"\r\n"
    asset_offset = len(prefix)
    preview_offset = asset_offset + len(bitmap)
    marker_offset = preview_offset + len(preview)
    manifest = (
        f"[Embedded]\r\n1 .bmp {asset_offset} {len(bitmap)} "
        f"{preview_offset} {len(preview)} \r\n{marker_offset:08d}\r\n"
    ).encode("ascii")
    document = parse_bytes(prefix + bitmap + preview + manifest)
    image = next(block for block in document.blocks if isinstance(block, Image))
    assert image.data == bitmap
    assert image.media_type == "image/bmp"


def test_embedded_total_limit_is_checked_before_payload_materialization() -> None:
    prefix = sam("assets")
    bitmap = b"BM" + b"\0" * 30
    marker_offset = len(prefix)
    manifest = (
        f"[Embedded]\r\n"
        f"1 .bmp {marker_offset + 128} {len(bitmap)} 0 0 \r\n"
        f"2 .bmp {marker_offset + 128} {len(bitmap)} 0 0 \r\n"
        f"{marker_offset:08d}\r\n"
    ).encode("ascii")

    with pytest.raises(ResourceLimitError, match="embedded asset total"):
        parse_bytes(
            prefix + manifest + bitmap,
            limits=ParseLimits(max_total_asset_bytes=len(bitmap)),
        )


def test_small_textual_preamble_is_recovered() -> None:
    document = parse_bytes(b"legacy-path\r\n" + sam("recovered"))
    assert "recovered" in document.text
    assert any(item.code == "leading-preamble" for item in document.diagnostics)


@pytest.mark.parametrize("payload", [b"", b"not sam", b"[ver]\n"])
def test_malformed_inputs_fail_safely(payload: bytes) -> None:
    with pytest.raises(ParseError):
        parse_bytes(payload)


def test_random_trailing_bytes_do_not_crash_text_parser() -> None:
    random_source = random.Random(20260813)
    for _ in range(50):
        tail = random_source.randbytes(random_source.randrange(0, 512))
        document = parse_bytes(sam("sentinel") + tail)
        assert "sentinel" in document.text
