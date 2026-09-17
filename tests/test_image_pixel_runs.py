"""Compact fill operations remain atomic, bounded and resize-lineage aware."""

import json
import random
from copy import deepcopy

import pytest
from sqlmodel import select
from test_image_operations import detail, make_document, pixel_packet, submit
from test_image_operations import image_client as image_client

from app import image_operations
from app.image_operations import (
    ImageOperationRequest,
    apply_actions,
    map_pixel_index,
    map_pixel_runs,
    normalize_document_palette,
)
from app.image_pixel_codec import MAX_CANVAS_PIXELS, compact_document, decode_document
from app.models import ImageHistoryEntry, ImageOperationReceipt, ProjectMember


def runs_action(runs=None, color="#aabbcc80", layer_id="base"):
    return {
        "type": "pixel-runs",
        "layer_id": layer_id,
        "color": color,
        "runs": runs if runs is not None else [[0, 4]],
    }


def test_one_large_fill_is_a_tiny_atomic_operation_and_shared_undo_redo(image_client):
    source = make_document(1024, 1024)
    resource = image_client["resource"]
    resource.data = {"pixel_art": compact_document(source)}
    image_client["session"].add(resource)
    image_client["session"].commit()
    packet = pixel_packet(
        "fill-all", width=1024, height=1024, actions=[runs_action([[0, 1_048_576]], "#ff00aa80")]
    )
    assert len(json.dumps(packet)) < 300
    result = submit(image_client, packet)
    assert result.status_code == 200, result.json()
    assert result.json()["applied_revision"] == 1
    assert len(result.content) < 30_000
    painted = decode_document(result.json()["resource"]["data"]["pixel_art"])
    assert all(color == "#FF00AA80" for color in painted["layers"][0]["pixels"])
    assert painted["palette"] == ["#FF00AA80"]
    assert len(image_client["session"].exec(select(ImageOperationReceipt)).all()) == 1
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 1
    for revision, kind, color in [(1, "undo", None), (2, "redo", "#FF00AA80")]:
        response = submit(
            image_client,
            pixel_packet(
                f"fill-{kind}",
                width=1024,
                height=1024,
                base_revision=revision,
                actions=[{"type": kind}],
            ),
            "editor",
        )
        assert response.status_code == 200, response.json()
        decoded = decode_document(response.json()["resource"]["data"]["pixel_art"])
        assert all(pixel == color for pixel in decoded["layers"][0]["pixels"])
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 1


@pytest.mark.parametrize("fill_first", [True, False])
def test_fill_and_concurrent_paint_respect_accepted_order(image_client, fill_first):
    fill = pixel_packet("race-fill", actions=[runs_action(color="#0000FF")])
    paint = pixel_packet("race-paint", index=1, color="#FF0000")
    first, second = (
        [(fill, "owner"), (paint, "editor")] if fill_first else [(paint, "editor"), (fill, "owner")]
    )
    for packet, actor in (first, second):
        result = submit(image_client, packet, actor)
        assert result.status_code == 200, result.json()
    expected = ["#0000FF", "#FF0000" if fill_first else "#0000FF", "#0000FF", "#0000FF"]
    assert detail(image_client)["data"]["pixel_art"]["layers"][0]["pixels"] == expected
    undone = submit(
        image_client, pixel_packet("race-undo", base_revision=2, actions=[{"type": "undo"}])
    )
    assert undone.status_code == 200
    assert undone.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == (
        ["#0000FF"] * 4 if fill_first else [None, "#FF0000", None, None]
    )


def test_multiple_runs_and_legacy_actions_keep_packet_order_and_one_history_entry(image_client):
    packet = pixel_packet(
        "ordered-run",
        actions=[
            runs_action(color="#0000FF"),
            {"type": "pixels", "layer_id": "base", "changes": [[1, "#FF0000"]]},
            runs_action([[2, 1]], "#00FF00"),
        ],
    )
    result = submit(image_client, packet)
    assert result.status_code == 200, result.json()
    assert result.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == [
        "#0000FF",
        "#FF0000",
        "#00FF00",
        "#0000FF",
    ]
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 1


def test_run_writes_are_copy_on_write_and_different_layers_share_one_revision(image_client):
    source = make_document()
    source["layers"].append({**source["layers"][0], "id": "second", "pixels": ["#11223380"] * 4})
    source = normalize_document_palette(source)
    before = deepcopy(source)
    packet = ImageOperationRequest.model_validate(pixel_packet("runs-cow", actions=[runs_action()]))
    applied = apply_actions(source, packet, image_client["resource"])
    assert source == before
    assert applied["layers"][0]["pixels"] is not source["layers"][0]["pixels"]
    assert applied["layers"][1]["pixels"] is source["layers"][1]["pixels"]
    resource = image_client["resource"]
    resource.data = {"pixel_art": source}
    image_client["session"].add(resource)
    image_client["session"].commit()
    response = submit(
        image_client,
        pixel_packet(
            "multi-layer-runs",
            actions=[
                runs_action(color="#FFFFFF"),
                runs_action(color=None, layer_id="second"),
            ],
        ),
    )
    assert response.status_code == 200, response.json()
    assert response.json()["applied_revision"] == 1
    layers = response.json()["resource"]["data"]["pixel_art"]["layers"]
    assert layers[0]["pixels"] == ["#FFFFFF"] * 4
    assert layers[1]["pixels"] == [None] * 4
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 1


@pytest.mark.parametrize("change", [{"color": "bad"}, {"color": True}, {"expected_color": None}])
def test_runs_reject_invalid_color_and_unknown_conditional_fields(image_client, change):
    response = submit(
        image_client, pixel_packet("bad-run-field", actions=[{**runs_action(), **change}])
    )
    assert response.status_code == 422
    assert detail(image_client)["revision"] == 0


def test_adjacent_runs_retry_idempotently_without_reapplying_and_changed_retry_rejects(
    image_client,
):
    packet = pixel_packet("run-retry", actions=[runs_action([[0, 2], [2, 2]])])
    accepted = submit(image_client, packet)
    assert accepted.status_code == 200
    retry = submit(image_client, deepcopy(packet))
    assert retry.status_code == 200
    assert retry.json()["applied_revision"] == accepted.json()["applied_revision"] == 1
    assert detail(image_client)["revision"] == 1
    assert len(image_client["session"].exec(select(ImageOperationReceipt)).all()) == 1
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 1
    changed = deepcopy(packet)
    changed["actions"][0]["runs"] = [[0, 1], [1, 3]]
    rejected = submit(image_client, changed)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "image_operation_id_reused"
    assert detail(image_client)["revision"] == 1


@pytest.mark.parametrize("role,code", [("viewer", 403), ("outsider", 404)])
def test_run_operations_preserve_authorization(image_client, role, code):
    result = submit(image_client, pixel_packet("forbidden-run", actions=[runs_action()]), role)
    assert result.status_code == code
    assert detail(image_client)["revision"] == 0


def test_revoked_actor_cannot_retrieve_a_run_receipt(image_client):
    packet = pixel_packet("revoked-run", actions=[runs_action()])
    assert submit(image_client, packet, "editor").status_code == 200
    member = (
        image_client["session"]
        .exec(select(ProjectMember).where(ProjectMember.user_id == image_client["editor"].id))
        .one()
    )
    member.role = "viewer"
    image_client["session"].add(member)
    image_client["session"].commit()
    assert submit(image_client, packet, "editor").status_code == 403


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [[True, 1]],
        [[0, True]],
        [[0, 0]],
        [[0, -1]],
        [[-1, 1]],
        [["0", 1]],
        [[0, 1.5]],
        [[0, 5]],
        [[4, 1]],
        [[0, 3], [2, 1]],
        [[2, 1], [0, 1]],
        [[0, 1, 2]],
        [[0, MAX_CANVAS_PIXELS + 1]],
    ],
)
def test_malformed_runs_reject_before_mutation(image_client, runs):
    result = submit(image_client, pixel_packet("invalid-run", actions=[runs_action(runs)]))
    assert result.status_code == 422, result.json()
    assert detail(image_client)["revision"] == 0
    assert not image_client["session"].exec(select(ImageOperationReceipt)).all()


def test_run_count_is_bounded_before_application(image_client):
    result = submit(
        image_client,
        pixel_packet(
            "too-many-runs",
            width=1024,
            height=1024,
            actions=[runs_action([[index, 1] for index in range(65_537)])],
        ),
    )
    assert result.status_code == 422
    assert detail(image_client)["revision"] == 0


def test_aggregate_coverage_includes_legacy_pixels_and_all_run_actions(image_client):
    actions = [
        runs_action([[0, MAX_CANVAS_PIXELS]], None),
        {"type": "pixels", "layer_id": "base", "changes": [[0, None]]},
    ]
    result = submit(
        image_client, pixel_packet("coverage-overflow", width=2048, height=2048, actions=actions)
    )
    assert result.status_code == 422
    actions = [runs_action([[0, 1_048_576]], None) for _ in range(5)]
    result = submit(
        image_client,
        pixel_packet("run-coverage-overflow", width=1024, height=1024, actions=actions),
    )
    assert result.status_code == 422
    assert detail(image_client)["revision"] == 0
    assert not image_client["session"].exec(select(ImageOperationReceipt)).all()


def test_runs_do_not_relax_legacy_quota_but_do_not_consume_it(image_client, monkeypatch):
    monkeypatch.setattr(image_operations, "MAX_PIXEL_CHANGES", 1)
    good = pixel_packet(
        "run-plus-legacy",
        actions=[runs_action(), {"type": "pixels", "layer_id": "base", "changes": [[0, None]]}],
    )
    assert submit(image_client, good).status_code == 200
    bad = pixel_packet(
        "too-much-legacy",
        actions=[
            runs_action(),
            {"type": "pixels", "layer_id": "base", "changes": [[0, None], [1, None]]},
        ],
    )
    assert submit(image_client, bad).status_code == 422
    assert detail(image_client)["revision"] == 1


def test_frame_maximum_validates_without_expanding_runs():
    packet = ImageOperationRequest.model_validate(
        pixel_packet(
            "max-run", width=4096, height=1024, actions=[runs_action([[0, MAX_CANVAS_PIXELS]])]
        )
    )
    assert packet.actions[0].runs == [(0, MAX_CANVAS_PIXELS)]


def test_run_rebase_matches_individual_pixel_mapping_for_random_small_lineages():
    generator = random.Random(934852)
    for _ in range(200):
        width, height = generator.randint(1, 8), generator.randint(1, 8)
        selected = [index for index in range(width * height) if generator.random() < 0.6]
        runs = []
        for index in selected:
            if runs and runs[-1][0] + runs[-1][1] == index:
                start, length = runs[-1]
                runs[-1] = (start, length + 1)
            else:
                runs.append((index, 1))
        transforms = []
        for _ in range(3):
            target_width, target_height = generator.randint(1, 8), generator.randint(1, 8)
            transforms.append(
                {
                    "from_width": width,
                    "from_height": height,
                    "to_width": target_width,
                    "to_height": target_height,
                    "offset_x": generator.randint(-width, target_width),
                    "offset_y": generator.randint(-height, target_height),
                }
            )
            width, height = target_width, target_height
        expected = [
            mapped
            for index in selected
            if (mapped := map_pixel_index(index, transforms)) is not None
        ]
        mapped_runs = map_pixel_runs(runs, transforms)
        assert [
            index for start, length in mapped_runs for index in range(start, start + length)
        ] == expected
        assert all(
            start + length < next_start
            for (start, length), (next_start, _) in zip(mapped_runs, mapped_runs[1:], strict=False)
        )


def test_large_rebase_uses_row_segments_not_individual_indexes(monkeypatch):
    def unexpected_mapping(*_args):
        raise AssertionError("Runs must never expand into individual pixel mappings")

    monkeypatch.setattr(image_operations, "map_pixel_index", unexpected_mapping)
    mapped = map_pixel_runs(
        [(0, 1_048_576)],
        [
            {
                "from_width": 1024,
                "from_height": 1024,
                "to_width": 2048,
                "to_height": 2048,
                "offset_x": 512,
                "offset_y": 512,
            }
        ],
    )
    assert len(mapped) == 1024
    assert sum(length for _, length in mapped) == 1_048_576
    assert mapped[0] == (512 * 2048 + 512, 1024)


def test_stale_runs_rebase_across_expand_crop_without_resurrecting_pixels(image_client):
    for operation_id, base, source_width, source_height, width, height in [
        ("crop", 0, 2, 2, 1, 1),
        ("expand", 1, 1, 1, 2, 2),
    ]:
        response = submit(
            image_client,
            pixel_packet(
                operation_id,
                width=source_width,
                height=source_height,
                base_revision=base,
                actions=[
                    {"type": "resize", "width": width, "height": height, "anchor": "top-left"}
                ],
            ),
        )
        assert response.status_code == 200, response.json()
    result = submit(
        image_client, pixel_packet("stale-full-run", actions=[runs_action(color="#123456")])
    )
    assert result.status_code == 200, result.json()
    assert result.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == [
        "#123456",
        None,
        None,
        None,
    ]


def test_fully_cropped_run_and_missing_layer_ack_as_noops(image_client):
    assert (
        submit(
            image_client,
            pixel_packet(
                "small-crop",
                actions=[{"type": "resize", "width": 1, "height": 1, "anchor": "top-left"}],
            ),
        ).status_code
        == 200
    )
    dropped = submit(image_client, pixel_packet("cropped-run", actions=[runs_action([[3, 1]])]))
    assert dropped.status_code == 200, dropped.json()
    assert dropped.json()["applied_revision"] == 2
    assert dropped.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == [None]
    missing = submit(
        image_client,
        pixel_packet(
            "missing-run",
            width=1,
            height=1,
            base_revision=2,
            actions=[runs_action([[0, 1]], layer_id="missing")],
        ),
    )
    assert missing.status_code == 200
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 1


def test_run_after_pending_resize_uses_its_exact_coordinate_dependency(image_client):
    assert (
        submit(
            image_client,
            pixel_packet(
                "run-resize",
                actions=[{"type": "resize", "width": 4, "height": 4, "anchor": "top-left"}],
            ),
        ).status_code
        == 200
    )
    packet = pixel_packet(
        "dependent-run",
        width=4,
        height=4,
        coordinate_after_operation_id="run-resize",
        actions=[runs_action([[0, 16]], "#123456")],
    )
    result = submit(image_client, packet)
    assert result.status_code == 200, result.json()
    assert result.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == ["#123456"] * 16


def test_large_run_response_limit_preserves_pending_state_and_rolls_back(image_client, monkeypatch):
    source = make_document(1024, 1024)
    resource = image_client["resource"]
    resource.data = {"pixel_art": compact_document(source)}
    image_client["session"].add(resource)
    image_client["session"].commit()
    before = deepcopy(detail(image_client))
    monkeypatch.setattr(image_operations, "MAX_CANONICAL_RESPONSE_BYTES", 1000)
    result = submit(
        image_client,
        pixel_packet(
            "oversized-run-response",
            width=1024,
            height=1024,
            actions=[runs_action([[0, 1_048_576]])],
        ),
    )
    assert result.status_code == 413
    assert detail(image_client) == before
    assert not image_client["session"].exec(select(ImageOperationReceipt)).all()
    assert not image_client["session"].exec(select(ImageHistoryEntry)).all()
