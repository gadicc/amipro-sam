from __future__ import annotations

import base64
import hashlib
import importlib.util
import random
import re
import struct
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

import amipro_sam.wmf as wmf_module
from amipro_sam.limits import ParseLimits
from amipro_sam.model import Document, UnsupportedObject, WmfGraphic
from amipro_sam.parser import parse_bytes
from amipro_sam.renderers import docx, html, json, markdown, odt, pdf, text
from amipro_sam.wmf import WmfDecodeError, decode_wmf, wmf_png

_EOF = 0x0000
_REALIZE_PALETTE = 0x0035
_CREATE_PALETTE = 0x00F7
_SET_MAP_MODE = 0x0103
_SET_WINDOW_ORIGIN = 0x020B
_SET_WINDOW_EXTENT = 0x020C
_SELECT_PALETTE = 0x0234
_ESCAPE = 0x0626
_DIB_STRETCH_BLT = 0x0B41


def _record(function: int, payload: bytes = b"") -> bytes:
    assert len(payload) % 2 == 0
    return struct.pack("<IH", 3 + len(payload) // 2, function) + payload


def _logical_palette(*, flags: int = 0) -> bytes:
    entries = bytes((0, 0, 0, flags, 255, 255, 255, 0))
    return struct.pack("<HH", 0x0300, 2) + entries


def _indexed_row(values: list[int], bits_per_pixel: int, stride: int) -> bytes:
    if bits_per_pixel == 8:
        packed = bytes(values)
    elif bits_per_pixel == 4:
        packed = bytes(
            (values[index] << 4)
            | (values[index + 1] if index + 1 < len(values) else 0)
            for index in range(0, len(values), 2)
        )
    else:
        result = bytearray((len(values) + 7) // 8)
        for index, value in enumerate(values):
            result[index // 8] |= (value & 1) << (7 - index % 8)
        packed = bytes(result)
    return packed + bytes(stride - len(packed))


def _dib(
    *,
    bits_per_pixel: int = 4,
    width: int = 2,
    height: int = 2,
    top_down: bool = False,
    reserved: int = 0,
    planes: int = 1,
    compression: int = 0,
    size_image_mode: str = "zero",
    colors_important: int = 0,
    default_color_table: bool = False,
) -> bytes:
    signed_height = -height if top_down else height
    row_stride = ((width * bits_per_pixel + 31) // 32) * 4
    if bits_per_pixel == 24:
        top = bytes((0, 0, 255, 0, 255, 0)) + bytes(max(0, row_stride - 6))
        bottom = bytes((255, 0, 0, 255, 255, 255)) + bytes(max(0, row_stride - 6))
        palette = b""
        colors_used = 0
    else:
        top = _indexed_row([0, 1], bits_per_pixel, row_stride)
        bottom = _indexed_row([1, 0], bits_per_pixel, row_stride)
        entries = [
            bytes((0, 0, 0, reserved)),
            bytes((255, 255, 255, 0)),
        ]
        if default_color_table:
            entries.extend(
                bytes((0, 0, 0, 0)) for _ in range((1 << bits_per_pixel) - 2)
            )
            colors_used = 0
        else:
            colors_used = 2
        palette = b"".join(entries)
    display_rows = [top if index % 2 == 0 else bottom for index in range(height)]
    rows = b"".join(display_rows if top_down else reversed(display_rows))
    size_image = len(rows) if size_image_mode == "exact" else 0
    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        signed_height,
        planes,
        bits_per_pixel,
        compression,
        size_image,
        0,
        0,
        colors_used,
        colors_important,
    )
    return header + palette + rows


def _raster_record(
    *,
    bits_per_pixel: int = 4,
    width: int = 2,
    height: int = 2,
    top_down: bool = False,
    destination_height: int | None = None,
    source_x: int = 0,
    source_y: int = 0,
    destination_x: int = 0,
    destination_y: int = 0,
    raster_operation: int = 0x00CC0020,
    dib: bytes | None = None,
) -> bytes:
    signed_source_height = -height if top_down else height
    destination_height = height if destination_height is None else destination_height
    payload = struct.pack(
        "<Ihhhhhhhh",
        raster_operation,
        signed_source_height,
        width,
        source_y,
        source_x,
        destination_height,
        width,
        destination_y,
        destination_x,
    )
    return _record(
        _DIB_STRETCH_BLT,
        payload
        + (
            dib
            if dib is not None
            else _dib(
                bits_per_pixel=bits_per_pixel,
                width=width,
                height=height,
                top_down=top_down,
            )
        ),
    )


def _wmf(
    *,
    bits_per_pixel: int = 4,
    width: int = 2,
    height: int = 2,
    top_down: bool = False,
    destination_height: int | None = None,
    placeable: bool = False,
    palette_before: bool = False,
    include_palette: bool = True,
    include_map_mode: bool = False,
    object_slots: int | None = None,
    records: list[bytes] | None = None,
) -> bytes:
    destination_height = height if destination_height is None else destination_height
    if records is None:
        records = []
        if include_map_mode:
            records.append(_record(_SET_MAP_MODE, struct.pack("<h", 8)))
        records.extend(
            [
                _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
                _record(
                    _SET_WINDOW_EXTENT,
                    struct.pack("<hh", destination_height, width),
                ),
            ]
        )
        palette_records = [
            _record(_CREATE_PALETTE, _logical_palette()),
            _record(_SELECT_PALETTE, struct.pack("<H", 0)),
        ]
        if palette_before and include_palette:
            records.extend(palette_records)
            records.append(_record(_REALIZE_PALETTE))
        records.append(
            _raster_record(
                bits_per_pixel=bits_per_pixel,
                width=width,
                height=height,
                top_down=top_down,
                destination_height=destination_height,
            )
        )
        if include_palette and not palette_before:
            records.extend(palette_records)
        records.append(_record(_EOF))
    object_slots = int(include_palette) if object_slots is None else object_slots
    body = b"".join(records)
    sizes = [struct.unpack_from("<I", record)[0] for record in records]
    standard = struct.pack(
        "<HHHIHIH",
        1,
        9,
        0x0300,
        9 + len(body) // 2,
        object_slots,
        max(sizes, default=3),
        0,
    ) + body
    if not placeable:
        return standard
    return _placeable(standard)


def _placeable(
    standard: bytes,
    *,
    handle: int = 0,
    left: int = 0,
    top: int = 0,
    right: int = 200,
    bottom: int = 100,
    inch: int = 100,
    reserved: int = 0,
) -> bytes:
    prefix = struct.pack(
        "<IHhhhhHI",
        0x9AC6CDD7,
        handle,
        left,
        top,
        right,
        bottom,
        inch,
        reserved,
    )
    checksum = 0
    for word in struct.unpack("<10H", prefix):
        checksum ^= word
    return prefix + struct.pack("<H", checksum) + standard


def _embedded_sam(asset: bytes) -> bytes:
    return _embedded_sam_many([asset])


def _embedded_sam_many(assets: list[bytes]) -> bytes:
    prefix = (
        "[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[charset]\r\n\t82\r\n"
        "\tANSI (Windows, IBM CP 1252)\r\n[edoc]\r\n"
        "Readable before\r\n>\r\n"
    ).encode("ascii")
    offset = len(prefix)
    rows: list[str] = []
    for asset_id, asset in enumerate(assets, start=1):
        rows.append(f"{asset_id} .wmf {offset} {len(asset)} 0 0 \r\n")
        offset += len(asset)
    marker_offset = offset + 2
    manifest = (
        "[Embedded]\r\n" + "".join(rows) + f"{marker_offset:08d}\r\n"
    ).encode("ascii")
    return prefix + b"".join(assets) + b"\r\n" + manifest


@pytest.mark.parametrize("bits_per_pixel", [1, 4, 8, 24])
def test_decodes_evidenced_uncompressed_dib_depths(bits_per_pixel: int) -> None:
    source = _wmf(bits_per_pixel=bits_per_pixel)

    graphic = decode_wmf(source, limits=ParseLimits())

    assert (graphic.width_px, graphic.height_px) == (2, 2)
    expected = (
        bytes((255, 0, 0, 0, 255, 0))
        if bits_per_pixel == 24
        else bytes((0, 0, 0, 255, 255, 255))
    )
    assert graphic.rgb_data[:6] == expected
    assert graphic.source_sha256 == hashlib.sha256(source).hexdigest()
    assert graphic.operations[-1] == "end-of-file"
    assert graphic.record_count == 6
    assert wmf_png(graphic) == wmf_png(graphic)
    assert wmf_png(graphic).startswith(b"\x89PNG\r\n\x1a\n")


def test_decodes_placeable_and_anisotropic_negative_destination_extent() -> None:
    source = _wmf(
        bits_per_pixel=24,
        destination_height=-2,
        placeable=True,
        palette_before=True,
        include_map_mode=True,
    )

    graphic = decode_wmf(source, limits=ParseLimits())

    assert graphic.placeable is True
    assert graphic.width_in == 2.0
    assert graphic.height_in == 1.0
    assert graphic.rgb_data[:6] == bytes((255, 0, 0, 0, 255, 0))
    assert "set-map-mode" in graphic.operations
    assert "realize-palette" in graphic.operations


def test_rejects_top_down_dib_without_corpus_evidence() -> None:
    source = _wmf(bits_per_pixel=24, top_down=True)

    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(source, limits=ParseLimits())
    assert raised.value.code == "unsupported-top-down-dib"


def test_negative_destination_extent_requires_anisotropic_map_mode() -> None:
    source = _wmf(destination_height=-2)

    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(source, limits=ParseLimits())
    assert raised.value.code == "unsupported-transform"


@pytest.mark.parametrize(
    ("field_offset", "value", "code"),
    [
        (0, 2, "unsupported-header"),
        (2, 8, "invalid-header-size"),
        (4, 0x0200, "unsupported-version"),
        (16, 1, "invalid-header"),
    ],
)
def test_rejects_malformed_standard_headers(
    field_offset: int, value: int, code: str
) -> None:
    source = bytearray(_wmf())
    source[field_offset : field_offset + 2] = struct.pack("<H", value)

    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(bytes(source), limits=ParseLimits())
    assert raised.value.code == code


def test_rejects_bad_sizes_max_record_and_placeable_checksum() -> None:
    wrong_size = bytearray(_wmf())
    declared_words = struct.unpack_from("<I", wrong_size, 6)[0]
    struct.pack_into("<I", wrong_size, 6, declared_words + 1)
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(bytes(wrong_size), limits=ParseLimits())
    assert raised.value.code == "file-size-mismatch"

    wrong_max = bytearray(_wmf())
    struct.pack_into("<I", wrong_max, 12, 3)
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(bytes(wrong_max), limits=ParseLimits())
    assert raised.value.code == "max-record-mismatch"

    wrong_checksum = bytearray(_wmf(placeable=True))
    wrong_checksum[20] ^= 1
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(bytes(wrong_checksum), limits=ParseLimits())
    assert raised.value.code == "placeable-checksum"


@pytest.mark.parametrize(
    ("placeable_fields", "expected_code"),
    [
        ({"handle": 1}, "invalid-placeable-header"),
        ({"reserved": 1}, "invalid-placeable-header"),
        ({"inch": 0}, "invalid-placeable-units"),
        ({"right": 0}, "invalid-placeable-bounds"),
        ({"right": -1}, "invalid-placeable-bounds"),
        ({"right": 32_767, "bottom": 32_767, "inch": 1}, "invalid-placeable-bounds"),
    ],
)
def test_rejects_malformed_placeable_header_fields(
    placeable_fields: dict[str, int], expected_code: str
) -> None:
    source = _placeable(_wmf(), **placeable_fields)

    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(source, limits=ParseLimits())
    assert raised.value.code == expected_code


def test_rejects_truncated_placeable_header() -> None:
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(_wmf(placeable=True)[:30], limits=ParseLimits())
    assert raised.value.code == "truncated-placeable-header"


def test_rejects_every_truncation_and_extreme_record_word_lengths() -> None:
    source = _wmf()
    for end in range(len(source)):
        with pytest.raises(WmfDecodeError):
            decode_wmf(source[:end], limits=ParseLimits())

    for record_words in (0, 1, 2, 0xFFFFFFFF):
        mutated = bytearray(source)
        struct.pack_into("<I", mutated, 18, record_words)
        with pytest.raises(WmfDecodeError) as raised:
            decode_wmf(bytes(mutated), limits=ParseLimits())
        assert raised.value.code in {"invalid-record-size", "truncated-record"}


def test_rejects_missing_early_and_malformed_eof() -> None:
    common = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _raster_record(),
    ]
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(
            _wmf(records=common, object_slots=0, include_palette=False),
            limits=ParseLimits(),
        )
    assert raised.value.code == "missing-eof"

    early = [*common, _record(_EOF), _record(_REALIZE_PALETTE)]
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(
            _wmf(records=early, object_slots=0, include_palette=False),
            limits=ParseLimits(),
        )
    assert raised.value.code == "early-eof"

    malformed = [*common, _record(_EOF, b"\0\0")]
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(
            _wmf(records=malformed, object_slots=0, include_palette=False),
            limits=ParseLimits(),
        )
    assert raised.value.code == "invalid-eof"


@pytest.mark.parametrize(
    ("function", "expected_code"),
    [(_ESCAPE, "unsafe-escape"), (0x1234, "unsupported-record")],
)
def test_rejects_escape_and_unknown_operations(
    function: int, expected_code: str
) -> None:
    records = [_record(function), _record(_EOF)]
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(
            _wmf(records=records, object_slots=0, include_palette=False),
            limits=ParseLimits(),
        )
    assert raised.value.code == expected_code


def test_object_table_and_palette_fields_are_bounded_and_typed() -> None:
    invalid_select = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _record(_CREATE_PALETTE, _logical_palette()),
        _record(_SELECT_PALETTE, struct.pack("<H", 0xFFFF)),
        _raster_record(),
        _record(_EOF),
    ]
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(_wmf(records=invalid_select, object_slots=1), limits=ParseLimits())
    assert raised.value.code == "invalid-object-index"

    overflow = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _record(_CREATE_PALETTE, _logical_palette()),
        _raster_record(),
        _record(_EOF),
    ]
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(_wmf(records=overflow, object_slots=0), limits=ParseLimits())
    assert raised.value.code == "object-table-overflow"

    bad_flags = bytearray(_logical_palette(flags=8))
    records = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _record(_CREATE_PALETTE, bytes(bad_flags)),
        _raster_record(),
        _record(_EOF),
    ]
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(_wmf(records=records, object_slots=1), limits=ParseLimits())
    assert raised.value.code == "invalid-palette-flags"


@pytest.mark.parametrize("object_index", [0, 1, 0x7FFF, 0xFFFF])
def test_object_index_boundary_matrix(object_index: int) -> None:
    records = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _record(_CREATE_PALETTE, _logical_palette()),
        _record(_SELECT_PALETTE, struct.pack("<H", object_index)),
        _raster_record(),
        _record(_EOF),
    ]
    source = _wmf(records=records, object_slots=1)

    if object_index == 0:
        assert decode_wmf(source, limits=ParseLimits()).width_px == 2
    else:
        with pytest.raises(WmfDecodeError) as raised:
            decode_wmf(source, limits=ParseLimits())
        assert raised.value.code == "invalid-object-index"


def test_explicit_record_object_palette_dimension_and_byte_limits() -> None:
    source = _wmf()
    for limits, expected_code in (
        (ParseLimits(max_wmf_records=5), "record-limit"),
        (ParseLimits(max_wmf_objects=0), "object-limit"),
        (ParseLimits(max_wmf_palette_entries=1), "palette-limit"),
        (ParseLimits(max_wmf_dimension=1), "dimension-limit"),
        (ParseLimits(max_wmf_pixels=3), "pixel-limit"),
        (ParseLimits(max_embedded_asset_bytes=1), "asset-limit"),
        (ParseLimits(max_file_bytes=1), "asset-limit"),
    ):
        with pytest.raises(WmfDecodeError) as raised:
            decode_wmf(source, limits=limits)
        assert raised.value.code == expected_code

    assert decode_wmf(source, limits=ParseLimits(max_wmf_records=6)).record_count == 6
    assert decode_wmf(source, limits=ParseLimits(max_wmf_objects=1)).width_px == 2
    assert decode_wmf(source, limits=ParseLimits(max_wmf_palette_entries=2)).width_px == 2


def test_caller_raised_limits_cannot_exceed_absolute_renderer_caps() -> None:
    source = _wmf(bits_per_pixel=1, width=4_097, height=1, include_palette=False)

    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(
            source,
            limits=ParseLimits(
                max_wmf_dimension=10_000,
                max_wmf_pixels=10_000,
                max_total_wmf_pixels=10_000,
            ),
        )
    assert raised.value.code == "dimension-limit"


def test_exact_image_size_and_default_color_table_are_accepted() -> None:
    dib = _dib(
        bits_per_pixel=4,
        size_image_mode="exact",
        default_color_table=True,
    )
    records = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _raster_record(dib=dib),
        _record(_EOF),
    ]

    graphic = decode_wmf(
        _wmf(records=records, object_slots=0, include_palette=False),
        limits=ParseLimits(),
    )

    assert graphic.rgb_data[:6] == bytes((0, 0, 0, 255, 255, 255))


def test_transform_dib_and_allocation_limits_reject_before_rendering() -> None:
    zero_extent = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 0, 2)),
        _raster_record(),
        _record(_EOF),
    ]
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(
            _wmf(records=zero_extent, object_slots=0, include_palette=False),
            limits=ParseLimits(),
        )
    assert raised.value.code == "invalid-transform"

    nonzero_origin = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 1, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _raster_record(),
        _record(_EOF),
    ]
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(
            _wmf(records=nonzero_origin, object_slots=0, include_palette=False),
            limits=ParseLimits(),
        )
    assert raised.value.code == "unsupported-transform"

    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(_wmf(include_palette=False), limits=ParseLimits(max_wmf_pixels=3))
    assert raised.value.code == "pixel-limit"

    huge_header = bytearray(_dib())
    struct.pack_into("<i", huge_header, 4, 10_000)
    records = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _raster_record(dib=bytes(huge_header)),
        _record(_EOF),
    ]
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(
            _wmf(records=records, object_slots=0, include_palette=False),
            limits=ParseLimits(),
        )
    assert raised.value.code == "dimension-limit"


@pytest.mark.parametrize("coordinate", [-32_768, -1, 0, 1, 32_767])
def test_window_origin_coordinate_boundary_matrix(coordinate: int) -> None:
    records = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", coordinate, coordinate)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _raster_record(),
        _record(_EOF),
    ]
    source = _wmf(records=records, object_slots=0, include_palette=False)

    if coordinate == 0:
        assert decode_wmf(source, limits=ParseLimits()).width_px == 2
    else:
        with pytest.raises(WmfDecodeError) as raised:
            decode_wmf(source, limits=ParseLimits())
        assert raised.value.code == "unsupported-transform"


@pytest.mark.parametrize("coordinate", [-32_768, -1, 0, 1, 32_767])
def test_source_and_destination_origin_coordinate_fuzz(coordinate: int) -> None:
    for source_origin in (True, False):
        records = [
            _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
            _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
            _raster_record(
                source_x=coordinate if source_origin else 0,
                source_y=coordinate if source_origin else 0,
                destination_x=0 if source_origin else coordinate,
                destination_y=0 if source_origin else coordinate,
            ),
            _record(_EOF),
        ]
        source = _wmf(records=records, object_slots=0, include_palette=False)
        if coordinate == 0:
            assert decode_wmf(source, limits=ParseLimits()).height_px == 2
        else:
            with pytest.raises(WmfDecodeError) as raised:
                decode_wmf(source, limits=ParseLimits())
            assert raised.value.code in {
                "unsupported-source-origin",
                "unsupported-transform",
            }


@pytest.mark.parametrize(
    ("destination_width", "destination_height", "expected_code"),
    [
        (2, 2, None),
        (2, -2, None),
        (-2, 2, "unsupported-transform"),
        (-2, -2, "unsupported-transform"),
        (0, 2, "invalid-transform"),
        (2, 0, "invalid-transform"),
    ],
)
def test_extent_sign_and_zero_coordinate_matrix(
    destination_width: int,
    destination_height: int,
    expected_code: str | None,
) -> None:
    records = [
        _record(_SET_MAP_MODE, struct.pack("<h", 8)),
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(
            _SET_WINDOW_EXTENT,
            struct.pack("<hh", destination_height, destination_width),
        ),
        _record(
            _DIB_STRETCH_BLT,
            struct.pack(
                "<Ihhhhhhhh",
                0x00CC0020,
                2,
                2,
                0,
                0,
                destination_height,
                destination_width,
                0,
                0,
            )
            + _dib(),
        ),
        _record(_EOF),
    ]
    source = _wmf(records=records, object_slots=0, include_palette=False)

    if expected_code is None:
        assert decode_wmf(source, limits=ParseLimits()).width_px == 2
    else:
        with pytest.raises(WmfDecodeError) as raised:
            decode_wmf(source, limits=ParseLimits())
        assert raised.value.code == expected_code


def test_late_map_mode_is_rejected_before_pixel_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real_materialize = wmf_module._materialize_rgb

    def counted_materialize(dib: object) -> bytes:
        nonlocal calls
        calls += 1
        return real_materialize(dib)  # type: ignore[arg-type]

    monkeypatch.setattr(wmf_module, "_materialize_rgb", counted_materialize)
    records = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", -2, 2)),
        _record(_SET_MAP_MODE, struct.pack("<h", 8)),
        _raster_record(destination_height=-2),
        _record(_EOF),
    ]

    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(
            _wmf(records=records, object_slots=0, include_palette=False),
            limits=ParseLimits(),
        )
    assert raised.value.code == "transform-order"
    assert calls == 0


def test_late_unknown_record_rejects_before_rgb_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real_materialize = wmf_module._materialize_rgb

    def counted_materialize(dib: object) -> bytes:
        nonlocal calls
        calls += 1
        return real_materialize(dib)  # type: ignore[arg-type]

    monkeypatch.setattr(wmf_module, "_materialize_rgb", counted_materialize)
    records = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _raster_record(),
        _record(0x1234),
        _record(_EOF),
    ]

    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(
            _wmf(records=records, object_slots=0, include_palette=False),
            limits=ParseLimits(),
        )
    assert raised.value.code == "unsupported-record"
    assert calls == 0


@pytest.mark.parametrize(
    ("dib_mutator", "expected_code"),
    [
        (lambda value: struct.pack_into("<I", value, 0, 12), "unsupported-dib-header"),
        (lambda value: struct.pack_into("<H", value, 12, 2), "invalid-dib-planes"),
        (lambda value: struct.pack_into("<H", value, 14, 32), "unsupported-dib-depth"),
        (
            lambda value: struct.pack_into("<I", value, 16, 1),
            "unsupported-dib-compression",
        ),
        (lambda value: struct.pack_into("<I", value, 20, 1), "image-size-mismatch"),
        (lambda value: value.__setitem__(43, 1), "invalid-color-table"),
    ],
)
def test_rejects_malformed_dib_fields(dib_mutator: object, expected_code: str) -> None:
    dib = bytearray(_dib())
    dib_mutator(dib)  # type: ignore[operator]
    records = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _raster_record(dib=bytes(dib)),
        _record(_EOF),
    ]
    with pytest.raises(WmfDecodeError) as raised:
        decode_wmf(
            _wmf(records=records, object_slots=0, include_palette=False),
            limits=ParseLimits(),
        )
    assert raised.value.code == expected_code


def test_deterministic_mutations_never_escape_controlled_rejection() -> None:
    source = _wmf(bits_per_pixel=8, palette_before=True)
    random_source = random.Random(20260813)
    accepted = 0
    rejected = 0
    for _ in range(250):
        mutated = bytearray(source)
        for _change in range(random_source.randint(1, 4)):
            offset = random_source.randrange(len(mutated))
            mutated[offset] ^= 1 << random_source.randrange(8)
        try:
            graphic = decode_wmf(bytes(mutated), limits=ParseLimits())
            rendered = wmf_png(graphic)
            assert rendered.startswith(b"\x89PNG")
            accepted += 1
        except WmfDecodeError:
            rejected += 1
    assert accepted + rejected == 250
    assert rejected > 0


def test_parser_materializes_only_validated_wmf_and_hides_raw_bytes_in_json() -> None:
    source = _wmf(bits_per_pixel=8)
    document = parse_bytes(_embedded_sam(source))
    graphic = next(block for block in document.blocks if isinstance(block, WmfGraphic))

    assert graphic.source_sha256 == hashlib.sha256(source).hexdigest()
    assert "Readable before" in document.text
    assert "WMF preview: 2 x 2 pixels" in document.text
    serialized = json.render(document)
    assert source not in serialized
    assert b'"encoding": "not-inlined"' in serialized


def test_parser_rejects_unsafe_wmf_as_visible_digest_placeholder() -> None:
    unsafe = _wmf(
        records=[_record(_ESCAPE, struct.pack("<HH", 0, 0)), _record(_EOF)],
        object_slots=0,
        include_palette=False,
    )
    document = parse_bytes(_embedded_sam(unsafe))
    placeholder = next(
        block
        for block in document.blocks
        if isinstance(block, UnsupportedObject) and block.kind == "embedded wmf"
    )

    assert hashlib.sha256(unsafe).hexdigest() in placeholder.description
    assert "safe preview unavailable" in placeholder.description
    assert "Readable before" in document.text
    assert any(item.code == "wmf-unsafe-escape" for item in document.diagnostics)
    assert not any(isinstance(block, WmfGraphic) for block in document.blocks)


def test_parser_total_wmf_pixel_budget_rejects_later_preview_before_allocation() -> None:
    source = _wmf(include_palette=False)
    prefix = (
        "[ver]\r\n\t4\r\n[sty]\r\n\t\r\n[charset]\r\n\t82\r\n"
        "\tANSI (Windows, IBM CP 1252)\r\n[edoc]\r\nBody\r\n>\r\n"
    ).encode("ascii")
    first_offset = len(prefix)
    second_offset = first_offset + len(source)
    marker_offset = second_offset + len(source) + 2
    manifest = (
        f"[Embedded]\r\n1 .wmf {first_offset} {len(source)} 0 0 \r\n"
        f"2 .wmf {second_offset} {len(source)} 0 0 \r\n{marker_offset:08d}\r\n"
    ).encode("ascii")

    document = parse_bytes(
        prefix + source + source + b"\r\n" + manifest,
        limits=ParseLimits(max_total_wmf_pixels=7),
    )

    assert sum(isinstance(block, WmfGraphic) for block in document.blocks) == 1
    assert any(item.code == "wmf-total-pixel-limit" for item in document.diagnostics)


def test_invalid_indexed_preview_does_not_consume_later_pixel_budget() -> None:
    invalid_dib = bytearray(_dib(bits_per_pixel=4))
    struct.pack_into("<I", invalid_dib, 32, 1)
    del invalid_dib[44:48]
    invalid_records = [
        _record(_SET_WINDOW_ORIGIN, struct.pack("<hh", 0, 0)),
        _record(_SET_WINDOW_EXTENT, struct.pack("<hh", 2, 2)),
        _raster_record(dib=bytes(invalid_dib)),
        _record(_EOF),
    ]
    invalid = _wmf(
        records=invalid_records,
        object_slots=0,
        include_palette=False,
    )
    valid = _wmf(include_palette=False)

    document = parse_bytes(
        _embedded_sam_many([invalid, valid]),
        limits=ParseLimits(max_total_wmf_pixels=4),
    )

    assert sum(isinstance(block, WmfGraphic) for block in document.blocks) == 1
    assert any(item.code == "wmf-invalid-palette-index" for item in document.diagnostics)
    assert not any(item.code == "wmf-total-pixel-limit" for item in document.diagnostics)


def _graphic_document() -> Document:
    graphic = decode_wmf(_wmf(bits_per_pixel=24), limits=ParseLimits())
    graphic.alt_text = 'WMF <preview> & "safe"'
    return Document("graphic.sam", "windows-1252", blocks=[graphic])


def test_textual_renderers_emit_inert_safe_wmf_output() -> None:
    document = _graphic_document()
    rendered_html = html.render(document)
    rendered_markdown = markdown.render(document)
    rendered_text = text.render(document)

    assert b"data:image/png;base64," in rendered_html
    assert b"image/wmf" not in rendered_html
    assert b"<script" not in rendered_html
    assert b'WMF &lt;preview&gt; &amp; "safe"' in rendered_html
    encoded = re.search(rb"data:image/png;base64,([A-Za-z0-9+/=]+)", rendered_html)
    assert encoded is not None
    assert base64.b64decode(encoded.group(1)).startswith(b"\x89PNG\r\n\x1a\n")
    assert b"WMF preview" in rendered_markdown
    assert b"&lt;preview&gt;" in rendered_markdown
    assert b"WMF preview" in rendered_text


def test_pdf_and_odt_embed_only_toolkit_generated_png() -> None:
    document = _graphic_document()
    first_pdf = pdf.render(document)
    assert first_pdf == pdf.render(document)
    assert first_pdf.startswith(b"%PDF-")
    assert b"/JavaScript" not in first_pdf
    assert b"/EmbeddedFile" not in first_pdf
    assert b"/URI" not in first_pdf

    first_odt = odt.render(document)
    assert first_odt == odt.render(document)
    with ZipFile(BytesIO(first_odt)) as archive:
        names = archive.namelist()
        assert names[-1] == "Pictures/WMF1.png"
        assert archive.read("Pictures/WMF1.png").startswith(b"\x89PNG")
        content = archive.read("content.xml")
        manifest = archive.read("META-INF/manifest.xml")
        for name in ("content.xml", "META-INF/manifest.xml"):
            ET.fromstring(archive.read(name))
    assert b'Pictures/WMF1.png' in content
    assert b'Pictures/WMF1.png' in manifest
    assert b'TargetMode="External"' not in first_odt
    assert b"image/wmf" not in first_odt


@pytest.mark.skipif(
    importlib.util.find_spec("docx") is None,
    reason="python-docx extra not installed",
)
def test_docx_embeds_generated_png_in_internal_relationship() -> None:
    document = _graphic_document()
    first = docx.render(document)
    assert first == docx.render(document)
    with ZipFile(BytesIO(first)) as archive:
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
        assert media == ["word/media/image1.png"]
        assert archive.read(media[0]).startswith(b"\x89PNG")
        for name in archive.namelist():
            if name.endswith(".rels"):
                relationships = archive.read(name)
                assert b'TargetMode="External"' not in relationships
                assert b"image/wmf" not in relationships


def test_hostile_manual_ir_is_rejected_by_every_renderer() -> None:
    invalid = WmfGraphic(
        width_px=100_000,
        height_px=100_000,
        rgb_data=b"not pixels",
        source_sha256="not trusted",
        alt_text="<script>bad()</script>",
    )
    document = Document("invalid.sam", "utf-8", blocks=[invalid])

    rendered_html = html.render(document)
    assert b"Invalid WMF preview" in rendered_html
    assert b"data:image/" not in rendered_html
    assert b"<script>bad" not in rendered_html
    assert b"Invalid WMF preview" in markdown.render(document)
    assert b"Invalid WMF preview" in text.render(document)
    assert pdf.render(document).startswith(b"%PDF-")
    with ZipFile(BytesIO(odt.render(document))) as archive:
        assert not any(name.startswith("Pictures/") for name in archive.namelist())
        extracted = "".join(ET.fromstring(archive.read("content.xml")).itertext())
        assert "InvalidWMFpreview" in extracted
    if importlib.util.find_spec("docx") is not None:
        with ZipFile(BytesIO(docx.render(document))) as archive:
            assert not any(name.startswith("word/media/") for name in archive.namelist())


def test_renderer_boundary_coerces_non_text_alt_value() -> None:
    graphic = decode_wmf(_wmf(include_palette=False), limits=ParseLimits())
    graphic.alt_text = b"<script>bad()</script>"  # type: ignore[assignment]
    document = Document("hostile-ir.sam", "utf-8", blocks=[graphic])

    assert b"WMF preview" in text.render(document)
    assert b"&lt;script&gt;" in markdown.render(document)
    rendered_html = html.render(document)
    assert b"<script>bad" not in rendered_html
    assert b"&lt;script&gt;" in rendered_html
