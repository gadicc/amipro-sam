"""Bounded decoding for the evidenced Ami Pro WMF preview subset.

The decoder intentionally implements a small, closed grammar.  A WMF either
produces one completely validated RGB preview or is rejected as a whole; raw
records are never passed to a renderer or external converter.
"""

from __future__ import annotations

import hashlib
import math
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass

from .limits import ParseLimits
from .model import SourceSpan, WmfGraphic

__all__ = [
    "WmfDecodeError",
    "decode_wmf",
    "wmf_display_size",
    "wmf_png",
]


_PLACEABLE_KEY = 0x9AC6CDD7
_STANDARD_HEADER_WORDS = 9
_WMF_VERSION_3 = 0x0300

_META_EOF = 0x0000
_META_REALIZEPALETTE = 0x0035
_META_CREATEPALETTE = 0x00F7
_META_SETMAPMODE = 0x0103
_META_SETWINDOWORG = 0x020B
_META_SETWINDOWEXT = 0x020C
_META_SELECTPALETTE = 0x0234
_META_ESCAPE = 0x0626
_META_DIBSTRETCHBLT = 0x0B41

_MM_ANISOTROPIC = 8
_SRCCOPY = 0x00CC0020
_BITMAPINFOHEADER_SIZE = 40
_BI_RGB = 0

_SAFE_DIMENSION = 4_096
_SAFE_PIXELS = 4_000_000
_SAFE_PNG_BYTES = 16 * 1024 * 1024
_SAFE_WMF_BYTES = 16 * 1024 * 1024
_SAFE_RECORDS = 10_000
_SAFE_OBJECTS = 4_096
_SAFE_PALETTE_ENTRIES = 4_096

_OPERATION_NAMES = {
    _META_REALIZEPALETTE: "realize-palette",
    _META_CREATEPALETTE: "create-palette",
    _META_SETMAPMODE: "set-map-mode",
    _META_SETWINDOWORG: "set-window-origin",
    _META_SETWINDOWEXT: "set-window-extent",
    _META_SELECTPALETTE: "select-palette",
    _META_DIBSTRETCHBLT: "dib-stretch-blit",
    _META_EOF: "end-of-file",
}


class WmfDecodeError(ValueError):
    """A controlled rejection carrying a stable diagnostic suffix."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class _Placeable:
    width_in: float
    height_in: float


@dataclass(slots=True)
class _DibDescription:
    width: int
    height: int
    bits_per_pixel: int
    palette: tuple[tuple[int, int, int], ...]
    pixel_data: bytes
    row_stride: int
    source_width: int
    source_height: int
    destination_x: int
    destination_y: int
    destination_width: int
    destination_height: int


def decode_wmf(
    data: bytes,
    *,
    limits: ParseLimits,
    source: SourceSpan | None = None,
    alt_text: str = "Embedded WMF preview",
    reserve_pixels: Callable[[int], None] | None = None,
) -> WmfGraphic:
    """Decode the closed, corpus-backed WMF/DIB subset into inert RGB bytes."""

    if not isinstance(data, bytes):
        raise WmfDecodeError("invalid-input", "WMF input must be immutable bytes")
    byte_limit = min(
        _SAFE_WMF_BYTES,
        _positive_limit(limits.max_file_bytes, "file byte"),
        _positive_limit(limits.max_embedded_asset_bytes, "embedded asset byte"),
    )
    if len(data) > byte_limit:
        raise WmfDecodeError(
            "asset-limit", f"WMF exceeds the effective {byte_limit}-byte limit"
        )
    if len(data) < 18:
        raise WmfDecodeError("truncated-header", "WMF is shorter than its header")

    header_offset = 0
    placeable: _Placeable | None = None
    if len(data) >= 4 and _u32(data, 0) == _PLACEABLE_KEY:
        placeable = _parse_placeable(data)
        header_offset = 22

    if len(data) - header_offset < 18:
        raise WmfDecodeError("truncated-header", "WMF standard header is truncated")

    (
        metafile_type,
        header_words,
        version,
        declared_words,
        object_slots,
        declared_max_record,
        parameter_count,
    ) = struct.unpack_from("<HHHIHIH", data, header_offset)

    if metafile_type != 1:
        raise WmfDecodeError(
            "unsupported-header", f"unsupported WMF type {metafile_type}"
        )
    if header_words != _STANDARD_HEADER_WORDS:
        raise WmfDecodeError(
            "invalid-header-size", f"WMF header is {header_words} words, not 9"
        )
    if version != _WMF_VERSION_3:
        raise WmfDecodeError(
            "unsupported-version", f"unsupported WMF version 0x{version:04x}"
        )
    if parameter_count != 0:
        raise WmfDecodeError(
            "invalid-header", "WMF header parameter count must be zero"
        )
    if declared_words < _STANDARD_HEADER_WORDS + 3:
        raise WmfDecodeError("invalid-file-size", "WMF declared size is too small")
    if declared_words * 2 != len(data) - header_offset:
        raise WmfDecodeError(
            "file-size-mismatch", "WMF declared size does not match its asset range"
        )
    object_limit = min(
        _SAFE_OBJECTS, _positive_limit(limits.max_wmf_objects, "object")
    )
    if object_slots > object_limit:
        raise WmfDecodeError(
            "object-limit",
            f"WMF object table exceeds {object_limit} slots",
        )
    if declared_max_record < 3:
        raise WmfDecodeError(
            "invalid-max-record", "WMF maximum record size is smaller than EOF"
        )

    objects: list[str | None] = [None] * object_slots
    selected_palette: int | None = None
    operations: list[str] = []
    map_mode: int | None = None
    window_origin: tuple[int, int] | None = None
    window_extent: tuple[int, int] | None = None
    dib: _DibDescription | None = None
    observed_max_record = 0
    record_count = 0
    eof_seen = False
    offset = header_offset + 18
    end = header_offset + declared_words * 2

    while offset < end:
        if end - offset < 6:
            raise WmfDecodeError("truncated-record", "WMF record header is truncated")
        record_words = _u32(data, offset)
        function = _u16(data, offset + 4)
        if record_words < 3:
            raise WmfDecodeError(
                "invalid-record-size",
                f"WMF record 0x{function:04x} has an invalid size",
            )
        record_bytes = record_words * 2
        if record_bytes > end - offset:
            raise WmfDecodeError(
                "truncated-record",
                f"WMF record 0x{function:04x} extends beyond the declared file",
            )

        record_count += 1
        record_limit = min(
            _SAFE_RECORDS, _positive_limit(limits.max_wmf_records, "record")
        )
        if record_count > record_limit:
            raise WmfDecodeError(
                "record-limit", f"WMF exceeds {record_limit} records"
            )
        observed_max_record = max(observed_max_record, record_words)
        payload = data[offset + 6 : offset + record_bytes]

        if function == _META_EOF:
            if record_words != 3 or payload:
                raise WmfDecodeError("invalid-eof", "WMF EOF record is malformed")
            if offset + record_bytes != end:
                raise WmfDecodeError(
                    "early-eof", "WMF contains data or records after EOF"
                )
            operations.append(_OPERATION_NAMES[function])
            eof_seen = True
            offset += record_bytes
            break

        if function == _META_ESCAPE:
            raise WmfDecodeError(
                "unsafe-escape", "WMF escape records are never activated"
            )
        if function not in _OPERATION_NAMES:
            raise WmfDecodeError(
                "unsupported-record",
                f"unsupported WMF record 0x{function:04x}",
            )

        if function == _META_SETMAPMODE:
            _require_payload(payload, 2, function)
            if (
                map_mode is not None
                or window_origin is not None
                or window_extent is not None
                or dib is not None
            ):
                raise WmfDecodeError(
                    "transform-order",
                    "WMF map mode must precede its window transform",
                )
            map_mode = _i16(payload, 0)
            if map_mode != _MM_ANISOTROPIC:
                raise WmfDecodeError(
                    "unsupported-map-mode", f"unsupported WMF map mode {map_mode}"
                )
        elif function == _META_SETWINDOWORG:
            _require_payload(payload, 4, function)
            if window_origin is not None:
                raise WmfDecodeError(
                    "duplicate-transform", "WMF sets its window origin more than once"
                )
            y, x = struct.unpack_from("<hh", payload)
            window_origin = (x, y)
        elif function == _META_SETWINDOWEXT:
            _require_payload(payload, 4, function)
            if window_extent is not None:
                raise WmfDecodeError(
                    "duplicate-transform", "WMF sets its window extent more than once"
                )
            y, x = struct.unpack_from("<hh", payload)
            if x == 0 or y == 0:
                raise WmfDecodeError(
                    "invalid-transform", "WMF window extent must be nonzero"
                )
            window_extent = (x, y)
        elif function == _META_CREATEPALETTE:
            _parse_create_palette(payload, limits)
            try:
                slot = objects.index(None)
            except ValueError as exc:
                raise WmfDecodeError(
                    "object-table-overflow", "WMF object table has no free slot"
                ) from exc
            objects[slot] = "palette"
        elif function == _META_SELECTPALETTE:
            _require_payload(payload, 2, function)
            slot = _u16(payload, 0)
            if slot >= len(objects) or objects[slot] != "palette":
                raise WmfDecodeError(
                    "invalid-object-index",
                    f"WMF selects unavailable palette object {slot}",
                )
            selected_palette = slot
        elif function == _META_REALIZEPALETTE:
            _require_payload(payload, 0, function)
            if selected_palette is None:
                raise WmfDecodeError(
                    "invalid-object-state", "WMF realizes no selected palette"
                )
        elif function == _META_DIBSTRETCHBLT:
            if dib is not None:
                raise WmfDecodeError(
                    "multiple-images", "WMF contains more than one raster operation"
                )
            if window_origin is None or window_extent is None:
                raise WmfDecodeError(
                    "missing-transform",
                    "WMF raster operation precedes its window transform",
                )
            dib = _parse_dib_stretch_blt(payload, limits)

        operations.append(_OPERATION_NAMES[function])
        offset += record_bytes

    if not eof_seen or offset != end:
        raise WmfDecodeError("missing-eof", "WMF has no final EOF record")
    if observed_max_record != declared_max_record:
        raise WmfDecodeError(
            "max-record-mismatch",
            "WMF declared maximum record size does not match its records",
        )
    if dib is None:
        raise WmfDecodeError(
            "missing-image", "WMF contains no supported raster operation"
        )
    if window_origin != (0, 0):
        raise WmfDecodeError(
            "unsupported-transform",
            "WMF uses a nonzero window origin outside the evidenced subset",
        )
    assert window_extent is not None
    if window_extent != (dib.destination_width, dib.destination_height):
        raise WmfDecodeError(
            "transform-mismatch",
            "WMF destination extent does not match its window extent",
        )
    if (dib.destination_width < 0 or dib.destination_height < 0) and (
        map_mode != _MM_ANISOTROPIC
    ):
        raise WmfDecodeError(
            "unsupported-transform",
            "negative WMF destination extents require anisotropic mapping",
        )
    if dib.destination_width < 0:
        raise WmfDecodeError(
            "unsupported-transform",
            "negative WMF destination width is outside the evidenced subset",
        )
    if (abs(window_extent[0]), abs(window_extent[1])) != (dib.width, dib.height):
        raise WmfDecodeError(
            "transform-mismatch", "WMF window extent does not match its bitmap"
        )
    if (dib.destination_x, dib.destination_y) != (0, 0):
        raise WmfDecodeError(
            "unsupported-transform",
            "WMF uses a nonzero raster destination origin",
        )
    if (abs(dib.destination_width), abs(dib.destination_height)) != (
        dib.width,
        dib.height,
    ):
        raise WmfDecodeError(
            "transform-mismatch", "WMF destination extent does not match its bitmap"
        )
    if (dib.source_width, dib.source_height) != (dib.width, dib.height):
        raise WmfDecodeError(
            "source-mismatch", "WMF source extent does not match its bitmap"
        )

    pixel_count = dib.width * dib.height
    if reserve_pixels is not None:
        reserve_pixels(pixel_count)
    rgb = _materialize_rgb(dib)

    return WmfGraphic(
        width_px=dib.width,
        height_px=dib.height,
        rgb_data=rgb,
        source_sha256=hashlib.sha256(data).hexdigest(),
        operations=tuple(operations),
        record_count=record_count,
        placeable=placeable is not None,
        width_in=placeable.width_in if placeable else None,
        height_in=placeable.height_in if placeable else None,
        alt_text=alt_text,
        source=source,
    )


def _parse_placeable(data: bytes) -> _Placeable:
    if len(data) < 40:
        raise WmfDecodeError(
            "truncated-placeable-header", "placeable WMF header is truncated"
        )
    key, handle, left, top, right, bottom, inch, reserved, checksum = (
        struct.unpack_from("<IHhhhhHIH", data)
    )
    if key != _PLACEABLE_KEY:
        raise WmfDecodeError("invalid-placeable-key", "invalid placeable WMF key")
    if handle != 0 or reserved != 0:
        raise WmfDecodeError(
            "invalid-placeable-header", "placeable WMF reserved fields are nonzero"
        )
    expected_checksum = 0
    for value in struct.unpack_from("<10H", data):
        expected_checksum ^= value
    if checksum != expected_checksum:
        raise WmfDecodeError(
            "placeable-checksum", "placeable WMF checksum does not match"
        )
    if inch == 0:
        raise WmfDecodeError(
            "invalid-placeable-units", "placeable WMF units per inch is zero"
        )
    if right <= left or bottom <= top:
        raise WmfDecodeError(
            "invalid-placeable-bounds", "placeable WMF bounds are empty or reversed"
        )
    width_in = (right - left) / inch
    height_in = (bottom - top) / inch
    if (
        not math.isfinite(width_in)
        or not math.isfinite(height_in)
        or width_in <= 0
        or height_in <= 0
        or width_in > 100
        or height_in > 100
    ):
        raise WmfDecodeError(
            "invalid-placeable-bounds", "placeable WMF physical bounds are unsafe"
        )
    return _Placeable(width_in=width_in, height_in=height_in)


def _parse_create_palette(payload: bytes, limits: ParseLimits) -> None:
    if len(payload) < 4:
        raise WmfDecodeError(
            "truncated-palette", "WMF logical palette header is truncated"
        )
    start, count = struct.unpack_from("<HH", payload)
    if start != _WMF_VERSION_3:
        raise WmfDecodeError(
            "unsupported-palette", f"unsupported logical palette start 0x{start:04x}"
        )
    palette_limit = min(
        _SAFE_PALETTE_ENTRIES,
        _positive_limit(limits.max_wmf_palette_entries, "palette entry"),
    )
    if count > palette_limit:
        raise WmfDecodeError(
            "palette-limit",
            f"WMF palette exceeds {palette_limit} entries",
        )
    if len(payload) != 4 + count * 4:
        raise WmfDecodeError(
            "invalid-palette-size", "WMF logical palette size is inconsistent"
        )
    for index in range(count):
        values = payload[4 + index * 4 + 3]
        if values not in {0, 1, 2, 4}:
            raise WmfDecodeError(
                "invalid-palette-flags",
                f"WMF logical palette entry {index} has invalid flags",
            )


def _parse_dib_stretch_blt(payload: bytes, limits: ParseLimits) -> _DibDescription:
    if len(payload) < 20 + _BITMAPINFOHEADER_SIZE:
        raise WmfDecodeError(
            "truncated-dib", "WMF DIBSTRETCHBLT record is truncated"
        )
    (
        raster_operation,
        source_height,
        source_width,
        source_y,
        source_x,
        destination_height,
        destination_width,
        destination_y,
        destination_x,
    ) = struct.unpack_from("<Ihhhhhhhh", payload)
    if raster_operation != _SRCCOPY:
        raise WmfDecodeError(
            "unsupported-raster-operation",
            f"unsupported WMF raster operation 0x{raster_operation:08x}",
        )
    if source_x != 0 or source_y != 0:
        raise WmfDecodeError(
            "unsupported-source-origin", "WMF uses a nonzero bitmap source origin"
        )

    dib = payload[20:]
    (
        header_size,
        width,
        signed_height,
        planes,
        bits_per_pixel,
        compression,
        size_image,
        _x_pixels_per_meter,
        _y_pixels_per_meter,
        colors_used,
        colors_important,
    ) = struct.unpack_from("<IiiHHIIiiII", dib)
    if header_size != _BITMAPINFOHEADER_SIZE:
        raise WmfDecodeError(
            "unsupported-dib-header", f"unsupported DIB header size {header_size}"
        )
    if signed_height < 0:
        raise WmfDecodeError(
            "unsupported-top-down-dib",
            "top-down WMF DIB orientation is outside the evidenced subset",
        )
    height = signed_height
    if width <= 0 or height == 0:
        raise WmfDecodeError("invalid-dib-dimensions", "DIB dimensions are empty")
    dimension_limit = min(
        _SAFE_DIMENSION, _positive_limit(limits.max_wmf_dimension, "dimension")
    )
    if width > dimension_limit or height > dimension_limit:
        raise WmfDecodeError(
            "dimension-limit",
            f"DIB dimensions exceed {dimension_limit} pixels",
        )
    pixel_count = width * height
    pixel_limit = min(
        _SAFE_PIXELS, _positive_limit(limits.max_wmf_pixels, "pixel")
    )
    if pixel_count > pixel_limit:
        raise WmfDecodeError(
            "pixel-limit", f"DIB exceeds {pixel_limit} pixels"
        )
    if planes != 1:
        raise WmfDecodeError("invalid-dib-planes", "DIB must contain one plane")
    if bits_per_pixel not in {1, 4, 8, 24}:
        raise WmfDecodeError(
            "unsupported-dib-depth", f"unsupported DIB depth {bits_per_pixel}"
        )
    if compression != _BI_RGB:
        raise WmfDecodeError(
            "unsupported-dib-compression", f"unsupported DIB compression {compression}"
        )

    if bits_per_pixel == 24:
        if colors_used != 0:
            raise WmfDecodeError(
                "invalid-color-table", "24-bit DIB unexpectedly declares a color table"
            )
        if colors_important != 0:
            raise WmfDecodeError(
                "invalid-color-table",
                "24-bit DIB unexpectedly declares important palette colors",
            )
        color_count = 0
    else:
        maximum_colors = 1 << bits_per_pixel
        color_count = colors_used or maximum_colors
        if color_count > maximum_colors:
            raise WmfDecodeError(
                "invalid-color-table", "DIB color table exceeds its bit depth"
            )
    palette_limit = min(
        _SAFE_PALETTE_ENTRIES,
        _positive_limit(limits.max_wmf_palette_entries, "palette entry"),
    )
    if color_count > palette_limit:
        raise WmfDecodeError(
            "palette-limit", "DIB color table exceeds the configured palette limit"
        )
    if colors_important > color_count and color_count:
        raise WmfDecodeError(
            "invalid-color-table", "DIB important-color count is inconsistent"
        )

    row_stride = ((width * bits_per_pixel + 31) // 32) * 4
    pixel_bytes = row_stride * height
    if size_image not in {0, pixel_bytes}:
        raise WmfDecodeError(
            "image-size-mismatch", "DIB declared image size is inconsistent"
        )
    palette_bytes = color_count * 4
    expected_length = _BITMAPINFOHEADER_SIZE + palette_bytes + pixel_bytes
    if len(dib) != expected_length:
        code = "truncated-dib" if len(dib) < expected_length else "trailing-dib-data"
        raise WmfDecodeError(code, "DIB storage does not match its dimensions")

    palette: list[tuple[int, int, int]] = []
    for index in range(color_count):
        blue, green, red, reserved = struct.unpack_from(
            "<BBBB", dib, _BITMAPINFOHEADER_SIZE + index * 4
        )
        if reserved != 0:
            raise WmfDecodeError(
                "invalid-color-table", "DIB RGBQUAD reserved byte is nonzero"
            )
        palette.append((red, green, blue))
    pixels_offset = _BITMAPINFOHEADER_SIZE + palette_bytes
    result = _DibDescription(
        width=width,
        height=height,
        bits_per_pixel=bits_per_pixel,
        palette=tuple(palette),
        pixel_data=dib[pixels_offset:],
        row_stride=row_stride,
        source_width=source_width,
        source_height=source_height,
        destination_x=destination_x,
        destination_y=destination_y,
        destination_width=destination_width,
        destination_height=destination_height,
    )
    _validate_palette_indices(result)
    return result


def _validate_palette_indices(dib: _DibDescription) -> None:
    """Validate indexed pixels without expanding them or allocating by pixel count."""

    palette_size = len(dib.palette)
    if dib.bits_per_pixel == 24 or palette_size == 1 << dib.bits_per_pixel:
        return
    for row_index in range(dib.height):
        row_start = row_index * dib.row_stride
        if dib.bits_per_pixel == 8:
            row = dib.pixel_data[row_start : row_start + dib.width]
            if row and max(row) >= palette_size:
                raise WmfDecodeError(
                    "invalid-palette-index",
                    "DIB pixel uses an undefined palette entry",
                )
        elif dib.bits_per_pixel == 4:
            full_bytes, trailing_pixel = divmod(dib.width, 2)
            for packed in dib.pixel_data[row_start : row_start + full_bytes]:
                if packed >> 4 >= palette_size or packed & 0x0F >= palette_size:
                    raise WmfDecodeError(
                        "invalid-palette-index",
                        "DIB pixel uses an undefined palette entry",
                    )
            if trailing_pixel:
                packed = dib.pixel_data[row_start + full_bytes]
                if packed >> 4 >= palette_size:
                    raise WmfDecodeError(
                        "invalid-palette-index",
                        "DIB pixel uses an undefined palette entry",
                    )
        elif palette_size < 2:
            full_bytes, trailing_bits = divmod(dib.width, 8)
            row = dib.pixel_data[row_start : row_start + full_bytes]
            if any(row):
                raise WmfDecodeError(
                    "invalid-palette-index",
                    "DIB pixel uses an undefined palette entry",
                )
            if trailing_bits:
                used_mask = 0xFF & (0xFF << (8 - trailing_bits))
                if dib.pixel_data[row_start + full_bytes] & used_mask:
                    raise WmfDecodeError(
                        "invalid-palette-index",
                        "DIB pixel uses an undefined palette entry",
                    )


def _materialize_rgb(dib: _DibDescription) -> bytes:
    """Expand pixels only after the complete WMF grammar has validated."""

    rgb = bytearray(dib.width * dib.height * 3)
    output_offset = 0
    for output_y in range(dib.height):
        stored_y = dib.height - output_y - 1
        row_offset = stored_y * dib.row_stride
        for x in range(dib.width):
            if dib.bits_per_pixel == 24:
                blue, green, red = struct.unpack_from(
                    "<BBB", dib.pixel_data, row_offset + x * 3
                )
            else:
                if dib.bits_per_pixel == 8:
                    palette_index = dib.pixel_data[row_offset + x]
                elif dib.bits_per_pixel == 4:
                    packed = dib.pixel_data[row_offset + x // 2]
                    palette_index = packed >> 4 if x % 2 == 0 else packed & 0x0F
                else:
                    packed = dib.pixel_data[row_offset + x // 8]
                    palette_index = (packed >> (7 - x % 8)) & 1
                if palette_index >= len(dib.palette):
                    raise WmfDecodeError(
                        "invalid-palette-index",
                        "DIB pixel uses an undefined palette entry",
                    )
                red, green, blue = dib.palette[palette_index]
            rgb[output_offset : output_offset + 3] = bytes((red, green, blue))
            output_offset += 3
    return bytes(rgb)


def wmf_png(graphic: WmfGraphic) -> bytes:
    """Return a deterministic PNG after revalidating renderer-facing IR."""

    width, height, rgb = _validated_rgb(graphic)
    scanlines = bytearray((width * 3 + 1) * height)
    source_offset = 0
    target_offset = 0
    row_bytes = width * 3
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
        raise WmfDecodeError("generated-output-limit", "generated WMF PNG is too large")
    return result


def wmf_display_size(
    graphic: WmfGraphic, *, max_width_in: float = 6.5, max_height_in: float = 8.0
) -> tuple[float, float]:
    """Return bounded display dimensions for a validated graphic."""

    width, height, _rgb = _validated_rgb(graphic)
    physical_width = _positive_number(graphic.width_in)
    physical_height = _positive_number(graphic.height_in)
    if physical_width is None or physical_height is None:
        physical_width = width / 96.0
        physical_height = height / 96.0
    maximum_width = _bounded_maximum(max_width_in, 6.5)
    maximum_height = _bounded_maximum(max_height_in, 8.0)
    scale = min(1.0, maximum_width / physical_width, maximum_height / physical_height)
    return physical_width * scale, physical_height * scale


def _validated_rgb(graphic: WmfGraphic) -> tuple[int, int, bytes]:
    if not isinstance(graphic, WmfGraphic):
        raise WmfDecodeError("invalid-ir", "WMF renderer input has the wrong type")
    width = graphic.width_px
    height = graphic.height_px
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
        raise WmfDecodeError("invalid-ir", "WMF renderer dimensions are unsafe")
    if not isinstance(graphic.rgb_data, bytes) or len(graphic.rgb_data) != width * height * 3:
        raise WmfDecodeError("invalid-ir", "WMF renderer pixel storage is inconsistent")
    return width, height, graphic.rgb_data


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


def _positive_limit(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WmfDecodeError(
            "invalid-limits", f"WMF {description} limit must be a nonnegative integer"
        )
    return value


def _png_chunk(kind: str, data: bytes) -> bytes:
    encoded_kind = kind.encode("ascii")
    return (
        struct.pack(">I", len(data))
        + encoded_kind
        + data
        + struct.pack(">I", zlib.crc32(encoded_kind + data) & 0xFFFFFFFF)
    )


def _require_payload(payload: bytes, expected: int, function: int) -> None:
    if len(payload) != expected:
        raise WmfDecodeError(
            "invalid-record-size",
            f"WMF record 0x{function:04x} has an inconsistent payload size",
        )


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _i16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<h", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]
