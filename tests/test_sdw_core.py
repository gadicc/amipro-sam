from __future__ import annotations

import hashlib
import struct
from dataclasses import replace

import pytest

from amipro_sam.limits import ParseLimits
from amipro_sam.model import SdwDrawing, SdwPreview
from amipro_sam.sdw import (
    SdwDecodeError,
    decode_sdw_preview,
    sdw_display_size,
    sdw_png,
    validate_sdw,
)

_HARD_ASSET_BYTES = 16 * 1024 * 1024


def _record(record_type: int = 99, payload: bytes = b"") -> bytes:
    return struct.pack("<HH", record_type, 4 + len(payload)) + payload


def _type4_points(points: list[tuple[int, int]]) -> bytes:
    assert len(points) <= 255
    result = bytearray(25 + 4 * len(points))
    struct.pack_into("<HH", result, 0, 4, len(result))
    result[23] = len(points)
    for index, point in enumerate(points):
        struct.pack_into("<hh", result, 25 + index * 4, *point)
    return bytes(result)


def _type5_points(points: list[tuple[int, int]]) -> bytes:
    result = bytearray(42 + 4 * len(points))
    struct.pack_into("<HH", result, 0, 5, len(result))
    struct.pack_into("<H", result, 40, len(points))
    for index, point in enumerate(points):
        struct.pack_into("<hh", result, 42 + index * 4, *point)
    return bytes(result)


def _container(child: bytes, *, byte_length: int = 18) -> bytes:
    return struct.pack("<HH", 14, byte_length) + bytes(byte_length - 4) + child


def _stream(
    entries: list[bytes],
    *,
    magic: bytes = b"SM\x02\x01",
    fields: tuple[int, int] = (7, 9),
    bounds: tuple[int, int, int, int] = (-1, -2, 30, 40),
    direct_count: int | None = None,
) -> bytes:
    body = b"".join(entries)
    return struct.pack(
        "<4sHHIhhhhH",
        magic,
        *fields,
        len(entries) if direct_count is None else direct_count,
        *bounds,
        22 + len(body),
    ) + body


def _nested_stream(levels: int) -> bytes:
    result = _stream([])
    for _index in range(levels):
        result = _stream([_container(result)])
    return result


def _packed_1_bit_row(values: list[int], stride: int) -> bytes:
    result = bytearray(stride)
    for x, value in enumerate(values):
        result[x // 8] |= (value & 1) << (7 - x % 8)
    return bytes(result)


def _ss(
    rows: list[list[int]],
    *,
    bits: int,
    planes: int,
    opaque: tuple[int, int, int, int] = (17, 23, 42, 99),
) -> bytes:
    height = len(rows)
    width = len(rows[0])
    assert all(len(row) == width for row in rows)
    stride = (((width * bits + 7) // 8) + 1) & ~1
    payload = bytearray()
    for row in rows:
        if bits == 8 and planes == 1:
            payload.extend(bytes(row) + bytes(stride - width))
        elif bits == 1:
            for plane in range(planes):
                payload.extend(
                    _packed_1_bit_row([(value >> plane) & 1 for value in row], stride)
                )
        else:
            payload.extend(bytes(stride * planes))
    return (
        struct.pack("<2sHHHBB4H", b"SS", width, height, stride, bits, planes, *opaque)
        + payload
    )


def _drawing(preview: SdwPreview | None) -> SdwDrawing:
    return SdwDrawing(
        asset_id="synthetic",
        declared_offset=0,
        declared_length=0,
        preview=preview,
    )


def _error_code(callable_: object, *args: object, **kwargs: object) -> str:
    with pytest.raises(SdwDecodeError) as raised:
        callable_(*args, **kwargs)  # type: ignore[operator]
    return raised.value.code


def test_validate_sdw_returns_flat_recursive_summaries_and_trailing_count() -> None:
    first = _type4_points([(-1, 2), (30, 40)])
    child = _stream([_type5_points([(1, 2), (3, 4), (5, 6)])], fields=(11, 12))
    marker = _container(child)
    last = _record(99, b"x")
    declared = _stream([first, marker, last])

    result = validate_sdw(declared + b"tail", limits=ParseLimits())

    assert result.signature_family == "common-sm-family"
    assert (result.header_field_1, result.header_field_2) == (7, 9)
    assert result.direct_record_count == 3
    assert result.bounds == (-1, -2, 30, 40)
    assert result.declared_stream_length == len(declared)
    assert result.trailing_bytes == 4
    assert [record.record_type for record in result.records] == [4, 14, 5, 99]
    assert [record.byte_length for record in result.records] == [33, 18, 54, 5]
    assert [record.depth for record in result.records] == [0, 0, 1, 0]
    assert [record.point_count for record in result.records] == [2, None, 3, None]
    # Nested offsets, like root offsets, are absolute from byte zero of the
    # preserved primary rather than relative to their containing stream.
    assert [record.offset for record in result.records] == [22, 55, 95, 149]


def test_validate_sdw_accepts_the_common_sm_family_without_guessing_byte_two() -> None:
    result = validate_sdw(_stream([], magic=b"SM\x7f\x01"), limits=ParseLimits())
    assert result.signature_family == "common-sm-family"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (bytearray(_stream([])), "invalid-input"),
        (b"SM\x02\x01", "truncated-header"),
        (_stream([], magic=b"XX\x02\x01"), "invalid-signature"),
        (_stream([], magic=b"SM\x02\x02"), "invalid-signature"),
    ],
)
def test_validate_sdw_rejects_bad_inputs(data: object, code: str) -> None:
    assert _error_code(validate_sdw, data, limits=ParseLimits()) == code


def test_synthetic_sdw_and_companion_truncations_and_single_bit_mutations_are_controlled() -> None:
    primary = _stream(
        [
            _type4_points([(-32768, 32767), (17, -23)]),
            _container(_stream([_type5_points([(42, -99)])])),
        ]
    )
    companion = _ss([[0, 1, 15], [15, 2, 7]], bits=1, planes=4)
    attempts = 0
    rejections = 0
    successes = 0

    for decoder, source in (
        (validate_sdw, primary),
        (decode_sdw_preview, companion),
    ):
        candidates = [source[:length] for length in range(len(source))]
        for offset in range(len(source)):
            for bit in range(8):
                mutated = bytearray(source)
                mutated[offset] ^= 1 << bit
                candidates.append(bytes(mutated))

        for candidate in candidates:
            attempts += 1
            try:
                decoder(candidate, limits=ParseLimits())
            except SdwDecodeError as exc:
                assert exc.code
                rejections += 1
            else:
                successes += 1

    expected_attempts = 9 * (len(primary) + len(companion))
    assert attempts == expected_attempts
    assert rejections > 0
    assert successes > 0


def test_validate_sdw_rejects_invalid_or_truncated_declared_lengths() -> None:
    too_small = bytearray(_stream([]))
    struct.pack_into("<H", too_small, 20, 21)
    assert _error_code(validate_sdw, bytes(too_small), limits=ParseLimits()) == (
        "invalid-stream-size"
    )

    too_large = bytearray(_stream([]))
    struct.pack_into("<H", too_large, 20, 23)
    assert _error_code(validate_sdw, bytes(too_large), limits=ParseLimits()) == (
        "truncated-stream"
    )


def test_validate_sdw_rejects_unordered_root_bounds() -> None:
    data = _stream([], bounds=(10, 0, 9, 1))
    assert _error_code(validate_sdw, data, limits=ParseLimits()) == "invalid-bounds"


def test_validate_sdw_rejects_unordered_nested_bounds() -> None:
    child = _stream([], bounds=(0, 2, 1, 1))
    data = _stream([_container(child)])
    assert _error_code(validate_sdw, data, limits=ParseLimits()) == "invalid-bounds"


@pytest.mark.parametrize("direct_count", [0, 2])
def test_validate_sdw_requires_direct_record_count_to_consume_envelope(
    direct_count: int,
) -> None:
    entries = [_record()]
    if direct_count == 2:
        entries = []
    data = _stream(entries, direct_count=direct_count)
    assert _error_code(validate_sdw, data, limits=ParseLimits()) == (
        "record-count-mismatch"
    )


def test_validate_sdw_rejects_bad_record_envelopes() -> None:
    short_size = _stream([struct.pack("<HH", 99, 3)])
    assert _error_code(validate_sdw, short_size, limits=ParseLimits()) == (
        "invalid-record-size"
    )

    extends = _stream([struct.pack("<HH", 99, 5)])
    assert _error_code(validate_sdw, extends, limits=ParseLimits()) == "truncated-record"

    bad_container = _stream([_container(_stream([]), byte_length=17)])
    assert _error_code(validate_sdw, bad_container, limits=ParseLimits()) == (
        "invalid-container-size"
    )

    missing_child = _stream([struct.pack("<HH", 14, 18) + bytes(14)])
    assert _error_code(validate_sdw, missing_child, limits=ParseLimits()) == (
        "truncated-header"
    )


@pytest.mark.parametrize(
    "record",
    [
        struct.pack("<HH", 4, 29) + bytes(19) + b"\x02" + bytes(5),
        struct.pack("<HH", 5, 46) + bytes(36) + struct.pack("<H", 2) + bytes(4),
    ],
)
def test_validate_sdw_rejects_point_count_length_mismatches(record: bytes) -> None:
    assert len(record) == struct.unpack_from("<H", record, 2)[0]
    assert _error_code(validate_sdw, _stream([record]), limits=ParseLimits()) == (
        "invalid-point-record"
    )


def test_validate_sdw_enforces_caller_lowered_record_and_point_caps() -> None:
    record_limits = replace(ParseLimits(), max_sdw_records=1)
    assert _error_code(
        validate_sdw, _stream([_record(), _record()]), limits=record_limits
    ) == "record-limit"

    point_limits = replace(ParseLimits(), max_sdw_points=2)
    assert _error_code(
        validate_sdw,
        _stream([_type5_points([(0, 0), (1, 1), (2, 2)])]),
        limits=point_limits,
    ) == "point-limit"


def test_validate_sdw_raised_limits_cannot_bypass_hard_record_or_depth_caps() -> None:
    raised = replace(
        ParseLimits(),
        max_sdw_records=100_000,
        max_sdw_depth=1_000,
    )
    assert _error_code(
        validate_sdw, _stream([_record()] * 10_001), limits=raised
    ) == "record-limit"
    assert _error_code(validate_sdw, _nested_stream(33), limits=raised) == "depth-limit"


def test_validate_sdw_enforces_caller_lowered_depth_and_hard_asset_caps() -> None:
    shallow = replace(ParseLimits(), max_sdw_depth=0)
    assert _error_code(validate_sdw, _nested_stream(1), limits=shallow) == "depth-limit"

    raised = replace(
        ParseLimits(),
        max_file_bytes=_HARD_ASSET_BYTES * 2,
        max_embedded_asset_bytes=_HARD_ASSET_BYTES * 2,
    )
    oversized = _stream([]) + bytes(_HARD_ASSET_BYTES - 21)
    assert len(oversized) == _HARD_ASSET_BYTES + 1
    assert _error_code(validate_sdw, oversized, limits=raised) == "asset-limit"


def test_validate_sdw_rejects_invalid_limit_types_with_a_controlled_error() -> None:
    limits = replace(ParseLimits(), max_sdw_points=-1)
    assert _error_code(validate_sdw, _stream([]), limits=limits) == "invalid-limits"


def test_decode_four_plane_preview_is_top_down_msb_first_and_row_interleaved() -> None:
    rows = [[0, 1, 2, 3], [15, 8, 4, 0]]
    source = _ss(rows, bits=1, planes=4)
    reservations: list[int] = []

    preview = decode_sdw_preview(
        source, limits=ParseLimits(), reserve_pixels=reservations.append
    )

    assert reservations == [8]
    assert (preview.width_px, preview.height_px) == (4, 2)
    assert (preview.bits_per_plane, preview.plane_count, preview.stride) == (1, 4, 2)
    assert preview.opaque_header == (17, 23, 42, 99)
    assert preview.source_sha256 == hashlib.sha256(source).hexdigest()
    expected_gray = [0, 17, 34, 51, 255, 136, 68, 0]
    assert preview.rgb_data == bytes(
        channel for gray in expected_gray for channel in (gray, gray, gray)
    )


@pytest.mark.parametrize(
    ("bits", "rows", "expected"),
    [
        (1, [[0, 1, 1]], [0, 255, 255]),
        (8, [[0, 127, 255]], [0, 127, 255]),
    ],
)
def test_decode_supported_single_plane_previews(
    bits: int, rows: list[list[int]], expected: list[int]
) -> None:
    preview = decode_sdw_preview(_ss(rows, bits=bits, planes=1), limits=ParseLimits())
    assert preview.rgb_data == bytes(
        channel for gray in expected for channel in (gray, gray, gray)
    )


@pytest.mark.parametrize("bits", [16, 24])
def test_decode_structurally_valid_high_color_companions_remains_unsupported(
    bits: int,
) -> None:
    source = _ss([[0, 0]], bits=bits, planes=1)
    reservations: list[int] = []
    assert _error_code(
        decode_sdw_preview,
        source,
        limits=ParseLimits(),
        reserve_pixels=reservations.append,
    ) == "unsupported-format"
    assert reservations == []


def test_decode_preview_validates_everything_before_reserving_pixels() -> None:
    source = _ss([[0, 1]], bits=1, planes=1)
    reservations: list[int] = []
    assert _error_code(
        decode_sdw_preview,
        source + b"x",
        limits=ParseLimits(),
        reserve_pixels=reservations.append,
    ) == "size-mismatch"
    assert reservations == []


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (bytearray(18), "invalid-input"),
        (b"SS", "truncated-header"),
        (
            struct.pack("<2sHHHBB4H", b"XX", 1, 1, 2, 1, 1, 0, 0, 0, 0)
            + b"\0\0",
            "invalid-signature",
        ),
        (struct.pack("<2sHHHBB4H", b"SS", 0, 1, 0, 1, 1, 0, 0, 0, 0), "invalid-dimensions"),
        (struct.pack("<2sHHHBB4H", b"SS", 1, 1, 0, 0, 1, 0, 0, 0, 0), "invalid-format"),
    ],
)
def test_decode_preview_rejects_bad_headers(data: object, code: str) -> None:
    assert _error_code(decode_sdw_preview, data, limits=ParseLimits()) == code


def test_decode_preview_rejects_wrong_stride_and_storage_length() -> None:
    source = bytearray(_ss([[0, 1]], bits=1, planes=1))
    struct.pack_into("<H", source, 6, 1)
    assert _error_code(decode_sdw_preview, bytes(source), limits=ParseLimits()) == (
        "stride-mismatch"
    )

    source = _ss([[0, 1]], bits=1, planes=1)
    assert _error_code(decode_sdw_preview, source[:-1], limits=ParseLimits()) == (
        "size-mismatch"
    )
    assert _error_code(decode_sdw_preview, source + b"x", limits=ParseLimits()) == (
        "size-mismatch"
    )


def test_decode_preview_enforces_lowered_and_hard_dimension_and_pixel_caps() -> None:
    lower_dimension = replace(ParseLimits(), max_sdw_dimension=1)
    assert _error_code(
        decode_sdw_preview, _ss([[0, 1]], bits=1, planes=1), limits=lower_dimension
    ) == "dimension-limit"

    lower_pixels = replace(ParseLimits(), max_sdw_pixels=3)
    assert _error_code(
        decode_sdw_preview,
        _ss([[0, 1], [1, 0]], bits=1, planes=1),
        limits=lower_pixels,
    ) == "pixel-limit"

    raised = replace(
        ParseLimits(),
        max_sdw_dimension=100_000,
        max_sdw_pixels=100_000_000,
    )
    over_dimension = struct.pack(
        "<2sHHHBB4H", b"SS", 4097, 1, 514, 1, 1, 0, 0, 0, 0
    )
    assert _error_code(decode_sdw_preview, over_dimension, limits=raised) == (
        "dimension-limit"
    )
    over_pixels = struct.pack(
        "<2sHHHBB4H", b"SS", 2001, 2000, 252, 1, 1, 0, 0, 0, 0
    )
    assert _error_code(decode_sdw_preview, over_pixels, limits=raised) == "pixel-limit"


def test_decode_preview_raised_byte_limits_cannot_bypass_hard_cap() -> None:
    limits = replace(
        ParseLimits(),
        max_file_bytes=_HARD_ASSET_BYTES * 2,
        max_embedded_asset_bytes=_HARD_ASSET_BYTES * 2,
    )
    oversized = b"SS" + bytes(_HARD_ASSET_BYTES - 1)
    assert len(oversized) == _HARD_ASSET_BYTES + 1
    assert _error_code(decode_sdw_preview, oversized, limits=limits) == "asset-limit"


def test_preview_output_is_immutable_and_independent_of_a_mutable_source() -> None:
    mutable = bytearray(_ss([[0, 1]], bits=1, planes=1))
    immutable = bytes(mutable)
    preview = decode_sdw_preview(immutable, limits=ParseLimits())
    mutable[-2:] = b"\xff\xff"
    assert isinstance(preview.rgb_data, bytes)
    assert preview.rgb_data == bytes((0, 0, 0, 255, 255, 255))


def test_sdw_png_is_fresh_deterministic_rgb_and_display_size_is_bounded() -> None:
    preview = decode_sdw_preview(
        _ss([[0, 127, 255]], bits=8, planes=1), limits=ParseLimits()
    )
    drawing = _drawing(preview)

    first = sdw_png(drawing)
    second = sdw_png(drawing)

    assert first == second
    assert first is not second
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack_from(">II", first, 16) == (3, 1)
    assert sdw_display_size(drawing) == (3 / 96.0, 1 / 96.0)
    assert sdw_display_size(drawing, max_width_in=1 / 96.0) == pytest.approx(
        (1 / 96.0, 1 / 288.0)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width_px", True),
        ("height_px", 4097),
        ("rgb_data", bytearray(6)),
        ("rgb_data", b""),
        ("bits_per_plane", 16),
        ("plane_count", 2),
        ("stride", 3),
        ("opaque_header", [0, 0, 0, 0]),
        ("opaque_header", (0, 0, 0, 65536)),
        ("source_sha256", "not-a-digest"),
    ],
)
def test_renderers_reject_hostile_manually_mutated_preview_ir(
    field: str, value: object
) -> None:
    preview = decode_sdw_preview(_ss([[0, 1]], bits=1, planes=1), limits=ParseLimits())
    setattr(preview, field, value)
    drawing = _drawing(preview)
    assert _error_code(sdw_png, drawing) == "invalid-ir"
    assert _error_code(sdw_display_size, drawing) == "invalid-ir"


def test_renderers_reject_wrong_or_missing_drawing_ir() -> None:
    assert _error_code(sdw_png, object()) == "invalid-ir"
    assert _error_code(sdw_png, _drawing(None)) == "preview-unavailable"
