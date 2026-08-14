from __future__ import annotations

import importlib.util
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from reportlab.platypus import Paragraph as ReportLabParagraph

from amipro_sam.model import (
    Document,
    Frame,
    PageLayout,
    PageVariantGeometry,
    Paragraph,
    TextRun,
    TwipRect,
)
from amipro_sam.renderers import docx, html, odt, pdf
from amipro_sam.renderers.paragraph_geometry import resolve_paragraph_region

BODY_WIDTH_TWIPS = 9_000
ODF = {
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _paragraph(
    text: str,
    *,
    x: int | None = None,
    width: int | None = None,
    left_indent_in: float | None = None,
) -> Paragraph:
    return Paragraph(
        runs=[TextRun(text)],
        left_indent_in=left_indent_in,
        region_x_twips=x,
        region_width_twips=width,
    )


def _document(*blocks: object) -> Document:
    geometry = PageVariantGeometry(
        side="odd",
        height_twips=15_840,
        width_twips=12_000,
        margin_left_twips=1_500,
        margin_right_twips=1_500,
        margin_top_twips=1_440,
        margin_bottom_twips=1_440,
        valid=True,
        page_rect=TwipRect(0, 0, 12_000, 15_840, valid=True),
        content_rect=TwipRect(1_500, 1_440, 10_500, 14_400, valid=True),
    )
    return Document(
        "regions.sam",
        "windows-1252",
        page_layouts=[PageLayout(index=0, odd=geometry, valid=True)],
        blocks=list(blocks),  # type: ignore[arg-type]
    )


def _odt_paragraph_properties(payload: bytes) -> dict[str, dict[str, str]]:
    with ZipFile(BytesIO(payload)) as archive:
        root = ET.fromstring(archive.read("content.xml"))
    styles = {
        style.attrib[f"{{{ODF['style']}}}name"]: style.find(
            "style:paragraph-properties", ODF
        )
        for style in root.findall(".//style:style", ODF)
    }
    result: dict[str, dict[str, str]] = {}
    for paragraph in root.findall(".//text:p", ODF):
        name = paragraph.attrib.get(f"{{{ODF['text']}}}style-name")
        properties = styles.get(name or "")
        if properties is not None:
            result["".join(paragraph.itertext()).replace(" ", "")] = properties.attrib
    return result


def test_region_resolution_is_atomic_and_allows_source_rounding() -> None:
    rounded = _paragraph("rounded", x=0, width=BODY_WIDTH_TWIPS + 3)
    resolved = resolve_paragraph_region(rounded, BODY_WIDTH_TWIPS)

    assert resolved is not None
    assert resolved.left_twips == 0
    assert resolved.right_twips == 0
    assert resolved.first_line_twips == 0

    positive_full_width = _paragraph(
        "full width with first-line position",
        x=426,
        width=BODY_WIDTH_TWIPS - 3,
    )
    full_width = resolve_paragraph_region(
        positive_full_width,
        BODY_WIDTH_TWIPS,
    )
    assert full_width is not None
    assert full_width.left_twips == 0
    assert full_width.right_twips == 0
    assert full_width.first_line_twips == 426

    impossible = _paragraph("impossible", x=8_000, width=2_000)
    assert resolve_paragraph_region(impossible, BODY_WIDTH_TWIPS) is None
    assert resolve_paragraph_region(impossible, None) is None


def test_html_uses_x_and_width_as_additive_region_margins() -> None:
    rendered = html.render(
        _document(
            _paragraph("full", x=0, width=BODY_WIDTH_TWIPS),
            _paragraph(
                "right column",
                x=4_500,
                width=4_500,
                left_indent_in=0.125,
            ),
            _paragraph("invalid", x=8_000, width=2_000, left_indent_in=0.25),
        )
    ).decode("utf-8")

    assert "<p>full</p>" in rendered
    assert '<p style="margin-left:3.25in">right column</p>' in rendered
    assert '<p style="margin-left:0.25in">invalid</p>' in rendered


def test_pdf_story_uses_custom_body_width_and_keeps_readable_columns() -> None:
    document = _document(
        _paragraph("full width", x=426, width=8_997),
        _paragraph("left column", x=0, width=4_500),
        _paragraph("right column", x=4_500, width=4_500),
        _paragraph("invalid", x=8_000, width=2_000, left_indent_in=0.25),
    )

    paragraphs = [
        item for item in pdf._primary_story(document) if isinstance(item, ReportLabParagraph)
    ]
    by_text = {paragraph.getPlainText(): paragraph.style for paragraph in paragraphs}

    assert by_text["full width"].leftIndent == pytest.approx(0.0)
    assert by_text["full width"].rightIndent == pytest.approx(0.0)
    assert by_text["full width"].firstLineIndent == pytest.approx(21.3)
    assert by_text["left column"].leftIndent == pytest.approx(0.0)
    assert by_text["left column"].rightIndent == pytest.approx(225.0)
    assert by_text["right column"].leftIndent == pytest.approx(225.0)
    assert by_text["right column"].rightIndent == pytest.approx(0.0)
    assert by_text["invalid"].leftIndent == pytest.approx(18.0)
    assert by_text["invalid"].rightIndent == pytest.approx(0.0)


def test_odt_emits_the_same_body_region_margins() -> None:
    properties = _odt_paragraph_properties(
        odt.render(
            _document(
                _paragraph("left column", x=0, width=4_500),
                _paragraph(
                    "right column",
                    x=4_500,
                    width=4_500,
                    left_indent_in=0.125,
                ),
                _paragraph("invalid", x=8_000, width=2_000, left_indent_in=0.25),
            )
        )
    )

    assert properties["leftcolumn"][f"{{{ODF['fo']}}}margin-right"] == "3.125in"
    assert properties["rightcolumn"][f"{{{ODF['fo']}}}margin-left"] == "3.25in"
    assert properties["invalid"][f"{{{ODF['fo']}}}margin-left"] == "0.25in"


def test_reflowed_frame_does_not_apply_region_against_the_page_body() -> None:
    child = _paragraph("frame child", x=4_500, width=4_500, left_indent_in=0.25)
    frame = Frame(
        blocks=[child],
        content_kind="text",
        placement="anchored",
        bounds=TwipRect(0, 0, 5_000, 2_000, valid=True),
    )

    rendered = html.render(_document(frame)).decode("utf-8")

    assert '<p style="margin-left:0.25in">frame child</p>' in rendered
    assert "margin-left:3.375in" not in rendered


@pytest.mark.skipif(
    importlib.util.find_spec("docx") is None,
    reason="python-docx extra not installed",
)
def test_docx_emits_region_margins_in_twips() -> None:
    payload = docx.render(
        _document(
            _paragraph(
                "right column",
                x=4_500,
                width=4_500,
                left_indent_in=0.125,
            )
        )
    )
    with ZipFile(BytesIO(payload)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    paragraph = next(
        item
        for item in root.findall(f".//{{{WORD}}}p")
        if "".join(item.itertext()) == "right column"
    )
    indent = paragraph.find(f"{{{WORD}}}pPr/{{{WORD}}}ind")

    assert indent is not None
    assert indent.attrib[f"{{{WORD}}}left"] == "4680"
