from __future__ import annotations

import re

import pytest
from reportlab.platypus import PageBreak as ReportLabPageBreak
from reportlab.platypus import Paragraph as ReportLabParagraph
from reportlab.platypus import Table as ReportLabTable

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
from amipro_sam.renderers import html, pdf


def _p(text: str, *, break_before: bool = False) -> Paragraph:
    return Paragraph(runs=[TextRun(text)], page_break_before=break_before)


def _geometry(
    *,
    side: str = "odd",
    top: int = 1_440,
    right: int = 2_160,
    bottom: int = 1_080,
    left: int = 720,
) -> PageVariantGeometry:
    return PageVariantGeometry(
        side=side,  # type: ignore[arg-type]
        height_twips=16_833,
        width_twips=11_908,
        margin_left_twips=left,
        margin_bottom_twips=bottom,
        margin_top_twips=top,
        margin_right_twips=right,
        valid=True,
        page_rect=TwipRect(0, 0, 11_908, 16_833, valid=True),
        content_rect=TwipRect(
            left,
            top,
            11_908 - right,
            16_833 - bottom,
            valid=True,
        ),
    )


def _document(*blocks: object, even: PageVariantGeometry | None = None) -> Document:
    layout = PageLayout(
        index=7,
        name="custom",
        odd=_geometry(),
        even=even,
        valid=True,
    )
    return Document(
        "geometry.sam",
        "windows-1252",
        page_layouts=[layout],
        blocks=list(blocks),  # type: ignore[arg-type]
    )


def _pdf_page_count(payload: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b", payload))


def _story_text(story: list[object]) -> str:
    return "\n".join(
        item.getPlainText()
        for item in story
        if isinstance(item, ReportLabParagraph)
    )


def test_html_emits_validated_base_and_odd_even_page_geometry() -> None:
    even = _geometry(side="even", top=720, right=720, bottom=720, left=1_440)
    payload = html.render(_document(_p("body"), even=even)).decode("utf-8")

    assert (
        "@page{size:8.26944in 11.6896in;"
        "margin:1in 1.5in 0.75in 0.5in}" in payload
    )
    assert "@page:right{margin:1in 1.5in 0.75in 0.5in}" in payload
    assert "@page:left{margin:0.5in 0.5in 0.5in 1in}" in payload
    assert "thead{display:table-header-group}" in payload
    assert "tr{break-inside:avoid;page-break-inside:avoid}" in payload


def test_html_keeps_odd_even_layout_furniture_visible_without_overlap() -> None:
    odd = Header(
        blocks=[_p("odd-header")],
        placement="odd",
        origin="layout",
        layout_index=7,
    )
    even = Header(
        blocks=[_p("even-header")],
        placement="even",
        origin="layout",
        layout_index=7,
    )
    payload = html.render(_document(odd, even, _p("body"))).decode("utf-8")

    assert payload.count("odd-header") == 1
    assert payload.count("even-header") == 1
    assert '<header class="document-header" data-placement="odd">' in payload
    assert '<header class="document-header" data-placement="even">' in payload
    assert '<header class="print-header"' not in payload


def test_html_keeps_all_page_furniture_inline_and_ignores_even_nonalternating_css() -> None:
    header = Header(
        blocks=[_p("all-header")],
        placement="all",
        origin="layout",
        layout_index=7,
    )
    even = _geometry(side="even", top=720, right=720, bottom=720, left=1_440)
    document = _document(header, _p("body"), even=even)
    document.page_layouts[0].non_alternating = True
    payload = html.render(document).decode("utf-8")

    assert payload.count("all-header") == 1
    assert '<header class="document-header" data-placement="all">' in payload
    assert '<header class="print-header"' not in payload
    assert "@page:left" not in payload


def test_pdf_promotes_unique_safe_odd_even_furniture_without_body_duplication() -> None:
    odd = Header(
        blocks=[_p("odd-header")],
        placement="odd",
        origin="layout",
        layout_index=7,
    )
    even = Header(
        blocks=[_p("even-header")],
        placement="even",
        origin="layout",
        layout_index=7,
    )
    footer = Footer(
        blocks=[_p("all-footer")],
        placement="all",
        origin="layout",
        layout_index=7,
    )
    document = _document(odd, even, footer, _p("body"))
    page = pdf._page_spec(document)
    promoted = pdf._promoted_furniture(document, page)

    assert promoted == {id(odd), id(even), id(footer)}
    assert _story_text(pdf._primary_story(document, promoted)) == "body"
    assert pdf._furniture_applies(odd, 1, page)
    assert not pdf._furniture_applies(odd, 2, page)
    assert not pdf._furniture_applies(even, 1, page)
    assert pdf._furniture_applies(even, 2, page)
    assert pdf._furniture_applies(footer, 1, page)
    assert pdf._furniture_applies(footer, 2, page)


@pytest.mark.parametrize("case", ["duplicate", "nonparagraph", "zero-margin"])
def test_pdf_unsafe_layout_furniture_falls_back_inline_without_loss(case: str) -> None:
    header = Header(
        blocks=[_p("kept-header")],
        placement="all",
        origin="layout",
        layout_index=7,
    )
    blocks: list[object] = [header, _p("body")]
    document = _document(*blocks)
    if case == "duplicate":
        blocks.insert(
            1,
            Header(
                blocks=[_p("second-header")],
                placement="odd",
                origin="layout",
                layout_index=7,
            ),
        )
        document.blocks = blocks  # type: ignore[assignment]
    elif case == "nonparagraph":
        header.blocks = [Frame(blocks=[_p("nested-header")])]  # type: ignore[list-item]
    else:
        document.page_layouts[0].odd = _geometry(top=0)

    promoted = pdf._promoted_furniture(document, pdf._page_spec(document))
    story_text = _story_text(pdf._primary_story(document, promoted))

    assert id(header) not in promoted
    assert "Header: all pages" in story_text
    if case == "nonparagraph":
        assert "nested-header" in story_text
    else:
        assert "kept-header" in story_text


def test_pdf_does_not_promote_unbreakable_furniture_off_the_page() -> None:
    value = "A" * 1_000
    header = Header(
        blocks=[_p(value)],
        placement="all",
        origin="layout",
        layout_index=7,
    )
    document = _document(header, _p("body"))

    promoted = pdf._promoted_furniture(document, pdf._page_spec(document))
    story_text = _story_text(pdf._primary_story(document, promoted))

    assert id(header) not in promoted
    assert value in story_text


def test_frames_preserve_nested_content_and_reject_hostile_manual_geometry() -> None:
    frame = Frame(
        blocks=[_p("frame-content")],
        bounds=TwipRect(-200, 100, 5_000, 2_000, valid=True),
    )
    frame.content_kind = []  # type: ignore[assignment]
    frame.placement = {}  # type: ignore[assignment]
    frame.region = object()  # type: ignore[assignment]
    frame.page_number = True  # type: ignore[assignment]
    invalid = _geometry()
    invalid.width_twips = 31_681
    document = _document(frame)
    document.page_layouts[0].odd = invalid

    html_payload = html.render(document).decode("utf-8")
    pdf_payload = pdf.render(document)

    assert "Frame - unknown; unknown; unknown region" in html_payload
    assert "frame-content" in html_payload
    assert pdf_payload.startswith(b"%PDF-")
    assert pdf._page_spec(document).width == 612
    assert "frame-content" in _story_text(pdf._primary_story(document))


def test_html_does_not_apply_pathologically_narrow_frame_width() -> None:
    frame = Frame(
        blocks=[_p("invented readable frame")],
        content_kind="text",
        placement="fixed-page",
        bounds=TwipRect(0, 0, 1, 1_440, valid=True),
    )

    payload = html.render(_document(frame)).decode("utf-8")

    assert 'class="document-frame"' in payload
    assert 'style="width:' not in payload
    assert "invented readable frame" in payload


def test_pdf_explicit_breaks_preserve_internal_blanks_but_trim_edge_artifacts() -> None:
    document = _document(
        PageBreak(),
        _p("first", break_before=True),
        PageBreak(),
        PageBreak(),
        _p("last"),
        PageBreak(),
    )
    story = pdf._primary_story(document)

    assert sum(isinstance(item, ReportLabPageBreak) for item in story) == 2
    assert _pdf_page_count(pdf.render(document)) == 3


def test_pdf_fallback_repeats_leading_table_header_rows() -> None:
    table = Table(
        rows=[
            TableRow(cells=[TableCell(blocks=[_p("header-one")])], is_header=True),
            TableRow(cells=[TableCell(blocks=[_p("header-two")])], is_header=True),
            TableRow(cells=[TableCell(blocks=[_p("body")])]),
        ]
    )

    rendered = pdf._fallback_table_flowable(table)

    assert isinstance(rendered, ReportLabTable)
    assert rendered.repeatRows == 2


def test_pdf_reflows_table_on_minimum_height_page_without_losing_rows() -> None:
    geometry = PageVariantGeometry(
        side="odd",
        width_twips=1_440,
        height_twips=1_440,
        margin_left_twips=360,
        margin_right_twips=360,
        margin_top_twips=360,
        margin_bottom_twips=360,
        valid=True,
        page_rect=TwipRect(0, 0, 1_440, 1_440, True),
        content_rect=TwipRect(360, 360, 1_080, 1_080, True),
    )
    table = Table(
        rows=[
            TableRow(cells=[TableCell(blocks=[_p("H")])], is_header=True),
            TableRow(cells=[TableCell(blocks=[_p("B")])]),
        ]
    )
    document = _document(table, _p("AFTER"))
    document.page_layouts[0].odd = geometry

    payload = pdf.render(document)

    assert payload.startswith(b"%PDF-")
    assert "H" in _story_text(pdf._fallback_story(document))
    assert "B" in _story_text(pdf._fallback_story(document))
    assert "AFTER" in _story_text(pdf._fallback_story(document))


def test_hostile_table_rows_have_visible_bounded_fallbacks() -> None:
    table = Table()
    table.rows = object()  # type: ignore[assignment]
    document = _document(table)

    html_payload = html.render(document).decode()
    pdf_story = pdf._primary_story(document)

    assert "Invalid table rows omitted" in html_payload
    assert "Invalid table rows omitted" in _story_text(pdf_story)


@pytest.mark.parametrize("blocks", [object(), [object()]])
def test_hostile_table_cell_content_is_visible_and_does_not_stop_rendering(
    blocks: object,
) -> None:
    cell = TableCell()
    cell.blocks = blocks  # type: ignore[assignment]
    table = Table(rows=[TableRow(cells=[cell])])
    document = _document(table, _p("AFTER"))

    html_payload = html.render(document).decode()
    pdf_story = pdf._primary_story(document)
    table_flowable = next(item for item in pdf_story if isinstance(item, ReportLabTable))
    cell_text = " ".join(
        item.getPlainText()
        for item in table_flowable._cellvalues[0][0]
        if isinstance(item, ReportLabParagraph)
    )

    assert "Invalid table cell content omitted" in html_payload
    assert "AFTER" in html_payload
    assert "Invalid table cell content omitted" in cell_text
    assert "AFTER" in _story_text(pdf_story)


def test_pathologically_narrow_html_frame_reflows_without_literal_width() -> None:
    frame = Frame(
        blocks=[_p("A" * 5_000)],
        content_kind="text",
        placement="fixed-page",
        bounds=TwipRect(0, 0, 1, 1_440, valid=True),
    )
    payload = html.render(_document(frame)).decode()

    assert 'style="width:' not in payload
    assert "A" * 100 in payload


@pytest.mark.parametrize("mutation", ["style", "object-text", "bytes-text"])
def test_hostile_text_run_fields_are_visible_and_do_not_stop_rendering(
    mutation: str,
) -> None:
    run = TextRun("BEFORE")
    if mutation == "style":
        run.style = object()  # type: ignore[assignment]
    elif mutation == "object-text":
        run.text = object()  # type: ignore[assignment]
    else:
        run.text = b"BYTES"  # type: ignore[assignment]
    document = _document(Paragraph(runs=[run]), _p("AFTER"))

    html_payload = html.render(document).decode()
    pdf_payload = _story_text(pdf._primary_story(document))

    assert "AFTER" in html_payload and "AFTER" in pdf_payload
    if mutation == "bytes-text":
        assert "BYTES" in html_payload and "BYTES" in pdf_payload
    else:
        assert "Invalid text run omitted" in html_payload
        assert "Invalid text run omitted" in pdf_payload


def test_pdf_fallback_keeps_repeating_page_furniture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    header = Header(
        blocks=[_p("repeated-header")],
        placement="all",
        origin="layout",
        layout_index=7,
    )
    document = _document(header, _p("first"), PageBreak(), _p("second"))
    pages: list[int] = []
    original_factory = pdf._page_furniture_callback

    def fail_primary(_document: Document) -> list[object]:
        raise pdf.LayoutError("force conservative renderer")

    def tracking_factory(document: Document, page: object):
        callback = original_factory(document, page)  # type: ignore[arg-type]

        def tracked(canvas: object, template: object) -> None:
            pages.append(canvas.getPageNumber())  # type: ignore[attr-defined]
            callback(canvas, template)  # type: ignore[arg-type]

        return tracked

    monkeypatch.setattr(pdf, "_primary_story", fail_primary)
    monkeypatch.setattr(pdf, "_page_furniture_callback", tracking_factory)

    payload = pdf.render(document)

    assert _pdf_page_count(payload) == 2
    assert pages == [1, 2]
