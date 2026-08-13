from __future__ import annotations

import builtins
import importlib.util
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZIP_STORED, ZipFile

import pytest

from amipro_sam.errors import RenderError
from amipro_sam.model import (
    Annotation,
    CharacterStyle,
    Document,
    Footer,
    Footnote,
    Header,
    Image,
    PageBreak,
    Paragraph,
    StyleDefinition,
    Table,
    TableCell,
    TableRow,
    TextRun,
    UnsupportedObject,
)
from amipro_sam.renderers import docx, odt, pdf


def _document() -> Document:
    return Document(
        source_name="untrusted <source>.sam",
        encoding="windows-1252",
        metadata={"author": "Must not leak into package metadata"},
        styles={
            "Centered": StyleDefinition(
                name="Centered",
                character=CharacterStyle(
                    italic=True,
                    font_family="Arial",
                    font_size_pt=13,
                    color="#123456",
                ),
                alignment="center",
                space_after_pt=8,
                line_spacing=1.25,
            )
        },
        blocks=[
            Paragraph(
                runs=[
                    TextRun(
                        "<script>alert('x') & safe</script>\tline\nnext",
                        CharacterStyle(bold=True, underline=True, superscript=True),
                    )
                ],
                style_name="Centered",
                page_break_before=True,
                keep_with_next=True,
            ),
            Paragraph(runs=[TextRun("First")], list_kind="number"),
            Paragraph(runs=[TextRun("Second")], list_kind="number"),
            Table(
                rows=[
                    TableRow(
                        cells=[
                            TableCell(
                                blocks=[Paragraph(runs=[TextRun("Heading")])],
                                column_span=2,
                            )
                        ],
                        is_header=True,
                    ),
                    TableRow(
                        cells=[
                            TableCell(blocks=[Paragraph(runs=[TextRun("A")])]),
                            TableCell(blocks=[Paragraph(runs=[TextRun("B")])]),
                        ]
                    ),
                ]
            ),
            Image(reference="../../do-not-open.png", alt_text="A & B <preview>"),
            UnsupportedObject(kind="OLE", description="macro <object> & data"),
            PageBreak(),
            Paragraph(runs=[TextRun("Final page")]),
        ],
    )


def test_pdf_is_deterministic_safe_and_structured() -> None:
    document = _document()

    first = pdf.render(document)
    second = pdf.render(document)

    assert first == second
    assert first.startswith(b"%PDF-")
    assert b"/JavaScript" not in first
    assert b"/EmbeddedFile" not in first
    assert b"/URI" not in first
    assert b"amipro-sam-toolkit" in first


def test_pdf_text_and_pages_are_recoverable_when_pypdf_is_available() -> None:
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(BytesIO(pdf.render(_document())))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    # A page-break-before property on the first paragraph has no preceding
    # page to finish; paged renderers now normalize that boundary artifact.
    assert len(reader.pages) == 2
    assert "<script>alert('x') & safe</script>" in text
    assert "Heading" in text
    assert "A & B <preview>" in text
    assert "Unsupported OLE: macro <object> & data" in text
    assert "Final page" in text


def test_pdf_handles_extreme_indents_and_leading_breaks() -> None:
    document = Document(
        source_name="synthetic.sam",
        encoding="windows-1252",
        styles={
            "Large title": StyleDefinition(
                name="Large title",
                character=CharacterStyle(font_family="Times New Roman", font_size_pt=36),
                alignment="center",
                line_spacing=2.4583333333333335,
            )
        },
        blocks=[
            Paragraph(
                runs=[
                    TextRun("\n" * 7, CharacterStyle(font_size_pt=10)),
                    TextRun("Visible title", CharacterStyle(font_size_pt=36)),
                ],
                style_name="Large title",
                alignment="center",
                line_spacing=2.4583333333333335,
            ),
            *[
                Paragraph(runs=[TextRun(f"Filler paragraph {index}.")])
                for index in range(55)
            ],
            Paragraph(
                runs=[TextRun("Readable narrow text " * 100)],
                left_indent_in=7.269444444444445,
                first_line_indent_in=6.508333333333334,
                alignment="justify",
            ),
        ],
    )

    first = pdf.render(document)
    second = pdf.render(document)

    assert first == second
    assert first.startswith(b"%PDF-")


def test_paged_renderers_reflow_typed_notes_headers_and_footers() -> None:
    document = Document(
        "containers.sam",
        "windows-1252",
        blocks=[
            Paragraph(runs=[TextRun("Body before")]),
            Annotation(blocks=[Paragraph(runs=[TextRun("Annotation text")])]),
            Footnote(blocks=[Paragraph(runs=[TextRun("Footnote text")])]),
            Header(
                blocks=[Paragraph(runs=[TextRun("Header text")])],
                placement="odd",
                origin="layout",
            ),
            Footer(
                blocks=[Paragraph(runs=[TextRun("Footer text")])],
                placement="even",
                origin="layout",
            ),
            Paragraph(runs=[TextRun("Body after")]),
        ],
    )

    pdf_output = pdf.render(document)
    odt_output = odt.render(document)

    assert pdf_output.startswith(b"%PDF-")
    with ZipFile(BytesIO(odt_output)) as archive:
        odt_text = "".join(ET.fromstring(archive.read("content.xml")).itertext())
    for expected in (
        "[Annotation]",
        "Annotation text",
        "[Footnote]",
        "Footnote text",
        "[Header: odd/right pages]",
        "Header text",
        "[Footer: even/left pages]",
        "Footer text",
    ):
        assert expected.replace(" ", "") in odt_text

    if importlib.util.find_spec("pypdf") is not None:
        import pypdf

        extracted = "\n".join(
            page.extract_text() or ""
            for page in pypdf.PdfReader(BytesIO(pdf_output)).pages
        )
        assert "Annotation text" in extracted
        assert "Footnote text" in extracted
        assert "Header text" in extracted
        assert "Footer text" in extracted


@pytest.mark.skipif(
    importlib.util.find_spec("docx") is None,
    reason="python-docx extra not installed",
)
def test_docx_reflows_typed_note_and_page_content() -> None:
    document = Document(
        "containers.sam",
        "windows-1252",
        blocks=[
            Annotation(blocks=[Paragraph(runs=[TextRun("Annotation text")])]),
            Footnote(blocks=[Paragraph(runs=[TextRun("Footnote text")])]),
            Header(blocks=[Paragraph(runs=[TextRun("Header text")])], placement="all"),
            Footer(blocks=[Paragraph(runs=[TextRun("Footer text")])], placement="all"),
        ],
    )

    with ZipFile(BytesIO(docx.render(document))) as archive:
        content = archive.read("word/document.xml")
        extracted = "".join(ET.fromstring(content).itertext())

    assert "[Annotation]" in extracted and "Annotation text" in extracted
    assert "[Footnote]" in extracted and "Footnote text" in extracted
    assert "[Header: all pages]" in extracted and "Header text" in extracted
    assert "[Footer: all pages]" in extracted and "Footer text" in extracted


def test_pdf_splits_a_table_row_taller_than_one_page() -> None:
    document = Document(
        source_name="synthetic-table.sam",
        encoding="windows-1252",
        blocks=[
            Table(
                rows=[
                    TableRow(
                        cells=[
                            TableCell(blocks=[Paragraph(runs=[TextRun("Heading one")])]),
                            TableCell(blocks=[Paragraph(runs=[TextRun("Heading two")])]),
                            TableCell(blocks=[Paragraph(runs=[TextRun("Heading three")])]),
                        ],
                        is_header=True,
                    ),
                    TableRow(
                        cells=[
                            TableCell(
                                blocks=[
                                    Paragraph(
                                        runs=[TextRun("Very tall cell content " * 1_000)]
                                    )
                                ]
                            ),
                            TableCell(blocks=[Paragraph(runs=[TextRun("Short cell")])]),
                            TableCell(blocks=[Paragraph(runs=[TextRun("Final cell")])]),
                        ]
                    ),
                ]
            )
        ],
    )

    output = pdf.render(document)

    assert output.startswith(b"%PDF-")
    assert len(output) > 1_000
    if importlib.util.find_spec("pypdf") is not None:
        import pypdf

        reader = pypdf.PdfReader(BytesIO(output))
        assert len(reader.pages) > 1
        assert all(
            "Heading one" in (page.extract_text() or "")
            for page in reader.pages
        )


def test_pdf_plain_layout_fallback_keeps_conversion_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_primary(_document: Document) -> list[object]:
        raise pdf.LayoutError("simulated ReportLab layout failure")

    monkeypatch.setattr(pdf, "_primary_story", fail_primary)

    output = pdf.render(_document())

    assert output.startswith(b"%PDF-")
    assert b"/JavaScript" not in output


def test_pdf_visibly_reflows_table_beyond_safe_grid_width() -> None:
    table = Table(
        rows=[
            TableRow(
                cells=[
                    TableCell(blocks=[Paragraph(runs=[TextRun(str(index))])])
                    for index in range(257)
                ]
            )
        ]
    )

    document = Document(
        "synthetic.sam",
        "windows-1252",
        blocks=[table, Paragraph(runs=[TextRun("AFTER")])],
    )

    story = pdf._primary_story(document)

    assert "Table cells omitted at safe 256-column limit" in story[0].getPlainText()
    assert "AFTER" in story[1].getPlainText()
    assert pdf.render(document).startswith(b"%PDF-")


def test_odt_package_is_valid_deterministic_and_self_contained() -> None:
    first = odt.render(_document())
    second = odt.render(_document())

    assert first == second
    with ZipFile(BytesIO(first)) as archive:
        assert archive.namelist() == [
            "mimetype",
            "content.xml",
            "styles.xml",
            "meta.xml",
            "settings.xml",
            "META-INF/manifest.xml",
        ]
        assert archive.infolist()[0].compress_type == ZIP_STORED
        assert archive.read("mimetype") == b"application/vnd.oasis.opendocument.text"
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())

        parsed = {
            name: ET.fromstring(archive.read(name))
            for name in archive.namelist()
            if name.endswith(".xml")
        }
        content = archive.read("content.xml")
        text = "".join(parsed["content.xml"].itertext())
        names = {element.tag.rsplit("}", 1)[-1] for element in parsed["content.xml"].iter()}

    assert b"&lt;script&gt;" in content
    assert "script" not in names
    # ODF represents preserved spaces with empty ``text:s`` elements, so
    # ElementTree.itertext() intentionally omits them.
    assert "<script>alert('x')&safe</script>" in text
    assert "Heading" in text
    assert "A&B<preview>" in text
    assert "sourcereferencenotopened:../../do-not-open.png" in text
    assert "UnsupportedOLE:macro<object>&data" in text
    assert {"table", "table-cell", "list", "list-item"}.issubset(names)
    assert b'fo:break-before="page"' in content
    assert b'fo:font-weight="bold"' in content
    assert b'style:text-underline-style="solid"' in content
    assert b'table:number-columns-spanned="2"' in content
    assert b"xlink:" not in content


def test_docx_missing_dependency_error_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def reject_docx(name: str, *args: object, **kwargs: object) -> object:
        if name == "docx" or name.startswith("docx."):
            raise ImportError("simulated missing optional package")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_docx)
    with pytest.raises(RenderError, match=r"optional 'python-docx'.*pip install"):
        docx.render(_document())


@pytest.mark.skipif(
    importlib.util.find_spec("docx") is None,
    reason="python-docx extra not installed",
)
def test_docx_is_valid_deterministic_scrubbed_and_has_real_document_objects() -> None:
    first = docx.render(_document())
    second = docx.render(_document())

    assert first == second
    with ZipFile(BytesIO(first)) as archive:
        names = archive.namelist()
        lowered_names = [name.casefold() for name in names]
        assert "word/document.xml" in names
        assert "word/numbering.xml" in names
        assert "docprops/custom.xml" not in lowered_names
        assert not any(name.startswith("customxml/") for name in lowered_names)
        assert not any(name.startswith("docprops/thumbnail.") for name in lowered_names)
        assert not any("vbaproject" in name for name in lowered_names)
        assert not any(name.startswith("word/embeddings/") for name in lowered_names)
        assert not any(name.startswith("word/activex/") for name in lowered_names)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())

        for name in names:
            if name.endswith((".xml", ".rels")):
                ET.fromstring(archive.read(name))
            if name.endswith(".rels"):
                assert b'TargetMode="External"' not in archive.read(name)

        content = archive.read("word/document.xml")
        core = archive.read("docProps/core.xml")
        app = archive.read("docProps/app.xml")
        text = "".join(ET.fromstring(content).itertext())

    assert "<script>alert('x') & safe</script>" in text
    assert "Heading" in text
    assert "A & B <preview>" in text
    assert "source reference not opened: ../../do-not-open.png" in text
    assert "Unsupported OLE: macro <object> & data" in text
    assert b"<w:tbl" in content
    assert b"<w:gridSpan" in content
    assert b"<w:tblHeader" in content
    assert b'<w:br w:type="page"' in content
    assert b"<w:b" in content
    assert b"<w:u" in content
    assert b"w:rsid" not in content
    assert b"Must not leak" not in core
    assert b"<dc:creator></dc:creator>" in core
    assert b"<cp:lastModifiedBy></cp:lastModifiedBy>" in core
    assert b"dcterms:created" not in core
    assert b"dcterms:modified" not in core
    assert b"amipro-sam-toolkit" in app
    assert b"Microsoft Macintosh Word" not in app
