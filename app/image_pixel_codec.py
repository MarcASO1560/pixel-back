"""Bounded, lossless indexed pixel transport for large canonical images."""

import base64
import binascii
import re
import sys
import zlib
from array import array
from typing import Any

MAX_IMAGE_DIMENSION = 4096
MAX_CANVAS_PIXELS = 4_194_304
MAX_DOCUMENT_PIXELS = 16_777_216
COMPACT_PIXEL_THRESHOLD = 65_536
PIXEL_ENCODING = "indexed-deflate-v1"
COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")


def canvas_pixel_count(width: Any, height: Any) -> int:
    if (
        type(width) is not int
        or type(height) is not int
        or not 1 <= width <= MAX_IMAGE_DIMENSION
        or not 1 <= height <= MAX_IMAGE_DIMENSION
        or width * height > MAX_CANVAS_PIXELS
    ):
        raise ValueError("Canvas dimensions exceed the supported pixel budget")
    return width * height


def decode_pixels(value: Any, expected_count: int | None = None) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict) or set(value) != {"encoding", "colors", "index_bytes", "data"}:
        raise ValueError("Invalid compact pixel object")
    colors = value["colors"]
    index_bytes = value["index_bytes"]
    if (
        value["encoding"] != PIXEL_ENCODING
        or type(index_bytes) is not int
        or index_bytes not in (1, 2, 4)
        or not isinstance(colors, list)
        or not colors
        or colors[0] is not None
        or len(colors) > MAX_CANVAS_PIXELS + 1
        or expected_count is not None
        and len(colors) > expected_count + 1
        or len(colors) > 1 << (index_bytes * 8)
        or not isinstance(value["data"], str)
    ):
        raise ValueError("Invalid compact pixel encoding")
    seen: set[str] = set()
    for color in colors[1:]:
        if not isinstance(color, str) or not COLOR_PATTERN.fullmatch(color) or color in seen:
            raise ValueError("Invalid or duplicated compact palette color")
        seen.add(color)
    if expected_count is not None and not 1 <= expected_count <= MAX_CANVAS_PIXELS:
        raise ValueError("Invalid compact pixel count")
    maximum = (expected_count or MAX_CANVAS_PIXELS) * index_bytes
    # The base64 ceiling is independent of compression ratio; never parse an
    # unbounded transport string before applying the decompressed-byte limit.
    # Low-level encoders (including zlib-ng level 1) may choose literal blocks
    # larger than their input. The exact inflated-byte ceiling below remains
    # authoritative; allow bounded compression overhead for valid encoders.
    compressed_budget = maximum * 2 + 1024
    if len(value["data"]) > 4 * ((compressed_budget + 2) // 3):
        raise ValueError("Compact pixel payload exceeds its byte budget")
    try:
        compressed = base64.b64decode(value["data"], validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, maximum + 1)
        if (
            len(raw) > maximum
            or not inflater.eof
            or inflater.unconsumed_tail
            or inflater.unused_data
            or len(raw) % index_bytes
            or not raw
            or expected_count is not None
            and len(raw) != maximum
        ):
            raise ValueError("Compact pixels have invalid length or deflate framing")
    except (binascii.Error, zlib.error) as error:
        raise ValueError("Invalid compact pixel base64 or deflate data") from error
    indices = array({1: "B", 2: "H", 4: "I"}[index_bytes])
    indices.frombytes(raw)
    if sys.byteorder != "little" and index_bytes > 1:
        indices.byteswap()
    if len(indices) > MAX_CANVAS_PIXELS:
        raise ValueError("Compact pixel index exceeds its palette")
    try:
        return [colors[index] for index in indices]
    except IndexError as error:
        raise ValueError("Compact pixel index exceeds its palette") from error


def encode_pixels(pixels: list[Any], *, force: bool = False) -> list[Any] | dict[str, Any]:
    if not force and len(pixels) <= COMPACT_PIXEL_THRESHOLD:
        return pixels
    if not 1 <= len(pixels) <= MAX_CANVAS_PIXELS:
        raise ValueError("Pixel buffer exceeds the supported canvas budget")
    colors: list[str | None] = [None]
    lookup: dict[str | None, int] = {None: 0}
    indices = array("I")
    for pixel in pixels:
        index = lookup.get(pixel)
        if index is None:
            if not isinstance(pixel, str) or not COLOR_PATTERN.fullmatch(pixel):
                raise ValueError("Invalid pixel color")
            index = len(colors)
            lookup[pixel] = index
            colors.append(pixel)
        indices.append(index)
    index_bytes = 1 if len(colors) <= 256 else 2 if len(colors) <= 65536 else 4
    if index_bytes != 4:
        indices = array("B" if index_bytes == 1 else "H", indices)
    if sys.byteorder != "little" and index_bytes > 1:
        indices.byteswap()
    return {
        "encoding": PIXEL_ENCODING,
        "colors": colors,
        "index_bytes": index_bytes,
        "data": base64.b64encode(zlib.compress(indices.tobytes(), level=1)).decode("ascii"),
    }


def decode_document(value: Any) -> Any:
    if not isinstance(value, dict) or value.get("version") != 2:
        return value
    count = canvas_pixel_count(value.get("width"), value.get("height"))
    layers = value.get("layers")
    if (
        not isinstance(layers, list)
        or len(layers) > 128
        or len(layers) * count > MAX_DOCUMENT_PIXELS
    ):
        raise ValueError("Image layers exceed the supported document pixel budget")
    decoded = []
    for layer in layers:
        if not isinstance(layer, dict):
            decoded.append(layer)
            continue
        pixels = layer.get("pixels")
        decoded.append(
            {**layer, "pixels": decode_pixels(pixels, count)} if isinstance(pixels, dict) else layer
        )
    return {**value, "layers": decoded}


def compact_document(value: Any) -> Any:
    if (
        not isinstance(value, dict)
        or value.get("version") != 2
        or not isinstance(value.get("layers"), list)
    ):
        return value
    layers = []
    changed = False
    for layer in value["layers"]:
        if isinstance(layer, dict) and isinstance(layer.get("pixels"), list):
            pixels = encode_pixels(layer["pixels"])
            changed |= pixels is not layer["pixels"]
            layers.append({**layer, "pixels": pixels} if pixels is not layer["pixels"] else layer)
        else:
            layers.append(layer)
    return {**value, "layers": layers} if changed else value


def compact_resource_data(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("pixel_art"), dict):
        document = compact_document(data["pixel_art"])
        return {**data, "pixel_art": document} if document is not data["pixel_art"] else data
    return compact_document(data)
