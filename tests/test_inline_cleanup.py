from __future__ import annotations

import pytest

from amipro_sam.errors import PreservationLossError
from amipro_sam.model import Paragraph
from amipro_sam.parser import parse_bytes


def _sam(body: bytes) -> bytes:
    return (
        b"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\n"
        + body
        + b"\r\n>\r\n"
    )


def _paragraph(source: bytes, *, strict: bool = False) -> tuple[object, Paragraph]:
    document = parse_bytes(_sam(source), strict=strict)
    paragraph = next(
        block for block in document.blocks if isinstance(block, Paragraph)
    )
    return document, paragraph


def test_paragraph_region_keeps_x_and_width_distinct_from_indents() -> None:
    document, paragraph = _paragraph(b"<:#426,9025>BODY")

    assert paragraph.text == "BODY"
    assert paragraph.region_x_twips == 426
    assert paragraph.region_width_twips == 9025
    assert paragraph.left_indent_in is None
    assert paragraph.first_line_indent_in is None
    assert not any(
        item.code == "unsupported-inline-tags" for item in document.diagnostics
    )
    assert any(
        item.code == "paragraph-region-reflowed" for item in document.diagnostics
    )
    with pytest.raises(PreservationLossError):
        _paragraph(b"<:#426,9025>BODY", strict=True)


@pytest.mark.parametrize("tag", [b":#426", b":#-1,9025", b":#426,0"])
def test_invalid_paragraph_regions_are_atomic_and_visible(tag: bytes) -> None:
    document, paragraph = _paragraph(b"BEFORE<" + tag + b">AFTER")

    assert "Unsupported inline command" in paragraph.text
    assert paragraph.region_x_twips is None
    assert paragraph.region_width_twips is None
    assert any(item.code == "unsupported-inline-tags" for item in document.diagnostics)


def test_four_field_indent_is_typed_but_not_guessed() -> None:
    document, paragraph = _paragraph(b"BEFORE<:I504,0,0,0>AFTER")

    assert paragraph.text == "BEFOREAFTER"
    assert paragraph.inline_indent_twips == (504, 0, 0, 0)
    assert paragraph.left_indent_in is None
    assert paragraph.first_line_indent_in is None
    assert "Unsupported inline command" not in document.text
    assert any(
        item.code == "inline-command-semantics-unapplied"
        for item in document.diagnostics
    )
    with pytest.raises(PreservationLossError):
        _paragraph(b"BEFORE<:I504,0,0,0>AFTER", strict=True)


def test_invalid_four_field_indent_cannot_partially_mutate_state() -> None:
    document, paragraph = _paragraph(b"BEFORE<:I1440,OPAQUE,720,0>AFTER")

    assert "Unsupported inline command" in paragraph.text
    assert paragraph.inline_indent_twips is None
    assert paragraph.left_indent_in is None
    assert paragraph.first_line_indent_in is None
    assert any(item.code == "unsupported-inline-tags" for item in document.diagnostics)


@pytest.mark.parametrize("tag", [b":f240,Wingdings,", b":f,,"])
def test_compact_font_command_has_an_empty_optional_color_tail(tag: bytes) -> None:
    document, paragraph = _paragraph(b"BEFORE<" + tag + b">AFTER", strict=True)

    assert paragraph.text == "BEFOREAFTER"
    assert "Unsupported inline command" not in document.text
    if tag.startswith(b":f240"):
        after = next(run for run in paragraph.runs if run.text == "AFTER")
        assert after.style.font_family == "Wingdings"
        assert after.style.font_size_pt == 12.0


def test_compact_font_omissions_restore_paragraph_font_defaults() -> None:
    document, paragraph = _paragraph(
        b"<:f300,Arial,255,0,0>OVERRIDE<:f240,Wingdings,>FAMILY<:f,,>DEFAULT"
    )

    family = next(run for run in paragraph.runs if run.text == "FAMILY")
    restored = next(run for run in paragraph.runs if run.text == "DEFAULT")
    assert family.style.font_family == "Wingdings"
    assert family.style.font_size_pt == 12.0
    assert family.style.color is None
    assert restored.style.font_family is None
    assert restored.style.font_size_pt is None
    assert restored.style.color is None
    assert "Unsupported inline command" not in document.text


def test_spell_state_and_matched_dynamic_field_terminator_are_not_body_text() -> None:
    spell_document, spell = _paragraph(b"BEFORE<:s>AFTER", strict=True)
    assert spell.text == "BEFOREAFTER"
    assert "Unsupported inline command" not in spell_document.text

    field_document, field = _paragraph(
        b'BEFORE<:X3,-32768;if Defined x x else "Recipient" endif>'
        b'<:X~3,-32768;if Defined x x else "Recipient" endif>AFTER'
    )
    assert field.text == "BEFORERecipientAFTER"
    assert "Unsupported inline command: <:X~3," not in field_document.text


def test_unmatched_dynamic_field_terminator_remains_visible() -> None:
    document, paragraph = _paragraph(b"BEFORE<:X~3>AFTER")

    assert "Unsupported inline command: <:X~3>" in paragraph.text
    assert any(item.code == "unsupported-inline-tags" for item in document.diagnostics)
