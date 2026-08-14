"""Bounded decoding for the evidenced Ami Draw ``SM``/``SS`` subset.

The vector stream parser deliberately assigns no drawing semantics to record
types.  It validates and summarizes the observed binary envelopes so the
original bytes can be preserved as inert data.  Only the independently
validated, palette-free companion formats are expanded to renderer-facing RGB.
"""

from __future__ import annotations

import hashlib
import math
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass

from .limits import ParseLimits
from .model import SdwDrawing, SdwPreview, SdwRecordSummary

__all__ = [
    "SdwDecodeError",
    "SdwValidation",
    "decode_sdw_preview",
    "sdw_asset_limit",
    "sdw_display_size",
    "sdw_png",
    "sdw_preview_caption",
    "validate_sdw",
]


_STREAM_HEADER_BYTES = 22
_PREVIEW_HEADER_BYTES = 18

_TYPE_POINT_COUNT_8 = 4
_TYPE_POINT_COUNT_16 = 5
_TYPE_CONTAINER = 14

_SAFE_ASSET_BYTES = 16 * 1024 * 1024
_SAFE_RECORDS = 10_000
_SAFE_DEPTH = 32
_SAFE_POINTS = 1_000_000
_SAFE_DIMENSION = 4_096
_SAFE_PIXELS = 4_000_000
_SAFE_PNG_BYTES = 16 * 1024 * 1024

_RENDERED_PREVIEW_FORMATS = frozenset({(1, 1), (1, 4), (8, 1)})
_PRESERVED_PREVIEW_FORMATS = _RENDERED_PREVIEW_FORMATS | {(16, 1), (24, 1)}


class SdwDecodeError(ValueError):
    """A controlled SDW rejection carrying a stable diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class SdwValidation:
    """Evidence-backed metadata for one completely validated root stream."""

    signature_family: str
    header_field_1: int
    header_field_2: int
    direct_record_count: int
    bounds: tuple[int, int, int, int]
    declared_stream_length: int
    records: list[SdwRecordSummary]
    trailing_bytes: int


@dataclass(slots=True)
class _StreamHeader:
    signature_family: str
    field_1: int
    field_2: int
    direct_record_count: int
    bounds: tuple[int, int, int, int]
    declared_length: int


@dataclass(slots=True)
class _ValidationState:
    data: bytes
    record_limit: int
    depth_limit: int
    point_limit: int
    records: list[SdwRecordSummary]
    record_count: int = 0
    point_count: int = 0


def validate_sdw(data: bytes, limits: ParseLimits) -> SdwValidation:
    """Validate and summarize the common binary ``SM ?? 01`` stream family.

    A root stream may have trailing preserved bytes.  Every byte inside its
    declared envelope, including recursively nested streams following type-14
    markers, must be accounted for exactly.
    """

    if not isinstance(data, bytes):
        raise SdwDecodeError("invalid-input", "SDW input must be immutable bytes")
    asset_limit = sdw_asset_limit(limits)
    record_limit = _effective_limit(limits, "max_sdw_records", _SAFE_RECORDS, "record")
    depth_limit = _effective_limit(limits, "max_sdw_depth", _SAFE_DEPTH, "depth")
    point_limit = _effective_limit(limits, "max_sdw_points", _SAFE_POINTS, "point")
    if len(data) > asset_limit:
        raise SdwDecodeError(
            "asset-limit", f"SDW exceeds the effective {asset_limit}-byte limit"
        )
    if len(data) < _STREAM_HEADER_BYTES:
        raise SdwDecodeError("truncated-header", "SDW stream header is truncated")

    state = _ValidationState(
        data=data,
        record_limit=record_limit,
        depth_limit=depth_limit,
        point_limit=point_limit,
        records=[],
    )
    root_end, header = _parse_stream(state, 0, len(data), 0)
    return SdwValidation(
        signature_family=header.signature_family,
        header_field_1=header.field_1,
        header_field_2=header.field_2,
        direct_record_count=header.direct_record_count,
        bounds=header.bounds,
        declared_stream_length=header.declared_length,
        records=state.records,
        trailing_bytes=len(data) - root_end,
    )


def _parse_stream(
    state: _ValidationState, offset: int, enclosing_end: int, depth: int
) -> tuple[int, _StreamHeader]:
    if depth > state.depth_limit:
        raise SdwDecodeError(
            "depth-limit", f"SDW nesting exceeds depth {state.depth_limit}"
        )
    if enclosing_end - offset < _STREAM_HEADER_BYTES:
        raise SdwDecodeError("truncated-header", "nested SDW stream header is truncated")

    data = state.data
    magic, field_1, field_2, direct_count, left, top, right, bottom, declared = (
        struct.unpack_from("<4sHHIhhhhH", data, offset)
    )
    if magic[:2] != b"SM" or magic[3] != 1:
        raise SdwDecodeError(
            "invalid-signature", "SDW stream does not use the SM ?? 01 signature family"
        )
    if left > right or top > bottom:
        raise SdwDecodeError(
            "invalid-bounds", "SDW stream bounds are not ordered"
        )
    if declared < _STREAM_HEADER_BYTES:
        raise SdwDecodeError(
            "invalid-stream-size", "SDW declared stream length is smaller than its header"
        )
    if declared > enclosing_end - offset:
        raise SdwDecodeError(
            "truncated-stream", "SDW declared stream extends beyond its enclosing envelope"
        )
    if direct_count > state.record_limit - state.record_count:
        raise SdwDecodeError(
            "record-limit", f"SDW exceeds the effective {state.record_limit}-record limit"
        )

    end = offset + declared
    cursor = offset + _STREAM_HEADER_BYTES
    for _index in range(direct_count):
        if end - cursor < 4:
            raise SdwDecodeError(
                "record-count-mismatch",
                "SDW direct record count exceeds its declared stream contents",
            )
        record_type, byte_length = struct.unpack_from("<HH", data, cursor)
        if byte_length < 4:
            raise SdwDecodeError(
                "invalid-record-size",
                f"SDW record type {record_type} has a length smaller than its header",
            )
        if byte_length > end - cursor:
            raise SdwDecodeError(
                "truncated-record",
                f"SDW record type {record_type} extends beyond its declared stream",
            )
        if record_type == _TYPE_CONTAINER and byte_length != 18:
            raise SdwDecodeError(
                "invalid-container-size", "SDW type-14 container marker is not 18 bytes"
            )
        if state.record_count >= state.record_limit:
            raise SdwDecodeError(
                "record-limit", f"SDW exceeds the effective {state.record_limit}-record limit"
            )

        point_count = _point_count(data, cursor, record_type, byte_length)
        if point_count is not None:
            if point_count > state.point_limit - state.point_count:
                raise SdwDecodeError(
                    "point-limit", f"SDW exceeds the effective {state.point_limit}-point limit"
                )
            state.point_count += point_count

        state.records.append(
            SdwRecordSummary(
                record_type=record_type,
                byte_length=byte_length,
                depth=depth,
                # This is deliberately absolute within the preserved primary,
                # including for records reached through nested streams.
                offset=cursor,
                point_count=point_count,
            )
        )
        state.record_count += 1
        cursor += byte_length

        if record_type == _TYPE_CONTAINER:
            cursor, _child_header = _parse_stream(state, cursor, end, depth + 1)

    if cursor != end:
        raise SdwDecodeError(
            "record-count-mismatch",
            "SDW direct record count does not consume its declared stream",
        )

    return end, _StreamHeader(
        signature_family="common-sm-family",
        field_1=field_1,
        field_2=field_2,
        direct_record_count=direct_count,
        bounds=(left, top, right, bottom),
        declared_length=declared,
    )


def _point_count(
    data: bytes, offset: int, record_type: int, byte_length: int
) -> int | None:
    if record_type == _TYPE_POINT_COUNT_8:
        if byte_length < 24:
            raise SdwDecodeError(
                "invalid-point-record", "SDW type-4 record is too short for its point count"
            )
        count = data[offset + 23]
        expected = 25 + 4 * count
    elif record_type == _TYPE_POINT_COUNT_16:
        if byte_length < 42:
            raise SdwDecodeError(
                "invalid-point-record", "SDW type-5 record is too short for its point count"
            )
        count = struct.unpack_from("<H", data, offset + 40)[0]
        expected = 42 + 4 * count
    else:
        return None
    if byte_length != expected:
        raise SdwDecodeError(
            "invalid-point-record",
            f"SDW type-{record_type} record length does not match its point count",
        )
    return count


def decode_sdw_preview(
    data: bytes,
    limits: ParseLimits,
    reserve_pixels: Callable[[int], None] | None = None,
) -> SdwPreview:
    """Validate an ``SS`` companion and expand the evidenced indexed subset."""

    if not isinstance(data, bytes):
        raise SdwDecodeError("invalid-input", "SDW companion input must be immutable bytes")
    if reserve_pixels is not None and not callable(reserve_pixels):
        raise SdwDecodeError(
            "invalid-reservation", "SDW pixel reservation hook must be callable"
        )
    asset_limit = sdw_asset_limit(limits)
    dimension_limit = _effective_limit(
        limits, "max_sdw_dimension", _SAFE_DIMENSION, "dimension"
    )
    pixel_limit = _effective_limit(limits, "max_sdw_pixels", _SAFE_PIXELS, "pixel")
    if len(data) > asset_limit:
        raise SdwDecodeError(
            "asset-limit", f"SDW companion exceeds the effective {asset_limit}-byte limit"
        )
    if len(data) < _PREVIEW_HEADER_BYTES:
        raise SdwDecodeError("truncated-header", "SDW companion header is truncated")

    magic, width, height, stride, bits, planes, opaque_1, opaque_2, opaque_3, opaque_4 = (
        struct.unpack_from("<2sHHHBB4H", data)
    )
    if magic != b"SS":
        raise SdwDecodeError("invalid-signature", "SDW companion does not begin with SS")
    if width == 0 or height == 0:
        raise SdwDecodeError(
            "invalid-dimensions", "SDW companion dimensions must be positive"
        )
    if width > dimension_limit or height > dimension_limit:
        raise SdwDecodeError(
            "dimension-limit",
            f"SDW companion exceeds the effective {dimension_limit}-pixel dimension limit",
        )
    pixel_count = width * height
    if pixel_count > pixel_limit:
        raise SdwDecodeError(
            "pixel-limit", f"SDW companion exceeds the effective {pixel_limit}-pixel limit"
        )
    if bits == 0 or planes == 0:
        raise SdwDecodeError(
            "invalid-format", "SDW companion bit and plane counts must be positive"
        )

    packed_row_bytes = (width * bits + 7) // 8
    expected_stride = (packed_row_bytes + 1) & ~1
    if stride != expected_stride:
        raise SdwDecodeError(
            "stride-mismatch",
            "SDW companion stride is not its exact word-aligned packed width",
        )
    expected_length = _PREVIEW_HEADER_BYTES + stride * height * planes
    if expected_length != len(data):
        raise SdwDecodeError(
            "size-mismatch",
            "SDW companion storage does not match its dimensions and planes",
        )

    preview_format = (bits, planes)
    if preview_format not in _PRESERVED_PREVIEW_FORMATS:
        raise SdwDecodeError(
            "unsupported-format",
            f"unsupported SDW companion format {bits}-bit x {planes} plane(s)",
        )
    if preview_format not in _RENDERED_PREVIEW_FORMATS:
        raise SdwDecodeError(
            "unsupported-format",
            f"SDW companion format {bits}-bit x {planes} plane(s) is preserved but not rendered",
        )

    # The reservation hook is deliberately the first action after complete
    # structural and format validation that can authorize pixel materialization.
    if reserve_pixels is not None:
        reserve_pixels(pixel_count)
    rgb = _materialize_preview(data, width, height, stride, bits, planes)
    return SdwPreview(
        width_px=width,
        height_px=height,
        rgb_data=rgb,
        source_sha256=hashlib.sha256(data).hexdigest(),
        bits_per_plane=bits,
        plane_count=planes,
        stride=stride,
        opaque_header=(opaque_1, opaque_2, opaque_3, opaque_4),
    )


def _materialize_preview(
    data: bytes, width: int, height: int, stride: int, bits: int, planes: int
) -> bytes:
    rgb = bytearray(width * height * 3)
    output = 0
    for y in range(height):
        row_start = _PREVIEW_HEADER_BYTES + y * stride * planes
        for x in range(width):
            if bits == 8:
                index = data[row_start + x]
                maximum = 255
            else:
                index = 0
                bit_shift = 7 - x % 8
                byte_index = x // 8
                for plane in range(planes):
                    packed = data[row_start + plane * stride + byte_index]
                    index |= ((packed >> bit_shift) & 1) << plane
                maximum = (1 << planes) - 1
            gray = (index * 255 + maximum // 2) // maximum
            rgb[output] = gray
            rgb[output + 1] = gray
            rgb[output + 2] = gray
            output += 3
    return bytes(rgb)


def sdw_png(drawing: SdwDrawing) -> bytes:
    """Return a fresh deterministic PNG after revalidating renderer-facing IR."""

    width, height, rgb = _validated_preview(drawing)
    row_bytes = width * 3
    scanlines = bytearray((row_bytes + 1) * height)
    source_offset = 0
    target_offset = 0
    for _row in range(height):
        scanlines[target_offset] = 0
        target_offset += 1
        scanlines[target_offset : target_offset + row_bytes] = rgb[
            source_offset : source_offset + row_bytes
        ]
        source_offset += row_bytes
        target_offset += row_bytes
    compressed = zlib.compress(bytes(scanlines), level=9)
    result = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk("IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk("IDAT", compressed)
        + _png_chunk("IEND", b"")
    )
    if len(result) > _SAFE_PNG_BYTES:
        raise SdwDecodeError(
            "generated-output-limit", "generated SDW companion PNG is too large"
        )
    return result


def sdw_display_size(
    drawing: SdwDrawing, *, max_width_in: float = 6.5, max_height_in: float = 8.0
) -> tuple[float, float]:
    """Return bounded 96-pixel-per-inch display dimensions for a companion."""

    width, height, _rgb = _validated_preview(drawing)
    physical_width = width / 96.0
    physical_height = height / 96.0
    maximum_width = _bounded_maximum(max_width_in, 6.5)
    maximum_height = _bounded_maximum(max_height_in, 8.0)
    scale = min(1.0, maximum_width / physical_width, maximum_height / physical_height)
    return physical_width * scale, physical_height * scale


def sdw_preview_caption(drawing: SdwDrawing) -> str:
    """Describe a companion preview without hiding the vector preservation state."""

    if not isinstance(drawing, SdwDrawing):
        return "Ami Draw companion preview — vector status=unavailable"
    status = _safe_text(drawing.status, "unavailable", maximum=64)
    parts = [
        "Ami Draw companion preview — grayscale/index rendering",
        f"vector status={status}",
        "vector semantics not rendered",
    ]
    if status != "validated":
        reason = _safe_text(drawing.reason, "validation unavailable", maximum=256)
        parts.append(f"reason={reason}")
    return "; ".join(parts)


def _validated_preview(drawing: SdwDrawing) -> tuple[int, int, bytes]:
    if not isinstance(drawing, SdwDrawing):
        raise SdwDecodeError("invalid-ir", "SDW renderer input has the wrong type")
    preview = drawing.preview
    if preview is None:
        raise SdwDecodeError("preview-unavailable", "SDW drawing has no decoded companion")
    if not isinstance(preview, SdwPreview):
        raise SdwDecodeError("invalid-ir", "SDW drawing preview has the wrong type")

    width = preview.width_px
    height = preview.height_px
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
        or width > _SAFE_DIMENSION
        or height > _SAFE_DIMENSION
        or width * height > _SAFE_PIXELS
    ):
        raise SdwDecodeError("invalid-ir", "SDW renderer dimensions are unsafe")
    if not isinstance(preview.rgb_data, bytes) or len(preview.rgb_data) != width * height * 3:
        raise SdwDecodeError(
            "invalid-ir", "SDW renderer pixel storage is inconsistent"
        )

    bits = preview.bits_per_plane
    planes = preview.plane_count
    if (
        isinstance(bits, bool)
        or isinstance(planes, bool)
        or not isinstance(bits, int)
        or not isinstance(planes, int)
        or (bits, planes) not in _RENDERED_PREVIEW_FORMATS
    ):
        raise SdwDecodeError("invalid-ir", "SDW renderer preview format is unsupported")
    stride = preview.stride
    expected_stride = (((width * bits + 7) // 8) + 1) & ~1
    if isinstance(stride, bool) or not isinstance(stride, int) or stride != expected_stride:
        raise SdwDecodeError("invalid-ir", "SDW renderer stride is inconsistent")

    opaque = preview.opaque_header
    if not isinstance(opaque, tuple) or len(opaque) != 4 or any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > 0xFFFF
        for value in opaque
    ):
        raise SdwDecodeError("invalid-ir", "SDW renderer opaque header is inconsistent")
    source_hash = preview.source_sha256
    if (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or any(character not in "0123456789abcdef" for character in source_hash)
    ):
        raise SdwDecodeError("invalid-ir", "SDW renderer source digest is inconsistent")
    return width, height, preview.rgb_data


def sdw_asset_limit(limits: ParseLimits) -> int:
    """Return the effective per-payload ceiling; caller values can only lower it."""

    embedded = _effective_limit(
        limits, "max_embedded_asset_bytes", _SAFE_ASSET_BYTES, "asset byte"
    )
    file_bytes = _effective_limit(limits, "max_file_bytes", _SAFE_ASSET_BYTES, "file byte")
    return min(embedded, file_bytes)


def _safe_text(value: object, default: str, *, maximum: int) -> str:
    if isinstance(value, str):
        result = value
    elif isinstance(value, bytes):
        result = value.decode("utf-8", errors="replace")
    elif isinstance(value, bool | int | float):
        try:
            result = str(value)
        except (TypeError, ValueError, OverflowError):
            return default
    else:
        return default
    result = " ".join(result.split())[:maximum]
    return result or default


def _effective_limit(
    limits: ParseLimits, attribute: str, hard_limit: int, description: str
) -> int:
    try:
        value = getattr(limits, attribute)
    except (AttributeError, TypeError) as error:
        raise SdwDecodeError(
            "invalid-limits", f"SDW {description} limit is unavailable"
        ) from error
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SdwDecodeError(
            "invalid-limits", f"SDW {description} limit must be a nonnegative integer"
        )
    return min(value, hard_limit)


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0 or number > 100:
        return None
    return number


def _bounded_maximum(value: object, default: float) -> float:
    number = _positive_number(value)
    return number if number is not None else default


def _png_chunk(kind: str, data: bytes) -> bytes:
    encoded_kind = kind.encode("ascii")
    return (
        struct.pack(">I", len(data))
        + encoded_kind
        + data
        + struct.pack(">I", zlib.crc32(encoded_kind + data) & 0xFFFFFFFF)
    )
