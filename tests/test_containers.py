from __future__ import annotations

import json
from pathlib import Path

import pytest

from amipro_sam.errors import ResourceLimitError
from amipro_sam.limits import ParseLimits
from amipro_sam.model import Annotation, Footer, Footnote, Header, Paragraph
from amipro_sam.parser import parse_bytes
from amipro_sam.renderers import html, markdown, text
from amipro_sam.renderers import json as json_renderer


def sam(body: str, *, extra: str = "") -> bytes:
    source = (
        "[ver]\n\t4\n[sty]\n\t\n"
        "[fopts]\n\t5\n\t3\n\t720\n\t360\n"
        + extra
        + "[edoc]\n"
        + body
        + "\n>\n"
    )
    return source.replace("\n", "\r\n").encode("cp1252")


def test_typed_nested_annotation_and_footnote_preserve_order_and_raw() -> None:
    document = parse_bytes(
        sam(
            "before\n\n"
            "<:N123\n"
            "annotation first\n\n"
            "<:F\n"
            "footnote <+!>bold<-!> and <[>script<;>\n"
            ">\n"
            "annotation last\n"
            ">\n"
            "after"
        )
    )

    annotation = next(block for block in document.blocks if isinstance(block, Annotation))
    footnote = next(block for block in annotation.blocks if isinstance(block, Footnote))

    assert annotation.metadata == "123"
    assert annotation.terminated is True
    assert "<:N123" in annotation.raw and "<:F" in annotation.raw
    assert "footnote" not in annotation.raw
    assert footnote.metadata == ""
    assert footnote.terminated is True
    assert any(
        isinstance(block, Paragraph) and "[script>" in block.text
        for block in footnote.blocks
    )
    assert document.text.index("before") < document.text.index("annotation first")
    assert document.text.index("annotation first") < document.text.index("footnote")
    assert document.text.index("footnote") < document.text.index("annotation last")
    assert document.text.index("annotation last") < document.text.index("after")


def test_body_header_footer_flags_are_typed_without_flattening() -> None:
    document = parse_bytes(
        sam("<:H6\nodd header\n>\n<:h9\neven footer\n>\nbody")
    )
    header = next(block for block in document.blocks if isinstance(block, Header))
    footer = next(block for block in document.blocks if isinstance(block, Footer))

    assert (header.flags, header.placement, header.origin) == (6, "odd", "body")
    assert (footer.flags, footer.placement, footer.origin) == (9, "even", "body")
    assert not any(
        diagnostic.code == "multiline-container-reflowed"
        for diagnostic in document.diagnostics
    )


def test_malformed_close_is_visible_and_does_not_end_container() -> None:
    document = parse_bytes(sam("<:N123\nfirst\n>trailing\nlast\n>\nafter"))
    annotation = next(block for block in document.blocks if isinstance(block, Annotation))

    assert annotation.terminated is True
    assert ">trailing" in document.text
    assert "last" in document.text and "after" in document.text
    assert any(
        diagnostic.code == "malformed-container-terminator"
        for diagnostic in document.diagnostics
    )


def test_unterminated_container_is_retained_with_specific_diagnostics() -> None:
    payload = (
        "[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\n"
        "<:F\r\nrecovered to eof\r\n"
    ).encode("cp1252")
    document = parse_bytes(payload)
    footnote = next(block for block in document.blocks if isinstance(block, Footnote))

    assert footnote.terminated is False
    assert "recovered to eof" in document.text
    assert {item.code for item in document.diagnostics} >= {
        "unterminated-footnote",
        "unterminated-edoc",
    }


def test_container_depth_is_bounded() -> None:
    with pytest.raises(ResourceLimitError, match="nesting exceeds 1"):
        parse_bytes(
            sam("<:N1\n<:F\nnested\n>\n>"),
            limits=ParseLimits(max_container_depth=1),
        )


def test_container_like_unknown_command_is_not_parser_confusion() -> None:
    document = parse_bytes(sam("before<:FootLike>after"))

    assert not any(isinstance(block, Footnote) for block in document.blocks)
    assert "Unsupported multiline record" in document.text
    assert "before" in document.text and "after" in document.text
    assert any(item.code == "unsupported-inline-tags" for item in document.diagnostics)

    nested_text = parse_bytes(
        sam('before<:X3,-32768;if Defined x x else "<:N123" endif><:X~3>after')
    )
    assert not any(isinstance(block, Annotation) for block in nested_text.blocks)
    assert "<:N123" in nested_text.text

    escaped_field = parse_bytes(sam("before<:X;<;><:N123>after"))
    assert not any(isinstance(block, Annotation) for block in escaped_field.blocks)
    assert "before" in escaped_field.text and "after" in escaped_field.text

    multiline_field = parse_bytes(
        sam('before<:X3,-32768;if Defined x x else "\n<:N123\n" endif><:X~3>after')
    )
    assert not any(isinstance(block, Annotation) for block in multiline_field.blocks)
    assert "<:N123" in multiline_field.text

    closed_text_inside_multiline_field = parse_bytes(
        sam('before<:X3,-32768;if Defined x x else "\n<:N123>\n" endif><:X~3>after')
    )
    assert not any(
        isinstance(block, Annotation)
        for block in closed_text_inside_multiline_field.blocks
    )
    assert "after" in closed_text_inside_multiline_field.text

    malformed_quoted = parse_bytes(
        sam('before<:X3,-32768;else "> <:N123" endif><:X~3>after')
    )
    assert not any(isinstance(block, Annotation) for block in malformed_quoted.blocks)
    assert "after" in malformed_quoted.text

    corrupt_unknown = parse_bytes(sam("before <:garbage\n<:N123\ninside\n>\nafter"))
    assert any(isinstance(block, Annotation) for block in corrupt_unknown.blocks)
    assert "inside" in corrupt_unknown.text and "after" in corrupt_unknown.text


def test_malformed_opener_and_earlier_literal_angle_do_not_hide_body() -> None:
    malformed = parse_bytes(sam("before\n<:F1\ninside\n>\nafter"))
    footnote = next(block for block in malformed.blocks if isinstance(block, Footnote))

    assert footnote.metadata == "1"
    assert "inside" in malformed.text and "after" in malformed.text
    assert any(
        item.code == "footnote-metadata-unsupported"
        for item in malformed.diagnostics
    )

    earlier_angle = parse_bytes(sam("before < corrupt <:N123\ninside\n>\nafter"))
    assert any(isinstance(block, Annotation) for block in earlier_angle.blocks)
    assert "before < corrupt" in earlier_angle.text
    assert "inside" in earlier_angle.text and "after" in earlier_angle.text

    letter_metadata = parse_bytes(sam("before\n<:Fbad\ninside\n>\nafter"))
    letter_footnote = next(
        block for block in letter_metadata.blocks if isinstance(block, Footnote)
    )
    assert letter_footnote.metadata == "bad"
    assert "inside" in letter_metadata.text and "after" in letter_metadata.text


def test_nested_raw_retention_is_linear() -> None:
    depth = 12
    payload = "x" * 50_000
    body = "\n".join(["<:N1"] * depth + [payload] + [">"] * depth)
    source = sam(body)
    document = parse_bytes(source, limits=ParseLimits(max_container_depth=depth))

    def raw_size(blocks: list[object]) -> int:
        total = 0
        for block in blocks:
            if isinstance(block, Annotation | Footnote | Header | Footer):
                total += len(block.raw)
                total += raw_size(block.blocks)
        return total

    assert raw_size(document.blocks) < len(source) * 2


def test_huge_header_flag_stays_bounded_and_visible() -> None:
    document = parse_bytes(sam(f"<:H{'9' * 5_000}\nheader\n>\nbody"))
    header = next(block for block in document.blocks if isinstance(block, Header))

    assert header.flags is None
    assert len(header.metadata) == 5_000
    assert any(
        item.code == "header-placement-unsupported" for item in document.diagnostics
    )


def test_footnote_options_are_bounded_and_structured() -> None:
    document = parse_bytes(sam("body"))
    options = document.footnote_options

    assert options is not None
    assert options.collect_at_page_end is True
    assert options.reset_number_each_page is False
    assert options.separator_line is True
    assert options.start_number == 3
    assert options.separator_length_in == 0.5
    assert options.indent_in == 0.25

    malformed = sam("body").replace(b"\t720\r\n", b"\t" + b"9" * 100 + b"\r\n")
    recovered = parse_bytes(malformed)
    assert recovered.footnote_options is None
    assert any(
        item.code == "malformed-footnote-options" for item in recovered.diagnostics
    )


def test_layout_page_variants_are_typed_and_body_order_is_unchanged() -> None:
    layout = """[lay]
\tStandard
\t0
\t[hrght]
\t\t[lyfrm]
\t\t\t1
\t\t[frmlay]
\t\t\t2
\t\t[txt]
\t\t\tOdd header <[>safe<;>
\t\t>
\t[frght]
\t\t[txt]
\t\t\tOdd footer
\t\t>
\t[hlft]
\t\t[txt]
\t\t\tEven header
\t\t>
\t[flft]
\t\t[txt]
\t\t\tEven footer
\t\t>
"""
    document = parse_bytes(sam("body sentinel", extra=layout))
    headers = [block for block in document.blocks if isinstance(block, Header)]
    footers = [block for block in document.blocks if isinstance(block, Footer)]

    assert [(item.origin, item.layout_index, item.placement) for item in headers] == [
        ("layout", 0, "odd"),
        ("layout", 0, "even"),
    ]
    assert [(item.origin, item.layout_index, item.placement) for item in footers] == [
        ("layout", 0, "odd"),
        ("layout", 0, "even"),
    ]
    assert document.text.index("body sentinel") < document.text.index("Odd header")
    assert "[safe>" in document.text
    assert all(item.raw for item in headers)
    assert headers[0].metadata


def test_multiple_layout_text_streams_are_all_visible_and_budgeted() -> None:
    layout = """[lay]
\tStandard
\t[hrght]
\t\t[txt]
\t\t\tFirst stream
\t\t>
\t\t[txt]
\t\t\tSecond stream
\t\t>
"""
    document = parse_bytes(sam("body", extra=layout))
    header = next(block for block in document.blocks if isinstance(block, Header))

    assert [block.text for block in header.blocks if isinstance(block, Paragraph)] == [
        "First stream",
        "Second stream",
    ]
    assert any(
        item.code == "multiple-layout-text-streams-reflowed"
        for item in document.diagnostics
    )

    many_branches = "[lay]\n" + "".join(
        "\t[hrght]\n\t\t[txt]\n\t\t\ttext\n\t\t>\n" for _ in range(3)
    )
    with pytest.raises(ResourceLimitError, match="lay/hrght/txt"):
        parse_bytes(sam("body", extra=many_branches), limits=ParseLimits(max_records=6))


def test_layout_text_stream_uses_container_depth_and_typed_nested_content() -> None:
    layout = """[lay]
\tStandard
\t[hrght]
\t\t[txt]
\t\t\tBefore nested note
\t\t\t<:N123
\t\t\tInside nested note
\t\t\t>
\t\t\tAfter nested note
\t\t>
"""
    document = parse_bytes(sam("body", extra=layout))
    header = next(block for block in document.blocks if isinstance(block, Header))

    assert any(isinstance(block, Annotation) for block in header.blocks)
    assert "Before nested note" in document.text
    assert "Inside nested note" in document.text
    assert "After nested note" in document.text


def test_layout_header_is_bounded_by_next_depth_one_section() -> None:
    layout = """[lay]
\tStandard
\t[hrght]
\t\t[txt]
\t\t\tHeader only
\t\t>
\t[rght]
\t\t[txt]
\t\t\tOrdinary frame text
\t\t>
"""
    document = parse_bytes(sam("body", extra=layout))
    header = next(block for block in document.blocks if isinstance(block, Header))

    header_text = "\n".join(
        block.text for block in header.blocks if isinstance(block, Paragraph)
    )
    assert "Header only" in header_text
    assert "Ordinary frame text" not in "\n".join(
        block.text for block in header.blocks if isinstance(block, Paragraph)
    )


def test_malformed_layout_indentation_keeps_readable_fallback_visible() -> None:
    layout = """[lay]
\tStandard
  [hrght]
    [txt]
      Malformed header remains readable
    >
"""
    document = parse_bytes(sam("body", extra=layout))

    assert "Malformed header remains readable" in document.text
    assert any(
        item.code == "malformed-layout-branch-indentation"
        for item in document.diagnostics
    )

    bounded_layout = """[lay]
\tStandard
  [hrght]
    [txt]
      Malformed header only
    >
\t[rght]
\t\t[txt]
\t\t\tOrdinary frame must not join fallback
\t\t>
"""
    bounded = parse_bytes(sam("body", extra=bounded_layout))
    placeholder_index = next(
        index
        for index, block in enumerate(bounded.blocks)
        if getattr(block, "kind", "") == "malformed layout header/footer"
    )
    fallback = bounded.blocks[placeholder_index + 1]
    assert isinstance(fallback, Paragraph)
    assert fallback.text == "Malformed header only"
    assert "Ordinary frame must not join fallback" not in fallback.text

    adjacent_layout = """[lay]
\tStandard
  [hrght]
    [txt]
      First malformed header
      [Visible bracket text]
    >
  [frght]
    [txt]
      Second malformed footer
    >
"""
    adjacent = parse_bytes(sam("body", extra=adjacent_layout))
    assert adjacent.text.count("First malformed header") == 1
    assert adjacent.text.count("[Visible bracket text]") == 1
    assert adjacent.text.count("Second malformed footer") == 1


def test_all_textual_renderers_mark_containers_and_escape_hostile_content() -> None:
    document = parse_bytes(
        sam("before\n\n<:N123\n<<script<;>alert(1)<< /script<;>\n>\nafter")
    )
    rendered_html = html.render(document).decode("utf-8")
    rendered_markdown = markdown.render(document).decode("utf-8")
    rendered_text = text.render(document).decode("utf-8")

    assert '<aside class="annotation" role="note">' in rendered_html
    assert "<script>" not in rendered_html
    assert "&lt;script&gt;alert(1)&lt; /script&gt;" in rendered_html
    assert "[Annotation]" in rendered_markdown
    assert "&lt;script&gt;alert(1)&lt; /script&gt;" in rendered_markdown
    assert "[Annotation]" in rendered_text and "[End Annotation]" in rendered_text


def test_json_keeps_typed_raw_records_without_inlining_external_data(tmp_path: Path) -> None:
    document = parse_bytes(sam("<:F\nsynthetic\n>"), source_directory=tmp_path)
    decoded = json.loads(json_renderer.render(document))

    footnote = next(block for block in decoded["blocks"] if block["type"] == "Footnote")
    assert footnote["raw"].startswith("<:F")
    assert footnote["blocks"][0]["type"] == "Paragraph"
