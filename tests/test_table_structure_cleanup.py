from __future__ import annotations

import importlib.util
import re
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from reportlab.lib.enums import TA_LEFT, TA_RIGHT

from amipro_sam.cli import build_parser
from amipro_sam.model import (
    Document,
    Footer,
    Frame,
    Header,
    Paragraph,
    Table,
    TextRun,
    TwipRect,
    UnsupportedObject,
)
from amipro_sam.parser import parse_bytes
from amipro_sam.renderers import docx, html, markdown, odt, pdf, text
from amipro_sam.renderers.table_geometry import table_column_widths


def _canonical_table_document() -> Document:
    source = """[ver]
	4
[sty]

[files]
[charset]
	82
	ANSI (Windows, IBM CP 1252)
[frm]
	1
	524288
	0
	0
	6000
	3000
	[tbl]
		2 2 300 0 600 0 4 43 43
		[h]
			0 300 0 16 0 0 0
			1 300 0 2 0 0 0
		[e]
		[w]
			0 300 0 2 0
			1 900 0 2 0
		[e]
		[data]
			0 0 384 1 2 0 0 0 0 0 0 0
wide heading
>
			0 1 128 0 1 0 0 0 0 0 0 0
>
			1 0 8 0 0 0 0 0 0 0 0 0
left
>
			1 1 16 0 0 0 0 0 0 0 0 0
right
>
		[tble]
[edoc]
before<:t0>after
>
"""
    return parse_bytes(source.replace("\n", "\r\n").encode("cp1252"))


def _paragraph(value: str) -> Paragraph:
    return Paragraph(runs=[TextRun(value)])


def _structure_document() -> Document:
    return Document(
        "structure.sam",
        "cp1252",
        blocks=[
            Frame(
                blocks=[_paragraph("frame text")],
                content_kind="text",
                placement="anchored",
                region="body",
                unknown_flag_bits=0x100000,
                bounds=TwipRect(0, 0, 1440, 1440, valid=True),
            ),
            Header(
                blocks=[_paragraph("header text")],
                placement="odd",
                origin="body",
            ),
            Footer(blocks=[], placement="even", origin="body"),
        ],
    )


def _odt_text(payload: bytes) -> str:
    with ZipFile(BytesIO(payload)) as archive:
        return "".join(ET.fromstring(archive.read("content.xml")).itertext())


def _docx_text(payload: bytes) -> str:
    with ZipFile(BytesIO(payload)) as archive:
        return "".join(ET.fromstring(archive.read("word/document.xml")).itertext())


def _pdf_text(payload: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(payload)).pages)


def test_cli_accepts_structure_label_audit_mode() -> None:
    arguments = build_parser().parse_args(
        ["convert", "sample.sam", "--format", "text", "--show-structure-labels"]
    )

    assert arguments.show_structure_labels is True


def test_canonical_table_metadata_replaces_body_fallback_atomically() -> None:
    document = _canonical_table_document()
    frame = next(block for block in document.blocks if isinstance(block, Frame))
    table = next(block for block in frame.blocks if isinstance(block, Table))

    assert table.definition is not None
    assert (table.definition.declared_rows, table.definition.declared_columns) == (2, 2)
    assert table.definition.reserved_fields == (43, 43)
    assert [(item.index, item.width_twips) for item in table.columns] == [
        (0, 300),
        (1, 900),
    ]
    assert table.rows[0].is_header is True
    assert table.rows[0].cells[0].column_span == 2
    assert [cell.alignment for cell in table.rows[1].cells] == ["left", "right"]
    assert not any(
        isinstance(block, UnsupportedObject) and block.kind == "table fields"
        for block in frame.blocks
    )
    assert not any(item.code == "table-fields-opaque" for item in document.diagnostics)
    assert any(item.code == "table-formatting-partial" for item in document.diagnostics)
    assert "Unsupported table fields" not in text.render(document).decode()


def test_table_widths_are_normalized_and_used_by_html_and_pdf() -> None:
    document = _canonical_table_document()
    frame = next(block for block in document.blocks if isinstance(block, Frame))
    table = next(block for block in frame.blocks if isinstance(block, Table))
    widths = table_column_widths(table, 2, 4_000)

    assert sum(widths) == 4_000
    assert widths[1] / widths[0] == pytest.approx(3, rel=0.002)
    html_output = html.render(document, include_warnings=False).decode()
    assert "<colgroup>" in html_output
    percentages = [
        float(value)
        for value in re.findall(r'<col style="width:([0-9.]+)%">', html_output)
    ]
    assert percentages == pytest.approx([25, 75], rel=0.002)

    flowable = pdf._table_flowable(document, table)
    assert sum(flowable._argW) == pytest.approx(  # type: ignore[attr-defined]
        pdf._page_spec(document).body_width - 12
    )
    assert flowable._argW[1] / flowable._argW[0] == pytest.approx(3, rel=0.002)  # type: ignore[attr-defined]
    assert flowable._cellStyles[1][0].alignment == "LEFT"  # type: ignore[attr-defined]
    assert flowable._cellStyles[1][1].alignment == "RIGHT"  # type: ignore[attr-defined]
    assert flowable._cellvalues[1][0][0].style.alignment == TA_LEFT  # type: ignore[attr-defined]
    assert flowable._cellvalues[1][1][0].style.alignment == TA_RIGHT  # type: ignore[attr-defined]

    with ZipFile(BytesIO(odt.render(document))) as archive:
        odt_root = ET.fromstring(archive.read("content.xml"))
    odt_widths = [
        float(next(iter(element.attrib.values())).removesuffix("pt"))
        for element in odt_root.iter()
        if element.tag.endswith("table-column-properties")
    ]
    assert odt_widths[1] / odt_widths[0] == pytest.approx(3, rel=0.002)


@pytest.mark.skipif(
    importlib.util.find_spec("docx") is None,
    reason="python-docx extra not installed",
)
def test_docx_uses_normalized_table_widths() -> None:
    document = _canonical_table_document()
    with ZipFile(BytesIO(docx.render(document))) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    widths = [
        int(next(iter(element.attrib.values())))
        for element in root.iter()
        if element.tag.endswith("gridCol")
    ]

    assert len(widths) == 2
    assert widths[1] / widths[0] == pytest.approx(3, rel=0.002)


@pytest.mark.parametrize("renderer", [text.render, markdown.render, html.render])
def test_textual_structure_labels_are_clean_by_default_and_opt_in(renderer: object) -> None:
    document = _structure_document()
    clean = renderer(document).decode("utf-8")  # type: ignore[operator]
    labelled = renderer(  # type: ignore[operator]
        document, show_structure_labels=True
    ).decode("utf-8")

    assert "frame text" in clean and "header text" in clean
    assert "Frame" not in clean and "Header" not in clean and "Footer" not in clean
    assert "Frame" in labelled and "Header" in labelled and "Footer" in labelled


def test_empty_furniture_with_invalid_geometry_stays_out_of_body_text() -> None:
    footer = Footer(
        blocks=[],
        placement="odd",
        origin="layout",
        frame=Frame(
            blocks=[],
            content_kind="text",
            placement="repeating",
            region="footer",
            bounds=TwipRect(0, 100, 1000, 100, valid=False),
        ),
    )
    document = Document("empty-footer.sam", "cp1252", blocks=[footer])

    assert text.render(document) == b""
    assert b"Footer: odd/right pages" in text.render(
        document, show_structure_labels=True
    )


def test_pdf_and_odt_structure_labels_are_clean_by_default_and_opt_in() -> None:
    document = _structure_document()
    for renderer, extractor in ((pdf.render, _pdf_text), (odt.render, _odt_text)):
        clean = extractor(renderer(document))
        labelled = extractor(renderer(document, show_structure_labels=True))
        compact_clean = clean.replace(" ", "")
        assert "frametext" in compact_clean and "headertext" in compact_clean
        assert "Frame:" not in clean and "Header:" not in clean and "Footer:" not in clean
        assert "Frame:" in labelled and "Header:" in labelled and "Footer:" in labelled


@pytest.mark.skipif(
    importlib.util.find_spec("docx") is None,
    reason="python-docx extra not installed",
)
def test_docx_structure_labels_are_clean_by_default_and_opt_in() -> None:
    document = _structure_document()
    clean = _docx_text(docx.render(document))
    labelled = _docx_text(docx.render(document, show_structure_labels=True))

    assert "frame text" in clean and "header text" in clean
    assert "Frame:" not in clean and "Header:" not in clean and "Footer:" not in clean
    assert "Frame:" in labelled and "Header:" in labelled and "Footer:" in labelled
