"""Regression coverage for server-authoritative collaborative image saving."""

from collections.abc import Generator
from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import image_operations
from app.api.deps import get_db
from app.core.security import create_access_token
from app.main import app
from app.models import ImageOperationReceipt, Project, ProjectMember, ProjectResource, User


def make_document(width: int = 2, height: int = 2) -> dict[str, Any]:
    return {
        "version": 2,
        "width": width,
        "height": height,
        "palette": [],
        "layers": [
            {
                "id": "base",
                "name": "Layer 1",
                "visible": True,
                "locked": False,
                "opacity": 1,
                "pixels": [None] * (width * height),
            }
        ],
    }


@pytest.fixture
def image_client() -> Generator[dict[str, Any]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        owner = User(email="operation-owner@example.com")
        editor = User(email="operation-editor@example.com")
        viewer = User(email="operation-viewer@example.com")
        outsider = User(email="operation-outsider@example.com")
        session.add_all([owner, editor, viewer, outsider])
        session.flush()
        project = Project(name="Operation project", owner_id=owner.id)
        session.add(project)
        session.flush()
        session.add_all(
            [
                ProjectMember(project_id=project.id, user_id=editor.id, role="editor"),
                ProjectMember(project_id=project.id, user_id=viewer.id, role="viewer"),
            ]
        )
        resource = ProjectResource(
            project_id=project.id,
            name="Image",
            type="pixel_art",
            data={"pixel_art": make_document(), "untouched": {"value": 42}},
        )
        session.add(resource)
        session.commit()

        def override_get_db() -> Generator[Session]:
            yield session

        app.dependency_overrides[get_db] = override_get_db
        url = f"/api/v1/projects/{project.id}/resources/{resource.id}"
        headers = {
            role: {"Authorization": f"Bearer {create_access_token(user.id, timedelta(hours=1))}"}
            for role, user in [
                ("owner", owner),
                ("editor", editor),
                ("viewer", viewer),
                ("outsider", outsider),
            ]
        }
        try:
            with TestClient(app) as client:
                yield {
                    "client": client,
                    "session": session,
                    "url": url,
                    "headers": headers,
                    "resource": resource,
                    "project": project,
                    "editor": editor,
                }
        finally:
            app.dependency_overrides.clear()
    engine.dispose()


def pixel_packet(
    operation_id: str, index: int = 0, color: str | None = "#FF0000", **kwargs: Any
) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "base_revision": 0,
        "width": 2,
        "height": 2,
        "actions": [{"type": "pixels", "layer_id": "base", "changes": [[index, color]]}],
        **kwargs,
    }


def submit(image: dict[str, Any], packet: dict[str, Any], role: str = "owner"):
    return image["client"].post(
        image["url"] + "/image-operations",
        json=packet,
        headers=image["headers"][role],
    )


def detail(image: dict[str, Any]) -> dict[str, Any]:
    return image["client"].get(image["url"], headers=image["headers"]["owner"]).json()


def test_stale_disjoint_pixels_merge_instead_of_replacing_document(image_client):
    first = submit(image_client, pixel_packet("first", 0, "#aabbcc80"))
    second = submit(image_client, pixel_packet("second", 1, "#00FF00"), "editor")
    assert first.status_code == second.status_code == 200
    assert first.json()["applied_revision"] == 1
    resource = second.json()["resource"]
    assert resource["revision"] == 2
    assert resource["data"]["pixel_art"]["layers"][0]["pixels"] == [
        "#AABBCC80",
        "#00FF00",
        None,
        None,
    ]
    assert resource["data"]["pixel_art"]["palette"] == ["#AABBCC80", "#00FF00"]
    assert resource["data"]["untouched"] == {"value": 42}


def test_last_server_accepted_same_pixel_wins(image_client):
    assert submit(image_client, pixel_packet("first")).status_code == 200
    latest = submit(image_client, pixel_packet("second", color="#0000FF"), "editor")
    assert latest.status_code == 200
    assert latest.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"][0] == "#0000FF"


def test_duplicate_after_later_edits_returns_current_state_without_reapplying(image_client):
    packet = pixel_packet("retry")
    first = submit(image_client, packet)
    assert submit(image_client, pixel_packet("later", color="#0000FF"), "editor").status_code == 200
    duplicate = submit(image_client, packet)
    assert duplicate.status_code == 200
    assert duplicate.json()["applied_revision"] == first.json()["applied_revision"] == 1
    assert duplicate.json()["resource"]["revision"] == 2
    assert duplicate.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"][0] == "#0000FF"
    assert len(image_client["session"].exec(select(ImageOperationReceipt)).all()) == 2


def test_duplicate_after_resize_acknowledges_original_packet(image_client):
    packet = pixel_packet("before-resize")
    assert submit(image_client, packet).status_code == 200
    resized = submit(
        image_client,
        pixel_packet(
            "resize",
            base_revision=1,
            actions=[{"type": "replace", "document": make_document(1, 1)}],
        ),
    )
    assert resized.status_code == 200
    duplicate = submit(image_client, packet)
    assert duplicate.status_code == 200
    assert duplicate.json()["applied_revision"] == 1
    assert duplicate.json()["resource"]["revision"] == 2
    assert duplicate.json()["resource"]["data"]["pixel_art"]["width"] == 1


def test_operation_id_reuse_with_different_body_is_rejected(image_client):
    assert submit(image_client, pixel_packet("same-id")).status_code == 200
    collision = submit(image_client, pixel_packet("same-id", color="#0000FF"))
    assert collision.status_code == 409
    assert collision.json()["detail"]["code"] == "image_operation_id_reused"
    assert detail(image_client)["revision"] == 1


def test_operation_identifiers_are_scoped_to_user_and_resource(image_client):
    assert submit(image_client, pixel_packet("same-id", 0)).status_code == 200
    assert submit(image_client, pixel_packet("same-id", 1), "editor").status_code == 200
    image = image_client["resource"]
    second = ProjectResource(
        project_id=image.project_id,
        name="Second",
        type="pixel_art",
        data={"pixel_art": make_document()},
    )
    image_client["session"].add(second)
    image_client["session"].commit()
    response = image_client["client"].post(
        image_client["url"].replace(str(image.id), str(second.id)) + "/image-operations",
        json=pixel_packet("same-id"),
        headers=image_client["headers"]["owner"],
    )
    assert response.status_code == 200
    assert response.json()["applied_revision"] == 1


@pytest.mark.parametrize("role,status", [("viewer", 403), ("outsider", 404)])
def test_only_project_editors_and_owners_can_submit(image_client, role, status):
    assert submit(image_client, pixel_packet("denied"), role).status_code == status
    assert detail(image_client)["revision"] == 0


def test_revoked_editor_cannot_read_state_using_duplicate_receipt(image_client):
    packet = pixel_packet("revoked")
    assert submit(image_client, packet, "editor").status_code == 200
    session = image_client["session"]
    member = session.exec(
        select(ProjectMember).where(ProjectMember.user_id == image_client["editor"].id)
    ).one()
    member.role = "viewer"
    session.add(member)
    session.commit()
    assert submit(image_client, packet, "editor").status_code == 403


def test_conditional_pixel_undo_does_not_overwrite_other_users(image_client):
    paint = pixel_packet(
        "paint",
        actions=[
            {"type": "pixels", "layer_id": "base", "changes": [[0, "#FF0000"], [1, "#FF0000"]]}
        ],
    )
    assert submit(image_client, paint).status_code == 200
    assert submit(image_client, pixel_packet("other", 0, "#0000FF"), "editor").status_code == 200
    undo = submit(
        image_client,
        pixel_packet(
            "undo",
            actions=[
                {
                    "type": "pixels",
                    "layer_id": "base",
                    "changes": [[0, None, "#FF0000"], [1, None, "#FF0000"]],
                }
            ],
        ),
    )
    assert undo.status_code == 200
    assert undo.json()["resource"]["data"]["pixel_art"]["layers"][0]["pixels"] == [
        "#0000FF",
        None,
        None,
        None,
    ]


def test_property_operations_merge_and_conditional_undo_preserves_remote_fields(image_client):
    assert (
        submit(
            image_client,
            pixel_packet(
                "rename",
                actions=[
                    {
                        "type": "layer-update",
                        "layer_id": "base",
                        "fields": {"name": "Mine"},
                    }
                ],
            ),
        ).status_code
        == 200
    )
    assert (
        submit(
            image_client,
            pixel_packet(
                "theirs",
                actions=[
                    {
                        "type": "layer-update",
                        "layer_id": "base",
                        "fields": {"name": "Theirs", "visible": False},
                    }
                ],
            ),
            "editor",
        ).status_code
        == 200
    )
    undo = submit(
        image_client,
        pixel_packet(
            "undo-name",
            actions=[
                {
                    "type": "layer-update",
                    "layer_id": "base",
                    "fields": {"name": "Layer 1", "opacity": 0.5},
                    "expected": {"name": "Mine"},
                }
            ],
        ),
    )
    layer = undo.json()["resource"]["data"]["pixel_art"]["layers"][0]
    assert (layer["name"], layer["visible"], layer["opacity"]) == ("Theirs", False, 0.5)


def test_add_and_reorder_retain_concurrently_added_layers(image_client):
    def add_packet(identifier: str, after: str | None):
        layer = {**make_document()["layers"][0], "id": identifier}
        return pixel_packet(
            "add-" + identifier, actions=[{"type": "layer-add", "layer": layer, "after_id": after}]
        )

    assert submit(image_client, add_packet("middle", "base")).status_code == 200
    assert submit(image_client, add_packet("end", "middle"), "editor").status_code == 200
    ordered = submit(
        image_client,
        pixel_packet("reorder", actions=[{"type": "layer-order", "layer_ids": ["end", "base"]}]),
    )
    assert ordered.status_code == 200
    assert [layer["id"] for layer in ordered.json()["resource"]["data"]["pixel_art"]["layers"]] == [
        "end",
        "middle",
        "base",
    ]


@pytest.mark.parametrize(
    "action,code",
    [
        (
            {"type": "pixels", "layer_id": "missing", "changes": [[0, "#FF0000"]]},
            "image_layer_missing",
        ),
        ({"type": "layer-remove", "layer_id": "base"}, "image_last_layer"),
        ({"type": "layer-order", "layer_ids": ["missing"]}, "image_layer_missing"),
    ],
)
def test_invalid_structural_actions_rollback_entire_packet(image_client, action, code):
    packet = pixel_packet("rollback", actions=[pixel_packet("unused")["actions"][0], action])
    response = submit(image_client, packet)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    current = detail(image_client)
    assert current["revision"] == 0
    assert current["data"]["pixel_art"]["layers"][0]["pixels"] == [None] * 4
    assert image_client["session"].exec(select(ImageOperationReceipt)).all() == []


def test_missing_removed_layer_is_not_blindly_recreated(image_client):
    layer = {**make_document()["layers"][0], "id": "temporary"}
    assert (
        submit(
            image_client,
            pixel_packet(
                "add", actions=[{"type": "layer-add", "layer": layer, "after_id": "base"}]
            ),
        ).status_code
        == 200
    )
    assert (
        submit(
            image_client,
            pixel_packet("remove", actions=[{"type": "layer-remove", "layer_id": "temporary"}]),
            "editor",
        ).status_code
        == 200
    )
    late = submit(
        image_client,
        pixel_packet(
            "late",
            actions=[{"type": "pixels", "layer_id": "temporary", "changes": [[0, "#FF0000"]]}],
        ),
    )
    assert late.status_code == 409
    assert late.json()["detail"]["code"] == "image_layer_missing"
    assert [layer["id"] for layer in detail(image_client)["data"]["pixel_art"]["layers"]] == [
        "base"
    ]


def test_resize_requires_fresh_revision_and_rejects_old_dimension_pixels(image_client):
    assert submit(image_client, pixel_packet("paint")).status_code == 200
    stale = submit(
        image_client,
        pixel_packet(
            "stale-resize", actions=[{"type": "replace", "document": make_document(1, 1)}]
        ),
    )
    assert stale.status_code == 409
    resized = submit(
        image_client,
        pixel_packet(
            "resize",
            base_revision=1,
            actions=[{"type": "replace", "document": make_document(1, 1)}],
        ),
    )
    assert resized.status_code == 200
    old = submit(image_client, pixel_packet("old-paint"), "editor")
    assert old.status_code == 409
    assert old.json()["detail"]["code"] == "image_dimensions_conflict"
    assert detail(image_client)["revision"] == 2


def test_legacy_data_has_stable_layer_identity_and_preserves_pixels(image_client):
    resource = image_client["resource"]
    resource.data = {
        "pixel_art": {"version": 1, "size": 2, "pixels": ["#aabbcc", None, "#12345680", None]}
    }
    image_client["session"].add(resource)
    image_client["session"].commit()
    response = submit(
        image_client,
        pixel_packet(
            "legacy",
            actions=[{"type": "pixels", "layer_id": "legacy-layer-1", "changes": [[1, "#FF0000"]]}],
        ),
    )
    assert response.status_code == 200
    document = response.json()["resource"]["data"]["pixel_art"]
    assert document["version"] == 2
    assert document["layers"][0]["pixels"] == ["#AABBCC", "#FF0000", "#12345680", None]


def test_legacy_full_image_patch_is_blocked_after_operation_but_name_patch_works(image_client):
    assert submit(image_client, pixel_packet("activate")).status_code == 200
    blocked = image_client["client"].patch(
        image_client["url"],
        json={"data": {"pixel_art": make_document()}},
        headers=image_client["headers"]["editor"],
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "image_operations_required"
    renamed = image_client["client"].patch(
        image_client["url"],
        json={"name": "Renamed", "base_revision": 1},
        headers=image_client["headers"]["owner"],
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Renamed"
    assert detail(image_client)["data"]["pixel_art"]["layers"][0]["pixels"][0] == "#FF0000"


@pytest.mark.parametrize(
    "change",
    [
        {"operation_id": "../unsafe"},
        {"operation_id": "x" * 81},
        {"base_revision": -1},
        {"base_revision": True},
        {"base_revision": "0"},
        {"width": 0},
        {"width": 257},
        {"height": 1.5},
        {"actions": []},
        {"actions": [{"type": "pixels", "layer_id": "base", "changes": [[4, "#FF0000"]]}]},
        {"actions": [{"type": "pixels", "layer_id": "base", "changes": [[True, "#FF0000"]]}]},
        {"actions": [{"type": "pixels", "layer_id": "base", "changes": [[0, "red"]]}]},
        {"actions": [{"type": "pixels", "layer_id": "base", "changes": [[0, "#FFF"]]}]},
        {"actions": [{"type": "pixels", "layer_id": "base", "changes": []}]},
        {"actions": [{"type": "layer-update", "layer_id": "base", "fields": {}}]},
        {"actions": [{"type": "layer-update", "layer_id": "base", "fields": {"opacity": 2}}]},
        {"actions": [{"type": "layer-update", "layer_id": "base", "fields": {"visible": None}}]},
        {"actions": [{"type": "layer-order", "layer_ids": ["base", "base"]}]},
        {"unknown_field": "reject"},
    ],
)
def test_invalid_packets_are_bounded_and_do_not_mutate(image_client, change):
    response = submit(image_client, {**pixel_packet("invalid"), **deepcopy(change)})
    assert response.status_code == 422
    assert detail(image_client)["revision"] == 0


def test_replace_cannot_be_mixed_with_pixel_actions(image_client):
    response = submit(
        image_client,
        pixel_packet(
            "mixed",
            actions=[
                {"type": "replace", "document": make_document()},
                pixel_packet("unused")["actions"][0],
            ],
        ),
    )
    assert response.status_code == 422


def test_postgresql_row_lock_compiles_on_canonical_resource():
    statement = select(ProjectResource).where(ProjectResource.id.is_not(None)).with_for_update()
    assert str(statement.compile(dialect=postgresql.dialect())).rstrip().endswith("FOR UPDATE")


def test_journal_commit_failure_rolls_back_the_image_and_receipt(image_client, monkeypatch):
    session = image_client["session"]

    def fail_commit():
        session.flush()
        raise RuntimeError("Simulated transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="Simulated transaction failure"):
            submit(image_client, pixel_packet("failure"))
    assert detail(image_client)["revision"] == 0
    assert detail(image_client)["data"]["pixel_art"]["layers"][0]["pixels"] == [None] * 4
    assert session.exec(select(ImageOperationReceipt)).all() == []
    assert submit(image_client, pixel_packet("failure")).status_code == 200


def test_added_layer_is_validated_before_any_pixel_mutation(image_client):
    invalid_layer = {**make_document()["layers"][0], "id": "short", "pixels": [None]}
    packet = pixel_packet(
        "invalid-add",
        actions=[
            pixel_packet("unused")["actions"][0],
            {"type": "layer-add", "layer": invalid_layer, "after_id": "base"},
        ],
    )
    assert submit(image_client, packet).status_code == 422
    assert detail(image_client)["revision"] == 0


def test_replace_rejects_duplicate_layer_ids(image_client):
    document = make_document()
    document["layers"].append(deepcopy(document["layers"][0]))
    packet = pixel_packet("duplicate-layers", actions=[{"type": "replace", "document": document}])
    assert submit(image_client, packet).status_code == 422
    assert detail(image_client)["revision"] == 0


def test_maximum_actions_is_enforced(image_client):
    packet = pixel_packet("too-many-actions")
    packet["actions"] *= 257
    assert submit(image_client, packet).status_code == 422


def test_aggregate_changed_pixels_is_bounded(image_client, monkeypatch):
    monkeypatch.setattr(image_operations, "MAX_PIXEL_CHANGES", 1)
    packet = pixel_packet(
        "too-many-pixels",
        actions=[
            {
                "type": "pixels",
                "layer_id": "base",
                "changes": [[0, "#FF0000"], [1, "#00FF00"]],
            }
        ],
    )
    assert submit(image_client, packet).status_code == 422


def test_serialized_packet_bytes_are_bounded(image_client, monkeypatch):
    monkeypatch.setattr(image_operations, "MAX_OPERATION_BYTES", 16)
    assert submit(image_client, pixel_packet("too-large")).status_code == 422


def test_non_image_resources_do_not_accept_image_operations(image_client):
    resource = image_client["resource"]
    resource.type = "text"
    image_client["session"].add(resource)
    image_client["session"].commit()
    assert submit(image_client, pixel_packet("wrong-type")).status_code == 422


def test_tile_resources_use_the_same_authoritative_operations(image_client):
    resource = image_client["resource"]
    resource.type = "tileset"
    image_client["session"].add(resource)
    image_client["session"].commit()
    assert submit(image_client, pixel_packet("tile")).status_code == 200


def test_blank_layer_names_cannot_produce_unreadable_canonical_documents(image_client):
    packet = pixel_packet(
        "blank-name",
        actions=[
            {
                "type": "layer-update",
                "layer_id": "base",
                "fields": {"name": "   "},
            }
        ],
    )
    assert submit(image_client, packet).status_code == 422


def test_receipt_resource_foreign_key_declares_cascade_delete(image_client):
    assert submit(image_client, pixel_packet("delete-resource")).status_code == 200
    # SQLite test engines do not enforce cascades by default. The receipt model
    # and additive PostgreSQL migration both explicitly declare ON DELETE CASCADE.
    foreign_key = next(iter(ImageOperationReceipt.__table__.c.resource_id.foreign_keys))
    assert foreign_key.ondelete == "CASCADE"


def test_conditional_layer_add_undo_does_not_delete_other_users_new_pixels(image_client):
    layer = {**make_document()["layers"][0], "id": "added"}
    assert (
        submit(
            image_client,
            pixel_packet(
                "add-layer",
                actions=[
                    {
                        "type": "layer-add",
                        "layer": layer,
                        "after_id": "base",
                    }
                ],
            ),
        ).status_code
        == 200
    )
    assert (
        submit(
            image_client,
            pixel_packet(
                "other-layer-paint",
                actions=[
                    {
                        "type": "pixels",
                        "layer_id": "added",
                        "changes": [[0, "#FF0000"]],
                    }
                ],
            ),
            "editor",
        ).status_code
        == 200
    )
    undo = submit(
        image_client,
        pixel_packet(
            "undo-add-layer",
            actions=[
                {
                    "type": "layer-remove",
                    "layer_id": "added",
                    "expected_layer": layer,
                }
            ],
        ),
    )
    assert undo.status_code == 200
    layers = undo.json()["resource"]["data"]["pixel_art"]["layers"]
    assert [layer["id"] for layer in layers] == ["base", "added"]
    assert layers[1]["pixels"][0] == "#FF0000"


def test_conditional_layer_remove_applies_only_if_snapshot_still_matches(image_client):
    layer = {**make_document()["layers"][0], "id": "added"}
    assert (
        submit(
            image_client,
            pixel_packet(
                "add-layer",
                actions=[
                    {
                        "type": "layer-add",
                        "layer": layer,
                        "after_id": "base",
                    }
                ],
            ),
        ).status_code
        == 200
    )
    undo = submit(
        image_client,
        pixel_packet(
            "undo-add",
            actions=[
                {
                    "type": "layer-remove",
                    "layer_id": "added",
                    "expected_layer": layer,
                }
            ],
        ),
    )
    assert undo.status_code == 200
    assert [layer["id"] for layer in undo.json()["resource"]["data"]["pixel_art"]["layers"]] == [
        "base"
    ]


def test_conditional_layer_order_undo_preserves_remote_reordering(image_client):
    layer = {**make_document()["layers"][0], "id": "added"}
    assert (
        submit(
            image_client,
            pixel_packet(
                "add-layer",
                actions=[
                    {
                        "type": "layer-add",
                        "layer": layer,
                        "after_id": "base",
                    }
                ],
            ),
        ).status_code
        == 200
    )
    assert (
        submit(
            image_client,
            pixel_packet(
                "their-order",
                actions=[
                    {
                        "type": "layer-order",
                        "layer_ids": ["added", "base"],
                    }
                ],
            ),
            "editor",
        ).status_code
        == 200
    )
    undo = submit(
        image_client,
        pixel_packet(
            "undo-order",
            actions=[
                {
                    "type": "layer-order",
                    "layer_ids": ["base", "added"],
                    "expected_layer_ids": ["base", "added"],
                }
            ],
        ),
    )
    assert undo.status_code == 200
    assert [layer["id"] for layer in undo.json()["resource"]["data"]["pixel_art"]["layers"]] == [
        "added",
        "base",
    ]


def test_imported_unicode_long_layer_id_remains_editable_without_reidentification(image_client):
    resource = image_client["resource"]
    identifier = "Mi capa ✨ " + "x" * 512
    document = make_document()
    document["layers"][0]["id"] = identifier
    document["layers"][0]["name"] = "Imported long name " + "y" * 512
    resource.data = {"pixel_art": document}
    image_client["session"].add(resource)
    image_client["session"].commit()
    response = submit(
        image_client,
        pixel_packet(
            "imported-id",
            actions=[
                {
                    "type": "pixels",
                    "layer_id": identifier,
                    "changes": [[0, "#FF0000"]],
                }
            ],
        ),
    )
    assert response.status_code == 200
    assert response.json()["resource"]["data"]["pixel_art"]["layers"][0]["id"] == identifier


def test_oversized_canonical_acknowledgement_rejects_before_commit_or_receipt(
    image_client, monkeypatch
):
    original = detail(image_client)
    monkeypatch.setattr(image_operations, "MAX_CANONICAL_RESPONSE_BYTES", 16)
    response = submit(image_client, pixel_packet("oversized-document"))
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "image_document_too_large"
    assert response.json()["detail"]["current_revision"] == 0
    assert "export a local JSON copy" in response.json()["detail"]["message"]
    current = detail(image_client)
    assert current["revision"] == original["revision"] == 0
    assert current["updated_at"] == original["updated_at"]
    assert current["data"] == original["data"]
    assert image_client["session"].exec(select(ImageOperationReceipt)).all() == []
    monkeypatch.setattr(image_operations, "MAX_CANONICAL_RESPONSE_BYTES", 4_000_000)
    assert submit(image_client, pixel_packet("oversized-document")).status_code == 200


def test_size_guard_counts_full_resource_metadata_and_multibyte_utf8(image_client, monkeypatch):
    resource = image_client["resource"]
    resource.resource_metadata = {"notes": "🖌️" * 200}
    image_client["session"].add(resource)
    image_client["session"].commit()
    # The pixel document alone fits this bound. The metadata and UTF-8 bytes
    # in the actual resource acknowledgement do not.
    pixel_json_bytes = len(
        image_operations.ImageDocument.model_validate(make_document())
        .model_dump_json()
        .encode("utf-8")
    )
    assert pixel_json_bytes < 1_000
    monkeypatch.setattr(image_operations, "MAX_CANONICAL_RESPONSE_BYTES", 1_000)
    response = submit(image_client, pixel_packet("utf8-overflow"))
    assert response.status_code == 413
    assert detail(image_client)["revision"] == 0
    assert image_client["session"].exec(select(ImageOperationReceipt)).all() == []
