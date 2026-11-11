"""Character escapes decode in the document code page (SAM-ESCAPE-002).

All documents here are invented. ``synthetic-cp1250-escapes.sam`` is
``synthetic-basic.sam`` with its ``[charset]`` description changed to
``ANSI (Windows, IBM CP 1250)``, its body replaced by the Czech phrase
"Žluťoučký kůň" written only with ``</x>``/``<\\x>`` escapes, and its empty
``[Embedded]`` directory dropped.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from amipro_sam import cli
from amipro_sam.errors import PreservationLossError
from amipro_sam.model import Frame, Paragraph, UnsupportedObject
from amipro_sam.parser import parse_bytes, parse_file
from amipro_sam.renderers import RENDERERS

FIXTURES = Path(__file__).parent / "fixtures"
CZECH = "Žluťoučký kůň"
CZECH_ESCAPED = "</N>lu</]>ou<\\h>k<\\}> k<\\y><\\r>"


def _sam(body: str, *, codepage: int = 1250, extra: str = "") -> bytes:
    source = (
        "[ver]\n\t4\n[sty]\n\t\n"
        f"[charset]\n\t82\n\tANSI (Windows, IBM CP {codepage})\n"
        + extra
        + "[edoc]\n"
        + body
        + "\n>\n"
    )
    return source.replace("\n", "\r\n").encode("ascii")


def _style(name: str) -> str:
    return f"""[tag]
\t{name}
\t2
\t[fnt]
\t\tTimes New Roman<\\m>
\t\t240
\t\t0
\t\t49152
\t[algn]
\t\t1
\t\t1
\t\t0
\t\t288
\t\t288
\t[spc]
\t\t1
\t\t273
\t\t1
\t\t0
\t\t0
\t\t1
\t\t100
\tBody Text
\t0
\t0
"""


def _texts(document) -> list[str]:
    return [block.text for block in document.blocks if isinstance(block, Paragraph)]


def _escape_codes(document) -> list[str]:
    return [item.code for item in document.diagnostics if item.code.startswith("escape-")]


def test_cp1250_fixture_decodes_czech() -> None:
    document = parse_file(FIXTURES / "synthetic-cp1250-escapes.sam")

    assert document.encoding == "cp1250"
    assert _texts(document) == [CZECH]
    assert _escape_codes(document) == []


def test_escape_decoding_adds_no_preservation_loss() -> None:
    document = parse_bytes(_sam(CZECH_ESCAPED), strict=True)

    assert _texts(document) == [CZECH]
    assert not any(item.code.startswith("escape-") for item in document.preservation_losses)


def test_cp1252_c1_range_escapes() -> None:
    document = parse_bytes(_sam("</S>x</T></R>", codepage=1252))

    assert _texts(document) == ["“x”'"]


def test_escaped_style_name_resolves() -> None:
    document = parse_bytes(_sam("@N<\\a>zev@text", extra=_style("N<\\a>zev")))

    assert "Název" in document.styles
    assert document.styles["Název"].character.font_family == "Times New Romaní"
    paragraph = next(block for block in document.blocks if isinstance(block, Paragraph))
    assert paragraph.style_name == "Název"
    assert paragraph.text == "text"
    assert not any(item.code == "undefined-style" for item in document.diagnostics)


def test_desc_and_frame_names_decoded() -> None:
    frame = """[frm]
\t0
\t524288
\t0
\t0
\t1440
\t1440
\t[frmname]
\t\tR<\\a>m
\t[txt]
r<\\h>
>
"""
    desc = "[desc]\n\tP<\\x><\\m>klad\n"
    document = parse_bytes(_sam("before<:A0>after", extra=desc + frame))

    assert document.metadata["desc"] == "Příklad"
    typed = next(block for block in document.blocks if isinstance(block, Frame))
    assert typed.name == "Rám"


def test_encoding_override_governs_escapes(tmp_path: Path, capsys) -> None:
    data = _sam("<\\x>", codepage=1252)

    assert _texts(parse_bytes(data)) == ["ø"]
    assert _texts(parse_bytes(data, encoding="cp1250")) == ["ř"]

    source = tmp_path / "override.sam"
    source.write_bytes(data)
    assert cli.main(["convert", str(source), "--encoding", "cp1250", "--format", "text"]) == 0
    assert "ř" in capsys.readouterr().out


@pytest.mark.parametrize("encoding", ["utf-8", "cp932", "gbk"])
def test_multibyte_and_utf_fallback_diagnosed(encoding: str) -> None:
    document = parse_bytes(_sam("<\\x> <\\x>"), encoding=encoding)

    assert _texts(document) == ["ø ø"]
    assert _escape_codes(document) == ["escape-codepage-fallback"]


def test_undefined_byte_preserved_and_diagnosed() -> None:
    document = parse_bytes(_sam("a</P>b"))

    assert _texts(document) == ["a\udc90b"]
    losses = [item for item in document.preservation_losses if item.code.startswith("escape-")]
    assert [item.code for item in losses] == ["escape-byte-undefined"]
    assert "0x90" in losses[0].message
    with pytest.raises(PreservationLossError) as raised:
        parse_bytes(_sam("a</P>b"), strict=True)
    assert any(item.code == "escape-byte-undefined" for item in raised.value.losses)
    assert "�" in RENDERERS["html"](document).decode("utf-8")


def test_diagnostics_follow_tokenizer_context() -> None:
    shielded = parse_bytes(_sam("x<\n</P>y"), strict=True)
    assert _texts(shielded) == ["x</P>y"]
    assert _escape_codes(shielded) == []

    split = parse_bytes(_sam("x</\nP>y"))
    assert _texts(split) == ["x\udc90y"]
    assert _escape_codes(split) == ["escape-byte-undefined"]
    with pytest.raises(PreservationLossError):
        parse_bytes(_sam("x</\nP>y"), strict=True)

    raw_header = parse_bytes(_sam("text", extra="[lang]\n\t</P>\n"))
    assert _escape_codes(raw_header) == []


def test_non_ascii_operand_is_not_an_escape() -> None:
    data = "[ver]\n\t4\n[sty]\n\t\n[edoc]\nx<\\š>y\n>\n".replace("\n", "\r\n")
    document = parse_bytes(data.encode("cp1250"))

    assert _escape_codes(document) == []
    assert any(isinstance(block, UnsupportedObject) for block in document.blocks) or any(
        "Unsupported inline command" in text for text in _texts(document)
    )


def test_literal_unescape_is_single_pass() -> None:
    document = parse_bytes(_sam("@A<<\\a>@text", extra=_style("A<<\\a>")))

    assert "A<\\a>" in document.styles
    assert _escape_codes(document) == []


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext is not installed")
def test_pdf_contains_decoded_text(tmp_path: Path) -> None:
    document = parse_file(FIXTURES / "synthetic-cp1250-escapes.sam")
    pdf = tmp_path / "out.pdf"
    pdf.write_bytes(RENDERERS["pdf"](document))
    text = subprocess.run(
        ["pdftotext", str(pdf), "-"], check=True, capture_output=True, text=True
    ).stdout

    assert CZECH in text
