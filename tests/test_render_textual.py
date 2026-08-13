from __future__ import annotations

import base64
import json as stdlib_json
import struct
from pathlib import Path

import pytest

from amipro_sam.errors import RenderError
from amipro_sam.model import (
    CharacterStyle,
    Diagnostic,
    Document,
    Image,
    PageBreak,
    Paragraph,
    Severity,
    SourceSpan,
    StyleDefinition,
    Table,
    TableCell,
    TableRow,
    TextRun,
    UnsupportedObject,
)
from amipro_sam.renderers import get_renderer
from amipro_sam.renderers import html as html_renderer
from amipro_sam.renderers import json as json_renderer
from amipro_sam.renderers import markdown as markdown_renderer
from amipro_sam.renderers import text as text_renderer

_ONE_PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
)


def _one_pixel_bmp() -> bytes:
    pixels = b"\x00\x80\xff\x00"
    size = 14 + 40 + len(pixels)
    return (
        b"BM"
        + struct.pack("<IHHI", size, 0, 0, 54)
        + struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
        + pixels
    )


def _paragraph(
    value: str,
    *,
    style: CharacterStyle | None = None,
    style_name: str | None = None,
    list_kind: str | None = None,
    list_level: int = 0,
) -> Paragraph:
    return Paragraph(
        runs=[TextRun(value, style or CharacterStyle())],
        style_name=style_name,
        list_kind=list_kind,  # type: ignore[arg-type]
        list_level=list_level,
    )


def _document() -> Document:
    source = SourceSpan(line=9, column=3, byte_offset=80, end_byte_offset=90)
    return Document(
        source_name='unsafe <source> & "name".sam',
        encoding="windows-1252",
        metadata={"title": "Recovered <archive>"},
        styles={
            "Ancestor": StyleDefinition(
                "Ancestor", character=CharacterStyle(bold=True, font_family="Times")
            ),
            "Heading 2": StyleDefinition(
                "Heading 2", parent="Ancestor", alignment="center"
            ),
        },
        blocks=[
            _paragraph(
                "A <script>alert('x')</script> heading",
                style=CharacterStyle(italic=True),
                style_name="Heading 2",
            ),
            _paragraph("first", list_kind="bullet"),
            _paragraph("second", list_kind="bullet"),
            Table(
                [
                    TableRow(
                        [
                            TableCell([_paragraph("Name")]),
                            TableCell([_paragraph("Value")], column_span=2),
                        ],
                        is_header=True,
                    ),
                    TableRow(
                        [
                            TableCell([_paragraph("alpha")]),
                            TableCell([_paragraph("1 | 2")]),
                        ]
                    ),
                ]
            ),
            Image(data=_ONE_PIXEL_GIF, media_type="image/svg+xml", alt_text='pixel "safe"'),
            Image(
                reference="https://example.invalid/tracker.png",
                alt_text="remote <image>",
            ),
            PageBreak(),
            UnsupportedObject("OLE <object>", "was not & must not be activated"),
        ],
        diagnostics=[
            Diagnostic(
                Severity.WARNING,
                "unknown-<tag>",
                "Ignored <script>bad()</script>",
                source,
            )
        ],
        source_directory=Path("/private/source"),
    )


def test_html_is_self_contained_semantic_and_safely_escaped() -> None:
    rendered = html_renderer.render(_document()).decode("utf-8")

    assert rendered.startswith("<!doctype html>\n")
    assert '<meta charset="utf-8">' in rendered
    assert "default-src &#39;none&#39;" in rendered
    assert '<h2 style="text-align:center">' in rendered
    assert "<strong><em>" in rendered
    assert "&lt;script&gt;alert('x')&lt;/script&gt;" in rendered
    assert "<script>" not in rendered
    assert "<ul>\n<li>first</li>\n<li>second</li>\n</ul>" in rendered
    assert '<th colspan="2">' in rendered
    assert '<hr class="page-break"' in rendered
    assert "[Unsupported OLE &lt;object&gt;:" in rendered
    assert "Conversion warnings" in rendered
    assert "unknown-&lt;tag&gt;" in rendered

    # The supplied SVG MIME type is ignored; validated bytes determine the
    # actual, inert raster media type.
    encoded = base64.b64encode(_ONE_PIXEL_GIF).decode("ascii")
    assert f"data:image/gif;base64,{encoded}" in rendered
    assert "data:image/svg+xml" not in rendered
    assert 'src="https://example.invalid' not in rendered
    assert "external reference not loaded: https://example.invalid/tracker.png" in rendered


def test_html_rejects_unvalidated_image_data_and_can_hide_diagnostics() -> None:
    document = Document(
        "bad.sam",
        "utf-8",
        blocks=[Image(data=b"\x89PNG\r\n\x1a\nnot-a-png", alt_text="bad")],
        diagnostics=[Diagnostic(Severity.WARNING, "problem", "a warning")],
    )

    rendered = html_renderer.render(document, include_warnings=False).decode("utf-8")

    assert "data:image/" not in rendered
    assert "not a validated PNG, JPEG, GIF, or BMP" in rendered
    assert "Conversion warnings" not in rendered
    assert "a warning" not in rendered


def test_html_embeds_only_structurally_validated_bmp_bytes() -> None:
    valid = _one_pixel_bmp()
    invalid = bytearray(valid)
    invalid[10:14] = struct.pack("<I", len(valid) + 100)  # Pixel offset out of bounds.
    document = Document(
        "bitmap.sam",
        "windows-1252",
        blocks=[
            Image(data=valid, media_type="text/html", alt_text="valid bitmap"),
            Image(data=bytes(invalid), media_type="image/bmp", alt_text="invalid bitmap"),
        ],
    )

    rendered = html_renderer.render(document).decode("utf-8")

    encoded = base64.b64encode(valid).decode("ascii")
    assert f"data:image/bmp;base64,{encoded}" in rendered
    assert "data:text/html" not in rendered
    assert "invalid bitmap (embedded data was not a validated" in rendered


def test_markdown_preserves_headings_emphasis_lists_tables_and_placeholders() -> None:
    rendered = markdown_renderer.render(_document()).decode("utf-8")

    assert rendered.startswith("## ***A &lt;script&gt;")
    assert "<script>" not in rendered
    assert "- first\n- second" in rendered
    assert "| Name | Value |  |" in rendered
    assert "| --- | --- | --- |" in rendered
    assert r"1 \| 2" in rendered
    assert r'\[Image: pixel "safe" (embedded image data)\]' in rendered
    assert "embedded image data" in rendered
    assert "external reference not loaded" in rendered
    assert "[Page break]" in rendered
    assert "Unsupported OLE &lt;object&gt;" in rendered


def test_plain_text_preserves_order_tsv_page_breaks_and_object_labels() -> None:
    document = Document(
        "ordered.sam",
        "utf-8",
        blocks=[
            _paragraph("before"),
            Table(
                [
                    TableRow([TableCell([_paragraph("a")]), TableCell([_paragraph("b")])]),
                    TableRow(
                        [
                            TableCell([_paragraph("one"), _paragraph("two")]),
                            TableCell([_paragraph("c")]),
                        ]
                    ),
                ]
            ),
            PageBreak(),
            Image(reference="/must/not/be/read.png", alt_text="portrait"),
            UnsupportedObject("equation", "formula unavailable"),
            _paragraph("after"),
        ],
    )

    rendered = text_renderer.render(document).decode("utf-8")

    assert rendered.index("before") < rendered.index("a\tb") < rendered.index("\f")
    assert "one / two\tc" in rendered
    assert "[Image: portrait (external reference not loaded: /must/not/be/read.png)]" in rendered
    assert "[Unsupported equation: formula unavailable]" in rendered
    assert rendered.rstrip().endswith("after")


def test_json_dump_is_deterministic_utf8_and_does_not_inline_bytes() -> None:
    document = Document(
        "café.sam",
        "windows-1252",
        blocks=[Image(data=b"secret bytes", alt_text="scan")],
        source_directory=Path("/source/path"),
    )

    first = json_renderer.render(document)
    second = json_renderer.render(document)
    decoded = stdlib_json.loads(first)

    assert first == second
    assert first.endswith(b"\n")
    assert b"caf\xc3\xa9.sam" in first
    assert b"secret bytes" not in first
    assert decoded["type"] == "Document"
    assert decoded["blocks"][0]["data"] == {
        "encoding": "not-inlined",
        "length": 12,
    }
    assert decoded["source_directory"] == "/source/path"


def test_renderer_registry_supports_textual_aliases() -> None:
    document = Document("empty.sam", "utf-8")

    assert get_renderer("md")(document) == b""
    assert get_renderer("txt")(document) == b""
    assert get_renderer("json")(document).startswith(b"{")
    with pytest.raises(RenderError, match="unknown output format"):
        get_renderer("executable")
