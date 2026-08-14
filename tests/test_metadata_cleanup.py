from __future__ import annotations

import pytest

from amipro_sam.errors import PreservationLossError
from amipro_sam.model import Frame, UnsupportedObject
from amipro_sam.parser import parse_bytes


def sam(body: str = "BODY", *, extra: str = "") -> bytes:
    source = "[ver]\n\t4\n[sty]\n\t\n" + extra + "[edoc]\n" + body + "\n>\n"
    return source.replace("\n", "\r\n").encode("cp1252")


def style_record(
    *,
    envelope: tuple[str, ...] = ("2", "Body Text", "0", "0"),
    font_flags: int = 0xC000,
    spacing_flags: int = 1,
    tightness: int = 100,
    all_indent: int = 0,
) -> str:
    shortcut = envelope[0]
    top = "\n".join(f"\t{value}" for value in envelope[1:])
    return f"""[tag]
\tBody Text
\t{shortcut}
\t[fnt]
\t\tTimes New Roman
\t\t240
\t\t0
\t\t{font_flags}
\t[algn]
\t\t1
\t\t1
\t\t{all_indent}
\t\t288
\t\t288
\t[spc]
\t\t{spacing_flags}
\t\t273
\t\t1
\t\t0
\t\t0
\t\t1
\t\t{tightness}
{top}
"""


def test_zero_revision_state_is_typed_but_noncanonical_states_stay_visible() -> None:
    clean = parse_bytes(sam(extra="[revisions]\n\t0\n"), strict=True)

    assert clean.metadata["revisions"] == "0"
    assert not any(item.code == "revisions-opaque" for item in clean.diagnostics)
    assert not any(record.record_type == "revision-state" for record in clean.unknown_records)

    for extra in (
        "[revisions]\n\t1\n",
        "[revisions]\n",
        "[revisions]\n\t0\n\textra\n",
        "[revisions]\n\t0\n[revisions]\n\t0\n",
    ):
        document = parse_bytes(sam(extra=extra))
        assert any(item.code == "revisions-opaque" for item in document.diagnostics)
        assert any(
            isinstance(block, UnsupportedObject) and block.kind == "revision state"
            for block in document.blocks
        )
        with pytest.raises(PreservationLossError):
            parse_bytes(sam(extra=extra), strict=True)


def test_canonical_style_envelope_and_documented_alignment_are_typed() -> None:
    document = parse_bytes(
        sam("@Body Text@BODY", extra=style_record()), strict=True
    )
    style = document.styles["Body Text"]

    assert style.shortcut_key == 2
    assert style.following_style == "Body Text"
    assert style.parent is None
    assert style.left_indent_in == pytest.approx(0.2)
    assert style.first_line_indent_in == pytest.approx(0.0)
    assert not any(
        item.code
        in {
            "style-top-level-fields-opaque",
            "style-subrecord-unknown-flags",
            "style-subrecord-fields-opaque",
        }
        for item in document.diagnostics
    )


def test_opaque_style_metadata_stays_lossy_but_out_of_body_flow() -> None:
    document = parse_bytes(
        sam(
            "@Body Text@BODY",
            extra=style_record(envelope=("2", "Body Text", "0", "7")),
        )
    )
    style = document.styles["Body Text"]
    record = next(
        item
        for item in document.unknown_records
        if item.record_type == "style-top-level-fields"
    )

    assert style.shortcut_key is None
    assert style.following_style is None
    assert style.parent is None
    assert "Body Text" in record.raw and "7" in record.raw
    assert any(
        item.code == "style-top-level-fields-opaque"
        for item in document.diagnostics
    )
    assert not any(
        isinstance(block, UnsupportedObject) and block.kind == "style fields"
        for block in document.blocks
    )
    with pytest.raises(PreservationLossError):
        parse_bytes(
            sam(
                "@Body Text@BODY",
                extra=style_record(envelope=("2", "Body Text", "0", "7")),
            ),
            strict=True,
        )


def test_spacing_behavior_and_nondefault_tightness_remain_semantic_losses() -> None:
    flags = parse_bytes(
        sam("@Body Text@BODY", extra=style_record(spacing_flags=0x21))
    )
    flag_record = next(
        item
        for item in flags.unknown_records
        if item.record_type == "style-subrecord-unknown-flags"
    )

    assert "[spc]" in flag_record.raw
    assert "[fnt]" not in flag_record.raw
    assert any(
        item.code == "style-subrecord-unknown-flags" for item in flags.diagnostics
    )
    assert not any(
        isinstance(block, UnsupportedObject) and block.kind == "style flag bits"
        for block in flags.blocks
    )

    tightness = parse_bytes(
        sam("@Body Text@BODY", extra=style_record(tightness=99))
    )
    assert any(
        item.code == "style-subrecord-fields-opaque"
        for item in tightness.diagnostics
    )

    both_sides = parse_bytes(
        sam("@Body Text@BODY", extra=style_record(all_indent=144))
    )
    assert any(
        item.code == "style-alignment-all-indent-unapplied"
        for item in both_sides.diagnostics
    )
    assert any(
        record.record_type == "style-alignment-all-indent"
        for record in both_sides.unknown_records
    )


def test_style_small_caps_stays_unapplied_while_strike_is_bit_0x80() -> None:
    small_caps = parse_bytes(
        sam("@Body Text@BODY", extra=style_record(font_flags=0xC020))
    )

    assert small_caps.styles["Body Text"].character.strike is False
    assert any(
        item.code == "style-subrecord-unknown-flags"
        for item in small_caps.diagnostics
    )
    with pytest.raises(PreservationLossError):
        parse_bytes(
            sam("@Body Text@BODY", extra=style_record(font_flags=0xC020)),
            strict=True,
        )

    strike = parse_bytes(
        sam("@Body Text@BODY", extra=style_record(font_flags=0xC080)),
        strict=True,
    )
    assert strike.styles["Body Text"].character.strike is True


def test_exact_empty_elay_and_single_l1_are_typed_without_selecting_layout() -> None:
    document = parse_bytes(
        sam(extra="[elay]\n[elay]\n[l1]\n\t1\n"), strict=True
    )

    assert document.l1_value == 1
    assert not any(item.code == "unknown-section" for item in document.diagnostics)
    assert document.to_dict()["l1_value"] == 1


@pytest.mark.parametrize(
    "extra",
    (
        "[elay]\n\topaque\n",
        "[l1]\n\t-1\n",
        "[l1]\n\t0\n\textra\n",
        "[l1]\n\t0\n[l1]\n\t1\n",
    ),
)
def test_malformed_structural_markers_remain_visible_losses(extra: str) -> None:
    document = parse_bytes(sam(extra=extra))

    assert any(item.code == "unknown-section" for item in document.diagnostics)
    assert any(
        isinstance(block, UnsupportedObject) and block.kind == "unknown section"
        for block in document.blocks
    )
    with pytest.raises(PreservationLossError):
        parse_bytes(sam(extra=extra), strict=True)


def test_exact_frame_name_is_typed_and_hostile_variants_remain_opaque() -> None:
    frame = """[frm]
\t0
\t524288
\t0
\t0
\t1440
\t1440
\t[frmname]
\t\tInvented Frame
\t[txt]
invented frame text
>
"""
    document = parse_bytes(sam("before<:A0>after", extra=frame), strict=True)
    typed = next(block for block in document.blocks if isinstance(block, Frame))

    assert typed.name == "Invented Frame"
    assert not any(item.code == "frame-subrecord-opaque" for item in document.diagnostics)

    hostile_variants = (
        frame.replace(
            "\t\tInvented Frame\n\t[txt]",
            "\t\tInvented Frame\n\t\textra field\n\t[txt]",
        ),
        frame.replace("Invented Frame", "F" * 257),
        frame.replace(
            "\t[txt]",
            "\t[frmname]\n\t\tSecond Frame\n\t[txt]",
        ),
    )
    for hostile in hostile_variants:
        preserved = parse_bytes(sam("before<:A0>after", extra=hostile))
        hostile_frame = next(
            block for block in preserved.blocks if isinstance(block, Frame)
        )

        assert hostile_frame.name is None
        assert any(
            item.code == "frame-subrecord-opaque" for item in preserved.diagnostics
        )
        assert any(
            record.section == "frame/frmname"
            for record in preserved.unknown_records
        )
        with pytest.raises(PreservationLossError):
            parse_bytes(sam("before<:A0>after", extra=hostile), strict=True)
