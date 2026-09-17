"""Copy-on-write buffers never alter source snapshots or shared history."""

import builtins
from copy import deepcopy

import pytest
from sqlmodel import select
from test_image_operations import detail, make_document, pixel_packet, submit
from test_image_operations import image_client as image_client

from app import image_operations, image_pixel_codec
from app.image_operations import (
    ImageDocument,
    ImageOperationRequest,
    apply_actions,
    canonical_history_source,
    decompress_document,
    normalize_document_palette,
    resize_document,
)
from app.image_pixel_codec import compact_document, decode_document, encode_pixels
from app.models import ImageHistoryEntry


def source_document():
    document = make_document()
    document["layers"][0]["pixels"] = [None, "#AABBCC", "#11223380", "#FF000000"]
    document["layers"].append(
        {
            **document["layers"][0],
            "id": "second",
            "name": "Second",
            "pixels": ["#010203", None, None, None],
        }
    )
    return normalize_document_palette(document)


@pytest.mark.parametrize(
    "kind", ["noop", "pixels", "conditional-noop", "update", "order", "add", "remove"]
)
def test_apply_actions_preserves_source_and_shares_only_untouched_buffers(image_client, kind):
    source = source_document()
    before = deepcopy(source)
    actions = {
        "noop": [{"type": "pixels", "layer_id": "base", "changes": [[1, "#aabbcc"]]}],
        "pixels": [{"type": "pixels", "layer_id": "base", "changes": [[0, "#ffffff80"]]}],
        "conditional-noop": [
            {"type": "pixels", "layer_id": "base", "changes": [[0, "#FFFFFF", "#010203"]]}
        ],
        "update": [
            {
                "type": "layer-update",
                "layer_id": "base",
                "fields": {"name": "Renamed", "visible": False, "opacity": 0.5},
            }
        ],
        "order": [{"type": "layer-order", "layer_ids": ["second", "base"]}],
        "add": [
            {
                "type": "layer-add",
                "layer": {
                    **source["layers"][0],
                    "id": "added",
                    "pixels": ["#abcdef80", None, None, None],
                },
                "after_id": "base",
            }
        ],
        "remove": [
            {"type": "layer-remove", "layer_id": "second", "expected_layer": source["layers"][1]}
        ],
    }[kind]
    packet = ImageOperationRequest.model_validate(pixel_packet("cow", actions=actions))
    result = apply_actions(source, packet, image_client["resource"])
    assert source == before
    assert result is not source
    assert result["layers"] is not source["layers"]
    originals = {layer["id"]: layer for layer in source["layers"]}
    for layer in result["layers"]:
        if layer["id"] in originals:
            original = originals[layer["id"]]
            assert layer is not original
            if kind == "pixels" and layer["id"] == "base":
                assert layer["pixels"] is not original["pixels"]
                assert layer["pixels"][0] == "#FFFFFF80"
            else:
                assert layer["pixels"] is original["pixels"]
    assert result["palette"] == list(
        dict.fromkeys(
            color for layer in result["layers"] for color in layer["pixels"] if color is not None
        )
    )
    if kind == "add":
        assert (
            next(layer for layer in result["layers"] if layer["id"] == "added")["pixels"][0]
            == "#ABCDEF80"
        )


def test_multiple_writes_copy_a_layer_pixel_buffer_only_once(image_client, monkeypatch):
    source = source_document()
    before = deepcopy(source)
    packet = ImageOperationRequest.model_validate(
        pixel_packet(
            "cow-multi",
            actions=[
                {"type": "pixels", "layer_id": "base", "changes": [[0, "#FFFFFF"]]},
                {"type": "pixels", "layer_id": "base", "changes": [[1, "#00000080"]]},
            ],
        )
    )
    copied_buffers = []

    def capture_list(value):
        result = builtins.list(value)
        if isinstance(value, builtins.list):
            copied_buffers.append(result)
        return result

    monkeypatch.setattr(image_operations, "list", capture_list, raising=False)
    result = apply_actions(source, packet, image_client["resource"])
    assert source == before
    assert copied_buffers == [result["layers"][0]["pixels"]]
    assert result["layers"][0]["pixels"][:2] == ["#FFFFFF", "#00000080"]
    assert result["layers"][1]["pixels"] is source["layers"][1]["pixels"]


def test_noncanonical_source_colors_normalize_without_mutating_the_source(image_client):
    source = source_document()
    source["layers"][0]["pixels"][1] = "#aabbcc"
    before = deepcopy(source)
    packet = ImageOperationRequest.model_validate(pixel_packet("cow-normalize", color=None))
    result = apply_actions(source, packet, image_client["resource"])
    assert source == before
    assert result["layers"][0]["pixels"][1] == "#AABBCC"
    assert result["layers"][0]["pixels"] is not source["layers"][0]["pixels"]
    assert result["layers"][1]["pixels"] is source["layers"][1]["pixels"]


def test_resize_allocates_new_buffers_without_deepcopying_originals(monkeypatch):
    source = source_document()
    before = deepcopy(source)

    def unexpected_deepcopy(*_args):
        raise AssertionError("Resize must not clone old pixel buffers before mapping")

    monkeypatch.setattr(image_operations, "deepcopy", unexpected_deepcopy)
    result = resize_document(
        source,
        {
            "from_width": 2,
            "from_height": 2,
            "to_width": 4,
            "to_height": 4,
            "offset_x": 1,
            "offset_y": 1,
        },
    )
    assert source == before
    assert (result["width"], result["height"]) == (4, 4)
    for original, layer in zip(source["layers"], result["layers"], strict=True):
        assert layer is not original
        assert layer["pixels"] is not original["pixels"]
        assert [layer["pixels"][index] for index in (5, 6, 9, 10)] == original["pixels"]
    result["layers"][0]["pixels"][5] = "#123456"
    assert source == before


@pytest.mark.parametrize(
    "variant", ["canonical", "lowercase", "stale-palette", "coerced-opacity", "legacy"]
)
def test_history_source_reuse_requires_equivalent_normalized_semantics(variant):
    stored = source_document()
    stored["layers"][0]["pixels"] = encode_pixels(stored["layers"][0]["pixels"], force=True)
    if variant == "lowercase":
        stored["layers"][0]["pixels"]["colors"][1] = "#aabbcc"
    elif variant == "stale-palette":
        stored["palette"] = []
    elif variant == "coerced-opacity":
        stored["layers"][0]["opacity"] = "0.5"
    canonical = ImageDocument.model_validate(stored).model_dump(mode="json")
    original_pixels = [layer["pixels"] for layer in canonical["layers"]]
    canonical = normalize_document_palette(canonical)
    if variant == "legacy":
        stored = {"version": 1, "width": 2, "height": 2, "pixels": [None] * 4}
    selected = canonical_history_source(stored, canonical, original_pixels)
    assert selected is (stored if variant == "canonical" else canonical)


@pytest.mark.parametrize("variant", ["canonical", "lowercase", "stale-palette"])
def test_history_snapshot_reuse_and_fallback_preserve_undo(image_client, monkeypatch, variant):
    source = make_document(257, 256)
    source["layers"][0]["pixels"][0] = "#AABBCC"
    source = normalize_document_palette(source)
    stored = compact_document(source)
    if variant == "lowercase":
        stored["layers"][0]["pixels"]["colors"][1] = "#aabbcc"
    elif variant == "stale-palette":
        stored["palette"] = []
    expected_before = deepcopy(source)
    resource = image_client["resource"]
    resource.data = {"pixel_art": stored}
    image_client["session"].add(resource)
    image_client["session"].commit()
    encoded_buffers = []
    original_encode = image_pixel_codec.encode_pixels

    def capture_encode(pixels, **kwargs):
        encoded_buffers.append(len(pixels))
        return original_encode(pixels, **kwargs)

    monkeypatch.setattr(image_pixel_codec, "encode_pixels", capture_encode)
    result = submit(image_client, pixel_packet("cow-history", index=1, width=257, height=256))
    assert result.status_code == 200, result.json()
    assert encoded_buffers == [257 * 256] * (1 if variant == "canonical" else 2)
    entry = image_client["session"].exec(select(ImageHistoryEntry)).one()
    assert decompress_document(entry.before_document) == expected_before
    assert expected_before["layers"][0]["pixels"][1] is None
    undone = submit(
        image_client,
        pixel_packet(
            "cow-undo", width=257, height=256, base_revision=1, actions=[{"type": "undo"}]
        ),
    )
    assert undone.status_code == 200, undone.json()
    assert decode_document(detail(image_client)["data"]["pixel_art"]) == expected_before
