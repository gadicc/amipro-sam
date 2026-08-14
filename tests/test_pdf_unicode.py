from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image as PillowImage
from reportlab.platypus import Paragraph as ReportLabParagraph

from amipro_sam.model import (
    CharacterStyle,
    Document,
    Paragraph,
    SdwDrawing,
    StyleDefinition,
    Table,
    TableCell,
    TableRow,
    TextRun,
)
from amipro_sam.pdf_unicode import (
    BidiTextFlowable,
    PdfTextBudget,
    base_direction,
    ensure_pdf_fonts,
)
from amipro_sam.renderers import pdf


def _document(*paragraphs: Paragraph) -> Document:
    return Document("invented-unicode.sam", "utf-8", blocks=list(paragraphs))


def _paragraph(text: str, style: CharacterStyle | None = None) -> Paragraph:
    return Paragraph(runs=[TextRun(text, style or CharacterStyle())])


def _poppler_text(payload: bytes, tmp_path: Path) -> str:
    source = tmp_path / "unicode.pdf"
    target = tmp_path / "unicode.txt"
    source.write_bytes(payload)
    subprocess.run(
        ["pdftotext", "-layout", str(source), str(target)],
        check=True,
        capture_output=True,
    )
    return target.read_text(encoding="utf-8")


def test_bundled_font_assets_have_pinned_hashes_and_coverage() -> None:
    root = Path(__file__).parents[1] / "src/amipro_sam/assets/fonts"
    expected = {
        "DejaVuSans.ttf": "8a301f4fc28b4cadd8668f41c61217e200ffd3e069d2912966b5a2903ab09434",
        "DejaVuSans-Bold.ttf": "6b4f83ef68e461c05a8d8b218177936226a32f746044cfc10e4b9351c4a9415d",
        "DejaVuSans-Oblique.ttf": (
            "6c4bf004bd06ad8b16ac3be38627e6cfd7f7da01b6563ddf6d385f227a8f28ac"
        ),
        "DejaVuSans-BoldOblique.ttf": (
            "6d26ecff69d04ad88af75bb046370d6f52d8908a97632cee8cc8682638dc9758"
        ),
        "AmiProPreservationCJK-Regular.ttf": (
            "267a6ba550900fec48fd45d8a4fd5f8941f6cff5db9a0f8b313d3b31966da2c0"
        ),
    }
    for filename, digest in expected.items():
        assert hashlib.sha256((root / filename).read_bytes()).hexdigest() == digest

    ensure_pdf_fonts()
    from reportlab.pdfbase import pdfmetrics

    sans = pdfmetrics.getFont("AmiProSans").face.charWidths
    cjk = pdfmetrics.getFont("AmiProCJK").face.charWidths
    assert all(ord(character) in sans for character in "Café Αθήνα Москва שלום مرحبا")
    assert all(ord(character) in cjk for character in "漢字かなカナ中文한글")
    # The renderer enforces BMP-only even when a face happens to contain an
    # emoji: ReportLab 4.4 does not emit surrogate-pair ToUnicode mappings.
    assert 0x20000 not in cjk


def test_bundled_font_registration_fails_closed_on_a_global_name_collision() -> None:
    script = (
        "from reportlab.pdfbase import pdfmetrics\n"
        "pdfmetrics.registerFont(pdfmetrics.Font("
        "'AmiProSans','Helvetica','WinAnsiEncoding'))\n"
        "from amipro_sam.pdf_unicode import UnicodePdfError, ensure_pdf_fonts\n"
        "try:\n"
        "    ensure_pdf_fonts()\n"
        "except UnicodePdfError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                item
                for item in ("src", os.environ.get("PYTHONPATH", ""))
                if item
            ),
        },
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


def test_ltr_unicode_styles_and_cjk_fallback_extract_logically(tmp_path: Path) -> None:
    styles = [
        CharacterStyle(),
        CharacterStyle(bold=True),
        CharacterStyle(italic=True),
        CharacterStyle(bold=True, italic=True),
    ]
    text = "Latin Café; Greek Αθήνα; Cyrillic Москва; CJK 漢字かなカナ한글"
    payload = pdf.render(_document(*(_paragraph(text, style) for style in styles)))
    extracted = _poppler_text(payload, tmp_path)

    assert payload == pdf.render(_document(*(_paragraph(text, style) for style in styles)))
    assert extracted.count("Latin Café") == 4
    assert extracted.count("Αθήνα") == 4
    assert extracted.count("Москва") == 4
    assert extracted.count("漢字かなカナ한글") == 4


def test_representative_unicode_pdf_rasterizes_with_visible_ink(tmp_path: Path) -> None:
    text = "Café Αθήνα Москва שלום עולם مرحبا بالعالم 漢字かなカナ한글 �"
    source = tmp_path / "raster.pdf"
    prefix = tmp_path / "raster-page"
    source.write_bytes(pdf.render(_document(_paragraph(text))))
    subprocess.run(
        ["pdftoppm", "-singlefile", "-png", "-r", "72", str(source), str(prefix)],
        check=True,
        capture_output=True,
    )

    with PillowImage.open(prefix.with_suffix(".png")) as image:
        grayscale = image.convert("L")
        dark_pixels = sum(grayscale.histogram()[:220])
    assert dark_pixels > 500


def test_mixed_hebrew_arabic_uses_bounded_custom_flowable_and_actualtext(
    tmp_path: Path,
) -> None:
    hebrew = "שלום עולם 123 ABC"
    arabic = "مرحبا بالعالم 123 ABC"
    document = _document(
        _paragraph(hebrew),
        _paragraph(arabic, CharacterStyle(italic=True)),
        _paragraph("أنا أحب Python 3.12 كثيرا"),
    )

    story = pdf._primary_story(document)
    assert all(isinstance(item, BidiTextFlowable) for item in story)
    payload = pdf.render(document)
    qdf = tmp_path / "unicode-qdf.pdf"
    source = tmp_path / "unicode.pdf"
    source.write_bytes(payload)
    subprocess.run(
        ["qpdf", "--qdf", "--object-streams=disable", str(source), str(qdf)],
        check=True,
        capture_output=True,
    )
    qdf_bytes = qdf.read_bytes()
    for logical in (hebrew, arabic):
        actual = ("\ufeff" + logical).encode("utf-16-be").hex().encode()
        assert actual in qdf_bytes

    # Poppler honors ActualText but applies its own bidi presentation to the
    # extracted line.  Pin logical content presence without pretending all
    # extractors expose marked-content replacement strings identically.
    extracted = _poppler_text(payload, tmp_path)
    assert "123 ABC" in extracted
    assert set("שלוםעולם") <= set(extracted)
    assert set("مرحبابالعالم") <= set(extracted)


def test_rtl_paired_punctuation_keeps_each_logical_word_once() -> None:
    from amipro_sam.pdf_unicode import bidiShapedText

    for logical, expected_visual in (
        ("שלום (ABC 123) עולם", "םלוע (ABC 123) םולש"),
        ("السَّلَامُ (ABC 123) عَلَيْكُمْ", None),
    ):
        shaped, _width = bidiShapedText(
            logical,
            "RTL",
            fontName="AmiProSans",
            fontSize=11,
            shaping=True,
        )
        visual = str(shaped)
        assert visual.count("ABC") == 1
        assert visual.count("123)") == 1
        if expected_visual is not None:
            assert visual == expected_visual


def test_unsupported_rtl_coverage_and_mixed_tokens_are_visible_replacements(
    tmp_path: Path,
) -> None:
    logical = "שלום(ABC)עולם السلام 漢字 \u0750 END"
    payload = pdf.render(_document(_paragraph(logical)))
    extracted = " ".join(_poppler_text(payload, tmp_path).split())

    assert extracted.count("�") >= 4
    assert "END" in extracted


def test_reportlab_preimport_does_not_change_rtl_pdf_bytes(tmp_path: Path) -> None:
    outputs: list[bytes] = []
    for index, preimport in enumerate((False, True)):
        target = tmp_path / f"order-{index}.pdf"
        script = (
            ("from reportlab.platypus import Paragraph\n" if preimport else "")
            + "from pathlib import Path\n"
            + "from amipro_sam.model import Document,Paragraph,TextRun\n"
            + "from amipro_sam.renderers import pdf\n"
            + "doc=Document('x.sam','utf-8',blocks=["
            + "Paragraph(runs=[TextRun('السلام عليكم 123 ABC')])])\n"
            + f"Path({str(target)!r}).write_bytes(pdf.render(doc))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[1],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    item
                    for item in ("src", os.environ.get("PYTHONPATH", ""))
                    if item
                ),
            },
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        outputs.append(target.read_bytes())
    assert outputs[0] == outputs[1]


def test_valid_table_cell_text_survives_an_invalid_sibling(tmp_path: Path) -> None:
    table = Table(
        rows=[
            TableRow(
                cells=[
                    TableCell(
                        blocks=[_paragraph("VALID CELL السلام 123"), object()]
                    )
                ]
            )
        ]
    )
    document = Document(
        "table.sam",
        "utf-8",
        blocks=[table, _paragraph("AFTER")],
    )
    extracted = _poppler_text(pdf.render(document), tmp_path)

    assert "VALID CELL" in extracted
    assert "Invalid table cell content omitted" in extracted
    assert "AFTER" in extracted


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            Document(
                "color.sam",
                "utf-8",
                blocks=[
                    Paragraph(
                        runs=[
                            TextRun(
                                "שלום ABC",
                                CharacterStyle(color=object()),
                            )
                        ]
                    ),
                    _paragraph("AFTER"),
                ],
            ),
            "AFTER",
        ),
        (
            Document(
                "style.sam",
                "utf-8",
                styles=object(),
                blocks=[Paragraph(runs=[TextRun("TEXT")], style_name="x"), _paragraph("AFTER")],
            ),
            "AFTER",
        ),
        (
            Document(
                "alignment.sam",
                "utf-8",
                blocks=[Paragraph(runs=[TextRun("TEXT")], alignment=[]), _paragraph("AFTER")],
            ),
            "AFTER",
        ),
    ],
)
def test_hostile_manual_style_fields_do_not_abort_following_text(
    document: Document, expected: str, tmp_path: Path
) -> None:
    assert expected in _poppler_text(pdf.render(document), tmp_path)


def test_rtl_newlines_tabs_and_leading_spaces_are_not_replacement_characters() -> None:
    flowable = BidiTextFlowable(
        "  FIRST السلام\nSECOND שלום\tNEXT",
        font_name="AmiProSans",
        font_size=11,
        leading=14,
        text_color=pdf.colors.black,
    )
    flowable.wrap(300, 700)

    assert "�" not in flowable.getPlainText()
    assert "\n" in flowable.getPlainText()
    assert flowable._lines[0].startswith("  ")
    assert any("SECOND" in line and "NEXT" in line for line in flowable._lines)


def test_bidi_flowable_chooses_base_direction_per_explicit_line() -> None:
    flowable = BidiTextFlowable(
        "English first\nمرحبا بالعالم 123 ABC",
        font_name="AmiProSans",
        font_size=11,
        leading=14,
        text_color=pdf.colors.black,
    )
    flowable.wrap(300, 700)

    assert base_direction(flowable._lines[0]) == "LTR"
    assert base_direction(flowable._lines[1]) == "RTL"


def test_non_bmp_controls_and_missing_glyphs_are_visible_replacements(tmp_path: Path) -> None:
    input_text = "before \U0001F600 \ud800 \ufdd0 \u2067RTL\u2069 after"
    payload = pdf.render(_document(_paragraph(input_text)))
    extracted = _poppler_text(payload, tmp_path)

    assert "before" in extracted and "after" in extracted
    assert "�" in extracted
    assert "\U0001F600" not in extracted
    assert "\ud800" not in extracted


def test_unicode_budget_bounds_paragraph_tokens_combining_and_controls() -> None:
    budget = PdfTextBudget()
    text = budget.prepare(
        "A" * 9_000
        + " e"
        + "\u0301" * 100
        + " "
        + "\u202b" * 4_100
        + "END",
        unit_boundary=True,
    )

    assert "overlong token omitted" in text
    assert text.count("\u0301") == 64
    assert text.count("�") >= 1
    assert len(text) <= 65_536


def test_maximum_unbroken_rtl_token_wraps_within_a_small_work_budget() -> None:
    from amipro_sam.pdf_unicode import _wrap_bidi_text, bidiShapedText

    ensure_pdf_fonts()
    token = "س" * 1_024
    _shaped, full_width = bidiShapedText(
        token,
        "RTL",
        fontName="AmiProSans",
        fontSize=1.0,
        shaping=True,
    )
    started = time.monotonic()
    lines = _wrap_bidi_text(token, "AmiProSans", 1.0, full_width - 0.001)
    elapsed = time.monotonic() - started

    assert "".join(lines) == token
    assert len(lines) == 2
    assert elapsed < 1.0


def test_shared_pdf_text_budget_covers_embedded_object_captions() -> None:
    budget = PdfTextBudget(remaining=1)
    drawing = SdwDrawing(
        "synthetic",
        0,
        1,
        alt_text="A" * 256,
        reason="R" * 256,
    )
    story = pdf._primary_story(
        Document("x.sam", "utf-8", blocks=[drawing]),
        text_budget=budget,
    )

    assert budget.remaining == 0
    assert len(story) == 1
    assert "PDF text omitted" in story[0].getPlainText()


def test_encoded_pdf_output_backstop_is_controlled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf, "_MAX_PDF_OUTPUT_BYTES", 100)
    with pytest.raises(pdf.RenderError, match="64 MiB output limit"):
        pdf.render(_document(_paragraph("bounded")))


def test_pdf_page_backstop_is_controlled(monkeypatch: pytest.MonkeyPatch) -> None:
    from amipro_sam.model import PageBreak

    monkeypatch.setattr(pdf, "_MAX_PDF_PAGES", 1)
    document = Document(
        "pages.sam",
        "utf-8",
        blocks=[_paragraph("one"), PageBreak(), _paragraph("two")],
    )
    with pytest.raises(pdf.RenderError, match="1-page limit"):
        pdf.render(document)


def test_giant_manual_font_size_is_clamped_without_overflow() -> None:
    document = Document(
        "font.sam",
        "utf-8",
        styles={
            "huge": StyleDefinition(
                name="huge",
                character=CharacterStyle(font_size_pt=10**10_000),
            )
        },
        blocks=[Paragraph(runs=[TextRun("TEXT")], style_name="huge")],
    )
    flowable = pdf._primary_story(document)[0]

    assert isinstance(flowable, ReportLabParagraph)
    assert flowable.style.fontSize == 11.0


def test_alternating_font_fallback_spans_have_a_visible_hard_limit(
    tmp_path: Path,
) -> None:
    text = ("A 漢 " * 3_000) + "AFTER"
    payload = pdf.render(_document(_paragraph(text)))
    extracted = _poppler_text(payload, tmp_path)

    assert "font fallback spans omitted at safe PDF limit" in " ".join(
        extracted.split()
    )
    assert "AFTER" not in extracted
    assert len(payload) < 5_000_000


def test_cjk_subset_boundary_and_fresh_process_determinism(tmp_path: Path) -> None:
    characters = "".join(chr(0x4E00 + index) for index in range(300))
    first = pdf.render(_document(_paragraph(characters)))
    second = pdf.render(_document(_paragraph(characters)))
    assert first == second
    assert len(re.findall(rb"/ToUnicode\s+\d+\s+0\s+R", first)) >= 2

    script = tmp_path / "determinism.py"
    output = tmp_path / "fresh.pdf"
    script.write_text(
        "from pathlib import Path\n"
        "from amipro_sam.model import Document, Paragraph, TextRun\n"
        "from amipro_sam.renderers import pdf\n"
        f"text={characters!r}\n"
        f"Path({str(output)!r}).write_bytes(pdf.render("
        "Document('x.sam','utf-8',blocks=[Paragraph(runs=[TextRun(text)])])))\n",
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, str(script)],
        check=True,
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                item
                for item in ("src", os.environ.get("PYTHONPATH", ""))
                if item
            ),
            "PYTHONHASHSEED": "123",
        },
        capture_output=True,
    )
    assert first == output.read_bytes()


def test_rtl_style_falls_back_to_coverage_complete_face() -> None:
    document = Document(
        "rtl.sam",
        "utf-8",
        styles={
            "italic": StyleDefinition(
                name="italic", character=CharacterStyle(italic=True)
            )
        },
        blocks=[
            Paragraph(
                runs=[TextRun("السلام عليكم 123 ABC")],
                style_name="italic",
            )
        ],
    )
    flowable = pdf._primary_story(document)[0]
    assert isinstance(flowable, BidiTextFlowable)
    assert flowable.font_name == "AmiProSans"


def test_pdf_has_no_host_font_path_or_source_family_path() -> None:
    hostile = CharacterStyle(font_family="../../fonts/secret.ttf")
    payload = pdf.render(_document(_paragraph("safe text", hostile)))
    assert b"/usr/share/fonts" not in payload
    assert b"secret.ttf" not in payload
    assert b"AmiProPreservationSans" in payload


def test_pypdf_ltr_extraction_when_available() -> None:
    pypdf = pytest.importorskip("pypdf")
    text = "Café Αθήνα Москва 漢字"
    reader = pypdf.PdfReader(BytesIO(pdf.render(_document(_paragraph(text)))))
    assert text in "\n".join(page.extract_text() or "" for page in reader.pages)


def test_reportlab_paragraph_remains_used_for_ltr_text() -> None:
    flowable = pdf._primary_story(_document(_paragraph("plain Latin")))[0]
    assert isinstance(flowable, ReportLabParagraph)
