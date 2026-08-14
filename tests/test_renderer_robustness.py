from __future__ import annotations

import importlib.util
import json as stdlib_json
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from amipro_sam.model import (
    CharacterStyle,
    Diagnostic,
    Document,
    Footnote,
    Frame,
    Image,
    Lossiness,
    Paragraph,
    Severity,
    TextRun,
    UnsupportedObject,
)
from amipro_sam.parser import parse_bytes
from amipro_sam.renderers import docx, html, json, markdown, odt, pdf, text


@dataclass
class _InventedBlock:
    payload: str = "INVENTED_PAYLOAD"


def _paragraph(value: str) -> Paragraph:
    return Paragraph(runs=[TextRun(value)])


def _pdf_story_text(document: Document) -> str:
    story = pdf._primary_story(document)  # type: ignore[attr-defined]
    return "\n".join(
        item.getPlainText()
        for item in story
        if callable(getattr(item, "getPlainText", None))
    )


def _odf_text(payload: bytes) -> str:
    with ZipFile(BytesIO(payload)) as archive:
        root = ET.fromstring(archive.read("content.xml"))

    def visit(node: ET.Element):
        if node.text:
            yield node.text
        for child in node:
            local_name = child.tag.rsplit("}", 1)[-1]
            if local_name == "s":
                yield " "
            elif local_name == "tab":
                yield "\t"
            elif local_name == "line-break":
                yield "\n"
            else:
                yield from visit(child)
            if child.tail:
                yield child.tail

    return "".join(visit(root))


def _docx_text(payload: bytes) -> str:
    with ZipFile(BytesIO(payload)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    return "".join(root.itertext())


def _parsed_unsupported_document() -> Document:
    prefix = (
        b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\n"
        b"BEFORE_INLINE<:mystery>AFTER_INLINE\r\n>\r\n"
    )
    return parse_bytes(
        prefix
        + b"[Embedded]\r\nBOGUS DIRECTORY ROW\r\n"
        + f"{len(prefix):08d}\r\n".encode("ascii")
    )


def _visible_output(renderer: str, document: Document) -> str:
    if renderer == "html":
        return html.render(document).decode("utf-8")
    if renderer == "markdown":
        return markdown.render(document).decode("utf-8")
    if renderer == "text":
        return text.render(document).decode("utf-8")
    if renderer == "json":
        return stdlib_json.dumps(
            stdlib_json.loads(json.render(document)), sort_keys=True
        )
    if renderer == "pdf":
        return _pdf_story_text(document)
    if renderer == "odt":
        return _odf_text(odt.render(document))
    if renderer == "docx":
        return _docx_text(docx.render(document))
    raise AssertionError(renderer)


_DOCX_MARK = pytest.mark.skipif(
    importlib.util.find_spec("docx") is None,
    reason="python-docx extra not installed",
)


@pytest.mark.parametrize(
    "renderer",
    [
        "html",
        "markdown",
        "text",
        "json",
        "pdf",
        "odt",
        pytest.param("docx", marks=_DOCX_MARK),
    ],
)
def test_invalid_root_block_container_is_visible(renderer: str) -> None:
    document = Document("manual.sam", "cp1252")
    document.blocks = object()  # type: ignore[assignment]

    rendered = _visible_output(renderer, document).casefold()

    assert "invalid" in rendered
    assert "block" in rendered or "nested content" in rendered
    assert "omitted" in rendered


@pytest.mark.parametrize(
    "renderer",
    [
        "html",
        "markdown",
        "text",
        "json",
        "pdf",
        "odt",
        pytest.param("docx", marks=_DOCX_MARK),
    ],
)
def test_unrecognized_block_is_marked_and_following_content_survives(
    renderer: str,
) -> None:
    document = Document(
        "manual.sam",
        "cp1252",
        blocks=[
            _paragraph("BEFORE_UNKNOWN"),
            _InventedBlock(),  # type: ignore[list-item]
            _paragraph("AFTER_UNKNOWN"),
        ],
    )

    rendered = _visible_output(renderer, document).replace("\\_", "_")

    assert "Unrecognized block" in rendered or "unrecognized-block" in rendered
    assert "omitted" in rendered
    assert "BEFORE_UNKNOWN" in rendered
    assert "AFTER_UNKNOWN" in rendered


@pytest.mark.parametrize(
    "renderer",
    [
        "html",
        "markdown",
        "text",
        "json",
        "pdf",
        "odt",
        pytest.param("docx", marks=_DOCX_MARK),
    ],
)
def test_parsed_inline_and_malformed_directory_losses_are_visible(
    renderer: str,
) -> None:
    rendered = _visible_output(renderer, _parsed_unsupported_document()).replace(
        "\\_", "_"
    )

    assert "BEFORE_INLINE" in rendered
    assert "Unsupported inline command" in rendered
    assert "AFTER_INLINE" in rendered
    assert "malformed embedded directory" in rendered.casefold()


@pytest.mark.parametrize(
    ("renderer", "module", "limit_name"),
    [
        ("html", html, "_MAX_RENDER_BLOCKS"),
        ("markdown", markdown, "_MAX_RENDER_BLOCKS"),
        ("text", text, "_MAX_RENDER_BLOCKS"),
        ("json", json, "_MAX_JSON_ITEMS"),
        ("pdf", pdf, "_MAX_RENDER_BLOCKS"),
        ("odt", odt, "_MAX_BLOCKS_PER_LIST"),
        pytest.param(
            "docx", docx, "_MAX_BLOCKS_PER_LIST", marks=_DOCX_MARK
        ),
    ],
)
def test_block_limit_has_a_visible_omission_marker(
    monkeypatch: pytest.MonkeyPatch,
    renderer: str,
    module: object,
    limit_name: str,
) -> None:
    monkeypatch.setattr(module, limit_name, 2)
    document = Document(
        "manual.sam",
        "cp1252",
        blocks=[_paragraph("FIRST"), _paragraph("SECOND"), _paragraph("THIRD")],
    )

    rendered = _visible_output(renderer, document).casefold()

    assert "omitted" in rendered
    assert "limit" in rendered


def test_html_diagnostics_are_typed_bounded_and_show_loss_category() -> None:
    diagnostic = Diagnostic(
        Severity.WARNING,
        "invented-loss",
        "M" * 100_000,
        lossiness=Lossiness.SEMANTIC,
    )
    document = Document(
        "manual.sam",
        "cp1252",
        blocks=[_paragraph("AFTER_DIAGNOSTICS")],
        diagnostics=[object(), *([diagnostic] * 10_000)],  # type: ignore[list-item]
    )

    rendered = html.render(document).decode("utf-8")

    assert "AFTER_DIAGNOSTICS" in rendered
    assert "Invalid diagnostic item omitted" in rendered
    assert "loss=<code>semantic</code>" in rendered
    assert "Diagnostic content omitted at safe rendering limit" in rendered
    assert len(rendered) < 200_000

    document.diagnostics = object()  # type: ignore[assignment]
    invalid = html.render(document).decode("utf-8")
    assert "Invalid diagnostics container omitted" in invalid
    assert "AFTER_DIAGNOSTICS" in invalid


@pytest.mark.parametrize(
    "renderer",
    [
        "html",
        "markdown",
        "text",
        "json",
        "pdf",
        "odt",
        pytest.param("docx", marks=_DOCX_MARK),
    ],
)
def test_hostile_style_fields_do_not_hide_following_content(renderer: str) -> None:
    hostile_style = CharacterStyle()
    hostile_style.color = object()  # type: ignore[assignment]
    hostile_style.font_family = object()  # type: ignore[assignment]
    hostile_style.font_size_pt = 10**10_000
    paragraph = Paragraph(runs=[TextRun("HOSTILE_STYLE", hostile_style)])
    paragraph.style_name = ["unhashable"]  # type: ignore[assignment]
    document = Document(
        "manual.sam",
        "cp1252",
        blocks=[paragraph, _paragraph("AFTER_STYLE")],
    )
    document.styles = object()  # type: ignore[assignment]

    rendered = _visible_output(renderer, document).replace("\\_", "_")

    assert "HOSTILE_STYLE" in rendered
    assert "AFTER_STYLE" in rendered


def test_json_bounds_huge_integers_and_preserves_hostile_mapping_entries() -> None:
    hostile_key = object()
    document = Document("manual.sam", "cp1252", original_size=10**10_000)
    document.metadata = {  # type: ignore[assignment]
        hostile_key: "OBJECT_VALUE",
        1: "INTEGER_VALUE",
        "1": "STRING_VALUE",
    }

    first = json.render(document)
    second = json.render(document)
    parsed = stdlib_json.loads(first)

    assert first == second
    assert len(first) < 100_000
    assert b"object at 0x" not in first
    assert parsed["original_size"] == {
        "encoding": "bounded-integer",
        "sign": "positive",
        "bits": (10**10_000).bit_length(),
    }
    assert parsed["metadata"]["encoding"] == "mapping-entries"
    assert {entry["value"] for entry in parsed["metadata"]["entries"]} == {
        "OBJECT_VALUE",
        "INTEGER_VALUE",
        "STRING_VALUE",
    }


def test_json_distinguishes_shared_source_spans_from_true_cycles() -> None:
    document = parse_bytes(
        b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\na<+!>b<-!>c\r\n>\r\n"
    )
    parsed = stdlib_json.loads(json.render(document))
    paragraph = parsed["blocks"][0]
    sources = [run["source"] for run in paragraph["runs"]] + [paragraph["source"]]

    assert all(source["byte_offset"] >= 0 for source in sources)
    assert all(source.get("encoding") != "recursive-reference" for source in sources)

    frame = Frame()
    frame.blocks = [frame]
    cyclic = stdlib_json.loads(
        json.render(Document("cycle.sam", "cp1252", blocks=[frame]))
    )
    recursive = cyclic["blocks"][0]["blocks"][0]
    assert recursive["encoding"] == "recursive-reference"


def test_json_bounds_repeated_large_block_aliases() -> None:
    content = "X" * 10_000
    paragraph = Paragraph(runs=[TextRun(content)])
    document = Document("aliases.sam", "cp1252", blocks=[paragraph] * 1_000)

    payload = json.render(document)
    parsed = stdlib_json.loads(payload)

    assert parsed["blocks"][0]["runs"][0]["text"] == content
    assert parsed["blocks"][1]["encoding"] == "repeated-reference"
    assert parsed["blocks"][-1]["encoding"] == "repeated-reference"
    assert payload.count(content.encode()) == 1
    assert len(payload) < 500_000


@pytest.mark.parametrize(
    "renderer",
    [
        "html",
        "markdown",
        "text",
        "json",
        "pdf",
        "odt",
        pytest.param("docx", marks=_DOCX_MARK),
    ],
)
def test_repeated_large_block_aliases_are_bounded_in_every_output(
    renderer: str,
) -> None:
    content = "SHAREDALIAS" + "X" * 10_000
    shared = Paragraph(runs=[TextRun(content)])
    document = Document(
        "aliases.sam",
        "cp1252",
        blocks=[*([shared] * 1_000), _paragraph("TAIL_AFTER_ALIASES")],
    )

    rendered = _visible_output(renderer, document).replace("\\_", "_")

    assert rendered.count("SHAREDALIAS") == 1
    assert "TAIL_AFTER_ALIASES" in rendered
    lowered = rendered.casefold()
    assert (
        "repeated block" in lowered
        or "repeated or cyclic block" in lowered
        or "repeated-reference" in lowered
    )
    assert len(rendered) < 500_000


@pytest.mark.parametrize(
    "renderer",
    [
        "html",
        "markdown",
        "text",
        "json",
        "odt",
        pytest.param("docx", marks=_DOCX_MARK),
    ],
)
def test_non_pdf_renderers_bound_shared_large_text_aliases_document_wide(
    renderer: str,
) -> None:
    shared_text = "SHARED_TEXT_ALIAS_" + "X" * 65_536
    document = Document(
        "invented-aliases.sam",
        "utf-8",
        blocks=[
            *(Paragraph(runs=[TextRun(shared_text)]) for _ in range(256)),
            _paragraph("TAIL_AFTER_SHARED_TEXT"),
        ],
    )

    rendered = _visible_output(renderer, document).replace("\\_", "_")

    assert rendered.count("SHARED_TEXT_ALIAS_") == 1
    assert "TAIL_AFTER_SHARED_TEXT" in rendered
    assert "repeated text" in rendered.casefold()
    assert "omitted" in rendered.casefold()
    assert len(rendered) < 500_000


def test_document_text_bounds_shared_large_text_aliases_and_keeps_tail() -> None:
    shared_text = "MODEL_SHARED_ALIAS_" + "Y" * 65_536
    document = Document(
        "invented-model-aliases.sam",
        "utf-8",
        blocks=[
            *(Paragraph(runs=[TextRun(shared_text)]) for _ in range(256)),
            _paragraph("MODEL_TAIL_AFTER_ALIASES"),
        ],
    )

    rendered = document.text

    assert rendered.count("MODEL_SHARED_ALIAS_") == 1
    assert "MODEL_TAIL_AFTER_ALIASES" in rendered
    assert "repeated text" in rendered.casefold()
    assert len(rendered) < 200_000


@pytest.mark.parametrize(
    "renderer",
    [
        "html",
        "markdown",
        "text",
        "json",
        "odt",
        pytest.param("docx", marks=_DOCX_MARK),
    ],
)
def test_non_pdf_text_budget_preserves_ordinary_repeated_text_exactly(
    renderer: str,
) -> None:
    ordinary_shared_text = "ORDINARY_SHARED_TEXT"
    document = Document(
        "invented-ordinary.sam",
        "utf-8",
        blocks=[
            Paragraph(runs=[TextRun(ordinary_shared_text)]),
            Paragraph(runs=[TextRun(ordinary_shared_text)]),
            _paragraph("ORDINARY_TAIL"),
        ],
    )

    rendered = _visible_output(renderer, document).replace("\\_", "_")

    assert rendered.count(ordinary_shared_text) == 2
    assert rendered.count("ORDINARY_TAIL") == 1
    assert "repeated text value omitted" not in rendered.casefold()


@pytest.mark.parametrize(
    "renderer",
    [
        "html",
        "markdown",
        "text",
        "json",
        "odt",
        pytest.param("docx", marks=_DOCX_MARK),
    ],
)
def test_non_pdf_renderers_enforce_a_document_wide_distinct_text_budget(
    renderer: str,
) -> None:
    document = Document(
        "invented-distinct-text.sam",
        "utf-8",
        blocks=[
            *(
                _paragraph(f"DISTINCT_TEXT_{index:03d}_" + "Z" * 65_536)
                for index in range(80)
            ),
            _paragraph("TAIL_AFTER_DISTINCT_TEXT_BUDGET"),
        ],
    )

    rendered = _visible_output(renderer, document).replace("\\_", "_")

    assert "DISTINCT_TEXT_000_" in rendered
    assert "TAIL_AFTER_DISTINCT_TEXT_BUDGET" in rendered
    assert "text content omitted" in rendered.casefold()
    assert len(rendered) < 4_500_000


@pytest.mark.parametrize("field", ["text", "style"])
def test_markdown_malformed_text_run_keeps_following_content(field: str) -> None:
    run = TextRun("RUN_CONTENT")
    setattr(run, field, object())
    document = Document(
        "manual.sam",
        "cp1252",
        blocks=[Paragraph(runs=[run]), _paragraph("TAIL_CONTENT")],
    )

    rendered = markdown.render(document).decode("utf-8").replace("\\_", "_")
    assert "Invalid text run" in rendered
    assert "TAIL_CONTENT" in rendered


def test_text_malformed_image_fields_keep_following_content() -> None:
    image = Image(reference=object(), alt_text=object())  # type: ignore[arg-type]
    document = Document(
        "manual.sam",
        "cp1252",
        blocks=[image, _paragraph("TAIL_CONTENT")],
    )

    rendered = text.render(document).decode("utf-8")
    assert "invalid external reference omitted" in rendered
    assert "TAIL_CONTENT" in rendered


def test_odt_malformed_alignment_keeps_following_content() -> None:
    paragraph = _paragraph("ALIGNED_CONTENT")
    paragraph.alignment = object()  # type: ignore[assignment]
    document = Document(
        "manual.sam",
        "cp1252",
        blocks=[paragraph, _paragraph("TAIL_CONTENT")],
    )

    rendered = _odf_text(odt.render(document))
    assert "ALIGNED_CONTENT" in rendered
    assert "TAIL_CONTENT" in rendered


def test_html_invalid_metadata_container_keeps_following_content() -> None:
    document = Document(
        "manual.sam", "cp1252", blocks=[_paragraph("TAIL_CONTENT")]
    )
    document.metadata = object()  # type: ignore[assignment]

    rendered = html.render(document).decode("utf-8")
    assert "TAIL_CONTENT" in rendered


@pytest.mark.parametrize(
    "renderer",
    [
        "html",
        "markdown",
        "text",
        "json",
        "pdf",
        "odt",
        pytest.param("docx", marks=_DOCX_MARK),
    ],
)
def test_oversized_scalar_labels_are_bounded_and_keep_following_content(
    renderer: str,
) -> None:
    huge_integer = 10**10_000
    document = Document(
        "manual.sam",
        "cp1252",
        blocks=[
            Footnote(number=huge_integer),
            _paragraph("TAIL_AFTER_FOOTNOTE"),
            Image(  # type: ignore[arg-type]
                alt_text=huge_integer,
                reference=huge_integer,
            ),
            _paragraph("TAIL_AFTER_IMAGE"),
            UnsupportedObject(  # type: ignore[arg-type]
                kind=huge_integer,
                description=huge_integer,
            ),
            _paragraph("TAIL_AFTER_UNSUPPORTED"),
        ],
    )

    rendered = _visible_output(renderer, document).replace("\\_", "_")
    repeated = _visible_output(renderer, document).replace("\\_", "_")

    assert rendered == repeated
    assert "integer" in rendered.casefold()
    assert "TAIL_AFTER_FOOTNOTE" in rendered
    assert "TAIL_AFTER_IMAGE" in rendered
    assert "TAIL_AFTER_UNSUPPORTED" in rendered
    assert len(rendered) < 50_000

    if renderer == "pdf":
        payload = pdf.render(document)
        assert payload.startswith(b"%PDF-")
        assert len(payload) < 1_000_000
