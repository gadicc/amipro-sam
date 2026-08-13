from __future__ import annotations

from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from amipro_sam.model import (
    Document,
    Footer,
    Frame,
    Header,
    PageBreak,
    PageLayout,
    PageVariantGeometry,
    Paragraph,
    Table,
    TableCell,
    TableRow,
    TextRun,
    TwipRect,
)
from amipro_sam.renderers import docx, odt

ODF = {
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _docx_available() -> bool:
    try:
        __import__("docx")
    except ImportError:
        return False
    return True


DOCX_AVAILABLE = _docx_available()


def _p(text: str) -> Paragraph:
    return Paragraph(runs=[TextRun(text)])


def _geometry() -> PageVariantGeometry:
    return PageVariantGeometry(
        side="odd",
        height_twips=16_833,
        width_twips=11_908,
        margin_left_twips=720,
        margin_bottom_twips=1_080,
        margin_top_twips=1_440,
        margin_right_twips=2_160,
        valid=True,
        page_rect=TwipRect(0, 0, 11_908, 16_833, valid=True),
        content_rect=TwipRect(720, 1_440, 9_748, 15_753, valid=True),
    )


def _layout_document() -> Document:
    layout = PageLayout(index=7, name="custom", odd=_geometry(), valid=True)
    table = Table(
        rows=[
            TableRow(cells=[TableCell(blocks=[_p("head-one")])], is_header=True),
            TableRow(cells=[TableCell(blocks=[_p("head-two")])], is_header=True),
            TableRow(cells=[TableCell(blocks=[_p("body-cell")])]),
        ]
    )
    return Document(
        "geometry.sam",
        "windows-1252",
        page_layouts=[layout],
        blocks=[
            PageBreak(),
            Header(
                blocks=[_p("odd-header")],
                placement="odd",
                origin="layout",
                layout_index=7,
            ),
            Header(
                blocks=[_p("even-header")],
                placement="even",
                origin="layout",
                layout_index=7,
            ),
            Footer(
                blocks=[_p("odd-footer")],
                placement="odd",
                origin="layout",
                layout_index=7,
            ),
            Footer(
                blocks=[_p("even-footer")],
                placement="even",
                origin="layout",
                layout_index=7,
            ),
            Frame(
                blocks=[_p("frame-content")],
                content_kind="text",
                placement="anchored",
                bounds=TwipRect(-200, 100, 5_000, 2_000, valid=True),
            ),
            table,
            _p("before-breaks"),
            PageBreak(),
            PageBreak(),
            _p("after-breaks"),
            PageBreak(),
        ],
    )


def _zip_xml(payload: bytes, name: str) -> ET.Element:
    with ZipFile(BytesIO(payload)) as archive:
        return ET.fromstring(archive.read(name))


def _itertext(root: ET.Element) -> str:
    return "".join(root.itertext())


def test_odt_uses_validated_geometry_native_page_content_and_reflowed_frames() -> None:
    document = _layout_document()

    first = odt.render(document)
    second = odt.render(document)

    assert first == second
    with ZipFile(BytesIO(first)) as archive:
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert all(
            not name.startswith(("/", "../")) and "/../" not in name
            for name in archive.namelist()
        )
        styles = ET.fromstring(archive.read("styles.xml"))
        content = ET.fromstring(archive.read("content.xml"))

    properties = styles.find(".//style:page-layout-properties", ODF)
    assert properties is not None
    assert properties.attrib[f"{{{ODF['fo']}}}page-width"] == "595.4pt"
    assert properties.attrib[f"{{{ODF['fo']}}}page-height"] == "841.65pt"
    assert properties.attrib[f"{{{ODF['fo']}}}margin-left"] == "36pt"
    assert properties.attrib[f"{{{ODF['fo']}}}margin-right"] == "108pt"
    assert properties.attrib[f"{{{ODF['fo']}}}margin-top"] == "72pt"
    assert properties.attrib[f"{{{ODF['fo']}}}margin-bottom"] == "54pt"
    assert properties.attrib[f"{{{ODF['style']}}}page-usage"] == "mirrored"
    page_layout = styles.find(".//style:page-layout", ODF)
    assert page_layout is not None
    assert f"{{{ODF['style']}}}page-usage" not in page_layout.attrib

    master = styles.find(".//style:master-page", ODF)
    assert master is not None
    expected = {
        "header": "odd-header",
        "header-left": "even-header",
        "footer": "odd-footer",
        "footer-left": "even-footer",
    }
    for local_name, text in expected.items():
        node = master.find(f"style:{local_name}", ODF)
        assert node is not None and text in _itertext(node)

    body_text = _itertext(content)
    assert all(text not in body_text for text in expected.values())
    assert "frame-content" in body_text
    assert "Frame:text;anchoredplacementreflowedinsourceorder" in body_text
    header_rows = content.find(".//table:table-header-rows", ODF)
    assert header_rows is not None
    repeated_rows = header_rows.findall("table:table-row", ODF)
    assert len(repeated_rows) == 2
    assert [_itertext(row) for row in repeated_rows] == ["head-one", "head-two"]
    table = content.find(".//table:table", ODF)
    assert table is not None
    ordinary_rows = table.findall("table:table-row", ODF)
    assert len(ordinary_rows) == 1
    assert _itertext(ordinary_rows[0]) == "body-cell"
    breaks = content.findall(".//text:p[@text:style-name='PageBreak']", ODF)
    assert len(breaks) == 2


def test_odt_rejects_hostile_geometry_and_reflows_nonparagraph_page_content() -> None:
    invalid = PageVariantGeometry(
        width_twips=True,
        height_twips=15_840,
        margin_left_twips=0,
        margin_right_twips=0,
        margin_top_twips=0,
        margin_bottom_twips=0,
        valid=True,
    )
    layout = PageLayout(index=1, odd=invalid, valid=True)
    frame = Frame(blocks=[_p("nested-frame")])
    frame.content_kind = []  # type: ignore[assignment]
    frame.placement = {}  # type: ignore[assignment]
    document = Document(
        "hostile.sam",
        "windows-1252",
        page_layouts=[layout],
        blocks=[
            Header(
                blocks=[frame],
                placement="odd",
                origin="layout",
                layout_index=1,
            )
        ],
    )

    payload = odt.render(document)
    styles = _zip_xml(payload, "styles.xml")
    content = _zip_xml(payload, "content.xml")
    properties = styles.find(".//style:page-layout-properties", ODF)

    assert properties is not None
    assert properties.attrib[f"{{{ODF['fo']}}}page-width"] == "612pt"
    assert properties.attrib[f"{{{ODF['fo']}}}page-height"] == "792pt"
    assert properties.attrib[f"{{{ODF['fo']}}}margin-left"] == "72pt"
    assert styles.find(".//style:header", ODF) is None
    text = _itertext(content)
    assert "Header:odd/rightpages" in text
    assert "Frame:unknown;unknownplacementreflowedinsourceorder" in text
    assert "nested-frame" in text


def test_odt_single_sided_furniture_emits_an_explicit_blank_opposite_side() -> None:
    document = _layout_document()
    document.blocks = [
        block
        for block in document.blocks
        if not (
            isinstance(block, Header | Footer) and block.placement == "even"
        )
    ]

    styles = _zip_xml(odt.render(document), "styles.xml")
    master = styles.find(".//style:master-page", ODF)
    assert master is not None
    header_left = master.find("style:header-left", ODF)
    footer_left = master.find("style:footer-left", ODF)
    assert header_left is not None and header_left.find("text:p", ODF) is not None
    assert footer_left is not None and footer_left.find("text:p", ODF) is not None


def test_odt_bounds_self_referential_frame_content() -> None:
    frame = Frame(content_kind="text", placement="anchored")
    frame.blocks.append(frame)
    document = Document("cycle.sam", "windows-1252", blocks=[frame])

    first = odt.render(document)
    second = odt.render(document)

    assert first == second
    text = _itertext(_zip_xml(first, "content.xml"))
    assert "Frame:text;anchoredplacementreflowedinsourceorder" in text
    assert "Nestedcontentomitted:repeatedorcyclicblockreference" in text


@pytest.mark.parametrize("blocks", [object(), [object()]])
def test_odt_hostile_table_cell_content_is_visible_and_following_text_survives(
    blocks: object,
) -> None:
    cell = TableCell()
    cell.blocks = blocks  # type: ignore[assignment]
    document = Document(
        "hostile-cell.sam",
        "windows-1252",
        blocks=[Table(rows=[TableRow(cells=[cell])]), _p("AFTER")],
    )

    rendered = _itertext(_zip_xml(odt.render(document), "content.xml"))

    assert "Invalidorrepeatedtablecellcontentomitted" in rendered
    assert "AFTER" in rendered


@pytest.mark.parametrize("mutation", ["style", "object-text", "bytes-text"])
def test_odt_hostile_text_run_fields_do_not_stop_following_content(
    mutation: str,
) -> None:
    run = TextRun("BEFORE")
    if mutation == "style":
        run.style = object()  # type: ignore[assignment]
    elif mutation == "object-text":
        run.text = object()  # type: ignore[assignment]
    else:
        run.text = b"BYTES"  # type: ignore[assignment]
    document = Document(
        "hostile-run.sam",
        "windows-1252",
        blocks=[Paragraph(runs=[run]), _p("AFTER")],
    )

    rendered = _itertext(_zip_xml(odt.render(document), "content.xml"))

    assert "AFTER" in rendered
    assert "BYTES" in rendered or "Invalidtextrunomitted" in rendered


@pytest.mark.skipif(
    not DOCX_AVAILABLE,
    reason="python-docx extra not installed",
)
def test_docx_uses_validated_geometry_native_page_content_and_reflowed_frames() -> None:
    document = _layout_document()

    first = docx.render(document)
    second = docx.render(document)

    assert first == second
    with ZipFile(BytesIO(first)) as archive:
        names = archive.namelist()
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert all(not name.startswith(("/", "../")) and "/../" not in name for name in names)
        for name in names:
            if name.endswith(".rels"):
                relationships = ET.fromstring(archive.read(name))
                assert all(
                    item.attrib.get("TargetMode", "").casefold() != "external"
                    for item in relationships
                )

        main = ET.fromstring(archive.read("word/document.xml"))
        settings = ET.fromstring(archive.read("word/settings.xml"))
        rels = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        relationship_targets = {
            item.attrib["Id"]: item.attrib["Target"] for item in rels
        }
        sect = main.find(f".//{{{WORD}}}sectPr")
        assert sect is not None

        extracted_native: dict[tuple[str, str], str] = {}
        for kind in ("header", "footer"):
            for reference in sect.findall(f"{{{WORD}}}{kind}Reference"):
                variant = reference.attrib[f"{{{WORD}}}type"]
                relationship_id = reference.attrib[f"{{{REL}}}id"]
                target = relationship_targets[relationship_id]
                part = target if target.startswith("word/") else f"word/{target}"
                extracted_native[(kind, variant)] = _itertext(
                    ET.fromstring(archive.read(part))
                )

    assert extracted_native == {
        ("header", "default"): "odd-header",
        ("header", "even"): "even-header",
        ("footer", "default"): "odd-footer",
        ("footer", "even"): "even-footer",
    }
    assert settings.find(f".//{{{WORD}}}evenAndOddHeaders") is not None
    page_size = sect.find(f"{{{WORD}}}pgSz")
    page_margin = sect.find(f"{{{WORD}}}pgMar")
    assert page_size is not None and page_margin is not None
    assert page_size.attrib[f"{{{WORD}}}w"] == "11908"
    assert page_size.attrib[f"{{{WORD}}}h"] == "16833"
    assert page_margin.attrib[f"{{{WORD}}}left"] == "720"
    assert page_margin.attrib[f"{{{WORD}}}right"] == "2160"
    assert page_margin.attrib[f"{{{WORD}}}top"] == "1440"
    assert page_margin.attrib[f"{{{WORD}}}bottom"] == "1080"

    body_text = _itertext(main)
    assert all(value not in body_text for value in extracted_native.values())
    assert "frame-content" in body_text
    assert "Frame: text; anchored placement reflowed in source order" in body_text
    assert len(main.findall(f".//{{{WORD}}}br[@{{{WORD}}}type='page']")) == 2
    assert len(main.findall(f".//{{{WORD}}}trPr/{{{WORD}}}tblHeader")) == 2


@pytest.mark.skipif(
    not DOCX_AVAILABLE,
    reason="python-docx extra not installed",
)
def test_docx_rejects_hostile_geometry_and_reflows_nonparagraph_page_content() -> None:
    invalid = PageVariantGeometry(
        width_twips=31_681,
        height_twips=15_840,
        margin_left_twips=0,
        margin_right_twips=0,
        margin_top_twips=0,
        margin_bottom_twips=0,
        valid=True,
    )
    layout = PageLayout(index=1, odd=invalid, valid=True)
    frame = Frame(blocks=[_p("nested-frame")])
    frame.content_kind = []  # type: ignore[assignment]
    frame.placement = {}  # type: ignore[assignment]
    document = Document(
        "hostile.sam",
        "windows-1252",
        page_layouts=[layout],
        blocks=[
            Header(
                blocks=[frame],
                placement="odd",
                origin="layout",
                layout_index=1,
            )
        ],
    )

    first = docx.render(document)
    second = docx.render(document)
    assert first == second
    main = _zip_xml(first, "word/document.xml")
    sect = main.find(f".//{{{WORD}}}sectPr")
    assert sect is not None
    page_size = sect.find(f"{{{WORD}}}pgSz")
    page_margin = sect.find(f"{{{WORD}}}pgMar")
    assert page_size is not None and page_margin is not None
    assert page_size.attrib[f"{{{WORD}}}w"] == "12240"
    assert page_size.attrib[f"{{{WORD}}}h"] == "15840"
    assert page_margin.attrib[f"{{{WORD}}}left"] == "1440"
    text = _itertext(main)
    assert "Header: odd/right pages" in text
    assert "Frame: unknown; unknown placement reflowed in source order" in text
    assert "nested-frame" in text


@pytest.mark.skipif(
    not DOCX_AVAILABLE,
    reason="python-docx extra not installed",
)
def test_docx_bounds_self_referential_frame_content() -> None:
    frame = Frame(content_kind="text", placement="anchored")
    frame.blocks.append(frame)
    document = Document("cycle.sam", "windows-1252", blocks=[frame])

    first = docx.render(document)
    second = docx.render(document)

    assert first == second
    text = _itertext(_zip_xml(first, "word/document.xml"))
    assert "Frame: text; anchored placement reflowed in source order" in text
    assert "Nested content omitted: repeated or cyclic block reference" in text
