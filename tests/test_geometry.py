from __future__ import annotations

from amipro_sam.model import (
    Document,
    Footer,
    Frame,
    Header,
    PageVariantGeometry,
    Paragraph,
    TwipRect,
)
from amipro_sam.parser import parse_bytes


def sam(body: str = "body", *, extra: str = "") -> bytes:
    text = "[ver]\n\t4\n[sty]\n\t\n" + extra + "[edoc]\n" + body + "\n>\n"
    return text.replace("\n", "\r\n").encode("cp1252")


def page_variant(
    *,
    height: str = "15840",
    width: str = "12240",
    left: str = "720",
    bottom: str = "900",
    unit: str = "1",
    top: str = "1080",
    right: str = "540",
) -> str:
    # Every value in this helper is invented for this test suite.
    return "\n".join(
        (height, width, "37", left, bottom, unit, top, right, "19")
    )


def test_page_layout_maps_the_nine_field_geometry_in_twips() -> None:
    layout = f"""[lay]
\tInvented Layout
\t1284
\t[rght]
\t\t{page_variant().replace(chr(10), chr(10) + chr(9) * 2)}
\t[lft]
\t\t{page_variant(left="540", right="720").replace(chr(10), chr(10) + chr(9) * 2)}
"""
    document = parse_bytes(sam(extra=layout))

    assert len(document.page_layouts) == 1
    parsed = document.page_layouts[0]
    assert (parsed.index, parsed.name, parsed.paper_kind) == (0, "Invented Layout", "a4")
    assert parsed.orientation == "landscape"
    assert parsed.mirrored is True
    assert parsed.non_alternating is False
    assert parsed.valid is True

    odd = parsed.odd
    assert isinstance(odd, PageVariantGeometry) and odd.valid
    assert (odd.height_twips, odd.width_twips, odd.reserved) == (15840, 12240, 37)
    assert (
        odd.margin_left_twips,
        odd.margin_bottom_twips,
        odd.display_unit,
        odd.margin_top_twips,
        odd.margin_right_twips,
        odd.flags,
    ) == (720, 900, 1, 1080, 540, 19)
    assert odd.page_rect == TwipRect(0, 0, 12240, 15840, True)
    assert odd.content_rect == TwipRect(720, 1080, 11700, 14940, True)
    assert odd.content_rect.width_twips == 10980
    assert parsed.primary_geometry is odd


def test_page_geometry_types_nine_field_prefix_and_retains_opaque_tail() -> None:
    prefix = page_variant()
    layout = f"""[lay]
\tTail Fields
\t7
\t[rght]
\t\t{prefix.replace(chr(10), chr(10) + chr(9) * 2)}
\t\t101
\t\t202
\t\t303
"""
    document = parse_bytes(sam(extra=layout))

    geometry = document.page_layouts[0].odd
    assert geometry is not None and geometry.valid is True
    assert geometry.raw_fields[-3:] == ("101", "202", "303")
    assert geometry.flags == 19


def test_geometry_typed_summary_is_capped_without_losing_section_raw() -> None:
    fields = page_variant().splitlines() + [str(10000 + index) for index in range(1100)]
    indented = "\n".join("\t\t" + value for value in fields)
    layout = f"""[lay]
\tCapped Fields
\t7
\t[rght]
{indented}
"""
    document = parse_bytes(sam(extra=layout))

    geometry = document.page_layouts[0].odd
    assert geometry is not None and geometry.valid is True
    assert len(geometry.raw_fields) == 1024
    assert fields[-1] in document.page_layouts[0].raw
    assert any(
        item.code == "page-geometry-summary-truncated"
        for item in document.diagnostics
    )


def test_page_geometry_accepts_the_ceiling_and_rejects_pathological_values() -> None:
    ceiling = page_variant(
        height="31680", width="31680", left="0", bottom="0", top="0", right="0"
    )
    too_wide = page_variant(width="31681")
    tiny_body = page_variant(width="1000", left="500", right="357")
    extra = f"""[lay]
\tCeiling
\t7
\t[rght]
\t\t{ceiling.replace(chr(10), chr(10) + chr(9) * 2)}
[lay]
\tToo Wide
\t7
\t[rght]
\t\t{too_wide.replace(chr(10), chr(10) + chr(9) * 2)}
[lay]
\tTiny Body
\t7
\t[rght]
\t\t{tiny_body.replace(chr(10), chr(10) + chr(9) * 2)}
"""
    document = parse_bytes(sam(extra=extra))

    assert document.page_layouts[0].odd is not None
    assert document.page_layouts[0].odd.valid is True
    assert document.page_layouts[1].odd is not None
    assert document.page_layouts[1].odd.valid is False
    assert document.page_layouts[2].odd is not None
    assert document.page_layouts[2].odd.valid is False
    assert sum(item.code == "invalid-page-geometry" for item in document.diagnostics) == 2


def test_malformed_page_geometry_remains_raw_without_integer_amplification() -> None:
    huge = "9" * 200
    layout = f"""[lay]
\tMalformed
\t7
\t[rght]
\t\t{huge}
\t\t12240
\t\t3
"""
    document = parse_bytes(sam(extra=layout))

    geometry = document.page_layouts[0].odd
    assert geometry is not None and geometry.valid is False
    assert geometry.raw_fields[0] == huge
    assert geometry.width_twips is None
    assert any(item.code == "malformed-page-geometry" for item in document.diagnostics)


def test_anchored_frames_wrap_contents_at_reference_order_with_typed_geometry() -> None:
    first_flags = 524288 | 64 | 128
    second_flags = 524288
    extra = f"""[frm]
\t2
\t{first_flags}
\t100
\t200
\t1540
\t1640
\t[frmlay]
\t\t11
\t\t22
\t[txt]
first invented frame
>
[frm]
\t3
\t{second_flags}
\t-120
\t240
\t1320
\t1680
\t[txt]
second invented frame
>
"""
    document = parse_bytes(
        sam("before\n\n<:A1>\n\nmiddle\n\n<:A0>\n\nafter", extra=extra)
    )
    frames = [block for block in document.blocks if isinstance(block, Frame)]

    assert [frame.anchor_index for frame in frames] == [1, 0]
    assert [frame.blocks[0].text for frame in frames] == [
        "second invented frame",
        "first invented frame",
    ]
    assert document.text.index("second invented frame") < document.text.index("middle")
    first = next(frame for frame in frames if frame.anchor_index == 0)
    assert first.placement == "anchored"
    assert first.region == "body" and first.layer_role == "unknown"
    assert first.bounds == TwipRect(100, 200, 1540, 1640, True)
    assert first.opaque is True and first.wrap_around is True
    assert first.frame_layout_fields == ("11", "22")


def test_invalid_fixed_frame_geometry_is_visible_but_never_marked_background() -> None:
    extra = """[frm]
\t4
\t0
\t32000
\t200
\t-32000
\t1640
\t[txt]
invented invalid frame
>
"""
    document = parse_bytes(sam(extra=extra))
    frame = next(block for block in document.blocks if isinstance(block, Frame))

    assert frame.placement == "fixed-page"
    assert frame.layer_role == "unknown"
    assert frame.bounds is not None and frame.bounds.valid is False
    assert "invented invalid frame" in document.text
    assert any(item.code == "invalid-frame-geometry" for item in document.diagnostics)


def test_frame_typed_field_summary_is_capped_and_diagnosed() -> None:
    header = ["6", "0", "100", "200", "1540", "1640"]
    header.extend(str(20000 + index) for index in range(1100))
    fields = "\n".join("\t" + value for value in header)
    extra = f"""[frm]
{fields}
\t[txt]
invented capped frame
>
"""
    document = parse_bytes(sam(extra=extra))
    frame = next(block for block in document.blocks if isinstance(block, Frame))

    assert len(frame.raw_header_fields) == 1024
    assert header[-1] in frame.raw
    assert frame.bounds == TwipRect(100, 200, 1540, 1640, True)
    assert any(
        item.code == "frame-field-summary-truncated"
        for item in document.diagnostics
    )


def test_page_hints_are_typed_but_deliberately_opaque() -> None:
    hints = """[pg]
\t17 23 41
\t[opaque]
\t\tinvented hint text
"""
    document = parse_bytes(sam(extra=hints))

    assert len(document.page_hints) == 1
    assert document.page_hints[0].raw == "\t17 23 41\n\t[opaque]\n\t\tinvented hint text"
    assert not hasattr(document.page_hints[0], "page_count")


def test_nested_layout_streams_keep_content_and_frame_geometry_separate() -> None:
    layout = """[lay]
\tInvented Header Layout
\t1
\t[rght]
\t\t15840
\t\t12240
\t\t5
\t\t720
\t\t720
\t\t1
\t\t720
\t\t720
\t\t0
\t[hrght]
\t\t[lyfrm]
\t\t\t1
\t\t\t2048
\t\t\t720
\t\t\t100
\t\t\t11520
\t\t\t500
\t\t[frmlay]
\t\t\t13
\t\t\t10800
\t\t[txt]
\t\t\tInvented odd header
\t\t>
\t[frght]
\t\t[lyfrm]
\t\t\t1
\t\t\t4096
\t\t\t720
\t\t\t15100
\t\t\t11520
\t\t\t15600
\t\t[txt]
\t\t\tInvented odd footer
\t\t>
"""
    document = parse_bytes(sam(extra=layout))
    header = next(block for block in document.blocks if isinstance(block, Header))
    footer = next(block for block in document.blocks if isinstance(block, Footer))

    assert [block.text for block in header.blocks if isinstance(block, Paragraph)] == [
        "Invented odd header"
    ]
    assert [block.text for block in footer.blocks if isinstance(block, Paragraph)] == [
        "Invented odd footer"
    ]
    assert header.frame is not None and header.frame.region == "header"
    assert footer.frame is not None and footer.frame.region == "footer"
    assert header.frame.blocks is header.blocks and footer.frame.blocks is footer.blocks
    assert header.frame.bounds == TwipRect(720, 100, 11520, 500, True)
    assert footer.frame.bounds == TwipRect(720, 15100, 11520, 15600, True)


def test_sibling_layout_records_belong_to_the_preceding_header_or_footer() -> None:
    layout = """[lay]
\tSibling Shape
\t1
\t[hrght]
\t[lyfrm]
\t\t2
\t\t2048
\t\t300
\t\t400
\t\t3900
\t\t800
\t[frmlay]
\t\t17
\t\t3600
\t[txt]
\t\tSibling invented header
\t>
\t[frght]
\t[lyfrm]
\t\t2
\t\t4096
\t\t300
\t\t5000
\t\t3900
\t\t5400
\t[txt]
\t\tSibling invented footer
\t>
\t[rght]
\t\t6000
\t\t4200
\t\t29
\t\t300
\t\t300
\t\t1
\t\t300
\t\t300
\t\t31
\t[bodyframe]
\t\t[txt]
\t\t\tMust not join footer
\t\t>
"""
    document = parse_bytes(sam(extra=layout))
    header = next(block for block in document.blocks if isinstance(block, Header))
    footer = next(block for block in document.blocks if isinstance(block, Footer))

    assert [block.text for block in header.blocks if isinstance(block, Paragraph)] == [
        "Sibling invented header"
    ]
    assert [block.text for block in footer.blocks if isinstance(block, Paragraph)] == [
        "Sibling invented footer"
    ]
    assert header.frame is not None
    assert header.frame.bounds == TwipRect(300, 400, 3900, 800, True)
    assert footer.frame is not None
    assert footer.frame.bounds == TwipRect(300, 5000, 3900, 5400, True)
    assert "Must not join footer" not in "\n".join(
        block.text for block in footer.blocks if isinstance(block, Paragraph)
    )


def test_malformed_layout_branch_bounds_the_preceding_valid_sibling_stream() -> None:
    layout = """[lay]
	Invented malformed boundary
	1
	[hrght]
	[txt]
		Odd text only
	>
		[hlft]
		[txt]
			Malformed even text stays fallback only
		>
"""
    document = parse_bytes(sam(extra=layout))
    header = next(block for block in document.blocks if isinstance(block, Header))

    typed = "\n".join(
        block.text for block in header.blocks if isinstance(block, Paragraph)
    )
    assert typed == "Odd text only"
    assert document.text.count("Malformed even text stays fallback only") == 1
    assert any(
        item.code == "malformed-layout-branch-indentation"
        for item in document.diagnostics
    )


def test_unterminated_sibling_stream_stops_at_next_exact_layout_branch() -> None:
    layout = """[lay]
	Invented corrupt sibling
	1
	[hrght]
	[txt]
		unterminated odd text
	[frght]
	[txt]
		valid footer text
	>
	[rght]
		6000
		4200
		0
		300
		300
		1
		300
		300
		0
"""
    document = parse_bytes(sam(extra=layout))
    header = next(block for block in document.blocks if isinstance(block, Header))
    footer = next(block for block in document.blocks if isinstance(block, Footer))

    assert header.terminated is False
    assert header.blocks[0].text == "unterminated odd text"
    assert footer.terminated is True
    assert footer.blocks[0].text == "valid footer text"
    assert any(
        item.code == "unterminated-layout-header-footer"
        for item in document.diagnostics
    )


def test_bracket_lookalike_inside_layout_text_is_not_a_branch_boundary() -> None:
    layout = """[lay]
	Invented literal marker
	1
	[hrght]
	[txt]
		BEFORE
		[[hrght]]
		AFTER
	>
"""
    document = parse_bytes(sam(extra=layout))
    header = next(block for block in document.blocks if isinstance(block, Header))

    assert "BEFORE" in document.text
    assert "[[hrght]]" in document.text
    assert "AFTER" in document.text
    assert header.terminated is True


def test_duplicate_page_variant_is_typed_but_not_renderer_valid() -> None:
    variant = page_variant()
    layout = f"""[lay]
	Ambiguous geometry
	1
	[rght]
		{variant.replace(chr(10), chr(10) + chr(9) * 2)}
	[rght]
		{variant.replace(chr(10), chr(10) + chr(9) * 2)}
"""
    document = parse_bytes(sam(extra=layout))

    assert document.page_layouts[0].odd is not None
    assert document.page_layouts[0].odd.valid is False
    assert document.page_layouts[0].valid is False
    assert any(item.code == "duplicate-page-variant" for item in document.diagnostics)


def test_twip_rectangle_rejects_hostile_manual_ir() -> None:
    assert TwipRect(0, 0, 1440, 1440, True).is_usable is True
    assert TwipRect(0, 0, 1440, 1440, False).is_usable is False
    assert TwipRect(False, 0, 1440, 1440, True).is_usable is False
    assert TwipRect(0, 0, 31681, 1440, True).is_usable is False


def test_recursive_manual_frame_ir_has_visible_text_and_json_markers() -> None:
    frame = Frame()
    frame.blocks.append(frame)
    document = Document(source_name="invented.sam", encoding="cp1252", blocks=[frame])

    assert document.text == "[Recursive content omitted]"
    encoded = document.to_dict()
    recursive = encoded["blocks"][0]["blocks"][0]
    assert recursive == {"encoding": "recursive-reference", "type": "Frame"}
