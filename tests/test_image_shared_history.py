"""Global document history and retained resize-coordinate regression coverage."""

import hashlib
import json

import pytest
from sqlalchemy import event
from sqlmodel import select
from test_image_operations import detail, make_document, pixel_packet, submit
from test_image_operations import image_client as image_client

from app import image_operations
from app.models import ImageCanvasTransform, ImageHistoryEntry, ImageOperationReceipt


def setup_document(image, width=3, height=3):
    resource = image["resource"]
    resource.data = {"pixel_art": make_document(width, height)}
    image["session"].add(resource)
    image["session"].commit()


def packet(identifier, action, width=3, height=3, **kwargs):
    return pixel_packet(identifier, width=width, height=height, actions=[action], **kwargs)


def resize(identifier, target_width, target_height, anchor="center", **kwargs):
    return packet(
        identifier,
        {"type": "resize", "width": target_width, "height": target_height, "anchor": anchor},
        **kwargs,
    )


def history_action(identifier, kind, **kwargs):
    return packet(identifier, {"type": kind}, **kwargs)


def state(image, revision=0, role="owner"):
    return image["client"].get(
        image["url"] + "/image-operations",
        params={"since_revision": revision},
        headers=image["headers"][role],
    )


def test_history_is_global_across_users_and_revision_is_monotonic(image_client):
    assert submit(image_client, pixel_packet("owner-paint", 0)).status_code == 200
    assert (
        submit(image_client, pixel_packet("editor-paint", 1, "#00FF00"), "editor").status_code
        == 200
    )
    undo = submit(image_client, history_action("undo-other-user", "undo"))
    assert undo.status_code == 200
    assert undo.json()["resource"]["revision"] == 3
    assert undo.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == [
        "#FF0000",
        None,
        None,
        None,
    ]
    assert undo.json()["history"] == {"can_undo": True, "can_redo": True}
    redo = submit(image_client, history_action("redo-shared", "redo"), "editor")
    assert redo.status_code == 200
    assert redo.json()["resource"]["revision"] == 4
    assert redo.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == [
        "#FF0000",
        "#00FF00",
        None,
        None,
    ]
    assert redo.json()["history"] == {"can_undo": True, "can_redo": False}
    assert state(image_client, role="viewer").json()["history"] == redo.json()["history"]


def test_new_real_edit_invalidates_redo_but_noops_do_not(image_client):
    assert submit(image_client, pixel_packet("paint")).status_code == 200
    assert submit(image_client, history_action("undo", "undo")).status_code == 200
    no_op = submit(image_client, pixel_packet("no-op", color=None), "editor")
    assert no_op.status_code == 200
    assert no_op.json()["history"] == {"can_undo": False, "can_redo": True}
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 1
    branch = submit(image_client, pixel_packet("branch", 1, "#00FF00"), "editor")
    assert branch.json()["history"] == {"can_undo": True, "can_redo": False}
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 1
    redo = submit(image_client, history_action("nothing-to-redo", "redo"))
    assert redo.status_code == 200
    assert redo.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == [
        None,
        "#00FF00",
        None,
        None,
    ]


def test_same_gesture_packets_group_and_undo_the_whole_stroke(image_client):
    assert (
        submit(image_client, pixel_packet("part-a", 0, history_group_id="gesture")).status_code
        == 200
    )
    assert (
        submit(image_client, pixel_packet("part-b", 1, history_group_id="gesture")).status_code
        == 200
    )
    entries = image_client["session"].exec(select(ImageHistoryEntry)).all()
    assert len(entries) == 1
    assert entries[0].latest_edit_revision == 2
    undo = submit(image_client, history_action("undo-stroke", "undo"))
    assert undo.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == [None] * 4


def test_other_users_real_edit_and_undo_redo_each_break_gesture_group(image_client):
    assert (
        submit(image_client, pixel_packet("part-a", 0, history_group_id="same")).status_code == 200
    )
    assert submit(image_client, pixel_packet("other", 1, "#00FF00"), "editor").status_code == 200
    assert (
        submit(image_client, pixel_packet("part-b", 2, history_group_id="same")).status_code == 200
    )
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 3
    assert submit(image_client, history_action("undo", "undo")).status_code == 200
    assert submit(image_client, history_action("redo", "redo")).status_code == 200
    assert (
        submit(image_client, pixel_packet("part-c", 3, history_group_id="same")).status_code == 200
    )
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 4


def test_semantic_resize_preserves_latest_concurrent_pixels_and_maps_old_packets(image_client):
    setup_document(image_client)
    assert (
        submit(image_client, pixel_packet("latest", 4, "#00FF00", width=3, height=3)).status_code
        == 200
    )
    expanded = submit(image_client, resize("expand", 4, 4), "editor")
    assert expanded.status_code == 200
    assert expanded.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"][10] == "#00FF00"
    event = expanded.json()["transforms"][0]
    assert (
        event["from_width"],
        event["to_width"],
        event["offset_x"],
        event["offset_y"],
        event["revision"],
    ) == (3, 4, 1, 1, 2)
    assert event["operation_id"] == "expand"
    stale = submit(image_client, pixel_packet("stale", 0, width=3, height=3))
    assert stale.status_code == 200
    assert stale.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"][5] == "#FF0000"
    assert stale.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"][10] == "#00FF00"


def test_concurrent_absolute_resize_targets_apply_to_current_canonical(image_client):
    setup_document(image_client)
    assert submit(image_client, pixel_packet("paint", 4, width=3, height=3)).status_code == 200
    assert submit(image_client, resize("first", 4, 4)).status_code == 200
    latest = submit(image_client, resize("second", 5, 5, "right"), "editor")
    assert latest.status_code == 200
    event = latest.json()["transforms"][-1]
    assert (event["from_width"], event["to_width"], event["offset_x"], event["offset_y"]) == (
        4,
        5,
        1,
        1,
    )
    assert latest.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"][18] == "#FF0000"


def test_equal_dimensions_aba_resize_chain_drops_intermediate_cropped_pixels(image_client):
    setup_document(image_client, 4, 4)
    assert submit(image_client, resize("crop", 2, 2, width=4, height=4)).status_code == 200
    assert (
        submit(image_client, resize("expand", 4, 4, width=2, height=2, base_revision=1)).status_code
        == 200
    )
    old = submit(
        image_client,
        pixel_packet(
            "aba",
            width=4,
            height=4,
            actions=[
                {
                    "type": "pixels",
                    "layer_id": "base",
                    "changes": [[0, "#FF0000"], [5, "#00FF00"]],
                }
            ],
        ),
    )
    assert old.status_code == 200
    pixels = old.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"]
    assert pixels[0] is None
    assert pixels[5] == "#00FF00"
    assert len(old.json()["transforms"]) == 2


def test_undo_resize_uses_exact_inverse_offset_not_recomputed_anchor(image_client):
    setup_document(image_client)
    assert submit(image_client, pixel_packet("paint", 8, width=3, height=3)).status_code == 200
    crop = submit(image_client, resize("crop", 2, 2))
    assert crop.json()["transforms"][-1]["offset_x"] == 0
    undo = submit(image_client, history_action("undo-crop", "undo"), "editor")
    restored = undo.json()["resource"]["data"]["pixel_art"]
    assert restored["width"] == restored["height"] == 3
    assert restored["layers"][0]["pixels"][8] == "#FF0000"
    assert undo.json()["transforms"][-1]["offset_x"] == 0
    redo = submit(image_client, history_action("redo-crop", "redo"))
    assert redo.json()["resource"]["data"]["pixel_art"]["width"] == 2
    assert redo.json()["transforms"][-1]["offset_x"] == 0


def test_stale_new_layer_pixels_are_mapped_through_actual_resize(image_client):
    setup_document(image_client)
    assert submit(image_client, resize("expand", 4, 4)).status_code == 200
    layer = {**make_document(3, 3)["layers"][0], "id": "new", "pixels": ["#FF0000"] + [None] * 8}
    added = submit(
        image_client,
        packet("add", {"type": "layer-add", "layer": layer, "after_id": "missing"}),
        "editor",
    )
    assert added.status_code == 200
    layers = added.json()["resource"]["data"]["pixel_art"]["layers"]
    assert layers[1]["pixels"][5] == "#FF0000"
    assert len(layers[1]["pixels"]) == 16


def test_frame_dependency_uses_accepted_resize_not_old_base_revision(image_client):
    setup_document(image_client)
    assert submit(image_client, resize("my-resize", 4, 4)).status_code == 200
    assert submit(image_client, resize("their-resize", 5, 5), "editor").status_code == 200
    pending = submit(
        image_client,
        pixel_packet(
            "after-my-resize", 0, width=4, height=4, coordinate_after_operation_id="my-resize"
        ),
    )
    assert pending.status_code == 200
    assert pending.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"][6] == "#FF0000"
    assert len(pending.json()["transforms"]) == 2


def test_frame_dependency_cannot_reference_another_users_operation(image_client):
    setup_document(image_client)
    assert submit(image_client, resize("theirs", 4, 4), "editor").status_code == 200
    pending = submit(
        image_client,
        pixel_packet("wrong-user", width=4, height=4, coordinate_after_operation_id="theirs"),
    )
    assert pending.status_code == 409
    assert pending.json()["detail"]["code"] == "image_coordinate_dependency_invalid"
    assert detail(image_client)["revision"] == 1


def test_unknown_author_frame_is_not_guessed_from_current_dimensions(image_client):
    setup_document(image_client)
    assert submit(image_client, resize("expand", 4, 4)).status_code == 200
    forged = submit(image_client, pixel_packet("wrong-frame", width=4, height=4))
    assert forged.status_code == 409
    assert forged.json()["detail"]["code"] == "image_coordinate_frame_unknown"
    assert detail(image_client)["revision"] == 1


def test_duplicate_undo_after_new_branch_does_not_undo_twice(image_client):
    assert submit(image_client, pixel_packet("paint")).status_code == 200
    undo_packet = history_action("lost-ack", "undo")
    first = submit(image_client, undo_packet)
    assert submit(image_client, pixel_packet("branch", 1, "#00FF00"), "editor").status_code == 200
    duplicate = submit(image_client, undo_packet)
    assert duplicate.status_code == 200
    assert duplicate.json()["applied_revision"] == first.json()["applied_revision"] == 2
    assert duplicate.json()["resource"]["revision"] == 3
    assert duplicate.json()["history"] == {"can_undo": True, "can_redo": False}
    assert duplicate.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"][1] == "#00FF00"


def test_history_cap_does_not_prune_coordinate_lineage(image_client, monkeypatch):
    setup_document(image_client)
    monkeypatch.setattr(image_operations, "MAX_HISTORY_ENTRIES", 1)
    assert submit(image_client, resize("expand", 4, 4)).status_code == 200
    assert submit(image_client, resize("expand-again", 5, 5)).status_code == 200
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 1
    assert len(image_client["session"].exec(select(ImageCanvasTransform)).all()) == 2
    old = submit(image_client, pixel_packet("old", width=3, height=3))
    assert old.status_code == 200
    assert old.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"][12] == "#FF0000"


def test_semantic_import_is_shared_undoable_and_does_not_require_stale_base_choice(image_client):
    assert submit(image_client, pixel_packet("other-paint"), "editor").status_code == 200
    imported = make_document(3, 3)
    imported["layers"][0]["pixels"][8] = "#00FF00"
    response = submit(
        image_client, packet("import", {"type": "import", "document": imported}, width=2, height=2)
    )
    assert response.status_code == 200
    assert response.json()["resource"]["data"]["pixel_art"]["width"] == 3
    undo = submit(image_client, history_action("undo-import", "undo"), "editor")
    assert undo.status_code == 200
    assert undo.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == [
        "#FF0000",
        None,
        None,
        None,
    ]


def test_old_deployed_receipt_hash_remains_valid_after_optional_fields_added(image_client):
    raw = pixel_packet("old-client")
    parsed = image_operations.ImageOperationRequest.model_validate(raw).model_dump(mode="json")
    parsed.pop("history_group_id")
    parsed.pop("coordinate_after_operation_id")
    receipt = ImageOperationReceipt(
        resource_id=image_client["resource"].id,
        user_id=image_client["resource"].project_id,
        operation_id="old-client",
        applied_revision=0,
        request_hash=hashlib.sha256(
            json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    # Use the same authenticated owner scope as an already accepted old packet.
    receipt.user_id = image_client["project"].owner_id
    image_client["session"].add(receipt)
    image_client["session"].commit()
    assert submit(image_client, raw).status_code == 200
    assert detail(image_client)["revision"] == 0


@pytest.mark.parametrize("kind", ["resize", "undo", "redo", "import"])
def test_history_and_canvas_semantic_actions_cannot_mix_with_pixels(image_client, kind):
    action = {"type": kind}
    if kind == "resize":
        action.update(width=3, height=3, anchor="center")
    if kind == "import":
        action["document"] = make_document()
    mixed = pixel_packet("mixed", actions=[action, pixel_packet("unused")["actions"][0]])
    assert submit(image_client, mixed).status_code == 422


def test_state_get_filters_lineage_by_revision_and_rejects_future_or_negative(image_client):
    setup_document(image_client)
    assert submit(image_client, resize("expand", 4, 4)).status_code == 200
    assert state(image_client, 0).json()["transforms"][0]["revision"] == 1
    assert state(image_client, 1).json()["transforms"] == []
    assert state(image_client, 2).status_code == 409
    assert state(image_client, -1).status_code == 422


def test_history_snapshots_and_lineage_roll_back_with_failed_receipt_commit(
    image_client, monkeypatch
):
    setup_document(image_client)
    session = image_client["session"]

    def fail_commit():
        session.flush()
        raise RuntimeError("Simulated history transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="Simulated history transaction failure"):
            submit(image_client, resize("failed", 4, 4))
    assert detail(image_client)["revision"] == 0
    assert session.exec(select(ImageHistoryEntry)).all() == []
    assert session.exec(select(ImageCanvasTransform)).all() == []
    assert session.exec(select(ImageOperationReceipt)).all() == []


def test_identity_resize_proves_lost_ack_frame_without_adding_history_or_clearing_redo(
    image_client,
):
    setup_document(image_client)
    assert submit(image_client, pixel_packet("paint", width=3, height=3)).status_code == 200
    assert submit(image_client, history_action("undo", "undo")).status_code == 200
    same = submit(image_client, resize("identity-lost-ack", 3, 3))
    assert same.status_code == 200
    assert same.json()["history"] == {"can_undo": False, "can_redo": True}
    event = same.json()["transforms"][-1]
    assert (event["from_width"], event["to_width"], event["offset_x"], event["operation_id"]) == (
        3,
        3,
        0,
        "identity-lost-ack",
    )
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 1
    assert submit(image_client, resize("theirs", 4, 4), "editor").status_code == 200
    pending = submit(
        image_client,
        pixel_packet(
            "dependent", width=3, height=3, coordinate_after_operation_id="identity-lost-ack"
        ),
    )
    assert pending.status_code == 200
    assert pending.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"][5] == "#FF0000"


def test_same_dimension_import_proves_dependency_even_after_lost_ack(image_client):
    imported = submit(
        image_client,
        packet(
            "same-size-import", {"type": "import", "document": make_document()}, width=2, height=2
        ),
    )
    assert imported.status_code == 200
    assert imported.json()["transforms"][0]["operation_id"] == "same-size-import"
    assert imported.json()["history"] == {"can_undo": False, "can_redo": False}
    pending = submit(
        image_client, pixel_packet("after-import", coordinate_after_operation_id="same-size-import")
    )
    assert pending.status_code == 200


def test_polling_and_normal_edits_do_not_fetch_history_snapshot_blobs(image_client):
    assert submit(image_client, pixel_packet("first")).status_code == 200
    statements = []
    engine = image_client["session"].get_bind()

    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        assert state(image_client).status_code == 200
        assert submit(image_client, pixel_packet("second", 1, "#00FF00")).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    selects = [
        statement
        for statement in statements
        if statement.lstrip().startswith("select") and "image_history_entries" in statement
    ]
    assert selects
    # SQL LENGTH accesses blobs inside PostgreSQL, but never transfers them to
    # the API worker. Metadata/EXISTS and scalar lengths are all this path needs.
    assert all(
        "before_document" not in statement and "after_document" not in statement
        for statement in selects
        if "length(" not in statement
    )


def test_history_byte_cap_evicts_old_snapshots_but_preserves_latest_undo(image_client, monkeypatch):
    monkeypatch.setattr(image_operations, "MAX_HISTORY_BYTES", 1)
    assert submit(image_client, pixel_packet("first")).status_code == 200
    assert submit(image_client, pixel_packet("second", 1, "#00FF00")).status_code == 200
    assert len(image_client["session"].exec(select(ImageHistoryEntry)).all()) == 1
    undone = submit(image_client, history_action("undo", "undo"))
    assert undone.json()["history"] == {"can_undo": False, "can_redo": True}
    assert undone.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == [
        "#FF0000",
        None,
        None,
        None,
    ]


def test_old_exact_replace_retry_backfills_end_frame_without_reapplying_or_retroactive_history(
    image_client,
):
    imported = make_document(3, 3)
    raw = packet("old-replace", {"type": "replace", "document": imported}, width=2, height=2)
    payload = image_operations.ImageOperationRequest.model_validate(raw).model_dump(mode="json")
    payload.pop("history_group_id")
    payload.pop("coordinate_after_operation_id")
    resource = image_client["resource"]
    resource.data = {"pixel_art": imported}
    resource.revision = 1
    receipt = ImageOperationReceipt(
        resource_id=resource.id,
        user_id=image_client["project"].owner_id,
        operation_id="old-replace",
        applied_revision=1,
        request_hash=hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    image_client["session"].add_all([resource, receipt])
    image_client["session"].commit()
    duplicate = submit(image_client, raw)
    assert duplicate.status_code == 200
    assert duplicate.json()["applied_revision"] == duplicate.json()["resource"]["revision"] == 1
    assert duplicate.json()["history"] == {"can_undo": False, "can_redo": False}
    assert duplicate.json()["transforms"] == []
    image_client["session"].refresh(receipt)
    assert (receipt.action_kind, receipt.coordinate_width, receipt.coordinate_height) == (
        "replace",
        3,
        3,
    )
    assert image_client["session"].exec(select(ImageHistoryEntry)).all() == []
    dependent = submit(
        image_client,
        pixel_packet(
            "after-old-replace",
            8,
            "#00FF00",
            width=3,
            height=3,
            coordinate_after_operation_id="old-replace",
        ),
    )
    assert dependent.status_code == 200
    assert dependent.json()["resource"]["revision"] == 2
    assert dependent.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"][8] == "#00FF00"
