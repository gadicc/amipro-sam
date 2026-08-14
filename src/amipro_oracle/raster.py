from __future__ import annotations

import binascii
import math
import struct
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Covers a 300-DPI Letter/A4 oracle page while bounding untrusted decode memory/CPU.
MAX_RASTER_PIXELS = 10_000_000
MAX_PNG_BYTES = 48 * 1024 * 1024


def encode_rgb_png(width: int, height: int, pixels: bytes) -> bytes:
    if width <= 0 or height <= 0 or len(pixels) != width * height * 3:
        raise ValueError("invalid RGB raster")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", binascii.crc32(body))

    scanlines = b"".join(
        b"\x00" + pixels[row * width * 3 : (row + 1) * width * 3]
        for row in range(height)
    )
    return b"".join(
        (
            PNG_SIGNATURE,
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(scanlines, level=9)),
            chunk(b"IEND", b""),
        )
    )


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def decode_png(path: Path) -> tuple[int, int, bytes]:
    size = path.stat().st_size
    if size > MAX_PNG_BYTES:
        raise ValueError(f"PNG exceeds the {MAX_PNG_BYTES} byte limit: {path}")
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError(f"not a PNG file: {path}")

    offset = len(PNG_SIGNATURE)
    width = height = color_type = bit_depth = interlace = None
    compressed = bytearray()
    saw_end = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError(f"truncated PNG chunk: {path}")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError(f"truncated PNG payload: {path}")
        payload = data[offset + 8 : offset + 8 + length]
        checksum = struct.unpack(">I", data[offset + 8 + length : end])[0]
        if binascii.crc32(kind + payload) != checksum:
            raise ValueError(f"PNG CRC mismatch in {kind!r}: {path}")
        if kind == b"IHDR":
            if len(payload) != 13 or width is not None:
                raise ValueError(f"invalid PNG header: {path}")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if compression != 0 or filtering != 0:
                raise ValueError(f"unsupported PNG compression/filter method: {path}")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            saw_end = True
            break
        offset = end

    if not saw_end or width is None or height is None:
        raise ValueError(f"incomplete PNG: {path}")
    if width <= 0 or height <= 0 or width * height > MAX_RASTER_PIXELS:
        raise ValueError(f"unsafe PNG dimensions {width}x{height}: {path}")
    if bit_depth != 8 or color_type not in {0, 2, 4, 6} or interlace != 0:
        raise ValueError(
            f"unsupported PNG format (depth={bit_depth}, color={color_type}, "
            f"interlace={interlace}): {path}"
        )

    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    stride = width * channels
    expected = height * (stride + 1)
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(bytes(compressed), expected + 1)
    if decompressor.unconsumed_tail or not decompressor.eof:
        raise ValueError(f"PNG image data exceeds its declared dimensions: {path}")
    raw += decompressor.flush()
    if len(raw) != expected or decompressor.unused_data:
        raise ValueError(f"unexpected PNG image-data length: {path}")

    decoded = bytearray(height * stride)
    source_offset = 0
    for row in range(height):
        filter_type = raw[source_offset]
        source_offset += 1
        current = bytearray(raw[source_offset : source_offset + stride])
        source_offset += stride
        previous_start = (row - 1) * stride
        for index in range(stride):
            left = current[index - channels] if index >= channels else 0
            above = decoded[previous_start + index] if row else 0
            upper_left = (
                decoded[previous_start + index - channels] if row and index >= channels else 0
            )
            if filter_type == 1:
                current[index] = (current[index] + left) & 0xFF
            elif filter_type == 2:
                current[index] = (current[index] + above) & 0xFF
            elif filter_type == 3:
                current[index] = (current[index] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                current[index] = (current[index] + _paeth(left, above, upper_left)) & 0xFF
            elif filter_type != 0:
                raise ValueError(f"unsupported PNG row filter {filter_type}: {path}")
        decoded[row * stride : (row + 1) * stride] = current

    rgba = bytearray(width * height * 4)
    for pixel in range(width * height):
        source = pixel * channels
        target = pixel * 4
        if color_type == 0:
            rgba[target : target + 4] = bytes((decoded[source],) * 3 + (255,))
        elif color_type == 2:
            rgba[target : target + 4] = decoded[source : source + 3] + b"\xff"
        elif color_type == 4:
            rgba[target : target + 4] = bytes(
                (decoded[source], decoded[source], decoded[source], decoded[source + 1])
            )
        else:
            rgba[target : target + 4] = decoded[source : source + 4]
    return width, height, bytes(rgba)


def raster_difference(
    expected: Path,
    actual: Path,
    *,
    pixel_threshold: float,
) -> dict[str, object]:
    if not math.isfinite(pixel_threshold) or not 0 <= pixel_threshold <= 1:
        raise ValueError("pixel threshold must be between zero and one")
    expected_width, expected_height, expected_pixels = decode_png(expected)
    actual_width, actual_height, actual_pixels = decode_png(actual)
    if (expected_width, expected_height) != (actual_width, actual_height):
        return {
            "dimensions_equal": False,
            "expected_dimensions": [expected_width, expected_height],
            "actual_dimensions": [actual_width, actual_height],
            "rmse": None,
            "different_pixel_ratio": 1.0,
        }

    squared = 0
    different = 0
    pixels = expected_width * expected_height
    threshold_value = pixel_threshold * 255
    for pixel in range(pixels):
        start = pixel * 4
        differences = [
            abs(expected_pixels[start + channel] - actual_pixels[start + channel])
            for channel in range(3)
        ]
        squared += sum(value * value for value in differences)
        if max(differences) > threshold_value:
            different += 1
    return {
        "dimensions_equal": True,
        "expected_dimensions": [expected_width, expected_height],
        "actual_dimensions": [actual_width, actual_height],
        "rmse": round(math.sqrt(squared / (pixels * 3)) / 255, 9),
        "different_pixel_ratio": round(different / pixels, 9),
    }
