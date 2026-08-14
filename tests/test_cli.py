from __future__ import annotations

import json
from pathlib import Path

import pytest

from amipro_sam import cli, model
from amipro_sam.cli import main
from amipro_sam.model import Document, Frame, PageBreak, Paragraph, TextRun
from amipro_sam.renderers import json as json_renderer


def _sam(text: str) -> bytes:
    source = f"[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[edoc]\r\n{text}\r\n>\r\n"
    return source.encode("cp1252")


def test_convert_refuses_to_replace_source_input(tmp_path: Path) -> None:
    source = tmp_path / "source.sam"
    original = _sam("sentinel")
    source.write_bytes(original)

    assert main(["convert", str(source), "--format", "text", "--output", str(source)]) == 1
    assert source.read_bytes() == original


def test_existing_output_requires_force(tmp_path: Path) -> None:
    source = tmp_path / "source.sam"
    output = tmp_path / "result.txt"
    source.write_bytes(_sam("converted"))
    output.write_text("keep me", encoding="utf-8")

    assert main(["convert", str(source), "-f", "txt", "-o", str(output)]) == 1
    assert output.read_text(encoding="utf-8") == "keep me"
    assert main(
        ["convert", str(source), "-f", "txt", "-o", str(output), "--force"]
    ) == 0
    assert output.read_text(encoding="utf-8").strip() == "converted"


def test_batch_collision_is_rejected_before_any_output(tmp_path: Path) -> None:
    first = tmp_path / "first" / "same.sam"
    second = tmp_path / "second" / "same.sam"
    destination = tmp_path / "out"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(_sam("first"))
    second.write_bytes(_sam("second"))

    assert main(
        ["convert", str(first), str(second), "-f", "txt", "-o", str(destination)]
    ) == 1
    assert not destination.exists()


def test_batch_continues_after_corrupt_input_and_supports_unicode_paths(
    tmp_path: Path,
) -> None:
    source_directory = tmp_path / "documents with spaces"
    destination = tmp_path / "résultats"
    source_directory.mkdir()
    (source_directory / "café.sam").write_bytes(_sam("recovered"))
    (source_directory / "broken.sam").write_bytes(b"")

    assert main(
        [
            "convert",
            str(source_directory),
            "--format",
            "text",
            "--output",
            str(destination),
        ]
    ) == 1
    assert (destination / "café.txt").read_text(encoding="utf-8").strip() == "recovered"
    assert not (destination / "broken.txt").exists()


def test_dump_refuses_source_and_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.sam"
    output = tmp_path / "dump.json"
    original = _sam("safe")
    source.write_bytes(original)
    output.write_text("keep", encoding="utf-8")

    assert main(["dump", str(source), "--output", str(source)]) == 1
    assert source.read_bytes() == original
    assert main(["dump", str(source), "--output", str(output)]) == 1
    assert output.read_text(encoding="utf-8") == "keep"


def test_dump_uses_bounded_json_renderer_with_visible_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.sam"
    output = tmp_path / "dump.json"
    source.write_bytes(_sam("safe"))
    document = Document(
        "manual.sam",
        "cp1252",
        blocks=[PageBreak(), PageBreak(), PageBreak()],
    )
    monkeypatch.setattr(cli, "parse_file", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(json_renderer, "_MAX_JSON_ITEMS", 2)

    assert main(["dump", str(source), "--output", str(output)]) == 0
    dumped = json.loads(output.read_text(encoding="utf-8"))
    assert dumped["blocks"][-1]["encoding"] == "block-limit"
    assert dumped["blocks"][-1]["omitted_count"] == 1


def test_document_text_marks_root_and_nested_block_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model, "_MAX_TEXT_BLOCKS", 2)
    blocks = [PageBreak(), PageBreak(), PageBreak()]

    root_text = Document("root.sam", "cp1252", blocks=blocks).text
    nested_text = Document(
        "nested.sam",
        "cp1252",
        blocks=[Frame(blocks=blocks)],
    ).text

    assert root_text.count("\f") == 2
    assert nested_text.count("\f") == 2
    assert "Block content omitted at safe text limit" in root_text
    assert "Block content omitted at safe text limit" in nested_text


def test_document_text_bounds_repeated_large_block_aliases() -> None:
    content = "X" * 10_000
    paragraph = Paragraph(runs=[TextRun(content)])
    document = Document("aliases.sam", "cp1252", blocks=[paragraph] * 1_000)

    rendered = document.text

    assert rendered.count(content) == 1
    assert rendered.count("Repeated block omitted at safe text limit") == 1
    assert len(rendered) < 20_000


def test_document_to_dict_marks_sequence_and_mapping_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model, "_MAX_JSON_ITEMS", 2)
    document = Document(
        "bounded.sam",
        "cp1252",
        metadata={"one": "1", "two": "2", "three": "3"},
        blocks=[PageBreak(), PageBreak(), PageBreak()],
    )

    encoded = document.to_dict()

    assert encoded["blocks"][-1] == {
        "encoding": "block-limit",
        "message": "[Content omitted at safe JSON item limit]",
        "omitted_count": 1,
    }
    assert encoded["metadata"]["encoding"] == "mapping-entries"
    assert encoded["metadata"]["omitted_count"] == 1
