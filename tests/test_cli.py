from __future__ import annotations

from pathlib import Path

from amipro_sam.cli import main


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
