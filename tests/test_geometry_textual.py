from __future__ import annotations

import json

from amipro_sam.model import Document, Frame, Paragraph, Table, TextRun, TwipRect
from amipro_sam.renderers import json as json_renderer
from amipro_sam.renderers import markdown, text


def _paragraph(value: str) -> Paragraph:
    return Paragraph(runs=[TextRun(value)])


def test_frame_textual_reflow_keeps_marker_content_and_anchor_order() -> None:
    frame = Frame(
        blocks=[_paragraph("invented nested frame text")],
        content_kind="text",
        placement="anchored",
        region="body",
        bounds=TwipRect(120, 240, 1560, 1680, valid=True),
    )
    document = Document(
        "invented.sam",
        "cp1252",
        blocks=[_paragraph("before"), frame, _paragraph("after")],
    )

    rendered_markdown = markdown.render(
        document, show_structure_labels=True
    ).decode()
    rendered_text = text.render(document, show_structure_labels=True).decode()
    structured = json.loads(json_renderer.render(document))

    marker = "[Frame: anchored; body; text; geometry reflowed]"
    assert marker in rendered_markdown and marker in rendered_text
    assert rendered_markdown.index("before") < rendered_markdown.index(
        "invented nested frame text"
    ) < rendered_markdown.index("after")
    assert rendered_text.index("before") < rendered_text.index(
        "invented nested frame text"
    ) < rendered_text.index("after")
    assert document.text.index("before") < document.text.index(
        "invented nested frame text"
    ) < document.text.index("after")
    assert structured["blocks"][1]["type"] == "Frame"
    assert structured["blocks"][1]["bounds"]["type"] == "TwipRect"


def test_hostile_manual_frame_fields_have_bounded_visible_textual_fallback() -> None:
    frame = Frame()
    frame.blocks = object()  # type: ignore[assignment]
    frame.content_kind = []  # type: ignore[assignment]
    frame.placement = {}  # type: ignore[assignment]
    frame.region = object()  # type: ignore[assignment]
    document = Document("hostile.sam", "cp1252", blocks=[frame])

    assert "unknown placement" in markdown.render(document).decode()
    assert "unknown placement" in text.render(document).decode()
    assert "Invalid nested content omitted" in document.text


def test_recursive_frame_is_bounded_in_text_and_json() -> None:
    frame = Frame(content_kind="text", placement="anchored")
    frame.blocks = [frame]
    document = Document("recursive.sam", "cp1252", blocks=[frame])

    assert "Recursive content omitted" in document.text
    structured = json.loads(json_renderer.render(document))
    nested = structured["blocks"][0]["blocks"][0]
    assert nested["encoding"] == "recursive-reference"


def test_paragraph_with_hostile_manual_runs_has_a_visible_bounded_fallback() -> None:
    paragraph = Paragraph()
    paragraph.runs = object()  # type: ignore[assignment]
    frame = Frame(blocks=[paragraph], content_kind="text", placement="anchored")
    document = Document("hostile-runs.sam", "cp1252", blocks=[frame])

    assert "Invalid paragraph runs omitted" in document.text
    assert "Invalid paragraph runs omitted" in markdown.render(document).decode()
    assert "Invalid paragraph runs omitted" in text.render(document).decode()


def test_table_with_hostile_manual_rows_has_visible_textual_fallback() -> None:
    table = Table()
    table.rows = object()  # type: ignore[assignment]
    document = Document("hostile-table.sam", "cp1252", blocks=[table])

    assert document.text == "[Invalid table rows omitted]"
    assert "Invalid table rows omitted" in markdown.render(document).decode()
    assert "Invalid table rows omitted" in text.render(document).decode()
