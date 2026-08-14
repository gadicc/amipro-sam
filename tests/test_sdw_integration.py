from __future__ import annotations

import hashlib
import importlib.util
import json as stdlib_json
import struct
from dataclasses import replace
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

import amipro_sam.parser as parser_module
from amipro_sam.errors import ParseError, ResourceLimitError
from amipro_sam.limits import ParseLimits
from amipro_sam.model import Document, SdwDrawing, SdwPreview
from amipro_sam.parser import parse_bytes
from amipro_sam.renderers import docx, html, json, markdown, odt, pdf, text


def _sdw(*records: bytes, declared_length: int | None = None) -> bytes:
    body = b"".join(records)
    length = 22 + len(body) if declared_length is None else declared_length
    return struct.pack(
        "<4sHHIhhhhH",
        b"SM\x02\x01",
        7,
        9,
        len(records),
        0,
        0,
        10,
        10,
        length,
    ) + body


def _sdw_preview(
    *,
    width: int = 2,
    height: int = 2,
    bits_per_plane: int = 8,
    plane_count: int = 1,
    payload: bytes | None = None,
) -> bytes:
    stride = ((width * bits_per_plane + 15) // 16) * 2
    if payload is None:
        if (bits_per_plane, plane_count) == (8, 1):
            payload = bytes((0, 255, 128, 64))
        elif (bits_per_plane, plane_count) == (1, 4):
            values = ((0, 15), (3, 12))
            packed = bytearray()
            for row in values:
                for plane in range(4):
                    byte = sum(((value >> plane) & 1) << (7 - x) for x, value in enumerate(row))
                    packed.extend((byte, 0))
            payload = bytes(packed)
        else:
            payload = bytes(stride * height * plane_count)
    return (
        b"SS"
        + struct.pack(
            "<HHHBB4H",
            width,
            height,
            stride,
            bits_per_plane,
            plane_count,
            17,
            23,
            42,
            99,
        )
        + payload
    )


def _embedded_sam(rows: list[tuple[bytes, bytes]]) -> bytes:
    prefix = (
        "[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[charset]\r\n\t82\r\n"
        "\tANSI (Windows, IBM CP 1252)\r\n[edoc]\r\n"
        "Readable before\r\n>\r\n"
    ).encode("ascii")
    payload = bytearray()
    manifest_rows: list[str] = []
    offset = len(prefix)
    for asset_id, (asset, companion) in enumerate(rows, start=1):
        asset_offset = offset + len(payload)
        payload.extend(asset)
        companion_offset = offset + len(payload)
        payload.extend(companion)
        manifest_rows.append(
            f"{asset_id} .sdw {asset_offset} {len(asset)} "
            f"{companion_offset} {len(companion)} \r\n"
        )
    marker_offset = offset + len(payload) + 2
    manifest = (
        "[Embedded]\r\n" + "".join(manifest_rows) + f"{marker_offset:08d}\r\n"
    ).encode("ascii")
    return prefix + bytes(payload) + b"\r\n" + manifest


def _drawings(document: Document) -> list[SdwDrawing]:
    return [block for block in document.blocks if isinstance(block, SdwDrawing)]


def _malformed_drawing_with_preview() -> tuple[Document, SdwDrawing]:
    malformed = _sdw(declared_length=21)
    document = parse_bytes(_embedded_sam([(malformed, _sdw_preview())]))
    return document, _drawings(document)[0]


def _manual_drawing(raw: bytes) -> SdwDrawing:
    return SdwDrawing(
        asset_id="manual",
        declared_offset=0,
        declared_length=len(raw),
        data=raw,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        status="malformed",
        reason="synthetic validation failure",
        preview=None,
        alt_text="Synthetic Ami Draw object",
    )


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON numeric token: {value}")


def _odt_text(payload: bytes) -> str:
    """Extract ODF text while expanding explicit ``text:s`` space elements."""

    text_namespace = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    space_tag = f"{{{text_namespace}}}s"
    count_attribute = f"{{{text_namespace}}}c"

    def visit(element: ET.Element) -> str:
        chunks = [element.text or ""]
        for child in element:
            if child.tag == space_tag:
                raw_count = child.attrib.get(count_attribute, "1")
                try:
                    count = max(1, int(raw_count))
                except ValueError:
                    count = 1
                chunks.append(" " * count)
            else:
                chunks.append(visit(child))
            chunks.append(child.tail or "")
        return "".join(chunks)

    return visit(ET.fromstring(payload))


def _render_all_available(document: Document) -> dict[str, bytes]:
    outputs = {
        "html": html.render(document),
        "markdown": markdown.render(document),
        "text": text.render(document),
        "json": json.render(document),
        "pdf": pdf.render(document),
        "odt": odt.render(document),
    }
    if importlib.util.find_spec("docx") is not None:
        outputs["docx"] = docx.render(document)
    return outputs


def _archive_members(payload: bytes) -> dict[str, bytes]:
    with ZipFile(BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _assert_sources_absent(outputs: dict[str, bytes], *sources: bytes) -> None:
    assert sources and all(sources)
    for output_name, payload in outputs.items():
        if output_name in {"odt", "docx"}:
            for member_name, member in _archive_members(payload).items():
                for source in sources:
                    assert source not in member, f"raw source reached {output_name}:{member_name}"
        else:
            for source in sources:
                assert source not in payload, f"raw source reached {output_name}"


def _assert_supported_preview_outputs(outputs: dict[str, bytes]) -> None:
    marker = b"Ami Draw companion preview"
    assert b"data:image/png;base64," in outputs["html"]
    assert marker in outputs["html"]
    assert marker in outputs["markdown"]
    assert marker in outputs["text"]
    assert b'"preview": {' in outputs["json"]
    assert b"/Subtype /Image" in outputs["pdf"]

    odt_members = _archive_members(outputs["odt"])
    assert any(name.startswith("Pictures/SDW") for name in odt_members)
    assert "Ami Draw companion preview" in _odt_text(odt_members["content.xml"])

    if "docx" in outputs:
        docx_members = _archive_members(outputs["docx"])
        assert any(name.startswith("word/media/") for name in docx_members)
        assert marker in docx_members["word/document.xml"]


def _assert_placeholder_outputs(outputs: dict[str, bytes]) -> None:
    marker = b"no valid companion preview"
    assert b"data:image/png;base64," not in outputs["html"]
    assert marker in outputs["html"]
    assert marker in outputs["markdown"]
    assert marker in outputs["text"]
    assert b'"preview": null' in outputs["json"]
    assert b"/Subtype /Image" not in outputs["pdf"]

    odt_members = _archive_members(outputs["odt"])
    assert not any(name.startswith("Pictures/SDW") for name in odt_members)
    assert "no valid companion preview" in _odt_text(odt_members["content.xml"])

    if "docx" in outputs:
        docx_members = _archive_members(outputs["docx"])
        assert not any(name.startswith("word/media/") for name in docx_members)
        assert marker in docx_members["word/document.xml"]


def test_parser_preserves_validated_sdw_and_builds_bounded_companion_preview() -> None:
    asset = _sdw()
    companion = _sdw_preview()
    document = parse_bytes(_embedded_sam([(asset, companion)]))
    drawing = _drawings(document)[0]

    assert drawing.data == asset
    assert drawing.source_sha256 == hashlib.sha256(asset).hexdigest()
    assert drawing.status == "validated"
    assert drawing.signature_family == "common-sm-family"
    assert drawing.header_field_1 == 7
    assert drawing.header_field_2 == 9
    assert drawing.direct_record_count == 0
    assert drawing.bounds == (0, 0, 10, 10)
    assert drawing.declared_stream_length == len(asset)
    assert drawing.companion_data == companion
    assert drawing.companion_sha256 == hashlib.sha256(companion).hexdigest()
    assert drawing.preview is not None
    assert (drawing.preview.width_px, drawing.preview.height_px) == (2, 2)
    assert drawing.preview.rgb_data == bytes(
        (0, 0, 0, 255, 255, 255, 128, 128, 128, 64, 64, 64)
    )
    assert "Readable before" in document.text
    assert "Ami Draw companion preview" in document.text
    assert any(item.code == "sdw-vector-unsupported" for item in document.diagnostics)

    with pytest.raises(ParseError, match="sdw-vector-unsupported"):
        parse_bytes(_embedded_sam([(asset, companion)]), strict=True)


def test_malformed_primary_remains_typed_and_can_use_valid_companion() -> None:
    document, drawing = _malformed_drawing_with_preview()
    malformed = _sdw(declared_length=21)
    companion = _sdw_preview()

    assert drawing.status == "malformed"
    assert drawing.data == malformed
    assert drawing.source_sha256 == hashlib.sha256(malformed).hexdigest()
    assert drawing.companion_data == companion
    assert drawing.companion_sha256 == hashlib.sha256(companion).hexdigest()
    assert drawing.preview is not None
    assert any(item.code == "sdw-invalid-stream-size" for item in document.diagnostics)
    assert "Readable before" in document.text
    assert "vector status=malformed" in document.text

    outputs = _render_all_available(document)
    _assert_sources_absent(outputs, malformed, companion)
    _assert_supported_preview_outputs(outputs)
    assert b'"status": "malformed"' in outputs["json"]


def test_public_ascii_signature_variant_is_preserved_without_speculative_decoding() -> None:
    asset = b"AMI_METAFILE_FORMAT VERSION synthetic-test-only\x00"
    document = parse_bytes(_embedded_sam([(asset, b"")]))
    drawing = _drawings(document)[0]

    assert drawing.data == asset
    assert drawing.source_sha256 == hashlib.sha256(asset).hexdigest()
    assert drawing.signature_family == "ascii-variant"
    assert drawing.status == "malformed"
    assert drawing.preview is None
    assert any(item.code == "sdw-invalid-signature" for item in document.diagnostics)

    outputs = _render_all_available(document)
    _assert_sources_absent(outputs, asset)
    _assert_placeholder_outputs(outputs)
    assert b'"signature_family": "ascii-variant"' in outputs["json"]


def test_validated_root_trailing_bytes_are_hashed_diagnosed_and_never_emitted() -> None:
    tail = b"synthetic-root-tail"
    asset = _sdw() + tail
    document = parse_bytes(_embedded_sam([(asset, b"")]))
    drawing = _drawings(document)[0]

    assert drawing.data == asset
    assert drawing.source_sha256 == hashlib.sha256(asset).hexdigest()
    assert drawing.status == "validated"
    assert drawing.trailing_bytes == len(tail)
    assert any(item.code == "sdw-trailing-data" for item in document.diagnostics)
    assert any(item.code == "sdw-vector-unsupported" for item in document.diagnostics)

    outputs = _render_all_available(document)
    _assert_sources_absent(outputs, asset)
    _assert_placeholder_outputs(outputs)
    assert f'"trailing_bytes": {len(tail)}'.encode("ascii") in outputs["json"]


@pytest.mark.parametrize(
    ("bits_per_plane", "payload"),
    [
        (16, bytes((1, 2, 3, 4))),
        (24, bytes((5, 6, 7, 8, 9, 10))),
    ],
)
def test_unsupported_companion_formats_are_preserved_without_guessing_colors(
    bits_per_plane: int, payload: bytes
) -> None:
    asset = _sdw()
    companion = _sdw_preview(
        width=2,
        height=1,
        bits_per_plane=bits_per_plane,
        payload=payload,
    )
    document = parse_bytes(_embedded_sam([(asset, companion)]))
    drawing = _drawings(document)[0]

    assert drawing.preview is None
    assert drawing.companion_data == companion
    assert drawing.companion_sha256 == hashlib.sha256(companion).hexdigest()
    assert any(
        item.code == "sdw-preview-unsupported-format" for item in document.diagnostics
    )

    outputs = _render_all_available(document)
    _assert_sources_absent(outputs, asset, companion)
    _assert_placeholder_outputs(outputs)


def test_invalid_preview_does_not_consume_later_document_pixel_budget() -> None:
    malformed = _sdw_preview() + b"extra"
    document = parse_bytes(
        _embedded_sam([(_sdw(), malformed), (_sdw(), _sdw_preview())]),
        limits=ParseLimits(max_total_sdw_pixels=4),
    )

    assert sum(drawing.preview is not None for drawing in _drawings(document)) == 1
    assert any(item.code == "sdw-preview-size-mismatch" for item in document.diagnostics)
    assert not any(
        item.code == "sdw-preview-total-pixel-limit" for item in document.diagnostics
    )


def test_document_wide_sdw_pixel_budget_is_bounded() -> None:
    document = parse_bytes(
        _embedded_sam([(_sdw(), _sdw_preview()), (_sdw(), _sdw_preview())]),
        limits=ParseLimits(max_total_sdw_pixels=4),
    )

    assert sum(drawing.preview is not None for drawing in _drawings(document)) == 1
    assert any(
        item.code == "sdw-preview-total-pixel-limit" for item in document.diagnostics
    )


def test_out_of_range_sdw_row_is_explicit_typed_unavailable_object() -> None:
    prefix = (
        "[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[charset]\r\n\t82\r\n"
        "\tANSI (Windows, IBM CP 1252)\r\n[edoc]\r\nBody\r\n>\r\n"
    ).encode("ascii")
    source = prefix + b"[Embedded]\r\n1 .sdw 999999 22 0 0 \r\n00000100\r\n"
    drawing = _drawings(parse_bytes(source))[0]

    assert drawing.status == "unavailable"
    assert drawing.data is None
    assert drawing.source_sha256 is None
    assert drawing.declared_length == 22


def test_caller_raised_sdw_byte_limit_cannot_load_over_hard_primary_or_companion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep this parser-integration regression small while exercising the same
    # before-slice branch as the fixed 16 MiB production ceiling.
    monkeypatch.setattr(parser_module, "sdw_asset_limit", lambda _limits: 22)
    over_hard_primary = _sdw() + b"x"
    over_hard_companion = _sdw_preview() + b"x"
    document = parse_bytes(
        _embedded_sam(
            [
                (over_hard_primary, b""),
                (_sdw(), over_hard_companion),
            ]
        ),
        limits=ParseLimits(max_embedded_asset_bytes=1_000_000),
    )
    first, second = _drawings(document)

    assert first.data is None
    assert first.source_sha256 is None
    assert first.status == "unavailable"
    assert second.data == _sdw()
    assert second.companion_data is None
    assert second.companion_sha256 is None
    assert second.preview is None
    assert any(item.code == "sdw-asset-too-large" for item in document.diagnostics)
    assert any(item.code == "sdw-preview-too-large" for item in document.diagnostics)


def test_repeated_sdw_range_cannot_bypass_caller_raised_total_asset_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _sdw()
    prefix = (
        "[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[charset]\r\n\t82\r\n"
        "\tANSI (Windows, IBM CP 1252)\r\n[edoc]\r\nReadable before\r\n>\r\n"
    ).encode("ascii")
    asset_offset = len(prefix)
    rows = "".join(
        f"{asset_id} .sdw {asset_offset} {len(asset)} 0 0 \r\n"
        for asset_id in (1, 2)
    )
    source = (
        prefix
        + asset
        + b"\r\n[Embedded]\r\n"
        + rows.encode("ascii")
        + b"00000000\r\n"
    )

    original = parser_module._effective_lowerable_limit

    def small_total_hard_cap(configured: object, hard: int, description: str) -> int:
        if description == "embedded asset total byte limit":
            hard = len(asset) * 2 - 1
        return original(configured, hard, description)

    monkeypatch.setattr(
        parser_module, "_effective_lowerable_limit", small_total_hard_cap
    )
    with pytest.raises(ResourceLimitError, match=r"embedded asset total exceeds 43 bytes"):
        parse_bytes(
            source,
            limits=ParseLimits(max_total_asset_bytes=1_000_000),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_total_asset_bytes", True, "embedded asset total byte limit"),
        ("max_total_asset_bytes", -1, "embedded asset total byte limit"),
        ("max_total_asset_bytes", "64", "embedded asset total byte limit"),
        ("max_total_sdw_pixels", True, "document-wide SDW pixel limit"),
        ("max_total_sdw_pixels", -1, "document-wide SDW pixel limit"),
        ("max_total_sdw_pixels", "8", "document-wide SDW pixel limit"),
    ],
)
def test_invalid_total_asset_and_sdw_pixel_limits_are_controlled(
    field: str, value: object, message: str
) -> None:
    limits = replace(ParseLimits(), **{field: value})

    with pytest.raises(ResourceLimitError, match=message):
        parse_bytes(_embedded_sam([(_sdw(), _sdw_preview())]), limits=limits)


def test_all_renderers_use_only_fresh_preview_or_explicit_marker() -> None:
    asset = _sdw()
    companion = _sdw_preview()
    document = parse_bytes(_embedded_sam([(asset, companion)]))
    outputs = _render_all_available(document)
    _assert_sources_absent(outputs, asset, companion)
    _assert_supported_preview_outputs(outputs)

    html_bytes = outputs["html"]
    assert b"data:image/png;base64," in html_bytes
    assert b"Ami Draw companion preview" in html_bytes
    assert b"<object" not in html_bytes and b"<embed" not in html_bytes

    json_bytes = outputs["json"]
    assert b'"encoding": "not-inlined"' in json_bytes
    assert drawing_hash(document).encode("ascii") in json_bytes

    pdf_bytes = outputs["pdf"]
    assert pdf_bytes.startswith(b"%PDF-")
    assert b"/JavaScript" not in pdf_bytes and b"/EmbeddedFile" not in pdf_bytes

    odt_bytes = outputs["odt"]
    with ZipFile(BytesIO(odt_bytes)) as archive:
        names = archive.namelist()
        assert any(name.startswith("Pictures/SDW") and name.endswith(".png") for name in names)
        assert not any(name.lower().endswith(".sdw") for name in names)
        assert "Ami Draw companion preview" in _odt_text(archive.read("content.xml"))
    assert b'TargetMode="External"' not in odt_bytes


def drawing_hash(document: Document) -> str:
    return _drawings(document)[0].source_sha256 or ""


@pytest.mark.skipif(
    importlib.util.find_spec("docx") is None,
    reason="python-docx extra not installed",
)
def test_docx_packages_only_generated_sdw_png() -> None:
    asset = _sdw()
    companion = _sdw_preview()
    document = parse_bytes(_embedded_sam([(asset, companion)]))
    payload = docx.render(document)
    _assert_sources_absent({"docx": payload}, asset, companion)

    with ZipFile(BytesIO(payload)) as archive:
        names = archive.namelist()
        assert any(name.startswith("word/media/") and name.endswith(".png") for name in names)
        assert not any(name.lower().endswith(".sdw") for name in names)
        assert b"Ami Draw companion preview" in archive.read("word/document.xml")
        relationships = b"".join(
            archive.read(name) for name in names if name.endswith(".rels")
        )
        assert b'TargetMode="External"' not in relationships


def test_hostile_manual_sdw_ir_cannot_crash_renderers_or_emit_raw_payload() -> None:
    raw = b"<script src='file:///secret'>"
    drawing = SdwDrawing(
        asset_id="1",
        declared_offset=0,
        declared_length=len(raw),
        data=raw,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        status="validated",
        reason="<img onerror=alert(1)>",
        preview=SdwPreview(
            width_px=1,
            height_px=1,
            rgb_data=b"",
            source_sha256="x",
            bits_per_plane=8,
            plane_count=1,
            stride=2,
            opaque_header=(0, 0, 0, 0),
        ),
        alt_text=b"<script>",  # type: ignore[arg-type]
    )
    document = Document("hostile.sam", "cp1252", blocks=[drawing])

    for renderer in (html, markdown, text, pdf, odt):
        rendered = renderer.render(document)
        assert raw not in rendered
    assert b"<script>" not in html.render(document)


def test_document_text_handles_invalid_manual_sdw_preview() -> None:
    drawing = SdwDrawing(
        asset_id="synthetic",
        declared_offset=0,
        declared_length=0,
        preview=object(),  # type: ignore[arg-type]
    )

    assert "rendering unavailable" in Document(
        "manual.sam", "cp1252", blocks=[drawing]
    ).text


@pytest.mark.parametrize(
    "renderer",
    [
        pytest.param(html, id="html"),
        pytest.param(markdown, id="markdown"),
        pytest.param(text, id="text"),
        pytest.param(pdf, id="pdf"),
        pytest.param(odt, id="odt"),
        pytest.param(
            docx,
            id="docx",
            marks=pytest.mark.skipif(
                importlib.util.find_spec("docx") is None,
                reason="python-docx extra not installed",
            ),
        ),
        pytest.param(json, id="json"),
    ],
)
@pytest.mark.parametrize(
    "field",
    [
        "alt_text",
        "status",
        "reason",
        "declared_length",
        "source_sha256",
        "preview",
    ],
)
def test_unsupported_manual_sdw_fields_do_not_crash_or_inline_source_bytes(
    renderer: object, field: str
) -> None:
    raw = b"RAW_SYNTHETIC_SDW_PAYLOAD"
    drawing = _manual_drawing(raw)
    setattr(drawing, field, object())
    document = Document("manual.sam", "cp1252", blocks=[drawing])

    rendered = renderer.render(document)  # type: ignore[attr-defined]

    assert raw not in rendered
    if renderer is json:
        encoded = stdlib_json.loads(rendered)["blocks"][0][field]
        assert encoded == {"encoding": "unsupported-value", "type": "object"}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_json_replaces_non_finite_manual_sdw_numbers(value: float) -> None:
    drawing = _manual_drawing(b"RAW_SYNTHETIC_SDW_PAYLOAD")
    drawing.declared_length = value  # type: ignore[assignment]

    rendered = json.render(Document("manual.sam", "cp1252", blocks=[drawing]))
    parsed = stdlib_json.loads(rendered, parse_constant=_reject_json_constant)

    assert parsed["blocks"][0]["declared_length"] == {
        "encoding": "non-finite-number"
    }
    assert b"NaN" not in rendered
    assert b"Infinity" not in rendered


def test_malformed_vector_status_is_visible_in_non_optional_outputs() -> None:
    document, drawing = _malformed_drawing_with_preview()
    marker = "vector status=malformed"

    assert marker in html.render(document).decode("utf-8")
    assert marker in markdown.render(document).decode("utf-8")
    assert marker in text.render(document).decode("utf-8")

    parsed = stdlib_json.loads(json.render(document))
    drawing_json = next(
        block for block in parsed["blocks"] if block["type"] == "SdwDrawing"
    )
    assert drawing_json["status"] == drawing.status == "malformed"

    with ZipFile(BytesIO(odt.render(document))) as archive:
        assert marker in _odt_text(archive.read("content.xml"))


def test_malformed_vector_status_is_visible_in_pdf_when_pypdf_is_available() -> None:
    pypdf = pytest.importorskip("pypdf")
    document, _drawing = _malformed_drawing_with_preview()

    reader = pypdf.PdfReader(BytesIO(pdf.render(document)))
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert "vector status=malformed" in " ".join(extracted.split())


@pytest.mark.skipif(
    importlib.util.find_spec("docx") is None,
    reason="python-docx extra not installed",
)
def test_malformed_vector_status_is_visible_in_docx() -> None:
    document, _drawing = _malformed_drawing_with_preview()

    with ZipFile(BytesIO(docx.render(document))) as archive:
        xml = ET.fromstring(archive.read("word/document.xml"))

    assert "vector status=malformed" in "".join(xml.itertext())
