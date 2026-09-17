"""Compact transport interoperability, expansion limits and large canvas history."""

import base64
import json
import zlib
from copy import deepcopy
from uuid import UUID

import pytest
from sqlmodel import select
from test_image_operations import detail, make_document, pixel_packet, submit
from test_image_operations import image_client as image_client

from app import image_operations
from app.image_pixel_codec import (
    MAX_DOCUMENT_PIXELS,
    compact_document,
    decode_document,
    decode_pixels,
    encode_pixels,
)
from app.models import ImageHistoryEntry, ImageOperationReceipt

INTEROP_PIXELS = [
    None,
    "#AABBCC",
    "#11223380",
    "#abcdefFF",
    "#00000000",
    None,
    "#AABBCC",
    "#11223380",
    None,
    None,
    None,
    "#FFFFFF",
    "#FFFFFF",
    "#AABBCC",
    "#11223380",
    None,
]
INTEROP_PACKED = {
    "encoding": "indexed-deflate-v1",
    "colors": [None, "#AABBCC", "#11223380", "#abcdefFF", "#00000000", "#FFFFFF"],
    "index_bytes": 1,
    "data": "eAFjYGRiZmFgZGJgYGBlZWRiAAAA4gAb",
}


def test_python_fixture_is_lossless_and_keeps_small_arrays_compatible():
    assert encode_pixels(INTEROP_PIXELS) is INTEROP_PIXELS
    assert encode_pixels(INTEROP_PIXELS, force=True) == INTEROP_PACKED
    assert decode_pixels(INTEROP_PACKED, 16) == INTEROP_PIXELS


@pytest.mark.parametrize("count,index_bytes", [(4, 1), (256, 2), (65536, 4)])
def test_index_width_boundaries_and_little_endian_roundtrip(count, index_bytes):
    pixels = [f"#{index:06X}" for index in range(count)]
    encoded = encode_pixels(pixels, force=True)
    assert encoded["index_bytes"] == index_bytes
    assert decode_pixels(encoded, count) == pixels


@pytest.mark.parametrize(
    "change",
    [
        {"encoding": "unknown"},
        {"index_bytes": True},
        {"index_bytes": 3},
        {"colors": [None, "#AABBCC", "#AABBCC"]},
        {"colors": ["#FFFFFF"]},
        {"colors": [None, "invalid"]},
        {"data": "not-base64!"},
        {"extra": 1},
        {"data": base64.b64encode(zlib.compress(bytes([255] * 16))).decode()},
        {"data": base64.b64encode(zlib.compress(bytes(17))).decode()},
        {"data": base64.b64encode(zlib.compress(bytes(15))).decode()},
        {"data": base64.b64encode(zlib.compress(bytes(16)) + b"trailing").decode()},
        {"data": base64.b64encode(zlib.compress(bytes(16))[:-1]).decode()},
    ],
)
def test_compact_pixels_reject_malformed_or_oversized_streams(change):
    with pytest.raises(ValueError):
        decode_pixels({**INTEROP_PACKED, **change}, 16)


def test_document_budget_is_checked_before_inflating(monkeypatch):
    def unexpected_decode(*_args):
        raise AssertionError("Decoder must not run before document budget validation")

    monkeypatch.setattr("app.image_pixel_codec.decode_pixels", unexpected_decode)
    document = {
        "version": 2,
        "width": 2048,
        "height": 2048,
        "layers": [{"pixels": INTEROP_PACKED}] * 5,
    }
    with pytest.raises(ValueError, match="document pixel budget"):
        decode_document(document)


@pytest.mark.parametrize("kind", ["layer-add", "layer-remove", "import"])
def test_packet_snapshot_budget_is_checked_before_any_inflation(image_client, monkeypatch, kind):
    def unexpected_decode(*_args):
        raise AssertionError("Packet budget must be checked before inflating snapshots")

    monkeypatch.setattr(image_operations, "decode_pixels", unexpected_decode)
    count = 2048 * 2048
    copies = MAX_DOCUMENT_PIXELS // count + 1
    actions = []
    for index in range(copies):
        layer = {"id": f"snapshot-{index}", "name": "Snapshot", "pixels": INTEROP_PACKED}
        if kind == "import":
            actions.append(
                {
                    "type": kind,
                    "document": {"version": 2, "width": 2048, "height": 2048, "layers": [layer]},
                }
            )
        else:
            actions.append(
                {"type": kind, "layer" if kind == "layer-add" else "expected_layer": layer}
            )
    result = submit(
        image_client, pixel_packet("snapshot-budget", width=2048, height=2048, actions=actions)
    )
    assert result.status_code == 413, result.json()
    assert result.json()["detail"]["code"] == "image_document_pixel_limit"
    assert detail(image_client)["revision"] == 0
    assert not image_client["session"].exec(select(ImageOperationReceipt)).all()


def test_legacy_oversized_canvas_rejects_before_blank_allocation(image_client):
    resource = image_client["resource"]
    resource.data = {"pixel_art": {"version": 1, "width": 4096, "height": 4096, "pixels": []}}
    image_client["session"].add(resource)
    image_client["session"].commit()
    result = submit(image_client, pixel_packet("legacy-budget"))
    assert result.status_code == 413
    assert result.json()["detail"]["code"] == "image_document_pixel_limit"
    assert not image_client["session"].exec(select(ImageOperationReceipt)).all()


def test_resize_document_budget_rejects_without_mutation(image_client):
    resource = image_client["resource"]
    document = make_document(2, 2)
    document["layers"] = [{**document["layers"][0], "id": f"layer-{index}"} for index in range(5)]
    resource.data = {"pixel_art": document}
    image_client["session"].add(resource)
    image_client["session"].commit()
    result = submit(
        image_client,
        pixel_packet(
            "resize-budget",
            actions=[
                {"type": "resize", "width": 2048, "height": 2048, "anchor": "center"},
            ],
        ),
    )
    assert result.status_code == 413, result.json()
    assert detail(image_client)["revision"] == 0
    assert not image_client["session"].exec(select(ImageOperationReceipt)).all()


def test_large_three_layer_resize_pixels_history_and_get_stay_compact(image_client):
    document = make_document(32, 32)
    document["layers"] = [
        {
            **document["layers"][0],
            "id": f"layer-{index}",
            "name": f"Layer {index}",
            "pixels": [None, "#AA1122", "#11223380", "#FFFFFF"] * 256,
        }
        for index in range(3)
    ]
    resource = image_client["resource"]
    resource.data = {"pixel_art": document}
    image_client["session"].add(resource)
    image_client["session"].commit()
    resize = pixel_packet(
        "large-resize",
        width=32,
        height=32,
        actions=[
            {"type": "resize", "width": 1024, "height": 1024, "anchor": "top-left"},
        ],
    )
    response = submit(image_client, resize)
    assert response.status_code == 200, response.json()
    assert len(response.content) < 100_000
    packed = response.json()["resource"]["data"]["pixel_art"]
    assert all(layer["pixels"]["encoding"] == "indexed-deflate-v1" for layer in packed["layers"])
    decoded = decode_document(packed)
    assert (decoded["width"], decoded["height"]) == (1024, 1024)
    assert all(len(layer["pixels"]) == 1024 * 1024 for layer in decoded["layers"])
    paint = pixel_packet(
        "large-paint",
        width=1024,
        height=1024,
        base_revision=0,
        coordinate_after_operation_id="large-resize",
        actions=[
            {"type": "pixels", "layer_id": "layer-0", "changes": [[1_048_575, "#FFFFFF80"]]},
        ],
    )
    painted = submit(image_client, paint)
    assert painted.status_code == 200, painted.json()
    assert (
        decode_document(painted.json()["resource"]["data"]["pixel_art"])["layers"][0]["pixels"][-1]
        == "#FFFFFF80"
    )
    for kind, expected in [("undo", None), ("redo", "#FFFFFF80")]:
        result = submit(
            image_client,
            pixel_packet(
                f"large-{kind}", width=1024, height=1024, base_revision=2, actions=[{"type": kind}]
            ),
        )
        assert result.status_code == 200, result.json()
        assert (
            decode_document(result.json()["resource"]["data"]["pixel_art"])["layers"][0]["pixels"][
                -1
            ]
            == expected
        )
    fetched = detail(image_client)["data"]["pixel_art"]
    assert fetched["layers"][0]["pixels"]["encoding"] == "indexed-deflate-v1"
    assert decode_document(fetched)["layers"][0]["pixels"][-1] == "#FFFFFF80"
    stored = image_client["session"].get(type(resource), resource.id)
    assert stored.data["pixel_art"]["layers"][0]["pixels"]["encoding"] == "indexed-deflate-v1"
    for entry in image_client["session"].exec(select(ImageHistoryEntry)).all():
        after = json.loads(zlib.decompress(entry.after_document))
        assert after["layers"][0]["pixels"]["encoding"] == "indexed-deflate-v1"


def test_compact_import_and_layer_snapshot_inputs_accept_large_buffers(image_client):
    document = make_document(1024, 1024)
    document["layers"][0]["pixels"] = ["#FF000080", None] * (1024 * 512)
    compact = compact_document(document)
    result = submit(
        image_client,
        pixel_packet("large-import", actions=[{"type": "import", "document": compact}]),
    )
    assert result.status_code == 200, result.json()
    added = {**compact["layers"][0], "id": "second", "name": "Second"}
    result = submit(
        image_client,
        pixel_packet(
            "large-layer-add",
            width=1024,
            height=1024,
            base_revision=1,
            actions=[{"type": "layer-add", "layer": added, "after_id": "base"}],
        ),
    )
    assert result.status_code == 200, result.json()
    assert len(result.json()["resource"]["data"]["pixel_art"]["layers"]) == 2


def test_generic_resource_creation_and_legacy_dense_get_encode_large_data(image_client):
    document = make_document(257, 256)
    endpoint = image_client["url"].rsplit("/", 1)[0]
    result = image_client["client"].post(
        endpoint,
        json={
            "name": "Large new image",
            "type": "pixel_art",
            "data": {"pixel_art": document},
        },
        headers=image_client["headers"]["owner"],
    )
    assert result.status_code in (200, 201), result.json()
    fetched = image_client["client"].get(
        endpoint + "/" + result.json()["id"], headers=image_client["headers"]["owner"]
    )
    assert fetched.status_code == 200, fetched.json()
    packed = fetched.json()["data"]["pixel_art"]
    assert packed["layers"][0]["pixels"]["encoding"] == "indexed-deflate-v1"
    stored = image_client["session"].get(type(image_client["resource"]), UUID(result.json()["id"]))
    assert stored.data["pixel_art"]["layers"][0]["pixels"]["encoding"] == "indexed-deflate-v1"
    resource = image_client["resource"]
    resource.data = {"pixel_art": document}
    image_client["session"].add(resource)
    image_client["session"].commit()
    assert (
        detail(image_client)["data"]["pixel_art"]["layers"][0]["pixels"]["encoding"]
        == "indexed-deflate-v1"
    )


def test_compressed_import_retry_is_idempotent_across_deflate_bytes(image_client):
    document = compact_document(make_document(257, 256))
    packet = pixel_packet("compact-retry", actions=[{"type": "import", "document": document}])
    first = submit(image_client, packet)
    assert first.status_code == 200, first.json()
    retried = deepcopy(packet)
    pixels = retried["actions"][0]["document"]["layers"][0]["pixels"]
    pixels["data"] = base64.b64encode(zlib.compress(bytes(257 * 256), level=9)).decode()
    retry = submit(image_client, retried)
    assert retry.status_code == 200, retry.json()
    assert retry.json()["applied_revision"] == first.json()["applied_revision"] == 1
    assert len(image_client["session"].exec(select(ImageOperationReceipt)).all()) == 1


@pytest.mark.parametrize("width,height", [(4097, 1), (4096, 4096), (2049, 2048)])
def test_oversized_canvas_actions_reject_without_mutation(image_client, width, height):
    result = submit(
        image_client,
        pixel_packet(
            "oversized",
            actions=[
                {"type": "resize", "width": width, "height": height, "anchor": "center"},
            ],
        ),
    )
    assert result.status_code == 422
    assert detail(image_client)["revision"] == 0
    assert not image_client["session"].exec(select(ImageOperationReceipt)).all()


def test_response_size_guard_still_rolls_back_large_compact_acceptance(image_client, monkeypatch):
    before = deepcopy(detail(image_client))
    monkeypatch.setattr(image_operations, "MAX_CANONICAL_RESPONSE_BYTES", 1000)
    result = submit(
        image_client,
        pixel_packet(
            "too-large-response",
            actions=[
                {"type": "resize", "width": 1024, "height": 1024, "anchor": "center"},
            ],
        ),
    )
    assert result.status_code == 413
    assert detail(image_client) == before
    assert not image_client["session"].exec(select(ImageOperationReceipt)).all()
    assert not image_client["session"].exec(select(ImageHistoryEntry)).all()
